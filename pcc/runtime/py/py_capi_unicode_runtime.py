"""pcc-Python owners for the no-libpython C-API unicode surface.

Replaces the simple PyUnicode_* block of py_capi_shim.c: the thin
construction / accessor / comparison wrappers that delegate to the existing
pcc-Python str ABIs (py_str_new, py_str_utf8, py_str_byte_len,
py_str_concat, py_str_contains, py_str_slice, py_str_replace_count,
py_str_startswith/endswith, py_str_latin1_encode) or to pcc-Python-owned
C-API siblings (PyErr_*, py_int_from_i64, py_bytes_new).

Not yet moved (kept C-side, each a later bounded slice):
  PyUnicode_FromKindAndData / Decode / DecodeUTF8 / FromEncodedObject
    (UTF-8/kind decoding engines),
  PyUnicode_Compare / CompareWithASCIIString / Find / FindChar / Count
    (search helpers),
  PyUnicode_Format (py_str_mod percent formatting),
  PyUnicode_New (writable storage is not supported),
  PyUnicode_AsUCS4 / AsUCS4Copy / AsLatin1String / FromOrdinal,
  PyUnicode_ReadChar / Kind / Writer family.

Public object type tags come from the generated ``py_abi_constants`` module.
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import PY_TYPE_BYTES, PY_TYPE_STR

from pcc.extern import c_abi_typed_export, c_int32, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    cstr,
    is_tagged_int,
    load_i32,
    load_i64,
    load_i8,
    load_ptr,
    null,
    ptr_add,
    ptr_is_null,
    stack_alloc,
    store_i32,
    store_i64,
    store_i8,
    store_ptr,
    strlen,
)

py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)
pcc_capi_str_utf8_pinned = extern(
    "pcc_capi_str_utf8_pinned", (c_ptr,), c_ptr
)
py_str_byte_len = extern("py_str_byte_len", (c_ptr,), c_int64)
py_str_len = extern("py_str_len", (c_ptr,), c_int64)
py_str_concat = extern("py_str_concat", (c_ptr, c_ptr), c_ptr)
py_str_contains = extern("py_str_contains", (c_ptr, c_ptr), c_int64)
py_str_slice = extern("py_str_slice", (c_ptr, c_ptr, c_ptr, c_ptr), c_ptr)
py_str_replace_count = extern(
    "py_str_replace_count", (c_ptr, c_ptr, c_ptr, c_int64), c_ptr
)
py_str_startswith = extern("py_str_startswith", (c_ptr, c_ptr), c_int64)
py_str_endswith = extern("py_str_endswith", (c_ptr, c_ptr), c_int64)
py_str_latin1_encode = extern("py_str_latin1_encode", (c_ptr,), c_ptr)
py_bytes_new = extern("py_bytes_new", (c_ptr, c_int64), c_ptr)
py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
py_incref = extern("py_incref", (c_ptr,), c_void)
memcmp_c = extern("memcmp", (c_ptr, c_ptr, c_int64), c_int64)
py_obj_eq = extern("py_obj_eq", (c_ptr, c_ptr), c_int64)
py_obj_lt = extern("py_obj_lt", (c_ptr, c_ptr), c_int64)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
PyErr_Fetch = extern("PyErr_Fetch", (c_ptr, c_ptr, c_ptr), c_void)
PyErr_Restore = extern("PyErr_Restore", (c_ptr, c_ptr, c_ptr), c_void)


def _is_str(obj) -> int:
    if ptr_is_null(obj) or is_tagged_int(obj):
        return 0
    if load_i32(obj, 8) == PY_TYPE_STR:  # PY_TYPE_STR
        return 1
    return 0


def _type_error(message) -> None:
    py_raise_owned(py_exc_new(3, message))  # PY_EXC_TYPEERROR


def _value_error(message) -> None:
    py_raise_owned(py_exc_new(2, message))  # PY_EXC_VALUEERROR


@c_abi_typed_export("PyUnicode_FromString", "ptr", ("ptr",))
def PyUnicode_FromString(value) -> c_ptr:
    if ptr_is_null(value):
        return py_str_new(cstr(""), 0)
    return py_str_new(value, strlen(value))


@c_abi_typed_export("PyUnicode_FromStringAndSize", "ptr", ("ptr", "i64"))
def PyUnicode_FromStringAndSize(value, length: int) -> c_ptr:
    if length < 0:
        _value_error(cstr("negative unicode size"))
        return null()
    if ptr_is_null(value) and length > 0:
        _value_error(cstr("NULL unicode data with nonzero size"))
        return null()
    return py_str_new(value, length)


@c_abi_typed_export("PyUnicode_FromObject", "ptr", ("ptr",))
def PyUnicode_FromObject(obj) -> c_ptr:
    if _is_str(obj) == 0:
        _type_error(cstr("expected str"))
        return null()
    py_incref(obj)
    return obj


@c_abi_typed_export("PyUnicode_InternFromString", "ptr", ("ptr",))
def PyUnicode_InternFromString(value) -> c_ptr:
    return PyUnicode_FromString(value)


@c_abi_typed_export("PyUnicode_AsUTF8", "ptr", ("ptr",))
def PyUnicode_AsUTF8(obj) -> c_ptr:
    if _is_str(obj) == 0:
        _type_error(cstr("expected str"))
        return null()
    return pcc_capi_str_utf8_pinned(obj)


@c_abi_typed_export("PyUnicode_AsUTF8AndSize", "ptr", ("ptr", "ptr"))
def PyUnicode_AsUTF8AndSize(obj, size_ptr) -> c_ptr:
    if _is_str(obj) == 0:
        _type_error(cstr("expected str"))
        return null()
    if not ptr_is_null(size_ptr):
        store_i64(size_ptr, 0, py_str_byte_len(obj))
    return pcc_capi_str_utf8_pinned(obj)


@c_abi_typed_export("PyUnicode_AsUTF8String", "ptr", ("ptr",))
def PyUnicode_AsUTF8String(obj) -> c_ptr:
    if _is_str(obj) == 0:
        _type_error(cstr("expected str"))
        return null()
    return py_bytes_new(py_str_utf8(obj), py_str_byte_len(obj))


@c_abi_typed_export("PyUnicode_AsASCIIString", "ptr", ("ptr",))
def PyUnicode_AsASCIIString(obj) -> c_ptr:
    if _is_str(obj) == 0:
        _type_error(cstr("expected str"))
        return null()
    raw = py_str_utf8(obj)
    n = py_str_byte_len(obj)
    i: int = 0
    while i < n:
        if (load_i8(raw, i) & 0xFF) > 0x7F:
            _value_error(cstr("non-ascii character"))
            return null()
        i += 1
    return py_bytes_new(raw, n)


@c_abi_typed_export("PyUnicode_GetLength", "i64", ("ptr",))
def PyUnicode_GetLength(obj) -> int:
    if _is_str(obj) == 0:
        _type_error(cstr("expected str"))
        return -1
    return py_str_len(obj)


@c_abi_typed_export("PyUnicode_Check", "i32", ("ptr",))
def PyUnicode_Check(obj) -> int:
    return _is_str(obj)


@c_abi_typed_export("PyUnicode_CheckExact", "i32", ("ptr",))
def PyUnicode_CheckExact(obj) -> int:
    return _is_str(obj)


@c_abi_typed_export("PyUnicode_Compare", "i32", ("ptr", "ptr"))
def PyUnicode_Compare(left, right) -> int:
    if _is_str(left) == 0 or _is_str(right) == 0:
        _type_error(cstr("expected str"))
        return -1
    if py_obj_eq(left, right) != 0:
        return 0
    if py_obj_lt(left, right) != 0:
        return -1
    return 1


@c_abi_typed_export("PyUnicode_CompareWithASCIIString", "i32", ("ptr", "ptr"))
def PyUnicode_CompareWithASCIIString(left, right) -> int:
    if ptr_is_null(right):
        right = cstr("")
    right_obj = PyUnicode_FromString(right)
    if ptr_is_null(right_obj):
        return -1
    result: int = PyUnicode_Compare(left, right_obj)
    py_decref(right_obj)
    return result


@c_abi_typed_export("PyUnicode_Concat", "ptr", ("ptr", "ptr"))
def PyUnicode_Concat(left, right) -> c_ptr:
    if _is_str(left) == 0 or _is_str(right) == 0:
        _type_error(cstr("expected str"))
        return null()
    return py_str_concat(left, right)


@c_abi_typed_export("PyUnicode_Contains", "i32", ("ptr", "ptr"))
def PyUnicode_Contains(container, element) -> int:
    if _is_str(container) == 0 or _is_str(element) == 0:
        _type_error(cstr("expected str"))
        return -1
    if py_str_contains(container, element) != 0:
        return 1
    return 0


@c_abi_typed_export("PyUnicode_Substring", "ptr", ("ptr", "i64", "i64"))
def PyUnicode_Substring(text, start: int, end: int) -> c_ptr:
    if _is_str(text) == 0:
        _type_error(cstr("expected str"))
        return null()
    lo = py_int_from_i64(start)
    hi = py_int_from_i64(end)
    if ptr_is_null(lo) or ptr_is_null(hi):
        if not ptr_is_null(lo):
            py_decref(lo)
        if not ptr_is_null(hi):
            py_decref(hi)
        return null()
    out = py_str_slice(text, lo, hi, null())
    py_decref(lo)
    py_decref(hi)
    return out


@c_abi_typed_export("PyUnicode_Replace", "ptr", ("ptr", "ptr", "ptr", "i64"))
def PyUnicode_Replace(text, substr, replstr, maxcount: int) -> c_ptr:
    if (
        _is_str(text) == 0
        or _is_str(substr) == 0
        or _is_str(replstr) == 0
    ):
        _type_error(cstr("expected str"))
        return null()
    return py_str_replace_count(text, substr, replstr, maxcount)


@c_abi_typed_export("PyUnicode_Tailmatch", "i64", ("ptr", "ptr", "i64", "i64", "i32"))
def PyUnicode_Tailmatch(
    text, substr, start: int, end: int, direction: int
) -> int:
    if _is_str(text) == 0 or _is_str(substr) == 0:
        _type_error(cstr("expected str"))
        return -1
    length = py_str_len(text)
    if start < 0:
        start = start + length
    if start < 0:
        start = 0
    if end < 0 or end > length:
        end = length
    if end < start:
        end = start
    window = PyUnicode_Substring(text, start, end)
    if ptr_is_null(window):
        return -1
    if direction < 0:
        matched = py_str_endswith(window, substr)
    else:
        matched = py_str_startswith(window, substr)
    py_decref(window)
    if matched != 0:
        return 1
    return 0


@c_abi_typed_export("PyUnicode_EqualToUTF8AndSize", "i32", ("ptr", "ptr", "i64"))
def PyUnicode_EqualToUTF8AndSize(unicode, text, text_len: int) -> int:
    # Preserve any latched exception across the comparison (CPython contract).
    exc_type = stack_alloc(8)
    exc_value = stack_alloc(8)
    exc_traceback = stack_alloc(8)
    store_ptr(exc_type, 0, null())
    store_ptr(exc_value, 0, null())
    store_ptr(exc_traceback, 0, null())
    PyErr_Fetch(exc_type, exc_value, exc_traceback)

    result: int = 0
    size_slot = stack_alloc(8)
    store_i64(size_slot, 0, 0)
    actual = PyUnicode_AsUTF8AndSize(unicode, size_slot)
    if (
        not ptr_is_null(actual)
        and not ptr_is_null(text)
        and py_str_byte_len(unicode) == text_len
    ):
        if memcmp_c(actual, text, text_len) == 0:
            result = 1

    PyErr_Restore(
        load_ptr(exc_type, 0), load_ptr(exc_value, 0), load_ptr(exc_traceback, 0)
    )
    return result


@c_abi_typed_export("PyUnicode_EqualToUTF8", "i32", ("ptr", "ptr"))
def PyUnicode_EqualToUTF8(unicode, text) -> int:
    if ptr_is_null(text):
        return 0
    return PyUnicode_EqualToUTF8AndSize(unicode, text, strlen(text))


# --- PyUnicode decode / kind engine -------------------------------
# Owns the UTF-8 / kind decode surface: PyUnicode_Decode, DecodeUTF8,
# FromEncodedObject, FromKindAndData, FromOrdinal, AsUCS4, AsUCS4Copy,
# AsLatin1String, plus the internal utf8/kind helpers.

PyMem_Malloc = extern("PyMem_Malloc", (c_int64,), c_ptr)
PyMem_Free = extern("PyMem_Free", (c_ptr,), c_void)
py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
PyErr_NoMemory = extern("PyErr_NoMemory", (), c_ptr)
strcmp_c = extern("strcmp", (c_ptr, c_ptr), c_int32)


def _utf8_codepoint_len(ch: int) -> int:
    if ch <= 0x7F:
        return 1
    if ch <= 0x7FF:
        return 2
    if ch <= 0xFFFF:
        return 3
    return 4


def _utf8_write(out, ch: int) -> int:
    if ch <= 0x7F:
        store_i8(out, 0, ch)
        return 1
    if ch <= 0x7FF:
        store_i8(out, 0, 0xC0 | (ch >> 6))
        store_i8(out, 1, 0x80 | (ch & 0x3F))
        return 2
    if ch <= 0xFFFF:
        store_i8(out, 0, 0xE0 | (ch >> 12))
        store_i8(out, 1, 0x80 | ((ch >> 6) & 0x3F))
        store_i8(out, 2, 0x80 | (ch & 0x3F))
        return 3
    store_i8(out, 0, 0xF0 | (ch >> 18))
    store_i8(out, 1, 0x80 | ((ch >> 12) & 0x3F))
    store_i8(out, 2, 0x80 | ((ch >> 6) & 0x3F))
    store_i8(out, 3, 0x80 | (ch & 0x3F))
    return 4


def _utf8_next_u4(data, length: int, pos_ptr, out_ptr) -> int:
    i: int = load_i64(pos_ptr, 0)
    if i < 0 or i >= length:
        return 0
    b0: int = load_i8(data, i) & 0xFF
    if b0 < 0x80:
        store_i32(out_ptr, 0, b0)
        store_i64(pos_ptr, 0, i + 1)
        return 1
    if 0xC2 <= b0 <= 0xDF:
        if i + 1 >= length:
            return -1
        b1: int = load_i8(data, i + 1) & 0xFF
        if (b1 & 0xC0) != 0x80:
            return -1
        store_i32(out_ptr, 0, ((b0 & 0x1F) << 6) | (b1 & 0x3F))
        store_i64(pos_ptr, 0, i + 2)
        return 1
    if 0xE0 <= b0 <= 0xEF:
        if i + 2 >= length:
            return -1
        b1 = load_i8(data, i + 1) & 0xFF
        b2 = load_i8(data, i + 2) & 0xFF
        if (b1 & 0xC0) != 0x80 or (b2 & 0xC0) != 0x80:
            return -1
        ch: int = ((b0 & 0x0F) << 12) | ((b1 & 0x3F) << 6) | (b2 & 0x3F)
        if ch < 0x800:
            return -1
        store_i32(out_ptr, 0, ch)
        store_i64(pos_ptr, 0, i + 3)
        return 1
    if 0xF0 <= b0 <= 0xF4:
        if i + 3 >= length:
            return -1
        b1 = load_i8(data, i + 1) & 0xFF
        b2 = load_i8(data, i + 2) & 0xFF
        b3 = load_i8(data, i + 3) & 0xFF
        if (b1 & 0xC0) != 0x80 or (b2 & 0xC0) != 0x80 or (b3 & 0xC0) != 0x80:
            return -1
        ch = ((b0 & 0x07) << 18) | ((b1 & 0x3F) << 12) | ((b2 & 0x3F) << 6) | (b3 & 0x3F)
        if ch < 0x10000 or ch > 0x10FFFF:
            return -1
        store_i32(out_ptr, 0, ch)
        store_i64(pos_ptr, 0, i + 4)
        return 1
    return -1


def _ucs4_len(text) -> int:
    if _is_str(text) == 0:
        return -1
    raw = py_str_utf8(text)
    byte_len = py_str_byte_len(text)
    count: int = 0
    i: int = 0
    ch_slot = stack_alloc(8)
    pos_slot = stack_alloc(8)
    while i < byte_len:
        store_i64(pos_slot, 0, i)
        ok = _utf8_next_u4(raw, byte_len, pos_slot, ch_slot)
        if ok <= 0:
            _value_error(cstr("invalid UTF-8 string data"))
            return -1
        i = load_i64(pos_slot, 0)
        count += 1
    return count


def _clamp_index(index: int, length: int) -> int:
    if index < 0:
        index = index + length
        if index < 0:
            return 0
    if index > length:
        return length
    return index


@c_abi_typed_export("PyUnicode_DecodeUTF8", "ptr", ("ptr", "i64", "ptr"))
def PyUnicode_DecodeUTF8(text, size: int, errors) -> c_ptr:
    if ptr_is_null(text) or size < 0:
        _type_error(cstr("invalid UTF-8 input"))
        return null()
    if not ptr_is_null(errors) and strcmp_c(errors, cstr("strict")) != 0:
        _value_error(cstr("unsupported UTF-8 error handler"))
        return null()
    pos: int = 0
    ch_slot = stack_alloc(8)
    pos_slot = stack_alloc(8)
    while pos < size:
        store_i64(pos_slot, 0, pos)
        ok = _utf8_next_u4(text, size, pos_slot, ch_slot)
        if ok <= 0:
            _value_error(cstr("invalid UTF-8 input"))
            return null()
        pos = load_i64(pos_slot, 0)
    return py_str_new(text, size)


@c_abi_typed_export("PyUnicode_FromOrdinal", "ptr", ("i32",))
def PyUnicode_FromOrdinal(ordinal: int) -> c_ptr:
    if ordinal < 0 or ordinal > 0x10FFFF:
        _value_error(cstr("unicode ordinal out of range"))
        return null()
    buf = stack_alloc(8)
    store_i32(buf, 0, ordinal)
    return PyUnicode_FromKindAndData(4, buf, 1)  # PyUnicode_4BYTE_KIND


@c_abi_typed_export("PyUnicode_AsLatin1String", "ptr", ("ptr",))
def PyUnicode_AsLatin1String(unicode) -> c_ptr:
    return PyUnicode_AsASCIIString(unicode)


@c_abi_typed_export("PyUnicode_AsEncodedString", "ptr", ("ptr", "ptr", "ptr"))
def PyUnicode_AsEncodedString(obj, encoding, errors) -> c_ptr:
    if _is_str(obj) == 0:
        _type_error(cstr("expected str"))
        return null()
    if _enc_utf8(encoding) or _enc_utf8_alt(encoding):
        return PyUnicode_AsUTF8String(obj)
    if _enc_ascii(encoding):
        return PyUnicode_AsASCIIString(obj)
    if _enc_latin1(encoding) or _enc_latin1_alt(encoding):
        out = py_str_latin1_encode(obj)
        if ptr_is_null(out) and py_err_occurred() == 0:
            _value_error(cstr("cannot encode latin-1"))
        return out
    _value_error(cstr("unsupported encoding"))
    return null()


def _enc_utf8(enc) -> int:
    if ptr_is_null(enc):
        return 0
    if strcmp_c(enc, cstr("utf-8")) == 0:
        return 1
    return 0


def _enc_utf8_alt(enc) -> int:
    if ptr_is_null(enc):
        return 0
    if strcmp_c(enc, cstr("UTF-8")) == 0 or strcmp_c(enc, cstr("utf8")) == 0:
        return 1
    if strcmp_c(enc, cstr("UTF8")) == 0:
        return 1
    return 0


def _enc_ascii(enc) -> int:
    if ptr_is_null(enc):
        return 0
    if strcmp_c(enc, cstr("ascii")) == 0 or strcmp_c(enc, cstr("ASCII")) == 0:
        return 1
    return 0


def _enc_latin1(enc) -> int:
    if ptr_is_null(enc):
        return 0
    if strcmp_c(enc, cstr("latin-1")) == 0 or strcmp_c(enc, cstr("LATIN-1")) == 0:
        return 1
    return 0


def _enc_latin1_alt(enc) -> int:
    if ptr_is_null(enc):
        return 0
    if strcmp_c(enc, cstr("latin1")) == 0 or strcmp_c(enc, cstr("LATIN1")) == 0:
        return 1
    return 0


def _kind_supported(kind: int) -> int:
    if kind == 1 or kind == 2 or kind == 4:
        return 1
    return 0


def _read_kind(buffer, kind: int, i: int) -> int:
    if kind == 1:
        return load_i8(ptr_add(buffer, i), 0) & 0xFF
    if kind == 2:
        lo: int = load_i8(ptr_add(buffer, i * 2), 0) & 0xFF
        hi: int = load_i8(ptr_add(buffer, i * 2 + 1), 0) & 0xFF
        return lo | (hi << 8)
    return load_i32(ptr_add(buffer, i * 4), 0)


@c_abi_typed_export("PyUnicode_FromKindAndData", "ptr", ("i32", "ptr", "i64"))
def PyUnicode_FromKindAndData(kind: int, buffer, size: int) -> c_ptr:
    if size < 0:
        _value_error(cstr("negative unicode size"))
        return null()
    if size == 0:
        return py_str_new(cstr(""), 0)
    if ptr_is_null(buffer):
        _value_error(cstr("NULL unicode data with nonzero size"))
        return null()
    if _kind_supported(kind) == 0:
        _value_error(cstr("unsupported unicode kind"))
        return null()
    byte_len: int = 0
    i: int = 0
    while i < size:
        byte_len = byte_len + _utf8_codepoint_len(_read_kind(buffer, kind, i))
        i += 1
    utf8 = PyMem_Malloc(byte_len)
    if ptr_is_null(utf8):
        PyErr_NoMemory()
        return null()
    pos: int = 0
    i = 0
    while i < size:
        n = _utf8_write(ptr_add(utf8, pos), _read_kind(buffer, kind, i))
        pos = pos + n
        i += 1
    out = py_str_new(utf8, byte_len)
    PyMem_Free(utf8)
    if ptr_is_null(out):
        PyErr_NoMemory()
    return out


@c_abi_typed_export("PyUnicode_AsUCS4", "ptr", ("ptr", "ptr", "i64", "i32"))
def PyUnicode_AsUCS4(unicode, buffer, buflen: int, copy_null: int) -> c_ptr:
    if _is_str(unicode) == 0:
        _type_error(cstr("expected str"))
        return null()
    if ptr_is_null(buffer):
        _type_error(cstr("expected UCS4 buffer"))
        return null()
    if buflen < 0:
        _raise_system_error(cstr("negative UCS4 buffer length"))
        return null()
    length = _ucs4_len(unicode)
    if length < 0:
        return null()
    required = length + (1 if copy_null != 0 else 0)
    if buflen < required:
        _raise_system_error(cstr("string is longer than the UCS4 buffer"))
        return null()
    raw = py_str_utf8(unicode)
    byte_len = py_str_byte_len(unicode)
    pos: int = 0
    out_idx: int = 0
    ch_slot = stack_alloc(8)
    while pos < byte_len:
        pos_slot = stack_alloc(8)
        store_i64(pos_slot, 0, pos)
        ok = _utf8_next_u4(raw, byte_len, pos_slot, ch_slot)
        if ok <= 0:
            _value_error(cstr("invalid UTF-8 string data"))
            return null()
        pos = load_i64(pos_slot, 0)
        store_i32(ptr_add(buffer, out_idx * 4), 0, load_i32(ch_slot, 0))
        out_idx += 1
    if copy_null != 0:
        store_i32(ptr_add(buffer, out_idx * 4), 0, 0)
    return buffer


@c_abi_typed_export("PyUnicode_AsUCS4Copy", "ptr", ("ptr",))
def PyUnicode_AsUCS4Copy(unicode) -> c_ptr:
    if _is_str(unicode) == 0:
        _type_error(cstr("expected str"))
        return null()
    length = _ucs4_len(unicode)
    if length < 0:
        return null()
    buffer = PyMem_Malloc((length + 1) * 4)
    if ptr_is_null(buffer):
        PyErr_NoMemory()
        return null()
    if PyUnicode_AsUCS4(unicode, buffer, length + 1, 1) == null():
        PyMem_Free(buffer)
        return null()
    return buffer


def _raise_system_error(message) -> None:
    # The compact native exception table intentionally represents
    # PyExc_SystemError with the RuntimeError class/tag (see
    # pcc_capi_exception_tag).  Tag 6 is AttributeError.
    py_raise_owned(py_exc_new(7, message))  # PY_EXC_RUNTIMEERROR / SystemError bridge


@c_abi_typed_export("PyUnicode_Decode", "ptr", ("ptr", "i64", "ptr", "ptr"))
def PyUnicode_Decode(text, size: int, encoding, errors) -> c_ptr:
    if (
        ptr_is_null(encoding)
        or strcmp_c(encoding, cstr("utf-8")) == 0
        or strcmp_c(encoding, cstr("UTF-8")) == 0
    ):
        return PyUnicode_DecodeUTF8(text, size, errors)
    if ptr_is_null(text) or size < 0:
        _type_error(cstr("invalid encoded input"))
        return null()
    if not ptr_is_null(errors) and strcmp_c(errors, cstr("strict")) != 0:
        _value_error(cstr("unsupported decode error handler"))
        return null()
    if strcmp_c(encoding, cstr("ascii")) == 0 or strcmp_c(encoding, cstr("ASCII")) == 0:
        i: int = 0
        while i < size:
            if load_i8(text, i) & 0xFF > 0x7F:
                _value_error(cstr("invalid ASCII input"))
                return null()
            i += 1
        return py_str_new(text, size)
    if strcmp_c(encoding, cstr("latin-1")) == 0 or strcmp_c(encoding, cstr("latin1")) == 0:
        return PyUnicode_FromKindAndData(1, text, size)  # PyUnicode_1BYTE_KIND
    _value_error(cstr("unsupported encoding"))
    return null()


@c_abi_typed_export("PyUnicode_FromEncodedObject", "ptr", ("ptr", "ptr", "ptr"))
def PyUnicode_FromEncodedObject(obj, encoding, errors) -> c_ptr:
    if ptr_is_null(obj):
        _type_error(cstr("expected str or bytes"))
        return null()
    if _is_str(obj) != 0:
        py_incref(obj)
        return obj
    if is_tagged_int(obj):
        _type_error(cstr("expected str or bytes"))
        return null()
    if load_i32(obj, 8) != PY_TYPE_BYTES:  # PY_TYPE_BYTES
        _type_error(cstr("expected str or bytes"))
        return null()
    if not ptr_is_null(encoding):
        if (
            strcmp_c(encoding, cstr("utf-8")) != 0
            and strcmp_c(encoding, cstr("UTF-8")) != 0
            and strcmp_c(encoding, cstr("ascii")) != 0
            and strcmp_c(encoding, cstr("ASCII")) != 0
            and strcmp_c(encoding, cstr("latin-1")) != 0
            and strcmp_c(encoding, cstr("latin1")) != 0
        ):
            _value_error(cstr("unsupported encoding"))
            return null()
    # PyBytesObject: header(16) + byte_len@16 + data@24
    byte_len = load_i64(obj, 16)
    return py_str_new(ptr_add(obj, 24), byte_len)


# --- PyUnicode_New / KIND -------------------------------------------

@c_abi_typed_export("PyUnicode_New", "ptr", ("i64", "i64"))
def PyUnicode_New(size: int, maxchar: int) -> c_ptr:
    if size < 0:
        py_raise_owned(py_exc_new(7, cstr("negative PyUnicode_New size")))  # PY_EXC_SYSTEMERROR
        return null()
    if size == 0:
        return py_str_new(cstr(""), 0)
    py_raise_owned(
        py_exc_new(11, cstr("nonempty writable PyUnicode_New storage is not supported"))
    )  # PY_EXC_NOTIMPLEMENTEDERROR
    return null()


@c_abi_typed_export("PyUnicode_KIND", "i32", ("ptr",))
def PyUnicode_KIND(op) -> int:
    return 1
