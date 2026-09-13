"""Set lowering helpers for L1CodeGen."""
from __future__ import annotations

from typing import Optional

from pcc.llvm_capi.compat import ir

from ..py_ast import (
    Attr,
    Call,
    ClassType,
    DictType,
    DynType,
    Expr,
    ListExpr,
    ListType,
    Name,
    SetType,
    StrType,
    TupleExpr,
    TupleType,
)
from . import marshal
from .freestanding_abi_constants import PY_TYPE_SET


_I1 = ir.IntType(1)
_I64 = ir.IntType(64)
_CSTR = ir.IntType(8).as_pointer()

_DYN_SET_METHOD_NATIVE = frozenset(
    {
        "add",
        "remove",
        "discard",
        "update",
        "issubset",
        "issuperset",
        "isdisjoint",
        "union",
        "intersection",
        "difference",
        "symmetric_difference",
        "intersection_update",
        "difference_update",
        "symmetric_difference_update",
        "copy",
        "pop",
    }
)


class SetLoweringMixin:
    def _emit_require_native_set_operands(
        self,
        lhs: ir.Value,
        rhs: ir.Value,
        span,
    ) -> None:
        """Branch to TypeError unless both dynamic objects are native sets."""
        lhs_tag = self.builder.call(
            self.runtime["py_obj_type_tag"],
            [lhs],
            name=self._fresh("set.binop.lhs.tag"),
        )
        rhs_tag = self.builder.call(
            self.runtime["py_obj_type_tag"],
            [rhs],
            name=self._fresh("set.binop.rhs.tag"),
        )
        lhs_is_set = self.builder.icmp_signed(
            "==",
            lhs_tag,
            ir.Constant(_I64, PY_TYPE_SET),
            name=self._fresh("set.binop.lhs.ok"),
        )
        rhs_is_set = self.builder.icmp_signed(
            "==",
            rhs_tag,
            ir.Constant(_I64, PY_TYPE_SET),
            name=self._fresh("set.binop.rhs.ok"),
        )
        valid = self.builder.and_(
            lhs_is_set,
            rhs_is_set,
            name=self._fresh("set.binop.operands.ok"),
        )
        fn = self.current_function
        ok_bb = fn.append_basic_block(name=self._fresh("set.binop.ok"))
        bad_bb = fn.append_basic_block(name=self._fresh("set.binop.bad"))
        self.builder.cbranch(valid, ok_bb, bad_bb)
        self.builder.position_at_end(bad_bb)
        message = self._ptr_to_cstr(
            self._cstr_global(
                "set binary operator requires set operands",
                ".set.binop.typeerror",
            )
        )
        exc = self.builder.call(
            self.runtime["py_exc_new"],
            [ir.Constant(_I64, 3), message],
            name=self._fresh("set.binop.exc"),
        )
        self.builder.call(self.runtime["py_raise"], [exc])
        err_target = getattr(self, "_try_err_block", None) or self._ensure_fn_err_exit()
        self.builder.branch(err_target)
        self.builder.position_at_end(ok_bb)

    def _emit_checked_set_binary_values(
        self,
        op: str,
        lhs: ir.Value,
        rhs: ir.Value,
        span,
    ) -> ir.Value:
        self._emit_require_native_set_operands(lhs, rhs, span)
        if op == "|":
            return self._emit_set_union_values(lhs, rhs)
        if op == "&":
            return self.builder.call(
                self.runtime["py_set_intersection"],
                [lhs, rhs],
                name=self._fresh("set.intersection"),
            )
        if op == "-":
            return self.builder.call(
                self.runtime["py_set_difference"],
                [lhs, rhs],
                name=self._fresh("set.difference"),
            )
        return self.builder.call(
            self.runtime["py_set_symmetric_difference"],
            [lhs, rhs],
            name=self._fresh("set.symmetric_difference"),
        )

    def _emit_checked_set_inplace_values(
        self,
        op: str,
        lhs: ir.Value,
        rhs: ir.Value,
        span,
    ) -> ir.Value:
        self._emit_require_native_set_operands(lhs, rhs, span)
        if op == "|":
            helper = "py_set_update"
        elif op == "&":
            helper = "py_set_intersection_update"
        elif op == "-":
            helper = "py_set_difference_update"
        else:
            helper = "py_set_symmetric_difference_update"
        self.builder.call(self.runtime[helper], [lhs, rhs])
        self._emit_post_call_err_check(span)
        return self._gc_retain(lhs, name=self._fresh("set.inplace.retain"))

    def _dict_keys_view_receiver(self, expr: Expr) -> Optional[Expr]:
        """Return the mapping behind a statically known ``dict.keys()`` view.

        The current runtime materialises ``dict.keys()`` as an insertion-
        ordered list.  That is sufficient for iteration, but a Python keys
        view also implements set-like binary operators.  Preserve that
        distinction from the source expression instead of treating every
        runtime list as set-like.
        """
        if not isinstance(expr, Call) or expr.args or expr.kwargs:
            return None
        if not isinstance(expr.func, Attr) or expr.func.name != "keys":
            return None
        receiver = expr.func.obj
        if not isinstance(receiver.ty, DictType):
            return None
        return receiver

    def _materialize_dict_keys_view_set(self, receiver: Expr) -> ir.Value:
        mapping = self._emit_expr(receiver)
        mapping_obj = marshal.marshal_to_object(
            self.builder,
            self.module,
            self.runtime,
            mapping,
            receiver.ty,
        )
        keys = self.builder.call(
            self.runtime["py_dict_keys"],
            [mapping_obj],
            name=self._fresh("dict.keys.set.keys"),
        )
        out = self.builder.call(
            self.runtime["py_set_new"],
            [],
            name=self._fresh("dict.keys.set"),
        )
        length = self.builder.call(
            self.runtime["py_list_len"],
            [keys],
            name=self._fresh("dict.keys.set.len"),
        )
        index = self._alloca_in_entry(_I64, name="dict.keys.set.index.addr")
        self.builder.store(ir.Constant(_I64, 0), index)
        fn = self.current_function
        cond_bb = fn.append_basic_block(name=self._fresh("dict.keys.set.cond"))
        body_bb = fn.append_basic_block(name=self._fresh("dict.keys.set.body"))
        end_bb = fn.append_basic_block(name=self._fresh("dict.keys.set.end"))
        self.builder.branch(cond_bb)
        self.builder.position_at_end(cond_bb)
        current = self.builder.load(index, name=self._fresh("dict.keys.set.index"))
        keep_going = self.builder.icmp_signed(
            "<",
            current,
            length,
            name=self._fresh("dict.keys.set.keep"),
        )
        self.builder.cbranch(keep_going, body_bb, end_bb)
        self.builder.position_at_end(body_bb)
        item = self.builder.call(
            self.runtime["py_list_get"],
            [keys, current],
            name=self._fresh("dict.keys.set.item"),
        )
        self.builder.call(self.runtime["py_set_add"], [out, item])
        self._emit_post_call_err_check(getattr(receiver, "span", None))
        self._gc_release(item, self._release_context_label("dict.keys.set.item"))
        next_index = self.builder.add(
            current,
            ir.Constant(_I64, 1),
            name=self._fresh("dict.keys.set.next"),
        )
        self.builder.store(next_index, index)
        self.builder.branch(cond_bb)
        self.builder.position_at_end(end_bb)
        self._gc_release(keys, self._release_context_label("dict.keys.set.keys"))
        self._gc_release_if_owned(mapping_obj, receiver)
        return out

    def _maybe_emit_dict_keys_view_binop(self, expr) -> Optional[ir.Value]:
        """Lower set operators involving a real ``dict.keys()`` view.

        This is intentionally source-shape constrained: ordinary lists must
        continue to reject ``list | set``.  Both operands are materialised as
        native sets only when the other side is a statically known set or
        another keys view.
        """
        if expr.op not in ("|", "&", "-", "^"):
            return None
        lhs_receiver = self._dict_keys_view_receiver(expr.lhs)
        rhs_receiver = self._dict_keys_view_receiver(expr.rhs)
        if lhs_receiver is None and rhs_receiver is None:
            return None
        if lhs_receiver is None and not isinstance(expr.lhs.ty, SetType):
            return None
        if rhs_receiver is None and not isinstance(expr.rhs.ty, SetType):
            return None

        lhs = (
            self._materialize_dict_keys_view_set(lhs_receiver)
            if lhs_receiver is not None
            else self._emit_expr(expr.lhs)
        )
        rhs = (
            self._materialize_dict_keys_view_set(rhs_receiver)
            if rhs_receiver is not None
            else self._emit_expr(expr.rhs)
        )
        if expr.op == "|":
            result = self._emit_set_union_values(lhs, rhs)
        elif expr.op == "&":
            result = self.builder.call(
                self.runtime["py_set_intersection"],
                [lhs, rhs],
                name=self._fresh("dict.keys.intersection"),
            )
        elif expr.op == "-":
            result = self.builder.call(
                self.runtime["py_set_difference"],
                [lhs, rhs],
                name=self._fresh("dict.keys.difference"),
            )
        else:
            result = self.builder.call(
                self.runtime["py_set_symmetric_difference"],
                [lhs, rhs],
                name=self._fresh("dict.keys.symmetric_difference"),
            )
        if lhs_receiver is not None:
            self._gc_release(lhs, self._release_context_label("dict.keys.set.lhs"))
        else:
            self._gc_release_if_owned(lhs, expr.lhs)
        if rhs_receiver is not None:
            self._gc_release(rhs, self._release_context_label("dict.keys.set.rhs"))
        else:
            self._gc_release_if_owned(rhs, expr.rhs)
        return result

    def _maybe_emit_set_method_via_dyn(
        self,
        expr: Call,
    ) -> Optional[ir.Value]:
        attr = expr.func
        assert isinstance(attr, Attr)
        if attr.name not in _DYN_SET_METHOD_NATIVE:
            return None
        # Name alone does not make the receiver a set: a user class with an
        # ``add``/``update``/``pop`` method reaching py_set_* silently did
        # nothing.  Test the runtime tag and send anything else to generic
        # dispatch.
        return self._emit_dyn_container_method_with_tag_guard(
            expr,
            (PY_TYPE_SET,),
            lambda recv: self._maybe_emit_set_method(
                expr, recv=recv, recv_borrowed=True
            ),
            "dyn.set",
        )

    def _maybe_emit_set_method(
        self,
        expr: Call,
        recv: Optional[ir.Value] = None,
        recv_borrowed: bool = False,
    ) -> Optional[ir.Value]:
        """Dispatch selected pcc-native set methods.

        Set/frozenset values carry a first-class ``SetType`` projection.
        """
        attr = expr.func
        assert isinstance(attr, Attr)
        if expr.kwargs:
            return None
        name = attr.name
        if name not in (
            "add", "remove", "discard", "update", "issubset", "issuperset",
            "isdisjoint", "union", "intersection", "difference",
            "symmetric_difference", "intersection_update", "difference_update",
            "symmetric_difference_update", "copy", "pop",
        ):
            return None
        if name in ("copy", "pop"):
            if expr.args:
                return None
        elif len(expr.args) != 1:
            # The 1-arg form covers the common case; multi-arg union/
            # intersection/etc. fall back to the generic path.
            return None
        if recv is None:
            recv = self._emit_expr(attr.obj)
        if recv in getattr(self, "_cpy_values", ()):
            return self._emit_cpy_method_call_src(
                recv,
                name,
                expr.args,
                kwargs=expr.kwargs,
            )
        if name == "update":
            self._spread_into_set(recv, expr.args[0])
            return self._emit_none_literal()
        if name in (
            "intersection_update",
            "difference_update",
            "symmetric_difference_update",
        ):
            # In-place mutators: the runtime helper rewrites the receiver's
            # contents in place (preserving receiver identity so aliases see
            # the change) using the corresponding
            # py_set_intersection/difference/symmetric_difference result.
            # They return None, matching CPython.
            fn_name = {
                "intersection_update": "py_set_intersection_update",
                "difference_update": "py_set_difference_update",
                "symmetric_difference_update": "py_set_symmetric_difference_update",
            }[name]
            self.builder.call(
                self.runtime[fn_name],
                [recv, self._emit_as_object(expr.args[0])],
            )
            self._emit_post_call_err_check(expr.span)
            return self._emit_none_literal()
        if name in ("issubset", "issuperset"):
            fn_name = (
                "py_set_issubset" if name == "issubset" else "py_set_issuperset"
            )
            if not self._owned_release_needed(recv, attr.obj):
                recv = self._gc_retain(recv, name=self._fresh("set.predicate.receiver"))
            self._gc_pin(recv)
            other = self._emit_expr_with_cpy_operand_cleanup(
                expr.args[0], (), pinned_pcc=((recv, True),), as_object=True,
            )
            result = self.builder.call(
                self.runtime[fn_name],
                [recv, other],
                name=self._fresh(f"set.{name}"),
            )
            self._gc_release_if_owned(other, expr.args[0])
            self._gc_unpin(recv)
            self._gc_release(recv)
            self._emit_post_call_err_check(expr.span)
            return self.builder.icmp_signed(
                "!=",
                result,
                ir.Constant(_I64, 0),
                name=self._fresh(f"set.{name}.i1"),
            )
        if name in ("intersection", "difference", "symmetric_difference"):
            # Each returns a NEW set; the runtime helpers already do so.
            fn_name = {
                "intersection": "py_set_intersection",
                "difference": "py_set_difference",
                "symmetric_difference": "py_set_symmetric_difference",
            }[name]
            return self.builder.call(
                self.runtime[fn_name],
                [recv, self._emit_as_object(expr.args[0])],
                name=self._fresh(f"set.{name}"),
            )
        if name in ("union", "copy"):
            # ``a.union(b)`` / ``a.copy()`` build a NEW set: seed it from recv
            # (and, for union, the argument) via py_set_update.
            new_set = self.builder.call(
                self.runtime["py_set_new"], [], name=self._fresh(f"set.{name}.new"),
            )
            self.builder.call(self.runtime["py_set_update"], [new_set, recv])
            self._emit_post_call_err_check(expr.span)
            if name == "union":
                self.builder.call(
                    self.runtime["py_set_update"],
                    [new_set, self._emit_as_object(expr.args[0])],
                )
                self._emit_post_call_err_check(expr.span)
            return new_set
        if name == "pop":
            result = self.builder.call(
                self.runtime["py_set_pop"],
                [recv],
                name=self._fresh("set.pop"),
            )
            self._emit_post_call_err_check(expr.span)
            return result
        if name == "isdisjoint":
            inter = self.builder.call(
                self.runtime["py_set_intersection"],
                [recv, self._emit_as_object(expr.args[0])],
                name=self._fresh("set.isdisjoint.inter"),
            )
            n_val = self.builder.call(
                self.runtime["py_obj_len"],
                [inter],
                name=self._fresh("set.isdisjoint.len"),
            )
            return self.builder.icmp_signed(
                "==",
                n_val,
                ir.Constant(_I64, 0),
                name=self._fresh("set.isdisjoint.i1"),
            )
        # Borrowed receiver: the dyn tag guard evaluated it and releases it
        # on every path.  See the same note in _emit_owned_dict_get -- taking
        # ownership here as well freed the set one reference early.
        recv_owned = (
            False if recv_borrowed else self._owned_release_needed(recv, attr.obj)
        )
        recv_root = self._enter_container_temp_root(recv, self._fresh("set.receiver"))
        item = self._emit_expr_with_cpy_operand_cleanup(
            expr.args[0], (), as_pcc_object=True,
            rooted_pcc_lifetimes=((recv_root, recv_owned),),
        )
        item_owned = self._owned_release_needed(item, expr.args[0])
        item_root = self._enter_container_temp_root(item, self._fresh("set.item"))
        recv = self.builder.call(self.runtime["pcc_gc_load_ptr"],
            [ir.Constant(_CSTR, None), self._as_gc_ptr(recv_root)])
        item = self.builder.call(self.runtime["pcc_gc_load_ptr"],
            [ir.Constant(_CSTR, None), self._as_gc_ptr(item_root)])
        if name == "add":
            self.builder.call(self.runtime["py_set_add"], [recv, item])
        else:
            removed = self.builder.call(self.runtime["py_set_remove"], [recv, item],
                                        name=self._fresh("set.remove"))
            if name == "remove":
                # Preserve a hash/equality failure. Only absence without a
                # pending exception creates KeyError, carrying the actual key.
                pending = self.builder.call(self.runtime["py_err_occurred"], [])
                missing = self.builder.and_(
                    self.builder.icmp_signed("<", removed, ir.Constant(_I64, 0)),
                    self.builder.icmp_signed("==", pending, ir.Constant(_I64, 0)),
                )
                miss_bb = self.current_function.append_basic_block(self._fresh("set.remove.miss"))
                end_bb = self.current_function.append_basic_block(self._fresh("set.remove.end"))
                self.builder.cbranch(missing, miss_bb, end_bb)
                self.builder.position_at_end(miss_bb)
                key = self.builder.call(self.runtime["pcc_gc_load_ptr"],
                    [ir.Constant(_CSTR, None), self._as_gc_ptr(item_root)])
                exc = self.builder.call(self.runtime["py_exc_new_with_value"],
                    [ir.Constant(_I64, 4), key], name=self._fresh("set.remove.exc"))
                self.builder.call(self.runtime["py_raise"], [exc])
                self._gc_release(exc)
                self.builder.branch(end_bb)
                self.builder.position_at_end(end_bb)
        # Reload through the rooted slots before releasing: a hash/equality
        # callback may collect, move objects or replace their original binding.
        self._release_rooted_pcc_lifetimes(
            ((recv_root, recv_owned), (item_root, item_owned)),
        )
        self._emit_post_call_err_check(expr.span)
        return self._emit_none_literal()

    def _maybe_emit_set_builtin(self, expr: Call) -> Optional[ir.Value]:
        """``set()`` / ``set([a, b])`` / ``set((a, b, c))`` / ``set(iterable)``.

        - no args → empty ``py_set_new``.
        - literal list/tuple → allocate + add each element.
        - non-literal arg → the runtime iterator protocol, including mapping
          keys and generators, with the same behavior for every type hint.
        """
        if expr.args and not isinstance(expr.args[0], (ListExpr, TupleExpr)):
            arg = expr.args[0]
            src = self._emit_as_object(arg)
            owned = self._owned_release_needed(src, arg)
            self._gc_pin(src)
            result = self.builder.call(
                self.runtime["py_set_from_iterable"], [src],
                name=self._fresh("set.from.iterable"),
            )
            self._emit_post_call_err_check(
                expr.span, pinned_release_on_error=((src, owned),),
            )
            # Releasing a temporary iterator may run its finalizer. Keep the
            # new set alive while balancing that argument's owner.
            root = self._enter_container_temp_root(result, self._fresh("set.result"))
            self._gc_unpin(src)
            self._gc_release_if_owned(src, arg)
            self._leave_container_temp_root(root)
            self._note_owned_object_value(result)
            return result
        new_set = self.builder.call(
            self.runtime["py_set_new"],
            [],
            name=self._fresh("set.new"),
        )
        if not expr.args:
            return new_set
        arg = expr.args[0]
        if isinstance(arg, (ListExpr, TupleExpr)):
            root = self._enter_container_temp_root(new_set, self._fresh("set.literal"))
            old_err = self._current_try_err_block()
            old_cpy_err = getattr(self, "_cpy_operand_cleanup_block", None)
            target = old_err if old_err is not None else self._ensure_fn_err_exit()
            self._try_err_block = self._make_cpy_operand_cleanup_block(
                (), (), target, "set.literal.error", rooted_pcc_lifetimes=((root, True),),
            )
            self._cpy_operand_cleanup_block = self._try_err_block
            try:
                for el in arg.elems:
                    if (
                        isinstance(el, Call)
                        and isinstance(el.func, Name)
                        and el.func.ident in ("*", "__starred__")
                        and len(el.args) == 1
                    ):
                        self._spread_into_set(new_set, el.args[0])
                        continue
                    # The same retaining-insertion contract as a set
                    # comprehension, including operand/error cleanup.
                    self._emit_comprehension_innermost("set", new_set, el, None, None)
            finally:
                self._try_err_block = old_err
                self._cpy_operand_cleanup_block = old_cpy_err
            self._leave_container_temp_root(root)
            return new_set
        return None

    def _emit_set_union_values(
        self,
        lhs: ir.Value,
        rhs: ir.Value,
    ) -> ir.Value:
        new_set = self.builder.call(
            self.runtime["py_set_new"],
            [],
            name=self._fresh("set.union"),
        )
        self.builder.call(self.runtime["py_set_update"], [new_set, lhs])
        self._emit_post_call_err_check()
        self.builder.call(self.runtime["py_set_update"], [new_set, rhs])
        self._emit_post_call_err_check()
        return new_set
    def _spread_into_set(self, dst_set: ir.Value, src_expr: "Expr") -> None:
        """Update from the actual iterator protocol, independent of type hints."""
        dst_root = self._enter_container_temp_root(dst_set, self._fresh("set.spread.dst"))
        src = self._emit_expr_with_cpy_operand_cleanup(
            src_expr, (), as_pcc_object=True,
            rooted_pcc_lifetimes=((dst_root, False),),
        )
        owned = self._owned_release_needed(src, src_expr)
        src_root = self._enter_container_temp_root(src, self._fresh("set.spread.src"))
        dst = self.builder.call(self.runtime["pcc_gc_load_ptr"],
            [ir.Constant(_CSTR, None), self._as_gc_ptr(dst_root)])
        src = self.builder.call(self.runtime["pcc_gc_load_ptr"],
            [ir.Constant(_CSTR, None), self._as_gc_ptr(src_root)])
        self.builder.call(self.runtime["py_set_update"], [dst, src])
        self._release_rooted_pcc_lifetimes(((dst_root, False), (src_root, owned)))
        self._emit_post_call_err_check(getattr(src_expr, "span", None))
