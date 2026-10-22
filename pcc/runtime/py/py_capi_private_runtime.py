"""pcc-Python owners for the CPython private link-readiness helpers.

Replaces the _PyObject_New / _PyObject_NewVar / _PyObject_GC_New /
_PyDict_GetItem_KnownHash block of py_capi_shim.c.  These are thin wrappers
over already-migrated C-API surfaces.

Owned surface (stable C ABI names):

  _PyObject_New, _PyObject_NewVar, _PyObject_GC_New, _PyDict_GetItem_KnownHash
"""

__pcc_runtime_port__ = True

from pcc.extern import c_abi_typed_export, c_int64, c_ptr, extern

PyType_GenericAlloc = extern("PyType_GenericAlloc", (c_ptr, c_int64), c_ptr)
PyDict_GetItem = extern("PyDict_GetItem", (c_ptr, c_ptr), c_ptr)


@c_abi_typed_export("_PyObject_New", "ptr", ("ptr",))
def _PyObject_New(type_obj) -> c_ptr:
    return PyType_GenericAlloc(type_obj, 0)


@c_abi_typed_export("_PyObject_NewVar", "ptr", ("ptr", "i64"))
def _PyObject_NewVar(type_obj, nitems: int) -> c_ptr:
    return PyType_GenericAlloc(type_obj, nitems)


@c_abi_typed_export("_PyObject_GC_New", "ptr", ("ptr",))
def _PyObject_GC_New(type_obj) -> c_ptr:
    return PyType_GenericAlloc(type_obj, 0)


@c_abi_typed_export("_PyDict_GetItem_KnownHash", "ptr", ("ptr", "ptr", "i64"))
def _PyDict_GetItem_KnownHash(mp, key, hash_val: int) -> c_ptr:
    return PyDict_GetItem(mp, key)
