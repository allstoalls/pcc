"""pcc-Python owners for the tuple, list, and bytes C-API facades.

Container semantics stay in ``py_tuple``, ``py_list``, and ``py_obj_stubs``.
This module only adapts their owned-reference ABI to CPython's borrowed/stolen
reference contracts and exposes the exact scalar C signatures extensions use.
"""

__pcc_runtime_port__ = True

from pcc.extern import (
    c_abi_typed_export,
    c_abi_variadic_export,
    c_int32,
    c_int64,
    c_ptr,
    c_void,
    extern,
)
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
    store_i64,
    store_ptr,
    strlen,
    va_arg_ptr,
    va_end,
    va_start,
)


py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_get = extern("py_tuple_get", (c_ptr, c_int64), c_ptr)
py_tuple_set_item = extern(
    "py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void
)
py_tuple_len = extern("py_tuple_len", (c_ptr,), c_int64)
py_list_new = extern("py_list_new", (c_int64,), c_ptr)
py_list_append = extern("py_list_append", (c_ptr, c_ptr), c_void)
py_list_get = extern("py_list_get", (c_ptr, c_int64), c_ptr)
py_list_len = extern("py_list_len", (c_ptr,), c_int64)
py_bytes_new = extern("py_bytes_new", (c_ptr, c_int64), c_ptr)
pcc_gc_store_ptr = extern(
    "pcc_gc_store_ptr", (c_ptr, c_ptr, c_ptr), c_void
)


def _raise_collection(kind: int, message) -> None:
    py_raise_owned(py_exc_new(kind, message))


def _is_exact_type(obj, expected_tag: int) -> int:
    if ptr_is_null(obj) or is_tagged_int(obj):
        return 0
    if load_i32(obj, 8) == expected_tag:
        return 1
    return 0


@c_abi_typed_export("PyTuple_Size", "i64", ("ptr",))
def PyTuple_Size(obj) -> int:
    if _is_exact_type(obj, 7) == 0:
        _raise_collection(3, cstr("expected tuple"))
        return -1
    return py_tuple_len(obj)


@c_abi_typed_export("PyTuple_GetItem", "ptr", ("ptr", "i64"))
def PyTuple_GetItem(obj, index: int):
    if _is_exact_type(obj, 7) == 0:
        _raise_collection(3, cstr("expected tuple"))
        return null()
    item = py_tuple_get(obj, index)
    if ptr_is_null(item):
        _raise_collection(7, cstr("tuple index out of range"))
        return null()
    # py_tuple_get returns owned; PyTuple_GetItem returns borrowed.
    pcc_gc_pin(item)
    py_decref(item)
    return item


@c_abi_typed_export("PyTuple_New", "ptr", ("i64",))
def PyTuple_New(size: int):
    if size < 0:
        _raise_collection(2, cstr("negative tuple size"))
        return null()
    return py_tuple_new(size)


@c_abi_typed_export("PyTuple_SetItem", "i32", ("ptr", "i64", "ptr"))
def PyTuple_SetItem(obj, index: int, value) -> int:
    if _is_exact_type(obj, 7) == 0 or ptr_is_null(value):
        _raise_collection(3, cstr("invalid PyTuple_SetItem call"))
        return -1
    size: int = py_tuple_len(obj)
    if index < 0 or index >= size:
        _raise_collection(7, cstr("tuple index out of range"))
        return -1
    py_tuple_set_item(obj, index, value)
    # PyTuple_SetItem steals the caller's reference on success.
    py_decref(value)
    return 0


@c_abi_typed_export("PyTuple_Pack", "ptr", ("i64",))
@c_abi_variadic_export("PyTuple_Pack")
def PyTuple_Pack(size: int):
    if size < 0:
        _raise_collection(2, cstr("negative tuple size"))
        return null()
    result = py_tuple_new(size)
    if ptr_is_null(result):
        return null()
    cursor = va_start()
    index: int = 0
    while index < size:
        item = va_arg_ptr(cursor)
        if ptr_is_null(item):
            va_end(cursor)
            py_decref(result)
            _raise_collection(3, cstr("NULL item in PyTuple_Pack"))
            return null()
        # Pack borrows its variadic inputs; py_tuple_set_item retains them.
        py_tuple_set_item(result, index, item)
        index = index + 1
    va_end(cursor)
    return result


@c_abi_typed_export("PyTuple_Check", "i32", ("ptr",))
def PyTuple_Check(obj) -> int:
    return _is_exact_type(obj, 7)


@c_abi_typed_export("PyTuple_CheckExact", "i32", ("ptr",))
def PyTuple_CheckExact(obj) -> int:
    return _is_exact_type(obj, 7)


@c_abi_typed_export("PyList_New", "ptr", ("i64",))
def PyList_New(size: int):
    if size < 0:
        _raise_collection(2, cstr("negative list size"))
        return null()
    result = py_list_new(size)
    if ptr_is_null(result):
        return null()
    items = load_ptr(result, 32)
    index: int = 0
    while index < size:
        store_ptr(items, index * 8, null())
        index = index + 1
    store_i64(result, 16, size)
    return result


@c_abi_typed_export("PyList_SetItem", "i32", ("ptr", "i64", "ptr"))
def PyList_SetItem(obj, index: int, value) -> int:
    if _is_exact_type(obj, 5) == 0 or ptr_is_null(value):
        _raise_collection(3, cstr("invalid PyList_SetItem call"))
        return -1
    size: int = load_i64(obj, 16)
    if index < 0 or index >= size:
        _raise_collection(7, cstr("list index out of range"))
        return -1
    items = load_ptr(obj, 32)
    pcc_gc_store_ptr(obj, ptr_add(items, index * 8), value)
    # PyList_SetItem steals the caller's reference on success.
    py_decref(value)
    return 0


@c_abi_typed_export("PyList_GetItem", "ptr", ("ptr", "i64"))
def PyList_GetItem(obj, index: int):
    if _is_exact_type(obj, 5) == 0:
        _raise_collection(3, cstr("expected list"))
        return null()
    item = py_list_get(obj, index)
    if ptr_is_null(item):
        _raise_collection(7, cstr("list index out of range"))
        return null()
    # py_list_get returns owned; PyList_GetItem returns borrowed.
    pcc_gc_pin(item)
    py_decref(item)
    return item


@c_abi_typed_export("PyList_GetItemRef", "ptr", ("ptr", "i64"))
def PyList_GetItemRef(obj, index: int):
    if _is_exact_type(obj, 5) == 0:
        _raise_collection(3, cstr("expected list"))
        return null()
    item = py_list_get(obj, index)
    if ptr_is_null(item):
        _raise_collection(7, cstr("list index out of range"))
    return item


@c_abi_typed_export("PyList_Size", "i64", ("ptr",))
def PyList_Size(obj) -> int:
    if _is_exact_type(obj, 5) == 0:
        _raise_collection(3, cstr("expected list"))
        return -1
    return py_list_len(obj)


@c_abi_typed_export("PyList_Append", "i32", ("ptr", "ptr"))
def PyList_Append(obj, value) -> int:
    if _is_exact_type(obj, 5) == 0 or ptr_is_null(value):
        _raise_collection(3, cstr("invalid PyList_Append call"))
        return -1
    py_list_append(obj, value)
    if py_err_occurred() != 0:
        return -1
    return 0


@c_abi_typed_export("PyList_AsTuple", "ptr", ("ptr",))
def PyList_AsTuple(obj):
    if _is_exact_type(obj, 5) == 0:
        _raise_collection(3, cstr("expected list"))
        return null()
    size: int = py_list_len(obj)
    result = py_tuple_new(size)
    if ptr_is_null(result):
        return null()
    index: int = 0
    while index < size:
        item = py_list_get(obj, index)
        if ptr_is_null(item):
            py_decref(result)
            _raise_collection(7, cstr("list item missing"))
            return null()
        py_tuple_set_item(result, index, item)
        py_decref(item)
        index = index + 1
    return result


@c_abi_typed_export("PyList_Check", "i32", ("ptr",))
def PyList_Check(obj) -> int:
    return _is_exact_type(obj, 5)


@c_abi_typed_export("PyList_CheckExact", "i32", ("ptr",))
def PyList_CheckExact(obj) -> int:
    return _is_exact_type(obj, 5)


@c_abi_typed_export("PyBytes_FromStringAndSize", "ptr", ("ptr", "i64"))
def PyBytes_FromStringAndSize(value, size: int):
    if size < 0:
        _raise_collection(2, cstr("negative bytes size"))
        return null()
    return py_bytes_new(value, size)


@c_abi_typed_export("PyBytes_FromString", "ptr", ("ptr",))
def PyBytes_FromString(value):
    if ptr_is_null(value):
        _raise_collection(3, cstr("NULL bytes string"))
        return null()
    return py_bytes_new(value, strlen(value))


@c_abi_typed_export("PyBytes_AsString", "ptr", ("ptr",))
def PyBytes_AsString(obj):
    if _is_exact_type(obj, 17) == 0:
        _raise_collection(3, cstr("expected bytes"))
        return null()
    pcc_gc_pin(obj)
    return ptr_add(obj, 24)


@c_abi_typed_export(
    "PyBytes_AsStringAndSize", "i32", ("ptr", "ptr", "ptr")
)
def PyBytes_AsStringAndSize(obj, buffer, length) -> int:
    if _is_exact_type(obj, 17) == 0 or ptr_is_null(buffer):
        _raise_collection(3, cstr("invalid PyBytes_AsStringAndSize call"))
        return -1
    size: int = load_i64(obj, 16)
    data = ptr_add(obj, 24)
    if ptr_is_null(length):
        index: int = 0
        while index < size:
            if load_i8(data, index) == 0:
                _raise_collection(2, cstr("embedded null byte"))
                return -1
            index = index + 1
    pcc_gc_pin(obj)
    store_ptr(buffer, 0, data)
    if not ptr_is_null(length):
        store_i64(length, 0, size)
    return 0


@c_abi_typed_export("PyBytes_Size", "i64", ("ptr",))
def PyBytes_Size(obj) -> int:
    if _is_exact_type(obj, 17) == 0:
        _raise_collection(3, cstr("expected bytes"))
        return -1
    return load_i64(obj, 16)


@c_abi_typed_export("PyBytes_Check", "i32", ("ptr",))
def PyBytes_Check(obj) -> int:
    return _is_exact_type(obj, 17)


@c_abi_typed_export("PyBytes_CheckExact", "i32", ("ptr",))
def PyBytes_CheckExact(obj) -> int:
    return _is_exact_type(obj, 17)
