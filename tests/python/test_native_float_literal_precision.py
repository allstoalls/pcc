"""Float literals round through the native string-to-binary64 owner."""
from __future__ import annotations

import os
import struct
import subprocess
import sys
import textwrap
from pathlib import Path

from pcc1_gate import find_current_pcc1, skip_or_fail_no_current_pcc1


REPO = Path(__file__).absolute().parents[2]


def test_parser_mirrors_match_python_binary64_conversion():
    from pcc.frontends.python.py_lift import _parse_float_literal_lift
    from pcc.frontends.python.py_parse import _parse_float_literal

    for text in (
        "1e100",
        "6.022e23",
        "8.98846567431158e307",
        "2.004168360008973e-292",
        "5e-324",
        "1_234.5_6e-7",
    ):
        expected = float(text.replace("_", ""))
        assert _parse_float_literal(text).hex() == expected.hex()
        assert _parse_float_literal_lift(text).hex() == expected.hex()


def test_float_unary_minus_uses_ieee_fneg_and_preserves_negative_zero(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "float_unary_minus.py"
    llvm_ir = tmp_path / "float_unary_minus.ll"
    src.write_text(
        "def negate(value: float) -> float:\n"
        "    return -value\n",
        encoding="utf-8",
    )
    compile_python(
        str(src),
        str(llvm_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    text = llvm_ir.read_text(encoding="utf-8")
    assert "fneg double %value" in text
    assert "fsub double 0.000000e+00, %value" not in text


def test_float_literal_precision_matches_cpython(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "fl.py"
    exe = tmp_path / "fl.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            print(1e100)
            print(1e-100)
            print(6.022e23)
            print(1.6e-19)
            print(2.5e10)
            print(1.5e-3)
            print(1E5)
            print(9.999e99)
            print(1.23456789e50)
            print(123456789.123456789)
            print(6.62607015e-34)
            # regressions: plain decimals + small exponents
            print(3.14)
            print(0.1)
            print(3.0)
            print(0.0)
            print(100.0)
            print(2.718281828459045)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
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


def test_current_pcc1_parses_large_and_tiny_finite_float_literals(tmp_path):
    pcc1 = find_current_pcc1(REPO)
    if pcc1 is None:
        skip_or_fail_no_current_pcc1(
            "no current pcc1 binary for finite float literal parser regression"
        )
    src = tmp_path / "finite_float_literals.py"
    llvm_ir = tmp_path / "finite_float_literals.ll"
    src.write_text(
        "large: float = 8.98846567431158e307\n"
        "tiny: float = 2.004168360008973e-292\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    proc = subprocess.run(
        [
            str(pcc1),
            "--python-library",
            "--python-libpython=off",
            "--emit-llvm=" + str(llvm_ir),
            str(src),
        ],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    ir_text = llvm_ir.read_text(encoding="utf-8")
    for text in ("8.98846567431158e307", "2.004168360008973e-292"):
        expected_bits = struct.unpack(">Q", struct.pack(">d", float(text)))[0]
        assert f"0x{expected_bits:016X}" in ir_text
