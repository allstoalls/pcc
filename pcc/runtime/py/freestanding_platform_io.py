"""Freestanding process/file-descriptor primitives authored in pcc-Python.

Darwin lowers these helpers to the explicitly named libSystem ABI. Linux
x86_64 lowers the same source to raw syscalls, so the generated object has no
libc dependency there.
"""

from pcc import i64
from pcc.extern import c_abi_export
from pcc.unsafe import close, getpid, pwrite_file, read, truncate_file, write

__pcc_freestanding__ = True


@c_abi_export("pcc_platform_read")
def pcc_platform_read(fd: i64, buffer, size: i64) -> i64:
    return read(fd, buffer, size)


@c_abi_export("pcc_platform_write")
def pcc_platform_write(fd: i64, buffer, size: i64) -> i64:
    return write(fd, buffer, size)


@c_abi_export("pcc_platform_pwrite")
def pcc_platform_pwrite(fd: i64, buffer, size: i64, offset: i64) -> i64:
    return pwrite_file(fd, buffer, size, offset)


@c_abi_export("pcc_platform_ftruncate")
def pcc_platform_ftruncate(fd: i64, length: i64) -> i64:
    return truncate_file(fd, length)


@c_abi_export("pcc_platform_close")
def pcc_platform_close(fd: i64) -> i64:
    return close(fd)


@c_abi_export("pcc_platform_getpid")
def pcc_platform_getpid() -> i64:
    return getpid()
