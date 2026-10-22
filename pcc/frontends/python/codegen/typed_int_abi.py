"""Typed-int ABI safety analysis for Python layer-1 codegen."""

from __future__ import annotations

import os
import sys
from typing import Optional

from pcc.frontends.python.py_ast import Assign, Attr, AugAssign, BinOp, BoolExpr, BoolLit, BoolType, Break, BytesLit, BytesType, ClassDef, ClassType, Call, Compare, Continue, Delete, DictExpr, DictType, DynType, Expr, ExprStmt, FloatLit, FloatType, For, FuncDef, Global, If, IfExpr, Import, ImportFrom, IntLit, IntType, Lambda, ListExpr, ListType, Name, Nonlocal, NoneLit, Pass, Raise, Return, Slice, Stmt, StrLit, StrType, Subscript, TupleExpr, TupleType, Try, Type, UnaryOp, While, With

_ClassDef = ClassDef


def _typed_int_unboxed_abi_mode() -> str:
    mode = os.environ.get("PCC_PYTHON_TYPED_INT_ABI", "auto").strip().lower()
    if mode == "0":
        return "off"
    if mode == "off":
        return "off"
    if mode == "false":
        return "off"
    if mode == "boxed":
        return "off"
    if mode == "unsafe-i64":
        return "unsafe-i64"
    if mode == "unsafe_i64":
        return "unsafe-i64"
    if mode == "raw-i64":
        return "unsafe-i64"
    if mode == "raw_i64":
        return "unsafe-i64"
    if mode == "i64":
        return "unsafe-i64"
    return "auto"


def _typed_int_unboxed_abi_enabled() -> bool:
    return _typed_int_unboxed_abi_mode() != "off"


def _typed_int_abi_debug(func_name: str, reason: str) -> None:
    if not os.environ.get("PCC_DEBUG_TYPED_INT_ABI", "").strip():
        return
    sys.stderr.write("[pcc.typed_int_abi] " + func_name + ": " + reason + "\n")


def _type_is_typed_int_abi_scalar(ty: Type) -> bool:
    return (
        isinstance(ty, IntType)
        or isinstance(ty, BoolType)
        or isinstance(ty, FloatType)
    )


def _type_is_typed_int_abi_param(ty: Type) -> bool:
    if _type_is_typed_int_abi_scalar(ty):
        return True
    return isinstance(ty, ListType) and isinstance(ty.elem, IntType)


def _int_literal_fits_i64(expr: IntLit) -> bool:
    value = int(expr.value)
    return -(1 << 63) <= value <= (1 << 63) - 1


def _expr_is_native_typed_int_shape(expr: Expr) -> bool:
    """Conservative predicate for functions eligible for native int ABI."""
    if isinstance(expr, IntLit):
        return _int_literal_fits_i64(expr)
    if isinstance(expr, FloatLit):
        return True
    if isinstance(expr, BoolLit):
        return True
    if isinstance(expr, Name):
        return (
            isinstance(expr.ty, IntType)
            or isinstance(expr.ty, BoolType)
            or isinstance(expr.ty, FloatType)
        )
    if isinstance(expr, BinOp):
        if isinstance(expr.ty, FloatType):
            if expr.op not in ("+", "-", "*", "/"):
                return False
        else:
            if (
                expr.op != "+"
                and expr.op != "-"
                and expr.op != "*"
                and expr.op != "//"
                and expr.op != "%"
            ):
                return False
            if not isinstance(expr.ty, IntType):
                return False
        return _expr_is_native_typed_int_shape(
            expr.lhs
        ) and _expr_is_native_typed_int_shape(expr.rhs)
    if isinstance(expr, UnaryOp):
        if expr.op != "+" and expr.op != "-" and expr.op != "not":
            return False
        return _expr_is_native_typed_int_shape(expr.operand)
    if isinstance(expr, Compare):
        if (
            expr.op != "=="
            and expr.op != "!="
            and expr.op != "<"
            and expr.op != "<="
            and expr.op != ">"
            and expr.op != ">="
        ):
            return False
        return _expr_is_native_typed_int_shape(
            expr.lhs
        ) and _expr_is_native_typed_int_shape(expr.rhs)
    if isinstance(expr, BoolExpr):
        return _expr_is_native_typed_int_shape(
            expr.left
        ) and _expr_is_native_typed_int_shape(expr.right)
    if isinstance(expr, IfExpr):
        return (
            _expr_is_native_typed_int_shape(expr.cond)
            and _expr_is_native_typed_int_shape(expr.then_e)
            and _expr_is_native_typed_int_shape(expr.else_e)
        )
    if isinstance(expr, Call):
        if expr.kwargs:
            return False
        if not isinstance(expr.func, Name):
            return False
        if not (isinstance(expr.ty, IntType) or isinstance(expr.ty, BoolType)):
            return False
        for arg in expr.args:
            if not _expr_is_native_typed_int_shape(arg):
                return False
        return True
    return False


def _typed_int_expr_elems(expr) -> tuple:
    try:
        elems = expr.elems
    except AttributeError:
        return ()
    if elems is None:
        return ()
    return elems


def _typed_int_expr_pairs(expr) -> tuple:
    try:
        pairs = expr.pairs
    except AttributeError:
        return ()
    if pairs is None:
        return ()
    return pairs


def _stmt_block_is_native_typed_int_shape(stmts: tuple[Stmt, ...]) -> bool:
    for stmt in stmts:
        if (
            isinstance(stmt, Pass)
            or isinstance(stmt, Break)
            or isinstance(stmt, Continue)
        ):
            continue
        if isinstance(stmt, Assign):
            if len(stmt.targets) != 1 or not isinstance(stmt.targets[0], Name):
                return False
            target = stmt.targets[0]
            target_ty = stmt.annotation if stmt.annotation is not None else target.ty
            if not _type_is_typed_int_abi_scalar(target_ty):
                return False
            if not _expr_is_native_typed_int_shape(stmt.value):
                return False
            continue
        if isinstance(stmt, AugAssign):
            if not isinstance(stmt.target, Name):
                return False
            if isinstance(stmt.target.ty, FloatType):
                if stmt.op not in ("+", "-", "*", "/"):
                    return False
            elif (
                stmt.op != "+"
                and stmt.op != "-"
                and stmt.op != "*"
                and stmt.op != "//"
                and stmt.op != "%"
            ):
                return False
            if not _type_is_typed_int_abi_scalar(stmt.target.ty):
                return False
            if not _expr_is_native_typed_int_shape(stmt.value):
                return False
            continue
        if isinstance(stmt, Return):
            if stmt.value is None:
                return False
            if not (
                isinstance(stmt.value.ty, IntType)
                or isinstance(stmt.value.ty, BoolType)
                or isinstance(stmt.value.ty, FloatType)
            ):
                return False
            if not _expr_is_native_typed_int_shape(stmt.value):
                return False
            continue
        if isinstance(stmt, If):
            if not _expr_is_native_typed_int_shape(stmt.cond):
                return False
            if not _stmt_block_is_native_typed_int_shape(stmt.body):
                return False
            if not _stmt_block_is_native_typed_int_shape(stmt.else_body):
                return False
            continue
        if isinstance(stmt, While):
            if not _expr_is_native_typed_int_shape(stmt.cond):
                return False
            if not _stmt_block_is_native_typed_int_shape(stmt.body):
                return False
            if not _stmt_block_is_native_typed_int_shape(stmt.else_body):
                return False
            continue
        return False
    return True


class TypedIntAbiMixin:
    def _funcdef_uses_unboxed_typed_int_abi(self, fd: FuncDef) -> bool:
        # Keep this as direct field checks. A previous dict cache used
        # Optional[bool] sentinel logic and pcc1 miscompiled the None test,
        # silently forcing typed-int functions back to the boxed ABI.
        mode = _typed_int_unboxed_abi_mode()
        bounded_proof = False
        for bounded_name in self._bounded_int_abi_function_names:
            if bounded_name == fd.name and not self._python_library:
                bounded_proof = True
                break
        result = False
        if mode != "off":
            result = True
            if fd.is_async:
                result = False
            if fd.is_method:
                result = False
            if len(fd.decorators) != 0:
                result = False
            if mode == "unsafe-i64" or bounded_proof:
                if not isinstance(fd.return_ty, (IntType, FloatType)):
                    result = False
            else:
                if not isinstance(fd.return_ty, FloatType):
                    result = False
            if result:
                for arg in fd.args:
                    if arg.name == "":
                        continue
                    if (
                        arg.kind != "pos"
                        and arg.kind != "pos_only"
                        and arg.kind != "kw_only"
                    ):
                        result = False
                        break
                    if mode == "unsafe-i64" or bounded_proof:
                        if not _type_is_typed_int_abi_param(arg.annotation):
                            result = False
                            break
                    else:
                        if not isinstance(arg.annotation, FloatType):
                            result = False
                            break
                    if arg.default is not None:
                        if not _type_is_typed_int_abi_scalar(arg.annotation):
                            result = False
                            break
                        if not _expr_is_native_typed_int_shape(arg.default):
                            result = False
                            break
            if (
                (mode == "unsafe-i64" or bounded_proof)
                and result
                and self._typed_int_abi_call_arg_safety
            ):
                arg_safety = self._typed_int_call_safety_for_name(
                    self._typed_int_abi_call_arg_safety,
                    fd.name,
                )
                if arg_safety is None:
                    result = False
                    _typed_int_abi_debug(
                        fd.name, "missing typed-int call arg safety record"
                    )
                else:
                    if len(arg_safety) != len(fd.args):
                        result = False
                        _typed_int_abi_debug(
                            fd.name,
                            "typed-int arg safety length mismatch",
                        )
                    else:
                        for i, arg in enumerate(fd.args):
                            if arg.name == "" or arg.kind not in (
                                "pos",
                                "pos_only",
                                "kw_only",
                            ):
                                continue
                            if not (
                                isinstance(arg.annotation, IntType)
                                or isinstance(arg.annotation, BoolType)
                            ):
                                continue
                            if not arg_safety[i]:
                                result = False
                                _typed_int_abi_debug(
                                    fd.name,
                                    "arg " + arg.name + " not i64-safe",
                                )
                                break
        return result

    def _typed_int_func_for_name(
        self,
        typed_int_funcs: list[tuple[str, FuncDef]],
        name: str,
    ) -> Optional[FuncDef]:
        for func_name, fd in typed_int_funcs:
            if func_name == name:
                return fd
        return None

    def _typed_int_call_safety_for_name(
        self,
        call_safety: list[tuple[str, list[bool]]],
        name: str,
    ) -> Optional[list[bool]]:
        for func_name, safety in call_safety:
            if func_name == name:
                return safety
        return None

    def _typed_int_env_get(
        self,
        env: list[tuple[str, bool]],
        name: str,
        default: bool,
    ) -> bool:
        for key, value in env:
            if key == name:
                return value
        return default

    def _typed_int_env_set(
        self,
        env: list[tuple[str, bool]],
        name: str,
        value: bool,
    ) -> None:
        i = 0
        while i < len(env):
            key, _old_value = env[i]
            if key == name:
                env[i] = (name, value)
                return
            i += 1
        env.append((name, value))

    def _typed_int_env_copy(
        self,
        env: list[tuple[str, bool]],
    ) -> list[tuple[str, bool]]:
        out: list[tuple[str, bool]] = []
        for item in env:
            out.append(item)
        return out

    def _typed_int_env_add_key(
        self,
        keys: list[str],
        name: str,
    ) -> None:
        for key in keys:
            if key == name:
                return
        keys.append(name)

    def _typed_int_true_flags(self, count: int) -> list[bool]:
        flags: list[bool] = []
        i = 0
        while i < count:
            flags.append(True)
            i += 1
        return flags

    def _compute_typed_int_abi_call_arg_safety(self) -> list[tuple[str, list[bool]]]:
        typed_int_funcs: list[tuple[str, FuncDef]] = []
        for stmt in self.ast_module.body:
            if not isinstance(stmt, FuncDef):
                continue
            if not self._funcdef_uses_unboxed_typed_int_abi(stmt):
                continue
            typed_int_funcs.append((stmt.name, stmt))
        if not typed_int_funcs:
            return []

        call_safety: list[tuple[str, list[bool]]] = []
        for name, fd in typed_int_funcs:
            call_safety.append((name, self._typed_int_true_flags(len(fd.args))))

        # Analyze top-level statements so assignments like
        # ``x = 2 ** 100; add_one(x)`` are visible at the next
        # call site.
        module_env: list[tuple[str, bool]] = []
        for stmt in self.ast_module.body:
            if isinstance(stmt, FuncDef):
                local_env: list[tuple[str, bool]] = []
                fn_uses_typed_int_abi = (
                    self._typed_int_func_for_name(typed_int_funcs, stmt.name)
                    is not None
                )
                for arg in stmt.args:
                    if arg.name != "" and arg.kind in ("pos", "pos_only", "kw_only"):
                        local_env.append((arg.name, fn_uses_typed_int_abi))
                self._collect_typed_int_abi_call_safety_stmts(
                    stmt.body,
                    typed_int_funcs,
                    call_safety,
                    local_env,
                )
                continue
            if isinstance(stmt, _ClassDef):
                continue
            self._collect_typed_int_abi_call_safety_stmt(
                stmt,
                typed_int_funcs,
                call_safety,
                module_env,
            )
        return call_safety

    def _collect_typed_int_abi_call_safety_stmts(
        self,
        stmts: tuple[Stmt, ...],
        typed_int_funcs: list[tuple[str, FuncDef]],
        call_safety: list[tuple[str, list[bool]]],
        env: list[tuple[str, bool]],
    ) -> None:
        for stmt in stmts:
            self._collect_typed_int_abi_call_safety_stmt(
                stmt,
                typed_int_funcs,
                call_safety,
                env,
            )

    def _collect_typed_int_abi_call_safety_stmt(
        self,
        stmt: Stmt,
        typed_int_funcs: list[tuple[str, FuncDef]],
        call_safety: list[tuple[str, list[bool]]],
        env: list[tuple[str, bool]],
    ) -> None:
        if isinstance(stmt, Assign):
            self._collect_typed_int_abi_expr(
                stmt.value,
                typed_int_funcs,
                call_safety,
                env,
            )
            value_safe = self._typed_int_expr_is_i64_safe(
                stmt.value,
                env,
            )
            for target in stmt.targets:
                if isinstance(target, Name) and target.ident != "":
                    self._typed_int_env_set(env, target.ident, value_safe)
            return
        if isinstance(stmt, AugAssign):
            self._collect_typed_int_abi_expr(
                stmt.target,
                typed_int_funcs,
                call_safety,
                env,
            )
            self._collect_typed_int_abi_expr(
                stmt.value,
                typed_int_funcs,
                call_safety,
                env,
            )
            if isinstance(stmt.target, Name) and stmt.target.ident != "":
                prev = self._typed_int_env_get(env, stmt.target.ident, False)
                self._typed_int_env_set(
                    env,
                    stmt.target.ident,
                    prev
                    and self._typed_int_expr_is_i64_safe(
                        stmt.value,
                        env,
                    ),
                )
            return
        if isinstance(stmt, ExprStmt):
            self._collect_typed_int_abi_expr(
                stmt.expr,
                typed_int_funcs,
                call_safety,
                env,
            )
            return
        if isinstance(stmt, Return):
            if stmt.value is not None:
                self._collect_typed_int_abi_expr(
                    stmt.value,
                    typed_int_funcs,
                    call_safety,
                    env,
                )
            return
        if isinstance(stmt, If):
            self._collect_typed_int_abi_expr(
                stmt.cond,
                typed_int_funcs,
                call_safety,
                env,
            )
            then_env = self._typed_int_env_copy(env)
            self._collect_typed_int_abi_call_safety_stmts(
                stmt.body,
                typed_int_funcs,
                call_safety,
                then_env,
            )
            else_env = self._typed_int_env_copy(env)
            self._collect_typed_int_abi_call_safety_stmts(
                stmt.else_body,
                typed_int_funcs,
                call_safety,
                else_env,
            )
            self._merge_typed_int_abi_call_arg_safety_env(env, then_env, else_env)
            return
        if isinstance(stmt, While):
            self._collect_typed_int_abi_expr(
                stmt.cond,
                typed_int_funcs,
                call_safety,
                env,
            )
            loop_env = self._typed_int_env_copy(env)
            self._collect_typed_int_abi_call_safety_stmts(
                stmt.body,
                typed_int_funcs,
                call_safety,
                loop_env,
            )
            self._merge_typed_int_abi_call_arg_safety_env(env, loop_env, [])
            self._collect_typed_int_abi_call_safety_stmts(
                stmt.else_body,
                typed_int_funcs,
                call_safety,
                env,
            )
            return
        if isinstance(stmt, For):
            self._collect_typed_int_abi_expr(
                stmt.iter,
                typed_int_funcs,
                call_safety,
                env,
            )
            self._collect_typed_int_abi_expr(
                stmt.target,
                typed_int_funcs,
                call_safety,
                env,
            )
            loop_env = self._typed_int_env_copy(env)
            if isinstance(stmt.target, Name) and stmt.target.ident != "":
                self._typed_int_env_set(loop_env, stmt.target.ident, False)
            self._collect_typed_int_abi_call_safety_stmts(
                stmt.body,
                typed_int_funcs,
                call_safety,
                loop_env,
            )
            self._merge_typed_int_abi_call_arg_safety_env(env, loop_env, [])
            self._collect_typed_int_abi_call_safety_stmts(
                stmt.else_body,
                typed_int_funcs,
                call_safety,
                env,
            )
            return
        if isinstance(stmt, Try):
            self._collect_typed_int_abi_call_safety_stmts(
                stmt.body,
                typed_int_funcs,
                call_safety,
                env,
            )
            for handler in stmt.handlers:
                self._collect_typed_int_abi_call_safety_stmts(
                    handler.body,
                    typed_int_funcs,
                    call_safety,
                    env,
                )
            self._collect_typed_int_abi_call_safety_stmts(
                stmt.else_body,
                typed_int_funcs,
                call_safety,
                env,
            )
            self._collect_typed_int_abi_call_safety_stmts(
                stmt.finally_body,
                typed_int_funcs,
                call_safety,
                env,
            )
            return
        if isinstance(stmt, With):
            self._collect_typed_int_abi_call_safety_stmts(
                stmt.body,
                typed_int_funcs,
                call_safety,
                env,
            )

    def _merge_typed_int_abi_call_arg_safety_env(
        self,
        base: list[tuple[str, bool]],
        then_env: list[tuple[str, bool]],
        else_env: list[tuple[str, bool]],
    ) -> None:
        keys: list[str] = []
        for key, _value in base:
            self._typed_int_env_add_key(keys, key)
        for key, _value in then_env:
            self._typed_int_env_add_key(keys, key)
        for key, _value in else_env:
            self._typed_int_env_add_key(keys, key)
        for key in keys:
            base_v = self._typed_int_env_get(base, key, False)
            then_v = self._typed_int_env_get(then_env, key, base_v)
            else_v = self._typed_int_env_get(else_env, key, base_v)
            self._typed_int_env_set(base, key, then_v and else_v)

    def _collect_typed_int_abi_expr(
        self,
        expr: Expr,
        typed_int_funcs: list[tuple[str, FuncDef]],
        call_safety: list[tuple[str, list[bool]]],
        env: list[tuple[str, bool]],
    ) -> None:
        if isinstance(expr, Call):
            self._record_typed_int_abi_call_arg_safety(
                expr,
                typed_int_funcs,
                call_safety,
                env,
            )
            self._collect_typed_int_abi_expr(
                expr.func,
                typed_int_funcs,
                call_safety,
                env,
            )
            for arg in expr.args:
                self._collect_typed_int_abi_expr(
                    arg,
                    typed_int_funcs,
                    call_safety,
                    env,
                )
            for _key, value in expr.kwargs:
                self._collect_typed_int_abi_expr(
                    value,
                    typed_int_funcs,
                    call_safety,
                    env,
                )
            return
        if isinstance(expr, BinOp):
            self._collect_typed_int_abi_expr(
                expr.lhs, typed_int_funcs, call_safety, env
            )
            self._collect_typed_int_abi_expr(
                expr.rhs, typed_int_funcs, call_safety, env
            )
            return
        if isinstance(expr, UnaryOp):
            self._collect_typed_int_abi_expr(
                expr.operand,
                typed_int_funcs,
                call_safety,
                env,
            )
            return
        if isinstance(expr, Compare):
            self._collect_typed_int_abi_expr(
                expr.lhs,
                typed_int_funcs,
                call_safety,
                env,
            )
            self._collect_typed_int_abi_expr(
                expr.rhs,
                typed_int_funcs,
                call_safety,
                env,
            )
            return
        if isinstance(expr, BoolExpr):
            self._collect_typed_int_abi_expr(
                expr.left,
                typed_int_funcs,
                call_safety,
                env,
            )
            self._collect_typed_int_abi_expr(
                expr.right,
                typed_int_funcs,
                call_safety,
                env,
            )
            return
        if isinstance(expr, Attr):
            self._collect_typed_int_abi_expr(
                expr.obj, typed_int_funcs, call_safety, env
            )
            return
        if isinstance(expr, Subscript):
            self._collect_typed_int_abi_expr(
                expr.obj, typed_int_funcs, call_safety, env
            )
            self._collect_typed_int_abi_expr(
                expr.idx, typed_int_funcs, call_safety, env
            )
            return
        if isinstance(expr, ListExpr):
            elems = _typed_int_expr_elems(expr)
            item_index = 0
            while item_index < len(elems):
                item = elems[item_index]
                self._collect_typed_int_abi_expr(
                    item,
                    typed_int_funcs,
                    call_safety,
                    env,
                )
                item_index += 1
            return
        if isinstance(expr, TupleExpr):
            elems = _typed_int_expr_elems(expr)
            item_index = 0
            while item_index < len(elems):
                item = elems[item_index]
                self._collect_typed_int_abi_expr(
                    item,
                    typed_int_funcs,
                    call_safety,
                    env,
                )
                item_index += 1
            return
        if isinstance(expr, DictExpr):
            pairs = _typed_int_expr_pairs(expr)
            pair_index = 0
            while pair_index < len(pairs):
                key, value = pairs[pair_index]
                self._collect_typed_int_abi_expr(
                    key,
                    typed_int_funcs,
                    call_safety,
                    env,
                )
                self._collect_typed_int_abi_expr(
                    value,
                    typed_int_funcs,
                    call_safety,
                    env,
                )
                pair_index += 1
            return
        if isinstance(expr, IfExpr):
            self._collect_typed_int_abi_expr(
                expr.cond,
                typed_int_funcs,
                call_safety,
                env,
            )
            self._collect_typed_int_abi_expr(
                expr.then_e,
                typed_int_funcs,
                call_safety,
                env,
            )
            self._collect_typed_int_abi_expr(
                expr.else_e,
                typed_int_funcs,
                call_safety,
                env,
            )
            return
        if isinstance(expr, Lambda):
            self._collect_typed_int_abi_expr(
                expr.body,
                typed_int_funcs,
                call_safety,
                env,
            )
            return

    def _record_typed_int_abi_call_arg_safety(
        self,
        expr: Call,
        typed_int_funcs: list[tuple[str, FuncDef]],
        call_safety: list[tuple[str, list[bool]]],
        env: list[tuple[str, bool]],
    ) -> None:
        if not isinstance(expr.func, Name):
            return
        target_fn = self._typed_int_func_for_name(
            typed_int_funcs,
            expr.func.ident,
        )
        if target_fn is None:
            return
        target_flags = self._typed_int_call_safety_for_name(
            call_safety,
            expr.func.ident,
        )
        if target_flags is None:
            return
        params = target_fn.args
        # Any non-static call shape should avoid unboxed lowering.
        if len(expr.args) > len(params):
            for idx, arg in enumerate(params):
                if isinstance(arg.annotation, (IntType, BoolType)):
                    target_flags[idx] = False
            return
        for i, arg_expr in enumerate(expr.args):
            if i >= len(params):
                return
            arg = params[i]
            if (
                arg.name == ""
                or arg.kind not in ("pos", "pos_only", "kw_only")
                or not isinstance(arg.annotation, (IntType, BoolType))
            ):
                continue
            target_flags[i] = target_flags[i] and self._typed_int_expr_is_i64_safe(
                arg_expr,
                env,
            )
        for kw_name, kw_value in expr.kwargs:
            for i, arg in enumerate(params):
                if not isinstance(arg.annotation, (IntType, BoolType)):
                    continue
                if arg.name != kw_name:
                    continue
                if arg.kind not in ("pos", "kw_only"):
                    target_flags[i] = False
                    continue
                target_flags[i] = target_flags[i] and self._typed_int_expr_is_i64_safe(
                    kw_value,
                    env,
                )
                break
            else:
                # Unknown keyword argument: disable all typed-int params to stay
                # on the safe side.
                for j, arg in enumerate(target_fn.args):
                    if isinstance(arg.annotation, (IntType, BoolType)):
                        target_flags[j] = False

    def _typed_int_expr_is_i64_safe(
        self,
        expr: Expr,
        env: list[tuple[str, bool]],
    ) -> bool:
        if isinstance(expr, IntLit):
            return _int_literal_fits_i64(expr)
        if isinstance(expr, BoolLit):
            return True
        if isinstance(expr, Name):
            return self._typed_int_env_get(env, expr.ident, False)
        if isinstance(expr, UnaryOp):
            if expr.op not in ("+", "-", "~"):
                return False
            return self._typed_int_expr_is_i64_safe(expr.operand, env)
        if isinstance(expr, Compare):
            return self._typed_int_expr_is_i64_safe(
                expr.lhs,
                env,
            ) and self._typed_int_expr_is_i64_safe(expr.rhs, env)
        if isinstance(expr, BoolExpr):
            return self._typed_int_expr_is_i64_safe(
                expr.left,
                env,
            ) and self._typed_int_expr_is_i64_safe(expr.right, env)
        if isinstance(expr, BinOp):
            if expr.op not in ("+", "-", "*", "//", "%", "&", "|", "^", "<<", ">>"):
                return False
            return self._typed_int_expr_is_i64_safe(
                expr.lhs,
                env,
            ) and self._typed_int_expr_is_i64_safe(
                expr.rhs,
                env,
            )
        if isinstance(expr, IfExpr):
            return (
                self._typed_int_expr_is_i64_safe(expr.cond, env)
                and self._typed_int_expr_is_i64_safe(expr.then_e, env)
                and self._typed_int_expr_is_i64_safe(expr.else_e, env)
            )
        if isinstance(expr, Subscript):
            if isinstance(expr.idx, Slice):
                return self._typed_int_expr_is_i64_safe(expr.obj, env)
            return self._typed_int_expr_is_i64_safe(
                expr.obj,
                env,
            ) and self._typed_int_expr_is_i64_safe(expr.idx, env)
        if isinstance(expr, Attr):
            return self._typed_int_expr_is_i64_safe(expr.obj, env)
        if isinstance(expr, ListExpr):
            elems = _typed_int_expr_elems(expr)
            item_index = 0
            while item_index < len(elems):
                item = elems[item_index]
                if not self._typed_int_expr_is_i64_safe(item, env):
                    return False
                item_index += 1
            return True
        if isinstance(expr, TupleExpr):
            elems = _typed_int_expr_elems(expr)
            item_index = 0
            while item_index < len(elems):
                item = elems[item_index]
                if not self._typed_int_expr_is_i64_safe(item, env):
                    return False
                item_index += 1
            return True
        if isinstance(expr, DictExpr):
            pairs = _typed_int_expr_pairs(expr)
            pair_index = 0
            while pair_index < len(pairs):
                key, value = pairs[pair_index]
                if not self._typed_int_expr_is_i64_safe(key, env):
                    return False
                if not self._typed_int_expr_is_i64_safe(value, env):
                    return False
                pair_index += 1
            return True
        if isinstance(expr, Lambda):
            for arg in expr.params:
                if not isinstance(arg.annotation, (IntType, BoolType)):
                    return False
            return self._typed_int_expr_is_i64_safe(expr.body, env)
        if isinstance(expr, Call):
            return False
        return False


__all__ = ["TypedIntAbiMixin"]
