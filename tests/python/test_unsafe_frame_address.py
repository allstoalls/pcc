"""``pcc.unsafe.frame_address`` reads the caller-linked frame pointer.

The precise stack-map walk follows saved frame pointers: each frame's first
word holds its caller's frame pointer.  These tests execute that chain and
check what each self backend reads for the intrinsic.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from pcc.backend.self_backend_dispatch import emit_self_asm
from pcc.frontends.python import pipeline


# Outside runtime-port modules a raw address is an int; compare addresses as
# ints (ptr_eq on a loaded address and an int parameter disagrees, a separate
# user-mode defect recorded in docs/status.md).
_CHAIN_PROGRAM = '''
from pcc.unsafe import frame_address, load_ptr, ptr_to_int


def descend(depth: int, above: int) -> int:
    here = frame_address()
    linked = 1
    if above != 0:
        linked = 1 if ptr_to_int(load_ptr(here, 0)) == above else 0
    if depth == 0:
        return linked
    return linked & descend(depth - 1, ptr_to_int(here))


def main() -> None:
    print("chain", descend(4, 0), 1 if ptr_to_int(frame_address()) != 0 else 0)


main()
'''


def test_saved_frame_pointers_link_each_frame_to_its_caller(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "chain.py"
    source.write_text(_CHAIN_PROGRAM, encoding="utf-8")
    output = tmp_path / "chain"
    python_program_compiler(
        str(source), str(output), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    ran = subprocess.run([str(output)], capture_output=True, text=True, timeout=60)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert ran.stdout == "chain 1 1\n"


@pytest.mark.parametrize("triple, instruction", [
    ("aarch64-apple-darwin", "  mov x10, x29"),
    ("aarch64-unknown-linux-gnu", "  mov x10, x29"),
    ("x86_64-unknown-linux-gnu", ", rbp"),
])
def test_self_backends_read_the_frame_pointer_register(tmp_path, triple, instruction):
    source = tmp_path / "chain.py"
    source.write_text(_CHAIN_PROGRAM, encoding="utf-8")
    ir = tmp_path / "chain.ll"
    pipeline.compile_python(
        str(source), str(ir), emit_llvm_only=True, libpython_mode="off",
        target_triple=triple,
    )
    text = ir.read_text(encoding="utf-8")
    assert "llvm.frameaddress" in text
    asm = emit_self_asm(text, triple)
    assert "llvm.frameaddress" not in asm
    lines = asm.splitlines()
    assert any(line.startswith(instruction) or line.endswith(instruction) for line in lines)


def test_frame_address_traps_under_cpython():
    from pcc import unsafe

    with pytest.raises(Exception):
        unsafe.frame_address()
    assert "frame_address" in unsafe.__all__
    assert sys.modules["pcc.unsafe"] is unsafe
