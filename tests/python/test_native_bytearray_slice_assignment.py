"""``bytearray`` slice assignment lowers natively.

There was no lowering at all: ``image[a:b] = data`` raised "Layer 1 slice
assignment on type ByteArrayType not supported", which blocked
``pcc.backend.elf_x86_64`` in the C-frontend self-host closure -- ELF image
construction patches headers and section payloads through exactly that
statement.

``py_bytearray_set_slice`` mutates in place.  A bytearray keeps its bytes
inline at offset 24 behind an exact-fit allocation, so the length can shrink
but not grow; a step-1 replacement longer than the span it replaces is
refused rather than truncated, and that limit is pinned by its own test
below so it cannot be mistaken for working.

CPython is the oracle for every supported shape, including the negative-step
extended slice, where the replacement's first byte belongs at the first
position in slice order (the highest index) rather than the lowest.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


_SOURCE = '''
def main() -> None:
    ba = bytearray(b"abcdefgh")
    ba[2:5] = b"XYZ"
    print(bytes(ba))

    ba2 = bytearray(b"abcdefgh")
    ba2[2:6] = b"XY"
    print(bytes(ba2), len(ba2))

    ba3 = bytearray(b"abcdefgh")
    ba3[:3] = b"123"
    print(bytes(ba3))

    ba4 = bytearray(b"abcdefgh")
    ba4[0:8:2] = b"WXYZ"
    print(bytes(ba4))

    ba5 = bytearray(b"abcdefgh")
    ba5[7:1:-2] = b"123"
    print(bytes(ba5))

    ba6 = bytearray(b"abcdefgh")
    ba6[3:3] = b""
    print(bytes(ba6))

    ba7 = bytearray(b"abcdefgh")
    try:
        ba7[0:8:2] = b"AB"
        print("no error")
    except ValueError:
        print("ValueError")

    ba8 = bytearray(b"abcd")
    ba8[1:3] = [65, 66]
    print(bytes(ba8))


main()
'''

_GROW_SOURCE = '''
def main() -> None:
    ba = bytearray(b"abcd")
    ba[1:2] = b"XYZ"
    print(bytes(ba))


main()
'''


def _build_and_run(tmp_path: Path, name: str, source: str) -> str:
    from pcc.py_frontend.pipeline import compile_python

    src = tmp_path / (name + ".py")
    exe = tmp_path / (name + ".out")
    src.write_text(source, encoding="utf-8")
    compile_python(str(src), str(exe), libpython_mode="off", backend="self")
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=180, check=True
    )
    reference = subprocess.run(
        ["python3", str(src)], text=True, capture_output=True, timeout=180, check=True
    )
    assert run.stdout == reference.stdout, (
        "pcc:\n" + run.stdout + "\ncpython:\n" + reference.stdout
    )
    return run.stdout


def test_bytearray_slice_stores_match_cpython(tmp_path):
    out = _build_and_run(tmp_path, "bytearray_slice_store", _SOURCE)
    assert out.splitlines() == [
        "b'abXYZfgh'",
        # A shorter replacement shifts the tail left and shrinks the length.
        "b'abXYgh' 6",
        "b'123defgh'",
        "b'WbXdYfZh'",
        # Negative step: "1" lands at index 7, "3" at index 3.
        "b'abc3e2g1'",
        "b'abcdefgh'",
        # An extended slice requires an exact size match, as in CPython.
        "ValueError",
        "b'aABd'",
    ]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "A bytearray stores its bytes inline behind an exact-fit "
        "pcc_gc_alloc(24 + n + 1), so a step-1 slice store that needs more "
        "room than it frees cannot grow in place, and reallocating would "
        "leave every other reference pointing at the old object.  "
        "py_bytearray_set_slice raises NotImplementedError instead of "
        "truncating; CPython prints b'aXYZcd'.  Growing needs the bytearray "
        "representation to gain a separate data pointer and capacity."
    ),
)
def test_bytearray_slice_store_can_grow(tmp_path):
    out = _build_and_run(tmp_path, "bytearray_slice_grow", _GROW_SOURCE)
    assert out.splitlines() == ["b'aXYZcd'"]


def test_elf_x86_64_lowers_under_strict_no_libpython():
    """The closure module the missing lowering blocked, compiled the pcc1 way."""
    from pcc.parse.py_lift import parse_and_lift
    from pcc.py_frontend import type_infer
    from pcc.py_frontend.codegen.layer1 import L1CodeGen

    path = Path(__file__).resolve().parents[2] / "pcc/backend/elf_x86_64.py"
    typed = type_infer.infer_module(
        parse_and_lift(
            path.read_text(encoding="utf-8"), str(path), "pcc.backend.elf_x86_64"
        )
    )
    codegen = L1CodeGen(typed, False, "on")
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    codegen._module_source_path = str(path)
    codegen._target_triple = ""
    codegen.generate(typed)
