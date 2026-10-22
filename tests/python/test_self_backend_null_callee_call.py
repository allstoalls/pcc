"""A call through a constant ``null`` callee lowers to the runtime trap.

Constant propagation on a dead path can leave ``call ... null(...)`` in the
IR the self backend is asked to compile (a platform-conditional libSystem
lookup, say).  Executing that call is undefined behaviour, so the parser
binds the callee to ``pcc_null_callee_trap`` instead of refusing the module;
the trap aborts with ``[NULL_CALL]`` if the path is ever reached.  The bar is
behavioural: the module must parse, the object must reference the trap, and
a linked program that takes the path must abort.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest

import pcc.tools.ir_to_obj as ir_to_obj
from pcc.backend.macho_exec import link_executable
from pcc.backend.self_backend_kernel import get_indexed_function_kernel
from pcc.backend.self_backend_parse import parse_self_backend_module

_IS_ARM64_DARWIN = os.uname().sysname == "Darwin" and os.uname().machine == "arm64"

_LIBRARY_IR = (
    'target triple = "arm64-apple-darwin"\n'
    "define i64 @f(i64 %x) {\n"
    "entry:\n"
    "  %r = call i64 null(i64 %x)\n"
    "  ret i64 %r\n"
    "}\n"
)

_MAIN_IR = (
    'target triple = "arm64-apple-darwin"\n'
    "\n"
    "define i32 @main() {\n"
    "entry:\n"
    "  %r = call i64 null(i64 7)\n"
    "  %t = trunc i64 %r to i32\n"
    "  ret i32 %t\n"
    "}\n"
)


def test_null_callee_call_parses_to_the_runtime_trap():
    module = parse_self_backend_module(_LIBRARY_IR)
    kernel = get_indexed_function_kernel(module.functions[0])

    call = kernel.diagnostic_call_data(0)
    assert call[2] == "pcc_null_callee_trap"
    assert [value for _type, value in call[4]] == ["x"]


def test_null_callee_object_references_the_trap_symbol():
    image = ir_to_obj.emit_object(_LIBRARY_IR, target_triple="arm64-apple-darwin")
    assert b"_pcc_null_callee_trap" in image
    assert b"_f" in image


@pytest.mark.integration
@pytest.mark.pcc_gate(
    unavailable=None if _IS_ARM64_DARWIN else "needs Darwin arm64"
)
def test_null_callee_trap_aborts_a_program_that_reaches_it(
    tmp_path, pcc_runtime_archive
):
    image = link_executable(
        [ir_to_obj.emit_object(_MAIN_IR, target_triple="arm64-apple-darwin")],
        archives=[Path(pcc_runtime_archive).read_bytes()],
        entry="_main",
    )
    executable = tmp_path / "null_call"
    executable.write_bytes(image)
    executable.chmod(0o755)

    ran = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=120
    )
    assert ran.returncode != 0
    assert "[NULL_CALL]" in ran.stderr
