"""int.to_bytes / int.from_bytes under strict no-libpython (2026-06-12).

Unsigned forms lower natively (`py_int_to_bytes` / `py_int_from_bytes`,
C-only helper walking the canonical base-2^32 bignum limbs); the
signed= keyword deliberately falls through so strict mode rejects it
honestly. Errors match CPython: OverflowError for too-small length and
for negative values, ValueError for a bad byteorder. Expected output
is the CPython oracle for the same program.
"""
from __future__ import annotations

import subprocess
import textwrap

_PROGRAM = textwrap.dedent(
    """
    def main() -> int:
        print((255).to_bytes(2, "big"))
        print((255).to_bytes(2, "little"))
        print((1048576).to_bytes(4, "big"))
        big = 2 ** 100
        print(big.to_bytes(16, "big"))
        print((0).to_bytes(0, "big"))
        print(int.from_bytes(b"\\x01\\x00", "big"))
        print(int.from_bytes(b"\\x01\\x00", "little"))
        mutable = bytearray(b"\\x01\\x00")
        print(int.from_bytes(mutable, "big"))
        print(int.from_bytes(memoryview(mutable), "little"))
        rt = int.from_bytes((2 ** 100).to_bytes(16, "big"), "big")
        print(rt == 2 ** 100)
        try:
            (256).to_bytes(1, "big")
            print("no-raise")
        except OverflowError:
            print("overflow-ok")
        try:
            (-1).to_bytes(2, "big")
            print("no-raise2")
        except OverflowError:
            print("neg-ok")
        try:
            (1).to_bytes(2, "middle")
            print("no-raise3")
        except ValueError:
            print("order-ok")
        return 0


    main()
    """
)

_EXPECTED = [
    "b'\\x00\\xff'",
    "b'\\xff\\x00'",
    "b'\\x00\\x10\\x00\\x00'",
    "b'\\x00\\x00\\x00\\x10\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00'",
    "b''",
    "256",
    "1",
    "256",
    "1",
    "True",
    "overflow-ok",
    "neg-ok",
    "order-ok",
]


def test_int_bytes_forms_match_cpython(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "int_bytes_prog.py"
    src.write_text(_PROGRAM, encoding="utf-8")
    exe = tmp_path / "int_bytes_prog"
    compile_python(
        str(src),
        str(exe),
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
    )
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == _EXPECTED
