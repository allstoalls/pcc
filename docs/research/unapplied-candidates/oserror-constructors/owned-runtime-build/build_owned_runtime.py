"""One admitted current-source OSError runtime build through the unchanged owner.

Run only through the reviewed single-process bootstrap and exclusive supervisor.
The payload rejects a provisioning reservation rather than removing it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import resource
import shutil
import stat
import sys
import tempfile
import time
import traceback


GiB = 1024 ** 3
TARGET = "x86_64-unknown-linux-gnu"
CONFIG = {"threads": True, "refcount": "atomic"}
SUPERVISOR_ENV = {"PCC_WORKER_TREE_STATE_PATH", "PCC_WORKER_TREE_BUDGET_BYTES"}
FIXED_ENV = {
    "PCC_DISABLE_ROADMAP_DEEPWIRE": "1",
    "PCC_NO_AUTO_PCC1": "1",
    "PCC_WITH_THREADS": "1",
    "PCC_REFCOUNT_KIND": "atomic",
    "PCC_RUNTIME_BUILD": "owned",
    "PCC_RUNTIME_PYTHON_IR_PASSES": "default",
    "PCC_RUNTIME_IR_PASSES": "mem2reg,sroa,instsimplify,inline-defined,instsimplify,instcombine,dce",
    "PCC_PYTHON_IR_PASSES": "default",
    "PCC_PYTHON_LIBPYTHON": "off",
    "PCC_IR_SCAFFOLD": "on",
    "PCC_SELF_TARGET_PASSES": "default",
    "PCC_SELF_TARGET_PASS_TRANSPORT": "text",
}


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


def check_row(root, row):
    relative = Path(row["path"])
    check(not relative.is_absolute() and ".." not in relative.parts, "unsafe inventory path")
    path = root / relative
    info = path.lstat()
    if row["mode"] == "120000":
        check(stat.S_ISLNK(info.st_mode), "missing inventory symlink: " + str(relative))
        data = os.readlink(path).encode()
        check(len(data) == row["bytes"] and hashlib.sha256(data).hexdigest() == row["sha256"],
              "changed inventory symlink: " + str(relative))
    else:
        check(row["mode"] in ("100644", "100755") and stat.S_ISREG(info.st_mode),
              "unexpected inventory file kind: " + str(relative))
        check(stat.S_IMODE(info.st_mode) == int(row["mode"][-3:], 8),
              "changed inventory mode: " + str(relative))
        check(info.st_size == row["bytes"] and sha256(path) == row["sha256"],
              "changed inventory bytes: " + str(relative))


def verify_tree(root, rows):
    expected = {row["path"] for row in rows}
    check(len(expected) == len(rows), "duplicate inventory paths")
    actual = set()
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in list(dirs):
            path = Path(directory) / name
            if path.is_symlink():
                actual.add(path.relative_to(root).as_posix())
                dirs.remove(name)
        for name in files:
            actual.add((Path(directory) / name).relative_to(root).as_posix())
    check(actual == expected, "inventory path set changed")
    for row in rows:
        check_row(root, row)
    return {"files": len(rows), "bytes": sum(row["bytes"] for row in rows), "status": "PASS"}



def validate_supervisor_state(expected_path):
    """Bind, never clear, the live reservation written by our actual parent."""
    check(os.environ.get("PCC_WORKER_TREE_BUDGET_BYTES") == str(4 * GiB), "supervisor budget differs")
    raw = os.environ.get("PCC_WORKER_TREE_STATE_PATH", "")
    check(raw and Path(raw).is_absolute(), "absolute supervisor state path required")
    path = Path(raw)
    check(not path.is_symlink() and path.resolve(strict=True) == expected_path.resolve(strict=True),
          "supervisor state differs from admitted path")
    check(path.name == "worker-rss.tsv", "unexpected supervisor state filename")
    deadline = time.monotonic() + 2.0
    while True:
        info = path.stat()
        check(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid(), "unowned supervisor state")
        lines = path.read_text(encoding="ascii").splitlines()
        check(len(lines) >= 3 and lines[0] == "pcc.worker-tree-rss.v1", "invalid supervisor state schema")
        check(int(lines[2]) == 4 * GiB, "state budget differs")
        sampled_at = float(lines[1])
        age = time.monotonic() - sampled_at
        check(math.isfinite(sampled_at) and 0 <= age <= 2.0, "stale supervisor state")
        rows = {}
        for line in lines[3:]:
            values = line.split("\t")
            check(len(values) == 3, "malformed supervisor process row")
            pid, parent, rss = map(int, values)
            check(pid > 0 and parent > 0 and rss >= 0 and pid not in rows, "invalid supervisor process row")
            rows[pid] = (parent, rss)
        receipt = json.loads((path.parent / "result.json").read_text())
        check(receipt.get("schema") == "pcc.scoped_lane_guard.v5", "unexpected supervisor receipt")
        check(receipt.get("rss_threshold_bytes") == 4 * GiB and receipt.get("min_free_bytes") == 4 * GiB,
              "supervisor memory/disk admission differs")
        check(receipt.get("timeout_s") == 1200, "supervisor timeout differs")
        if receipt.get("child_pid") == os.getpid() and set(rows) == {os.getpid()}:
            check(receipt.get("child_launched") is True and receipt.get("status") == "RUNNING",
                  "supervisor has not admitted this running root")
            check(rows[os.getpid()][0] == os.getppid(), "supervisor parent differs")
            return {"schema": lines[0], "budget_bytes": 4 * GiB,
                    "root_pid": os.getpid(), "parent_pid": os.getppid(), "status": "PASS"}
        check(time.monotonic() < deadline, "supervisor never recorded this owned root")
        time.sleep(0.02)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--source-manifest", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--worker-state", required=True, type=Path)
    args = parser.parse_args()
    packet = Path(__file__).resolve().parent
    manifest = json.loads((packet / "manifest.json").read_text())
    check(sha256(Path(__file__)) == manifest["driver_sha256"], "driver differs from packet")
    check(sys.flags.isolated and sys.flags.no_site and not sys.flags.optimize and sys.dont_write_bytecode,
          "require isolated -I -S -B host startup")
    check(resource.getrlimit(resource.RLIMIT_NPROC) == (0, 0), "require hard NPROC=0")
    check(resource.getrlimit(resource.RLIMIT_AS) == (4 * GiB, 4 * GiB), "require hard AS=4 GiB")
    source = args.source.resolve(strict=True)
    inventory_path = args.source_manifest.resolve(strict=True)
    check(sha256(inventory_path) == manifest["candidate_source_inventory_sha256"], "source inventory identity changed")
    inventory = json.loads(inventory_path.read_text())
    check(inventory["base_commit"] == manifest["canonical_snapshot_commit"], "source base differs")
    check(inventory["base_tree"] == manifest["canonical_tree"], "source tree differs")
    check("PCC_TEST_NO_NATIVE_PROVISIONING" not in os.environ, "provisioning environment reservation must be absent at admission")
    reservation = source / "build/.pcc-test-no-native-provisioning"
    check(not reservation.exists() and not reservation.is_symlink(), "active on-disk provisioning reservation")
    check(not any(name == "pcc" or name.startswith("pcc.") for name in sys.modules), "PCC was imported before source binding")
    unexpected = sorted(name for name in os.environ if name.startswith("PCC_") and name not in FIXED_ENV and name not in SUPERVISOR_ENV)
    check(not unexpected, "unbound PCC environment keys: " + ",".join(unexpected))
    for name, value in FIXED_ENV.items():
        check(name not in os.environ or os.environ[name] == value, "conflicting bound environment: " + name)
        os.environ[name] = value
    source_before = verify_tree(source, inventory["files"])
    supervisor_before = validate_supervisor_state(args.worker_state)
    supervisor_environment = {name: os.environ[name] for name in SUPERVISOR_ENV}
    output = args.output.resolve()
    check(not output.is_relative_to(source) and not source.is_relative_to(output), "output overlaps source")
    check(not output.is_relative_to(packet) and not packet.is_relative_to(output), "output overlaps packet")
    check(not output.exists(), "fresh output required")
    check(output.parent.is_dir(), "output parent must already exist")
    check(shutil.disk_usage(output.parent).free >= 8 * GiB, "need 4 GiB planned output plus 4 GiB reserve")
    sys.path.insert(0, str(source))
    from pcc.driver.native_provisioning import require_native_provisioning_allowed
    require_native_provisioning_allowed()
    output.mkdir()
    started = time.monotonic()
    result = {"status": "RUNNING", "phase": "copy_runtime_sources", "target": TARGET,
              "runtime_build_config": CONFIG, "candidate_source_inventory_sha256": sha256(inventory_path),
              "source_before": source_before, "supervisor_before": supervisor_before, "bound_environment": FIXED_ENV,
              "limits": manifest["limits"], "native_execution": "UNRUN"}

    def phase(name):
        result.update(phase=name, elapsed_seconds=time.monotonic() - started)
        save_json(output / "result.json", result)

    try:
        phase("copy_runtime_sources")
        runtime = output / "runtime-source"
        runtime.mkdir()
        runtime_rows = []
        for row in inventory["files"]:
            if not row["path"].startswith("pcc/runtime/"):
                continue
            check(row["mode"] == "100644", "runtime subtree must contain only ordinary 0644 files")
            relative = row["path"][len("pcc/runtime/"):]
            copied = dict(row, path=relative)
            destination = runtime / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / row["path"], destination)
            destination.chmod(0o644)
            runtime_rows.append(copied)
        check(len(runtime_rows) == 220 and sum(row["bytes"] for row in runtime_rows) == 5144901,
              "complete runtime source inventory changed")
        result["runtime_source_before"] = verify_tree(runtime, runtime_rows)
        save_json(output / "runtime-source-inventory.json", runtime_rows)
        temporary = output / "tmp"
        temporary.mkdir()
        os.environ["TMPDIR"] = str(temporary)
        tempfile.tempdir = str(temporary)
        # These are the actual, unchanged production owners. No monkeypatch,
        # object worker threshold adjustment, reduced module list or subprocess.
        from pcc.frontends.python import owned_runtime_build as builder
        from pcc.frontends.python.pipeline_targets import host_target_triple
        from pcc.tools import runtime_archive_provenance as provenance
        from pcc.backend.ar_writer import _defined_symbols
        from tests.runtime_fixture_provenance import _verified_test_runtime_archive
        check(host_target_triple() == TARGET, "runtime target differs from actual host ABI")
        check(builder.runtime_build_config() == CONFIG, "runtime configuration differs")
        check(builder.runtime_ir_passes(str(runtime)) == FIXED_ENV["PCC_RUNTIME_IR_PASSES"], "runtime pass list differs")
        names = builder.runtime_modules(str(runtime), TARGET, True)
        check(len(names) == len(set(names)) == 191, "require complete 191-member runtime")
        compiler = provenance.codegen_checksum()
        check(len(compiler) == 64 and all(c in "0123456789abcdef" for c in compiler), "compiler identity unavailable")
        check(compiler == manifest["expected_codegen_checksum"], "compiler differs from exact host-qualified source")
        result.update(codegen_checksum=compiler, expected_members=[name + ".o" for name in names])
        archive = output / "libpy_runtime_pcc_py.a"
        phase("unchanged_production_build_runtime_archive")
        build_started = time.monotonic()
        builder.build_runtime_archive(str(runtime), str(archive), TARGET)
        result["build_seconds"] = time.monotonic() - build_started
        phase("strict_production_archive_admission")
        admitted, receipt = _verified_test_runtime_archive(archive, threads=True, runtime_root=runtime)
        check(admitted == archive, "admitted archive path differs")
        check([row["member"] for row in receipt["members"]] == result["expected_members"], "ordered inventory differs")
        check(receipt["member_count"] == 191, "archive member count differs")
        phase("deep_retained_member_verification")
        members = []
        capi = set()
        objects = Path(str(archive) + ".objects")
        for row in receipt["members"]:
            member = row["member"]
            object_path = objects / member
            ir_path = object_path.with_suffix(".ll")
            receipt_path = Path(str(object_path) + ".provenance.json")
            check(json.loads(receipt_path.read_text()) == row, "retained member receipt differs: " + member)
            check(sha256(object_path) == row["object_sha256"], "retained object differs: " + member)
            check(sha256(ir_path) == row["ir_sha256"], "retained IR differs: " + member)
            check(row["codegen_checksum"] == compiler and row["runtime_build_config"] == CONFIG,
                  "member compiler/config differs: " + member)
            check(row["source"] == "pcc/runtime/py/" + object_path.stem + ".py", "member source differs")
            check(sha256(runtime / "py" / (object_path.stem + ".py")) == row["source_sha256"], "member source bytes differ")
            data = object_path.read_bytes()
            check(data.startswith(b"\x7fELF"), "non-ELF runtime member: " + member)
            _kind, definitions = _defined_symbols(data)
            for symbol in definitions:
                bare = symbol[1:] if symbol.startswith("_") else symbol
                if bare.startswith(("Py", "_Py")):
                    capi.add(symbol)
            members.append({"member": member, "object_bytes": len(data), "object_sha256": row["object_sha256"],
                            "ir_bytes": ir_path.stat().st_size, "ir_sha256": row["ir_sha256"],
                            "receipt_sha256": sha256(receipt_path), "source_sha256": row["source_sha256"]})
            del data
        check(sorted(capi) == receipt["capi_symbols"], "real object C-API definitions differ from inventory")
        save_json(output / "deep-member-verification.json", {"status": "PASS", "members": members})
        phase("postbuild_source_and_environment_seals")
        check(not (runtime / ".pcc-runtime-build.lock").exists(), "builder lock was not released")
        result["runtime_source_after"] = verify_tree(runtime, runtime_rows)
        result["source_after"] = verify_tree(source, inventory["files"])
        check(sha256(inventory_path) == manifest["candidate_source_inventory_sha256"], "source manifest changed")
        require_native_provisioning_allowed()
        check(all(os.environ.get(name) == value for name, value in supervisor_environment.items()),
              "supervisor reservation environment changed")
        result["supervisor_after"] = validate_supervisor_state(args.worker_state)
        for name, value in FIXED_ENV.items():
            check(os.environ.get(name) == value, "builder did not restore environment: " + name)
        foreign = []
        for name, module in list(sys.modules.items()):
            if name == "pcc" or name.startswith("pcc.") or name == "tests" or name.startswith("tests."):
                location = getattr(module, "__file__", None)
                if location is not None and not Path(location).resolve().is_relative_to(source):
                    foreign.append(name)
        check(not foreign, "foreign imported source modules: " + ",".join(foreign))
        check(resource.getrlimit(resource.RLIMIT_AS) == (4 * GiB, 4 * GiB), "AS limit changed")
        check(resource.getrlimit(resource.RLIMIT_NPROC) == (0, 0), "NPROC limit changed")
        check(shutil.disk_usage(output).free >= 4 * GiB, "free-space reserve crossed")
        result.update(status="PASS", archive_sha256=sha256(archive), archive_bytes=archive.stat().st_size,
                      provenance_sha256=sha256(Path(str(archive) + ".provenance.json")),
                      capi_inventory_sha256=sha256(Path(str(archive) + ".capi_syms")),
                      deep_member_verification_sha256=sha256(output / "deep-member-verification.json"),
                      member_count=191, capi_symbol_count=len(capi), imported_source_closure="PASS")
        phase("complete")
        return 0
    except BaseException as exc:
        result.update(status="FAIL", error=type(exc).__name__ + ": " + str(exc)[:2000],
                      elapsed_seconds=time.monotonic() - started)
        save_json(output / "result.json", result)
        (output / "failure.txt").write_text(traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
