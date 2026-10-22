"""Owned filesystem transactions for a native os.walk generator.

The public Python generator calls the c_obj adapters below; its compiler frame
owns suspended traversal state. The native portion owns one synchronous scan.
Inputs and outputs are authoritative registered caller roots. Raw directory
streams never survive a return; only managed paths, names and lists escape.
"""

__pcc_runtime_port__ = True

from pcc.extern import (
    c_abi_export, c_int32, c_int64, c_ptr, c_void, extern,
)
from pcc.runtime.py.py_abi_constants import (
    C_POINTER_SIZE, PYBYTESOBJECT_BYTE_LEN_OFFSET, PYBYTESOBJECT_DATA_OFFSET,
    PYOBJECTHEADER_FLAGS_OFFSET, PY_FLAG_GC_PINNED,
    PYOBJECTHEADER_TYPE_TAG_OFFSET, PY_TYPE_BYTES,
)
from pcc.unsafe import (
    cstr, define_global_i32, directory_close, directory_entry_type, directory_error, directory_next, directory_open,
    global_addr,
    is_symlink, is_tagged_int, load_i8, load_i32, load_i64, load_ptr, memset,
    null, ptr_add, ptr_eq, ptr_is_null, stack_alloc, stat_kind, lstat_kind, store_i64,
    store_ptr, strlen,
)

py_err_occurred = extern("py_err_occurred", (), c_int64)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_tls_exc_swap_slot = extern("py_tls_exc_swap_slot", (c_ptr,), c_void)
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_incref = extern("py_incref", (c_ptr,), c_void)
py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_str_check = extern("py_str_check", (c_ptr,), c_int64)
py_text_encode_ids = extern("py_text_encode_ids", (c_ptr, c_int64, c_int64), c_ptr)
py_bytes_decode_utf8_surrogateescape = extern("py_bytes_decode_utf8_surrogateescape", (c_ptr,), c_ptr)
py_bytes_new = extern("py_bytes_new", (c_ptr, c_int64), c_ptr)
py_bytes_concat = extern("py_bytes_concat", (c_ptr, c_ptr), c_ptr)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_list_new = extern("py_list_new", (c_int64,), c_ptr)
py_list_append = extern("py_list_append", (c_ptr, c_ptr), c_void)
py_obj_truthy = extern("py_obj_truthy", (c_ptr,), c_int64)
py_obj_setattr = extern("py_obj_setattr", (c_ptr, c_ptr, c_ptr), c_int64)
py_os_fsencode_slots = extern("py_os_fsencode_slots", (c_ptr, c_ptr), c_int64)
py_os_path_join = extern("py_os_path_join", (c_ptr,), c_ptr)
pcc_errno_get = extern("pcc_errno_get", (), c_int32)
pcc_errno_exception_kind = extern("pcc_errno_exception_kind", (c_int32,), c_int64)
pcc_errno_message_into = extern("pcc_errno_message_into", (c_int32, c_ptr, c_int64), c_int32)
pcc_gc_scheduler_root_register_handle = extern("pcc_gc_scheduler_root_register_handle", (c_ptr,), c_ptr)
pcc_gc_scheduler_root_unregister_handle = extern("pcc_gc_scheduler_root_unregister_handle", (c_ptr,), c_void)
pcc_gc_root_copy_lease = extern("pcc_gc_root_copy_lease", (c_ptr, c_ptr), c_int64)
pcc_gc_root_move = extern("pcc_gc_root_move", (c_ptr, c_ptr), c_int64)
pcc_gc_foreign_lease_acquire = extern("pcc_gc_foreign_lease_acquire", (c_ptr,), c_int64)
pcc_gc_foreign_lease_release = extern("pcc_gc_foreign_lease_release", (c_ptr, c_int64), c_int64)
pcc_gc_store_root = extern("pcc_gc_store_root", (c_ptr, c_ptr), c_void)
pcc_gc_note_slot_write_barrier = extern("pcc_gc_note_slot_write_barrier", (c_ptr, c_ptr, c_ptr), c_void)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
pcc_gc_frame_leave = extern("pcc_gc_frame_leave", (c_ptr,), c_void)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
pcc_gc_take_pinned_slot = extern("pcc_gc_take_pinned_slot", (c_ptr, c_int64), c_ptr)
pcc_platform_abort = extern("pcc_platform_abort", (), c_void)

# Each index names one registered owning slot and its counted address lease.
_WALK_PATH = 0
_WALK_TOPDOWN = 1
_WALK_FOLLOWLINKS = 2
_WALK_PATH_BYTES = 3
_WALK_PATH_TEXT = 4
_WALK_EMPTY = 5
_WALK_JOIN_ARGS = 6
_WALK_PREFIX_TEXT = 7
_WALK_PREFIX_BYTES = 8
_WALK_DIRECTORIES = 9
_WALK_FILES = 10
_WALK_CHILDREN = 11
_WALK_ENTRY_BYTES = 12
_WALK_ENTRY_NAME = 13
_WALK_CHILD_BYTES = 14
_WALK_CHILD_NAME = 15
_WALK_RESULT = 16
_WALK_ERROR_OBJECT = 17
_WALK_ERROR_NUMBER = 18
_WALK_ERROR_TEXT = 19
_WALK_ERROR_ARGS = 20
_WALK_ENTRY_EXCEPTION = 21
_WALK_EXIT_EXCEPTION = 22
_WALK_SLOT_COUNT = 23

define_global_i32("pcc_walk_ffi_frame_map", _WALK_SLOT_COUNT)


def _walk_fail(kind: int, message) -> int:
    if not py_err_occurred():
        py_raise_owned(py_exc_new(kind, message))
    return -1


def _walk_open(slots, tokens, handles) -> int:
    memset(slots, 0, _WALK_SLOT_COUNT * C_POINTER_SIZE)
    memset(tokens, 0, _WALK_SLOT_COUNT * C_POINTER_SIZE)
    memset(handles, 0, _WALK_SLOT_COUNT * C_POINTER_SIZE)
    count: int = 0
    while count < _WALK_SLOT_COUNT:
        handle = pcc_gc_scheduler_root_register_handle(ptr_add(slots, count * C_POINTER_SIZE))
        if ptr_is_null(handle):
            return count
        store_ptr(handles, count * C_POINTER_SIZE, handle)
        count = count + 1
    return count


def _walk_copy(slots, tokens, index: int, source) -> int:
    token: int = pcc_gc_root_copy_lease(ptr_add(slots, index * C_POINTER_SIZE), source)
    if token < 0:
        return _walk_fail(7, cstr("walk could not copy an authoritative owner"))
    store_i64(tokens, index * C_POINTER_SIZE, token)
    return 0


def _walk_adopt(slots, tokens, index: int) -> int:
    slot = ptr_add(slots, index * C_POINTER_SIZE)
    if ptr_is_null(load_ptr(slot, 0)):
        return _walk_fail(19, cstr("walk allocation returned no owner"))
    token: int = pcc_gc_foreign_lease_acquire(slot)
    if token < 0:
        return _walk_fail(7, cstr("walk could not lease a produced owner"))
    store_i64(tokens, index * C_POINTER_SIZE, token)
    pcc_py_gc_minor_graph_lock()
    pcc_gc_note_slot_write_barrier(null(), slot, load_ptr(slot, 0))
    pcc_py_gc_minor_graph_unlock()
    return -1 if py_err_occurred() else 0


def _walk_drop(slots, tokens, index: int) -> None:
    slot = ptr_add(slots, index * C_POINTER_SIZE)
    token: int = load_i64(tokens, index * C_POINTER_SIZE)
    if pcc_gc_foreign_lease_release(slot, token) != 0:
        pcc_platform_abort()
        return
    store_i64(tokens, index * C_POINTER_SIZE, 0)
    pcc_gc_store_root(slot, null())


def _walk_close(slots, tokens, handles, count: int, suspended: int, keep_result: int = 0) -> None:
    if suspended:
        py_tls_exc_swap_slot(ptr_add(slots, _WALK_EXIT_EXCEPTION * C_POINTER_SIZE))
        index: int = _WALK_ERROR_ARGS
        while index >= _WALK_PATH:
            if not keep_result or index != _WALK_RESULT:
                _walk_drop(slots, tokens, index)
            index = index - 1
        py_clear_exception()
        if not ptr_is_null(load_ptr(slots, _WALK_EXIT_EXCEPTION * C_POINTER_SIZE)):
            pcc_gc_store_root(ptr_add(slots, _WALK_ENTRY_EXCEPTION * C_POINTER_SIZE), null())
            py_clear_exception()
            py_tls_exc_swap_slot(ptr_add(slots, _WALK_EXIT_EXCEPTION * C_POINTER_SIZE))
        else:
            py_tls_exc_swap_slot(ptr_add(slots, _WALK_ENTRY_EXCEPTION * C_POINTER_SIZE))
    if not keep_result:
        _walk_unregister(handles, count)


def _walk_unregister(handles, count: int) -> None:
    index: int = 0
    while index < count:
        pcc_gc_scheduler_root_unregister_handle(load_ptr(handles, index * C_POINTER_SIZE))
        index = index + 1


def _walk_publish(slots, tokens, output) -> int:
    result = ptr_add(slots, _WALK_RESULT * C_POINTER_SIZE)
    if pcc_gc_root_move(output, result) != 0:
        return _walk_fail(7, cstr("walk could not publish its result owner"))
    token: int = load_i64(tokens, _WALK_RESULT * C_POINTER_SIZE)
    store_i64(tokens, _WALK_RESULT * C_POINTER_SIZE, 0)
    if pcc_gc_foreign_lease_release(output, token) != 0:
        pcc_platform_abort()
        return -1
    return 0


def _walk_bytes_path(slots) -> int:
    path = load_ptr(slots, _WALK_PATH * C_POINTER_SIZE)
    if ptr_is_null(path) or is_tagged_int(path):
        return 0
    return load_i32(path, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_BYTES


def _walk_prepare_path(slots, tokens) -> int:
    if py_os_fsencode_slots(ptr_add(slots, _WALK_PATH * C_POINTER_SIZE),
                            ptr_add(slots, _WALK_PATH_BYTES * C_POINTER_SIZE)) != 0:
        return -1
    if _walk_adopt(slots, tokens, _WALK_PATH_BYTES) != 0:
        return -1
    raw_owner = load_ptr(slots, _WALK_PATH_BYTES * C_POINTER_SIZE)
    length: int = load_i64(raw_owner, PYBYTESOBJECT_BYTE_LEN_OFFSET)
    raw = ptr_add(raw_owner, PYBYTESOBJECT_DATA_OFFSET)
    index: int = 0
    while index < length:
        if load_i8(raw, index) == 0:
            return _walk_fail(2, cstr("embedded null character"))
        index = index + 1
    # Decode only for the existing target-aware path join. The filesystem
    # name round trip is UTF-8/surrogateescape; bytes results remain bytes.
    store_ptr(slots, _WALK_PATH_TEXT * C_POINTER_SIZE,
              py_bytes_decode_utf8_surrogateescape(load_ptr(slots, _WALK_PATH_BYTES * C_POINTER_SIZE)))
    if _walk_adopt(slots, tokens, _WALK_PATH_TEXT) != 0:
        return -1
    store_ptr(slots, _WALK_EMPTY * C_POINTER_SIZE, py_str_new(cstr(""), 0))
    if _walk_adopt(slots, tokens, _WALK_EMPTY) != 0:
        return -1
    store_ptr(slots, _WALK_JOIN_ARGS * C_POINTER_SIZE, py_tuple_new(2))
    if _walk_adopt(slots, tokens, _WALK_JOIN_ARGS) != 0:
        return -1
    py_tuple_set_item(load_ptr(slots, _WALK_JOIN_ARGS * C_POINTER_SIZE), 0,
                      load_ptr(slots, _WALK_PATH_TEXT * C_POINTER_SIZE))
    py_tuple_set_item(load_ptr(slots, _WALK_JOIN_ARGS * C_POINTER_SIZE), 1,
                      load_ptr(slots, _WALK_EMPTY * C_POINTER_SIZE))
    if py_err_occurred():
        return -1
    store_ptr(slots, _WALK_PREFIX_TEXT * C_POINTER_SIZE,
              py_os_path_join(load_ptr(slots, _WALK_JOIN_ARGS * C_POINTER_SIZE)))
    if _walk_adopt(slots, tokens, _WALK_PREFIX_TEXT) != 0:
        return -1
    store_ptr(slots, _WALK_PREFIX_BYTES * C_POINTER_SIZE,
              py_text_encode_ids(load_ptr(slots, _WALK_PREFIX_TEXT * C_POINTER_SIZE), 0, 3))
    return _walk_adopt(slots, tokens, _WALK_PREFIX_BYTES)


def _walk_os_error(slots, tokens, number: int) -> int:
    kind: int = pcc_errno_exception_kind(number)
    message = stack_alloc(256)
    pcc_errno_message_into(number, message, 256)
    store_ptr(slots, _WALK_ERROR_OBJECT * C_POINTER_SIZE, py_exc_new(kind, message))
    if _walk_adopt(slots, tokens, _WALK_ERROR_OBJECT) != 0:
        return -1
    store_ptr(slots, _WALK_ERROR_NUMBER * C_POINTER_SIZE, py_int_from_i64(number))
    if _walk_adopt(slots, tokens, _WALK_ERROR_NUMBER) != 0:
        return -1
    store_ptr(slots, _WALK_ERROR_TEXT * C_POINTER_SIZE, py_str_new(message, strlen(message)))
    if _walk_adopt(slots, tokens, _WALK_ERROR_TEXT) != 0:
        return -1
    store_ptr(slots, _WALK_ERROR_ARGS * C_POINTER_SIZE, py_tuple_new(2))
    if _walk_adopt(slots, tokens, _WALK_ERROR_ARGS) != 0:
        return -1
    py_tuple_set_item(load_ptr(slots, _WALK_ERROR_ARGS * C_POINTER_SIZE), 0,
                      load_ptr(slots, _WALK_ERROR_NUMBER * C_POINTER_SIZE))
    py_tuple_set_item(load_ptr(slots, _WALK_ERROR_ARGS * C_POINTER_SIZE), 1,
                      load_ptr(slots, _WALK_ERROR_TEXT * C_POINTER_SIZE))
    if not py_err_occurred():
        py_obj_setattr(load_ptr(slots, _WALK_ERROR_OBJECT * C_POINTER_SIZE), cstr("errno"),
                       load_ptr(slots, _WALK_ERROR_NUMBER * C_POINTER_SIZE))
    if not py_err_occurred():
        py_obj_setattr(load_ptr(slots, _WALK_ERROR_OBJECT * C_POINTER_SIZE), cstr("strerror"),
                       load_ptr(slots, _WALK_ERROR_TEXT * C_POINTER_SIZE))
    if not py_err_occurred():
        py_obj_setattr(load_ptr(slots, _WALK_ERROR_OBJECT * C_POINTER_SIZE), cstr("filename"),
                       load_ptr(slots, _WALK_PATH * C_POINTER_SIZE))
    if not py_err_occurred():
        py_obj_setattr(load_ptr(slots, _WALK_ERROR_OBJECT * C_POINTER_SIZE), cstr("args"),
                       load_ptr(slots, _WALK_ERROR_ARGS * C_POINTER_SIZE))
    if not py_err_occurred():
        error = load_ptr(slots, _WALK_ERROR_OBJECT * C_POINTER_SIZE)
        py_incref(error)
        py_raise_owned(error)
    return -1


def _walk_entry(slots, tokens, entry, bytes_path: int, entry_type: int) -> int:
    store_ptr(slots, _WALK_ENTRY_BYTES * C_POINTER_SIZE, py_bytes_new(entry, strlen(entry)))
    if _walk_adopt(slots, tokens, _WALK_ENTRY_BYTES) != 0:
        return -1
    if bytes_path:
        if _walk_copy(slots, tokens, _WALK_ENTRY_NAME, ptr_add(slots, _WALK_ENTRY_BYTES * C_POINTER_SIZE)) != 0:
            return -1
    else:
        store_ptr(slots, _WALK_ENTRY_NAME * C_POINTER_SIZE,
                  py_bytes_decode_utf8_surrogateescape(load_ptr(slots, _WALK_ENTRY_BYTES * C_POINTER_SIZE)))
        if _walk_adopt(slots, tokens, _WALK_ENTRY_NAME) != 0:
            return -1
    store_ptr(slots, _WALK_CHILD_BYTES * C_POINTER_SIZE,
              py_bytes_concat(load_ptr(slots, _WALK_PREFIX_BYTES * C_POINTER_SIZE),
                              load_ptr(slots, _WALK_ENTRY_BYTES * C_POINTER_SIZE)))
    if _walk_adopt(slots, tokens, _WALK_CHILD_BYTES) != 0:
        return -1
    child = ptr_add(load_ptr(slots, _WALK_CHILD_BYTES * C_POINTER_SIZE), PYBYTESOBJECT_DATA_OFFSET)
    # scandir carries a snapshot of d_type. Do not re-stat known ordinary
    # records after an earlier truth callback has changed the filesystem.
    # An unknown record obtains one no-follow snapshot before callbacks.
    # Like DirEntry's cached lstat, this preserves both the directory kind
    # and link flag if a truth method replaces this path during the scan.
    if entry_type == 0:
        entry_type = lstat_kind(child)
    is_directory: int = entry_type == 2
    if entry_type == 3:
        is_directory = stat_kind(child) == 2
    py_list_append(load_ptr(slots, (_WALK_DIRECTORIES if is_directory else _WALK_FILES) * C_POINTER_SIZE),
                   load_ptr(slots, _WALK_ENTRY_NAME * C_POINTER_SIZE))
    if py_err_occurred():
        return -1
    topdown: int = py_obj_truthy(load_ptr(slots, _WALK_TOPDOWN * C_POINTER_SIZE))
    if py_err_occurred():
        return -1
    if not topdown and is_directory:
        follow: int = py_obj_truthy(load_ptr(slots, _WALK_FOLLOWLINKS * C_POINTER_SIZE))
        if py_err_occurred():
            return -1
        symlink: int = entry_type == 3
        if follow or not symlink:
            if bytes_path:
                if _walk_copy(slots, tokens, _WALK_CHILD_NAME, ptr_add(slots, _WALK_CHILD_BYTES * C_POINTER_SIZE)) != 0:
                    return -1
            else:
                store_ptr(slots, _WALK_CHILD_NAME * C_POINTER_SIZE,
                          py_bytes_decode_utf8_surrogateescape(load_ptr(slots, _WALK_CHILD_BYTES * C_POINTER_SIZE)))
                if _walk_adopt(slots, tokens, _WALK_CHILD_NAME) != 0:
                    return -1
            py_list_append(load_ptr(slots, _WALK_CHILDREN * C_POINTER_SIZE),
                           load_ptr(slots, _WALK_CHILD_NAME * C_POINTER_SIZE))
    return -1 if py_err_occurred() else 0


def _walk_scan(slots, tokens) -> int:
    if _walk_prepare_path(slots, tokens) != 0:
        return -1
    index: int = _WALK_DIRECTORIES
    while index <= _WALK_CHILDREN:
        store_ptr(slots, index * C_POINTER_SIZE, py_list_new(0))
        if _walk_adopt(slots, tokens, index) != 0:
            return -1
        index = index + 1
    raw = ptr_add(load_ptr(slots, _WALK_PATH_BYTES * C_POINTER_SIZE), PYBYTESOBJECT_DATA_OFFSET)
    stream = directory_open(raw)
    if ptr_is_null(stream):
        number: int = pcc_errno_get()
        if number <= 0:
            return _walk_fail(11, cstr("directory provider did not preserve open errno"))
        return _walk_os_error(slots, tokens, number)
    status: int = 0
    filesystem_error: int = 0
    bytes_path: int = _walk_bytes_path(slots)
    while status == 0:
        entry = directory_next(stream)
        if ptr_is_null(entry):
            filesystem_error = directory_error(stream)
            break
        if load_i8(entry, 0) == 46:
            if load_i8(entry, 1) == 0 or (load_i8(entry, 1) == 46 and load_i8(entry, 2) == 0):
                continue
        status = _walk_entry(slots, tokens, entry, bytes_path, directory_entry_type(stream, entry))
        if status == 0:
            index = _WALK_CHILD_NAME
            while index >= _WALK_ENTRY_BYTES:
                _walk_drop(slots, tokens, index)
                index = index - 1
            if py_err_occurred():
                status = -1
    # The scan owns the stream until this unconditional close. Preserve an
    # allocation/codec/callback error over diagnostics caused by close.
    py_tls_exc_swap_slot(ptr_add(slots, _WALK_EXIT_EXCEPTION * C_POINTER_SIZE))
    directory_close(stream)
    py_clear_exception()
    py_tls_exc_swap_slot(ptr_add(slots, _WALK_EXIT_EXCEPTION * C_POINTER_SIZE))
    if status != 0:
        return -1
    if filesystem_error < 0:
        return _walk_os_error(slots, tokens, 0 - filesystem_error)
    store_ptr(slots, _WALK_RESULT * C_POINTER_SIZE, py_tuple_new(3))
    if _walk_adopt(slots, tokens, _WALK_RESULT) != 0:
        return -1
    index = _WALK_DIRECTORIES
    while index <= _WALK_CHILDREN:
        py_tuple_set_item(load_ptr(slots, _WALK_RESULT * C_POINTER_SIZE), index - _WALK_DIRECTORIES,
                          load_ptr(slots, index * C_POINTER_SIZE))
        if py_err_occurred():
            return -1
        index = index + 1
    return 0


def _walk_transaction(path_slot, topdown_slot, followlinks_slot, output_slot, scan: int) -> int:
    if ptr_is_null(path_slot) or ptr_is_null(output_slot):
        return _walk_fail(7, cstr("walk requires authoritative input and output slots"))
    slots = stack_alloc(_WALK_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_WALK_SLOT_COUNT * C_POINTER_SIZE)
    handles = stack_alloc(_WALK_SLOT_COUNT * C_POINTER_SIZE)
    count: int = _walk_open(slots, tokens, handles)
    status: int = -1
    suspended: int = 0
    if count == _WALK_SLOT_COUNT:
        py_tls_exc_swap_slot(ptr_add(slots, _WALK_ENTRY_EXCEPTION * C_POINTER_SIZE))
        suspended = 1
        if not ptr_is_null(load_ptr(output_slot, 0)):
            status = _walk_fail(7, cstr("walk output slot must be empty"))
        else:
            status = _walk_copy(slots, tokens, _WALK_PATH, path_slot)
            if status == 0 and scan:
                if ptr_is_null(topdown_slot) or ptr_is_null(followlinks_slot):
                    status = _walk_fail(7, cstr("walk scan options require owning slots"))
                else:
                    status = _walk_copy(slots, tokens, _WALK_TOPDOWN, topdown_slot)
                    if status == 0:
                        status = _walk_copy(slots, tokens, _WALK_FOLLOWLINKS, followlinks_slot)
            if status == 0:
                if scan:
                    status = _walk_scan(slots, tokens)
                else:
                    status = _walk_prepare_path(slots, tokens)
                    if status == 0:
                        prefix_index: int = _WALK_PREFIX_BYTES if _walk_bytes_path(slots) else _WALK_PREFIX_TEXT
                        status = _walk_copy(slots, tokens, _WALK_RESULT,
                                            ptr_add(slots, prefix_index * C_POINTER_SIZE))
            if status == 0:
                status = _walk_publish(slots, tokens, output_slot)
    _walk_close(slots, tokens, handles, count, suspended)
    if count != _WALK_SLOT_COUNT:
        return _walk_fail(19, cstr("walk root registration failed"))
    return status


@c_abi_export("py_os_walk_scan_slots")
def py_os_walk_scan_slots(path_slot, topdown_slot, followlinks_slot, output_slot) -> int:
    return _walk_transaction(path_slot, topdown_slot, followlinks_slot, output_slot, 1)


@c_abi_export("py_os_walk_prefix_slots")
def py_os_walk_prefix_slots(path_slot, output_slot) -> int:
    return _walk_transaction(path_slot, null(), null(), output_slot, 0)


def _walk_c_object(path, topdown, followlinks, scan: int):
    # This raw ABI is entered only by c_obj extern calls. Their counted
    # address leases keep the borrowed arguments stable until return. Retain
    # each value in a registered owning slot before any managed work.
    slots = stack_alloc(_WALK_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_WALK_SLOT_COUNT * C_POINTER_SIZE)
    memset(slots, 0, _WALK_SLOT_COUNT * C_POINTER_SIZE)
    memset(tokens, 0, _WALK_SLOT_COUNT * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_walk_ffi_frame_map"), slots)
    py_tls_exc_swap_slot(ptr_add(slots, _WALK_ENTRY_EXCEPTION * C_POINTER_SIZE))
    pcc_gc_store_root(ptr_add(slots, _WALK_PATH * C_POINTER_SIZE), path)
    status: int = _walk_adopt(slots, tokens, _WALK_PATH)
    if status == 0 and scan:
        pcc_gc_store_root(ptr_add(slots, _WALK_TOPDOWN * C_POINTER_SIZE), topdown)
        status = _walk_adopt(slots, tokens, _WALK_TOPDOWN)
        if status == 0:
            pcc_gc_store_root(ptr_add(slots, _WALK_FOLLOWLINKS * C_POINTER_SIZE), followlinks)
            status = _walk_adopt(slots, tokens, _WALK_FOLLOWLINKS)
    if status == 0:
        if scan:
            status = _walk_scan(slots, tokens)
        else:
            status = _walk_prepare_path(slots, tokens)
            if status == 0:
                prefix_index: int = _WALK_PREFIX_BYTES if _walk_bytes_path(slots) else _WALK_PREFIX_TEXT
                status = _walk_copy(slots, tokens, _WALK_RESULT,
                                    ptr_add(slots, prefix_index * C_POINTER_SIZE))
    if status != 0:
        # Foreign calls do not implicitly raise TLS errors. Return the
        # exception as a NEW managed owner; the ordinary Python wrapper
        # raises it inside its normal try/except region.
        if not py_err_occurred():
            _walk_fail(7, cstr("walk failed without an exception"))
        pending = ptr_add(slots, _WALK_EXIT_EXCEPTION * C_POINTER_SIZE)
        py_tls_exc_swap_slot(pending)
        _walk_drop(slots, tokens, _WALK_RESULT)
        py_clear_exception()
        if pcc_gc_root_move(ptr_add(slots, _WALK_RESULT * C_POINTER_SIZE), pending) != 0:
            pcc_platform_abort()
            return null()
    _walk_close(slots, tokens, null(), 0, 1, 1)
    output = ptr_add(slots, _WALK_RESULT * C_POINTER_SIZE)
    # Leases protect the produced tuple/string until the no-safepoint return
    # handoff. The Boolean pin is independent and preserves any earlier pin.
    pcc_py_gc_minor_graph_lock()
    value = load_ptr(output, 0)
    prior_pin: int = 0
    if not ptr_is_null(value) and not is_tagged_int(value):
        prior_pin = load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & PY_FLAG_GC_PINNED
        pcc_gc_pin(value)
    pcc_py_gc_minor_graph_unlock()
    token: int = load_i64(tokens, _WALK_RESULT * C_POINTER_SIZE)
    if pcc_gc_foreign_lease_release(output, token) != 0:
        pcc_platform_abort()
        return null()
    pcc_gc_frame_leave(slots)
    return pcc_gc_take_pinned_slot(output, prior_pin)


@c_abi_export("py_os_walk_scan")
def py_os_walk_scan(path, topdown, followlinks):
    return _walk_c_object(path, topdown, followlinks, 1)


@c_abi_export("py_os_walk_prefix")
def py_os_walk_prefix(path):
    return _walk_c_object(path, null(), null(), 0)
