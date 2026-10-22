"""Unit tests for the --ir-scaffold flag plumbing (Path A / Issue 1).

Current contract: the flag is parsed and threaded through to codegen,
and AUTO/default resolves to ON for the closed-world self-host path.
Explicit OFF remains available as the historical compatibility mode.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).absolute().parents[2]


@pytest.fixture
def simple_py_src(tmp_path):
    src = tmp_path / "smoketest.py"
    src.write_text(
        textwrap.dedent(
            """
            def add(a: int, b: int) -> int:
                return a + b

            print(add(3, 4))
            """
        )
    , encoding="utf-8")
    return str(src)


def _emit_ll(src_path: str, out_path: str, *, mode: str | None) -> int:
    cmd = [sys.executable, "-m", "pcc"]
    if mode is not None:
        cmd.extend(["--ir-scaffold", mode])
    cmd.extend([src_path, "-o", out_path, "--emit-llvm"])
    result = subprocess.run(
        cmd,
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.returncode


def test_resolve_ir_scaffold_mode_default_on():
    from pcc.frontends.python.pipeline import _resolve_ir_scaffold_mode

    assert _resolve_ir_scaffold_mode(None) == "on"
    assert _resolve_ir_scaffold_mode("") == "on"


def test_resolve_ir_scaffold_mode_off():
    from pcc.frontends.python.pipeline import _resolve_ir_scaffold_mode

    assert _resolve_ir_scaffold_mode("off") == "off"
    assert _resolve_ir_scaffold_mode("OFF") == "off"


def test_resolve_ir_scaffold_mode_on():
    from pcc.frontends.python.pipeline import _resolve_ir_scaffold_mode

    assert _resolve_ir_scaffold_mode("on") == "on"
    assert _resolve_ir_scaffold_mode("ON") == "on"
    assert _resolve_ir_scaffold_mode("1") == "on"
    assert _resolve_ir_scaffold_mode("yes") == "on"
    assert _resolve_ir_scaffold_mode("true") == "on"


def test_resolve_ir_scaffold_mode_auto_aliases_on():
    from pcc.frontends.python.pipeline import _resolve_ir_scaffold_mode

    assert _resolve_ir_scaffold_mode("auto") == "on"


def test_resolve_ir_scaffold_mode_invalid_raises():
    from pcc.frontends.python.pipeline import (
        PyPipelineError,
        _resolve_ir_scaffold_mode,
    )

    with pytest.raises(PyPipelineError):
        _resolve_ir_scaffold_mode("garbage")


def test_resolve_ir_scaffold_mode_env_default():
    from pcc.frontends.python.pipeline import _resolve_ir_scaffold_mode

    saved = os.environ.get("PCC_IR_SCAFFOLD")
    try:
        os.environ["PCC_IR_SCAFFOLD"] = "on"
        assert _resolve_ir_scaffold_mode(None) == "on"
        os.environ["PCC_IR_SCAFFOLD"] = "off"
        assert _resolve_ir_scaffold_mode(None) == "off"
        os.environ.pop("PCC_IR_SCAFFOLD", None)
        assert _resolve_ir_scaffold_mode(None) == "on"
    finally:
        if saved is None:
            os.environ.pop("PCC_IR_SCAFFOLD", None)
        else:
            os.environ["PCC_IR_SCAFFOLD"] = saved


def test_l1_codegen_constructor_accepts_mode():
    """L1CodeGen accepts ir_scaffold_mode and stores it. Invalid value
    raises ValueError immediately at construction time."""
    from pcc.frontends.python.codegen import layer1
    from pcc.frontends.python.py_ast import Module

    empty = Module(name="empty", body=[])

    cg = layer1.L1CodeGen(empty, ir_scaffold_mode="off")
    assert cg.ir_scaffold_mode == "off"

    cg_on = layer1.L1CodeGen(empty, ir_scaffold_mode="on")
    assert cg_on.ir_scaffold_mode == "on"

    cg_default = layer1.L1CodeGen(empty)
    assert cg_default.ir_scaffold_mode == "off"

    with pytest.raises(ValueError):
        layer1.L1CodeGen(empty, ir_scaffold_mode="garbage")


def test_cli_default_on_equals_explicit_on(simple_py_src, tmp_path):
    """The implicit default (no flag) must be byte-identical to
    explicit --ir-scaffold=on."""
    out_default = str(tmp_path / "default.ll")
    out_on = str(tmp_path / "on.ll")
    assert _emit_ll(simple_py_src, out_default, mode=None) == 0
    assert _emit_ll(simple_py_src, out_on, mode="on") == 0

    with open(out_default, "rb") as f:
        a = f.read()
    with open(out_on, "rb") as f:
        b = f.read()
    assert a == b, "default mode must equal explicit --ir-scaffold=on"


def test_cli_on_does_not_crash_phase1(simple_py_src, tmp_path):
    """In Phase 1 (no lowering wired yet), ON mode should still
    successfully compile this trivial program — the flag is only
    plumbing at this point. Behavioural divergence comes in later
    phases as individual IRBuilder methods are migrated."""
    out_on = str(tmp_path / "on.ll")
    assert _emit_ll(simple_py_src, out_on, mode="on") == 0
    assert os.path.getsize(out_on) > 0


def test_cli_invalid_mode_rejected(simple_py_src, tmp_path):
    out = str(tmp_path / "bad.ll")
    rc = _emit_ll(simple_py_src, out, mode="garbage")
    assert rc != 0, "invalid mode must fail with non-zero exit"
