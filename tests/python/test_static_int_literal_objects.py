"""Int literals outside the tagged lane are one static immortal object each.

A literal such as ``0x10000000000000000`` used to call
``py_int_from_cstr("18446744073709551616", 10)`` every time it was evaluated,
and an i64 outside the tagged lane allocated through ``py_int_from_i64``.
``struct.pack`` evaluated 2**64 per field, which made bigint parsing a large
share of native ELF encoding. Like str literals, the object now lives in the
module's data with the immortal flag, shared as CPython shares constants.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from textwrap import dedent

import pytest

from pcc.frontends.python.pipeline import compile_python


_PROGRAM = dedent('''
    def big():
        return 123456789012345678901234567890


    def lane_edge():
        return 4611686018427387904


    def above_i64():
        return 0x10000000000000000


    def negative():
        return -98765432109876543210


    def main():
        total = 0
        for i in range(2000):
            total += big() % (i + 7) + (above_i64() >> 64)
        print(total)
        print(big() * 2, big() - big(), lane_edge() + 1, lane_edge() - 1)
        print(negative(), -negative(), above_i64() - 1, 0xFFFFFFFFFFFFFFFF & 0xFFFF)
        print(big() is big(), above_i64() == 2 ** 64, hash(big()) == hash(123456789012345678901234567890))
        print(9223372036854775807, -9223372036854775808, 9223372036854775808)


    main()
''')


def test_big_literals_are_static_objects_in_ir(tmp_path):
    source = tmp_path / "int_literals.py"
    source.write_text(_PROGRAM, encoding="utf-8")
    ll = tmp_path / "int_literals.ll"
    compile_python(str(source), str(ll), libpython_mode="off", ir_scaffold_mode="on", backend="self",
                   emit_llvm_only=True)
    text = ll.read_text()
    for name in ("big", "lane_edge", "above_i64", "negative"):
        body = re.search(r"^define [^\n]*@user_int_literals_" + name + r"\(.*?^}", text, re.M | re.S).group(0)
        assert "@py_int_from_cstr" not in body and "@py_int_from_i64" not in body, name
    assert re.search(r"^@\.pyint\.obj\.\d+ = internal global", text, re.M)


@pytest.mark.integration
def test_static_int_literals_match_cpython_under_every_collector(tmp_path, pcc_runtime_archive):
    source = tmp_path / "int_literals.py"
    source.write_text(_PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True,
                              timeout=30, check=True).stdout
    binary = tmp_path / "int_literals"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, f"GC{gc}: {result.stderr}"
        assert result.stdout == expected, f"GC{gc}: {result.stdout}"


_MODULE_PROGRAM = dedent('''
    MASK = 0xFFFFFFFFFFFFFFFF
    LIMITS = (9223372036854775808, -9223372036854775809)
    MASK = MASK + 0
    MASK = 0xFFFFFFFFFFFFFFFF


    def fold(values):
        acc = 14695981039346656037
        for value in values:
            acc = ((acc ^ value) * 1099511628211) & MASK
        return acc


    print(fold(range(50)), LIMITS, MASK)
''')


def test_static_int_literals_are_registered_managed_objects(tmp_path):
    """Refcount operations on them must pass the provenance audit.

    Static str and float literal objects are registered with the collector's
    managed-pointer index when the module starts; static ints were not, so a
    pcc1 that rebinds a module-level over-lane constant (the Stage1 smoke,
    ``literal_lowering``'s module top) failed the bootstrap audit with
    "refcount operation on an unmanaged pointer".
    """
    source = tmp_path / "module_ints.py"
    source.write_text(_MODULE_PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True,
                              timeout=30, check=True).stdout
    binary = tmp_path / "module_ints"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on")
    for gc in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc),
                                         PCC_GC_REFCOUNT_PROVENANCE_PROBE="2"))
        assert "unmanaged pointer" not in result.stderr, f"GC{gc}: {result.stderr}"
        assert result.returncode == 0, f"GC{gc}: {result.stderr}"
        assert result.stdout == expected, f"GC{gc}: {result.stdout}"
