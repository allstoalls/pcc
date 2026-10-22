"""pcc-Python port of py_os_path.c.

Narrow os.path runtime helpers: join, basename, and exists. Non-string
path objects are coerced through py_obj_str before reading UTF-8 bytes.
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import (
    C_POINTER_SIZE, PYBYTESOBJECT_BYTE_LEN_OFFSET, PYBYTESOBJECT_DATA_OFFSET,
    PYOBJECTHEADER_FLAGS_OFFSET, PYOBJECTHEADER_TYPE_TAG_OFFSET, PY_FLAG_GC_PINNED,
    PY_TYPE_BYTES, PY_TYPE_INT, PY_TYPE_LIST,
    PY_TYPE_STR, PY_TYPE_TUPLE,
)
from pcc.extern import (
    extern, c_abi_export, c_double, c_ptr, c_int32, c_int64, c_void,
)
from pcc.unsafe import (
    target_sys_platform, windows_full_path,
    chmod_file,
    cstr,
    define_global_i32,
    global_addr,
    memset,
    stack_alloc,
    store_i32,
    store_ptr,
    free,
    global_load_ptr,
    is_tagged_int,
    load_i8,
    load_i32,
    load_i64,
    load_ptr,
    malloc,
    memmove,
    null,
    ptr_add,
    ptr_is_null,
    realloc,
    store_i8,
    store_i64,
    rename_file,
    sync_file,
    unlinkat,
    stat_size,
    is_symlink,
    strlen,
)


py_decref         = extern("py_decref",         (c_ptr,),         c_void)
py_obj_str        = extern("py_obj_str",        (c_ptr,),         c_ptr)
py_str_new        = extern("py_str_new",        (c_ptr, c_int64), c_ptr)
py_int_from_i64   = extern("py_int_from_i64",   (c_int64,),       c_ptr)
py_float_from_f64 = extern("py_float_from_f64", (c_double,),      c_ptr)
pcc_gc_load_ptr   = extern("pcc_gc_load_ptr",   (c_ptr, c_ptr),   c_ptr)
py_file_open      = extern("py_file_open",      (c_ptr, c_ptr),   c_ptr)
py_file_read_all  = extern("py_file_read_all",  (c_ptr,),         c_ptr)
py_file_close     = extern("py_file_close",     (c_ptr,),         c_void)
py_str_byte_len   = extern("py_str_byte_len",   (c_ptr,),         c_int64)
# Low-level platform-portable stat helpers (defined in C; see
# src/py_os_substrate.c). Avoids encoding struct stat layout — which
# differs between Linux and macOS — in pcc-Python.
py_path_stat_kind  = extern("py_path_stat_kind",  (c_ptr,),         c_int32)
py_path_stat_mtime = extern("py_path_stat_mtime", (c_ptr,),         c_double)
py_path_getcwd     = extern("py_path_getcwd",     (),               c_ptr)
py_path_realpath   = extern("py_path_realpath",   (c_ptr,),         c_ptr)
py_tuple_new       = extern("py_tuple_new",       (c_int64,),       c_ptr)
py_tuple_set_item  = extern("py_tuple_set_item",  (c_ptr, c_int64, c_ptr), c_void)
py_str_lower       = extern("py_str_lower", (c_ptr,), c_ptr)
py_str_utf8        = extern("py_str_utf8",        (c_ptr,),         c_ptr)
py_exc_new         = extern("py_exc_new",         (c_int64, c_ptr), c_ptr)
py_raise_owned     = extern("py_raise_owned",     (c_ptr,),         c_void)
mkdir_sys          = extern("mkdir",              (c_ptr, c_int32), c_int32)
access             = extern("pcc_platform_access", (c_ptr, c_int64), c_int64)
getenv             = extern("pcc_platform_getenv", (c_ptr,), c_ptr)
memcmp_c           = extern("memcmp", (c_ptr, c_ptr, c_int64), c_int64)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
pcc_gc_unpin = extern("pcc_gc_unpin", (c_ptr,), c_void)
pcc_gc_take_pinned_slot = extern("pcc_gc_take_pinned_slot", (c_ptr, c_int64), c_ptr)
pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
pcc_gc_frame_leave = extern("pcc_gc_frame_leave", (c_ptr,), c_void)
pcc_gc_store_root = extern("pcc_gc_store_root", (c_ptr, c_ptr), c_void)
pcc_gc_note_write_barrier = extern("pcc_gc_note_write_barrier", (c_ptr, c_ptr), c_void)

py_bool_from_bit = extern("py_bool_from_bit", (c_int32,), c_ptr)
py_exc_matches = extern("py_exc_matches", (c_ptr, c_ptr), c_int64)
py_file_fspath = extern("py_file_fspath", (c_ptr,), c_ptr)
py_text_encode_ids = extern("py_text_encode_ids", (c_ptr, c_int64, c_int64), c_ptr)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_tls_exc_swap_slot = extern("py_tls_exc_swap_slot", (c_ptr,), c_void)
pcc_gc_root_copy_lease = extern("pcc_gc_root_copy_lease", (c_ptr, c_ptr), c_int64)
pcc_gc_root_copy_borrowed_lease = extern("pcc_gc_root_copy_borrowed_lease", (c_ptr, c_ptr), c_int64)
pcc_gc_root_move = extern("pcc_gc_root_move", (c_ptr, c_ptr), c_int64)
pcc_gc_foreign_lease_acquire = extern("pcc_gc_foreign_lease_acquire", (c_ptr,), c_int64)
pcc_gc_foreign_lease_release = extern("pcc_gc_foreign_lease_release", (c_ptr, c_int64), c_int64)
pcc_gc_note_slot_write_barrier = extern("pcc_gc_note_slot_write_barrier", (c_ptr, c_ptr, c_ptr), c_void)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
pcc_platform_abort = extern("pcc_platform_abort", (), c_void)
py_incref = extern("py_incref", (c_ptr,), c_void)
py_os_error_new = extern("py_os_error_new", (c_int64, c_ptr), c_ptr)
pcc_errno_get = extern("pcc_errno_get", (), c_int32)
pcc_errno_message_into = extern("pcc_errno_message_into", (c_int32, c_ptr, c_int64), c_int32)

_FS_PATH = 0
_FS_NORMALIZED = 1
_FS_ENCODED = 2
_FS_QUERY_CLASS = 3
_FS_QUERY_ERROR = 4
_FS_RESULT = 5
_FS_ENTRY_ERROR = 6
_FS_EXIT_ERROR = 7
_FS_SLOT_COUNT = 8

define_global_i32("pcc_os_fsencode_frame_map", _FS_SLOT_COUNT)
define_global_i32("pcc_os_fsencode_borrowed_map", -1)
define_global_i32("pcc_os_path_abspath_frame_map", 3)
define_global_i32("pcc_os_path_normpath_frame_map", 2)
define_global_i32("pcc_os_path_relpath_frame_map", 4)
define_global_i32("pcc_os_path_compare_frame_map", 4)

# Owned slots while an errno failure of os.replace/unlink/rmdir becomes the
# CPython OSError: the path copies, errno, strerror, args, the exception and
# the pending-exception swap that survives slot release.
_OSE_FIRST = 0
_OSE_SECOND = 1
_OSE_NUMBER = 2
_OSE_MESSAGE = 3
_OSE_ARGS = 4
_OSE_ERROR = 5
_OSE_PENDING = 6
_OSE_COUNT = 7
define_global_i32("pcc_os_errno_owned_map", _OSE_COUNT)
define_global_i32("pcc_os_errno_borrowed_map", -2)


def _path_clear_root_prefix(slots, pins, count: int) -> None:
    # Reverse acquisition order preserves pre-existing pins even when two
    # owned slots alias. Clear through the canonical root-store RC owner.
    index: int = count
    while index > 0:
        index = index - 1
        offset: int = index * 8
        value = load_ptr(slots, offset)
        if not ptr_is_null(value):
            prior_pin: int = load_i64(pins, offset)
            pcc_gc_unpin(value)
            if prior_pin:
                store_i32(value, 12, load_i32(value, 12) | 64)
        pcc_gc_store_root(ptr_add(slots, offset), null())




def _path_windows() -> int:
    return 1 if load_i8(target_sys_platform(), 0) == 119 else 0


def _path_sep(byte: int) -> int:
    return 1 if byte == 47 or (_path_windows() and byte == 92) else 0


def _path_drive_length(data, n: int) -> int:
    if not _path_windows():
        return 0
    if n >= 2 and load_i8(data, 1) == 58:
        return 2
    if n >= 6 and _path_sep(load_i8(data, 0)) and _path_sep(load_i8(data, 1)) and (load_i8(data, 2) == 63 or load_i8(data, 2) == 46) and _path_sep(load_i8(data, 3)) and load_i8(data, 5) == 58:
        return 6
    if n < 2 or not _path_sep(load_i8(data, 0)) or not _path_sep(load_i8(data, 1)):
        return 0
    start: int = 2
    if n >= 8 and load_i8(data, 2) == 63 and _path_sep(load_i8(data, 3)):
        if (load_i8(data, 4) | 32) == 117 and (load_i8(data, 5) | 32) == 110 and (load_i8(data, 6) | 32) == 99 and _path_sep(load_i8(data, 7)):
            start = 8
    first: int = start
    while first < n and not _path_sep(load_i8(data, first)):
        first = first + 1
    if first == n:
        return n
    second: int = first + 1
    while second < n and not _path_sep(load_i8(data, second)):
        second = second + 1
    return second


def _path_windows_equal_build(left, left_len: int, right, right_len: int, slots, pins) -> int:
    left_text = py_str_new(left, left_len)
    store_ptr(slots, 0, left_text)
    if not ptr_is_null(left_text):
        store_i64(pins, 0, load_i32(left_text, 12) & 64)
        pcc_gc_pin(left_text)
        pcc_gc_note_write_barrier(null(), left_text)
    if ptr_is_null(left_text):
        return 0
    right_text = py_str_new(right, right_len)
    store_ptr(slots, 8, right_text)
    if not ptr_is_null(right_text):
        store_i64(pins, 8, load_i32(right_text, 12) & 64)
        pcc_gc_pin(right_text)
        pcc_gc_note_write_barrier(null(), right_text)
    if ptr_is_null(right_text):
        return 0
    left_key = py_os_path_normcase(load_ptr(slots, 0))
    store_ptr(slots, 16, left_key)
    if not ptr_is_null(left_key):
        store_i64(pins, 16, load_i32(left_key, 12) & 64)
        pcc_gc_pin(left_key)
        pcc_gc_note_write_barrier(null(), left_key)
    if ptr_is_null(left_key):
        return 0
    right_key = py_os_path_normcase(load_ptr(slots, 8))
    store_ptr(slots, 24, right_key)
    if not ptr_is_null(right_key):
        store_i64(pins, 24, load_i32(right_key, 12) & 64)
        pcc_gc_pin(right_key)
        pcc_gc_note_write_barrier(null(), right_key)
    if ptr_is_null(right_key):
        return 0
    left_key = load_ptr(slots, 16)
    right_key = load_ptr(slots, 24)
    size: int = load_i64(left_key, 16)
    if size != load_i64(right_key, 16):
        return 0
    return 1 if memcmp_c(ptr_add(left_key, 40), ptr_add(right_key, 40), size) == 0 else 0


def _path_windows_equal(left, left_len: int, right, right_len: int) -> int:
    # The caller owns stable raw spans; this frame owns all allocated keys.
    slots = stack_alloc(32)
    pins = stack_alloc(32)
    memset(slots, 0, 32)
    memset(pins, 0, 32)
    pcc_gc_frame_enter(global_addr("pcc_os_path_compare_frame_map"), slots)
    result: int = _path_windows_equal_build(left, left_len, right, right_len, slots, pins)
    _path_clear_root_prefix(slots, pins, 4)
    pcc_gc_frame_leave(slots)
    return result


def _windows_join(parts):
    count: int = _path_seq_len(parts)
    capacity: int = count + 2
    index: int = 0
    while index < count:
        item, owned = _coerce_path_str(_path_seq_borrow(parts, index))
        if ptr_is_null(item):
            return null()
        capacity = capacity + load_i64(item, 16)
        if not ptr_is_null(owned):
            py_decref(owned)
        index = index + 1
    work = malloc(capacity)
    if ptr_is_null(work):
        return null()
    size: int = 0
    drive: int = 0
    index = 0
    while index < count:
        item, owned = _coerce_path_str(_path_seq_borrow(parts, index))
        if ptr_is_null(item):
            free(work)
            return null()
        data = ptr_add(item, 40)
        n: int = load_i64(item, 16)
        new_drive: int = _path_drive_length(data, n)
        same_drive: int = _path_windows_equal(work, drive, data, new_drive)
        if new_drive and not same_drive:
            memmove(work, data, n)
            size = n
            drive = new_drive
        elif n > new_drive and _path_sep(load_i8(data, new_drive)):
            if new_drive:
                memmove(work, data, new_drive)
                drive = new_drive
            memmove(ptr_add(work, drive), ptr_add(data, new_drive), n - new_drive)
            size = drive + n - new_drive
        else:
            if new_drive:
                if new_drive != drive:
                    memmove(ptr_add(work, new_drive), ptr_add(work, drive), size - drive)
                    size = size - drive + new_drive
                memmove(work, data, new_drive)
                drive = new_drive
            if size > 0 and not _path_sep(load_i8(work, size - 1)) and load_i8(work, size - 1) != 58:
                store_i8(work, size, 92)
                size = size + 1
            memmove(ptr_add(work, size), ptr_add(data, new_drive), n - new_drive)
            size = size + n - new_drive
        if not ptr_is_null(owned):
            py_decref(owned)
        index = index + 1
    result = py_str_new(work, size)
    free(work)
    return result

def _type_of(obj) -> int:
    if is_tagged_int(obj):
        return PY_TYPE_INT       # PY_TYPE_INT
    return load_i32(obj, 8)


def _coerce_path_str(o):
    if ptr_is_null(o) != 0:
        return null(), null()
    if _type_of(o) == PY_TYPE_STR:           # PY_TYPE_STR
        return o, null()
    owned = py_obj_str(o)
    return owned, owned


def _path_seq_len(parts) -> int:
    if ptr_is_null(parts) != 0:
        return -1
    if is_tagged_int(parts):
        return -1
    tag: int = _type_of(parts)
    if tag == PY_TYPE_LIST:                  # PY_TYPE_LIST
        return load_i64(parts, 16)
    if tag == PY_TYPE_TUPLE:                  # PY_TYPE_TUPLE
        return load_i64(parts, 16)
    return -1


def _path_seq_borrow(parts, i: int):
    if ptr_is_null(parts) != 0:
        return null()
    if is_tagged_int(parts):
        return null()
    tag: int = _type_of(parts)
    if tag == PY_TYPE_LIST:                  # PY_TYPE_LIST
        items = load_ptr(parts, 32)
        return pcc_gc_load_ptr(parts, ptr_add(items, i * 8))
    if tag == PY_TYPE_TUPLE:                  # PY_TYPE_TUPLE
        return pcc_gc_load_ptr(parts, ptr_add(parts, 24 + i * 8))
    return null()


@c_abi_export("py_os_makedirs")
def py_os_makedirs(path, mode: int, exist_ok: int):
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        py_raise_owned(py_exc_new(3, cstr("path must be string-like")))
        return null()

    raw = py_str_utf8(item)
    raw_len: int = py_str_byte_len(item)
    if ptr_is_null(raw) != 0 or raw_len <= 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        py_raise_owned(py_exc_new(14, cstr("cannot create empty path")))
        return null()

    buf = malloc(raw_len + 1)
    if ptr_is_null(buf) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        py_raise_owned(py_exc_new(14, cstr("could not allocate path")))
        return null()
    memmove(buf, raw, raw_len)
    store_i8(buf, raw_len, 0)

    drive: int = _path_drive_length(buf, raw_len)
    root_end: int = drive
    if root_end < raw_len and _path_sep(load_i8(buf, root_end)):
        root_end = root_end + 1
    if root_end < 1:
        root_end = 1
    end: int = raw_len
    while end > root_end and _path_sep(load_i8(buf, end - 1)):
        end = end - 1
    store_i8(buf, end, 0)

    i: int = max(1, drive + 1)
    if i > end:
        i = end
    while i <= end:
        if i == end or _path_sep(load_i8(buf, i)):
            saved: int = load_i8(buf, i)
            store_i8(buf, i, 0)
            component_mode: int = mode if i == end else 511
            if mkdir_sys(buf, component_mode) != 0:
                kind: int = py_path_stat_kind(buf)
                if kind != 2 or (i == end and exist_ok == 0):
                    store_i8(buf, i, saved)
                    free(buf)
                    if ptr_is_null(owned) == 0:
                        py_decref(owned)
                    py_raise_owned(
                        py_exc_new(14, cstr("could not create directory"))
                    )
                    return null()
            store_i8(buf, i, saved)
        i = i + 1

    free(buf)
    if ptr_is_null(owned) == 0:
        py_decref(owned)
    return global_load_ptr("py_None")


def _os_failure_errno(result: int) -> int:
    # Darwin libc calls return -1 and set errno; the Linux syscalls, the
    # Windows layer and the normalized Darwin rename return the negated errno.
    # Read errno before anything else can run.
    if result == -1 and load_i8(target_sys_platform(), 0) == 100:
        return pcc_errno_get()
    return 0 - result if result < 0 else result


def _os_errno_fail(kind: int, message) -> int:
    if py_err_occurred() == 0:
        py_raise_owned(py_exc_new(kind, message))
    return -1


def _os_errno_adopt(slots, tokens, index: int) -> int:
    slot = ptr_add(slots, index * C_POINTER_SIZE)
    if ptr_is_null(load_ptr(slot, 0)):
        return _os_errno_fail(19, cstr("os error could not allocate its fields"))
    token: int = pcc_gc_foreign_lease_acquire(slot)
    store_i64(tokens, index * C_POINTER_SIZE, token)
    if token < 0:
        return _os_errno_fail(19, cstr("os error could not lease an owner"))
    return 0


def _os_errno_drop(slots, tokens, index: int) -> None:
    slot = ptr_add(slots, index * C_POINTER_SIZE)
    token: int = load_i64(tokens, index * C_POINTER_SIZE)
    if token >= 0:
        if pcc_gc_foreign_lease_release(slot, token) < 0:
            pcc_platform_abort()
            return
    store_i64(tokens, index * C_POINTER_SIZE, -1)
    pcc_gc_store_root(slot, null())


def _os_errno_body(slots, tokens, borrowed, number: int, paths: int) -> int:
    # Copy the caller's path owners before any allocation can move them.
    index: int = 0
    while index < paths:
        token: int = pcc_gc_root_copy_borrowed_lease(
            ptr_add(slots, (_OSE_FIRST + index) * C_POINTER_SIZE),
            ptr_add(borrowed, index * C_POINTER_SIZE))
        store_i64(tokens, (_OSE_FIRST + index) * C_POINTER_SIZE, token)
        if token < 0:
            return _os_errno_fail(7, cstr("os error could not retain its path"))
        index = index + 1
    message = stack_alloc(256)
    pcc_errno_message_into(number, message, 256)
    store_ptr(slots, _OSE_NUMBER * C_POINTER_SIZE, py_int_from_i64(number))
    if _os_errno_adopt(slots, tokens, _OSE_NUMBER) != 0:
        return -1
    store_ptr(slots, _OSE_MESSAGE * C_POINTER_SIZE, py_str_new(message, strlen(message)))
    if _os_errno_adopt(slots, tokens, _OSE_MESSAGE) != 0:
        return -1
    # OSError(errno, strerror, filename[, winerror, filename2]) selects the
    # errno subclass and keeps args == (errno, strerror), as CPython's.
    store_ptr(slots, _OSE_ARGS * C_POINTER_SIZE, py_tuple_new(5 if paths == 2 else 3))
    if _os_errno_adopt(slots, tokens, _OSE_ARGS) != 0:
        return -1
    args = load_ptr(slots, _OSE_ARGS * C_POINTER_SIZE)
    py_tuple_set_item(args, 0, load_ptr(slots, _OSE_NUMBER * C_POINTER_SIZE))
    py_tuple_set_item(args, 1, load_ptr(slots, _OSE_MESSAGE * C_POINTER_SIZE))
    py_tuple_set_item(args, 2, load_ptr(slots, _OSE_FIRST * C_POINTER_SIZE))
    if paths == 2:
        py_tuple_set_item(args, 3, global_load_ptr("py_None"))
        py_tuple_set_item(args, 4, load_ptr(slots, _OSE_SECOND * C_POINTER_SIZE))
    if py_err_occurred():
        return -1
    store_ptr(slots, _OSE_ERROR * C_POINTER_SIZE,
              py_os_error_new(14, load_ptr(slots, _OSE_ARGS * C_POINTER_SIZE)))
    if _os_errno_adopt(slots, tokens, _OSE_ERROR) != 0:
        return -1
    error = load_ptr(slots, _OSE_ERROR * C_POINTER_SIZE)
    py_incref(error)
    py_raise_owned(error)
    return -1


def _os_raise_errno(number: int, first, second) -> None:
    """Raise CPython's OSError subclass for ``number`` naming the path(s)."""
    borrowed = stack_alloc(2 * C_POINTER_SIZE)
    store_ptr(borrowed, 0, first)
    store_ptr(borrowed, C_POINTER_SIZE, second)
    pcc_gc_frame_enter(global_addr("pcc_os_errno_borrowed_map"), borrowed)
    slots = stack_alloc(_OSE_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_OSE_COUNT * C_POINTER_SIZE)
    memset(slots, 0, _OSE_COUNT * C_POINTER_SIZE)
    index: int = 0
    while index < _OSE_COUNT:
        store_i64(tokens, index * C_POINTER_SIZE, -1)
        index = index + 1
    pcc_gc_frame_enter(global_addr("pcc_os_errno_owned_map"), slots)
    _os_errno_body(slots, tokens, borrowed, number, 1 if ptr_is_null(second) else 2)
    # Hold the raised exception while releasing the field owners.
    py_tls_exc_swap_slot(ptr_add(slots, _OSE_PENDING * C_POINTER_SIZE))
    index = _OSE_ERROR
    while index >= _OSE_FIRST:
        _os_errno_drop(slots, tokens, index)
        index = index - 1
    py_clear_exception()
    py_tls_exc_swap_slot(ptr_add(slots, _OSE_PENDING * C_POINTER_SIZE))
    pcc_gc_frame_leave(slots)
    pcc_gc_frame_leave(borrowed)


@c_abi_export("py_os_unlink")
def py_os_unlink(path):
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        py_decref(owned)
        py_raise_owned(py_exc_new(3, cstr("path must be string-like")))
        return null()
    result: int = unlinkat(py_str_utf8(item), 0)
    number: int = 0 if result == 0 else _os_failure_errno(result)
    py_decref(owned)
    if result != 0:
        _os_raise_errno(number, path, null())
        return null()
    return global_load_ptr("py_None")


@c_abi_export("py_os_rmdir")
def py_os_rmdir(path):
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        py_decref(owned)
        py_raise_owned(py_exc_new(3, cstr("path must be string-like")))
        return null()
    result: int = unlinkat(py_str_utf8(item), 1)
    number: int = 0 if result == 0 else _os_failure_errno(result)
    py_decref(owned)
    if result != 0:
        _os_raise_errno(number, path, null())
        return null()
    return global_load_ptr("py_None")


@c_abi_export("py_os_replace")
def py_os_replace(source, destination):
    source_item, source_owned = _coerce_path_str(source)
    destination_item, destination_owned = _coerce_path_str(destination)
    if ptr_is_null(source_item) != 0 or ptr_is_null(destination_item) != 0:
        py_decref(source_owned)
        py_decref(destination_owned)
        py_raise_owned(py_exc_new(3, cstr("paths must be string-like")))
        return null()
    result: int = rename_file(
        py_str_utf8(source_item), py_str_utf8(destination_item)
    )
    number: int = 0 if result == 0 else _os_failure_errno(result)
    py_decref(source_owned)
    py_decref(destination_owned)
    if result != 0:
        _os_raise_errno(number, source, destination)
        return null()
    return global_load_ptr("py_None")


@c_abi_export("py_os_chmod")
def py_os_chmod(path, mode: int):
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        py_decref(owned)
        py_raise_owned(py_exc_new(3, cstr("path must be string-like")))
        return null()
    result: int = chmod_file(py_str_utf8(item), mode)
    py_decref(owned)
    if result != 0:
        py_raise_owned(py_exc_new(14, cstr("could not change path mode")))
        return null()
    return global_load_ptr("py_None")


@c_abi_export("py_os_fsync")
def py_os_fsync(fd: int):
    if sync_file(fd) != 0:
        py_raise_owned(py_exc_new(14, cstr("could not synchronize file")))
        return null()
    return global_load_ptr("py_None")


@c_abi_export("py_os_path_join")
def py_os_path_join(parts):
    if _path_windows():
        return _windows_join(parts)
    n: int = _path_seq_len(parts)
    if n < 0:
        return null()
    if n == 0:
        return py_str_new(null(), 0)

    # First pass: validate/coerce path parts, find the last absolute
    # component, and compute final byte length. Do not shuttle raw
    # char* buffers through Python tuples; tuple assignment would treat
    # them as PyObject* and corrupt memory through refcounting.
    start_idx: int = 0
    total: int = 0
    last_char: int = 0
    i: int = 0
    while i < n:
        item, owned = _coerce_path_str(_path_seq_borrow(parts, i))
        if ptr_is_null(item) != 0:
            if ptr_is_null(owned) == 0:
                py_decref(owned)
            return null()

        part = ptr_add(item, 40)
        part_len: int = load_i64(item, 16)

        if part_len > 0 and load_i8(part, 0) == 47:     # '/'
            if i > 0:
                start_idx = i
                total = 0
        if i > start_idx:
            if total > 0 and last_char != 47:
                total = total + 1
                last_char = 47
        total = total + part_len
        if part_len > 0:
            last_char = load_i8(part, part_len - 1)
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        i = i + 1

    out = py_str_new(null(), total)
    if ptr_is_null(out) != 0:
        return null()
    dst = ptr_add(out, 40)

    # Second pass: copy bytes into the final PyStrObject.
    off: int = 0
    j: int = start_idx
    while j < n:
        item2, owned2 = _coerce_path_str(_path_seq_borrow(parts, j))
        if ptr_is_null(item2) != 0:
            if ptr_is_null(owned2) == 0:
                py_decref(owned2)
            return null()
        part2 = ptr_add(item2, 40)
        part2_len: int = load_i64(item2, 16)
        if j > start_idx:
            if off > 0 and load_i8(dst, off - 1) != 47:
                store_i8(dst, off, 47)
                off = off + 1
        if part2_len > 0:
            memmove(ptr_add(dst, off), part2, part2_len)
            off = off + part2_len
        if ptr_is_null(owned2) == 0:
            py_decref(owned2)
        j = j + 1
    return out


@c_abi_export("py_os_path_dirname")
def py_os_path_dirname(path):
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return null()

    data = ptr_add(item, 40)
    n: int = load_i64(item, 16)

    drive: int = _path_drive_length(data, n)
    last: int = drive - 1
    i: int = drive
    while i < n:
        if _path_sep(load_i8(data, i)):
            last = i
        i = i + 1

    head_len: int = last + 1
    if head_len == 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return py_str_new(null(), 0)

    all_slash: int = 1
    j: int = drive
    while j < head_len:
        if not _path_sep(load_i8(data, j)):
            all_slash = 0
        j = j + 1

    out_len: int = head_len
    if all_slash == 0:
        while out_len > drive and _path_sep(load_i8(data, out_len - 1)):
            out_len = out_len - 1

    out = py_str_new(data, out_len)
    if ptr_is_null(owned) == 0:
        py_decref(owned)
    return out


@c_abi_export("py_os_path_basename")
def py_os_path_basename(path):
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return null()

    # posixpath.basename: everything after the last "/", so a trailing
    # slash yields "" (unlike the shell's basename, which strips it first).
    data = ptr_add(item, 40)
    end: int = load_i64(item, 16)
    start: int = end
    drive: int = _path_drive_length(data, end)
    while start > drive and not _path_sep(load_i8(data, start - 1)):
        start = start - 1
    out = py_str_new(ptr_add(data, start), end - start)
    if ptr_is_null(owned) == 0:
        py_decref(owned)
    return out


@c_abi_export("py_os_path_split")
def py_os_path_split(path):
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return null()

    data = ptr_add(item, 40)
    n: int = load_i64(item, 16)

    drive: int = _path_drive_length(data, n)
    split_at: int = drive
    i: int = drive
    while i < n:
        if _path_sep(load_i8(data, i)):
            split_at = i + 1
        i = i + 1

    head_len: int = split_at
    if head_len > 0:
        all_slash: int = 1
        j: int = drive
        while j < head_len:
            if not _path_sep(load_i8(data, j)):
                all_slash = 0
            j = j + 1
        if all_slash == 0:
            while head_len > drive and _path_sep(load_i8(data, head_len - 1)):
                head_len = head_len - 1

    head = py_str_new(data, head_len)
    tail = py_str_new(ptr_add(data, split_at), n - split_at)
    out = py_tuple_new(2)
    if ptr_is_null(out) == 0:
        py_tuple_set_item(out, 0, head)
        py_tuple_set_item(out, 1, tail)
    else:
        py_decref(head)
        py_decref(tail)

    if ptr_is_null(owned) == 0:
        py_decref(owned)
    return out


@c_abi_export("py_os_path_isfile")
def py_os_path_isfile(path) -> int:
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return 0
    raw = ptr_add(item, 40)
    kind: int = py_path_stat_kind(raw)
    if ptr_is_null(owned) == 0:
        py_decref(owned)
    if kind == 1:
        return 1
    return 0


def _fs_fail(kind: int, message) -> int:
    if not py_err_occurred():
        py_raise_owned(py_exc_new(kind, message))
    return -1


def _fs_open(slots, tokens) -> None:
    memset(slots, 0, _FS_SLOT_COUNT * C_POINTER_SIZE)
    memset(tokens, 0, _FS_SLOT_COUNT * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_os_fsencode_frame_map"), slots)
    py_tls_exc_swap_slot(ptr_add(slots, _FS_ENTRY_ERROR * C_POINTER_SIZE))


def _fs_adopt(slots, tokens, index: int) -> int:
    slot = ptr_add(slots, index * C_POINTER_SIZE)
    if ptr_is_null(load_ptr(slot, 0)):
        return _fs_fail(19, cstr("filesystem encoding returned no owner"))
    token: int = pcc_gc_foreign_lease_acquire(slot)
    if token < 0:
        return _fs_fail(7, cstr("filesystem encoding could not lease its owner"))
    store_i64(tokens, index * C_POINTER_SIZE, token)
    pcc_py_gc_minor_graph_lock()
    pcc_gc_note_slot_write_barrier(null(), slot, load_ptr(slot, 0))
    pcc_py_gc_minor_graph_unlock()
    return -1 if py_err_occurred() else 0


def _fs_copy(slots, tokens, index: int, source, borrowed: int) -> int:
    destination = ptr_add(slots, index * C_POINTER_SIZE)
    token: int = pcc_gc_root_copy_borrowed_lease(destination, source) if borrowed else pcc_gc_root_copy_lease(destination, source)
    if token < 0:
        return _fs_fail(7, cstr("filesystem encoding requires an authoritative owner"))
    store_i64(tokens, index * C_POINTER_SIZE, token)
    return 0


def _fs_drop(slots, tokens, index: int) -> None:
    slot = ptr_add(slots, index * C_POINTER_SIZE)
    if pcc_gc_foreign_lease_release(slot, load_i64(tokens, index * C_POINTER_SIZE)) != 0:
        pcc_platform_abort()
        return
    store_i64(tokens, index * C_POINTER_SIZE, 0)
    pcc_gc_store_root(slot, null())


def _fs_close(slots, tokens, keep_result: int = 0) -> None:
    py_tls_exc_swap_slot(ptr_add(slots, _FS_EXIT_ERROR * C_POINTER_SIZE))
    index: int = _FS_RESULT
    while index >= _FS_PATH:
        if not keep_result or index != _FS_RESULT:
            _fs_drop(slots, tokens, index)
        index = index - 1
    py_clear_exception()
    if not ptr_is_null(load_ptr(slots, _FS_EXIT_ERROR * C_POINTER_SIZE)):
        pcc_gc_store_root(ptr_add(slots, _FS_ENTRY_ERROR * C_POINTER_SIZE), null())
        py_clear_exception()
        py_tls_exc_swap_slot(ptr_add(slots, _FS_EXIT_ERROR * C_POINTER_SIZE))
    else:
        py_tls_exc_swap_slot(ptr_add(slots, _FS_ENTRY_ERROR * C_POINTER_SIZE))
    if not keep_result:
        pcc_gc_frame_leave(slots)


def _fs_encode(slots, tokens) -> int:
    # Normalize via the shared special-method protocol. Bytes retain their
    # exact contents; text follows the runtime filesystem codec contract.
    path = load_ptr(slots, _FS_PATH * C_POINTER_SIZE)
    native_path: int = 0
    if not ptr_is_null(path) and not is_tagged_int(path):
        kind: int = load_i32(path, PYOBJECTHEADER_TYPE_TAG_OFFSET)
        native_path = kind == PY_TYPE_STR or kind == PY_TYPE_BYTES
    if native_path:
        if _fs_copy(slots, tokens, _FS_NORMALIZED,
                    ptr_add(slots, _FS_PATH * C_POINTER_SIZE), 0) != 0:
            return -1
    else:
        store_ptr(slots, _FS_NORMALIZED * C_POINTER_SIZE, py_file_fspath(path))
        if _fs_adopt(slots, tokens, _FS_NORMALIZED) != 0:
            return -1
    path = load_ptr(slots, _FS_NORMALIZED * C_POINTER_SIZE)
    if load_i32(path, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_BYTES:
        return _fs_copy(slots, tokens, _FS_ENCODED,
                        ptr_add(slots, _FS_NORMALIZED * C_POINTER_SIZE), 0)
    store_ptr(slots, _FS_ENCODED * C_POINTER_SIZE, py_text_encode_ids(path, 0, 3))
    return _fs_adopt(slots, tokens, _FS_ENCODED)


@c_abi_export("py_os_fsencode_slots")
def py_os_fsencode_slots(path_slot, output_slot) -> int:
    """Publish filesystem-encoded bytes from an authoritative path owner."""
    if ptr_is_null(path_slot) or ptr_is_null(output_slot):
        return _fs_fail(7, cstr("filesystem encoding requires input and output roots"))
    slots = stack_alloc(_FS_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_FS_SLOT_COUNT * C_POINTER_SIZE)
    _fs_open(slots, tokens)
    status: int = -1
    if not ptr_is_null(load_ptr(output_slot, 0)):
        _fs_fail(7, cstr("filesystem encoding output must be empty"))
    elif _fs_copy(slots, tokens, _FS_PATH, path_slot, 0) == 0:
        status = _fs_encode(slots, tokens)
        if status == 0:
            encoded = ptr_add(slots, _FS_ENCODED * C_POINTER_SIZE)
            if pcc_gc_root_move(output_slot, encoded) != 0:
                status = _fs_fail(7, cstr("filesystem encoding could not publish its owner"))
            else:
                token: int = load_i64(tokens, _FS_ENCODED * C_POINTER_SIZE)
                store_i64(tokens, _FS_ENCODED * C_POINTER_SIZE, 0)
                if pcc_gc_foreign_lease_release(output_slot, token) != 0:
                    pcc_platform_abort()
                    status = -1
    _fs_close(slots, tokens)
    return status


def _fs_suppress_link_error(slots, tokens) -> None:
    # os.path.islink suppresses OSError and ValueError (including encoding
    # errors), but preserves TypeError and arbitrary PathLike exceptions.
    # Cached exception classes already have mapped owning slots; copy from
    # those slots without allocating a fresh matcher or using a raw class.
    error = ptr_add(slots, _FS_QUERY_ERROR * C_POINTER_SIZE)
    py_tls_exc_swap_slot(error)
    if ptr_is_null(load_ptr(error, 0)):
        return
    matched: int = 0
    if _fs_adopt(slots, tokens, _FS_QUERY_ERROR) == 0:
        kind: int = 2
        while True:
            if _fs_copy(slots, tokens, _FS_QUERY_CLASS,
                        ptr_add(global_addr("py_exc_classes"), kind * C_POINTER_SIZE), 0) != 0:
                break
            matched = py_exc_matches(load_ptr(error, 0),
                                      load_ptr(slots, _FS_QUERY_CLASS * C_POINTER_SIZE))
            _fs_drop(slots, tokens, _FS_QUERY_CLASS)
            if matched or kind == 14:
                break
            kind = 14
    py_clear_exception()
    if matched:
        _fs_drop(slots, tokens, _FS_QUERY_ERROR)
        py_clear_exception()
    else:
        if pcc_gc_foreign_lease_release(error, load_i64(tokens, _FS_QUERY_ERROR * C_POINTER_SIZE)) != 0:
            pcc_platform_abort()
            return
        store_i64(tokens, _FS_QUERY_ERROR * C_POINTER_SIZE, 0)
        py_tls_exc_swap_slot(error)


def _fs_link_query(slots, tokens) -> int:
    result: int = 0
    if _fs_encode(slots, tokens) == 0:
        encoded = load_ptr(slots, _FS_ENCODED * C_POINTER_SIZE)
        raw = ptr_add(encoded, PYBYTESOBJECT_DATA_OFFSET)
        length: int = load_i64(encoded, PYBYTESOBJECT_BYTE_LEN_OFFSET)
        index: int = 0
        while index < length and load_i8(raw, index) != 0:
            index = index + 1
        if index == length:
            result = is_symlink(raw)
    if py_err_occurred():
        _fs_suppress_link_error(slots, tokens)
    return result


@c_abi_export("py_os_path_islink")
def py_os_path_islink(path) -> int:
    borrowed = stack_alloc(C_POINTER_SIZE)
    store_ptr(borrowed, 0, path)
    pcc_gc_frame_enter(global_addr("pcc_os_fsencode_borrowed_map"), borrowed)
    slots = stack_alloc(_FS_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_FS_SLOT_COUNT * C_POINTER_SIZE)
    _fs_open(slots, tokens)
    result: int = 0
    if _fs_copy(slots, tokens, _FS_PATH, borrowed, 1) == 0:
        result = _fs_link_query(slots, tokens)
    _fs_close(slots, tokens)
    store_ptr(borrowed, 0, null())
    pcc_gc_frame_leave(borrowed)
    return result


@c_abi_export("py_os_path_islink_result")
def py_os_path_islink_result(path):
    """Return an owned bool or exception for an ordinary c_obj wrapper."""
    borrowed = stack_alloc(C_POINTER_SIZE)
    store_ptr(borrowed, 0, path)
    pcc_gc_frame_enter(global_addr("pcc_os_fsencode_borrowed_map"), borrowed)
    slots = stack_alloc(_FS_SLOT_COUNT * C_POINTER_SIZE)
    tokens = stack_alloc(_FS_SLOT_COUNT * C_POINTER_SIZE)
    _fs_open(slots, tokens)
    result: int = 0
    if _fs_copy(slots, tokens, _FS_PATH, borrowed, 1) == 0:
        result = _fs_link_query(slots, tokens)
    output = ptr_add(slots, _FS_RESULT * C_POINTER_SIZE)
    if py_err_occurred():
        py_tls_exc_swap_slot(output)
    else:
        pcc_gc_store_root(output, py_bool_from_bit(result))
    _fs_close(slots, tokens, 1)
    pcc_py_gc_minor_graph_lock()
    value = load_ptr(output, 0)
    prior_pin: int = load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & PY_FLAG_GC_PINNED
    pcc_gc_pin(value)
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_frame_leave(slots)
    store_ptr(borrowed, 0, null())
    pcc_gc_frame_leave(borrowed)
    return pcc_gc_take_pinned_slot(output, prior_pin)


@c_abi_export("py_os_path_isabs")
def py_os_path_isabs(path) -> int:
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return 0
    data = ptr_add(item, 40)
    n: int = load_i64(item, 16)
    ok: int = 0
    if _path_windows():
        drive: int = _path_drive_length(data, n)
        if drive > 0 and n > drive and _path_sep(load_i8(data, drive)):
            ok = 1
        elif drive > 1 and _path_sep(load_i8(data, 0)) and _path_sep(load_i8(data, 1)):
            ok = 1
    elif n > 0 and load_i8(data, 0) == 47:
        ok = 1
    if ptr_is_null(owned) == 0:
        py_decref(owned)
    return ok


@c_abi_export("py_os_path_isdir")
def py_os_path_isdir(path) -> int:
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return 0
    raw = ptr_add(item, 40)
    kind: int = py_path_stat_kind(raw)
    if ptr_is_null(owned) == 0:
        py_decref(owned)
    if kind == 2:
        return 1
    return 0


def _path_abspath_build(path, slots, pins) -> None:
    # Inputs are borrowed and pinned by the managed call boundary. New owned
    # strings go into an already registered slot before any following call.
    if ptr_is_null(path):
        return
    item = path
    if _type_of(path) != PY_TYPE_STR:
        converted = py_obj_str(path)
        store_ptr(slots, 0, converted)
        if not ptr_is_null(converted):
            store_i64(pins, 0, load_i32(converted, 12) & 64)
            pcc_gc_pin(converted)
            pcc_gc_note_write_barrier(null(), converted)
        if ptr_is_null(converted):
            return
        item = load_ptr(slots, 0)
    data = ptr_add(item, 40)
    in_len: int = load_i64(item, 16)
    if _path_windows():
        buffer = malloc(131072)
        result = windows_full_path(py_str_utf8(item), buffer, 131072) if not ptr_is_null(buffer) else null()
        if not ptr_is_null(result):
            length: int = 0
            while load_i8(result, length) != 0:
                length = length + 1
            out = py_str_new(result, length)
            store_ptr(slots, 8, out)
            if not ptr_is_null(out):
                store_i64(pins, 8, load_i32(out, 12) & 64)
                pcc_gc_pin(out)
                pcc_gc_note_write_barrier(null(), out)
        free(buffer)
        return
    if in_len > 0 and load_i8(data, 0) == 47:
        out = py_str_new(data, in_len)
        store_ptr(slots, 8, out)
        if not ptr_is_null(out):
            store_i64(pins, 8, load_i32(out, 12) & 64)
            pcc_gc_pin(out)
            pcc_gc_note_write_barrier(null(), out)
        return
    cwd_ptr = py_path_getcwd()
    if ptr_is_null(cwd_ptr):
        return
    cwd_len: int = 0
    while load_i8(cwd_ptr, cwd_len) != 0:
        cwd_len = cwd_len + 1
    if in_len == 0:
        out = py_str_new(cwd_ptr, cwd_len)
        store_ptr(slots, 8, out)
        if not ptr_is_null(out):
            store_i64(pins, 8, load_i32(out, 12) & 64)
            pcc_gc_pin(out)
            pcc_gc_note_write_barrier(null(), out)
        return
    # getcwd("/") already supplies the separator. Inventing a second slash
    # would create a distinct POSIX // prefix that normpath must preserve.
    separator_len: int = 0 if cwd_len > 0 and load_i8(cwd_ptr, cwd_len - 1) == 47 else 1
    total: int = cwd_len + separator_len + in_len
    out = py_str_new(null(), total)
    store_ptr(slots, 8, out)
    if not ptr_is_null(out):
        store_i64(pins, 8, load_i32(out, 12) & 64)
        pcc_gc_pin(out)
        pcc_gc_note_write_barrier(null(), out)
    if ptr_is_null(out):
        return
    out = load_ptr(slots, 8)
    dst = ptr_add(out, 40)
    memmove(dst, cwd_ptr, cwd_len)
    if separator_len:
        store_i8(dst, cwd_len, 47)
    memmove(ptr_add(dst, cwd_len + separator_len), data, in_len)


@c_abi_export("py_os_path_abspath")
def py_os_path_abspath(path):
    slots = stack_alloc(24)
    pins = stack_alloc(24)
    memset(slots, 0, 24)
    memset(pins, 0, 24)
    pcc_gc_frame_enter(global_addr("pcc_os_path_abspath_frame_map"), slots)
    _path_abspath_build(path, slots, pins)
    if not ptr_is_null(load_ptr(slots, 8)):
        # abspath is normpath(join(cwd, path)), including dot/dotdot and
        # the POSIX two-leading-slash rule. Reuse the existing path algorithm.
        normalized = py_os_path_normpath(load_ptr(slots, 8))
        store_ptr(slots, 16, normalized)
        if not ptr_is_null(normalized):
            store_i64(pins, 16, load_i32(normalized, 12) & 64)
            pcc_gc_pin(normalized)
            pcc_gc_note_write_barrier(null(), normalized)
    _path_clear_root_prefix(slots, pins, 2)
    pcc_gc_frame_leave(slots)
    return pcc_gc_take_pinned_slot(ptr_add(slots, 16), load_i64(pins, 16))


def _windows_expanduser(item):
    data = ptr_add(item, 40)
    size: int = load_i64(item, 16)
    if size == 0 or load_i8(data, 0) != 126:
        return py_str_new(data, size)
    end: int = 1
    while end < size and not _path_sep(load_i8(data, end)):
        end = end + 1
    profile = getenv(cstr("USERPROFILE"))
    home = null()
    if not ptr_is_null(profile):
        home = py_str_new(profile, _path_cstr_len(profile))
    else:
        homepath = getenv(cstr("HOMEPATH"))
        if ptr_is_null(homepath):
            return py_str_new(data, size)
        drive = getenv(cstr("HOMEDRIVE"))
        parts = py_tuple_new(2)
        if ptr_is_null(parts):
            return null()
        py_tuple_set_item(parts, 0, py_str_new(drive, 0 if ptr_is_null(drive) else _path_cstr_len(drive)))
        py_tuple_set_item(parts, 1, py_str_new(homepath, _path_cstr_len(homepath)))
        home = _windows_join(parts)
        py_decref(parts)
    if ptr_is_null(home):
        return null()
    if end > 1:
        username = getenv(cstr("USERNAME"))
        user_len: int = 0 if ptr_is_null(username) else _path_cstr_len(username)
        target_len: int = end - 1
        same_user: int = 0
        if not ptr_is_null(username) and target_len == user_len:
            same_user = _path_bytes_equal(ptr_add(data, 1), username, target_len)
        if not same_user:
            # A sibling profile is only a valid guess when the current
            # profile's basename actually equals USERNAME.
            base = py_os_path_basename(home)
            valid: int = 0
            if not ptr_is_null(base) and not ptr_is_null(username) and load_i64(base, 16) == user_len:
                valid = _path_bytes_equal(ptr_add(base, 40), username, user_len)
            py_decref(base)
            if not valid:
                py_decref(home)
                return py_str_new(data, size)
            parts = py_tuple_new(2)
            if ptr_is_null(parts):
                py_decref(home)
                return null()
            py_tuple_set_item(parts, 0, py_os_path_dirname(home))
            py_tuple_set_item(parts, 1, py_str_new(ptr_add(data, 1), target_len))
            replacement = _windows_join(parts)
            py_decref(parts)
            py_decref(home)
            home = replacement
            if ptr_is_null(home):
                return null()
    home_len: int = load_i64(home, 16)
    out = py_str_new(null(), home_len + size - end)
    if not ptr_is_null(out):
        memmove(ptr_add(out, 40), ptr_add(home, 40), home_len)
        memmove(ptr_add(out, 40 + home_len), ptr_add(data, end), size - end)
    py_decref(home)
    return out


@c_abi_export("py_os_path_expanduser")
def py_os_path_expanduser(path):
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return null()
    data = ptr_add(item, 40)
    n: int = load_i64(item, 16)
    if _path_windows():
        out = _windows_expanduser(item)
        py_decref(owned)
        return out

    # Only a bare "~" or "~/..." prefix expands to $HOME. A "~user" prefix
    # (no '/' right after '~') and a path without a leading '~' (126) are
    # returned unchanged, matching CPython posixpath.expanduser.
    is_home: int = 0
    if n >= 1:
        if load_i8(data, 0) == 126:
            if n == 1:
                is_home = 1
            if n > 1:
                if _path_sep(load_i8(data, 1)):
                    is_home = 1
    home_ptr = null()
    if is_home == 1:
        home_ptr = getenv(cstr("HOME"))
    if is_home == 0 or ptr_is_null(home_ptr) != 0:
        out = py_str_new(data, n)
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return out

    # userhome = home.rstrip('/'); result = (userhome + path[1:]) or "/".
    home_len: int = 0
    while load_i8(home_ptr, home_len) != 0:
        home_len = home_len + 1
    while home_len > 0 and _path_sep(load_i8(home_ptr, home_len - 1)):
        home_len = home_len - 1
    rest_len: int = n - 1
    total: int = home_len + rest_len
    if total == 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return py_str_new(cstr("/"), 1)
    out = py_str_new(null(), total)
    if ptr_is_null(out) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return null()
    dst = ptr_add(out, 40)
    memmove(dst, home_ptr, home_len)
    memmove(ptr_add(dst, home_len), ptr_add(data, 1), rest_len)
    if ptr_is_null(owned) == 0:
        py_decref(owned)
    return out


@c_abi_export("py_os_path_realpath")
def py_os_path_realpath(path):
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return null()
    raw = ptr_add(item, 40)
    resolved = py_path_realpath(raw)
    if ptr_is_null(resolved) == 0:
        rlen: int = 0
        while load_i8(resolved, rlen) != 0:
            rlen = rlen + 1
        out = py_str_new(resolved, rlen)
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return out
    # realpath(3) failed (path or a component does not exist): fall back to
    # lexical normpath(abspath(path)) — absolute with "." / ".." collapsed.
    if ptr_is_null(owned) == 0:
        py_decref(owned)
    abs_path = py_os_path_abspath(path)
    if ptr_is_null(abs_path) != 0:
        return null()
    out = py_os_path_normpath(abs_path)
    py_decref(abs_path)
    return out


@c_abi_export("py_os_path_getmtime")
def py_os_path_getmtime(path):
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return null()
    raw = py_str_utf8(item)
    length: int = py_str_byte_len(item)
    index: int = 0
    while index < length:
        if load_i8(raw, index) == 0:
            py_decref(owned)
            py_raise_owned(py_exc_new(2, cstr("embedded null byte")))
            return null()
        index = index + 1
    t: float = py_path_stat_mtime(raw)
    py_decref(owned)
    # The raw metadata boundary reserves NaN for stat failure. Epoch zero
    # and pre-epoch timestamps are valid; neither can serve as an error flag.
    if t != t:
        py_raise_owned(py_exc_new(14, cstr("could not stat path")))
        return null()
    return py_float_from_f64(t)


@c_abi_export("py_os_path_getsize")
def py_os_path_getsize(path):
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return null()
    raw = py_str_utf8(item)
    length: int = py_str_byte_len(item)
    index: int = 0
    while index < length:
        if load_i8(raw, index) == 0:
            py_decref(owned)
            py_raise_owned(py_exc_new(2, cstr("embedded null byte")))
            return null()
        index = index + 1
    size: int = stat_size(raw)
    py_decref(owned)
    if size < 0:
        kind: int = 14
        if size == -2:
            kind = 34
        elif size == -20:
            kind = 38
        elif size == -13 or size == -1:
            kind = 36
        py_raise_owned(py_exc_new(kind, cstr("could not stat path")))
        return null()
    return py_int_from_i64(size)


@c_abi_export("py_os_path_splitext")
def py_os_path_splitext(path):
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return null()
    data = ptr_add(item, 40)
    n: int = load_i64(item, 16)

    slash: int = -1
    dot: int = -1
    i: int = 0
    while i < n:
        b: int = load_i8(data, i)
        if _path_sep(b):
            slash = i
            dot = -1
        elif b == 46:      # '.'
            dot = i
        i = i + 1

    non_dot: int = slash + 1
    while non_dot < n and load_i8(data, non_dot) == 46:
        non_dot = non_dot + 1
    if dot < non_dot:
        base = py_str_new(data, n)
        ext = py_str_new(null(), 0)
    else:
        base = py_str_new(data, dot)
        ext = py_str_new(ptr_add(data, dot), n - dot)

    out = py_tuple_new(2)
    if ptr_is_null(out) == 0:
        py_tuple_set_item(out, 0, base)
        py_tuple_set_item(out, 1, ext)
    else:
        py_decref(base)
        py_decref(ext)

    if ptr_is_null(owned) == 0:
        py_decref(owned)
    return out


@c_abi_export("py_os_path_normcase")
def py_os_path_normcase(path):
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return null()
    data = ptr_add(item, 40)
    n: int = load_i64(item, 16)
    out = py_str_new(data, n)
    if _path_windows() and not ptr_is_null(out):
        index: int = 0
        while index < n:
            if load_i8(ptr_add(out, 40), index) == 47:
                store_i8(ptr_add(out, 40), index, 92)
            index = index + 1
        lowered = py_str_lower(out)
        py_decref(out)
        out = lowered
    if ptr_is_null(owned) == 0:
        py_decref(owned)
    return out


def _path_component_is_dotdot(data, start: int, n: int) -> int:
    if n == 2:
        if load_i8(data, start) == 46 and load_i8(data, start + 1) == 46:
            return 1
    return 0


def _path_normpath_build(path, slots, pins) -> None:
    if ptr_is_null(path):
        return
    item = path
    if _type_of(path) != PY_TYPE_STR:
        converted = py_obj_str(path)
        store_ptr(slots, 0, converted)
        if not ptr_is_null(converted):
            store_i64(pins, 0, load_i32(converted, 12) & 64)
            pcc_gc_pin(converted)
            pcc_gc_note_write_barrier(null(), converted)
        if ptr_is_null(converted):
            return
        item = load_ptr(slots, 0)
    data = ptr_add(item, 40)
    n: int = load_i64(item, 16)
    work = malloc(n + 4)
    starts = malloc((n + 1) * 8)
    lens = malloc((n + 1) * 8)
    if ptr_is_null(work) or ptr_is_null(starts) or ptr_is_null(lens):
        free(work)
        free(starts)
        free(lens)
        return
    windows: int = _path_windows()
    separator: int = 92 if windows else 47
    drive: int = _path_drive_length(data, n)
    cursor: int = 0
    while cursor < drive:
        byte: int = load_i8(data, cursor)
        store_i8(work, cursor, separator if windows and _path_sep(byte) else byte)
        cursor = cursor + 1
    rooted: int = 0
    if n > drive and _path_sep(load_i8(data, drive)):
        rooted = 1
        if not windows and n > 1 and load_i8(data, 1) == 47 and (n == 2 or load_i8(data, 2) != 47):
            rooted = 2
    out_len: int = drive
    cursor = 0
    while cursor < rooted:
        store_i8(work, out_len, separator)
        out_len = out_len + 1
        cursor = cursor + 1
    base_len: int = out_len
    comps: int = 0
    cursor = drive + rooted
    while cursor < n:
        while cursor < n and _path_sep(load_i8(data, cursor)):
            cursor = cursor + 1
        start: int = cursor
        while cursor < n and not _path_sep(load_i8(data, cursor)):
            cursor = cursor + 1
        size: int = cursor - start
        if size == 0 or (size == 1 and load_i8(data, start) == 46):
            continue
        if _path_component_is_dotdot(data, start, size):
            if comps > 0:
                last_start: int = load_i64(starts, (comps - 1) * 8)
                last_size: int = load_i64(lens, (comps - 1) * 8)
                if not _path_component_is_dotdot(work, last_start, last_size):
                    comps = comps - 1
                    out_len = last_start
                    if out_len > base_len and load_i8(work, out_len - 1) == separator:
                        out_len = out_len - 1
                    continue
            if rooted:
                continue
        if out_len > base_len and load_i8(work, out_len - 1) != separator:
            store_i8(work, out_len, separator)
            out_len = out_len + 1
        store_i64(starts, comps * 8, out_len)
        store_i64(lens, comps * 8, size)
        memmove(ptr_add(work, out_len), ptr_add(data, start), size)
        out_len = out_len + size
        comps = comps + 1
    if out_len == 0:
        store_i8(work, out_len, 46)
        out_len = out_len + 1
    result = py_str_new(work, out_len)
    store_ptr(slots, 8, result)
    if not ptr_is_null(result):
        store_i64(pins, 8, load_i32(result, 12) & 64)
        pcc_gc_pin(result)
        pcc_gc_note_write_barrier(null(), result)
    free(work)
    free(starts)
    free(lens)
    return


@c_abi_export("py_os_path_normpath")
def py_os_path_normpath(path):
    slots = stack_alloc(16)
    pins = stack_alloc(16)
    memset(slots, 0, 16)
    memset(pins, 0, 16)
    pcc_gc_frame_enter(global_addr("pcc_os_path_normpath_frame_map"), slots)
    _path_normpath_build(path, slots, pins)
    _path_clear_root_prefix(slots, pins, 1)
    pcc_gc_frame_leave(slots)
    return pcc_gc_take_pinned_slot(ptr_add(slots, 8), load_i64(pins, 8))


@c_abi_export("py_os_path_splitdrive")
def py_os_path_splitdrive(path):
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return null()
    data = ptr_add(item, 40)
    n: int = load_i64(item, 16)
    drive_length: int = _path_drive_length(data, n)
    drive = py_str_new(data, drive_length)
    tail = py_str_new(ptr_add(data, drive_length), n - drive_length)
    out = py_tuple_new(2)
    if ptr_is_null(out) == 0:
        py_tuple_set_item(out, 0, drive)
        py_tuple_set_item(out, 1, tail)
    else:
        py_decref(drive)
        py_decref(tail)
    if ptr_is_null(owned) == 0:
        py_decref(owned)
    return out


@c_abi_export("py_os_path_commonprefix")
def py_os_path_commonprefix(paths):
    n: int = _path_seq_len(paths)
    if n < 0:
        return null()
    if n == 0:
        return py_str_new(null(), 0)

    first, first_owned = _coerce_path_str(_path_seq_borrow(paths, 0))
    if ptr_is_null(first) != 0:
        if ptr_is_null(first_owned) == 0:
            py_decref(first_owned)
        return null()
    first_data = ptr_add(first, 40)
    common_len: int = load_i64(first, 16)

    i: int = 1
    while i < n:
        item, owned = _coerce_path_str(_path_seq_borrow(paths, i))
        if ptr_is_null(item) != 0:
            if ptr_is_null(owned) == 0:
                py_decref(owned)
            if ptr_is_null(first_owned) == 0:
                py_decref(first_owned)
            return null()
        data = ptr_add(item, 40)
        item_len: int = load_i64(item, 16)
        limit: int = common_len
        if item_len < limit:
            limit = item_len
        j: int = 0
        while j < limit and load_i8(first_data, j) == load_i8(data, j):
            j = j + 1
        common_len = j
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        if common_len == 0:
            break
        i = i + 1

    out = py_str_new(first_data, common_len)
    if ptr_is_null(first_owned) == 0:
        py_decref(first_owned)
    return out


def _windows_component_spans(data, size: int, start: int, offsets, lengths) -> int:
    count: int = 0
    cursor: int = start
    while cursor < size:
        while cursor < size and _path_sep(load_i8(data, cursor)):
            cursor = cursor + 1
        begin: int = cursor
        while cursor < size and not _path_sep(load_i8(data, cursor)):
            cursor = cursor + 1
        length: int = cursor - begin
        if length == 0 or (length == 1 and load_i8(data, begin) == 46):
            continue
        # commonpath is lexical: '..' remains a component.
        store_i64(offsets, count * 8, begin)
        store_i64(lengths, count * 8, length)
        count = count + 1
    return count


def _windows_commonpath(paths):
    count: int = _path_seq_len(paths)
    if count <= 0:
        py_raise_owned(py_exc_new(2, cstr("commonpath() arg is an empty sequence")))
        return null()
    first, first_owned = _coerce_path_str(_path_seq_borrow(paths, 0))
    if ptr_is_null(first):
        py_decref(first_owned)
        return null()
    data = ptr_add(first, 40)
    size: int = load_i64(first, 16)
    drive: int = _path_drive_length(data, size)
    root: int = 1 if size > drive and _path_sep(load_i8(data, drive)) else 0
    offsets = malloc((size + 1) * 8)
    lengths = malloc((size + 1) * 8)
    if ptr_is_null(offsets) or ptr_is_null(lengths):
        free(offsets)
        free(lengths)
        py_decref(first_owned)
        return null()
    common: int = _windows_component_spans(data, size, drive, offsets, lengths)
    failure: int = 0
    index: int = 1
    while index < count and failure == 0:
        item, owned = _coerce_path_str(_path_seq_borrow(paths, index))
        if ptr_is_null(item):
            failure = 1
        else:
            other = ptr_add(item, 40)
            other_size: int = load_i64(item, 16)
            other_drive: int = _path_drive_length(other, other_size)
            other_root: int = 1 if other_size > other_drive and _path_sep(load_i8(other, other_drive)) else 0
            if not _path_windows_equal(data, drive, other, other_drive):
                failure = 2
            elif root != other_root:
                failure = 3
            else:
                cursor: int = other_drive
                matched: int = 0
                while cursor < other_size and matched < common:
                    while cursor < other_size and _path_sep(load_i8(other, cursor)):
                        cursor = cursor + 1
                    begin: int = cursor
                    while cursor < other_size and not _path_sep(load_i8(other, cursor)):
                        cursor = cursor + 1
                    part_size: int = cursor - begin
                    if part_size == 0 or (part_size == 1 and load_i8(other, begin) == 46):
                        continue
                    if not _path_windows_equal(ptr_add(data, load_i64(offsets, matched * 8)), load_i64(lengths, matched * 8), ptr_add(other, begin), part_size):
                        break
                    matched = matched + 1
                common = matched
        py_decref(owned)
        index = index + 1
    if failure:
        free(offsets)
        free(lengths)
        py_decref(first_owned)
        if failure == 2:
            py_raise_owned(py_exc_new(2, cstr("Paths don't have the same drive")))
        elif failure == 3:
            py_raise_owned(py_exc_new(2, cstr("Can't mix rooted and not-rooted paths")))
        return null()
    out_size: int = drive + root
    index = 0
    while index < common:
        out_size = out_size + load_i64(lengths, index * 8) + (1 if index else 0)
        index = index + 1
    out = py_str_new(null(), out_size)
    if not ptr_is_null(out):
        destination = ptr_add(out, 40)
        cursor = 0
        while cursor < drive:
            byte: int = load_i8(data, cursor)
            store_i8(destination, cursor, 92 if _path_sep(byte) else byte)
            cursor = cursor + 1
        if root:
            store_i8(destination, cursor, 92)
            cursor = cursor + 1
        index = 0
        while index < common:
            if index:
                store_i8(destination, cursor, 92)
                cursor = cursor + 1
            part_size = load_i64(lengths, index * 8)
            memmove(ptr_add(destination, cursor), ptr_add(data, load_i64(offsets, index * 8)), part_size)
            cursor = cursor + part_size
            index = index + 1
    free(offsets)
    free(lengths)
    py_decref(first_owned)
    return out


@c_abi_export("py_os_path_commonpath")
def py_os_path_commonpath(paths):
    """Return the shared POSIX component prefix of a list or tuple."""
    if _path_windows():
        return _windows_commonpath(paths)
    n: int = _path_seq_len(paths)
    if n <= 0:
        return py_str_new(null(), 0)

    first, first_owned = _coerce_path_str(_path_seq_borrow(paths, 0))
    if ptr_is_null(first) != 0:
        if ptr_is_null(first_owned) == 0:
            py_decref(first_owned)
        return null()
    first_data = ptr_add(first, 40)
    first_len: int = load_i64(first, 16)
    prefix_len: int = first_len

    i: int = 1
    while i < n:
        item, owned = _coerce_path_str(_path_seq_borrow(paths, i))
        if ptr_is_null(item) != 0:
            if ptr_is_null(owned) == 0:
                py_decref(owned)
            if ptr_is_null(first_owned) == 0:
                py_decref(first_owned)
            return null()
        data = ptr_add(item, 40)
        item_len: int = load_i64(item, 16)
        limit: int = prefix_len
        if item_len < limit:
            limit = item_len
        j: int = 0
        while j < limit and load_i8(first_data, j) == load_i8(data, j):
            j = j + 1
        prefix_len = j
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        if prefix_len == 0:
            break
        i = i + 1

    if prefix_len > 0:
        diverged: int = 0
        k: int = 1
        while k < n:
            current, current_owned = _coerce_path_str(_path_seq_borrow(paths, k))
            if ptr_is_null(current) != 0:
                if ptr_is_null(current_owned) == 0:
                    py_decref(current_owned)
                if ptr_is_null(first_owned) == 0:
                    py_decref(first_owned)
                return null()
            current_len: int = load_i64(current, 16)
            if current_len > prefix_len:
                if load_i8(ptr_add(current, 40), prefix_len) != 47:
                    diverged = 1
            if ptr_is_null(current_owned) == 0:
                py_decref(current_owned)
            k = k + 1
        if prefix_len < first_len:
            if load_i8(first_data, prefix_len) != 47:
                diverged = 1
        if diverged != 0:
            while prefix_len > 0 and load_i8(first_data, prefix_len - 1) != 47:
                prefix_len = prefix_len - 1
        while prefix_len > 1 and load_i8(first_data, prefix_len - 1) == 47:
            prefix_len = prefix_len - 1

    out = py_str_new(first_data, prefix_len)
    if ptr_is_null(first_owned) == 0:
        py_decref(first_owned)
    return out


def _path_env_name_char(value: int) -> int:
    if value == 95:  # '_'
        return 1
    if value >= 65 and value <= 90:
        return 1
    if value >= 97 and value <= 122:
        return 1
    if value >= 48 and value <= 57:
        return 1
    return 0


def _path_cstr_len(value) -> int:
    length: int = 0
    while load_i8(value, length) != 0:
        length = length + 1
    return length


@c_abi_export("py_os_path_expandvars")
def py_os_path_expandvars(path):
    """Expand the selected platform's environment-reference syntax."""
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return null()
    data = ptr_add(item, 40)
    n: int = load_i64(item, 16)
    windows: int = _path_windows()

    has_dollar: int = 0
    probe: int = 0
    while probe < n:
        if load_i8(data, probe) == 36 or (windows and load_i8(data, probe) == 37):
            has_dollar = 1
            break
        probe = probe + 1
    if has_dollar == 0:
        out = py_str_new(data, n)
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return out

    cap: int = n + 16
    if cap < 16:
        cap = 16
    buf = malloc(cap)
    if ptr_is_null(buf) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return null()

    out_len: int = 0
    i: int = 0
    failed: int = 0
    while i < n:
        seg = ptr_add(data, i)
        seg_len: int = 1
        advance: int = 1
        current: int = load_i8(data, i)
        if windows and current == 39:  # single-quoted text is literal
            quote_end: int = i + 1
            while quote_end < n and load_i8(data, quote_end) != 39:
                quote_end = quote_end + 1
            if quote_end < n:
                quote_end = quote_end + 1
            seg_len = quote_end - i
            advance = seg_len
        elif windows and current == 37:
            percent_end: int = i + 1
            while percent_end < n and load_i8(data, percent_end) != 37:
                percent_end = percent_end + 1
            if percent_end == i + 1 and percent_end < n:
                advance = 2  # %% becomes a literal percent sign
            elif percent_end == n:
                seg_len = n - i
                advance = seg_len
            else:
                name_len3: int = percent_end - i - 1
                name3 = malloc(name_len3 + 1)
                if ptr_is_null(name3):
                    failed = 1
                else:
                    memmove(name3, ptr_add(data, i + 1), name_len3)
                    store_i8(name3, name_len3, 0)
                    value3 = getenv(name3)
                    free(name3)
                    advance = percent_end - i + 1
                    seg_len = advance
                    if not ptr_is_null(value3):
                        seg = value3
                        seg_len = _path_cstr_len(value3)
        elif current == 36 and i + 1 < n:
            next_value: int = load_i8(data, i + 1)
            if windows and next_value == 36:
                advance = 2
            elif next_value == 123:  # '{'
                end: int = i + 2
                while end < n and load_i8(data, end) != 125:  # '}'
                    end = end + 1
                if end < n:
                    name_len: int = end - (i + 2)
                    name = malloc(name_len + 1)
                    if ptr_is_null(name) != 0:
                        failed = 1
                    else:
                        memmove(name, ptr_add(data, i + 2), name_len)
                        store_i8(name, name_len, 0)
                        value = getenv(name)
                        free(name)
                        if ptr_is_null(value) == 0:
                            seg = value
                            seg_len = _path_cstr_len(value)
                        else:
                            seg = ptr_add(data, i)
                            seg_len = end - i + 1
                        advance = end - i + 1
                elif windows:
                    seg_len = n - i
                    advance = seg_len
            elif _path_env_name_char(next_value) != 0 or (windows and next_value == 45):
                end2: int = i + 1
                while end2 < n and (_path_env_name_char(load_i8(data, end2)) != 0 or (windows and load_i8(data, end2) == 45)):
                    end2 = end2 + 1
                name_len2: int = end2 - (i + 1)
                name2 = malloc(name_len2 + 1)
                if ptr_is_null(name2) != 0:
                    failed = 1
                else:
                    memmove(name2, ptr_add(data, i + 1), name_len2)
                    store_i8(name2, name_len2, 0)
                    value2 = getenv(name2)
                    free(name2)
                    if ptr_is_null(value2) == 0:
                        seg = value2
                        seg_len = _path_cstr_len(value2)
                    else:
                        seg = ptr_add(data, i)
                        seg_len = end2 - i
                    advance = end2 - i

        if failed != 0:
            break
        while out_len + seg_len + 1 > cap:
            new_cap: int = cap * 2
            grown = realloc(buf, new_cap)
            if ptr_is_null(grown) != 0:
                failed = 1
                break
            buf = grown
            cap = new_cap
        if failed != 0:
            break
        memmove(ptr_add(buf, out_len), seg, seg_len)
        out_len = out_len + seg_len
        i = i + advance

    if failed != 0:
        free(buf)
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return null()
    out2 = py_str_new(buf, out_len)
    free(buf)
    if ptr_is_null(owned) == 0:
        py_decref(owned)
    return out2


def _path_split_normalized_components(data, n: int, offsets, lengths) -> int:
    count: int = 0
    i: int = _path_drive_length(data, n)
    while i < n:
        while i < n and _path_sep(load_i8(data, i)):
            i = i + 1
        if i >= n:
            break
        start: int = i
        while i < n and not _path_sep(load_i8(data, i)):
            i = i + 1
        part_len: int = i - start
        if part_len == 1 and load_i8(data, start) == 46:
            continue
        if part_len == 2:
            if load_i8(data, start) == 46 and load_i8(data, start + 1) == 46:
                if count > 0:
                    count = count - 1
                continue
        store_i64(offsets, count * 8, start)
        store_i64(lengths, count * 8, part_len)
        count = count + 1
    return count


def _path_bytes_equal(left, right, length: int) -> int:
    i: int = 0
    while i < length:
        if load_i8(left, i) != load_i8(right, i):
            return 0
        i = i + 1
    return 1


def _path_relpath_build(path, start, slots, pins) -> None:
    if ptr_is_null(path):
        return
    path_item = path
    if _type_of(path) != PY_TYPE_STR:
        converted = py_obj_str(path)
        store_ptr(slots, 0, converted)
        if not ptr_is_null(converted):
            store_i64(pins, 0, load_i32(converted, 12) & 64)
            pcc_gc_pin(converted)
            pcc_gc_note_write_barrier(null(), converted)
        if ptr_is_null(converted):
            return
        path_item = load_ptr(slots, 0)
    if load_i64(path_item, 16) == 0:
        py_raise_owned(py_exc_new(2, cstr("no path specified")))
        return
    path_absolute = py_os_path_abspath(path_item)
    store_ptr(slots, 8, path_absolute)
    if not ptr_is_null(path_absolute):
        store_i64(pins, 8, load_i32(path_absolute, 12) & 64)
        pcc_gc_pin(path_absolute)
        pcc_gc_note_write_barrier(null(), path_absolute)
    if ptr_is_null(path_absolute):
        return
    start_absolute = py_os_path_abspath(start)
    store_ptr(slots, 16, start_absolute)
    if not ptr_is_null(start_absolute):
        store_i64(pins, 16, load_i32(start_absolute, 12) & 64)
        pcc_gc_pin(start_absolute)
        pcc_gc_note_write_barrier(null(), start_absolute)
    if ptr_is_null(start_absolute):
        return
    windows: int = _path_windows()
    separator: int = 92 if windows else 47
    path_item = load_ptr(slots, 8)
    start_item = load_ptr(slots, 16)
    path_data = ptr_add(path_item, 40)
    path_len: int = load_i64(path_item, 16)
    start_data = ptr_add(start_item, 40)
    start_len: int = load_i64(start_item, 16)
    if windows and not _path_windows_equal(path_data, _path_drive_length(path_data, path_len), start_data, _path_drive_length(start_data, start_len)):
        py_raise_owned(py_exc_new(2, cstr("path and start are on different mounts")))
        return
    path_offsets = malloc((path_len + 1) * 8)
    path_lengths = malloc((path_len + 1) * 8)
    start_offsets = malloc((start_len + 1) * 8)
    start_lengths = malloc((start_len + 1) * 8)
    if ptr_is_null(path_offsets) or ptr_is_null(path_lengths) or ptr_is_null(start_offsets) or ptr_is_null(start_lengths):
        free(path_offsets)
        free(path_lengths)
        free(start_offsets)
        free(start_lengths)
        return
    path_data = ptr_add(load_ptr(slots, 8), 40)
    path_count: int = _path_split_normalized_components(path_data, path_len, path_offsets, path_lengths)
    start_data = ptr_add(load_ptr(slots, 16), 40)
    start_count: int = _path_split_normalized_components(start_data, start_len, start_offsets, start_lengths)
    common: int = 0
    while common < path_count and common < start_count:
        # Reload from the registered owners on each iteration, including the
        # edge after a loop safepoint. Owners stay pinned while callees use
        # interior views. Dynamic views do not outlive the leaf comparison.
        path_data = ptr_add(load_ptr(slots, 8), 40)
        start_data = ptr_add(load_ptr(slots, 16), 40)
        component_len: int = load_i64(path_lengths, common * 8)
        start_component_len: int = load_i64(start_lengths, common * 8)
        path_component = ptr_add(path_data, load_i64(path_offsets, common * 8))
        start_component = ptr_add(start_data, load_i64(start_offsets, common * 8))
        if windows:
            if not _path_windows_equal(path_component, component_len, start_component, start_component_len):
                break
        elif component_len != start_component_len or memcmp_c(path_component, start_component, component_len) != 0:
            break
        common = common + 1
    up: int = start_count - common
    tail: int = path_count - common
    total: int = up + tail
    if total == 0:
        free(path_offsets)
        free(path_lengths)
        free(start_offsets)
        free(start_lengths)
        out = py_str_new(cstr("."), 1)
        store_ptr(slots, 24, out)
        if not ptr_is_null(out):
            store_i64(pins, 24, load_i32(out, 12) & 64)
            pcc_gc_pin(out)
            pcc_gc_note_write_barrier(null(), out)
        return
    output_len: int = 2 * up + total - 1
    j: int = common
    while j < path_count:
        output_len = output_len + load_i64(path_lengths, j * 8)
        j = j + 1
    out = py_str_new(null(), output_len)
    store_ptr(slots, 24, out)
    if not ptr_is_null(out):
        store_i64(pins, 24, load_i32(out, 12) & 64)
        pcc_gc_pin(out)
        pcc_gc_note_write_barrier(null(), out)
    if ptr_is_null(out):
        free(path_offsets)
        free(path_lengths)
        free(start_offsets)
        free(start_lengths)
        return
    pos: int = 0
    written: int = 0
    k: int = 0
    while k < up:
        destination = ptr_add(load_ptr(slots, 24), 40)
        if written > 0:
            store_i8(destination, pos, separator)
            pos = pos + 1
        store_i8(destination, pos, 46)
        store_i8(destination, pos + 1, 46)
        pos = pos + 2
        written = written + 1
        k = k + 1
    j2: int = common
    while j2 < path_count:
        destination = ptr_add(load_ptr(slots, 24), 40)
        path_data = ptr_add(load_ptr(slots, 8), 40)
        if written > 0:
            store_i8(destination, pos, separator)
            pos = pos + 1
        part_len2: int = load_i64(path_lengths, j2 * 8)
        part_offset2: int = load_i64(path_offsets, j2 * 8)
        memmove(ptr_add(destination, pos), ptr_add(path_data, part_offset2), part_len2)
        pos = pos + part_len2
        written = written + 1
        j2 = j2 + 1
    free(path_offsets)
    free(path_lengths)
    free(start_offsets)
    free(start_lengths)


@c_abi_export("py_os_path_relpath")
def py_os_path_relpath(path, start):
    slots = stack_alloc(32)
    pins = stack_alloc(32)
    memset(slots, 0, 32)
    memset(pins, 0, 32)
    pcc_gc_frame_enter(global_addr("pcc_os_path_relpath_frame_map"), slots)
    _path_relpath_build(path, start, slots, pins)
    _path_clear_root_prefix(slots, pins, 3)
    # The result remains pinned through the last potentially parking call.
    pcc_gc_frame_leave(slots)
    return pcc_gc_take_pinned_slot(ptr_add(slots, 24), load_i64(pins, 24))


@c_abi_export("py_os_path_exists")
def py_os_path_exists(path) -> int:
    item, owned = _coerce_path_str(path)
    if ptr_is_null(item) != 0:
        if ptr_is_null(owned) == 0:
            py_decref(owned)
        return 0

    raw = ptr_add(item, 40)
    ok: int = 0
    if access(raw, 0) == 0:        # F_OK
        ok = 1
    if ptr_is_null(owned) == 0:
        py_decref(owned)
    return ok
