"""Execute directory traversal and path-owner errors with the owned runtime."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys

import pytest

from pcc.diagnostics.gc_log import parse_log_lines
from tests.python.owned_regression_support import explicit_owned_runtime
from tests.python.process_timeout import run_process_group_timeout


CONTROLS = Path(__file__).resolve().parents[1] / "fixtures" / "owned_walk"
CASES = (
    ("provider_traversal", "OWNED_WALK_TRAVERSAL_LIFETIME_OK\n"),
    ("scalar_pathlike", "OWNED_ISLINK_PATHLIKE_ERROR_OK\n"),
    ("scalar_predicates", "OWNED_PATH_PREDICATES_VOID_ERROR_OK\n"),
)


@pytest.fixture
def walk_tree(tmp_path):
    root = tmp_path / "tree"
    for directory in ("a/deep", "b", "skip"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    for name in ("root.txt", "a/a.txt", "a/deep/deep.txt", "b/b.txt", "skip/skip.txt"):
        (root / name).write_text(name, encoding="utf-8")
    (root / "link").symlink_to("a", target_is_directory=True)
    return root


def _execute(command, environment):
    result = run_process_group_timeout(command, env=environment, timeout=30)
    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


@pytest.mark.parametrize("name,expected", CASES)
def test_owned_walk_controls_match_reference(name, expected, walk_tree):
    result = _execute(
        [sys.executable, "-B", str(CONTROLS / (name + ".py")), str(walk_tree)],
        dict(os.environ),
    )
    assert result == {"returncode": 0, "stdout": expected, "stderr": ""}


@pytest.mark.integration
@pytest.mark.parametrize("name,expected", CASES)
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_owned_walk_matches_reference_on_observed_collectors(
    name,
    expected,
    walk_tree,
    tmp_path,
    request,
    explicit_owned_runtime,
    python_program_compiler,
    capfd,
):
    source = CONTROLS / (name + ".py")
    binary = tmp_path / name
    oracle = _execute([sys.executable, "-B", str(source), str(walk_tree)], dict(os.environ))
    assert oracle == {"returncode": 0, "stdout": expected, "stderr": ""}
    receipt = {
        "status": "COMPILING",
        "compiler_parameter": request.node.callspec.params["python_program_compiler"],
        "compiler_module": python_program_compiler.__module__,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "archive_sha256": hashlib.sha256(explicit_owned_runtime.read_bytes()).hexdigest(),
        "reference": oracle,
        "executions": [],
    }
    receipt_path = tmp_path / "walk-execution.json"

    def save():
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")

    save()
    try:
        python_program_compiler(
            str(source),
            str(binary),
            backend="self",
            libpython_mode="off",
            ir_scaffold_mode="on",
            runtime_archive=str(explicit_owned_runtime),
        )
    except Exception as error:
        receipt["status"] = "COMPILE_FAILED"
        receipt["error"] = type(error).__name__ + ": " + str(error)
        save()
        raise
    finally:
        output = capfd.readouterr()
        (tmp_path / "compile.stdout").write_text(output.out, encoding="utf-8")
        (tmp_path / "compile.stderr").write_text(output.err, encoding="utf-8")
    receipt["binary_sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
    for backend in range(5):
        log = tmp_path / ("gc" + str(backend) + ".jsonl")
        result = _execute(
            [str(binary), str(walk_tree)],
            dict(
                os.environ,
                PCC_GC_BACKEND=str(backend),
                PCC_GC_REFCOUNT_PROVENANCE_PROBE="2",
                PCC_LOG="gc",
                PCC_LOG_FORMAT="json",
                PCC_LOG_FILE=str(log),
            ),
        )
        observed = []
        if log.is_file():
            observed = sorted({
                event.fields["value1"]
                for event in parse_log_lines(log.read_text(encoding="utf-8").splitlines())
                if event.fields.get("category") == "gc"
                and event.event in ("collect_start", "collect_stop", "collect_end")
            })
        receipt["executions"].append({
            "requested_backend": backend,
            "observed_backends": observed,
            **result,
        })
        receipt["status"] = "RUNNING" if result == oracle and observed == [backend] else "FAILED"
        save()
        assert result == oracle, receipt["executions"][-1]
        assert observed == [backend], receipt["executions"][-1]
    receipt["status"] = "PASS"
    save()
