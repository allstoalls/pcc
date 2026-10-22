"""pcc-Python owners for the no-libpython C-API PyCFunction accessors.

Replaces the PyCFunction_GetSelf / GetFlags / GetFunction block of
py_capi_shim.c.  Reads the pcc PyFuncObject layout (header@0, capi_method@16,
capi_self@24, entry@56, self_obj@80) and the fake-libc PyMethodDef layout
(ml_name@0, ml_meth@8, ml_flags@16, ml_doc@24) plus the C-extension
PyCFunctionObject prefix (m_ml@16, m_self@24).

Owned surface (stable C ABI names):

  PyCFunction_GetFunction, PyCFunction_GetSelf, PyCFunction_GetFlags

Public object type tags come from the generated ``py_abi_constants`` module.
Private method flags remain owned by the C-function contract:
  METH_VARARGS = 0x0001
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import PY_TYPE_FUNC

from pcc.extern import c_abi_typed_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    is_tagged_int,
    load_i32,
    load_ptr,
    null,
    ptr_is_null,
)


def _func_method_def(op) -> c_ptr:
    if ptr_is_null(op) or is_tagged_int(op) or load_i32(op, 8) != PY_TYPE_FUNC:
        return null()
    return load_ptr(op, 16)  # capi_method


@c_abi_typed_export("PyCFunction_GetFunction", "ptr", ("ptr",))
def PyCFunction_GetFunction(op) -> c_ptr:
    if ptr_is_null(op):
        return null()
    if not is_tagged_int(op) and load_i32(op, 8) == PY_TYPE_FUNC:  # PY_TYPE_FUNC
        method = _func_method_def(op)
        if not ptr_is_null(method):
            return load_ptr(method, 8)  # ml_meth
        return load_ptr(op, 56)  # entry
    m_ml = load_ptr(op, 16)
    if ptr_is_null(m_ml):
        return null()
    return load_ptr(m_ml, 8)  # ml_meth


@c_abi_typed_export("PyCFunction_GetSelf", "ptr", ("ptr",))
def PyCFunction_GetSelf(op) -> c_ptr:
    if not ptr_is_null(op) and not is_tagged_int(op) and load_i32(op, 8) == PY_TYPE_FUNC:
        method = _func_method_def(op)
        if not ptr_is_null(method):
            return load_ptr(op, 24)  # capi_self
        return load_ptr(op, 80)  # self_obj
    if ptr_is_null(op):
        return null()
    return load_ptr(op, 24)  # m_self


@c_abi_typed_export("PyCFunction_GetFlags", "i32", ("ptr",))
def PyCFunction_GetFlags(op) -> int:
    if ptr_is_null(op):
        return -1
    if not is_tagged_int(op) and load_i32(op, 8) == PY_TYPE_FUNC:  # PY_TYPE_FUNC
        method = _func_method_def(op)
        if not ptr_is_null(method):
            return load_i32(method, 16)  # ml_flags
        return 0x0001
    m_ml = load_ptr(op, 16)
    if ptr_is_null(m_ml):
        return 0
    return load_i32(m_ml, 16)  # ml_flags
