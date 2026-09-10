"""Await delegation through traced resumable frames, including throw/send."""

from pcc.llvm_capi.compat import ir

from .generator_lowering import generator_may_park_child_slot


def emit_resumable_await(host, expr):
    child_slot, child_root = generator_may_park_child_slot(host, expr, "asyncio.await.child")
    send_slot, send_root = generator_may_park_child_slot(host, expr, "asyncio.await.send")
    error_slot, error_root = generator_may_park_child_slot(host, expr, "asyncio.await.error")
    source = expr.args[0]
    value = host._emit_as_object(source)
    iterator = host.builder.call(host.runtime["py_await_iterator"], [value], name=host._fresh("await.iterator"))
    host.builder.call(host.runtime["pcc_gc_store_root"], [child_root, iterator])
    host._gc_release(iterator)
    host._gc_release_if_owned(value, source)
    host._emit_post_call_err_check(expr.span)
    null = ir.Constant(ir.IntType(8).as_pointer(), None)
    host.builder.call(host.runtime["pcc_gc_store_root"], [send_root, null])
    host.builder.call(host.runtime["pcc_gc_store_root"], [error_root, null])

    fn = host.current_function
    step = fn.append_basic_block(host._fresh("await.step"))
    yielded = fn.append_basic_block(host._fresh("await.yielded"))
    stopped = fn.append_basic_block(host._fresh("await.stopped"))
    completed = fn.append_basic_block(host._fresh("await.completed"))
    failed = fn.append_basic_block(host._fresh("await.failed"))
    thrown = fn.append_basic_block(host._fresh("await.throw"))
    outer_error = getattr(host, "_try_err_block", None) or host._ensure_fn_err_exit()
    host.builder.branch(step)
    host.builder.position_at_end(step)
    result = host.builder.call(host.runtime["py_await_step"], [
        host.builder.load(child_slot), host.builder.load(send_slot), host.builder.load(error_slot),
    ], name=host._fresh("await.step.value"))
    host.builder.call(host.runtime["pcc_gc_store_root"], [send_root, null])
    host.builder.call(host.runtime["pcc_gc_store_root"], [error_root, null])
    host.builder.cbranch(host.builder.icmp_unsigned("==", result, null), stopped, yielded)

    host.builder.position_at_end(yielded)
    host._emit_generator_yield_value(result, resume_err_target=thrown)
    sent = host._emit_generator_take_send()
    host.builder.call(host.runtime["pcc_gc_store_root"], [send_root, sent])
    host._gc_release(sent)
    host.builder.branch(step)

    host.builder.position_at_end(thrown)
    error = host.builder.call(host.runtime["py_current_exception"], [], name=host._fresh("await.thrown"))
    host.builder.call(host.runtime["pcc_gc_store_root"], [error_root, error])
    host.builder.call(host.runtime["py_clear_exception"], [])
    host.builder.branch(step)

    host.builder.position_at_end(stopped)
    error = host.builder.call(host.runtime["py_current_exception"], [], name=host._fresh("await.exception"))
    stop_type = host.builder.call(host.runtime["py_exc_builtin_class"], [ir.Constant(ir.IntType(64), 8)])
    match = host.builder.call(host.runtime["py_exc_matches"], [error, stop_type])
    host.builder.cbranch(host.builder.icmp_signed("!=", match, ir.Constant(ir.IntType(64), 0)), completed, failed)

    host.builder.position_at_end(failed)
    host.builder.call(host.runtime["pcc_gc_store_root"], [child_root, null])
    host.builder.branch(outer_error)

    host.builder.position_at_end(completed)
    value = host.builder.call(host.runtime["py_exc_get_message"], [error], name=host._fresh("await.result.borrowed"))
    value = host.builder.select(host.builder.icmp_unsigned("==", value, null), host._emit_none_literal(), value)
    host.builder.call(host.runtime["pcc_gc_store_root"], [child_root, value])
    host.builder.call(host.runtime["py_clear_exception"], [])
    value = host.builder.call(host.runtime["pcc_gc_load_ptr"], [null, child_root], name=host._fresh("await.result.rooted"))
    owned = host._gc_retain(value, name=host._fresh("await.result"))
    host.builder.call(host.runtime["pcc_gc_store_root"], [child_root, null])
    host._note_owned_object_value(owned)
    return owned
