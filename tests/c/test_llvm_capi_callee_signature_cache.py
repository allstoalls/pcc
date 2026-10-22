from __future__ import annotations

import ast
import gc
import inspect
import weakref

import pytest

from pcc.ir import ir


def _append_call(module: ir.Module, callee: ir.Function, args):
    caller_type = ir.FunctionType(ir.VoidType(), [])
    caller_name = "caller" + str(len(module._functions))
    caller = ir.Function(module, caller_type, name=caller_name)
    block = caller.append_basic_block("entry")
    builder = ir.IRBuilder(block)
    result = builder.call(callee, args, name="result")
    builder.ret_void()
    return result, block.render()


def test_declared_callee_signature_is_rebuilt_without_memoization() -> None:
    class CountingType(ir.Type):
        def __init__(self) -> None:
            self.render_count = 0

        def __str__(self) -> str:
            self.render_count += 1
            return "i32"

    module = ir.Module("no-signature-cache")
    counted = CountingType()
    signature = ir.FunctionType(counted, [counted])
    callee = ir.Function(module, signature, name="callee")

    _append_call(module, callee, [ir.Constant(counted, 1)])
    first_render_count = counted.render_count
    _append_call(module, callee, [ir.Constant(counted, 2)])

    assert counted.render_count > first_render_count
    assert not hasattr(callee, "_callee_signature_cache_entry")
    assert not hasattr(module, "_callee_signature_cache")
    assert not hasattr(ir, "_CALLEE_SIGNATURE_CACHE")


def test_declared_callee_observes_in_place_function_type_mutations() -> None:
    module = ir.Module("signature-mutation")
    i32 = ir.IntType(32)
    i64 = ir.IntType(64)
    signature = ir.FunctionType(i32, [i32])
    callee = ir.Function(module, signature, name="callee")

    _append_call(module, callee, [ir.Constant(i32, 1)])

    signature.return_type = i64
    result, rendered = _append_call(module, callee, [ir.Constant(i32, 2)])
    assert result.type is i64
    assert "call i64 (i32) @callee(i32 2)" in rendered

    signature.args = [i32]
    _append_call(module, callee, [ir.Constant(i32, 3)])
    signature.args[0] = i64
    _, rendered = _append_call(module, callee, [ir.Constant(i64, 4)])
    assert "call i64 (i64) @callee(i64 4)" in rendered

    signature.var_arg = True
    _, rendered = _append_call(
        module,
        callee,
        [ir.Constant(i64, 5), ir.Constant(i32, 6)],
    )
    assert "call i64 (i64, ...) @callee(i64 5, i32 6)" in rendered

    nested = ir.ArrayType(i32, 1)
    nested_signature = ir.FunctionType(ir.VoidType(), [nested])
    nested_callee = ir.Function(
        module,
        nested_signature,
        name="nested_callee",
    )
    _append_call(module, nested_callee, [ir.Value(nested, "%before")])
    nested.count = 2
    _, rendered = _append_call(
        module,
        nested_callee,
        [ir.Value(nested, "%after")],
    )
    assert "call void ([2 x i32]) @nested_callee([2 x i32] %after)" in rendered


def test_function_subclass_keeps_dynamic_signature_attributes() -> None:
    module = ir.Module("function-subclass-dynamic-fields")
    i32 = ir.IntType(32)
    i64 = ir.IntType(64)
    original_signature = ir.FunctionType(i32, [i32])
    dynamic_signature = ir.FunctionType(i64, [i64])

    class DynamicFunction(ir.Function):
        def __getattribute__(self, name: str):
            if name == "ftype":
                return dynamic_signature
            if name == "name":
                return "dynamic_callee"
            return object.__getattribute__(self, name)

    callee = DynamicFunction(
        module,
        original_signature,
        name="stored_callee",
    )
    result, rendered = _append_call(module, callee, [ir.Constant(i64, 7)])

    assert result.type is i64
    assert "call i64 (i64) @dynamic_callee(i64 7)" in rendered


def test_declared_callee_rendering_does_not_depend_on_object_ids(
    monkeypatch,
) -> None:
    module = ir.Module("id-independent-signatures")
    i32 = ir.IntType(32)
    i64 = ir.IntType(64)
    first = ir.Function(module, ir.FunctionType(i32, [i32]), name="first")
    second = ir.Function(module, ir.FunctionType(i64, [i64]), name="second")
    monkeypatch.setattr(ir, "id", lambda _value: 7, raising=False)

    _, first_rendered = _append_call(module, first, [ir.Constant(i32, 1)])
    _, second_rendered = _append_call(module, second, [ir.Constant(i64, 2)])

    assert "call i32 (i32) @first(i32 1)" in first_rendered
    assert "call i64 (i64) @second(i64 2)" in second_rendered
    assert not hasattr(first, "_callee_signature_cache_entry")
    assert not hasattr(second, "_callee_signature_cache_entry")


def test_declared_callee_signatures_remain_isolated_by_function() -> None:
    i32 = ir.IntType(32)
    module = ir.Module("function-owned")
    first = ir.Function(
        module,
        ir.FunctionType(i32, [i32]),
        name="first",
    )
    second = ir.Function(
        module,
        ir.FunctionType(i32, [i32]),
        name="second",
    )

    _, first_rendered = _append_call(module, first, [ir.Constant(i32, 1)])
    _, second_rendered = _append_call(module, second, [ir.Constant(i32, 2)])

    assert "call i32 (i32) @first(i32 1)" in first_rendered
    assert "call i32 (i32) @second(i32 2)" in second_rendered


def test_signature_rendering_does_not_retain_completed_module_graph() -> None:
    def build_private_graph():
        context = ir.Context()
        identified = context.get_identified_type("Private")
        pointer = identified.as_pointer()
        module = ir.Module("cache-lifetime", context=context)
        signature = ir.FunctionType(pointer, [pointer])
        callee = ir.Function(module, signature, name="callee")
        _append_call(module, callee, [ir.Constant(pointer, None)])
        assert not hasattr(callee, "_callee_signature_cache_entry")
        assert not hasattr(module, "_callee_signature_cache")
        return (
            weakref.ref(context),
            weakref.ref(identified),
            weakref.ref(signature),
            weakref.ref(module),
            weakref.ref(callee),
        )

    refs = build_private_graph()
    gc.collect()

    assert all(reference() is None for reference in refs)
    assert not hasattr(ir, "_CALLEE_SIGNATURE_CACHE")


def _render_fixed_or_generic_call(
    *,
    arity: int,
    return_kind: str,
    formal_kinds: tuple[str, ...],
    actual_kinds: tuple[str, ...],
    var_arg: bool,
    function_pointer: bool,
    fixed: bool,
) -> tuple[str, str]:
    type_by_name = {
        "void": ir.VoidType(),
        "i32": ir.IntType(32),
        "i64": ir.IntType(64),
    }
    return_type = type_by_name[return_kind]
    formal_types = [type_by_name[kind] for kind in formal_kinds]
    signature = ir.FunctionType(return_type, formal_types, var_arg=var_arg)
    module = ir.Module("fixed-call-differential")
    if function_pointer:
        caller_type = ir.FunctionType(ir.VoidType(), [signature.as_pointer()])
        caller = ir.Function(module, caller_type, name="caller")
        callee = caller.args[0]
    else:
        caller_type = ir.FunctionType(ir.VoidType(), [])
        caller = ir.Function(module, caller_type, name="caller")
        callee = ir.Function(module, signature, name="callee")
    block = caller.append_basic_block("entry")
    builder = ir.IRBuilder(block)
    args = [
        ir.Constant(type_by_name[kind], index + 1)
        for index, kind in enumerate(actual_kinds)
    ]
    if fixed:
        fixed_call = getattr(ir, "IRBuilder_call" + str(arity))
        result = fixed_call(builder, callee, *args)
    else:
        result = ir._irbuilder_call_from_args_list(builder, callee, args)
    builder.ret_void()
    return str(result.type), block.render()


@pytest.mark.parametrize(
    (
        "arity",
        "return_kind",
        "formal_kinds",
        "actual_kinds",
        "var_arg",
        "function_pointer",
    ),
    [
        (0, "void", (), (), False, False),
        (0, "i32", (), (), False, False),
        (1, "i64", ("i32",), ("i32",), False, False),
        (2, "i32", ("i32", "i64"), ("i32", "i64"), False, False),
        (2, "i32", ("i32",), ("i32", "i64"), True, False),
        (1, "i32", ("i64",), ("i64",), False, True),
        (2, "i32", ("i32",), ("i32", "i64"), True, True),
        (1, "i32", ("i64",), ("i32",), False, False),
    ],
)
def test_fixed_call_arity_matches_generic_ir_byte_for_byte(
    arity: int,
    return_kind: str,
    formal_kinds: tuple[str, ...],
    actual_kinds: tuple[str, ...],
    var_arg: bool,
    function_pointer: bool,
) -> None:
    common = {
        "arity": arity,
        "return_kind": return_kind,
        "formal_kinds": formal_kinds,
        "actual_kinds": actual_kinds,
        "var_arg": var_arg,
        "function_pointer": function_pointer,
    }
    generic = _render_fixed_or_generic_call(**common, fixed=False)
    fixed = _render_fixed_or_generic_call(**common, fixed=True)
    assert fixed == generic


def test_fixed_call_uses_formal_type_before_reading_duck_operand_type() -> None:
    class RefOnlyOperand:
        _ref = "%duck"

    def render(fixed: bool) -> str:
        module = ir.Module("fixed-call-duck")
        i32 = ir.IntType(32)
        callee = ir.Function(
            module,
            ir.FunctionType(i32, [i32]),
            name="callee",
        )
        _append_call(module, callee, [ir.Constant(i32, 0)])
        caller = ir.Function(
            module,
            ir.FunctionType(ir.VoidType(), []),
            name="caller",
        )
        block = caller.append_basic_block("entry")
        builder = ir.IRBuilder(block)
        operand = RefOnlyOperand()
        if fixed:
            ir.IRBuilder_call1(builder, callee, operand)
        else:
            ir._irbuilder_call_from_args_list(builder, callee, [operand])
        builder.ret_void()
        return block.render()

    assert render(fixed=True) == render(fixed=False)


@pytest.mark.parametrize("arity", [0, 1, 2])
def test_fixed_call_nonfunction_fallback_matches_generic_ir(arity: int) -> None:
    def render(fixed: bool) -> tuple[str, str]:
        module = ir.Module("fixed-call-nonfunction")
        i32 = ir.IntType(32)
        opaque_callee = ir.Value(ir.PointerType(ir.IntType(8)), "%opaque")
        caller = ir.Function(
            module,
            ir.FunctionType(ir.VoidType(), []),
            name="caller",
        )
        block = caller.append_basic_block("entry")
        builder = ir.IRBuilder(block)
        args = [ir.Constant(i32, index + 1) for index in range(arity)]
        if fixed:
            helper = getattr(ir, "IRBuilder_call" + str(arity))
            result = helper(builder, opaque_callee, *args)
        else:
            result = ir._irbuilder_call_from_args_list(
                builder,
                opaque_callee,
                args,
            )
        builder.ret_void()
        return str(result.type), block.render()

    assert render(fixed=True) == render(fixed=False)


def test_fixed_call_observes_signature_mutation_without_cache() -> None:
    module = ir.Module("fixed-call-signature-mutation")
    i32 = ir.IntType(32)
    i64 = ir.IntType(64)
    signature = ir.FunctionType(i32, [i32])
    callee = ir.Function(module, signature, name="callee")
    caller = ir.Function(
        module,
        ir.FunctionType(ir.VoidType(), []),
        name="caller",
    )
    block = caller.append_basic_block("entry")
    builder = ir.IRBuilder(block)

    ir.IRBuilder_call1(builder, callee, ir.Constant(i32, 1))
    ir.IRBuilder_call1(builder, callee, ir.Constant(i32, 2))
    assert not hasattr(callee, "_callee_signature_cache_entry")

    signature.return_type = i64
    signature.args = (i64,)
    signature.var_arg = True
    result = ir.IRBuilder_call2(
        builder,
        callee,
        ir.Constant(i64, 3),
        ir.Constant(i32, 4),
    )
    assert result.type is i64
    assert "call i64 (i64, ...) @callee(i64 3, i32 4)" in block.render()


def test_small_arity_call_wrappers_delegate_directly_to_generic_core() -> None:
    for helper in (ir.IRBuilder_call0, ir.IRBuilder_call1, ir.IRBuilder_call2):
        tree = ast.parse(inspect.getsource(helper))
        called_names = [
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        ]
        assert called_names.count("_irbuilder_call_from_args_list") == 1
        assert not any(name.endswith("_fast") for name in called_names)
        assert not any(name.endswith("_slow") for name in called_names)


def test_exact_function_path_does_not_repeat_isinstance_check() -> None:
    tree = ast.parse(inspect.getsource(ir._irbuilder_call_from_args_list))
    called_names = [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]

    assert called_names.count("_is_exact_function") == 1
    assert called_names.count("isinstance") == 0


def test_small_arity_call_wrapper_invokes_generic_once(
    monkeypatch,
    capsys,
) -> None:
    module = ir.Module("generic-small-arity-call")
    i32 = ir.IntType(32)
    callee = ir.Function(
        module,
        ir.FunctionType(i32, [i32]),
        name="callee",
    )
    _append_call(module, callee, [ir.Constant(i32, 0)])
    caller = ir.Function(
        module,
        ir.FunctionType(ir.VoidType(), []),
        name="caller",
    )
    block = caller.append_basic_block("entry")
    builder = ir.IRBuilder(block)
    generic_calls = []
    original_generic = ir._irbuilder_call_from_args_list

    def record_generic(
        generic_builder,
        generic_callee,
        generic_args,
        name="",
        tail=False,
    ):
        generic_calls.append(
            (generic_builder, generic_callee, generic_args, name, tail)
        )
        return original_generic(
            generic_builder,
            generic_callee,
            generic_args,
            name=name,
            tail=tail,
        )

    monkeypatch.setattr(ir, "_DEBUG_IR_CALL_TRACE_ENABLED", False)
    monkeypatch.setattr(ir, "_irbuilder_call_from_args_list", record_generic)
    argument = ir.Constant(i32, 1)
    ir.IRBuilder_call1(builder, callee, argument)

    assert len(generic_calls) == 1
    assert generic_calls[0][0] is builder
    assert generic_calls[0][1] is callee
    assert generic_calls[0][2] == [argument]
    assert generic_calls[0][3:] == ("", False)
    assert block.render().count("call i32 (i32) @callee(i32 1)") == 1
    assert capsys.readouterr().err == ""


def test_debug_call_trace_remains_opt_in_after_small_arity_denial(
    monkeypatch,
    capsys,
) -> None:
    module = ir.Module("traced-small-arity-call")
    i32 = ir.IntType(32)
    callee = ir.Function(
        module,
        ir.FunctionType(i32, [i32]),
        name="callee",
    )
    _append_call(module, callee, [ir.Constant(i32, 0)])
    caller = ir.Function(
        module,
        ir.FunctionType(ir.VoidType(), []),
        name="caller",
    )
    block = caller.append_basic_block("entry")
    builder = ir.IRBuilder(block)
    monkeypatch.setattr(ir, "_DEBUG_IR_CALL_TRACE_ENABLED", True)
    ir.IRBuilder_call1(builder, callee, ir.Constant(i32, 1))

    trace = capsys.readouterr().err
    assert "[pcc.ir.call] enter argc=1" in trace
    assert "[pcc.ir.call] emit value" in trace
