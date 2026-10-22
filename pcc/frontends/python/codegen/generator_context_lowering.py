"""Declared generator context managers, using the ordinary owning-slot ABI.

This is the compiler's existing static @contextmanager capability. It does
not add __enter__/__exit__ to generators or implement the dynamic decorator.
"""

from pcc.ir.compat import ir


_I64 = ir.IntType(64)
_CSTR = ir.IntType(8).as_pointer()
_NULL = ir.Constant(_CSTR, None)


def _swap(host, slot):
    function = host.module.globals.get("py_tls_exc_swap_slot")
    if function is None:
        function = ir.Function(
            host.module, ir.FunctionType(ir.VoidType(), [_CSTR]),
            name="py_tls_exc_swap_slot",
        )
    host.builder.call(function, [host._as_gc_ptr(slot)])
    host._slot_call_note_published(slot)


def _cleanup(host, roots, target):
    host._try_err_block = host._slot_call_cleanup_block(tuple(roots), target)
    host._cpy_operand_cleanup_block = host._try_err_block


def _clear(host, slot):
    host.builder.call(host.runtime["pcc_gc_store_root"], [host._as_gc_ptr(slot), _NULL])


def _matches(host, error, tag, span):
    # Builtin exception classes are borrowed from a lazy, moving cache.
    # Materialize the cache, then copy its authoritative slot, as next() does.
    previous = host._current_try_err_block()
    saved_cpy = host._cpy_operand_cleanup_block
    cls = host._new_slot_call_root("contextmanager.exception.class")
    try:
        _cleanup(host, (cls,), previous)
        host.builder.call(host.runtime["py_exc_builtin_class"], [ir.Constant(_I64, tag)])
        host._emit_post_call_err_check(span)
        cache = host.builder.call(host.runtime["py_subs_exc_cache_slot"], [ir.Constant(_I64, tag)])
        host._slot_call_copy_source(cls, cache, span=span)
        matched = host._slot_call_runtime_call("py_exc_matches", (error, cls), span=span)
        host._release_slot_call_roots((cls,))
        return host.builder.icmp_signed("!=", matched, ir.Constant(_I64, 0))
    finally:
        host._try_err_block = previous
        host._cpy_operand_cleanup_block = saved_cpy


def _runtime_error(host, output, message, span):
    host._slot_call_runtime_call(
        "py_exc_new", (), result_slot=output,
        suffix_args=(ir.Constant(_I64, 7), host._pooled_cstr_ptr(message, ".contextmanager.error")),
        span=span,
    )


def emit_generator_context_enter(host, stmt):
    previous = host._current_try_err_block()
    target = previous if previous is not None else host._ensure_fn_err_exit()
    saved_cpy = host._cpy_operand_cleanup_block
    manager = host._emit_slot_call_operand(stmt.items[0][0], "with.generator.manager")
    roots = [manager]
    try:
        _cleanup(host, roots, target)
        entered = host._new_slot_call_root("with.generator.enter")
        pending = host._new_slot_call_root("with.generator.pending")
        roots.extend((entered, pending))
        _cleanup(host, roots, target)
        host._slot_call_runtime_call(
            "py_gen_next", (manager,), result_slot=entered,
            exception_slot=pending, span=stmt.span,
        )
        failed = host.current_function.append_basic_block(host._fresh("contextmanager.enter.failed"))
        ready = host.current_function.append_basic_block(host._fresh("contextmanager.enter.ready"))
        host.builder.cbranch(host.builder.icmp_unsigned("!=", host.builder.load(pending), _NULL), failed, ready)
        host.builder.position_at_end(failed)
        empty = host.current_function.append_basic_block(host._fresh("contextmanager.enter.empty"))
        propagate = host.current_function.append_basic_block(host._fresh("contextmanager.enter.propagate"))
        host.builder.cbranch(_matches(host, pending, 8, stmt.span), empty, propagate)
        host.builder.position_at_end(empty)
        _clear(host, pending)
        _runtime_error(host, pending, "generator didn't yield", stmt.span)
        host.builder.branch(propagate)
        host.builder.position_at_end(propagate)
        host.builder.call(host.runtime["py_clear_exception"], [])
        _swap(host, pending)
        host.builder.branch(host._try_err_block)
        host.builder.position_at_end(ready)
        host._guard_cpy_value_not_null(host.builder.load(entered))
        host._release_slot_call_roots((pending,))
        return manager, entered
    finally:
        host._try_err_block = previous
        host._cpy_operand_cleanup_block = saved_cpy


def emit_generator_context_exit(host, context_target, clear_context, exceptional, span):
    """Finish once, preserving outcome identity across manager disposal.

    Both normal and nonlocal exits call here through the shared finally
    stack. The body exception is removed from TLS before any resume; a
    distinct StopIteration suppresses it, but the same object never does.
    """
    previous = host._current_try_err_block()
    outer_error = previous if previous is not None else host._ensure_fn_err_exit()
    target = host.current_function.append_basic_block(host._fresh("contextmanager.exit.error"))
    saved_cpy = host._cpy_operand_cleanup_block
    original = host._new_slot_call_root("contextmanager.original")
    roots = [original]
    if exceptional:
        _swap(host, original)
    try:
        _cleanup(host, roots, target)
        manager = host._emit_slot_call_operand(context_target, "contextmanager.manager")
        roots.append(manager)
        yielded = host._new_slot_call_root("contextmanager.yielded")
        pending = host._new_slot_call_root("contextmanager.pending")
        outcome = host._new_slot_call_root("contextmanager.outcome")
        roots.extend((yielded, pending, outcome))
        _cleanup(host, roots, target)
        suppressed = host._alloca_in_entry(_I64, name=host._fresh("contextmanager.suppressed"))
        host.builder.store(ir.Constant(_I64, 0), suppressed)
        arguments = (manager, original) if exceptional else (manager,)
        host._slot_call_runtime_call(
            "py_gen_throw" if exceptional else "py_gen_next", arguments,
            result_slot=yielded, exception_slot=pending, span=span,
        )
        fn = host.current_function
        stopped = fn.append_basic_block(host._fresh("contextmanager.stopped"))
        multiple = fn.append_basic_block(host._fresh("contextmanager.multiple"))
        propagate = fn.append_basic_block(host._fresh("contextmanager.propagate"))
        success = fn.append_basic_block(host._fresh("contextmanager.success"))
        finish = fn.append_basic_block(host._fresh("contextmanager.finish"))
        done = fn.append_basic_block(host._fresh("contextmanager.done"))
        host.builder.cbranch(host.builder.icmp_unsigned("==", host.builder.load(yielded), _NULL), stopped, multiple)

        host.builder.position_at_end(multiple)
        _clear(host, pending)
        _runtime_error(host, outcome, "generator didn't stop after throw()" if exceptional else "generator didn't stop", span)
        _clear(host, yielded)
        # contextlib closes in a finally after detecting the extra yield.
        # Keep its protocol error alive; an exception from close replaces it.
        host._slot_call_runtime_call(
            "py_gen_close", (manager,), result_slot=yielded,
            exception_slot=pending, span=span,
        )
        close_failed = fn.append_basic_block(host._fresh("contextmanager.close.failed"))
        host.builder.cbranch(host.builder.icmp_unsigned("!=", host.builder.load(pending), _NULL), close_failed, finish)
        host.builder.position_at_end(close_failed)
        host._slot_call_runtime_call("py_exc_set_context", (pending, outcome), span=span)
        _clear(host, outcome)
        host.builder.branch(propagate)

        host.builder.position_at_end(stopped)
        classify = fn.append_basic_block(host._fresh("contextmanager.classify"))
        if exceptional:
            # No safepoint separates these loads/identity comparison.
            host.builder.cbranch(
                host.builder.icmp_unsigned("==", host.builder.load(pending), host.builder.load(original)),
                propagate, classify,
            )
        else:
            host.builder.branch(classify)
        host.builder.position_at_end(classify)
        other = fn.append_basic_block(host._fresh("contextmanager.other"))
        host.builder.cbranch(_matches(host, pending, 8, span), success, other)
        host.builder.position_at_end(other)
        if exceptional:
            # PEP 479 wraps a thrown StopIteration in RuntimeError. contextlib
            # unwraps only when its explicit cause is the identical body error.
            check_original = fn.append_basic_block(host._fresh("contextmanager.pep479.original"))
            check_cause = fn.append_basic_block(host._fresh("contextmanager.pep479.cause"))
            restore_original = fn.append_basic_block(host._fresh("contextmanager.pep479.restore"))
            host.builder.cbranch(_matches(host, pending, 7, span), check_original, propagate)
            host.builder.position_at_end(check_original)
            host.builder.cbranch(_matches(host, original, 8, span), check_cause, propagate)
            host.builder.position_at_end(check_cause)
            # py_exc_get_cause returns a NEW owner; reuse the empty yielded slot.
            host._slot_call_runtime_call("py_exc_get_cause", (pending,), result_slot=yielded, span=span)
            host.builder.cbranch(
                host.builder.icmp_unsigned("==", host.builder.load(yielded), host.builder.load(original)),
                restore_original, propagate,
            )
            host.builder.position_at_end(restore_original)
            host._slot_call_copy_source(outcome, original, span=span)
            host.builder.branch(finish)
        else:
            host.builder.branch(propagate)

        host.builder.position_at_end(success)
        if exceptional:
            host.builder.store(ir.Constant(_I64, 1), suppressed)
        host.builder.branch(finish)
        host.builder.position_at_end(propagate)
        host._slot_call_copy_source(outcome, pending, span=span)
        host.builder.branch(finish)
        host.builder.position_at_end(finish)
        # The selected outcome stays rooted while the lexical manager and all
        # resume temporaries are released. Finalizers cannot replace it.
        clear_context()
        host.builder.call(host.runtime["py_clear_exception"], [])
        _swap(host, outcome)
        host.builder.branch(host._slot_call_cleanup_block(tuple(roots), done))
        host.builder.position_at_end(done)
        result = host.builder.load(suppressed)
        # Even a lease/classification failure retires the lexical manager.
        # Its disposal must not replace the error selected by the protocol.
        host.builder.position_at_end(target)
        host._try_err_block = outer_error
        host._cpy_operand_cleanup_block = outer_error
        failed = host._new_slot_call_root("contextmanager.exit.failure")
        _swap(host, failed)
        clear_context()
        host.builder.call(host.runtime["py_clear_exception"], [])
        _swap(host, failed)
        host.builder.branch(host._slot_call_cleanup_block((failed,), outer_error))
        host.builder.position_at_end(done)
        return result
    finally:
        host._try_err_block = previous
        host._cpy_operand_cleanup_block = saved_cpy
