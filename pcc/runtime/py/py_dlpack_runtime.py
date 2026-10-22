"""Classic kDLMetal DLPack capsule ABI authored in pcc-Python."""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import PY_TYPE_SET

from pcc.extern import c_abi_export, c_int32, c_int64, c_ptr, extern
from pcc.unsafe import (
    atomic_cas_i32,
    calloc,
    cstr,
    free,
    function_addr,
    int_to_ptr,
    load_i8,
    load_i32,
    load_i64,
    load_ptr,
    memset,
    null,
    ptr_add,
    ptr_is_null,
    stack_alloc,
    store_i8,
    store_i32,
    store_i64,
    store_ptr,
    unsigned_div_i64,
    unsigned_greater_i64,
    unsigned_rem_i64,
    wrapping_mul_i64,
    call_void_ptr1,
)


pcc_py_capsule_new = extern("pcc_py_capsule_new", (c_ptr, c_ptr, c_ptr), c_ptr)
pcc_py_capsule_get_pointer = extern(
    "pcc_py_capsule_get_pointer", (c_ptr, c_ptr), c_ptr
)
pcc_py_capsule_get_name = extern("pcc_py_capsule_get_name", (c_ptr,), c_ptr)
pcc_py_capsule_is_valid = extern(
    "pcc_py_capsule_is_valid", (c_ptr, c_ptr), c_int32
)
pcc_py_capsule_set_name = extern(
    "pcc_py_capsule_set_name", (c_ptr, c_ptr), c_int32
)
strcmp = extern("strcmp", (c_ptr, c_ptr), c_int32)
pcc_gc_external_resource_backend = extern(
    "pcc_gc_external_resource_backend", (c_int64,), c_int64
)
pcc_gc_external_resource_release_after_fence = extern(
    "pcc_gc_external_resource_release_after_fence", (c_int64,), c_int64
)


@c_abi_export("pcc_dlpack_compute_nbytes_runtime")
def _compute_nbytes(
    bits: int,
    lanes: int,
    ndim: int,
    shape: c_ptr,
    out_nbytes: c_ptr,
) -> int:
    if (
        bits <= 0
        or bits > 255
        or lanes <= 0
        or lanes > 65535
        or ndim <= 0
        or ndim > 8
        or ptr_is_null(shape)
        or ptr_is_null(out_nbytes)
    ):
        return -1
    lane_bits: int = bits * lanes
    if lane_bits == 0 or unsigned_rem_i64(lane_bits, 8) != 0:
        return -1
    elements: int = 1
    index: int = 0
    while index < ndim:
        dim: int = load_i64(shape, index * 8)
        if dim <= 0:
            return -1
        if unsigned_greater_i64(elements, unsigned_div_i64(-1, dim)):
            return -1
        elements = wrapping_mul_i64(elements, dim)
        index = index + 1
    item_bytes: int = unsigned_div_i64(lane_bits, 8)
    if unsigned_greater_i64(elements, unsigned_div_i64(-1, item_bytes)):
        return -1
    store_i64(out_nbytes, 0, wrapping_mul_i64(elements, item_bytes))
    return 0


@c_abi_export("pcc_dlpack_managed_deleter_runtime")
def _managed_deleter(managed: c_ptr) -> None:
    if ptr_is_null(managed):
        return
    owner = load_ptr(managed, 48)
    if ptr_is_null(owner):
        return
    if atomic_cas_i32(owner, 136, 0, 1, "acq_rel", "acquire") != 0:
        return
    pcc_gc_external_resource_release_after_fence(load_i64(owner, 128))
    free(owner)


@c_abi_export("pcc_dlpack_capsule_destructor_runtime")
def _capsule_destructor(capsule: c_ptr) -> None:
    if pcc_py_capsule_is_valid(capsule, cstr("dltensor")) == 0:
        return
    managed = pcc_py_capsule_get_pointer(capsule, cstr("dltensor"))
    if ptr_is_null(managed):
        return
    deleter = load_ptr(managed, 56)
    if not ptr_is_null(deleter):
        call_void_ptr1(deleter, managed)


@c_abi_export("pcc_dlpack_buffer_handle_packet_size")
def pcc_dlpack_buffer_handle_packet_size() -> int:
    return 120


@c_abi_export("pcc_dlpack_metal_capsule_new")
def pcc_dlpack_metal_capsule_new(
    external_resource_id: int,
    native_handle: int,
    dtype_code: int,
    dtype_bits: int,
    dtype_lanes: int,
    ndim: int,
    shape: c_ptr,
) -> c_ptr:
    nbytes = stack_alloc(8)
    if (
        external_resource_id == 0
        or native_handle == 0
        or dtype_code < 0
        or dtype_code > 255
        or pcc_gc_external_resource_backend(external_resource_id) < 0
        or _compute_nbytes(dtype_bits, dtype_lanes, ndim, shape, nbytes) != 0
    ):
        return null()

    owner = calloc(1, 144)
    if ptr_is_null(owner):
        return null()
    index: int = 0
    while index < ndim:
        store_i64(owner, 64 + index * 8, load_i64(shape, index * 8))
        index = index + 1
    store_i64(owner, 128, external_resource_id)
    store_ptr(owner, 0, int_to_ptr(native_handle))
    store_i32(owner, 8, PY_TYPE_SET)
    store_i32(owner, 12, 0)
    store_i32(owner, 16, ndim)
    store_i8(owner, 20, dtype_code)
    store_i8(owner, 21, dtype_bits)
    store_i8(owner, 22, dtype_lanes & 255)
    store_i8(owner, 23, unsigned_div_i64(dtype_lanes, 256) & 255)
    store_ptr(owner, 24, ptr_add(owner, 64))
    store_ptr(owner, 32, null())
    store_i64(owner, 40, 0)
    store_ptr(owner, 48, owner)
    store_ptr(owner, 56, function_addr("pcc_dlpack_managed_deleter_runtime"))

    capsule = pcc_py_capsule_new(
        owner,
        cstr("dltensor"),
        function_addr("pcc_dlpack_capsule_destructor_runtime"),
    )
    if ptr_is_null(capsule):
        free(owner)
        return null()
    return capsule


@c_abi_export("pcc_dlpack_capsule_name_code")
def pcc_dlpack_capsule_name_code(capsule: c_ptr) -> int:
    name = pcc_py_capsule_get_name(capsule)
    if ptr_is_null(name):
        return 0
    if strcmp(name, cstr("dltensor")) == 0:
        return 1
    if strcmp(name, cstr("used_dltensor")) == 0:
        return 2
    return 0


@c_abi_export("pcc_dlpack_capsule_consume")
def pcc_dlpack_capsule_consume(
    capsule: c_ptr,
    out_handle: c_ptr,
    out_managed_tensor: c_ptr,
) -> int:
    if ptr_is_null(out_handle) or ptr_is_null(out_managed_tensor):
        return -1
    store_ptr(out_managed_tensor, 0, null())
    if pcc_py_capsule_is_valid(capsule, cstr("dltensor")) == 0:
        return 2
    managed = pcc_py_capsule_get_pointer(capsule, cstr("dltensor"))
    if (
        ptr_is_null(managed)
        or ptr_is_null(load_ptr(managed, 48))
        or ptr_is_null(load_ptr(managed, 56))
        or ptr_is_null(load_ptr(managed, 0))
        or load_i32(managed, 8) != PY_TYPE_SET
        or load_i32(managed, 12) != 0
        or load_i32(managed, 16) <= 0
        or load_i32(managed, 16) > 8
        or ptr_is_null(load_ptr(managed, 24))
        or not ptr_is_null(load_ptr(managed, 32))
        or load_i64(managed, 40) != 0
    ):
        return -2
    owner = load_ptr(managed, 48)
    nbytes = stack_alloc(8)
    lanes: int = (load_i8(managed, 22) & 255) + (
        (load_i8(managed, 23) & 255) * 256
    )
    if (
        _compute_nbytes(
            load_i8(managed, 21) & 255,
            lanes,
            load_i32(managed, 16),
            load_ptr(managed, 24),
            nbytes,
        )
        != 0
    ):
        return -3

    memset(out_handle, 0, 120)
    store_i64(out_handle, 0, load_i64(managed, 0))
    store_i64(out_handle, 8, load_i64(owner, 128))
    store_i64(out_handle, 16, load_i64(nbytes, 0))
    ndim: int = load_i32(managed, 16)
    index: int = 0
    while index < ndim:
        store_i64(out_handle, 24 + index * 8, load_i64(load_ptr(managed, 24), index * 8))
        index = index + 1
    store_i64(out_handle, 88, ndim)
    store_i32(out_handle, 96, load_i32(managed, 8))
    store_i32(out_handle, 100, load_i32(managed, 12))
    store_i32(out_handle, 104, load_i8(managed, 20) & 255)
    store_i32(out_handle, 108, load_i8(managed, 21) & 255)
    store_i32(out_handle, 112, lanes)
    if pcc_py_capsule_set_name(capsule, cstr("used_dltensor")) != 0:
        memset(out_handle, 0, 120)
        return -4
    store_ptr(out_managed_tensor, 0, managed)
    return 0


@c_abi_export("pcc_dlpack_managed_tensor_release")
def pcc_dlpack_managed_tensor_release(managed_tensor: c_ptr) -> int:
    if ptr_is_null(managed_tensor):
        return -1
    deleter = load_ptr(managed_tensor, 56)
    if ptr_is_null(deleter):
        return -1
    call_void_ptr1(deleter, managed_tensor)
    return 0
