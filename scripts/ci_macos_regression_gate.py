#!/usr/bin/env python3
"""Focused Darwin pcc0 gates; never certify Stage1 or a fixed point.

Run under scripts/run_process_tree_sample.py. The workflow bounds the complete
process tree, then collects small receipts even after a timeout. Runtime and
executable artifacts stay on the runner. No test/runtime failure becomes a skip.
"""

from __future__ import annotations

import argparse
import ast
from importlib.metadata import version
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OWNERSHIP_NODE = (
    "tests/python/test_valueclass_aggregate_ownership_native.py::"
    "test_aggregate_ownership_native_five_gc"
)
GC_SWEEP_MODES = (
    "live-transitive", "dead-self-cycle", "self-resurrection",
    "resurrected-finalizer-batch", "gc-start-callback", "nonpositive-budget",
    "partial-budget-finalizer-once",
)
GC_SWEEP_NATIVE_NODES = (
    "tests/python/test_gc_relocation_sweep_verdict.py::"
    "test_relocation_copy_clears_stale_sweep_verdict_backend4[pcc_python]",
    *("tests/python/test_gc_tracing_sweep_verdict_native.py::"
      f"test_pending_verdict_native[gc{backend}-{mode}]"
      for backend in (2, 1, 3, 4) for mode in GC_SWEEP_MODES),
)
GC_SWEEP_HOST_NODES = tuple(
    "tests/python/test_gc_tracing_sweep_verdict.py::" + name for name in (
        "test_live_pending_candidate_is_rechecked_before_finalizer_or_weakref",
        "test_dead_cycle_finalizes_before_clear_and_reclaims_once",
        "test_post_finalizer_recheck_preserves_resurrected_graph_and_weakref",
        "test_finalizers_keep_isolated_batch_semantics_when_one_resurrects_peer",
        "test_nonpositive_budget_does_not_mark_finalize_or_clear[0]",
        "test_nonpositive_budget_does_not_mark_finalize_or_clear[-1]",
        "test_budget_limits_reclamation_without_repeating_finalizers",
        "test_pinned_and_cext_finalizer_exclusions_are_preserved",
        "test_gc4_forwarding_shell_is_excluded_while_unrelated_dead_object_is_swept",
        "test_gc4_selected_but_unforwarded_candidate_still_finalizes_and_reclaims",
    )
)
GATES = (
    ("gc-sweep", GC_SWEEP_NATIVE_NODES + GC_SWEEP_HOST_NODES),
    ("clock", (
        "tests/python/test_worker_state_clock_contract.py::"
        "test_emitted_host_clock_shares_cpython_epoch_across_processes",
        "tests/python/test_worker_state_clock_contract.py::"
        "test_darwin_clock_abi_matches_host_state_after_system_suspend[17924]",
    )),
    # These unchanged programs request all five GC configurations; the shared
    # helper checks native results, not collector-selection telemetry.
    ("oserror", (
        "tests/python/test_os_error_constructors.py::"
        "test_os_error_constructor_native_five_gc[pcc0]",
        "tests/python/test_native_open_errno.py::"
        "test_open_errno_native_five_gc[pcc0]",
        "tests/python/test_native_open_pathlike.py::"
        "test_native_open_accepts_pathlike_like_cpython",
    )),
    ("time", (
        "tests/python/test_owned_darwin_time_runtime.py::"
        "test_darwin_import_time_and_gmtime_execute[pcc0]",
    )),
    ("native-worker-clock", (
        "tests/python/test_native_worker_state_clock.py::"
        "test_native_worker_state_reader_matches_host_clock[pcc0]",
    )),
    ("worker-handles", (
        "tests/python/test_worker_resource_plan.py::"
        "test_resource_acceptance_driver_matches_cpython[handles]",
        "tests/python/test_worker_resource_setpgid.py::"
        "test_darwin_resource_fixture_handles_execute[pcc0]",
    )),
    ("root-joins", tuple(
        OWNERSHIP_NODE + "[pcc0-" + case + "]"
        for case in (
            "typed-list-unbox", "typed-loop-unbox", "typed-comprehension-unbox",
            "original-direct-payload", "original-valuebox",
        )
    )),
    ("indexed-worker-lifetime", (
        "tests/python/test_worker_resource_plan.py::"
        "test_failure_retires_running_peers_and_does_not_launch_tail",
        "tests/python/test_worker_resource_plan.py::"
        "test_progressive_retry_drains_peers_then_restores_concurrency",
        "tests/python/test_worker_resource_plan.py::"
        "test_ready_unknown_head_drains_peer_before_exclusive_calibration",
    )),
    ("indexed-split", (
        "tests/python/test_indexed_process_split_real.py::"
        "test_real_split_and_unsplit_match_complete_objects",
    )),
    ("indexed-handoff-closure", (
        "tests/python/test_indexed_handoff_closed_world.py::"
        "test_handoff_provider_closed_world_aarch64",
    )),
    ("float-protocol", (
        "tests/python/test_native_float_protocol.py::"
        "test_float_protocol_native_five_gc[pcc0]",
    )),
    ("class-docstrings", (
        "tests/python/test_native_class_docstrings.py::"
        "test_class_docstrings_native_five_gc[pcc0]",
    )),
    ("float-callback-movement", (
        "tests/python/test_native_float_callback_movement.py::"
        "test_canonical_float_callback_movement_gc4[pcc0]",
    )),
)
PROFILES = {
    "default": (
        "clock", "runtime-build", "gc-sweep", "oserror", "time", "native-worker-clock",
        "worker-handles", "root-joins",
    ),
    "indexed-split": (
        "indexed-worker-lifetime", "indexed-split", "indexed-handoff-closure",
    ),
    "float-doc": (
        "runtime-build", "float-protocol", "class-docstrings", "float-callback-movement",
    ),
}
EVIDENCE_LIMIT = 16 * 1024 * 1024
RECEIPT_NAMES = {
    "qualification.json", "command.json", "result.json", "runtime-identity.json",
    "live.jsonl", "junit.xml", "ownership-regression.json", "execution.json",
    "darwin-resource-handles.json", "native-worker-clock.json", "reference.json",
    "float-callback-movement.json", "build.json",
    *(f"gc{backend}-{mode}.json" for backend in (2, 1, 3, 4) for mode in GC_SWEEP_MODES),
    "indexed-process-split.json", "indexed-handoff-closed-world.json",
    *(f"gc{backend}.json" for backend in range(5)),
}


def _save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_execution_report(path, expected):
    """Require every exact node to execute, including setup and teardown."""
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    starts = [row for row in rows if row["event"] == "start"]
    finishes = [row for row in rows if row["event"] == "finish"]
    collections = [row["nodeids"] for row in rows if row["event"] == "collected"]
    assert len(starts) == 1 and not starts[0]["collect_only"], "collection is not execution"
    assert collections == [list(expected)], ("missing/reordered gates", collections, expected)
    assert not any(row["event"] == "deselected" for row in rows), "a required gate was deselected"
    assert len(finishes) == 1 and finishes[0]["exitstatus"] == 0, "pytest did not finish successfully"
    assert finishes[0]["testscollected"] == len(expected)
    assert finishes[0]["testsfailed"] == 0
    reports = [row for row in rows if row["event"] == "report"]
    assert len(reports) == 3 * len(expected), "missing or duplicate execution reports"
    for node in expected:
        actual = [row for row in reports if row["nodeid"] == node]
        assert [row["when"] for row in actual] == ["setup", "call", "teardown"], node
        assert all(row["outcome"] == "passed" and "wasxfail" not in row for row in actual), actual
    return list(expected)


def _command(command, directory, environment):
    directory.mkdir(parents=True, exist_ok=True)
    _save(directory / "command.json", {
        "command": command,
        "worker_memory_guard": {
            key: environment.get(key, "") for key in (
                "PCC_WORKER_TREE_BUDGET_BYTES", "PCC_WORKER_TREE_STATE_PATH",
            )
        },
    })
    with (directory / "target.stdout").open("wb") as stdout, \
            (directory / "target.stderr").open("wb") as stderr:
        return subprocess.run(command, cwd=ROOT, env=environment,
                              stdout=stdout, stderr=stderr).returncode


def verify_gc_sweep_xdist(directory, expected):
    """The owned GC controls must use the unchanged project xdist defaults."""
    lines = {line.strip() for line in (directory / "target.stdout").read_text().splitlines()}
    assert "created: 6/6 workers" in lines, "six actual xdist workers were not created"
    assert f"6 workers [{len(expected)} items]" in lines, "six worker collections did not complete"
    assert "scheduling tests via LoadGroupScheduling" in lines, "default scheduler was not used"
    rows = [json.loads(line) for line in (directory / "live.jsonl").read_text().splitlines()]
    start, = [row for row in rows if row["event"] == "start"]
    assert not start["override_ini"]
    assert Path(start["inifile"]).resolve() == ROOT / "pyproject.toml"
    assert start["markexpr"] == "integration or not integration" and not start["keyword"]
    return verify_execution_report(directory / "live.jsonl", expected)


def _pytest_gate(name, nodes, out, environment):
    directory = out / "work" / name
    if name == "gc-sweep":
        # Admit the unmarked host models/relocation probe and integration matrix,
        # without overriding the project's six-worker/loadgroup defaults.
        options = ["-x", "-m", "integration or not integration", "--color=no"]
        environment = dict(environment, PYTEST_ADDOPTS="", PCC_PYTEST_AUTO_CLEAN="0")
    else:
        options = ["-o", "addopts=", "-x", "-n0"]
    command = [
        sys.executable, "-m", "pytest", *options,
        "-vv", "--tb=short", "-p", "scripts.pytest_live_report",
        "--pcc-live-report", str(directory / "live.jsonl"),
        "--junitxml", str(directory / "junit.xml"),
        "--basetemp", str(directory / "tests"), *nodes,
    ]
    code = _command(command, directory, environment)
    assert code == 0, f"{name}: pytest exited {code}; see {directory}/target.stderr and target.stdout"
    if name == "gc-sweep":
        return verify_gc_sweep_xdist(directory, nodes)
    return verify_execution_report(directory / "live.jsonl", nodes)


HANDOFF_TEST = "tests/python/test_pipeline_indexed_handoff.py"


def _handoff_expected_nodes():
    tree = ast.parse((ROOT / HANDOFF_TEST).read_text())
    owner, = [node for node in tree.body
              if isinstance(node, ast.ClassDef) and node.name == "HandoffContractTests"]
    names = sorted(node.name for node in owner.body
                   if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"))
    assert len(names) == 29, "update the explicit handoff xdist gate census"
    return [HANDOFF_TEST + "::HandoffContractTests::" + name for name in names]


def verify_handoff_xdist(directory, expected):
    """Cross-check normal xdist output against durable controller and JUnit reports."""
    stdout = (directory / "target.stdout").read_text()
    lines = {line.strip() for line in stdout.splitlines()}
    # xdist emits the second line only after every worker finishes collection.
    assert "created: 6/6 workers" in lines, "six actual xdist workers were not created"
    assert f"6 workers [{len(expected)} items]" in lines, "six worker collections did not complete"
    assert "scheduling tests via LoadGroupScheduling" in lines, "default scheduler was not used"
    rows = [json.loads(line) for line in (directory / "live.jsonl").read_text().splitlines()]
    start, = [row for row in rows if row["event"] == "start"]
    finish, = [row for row in rows if row["event"] == "finish"]
    assert not start["collect_only"] and not start["override_ini"]
    assert Path(start["inifile"]).resolve() == ROOT / "pyproject.toml"
    assert start["markexpr"] == "not integration" and not start["keyword"]
    assert not any(row["event"] in {"deselected", "collection"} for row in rows)
    assert [row["nodeids"] for row in rows if row["event"] == "collected"] == [expected]
    assert finish["exitstatus"] == 0 and finish["testsfailed"] == 0
    assert finish["testscollected"] == len(expected)
    reports = [row for row in rows if row["event"] == "report"]
    assert {row["nodeid"] for row in reports} == set(expected)
    assert all(row["outcome"] == "passed" and "wasxfail" not in row for row in reports)
    calls = 0
    for node in expected:
        phases = [row["when"] for row in reports if row["nodeid"] == node]
        assert phases[0] == "setup" and phases[-1] == "teardown", (node, phases)
        assert len(phases) >= 3 and all(phase == "call" for phase in phases[1:-1])
        calls += len(phases) - 2
    xml = ET.parse(directory / "junit.xml")
    # pytest 9 includes successful subreports in suite.tests, not extra testcase nodes.
    assert not any(list(xml.iter(tag)) for tag in ("failure", "error", "skipped"))
    actual = [(case.get("classname"), case.get("name")) for case in xml.iter("testcase")]
    wanted = [(HANDOFF_TEST.removesuffix(".py").replace("/", ".") + ".HandoffContractTests",
               node.rsplit("::", 1)[1]) for node in expected]
    assert len(actual) == len(wanted) and set(actual) == set(wanted)
    return {"worker_count": 6, "scheduler": "loadgroup", "executed_nodes": expected,
            "extra_successful_call_reports": calls - len(expected)}


def run_handoff_xdist(out):
    """Run the exact host file with normal project addopts, under the outer guard."""
    assert sys.platform == "darwin" and platform.machine() == "arm64"
    directory = out / "work" / "handoff-xdist"
    expected = _handoff_expected_nodes()
    environment = dict(os.environ)
    environment.pop("LC_ALL", None)
    environment.update(PYTEST_ADDOPTS="", PCC_PYTEST_AUTO_CLEAN="0",
                       PCC_NO_AUTO_PCC1="1", PCC_TEST_NO_NATIVE_PROVISIONING="1")
    receipt = {"schema": "pcc.handoff-default-xdist.v1", "status": "RUNNING",
               "source_commit": environment.get("GITHUB_SHA", ""),
               "test_sha256": _sha256(ROOT / HANDOFF_TEST),
               "uv_lock_sha256": _sha256(ROOT / "uv.lock"),
               "versions": {name: version(name) for name in ("pytest", "pytest-xdist", "execnet")},
               "expected_nodes": expected,
               "scope": "real default-xdist host contracts; no compiler or native execution"}
    _save(directory / "result.json", receipt)
    command = ["uv", "run", "--frozen", "pytest", "-x", "-vv", "--tb=short", "--color=no",
               "-p", "scripts.pytest_live_report", "--pcc-live-report", str(directory / "live.jsonl"),
               "--junitxml", str(directory / "junit.xml"), "--basetemp", str(directory / "tests"),
               HANDOFF_TEST]
    try:
        receipt["returncode"] = _command(command, directory, environment)
        assert receipt["returncode"] == 0, "handoff default-xdist pytest failed; inspect complete logs"
        receipt.update(verify_handoff_xdist(directory, expected))
    except Exception as error:
        receipt.update(status="FAIL", error=str(error))
        _save(directory / "result.json", receipt)
        raise
    receipt["status"] = "PASS"
    _save(directory / "result.json", receipt)


def run(out, profile="default"):
    if profile not in PROFILES:
        raise ValueError("unknown Mac regression profile: " + str(profile))
    order = PROFILES[profile]
    assert sys.platform == "darwin" and platform.machine() == "arm64", "Darwin arm64 execution is required"
    assert not os.environ.get("PCC_TEST_COMPILER"), "these gates require the host pcc0 compiler"
    environment = dict(os.environ)
    environment.pop("LC_ALL", None)
    environment.update(PCC_NO_AUTO_PCC1="1", PCC_TEST_COMPILER_STRICT="1")
    if profile == "indexed-split":
        # The pair toggles only its own compiler children. No runtime is needed
        # for PCO equality or actual provider closed-world lowering/emission.
        environment.update(PCC_HOST_INDEXED_PROCESS_SPLIT="0",
                           PCC_TEST_NO_NATIVE_PROVISIONING="1")
    receipt = {
        "schema": "pcc.macos-regression-ci.v1", "status": "RUNNING",
        "profile": profile,
        "source_commit": environment.get("GITHUB_SHA", ""),
        "scope": "host pcc0 self/off emitted execution; Stage1 is a separate job",
        "gates": {name: {"status": "NOT_RUN", "expected_nodes": list(dict(GATES).get(name, ()))}
                  for name in order},
    }
    if profile == "default":
        receipt["gates"]["gc-sweep"].update(
            native_case_count=len(GC_SWEEP_NATIVE_NODES),
            host_case_count=len(GC_SWEEP_HOST_NODES),
        )
        receipt["clock_scope"] = "real host/native clock epoch; simulated suspend; emitted native worker-state reader"
    elif profile == "indexed-split":
        receipt["scope"] = (
            "host pcc0 split/unsplit complete object equality and real worker lifetime; "
            "actual handoff provider no-libpython ARM lowering/emission; "
            "no native helper execution, runtime or GC qualification"
        )
    else:
        receipt["collector_scope"] = (
            "float protocol and class docstrings request GC0-4 without collector-selection telemetry; "
            "float callback movement asserts actual GC4 and positive selection/relocation "
            "in both callback and cleanup"
        )
    receipt_path = out / "work" / "qualification.json"
    _save(receipt_path, receipt)
    current = order[0]
    try:
        for name in order:
            current = name
            receipt["gates"][name]["status"] = "RUNNING"
            _save(receipt_path, receipt)
            print("MACOS_REGRESSION_START " + name, flush=True)
            if name == "runtime-build":
                environment.update(
                    PCC_WITH_THREADS="1", PCC_REFCOUNT_KIND="atomic",
                    PCC_RUNTIME_BUILD="owned", PCC_RUNTIME_CC="pcc",
                    PCC_RUNTIME_HIGH="py", PCC_PYTHON_IR_PASSES="off",
                    PCC_SELF_LINK="pcc", PCC_SELF_OBJ="pcc",
                    PCC_IR_TO_OBJ_EMITTER="pcc",
                )
                archive = out / "runtime" / "libpy_runtime_pcc_py.a"
                code = _command([
                    sys.executable, "-m", "pcc.frontends.python.owned_runtime_build",
                    "--output", str(archive),
                ], out / "work" / name, environment)
                assert code == 0, f"owned threaded runtime build exited {code}"
                from tests.runtime_fixture_provenance import _verified_test_runtime_archive
                checked, manifest = _verified_test_runtime_archive(archive, threads=True)
                assert checked == archive
                assert all(member["runtime_build_config"] == {
                    "threads": True, "refcount": "atomic",
                } for member in manifest["members"])
                _save(out / "work" / name / "runtime-identity.json", {
                    "archive_sha256": _sha256(archive),
                    "provenance_sha256": _sha256(Path(str(archive) + ".provenance.json")),
                    "capi_inventory_sha256": _sha256(Path(str(archive) + ".capi_syms")),
                    "target": manifest["target_triple"],
                    "member_count": manifest["member_count"],
                    "config": {"threads": True, "refcount": "atomic"},
                    "codegen_sha256": sorted({member["codegen_checksum"] for member in manifest["members"]}),
                })
                environment.update(PCC_RUNTIME_ARCHIVE=str(archive),
                                   PCC_THREADED_RUNTIME_ARCHIVE=str(archive),
                                   PCC_TEST_NO_NATIVE_PROVISIONING="1")
            else:
                nodes = dict(GATES)[name]
                executed = _pytest_gate(name, nodes, out, environment)
                receipt["gates"][name]["executed_nodes"] = executed
            receipt["gates"][name]["status"] = "PASS"
            _save(receipt_path, receipt)
            print("MACOS_REGRESSION_PASS " + name, flush=True)
    except Exception as error:
        receipt["gates"][current].update(status="FAIL", error=str(error))
        receipt["status"] = "FAIL"
        _save(receipt_path, receipt)
        raise
    receipt["status"] = "PASS"
    _save(receipt_path, receipt)


def collect_evidence(out):
    """Compress complete selected receipts, never archive binaries or IR.

    The cap is explicit and fail-closed. If compressed receipts exceed 16 MiB,
    the index names and hashes the omitted files; no truncated log is presented
    as complete. Full originals remain on the runner for that job's lifetime.
    """
    destination = out / "evidence"
    destination.mkdir(parents=True, exist_ok=True)
    index, used, omitted = [], 0, False
    for path in sorted((out / "work").rglob("*")):
        if not path.is_file() or not (
            path.name in RECEIPT_NAMES or path.suffix in {".stdout", ".stderr", ".tsv"}
        ):
            continue
        relative = path.relative_to(out / "work")
        target = destination / relative.with_name(relative.name + ".gz")
        target.parent.mkdir(parents=True, exist_ok=True)
        with path.open("rb") as source, gzip.open(target, "wb") as output:
            shutil.copyfileobj(source, output)
        size = target.stat().st_size
        record = {"source": str(relative), "bytes": path.stat().st_size,
                  "sha256": _sha256(path), "compressed_bytes": size}
        if used + size > EVIDENCE_LIMIT:
            target.unlink()
            record["status"] = "OMITTED_EVIDENCE_LIMIT"
            omitted = True
        else:
            used += size
            record.update(status="COMPLETE", artifact=str(target.relative_to(destination)))
        index.append(record)
    _save(destination / "index.json", {"limit_bytes": EVIDENCE_LIMIT,
          "compressed_bytes": used, "status": "LIMIT_EXCEEDED" if omitted else "COMPLETE",
          "files": index})
    assert not omitted, "Evidence exceeded the 16 MiB compressed cap; index.json identifies omitted complete files"
    assert index, "No regression evidence was produced"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--profile", choices=tuple(PROFILES), default="default")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--collect-evidence", action="store_true")
    mode.add_argument("--handoff-xdist", action="store_true")
    args = parser.parse_args(argv)
    out = args.out_dir.resolve()
    if args.collect_evidence:
        collect_evidence(out)
    elif args.handoff_xdist:
        run_handoff_xdist(out)
    else:
        run(out, profile=args.profile)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
