"""Regression tests for ``pcc.ir.ir``'s no-CPython closure.

Historically these tests proved that the recursive ``pcc.ir.support.struct``
port reduced ir.py's py_cpy_* count. ir.py now carries pcc-friendly
float bit helpers directly, so the stronger invariant is that its own
functions stay at zero py_cpy_* with or without recursive stdlib.
"""
from __future__ import annotations

import re
import tempfile
from pathlib import Path

_IR_PY = Path(__file__).absolute().parents[2] / "pcc" / "ir" / "ir.py"


def _count_py_cpy_in_module_funcs(ir_text: str, mod_prefix: str) -> int:
    """Count py_cpy_* calls in functions belonging to ``mod_prefix``."""
    n = 0
    pattern = re.compile(
        r"define[^\n]+@(" + re.escape(mod_prefix)
        + r"[A-Za-z0-9_]+)[^{]*\{(.+?)\n\}",
        re.DOTALL,
    )
    for m in pattern.finditer(ir_text):
        n += len(re.findall(r"\bcall [^\n]*@py_cpy_", m.group(2)))
    return n


def _function_body(ir_text: str, symbol: str) -> str:
    match = re.search(
        r"define[^\n]+@" + re.escape(symbol) + r"\([^)]*\)[^{]*\{(.+?)\n\}",
        ir_text,
        re.DOTALL,
    )
    assert match is not None, symbol
    return match.group(1)


def test_ir_py_functions_stay_no_cpy_with_recursive_stdlib():
    """ir.py's own functions should stay py_cpy-free whether or not
    the recursive stdlib walker is enabled."""
    from pcc.frontends.python.pipeline import compile_python

    with tempfile.TemporaryDirectory() as td:
        out_baseline = Path(td) / "baseline.ll"
        out_with_port = Path(td) / "with_port.ll"
        compile_python(
            str(_IR_PY), str(out_baseline),
            emit_llvm_only=True, ir_scaffold_mode="on",
        )
        compile_python(
            str(_IR_PY), str(out_with_port),
            emit_llvm_only=True, ir_scaffold_mode="on",
            recursive_stdlib=True,
        )

        # Both compiles should succeed.
        assert out_baseline.exists()
        assert out_with_port.exists()

        baseline_text = out_baseline.read_text(encoding="utf-8")
        with_port_text = out_with_port.read_text(encoding="utf-8")

        n_baseline = _count_py_cpy_in_module_funcs(
            baseline_text, "user_pcc_ir_ir_",
        )
        n_with_port = _count_py_cpy_in_module_funcs(
            with_port_text, "user_pcc_ir_ir_",
        )
        n_float_bits = _count_py_cpy_in_module_funcs(
            with_port_text, "user_pcc_ir_support__float_bits_",
        )

        assert n_baseline == 0
        assert n_with_port == 0
        assert n_float_bits == 0
        module_str = _function_body(
            with_port_text,
            "user_pcc_ir_ir_Module___str__",
        )
        assert "strict.nolib.stub" not in module_str
        assert "py_cpy_" not in module_str
        assert "user_pcc_ir_ir__render_metadata_definition" in module_str
        metadata_renderer = _function_body(
            with_port_text,
            "user_pcc_ir_ir__render_metadata_definition",
        )
        assert "strict.nolib.stub" not in metadata_renderer
        assert "py_cpy_" not in metadata_renderer


def test_ir_py_shared_float_bits_helpers_are_compiled():
    """IR wrappers and the one canonical float-bit owner are compiled."""
    from pcc.frontends.python.pipeline import compile_python

    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "with_port.ll"
        compile_python(
            str(_IR_PY), str(out),
            emit_llvm_only=True, ir_scaffold_mode="on",
            recursive_stdlib=True,
        )
        text = out.read_text(encoding="utf-8")
        assert re.search(
            r"@user_pcc_ir_ir__float64_to_bits_ir\b", text,
        ), "ir._float64_to_bits_ir should be defined"
        assert re.search(
            r"@user_pcc_ir_ir__round_to_float32_ir\b", text,
        ), "ir._round_to_float32_ir should be defined"
        assert re.search(
            r"@user_pcc_ir_support__float_bits__float64_to_bits\b", text,
        ), "the canonical pcc.ir.support._float_bits helper should be defined"
        assert "math.copysign.sign.bits" in text
        assert "@user_pcc_stdlib_math_copysign" not in text


def test_ir_py_recursive_stdlib_pulls_float_bits_but_not_struct_port():
    """IR uses the arithmetic owner without pulling the bytes API."""
    from pcc.frontends.python.pipeline import compile_python

    with tempfile.TemporaryDirectory() as td:
        out_combined = Path(td) / "combined.ll"
        compile_python(
            str(_IR_PY), str(out_combined),
            emit_llvm_only=True, ir_scaffold_mode="on",
            recursive_stdlib=True,
        )

        text = out_combined.read_text(encoding="utf-8")
        assert "@user_pcc_ir_support_struct_" not in text
        assert "@user_pcc_ir_support__float_bits_" in text
