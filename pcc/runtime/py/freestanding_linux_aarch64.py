"""AArch64 kernel structures adapted to pcc's shared Linux event ABI."""

from pcc import i64
from pcc.extern import c_abi_export, c_ptr
from pcc.unsafe import (
    page_alloc, page_free, ptr_is_null, load_i32, load_i64,
    store_i32, store_i64, syscall6,
)

__pcc_freestanding__ = True


@c_abi_export("pcc_linux_aarch64_epoll_wait")
def epoll_wait(fd: i64, output: c_ptr, capacity: i64, timeout: i64) -> i64:
    if capacity <= 0 or capacity > 134217727:
        return -22
    storage = page_alloc(capacity * 16)
    if ptr_is_null(storage):
        return -12
    result: i64 = syscall6(22, fd, storage, capacity, timeout, 0, 8)
    index: i64 = 0
    while index < result:
        store_i32(output, index * 12, load_i32(storage, index * 16))
        store_i64(output, index * 12 + 4, load_i64(storage, index * 16 + 8))
        index = index + 1
    page_free(storage, capacity * 16)
    return result
