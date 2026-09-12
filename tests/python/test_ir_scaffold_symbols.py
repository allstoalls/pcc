"""Phase 3 Task 17: ``ir.X`` type/value constructor scaffold lowering.

Each supported ``ir.X(args)`` lowers to an explicit scaffold helper in
ON mode. Helpers intentionally encode the real constructor ABI instead
of pretending every Python ``__init__`` is a C-style constructor.

Note: testing OFF mode for these constructors is not meaningful —
``pcc.llvm_capi`` is a scaffold module, so OFF treats imported ``ir``
as compile-time-only and rejects runtime references with
L1CodegenError "unbound name 'ir'". The detection / dispatch tests
already cover that ON mode raises cleanly when a symbol isn't
implemented.
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
    from pcc.py_frontend.pipeline import compile_python

    src = _BUILD / f"{name}.py"
    out = _BUILD / f"{name}.ll"
    src.write_text(source, encoding="utf-8")
    compile_python(
        str(src), str(out),
        emit_llvm_only=True,
        ir_scaffold_mode=mode,
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


_PARAM_SIMPLE = [
    ("IntType", 1),
    ("PointerType", 1),
    ("VoidType", 0),
    ("DoubleType", 0),
    ("FloatType", 0),
    ("HalfType", 0),
    ("ArrayType", 2),
    ("Constant", 2),
    ("Module", 0),          # 0 positional + optional name kwarg
    ("IRBuilder", 1),
    ("IdentifiedStructType", 2),
    ("Context", 0),
]


_SPECIAL_SYMBOL_SUFFIXES = {
    "IntType": "scaffold_IntType",
    "PointerType": "scaffold_PointerType",
    "ArrayType": "scaffold_ArrayType",
    "Constant": "scaffold_Constant_obj",
    "IRBuilder": "scaffold_IRBuilder",
    "IdentifiedStructType": "scaffold_IdentifiedStructType",
    "Context": "scaffold_Context",
    "Module": "scaffold_Module___init__",
}


def _expected_scaffold_symbol(symbol: str) -> str:
    suffix = _SPECIAL_SYMBOL_SUFFIXES.get(symbol, f"{symbol}___init__")
    return f"@user_pcc_llvm_capi_ir_{suffix}"


def _gen_simple_program(symbol: str, arity: int) -> str:
    args = ", ".join(f"a{i}" for i in range(arity))
    params = ", ".join([f"a{i}" for i in range(arity)]) or ""
    sig = "()" if arity == 0 else "(" + params + ")"
    body_args = "()" if arity == 0 else "(" + args + ")"
    return textwrap.dedent(
        f"""
        from pcc.llvm_capi import ir

        def use_sym{sig}:
            return ir.{symbol}{body_args}
        """
    )


@pytest.mark.parametrize("symbol,arity", _PARAM_SIMPLE)
def test_simple_symbol_emits_extern(symbol, arity):
    program = _gen_simple_program(symbol, arity)
    ir_text = _compile_to_ll(program, f"sym_{symbol}", mode="on")
    expected = _expected_scaffold_symbol(symbol)
    assert expected in ir_text, (
        f"ON mode must emit {expected}; got:\n" + ir_text
    )


@pytest.mark.parametrize("symbol,arity", _PARAM_SIMPLE)
def test_simple_symbol_body_clean(symbol, arity):
    program = _gen_simple_program(symbol, arity)
    ir_text = _compile_to_ll(program, f"sym_{symbol}_clean", mode="on")
    body = _function_body(ir_text, "use_sym")
    assert body is not None, ir_text
    assert _expected_scaffold_symbol(symbol) in body, body
    assert "py_cpy_" not in body, (
        f"ON mode use_sym body for ir.{symbol} must have ZERO "
        f"py_cpy_*; got:\n" + body
    )


def test_constant_dynamic_int_uses_i64_scaffold_not_object_handle():
    program = textwrap.dedent(
        """
        from pcc.llvm_capi import ir

        def use_const(ty, value: int):
            return ir.Constant(ty, value)
        """
    )
    ir_text = _compile_to_ll(program, "sym_constant_dynamic_int", mode="on")
    body = _function_body(ir_text, "use_const")
    assert body is not None
    assert "@user_pcc_llvm_capi_ir_scaffold_Constant_i64" in body, body
    assert "@user_pcc_llvm_capi_ir_scaffold_Constant_obj" not in body, body
    assert "inttoptr i64 %value" not in body, body


def test_constant_runtime_int_value_uses_i64_scaffold_not_object_handle():
    program = textwrap.dedent(
        """
        from pcc.llvm_capi import ir

        def use_const(ty, xs):
            for i, _x in enumerate(xs):
                return ir.Constant(ty, i)
            return ir.Constant(ty, 0)
        """
    )
    ir_text = _compile_to_ll(program, "sym_constant_runtime_int", mode="on")
    body = _function_body(ir_text, "use_const")
    assert body is not None
    assert "@user_pcc_llvm_capi_ir_scaffold_Constant_i64" in body, body
    assert "@user_pcc_llvm_capi_ir_scaffold_Constant_obj" not in body, body
    assert "inttoptr i64 %i" not in body, body


def test_value_ctor_dispatches_to_native_scaffold_symbol():
    program = textwrap.dedent(
        """
        from pcc.llvm_capi import ir

        def use_value(ty, ref):
            return ir.Value(ty, ref)
        """
    )
    ir_text = _compile_to_ll(program, "sym_value_ctor", mode="on")
    assert "@user_pcc_llvm_capi_ir_scaffold_Value" in ir_text
    body = _function_body(ir_text, "use_value")
    assert body is not None
    assert "py_cpy_" not in body, body


def test_module_named_kwarg_uses_named_extern():
    program = textwrap.dedent(
        """
        from pcc.llvm_capi import ir

        def use_module(name):
            return ir.Module(name=name)
        """
    )
    ir_text = _compile_to_ll(program, "sym_module_named", mode="on")
    assert "@user_pcc_llvm_capi_ir_Module___init___named" in ir_text


def test_global_variable_named_kwarg_uses_named_extern():
    program = textwrap.dedent(
        """
        from pcc.llvm_capi import ir

        def use_gv(module, ty, name):
            return ir.GlobalVariable(module, ty, name=name)
        """
    )
    ir_text = _compile_to_ll(program, "sym_gv_named", mode="on")
    assert (
        "@user_pcc_llvm_capi_ir_GlobalVariable___init___named" in ir_text
    )


def test_function_ctor_no_name_kwarg():
    program = textwrap.dedent(
        """
        from pcc.llvm_capi import ir

        def use_fn(module, fn_ty):
            return ir.Function(module, fn_ty)
        """
    )
    ir_text = _compile_to_ll(program, "sym_function_no_name", mode="on")
    assert "@user_pcc_llvm_capi_ir_Function___init__" in ir_text
    assert (
        "@user_pcc_llvm_capi_ir_Function___init___named" not in ir_text
    )


def test_function_ctor_with_name_kwarg():
    program = textwrap.dedent(
        """
        from pcc.llvm_capi import ir

        def use_fn(module, fn_ty, name):
            return ir.Function(module, fn_ty, name=name)
        """
    )
    ir_text = _compile_to_ll(program, "sym_function_named", mode="on")
    assert "@user_pcc_llvm_capi_ir_Function___init___named" in ir_text


def test_function_type_ctor():
    program = textwrap.dedent(
        """
        from pcc.llvm_capi import ir

        def use_fnty(ret_ty, p1, p2):
            return ir.FunctionType(ret_ty, [p1, p2])
        """
    )
    ir_text = _compile_to_ll(program, "sym_fnty", mode="on")
    assert "@user_pcc_llvm_capi_ir_FunctionType___init__2" in ir_text
    body = _function_body(ir_text, "use_fnty")
    assert body is not None
    assert "py_cpy_" not in body, body


def test_function_type_ctor_zero_params():
    program = textwrap.dedent(
        """
        from pcc.llvm_capi import ir

        def use_fnty0(ret_ty):
            return ir.FunctionType(ret_ty, [])
        """
    )
    ir_text = _compile_to_ll(program, "sym_fnty0", mode="on")
    assert "@user_pcc_llvm_capi_ir_FunctionType___init__0" in ir_text


def test_function_type_ctor_three_params_has_runtime_bridge():
    program = textwrap.dedent(
        """
        from pcc.llvm_capi import ir

        def use_fnty3(ret_ty, p1, p2, p3):
            return ir.FunctionType(ret_ty, [p1, p2, p3])
        """
    )
    ir_text = _compile_to_ll(program, "sym_fnty3", mode="on")
    assert "@user_pcc_llvm_capi_ir_FunctionType___init__3" in ir_text
    assert "def FunctionType___init__3(" in (
        _REPO_ROOT / "pcc" / "llvm_capi" / "ir.py"
    ).read_text(encoding="utf-8")


def test_function_type_ctor_eight_params_has_runtime_bridge():
    program = textwrap.dedent(
        """
        from pcc.llvm_capi import ir

        def use_fnty8(ret_ty, p1, p2, p3, p4, p5, p6, p7, p8):
            return ir.FunctionType(ret_ty, [p1, p2, p3, p4, p5, p6, p7, p8])
        """
    )
    ir_text = _compile_to_ll(program, "sym_fnty8", mode="on")
    assert "@user_pcc_llvm_capi_ir_FunctionType___init__8" in ir_text
    assert "def FunctionType___init__8(" in (
        _REPO_ROOT / "pcc" / "llvm_capi" / "ir.py"
    ).read_text(encoding="utf-8")


def test_function_type_ctor_dynamic_variadic_flag_boxes_bool_handle():
    program = textwrap.dedent(
        """
        from pcc.llvm_capi import ir

        def use_fnty_dyn(ret_ty, param_types, is_variadic: bool):
            return ir.FunctionType(
                ret_ty,
                param_types,
                var_arg=is_variadic,
            )
        """
    )
    ir_text = _compile_to_ll(program, "sym_fnty_dyn_va", mode="on")
    body = _function_body(ir_text, "use_fnty_dyn")
    assert body is not None
    assert "@user_pcc_llvm_capi_ir_FunctionType___init___dyn" in body
    assert "@py_bool_from_bit" in body
    assert "inttoptr i1" not in body
    assert "py_cpy_" not in body, body


def test_literal_struct_three_elems():
    program = textwrap.dedent(
        """
        from pcc.llvm_capi import ir

        def use_struct(t1, t2, t3):
            return ir.LiteralStructType([t1, t2, t3])
        """
    )
    ir_text = _compile_to_ll(program, "sym_literal_struct", mode="on")
    assert (
        "@user_pcc_llvm_capi_ir_LiteralStructType___init__3" in ir_text
    )
    body = _function_body(ir_text, "use_struct")
    assert body is not None
    assert "py_cpy_" not in body, body


def test_literal_struct_one_and_two_elems_have_runtime_providers():
    program = textwrap.dedent(
        """
        from pcc.llvm_capi import ir

        def use_struct1(t1):
            return ir.LiteralStructType([t1])

        def use_struct2(t1, t2):
            return ir.LiteralStructType([t1, t2])
        """
    )
    ir_text = _compile_to_ll(program, "sym_literal_struct_small", mode="on")
    assert "@user_pcc_llvm_capi_ir_LiteralStructType___init__1" in ir_text
    assert "@user_pcc_llvm_capi_ir_LiteralStructType___init__2" in ir_text
    body1 = _function_body(ir_text, "use_struct1")
    body2 = _function_body(ir_text, "use_struct2")
    assert body1 is not None
    assert body2 is not None
    assert "py_cpy_" not in body1, body1
    assert "py_cpy_" not in body2, body2
