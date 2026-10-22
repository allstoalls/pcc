"""Native 2-arg ``iter(callable, sentinel)`` lowering (no libpython).

CPython's ``iter(callable, sentinel)`` returns a callable-iterator: each
``next()`` calls ``callable()`` and yields the result until it compares equal
to ``sentinel``, at which point ``StopIteration`` is raised (the sentinel value
itself is not yielded). pcc lowers this natively onto the existing
``PY_TYPE_ITER`` object (the single ``seq`` slot holds a ``(callable, sentinel)``
tuple; the ``index`` field is a negative state discriminator), so the whole
construct stays libpython-free.

Compiling in default mode links the pcc-Python runtime ports and hard-errors on
any libpython fallback, so a successful compile+run here proves the native
callable-iterator path is exercised end to end. Output is diffed against the
CPython reference for the exact same source.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

from pcc.frontends.python.pipeline import compile_python


_PROGRAM = textwrap.dedent(
    """
    class Counter:
        def __init__(self) -> None:
            self.n = 0

        def __call__(self) -> int:
            self.n = self.n + 1
            return self.n


    def main() -> None:
        # for-loop over a callable-iterator: yields 1..4, stops at sentinel 5.
        c = Counter()
        out = []
        for x in iter(c, 5):
            out.append(x)
        print(out)

        # next() on a callable-iterator: yields until sentinel, then StopIteration.
        c2 = Counter()
        it2 = iter(c2, 3)
        print(next(it2))
        print(next(it2))
        try:
            next(it2)
        except StopIteration:
            print("stop")

        # next() with a default swallows the StopIteration after exhaustion.
        c3 = Counter()
        it3 = iter(c3, 2)
        print(next(it3))
        print(next(it3, "done"))
        print(next(it3, "done"))


    if __name__ == "__main__":
        main()
    """
).lstrip()


_EXPECTED = "[1, 2, 3, 4]\n1\n2\nstop\n1\ndone\ndone\n"


def test_iter_callable_sentinel_matches_cpython(tmp_path):
    # Reference: CPython must produce exactly the expected output.
    ref = subprocess.run(
        [sys.executable, "-c", _PROGRAM],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert ref.returncode == 0, ref.stderr
    assert ref.stdout == _EXPECTED, ref.stdout

    # pcc: compile in default (no-libpython) mode and run the native binary.
    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(_PROGRAM, encoding="utf-8")
    compile_python(str(src), str(exe), ir_scaffold_mode="on")
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == _EXPECTED, run.stdout
