"""CPython parity for ``int`` arithmetic, ported from
``Lib/test/test_int.py`` and ``Lib/test/test_long.py``.

Each test compiles a tiny program with pcc's no-libpython pipeline
(``libpython_mode="off"``, ``ir_scaffold_mode="on"``) and asserts the
exact stdout that CPython would produce. Without ``libpython=off``
this whole file is meaningless — the goal is to verify pcc's native
``int`` runtime, not delegate back to CPython.

Reference contract (from CPython):
  * ``int`` is arbitrary precision (bignum)
  * ``int / int`` always returns ``float``
  * ``//`` is floor division (rounds toward -inf)
  * ``%`` matches the divisor's sign
  * ``**`` honours operand types and overflow promotion
  * shift / bitwise on negatives uses two's complement view
"""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path


def _compile(monkeypatch, src: Path, exe: Path) -> None:
    from pcc.frontends.python.pipeline import compile_python

    compile_python(
        str(src), str(exe),
        ir_scaffold_mode="on", libpython_mode="off",
    )


def _run(exe: Path, timeout: float = 30.0) -> str:
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=timeout,
    )
    assert result.returncode == 0, (
        f"{exe.name} exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result.stdout


def test_int_basic_addition_subtraction(tmp_path, monkeypatch):
    src = tmp_path / "int_add_sub.py"
    exe = tmp_path / "int_add_sub.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            print(1 + 2)
            print(10 - 3)
            print(0 - 5)
            print(-3 + 5)
            print(100 + 200 + 300)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["3", "7", "-5", "2", "600"]


def test_int_multiplication_division(tmp_path, monkeypatch):
    src = tmp_path / "int_mul_div.py"
    exe = tmp_path / "int_mul_div.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            print(3 * 4)
            print(-2 * 5)
            print(10 // 3)
            print(-10 // 3)
            print(10 % 3)
            print(-10 % 3)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    # CPython floor-div: -10 // 3 == -4; -10 % 3 == 2.
    assert _run(exe).strip().splitlines() == ["12", "-10", "3", "-4", "1", "2"]


def test_int_power(tmp_path, monkeypatch):
    src = tmp_path / "int_pow.py"
    exe = tmp_path / "int_pow.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            print(2 ** 10)
            print(3 ** 0)
            print(0 ** 0)
            print(2 ** 30)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["1024", "1", "1", "1073741824"]


def test_int_bitwise_ops(tmp_path, monkeypatch):
    src = tmp_path / "int_bitwise.py"
    exe = tmp_path / "int_bitwise.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            print(0xF0 & 0x0F)
            print(0xF0 | 0x0F)
            print(0xFF ^ 0x0F)
            print(~0)
            print(0xAA & 0x55)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["0", "255", "240", "-1", "0"]


def test_int_shift_ops(tmp_path, monkeypatch):
    src = tmp_path / "int_shift.py"
    exe = tmp_path / "int_shift.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            print(1 << 5)
            print(256 >> 2)
            print(1 << 30)
            print((-1) >> 1)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    # CPython arithmetic shift: -1 >> 1 == -1.
    assert _run(exe).strip().splitlines() == ["32", "64", "1073741824", "-1"]


def test_int_to_str_conversion(tmp_path, monkeypatch):
    src = tmp_path / "int_to_str.py"
    exe = tmp_path / "int_to_str.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            print(str(0))
            print(str(42))
            print(str(-7))
            print(str(1000000))

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["0", "42", "-7", "1000000"]


def test_int_from_str_conversion(tmp_path, monkeypatch):
    src = tmp_path / "int_from_str.py"
    exe = tmp_path / "int_from_str.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            print(int("0"))
            print(int("42"))
            print(int("-7"))
            print(int("  100  "))

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["0", "42", "-7", "100"]


def test_int_comparisons(tmp_path, monkeypatch):
    src = tmp_path / "int_cmp.py"
    exe = tmp_path / "int_cmp.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            print(1 < 2)
            print(2 <= 2)
            print(3 > 2)
            print(3 >= 3)
            print(1 == 1)
            print(1 != 2)
            print(-1 < 0)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["True"] * 7

def test_int_large_bignum_print(tmp_path, monkeypatch):
    src = tmp_path / "int_bignum.py"
    exe = tmp_path / "int_bignum.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            print(10 ** 40)
            print(2 ** 100)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == [
        "10000000000000000000000000000000000000000",
        "1267650600228229401496703205376",
    ]
