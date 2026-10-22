"""A builtin type called with * or ** arguments constructs its value.

``str(*args)`` compiled the ``*args`` marker as an ordinary call, so the
program raised "NameError: name '*' is not defined": the builtin types'
direct lowerings take fixed operands. A call of an unshadowed builtin type
with an unpacked argument list now calls the type object, which parses it,
as CPython's type call does.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


PROGRAM = r'''
def build(kind, *args, **kwargs):
    return kind(*args, **kwargs)


def main():
    text_args = (b"caf\xc3\xa9", "utf-8")
    print(str(*text_args), int(*["7f", 16]), float(*["2.5"]), bool(*[[]]))
    print(list(*[(1, 2)]), tuple(*["ab"]), sorted(set(*[[3, 1, 3]])))
    print(sorted(dict(**{"a": 1, "b": 2}).items()), dict(*[[("k", "v")]], extra=1))
    print(bytes(*[[104, 105]]), bytearray(*[b"xy"]), complex(*[1, 2]))
    print(build(str, 42), build(int, "11", base=2))
    try:
        str(*[b"\xff", "ascii"])
    except UnicodeDecodeError as caught:
        print(type(caught).__name__, caught.reason)


main()
'''


@pytest.mark.integration
def test_unpacked_builtin_type_calls_match_cpython_on_five_collectors(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "unpacked_types.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "unpacked_types"
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        environment = dict(os.environ, PCC_GC_BACKEND=str(backend))
        environment.pop("LC_ALL", None)
        ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60, env=environment)
        assert ran.returncode == 0, f"GC{backend}: {ran.stdout}{ran.stderr}"
        assert ran.stdout == expected.stdout, f"GC{backend}: {ran.stdout}"
