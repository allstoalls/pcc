"""pcc-Python port of py_gen.c."""

__pcc_runtime_port__ = True

from pcc.py_runtime.py.py_abi_constants import (
    C_POINTER_SIZE,
    PYLISTOBJECT_CAPACITY_OFFSET,
    PYLISTOBJECT_ITEMS_OFFSET,
    PYLISTOBJECT_LENGTH_OFFSET,
    PYLISTOBJECT_SIZE,
    PY_TYPE_COROUTINE,
    PY_TYPE_GEN,
    PY_TYPE_LIST,
)

from pcc.extern import extern, c_abi_export, c_int32, c_ptr, c_int64, c_void
from pcc.unsafe import (
    call_ptr2,
    cstr,
    global_load_ptr,
    function_addr,
    is_tagged_int,
    load_i32,
    load_i64,
    load_ptr,
    malloc,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    stack_alloc,
    store_i32,
    store_i64,
    store_ptr,
)

py_incref   = extern("py_incref",   (c_ptr,),         c_void)
py_decref   = extern("py_decref",   (c_ptr,),         c_void)
py_exc_new  = extern("py_exc_new",  (c_int64, c_ptr), c_ptr)
py_exc_new_with_value = extern("py_exc_new_with_value", (c_int64, c_ptr), c_ptr)
py_raise    = extern("py_raise",    (c_ptr,),         c_void)
# py_raise increfs the exception it stores, so a caller that created it still
# owns a reference.  py_raise_owned raises and releases that reference.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_coroutine_run = extern("py_coroutine_run", (c_ptr,), c_ptr)
py_current_exception = extern("py_current_exception", (), c_ptr)
py_clear_exception   = extern("py_clear_exception",   (), c_void)
py_exc_builtin_class = extern("py_exc_builtin_class", (c_int64,), c_ptr)
py_exc_matches       = extern("py_exc_matches",       (c_ptr, c_ptr), c_int64)
py_gc_track          = extern("py_gc_track",          (c_ptr,),         c_void)
pcc_gc_publish_initialized = extern(
    "pcc_gc_publish_initialized", (c_ptr,), c_void
)
pcc_gc_alloc         = extern("pcc_gc_alloc",         (c_int64, c_int32, c_int32), c_ptr)
pcc_gc_load_ptr      = extern("pcc_gc_load_ptr",      (c_ptr, c_ptr), c_ptr)
pcc_gc_store_ptr     = extern("pcc_gc_store_ptr",     (c_ptr, c_ptr, c_ptr), c_void)
pcc_gc_store_root    = extern("pcc_gc_store_root",    (c_ptr, c_ptr), c_void)
pcc_gc_scheduler_root_register_handle = extern(
    "pcc_gc_scheduler_root_register_handle", (c_ptr,), c_ptr
)
pcc_gc_scheduler_root_unregister_handle = extern(
    "pcc_gc_scheduler_root_unregister_handle", (c_ptr,), c_void
)
py_exc_set_context = extern("py_exc_set_context", (c_ptr, c_ptr), c_void)
pcc_gc_free_object_memory = extern("pcc_gc_free_object_memory", (c_ptr,), c_void)
py_runtime_error_if_unset = extern(
    "py_runtime_error_if_unset", (c_ptr, c_ptr), c_ptr
)
pcc_gc_backend4_zpage_register_owner_payload_span = extern(
    "pcc_gc_backend4_zpage_register_owner_payload_span",
    (c_ptr, c_ptr, c_int64), c_int64,
)


def _require_result(result, helper_name, message):
    if ptr_is_null(result):
        py_runtime_error_if_unset(helper_name, message)
    return result


@c_abi_export("py_gen_frame_new")
def py_gen_frame_new(slot_count: int):
    """Construct the compiler's fixed-size list frame before publishing it."""
    if slot_count < 0 or slot_count > 134217728:
        return _require_result(null(), cstr("py_gen_frame_new"), cstr("invalid generator frame size"))
    frame = pcc_gc_alloc(PYLISTOBJECT_SIZE, PY_TYPE_LIST, 0)
    if ptr_is_null(frame):
        return _require_result(null(), cstr("pcc_gc_alloc"), cstr("generator frame allocation failed"))
    capacity: int = slot_count
    if capacity < 4:
        capacity = 4
    store_i64(frame, PYLISTOBJECT_LENGTH_OFFSET, 0)
    store_i64(frame, PYLISTOBJECT_CAPACITY_OFFSET, capacity)
    store_ptr(frame, PYLISTOBJECT_ITEMS_OFFSET, null())
    items = malloc(capacity * C_POINTER_SIZE)
    if ptr_is_null(items):
        py_decref(frame)
        return _require_result(null(), cstr("malloc"), cstr("generator frame slots allocation failed"))
    store_ptr(frame, PYLISTOBJECT_ITEMS_OFFSET, items)
    none = global_load_ptr("py_None")
    index: int = 0
    # None is immortal. Fill the unpublished frame without retain/release or
    # growth; subsequent argument/local stores use the ordinary list barriers.
    while index < slot_count:
        store_ptr(items, index * C_POINTER_SIZE, none)
        index += 1
    store_i64(frame, PYLISTOBJECT_LENGTH_OFFSET, slot_count)
    pcc_gc_backend4_zpage_register_owner_payload_span(frame, items, capacity * C_POINTER_SIZE)
    py_gc_track(frame)
    pcc_gc_publish_initialized(frame)
    return frame


@c_abi_export("py_gen_new")
def py_gen_new(resume, frame):
    if ptr_is_null(resume) or ptr_is_null(frame):
        return _require_result(
            null(),
            cstr("py_gen_new"),
            cstr("generator construction received a NULL resume thunk or frame"),
        )
    g = pcc_gc_alloc(56, PY_TYPE_GEN, 0)
    if ptr_is_null(g):
        return _require_result(
            null(),
            cstr("pcc_gc_alloc"),
            cstr("generator construction could not allocate generator state"),
        )
    store_ptr(g, 16, resume)
    store_ptr(g, 24, null())
    store_i64(g, 32, 0)       # state
    store_i64(g, 40, 0)       # done
    store_ptr(g, 48, null())   # pending send value
    pcc_gc_store_ptr(g, ptr_add(g, 24), frame)
    pcc_gc_store_ptr(g, ptr_add(g, 48), global_load_ptr("py_None"))
    py_gc_track(g)
    pcc_gc_publish_initialized(g)
    return g


@c_abi_export("py_dealloc_gen")
def py_dealloc_gen(o) -> None:
    frame = pcc_gc_load_ptr(o, ptr_add(o, 24))
    if not ptr_is_null(frame):
        py_decref(frame)
    send_value = pcc_gc_load_ptr(o, ptr_add(o, 48))
    if not ptr_is_null(send_value):
        py_decref(send_value)
    pcc_gc_free_object_memory(o)


def _checked_gen(gen):
    if ptr_is_null(gen):
        exc = py_exc_new(3, null())
        py_raise_owned(exc)
        return null()
    if is_tagged_int(gen):
        exc = py_exc_new(3, null())
        py_raise_owned(exc)
        return null()
    if load_i32(gen, 8) != PY_TYPE_GEN:
        exc = py_exc_new(3, null())
        py_raise_owned(exc)
        return null()
    return gen


@c_abi_export("py_gen_set_may_park")
def py_gen_set_may_park(gen) -> None:
    gen = _checked_gen(gen)
    if ptr_is_null(gen):
        return
    store_i32(gen, 12, load_i32(gen, 12) | 1048576)


@c_abi_export("py_gen_is_may_park")
def py_gen_is_may_park(gen) -> int:
    if ptr_is_null(gen) or is_tagged_int(gen):
        return 0
    if load_i32(gen, 8) != PY_TYPE_GEN:
        return 0
    return 1 if (load_i32(gen, 12) & 1048576) != 0 else 0


def _set_send_value(gen, value) -> None:
    if ptr_is_null(value):
        value = global_load_ptr("py_None")
    pcc_gc_store_ptr(gen, ptr_add(gen, 48), value)


@c_abi_export("py_gen_state")
def py_gen_state(gen) -> int:
    gen = _checked_gen(gen)
    if ptr_is_null(gen):
        return -1
    return load_i64(gen, 32)


@c_abi_export("py_gen_set_state")
def py_gen_set_state(gen, state: int) -> None:
    gen = _checked_gen(gen)
    if ptr_is_null(gen):
        return
    store_i64(gen, 32, state)


@c_abi_export("py_gen_set_done")
def py_gen_set_done(gen) -> None:
    gen = _checked_gen(gen)
    if ptr_is_null(gen):
        return
    store_i64(gen, 40, 1)


@c_abi_export("py_gen_is_done")
def py_gen_is_done(gen) -> int:
    gen = _checked_gen(gen)
    if ptr_is_null(gen):
        return 1
    if load_i64(gen, 40) != 0:
        return 1
    return 0


@c_abi_export("py_gen_finish")
def py_gen_finish(gen, value):
    gen = _checked_gen(gen)
    if ptr_is_null(gen):
        return null()
    store_i64(gen, 40, 1)
    if ptr_is_null(value):
        value = global_load_ptr("py_None")
    stop = py_exc_new_with_value(8, value)       # PY_EXC_STOPITERATION
    if ptr_is_null(stop):
        return _require_result(
            null(),
            cstr("py_exc_new_with_value"),
            cstr("generator finish could not allocate StopIteration"),
        )
    py_raise(stop)
    py_decref(stop)
    return null()


@c_abi_export("py_gen_completed_resume")
def py_gen_completed_resume(gen, value):
    if not ptr_is_null(py_current_exception()):
        py_gen_set_done(gen)
        return null()
    return py_gen_finish(gen, value)


@c_abi_export("py_gen_completed")
def py_gen_completed(value):
    # The resume ABI accepts arbitrary managed userdata. A completed call
    # needs its result owner, not a list of suspended Python locals.
    if ptr_is_null(value):
        value = global_load_ptr("py_None")
    gen = py_gen_new(function_addr("py_gen_completed_resume"), value)
    if not ptr_is_null(gen):
        py_gen_set_may_park(gen)
    return gen


@c_abi_export("py_gen_next")
def py_gen_next(gen):
    gen = _checked_gen(gen)
    if ptr_is_null(gen):
        return null()
    if load_i64(gen, 40) != 0:
        exc = py_exc_new(8, null())
        py_raise_owned(exc)
        return null()
    resume = load_ptr(gen, 16)
    frame = pcc_gc_load_ptr(gen, ptr_add(gen, 24))
    if ptr_is_null(resume):
        exc = py_exc_new(8, null())
        py_raise_owned(exc)
        return null()
    _set_send_value(gen, global_load_ptr("py_None"))
    return _require_result(
        call_ptr2(resume, gen, frame),
        cstr("py_gen_next"),
        cstr("generator resume returned NULL without StopIteration or an exception"),
    )


@c_abi_export("py_gen_send")
def py_gen_send(gen, value):
    if not ptr_is_null(gen):
        if is_tagged_int(gen) == 0:
            if load_i32(gen, 8) == PY_TYPE_COROUTINE:       # PY_TYPE_COROUTINE
                none = global_load_ptr("py_None")
                if not ptr_is_null(value):
                    if ptr_eq(value, none) == 0:
                        exc = py_exc_new(3, null())
                        py_raise_owned(exc)
                        return null()
                return _require_result(
                    py_coroutine_run(gen),
                    cstr("py_coroutine_run"),
                    cstr("coroutine send returned NULL without setting an exception"),
                )
    gen = _checked_gen(gen)
    if ptr_is_null(gen):
        return null()
    if load_i64(gen, 40) != 0:
        exc = py_exc_new(8, null())
        py_raise_owned(exc)
        return null()
    resume = load_ptr(gen, 16)
    frame = pcc_gc_load_ptr(gen, ptr_add(gen, 24))
    if ptr_is_null(resume):
        exc = py_exc_new(8, null())
        py_raise_owned(exc)
        return null()
    none = global_load_ptr("py_None")
    if load_i64(gen, 32) == 0 and not ptr_is_null(value):
        if ptr_eq(value, none) == 0:
            exc = py_exc_new(3, null())
            py_raise_owned(exc)
            return null()
    _set_send_value(gen, value)
    return _require_result(
        call_ptr2(resume, gen, frame),
        cstr("py_gen_send"),
        cstr("generator send returned NULL without StopIteration or an exception"),
    )


@c_abi_export("py_gen_throw")
def py_gen_throw(gen, exc):
    gen = _checked_gen(gen)
    if ptr_is_null(gen):
        return null()
    if load_i64(gen, 40) != 0:
        stop = py_exc_new(8, null())
        py_raise_owned(stop)
        return null()
    resume = load_ptr(gen, 16)
    frame = pcc_gc_load_ptr(gen, ptr_add(gen, 24))
    if ptr_is_null(resume):
        stop = py_exc_new(8, null())
        py_raise_owned(stop)
        return null()
    _set_send_value(gen, global_load_ptr("py_None"))
    py_raise(exc)
    return _require_result(
        call_ptr2(resume, gen, frame),
        cstr("py_gen_throw"),
        cstr("generator throw returned NULL without setting an exception"),
    )


@c_abi_export("py_gen_close")
def py_gen_close(gen):
    gen = _checked_gen(gen)
    if ptr_is_null(gen):
        return null()
    if load_i64(gen, 40) == 0:
        resume = load_ptr(gen, 16)
        if not ptr_is_null(resume):
            gen_slot = stack_alloc(8)
            exc_slot = stack_alloc(8)
            store_ptr(gen_slot, 0, null())
            store_ptr(exc_slot, 0, null())
            gen_root = pcc_gc_scheduler_root_register_handle(gen_slot)
            if ptr_is_null(gen_root):
                root_error = py_exc_new(19, null())
                py_raise(root_error)
                py_decref(root_error)
                return null()
            pcc_gc_store_root(gen_slot, gen)
            exc_root = pcc_gc_scheduler_root_register_handle(exc_slot)
            if ptr_is_null(exc_root):
                pcc_gc_store_root(gen_slot, null())
                pcc_gc_scheduler_root_unregister_handle(gen_root)
                root_error = py_exc_new(19, null())
                py_raise(root_error)
                py_decref(root_error)
                return null()
            exc = py_exc_new(0, null())       # GeneratorExit ~= BaseException
            if ptr_is_null(exc):
                pcc_gc_store_root(exc_slot, null())
                pcc_gc_scheduler_root_unregister_handle(exc_root)
                pcc_gc_store_root(gen_slot, null())
                pcc_gc_scheduler_root_unregister_handle(gen_root)
                return null()
            pcc_gc_store_root(exc_slot, exc)
            gen = load_ptr(gen_slot, 0)
            _set_send_value(gen, global_load_ptr("py_None"))
            py_raise(load_ptr(exc_slot, 0))
            py_decref(load_ptr(exc_slot, 0))  # TLS and root own the exception
            # Raising/decref may allocate or safepoint.  Reload the moving
            # generator from its updateable root before reading its frame.
            gen = load_ptr(gen_slot, 0)
            frame = pcc_gc_load_ptr(gen, ptr_add(gen, 24))
            resume = load_ptr(gen, 16)
            result = call_ptr2(resume, gen, frame)
            if not ptr_is_null(result):
                py_decref(result)
                gen = load_ptr(gen_slot, 0)
                store_i64(gen, 40, 1)
                pcc_gc_store_root(exc_slot, null())
                pcc_gc_scheduler_root_unregister_handle(exc_root)
                pcc_gc_store_root(gen_slot, null())
                pcc_gc_scheduler_root_unregister_handle(gen_root)
                runtime_error = py_exc_new(7, null())
                py_raise(runtime_error)
                py_decref(runtime_error)
                return null()
            if ptr_is_null(py_current_exception()):
                _require_result(
                    null(),
                    cstr("py_gen_close"),
                    cstr(
                        "generator close resume returned NULL without setting an exception"
                    ),
                )
            cur = py_current_exception()
            stop_cls = py_exc_builtin_class(8)
            injected = load_ptr(exc_slot, 0)
            injected_propagated = ptr_eq(cur, injected)
            stopped = 0
            if injected_propagated == 0:
                stopped = py_exc_matches(cur, stop_cls)
            gen = load_ptr(gen_slot, 0)
            if stopped != 0:
                store_i64(gen, 40, 1)
                py_clear_exception()
            elif injected_propagated != 0:
                # Our injected GeneratorExit propagated back unhandled:
                # that IS the normal close path in CPython — swallow it.
                # Any OTHER exception from the body keeps propagating.
                store_i64(gen, 40, 1)
                py_clear_exception()
            else:
                store_i64(gen, 40, 1)
                pcc_gc_store_root(exc_slot, null())
                pcc_gc_scheduler_root_unregister_handle(exc_root)
                pcc_gc_store_root(gen_slot, null())
                pcc_gc_scheduler_root_unregister_handle(gen_root)
                return null()
            pcc_gc_store_root(exc_slot, null())
            pcc_gc_scheduler_root_unregister_handle(exc_root)
            pcc_gc_store_root(gen_slot, null())
            pcc_gc_scheduler_root_unregister_handle(gen_root)
    none = global_load_ptr("py_None")
    py_incref(none)
    return none


@c_abi_export("py_gen_close_preserving_exception")
def py_gen_close_preserving_exception(gen) -> int:
    gen_slot = stack_alloc(8)
    store_ptr(gen_slot, 0, null())
    gen_root = pcc_gc_scheduler_root_register_handle(gen_slot)
    if ptr_is_null(gen_root):
        return -1
    pcc_gc_store_root(gen_slot, gen)
    borrowed = py_current_exception()
    saved_slot = stack_alloc(8)
    store_ptr(saved_slot, 0, null())
    saved_root = null()
    if ptr_is_null(borrowed) == 0:
        saved_root = pcc_gc_scheduler_root_register_handle(saved_slot)
        if ptr_is_null(saved_root):
            pcc_gc_store_root(gen_slot, null())
            pcc_gc_scheduler_root_unregister_handle(gen_root)
            return -1
        pcc_gc_store_root(saved_slot, borrowed)
        py_clear_exception()

    closed = py_gen_close(load_ptr(gen_slot, 0))
    if ptr_is_null(closed) == 0:
        py_decref(closed)
        saved = load_ptr(saved_slot, 0)
        if ptr_is_null(saved) == 0:
            py_raise(saved)
        if ptr_is_null(saved_root) == 0:
            pcc_gc_store_root(saved_slot, null())
            pcc_gc_scheduler_root_unregister_handle(saved_root)
        pcc_gc_store_root(gen_slot, null())
        pcc_gc_scheduler_root_unregister_handle(gen_root)
        return 0

    cleanup_error = py_current_exception()
    saved = load_ptr(saved_slot, 0)
    if (
        ptr_is_null(cleanup_error) == 0
        and ptr_is_null(saved) == 0
        and ptr_eq(cleanup_error, saved) == 0
    ):
        py_exc_set_context(cleanup_error, saved)
    if ptr_is_null(saved_root) == 0:
        pcc_gc_store_root(saved_slot, null())
        pcc_gc_scheduler_root_unregister_handle(saved_root)
    pcc_gc_store_root(gen_slot, null())
    pcc_gc_scheduler_root_unregister_handle(gen_root)
    return -1


@c_abi_export("py_gen_take_send")
def py_gen_take_send(gen):
    gen = _checked_gen(gen)
    if ptr_is_null(gen):
        return null()
    none = global_load_ptr("py_None")
    value = pcc_gc_load_ptr(gen, ptr_add(gen, 48))
    if ptr_is_null(value):
        value = none
    py_incref(value)
    pcc_gc_store_ptr(gen, ptr_add(gen, 48), none)
    return value
