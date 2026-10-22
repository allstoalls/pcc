"""Await delegation through persistent owning slots, including throw/send."""

from pcc.ir.compat import ir


_CSTR = ir.IntType(8).as_pointer()
_I64 = ir.IntType(64)


def emit_resumable_await(host, expr):
    previous = host._current_try_err_block()
    target = previous if previous is not None else host._ensure_fn_err_exit()
    saved_cpy = host._cpy_operand_cleanup_block
    sink = host._slot_call_result_sink(expr)
    output = sink
    roots = []
    if output is None:
        output = host._new_slot_call_root("await.output")
        roots.append(output)
    child = host._new_slot_call_root("await.child")
    send = host._new_slot_call_root("await.send")
    error = host._new_slot_call_root("await.error")
    step_value = host._new_slot_call_root("await.step")
    exception = host._new_slot_call_root("await.exception")
    throw_args = host._new_slot_call_root("await.throw_args")
    roots.extend((child, send, error, step_value, exception, throw_args))
    null = ir.Constant(_CSTR, None)
    swap = host.module.globals.get("py_tls_exc_swap_slot")
    if swap is None:
        swap = ir.Function(host.module, ir.FunctionType(ir.VoidType(), [_CSTR]), name="py_tls_exc_swap_slot")
    try:
        host._try_err_block = host._slot_call_cleanup_block(tuple(roots), target)
        host._cpy_operand_cleanup_block = host._try_err_block
        source = host._emit_slot_call_operand(expr.args[0], "await.source")
        roots.append(source)
        host._try_err_block = host._slot_call_cleanup_block(tuple(roots), target)
        host._cpy_operand_cleanup_block = host._try_err_block
        host._slot_call_runtime_call("py_await_iterator", (source,), result_slot=child, span=expr.span)
        host._release_slot_call_roots((source,))

        fn = host.current_function
        step = fn.append_basic_block(host._fresh("await.step"))
        step_done = fn.append_basic_block(host._fresh("await.step_done"))
        thrown_args = fn.append_basic_block(host._fresh("await.throw_arguments"))
        yielded = fn.append_basic_block(host._fresh("await.yielded"))
        stopped = fn.append_basic_block(host._fresh("await.stopped"))
        completed = fn.append_basic_block(host._fresh("await.completed"))
        failed = fn.append_basic_block(host._fresh("await.failed"))
        thrown = fn.append_basic_block(host._fresh("await.throw"))
        host.builder.branch(step)
        host.builder.position_at_end(step)
        host._slot_call_runtime_call(
            "py_await_step", (child, send, error), result_slot=step_value,
            exception_slot=exception, span=expr.span,
        )
        host.builder.branch(step_done)
        host.builder.position_at_end(step_done)
        host._release_slot_call_roots((send, error))
        result = host.builder.load(step_value)
        host.builder.cbranch(host.builder.icmp_unsigned("==", result, null), stopped, yielded)

        host.builder.position_at_end(yielded)
        host._emit_generator_yield_value(
            result, result_slot=step_value, resume_err_target=thrown,
            resume_args_target=thrown_args,
        )
        sent = host._emit_generator_take_send()
        host._publish_slot_call_owned(send, sent, label="await sent value")
        host.builder.branch(step)

        host.builder.position_at_end(thrown_args)
        pending_args = host.builder.call(
            host.runtime["py_coroutine_take_throw_arguments"],
            [host._generator_current_object(host._generator_ctx_stack[-1])],
            name=host._fresh("await.throw_args.owner"),
        )
        host._publish_slot_call_owned(throw_args, pending_args, label="await throw arguments")
        host._emit_resume_handled_exception_scopes()
        host._slot_call_runtime_call(
            "py_await_throw_arguments", (child, throw_args), result_slot=step_value,
            exception_slot=exception, span=expr.span,
        )
        host._release_slot_call_roots((throw_args,))
        host.builder.branch(step_done)

        host.builder.position_at_end(thrown)
        host.builder.call(swap, [host._as_gc_ptr(error)])
        host._slot_call_note_published(error)
        host.builder.branch(step)

        host.builder.position_at_end(stopped)
        # The TLS owner was captured at the producer boundary. Keep both the
        # exception and any borrowed payload stable while inspecting them.
        host.builder.call(host.runtime["pcc_py_gc_minor_graph_lock"], [])
        current_error = host.builder.load(exception)
        stop_type = host.builder.call(host.runtime["py_exc_builtin_class"], [ir.Constant(_I64, 8)])
        match = host.builder.call(host.runtime["py_exc_matches"], [current_error, stop_type])
        host.builder.call(host.runtime["pcc_py_gc_minor_graph_unlock"], [])
        host.builder.cbranch(host.builder.icmp_signed("!=", match, ir.Constant(_I64, 0)), completed, failed)

        host.builder.position_at_end(failed)
        host.builder.call(host.runtime["py_clear_exception"], [])
        host.builder.call(swap, [host._as_gc_ptr(exception)])
        host.builder.branch(host._try_err_block)

        host.builder.position_at_end(completed)
        host.builder.call(host.runtime["pcc_py_gc_minor_graph_lock"], [])
        current_error = host.builder.load(exception)
        value = host.builder.call(host.runtime["py_exc_get_message"], [current_error], name=host._fresh("await.result.borrowed"))
        value = host.builder.select(host.builder.icmp_unsigned("==", value, null), host._emit_none_literal(), value)
        owned = host._gc_retain(value, name=host._fresh("await.result.owner"))
        # Retain and first publication are under one graph lock. The borrowed
        # payload remains owned by the registered exception throughout.
        host.builder.store(owned, output)
        host.builder.call(host.runtime["pcc_gc_note_slot_write_barrier"], [null, host._as_gc_ptr(output), owned])
        host._slot_call_note_published(output)
        host.builder.call(host.runtime["pcc_py_gc_minor_graph_unlock"], [])
        host._release_slot_call_roots(tuple(roots[1:]) if sink is None else tuple(roots))
        if sink is None:
            return host._take_slot_call_root(output)
        return host.builder.load(output, name=host._fresh("await.result.current"))
    finally:
        host._try_err_block = previous
        host._cpy_operand_cleanup_block = saved_cpy
