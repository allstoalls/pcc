"""Temporary print argument roots unwind through the surrounding handler.

Multi-argument native print constructs its tuple before evaluating every later
operand and ``sep``/``end`` expression.  A failure in one of those expressions
must leave the temporary LIFO GC root before the exceptional edge joins the
handler (or the function error epilogue).  The self backend's precise stack-map
analysis is the structural oracle; executing the result proves that cleanup did
not bypass the source-level ``try`` target.
"""
from __future__ import annotations

import os
import subprocess

import pytest

from pcc.backend.self_backend_dispatch import emit_self_asm
from pcc.frontends.python.pipeline import compile_python


pytestmark = pytest.mark.xdist_group(name="pcc_heavy_llvm")


SOURCE = """
def fail_fixed() -> int:
    raise RuntimeError("fixed")

def fail_sep() -> str:
    raise RuntimeError("sep")

def main() -> None:
    try:
        print("prefix", fail_fixed())
    except RuntimeError:
        print("caught-fixed")

    values = [1, 2]
    try:
        print(*values, sep=fail_sep())
    except RuntimeError:
        print("caught-splat")

    print("done")

main()
"""


def test_print_operand_cleanup_has_consistent_self_stackmap(tmp_path):
    source = tmp_path / "print_exception_cleanup.py"
    source.write_text(SOURCE, encoding="utf-8")
    llvm_ir = tmp_path / "print_exception_cleanup.ll"
    compile_python(
        str(source),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    ir_text = llvm_ir.read_text(encoding="utf-8")

    # The self backend performs precise root-state analysis while emitting
    # assembly.  Before the fix this raises at try.done/err.exit instead.
    emit_self_asm(ir_text)
    assert "print.args.pcc.cleanup" in ir_text
    assert "print.splat.pcc.cleanup" in ir_text
    assert ir_text.count("@pcc_gc_frame_enter_lifo") >= 2
    assert ir_text.count("@pcc_gc_frame_leave_lifo") >= 4


def test_print_operand_errors_leave_lifo_roots_and_reach_handler(
    tmp_path,
    pcc_runtime_archive,
):
    source = tmp_path / "print_exception_cleanup.py"
    source.write_text(SOURCE, encoding="utf-8")
    executable = tmp_path / "print_exception_cleanup"
    environment = os.environ.copy()
    environment.pop("LC_ALL", None)
    environment["PCC_RUNTIME_CC"] = "pcc"
    environment["PCC_RUNTIME_ARCHIVE"] = str(pcc_runtime_archive)

    compiled = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(source),
            "-o",
            str(executable),
        ],
        text=True,
        capture_output=True,
        timeout=300,
        env=environment,
    )
    assert compiled.returncode == 0, compiled.stderr

    result = subprocess.run(
        [str(executable)],
        text=True,
        capture_output=True,
        timeout=30,
        env=environment,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["caught-fixed", "caught-splat", "done"]
