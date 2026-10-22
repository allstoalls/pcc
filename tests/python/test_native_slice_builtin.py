"""``slice(...)`` is an owned value wherever a call operand needs one.

The builtin ``slice()`` returned py_slice_new's NEW reference without an
owner record and built it from unrooted bounds, so as an argument of a call
(``self.assertRaises(ValueError, u.__getitem__, slice(0, 10, 0))``, as
seq_tests, list_tests and string_tests do) the compile failed with
"slot-call operand has no authoritative source or owned result: Call". It
now builds the slice from slot-call operand bounds into the caller's output
root, as a subscript's slice key is built. A slice object as a list, tuple,
str or bytes subscript (``items[slice(1, 4)]``, assignment and deletion
included) slices the sequence, as CPython's sequence ``__getitem__`` does;
the runtime read it as an integer index.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


PROGRAM = r'''
class Seq:
    def __init__(self, items):
        self.items = list(items)

    def __getitem__(self, key):
        if isinstance(key, slice):
            return ("slice", key.start, key.stop, key.step, self.items[key])
        return ("item", self.items[key])

    def __setitem__(self, key, value):
        if isinstance(key, slice) and key.step == 0:
            raise ValueError("slice step cannot be zero")
        self.items[key] = value


def raises(kind, function, *args):
    try:
        function(*args)
    except kind as caught:
        return type(caught).__name__ + ": " + str(caught)
    return "no error"


def bounds(value):
    return (value.start, value.stop, value.step)


def main():
    seq = Seq(range(10))
    print(seq.__getitem__(slice(1, 4)), seq[slice(None, 3)])
    print(raises(ValueError, seq.__setitem__, slice(0, 10, 0), [1, 2, 3]))
    print(raises(ValueError, seq.__getitem__, slice(0, 10, 0)))
    print(bounds(slice(4)), bounds(slice(1, 2)), bounds(slice(-1, None, -2)))
    numbers = list(range(10))
    numbers[slice(2, 5)] = ["x"]
    print(numbers, "abcdef"[slice(1, None, 2)], isinstance(slice(1), slice))
    window = slice(len(numbers) - 3, None)
    print(numbers[window], [bounds(slice(i, i + 2)) for i in range(3)])
    del numbers[slice(0, 2)]
    letters = ("a", "b", "c", "d")
    print(numbers, letters[slice(None, None, -1)], b"bytes"[slice(1, 3)], "text"[window])


main()
'''


@pytest.mark.integration
def test_slice_builtin_as_call_operand_on_five_collectors(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "slice_builtin.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "slice_builtin"
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
