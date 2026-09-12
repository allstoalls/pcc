"""A parameter reassigned from a tuple must not make `for` assume a tuple.

`for x in xs` lowers to `py_tuple_len` + `py_tuple_get` whenever `xs`'s static
type is a ListType/TupleType (`for_loop_lowering._emit_for_list_index`), with
no runtime check -- its docstring says it assumes "the runtime value is a
PyObject* tuple/list".

When `xs` is an unannotated parameter that one branch reassigns from an
annotated tuple field, the merged type was taken to be that tuple.  A caller
passing a generator then had its generator read through the tuple layout: the
loop yielded whatever those words happened to be, and a self-hosted link died
with `'list' object has no attribute 'offset'` -- or, in the reduced shape
below, with SIGSEGV.

The parameter can hold anything the caller passes, so the merge has to stay
dynamic.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]

_PROGRAM = textwrap.dedent(
    '''\
    from dataclasses import dataclass


    class Row:
        def __init__(self, offset: int) -> None:
            self.offset = offset


    @dataclass(frozen=True)
    class Section:
        relocations: tuple = ()


    def source_rows(sect):
        for raw in sect.relocations:
            yield Row(offset=raw.offset + 1)


    def validate(sec: Section, *, relocations=None) -> list:
        if relocations is None:
            relocations = sec.relocations
        kinds = []
        for item in relocations:
            kinds.append(type(item).__name__)
        return kinds


    def main() -> int:
        sect = Section((Row(8), Row(16), Row(24)))
        print("fallback:", validate(sect))
        print("generator:", validate(sect, relocations=source_rows(sect)))
        return 0


    main()
    '''
)

_EXPECTED = "fallback: ['Row', 'Row', 'Row']\ngenerator: ['Row', 'Row', 'Row']\n"


@pytest.mark.pcc_gate(probe="self_backend")
def test_generator_argument_survives_a_tuple_typed_reassignment(tmp_path):
    source = tmp_path / "reassigned.py"
    source.write_text(_PROGRAM, encoding="utf-8")
    binary = tmp_path / "reassigned"

    build = subprocess.run(
        [
            sys.executable, "-m", "pcc",
            "--ir-scaffold=on", "--backend", "self",
            "--python-libpython", "off",
            str(source), "-o", str(binary),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=1800,
    )
    assert build.returncode == 0, build.stdout + build.stderr

    run = subprocess.run(
        [str(binary)], capture_output=True, text=True, timeout=120,
    )
    assert run.returncode == 0, (
        f"compiled program exited {run.returncode} "
        f"(-11/139 is the tuple-layout read of a generator)\n{run.stderr}"
    )
    assert run.stdout == _EXPECTED, run.stdout


def test_cpython_reference_behaviour(tmp_path):
    """The same program under CPython, so the contract is not self-defined."""
    source = tmp_path / "reassigned.py"
    source.write_text(_PROGRAM, encoding="utf-8")
    run = subprocess.run(
        [sys.executable, str(source)], capture_output=True, text=True, timeout=120,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == _EXPECTED
