"""pcc-Python owners for the no-libpython sequence-iterator surface.

Replaces the pcc_capi_seqiter type + PySeqIter_New / pcc_capi_is_seqiter /
pcc_capi_seqiter_next block of py_capi_shim.c.  The iterator object is a
40-byte heap struct (header 16 + ob_type 8 + seq@24 + index@32) whose type
is built here.

Owned surface (stable C ABI names):

  PySeqIter_New, pcc_capi_is_seqiter, pcc_capi_seqiter_next,
  pcc_capi_seqiter_dealloc, pcc_capi_seqiter_traverse

Constants:
  Py_TPFLAGS_READY = 0x1000, Py_TPFLAGS_HAVE_GC = 0x2000,
  PCC_TPFLAGS_MANAGED_DEALLOC = 1 << 62
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import PY_TYPE_GEN, PY_TYPE_ITER

from pcc.extern import (
    c_abi_typed_export,
    c_int32,
    c_int64,
    c_ptr,
    c_void,
    extern,
)
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
    ptr_is_null,
    store_i64,
    store_ptr,
)

py_type_of = extern("pcc_py_type_of", (c_ptr,), c_int64)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_clear_exception = extern("py_clear_exception", (), c_void)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_store_ptr = extern("pcc_gc_store_ptr", (c_ptr, c_ptr, c_ptr), c_void)
PyType_GenericAlloc = extern("PyType_GenericAlloc", (c_ptr, c_int64), c_ptr)
PySequence_GetItem = extern("PySequence_GetItem", (c_ptr, c_int64), c_ptr)
pcc_capi_cext_tag_for = extern("pcc_capi_cext_tag_for", (c_ptr,), c_int32)
pcc_capi_visit_slot = extern("pcc_capi_visit_slot", (c_ptr, c_ptr, c_ptr), c_int64)
pcc_capi_cext_object_is_iterator = extern(
    "pcc_capi_cext_object_is_iterator", (c_ptr,), c_int64
)
py_obj_next = extern("py_obj_next", (c_ptr,), c_ptr)
PyErr_ExceptionMatches = extern("PyErr_ExceptionMatches", (c_ptr,), c_int32)
PyErr_Clear = extern("PyErr_Clear", (), c_void)

define_global_i64_array(
    "pcc_capi_seqiter_type",
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0,
)


@c_abi_typed_export("pcc_capi_seqiter_dealloc", "void", ("ptr",))
def pcc_capi_seqiter_dealloc(obj) -> None:
    seq = pcc_gc_load_ptr(obj, ptr_add(obj, 24))
    store_ptr(obj, 24, null())
    py_decref(seq)


@c_abi_typed_export("pcc_capi_seqiter_traverse", "i32", ("ptr", "ptr", "ptr"))
def pcc_capi_seqiter_traverse(obj, visit, arg) -> int:
    return pcc_capi_visit_slot(ptr_add(obj, 24), visit, arg)


def _seqiter_type() -> c_ptr:
    t = global_addr("pcc_capi_seqiter_type")
    if load_i64(t, 392) != 0:
        return t
    store_i64(t, 0, 1)  # refcount
    store_ptr(t, 32, cstr("iterator"))
    store_i64(t, 40, 40)  # tp_basicsize
    store_i64(t, 176, 0x1000 | 0x2000 | 4611686018427387904)
    store_ptr(t, 56, function_addr("pcc_capi_seqiter_dealloc"))
    store_ptr(t, 192, function_addr("pcc_capi_seqiter_traverse"))
    tag: int = pcc_capi_cext_tag_for(t)
    return t


@c_abi_typed_export("pcc_capi_is_seqiter", "i32", ("ptr",))
def pcc_capi_is_seqiter(obj) -> int:
    if ptr_is_null(obj) or is_tagged_int(obj):
        return 0
    type_obj = _seqiter_type()
    expected: int = pcc_capi_cext_tag_for(type_obj)
    if load_i32(obj, 8) == expected:
        return 1
    return 0


@c_abi_typed_export("pcc_capi_seqiter_next", "ptr", ("ptr",))
def pcc_capi_seqiter_next(obj) -> c_ptr:
    seq = pcc_gc_load_ptr(obj, ptr_add(obj, 24))
    index: int = load_i64(obj, 32)
    item = PySequence_GetItem(seq, index)
    if ptr_is_null(item):
        if py_err_occurred() != 0:
            py_clear_exception()
        return null()
    store_i64(obj, 32, index + 1)
    return item


@c_abi_typed_export("PyIter_Check", "i32", ("ptr",))
def PyIter_Check(obj) -> int:
    if ptr_is_null(obj) or is_tagged_int(obj):
        return 0
    tag: int = py_type_of(obj)
    if tag == PY_TYPE_ITER or tag == PY_TYPE_GEN:  # PY_TYPE_ITER / PY_TYPE_GEN
        return 1
    if pcc_capi_cext_object_is_iterator(obj) != 0:
        return 1
    return 0


@c_abi_typed_export("PyIter_Next", "ptr", ("ptr",))
def PyIter_Next(obj) -> c_ptr:
    if ptr_is_null(obj):
        py_raise_owned(py_exc_new(3, cstr("NULL object is not an iterator")))
        return null()
    if pcc_capi_is_seqiter(obj) != 0:
        return pcc_capi_seqiter_next(obj)
    item = py_obj_next(obj)
    if ptr_is_null(item) and PyErr_ExceptionMatches(
        global_load_ptr("PyExc_StopIteration")
    ) != 0:
        PyErr_Clear()
    return item


@c_abi_typed_export("PyIter_NextItem", "i32", ("ptr", "ptr"))
def PyIter_NextItem(iter_obj, item_out) -> int:
    if ptr_is_null(item_out):
        py_raise_owned(py_exc_new(7, cstr("NULL result pointer")))
        return -1
    store_ptr(item_out, 0, null())
    if ptr_is_null(iter_obj) or PyIter_Check(iter_obj) == 0:
        py_raise_owned(py_exc_new(3, cstr("expected an iterator")))
        return -1
    item = PyIter_Next(iter_obj)
    store_ptr(item_out, 0, item)
    if not ptr_is_null(item):
        return 1
    if py_err_occurred() == 0:
        return 0
    return -1


@c_abi_typed_export("PySeqIter_New", "ptr", ("ptr",))
def PySeqIter_New(seq) -> c_ptr:
    if ptr_is_null(seq):
        py_raise_owned(py_exc_new(3, cstr("iteration over a non-sequence")))
        return null()
    type_obj = _seqiter_type()
    obj = PyType_GenericAlloc(type_obj, 0)
    if ptr_is_null(obj):
        return null()
    pcc_gc_store_ptr(obj, ptr_add(obj, 24), seq)
    return obj
