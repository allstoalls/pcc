"""Architecture contracts for the extracted pcc1 pytest harness."""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).absolute().parents[2]
CLI_SOURCE = REPO_ROOT / "pcc" / "driver" / "cli_bootstrap.py"
HARNESS_SOURCE = REPO_ROOT / "pcc" / "driver" / "cli_bootstrap_pytest.py"


def test_cli_keeps_both_native_pytest_request_spellings() -> None:
    import pcc.driver.cli_bootstrap as cli

    assert cli._is_pytest_request(["--pytest"])
    assert cli._is_pytest_request(["pytest"])
    assert not cli._is_pytest_request([])
    assert not cli._is_pytest_request(["-m", "package"])


def test_cli_pytest_facade_forwards_current_stage_and_timeout(monkeypatch) -> None:
    import pcc.driver.cli_bootstrap as cli

    observed = {}

    def fake_harness(argv, executable, timeout_seconds):
        observed["argv"] = argv
        observed["executable"] = executable
        observed["timeout_seconds"] = timeout_seconds
        return 23

    monkeypatch.setattr(cli, "_run_pcc1_pytest_harness", fake_harness)
    monkeypatch.setattr(cli, "_bootstrap_subprocess_timeout_seconds", lambda: 41)
    monkeypatch.setattr(cli.sys, "executable", "/tmp/current-pcc1")
    argv = ["--pytest", "tests/python", "-q", "-n0"]

    assert cli._run_pytest_from_pcc1(argv) == 23
    assert observed == {
        "argv": argv,
        "executable": "/tmp/current-pcc1",
        "timeout_seconds": 41,
    }


def test_extracted_harness_launcher_uses_supplied_stage_and_bounded_runs(
    monkeypatch,
    tmp_path: Path,
) -> None:
    import pcc.driver.cli_bootstrap_pytest as harness

    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_sample.py").write_text(
        "def test_sample() -> None:\n"
        "    assert 2 + 2 == 4\n",
        encoding="utf-8",
    )
    calls = []

    def fake_run(cmd, *, check, timeout):
        calls.append((cmd, check, timeout))
        if cmd[:2] == ["mkdir", "-p"]:
            Path(cmd[2]).mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(harness.subprocess, "run", fake_run)
    monkeypatch.setenv("TMPDIR", str(tmp_path))

    status = harness.run_pcc1_pytest(
        ["--pytest", str(tests_dir), "-q", "-n0"],
        "/tmp/current-pcc1",
        37,
    )

    assert status == 0
    assert len(calls) == 3
    assert calls[1][0][0] == "/tmp/current-pcc1"
    assert calls[1][0][-2:] == ["--python-libpython=off", "--ir-scaffold=on"]
    assert calls[2][0][0].endswith(".out")
    assert all(check is True and timeout == 37 for _, check, timeout in calls)


def test_pytest_harness_source_is_owned_by_sibling_and_facade_stays_thin() -> None:
    cli_tree = ast.parse(CLI_SOURCE.read_text(encoding="utf-8"))
    harness_tree = ast.parse(HARNESS_SOURCE.read_text(encoding="utf-8"))
    cli_defs = {
        node.name: node
        for node in cli_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    harness_defs = {
        node.name
        for node in harness_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }

    assert not any(name.startswith("_pcc1_pytest_") for name in cli_defs)
    assert "_pytest_marker_arg" not in cli_defs
    assert "_pytest_path_args" not in cli_defs
    assert {
        "_pcc1_pytest_collect_files",
        "_pcc1_pytest_discover_funcs",
        "_pcc1_pytest_write_runner_source",
        "run_pcc1_pytest",
    } <= harness_defs

    facade = cli_defs["_run_pytest_from_pcc1"]
    executable_body = list(facade.body)
    if ast.get_docstring(facade) is not None:
        executable_body = executable_body[1:]
    assert len(executable_body) == 1
    assert isinstance(executable_body[0], ast.Return)
    call = executable_body[0].value
    assert isinstance(call, ast.Call)
    assert isinstance(call.func, ast.Name)
    assert call.func.id == "_run_pcc1_pytest_harness"

    facade_imports = [
        node
        for node in cli_tree.body
        if isinstance(node, ast.ImportFrom)
        and node.level == 0
        and node.module == "pcc.driver.cli_bootstrap_pytest"
    ]
    assert len(facade_imports) == 1
    assert [
        (alias.name, alias.asname) for alias in facade_imports[0].names
    ] == [("run_pcc1_pytest", "_run_pcc1_pytest_harness")]

    harness_from_imports = {
        (node.level, node.module)
        for node in harness_tree.body
        if isinstance(node, ast.ImportFrom)
    }
    harness_plain_imports = {
        alias.name
        for node in harness_tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert harness_from_imports == {(0, "__future__")}
    assert harness_plain_imports == {"os", "subprocess", "sys"}


def test_repo_main_closure_and_static_exports_include_pytest_harness() -> None:
    from pcc.frontends.python.codegen.layer1_support import (
        _default_native_module_exports,
    )
    from pcc.frontends.python.pipeline import (
        _collect_relative_module_closure,
        _filter_ir_scaffold_closure,
    )

    entry = REPO_ROOT / "pcc" / "__main__.py"
    sources, modules = _collect_relative_module_closure(
        str(entry),
        include_same_package_absolute=True,
        recurse_same_package_absolute=True,
    )
    sources, modules = _filter_ir_scaffold_closure(
        sources,
        modules,
        ir_scaffold_mode="on",
    )

    index = modules.index("pcc.driver.cli_bootstrap_pytest")
    assert Path(sources[index]).resolve() == HARNESS_SOURCE.resolve()
    exports = _default_native_module_exports("pcc.driver.cli_bootstrap_pytest")
    assert exports is not None
    signature = exports["pcc.driver.cli_bootstrap_pytest"]["run_pcc1_pytest"]
    assert signature["kind"] == "function"
    assert signature["return_ty"] == ("int",)
    assert signature["param_types"] == (
        ("dyn",),
        ("str",),
        ("int", 64, True),
    )
