"""Freestanding uname and CPU-count platform primitives."""

from pcc import i64
from pcc.extern import c_abi_export
from pcc.unsafe import (
    cpu_query,
    load_i8,
    logical_shift_right_i64,
    stack_alloc,
    uname,
    uname_field,
)

__pcc_freestanding__ = True


@c_abi_export("pcc_platform_uname")
def pcc_platform_uname(buffer) -> i64:
    return uname(buffer)


@c_abi_export("pcc_platform_uname_field")
def pcc_platform_uname_field(buffer, index: i64):
    return uname_field(buffer, index)


@c_abi_export("pcc_platform_cpu_count")
def pcc_platform_cpu_count() -> i64:
    affinity = stack_alloc(128)
    raw = cpu_query(affinity, 128)
    if raw < 0:
        return -raw
    if raw == 0:
        return 0
    count: i64 = 0
    byte_index: i64 = 0
    while byte_index < raw:
        value = load_i8(affinity, byte_index)
        bit_index: i64 = 0
        while bit_index < 8:
            count = count + (logical_shift_right_i64(value, bit_index) & 1)
            bit_index = bit_index + 1
        byte_index = byte_index + 1
    return count
