"""Numeric and aggregate builtin lowering helpers for L1CodeGen."""
from __future__ import annotations

import os
import sys
from typing import Optional

from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import (
    BoolType,
    ByteArrayType,
    BytesType,
    Call,
    ClassType,
    ComplexType,
    DynType,
    FloatType,
    IntType,
    Lambda,
    ListType,
    MemoryViewType,
    Name,
    StrType,
    TupleExpr,
    TupleType,
)
from pcc.frontends.python.codegen import marshal
from pcc.frontends.python.codegen.freestanding_abi_constants import PY_TYPE_BOOL, PY_TYPE_FLOAT, PY_TYPE_STR


_I1 = ir.IntType(1)
_I32 = ir.IntType(32)
_I64 = ir.IntType(64)
_CSTR = ir.IntType(8).as_pointer()
_DOUBLE = ir.DoubleType()


def _is_class_type_for_numeric_builtin(ty) -> bool:
    if isinstance(ty, ClassType):
        return True
    return type(ty).__name__ in ("ClassType", "ValueClassType")


class NumericBuiltinLoweringMixin:
    def emit_int_builtin_as_object(self, expr) -> Optional[ir.Value]:
        """Produce int's object projection before any lossy scalar conversion.

        The ordinary lowering parses to an object and then unboxes it to satisfy
        this builtin's i64 contract, which truncates anything above 2**63-1 to
        0.  Callers that want the object projection ask for it here instead, so
        no re-box has to recover a value that was already destroyed.

        String and dynamic conversions return NEW runtime references, recorded
        in the ownership ledger. An already-integer argument keeps its own
        projection and ownership; callers must not treat that borrow as new.
        """
        if len(expr.args) not in (1, 2):
            return None
        arg = expr.args[0]
        if len(expr.args) == 2 or isinstance(arg.ty, (DynType, BytesType, ByteArrayType, MemoryViewType)):
            # Evaluate in Python order into updateable owners, then acquire
            # counted address leases before exposing raw runtime ABI copies.
            # Base.__index__ may allocate, relocate or clear Boolean pins.
            roots = []
            leases = []
            previous = self._current_try_err_block()
            previous_cpy = getattr(self, "_cpy_operand_cleanup_block", None)
            target = previous if previous is not None else self._ensure_fn_err_exit()
            try:
                for operand in expr.args:
                    cleanup = self._extern_cleanup_block(tuple(roots), target)
                    self._try_err_block = cleanup
                    self._cpy_operand_cleanup_block = cleanup
                    value = self._emit_expr_with_cpy_operand_cleanup(
                        operand, (), as_pcc_object=True,
                    )
                    roots.append(self._extern_enter_root(
                        value, self._owned_release_needed(value, operand), "int.argument.root",
                    ))
                for root in roots:
                    acquired = self.builder.call(
                        self.runtime["pcc_gc_foreign_lease_acquire"],
                        [self._as_gc_ptr(root[0])], name=self._fresh("int.lease.acquire"),
                    )
                    failed = self.builder.icmp_signed("<", acquired, ir.Constant(_I64, 0))
                    error = self.current_function.append_basic_block(self._fresh("int.lease.error"))
                    ready = self.current_function.append_basic_block(self._fresh("int.lease.ready"))
                    self.builder.cbranch(failed, error, ready)
                    self.builder.position_at_end(error)
                    cleanup = self._extern_cleanup_block(tuple(roots), target, tuple(leases))
                    self._try_err_block = cleanup
                    self._cpy_operand_cleanup_block = cleanup
                    self._emit_builtin_exception_and_branch(
                        "RuntimeError", "integer conversion requires a stable managed owner", expr.span,
                    )
                    self.builder.position_at_end(ready)
                    leases.append((root, acquired))
                cleanup = self._extern_cleanup_block(tuple(roots), target, tuple(leases))
                self._try_err_block = cleanup
                self._cpy_operand_cleanup_block = cleanup
                obj = self._extern_load_root(roots[0])
                base = (self._extern_load_root(roots[1])
                        if len(roots) == 2 else ir.Constant(_CSTR, None))
                result = self.builder.call(
                    self.runtime["py_obj_as_int_object_args"], [obj, base],
                    name=self._fresh("int.obj.args"),
                )
                self._emit_post_call_err_check(getattr(expr, "span", None))
                result_root = self._extern_enter_root(result, True, "int.result.root")
                prior = result_root[2]
                for root in reversed(roots):
                    alias = self.builder.icmp_unsigned("==", result, self._extern_load_root(root))
                    prior = self.builder.select(alias, root[2], prior)
                result_root = (result_root[0], True, prior)
            finally:
                self._try_err_block = previous
                self._cpy_operand_cleanup_block = previous_cpy
            failed = self._extern_release_foreign_leases(tuple(leases))
            self._extern_check_lease_cleanup(failed, tuple(roots) + (result_root,))
            self._extern_release_roots(tuple(roots))
            result = self._extern_take_root(result_root)
            self._note_owned_object_value(result)
            return result
        if len(expr.args) == 2:
            base_val = self._emit_expr_as_i64(expr.args[1])
            base_val = self.builder.trunc(
                base_val, _I32, name=self._fresh("int.obj.base")
            )
        else:
            base_val = ir.Constant(_I32, 10)
        if isinstance(arg.ty, IntType):
            # `int(<already an int>)` is the identity, so hand back the argument's
            # OBJECT projection.  Without this the argument goes through
            # `_emit_expr` -> i64 and a bignum is truncated -- which is the hop
            # that still lost pcc1's own parse, where `IntLit.value` is re-wrapped
            # as `int(expr.value)`.  No basic blocks are created here.
            return self._maybe_emit_exact_int_object(arg)
        if isinstance(arg.ty, DynType):
            # ONE call, no basic blocks.  The runtime does the four-way dispatch
            # (`py_obj_as_int_object`), because building the dispatch here means
            # creating blocks and a phi inside `_maybe_emit_exact_int_object` --
            # a probe whose result callers may discard, which orphans the blocks
            # and leaves the builder parked on the join.  That shape compiled
            # every small host program and still broke stage1.
            obj = self._emit_expr(arg)
            if not isinstance(obj.type, ir.PointerType):
                return None
            got = self.builder.call(
                self.runtime["py_obj_as_int_object"],
                [obj, base_val],
                name=self._fresh("int.obj.dyn"),
            )
            self._emit_post_call_err_check(getattr(expr, "span", None))
            self._note_owned_dynamic_call_value(got)
            return got
        if isinstance(arg.ty, FloatType) and len(expr.args) == 1:
            # Exact: int(1e20) is a bignum, NaN/inf raise CPython's errors.
            value = self._f64_from_emitted(self._emit_expr(arg), arg)
            got = self.builder.call(
                self.runtime["py_int_from_f64_exact"],
                [value],
                name=self._fresh("int.obj.float"),
            )
            self._emit_post_call_err_check(getattr(expr, "span", None))
            self._note_owned_dynamic_call_value(got)
            return got
        if not isinstance(arg.ty, StrType):
            return None
        s_obj = self._emit_expr(arg)
        cstr = self.builder.call(
            self.runtime["py_str_utf8"],
            [s_obj],
            name=self._fresh("int.obj.cstr"),
        )
        boxed = self.builder.call(
            self.runtime["py_int_from_cstr_or_raise"],
            [cstr, base_val],
            name=self._fresh("int.obj.parse"),
        )
        self._emit_post_call_err_check(getattr(expr, "span", None))
        self._note_owned_dynamic_call_value(boxed)
        return boxed

    def _emit_float_to_i64_lane(self, v: ir.Value, expr) -> ir.Value:
        """``int(<float>)`` into the i64 lane.

        A bare ``fptosi`` saturated ``int(1e20)`` and read NaN as 0.  The
        in-range case stays one compare pair and an ``fptosi``; NaN, the
        infinities and |x| >= 2**63 take a runtime call that raises CPython's
        error (the lane cannot hold the exact result; the object projection,
        ``emit_int_builtin_as_object``, returns it).
        """
        fn = self.builder.function
        in_low = self.builder.fcmp_ordered(
            ">=", v, ir.Constant(_DOUBLE, -9223372036854775808.0),
            name=self._fresh("int.f.lo"),
        )
        in_high = self.builder.fcmp_ordered(
            "<", v, ir.Constant(_DOUBLE, 9223372036854775808.0),
            name=self._fresh("int.f.hi"),
        )
        in_range = self.builder.and_(in_low, in_high, name=self._fresh("int.f.ok"))
        fast_bb = fn.append_basic_block(name=self._fresh("int.f.fast"))
        slow_bb = fn.append_basic_block(name=self._fresh("int.f.slow"))
        join_bb = fn.append_basic_block(name=self._fresh("int.f.join"))
        self.builder.cbranch(in_range, fast_bb, slow_bb)
        self.builder.position_at_end(fast_bb)
        fast = self.builder.fptosi(v, _I64, name=self._fresh("int.from_float"))
        fast_end = self.builder.block
        self.builder.branch(join_bb)
        self.builder.position_at_end(slow_bb)
        slow = self.builder.call(
            self.runtime["py_float_to_i64_checked"],
            [v],
            name=self._fresh("int.from_float.checked"),
        )
        self._emit_post_call_err_check(getattr(expr, "span", None))
        slow_end = self.builder.block
        self.builder.branch(join_bb)
        self.builder.position_at_end(join_bb)
        result = self.builder.phi(_I64, name=self._fresh("int.from_float.join"))
        result.add_incoming(fast, fast_end)
        result.add_incoming(slow, slow_end)
        return result

    def _emit_int_dyn_as_object(self, expr, arg) -> Optional[ir.Value]:
        """``int(<dyn>)`` as an OBJECT: dispatch on the tag, phi the objects.

        The i64-returning form unboxes all four branches and phis them as i64,
        which destroys a bignum.  Carrying an object phi *there* was tried and
        leaked: the objects are built eagerly and most callers only want the
        i64, so nothing consumes them.  Here the caller asked for the object, so
        the join is always consumed and every branch can hand back an OWNED
        reference -- the str branch's parse result already is one, the int branch
        retains its borrowed input, and float/bool box their exact i64.
        """
        obj = self._emit_expr(arg)
        if not isinstance(obj.type, ir.PointerType):
            return None
        if len(expr.args) == 2:
            base_val = self._emit_expr_as_i64(expr.args[1])
            base_val = self.builder.trunc(
                base_val, _I32, name=self._fresh("int.dynobj.base")
            )
        else:
            base_val = ir.Constant(_I32, 10)
        fn = self.builder.function
        tag = self.builder.call(
            self.runtime["py_obj_type_tag"], [obj], name=self._fresh("int.dynobj.tag")
        )
        str_bb = fn.append_basic_block(name=self._fresh("int.dynobj.str"))
        non_str_bb = fn.append_basic_block(name=self._fresh("int.dynobj.non_str"))
        float_bb = fn.append_basic_block(name=self._fresh("int.dynobj.float"))
        non_float_bb = fn.append_basic_block(name=self._fresh("int.dynobj.non_float"))
        bool_bb = fn.append_basic_block(name=self._fresh("int.dynobj.bool"))
        int_bb = fn.append_basic_block(name=self._fresh("int.dynobj.int"))
        join_bb = fn.append_basic_block(name=self._fresh("int.dynobj.join"))

        self.builder.cbranch(
            self.builder.icmp_signed(
                "==", tag, ir.Constant(_I64, PY_TYPE_STR),
                name=self._fresh("int.dynobj.is_str"),
            ),
            str_bb, non_str_bb,
        )

        self.builder.position_at_end(str_bb)
        cstr = self.builder.call(
            self.runtime["py_str_utf8"], [obj], name=self._fresh("int.dynobj.cstr")
        )
        str_obj = self.builder.call(
            self.runtime["py_int_from_cstr_or_raise"],
            [cstr, base_val],
            name=self._fresh("int.dynobj.parse"),
        )
        self._emit_post_call_err_check(getattr(expr, "span", None))
        str_exit = self.builder._block
        self.builder.branch(join_bb)

        self.builder.position_at_end(non_str_bb)
        self.builder.cbranch(
            self.builder.icmp_signed(
                "==", tag, ir.Constant(_I64, PY_TYPE_FLOAT),
                name=self._fresh("int.dynobj.is_float"),
            ),
            float_bb, non_float_bb,
        )

        self.builder.position_at_end(float_bb)
        f64 = self.builder.call(
            self.runtime["py_float_to_f64"], [obj], name=self._fresh("int.dynobj.f64")
        )
        float_obj = self.builder.call(
            self.runtime["py_int_from_i64"],
            [self.builder.fptosi(f64, _I64, name=self._fresh("int.dynobj.trunc"))],
            name=self._fresh("int.dynobj.float.obj"),
        )
        float_exit = self.builder._block
        self.builder.branch(join_bb)

        self.builder.position_at_end(non_float_bb)
        self.builder.cbranch(
            self.builder.icmp_signed(
                "==", tag, ir.Constant(_I64, PY_TYPE_BOOL),
                name=self._fresh("int.dynobj.is_bool"),
            ),
            bool_bb, int_bb,
        )

        self.builder.position_at_end(bool_bb)
        bool_obj = self.builder.call(
            self.runtime["py_int_from_i64"],
            [self.builder.call(
                self.runtime["py_obj_truthy"], [obj],
                name=self._fresh("int.dynobj.truthy"),
            )],
            name=self._fresh("int.dynobj.bool.obj"),
        )
        bool_exit = self.builder._block
        self.builder.branch(join_bb)

        self.builder.position_at_end(int_bb)
        self.builder.call(self.runtime["py_incref"], [obj])
        int_exit = self.builder._block
        self.builder.branch(join_bb)

        self.builder.position_at_end(join_bb)
        phi = self.builder.phi(_CSTR, name=self._fresh("int.dynobj"))
        phi.add_incoming(str_obj, str_exit)
        phi.add_incoming(float_obj, float_exit)
        phi.add_incoming(bool_obj, bool_exit)
        phi.add_incoming(obj, int_exit)
        self._note_owned_dynamic_call_value(phi)
        return phi

    def _maybe_emit_int_builtin(self, expr: Call) -> Optional[ir.Value]:
        """``int(x)`` / ``int(s, base)``:

        - int argument → identity (already int).
        - bool argument → ``zext`` to i64.
        - float argument → ``fptosi``.
        - str argument → ``py_int_from_cstr(utf8, base)`` then unbox.
        - class instance → call ``__int__`` and unbox the result.
        Returns None for unsupported shapes so the caller errors.
        """
        arg = expr.args[0]
        arg_ty = arg.ty
        if len(expr.args) == 2 or isinstance(arg_ty, (DynType, BytesType, ByteArrayType, MemoryViewType)):
            boxed = self.emit_int_builtin_as_object(expr)
            return marshal.marshal_from_object(
                self.builder, self.module, self.runtime, boxed, IntType(name="int"),
            )
        base_val: ir.Value
        if len(expr.args) == 2:
            base_val = self._emit_expr_as_i64(expr.args[1])
            base_val = self.builder.trunc(
                base_val,
                _I32,
                name=self._fresh("int.base"),
            )
        else:
            base_val = ir.Constant(_I32, 10)
        if isinstance(arg_ty, IntType):
            return self._emit_expr(arg)
        if isinstance(arg_ty, BoolType):
            v = self._emit_expr(arg)
            if self._ir_type_matches(v.type, _I1):
                return self.builder.zext(
                    v,
                    _I64,
                    name=self._fresh("int.from_bool"),
                )
            return v
        if isinstance(arg_ty, FloatType):
            v = self._emit_expr(arg)
            return self._emit_float_to_i64_lane(v, expr)
        if isinstance(arg_ty, StrType):
            s_obj = self._emit_expr(arg)
            cstr = self.builder.call(
                self.runtime["py_str_utf8"],
                [s_obj],
                name=self._fresh("int.cstr"),
            )
            boxed = self.builder.call(
                self.runtime["py_int_from_cstr_or_raise"],
                [cstr, base_val],
                name=self._fresh("int.parse"),
            )
            # py_int_from_cstr_or_raise raises ValueError on invalid input
            # (py_int_from_cstr returned NULL, which would otherwise unbox to 0
            # -> int('xyz') silently became 0). It raises the CPython-accurate
            # message from the (string, base) it receives: either
            # "int() base must be >= 2 and <= 36, or 0" for a bad base, or
            # "invalid literal for int() with base <base>: <repr(s)>" for a bad
            # literal — so ``base_val`` must be the original base argument.
            # Emit the err check so try/except can catch it and the bad value
            # never propagates.
            self._emit_post_call_err_check(getattr(expr, "span", None))
            # Unbox to native i64 via the existing marshal helper.
            return marshal.marshal_from_object(
                self.builder,
                self.module,
                self.runtime,
                boxed,
                IntType(name="int"),
            )
        if len(expr.args) == 1 and _is_class_type_for_numeric_builtin(arg_ty):
            int_obj = self._emit_callable_attribute_call(
                arg,
                "__int__",
                (),
                (),
                expr.span,
            )
            return marshal.marshal_from_object(
                self.builder,
                self.module,
                self.runtime,
                int_obj,
                IntType(name="int"),
            )
        if isinstance(arg_ty, DynType):
            obj = self._emit_expr(arg)
            if not isinstance(obj.type, ir.PointerType):
                obj = marshal.marshal_to_object(
                    self.builder,
                    self.module,
                    self.runtime,
                    obj,
                    arg_ty,
                )
            elif obj in getattr(self, "_cpy_values", ()):
                return self.builder.call(
                    self.runtime["py_cpy_to_i64"],
                    [obj],
                    name=self._fresh("int.dyn.cpy_i64"),
                )
            fn = self.current_function
            if fn is None:
                return None
            tag = self.builder.call(
                self.runtime["py_obj_type_tag"],
                [obj],
                name=self._fresh("int.dyn.tag"),
            )
            str_bb = fn.append_basic_block(name=self._fresh("int.dyn.str"))
            non_str_bb = fn.append_basic_block(
                name=self._fresh("int.dyn.non_str"),
            )
            float_bb = fn.append_basic_block(
                name=self._fresh("int.dyn.float"),
            )
            non_float_bb = fn.append_basic_block(
                name=self._fresh("int.dyn.non_float"),
            )
            bool_bb = fn.append_basic_block(name=self._fresh("int.dyn.bool"))
            int_bb = fn.append_basic_block(name=self._fresh("int.dyn.int"))
            join_bb = fn.append_basic_block(name=self._fresh("int.dyn.join"))

            is_str = self.builder.icmp_signed(
                "==",
                tag,
                ir.Constant(_I64, PY_TYPE_STR),
                name=self._fresh("int.dyn.is_str"),
            )
            self.builder.cbranch(is_str, str_bb, non_str_bb)

            self.builder.position_at_end(str_bb)
            cstr = self.builder.call(
                self.runtime["py_str_utf8"],
                [obj],
                name=self._fresh("int.dyn.cstr"),
            )
            boxed = self.builder.call(
                self.runtime["py_int_from_cstr_or_raise"],
                [cstr, base_val],
                name=self._fresh("int.dyn.parse"),
            )
            # ValueError on invalid input (was silent 0); the helper builds the
            # CPython-accurate message from the original (string, base) — bad
            # base vs bad literal, with base/repr embedded. Err check so
            # try/except can catch it. The check's continuation block becomes
            # the current block, so the marshal + branch below stay correct.
            self._emit_post_call_err_check(getattr(expr, "span", None))
            str_val = marshal.marshal_from_object(
                self.builder,
                self.module,
                self.runtime,
                boxed,
                IntType(name="int"),
            )
            str_exit = self.builder._block
            self.builder.branch(join_bb)

            self.builder.position_at_end(non_str_bb)
            is_float = self.builder.icmp_signed(
                "==",
                tag,
                ir.Constant(_I64, PY_TYPE_FLOAT),
                name=self._fresh("int.dyn.is_float"),
            )
            self.builder.cbranch(is_float, float_bb, non_float_bb)

            self.builder.position_at_end(float_bb)
            f64 = self.builder.call(
                self.runtime["py_float_to_f64"],
                [obj],
                name=self._fresh("int.dyn.f64"),
            )
            float_val = self.builder.fptosi(
                f64,
                _I64,
                name=self._fresh("int.dyn.from_float"),
            )
            float_exit = self.builder._block
            self.builder.branch(join_bb)

            self.builder.position_at_end(non_float_bb)
            is_bool = self.builder.icmp_signed(
                "==",
                tag,
                ir.Constant(_I64, PY_TYPE_BOOL),
                name=self._fresh("int.dyn.is_bool"),
            )
            self.builder.cbranch(is_bool, bool_bb, int_bb)

            self.builder.position_at_end(bool_bb)
            bool_val = self.builder.call(
                self.runtime["py_obj_truthy"],
                [obj],
                name=self._fresh("int.dyn.from_bool"),
            )
            bool_exit = self.builder._block
            self.builder.branch(join_bb)

            self.builder.position_at_end(int_bb)
            int_val = self.builder.call(
                self.runtime["py_int_to_i64_lane"],
                [obj, ir.Constant(_I32.as_pointer(), None)],
                name=self._fresh("int.dyn.from_int"),
            )
            int_exit = self.builder._block
            self.builder.branch(join_bb)

            self.builder.position_at_end(join_bb)
            phi = self.builder.phi(_I64, name=self._fresh("int.dyn"))
            phi.add_incoming(str_val, str_exit)
            phi.add_incoming(float_val, float_exit)
            phi.add_incoming(bool_val, bool_exit)
            phi.add_incoming(int_val, int_exit)
            return phi
        return None
    def _maybe_emit_any_all_literal(
        self,
        expr: Call,
        name: str,
    ) -> Optional[ir.Value]:
        """``any(x)`` / ``all(x)`` through the runtime's iterator protocol.

        The argument is evaluated completely, as CPython evaluates it (a
        literal list builds every element first); the runtime then iterates
        with CPython's short circuit, so generators, sets, strings, map
        objects and user iterables stop exactly where CPython stops.
        """
        # ``any(map(lambda x: ..., xs))`` keeps its inlined walk until native
        # map objects exist; a bare map(...) value is not lowered yet.
        mapped = self._maybe_emit_any_all_map_lambda(expr.args[0], name)
        if mapped is not None:
            return mapped
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        try:
            self._try_err_block = self._slot_call_cleanup_block((), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            operand = self._emit_slot_call_operand(expr.args[0], name + ".iterable")
            self._try_err_block = self._slot_call_cleanup_block((operand,), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            status = self._slot_call_runtime_call(
                "py_builtin_any_all", (operand,),
                suffix_args=(ir.Constant(_I64, 1 if name == "all" else 0),), span=expr.span,
            )
            self._release_slot_call_roots((operand,))
            return self.builder.icmp_signed(
                "==", status, ir.Constant(_I64, 1), name=self._fresh(name + ".result"),
            )
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy

    def _maybe_emit_any_all_map_lambda(
        self,
        arg: object,
        name: str,
    ) -> Optional[ir.Value]:
        if (
            not isinstance(arg, Call)
            or not isinstance(arg.func, Name)
            or arg.func.ident != "map"
            or arg.kwargs
            or len(arg.args) != 2
            or not isinstance(arg.args[0], Lambda)
            or len(arg.args[0].params) != 1
        ):
            return None
        lam = arg.args[0]
        param = lam.params[0]
        if param.kind != "pos":
            return None

        src_expr = arg.args[1]
        src_val = self._emit_expr(src_expr)
        src_obj = marshal.marshal_to_object(
            self.builder,
            self.module,
            self.runtime,
            src_val,
            src_expr.ty,
        )
        fn_ = self.current_function
        n_val = self.builder.call(
            self.runtime["py_obj_len"],
            [src_obj],
            name=self._fresh(f"{name}.map.src.len"),
        )
        idx_slot = self._alloca_in_entry(_I64, name=f"{name}.map.idx.addr")
        item_slot = self._alloca_in_entry(_CSTR, name=f"{name}.map.item.addr")
        result_slot = self._alloca_in_entry(
            _I1,
            name=f"{name}.map.result.addr",
        )
        init = 0 if name == "any" else 1
        self.builder.store(ir.Constant(_I1, init), result_slot)
        self.builder.store(ir.Constant(_I64, 0), idx_slot)

        cond_bb = fn_.append_basic_block(name=self._fresh(f"{name}.map.cond"))
        body_bb = fn_.append_basic_block(name=self._fresh(f"{name}.map.body"))
        step_bb = fn_.append_basic_block(name=self._fresh(f"{name}.map.step"))
        end_bb = fn_.append_basic_block(name=self._fresh(f"{name}.map.end"))
        self.builder.branch(cond_bb)

        self.builder.position_at_end(cond_bb)
        cur = self.builder.load(idx_slot, name=self._fresh(f"{name}.map.idx"))
        cond = self.builder.icmp_signed(
            "<",
            cur,
            n_val,
            name=self._fresh(f"{name}.map.cond.i1"),
        )
        self.builder.cbranch(cond, body_bb, end_bb)

        self.builder.position_at_end(body_bb)
        idx_box = self.builder.call(
            self.runtime["py_int_from_i64"],
            [cur],
            name=self._fresh(f"{name}.map.idx.box"),
        )
        elem = self.builder.call(
            self.runtime["py_obj_getitem"],
            [src_obj, idx_box],
            name=self._fresh(f"{name}.map.elem"),
        )
        self.builder.store(elem, item_slot)

        param_name = param.name
        old_env = self.env.get(param_name)
        old_cpy_flag = getattr(self, "_cpy_env_flags", {}).get(param_name)
        old_class_hint = self.env_class_hint.get(param_name)
        old_class_obj_hint = self.env_class_object_hint.get(param_name)
        old_elem_hint = self.env_list_elem_class_hint.get(param_name)
        old_exact_int_flag = getattr(self, "_exact_int_env_flags", {}).get(
            param_name
        )
        self.env[param_name] = (item_slot, _CSTR, DynType(name="dyn"))
        self._cpy_env_flags.pop(param_name, None)
        self.env_class_hint.pop(param_name, None)
        self.env_class_object_hint.pop(param_name, None)
        self.env_list_elem_class_hint.pop(param_name, None)
        self._exact_int_env_flags.pop(param_name, None)
        try:
            mapped = self._emit_expr(lam.body)
        finally:
            if old_env is None:
                self.env.pop(param_name, None)
            else:
                self.env[param_name] = old_env
            if old_cpy_flag is None:
                self._cpy_env_flags.pop(param_name, None)
            else:
                self._cpy_env_flags[param_name] = old_cpy_flag
            if old_class_hint is None:
                self.env_class_hint.pop(param_name, None)
            else:
                self.env_class_hint[param_name] = old_class_hint
            if old_class_obj_hint is None:
                self.env_class_object_hint.pop(param_name, None)
            else:
                self.env_class_object_hint[param_name] = old_class_obj_hint
            if old_elem_hint is None:
                self.env_list_elem_class_hint.pop(param_name, None)
            else:
                self.env_list_elem_class_hint[param_name] = old_elem_hint
            if old_exact_int_flag is None:
                self._exact_int_env_flags.pop(param_name, None)
            else:
                self._exact_int_env_flags[param_name] = old_exact_int_flag
        truthy = self._truthy(mapped, lam.body.ty)
        if name == "any":
            hit_bb = fn_.append_basic_block(name=self._fresh("any.map.hit"))
            self.builder.cbranch(truthy, hit_bb, step_bb)
            self.builder.position_at_end(hit_bb)
            self.builder.store(ir.Constant(_I1, 1), result_slot)
            self.builder.branch(end_bb)
        else:
            miss_bb = fn_.append_basic_block(name=self._fresh("all.map.miss"))
            self.builder.cbranch(truthy, step_bb, miss_bb)
            self.builder.position_at_end(miss_bb)
            self.builder.store(ir.Constant(_I1, 0), result_slot)
            self.builder.branch(end_bb)

        self.builder.position_at_end(step_bb)
        nxt = self.builder.add(
            cur,
            ir.Constant(_I64, 1),
            name=self._fresh(f"{name}.map.idx.next"),
        )
        self.builder.store(nxt, idx_slot)
        self.builder.branch(cond_bb)
        self.builder.position_at_end(end_bb)
        return self.builder.load(
            result_slot,
            name=self._fresh(f"{name}.map.result"),
        )

    def _min_max_needs_object_compare(self, ty) -> bool:
        """True if min()/max() over ``ty`` must compare elements as objects
        rather than assuming that every element can use an integer lane.
        Unknown elements must retain their runtime type and selected identity.
        Static custom-__lt__ and key folds are resolved before this route."""
        if isinstance(ty, (StrType, DynType)):
            return True
        if isinstance(ty, ListType):
            return not isinstance(getattr(ty, "elem", None), IntType)
        if isinstance(ty, TupleType):
            return not ty.elems or any(not isinstance(elem, IntType) for elem in ty.elems)
        return False

    def _min_max_obj_lt_class(self, arg):
        """If ``arg`` is a list whose element class has a resolvable user
        ``__lt__``, return ``(class_name, elem_ty)``; else ``None``. Mirrors
        the element-class detection used by the sorted() fix (#54): a list
        literal of constructor calls (``_list_elem_class_hint_for_expr``) or a
        named list variable (``env_list_elem_class_hint``)."""
        class_name = self._list_elem_class_hint_for_expr(arg)
        if class_name is None and isinstance(arg, Name):
            class_name = self.env_list_elem_class_hint.get(arg.ident)
        if class_name is None:
            return None
        if self._resolve_method_mro(class_name, "__lt__") is None:
            return None
        elem_ty = (
            arg.ty.elem if isinstance(arg.ty, ListType) else DynType(name="dyn")
        )
        return (class_name, elem_ty)

    def _emit_min_max_obj_lt_fold(self, expr, name, class_name, elem_ty):
        """``min(xs)`` / ``max(xs)`` over a list of objects with a user
        ``__lt__``, returning the extreme ELEMENT object. Linear scan with an
        object accumulator; the per-pair compare is the same static dunder
        resolution (`_emit_direct_method_value_call`) the ``<`` operator and
        the sorted() fix use, so it never touches the __lt__-blind runtime
        comparison primitive. ``default=`` seeds the empty case (any other
        kwarg was already excluded by the caller)."""
        default_obj = None
        for k, v in expr.kwargs or ():
            if k == "default":
                default_obj = self._emit_as_object(v)
        lt_info = self._resolve_method_mro(class_name, "__lt__")
        lt_fn = lt_info.methods["__lt__"]
        arg = expr.args[0]
        src_obj = marshal.marshal_to_object(
            self.builder,
            self.module,
            self.runtime,
            self._emit_expr(arg),
            arg.ty,
        )
        _CSTR = ir.IntType(8).as_pointer()
        fn = self.current_function
        n_val = self.builder.call(
            self.runtime["py_list_len"],
            [src_obj],
            name=self._fresh(f"{name}.lt.len"),
        )
        acc_slot = self._alloca_in_entry(_CSTR, name=f"{name}.lt.acc.addr")
        idx_slot = self._alloca_in_entry(_I64, name=f"{name}.lt.idx.addr")
        is_empty = self.builder.icmp_signed(
            "==",
            n_val,
            ir.Constant(_I64, 0),
            name=self._fresh(f"{name}.lt.is_empty"),
        )
        empty_bb = fn.append_basic_block(name=self._fresh(f"{name}.lt.empty"))
        seed_bb = fn.append_basic_block(name=self._fresh(f"{name}.lt.seed"))
        end_bb = fn.append_basic_block(name=self._fresh(f"{name}.lt.end"))
        self.builder.cbranch(is_empty, empty_bb, seed_bb)

        self.builder.position_at_end(empty_bb)
        self.builder.store(
            default_obj if default_obj is not None else self._emit_none_literal(),
            acc_slot,
        )
        self.builder.branch(end_bb)

        self.builder.position_at_end(seed_bb)
        first = self.builder.call(
            self.runtime["py_list_get"],
            [src_obj, ir.Constant(_I64, 0)],
            name=self._fresh(f"{name}.lt.first"),
        )
        self.builder.store(first, acc_slot)
        self.builder.store(ir.Constant(_I64, 1), idx_slot)
        cond_bb = fn.append_basic_block(name=self._fresh(f"{name}.lt.cond"))
        body_bb = fn.append_basic_block(name=self._fresh(f"{name}.lt.body"))
        step_bb = fn.append_basic_block(name=self._fresh(f"{name}.lt.step"))
        self.builder.branch(cond_bb)

        self.builder.position_at_end(cond_bb)
        cur = self.builder.load(idx_slot, name=self._fresh(f"{name}.lt.idx"))
        cond = self.builder.icmp_signed(
            "<", cur, n_val, name=self._fresh(f"{name}.lt.cond.i1")
        )
        self.builder.cbranch(cond, body_bb, end_bb)

        self.builder.position_at_end(body_bb)
        elem = self.builder.call(
            self.runtime["py_list_get"],
            [src_obj, cur],
            name=self._fresh(f"{name}.lt.elem"),
        )
        acc_cur = self.builder.load(acc_slot, name=self._fresh(f"{name}.lt.acc"))
        # min keeps the smaller: replace when ``elem < acc``.
        # max keeps the larger:  replace when ``acc < elem``.
        if name == "min":
            lhs, rhs = elem, acc_cur
        else:
            lhs, rhs = acc_cur, elem
        less = self._emit_direct_method_value_call(
            lt_fn, lhs, lt_info, "__lt__", ((rhs, elem_ty),)
        )
        if less.type is not _I1:
            if isinstance(less.type, ir.IntType):
                less = self.builder.icmp_signed(
                    "!=",
                    less,
                    ir.Constant(less.type, 0),
                    name=self._fresh(f"{name}.lt.less_i1"),
                )
            else:
                truth = self.builder.call(
                    self.runtime["py_obj_truthy"],
                    [less],
                    name=self._fresh(f"{name}.lt.truthy"),
                )
                less = self.builder.icmp_signed(
                    "!=",
                    truth,
                    ir.Constant(truth.type, 0),
                    name=self._fresh(f"{name}.lt.truthy_i1"),
                )
        new_acc = self.builder.select(
            less, elem, acc_cur, name=self._fresh(f"{name}.lt.pick")
        )
        self.builder.store(new_acc, acc_slot)
        self.builder.branch(step_bb)

        self.builder.position_at_end(step_bb)
        nxt = self.builder.add(
            cur, ir.Constant(_I64, 1), name=self._fresh(f"{name}.lt.next")
        )
        self.builder.store(nxt, idx_slot)
        self.builder.branch(cond_bb)

        self.builder.position_at_end(end_bb)
        return self.builder.load(acc_slot, name=self._fresh(f"{name}.lt.result"))

    def _maybe_emit_min_max_iter(
        self,
        expr: Call,
        name: str,
    ) -> Optional[ir.Value]:
        """``min(xs)`` / ``max(xs)`` on a ListType / TupleType /
        DynType iterable of ints. ``default`` kwarg is accepted via
        the resolver and seeds the accumulator on empty."""
        arg = expr.args[0]
        arg_ty = arg.ty
        # A keyword (key=, default=) runs the builtin's Python wrapper, which
        # iterates once and calls key(item) before each comparison as CPython
        # does; the call lowering routes those calls before they reach here.
        if expr.kwargs:
            return None
        # A list whose element class defines a user __lt__: the i64-accumulator
        # fold below reads instance pointers as integers (comparing addresses),
        # and the py_obj_min_max route is __lt__-blind (runtime cmp_threeway
        # pointer-compares). Route to a static-__lt__ object fold instead —
        # the min/max sibling of the sorted() fix (#54). Only when there is no
        # key= (or other) kwarg; default= is handled inside the fold. See
        # docs/investigations/sorted-min-max-custom-lt-not-used-no-libpython.md.
        obj_lt = self._min_max_obj_lt_class(arg)
        if obj_lt is not None and not any(
            k != "default" for k, _ in (expr.kwargs or ())
        ):
            return self._emit_min_max_obj_lt_fold(
                expr, name, obj_lt[0], obj_lt[1]
            )
        # Non-int-element iterables (a str, or a list of str) compare via the
        # generic object helper py_obj_min_max (py_obj_cmp_threeway); the
        # int-accumulator path below is correct only for int elements. Without
        # this, max("abc") / max(["a","b"]) bailed to a name lookup (runtime
        # "NameError: name 'max'"). Returns the extreme element object directly.
        if not expr.kwargs and self._min_max_needs_object_compare(arg_ty):
            # The selected element is NEW even when it aliases an item in the
            # iterable. Publish it before error checks or iterable cleanup,
            # which can invoke callbacks and move or release that element.
            sink = self._slot_call_result_sink(expr)
            output = sink
            roots = []
            if output is None:
                output = self._new_slot_call_root(name + ".result")
                roots.append(output)
            previous = self._current_try_err_block()
            target = previous if previous is not None else self._ensure_fn_err_exit()
            saved_cleanup = self._cpy_operand_cleanup_block
            try:
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                source = self._emit_slot_call_operand(arg, name + ".iterable")
                roots.append(source)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                self._slot_call_runtime_call(
                    "py_obj_min_max", (source,), result_slot=output,
                    suffix_args=(ir.Constant(_I64, 1 if name == "max" else 0),),
                    span=expr.span,
                )
                self._release_slot_call_roots((source,))
                if sink is None:
                    return self._take_slot_call_root(output)
                return self.builder.load(output, name=self._fresh(name + ".current"))
            finally:
                self._try_err_block = previous
                self._cpy_operand_cleanup_block = saved_cleanup
        is_class_iter = isinstance(arg_ty, ClassType)
        if not is_class_iter and not isinstance(
            arg_ty, (ListType, TupleType, DynType)
        ):
            return None
        # Optional ``default=`` kwarg.
        default_val = None
        for k, v in expr.kwargs or ():
            if k == "default":
                default_val = self._emit_expr_as_i64(v)
            else:
                return None  # unknown kwarg
        if is_class_iter:
            # A user-class instance (custom __iter__/__next__, no __len__ /
            # __getitem__): materialise to a list via the iterator protocol,
            # then run the index-based min/max over the list. Without this,
            # min/max(<custom iterator>) bailed to a name lookup (runtime
            # "NameError: name 'min'/'max'"). Matches CPython min/max via iter.
            raw = self._emit_expr(arg)
            raw_obj = marshal.marshal_to_object(
                self.builder, self.module, self.runtime, raw, arg_ty,
            )
            src_obj = self.builder.call(
                self.runtime["py_list_new"],
                [ir.Constant(_I64, 0)],
                name=self._fresh(f"{name}.iter.list"),
            )
            self._emit_list_append_via_iter(
                src_obj, raw_obj, getattr(arg, "span", None),
            )
        else:
            src_val = self._emit_expr(arg)
            src_obj = marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                src_val,
                arg_ty,
            )
        n_val = self.builder.call(
            self.runtime["py_obj_len"],
            [src_obj],
            name=self._fresh(f"{name}.src.len"),
        )
        fn = self.current_function
        idx_slot = self._alloca_in_entry(_I64, name=f"{name}.idx.addr")
        acc_slot = self._alloca_in_entry(_I64, name=f"{name}.acc.addr")
        # Initial fill: if empty and no default → runtime error (we'd
        # have to emit a trap). With default, seed. With non-empty,
        # seed from elem[0] below.
        is_empty = self.builder.icmp_signed(
            "==",
            n_val,
            ir.Constant(_I64, 0),
            name=self._fresh(f"{name}.empty"),
        )
        empty_bb = fn.append_basic_block(name=self._fresh(f"{name}.empty"))
        seed_bb = fn.append_basic_block(name=self._fresh(f"{name}.seed"))
        self.builder.cbranch(is_empty, empty_bb, seed_bb)
        self.builder.position_at_end(empty_bb)
        if default_val is not None:
            self.builder.store(default_val, acc_slot)
        else:
            # No default: store 0 as a fallback (Python would raise
            # ValueError; we don't have exception wiring here).
            self.builder.store(ir.Constant(_I64, 0), acc_slot)
        self.builder.store(ir.Constant(_I64, 1), idx_slot)
        end_bb = fn.append_basic_block(name=self._fresh(f"{name}.end"))
        self.builder.branch(end_bb)

        self.builder.position_at_end(seed_bb)
        # Seed accumulator from index 0.
        zero_box = self.builder.call(
            self.runtime["py_int_from_i64"],
            [ir.Constant(_I64, 0)],
            name=self._fresh(f"{name}.seed.box"),
        )
        first = self.builder.call(
            self.runtime["py_obj_getitem"],
            [src_obj, zero_box],
            name=self._fresh(f"{name}.first"),
        )
        first_i64 = marshal.marshal_from_object(
            self.builder,
            self.module,
            self.runtime,
            first,
            IntType(name="int"),
        )
        self.builder.store(first_i64, acc_slot)
        self.builder.store(ir.Constant(_I64, 1), idx_slot)

        cond_bb = fn.append_basic_block(name=self._fresh(f"{name}.cond"))
        body_bb = fn.append_basic_block(name=self._fresh(f"{name}.body"))
        step_bb = fn.append_basic_block(name=self._fresh(f"{name}.step"))
        self.builder.branch(cond_bb)
        self.builder.position_at_end(cond_bb)
        cur = self.builder.load(idx_slot, name=self._fresh(f"{name}.idx"))
        cond = self.builder.icmp_signed(
            "<",
            cur,
            n_val,
            name=self._fresh(f"{name}.cond.i1"),
        )
        self.builder.cbranch(cond, body_bb, end_bb)
        self.builder.position_at_end(body_bb)
        idx_box = self.builder.call(
            self.runtime["py_int_from_i64"],
            [cur],
            name=self._fresh(f"{name}.idx.box"),
        )
        elem_obj = self.builder.call(
            self.runtime["py_obj_getitem"],
            [src_obj, idx_box],
            name=self._fresh(f"{name}.elem"),
        )
        elem_i64 = marshal.marshal_from_object(
            self.builder,
            self.module,
            self.runtime,
            elem_obj,
            IntType(name="int"),
        )
        acc_cur = self.builder.load(
            acc_slot,
            name=self._fresh(f"{name}.acc"),
        )
        cmp_op = "<" if name == "min" else ">"
        is_better = self.builder.icmp_signed(
            cmp_op,
            elem_i64,
            acc_cur,
            name=self._fresh(f"{name}.cmp"),
        )
        new_acc = self.builder.select(
            is_better,
            elem_i64,
            acc_cur,
            name=self._fresh(f"{name}.pick"),
        )
        self.builder.store(new_acc, acc_slot)
        self.builder.branch(step_bb)
        self.builder.position_at_end(step_bb)
        nxt = self.builder.add(
            cur,
            ir.Constant(_I64, 1),
            name=self._fresh(f"{name}.idx.next"),
        )
        self.builder.store(nxt, idx_slot)
        self.builder.branch(cond_bb)
        self.builder.position_at_end(end_bb)
        return self.builder.load(
            acc_slot,
            name=self._fresh(f"{name}.result"),
        )
    def _min_max_argument_lane(self, args) -> Optional[str]:
        """``"int"`` or ``"float"`` when every argument has that one scalar
        type (bool is its own type, folded as int); None otherwise."""
        first = args[0].ty
        for arg in args:
            ty = arg.ty
            if isinstance(first, BoolType):
                if not isinstance(ty, BoolType):
                    return None
            elif isinstance(first, IntType):
                if not isinstance(ty, IntType):
                    return None
            elif isinstance(first, FloatType):
                if not isinstance(ty, FloatType):
                    return None
            else:
                return None
        if isinstance(first, FloatType):
            return "float"
        if isinstance(first, BoolType):
            return "int"
        # Exact ints are arbitrary precision (min(10 ** 30, 1)); only the
        # raw-int scaffold keeps machine ints, which fit the i64 lane.
        return "int" if getattr(self, "_module_uses_raw_int_scaffold", False) else None

    def _emit_min_max_arguments(self, expr: Call, name: str) -> ir.Value:
        """``min(a, b, ...)`` / ``max(a, b, ...)`` return one argument.

        Like CPython, a later argument replaces the current one only when it
        is strictly smaller (min) or larger (max), so ties, NaN and signed
        zeros keep the earlier operand. Arguments of one scalar type fold in
        their native lane; any other mix is ``min((a, b, ...))``: the
        runtime orders the objects with rich comparison (raising TypeError
        for unorderable pairs) and returns the selected object itself.
        """
        args = expr.args
        op = "<" if name == "min" else ">"
        lane = self._min_max_argument_lane(args)
        if lane == "float":
            values = [self._to_double(self._emit_expr(arg), arg.ty) for arg in args]
            acc = values[0]
            for value in values[1:]:
                better = self.builder.fcmp_ordered(op, value, acc, name=self._fresh(f"{name}.cmp"))
                acc = self.builder.select(better, value, acc, name=self._fresh(name))
            return acc
        if lane == "int":
            values = [self._emit_expr_as_i64(arg) for arg in args]
            acc = values[0]
            for value in values[1:]:
                better = self.builder.icmp_signed(op, value, acc, name=self._fresh(f"{name}.cmp"))
                acc = self.builder.select(better, value, acc, name=self._fresh(name))
            return acc
        # Dyn slots keep the iterable fold on objects: an all-int tuple
        # would otherwise take its i64 accumulator.
        items = TupleExpr(
            span=expr.span,
            ty=TupleType(name="tuple", elems=tuple(DynType(name="dyn") for _ in args)),
            elems=tuple(args),
        )
        result = self._maybe_emit_min_max_iter(
            Call(span=expr.span, ty=expr.ty, func=expr.func, args=(items,), kwargs=()),
            name,
        )
        if result is None:
            raise NotImplementedError(f"{name}() of {len(args)} arguments has no native fold")
        return result

    def _emit_min_max_builtin(self, expr: Call, name: str) -> ir.Value:
        return self._emit_min_max_arguments(expr, name)

    def _emit_min_max_variadic(self, expr: Call, name: str):
        return self._emit_min_max_arguments(expr, name)

    def _emit_abs_builtin(self, expr: Call) -> ir.Value:
        """``abs(x)`` for native int / float / bool, with every other operand
        routed through the pcc object runtime."""
        a_expr = expr.args[0]
        a_ty = a_expr.ty
        if isinstance(a_ty, (DynType, IntType)):
            return self._emit_abs_runtime(expr)
        if isinstance(a_ty, BoolType):
            # bool is always 0/1, so the i64 path is exact. (NOTE: ``abs(bool)``
            # is an ``int`` in CPython and ``print(abs(True))`` hits a separate
            # pre-existing bool-print typing bug — tracked separately, not
            # widened here to keep this fix scoped to the bignum truncation.)
            v = self._emit_expr_as_i64(a_expr)
            zero = ir.Constant(_I64, 0)
            neg = self.builder.icmp_signed(
                "<",
                v,
                zero,
                name=self._fresh("abs.neg"),
            )
            negated = self.builder.sub(
                zero,
                v,
                name=self._fresh("abs.negate"),
            )
            return self.builder.select(
                neg,
                negated,
                v,
                name=self._fresh("abs"),
            )
        if isinstance(a_ty, FloatType):
            # Clear the sign bit, as CPython does: -0.0 and a negative NaN
            # compare false against zero, so compare-and-negate kept their
            # sign.  One ``fabs`` instruction.
            v = self._emit_expr(a_expr)
            v = self._to_double(v, a_ty)
            return self.builder.call(
                self._get_fabs_intrinsic(), [v], name=self._fresh("abs")
            )
        if isinstance(a_ty, ComplexType):
            # abs(complex) is the magnitude sqrt(re**2 + im**2) -> float.
            arg_obj = self._emit_as_object(a_expr)
            return self.builder.call(
                self.runtime["py_complex_abs"],
                [arg_obj],
                name=self._fresh("complex.abs"),
            )
        if isinstance(a_ty, ClassType):
            # abs(obj) dispatches to the user __abs__ method, mirroring unary
            # -obj -> __neg__ (unary_call_lowering). The helper emits the
            # receiver itself.
            dunder = self._try_dispatch_dunder_unary(a_expr, "__abs__", ())
            if dunder is not None:
                return dunder
        # Any other operand (str, list, a class without __abs__) takes the
        # runtime's protocol and its CPython TypeError.
        return self._emit_abs_runtime(expr)

    def _emit_abs_runtime(self, expr: Call) -> ir.Value:
        # py_obj_abs returns NEW, including its retained positive-int alias
        # and the protocol callback result. Keep full integer precision and
        # publish before disposing an operand that may alias that result.
        a_expr = expr.args[0]
        sink = self._slot_call_result_sink(expr)
        output = sink
        roots = []
        if output is None:
            output = self._new_slot_call_root("abs.result")
            roots.append(output)
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cleanup = self._cpy_operand_cleanup_block
        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            operand = self._emit_slot_call_operand(a_expr, "abs.operand")
            roots.append(operand)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            self._slot_call_runtime_call(
                "py_obj_abs", (operand,), result_slot=output, span=expr.span,
            )
            self._guard_cpy_value_not_null(self.builder.load(output))
            self._release_slot_call_roots((operand,))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cleanup
        if sink is not None:
            return self.builder.load(output, name=self._fresh("abs.output"))
        return self._take_slot_call_root(output)
