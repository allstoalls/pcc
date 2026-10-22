"""Phase 1 Task 5: scaffold dispatch detection tests.

What's tested:
- ``_ir_scaffold_target`` correctly identifies ``self.builder.M``,
  ``parent.builder.M``, ``self.parent.builder.M``, and ``builder.M``
  patterns when M is a recognised IRBuilder method.
- ``_ir_module_symbol_target`` correctly identifies ``ir.X``
  constructor patterns.
- In ON mode, codegen raises ``ScaffoldUnsupportedError`` (a subclass
  of NotImplementedError) for recognised-but-unimplemented patterns.
- In OFF mode, the same patterns silently fall through to existing
  ``py_cpy_*`` dispatch (no behaviour change).
- The error message names the specific method/symbol so per-file
  migration can identify exactly which symbols still need lowering.
"""
from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).absolute().parents[2]
_BUILD = _REPO_ROOT / "build"
_BUILD.mkdir(parents=True, exist_ok=True)


def _build_codegen(source: str, *, mode: str):
    """Parse + type-infer ``source`` and return a fresh L1CodeGen
    instance configured with the given ir_scaffold_mode."""
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python import type_infer
    from pcc.frontends.python.codegen import layer1

    ast_mod = parse_and_lift(source, "<test>", "test_module")
    typed = type_infer.infer_module(ast_mod)
    return layer1.L1CodeGen(typed, ir_scaffold_mode=mode)


def _generate_ir(source: str, *, mode: str) -> str:
    cg = _build_codegen(source, mode=mode)
    return str(cg.generate(cg.ast_module))


def _function_body(ir_text: str, fn_name_suffix: str) -> str | None:
    pattern = re.compile(
        r"define\s+[^\n]*?@[A-Za-z0-9_]*"
        + re.escape(fn_name_suffix)
        + r"\s*\([^)]*\)[^{]*\{(.+?)\n\}",
        re.DOTALL,
    )
    m = pattern.search(ir_text)
    return m.group(1) if m else None


def _compile_pipeline_ir(source: str, name: str, *, mode: str) -> str:
    from pcc.frontends.python.pipeline import compile_python

    src = _BUILD / f"{name}.py"
    out = _BUILD / f"{name}.ll"
    src.write_text(source, encoding="utf-8")
    compile_python(
        str(src), str(out),
        emit_llvm_only=True,
        ir_scaffold_mode=mode,
    )
    return out.read_text(encoding="utf-8")


def _zero_span():
    from pcc.frontends.python.py_ast import SourceSpan

    return SourceSpan(file="<test>", line=1, col=1, end_line=1, end_col=1)


def _dyn():
    from pcc.frontends.python.py_ast import DynType

    return DynType(name="dyn")


def _name(ident: str):
    from pcc.frontends.python.py_ast import Name

    return Name(span=_zero_span(), ty=_dyn(), ident=ident)


def _attr(obj, name: str):
    from pcc.frontends.python.py_ast import Attr

    return Attr(span=_zero_span(), ty=_dyn(), obj=obj, name=name)


def test_ir_scaffold_target_self_builder():
    cg = _build_codegen("x = 1\n", mode="on")
    store_attr = _attr(_attr(_name("self"), "builder"), "store")
    assert cg._ir_scaffold_target(store_attr) == "store"


def test_ir_scaffold_target_local_builder():
    cg = _build_codegen("x = 1\n", mode="on")
    load_attr = _attr(_name("builder"), "load")
    assert cg._ir_scaffold_target(load_attr) == "load"


def test_ir_scaffold_target_tracked_irbuilder_local():
    cg = _build_codegen("x = 1\n", mode="on")
    cg._ir_builder_env_flags = {"tmp_builder": True}
    ret_attr = _attr(_name("tmp_builder"), "ret")
    assert cg._ir_scaffold_target(ret_attr) == "ret"


def test_ir_scaffold_target_parent_builder():
    cg = _build_codegen("x = 1\n", mode="on")
    ret_attr = _attr(_attr(_name("parent"), "builder"), "ret")
    assert cg._ir_scaffold_target(ret_attr) == "ret"


def test_ir_scaffold_target_self_parent_builder():
    cg = _build_codegen("x = 1\n", mode="on")
    store_attr = _attr(
        _attr(_attr(_name("self"), "parent"), "builder"),
        "store",
    )
    assert cg._ir_scaffold_target(store_attr) == "store"


def test_ir_scaffold_target_unknown_method_returns_none():
    cg = _build_codegen("x = 1\n", mode="on")
    bogus = _attr(_attr(_name("self"), "builder"), "not_a_real_method")
    assert cg._ir_scaffold_target(bogus) is None


def test_ir_scaffold_target_other_attr_returns_none():
    """``self.foo.store`` should NOT match — only ``self.builder.M``."""
    cg = _build_codegen("x = 1\n", mode="on")
    store_attr = _attr(_attr(_name("self"), "foo"), "store")
    assert cg._ir_scaffold_target(store_attr) is None


def test_ir_module_symbol_target():
    cg = _build_codegen("x = 1\n", mode="on")
    int_type = _attr(_name("ir"), "IntType")
    assert cg._ir_module_symbol_target(int_type) == "IntType"

    bogus = _attr(_name("ir"), "NotARealType")
    assert cg._ir_module_symbol_target(bogus) is None

    not_ir = _attr(_name("other"), "IntType")
    assert cg._ir_module_symbol_target(not_ir) is None


def test_llvm_capi_compat_import_is_runtime_binding_in_off_mode():
    source = textwrap.dedent(
        """
        from pcc.ir.compat import ir

        def make(n):
            return ir.IntType(n)
        """
    )

    ir_text = _generate_ir(source, mode="off")

    assert re.search(r"\bcall [^\n]*@py_cpy_import\b", ir_text)
    assert re.search(r"\bcall [^\n]*@py_cpy_getattr\b", ir_text)


def test_llvm_capi_compat_import_is_scaffold_in_on_mode():
    source = textwrap.dedent(
        """
        from pcc.ir.compat import ir

        def make(n):
            return ir.IntType(n)
        """
    )

    ir_text = _compile_pipeline_ir(source, "compat_import_on", mode="on")

    assert "@user_pcc_ir_ir_scaffold_IntType" in ir_text
    assert not re.search(r"\bcall [^\n]*@py_cpy_import\b", ir_text)


def test_dyn_append_prefers_native_list_over_unrelated_class_method():
    source = textwrap.dedent(
        """
        class Block:
            def append(self, line: str) -> None:
                return None


        def remember(active_excs, handler_exc):
            active_excs.append(handler_exc)
        """
    )

    ir_text = _generate_ir(source, mode="on")
    remember_body = _function_body(ir_text, "remember")

    assert remember_body is not None
    assert "@py_list_append" in remember_body
    assert not re.search(
        r"\bcall [^\n]*@user_test_module_Block_append\b",
        remember_body,
    )


def test_unimplemented_method_raises_via_dispatch(monkeypatch):
    """Defensive: if someone adds a method to ``_IR_BUILDER_METHODS``
    without adding it to ``_IR_SCAFFOLD_METHOD_IMPL``, scaffold mode
    must raise ``ScaffoldUnsupportedError`` with the method name in
    the message — not silently fall through to ``py_cpy_*``.

    Tests the dispatcher directly so it stays meaningful even after
    every method in ``_IR_BUILDER_METHODS`` has a lowering.
    """
    from pcc.frontends.python.codegen import ir_scaffold_lowering as layer1

    # Pick any recognised method, then temporarily evict it from IMPL
    # to simulate the "added to recognition but not yet implemented"
    # situation a future migration step might create.
    method = "store"
    assert method in layer1._IR_BUILDER_METHODS
    assert method in layer1._IR_SCAFFOLD_METHOD_IMPL

    monkeypatch.setattr(
        layer1, "_IR_SCAFFOLD_METHOD_IMPL",
        layer1._IR_SCAFFOLD_METHOD_IMPL - {method},
    )

    cg = _build_codegen("from pcc.ir.compat import ir\nx = 1\n", mode="on")
    cg._ir_builder_env_flags = {"builder": "IRBuilder"}
    self_builder = _attr(_name("builder"), method)
    from pcc.frontends.python.py_ast import Call

    fake_call = Call(
        span=_zero_span(),
        ty=_dyn(),
        func=self_builder,
        args=(_name("v"), _name("p")),
        kwargs=(),
    )
    with pytest.raises(layer1.ScaffoldUnsupportedError) as excinfo:
        cg._maybe_emit_ir_scaffold_call(fake_call)
    assert method in str(excinfo.value), (
        f"error must name the missing method {method!r}: {excinfo.value}"
    )


def test_unimplemented_symbol_raises_via_dispatch(monkeypatch):
    """Same property for ``ir.X`` symbol detection."""
    from pcc.frontends.python.codegen import ir_scaffold_lowering as layer1

    symbol = "IntType"
    assert symbol in layer1._IR_MODULE_SYMBOLS
    assert symbol in layer1._IR_SCAFFOLD_SYMBOL_IMPL

    monkeypatch.setattr(
        layer1, "_IR_SCAFFOLD_SYMBOL_IMPL",
        layer1._IR_SCAFFOLD_SYMBOL_IMPL - {symbol},
    )

    cg = _build_codegen("from pcc.ir.compat import ir\nx = 1\n", mode="on")
    ir_attr = _attr(_name("ir"), symbol)
    from pcc.frontends.python.py_ast import Call, IntLit

    fake_call = Call(
        span=_zero_span(),
        ty=_dyn(),
        func=ir_attr,
        args=(IntLit(span=_zero_span(), ty=_dyn(), value=64),),
        kwargs=(),
    )
    with pytest.raises(layer1.ScaffoldUnsupportedError) as excinfo:
        cg._maybe_emit_ir_scaffold_call(fake_call)
    assert symbol in str(excinfo.value), (
        f"error must name the missing symbol {symbol!r}: "
        f"{excinfo.value}"
    )


def test_off_mode_method_call_still_routes_to_py_cpy():
    """OFF mode never raises ScaffoldUnsupportedError; an arbitrary
    method call falls through to the existing ``py_cpy_*`` dispatch
    (status quo). Uses ``builder.fake_method(...)`` — not in the
    IRBuilder set at all, so OFF can compile it and route via
    py_cpy_*.
    """
    from pcc.frontends.python.pipeline import compile_python

    src = _REPO_ROOT / "build" / "ir_scaffold_offcheck_method.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(
        textwrap.dedent(
            """
            def fake(builder, a, b):
                builder.fake_method(a, b)
            """
        )
    , encoding="utf-8")
    out = src.with_suffix(".ll")
    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        ir_scaffold_mode="off",
    )
    text = out.read_text(encoding="utf-8")
    assert "py_cpy_" in text, (
        "OFF mode should keep the historical py_cpy_* dispatch path; "
        "found no py_cpy_* in IR — codegen route may have changed"
    )
