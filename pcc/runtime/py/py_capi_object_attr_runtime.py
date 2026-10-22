"""pcc-Python owners for the no-libpython C-API object attribute surface.

Replaces the PyObject_GetAttr/SetAttr/HasAttr/GetOptionalAttr block of
py_capi_shim.c.  All delegate to the migrated C-API siblings (PyUnicode_AsUTF8,
pcc_capi_builtin_object_getattr, PyErr_*) and the pcc-Python object ABIs
(py_obj_getattr, py_obj_setattr).

Owned surface (stable C ABI names):

  PyObject_GetAttr, PyObject_GetAttrString, PyObject_SetAttr,
  PyObject_SetAttrString, PyObject_HasAttr, PyObject_HasAttrString,
  PyObject_HasAttrWithError, PyObject_HasAttrStringWithError,
  PyObject_GetOptionalAttr, PyObject_GetOptionalAttrString

Constants (inlined per the pcc-Python runtime-module contract):
  PY_EXC_TYPEERROR = 3, PY_EXC_ATTRIBUTEERROR = 6
"""

__pcc_runtime_port__ = True

from pcc.extern import c_abi_typed_export, c_int32, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    cstr,
    load_i64,
    null,
    ptr_is_null,
    store_i32,
    store_i64,
    store_ptr,
)
from pcc.runtime.py.py_abi_constants import PY_FLAG_GC_MALLOC_ALLOC

py_obj_getattr = extern("py_obj_getattr", (c_ptr, c_ptr), c_ptr)
py_obj_setattr = extern("py_obj_setattr", (c_ptr, c_ptr, c_ptr), c_int64)
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
PyUnicode_AsUTF8 = extern("PyUnicode_AsUTF8", (c_ptr,), c_ptr)
pcc_capi_builtin_object_getattr = extern(
    "pcc_capi_builtin_object_getattr", (c_ptr, c_ptr), c_ptr
)
pcc_gc_pointer_register = extern(
    "pcc_gc_pointer_register", (c_ptr,), c_int64
)
pcc_gc_note_object_allocated_sized = extern(
    "pcc_gc_note_object_allocated_sized", (c_ptr, c_int64), c_void
)


def _type_error(message) -> None:
    py_raise_owned(py_exc_new(3, message))  # PY_EXC_TYPEERROR


def _is_attribute_error() -> int:
    cur = py_current_exception()
    if ptr_is_null(cur):
        return 0
    attr_cls = py_exc_builtin_class(6)  # PY_EXC_ATTRIBUTEERROR
    if ptr_is_null(attr_cls):
        return 0
    if py_exc_matches(cur, attr_cls) != 0:
        return 1
    return 0


@c_abi_typed_export("PyObject_GetAttrString", "ptr", ("ptr", "ptr"))
def PyObject_GetAttrString(obj, attr) -> c_ptr:
    if ptr_is_null(obj) or ptr_is_null(attr):
        _type_error(cstr("invalid PyObject_GetAttrString call"))
        return null()
    builtin_attr = pcc_capi_builtin_object_getattr(obj, attr)
    if not ptr_is_null(builtin_attr) or py_err_occurred() != 0:
        return builtin_attr
    return py_obj_getattr(obj, attr)


@c_abi_typed_export("PyObject_GetAttr", "ptr", ("ptr", "ptr"))
def PyObject_GetAttr(obj, attr) -> c_ptr:
    name = PyUnicode_AsUTF8(attr)
    if ptr_is_null(name):
        return null()
    return PyObject_GetAttrString(obj, name)


@c_abi_typed_export("PyObject_SetAttrString", "i32", ("ptr", "ptr", "ptr"))
def PyObject_SetAttrString(obj, attr, value) -> int:
    if ptr_is_null(obj) or ptr_is_null(attr) or ptr_is_null(value):
        _type_error(cstr("invalid PyObject_SetAttrString call"))
        return -1
    if py_obj_setattr(obj, attr, value) == 0:
        return 0
    return -1


@c_abi_typed_export("PyObject_SetAttr", "i32", ("ptr", "ptr", "ptr"))
def PyObject_SetAttr(obj, attr, value) -> int:
    name = PyUnicode_AsUTF8(attr)
    if ptr_is_null(name):
        return -1
    return PyObject_SetAttrString(obj, name, value)


@c_abi_typed_export("PyObject_HasAttrWithError", "i32", ("ptr", "ptr"))
def PyObject_HasAttrWithError(obj, attr) -> int:
    value = PyObject_GetAttr(obj, attr)
    if ptr_is_null(value):
        if _is_attribute_error() != 0:
            py_clear_exception()
            return 0
        if py_err_occurred() != 0:
            return -1
        return 0
    py_decref(value)
    return 1


@c_abi_typed_export("PyObject_HasAttrStringWithError", "i32", ("ptr", "ptr"))
def PyObject_HasAttrStringWithError(obj, attr) -> int:
    value = PyObject_GetAttrString(obj, attr)
    if ptr_is_null(value):
        if _is_attribute_error() != 0:
            py_clear_exception()
            return 0
        if py_err_occurred() != 0:
            return -1
        return 0
    py_decref(value)
    return 1


@c_abi_typed_export("PyObject_HasAttr", "i32", ("ptr", "ptr"))
def PyObject_HasAttr(obj, attr) -> int:
    rc = PyObject_HasAttrWithError(obj, attr)
    if rc < 0:
        py_clear_exception()
        return 0
    return rc


@c_abi_typed_export("PyObject_HasAttrString", "i32", ("ptr", "ptr"))
def PyObject_HasAttrString(obj, attr) -> int:
    rc = PyObject_HasAttrStringWithError(obj, attr)
    if rc < 0:
        py_clear_exception()
        return 0
    return rc


@c_abi_typed_export("PyObject_GetOptionalAttr", "i32", ("ptr", "ptr", "ptr"))
def PyObject_GetOptionalAttr(obj, attr, result_ptr) -> int:
    if ptr_is_null(result_ptr):
        _type_error(cstr("NULL result pointer"))
        return -1
    store_ptr(result_ptr, 0, null())
    value = PyObject_GetAttr(obj, attr)
    if not ptr_is_null(value):
        store_ptr(result_ptr, 0, value)
        return 1
    if _is_attribute_error() != 0:
        py_clear_exception()
        return 0
    if py_err_occurred() != 0:
        return -1
    return 0


@c_abi_typed_export("PyObject_GetOptionalAttrString", "i32", ("ptr", "ptr", "ptr"))
def PyObject_GetOptionalAttrString(obj, attr, result_ptr) -> int:
    if ptr_is_null(result_ptr):
        _type_error(cstr("NULL result pointer"))
        return -1
    store_ptr(result_ptr, 0, null())
    value = PyObject_GetAttrString(obj, attr)
    if not ptr_is_null(value):
        store_ptr(result_ptr, 0, value)
        return 1
    if _is_attribute_error() != 0:
        py_clear_exception()
        return 0
    if py_err_occurred() != 0:
        return -1
    return 0


# --- PyObject_Init / InitVar / IsSubclass ---------------------------

pcc_capi_cext_tag_for = extern("pcc_capi_cext_tag_for", (c_ptr,), c_int32)
PyType_IsSubtype = extern("PyType_IsSubtype", (c_ptr, c_ptr), c_int32)
from pcc.unsafe import store_i64, store_ptr, load_i32, is_tagged_int


@c_abi_typed_export("PyObject_Init", "ptr", ("ptr", "ptr"))
def PyObject_Init(op, type_obj) -> c_ptr:
    if ptr_is_null(op):
        return op
    store_i64(op, 0, 1)  # refcount
    store_i32(op, 8, pcc_capi_cext_tag_for(type_obj))  # type_tag
    store_i32(op, 12, PY_FLAG_GC_MALLOC_ALLOC)
    if pcc_gc_pointer_register(op) < 0:
        return null()
    tracked_size: int = 16
    if ptr_is_null(type_obj) == 0:
        tracked_size = load_i64(type_obj, 40)
    if tracked_size < 16:
        tracked_size = 16
    pcc_gc_note_object_allocated_sized(op, tracked_size)
    store_ptr(op, 16, type_obj)  # ob_type slot
    return op


@c_abi_typed_export("PyObject_InitVar", "ptr", ("ptr", "ptr", "i64"))
def PyObject_InitVar(op, type_obj, size: int) -> c_ptr:
    if ptr_is_null(op):
        return op
    store_i64(op, 0, 1)
    store_i32(op, 8, pcc_capi_cext_tag_for(type_obj))  # type_tag
    store_i32(op, 12, PY_FLAG_GC_MALLOC_ALLOC)
    if pcc_gc_pointer_register(op) < 0:
        return null()
    tracked_size: int = 16
    item_size: int = 0
    if ptr_is_null(type_obj) == 0:
        tracked_size = load_i64(type_obj, 40)
        item_size = load_i64(type_obj, 48)
    if tracked_size < 16:
        tracked_size = 16
    if size > 0 and item_size > 0:
        if size <= (9223372036854775807 - tracked_size) // item_size:
            tracked_size = tracked_size + size * item_size
    pcc_gc_note_object_allocated_sized(op, tracked_size)
    store_i64(op, 24, size)  # ob_size after 16-byte header + ob_type
    return op


@c_abi_typed_export("PyObject_IsSubclass", "i32", ("ptr", "ptr"))
def PyObject_IsSubclass(derived, cls) -> int:
    return PyType_IsSubtype(derived, cls)
