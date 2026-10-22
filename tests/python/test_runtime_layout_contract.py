"""Layout contract between the C runtime structs and the pcc-Python ports.

The pcc-Python runtime ports under ``pcc/runtime/py/`` read C objects
through raw byte offsets (``ptr_add``/``load_i64`` — 140+ sites, e.g. the
layout table in ``py_obj_ops_compare.py``'s docstring). Nothing ties those
hand-written offsets to the C structs in ``py_internal.h``/``py_runtime.h``,
so a C-side field insertion silently corrupts every port reader.

This test compiles a C probe with the real headers and asserts
``offsetof``/``sizeof`` for every struct the ports mirror. If it fails, DO
NOT just update the numbers here: every ``pcc/runtime/py/`` reader of the
changed struct is now wrong too — fix them together.
"""
from __future__ import annotations

import shutil
import subprocess
import textwrap

import pytest

from pcc1_gate import repo_root
from pcc.runtime.py.py_abi_constants import (
    PYCLASSMETHOD_FUNC_OFFSET,
    PYCLASSMETHOD_NAME_HASH_OFFSET,
    PYCLASSMETHOD_NAME_LENGTH_OFFSET,
    PYCLASSMETHOD_NAME_OFFSET,
    PYCLASSMETHOD_SIZE,
)

REPO = repo_root()
RUNTIME_DIR = REPO / "pcc" / "runtime"

_CC = shutil.which("cc")
_CC_MISSING = None if _CC else "system cc is required to probe C struct layouts"

# The port-side contract: (struct, field) -> byte offset, and struct -> size.
# Sources: pcc/runtime/py/py_obj_ops_compare.py docstring layout table,
# AGENTS.md "PyClassObject layout", and the header comments in py_internal.h.
EXPECTED_OFFSETS = {
    ("PyObjectHeader", "refcount"): 0,
    ("PyObjectHeader", "type_tag"): 8,
    ("PyObjectHeader", "flags"): 12,
    ("PyIntObject", "sign"): 16,
    ("PyIntObject", "ndigits"): 20,
    ("PyIntObject", "digits"): 24,
    ("PyFloatObject", "value"): 16,
    ("PyComplexObject", "real"): 16,
    ("PyComplexObject", "imag"): 24,
    ("PyBytesObject", "byte_len"): 16,
    ("PyBytesObject", "data"): 24,
    ("PyByteArrayObject", "byte_len"): 16,
    ("PyByteArrayObject", "data"): 24,
    ("PyMemoryViewObject", "base"): 16,
    ("PyStrObject", "byte_len"): 16,
    ("PyStrObject", "cp_len"): 24,
    ("PyStrObject", "hash"): 32,
    ("PyStrObject", "data"): 40,
    ("PyListObject", "length"): 16,
    ("PyListObject", "capacity"): 24,
    ("PyListObject", "items"): 32,
    ("PyTupleObject", "len"): 16,
    ("PyTupleObject", "items"): 24,
    ("PyDictObject", "size"): 16,
    ("PyDictObject", "capacity"): 24,
    ("PyDictObject", "indices"): 32,
    ("PyDictObject", "entries"): 40,
    ("PyDictObject", "entries_used"): 48,
    ("DictEntry", "hash"): 0,
    ("DictEntry", "key"): 8,
    ("DictEntry", "value"): 16,
    ("PyClassObject", "name"): 16,
    ("PyClassObject", "n_bases"): 24,
    ("PyClassObject", "bases"): 32,
    ("PyClassObject", "n_mro"): 40,
    ("PyClassObject", "mro"): 48,
    ("PyClassObject", "n_methods"): 56,
    ("PyClassObject", "methods"): 64,
    ("PyClassObject", "n_fields"): 72,
    ("PyClassObject", "field_names"): 80,
    ("PyClassObject", "instance_size"): 88,
    ("PyClassObject", "type_tag_alloc"): 92,
    ("PyClassObject", "del_method"): 96,
    ("PyClassObject", "attrs"): 104,
    ("PyClassObject", "metaclass"): 112,
    # py_class.py uses these shared constants for method-table traversal and
    # insertion, including the hash/length metadata added to each record.
    ("PyClassMethod", "name"): PYCLASSMETHOD_NAME_OFFSET,
    ("PyClassMethod", "func"): PYCLASSMETHOD_FUNC_OFFSET,
    ("PyClassMethod", "name_hash"): PYCLASSMETHOD_NAME_HASH_OFFSET,
    ("PyClassMethod", "name_length"): PYCLASSMETHOD_NAME_LENGTH_OFFSET,
    ("PyInstanceObject", "cls"): 16,
    ("PyInstanceObject", "fields"): 24,
    ("PyPropertyObject", "fget"): 16,
    ("PyPropertyObject", "fset"): 24,
    ("PyPropertyObject", "fdel"): 32,
    ("PyClassMethodObject", "func"): 16,
    ("PyStaticMethodObject", "func"): 16,
}
EXPECTED_SIZES = {
    "PyObjectHeader": 16,
    "DictEntry": 24,
    "PyClassObject": 120,
    "PyClassMethod": PYCLASSMETHOD_SIZE,
    "PyInstanceObject": 24,
    "PyPropertyObject": 40,
    "PyClassMethodObject": 24,
    "PyStaticMethodObject": 24,
}


@pytest.mark.pcc_gate(unavailable=_CC_MISSING)
def test_c_struct_layouts_match_pcc_python_port_contract(tmp_path):
    lines = [
        f'    printf("{struct}.{field} %zu\\n", offsetof({struct}, {field}));'
        for (struct, field) in EXPECTED_OFFSETS
    ]
    lines += [
        f'    printf("{struct}.__size__ %zu\\n", sizeof({struct}));'
        for struct in EXPECTED_SIZES
    ]
    src = tmp_path / "layout_probe.c"
    exe = tmp_path / "layout_probe.out"
    body = "\n".join(lines)
    src.write_text(
        textwrap.dedent(
            """
            #include "py_internal.h"
            #include <stddef.h>
            #include <stdio.h>

            int main(void) {
            %s
                return 0;
            }
            """
        ).lstrip()
        % body,
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            _CC,
            "-std=c11",
            f"-I{RUNTIME_DIR / 'include'}",
            f"-I{RUNTIME_DIR / 'src'}",
            str(src),
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, run.stdout + run.stderr

    actual = {}
    for line in run.stdout.splitlines():
        name, value = line.rsplit(" ", 1)
        actual[name] = int(value)

    mismatches = []
    for (struct, field), expected in EXPECTED_OFFSETS.items():
        got = actual.get(f"{struct}.{field}")
        if got != expected:
            mismatches.append(f"offsetof({struct}, {field}) = {got}, ports assume {expected}")
    for struct, expected in EXPECTED_SIZES.items():
        got = actual.get(f"{struct}.__size__")
        if got != expected:
            mismatches.append(f"sizeof({struct}) = {got}, ports assume {expected}")
    assert not mismatches, (
        "C runtime struct layout drifted from the pcc-Python port contract.\n"
        "Every pcc/runtime/py/ reader of these structs uses hand-written\n"
        "byte offsets and is now silently wrong — fix the ports (and this\n"
        "table) together, never this table alone:\n  "
        + "\n  ".join(mismatches)
    )
