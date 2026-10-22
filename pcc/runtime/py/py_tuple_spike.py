"""Phase 4c.1 spike: py_tuple.c ported to pcc-compilable Python.

Mirrors pcc/runtime/src/py_tuple.c semantics exactly:

    typedef struct {
        PyObjectHeader h;           // int64 refcount, int32 type_tag, int32 flags (16 bytes)
        int64_t        len;         // 8 bytes
        PyObject       *items[];    // flexible array of owned refs
    } PyTupleObject;

Layout offsets (LP64, pcc int64_t = long):
    0:  refcount (int64)
    8:  type_tag (int32)
    12: flags    (int32)
    16: len      (int64)
    24: items[0], items[1], ...      (ptr each, 8 bytes)
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import PY_TYPE_TUPLE
from pcc.extern import extern, c_ptr, c_int64, c_void
from pcc.unsafe import (
    load_i64,
    load_ptr,
    malloc,
    memset,
    ptr_add,
    store_i32,
    store_i64,
    store_ptr,
)

py_incref         = extern("py_incref",         (c_ptr,),                             c_void)


# Layout constants.
OFFSET_REFCOUNT: int = 0
OFFSET_TYPE_TAG: int = 8
OFFSET_FLAGS:    int = 12
OFFSET_LEN:      int = 16
OFFSET_ITEMS:    int = 24
SIZEOF_PTR:      int = 8

def py_tuple_new_py(n: int):
    if n < 0:
        n = 0
    bytes_total: int = OFFSET_ITEMS + n * SIZEOF_PTR
    t = malloc(bytes_total)
    store_i64(t, OFFSET_REFCOUNT, 1)
    store_i32(t, OFFSET_TYPE_TAG, PY_TYPE_TUPLE)
    store_i32(t, OFFSET_FLAGS, 0)
    store_i64(t, OFFSET_LEN, n)
    if n > 0:
        memset(ptr_add(t, OFFSET_ITEMS), 0, n * SIZEOF_PTR)
    return t


def py_tuple_set_item_py(tuple_ptr, i: int, item) -> None:
    if tuple_ptr is None:
        return
    tuple_len: int = load_i64(tuple_ptr, OFFSET_LEN)
    if i < 0 or i >= tuple_len:
        return
    py_incref(item)
    slot_offset: int = OFFSET_ITEMS + i * SIZEOF_PTR
    store_ptr(tuple_ptr, slot_offset, item)


def py_tuple_get_py(tuple_ptr, i: int):
    if tuple_ptr is None:
        return None
    tuple_len: int = load_i64(tuple_ptr, OFFSET_LEN)
    if i < 0:
        i = i + tuple_len
    if i < 0 or i >= tuple_len:
        return None
    slot_offset: int = OFFSET_ITEMS + i * SIZEOF_PTR
    v = load_ptr(tuple_ptr, slot_offset)
    py_incref(v)
    return v


def py_tuple_len_py(tuple_ptr) -> int:
    if tuple_ptr is None:
        return 0
    return load_i64(tuple_ptr, OFFSET_LEN)


# Runtime test: allocate a 3-tuple, set items, read back.
def main() -> None:
    py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
    a = py_int_from_i64(10)
    b = py_int_from_i64(20)
    c = py_int_from_i64(30)

    t = py_tuple_new_py(3)
    py_tuple_set_item_py(t, 0, a)
    py_tuple_set_item_py(t, 1, b)
    py_tuple_set_item_py(t, 2, c)

    n: int = py_tuple_len_py(t)
    print("len", n)

    for i in range(3):
        item = py_tuple_get_py(t, i)
        # print the int via existing runtime
        py_print = extern("py_print", (c_ptr,), c_void)
        py_print(item)


main()
