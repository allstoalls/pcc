"""pcc-Python owner for the no-libpython Py_BuildValue surface.

Replaces the Py_BuildValue + pcc_capi_build_many/build_one/build_none/
build_skip block of py_capi_shim.c.  The format engine consumes a va_list via
the pcc.unsafe va_* intrinsics.

Supported format codes (mirroring the C shim): ( ) [ ] { } b h i l ll L n
k K f d s s# y y# O S Y U N u z z# u#.

Owned surface (stable C ABI names):

  Py_BuildValue
"""

__pcc_runtime_port__ = True

from pcc.extern import (
    c_abi_typed_export,
    c_abi_variadic_export,
    c_double,
    c_int64,
    c_ptr,
    c_void,
    extern,
)
from pcc.unsafe import (
    cstr,
    global_load_ptr,
    load_i8,
    load_ptr,
    null,
    ptr_add,
    ptr_is_null,
    stack_alloc,
    store_ptr,
    strlen,
    va_arg_f64,
    va_arg_i64,
    va_arg_ptr,
    va_end,
    va_start,
)

py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
PyLong_FromLong = extern("PyLong_FromLong", (c_int64,), c_ptr)
PyLong_FromLongLong = extern("PyLong_FromLongLong", (c_int64,), c_ptr)
PyLong_FromUnsignedLong = extern("PyLong_FromUnsignedLong", (c_int64,), c_ptr)
PyLong_FromUnsignedLongLong = extern("PyLong_FromUnsignedLongLong", (c_int64,), c_ptr)
PyFloat_FromDouble = extern("PyFloat_FromDouble", (c_double,), c_ptr)
PyUnicode_FromString = extern("PyUnicode_FromString", (c_ptr,), c_ptr)
PyUnicode_FromStringAndSize = extern("PyUnicode_FromStringAndSize", (c_ptr, c_int64), c_ptr)
PyBytes_FromStringAndSize = extern("PyBytes_FromStringAndSize", (c_ptr, c_int64), c_ptr)
PySequence_List = extern("PySequence_List", (c_ptr,), c_ptr)
PyDict_New = extern("PyDict_New", (), c_ptr)
PyDict_SetItem = extern("PyDict_SetItem", (c_ptr, c_ptr, c_ptr), c_int64)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
PyList_Append = extern("PyList_Append", (c_ptr, c_ptr), c_int64)


def _value_error(message) -> None:
    py_raise_owned(py_exc_new(2, message))  # PY_EXC_VALUEERROR


def _build_none() -> c_ptr:
    py_incref(global_load_ptr("py_None"))
    return global_load_ptr("py_None")


def _build_skip(p_ptr) -> None:
    while True:
        p = load_ptr(p_ptr, 0)
        if ptr_is_null(p):
            return
        c: int = load_i8(p, 0)
        if c == 0:
            return
        if c == 32 or c == 9 or c == 10 or c == 44:  # space tab nl comma
            store_ptr(p_ptr, 0, ptr_add(p, 1))
        else:
            return


def _build_many(p_ptr, cursor, terminator: int, force_tuple: int) -> c_ptr:
    items = stack_alloc(512)
    count: int = 0
    while True:
        _build_skip(p_ptr)
        p = load_ptr(p_ptr, 0)
        if ptr_is_null(p):
            break
        c: int = load_i8(p, 0)
        if c == 0:
            break
        if terminator != 0 and c == terminator:
            break
        item = _build_one(p_ptr, cursor)
        if ptr_is_null(item):
            return null()
        store_ptr(items, count * 8, item)
        count += 1
        if count >= 64:
            break
    if terminator != 0:
        p = load_ptr(p_ptr, 0)
        if ptr_is_null(p) or load_i8(p, 0) != terminator:
            _value_error(cstr("unterminated Py_BuildValue tuple"))
            return null()
        store_ptr(p_ptr, 0, ptr_add(p, 1))
    if count == 1 and force_tuple == 0:
        return load_ptr(items, 0)
    out = py_tuple_new(count)
    if ptr_is_null(out):
        return null()
    i: int = 0
    while i < count:
        py_tuple_set_item(out, i, load_ptr(items, i * 8))
        i += 1
    return out


def _build_one(p_ptr, cursor) -> c_ptr:
    _build_skip(p_ptr)
    p = load_ptr(p_ptr, 0)
    if ptr_is_null(p):
        return null()
    code: int = load_i8(p, 0)
    if code == 0:
        return _build_none()
    store_ptr(p_ptr, 0, ptr_add(p, 1))
    if code == 40:  # '('
        return _build_many(p_ptr, cursor, 41, 1)  # ')'
    if code == 91:  # '['
        items = _build_many(p_ptr, cursor, 93, 1)  # ']'
        if ptr_is_null(items):
            return null()
        list_obj = PySequence_List(items)
        py_decref(items)
        return list_obj
    if code == 123:  # '{'
        dict_obj = PyDict_New()
        if ptr_is_null(dict_obj):
            return null()
        while True:
            _build_skip(p_ptr)
            p2 = load_ptr(p_ptr, 0)
            if ptr_is_null(p2):
                break
            c2: int = load_i8(p2, 0)
            if c2 == 125:  # '}'
                store_ptr(p_ptr, 0, ptr_add(p2, 1))
                return dict_obj
            if c2 == 0:
                py_decref(dict_obj)
                _value_error(cstr("unterminated Py_BuildValue dict"))
                return null()
            key = _build_one(p_ptr, cursor)
            if ptr_is_null(key):
                py_decref(dict_obj)
                return null()
            _build_skip(p_ptr)
            p3 = load_ptr(p_ptr, 0)
            if not ptr_is_null(p3) and load_i8(p3, 0) == 58:  # ':'
                store_ptr(p_ptr, 0, ptr_add(p3, 1))
            _build_skip(p_ptr)
            p4 = load_ptr(p_ptr, 0)
            if ptr_is_null(p4) or load_i8(p4, 0) == 125 or load_i8(p4, 0) == 0:
                # A key with no following value ('}' or NUL) is the error case.
                _value_error(cstr("Py_BuildValue dict requires key/value pairs"))
                py_decref(key)
                py_decref(dict_obj)
                return null()
            value = _build_one(p_ptr, cursor)
            if ptr_is_null(value):
                py_decref(key)
                py_decref(dict_obj)
                return null()
            rc = PyDict_SetItem(dict_obj, key, value)
            py_decref(key)
            py_decref(value)
            if rc != 0:
                py_decref(dict_obj)
                return null()
        py_decref(dict_obj)
        return null()
    if code == 98 or code == 104 or code == 105:  # b h i
        return PyLong_FromLong(va_arg_i64(cursor))
    if code == 108:  # 'l'
        p = load_ptr(p_ptr, 0)
        if not ptr_is_null(p) and load_i8(p, 0) == 108:  # 'll'
            store_ptr(p_ptr, 0, ptr_add(p, 1))
            return PyLong_FromLongLong(va_arg_i64(cursor))
        return PyLong_FromLong(va_arg_i64(cursor))
    if code == 76:  # 'L'
        return PyLong_FromLongLong(va_arg_i64(cursor))
    if code == 110:  # 'n'
        return PyLong_FromLong(va_arg_i64(cursor))
    if code == 107:  # 'k'
        return PyLong_FromUnsignedLong(va_arg_i64(cursor))
    if code == 75:  # 'K'
        return PyLong_FromUnsignedLongLong(va_arg_i64(cursor))
    if code == 102 or code == 100:  # f d
        return PyFloat_FromDouble(va_arg_f64(cursor))
    if code == 115:  # 's'
        value = va_arg_ptr(cursor)
        p = load_ptr(p_ptr, 0)
        if not ptr_is_null(p) and load_i8(p, 0) == 35:  # '#'
            store_ptr(p_ptr, 0, ptr_add(p, 1))
            length: int = va_arg_i64(cursor)
            if ptr_is_null(value):
                return _build_none()
            return PyUnicode_FromStringAndSize(value, length)
        if ptr_is_null(value):
            return _build_none()
        return PyUnicode_FromString(value)
    if code == 121:  # 'y'
        value = va_arg_ptr(cursor)
        p = load_ptr(p_ptr, 0)
        if not ptr_is_null(p) and load_i8(p, 0) == 35:  # '#'
            store_ptr(p_ptr, 0, ptr_add(p, 1))
            length = va_arg_i64(cursor)
            if ptr_is_null(value):
                return _build_none()
            return PyBytes_FromStringAndSize(value, length)
        if ptr_is_null(value):
            return _build_none()
        return PyBytes_FromStringAndSize(value, strlen(value))
    if code == 79 or code == 83 or code == 89 or code == 85:  # O S Y U
        obj = va_arg_ptr(cursor)
        if ptr_is_null(obj):
            return _build_none()
        py_incref(obj)
        return obj
    if code == 78:  # 'N'
        return va_arg_ptr(cursor)
    if code == 117 or code == 122:  # u z (wchar-ish; treat as utf8)
        value = va_arg_ptr(cursor)
        p = load_ptr(p_ptr, 0)
        if not ptr_is_null(p) and load_i8(p, 0) == 35:  # u# z#
            store_ptr(p_ptr, 0, ptr_add(p, 1))
            length = va_arg_i64(cursor)
            if ptr_is_null(value):
                return _build_none()
            return PyUnicode_FromStringAndSize(value, length)
        if ptr_is_null(value):
            return _build_none()
        return PyUnicode_FromString(value)
    _value_error(cstr("unsupported Py_BuildValue format code"))
    return null()


@c_abi_typed_export("Py_BuildValue", "ptr", ("ptr",))
@c_abi_variadic_export("Py_BuildValue")
def Py_BuildValue(format) -> c_ptr:
    if ptr_is_null(format):
        _value_error(cstr("NULL Py_BuildValue format"))
        return null()
    p_ptr = stack_alloc(8)
    store_ptr(p_ptr, 0, format)
    cursor = va_start()
    out = _build_many(p_ptr, cursor, 0, 0)
    va_end(cursor)
    return out


@c_abi_typed_export("pcc_capi_build_call_args", "ptr", ("ptr", "ptr"))
def pcc_capi_build_call_args(format, cursor) -> c_ptr:
    # Full-format args builder for PyObject_CallFunction/CallMethod: same
    # engine as Py_BuildValue but force_tuple=1 (mirrors the C shim helper of
    # the same name). The cursor is the caller's va_start() cursor.
    if ptr_is_null(format):
        return py_tuple_new(0)
    p_ptr = stack_alloc(8)
    store_ptr(p_ptr, 0, format)
    return _build_many(p_ptr, cursor, 0, 1)
