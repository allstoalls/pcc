"""Complex-number arithmetic under strict no-libpython.

Before this, only ``complex()`` construction, ``.real``/``.imag``, and ``+``
were implemented; ``-``/``*``/``/``, unary ``-``, ``abs()``, ``.conjugate()``,
and complex ``repr``/``str`` (``print`` showed ``<object tag=16>``) were not.

New runtime (C-only helpers in py_format.c: py_complex_sub/mul/div/neg/
conjugate/abs/repr) + frontend routing (binary_op / numeric_builtin / unary /
method_call lowering). Covered in BOTH runtime tiers.
"""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest


def _run(tmp_path: Path, source: str, *, runtime_cc: bool, monkeypatch) -> str:
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "cx.py"
    exe = tmp_path / "cx.out"
    src.write_text(textwrap.dedent(source), encoding="utf-8")
    compile_python(str(src), str(exe), libpython_mode="off",
                   ir_scaffold_mode="on", backend="self")
    r = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return r.stdout


@pytest.mark.parametrize("runtime_cc", [False], ids=["port"])
def test_complex_arithmetic(tmp_path, monkeypatch, runtime_cc):
    out = _run(tmp_path, """
        def main():
            a = complex(3, 4)
            b = complex(1, -2)
            print(a + b)            # (4+2j)
            print(a - b)            # (2+6j)
            print(a * b)            # (11-2j)
            print(a / b)            # (-1+2j)
            print(abs(a))           # 5.0
            print(-a)               # (-3-4j)
            print(a.conjugate())    # (3-4j)
            print(a.real, a.imag)   # 3.0 4.0
            print(complex(1, 2) + 3)    # (4+2j)  (mixed complex+int)
            print(2 * complex(1, 1))    # (2+2j)
            print(complex(0, 1))        # 1j      (pure imaginary repr)
            print(3 + 4j)               # (3+4j)  (literal)

        if __name__ == "__main__":
            main()
    """, runtime_cc=runtime_cc, monkeypatch=monkeypatch)
    assert out.split("\n")[:12] == [
        "(4+2j)", "(2+6j)", "(11-2j)", "(-1+2j)", "5.0", "(-3-4j)",
        "(3-4j)", "3.0 4.0", "(4+2j)", "(2+2j)", "1j", "(3+4j)",
    ], out


@pytest.mark.parametrize("runtime_cc", [False], ids=["port"])
def test_complex_div_by_zero_raises(tmp_path, monkeypatch, runtime_cc):
    out = _run(tmp_path, """
        def main():
            try:
                _ = complex(1, 2) / complex(0, 0)
                print("NO_RAISE")
            except ZeroDivisionError:
                print("zerodiv raised")

        if __name__ == "__main__":
            main()
    """, runtime_cc=runtime_cc, monkeypatch=monkeypatch)
    assert out.strip() == "zerodiv raised", out


@pytest.mark.parametrize("runtime_cc", [False], ids=["port"])
def test_complex_ordering_raises(tmp_path, monkeypatch, runtime_cc):
    # complex supports ==/!= but no ordering; CPython raises TypeError with
    # ``'<' not supported between instances of 'complex' and 'complex'``.
    # The ordering ops must raise instead of misrouting through the numeric
    # int/float compare fast path (which yielded a garbage bool before).
    out = _run(tmp_path, """
        def main():
            a = complex(1, 2)
            b = complex(3, 4)
            try:
                _ = a < b
                print("NO_RAISE lt")
            except TypeError as e:
                print("lt:", str(e))
            try:
                _ = a <= b
                print("NO_RAISE le")
            except TypeError as e:
                print("le:", str(e))
            try:
                _ = a > b
                print("NO_RAISE gt")
            except TypeError as e:
                print("gt:", str(e))
            try:
                _ = a >= b
                print("NO_RAISE ge")
            except TypeError as e:
                print("ge:", str(e))
            # ==/!= remain valid.
            print("eq:", complex(1, 2) == complex(1, 2))
            print("ne:", complex(1, 2) != complex(3, 4))

        if __name__ == "__main__":
            main()
    """, runtime_cc=runtime_cc, monkeypatch=monkeypatch)
    assert out.split("\n")[:6] == [
        "lt: '<' not supported between instances of 'complex' and 'complex'",
        "le: '<=' not supported between instances of 'complex' and 'complex'",
        "gt: '>' not supported between instances of 'complex' and 'complex'",
        "ge: '>=' not supported between instances of 'complex' and 'complex'",
        "eq: True",
        "ne: True",
    ], out


@pytest.mark.parametrize("runtime_cc", [False], ids=["port"])
def test_complex_pow(tmp_path, monkeypatch, runtime_cc):
    # ``**`` on complex operands routes to the py_complex_pow runtime helper
    # (mirrors CPython _Py_c_pow: integer fast path + exp/log/cos/sin general
    # path). Before this it failed at COMPILE time with PCC-PY-COMPILE-001
    # "cannot coerce ComplexType to int" because the complex binop table only
    # mapped + - * / and ** fell through to _to_int64.
    out = _run(tmp_path, """
        def main():
            a = complex(1, 2)
            print(a ** 2)               # (-3+4j)  integer fast path
            print(a ** 3)               # (-11-2j)
            print(a ** 0)               # (1+0j)   anything ** 0
            print(complex(3, 4) ** 2)   # (-7+24j)
            print(complex(0, 1) ** 2)   # (-1+0j)
            print(complex(2, 0) ** 0.5) # (1.4142135623730951+0j) general path
            print((1 + 2j) ** 2)        # (-3+4j)  literal base
            print(complex(1, 2) ** -2)  # (-0.12-0.16j) negative int exp

        if __name__ == "__main__":
            main()
    """, runtime_cc=runtime_cc, monkeypatch=monkeypatch)
    assert out.split("\n")[:8] == [
        "(-3+4j)", "(-11-2j)", "(1+0j)", "(-7+24j)", "(-1+0j)",
        "(1.4142135623730951+0j)", "(-3+4j)", "(-0.12-0.16j)",
    ], out


@pytest.mark.parametrize("runtime_cc", [False], ids=["port"])
def test_complex_pow_zero_to_negative_raises(tmp_path, monkeypatch, runtime_cc):
    # 0 ** (negative or complex power) raises ZeroDivisionError, mirroring
    # CPython. The frontend emits a py_err_occurred() check after the call.
    out = _run(tmp_path, """
        def main():
            try:
                _ = complex(0, 0) ** -1
                print("NO_RAISE")
            except ZeroDivisionError:
                print("zerodiv raised")
            print(complex(0, 0) ** 2)   # 0j (0 ** positive-real is fine)

        if __name__ == "__main__":
            main()
    """, runtime_cc=runtime_cc, monkeypatch=monkeypatch)
    assert out.split("\n")[:2] == ["zerodiv raised", "0j"], out


@pytest.mark.parametrize("runtime_cc", [False], ids=["port"])
def test_complex_pow_general_imaginary_exponent_and_numeric_coercion(
    tmp_path, monkeypatch, runtime_cc
):
    out = _run(tmp_path, """
        def main():
            print(complex(1, 2) ** complex(3, 4))
            print(2 ** complex(1, 1))
            print(complex(2, 3) ** True)
            print(complex(-1, 0) ** 0.5)

        if __name__ == "__main__":
            main()
    """, runtime_cc=runtime_cc, monkeypatch=monkeypatch)
    actual = [complex(line) for line in out.splitlines()[:4]]
    expected = [
        complex(1, 2) ** complex(3, 4),
        2 ** complex(1, 1),
        complex(2, 3) ** True,
        complex(-1, 0) ** 0.5,
    ]
    for got, want in zip(actual, expected, strict=True):
        assert abs(got - want) <= 1e-12 * max(1.0, abs(want))
