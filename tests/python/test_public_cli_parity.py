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
    import pcc.cli_bootstrap as bootstrap
    import pcc.cli_launcher as launcher

    calls = []
    monkeypatch.setattr(runpy, "run_module", lambda *a, **k: pytest.fail("host interpretation"))
    monkeypatch.setattr(bootstrap, "_run_compiled_python_module_from_pcc1",
                        lambda name, args: calls.append((name, args)) or 23)
    assert launcher.main(["-m", "demo", "left", "--right"]) == 23
    assert calls == [("demo", ["left", "--right"])]


def test_c_request_enters_full_frontend_in_process(monkeypatch):
    import pcc.cli_bootstrap as bootstrap
    import pcc.cli_core as core
    import pcc.cli_launcher as launcher

    calls = []
    monkeypatch.setenv("PCC_HOST_PYTHON", "/usr/bin/false")
    monkeypatch.setenv("PCC_HOST_PCC", "/usr/bin/false")
    monkeypatch.delenv("PCC_BACKEND", raising=False)
    monkeypatch.setattr(bootstrap, "_bootstrap_subprocess_run",
                        lambda *a, **k: pytest.fail("host C delegation"))
    monkeypatch.setattr(core, "cli_main", lambda args: calls.append(args) or 19)
    assert launcher.main(["--separate-tus", "project"]) == 19
    assert calls == [["--backend", "self", "--separate-tus", "project"]]


def test_python_output_and_program_arguments_do_not_select_c(monkeypatch, tmp_path):
    import pcc.cli_bootstrap as bootstrap

    calls = []
    monkeypatch.delenv("PCC_BACKEND", raising=False)
    monkeypatch.setattr(bootstrap, "_compile_python", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(bootstrap, "_run_c_cli", lambda args: pytest.fail("misclassified as C"))
    source = tmp_path / "program.py"
    source.write_text("print(42)\n")
    assert bootstrap.bootstrap_cli_main(["-o", str(tmp_path / "output.c"), str(source)]) == 0
    assert calls[0][1]["backend"] == "self"


def test_explicit_c_backend_is_not_overwritten(monkeypatch):
    import pcc.cli_bootstrap as bootstrap
    import pcc.cli_core as core

    captured = []
    monkeypatch.setattr(core, "cli_main", lambda args: captured.append(args) or 0)
    assert bootstrap.bootstrap_cli_main(["--backend", "llvm", "input.c"]) == 0
    parsed, status, error = core.parse_cli_args(captured[0])
    assert status == 0, error
    assert parsed[-7] == "llvm"


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
    from pcc.cli_contract import cli_input_path

    assert cli_input_path(argv) == expected


def test_full_python_compile_options_survive_public_dispatch(monkeypatch, tmp_path):
    import pcc.cli_core as core
    import pcc.cli_launcher as launcher

    calls = []
    monkeypatch.delenv("PCC_BACKEND", raising=False)
    monkeypatch.setattr(core, "execute_cli", lambda **kwargs: calls.append(kwargs) or 0)
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
    import pcc.cli_core as core
    import pcc.cli_launcher as launcher

    calls = []
    monkeypatch.setattr(core, "execute_cli", lambda **kwargs: calls.append(kwargs) or 0)
    source = tmp_path / "input.py"
    source.write_text("print(1)\n")
    assert launcher.main(["--disable-pass", "dce", str(source)]) == 0
    assert calls[0]["disabled_passes"] == ["dce"]
