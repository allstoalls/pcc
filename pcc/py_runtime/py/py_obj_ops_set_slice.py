"""Set-slice dispatch split out from py_obj_ops_dispatch.

Keep ``py_obj_set_slice`` in its own archive member so generic truthy/add/getitem
dispatch does not force list/extension set-slice support into ordinary
executables.
"""

__pcc_runtime_port__ = True

from pcc.py_runtime.py.py_abi_constants import (
    PY_TYPE_BYTEARRAY,
    PY_TYPE_INT,
    PY_TYPE_LIST,
)

from pcc.extern import c_abi_export, c_int32, c_int64, c_ptr, c_void, extern
from pcc.unsafe import cstr, is_tagged_int, load_i32, ptr_is_null


py_list_set_slice = extern(
    "py_list_set_slice", (c_ptr, c_ptr, c_ptr, c_ptr, c_ptr), c_int64
)
pcc_capi_is_cext_type_tag = extern(
    "pcc_capi_is_cext_type_tag", (c_int64,), c_int64
)
pcc_capi_cext_object_setitem = extern(
    "pcc_capi_cext_object_setitem", (c_ptr, c_ptr, c_ptr), c_int64
)
py_bytearray_set_slice = extern(
    "py_bytearray_set_slice",
    (c_ptr, c_ptr, c_ptr, c_ptr, c_ptr),
    c_int64,
)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_slice_new = extern("py_slice_new", (c_ptr, c_ptr, c_ptr), c_ptr)
py_decref = extern("py_decref", (c_ptr,), c_void)


def _type_of(o) -> int:
    if is_tagged_int(o) != 0:
        return PY_TYPE_INT
    return load_i32(o, 8)


@c_abi_export("py_obj_set_slice")
def py_obj_set_slice(o, lo, hi, step, replacement) -> int:
    if ptr_is_null(o) != 0:
        return -1
    tag: int = _type_of(o)
    if pcc_capi_is_cext_type_tag(tag) != 0:
        slice_obj = py_slice_new(lo, hi, step)
        if ptr_is_null(slice_obj) != 0:
            return -1
        result: int = pcc_capi_cext_object_setitem(o, slice_obj, replacement)
        py_decref(slice_obj)
        return result
    if tag == PY_TYPE_LIST:
        return py_list_set_slice(o, lo, hi, step, replacement)
    if tag == PY_TYPE_BYTEARRAY:
        return py_bytearray_set_slice(o, lo, hi, step, replacement)
    # Returning -1 without setting an exception made an unsupported slice
    # store vanish: the caller's pending-exception check saw nothing and
    # carried on.  A bytearray reaching here -- which it did whenever the
    # receiver's static type was dyn, as a closure capture invoked
    # indirectly is -- left the linker's output image entirely zero-filled
    # with a successful return code.
    py_raise_owned(
        py_exc_new(3, cstr("object does not support slice assignment"))
    )
    return -1
