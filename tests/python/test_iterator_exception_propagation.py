"""Iterator-protocol exception propagation (err-check audit pin).

py_obj_next returns NULL both at exhaustion and on error; every
consumer must route NULL through the StopIteration-match check
(clear and finish) versus propagate (real exception reaches the
enclosing try/except). The 2026-06-11 err-check audit flagged these
sites as suspects; review showed the maybe_end/propagate routing is
present everywhere — this test pins that behavior end to end.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from textwrap import dedent

from pcc.frontends.python.pipeline import compile_python

_SOURCE = """
class Bad:
    def __init__(self):
        self.i = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self.i >= 2:
            raise ValueError("boom")
        self.i = self.i + 1
        return self.i


def main() -> int:
    b = Bad()
    try:
        xs = list(b)
        print("no-raise", len(xs))
    except ValueError:
        print("valueerror")
    c = Bad()
    try:
        total = sum(c)
        print("no-raise-sum", total)
    except ValueError:
        print("valueerror-sum")
    d = Bad()
    try:
        ys = [x for x in d]
        print("no-raise-comp", len(ys))
    except ValueError:
        print("valueerror-comp")
    return 0


main()
"""


def test_next_exception_propagates_through_list_sum_comprehension(tmp_path):
    src = tmp_path / "next_err_probe.py"
    exe = tmp_path / "next_err_probe"
    src.write_text(dedent(_SOURCE), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
    )
    proc = subprocess.run(
        [str(exe)], text=True, capture_output=True, check=True, timeout=30
    )
    assert [l for l in proc.stdout.splitlines() if l] == [
        "valueerror",
        "valueerror-sum",
        "valueerror-comp",
    ]
