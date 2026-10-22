"""pcc-Python port of py_coroutine.c.

Coroutine object layout:
    offset  0   PyObjectHeader
    offset 16   name const char*
    offset 24   entry PyNativeFuncEntry
    offset 32   captures tuple
    offset 40   args tuple
    offset 48   suspended generator or cached result (selected by done)
    offset 56   closed i32
    offset 60   state: -3 executing, -2 suspended, -1 factory, 0 legacy, 1 done
    total size: 64 bytes
"""

__pcc_runtime_port__ = True

from pcc.extern import extern, c_abi_export, c_int32, c_int64, c_ptr, c_void
from pcc.runtime.py.py_abi_constants import PYOBJECTHEADER_FLAGS_OFFSET, PYOBJECTHEADER_TYPE_TAG_OFFSET, PY_FLAG_IMMORTAL, PY_TYPE_CONTINUATION, PY_TYPE_INT, PY_TYPE_TASK
from pcc.runtime.py.py_abi_constants import (
    PY_TYPE_CLASS,
    PY_TYPE_COROUTINE,
    PY_TYPE_EXC,
    PY_TYPE_GEN,
    PY_TYPE_TUPLE,
)
from pcc.unsafe import (
    atomic_rmw_i32,
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
    memset,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    stack_alloc,
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
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_obj_iter = extern("py_obj_iter", (c_ptr,), c_ptr)
py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_exc_new_with_value = extern("py_exc_new_with_value", (c_int64, c_ptr), c_ptr)
py_gen_send = extern("py_gen_send", (c_ptr, c_ptr), c_ptr)
py_gen_throw = extern("py_gen_throw", (c_ptr, c_ptr), c_ptr)
py_gen_close = extern("py_gen_close", (c_ptr,), c_ptr)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
pcc_gc_unpin = extern("pcc_gc_unpin", (c_ptr,), c_void)
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


@c_abi_export("py_coroutine_new_resumable")
def py_coroutine_new_resumable(name, entry, captures_tuple, args_tuple):
    coro = py_coroutine_new_native(name, entry, captures_tuple, args_tuple)
    if ptr_is_null(coro) == 0:
        store_i32(coro, 60, -1)
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
    if load_i32(coro, 60) < 0:
        result = py_coroutine_send(coro, global_load_ptr("py_None"), null())
        if ptr_is_null(result) == 0:
            py_decref(result)
            _raise_runtimeerror(cstr("suspended coroutine requires an asyncio event loop"))
            return null()
        error = py_current_exception()
        if py_exc_matches(error, py_exc_builtin_class(8)) != 0:
            value = py_exc_get_message(error)
            if ptr_is_null(value):
                value = global_load_ptr("py_None")
            py_incref(value)
            py_clear_exception()
            return value
        return null()
    if load_i32(coro, 60) == 1:
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
    if load_i32(coro, 60) == 1:
        return 1
    return 0


@c_abi_export("py_coroutine_get_result")
def py_coroutine_get_result(coro):
    coro = _checked_coroutine(coro)
    if ptr_is_null(coro):
        return null()
    result = null()
    if load_i32(coro, 60) == 1:
        result = pcc_gc_load_ptr(coro, ptr_add(coro, 48))
    if ptr_is_null(result):
        result = global_load_ptr("py_None")
    py_incref(result)
    return result


# Existing object layouts; the protocol adds no fields or trace families.
_CORO_ENTRY_OFFSET = 24
_CORO_CAPTURES_OFFSET = 32
_CORO_ARGUMENTS_OFFSET = 40
_CORO_SUSPENDED_OFFSET = 48
_CORO_CLOSED_OFFSET = 56
_CORO_STATE_OFFSET = 60
_CORO_EXCEPTION_VALUE_OFFSET = 24
_CORO_LEGACY_STATE = 0
_CORO_FRESH_STATE = -1
_CORO_SUSPENDED_STATE = -2
_CORO_EXECUTING_STATE = -3
_CORO_DONE_STATE = 1
_GEN_ENTRY_OFFSET = 16
_GEN_FRAME_OFFSET = 24
_GEN_STATE_OFFSET = 32
_GEN_DONE_OFFSET = 40
_GEN_SEND_OFFSET = 48
# Generator state 0 is unstarted; positive values identify suspension sites.
# Only an internal coroutine throw negates a positive suspension index. The
# negative index means GEN_SEND owns a tuple of ORIGINAL throw arguments.
# py_gen_state decodes the index for dispatch; take_throw_arguments restores
# the positive index and transfers GEN_SEND before handled scopes resume.
_GEN_INITIAL_STATE = 0


def _coroutine_resume(coro, value, error, slots, pins):
    state: int = load_i32(coro, _CORO_STATE_OFFSET)
    if state == _CORO_DONE_STATE or load_i32(coro, _CORO_CLOSED_OFFSET) != 0:
        _raise_runtimeerror(cstr("cannot reuse already awaited coroutine"))
        return null()
    if state == _CORO_EXECUTING_STATE:
        py_raise_owned(py_exc_new(2, cstr("coroutine already executing")))
        return null()
    if state == _CORO_LEGACY_STATE or state == _CORO_FRESH_STATE:
        if ptr_is_null(error) and ptr_is_null(value) == 0 and ptr_eq(value, global_load_ptr("py_None")) == 0:
            _raise_typeerror(cstr("cannot send non-None value to a just-started coroutine"))
            return null()
    if state == _CORO_LEGACY_STATE:
        if ptr_is_null(error) == 0:
            store_i32(coro, _CORO_STATE_OFFSET, _CORO_DONE_STATE)
            py_raise(error)
            return null()
        result = py_coroutine_run(coro)
        if ptr_is_null(result):
            store_i32(coro, _CORO_STATE_OFFSET, _CORO_DONE_STATE)
            return null()
        stopped = py_exc_new_with_value(8, result)
        py_decref(result)
        py_raise_owned(stopped)
        return null()
    if state == _CORO_FRESH_STATE:
        if ptr_is_null(error) == 0:
            store_i32(coro, _CORO_STATE_OFFSET, _CORO_DONE_STATE)
            py_raise(error)
            return null()
        if ptr_is_null(value) == 0 and ptr_eq(value, global_load_ptr("py_None")) == 0:
            _raise_typeerror(cstr("cannot send non-None value to a just-started coroutine"))
            return null()
        entry = load_ptr(coro, _CORO_ENTRY_OFFSET)
        store_ptr(slots, _CORO_TEMP, call_ptr2(entry, pcc_gc_load_ptr(coro, ptr_add(coro, _CORO_CAPTURES_OFFSET)), pcc_gc_load_ptr(coro, ptr_add(coro, _CORO_ARGUMENTS_OFFSET))))
        if _coroutine_slot_adopt(slots, pins, _CORO_TEMP) < 0:
            store_i32(coro, _CORO_STATE_OFFSET, _CORO_DONE_STATE)
            return null()
        iterator = load_ptr(slots, _CORO_TEMP)
        if ptr_is_null(iterator):
            store_i32(coro, _CORO_STATE_OFFSET, _CORO_DONE_STATE)
            return _coroutine_require_result(null(), cstr("coroutine factory"), cstr("coroutine factory returned NULL"))
        pcc_gc_store_ptr(coro, ptr_add(coro, _CORO_SUSPENDED_OFFSET), iterator)
        store_i32(coro, _CORO_STATE_OFFSET, _CORO_SUSPENDED_STATE)
    if _coroutine_slot_borrow(slots, pins, _CORO_ARGS, ptr_add(coro, _CORO_SUSPENDED_OFFSET)) < 0:
        return null()
    iterator = load_ptr(slots, _CORO_ARGS)
    store_i32(coro, _CORO_STATE_OFFSET, _CORO_EXECUTING_STATE)
    result = null()
    if ptr_is_null(error):
        result = py_gen_send(iterator, value)
    else:
        result = py_gen_throw(iterator, error)
    if ptr_is_null(result):
        store_i32(coro, _CORO_STATE_OFFSET, _CORO_DONE_STATE)
    else:
        store_i32(coro, _CORO_STATE_OFFSET, _CORO_SUSPENDED_STATE)
    return result


# Bound methods and the raw coroutine ABI share this ownership frame. The
# receiver/inputs/operand slots own the public call inputs. AUX and TEMP own
# extracted delegates, callable/argument temporaries, or completion values.
# RESULT and ERROR preserve the outcome across callbacks and owner release.
# Each owner has a parallel legacy-pin slot and counted-lease token. The
# final nonmanaged pins word remembers the borrowed-input frame to retire.
_CORO_RECEIVER = 0
_CORO_ARGS = 8
_CORO_OPERAND = 16
_CORO_AUX = 24
_CORO_TEMP = 32
_CORO_RESULT = 40
_CORO_ERROR = 48
_CORO_FRAME_SIZE = 56
_CORO_LEASES_BASE = 56  # one legacy-pin word for each owner slot
_CORO_BORROWED_FRAME = 112  # after seven pins and seven lease tokens
_CORO_PINS_SIZE = 120  # two scalar lanes plus one borrowed-frame pointer
define_global_i32("pcc_coroutine_protocol_frame_map", 7)
define_global_i32("pcc_coroutine_borrowed_one_map", -1)
define_global_i32("pcc_coroutine_borrowed_two_map", -2)
define_global_i32("pcc_coroutine_borrowed_three_map", -3)

pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
pcc_gc_frame_leave = extern("pcc_gc_frame_leave", (c_ptr,), c_void)
pcc_gc_root_copy_borrowed_lease = extern("pcc_gc_root_copy_borrowed_lease", (c_ptr, c_ptr), c_int64)
pcc_gc_foreign_lease_acquire = extern("pcc_gc_foreign_lease_acquire", (c_ptr,), c_int64)
pcc_gc_foreign_lease_release = extern("pcc_gc_foreign_lease_release", (c_ptr, c_int64), c_int64)
pcc_gc_take_pinned_slot = extern("pcc_gc_take_pinned_slot", (c_ptr, c_int64), c_ptr)
pcc_platform_abort = extern("pcc_platform_abort", (), c_void)
py_tls_exc_swap_slot = extern("py_tls_exc_swap_slot", (c_ptr,), c_void)
py_tuple_len = extern("py_tuple_len", (c_ptr,), c_int64)
py_tuple_get = extern("py_tuple_get", (c_ptr, c_int64), c_ptr)
py_func_new_bound = extern("py_func_new_bound", (c_ptr, c_ptr, c_ptr, c_ptr), c_ptr)


def _coroutine_slot_pin(slots, pins, offset: int) -> None:
    value = load_ptr(slots, offset)
    store_i64(pins, offset, 0)
    if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        store_i64(pins, offset, load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & 64)
        pcc_gc_pin(value)


def _coroutine_slot_adopt(slots, pins, offset: int) -> int:
    # A producer stores its NEW owner directly into the registered slot,
    # before calling this helper (whose entry can safepoint).
    store_i64(pins, offset, -1)
    token: int = pcc_gc_foreign_lease_acquire(ptr_add(slots, offset))
    if token < 0:
        pcc_gc_store_root(ptr_add(slots, offset), null())
        _raise_runtimeerror(cstr("coroutine result owner lease failed"))
        return -1
    store_i64(pins, _CORO_LEASES_BASE + offset, token)
    _coroutine_slot_pin(slots, pins, offset)
    return 0


def _coroutine_slot_borrow(slots, pins, offset: int, source) -> int:
    store_i64(pins, offset, -1)
    token: int = pcc_gc_root_copy_borrowed_lease(ptr_add(slots, offset), source)
    if token < 0:
        _raise_runtimeerror(cstr("coroutine argument owner lease failed"))
        return -1
    store_i64(pins, _CORO_LEASES_BASE + offset, token)
    _coroutine_slot_pin(slots, pins, offset)
    return 0


def _coroutine_slot_drop(slots, pins, offset: int) -> None:
    slot = ptr_add(slots, offset)
    if pcc_gc_foreign_lease_release(slot, load_i64(pins, _CORO_LEASES_BASE + offset)) != 0:
        pcc_platform_abort()
        return
    store_i64(pins, _CORO_LEASES_BASE + offset, 0)
    value = load_ptr(slots, offset)
    prior: int = load_i64(pins, offset)
    if prior >= 0 and ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        pcc_gc_unpin(value)
        if prior != 0:
            atomic_rmw_i32("or", value, PYOBJECTHEADER_FLAGS_OFFSET, 64, "relaxed")
    pcc_gc_store_root(slot, null())


def _coroutine_keep_error(slots, pins) -> None:
    if ptr_is_null(load_ptr(slots, _CORO_ERROR)):
        py_tls_exc_swap_slot(ptr_add(slots, _CORO_ERROR))
        if ptr_is_null(load_ptr(slots, _CORO_ERROR)) == 0:
            _coroutine_slot_adopt(slots, pins, _CORO_ERROR)


def _coroutine_finish(slots, pins):
    if ptr_is_null(load_ptr(slots, _CORO_RESULT)):
        _coroutine_keep_error(slots, pins)
    prior: int = load_i64(pins, _CORO_RESULT)
    offset: int = _CORO_RESULT
    while offset > 0:
        offset = offset - 8
        if ptr_eq(load_ptr(slots, _CORO_RESULT), load_ptr(slots, offset)):
            prior = load_i64(pins, offset)
        _coroutine_slot_drop(slots, pins, offset)
    if ptr_is_null(load_ptr(slots, _CORO_ERROR)) == 0:
        py_clear_exception()
        error = load_ptr(slots, _CORO_ERROR)
        error_prior: int = load_i64(pins, _CORO_ERROR)
        if pcc_gc_foreign_lease_release(ptr_add(slots, _CORO_ERROR), load_i64(pins, _CORO_LEASES_BASE + _CORO_ERROR)) != 0:
            pcc_platform_abort()
        py_tls_exc_swap_slot(ptr_add(slots, _CORO_ERROR))
        if error_prior >= 0:
            pcc_gc_unpin(error)
            if error_prior != 0:
                atomic_rmw_i32("or", error, PYOBJECTHEADER_FLAGS_OFFSET, 64, "relaxed")
    result = load_ptr(slots, _CORO_RESULT)
    if ptr_is_null(result) == 0 and is_tagged_int(result) == 0:
        atomic_rmw_i32("or", result, PYOBJECTHEADER_FLAGS_OFFSET, 64, "relaxed")
    pcc_gc_frame_leave(slots)
    pcc_gc_frame_leave(load_ptr(pins, _CORO_BORROWED_FRAME))
    if pcc_gc_foreign_lease_release(ptr_add(slots, _CORO_RESULT), load_i64(pins, _CORO_LEASES_BASE + _CORO_RESULT)) != 0:
        pcc_platform_abort()
        return null()
    return pcc_gc_take_pinned_slot(ptr_add(slots, _CORO_RESULT), prior)


def _coroutine_release_completed(slots, pins, keep_result: int) -> None:
    # Save the original outcome before frame destruction can invoke finalizers.
    _coroutine_keep_error(slots, pins)
    _coroutine_slot_drop(slots, pins, _CORO_TEMP)
    if keep_result != 0:
        error = load_ptr(slots, _CORO_ERROR)
        if py_exc_matches(error, py_exc_builtin_class(8)) != 0:
            if _type_of(load_ptr(slots, _CORO_ERROR)) == PY_TYPE_EXC:
                _coroutine_slot_borrow(slots, pins, _CORO_TEMP, ptr_add(load_ptr(slots, _CORO_ERROR), _CORO_EXCEPTION_VALUE_OFFSET))
    completed = load_ptr(slots, _CORO_TEMP)
    if ptr_is_null(completed):
        completed = global_load_ptr("py_None")
    coro = load_ptr(slots, _CORO_RECEIVER)
    pcc_gc_store_ptr(coro, ptr_add(coro, _CORO_SUSPENDED_OFFSET), completed)
    pcc_gc_store_ptr(coro, ptr_add(coro, _CORO_CAPTURES_OFFSET), null())
    pcc_gc_store_ptr(coro, ptr_add(coro, _CORO_ARGUMENTS_OFFSET), null())


@c_abi_export("py_coroutine_send")
def py_coroutine_send(coro, value, error):
    borrowed = stack_alloc(24)
    store_ptr(borrowed, 0, coro)
    store_ptr(borrowed, 8, value)
    store_ptr(borrowed, 16, error)
    pcc_gc_frame_enter(global_addr("pcc_coroutine_borrowed_three_map"), borrowed)
    slots = stack_alloc(_CORO_FRAME_SIZE)
    pins = stack_alloc(_CORO_PINS_SIZE)
    memset(slots, 0, _CORO_FRAME_SIZE)
    memset(pins, 0, _CORO_BORROWED_FRAME)
    store_ptr(pins, _CORO_BORROWED_FRAME, borrowed)
    pcc_gc_frame_enter(global_addr("pcc_coroutine_protocol_frame_map"), slots)
    if _coroutine_slot_borrow(slots, pins, _CORO_RECEIVER, borrowed) < 0:
        return _coroutine_finish(slots, pins)
    if _coroutine_slot_borrow(slots, pins, _CORO_OPERAND, ptr_add(borrowed, 8)) < 0:
        return _coroutine_finish(slots, pins)
    if _coroutine_slot_borrow(slots, pins, _CORO_AUX, ptr_add(borrowed, 16)) < 0:
        return _coroutine_finish(slots, pins)
    if ptr_is_null(_checked_coroutine(load_ptr(slots, _CORO_RECEIVER))):
        return _coroutine_finish(slots, pins)
    store_ptr(slots, _CORO_RESULT, _coroutine_resume(load_ptr(slots, _CORO_RECEIVER), load_ptr(slots, _CORO_OPERAND), load_ptr(slots, _CORO_AUX), slots, pins))
    _coroutine_slot_adopt(slots, pins, _CORO_RESULT)
    if load_i32(load_ptr(slots, _CORO_RECEIVER), _CORO_STATE_OFFSET) == _CORO_DONE_STATE:
        _coroutine_release_completed(slots, pins, 1)
    return _coroutine_finish(slots, pins)


@c_abi_export("py_coroutine_close")
def py_coroutine_close(coro):
    borrowed = stack_alloc(8)
    store_ptr(borrowed, 0, coro)
    pcc_gc_frame_enter(global_addr("pcc_coroutine_borrowed_one_map"), borrowed)
    slots = stack_alloc(_CORO_FRAME_SIZE)
    pins = stack_alloc(_CORO_PINS_SIZE)
    memset(slots, 0, _CORO_FRAME_SIZE)
    memset(pins, 0, _CORO_BORROWED_FRAME)
    store_ptr(pins, _CORO_BORROWED_FRAME, borrowed)
    pcc_gc_frame_enter(global_addr("pcc_coroutine_protocol_frame_map"), slots)
    if _coroutine_slot_borrow(slots, pins, _CORO_RECEIVER, borrowed) < 0:
        return _coroutine_finish(slots, pins)
    coro = _checked_coroutine(load_ptr(slots, _CORO_RECEIVER))
    if ptr_is_null(coro):
        return _coroutine_finish(slots, pins)
    state: int = load_i32(coro, _CORO_STATE_OFFSET)
    if state == _CORO_EXECUTING_STATE:
        py_raise_owned(py_exc_new(2, cstr("coroutine already executing")))
        return _coroutine_finish(slots, pins)
    if state == _CORO_SUSPENDED_STATE:
        if _coroutine_slot_borrow(slots, pins, _CORO_ARGS, ptr_add(coro, _CORO_SUSPENDED_OFFSET)) < 0:
            return _coroutine_finish(slots, pins)
        store_i32(coro, _CORO_STATE_OFFSET, _CORO_EXECUTING_STATE)
        store_ptr(slots, _CORO_AUX, py_gen_close(load_ptr(slots, _CORO_ARGS)))
        _coroutine_slot_adopt(slots, pins, _CORO_AUX)
        coro = load_ptr(slots, _CORO_RECEIVER)
        iterator = pcc_gc_load_ptr(coro, ptr_add(coro, _CORO_SUSPENDED_OFFSET))
        if ptr_is_null(load_ptr(slots, _CORO_AUX)) and load_i64(iterator, _GEN_DONE_OFFSET) == 0:
            # A body that yields during GeneratorExit is still suspended.
            store_i32(coro, _CORO_STATE_OFFSET, _CORO_SUSPENDED_STATE)
            return _coroutine_finish(slots, pins)
    store_i32(coro, _CORO_CLOSED_OFFSET, 1)
    store_i32(coro, _CORO_STATE_OFFSET, _CORO_DONE_STATE)
    _coroutine_release_completed(slots, pins, 0)
    if ptr_is_null(load_ptr(slots, _CORO_ERROR)):
        if state == _CORO_SUSPENDED_STATE:
            _coroutine_slot_borrow(slots, pins, _CORO_RESULT, ptr_add(slots, _CORO_AUX))
        else:
            pcc_gc_store_root(ptr_add(slots, _CORO_RESULT), global_load_ptr("py_None"))
            _coroutine_slot_adopt(slots, pins, _CORO_RESULT)
    return _coroutine_finish(slots, pins)


def _coroutine_normalize_throw(slots, pins, count: int) -> int:
    # Validate completely before resuming or closing the target coroutine.
    if count < 1 or count > 3:
        _raise_typeerror(cstr("throw() takes one to three arguments"))
        return -1
    store_ptr(slots, _CORO_OPERAND, py_tuple_get(load_ptr(slots, _CORO_ARGS), 0))
    if _coroutine_slot_adopt(slots, pins, _CORO_OPERAND) < 0:
        return -1
    if count == 3:
        store_ptr(slots, _CORO_AUX, py_tuple_get(load_ptr(slots, _CORO_ARGS), 2))
        if _coroutine_slot_adopt(slots, pins, _CORO_AUX) < 0:
            return -1
        if ptr_eq(load_ptr(slots, _CORO_AUX), global_load_ptr("py_None")) == 0:
            _raise_typeerror(cstr("throw() third argument must be a traceback object"))
            return -1
        _coroutine_slot_drop(slots, pins, _CORO_AUX)
    if py_exc_matches(load_ptr(slots, _CORO_OPERAND), py_exc_builtin_class(0)) == 0:
        _raise_typeerror(cstr("exceptions must be classes or instances deriving from BaseException"))
        return -1
    if count >= 2:
        store_ptr(slots, _CORO_AUX, py_tuple_get(load_ptr(slots, _CORO_ARGS), 1))
        if _coroutine_slot_adopt(slots, pins, _CORO_AUX) < 0:
            return -1
    value = load_ptr(slots, _CORO_AUX)
    if _type_of(load_ptr(slots, _CORO_OPERAND)) != PY_TYPE_CLASS:
        if ptr_is_null(value) == 0 and ptr_eq(value, global_load_ptr("py_None")) == 0:
            _raise_typeerror(cstr("instance exception may not have a separate value"))
            return -1
        return 0
    if ptr_is_null(value) == 0 and py_exc_matches(value, load_ptr(slots, _CORO_OPERAND)) != 0 and _type_of(value) != PY_TYPE_CLASS:
        _coroutine_slot_drop(slots, pins, _CORO_OPERAND)
        return _coroutine_slot_borrow(slots, pins, _CORO_OPERAND, ptr_add(slots, _CORO_AUX))
    arity: int = 0
    if ptr_is_null(value) == 0 and ptr_eq(value, global_load_ptr("py_None")) == 0:
        arity = 1
        if _type_of(value) == PY_TYPE_TUPLE:
            if _coroutine_slot_borrow(slots, pins, _CORO_TEMP, ptr_add(slots, _CORO_AUX)) < 0:
                return -1
    if ptr_is_null(load_ptr(slots, _CORO_TEMP)):
        store_ptr(slots, _CORO_TEMP, py_tuple_new(arity))
        if _coroutine_slot_adopt(slots, pins, _CORO_TEMP) < 0:
            return -1
        if arity == 1:
            py_tuple_set_item(load_ptr(slots, _CORO_TEMP), 0, load_ptr(slots, _CORO_AUX))
    _coroutine_slot_drop(slots, pins, _CORO_AUX)
    store_ptr(slots, _CORO_AUX, py_obj_call(load_ptr(slots, _CORO_OPERAND), load_ptr(slots, _CORO_TEMP), null()))
    if _coroutine_slot_adopt(slots, pins, _CORO_AUX) < 0 or ptr_is_null(load_ptr(slots, _CORO_AUX)):
        return -1
    if _type_of(load_ptr(slots, _CORO_AUX)) == PY_TYPE_CLASS or py_exc_matches(load_ptr(slots, _CORO_AUX), py_exc_builtin_class(0)) == 0:
        _raise_typeerror(cstr("calling exception class did not return a BaseException instance"))
        return -1
    _coroutine_slot_drop(slots, pins, _CORO_OPERAND)
    return _coroutine_slot_borrow(slots, pins, _CORO_OPERAND, ptr_add(slots, _CORO_AUX))


def _coroutine_throw_call(slots, pins) -> None:
    count: int = py_tuple_len(load_ptr(slots, _CORO_ARGS))
    if count < 1 or count > 3:
        _raise_typeerror(cstr("throw() takes one to three arguments"))
        return
    coro = load_ptr(slots, _CORO_RECEIVER)
    state: int = load_i32(coro, _CORO_STATE_OFFSET)
    if state == _CORO_DONE_STATE or load_i32(coro, _CORO_CLOSED_OFFSET) != 0:
        _raise_runtimeerror(cstr("cannot reuse already awaited coroutine"))
        return
    if state == _CORO_EXECUTING_STATE:
        py_raise_owned(py_exc_new(2, cstr("coroutine already executing")))
        return
    if state == _CORO_SUSPENDED_STATE:
        # A negative resume index means the existing traced send slot owns
        # the original throw-argument tuple. No synthetic exception enters
        # TLS: generated await resume code consumes this slot before restoring
        # handled exceptions or running any user callback.
        if _coroutine_slot_borrow(slots, pins, _CORO_AUX, ptr_add(coro, _CORO_SUSPENDED_OFFSET)) < 0:
            return
        gen = load_ptr(slots, _CORO_AUX)
        resume_state: int = load_i64(gen, _GEN_STATE_OFFSET)
        if resume_state <= _GEN_INITIAL_STATE:
            _raise_runtimeerror(cstr("coroutine has no suspended await state"))
            return
        if _coroutine_slot_borrow(slots, pins, _CORO_TEMP, ptr_add(gen, _GEN_FRAME_OFFSET)) < 0:
            return
        store_i32(coro, _CORO_STATE_OFFSET, _CORO_EXECUTING_STATE)
        pcc_gc_store_ptr(gen, ptr_add(gen, _GEN_SEND_OFFSET), load_ptr(slots, _CORO_ARGS))
        store_i64(gen, _GEN_STATE_OFFSET, -resume_state)
        store_ptr(slots, _CORO_RESULT, call_ptr2(load_ptr(gen, _GEN_ENTRY_OFFSET), gen, load_ptr(slots, _CORO_TEMP)))
        # Capture the NEW owner before a state helper can safepoint.
        _coroutine_slot_adopt(slots, pins, _CORO_RESULT)
        coro = load_ptr(slots, _CORO_RECEIVER)
        if ptr_is_null(load_ptr(slots, _CORO_RESULT)):
            py_runtime_error_if_unset(cstr("coroutine throw"), cstr("coroutine throw returned NULL without setting an exception"))
            store_i32(coro, _CORO_STATE_OFFSET, _CORO_DONE_STATE)
            _coroutine_release_completed(slots, pins, 1)
        else:
            store_i32(coro, _CORO_STATE_OFFSET, _CORO_SUSPENDED_STATE)
        return
    # Exception construction can run user code. Match CPython's executing
    # guard while it runs, restoring a fresh coroutine after validation fails.
    store_i32(coro, _CORO_STATE_OFFSET, _CORO_EXECUTING_STATE)
    if _coroutine_normalize_throw(slots, pins, count) < 0:
        store_i32(load_ptr(slots, _CORO_RECEIVER), _CORO_STATE_OFFSET, state)
        return
    store_i32(load_ptr(slots, _CORO_RECEIVER), _CORO_STATE_OFFSET, _CORO_DONE_STATE)
    py_raise(load_ptr(slots, _CORO_OPERAND))
    _coroutine_release_completed(slots, pins, 1)


@c_abi_export("py_coroutine_has_throw_arguments")
def py_coroutine_has_throw_arguments(gen) -> int:
    return 1 if load_i64(gen, _GEN_STATE_OFFSET) < _GEN_INITIAL_STATE else 0


@c_abi_export("py_coroutine_take_throw_arguments")
def py_coroutine_take_throw_arguments(gen):
    borrowed = stack_alloc(8)
    store_ptr(borrowed, 0, gen)
    pcc_gc_frame_enter(global_addr("pcc_coroutine_borrowed_one_map"), borrowed)
    slots = stack_alloc(_CORO_FRAME_SIZE)
    pins = stack_alloc(_CORO_PINS_SIZE)
    memset(slots, 0, _CORO_FRAME_SIZE)
    memset(pins, 0, _CORO_BORROWED_FRAME)
    store_ptr(pins, _CORO_BORROWED_FRAME, borrowed)
    pcc_gc_frame_enter(global_addr("pcc_coroutine_protocol_frame_map"), slots)
    if _coroutine_slot_borrow(slots, pins, _CORO_RECEIVER, borrowed) < 0:
        return _coroutine_finish(slots, pins)
    gen = load_ptr(slots, _CORO_RECEIVER)
    state: int = load_i64(gen, _GEN_STATE_OFFSET)
    if state >= _GEN_INITIAL_STATE:
        _raise_runtimeerror(cstr("coroutine has no pending throw arguments"))
        return _coroutine_finish(slots, pins)
    if _coroutine_slot_borrow(slots, pins, _CORO_RESULT, ptr_add(gen, _GEN_SEND_OFFSET)) < 0:
        return _coroutine_finish(slots, pins)
    store_i64(gen, _GEN_STATE_OFFSET, -state)
    pcc_gc_store_ptr(gen, ptr_add(gen, _GEN_SEND_OFFSET), global_load_ptr("py_None"))
    return _coroutine_finish(slots, pins)


def _await_close_iterator(slots, pins) -> int:
    tag: int = _type_of(load_ptr(slots, _CORO_RECEIVER))
    if tag == PY_TYPE_COROUTINE:
        store_ptr(slots, _CORO_RESULT, py_coroutine_close(load_ptr(slots, _CORO_RECEIVER)))
    elif tag == PY_TYPE_GEN:
        store_ptr(slots, _CORO_RESULT, py_gen_close(load_ptr(slots, _CORO_RECEIVER)))
    else:
        store_ptr(slots, _CORO_TEMP, py_obj_getattr(load_ptr(slots, _CORO_RECEIVER), cstr("close")))
        _coroutine_slot_adopt(slots, pins, _CORO_TEMP)
        if ptr_is_null(load_ptr(slots, _CORO_TEMP)):
            if py_exc_matches(py_current_exception(), py_exc_builtin_class(6)) == 0:
                return -1
            py_clear_exception()
            return 0
        store_ptr(slots, _CORO_AUX, py_tuple_new(0))
        if _coroutine_slot_adopt(slots, pins, _CORO_AUX) < 0 or ptr_is_null(load_ptr(slots, _CORO_AUX)):
            return -1
        store_ptr(slots, _CORO_RESULT, py_obj_call(load_ptr(slots, _CORO_TEMP), load_ptr(slots, _CORO_AUX), null()))
    _coroutine_slot_adopt(slots, pins, _CORO_RESULT)
    if ptr_is_null(load_ptr(slots, _CORO_RESULT)):
        return -1
    # A delegate close return value is discarded; only the outer coroutine's
    # own return value belongs to the public close() result.
    _coroutine_slot_drop(slots, pins, _CORO_RESULT)
    _coroutine_slot_drop(slots, pins, _CORO_TEMP)
    _coroutine_slot_drop(slots, pins, _CORO_AUX)
    return 0


def _await_close_and_raise(iterator, error):
    borrowed = stack_alloc(16)
    store_ptr(borrowed, 0, iterator)
    store_ptr(borrowed, 8, error)
    pcc_gc_frame_enter(global_addr("pcc_coroutine_borrowed_two_map"), borrowed)
    slots = stack_alloc(_CORO_FRAME_SIZE)
    pins = stack_alloc(_CORO_PINS_SIZE)
    memset(slots, 0, _CORO_FRAME_SIZE)
    memset(pins, 0, _CORO_BORROWED_FRAME)
    store_ptr(pins, _CORO_BORROWED_FRAME, borrowed)
    pcc_gc_frame_enter(global_addr("pcc_coroutine_protocol_frame_map"), slots)
    if _coroutine_slot_borrow(slots, pins, _CORO_RECEIVER, borrowed) < 0:
        return _coroutine_finish(slots, pins)
    if _coroutine_slot_borrow(slots, pins, _CORO_OPERAND, ptr_add(borrowed, 8)) < 0:
        return _coroutine_finish(slots, pins)
    if _await_close_iterator(slots, pins) == 0:
        py_raise(load_ptr(slots, _CORO_OPERAND))
    return _coroutine_finish(slots, pins)


@c_abi_export("py_await_throw_arguments")
def py_await_throw_arguments(iterator, args):
    borrowed = stack_alloc(16)
    store_ptr(borrowed, 0, iterator)
    store_ptr(borrowed, 8, args)
    pcc_gc_frame_enter(global_addr("pcc_coroutine_borrowed_two_map"), borrowed)
    slots = stack_alloc(_CORO_FRAME_SIZE)
    pins = stack_alloc(_CORO_PINS_SIZE)
    memset(slots, 0, _CORO_FRAME_SIZE)
    memset(pins, 0, _CORO_BORROWED_FRAME)
    store_ptr(pins, _CORO_BORROWED_FRAME, borrowed)
    pcc_gc_frame_enter(global_addr("pcc_coroutine_protocol_frame_map"), slots)
    if _coroutine_slot_borrow(slots, pins, _CORO_RECEIVER, borrowed) < 0:
        return _coroutine_finish(slots, pins)
    if _coroutine_slot_borrow(slots, pins, _CORO_ARGS, ptr_add(borrowed, 8)) < 0:
        return _coroutine_finish(slots, pins)
    store_ptr(slots, _CORO_OPERAND, py_tuple_get(load_ptr(slots, _CORO_ARGS), 0))
    _coroutine_slot_adopt(slots, pins, _CORO_OPERAND)
    closing: int = py_exc_matches(load_ptr(slots, _CORO_OPERAND), py_exc_builtin_class(55))
    _coroutine_slot_drop(slots, pins, _CORO_OPERAND)
    if closing != 0:
        if _await_close_iterator(slots, pins) < 0:
            return _coroutine_finish(slots, pins)
        if _coroutine_normalize_throw(slots, pins, py_tuple_len(load_ptr(slots, _CORO_ARGS))) == 0:
            py_raise(load_ptr(slots, _CORO_OPERAND))
        return _coroutine_finish(slots, pins)
    tag: int = _type_of(load_ptr(slots, _CORO_RECEIVER))
    if tag == PY_TYPE_COROUTINE:
        _coroutine_throw_call(slots, pins)
        return _coroutine_finish(slots, pins)
    elif tag == PY_TYPE_GEN:
        if _coroutine_normalize_throw(slots, pins, py_tuple_len(load_ptr(slots, _CORO_ARGS))) < 0:
            return _coroutine_finish(slots, pins)
        store_ptr(slots, _CORO_RESULT, py_gen_throw(load_ptr(slots, _CORO_RECEIVER), load_ptr(slots, _CORO_OPERAND)))
    else:
        store_ptr(slots, _CORO_TEMP, py_obj_getattr(load_ptr(slots, _CORO_RECEIVER), cstr("throw")))
        _coroutine_slot_adopt(slots, pins, _CORO_TEMP)
        if ptr_is_null(load_ptr(slots, _CORO_TEMP)):
            if py_exc_matches(py_current_exception(), py_exc_builtin_class(6)) == 0:
                return _coroutine_finish(slots, pins)
            py_clear_exception()
            if _coroutine_normalize_throw(slots, pins, py_tuple_len(load_ptr(slots, _CORO_ARGS))) < 0:
                return _coroutine_finish(slots, pins)
            py_raise(load_ptr(slots, _CORO_OPERAND))
            return _coroutine_finish(slots, pins)
        store_ptr(slots, _CORO_RESULT, py_obj_call(load_ptr(slots, _CORO_TEMP), load_ptr(slots, _CORO_ARGS), null()))
    _coroutine_slot_adopt(slots, pins, _CORO_RESULT)
    return _coroutine_finish(slots, pins)


def _coroutine_method_entry(captures, args, operation: int):
    borrowed = stack_alloc(16)
    store_ptr(borrowed, 0, captures)
    store_ptr(borrowed, 8, args)
    pcc_gc_frame_enter(global_addr("pcc_coroutine_borrowed_two_map"), borrowed)
    slots = stack_alloc(_CORO_FRAME_SIZE)
    pins = stack_alloc(_CORO_PINS_SIZE)
    memset(slots, 0, _CORO_FRAME_SIZE)
    memset(pins, 0, _CORO_BORROWED_FRAME)
    store_ptr(pins, _CORO_BORROWED_FRAME, borrowed)
    pcc_gc_frame_enter(global_addr("pcc_coroutine_protocol_frame_map"), slots)
    if _coroutine_slot_borrow(slots, pins, _CORO_TEMP, borrowed) < 0:
        return _coroutine_finish(slots, pins)
    if _coroutine_slot_borrow(slots, pins, _CORO_ARGS, ptr_add(borrowed, 8)) < 0:
        return _coroutine_finish(slots, pins)
    count: int = py_tuple_len(load_ptr(slots, _CORO_ARGS))
    if operation == 0 and count != 1:
        _raise_typeerror(cstr("send() takes exactly one argument"))
        return _coroutine_finish(slots, pins)
    if operation == 2 and count != 0:
        _raise_typeerror(cstr("close() takes no arguments"))
        return _coroutine_finish(slots, pins)
    store_ptr(slots, _CORO_RECEIVER, py_tuple_get(load_ptr(slots, _CORO_TEMP), 0))
    _coroutine_slot_adopt(slots, pins, _CORO_RECEIVER)
    _coroutine_slot_drop(slots, pins, _CORO_TEMP)
    if operation == 0:
        store_ptr(slots, _CORO_OPERAND, py_tuple_get(load_ptr(slots, _CORO_ARGS), 0))
        _coroutine_slot_adopt(slots, pins, _CORO_OPERAND)
        store_ptr(slots, _CORO_RESULT, py_coroutine_send(load_ptr(slots, _CORO_RECEIVER), load_ptr(slots, _CORO_OPERAND), null()))
    elif operation == 1:
        _coroutine_throw_call(slots, pins)
        return _coroutine_finish(slots, pins)
    else:
        store_ptr(slots, _CORO_RESULT, py_coroutine_close(load_ptr(slots, _CORO_RECEIVER)))
    _coroutine_slot_adopt(slots, pins, _CORO_RESULT)
    return _coroutine_finish(slots, pins)


def _coroutine_send_entry(captures, args):
    return _coroutine_method_entry(captures, args, 0)


def _coroutine_throw_entry(captures, args):
    return _coroutine_method_entry(captures, args, 1)


def _coroutine_close_entry(captures, args):
    return _coroutine_method_entry(captures, args, 2)


@c_abi_export("py_coroutine_bound_method")
def py_coroutine_bound_method(coro, operation: int):
    borrowed = stack_alloc(8)
    store_ptr(borrowed, 0, coro)
    pcc_gc_frame_enter(global_addr("pcc_coroutine_borrowed_one_map"), borrowed)
    slots = stack_alloc(_CORO_FRAME_SIZE)
    pins = stack_alloc(_CORO_PINS_SIZE)
    memset(slots, 0, _CORO_FRAME_SIZE)
    memset(pins, 0, _CORO_BORROWED_FRAME)
    store_ptr(pins, _CORO_BORROWED_FRAME, borrowed)
    pcc_gc_frame_enter(global_addr("pcc_coroutine_protocol_frame_map"), slots)
    if _coroutine_slot_borrow(slots, pins, _CORO_RECEIVER, borrowed) < 0:
        return _coroutine_finish(slots, pins)
    store_ptr(slots, _CORO_ARGS, py_tuple_new(1))
    if _coroutine_slot_adopt(slots, pins, _CORO_ARGS) < 0 or ptr_is_null(load_ptr(slots, _CORO_ARGS)):
        return _coroutine_finish(slots, pins)
    py_tuple_set_item(load_ptr(slots, _CORO_ARGS), 0, load_ptr(slots, _CORO_RECEIVER))
    if operation == 0:
        store_ptr(slots, _CORO_RESULT, py_func_new_bound(_coroutine_send_entry, load_ptr(slots, _CORO_ARGS), cstr("send"), load_ptr(slots, _CORO_RECEIVER)))
    elif operation == 1:
        store_ptr(slots, _CORO_RESULT, py_func_new_bound(_coroutine_throw_entry, load_ptr(slots, _CORO_ARGS), cstr("throw"), load_ptr(slots, _CORO_RECEIVER)))
    else:
        store_ptr(slots, _CORO_RESULT, py_func_new_bound(_coroutine_close_entry, load_ptr(slots, _CORO_ARGS), cstr("close"), load_ptr(slots, _CORO_RECEIVER)))
    _coroutine_slot_adopt(slots, pins, _CORO_RESULT)
    return _coroutine_finish(slots, pins)


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


@c_abi_export("py_await_iterator")
def py_await_iterator(awaitable):
    if ptr_is_null(awaitable) or is_tagged_int(awaitable):
        _raise_typeerror(cstr("object is not awaitable"))
        return null()
    if _type_of(awaitable) == PY_TYPE_COROUTINE:
        py_incref(awaitable)
        return awaitable
    method = py_obj_getattr(awaitable, cstr("__await__"))
    if ptr_is_null(method):
        py_clear_exception()
        _raise_typeerror(cstr("object is not awaitable"))
        return null()
    args = py_tuple_new(0)
    if ptr_is_null(args):
        py_decref(method)
        return null()
    iterator = py_obj_call(method, args, global_load_ptr("py_None"))
    py_decref(args)
    py_decref(method)
    if ptr_is_null(iterator):
        return null()
    # __await__ returns an iterator, not another coroutine or arbitrary value.
    checked = py_obj_iter(iterator)
    if ptr_is_null(checked):
        py_decref(iterator)
        return null()
    if ptr_eq(checked, iterator) == 0:
        py_decref(checked)
        py_decref(iterator)
        _raise_typeerror(cstr("__await__ returned a non-iterator"))
        return null()
    py_decref(checked)
    return iterator


@c_abi_export("py_await_step")
def py_await_step(iterator, value, error):
    if ptr_is_null(iterator) or is_tagged_int(iterator):
        _raise_typeerror(cstr("invalid await iterator"))
        return null()
    # "No exception" arrives here spelled two ways. The compiler passes NULL;
    # a Python-level caller passes None, which is a real object pointer. Every
    # test below decides between send and throw by null-checking `error`, so
    # None used to select throw and asyncio's `Task._step` failed every
    # ordinary resume with "exceptions must derive from BaseException". The
    # `value` argument has always accepted either spelling (see the send test
    # further down); this makes `error` agree with it.
    if ptr_is_null(error) == 0 and ptr_eq(error, global_load_ptr("py_None")) != 0:
        error = null()
    if ptr_is_null(error) == 0 and py_exc_matches(error, py_exc_builtin_class(55)) != 0:
        return _await_close_and_raise(iterator, error)
    tag: int = _type_of(iterator)
    if tag == PY_TYPE_COROUTINE:
        return py_coroutine_send(iterator, value, error)
    if tag == PY_TYPE_GEN:
        if ptr_is_null(error) == 0:
            return py_gen_throw(iterator, error)
        return py_gen_send(iterator, value)
    if ptr_is_null(error) and (ptr_is_null(value) or ptr_eq(value, global_load_ptr("py_None")) != 0):
        return py_obj_next(iterator)
    method = null()
    argument = value
    if ptr_is_null(error):
        method = py_obj_getattr(iterator, cstr("send"))
    else:
        method = py_obj_getattr(iterator, cstr("throw"))
        argument = error
    if ptr_is_null(method):
        if ptr_is_null(error) == 0:
            py_clear_exception()
            py_raise(error)
        return null()
    args = py_tuple_new(1)
    if ptr_is_null(args):
        py_decref(method)
        return null()
    py_tuple_set_item(args, 0, argument)
    result = py_obj_call(method, args, global_load_ptr("py_None"))
    py_decref(args)
    py_decref(method)
    return result


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
