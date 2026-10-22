"""Membership tests stay native when only the needle is CPython-backed.

The old lowering checked ``_expr_looks_cpython(lhs) or
_expr_looks_cpython(rhs)`` before emitting either side. That was too broad:
native ``getattr(obj, name, default)`` expressions look CPython-ish because
``getattr`` is also a fallback builtin, so ``x in getattr(...)`` routed the
whole membership test through CPython ``__contains__``.
"""
from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).absolute().parents[2]
_BUILD = _REPO_ROOT / "build"
_BUILD.mkdir(parents=True, exist_ok=True)


def _compile_to_ll(source: str, name: str, *, mode: str) -> str:
    from pcc.frontends.python.pipeline import compile_python

    src = _BUILD / f"{name}.py"
    out = _BUILD / f"{name}.ll"
    src.write_text(source, encoding="utf-8")
    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        ir_scaffold_mode=mode,
        libpython_mode="auto",
    )
    return out.read_text(encoding="utf-8")


def _function_body(ir_text: str, fn_name_suffix: str) -> str | None:
    pattern = re.compile(
        r"define\s+[^\n]*?@[A-Za-z0-9_]*"
        + re.escape(fn_name_suffix)
        + r"\s*\([^)]*\)[^{]*\{(.+?)\n\}",
        re.DOTALL,
    )
    m = pattern.search(ir_text)
    return m.group(1) if m else None


@pytest.mark.parametrize("mode", ["off", "on"])
def test_getattr_membership_uses_native_obj_contains(mode):
    program = textwrap.dedent(
        """
        class Box:
            def __init__(self) -> None:
                self.items = ("a", "b")

        def f(box: Box, name: str) -> bool:
            return name in getattr(box, "items", ())
        """
    )
    ir = _compile_to_ll(program, f"membership_getattr_{mode}", mode=mode)
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_obj_contains" in body, body
    assert "cpy.fn.__contains__" not in body, body
    assert "cpy.contains" not in body, body


@pytest.mark.parametrize("mode", ["off", "on"])
def test_cpy_needle_in_native_list_bridges(mode):
    program = textwrap.dedent(
        """
        import decimal

        def f(values: list[str]) -> bool:
            x = decimal.Decimal("1")
            return x in values
        """
    )
    ir = _compile_to_ll(program, f"membership_cpy_needle_{mode}", mode=mode)
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_list_contains" in body, body
    assert "@py_cpy_to_pcc_obj" in body, body
    assert "cpy.fn.__contains__" not in body, body


def test_cpy_container_still_uses_cpython_contains():
    # os.environ.keys() is now native and no longer exercises this boundary.
    program = textwrap.dedent(
        """
        def f(name: str) -> bool:
            return name in eval("['a', 'b']")
        """
    )
    ir = _compile_to_ll(program, "membership_cpy_container", mode="off")
    body = _function_body(ir, "f")
    assert body is not None
    # The pcc SSA naming for the looked-up dunder method is
    # ``cpy.fn.__contains`` (the trailing ``__`` was dropped from
    # generated value names at some point); the attribute *symbol*
    # that's getattr'd still spells the full dunder name. Match the
    # attribute symbol which is the actual semantic marker that pcc is
    # routing through CPython's ``__contains__`` rather than the
    # native ``py_obj_contains`` path.
    assert "@.cpy.attr.__contains__" in body, body
    assert "@py_obj_contains" not in body, body


def test_os_environ_keys_membership_stays_native():
    ir = _compile_to_ll(
        'import os\ndef f(name: str) -> bool:\n    return name in os.environ.keys()\n',
        "membership_native_environ_keys", mode="off",
    )
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_os_environ_snapshot" in body
    assert "@py_obj_contains" in body
    assert "@.cpy.attr.__contains__" not in body


@pytest.mark.parametrize("foreign_needle", [False, True])
def test_late_cpy_container_preserves_precomputed_needle(monkeypatch, foreign_needle):
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.py_ast import Call, Name

    original_hint = L1CodeGen._expr_looks_cpython
    original_emit = L1CodeGen._emit_expr

    def is_eval(expr):
        return isinstance(expr, Call) and isinstance(expr.func, Name) and expr.func.ident == "eval"

    def hide_eval_hint(self, expr):
        return False if is_eval(expr) else original_hint(self, expr)

    def emit_late_eval(self, expr):
        # Supply a real CPython call despite the deliberately hidden source
        # hint. The operand's emitted domain must govern the late dispatch.
        if is_eval(expr):
            function = self._load_cpython_builtin("eval")
            return self._emit_cpy_func_call(function, "late.eval", expr.args)
        return original_emit(self, expr)

    monkeypatch.setattr(L1CodeGen, "_expr_looks_cpython", hide_eval_hint)
    monkeypatch.setattr(L1CodeGen, "_emit_expr", emit_late_eval)
    needle = 'decimal.Decimal("1")' if foreign_needle else '"a"'
    ir = _compile_to_ll(
        'import decimal\ndef f() -> bool:\n    return ' + needle + ' in eval("[1]")\n',
        "membership_late_cpy_" + str(foreign_needle), mode="off",
    )
    body = _function_body(ir, "f")
    assert body is not None
    assert "@.cpy.attr.__contains__" in body
    assert "@py_cpy_to_pcc_obj" not in body
    if not foreign_needle:
        assert "@py_cpy_from_pcc_obj" in body


@pytest.mark.parametrize("mode", ["off", "on"])
def test_narrowed_optional_dict_membership_uses_native_dict_contains(mode):
    program = textwrap.dedent(
        """
        from typing import Optional

        def f(name: str, native_table: Optional[dict]) -> bool:
            return native_table is not None and name in native_table
        """
    )
    ir = _compile_to_ll(program, f"membership_optional_dict_{mode}", mode=mode)
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_dict_contains" in body, body
    assert "cpy.fn.__contains__" not in body, body
