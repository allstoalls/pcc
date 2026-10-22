"""pcc-Python owners for the no-libpython C-API mapping surface.

Replaces the PyMapping_* block of py_capi_shim.c.  All functions delegate to
the migrated C-API siblings (PyObject_GetItem/SetItem/Size, PyUnicode_FromString,
PyDict_Keys/Values/Check, PyErr_*) or the pcc-Python object ABIs
(py_obj_getattr).

Owned surface (stable C ABI names):

  PyMapping_Check, PyMapping_Size, PyMapping_Length, PyMapping_Keys,
  PyMapping_Values, PyMapping_Items, PyMapping_GetItemString,
  PyMapping_SetItemString, PyMapping_GetOptionalItem,
  PyMapping_GetOptionalItemString, PyMapping_HasKey, PyMapping_HasKeyString,
  PyMapping_HasKeyWithError, PyMapping_HasKeyStringWithError

Public object type tags come from the generated ``py_abi_constants`` module.
Private exception codes remain owned by the mapping C-API contract:
  PY_EXC_TYPEERROR = 3, PY_EXC_KEYERROR = 4
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import PY_TYPE_DICT, PY_TYPE_INSTANCE, PY_TYPE_USER_CLASS_START

from pcc.extern import c_abi_typed_export, c_int32, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    cstr,
    global_load_ptr,
    is_tagged_int,
    load_i32,
    load_ptr,
    null,
    ptr_is_null,
    stack_alloc,
    store_ptr,
)

py_obj_getattr = extern("py_obj_getattr", (c_ptr, c_ptr), c_ptr)
py_obj_call = extern("py_obj_call", (c_ptr, c_ptr, c_ptr), c_ptr)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_exc_matches = extern("py_exc_matches", (c_ptr, c_ptr), c_int64)
py_exc_builtin_class = extern("py_exc_builtin_class", (c_int64,), c_ptr)
py_current_exception = extern("py_current_exception", (), c_ptr)
PyObject_Size = extern("PyObject_Size", (c_ptr,), c_int64)
PyObject_GetItem = extern("PyObject_GetItem", (c_ptr, c_ptr), c_ptr)
PyObject_SetItem = extern("PyObject_SetItem", (c_ptr, c_ptr, c_ptr), c_int64)
PyUnicode_FromString = extern("PyUnicode_FromString", (c_ptr,), c_ptr)
PyDict_Check = extern("PyDict_Check", (c_ptr,), c_int32)
PyDict_Keys = extern("PyDict_Keys", (c_ptr,), c_ptr)
PyDict_Values = extern("PyDict_Values", (c_ptr,), c_ptr)
PyDict_Items = extern("PyDict_Items", (c_ptr,), c_ptr)


def _py_none() -> c_ptr:
    return global_load_ptr("py_None")


def _type_error(message) -> None:
    py_raise_owned(py_exc_new(3, message))  # PY_EXC_TYPEERROR


def _is_key_error() -> int:
    cur = py_current_exception()
    if ptr_is_null(cur):
        return 0
    key_cls = py_exc_builtin_class(4)  # PY_EXC_KEYERROR
    if ptr_is_null(key_cls):
        return 0
    if py_exc_matches(cur, key_cls) != 0:
        return 1
    return 0


@c_abi_typed_export("PyMapping_Check", "i32", ("ptr",))
def PyMapping_Check(obj) -> int:
    if ptr_is_null(obj) or is_tagged_int(obj):
        return 0
    tag: int = load_i32(obj, 8)
    if (
        tag == PY_TYPE_DICT
        or tag == PY_TYPE_INSTANCE
        or tag >= PY_TYPE_USER_CLASS_START
    ):
        return 1
    return 0


@c_abi_typed_export("PyMapping_Size", "i64", ("ptr",))
def PyMapping_Size(obj) -> int:
    if PyMapping_Check(obj) == 0:
        _type_error(cstr("expected mapping"))
        return -1
    return PyObject_Size(obj)


@c_abi_typed_export("PyMapping_Length", "i64", ("ptr",))
def PyMapping_Length(obj) -> int:
    return PyMapping_Size(obj)


def _mapping_noarg(obj, method) -> c_ptr:
    if PyMapping_Check(obj) == 0:
        _type_error(cstr("expected mapping"))
        return null()
    method_obj = py_obj_getattr(obj, method)
    if ptr_is_null(method_obj):
        return null()
    empty = py_tuple_new(0)
    if ptr_is_null(empty):
        py_decref(method_obj)
        return null()
    result = py_obj_call(method_obj, empty, _py_none())
    py_decref(empty)
    py_decref(method_obj)
    return result


@c_abi_typed_export("PyMapping_Keys", "ptr", ("ptr",))
def PyMapping_Keys(obj) -> c_ptr:
    if PyDict_Check(obj) != 0:
        return PyDict_Keys(obj)
    return _mapping_noarg(obj, cstr("keys"))


@c_abi_typed_export("PyMapping_Values", "ptr", ("ptr",))
def PyMapping_Values(obj) -> c_ptr:
    if PyDict_Check(obj) != 0:
        return PyDict_Values(obj)
    return _mapping_noarg(obj, cstr("values"))


@c_abi_typed_export("PyMapping_Items", "ptr", ("ptr",))
def PyMapping_Items(obj) -> c_ptr:
    if PyDict_Check(obj) != 0:
        return PyDict_Items(obj)
    return _mapping_noarg(obj, cstr("items"))


@c_abi_typed_export("PyMapping_GetItemString", "ptr", ("ptr", "ptr"))
def PyMapping_GetItemString(obj, key) -> c_ptr:
    if ptr_is_null(key):
        _type_error(cstr("NULL mapping key"))
        return null()
    key_obj = PyUnicode_FromString(key)
    if ptr_is_null(key_obj):
        return null()
    out = PyObject_GetItem(obj, key_obj)
    py_decref(key_obj)
    return out


@c_abi_typed_export("PyMapping_SetItemString", "i32", ("ptr", "ptr", "ptr"))
def PyMapping_SetItemString(obj, key, value) -> int:
    if ptr_is_null(key):
        _type_error(cstr("NULL mapping key"))
        return -1
    key_obj = PyUnicode_FromString(key)
    if ptr_is_null(key_obj):
        return -1
    rc = PyObject_SetItem(obj, key_obj, value)
    py_decref(key_obj)
    return rc


@c_abi_typed_export("PyMapping_GetOptionalItem", "i32", ("ptr", "ptr", "ptr"))
def PyMapping_GetOptionalItem(obj, key, result_ptr) -> int:
    if ptr_is_null(result_ptr):
        _type_error(cstr("NULL result pointer"))
        return -1
    store_ptr(result_ptr, 0, null())
    value = PyObject_GetItem(obj, key)
    if not ptr_is_null(value):
        store_ptr(result_ptr, 0, value)
        return 1
    if _is_key_error() != 0:
        py_clear_exception()
        return 0
    if py_err_occurred() != 0:
        return -1
    return 0


@c_abi_typed_export("PyMapping_GetOptionalItemString", "i32", ("ptr", "ptr", "ptr"))
def PyMapping_GetOptionalItemString(obj, key, result_ptr) -> int:
    if ptr_is_null(key):
        _type_error(cstr("NULL mapping key"))
        return -1
    key_obj = PyUnicode_FromString(key)
    if ptr_is_null(key_obj):
        return -1
    rc = PyMapping_GetOptionalItem(obj, key_obj, result_ptr)
    py_decref(key_obj)
    return rc


@c_abi_typed_export("PyMapping_HasKeyWithError", "i32", ("ptr", "ptr"))
def PyMapping_HasKeyWithError(obj, key) -> int:
    item_ptr = stack_alloc(8)
    store_ptr(item_ptr, 0, null())
    rc = PyMapping_GetOptionalItem(obj, key, item_ptr)
    item = load_ptr(item_ptr, 0)
    if not ptr_is_null(item):
        py_decref(item)
    return rc


@c_abi_typed_export("PyMapping_HasKeyStringWithError", "i32", ("ptr", "ptr"))
def PyMapping_HasKeyStringWithError(obj, key) -> int:
    item_ptr = stack_alloc(8)
    store_ptr(item_ptr, 0, null())
    rc = PyMapping_GetOptionalItemString(obj, key, item_ptr)
    item = load_ptr(item_ptr, 0)
    if not ptr_is_null(item):
        py_decref(item)
    return rc


@c_abi_typed_export("PyMapping_HasKey", "i32", ("ptr", "ptr"))
def PyMapping_HasKey(obj, key) -> int:
    rc = PyMapping_HasKeyWithError(obj, key)
    if rc < 0:
        py_clear_exception()
        return 0
    return rc


@c_abi_typed_export("PyMapping_HasKeyString", "i32", ("ptr", "ptr"))
def PyMapping_HasKeyString(obj, key) -> int:
    rc = PyMapping_HasKeyStringWithError(obj, key)
    if rc < 0:
        py_clear_exception()
        return 0
    return rc
