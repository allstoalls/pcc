"""Slice dispatch split out from py_obj_ops_dispatch.

Keep ``py_obj_slice`` in its own archive member so generic getitem/len/truthy
dispatch does not force list/tuple/str/bytes slicing helpers into ordinary
executables.
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import PY_TYPE_BYTEARRAY, PY_TYPE_BYTES, PY_TYPE_INT, PY_TYPE_INSTANCE, PY_TYPE_LIST, PY_TYPE_MEMORYVIEW, PY_TYPE_STR, PY_TYPE_TUPLE, PY_TYPE_USER_CLASS_START

from pcc.extern import c_abi_export, c_int32, c_int64, c_ptr, c_void, extern
from pcc.unsafe import is_tagged_int, load_i32, null, ptr_is_null


py_list_slice = extern("py_list_slice", (c_ptr, c_ptr, c_ptr, c_ptr), c_ptr)
py_tuple_slice = extern("py_tuple_slice", (c_ptr, c_ptr, c_ptr, c_ptr), c_ptr)
py_str_slice = extern("py_str_slice", (c_ptr, c_ptr, c_ptr, c_ptr), c_ptr)
py_bytes_slice = extern("py_bytes_slice", (c_ptr, c_ptr, c_ptr, c_ptr), c_ptr)
py_slice_new = extern("py_slice_new", (c_ptr, c_ptr, c_ptr), c_ptr)
py_obj_getitem = extern("py_obj_getitem", (c_ptr, c_ptr), c_ptr)
py_decref = extern("py_decref", (c_ptr,), c_void)
pcc_diagnostics_runtime_log_event_code = extern(
    "pcc_diagnostics_runtime_log_event_code",
    (c_int32, c_int32, c_int64, c_int64, c_ptr),
    c_void,
)


def _type_of(o) -> int:
    if is_tagged_int(o) != 0:
        return PY_TYPE_INT
    return load_i32(o, 8)


@c_abi_export("py_obj_slice")
def py_obj_slice(o, lo, hi, step):
    if ptr_is_null(o) != 0:
        return null()
    tag: int = _type_of(o)
    pcc_diagnostics_runtime_log_event_code(7, 2, tag, 0, o)
    if tag == PY_TYPE_LIST:
        return py_list_slice(o, lo, hi, step)
    if tag == PY_TYPE_TUPLE:
        return py_tuple_slice(o, lo, hi, step)
    if tag == PY_TYPE_STR:
        return py_str_slice(o, lo, hi, step)
    if tag == PY_TYPE_BYTES or tag == PY_TYPE_BYTEARRAY or tag == PY_TYPE_MEMORYVIEW:
        return py_bytes_slice(o, lo, hi, step)
    if tag == PY_TYPE_INSTANCE or tag >= PY_TYPE_USER_CLASS_START:
        # obj[lo:hi:step] dispatches __getitem__(slice(lo, hi, step)) via the
        # generic getitem path, like CPython.
        sl = py_slice_new(lo, hi, step)
        if ptr_is_null(sl) != 0:
            return null()
        r = py_obj_getitem(o, sl)
        py_decref(sl)
        return r
    return null()
