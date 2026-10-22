"""pcc-Python owners for the no-libpython PyModule attribute surface.

Replaces the PyModule_GetDict / PyModule_Add* block of py_capi_shim.c.
PyModule_Create2 / PyModule_GetState stay C-side (module-state registry is
GC-integration infrastructure); PyModuleDef_Init stays C-side (C static
moduledef marker).

Owned surface (stable C ABI names):

  PyModule_GetDict, PyModule_AddObject, PyModule_AddObjectRef, PyModule_Add,
  PyModule_AddIntConstant, PyModule_AddStringConstant

Constants (inlined per the pcc-Python runtime-module contract):
  PY_EXC_TYPEERROR = 3, PY_EXC_RUNTIMEERROR = 6, PY_EXC_SYSTEMERROR = 9
"""

__pcc_runtime_port__ = True

from pcc.extern import c_abi_typed_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    cstr,
    define_global_i32,
    global_addr,
    load_ptr,
    null,
    ptr_eq,
    ptr_is_null,
    store_ptr,
)

py_obj_getattr = extern("py_obj_getattr", (c_ptr, c_ptr), c_ptr)
py_obj_setattr = extern("py_obj_setattr", (c_ptr, c_ptr, c_ptr), c_int64)
py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
PyLong_FromLong = extern("PyLong_FromLong", (c_int64,), c_ptr)
PyUnicode_FromString = extern("PyUnicode_FromString", (c_ptr,), c_ptr)


def _type_error(message) -> None:
    py_raise_owned(py_exc_new(3, message))  # PY_EXC_TYPEERROR


def _runtime_error(message) -> None:
    py_raise_owned(py_exc_new(7, message))  # PY_EXC_RUNTIMEERROR (6 is AttributeError)


def _system_error(message) -> None:
    py_raise_owned(py_exc_new(7, message))  # PY_EXC_SYSTEMERROR


@c_abi_typed_export("PyModule_GetDict", "ptr", ("ptr",))
def PyModule_GetDict(module) -> c_ptr:
    if ptr_is_null(module):
        _type_error(cstr("NULL module"))
        return null()
    dict_obj = py_obj_getattr(module, cstr("__dict__"))
    if ptr_is_null(dict_obj):
        _type_error(cstr("expected module object"))
        return null()
    # py_obj_getattr returns owned; CPython PyModule_GetDict returns borrowed.
    py_decref(dict_obj)
    return dict_obj


@c_abi_typed_export("PyModule_AddObject", "i32", ("ptr", "ptr", "ptr"))
def PyModule_AddObject(module, name, value) -> int:
    if ptr_is_null(module) or ptr_is_null(name) or ptr_is_null(value):
        _runtime_error(cstr("invalid PyModule_AddObject call"))
        return -1
    rc = py_obj_setattr(module, name, value)
    if rc != 0:
        return -1
    py_decref(value)
    return 0


@c_abi_typed_export("PyModule_AddObjectRef", "i32", ("ptr", "ptr", "ptr"))
def PyModule_AddObjectRef(module, name, value) -> int:
    if ptr_is_null(value):
        if py_err_occurred() == 0:
            _system_error(
                cstr("PyModule_AddObjectRef must be called with an exception raised if value is NULL")
            )
        return -1
    py_incref(value)
    rc = PyModule_AddObject(module, name, value)
    if rc != 0:
        py_decref(value)
    return rc


@c_abi_typed_export("PyModule_Add", "i32", ("ptr", "ptr", "ptr"))
def PyModule_Add(module, name, value) -> int:
    rc = PyModule_AddObjectRef(module, name, value)
    if not ptr_is_null(value):
        py_decref(value)
    return rc


@c_abi_typed_export("PyModule_AddIntConstant", "i32", ("ptr", "ptr", "i64"))
def PyModule_AddIntConstant(module, name, value: int) -> int:
    obj = PyLong_FromLong(value)
    if ptr_is_null(obj):
        return -1
    return PyModule_AddObject(module, name, obj)


@c_abi_typed_export("PyModule_AddStringConstant", "i32", ("ptr", "ptr", "ptr"))
def PyModule_AddStringConstant(module, name, value) -> int:
    if ptr_is_null(value):
        value = cstr("")
    obj = PyUnicode_FromString(value)
    if ptr_is_null(obj):
        return -1
    return PyModule_AddObject(module, name, obj)


# --- PyModuleDef_Init / pcc_capi_is_moduledef ------------------------

define_global_i32("pcc_capi_moduledef_marker", 0)


@c_abi_typed_export("PyModuleDef_Init", "ptr", ("ptr",))
def PyModuleDef_Init(def_obj) -> c_ptr:
    if ptr_is_null(def_obj):
        return null()
    store_ptr(def_obj, 0, global_addr("pcc_capi_moduledef_marker"))
    return def_obj


@c_abi_typed_export("pcc_capi_is_moduledef", "i32", ("ptr",))
def pcc_capi_is_moduledef(o) -> int:
    if ptr_is_null(o):
        return 0
    if ptr_eq(load_ptr(o, 0), global_addr("pcc_capi_moduledef_marker")):
        return 1
    return 0
