"""Numeric and aggregate builtin lowering helpers for L1CodeGen."""
from __future__ import annotations

import os
import sys
from typing import Optional

from pcc.llvm_capi.compat import ir

from ..py_ast import (
    BoolType,
    ByteArrayType,
    BytesType,
    Call,
    ClassType,
    ComplexType,
    DictType,
    DynType,
    FloatType,
    IntType,
    Lambda,
    ListExpr,
    ListType,
    Name,
    SetType,
    StrType,
    TupleExpr,
    TupleType,
)
from . import marshal
from .freestanding_abi_constants import PY_TYPE_BOOL, PY_TYPE_FLOAT, PY_TYPE_STR


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
            return self.builder.fptosi(
                v,
                _I64,
                name=self._fresh("int.from_float"),
            )
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
                self.runtime["py_int_to_i64"],
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
    def _emit_sum_start_object(self, start):
        if start is None:
            return self.builder.call(
                self.runtime["py_int_from_i64"],
                [ir.Constant(_I64, 0)],
                name=self._fresh("sum.start.zero"),
            )
        raw = self._emit_expr(start)
        return marshal.marshal_to_object(
            self.builder,
            self.module,
            self.runtime,
            raw,
            start.ty,
        )

    def _emit_sum_add_objects(self, acc_obj, elem_obj):
        return self.builder.call(
            self.runtime["py_int_add"],
            [acc_obj, elem_obj],
            name=self._fresh("sum.obj.next"),
        )

    def _emit_sum_via_iter(self, src_obj, start, span):
        """``sum(<iterable>)`` over a DynType source via the iterator protocol
        (py_obj_iter/py_obj_next), accumulating with Python int semantics. Used
        for iterator-only objects such as generators (no length / __getitem__).
        Mirrors the for-loop / list-builtin iterator path; clears a terminal
        StopIteration (tag 8) and propagates any other exception."""
        cstr = ir.IntType(8).as_pointer()
        fn = self.current_function
        acc_slot = self._alloca_in_entry(_CSTR, name="sum.iter.acc.addr")
        self.builder.store(self._emit_sum_start_object(start), acc_slot)
        iterator = self.builder.call(
            self.runtime["py_obj_iter"],
            [src_obj],
            name=self._fresh("sum.iter.obj"),
        )
        self._emit_post_call_err_check(span)
        header_bb = fn.append_basic_block(name=self._fresh("sum.iter.next"))
        body_bb = fn.append_basic_block(name=self._fresh("sum.iter.body"))
        maybe_end_bb = fn.append_basic_block(name=self._fresh("sum.iter.maybe_end"))
        clear_bb = fn.append_basic_block(name=self._fresh("sum.iter.clear"))
        propagate_bb = fn.append_basic_block(name=self._fresh("sum.iter.propagate"))
        end_bb = fn.append_basic_block(name=self._fresh("sum.iter.end"))
        self.builder.branch(header_bb)
        self.builder.position_at_end(header_bb)
        item = self.builder.call(
            self.runtime["py_obj_next"],
            [iterator],
            name=self._fresh("sum.iter.item"),
        )
        is_null = self.builder.icmp_unsigned(
            "==", item, ir.Constant(cstr, None), name=self._fresh("sum.iter.null")
        )
        self.builder.cbranch(is_null, maybe_end_bb, body_bb)
        self.builder.position_at_end(body_bb)
        acc_cur = self.builder.load(acc_slot, name=self._fresh("sum.iter.acc"))
        self.builder.store(self._emit_sum_add_objects(acc_cur, item), acc_slot)
        self.builder.branch(header_bb)
        self.builder.position_at_end(maybe_end_bb)
        current_exc = self.builder.call(
            self.runtime["py_current_exception"], [], name=self._fresh("sum.iter.cur_exc")
        )
        stop_cls = self.builder.call(
            self.runtime["py_exc_builtin_class"],
            [ir.Constant(_I64, 8)],            # StopIteration
            name=self._fresh("sum.iter.stop_cls"),
        )
        match_i64 = self.builder.call(
            self.runtime["py_exc_matches"],
            [current_exc, stop_cls],
            name=self._fresh("sum.iter.stop_match"),
        )
        is_stop = self.builder.icmp_signed(
            "!=", match_i64, ir.Constant(_I64, 0), name=self._fresh("sum.iter.stop_i1")
        )
        self.builder.cbranch(is_stop, clear_bb, propagate_bb)
        self.builder.position_at_end(clear_bb)
        self.builder.call(self.runtime["py_clear_exception"], [])
        self.builder.branch(end_bb)
        self.builder.position_at_end(propagate_bb)
        err_target = getattr(self, "_try_err_block", None)
        if err_target is None:
            err_target = self._ensure_fn_err_exit()
        self.builder.branch(err_target)
        self.builder.position_at_end(end_bb)
        return self.builder.load(acc_slot, name=self._fresh("sum.iter.result"))

    def _maybe_emit_sum_literal(self, expr: Call) -> Optional[ir.Value]:
        """``sum([a, b, c])`` / ``sum((a, b), start)`` for numeric
        literal containers — fold element-wise add, seeded with the
        start value if given else 0.

        Also handles the runtime case ``sum(iterable)`` when the
        iterable's static type is ``ListType`` / ``TupleType`` /
        ``DynType`` — assumes int elements and uses the generic
        ``py_obj_len`` / ``py_obj_getitem`` loop.
        """
        arg = expr.args[0]
        start = expr.args[1] if len(expr.args) == 2 else None
        if not isinstance(arg, (TupleExpr, ListExpr)):
            if not isinstance(
                arg.ty,
                (ListType, TupleType, DynType, ClassType),
            ):
                return None
            # Runtime iteration path — always int-result; float
            # sum(iterable) falls through to NotImplementedError.
            src_val = self._emit_expr(arg)
            src_obj = marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                src_val,
                arg.ty,
            )
            if isinstance(arg.ty, (DynType, ClassType)):
                # DynType / a user-class instance may be iterator-only
                # (generator, or a custom __iter__/__next__ class) with no
                # length / __getitem__. Consume via the iterator protocol.
                # Without this, sum(CustomIterable()) bailed to a generic name
                # lookup ("name 'sum' is not defined"). See
                # docs/investigations/sequence-builtins-len-getitem-not-iterator-protocol.md
                return self._emit_sum_via_iter(
                    src_obj, start, getattr(arg, "span", None)
                )
            n_val = self.builder.call(
                self.runtime["py_obj_len"],
                [src_obj],
                name=self._fresh("sum.src.len"),
            )
            fn_ = self.current_function
            idx_slot = self._alloca_in_entry(_I64, name="sum.idx.addr")
            acc_slot = self._alloca_in_entry(_CSTR, name="sum.acc.addr")
            self.builder.store(ir.Constant(_I64, 0), idx_slot)
            self.builder.store(self._emit_sum_start_object(start), acc_slot)
            cond_bb = fn_.append_basic_block(name=self._fresh("sum.cond"))
            body_bb = fn_.append_basic_block(name=self._fresh("sum.body"))
            step_bb = fn_.append_basic_block(name=self._fresh("sum.step"))
            end_bb = fn_.append_basic_block(name=self._fresh("sum.end"))
            self.builder.branch(cond_bb)
            self.builder.position_at_end(cond_bb)
            cur = self.builder.load(idx_slot, name=self._fresh("sum.idx"))
            cond = self.builder.icmp_signed(
                "<",
                cur,
                n_val,
                name=self._fresh("sum.cond.i1"),
            )
            self.builder.cbranch(cond, body_bb, end_bb)
            self.builder.position_at_end(body_bb)
            idx_box = self.builder.call(
                self.runtime["py_int_from_i64"],
                [cur],
                name=self._fresh("sum.idx.box"),
            )
            elem_obj = self.builder.call(
                self.runtime["py_obj_getitem"],
                [src_obj, idx_box],
                name=self._fresh("sum.elem"),
            )
            acc_cur = self.builder.load(
                acc_slot,
                name=self._fresh("sum.acc"),
            )
            new_acc = self._emit_sum_add_objects(acc_cur, elem_obj)
            self.builder.store(new_acc, acc_slot)
            self.builder.branch(step_bb)
            self.builder.position_at_end(step_bb)
            nxt = self.builder.add(
                cur,
                ir.Constant(_I64, 1),
                name=self._fresh("sum.idx.next"),
            )
            self.builder.store(nxt, idx_slot)
            self.builder.branch(cond_bb)
            self.builder.position_at_end(end_bb)
            return self.builder.load(
                acc_slot,
                name=self._fresh("sum.result"),
            )
        elems = arg.elems
        start = expr.args[1] if len(expr.args) == 2 else None
        any_float = any(isinstance(e.ty, FloatType) for e in elems)
        if start is not None and isinstance(start.ty, FloatType):
            any_float = True
        if not all(isinstance(e.ty, (IntType, FloatType, BoolType)) for e in elems):
            return None
        if start is not None and not isinstance(
            start.ty,
            (IntType, FloatType, BoolType),
        ):
            return None
        if any_float:
            if start is not None:
                acc = self._emit_expr(start)
                if not isinstance(start.ty, FloatType):
                    acc = self._to_double(acc, start.ty)
            else:
                acc = ir.Constant(_DOUBLE, 0.0)
            for e in elems:
                v = self._emit_expr(e)
                if not isinstance(e.ty, FloatType):
                    v = self._to_double(v, e.ty)
                acc = self.builder.fadd(
                    acc,
                    v,
                    name=self._fresh("sum"),
                )
            return acc
        # All-int path.
        acc = self._emit_sum_start_object(start)
        for e in elems:
            v = self._emit_expr(e)
            v_obj = marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                v,
                e.ty,
            )
            acc = self._emit_sum_add_objects(acc, v_obj)
        return acc
    def _maybe_emit_any_all_literal(
        self,
        expr: Call,
        name: str,
    ) -> Optional[ir.Value]:
        """``any((a, b, c))`` / ``all([a, b, c])`` over a literal tuple
        or list — lower via a short-circuit chain of ``or`` / ``and``
        over the elements' truthiness. For runtime iterables
        (ListType / TupleType / DictType / DynType) iterate via
        ``py_obj_len`` / ``py_obj_getitem`` with early exit."""
        arg = expr.args[0]
        mapped = self._maybe_emit_any_all_map_lambda(arg, name)
        if mapped is not None:
            return mapped
        if not isinstance(arg, (TupleExpr, ListExpr)):
            arg_ty = arg.ty
            if not isinstance(
                arg_ty,
                (ListType, TupleType, DictType, SetType, BytesType, ByteArrayType, DynType),
            ):
                return None
            src_val = self._emit_expr(arg)
            src_obj = marshal.marshal_to_object(
                self.builder,
                self.module,
                self.runtime,
                src_val,
                arg_ty,
            )
            owned_dict_keys = None
            if isinstance(arg_ty, DictType):
                # The walk below indexes the source positionally, and for a
                # dict py_obj_getitem(d, i) is a KEY lookup for 0, 1, 2 ….
                # Iterating a mapping yields its keys, so any()/all() over a
                # string-keyed dict silently answered False/False.
                src_obj = self.builder.call(
                    self.runtime["py_dict_keys"],
                    [src_obj],
                    name=self._fresh(f"{name}.src.dict.keys"),
                )
                owned_dict_keys = src_obj
            fn_ = self.current_function
            n_val = self.builder.call(
                self.runtime["py_obj_len"],
                [src_obj],
                name=self._fresh(f"{name}.src.len"),
            )
            # Fail-closed edges are emitted for DynType sources only: a
            # statically known List/Tuple/Dict/Set walk cannot raise from
            # len/getitem, and adding checks there would grow every static
            # shape's IR for nothing (the cost guard in
            # tests/python/test_native_container_builtin_error_paths.py
            # pins that).  A dyn source can raise from a user __len__ and,
            # for a dyn-held mapping, from the positional py_obj_getitem
            # (KeyError) — without the checks the walk kept looping over
            # NULL elements and returned a silently wrong bool.
            walk_can_raise = isinstance(arg_ty, DynType)
            if walk_can_raise:
                self._emit_post_call_err_check(expr.span)
                # py_obj_len returned 0 silently for a dyn-held scalar, so
                # any(5)/all(5) answered False/True where CPython raises.
                self._emit_non_iterable_scalar_guard(src_obj, name)
            idx_slot = self._alloca_in_entry(_I64, name=f"{name}.idx.addr")
            self.builder.store(ir.Constant(_I64, 0), idx_slot)
            # Result alloca — default identity (any=False, all=True).
            result_slot = self._alloca_in_entry(
                _I1,
                name=f"{name}.result.addr",
            )
            init = 0 if name == "any" else 1
            self.builder.store(
                ir.Constant(_I1, init),
                result_slot,
            )

            cond_bb = fn_.append_basic_block(
                name=self._fresh(f"{name}.cond"),
            )
            body_bb = fn_.append_basic_block(
                name=self._fresh(f"{name}.body"),
            )
            step_bb = fn_.append_basic_block(
                name=self._fresh(f"{name}.step"),
            )
            end_bb = fn_.append_basic_block(
                name=self._fresh(f"{name}.end"),
            )
            self.builder.branch(cond_bb)
            self.builder.position_at_end(cond_bb)
            cur = self.builder.load(
                idx_slot,
                name=self._fresh(f"{name}.idx"),
            )
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
            elem = self.builder.call(
                self.runtime["py_obj_getitem"],
                [src_obj, idx_box],
                name=self._fresh(f"{name}.elem"),
            )
            if walk_can_raise:
                # elem is the raising call's own NULL return on this edge
                # (pcc_gc_release is NULL-safe); idx_box is live owned.
                self._emit_post_call_err_check(
                    expr.span,
                    release_on_error=(elem, idx_box),
                )
                # py_obj_getitem returns NULL WITHOUT raising for a missing
                # dict key (py_dict_get is the silent variant) and for
                # unsupported tags.  Without this guard the walk fed NULL
                # into py_obj_truthy and returned a silently wrong bool for
                # a dyn-held mapping.
                elem_null = self.builder.icmp_unsigned(
                    "==",
                    elem,
                    ir.Constant(elem.type, None),
                    name=self._fresh(f"{name}.elem.null"),
                )
                badelem_bb = fn_.append_basic_block(
                    name=self._fresh(f"{name}.elem.bad")
                )
                elemok_bb = fn_.append_basic_block(
                    name=self._fresh(f"{name}.elem.ok")
                )
                self.builder.cbranch(elem_null, badelem_bb, elemok_bb)
                self.builder.position_at_end(badelem_bb)
                self._gc_release(
                    idx_box,
                    self._release_context_label(f"{name}.idx.box"),
                )
                message = self._ptr_to_cstr(
                    self._cstr_global(
                        name + "() argument is not indexable from 0..len-1"
                        " (mappings iterate by key in CPython; unsupported"
                        " here for dyn sources)",
                        f".{name}.dyn.typeerror",
                    )
                )
                exc = self.builder.call(
                    self.runtime["py_exc_new"],
                    [ir.Constant(_I64, 3), message],
                    name=self._fresh(f"{name}.dyn.exc"),
                )
                self.builder.call(self.runtime["py_raise"], [exc])
                err_target = (
                    self._current_try_err_block()
                    or self._ensure_fn_err_exit()
                )
                self.builder.branch(err_target)
                self.builder.position_at_end(elemok_bb)
            truthy_i32 = self.builder.call(
                self.runtime["py_obj_truthy"],
                [elem],
                name=self._fresh(f"{name}.truthy"),
            )
            # py_int_from_i64 and py_obj_getitem both return NEW refs, and
            # nothing downstream takes ownership of either: the truthiness
            # test is read-only.  Released here rather than after the
            # short-circuit branch below, so every exit edge is covered by
            # construction instead of by three separate cleanups.
            self._gc_release(elem, self._release_context_label(f"{name}.elem"))
            self._gc_release(
                idx_box, self._release_context_label(f"{name}.idx.box")
            )
            if walk_can_raise:
                # Covers a raising user __bool__/__len__ inside
                # py_obj_truthy; the loop temps were already released just
                # above, so this edge has nothing left to drop.
                self._emit_post_call_err_check(expr.span)
            truthy = self.builder.icmp_signed(
                "!=",
                truthy_i32,
                ir.Constant(_I32, 0),
                name=self._fresh(f"{name}.truthy.i1"),
            )
            if name == "any":
                # Truthy → result True, exit. Falsy → continue.
                exit_bb = fn_.append_basic_block(
                    name=self._fresh("any.hit"),
                )
                self.builder.cbranch(truthy, exit_bb, step_bb)
                self.builder.position_at_end(exit_bb)
                self.builder.store(ir.Constant(_I1, 1), result_slot)
                self.builder.branch(end_bb)
            else:  # all
                exit_bb = fn_.append_basic_block(
                    name=self._fresh("all.miss"),
                )
                self.builder.cbranch(truthy, step_bb, exit_bb)
                self.builder.position_at_end(exit_bb)
                self.builder.store(ir.Constant(_I1, 0), result_slot)
                self.builder.branch(end_bb)
            self.builder.position_at_end(step_bb)
            nxt = self.builder.add(
                cur,
                ir.Constant(_I64, 1),
                name=self._fresh(f"{name}.idx.next"),
            )
            self.builder.store(nxt, idx_slot)
            self.builder.branch(cond_bb)
            self.builder.position_at_end(end_bb)
            if owned_dict_keys is not None:
                # ``py_dict_keys`` returns a NEW list ref (py_runtime.h).
                # ``end_bb`` is the single join for the exhausted walk and for
                # both short-circuit exits, so one release here covers every
                # edge that reaches the result load.
                self._gc_release(
                    owned_dict_keys,
                    self._release_context_label(f"{name}.src.dict.keys"),
                )
            if isinstance(arg_ty, (BytesType, ByteArrayType)):
                self._gc_release_if_owned(src_obj, arg)
            return self.builder.load(
                result_slot,
                name=self._fresh(f"{name}.result"),
            )
        elems = arg.elems
        if not elems:
            return ir.Constant(_I1, 0 if name == "any" else 1)

        # Open the diamond per element to get a true short-circuit
        # chain; phi at the join carries either the accumulated result
        # or the per-element truthy value.
        fn = self.current_function
        join_bb = fn.append_basic_block(
            name=self._fresh(f"{name}.join"),
        )
        incoming: list[tuple[ir.Value, object]] = []
        for i, literal_elem in enumerate(elems):
            v = self._emit_expr(literal_elem)
            truthy = self._truthy(v, literal_elem.ty)
            is_last = i == len(elems) - 1
            if is_last:
                incoming.append((truthy, self.builder._block))
                self.builder.branch(join_bb)
                break
            next_bb = fn.append_basic_block(
                name=self._fresh(f"{name}.next"),
            )
            if name == "any":
                # Truthy wins — branch to join with True.
                true_val = ir.Constant(_I1, 1)
                # Need to go through a small block so the incoming
                # value recorded at join is the constant rather than
                # a phi-dependent SSA mapping.
                true_bb = fn.append_basic_block(
                    name=self._fresh("any.true"),
                )
                self.builder.cbranch(truthy, true_bb, next_bb)
                self.builder.position_at_end(true_bb)
                incoming.append((true_val, true_bb))
                self.builder.branch(join_bb)
            else:  # all
                false_val = ir.Constant(_I1, 0)
                false_bb = fn.append_basic_block(
                    name=self._fresh("all.false"),
                )
                self.builder.cbranch(truthy, next_bb, false_bb)
                self.builder.position_at_end(false_bb)
                incoming.append((false_val, false_bb))
                self.builder.branch(join_bb)
            self.builder.position_at_end(next_bb)

        self.builder.position_at_end(join_bb)
        phi = self.builder.phi(_I1, name=self._fresh(name))
        for val, pred_bb in incoming:
            phi.add_incoming(val, pred_bb)
        return phi

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

    def _emit_min_max_by_key_fold(self, expr, name, key_spec):
        """``min(xs, key=k)`` / ``max(xs, key=k)`` for a simple attr/index key
        (key_spec from _sorted_key_spec_from_lambda, the sorted() #56 helper),
        returning the extreme ELEMENT. Materialises any iterable to a list,
        then a linear scan with an object accumulator comparing inline-extracted
        keys via py_obj_lt (correct for int/str/float keys). Strict ``<`` keeps
        the FIRST extreme element, matching CPython. ``default=`` seeds empty."""
        default_obj = None
        for k, v in expr.kwargs or ():
            if k == "default":
                default_obj = self._emit_as_object(v)
        arg = expr.args[0]
        raw_obj = marshal.marshal_to_object(
            self.builder,
            self.module,
            self.runtime,
            self._emit_expr(arg),
            arg.ty,
        )
        _CSTR = ir.IntType(8).as_pointer()
        fn = self.current_function
        # Materialise any iterable (list / tuple / generator / range) to a list
        # so py_list_len / py_list_get index cleanly.
        src_obj = self.builder.call(
            self.runtime["py_list_new"],
            [ir.Constant(_I64, 0)],
            name=self._fresh(f"{name}.key.list"),
        )
        self._emit_list_append_via_iter(
            src_obj, raw_obj, getattr(arg, "span", None)
        )
        n_val = self.builder.call(
            self.runtime["py_list_len"],
            [src_obj],
            name=self._fresh(f"{name}.key.len"),
        )
        acc_slot = self._alloca_in_entry(_CSTR, name=f"{name}.key.acc.addr")
        idx_slot = self._alloca_in_entry(_I64, name=f"{name}.key.idx.addr")
        is_empty = self.builder.icmp_signed(
            "==",
            n_val,
            ir.Constant(_I64, 0),
            name=self._fresh(f"{name}.key.is_empty"),
        )
        empty_bb = fn.append_basic_block(name=self._fresh(f"{name}.key.empty"))
        seed_bb = fn.append_basic_block(name=self._fresh(f"{name}.key.seed"))
        end_bb = fn.append_basic_block(name=self._fresh(f"{name}.key.end"))
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
            name=self._fresh(f"{name}.key.first"),
        )
        self.builder.store(first, acc_slot)
        self.builder.store(ir.Constant(_I64, 1), idx_slot)
        cond_bb = fn.append_basic_block(name=self._fresh(f"{name}.key.cond"))
        body_bb = fn.append_basic_block(name=self._fresh(f"{name}.key.body"))
        step_bb = fn.append_basic_block(name=self._fresh(f"{name}.key.step"))
        self.builder.branch(cond_bb)

        self.builder.position_at_end(cond_bb)
        cur = self.builder.load(idx_slot, name=self._fresh(f"{name}.key.idx"))
        cond = self.builder.icmp_signed(
            "<", cur, n_val, name=self._fresh(f"{name}.key.cond.i1")
        )
        self.builder.cbranch(cond, body_bb, end_bb)

        self.builder.position_at_end(body_bb)
        elem = self.builder.call(
            self.runtime["py_list_get"],
            [src_obj, cur],
            name=self._fresh(f"{name}.key.elem"),
        )
        acc_cur = self.builder.load(acc_slot, name=self._fresh(f"{name}.key.acc"))
        # min keeps the smaller: replace when key(elem) < key(acc).
        # max keeps the larger:  replace when key(acc) < key(elem).
        if name == "min":
            lhs, rhs = elem, acc_cur
        else:
            lhs, rhs = acc_cur, elem
        key_lhs = self._emit_key_of(lhs, key_spec)
        key_rhs = self._emit_key_of(rhs, key_spec)
        less_i64 = self.builder.call(
            self.runtime["py_obj_lt"],
            [key_lhs, key_rhs],
            name=self._fresh(f"{name}.key.lt"),
        )
        less = self.builder.icmp_signed(
            "!=",
            less_i64,
            ir.Constant(less_i64.type, 0),
            name=self._fresh(f"{name}.key.lt.i1"),
        )
        new_acc = self.builder.select(
            less, elem, acc_cur, name=self._fresh(f"{name}.key.pick")
        )
        self.builder.store(new_acc, acc_slot)
        self.builder.branch(step_bb)

        self.builder.position_at_end(step_bb)
        nxt = self.builder.add(
            cur, ir.Constant(_I64, 1), name=self._fresh(f"{name}.key.next")
        )
        self.builder.store(nxt, idx_slot)
        self.builder.branch(cond_bb)

        self.builder.position_at_end(end_bb)
        return self.builder.load(acc_slot, name=self._fresh(f"{name}.key.result"))

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
        # key=<supported inline callable>: inline the key extraction (no
        # first-class-fn boxing), reusing the sorted() #56 key machinery, in an
        # object fold returning the extreme ELEMENT. Takes precedence over the
        # element-__lt__ route below (Python uses key=, ignoring __lt__). A
        # non-simple lambda / unsupported key / unknown kwarg returns None here
        # -> libpython fallback (we must NOT run the key-blind folds below).
        if any(k == "key" for k, _ in (expr.kwargs or ())):
            key_expr = None
            handled = True
            for k, v in expr.kwargs:
                if k == "key":
                    key_expr = v
                elif k == "default":
                    pass
                else:
                    handled = False
            if handled and key_expr is not None:
                elem_ty = (
                    arg_ty.elem if isinstance(arg_ty, ListType) else None
                )
                key_spec = self._key_spec_from_callable(key_expr, elem_ty)
                if key_spec is not None:
                    return self._emit_min_max_by_key_fold(expr, name, key_spec)
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
            src_obj = self._emit_as_object(arg)
            want_max = ir.Constant(_I64, 1 if name == "max" else 0)
            res = self.builder.call(
                self.runtime["py_obj_min_max"],
                [src_obj, want_max],
                name=self._fresh(f"{name}.obj"),
            )
            self._emit_post_call_err_check(getattr(arg, "span", None))
            return res
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
    def _emit_min_max_builtin(self, expr: Call, name: str) -> ir.Value:
        """Lower ``min(a, b)`` / ``max(a, b)`` when both args are
        native int / float / bool (or DynType narrowed via
        ``_emit_expr_as_i64``). Non-numeric container forms fall
        through to NotImplementedError."""
        a_expr, b_expr = expr.args
        try:
            a_ty = a_expr.ty
            b_ty = b_expr.ty
        except AttributeError as exc:
            if os.environ.get("PCC_DEBUG_BOOTSTRAP_TRACE", "").strip().lower() in (
                "1",
                "true",
                "yes",
                "on",
            ):
                sys.stderr.write(
                    "debug: minmax_arg_ty_missing name="
                    + name
                    + " missing="
                    + str(exc)
                    + " a_type="
                    + type(a_expr).__name__
                    + " a_kind="
                    + str(getattr(type(a_expr), "__name__", ""))
                    + " a_func="
                    + str(getattr(getattr(a_expr, "func", None), "ident", ""))
                    + " a_has_ty="
                    + str(hasattr(a_expr, "ty"))
                    + " b_type="
                    + type(b_expr).__name__
                    + " b_func="
                    + str(getattr(getattr(b_expr, "func", None), "ident", ""))
                    + " b_has_ty="
                    + str(hasattr(b_expr, "ty"))
                    + "\n"
                )
            raise

        def is_numeric_type(ty):
            return (
                isinstance(ty, IntType)
                or isinstance(ty, FloatType)
                or isinstance(ty, BoolType)
                or isinstance(ty, DynType)
            )

        if not (is_numeric_type(a_ty) and is_numeric_type(b_ty)):
            raise NotImplementedError(
                f"Layer 1 {name}() with non-numeric args "
                f"({a_ty!r}, {b_ty!r}) needs runtime support"
            )
        if isinstance(a_ty, FloatType) or isinstance(b_ty, FloatType):
            av = self._emit_expr(a_expr)
            bv = self._emit_expr(b_expr)
            av = self._to_double(av, a_ty)
            bv = self._to_double(bv, b_ty)
            cmp = self.builder.fcmp_ordered(
                "<" if name == "min" else ">",
                av,
                bv,
                name=self._fresh(f"{name}.cmp"),
            )
        else:
            av = self._emit_expr_as_i64(a_expr)
            bv = self._emit_expr_as_i64(b_expr)
            cmp = self.builder.icmp_signed(
                "<" if name == "min" else ">",
                av,
                bv,
                name=self._fresh(f"{name}.cmp"),
            )
        return self.builder.select(
            cmp,
            av,
            bv,
            name=self._fresh(name),
        )
    def _emit_min_max_variadic(self, expr: Call, name: str):
        """Lower ``min(a, b, c, ...)`` / ``max(a, b, c, ...)`` (3+ positional
        args) by folding the same compare+select used by the 2-arg path. Only
        the all-numeric (int / float / bool / DynType) case is handled here;
        any non-numeric arg returns ``None`` so the caller can fall through.
        Float promotion matches the 2-arg path: if any arg is a float, all
        operands are widened to double; otherwise everything folds as i64
        (``_emit_expr_as_i64`` covers int / bool / DynType)."""
        args = expr.args

        def is_numeric_type(ty):
            return isinstance(ty, (IntType, FloatType, BoolType, DynType))

        if not all(is_numeric_type(a.ty) for a in args):
            return None
        op = "<" if name == "min" else ">"
        if any(isinstance(a.ty, FloatType) for a in args):
            # Float fold: promote int/bool args to double via an i64 first
            # (``_emit_expr`` may hand back a boxed void* for non-float args,
            # which can't sitofp). A DynType arg can't be safely widened to
            # double here, so bail to the fallback.
            if not all(isinstance(a.ty, (IntType, FloatType, BoolType)) for a in args):
                return None
            vals = []
            for a in args:
                if isinstance(a.ty, FloatType):
                    vals.append(self._emit_expr(a))
                else:
                    iv = self._emit_expr_as_i64(a)
                    vals.append(
                        self.builder.sitofp(iv, _DOUBLE, name=self._fresh("promote"))
                    )
            acc = vals[0]
            for v in vals[1:]:
                cmp = self.builder.fcmp_ordered(op, acc, v, name=self._fresh(f"{name}.cmp"))
                acc = self.builder.select(cmp, acc, v, name=self._fresh(name))
            return acc
        vals = [self._emit_expr_as_i64(a) for a in args]
        acc = vals[0]
        for v in vals[1:]:
            cmp = self.builder.icmp_signed(op, acc, v, name=self._fresh(f"{name}.cmp"))
            acc = self.builder.select(cmp, acc, v, name=self._fresh(name))
        return acc

    def _emit_abs_builtin(self, expr: Call) -> ir.Value:
        """``abs(x)`` for native int / float / bool, with DynType
        routed through the pcc object runtime."""
        a_expr = expr.args[0]
        a_ty = a_expr.ty
        if isinstance(a_ty, DynType):
            arg_obj = self._emit_as_object(a_expr)
            result = self.builder.call(
                self.runtime["py_obj_abs"],
                [arg_obj],
                name=self._fresh("obj.abs"),
            )
            self._emit_post_call_err_check(self._expr_span_or_none(expr))
            return result
        if isinstance(a_ty, IntType):
            # ``int`` is arbitrary precision: the i64 fast path truncated a
            # bignum (abs(10**40) collapsed to 0). Route through the object
            # runtime py_obj_abs (the bignum-correct path DynType uses), which
            # preserves/promotes bignums instead of truncating to i64.
            arg_obj = self._emit_as_object(a_expr)
            result = self.builder.call(
                self.runtime["py_obj_abs"],
                [arg_obj],
                name=self._fresh("int.abs"),
            )
            self._emit_post_call_err_check(self._expr_span_or_none(expr))
            return result
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
            v = self._emit_expr(a_expr)
            zero = ir.Constant(_DOUBLE, 0.0)
            neg = self.builder.fcmp_ordered(
                "<",
                v,
                zero,
                name=self._fresh("abs.neg"),
            )
            negated = self.builder.fsub(
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
        raise NotImplementedError(
            f"Layer 1 abs() with arg type {a_ty!r} needs runtime support"
        )
