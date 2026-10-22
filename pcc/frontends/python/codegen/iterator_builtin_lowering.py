"""Iterator builtin lowering through authoritative operand and result owners."""
from __future__ import annotations

from typing import Optional

from pcc.ir.compat import ir

from pcc.frontends.python.py_ast import Call, Name, NoneLit, NoneType


_I64 = ir.IntType(64)
_CSTR = ir.IntType(8).as_pointer()
_STOP_ITERATION_TAG = 8


class IteratorBuiltinLoweringMixin:
    def _iterator_builtin_is_shadowed(self, name):
        return (
            name in self.env or name in self._module_globals or name in self.functions
            or name in getattr(getattr(self, "class_lowering", None), "classes", {})
        )

    def _emit_iterator_callable_operand(self, expr, label):
        previous = self._prefer_native_callable_values
        self._prefer_native_callable_values = True
        try:
            return self._emit_slot_call_operand(expr, label)
        finally:
            self._prefer_native_callable_values = previous

    def _iterator_install_cleanup(self, roots, target):
        self._try_err_block = self._slot_call_cleanup_block(tuple(roots), target)
        self._cpy_operand_cleanup_block = self._try_err_block

    def _maybe_emit_iter_builtin(self, expr: Call) -> Optional[ir.Value]:
        if self._iterator_builtin_is_shadowed("iter") or expr.kwargs or not (1 <= len(expr.args) <= 2):
            return None
        if len(expr.args) == 1:
            # py_obj_iter returns a NEW owner, including when __iter__ returns
            # its receiver. Publish before any check or operand retirement.
            return self._emit_owned_unary_runtime_call(expr, "py_obj_iter")
        return self._emit_iter_callable_sentinel(expr)

    def _emit_iter_callable_sentinel(self, expr: Call) -> ir.Value:
        """Evaluate callable then sentinel and publish the NEW iterator owner."""
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        sink = self._slot_call_result_sink(expr)
        output = sink
        roots = []
        if output is None:
            output = self._new_slot_call_root("iter.result")
            roots.append(output)
        try:
            self._iterator_install_cleanup(roots, target)
            callable_obj = self._emit_iterator_callable_operand(expr.args[0], "iter.callable")
            roots.append(callable_obj)
            self._iterator_install_cleanup(roots, target)
            sentinel = self._emit_slot_call_operand(expr.args[1], "iter.sentinel")
            roots.append(sentinel)
            self._iterator_install_cleanup(roots, target)
            callable_status = self._new_slot_call_root("iter.callable.status")
            roots.append(callable_status)
            self._iterator_install_cleanup(roots, target)
            # This classifier returns py_bool_from_bit's immortal singleton,
            # which can safely inhabit an owning root without a borrowed heap
            # reference. Validate only after both call arguments have run.
            self._slot_call_runtime_call(
                "py_builtin_callable", (callable_obj,), result_slot=callable_status,
                span=expr.span,
            )
            truth = self._slot_call_runtime_call("py_obj_truthy", (callable_status,), span=expr.span)
            valid = self.current_function.append_basic_block(self._fresh("iter.callable.valid"))
            invalid = self.current_function.append_basic_block(self._fresh("iter.callable.invalid"))
            self.builder.cbranch(
                self.builder.icmp_signed("!=", truth, ir.Constant(_I64, 0)), valid, invalid,
            )
            self.builder.position_at_end(invalid)
            self._emit_builtin_exception_and_branch(
                "TypeError", "iter(v, w): v must be callable", expr.span,
            )
            self.builder.position_at_end(valid)
            self._release_slot_call_roots((callable_status,))
            roots.pop()
            self._iterator_install_cleanup(roots, target)
            self._slot_call_runtime_call(
                "py_iter_callable_new", (callable_obj, sentinel),
                result_slot=output, span=expr.span,
            )
            self._release_slot_call_roots((callable_obj, sentinel))
            if sink is None:
                return self._take_slot_call_root(output)
            return self.builder.load(output, name=self._fresh("iter.current"))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy

    def _emit_next_owned_step(
        self, iterator, output, pending, stop_class, default, span, found, exhausted,
    ):
        """Publish the result and park the exact exception before retiring leases."""
        self._slot_call_runtime_call(
            "py_obj_next", (iterator,), result_slot=output, span=span,
            exception_slot=pending,
        )
        failed = self.current_function.append_basic_block(self._fresh("next.failed"))
        ready = self.current_function.append_basic_block(self._fresh("next.ready"))
        self.builder.cbranch(
            self.builder.icmp_unsigned(
                "!=", self.builder.load(pending), ir.Constant(_CSTR, None),
            ),
            failed, ready,
        )
        self.builder.position_at_end(ready)
        self._guard_cpy_value_not_null(self.builder.load(output))
        self.builder.branch(found)

        self.builder.position_at_end(failed)
        self._emit_next_pending_or_default(output, pending, stop_class, default, span, exhausted)

    def _emit_next_pending_or_default(self, output, pending, stop_class, default, span, exhausted):
        """Handle the exception of the complete next protocol invocation."""
        if default is not None:
            matched = self._slot_call_runtime_call(
                "py_exc_matches", (pending, stop_class), span=span,
            )
            use_default = self.current_function.append_basic_block(self._fresh("next.default"))
            propagate = self.current_function.append_basic_block(self._fresh("next.propagate"))
            self.builder.cbranch(
                self.builder.icmp_signed("!=", matched, ir.Constant(_I64, 0)),
                use_default, propagate,
            )
            self.builder.position_at_end(use_default)
            self.builder.call(
                self.runtime["pcc_gc_store_root"],
                [self._as_gc_ptr(output), ir.Constant(_CSTR, None)],
            )
            self._slot_call_copy_source(output, default, span=span)
            self.builder.call(
                self.runtime["pcc_gc_store_root"],
                [self._as_gc_ptr(pending), ir.Constant(_CSTR, None)],
            )
            self.builder.branch(exhausted)
            self.builder.position_at_end(propagate)
        # Restore the selecting exception to TLS. The existing cleanup then
        # preserves it across iterator/default disposal and reentrant GC.
        self.builder.call(self.runtime["py_clear_exception"], [])
        self.builder.call(
            self.module.globals["py_tls_exc_swap_slot"], [self._as_gc_ptr(pending)],
        )
        self.builder.branch(self._current_try_err_block())

    def _emit_next_filter_truth(self, output, predicate, call_args, pred_result, none_obj, span):
        if predicate is None:
            return self._slot_call_runtime_call("py_obj_truthy", (output,), span=span)
        fn = self.current_function
        identity = fn.append_basic_block(self._fresh("next.filter.identity"))
        invoke = fn.append_basic_block(self._fresh("next.filter.invoke"))
        done = fn.append_basic_block(self._fresh("next.filter.truth"))
        # None is a runtime value too: filter(factory(), source) must select
        # identity truth testing when the factory returns None.
        self.builder.cbranch(
            self.builder.icmp_unsigned("==", self.builder.load(predicate), self.builder.load(none_obj)),
            identity, invoke,
        )
        self.builder.position_at_end(identity)
        item_truth = self._slot_call_runtime_call("py_obj_truthy", (output,), span=span)
        identity_exit = self.builder._block
        self.builder.branch(done)
        self.builder.position_at_end(invoke)
        self._slot_call_runtime_call(
            "py_tuple_new", (), result_slot=call_args,
            suffix_args=(ir.Constant(_I64, 1),), span=span,
        )
        self._slot_call_runtime_call(
            "py_tuple_set_item", (call_args, output),
            suffix_args=(ir.Constant(_I64, 0),), argument_order=(0, 2, 1), span=span,
        )
        self._slot_call_runtime_call(
            "py_obj_call", (predicate, call_args, none_obj), result_slot=pred_result, span=span,
        )
        predicate_truth = self._slot_call_runtime_call("py_obj_truthy", (pred_result,), span=span)
        for temporary in (pred_result, call_args):
            self.builder.call(
                self.runtime["pcc_gc_store_root"],
                [self._as_gc_ptr(temporary), ir.Constant(_CSTR, None)],
            )
        invoke_exit = self.builder._block
        self.builder.branch(done)
        self.builder.position_at_end(done)
        truth = self.builder.phi(_I64, name=self._fresh("next.filter.keep"))
        truth.add_incoming(item_truth, identity_exit)
        truth.add_incoming(predicate_truth, invoke_exit)
        return truth

    def _maybe_emit_next_builtin(self, expr: Call) -> Optional[ir.Value]:
        if self._iterator_builtin_is_shadowed("next") or expr.kwargs or not (1 <= len(expr.args) <= 2):
            return None
        source = expr.args[0]
        filtered = (
            isinstance(source, Call) and isinstance(source.func, Name)
            and source.func.ident == "filter" and not source.kwargs
            and len(source.args) == 2
            and not self._iterator_builtin_is_shadowed("filter")
        )
        materialized = (
            isinstance(source, Call) and isinstance(source.func, Name)
            and source.func.ident in ("_gen_comp", "__genexpr__")
        )
        previous = self._current_try_err_block()
        target = previous if previous is not None else self._ensure_fn_err_exit()
        saved_cpy = self._cpy_operand_cleanup_block
        sink = self._slot_call_result_sink(expr)
        output = sink
        roots = []
        if output is None:
            output = self._new_slot_call_root("next.result")
            roots.append(output)
        try:
            self._iterator_install_cleanup(roots, target)
            predicate = None
            if filtered and not isinstance(source.args[0], NoneLit):
                # filter(predicate, iterable) evaluates its arguments in this
                # order; acquiring the iterable's iterator precedes next's
                # second argument, just as construction of filter does.
                predicate = self._emit_iterator_callable_operand(source.args[0], "next.filter.predicate")
                roots.append(predicate)
                self._iterator_install_cleanup(roots, target)
            source_expr = source.args[1] if filtered else source
            source_root = self._emit_slot_call_operand(source_expr, "next.source")
            roots.append(source_root)
            self._iterator_install_cleanup(roots, target)
            iterator = source_root
            if filtered or materialized:
                # Generator expressions are still materialized by their own
                # producer. Iterate that list to handle empty input through
                # StopIteration; this does not claim generator laziness.
                iterator = self._new_slot_call_root("next.iterator")
                roots.append(iterator)
                self._iterator_install_cleanup(roots, target)
                self._slot_call_runtime_call(
                    "py_obj_iter", (source_root,), result_slot=iterator, span=source.span,
                )
                # filter retains the iterator, not an independent reference
                # to its input iterable. Retire that argument before next's
                # default runs, while retaining the empty lexical frame.
                self.builder.call(
                    self.runtime["pcc_gc_store_root"],
                    [self._as_gc_ptr(source_root), ir.Constant(_CSTR, None)],
                )
            default = None
            if len(expr.args) == 2:
                # Ordinary call arguments are eager, even when next succeeds
                # or raises a different exception. A failing default prevents
                # __next__ from being invoked at all.
                default = self._emit_slot_call_operand(expr.args[1], "next.default")
                roots.append(default)
                self._iterator_install_cleanup(roots, target)
            stop_class = None
            if default is not None:
                stop_class = self._new_slot_call_root("next.stop.class")
                roots.append(stop_class)
                self._iterator_install_cleanup(roots, target)
                # py_exc_builtin_class is a borrowed cache lookup. Copy its
                # authoritative slot, never publish the raw borrowed pointer.
                self.builder.call(
                    self.runtime["py_exc_builtin_class"], [ir.Constant(_I64, _STOP_ITERATION_TAG)],
                )
                self._emit_post_call_err_check(expr.span)
                cache = self.builder.call(
                    self.runtime["py_subs_exc_cache_slot"], [ir.Constant(_I64, _STOP_ITERATION_TAG)],
                )
                self._slot_call_copy_source(stop_class, cache, span=expr.span)
            pending = self._new_slot_call_root("next.pending")
            roots.append(pending)
            self._iterator_install_cleanup(roots, target)
            call_args = None
            pred_result = None
            none_obj = None
            if predicate is not None:
                call_args = self._new_slot_call_root("next.filter.arguments")
                roots.append(call_args)
                self._iterator_install_cleanup(roots, target)
                pred_result = self._new_slot_call_root("next.filter.result")
                roots.append(pred_result)
                self._iterator_install_cleanup(roots, target)
                none_obj = self._emit_slot_call_operand(
                    NoneLit(span=expr.span, ty=NoneType(name="None")), "next.filter.none",
                )
                roots.append(none_obj)
                self._iterator_install_cleanup(roots, target)
            fn = self.current_function
            step = fn.append_basic_block(self._fresh("next.step"))
            found = fn.append_basic_block(self._fresh("next.found"))
            done = fn.append_basic_block(self._fresh("next.done"))
            self.builder.branch(step)
            self.builder.position_at_end(step)
            self._emit_next_owned_step(
                iterator, output, pending, stop_class, default, expr.span, found, done,
            )
            self.builder.position_at_end(found)
            if filtered:
                failed = fn.append_basic_block(self._fresh("next.filter.failed"))
                cleanup = self._current_try_err_block()
                self._try_err_block = failed
                self._cpy_operand_cleanup_block = failed
                try:
                    # Predicate and truth callbacks are part of filter's
                    # __next__. Their StopIteration belongs to the outer
                    # next(default) handler just like iterator exhaustion.
                    truth = self._emit_next_filter_truth(
                        output, predicate, call_args, pred_result, none_obj, expr.span,
                    )
                finally:
                    self._try_err_block = cleanup
                    self._cpy_operand_cleanup_block = cleanup
                reject = fn.append_basic_block(self._fresh("next.filter.reject"))
                self.builder.cbranch(
                    self.builder.icmp_signed("!=", truth, ir.Constant(_I64, 0)), done, reject,
                )
                self.builder.position_at_end(reject)
                self.builder.call(
                    self.runtime["pcc_gc_store_root"],
                    [self._as_gc_ptr(output), ir.Constant(_CSTR, None)],
                )
                self.builder.branch(step)
                self.builder.position_at_end(failed)
                self.builder.call(
                    self.module.globals["py_tls_exc_swap_slot"], [self._as_gc_ptr(pending)],
                )
                self._emit_next_pending_or_default(
                    output, pending, stop_class, default, expr.span, done,
                )
            else:
                self.builder.branch(done)
            self.builder.position_at_end(done)
            self._release_slot_call_roots(tuple(roots if sink is not None else roots[1:]))
            if sink is None:
                return self._take_slot_call_root(output)
            return self.builder.load(output, name=self._fresh("next.current"))
        finally:
            self._try_err_block = previous
            self._cpy_operand_cleanup_block = saved_cpy
