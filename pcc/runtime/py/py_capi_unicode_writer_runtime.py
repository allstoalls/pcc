"""pcc-Python owners for the no-libpython PyUnicodeWriter surface.

Replaces the PyUnicodeWriter_* block of py_capi_shim.c.  The writer is a
3-slot heap struct (data@0, length@8, capacity@16) grown with PyMem_Realloc.
The UTF-8 encode/decode helpers (_utf8_write / _utf8_next_u4) are duplicated
here — pcc-Python runtime modules compile standalone and cannot import each
other's private helpers.

Owned surface (stable C ABI names):

  PyUnicodeWriter_Create, PyUnicodeWriter_Finish, PyUnicodeWriter_Discard,
  PyUnicodeWriter_WriteChar, PyUnicodeWriter_WriteUTF8, PyUnicodeWriter_WriteStr,
  PyUnicodeWriter_WriteSubstring

Constants (inlined per the pcc-Python runtime-module contract):
  PY_EXC_VALUEERROR = 2, PY_EXC_SYSTEMERROR = 9, PY_EXC_OVERFLOWERROR = 10
"""

__pcc_runtime_port__ = True

from pcc.extern import c_abi_typed_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    cstr,
    is_tagged_int,
    load_i32,
    load_i64,
    load_i8,
    load_ptr,
    memcpy,
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
py_decref = extern("py_decref", (c_ptr,), c_void)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
PyMem_Malloc = extern("PyMem_Malloc", (c_int64,), c_ptr)
PyMem_Realloc = extern("PyMem_Realloc", (c_ptr, c_int64), c_ptr)
PyMem_Free = extern("PyMem_Free", (c_ptr,), c_void)
PyErr_NoMemory = extern("PyErr_NoMemory", (), c_ptr)
PyObject_Str = extern("PyObject_Str", (c_ptr,), c_ptr)
PyUnicode_AsUTF8AndSize = extern("PyUnicode_AsUTF8AndSize", (c_ptr, c_ptr), c_ptr)
PyUnicode_Substring = extern("PyUnicode_Substring", (c_ptr, c_int64, c_int64), c_ptr)


def _value_error(message) -> None:
    py_raise_owned(py_exc_new(2, message))  # PY_EXC_VALUEERROR


def _system_error(message) -> None:
    py_raise_owned(py_exc_new(7, message))  # PY_EXC_SYSTEMERROR


def _overflow_error(message) -> None:
    py_raise_owned(py_exc_new(15, message))  # PY_EXC_OVERFLOWERROR


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
    if ch <= 0x10FFFF:
        store_i8(out, 0, 0xF0 | (ch >> 18))
        store_i8(out, 1, 0x80 | ((ch >> 12) & 0x3F))
        store_i8(out, 2, 0x80 | ((ch >> 6) & 0x3F))
        store_i8(out, 3, 0x80 | (ch & 0x3F))
        return 4
    return -1


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


def _writer_reserve(writer, extra: int) -> int:
    if ptr_is_null(writer) or extra < 0:
        _overflow_error(cstr("unicode writer size overflow"))
        return -1
    length: int = load_i64(writer, 8)
    if length > 9223372036854775807 - extra:  # LONG_MAX - extra
        _overflow_error(cstr("unicode writer size overflow"))
        return -1
    needed: int = length + extra
    capacity: int = load_i64(writer, 16)
    if needed <= capacity:
        return 0
    if capacity > 0:
        capacity = capacity
    else:
        capacity = 16
    while capacity < needed:
        if capacity > 4611686018427387903:  # LONG_MAX / 2
            capacity = needed
            break
        capacity *= 2
    data: c_ptr = load_ptr(writer, 0)
    resized = PyMem_Realloc(data, capacity + 1)
    if ptr_is_null(resized):
        PyErr_NoMemory()
        return -1
    store_ptr(writer, 0, resized)
    store_i64(writer, 16, capacity)
    store_i8(resized, length, 0)
    return 0


def _writer_append(writer, data, size: int) -> int:
    if ptr_is_null(writer) or size < 0 or (ptr_is_null(data) and size > 0):
        _system_error(cstr("invalid unicode writer append"))
        return -1
    if _writer_reserve(writer, size) < 0:
        return -1
    if size > 0:
        dest = ptr_add(load_ptr(writer, 0), load_i64(writer, 8))
        memcpy(dest, data, size)
    new_length: int = load_i64(writer, 8) + size
    store_i64(writer, 8, new_length)
    store_i8(load_ptr(writer, 0), new_length, 0)
    return 0


@c_abi_typed_export("PyUnicodeWriter_Create", "ptr", ("i64",))
def PyUnicodeWriter_Create(length: int) -> c_ptr:
    if length < 0:
        _value_error(cstr("negative unicode writer length"))
        return null()
    writer = PyMem_Malloc(24)
    if ptr_is_null(writer):
        PyErr_NoMemory()
        return null()
    store_ptr(writer, 0, null())  # data
    store_i64(writer, 8, 0)  # length
    store_i64(writer, 16, 0)  # capacity
    if length > 0 and _writer_reserve(writer, length) < 0:
        PyMem_Free(writer)
        return null()
    return writer


@c_abi_typed_export("PyUnicodeWriter_Finish", "ptr", ("ptr",))
def PyUnicodeWriter_Finish(writer) -> c_ptr:
    if ptr_is_null(writer):
        _system_error(cstr("NULL unicode writer"))
        return null()
    data = load_ptr(writer, 0)
    length: int = load_i64(writer, 8)
    result = py_str_new(data, length)
    PyMem_Free(data)
    PyMem_Free(writer)
    if ptr_is_null(result):
        PyErr_NoMemory()
    return result


@c_abi_typed_export("PyUnicodeWriter_Discard", "void", ("ptr",))
def PyUnicodeWriter_Discard(writer) -> None:
    if ptr_is_null(writer):
        return
    PyMem_Free(load_ptr(writer, 0))
    PyMem_Free(writer)


@c_abi_typed_export("PyUnicodeWriter_WriteChar", "i32", ("ptr", "i32"))
def PyUnicodeWriter_WriteChar(writer, ch: int) -> int:
    encoded = stack_alloc(4)
    size = _utf8_write(encoded, ch)
    if size < 0 or (ch >= 0xD800 and ch <= 0xDFFF):
        _value_error(cstr("invalid unicode codepoint"))
        return -1
    return _writer_append(writer, encoded, size)


@c_abi_typed_export("PyUnicodeWriter_WriteUTF8", "i32", ("ptr", "ptr", "i64"))
def PyUnicodeWriter_WriteUTF8(writer, text, size: int) -> int:
    if ptr_is_null(text):
        _system_error(cstr("NULL UTF-8 input"))
        return -1
    if size == -1:
        size = strlen(text)
    if size < 0:
        _value_error(cstr("negative UTF-8 size"))
        return -1
    pos_slot = stack_alloc(8)
    ch_slot = stack_alloc(4)
    store_i64(pos_slot, 0, 0)
    while load_i64(pos_slot, 0) < size:
        ok = _utf8_next_u4(text, size, pos_slot, ch_slot)
        if ok <= 0:
            _value_error(cstr("invalid UTF-8 input"))
            return -1
    return _writer_append(writer, text, size)


@c_abi_typed_export("PyUnicodeWriter_WriteStr", "i32", ("ptr", "ptr"))
def PyUnicodeWriter_WriteStr(writer, obj) -> int:
    text = PyObject_Str(obj)
    if ptr_is_null(text):
        return -1
    size_slot = stack_alloc(8)
    store_i64(size_slot, 0, 0)
    data = PyUnicode_AsUTF8AndSize(text, size_slot)
    if ptr_is_null(data):
        result = -1
    else:
        result = _writer_append(writer, data, load_i64(size_slot, 0))
    py_decref(text)
    return result


@c_abi_typed_export("PyUnicodeWriter_WriteSubstring", "i32", ("ptr", "ptr", "i64", "i64"))
def PyUnicodeWriter_WriteSubstring(writer, text, start: int, end: int) -> int:
    sub = PyUnicode_Substring(text, start, end)
    if ptr_is_null(sub):
        return -1
    size_slot = stack_alloc(8)
    store_i64(size_slot, 0, 0)
    data = PyUnicode_AsUTF8AndSize(sub, size_slot)
    if ptr_is_null(data):
        result = -1
    else:
        result = _writer_append(writer, data, load_i64(size_slot, 0))
    py_decref(sub)
    return result
