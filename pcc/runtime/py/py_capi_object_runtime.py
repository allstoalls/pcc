"""pcc-Python owners for the no-libpython C-API object basics.

Replaces the simple PyObject_* block of py_capi_shim.c: type, truth, str,
repr, bytes, format, hash, size, item access, iteration, rich comparison,
isinstance, weakref invalidation, GC-track no-ops, file-descriptor coercion,
and length hinting.  Every function delegates to the existing pcc-Python
object ABIs (py_obj_*, py_type_builtin, py_weakref_invalidate,
py_user_len_dispatch) or to pcc-Python-owned C-API siblings
(PyBool_FromLong, PyLong_Check/AsLong, PyObject_Free).

Not yet moved (kept C-side, each a later bounded slice):
  PyObject_GetAttr/SetAttr/HasAttr (needs the retained
    pcc_capi_builtin_object_getattr list.sort method bridge),
  PyObject_IsSubclass / PyType_IsSubtype (cext type-object machinery),
  PyObject_CheckBuffer / PyObject_GetBuffer (buffer-slice),
  PyObject_Print (FILE* stdio wiring),
  PyObject_GetBuffer / vectorcall / call machinery,
  PyObject_GenericGetAttr / GenericSetAttr / GenericGetDict,
  PyObject_Call* family.

Public object type tags come from the generated ``py_abi_constants`` module.
Private exception codes remain owned by this C-API contract:
  PY_EXC_TYPEERROR=3 KEYERROR=4 VALUEERROR=2 OSError=14
  Py_LT=0 LE=1 EQ=2 NE=3 GT=4 GE=5
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import PY_TYPE_BYTEARRAY, PY_TYPE_BYTES, PY_TYPE_DICT, PY_TYPE_INSTANCE, PY_TYPE_LIST, PY_TYPE_MEMORYVIEW, PY_TYPE_SET, PY_TYPE_STR, PY_TYPE_TUPLE, PY_TYPE_USER_CLASS_START

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
    is_tagged_int,
    load_i32,
    load_i64,
    null,
    ptr_is_null,
    stack_alloc,
    store_i64,
)

py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_type_builtin = extern("py_type_builtin", (c_ptr,), c_ptr)
py_obj_truthy = extern("py_obj_truthy", (c_ptr,), c_int64)
py_obj_str = extern("py_obj_str", (c_ptr,), c_ptr)
py_obj_repr = extern("py_obj_repr", (c_ptr,), c_ptr)
py_bytes_from_obj = extern("py_bytes_from_obj", (c_ptr,), c_ptr)
py_obj_format = extern("py_obj_format", (c_ptr, c_ptr), c_ptr)
py_obj_hash = extern("py_obj_hash", (c_ptr,), c_int64)
py_obj_len = extern("py_obj_len", (c_ptr,), c_int64)
py_obj_getitem = extern("py_obj_getitem", (c_ptr, c_ptr), c_ptr)
py_obj_setitem = extern("py_obj_setitem", (c_ptr, c_ptr, c_ptr), c_int64)
py_obj_delitem = extern("py_obj_delitem", (c_ptr, c_ptr), c_int64)
py_obj_iter = extern("py_obj_iter", (c_ptr,), c_ptr)
py_obj_eq = extern("py_obj_eq", (c_ptr, c_ptr), c_int64)
py_obj_eq_value = extern("py_obj_eq_value", (c_ptr, c_ptr), c_int64)
py_obj_lt = extern("py_obj_lt", (c_ptr, c_ptr), c_int64)
py_obj_le = extern("py_obj_le", (c_ptr, c_ptr), c_int64)
py_obj_gt = extern("py_obj_gt", (c_ptr, c_ptr), c_int64)
py_obj_ge = extern("py_obj_ge", (c_ptr, c_ptr), c_int64)
py_obj_isinstance = extern("py_obj_isinstance", (c_ptr, c_ptr), c_int64)
py_weakref_invalidate = extern("py_weakref_invalidate", (c_ptr,), c_void)
py_user_len_dispatch = extern("py_user_len_dispatch", (c_ptr, c_ptr), c_int64)
PyBool_FromLong = extern("PyBool_FromLong", (c_int64,), c_ptr)
PyLong_Check = extern("PyLong_Check", (c_ptr,), c_int32)
PyLong_AsLong = extern("PyLong_AsLong", (c_ptr,), c_int64)
PyObject_Free = extern("PyObject_Free", (c_ptr,), c_void)


def _type_error(message) -> None:
    py_raise_owned(py_exc_new(3, message))  # PY_EXC_TYPEERROR


@c_abi_typed_export("PyObject_Type", "ptr", ("ptr",))
def PyObject_Type(obj):
    if ptr_is_null(obj):
        _type_error(cstr("NULL object"))
        return null()
    return py_type_builtin(obj)


@c_abi_typed_export("PyObject_IsTrue", "i32", ("ptr",))
def PyObject_IsTrue(obj) -> int:
    truth: int = py_obj_truthy(obj)
    if py_err_occurred() != 0:
        return -1
    if truth != 0:
        return 1
    return 0


@c_abi_typed_export("PyObject_Not", "i32", ("ptr",))
def PyObject_Not(obj) -> int:
    truth: int = PyObject_IsTrue(obj)
    if truth < 0:
        return -1
    if truth != 0:
        return 0
    return 1


@c_abi_typed_export("PyObject_Str", "ptr", ("ptr",))
def PyObject_Str(obj):
    out = py_obj_str(obj)
    if ptr_is_null(out) and py_err_occurred() == 0:
        _type_error(cstr("object cannot be converted to str"))
    return out


@c_abi_typed_export("PyObject_Repr", "ptr", ("ptr",))
def PyObject_Repr(obj):
    out = py_obj_repr(obj)
    if ptr_is_null(out) and py_err_occurred() == 0:
        _type_error(cstr("object cannot be converted to repr"))
    return out


@c_abi_typed_export("PyObject_Bytes", "ptr", ("ptr",))
def PyObject_Bytes(obj):
    out = py_bytes_from_obj(obj)
    if ptr_is_null(out) and py_err_occurred() == 0:
        _type_error(cstr("object cannot be converted to bytes"))
    return out


@c_abi_typed_export("PyObject_Format", "ptr", ("ptr", "ptr"))
def PyObject_Format(obj, format_spec):
    out = py_obj_format(obj, format_spec)
    if ptr_is_null(out) and py_err_occurred() == 0:
        py_raise_owned(py_exc_new(2, cstr("object cannot be formatted")))  # ValueError
    return out


@c_abi_typed_export("PyObject_Hash", "i64", ("ptr",))
def PyObject_Hash(obj) -> int:
    if ptr_is_null(obj):
        _type_error(cstr("NULL object"))
        return -1
    return py_obj_hash(obj)


@c_abi_typed_export("PyObject_Size", "i64", ("ptr",))
def PyObject_Size(obj) -> int:
    if ptr_is_null(obj):
        _type_error(cstr("NULL object"))
        return -1
    n: int = py_obj_len(obj)
    if py_err_occurred() != 0:
        return -1
    return n


@c_abi_typed_export("PyObject_Length", "i64", ("ptr",))
def PyObject_Length(obj) -> int:
    return PyObject_Size(obj)


@c_abi_typed_export("PyObject_GetItem", "ptr", ("ptr", "ptr"))
def PyObject_GetItem(obj, key):
    if ptr_is_null(obj) or ptr_is_null(key):
        _type_error(cstr("invalid PyObject_GetItem call"))
        return null()
    out = py_obj_getitem(obj, key)
    if ptr_is_null(out) and py_err_occurred() == 0:
        py_raise_owned(py_exc_new(4, cstr("item not found")))  # PY_EXC_KEYERROR
    return out


@c_abi_typed_export("PyObject_SetItem", "i32", ("ptr", "ptr", "ptr"))
def PyObject_SetItem(obj, key, value) -> int:
    if ptr_is_null(obj) or ptr_is_null(key) or ptr_is_null(value):
        _type_error(cstr("invalid PyObject_SetItem call"))
        return -1
    rc: int = py_obj_setitem(obj, key, value)
    if rc != 0 and py_err_occurred() == 0:
        _type_error(cstr("object does not support item assignment"))
    if rc == 0:
        return 0
    return -1


@c_abi_typed_export("PyObject_DelItem", "i32", ("ptr", "ptr"))
def PyObject_DelItem(obj, key) -> int:
    if ptr_is_null(obj) or ptr_is_null(key):
        _type_error(cstr("invalid PyObject_DelItem call"))
        return -1
    rc: int = py_obj_delitem(obj, key)
    if rc != 0 and py_err_occurred() == 0:
        _type_error(cstr("object does not support item deletion"))
    if rc == 0:
        return 0
    return -1


@c_abi_typed_export("PyObject_GetIter", "ptr", ("ptr",))
def PyObject_GetIter(obj):
    if ptr_is_null(obj):
        _type_error(cstr("NULL object is not iterable"))
        return null()
    return py_obj_iter(obj)


@c_abi_typed_export("PyObject_SelfIter", "ptr", ("ptr",))
def PyObject_SelfIter(obj):
    if ptr_is_null(obj):
        _type_error(cstr("NULL object has no self iterator"))
        return null()
    py_incref(obj)
    return obj


@c_abi_typed_export("PyObject_RichCompareBool", "i32", ("ptr", "ptr", "i32"))
def PyObject_RichCompareBool(left, right, opid: int) -> int:
    # Orderings raise TypeError for unorderable operands: -1, like EQ/NE.
    if opid == 0:  # Py_LT
        lt_result: int = py_obj_lt(left, right)
        if py_err_occurred() != 0:
            return -1
        return 1 if lt_result != 0 else 0
    if opid == 1:  # Py_LE
        le_result: int = py_obj_le(left, right)
        if py_err_occurred() != 0:
            return -1
        return 1 if le_result != 0 else 0
    if opid == 2:  # Py_EQ
        eq_result: int = py_obj_eq(left, right)
        if py_err_occurred() != 0:
            return -1
        if eq_result != 0:
            return 1
        return 0
    if opid == 3:  # Py_NE
        ne_result: int = py_obj_eq(left, right)
        if py_err_occurred() != 0:
            return -1
        if ne_result != 0:
            return 0
        return 1
    if opid == 4:  # Py_GT
        gt_result: int = py_obj_gt(left, right)
        if py_err_occurred() != 0:
            return -1
        return 1 if gt_result != 0 else 0
    if opid == 5:  # Py_GE
        ge_result: int = py_obj_ge(left, right)
        if py_err_occurred() != 0:
            return -1
        return 1 if ge_result != 0 else 0
    py_raise_owned(py_exc_new(2, cstr("invalid rich-compare operation")))  # ValueError
    return -1


@c_abi_typed_export("PyObject_RichCompare", "ptr", ("ptr", "ptr", "i32"))
def PyObject_RichCompare(left, right, opid: int):
    if opid == 2 or opid == 3:
        equal: int = py_obj_eq_value(left, right)
        if py_err_occurred() != 0:
            return null()
        if opid == 3:
            if equal != 0:
                return PyBool_FromLong(0)
            return PyBool_FromLong(1)
        return PyBool_FromLong(equal)
    result: int = PyObject_RichCompareBool(left, right, opid)
    if result < 0:
        return null()
    return PyBool_FromLong(result)


@c_abi_typed_export("PyObject_IsInstance", "i32", ("ptr", "ptr"))
def PyObject_IsInstance(obj, cls) -> int:
    if ptr_is_null(obj) or ptr_is_null(cls):
        _type_error(cstr("invalid PyObject_IsInstance call"))
        return -1
    result: int = py_obj_isinstance(obj, cls)
    if result < 0:
        return -1
    if result != 0:
        return 1
    return 0


@c_abi_typed_export("PyObject_ClearWeakRefs", "void", ("ptr",))
def PyObject_ClearWeakRefs(obj) -> None:
    if not ptr_is_null(obj):
        py_weakref_invalidate(obj)


@c_abi_typed_export("PyObject_GC_Track", "void", ("ptr",))
def PyObject_GC_Track(op) -> None:
    return


@c_abi_typed_export("PyObject_GC_UnTrack", "void", ("ptr",))
def PyObject_GC_UnTrack(op) -> None:
    return


@c_abi_typed_export("PyObject_GC_Del", "void", ("ptr",))
def PyObject_GC_Del(op) -> None:
    PyObject_Free(op)


@c_abi_typed_export("PyObject_AsFileDescriptor", "i32", ("ptr",))
def PyObject_AsFileDescriptor(o) -> int:
    if not ptr_is_null(o) and PyLong_Check(o) != 0:
        return PyLong_AsLong(o)
    _type_error(cstr("argument must be an int file descriptor"))
    return -1


def _len_hint_value(obj, out_ptr) -> int:
    if ptr_is_null(obj) or is_tagged_int(obj) or ptr_is_null(out_ptr):
        return 0
    tag: int = load_i32(obj, 8)
    if (
        tag == PY_TYPE_LIST  # PY_TYPE_LIST
        or tag == PY_TYPE_TUPLE  # PY_TYPE_TUPLE
        or tag == PY_TYPE_STR  # PY_TYPE_STR
        or tag == PY_TYPE_BYTES  # PY_TYPE_BYTES
        or tag == PY_TYPE_BYTEARRAY  # PY_TYPE_BYTEARRAY
        or tag == PY_TYPE_MEMORYVIEW  # PY_TYPE_MEMORYVIEW
        or tag == PY_TYPE_DICT  # PY_TYPE_DICT
        or tag == PY_TYPE_SET  # PY_TYPE_SET
    ):
        store_i64(out_ptr, 0, py_obj_len(obj))
        return 1
    if tag == PY_TYPE_INSTANCE or tag >= PY_TYPE_USER_CLASS_START:
        handled_slot = stack_alloc(8)
        store_i64(handled_slot, 0, 0)
        user_len: int = py_user_len_dispatch(obj, handled_slot)
        if load_i64(handled_slot, 0) != 0:
            store_i64(out_ptr, 0, user_len)
            return 1
    return 0


@c_abi_typed_export("PyObject_LengthHint", "i64", ("ptr", "i64"))
def PyObject_LengthHint(obj, default_value: int) -> int:
    if default_value < 0:
        py_raise_owned(py_exc_new(2, cstr("default length hint must be non-negative")))
        return -1
    n_slot = stack_alloc(8)
    store_i64(n_slot, 0, 0)
    if _len_hint_value(obj, n_slot) == 0:
        return default_value
    n: int = load_i64(n_slot, 0)
    if n < 0:
        py_raise_owned(py_exc_new(2, cstr("negative length hint")))
        return -1
    return n
