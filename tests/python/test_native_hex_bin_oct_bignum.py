"""``hex()``/``bin()``/``oct()`` of a bignum int, no-libpython (both tiers).

Was a C<->port DRIFT bug: `py_int_based_repr` reduced the operand to i64, and on
overflow (>64-bit) the C source RAISED "not yet supported" while the pcc-Python
port silently returned the DECIMAL value (wrong, no base/prefix). Fix adds a
bignum base-N converter `py_bigint_to_base_cstr` in BOTH tiers
(`py_int_decimal.c` / `py_int_decimal.py`, mirroring the decimal `py_bigint_to_cstr`
via repeated divmod by the base) and wires the `py_int_based_repr` bignum branch
to it. Now matches CPython for arbitrary-precision ints.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

PROGRAM = textwrap.dedent("""
    def main() -> None:
        print(hex(2 ** 64))
        print(bin(2 ** 64))
        print(oct(2 ** 64))
        print(hex(-(2 ** 64)))
        print(hex(10 ** 20))
        print(hex(2 ** 128 - 1))
        # regressions: small int hex/bin/oct unchanged
        print(hex(255))
        print(bin(5))
        print(oct(64))
        print(hex(-255))

    if __name__ == "__main__":
        main()
    """).lstrip()


@pytest.mark.parametrize("runtime_cc", [None], ids=["port"])
def test_hex_bin_oct_bignum_matches_cpython(tmp_path, monkeypatch, runtime_cc):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "hexbig.py"
    exe = tmp_path / "hexbig.out"
    src.write_text(PROGRAM, encoding="utf-8")
    compile_python(
        str(src), str(exe),
        ir_scaffold_mode="on", libpython_mode="off", backend="self",
    )
    cpython = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True, timeout=30,
    ).stdout
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == cpython
