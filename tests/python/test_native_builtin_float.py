"""Native dispatch for ``float(x)`` with str literal args.

Issue 11.A.2 fix: ``float("inf")`` / ``float("-inf")`` / ``float("nan")``
should lower to a native float constant, not via CPython's ``float``
builtin. Other StrLit float literals (e.g. ``float("3.14")``) likewise
fold at codegen time.
"""
from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).absolute().parents[2]
_BUILD = _REPO_ROOT / "build"
_BUILD.mkdir(parents=True, exist_ok=True)


def _compile_to_ll(source: str, name: str) -> str:
    from pcc.frontends.python.pipeline import compile_python

    src = _BUILD / f"{name}.py"
    out = _BUILD / f"{name}.ll"
    src.write_text(source, encoding="utf-8")
    compile_python(str(src), str(out), emit_llvm_only=True)
    return out.read_text(encoding="utf-8")


def _fn_body(ir_text: str, fn_suffix: str) -> str | None:
    pattern = re.compile(
        r"define\s+[^\n]*?@[A-Za-z0-9_]*"
        + re.escape(fn_suffix)
        + r"\s*\([^)]*\)[^{]*\{(.+?)\n\}",
        re.DOTALL,
    )
    m = pattern.search(ir_text)
    return m.group(1) if m else None


def test_float_inf_no_py_cpy():
    program = textwrap.dedent(
        """
        def get_inf() -> float:
            return float('inf')
        """
    )
    ir_text = _compile_to_ll(program, "nb_float_inf")
    body = _fn_body(ir_text, "get_inf")
    assert body is not None, ir_text
    assert "py_cpy_" not in body, (
        f"float('inf') body must have NO py_cpy_*; got:\n{body}"
    )


def test_float_neg_inf_no_py_cpy():
    program = textwrap.dedent(
        """
        def get_neg_inf() -> float:
            return float('-inf')
        """
    )
    ir_text = _compile_to_ll(program, "nb_float_neg_inf")
    body = _fn_body(ir_text, "get_neg_inf")
    assert body is not None, ir_text
    assert "py_cpy_" not in body, body


def test_float_nan_no_py_cpy():
    program = textwrap.dedent(
        """
        def get_nan() -> float:
            return float('nan')
        """
    )
    ir_text = _compile_to_ll(program, "nb_float_nan")
    body = _fn_body(ir_text, "get_nan")
    assert body is not None, ir_text
    assert "py_cpy_" not in body, body


def test_float_int_arg_already_native():
    """float(int_var) stays native without truncating arbitrary-precision ints."""
    program = textwrap.dedent(
        """
        def f(x: int) -> float:
            return float(x)
        """
    )
    ir_text = _compile_to_ll(program, "nb_float_int")
    body = _fn_body(ir_text, "_f")
    assert body is not None
    assert "py_cpy_" not in body
    # Function arguments are boxed Python ints. Converting through
    # py_float_to_f64 preserves bignum semantics; unbox-to-i64+sitofp would
    # silently truncate large Python ints.
    assert "py_float_to_f64" in body
    assert "py_int_to_i64" not in body


def test_float_inf_emits_native_constant():
    """The IR should reference ``0x7FF0000000000000`` (positive
    infinity bit pattern) directly."""
    program = textwrap.dedent(
        """
        def get_inf() -> float:
            return float('inf')
        """
    )
    ir_text = _compile_to_ll(program, "nb_float_inf_const")
    # LLVM may print +inf as 0x7FF0000000000000 hex or "inf" keyword.
    body = _fn_body(ir_text, "get_inf")
    assert body is not None
    has_inf_const = (
        "0x7FF0000000000000" in body
        or "inf" in body.lower()
    )
    assert has_inf_const, (
        f"expected an +inf constant in body, got:\n{body}"
    )


def test_float_literals_keep_decimal_value_in_ir():
    program = textwrap.dedent(
        """
        def zero() -> float:
            return 0.0

        def one_half() -> float:
            return 1.5
        """
    )
    ir_text = _compile_to_ll(program, "nb_float_literal_values")
    zero_body = _fn_body(ir_text, "zero")
    half_body = _fn_body(ir_text, "one_half")
    assert zero_body is not None
    assert half_body is not None
    assert (
        "0x0000000000000000" in zero_body
        or "0.000000e+00" in zero_body
    )
    assert (
        "0x3FF8000000000000" in half_body
        or "1.500000e+00" in half_body
    )
