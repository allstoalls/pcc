"""Integer literals above the tagged lane evaluate exactly in compiled code.

This file used to ban such literals from the self-host closure: an early
pcc evaluated ``9223372036854775808`` or ``0xFFFFFFFFFFFFFFFF`` as 0 (pcc2
could not print any integer because a literal mask became ``& 0``, tracked as
M5-SELFHOST-BIG-INT-LITERAL). The ban had stopped holding (compiler sources
use FNV/xxhash constants) while its own note recorded that the defect no
longer reproduced. Literals outside the lane are now module-static immortal
objects built at compile time, so the check is behavioral: every literal
position the compiler sources use is compiled and must print what CPython
prints.
"""
from __future__ import annotations

import subprocess
import sys

from pcc.frontends.python.pipeline import compile_python


_PROGRAM = '''
FNV_OFFSET = 14695981039346656037
TABLE = (13787848793156543929, 10723151780598845931, 0xFFFFFFFFFFFFFFFF)
LIMITS = {"i64_max": 9223372036854775807, "i64_min": -9223372036854775808}


def lane_edges():
    return [4611686018427387903, 4611686018427387904, -4611686018427387904, -4611686018427387905]


def wide(seed=340282366920938463463374607431768211455):
    return seed >> 64, seed & 0xFFFFFFFFFFFFFFFF


def fnv(data):
    value = FNV_OFFSET
    for byte in data:
        value = ((value ^ byte) * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return value


def main():
    print(FNV_OFFSET, TABLE, sorted(LIMITS.items()))
    print(lane_edges(), wide(), wide(2**70 + 5))
    print(fnv(b"pcc"), fnv(b""), 9223372036854775808 - 1, -0x8000000000000000)
    print(0xFFFFFFFFFFFFFFFF + 1 == 2**64, 18446744073709551615 // 3, hex(0xDEADBEEFCAFEBABE12))


main()
'''


def test_over_lane_int_literals_match_cpython(tmp_path):
    source = tmp_path / "over_lane_literals.py"
    source.write_text(_PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True,
                              timeout=30, check=True).stdout
    exe = tmp_path / "over_lane_literals"
    compile_python(str(source), str(exe), libpython_mode="off", ir_scaffold_mode="on", backend="self")
    actual = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30, check=True).stdout
    assert actual == expected
