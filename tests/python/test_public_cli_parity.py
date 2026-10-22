from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _entry_commands():
    return ([str(Path(sys.executable).parent / "pcc")], [sys.executable, "-m", "pcc"])


@pytest.mark.parametrize("args", [["--help"], ["env", "info", "--json"], ["--unknown-option"]])
def test_installed_console_and_module_entry_match(args):
    results = [
        subprocess.run(command + args, cwd=ROOT, capture_output=True, timeout=20)
        for command in _entry_commands()
    ]
    assert (results[0].returncode, results[0].stdout, results[0].stderr) == (
        results[1].returncode, results[1].stdout, results[1].stderr
    )


def test_public_module_runner_never_interprets_module_with_runpy(monkeypatch):
    import runpy
    import pcc.driver.cli_bootstrap as bootstrap
    import pcc.driver.cli_launcher as launcher

    calls = []
    monkeypatch.setattr(runpy, "run_module", lambda *a, **k: pytest.fail("host interpretation"))
    monkeypatch.setattr(bootstrap, "_run_compiled_python_module_from_pcc1",
                        lambda name, args: calls.append((name, args)) or 23)
    assert launcher.main(["-m", "demo", "left", "--right"]) == 23
    assert calls == [("demo", ["left", "--right"])]


def test_c_request_enters_full_frontend_in_process(monkeypatch):
    import pcc.driver.cli_bootstrap as bootstrap
    import pcc.driver.cli_core as core
    import pcc.driver.cli_launcher as launcher

    calls = []
    monkeypatch.setenv("PCC_HOST_PYTHON", "/usr/bin/false")
    monkeypatch.setenv("PCC_HOST_PCC", "/usr/bin/false")
    monkeypatch.delenv("PCC_BACKEND", raising=False)
    monkeypatch.setattr(bootstrap, "_bootstrap_subprocess_run",
                        lambda *a, **k: pytest.fail("host C delegation"))
    monkeypatch.setattr(core, "cli_main", lambda args: calls.append(args) or 19)
    assert launcher.main(["--separate-tus", "project"]) == 19
    assert calls == [["--separate-tus", "project"]]


def test_sync_help_reaches_tool_dispatch_from_public_module():
    result = subprocess.run(
        [sys.executable, "-m", "pcc", "sync", "--help"],
        cwd=ROOT, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "usage: pcc sync" in result.stdout
    assert "--lock" in result.stdout
    assert "Pcc - an owned C and Python compiler" not in result.stdout


def test_core_dispatch_preserves_tool_arguments_and_exit_status(monkeypatch):
    import pcc.driver.cli_bootstrap as bootstrap
    import pcc.package.uv_lock_sync as sync

    calls = []
    monkeypatch.setattr(sync, "main", lambda args: calls.append(args) or 23)
    assert bootstrap.bootstrap_cli_main(["sync", "--lock", "project.lock"]) == 23
    assert calls == [["--lock", "project.lock"]]


def test_python_output_and_program_arguments_do_not_select_c(monkeypatch, tmp_path):
    import pcc.driver.cli_bootstrap as bootstrap

    calls = []
    monkeypatch.delenv("PCC_BACKEND", raising=False)
    monkeypatch.setattr(bootstrap, "_compile_python", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(bootstrap, "_run_c_cli", lambda args: pytest.fail("misclassified as C"))
    source = tmp_path / "program.py"
    source.write_text("print(42)\n")
    assert bootstrap.bootstrap_cli_main(["-o", str(tmp_path / "output.c"), str(source)]) == 0
    assert calls[0][1]["backend"] == "self"


def test_explicit_c_backend_is_not_overwritten(monkeypatch):
    import pcc.driver.cli_bootstrap as bootstrap
    import pcc.driver.cli_core as core

    captured = []
    monkeypatch.setattr(core, "cli_main", lambda args: captured.append(args) or 0)
    assert bootstrap.bootstrap_cli_main(["--backend", "self", "input.c"]) == 0
    parsed, status, error = core.parse_cli_args(captured[0])
    assert status == 0, error
    assert parsed[-7] == "self"


def test_removed_llvm_backend_is_rejected_for_c():
    from pcc.driver.cli_core import parse_cli_args

    parsed, status, error = parse_cli_args(["--backend", "llvm", "input.c"])
    assert parsed is None
    assert status == 2
    assert "expected self" in error


@pytest.mark.parametrize("argv, expected", [
    (["-o", "out.c", "input.py"], "input.py"),
    (["-oout.c", "input.py"], "input.py"),
    (["--cpp-arg=-DVALUE=7", "input.c"], "input.c"),
    (["--target", "x86_64-unknown-linux-gnu", "input.py"], "input.py"),
    (["--emit-llvm", "out.ll", "input.c"], "input.c"),
    (["--emit-llvm", "input.py"], "input.py"),
    (["--", "input.c", "--argument"], "input.c"),
    (["input.py", "--", "argument.c"], "input.py"),
    (["--separate-tus", "project"], "project"),
    (["--backend"], ""),
])
def test_input_classification_respects_compiler_and_program_arguments(argv, expected):
    from pcc.driver.cli_contract import cli_input_path

    assert cli_input_path(argv) == expected


def test_full_python_compile_options_survive_public_dispatch(monkeypatch, tmp_path):
    import pcc.driver.cli_launcher as launcher
    from pcc.frontends.python import pipeline

    calls = []
    monkeypatch.delenv("PCC_BACKEND", raising=False)
    monkeypatch.setattr(pipeline, "compile_python", lambda *args, **kwargs: calls.append(kwargs))
    source = tmp_path / "input.py"
    source.write_text("print(1)\n")
    assert launcher.main([
        "--target", "x86_64-unknown-linux-gnu", "--link-arg=-lm",
        "--gpu-backend", "none", str(source), "-o", str(tmp_path / "app"),
    ]) == 0
    assert calls[0]["target_triple"] == "x86_64-unknown-linux-gnu"
    assert calls[0]["link_args"] == ["-lm"]
    assert calls[0]["backend"] == "self"


def test_pass_options_are_applied_not_discarded(monkeypatch, tmp_path):
    import pcc.driver.cli_launcher as launcher
    from pcc.frontends.python import pipeline, pipeline_pass_config

    calls = []
    monkeypatch.delenv("PCC_PYTHON_IR_PASSES", raising=False)
    monkeypatch.setattr(pipeline, "compile_python", lambda *args, **kwargs: calls.append(
        pipeline_pass_config.resolve_python_ir_pass_names(default_raw="default")
    ))
    source = tmp_path / "input.py"
    source.write_text("print(1)\n")
    assert launcher.main(["--pass", "dce", "--disable-pass", "dce", str(source), "-o", str(tmp_path / "out")]) == 0
    assert calls == [[]]
    assert "PCC_PYTHON_IR_PASSES" not in os.environ


def test_python_pass_override_filters_and_restores_existing_selection(monkeypatch):
    from pcc.driver.cli_core import _python_ir_pass_env_overrides, _temporary_env
    from pcc.frontends.python.pipeline_pass_config import resolve_python_ir_pass_names

    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "mem2reg,sroa,dce")
    with _temporary_env(_python_ir_pass_env_overrides([], ["mem2reg"])):
        assert resolve_python_ir_pass_names() == ["sroa", "dce"]
    assert os.environ["PCC_PYTHON_IR_PASSES"] == "mem2reg,sroa,dce"


def test_python_pass_override_rejects_retired_external_pass():
    from pcc.driver.cli_core import _python_ir_pass_env_overrides

    with pytest.raises(ValueError, match="unsupported owned Python IR pass"):
        _python_ir_pass_env_overrides(["licm"], [])
