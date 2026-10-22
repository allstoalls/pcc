"""pcc-Python owners for the no-libpython buffer/memoryview surface.

Replaces the PyObject_CheckBuffer / PyObject_GetBuffer / PyMemoryView_*
block of py_capi_shim.c.  The buffer-data probe (bytes/bytearray/memoryview
layout) is duplicated here; PyBuffer_Release already lives in
py_capi_misc_runtime.py.

Py_buffer layout (CPython): buf@0, obj@8, len@16, itemsize@24,
readonly@32 (i32), ndim@36 (i32), format@40, shape@48, strides@56,
suboffsets@64, internal@72.
PccBufferMeta: shape@0 (i64), strides@8 (i64).
PyBytesObject/PyByteArrayObject: byte_len@16, data@24.
PyMemoryViewObject: base@16, owned Py_buffer allocation@24.

The owned allocation is 96 bytes: the public 80-byte Py_buffer followed by
inline shape@80 and strides@88 words.  Py_buffer.obj is a derived borrowed
alias of the memoryview's owned base reference; the GC visits base@16 and the
refresh hook rewrites obj/buf after relocation.

Owned surface (stable C ABI names):

  PyObject_CheckBuffer, PyObject_GetBuffer, PyMemoryView_Check,
  PyMemoryView_FromObject, PyMemoryView_FromMemory

Public object type tags come from the generated ``py_abi_constants`` module.
Private buffer flags remain owned here:
  PyBUF_WRITABLE 0x0001, PyBUF_FORMAT 0x0004, PyBUF_ND 0x0008,
  PyBUF_STRIDES 0x0018, PyBUF_WRITE 0x0200
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import PY_TYPE_BYTEARRAY, PY_TYPE_BYTES, PY_TYPE_MEMORYVIEW

from pcc.extern import c_abi_typed_export, c_int32, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    atomic_cas_i64,
    atomic_load_i64,
    cstr,
    define_global_ptr_null,
    global_load_ptr,
    global_store_ptr,
    int_to_ptr,
    is_tagged_int,
    load_i32,
    load_i64,
    load_ptr,
    memcpy,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    ptr_to_int,
    stack_alloc,
    store_i32,
    store_i64,
    store_i8,
    store_ptr,
)

py_type_of = extern("pcc_py_type_of", (c_ptr,), c_int64)
py_bytes_new = extern("py_bytes_new", (c_ptr, c_int64), c_ptr)
py_memoryview_new = extern("py_memoryview_new", (c_ptr,), c_ptr)
py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
PyMem_Malloc = extern("PyMem_Malloc", (c_int64,), c_ptr)
PyMem_Free = extern("PyMem_Free", (c_ptr,), c_void)
pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_ptr)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
pcc_gc_memoryview_initialize_owned_buffer = extern(
    "pcc_gc_memoryview_initialize_owned_buffer", (c_ptr, c_ptr), c_int64
)
pcc_gc_memoryview_refresh_owned_buffer = extern(
    "pcc_gc_memoryview_refresh_owned_buffer", (c_ptr,), c_int64
)

define_global_ptr_null("pcc_capi_buffer_lease_head")


def _type_error(message) -> None:
    py_raise_owned(py_exc_new(3, message))  # PY_EXC_TYPEERROR


def _runtime_error(message) -> None:
    py_raise_owned(py_exc_new(7, message))  # PY_EXC_RUNTIMEERROR


def _value_error(message) -> None:
    py_raise_owned(py_exc_new(2, message))  # PY_EXC_VALUEERROR


def _buffer_data(obj, buf_ptr, len_ptr, ro_ptr) -> int:
    if ptr_is_null(obj) or is_tagged_int(obj):
        return -1
    tag: int = load_i32(obj, 8)
    if tag == PY_TYPE_BYTES:  # PY_TYPE_BYTES
        store_ptr(buf_ptr, 0, ptr_add(obj, 24))
        store_i64(len_ptr, 0, load_i64(obj, 16))
        store_i32(ro_ptr, 0, 1)
        return 0
    if tag == PY_TYPE_BYTEARRAY:  # PY_TYPE_BYTEARRAY
        store_ptr(buf_ptr, 0, ptr_add(obj, 24))
        store_i64(len_ptr, 0, load_i64(obj, 16))
        store_i32(ro_ptr, 0, 0)
        return 0
    if tag == PY_TYPE_MEMORYVIEW:  # PY_TYPE_MEMORYVIEW
        base = pcc_gc_load_ptr(
            obj, ptr_add(obj, 16)
        )  # view->base
        return _buffer_data(base, buf_ptr, len_ptr, ro_ptr)
    return -1


def _buffer_lease_owner(obj):
    depth: int = 0
    while depth < 16:
        if ptr_is_null(obj) or is_tagged_int(obj):
            return null()
        if load_i32(obj, 8) != PY_TYPE_MEMORYVIEW:
            return obj
        obj = pcc_gc_load_ptr(obj, ptr_add(obj, 16))
        depth = depth + 1
    return null()


def _bytearray_from_memory(data, length: int) -> c_ptr:
    if length < 0:
        length = 0
    total: int = 48 + length + 1  # sizeof(PyByteArrayObject) + len + 1
    obj = pcc_gc_alloc(total, PY_TYPE_BYTEARRAY, 0)  # PY_TYPE_BYTEARRAY
    if ptr_is_null(obj):
        return null()
    store_i64(obj, 16, length)
    if length > 0 and not ptr_is_null(data):
        memcpy(ptr_add(obj, 24), data, length)
    store_i8(obj, 24 + length, 0)
    return obj


@c_abi_typed_export("PyObject_CheckBuffer", "i32", ("ptr",))
def PyObject_CheckBuffer(obj) -> int:
    buf_ptr = stack_alloc(8)
    len_ptr = stack_alloc(8)
    ro_ptr = stack_alloc(4)
    store_ptr(buf_ptr, 0, null())
    store_i64(len_ptr, 0, 0)
    store_i32(ro_ptr, 0, 1)
    if _buffer_data(obj, buf_ptr, len_ptr, ro_ptr) == 0:
        return 1
    return 0


@c_abi_typed_export("PyObject_GetBuffer", "i32", ("ptr", "ptr", "i32"))
def PyObject_GetBuffer(obj, view, flags: int) -> int:
    if ptr_is_null(view):
        _runtime_error(cstr("NULL Py_buffer"))
        return -1
    # memset(view, 0, sizeof(Py_buffer)) — 80 bytes
    i: int = 0
    while i < 80:
        store_i8(view, i, 0)
        i += 1
    buf_ptr = stack_alloc(8)
    len_ptr = stack_alloc(8)
    ro_ptr = stack_alloc(4)
    store_ptr(buf_ptr, 0, null())
    store_i64(len_ptr, 0, 0)
    store_i32(ro_ptr, 0, 1)
    if _buffer_data(obj, buf_ptr, len_ptr, ro_ptr) != 0:
        _type_error(cstr("object does not support buffer protocol"))
        return -1
    buf = load_ptr(buf_ptr, 0)
    length: int = load_i64(len_ptr, 0)
    readonly: int = load_i32(ro_ptr, 0)
    if (flags & 0x0001) != 0 and readonly != 0:  # PyBUF_WRITABLE
        _type_error(cstr("object is not writable"))
        return -1
    lease_owner = _buffer_lease_owner(obj)
    meta = PyMem_Malloc(40)
    if ptr_is_null(lease_owner) or ptr_is_null(meta):
        if not ptr_is_null(meta):
            PyMem_Free(meta)
        _runtime_error(cstr("out of memory creating buffer view"))
        return -1
    store_i64(meta, 0, length)  # shape
    store_i64(meta, 8, 1)  # strides
    store_ptr(meta, 16, lease_owner)
    store_ptr(meta, 24, obj)
    pcc_py_gc_minor_graph_lock()
    scan = global_load_ptr("pcc_capi_buffer_lease_head")
    owner_already_leased: int = 0
    view_already_leased: int = 0
    while not ptr_is_null(scan):
        if ptr_eq(load_ptr(scan, 16), lease_owner) or ptr_eq(load_ptr(scan, 24), lease_owner):
            owner_already_leased = 1
        if ptr_eq(load_ptr(scan, 16), obj) or ptr_eq(load_ptr(scan, 24), obj):
            view_already_leased = 1
        scan = load_ptr(scan, 32)
    store_ptr(meta, 32, global_load_ptr("pcc_capi_buffer_lease_head"))
    global_store_ptr("pcc_capi_buffer_lease_head", meta)
    if owner_already_leased == 0:
        pcc_gc_pin(lease_owner)
    if ptr_eq(obj, lease_owner) == 0 and view_already_leased == 0:
        pcc_gc_pin(obj)
    pcc_py_gc_minor_graph_unlock()
    store_ptr(view, 0, buf)  # buf
    store_ptr(view, 8, obj)  # obj
    store_i64(view, 16, length)  # len
    store_i64(view, 24, 1)  # itemsize
    store_i32(view, 32, readonly)  # readonly
    if (flags & 0x0008) != 0:  # PyBUF_ND
        store_i32(view, 36, 1)  # ndim
    else:
        store_i32(view, 36, 0)
    if (flags & 0x0004) != 0:  # PyBUF_FORMAT
        store_ptr(view, 40, cstr("B"))
    else:
        store_ptr(view, 40, null())
    if (flags & 0x0008) != 0:
        store_ptr(view, 48, meta)  # shape
        if (flags & 0x0018) != 0:  # PyBUF_STRIDES
            store_ptr(view, 56, ptr_add(meta, 8))  # strides
        else:
            store_ptr(view, 56, null())
    else:
        store_ptr(view, 48, null())
        store_ptr(view, 56, null())
    store_ptr(view, 64, null())  # suboffsets
    store_ptr(view, 72, meta)  # internal
    py_incref(obj)
    return 0


@c_abi_typed_export("PyMemoryView_Check", "i32", ("ptr",))
def PyMemoryView_Check(obj) -> int:
    if ptr_is_null(obj) or is_tagged_int(obj):
        return 0
    if load_i32(obj, 8) == PY_TYPE_MEMORYVIEW:  # PY_TYPE_MEMORYVIEW
        return 1
    return 0


@c_abi_typed_export("PyMemoryView_FromObject", "ptr", ("ptr",))
def PyMemoryView_FromObject(obj) -> c_ptr:
    buf_ptr = stack_alloc(8)
    len_ptr = stack_alloc(8)
    ro_ptr = stack_alloc(4)
    store_ptr(buf_ptr, 0, null())
    store_i64(len_ptr, 0, 0)
    store_i32(ro_ptr, 0, 1)
    if _buffer_data(obj, buf_ptr, len_ptr, ro_ptr) != 0:
        _type_error(cstr("object does not support buffer protocol"))
        return null()
    return py_memoryview_new(obj)


@c_abi_typed_export("PyMemoryView_FromMemory", "ptr", ("ptr", "i64", "i32"))
def PyMemoryView_FromMemory(mem, size: int, flags: int) -> c_ptr:
    if size < 0:
        _value_error(cstr("negative memoryview size"))
        return null()
    if ptr_is_null(mem) and size > 0:
        _value_error(cstr("NULL memoryview buffer"))
        return null()
    if (flags & 0x0200) != 0:  # PyBUF_WRITE
        base = _bytearray_from_memory(mem, size)
    else:
        base = py_bytes_new(mem, size)
    if ptr_is_null(base):
        _runtime_error(cstr("out of memory creating memoryview"))
        return null()
    view = py_memoryview_new(base)
    py_decref(base)
    return view


# --- pcc_PyMemoryView_GET_BASE ---------------------------------------


def _memoryview_owned_buffer(obj) -> c_ptr:
    bits: int = atomic_load_i64(
        obj, 24, "acquire"
    )
    if bits != 0:
        pcc_gc_memoryview_refresh_owned_buffer(obj)
        return int_to_ptr(bits)

    base = pcc_gc_load_ptr(
        obj, ptr_add(obj, 16)
    )
    if ptr_is_null(base):
        _runtime_error(cstr("memoryview has no base object"))
        return null()
    candidate = PyMem_Malloc(96)
    if ptr_is_null(candidate):
        _runtime_error(cstr("out of memory creating memoryview buffer"))
        return null()
    if pcc_gc_memoryview_initialize_owned_buffer(obj, candidate) == 0:
        PyMem_Free(candidate)
        _type_error(cstr("memoryview base does not support buffer protocol"))
        return null()

    candidate_bits: int = ptr_to_int(candidate)
    installed: int = atomic_cas_i64(
        obj,
        24,
        0,
        candidate_bits,
        "acq_rel",
        "acquire",
    )
    if installed != 0:
        PyMem_Free(candidate)
        pcc_gc_memoryview_refresh_owned_buffer(obj)
        return int_to_ptr(installed)
    return candidate


@c_abi_typed_export("pcc_PyMemoryView_GET_BASE", "ptr", ("ptr",))
def pcc_PyMemoryView_GET_BASE(obj) -> c_ptr:
    if PyMemoryView_Check(obj) == 0:
        _type_error(cstr("expected memoryview"))
        return null()
    pcc_gc_memoryview_refresh_owned_buffer(obj)
    return pcc_gc_load_ptr(obj, ptr_add(obj, 16))  # view->base


# --- pcc_PyMemoryView_GET_BUFFER -------------------------------------


@c_abi_typed_export("pcc_PyMemoryView_GET_BUFFER", "ptr", ("ptr",))
def pcc_PyMemoryView_GET_BUFFER(obj) -> c_ptr:
    if PyMemoryView_Check(obj) == 0:
        _type_error(cstr("expected memoryview"))
        return null()
    return _memoryview_owned_buffer(obj)
