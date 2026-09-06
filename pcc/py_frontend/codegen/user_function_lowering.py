"""User-function body and direct-call lowering for L1CodeGen."""

from __future__ import annotations

import os
import sys
from typing import Optional

from pcc.llvm_capi.compat import ir

from .. import low_ir as pcc_low_ir
from ..py_ast import (
    Arg,
    Assign,
    Attr,
    BinOp,
    BoolLit,
    BoolType,
    BytesLit,
    Call,
    Compare,
    DynType,
    Expr,
    ExprStmt,
    FloatLit,
    FloatType,
    FuncDef,
    If,
    IfExpr,
    IntLit,
    IntType,
    ListType,
    Name,
    NoneLit,
    NoneType,
    Return,
    StrLit,
    StrType,
    Type,
    UnaryOp,
    ValueArrayType,
    While,
)
from . import marshal
from .errors import L1CodegenError
from .exact_int_lowering import (
    allocate_forced_exact_int_locals,
    bind_forced_exact_int_parameter,
    forced_exact_int_local_names,
)
from .expr_helper_lowering import emit_python_floordiv_i64_unchecked
from .runtime_abi import declare_runtime_global

_I1 = ir.IntType(1)
_I8 = ir.IntType(8)
_I32 = ir.IntType(32)
_I64 = ir.IntType(64)
_DOUBLE = ir.DoubleType()
_VOID = ir.VoidType()
_CSTR = _I8.as_pointer()
_LOW_F64 = 5

_METH_DIRECT = "direct"
_METH_CLASS = "class"
_METH_STATIC = "static"

_PCC_FUNC_SIGNATURE_MAGIC = "__pcc_func_signature_v1__"
_PCC_FUNC_KIND = {
    "pos": 0,
    "pos_only": 1,
    "kw_only": 2,
    "*args": 3,
    "**kwargs": 4,
}


def _func_codegen_log(parent, enabled: bool, func_name: str, label: str) -> None:
    if not enabled:
        return
    mod_name = parent.ast_module.name or "<module>"
    sys.stderr.write("[pcc.codegen] " + mod_name + ":" + func_name + ":" + label + "\n")


def _export_arg(
    name: str,
    annotation=None,
    *,
    kind: str = "pos",
    has_default: bool = False,
):
    return {
        "name": name,
        "kind": kind,
        "has_default": has_default,
        "annotation": annotation,
    }


def _type_kind_key(ty: Type) -> str:
    if isinstance(ty, IntType):
        return "IntType"
    if isinstance(ty, BoolType):
        return "BoolType"
    if isinstance(ty, StrType):
        return "StrType"
    if isinstance(ty, NoneType):
        return "NoneType"
    if isinstance(ty, DynType):
        return "DynType"
    return ty.__class__.__name__


def _low_ir_enabled() -> bool:
    raw = os.environ.get("PCC_PYTHON_LOW_IR", "on").lower()
    return raw not in ("0", "off", "false", "no")


def _low_ir_type_for_expr(expr: Expr) -> Optional[int]:
    ty = expr.ty
    if isinstance(ty, BoolType):
        return pcc_low_ir.LOW_I1
    if isinstance(ty, IntType):
        # LOW_I64 currently carries signed arithmetic/comparison semantics.
        # Keep explicit unsigned values on the full lowering path until the
        # compact IR records signedness as part of its type.
        if ty.name == "pcc.u64":
            return None
        return pcc_low_ir.LOW_I64
    if isinstance(ty, FloatType):
        return _LOW_F64
    return None


def _low_ir_nonzero_literal(expr: Expr) -> bool:
    """True if ``expr`` is an int/float literal that is provably non-zero.

    The low_ir scaffold lowers ``/`` / ``//`` / ``%`` to a bare
    ``fdiv`` / ``sdiv`` / ``srem`` with no zero-divisor trap (it is the
    pure-leaf path and carries no error-exit), so a zero divisor would
    silently yield ``0`` / ``inf`` instead of raising ``ZeroDivisionError``.
    A division whose divisor is a non-zero literal can never trap, so it
    stays on the fast path; every other divisor bails to the full lowering
    (``binary_op_lowering`` / ``expr_helper_lowering``), which guards it.
    """
    if isinstance(expr, IntLit):
        return expr.value != 0
    if isinstance(expr, FloatLit):
        return expr.value != 0.0
    return False


def _low_ir_coerce_value(
    value: pcc_low_ir.LowValue,
    target_ty: int,
):
    if value is None:
        return None
    if value.ty == target_ty:
        return None
    if value.ty == pcc_low_ir.LOW_I1 and target_ty == pcc_low_ir.LOW_I64:
        return pcc_low_ir.LowSelect(
            pcc_low_ir.LOW_I64,
            value,
            pcc_low_ir.LowConst(pcc_low_ir.LOW_I64, 1),
            pcc_low_ir.LowConst(pcc_low_ir.LOW_I64, 0),
        )
    if value.ty == pcc_low_ir.LOW_I64 and target_ty == pcc_low_ir.LOW_I1:
        return pcc_low_ir.LowCompare(
            pcc_low_ir.LOW_I1,
            "!=",
            value,
            pcc_low_ir.LowConst(pcc_low_ir.LOW_I64, 0),
        )
    if target_ty == _LOW_F64 and value.ty == pcc_low_ir.LOW_I64:
        return pcc_low_ir.LowUnary(_LOW_F64, "sitofp", value)
    if target_ty == _LOW_F64 and value.ty == pcc_low_ir.LOW_I1:
        return pcc_low_ir.LowSelect(
            _LOW_F64,
            value,
            pcc_low_ir.LowF64Const(1.0),
            pcc_low_ir.LowF64Const(0.0),
        )
    return None


def _low_value_ty(value: pcc_low_ir.LowValue) -> int:
    return value.ty


def _low_ir_expr_to_value(
    expr: Expr,
    builder: pcc_low_ir.LowBuilder,
    direct_symbols,
):
    if isinstance(expr, IntLit):
        return pcc_low_ir.LowConst(pcc_low_ir.LOW_I64, expr.value)
    if isinstance(expr, FloatLit):
        return pcc_low_ir.LowF64Const(expr.value)
    if isinstance(expr, BoolLit):
        return pcc_low_ir.LowConst(pcc_low_ir.LOW_I1, 1 if expr.value else 0)
    if isinstance(expr, Name):
        low_ty = _low_ir_type_for_expr(expr)
        if low_ty is None:
            return None
        return pcc_low_ir.LowLocal(low_ty, expr.ident)
    if isinstance(expr, UnaryOp):
        operand = _low_ir_expr_to_value(expr.operand, builder, direct_symbols)
        if operand is None:
            return None
        if expr.op == "-":
            target_ty = (
                _LOW_F64 if _low_value_ty(operand) == _LOW_F64 else pcc_low_ir.LOW_I64
            )
            if _low_value_ty(operand) != target_ty:
                operand = _low_ir_coerce_value(operand, target_ty)
                if operand is None:
                    return None
            if target_ty == _LOW_F64:
                return pcc_low_ir.LowUnary(_LOW_F64, "-", operand)
            return pcc_low_ir.LowBinOp(
                target_ty,
                "-",
                pcc_low_ir.LowConst(target_ty, 0),
                operand,
            )
        if expr.op == "+":
            if _low_value_ty(operand) == _LOW_F64:
                return operand
            if _low_value_ty(operand) == pcc_low_ir.LOW_I64:
                return operand
            return _low_ir_coerce_value(operand, pcc_low_ir.LOW_I64)
        if expr.op == "not":
            if _low_value_ty(operand) != pcc_low_ir.LOW_I1:
                operand = _low_ir_coerce_value(operand, pcc_low_ir.LOW_I1)
                if operand is None:
                    return None
            return pcc_low_ir.LowCompare(
                pcc_low_ir.LOW_I1,
                "==",
                operand,
                pcc_low_ir.LowConst(pcc_low_ir.LOW_I1, 0),
            )
        return None
    if isinstance(expr, BinOp):
        lhs = _low_ir_expr_to_value(expr.lhs, builder, direct_symbols)
        rhs = _low_ir_expr_to_value(expr.rhs, builder, direct_symbols)
        if lhs is None or rhs is None:
            return None
        result_ty = _low_ir_type_for_expr(expr)
        if result_ty == _LOW_F64:
            if _low_value_ty(lhs) != _LOW_F64:
                lhs = _low_ir_coerce_value(lhs, _LOW_F64)
                if lhs is None:
                    return None
            if _low_value_ty(rhs) != _LOW_F64:
                rhs = _low_ir_coerce_value(rhs, _LOW_F64)
                if rhs is None:
                    return None
            if expr.op in ("+", "-", "*", "/"):
                if expr.op == "/" and not _low_ir_nonzero_literal(expr.rhs):
                    return None  # ``x / 0.0`` must raise; bail to guarded path
                return pcc_low_ir.LowBinOp(_LOW_F64, expr.op, lhs, rhs)
            return None
        if _low_value_ty(lhs) != pcc_low_ir.LOW_I64:
            lhs = _low_ir_coerce_value(lhs, pcc_low_ir.LOW_I64)
            if lhs is None:
                return None
        if _low_value_ty(rhs) != pcc_low_ir.LOW_I64:
            rhs = _low_ir_coerce_value(rhs, pcc_low_ir.LOW_I64)
            if rhs is None:
                return None
        op = expr.op
        if op in ("//", "%") and not _low_ir_nonzero_literal(expr.rhs):
            return None  # ``a // 0`` / ``a % 0`` must raise; bail to guarded path
        if op in ("+", "-", "*", "%", "//", "&", "|", "^", "<<", ">>"):
            return pcc_low_ir.LowBinOp(pcc_low_ir.LOW_I64, op, lhs, rhs)
        return None
    if isinstance(expr, Compare):
        lhs = _low_ir_expr_to_value(expr.lhs, builder, direct_symbols)
        rhs = _low_ir_expr_to_value(expr.rhs, builder, direct_symbols)
        if lhs is None or rhs is None:
            return None
        if _low_value_ty(lhs) != pcc_low_ir.LOW_I64:
            lhs = _low_ir_coerce_value(lhs, pcc_low_ir.LOW_I64)
            if lhs is None:
                return None
        if _low_value_ty(rhs) != pcc_low_ir.LOW_I64:
            rhs = _low_ir_coerce_value(rhs, pcc_low_ir.LOW_I64)
            if rhs is None:
                return None
        return pcc_low_ir.LowCompare(pcc_low_ir.LOW_I1, expr.op, lhs, rhs)
    if isinstance(expr, IfExpr):
        cond = _low_ir_expr_to_value(expr.cond, builder, direct_symbols)
        then_value = _low_ir_expr_to_value(expr.then_e, builder, direct_symbols)
        else_value = _low_ir_expr_to_value(expr.else_e, builder, direct_symbols)
        if cond is None or then_value is None or else_value is None:
            return None
        target_ty = _low_ir_type_for_expr(expr)
        if target_ty is None:
            return None
        if _low_value_ty(cond) != pcc_low_ir.LOW_I1:
            cond = _low_ir_coerce_value(cond, pcc_low_ir.LOW_I1)
            if cond is None:
                return None
        if _low_value_ty(then_value) != target_ty:
            then_value = _low_ir_coerce_value(then_value, target_ty)
            if then_value is None:
                return None
        if _low_value_ty(else_value) != target_ty:
            else_value = _low_ir_coerce_value(else_value, target_ty)
            if else_value is None:
                return None
        return pcc_low_ir.LowSelect(target_ty, cond, then_value, else_value)
    if isinstance(expr, Call) and isinstance(expr.func, Name):
        symbol = direct_symbols.get(expr.func.ident)
        if symbol is None:
            return None
        args = []
        for arg in expr.args:
            value = _low_ir_expr_to_value(arg, builder, direct_symbols)
            if value is None:
                return None
            if _low_value_ty(value) != pcc_low_ir.LOW_I64:
                value = _low_ir_coerce_value(value, pcc_low_ir.LOW_I64)
                if value is None:
                    return None
            args.append(value)
        return pcc_low_ir.LowCallDirect(
            pcc_low_ir.LOW_I64,
            symbol,
            tuple(args),
            may_raise=False,
            span=expr.span,
        )
    return None


def _low_builder_add_local(
    builder: pcc_low_ir.LowBuilder,
    name: str,
    ty: int,
) -> None:
    if name in builder._local_names:
        return
    builder._local_names.add(name)
    builder.locals.append((name, ty))


def _low_builder_new_block(
    builder: pcc_low_ir.LowBuilder,
    prefix: str,
) -> pcc_low_ir.LowBlock:
    if not builder.blocks:
        name = prefix
    else:
        builder._block_counter += 1
        name = prefix + "." + str(builder._block_counter)
    block = pcc_low_ir.LowBlock(name=name)
    builder.blocks.append(block)
    return block


def _low_builder_position_at_end(
    builder: pcc_low_ir.LowBuilder,
    block: pcc_low_ir.LowBlock,
) -> None:
    builder.current = block


def _low_builder_terminated(builder: pcc_low_ir.LowBuilder) -> bool:
    return builder.current.terminator is not None


def _low_builder_store(
    builder: pcc_low_ir.LowBuilder,
    name: str,
    value: pcc_low_ir.LowValue,
) -> None:
    _low_builder_add_local(builder, name, value.ty)
    if builder.current.terminator is None:
        builder.current.instrs.append(pcc_low_ir.LowStoreLocal(name=name, value=value))


def _low_builder_eval(
    builder: pcc_low_ir.LowBuilder,
    value: pcc_low_ir.LowValue,
) -> None:
    if builder.current.terminator is None:
        builder.current.instrs.append(pcc_low_ir.LowEval(value=value))


def _low_builder_thread_safepoint(builder: pcc_low_ir.LowBuilder) -> None:
    _low_builder_eval(
        builder,
        pcc_low_ir.LowCallRuntime(
            pcc_low_ir.LOW_VOID,
            "pcc_thread_safepoint",
            (),
            may_raise=False,
        ),
    )


def _emit_thread_safepoint_poll_llvm(builder, llvm_module, fn, runtime) -> None:
    block = getattr(builder, "_block", None)
    if block is not None and getattr(block, "terminator", None) is not None:
        return
    flag_gv = declare_runtime_global(llvm_module, "pcc_thread_stop_requested")
    flag = builder.load_atomic(
        flag_gv,
        "acquire",
        4,
        name="low.safepoint.flag",
    )
    need_slow = builder.icmp_unsigned(
        "!=",
        flag,
        ir.Constant(_I32, 0),
        name="low.safepoint.need",
    )
    slow_block = fn.append_basic_block(name="low.safepoint.slow")
    cont_block = fn.append_basic_block(name="low.safepoint.cont")
    builder.cbranch(need_slow, slow_block, cont_block)
    builder.position_at_end(slow_block)
    builder.call(runtime["pcc_thread_safepoint"], [])
    block = getattr(builder, "_block", None)
    if block is None or getattr(block, "terminator", None) is None:
        builder.branch(cont_block)
    builder.position_at_end(cont_block)


def _low_builder_branch(
    builder: pcc_low_ir.LowBuilder,
    target: pcc_low_ir.LowBlock,
) -> None:
    if builder.current.terminator is None:
        builder.current.terminator = pcc_low_ir.LowBranch(target=target.name)


def _low_builder_cbranch(
    builder: pcc_low_ir.LowBuilder,
    cond: pcc_low_ir.LowValue,
    true_target: pcc_low_ir.LowBlock,
    false_target: pcc_low_ir.LowBlock,
) -> None:
    if builder.current.terminator is None:
        builder.current.terminator = pcc_low_ir.LowCondBranch(
            cond=cond,
            true_target=true_target.name,
            false_target=false_target.name,
        )


def _low_builder_ret(
    builder: pcc_low_ir.LowBuilder,
    value,
) -> None:
    if builder.current.terminator is None:
        builder.current.terminator = pcc_low_ir.LowReturn(value=value)


def _low_builder_finish(builder: pcc_low_ir.LowBuilder) -> pcc_low_ir.LowFunction:
    return pcc_low_ir.LowFunction(
        name=builder.name,
        symbol=builder.symbol,
        params=builder.params,
        return_ty=builder.return_ty,
        blocks=tuple(builder.blocks),
        locals=tuple(builder.locals),
    )


def _low_ir_lower_stmt_block(
    stmts,
    builder: pcc_low_ir.LowBuilder,
    return_ty: int,
    direct_symbols,
) -> bool:
    for stmt in stmts:
        if isinstance(stmt, Assign):
            value = _low_ir_expr_to_value(stmt.value, builder, direct_symbols)
            if value is None:
                return False
            for target in stmt.targets:
                if not isinstance(target, Name):
                    return False
                target_ty = _low_ir_type_for_expr(target)
                if target_ty is None:
                    return False
                if _low_value_ty(value) == target_ty:
                    _low_builder_store(builder, target.ident, value)
                else:
                    coerced = _low_ir_coerce_value(value, target_ty)
                    if coerced is None:
                        return False
                    _low_builder_store(builder, target.ident, coerced)
            continue
        if isinstance(stmt, ExprStmt):
            value = _low_ir_expr_to_value(stmt.expr, builder, direct_symbols)
            if value is None:
                return False
            _low_builder_eval(builder, value)
            continue
        if isinstance(stmt, Return):
            if stmt.value is None:
                _low_builder_ret(builder, None)
                continue
            value = _low_ir_expr_to_value(stmt.value, builder, direct_symbols)
            if value is None:
                return False
            target_ty = return_ty
            if _low_value_ty(value) == target_ty:
                _low_builder_ret(builder, value)
            else:
                coerced = _low_ir_coerce_value(value, target_ty)
                if coerced is None:
                    return False
                _low_builder_ret(builder, coerced)
            continue
        if isinstance(stmt, While):
            cond_bb = _low_builder_new_block(builder, "while.cond")
            body_bb = _low_builder_new_block(builder, "while.body")
            end_bb = _low_builder_new_block(builder, "while.end")
            _low_builder_branch(builder, cond_bb)
            _low_builder_position_at_end(builder, cond_bb)
            cond = _low_ir_expr_to_value(stmt.cond, builder, direct_symbols)
            if cond is None:
                return False
            if _low_value_ty(cond) != pcc_low_ir.LOW_I1:
                cond = _low_ir_coerce_value(cond, pcc_low_ir.LOW_I1)
                if cond is None:
                    return False
            _low_builder_cbranch(builder, cond, body_bb, end_bb)
            _low_builder_position_at_end(builder, body_bb)
            if not _low_ir_lower_stmt_block(
                stmt.body,
                builder,
                return_ty,
                direct_symbols,
            ):
                return False
            _low_builder_thread_safepoint(builder)
            _low_builder_branch(builder, cond_bb)
            _low_builder_position_at_end(builder, end_bb)
            if stmt.else_body:
                if not _low_ir_lower_stmt_block(
                    stmt.else_body,
                    builder,
                    return_ty,
                    direct_symbols,
                ):
                    return False
            continue
        if isinstance(stmt, If):
            cond = _low_ir_expr_to_value(stmt.cond, builder, direct_symbols)
            if cond is None:
                return False
            if _low_value_ty(cond) != pcc_low_ir.LOW_I1:
                cond = _low_ir_coerce_value(cond, pcc_low_ir.LOW_I1)
                if cond is None:
                    return False
            then_bb = _low_builder_new_block(builder, "if.then")
            else_bb = _low_builder_new_block(builder, "if.else")
            end_bb = _low_builder_new_block(builder, "if.end")
            _low_builder_cbranch(builder, cond, then_bb, else_bb)
            _low_builder_position_at_end(builder, then_bb)
            if not _low_ir_lower_stmt_block(
                stmt.body,
                builder,
                return_ty,
                direct_symbols,
            ):
                return False
            _low_builder_branch(builder, end_bb)
            _low_builder_position_at_end(builder, else_bb)
            if not _low_ir_lower_stmt_block(
                stmt.else_body,
                builder,
                return_ty,
                direct_symbols,
            ):
                return False
            _low_builder_branch(builder, end_bb)
            _low_builder_position_at_end(builder, end_bb)
            continue
        return False
    return True


def _low_ir_lower_typed_int_function(
    fd: FuncDef,
    symbol: str,
    direct_symbols,
) -> Optional[pcc_low_ir.LowFunction]:
    params = []
    for arg in fd.args:
        ann = arg.annotation
        if not isinstance(ann, (IntType, BoolType, FloatType)):
            return None
        if isinstance(ann, BoolType):
            low_ty = pcc_low_ir.LOW_I1
        elif isinstance(ann, FloatType):
            low_ty = _LOW_F64
        else:
            low_ty = pcc_low_ir.LOW_I64
        params.append((arg.name, low_ty))
    if not isinstance(fd.return_ty, (IntType, BoolType, FloatType)):
        return None
    builder: pcc_low_ir.LowBuilder = pcc_low_ir.LowBuilder(
        fd.name,
        symbol,
        tuple(params),
    )
    returns_bool = isinstance(fd.return_ty, BoolType)
    returns_float = isinstance(fd.return_ty, FloatType)
    if returns_bool:
        return_ty = pcc_low_ir.LOW_I1
    elif returns_float:
        return_ty = _LOW_F64
    else:
        return_ty = pcc_low_ir.LOW_I64
    builder.return_ty = return_ty
    if not _low_ir_lower_stmt_block(fd.body, builder, return_ty, direct_symbols):
        return None
    if not _low_builder_terminated(builder):
        if returns_bool:
            _low_builder_ret(builder, pcc_low_ir.LowConst(pcc_low_ir.LOW_I1, 0))
        elif returns_float:
            _low_builder_ret(builder, pcc_low_ir.LowF64Const(0.0))
        else:
            _low_builder_ret(builder, pcc_low_ir.LowConst(pcc_low_ir.LOW_I64, 0))
    return _low_builder_finish(builder)


def _low_ir_llvm_type(low_ty: int):
    if low_ty == pcc_low_ir.LOW_I1:
        return _I1
    if low_ty == pcc_low_ir.LOW_I64:
        return _I64
    if low_ty == _LOW_F64:
        return _DOUBLE
    if low_ty == pcc_low_ir.LOW_VOID:
        return _VOID
    return _CSTR


def _low_ir_coerce_llvm_value(builder, value, from_ty: int, to_ty: int):
    if from_ty == to_ty:
        return value
    if from_ty == pcc_low_ir.LOW_I1 and to_ty == pcc_low_ir.LOW_I64:
        return builder.select(value, ir.Constant(_I64, 1), ir.Constant(_I64, 0))
    if from_ty == pcc_low_ir.LOW_I64 and to_ty == pcc_low_ir.LOW_I1:
        return builder.icmp_signed("!=", value, ir.Constant(_I64, 0))
    if from_ty == pcc_low_ir.LOW_I64 and to_ty == _LOW_F64:
        return builder.sitofp(value, _DOUBLE)
    if from_ty == pcc_low_ir.LOW_I1 and to_ty == _LOW_F64:
        return builder.select(
            value,
            ir.Constant(_DOUBLE, 1.0),
            ir.Constant(_DOUBLE, 0.0),
        )
    return value


def _low_ir_emit_value(
    builder, value, slots, runtime, functions, post_call_error_check
):
    if value is None:
        return None
    if isinstance(value, pcc_low_ir.LowConst):
        return ir.Constant(_low_ir_llvm_type(value.ty), value.value)
    if isinstance(value, pcc_low_ir.LowF64Const):
        return ir.Constant(_DOUBLE, value.value)
    if isinstance(value, pcc_low_ir.LowLocal):
        return builder.load(slots[value.name], name="low." + value.name)
    if isinstance(value, pcc_low_ir.LowUnary):
        inner = _low_ir_emit_value(
            builder,
            value.value,
            slots,
            runtime,
            functions,
            post_call_error_check,
        )
        if value.op == "sitofp":
            return builder.sitofp(inner, _DOUBLE, name="low.sitofp")
        if value.op == "-":
            if isinstance(inner.type, ir.DoubleType):
                return builder.fneg(inner, name="low.fneg")
            return builder.sub(ir.Constant(inner.type, 0), inner, name="low.neg")
        return inner
    if isinstance(value, pcc_low_ir.LowBinOp):
        lhs = _low_ir_emit_value(
            builder,
            value.lhs,
            slots,
            runtime,
            functions,
            post_call_error_check,
        )
        rhs = _low_ir_emit_value(
            builder,
            value.rhs,
            slots,
            runtime,
            functions,
            post_call_error_check,
        )
        if value.ty == _LOW_F64:
            if value.op == "+":
                return builder.fadd(lhs, rhs, name="low.fadd")
            if value.op == "-":
                return builder.fsub(lhs, rhs, name="low.fsub")
            if value.op == "*":
                return builder.fmul(lhs, rhs, name="low.fmul")
            if value.op == "/":
                return builder.fdiv(lhs, rhs, name="low.fdiv")
        if value.op == "+":
            return builder.add(lhs, rhs, name="low.add")
        if value.op == "-":
            return builder.sub(lhs, rhs, name="low.sub")
        if value.op == "*":
            return builder.mul(lhs, rhs, name="low.mul")
        if value.op == "%":
            # Python ``%`` follows the divisor's sign, unlike C ``%``
            # which follows the dividend's.  Adjust: ``r = a srem b;
            # if r != 0 and (r ^ b) < 0 then r += b``.  See
            # docs/issues/python-data-model-gaps.md and the
            # arith_mod_neg corpus case.
            zero = ir.Constant(lhs.type, 0)
            r = builder.srem(lhs, rhs, name="low.mod.r")
            r_nz = builder.icmp_signed("!=", r, zero, name="low.mod.r_nz")
            sign_xor = builder.xor(r, rhs, name="low.mod.sign_xor")
            sign_diff = builder.icmp_signed(
                "<", sign_xor, zero, name="low.mod.sign_diff"
            )
            need_fix = builder.and_(r_nz, sign_diff, name="low.mod.need_fix")
            r_plus_b = builder.add(r, rhs, name="low.mod.r_fix")
            return builder.select(need_fix, r_plus_b, r, name="low.mod")
        if value.op == "//":
            return emit_python_floordiv_i64_unchecked(
                builder,
                lhs,
                rhs,
                "low.div",
            )
        if value.op == "&":
            return builder.and_(lhs, rhs, name="low.and")
        if value.op == "|":
            return builder.or_(lhs, rhs, name="low.or")
        if value.op == "^":
            return builder.xor(lhs, rhs, name="low.xor")
        if value.op == "<<":
            return builder.shl(lhs, rhs, name="low.shl")
        if value.op == ">>":
            return builder.ashr(lhs, rhs, name="low.shr")
        raise L1CodegenError("unsupported low-ir binop " + value.op)
    if isinstance(value, pcc_low_ir.LowCompare):
        lhs = _low_ir_emit_value(
            builder,
            value.lhs,
            slots,
            runtime,
            functions,
            post_call_error_check,
        )
        rhs = _low_ir_emit_value(
            builder,
            value.rhs,
            slots,
            runtime,
            functions,
            post_call_error_check,
        )
        if isinstance(lhs.type, ir.DoubleType) or isinstance(rhs.type, ir.DoubleType):
            if value.op == "!=":
                return builder.fcmp_unordered(value.op, lhs, rhs, name="low.fcmp")
            return builder.fcmp_ordered(value.op, lhs, rhs, name="low.fcmp")
        return builder.icmp_signed(value.op, lhs, rhs, name="low.cmp")
    if isinstance(value, pcc_low_ir.LowSelect):
        cond = _low_ir_emit_value(
            builder,
            value.cond,
            slots,
            runtime,
            functions,
            post_call_error_check,
        )
        then_value = _low_ir_emit_value(
            builder,
            value.then_value,
            slots,
            runtime,
            functions,
            post_call_error_check,
        )
        else_value = _low_ir_emit_value(
            builder,
            value.else_value,
            slots,
            runtime,
            functions,
            post_call_error_check,
        )
        return builder.select(cond, then_value, else_value, name="low.select")
    if isinstance(value, pcc_low_ir.LowCallDirect):
        args = []
        for arg in value.args:
            args.append(
                _low_ir_emit_value(
                    builder,
                    arg,
                    slots,
                    runtime,
                    functions,
                    post_call_error_check,
                )
            )
        call = builder.call(functions[value.symbol], args, name="low.call")
        if value.may_raise:
            post_call_error_check(value.span)
        return call
    if isinstance(value, pcc_low_ir.LowCallRuntime):
        if value.name == "pcc_thread_safepoint" and len(value.args) == 0:
            builder.call(runtime["pcc_thread_safepoint"], [])
            return ir.Constant(_I64, 0)
        args = []
        for arg in value.args:
            args.append(
                _low_ir_emit_value(
                    builder,
                    arg,
                    slots,
                    runtime,
                    functions,
                    post_call_error_check,
                )
            )
        call = builder.call(runtime[value.name], args, name="low.runtime")
        if value.may_raise:
            post_call_error_check(value.span)
        return call
    raise L1CodegenError("unsupported low-ir value " + value.__class__.__name__)


def _low_ir_emit_function_to_llvm(
    low_fn,
    *,
    llvm_module,
    fn,
    runtime,
    functions,
    post_call_error_check,
    emit_thread_safepoints=True,
) -> None:
    llvm_blocks = {}
    for low_block in low_fn.blocks:
        llvm_blocks[low_block.name] = fn.append_basic_block(
            name="low." + low_block.name
        )

    builder = ir.IRBuilder(llvm_blocks[low_fn.blocks[0].name])
    slots = {}
    for name, low_ty in low_fn.locals:
        slot = builder.alloca(_low_ir_llvm_type(low_ty), name=name + ".addr")
        slots[name] = slot
    for idx, pair in enumerate(low_fn.params):
        name, _low_ty = pair
        if name in slots:
            builder.store(fn.args[idx], slots[name])

    for low_block in low_fn.blocks:
        block = llvm_blocks[low_block.name]
        builder.position_at_end(block)
        if emit_thread_safepoints and low_block is low_fn.blocks[0]:
            _emit_thread_safepoint_poll_llvm(builder, llvm_module, fn, runtime)
        for instr in low_block.instrs:
            if isinstance(instr, pcc_low_ir.LowStoreLocal):
                value = _low_ir_emit_value(
                    builder,
                    instr.value,
                    slots,
                    runtime,
                    functions,
                    post_call_error_check,
                )
                builder.store(value, slots[instr.name])
            elif isinstance(instr, pcc_low_ir.LowEval):
                _low_ir_emit_value(
                    builder,
                    instr.value,
                    slots,
                    runtime,
                    functions,
                    post_call_error_check,
                )
        term = low_block.terminator
        if isinstance(term, pcc_low_ir.LowBranch):
            builder.branch(llvm_blocks[term.target])
        elif isinstance(term, pcc_low_ir.LowCondBranch):
            cond = _low_ir_emit_value(
                builder,
                term.cond,
                slots,
                runtime,
                functions,
                post_call_error_check,
            )
            cond = _low_ir_coerce_llvm_value(
                builder,
                cond,
                term.cond.ty,
                pcc_low_ir.LOW_I1,
            )
            builder.cbranch(
                cond,
                llvm_blocks[term.true_target],
                llvm_blocks[term.false_target],
            )
        elif isinstance(term, pcc_low_ir.LowReturn):
            if term.value is None:
                builder.ret_void()
            else:
                value = _low_ir_emit_value(
                    builder,
                    term.value,
                    slots,
                    runtime,
                    functions,
                    post_call_error_check,
                )
                builder.ret(value)
        elif term is None:
            builder.ret(ir.Constant(_low_ir_llvm_type(low_fn.return_ty), 0))
        else:
            raise L1CodegenError("unsupported low-ir terminator")


def _replace_arg_with_none_default(arg):
    return Arg(
        name=arg.name,
        annotation=arg.annotation,
        default=NoneLit(
            span=None,
            ty=NoneType(name="None"),
        ),
        kind=arg.kind,
        has_default=True,
    )


class UserFunctionLoweringMixin:
    def _try_emit_low_ir_user_function(
        self,
        fd: FuncDef,
        fn: ir.Function,
        *,
        c_abi_sym: str | None,
        box_int_abi: bool,
    ) -> bool:
        if not _low_ir_enabled():
            return False
        # Low-IR direct-symbol resolution has no live module-binding model.
        # Keep a module containing a repeated ``def`` on the normal lowering
        # path, where calls load the current rooted module-global callable.
        if self._duplicate_module_function_names:
            return False
        if c_abi_sym is not None:
            return False
        if box_int_abi:
            return False
        if not self._funcdef_uses_unboxed_typed_int_abi(fd):
            return False

        direct_symbols: dict[str, str] = {}
        for stmt in self.ast_module.body:
            if not isinstance(stmt, FuncDef):
                continue
            if not self._funcdef_uses_unboxed_typed_int_abi(stmt):
                continue
            target_fn = self.functions.get(stmt.name)
            if target_fn is None:
                continue
            direct_symbols[stmt.name] = target_fn.name

        low_fn = _low_ir_lower_typed_int_function(
            fd,
            fn.name,
            direct_symbols,
        )
        if low_fn is None:
            return False

        old_builder = self.builder
        old_current_function = self.current_function
        old_current_func_def = self.current_func_def
        old_global_names = self._current_global_names
        try:
            self.builder = None
            self.current_function = fn
            self.current_func_def = fd
            self._current_global_names = self._collect_explicit_global_names(fd.body)
            functions_by_symbol: dict[str, ir.Function] = {}
            for _display_name, target_fn in self.functions.items():
                functions_by_symbol[target_fn.name] = target_fn
            _low_ir_emit_function_to_llvm(
                low_fn,
                llvm_module=self.module,
                fn=fn,
                runtime=self.runtime,
                functions=functions_by_symbol,
                post_call_error_check=None,
                emit_thread_safepoints=getattr(
                    self,
                    "_thread_safepoints_enabled",
                    False,
                ),
            )
        finally:
            self.builder = old_builder
            self.current_function = old_current_function
            self.current_func_def = old_current_func_def
            self._current_global_names = old_global_names

        if os.environ.get("PCC_DEBUG_LOW_IR", "").strip():
            sys.stderr.write(
                "[pcc.low_ir] emitted "
                + str(fd.name)
                + " blocks="
                + str(len(low_fn.blocks))
                + "\n"
            )
        return True

    def _strict_stub_user_function_with_cpy_fallback(
        self,
        fn: ir.Function,
        fd: FuncDef,
    ) -> bool:
        """Replace a deferred libpython-only body with an honest native stub.

        Strict no-libpython compilation must not reject a whole import merely
        because an otherwise-unreached public function still needs CPython.
        Keep the exported function and ABI, but make an attempted call raise a
        stable native NotImplementedError instead of linking libpython or
        returning a fabricated result. Module top-level code is emitted by a
        separate path and is deliberately outside this projection.
        """
        if not getattr(self, "_strict_no_libpython", False):
            return False

        direct_callee = fn._direct_first_libpython_callee
        first_cpy_call = (
            None if not direct_callee else "call @" + direct_callee
        )
        if first_cpy_call is None:
            for block in fn.blocks:
                for instr in block.instructions:
                    text = str(instr)
                    if "call " in text and "@py_cpy_" in text:
                        first_cpy_call = text
                        break
                if first_cpy_call is not None:
                    break
        if first_cpy_call is None:
            return False

        module_name = self.ast_module.name or "<module>"
        debug_filter = os.environ.get("PCC_DEBUG_STRICT_NOLIB_STUB", "").strip()
        qualified_name = module_name + "." + fd.name
        if debug_filter and (debug_filter == "1" or debug_filter in qualified_name):
            sys.stderr.write(
                "[pcc.strict-nolib-stub] "
                + qualified_name
                + ": "
                + first_cpy_call
                + "\n"
            )

        fn.blocks.clear()
        # The strict stub replaces the complete body.  The direct builder is
        # the structured mirror of that body, so retaining it here leaves
        # calls from the discarded CPython-backed implementation reachable in
        # the direct call plane even though canonical LLVM text contains only
        # the native fail-closed stub.
        fn._direct_indexed_builder = None
        fn._direct_indexed_function_cache = None
        fn._direct_first_libpython_callee = ""
        entry = fn.append_basic_block(name="strict.nolib.stub")
        self.builder = ir.IRBuilder(entry)
        self._current_entry_block = entry
        msg = self._pooled_cstr_ptr(
            "no-libpython function unavailable: " + module_name + "." + fd.name,
            ".strict.nolib.msg",
        )
        exc = self.builder.call(
            self.runtime["py_exc_new"],
            [ir.Constant(_I64, 11), msg],
            name=self._fresh("strict.nolib.exc"),
        )
        self.builder.call(self.runtime["py_raise"], [exc])
        self._gc_release(exc)
        return_ty = fn.function_type.return_type
        if isinstance(return_ty, ir.VoidType):
            self.builder.ret_void()
        elif isinstance(return_ty, ir.PointerType):
            self.builder.ret(ir.Constant(return_ty, None))
        else:
            self.builder.ret(self._zero_of(return_ty))
        return True

    def _prologue_mark(self, tag: str) -> None:
        """Sub-function-level marker: the statement marker stayed empty while
        the function marker was set, so the failure is inside the prologue --
        before the first statement is lowered.  Narrow that window."""
        path = ""
        try:
            path = str(os.environ.get("PCC_STMT_PROGRESS_FILE", ""))
        except Exception:
            path = ""
        if not path:
            return
        try:
            s = open(path + ".pro." + str(os.getpid()), "w")
            s.write(str(tag) + "\n")
            s.close()
        except Exception:
            pass

    def _emit_user_function(self, fd: FuncDef) -> None:
        # Function-level progress marker, same technique that localised the
        # failing MODULE: under pcc1 a caught exception carries no traceback
        # (verified) so the site cannot be recovered afterwards -- it has to be
        # written down before the work.  Last value written is where it died.
        _marker = ""
        try:
            _marker = str(os.environ.get("PCC_COMPILE_PROGRESS_FILE", ""))
        except Exception:
            _marker = ""
        if _marker:
            try:
                _s = open(_marker + ".fn." + str(os.getpid()), "w", encoding="utf-8")
                _s.write(str(getattr(self, "_codegen_current_module_name", "")) + " :: " + str(fd.name) + "\n")
                _s.close()
            except Exception:
                pass
        debug_codegen = bool(os.environ.get("PCC_DEBUG_CODEGEN_PHASES"))

        if self._codegen_trace_is_enabled():
            saved_trace_stmt_index = self._codegen_current_stmt_index
            saved_trace_stmt_kind = self._codegen_current_stmt_kind
            saved_trace_expr_kind = self._codegen_current_expr_kind
        else:
            saved_trace_stmt_index = -1
            saved_trace_stmt_kind = ""
            saved_trace_expr_kind = ""

        _func_codegen_log(self, debug_codegen, fd.name, "enter")
        fn = self._funcdef_functions.get(id(fd))
        if fn is None:
            fn = self.functions[fd.name]
        _func_codegen_log(self, debug_codegen, fd.name, "generator check begin")
        # Preserve state needed by nested codegen and diagnostics.
        saved_builder = self.builder
        saved_fn = self.current_function
        saved_fd = self.current_func_def
        saved_entry_block = getattr(self, "_current_entry_block", None)
        saved_try_err_block = getattr(self, "_try_err_block", None)
        saved_cpy_operand_cleanup_block = getattr(
            self,
            "_cpy_operand_cleanup_block",
            None,
        )
        saved_env = self.env
        saved_env_class_hint = self.env_class_hint
        saved_env_class_object_hint = self.env_class_object_hint
        saved_env_list_elem_class_hint = self.env_list_elem_class_hint
        saved_box_int_locals = self._box_int_locals
        saved_exact_int_flags = self._exact_int_env_flags
        saved_planned_exact_int_local_names = self._planned_exact_int_local_names
        saved_ir_builder_flags = self._ir_builder_env_flags
        saved_threading_list_elem_flags = self._threading_list_elem_flags
        saved_weak_dict_flags = self._weak_dict_env_flags
        saved_cpy_env_flags = dict(getattr(self, "_cpy_env_flags", {}))
        saved_cpy_values = set(getattr(self, "_cpy_values", set()))
        saved_owned_cpy_values = set(getattr(self, "_owned_cpy_values", set()))
        saved_async_body_depth = getattr(self, "_async_body_depth", 0)
        saved_owned_local_names = self._owned_local_names
        saved_owned_local_has_value = self._owned_local_has_value
        saved_owned_local_flag_slots = self._owned_local_flag_slots
        saved_owned_local_flag_allocas = getattr(
            self,
            "_owned_local_flag_allocas",
            {},
        )
        saved_for_target_owned_names = getattr(
            self,
            "_for_target_owned_names",
            set(),
        )
        saved_gc_rooted_local_names = self._gc_rooted_local_names
        saved_gc_rooted_local_order = getattr(self, "_gc_rooted_local_order", [])
        saved_borrowed_gc_rooted_local_names = getattr(
            self,
            "_borrowed_gc_rooted_local_names",
            set(),
        )
        saved_pinned_gc_rooted_local_names = self._pinned_gc_rooted_local_names
        saved_except_binding_names = getattr(self, "_except_binding_names", set())
        saved_active_handler_excs = self._active_handler_excs
        saved_container_temp_root_slot_names = getattr(
            self,
            "_container_temp_root_slot_names",
            [],
        )
        saved_current_param_names = self._current_param_names
        saved_lambda_lexical_shadow_names = getattr(
            self,
            "_lambda_lexical_shadow_names",
            set(),
        )
        saved_global_names = self._current_global_names
        saved_loop_stack = self.loop_stack
        saved_class = getattr(self, "current_class", None)
        saved_kind = getattr(self, "current_method_kind", None)
        # Locations inside this body must name this subprogram as their scope;
        # LLVM discards the compile unit if one points at an enclosing one.
        saved_di_scope = self._di_declare_function(
            fn, fd.name, getattr(getattr(fd, "span", None), "line", 0)
        )

        if self._codegen_trace_is_enabled():
            self._codegen_current_stmt_index = -1
            self._codegen_current_stmt_kind = ""
            self._codegen_current_expr_kind = ""
            self._codegen_trace_set_stmt_context(-1, "")
            self._codegen_trace_push(
                "function",
                -1,
                "",
                "",
                self._codegen_trace_span(fd),
            )

        self.current_function = fn
        self.current_func_def = fd
        self._try_err_block = None
        self._cpy_operand_cleanup_block = None
        # An exception handler cannot span a Python function boundary.  Keep
        # this codegen-time stack function-local as well: a retained IR value
        # from an outer/nested emission is invalid in ``fn`` and must never be
        # used as the implicit context of a raise generated here.
        self._active_handler_excs = []
        enclosing_class_name = getattr(self, "_hoisted_enclosing_class", {}).get(
            fd.name
        )
        if enclosing_class_name is not None:
            self.current_class = self.class_lowering.classes.get(enclosing_class_name)
            self.current_method_kind = getattr(
                self,
                "_hoisted_enclosing_method_kind",
                {},
            ).get(fd.name, "instance")

        try:
            is_generator = self._funcdef_has_yield_sentinel(fd)
            is_factory = self._funcdef_is_continuation_factory(fd)
            if is_factory:
                if self.current_class is None:
                    raise L1CodegenError("continuation_factory currently requires a method")
                is_generator = False
            if (
                not is_generator
                and not is_factory
                and fd.name not in self._duplicate_module_function_names
            ):
                is_generator = fd.name in getattr(
                    self,
                    "_generator_func_names",
                    set(),
                )
            if is_generator:
                _func_codegen_log(
                    self, debug_codegen, fd.name, "generator wrapper begin"
                )
                if not hasattr(self, "_generator_func_names"):
                    self._generator_func_names = set()
                self._generator_func_names.add(fd.name)
                # Mirror the normal-function reset (~line 1053): the generator
                # path returns early and skips that reset, so each new
                # generator inherits the owned-flag / gc-root caches from the
                # previously-emitted user function (mutated in place — the
                # finally restore at the bottom reassigns the same dict
                # reference). When two sibling generators share a local name
                # (e.g. numpy.distutils.misc_util's general_source_files and
                # general_source_directories_files both carry
                # ``pruned_directories``), the second generator's resume
                # function references the first's alloca, producing an
                # undefined-pointer-value IR that the self backend rejects
                # ("self backend expected pointer value
                # 'pruned_directories.owned.<N>'") and capping the numpy
                # auto-mode diagnostic at 149 IR modules.
                self._owned_local_flag_slots = {}
                self._owned_local_flag_allocas = {}
                self._for_target_owned_names = set()
                self._gc_rooted_local_names = set()
                self._gc_rooted_local_order = []
                self._borrowed_gc_rooted_local_names = set()
                self._pinned_gc_rooted_local_names = set()
                self._except_binding_names = set()
                self._cpy_env_flags = {}
                self._cpy_values = set()
                self._owned_cpy_values = set()
                self._emit_generator_wrapper_function(fd, fn)
                _func_codegen_log(self, debug_codegen, fd.name, "generator wrapper end")
                return

            _func_codegen_log(self, debug_codegen, fd.name, "generator check end")
            c_abi_sym = self._func_c_abi_export_symbol(fd)
            auto_root_borrowed_params = c_abi_sym is None and not getattr(
                self, "_suppress_implicit_gc_roots", False
            )
            box_int_abi = self._funcdef_uses_boxed_int_abi(
                fd,
                c_abi_sym=c_abi_sym,
            )
            if self._try_emit_low_ir_user_function(
                fd,
                fn,
                c_abi_sym=c_abi_sym,
                box_int_abi=box_int_abi,
            ):
                _func_codegen_log(self, debug_codegen, fd.name, "low_ir")
                return

            self._current_global_names = self._collect_explicit_global_names(fd.body)
            forced_exact_int_names = forced_exact_int_local_names(
                self,
                fd,
                self._current_global_names,
            )
            forced_exact_int_name_set = set(forced_exact_int_names)

            # Pick an entry-block name that can't collide with a parameter
            # or local variable of the same name. LLVM keeps labels in the
            # same namespace as SSA value names, so a function with a
            # parameter literally named ``entry`` would otherwise trigger
            # ``unable to create block named 'entry'`` at parse time.
            param_names = {a.name for a in fd.args}
            entry_label = "entry"
            if "entry" in param_names:
                entry_label = "fn.entry"
                while entry_label in param_names:
                    entry_label += "_"
            entry = fn.append_basic_block(name=entry_label)
            self.builder = ir.IRBuilder(entry)
            self._current_entry_block = entry
            self.env = {}
            self.env_class_hint = {}
            self.env_class_object_hint = {}
            self.env_list_elem_class_hint = {}
            self._threading_list_elem_flags = dict(saved_threading_list_elem_flags)
            self._weak_dict_env_flags = dict(saved_weak_dict_flags)
            self._cpy_env_flags = {}
            self._cpy_values = set()
            self._owned_cpy_values = set()
            self._ir_builder_env_flags = {}
            self._box_int_locals = box_int_abi
            self._exact_int_env_flags = {
                name: True for name in forced_exact_int_names
            }
            self._planned_exact_int_local_names = forced_exact_int_name_set
            self._async_body_depth = saved_async_body_depth + (1 if fd.is_async else 0)
            self.loop_stack = []
            self._owned_local_names = set()
            self._owned_local_has_value = set()
            self._owned_local_flag_slots = {}
            self._owned_local_flag_allocas = {}
            self._for_target_owned_names = set()
            self._gc_rooted_local_names = set()
            self._gc_rooted_local_order = []
            self._borrowed_gc_rooted_local_names = set()
            self._pinned_gc_rooted_local_names = set()
            self._except_binding_names = set()
            self._container_temp_root_slot_names = []

            # Promote each incoming argument to an entry-block alloca so
            # assignments within the function body are uniform. Skip the
            # bare ``*`` separator — it has no IR slot.
            _func_codegen_log(self, debug_codegen, fd.name, "params begin")
            runtime_args = []
            for a in fd.args:
                if a.name != "":
                    runtime_args.append(a)
            current_param_names = set()
            for a in runtime_args:
                current_param_names.add(a.name)
            self._current_param_names = current_param_names
            self._lambda_lexical_shadow_names = set(current_param_names)
            boxed_param_names = set(
                getattr(self, "_closure_boxed_params", {}).get(fd.name, ())
            )
            fn_arg_types = fn.function_type.args
            param_index = 0
            runtime_arg_count = len(runtime_args)
            fn_arg_count = len(fn_arg_types)
            if debug_codegen:
                _func_codegen_log(
                    self,
                    debug_codegen,
                    fd.name,
                    "params counts runtime="
                    + str(runtime_arg_count)
                    + " fn="
                    + str(fn_arg_count)
                    + " fn_args_obj="
                    + str(type(fn.args).__name__),
                )
            while param_index < runtime_arg_count and param_index < fn_arg_count:
                ir_arg = fn.args[param_index]
                ast_arg = runtime_args[param_index]
                ir_ty = fn_arg_types[param_index]
                param_index += 1
                if debug_codegen:
                    _func_codegen_log(
                        self,
                        debug_codegen,
                        fd.name,
                        "param bind "
                        + str(param_index)
                        + " name="
                        + str(ast_arg.name)
                        + " ir_ty="
                        + str(ir_ty),
                    )
                _decl_ir_ty, bind_ty = self._param_ir_and_bind_type(
                    ast_arg,
                    require_annotation=True,
                    owner_name=fd.name,
                    box_int_params=box_int_abi,
                )
                if ast_arg.name in boxed_param_names:
                    cell = self.builder.call(
                        self.runtime["py_list_new"],
                        [ir.Constant(_I64, 0)],
                        name=self._fresh(f"{ast_arg.name}.cell"),
                    )
                    initial = self._emit_value_as_pcc_object_or_bridge(
                        ir_arg,
                        bind_ty or DynType(name="dyn"),
                        f"{ast_arg.name}.cell.init",
                    )
                    self.builder.call(
                        self.runtime["py_list_append"],
                        [cell, initial],
                    )
                    slot = self._alloca_in_entry(_CSTR, name=f"{ast_arg.name}.addr")
                    self.builder.store(cell, slot)
                    self.env[ast_arg.name] = (
                        slot,
                        _CSTR,
                        ListType(name="list", elem=DynType(name="dyn")),
                    )
                    if auto_root_borrowed_params:
                        self._ensure_borrowed_local_gc_root(ast_arg.name, slot, _CSTR)
                    continue
                if bind_forced_exact_int_parameter(
                    self,
                    ast_arg,
                    ir_arg,
                    ir_ty,
                    bind_ty,
                ):
                    continue
                slot = self._alloca_in_entry(ir_ty, name=f"{ast_arg.name}.addr")
                self.builder.store(ir_arg, slot)
                if debug_codegen:
                    try:
                        entry_instr_count = len(fn.blocks[0]._instrs)
                    except AttributeError:
                        entry_instr_count = -1
                    _func_codegen_log(
                        self,
                        debug_codegen,
                        fd.name,
                        "param stored "
                        + str(ast_arg.name)
                        + " entry_instrs="
                        + str(entry_instr_count),
                    )
                self.env[ast_arg.name] = (slot, ir_ty, bind_ty)
                if (
                    getattr(ast_arg, "has_default", False)
                    and ast_arg.default is not None
                ):
                    class_object_hint = self._class_object_hint_for_expr(
                        ast_arg.default
                    )
                    if class_object_hint is not None:
                        self.env_class_object_hint[ast_arg.name] = class_object_hint
                if auto_root_borrowed_params and self._is_object(bind_ty):
                    self._ensure_borrowed_local_gc_root(ast_arg.name, slot, ir_ty)
                if auto_root_borrowed_params and self._is_valueclass_payload_type(
                    bind_ty
                ):
                    self._ensure_valueclass_payload_gc_roots(
                        ast_arg.name, slot, bind_ty
                    )
                threading_elem_kind = self._threading_list_elem_kind_for_type(bind_ty)
                if threading_elem_kind is not None:
                    self._threading_list_elem_flags[ast_arg.name] = threading_elem_kind
            _func_codegen_log(self, debug_codegen, fd.name, "params end")

            # Allocate every non-parameter exact-int local before emitting any
            # branch or loop.  The null value means "not assigned yet" only;
            # all valid incoming scalar values are boxed into this same slot,
            # so a fallthrough/zero-iteration edge can never select a synthetic
            # null in place of an earlier integer binding.
            allocate_forced_exact_int_locals(
                self,
                forced_exact_int_names,
                self._current_global_names,
            )
            self._emit_thread_safepoint()

            # Emit body.
            # A leading string expression is function metadata, not an
            # executable statement.  ``_emit_native_func_value`` publishes it
            # as ``__doc__`` when a Python function object is materialized;
            # the native body must never allocate/release that literal on
            # entry (and freestanding functions cannot reference the managed
            # string runtime at all).
            body_stmts = fd.body
            if (
                body_stmts
                and isinstance(body_stmts[0], ExprStmt)
                and isinstance(body_stmts[0].expr, StrLit)
            ):
                body_stmts = body_stmts[1:]
            if debug_codegen:
                _func_codegen_log(self, debug_codegen, fd.name, "body len begin")
                _func_codegen_log(
                    self,
                    debug_codegen,
                    fd.name,
                    "body len " + str(len(body_stmts)),
                )
                body_index = 0
                for raw_stmt in body_stmts:
                    span = getattr(raw_stmt, "span", None)
                    loc = ""
                    if span is not None:
                        loc = ":" + str(span.line) + ":" + str(span.col)
                    _func_codegen_log(
                        self,
                        debug_codegen,
                        fd.name,
                        "body raw "
                        + str(body_index)
                        + " "
                        + type(raw_stmt).__name__
                        + loc,
                    )
                    body_index += 1
                _func_codegen_log(self, debug_codegen, fd.name, "body len end")
            _func_codegen_log(self, debug_codegen, fd.name, "body begin")
            if debug_codegen:
                _func_codegen_log(
                    self, debug_codegen, fd.name, "terminated probe begin"
                )
                if self._builder_block_is_terminated():
                    _func_codegen_log(
                        self, debug_codegen, fd.name, "terminated probe true"
                    )
                else:
                    _func_codegen_log(
                        self,
                        debug_codegen,
                        fd.name,
                        "terminated probe false",
                    )
                _func_codegen_log(self, debug_codegen, fd.name, "terminated probe end")
                direct_index = 0
                for direct_stmt in body_stmts:
                    if self._builder_block_is_terminated():
                        _func_codegen_log(
                            self,
                            debug_codegen,
                            fd.name,
                            "direct stmt stop terminated " + str(direct_index),
                        )
                        break
                    try:
                        self._codegen_trace_set_stmt_context(
                            direct_index,
                            type(direct_stmt).__name__,
                        )
                    except Exception:
                        pass
                    span = getattr(direct_stmt, "span", None)
                    loc = ""
                    if span is not None:
                        loc = ":" + str(span.line) + ":" + str(span.col)
                    _func_codegen_log(
                        self,
                        debug_codegen,
                        fd.name,
                        "direct stmt begin "
                        + str(direct_index)
                        + " "
                        + type(direct_stmt).__name__
                        + loc,
                    )
                    self._emit_stmt(direct_stmt)
                    _func_codegen_log(
                        self,
                        debug_codegen,
                        fd.name,
                        "direct stmt end "
                        + str(direct_index)
                        + " "
                        + type(direct_stmt).__name__,
                    )
                    direct_index += 1
            else:
                self._emit_stmts(body_stmts)
            _func_codegen_log(self, debug_codegen, fd.name, "body end")
            if debug_codegen:
                try:
                    entry_instr_count = len(fn.blocks[0]._instrs)
                except AttributeError:
                    entry_instr_count = -1
                _func_codegen_log(
                    self,
                    debug_codegen,
                    fd.name,
                    "post body entry_instrs " + str(entry_instr_count),
                )
                try:
                    first_instr = fn.blocks[0]._instrs[0]
                    _func_codegen_log(
                        self,
                        debug_codegen,
                        fd.name,
                        "post body entry_first " + str(first_instr.text),
                    )
                except (AttributeError, IndexError):
                    _func_codegen_log(
                        self,
                        debug_codegen,
                        fd.name,
                        "post body entry_first <missing>",
                    )

            # If the terminator is missing (body fell through), insert the
            # Python implicit-None return. Pointer-ABI functions must return
            # ``py_None`` here: a null pointer is reserved for the C-API error
            # sentinel and makes successful PyObject_Call users report a
            # spurious failure. Non-pointer typed ABIs retain their historical
            # zero-value fallback to keep otherwise-invalid IR well-formed.
            _func_codegen_log(self, debug_codegen, fd.name, "default return begin")
            if not self._builder_block_is_terminated():
                if isinstance(fn.function_type.return_type, ir.VoidType):
                    self._emit_owned_local_cleanup()
                    self.builder.ret_void()
                elif isinstance(fn.function_type.return_type, ir.PointerType):
                    self._emit_owned_local_cleanup()
                    if (
                        getattr(self, "_freestanding_module", False)
                        and c_abi_sym is not None
                    ):
                        self.builder.ret(
                            ir.Constant(fn.function_type.return_type, None)
                        )
                    else:
                        self.builder.ret(self._emit_none_literal())
                else:
                    self._emit_owned_local_cleanup()
                    self.builder.ret(self._zero_of(fn.function_type.return_type))
            _func_codegen_log(self, debug_codegen, fd.name, "default return end")
            _func_codegen_log(self, debug_codegen, fd.name, "exit")
        except BaseException as exc:
            self._codegen_trace_dump(exc)
            raise exc
        finally:
            self._strict_stub_user_function_with_cpy_fallback(fn, fd)
            self.builder = saved_builder
            self._di_restore_scope(saved_di_scope)
            self.current_function = saved_fn
            self.current_func_def = saved_fd
            self._current_entry_block = saved_entry_block
            self._try_err_block = saved_try_err_block
            self._cpy_operand_cleanup_block = saved_cpy_operand_cleanup_block
            self._current_global_names = saved_global_names
            self.env = saved_env
            self.env_class_hint = saved_env_class_hint
            self.env_class_object_hint = saved_env_class_object_hint
            self.env_list_elem_class_hint = saved_env_list_elem_class_hint
            self._threading_list_elem_flags = saved_threading_list_elem_flags
            self._weak_dict_env_flags = saved_weak_dict_flags
            self._cpy_env_flags = saved_cpy_env_flags
            self._cpy_values = saved_cpy_values
            self._owned_cpy_values = saved_owned_cpy_values
            self._ir_builder_env_flags = saved_ir_builder_flags
            self._box_int_locals = saved_box_int_locals
            self._exact_int_env_flags = saved_exact_int_flags
            self._planned_exact_int_local_names = (
                saved_planned_exact_int_local_names
            )
            self._async_body_depth = saved_async_body_depth
            self._owned_local_names = saved_owned_local_names
            self._owned_local_has_value = saved_owned_local_has_value
            self._owned_local_flag_slots = saved_owned_local_flag_slots
            self._owned_local_flag_allocas = saved_owned_local_flag_allocas
            self._for_target_owned_names = saved_for_target_owned_names
            self._gc_rooted_local_names = saved_gc_rooted_local_names
            self._gc_rooted_local_order = saved_gc_rooted_local_order
            self._borrowed_gc_rooted_local_names = saved_borrowed_gc_rooted_local_names
            self._pinned_gc_rooted_local_names = saved_pinned_gc_rooted_local_names
            self._except_binding_names = saved_except_binding_names
            self._active_handler_excs = saved_active_handler_excs
            self._container_temp_root_slot_names = saved_container_temp_root_slot_names
            self._current_param_names = saved_current_param_names
            self._lambda_lexical_shadow_names = saved_lambda_lexical_shadow_names
            self.loop_stack = saved_loop_stack
            self.current_class = saved_class
            self.current_method_kind = saved_kind
            if self._codegen_trace_is_enabled():
                self._codegen_current_stmt_index = saved_trace_stmt_index
                self._codegen_current_stmt_kind = saved_trace_stmt_kind
                self._codegen_current_expr_kind = saved_trace_expr_kind

    def _emit_hoist_adapter(
        self,
        orig_name: str,
        full_fn: ir.Function,
        entry: dict,
    ) -> ir.Function:
        """Synthesize an adapter ir.Function for a hoisted nested def
        with captures, matching the ORIGINAL arity. The adapter:

        1. Reads each capture from an internal global (populated in
           the outer scope at wrap time via ``_emit_hoist_adapter_caps``).
        2. Calls the full hoisted function with
           ``(original_args..., cap_0, cap_1, ...)``.
        3. Returns the result.

        Stores the capture-globals dict in ``entry`` so the outer
        scope's wrap-site can store into the same globals. Caller is
        the wrap site; it invokes this method which is idempotent
        (re-using the adapter on subsequent references)."""
        cached = entry.get("adapter_ir")
        if cached is not None:
            # Still need to populate capture globals from the outer
            # scope. Emit the stores at the CURRENT builder position.
            for fv in entry["free_names"]:
                gv = entry["capture_globals"][fv]
                cpy_val = self._capture_value_as_cpython(fv)
                if cpy_val is None:
                    continue
                self.builder.store(cpy_val, gv)
            return cached

        arity = entry["original_arity"]
        free_names = entry["free_names"]
        adapter_name = (
            f"user_{(self.ast_module.name or 'mod').replace('.', '_')}"
            f"_{orig_name}_adapter"
        )
        fnty = ir.FunctionType(_CSTR, [_CSTR] * arity)
        existing = self.module.globals.get(adapter_name)
        if isinstance(existing, ir.Function):
            adapter_ir = existing
        else:
            adapter_ir = ir.Function(self.module, fnty, name=adapter_name)
            adapter_ir.linkage = "internal"

        # Create a capture-global per free var (if not already).
        capture_globals: dict = {}
        for fv in free_names:
            gv_name = f".hoist_cap_{orig_name}_{fv}"
            gv = self.module.globals.get(gv_name)
            if gv is None:
                gv = ir.GlobalVariable(self.module, _CSTR, name=gv_name)
                gv.linkage = "internal"
                gv.initializer = ir.Constant(_CSTR, None)
            capture_globals[fv] = gv
        entry["capture_globals"] = capture_globals

        # Emit the adapter body. Save outer builder state.
        saved_builder = self.builder
        # Swap self.builder rather than using a local
        # ``tmp_builder = ir.IRBuilder(...)`` alias: pcc-py self-host's
        # ``_ir_builder_env_flags`` doesn't reliably register local
        # IRBuilder names, so ``tmp_builder.METHOD`` falls through
        # scaffold dispatch into the wrong class. The scaffold dispatch
        # recognizes the leading ``self.builder`` form directly.
        entry_block = adapter_ir.append_basic_block(name="entry")
        self.builder = ir.IRBuilder(entry_block)

        # Load each capture from its global.
        cap_vals = []
        for fv in free_names:
            gv = capture_globals[fv]
            v = self.builder.load(gv, name=f"{fv}.cap")
            cap_vals.append(v)
        # Call the full hoisted function with original args + captures.
        all_args = list(adapter_ir.args) + cap_vals
        ret_ty = full_fn.function_type.return_type
        if isinstance(ret_ty, ir.PointerType):
            result = self.builder.call(full_fn, all_args, name="result")
            self.builder.ret(result)
        elif isinstance(ret_ty, ir.VoidType):
            self.builder.call(full_fn, all_args)
            py_none_gv = declare_runtime_global(self.module, "py_None")
            self.builder.ret(self.builder.load(py_none_gv, name="none"))
        elif isinstance(ret_ty, ir.IntType) and ret_ty.width == 1:
            raw = self.builder.call(full_fn, all_args, name="raw")
            bit = self.builder.zext(raw, _I32, name="b2i32")
            boxed = self.builder.call(
                self.runtime["py_bool_from_bit"],
                [bit],
                name="boxed",
            )
            self.builder.ret(boxed)
        elif isinstance(ret_ty, ir.IntType) and ret_ty.width == 64:
            raw = self.builder.call(full_fn, all_args, name="raw")
            boxed = self.builder.call(
                self.runtime["py_int_from_i64"],
                [raw],
                name="boxed",
            )
            self.builder.ret(boxed)
        else:
            raise NotImplementedError(
                f"capture adapter for return type {ret_ty} not supported"
            )

        # Restore the outer builder.
        self.builder = saved_builder
        entry["adapter_ir"] = adapter_ir

        # Store outer-scope capture values into the globals before the
        # adapter is handed to ``py_cpy_wrap_pcc_Narg``. If a capture
        # isn't in env (e.g. the capture is a top-level user function
        # reference), skip the store — the initializer None will leave
        # it unset, which is wrong but matches the existing hoist's
        # fall-through case.
        for fv in free_names:
            gv = capture_globals[fv]
            cpy_val = self._capture_value_as_cpython(fv)
            if cpy_val is None:
                continue
            self.builder.store(cpy_val, gv)

        return adapter_ir

    def _emit_native_func_adapter(
        self,
        orig_name: str,
        full_fn: ir.Function,
        original_args: tuple,
        free_names: tuple[str, ...],
        return_ty: Type | None,
    ) -> ir.Function:
        """Build the generic pcc-native function adapter.

        Runtime function objects always call `entry(captures, args)`.
        The adapter unpacks that generic tuple ABI and forwards into the
        real hoisted FuncDef ABI, then boxes the result back to PyObject*.
        """
        adapter_name = (
            f"user_{(self.ast_module.name or 'mod').replace('.', '_')}"
            f"_{orig_name}_native_adapter"
        )
        # The Python name is a live binding and may be reused by a later
        # ``def``.  The native symbol is the stable identity of this exact
        # FuncDef body, so repeated definitions must not share the legacy
        # name-keyed adapter.
        adapter_fd = self._native_symbol_funcdefs.get(full_fn.name)
        if (
            adapter_fd is not None
            and adapter_fd.name in self._duplicate_module_function_names
        ):
            adapter_name = full_fn.name + "_native_adapter"
        existing = self.module.globals.get(adapter_name)
        if isinstance(existing, ir.Function):
            return existing

        adapter_ty = ir.FunctionType(_CSTR, [_CSTR, _CSTR])
        adapter_ir = ir.Function(self.module, adapter_ty, name=adapter_name)
        entry = adapter_ir.append_basic_block(name="entry")
        saved_builder = self.builder
        saved_current_function = self.current_function
        saved_entry_block = getattr(self, "_current_entry_block", None)
        self.builder = ir.IRBuilder(entry)
        self.current_function = adapter_ir
        self._current_entry_block = entry

        array_boundary = isinstance(return_ty, ValueArrayType)
        if not array_boundary:
            for ast_arg in original_args:
                if isinstance(ast_arg.annotation, ValueArrayType):
                    array_boundary = True
                    break
        if array_boundary:
            msg = self._pooled_cstr_ptr(
                "pcc.array is unavailable through a dynamic function boundary",
                ".value.array.dynamic.msg",
            )
            exc = self.builder.call(
                self.runtime["py_exc_new"],
                [ir.Constant(_I64, 3), msg],
                name=self._fresh("value.array.dynamic.exc"),
            )
            self.builder.call(self.runtime["py_raise"], [exc])
            self._gc_release(exc)
            self.builder.ret(ir.Constant(_CSTR, None))
            self.builder = saved_builder
            self.current_function = saved_current_function
            self._current_entry_block = saved_entry_block
            return adapter_ir

        forwarded: list[ir.Value] = []
        owned_inputs: list[ir.Value] = []
        full_fn_arg_types = tuple(getattr(full_fn.function_type, "args", ()))
        for i, ast_arg in enumerate(original_args):
            param_ir_ty = getattr(full_fn.args[i], "type", None)
            if param_ir_ty is None and i < len(full_fn_arg_types):
                param_ir_ty = full_fn_arg_types[i]
            if ast_arg.kind == "*args":
                arg_obj = self.builder.call(
                    self.runtime["py_tuple_get_known"],
                    [adapter_ir.args[1], ir.Constant(_I64, i)],
                    name=f"arg.varargs.{i}",
                )
                owned_inputs.append(arg_obj)
                if isinstance(param_ir_ty, ir.PointerType):
                    forwarded.append(arg_obj)
                else:
                    forwarded.append(
                        marshal.marshal_from_object(
                            self.builder,
                            self.module,
                            self.runtime,
                            arg_obj,
                            ast_arg.annotation or DynType(name="dyn"),
                        )
                    )
                continue
            arg_obj = self.builder.call(
                self.runtime["py_tuple_get_known"],
                [adapter_ir.args[1], ir.Constant(_I64, i)],
                name=f"arg.{i}",
            )
            owned_inputs.append(arg_obj)
            target_ty = ast_arg.annotation or DynType(name="dyn")
            if isinstance(param_ir_ty, ir.PointerType):
                forwarded.append(arg_obj)
            else:
                payload = self._emit_object_to_valueclass_payload(
                    arg_obj,
                    target_ty,
                )
                if payload is not None:
                    forwarded.append(payload)
                else:
                    forwarded.append(
                        marshal.marshal_from_object(
                            self.builder,
                            self.module,
                            self.runtime,
                            arg_obj,
                            target_ty,
                        )
                    )

        base = len(original_args)
        for i, _fv in enumerate(free_names):
            cap_obj = self.builder.call(
                self.runtime["py_tuple_get"],
                [adapter_ir.args[0], ir.Constant(_I64, i)],
                name=f"cap.{i}",
            )
            owned_inputs.append(cap_obj)
            arg_index = base + i
            param_ir_ty = getattr(full_fn.args[arg_index], "type", None)
            if param_ir_ty is None and arg_index < len(full_fn_arg_types):
                param_ir_ty = full_fn_arg_types[arg_index]
            if isinstance(param_ir_ty, ir.PointerType):
                forwarded.append(cap_obj)
            else:
                forwarded.append(
                    marshal.marshal_from_object(
                        self.builder,
                        self.module,
                        self.runtime,
                        cap_obj,
                        DynType(name="dyn"),
                    )
                )

        ret_ty = full_fn.function_type.return_type
        if isinstance(ret_ty, ir.VoidType):
            self.builder.call(full_fn, forwarded)
            for owned in owned_inputs:
                self._gc_release(owned, "native_adapter_input")
            none_gv = declare_runtime_global(self.module, "py_None")
            self.builder.ret(self.builder.load(none_gv, name="none"))
        else:
            result = self.builder.call(full_fn, forwarded, name="result")
            for owned in owned_inputs:
                self._gc_release(owned, "native_adapter_input")
            if isinstance(ret_ty, ir.PointerType):
                self.builder.ret(result)
            else:
                boxed = self._emit_valueclass_payload_to_object(
                    result,
                    return_ty or DynType(name="dyn"),
                    consume_fields=True,
                )
                if boxed is None:
                    boxed = marshal.marshal_to_object(
                        self.builder,
                        self.module,
                        self.runtime,
                        result,
                        return_ty or DynType(name="dyn"),
                    )
                self.builder.ret(boxed)

        self.builder = saved_builder
        self.current_function = saved_current_function
        self._current_entry_block = saved_entry_block
        return adapter_ir

    def _emit_async_native_func_value_adapter(
        self,
        orig_name: str,
        body_adapter: ir.Function,
    ) -> ir.Function:
        """Build a PyFunc entry for async function values.

        Direct async calls are lowered to ``py_coroutine_new_native`` at the
        call site. When an async function is first-class (for example through
        ``functools.partial``), the PyFunc entry itself must return that
        coroutine shell instead of running the async body immediately.
        """
        native_suffix = "_native_adapter"
        if body_adapter.name.endswith(native_suffix):
            adapter_name = (
                body_adapter.name[: -len(native_suffix)]
                + "_async_value_adapter"
            )
        else:
            adapter_name = body_adapter.name + "_async_value_adapter"
        existing = self.module.globals.get(adapter_name)
        if isinstance(existing, ir.Function):
            return existing

        adapter_ty = ir.FunctionType(_CSTR, [_CSTR, _CSTR])
        adapter_ir = ir.Function(self.module, adapter_ty, name=adapter_name)
        entry = adapter_ir.append_basic_block(name="entry")
        saved_builder = self.builder
        self.builder = ir.IRBuilder(entry)
        runner = self.builder.bitcast(
            body_adapter,
            _CSTR,
            name=f"{orig_name}.async.runner",
        )
        coro = self.builder.call(
            self.runtime["py_coroutine_new_native"],
            [
                self._attr_name_ptr(orig_name),
                runner,
                adapter_ir.args[0],
                adapter_ir.args[1],
            ],
            name=f"{orig_name}.async.value.coro",
        )
        self.builder.ret(coro)
        self.builder = saved_builder
        return adapter_ir

    def _emit_native_func_default_object(self, expr: Expr) -> ir.Value:
        valueclass_payload = self._maybe_emit_valueclass_constructor_payload(
            getattr(expr, "ty", None),
            expr,
        )
        if valueclass_payload is not None:
            boxed_valueclass = self._emit_valueclass_payload_to_object(
                valueclass_payload,
                expr.ty,
                consume_fields=True,
            )
            if boxed_valueclass is not None:
                return boxed_valueclass
        old_prefer_native = self._prefer_native_callable_values
        self._prefer_native_callable_values = True
        try:
            raw = self._emit_expr(expr)
        finally:
            self._prefer_native_callable_values = old_prefer_native
        if raw in getattr(self, "_cpy_values", ()):
            return self._emit_value_as_pcc_object_or_bridge(
                raw,
                expr.ty,
                "func.default.bridge",
            )
        return marshal.marshal_to_object(
            self.builder,
            self.module,
            self.runtime,
            raw,
            expr.ty,
        )

    def _emit_native_func_signature(self, original_args: tuple) -> ir.Value:
        n = len(original_args)
        names = self.builder.call(
            self.runtime["py_tuple_new"],
            [ir.Constant(_I64, n)],
            name=self._fresh("func.sig.names"),
        )
        kinds = self.builder.call(
            self.runtime["py_tuple_new"],
            [ir.Constant(_I64, n)],
            name=self._fresh("func.sig.kinds"),
        )
        has_defaults = self.builder.call(
            self.runtime["py_tuple_new"],
            [ir.Constant(_I64, n)],
            name=self._fresh("func.sig.has_defaults"),
        )
        defaults = self.builder.call(
            self.runtime["py_tuple_new"],
            [ir.Constant(_I64, n)],
            name=self._fresh("func.sig.defaults"),
        )

        for i, ast_arg in enumerate(original_args):
            name_obj = self._emit_str_literal(ast_arg.name)
            kind_obj = self.builder.call(
                self.runtime["py_int_from_i64"],
                [ir.Constant(_I64, _PCC_FUNC_KIND.get(ast_arg.kind, 0))],
                name=self._fresh("func.sig.kind"),
            )
            has_default = bool(
                getattr(ast_arg, "has_default", False)
                and getattr(ast_arg, "default", None) is not None
            )
            has_default_obj = self.builder.call(
                self.runtime["py_bool_from_bit"],
                [ir.Constant(_I32, 1 if has_default else 0)],
                name=self._fresh("func.sig.has_default"),
            )
            if has_default:
                default_expr = ast_arg.default
                default_obj = self._emit_native_func_default_object(default_expr)
                default_is_owned = self._container_store_temp_needs_release(
                    default_expr,
                    default_expr.ty,
                    self._expr_looks_cpython(default_expr),
                )
            else:
                default_obj = self._emit_none_literal()
                default_is_owned = True
            self.builder.call(
                self.runtime["py_tuple_set_item"],
                [names, ir.Constant(_I64, i), name_obj],
            )
            self.builder.call(
                self.runtime["py_tuple_set_item"],
                [kinds, ir.Constant(_I64, i), kind_obj],
            )
            self.builder.call(
                self.runtime["py_tuple_set_item"],
                [has_defaults, ir.Constant(_I64, i), has_default_obj],
            )
            self.builder.call(
                self.runtime["py_tuple_set_item"],
                [defaults, ir.Constant(_I64, i), default_obj],
            )
            self._gc_release(name_obj)
            self._gc_release(kind_obj)
            self._gc_release(has_default_obj)
            if default_is_owned:
                self._gc_release(default_obj)

        sig = self.builder.call(
            self.runtime["py_tuple_new"],
            [ir.Constant(_I64, 5)],
            name=self._fresh("func.sig"),
        )
        self.builder.call(
            self.runtime["py_tuple_set_item"],
            [
                sig,
                ir.Constant(_I64, 0),
                self._emit_str_literal(_PCC_FUNC_SIGNATURE_MAGIC),
            ],
        )
        self.builder.call(
            self.runtime["py_tuple_set_item"],
            [sig, ir.Constant(_I64, 1), names],
        )
        self.builder.call(
            self.runtime["py_tuple_set_item"],
            [sig, ir.Constant(_I64, 2), kinds],
        )
        self.builder.call(
            self.runtime["py_tuple_set_item"],
            [sig, ir.Constant(_I64, 3), has_defaults],
        )
        self.builder.call(
            self.runtime["py_tuple_set_item"],
            [sig, ir.Constant(_I64, 4), defaults],
        )
        self._gc_release(names)
        self._gc_release(kinds)
        self._gc_release(has_defaults)
        self._gc_release(defaults)
        return sig

    def _native_func_signature_has_defaults(self, original_args: tuple) -> bool:
        for ast_arg in original_args:
            if (
                getattr(ast_arg, "has_default", False)
                and getattr(ast_arg, "default", None) is not None
            ):
                return True
        return False

    def _native_func_signature_cache_name(self, key: str) -> str:
        safe = []
        for ch in key:
            if ("a" <= ch <= "z") or ("A" <= ch <= "Z") or ("0" <= ch <= "9"):
                safe.append(ch)
            else:
                safe.append("_")
        return "__pcc_native_func_sig_cache_" + "".join(safe)

    def _emit_cached_native_func_signature(
        self,
        original_args: tuple,
        cache_key: str,
    ) -> ir.Value:
        if self._native_func_signature_has_defaults(original_args):
            return self._emit_native_func_signature(original_args)

        cache_name = self._native_func_signature_cache_name(cache_key)
        existing = self.module.globals.get(cache_name)
        if isinstance(existing, ir.GlobalVariable):
            cache_gv = existing
        else:
            cache_gv = ir.GlobalVariable(self.module, _CSTR, name=cache_name)
            cache_gv.linkage = "internal"
            cache_gv.initializer = ir.Constant(_CSTR, None)

        cached = self.builder.load(
            cache_gv,
            name=self._fresh("func.sig.cached"),
        )
        has_cached = self.builder.icmp_unsigned(
            "!=",
            cached,
            ir.Constant(_CSTR, None),
            name=self._fresh("func.sig.has_cached"),
        )
        check_bb = self.builder.block
        create_bb = self.current_function.append_basic_block(
            name=self._fresh("func.sig.create"),
        )
        done_bb = self.current_function.append_basic_block(
            name=self._fresh("func.sig.done"),
        )
        self.builder.cbranch(has_cached, done_bb, create_bb)

        self.builder.position_at_end(create_bb)
        created = self._emit_native_func_signature(original_args)
        self._gc_pin(created)
        self.builder.store(created, cache_gv)
        create_exit = self.builder.block
        self.builder.branch(done_bb)

        self.builder.position_at_end(done_bb)
        result = self.builder.phi(_CSTR, name=self._fresh("func.sig"))
        result.add_incoming(cached, check_bb)
        result.add_incoming(created, create_exit)
        return result

    def _emit_native_func_value(
        self,
        orig_name: str,
        resolved_name: str,
        full_fn: ir.Function,
        free_names: tuple[str, ...],
    ) -> ir.Value:
        # Metadata (arity/defaults/docstring/async) belongs to the exact
        # FuncDef behind ``full_fn``.  A name lookup returns the first or last
        # rebinding and can silently give another definition's contract.
        fd = self._native_symbol_funcdefs.get(full_fn.name)
        if fd is None:
            fd = self._find_user_funcdef(resolved_name)
        runtime_args = tuple(a for a in fd.args if a.name != "")
        original_arity = max(len(runtime_args) - len(free_names), 0)
        original_args = runtime_args[:original_arity]
        body_adapter = self._emit_native_func_adapter(
            orig_name,
            full_fn,
            original_args,
            free_names,
            fd.return_ty,
        )
        adapter = (
            self._emit_async_native_func_value_adapter(orig_name, body_adapter)
            if fd.is_async
            else body_adapter
        )
        captures = self.builder.call(
            self.runtime["py_tuple_new"],
            [ir.Constant(_I64, len(free_names))],
            name=self._fresh("closure.captures"),
        )
        for i, fv in enumerate(free_names):
            if (
                fv == "__class__"
                and getattr(self, "current_class", None) is not None
                and self.env.get(fv) is None
            ):
                raw = self.builder.load(
                    self.current_class.global_var,
                    name=self._fresh(f"closure.cls.{self.current_class.name}"),
                )
            else:
                raw = self._emit_name(
                    Name(span=fd.span, ty=DynType(name="dyn"), ident=fv)
                )
            env_entry = self.env.get(fv)
            capture_ty = (
                env_entry[2]
                if env_entry is not None and len(env_entry) >= 3
                else DynType(name="dyn")
            )
            obj = self._emit_value_as_pcc_object_or_bridge(
                raw,
                capture_ty,
                "closure.cap.bridge",
            )
            self.builder.call(
                self.runtime["py_tuple_set_item"],
                [captures, ir.Constant(_I64, i), obj],
            )
        signature = self._emit_native_func_signature(original_args)
        wrapped_captures = self.builder.call(
            self.runtime["py_tuple_new"],
            [ir.Constant(_I64, 2)],
            name=self._fresh("closure.signature.wrapper"),
        )
        self.builder.call(
            self.runtime["py_tuple_set_item"],
            [wrapped_captures, ir.Constant(_I64, 0), captures],
        )
        self.builder.call(
            self.runtime["py_tuple_set_item"],
            [wrapped_captures, ir.Constant(_I64, 1), signature],
        )
        fn_obj = self.builder.call(
            self.runtime["py_func_new_named"],
            [adapter, wrapped_captures, self._attr_name_ptr(orig_name)],
            name=self._fresh(f"{orig_name}.func"),
        )
        if (
            fd.body
            and isinstance(fd.body[0], ExprStmt)
            and isinstance(fd.body[0].expr, StrLit)
        ):
            doc = self._emit_str_literal(str(fd.body[0].expr.value))
            self.builder.call(
                self.runtime["py_obj_setattr"],
                [fn_obj, self._attr_name_ptr("__doc__"), doc],
            )
            self._emit_post_call_err_check(fd.span)
        self._gc_release(captures)
        self._gc_release(signature)
        self._gc_release(wrapped_captures)

        # Hoisting appends nested defs to ``ast_module.body`` so their native
        # bodies can be declared, but executing an enclosing ``def`` must still
        # create a fresh function object (and freshly evaluated defaults) each
        # time.  ``_hoisted_capture_params`` is the authoritative lexical-
        # nesting marker even when the nested function captures no body names.
        # Never let those synthetic module declarations enter the stable
        # module-function cache.
        if resolved_name in getattr(self, "_hoisted_capture_params", {}):
            return fn_obj

        is_module_top_level = False
        fd_id = id(fd)
        for top_stmt in self.ast_module.body:
            if isinstance(top_stmt, FuncDef) and id(top_stmt) == fd_id:
                is_module_top_level = True
                break
        if not is_module_top_level or free_names:
            return fn_obj

        safe_mod = (self.ast_module.name or "mod").replace(".", "_").replace("-", "_")
        safe_name = resolved_name.replace(".", "_").replace("-", "_")
        cache_name = "__pcc_native_func_value_cache_" + safe_mod + "_" + safe_name
        if fd.name in self._duplicate_module_function_names:
            cache_name = cache_name + "." + full_fn.name
        existing_cache = self.module.globals.get(cache_name)
        if isinstance(existing_cache, ir.GlobalVariable):
            cache_gv = existing_cache
        else:
            cache_gv = ir.GlobalVariable(self.module, _CSTR, name=cache_name)
            cache_gv.linkage = "internal"
            cache_gv.initializer = ir.Constant(_CSTR, None)

        cached = self.builder.load(cache_gv, name=self._fresh("func.value.cached"))
        has_cached = self.builder.icmp_unsigned(
            "!=",
            cached,
            ir.Constant(_CSTR, None),
            name=self._fresh("func.value.has_cached"),
        )
        cached_bb = self.current_function.append_basic_block(
            name=self._fresh("func.value.use_cached"),
        )
        created_bb = self.current_function.append_basic_block(
            name=self._fresh("func.value.use_created"),
        )
        done_bb = self.current_function.append_basic_block(
            name=self._fresh("func.value.done"),
        )
        self.builder.cbranch(has_cached, cached_bb, created_bb)

        self.builder.position_at_end(cached_bb)
        self._gc_release(fn_obj)
        self.builder.call(self.runtime["py_incref"], [cached])
        cached_exit = self.builder.block
        self.builder.branch(done_bb)

        self.builder.position_at_end(created_bb)
        self.builder.call(self.runtime["py_incref"], [fn_obj])
        self._gc_pin(fn_obj)
        self.builder.store(fn_obj, cache_gv)
        created_exit = self.builder.block
        self.builder.branch(done_bb)

        self.builder.position_at_end(done_bb)
        result = self.builder.phi(_CSTR, name=self._fresh("func.value"))
        result.add_incoming(cached, cached_exit)
        result.add_incoming(fn_obj, created_exit)
        return result

    def _decorators_are_native_functions(self, fd: FuncDef) -> bool:
        decorators = self._func_decorators(fd)
        if not decorators:
            return False
        saw_native_decorator = False
        i = 0
        while i < len(decorators):
            dec = decorators[i]
            if self._decorator_is_runtime_partial_factory(dec):
                saw_native_decorator = True
                i += 1
                continue
            if self._decorator_is_noop_whitelist(dec):
                i += 1
                continue
            if not isinstance(dec, Name):
                return False
            if dec.ident not in self.functions:
                return False
            saw_native_decorator = True
            i += 1
        return saw_native_decorator

    def _decorator_is_lru_cache_family(self, dec: Expr) -> bool:
        qn = self._decorator_qualname(dec)
        return qn in (
            "lru_cache",
            "functools.lru_cache",
            "cache",
            "functools.cache",
        )

    def _func_has_lru_cache_family_decorator(self, fd: FuncDef) -> bool:
        for dec in self._func_decorators(fd):
            if self._decorator_is_lru_cache_family(dec):
                return True
        return False

    def _lru_cache_arg_expr_is_safe_to_reemit(self, expr: Expr) -> bool:
        if isinstance(
            expr,
            (Name, Attr, IntLit, BoolLit, FloatLit, StrLit, BytesLit, NoneLit),
        ):
            if isinstance(expr, Attr):
                return self._lru_cache_arg_expr_is_safe_to_reemit(expr.obj)
            return True
        if isinstance(expr, Call) and isinstance(expr.func, Name):
            return (
                expr.func.ident == "len"
                and len(expr.args) == 1
                and not expr.kwargs
                and self._lru_cache_arg_expr_is_safe_to_reemit(expr.args[0])
            )
        return False

    def _lru_cache_args_are_safe_to_reemit(
        self,
        args: tuple[Expr, ...],
        kwargs: tuple[tuple[str, Expr], ...],
    ) -> bool:
        if kwargs:
            return False
        for arg in args:
            if not self._lru_cache_arg_expr_is_safe_to_reemit(arg):
                return False
        return True

    def _lru_cache_global_name(self, func_name: str) -> str:
        mod_name = self.ast_module.name or "mod"
        safe_mod = mod_name.replace(".", "_").replace("-", "_")
        safe_func = func_name.replace(".", "_").replace("-", "_")
        return "__pcc_lru_cache_" + safe_mod + "_" + safe_func

    def _emit_lru_cache_dict_for_function(self, func_name: str) -> ir.Value:
        cache_name = self._lru_cache_global_name(func_name)
        gv, declared_ty = self._ensure_module_global_name(
            cache_name,
            DynType(name="dyn"),
        )
        loaded = self.builder.load(gv, name=self._fresh(func_name + ".cache"))
        is_null = self.builder.icmp_unsigned(
            "==",
            loaded,
            ir.Constant(loaded.type, None),
            name=self._fresh(func_name + ".cache.isnull"),
        )
        fn = self.current_function
        init_bb = fn.append_basic_block(self._fresh(func_name + ".cache.init"))
        ready_bb = fn.append_basic_block(self._fresh(func_name + ".cache.ready"))
        current_bb = self.builder.block
        self.builder.cbranch(is_null, init_bb, ready_bb)

        self.builder.position_at_end(init_bb)
        new_cache = self.builder.call(
            self.runtime["py_dict_new"],
            [],
            name=self._fresh(func_name + ".cache.new"),
        )
        self._store_module_global_root_value(
            gv,
            new_cache,
            declared_ty=declared_ty,
            value_is_owned=True,
        )
        self.builder.branch(ready_bb)
        init_end_bb = self.builder.block

        self.builder.position_at_end(ready_bb)
        phi = self.builder.phi(loaded.type, name=self._fresh(func_name + ".cache.phi"))
        phi.add_incoming(loaded, current_bb)
        phi.add_incoming(new_cache, init_end_bb)
        return phi

    def _emit_lru_cache_key_for_args(self, args: tuple[Expr, ...]) -> ir.Value:
        if not args:
            return self._emit_empty_tuple(self._fresh("lru.key.empty"))
        return self._emit_call_args_tuple(args)

    def _maybe_emit_lru_cached_user_function_call(
        self,
        *,
        name: str,
        fn: ir.Function,
        ast_func_def: FuncDef,
        args: tuple[Expr, ...],
        kwargs: tuple[tuple[str, Expr], ...],
    ) -> Optional[ir.Value]:
        if not self._func_has_lru_cache_family_decorator(ast_func_def):
            return None
        if not self._lru_cache_args_are_safe_to_reemit(args, kwargs):
            return None
        ret_ty = ast_func_def.return_ty
        if ret_ty is None or isinstance(ret_ty, NoneType):
            return None
        if isinstance(fn.function_type.return_type, ir.VoidType):
            return None

        cache = self._emit_lru_cache_dict_for_function(name)
        key = self._emit_lru_cache_key_for_args(args)
        cached = self.builder.call(
            self.runtime["py_dict_get"],
            [cache, key],
            name=self._fresh(name + ".cache.get"),
        )
        hit = self.builder.icmp_unsigned(
            "!=",
            cached,
            ir.Constant(cached.type, None),
            name=self._fresh(name + ".cache.hit"),
        )
        fn_cur = self.current_function
        hit_bb = fn_cur.append_basic_block(self._fresh(name + ".cache.hit.bb"))
        miss_bb = fn_cur.append_basic_block(self._fresh(name + ".cache.miss.bb"))
        done_bb = fn_cur.append_basic_block(self._fresh(name + ".cache.done.bb"))
        self.builder.cbranch(hit, hit_bb, miss_bb)

        self.builder.position_at_end(hit_bb)
        hit_value = self._coerce_from_object(cached, ret_ty)
        if not (self._is_object(ret_ty) or isinstance(ret_ty, DynType)):
            self._gc_release(cached, self._release_context_label("lru_cache_hit"))
        self._gc_release(key, self._release_context_label("lru_cache_key_hit"))
        self.builder.branch(done_bb)
        hit_end_bb = self.builder.block

        self.builder.position_at_end(miss_bb)
        miss_value = self._emit_direct_user_function_call(
            display_name=name,
            fn=fn,
            ast_func_def=ast_func_def,
            args=args,
            kwargs=kwargs,
        )
        value_obj = self._emit_value_as_pcc_object_or_bridge(
            miss_value,
            ret_ty,
            self._fresh(name + ".cache.value"),
        )
        self.builder.call(self.runtime["py_dict_set"], [cache, key, value_obj])
        if value_obj is not miss_value:
            self._gc_release(
                value_obj,
                self._release_context_label("lru_cache_store_tmp"),
            )
        self._gc_release(key, self._release_context_label("lru_cache_key_miss"))
        self.builder.branch(done_bb)
        miss_end_bb = self.builder.block

        self.builder.position_at_end(done_bb)
        phi = self.builder.phi(
            fn.function_type.return_type,
            name=self._fresh(name + ".cache.result"),
        )
        phi.add_incoming(hit_value, hit_end_bb)
        phi.add_incoming(miss_value, miss_end_bb)
        return phi

    def _emit_decorated_user_function_value(
        self,
        *,
        name: str,
        fn: ir.Function,
        ast_func_def: FuncDef,
    ) -> ir.Value:
        fn_obj = self._emit_native_func_value(name, name, fn, ())
        for dec in reversed(self._func_decorators(ast_func_def)):
            if self._decorator_is_runtime_partial_factory(dec):
                decorator_callable = self._emit_expr(dec)
                apply_args = self.builder.call(
                    self.runtime["py_tuple_new"],
                    [ir.Constant(_I64, 1)],
                    name=self._fresh(f"{name}.decorator.args"),
                )
                self.builder.call(
                    self.runtime["py_tuple_set_item"],
                    [apply_args, ir.Constant(_I64, 0), fn_obj],
                )
                none_gv = declare_runtime_global(self.module, "py_None")
                none_obj = self.builder.load(
                    none_gv,
                    name=self._fresh(f"{name}.decorator.none"),
                )
                fn_obj = self.builder.call(
                    self.runtime["py_obj_call"],
                    [decorator_callable, apply_args, none_obj],
                    name=self._fresh(f"{name}.decorator.apply"),
                )
                self._gc_release(apply_args)
                self._gc_release(decorator_callable)
                self._emit_post_call_err_check(ast_func_def.span)
                continue
            if self._decorator_is_noop_whitelist(dec):
                continue
            assert isinstance(dec, Name)
            dec_fn = self.functions[dec.ident]
            dec_fd = self._find_user_funcdef(dec.ident)
            temp_name = f"__pcc_decorated_fn_{len(self.env)}"
            slot = self._alloca_in_entry(_CSTR, name=f"{temp_name}.addr")
            self.env[temp_name] = (slot, _CSTR, DynType(name="dyn"))
            self.builder.store(fn_obj, slot)
            fn_obj = self._emit_direct_user_function_call(
                display_name=dec.ident,
                fn=dec_fn,
                ast_func_def=dec_fd,
                args=(
                    Name(
                        span=ast_func_def.span,
                        ty=DynType(name="dyn"),
                        ident=temp_name,
                    ),
                ),
                kwargs=(),
            )
        return fn_obj

    def _emit_decorated_user_function_call(
        self,
        *,
        name: str,
        fn: ir.Function,
        ast_func_def: FuncDef,
        args: tuple[Expr, ...],
        kwargs: tuple[tuple[str, Expr], ...],
    ) -> ir.Value:
        fn_obj = self._emit_decorated_user_function_value(
            name=name,
            fn=fn,
            ast_func_def=ast_func_def,
        )
        kwdict_unpack = self._split_starstar_kwargs_unpack(args)
        arg_exprs = args
        kwargs_expr = None
        if kwdict_unpack is not None:
            arg_exprs, kwargs_expr = kwdict_unpack
        args_owned = not self._is_starred_unpack(arg_exprs)
        args_tuple = self._emit_dynamic_call_args_tuple(arg_exprs)
        kwargs_obj = self._emit_dynamic_call_kwargs_object(
            kwargs,
            kwargs_expr,
            ast_func_def.span,
        )
        result = self.builder.call(
            self.runtime["py_obj_call"],
            [fn_obj, args_tuple, kwargs_obj],
            name=self._fresh(f"{name}.decorated.call"),
        )
        if args_owned:
            self._gc_release(args_tuple)
        if kwargs:
            self._gc_release(kwargs_obj)
        self._emit_post_call_err_check(ast_func_def.span)
        return result

    def _emit_function_annotations_dict(
        self,
        name: str,
    ) -> Optional[ir.Value]:
        fd = None
        for stmt in self.ast_module.body:
            if isinstance(stmt, FuncDef) and stmt.name == name:
                fd = stmt
                break
        if fd is None:
            return None
        out = self.builder.call(
            self.runtime["py_dict_new"],
            [],
            name=self._fresh(f"{name}.annotations"),
        )
        for arg in fd.args:
            ann = arg.annotation
            if ann is None:
                continue
            key = self._emit_str_literal(arg.name)
            val = self._emit_str_literal(self._annotation_runtime_name(ann))
            self.builder.call(self.runtime["py_dict_set"], [out, key, val])
        if fd.return_ty is not None:
            key = self._emit_str_literal("return")
            val = self._emit_str_literal(self._annotation_runtime_name(fd.return_ty))
            self.builder.call(self.runtime["py_dict_set"], [out, key, val])
        return out

    def _emit_direct_user_function_call(
        self,
        *,
        display_name: str,
        fn: ir.Function,
        ast_func_def: FuncDef,
        args: tuple[Expr, ...],
        kwargs: tuple[tuple[str, Expr], ...],
        hoist_capture_name: Optional[str] = None,
    ) -> ir.Value:
        """Shared direct-call lowering for user / extern-native
        functions that already resolved to a concrete IR function."""
        has_click_decorator = any(
            self._decorator_is_noop_whitelist(d)
            and (self._decorator_qualname(d) or "").startswith("click.")
            for d in ast_func_def.decorators
        )
        call_kwargs = kwargs
        hoist_key = (
            hoist_capture_name if hoist_capture_name is not None else display_name
        )
        hoist_caps = getattr(self, "_hoisted_capture_params", {}).get(hoist_key)
        if hoist_caps:
            present_kw = {k for k, _ in call_kwargs}
            extra_kw = tuple(
                (fv, Name(span=ast_arg.span, ty=DynType(name="dyn"), ident=fv))
                for ast_arg in args[:1]
                or (
                    Name(
                        span=ast_func_def.span,
                        ty=DynType(name="dyn"),
                        ident="__unused__",
                    ),
                )
                for fv in hoist_caps
                if fv not in present_kw
            )
            if extra_kw:
                call_kwargs = call_kwargs + extra_kw
        if has_click_decorator:
            patched = tuple(
                (a if a.default is not None else _replace_arg_with_none_default(a))
                for a in ast_func_def.args
            )
            try:
                resolved_args = self._resolve_call_kwargs(
                    args,
                    call_kwargs,
                    patched,
                )
            except L1CodegenError:
                resolved_args = self._resolve_call_kwargs(
                    args,
                    call_kwargs,
                    ast_func_def.args,
                )
        else:
            resolved_args = self._resolve_call_kwargs(
                args,
                call_kwargs,
                ast_func_def.args,
            )
        runtime_formals = [a for a in ast_func_def.args if a.name != ""]
        args_ir: list[ir.Value] = []
        pinned_arg_temps: list[tuple[ir.Value, bool]] = []
        for index, (ast_arg, arg_def, ir_arg) in enumerate(
            zip(resolved_args, runtime_formals, fn.args)
        ):
            target_ty = arg_def.annotation or DynType(name="dyn")
            param_ir_ty = self._function_arg_ir_type_or_none(fn, index, ir_arg)
            if param_ir_ty is None:
                param_ir_ty = self._abi_ir_type(target_ty, box_int_abi=False)
            v = self._emit_arg_for_abi_param_with_cleanup(
                ast_arg,
                target_ty,
                param_ir_ty,
                tuple(pinned_arg_temps),
            )
            if (
                not getattr(self, "_freestanding_module", False)
                and not getattr(self, "_module_has_c_abi_export", False)
                and isinstance(v.type, ir.PointerType)
                and v not in getattr(self, "_cpy_values", ())
            ):
                owned = getattr(self, "_last_call_arg_owned_temp", False)
                self._gc_pin(v)
                pinned_arg_temps.append((v, owned))
            args_ir.append(v)
        call_name = (
            ""
            if isinstance(fn.function_type.return_type, ir.VoidType)
            else self._fresh(f"{display_name}_ret")
        )
        result = self._call_user(
            fn,
            args_ir,
            call_name,
            pinned_arg_temps=tuple(pinned_arg_temps),
        )
        for arg_value, owned in pinned_arg_temps:
            self._gc_unpin(arg_value)
            if owned:
                self._gc_release(
                    arg_value,
                    self._release_context_label("direct_call_arg"),
                )
        if self._user_func_returns_cpython(
            ast_func_def,
            runtime_formals,
            resolved_args,
        ):
            self._mark_owned_cpy_value(result)
        return result

    def _call_would_use_callee_defaults(
        self,
        positional: tuple[Expr, ...],
        kwargs_pairs: tuple[tuple[str, Expr], ...],
        formal_args: tuple[Arg, ...],
        *,
        skip_self: bool = False,
    ) -> bool:
        """Best-effort test for calls that would need caller-side
        default filling.

        Cross-module direct calls cannot safely inline default
        expressions when those defaults reference names from the
        callee's module scope. For those cases, callers should fall
        back to the CPython-backed module path instead. This is still
        allowed for simple literal defaults that are safe to materialize
        in the caller."""

        def _safe_default_expr(expr: Expr | None) -> bool:
            if expr is None:
                return True
            return isinstance(
                expr,
                (NoneLit, BoolLit, IntLit, FloatLit, StrLit, BytesLit),
            )

        formals = list(formal_args)
        if skip_self and formals:
            formals = formals[1:]
        formals = [f for f in formals if f.name != ""]
        resolved = [False] * len(formals)
        var_pos_idx = next(
            (i for i, f in enumerate(formals) if f.kind == "*args"),
            None,
        )
        var_kw_idx = next(
            (i for i, f in enumerate(formals) if f.kind == "**kwargs"),
            None,
        )
        pos_formal_indices = [
            i for i, f in enumerate(formals) if f.kind in ("pos", "pos_only")
        ]
        for i, _expr in enumerate(positional):
            if i < len(pos_formal_indices):
                resolved[pos_formal_indices[i]] = True
                continue
            if var_pos_idx is not None:
                continue
            return False
        name_to_idx = {
            f.name: i
            for i, f in enumerate(formals)
            if f.kind not in ("*args", "**kwargs")
        }
        for kw_name, _kw_expr in kwargs_pairs:
            idx = name_to_idx.get(kw_name)
            if idx is None:
                if var_kw_idx is not None:
                    continue
                return False
            if formals[idx].kind == "pos_only" or resolved[idx]:
                return False
            resolved[idx] = True
        for i, formal in enumerate(formals):
            if resolved[i]:
                continue
            if formal.kind in ("*args", "**kwargs"):
                continue
            if formal.has_default:
                if not _safe_default_expr(formal.default):
                    return True
                continue
            return True
        return False
