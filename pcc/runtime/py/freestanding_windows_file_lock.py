"""Windows open-handle region locks; no CRT or user-space lock ownership map."""
from pcc import i64
from pcc.extern import c_abi_export, c_int, c_int64, c_ptr, extern
from pcc.unsafe import (
    ptr_is_null, ptr_to_int, stack_alloc, memset, store_i32, unsigned_div_i64,
)

__pcc_freestanding__ = True
handle = extern("pcc_win_fd_handle", (c_int64,), c_ptr)
error = extern("pcc_win_error", (), c_int64)
GetLastError = extern("GetLastError", (), c_int)
GetFileType = extern("GetFileType", (c_ptr,), c_int)
LockFileEx = extern("LockFileEx", (c_ptr, c_int, c_int, c_int, c_int, c_ptr), c_int)
UnlockFileEx = extern("UnlockFileEx", (c_ptr, c_int, c_int, c_int, c_ptr), c_int)


@c_abi_export("pcc_win_file_lock_region")
def file_lock_region(fd: i64, operation: i64, offset: i64, length: i64) -> i64:
    if fd < 0 or offset < 0 or length < 0 or length > 2147483647:
        return -22
    if operation != 0 and operation != 1:
        return -22
    file = handle(fd)
    if ptr_is_null(file) or ptr_to_int(file) == -1:
        return -9
    if GetFileType(file) != 1:
        return -9
    overlapped = stack_alloc(32)
    memset(overlapped, 0, 32)
    # Win64 OVERLAPPED: ULONG_PTR[2], DWORD Offset/OffsetHigh, HANDLE hEvent.
    store_i32(overlapped, 16, offset & 4294967295)
    store_i32(overlapped, 20, unsigned_div_i64(offset, 4294967296))
    status: i64 = 0
    if operation == 0:
        status = UnlockFileEx(file, 0, length, 0, overlapped)
    else:
        status = LockFileEx(file, 3, 0, length, 0, overlapped)
    if status != 0:
        return 0
    code: i64 = GetLastError()
    if code == 33 or code == 158:
        return -13
    return error()
