"""Bigint parsing for integer literals and ``int(str[, base])`` in
no-libpython mode.

Before this regression, ``py_int_from_cstr`` (both the C runtime and the
pcc-Python port) capped accumulation at int64 and returned NULL on overflow,
so a >int64 integer literal rendered ``<null>`` and ``int("<bignum>")``
returned ``0``.  Both tiers now fall back to bignum accumulation
(``py_int_mul`` / ``py_int_add``) past the int64 limit.  Computed bignums
(``2 ** 70``) always worked and guard against regressing the fast path.
"""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path


def _compile(monkeypatch, src: Path, exe: Path) -> None:
    from pcc.frontends.python.pipeline import compile_python

    compile_python(
        str(src), str(exe),
        ir_scaffold_mode="on", libpython_mode="off", backend="self",
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


def test_bigint_literal_and_int_str(tmp_path, monkeypatch):
    src = tmp_path / "bigint_parse.py"
    exe = tmp_path / "bigint_parse.out"
    # NOTE: bignum *integer literals* go through ``py_int_from_cstr`` and stay
    # boxed, so they render correctly with this fix.  ``int("<bignum>")`` is a
    # separate, still-open bug: numeric_builtin_lowering.py unboxes the parsed
    # PyInt to native i64 (truncating bignums), so only int64-range
    # ``int(str)`` is asserted here.  See current-goal-state follow-up.
    program = textwrap.dedent("""
        def main() -> None:
            print(123456789012345678901234567890)        # bignum literal
            print(-987654321098765432109876543210)        # negative bignum literal
            print(int("9223372036854775807"))             # int64 max (fits i64)
            print(int("-9223372036854775808"))            # int64 min (fits i64)
            print(int("42"), int("-7"), int("0"))
            print(int("ff", 16), int("zz", 36))
            print(2 ** 70)                                 # computed bignum
            print(10 ** 30 + 1)

        if __name__ == "__main__":
            main()
        """).lstrip()
    src.write_text(program, encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).splitlines() == [
        "123456789012345678901234567890",
        "-987654321098765432109876543210",
        "9223372036854775807",
        "-9223372036854775808",
        "42 -7 0",
        "255 1295",
        "1180591620717411303424",
        "1000000000000000000000000000001",
    ]


def test_sum_preserves_bignum_accumulator(tmp_path, monkeypatch):
    src = tmp_path / "bigint_sum.py"
    exe = tmp_path / "bigint_sum.out"
    program = textwrap.dedent("""
        def main() -> None:
            print(sum([10**18] * 100))
            print(sum([10**30, 10**30, 1]))
            print(sum([10**18] * 3, 10**18))

        if __name__ == "__main__":
            main()
        """).lstrip()
    src.write_text(program, encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).splitlines() == [
        "100000000000000000000",
        "2000000000000000000000000000001",
        "4000000000000000000",
    ]
