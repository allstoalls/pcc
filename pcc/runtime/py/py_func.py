"""pcc-Python port of py_func.c.

Function object layout (unchanged):
    offset  0   PyObjectHeader (compiler entry roles use header flag bits)
    offset 16   C-API-compatible function prefix, through offset 48
    offset 56   PyNativeFuncEntry
    offset 64   captures tuple
    offset 72   borrowed const char* name, nullable
    offset 80   bound self object, nullable
    offset 88   attributes, nullable
    total size: 96 bytes
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import (
    C_POINTER_SIZE,
    PYOBJECTHEADER_TYPE_TAG_OFFSET,
    PYTUPLEOBJECT_ITEMS_OFFSET,
    PYTUPLEOBJECT_LEN_OFFSET,
    PY_FLAG_FUNC_AUTO_PARK,
    PY_FLAG_FUNC_CONTINUATION_FACTORY,
    PY_FLAG_FUNC_TRANSPARENT_CALL,
    PY_TYPE_DICT,
    PY_TYPE_FUNC,
    PY_TYPE_INT,
    PY_TYPE_NONE,
    PY_TYPE_STR,
    PY_TYPE_TUPLE,
)

from pcc.extern import extern, c_abi_export, c_int32, c_int64, c_ptr, c_void
from pcc.unsafe import (
    atomic_cas_i64,
    atomic_load_i64,
    atomic_rmw_i32,
    call_ptr2,
    define_global_i32,
    define_global_i64,
    define_global_ptr_null,
    define_thread_local_i32,
    global_addr,
    memset,
    stack_alloc,
    store_i32,
    store_i64,
    cstr,
    global_load_ptr,
    global_store_ptr,
    ptr_add,
    is_tagged_int,
    int_to_ptr,
    load_i8,
    load_i32,
    load_i64,
    load_ptr,
    null,
    ptr_eq,
    ptr_is_null,
    ptr_to_int,
    store_ptr,
    strlen,
)

py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_get = extern("py_tuple_get", (c_ptr, c_int64), c_ptr)
py_tuple_len = extern("py_tuple_len", (c_ptr,), c_int64)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_dict_new = extern("py_dict_new", (), c_ptr)
py_dict_set = extern("py_dict_set", (c_ptr, c_ptr, c_ptr), c_void)
py_dict_contains = extern("py_dict_contains", (c_ptr, c_ptr), c_int64)
py_dict_del = extern("py_dict_del", (c_ptr, c_ptr), c_int64)
py_dict_get = extern("py_dict_get", (c_ptr, c_ptr), c_ptr)
py_dict_len = extern("py_dict_len", (c_ptr,), c_int64)
py_call_merge_kwargs = extern("py_call_merge_kwargs", (c_ptr, c_ptr), c_ptr)
py_call_validate_kwargs = extern("py_call_validate_kwargs", (c_ptr,), c_int64)
py_obj_call = extern("py_obj_call", (c_ptr, c_ptr, c_ptr), c_ptr)
py_obj_call_default = extern("py_obj_call_default", (c_ptr, c_ptr, c_ptr), c_ptr)
py_obj_call_slots = extern("py_obj_call_slots", (c_ptr, c_ptr, c_ptr, c_ptr), c_int64)
py_obj_getattr = extern("py_obj_getattr", (c_ptr, c_ptr), c_ptr)
py_obj_truthy = extern("py_obj_truthy", (c_ptr,), c_int64)
py_obj_abs = extern("py_obj_abs", (c_ptr,), c_ptr)
py_obj_setattr = extern("py_obj_setattr", (c_ptr, c_ptr, c_ptr), c_int64)
py_dict_update = extern("py_dict_update", (c_ptr, c_ptr), c_void)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_int_value_i64 = extern("py_int_value_i64", (c_ptr,), c_int64)
py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_str_eq = extern("py_str_eq", (c_ptr, c_ptr), c_int64)
py_str_concat = extern("py_str_concat", (c_ptr, c_ptr), c_ptr)
py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)
py_int_to_str_obj = extern("py_int_to_str_obj", (c_ptr,), c_ptr)
py_dict_keys = extern("py_dict_keys", (c_ptr,), c_ptr)
py_list_len = extern("py_list_len", (c_ptr,), c_int64)
py_list_get = extern("py_list_get", (c_ptr, c_int64), c_ptr)
py_list_new = extern("py_list_new", (c_int64,), c_ptr)
py_list_append = extern("py_list_append", (c_ptr, c_ptr), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_raise = extern("py_raise", (c_ptr,), c_void)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_runtime_error_if_unset = extern(
    "py_runtime_error_if_unset", (c_ptr, c_ptr), c_ptr
)
py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_gc_track = extern("py_gc_track", (c_ptr,), c_void)
pcc_gc_publish_initialized = extern(
    "pcc_gc_publish_initialized", (c_ptr,), c_void
)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_store_ptr = extern("pcc_gc_store_ptr", (c_ptr, c_ptr, c_ptr), c_void)
pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_ptr)
pcc_gc_free_object_memory = extern("pcc_gc_free_object_memory", (c_ptr,), c_void)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
pcc_gc_unpin = extern("pcc_gc_unpin", (c_ptr,), c_void)
pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
pcc_gc_frame_leave = extern("pcc_gc_frame_leave", (c_ptr,), c_void)
pcc_gc_store_root = extern("pcc_gc_store_root", (c_ptr, c_ptr), c_void)
pcc_gc_note_write_barrier = extern("pcc_gc_note_write_barrier", (c_ptr, c_ptr), c_void)
pcc_gc_take_pinned_slot = extern("pcc_gc_take_pinned_slot", (c_ptr, c_int64), c_ptr)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
pcc_gc_scheduler_root_register_handle = extern("pcc_gc_scheduler_root_register_handle", (c_ptr,), c_ptr)
pcc_gc_scheduler_root_unregister_handle = extern("pcc_gc_scheduler_root_unregister_handle", (c_ptr,), c_void)
pcc_gc_foreign_lease_acquire = extern("pcc_gc_foreign_lease_acquire", (c_ptr,), c_int64)
pcc_gc_foreign_lease_release = extern("pcc_gc_foreign_lease_release", (c_ptr, c_int64), c_int64)
pcc_gc_root_copy_lease = extern("pcc_gc_root_copy_lease", (c_ptr, c_ptr), c_int64)
pcc_gc_root_copy_lease_prepare_locked = extern("pcc_gc_root_copy_lease_prepare_locked", (c_ptr, c_ptr, c_int64, c_ptr), c_int64)
pcc_gc_root_copy_lease_finish = extern("pcc_gc_root_copy_lease_finish", (c_ptr,), c_void)
pcc_gc_resolve_root_slot_unlocked = extern("pcc_gc_resolve_root_slot_unlocked", (c_ptr, c_int64), c_ptr)
pcc_gc_note_slot_write_barrier = extern("pcc_gc_note_slot_write_barrier", (c_ptr, c_ptr, c_ptr), c_void)
py_tls_exc_swap_slot = extern("py_tls_exc_swap_slot", (c_ptr,), c_void)
pcc_platform_abort = extern("pcc_platform_abort", (), c_void)
pcc_mutex_new = extern("pcc_mutex_new", (), c_ptr)
pcc_mutex_free = extern("pcc_mutex_free", (c_ptr,), c_void)
pcc_mutex_lock = extern("pcc_mutex_lock", (c_ptr,), c_int64)
pcc_mutex_unlock = extern("pcc_mutex_unlock", (c_ptr,), c_int64)
py_gen_run_may_park_sync = extern("py_gen_run_may_park_sync", (c_ptr,), c_ptr)
py_current_exception = extern("py_current_exception", (), c_ptr)
py_bound_method_function = extern("py_bound_method_function", (c_ptr,), c_ptr)

# Private frame layout for callable metadata initialization.
_FUNC_METADATA_FUNCTION_SLOT = 0
_FUNC_METADATA_VALUE_SLOT = 1
_FUNC_METADATA_ERROR_SLOT = 2
_FUNC_METADATA_SLOT_COUNT = 3


def _func_metadata_close(
    slots: c_ptr, tokens: c_ptr, handles: c_ptr, registered: int,
) -> None:
    if registered == _FUNC_METADATA_SLOT_COUNT:
        # Save the actual TLS owner before cleanup can invoke a finalizer.
        py_tls_exc_swap_slot(ptr_add(slots, _FUNC_METADATA_ERROR_SLOT * C_POINTER_SIZE))
        index: int = _FUNC_METADATA_VALUE_SLOT
        while index >= _FUNC_METADATA_FUNCTION_SLOT:
            offset: int = index * C_POINTER_SIZE
            slot = ptr_add(slots, offset)
            if pcc_gc_foreign_lease_release(slot, load_i64(tokens, offset)) != 0:
                pcc_platform_abort()
                return
            store_i64(tokens, offset, 0)
            pcc_gc_store_root(slot, null())
            index = index - 1
        py_clear_exception()
        py_tls_exc_swap_slot(ptr_add(slots, _FUNC_METADATA_ERROR_SLOT * C_POINTER_SIZE))
    index = registered - 1
    while index >= 0:
        pcc_gc_scheduler_root_unregister_handle(
            load_ptr(handles, index * C_POINTER_SIZE)
        )
        index = index - 1


def _func_metadata_set_text(
    slots: c_ptr, tokens: c_ptr, attribute: c_ptr, text: c_ptr,
) -> int:
    value_slot = ptr_add(slots, _FUNC_METADATA_VALUE_SLOT * C_POINTER_SIZE)
    # Publish NEW before lease acquisition, error checks or other callbacks.
    store_ptr(value_slot, 0, py_str_new(text, strlen(text)))
    token: int = pcc_gc_foreign_lease_acquire(value_slot)
    if token < 0:
        py_runtime_error_if_unset(
            cstr("function metadata"), cstr("metadata value lease failed")
        )
        return -1
    store_i64(tokens, _FUNC_METADATA_VALUE_SLOT * C_POINTER_SIZE, token)
    pcc_py_gc_minor_graph_lock()
    pcc_gc_note_slot_write_barrier(null(), value_slot, load_ptr(value_slot, 0))
    pcc_py_gc_minor_graph_unlock()
    if py_err_occurred() != 0 or ptr_is_null(load_ptr(value_slot, 0)) != 0:
        py_runtime_error_if_unset(
            cstr("function metadata"), cstr("metadata string allocation failed")
        )
        return -1
    status: int = py_obj_setattr(
        load_ptr(slots, _FUNC_METADATA_FUNCTION_SLOT * C_POINTER_SIZE), attribute,
        load_ptr(slots, _FUNC_METADATA_VALUE_SLOT * C_POINTER_SIZE),
    )
    if status != 0 or py_err_occurred() != 0:
        py_runtime_error_if_unset(
            cstr("function metadata"), cstr("metadata attribute assignment failed")
        )
        return -1
    if pcc_gc_foreign_lease_release(value_slot, token) != 0:
        pcc_platform_abort()
        return -1
    store_i64(tokens, _FUNC_METADATA_VALUE_SLOT * C_POINTER_SIZE, 0)
    pcc_gc_store_root(value_slot, null())
    return -1 if py_err_occurred() != 0 else 0


@c_abi_export("py_func_init_metadata_slots")
def py_func_init_metadata_slots(
    callable_slot: c_ptr, module_name: c_ptr, qualname: c_ptr,
) -> int:
    """Borrow an authoritative callable slot; initialize namespace metadata."""
    if py_err_occurred() != 0:
        return -1
    if ptr_is_null(callable_slot) != 0 or ptr_is_null(module_name) != 0:
        py_runtime_error_if_unset(
            cstr("function metadata"), cstr("invalid metadata source")
        )
        return -1
    slots = stack_alloc(_FUNC_METADATA_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_FUNC_METADATA_SLOT_COUNT * C_POINTER_SIZE)
    handles = stack_alloc(_FUNC_METADATA_SLOT_COUNT * C_POINTER_SIZE)
    memset(slots, 0, _FUNC_METADATA_SLOT_COUNT * C_POINTER_SIZE)
    memset(tokens, 0, _FUNC_METADATA_SLOT_COUNT * C_POINTER_SIZE)
    memset(handles, 0, _FUNC_METADATA_SLOT_COUNT * C_POINTER_SIZE)
    registered: int = 0
    while registered < _FUNC_METADATA_SLOT_COUNT:
        slot = ptr_add(slots, registered * C_POINTER_SIZE)
        handle = pcc_gc_scheduler_root_register_handle(slot)
        if ptr_is_null(handle) != 0:
            break
        store_ptr(handles, registered * C_POINTER_SIZE, handle)
        registered = registered + 1
    if registered != _FUNC_METADATA_SLOT_COUNT:
        py_runtime_error_if_unset(
            cstr("function metadata"), cstr("metadata root registration failed")
        )
        _func_metadata_close(slots, tokens, handles, registered)
        return -1
    token: int = pcc_gc_root_copy_lease(
        ptr_add(slots, _FUNC_METADATA_FUNCTION_SLOT * C_POINTER_SIZE), callable_slot
    )
    status: int = -1
    if token < 0:
        py_runtime_error_if_unset(
            cstr("function metadata"), cstr("callable owner transfer failed")
        )
    else:
        store_i64(tokens, _FUNC_METADATA_FUNCTION_SLOT * C_POINTER_SIZE, token)
        function = load_ptr(slots, _FUNC_METADATA_FUNCTION_SLOT * C_POINTER_SIZE)
        if ptr_is_null(function) != 0 or is_tagged_int(function) != 0:
            py_runtime_error_if_unset(
                cstr("function metadata"), cstr("invalid callable metadata owner")
            )
        elif load_i32(function, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_FUNC:
            py_runtime_error_if_unset(
                cstr("function metadata"), cstr("metadata owner is not a function")
            )
        else:
            status = _func_metadata_set_text(slots, tokens, cstr("__module__"), module_name)
            if status == 0 and ptr_is_null(qualname) == 0:
                status = _func_metadata_set_text(slots, tokens, cstr("__qualname__"), qualname)
    _func_metadata_close(slots, tokens, handles, registered)
    return status


py_func_new_named_raw = extern("py_func_new_named", (c_ptr, c_ptr, c_ptr), c_ptr)
py_func_init_metadata_slots_raw = extern("py_func_init_metadata_slots", (c_ptr, c_ptr, c_ptr), c_int64)

# Fixed construction frame; expression evaluation remains in the caller.
_FUNC_CONSTRUCTION_CAPTURES_SLOT = 0
_FUNC_CONSTRUCTION_SIGNATURE_SLOT = 1
_FUNC_CONSTRUCTION_WRAPPER_SLOT = 2
_FUNC_CONSTRUCTION_ERROR_SLOT = 3
_FUNC_CONSTRUCTION_SLOT_COUNT = 4


def _func_construction_close(
    slots: c_ptr, tokens: c_ptr, handles: c_ptr, registered: int,
) -> None:
    if registered == _FUNC_CONSTRUCTION_SLOT_COUNT:
        py_tls_exc_swap_slot(ptr_add(slots, _FUNC_CONSTRUCTION_ERROR_SLOT * C_POINTER_SIZE))
        index: int = _FUNC_CONSTRUCTION_WRAPPER_SLOT
        while index >= _FUNC_CONSTRUCTION_CAPTURES_SLOT:
            offset: int = index * C_POINTER_SIZE
            slot = ptr_add(slots, offset)
            if pcc_gc_foreign_lease_release(slot, load_i64(tokens, offset)) != 0:
                pcc_platform_abort()
                return
            store_i64(tokens, offset, 0)
            pcc_gc_store_root(slot, null())
            index = index - 1
        py_clear_exception()
        py_tls_exc_swap_slot(ptr_add(slots, _FUNC_CONSTRUCTION_ERROR_SLOT * C_POINTER_SIZE))
    index = registered - 1
    while index >= 0:
        pcc_gc_scheduler_root_unregister_handle(load_ptr(handles, index * C_POINTER_SIZE))
        index = index - 1


def _func_construction_lease_new(slot: c_ptr) -> int:
    # The producer must have stored NEW directly into this registered empty
    # slot, with no intervening call. Return the live token even if TLS is set.
    token: int = pcc_gc_foreign_lease_acquire(slot)
    if token < 0:
        py_runtime_error_if_unset(cstr("function construction"), cstr("result lease failed"))
        return token
    pcc_py_gc_minor_graph_lock()
    pcc_gc_note_slot_write_barrier(null(), slot, load_ptr(slot, 0))
    pcc_py_gc_minor_graph_unlock()
    if ptr_is_null(load_ptr(slot, 0)) != 0:
        py_runtime_error_if_unset(cstr("function construction"), cstr("allocation failed"))
    return token


@c_abi_export("py_func_new_signature_slots")
def py_func_new_signature_slots(
    captures_slot: c_ptr, signature_slot: c_ptr, entry: c_ptr,
    name: c_ptr, output_slot: c_ptr,
) -> int:
    """Borrow evaluated roots; construct exactly [captures, signature].

    entry/name are static unmanaged ABI pointers. output_slot must be a
    distinct, registered EMPTY owning root. Success publishes one NEW owner.
    On failure it may still own a non-null result; the caller MUST clean it
    up, even when status is negative. Input owners are never retired here.
    NEW publication precedes every poll, lease, diagnostic and cleanup.
    """
    if py_err_occurred() != 0:
        return -1
    if ptr_is_null(captures_slot) != 0 or ptr_is_null(signature_slot) != 0 or ptr_is_null(output_slot) != 0:
        py_runtime_error_if_unset(cstr("function construction"), cstr("invalid source slot"))
        return -1
    if ptr_eq(output_slot, captures_slot) != 0 or ptr_eq(output_slot, signature_slot) != 0 or ptr_is_null(load_ptr(output_slot, 0)) == 0:
        py_runtime_error_if_unset(cstr("function construction"), cstr("output slot must be empty and distinct"))
        return -1
    slots = stack_alloc(_FUNC_CONSTRUCTION_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_FUNC_CONSTRUCTION_SLOT_COUNT * C_POINTER_SIZE)
    handles = stack_alloc(_FUNC_CONSTRUCTION_SLOT_COUNT * C_POINTER_SIZE)
    memset(slots, 0, _FUNC_CONSTRUCTION_SLOT_COUNT * C_POINTER_SIZE)
    memset(tokens, 0, _FUNC_CONSTRUCTION_SLOT_COUNT * C_POINTER_SIZE)
    memset(handles, 0, _FUNC_CONSTRUCTION_SLOT_COUNT * C_POINTER_SIZE)
    registered: int = 0
    while registered < _FUNC_CONSTRUCTION_SLOT_COUNT:
        handle = pcc_gc_scheduler_root_register_handle(ptr_add(slots, registered * C_POINTER_SIZE))
        if ptr_is_null(handle) != 0:
            break
        store_ptr(handles, registered * C_POINTER_SIZE, handle)
        registered = registered + 1
    if registered != _FUNC_CONSTRUCTION_SLOT_COUNT:
        py_runtime_error_if_unset(cstr("function construction"), cstr("root registration failed"))
        _func_construction_close(slots, tokens, handles, registered)
        return -1
    captures = ptr_add(slots, _FUNC_CONSTRUCTION_CAPTURES_SLOT * C_POINTER_SIZE)
    signature = ptr_add(slots, _FUNC_CONSTRUCTION_SIGNATURE_SLOT * C_POINTER_SIZE)
    wrapper = ptr_add(slots, _FUNC_CONSTRUCTION_WRAPPER_SLOT * C_POINTER_SIZE)
    token: int = pcc_gc_root_copy_lease(captures, captures_slot)
    if token >= 0:
        store_i64(tokens, _FUNC_CONSTRUCTION_CAPTURES_SLOT * C_POINTER_SIZE, token)
        token = pcc_gc_root_copy_lease(signature, signature_slot)
        if token >= 0:
            store_i64(tokens, _FUNC_CONSTRUCTION_SIGNATURE_SLOT * C_POINTER_SIZE, token)
    if token < 0:
        py_runtime_error_if_unset(cstr("function construction"), cstr("source owner copy failed"))
        _func_construction_close(slots, tokens, handles, registered)
        return -1
    store_ptr(wrapper, 0, py_tuple_new(2))
    token = _func_construction_lease_new(wrapper)
    if token >= 0:
        store_i64(tokens, _FUNC_CONSTRUCTION_WRAPPER_SLOT * C_POINTER_SIZE, token)
    status: int = -1
    if token >= 0 and py_err_occurred() == 0:
        py_tuple_set_item(load_ptr(wrapper, 0), 0, load_ptr(captures, 0))
        if py_err_occurred() == 0:
            py_tuple_set_item(load_ptr(wrapper, 0), 1, load_ptr(signature, 0))
            if py_err_occurred() == 0:
                store_ptr(output_slot, 0, py_func_new_named_raw(entry, load_ptr(wrapper, 0), name))
                token = _func_construction_lease_new(output_slot)
                if token >= 0:
                    if pcc_gc_foreign_lease_release(output_slot, token) != 0:
                        pcc_platform_abort()
                        return -1
                    if py_err_occurred() == 0:
                        status = 0
    _func_construction_close(slots, tokens, handles, registered)
    return status


py_static_aggregate_build = extern("py_static_aggregate_build", (c_ptr,), c_ptr)


@c_abi_export("py_func_new_static_def")
def py_func_new_static_def(
    entry: c_ptr, name: c_ptr, signature_desc: c_ptr, module_name: c_ptr,
    qualname: c_ptr, doc: c_ptr, role: int, cache: c_ptr, output_slot: c_ptr,
) -> int:
    """Construct a capture-free callable from static data in one call.

    The same steps, roots and order the general inline route emits around
    ``py_func_new_signature_slots``: an empty captures tuple, the signature
    built from ``signature_desc`` (a static aggregate descriptor), the
    callable, its ``__module__``/``__qualname__`` metadata, then ``__doc__``,
    the compiler role flag and the definition cache under one publication
    lease. ``doc`` is null or an immortal static str; ``role`` is 0,
    PY_FLAG_FUNC_AUTO_PARK, PY_FLAG_FUNC_CONTINUATION_FACTORY or
    PY_FLAG_FUNC_TRANSPARENT_CALL; ``cache`` is
    null or the definition-cache global. output_slot follows
    ``py_func_new_signature_slots``: a distinct, registered EMPTY owning
    root; on failure it may own a result the caller must clean up.
    """
    if py_err_occurred() != 0:
        return -1
    if ptr_is_null(signature_desc) != 0 or ptr_is_null(output_slot) != 0 or ptr_is_null(load_ptr(output_slot, 0)) == 0:
        py_runtime_error_if_unset(cstr("function construction"), cstr("invalid static definition"))
        return -1
    if role != 0 and role != 8388608 and role != 67108864 and role != 16777216:
        py_runtime_error_if_unset(cstr("function construction"), cstr("invalid static definition"))
        return -1
    slots = stack_alloc(_FUNC_CONSTRUCTION_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_FUNC_CONSTRUCTION_SLOT_COUNT * C_POINTER_SIZE)
    handles = stack_alloc(_FUNC_CONSTRUCTION_SLOT_COUNT * C_POINTER_SIZE)
    memset(slots, 0, _FUNC_CONSTRUCTION_SLOT_COUNT * C_POINTER_SIZE)
    memset(tokens, 0, _FUNC_CONSTRUCTION_SLOT_COUNT * C_POINTER_SIZE)
    memset(handles, 0, _FUNC_CONSTRUCTION_SLOT_COUNT * C_POINTER_SIZE)
    registered: int = 0
    while registered < _FUNC_CONSTRUCTION_SLOT_COUNT:
        handle = pcc_gc_scheduler_root_register_handle(ptr_add(slots, registered * C_POINTER_SIZE))
        if ptr_is_null(handle) != 0:
            break
        store_ptr(handles, registered * C_POINTER_SIZE, handle)
        registered = registered + 1
    if registered != _FUNC_CONSTRUCTION_SLOT_COUNT:
        py_runtime_error_if_unset(cstr("function construction"), cstr("root registration failed"))
        _func_construction_close(slots, tokens, handles, registered)
        return -1
    captures = ptr_add(slots, _FUNC_CONSTRUCTION_CAPTURES_SLOT * C_POINTER_SIZE)
    signature = ptr_add(slots, _FUNC_CONSTRUCTION_SIGNATURE_SLOT * C_POINTER_SIZE)
    status: int = -1
    store_ptr(captures, 0, py_tuple_new(0))
    token: int = _func_construction_lease_new(captures)
    if token >= 0:
        store_i64(tokens, _FUNC_CONSTRUCTION_CAPTURES_SLOT * C_POINTER_SIZE, token)
        if py_err_occurred() == 0:
            store_ptr(signature, 0, py_static_aggregate_build(signature_desc))
            token = _func_construction_lease_new(signature)
            if token >= 0:
                store_i64(tokens, _FUNC_CONSTRUCTION_SIGNATURE_SLOT * C_POINTER_SIZE, token)
                if py_err_occurred() == 0:
                    status = py_func_new_signature_slots(captures, signature, entry, name, output_slot)
    _func_construction_close(slots, tokens, handles, registered)
    if status != 0 or py_err_occurred() != 0:
        return -1
    if py_func_init_metadata_slots(output_slot, module_name, qualname) != 0 or py_err_occurred() != 0:
        return -1
    if ptr_is_null(doc) == 0:
        token = pcc_gc_foreign_lease_acquire(output_slot)
        if token < 0:
            py_runtime_error_if_unset(cstr("function construction"), cstr("docstring lease failed"))
            return -1
        status = py_obj_setattr(load_ptr(output_slot, 0), cstr("__doc__"), doc)
        if pcc_gc_foreign_lease_release(output_slot, token) != 0:
            pcc_platform_abort()
            return -1
        if status != 0 or py_err_occurred() != 0:
            return -1
    token = pcc_gc_foreign_lease_acquire(output_slot)
    if token < 0:
        py_runtime_error_if_unset(cstr("function construction"), cstr("callable publication lease failed"))
        return -1
    current = load_ptr(output_slot, 0)
    # The role write inserts no safepoint; literal flags as at every other
    # atomic flag update in this port.
    if role == 8388608:
        atomic_rmw_i32("or", current, 12, 8388608, "relaxed")
    if role == 67108864:
        atomic_rmw_i32("or", current, 12, 67108864, "relaxed")
    if role == 16777216:
        atomic_rmw_i32("or", current, 12, 16777216, "relaxed")
    if ptr_is_null(cache) == 0:
        py_incref(current)
        pcc_gc_pin(current)
        store_ptr(cache, 0, current)
    if pcc_gc_foreign_lease_release(output_slot, token) != 0:
        py_runtime_error_if_unset(cstr("function construction"), cstr("callable publication release failed"))
        return -1
    return 0


# A deferred request is consumed by the actual semantic callee. Ordinary
# function bodies run with synchronous dynamic-call semantics. Transparent
# bind/partial entries forward only their target call through the second lane.
define_thread_local_i32("pcc_native_callable_sync_context", 1)
define_thread_local_i32("pcc_native_callable_forward_context", 1)
define_global_i32("pcc_native_callable_result_frame_map", 11)
define_global_ptr_null("pcc_builtin_function_cache")
define_global_i64("pcc_builtin_function_cache_mutex_bits", 0)
define_global_i32("pcc_builtin_function_cache_registered", 0)

py_class_new = extern(
    "py_class_new",
    (c_ptr, c_ptr, c_int32, c_ptr, c_int32),
    c_ptr,
)
py_instance_new = extern("py_instance_new", (c_ptr,), c_ptr)
py_func_new_bound_raw = extern(
    "py_func_new_bound",
    (c_ptr, c_ptr, c_ptr, c_ptr),
    c_ptr,
)


def _checked_func(obj):
    if ptr_is_null(obj):
        return null()
    if is_tagged_int(obj):
        return null()
    if load_i32(obj, 8) != PY_TYPE_FUNC:
        return null()
    return obj


def _is_none_or_null(obj) -> int:
    if ptr_is_null(obj):
        return 1
    if is_tagged_int(obj):
        return 0
    if load_i32(obj, 8) == PY_TYPE_NONE:
        return 1
    return 0


def _is_tuple(obj) -> int:
    if ptr_is_null(obj):
        return 0
    if is_tagged_int(obj):
        return 0
    if load_i32(obj, 8) == PY_TYPE_TUPLE:
        return 1
    return 0


def _is_dict(obj) -> int:
    if ptr_is_null(obj):
        return 0
    if is_tagged_int(obj):
        return 0
    if load_i32(obj, 8) == PY_TYPE_DICT:
        return 1
    return 0


def _func_type_error(message):
    exc = py_exc_new(3, message)
    py_raise(exc)
    if ptr_is_null(exc) == 0:
        py_decref(exc)
    return null()


def _func_runtime_error_if_unset(helper_name, message):
    if py_err_occurred() != 0:
        return null()
    return py_runtime_error_if_unset(helper_name, message)


def _signature_runtime_error_if_unset(fn, message):
    if py_err_occurred() != 0:
        return null()
    context = cstr("py_func_bind_signature")
    if ptr_is_null(fn) == 0 and is_tagged_int(fn) == 0:
        if load_i32(fn, 8) == PY_TYPE_FUNC:
            stored = load_ptr(fn, 72)
            if ptr_is_null(stored) == 0 and load_i8(stored, 0) != 0:
                context = stored
    return _func_runtime_error_if_unset(context, message)


def _kwargs_empty(kwargs) -> int:
    if _is_none_or_null(kwargs) != 0:
        return 1
    if _is_dict(kwargs) == 0:
        return 0
    if py_dict_len(kwargs) == 0:
        return 1
    return 0


def _signature_valid(sig) -> int:
    if _is_tuple(sig) == 0:
        return 0
    sig_len: int = py_tuple_len(sig)
    if sig_len < 5:
        return 0
    # Every native call checks this.  ``sig`` is a tuple of at least five
    # items, so its first slot is read borrowed, and the 25-byte spelling
    # "__pcc_func_signature_v1__" (the str payload starts at offset 40) is
    # compared as three aligned words and a final byte rather than 25 bytes.
    magic = _tuple_borrow_known(sig, 0)
    if ptr_is_null(magic):
        return 0
    if is_tagged_int(magic) != 0:
        return 0
    if load_i32(magic, 8) != PY_TYPE_STR:
        return 0
    if load_i64(magic, 16) != 25:
        return 0
    if load_i64(magic, 40) != 0x75665F6363705F5F:  # "__pcc_fu"
        return 0
    if load_i64(magic, 48) != 0x616E6769735F636E:  # "nc_signa"
        return 0
    if load_i64(magic, 56) != 0x5F31765F65727574:  # "ture_v1_"
        return 0
    if load_i8(magic, 64) != 95:  # "_"
        return 0
    return 1


def _code_set_owned_attr(code, name, value) -> int:
    if ptr_is_null(value):
        return -1
    rc: int = py_obj_setattr(code, name, value)
    py_decref(value)
    return rc


@c_abi_export("py_func_attach_code_metadata")
def py_func_attach_code_metadata(func, signature, name) -> int:
    if ptr_is_null(func) or is_tagged_int(func):
        return -1
    if load_i32(func, 8) != PY_TYPE_FUNC or _signature_valid(signature) == 0:
        return -1

    names = py_tuple_get(signature, 1)
    kinds = py_tuple_get(signature, 2)
    if ptr_is_null(names) or ptr_is_null(kinds):
        if ptr_is_null(names) == 0:
            py_decref(names)
        if ptr_is_null(kinds) == 0:
            py_decref(kinds)
        return -1
    count: int = py_tuple_len(names)
    if py_tuple_len(kinds) != count:
        py_decref(names)
        py_decref(kinds)
        return -1

    argcount: int = 0
    posonlyargcount: int = 0
    kwonlyargcount: int = 0
    flags: int = 0
    i: int = 0
    while i < count:
        kind_obj = py_tuple_get(kinds, i)
        if ptr_is_null(kind_obj):
            py_decref(names)
            py_decref(kinds)
            return -1
        kind: int = py_int_value_i64(kind_obj)
        py_decref(kind_obj)
        if kind == 0 or kind == 1:
            argcount += 1
        if kind == 1:
            posonlyargcount += 1
        if kind == 2:
            kwonlyargcount += 1
        if kind == 3:
            flags = flags | 4
        if kind == 4:
            flags = flags | 8
        i += 1

    varnames = py_tuple_new(count)
    if ptr_is_null(varnames):
        py_decref(names)
        py_decref(kinds)
        return -1
    out_index: int = 0
    group: int = 0
    while group < 4:
        i = 0
        while i < count:
            kind_obj = py_tuple_get(kinds, i)
            if ptr_is_null(kind_obj):
                py_decref(varnames)
                py_decref(names)
                py_decref(kinds)
                return -1
            kind = py_int_value_i64(kind_obj)
            py_decref(kind_obj)
            matches: int = 0
            if group == 0 and (kind == 0 or kind == 1):
                matches = 1
            elif group == 1 and kind == 2:
                matches = 1
            elif group == 2 and kind == 3:
                matches = 1
            elif group == 3 and kind == 4:
                matches = 1
            if matches != 0:
                arg_name = py_tuple_get(names, i)
                if ptr_is_null(arg_name):
                    py_decref(varnames)
                    py_decref(names)
                    py_decref(kinds)
                    return -1
                py_tuple_set_item(varnames, out_index, arg_name)
                py_decref(arg_name)
                out_index += 1
            i += 1
        group += 1

    code_class = global_load_ptr("py_func_code_class_cache")
    if ptr_is_null(code_class):
        code_class = py_class_new(cstr("code"), null(), 0, null(), 0)
        if ptr_is_null(code_class):
            py_decref(varnames)
            py_decref(names)
            py_decref(kinds)
            return -1
        pcc_gc_pin(code_class)
        global_store_ptr("py_func_code_class_cache", code_class)
    code = py_instance_new(code_class)
    if ptr_is_null(code):
        py_decref(varnames)
        py_decref(names)
        py_decref(kinds)
        return -1

    if _code_set_owned_attr(code, cstr("co_argcount"), py_int_from_i64(argcount)) != 0:
        py_decref(varnames)
        py_decref(code)
        py_decref(names)
        py_decref(kinds)
        return -1
    if (
        _code_set_owned_attr(
            code, cstr("co_posonlyargcount"), py_int_from_i64(posonlyargcount)
        )
        != 0
    ):
        py_decref(varnames)
        py_decref(code)
        py_decref(names)
        py_decref(kinds)
        return -1
    if (
        _code_set_owned_attr(
            code, cstr("co_kwonlyargcount"), py_int_from_i64(kwonlyargcount)
        )
        != 0
    ):
        py_decref(varnames)
        py_decref(code)
        py_decref(names)
        py_decref(kinds)
        return -1
    if _code_set_owned_attr(code, cstr("co_flags"), py_int_from_i64(flags)) != 0:
        py_decref(varnames)
        py_decref(code)
        py_decref(names)
        py_decref(kinds)
        return -1
    if _code_set_owned_attr(code, cstr("co_varnames"), varnames) != 0:
        py_decref(code)
        py_decref(names)
        py_decref(kinds)
        return -1
    name_value = py_str_new(name, strlen(name))
    if _code_set_owned_attr(code, cstr("co_name"), name_value) != 0:
        py_decref(code)
        py_decref(names)
        py_decref(kinds)
        return -1
    rc: int = py_obj_setattr(func, cstr("__code__"), code)
    py_decref(code)
    py_decref(names)
    py_decref(kinds)
    return rc


def _signature_from_captures(captures, out_actual_slot):
    store_ptr(out_actual_slot, 0, captures)
    if _is_tuple(captures) == 0:
        return null()
    captures_len: int = py_tuple_len(captures)
    if captures_len != 2:
        return null()
    candidate = py_tuple_get(captures, 1)
    if _signature_valid(candidate) == 0:
        if ptr_is_null(candidate) == 0:
            py_decref(candidate)
        return null()
    inner = py_tuple_get(captures, 0)
    if ptr_is_null(inner):
        py_decref(candidate)
        return null()
    store_ptr(out_actual_slot, 0, inner)
    return candidate


@c_abi_export("py_func_get_code_metadata")
def py_func_get_code_metadata(func):
    if ptr_is_null(func) or is_tagged_int(func):
        return null()
    if load_i32(func, 8) != PY_TYPE_FUNC:
        return null()
    captures = pcc_gc_load_ptr(func, ptr_add(func, 64))
    if _is_tuple(captures) == 0 or py_tuple_len(captures) != 2:
        return null()
    signature = py_tuple_get(captures, 1)
    if _signature_valid(signature) == 0:
        if ptr_is_null(signature) == 0:
            py_decref(signature)
        return null()
    name = load_ptr(func, 72)
    rc: int = py_func_attach_code_metadata(func, signature, name)
    py_decref(signature)
    if rc != 0:
        return null()
    attrs = pcc_gc_load_ptr(func, ptr_add(func, 88))
    if ptr_is_null(attrs):
        return null()
    key = py_str_new(cstr("__code__"), 8)
    if ptr_is_null(key):
        return null()
    code = py_dict_get(attrs, key)
    py_decref(key)
    return code


@c_abi_export("py_func_get_defaults_metadata")
def py_func_get_defaults_metadata(func):
    if ptr_is_null(func) or is_tagged_int(func):
        return null()
    if load_i32(func, 8) != PY_TYPE_FUNC:
        return null()
    captures = pcc_gc_load_ptr(func, ptr_add(func, 64))
    if _is_tuple(captures) == 0 or py_tuple_len(captures) != 2:
        return null()
    signature = py_tuple_get(captures, 1)
    if _signature_valid(signature) == 0:
        if ptr_is_null(signature) == 0:
            py_decref(signature)
        return null()

    kinds = py_tuple_get(signature, 2)
    has_defaults = py_tuple_get(signature, 3)
    defaults = py_tuple_get(signature, 4)
    if (
        ptr_is_null(kinds)
        or ptr_is_null(has_defaults)
        or ptr_is_null(defaults)
    ):
        if ptr_is_null(kinds) == 0:
            py_decref(kinds)
        if ptr_is_null(has_defaults) == 0:
            py_decref(has_defaults)
        if ptr_is_null(defaults) == 0:
            py_decref(defaults)
        py_decref(signature)
        return null()
    count: int = py_tuple_len(kinds)
    if py_tuple_len(has_defaults) != count or py_tuple_len(defaults) != count:
        py_decref(kinds)
        py_decref(has_defaults)
        py_decref(defaults)
        py_decref(signature)
        return null()

    default_count: int = 0
    i: int = 0
    while i < count:
        kind_obj = py_tuple_get(kinds, i)
        has_default_obj = py_tuple_get(has_defaults, i)
        if ptr_is_null(kind_obj) or ptr_is_null(has_default_obj):
            if ptr_is_null(kind_obj) == 0:
                py_decref(kind_obj)
            if ptr_is_null(has_default_obj) == 0:
                py_decref(has_default_obj)
            py_decref(kinds)
            py_decref(has_defaults)
            py_decref(defaults)
            py_decref(signature)
            return null()
        kind: int = py_int_value_i64(kind_obj)
        has_default: int = py_obj_truthy(has_default_obj)
        py_decref(kind_obj)
        py_decref(has_default_obj)
        if (kind == 0 or kind == 1) and has_default != 0:
            default_count += 1
        i += 1

    if default_count == 0:
        out = global_load_ptr("py_None")
        py_incref(out)
    else:
        out = py_tuple_new(default_count)
        if ptr_is_null(out):
            py_decref(kinds)
            py_decref(has_defaults)
            py_decref(defaults)
            py_decref(signature)
            return null()
        out_index: int = 0
        i = 0
        while i < count:
            kind_obj = py_tuple_get(kinds, i)
            has_default_obj = py_tuple_get(has_defaults, i)
            if ptr_is_null(kind_obj) or ptr_is_null(has_default_obj):
                if ptr_is_null(kind_obj) == 0:
                    py_decref(kind_obj)
                if ptr_is_null(has_default_obj) == 0:
                    py_decref(has_default_obj)
                py_decref(out)
                py_decref(kinds)
                py_decref(has_defaults)
                py_decref(defaults)
                py_decref(signature)
                return null()
            kind = py_int_value_i64(kind_obj)
            has_default = py_obj_truthy(has_default_obj)
            py_decref(kind_obj)
            py_decref(has_default_obj)
            if (kind == 0 or kind == 1) and has_default != 0:
                default_obj = py_tuple_get(defaults, i)
                if ptr_is_null(default_obj):
                    py_decref(out)
                    py_decref(kinds)
                    py_decref(has_defaults)
                    py_decref(defaults)
                    py_decref(signature)
                    return null()
                py_tuple_set_item(out, out_index, default_obj)
                py_decref(default_obj)
                out_index += 1
            i += 1

    py_decref(kinds)
    py_decref(has_defaults)
    py_decref(defaults)
    py_decref(signature)
    if py_obj_setattr(func, cstr("__defaults__"), out) != 0:
        py_decref(out)
        return null()
    return out


def _copy_varargs(args, start: int, nargs: int):
    count: int = nargs - start
    if count < 0:
        count = 0
    out = py_tuple_new(count)
    if ptr_is_null(out):
        return null()
    i: int = 0
    while i < count:
        item = py_tuple_get(args, start + i)
        if ptr_is_null(item):
            py_decref(out)
            return null()
        py_tuple_set_item(out, i, item)
        py_decref(item)
        i = i + 1
    return out


def _tuple_borrow_known(t, i: int):
    return pcc_gc_load_ptr(t, ptr_add(t, 24 + i * 8))


def _copy_varargs_known(args, start: int, nargs: int, fn):
    count: int = nargs - start
    if count < 0:
        count = 0
    out = py_tuple_new(count)
    if ptr_is_null(out):
        return _signature_runtime_error_if_unset(
            fn, cstr("native function varargs tuple allocation returned NULL")
        )
    i: int = 0
    while i < count:
        item = _tuple_borrow_known(args, start + i)
        if ptr_is_null(item):
            _signature_runtime_error_if_unset(
                fn, cstr("native function varargs argument entry is NULL")
            )
            py_decref(out)
            return null()
        py_tuple_set_item(out, i, item)
        i = i + 1
    return out


def _sig_cat(acc, piece):
    """Concatenate two owned strs, releasing both; NULL propagates."""
    if ptr_is_null(acc) or ptr_is_null(piece):
        if ptr_is_null(acc) == 0:
            py_decref(acc)
        if ptr_is_null(piece) == 0:
            py_decref(piece)
        return null()
    out = py_str_concat(acc, piece)
    py_decref(acc)
    py_decref(piece)
    return out


def _sig_lit(text):
    return py_str_new(text, strlen(text))


def _sig_int(value: int):
    boxed = py_int_from_i64(value)
    if ptr_is_null(boxed):
        return null()
    out = py_int_to_str_obj(boxed)
    py_decref(boxed)
    return out


def _sig_quoted(name):
    """NEW ``'name'`` for a borrowed str."""
    py_incref(name)
    return _sig_cat(_sig_cat(_sig_lit(cstr("'")), name), _sig_lit(cstr("'")))


def _sig_name_list(names_list):
    """CPython's list: 'a' / 'a' and 'b' / 'a', 'b', and 'c'."""
    n: int = py_list_len(names_list)
    out = _sig_lit(cstr(""))
    i: int = 0
    while i < n:
        if i > 0:
            if n == 2:
                out = _sig_cat(out, _sig_lit(cstr(" and ")))
            elif i == n - 1:
                out = _sig_cat(out, _sig_lit(cstr(", and ")))
            else:
                out = _sig_cat(out, _sig_lit(cstr(", ")))
        item = py_list_get(names_list, i)
        out = _sig_cat(out, _sig_quoted(item))
        py_decref(item)
        i = i + 1
    return out


@c_abi_export("py_func_display_name")
def py_func_display_name(fn):
    """NEW str: ``fn.__qualname__`` when set (nested defs, methods), else its
    ``__name__`` -- what reprs and argument errors print."""
    if ptr_is_null(fn) == 0 and is_tagged_int(fn) == 0 and load_i32(fn, 8) == PY_TYPE_FUNC:
        attrs = pcc_gc_load_ptr(fn, ptr_add(fn, 88))
        if ptr_is_null(attrs) == 0:
            key = py_str_new(cstr("__qualname__"), 12)
            if ptr_is_null(key) == 0:
                value = py_dict_get(attrs, key)
                py_decref(key)
                if ptr_is_null(value) == 0:
                    if is_tagged_int(value) == 0 and load_i32(value, 8) == PY_TYPE_STR:
                        return value
                    py_decref(value)
        stored = load_ptr(fn, 72)
        if ptr_is_null(stored) == 0:
            return py_str_new(stored, strlen(stored))
    return py_str_new(cstr("function"), 8)


def _sig_raise(fn, tail):
    """Raise ``TypeError(f"{qualname}() {tail}")``; ``tail`` is owned."""
    text = _sig_cat(_sig_cat(py_func_display_name(fn), _sig_lit(cstr("() "))), tail)
    if ptr_is_null(text):
        return _func_type_error(cstr("function argument binding failed"))
    exc = py_exc_new(3, py_str_utf8(text))
    py_decref(text)
    py_raise(exc)
    if ptr_is_null(exc) == 0:
        py_decref(exc)
    return null()


def _sig_kind(kinds, index: int) -> int:
    kind_obj = py_tuple_get(kinds, index)
    if ptr_is_null(kind_obj):
        return -1
    kind: int = py_int_value_i64(kind_obj)
    py_decref(kind_obj)
    return kind


def _sig_has_default(has_defaults, index: int) -> int:
    flag = py_tuple_get(has_defaults, index)
    if ptr_is_null(flag):
        return 0
    out: int = py_obj_truthy(flag)
    py_decref(flag)
    return out


def _signature_diagnostic_open(slots, tokens, handles) -> int:
    memset(slots, 0, 24)
    memset(tokens, 0, 24)
    memset(handles, 0, 24)
    count: int = 0
    while count < 3:
        handle = pcc_gc_scheduler_root_register_handle(ptr_add(slots, count * 8))
        if ptr_is_null(handle):
            return count
        store_ptr(handles, count * 8, handle)
        count = count + 1
    return count


def _signature_diagnostic_adopt(slots, tokens) -> int:
    token: int = pcc_gc_foreign_lease_acquire(slots)
    if token < 0:
        return -1
    store_i64(tokens, 0, token)
    pcc_py_gc_minor_graph_lock()
    pcc_gc_note_slot_write_barrier(null(), slots, load_ptr(slots, 0))
    pcc_py_gc_minor_graph_unlock()
    return 0


def _signature_diagnostic_copy_signature(slots, tokens) -> int:
    plan = stack_alloc(256)
    prepared: int = 0
    token: int = 0
    pcc_py_gc_minor_graph_lock()
    original = load_ptr(slots, 0)
    captures = pcc_gc_resolve_root_slot_unlocked(ptr_add(original, 64), 0)
    if _is_tuple(captures) != 0 and load_i64(captures, 16) == 2:
        token = pcc_gc_root_copy_lease_prepare_locked(ptr_add(slots, 8), ptr_add(captures, 32), 0, plan)
        prepared = 1
        if token >= 0:
            store_i64(tokens, 8, token)
    pcc_py_gc_minor_graph_unlock()
    if prepared != 0:
        pcc_gc_root_copy_lease_finish(plan)
    return -1 if token < 0 else 0


def _signature_diagnostic_close(slots, tokens, handles, count: int) -> None:
    if count == 3:
        py_tls_exc_swap_slot(ptr_add(slots, 16))
        index: int = 1
        while index >= 0:
            slot = ptr_add(slots, index * 8)
            if pcc_gc_foreign_lease_release(slot, load_i64(tokens, index * 8)) != 0:
                pcc_platform_abort()
                return
            store_i64(tokens, index * 8, 0)
            pcc_gc_store_root(slot, null())
            index = index - 1
        py_clear_exception()
        py_tls_exc_swap_slot(ptr_add(slots, 16))
    index = 0
    while index < count:
        pcc_gc_scheduler_root_unregister_handle(load_ptr(handles, index * 8))
        index = index + 1


# Declaration snapshots use independent owners for every traversed child.
# The first two slots also match _signature_diagnostic_copy_signature.
_FUNC_DEFAULTS_FUNCTION = 0
_FUNC_DEFAULTS_SIGNATURE = 1
_FUNC_DEFAULTS_VALUES = 2
_FUNC_DEFAULTS_ITEM = 3
_FUNC_DEFAULTS_ERROR = 4
_FUNC_DEFAULTS_COUNT = 5
_FUNC_SNAPSHOT_RELEASE_VALUE = 0
_FUNC_SNAPSHOT_RELEASE_ERROR = 1
_FUNC_SNAPSHOT_RELEASE_COUNT = 2
define_global_i32("pcc_func_snapshot_release_frame_map", 2)


@c_abi_export("py_func_release_default_snapshot")
def py_func_release_default_snapshot(snapshot: c_ptr) -> None:
    """Consume a NEW pinned snapshot owner without an unpin/decref gap.

    The caller transfers its owned reference and explicit legacy pin. The
    pin keeps the input stable until the registered frame owns it; every
    operation after unpinning uses the authoritative slot instead of SSA.
    """
    slots = stack_alloc(_FUNC_SNAPSHOT_RELEASE_COUNT * C_POINTER_SIZE)
    memset(slots, 0, _FUNC_SNAPSHOT_RELEASE_COUNT * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_func_snapshot_release_frame_map"), slots)
    store_ptr(slots, _FUNC_SNAPSHOT_RELEASE_VALUE * C_POINTER_SIZE, snapshot)
    pcc_py_gc_minor_graph_lock()
    pcc_gc_note_slot_write_barrier(null(), slots, load_ptr(slots, 0))
    pcc_py_gc_minor_graph_unlock()
    error_slot = ptr_add(slots, _FUNC_SNAPSHOT_RELEASE_ERROR * C_POINTER_SIZE)
    py_tls_exc_swap_slot(error_slot)
    pcc_gc_unpin(snapshot)
    pcc_gc_store_root(slots, null())
    py_clear_exception()
    py_tls_exc_swap_slot(error_slot)
    pcc_gc_frame_leave(slots)


def _func_defaults_copy_item(source_slot, index: int, result_slot) -> int:
    """Copy one tuple field under the graph lock; return its address lease."""
    plan = stack_alloc(256)
    prepared: int = 0
    token: int = -1
    pcc_py_gc_minor_graph_lock()
    source = pcc_gc_resolve_root_slot_unlocked(source_slot, 0)
    if _is_tuple(source) != 0:
        length: int = load_i64(source, PYTUPLEOBJECT_LEN_OFFSET)
        if index >= 0 and index < length:
            token = pcc_gc_root_copy_lease_prepare_locked(
                result_slot,
                ptr_add(source, PYTUPLEOBJECT_ITEMS_OFFSET + index * C_POINTER_SIZE),
                0, plan,
            )
            prepared = 1
    pcc_py_gc_minor_graph_unlock()
    if prepared != 0:
        pcc_gc_root_copy_lease_finish(plan)
    return token


@c_abi_export("py_func_copy_default_slots")
def py_func_copy_default_slots(defaults_slot: c_ptr, index: int, result_slot: c_ptr) -> int:
    """Copy a declaration value between authoritative, independently owned slots."""
    if py_err_occurred() != 0:
        return -1
    if ptr_is_null(defaults_slot) != 0 or ptr_is_null(result_slot) != 0:
        _func_runtime_error_if_unset(cstr("function defaults"), cstr("missing default owner slot"))
        return -1
    if ptr_is_null(load_ptr(result_slot, 0)) == 0:
        _func_runtime_error_if_unset(cstr("function defaults"), cstr("default output slot is not empty"))
        return -1
    token: int = _func_defaults_copy_item(defaults_slot, index, result_slot)
    if token < 0:
        _func_runtime_error_if_unset(cstr("function defaults"), cstr("default value owner transfer failed"))
        return -1
    if pcc_gc_foreign_lease_release(result_slot, token) != 0:
        pcc_platform_abort()
        return -1
    if ptr_is_null(load_ptr(result_slot, 0)) != 0:
        _func_runtime_error_if_unset(cstr("function defaults"), cstr("default value is absent"))
        return -1
    return 0


def _func_defaults_close(slots, tokens, handles, registered: int) -> None:
    if registered == _FUNC_DEFAULTS_COUNT:
        error_slot = ptr_add(slots, _FUNC_DEFAULTS_ERROR * C_POINTER_SIZE)
        py_tls_exc_swap_slot(error_slot)
        index: int = _FUNC_DEFAULTS_ITEM
        while index >= _FUNC_DEFAULTS_FUNCTION:
            offset: int = index * C_POINTER_SIZE
            slot = ptr_add(slots, offset)
            if pcc_gc_foreign_lease_release(slot, load_i64(tokens, offset)) != 0:
                pcc_platform_abort()
                return
            store_i64(tokens, offset, 0)
            pcc_gc_store_root(slot, null())
            index = index - 1
        py_clear_exception()
        py_tls_exc_swap_slot(error_slot)
    index = registered - 1
    while index >= 0:
        pcc_gc_scheduler_root_unregister_handle(load_ptr(handles, index * C_POINTER_SIZE))
        index = index - 1


def _func_defaults_fill(slots, tokens, result_slot) -> int:
    signature_slot = ptr_add(slots, _FUNC_DEFAULTS_SIGNATURE * C_POINTER_SIZE)
    values_slot = ptr_add(slots, _FUNC_DEFAULTS_VALUES * C_POINTER_SIZE)
    item_slot = ptr_add(slots, _FUNC_DEFAULTS_ITEM * C_POINTER_SIZE)
    if ptr_is_null(_checked_func(load_ptr(slots, 0))) != 0:
        return -1
    if _signature_diagnostic_copy_signature(slots, tokens) != 0:
        return -1
    token: int = _func_defaults_copy_item(signature_slot, 4, values_slot)
    if token < 0:
        return -1
    store_i64(tokens, _FUNC_DEFAULTS_VALUES * C_POINTER_SIZE, token)
    values = load_ptr(values_slot, 0)
    if _is_tuple(values) == 0:
        return -1
    length: int = load_i64(values, PYTUPLEOBJECT_LEN_OFFSET)
    # Publish NEW before any lease acquisition or allocation error check.
    store_ptr(result_slot, 0, py_tuple_new(length))
    result_token: int = pcc_gc_foreign_lease_acquire(result_slot)
    if result_token < 0:
        return -1
    pcc_py_gc_minor_graph_lock()
    pcc_gc_note_slot_write_barrier(null(), result_slot, load_ptr(result_slot, 0))
    pcc_py_gc_minor_graph_unlock()
    status: int = -1
    if ptr_is_null(load_ptr(result_slot, 0)) == 0 and py_err_occurred() == 0:
        status = 0
        index: int = 0
        while index < length and status == 0:
            token = _func_defaults_copy_item(values_slot, index, item_slot)
            if token < 0:
                status = -1
            else:
                store_i64(tokens, _FUNC_DEFAULTS_ITEM * C_POINTER_SIZE, token)
                if ptr_is_null(load_ptr(item_slot, 0)) != 0:
                    status = -1
                else:
                    py_tuple_set_item(load_ptr(result_slot, 0), index, load_ptr(item_slot, 0))
                    if py_err_occurred() != 0:
                        status = -1
                if status == 0:
                    if pcc_gc_foreign_lease_release(item_slot, token) != 0:
                        pcc_platform_abort()
                        return -1
                    store_i64(tokens, _FUNC_DEFAULTS_ITEM * C_POINTER_SIZE, 0)
                    pcc_gc_store_root(item_slot, null())
                index = index + 1
    if pcc_gc_foreign_lease_release(result_slot, result_token) != 0:
        pcc_platform_abort()
        return -1
    return status


@c_abi_export("py_func_copy_signature_defaults_slots")
def py_func_copy_signature_defaults_slots(function_slot: c_ptr, result_slot: c_ptr) -> int:
    """Snapshot the full immutable default vector without touching __defaults__."""
    if py_err_occurred() != 0:
        return -1
    if ptr_is_null(function_slot) != 0 or ptr_is_null(result_slot) != 0:
        _func_runtime_error_if_unset(cstr("function defaults"), cstr("missing signature owner slot"))
        return -1
    if ptr_is_null(load_ptr(result_slot, 0)) == 0:
        _func_runtime_error_if_unset(cstr("function defaults"), cstr("signature output slot is not empty"))
        return -1
    slots = stack_alloc(_FUNC_DEFAULTS_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_FUNC_DEFAULTS_COUNT * C_POINTER_SIZE)
    handles = stack_alloc(_FUNC_DEFAULTS_COUNT * C_POINTER_SIZE)
    memset(slots, 0, _FUNC_DEFAULTS_COUNT * C_POINTER_SIZE)
    memset(tokens, 0, _FUNC_DEFAULTS_COUNT * C_POINTER_SIZE)
    memset(handles, 0, _FUNC_DEFAULTS_COUNT * C_POINTER_SIZE)
    registered: int = 0
    while registered < _FUNC_DEFAULTS_COUNT:
        handle = pcc_gc_scheduler_root_register_handle(ptr_add(slots, registered * C_POINTER_SIZE))
        if ptr_is_null(handle) != 0:
            break
        store_ptr(handles, registered * C_POINTER_SIZE, handle)
        registered = registered + 1
    status: int = -1
    if registered == _FUNC_DEFAULTS_COUNT:
        token: int = pcc_gc_root_copy_lease(slots, function_slot)
        if token >= 0:
            store_i64(tokens, 0, token)
            status = _func_defaults_fill(slots, tokens, result_slot)
    if status != 0:
        _func_runtime_error_if_unset(cstr("function defaults"), cstr("signature default snapshot failed"))
    _func_defaults_close(slots, tokens, handles, registered)
    return status


def _signature_error(fn, sig, nargs: int, kwargs):
    # Bound wrappers bind a signature with self removed, but CPython diagnoses
    # the original function with self counted. Read its live identity and
    # signature only on failure; never snapshot mutable diagnostic metadata.
    slots = stack_alloc(24)
    tokens = stack_alloc(24)
    handles = stack_alloc(24)
    count: int = _signature_diagnostic_open(slots, tokens, handles)
    if count != 3:
        _func_runtime_error_if_unset(cstr("function binding diagnostic"), cstr("diagnostic root registration failed"))
        _signature_diagnostic_close(slots, tokens, handles, count)
        return null()
    # Immediate publication precedes the first parking lease acquisition.
    store_ptr(slots, 0, py_bound_method_function(fn))
    if ptr_is_null(load_ptr(slots, 0)) == 0:
        if _signature_diagnostic_adopt(slots, tokens) != 0 or _signature_diagnostic_copy_signature(slots, tokens) != 0:
            _func_runtime_error_if_unset(cstr("function binding diagnostic"), cstr("diagnostic source lease failed"))
        elif _signature_valid(load_ptr(slots, 8)) != 0:
            _signature_error_unbound(load_ptr(slots, 0), load_ptr(slots, 8), nargs + 1, kwargs)
        else:
            _signature_error_unbound(fn, sig, nargs, kwargs)
    else:
        _signature_error_unbound(fn, sig, nargs, kwargs)
    _signature_diagnostic_close(slots, tokens, handles, count)
    return null()


def _signature_error_unbound(fn, sig, nargs: int, kwargs):
    """Raise the TypeError CPython gives when ``nargs`` positional arguments
    and ``kwargs`` do not bind to ``sig``, checked in CPython's order: keyword problems, too many
    positional arguments, missing positional, missing keyword-only.
    """
    names = py_tuple_get(sig, 1)
    kinds = py_tuple_get(sig, 2)
    has_defaults = py_tuple_get(sig, 3)
    if ptr_is_null(names) or ptr_is_null(kinds) or ptr_is_null(has_defaults):
        _cleanup_signature_parts(names, kinds, has_defaults, null())
        return _func_type_error(cstr("function argument binding failed"))
    n: int = py_tuple_len(names)
    has_varargs: int = 0
    has_varkw: int = 0
    npos: int = 0
    nrequired_pos: int = 0
    nkwonly_given: int = 0
    i: int = 0
    while i < n:
        kind: int = _sig_kind(kinds, i)
        if kind == 3:
            has_varargs = 1
        elif kind == 4:
            has_varkw = 1
        elif kind == 0 or kind == 1:
            npos = npos + 1
            if _sig_has_default(has_defaults, i) == 0:
                nrequired_pos = nrequired_pos + 1
        i = i + 1
    result = null()
    kw_keys = null()
    if _kwargs_empty(kwargs) == 0:
        kw_keys = py_dict_keys(kwargs)
    # 1. keyword problems, in keyword order.
    if ptr_is_null(kw_keys) == 0:
        posonly_as_kw = py_list_new(0)
        k: int = 0
        nkw: int = py_list_len(kw_keys)
        while k < nkw and ptr_is_null(result):
            key = py_list_get(kw_keys, k)
            if ptr_is_null(key) or is_tagged_int(key) or load_i32(key, 8) != PY_TYPE_STR:
                if ptr_is_null(key) == 0:
                    py_decref(key)
                py_decref(posonly_as_kw)
                py_decref(kw_keys)
                _cleanup_signature_parts(names, kinds, has_defaults, null())
                return _func_type_error(cstr("keywords must be strings"))
            match: int = -1
            match_kind: int = -1
            j: int = 0
            while j < n and match < 0:
                formal = py_tuple_get(names, j)
                if ptr_is_null(formal) == 0:
                    if py_str_eq(formal, key) != 0:
                        match = j
                        match_kind = _sig_kind(kinds, j)
                    py_decref(formal)
                j = j + 1
            if match < 0 or match_kind == 3 or match_kind == 4:
                if has_varkw == 0:
                    tail = _sig_lit(cstr("got an unexpected keyword argument "))
                    result = _sig_raise(fn, _sig_cat(tail, _sig_quoted(key)))
                    py_decref(key)
                    py_decref(posonly_as_kw)
                    py_decref(kw_keys)
                    _cleanup_signature_parts(names, kinds, has_defaults, null())
                    return result
            elif match_kind == 1:
                if has_varkw == 0:
                    py_list_append(posonly_as_kw, key)
            elif match_kind == 0 and match < nargs:
                tail = _sig_lit(cstr("got multiple values for argument "))
                result = _sig_raise(fn, _sig_cat(tail, _sig_quoted(key)))
                py_decref(key)
                py_decref(posonly_as_kw)
                py_decref(kw_keys)
                _cleanup_signature_parts(names, kinds, has_defaults, null())
                return result
            elif match_kind == 2:
                nkwonly_given = nkwonly_given + 1
            py_decref(key)
            k = k + 1
        if py_list_len(posonly_as_kw) > 0:
            tail = _sig_lit(
                cstr("got some positional-only arguments passed as keyword arguments: ")
            )
            quoted = py_list_new(0)
            q: int = 0
            while q < py_list_len(posonly_as_kw):
                item = py_list_get(posonly_as_kw, q)
                py_list_append(quoted, item)
                py_decref(item)
                q = q + 1
            listing = _sig_lit(cstr(""))
            q = 0
            while q < py_list_len(quoted):
                if q > 0:
                    listing = _sig_cat(listing, _sig_lit(cstr(", ")))
                item = py_list_get(quoted, q)
                listing = _sig_cat(listing, _sig_quoted(item))
                py_decref(item)
                q = q + 1
            py_decref(quoted)
            py_decref(posonly_as_kw)
            py_decref(kw_keys)
            _cleanup_signature_parts(names, kinds, has_defaults, null())
            return _sig_raise(fn, _sig_cat(tail, listing))
        py_decref(posonly_as_kw)
    # 2. too many positional arguments.
    if has_varargs == 0 and nargs > npos:
        tail = _sig_lit(cstr("takes "))
        if nrequired_pos == npos:
            tail = _sig_cat(tail, _sig_int(npos))
        else:
            tail = _sig_cat(tail, _sig_lit(cstr("from ")))
            tail = _sig_cat(tail, _sig_int(nrequired_pos))
            tail = _sig_cat(tail, _sig_lit(cstr(" to ")))
            tail = _sig_cat(tail, _sig_int(npos))
        if npos == 1 and nrequired_pos == npos:
            tail = _sig_cat(tail, _sig_lit(cstr(" positional argument but ")))
        else:
            tail = _sig_cat(tail, _sig_lit(cstr(" positional arguments but ")))
        tail = _sig_cat(tail, _sig_int(nargs))
        if nkwonly_given != 0:
            if nargs == 1:
                tail = _sig_cat(tail, _sig_lit(cstr(" positional argument (and ")))
            else:
                tail = _sig_cat(tail, _sig_lit(cstr(" positional arguments (and ")))
            tail = _sig_cat(tail, _sig_int(nkwonly_given))
            if nkwonly_given == 1:
                tail = _sig_cat(tail, _sig_lit(cstr(" keyword-only argument) were given")))
            else:
                tail = _sig_cat(tail, _sig_lit(cstr(" keyword-only arguments) were given")))
        elif nargs == 1:
            tail = _sig_cat(tail, _sig_lit(cstr(" was given")))
        else:
            tail = _sig_cat(tail, _sig_lit(cstr(" were given")))
        if ptr_is_null(kw_keys) == 0:
            py_decref(kw_keys)
        _cleanup_signature_parts(names, kinds, has_defaults, null())
        return _sig_raise(fn, tail)
    # 3./4. missing positional, then missing keyword-only.
    pass_kind: int = 0
    while pass_kind < 2:
        missing = py_list_new(0)
        i = 0
        while i < n:
            kind = _sig_kind(kinds, i)
            wanted: int = 0
            if pass_kind == 0 and (kind == 0 or kind == 1) and i >= nargs:
                wanted = 1
            if pass_kind == 1 and kind == 2:
                wanted = 1
            if wanted != 0 and _sig_has_default(has_defaults, i) == 0:
                formal = py_tuple_get(names, i)
                supplied: int = 0
                if ptr_is_null(kwargs) == 0 and ptr_is_null(kw_keys) == 0 and kind != 1:
                    supplied = py_dict_contains(kwargs, formal)
                if supplied == 0:
                    py_list_append(missing, formal)
                py_decref(formal)
            i = i + 1
        count: int = py_list_len(missing)
        if count > 0:
            tail = _sig_lit(cstr("missing "))
            tail = _sig_cat(tail, _sig_int(count))
            if pass_kind == 0:
                tail = _sig_cat(tail, _sig_lit(cstr(" required positional argument")))
            else:
                tail = _sig_cat(tail, _sig_lit(cstr(" required keyword-only argument")))
            if count > 1:
                tail = _sig_cat(tail, _sig_lit(cstr("s")))
            tail = _sig_cat(tail, _sig_lit(cstr(": ")))
            tail = _sig_cat(tail, _sig_name_list(missing))
            py_decref(missing)
            if ptr_is_null(kw_keys) == 0:
                py_decref(kw_keys)
            _cleanup_signature_parts(names, kinds, has_defaults, null())
            return _sig_raise(fn, tail)
        py_decref(missing)
        pass_kind = pass_kind + 1
    if ptr_is_null(kw_keys) == 0:
        py_decref(kw_keys)
    _cleanup_signature_parts(names, kinds, has_defaults, null())
    return _func_type_error(cstr("function arguments do not match its signature"))


def _cleanup_signature_parts(names, kinds, has_defaults, defaults) -> None:
    if ptr_is_null(names) == 0:
        py_decref(names)
    if ptr_is_null(kinds) == 0:
        py_decref(kinds)
    if ptr_is_null(has_defaults) == 0:
        py_decref(has_defaults)
    if ptr_is_null(defaults) == 0:
        py_decref(defaults)


def _signature_default_kind(flag) -> int:
    # Ordinary signatures keep bool flags. Only the explicitly synthesized
    # dataclass signature uses integer 2 to denote a captured factory.
    if is_tagged_int(flag) != 0:
        return py_int_value_i64(flag)
    if ptr_is_null(flag) == 0 and load_i32(flag, 8) == PY_TYPE_INT:
        return py_int_value_i64(flag)
    return 1 if py_obj_truthy(flag) != 0 else 0


def _factory_binding_root(slots, pins, offset: int, value) -> None:
    if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        store_i64(pins, offset, load_i32(value, 12) & 64)
        pcc_gc_pin(value)
    pcc_gc_store_root(ptr_add(slots, offset), value)


def _bind_dataclass_factory(factory, bound, remaining, names, kinds, has_defaults, defaults, name, kind_obj, has_default_obj):
    # Binding now has an arbitrary application callback. Pin and root every
    # suspended binding owner, including products of earlier factories and the
    # current name/flag temporaries. The surrounding call owns pinned fn/args.
    # Reuse its eleven-slot trace map; this introduces no object/record layout.
    slots = stack_alloc(88)
    pins = stack_alloc(88)
    memset(slots, 0, 88)
    memset(pins, 0, 88)
    pcc_gc_frame_enter(global_addr("pcc_native_callable_result_frame_map"), slots)
    _factory_binding_root(slots, pins, 8, factory)
    _factory_binding_root(slots, pins, 16, bound)
    _factory_binding_root(slots, pins, 24, remaining)
    _factory_binding_root(slots, pins, 32, names)
    _factory_binding_root(slots, pins, 40, kinds)
    _factory_binding_root(slots, pins, 48, has_defaults)
    _factory_binding_root(slots, pins, 56, defaults)
    _factory_binding_root(slots, pins, 64, name)
    _factory_binding_root(slots, pins, 72, kind_obj)
    _factory_binding_root(slots, pins, 80, has_default_obj)
    empty_args = py_tuple_new(0)
    _factory_binding_root(slots, pins, 0, empty_args)
    py_decref(empty_args)
    result = null()
    if ptr_is_null(load_ptr(slots, 0)) == 0:
        # A factory returning a generator supplies that generator as the field
        # value. Do not synchronously drive compiler continuation roles here.
        result = py_obj_call_deferred(load_ptr(slots, 8), load_ptr(slots, 0), null())
    if ptr_is_null(result):
        _func_runtime_error_if_unset(
            cstr("dataclass default factory"),
            cstr("dataclass default factory returned NULL without exception"),
        )
    prior_result_pin: int = 0
    if ptr_is_null(result) == 0 and is_tagged_int(result) == 0:
        prior_result_pin = load_i32(result, 12) & 64
    # Later leases may observe an already-pinned aliased input. The earliest
    # lease carries the external bit to restore when handing off the result.
    offset: int = 80
    while offset >= 0:
        if ptr_eq(result, load_ptr(slots, offset)) != 0:
            prior_result_pin = load_i64(pins, offset)
        offset = offset - 8
    empty_args = load_ptr(slots, 0)
    if ptr_is_null(empty_args) == 0 and is_tagged_int(empty_args) == 0:
        pcc_gc_unpin(empty_args)
        if load_i64(pins, 0) != 0:
            atomic_rmw_i32("or", empty_args, 12, 64, "relaxed")
    pcc_gc_store_root(slots, result)
    if ptr_is_null(result) == 0 and is_tagged_int(result) == 0:
        pcc_gc_pin(result)
    py_decref(result)
    offset = 80
    while offset >= 8:
        _func_clear_call_root(slots, pins, offset)
        offset = offset - 8
    result = load_ptr(slots, 0)
    if ptr_is_null(result) == 0 and is_tagged_int(result) == 0:
        atomic_rmw_i32("or", result, 12, 64, "relaxed")
    pcc_gc_frame_leave(slots)
    return pcc_gc_take_pinned_slot(slots, prior_result_pin)


def _bind_signature(sig, args_tuple, kwargs, fn):
    names = py_tuple_get(sig, 1)
    kinds = py_tuple_get(sig, 2)
    has_defaults = py_tuple_get(sig, 3)
    defaults = py_tuple_get(sig, 4)
    if (
        ptr_is_null(names)
        or ptr_is_null(kinds)
        or ptr_is_null(has_defaults)
        or ptr_is_null(defaults)
    ):
        _cleanup_signature_parts(names, kinds, has_defaults, defaults)
        return null()

    nformals: int = py_tuple_len(names)
    kinds_len: int = py_tuple_len(kinds)
    has_defaults_len: int = py_tuple_len(has_defaults)
    defaults_len: int = py_tuple_len(defaults)
    if kinds_len != nformals:
        _cleanup_signature_parts(names, kinds, has_defaults, defaults)
        return _func_type_error(cstr("invalid native function signature"))
    if has_defaults_len != nformals:
        _cleanup_signature_parts(names, kinds, has_defaults, defaults)
        return _func_type_error(cstr("invalid native function signature"))
    if defaults_len != nformals:
        _cleanup_signature_parts(names, kinds, has_defaults, defaults)
        return _func_type_error(cstr("invalid native function signature"))

    args = args_tuple
    made_args: int = 0
    if _is_none_or_null(args) != 0:
        args = py_tuple_new(0)
        made_args = 1
    if _is_tuple(args) == 0:
        if made_args != 0:
            py_decref(args)
        _cleanup_signature_parts(names, kinds, has_defaults, defaults)
        return _func_type_error(cstr("native function args must be a tuple"))

    nargs: int = py_tuple_len(args)
    if py_call_validate_kwargs(kwargs) != 0:
        if made_args != 0:
            py_decref(args)
        _cleanup_signature_parts(names, kinds, has_defaults, defaults)
        return null()

    remaining = py_call_merge_kwargs(null(), kwargs)
    if ptr_is_null(remaining):
        if made_args != 0:
            py_decref(args)
        _cleanup_signature_parts(names, kinds, has_defaults, defaults)
        return null()

    bound = py_tuple_new(nformals)
    if ptr_is_null(bound):
        py_decref(remaining)
        if made_args != 0:
            py_decref(args)
        _cleanup_signature_parts(names, kinds, has_defaults, defaults)
        return null()

    pos_index: int = 0
    saw_varkw: int = 0
    i: int = 0
    while i < nformals:
        name = py_tuple_get(names, i)
        kind_obj = py_tuple_get(kinds, i)
        has_default_obj = py_tuple_get(has_defaults, i)
        default_obj = py_tuple_get(defaults, i)
        if (
            ptr_is_null(name)
            or ptr_is_null(kind_obj)
            or ptr_is_null(has_default_obj)
            or ptr_is_null(default_obj)
        ):
            if ptr_is_null(name) == 0:
                py_decref(name)
            if ptr_is_null(kind_obj) == 0:
                py_decref(kind_obj)
            if ptr_is_null(has_default_obj) == 0:
                py_decref(has_default_obj)
            if ptr_is_null(default_obj) == 0:
                py_decref(default_obj)
            py_decref(bound)
            py_decref(remaining)
            if made_args != 0:
                py_decref(args)
            _cleanup_signature_parts(names, kinds, has_defaults, defaults)
            return null()

        kind: int = py_int_value_i64(kind_obj)
        has_default: int = _signature_default_kind(has_default_obj)

        if kind == 3:  # PCC_FUNC_KIND_VARARGS
            varargs = _copy_varargs(args, pos_index, nargs)
            if ptr_is_null(varargs):
                py_decref(name)
                py_decref(kind_obj)
                py_decref(has_default_obj)
                py_decref(default_obj)
                py_decref(bound)
                py_decref(remaining)
                if made_args != 0:
                    py_decref(args)
                _cleanup_signature_parts(names, kinds, has_defaults, defaults)
                return null()
            py_tuple_set_item(bound, i, varargs)
            py_decref(varargs)
            pos_index = nargs
            py_decref(name)
            py_decref(kind_obj)
            py_decref(has_default_obj)
            py_decref(default_obj)
            i = i + 1
            continue

        if kind == 4:  # PCC_FUNC_KIND_VARKW
            py_tuple_set_item(bound, i, remaining)
            saw_varkw = 1
            py_decref(name)
            py_decref(kind_obj)
            py_decref(has_default_obj)
            py_decref(default_obj)
            i = i + 1
            continue

        if kind != 2 and pos_index < nargs:  # PCC_FUNC_KIND_KW_ONLY
            if (
                kind != 1  # PCC_FUNC_KIND_POS_ONLY
                and py_dict_contains(remaining, name) != 0
            ):
                py_decref(name)
                py_decref(kind_obj)
                py_decref(has_default_obj)
                py_decref(default_obj)
                py_decref(bound)
                py_decref(remaining)
                if made_args != 0:
                    py_decref(args)
                _cleanup_signature_parts(names, kinds, has_defaults, defaults)
                return _signature_error(fn, sig, nargs, kwargs)
            item = py_tuple_get(args, pos_index)
            pos_index = pos_index + 1
            if ptr_is_null(item):
                py_decref(name)
                py_decref(kind_obj)
                py_decref(has_default_obj)
                py_decref(default_obj)
                py_decref(bound)
                py_decref(remaining)
                if made_args != 0:
                    py_decref(args)
                _cleanup_signature_parts(names, kinds, has_defaults, defaults)
                return null()
            py_tuple_set_item(bound, i, item)
            py_decref(item)
            py_decref(name)
            py_decref(kind_obj)
            py_decref(has_default_obj)
            py_decref(default_obj)
            i = i + 1
            continue

        if kind != 1 and py_dict_contains(remaining, name) != 0:
            item2 = py_dict_get(remaining, name)
            py_dict_del(remaining, name)
            if ptr_is_null(item2):
                py_decref(name)
                py_decref(kind_obj)
                py_decref(has_default_obj)
                py_decref(default_obj)
                py_decref(bound)
                py_decref(remaining)
                if made_args != 0:
                    py_decref(args)
                _cleanup_signature_parts(names, kinds, has_defaults, defaults)
                return null()
            py_tuple_set_item(bound, i, item2)
            py_decref(item2)
            py_decref(name)
            py_decref(kind_obj)
            py_decref(has_default_obj)
            py_decref(default_obj)
            i = i + 1
            continue

        if has_default != 0:
            if has_default == 2:
                item3 = _bind_dataclass_factory(default_obj, bound, remaining, names, kinds, has_defaults, defaults, name, kind_obj, has_default_obj)
                if ptr_is_null(item3):
                    py_decref(name)
                    py_decref(kind_obj)
                    py_decref(has_default_obj)
                    py_decref(default_obj)
                    py_decref(bound)
                    py_decref(remaining)
                    if made_args != 0:
                        py_decref(args)
                    _cleanup_signature_parts(names, kinds, has_defaults, defaults)
                    return null()
                py_tuple_set_item(bound, i, item3)
                py_decref(item3)
            else:
                py_tuple_set_item(bound, i, default_obj)
            py_decref(name)
            py_decref(kind_obj)
            py_decref(has_default_obj)
            py_decref(default_obj)
            i = i + 1
            continue

        py_decref(name)
        py_decref(kind_obj)
        py_decref(has_default_obj)
        py_decref(default_obj)
        py_decref(bound)
        py_decref(remaining)
        if made_args != 0:
            py_decref(args)
        _cleanup_signature_parts(names, kinds, has_defaults, defaults)
        return _signature_error(fn, sig, nargs, kwargs)

    if pos_index < nargs:
        py_decref(bound)
        py_decref(remaining)
        if made_args != 0:
            py_decref(args)
        _cleanup_signature_parts(names, kinds, has_defaults, defaults)
        return _signature_error(fn, sig, nargs, kwargs)

    if saw_varkw == 0 and py_dict_len(remaining) != 0:
        py_decref(bound)
        py_decref(remaining)
        if made_args != 0:
            py_decref(args)
        _cleanup_signature_parts(names, kinds, has_defaults, defaults)
        return _signature_error(fn, sig, nargs, kwargs)

    py_decref(remaining)
    if made_args != 0:
        py_decref(args)
    _cleanup_signature_parts(names, kinds, has_defaults, defaults)
    return bound


def _bind_signature_no_kwargs(sig, args_tuple, fn):
    # Fast path for the dominant native-call shape: positional args only.
    # It mirrors _bind_signature but avoids constructing an empty kwargs dict
    # and reads validated tuple slots as borrowed values.
    names = _tuple_borrow_known(sig, 1)
    kinds = _tuple_borrow_known(sig, 2)
    has_defaults = _tuple_borrow_known(sig, 3)
    defaults = _tuple_borrow_known(sig, 4)
    if (
        ptr_is_null(names)
        or ptr_is_null(kinds)
        or ptr_is_null(has_defaults)
        or ptr_is_null(defaults)
    ):
        if ptr_is_null(names):
            return _signature_runtime_error_if_unset(
                fn, cstr("native function signature names tuple is NULL")
            )
        if ptr_is_null(kinds):
            return _signature_runtime_error_if_unset(
                fn, cstr("native function signature kinds tuple is NULL")
            )
        if ptr_is_null(has_defaults):
            return _signature_runtime_error_if_unset(
                fn, cstr("native function signature default flags tuple is NULL")
            )
        return _signature_runtime_error_if_unset(
            fn, cstr("native function signature defaults tuple is NULL")
        )

    nformals: int = load_i64(names, 16)
    if load_i64(kinds, 16) != nformals:
        return _func_type_error(cstr("invalid native function signature"))
    if load_i64(has_defaults, 16) != nformals:
        return _func_type_error(cstr("invalid native function signature"))
    if load_i64(defaults, 16) != nformals:
        return _func_type_error(cstr("invalid native function signature"))

    args = args_tuple
    made_args: int = 0
    if _is_none_or_null(args) != 0:
        args = py_tuple_new(0)
        made_args = 1
    if _is_tuple(args) == 0:
        if made_args != 0:
            py_decref(args)
        return _func_type_error(cstr("native function args must be a tuple"))

    nargs: int = load_i64(args, 16)
    if nargs == nformals:
        positional_only: int = 1
        check_i: int = 0
        while check_i < nformals:
            check_kind_obj = _tuple_borrow_known(kinds, check_i)
            if ptr_is_null(check_kind_obj):
                _signature_runtime_error_if_unset(
                    fn, cstr("native function signature kind entry is NULL")
                )
                if made_args != 0:
                    py_decref(args)
                return null()
            check_kind: int = py_int_value_i64(check_kind_obj)
            if check_kind != 0 and check_kind != 1:
                positional_only = 0
            check_i = check_i + 1
        if positional_only != 0:
            if made_args == 0:
                py_incref(args)
            return args

    bound = py_tuple_new(nformals)
    if ptr_is_null(bound):
        _signature_runtime_error_if_unset(
            fn, cstr("native function bound argument tuple allocation returned NULL")
        )
        if made_args != 0:
            py_decref(args)
        return null()

    pos_index: int = 0
    i: int = 0
    while i < nformals:
        kind_obj = _tuple_borrow_known(kinds, i)
        has_default_obj = _tuple_borrow_known(has_defaults, i)
        default_obj = _tuple_borrow_known(defaults, i)
        if (
            ptr_is_null(kind_obj)
            or ptr_is_null(has_default_obj)
            or ptr_is_null(default_obj)
        ):
            if ptr_is_null(kind_obj):
                _signature_runtime_error_if_unset(
                    fn, cstr("native function signature kind entry is NULL")
                )
            elif ptr_is_null(has_default_obj):
                _signature_runtime_error_if_unset(
                    fn, cstr("native function signature default flag entry is NULL")
                )
            else:
                _signature_runtime_error_if_unset(
                    fn, cstr("native function signature default entry is NULL")
                )
            py_decref(bound)
            if made_args != 0:
                py_decref(args)
            return null()

        kind: int = py_int_value_i64(kind_obj)
        if kind == 3:  # PCC_FUNC_KIND_VARARGS
            varargs = _copy_varargs_known(args, pos_index, nargs, fn)
            if ptr_is_null(varargs):
                py_decref(bound)
                if made_args != 0:
                    py_decref(args)
                return null()
            py_tuple_set_item(bound, i, varargs)
            py_decref(varargs)
            pos_index = nargs
            i = i + 1
            continue

        if kind == 4:  # PCC_FUNC_KIND_VARKW
            empty_kwargs = py_dict_new()
            if ptr_is_null(empty_kwargs):
                _signature_runtime_error_if_unset(
                    fn, cstr("native function empty kwargs dict allocation returned NULL")
                )
                py_decref(bound)
                if made_args != 0:
                    py_decref(args)
                return null()
            py_tuple_set_item(bound, i, empty_kwargs)
            py_decref(empty_kwargs)
            i = i + 1
            continue

        if kind != 2 and pos_index < nargs:  # not KW_ONLY
            item = _tuple_borrow_known(args, pos_index)
            pos_index = pos_index + 1
            if ptr_is_null(item):
                _signature_runtime_error_if_unset(
                    fn, cstr("native function positional argument entry is NULL")
                )
                py_decref(bound)
                if made_args != 0:
                    py_decref(args)
                return null()
            py_tuple_set_item(bound, i, item)
            i = i + 1
            continue

        default_kind: int = _signature_default_kind(has_default_obj)
        if default_kind != 0:
            if default_kind == 2:
                item2 = _bind_dataclass_factory(default_obj, bound, null(), names, kinds, has_defaults, defaults, null(), kind_obj, has_default_obj)
                if ptr_is_null(item2):
                    _signature_runtime_error_if_unset(
                        fn, cstr("native function default factory binding returned NULL")
                    )
                    py_decref(bound)
                    if made_args != 0:
                        py_decref(args)
                    return null()
                py_tuple_set_item(bound, i, item2)
                py_decref(item2)
            else:
                py_tuple_set_item(bound, i, default_obj)
            i = i + 1
            continue

        py_decref(bound)
        if made_args != 0:
            py_decref(args)
        return _signature_error(fn, sig, nargs, null())

    if pos_index < nargs:
        py_decref(bound)
        if made_args != 0:
            py_decref(args)
        return _signature_error(fn, sig, nargs, null())

    if made_args != 0:
        py_decref(args)
    return bound


@c_abi_export("py_func_new_bound")
def py_func_new_bound(entry, captures_tuple, name, self_obj):
    if ptr_is_null(entry):
        return null()
    fn = pcc_gc_alloc(96, PY_TYPE_FUNC, 0)
    if ptr_is_null(fn):
        return null()
    store_ptr(fn, 16, null())
    store_ptr(fn, 24, null())
    store_ptr(fn, 32, null())
    store_ptr(fn, 40, null())
    store_ptr(fn, 48, null())
    store_ptr(fn, 56, entry)
    store_ptr(fn, 72, name)
    store_ptr(fn, 80, null())
    store_ptr(fn, 88, null())
    captures = captures_tuple
    made_captures: int = 0
    if ptr_is_null(captures):
        captures = py_tuple_new(0)
        made_captures = 1
    store_ptr(fn, 64, null())
    pcc_gc_store_ptr(fn, ptr_add(fn, 64), captures)
    if ptr_is_null(self_obj) == 0:
        pcc_gc_store_ptr(fn, ptr_add(fn, 80), self_obj)
    if made_captures != 0:
        py_decref(captures)
    py_gc_track(fn)
    pcc_gc_publish_initialized(fn)
    return fn


@c_abi_export("py_func_new_named")
def py_func_new_named(entry, captures_tuple, name):
    return py_func_new_bound(entry, captures_tuple, name, null())


@c_abi_export("py_func_new")
def py_func_new(entry, captures_tuple):
    return py_func_new_named(entry, captures_tuple, null())


@c_abi_export("py_func_slot_has_native_entry")
def py_func_slot_has_native_entry(slot, entry) -> int:
    """1 if the root ``slot`` holds an unbound function object of ``entry``.

    A caller that statically knows the native body behind a live binding asks
    this about the callable it loaded, and calls that body directly instead
    of binding a generic argument tuple. Captures are not compared: such an
    entry belongs to exactly one capture-free module-level definition.
    """
    fn = pcc_gc_load_ptr(null(), slot)
    if ptr_is_null(fn) or is_tagged_int(fn):
        return 0
    if load_i32(fn, 8) != PY_TYPE_FUNC:
        return 0
    if not ptr_eq(load_ptr(fn, 56), entry):
        return 0
    # Offset 80 is the receiver of a bound function (py_func_new_bound).
    if not ptr_is_null(load_ptr(fn, 80)):
        return 0
    return 1


def _builtin_function_cache_mutex():
    slot = global_addr("pcc_builtin_function_cache_mutex_bits")
    bits: int = atomic_load_i64(slot, 0, "acquire")
    if bits != 0:
        return int_to_ptr(bits)
    candidate = pcc_mutex_new()
    if ptr_is_null(candidate):
        return null()
    installed: int = atomic_cas_i64(slot, 0, 0, ptr_to_int(candidate), "acq_rel", "acquire")
    if installed != 0:
        pcc_mutex_free(candidate)
        return int_to_ptr(installed)
    return candidate


def _builtin_value_pin_slot(slots, offset: int) -> int:
    # A root is authoritative throughout construction. Pin only a bounded
    # raw-ABI operation or the final frame-leave/return handoff.
    pcc_py_gc_minor_graph_lock()
    value = pcc_gc_load_ptr(null(), ptr_add(slots, offset))
    prior: int = 0
    if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        prior = load_i32(value, 12) & 64
        pcc_gc_pin(value)
    pcc_py_gc_minor_graph_unlock()
    return prior


def _builtin_value_unpin_slot(slots, offset: int, prior: int) -> None:
    value = pcc_gc_load_ptr(null(), ptr_add(slots, offset))
    if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        pcc_gc_unpin(value)
        if prior != 0:
            atomic_rmw_i32("or", value, 12, 64, "relaxed")


def _builtin_value_finish(slots, result_offset: int):
    pins = stack_alloc(88)
    memset(pins, 0, 88)
    if ptr_is_null(load_ptr(slots, result_offset)):
        _func_keep_call_error(slots, pins, 32)
    offset: int = 0
    while offset < 88:
        if offset != result_offset and offset != 32:
            pcc_gc_store_root(ptr_add(slots, offset), null())
        offset = offset + 8
    _func_clear_call_root(slots, pins, 32)
    prior: int = _builtin_value_pin_slot(slots, result_offset)
    pcc_gc_frame_leave(slots)
    return pcc_gc_take_pinned_slot(ptr_add(slots, result_offset), prior)


def _builtin_function_cache_load(slots) -> None:
    pcc_py_gc_minor_graph_lock()
    cache = pcc_gc_load_ptr(null(), global_addr("pcc_builtin_function_cache"))
    pcc_gc_store_root(ptr_add(slots, 8), cache)
    pcc_py_gc_minor_graph_unlock()


def _builtin_function_cache_get(slots) -> None:
    cache_pin: int = _builtin_value_pin_slot(slots, 8)
    key_pin: int = _builtin_value_pin_slot(slots, 0)
    value = py_dict_get(pcc_gc_load_ptr(null(), ptr_add(slots, 8)),
                        pcc_gc_load_ptr(null(), slots))
    # py_dict_get returns one owner. Transfer it directly into the frame.
    store_ptr(slots, 24, value)
    pcc_gc_note_write_barrier(null(), value)
    _builtin_value_unpin_slot(slots, 0, key_pin)
    _builtin_value_unpin_slot(slots, 8, cache_pin)


def _builtin_function_cache_set(slots) -> None:
    cache_pin: int = _builtin_value_pin_slot(slots, 8)
    key_pin: int = _builtin_value_pin_slot(slots, 0)
    value_pin: int = _builtin_value_pin_slot(slots, 16)
    py_dict_set(pcc_gc_load_ptr(null(), ptr_add(slots, 8)),
                pcc_gc_load_ptr(null(), slots),
                pcc_gc_load_ptr(null(), ptr_add(slots, 16)))
    _builtin_value_unpin_slot(slots, 16, value_pin)
    _builtin_value_unpin_slot(slots, 0, key_pin)
    _builtin_value_unpin_slot(slots, 8, cache_pin)


def _builtin_function_cache_begin(slots, name):
    """Key in slot 0, cache in 8, any published value in 24; the mutex or NULL.

    Registers the cache root and creates the cache once. A NULL return has
    set an exception.
    """
    key = py_str_new(name, strlen(name))
    store_ptr(slots, 0, key)
    pcc_gc_note_write_barrier(null(), key)
    mutex = _builtin_function_cache_mutex()
    if ptr_is_null(key) or ptr_is_null(mutex) or pcc_mutex_lock(mutex) != 0:
        _func_runtime_error_if_unset(cstr("builtin value"), cstr("builtin cache initialization failed"))
        return null()
    if load_i32(global_addr("pcc_builtin_function_cache_registered"), 0) == 0:
        handle = pcc_gc_scheduler_root_register_handle(global_addr("pcc_builtin_function_cache"))
        if ptr_is_null(handle):
            pcc_mutex_unlock(mutex)
            _func_runtime_error_if_unset(cstr("builtin value"), cstr("builtin cache root registration failed"))
            return null()
        store_i32(global_addr("pcc_builtin_function_cache_registered"), 0, 1)
    _builtin_function_cache_load(slots)
    pcc_mutex_unlock(mutex)
    if ptr_is_null(load_ptr(slots, 8)):
        cache = py_dict_new()
        store_ptr(slots, 8, cache)
        pcc_gc_note_write_barrier(null(), cache)
        if ptr_is_null(cache) or pcc_mutex_lock(mutex) != 0:
            _func_runtime_error_if_unset(cstr("builtin value"), cstr("builtin cache allocation failed"))
            return null()
        pcc_py_gc_minor_graph_lock()
        published = pcc_gc_load_ptr(null(), global_addr("pcc_builtin_function_cache"))
        if ptr_is_null(published):
            pcc_gc_store_root(global_addr("pcc_builtin_function_cache"), pcc_gc_load_ptr(null(), ptr_add(slots, 8)))
        else:
            pcc_gc_store_root(ptr_add(slots, 8), published)
        pcc_py_gc_minor_graph_unlock()
        pcc_mutex_unlock(mutex)
    if pcc_mutex_lock(mutex) != 0:
        _func_runtime_error_if_unset(cstr("builtin value"), cstr("builtin cache lookup lock failed"))
        return null()
    _builtin_function_cache_get(slots)
    pcc_mutex_unlock(mutex)
    return mutex


def _builtin_function_cache_publish(slots, mutex) -> None:
    """Publish slot 16 unless another thread won; slot 24 holds the winner."""
    if pcc_mutex_lock(mutex) != 0:
        _func_runtime_error_if_unset(cstr("builtin value"), cstr("builtin cache publication lock failed"))
        return
    _builtin_function_cache_get(slots)
    if ptr_is_null(load_ptr(slots, 24)) and py_err_occurred() == 0:
        _builtin_function_cache_set(slots)
        if py_err_occurred() == 0:
            _builtin_function_cache_get(slots)
    pcc_mutex_unlock(mutex)
    if ptr_is_null(load_ptr(slots, 24)):
        _func_runtime_error_if_unset(cstr("builtin value"), cstr("builtin cache publication failed"))


@c_abi_export("py_builtin_function_value")
def py_builtin_function_value(entry, name):
    if ptr_is_null(entry) or ptr_is_null(name):
        return _func_runtime_error_if_unset(cstr("builtin value"), cstr("invalid builtin function identity"))
    slots = stack_alloc(88)
    memset(slots, 0, 88)
    pcc_gc_frame_enter(global_addr("pcc_native_callable_result_frame_map"), slots)
    mutex = _builtin_function_cache_begin(slots, name)
    if ptr_is_null(mutex) or ptr_is_null(load_ptr(slots, 24)) == 0 or py_err_occurred() != 0:
        return _builtin_value_finish(slots, 24)
    captures = py_tuple_new(0)
    store_ptr(slots, 16, captures)
    pcc_gc_note_write_barrier(null(), captures)
    if ptr_is_null(captures):
        _func_runtime_error_if_unset(cstr("builtin value"), cstr("builtin captures allocation failed"))
        return _builtin_value_finish(slots, 24)
    captures_pin: int = _builtin_value_pin_slot(slots, 16)
    candidate = py_func_new_named(entry, pcc_gc_load_ptr(null(), ptr_add(slots, 16)), name)
    store_ptr(slots, 40, candidate)
    pcc_gc_note_write_barrier(null(), candidate)
    _builtin_value_unpin_slot(slots, 16, captures_pin)
    pcc_gc_store_root(ptr_add(slots, 16), pcc_gc_load_ptr(null(), ptr_add(slots, 40)))
    pcc_gc_store_root(ptr_add(slots, 40), null())
    if ptr_is_null(candidate):
        _func_runtime_error_if_unset(cstr("builtin value"), cstr("builtin function construction failed"))
        return _builtin_value_finish(slots, 24)
    _builtin_function_cache_publish(slots, mutex)
    return _builtin_value_finish(slots, 24)


@c_abi_export("py_builtin_function_publish")
def py_builtin_function_publish(candidate, name):
    """The one process-wide builtin function object named ``name`` (NEW).

    Every compiled module builds its own wrapper for a builtin it names as
    a value. The first one published becomes the builtin: its __module__ is
    'builtins' and it carries PY_FLAG_FUNC_BUILTIN, both set before any
    other thread can see it. Later candidates are left to their module, so
    ``len is len`` holds across modules.
    """
    if ptr_is_null(name) or ptr_is_null(_checked_func(candidate)):
        return _func_runtime_error_if_unset(cstr("builtin value"), cstr("invalid builtin function candidate"))
    slots = stack_alloc(88)
    memset(slots, 0, 88)
    pcc_gc_frame_enter(global_addr("pcc_native_callable_result_frame_map"), slots)
    pcc_gc_store_root(ptr_add(slots, 16), candidate)
    mutex = _builtin_function_cache_begin(slots, name)
    if ptr_is_null(mutex) or ptr_is_null(load_ptr(slots, 24)) == 0 or py_err_occurred() != 0:
        return _builtin_value_finish(slots, 24)
    if py_func_init_metadata_slots_raw(ptr_add(slots, 16), cstr("builtins"), name) != 0:
        return _builtin_value_finish(slots, 24)
    token: int = pcc_gc_foreign_lease_acquire(ptr_add(slots, 16))
    if token < 0:
        _func_runtime_error_if_unset(cstr("builtin value"), cstr("builtin candidate lease failed"))
        return _builtin_value_finish(slots, 24)
    # PY_FLAG_FUNC_BUILTIN; a literal, as at every atomic flag update here.
    atomic_rmw_i32("or", load_ptr(slots, 16), 12, 536870912, "relaxed")
    if pcc_gc_foreign_lease_release(ptr_add(slots, 16), token) != 0:
        pcc_platform_abort()
        return null()
    _builtin_function_cache_publish(slots, mutex)
    return _builtin_value_finish(slots, 24)


@c_abi_export("py_builtin_abs_entry")
def py_builtin_abs_entry(captures, args):
    slots = stack_alloc(88)
    memset(slots, 0, 88)
    pcc_gc_frame_enter(global_addr("pcc_native_callable_result_frame_map"), slots)
    pcc_gc_store_root(slots, args)
    if py_tuple_len(pcc_gc_load_ptr(null(), slots)) != 1:
        _func_type_error(cstr("abs() takes exactly one argument"))
        return _builtin_value_finish(slots, 16)
    argument = py_tuple_get(pcc_gc_load_ptr(null(), slots), 0)
    store_ptr(slots, 8, argument)
    pcc_gc_note_write_barrier(null(), argument)
    argument_pin: int = _builtin_value_pin_slot(slots, 8)
    result = py_obj_abs(pcc_gc_load_ptr(null(), ptr_add(slots, 8)))
    store_ptr(slots, 16, result)
    pcc_gc_note_write_barrier(null(), result)
    _builtin_value_unpin_slot(slots, 8, argument_pin)
    if ptr_is_null(result):
        _func_runtime_error_if_unset(cstr("abs"), cstr("absolute result returned NULL without exception"))
    return _builtin_value_finish(slots, 16)


def _func_keep_call_error(slots, pins, offset: int) -> None:
    if ptr_is_null(load_ptr(slots, offset)) == 0:
        return
    error = py_current_exception()
    if ptr_is_null(error):
        return
    prior_pin: int = load_i32(error, 12) & 64
    pcc_gc_pin(error)
    py_incref(error)
    store_ptr(slots, offset, error)
    store_i64(pins, offset, prior_pin)
    pcc_gc_note_write_barrier(null(), error)


def _func_clear_call_root(slots, pins, offset: int) -> None:
    value = load_ptr(slots, offset)
    if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        pcc_gc_unpin(value)
        if load_i64(pins, offset) != 0:
            atomic_rmw_i32("or", value, 12, 64, "relaxed")
    pcc_gc_store_root(ptr_add(slots, offset), null())


def _func_call_pin_resolved(slots, pins, offset: int) -> None:
    # Only called after a counted lease has resolved this authoritative slot.
    value = load_ptr(slots, offset)
    store_i64(pins, offset, 0)
    if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        store_i64(pins, offset, load_i32(value, 12) & 64)
        pcc_gc_pin(value)
    pcc_py_gc_minor_graph_lock()
    pcc_gc_note_slot_write_barrier(null(), ptr_add(slots, offset), load_ptr(slots, offset))
    pcc_py_gc_minor_graph_unlock()


def _func_call_adopt_owned(slots, pins, offset: int) -> int:
    # Producer stores NEW directly into its preregistered slot, then acquires
    # before inspecting a header: the returned address may already forward.
    store_i64(pins, offset, -1)
    token: int = pcc_gc_foreign_lease_acquire(ptr_add(slots, offset))
    if token < 0:
        pcc_gc_store_root(ptr_add(slots, offset), null())
        _func_runtime_error_if_unset(cstr("function call"), cstr("returned owner lease acquisition failed"))
        return -1
    store_i64(pins, 88 + offset, token)
    _func_call_pin_resolved(slots, pins, offset)
    return 0


def _func_call_copy_captures(slots, pins) -> int:
    # fn.captures is an owning managed field. Copy its actual slot, not a
    # borrowed value loaded before a lock wait or callback can move the child.
    store_i64(pins, 24, -1)
    fn = load_ptr(slots, 64)
    token: int = pcc_gc_root_copy_lease(ptr_add(slots, 24), ptr_add(fn, 64))
    if token < 0:
        _func_runtime_error_if_unset(cstr("function call"), cstr("captures owner transfer failed"))
        return -1
    store_i64(pins, 112, token)
    _func_call_pin_resolved(slots, pins, 24)
    return 0


def _func_call_drop(slots, pins, offset: int) -> None:
    slot = ptr_add(slots, offset)
    if pcc_gc_foreign_lease_release(slot, load_i64(pins, 88 + offset)) != 0:
        pcc_platform_abort()
        return
    store_i64(pins, 88 + offset, 0)
    if load_i64(pins, offset) < 0:
        # An adoption failure did not acquire the legacy pin protocol.
        pcc_gc_store_root(slot, null())
    else:
        _func_clear_call_root(slots, pins, offset)


def _func_call_keep_error(slots, pins) -> None:
    if ptr_is_null(load_ptr(slots, 16)) == 0:
        return
    # Transfer TLS's owner into the registered slot before any header access.
    py_tls_exc_swap_slot(ptr_add(slots, 16))
    if ptr_is_null(load_ptr(slots, 16)) == 0:
        _func_call_adopt_owned(slots, pins, 16)


def _func_call_restore_error(slots, pins) -> None:
    if ptr_is_null(load_ptr(slots, 16)):
        return
    py_clear_exception()
    error = load_ptr(slots, 16)
    prior: int = load_i64(pins, 16)
    # Release through the token-bearing slot BEFORE transferring it to TLS.
    if pcc_gc_foreign_lease_release(ptr_add(slots, 16), load_i64(pins, 104)) != 0:
        pcc_platform_abort()
        return
    store_i64(pins, 104, 0)
    py_tls_exc_swap_slot(ptr_add(slots, 16))
    if ptr_is_null(error) == 0 and is_tagged_int(error) == 0 and prior >= 0:
        pcc_gc_unpin(error)
        if prior != 0:
            atomic_rmw_i32("or", error, 12, 64, "relaxed")
    store_i64(pins, 16, 0)


@c_abi_export("py_func_call_kwargs")
def py_func_call_kwargs(callable_obj, args_tuple, kwargs):
    # The existing C ABI borrows inputs for this call. Keep each input stable
    # across binder/entry polls, including nested transparent adapters.
    argument_pins = stack_alloc(24)
    memset(argument_pins, 0, 24)
    # Unrolled: a loop latch would park with later borrowed copies unpinned.
    if ptr_is_null(callable_obj) == 0 and is_tagged_int(callable_obj) == 0:
        store_i64(argument_pins, 0, load_i32(callable_obj, 12) & 64)
        pcc_gc_pin(callable_obj)
    if ptr_is_null(args_tuple) == 0 and is_tagged_int(args_tuple) == 0:
        store_i64(argument_pins, 8, load_i32(args_tuple, 12) & 64)
        pcc_gc_pin(args_tuple)
    if ptr_is_null(kwargs) == 0 and is_tagged_int(kwargs) == 0:
        store_i64(argument_pins, 16, load_i32(kwargs, 12) & 64)
        pcc_gc_pin(kwargs)
    roles: int = 0
    if ptr_is_null(callable_obj) == 0 and is_tagged_int(callable_obj) == 0:
        if load_i32(callable_obj, 8) == PY_TYPE_FUNC:
            roles = load_i32(callable_obj, 12)
    context: int = load_i32(global_addr("pcc_native_callable_sync_context"), 0)
    previous_forward: int = load_i32(global_addr("pcc_native_callable_forward_context"), 0)
    store_i32(global_addr("pcc_native_callable_sync_context"), 0, 1)
    forward: int = (context & 1) if (roles & PY_FLAG_FUNC_TRANSPARENT_CALL) != 0 else 1
    store_i32(global_addr("pcc_native_callable_forward_context"), 0, forward)
    drive: int = 1 if (context & 1) != 0 and (roles & (PY_FLAG_FUNC_AUTO_PARK | PY_FLAG_FUNC_CONTINUATION_FACTORY)) != 0 and (roles & PY_FLAG_FUNC_TRANSPARENT_CALL) == 0 else 0
    slots = stack_alloc(88)
    # First lane preserves the legacy raw-ABI pin state; the independent
    # second lane owns counted leases for extracted children and NEW results.
    pins = stack_alloc(176)
    memset(slots, 0, 88)
    memset(pins, 0, 176)
    pcc_gc_frame_enter(global_addr("pcc_native_callable_result_frame_map"), slots)
    store_i64(pins, 64, load_i64(argument_pins, 0))
    pcc_gc_store_root(ptr_add(slots, 64), callable_obj)
    store_i64(pins, 72, load_i64(argument_pins, 8))
    pcc_gc_store_root(ptr_add(slots, 72), args_tuple)
    store_i64(pins, 80, load_i64(argument_pins, 16))
    pcc_gc_store_root(ptr_add(slots, 80), kwargs)
    # slot0 = compiler-created child; slot1 = returned user value; slot2 =
    # pending exception protected across all owner cleanup. Slots3..7 own
    # captures, signature, inner captures, synthesized args and bound args.
    output_offset: int = 0 if drive else 8
    _func_call_kwargs_body(slots, pins, output_offset, context & 2)
    if drive and ptr_is_null(load_ptr(slots, 0)) == 0:
        store_ptr(slots, 8, py_gen_run_may_park_sync(load_ptr(slots, 0)))
        _func_call_adopt_owned(slots, pins, 8)
    if ptr_is_null(load_ptr(slots, 8)):
        _func_call_keep_error(slots, pins)
    index: int = 0
    result = load_ptr(slots, 8)
    prior_result_pin: int = load_i64(pins, 8)
    if ptr_is_null(result) == 0:
        if ptr_eq(result, load_ptr(slots, 0)):
            prior_result_pin = load_i64(pins, 0)
        # A native continuation factory can return its captures container.
        # The body keeps this lease through synchronous driving; the other
        # binder temporaries have already been cleared before the drive.
        if ptr_eq(result, load_ptr(slots, 24)):
            prior_result_pin = load_i64(pins, 24)
        # An aliased result restores the external pin state, not a pin this
        # invocation itself acquired for the earliest matching argument.
        index = 3
        while index > 0:
            index = index - 1
            if ptr_eq(result, load_ptr(slots, 64 + index * 8)):
                prior_result_pin = load_i64(pins, 64 + index * 8)
    _func_call_drop(slots, pins, 0)
    _func_call_drop(slots, pins, 56)
    _func_call_drop(slots, pins, 48)
    _func_call_drop(slots, pins, 40)
    _func_call_drop(slots, pins, 32)
    _func_call_drop(slots, pins, 24)
    _func_call_drop(slots, pins, 80)
    _func_call_drop(slots, pins, 72)
    _func_call_drop(slots, pins, 64)
    _func_call_restore_error(slots, pins)
    # Argument/child unpins may have cleared the same bit on an aliased
    # result. The result lease still exists; reload the healed root and
    # restore that bit without acquiring an extra metric-counted pin.
    result = load_ptr(slots, 8)
    if ptr_is_null(result) == 0 and is_tagged_int(result) == 0:
        atomic_rmw_i32("or", result, 12, 64, "relaxed")
    pcc_gc_frame_leave(slots)
    store_i32(global_addr("pcc_native_callable_forward_context"), 0, previous_forward)
    store_i32(global_addr("pcc_native_callable_sync_context"), 0, context)
    # This is the strictly terminal raw-ABI bridge. The counted output owner
    # stayed live through every callback; its legacy pin now bridges the last
    # lease release and the nonparking transfer to the caller.
    if pcc_gc_foreign_lease_release(ptr_add(slots, 8), load_i64(pins, 96)) != 0:
        pcc_platform_abort()
        return null()
    store_i64(pins, 96, 0)
    return pcc_gc_take_pinned_slot(ptr_add(slots, 8), prior_result_pin)


@c_abi_export("py_obj_call_sync")
def py_obj_call_sync(callable_obj, args, kwargs):
    previous: int = load_i32(global_addr("pcc_native_callable_sync_context"), 0)
    store_i32(global_addr("pcc_native_callable_sync_context"), 0, 1)
    result = py_obj_call(callable_obj, args, kwargs)
    store_i32(global_addr("pcc_native_callable_sync_context"), 0, previous)
    return result


@c_abi_export("py_obj_call_default_sync")
def py_obj_call_default_sync(callable_obj, args, kwargs):
    # Preserve the slot dispatcher's default-only decision across the
    # synchronous-construction boundary; do not repeat metaclass lookup.
    previous: int = load_i32(global_addr("pcc_native_callable_sync_context"), 0)
    store_i32(global_addr("pcc_native_callable_sync_context"), 0, 1)
    result = py_obj_call_default(callable_obj, args, kwargs)
    store_i32(global_addr("pcc_native_callable_sync_context"), 0, previous)
    return result


@c_abi_export("py_obj_call_context_is_deferred")
def py_obj_call_context_is_deferred() -> int:
    # The actual non-PyFunc semantic callee consumes this request too. Keep
    # TLS storage in its defining object; other objects use this owned query.
    return 1 if (load_i32(global_addr("pcc_native_callable_sync_context"), 0) & 1) == 0 else 0


@c_abi_export("py_obj_call_slots_sync")
def py_obj_call_slots_sync(callable_slot, args_slot, kwargs_slot, result_slot) -> int:
    # Construction consumes defer before special-method selection too.
    # Preserve the slot contract and restore context on both scalar outcomes.
    previous: int = load_i32(global_addr("pcc_native_callable_sync_context"), 0)
    store_i32(global_addr("pcc_native_callable_sync_context"), 0, 1)
    status: int = py_obj_call_slots(callable_slot, args_slot, kwargs_slot, result_slot)
    store_i32(global_addr("pcc_native_callable_sync_context"), 0, previous)
    return status


@c_abi_export("py_obj_call_deferred")
def py_obj_call_deferred(callable_obj, args, kwargs):
    previous: int = load_i32(global_addr("pcc_native_callable_sync_context"), 0)
    store_i32(global_addr("pcc_native_callable_sync_context"), 0, 0)
    result = py_obj_call(callable_obj, args, kwargs)
    store_i32(global_addr("pcc_native_callable_sync_context"), 0, previous)
    return result


@c_abi_export("py_obj_call_forward")
def py_obj_call_forward(callable_obj, args, kwargs):
    # pcc.virtual_thread.forward_call(target, args, kwargs) lowers here.
    if _is_tuple(args) == 0:
        return _func_type_error(cstr("forward_call() arguments must be a tuple"))
    if ptr_is_null(kwargs) == 0 and _is_dict(kwargs) == 0:
        return _func_type_error(cstr("forward_call() keyword arguments must be a dict"))
    previous: int = load_i32(global_addr("pcc_native_callable_sync_context"), 0)
    forwarded: int = load_i32(global_addr("pcc_native_callable_forward_context"), 0)
    store_i32(global_addr("pcc_native_callable_sync_context"), 0, forwarded)
    result = py_obj_call(callable_obj, args, kwargs)
    store_i32(global_addr("pcc_native_callable_sync_context"), 0, previous)
    return result


@c_abi_export("py_func_call_bound_forward")
def py_func_call_bound_forward(callable_obj, args):
    previous: int = load_i32(global_addr("pcc_native_callable_sync_context"), 0)
    forwarded: int = load_i32(global_addr("pcc_native_callable_forward_context"), 0) & 1
    # bit1 is a one-callee binder token, never forwarded to ordinary body calls.
    store_i32(global_addr("pcc_native_callable_sync_context"), 0, forwarded | 2)
    result = py_func_call_kwargs(callable_obj, args, null())
    store_i32(global_addr("pcc_native_callable_sync_context"), 0, previous)
    return result


def _func_call_kwargs_body(slots, pins, output_offset: int, already_bound: int) -> None:
    callable_obj = load_ptr(slots, 64)
    args_tuple = load_ptr(slots, 72)
    kwargs = load_ptr(slots, 80)
    if py_call_validate_kwargs(kwargs) != 0:
        return
    fn = _checked_func(callable_obj)
    if ptr_is_null(fn):
        if ptr_is_null(callable_obj):
            _func_type_error(
                cstr("native function call received NULL callable")
            )
            return
        _func_type_error(
            cstr("native function call requires a function object")
        )
        return
    entry = load_ptr(fn, 56)
    if ptr_is_null(entry):
        _func_runtime_error_if_unset(
            cstr("py_func_call_kwargs"),
            cstr("native function object has no entry point")
        )
        return
    args = args_tuple
    made_args: int = 0
    if _is_none_or_null(args) != 0:
        args = py_tuple_new(0)
        store_ptr(slots, 48, args)
        if ptr_is_null(args) == 0 and is_tagged_int(args) == 0:
            store_i64(pins, 48, load_i32(args, 12) & 64)
            pcc_gc_pin(args)
            pcc_gc_note_write_barrier(null(), args)
        made_args = 1
        if ptr_is_null(args):
            _func_runtime_error_if_unset(
                cstr("py_tuple_new"),
                cstr("native function could not create its argument tuple")
            )
            return
    captures = pcc_gc_load_ptr(fn, ptr_add(fn, 64))
    # This field load borrows from a pinned function. Pin before retaining;
    # thereafter the owned slot protects it independently of function mutation.
    if ptr_is_null(captures) == 0 and is_tagged_int(captures) == 0:
        store_i64(pins, 24, load_i32(captures, 12) & 64)
        pcc_gc_pin(captures)
    py_incref(captures)
    store_ptr(slots, 24, captures)
    pcc_gc_note_write_barrier(null(), captures)
    actual_captures = captures
    sig = null()
    owns_actual_captures: int = 0

    captures_len: int = 0
    if _is_tuple(captures) != 0:
        captures_len = py_tuple_len(captures)
    if captures_len == 2:
        candidate = py_tuple_get(captures, 1)
        store_ptr(slots, 32, candidate)
        if ptr_is_null(candidate) == 0 and is_tagged_int(candidate) == 0:
            store_i64(pins, 32, load_i32(candidate, 12) & 64)
            pcc_gc_pin(candidate)
            pcc_gc_note_write_barrier(null(), candidate)
        if _signature_valid(candidate) != 0:
            inner = py_tuple_get(captures, 0)
            store_ptr(slots, 40, inner)
            if ptr_is_null(inner) == 0 and is_tagged_int(inner) == 0:
                store_i64(pins, 40, load_i32(inner, 12) & 64)
                pcc_gc_pin(inner)
                pcc_gc_note_write_barrier(null(), inner)
            if ptr_is_null(inner):
                _func_clear_call_root(slots, pins, 32)
                if made_args != 0:
                    _func_clear_call_root(slots, pins, 48)
                _func_runtime_error_if_unset(
                    cstr("py_func_signature_from_captures"),
                    cstr("native function signature has no captures tuple")
                )
                return
            sig = candidate
            actual_captures = inner
            owns_actual_captures = 1
        else:
            if ptr_is_null(candidate) == 0:
                _func_clear_call_root(slots, pins, 32)

    kwargs_are_empty: int = _kwargs_empty(kwargs)
    if ptr_is_null(sig) and kwargs_are_empty == 0:
        if made_args != 0:
            _func_clear_call_root(slots, pins, 48)
        _func_type_error(cstr("native function does not accept keywords"))
        return

    call_args = args
    owns_call_args: int = 0
    if ptr_is_null(sig) == 0 and already_bound == 0:
        if kwargs_are_empty != 0:
            call_args = _bind_signature_no_kwargs(sig, args, fn)
        else:
            call_args = _bind_signature(sig, args, kwargs, fn)
        store_ptr(slots, 56, call_args)
        if ptr_is_null(call_args) == 0 and is_tagged_int(call_args) == 0:
            store_i64(pins, 56, load_i32(call_args, 12) & 64)
            pcc_gc_pin(call_args)
            pcc_gc_note_write_barrier(null(), call_args)
        if ptr_is_null(call_args):
            # Validate the binder's return before cleanup can run deallocators
            # and accidentally provide an unrelated pending exception.
            _func_runtime_error_if_unset(
                cstr("py_func_bind_signature"),
                cstr(
                    "native function argument binding returned NULL without exception"
                )
            )
            _func_clear_call_root(slots, pins, 32)
            if owns_actual_captures != 0:
                _func_clear_call_root(slots, pins, 40)
            if made_args != 0:
                _func_clear_call_root(slots, pins, 48)
            return
        owns_call_args = 1

    # Nested calls can clear the single pin bit. Caller and binder roots are
    # authoritative after arbitrary callbacks; reload every pointer we reuse.
    actual_captures = load_ptr(slots, 40) if owns_actual_captures else load_ptr(slots, 24)
    if owns_call_args:
        call_args = load_ptr(slots, 56)
    elif made_args:
        call_args = load_ptr(slots, 48)
    else:
        call_args = load_ptr(slots, 72)
    result = call_ptr2(entry, actual_captures, call_args)
    store_ptr(slots, output_offset, result)
    if ptr_is_null(result) == 0 and is_tagged_int(result) == 0:
        store_i64(pins, output_offset, load_i32(result, 12) & 64)
        pcc_gc_pin(result)
        pcc_gc_note_write_barrier(null(), result)
    # The result may alias a binder temporary. Restore the pin state from
    # before the earliest matching temporary lease, not that lease's own bit.
    if ptr_is_null(result) == 0:
        if ptr_eq(result, load_ptr(slots, 56)):
            store_i64(pins, output_offset, load_i64(pins, 56))
        if ptr_eq(result, load_ptr(slots, 40)):
            store_i64(pins, output_offset, load_i64(pins, 40))
        if ptr_eq(result, load_ptr(slots, 32)):
            store_i64(pins, output_offset, load_i64(pins, 32))
        if ptr_eq(result, load_ptr(slots, 24)):
            store_i64(pins, output_offset, load_i64(pins, 24))
        if ptr_eq(result, load_ptr(slots, 48)):
            store_i64(pins, output_offset, load_i64(pins, 48))
    # The compiled entry owns its exception contract.  Check it before
    # releasing call temporaries so cleanup cannot mask a silent NULL return.
    if ptr_is_null(result):
        entry_name = load_ptr(load_ptr(slots, 64), 72)
        if ptr_is_null(entry_name):
            entry_name = cstr("<compiled native function>")
        _func_runtime_error_if_unset(
            entry_name,
            cstr("compiled native function returned NULL without exception")
        )
        _func_keep_call_error(slots, pins, 16)
    if owns_call_args != 0:
        _func_clear_call_root(slots, pins, 56)
    if ptr_is_null(sig) == 0:
        _func_clear_call_root(slots, pins, 32)
    if owns_actual_captures != 0:
        _func_clear_call_root(slots, pins, 40)
    if made_args != 0:
        _func_clear_call_root(slots, pins, 48)
    return


@c_abi_export("py_func_call")
def py_func_call(callable_obj, args_tuple):
    return py_func_call_kwargs(callable_obj, args_tuple, null())


def _partial_entry_build(slots, pins, has_kwargs: int) -> None:
    # slots: output0, captures8, call args16, function24, bound32,
    # kwargs40, full args48, current item56, preserved error64.
    fn = py_tuple_get(load_ptr(slots, 8), 0)
    store_ptr(slots, 24, fn)
    if ptr_is_null(fn) == 0 and is_tagged_int(fn) == 0:
        store_i64(pins, 24, load_i32(fn, 12) & 64)
        pcc_gc_pin(fn)
        pcc_gc_note_write_barrier(null(), fn)
    if ptr_is_null(fn):
        return
    bound = py_tuple_get(load_ptr(slots, 8), 1)
    store_ptr(slots, 32, bound)
    if ptr_is_null(bound) == 0 and is_tagged_int(bound) == 0:
        store_i64(pins, 32, load_i32(bound, 12) & 64)
        pcc_gc_pin(bound)
        pcc_gc_note_write_barrier(null(), bound)
    if ptr_is_null(bound):
        return
    if has_kwargs:
        kwargs = py_tuple_get(load_ptr(slots, 8), 2)
        store_ptr(slots, 40, kwargs)
        if ptr_is_null(kwargs) == 0 and is_tagged_int(kwargs) == 0:
            store_i64(pins, 40, load_i32(kwargs, 12) & 64)
            pcc_gc_pin(kwargs)
            pcc_gc_note_write_barrier(null(), kwargs)
        if ptr_is_null(kwargs):
            return
    nb: int = py_tuple_len(load_ptr(slots, 32))
    na: int = py_tuple_len(load_ptr(slots, 16))
    full = py_tuple_new(nb + na)
    store_ptr(slots, 48, full)
    if ptr_is_null(full) == 0 and is_tagged_int(full) == 0:
        store_i64(pins, 48, load_i32(full, 12) & 64)
        pcc_gc_pin(full)
        pcc_gc_note_write_barrier(null(), full)
    if ptr_is_null(full):
        return
    i: int = 0
    while i < nb + na:
        if i < nb:
            item = py_tuple_get(load_ptr(slots, 32), i)
        else:
            item = py_tuple_get(load_ptr(slots, 16), i - nb)
        store_ptr(slots, 56, item)
        if ptr_is_null(item) == 0 and is_tagged_int(item) == 0:
            store_i64(pins, 56, load_i32(item, 12) & 64)
            pcc_gc_pin(item)
            pcc_gc_note_write_barrier(null(), item)
        if ptr_is_null(item):
            return
        py_tuple_set_item(load_ptr(slots, 48), i, load_ptr(slots, 56))
        _func_clear_call_root(slots, pins, 56)
        i = i + 1
    result = py_obj_call_forward(load_ptr(slots, 24), load_ptr(slots, 48), load_ptr(slots, 40))
    store_ptr(slots, 0, result)
    if ptr_is_null(result) == 0 and is_tagged_int(result) == 0:
        store_i64(pins, 0, load_i32(result, 12) & 64)
        pcc_gc_pin(result)
        pcc_gc_note_write_barrier(null(), result)


def _partial_entry(captures, args, has_kwargs: int):
    slots = stack_alloc(88)
    pins = stack_alloc(88)
    memset(slots, 0, 88)
    memset(pins, 0, 88)
    pcc_gc_frame_enter(global_addr("pcc_native_callable_result_frame_map"), slots)
    if ptr_is_null(captures) == 0 and is_tagged_int(captures) == 0:
        store_i64(pins, 8, load_i32(captures, 12) & 64)
        pcc_gc_pin(captures)
    pcc_gc_store_root(ptr_add(slots, 8), captures)
    if ptr_is_null(args) == 0 and is_tagged_int(args) == 0:
        store_i64(pins, 16, load_i32(args, 12) & 64)
        pcc_gc_pin(args)
    pcc_gc_store_root(ptr_add(slots, 16), args)
    _partial_entry_build(slots, pins, has_kwargs)
    if ptr_is_null(load_ptr(slots, 0)):
        _func_keep_call_error(slots, pins, 64)
    prior_result_pin: int = load_i64(pins, 0)
    # Reverse acquisition order preserves an alias's external pin state.
    index: int = 7
    while index > 0:
        if ptr_eq(load_ptr(slots, 0), load_ptr(slots, index * 8)):
            prior_result_pin = load_i64(pins, index * 8)
        index = index - 1
    _func_clear_call_root(slots, pins, 56)
    _func_clear_call_root(slots, pins, 48)
    _func_clear_call_root(slots, pins, 40)
    _func_clear_call_root(slots, pins, 32)
    _func_clear_call_root(slots, pins, 24)
    _func_clear_call_root(slots, pins, 16)
    _func_clear_call_root(slots, pins, 8)
    error = load_ptr(slots, 64)
    if ptr_is_null(error) == 0:
        py_raise(error)
        _func_clear_call_root(slots, pins, 64)
    result = load_ptr(slots, 0)
    if ptr_is_null(result) == 0 and is_tagged_int(result) == 0:
        atomic_rmw_i32("or", result, 12, 64, "relaxed")
    pcc_gc_frame_leave(slots)
    return pcc_gc_take_pinned_slot(slots, prior_result_pin)


def _pcc_partial_entry(captures, args):
    return _partial_entry(captures, args, 0)


def _pcc_partial_kw_entry(captures, args):
    return _partial_entry(captures, args, 1)


@c_abi_export("py_functools_partial")
def py_functools_partial(fn, bound_args):
    if ptr_is_null(fn):
        return null()
    bound = bound_args
    made_bound: int = 0
    if ptr_is_null(bound):
        bound = py_tuple_new(0)
        made_bound = 1
    if ptr_is_null(bound):
        return null()
    captures = py_tuple_new(2)
    if ptr_is_null(captures):
        if made_bound != 0:
            py_decref(bound)
        return null()
    py_tuple_set_item(captures, 0, fn)
    py_tuple_set_item(captures, 1, bound)
    p = py_func_new_bound_raw(_pcc_partial_entry, captures, cstr("partial"), null())
    if ptr_is_null(p) == 0:
        atomic_rmw_i32("or", p, 12, PY_FLAG_FUNC_TRANSPARENT_CALL, "relaxed")
    py_decref(captures)
    if made_bound != 0:
        py_decref(bound)
    return p


@c_abi_export("py_functools_partial_kw")
def py_functools_partial_kw(fn, bound_args, bound_kwargs):
    if ptr_is_null(fn):
        return null()
    bound = bound_args
    made_bound: int = 0
    if ptr_is_null(bound):
        bound = py_tuple_new(0)
        made_bound = 1
    if ptr_is_null(bound):
        return null()
    kwargs = bound_kwargs
    made_kwargs: int = 0
    if ptr_is_null(kwargs):
        kwargs = py_dict_new()
        made_kwargs = 1
    if ptr_is_null(kwargs):
        if made_bound != 0:
            py_decref(bound)
        return null()
    captures = py_tuple_new(3)
    if ptr_is_null(captures):
        if made_bound != 0:
            py_decref(bound)
        if made_kwargs != 0:
            py_decref(kwargs)
        return null()
    py_tuple_set_item(captures, 0, fn)
    py_tuple_set_item(captures, 1, bound)
    py_tuple_set_item(captures, 2, kwargs)
    p = py_func_new_bound_raw(
        _pcc_partial_kw_entry,
        captures,
        cstr("partial"),
        null(),
    )
    if ptr_is_null(p) == 0:
        atomic_rmw_i32("or", p, 12, PY_FLAG_FUNC_TRANSPARENT_CALL, "relaxed")
    py_decref(captures)
    if made_bound != 0:
        py_decref(bound)
    if made_kwargs != 0:
        py_decref(kwargs)
    return p


def _update_wrapper_copy_attr(wrapper, wrapped, name) -> int:
    value = py_obj_getattr(wrapped, name)
    if ptr_is_null(value):
        py_clear_exception()
        return 0
    status: int = py_obj_setattr(wrapper, name, value)
    py_decref(value)
    return status


@c_abi_export("py_functools_update_wrapper")
def py_functools_update_wrapper(wrapper, wrapped):
    if ptr_is_null(wrapper) or ptr_is_null(wrapped):
        return null()
    if _update_wrapper_copy_attr(wrapper, wrapped, cstr("__module__")) != 0:
        return null()
    if _update_wrapper_copy_attr(wrapper, wrapped, cstr("__name__")) != 0:
        return null()
    if _update_wrapper_copy_attr(wrapper, wrapped, cstr("__qualname__")) != 0:
        return null()
    if _update_wrapper_copy_attr(wrapper, wrapped, cstr("__doc__")) != 0:
        return null()
    if _update_wrapper_copy_attr(wrapper, wrapped, cstr("__annotations__")) != 0:
        return null()
    if _update_wrapper_copy_attr(wrapper, wrapped, cstr("__type_params__")) != 0:
        return null()

    wrapper_dict = py_obj_getattr(wrapper, cstr("__dict__"))
    if ptr_is_null(wrapper_dict):
        py_clear_exception()
    wrapped_dict = py_obj_getattr(wrapped, cstr("__dict__"))
    if ptr_is_null(wrapped_dict):
        py_clear_exception()
    if ptr_is_null(wrapper_dict) == 0 and ptr_is_null(wrapped_dict) == 0:
        py_dict_update(wrapper_dict, wrapped_dict)
    py_decref(wrapper_dict)
    py_decref(wrapped_dict)

    if py_obj_setattr(wrapper, cstr("__wrapped__"), wrapped) != 0:
        return null()
    py_incref(wrapper)
    return wrapper


@c_abi_export("py_dealloc_func")
def py_dealloc_func(o) -> None:
    capi_self = pcc_gc_load_ptr(o, ptr_add(o, 24))
    capi_module = pcc_gc_load_ptr(o, ptr_add(o, 32))
    capi_weakreflist = pcc_gc_load_ptr(o, ptr_add(o, 40))
    captures = pcc_gc_load_ptr(o, ptr_add(o, 64))
    self_obj = pcc_gc_load_ptr(o, ptr_add(o, 80))
    attrs = pcc_gc_load_ptr(o, ptr_add(o, 88))
    if ptr_is_null(capi_self) == 0:
        py_decref(capi_self)
    if ptr_is_null(capi_module) == 0:
        py_decref(capi_module)
    if ptr_is_null(capi_weakreflist) == 0:
        py_decref(capi_weakreflist)
    py_decref(captures)
    if ptr_is_null(self_obj) == 0:
        py_decref(self_obj)
    if ptr_is_null(attrs) == 0:
        py_decref(attrs)
    pcc_gc_free_object_memory(o)
