#!/usr/bin/env python3
"""Bounded host-pcc0 ordering execution with the admitted Stage1 runtime.

Reuse the existing archive and 300-second/default-xdist guard. This gate never
builds Stage1 or a runtime, and does not qualify pcc1 or a self-host fixed point.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.ci_worker_size_prior_gate import (
    environment,
    run_pytest,
    runtime_identity,
    save,
    sha,
    source_identity,
)
from tests.python.test_native_ordering_semantics import (
    COLLECTOR_PROBE,
    NATIVE_PROGRAM,
    PROGRAM,
    SOURCE_INPUTS,
    _verify_collector_output,
)


NAME = "native-ordering"
TIMEOUT_S = 300
NODE = "tests/python/test_native_ordering_semantics.py::test_native_ordering_matches_cpython"
INPUTS = (
    *SOURCE_INPUTS,
    "tests/python/test_user_ordering_exhaustion.py",
    "tests/test_ci_native_ordering_gate.py",
    "scripts/ci_native_ordering_gate.py",
    "scripts/ci_worker_size_prior_gate.py",
    "scripts/ci_native_object_regressions.py",
    "tests/runtime_fixture_provenance.py",
    "tests/python/conftest.py",
    "tests/conftest.py",
    "conftest.py",
    "pyproject.toml",
    ".github/workflows/pcc1-package-parity.yml",
)


def verify_execution_rows(record, runtime):
    rows = record["executions"]
    assert [row["requested_gc_backend"] for row in rows] == list(range(5))
    reference = record["reference"]
    assert reference["returncode"] == 0 and reference["stderr"] == ""
    for backend, row in enumerate(rows):
        assert type(row["requested_gc_backend"]) is int
        assert type(row["observed_gc_backend"]) is int
        assert row["runtime_before"] == row["runtime_after"] == runtime
        assert row["returncode"] == 0 and row["stderr"] == ""
        assert row["observed_gc_backend"] == _verify_collector_output(
            row["stdout"], backend, reference["stdout"],
        )
    return rows


def verify_receipt(directory, runtime, inputs):
    import hashlib
    from scripts.ci_native_object_regressions import verify_native_image

    paths = list(directory.rglob("native-ordering.json"))
    assert len(paths) == 1, "missing or duplicate native ordering receipt"
    path = paths[0]
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["schema"] == "pcc.native-ordering.v1" and record["status"] == "PASS"
    assert record["compiler_mode"] == "host-pcc0"
    assert record["backend"] == "self" and record["libpython"] == "off"
    assert record["runtime"] == runtime
    assert record["source_inputs"] == {name: inputs[name] for name in SOURCE_INPUTS}
    native_source = path.parent / "ordering.py"
    oracle_source = path.parent / "ordering-oracle.py"
    binary = path.parent / "ordering.out"
    assert native_source.read_text(encoding="utf-8") == NATIVE_PROGRAM
    assert oracle_source.read_text(encoding="utf-8") == PROGRAM
    assert record["native_source_sha256"] == sha(native_source)
    assert record["oracle_source_sha256"] == sha(oracle_source)
    assert record["probe_sha256"] == hashlib.sha256(COLLECTOR_PROBE.encode()).hexdigest()
    assert record["binary_sha256"] == sha(binary)
    rows = verify_execution_rows(record, runtime)
    assert all(row["command"] == [str(binary)] for row in rows)
    return {"receipt": str(path), **record, "image": verify_native_image(binary)}


def run(out):
    out = Path(out).resolve()
    receipt_path = out / (NAME + ".json")
    assert not receipt_path.exists(), "refuse a stale native ordering gate receipt"
    record = {
        "schema": "pcc.native-ordering-gate.v1", "status": "RUNNING",
        "compiler_mode": "host-pcc0", "qualifies_pcc1": False,
        "nodes": [NODE], "timeout_s": TIMEOUT_S,
        "collector_evidence": "backend selection observed; no collection/movement witness",
    }
    save(receipt_path, record)
    try:
        env = environment()
        selected = env.get("PCC_RUNTIME_ARCHIVE")
        assert selected, "reuse the admitted Stage1 runtime; never provision another"
        archive = Path(selected)
        assert archive.is_absolute() and archive.is_file()
        source = source_identity()
        assert source["clean_commit"] is True and source["commit"] == os.environ["GITHUB_SHA"]
        stage = json.loads((out / "stage1.json").read_text(encoding="utf-8"))
        assert stage["phase"] == "stage1" and stage["status"] in ("PASS", "FAIL")
        assert stage["source"] == source
        runtime = runtime_identity(archive)
        assert runtime == stage["runtime"]
        inputs = {name: sha(ROOT / name) for name in INPUTS}
        record.update(source=source, runtime=runtime, test_inputs=inputs)
        save(receipt_path, record)
        env.update(PCC_TEST_NO_NATIVE_PROVISIONING="1", UV_PYTHON_DOWNLOADS="never")
        try:
            # The peer fixture is unmarked. An integration-only invocation
            # would deselect it; exact default selection must execute this node.
            run_pytest(out, NAME, [NODE], env, integration=False, timeout=TIMEOUT_S)
            record["program"] = verify_receipt(out / NAME, runtime, inputs)
        finally:
            record["source_after"] = source_identity()
            record["runtime_after"] = runtime_identity(archive)
            assert record["source_after"] == source
            assert record["runtime_after"] == runtime
            assert {name: sha(ROOT / name) for name in INPUTS} == inputs
        record["status"] = "PASS"
    except BaseException as error:
        record.update(status="FAIL", error=type(error).__name__ + ": " + str(error))
        raise
    finally:
        save(receipt_path, record)
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    run(args.out_dir)


if __name__ == "__main__":
    main()
