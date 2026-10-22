"""Owned first-class TemporaryDirectory provider.

The manager is an ordinary managed instance. A registered cleanup state owns a
weak reference, never the manager; aliases and bound methods therefore keep the
resource alive without making the finalizer keep its referent alive. Every
managed edge uses the existing class/list/tuple/weakref slot contract.

Currently supported filesystem arguments are str or None. Bytes and custom
PathLike inputs fail explicitly; their encoding contract is not approximated by
str(). ResourceWarning filtering is still an owned-runtime capability gap.
"""

__pcc_runtime_port__ = True

from pcc.extern import (
    extern, c_abi_export, c_int32, c_int64, c_ptr, c_void,
)
from pcc.runtime.py.py_abi_constants import (
    C_POINTER_SIZE, PYOBJECTHEADER_FLAGS_OFFSET, PYOBJECTHEADER_TYPE_TAG_OFFSET,
    PY_FLAG_GC_PINNED, PY_TYPE_STR, PY_TYPE_BYTES, PY_TYPE_BYTEARRAY,
    PY_TYPE_MEMORYVIEW, PY_TYPE_LIST,
    PY_TYPE_INT, PY_TYPE_BOOL,
    PYSTROBJECT_BYTE_LEN_OFFSET, PYBYTESOBJECT_BYTE_LEN_OFFSET, PYBYTESOBJECT_DATA_OFFSET,
)
from pcc.unsafe import (
    atomic_cas_i64, atomic_load_i64, close, cstr, function_addr, define_global_i32,
    define_global_i64, define_global_ptr_null, free, global_addr,
    global_load_ptr, global_store_ptr, int_to_ptr, is_tagged_int,
    load_i8, load_i32, load_i64, load_ptr, malloc, memcpy, memset,
    null, open_file_flags, ptr_add, ptr_eq, ptr_is_null, ptr_to_int, readlink,
    stack_alloc, stat_kind, store_i8, store_i32, store_i64, store_ptr,
    strlen, target_sys_platform, unlinkat,
)

py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)
py_str_concat = extern("py_str_concat", (c_ptr, c_ptr), c_ptr)
py_obj_repr = extern("py_obj_repr", (c_ptr,), c_ptr)
py_obj_truthy = extern("py_obj_truthy", (c_ptr,), c_int64)
py_obj_getattr = extern("py_obj_getattr", (c_ptr, c_ptr), c_ptr)
py_obj_call = extern("py_obj_call", (c_ptr, c_ptr, c_ptr), c_ptr)
py_obj_setattr = extern("py_obj_setattr", (c_ptr, c_ptr, c_ptr), c_int64)
py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
py_index_i64_checked_slots = extern("py_index_i64_checked_slots", (c_ptr,), c_int64)
py_bytes_from_obj = extern("py_bytes_from_obj", (c_ptr,), c_ptr)
pcc_platform_pwrite = extern(
    "pcc_platform_pwrite", (c_int64, c_ptr, c_int64, c_int64), c_int64
)
pcc_platform_ftruncate = extern(
    "pcc_platform_ftruncate", (c_int64, c_int64), c_int64
)
pcc_thread_safepoint = extern("pcc_thread_safepoint", (), c_void)
py_file_fdopen_options = extern("py_file_fdopen_options",
    (c_ptr, c_ptr, c_ptr, c_ptr, c_ptr, c_int64, c_int64), c_ptr)
py_file_fspath = extern("py_file_fspath", (c_ptr,), c_ptr)
py_file_close = extern("py_file_close", (c_ptr,), c_void)
py_file_close_checked = extern("py_file_close_checked", (c_ptr,), c_void)
py_instance_new = extern("py_instance_new", (c_ptr,), c_ptr)
py_obj_next = extern("py_obj_next", (c_ptr,), c_ptr)
py_builtin_callable = extern("py_builtin_callable", (c_ptr,), c_ptr)
strcmp = extern("strcmp", (c_ptr, c_ptr), c_int32)
py_list_new = extern("py_list_new", (c_int64,), c_ptr)
py_list_get = extern("py_list_get", (c_ptr, c_int64), c_ptr)
py_list_len = extern("py_list_len", (c_ptr,), c_int64)
py_list_set = extern("py_list_set", (c_ptr, c_int64, c_ptr), c_void)
py_list_setitem = extern("py_list_setitem", (c_ptr, c_int64, c_ptr), c_int64)
py_list_append = extern("py_list_append", (c_ptr, c_ptr), c_void)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_get = extern("py_tuple_get", (c_ptr, c_int64), c_ptr)
py_tuple_len = extern("py_tuple_len", (c_ptr,), c_int64)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_func_new_named = extern("py_func_new_named", (c_ptr, c_ptr, c_ptr), c_ptr)
py_class_new = extern("py_class_new", (c_ptr, c_ptr, c_int32, c_ptr, c_int32), c_ptr)
py_class_setattr = extern("py_class_setattr", (c_ptr, c_ptr, c_ptr), c_int64)
py_class_add_method = extern("py_class_add_method", (c_ptr, c_ptr, c_ptr), c_void)
py_weakref_new = extern("py_weakref_new", (c_ptr, c_ptr), c_ptr)
py_os_path_abspath = extern("py_os_path_abspath", (c_ptr,), c_ptr)
py_os_path_join = extern("py_os_path_join", (c_ptr,), c_ptr)
py_os_path_exists = extern("py_os_path_exists", (c_ptr,), c_int64)
py_tls_exc_swap_slot = extern("py_tls_exc_swap_slot", (c_ptr,), c_void)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
pcc_gc_unpin = extern("pcc_gc_unpin", (c_ptr,), c_void)
pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
pcc_gc_frame_leave = extern("pcc_gc_frame_leave", (c_ptr,), c_void)
pcc_gc_store_root = extern("pcc_gc_store_root", (c_ptr, c_ptr), c_void)
pcc_gc_take_pinned_slot = extern("pcc_gc_take_pinned_slot", (c_ptr, c_int64), c_ptr)
pcc_gc_note_write_barrier = extern("pcc_gc_note_write_barrier", (c_ptr, c_ptr), c_void)
pcc_gc_scheduler_root_register_handle = extern("pcc_gc_scheduler_root_register_handle", (c_ptr,), c_ptr)
pcc_gc_scheduler_root_unregister_handle = extern("pcc_gc_scheduler_root_unregister_handle", (c_ptr,), c_void)
pcc_gc_root_copy_lease = extern("pcc_gc_root_copy_lease", (c_ptr, c_ptr), c_int64)
pcc_gc_foreign_lease_release = extern("pcc_gc_foreign_lease_release", (c_ptr, c_int64), c_int64)
pcc_mutex_new = extern("pcc_mutex_new", (), c_ptr)
pcc_mutex_free = extern("pcc_mutex_free", (c_ptr,), c_void)
pcc_mutex_lock = extern("pcc_mutex_lock", (c_ptr,), c_int64)
pcc_mutex_unlock = extern("pcc_mutex_unlock", (c_ptr,), c_int64)
getenv = extern("pcc_platform_getenv", (c_ptr,), c_ptr)
mkdtemps = extern("pcc_platform_mkdtemp_suffix", (c_ptr, c_int64), c_int64)
create_owned = extern("pcc_platform_tempdir_create_owned", (c_ptr, c_int64, c_ptr), c_int64)
remove_owned = extern("pcc_platform_tempdir_remove_owned", (c_ptr,), c_int64)
close_owned = extern("pcc_platform_tempdir_close_owned", (c_ptr,), c_int64)
mkstemps = extern("pcc_platform_mkstemp_suffix", (c_ptr, c_int64), c_int64)
remove_tree = extern("pcc_platform_tempdir_remove_tree", (c_ptr,), c_int64)
atexit = extern("atexit", (c_ptr,), c_int32)
errno_message = extern("pcc_errno_message_into", (c_int32, c_ptr, c_int64), c_int32)

# Named fixed frame, shared by leaf entrypoints. Every slot owns one reference.
_TD_SLOTS = 24
_TD_OUTPUT = 23
# Public mkdtemp entry and callable-factory views of the shared fixed frame.
# Both publish through _TD_OUTPUT; their independent lifetimes reuse slot 0.
_MKDTEMP_SUFFIX = 0
_MKDTEMP_PREFIX = 1
_MKDTEMP_DIRECTORY = 2
_MKDTEMP_ARGUMENT_COUNT = 3
_MKDTEMP_CALLABLE_CANDIDATE = 0
# fdopen callback view; values remain rooted throughout option validation.
_FDOPEN_DESCRIPTOR = 0
_FDOPEN_MODE = 1
_FDOPEN_BUFFERING = 2
_FDOPEN_ENCODING = 3
_FDOPEN_ERRORS = 4
_FDOPEN_NEWLINE = 5
_FDOPEN_CLOSEFD = 6
_FDOPEN_OPENER = 7
_FDOPEN_ARGUMENT_COUNT = 8
# Callable factory/signature view of the same fixed frame.
_TYPE_CANDIDATE = 0
_TYPE_METHOD = 9
_TYPE_MODULE = 10
_FILE_CALLABLE_CANDIDATE = 0
_FILE_SIGNATURE_NAMES = 1
_FILE_SIGNATURE_KINDS = 2
_FILE_SIGNATURE_PRESENT = 3
_FILE_SIGNATURE_DEFAULTS = 4
_FILE_SIGNATURE_ITEM = 5
_FILE_SIGNATURE = 6
_FILE_CAPTURES_EMPTY = 7
_FILE_CAPTURES = 8
# NamedTemporaryFile argument/scratch view of the shared owned frame.
_NT_MODE = 0
_NT_BUFFERING = 1
_NT_ENCODING = 2
_NT_NEWLINE = 3
_NT_SUFFIX = 4
_NT_PREFIX = 5
_NT_DIRECTORY = 6
_NT_DELETE = 7
_NT_ERRORS = 8
_NT_DELETE_ON_CLOSE = 9
_NT_ARGUMENT_COUNT = 10
_NT_PATH = 10
_NT_FILE = 11
_NT_WRAPPER = 12
_NT_NORMALIZED_DIRECTORY = 13
_NT_TYPE = 14
_NT_RAW_NAME = 15
_NT_DESCRIPTOR = 16
# Wrapper method/cleanup view; args and captures are independently retained.
_NT_SELF = 0
_NT_METHOD_FILE = 1
_NT_METHOD_NAME = 2
_NT_METHOD_VALUE = 3
_NT_METHOD_CAPTURES = 4
_NT_METHOD_ARGS = 5
_NT_METHOD_BOOL = 6
_NT_CLEANUP_PATH = 2
_NT_CLEANUP_DELETE = 3
_NT_CLEANUP_ON_CLOSE = 4
_NT_CLEANUP_REMOVED = 5
_NT_CLEANUP_READY = 6
_NT_CLOSE_ERROR = 21
_OS_OPEN_PATH = 0
_OS_OPEN_FLAGS = 1
_OS_OPEN_MODE = 2
_OS_OPEN_DIR_FD = 3
_OS_OPEN_NORMALIZED = 4
# Positional descriptor operations share this named, independently rooted view.
_OS_POSITIONAL_DESCRIPTOR = 0
_OS_POSITIONAL_BUFFER = 1
_OS_POSITIONAL_OFFSET = 2
_OS_POSITIONAL_SNAPSHOT = 3
_OS_TRUNCATE_LENGTH = 1
# Registered native record: only state is managed, with its own root handle.
_TD_NEXT = 0
_TD_PREVIOUS = 8
_TD_STATE = 16
_TD_HANDLE = 24
_TD_FILESYSTEM_OWNER = 32
_TD_RECORD_SIZE = 40
# Managed finalizer state: no strong edge points back to its manager.
_TD_NAME = 0
_TD_IGNORE = 1
_TD_DELETE = 2
_TD_WEAKREF = 3
_TD_SAVED_ERROR = 22

define_global_i32("pcc_tempdir_frame_map", _TD_SLOTS)
define_global_i64("pcc_tempdir_mutex_bits", 0)
define_global_ptr_null("pcc_tempdir_class")
define_global_ptr_null("pcc_tempdir_class_root_handle")
define_global_ptr_null("pcc_tempdir_records")
define_global_ptr_null("pcc_tempfile_mkdtemp_function")
define_global_ptr_null("pcc_tempfile_mkdtemp_root_handle")
define_global_ptr_null("pcc_file_fdopen_function")
define_global_ptr_null("pcc_file_fdopen_root_handle")
define_global_ptr_null("pcc_namedtempfile_function")
define_global_ptr_null("pcc_namedtempfile_root_handle")
define_global_ptr_null("pcc_namedtempfile_wrapper_type")
define_global_ptr_null("pcc_namedtempfile_wrapper_root_handle")
define_global_ptr_null("pcc_os_open_function")
define_global_ptr_null("pcc_os_open_root_handle")
define_global_ptr_null("pcc_os_close_function")
define_global_ptr_null("pcc_os_close_root_handle")
define_global_ptr_null("pcc_os_pwrite_function")
define_global_ptr_null("pcc_os_pwrite_root_handle")
define_global_ptr_null("pcc_os_ftruncate_function")
define_global_ptr_null("pcc_os_ftruncate_root_handle")


def _td_none():
    return global_load_ptr("py_None")


def _td_error(tag: int, message):
    py_raise_owned(py_exc_new(tag, message))
    return null()


def _td_hold(slots, pins, index: int, value) -> None:
    # A NEW return enters its registered slot before the next safepoint.
    offset: int = index * C_POINTER_SIZE
    if ptr_is_null(value) and not py_err_occurred():
        _td_error(19, cstr("TemporaryDirectory owned object allocation failed"))
    store_ptr(slots, offset, value)
    if not ptr_is_null(value) and not is_tagged_int(value):
        store_i64(pins, offset, load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & PY_FLAG_GC_PINNED)
        pcc_gc_pin(value)
        pcc_gc_note_write_barrier(null(), value)


def _td_drop(slots, pins, index: int) -> None:
    offset: int = index * C_POINTER_SIZE
    value = load_ptr(slots, offset)
    if not ptr_is_null(value) and not is_tagged_int(value):
        pcc_gc_unpin(value)
        keep_result: int = ptr_eq(value, load_ptr(slots, _TD_OUTPUT * C_POINTER_SIZE))
        if keep_result and not load_i64(pins, offset):
            store_i64(pins, _TD_OUTPUT * C_POINTER_SIZE, 0)
        if load_i64(pins, offset) or keep_result:
            store_i32(value, PYOBJECTHEADER_FLAGS_OFFSET,
                      load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) | PY_FLAG_GC_PINNED)
    pcc_gc_store_root(ptr_add(slots, offset), null())
    store_i64(pins, offset, 0)


def _td_finish(slots, pins):
    index: int = _TD_OUTPUT
    while index > 0:
        index = index - 1
        _td_drop(slots, pins, index)
    pcc_gc_frame_leave(slots)
    return pcc_gc_take_pinned_slot(ptr_add(slots, _TD_OUTPUT * C_POINTER_SIZE),
                                   load_i64(pins, _TD_OUTPUT * C_POINTER_SIZE))


def _td_mutex():
    slot = global_addr("pcc_tempdir_mutex_bits")
    bits: int = atomic_load_i64(slot, 0, "acquire")
    if bits != 0:
        return int_to_ptr(bits)
    candidate = pcc_mutex_new()
    if ptr_is_null(candidate):
        _td_error(19, cstr("TemporaryDirectory registry allocation failed"))
        return null()
    previous: int = atomic_cas_i64(slot, 0, 0, ptr_to_int(candidate), "acq_rel", "acquire")
    if previous != 0:
        pcc_mutex_free(candidate)
        return int_to_ptr(previous)
    return candidate


def _td_os_error(status: int, name):
    tag: int = 14
    if status == -2:
        tag = 34
    elif status == -17:
        tag = 35
    elif status == -13 or status == -1:
        tag = 36
    elif status == -20:
        tag = 38
    slots = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    pins = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    memset(slots, 0, _TD_SLOTS * C_POINTER_SIZE)
    memset(pins, 0, _TD_SLOTS * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_tempdir_frame_map"), slots)
    py_incref(name)
    _td_hold(slots, pins, 0, name)
    message = stack_alloc(256)
    errno_message(0 - status, message, 256)
    _td_hold(slots, pins, 1, py_exc_new(tag, message))
    _td_hold(slots, pins, 2, py_int_from_i64(0 - status))
    _td_hold(slots, pins, 3, py_str_new(message, strlen(message)))
    _td_hold(slots, pins, 4, py_tuple_new(2))
    exc = load_ptr(slots, C_POINTER_SIZE)
    if not ptr_is_null(exc) and not py_err_occurred():
        py_tuple_set_item(load_ptr(slots, 4 * C_POINTER_SIZE), 0, load_ptr(slots, 2 * C_POINTER_SIZE))
        py_tuple_set_item(load_ptr(slots, 4 * C_POINTER_SIZE), 1, load_ptr(slots, 3 * C_POINTER_SIZE))
        if py_obj_setattr(exc, cstr("errno"), load_ptr(slots, 2 * C_POINTER_SIZE)) != 0:
            return _td_finish(slots, pins)
        if py_obj_setattr(exc, cstr("strerror"), load_ptr(slots, 3 * C_POINTER_SIZE)) != 0:
            return _td_finish(slots, pins)
        if py_obj_setattr(exc, cstr("filename"), load_ptr(slots, 0)) != 0:
            return _td_finish(slots, pins)
        if py_obj_setattr(exc, cstr("args"), load_ptr(slots, 4 * C_POINTER_SIZE)) != 0:
            return _td_finish(slots, pins)
        if not py_err_occurred():
            py_incref(exc)
            py_raise_owned(exc)
    return _td_finish(slots, pins)


def _td_text(value, default):
    if ptr_eq(value, _td_none()):
        return default
    if ptr_is_null(value) or is_tagged_int(value):
        return _td_error(3, cstr("TemporaryDirectory paths must be strings"))
    tag: int = load_i32(value, PYOBJECTHEADER_TYPE_TAG_OFFSET)
    if tag != PY_TYPE_STR:
        if tag == PY_TYPE_BYTES:
            return _td_error(11, cstr("pcc TemporaryDirectory: bytes paths require an owned filesystem encoding provider"))
        return _td_error(11, cstr("pcc TemporaryDirectory: custom PathLike arguments require an owned fspath provider"))
    raw = py_str_utf8(value)
    if ptr_is_null(raw):
        return null()
    # utf8 byte length is the native string payload length, not code points.
    length: int = load_i64(value, PYSTROBJECT_BYTE_LEN_OFFSET)
    index: int = 0
    while index < length:
        if load_i8(raw, index) == 0:
            return _td_error(2, cstr("embedded null character"))
        index = index + 1
    return raw


# These malloc/free buffers stay raw through the platform-selection join and
# its caller; they must never acquire a managed reference or root slot.
def _td_path_template(root, prefix, suffix) -> c_ptr:
    rlen: int = strlen(root)
    if load_i8(prefix, 0) == 47:
        rlen = 0
    plen: int = strlen(prefix)
    slen: int = strlen(suffix)
    slash: int = 0
    if rlen > 0 and load_i8(root, rlen - 1) != 47:
        slash = 1
    size: int = rlen + slash + plen + 6 + slen
    path = malloc(size + 1)
    if ptr_is_null(path):
        return null()
    memcpy(path, root, rlen)
    if slash:
        store_i8(path, rlen, 47)
    memcpy(ptr_add(path, rlen + slash), prefix, plen)
    memcpy(ptr_add(path, rlen + slash + plen), cstr("XXXXXX"), 6)
    memcpy(ptr_add(path, size - slen), suffix, slen)
    store_i8(path, size, 0)
    return path


def _td_windows_path_template(root, prefix, suffix) -> c_ptr:
    # Use the existing owned Windows drive/UNC join implementation, and fix
    # the absolute spelling BEFORE creation (never resolve a later cwd).
    slots = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    pins = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    memset(slots, 0, _TD_SLOTS * C_POINTER_SIZE)
    memset(pins, 0, _TD_SLOTS * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_tempdir_frame_map"), slots)
    raw = _td_path_template(cstr(""), prefix, suffix)
    result = null()
    if not ptr_is_null(raw):
        _td_hold(slots, pins, 0, py_str_new(root, strlen(root)))
        _td_hold(slots, pins, 1, py_str_new(raw, strlen(raw)))
        free(raw)
        _td_hold(slots, pins, 2, py_tuple_new(2))
        if not py_err_occurred():
            py_tuple_set_item(load_ptr(slots, 2 * C_POINTER_SIZE), 0, load_ptr(slots, 0))
            py_tuple_set_item(load_ptr(slots, 2 * C_POINTER_SIZE), 1, load_ptr(slots, C_POINTER_SIZE))
            _td_hold(slots, pins, 3, py_os_path_join(load_ptr(slots, 2 * C_POINTER_SIZE)))
            if not py_err_occurred():
                _td_hold(slots, pins, 4, py_os_path_abspath(load_ptr(slots, 3 * C_POINTER_SIZE)))
                if not py_err_occurred():
                    text = py_str_utf8(load_ptr(slots, 4 * C_POINTER_SIZE))
                    if not ptr_is_null(text):
                        length: int = strlen(text)
                        result = malloc(length + 1)
                        if not ptr_is_null(result):
                            memcpy(result, text, length + 1)
    _td_finish(slots, pins)
    return result


def _td_mkdir_at(root, prefix, suffix, status, owner) -> c_ptr:
    previous_failure = load_ptr(status, C_POINTER_SIZE)
    if not ptr_is_null(previous_failure):
        free(previous_failure)
    store_ptr(status, C_POINTER_SIZE, null())
    path = _td_windows_path_template(root, prefix, suffix) if load_i8(target_sys_platform(), 0) == 119 else _td_path_template(root, prefix, suffix)
    if ptr_is_null(path):
        store_i64(status, 0, -12)
        return null()
    result: int = create_owned(path, strlen(suffix), owner) if not ptr_is_null(owner) else mkdtemps(path, strlen(suffix))
    store_i64(status, 0, result)
    if result < 0:
        if not ptr_is_null(owner) and not ptr_is_null(load_ptr(owner, 0)):
            _td_remove_created(path, owner)
        store_ptr(status, C_POINTER_SIZE, path)
        return null()
    return path


def _td_remove_created(path, owner) -> int:
    if load_i8(target_sys_platform(), 0) == 119:
        handle = load_ptr(owner, 0)
        if ptr_is_null(handle):
            return 0
        status: int = remove_owned(handle)
        closed: int = close_owned(handle)
        store_ptr(owner, 0, null())
        return status if status < 0 else closed
    return remove_tree(path)


def _td_mkdtemp(suffix, prefix, directory, slots, pins, owner):
    store_ptr(owner, 0, null())
    suffix_raw = _td_text(suffix, cstr(""))
    if ptr_is_null(suffix_raw):
        return null()
    prefix_raw = _td_text(prefix, cstr("tmp"))
    if ptr_is_null(prefix_raw):
        return null()
    status = stack_alloc(16)
    store_i64(status, 0, -2)
    store_ptr(status, C_POINTER_SIZE, null())
    path = null()
    if not ptr_eq(directory, _td_none()):
        root = _td_text(directory, cstr(""))
        if ptr_is_null(root):
            return null()
        path = _td_mkdir_at(root, prefix_raw, suffix_raw, status, owner)
    else:
        # CPython's default directory search order. A failed candidate does
        # not become an empty successful name and does not suppress errors.
        candidate: int = 0
        windows: int = load_i8(target_sys_platform(), 0) == 119
        probe_owner = stack_alloc(C_POINTER_SIZE)
        while candidate < (10 if windows else 7) and ptr_is_null(path):
            root = null()
            if candidate == 0:
                root = getenv(cstr("TMPDIR"))
            elif candidate == 1:
                root = getenv(cstr("TEMP"))
            elif candidate == 2:
                root = getenv(cstr("TMP"))
            elif windows and candidate == 3:
                root = getenv(cstr("USERPROFILE"))
                if not ptr_is_null(root) and load_i8(root, 0) != 0:
                    _td_hold(slots, pins, 12, py_str_new(root, strlen(root)))
                    _td_hold(slots, pins, 13, py_str_new(cstr("\\AppData\\Local\\Temp"), 19))
                    _td_hold(slots, pins, 14, py_str_concat(load_ptr(slots, 12 * C_POINTER_SIZE), load_ptr(slots, 13 * C_POINTER_SIZE)))
                    if not py_err_occurred():
                        root = py_str_utf8(load_ptr(slots, 14 * C_POINTER_SIZE))
            elif windows and candidate == 4:
                root = getenv(cstr("SYSTEMROOT"))
                if not ptr_is_null(root) and load_i8(root, 0) != 0:
                    _td_hold(slots, pins, 15, py_str_new(root, strlen(root)))
                    _td_hold(slots, pins, 16, py_str_new(cstr("\\Temp"), 5))
                    _td_hold(slots, pins, 17, py_str_concat(load_ptr(slots, 15 * C_POINTER_SIZE), load_ptr(slots, 16 * C_POINTER_SIZE)))
                    if not py_err_occurred():
                        root = py_str_utf8(load_ptr(slots, 17 * C_POINTER_SIZE))
            elif windows and candidate == 5:
                root = cstr("C:\\TEMP")
            elif windows and candidate == 6:
                root = cstr("C:\\TMP")
            elif windows and candidate == 7:
                root = cstr("\\TEMP")
            elif windows and candidate == 8:
                root = cstr("\\TMP")
            elif windows:
                root = cstr(".")
            elif candidate == 3:
                root = cstr("/tmp")
            elif candidate == 4:
                root = cstr("/var/tmp")
            elif candidate == 5:
                root = cstr("/usr/tmp")
            else:
                root = cstr(".")
            if py_err_occurred():
                break
            if not ptr_is_null(root) and load_i8(root, 0) != 0:
                # Select the root independently of user prefix/suffix errors.
                # Once a writable candidate is found, errors in the requested
                # name must propagate instead of trying a different directory.
                store_ptr(probe_owner, 0, null())
                probe_path = _td_mkdir_at(root, cstr(".pcc_probe_"), cstr(""), status, probe_owner)
                if not ptr_is_null(probe_path):
                    probe_status: int = _td_remove_created(probe_path, probe_owner)
                    if probe_status < 0:
                        store_i64(status, 0, probe_status)
                        store_ptr(status, C_POINTER_SIZE, probe_path)
                        break
                    free(probe_path)
                    path = _td_mkdir_at(root, prefix_raw, suffix_raw, status, owner)
                    break
            candidate = candidate + 1
    if ptr_is_null(path):
        failed_path = load_ptr(status, C_POINTER_SIZE)
        if not ptr_is_null(failed_path):
            _td_hold(slots, pins, 8, py_str_new(failed_path, strlen(failed_path)))
            free(failed_path)
            if not ptr_is_null(load_ptr(slots, 8 * C_POINTER_SIZE)) and not py_err_occurred():
                return _td_os_error(load_i64(status, 0), load_ptr(slots, 8 * C_POINTER_SIZE))
            return null()
        return _td_os_error(load_i64(status, 0), directory)
    _td_hold(slots, pins, 8, py_str_new(path, strlen(path)))
    if ptr_is_null(load_ptr(slots, 8 * C_POINTER_SIZE)):
        _td_remove_created(path, owner)
        free(path)
        return null()
    # Since 3.12 mkdtemp returns an absolute path even for relative dir.
    result = null()
    if load_i8(target_sys_platform(), 0) == 119:
        result = load_ptr(slots, 8 * C_POINTER_SIZE)
        py_incref(result)
    else:
        result = py_os_path_abspath(load_ptr(slots, 8 * C_POINTER_SIZE))
    if ptr_is_null(result):
        _td_remove_created(path, owner)
    free(path)
    return result


def _td_remove(name, ignore) -> None:
    raw = _td_text(name, null())
    if ptr_is_null(raw):
        return
    ignored: int = py_obj_truthy(ignore)
    if py_err_occurred():
        return
    # Never follow or unlink a replaced root symlink as if it were a tree.
    probe = stack_alloc(1)
    if readlink(raw, probe, 1) >= 0:
        # shutil.rmtree's root symlink refusal is OSError without an errno.
        # ignore_cleanup_errors does not authorize following the target.
        if ignored == 0:
            _td_error(14, cstr("Cannot call rmtree on a symbolic link"))
        return
    status: int = remove_tree(raw)
    if status < 0 and status != -2 and ignored == 0:
        _td_os_error(status, name)


def _td_register(state, owner) -> int:
    record = malloc(_TD_RECORD_SIZE)
    if ptr_is_null(record):
        _td_error(19, cstr("TemporaryDirectory finalizer allocation failed"))
        return -1
    memset(record, 0, _TD_RECORD_SIZE)
    handle = pcc_gc_scheduler_root_register_handle(ptr_add(record, _TD_STATE))
    if ptr_is_null(handle):
        free(record)
        _td_error(19, cstr("TemporaryDirectory finalizer root registration failed"))
        return -1
    store_ptr(record, _TD_HANDLE, handle)
    store_ptr(record, _TD_FILESYSTEM_OWNER, owner)
    pcc_gc_store_root(ptr_add(record, _TD_STATE), state)
    mutex = _td_mutex()
    if ptr_is_null(mutex):
        pcc_gc_scheduler_root_unregister_handle(handle)
        pcc_gc_store_root(ptr_add(record, _TD_STATE), null())
        free(record)
        return -1
    pcc_mutex_lock(mutex)
    previous = global_load_ptr("pcc_tempdir_records")
    store_ptr(record, _TD_NEXT, previous)
    if not ptr_is_null(previous):
        store_ptr(previous, _TD_PREVIOUS, record)
    global_store_ptr("pcc_tempdir_records", record)
    pcc_mutex_unlock(mutex)
    return 0


def _td_detach(state) -> int:
    mutex = _td_mutex()
    if ptr_is_null(mutex):
        return 0
    pcc_mutex_lock(mutex)
    # State identity is resolved under the shared graph lock. No native
    # address is exposed in mutable Python attributes or collection items.
    pcc_py_gc_minor_graph_lock()
    record = global_load_ptr("pcc_tempdir_records")
    while not ptr_is_null(record):
        current = pcc_gc_load_ptr(null(), ptr_add(record, _TD_STATE))
        if ptr_eq(current, state):
            break
        record = load_ptr(record, _TD_NEXT)
    pcc_py_gc_minor_graph_unlock()
    if ptr_is_null(record):
        pcc_mutex_unlock(mutex)
        return 0
    owner = load_ptr(record, _TD_FILESYSTEM_OWNER)
    closed: int = 0
    if not ptr_is_null(owner):
        closed = close_owned(owner)
        store_ptr(record, _TD_FILESYSTEM_OWNER, null())
    # Unlink before dropping a weakref or registry owner can call arbitrary
    # finalizers. Repeated/manual/weakref/shutdown paths converge here.
    previous = load_ptr(record, _TD_PREVIOUS)
    following = load_ptr(record, _TD_NEXT)
    if ptr_is_null(previous):
        global_store_ptr("pcc_tempdir_records", following)
    else:
        store_ptr(previous, _TD_NEXT, following)
    if not ptr_is_null(following):
        store_ptr(following, _TD_PREVIOUS, previous)
    pcc_mutex_unlock(mutex)
    py_list_set(state, _TD_WEAKREF, _td_none())
    pcc_gc_scheduler_root_unregister_handle(load_ptr(record, _TD_HANDLE))
    pcc_gc_store_root(ptr_add(record, _TD_STATE), null())
    free(record)
    return closed if closed < 0 else 1


def _td_windows_cleanup(state, delete: int, terminal: int) -> int:
    # Serialize raw handle use; no managed callbacks run while this lock is
    # held. A failed explicit cleanup retains its owner for a later retry.
    mutex = _td_mutex()
    if ptr_is_null(mutex):
        return -12
    pcc_mutex_lock(mutex)
    pcc_py_gc_minor_graph_lock()
    record = global_load_ptr("pcc_tempdir_records")
    while not ptr_is_null(record):
        current = pcc_gc_load_ptr(null(), ptr_add(record, _TD_STATE))
        if ptr_eq(current, state):
            break
        record = load_ptr(record, _TD_NEXT)
    pcc_py_gc_minor_graph_unlock()
    result: int = 0
    if not ptr_is_null(record):
        owner = load_ptr(record, _TD_FILESYSTEM_OWNER)
        if not ptr_is_null(owner) and delete:
            result = remove_owned(owner)
        if result == 0 and not terminal and not ptr_is_null(owner):
            result = close_owned(owner)
            if result == 0:
                store_ptr(record, _TD_FILESYSTEM_OWNER, null())
    pcc_mutex_unlock(mutex)
    if terminal or result == 0:
        detached: int = _td_detach(state)
        if result == 0 and detached < 0:
            result = detached
    return result


def _td_finalize_state(state) -> None:
    slots = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    pins = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    memset(slots, 0, _TD_SLOTS * C_POINTER_SIZE)
    memset(pins, 0, _TD_SLOTS * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_tempdir_frame_map"), slots)
    py_incref(state)
    _td_hold(slots, pins, 0, state)
    if load_i8(target_sys_platform(), 0) == 119:
        _td_hold(slots, pins, 1, py_list_get(load_ptr(slots, 0), _TD_DELETE))
        should_delete: int = py_obj_truthy(load_ptr(slots, C_POINTER_SIZE))
        # Even a failing user truth conversion must release the OS resource
        # and registry entry; shutdown must not revisit an undetachable node.
        status: int = _td_windows_cleanup(load_ptr(slots, 0),
            should_delete if not py_err_occurred() else 0, 1)
        if status < 0 and not py_err_occurred():
            _td_hold(slots, pins, 2, py_list_get(load_ptr(slots, 0), _TD_NAME))
            _td_hold(slots, pins, 3, py_list_get(load_ptr(slots, 0), _TD_IGNORE))
            ignored: int = py_obj_truthy(load_ptr(slots, 3 * C_POINTER_SIZE))
            if not ignored and not py_err_occurred():
                _td_os_error(status, load_ptr(slots, 2 * C_POINTER_SIZE))
    elif _td_detach(load_ptr(slots, 0)):
        _td_hold(slots, pins, 1, py_list_get(load_ptr(slots, 0), _TD_DELETE))
        should_delete: int = py_obj_truthy(load_ptr(slots, C_POINTER_SIZE))
        if should_delete and not py_err_occurred():
            _td_hold(slots, pins, 2, py_list_get(load_ptr(slots, 0), _TD_NAME))
            _td_hold(slots, pins, 3, py_list_get(load_ptr(slots, 0), _TD_IGNORE))
            _td_remove(load_ptr(slots, 2 * C_POINTER_SIZE), load_ptr(slots, 3 * C_POINTER_SIZE))
    _td_finish(slots, pins)


def _td_weakref_callback(captures, args):
    slots = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    pins = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    memset(slots, 0, _TD_SLOTS * C_POINTER_SIZE)
    memset(pins, 0, _TD_SLOTS * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_tempdir_frame_map"), slots)
    _td_hold(slots, pins, 0, py_tuple_get(captures, 0))
    if not ptr_is_null(load_ptr(slots, 0)):
        _td_finalize_state(load_ptr(slots, 0))
    if not py_err_occurred():
        py_incref(_td_none())
        _td_hold(slots, pins, _TD_OUTPUT, _td_none())
    return _td_finish(slots, pins)


def _td_shutdown() -> None:
    # Reverse creation order, as weakref.finalize's default atexit policy.
    slots = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    pins = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    memset(slots, 0, _TD_SLOTS * C_POINTER_SIZE)
    memset(pins, 0, _TD_SLOTS * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_tempdir_frame_map"), slots)
    mutex = _td_mutex()
    while not ptr_is_null(mutex):
        pcc_mutex_lock(mutex)
        record = global_load_ptr("pcc_tempdir_records")
        token: int = -1
        if not ptr_is_null(record):
            token = pcc_gc_root_copy_lease(slots, ptr_add(record, _TD_STATE))
        pcc_mutex_unlock(mutex)
        if ptr_is_null(record) or token < 0:
            break
        _td_finalize_state(load_ptr(slots, 0))
        pcc_gc_foreign_lease_release(slots, token)
        pcc_gc_store_root(slots, null())
        py_clear_exception()
    _td_finish(slots, pins)


@c_abi_export("pcc_tempdir_init_entry")
def _td_init(captures, args):
    slots = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    pins = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    memset(slots, 0, _TD_SLOTS * C_POINTER_SIZE)
    memset(pins, 0, _TD_SLOTS * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_tempdir_frame_map"), slots)
    owner = stack_alloc(C_POINTER_SIZE)
    store_ptr(owner, 0, null())
    index: int = 0
    while index < 6:
        _td_hold(slots, pins, index, py_tuple_get(args, index))
        index = index + 1
    _td_hold(slots, pins, 6, _td_mkdtemp(load_ptr(slots, C_POINTER_SIZE),
        load_ptr(slots, 2 * C_POINTER_SIZE), load_ptr(slots, 3 * C_POINTER_SIZE), slots, pins, owner))
    if not ptr_is_null(load_ptr(slots, 6 * C_POINTER_SIZE)):
        _td_hold(slots, pins, 7, py_list_new(4))
        state = load_ptr(slots, 7 * C_POINTER_SIZE)
        if not ptr_is_null(state) and not py_err_occurred():
            # py_list_new reserves capacity, not logical elements. Populate
            # owned slots before exposing the finalizer state to callbacks.
            py_list_append(state, load_ptr(slots, 6 * C_POINTER_SIZE))
            if not py_err_occurred():
                py_list_append(state, load_ptr(slots, 4 * C_POINTER_SIZE))
            if not py_err_occurred():
                py_list_append(state, load_ptr(slots, 5 * C_POINTER_SIZE))
            if not py_err_occurred():
                py_list_append(state, _td_none())
            if not py_err_occurred():
                state_length: int = py_list_len(state)
                if not py_err_occurred() and state_length != 4:
                    _td_error(19, cstr("TemporaryDirectory finalizer state initialization failed"))
        if not ptr_is_null(state) and not py_err_occurred():
            _td_hold(slots, pins, 9, py_tuple_new(1))
            if not ptr_is_null(load_ptr(slots, 9 * C_POINTER_SIZE)):
                py_tuple_set_item(load_ptr(slots, 9 * C_POINTER_SIZE), 0, state)
                _td_hold(slots, pins, 10, py_func_new_named(_td_weakref_callback,
                    load_ptr(slots, 9 * C_POINTER_SIZE), cstr("TemporaryDirectory finalizer")))
                if not ptr_is_null(load_ptr(slots, 10 * C_POINTER_SIZE)):
                    _td_hold(slots, pins, 11, py_weakref_new(load_ptr(slots, 0),
                        load_ptr(slots, 10 * C_POINTER_SIZE)))
                    if not ptr_is_null(load_ptr(slots, 11 * C_POINTER_SIZE)) and not py_err_occurred():
                        if _td_register(state, load_ptr(owner, 0)) == 0:
                            store_ptr(owner, 0, null())  # Registry takes ownership.
                            # Do not form state -> weakref -> callback -> state
                            # until rollback can find and detach its record.
                            # The internal void setter cannot report a failed
                            # moving-root/store transaction; use its checked ABI.
                            installed: int = -1
                            if not py_err_occurred():
                                installed = py_list_setitem(state, _TD_WEAKREF,
                                    load_ptr(slots, 11 * C_POINTER_SIZE))
                            if installed == 0 and not py_err_occurred():
                                py_obj_setattr(load_ptr(slots, 0), cstr("name"), load_ptr(slots, 6 * C_POINTER_SIZE))
                                py_obj_setattr(load_ptr(slots, 0), cstr("_ignore_cleanup_errors"), load_ptr(slots, 4 * C_POINTER_SIZE))
                                py_obj_setattr(load_ptr(slots, 0), cstr("_delete"), load_ptr(slots, 5 * C_POINTER_SIZE))
                                py_obj_setattr(load_ptr(slots, 0), cstr("_pcc_finalizer_state"), state)
                                if not py_err_occurred():
                                    py_incref(_td_none())
                                    _td_hold(slots, pins, _TD_OUTPUT, _td_none())
        if ptr_is_null(load_ptr(slots, _TD_OUTPUT * C_POINTER_SIZE)):
            # Preserve the constructor error while cleanup executes. A pending
            # exception must neither skip rollback nor be replaced by it.
            py_tls_exc_swap_slot(ptr_add(slots, _TD_SAVED_ERROR * C_POINTER_SIZE))
            saved = load_ptr(slots, _TD_SAVED_ERROR * C_POINTER_SIZE)
            if not ptr_is_null(saved):
                _td_hold(slots, pins, _TD_SAVED_ERROR, saved)
            if load_i8(target_sys_platform(), 0) == 119:
                if not ptr_is_null(load_ptr(slots, 7 * C_POINTER_SIZE)):
                    _td_windows_cleanup(load_ptr(slots, 7 * C_POINTER_SIZE), 1, 1)
                if not ptr_is_null(load_ptr(owner, 0)):
                    _td_remove_created(null(), owner)
            else:
                if not ptr_is_null(load_ptr(slots, 7 * C_POINTER_SIZE)):
                    _td_detach(load_ptr(slots, 7 * C_POINTER_SIZE))
                # A failed constructor rolls back even with delete=False.
                _td_remove(load_ptr(slots, 6 * C_POINTER_SIZE), global_load_ptr("py_True"))
            py_clear_exception()
            saved = load_ptr(slots, _TD_SAVED_ERROR * C_POINTER_SIZE)
            if not ptr_is_null(saved):
                pcc_gc_unpin(saved)
                if load_i64(pins, _TD_SAVED_ERROR * C_POINTER_SIZE):
                    store_i32(saved, PYOBJECTHEADER_FLAGS_OFFSET,
                              load_i32(saved, PYOBJECTHEADER_FLAGS_OFFSET) | PY_FLAG_GC_PINNED)
                py_tls_exc_swap_slot(ptr_add(slots, _TD_SAVED_ERROR * C_POINTER_SIZE))
            if not py_err_occurred():
                _td_error(19, cstr("TemporaryDirectory construction failed"))
    return _td_finish(slots, pins)


@c_abi_export("pcc_tempdir_enter_entry")
def _td_enter(captures, args):
    if py_tuple_len(args) != 1:
        return _td_error(3, cstr("TemporaryDirectory.__enter__ expects no arguments"))
    slots = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    pins = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    memset(slots, 0, _TD_SLOTS * C_POINTER_SIZE)
    memset(pins, 0, _TD_SLOTS * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_tempdir_frame_map"), slots)
    _td_hold(slots, pins, 0, py_tuple_get(args, 0))
    _td_hold(slots, pins, _TD_OUTPUT, py_obj_getattr(load_ptr(slots, 0), cstr("name")))
    return _td_finish(slots, pins)


def _td_cleanup_manager(manager) -> None:
    slots = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    pins = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    memset(slots, 0, _TD_SLOTS * C_POINTER_SIZE)
    memset(pins, 0, _TD_SLOTS * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_tempdir_frame_map"), slots)
    py_incref(manager)
    _td_hold(slots, pins, 0, manager)
    _td_hold(slots, pins, 1, py_obj_getattr(load_ptr(slots, 0), cstr("_pcc_finalizer_state")))
    if not ptr_is_null(load_ptr(slots, C_POINTER_SIZE)):
        if load_i8(target_sys_platform(), 0) == 119:
            _td_hold(slots, pins, 2, py_list_get(load_ptr(slots, C_POINTER_SIZE), _TD_NAME))
            _td_hold(slots, pins, 3, py_obj_getattr(load_ptr(slots, 0), cstr("_ignore_cleanup_errors")))
            ignored: int = py_obj_truthy(load_ptr(slots, 3 * C_POINTER_SIZE)) if not py_err_occurred() else 0
            if not py_err_occurred():
                status: int = _td_windows_cleanup(load_ptr(slots, C_POINTER_SIZE), 1, 0)
                if status < 0 and not ignored and not py_err_occurred():
                    _td_os_error(status, load_ptr(slots, 2 * C_POINTER_SIZE))
        else:
            detached: int = _td_detach(load_ptr(slots, C_POINTER_SIZE))
            _td_hold(slots, pins, 2, py_obj_getattr(load_ptr(slots, 0), cstr("name")))
            if not ptr_is_null(load_ptr(slots, 2 * C_POINTER_SIZE)):
                if detached or py_os_path_exists(load_ptr(slots, 2 * C_POINTER_SIZE)):
                    _td_hold(slots, pins, 3, py_obj_getattr(load_ptr(slots, 0), cstr("_ignore_cleanup_errors")))
                    if not ptr_is_null(load_ptr(slots, 3 * C_POINTER_SIZE)):
                        _td_remove(load_ptr(slots, 2 * C_POINTER_SIZE), load_ptr(slots, 3 * C_POINTER_SIZE))
    _td_finish(slots, pins)


@c_abi_export("pcc_tempdir_cleanup_entry")
def _td_cleanup(captures, args):
    if py_tuple_len(args) != 1:
        return _td_error(3, cstr("TemporaryDirectory.cleanup expects no arguments"))
    slots = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    pins = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    memset(slots, 0, _TD_SLOTS * C_POINTER_SIZE)
    memset(pins, 0, _TD_SLOTS * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_tempdir_frame_map"), slots)
    _td_hold(slots, pins, 0, py_tuple_get(args, 0))
    if not ptr_is_null(load_ptr(slots, 0)):
        _td_cleanup_manager(load_ptr(slots, 0))
    if not py_err_occurred():
        py_incref(_td_none())
        _td_hold(slots, pins, _TD_OUTPUT, _td_none())
    return _td_finish(slots, pins)


@c_abi_export("pcc_tempdir_exit_entry")
def _td_exit(captures, args):
    if py_tuple_len(args) != 4:
        return _td_error(3, cstr("TemporaryDirectory.__exit__ expects three arguments"))
    slots = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    pins = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    memset(slots, 0, _TD_SLOTS * C_POINTER_SIZE)
    memset(pins, 0, _TD_SLOTS * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_tempdir_frame_map"), slots)
    _td_hold(slots, pins, 0, py_tuple_get(args, 0))
    _td_hold(slots, pins, 1, py_obj_getattr(load_ptr(slots, 0), cstr("_delete")))
    if not ptr_is_null(load_ptr(slots, C_POINTER_SIZE)):
        should_delete: int = py_obj_truthy(load_ptr(slots, C_POINTER_SIZE))
        if should_delete and not py_err_occurred():
            # Python resolves self.cleanup at exit time. Instance overrides and
            # subclass methods keep their ordinary binding/exception behavior.
            _td_hold(slots, pins, 2, py_obj_getattr(load_ptr(slots, 0), cstr("cleanup")))
            _td_hold(slots, pins, 3, py_tuple_new(0))
            if not py_err_occurred():
                _td_hold(slots, pins, 4, py_obj_call(load_ptr(slots, 2 * C_POINTER_SIZE),
                    load_ptr(slots, 3 * C_POINTER_SIZE), null()))
        if not py_err_occurred():
            py_incref(_td_none())
            _td_hold(slots, pins, _TD_OUTPUT, _td_none())
    return _td_finish(slots, pins)


@c_abi_export("pcc_tempdir_repr_entry")
def _td_repr(captures, args):
    slots = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    pins = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    memset(slots, 0, _TD_SLOTS * C_POINTER_SIZE)
    memset(pins, 0, _TD_SLOTS * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_tempdir_frame_map"), slots)
    _td_hold(slots, pins, 0, py_tuple_get(args, 0))
    _td_hold(slots, pins, 1, py_obj_getattr(load_ptr(slots, 0), cstr("name")))
    if not ptr_is_null(load_ptr(slots, C_POINTER_SIZE)):
        _td_hold(slots, pins, 2, py_obj_repr(load_ptr(slots, C_POINTER_SIZE)))
        _td_hold(slots, pins, 3, py_str_new(cstr("<TemporaryDirectory "), 20))
        if not ptr_is_null(load_ptr(slots, 2 * C_POINTER_SIZE)) and not ptr_is_null(load_ptr(slots, 3 * C_POINTER_SIZE)):
            _td_hold(slots, pins, 4, py_str_concat(load_ptr(slots, 3 * C_POINTER_SIZE), load_ptr(slots, 2 * C_POINTER_SIZE)))
            _td_hold(slots, pins, 5, py_str_new(cstr(">"), 1))
            if not ptr_is_null(load_ptr(slots, 4 * C_POINTER_SIZE)) and not ptr_is_null(load_ptr(slots, 5 * C_POINTER_SIZE)):
                _td_hold(slots, pins, _TD_OUTPUT, py_str_concat(load_ptr(slots, 4 * C_POINTER_SIZE), load_ptr(slots, 5 * C_POINTER_SIZE)))
    return _td_finish(slots, pins)


def _td_init_signature(slots, pins, constructor: int = 1):
    count: int = 6 if constructor else 3
    _td_hold(slots, pins, 1, py_tuple_new(count))
    _td_hold(slots, pins, 2, py_tuple_new(count))
    _td_hold(slots, pins, 3, py_tuple_new(count))
    _td_hold(slots, pins, 4, py_tuple_new(count))
    index: int = 0
    while index < count:
        parameter: int = index if constructor else index + 1
        name = cstr("self")
        if parameter == 1:
            name = cstr("suffix")
        elif parameter == 2:
            name = cstr("prefix")
        elif parameter == 3:
            name = cstr("dir")
        elif parameter == 4:
            name = cstr("ignore_cleanup_errors")
        elif parameter == 5:
            name = cstr("delete")
        _td_hold(slots, pins, 5, py_str_new(name, strlen(name)))
        py_tuple_set_item(load_ptr(slots, C_POINTER_SIZE), index, load_ptr(slots, 5 * C_POINTER_SIZE))
        _td_drop(slots, pins, 5)
        _td_hold(slots, pins, 5, py_int_from_i64(2 if parameter == 5 else 0))
        py_tuple_set_item(load_ptr(slots, 2 * C_POINTER_SIZE), index, load_ptr(slots, 5 * C_POINTER_SIZE))
        _td_drop(slots, pins, 5)
        py_tuple_set_item(load_ptr(slots, 3 * C_POINTER_SIZE), index,
                          global_load_ptr("py_False") if parameter == 0 else global_load_ptr("py_True"))
        default = _td_none()
        if parameter == 4:
            default = global_load_ptr("py_False")
        elif parameter == 5:
            default = global_load_ptr("py_True")
        py_tuple_set_item(load_ptr(slots, 4 * C_POINTER_SIZE), index, default)
        index = index + 1
    _td_hold(slots, pins, 5, py_tuple_new(5))
    _td_hold(slots, pins, 6, py_str_new(cstr("__pcc_func_signature_v1__"), 25))
    py_tuple_set_item(load_ptr(slots, 5 * C_POINTER_SIZE), 0, load_ptr(slots, 6 * C_POINTER_SIZE))
    index = 1
    while index < 5:
        py_tuple_set_item(load_ptr(slots, 5 * C_POINTER_SIZE), index, load_ptr(slots, index * C_POINTER_SIZE))
        index = index + 1
    _td_hold(slots, pins, 7, py_tuple_new(0))
    _td_hold(slots, pins, 8, py_tuple_new(2))
    py_tuple_set_item(load_ptr(slots, 8 * C_POINTER_SIZE), 0, load_ptr(slots, 7 * C_POINTER_SIZE))
    py_tuple_set_item(load_ptr(slots, 8 * C_POINTER_SIZE), 1, load_ptr(slots, 5 * C_POINTER_SIZE))
    return load_ptr(slots, 8 * C_POINTER_SIZE)


@c_abi_export("pcc_tempdir_mkdtemp_entry")
def _td_mkdtemp_entry(captures, args):
    # The ordinary callable binder supplies all three defaults and validates
    # argument names/duplicates before this entry. No manager owns this path:
    # mkdtemp leaves removal to its caller.
    if py_tuple_len(args) != _MKDTEMP_ARGUMENT_COUNT:
        return _td_error(3, cstr("mkdtemp expects three bound arguments"))
    slots = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    pins = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    memset(slots, 0, _TD_SLOTS * C_POINTER_SIZE)
    memset(pins, 0, _TD_SLOTS * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_tempdir_frame_map"), slots)
    owner = stack_alloc(C_POINTER_SIZE)
    store_ptr(owner, 0, null())
    index: int = _MKDTEMP_SUFFIX
    while index < _MKDTEMP_ARGUMENT_COUNT:
        _td_hold(slots, pins, index, py_tuple_get(args, index))
        index = index + 1
    if not py_err_occurred():
        _td_hold(slots, pins, _TD_OUTPUT, _td_mkdtemp(
            load_ptr(slots, _MKDTEMP_SUFFIX * C_POINTER_SIZE),
            load_ptr(slots, _MKDTEMP_PREFIX * C_POINTER_SIZE),
            load_ptr(slots, _MKDTEMP_DIRECTORY * C_POINTER_SIZE), slots, pins, owner))
    if not ptr_is_null(load_ptr(owner, 0)):
        closed: int = close_owned(load_ptr(owner, 0))
        if closed < 0:
            # The caller did not receive the name, so retain the original
            # error while rolling back precisely the still-owned directory.
            _td_remove_created(null(), owner)
            if not py_err_occurred():
                _td_os_error(closed, load_ptr(slots, _TD_OUTPUT * C_POINTER_SIZE))
            _td_drop(slots, pins, _TD_OUTPUT)
    return _td_finish(slots, pins)


@c_abi_export("py_tempfile_mkdtemp_function")
def py_tempfile_mkdtemp_function():
    slots = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    pins = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    memset(slots, 0, _TD_SLOTS * C_POINTER_SIZE)
    memset(pins, 0, _TD_SLOTS * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_tempdir_frame_map"), slots)
    mutex = _td_mutex()
    if ptr_is_null(mutex):
        return _td_finish(slots, pins)
    pcc_mutex_lock(mutex)
    source = global_addr("pcc_tempfile_mkdtemp_function")
    if not ptr_is_null(global_load_ptr("pcc_tempfile_mkdtemp_function")):
        pcc_mutex_unlock(mutex)
        _td_copy_cached_owner(slots, pins, source)
        return _td_finish(slots, pins)
    pcc_mutex_unlock(mutex)
    # Allocation and retirement can invoke callbacks. Build outside the cache
    # mutex, then retain only the winning candidate in the permanent root.
    captures = _td_init_signature(slots, pins, 0)
    if not py_err_occurred():
        _td_hold(slots, pins, _MKDTEMP_CALLABLE_CANDIDATE, py_func_new_named(
            function_addr("pcc_tempdir_mkdtemp_entry"), captures, cstr("mkdtemp")))
    pcc_mutex_lock(mutex)
    root_failed: int = 0
    if not py_err_occurred() and not ptr_is_null(
            load_ptr(slots, _MKDTEMP_CALLABLE_CANDIDATE * C_POINTER_SIZE)):
        if ptr_is_null(global_load_ptr("pcc_tempfile_mkdtemp_function")):
            handle = pcc_gc_scheduler_root_register_handle(source)
            if ptr_is_null(handle):
                root_failed = 1
            else:
                global_store_ptr("pcc_tempfile_mkdtemp_root_handle", handle)
                pcc_gc_store_root(source,
                    load_ptr(slots, _MKDTEMP_CALLABLE_CANDIDATE * C_POINTER_SIZE))
    pcc_mutex_unlock(mutex)
    if root_failed:
        _td_error(19, cstr("mkdtemp callable root registration failed"))
    if not py_err_occurred():
        _td_copy_cached_owner(slots, pins, source)
    return _td_finish(slots, pins)


def _td_copy_cached_owner(slots, pins, source) -> None:
    output = ptr_add(slots, _TD_OUTPUT * C_POINTER_SIZE)
    token: int = pcc_gc_root_copy_lease(output, source)
    if token < 0:
        _td_error(7, cstr("cached owner copy failed"))
        return
    # The counted address lease is independent of the legacy pin flag, and
    # protects this load until the return pin has been established.
    _td_hold(slots, pins, _TD_OUTPUT, load_ptr(output, 0))
    if pcc_gc_foreign_lease_release(output, token) < 0:
        _td_error(7, cstr("cached owner lease release failed"))


def _fdopen_signature(slots, pins, temporary: int = 0):
    count: int = _NT_ARGUMENT_COUNT if temporary else _FDOPEN_ARGUMENT_COUNT
    index: int = _FILE_SIGNATURE_NAMES
    while index <= _FILE_SIGNATURE_DEFAULTS:
        _td_hold(slots, pins, index, py_tuple_new(count))
        index = index + 1
    index = _FDOPEN_DESCRIPTOR
    while index < count:
        name = cstr("fd")
        if index == _FDOPEN_MODE:
            name = cstr("mode")
        elif index == _FDOPEN_BUFFERING:
            name = cstr("buffering")
        elif index == _FDOPEN_ENCODING:
            name = cstr("encoding")
        elif index == _FDOPEN_ERRORS:
            name = cstr("errors")
        elif index == _FDOPEN_NEWLINE:
            name = cstr("newline")
        elif index == _FDOPEN_CLOSEFD:
            name = cstr("closefd")
        elif index == _FDOPEN_OPENER:
            name = cstr("opener")
        if temporary:
            if index == _NT_MODE:
                name = cstr("mode")
            elif index == _NT_BUFFERING:
                name = cstr("buffering")
            elif index == _NT_ENCODING:
                name = cstr("encoding")
            elif index == _NT_NEWLINE:
                name = cstr("newline")
            elif index == _NT_SUFFIX:
                name = cstr("suffix")
            elif index == _NT_PREFIX:
                name = cstr("prefix")
            elif index == _NT_DIRECTORY:
                name = cstr("dir")
            elif index == _NT_DELETE:
                name = cstr("delete")
            elif index == _NT_ERRORS:
                name = cstr("errors")
            else:
                name = cstr("delete_on_close")
        _td_hold(slots, pins, _FILE_SIGNATURE_ITEM, py_str_new(name, strlen(name)))
        py_tuple_set_item(load_ptr(slots, _FILE_SIGNATURE_NAMES * C_POINTER_SIZE), index,
                          load_ptr(slots, _FILE_SIGNATURE_ITEM * C_POINTER_SIZE))
        _td_drop(slots, pins, _FILE_SIGNATURE_ITEM)
        _td_hold(slots, pins, _FILE_SIGNATURE_ITEM,
                 py_int_from_i64(2 if temporary and index >= _NT_ERRORS else 0))
        py_tuple_set_item(load_ptr(slots, _FILE_SIGNATURE_KINDS * C_POINTER_SIZE), index,
                          load_ptr(slots, _FILE_SIGNATURE_ITEM * C_POINTER_SIZE))
        _td_drop(slots, pins, _FILE_SIGNATURE_ITEM)
        py_tuple_set_item(load_ptr(slots, _FILE_SIGNATURE_PRESENT * C_POINTER_SIZE), index,
            global_load_ptr("py_False") if not temporary and index == _FDOPEN_DESCRIPTOR else global_load_ptr("py_True"))
        default = _td_none()
        if index == (_NT_MODE if temporary else _FDOPEN_MODE):
            _td_hold(slots, pins, _FILE_SIGNATURE_ITEM,
                     py_str_new(cstr("w+b") if temporary else cstr("r"), 3 if temporary else 1))
            default = load_ptr(slots, _FILE_SIGNATURE_ITEM * C_POINTER_SIZE)
        elif index == (_NT_BUFFERING if temporary else _FDOPEN_BUFFERING):
            _td_hold(slots, pins, _FILE_SIGNATURE_ITEM, py_int_from_i64(-1))
            default = load_ptr(slots, _FILE_SIGNATURE_ITEM * C_POINTER_SIZE)
        elif (not temporary and index == _FDOPEN_CLOSEFD
              or temporary and (index == _NT_DELETE or index == _NT_DELETE_ON_CLOSE)):
            default = global_load_ptr("py_True")
        py_tuple_set_item(load_ptr(slots, _FILE_SIGNATURE_DEFAULTS * C_POINTER_SIZE), index, default)
        _td_drop(slots, pins, _FILE_SIGNATURE_ITEM)
        index = index + 1
    _td_hold(slots, pins, _FILE_SIGNATURE, py_tuple_new(5))
    _td_hold(slots, pins, _FILE_SIGNATURE_ITEM, py_str_new(cstr("__pcc_func_signature_v1__"), 25))
    py_tuple_set_item(load_ptr(slots, _FILE_SIGNATURE * C_POINTER_SIZE), 0,
                      load_ptr(slots, _FILE_SIGNATURE_ITEM * C_POINTER_SIZE))
    index = _FILE_SIGNATURE_NAMES
    while index <= _FILE_SIGNATURE_DEFAULTS:
        py_tuple_set_item(load_ptr(slots, _FILE_SIGNATURE * C_POINTER_SIZE), index,
                          load_ptr(slots, index * C_POINTER_SIZE))
        index = index + 1
    _td_hold(slots, pins, _FILE_CAPTURES_EMPTY, py_tuple_new(0))
    _td_hold(slots, pins, _FILE_CAPTURES, py_tuple_new(2))
    py_tuple_set_item(load_ptr(slots, _FILE_CAPTURES * C_POINTER_SIZE), 0,
                      load_ptr(slots, _FILE_CAPTURES_EMPTY * C_POINTER_SIZE))
    py_tuple_set_item(load_ptr(slots, _FILE_CAPTURES * C_POINTER_SIZE), 1,
                      load_ptr(slots, _FILE_SIGNATURE * C_POINTER_SIZE))
    return load_ptr(slots, _FILE_CAPTURES * C_POINTER_SIZE)


def _file_c_int_argument(slots, index: int) -> int:
    # This is an authoritative owning frame slot. The protocol helper retains
    # both the receiver and callback result across arbitrary __index__ code.
    value: int = py_index_i64_checked_slots(ptr_add(slots, index * C_POINTER_SIZE))
    if not py_err_occurred() and (value < -2147483648 or value > 2147483647):
        _td_error(15, cstr("Python int too large to convert to C int"))
    return value


def _fdopen_descriptor_type(slots) -> int:
    # os.fdopen explicitly requires an int before delegating to io.open;
    # an arbitrary __index__ provider is accepted by os.close, not fdopen.
    descriptor = load_ptr(slots, _FDOPEN_DESCRIPTOR * C_POINTER_SIZE)
    if is_tagged_int(descriptor):
        return 0
    if not ptr_is_null(descriptor):
        tag: int = load_i32(descriptor, PYOBJECTHEADER_TYPE_TAG_OFFSET)
        if tag == PY_TYPE_INT or tag == PY_TYPE_BOOL:
            return 0
    _td_error(3, cstr("invalid fd type: expected integer"))
    return -1


@c_abi_export("pcc_file_fdopen_entry")
def _fdopen_entry(captures, args):
    if py_tuple_len(args) != _FDOPEN_ARGUMENT_COUNT:
        return _td_error(3, cstr("fdopen expects eight bound arguments"))
    if load_i8(target_sys_platform(), 0) == 119:
        return _td_error(11, cstr("native fdopen requires an owned Windows provider"))
    slots = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    pins = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    memset(slots, 0, _TD_SLOTS * C_POINTER_SIZE)
    memset(pins, 0, _TD_SLOTS * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_tempdir_frame_map"), slots)
    index: int = _FDOPEN_DESCRIPTOR
    while index < _FDOPEN_ARGUMENT_COUNT:
        _td_hold(slots, pins, index, py_tuple_get(args, index))
        index = index + 1
    buffering: int = -1
    closefd: int = 0
    if not py_err_occurred() and _fdopen_descriptor_type(slots) == 0:
        buffering = _file_c_int_argument(slots, _FDOPEN_BUFFERING)
    if not py_err_occurred():
        closefd = py_obj_truthy(load_ptr(slots, _FDOPEN_CLOSEFD * C_POINTER_SIZE))
    if not py_err_occurred() and not ptr_eq(load_ptr(slots, _FDOPEN_OPENER * C_POINTER_SIZE), _td_none()):
        _td_error(11, cstr("native fdopen custom openers are not implemented"))
    if not py_err_occurred():
        _td_hold(slots, pins, _TD_OUTPUT, py_file_fdopen_options(
            load_ptr(slots, _FDOPEN_DESCRIPTOR * C_POINTER_SIZE),
            load_ptr(slots, _FDOPEN_MODE * C_POINTER_SIZE),
            load_ptr(slots, _FDOPEN_ENCODING * C_POINTER_SIZE),
            load_ptr(slots, _FDOPEN_ERRORS * C_POINTER_SIZE),
            load_ptr(slots, _FDOPEN_NEWLINE * C_POINTER_SIZE), closefd, buffering))
    return _nt_finish(slots, pins)


def _file_provider_function(temporary: int):
    slots = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    pins = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    memset(slots, 0, _TD_SLOTS * C_POINTER_SIZE)
    memset(pins, 0, _TD_SLOTS * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_tempdir_frame_map"), slots)
    mutex = _td_mutex()
    if ptr_is_null(mutex):
        return _td_finish(slots, pins)
    source = global_addr("pcc_namedtempfile_function") if temporary else global_addr("pcc_file_fdopen_function")
    if temporary == 2:
        source = global_addr("pcc_os_open_function")
    elif temporary == 3:
        source = global_addr("pcc_os_close_function")
    elif temporary == 4:
        source = global_addr("pcc_os_pwrite_function")
    elif temporary == 5:
        source = global_addr("pcc_os_ftruncate_function")
    pcc_mutex_lock(mutex)
    if not ptr_is_null(load_ptr(source, 0)):
        pcc_mutex_unlock(mutex)
        _td_copy_cached_owner(slots, pins, source)
        return _td_finish(slots, pins)
    pcc_mutex_unlock(mutex)
    captures = _os_descriptor_signature(slots, pins, temporary) if temporary >= 2 else _fdopen_signature(slots, pins, temporary)
    if not py_err_occurred():
        entry = function_addr("pcc_namedtempfile_entry") if temporary else function_addr("pcc_file_fdopen_entry")
        name = cstr("NamedTemporaryFile") if temporary else cstr("fdopen")
        if temporary == 2:
            entry = function_addr("pcc_os_open_entry")
            name = cstr("open")
        elif temporary == 3:
            entry = function_addr("pcc_os_close_entry")
            name = cstr("close")
        elif temporary == 4:
            entry = function_addr("pcc_os_pwrite_entry")
            name = cstr("pwrite")
        elif temporary == 5:
            entry = function_addr("pcc_os_ftruncate_entry")
            name = cstr("ftruncate")
        _td_hold(slots, pins, _FILE_CALLABLE_CANDIDATE, py_func_new_named(
            entry, captures, name))
    root_failed: int = 0
    pcc_mutex_lock(mutex)
    if not py_err_occurred() and not ptr_is_null(
            load_ptr(slots, _FILE_CALLABLE_CANDIDATE * C_POINTER_SIZE)):
        if ptr_is_null(load_ptr(source, 0)):
            handle = pcc_gc_scheduler_root_register_handle(source)
            if ptr_is_null(handle):
                root_failed = 1
            else:
                if temporary == 2:
                    global_store_ptr("pcc_os_open_root_handle", handle)
                elif temporary == 3:
                    global_store_ptr("pcc_os_close_root_handle", handle)
                elif temporary == 4:
                    global_store_ptr("pcc_os_pwrite_root_handle", handle)
                elif temporary == 5:
                    global_store_ptr("pcc_os_ftruncate_root_handle", handle)
                elif temporary:
                    global_store_ptr("pcc_namedtempfile_root_handle", handle)
                else:
                    global_store_ptr("pcc_file_fdopen_root_handle", handle)
                pcc_gc_store_root(source,
                    load_ptr(slots, _FILE_CALLABLE_CANDIDATE * C_POINTER_SIZE))
    pcc_mutex_unlock(mutex)
    if root_failed:
        _td_error(19, cstr("fdopen callable root registration failed"))
    if not py_err_occurred():
        _td_copy_cached_owner(slots, pins, source)
    return _td_finish(slots, pins)


@c_abi_export("py_file_fdopen_function")
def py_file_fdopen_function():
    return _file_provider_function(0)


@c_abi_export("py_namedtempfile_function")
def py_namedtempfile_function():
    return _file_provider_function(1)


@c_abi_export("py_os_open_function")
def py_os_open_function():
    return _file_provider_function(2)


@c_abi_export("py_os_close_function")
def py_os_close_function():
    return _file_provider_function(3)


@c_abi_export("py_os_pwrite_function")
def py_os_pwrite_function():
    return _file_provider_function(4)


@c_abi_export("py_os_ftruncate_function")
def py_os_ftruncate_function():
    return _file_provider_function(5)


def _os_descriptor_signature(slots, pins, operation: int):
    closing: int = 1 if operation == 3 else 0
    positional: int = 1 if operation == 4 or operation == 5 else 0
    count: int = 1 if closing else 4
    if operation == 4:
        count = 3
    elif operation == 5:
        count = 2
    index: int = _FILE_SIGNATURE_NAMES
    while index <= _FILE_SIGNATURE_DEFAULTS:
        _td_hold(slots, pins, index, py_tuple_new(count))
        index = index + 1
    index = 0
    while index < count:
        name = cstr("fd") if closing or positional else cstr("path")
        if positional and index == 1:
            name = cstr("buffer") if operation == 4 else cstr("length")
        elif positional and index == 2:
            name = cstr("offset")
        elif index == _OS_OPEN_FLAGS:
            name = cstr("flags")
        elif index == _OS_OPEN_MODE:
            name = cstr("mode")
        elif index == _OS_OPEN_DIR_FD:
            name = cstr("dir_fd")
        _td_hold(slots, pins, _FILE_SIGNATURE_ITEM, py_str_new(name, strlen(name)))
        py_tuple_set_item(load_ptr(slots, _FILE_SIGNATURE_NAMES * C_POINTER_SIZE), index,
                          load_ptr(slots, _FILE_SIGNATURE_ITEM * C_POINTER_SIZE))
        _td_drop(slots, pins, _FILE_SIGNATURE_ITEM)
        kind: int = 1 if positional else (2 if index == _OS_OPEN_DIR_FD else 0)
        _td_hold(slots, pins, _FILE_SIGNATURE_ITEM, py_int_from_i64(kind))
        py_tuple_set_item(load_ptr(slots, _FILE_SIGNATURE_KINDS * C_POINTER_SIZE), index,
                          load_ptr(slots, _FILE_SIGNATURE_ITEM * C_POINTER_SIZE))
        _td_drop(slots, pins, _FILE_SIGNATURE_ITEM)
        py_tuple_set_item(load_ptr(slots, _FILE_SIGNATURE_PRESENT * C_POINTER_SIZE), index,
            global_load_ptr("py_True") if not positional and index >= _OS_OPEN_MODE else global_load_ptr("py_False"))
        default = _td_none()
        if not positional and index == _OS_OPEN_MODE:
            _td_hold(slots, pins, _FILE_SIGNATURE_ITEM, py_int_from_i64(511))
            default = load_ptr(slots, _FILE_SIGNATURE_ITEM * C_POINTER_SIZE)
        py_tuple_set_item(load_ptr(slots, _FILE_SIGNATURE_DEFAULTS * C_POINTER_SIZE), index, default)
        _td_drop(slots, pins, _FILE_SIGNATURE_ITEM)
        index = index + 1
    _td_hold(slots, pins, _FILE_SIGNATURE, py_tuple_new(5))
    _td_hold(slots, pins, _FILE_SIGNATURE_ITEM, py_str_new(cstr("__pcc_func_signature_v1__"), 25))
    py_tuple_set_item(load_ptr(slots, _FILE_SIGNATURE * C_POINTER_SIZE), 0,
                      load_ptr(slots, _FILE_SIGNATURE_ITEM * C_POINTER_SIZE))
    index = _FILE_SIGNATURE_NAMES
    while index <= _FILE_SIGNATURE_DEFAULTS:
        py_tuple_set_item(load_ptr(slots, _FILE_SIGNATURE * C_POINTER_SIZE), index,
                          load_ptr(slots, index * C_POINTER_SIZE))
        index = index + 1
    _td_hold(slots, pins, _FILE_CAPTURES_EMPTY, py_tuple_new(0))
    _td_hold(slots, pins, _FILE_CAPTURES, py_tuple_new(2))
    py_tuple_set_item(load_ptr(slots, _FILE_CAPTURES * C_POINTER_SIZE), 0,
                      load_ptr(slots, _FILE_CAPTURES_EMPTY * C_POINTER_SIZE))
    py_tuple_set_item(load_ptr(slots, _FILE_CAPTURES * C_POINTER_SIZE), 1,
                      load_ptr(slots, _FILE_SIGNATURE * C_POINTER_SIZE))
    return load_ptr(slots, _FILE_CAPTURES * C_POINTER_SIZE)


@c_abi_export("pcc_os_open_entry")
def _os_open_entry(captures, args):
    if py_tuple_len(args) != 4:
        return _td_error(3, cstr("os.open expects four bound arguments"))
    if load_i8(target_sys_platform(), 0) == 119:
        return _td_error(11, cstr("native os.open requires an owned Windows provider"))
    slots = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    pins = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    memset(slots, 0, _TD_SLOTS * C_POINTER_SIZE)
    memset(pins, 0, _TD_SLOTS * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_tempdir_frame_map"), slots)
    index: int = _OS_OPEN_PATH
    while index <= _OS_OPEN_DIR_FD:
        _td_hold(slots, pins, index, py_tuple_get(args, index))
        index = index + 1
    flags: int = 0
    permissions: int = 0
    directory_fd: int = -2 if load_i8(target_sys_platform(), 0) == 100 else -100
    if not py_err_occurred():
        _td_hold(slots, pins, _OS_OPEN_NORMALIZED,
                 py_file_fspath(load_ptr(slots, _OS_OPEN_PATH * C_POINTER_SIZE)))
    if not py_err_occurred():
        flags = _file_c_int_argument(slots, _OS_OPEN_FLAGS)
    if not py_err_occurred():
        permissions = _file_c_int_argument(slots, _OS_OPEN_MODE)
    if not py_err_occurred() and not ptr_eq(load_ptr(slots, _OS_OPEN_DIR_FD * C_POINTER_SIZE), _td_none()):
        directory_fd = _file_c_int_argument(slots, _OS_OPEN_DIR_FD)
    if not py_err_occurred():
        path = load_ptr(slots, _OS_OPEN_NORMALIZED * C_POINTER_SIZE)
        data = null()
        length: int = 0
        if load_i32(path, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_BYTES:
            data = ptr_add(path, PYBYTESOBJECT_DATA_OFFSET)
            length = load_i64(path, PYBYTESOBJECT_BYTE_LEN_OFFSET)
        else:
            data = py_str_utf8(path)
            length = load_i64(path, PYSTROBJECT_BYTE_LEN_OFFSET)
        index = 0
        while index < length:
            if load_i8(data, index) == 0:
                _td_error(2, cstr("embedded null character"))
                break
            index = index + 1
        if not py_err_occurred():
            flags = flags | (16777216 if load_i8(target_sys_platform(), 0) == 100 else 524288)
            descriptor: int = open_file_flags(data, flags, permissions, directory_fd)
            while descriptor == -4:
                descriptor = open_file_flags(data, flags, permissions, directory_fd)
            if descriptor < 0:
                _td_os_error(descriptor, path)
            else:
                _td_hold(slots, pins, _TD_OUTPUT, py_int_from_i64(descriptor))
                if py_err_occurred():
                    close(descriptor)
    return _nt_finish(slots, pins)


@c_abi_export("pcc_os_close_entry")
def _os_close_entry(captures, args):
    if py_tuple_len(args) != 1:
        return _td_error(3, cstr("os.close expects one argument"))
    slots = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    pins = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    memset(slots, 0, _TD_SLOTS * C_POINTER_SIZE)
    memset(pins, 0, _TD_SLOTS * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_tempdir_frame_map"), slots)
    _td_hold(slots, pins, _FDOPEN_DESCRIPTOR, py_tuple_get(args, 0))
    descriptor: int = 0
    if not py_err_occurred():
        descriptor = _file_c_int_argument(slots, _FDOPEN_DESCRIPTOR)
    if not py_err_occurred():
        status: int = close(descriptor)
        if status < 0:
            _td_os_error(status, _td_none())
        else:
            py_incref(_td_none())
            _td_hold(slots, pins, _TD_OUTPUT, _td_none())
    return _nt_finish(slots, pins)


@c_abi_export("pcc_os_pwrite_entry")
def _os_pwrite_entry(captures, args):
    """Write immutable bytes; mutable/view exports remain an explicit gap.

    A raw address lease prevents GC relocation, but does not implement
    CPython's exported-buffer resize prohibition during offset.__index__.
    Reject those buffer forms before that callback rather than silently
    applying snapshot semantics to a different observable contract.
    """
    if py_tuple_len(args) != 3:
        return _td_error(3, cstr("os.pwrite expects three positional arguments"))
    if load_i8(target_sys_platform(), 0) == 119:
        return _td_error(11, cstr("native os.pwrite requires an owned Windows provider"))
    slots = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    pins = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    memset(slots, 0, _TD_SLOTS * C_POINTER_SIZE)
    memset(pins, 0, _TD_SLOTS * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_tempdir_frame_map"), slots)
    index: int = _OS_POSITIONAL_DESCRIPTOR
    while index <= _OS_POSITIONAL_OFFSET and not py_err_occurred():
        _td_hold(slots, pins, index, py_tuple_get(args, index))
        index = index + 1
    descriptor: int = 0
    offset: int = 0
    if not py_err_occurred():
        descriptor = _file_c_int_argument(slots, _OS_POSITIONAL_DESCRIPTOR)
    if not py_err_occurred():
        buffer = load_ptr(slots, _OS_POSITIONAL_BUFFER * C_POINTER_SIZE)
        tag: int = -1
        if not ptr_is_null(buffer) and not is_tagged_int(buffer):
            tag = load_i32(buffer, PYOBJECTHEADER_TYPE_TAG_OFFSET)
        if tag == PY_TYPE_BYTEARRAY or tag == PY_TYPE_MEMORYVIEW:
            _td_error(11, cstr("native os.pwrite mutable buffers and memoryviews require owned buffer-export semantics"))
        elif tag != PY_TYPE_BYTES:
            _td_error(3, cstr("os.pwrite requires a bytes-like object"))
    if not py_err_occurred():
        offset = py_index_i64_checked_slots(
            ptr_add(slots, _OS_POSITIONAL_OFFSET * C_POINTER_SIZE)
        )
    if not py_err_occurred():
        # Exact bytes retain their identity with an independent owned result.
        # Publish and pin that owner before exposing its payload to a syscall.
        _td_hold(slots, pins, _OS_POSITIONAL_SNAPSHOT, py_bytes_from_obj(
            load_ptr(slots, _OS_POSITIONAL_BUFFER * C_POINTER_SIZE)
        ))
    if not py_err_occurred():
        snapshot = load_ptr(slots, _OS_POSITIONAL_SNAPSHOT * C_POINTER_SIZE)
        length: int = load_i64(snapshot, PYBYTESOBJECT_BYTE_LEN_OFFSET)
        status: int = pcc_platform_pwrite(
            descriptor, ptr_add(snapshot, PYBYTESOBJECT_DATA_OFFSET), length, offset
        )
        while status == -4 and not py_err_occurred():
            pcc_thread_safepoint()
            if not py_err_occurred():
                snapshot = load_ptr(slots, _OS_POSITIONAL_SNAPSHOT * C_POINTER_SIZE)
                status = pcc_platform_pwrite(
                    descriptor, ptr_add(snapshot, PYBYTESOBJECT_DATA_OFFSET), length, offset
                )
        if not py_err_occurred():
            if status < 0:
                _td_os_error(status, _td_none())
            else:
                # A successful short write is observable to Python. The
                # caller decides whether and how to complete the remainder.
                _td_hold(slots, pins, _TD_OUTPUT, py_int_from_i64(status))
    return _nt_finish(slots, pins)


@c_abi_export("pcc_os_ftruncate_entry")
def _os_ftruncate_entry(captures, args):
    if py_tuple_len(args) != 2:
        return _td_error(3, cstr("os.ftruncate expects two positional arguments"))
    if load_i8(target_sys_platform(), 0) == 119:
        return _td_error(11, cstr("native os.ftruncate requires an owned Windows provider"))
    slots = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    pins = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    memset(slots, 0, _TD_SLOTS * C_POINTER_SIZE)
    memset(pins, 0, _TD_SLOTS * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_tempdir_frame_map"), slots)
    _td_hold(slots, pins, _OS_POSITIONAL_DESCRIPTOR, py_tuple_get(args, 0))
    if not py_err_occurred():
        _td_hold(slots, pins, _OS_TRUNCATE_LENGTH, py_tuple_get(args, 1))
    descriptor: int = 0
    length: int = 0
    if not py_err_occurred():
        descriptor = _file_c_int_argument(slots, _OS_POSITIONAL_DESCRIPTOR)
    if not py_err_occurred():
        length = py_index_i64_checked_slots(
            ptr_add(slots, _OS_TRUNCATE_LENGTH * C_POINTER_SIZE)
        )
    if not py_err_occurred():
        # The platform rejects negative lengths with errno; descriptor
        # lifetime and current offset always remain the caller's property.
        status: int = pcc_platform_ftruncate(descriptor, length)
        while status == -4 and not py_err_occurred():
            pcc_thread_safepoint()
            if not py_err_occurred():
                status = pcc_platform_ftruncate(descriptor, length)
        if not py_err_occurred():
            if status < 0:
                _td_os_error(status, _td_none())
            else:
                py_incref(_td_none())
                _td_hold(slots, pins, _TD_OUTPUT, _td_none())
    return _nt_finish(slots, pins)


def _nt_finish(slots, pins):
    # Retiring file/callable owners may execute user finalizers. Preserve the
    # exception which belongs to this operation until all other owners retire.
    saved_slot = ptr_add(slots, _TD_SAVED_ERROR * C_POINTER_SIZE)
    py_tls_exc_swap_slot(saved_slot)
    saved = load_ptr(saved_slot, 0)
    if not ptr_is_null(saved):
        _td_hold(slots, pins, _TD_SAVED_ERROR, saved)
    index: int = _TD_SAVED_ERROR
    while index > 0:
        index = index - 1
        _td_drop(slots, pins, index)
    py_clear_exception()
    saved = load_ptr(saved_slot, 0)
    if not ptr_is_null(saved):
        pcc_gc_unpin(saved)
        if load_i64(pins, _TD_SAVED_ERROR * C_POINTER_SIZE):
            store_i32(saved, PYOBJECTHEADER_FLAGS_OFFSET,
                      load_i32(saved, PYOBJECTHEADER_FLAGS_OFFSET) | PY_FLAG_GC_PINNED)
    py_tls_exc_swap_slot(saved_slot)
    pcc_gc_frame_leave(slots)
    return pcc_gc_take_pinned_slot(ptr_add(slots, _TD_OUTPUT * C_POINTER_SIZE),
                                   load_i64(pins, _TD_OUTPUT * C_POINTER_SIZE))


def _nt_create_name(slots, pins, descriptor_out):
    if load_i8(target_sys_platform(), 0) == 119:
        return _td_error(11, cstr("native NamedTemporaryFile requires an owned Windows provider"))
    suffix = _td_text(load_ptr(slots, _NT_SUFFIX * C_POINTER_SIZE), cstr(""))
    prefix = _td_text(load_ptr(slots, _NT_PREFIX * C_POINTER_SIZE), cstr("tmp"))
    if ptr_is_null(suffix) or ptr_is_null(prefix):
        return null()
    directory = load_ptr(slots, _NT_DIRECTORY * C_POINTER_SIZE)
    root = null()
    if not ptr_eq(directory, _td_none()):
        _td_hold(slots, pins, _NT_NORMALIZED_DIRECTORY, py_file_fspath(directory))
        if py_err_occurred():
            return null()
        root = _td_text(load_ptr(slots, _NT_NORMALIZED_DIRECTORY * C_POINTER_SIZE), null())
    else:
        status = stack_alloc(2 * C_POINTER_SIZE)
        store_i64(status, 0, -2)
        store_ptr(status, C_POINTER_SIZE, null())
        candidate: int = 0
        while candidate < 7:
            if candidate == 0:
                root = getenv(cstr("TMPDIR"))
            elif candidate == 1:
                root = getenv(cstr("TEMP"))
            elif candidate == 2:
                root = getenv(cstr("TMP"))
            elif candidate == 3:
                root = cstr("/tmp")
            elif candidate == 4:
                root = cstr("/var/tmp")
            elif candidate == 5:
                root = cstr("/usr/tmp")
            else:
                root = cstr(".")
            if not ptr_is_null(root) and load_i8(root, 0) != 0:
                probe = _td_mkdir_at(root, cstr(".pcc_probe_"), cstr(""), status, null())
                if not ptr_is_null(probe):
                    removed: int = remove_tree(probe)
                    free(probe)
                    if removed < 0:
                        return _td_os_error(removed, directory)
                    break
            root = null()
            candidate = candidate + 1
        failed_path = load_ptr(status, C_POINTER_SIZE)
        if not ptr_is_null(failed_path):
            free(failed_path)
    if ptr_is_null(root):
        if not py_err_occurred():
            _td_os_error(-2, directory)
        return null()
    path = _td_path_template(root, prefix, suffix)
    if ptr_is_null(path):
        return _td_error(19, cstr("cannot allocate temporary filename"))
    descriptor: int = mkstemps(path, strlen(suffix))
    _td_hold(slots, pins, _NT_RAW_NAME, py_str_new(path, strlen(path)))
    if descriptor < 0:
        free(path)
        if not py_err_occurred():
            return _td_os_error(descriptor, load_ptr(slots, _NT_RAW_NAME * C_POINTER_SIZE))
        return null()
    if not py_err_occurred():
        _td_hold(slots, pins, _NT_PATH,
                 py_os_path_abspath(load_ptr(slots, _NT_RAW_NAME * C_POINTER_SIZE)))
    if py_err_occurred():
        close(descriptor)
        unlinkat(path, 0)
        free(path)
        return null()
    free(path)
    store_i64(descriptor_out, 0, descriptor)
    return load_ptr(slots, _NT_PATH * C_POINTER_SIZE)


def _nt_cleanup(manager, mode: int):
    slots = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    pins = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    memset(slots, 0, _TD_SLOTS * C_POINTER_SIZE)
    memset(pins, 0, _TD_SLOTS * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_tempdir_frame_map"), slots)
    py_incref(manager)
    _td_hold(slots, pins, _NT_SELF, manager)
    _td_hold(slots, pins, _NT_CLEANUP_READY, py_obj_getattr(manager, cstr("_ready")))
    if mode == 2 and py_err_occurred():
        py_clear_exception()  # A failed constructor performs its own rollback.
    elif not py_err_occurred() and py_obj_truthy(load_ptr(slots, _NT_CLEANUP_READY * C_POINTER_SIZE)):
        _td_hold(slots, pins, _NT_METHOD_FILE, py_obj_getattr(manager, cstr("_cleanup_file")))
        _td_hold(slots, pins, _NT_CLEANUP_PATH, py_obj_getattr(manager, cstr("_cleanup_name")))
        _td_hold(slots, pins, _NT_CLEANUP_DELETE, py_obj_getattr(manager, cstr("_delete")))
        _td_hold(slots, pins, _NT_CLEANUP_ON_CLOSE, py_obj_getattr(manager, cstr("_delete_on_close")))
        _td_hold(slots, pins, _NT_CLEANUP_REMOVED, py_obj_getattr(manager, cstr("_removed")))
        if not py_err_occurred():
            py_file_close_checked(load_ptr(slots, _NT_METHOD_FILE * C_POINTER_SIZE))
            close_error = ptr_add(slots, _NT_CLOSE_ERROR * C_POINTER_SIZE)
            py_tls_exc_swap_slot(close_error)
            if not ptr_is_null(load_ptr(close_error, 0)):
                _td_hold(slots, pins, _NT_CLOSE_ERROR, load_ptr(close_error, 0))
            should_remove: int = py_obj_truthy(load_ptr(slots, _NT_CLEANUP_DELETE * C_POINTER_SIZE))
            on_close: int = py_obj_truthy(load_ptr(slots, _NT_CLEANUP_ON_CLOSE * C_POINTER_SIZE))
            removed: int = py_obj_truthy(load_ptr(slots, _NT_CLEANUP_REMOVED * C_POINTER_SIZE))
            if should_remove and (on_close or mode != 0) and not removed and not py_err_occurred():
                py_obj_setattr(manager, cstr("_removed"), global_load_ptr("py_True"))
                if not py_err_occurred():
                    path = _td_text(load_ptr(slots, _NT_CLEANUP_PATH * C_POINTER_SIZE), null())
                    if not ptr_is_null(path):
                        status: int = unlinkat(path, 0)
                        # An externally removed pathname is already cleaned,
                        # as in tempfile._TemporaryFileCloser.cleanup.
                        if status < 0 and status != -2:
                            _td_os_error(status, load_ptr(slots, _NT_CLEANUP_PATH * C_POINTER_SIZE))
            if not py_err_occurred() and not ptr_is_null(load_ptr(close_error, 0)):
                saved = load_ptr(close_error, 0)
                pcc_gc_unpin(saved)
                if load_i64(pins, _NT_CLOSE_ERROR * C_POINTER_SIZE):
                    store_i32(saved, PYOBJECTHEADER_FLAGS_OFFSET,
                              load_i32(saved, PYOBJECTHEADER_FLAGS_OFFSET) | PY_FLAG_GC_PINNED)
                py_tls_exc_swap_slot(close_error)
                store_i64(pins, _NT_CLOSE_ERROR * C_POINTER_SIZE, 0)
    if mode == 2:
        py_clear_exception()
    if not py_err_occurred():
        result = global_load_ptr("py_False") if mode == 1 else _td_none()
        py_incref(result)
        _td_hold(slots, pins, _TD_OUTPUT, result)
    return _nt_finish(slots, pins)


@c_abi_export("pcc_namedtempfile_close_entry")
def _nt_close(captures, args):
    if py_tuple_len(args) != 1:
        return _td_error(3, cstr("close expects no arguments"))
    return _nt_cleanup_entry(args, 0)


@c_abi_export("pcc_namedtempfile_exit_entry")
def _nt_exit(captures, args):
    if py_tuple_len(args) != 4:
        return _td_error(3, cstr("__exit__ expects three arguments"))
    return _nt_cleanup_entry(args, 1)


@c_abi_export("pcc_namedtempfile_del_entry")
def _nt_del(captures, args):
    return _nt_cleanup_entry(args, 2)


def _nt_cleanup_entry(args, mode: int):
    slots = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    pins = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    memset(slots, 0, _TD_SLOTS * C_POINTER_SIZE)
    memset(pins, 0, _TD_SLOTS * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_tempdir_frame_map"), slots)
    prior = ptr_add(slots, _NT_CLOSE_ERROR * C_POINTER_SIZE)
    if mode == 2:
        py_tls_exc_swap_slot(prior)
        if not ptr_is_null(load_ptr(prior, 0)):
            _td_hold(slots, pins, _NT_CLOSE_ERROR, load_ptr(prior, 0))
    _td_hold(slots, pins, _NT_SELF, py_tuple_get(args, 0))
    if not py_err_occurred():
        _td_hold(slots, pins, _TD_OUTPUT,
                 _nt_cleanup(load_ptr(slots, _NT_SELF * C_POINTER_SIZE), mode))
    if mode == 2:
        py_clear_exception()
        saved = load_ptr(prior, 0)
        if not ptr_is_null(saved):
            pcc_gc_unpin(saved)
            if load_i64(pins, _NT_CLOSE_ERROR * C_POINTER_SIZE):
                store_i32(saved, PYOBJECTHEADER_FLAGS_OFFSET,
                          load_i32(saved, PYOBJECTHEADER_FLAGS_OFFSET) | PY_FLAG_GC_PINNED)
        py_tls_exc_swap_slot(prior)
        store_i64(pins, _NT_CLOSE_ERROR * C_POINTER_SIZE, 0)
    return _nt_finish(slots, pins)


@c_abi_export("pcc_namedtempfile_getattr_entry")
def _nt_getattr(captures, args):
    if py_tuple_len(args) != 2:
        return _td_error(3, cstr("__getattr__ expects a name"))
    slots = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    pins = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    memset(slots, 0, _TD_SLOTS * C_POINTER_SIZE)
    memset(pins, 0, _TD_SLOTS * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_tempdir_frame_map"), slots)
    _td_hold(slots, pins, _NT_SELF, py_tuple_get(args, 0))
    _td_hold(slots, pins, _NT_METHOD_NAME, py_tuple_get(args, 1))
    name = py_str_utf8(load_ptr(slots, _NT_METHOD_NAME * C_POINTER_SIZE))
    if load_i8(name, 0) == 95 or strcmp(name, cstr("file")) == 0 or strcmp(name, cstr("name")) == 0:
        _td_error(6, cstr("temporary file wrapper attribute is not initialized"))
    else:
        _td_hold(slots, pins, _NT_METHOD_FILE,
                 py_obj_getattr(load_ptr(slots, _NT_SELF * C_POINTER_SIZE), cstr("file")))
        if not py_err_occurred():
            _td_hold(slots, pins, _NT_METHOD_VALUE,
                     py_obj_getattr(load_ptr(slots, _NT_METHOD_FILE * C_POINTER_SIZE), name))
        if not py_err_occurred():
            _td_hold(slots, pins, _NT_METHOD_BOOL,
                     py_builtin_callable(load_ptr(slots, _NT_METHOD_VALUE * C_POINTER_SIZE)))
        if not py_err_occurred():
            if py_obj_truthy(load_ptr(slots, _NT_METHOD_BOOL * C_POINTER_SIZE)):
                # A saved write/read/etc method keeps the wrapper and its
                # delete policy alive, not just the underlying FILE object.
                _td_hold(slots, pins, _NT_METHOD_CAPTURES, py_tuple_new(2))
                if not py_err_occurred():
                    py_tuple_set_item(load_ptr(slots, _NT_METHOD_CAPTURES * C_POINTER_SIZE), 0,
                                      load_ptr(slots, _NT_SELF * C_POINTER_SIZE))
                    py_tuple_set_item(load_ptr(slots, _NT_METHOD_CAPTURES * C_POINTER_SIZE), 1,
                                      load_ptr(slots, _NT_METHOD_VALUE * C_POINTER_SIZE))
                    _td_hold(slots, pins, _TD_OUTPUT, py_func_new_named(
                        function_addr("pcc_namedtempfile_delegate_entry"),
                        load_ptr(slots, _NT_METHOD_CAPTURES * C_POINTER_SIZE), cstr("temporary_file_method")))
            else:
                value = load_ptr(slots, _NT_METHOD_VALUE * C_POINTER_SIZE)
                py_incref(value)
                _td_hold(slots, pins, _TD_OUTPUT, value)
    return _nt_finish(slots, pins)


@c_abi_export("pcc_namedtempfile_delegate_entry")
def _nt_delegate(captures, args):
    slots = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    pins = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    memset(slots, 0, _TD_SLOTS * C_POINTER_SIZE)
    memset(pins, 0, _TD_SLOTS * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_tempdir_frame_map"), slots)
    py_incref(captures)
    _td_hold(slots, pins, _NT_METHOD_CAPTURES, captures)
    py_incref(args)
    _td_hold(slots, pins, _NT_METHOD_ARGS, args)
    _td_hold(slots, pins, _NT_METHOD_VALUE,
             py_tuple_get(load_ptr(slots, _NT_METHOD_CAPTURES * C_POINTER_SIZE), 1))
    if not py_err_occurred():
        _td_hold(slots, pins, _TD_OUTPUT, py_obj_call(
            load_ptr(slots, _NT_METHOD_VALUE * C_POINTER_SIZE),
            load_ptr(slots, _NT_METHOD_ARGS * C_POINTER_SIZE), null()))
    return _nt_finish(slots, pins)


def _nt_access_entry(args, operation: int):
    if py_tuple_len(args) != 1:
        return _td_error(3, cstr("temporary file context/iterator expects no arguments"))
    slots = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    pins = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    memset(slots, 0, _TD_SLOTS * C_POINTER_SIZE)
    memset(pins, 0, _TD_SLOTS * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_tempdir_frame_map"), slots)
    _td_hold(slots, pins, _NT_SELF, py_tuple_get(args, 0))
    _td_hold(slots, pins, _NT_METHOD_FILE,
             py_obj_getattr(load_ptr(slots, _NT_SELF * C_POINTER_SIZE), cstr("file")))
    if not py_err_occurred():
        _td_hold(slots, pins, _NT_METHOD_BOOL,
                 py_obj_getattr(load_ptr(slots, _NT_METHOD_FILE * C_POINTER_SIZE), cstr("closed")))
    if not py_err_occurred():
        if py_obj_truthy(load_ptr(slots, _NT_METHOD_BOOL * C_POINTER_SIZE)):
            _td_error(2, cstr("I/O operation on closed file."))
        elif operation == 2:
            value = py_obj_next(load_ptr(slots, _NT_METHOD_FILE * C_POINTER_SIZE))
            if ptr_is_null(value) and not py_err_occurred():
                _td_error(8, cstr(""))
            _td_hold(slots, pins, _TD_OUTPUT, value)
        else:
            value = load_ptr(slots, _NT_SELF * C_POINTER_SIZE)
            py_incref(value)
            _td_hold(slots, pins, _TD_OUTPUT, value)
    return _nt_finish(slots, pins)


@c_abi_export("pcc_namedtempfile_enter_entry")
def _nt_enter(captures, args):
    return _nt_access_entry(args, 0)


@c_abi_export("pcc_namedtempfile_iter_entry")
def _nt_iter(captures, args):
    return _nt_access_entry(args, 1)


@c_abi_export("pcc_namedtempfile_next_entry")
def _nt_next(captures, args):
    return _nt_access_entry(args, 2)


def _temporary_type(named_file: int):
    slots = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    pins = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    memset(slots, 0, _TD_SLOTS * C_POINTER_SIZE)
    memset(pins, 0, _TD_SLOTS * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_tempdir_frame_map"), slots)
    mutex = _td_mutex()
    if ptr_is_null(mutex):
        return _nt_finish(slots, pins)
    source = global_addr("pcc_namedtempfile_wrapper_type") if named_file else global_addr("pcc_tempdir_class")
    handle_slot = global_addr("pcc_namedtempfile_wrapper_root_handle") if named_file else global_addr("pcc_tempdir_class_root_handle")
    pcc_mutex_lock(mutex)
    present: int = not ptr_is_null(load_ptr(source, 0))
    pcc_mutex_unlock(mutex)
    if present != 0:
        _td_copy_cached_owner(slots, pins, source)
        return _nt_finish(slots, pins)
    # Construction occurs outside the publication mutex. Every candidate is
    # rooted; only a complete class is published to the permanent owner.
    _td_hold(slots, pins, _TYPE_CANDIDATE, py_class_new(
        cstr("_TemporaryFileWrapper") if named_file else cstr("TemporaryDirectory"),
        null(), 0, null(), 0))
    if not py_err_occurred():
        if named_file:
            _td_hold(slots, pins, _FILE_CAPTURES_EMPTY, py_tuple_new(0))
        else:
            captures = _td_init_signature(slots, pins)
            _td_add_init(load_ptr(slots, _TYPE_CANDIDATE * C_POINTER_SIZE),
                function_addr("pcc_tempdir_init_entry"), captures, slots, pins)
    cls = load_ptr(slots, _TYPE_CANDIDATE * C_POINTER_SIZE)
    captures = load_ptr(slots, _FILE_CAPTURES_EMPTY * C_POINTER_SIZE)
    if not py_err_occurred():
        if named_file:
            _td_add_method(cls, cstr("close"), function_addr("pcc_namedtempfile_close_entry"), captures, slots, pins)
            _td_add_method(cls, cstr("__enter__"), function_addr("pcc_namedtempfile_enter_entry"), captures, slots, pins)
            _td_add_method(cls, cstr("__exit__"), function_addr("pcc_namedtempfile_exit_entry"), captures, slots, pins)
            _td_add_method(cls, cstr("__del__"), function_addr("pcc_namedtempfile_del_entry"), captures, slots, pins)
            _td_add_method(cls, cstr("__getattr__"), function_addr("pcc_namedtempfile_getattr_entry"), captures, slots, pins)
            _td_add_method(cls, cstr("__iter__"), function_addr("pcc_namedtempfile_iter_entry"), captures, slots, pins)
            _td_add_method(cls, cstr("__next__"), function_addr("pcc_namedtempfile_next_entry"), captures, slots, pins)
        else:
            _td_add_method(cls, cstr("__enter__"), function_addr("pcc_tempdir_enter_entry"), captures, slots, pins)
            _td_add_method(cls, cstr("__exit__"), function_addr("pcc_tempdir_exit_entry"), captures, slots, pins)
            _td_add_method(cls, cstr("cleanup"), function_addr("pcc_tempdir_cleanup_entry"), captures, slots, pins)
            _td_add_method(cls, cstr("__repr__"), function_addr("pcc_tempdir_repr_entry"), captures, slots, pins)
            if not py_err_occurred():
                _td_hold(slots, pins, _TYPE_MODULE, py_str_new(cstr("tempfile"), 8))
            if not py_err_occurred():
                py_class_setattr(cls, cstr("__module__"), load_ptr(slots, _TYPE_MODULE * C_POINTER_SIZE))
    root_failed: int = 0
    shutdown_failed: int = 0
    if not py_err_occurred():
        pcc_mutex_lock(mutex)
        if ptr_is_null(load_ptr(source, 0)):
            handle = pcc_gc_scheduler_root_register_handle(source)
            if ptr_is_null(handle):
                root_failed = 1
            elif named_file == 0 and atexit(_td_shutdown) != 0:
                pcc_gc_scheduler_root_unregister_handle(handle)
                shutdown_failed = 1
            else:
                store_ptr(handle_slot, 0, handle)
                pcc_gc_store_root(source, load_ptr(slots, _TYPE_CANDIDATE * C_POINTER_SIZE))
        pcc_mutex_unlock(mutex)
    if root_failed != 0:
        _td_error(19, cstr("temporary class root registration failed"))
    elif shutdown_failed != 0:
        _td_error(19, cstr("TemporaryDirectory could not register shutdown cleanup"))
    if not py_err_occurred():
        # Reload through the authoritative root after registration/relocation.
        # Losing local candidate roots retire after the winner has its own lease.
        _td_copy_cached_owner(slots, pins, source)
    return _nt_finish(slots, pins)


def _nt_wrapper_type():
    return _temporary_type(1)


@c_abi_export("pcc_namedtempfile_entry")
def _nt_entry(captures, args):
    if py_tuple_len(args) != _NT_ARGUMENT_COUNT:
        return _td_error(3, cstr("NamedTemporaryFile expects ten bound arguments"))
    slots = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    pins = stack_alloc(_TD_SLOTS * C_POINTER_SIZE)
    memset(slots, 0, _TD_SLOTS * C_POINTER_SIZE)
    memset(pins, 0, _TD_SLOTS * C_POINTER_SIZE)
    pcc_gc_frame_enter(global_addr("pcc_tempdir_frame_map"), slots)
    index: int = _NT_MODE
    while index < _NT_ARGUMENT_COUNT:
        _td_hold(slots, pins, index, py_tuple_get(args, index))
        index = index + 1
    buffering: int = -1
    if not py_err_occurred():
        buffering = _file_c_int_argument(slots, _NT_BUFFERING)
    descriptor_out = stack_alloc(C_POINTER_SIZE)
    store_i64(descriptor_out, 0, -1)
    if not py_err_occurred():
        _nt_create_name(slots, pins, descriptor_out)
    descriptor: int = load_i64(descriptor_out, 0)
    if descriptor >= 0 and not py_err_occurred():
        _td_hold(slots, pins, _NT_DESCRIPTOR, py_int_from_i64(descriptor))
        if not py_err_occurred():
            _td_hold(slots, pins, _NT_FILE, py_file_fdopen_options(
                load_ptr(slots, _NT_DESCRIPTOR * C_POINTER_SIZE),
                load_ptr(slots, _NT_MODE * C_POINTER_SIZE),
                load_ptr(slots, _NT_ENCODING * C_POINTER_SIZE),
                load_ptr(slots, _NT_ERRORS * C_POINTER_SIZE),
                load_ptr(slots, _NT_NEWLINE * C_POINTER_SIZE), 1, buffering))
        if not ptr_is_null(load_ptr(slots, _NT_FILE * C_POINTER_SIZE)):
            descriptor = -1  # The file owner now closes this descriptor.
        if not py_err_occurred():
            _td_hold(slots, pins, _NT_TYPE, _nt_wrapper_type())
        if not py_err_occurred():
            _td_hold(slots, pins, _NT_WRAPPER, py_instance_new(load_ptr(slots, _NT_TYPE * C_POINTER_SIZE)))
        if not py_err_occurred():
            manager = load_ptr(slots, _NT_WRAPPER * C_POINTER_SIZE)
            py_obj_setattr(manager, cstr("_ready"), global_load_ptr("py_False"))
            py_obj_setattr(manager, cstr("name"), load_ptr(slots, _NT_PATH * C_POINTER_SIZE))
            py_obj_setattr(manager, cstr("file"), load_ptr(slots, _NT_FILE * C_POINTER_SIZE))
            py_obj_setattr(manager, cstr("_cleanup_name"), load_ptr(slots, _NT_PATH * C_POINTER_SIZE))
            py_obj_setattr(manager, cstr("_cleanup_file"), load_ptr(slots, _NT_FILE * C_POINTER_SIZE))
            py_obj_setattr(manager, cstr("_delete"), load_ptr(slots, _NT_DELETE * C_POINTER_SIZE))
            py_obj_setattr(manager, cstr("delete"), load_ptr(slots, _NT_DELETE * C_POINTER_SIZE))
            py_obj_setattr(manager, cstr("_delete_on_close"), load_ptr(slots, _NT_DELETE_ON_CLOSE * C_POINTER_SIZE))
            py_obj_setattr(manager, cstr("_removed"), global_load_ptr("py_False"))
            if not py_err_occurred():
                py_obj_setattr(manager, cstr("_ready"), global_load_ptr("py_True"))
                if not py_err_occurred():
                    py_incref(manager)
                    _td_hold(slots, pins, _TD_OUTPUT, manager)
    if ptr_is_null(load_ptr(slots, _TD_OUTPUT * C_POINTER_SIZE)):
        if descriptor >= 0:
            close(descriptor)
        file = load_ptr(slots, _NT_FILE * C_POINTER_SIZE)
        if not ptr_is_null(file):
            py_file_close(file)
        path = load_ptr(slots, _NT_PATH * C_POINTER_SIZE)
        if not ptr_is_null(path):
            unlinkat(py_str_utf8(path), 0)
    return _nt_finish(slots, pins)


def _td_add_method(cls, name, entry, captures, slots, pins) -> None:
    if py_err_occurred():
        return
    _td_hold(slots, pins, _TYPE_METHOD, py_func_new_named(entry, captures, name))
    if not py_err_occurred():
        py_class_setattr(cls, name, load_ptr(slots, _TYPE_METHOD * C_POINTER_SIZE))
    _td_drop(slots, pins, _TYPE_METHOD)


def _td_add_init(cls, entry, captures, slots, pins) -> None:
    # Instance construction resolves __init__ from the method table
    # (py_class_lookup), not the attribute dict. Publish the function in both:
    # the attribute owns it, the table entry is a borrowed metadata slot.
    if py_err_occurred():
        return
    _td_hold(slots, pins, _TYPE_METHOD, py_func_new_named(entry, captures, cstr("__init__")))
    if not py_err_occurred():
        method = load_ptr(slots, _TYPE_METHOD * C_POINTER_SIZE)
        if py_class_setattr(cls, cstr("__init__"), method) == 0:
            py_class_add_method(cls, cstr("__init__"), method)
    _td_drop(slots, pins, _TYPE_METHOD)


@c_abi_export("py_tempdir_type")
def py_tempdir_type():
    return _temporary_type(0)
