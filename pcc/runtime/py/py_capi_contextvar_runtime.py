"""pcc-Python owners for the no-libpython ContextVar surface.

Replaces the pcc_capi_contextvar type + PyContextVar_New/Get/Set + the
get/set/reset method callbacks block of py_capi_shim.c.  The var object is a
48-byte heap struct (header 16 + ob_type 8 + name@24 + def@32 + value@40).

Owned surface (stable C ABI names):

  PyContextVar_New, PyContextVar_Get, PyContextVar_Set,
  pcc_capi_contextvar_get_method, pcc_capi_contextvar_set_method,
  pcc_capi_contextvar_reset_method, pcc_capi_contextvar_dealloc,
  pcc_capi_contextvar_traverse

Constants:
  Py_TPFLAGS_READY = 0x1000, Py_TPFLAGS_HAVE_GC = 0x2000,
  PCC_TPFLAGS_MANAGED_DEALLOC = 1 << 62
  METH_VARARGS = 0x0001, METH_O = 0x0008
"""

__pcc_runtime_port__ = True

from pcc.extern import c_abi_typed_export, c_int32, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    cstr,
    define_global_i64_array,
    function_addr,
    global_addr,
    global_load_ptr,
    load_i64,
    load_ptr,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    stack_alloc,
    store_i64,
    store_ptr,
)

py_decref = extern("py_decref", (c_ptr,), c_void)
py_incref = extern("py_incref", (c_ptr,), c_void)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_store_ptr = extern("pcc_gc_store_ptr", (c_ptr, c_ptr, c_ptr), c_void)
pcc_gc_note_slot_write_barrier = extern(
    "pcc_gc_note_slot_write_barrier", (c_ptr, c_ptr, c_ptr), c_void
)
PyType_GenericAlloc = extern("PyType_GenericAlloc", (c_ptr, c_int64), c_ptr)
PyTuple_New = extern("PyTuple_New", (c_int64,), c_ptr)
PyTuple_SetItem = extern("PyTuple_SetItem", (c_ptr, c_int64, c_ptr), c_int64)
PyTuple_Size = extern("PyTuple_Size", (c_ptr,), c_int64)
PyTuple_GetItem = extern("PyTuple_GetItem", (c_ptr, c_int64), c_ptr)
PyBool_FromLong = extern("PyBool_FromLong", (c_int64,), c_ptr)
PyObject_IsTrue = extern("PyObject_IsTrue", (c_ptr,), c_int64)
pcc_capi_cext_tag_for = extern("pcc_capi_cext_tag_for", (c_ptr,), c_int32)
pcc_capi_visit_slot = extern("pcc_capi_visit_slot", (c_ptr, c_ptr, c_ptr), c_int64)

define_global_i64_array(
    "pcc_capi_contextvar_type",
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0,
)
# PyMethodDef table: 4 entries x 32 bytes (name@0, meth@8, flags@16, doc@24)
define_global_i64_array(
    "pcc_capi_contextvar_methods",
    0, 0, 0, 0,
    0, 0, 0, 0,
    0, 0, 0, 0,
    0, 0, 0, 0,
)


def _type_error(message) -> None:
    py_raise_owned(py_exc_new(3, message))  # PY_EXC_TYPEERROR


def _value_error(message) -> None:
    py_raise_owned(py_exc_new(2, message))  # PY_EXC_VALUEERROR


def _lookup_error(message) -> None:
    py_raise_owned(py_exc_new(13, message))  # PY_EXC_LOOKUPERROR


# NOTE: never wrap stack_alloc in a helper that returns it -- the allocation
# lives in the helper's own frame and is dangling after return.


@c_abi_typed_export("pcc_capi_contextvar_dealloc", "void", ("ptr",))
def pcc_capi_contextvar_dealloc(obj) -> None:
    def_obj = pcc_gc_load_ptr(obj, ptr_add(obj, 32))
    value = pcc_gc_load_ptr(obj, ptr_add(obj, 40))
    store_ptr(obj, 32, null())
    store_ptr(obj, 40, null())
    py_decref(def_obj)
    py_decref(value)


@c_abi_typed_export("pcc_capi_contextvar_traverse", "i32", ("ptr", "ptr", "ptr"))
def pcc_capi_contextvar_traverse(obj, visit, arg) -> int:
    result = pcc_capi_visit_slot(ptr_add(obj, 32), visit, arg)
    if result != 0:
        return result
    return pcc_capi_visit_slot(ptr_add(obj, 40), visit, arg)



@c_abi_typed_export("pcc_capi_contextvar_get_method", "ptr", ("ptr", "ptr"))
def pcc_capi_contextvar_get_method(self, args) -> c_ptr:
    nargs = PyTuple_Size(args)
    if nargs < 0:
        return null()
    if nargs > 1:
        _type_error(cstr("ContextVar.get expected at most 1 argument"))
        return null()
    if nargs == 1:
        default_value = PyTuple_GetItem(args, 0)
    else:
        default_value = null()
    value_ptr = stack_alloc(8)
    value = null()
    store_ptr(value_ptr, 0, null())
    if PyContextVar_Get(self, default_value, value_ptr) != 0:
        return null()
    value = load_ptr(value_ptr, 0)
    if ptr_is_null(value):
        _lookup_error(cstr("ContextVar has no value"))
        return null()
    return value


@c_abi_typed_export("pcc_capi_contextvar_set_method", "ptr", ("ptr", "ptr"))
def pcc_capi_contextvar_set_method(self, value) -> c_ptr:
    return PyContextVar_Set(self, value)


@c_abi_typed_export("pcc_capi_contextvar_reset_method", "ptr", ("ptr", "ptr"))
def pcc_capi_contextvar_reset_method(self, token) -> c_ptr:
    if ptr_is_null(token) or PyTuple_Size(token) != 3:
        _type_error(cstr("ContextVar.reset expected a token"))
        return null()
    token_var = PyTuple_GetItem(token, 0)
    had_value = PyTuple_GetItem(token, 1)
    previous = PyTuple_GetItem(token, 2)
    if not ptr_eq(token_var, self) or ptr_is_null(had_value) or ptr_is_null(previous):
        _value_error(cstr("Token was created by a different ContextVar"))
        return null()
    if PyObject_IsTrue(had_value) != 0:
        pcc_gc_store_ptr(self, ptr_add(self, 40), previous)
    else:
        store_ptr(self, 40, null())
        pcc_gc_store_ptr(self, ptr_add(self, 40), null())
    py_incref(global_load_ptr("py_None"))
    return global_load_ptr("py_None")

def _fill_methods() -> None:
    m = global_addr("pcc_capi_contextvar_methods")
    store_ptr(m, 0, cstr("get"))
    store_ptr(m, 8, function_addr("pcc_capi_contextvar_get_method"))
    store_i64(m, 16, 1)
    store_ptr(m, 32, cstr("set"))
    store_ptr(m, 40, function_addr("pcc_capi_contextvar_set_method"))
    store_i64(m, 48, 8)
    store_ptr(m, 64, cstr("reset"))
    store_ptr(m, 72, function_addr("pcc_capi_contextvar_reset_method"))
    store_i64(m, 80, 8)
    t = global_addr("pcc_capi_contextvar_type")
    store_ptr(t, 240, m)  # tp_methods


def _contextvar_type() -> c_ptr:
    t = global_addr("pcc_capi_contextvar_type")
    if load_i64(t, 392) != 0:
        return t
    _fill_methods()
    store_i64(t, 0, 1)  # refcount
    store_ptr(t, 32, cstr("ContextVar"))
    store_i64(t, 40, 48)  # tp_basicsize
    store_i64(t, 176, 0x1000 | 0x2000 | 4611686018427387904)
    store_ptr(t, 56, function_addr("pcc_capi_contextvar_dealloc"))
    store_ptr(t, 192, function_addr("pcc_capi_contextvar_traverse"))
    tag: int = pcc_capi_cext_tag_for(t)
    return t


@c_abi_typed_export("PyContextVar_New", "ptr", ("ptr", "ptr"))
def PyContextVar_New(name, def_obj) -> c_ptr:
    type_obj = _contextvar_type()
    obj = PyType_GenericAlloc(type_obj, 0)
    if ptr_is_null(obj):
        return null()
    store_ptr(obj, 24, name)  # cv->name (borrowed)
    pcc_gc_store_ptr(obj, ptr_add(obj, 32), def_obj)
    return obj


@c_abi_typed_export("PyContextVar_Get", "i32", ("ptr", "ptr", "ptr"))
def PyContextVar_Get(var, default_value, value_ptr) -> int:
    if ptr_is_null(var) or ptr_is_null(value_ptr):
        return -1
    res = pcc_gc_load_ptr(var, ptr_add(var, 40))  # value
    if ptr_is_null(res):
        res = default_value
    if ptr_is_null(res):
        res = pcc_gc_load_ptr(var, ptr_add(var, 32))  # def
    if not ptr_is_null(res):
        py_incref(res)
    store_ptr(value_ptr, 0, res)
    return 0


@c_abi_typed_export("PyContextVar_Set", "ptr", ("ptr", "ptr"))
def PyContextVar_Set(var, value) -> c_ptr:
    if ptr_is_null(var):
        _type_error(cstr("ContextVar required"))
        return null()
    prev = pcc_gc_load_ptr(var, ptr_add(var, 40))
    if not ptr_is_null(value):
        py_incref(value)
    store_ptr(var, 40, value)
    pcc_gc_note_slot_write_barrier(var, ptr_add(var, 40), value)
    tok = PyTuple_New(3)
    if ptr_is_null(tok):
        if not ptr_is_null(value):
            py_decref(value)
        store_ptr(var, 40, prev)
        pcc_gc_note_slot_write_barrier(var, ptr_add(var, 40), prev)
        return null()
    py_incref(var)
    PyTuple_SetItem(tok, 0, var)
    had_value = PyBool_FromLong(1 if not ptr_is_null(prev) else 0)
    if ptr_is_null(had_value) or PyTuple_SetItem(tok, 1, had_value) != 0:
        if not ptr_is_null(had_value):
            py_decref(had_value)
        py_decref(tok)
        return null()
    if ptr_is_null(prev):
        py_incref(global_load_ptr("py_None"))
        PyTuple_SetItem(tok, 2, global_load_ptr("py_None"))
    else:
        PyTuple_SetItem(tok, 2, prev)
    return tok
