"""Integer packing at every width boundary matches CPython, natively too.

64-bit fields used to range-check against ``0x10000000000000000``: a bigint
literal reparsed on every pack plus bigint division and comparison, which made
``Struct.pack`` about half of a native ELF encoder's time. The checks are
shifts now and 8-byte values are written as two 32-bit halves.
"""
from __future__ import annotations

import importlib
import os
import struct as cpython_struct
import subprocess
import sys

import pytest

from pcc.frontends.python.pipeline import compile_python


_FORMATS = ("b", "B", "h", "H", "i", "I", "l", "L", "q", "Q", "?")
_ORDERS = ("<", ">", "!", "=", "@")
_VALUES = (
    0, 1, -1, 127, 128, -128, -129, 255, 256, 32767, 32768, -32768, -32769, 65535, 65536,
    2**31 - 1, 2**31, -2**31, -2**31 - 1, 2**32 - 1, 2**32,
    2**62, 2**63 - 1, 2**63, -2**63, -2**63 - 1, 2**64 - 1, 2**64, -2**64, 3**50, -3**50,
    True, False,
)


def _outcome(module, fmt, value):
    try:
        return ("ok", module.pack(fmt, value))
    except module.error:
        return ("error",)


def test_integer_pack_boundaries_match_cpython():
    pcc_struct = importlib.import_module("pcc.stdlib.struct")
    for order in _ORDERS:
        for code in _FORMATS:
            fmt = order + code
            for value in _VALUES:
                assert _outcome(pcc_struct, fmt, value) == _outcome(cpython_struct, fmt, value), (fmt, value)
    layout = pcc_struct.Struct("<QqIhB?3x2s")
    expected = cpython_struct.Struct("<QqIhB?3x2s")
    values = (2**64 - 1, -2**63, 2**32 - 1, -1, 255, True, b"z")
    assert layout.pack(*values) == expected.pack(*values)
    buffer = bytearray(40)
    reference = bytearray(40)
    layout.pack_into(buffer, 3, *values)
    expected.pack_into(reference, 3, *values)
    assert buffer == reference


_PROGRAM = '''
import struct

VALUES = [0, 1, -1, 127, 128, -128, 255, 256, 32767, -32768, 65535,
          2**31 - 1, -2**31, 2**32 - 1, 2**63 - 1, 2**63, -2**63, 2**64 - 1, 2**64, -2**64 - 1]


def main():
    for order in "<>":
        for code in "bBhHiIqQ":
            fmt = order + code
            for value in VALUES:
                try:
                    print(fmt, value, struct.pack(fmt, value).hex())
                except struct.error:
                    print(fmt, value, "error")
    layout = struct.Struct("<QqIhB?3x2s")
    print(layout.pack(2**64 - 1, -2**63, 2**32 - 1, -1, 255, True, b"z").hex())
    buffer = bytearray(40)
    layout.pack_into(buffer, 3, 2**63, -5, 7, -2, 1, False, b"ab")
    print(bytes(buffer).hex())


main()
'''


@pytest.mark.integration
def test_native_integer_pack_boundaries_match_cpython_under_every_collector(tmp_path, pcc_runtime_archive):
    source = tmp_path / "struct_edges.py"
    source.write_text(_PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True,
                              timeout=30, check=True).stdout
    binary = tmp_path / "struct_edges"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f"GC{gc}: {result.stderr}"
        assert result.stdout == expected, f"GC{gc}"
