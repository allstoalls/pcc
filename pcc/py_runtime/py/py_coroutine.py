"""pcc-Python port of py_coroutine.c.

Coroutine object layout:
    offset  0   PyObjectHeader
    offset 16   name const char*
    offset 24   entry PyNativeFuncEntry
    offset 32   captures tuple
    offset 40   args tuple
    offset 48   cached result
    offset 56   closed i32
    offset 60   done i32
    total size: 64 bytes
"""

__pcc_runtime_port__ = True

from pcc.extern import extern, c_abi_export, c_int32, c_int64, c_ptr, c_void
from pcc.py_runtime.py.py_abi_constants import (
    PYOBJECTHEADER_FLAGS_OFFSET,
    PYOBJECTHEADER_TYPE_TAG_OFFSET,
    PY_FLAG_IMMORTAL,
    PY_TYPE_CONTINUATION,
    PY_TYPE_INT,
    PY_TYPE_TASK,
)
from pcc.py_runtime.py.py_abi_constants import (
    PY_TYPE_COROUTINE,
    PY_TYPE_GEN,
)
from pcc.unsafe import (
    call_ptr2,
    calloc,
    cstr,
    define_global_i32,
    define_global_ptr_null,
    free,
    global_addr,
    global_load_ptr,
    global_store_ptr,
    is_tagged_int,
    load_i32,
    load_i64,
    load_ptr,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    store_i32,
    store_i64,
    store_ptr,
)

py_class_new = extern(
    "py_class_new",
    (c_ptr, c_ptr, c_int32, c_ptr, c_int32),
    c_ptr,
)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_runtime_error_if_unset = extern(
    "py_runtime_error_if_unset", (c_ptr, c_ptr), c_ptr
)
py_obj_next = extern("py_obj_next", (c_ptr,), c_ptr)
py_current_exception = extern("py_current_exception", (), c_ptr)
py_exc_builtin_class = extern("py_exc_builtin_class", (c_int64,), c_ptr)
py_exc_matches = extern("py_exc_matches", (c_ptr, c_ptr), c_int64)
py_exc_get_message = extern("py_exc_get_message", (c_ptr,), c_ptr)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_obj_getattr = extern("py_obj_getattr", (c_ptr, c_ptr), c_ptr)
py_obj_call = extern("py_obj_call", (c_ptr, c_ptr, c_ptr), c_ptr)
py_gc_track = extern("py_gc_track", (c_ptr,), c_void)
pcc_gc_publish_initialized = extern(
    "pcc_gc_publish_initialized", (c_ptr,), c_void
)
pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_ptr)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_store_ptr = extern("pcc_gc_store_ptr", (c_ptr, c_ptr, c_ptr), c_void)
pcc_gc_store_root = extern("pcc_gc_store_root", (c_ptr, c_ptr), c_void)
pcc_gc_backend4_zpage_register_owner_payload_span = extern(
    "pcc_gc_backend4_zpage_register_owner_payload_span",
    (c_ptr, c_ptr, c_int64),
    c_int64,
)
pcc_gc_register_continuation_root = extern(
    "pcc_gc_register_continuation_root",
    (c_ptr, c_ptr),
    c_void,
)
pcc_platform_getenv = extern("pcc_platform_getenv", (c_ptr,), c_ptr)
pcc_gc_register_continuation_root_node = extern(
    "pcc_gc_register_continuation_root_node", (c_ptr, c_ptr), c_ptr
)
pcc_gc_unregister_continuation_root_node = extern(
    "pcc_gc_unregister_continuation_root_node", (c_ptr,), c_void
)
pcc_gc_unregister_continuation_root = extern(
    "pcc_gc_unregister_continuation_root",
    (c_ptr,),
    c_void,
)
pcc_gc_note_relocation_read = extern(
    "pcc_gc_note_relocation_read",
    (c_ptr,),
    c_ptr,
)
pcc_gc_free_object_memory = extern("pcc_gc_free_object_memory", (c_ptr,), c_void)


define_global_ptr_null("py_coroutine_class_cache")
define_global_ptr_null("py_continuation_class_cache")


def _type_of(obj) -> int:
    if is_tagged_int(obj):
        return PY_TYPE_INT
    return load_i32(obj, PYOBJECTHEADER_TYPE_TAG_OFFSET)


def _raise_typeerror(message) -> None:
    exc = py_exc_new(3, message)  # PY_EXC_TYPEERROR
    py_raise_owned(exc)


def _raise_runtimeerror(message) -> None:
    exc = py_exc_new(7, message)  # PY_EXC_RUNTIMEERROR
    py_raise_owned(exc)


def _coroutine_require_result(result, helper_name, message):
    if ptr_is_null(result):
        py_runtime_error_if_unset(helper_name, message)
    return result


@c_abi_export("py_coroutine_class")
def py_coroutine_class():
    cls = global_load_ptr("py_coroutine_class_cache")
    if not ptr_is_null(cls):
        return cls
    cls = py_class_new(cstr("coroutine"), null(), 0, null(), 0)
    if not ptr_is_null(cls):
        flags: int = load_i32(cls, PYOBJECTHEADER_FLAGS_OFFSET)
        store_i32(
            cls,
            PYOBJECTHEADER_FLAGS_OFFSET,
            flags | PY_FLAG_IMMORTAL,
        )
        global_store_ptr("py_coroutine_class_cache", cls)
    return cls


@c_abi_export("py_coroutine_new")
def py_coroutine_new(name):
    return py_coroutine_new_native(name, null(), null(), null())


@c_abi_export("py_coroutine_new_native")
def py_coroutine_new_native(name, entry, captures_tuple, args_tuple):
    coro = pcc_gc_alloc(64, PY_TYPE_COROUTINE, 0)
    if ptr_is_null(coro):
        return _coroutine_require_result(
            null(),
            cstr("pcc_gc_alloc"),
            cstr("coroutine construction could not allocate coroutine state"),
        )
    store_ptr(coro, 16, name)
    store_ptr(coro, 24, entry)
    made_captures: int = 0
    if ptr_is_null(captures_tuple):
        captures_tuple = py_tuple_new(0)
        made_captures = 1
        if ptr_is_null(captures_tuple):
            _coroutine_require_result(
                null(),
                cstr("py_tuple_new"),
                cstr("coroutine construction could not allocate captures tuple"),
            )
            py_decref(coro)
            return null()
    made_args: int = 0
    if ptr_is_null(args_tuple):
        args_tuple = py_tuple_new(0)
        made_args = 1
        if ptr_is_null(args_tuple):
            _coroutine_require_result(
                null(),
                cstr("py_tuple_new"),
                cstr("coroutine construction could not allocate arguments tuple"),
            )
            if made_captures != 0:
                py_decref(captures_tuple)
            py_decref(coro)
            return null()
    store_ptr(coro, 32, null())
    store_ptr(coro, 40, null())
    store_ptr(coro, 48, null())  # result
    store_i32(coro, 56, 0)  # closed
    store_i32(coro, 60, 0)  # done
    pcc_gc_store_ptr(coro, ptr_add(coro, 32), captures_tuple)
    pcc_gc_store_ptr(coro, ptr_add(coro, 40), args_tuple)
    if made_captures != 0:
        py_decref(captures_tuple)
    if made_args != 0:
        py_decref(args_tuple)
    py_gc_track(coro)
    pcc_gc_publish_initialized(coro)
    return coro


def _checked_coroutine(coro):
    if ptr_is_null(coro):
        _raise_typeerror(cstr("object is not a coroutine"))
        return null()
    if is_tagged_int(coro):
        _raise_typeerror(cstr("object is not a coroutine"))
        return null()
    if _type_of(coro) != PY_TYPE_COROUTINE:  # PY_TYPE_COROUTINE
        _raise_typeerror(cstr("object is not a coroutine"))
        return null()
    return coro


@c_abi_export("py_coroutine_run")
def py_coroutine_run(coro):
    coro = _checked_coroutine(coro)
    if ptr_is_null(coro):
        return null()
    if load_i32(coro, 56) != 0:
        _raise_runtimeerror(cstr("cannot reuse closed coroutine"))
        return null()
    if load_i32(coro, 60) != 0:
        _raise_runtimeerror(cstr("cannot reuse already awaited coroutine"))
        return null()
    entry = load_ptr(coro, 24)
    result = global_load_ptr("py_None")
    if not ptr_is_null(entry):
        captures = pcc_gc_load_ptr(coro, ptr_add(coro, 32))
        args = pcc_gc_load_ptr(coro, ptr_add(coro, 40))
        result = call_ptr2(entry, captures, args)
        if ptr_is_null(result):
            entry_name = load_ptr(coro, 16)
            if ptr_is_null(entry_name):
                entry_name = cstr("coroutine entry")
            return _coroutine_require_result(
                null(),
                entry_name,
                cstr("coroutine entry returned NULL without setting an exception"),
            )
    else:
        py_incref(result)
    pcc_gc_store_ptr(coro, ptr_add(coro, 48), result)
    store_i32(coro, 60, 1)
    return result


@c_abi_export("py_coroutine_is_done")
def py_coroutine_is_done(coro) -> int:
    coro = _checked_coroutine(coro)
    if ptr_is_null(coro):
        return 1
    if load_i32(coro, 60) != 0:
        return 1
    return 0


@c_abi_export("py_coroutine_get_result")
def py_coroutine_get_result(coro):
    coro = _checked_coroutine(coro)
    if ptr_is_null(coro):
        return null()
    result = pcc_gc_load_ptr(coro, ptr_add(coro, 48))
    if ptr_is_null(result):
        result = global_load_ptr("py_None")
    py_incref(result)
    return result


@c_abi_export("py_coroutine_close")
def py_coroutine_close(coro):
    if ptr_is_null(coro):
        return global_load_ptr("py_None")
    if is_tagged_int(coro):
        return global_load_ptr("py_None")
    if _type_of(coro) == PY_TYPE_COROUTINE:
        store_i32(coro, 56, 1)
    return global_load_ptr("py_None")


def _await_iterator(it):
    if ptr_is_null(it):
        return _coroutine_require_result(
            null(),
            cstr("await_iterator"),
            cstr("await iterator received NULL iterator"),
        )
    while True:
        item = py_obj_next(it)
        if not ptr_is_null(item):
            py_decref(item)
            continue
        cur = py_current_exception()
        stop_cls = py_exc_builtin_class(8)  # PY_EXC_STOPITERATION
        if py_exc_matches(cur, stop_cls) != 0:
            value = py_exc_get_message(cur)
            if ptr_is_null(value):
                value = global_load_ptr("py_None")
            py_incref(value)
            py_clear_exception()
            return value
        return null()


@c_abi_export("py_await")
def py_await(awaitable):
    if ptr_is_null(awaitable):
        return _coroutine_require_result(
            null(),
            cstr("py_await"),
            cstr("py_await received NULL awaitable"),
        )
    if is_tagged_int(awaitable) == 0:
        tag: int = _type_of(awaitable)
        if tag == PY_TYPE_COROUTINE:  # PY_TYPE_COROUTINE
            return py_coroutine_run(awaitable)
        if tag == PY_TYPE_GEN:  # PY_TYPE_GEN
            return _await_iterator(awaitable)
    method = py_obj_getattr(awaitable, cstr("__await__"))
    if not ptr_is_null(method):
        args = py_tuple_new(0)
        if ptr_is_null(args):
            _coroutine_require_result(
                null(),
                cstr("py_tuple_new"),
                cstr("__await__ could not allocate its argument tuple"),
            )
            py_decref(method)
            return null()
        iterator = py_obj_call(method, args, global_load_ptr("py_None"))
        _coroutine_require_result(
            iterator,
            cstr("__await__"),
            cstr("__await__ returned NULL without setting an exception"),
        )
        py_decref(args)
        py_decref(method)
        if ptr_is_null(iterator):
            return null()
        result = _await_iterator(iterator)
        py_decref(iterator)
        return result
    _raise_typeerror(cstr("object is not awaitable"))
    return null()


@c_abi_export("py_asyncio_sleep")
def py_asyncio_sleep(delay):
    return py_coroutine_new_native(cstr("sleep"), null(), null(), null())


@c_abi_export("py_coroutine_get_args")
def py_coroutine_get_args(coro):
    coro = _checked_coroutine(coro)
    if ptr_is_null(coro):
        return null()
    args = pcc_gc_load_ptr(coro, ptr_add(coro, 40))
    if ptr_is_null(args):
        args = global_load_ptr("py_None")
    py_incref(args)
    return args


def _continuation_slot_count_from_map(frame_map) -> int:
    if ptr_is_null(frame_map):
        return 0
    n: int = load_i32(frame_map, 0)
    if n > 0:
        return n
    return 0


def _continuation_chunk_slots(cont):
    chunk = load_ptr(cont, 24)
    if ptr_is_null(chunk):
        return null()
    return load_ptr(chunk, 16)


def _continuation_frame_map(cont):
    chunk = load_ptr(cont, 24)
    if ptr_is_null(chunk):
        return null()
    return chunk


def _checked_continuation(cont):
    if ptr_is_null(cont):
        _raise_typeerror(cstr("object is not a continuation"))
        return null()
    if is_tagged_int(cont):
        _raise_typeerror(cstr("object is not a continuation"))
        return null()
    cont = pcc_gc_note_relocation_read(cont)
    if _type_of(cont) != PY_TYPE_CONTINUATION:
        _raise_typeerror(cstr("object is not a continuation"))
        return null()
    return cont


@c_abi_export("py_continuation_class")
def py_continuation_class():
    cls = global_load_ptr("py_continuation_class_cache")
    if not ptr_is_null(cls):
        return cls
    cls = py_class_new(cstr("continuation"), null(), 0, null(), 0)
    if not ptr_is_null(cls):
        flags: int = load_i32(cls, PYOBJECTHEADER_FLAGS_OFFSET)
        store_i32(
            cls,
            PYOBJECTHEADER_FLAGS_OFFSET,
            flags | PY_FLAG_IMMORTAL,
        )
        global_store_ptr("py_continuation_class_cache", cls)
    return cls


def _py_continuation_new_with_abi(frame_map, slots, resume_pc, resume_abi: int):
    n_slots: int = _continuation_slot_count_from_map(frame_map)
    if n_slots > 0 and ptr_is_null(slots):
        _raise_typeerror(cstr("continuation slots are null"))
        return null()
    # 32, not 24: [24] holds the continuation-root registration node so
    # unregistering is O(1) instead of a walk of the whole root list.
    chunk = calloc(1, 32)
    if ptr_is_null(chunk):
        return null()
    store_i32(chunk, 0, n_slots)
    store_i32(chunk, 4, 0)
    store_i64(chunk, 8, n_slots)
    store_ptr(chunk, 16, null())
    store_ptr(chunk, 24, null())
    chunk_slots = null()
    if n_slots > 0:
        chunk_slots = calloc(n_slots, 8)
        if ptr_is_null(chunk_slots):
            free(chunk)
            return null()
        store_ptr(chunk, 16, chunk_slots)

    cont = pcc_gc_alloc(48, PY_TYPE_CONTINUATION, 0)
    if ptr_is_null(cont):
        if not ptr_is_null(chunk_slots):
            free(chunk_slots)
        free(chunk)
        return null()
    store_ptr(cont, 16, resume_pc)  # resume_pc
    store_ptr(cont, 24, chunk)  # stack_chunk
    store_i64(cont, 32, 1)  # mounted
    store_i64(cont, 40, resume_abi)  # typed resume ABI
    if n_slots > 0:
        pcc_gc_backend4_zpage_register_owner_payload_span(
            cont,
            chunk_slots,
            n_slots * 8,
        )

    i: int = 0
    while i < n_slots:
        value = load_ptr(slots, i * 8)
        pcc_gc_store_ptr(cont, ptr_add(chunk_slots, i * 8), value)
        i = i + 1
    py_gc_track(cont)
    pcc_gc_publish_initialized(cont)
    if py_continuation_unmount(cont, null(), resume_pc) != 0:
        py_decref(cont)
        return null()
    return cont


@c_abi_export("py_continuation_new")
def py_continuation_new(frame_map, slots, resume_pc):
    return _py_continuation_new_with_abi(frame_map, slots, resume_pc, 0)


@c_abi_export("py_continuation_new_typed")
def py_continuation_new_typed(frame_map, slots, resume_pc):
    return _py_continuation_new_with_abi(frame_map, slots, resume_pc, 1)


define_global_i32("pcc_continuation_root_handle_cache", -1)


def _continuation_root_handle_enabled() -> int:
    """Keep the O(n) removal reachable as a measurement control arm.

    ``PCC_CONTINUATION_ROOT_HANDLE=0`` makes unmount not retain the
    registration node, so removal falls back to searching the root list -- the
    behaviour before the handle existed.  One archive, one binary, two arms
    differing in exactly this, which is what an A/B of this change needs.
    Same cached-global idiom as pcc_debug_runtime_enabled.
    """
    slot = global_addr("pcc_continuation_root_handle_cache")
    cached: int = load_i32(slot, 0)
    if cached >= 0:
        return cached
    value: int = 1
    setting = pcc_platform_getenv(cstr("PCC_CONTINUATION_ROOT_HANDLE"))
    if ptr_is_null(setting) == 0:
        if load_i32(setting, 0) & 255 == 48:
            value = 0
    store_i32(slot, 0, value)
    return value


@c_abi_export("py_continuation_mount")
def py_continuation_mount(cont, slots_out) -> int:
    cont = _checked_continuation(cont)
    if ptr_is_null(cont):
        return -1
    chunk = load_ptr(cont, 24)
    if ptr_is_null(chunk):
        return -1
    slots = load_ptr(chunk, 16)
    n_slots: int = load_i64(chunk, 8)
    if load_i64(cont, 32) == 0:
        root_node = load_ptr(chunk, 24)
        store_ptr(chunk, 24, null())
        if ptr_is_null(root_node):
            pcc_gc_unregister_continuation_root(slots)
        else:
            pcc_gc_unregister_continuation_root_node(root_node)
    if not ptr_is_null(slots_out):
        i: int = 0
        while i < n_slots:
            value = load_ptr(slots, i * 8)
            pcc_gc_store_root(ptr_add(slots_out, i * 8), value)
            i = i + 1
    store_i64(cont, 32, 1)
    return 0


@c_abi_export("py_continuation_unmount")
def py_continuation_unmount(cont, slots_in, resume_pc) -> int:
    cont = _checked_continuation(cont)
    if ptr_is_null(cont):
        return -1
    chunk = load_ptr(cont, 24)
    if ptr_is_null(chunk):
        return -1
    slots = load_ptr(chunk, 16)
    n_slots: int = load_i64(chunk, 8)
    if not ptr_is_null(slots_in):
        i: int = 0
        while i < n_slots:
            value = load_ptr(slots_in, i * 8)
            pcc_gc_store_ptr(cont, ptr_add(slots, i * 8), value)
            i = i + 1
    store_ptr(cont, 16, resume_pc)
    if load_i64(cont, 32) != 0:
        if _continuation_root_handle_enabled() != 0:
            store_ptr(
                chunk,
                24,
                pcc_gc_register_continuation_root_node(
                    _continuation_frame_map(cont), slots
                ),
            )
        else:
            pcc_gc_register_continuation_root(
                _continuation_frame_map(cont), slots
            )
    store_i64(cont, 32, 0)
    return 0


@c_abi_export("py_continuation_is_mounted")
def py_continuation_is_mounted(cont) -> int:
    cont = _checked_continuation(cont)
    if ptr_is_null(cont):
        return 0
    if load_i64(cont, 32) != 0:
        return 1
    return 0


@c_abi_export("py_continuation_resume_pc")
def py_continuation_resume_pc(cont):
    cont = _checked_continuation(cont)
    if ptr_is_null(cont):
        return null()
    return load_ptr(cont, 16)


@c_abi_export("py_continuation_resume_abi")
def py_continuation_resume_abi(cont) -> int:
    cont = _checked_continuation(cont)
    if ptr_is_null(cont):
        return 0
    return load_i64(cont, 40)


@c_abi_export("py_continuation_slot_count")
def py_continuation_slot_count(cont) -> int:
    cont = _checked_continuation(cont)
    if ptr_is_null(cont):
        return 0
    chunk = load_ptr(cont, 24)
    if ptr_is_null(chunk):
        return 0
    return load_i64(chunk, 8)


@c_abi_export("py_continuation_get_slot")
def py_continuation_get_slot(cont, index: int):
    cont = _checked_continuation(cont)
    if ptr_is_null(cont):
        return null()
    chunk = load_ptr(cont, 24)
    if ptr_is_null(chunk):
        return null()
    n_slots: int = load_i64(chunk, 8)
    if index < 0 or index >= n_slots:
        exc = py_exc_new(5, cstr("continuation slot out of range"))
        py_raise_owned(exc)
        return null()
    slots = load_ptr(chunk, 16)
    value = pcc_gc_load_ptr(cont, ptr_add(slots, index * 8))
    if ptr_is_null(value):
        value = global_load_ptr("py_None")
    py_incref(value)
    return value


@c_abi_export("py_continuation_set_slot")
def py_continuation_set_slot(cont, index: int, value) -> int:
    cont = _checked_continuation(cont)
    if ptr_is_null(cont):
        return -1
    chunk = load_ptr(cont, 24)
    if ptr_is_null(chunk):
        return -1
    n_slots: int = load_i64(chunk, 8)
    if index < 0 or index >= n_slots:
        exc = py_exc_new(5, cstr("continuation slot out of range"))
        py_raise_owned(exc)
        return -1
    slots = load_ptr(chunk, 16)
    pcc_gc_store_ptr(cont, ptr_add(slots, index * 8), value)
    return 0


def _checked_task(task):
    original = task
    if ptr_is_null(task):
        _raise_typeerror(cstr("object is not a task"))
        return null()
    if is_tagged_int(task):
        _raise_typeerror(cstr("object is not a task"))
        return null()
    task = pcc_gc_note_relocation_read(task)
    if _type_of(task) != PY_TYPE_TASK:
        _raise_typeerror(cstr("object is not a task"))
        return null()
    if ptr_eq(task, original) == 0:
        if _type_of(original) == PY_TYPE_TASK:
            pcc_gc_store_ptr(original, ptr_add(original, 16), null())
            pcc_gc_store_ptr(original, ptr_add(original, 24), null())
            pcc_gc_store_ptr(original, ptr_add(original, 32), null())
    return task


@c_abi_export("py_task_new")
def py_task_new(coro):
    task = pcc_gc_alloc(48, PY_TYPE_TASK, 0)
    if ptr_is_null(task):
        return null()
    store_ptr(task, 16, null())  # coro
    store_ptr(task, 24, null())  # result
    store_ptr(task, 32, null())  # waiter
    store_i32(task, 40, 0)  # done low word
    store_i32(task, 44, 0)  # done high word / padding
    if ptr_is_null(coro):
        coro = global_load_ptr("py_None")
    pcc_gc_store_ptr(task, ptr_add(task, 16), coro)
    py_gc_track(task)
    pcc_gc_publish_initialized(task)
    return task


@c_abi_export("py_task_step")
def py_task_step(task):
    task = _checked_task(task)
    if ptr_is_null(task):
        return null()
    if load_i32(task, 40) != 0:
        result = pcc_gc_load_ptr(task, ptr_add(task, 24))
        if ptr_is_null(result):
            result = global_load_ptr("py_None")
        py_incref(result)
        return result
    coro = pcc_gc_load_ptr(task, ptr_add(task, 16))
    result = py_await(coro)
    if ptr_is_null(result):
        return null()
    pcc_gc_store_ptr(task, ptr_add(task, 24), result)
    pcc_gc_store_ptr(task, ptr_add(task, 32), null())
    store_i32(task, 40, 1)
    return result


@c_abi_export("py_task_is_done")
def py_task_is_done(task) -> int:
    task = _checked_task(task)
    if ptr_is_null(task):
        return 1
    if load_i32(task, 40) != 0:
        return 1
    return 0


@c_abi_export("py_task_set_result")
def py_task_set_result(task, result) -> None:
    task = _checked_task(task)
    if ptr_is_null(task):
        return
    if ptr_is_null(result):
        result = global_load_ptr("py_None")
    pcc_gc_store_ptr(task, ptr_add(task, 24), result)
    pcc_gc_store_ptr(task, ptr_add(task, 32), null())
    store_i32(task, 40, 1)


@c_abi_export("py_task_set_waiter")
def py_task_set_waiter(task, waiter) -> None:
    task = _checked_task(task)
    if ptr_is_null(task):
        return
    pcc_gc_store_ptr(task, ptr_add(task, 32), waiter)


@c_abi_export("py_task_get_coro")
def py_task_get_coro(task):
    task = _checked_task(task)
    if ptr_is_null(task):
        return null()
    coro = pcc_gc_load_ptr(task, ptr_add(task, 16))
    if ptr_is_null(coro):
        coro = global_load_ptr("py_None")
    py_incref(coro)
    return coro


@c_abi_export("py_task_get_result")
def py_task_get_result(task):
    task = _checked_task(task)
    if ptr_is_null(task):
        return null()
    result = pcc_gc_load_ptr(task, ptr_add(task, 24))
    if ptr_is_null(result):
        result = global_load_ptr("py_None")
    py_incref(result)
    return result


@c_abi_export("py_task_get_waiter")
def py_task_get_waiter(task):
    task = _checked_task(task)
    if ptr_is_null(task):
        return null()
    waiter = pcc_gc_load_ptr(task, ptr_add(task, 32))
    if ptr_is_null(waiter):
        waiter = global_load_ptr("py_None")
    py_incref(waiter)
    return waiter


@c_abi_export("py_dealloc_task")
def py_dealloc_task(o) -> None:
    coro = pcc_gc_load_ptr(o, ptr_add(o, 16))
    if not ptr_is_null(coro):
        py_decref(coro)
    result = pcc_gc_load_ptr(o, ptr_add(o, 24))
    if not ptr_is_null(result):
        py_decref(result)
    waiter = pcc_gc_load_ptr(o, ptr_add(o, 32))
    if not ptr_is_null(waiter):
        py_decref(waiter)
    pcc_gc_free_object_memory(o)


@c_abi_export("py_dealloc_coroutine")
def py_dealloc_coroutine(o) -> None:
    captures = pcc_gc_load_ptr(o, ptr_add(o, 32))
    if not ptr_is_null(captures):
        py_decref(captures)
    args = pcc_gc_load_ptr(o, ptr_add(o, 40))
    if not ptr_is_null(args):
        py_decref(args)
    result = pcc_gc_load_ptr(o, ptr_add(o, 48))
    if not ptr_is_null(result):
        py_decref(result)
    pcc_gc_free_object_memory(o)


@c_abi_export("py_dealloc_continuation")
def py_dealloc_continuation(o) -> None:
    chunk = load_ptr(o, 24)
    if not ptr_is_null(chunk):
        slots = load_ptr(chunk, 16)
        if load_i64(o, 32) == 0:
            root_node = load_ptr(chunk, 24)
            store_ptr(chunk, 24, null())
            if ptr_is_null(root_node):
                pcc_gc_unregister_continuation_root(slots)
            else:
                pcc_gc_unregister_continuation_root_node(root_node)
        n_slots: int = load_i64(chunk, 8)
        if not ptr_is_null(slots):
            i: int = 0
            while i < n_slots:
                value = pcc_gc_load_ptr(o, ptr_add(slots, i * 8))
                if not ptr_is_null(value):
                    py_decref(value)
                i = i + 1
            free(slots)
        free(chunk)
    store_ptr(o, 24, null())
    pcc_gc_free_object_memory(o)
