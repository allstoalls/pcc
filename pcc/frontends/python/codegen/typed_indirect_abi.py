"""Literal-signature, raw-storage C calls; target ABI lowering stays in backend."""
from __future__ import annotations

from pcc.ir.compat import ir
from pcc.frontends.python.py_ast import (
    BoolType, DynType, FloatType, IntType, NoneLit, RawPointerType, StrLit, TupleExpr,
)
from pcc.frontends.python.codegen.user_function_decl_lowering import _typed_c_abi_ir_type


def _literal_type(expr):
    if not isinstance(expr, StrLit):
        raise ValueError("typed C ABI type must be a string literal")
    return _typed_c_abi_ir_type(expr.value)


def _is_raw_pointer_annotation(owner, ty):
    return isinstance(ty, RawPointerType)


def _validate_operand(owner, expr, ty):
    if isinstance(ty, ir.LiteralStructType):
        if not isinstance(expr, TupleExpr) or len(expr.elems) != len(ty.elements):
            raise ValueError("typed C ABI aggregate requires a matching nested tuple literal")
        for element, field in zip(expr.elems, ty.elements):
            _validate_operand(owner, element, field)
        return
    if isinstance(expr, TupleExpr):
        raise ValueError("typed C ABI scalar argument cannot be a tuple")
    semantic_type = expr.ty
    if isinstance(ty, ir.PointerType):
        valid = (isinstance(semantic_type, (IntType, DynType))
                 or _is_raw_pointer_annotation(owner, semantic_type))
    elif isinstance(ty, ir.IntType):
        valid = isinstance(semantic_type, (IntType, BoolType, DynType))
    else:
        valid = isinstance(semantic_type, (IntType, BoolType, FloatType, DynType))
    if not valid:
        raise ValueError("typed C ABI argument has an incompatible value type")


def _scalar(owner, expr, ty, roots, cpy_owned, pointer_updates, slot):
    value = owner._emit_expr_with_cpy_operand_cleanup(
        expr, tuple(cpy_owned), rooted_pcc_lifetimes=tuple(roots),
    )
    if (isinstance(value.type, ir.PointerType) and owner._raw_addresses_are_ints()
            and not _is_raw_pointer_annotation(owner, expr.ty)):
        if value in getattr(owner, "_cpy_values", ()):
            if owner._cpy_value_is_owned(value):
                cpy_owned.append(value)
        elif not owner._value_is_never_gc_object(value):
            root = owner._enter_container_temp_root(value, owner._fresh("abi.argument"))
            roots.append((root, owner._owned_release_needed(value, expr)))
            if isinstance(ty, ir.PointerType) and not isinstance(expr.ty, IntType):
                pointer_updates.append((root, expr.ty, slot))
    previous = owner._current_try_err_block()
    if roots or cpy_owned:
        target = previous if previous is not None else owner._ensure_fn_err_exit()
        owner._try_err_block = owner._make_cpy_operand_cleanup_block(
            tuple(cpy_owned), (), target, "abi.argument.cleanup",
            rooted_pcc_lifetimes=tuple(roots),
        )
    try:
        if isinstance(ty, ir.PointerType):
            value = owner._pointer_or_address_operand(value, expr.ty)
            if not owner._ir_type_matches(value.type, ty):
                value = owner.builder.bitcast(value, ty)
        elif isinstance(ty, ir.IntType):
            value = owner._to_int64(value, expr.ty)
            if owner._raw_addresses_are_ints():
                owner._emit_post_call_err_check(expr.span)
            if ty.width < 64:
                value = owner.builder.trunc(value, ty)
        else:
            value = owner._to_double(value, expr.ty)
            if owner._raw_addresses_are_ints():
                owner._emit_post_call_err_check(expr.span)
            if isinstance(ty, ir.FloatType) and isinstance(value.type, ir.DoubleType):
                value = owner.builder.fptrunc(value, ty)
            elif isinstance(ty, ir.DoubleType) and isinstance(value.type, ir.FloatType):
                value = owner.builder.fpext(value, ty)
        return value
    finally:
        owner._try_err_block = previous


def _store_operand(owner, expr, ty, slot, roots, cpy_owned, pointer_updates):
    if not isinstance(ty, ir.LiteralStructType):
        value = _scalar(owner, expr, ty, roots, cpy_owned, pointer_updates, slot)
        owner.builder.store(value, slot)
        return
    zero = ir.Constant(ir.IntType(32), 0)
    for index, field in enumerate(ty.elements):
        address = owner.builder.gep(slot, [zero, ir.Constant(ir.IntType(32), index)])
        _store_operand(owner, expr.elems[index], field, address, roots, cpy_owned, pointer_updates)


def _operand_slot(owner, expr, ty, roots, cpy_owned, pointer_updates):
    slot = owner.builder.alloca(ty, name=owner._fresh("abi.operand"))
    _store_operand(owner, expr, ty, slot, roots, cpy_owned, pointer_updates)
    return slot


def emit_call(owner, expr):
    owner._unsafe_expect_arity("call_c_abi", expr, 5)
    restype = _literal_type(expr.args[1])
    argtypes_expr = expr.args[2]
    values = expr.args[3]
    if not isinstance(argtypes_expr, TupleExpr):
        raise ValueError("typed C ABI argtypes must be a tuple literal")
    argtypes = [_literal_type(arg) for arg in argtypes_expr.elems]
    if any(isinstance(ty, ir.VoidType) for ty in argtypes):
        raise ValueError("typed C ABI arguments cannot be void")
    if not isinstance(values, TupleExpr) or len(values.elems) != len(argtypes):
        raise ValueError("typed C ABI values must be a tuple literal matching argtypes")
    pointer = ir.IntType(8).as_pointer()
    _validate_operand(owner, expr.args[0], pointer)
    for value, ty in zip(values.elems, argtypes):
        _validate_operand(owner, value, ty)
    is_void = isinstance(restype, ir.VoidType)
    if is_void:
        if not isinstance(expr.args[4], NoneLit):
            raise ValueError("typed C ABI void result requires None storage")
    else:
        if isinstance(expr.args[4], NoneLit):
            raise ValueError("typed C ABI non-void result requires storage")
        _validate_operand(owner, expr.args[4], pointer)

    # Validate the entire schema before evaluating anything or trusting the
    # freestanding indirect-call boundary. Operands then run once, left to right.
    roots = []
    cpy_owned = []
    pointer_updates = []
    function_slot = _operand_slot(owner, expr.args[0], pointer, roots, cpy_owned, pointer_updates)
    argument_slots = [_operand_slot(owner, value, ty, roots, cpy_owned, pointer_updates)
                      for value, ty in zip(values.elems, argtypes)]
    destination_slot = None
    if not is_void:
        destination_slot = _operand_slot(owner, expr.args[4], pointer, roots, cpy_owned, pointer_updates)
    # Another operand may have temporarily pinned/unpinned an alias. Reload
    # roots and rebuild pointer fields only after every operand has run, then
    # pin the current pointees for the complete foreign call and result store.
    for root, _owned in roots:
        current = owner.builder.call(owner.runtime["pcc_gc_load_ptr"],
            [ir.Constant(pointer, None), owner._as_gc_ptr(root)])
        owner._gc_unpin(current)
        owner._gc_pin(current)
    for root, semantic_type, slot in pointer_updates:
        current = owner.builder.call(owner.runtime["pcc_gc_load_ptr"],
            [ir.Constant(pointer, None), owner._as_gc_ptr(root)])
        owner.builder.store(owner._pointer_or_address_operand(current, semantic_type), slot)
    function = owner.builder.load(function_slot)
    arguments = [owner.builder.load(slot) for slot in argument_slots]
    signature = ir.FunctionType(restype, argtypes)
    function = owner.builder.bitcast(function, signature.as_pointer())
    result = owner.builder.call(function, arguments,
                                name="" if is_void else owner._fresh("abi.result"))
    if destination_slot is not None:
        destination = owner.builder.load(destination_slot)
        destination = owner.builder.bitcast(destination, restype.as_pointer())
        owner.builder.store(result, destination)
    if roots:
        owner._release_rooted_pcc_lifetimes(tuple(roots))
    for value in reversed(cpy_owned):
        owner.builder.call(owner.runtime["py_cpy_decref"], [value])
        owner._forget_owned_cpy_value(value)
    return owner._unsafe_void_result()


def emit_layout(owner, intrinsic, expr):
    owner._unsafe_expect_arity(intrinsic, expr, 1)
    ty = _literal_type(expr.args[0])
    if isinstance(ty, ir.VoidType):
        raise ValueError("typed C ABI void has no storage size or alignment")
    i32 = ir.IntType(32)
    one = ir.Constant(i32, 1)
    if intrinsic == "c_abi_sizeof":
        address = owner.builder.gep(ir.Constant(ty.as_pointer(), None), [one])
    else:
        # The offset of T in {i8,T} is its alignment for the supported
        # naturally aligned scalar/struct grammar. The backend owns padding.
        padded = ir.LiteralStructType([ir.IntType(8), ty])
        address = owner.builder.gep(ir.Constant(padded.as_pointer(), None),
                                    [ir.Constant(i32, 0), one])
    return owner.builder.ptrtoint(address, ir.IntType(64))
