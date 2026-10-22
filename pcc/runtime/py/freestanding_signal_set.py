"""Finite signal-set operations over the owned Linux/Windows ABI record."""

from pcc import i64
from pcc.extern import c_abi_export, c_int32, c_void, extern
from pcc.unsafe import ptr_is_null, load_i8, store_i8, memset

__pcc_freestanding__ = True

set_errno = extern("pcc_errno_set", (c_int32,), c_void)


@c_abi_export("pcc_signal_set_valid")
def valid(mask, number: i64) -> i64:
    if ptr_is_null(mask) or number < 1 or number > 64:
        set_errno(22)
        return 0
    return 1


@c_abi_export("sigaddset")
def sigaddset(mask, number: i64) -> i64:
    if not valid(mask, number):
        return -1
    offset: i64 = (number - 1) // 8
    bit: i64 = 1 << ((number - 1) % 8)
    store_i8(mask, offset, load_i8(mask, offset) | bit)
    return 0


@c_abi_export("sigdelset")
def sigdelset(mask, number: i64) -> i64:
    if not valid(mask, number):
        return -1
    offset: i64 = (number - 1) // 8
    bit: i64 = 1 << ((number - 1) % 8)
    store_i8(mask, offset, load_i8(mask, offset) & ~bit)
    return 0


@c_abi_export("sigismember")
def sigismember(mask, number: i64) -> i64:
    if not valid(mask, number):
        return -1
    offset: i64 = (number - 1) // 8
    bit: i64 = 1 << ((number - 1) % 8)
    if load_i8(mask, offset) & bit:
        return 1
    return 0


@c_abi_export("sigfillset")
def sigfillset(mask) -> i64:
    if ptr_is_null(mask):
        set_errno(22)
        return -1
    memset(mask, 0, 128)
    memset(mask, 255, 8)
    return 0
