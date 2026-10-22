"""Unary dunder dispatch regressions (``-obj`` / ``~obj``).

``-instance`` crashed codegen with "Layer 1 cannot coerce ClassType to
int" (found via numpy ``_utils/_pep440.py``'s ``-Infinity``); unary minus
and invert now dispatch ``__neg__`` / ``__invert__`` on hinted class
instances like CPython.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from textwrap import dedent

from pcc.frontends.python.pipeline import compile_python


def _build_and_run(tmp_path: Path, source: str) -> list[str]:
    src = tmp_path / "unary_dunder_probe.py"
    exe = tmp_path / "unary_dunder_probe"
    src.write_text(dedent(source), encoding="utf-8")
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
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def test_unary_neg_and_invert_dispatch_dunders(tmp_path):
    out = _build_and_run(
        tmp_path,
        """
        class NegT:
            def __neg__(self):
                return 42

            def __invert__(self):
                return 7

        class Inf:
            def __neg__(self):
                return Inf()

            def __repr__(self) -> str:
                return "-Infinity"

        def main():
            t = NegT()
            print(-t)
            print(~t)
            inf = Inf()
            print(repr(-inf))

        main()
        """,
    )
    assert out == ["42", "7", "-Infinity"]


def test_abs_dispatches_abs_dunder(tmp_path):
    """``abs(obj)`` dispatches the user ``__abs__`` method (it previously
    raised "Layer 1 abs() with arg type ClassType needs runtime support").
    Reuses the same dunder-dispatch helper as unary ``-obj`` -> ``__neg__``."""
    out = _build_and_run(
        tmp_path,
        """
        class Num:
            def __init__(self, v: int):
                self.v = v

            def __neg__(self):
                return Num(-self.v)

            def __abs__(self):
                return Num(abs(self.v))

            def __repr__(self) -> str:
                return "Num(" + str(self.v) + ")"

        def main():
            n = Num(-5)
            print(-n)
            print(abs(n))
            print(abs(Num(3)))

        main()
        """,
    )
    assert out == ["Num(5)", "Num(5)", "Num(3)"], out


def test_abs_dispatches_abs_dunder_on_dyntype(tmp_path):
    """``abs(x)`` where ``x`` is a DynType (untyped) value holding a user
    instance dispatches __abs__ at runtime (py_obj_abs -> py_user_abs_dispatch).
    The static-ClassType path was fixed earlier; this completes the dyn path."""
    out = _build_and_run(
        tmp_path,
        """
        class Num:
            def __init__(self, v: int):
                self.v = v
            def __abs__(self):
                return Num(abs(self.v))
            def __repr__(self) -> str:
                return "Num(" + str(self.v) + ")"

        def f(x):
            return abs(x)

        def main():
            print(f(Num(-5)))   # Num(5)  (dyn receiver -> __abs__)
            print(f(-7))        # 7
            print(f(-2.5))      # 2.5

        main()
        """,
    )
    assert out == ["Num(5)", "7", "2.5"], out
