"""pcc-Python owner for the CPython-compatible raw allocation facade.

These entrypoints intentionally delegate to pcc's freestanding allocator.
They preserve CPython's non-null zero-size convention without introducing a
second allocator or object-layout policy in the C-API layer.
"""

__pcc_runtime_port__ = True

from pcc.extern import c_abi_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    calloc,
    free,
    malloc,
    null,
    ptr_is_null,
    realloc,
    unsigned_div_i64,
    unsigned_greater_i64,
)

PyErr_NoMemory = extern("PyErr_NoMemory", (), c_ptr)
pcc_gc_pointer_unregister = extern(
    "pcc_gc_pointer_unregister", (c_ptr,), c_int64
)
pcc_gc_pointer_is_managed = extern(
    "pcc_gc_pointer_is_managed", (c_ptr,), c_int64
)
pcc_gc_granule_object_retire = extern(
    "pcc_gc_granule_object_retire", (c_ptr,), c_int64
)
pcc_gc_note_object_freeing = extern(
    "pcc_gc_note_object_freeing", (c_ptr,), c_void
)


@c_abi_export("pcc_capi_nonzero_size")
def _nonzero_size(size: int) -> int:
    if size == 0:
        return 1
    return size


@c_abi_export("PyMem_Malloc")
def PyMem_Malloc(size: int) -> c_ptr:
    return malloc(_nonzero_size(size))


@c_abi_export("PyMem_RawMalloc")
def PyMem_RawMalloc(size: int) -> c_ptr:
    return PyMem_Malloc(size)


@c_abi_export("PyMem_Calloc")
def PyMem_Calloc(nelem: int, elsize: int) -> c_ptr:
    # size_t is carried in the raw i64 lane.  Compare and divide as unsigned
    # so values with bit 63 set cannot evade the multiplication guard.
    if nelem != 0 and unsigned_greater_i64(
        elsize, unsigned_div_i64(-1, nelem)
    ):
        PyErr_NoMemory()
        return null()
    return calloc(_nonzero_size(nelem), _nonzero_size(elsize))


@c_abi_export("PyMem_RawCalloc")
def PyMem_RawCalloc(nelem: int, elsize: int) -> c_ptr:
    return PyMem_Calloc(nelem, elsize)


@c_abi_export("PyMem_Realloc")
def PyMem_Realloc(ptr, new_size: int) -> c_ptr:
    return realloc(ptr, _nonzero_size(new_size))


@c_abi_export("PyMem_RawRealloc")
def PyMem_RawRealloc(ptr, new_size: int) -> c_ptr:
    return PyMem_Realloc(ptr, new_size)


@c_abi_export("PyMem_Free")
def PyMem_Free(ptr) -> None:
    if not ptr_is_null(ptr):
        free(ptr)


@c_abi_export("PyMem_RawFree")
def PyMem_RawFree(ptr) -> None:
    PyMem_Free(ptr)


@c_abi_export("PyObject_Malloc")
def PyObject_Malloc(size: int) -> c_ptr:
    return PyMem_Malloc(size)


@c_abi_export("PyObject_Calloc")
def PyObject_Calloc(nelem: int, elsize: int) -> c_ptr:
    return PyMem_Calloc(nelem, elsize)


@c_abi_export("PyObject_Realloc")
def PyObject_Realloc(ptr, new_size: int) -> c_ptr:
    return PyMem_Realloc(ptr, new_size)


@c_abi_export("PyObject_Free")
def PyObject_Free(ptr) -> None:
    if pcc_gc_pointer_is_managed(ptr) != 0:
        pcc_gc_note_object_freeing(ptr)
        # A corrupt ordinary-slab lifecycle must stay quarantined.  Returning
        # the same address to PyMem_Free would overwrite the invalid state and
        # make a later reuse look like a valid allocator transition.
        if pcc_gc_pointer_unregister(ptr) < 0:
            return
    elif pcc_gc_granule_object_retire(ptr) < 0:
        # Backend 0 has no object-index/exact-set fallback for ordinary LIVE
        # slab cells.  A corrupt marker therefore makes the pointer appear
        # unmanaged, but it must still be quarantined rather than recycled.
        return
    PyMem_Free(ptr)
