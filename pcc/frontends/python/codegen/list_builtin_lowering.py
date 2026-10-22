"""List builtin lowering helpers for L1CodeGen."""

from __future__ import annotations

from typing import Optional

from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import (
    Call,
    DictType,
    DynType,
    Lambda,
    Name,
    NoneLit,
)
from pcc.frontends.python.codegen.freestanding_abi_constants import PY_TYPE_TUPLE
from pcc.frontends.python.codegen import marshal

_I64 = ir.IntType(64)
_CSTR = ir.IntType(8).as_pointer()


class ListBuiltinLoweringMixin:
    def _emit_reversed_builtin(self, expr: Call) -> ir.Value:
        """Publish the existing materialized reverse before fill or teardown."""
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        sink = self._slot_call_result_sink(expr)
        output = sink
        roots = []
        if output is None:
            output = self._new_slot_call_root("reversed.result")
            roots.append(output)
        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            source = self._emit_slot_call_operand(expr.args[0], "reversed.source")
            roots.append(source)
            index = self._new_slot_call_root("reversed.index")
            item = self._new_slot_call_root("reversed.item")
            roots.extend((index, item))
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            if isinstance(expr.args[0].ty, DictType):
                keys = self._new_slot_call_root("reversed.keys")
                roots.append(keys)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                self._slot_call_runtime_call(
                    "py_dict_keys", (source,), result_slot=keys, span=expr.span,
                )
                source = keys
            length = self._slot_call_runtime_call("py_obj_len", (source,), span=expr.span)
            self._slot_call_runtime_call(
                "py_list_new", (), result_slot=output,
                suffix_args=(length,), span=expr.span,
            )
            counter = self._alloca_in_entry(_I64, name="reversed.index.addr")
            self.builder.store(self.builder.sub(length, ir.Constant(_I64, 1)), counter)
            cond = self.current_function.append_basic_block(self._fresh("reversed.cond"))
            body = self.current_function.append_basic_block(self._fresh("reversed.body"))
            done = self.current_function.append_basic_block(self._fresh("reversed.done"))
            self.builder.branch(cond)
            self.builder.position_at_end(cond)
            current = self.builder.load(counter)
            self.builder.cbranch(
                self.builder.icmp_signed(">=", current, ir.Constant(_I64, 0)), body, done,
            )
            self.builder.position_at_end(body)
            self._slot_call_runtime_call(
                "py_int_from_i64", (), result_slot=index,
                suffix_args=(current,), span=expr.span,
            )
            self._slot_call_runtime_call(
                "py_obj_getitem", (source, index), result_slot=item, span=expr.span,
            )
            self._slot_call_runtime_call("py_list_append", (output, item), span=expr.span)
            # These frames span the loop (and may sit below the keys frame).
            # Dispose iteration values without unregistering reusable slots.
            for slot in (item, index):
                self.builder.call(
                    self.runtime["pcc_gc_store_root"],
                    [self._as_gc_ptr(slot), ir.Constant(_CSTR, None)],
                )
            self.builder.store(self.builder.sub(current, ir.Constant(_I64, 1)), counter)
            self.builder.branch(cond)
            self.builder.position_at_end(done)
            self._release_slot_call_roots(tuple(roots[1:]) if sink is None else tuple(roots))
            if sink is None:
                return self._take_slot_call_root(output)
            return self.builder.load(output, name=self._fresh("reversed.current"))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy

    def _maybe_emit_list_builtin(
        self,
        expr: Call,
        tuple_result: bool = False,
    ) -> Optional[ir.Value]:
        """Build a sequence in a caller-owned output before any cleanup.

        Every runtime operand is independently owned. A list always copies;
        tuple's exact-tuple edge keeps identity with a second owned reference.
        General iteration and partially-filled list cleanup belong to extend;
        the tuple conversion consumes neither its list nor its elements.
        """
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        sink = self._slot_call_result_sink(expr)
        output = sink
        roots = []
        label = "tuple.builtin" if tuple_result else "list.builtin"
        if output is None:
            output = self._new_slot_call_root(label + ".result")
            roots.append(output)
        try:
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            if not expr.args:
                self._slot_call_runtime_call(
                    "py_tuple_new" if tuple_result else "py_list_new", (),
                    result_slot=output, suffix_args=(ir.Constant(_I64, 0),),
                    span=expr.span,
                )
            else:
                arg = expr.args[0]
                # The specialized map/filter producer owns the same output
                # transaction; it reports a match before the general operand
                # path tries to resolve map/filter as ordinary callables.
                sequence = output
                if tuple_result:
                    sequence = self._new_slot_call_root(label + ".items")
                    roots.append(sequence)
                    self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                    self._cpy_operand_cleanup_block = self._try_err_block
                mapped = None
                if isinstance(arg, Call):
                    mapped = self._maybe_emit_list_from_map_filter(arg, sequence)
                if mapped is not None:
                    if tuple_result:
                        self._slot_call_runtime_call(
                            "py_tuple_from_list", (sequence,), result_slot=output,
                            span=expr.span,
                        )
                else:
                    source = self._emit_slot_call_operand(arg, label + ".source")
                    roots.append(source)
                    self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                    self._cpy_operand_cleanup_block = self._try_err_block
                    done = None
                    if tuple_result:
                        tag = self._slot_call_runtime_call(
                            "py_obj_type_tag", (source,), span=expr.span,
                        )
                        exact_tuple = self.builder.icmp_signed(
                            "==", tag, ir.Constant(_I64, PY_TYPE_TUPLE),
                        )
                        alias = self.current_function.append_basic_block(self._fresh(label + ".alias"))
                        copy = self.current_function.append_basic_block(self._fresh(label + ".copy"))
                        done = self.current_function.append_basic_block(self._fresh(label + ".ready"))
                        self.builder.cbranch(exact_tuple, alias, copy)
                        self.builder.position_at_end(alias)
                        self._slot_call_copy_source(output, source, span=expr.span)
                        self._slot_call_note_published(output)
                        self.builder.branch(done)
                        self.builder.position_at_end(copy)
                    self._slot_call_runtime_call(
                        "py_list_new", (), result_slot=sequence,
                        suffix_args=(ir.Constant(_I64, 0),), span=expr.span,
                    )
                    self._slot_call_runtime_call(
                        "py_list_extend", (sequence, source), span=expr.span,
                    )
                    if tuple_result:
                        self._slot_call_runtime_call(
                            "py_tuple_from_list", (sequence,), result_slot=output,
                            span=expr.span,
                        )
                        self.builder.branch(done)
                        self.builder.position_at_end(done)
                self._release_slot_call_roots(tuple(roots[1:]) if sink is None else tuple(roots))
            if sink is None:
                return self._take_slot_call_root(output)
            return self.builder.load(output, name=self._fresh(label + ".current"))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy

    def _emit_list_append_via_iter(self, new_list, src_obj, span, *, source_owned=False):
        """Append every item of an iterable to ``new_list`` via the iterator
        protocol (``py_obj_iter`` / ``py_obj_next``). Used for DynType sources
        such as generators that have no length / ``__getitem__``. Mirrors the
        statement for-loop and the comprehension obj-iterator path, clearing a
        terminal StopIteration (tag 8) and propagating any other exception.

        The caller transfers a fresh output list. On success its owner is
        returned; on error it is released with the owned iterator. Each next
        result owns one reference in addition to the list's append retain.
        """
        fn = self.current_function
        self._gc_pin(new_list)
        source_cleanup = ()
        if source_owned:
            self._gc_pin(src_obj)
            source_cleanup = ((src_obj, True),)
        iterator = self.builder.call(
            self.runtime["py_obj_iter"],
            [src_obj],
            name=self._fresh("list.iter.obj"),
        )
        self._emit_post_call_err_check(
            span,
            release_on_error=(iterator,),
            pinned_release_on_error=((new_list, True),) + source_cleanup,
        )
        self._gc_pin(iterator)
        header_bb = fn.append_basic_block(name=self._fresh("list.iter.next"))
        body_bb = fn.append_basic_block(name=self._fresh("list.iter.body"))
        maybe_end_bb = fn.append_basic_block(name=self._fresh("list.iter.maybe_end"))
        clear_bb = fn.append_basic_block(name=self._fresh("list.iter.clear"))
        propagate_bb = fn.append_basic_block(name=self._fresh("list.iter.propagate"))
        end_bb = fn.append_basic_block(name=self._fresh("list.iter.end"))
        self.builder.branch(header_bb)
        self.builder.position_at_end(header_bb)
        item = self.builder.call(
            self.runtime["py_obj_next"],
            [iterator],
            name=self._fresh("list.iter.item"),
        )
        is_null = self.builder.icmp_unsigned(
            "==",
            item,
            ir.Constant(_CSTR, None),
            name=self._fresh("list.iter.null"),
        )
        self.builder.cbranch(is_null, maybe_end_bb, body_bb)
        self.builder.position_at_end(body_bb)
        self.builder.call(self.runtime["py_list_append"], [new_list, item])
        self._gc_release(item)
        self._emit_post_call_err_check(
            span,
            pinned_release_on_error=((iterator, True), (new_list, True)) + source_cleanup,
        )
        self.builder.branch(header_bb)
        self.builder.position_at_end(maybe_end_bb)
        current_exc = self.builder.call(
            self.runtime["py_current_exception"],
            [],
            name=self._fresh("list.iter.cur_exc"),
        )
        stop_cls = self.builder.call(
            self.runtime["py_exc_builtin_class"],
            [ir.Constant(_I64, 8)],  # StopIteration
            name=self._fresh("list.iter.stop_cls"),
        )
        match_i64 = self.builder.call(
            self.runtime["py_exc_matches"],
            [current_exc, stop_cls],
            name=self._fresh("list.iter.stop_match"),
        )
        is_stop = self.builder.icmp_signed(
            "!=",
            match_i64,
            ir.Constant(_I64, 0),
            name=self._fresh("list.iter.stop_i1"),
        )
        self.builder.cbranch(is_stop, clear_bb, propagate_bb)
        self.builder.position_at_end(clear_bb)
        self.builder.call(self.runtime["py_clear_exception"], [])
        self.builder.branch(end_bb)
        self.builder.position_at_end(propagate_bb)
        self._gc_unpin(iterator)
        self._gc_release(iterator)
        self._gc_unpin(new_list)
        self._gc_release(new_list)
        if source_owned:
            self._gc_unpin(src_obj)
            self._gc_release(src_obj)
        err_target = getattr(self, "_try_err_block", None)
        if err_target is None:
            err_target = self._ensure_fn_err_exit()
        self.builder.branch(err_target)
        self.builder.position_at_end(end_bb)
        self._gc_unpin(iterator)
        self._gc_release(iterator)
        if source_owned:
            self._gc_unpin(src_obj)
            self._gc_release(src_obj)
        self._gc_unpin(new_list)
        return new_list

    def _maybe_emit_list_from_map_filter(
        self,
        call: Call,
        out_list: ir.Value,
    ) -> Optional[ir.Value]:
        # ``filter(None, iterable)`` keeps the truthy elements (no predicate
        # function); args[0] is a NoneLit, not a Name.
        filter_none = (
            isinstance(call.func, Name)
            and call.func.ident == "filter"
            and not call.kwargs
            and len(call.args) == 2
            and isinstance(call.args[0], NoneLit)
        )
        # ``map``/``filter`` with an inline ``lambda`` (1 positional param):
        # bind the param to each element and emit the lambda body inline (it
        # closes over the current scope). args[0] is a Lambda, not a Name.
        lam = None
        if (
            not filter_none
            and isinstance(call.func, Name)
            and call.func.ident in ("map", "filter")
            and not call.kwargs
            and len(call.args) == 2
            and isinstance(call.args[0], Lambda)
            and len(call.args[0].params) == 1
            and call.args[0].params[0].kind in ("pos", "pos_only")
        ):
            lam = call.args[0]
        if not filter_none and lam is None:
            if (
                not isinstance(call.func, Name)
                or call.func.ident not in ("map", "filter")
                or call.kwargs
                or len(call.args) != 2
                or not isinstance(call.args[0], Name)
            ):
                return None
        mode = call.func.ident
        if filter_none or lam is not None:
            func_name = ""
            fn = None
            builtin_map_str = False
        else:
            func_name = call.args[0].ident
            fn = self.functions.get(func_name)
            builtin_map_str = mode == "map" and func_name == "str"
            builtin_map_chr = mode == "map" and func_name == "chr"
            if fn is None and not builtin_map_str and not builtin_map_chr:
                return None
        if filter_none or lam is not None:
            builtin_map_chr = False
        ast_fd = self._find_user_funcdef(func_name) if fn is not None else None
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        roots = []
        lam_param = lam.params[0].name if lam is not None else ""
        lam_had_binding = lam_param in self.env
        lam_saved = self.env.get(lam_param)
        try:
            source = self._emit_slot_call_operand(call.args[1], mode + ".source")
            roots.append(source)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            self._slot_call_runtime_call(
                "py_list_new", (), result_slot=out_list,
                suffix_args=(ir.Constant(_I64, 0),), span=call.span,
            )
            n_val = self._slot_call_runtime_call("py_obj_len", (source,), span=call.span)
            idx_slot = self._alloca_in_entry(_I64, name=mode + ".idx.addr")
            self.builder.store(ir.Constant(_I64, 0), idx_slot)
            # Loop temporaries are registered once and cleared each iteration.
            # Their physical slots survive lambda/callee reentry and collection.
            index = self._new_slot_call_root(mode + ".index")
            item = self._new_slot_call_root(mode + ".item")
            roots.extend((index, item))
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            cond = self.current_function.append_basic_block(self._fresh(mode + ".cond"))
            body = self.current_function.append_basic_block(self._fresh(mode + ".body"))
            step = self.current_function.append_basic_block(self._fresh(mode + ".step"))
            end = self.current_function.append_basic_block(self._fresh(mode + ".end"))
            self.builder.branch(cond)
            self.builder.position_at_end(cond)
            current = self.builder.load(idx_slot, name=self._fresh(mode + ".index"))
            keep_going = self.builder.icmp_signed("<", current, n_val)
            self.builder.cbranch(keep_going, body, end)
            self.builder.position_at_end(body)
            self._slot_call_runtime_call(
                "py_int_from_i64", (), result_slot=index,
                suffix_args=(current,), span=call.span,
            )
            self._slot_call_runtime_call(
                "py_obj_getitem", (source, index), result_slot=item, span=call.span,
            )
            result = item
            if lam is not None:
                self.env[lam_param] = (item, _CSTR, DynType(name="dyn"))
                result = self._emit_slot_call_operand(lam.body, mode + ".result")
            elif fn is not None:
                temp_name = self._fresh("__pcc_" + mode + "_item")
                self.env[temp_name] = (item, _CSTR, DynType(name="dyn"))
                argument = Name(span=call.span, ty=DynType(name="dyn"), ident=temp_name)
                invocation = Call(
                    span=call.span, ty=ast_fd.return_ty or DynType(name="dyn"),
                    func=Name(span=call.span, ty=call.args[0].ty, ident=func_name),
                    args=(argument,), kwargs=(),
                )
                try:
                    result = self._emit_slot_call_operand(invocation, mode + ".result")
                finally:
                    self.env.pop(temp_name, None)
            elif not filter_none:
                result = self._new_slot_call_root(mode + ".result")
                roots.append(result)
                self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
                self._cpy_operand_cleanup_block = self._try_err_block
                if builtin_map_chr:
                    codepoint = self._slot_call_runtime_call("py_obj_index_i64", (item,), span=call.span)
                    self._slot_call_runtime_call(
                        "py_chr_from_i64", (), result_slot=result,
                        suffix_args=(codepoint,), span=call.span,
                    )
                else:
                    self._slot_call_runtime_call("py_obj_str", (item,), result_slot=result, span=call.span)
            if result is not item and result not in roots:
                roots.append(result)
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            if mode == "map":
                self._slot_call_runtime_call("py_list_append", (out_list, result), span=call.span)
                self.builder.branch(step)
            else:
                truth = self._slot_call_runtime_call("py_obj_truthy", (result,), span=call.span)
                keep = self.builder.icmp_signed("!=", truth, ir.Constant(_I64, 0))
                append = self.current_function.append_basic_block(self._fresh("filter.keep"))
                self.builder.cbranch(keep, append, step)
                self.builder.position_at_end(append)
                self._slot_call_runtime_call("py_list_append", (out_list, item), span=call.span)
                self.builder.branch(step)
            self.builder.position_at_end(step)
            if result is not item:
                self._release_slot_call_roots((result,))
                roots.pop()
            # Keep these two root registrations live around the loop; clearing
            # retires their owners, but does not pop module LIFO root frames.
            for root in (item, index):
                self.builder.call(
                    self.runtime["pcc_gc_store_root"],
                    [self._as_gc_ptr(root), ir.Constant(_CSTR, None)],
                )
            self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
            self._cpy_operand_cleanup_block = self._try_err_block
            self.builder.store(self.builder.add(current, ir.Constant(_I64, 1)), idx_slot)
            self.builder.branch(cond)
            self.builder.position_at_end(end)
            self._release_slot_call_roots(tuple(roots))
            return out_list
        finally:
            if lam is not None:
                if lam_had_binding:
                    self.env[lam_param] = lam_saved
                else:
                    self.env.pop(lam_param, None)
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
