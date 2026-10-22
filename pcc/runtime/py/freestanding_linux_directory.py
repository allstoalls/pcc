"""Owned Linux directory streams over getdents64."""

from pcc import i64
from pcc.extern import (
    c_abi_export, c_int32, c_ptr, c_void, extern,
)
from pcc.unsafe import (
    malloc, free, null, ptr_is_null, ptr_add, load_i8, load_i64,
    store_i64, close, syscall6, target_platform_machine,
)

__pcc_freestanding__ = True

pcc_errno_set = extern("pcc_errno_set", (c_int32,), c_void)


@c_abi_export("pcc_linux_directory_open")
def directory_open(path: c_ptr) -> c_ptr:
    arm: i64 = 1 if load_i8(target_platform_machine(), 0) == 97 else 0
    # O_DIRECTORY | O_CLOEXEC: AArch64 (16384 | 524288), x86-64 (65536 | 524288).
    flags: i64 = 540672 if arm else 589824
    fd: i64 = syscall6(56 if arm else 257, -100, path, flags, 0, 0, 0)
    if fd < 0:
        pcc_errno_set(0 - fd)
        return null()
    stream = malloc(32800)
    if ptr_is_null(stream):
        close(fd)
        pcc_errno_set(12)
        return null()
    store_i64(stream, 0, fd)
    store_i64(stream, 8, 0)
    store_i64(stream, 16, 0)
    store_i64(stream, 24, 0)
    return stream


@c_abi_export("pcc_linux_directory_next")
def directory_next(stream: c_ptr) -> c_ptr:
    if load_i64(stream, 24) < 0:
        return null()
    position: i64 = load_i64(stream, 8)
    size: i64 = load_i64(stream, 16)
    if position >= size:
        arm: i64 = 1 if load_i8(target_platform_machine(), 0) == 97 else 0
        while True:
            size = syscall6(61 if arm else 217, load_i64(stream, 0), ptr_add(stream, 32), 32768, 0, 0, 0)
            if size != -4:
                break
        if size <= 0:
            store_i64(stream, 24, size)
            return null()
        position = 0
        store_i64(stream, 16, size)
    entry = ptr_add(stream, 32 + position)
    if size - position < 20:
        store_i64(stream, 24, -5)
        return null()
    record_size: i64 = (load_i8(entry, 16) & 255) | ((load_i8(entry, 17) & 255) << 8)
    if record_size < 20 or position + record_size > size:
        store_i64(stream, 24, -5)
        return null()
    end: i64 = 19
    while end < record_size and load_i8(entry, end) != 0:
        end = end + 1
    if end == record_size:
        store_i64(stream, 24, -5)
        return null()
    store_i64(stream, 8, position + record_size)
    return ptr_add(entry, 19)


@c_abi_export("pcc_linux_directory_error")
def directory_error(stream: c_ptr) -> i64:
    return load_i64(stream, 24)


@c_abi_export("pcc_linux_directory_close")
def directory_close(stream: c_ptr) -> i64:
    result: i64 = close(load_i64(stream, 0))
    free(stream)
    return result
