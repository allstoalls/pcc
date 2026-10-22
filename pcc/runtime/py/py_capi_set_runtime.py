"""pcc-Python owners for the no-libpython C-API set surface.

Replaces the PySet_* / PyAnySet_* block of py_capi_shim.c.  All functions
delegate to the existing pcc-Python set ABIs (py_set_new/add/contains/
remove/len) or to pcc-Python-owned C-API siblings (PyErr_*, py_obj_iter,
py_obj_next).  PySet_New iterates through py_obj_iter/py_obj_next instead of
the C shim's PyIter_Next so it does not depend on the seqiter/cext iterator
machinery (which stays C-side for a later slice).

Owned surface (stable C ABI names):

  PySet_New, PySet_Add, PySet_Contains, PySet_Discard, PySet_Size,
  PySet_Check, PySet_CheckExact, PyAnySet_Check, PyAnySet_CheckExact

Public object type tags come from the generated ``py_abi_constants`` module.
Private exception codes remain owned by the set C-API contract:
  PY_EXC_TYPEERROR = 3
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import PYOBJECTHEADER_TYPE_TAG_OFFSET, PY_TYPE_SET

from pcc.extern import c_abi_typed_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    cstr,
    is_tagged_int,
    load_i32,
    null,
    ptr_is_null,
)

py_set_new = extern("py_set_new", (), c_ptr)
py_set_add = extern("py_set_add", (c_ptr, c_ptr), c_void)
py_set_contains = extern("py_set_contains", (c_ptr, c_ptr), c_int64)
py_set_remove = extern("py_set_remove", (c_ptr, c_ptr), c_int64)
py_set_len = extern("py_set_len", (c_ptr,), c_int64)
py_obj_iter = extern("py_obj_iter", (c_ptr,), c_ptr)
py_obj_next = extern("py_obj_next", (c_ptr,), c_ptr)
py_incref = extern("py_incref", (c_ptr,), c_void)
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


def _is_set(obj) -> int:
    if ptr_is_null(obj) or is_tagged_int(obj):
        return 0
    if load_i32(obj, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_SET:
        return 1
    return 0


def _type_error(message) -> None:
    py_raise_owned(py_exc_new(3, message))  # PY_EXC_TYPEERROR


@c_abi_typed_export("PySet_New", "ptr", ("ptr",))
def PySet_New(iterable) -> c_ptr:
    result = py_set_new()
    if ptr_is_null(result):
        return null()
    if ptr_is_null(iterable):
        return result
    it = py_obj_iter(iterable)
    if ptr_is_null(it):
        py_decref(result)
        return null()
    while True:
        item = py_obj_next(it)
        if ptr_is_null(item):
            if py_err_occurred() != 0:
                cur = py_current_exception()
                stop = py_exc_builtin_class(8)  # PY_EXC_STOPITERATION
                if ptr_is_null(stop) == 0 and py_exc_matches(cur, stop) != 0:
                    py_clear_exception()
                else:
                    py_decref(it)
                    py_decref(result)
                    return null()
            break
        py_set_add(result, item)
        py_decref(item)
        if py_err_occurred() != 0:
            py_decref(it)
            py_decref(result)
            return null()
    py_decref(it)
    return result


@c_abi_typed_export("PySet_Add", "i32", ("ptr", "ptr"))
def PySet_Add(set_obj, key) -> int:
    if _is_set(set_obj) == 0 or ptr_is_null(key):
        _type_error(cstr("invalid PySet_Add call"))
        return -1
    py_set_add(set_obj, key)
    if py_err_occurred() != 0:
        return -1
    return 0


@c_abi_typed_export("PySet_Contains", "i32", ("ptr", "ptr"))
def PySet_Contains(set_obj, key) -> int:
    if _is_set(set_obj) == 0 or ptr_is_null(key):
        _type_error(cstr("invalid PySet_Contains call"))
        return -1
    rc = py_set_contains(set_obj, key)
    if py_err_occurred() != 0:
        return -1
    if rc != 0:
        return 1
    return 0


@c_abi_typed_export("PySet_Discard", "i32", ("ptr", "ptr"))
def PySet_Discard(set_obj, key) -> int:
    if _is_set(set_obj) == 0 or ptr_is_null(key):
        _type_error(cstr("invalid PySet_Discard call"))
        return -1
    rc = py_set_remove(set_obj, key)
    if py_err_occurred() != 0:
        return -1
    if rc == 0:
        return 1
    return 0


@c_abi_typed_export("PySet_Size", "i64", ("ptr",))
def PySet_Size(set_obj) -> int:
    if _is_set(set_obj) == 0:
        _type_error(cstr("invalid PySet_Size call"))
        return -1
    return py_set_len(set_obj)


@c_abi_typed_export("PySet_Check", "i32", ("ptr",))
def PySet_Check(obj) -> int:
    return _is_set(obj)


@c_abi_typed_export("PySet_CheckExact", "i32", ("ptr",))
def PySet_CheckExact(obj) -> int:
    return _is_set(obj)


@c_abi_typed_export("PyAnySet_Check", "i32", ("ptr",))
def PyAnySet_Check(obj) -> int:
    return _is_set(obj)


@c_abi_typed_export("PyAnySet_CheckExact", "i32", ("ptr",))
def PyAnySet_CheckExact(obj) -> int:
    return _is_set(obj)
