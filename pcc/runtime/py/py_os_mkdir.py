"""Owned first-class os.mkdir for POSIX targets.

Filesystem strings use UTF-8/surrogateescape, matching the runtime's POSIX
filesystem encoding contract. Bytes pass unchanged; PathLike and integer
protocols use the shared rooted providers. Windows is an explicit capability
gap. OSError metadata uses the shared exception payload owner.
"""
__pcc_runtime_port__ = True

from pcc.extern import extern, c_abi_export, c_int32, c_int64, c_ptr, c_void
from pcc.runtime.py.py_abi_constants import (
    C_POINTER_SIZE, PYOBJECTHEADER_FLAGS_OFFSET, PYOBJECTHEADER_TYPE_TAG_OFFSET,
    PY_FLAG_GC_PINNED, PY_TYPE_BYTES, PYBYTESOBJECT_BYTE_LEN_OFFSET,
    PYBYTESOBJECT_DATA_OFFSET,
)
from pcc.unsafe import (
    atomic_cas_i64, atomic_load_i64, cstr, define_global_i32, define_global_i64,
    define_global_ptr_null, function_addr, global_addr, global_load_ptr,
    global_store_ptr, int_to_ptr, is_tagged_int, load_i8, load_i32, load_i64,
    load_ptr, memset, mkdir_at, null, ptr_add, ptr_eq, ptr_is_null, ptr_to_int,
    stack_alloc, store_i32, store_i64, store_ptr, strlen, target_sys_platform,
)

py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_obj_setattr = extern("py_obj_setattr", (c_ptr, c_ptr, c_ptr), c_int64)
py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
py_index_i64_checked_slots = extern("py_index_i64_checked_slots", (c_ptr,), c_int64)
py_file_fspath = extern("py_file_fspath", (c_ptr,), c_ptr)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_get = extern("py_tuple_get", (c_ptr, c_int64), c_ptr)
py_tuple_len = extern("py_tuple_len", (c_ptr,), c_int64)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_func_new_named = extern("py_func_new_named", (c_ptr, c_ptr, c_ptr), c_ptr)
py_tls_exc_swap_slot = extern("py_tls_exc_swap_slot", (c_ptr,), c_void)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
pcc_gc_unpin = extern("pcc_gc_unpin", (c_ptr,), c_void)
pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
pcc_gc_frame_leave = extern("pcc_gc_frame_leave", (c_ptr,), c_void)
pcc_gc_store_root = extern("pcc_gc_store_root", (c_ptr, c_ptr), c_void)
pcc_gc_take_pinned_slot = extern("pcc_gc_take_pinned_slot", (c_ptr, c_int64), c_ptr)
pcc_gc_note_write_barrier = extern("pcc_gc_note_write_barrier", (c_ptr, c_ptr), c_void)
pcc_gc_scheduler_root_register_handle = extern("pcc_gc_scheduler_root_register_handle", (c_ptr,), c_ptr)
pcc_gc_root_copy_lease = extern("pcc_gc_root_copy_lease", (c_ptr, c_ptr), c_int64)
pcc_gc_foreign_lease_release = extern("pcc_gc_foreign_lease_release", (c_ptr, c_int64), c_int64)
pcc_mutex_new = extern("pcc_mutex_new", (), c_ptr)
pcc_mutex_free = extern("pcc_mutex_free", (c_ptr,), c_void)
pcc_mutex_lock = extern("pcc_mutex_lock", (c_ptr,), c_int64)
pcc_mutex_unlock = extern("pcc_mutex_unlock", (c_ptr,), c_int64)
errno_message = extern("pcc_errno_message_into", (c_int32, c_ptr, c_int64), c_int32)
py_text_encode_ids = extern("py_text_encode_ids", (c_ptr, c_int64, c_int64), c_ptr)

# Named slots are shared by non-overlapping callback and factory lifetimes.
_MKDIR_SLOTS = 12
_MKDIR_SAVED_ERROR = 10
_MKDIR_OUTPUT = 11
_MKDIR_PATH = 0
_MKDIR_MODE = 1
_MKDIR_DIR_FD = 2
_MKDIR_NORMALIZED = 3
_MKDIR_ENCODED = 4
_MKDIR_EXCEPTION = 5
_MKDIR_ERRNO = 6
_MKDIR_MESSAGE = 7
_MKDIR_ARGS = 8
_MKDIR_CANDIDATE = 0
_MKDIR_NAMES = 1
_MKDIR_KINDS = 2
_MKDIR_PRESENT = 3
_MKDIR_DEFAULTS = 4
_MKDIR_ITEM = 5
_MKDIR_SIGNATURE = 6
_MKDIR_EMPTY = 7
_MKDIR_CAPTURES = 8

define_global_i32("pcc_mkdir_frame_map", _MKDIR_SLOTS)
define_global_i64("pcc_mkdir_mutex_bits", 0)
define_global_ptr_null("pcc_os_mkdir_function")
define_global_ptr_null("pcc_os_mkdir_root_handle")

def _mkdir_none():
    return global_load_ptr("py_None")

def _mkdir_error(tag: int, message):
    py_raise_owned(py_exc_new(tag, message))
    return null()

def _mkdir_hold(slots, pins, index: int, value) -> None:
    # A NEW return enters its registered slot before the next safepoint.
    offset: int = index * C_POINTER_SIZE
    if ptr_is_null(value) and not py_err_occurred():
        _mkdir_error(19, cstr("os.mkdir owned object allocation failed"))
    store_ptr(slots, offset, value)
    if not ptr_is_null(value) and not is_tagged_int(value):
        store_i64(pins, offset, load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & PY_FLAG_GC_PINNED)
        pcc_gc_pin(value)
        pcc_gc_note_write_barrier(null(), value)

def _mkdir_drop(slots, pins, index: int) -> None:
    offset: int = index * C_POINTER_SIZE
    value = load_ptr(slots, offset)
    if not ptr_is_null(value) and not is_tagged_int(value):
        pcc_gc_unpin(value)
        keep_result: int = ptr_eq(value, load_ptr(slots, _MKDIR_OUTPUT * C_POINTER_SIZE))
        if keep_result and not load_i64(pins, offset):
            store_i64(pins, _MKDIR_OUTPUT * C_POINTER_SIZE, 0)
        keep_error: int = ptr_eq(value, load_ptr(slots, _MKDIR_SAVED_ERROR * C_POINTER_SIZE))
        if keep_error and not load_i64(pins, offset):
            store_i64(pins, _MKDIR_SAVED_ERROR * C_POINTER_SIZE, 0)
        if load_i64(pins, offset) or keep_result or keep_error:
            store_i32(value, PYOBJECTHEADER_FLAGS_OFFSET,
                      load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) | PY_FLAG_GC_PINNED)
    pcc_gc_store_root(ptr_add(slots, offset), null())
    store_i64(pins, offset, 0)

def _mkdir_finish(slots, pins):
    # Retiring file/callable owners may execute user finalizers. Preserve the
    # exception which belongs to this operation until all other owners retire.
    saved_slot = ptr_add(slots, _MKDIR_SAVED_ERROR * C_POINTER_SIZE)
    py_tls_exc_swap_slot(saved_slot)
    saved = load_ptr(saved_slot, 0)
    if not ptr_is_null(saved):
        _mkdir_hold(slots, pins, _MKDIR_SAVED_ERROR, saved)
    index: int = _MKDIR_SAVED_ERROR
    while index > 0:
        index = index - 1
        _mkdir_drop(slots, pins, index)
    py_clear_exception()
    saved = load_ptr(saved_slot, 0)
    if not ptr_is_null(saved):
        pcc_gc_unpin(saved)
        if load_i64(pins, _MKDIR_SAVED_ERROR * C_POINTER_SIZE):
            store_i32(saved, PYOBJECTHEADER_FLAGS_OFFSET,
                      load_i32(saved, PYOBJECTHEADER_FLAGS_OFFSET) | PY_FLAG_GC_PINNED)
    py_tls_exc_swap_slot(saved_slot)
    pcc_gc_frame_leave(slots)
    return pcc_gc_take_pinned_slot(ptr_add(slots, _MKDIR_OUTPUT * C_POINTER_SIZE),
                                   load_i64(pins, _MKDIR_OUTPUT * C_POINTER_SIZE))

def _mkdir_mutex():
    slot = global_addr("pcc_mkdir_mutex_bits")
    bits: int = atomic_load_i64(slot, 0, "acquire")
    if bits != 0:
        return int_to_ptr(bits)
    candidate = pcc_mutex_new()
    if ptr_is_null(candidate):
        _mkdir_error(19, cstr("os.mkdir registry allocation failed"))
        return null()
    previous: int = atomic_cas_i64(slot, 0, 0, ptr_to_int(candidate), "acq_rel", "acquire")
    if previous != 0:
        pcc_mutex_free(candidate)
        return int_to_ptr(previous)
    return candidate

def _mkdir_copy_cached_owner(slots, pins, source) -> None:
    output = ptr_add(slots, _MKDIR_OUTPUT * C_POINTER_SIZE)
    token: int = pcc_gc_root_copy_lease(output, source)
    if token < 0:
        _mkdir_error(7, cstr("cached owner copy failed"))
        return
    # The counted address lease is independent of the legacy pin flag, and
    # protects this load until the return pin has been established.
    _mkdir_hold(slots, pins, _MKDIR_OUTPUT, load_ptr(output, 0))
    if pcc_gc_foreign_lease_release(output, token) < 0:
        _mkdir_error(7, cstr("cached owner lease release failed"))


def _mkdir_signature(slots, pins):
    index: int = _MKDIR_NAMES
    while index <= _MKDIR_DEFAULTS:
        _mkdir_hold(slots, pins, index, py_tuple_new(3))
        if py_err_occurred():
            return null()
        index = index + 1
    index = 0
    while index < 3 and not py_err_occurred():
        name = cstr("path")
        if index == _MKDIR_MODE:
            name = cstr("mode")
        elif index == _MKDIR_DIR_FD:
            name = cstr("dir_fd")
        _mkdir_hold(slots, pins, _MKDIR_ITEM, py_str_new(name, strlen(name)))
        if py_err_occurred():
            return null()
        py_tuple_set_item(load_ptr(slots, _MKDIR_NAMES * C_POINTER_SIZE), index,
                          load_ptr(slots, _MKDIR_ITEM * C_POINTER_SIZE))
        _mkdir_drop(slots, pins, _MKDIR_ITEM)
        _mkdir_hold(slots, pins, _MKDIR_ITEM,
                    py_int_from_i64(2 if index == _MKDIR_DIR_FD else 0))
        if py_err_occurred():
            return null()
        py_tuple_set_item(load_ptr(slots, _MKDIR_KINDS * C_POINTER_SIZE), index,
                          load_ptr(slots, _MKDIR_ITEM * C_POINTER_SIZE))
        _mkdir_drop(slots, pins, _MKDIR_ITEM)
        py_tuple_set_item(load_ptr(slots, _MKDIR_PRESENT * C_POINTER_SIZE), index,
            global_load_ptr("py_False") if index == _MKDIR_PATH else global_load_ptr("py_True"))
        default = _mkdir_none()
        if index == _MKDIR_MODE:
            _mkdir_hold(slots, pins, _MKDIR_ITEM, py_int_from_i64(511))
            if py_err_occurred():
                return null()
            default = load_ptr(slots, _MKDIR_ITEM * C_POINTER_SIZE)
        py_tuple_set_item(load_ptr(slots, _MKDIR_DEFAULTS * C_POINTER_SIZE), index, default)
        _mkdir_drop(slots, pins, _MKDIR_ITEM)
        index = index + 1
    if py_err_occurred():
        return null()
    _mkdir_hold(slots, pins, _MKDIR_SIGNATURE, py_tuple_new(5))
    if py_err_occurred():
        return null()
    _mkdir_hold(slots, pins, _MKDIR_ITEM, py_str_new(cstr("__pcc_func_signature_v1__"), 25))
    if py_err_occurred():
        return null()
    py_tuple_set_item(load_ptr(slots, _MKDIR_SIGNATURE * C_POINTER_SIZE), 0,
                      load_ptr(slots, _MKDIR_ITEM * C_POINTER_SIZE))
    index = _MKDIR_NAMES
    while index <= _MKDIR_DEFAULTS:
        py_tuple_set_item(load_ptr(slots, _MKDIR_SIGNATURE * C_POINTER_SIZE), index,
                          load_ptr(slots, index * C_POINTER_SIZE))
        index = index + 1
    _mkdir_hold(slots, pins, _MKDIR_EMPTY, py_tuple_new(0))
    if py_err_occurred():
        return null()
    _mkdir_hold(slots, pins, _MKDIR_CAPTURES, py_tuple_new(2))
    if py_err_occurred():
        return null()
    py_tuple_set_item(load_ptr(slots, _MKDIR_CAPTURES * C_POINTER_SIZE), 0,
                      load_ptr(slots, _MKDIR_EMPTY * C_POINTER_SIZE))
    py_tuple_set_item(load_ptr(slots, _MKDIR_CAPTURES * C_POINTER_SIZE), 1,
                      load_ptr(slots, _MKDIR_SIGNATURE * C_POINTER_SIZE))
    return load_ptr(slots, _MKDIR_CAPTURES * C_POINTER_SIZE)


@c_abi_export("py_os_mkdir_function")
def py_os_mkdir_function():
    slots = stack_alloc(_MKDIR_SLOTS * C_POINTER_SIZE)
    pins = stack_alloc(_MKDIR_SLOTS * C_POINTER_SIZE)
    memset(slots, 0, _MKDIR_SLOTS * C_POINTER_SIZE)
    memset(pins, 0, _MKDIR_SLOTS * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_mkdir_frame_map"), slots)
    mutex = _mkdir_mutex()
    if ptr_is_null(mutex):
        return _mkdir_finish(slots, pins)
    source = global_addr("pcc_os_mkdir_function")
    pcc_mutex_lock(mutex)
    if not ptr_is_null(load_ptr(source, 0)):
        pcc_mutex_unlock(mutex)
        _mkdir_copy_cached_owner(slots, pins, source)
        return _mkdir_finish(slots, pins)
    pcc_mutex_unlock(mutex)
    captures = _mkdir_signature(slots, pins)
    if not py_err_occurred():
        _mkdir_hold(slots, pins, _MKDIR_CANDIDATE, py_func_new_named(
            function_addr("pcc_os_mkdir_entry"), captures, cstr("mkdir")))
    root_failed: int = 0
    pcc_mutex_lock(mutex)
    if not py_err_occurred() and not ptr_is_null(
            load_ptr(slots, _MKDIR_CANDIDATE * C_POINTER_SIZE)):
        if ptr_is_null(load_ptr(source, 0)):
            handle = pcc_gc_scheduler_root_register_handle(source)
            if ptr_is_null(handle):
                root_failed = 1
            else:
                global_store_ptr("pcc_os_mkdir_root_handle", handle)
                pcc_gc_store_root(source,
                    load_ptr(slots, _MKDIR_CANDIDATE * C_POINTER_SIZE))
    pcc_mutex_unlock(mutex)
    if root_failed:
        _mkdir_error(19, cstr("mkdir callable root registration failed"))
    if not py_err_occurred():
        _mkdir_copy_cached_owner(slots, pins, source)
    return _mkdir_finish(slots, pins)


def _mkdir_c_int(slots, index: int) -> int:
    value: int = py_index_i64_checked_slots(ptr_add(slots, index * C_POINTER_SIZE))
    if not py_err_occurred() and (value < -2147483648 or value > 2147483647):
        _mkdir_error(15, cstr("Python int too large to convert to C int"))
    return value


def _mkdir_os_error(slots, pins, status: int):
    tag: int = 14
    if status == -2:
        tag = 34
    elif status == -17:
        tag = 35
    elif status == -13 or status == -1:
        tag = 36
    elif status == -20:
        tag = 38
    message = stack_alloc(256)
    errno_message(0 - status, message, 256)
    _mkdir_hold(slots, pins, _MKDIR_EXCEPTION, py_exc_new(tag, message))
    if py_err_occurred():
        return
    _mkdir_hold(slots, pins, _MKDIR_ERRNO, py_int_from_i64(0 - status))
    if py_err_occurred():
        return
    _mkdir_hold(slots, pins, _MKDIR_MESSAGE, py_str_new(message, strlen(message)))
    if py_err_occurred():
        return
    _mkdir_hold(slots, pins, _MKDIR_ARGS, py_tuple_new(2))
    if not py_err_occurred():
        py_tuple_set_item(load_ptr(slots, _MKDIR_ARGS * C_POINTER_SIZE), 0,
                          load_ptr(slots, _MKDIR_ERRNO * C_POINTER_SIZE))
        py_tuple_set_item(load_ptr(slots, _MKDIR_ARGS * C_POINTER_SIZE), 1,
                          load_ptr(slots, _MKDIR_MESSAGE * C_POINTER_SIZE))
        exc = load_ptr(slots, _MKDIR_EXCEPTION * C_POINTER_SIZE)
        if py_obj_setattr(exc, cstr("errno"), load_ptr(slots, _MKDIR_ERRNO * C_POINTER_SIZE)) != 0:
            return
        if py_obj_setattr(exc, cstr("strerror"), load_ptr(slots, _MKDIR_MESSAGE * C_POINTER_SIZE)) != 0:
            return
        if py_obj_setattr(exc, cstr("filename"), load_ptr(slots, _MKDIR_NORMALIZED * C_POINTER_SIZE)) != 0:
            return
        if py_obj_setattr(exc, cstr("args"), load_ptr(slots, _MKDIR_ARGS * C_POINTER_SIZE)) != 0:
            return
        if not py_err_occurred():
            py_incref(exc)
            py_raise_owned(exc)


@c_abi_export("pcc_os_mkdir_entry")
def _mkdir_entry(captures, args):
    if py_tuple_len(args) != 3:
        return _mkdir_error(3, cstr("os.mkdir expects three bound arguments"))
    if load_i8(target_sys_platform(), 0) == 119:
        return _mkdir_error(11, cstr("native os.mkdir requires an owned Windows provider"))
    slots = stack_alloc(_MKDIR_SLOTS * C_POINTER_SIZE)
    pins = stack_alloc(_MKDIR_SLOTS * C_POINTER_SIZE)
    memset(slots, 0, _MKDIR_SLOTS * C_POINTER_SIZE)
    memset(pins, 0, _MKDIR_SLOTS * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_mkdir_frame_map"), slots)
    index: int = _MKDIR_PATH
    while index <= _MKDIR_DIR_FD:
        _mkdir_hold(slots, pins, index, py_tuple_get(args, index))
        index = index + 1
    if not py_err_occurred():
        _mkdir_hold(slots, pins, _MKDIR_NORMALIZED,
                    py_file_fspath(load_ptr(slots, _MKDIR_PATH * C_POINTER_SIZE)))
    if not py_err_occurred():
        path = load_ptr(slots, _MKDIR_NORMALIZED * C_POINTER_SIZE)
        if load_i32(path, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_BYTES:
            py_incref(path)
            _mkdir_hold(slots, pins, _MKDIR_ENCODED, path)
        else:
            _mkdir_hold(slots, pins, _MKDIR_ENCODED, py_text_encode_ids(path, 0, 3))
    # Path conversion, including encoding and embedded NUL rejection, precedes
    # either integer callback, just as the public POSIX argument parser does.
    if not py_err_occurred():
        encoded = load_ptr(slots, _MKDIR_ENCODED * C_POINTER_SIZE)
        data = ptr_add(encoded, PYBYTESOBJECT_DATA_OFFSET)
        length: int = load_i64(encoded, PYBYTESOBJECT_BYTE_LEN_OFFSET)
        index = 0
        while index < length:
            if load_i8(data, index) == 0:
                _mkdir_error(2, cstr("embedded null character"))
                break
            index = index + 1
    permissions: int = 0
    directory: int = -2 if load_i8(target_sys_platform(), 0) == 100 else -100
    if not py_err_occurred():
        permissions = _mkdir_c_int(slots, _MKDIR_MODE)
    if not py_err_occurred() and not ptr_eq(
            load_ptr(slots, _MKDIR_DIR_FD * C_POINTER_SIZE), _mkdir_none()):
        directory = _mkdir_c_int(slots, _MKDIR_DIR_FD)
    if not py_err_occurred():
        # Re-load after arbitrary __index__ callbacks. The encoded bytes owner
        # stays rooted and pinned until the synchronous platform call returns.
        data = ptr_add(load_ptr(slots, _MKDIR_ENCODED * C_POINTER_SIZE), PYBYTESOBJECT_DATA_OFFSET)
        status: int = mkdir_at(data, permissions, directory)
        while status == -4:
            status = mkdir_at(data, permissions, directory)
        if status < 0:
            _mkdir_os_error(slots, pins, status)
        else:
            py_incref(_mkdir_none())
            _mkdir_hold(slots, pins, _MKDIR_OUTPUT, _mkdir_none())
    return _mkdir_finish(slots, pins)
