"""pcc-Python port of py_str.c.

Only py_str_new remains here; the rest of the string runtime lives in
py_str_accessors.py.
"""

__pcc_runtime_port__ = True

from pcc.extern import extern, c_abi_export, c_int32, c_int64, c_ptr
from pcc.runtime.py.py_abi_constants import PYOBJECTHEADER_REFCOUNT_OFFSET, PYOBJECTHEADER_TYPE_TAG_OFFSET, PYSTROBJECT_BYTE_LEN_OFFSET, PYSTROBJECT_CP_LEN_OFFSET, PYSTROBJECT_DATA_OFFSET, PYSTROBJECT_HASH_OFFSET, PYSTROBJECT_SIZE, PY_TYPE_STR
from pcc.unsafe import (
    memmove,
    null,
    ptr_add,
    ptr_is_null,
    store_i8,
    store_i32,
    store_i64,
)

pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_ptr)


def _str_alloc(byte_len: int):
    if byte_len < 0:
        return null()
    s = pcc_gc_alloc(PYSTROBJECT_SIZE + byte_len + 1, PY_TYPE_STR, 0)
    if ptr_is_null(s) != 0:
        return null()
    store_i64(s, PYOBJECTHEADER_REFCOUNT_OFFSET, 1)
    store_i32(s, PYOBJECTHEADER_TYPE_TAG_OFFSET, PY_TYPE_STR)
    store_i64(s, PYSTROBJECT_BYTE_LEN_OFFSET, byte_len)
    store_i64(s, PYSTROBJECT_CP_LEN_OFFSET, -1)
    store_i64(s, PYSTROBJECT_HASH_OFFSET, -1)
    store_i8(s, PYSTROBJECT_DATA_OFFSET + byte_len, 0)
    return s


@c_abi_export("py_str_new")
def py_str_new(utf8, byte_len: int):
    if byte_len < 0:
        byte_len = 0
    s = _str_alloc(byte_len)
    if ptr_is_null(s) != 0:
        return null()
    if ptr_is_null(utf8) == 0:
        if byte_len > 0:
            memmove(ptr_add(s, PYSTROBJECT_DATA_OFFSET), utf8, byte_len)
    return s
