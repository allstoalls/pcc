"""Freestanding pcc-Python owner of the prebuilt Metal bridge ABI."""

from pcc import i64
from pcc.extern import c_abi_export
from pcc.unsafe import (
    call_i64_i64_ptr,
    call_i64_ptr1,
    call_i64_ptr2,
    call_i64_ptr_i64_ptr_i64,
    call_i64_ptr_i64_ptr_ptr_ptr_ptr_bool,
    call_i64_ptr_ptr_ptr_ptr_ptr_bool,
    calloc,
    cstr,
    dynamic_library_close,
    dynamic_library_open,
    dynamic_library_symbol,
    free,
    int_to_ptr,
    load_i8,
    load_i64,
    load_ptr,
    null,
    ptr_add,
    ptr_diff,
    ptr_is_null,
    stack_alloc,
    store_i64,
    store_ptr,
)


__pcc_freestanding__ = True


@c_abi_export("pcc_freestanding_metal_load_symbol")
def _load_symbol(library_path, symbol, out_handle, out_symbol) -> i64:
    if ptr_is_null(library_path) or load_i8(library_path, 0) == 0:
        return -1
    if ptr_is_null(symbol) or load_i8(symbol, 0) == 0:
        return -2
    if ptr_is_null(out_handle) or ptr_is_null(out_symbol):
        return -10
    handle = dynamic_library_open(library_path)
    if ptr_is_null(handle):
        return -8
    fn = dynamic_library_symbol(handle, symbol)
    if ptr_is_null(fn):
        dynamic_library_close(handle)
        return -9
    store_ptr(out_handle, 0, handle)
    store_ptr(out_symbol, 0, fn)
    return 0


@c_abi_export("pcc_freestanding_metal_build_buffer_slots")
def _build_buffer_slots(native_buffer_ptrs, num_buffers: i64):
    if num_buffers == 0:
        return null()
    buffers = calloc(num_buffers, 8)
    if ptr_is_null(buffers):
        return null()
    index: i64 = 0
    while index < num_buffers:
        store_ptr(
            buffers,
            index * 8,
            int_to_ptr(load_i64(native_buffer_ptrs, index * 8)),
        )
        index = index + 1
    return buffers


@c_abi_export("pcc_freestanding_metal_build_scalar_slots")
def _build_scalar_slots(scalar_payload, scalar_offsets, num_scalars: i64):
    if num_scalars == 0:
        return null()
    scalars = calloc(num_scalars, 8)
    if ptr_is_null(scalars):
        return null()
    index: i64 = 0
    while index < num_scalars:
        store_ptr(
            scalars,
            index * 8,
            ptr_add(scalar_payload, load_i64(scalar_offsets, index * 8)),
        )
        index = index + 1
    return scalars


@c_abi_export("pcc_metal_source_runtime_call_prebuilt")
def pcc_metal_source_runtime_call_prebuilt(
    bridge_library_path,
    symbol,
    metal_source,
    metal_source_nbytes: i64,
    native_buffer_ptrs,
    num_buffers: i64,
    scalar_payload,
    scalar_offsets,
    num_scalars: i64,
    wait_until_completed: i64,
) -> i64:
    if ptr_is_null(bridge_library_path) or load_i8(bridge_library_path, 0) == 0:
        return -1
    if ptr_is_null(symbol) or load_i8(symbol, 0) == 0:
        return -2
    if ptr_is_null(metal_source) or metal_source_nbytes == 0:
        return -3
    if wait_until_completed == 0:
        return -4
    if num_buffers > 0 and ptr_is_null(native_buffer_ptrs):
        return -5
    if num_scalars > 0 and (
        ptr_is_null(scalar_payload) or ptr_is_null(scalar_offsets)
    ):
        return -6

    buffers = _build_buffer_slots(native_buffer_ptrs, num_buffers)
    if num_buffers > 0 and ptr_is_null(buffers):
        return -7
    scalars = _build_scalar_slots(scalar_payload, scalar_offsets, num_scalars)
    if num_scalars > 0 and ptr_is_null(scalars):
        free(buffers)
        return -7

    handle_slot = stack_alloc(8)
    symbol_slot = stack_alloc(8)
    store_ptr(handle_slot, 0, null())
    store_ptr(symbol_slot, 0, null())
    load_rc = _load_symbol(
        bridge_library_path,
        symbol,
        handle_slot,
        symbol_slot,
    )
    if load_rc != 0:
        free(scalars)
        free(buffers)
        return load_rc
    rc = call_i64_ptr_i64_ptr_ptr_ptr_ptr_bool(
        load_ptr(symbol_slot, 0),
        metal_source,
        metal_source_nbytes,
        buffers,
        scalars,
        null(),
        null(),
        1,
    )
    dynamic_library_close(load_ptr(handle_slot, 0))
    free(scalars)
    free(buffers)
    return rc


@c_abi_export("pcc_metal_metallib_runtime_call_prebuilt")
def pcc_metal_metallib_runtime_call_prebuilt(
    bridge_library_path,
    symbol,
    metallib_path,
    native_buffer_ptrs,
    num_buffers: i64,
    scalar_payload,
    scalar_offsets,
    num_scalars: i64,
    wait_until_completed: i64,
) -> i64:
    if ptr_is_null(bridge_library_path) or load_i8(bridge_library_path, 0) == 0:
        return -1
    if ptr_is_null(symbol) or load_i8(symbol, 0) == 0:
        return -2
    if ptr_is_null(metallib_path) or load_i8(metallib_path, 0) == 0:
        return -13
    if wait_until_completed == 0:
        return -4
    if num_buffers > 0 and ptr_is_null(native_buffer_ptrs):
        return -5
    if num_scalars > 0 and (
        ptr_is_null(scalar_payload) or ptr_is_null(scalar_offsets)
    ):
        return -6

    buffers = _build_buffer_slots(native_buffer_ptrs, num_buffers)
    if num_buffers > 0 and ptr_is_null(buffers):
        return -7
    scalars = _build_scalar_slots(scalar_payload, scalar_offsets, num_scalars)
    if num_scalars > 0 and ptr_is_null(scalars):
        free(buffers)
        return -7

    handle_slot = stack_alloc(8)
    symbol_slot = stack_alloc(8)
    store_ptr(handle_slot, 0, null())
    store_ptr(symbol_slot, 0, null())
    load_rc = _load_symbol(
        bridge_library_path,
        symbol,
        handle_slot,
        symbol_slot,
    )
    if load_rc != 0:
        free(scalars)
        free(buffers)
        return load_rc
    rc = call_i64_ptr_ptr_ptr_ptr_ptr_bool(
        load_ptr(symbol_slot, 0),
        metallib_path,
        buffers,
        scalars,
        null(),
        null(),
        1,
    )
    dynamic_library_close(load_ptr(handle_slot, 0))
    free(scalars)
    free(buffers)
    return rc


@c_abi_export("pcc_metal_buffer_runtime_create_prebuilt")
def pcc_metal_buffer_runtime_create_prebuilt(
    runtime_library_path,
    nbytes: i64,
    out_buffer_ptr,
) -> i64:
    if ptr_is_null(out_buffer_ptr):
        return -10
    store_i64(out_buffer_ptr, 0, 0)
    handle_slot = stack_alloc(8)
    symbol_slot = stack_alloc(8)
    load_rc = _load_symbol(
        runtime_library_path,
        cstr("pcc_metal_buffer_runtime_create"),
        handle_slot,
        symbol_slot,
    )
    if load_rc != 0:
        return load_rc
    native_slot = stack_alloc(8)
    store_ptr(native_slot, 0, null())
    rc = call_i64_i64_ptr(load_ptr(symbol_slot, 0), nbytes, native_slot)
    native = load_ptr(native_slot, 0)
    if rc == 0 and not ptr_is_null(native):
        store_i64(out_buffer_ptr, 0, ptr_diff(native, null()))
    dynamic_library_close(load_ptr(handle_slot, 0))
    return rc


@c_abi_export("pcc_metal_buffer_runtime_length_prebuilt")
def pcc_metal_buffer_runtime_length_prebuilt(
    runtime_library_path,
    buffer_ptr: i64,
    out_nbytes,
) -> i64:
    if buffer_ptr == 0:
        return -12
    if ptr_is_null(out_nbytes):
        return -10
    store_i64(out_nbytes, 0, 0)
    handle_slot = stack_alloc(8)
    symbol_slot = stack_alloc(8)
    load_rc = _load_symbol(
        runtime_library_path,
        cstr("pcc_metal_buffer_runtime_length"),
        handle_slot,
        symbol_slot,
    )
    if load_rc != 0:
        return load_rc
    rc = call_i64_ptr2(
        load_ptr(symbol_slot, 0),
        int_to_ptr(buffer_ptr),
        out_nbytes,
    )
    dynamic_library_close(load_ptr(handle_slot, 0))
    return rc


@c_abi_export("pcc_metal_buffer_runtime_write_prebuilt")
def pcc_metal_buffer_runtime_write_prebuilt(
    runtime_library_path,
    buffer_ptr: i64,
    offset: i64,
    src,
    nbytes: i64,
) -> i64:
    if buffer_ptr == 0:
        return -12
    if ptr_is_null(src) and nbytes > 0:
        return -11
    return _buffer_transfer(
        runtime_library_path,
        cstr("pcc_metal_buffer_runtime_write"),
        buffer_ptr,
        offset,
        src,
        nbytes,
    )


@c_abi_export("pcc_metal_buffer_runtime_read_prebuilt")
def pcc_metal_buffer_runtime_read_prebuilt(
    runtime_library_path,
    buffer_ptr: i64,
    offset: i64,
    dst,
    nbytes: i64,
) -> i64:
    if buffer_ptr == 0:
        return -12
    if ptr_is_null(dst) and nbytes > 0:
        return -11
    return _buffer_transfer(
        runtime_library_path,
        cstr("pcc_metal_buffer_runtime_read"),
        buffer_ptr,
        offset,
        dst,
        nbytes,
    )


@c_abi_export("pcc_freestanding_metal_buffer_transfer")
def _buffer_transfer(
    runtime_library_path,
    symbol,
    buffer_ptr: i64,
    offset: i64,
    data,
    nbytes: i64,
) -> i64:
    handle_slot = stack_alloc(8)
    symbol_slot = stack_alloc(8)
    load_rc = _load_symbol(
        runtime_library_path,
        symbol,
        handle_slot,
        symbol_slot,
    )
    if load_rc != 0:
        return load_rc
    rc = call_i64_ptr_i64_ptr_i64(
        load_ptr(symbol_slot, 0),
        int_to_ptr(buffer_ptr),
        offset,
        data,
        nbytes,
    )
    dynamic_library_close(load_ptr(handle_slot, 0))
    return rc


@c_abi_export("pcc_metal_buffer_runtime_release_prebuilt")
def pcc_metal_buffer_runtime_release_prebuilt(
    runtime_library_path,
    buffer_ptr: i64,
) -> i64:
    if buffer_ptr == 0:
        return -12
    handle_slot = stack_alloc(8)
    symbol_slot = stack_alloc(8)
    load_rc = _load_symbol(
        runtime_library_path,
        cstr("pcc_metal_buffer_runtime_release"),
        handle_slot,
        symbol_slot,
    )
    if load_rc != 0:
        return load_rc
    rc = call_i64_ptr1(load_ptr(symbol_slot, 0), int_to_ptr(buffer_ptr))
    dynamic_library_close(load_ptr(handle_slot, 0))
    return rc
