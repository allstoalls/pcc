"""Compile the unchanged OSError constructor fixture; execution is separate."""
from __future__ import annotations

import argparse
import ast
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import resource
import shutil
import stat
import sys
import tempfile
import time
import traceback


GiB = 1024 ** 3
TARGET = "x86_64-unknown-linux-gnu"
FIXTURE = "tests/python/test_os_error_constructors.py"
SUPERVISOR_ENV = {"PCC_WORKER_TREE_STATE_PATH", "PCC_WORKER_TREE_BUDGET_BYTES"}
TARGET_ENV = ("PCC_SELF_TARGET_PASSES", "PCC_SELF_TARGET_PASS_TRANSPORT")
FIXED_ENV = {
    "PCC_DISABLE_ROADMAP_DEEPWIRE": "1", "PCC_NO_AUTO_PCC1": "1",
    "PCC_TEST_NO_NATIVE_PROVISIONING": "1", "PCC_WITH_THREADS": "1",
    "PCC_REFCOUNT_KIND": "atomic", "PCC_RUNTIME_CC": "pcc",
    "PCC_RUNTIME_HIGH": "py", "PCC_SELF_LINK": "pcc", "PCC_SELF_OBJ": "pcc",
    "PCC_IR_TO_OBJ_EMITTER": "pcc", "PCC_PYTHON_IR_PASSES": "off",
    "PCC_DISABLE_PY_RUN_CACHE": "1", "PCC_HOST_PYTHON": "/nonexistent/host-python",
    "PCC_HOST_PCC": "/nonexistent/host-pcc", "PCC_IR_SCAFFOLD": "on",
    "PCC_PYTHON_LIBPYTHON": "off",
}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def check(condition, message):
    if not condition:
        raise RuntimeError(message)


def save_json(path, value):
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


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
        relative = Path(row["path"])
        check(not relative.is_absolute() and ".." not in relative.parts, "unsafe inventory path")
        path = root / relative
        info = path.lstat()
        if row["mode"] == "120000":
            check(stat.S_ISLNK(info.st_mode), "inventory symlink changed")
            data = os.readlink(path).encode()
            check(len(data) == row["bytes"] and hashlib.sha256(data).hexdigest() == row["sha256"],
                  "inventory symlink bytes changed")
        else:
            check(row["mode"] in ("100644", "100755") and stat.S_ISREG(info.st_mode),
                  "inventory file kind changed: " + str(relative))
            check(stat.S_IMODE(info.st_mode) == int(row["mode"][-3:], 8), "inventory mode changed")
            check(info.st_size == row["bytes"] and digest(path) == row["sha256"],
                  "inventory bytes changed: " + str(relative))
    return {"status": "PASS", "files": len(rows), "bytes": sum(row["bytes"] for row in rows)}


def check_supervisor(path, manifest):
    check(os.environ.get("PCC_WORKER_TREE_BUDGET_BYTES") == str(4 * GiB), "wrong live budget")
    check(Path(os.environ["PCC_WORKER_TREE_STATE_PATH"]).resolve(strict=True) == path,
          "supervisor state differs")
    check(path.name == "worker-rss.tsv" and not path.is_symlink(), "unexpected supervisor state")
    deadline = time.monotonic() + 2.0
    while True:
        info = path.stat()
        check(stat.S_ISREG(info.st_mode) and info.st_uid == os.geteuid(), "unowned supervisor state")
        lines = path.read_text(encoding="ascii").splitlines()
        check(len(lines) >= 3 and lines[0] == "pcc.worker-tree-rss.v1", "wrong supervisor schema")
        age = time.monotonic() - float(lines[1])
        check(math.isfinite(age) and 0 <= age <= 2.0 and int(lines[2]) == 4 * GiB,
              "stale or different supervisor budget")
        rows = [tuple(map(int, line.split("\t"))) for line in lines[3:]]
        receipt = json.loads((path.parent / "result.json").read_text())
        check(receipt.get("schema") == "pcc.scoped_lane_guard.v5", "wrong guard schema")
        check(receipt.get("supervisor_sha256") == manifest["supervisor_sha256"], "wrong supervisor")
        check(receipt.get("timeout_s") == 300 and receipt.get("rss_threshold_bytes") == 4 * GiB
              and receipt.get("min_free_bytes") == 4 * GiB and receipt["lock"]["exclusive"],
              "wrong guard limits or lock")
        if receipt.get("child_pid") == os.getpid() and len(rows) == 1 and rows[0][:2] == (os.getpid(), os.getppid()):
            check(len(rows[0]) == 3 and rows[0][2] >= 0, "invalid supervisor root row")
            check(receipt.get("child_launched") is True and receipt.get("status") == "RUNNING",
                  "supervisor root not admitted")
            return {"status": "PASS", "pid": os.getpid(), "ppid": os.getppid()}
        check(time.monotonic() < deadline, "supervisor did not publish this root")
        time.sleep(0.02)


def original_program(path):
    tree = ast.parse(path.read_text(), filename=str(path))
    assignments = [node for node in tree.body if isinstance(node, ast.Assign)
                   and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
                   and node.targets[0].id == "PROGRAM"]
    check(len(assignments) == 1, "original PROGRAM assignment count changed")
    raw = ast.literal_eval(assignments[0].value)
    check(isinstance(raw, str), "original PROGRAM is not a string literal")
    return raw, {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--runtime-output", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker-state", type=Path, required=True)
    args = parser.parse_args()
    packet = Path(__file__).resolve().parent
    manifest = json.loads((packet / "manifest.json").read_text())
    source = args.source.resolve(strict=True)
    inventory_path = args.source_manifest.resolve(strict=True)
    runtime_output = args.runtime_output.resolve(strict=True)
    runtime = runtime_output / "libpy_runtime_pcc_py.a"
    runtime_sources = runtime_output / "runtime-source"
    program = packet / "program.py"
    check(digest(Path(__file__)) == manifest["driver_sha256"], "driver changed")
    check(sys.flags.isolated and sys.flags.no_site and not sys.flags.optimize and sys.dont_write_bytecode,
          "require -I -S -B without optimization")
    check(resource.getrlimit(resource.RLIMIT_AS) == (4 * GiB, 4 * GiB)
          and resource.getrlimit(resource.RLIMIT_NPROC) == (0, 0), "wrong hard limits")
    check(digest(inventory_path) == manifest["source_manifest_sha256"], "source manifest changed")
    inventory = json.loads(inventory_path.read_text())
    check(inventory["base_commit"] == manifest["base_commit"] and inventory["base_tree"] == manifest["base_tree"],
          "source base changed")
    source_before = verify_tree(source, inventory["files"])
    check(digest(source / FIXTURE) == manifest["fixture_sha256"] and digest(program) == manifest["program_sha256"],
          "fixture or extracted program changed")
    exact_program, messages = original_program(source / FIXTURE)
    check(program.read_text() == exact_program, "frozen program differs from the original host fixture")
    required = manifest["runtime_required"]
    check(digest(runtime_output / "result.json") == required["result_sha256"], "runtime result changed")
    built = json.loads((runtime_output / "result.json").read_text())
    check(built["status"] == "PASS" and built["member_count"] == 191, "runtime was not qualified")
    for name in ("archive_sha256", "provenance_sha256", "codegen_checksum"):
        check(built[name] == required[name], "runtime identity differs: " + name)
    check(built["candidate_source_inventory_sha256"] == manifest["source_manifest_sha256"], "runtime source differs")
    check(built["target"] == TARGET and built["runtime_build_config"] == {"threads": True, "refcount": "atomic"},
          "runtime target/configuration differs")
    check(built["source_after"]["status"] == built["runtime_source_after"]["status"] == "PASS", "runtime seals failed")
    check(digest(runtime) == required["archive_sha256"] and runtime.stat().st_size == required["archive_bytes"],
          "runtime archive changed")
    check(digest(Path(str(runtime) + ".provenance.json")) == required["provenance_sha256"], "runtime provenance changed")
    check(digest(Path(str(runtime) + ".capi_syms")) == built["capi_inventory_sha256"], "C API inventory changed")
    runtime_inventory = runtime_output / "runtime-source-inventory.json"
    check(digest(runtime_inventory) == required["source_inventory_sha256"], "runtime source inventory changed")
    runtime_rows = json.loads(runtime_inventory.read_text())
    runtime_before = verify_tree(runtime_sources, runtime_rows)
    check(not any(name == "pcc" or name.startswith("pcc.") for name in sys.modules), "PCC imported before binding")
    extra = {"PCC_RUNTIME_DIR": str(runtime_sources), "PCC_RUNTIME_ARCHIVE": str(runtime)}
    check(not [name for name in os.environ if name.startswith("PCC_") and name not in set(FIXED_ENV) | set(extra) | SUPERVISOR_ENV],
          "unbound PCC environment")
    # The original fixture does not set these. Require the admitted ordinary
    # default selection, then record what the production resolver actually uses.
    check(all(name not in os.environ for name in TARGET_ENV), "target-pass environment differs from admission")
    target_environment = {name: os.environ.get(name) for name in TARGET_ENV}
    for name, value in {**FIXED_ENV, **extra}.items():
        check(name not in os.environ or os.environ[name] == value, "conflicting environment: " + name)
        os.environ[name] = value
    os.environ["PATH"] = ""
    worker_state = args.worker_state.resolve(strict=True)
    supervisor_before = check_supervisor(worker_state, manifest)
    supervisor_environment = {name: os.environ[name] for name in SUPERVISOR_ENV}
    output = args.output.resolve()
    check(all(not output.is_relative_to(root) and not root.is_relative_to(output)
              for root in (source, packet, runtime_output)), "output overlaps an input")
    check(output.parent.is_dir() and not output.exists(), "fresh output required")
    check(shutil.disk_usage(output.parent).free >= 4 * GiB + 512 * 1024 ** 2, "insufficient output reserve")
    output.mkdir()
    temporary = output / "tmp"
    temporary.mkdir()
    os.environ["TMPDIR"] = str(temporary)
    tempfile.tempdir = str(temporary)
    started = time.monotonic()
    result = {"status": "RUNNING", "arm": "candidate", "native_execution": "UNRUN",
              "source_manifest_sha256": manifest["source_manifest_sha256"], "program_sha256": digest(program),
              "runtime_sha256": digest(runtime), "codegen_checksum": built["codegen_checksum"], "target": TARGET,
              "source_before": source_before, "runtime_source_before": runtime_before,
              "supervisor_before": supervisor_before, "bound_environment": {**FIXED_ENV, **extra},
              "target_pass_environment": target_environment, "messages": messages,
              "locale_environment": {name: os.environ.get(name) for name in ("LANG", "LC_ALL", "LC_MESSAGES")}}

    def phase(name):
        result.update(phase=name, elapsed_seconds=time.monotonic() - started)
        save_json(output / "result.json", result)

    try:
        phase("unchanged_same_process_reference")
        stdout, stderr = io.StringIO(), io.StringIO()
        namespace = {"__name__": "__main__", "__file__": str(program)}
        saved_argv = sys.argv
        try:
            sys.argv = [str(program)]
            with redirect_stdout(stdout), redirect_stderr(stderr):
                exec(compile(exact_program, str(program), "exec"), namespace)
        finally:
            sys.argv = saved_argv
        oracle = {"mode": "same-process CPython", "stdout": stdout.getvalue(), "stderr": stderr.getvalue()}
        save_json(output / "reference.json", oracle)
        check((oracle["stdout"], oracle["stderr"]) == (manifest["expected_stdout"], ""), "reference output differs")
        check(not any(temporary.iterdir()), "reference left temporary files")
        del namespace
        sys.path.insert(0, str(source))
        phase("imports_and_strict_runtime_admission")
        from pcc.frontends.python import pipeline
        from pcc.frontends.python.codegen.layer1 import L1CodeGen
        from pcc.frontends.python.pipeline_targets import host_target_triple
        from pcc.backend.self_backend_target_passes import resolve_self_target_pass_names, resolve_self_target_pass_transport
        from pcc.tools.runtime_archive_provenance import codegen_checksum
        from tests.runtime_fixture_provenance import _verified_test_runtime_archive
        from tests.owned_ir_validation import verify_ir_text
        check(host_target_triple() == TARGET and codegen_checksum() == required["codegen_checksum"], "compiler/target changed")
        admitted, receipt = _verified_test_runtime_archive(runtime, threads=True, runtime_root=runtime_sources)
        check(admitted == runtime and receipt["member_count"] == 191, "strict runtime admission differs")
        transport = resolve_self_target_pass_transport()
        result["effective_target_passes"] = {"transport": transport, "names": resolve_self_target_pass_names(transport=transport)}
        closures, generated = [], []
        raw_closure = pipeline._prepare_multi_source_compile_closure
        raw_generate = L1CodeGen.generate

        def observe_closure(*positional, **keywords):
            paths, names = raw_closure(*positional, **keywords)
            row = {"sources": [str(Path(path).resolve()) for path in paths], "modules": list(names),
                   "recursive_stdlib": keywords["recursive_stdlib"]}
            closures.append(row)
            save_json(output / "discovered-closure.json", closures)
            check(row["sources"] == [str(program)] and row["modules"] == ["program"] and row["recursive_stdlib"] is True,
                  "ordinary import discovery expanded beyond admitted fixture")
            return paths, names

        def observe_generate(self, module=None):
            text = raw_generate(self, module)
            generated.append({"source": self._module_source_path, "target": str(self.module.triple), "text": str(text)})
            return text

        profile = {}
        executable = output / "program.out"
        try:
            pipeline._prepare_multi_source_compile_closure = observe_closure
            L1CodeGen.generate = observe_generate
            phase("unchanged_public_compile_python")
            pipeline.compile_python(str(program), str(executable), backend="self", libpython_mode="off",
                                    ir_scaffold_mode="on", runtime_archive=str(runtime), target_triple=TARGET, profile=profile)
        finally:
            pipeline._prepare_multi_source_compile_closure = raw_closure
            L1CodeGen.generate = raw_generate
            save_json(output / "compiler-profile.json", profile)
        phase("actual_ir_and_source_seals")
        check(len(closures) == len(generated) == 1, "unexpected generated closure")
        capture = generated[0]
        check(capture["source"] == str(program) and capture["target"] == TARGET, "generated owner/target differs")
        text = capture["text"]
        (output / "actual-generated.ll").write_text(text)
        check("strict.nolib.stub" not in text and not re.search(r"\bcall[^\n]*@py_cpy_", text), "host fallback in actual IR")
        check(re.search(r"\bcall[^\n]*@py_obj_call_slots\(", text)
              and re.search(r"\bcall[^\n]*@pcc_gc_collect\(i32 -1\)", text),
              "expected actual object-call/GC callsites missing")
        verify_ir_text(text)
        check(executable.is_file() and stat.S_IMODE(executable.stat().st_mode) == 0o755, "executable absent or wrong mode")
        with executable.open("rb") as stream:
            check(stream.read(4) == b"\x7fELF", "expected ELF")
        result["source_after"] = verify_tree(source, inventory["files"])
        result["runtime_source_after"] = verify_tree(runtime_sources, runtime_rows)
        check(digest(inventory_path) == manifest["source_manifest_sha256"] and digest(program) == manifest["program_sha256"],
              "source manifest or fixture changed")
        check(digest(runtime) == required["archive_sha256"] and digest(Path(str(runtime) + ".provenance.json")) == required["provenance_sha256"],
              "runtime archive/provenance changed")
        check(codegen_checksum() == required["codegen_checksum"], "compiler changed")
        for name, value in {**FIXED_ENV, **extra, **supervisor_environment, **target_environment}.items():
            check(os.environ.get(name) == value, "environment changed: " + name)
        origins = {}
        for name, module in tuple(sys.modules.items()):
            if name == "pcc" or name.startswith("pcc.") or name == "tests" or name.startswith("tests."):
                location = getattr(module, "__file__", None)
                if location is not None:
                    path = Path(location).resolve()
                    check(path.is_relative_to(source), "foreign import: " + name)
                    origins[name] = path.relative_to(source).as_posix()
        result["imported_module_paths"] = origins
        result["supervisor_after"] = check_supervisor(worker_state, manifest)
        check(resource.getrlimit(resource.RLIMIT_AS) == (4 * GiB, 4 * GiB)
              and resource.getrlimit(resource.RLIMIT_NPROC) == (0, 0), "hard limits changed")
        check(shutil.disk_usage(output).free >= 4 * GiB, "free-space reserve crossed")
        result.update(status="PASS", executable_sha256=digest(executable), executable_bytes=executable.stat().st_size,
                      generated_ir_sha256=digest(output / "actual-generated.ll"), generated_ir_bytes=len(text.encode()),
                      frontend_route="public compile_python/full ordinary discovery", libpython="off",
                      imported_source_closure="PASS", reference="PASS")
        phase("complete")
    except BaseException as exc:
        result.update(status="FAIL", error=type(exc).__name__ + ": " + str(exc)[:2000])
        phase("failed")
        (output / "failure.txt").write_text(traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
