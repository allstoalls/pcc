"""Set lowering helpers for L1CodeGen."""
from __future__ import annotations

from typing import Optional

from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import Attr, Call, ClassType, DictType, DynType, Expr, ListExpr, ListType, Name, SetType, StrType, TupleExpr, TupleType
from pcc.frontends.python.codegen import marshal
from pcc.frontends.python.codegen.freestanding_abi_constants import PY_TYPE_SET
from pcc.frontends.python.codegen.errors import L1CodegenError


_I1 = ir.IntType(1)
_I64 = ir.IntType(64)
_CSTR = ir.IntType(8).as_pointer()

# Shared with py_set_call_method_slots. All these methods bind positional
# iterables at runtime, including the zero-argument and expanded-call forms.
_SET_ALGEBRA_METHODS = {
    "union": 0,
    "intersection": 1,
    "difference": 2,
    "update": 3,
    "intersection_update": 4,
    "difference_update": 5,
    "symmetric_difference": 6,
    "symmetric_difference_update": 7,
}

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
    def _set_call_operands(self, expr: Call):
        """Separate positional splats from the ordered keyword merge stream."""
        positional = []
        mappings = []
        for index, argument in enumerate(expr.args):
            if (isinstance(argument, Call) and isinstance(argument.func, Name)
                    and argument.func.ident == "**" and len(argument.args) == 1):
                mappings.append(index)
            else:
                positional.append(argument)
        if not mappings:
            return tuple(positional), expr.kwargs
        keywords = []
        if expr.operand_order:
            for kind, index in expr.operand_order:
                if kind == "kw":
                    keywords.append(expr.kwargs[index])
                elif index in mappings:
                    keywords.append(("**", expr.args[index].args[0]))
        else:
            if expr.kwargs:
                raise L1CodegenError("set unpack is missing keyword operand-order metadata")
            for index in mappings:
                keywords.append(("**", expr.args[index].args[0]))
        return tuple(positional), tuple(keywords)

    def _emit_set_algebra_call(
        self, expr: Call, dynamic: bool = False, *, receiver_slot=None, output_slot=None,
        algebra_method=None,
    ):
        """Evaluate once, bind expanded arguments, and preserve slot ownership.

        ``algebra_method`` overrides the operation named by the call (copy is
        union with no arguments); generic dispatch still looks up the name.
        """
        attr = expr.func
        assert isinstance(attr, Attr)
        if self._expr_looks_cpython(attr.obj):
            return self._emit_cpy_method_call_src(
                self._emit_expr(attr.obj), attr.name, expr.args,
                kwargs=expr.kwargs, operand_order=expr.operand_order,
            )
        positional, keywords = self._set_call_operands(expr)
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        # The output is the oldest root, so argument teardown also respects
        # module-scope LIFO frames. Runtime publishes into this empty slot.
        sink = output_slot if output_slot is not None else self._slot_call_result_sink(expr)
        result_root = sink
        roots = []
        if result_root is None:
            result_root = self._new_slot_call_root("set.call.result")
            roots.append(result_root)
        result_root_count = len(roots)
        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            receiver_root = receiver_slot
            if receiver_root is None:
                receiver_root = self._emit_slot_call_operand(attr.obj, "set.call.receiver")
                roots.append(receiver_root)
            else:
                # The shared dict/set update owner already evaluated it. A
                # registered slot, never its earlier raw load, crosses here.
                self._slot_call_root_record(receiver_root)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            is_set = None
            method_root = None
            if dynamic:
                method_root = self._new_slot_call_root("set.call.generic.method")
                roots.append(method_root)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                tag = self._slot_call_runtime_call(
                    "py_obj_type_tag", (receiver_root,), span=expr.span,
                )
                is_set = self.builder.icmp_signed("==", tag, ir.Constant(_I64, PY_TYPE_SET))
                lookup = self.current_function.append_basic_block(self._fresh("set.call.generic.lookup"))
                ready = self.current_function.append_basic_block(self._fresh("set.call.lookup.ready"))
                self.builder.cbranch(is_set, ready, lookup)
                self.builder.position_at_end(lookup)
                # Python resolves the method before evaluating any argument.
                # A class merely sharing a set method's name stays generic.
                self._slot_call_runtime_call(
                    "py_obj_getattr", (receiver_root,), result_slot=method_root,
                    suffix_args=(self._attr_name_ptr(attr.name),), span=expr.span,
                )
                method = self.builder.load(method_root, name=self._fresh("set.call.generic.method.present"))
                self._emit_attribute_error_if_null(method, attr.name, attr.span)
                self.builder.branch(ready)
                self.builder.position_at_end(ready)
            args_root = self._emit_slot_call_args_tuple(positional, "set.call.args")
            roots.append(args_root)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            kwargs_root = self._emit_slot_call_kwargs_object(
                keywords, None, expr.span, "set.call.kwargs",
            )
            roots.append(kwargs_root)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            done = None
            if dynamic:
                native = self.current_function.append_basic_block(self._fresh("set.call.native"))
                generic = self.current_function.append_basic_block(self._fresh("set.call.generic"))
                done = self.current_function.append_basic_block(self._fresh("set.call.done"))
                self.builder.cbranch(is_set, native, generic)
                self.builder.position_at_end(generic)
                status = self.builder.call(self.runtime["py_obj_call_slots"], [
                    self._as_gc_ptr(method_root), self._as_gc_ptr(args_root),
                    self._as_gc_ptr(kwargs_root), self._as_gc_ptr(result_root),
                ], name=self._fresh("set.call.generic.status"))
                self._slot_call_note_published(result_root)
                self._emit_post_call_err_check(expr.span)
                self._slot_call_check_status(status, "generic method call", expr.span)
                self.builder.branch(done)
                self.builder.position_at_end(native)
            method_id = (_SET_ALGEBRA_METHODS[attr.name] if algebra_method is None
                         else algebra_method)
            status = self.builder.call(self.runtime["py_set_call_method_slots"], [
                self._as_gc_ptr(receiver_root),
                ir.Constant(_I64, method_id),
                self._as_gc_ptr(args_root), self._as_gc_ptr(kwargs_root),
                self._as_gc_ptr(result_root),
            ], name=self._fresh("set.call.status"))
            self._slot_call_note_published(result_root)
            self._emit_post_call_err_check(expr.span)
            self._slot_call_check_status(status, "set method call", expr.span)
            if done is not None:
                self.builder.branch(done)
                self.builder.position_at_end(done)
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
        self._release_slot_call_roots(tuple(roots[result_root_count:]))
        if sink is not None:
            return self.builder.load(result_root, name=self._fresh("set.call.current"))
        result = self._take_slot_call_root(result_root)
        self._note_owned_dynamic_call_value(result)
        return result

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
        if attr.name in _SET_ALGEBRA_METHODS:
            return self._emit_set_algebra_call(expr, dynamic=True)
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
        if attr.name in _SET_ALGEBRA_METHODS:
            # Algebra never treats a * / ** parser marker as one iterable.
            # The dynamic owner routes here before evaluating its receiver.
            if recv is not None:
                raise L1CodegenError("set algebra requires an authoritative receiver slot")
            return self._emit_set_algebra_call(expr)
        if self._has_unpacked_operands(expr.args, expr.kwargs):
            return None  # the generic method call binds unpacked arguments
        if attr.name == "copy" and not expr.args and not expr.kwargs and recv is None:
            # set.copy() is union() with no arguments: a new set holding the
            # receiver's items, built and published by the slot-owned algebra
            # call. A bare py_set_new result was unrooted across py_set_update
            # and could not be handed to an operand slot (sorted(s.copy())).
            return self._emit_set_algebra_call(
                expr, algebra_method=_SET_ALGEBRA_METHODS["union"],
            )
        if expr.kwargs:
            return None
        name = attr.name
        if name not in (
            "add", "remove", "discard", "issubset", "issuperset",
            "isdisjoint", "copy", "pop", "clear",
        ):
            return None
        if name in ("copy", "pop", "clear"):
            if expr.args:
                return None
        elif len(expr.args) != 1:
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
        if name == "copy":
            new_set = self.builder.call(
                self.runtime["py_set_new"], [], name=self._fresh(f"set.{name}.new"),
            )
            self.builder.call(self.runtime["py_set_update"], [new_set, recv])
            self._emit_post_call_err_check(expr.span)
            return new_set
        if name == "clear":
            self.builder.call(self.runtime["py_set_clear"], [recv])
            if not recv_borrowed:
                self._gc_release_if_owned(recv, attr.obj)
            self._emit_post_call_err_check(expr.span)
            return self._emit_none_literal()
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

    def _emit_owned_set_constructor(self, expr):
        """Publish a native zero/one-argument constructor before any cleanup."""
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        sink = self._slot_call_result_sink(expr)
        output = sink
        roots = []
        if output is None:
            output = self._new_slot_call_root("set.constructor.result")
            roots.append(output)
        operands = []
        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            if expr.args:
                argument = self._emit_slot_call_operand(expr.args[0], "set.constructor.argument")
                operands.append(argument)
                roots.append(argument)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
            runtime_name = "py_set_from_iterable" if operands else "py_set_new"
            self._slot_call_runtime_call(runtime_name, tuple(operands), result_slot=output,
                                         span=expr.span)
            self._release_slot_call_roots(tuple(operands))
            if sink is None:
                return self._take_slot_call_root(output)
            return self.builder.load(output, name=self._fresh("set.constructor.current"))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy

    def _maybe_emit_set_builtin(self, expr: Call) -> Optional[ir.Value]:
        """``set()`` / ``set([a, b])`` / ``set((a, b, c))`` / ``set(iterable)``.

        - no args → empty ``py_set_new``.
        - syntax-marked set display → allocate + add each element.
        - explicit argument, including a literal list/tuple → evaluate the
          complete argument before the constructor starts hashing members.
          Then use the runtime iterator protocol for every type hint.
        """
        if (not expr.is_set_literal and not expr.kwargs and len(expr.args) <= 1
                and (not expr.args or not self._expr_looks_cpython(expr.args[0]))):
            return self._emit_owned_set_constructor(expr)
        if expr.args and not expr.is_set_literal:
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
