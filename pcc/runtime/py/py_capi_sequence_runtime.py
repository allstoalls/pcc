"""pcc-Python owners for the no-libpython C-API sequence surface.

Replaces the PySequence_* block of py_capi_shim.c.  All functions delegate to
the existing pcc-Python object ABIs (py_obj_getitem/setitem/len/contains/add)
or to pcc-Python-owned C-API siblings (PyList_New/Append, PyTuple_New,
PyNumber_Index, PyErr_*).

Owned surface (stable C ABI names):

  PySequence_Check, PySequence_Size, PySequence_GetItem, PySequence_SetItem,
  PySequence_Contains, PySequence_Concat, PySequence_InPlaceConcat,
  PySequence_Repeat, PySequence_InPlaceRepeat, PySequence_Fast,
  PySequence_Fast_GET_SIZE, PySequence_Fast_ITEMS, PySequence_List,
  PySequence_Tuple, PySequence_Length

Public object type tags come from the generated ``py_abi_constants`` module.
Private exception codes and sequence-layout details remain owned here:
  PY_EXC_TYPEERROR = 3
  PyTupleObject items@24, PyListObject items@24
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import PY_TYPE_BYTEARRAY, PY_TYPE_BYTES, PY_TYPE_LIST, PY_TYPE_MEMORYVIEW, PY_TYPE_STR, PY_TYPE_TUPLE

from pcc.extern import c_abi_typed_export, c_int32, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    cstr,
    is_tagged_int,
    load_i32,
    load_i64,
    load_ptr,
    null,
    ptr_add,
    ptr_is_null,
    store_i64,
)

py_obj_getitem = extern("py_obj_getitem", (c_ptr, c_ptr), c_ptr)
py_obj_setitem = extern("py_obj_setitem", (c_ptr, c_ptr, c_ptr), c_int64)
py_obj_len = extern("py_obj_len", (c_ptr,), c_int64)
py_obj_contains = extern("py_obj_contains", (c_ptr, c_ptr), c_int64)
py_obj_add = extern("py_obj_add", (c_ptr, c_ptr), c_ptr)
py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_err_occurred = extern("py_err_occurred", (), c_int64)
pcc_capi_cext_type_tag = extern("pcc_capi_is_cext_type_tag", (c_int64,), c_int64)
pcc_capi_cext_type_for_object = extern("pcc_capi_cext_type_for_object", (c_ptr,), c_ptr)
PyList_New = extern("PyList_New", (c_int64,), c_ptr)
PyList_Append = extern("PyList_Append", (c_ptr, c_ptr), c_int64)
PyTuple_Check = extern("PyTuple_Check", (c_ptr,), c_int32)
PyList_Check = extern("PyList_Check", (c_ptr,), c_int32)
py_tuple_len = extern("py_tuple_len", (c_ptr,), c_int64)
py_list_len = extern("py_list_len", (c_ptr,), c_int64)
py_str_repeat = extern("py_str_repeat", (c_ptr, c_ptr), c_ptr)
py_bytes_repeat = extern("py_bytes_repeat", (c_ptr, c_int64), c_ptr)
py_list_repeat = extern("py_list_repeat", (c_ptr, c_int64), c_ptr)
py_tuple_repeat = extern("py_tuple_repeat", (c_ptr, c_int64), c_ptr)
py_int_value_i64 = extern("py_int_value_i64", (c_ptr,), c_int64)
pcc_capi_cext_object_getitem = extern(
    "pcc_capi_cext_object_getitem", (c_ptr, c_ptr), c_ptr
)


def _type_error(message) -> None:
    py_raise_owned(py_exc_new(3, message))  # PY_EXC_TYPEERROR


def _is_sequence_tag(tag: int) -> int:
    if (
        tag == PY_TYPE_TUPLE
        or tag == PY_TYPE_LIST
        or tag == PY_TYPE_STR
        or tag == PY_TYPE_BYTES
        or tag == PY_TYPE_BYTEARRAY
        or tag == PY_TYPE_MEMORYVIEW
    ):
        return 1
    return 0


@c_abi_typed_export("PySequence_Check", "i32", ("ptr",))
def PySequence_Check(obj) -> int:
    if ptr_is_null(obj) or is_tagged_int(obj):
        return 0
    tag: int = load_i32(obj, 8)
    if _is_sequence_tag(tag) != 0:
        return 1
    if pcc_capi_cext_type_tag(tag) != 0:
        type_obj = pcc_capi_cext_type_for_object(obj)
        if not ptr_is_null(type_obj):
            seq = load_ptr(type_obj, 112)  # tp_as_sequence
            if not ptr_is_null(seq) and not ptr_is_null(load_ptr(seq, 24)):  # sq_item
                return 1
            mapping = load_ptr(type_obj, 120)  # tp_as_mapping
            if not ptr_is_null(mapping) and not ptr_is_null(load_ptr(mapping, 8)):  # mp_subscript
                return 1
    return 0


@c_abi_typed_export("PySequence_Size", "i64", ("ptr",))
def PySequence_Size(obj) -> int:
    if PySequence_Check(obj) == 0:
        _type_error(cstr("expected sequence"))
        return -1
    return py_obj_len(obj)


@c_abi_typed_export("PySequence_Length", "i64", ("ptr",))
def PySequence_Length(obj) -> int:
    return PySequence_Size(obj)


@c_abi_typed_export("PySequence_GetItem", "ptr", ("ptr", "i64"))
def PySequence_GetItem(obj, index: int) -> c_ptr:
    if PySequence_Check(obj) == 0:
        _type_error(cstr("expected sequence"))
        return null()
    key = py_int_from_i64(index)
    if ptr_is_null(key):
        return null()
    item = py_obj_getitem(obj, key)
    py_decref(key)
    return item


@c_abi_typed_export("PySequence_SetItem", "i32", ("ptr", "i64", "ptr"))
def PySequence_SetItem(obj, index: int, value) -> int:
    if PySequence_Check(obj) == 0 or ptr_is_null(value):
        _type_error(cstr("invalid PySequence_SetItem call"))
        return -1
    key = py_int_from_i64(index)
    if ptr_is_null(key):
        return -1
    rc = py_obj_setitem(obj, key, value)
    py_decref(key)
    if rc != 0 and py_err_occurred() == 0:
        _type_error(cstr("sequence does not support item assignment"))
    if rc == 0:
        return 0
    return -1


@c_abi_typed_export("PySequence_Contains", "i32", ("ptr", "ptr"))
def PySequence_Contains(obj, value) -> int:
    if ptr_is_null(obj) or ptr_is_null(value):
        _type_error(cstr("invalid PySequence_Contains call"))
        return -1
    contains = py_obj_contains(obj, value)
    if py_err_occurred() != 0:
        return -1
    if contains != 0:
        return 1
    return 0


@c_abi_typed_export("PySequence_Concat", "ptr", ("ptr", "ptr"))
def PySequence_Concat(left, right) -> c_ptr:
    if PySequence_Check(left) == 0 or PySequence_Check(right) == 0:
        _type_error(cstr("expected sequences"))
        return null()
    result = py_obj_add(left, right)
    if ptr_is_null(result) and py_err_occurred() == 0:
        _type_error(cstr("unsupported sequence concatenation"))
    return result


@c_abi_typed_export("PySequence_InPlaceConcat", "ptr", ("ptr", "ptr"))
def PySequence_InPlaceConcat(left, right) -> c_ptr:
    return PySequence_Concat(left, right)


@c_abi_typed_export("PySequence_Repeat", "ptr", ("ptr", "i64"))
def PySequence_Repeat(obj, count: int) -> c_ptr:
    if PySequence_Check(obj) == 0:
        _type_error(cstr("expected sequence"))
        return null()
    count_obj = py_int_from_i64(count)
    if ptr_is_null(count_obj):
        return null()
    result = _repeat_sequence(obj, count_obj)
    py_decref(count_obj)
    if ptr_is_null(result) and py_err_occurred() == 0:
        _type_error(cstr("unsupported sequence repeat"))
    return result


@c_abi_typed_export("PySequence_InPlaceRepeat", "ptr", ("ptr", "i64"))
def PySequence_InPlaceRepeat(obj, count: int) -> c_ptr:
    return PySequence_Repeat(obj, count)


def _repeat_sequence(seq, count_obj) -> c_ptr:
    if ptr_is_null(seq) or ptr_is_null(count_obj) or is_tagged_int(seq):
        return null()
    tag: int = load_i32(seq, 8)
    if _is_sequence_tag(tag) == 0:
        return null()
    n_obj = count_obj
    count: int = py_int_value_i64(n_obj)
    if py_err_occurred() != 0:
        return null()
    if tag == PY_TYPE_STR:  # str
        return py_str_repeat(seq, n_obj)
    if tag == PY_TYPE_BYTES or tag == PY_TYPE_BYTEARRAY:  # bytes/bytearray
        return py_bytes_repeat(seq, count)
    if tag == PY_TYPE_LIST:  # list
        return py_list_repeat(seq, count)
    if tag == PY_TYPE_TUPLE:  # tuple
        return py_tuple_repeat(seq, count)
    return null()


@c_abi_typed_export("PySequence_Fast", "ptr", ("ptr", "ptr"))
def PySequence_Fast(obj, message) -> c_ptr:
    if PyTuple_Check(obj) != 0 or PyList_Check(obj) != 0:
        py_incref(obj)
        return obj
    if PySequence_Check(obj) == 0:
        if ptr_is_null(message):
            message = cstr("expected sequence")
        _type_error(message)
        return null()
    return PySequence_Tuple(obj)


@c_abi_typed_export("PySequence_Fast_GET_SIZE", "i64", ("ptr",))
def PySequence_Fast_GET_SIZE(obj) -> int:
    if PyTuple_Check(obj) != 0:
        return py_tuple_len(obj)
    return py_list_len(obj)


@c_abi_typed_export("PySequence_Fast_ITEMS", "ptr", ("ptr",))
def PySequence_Fast_ITEMS(obj) -> c_ptr:
    # PyTupleObject stores its items inline at offset 24; PyListObject stores
    # a pointer at offset 32 (length@16, capacity@24, items-pointer@32).
    pcc_gc_pin(obj)
    if PyList_Check(obj) != 0:
        return load_ptr(obj, 32)
    return ptr_add(obj, 24)


@c_abi_typed_export("PySequence_List", "ptr", ("ptr",))
def PySequence_List(obj) -> c_ptr:
    n = PySequence_Size(obj)
    if n < 0:
        return null()
    out = PyList_New(0)
    if ptr_is_null(out):
        return null()
    i: int = 0
    while i < n:
        item = PySequence_GetItem(obj, i)
        if ptr_is_null(item):
            py_decref(out)
            return null()
        if PyList_Append(out, item) != 0:
            py_decref(item)
            py_decref(out)
            return null()
        py_decref(item)
        i += 1
    return out


@c_abi_typed_export("PySequence_Tuple", "ptr", ("ptr",))
def PySequence_Tuple(obj) -> c_ptr:
    n = PySequence_Size(obj)
    if n < 0:
        return null()
    out = py_tuple_new(n)
    if ptr_is_null(out):
        return null()
    i: int = 0
    while i < n:
        item = PySequence_GetItem(obj, i)
        if ptr_is_null(item):
            py_decref(out)
            return null()
        py_tuple_set_item(out, i, item)
        py_decref(item)
        i += 1
    return out


@c_abi_typed_export("PySlice_AdjustIndices", "i64", ("i64", "ptr", "ptr", "i64"))
def PySlice_AdjustIndices(length: int, start_ptr, stop_ptr, step: int) -> int:
    start: int = load_i64(start_ptr, 0)
    stop: int = load_i64(stop_ptr, 0)
    if start < 0:
        start += length
        if start < 0:
            if step < 0:
                start = -1
            else:
                start = 0
    elif start >= length:
        if step < 0:
            start = length - 1
        else:
            start = length
    if stop < 0:
        stop += length
        if stop < 0:
            if step < 0:
                stop = -1
            else:
                stop = 0
    elif stop >= length:
        if step < 0:
            stop = length - 1
        else:
            stop = length
    store_i64(start_ptr, 0, start)
    store_i64(stop_ptr, 0, stop)
    if step < 0:
        if stop < start:
            return _c_trunc_div(start - stop - 1, -step) + 1
    else:
        if start < stop:
            return _c_trunc_div(stop - start - 1, step) + 1
    return 0


def _c_trunc_div(a: int, b: int) -> int:
    # C-style truncated division: a / b toward zero.
    if a >= 0:
        return a // b
    return -((-a) // b)
