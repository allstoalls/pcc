"""pcc-Python owners for the no-libpython PyUnicode search/read surface.

Replaces the PyUnicode_Count / PyUnicode_Find / PyUnicode_FindChar /
PyUnicode_ReadChar block of py_capi_shim.c.  The UTF-8 decode helper
(_utf8_next_u4) is duplicated here — pcc-Python runtime modules cannot import
each other's private helpers; each module compiles standalone.

Owned surface (stable C ABI names):

  PyUnicode_Count, PyUnicode_Find, PyUnicode_FindChar, PyUnicode_ReadChar

Public object type tags come from the generated ``py_abi_constants`` module.
Private exception codes remain owned by this Unicode-search contract:
  PY_EXC_TYPEERROR = 3, PY_EXC_VALUEERROR = 2,
  PY_EXC_INDEXERROR = 5
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import PY_TYPE_STR

from pcc.extern import c_abi_typed_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    cstr,
    is_tagged_int,
    load_i32,
    load_i64,
    load_i8,
    load_ptr,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    stack_alloc,
    store_i32,
    store_i64,
    strlen,
)

py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)
py_str_byte_len = extern("py_str_byte_len", (c_ptr,), c_int64)
py_str_len = extern("py_str_len", (c_ptr,), c_int64)
py_str_count = extern("py_str_count", (c_ptr, c_ptr), c_int64)
py_str_find = extern("py_str_find", (c_ptr, c_ptr), c_int64)
py_str_rfind = extern("py_str_rfind", (c_ptr, c_ptr), c_int64)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
PyUnicode_Substring = extern("PyUnicode_Substring", (c_ptr, c_int64, c_int64), c_ptr)
PyUnicode_GetLength = extern("PyUnicode_GetLength", (c_ptr,), c_int64)


def _is_str(obj) -> int:
    if ptr_is_null(obj) or is_tagged_int(obj):
        return 0
    tag: int = load_i32(obj, 8)
    if tag == PY_TYPE_STR:
        return 1
    return 0


def _type_error(message) -> None:
    py_raise_owned(py_exc_new(3, message))  # PY_EXC_TYPEERROR


def _value_error(message) -> None:
    py_raise_owned(py_exc_new(2, message))  # PY_EXC_VALUEERROR


def _index_error(message) -> None:
    py_raise_owned(py_exc_new(5, message))  # PY_EXC_INDEXERROR


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
    raw = py_str_utf8(text)
    byte_len = py_str_byte_len(text)
    pos_slot = stack_alloc(8)
    ch_slot = stack_alloc(4)
    store_i64(pos_slot, 0, 0)
    count: int = 0
    while True:
        ok = _utf8_next_u4(raw, byte_len, pos_slot, ch_slot)
        if ok == 0:
            break
        if ok < 0:
            _value_error(cstr("invalid UTF-8 string data"))
            return -1
        count += 1
    return count


def _clamp_index(index: int, length: int) -> int:
    if index < 0:
        index += length
    if index < 0:
        return 0
    if index > length:
        return length
    return index


@c_abi_typed_export("PyUnicode_Count", "i64", ("ptr", "ptr", "i64", "i64"))
def PyUnicode_Count(text, substr, start: int, end: int) -> int:
    if _is_str(text) == 0 or _is_str(substr) == 0:
        _type_error(cstr("expected str"))
        return -1
    length = _ucs4_len(text)
    if length < 0:
        return -1
    start = _clamp_index(start, length)
    end = _clamp_index(end, length)
    if end < start:
        end = start
    window = PyUnicode_Substring(text, start, end)
    if ptr_is_null(window):
        return -1
    if py_str_byte_len(substr) == 0:
        count = PyUnicode_GetLength(window) + 1
    else:
        count = py_str_count(window, substr)
    py_decref(window)
    return count


@c_abi_typed_export("PyUnicode_Find", "i64", ("ptr", "ptr", "i64", "i64", "i32"))
def PyUnicode_Find(text, substr, start: int, end: int, direction: int) -> int:
    if _is_str(text) == 0 or _is_str(substr) == 0:
        _type_error(cstr("expected str"))
        return -2
    length = _ucs4_len(text)
    if length < 0:
        return -2
    start = _clamp_index(start, length)
    end = _clamp_index(end, length)
    if end < start:
        end = start
    window = PyUnicode_Substring(text, start, end)
    if ptr_is_null(window):
        return -2
    if direction < 0:
        found = py_str_rfind(window, substr)
    else:
        found = py_str_find(window, substr)
    py_decref(window)
    if found < 0:
        return -1
    return start + found


@c_abi_typed_export("PyUnicode_FindChar", "i64", ("ptr", "i32", "i64", "i64", "i32"))
def PyUnicode_FindChar(text, ch: int, start: int, end: int, direction: int) -> int:
    if _is_str(text) == 0:
        _type_error(cstr("expected str"))
        return -2
    if ch > 0x10FFFF:
        _value_error(cstr("unicode character out of range"))
        return -2
    length = _ucs4_len(text)
    if length < 0:
        return -2
    start = _clamp_index(start, length)
    end = _clamp_index(end, length)
    if end < start:
        end = start
    raw = py_str_utf8(text)
    byte_len = py_str_byte_len(text)
    pos_slot = stack_alloc(8)
    ch_slot = stack_alloc(4)
    store_i64(pos_slot, 0, 0)
    index: int = 0
    found: int = -1
    while True:
        ok = _utf8_next_u4(raw, byte_len, pos_slot, ch_slot)
        if ok == 0:
            break
        if ok < 0:
            _value_error(cstr("invalid UTF-8 string data"))
            return -2
        current: int = load_i32(ch_slot, 0)
        if index >= start and index < end and current == ch:
            found = index
            if direction >= 0:
                return found
        index += 1
    return found


@c_abi_typed_export("PyUnicode_ReadChar", "i64", ("ptr", "i64"))
def PyUnicode_ReadChar(unicode_obj, index: int) -> int:
    if _is_str(unicode_obj) == 0:
        _type_error(cstr("expected str"))
        return -1
    if index < 0:
        _index_error(cstr("string index out of range"))
        return -1
    raw = py_str_utf8(unicode_obj)
    byte_len = py_str_byte_len(unicode_obj)
    pos_slot = stack_alloc(8)
    ch_slot = stack_alloc(4)
    store_i64(pos_slot, 0, 0)
    current_index: int = 0
    while True:
        ok = _utf8_next_u4(raw, byte_len, pos_slot, ch_slot)
        if ok == 0:
            break
        if ok < 0:
            _value_error(cstr("invalid UTF-8 string data"))
            return -1
        if current_index == index:
            return load_i32(ch_slot, 0)
        current_index += 1
    _index_error(cstr("string index out of range"))
    return -1


# --- pcc_capi_unicode_read -------------------------------------------

py_type_of = extern("pcc_py_type_of", (c_ptr,), c_int64)


@c_abi_typed_export("pcc_capi_unicode_read", "i64", ("i32", "ptr", "i64"))
def pcc_capi_unicode_read(kind: int, data, index: int) -> int:
    if ptr_is_null(data) or index < 0:
        return -1
    # Data points at the PyStrObject data payload; recover the owner object.
    # PyStrObject: header(16) + byte_len(8) + cp_len(8) + hash(8), so data
    # lives at offset 40.  Fall back to strlen when the owner cannot be
    # verified (matches the C shim's defensive probe).
    owner = ptr_add(data, -40)
    byte_len: int = 0
    if load_i32(owner, 8) == PY_TYPE_STR and ptr_eq(ptr_add(owner, 40), data):  # PY_TYPE_STR
        byte_len = py_str_byte_len(owner)
    else:
        byte_len = strlen(data)
    if byte_len < 0:
        return -1
    pos_slot = stack_alloc(8)
    ch_slot = stack_alloc(4)
    store_i64(pos_slot, 0, 0)
    current: int = 0
    while True:
        ok = _utf8_next_u4(data, byte_len, pos_slot, ch_slot)
        if ok == 0:
            break
        if ok < 0:
            return -1
        if current == index:
            return load_i32(ch_slot, 0)
        current += 1
    return -1
