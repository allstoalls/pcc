"""Owned optimization levels are independent of the retired LLVM vectorizer."""

import os
import subprocess
import sys

import pytest
from pcc.driver import cli_core


@pytest.mark.parametrize("level", [0, 1, 2, 3])
def test_self_backend_honours_requested_optimization_level(monkeypatch, level):
    monkeypatch.delenv("PCC_BACKEND", raising=False)
    assert cli_core._effective_self_backend_opt_level("self", level) == level


@pytest.mark.parametrize("value", ["", "off", "on"])
def test_retired_vectorizer_switch_cannot_change_owned_optimization(monkeypatch, value):
    monkeypatch.setenv("PCC_SELF_BACKEND_VECTORIZE", value)
    assert cli_core._effective_self_backend_opt_level("self", 2) == 2


def test_owned_optimization_emits_no_llvm_clamp_warning(capsys):
    cli_core._warn_if_self_backend_opt_level_clamped("self", 2, 2)
    assert capsys.readouterr().err == ""


def test_owned_c_cli_o2_emits_ir_and_honours_requested_level(tmp_path):
    source = tmp_path / "main.c"
    output = tmp_path / "main.ll"
    source.write_text("int helper(void) {return 42;} int main(void) {return helper();}\n")
    env = dict(os.environ)
    env.pop("PCC_BACKEND", None)
    result = subprocess.run([sys.executable, "-m", "pcc", "--backend", "self", "-O2", "--emit-llvm=" + str(output), str(source)], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert output.is_file() and "define" in output.read_text()
    assert "is using -O0" not in result.stderr
