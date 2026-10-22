"""pcc-Python owners for the no-libpython slice surface.

Replaces the pcc_capi_slice type + PySlice_New / PySlice_GetIndicesEx block of
py_capi_shim.c.  The slice object is a 48-byte heap struct
(header 16 + ob_type 8 + start@24 + stop@32 + step@40) whose type is built
here via define_global_* words; the dealloc/traverse callbacks are exported
pcc-Python functions.

_typeobject layout offsets used: tp_name@32, tp_basicsize@40, tp_dealloc@56,
tp_traverse@192, tp_flags@176, tp_version_tag@392.

Owned surface (stable C ABI names):

  PySlice_New, PySlice_GetIndicesEx, pcc_capi_slice_dealloc,
  pcc_capi_slice_traverse

Constants:
  Py_TPFLAGS_READY = 0x1000, Py_TPFLAGS_HAVE_GC = 0x2000,
  PCC_TPFLAGS_MANAGED_DEALLOC = 1 << 62
"""

__pcc_runtime_port__ = True

from pcc.extern import c_abi_typed_export, c_int32, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    cstr,
    define_global_i64_array,
    function_addr,
    global_addr,
    global_load_ptr,
    is_tagged_int,
    load_i32,
    load_i64,
    load_ptr,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    store_i64,
    store_ptr,
)

py_obj_is_slice = extern("py_obj_is_slice", (c_ptr,), c_int64)
py_obj_getattr = extern("py_obj_getattr", (c_ptr, c_ptr), c_ptr)
py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_err_occurred = extern("py_err_occurred", (), c_int64)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_store_ptr = extern("pcc_gc_store_ptr", (c_ptr, c_ptr, c_ptr), c_void)
PyType_GenericAlloc = extern("PyType_GenericAlloc", (c_ptr, c_int64), c_ptr)
PyLong_AsLong = extern("PyLong_AsLong", (c_ptr,), c_int64)
pcc_capi_cext_tag_for = extern("pcc_capi_cext_tag_for", (c_ptr,), c_int32)
pcc_capi_visit_slot = extern("pcc_capi_visit_slot", (c_ptr, c_ptr, c_ptr), c_int64)
PySlice_AdjustIndices = extern("PySlice_AdjustIndices", (c_int64, c_ptr, c_ptr, c_int64), c_int64)


def _value_error(message) -> None:
    py_raise_owned(py_exc_new(2, message))  # PY_EXC_VALUEERROR


@c_abi_typed_export("pcc_capi_slice_dealloc", "void", ("ptr",))
def pcc_capi_slice_dealloc(obj) -> None:
    start = pcc_gc_load_ptr(obj, ptr_add(obj, 24))
    stop = pcc_gc_load_ptr(obj, ptr_add(obj, 32))
    step = pcc_gc_load_ptr(obj, ptr_add(obj, 40))
    store_ptr(obj, 24, null())
    store_ptr(obj, 32, null())
    store_ptr(obj, 40, null())
    py_decref(start)
    py_decref(stop)
    py_decref(step)


@c_abi_typed_export("pcc_capi_slice_traverse", "i32", ("ptr", "ptr", "ptr"))
def pcc_capi_slice_traverse(obj, visit, arg) -> int:
    result = pcc_capi_visit_slot(ptr_add(obj, 24), visit, arg)
    if result != 0:
        return result
    result = pcc_capi_visit_slot(ptr_add(obj, 32), visit, arg)
    if result != 0:
        return result
    return pcc_capi_visit_slot(ptr_add(obj, 40), visit, arg)


# --- the slice type object (48 bytes basicsize) ----------------------
define_global_i64_array("pcc_capi_slice_obj_type", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                        0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                        0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
                        0, 0, 0, 0, 0)
# tp_name@32 (ptr), tp_basicsize@40 (i64), tp_flags@176 (i64),
# tp_dealloc@56 (ptr), tp_traverse@192 (ptr), tp_version_tag@392 (i64)
# ob_base: refcount@0=1, type_tag@8=0, ob_size@16=0; ob_type@24 = NULL (set
# when registered via cext tag)

def _slice_type() -> c_ptr:
    t = global_addr("pcc_capi_slice_obj_type")
    if load_i64(t, 392) != 0:  # tp_version_tag already stamped by Ready
        return t
    store_i64(t, 0, 1)  # refcount
    store_ptr(t, 32, cstr("slice"))
    store_i64(t, 40, 48)  # tp_basicsize
    store_i64(
        t, 176, 0x1000 | 0x2000 | 4611686018427387904
    )  # READY|HAVE_GC|MANAGED_DEALLOC
    store_ptr(t, 56, function_addr("pcc_capi_slice_dealloc"))
    store_ptr(t, 192, function_addr("pcc_capi_slice_traverse"))
    # Register: stamp the cext tag so GenericAlloc / dealloc dispatch work.
    tag: int = pcc_capi_cext_tag_for(t)
    return t


@c_abi_typed_export("PySlice_New", "ptr", ("ptr", "ptr", "ptr"))
def PySlice_New(start, stop, step) -> c_ptr:
    type_obj = _slice_type()
    obj = PyType_GenericAlloc(type_obj, 0)
    if ptr_is_null(obj):
        return null()
    if ptr_is_null(start):
        start = global_load_ptr("py_None")
    if ptr_is_null(stop):
        stop = global_load_ptr("py_None")
    if ptr_is_null(step):
        step = global_load_ptr("py_None")
    pcc_gc_store_ptr(obj, ptr_add(obj, 24), start)
    pcc_gc_store_ptr(obj, ptr_add(obj, 32), stop)
    pcc_gc_store_ptr(obj, ptr_add(obj, 40), step)
    return obj


@c_abi_typed_export("PySlice_GetIndicesEx", "i32", ("ptr", "i64", "ptr", "ptr", "ptr", "ptr"))
def PySlice_GetIndicesEx(r, length: int, start_out, stop_out, step_out, len_out) -> int:
    native_slice = py_obj_is_slice(r) != 0
    if native_slice:
        start_obj = py_obj_getattr(r, cstr("start"))
        stop_obj = py_obj_getattr(r, cstr("stop"))
        step_obj = py_obj_getattr(r, cstr("step"))
        if ptr_is_null(start_obj) or ptr_is_null(stop_obj) or ptr_is_null(step_obj):
            if not ptr_is_null(start_obj):
                py_decref(start_obj)
            if not ptr_is_null(stop_obj):
                py_decref(stop_obj)
            if not ptr_is_null(step_obj):
                py_decref(step_obj)
            return -1
    else:
        start_obj = pcc_gc_load_ptr(r, ptr_add(r, 24))
        stop_obj = pcc_gc_load_ptr(r, ptr_add(r, 32))
        step_obj = pcc_gc_load_ptr(r, ptr_add(r, 40))
    if ptr_eq(step_obj, global_load_ptr("py_None")):
        stp: int = 1
    else:
        stp = PyLong_AsLong(step_obj)
        if py_err_occurred() != 0:
            return -1
        if stp == 0:
            _value_error(cstr("slice step cannot be zero"))
            if native_slice:
                py_decref(start_obj)
                py_decref(stop_obj)
                py_decref(step_obj)
            return -1
    neg = 0
    if stp < 0:
        neg = 1
    lower: int = 0
    upper: int = length
    if neg != 0:
        lower = -1
        upper = length - 1
    if ptr_eq(start_obj, global_load_ptr("py_None")):
        if neg != 0:
            st = upper
        else:
            st = lower
    else:
        st = PyLong_AsLong(start_obj)
        if py_err_occurred() != 0:
            return -1
        if st < 0:
            st += length
        if st < lower:
            st = lower
        if st > upper:
            st = upper
    if ptr_eq(stop_obj, global_load_ptr("py_None")):
        if neg != 0:
            sp = lower
        else:
            sp = upper
    else:
        sp = PyLong_AsLong(stop_obj)
        if py_err_occurred() != 0:
            return -1
        if sp < 0:
            sp += length
        if sp < lower:
            sp = lower
        if sp > upper:
            sp = upper
    store_i64(start_out, 0, st)
    store_i64(stop_out, 0, sp)
    store_i64(step_out, 0, stp)
    if neg != 0:
        if sp < st:
            slicelen = _c_trunc_div(st - sp - 1, -stp) + 1
        else:
            slicelen = 0
    else:
        if st < sp:
            slicelen = _c_trunc_div(sp - st - 1, stp) + 1
        else:
            slicelen = 0
    store_i64(len_out, 0, slicelen)
    if native_slice:
        py_decref(start_obj)
        py_decref(stop_obj)
        py_decref(step_obj)
    return 0


def _c_trunc_div(a: int, b: int) -> int:
    if a >= 0:
        return a // b
    return -((-a) // b)
