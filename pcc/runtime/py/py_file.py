"""pcc-Python port of py_file.c."""

__pcc_runtime_port__ = True

from pcc.extern import (
    extern,
    c_abi_export,
    c_int32,
    c_int64,
    c_ptr,
    c_size_t,
    c_void,
)
from pcc.runtime.py.py_abi_constants import (
    PYBYTESOBJECT_BYTE_LEN_OFFSET, PYBYTESOBJECT_DATA_OFFSET,
    PYMEMORYVIEWOBJECT_BASE_OFFSET, PYOBJECTHEADER_FLAGS_OFFSET,
    PYOBJECTHEADER_REFCOUNT_OFFSET, PYOBJECTHEADER_TYPE_TAG_OFFSET,
    PY_FLAG_GC_MALLOC_ALLOC, PY_FLAG_GC_PINNED, PY_FLAG_IMMORTAL, PY_TYPE_BOOL,
    PY_TYPE_BYTEARRAY, PY_TYPE_BYTES, PY_TYPE_FILE, PY_TYPE_INT,
    PY_TYPE_LIST, PY_TYPE_MEMORYVIEW, PY_TYPE_STR, PY_TYPE_TUPLE,
)
from pcc.unsafe import (
    cstr,
    define_global_ptr_null,
    define_global_i32,
    stack_alloc,
    free,
    global_addr,
    global_load_ptr,
    global_store_ptr,
    is_tagged_int,
    load_i8,
    load_i32,
    load_i64,
    load_ptr,
    malloc,
    memcpy,
    memset,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    realloc,
    seek_file,
    store_i8,
    store_i32,
    store_i64,
    store_ptr,
    strlen,
)

fclose = extern("fclose", (c_ptr,), c_int32)
ferror = extern("ferror", (c_ptr,), c_int32)
fflush = extern("fflush", (c_ptr,), c_int32)
fgetc = extern("fgetc", (c_ptr,), c_int32)
fopen = extern("fopen", (c_ptr, c_ptr), c_ptr)
pcc_stdio_fdopen = extern("pcc_stdio_fdopen", (c_int64, c_ptr, c_int64, c_int64), c_ptr)
fread = extern("fread", (c_ptr, c_size_t, c_size_t, c_ptr), c_size_t)
# LP64 targets (aarch64-darwin / x86_64-linux): C ``long`` is 64-bit, so
# fseek/ftell take/return c_int64 here.
fseek = extern("fseek", (c_ptr, c_int64, c_int32), c_int32)
ftell = extern("ftell", (c_ptr,), c_int64)
fwrite = extern("fwrite", (c_ptr, c_size_t, c_size_t, c_ptr), c_size_t)
fileno = extern("fileno", (c_ptr,), c_int32)
isatty = extern("isatty", (c_int32,), c_int32)
py_func_new_bound = extern("py_func_new_bound", (c_ptr, c_ptr, c_ptr, c_ptr), c_ptr)
py_obj_iter = extern("py_obj_iter", (c_ptr,), c_ptr)
py_obj_next = extern("py_obj_next", (c_ptr,), c_ptr)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_current_exception = extern("py_current_exception", (), c_ptr)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_exc_matches = extern("py_exc_matches", (c_ptr, c_ptr), c_int64)
py_exc_builtin_class = extern("py_exc_builtin_class", (c_int64,), c_ptr)
pcc_gc_pointer_register = extern("pcc_gc_pointer_register", (c_ptr,), c_int64)
strcmp = extern("strcmp", (c_ptr, c_ptr), c_int32)

define_global_ptr_null("py_sys_stdin_object")
define_global_ptr_null("py_sys_stdout_object")
define_global_ptr_null("py_sys_stderr_object")

py_decref = extern("py_decref", (c_ptr,), c_void)
py_incref = extern("py_incref", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_bool_from_bit = extern("py_bool_from_bit", (c_int32,), c_ptr)
py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
py_int_value_i64 = extern("py_int_value_i64", (c_ptr,), c_int64)
py_int_to_i64 = extern("py_int_to_i64", (c_ptr, c_ptr), c_int64)
py_index_i64_checked_slots = extern("py_index_i64_checked_slots", (c_ptr,), c_int64)
py_list_new = extern("py_list_new", (c_int64,), c_ptr)
py_list_append = extern("py_list_append", (c_ptr, c_ptr), c_void)
py_list_get = extern("py_list_get", (c_ptr, c_int64), c_ptr)
py_list_set = extern("py_list_set", (c_ptr, c_int64, c_ptr), c_void)
py_list_len = extern("py_list_len", (c_ptr,), c_int64)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_tuple_get = extern("py_tuple_get", (c_ptr, c_int64), c_ptr)
py_tuple_len = extern("py_tuple_len", (c_ptr,), c_int64)
py_obj_call = extern("py_obj_call", (c_ptr, c_ptr, c_ptr), c_ptr)
py_obj_getattr = extern("py_obj_getattr", (c_ptr, c_ptr), c_ptr)
py_obj_special_call_slots = extern("py_obj_special_call_slots",
    (c_ptr, c_ptr, c_ptr, c_ptr, c_ptr, c_ptr), c_int64)
py_obj_setattr = extern("py_obj_setattr", (c_ptr, c_ptr, c_ptr), c_int64)
pcc_errno_get = extern("pcc_errno_get", (), c_int32)
pcc_errno_exception_kind = extern("pcc_errno_exception_kind", (c_int32,), c_int64)
pcc_errno_message_into = extern("pcc_errno_message_into", (c_int32, c_ptr, c_int64), c_int32)
py_str_splitlines_keepends = extern(
    "py_str_splitlines_keepends", (c_ptr, c_int32), c_ptr
)
py_bytes_new = extern("py_bytes_new", (c_ptr, c_int64), c_ptr)
pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_ptr)
py_obj_str = extern("py_obj_str", (c_ptr,), c_ptr)
py_obj_type_name = extern("py_obj_type_name", (c_ptr,), c_ptr)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
py_str_byte_len = extern("py_str_byte_len", (c_ptr,), c_int64)
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)


def _type_of(obj) -> int:
    if is_tagged_int(obj):
        return PY_TYPE_INT
    return load_i32(obj, 8)


def _coerce_str(o):
    if ptr_is_null(o):
        return null()
    if _type_of(o) == PY_TYPE_STR:
        return o
    return py_obj_str(o)


def _checked_file(file):
    if ptr_is_null(file):
        return null()
    if _type_of(file) != PY_TYPE_FILE:
        return null()
    if load_i32(file, 24) != 0:
        return null()
    if ptr_is_null(load_ptr(file, 16)):
        return null()
    return file


def _mode_is_binary(mode_s) -> int:
    if ptr_is_null(mode_s):
        return 0
    data = py_str_utf8(mode_s)
    n: int = py_str_byte_len(mode_s)
    i: int = 0
    while i < n:
        if (load_i8(data, i) & 255) == 98:
            return 1
        i = i + 1
    return 0


# Offset 32 of a file object: bits 0-7 hold ``fd + 1`` for a standard stream
# (0 for opened files), bit 8 readable, bit 9 writable.
def _mode_access_bits(mode_s) -> int:
    if ptr_is_null(mode_s):
        return 256
    data = py_str_utf8(mode_s)
    n: int = py_str_byte_len(mode_s)
    bits: int = 0
    i: int = 0
    while i < n:
        c: int = load_i8(data, i) & 255
        if c == 114:  # r
            bits = bits | 256
        elif c == 119 or c == 97 or c == 120:  # w a x
            bits = bits | 512
        elif c == 43:  # +
            bits = bits | 768
        i = i + 1
    return bits


def _file_std_fd_plus1(file) -> int:
    return load_i64(file, 32) & 255


def _file_binary(file) -> int:
    return load_i32(file, 28)


def _file_bytes_or_str(file, data, n: int):
    if _file_binary(file) != 0:
        return py_bytes_new(data, n)
    return py_str_new(data, n)


def _file_bytes_like_base(value):
    current = value
    while not ptr_is_null(current) and not is_tagged_int(current):
        tag: int = _type_of(current)
        if tag == PY_TYPE_BYTES or tag == PY_TYPE_BYTEARRAY:
            return current
        if tag != PY_TYPE_MEMORYVIEW:
            return null()
        current = pcc_gc_load_ptr(
            current,
            ptr_add(current, PYMEMORYVIEWOBJECT_BASE_OFFSET),
        )
    return null()


# File body: header + FILE* + closed/binary + access/codec/error bits + native
# decoder-state pointer (48 bytes). The decoder owns only malloc storage;
# managed owners stay in the explicit frame below. The GC has no extra edges.
_FILE_FIRST = 0
_FILE_SECOND = 1
_FILE_ENCODING = 2
_FILE_ERRORS = 3
_FILE_NEWLINE = 4
_FILE_TEMP = 5
_FILE_RESULT = 6
_FILE_ERROR = 7
_FILE_COUNT = 8
_FILE_BYTES = 8
_FILE_SIZE = 48
_FILE_STATE_SIZE = 96
# Offset 32 keeps the original descriptor name after the FILE has closed.
_FILE_DESCRIPTOR_NAME = 1073741824
_FILE_DESCRIPTOR_SHIFT = 32
_FILE_FDOPEN_OWNED = 6
_FILE_FDOPEN_BORROWED = 7
_FILE_ERRNO_EXCEPTION = 0
_FILE_ERRNO_NUMBER = 1
_FILE_ERRNO_MESSAGE = 2
_FILE_ERRNO_ARGS = 3
_FILE_ERRNO_FILENAME = 4
_FILE_DYNAMIC_METHOD = 8
_FILE_BOUND_METHOD = 9
_FILE_FSPATH = 10
_FILE_METHOD_CAPTURES = 0
_FILE_METHOD_ARGS = 1
_FILE_METHOD_CODE = 2
_FILE_METHOD_OPERAND = 3
_FILE_METHOD_RECEIVER = 5

# Native decoder-state layout. Input tails never exceed three UTF-8 bytes.
_FILE_DECODED = 0
_FILE_LENGTH = 8
_FILE_POSITION = 16
_FILE_TAIL = 24
_FILE_TAIL_LENGTH = 32
_FILE_EOF = 40
_FILE_PENDING_CR = 48
_FILE_CR_OFFSET = 56
_FILE_RAW_OFFSET = 64
_FILE_MAP = 72
_FILE_LOGICAL_OFFSET = 80
# Error-handler span list filled by py_text_decode_buffer_spans.
_TEXT_SPANS_RECORDS = 0
_TEXT_SPANS_COUNT = 8
_TEXT_SPANS_CAPACITY = 16
_TEXT_SPAN_BYTES = 24
_TEXT_SPAN_OUT_START = 0
_TEXT_SPAN_OUT_LENGTH = 8
_TEXT_SPAN_RAW_END = 16
# tell() inside a multi-character handler unit (backslashreplace) returns the
# unit's first input byte plus, from bit 56, the characters already returned.
# seek() re-decodes the unit and skips them, like CPython's opaque cookies.
_FILE_COOKIE_SKIP_SHIFT = 56
_FILE_COOKIE_OFFSET_MASK = 72057594037927935

pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
pcc_gc_frame_leave = extern("pcc_gc_frame_leave", (c_ptr,), c_void)
pcc_gc_store_root = extern("pcc_gc_store_root", (c_ptr, c_ptr), c_void)
pcc_gc_root_copy_borrowed_lease = extern("pcc_gc_root_copy_borrowed_lease", (c_ptr, c_ptr), c_int64)
pcc_gc_foreign_lease_acquire = extern("pcc_gc_foreign_lease_acquire", (c_ptr,), c_int64)
pcc_gc_foreign_lease_release = extern("pcc_gc_foreign_lease_release", (c_ptr, c_int64), c_int64)
pcc_gc_take_pinned_slot = extern("pcc_gc_take_pinned_slot", (c_ptr, c_int64), c_ptr)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
pcc_platform_abort = extern("pcc_platform_abort", (), c_void)
py_tls_exc_swap_slot = extern("py_tls_exc_swap_slot", (c_ptr,), c_void)
py_text_codec_id = extern("py_text_codec_id", (c_ptr,), c_int64)
py_text_error_id = extern("py_text_error_id", (c_ptr,), c_int64)
py_text_decode_buffer_spans = extern(
    "py_text_decode_buffer_spans",
    (c_ptr, c_int64, c_int64, c_int64, c_int64, c_ptr, c_ptr), c_int64,
)
py_text_encode_ids = extern("py_text_encode_ids", (c_ptr, c_int64, c_int64), c_ptr)
py_bytes_from_obj = extern("py_bytes_from_obj", (c_ptr,), c_ptr)
py_str_len = extern("py_str_len", (c_ptr,), c_int64)
define_global_i32("pcc_file_borrowed_map", -5)
define_global_i32("pcc_file_owned_map", _FILE_COUNT)


def _file_error(kind: int, message) -> int:
    if py_err_occurred() == 0:
        py_raise_owned(py_exc_new(kind, message))
    return -1


def _file_drop(slots, tokens, index: int) -> None:
    slot = ptr_add(slots, index * _FILE_BYTES)
    token: int = load_i64(tokens, index * _FILE_BYTES)
    if token >= 0:
        if pcc_gc_foreign_lease_release(slot, token) < 0:
            pcc_platform_abort()
            return
    store_i64(tokens, index * _FILE_BYTES, -1)
    pcc_gc_store_root(slot, null())


def _file_adopt(slots, tokens, index: int) -> int:
    slot = ptr_add(slots, index * _FILE_BYTES)
    if ptr_is_null(load_ptr(slot, 0)):
        return _file_error(19, cstr("file operation could not allocate a result"))
    token: int = pcc_gc_foreign_lease_acquire(slot)
    store_i64(tokens, index * _FILE_BYTES, token)
    if token < 0:
        return _file_error(19, cstr("file operation could not lease an owner"))
    return 0


def _file_is_none(value) -> int:
    return ptr_is_null(value) or ptr_eq(value, global_load_ptr("py_None"))


def _file_encoding(file) -> int:
    return (load_i64(file, 32) >> 10) & 3


def _file_errors(file) -> int:
    # Policy 8 keeps an unknown handler name; like CPython, it is reported
    # only when a codec error needs the handler.
    policy: int = (load_i64(file, 32) >> 12) & 15
    return -1 if policy == 8 else policy


def _file_validate_cstr(value, label) -> int:
    if ptr_is_null(value) or is_tagged_int(value) or _type_of(value) != PY_TYPE_STR:
        return _file_error(3, label)
    count: int = py_str_byte_len(value)
    data = py_str_utf8(value)
    index: int = 0
    while index < count:
        if load_i8(data, index) == 0:
            return _file_error(2, cstr("embedded null character"))
        index = index + 1
    return 0


def _file_validate_mode(mode) -> int:
    data = py_str_utf8(mode)
    count: int = py_str_byte_len(mode)
    seen: int = 0
    base_count: int = 0
    index: int = 0
    while index < count:
        char: int = load_i8(data, index)
        bit: int = 0
        if char == 114 or char == 119 or char == 97 or char == 120:
            base_count = base_count + 1
        elif char == 98:
            bit = 1
        elif char == 116:
            bit = 2
        elif char == 43:
            bit = 4
        else:
            return _file_error(2, cstr("invalid file mode"))
        if bit and (seen & bit):
            return _file_error(2, cstr("invalid duplicate mode character"))
        seen = seen | bit
        index = index + 1
    if base_count != 1 or (seen & 3) == 3:
        return _file_error(2, cstr("must have exactly one of create/read/write/append and text/binary mode"))
    return 0


def _file_native_mode(mode, output) -> None:
    data = py_str_utf8(mode)
    count: int = py_str_byte_len(mode)
    index: int = 0
    base: int = 114
    plus: int = 0
    binary: int = 0
    while index < count:
        char: int = load_i8(data, index)
        if char == 114 or char == 119 or char == 97 or char == 120:
            base = char
        elif char == 43:
            plus = 1
        elif char == 98:
            binary = 1
        index = index + 1
    store_i8(output, 0, base)
    index = 1
    if binary:
        store_i8(output, index, 98)
        index = index + 1
    if plus:
        store_i8(output, index, 43)
        index = index + 1
    store_i8(output, index, 0)


def _file_raise_errno(code: int, filename_slot) -> int:
    slots = stack_alloc(_FILE_COUNT * _FILE_BYTES)
    tokens = stack_alloc(_FILE_COUNT * _FILE_BYTES)
    memset(slots, 0, _FILE_COUNT * _FILE_BYTES)
    index: int = 0
    while index < _FILE_COUNT:
        store_i64(tokens, index * _FILE_BYTES, -1)
        index = index + 1
    pcc_gc_frame_enter(global_addr("pcc_file_owned_map"), slots)
    if not ptr_is_null(filename_slot):
        # Copy from the caller's authoritative input slot before allocating.
        # This owner preserves the exact str/bytes filename through cleanup.
        token: int = pcc_gc_root_copy_borrowed_lease(
            ptr_add(slots, _FILE_ERRNO_FILENAME * _FILE_BYTES), filename_slot)
        store_i64(tokens, _FILE_ERRNO_FILENAME * _FILE_BYTES, token)
        if token < 0:
            _file_error(19, cstr("file error could not retain its filename"))
    message = stack_alloc(256)
    pcc_errno_message_into(code, message, 256)
    count: int = 0
    while count < 255 and load_i8(message, count) != 0:
        count = count + 1
    if not py_err_occurred():
        kind: int = pcc_errno_exception_kind(code)
        store_ptr(slots, _FILE_ERRNO_EXCEPTION * _FILE_BYTES, py_exc_new(kind, message))
    if not py_err_occurred() and _file_adopt(slots, tokens, _FILE_ERRNO_EXCEPTION) == 0:
        store_ptr(slots, _FILE_ERRNO_NUMBER * _FILE_BYTES, py_int_from_i64(code))
        if _file_adopt(slots, tokens, _FILE_ERRNO_NUMBER) == 0:
            store_ptr(slots, _FILE_ERRNO_MESSAGE * _FILE_BYTES, py_str_new(message, count))
            if _file_adopt(slots, tokens, _FILE_ERRNO_MESSAGE) == 0:
                store_ptr(slots, _FILE_ERRNO_ARGS * _FILE_BYTES, py_tuple_new(2))
                _file_adopt(slots, tokens, _FILE_ERRNO_ARGS)
        if not py_err_occurred():
            exception = load_ptr(slots, _FILE_ERRNO_EXCEPTION * _FILE_BYTES)
            number = load_ptr(slots, _FILE_ERRNO_NUMBER * _FILE_BYTES)
            text = load_ptr(slots, _FILE_ERRNO_MESSAGE * _FILE_BYTES)
            args = load_ptr(slots, _FILE_ERRNO_ARGS * _FILE_BYTES)
            py_tuple_set_item(args, 0, number)
            py_tuple_set_item(args, 1, text)
            py_obj_setattr(exception, cstr("errno"), number)
            if not py_err_occurred():
                py_obj_setattr(exception, cstr("strerror"), text)
            if not py_err_occurred() and not ptr_is_null(filename_slot):
                py_obj_setattr(exception, cstr("filename"),
                    load_ptr(slots, _FILE_ERRNO_FILENAME * _FILE_BYTES))
            if not py_err_occurred():
                py_obj_setattr(exception, cstr("args"), args)
            if not py_err_occurred():
                py_incref(exception)
                py_raise_owned(exception)
    py_tls_exc_swap_slot(ptr_add(slots, _FILE_ERROR * _FILE_BYTES))
    index = _FILE_ERRNO_FILENAME
    while index >= _FILE_ERRNO_EXCEPTION:
        _file_drop(slots, tokens, index)
        index = index - 1
    py_clear_exception()
    py_tls_exc_swap_slot(ptr_add(slots, _FILE_ERROR * _FILE_BYTES))
    pcc_gc_frame_leave(slots)
    return -1


def _file_message_append(message, length: int, text, count: int) -> int:
    if count > 120:
        count = 120
    memcpy(ptr_add(message, length), text, count)
    return length + count


def _file_message_type(message, length: int, value) -> int:
    name = py_obj_type_name(value)
    if ptr_is_null(name):
        return length
    length = _file_message_append(message, length, py_str_utf8(name), py_str_byte_len(name))
    py_decref(name)
    return length


def _file_fspath_type_error(value, result) -> int:
    # CPython: "expected str, bytes or os.PathLike object, not float", and for
    # a bad result "expected Bad.__fspath__() to return str or bytes, not int".
    message = stack_alloc(400)
    length: int = 0
    if ptr_is_null(result):
        prefix = cstr("expected str, bytes or os.PathLike object, not ")
        length = _file_message_append(message, length, prefix, strlen(prefix))
        length = _file_message_type(message, length, value)
    else:
        prefix = cstr("expected ")
        length = _file_message_append(message, length, prefix, strlen(prefix))
        length = _file_message_type(message, length, value)
        middle = cstr(".__fspath__() to return str or bytes, not ")
        length = _file_message_append(message, length, middle, strlen(middle))
        length = _file_message_type(message, length, result)
    store_i8(message, length, 0)
    return _file_error(3, message)


def _file_fspath_body(slots, tokens) -> int:
    value = load_ptr(slots, _FILE_FIRST * _FILE_BYTES)
    if not ptr_is_null(value) and not is_tagged_int(value):
        tag: int = _type_of(value)
        if tag == PY_TYPE_STR or tag == PY_TYPE_BYTES:
            py_incref(value)
            store_ptr(slots, _FILE_RESULT * _FILE_BYTES, value)
            return _file_adopt(slots, tokens, _FILE_RESULT)
    # os.fspath performs special-method lookup on the type, bypassing an
    # instance's __getattribute__/__getattr__ and same-named instance field.
    handled = stack_alloc(_FILE_BYTES)
    store_i64(handled, 0, 0)
    if py_obj_special_call_slots(ptr_add(slots, _FILE_FIRST * _FILE_BYTES),
            cstr("__fspath__"), null(), null(),
            ptr_add(slots, _FILE_RESULT * _FILE_BYTES), handled) != 0:
        return -1
    if load_i64(handled, 0) == 0:
        return _file_fspath_type_error(load_ptr(slots, _FILE_FIRST * _FILE_BYTES), null())
    if _file_adopt(slots, tokens, _FILE_RESULT) != 0:
        return -1
    result = load_ptr(slots, _FILE_RESULT * _FILE_BYTES)
    if is_tagged_int(result) or (_type_of(result) != PY_TYPE_STR and _type_of(result) != PY_TYPE_BYTES):
        return _file_fspath_type_error(load_ptr(slots, _FILE_FIRST * _FILE_BYTES), result)
    return 0


def _file_open_body(slots, tokens, descriptor_mode: int = 0, buffering: int = -1) -> int:
    path = load_ptr(slots, _FILE_FIRST * _FILE_BYTES)
    # io.open resolves any other path object through os.fspath and reports
    # the converted str/bytes as OSError.filename; RESULT keeps it leased
    # until the native open has used its bytes.
    path_slot: int = _FILE_FIRST
    if descriptor_mode == 0 and not is_tagged_int(path):
        path_tag: int = -1 if ptr_is_null(path) else _type_of(path)
        if (path_tag != PY_TYPE_STR and path_tag != PY_TYPE_BYTES
                and path_tag != PY_TYPE_INT and path_tag != PY_TYPE_BOOL):
            if _file_fspath_body(slots, tokens) != 0:
                return -1
            path_slot = _FILE_RESULT
            path = load_ptr(slots, _FILE_RESULT * _FILE_BYTES)
    mode = load_ptr(slots, _FILE_SECOND * _FILE_BYTES)
    path_data = null()
    path_count: int = 0
    descriptor: int = -1
    if descriptor_mode != 0:
        if ptr_is_null(path) or (_type_of(path) != PY_TYPE_INT and _type_of(path) != PY_TYPE_BOOL):
            return _file_error(3, cstr("invalid fd type: expected integer"))
        overflow = stack_alloc(4)
        store_i32(overflow, 0, 0)
        descriptor = py_int_to_i64(path, overflow)
        if py_err_occurred():
            return -1
        # io.open considers only C-int-range integers to be descriptors.
        if load_i32(overflow, 0) != 0 or descriptor < -2147483648 or descriptor > 2147483647:
            return _file_error(3, cstr("expected str, bytes or os.PathLike object, not int"))
        if descriptor < 0:
            return _file_error(2, cstr("negative file descriptor"))
    elif not ptr_is_null(path) and not is_tagged_int(path) and _type_of(path) == PY_TYPE_BYTES:
        path_data = ptr_add(path, PYBYTESOBJECT_DATA_OFFSET)
        path_count = load_i64(path, PYBYTESOBJECT_BYTE_LEN_OFFSET)
        index: int = 0
        while index < path_count:
            if load_i8(path_data, index) == 0:
                return _file_error(2, cstr("embedded null byte"))
            index = index + 1
    else:
        if _file_validate_cstr(path, cstr("native open path must be str or bytes")) != 0:
            return -1
        path_data = py_str_utf8(path)
    if ptr_is_null(mode):
        store_ptr(slots, _FILE_TEMP * _FILE_BYTES, py_str_new(cstr("r"), 1))
        if _file_adopt(slots, tokens, _FILE_TEMP) != 0:
            return -1
        mode = load_ptr(slots, _FILE_TEMP * _FILE_BYTES)
    if _file_validate_cstr(mode, cstr("open mode must be str")) != 0:
        return -1
    if _file_validate_mode(mode) != 0:
        return -1
    binary: int = _mode_is_binary(mode)
    if descriptor_mode != 0 and buffering == 0 and binary == 0:
        return _file_error(2, cstr("cannot have unbuffered text I/O"))
    if descriptor_mode != 0 and buffering == 1:
        return _file_error(11, cstr("native fdopen line buffering is not implemented"))
    encoding = load_ptr(slots, _FILE_ENCODING * _FILE_BYTES)
    errors = load_ptr(slots, _FILE_ERRORS * _FILE_BYTES)
    newline = load_ptr(slots, _FILE_NEWLINE * _FILE_BYTES)
    if binary != 0:
        if _file_is_none(encoding) == 0:
            return _file_error(2, cstr("binary mode doesn't take an encoding argument"))
        if _file_is_none(errors) == 0:
            return _file_error(2, cstr("binary mode doesn't take an errors argument"))
        if _file_is_none(newline) == 0:
            return _file_error(2, cstr("binary mode doesn't take a newline argument"))
    codec: int = 0
    policy: int = 0
    if _file_is_none(encoding) == 0:
        codec = py_text_codec_id(encoding)
        if py_err_occurred() != 0:
            return -1
    if _file_is_none(errors) == 0:
        policy = py_text_error_id(errors)
        if py_err_occurred() != 0:
            return -1
    if policy < 0:
        policy = 8
    if _file_is_none(newline) == 0:
        return _file_error(11, cstr("native text files support universal newlines only"))
    access: int = _mode_access_bits(mode)
    mode_data = stack_alloc(4)
    _file_native_mode(mode, mode_data)
    fp = null()
    out = null()
    if descriptor_mode == 0:
        fp = fopen(path_data, mode_data)
    else:
        # An unsuccessful fdopen never consumes the caller's descriptor.
        # Finish managed allocation/rooting before native FILE adoption.
        out = pcc_gc_alloc(_FILE_SIZE, PY_TYPE_FILE, 0)
        if ptr_is_null(out):
            return _file_error(19, cstr("cannot allocate file object"))
        memset(ptr_add(out, 16), 0, _FILE_SIZE - 16)
        store_ptr(slots, _FILE_RESULT * _FILE_BYTES, out)
        if _file_adopt(slots, tokens, _FILE_RESULT) != 0:
            return -1
        fp = pcc_stdio_fdopen(descriptor, mode_data,
                             1 if descriptor_mode == _FILE_FDOPEN_OWNED else 0, buffering)
    if ptr_is_null(fp):
        # Snapshot errno before any allocation, formatting or owner release.
        code: int = pcc_errno_get()
        if descriptor_mode != 0:
            return _file_raise_errno(code, null())
        return _file_raise_errno(code, ptr_add(slots, path_slot * _FILE_BYTES))
    if descriptor_mode == 0:
        if path_slot == _FILE_RESULT:
            _file_drop(slots, tokens, _FILE_RESULT)
        out = pcc_gc_alloc(_FILE_SIZE, PY_TYPE_FILE, 0)
        if ptr_is_null(out):
            fclose(fp)
            return _file_error(19, cstr("cannot allocate file object"))
        memset(ptr_add(out, 16), 0, _FILE_SIZE - 16)
    else:
        out = load_ptr(slots, _FILE_RESULT * _FILE_BYTES)
    store_ptr(out, 16, fp)
    store_i32(out, 28, binary)
    metadata: int = access | (codec << 10) | (policy << 12)
    if descriptor_mode != 0:
        metadata = metadata | _FILE_DESCRIPTOR_NAME | (descriptor << _FILE_DESCRIPTOR_SHIFT)
    store_i64(out, 32, metadata)
    store_ptr(slots, _FILE_RESULT * _FILE_BYTES, out)
    if descriptor_mode != 0:
        return 0
    return _file_adopt(slots, tokens, _FILE_RESULT)


def _file_clear_decoder(file) -> None:
    state = load_ptr(file, 40)
    if ptr_is_null(state) == 0:
        store_ptr(file, 40, null())
        free(load_ptr(state, _FILE_DECODED))
        free(load_ptr(state, _FILE_MAP))
        free(state)


def _file_decoder(file):
    state = load_ptr(file, 40)
    if ptr_is_null(state):
        state = malloc(_FILE_STATE_SIZE)
        if ptr_is_null(state):
            _file_error(19, cstr("cannot allocate file decoder"))
            return null()
        memset(state, 0, _FILE_STATE_SIZE)
        # A failed ftell poisons the owned FILE error flag (notably on
        # pipes). Probe the descriptor directly, leaving FILE state intact.
        fp = load_ptr(file, 16)
        if (load_i64(file, 32) & 512) != 0:
            if fflush(fp) != 0:
                free(state)
                _file_error(14, cstr("file flush before read failed"))
                return null()
        position: int = seek_file(fileno(fp), 0, 1)
        if position < 0:
            position = 0
        store_i64(state, _FILE_RAW_OFFSET, position)
        store_i64(state, _FILE_LOGICAL_OFFSET, position)
        store_ptr(file, 40, state)
    return state


def _file_text_width(data, at: int) -> int:
    first: int = load_i8(data, at) & 255
    if first < 128:
        return 1
    if first < 224:
        return 2
    if first < 240:
        return 3
    return 4


def _file_text_fill(file, state) -> int:
    if load_i64(state, _FILE_EOF) != 0:
        return 0
    free(load_ptr(state, _FILE_DECODED))
    free(load_ptr(state, _FILE_MAP))
    store_ptr(state, _FILE_DECODED, null())
    store_ptr(state, _FILE_MAP, null())
    store_i64(state, _FILE_LENGTH, 0)
    store_i64(state, _FILE_POSITION, 0)
    raw = malloc(8196)
    if ptr_is_null(raw):
        return _file_error(19, cstr("cannot allocate file input buffer"))
    previous: int = load_i64(state, _FILE_TAIL_LENGTH)
    index: int = 0
    while index < previous:
        store_i8(raw, index, load_i8(state, _FILE_TAIL + index))
        index = index + 1
    origin: int = load_i64(state, _FILE_RAW_OFFSET) - previous
    received: int = fread(ptr_add(raw, previous), 1, 8192, load_ptr(file, 16))
    if received == 0 and ferror(load_ptr(file, 16)) != 0:
        free(raw)
        return _file_error(14, cstr("file read failed"))
    total: int = previous + received
    store_i64(state, _FILE_RAW_OFFSET, origin + total)
    final: int = 1 if received == 0 else 0
    return _file_decode_chunk(file, state, raw, total, origin, final)


def _file_decode_chunk(file, state, raw, total: int, origin: int, final: int) -> int:
    decoded = stack_alloc(24)
    spans = stack_alloc(24)
    memset(spans, 0, 24)
    status: int = py_text_decode_buffer_spans(
        raw, total, _file_encoding(file), _file_errors(file), final, decoded, spans)
    records = load_ptr(spans, _TEXT_SPANS_RECORDS)
    if status != 0:
        free(records)
        free(raw)
        # The bytes have been consumed by the OS even when decoding fails.
        store_i64(state, _FILE_TAIL_LENGTH, 0)
        return -1
    consumed: int = load_i64(decoded, 16)
    remaining: int = total - consumed
    index = 0
    while index < remaining:
        store_i8(state, _FILE_TAIL + index, load_i8(raw, consumed + index))
        index = index + 1
    store_i64(state, _FILE_TAIL_LENGTH, remaining)
    text = load_ptr(decoded, 0)
    count: int = load_i64(decoded, 8)
    if count > 1152921504606846973:
        free(records)
        free(text)
        free(raw)
        return _file_error(15, cstr("text file buffer is too large"))
    output = malloc(count + 2)
    offsets = malloc((count + 2) * 8)
    if ptr_is_null(output) or ptr_is_null(offsets):
        free(output)
        free(offsets)
        free(records)
        free(text)
        free(raw)
        return _file_error(19, cstr("cannot allocate text file buffer"))
    # Decoded characters copy their input, except error-handler units, whose
    # input extents come from the decoder's span list.
    single_byte: int = 1 if _file_encoding(file) != 0 else 0
    span_count: int = load_i64(spans, _TEXT_SPANS_COUNT)
    span_index: int = 0
    unit_end: int = 0
    unit_raw_start: int = 0
    unit_raw_end: int = 0
    unit_chars: int = 0
    source: int = 0
    position: int = 0
    used: int = 0
    pending_cr: int = load_i64(state, _FILE_PENDING_CR)
    cr_offset: int = load_i64(state, _FILE_CR_OFFSET)
    while position < count:
        width: int = _file_text_width(text, position)
        first: int = load_i8(text, position) & 255
        while span_index < span_count and load_i64(records, span_index * _TEXT_SPAN_BYTES + _TEXT_SPAN_OUT_START) == position:
            span = ptr_add(records, span_index * _TEXT_SPAN_BYTES)
            span_index = span_index + 1
            if load_i64(span, _TEXT_SPAN_OUT_LENGTH) == 0:
                # Ignored input is consumed before the next character.
                source = load_i64(span, _TEXT_SPAN_RAW_END)
            else:
                unit_end = position + load_i64(span, _TEXT_SPAN_OUT_LENGTH)
                unit_raw_start = source
                unit_raw_end = load_i64(span, _TEXT_SPAN_RAW_END)
                unit_chars = 0
        end: int = 0
        if position < unit_end:
            unit_chars = unit_chars + 1
            if position + width < unit_end:
                end = (origin + unit_raw_start) | (unit_chars << _FILE_COOKIE_SKIP_SHIFT)
            else:
                source = unit_raw_end
                end = origin + source
        else:
            source = source + (1 if single_byte != 0 else width)
            end = origin + source
        if pending_cr != 0:
            store_i8(output, used, 10)
            store_i64(offsets, used * 8, end if first == 10 else cr_offset)
            used = used + 1
            pending_cr = 0
            if first == 10:
                position = position + 1
                continue
        if first == 13:
            pending_cr = 1
            cr_offset = end
        else:
            index = 0
            while index < width:
                store_i8(output, used, load_i8(text, position + index))
                store_i64(offsets, used * 8, end)
                used = used + 1
                index = index + 1
        position = position + width
    if final != 0 and pending_cr != 0:
        store_i8(output, used, 10)
        store_i64(offsets, used * 8, cr_offset)
        used = used + 1
        pending_cr = 0
    store_i64(state, _FILE_PENDING_CR, pending_cr)
    store_i64(state, _FILE_CR_OFFSET, cr_offset)
    store_i64(state, _FILE_EOF, final)
    store_ptr(state, _FILE_DECODED, output)
    store_ptr(state, _FILE_MAP, offsets)
    store_i64(state, _FILE_LENGTH, used)
    free(records)
    free(text)
    free(raw)
    return used


def _file_text_fill_all(file, state) -> int:
    free(load_ptr(state, _FILE_DECODED))
    free(load_ptr(state, _FILE_MAP))
    store_ptr(state, _FILE_DECODED, null())
    store_ptr(state, _FILE_MAP, null())
    store_i64(state, _FILE_LENGTH, 0)
    store_i64(state, _FILE_POSITION, 0)
    capacity: int = 8192
    raw = malloc(capacity)
    if ptr_is_null(raw):
        return _file_error(19, cstr("cannot allocate file input buffer"))
    total: int = load_i64(state, _FILE_TAIL_LENGTH)
    index: int = 0
    while index < total:
        store_i8(raw, index, load_i8(state, _FILE_TAIL + index))
        index = index + 1
    origin: int = load_i64(state, _FILE_RAW_OFFSET) - total
    while True:
        if capacity - total < 4096:
            if capacity > 4611686018427387903:
                free(raw)
                return _file_error(15, cstr("file read result is too large"))
            capacity = capacity * 2
            grown = realloc(raw, capacity)
            if ptr_is_null(grown):
                free(raw)
                return _file_error(19, cstr("cannot grow file input buffer"))
            raw = grown
        received: int = fread(ptr_add(raw, total), 1, capacity - total, load_ptr(file, 16))
        total = total + received
        if received == 0:
            if ferror(load_ptr(file, 16)) != 0:
                free(raw)
                return _file_error(14, cstr("file read failed"))
            break
    store_i64(state, _FILE_RAW_OFFSET, origin + total)
    return _file_decode_chunk(file, state, raw, total, origin, 1)


def _file_read_body(file, limit: int, line: int):
    checked = _checked_open_file(file)
    if ptr_is_null(checked):
        return null()
    if (load_i64(file, 32) & 256) == 0:
        _file_error(14, cstr("file is not readable"))
        return null()
    if _file_binary(file) != 0:
        if line != 0:
            return _file_binary_readline(file, limit)
        if limit < 0:
            return _file_binary_read_all(file)
        return _file_binary_read(file, limit)
    state = _file_decoder(file)
    if ptr_is_null(state):
        return null()
    result = null()
    capacity: int = 0
    used: int = 0
    characters: int = 0
    stop: int = 0
    while stop == 0 and (limit < 0 or characters < limit):
        position: int = load_i64(state, _FILE_POSITION)
        if position >= load_i64(state, _FILE_LENGTH):
            if load_i64(state, _FILE_EOF) != 0:
                break
            count: int = 0
            if limit < 0 and line == 0:
                count = _file_text_fill_all(file, state)
            else:
                count = _file_text_fill(file, state)
            if count < 0:
                free(result)
                return null()
            if count == 0:
                continue
            position = 0
        data = load_ptr(state, _FILE_DECODED)
        width: int = _file_text_width(data, position)
        if used + width > capacity:
            if capacity > 4611686018427387903:
                free(result)
                _file_error(15, cstr("file read result is too large"))
                return null()
            next_capacity: int = 128 if capacity == 0 else capacity * 2
            if next_capacity < used + width:
                next_capacity = used + width
            grown = realloc(result, next_capacity)
            if ptr_is_null(grown):
                free(result)
                _file_error(19, cstr("cannot allocate file read result"))
                return null()
            result = grown
            capacity = next_capacity
        memcpy(ptr_add(result, used), ptr_add(data, position), width)
        used = used + width
        characters = characters + 1
        if line != 0 and load_i8(data, position) == 10:
            stop = 1
        position = position + width
        store_i64(state, _FILE_POSITION, position)
        store_i64(state, _FILE_LOGICAL_OFFSET, load_i64(load_ptr(state, _FILE_MAP), (position - 1) * 8))
    output = py_str_new(result, used)
    free(result)
    return output


def _file_write_body(slots, tokens) -> int:
    file = load_ptr(slots, _FILE_FIRST * _FILE_BYTES)
    text = load_ptr(slots, _FILE_SECOND * _FILE_BYTES)
    if ptr_is_null(_checked_open_file(file)):
        return -1
    if (load_i64(file, 32) & 512) == 0:
        return _file_error(14, cstr("file is not writable"))
    characters: int = 0
    if _file_binary(file) == 0:
        if ptr_is_null(text) or is_tagged_int(text) or _type_of(text) != PY_TYPE_STR:
            return _file_error(3, cstr("write() argument must be str"))
        characters = py_str_len(text)
        store_ptr(slots, _FILE_TEMP * _FILE_BYTES, py_text_encode_ids(text, _file_encoding(file), _file_errors(file)))
    else:
        if ptr_is_null(_file_bytes_like_base(text)):
            return _file_error(3, cstr("a bytes-like object is required for binary write"))
        store_ptr(slots, _FILE_TEMP * _FILE_BYTES, py_bytes_from_obj(text))
    if _file_adopt(slots, tokens, _FILE_TEMP) != 0:
        return -1
    encoded = load_ptr(slots, _FILE_TEMP * _FILE_BYTES)
    count: int = load_i64(encoded, PYBYTESOBJECT_BYTE_LEN_OFFSET)
    position: int = 0
    while position < count:
        wrote: int = fwrite(ptr_add(encoded, PYBYTESOBJECT_DATA_OFFSET + position), 1, count - position, load_ptr(file, 16))
        if wrote <= 0:
            return _file_error(14, cstr("file write failed"))
        position = position + wrote
    _file_clear_decoder(file)
    if _file_binary(file) != 0:
        characters = position
    store_ptr(slots, _FILE_RESULT * _FILE_BYTES, py_int_from_i64(characters))
    return _file_adopt(slots, tokens, _FILE_RESULT)


def _file_readlines_body(slots, tokens) -> int:
    store_ptr(slots, _FILE_RESULT * _FILE_BYTES, py_list_new(0))
    if _file_adopt(slots, tokens, _FILE_RESULT) != 0:
        return -1
    while True:
        store_ptr(slots, _FILE_TEMP * _FILE_BYTES, py_file_readline(load_ptr(slots, _FILE_FIRST * _FILE_BYTES), -1))
        if _file_adopt(slots, tokens, _FILE_TEMP) != 0:
            return -1
        line = load_ptr(slots, _FILE_TEMP * _FILE_BYTES)
        length: int = py_str_byte_len(line) if _type_of(line) == PY_TYPE_STR else load_i64(line, PYBYTESOBJECT_BYTE_LEN_OFFSET)
        if length == 0:
            return 0
        py_list_append(load_ptr(slots, _FILE_RESULT * _FILE_BYTES), line)
        if py_err_occurred() != 0:
            return -1
        _file_drop(slots, tokens, _FILE_TEMP)


def _file_restore_pending(slots, tokens, index: int) -> None:
    slot = ptr_add(slots, index * _FILE_BYTES)
    token: int = load_i64(tokens, index * _FILE_BYTES)
    if token >= 0:
        if pcc_gc_foreign_lease_release(slot, token) < 0:
            pcc_platform_abort()
            return
    store_i64(tokens, index * _FILE_BYTES, -1)
    py_tls_exc_swap_slot(slot)


def _file_writelines_body(slots, tokens) -> int:
    store_ptr(slots, _FILE_TEMP * _FILE_BYTES, py_obj_iter(load_ptr(slots, _FILE_SECOND * _FILE_BYTES)))
    if _file_adopt(slots, tokens, _FILE_TEMP) != 0:
        return -1
    # The iterator now owns everything it needs. Reuse the independently
    # owned input slot for each item, and the result slot for each write.
    _file_drop(slots, tokens, _FILE_SECOND)
    while True:
        store_ptr(slots, _FILE_SECOND * _FILE_BYTES, py_obj_next(load_ptr(slots, _FILE_TEMP * _FILE_BYTES)))
        if ptr_is_null(load_ptr(slots, _FILE_SECOND * _FILE_BYTES)):
            if py_err_occurred() != 0:
                # Publish and lease the pending exception before class-cache
                # construction can allocate or move it.
                error_slot = ptr_add(slots, _FILE_ERRORS * _FILE_BYTES)
                py_tls_exc_swap_slot(error_slot)
                if _file_adopt(slots, tokens, _FILE_ERRORS) != 0:
                    _file_restore_pending(slots, tokens, _FILE_ERRORS)
                    return -1
                cls = py_exc_builtin_class(8)
                py_incref(cls)
                store_ptr(slots, _FILE_ENCODING * _FILE_BYTES, cls)
                if _file_adopt(slots, tokens, _FILE_ENCODING) != 0:
                    _file_restore_pending(slots, tokens, _FILE_ERRORS)
                    return -1
                exhausted: int = py_exc_matches(load_ptr(error_slot, 0), load_ptr(slots, _FILE_ENCODING * _FILE_BYTES))
                if exhausted == 0:
                    _file_restore_pending(slots, tokens, _FILE_ERRORS)
                    return -1
                _file_drop(slots, tokens, _FILE_ERRORS)
                py_clear_exception()
            none = global_load_ptr("py_None")
            py_incref(none)
            store_ptr(slots, _FILE_RESULT * _FILE_BYTES, none)
            return _file_adopt(slots, tokens, _FILE_RESULT)
        if _file_adopt(slots, tokens, _FILE_SECOND) != 0:
            return -1
        store_ptr(slots, _FILE_RESULT * _FILE_BYTES, py_file_write(load_ptr(slots, _FILE_FIRST * _FILE_BYTES), load_ptr(slots, _FILE_SECOND * _FILE_BYTES)))
        if _file_adopt(slots, tokens, _FILE_RESULT) != 0:
            return -1
        _file_drop(slots, tokens, _FILE_RESULT)
        _file_drop(slots, tokens, _FILE_SECOND)


def _file_guarded(first, second, encoding, errors, newline, operation: int, limit: int):
    borrowed = stack_alloc(5 * _FILE_BYTES)
    store_ptr(borrowed, 0, first)
    store_ptr(borrowed, 8, second)
    store_ptr(borrowed, 16, encoding)
    store_ptr(borrowed, 24, errors)
    store_ptr(borrowed, 32, newline)
    pcc_gc_frame_enter(global_addr("pcc_file_borrowed_map"), borrowed)
    slots = stack_alloc(_FILE_COUNT * _FILE_BYTES)
    tokens = stack_alloc(_FILE_COUNT * _FILE_BYTES)
    memset(slots, 0, _FILE_COUNT * _FILE_BYTES)
    index: int = 0
    while index < _FILE_COUNT:
        store_i64(tokens, index * _FILE_BYTES, -1)
        index = index + 1
    pcc_gc_frame_enter(global_addr("pcc_file_owned_map"), slots)
    index = 0
    status: int = 0
    while index < 5 and status == 0:
        token: int = pcc_gc_root_copy_borrowed_lease(ptr_add(slots, index * _FILE_BYTES), ptr_add(borrowed, index * _FILE_BYTES))
        store_i64(tokens, index * _FILE_BYTES, token)
        if token < 0:
            status = _file_error(19, cstr("file operation could not retain an input"))
        index = index + 1
    if status == 0:
        if operation == 0:
            status = _file_open_body(slots, tokens)
        elif operation == _FILE_FDOPEN_OWNED or operation == _FILE_FDOPEN_BORROWED:
            status = _file_open_body(slots, tokens, operation, limit)
        elif operation == _FILE_DYNAMIC_METHOD:
            status = _file_method_body(slots, tokens)
        elif operation == _FILE_BOUND_METHOD:
            status = _file_bound_method_body(slots, tokens, limit)
        elif operation == _FILE_FSPATH:
            status = _file_fspath_body(slots, tokens)
        elif operation == 3:
            status = _file_write_body(slots, tokens)
        elif operation == 4:
            status = _file_readlines_body(slots, tokens)
        elif operation == 5:
            status = _file_writelines_body(slots, tokens)
        else:
            store_ptr(slots, _FILE_RESULT * _FILE_BYTES, _file_read_body(load_ptr(slots, _FILE_FIRST * _FILE_BYTES), limit, 1 if operation == 2 else 0))
            status = _file_adopt(slots, tokens, _FILE_RESULT)
    result = ptr_add(slots, _FILE_RESULT * _FILE_BYTES)
    pending = ptr_add(slots, _FILE_ERROR * _FILE_BYTES)
    py_tls_exc_swap_slot(pending)
    memset(borrowed, 0, 5 * _FILE_BYTES)
    if status != 0:
        _file_drop(slots, tokens, _FILE_RESULT)
    index = _FILE_TEMP
    while index >= 0:
        _file_drop(slots, tokens, index)
        index = index - 1
    py_clear_exception()
    py_tls_exc_swap_slot(pending)
    pcc_py_gc_minor_graph_lock()
    value = pcc_gc_load_ptr(null(), result)
    prior: int = 0
    if not ptr_is_null(value) and not is_tagged_int(value):
        prior = load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & PY_FLAG_GC_PINNED
        pcc_gc_pin(value)
    pcc_py_gc_minor_graph_unlock()
    token = load_i64(tokens, _FILE_RESULT * _FILE_BYTES)
    if token >= 0:
        if pcc_gc_foreign_lease_release(result, token) < 0:
            pcc_platform_abort()
            return null()
    pcc_gc_frame_leave(slots)
    pcc_gc_frame_leave(borrowed)
    return pcc_gc_take_pinned_slot(result, prior)


@c_abi_export("py_file_open_options")
def py_file_open_options(path, mode, encoding, errors, newline):
    return _file_guarded(path, mode, encoding, errors, newline, 0, 0)


@c_abi_export("py_file_fdopen_options")
def py_file_fdopen_options(descriptor, mode, encoding, errors, newline, closefd: int, buffering: int):
    operation: int = _FILE_FDOPEN_OWNED if closefd else _FILE_FDOPEN_BORROWED
    return _file_guarded(descriptor, mode, encoding, errors, newline, operation, buffering)


@c_abi_export("py_file_fspath")
def py_file_fspath(path):
    return _file_guarded(path, null(), null(), null(), null(), _FILE_FSPATH, 0)


@c_abi_export("py_file_open")
def py_file_open(path, mode):
    return py_file_open_options(path, mode, null(), null(), null())


@c_abi_export("py_file_read_all")
def py_file_read_all(file):
    return _file_guarded(file, null(), null(), null(), null(), 1, -1)


@c_abi_export("py_file_read")
def py_file_read(file, limit: int):
    return _file_guarded(file, null(), null(), null(), null(), 1, limit)


@c_abi_export("py_file_readline")
def py_file_readline(file, limit: int):
    return _file_guarded(file, null(), null(), null(), null(), 2, limit)


@c_abi_export("py_file_write")
def py_file_write(file, text):
    return _file_guarded(file, text, null(), null(), null(), 3, 0)


def _file_binary_read_all(file):
    f = _checked_open_file(file)
    if ptr_is_null(f):
        return null()
    fp = load_ptr(f, 16)

    buf = null()
    length: int = 0
    cap: int = 0
    tmp = malloc(4096)
    if ptr_is_null(tmp):
        return null()
    while True:
        n: int = fread(tmp, 1, 4096, fp)
        if n > 0:
            if length + n + 1 > cap:
                new_cap: int = cap
                if new_cap == 0:
                    new_cap = 4096
                while new_cap < length + n + 1:
                    new_cap = new_cap * 2
                grown = realloc(buf, new_cap)
                if ptr_is_null(grown):
                    free(tmp)
                    free(buf)
                    return null()
                buf = grown
                cap = new_cap
            memcpy(ptr_add(buf, length), tmp, n)
            length = length + n
        if n < 4096:
            if ferror(fp) != 0:
                free(tmp)
                free(buf)
                return null()
            break
    data = buf
    if ptr_is_null(data):
        data = cstr("")
    out = _file_bytes_or_str(f, data, length)
    free(tmp)
    free(buf)
    return out


def _file_binary_read(file, limit: int):
    if limit < 0:
        return _file_binary_read_all(file)
    f = _checked_open_file(file)
    if ptr_is_null(f):
        return null()
    fp = load_ptr(f, 16)
    buf = null()
    n: int = 0
    if limit > 0:
        buf = malloc(limit)
        if ptr_is_null(buf):
            return null()
        n = fread(buf, 1, limit, fp)
        if n < limit and ferror(fp) != 0:
            free(buf)
            return null()
    data = buf
    if ptr_is_null(data):
        data = cstr("")
    out = _file_bytes_or_str(f, data, n)
    free(buf)
    return out


def _checked_open_file(file):
    """Shared open-file precondition for reads, seek, tell and flush.

    NULL / non-file receivers return null silently; a closed file raises
    ValueError exactly like
    CPython ("I/O operation on closed file.").
    """
    if ptr_is_null(file):
        return null()
    if _type_of(file) != PY_TYPE_FILE:
        return null()
    if load_i32(file, 24) != 0 or ptr_is_null(load_ptr(file, 16)):
        # 2 == PY_EXC_VALUEERROR
        py_raise_owned(py_exc_new(2, cstr("I/O operation on closed file.")))
        return null()
    return file


def _file_binary_readline(file, limit: int):
    f = _checked_open_file(file)
    if ptr_is_null(f):
        return null()
    fp = load_ptr(f, 16)

    buf = null()
    length: int = 0
    cap: int = 0
    while True:
        if limit >= 0 and length >= limit:
            break
        ch: int = fgetc(fp)
        if ch < 0:
            if ferror(fp) != 0:
                free(buf)
                return null()
            break
        if length + 2 > cap:
            new_cap: int = 128
            if cap != 0:
                new_cap = cap * 2
            grown = realloc(buf, new_cap)
            if ptr_is_null(grown):
                free(buf)
                return null()
            buf = grown
            cap = new_cap
        store_i8(buf, length, ch)
        length = length + 1
        if ch == 10:
            break
    data = buf
    if ptr_is_null(data):
        data = cstr("")
    out = _file_bytes_or_str(f, data, length)
    free(buf)
    return out


@c_abi_export("py_file_seek")
def py_file_seek(file, offset: int, whence: int):
    f = _checked_open_file(file)
    if ptr_is_null(f):
        return null()
    fp = load_ptr(f, 16)
    if _file_binary(f) == 0 and whence != 0 and offset != 0:
        _file_error(14, cstr("can't do nonzero cur-relative seeks"))
        return null()
    if _file_binary(f) == 0 and whence == 1 and offset == 0:
        return py_file_tell(f)
    if _file_binary(f) == 0 and whence == 0 and offset > _FILE_COOKIE_OFFSET_MASK:
        return _file_seek_cookie(f, offset)
    # SEEK_SET/SEEK_CUR/SEEK_END are 0/1/2 on both LP64 targets; unknown
    # whence values fall back to SEEK_SET like the C mirror.
    w: int = 0
    if whence == 1:
        w = 1
    if whence == 2:
        w = 2
    rc: int = fseek(fp, offset, w)
    pos: int = -1
    if rc == 0:
        pos = ftell(fp)
    if rc != 0 or pos < 0:
        # 14 == PY_EXC_OSERROR
        py_raise_owned(py_exc_new(14, cstr("Invalid argument")))
        return null()
    _file_clear_decoder(f)
    return py_int_from_i64(pos)


def _file_seek_cookie(f, cookie: int):
    # Re-decode the handler unit at the cookie's input offset, then skip the
    # characters a previous read already returned from it.
    if fseek(load_ptr(f, 16), cookie & _FILE_COOKIE_OFFSET_MASK, 0) != 0:
        py_raise_owned(py_exc_new(14, cstr("Invalid argument")))
        return null()
    _file_clear_decoder(f)
    state = _file_decoder(f)
    if ptr_is_null(state):
        return null()
    # An incomplete sequence decodes only once more input or EOF arrives.
    while load_i64(state, _FILE_LENGTH) == 0 and load_i64(state, _FILE_EOF) == 0:
        if _file_text_fill(f, state) < 0:
            return null()
    data = load_ptr(state, _FILE_DECODED)
    length: int = load_i64(state, _FILE_LENGTH)
    skip: int = cookie >> _FILE_COOKIE_SKIP_SHIFT
    position: int = 0
    while skip > 0 and position < length:
        position = position + _file_text_width(data, position)
        skip = skip - 1
    store_i64(state, _FILE_POSITION, position)
    store_i64(state, _FILE_LOGICAL_OFFSET, cookie)
    return py_int_from_i64(cookie)


@c_abi_export("py_file_tell")
def py_file_tell(file):
    f = _checked_open_file(file)
    if ptr_is_null(f):
        return null()
    pos: int = ftell(load_ptr(f, 16))
    if pos < 0:
        # 14 == PY_EXC_OSERROR
        py_raise_owned(py_exc_new(14, cstr("Invalid argument")))
        return null()
    state = load_ptr(f, 40)
    if _file_binary(f) == 0 and ptr_is_null(state) == 0:
        pos = load_i64(state, _FILE_LOGICAL_OFFSET)
    return py_int_from_i64(pos)


@c_abi_export("py_file_flush")
def py_file_flush(file):
    f = _checked_open_file(file)
    if ptr_is_null(f):
        return null()
    if fflush(load_ptr(f, 16)) != 0:
        _file_raise_errno(pcc_errno_get(), null())
        return null()
    none = global_load_ptr("py_None")
    py_incref(none)
    return none


@c_abi_export("py_file_fileno")
def py_file_fileno(file):
    f = _checked_open_file(file)
    if ptr_is_null(f):
        return null()
    fd: int = fileno(load_ptr(f, 16))
    if fd < 0:
        py_raise_owned(py_exc_new(14, cstr("could not get file descriptor")))
        return null()
    return py_int_from_i64(fd)


def _file_close(file, report: int) -> None:
    if ptr_is_null(file):
        return
    if _type_of(file) != PY_TYPE_FILE:
        return
    _file_clear_decoder(file)
    if load_i32(file, 24) == 0:
        fp = load_ptr(file, 16)
        if not ptr_is_null(fp):
            if _file_std_fd_plus1(file) != 0:
                # sys.stdin/stdout/stderr: closefd=False, like CPython.
                fflush(fp)
            else:
                status: int = fclose(fp)
                store_ptr(file, 16, null())
                store_i32(file, 24, 1)
                if report and status != 0:
                    _file_error(14, cstr("file close failed"))
            store_i32(file, 24, 1)


@c_abi_export("py_file_close")
def py_file_close(file) -> None:
    _file_close(file, 0)


@c_abi_export("py_file_close_checked")
def py_file_close_checked(file) -> None:
    _file_close(file, 1)


@c_abi_export("py_file_readlines")
def py_file_readlines(file):
    return _file_guarded(file, null(), null(), null(), null(), 4, 0)


@c_abi_export("py_file_writelines")
def py_file_writelines(file, lines):
    return _file_guarded(file, lines, null(), null(), null(), 5, 0)


def _std_stream_new(fd: int):
    """A file object over the runtime's own standard stream ``fd``.

    The freestanding stdio's standard streams carry no buffer, so writes go
    straight to the descriptor and interleave with print() and the direct
    ``sys.stdout.write`` path, and reads consume no more than
    ``sys.stdin.readline()`` does.  Cached in a raw global pointer, so it gets
    stable storage like the ``object`` root.  Offset 32 marks it as standard
    stream ``fd``: it never closes its descriptor (CPython's closefd=False).
    """
    fp = null()
    if fd == 0:
        fp = global_addr("pcc_stdio_stdin_storage")
    elif fd == 1:
        fp = global_addr("pcc_stdio_stdout_storage")
    else:
        fp = global_addr("pcc_stdio_stderr_storage")
    out = malloc(_FILE_SIZE)
    if ptr_is_null(out):
        return null()
    memset(out, 0, _FILE_SIZE)
    store_i64(out, PYOBJECTHEADER_REFCOUNT_OFFSET, 1)
    store_i32(out, PYOBJECTHEADER_TYPE_TAG_OFFSET, PY_TYPE_FILE)
    store_i32(out, PYOBJECTHEADER_FLAGS_OFFSET, PY_FLAG_IMMORTAL | PY_FLAG_GC_MALLOC_ALLOC)
    if pcc_gc_pointer_register(out) < 0:
        free(out)
        return null()
    store_ptr(out, 16, fp)
    store_i32(out, 24, 0)
    store_i32(out, 28, 0)
    if fd == 0:
        store_i64(out, 32, (fd + 1) | 256)
    else:
        store_i64(out, 32, (fd + 1) | 512)
    return out


@c_abi_export("py_sys_stream_object")
def py_sys_stream_object(fd: int):
    """``sys.stdin`` / ``sys.stdout`` / ``sys.stderr`` (fd 0/1/2) as values."""
    cached = null()
    if fd == 0:
        cached = global_load_ptr("py_sys_stdin_object")
    elif fd == 1:
        cached = global_load_ptr("py_sys_stdout_object")
    else:
        cached = global_load_ptr("py_sys_stderr_object")
    if ptr_is_null(cached):
        if fd == 0:
            cached = _std_stream_new(0)
            global_store_ptr("py_sys_stdin_object", cached)
        elif fd == 1:
            cached = _std_stream_new(1)
            global_store_ptr("py_sys_stdout_object", cached)
        else:
            cached = _std_stream_new(2)
            global_store_ptr("py_sys_stderr_object", cached)
        if ptr_is_null(cached):
            return null()
    py_incref(cached)
    return cached


# Dynamic method codes for file objects (``destination.write(text)`` on a
# file that no static type describes).
def _file_method_code(name) -> int:
    if strcmp(name, cstr("write")) == 0:
        return 1
    if strcmp(name, cstr("read")) == 0:
        return 2
    if strcmp(name, cstr("readline")) == 0:
        return 3
    if strcmp(name, cstr("readlines")) == 0:
        return 4
    if strcmp(name, cstr("writelines")) == 0:
        return 5
    if strcmp(name, cstr("flush")) == 0:
        return 6
    if strcmp(name, cstr("close")) == 0:
        return 7
    if strcmp(name, cstr("fileno")) == 0:
        return 8
    if strcmp(name, cstr("seek")) == 0:
        return 9
    if strcmp(name, cstr("tell")) == 0:
        return 10
    if strcmp(name, cstr("isatty")) == 0:
        return 11
    if strcmp(name, cstr("__enter__")) == 0:
        return 12
    if strcmp(name, cstr("__exit__")) == 0:
        return 13
    if strcmp(name, cstr("readable")) == 0:
        return 14
    if strcmp(name, cstr("writable")) == 0:
        return 15
    return 0


def _method_arg_i64(slots, tokens, index: int, default: int, allow_none: int) -> int:
    """Convert one rooted method operand without retaining a raw callback result."""
    args = load_ptr(slots, _FILE_METHOD_ARGS * _FILE_BYTES)
    if ptr_is_null(args) != 0 or index >= py_tuple_len(args):
        return default
    _file_drop(slots, tokens, _FILE_METHOD_OPERAND)
    store_ptr(slots, _FILE_METHOD_OPERAND * _FILE_BYTES, py_tuple_get(args, index))
    if _file_adopt(slots, tokens, _FILE_METHOD_OPERAND) != 0:
        return 0
    if allow_none != 0 and ptr_eq(load_ptr(slots, _FILE_METHOD_OPERAND * _FILE_BYTES), global_load_ptr("py_None")) != 0:
        return default
    return py_index_i64_checked_slots(ptr_add(slots, _FILE_METHOD_OPERAND * _FILE_BYTES))


def _file_method_body(slots, tokens) -> int:
    captures = load_ptr(slots, _FILE_METHOD_CAPTURES * _FILE_BYTES)
    args = load_ptr(slots, _FILE_METHOD_ARGS * _FILE_BYTES)
    store_ptr(slots, _FILE_METHOD_RECEIVER * _FILE_BYTES, py_tuple_get(captures, 0))
    if _file_adopt(slots, tokens, _FILE_METHOD_RECEIVER) != 0:
        return -1
    store_ptr(slots, _FILE_METHOD_CODE * _FILE_BYTES, py_tuple_get(captures, 1))
    if _file_adopt(slots, tokens, _FILE_METHOD_CODE) != 0:
        return -1
    f = load_ptr(slots, _FILE_METHOD_RECEIVER * _FILE_BYTES)
    code: int = py_int_value_i64(load_ptr(slots, _FILE_METHOD_CODE * _FILE_BYTES))
    nargs: int = 0
    if ptr_is_null(args) == 0:
        nargs = py_tuple_len(args)
    if ((code >= 6 and code <= 8 or code >= 10 and code <= 12 or code >= 14) and nargs != 0
            or code == 13 and nargs != 3 or (code == 2 or code == 3) and nargs > 1
            or code == 9 and (nargs < 1 or nargs > 2)):
        return _file_error(3, cstr("invalid file method argument count"))
    none = global_load_ptr("py_None")
    result = null()
    if code == 1 or code == 5:
        if nargs != 1:
            py_raise_owned(py_exc_new(3, cstr("expected exactly one argument")))
        else:
            store_ptr(slots, _FILE_METHOD_OPERAND * _FILE_BYTES, py_tuple_get(args, 0))
            if _file_adopt(slots, tokens, _FILE_METHOD_OPERAND) != 0:
                return -1
            arg = load_ptr(slots, _FILE_METHOD_OPERAND * _FILE_BYTES)
            if code == 1:
                result = py_file_write(f, arg)
            else:
                result = py_file_writelines(f, arg)
    elif code == 2:
        limit: int = _method_arg_i64(slots, tokens, 0, -1, 1)
        if not py_err_occurred():
            result = py_file_read(load_ptr(slots, _FILE_METHOD_RECEIVER * _FILE_BYTES), limit)
    elif code == 3:
        limit: int = _method_arg_i64(slots, tokens, 0, -1, 1)
        if not py_err_occurred():
            result = py_file_readline(load_ptr(slots, _FILE_METHOD_RECEIVER * _FILE_BYTES), limit)
    elif code == 4:
        result = py_file_readlines(f)
    elif code == 6:
        result = py_file_flush(f)
    elif code == 7:
        py_file_close_checked(f)
        if not py_err_occurred():
            py_incref(none)
            result = none
    elif code == 8:
        result = py_file_fileno(f)
    elif code == 9:
        offset: int = _method_arg_i64(slots, tokens, 0, 0, 0)
        whence: int = 0
        if not py_err_occurred():
            whence = _method_arg_i64(slots, tokens, 1, 0, 0)
        if not py_err_occurred() and (whence < -2147483648 or whence > 2147483647):
            return _file_error(15, cstr("Python int too large to convert to C int"))
        if not py_err_occurred():
            result = py_file_seek(load_ptr(slots, _FILE_METHOD_RECEIVER * _FILE_BYTES), offset, whence)
    elif code == 10:
        result = py_file_tell(f)
    elif code == 11:
        checked = _checked_open_file(f)
        if ptr_is_null(checked) == 0:
            result = py_bool_from_bit(isatty(fileno(load_ptr(f, 16))))
    elif code == 12:
        checked = _checked_open_file(f)
        if ptr_is_null(checked) == 0:
            py_incref(f)
            result = f
    elif code == 13:
        py_file_close_checked(f)
        if not py_err_occurred():
            result = global_load_ptr("py_False")
    elif code == 14 or code == 15:
        checked = _checked_open_file(f)
        if ptr_is_null(checked) == 0:
            wanted: int = 256
            if code == 15:
                wanted = 512
            result = py_bool_from_bit((load_i64(f, 32) & wanted) != 0)
    store_ptr(slots, _FILE_RESULT * _FILE_BYTES, result)
    if py_err_occurred():
        return -1
    return _file_adopt(slots, tokens, _FILE_RESULT)


def _file_method_entry(captures, args):
    return _file_guarded(captures, args, null(), null(), null(), _FILE_DYNAMIC_METHOD, 0)


def _file_method_name(code: int):
    if code == 1:
        return cstr("write")
    if code == 2:
        return cstr("read")
    if code == 3:
        return cstr("readline")
    if code == 4:
        return cstr("readlines")
    if code == 5:
        return cstr("writelines")
    if code == 6:
        return cstr("flush")
    if code == 7:
        return cstr("close")
    if code == 8:
        return cstr("fileno")
    if code == 9:
        return cstr("seek")
    if code == 10:
        return cstr("tell")
    if code == 11:
        return cstr("isatty")
    if code == 12:
        return cstr("__enter__")
    if code == 13:
        return cstr("__exit__")
    if code == 14:
        return cstr("readable")
    return cstr("writable")


def _file_bound_method_body(slots, tokens, code: int) -> int:
    store_ptr(slots, _FILE_SECOND * _FILE_BYTES, py_tuple_new(2))
    if _file_adopt(slots, tokens, _FILE_SECOND) != 0:
        return -1
    store_ptr(slots, _FILE_ENCODING * _FILE_BYTES, py_int_from_i64(code))
    if _file_adopt(slots, tokens, _FILE_ENCODING) != 0:
        return -1
    captures = load_ptr(slots, _FILE_SECOND * _FILE_BYTES)
    file = load_ptr(slots, _FILE_FIRST * _FILE_BYTES)
    py_tuple_set_item(captures, 0, file)
    py_tuple_set_item(captures, 1, load_ptr(slots, _FILE_ENCODING * _FILE_BYTES))
    store_ptr(slots, _FILE_RESULT * _FILE_BYTES,
              py_func_new_bound(_file_method_entry, captures, _file_method_name(code), file))
    return _file_adopt(slots, tokens, _FILE_RESULT)


@c_abi_export("py_file_type_kind")
def py_file_type_kind(file) -> int:
    """0 TextIOWrapper, 1 BufferedReader, 2 BufferedWriter, 3 BufferedRandom."""
    if ptr_is_null(file) or _type_of(file) != PY_TYPE_FILE:
        return 0
    if _file_binary(file) == 0:
        return 0
    access: int = load_i64(file, 32) & 768
    if access == 768:
        return 3
    if access == 512:
        return 2
    return 1


@c_abi_export("py_file_getattr")
def py_file_getattr(file, name):
    """Attribute load on a file object: ``closed`` and bound methods.

    NULL without an exception when ``name`` is not a file attribute.
    """
    if ptr_is_null(file) or _type_of(file) != PY_TYPE_FILE or ptr_is_null(name):
        return null()
    if strcmp(name, cstr("closed")) == 0:
        return py_bool_from_bit(load_i32(file, 24))
    if strcmp(name, cstr("name")) == 0 and (load_i64(file, 32) & _FILE_DESCRIPTOR_NAME) != 0:
        return py_int_from_i64(load_i64(file, 32) >> _FILE_DESCRIPTOR_SHIFT)
    code: int = _file_method_code(name)
    if code == 0:
        return null()
    return _file_guarded(file, null(), null(), null(), null(), _FILE_BOUND_METHOD, code)


# Keep fileinput state indexes as integer literals at use sites below. The
# pcc-Python runtime path cannot safely rely on module-level integer constants
# during early bootstrap module initialization.


def _state_get(state, index: int):
    return py_list_get(state, index)


def _state_get_i64(state, index: int) -> int:
    item = py_list_get(state, index)
    out: int = 0
    none = global_load_ptr("py_None")
    if not ptr_is_null(item) and ptr_eq(item, none) == 0:
        out = py_int_value_i64(item)
    py_decref(item)
    return out


def _state_set_i64(state, index: int, value: int) -> None:
    obj = py_int_from_i64(value)
    py_list_set(state, index, obj)


def _files_len(files) -> int:
    if ptr_is_null(files):
        return 0
    tag: int = _type_of(files)
    if tag == PY_TYPE_STR:
        return 1
    if tag == PY_TYPE_LIST:
        return py_list_len(files)
    if tag == PY_TYPE_TUPLE:
        return py_tuple_len(files)
    return 0


def _files_get(files, index: int):
    if ptr_is_null(files):
        return null()
    tag: int = _type_of(files)
    if tag == PY_TYPE_STR:
        if index != 0:
            return null()
        py_incref(files)
        return files
    if tag == PY_TYPE_LIST:
        return py_list_get(files, index)
    if tag == PY_TYPE_TUPLE:
        return py_tuple_get(files, index)
    return null()


def _fileinput_open_text(filename):
    mode = py_str_new(cstr("r"), 1)
    file = py_file_open(filename, mode)
    py_decref(mode)
    return file


def _open_next(state) -> int:
    files = _state_get(state, 0)
    idx: int = _state_get_i64(state, 2)
    nfiles: int = _files_len(files)
    while idx < nfiles:
        filename = _files_get(files, idx)
        _state_set_i64(state, 2, idx + 1)
        idx = idx + 1
        if ptr_is_null(filename):
            continue
        py_list_set(state, 6, filename)
        file = _fileinput_open_text(filename)
        if ptr_is_null(file):
            py_decref(files)
            return 0
        text = py_file_read_all(file)
        py_file_close(file)
        py_decref(file)
        if ptr_is_null(text):
            py_decref(files)
            return 0
        lines = py_str_splitlines_keepends(text, 1)
        py_decref(text)
        if ptr_is_null(lines):
            py_decref(files)
            return 0
        py_list_set(state, 3, lines)
        _state_set_i64(state, 4, 0)
        if py_list_len(lines) > 0:
            py_decref(files)
            return 1
    py_decref(files)
    return 0


@c_abi_export("py_fileinput_new")
def py_fileinput_new(files, openhook):
    state = py_list_new(7)
    zero = py_int_from_i64(0)
    empty = py_list_new(0)
    none = global_load_ptr("py_None")
    if ptr_is_null(files):
        py_list_append(state, none)
    else:
        py_list_append(state, files)
    if ptr_is_null(openhook):
        py_list_append(state, none)
    else:
        py_list_append(state, openhook)
    py_list_append(state, zero)
    py_list_append(state, empty)
    py_list_append(state, zero)
    py_list_append(state, zero)
    py_list_append(state, none)
    return state


@c_abi_export("py_fileinput_readline")
def py_fileinput_readline(state):
    if ptr_is_null(state):
        return py_str_new(cstr(""), 0)
    while True:
        lines = _state_get(state, 3)
        line_idx: int = _state_get_i64(state, 4)
        nlines: int = py_list_len(lines)
        if line_idx < nlines:
            line = py_list_get(lines, line_idx)
            py_decref(lines)
            _state_set_i64(state, 4, line_idx + 1)
            total: int = _state_get_i64(state, 5)
            _state_set_i64(state, 5, total + 1)
            return line
        py_decref(lines)
        if _open_next(state) == 0:
            return py_str_new(cstr(""), 0)


@c_abi_export("py_fileinput_filename")
def py_fileinput_filename(state):
    filename = _state_get(state, 6)
    if ptr_is_null(filename):
        none = global_load_ptr("py_None")
        py_incref(none)
        return none
    return filename


@c_abi_export("py_fileinput_lineno")
def py_fileinput_lineno(state):
    return py_int_from_i64(_state_get_i64(state, 5))


@c_abi_export("py_fileinput_filelineno")
def py_fileinput_filelineno(state):
    return py_int_from_i64(_state_get_i64(state, 4))


@c_abi_export("py_fileinput_isfirstline")
def py_fileinput_isfirstline(state):
    first: int = 0
    if _state_get_i64(state, 4) == 1:
        first = 1
    return py_bool_from_bit(first)


@c_abi_export("py_fileinput_close")
def py_fileinput_close(state):
    none = global_load_ptr("py_None")
    py_incref(none)
    return none
