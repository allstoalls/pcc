"""Python object semantics for os.uname() and os.cpu_count()."""

__pcc_runtime_port__ = True

from pcc.extern import extern, c_abi_export, c_ptr, c_int64, c_void
from pcc.unsafe import cstr, null, ptr_is_null, stack_alloc, strlen


pcc_platform_uname = extern("pcc_platform_uname", (c_ptr,), c_int64)
pcc_platform_uname_field = extern(
    "pcc_platform_uname_field", (c_ptr, c_int64), c_ptr
)
pcc_platform_cpu_count = extern("pcc_platform_cpu_count", (), c_int64)

py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_set_item = extern("py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void)
py_str_new = extern("py_str_new", (c_ptr, c_int64), c_ptr)
py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)


@c_abi_export("py_os_uname")
def py_os_uname():
    buffer = stack_alloc(2048)
    if pcc_platform_uname(buffer) != 0:
        py_raise_owned(py_exc_new(14, cstr("os.uname() failed")))
        return null()
    out = py_tuple_new(5)
    if ptr_is_null(out):
        return null()
    index = 0
    while index < 5:
        raw = pcc_platform_uname_field(buffer, index)
        field = py_str_new(raw, strlen(raw))
        if ptr_is_null(field):
            py_decref(out)
            return null()
        py_tuple_set_item(out, index, field)
        py_decref(field)
        index = index + 1
    return out


@c_abi_export("py_os_cpu_count")
def py_os_cpu_count():
    count = pcc_platform_cpu_count()
    if count <= 0:
        count = 0
    return py_int_from_i64(count)
