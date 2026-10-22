"""Owned Win64 RSS observations and atomic worker report publication.

PROCESS_MEMORY_COUNTERS has two DWORDs followed by eight SIZE_Ts: 72 bytes
on the supported 64-bit Windows target, with peak/current working sets at
offsets 8/16. K32GetProcessMemoryInfo returns BOOL; zero is an error, never
a zero-byte sample. See Microsoft's psapi.h PROCESS_MEMORY_COUNTERS and
GetProcessMemoryInfo reference. This is working-set RSS, not commit charge.
"""

from pcc import i64
from pcc.extern import c_abi_export, c_int, c_int64, c_ptr, extern
from pcc.unsafe import (
    free, int_to_ptr, load_i8, load_i32, load_i64, load_ptr, malloc, memcpy, memset, null, ptr_add,
    ptr_is_null, ptr_to_int, stack_alloc, store_i32, store_i64, store_ptr,
)


__pcc_freestanding__ = True

GetCurrentProcess = extern("GetCurrentProcess", (), c_ptr)
K32GetProcessMemoryInfo = extern("K32GetProcessMemoryInfo", (c_ptr, c_ptr, c_int), c_int)
GetFullPathNameW = extern("GetFullPathNameW", (c_ptr, c_int, c_ptr, c_ptr), c_int)
CreateFileW = extern("CreateFileW", (c_ptr, c_int, c_int, c_ptr, c_int, c_int, c_ptr), c_ptr)
GetFileInformationByHandleEx = extern("GetFileInformationByHandleEx", (c_ptr, c_int, c_ptr, c_int), c_int)
NtSetInformationFile = extern("NtSetInformationFile", (c_ptr, c_ptr, c_ptr, c_int, c_int), c_int)
RtlNtStatusToDosError = extern("RtlNtStatusToDosError", (c_int,), c_int)
CloseHandle = extern("CloseHandle", (c_ptr,), c_int)
utf16 = extern("pcc_win_utf16", (c_ptr,), c_ptr)
GetLastError = extern("GetLastError", (), c_int)
error_code = extern("pcc_win_error_code", (c_int64,), c_int64)
component_valid = extern("pcc_win_tempdir_component_valid", (c_ptr, c_int64), c_int64)
dos_leaf_valid = extern("pcc_win_tempdir_dos_leaf_valid", (c_ptr, c_int64), c_int64)

_COUNTERS_SIZE = 72
_PEAK_WORKING_SET_OFFSET = 8
_WORKING_SET_OFFSET = 16
_RENAME_HEADER_SIZE = 24
_RENAME_NAME_OFFSET = 20
_RENAME_IO_STATUS_SIZE = 16
_RENAME_ATTRIBUTE_TAG_SIZE = 8
_RENAME_CLEANUP_ERROR_BASE = 65536
# Caller-owned diagnostic record: three i64 slots, no shared or TLS state.
_RENAME_DIAGNOSTIC_SIZE = 24
_RENAME_STAGE_OFFSET = 0
_RENAME_WINERROR_OFFSET = 8
_RENAME_CLOSE_WINERROR_OFFSET = 16
_RENAME_DESTINATION_PATH = 1
_RENAME_SOURCE_PATH = 2
_RENAME_SOURCE_VALIDATION = 3
_RENAME_BUFFER_ALLOCATION = 4
_RENAME_OPEN_SOURCE = 5
_RENAME_RENAME = 6
_RENAME_CLOSE = 7
_RENAME_SOURCE_INFORMATION = 8


@c_abi_export("pcc_windows_rss_bytes")
def _windows_rss_bytes(want_peak: i64) -> i64:
    counters = stack_alloc(_COUNTERS_SIZE)
    memset(counters, 0, _COUNTERS_SIZE)
    store_i32(counters, 0, _COUNTERS_SIZE)
    if K32GetProcessMemoryInfo(GetCurrentProcess(), counters, _COUNTERS_SIZE) == 0:
        return -1
    offset: i64 = _PEAK_WORKING_SET_OFFSET if want_peak != 0 else _WORKING_SET_OFFSET
    return load_i64(counters, offset)


@c_abi_export("pcc_os_current_rss_bytes")
def pcc_os_current_rss_bytes() -> i64:
    return _windows_rss_bytes(0)


@c_abi_export("pcc_os_peak_rss_bytes")
def pcc_os_peak_rss_bytes() -> i64:
    return _windows_rss_bytes(1)


@c_abi_export("pcc_windows_worker_resource_full_path")
def _worker_resource_full_path(path: c_ptr, output: c_ptr) -> i64:
    """Keep the original path-helper ABI and ownership contract."""
    winerror_output = stack_alloc(8)
    return _worker_resource_full_path_detailed(path, output, winerror_output)


@c_abi_export("pcc_windows_worker_resource_full_path_detailed")
def _worker_resource_full_path_detailed(path: c_ptr, output: c_ptr,
                                       winerror_output: c_ptr) -> i64:
    """Return the original path status and snapshot any failing WinAPI DWORD."""
    store_i64(winerror_output, 0, 0)
    store_ptr(output, 0, null())
    wide = utf16(path)
    if ptr_is_null(wide):
        return -22
    size: i64 = GetFullPathNameW(wide, 0, null(), null())
    if size <= 0:
        code: i64 = GetLastError()
        code = code & 4294967295
        store_i64(winerror_output, 0, code)
        failure: i64 = error_code(code)
        free(wide)
        return failure
    # Bound both allocation arithmetic and the DWORD rename-buffer length.
    # Windows native full paths have at most 32767 UTF-16 code units.
    if size > 32768:
        free(wide)
        return -36
    absolute = malloc(size * 2)
    if ptr_is_null(absolute):
        free(wide)
        return -12
    actual: i64 = GetFullPathNameW(wide, size, absolute, null())
    failure: i64 = -36
    if actual == 0:
        code: i64 = GetLastError()
        code = code & 4294967295
        store_i64(winerror_output, 0, code)
        failure = error_code(code)
    free(wide)
    if actual <= 0 or actual >= size:
        free(absolute)
        return failure
    store_ptr(output, 0, absolute)
    return actual


@c_abi_export("pcc_worker_resource_replace")
def pcc_worker_resource_replace(source: c_ptr, destination: c_ptr) -> i64:
    """Replace only a report's closed .tmp, retaining any open old snapshot.

    Inputs are UTF-8 views held by the caller's c_str leases. This leaf never
    allocates managed objects. Zero confirms rename and handle close. Negative
    errno means no commit and confirmed cleanup (or no handle was opened).
    A positive status encodes primary_errno * 65536 + close_errno: zero primary
    means committed; nonzero primary means rename failed. Neither may retry.
    """
    diagnostic = stack_alloc(_RENAME_DIAGNOSTIC_SIZE)
    return pcc_worker_resource_replace_detailed(source, destination, ptr_to_int(diagnostic))


@c_abi_export("pcc_worker_resource_replace_detailed")
def pcc_worker_resource_replace_detailed(source: c_ptr, destination: c_ptr,
                                         diagnostic_address: i64) -> i64:
    """Use the original status protocol plus a caller-owned 24-byte record.

    The caller provides three writable i64 slots: failure stage, primary
    Win32 DWORD, close Win32 DWORD. Zero means no corresponding failure/API
    code. The record is initialized for every call, including all early exits.
    Capture each DWORD before mapping errno or performing another API/cleanup.
    Nothing can overwrite a different invocation's record during cleanup.
    """
    # Application raw addresses cross extern as integers, never managed c_ptr.
    diagnostic = int_to_ptr(diagnostic_address)
    memset(diagnostic, 0, _RENAME_DIAGNOSTIC_SIZE)
    paths = stack_alloc(16)
    memset(paths, 0, 16)
    winerror_output = ptr_add(diagnostic, _RENAME_WINERROR_OFFSET)
    destination_length: i64 = _worker_resource_full_path_detailed(
        destination, ptr_add(paths, 8), winerror_output)
    if destination_length < 0:
        store_i64(diagnostic, _RENAME_STAGE_OFFSET, _RENAME_DESTINATION_PATH)
        return destination_length
    source_length: i64 = _worker_resource_full_path_detailed(source, paths, winerror_output)
    if source_length < 0:
        store_i64(diagnostic, _RENAME_STAGE_OFFSET, _RENAME_SOURCE_PATH)
        free(load_ptr(paths, 8))
        return source_length
    a = load_ptr(paths, 0)
    b = load_ptr(paths, 8)
    valid: i64 = 1 if source_length == destination_length + 4 else 0
    index: i64 = 0
    while valid and index < destination_length * 2:
        if load_i8(a, index) != load_i8(b, index):
            valid = 0
        index = index + 1
    offset: i64 = destination_length * 2
    if valid:
        valid = (load_i8(a, offset) == 46 and load_i8(a, offset + 1) == 0
                 and load_i8(a, offset + 2) == 116 and load_i8(a, offset + 3) == 0
                 and load_i8(a, offset + 4) == 109 and load_i8(a, offset + 5) == 0
                 and load_i8(a, offset + 6) == 112 and load_i8(a, offset + 7) == 0)
    leaf_offset: i64 = 0
    index = 0
    while valid and index < destination_length * 2:
        low: i64 = load_i8(b, index)
        if (low == 92 or low == 47) and load_i8(b, index + 1) == 0:
            leaf_offset = index + 2
        index = index + 2
    leaf = ptr_add(b, leaf_offset)
    leaf_length: i64 = destination_length * 2 - leaf_offset
    # NT simple names must not introduce a namespace the Win32 source open
    # would interpret differently (streams, devices, trailing dots/spaces).
    if valid:
        valid = component_valid(leaf, leaf_length)
    if valid:
        valid = dos_leaf_valid(leaf, leaf_length)
    if not valid:
        store_i64(diagnostic, _RENAME_STAGE_OFFSET, _RENAME_SOURCE_VALIDATION)
        free(a)
        free(b)
        return -22
    size: i64 = _RENAME_HEADER_SIZE + leaf_length
    information = malloc(size)
    if ptr_is_null(information):
        store_i64(diagnostic, _RENAME_STAGE_OFFSET, _RENAME_BUFFER_ALLOCATION)
        free(a)
        free(b)
        return -12
    memset(information, 0, size)
    # Win64 FILE_RENAME_INFORMATION: Flags@0, RootDirectory@8, Length@16,
    # Name@20. Native simple-name + NULL root renames in the source directory.
    # A full destination (or non-NULL root) asks IopOpenLinkOrRenameTarget to
    # open a target parent, which can conflict with the retained TemporaryDirectory
    # owner. Keep that owner's access, sharing and lifetime unchanged.
    # Win32 SetFileInformationByHandle relative names are cwd-relative, so it
    # cannot substitute for the native same-directory operation here.
    # REPLACE_IF_EXISTS | POSIX_SEMANTICS; never IGNORE_READONLY_ATTRIBUTE.
    store_i32(information, 0, 3)
    store_i32(information, 16, leaf_length)
    memcpy(ptr_add(information, _RENAME_NAME_OFFSET), leaf, leaf_length)
    # DELETE | FILE_READ_ATTRIBUTES, all share modes, noninheritable,
    # OPEN_EXISTING, NORMAL | OPEN_REPARSE_POINT. Attribute access validates
    # this handle; it changes neither file permissions nor the directory owner.
    handle = CreateFileW(a, 65664, 7, null(), 3, 2097280, null())
    status: i64 = 0
    if ptr_to_int(handle) == -1:
        code: i64 = GetLastError()
        code = code & 4294967295
        store_i64(diagnostic, _RENAME_STAGE_OFFSET, _RENAME_OPEN_SOURCE)
        store_i64(diagnostic, _RENAME_WINERROR_OFFSET, code)
        status = error_code(code)
    else:
        # A followed source link could move the same-directory destination to
        # its target's parent. Inspect the exact no-follow source handle and
        # reject every reparse tag before any rename can commit.
        attributes = stack_alloc(_RENAME_ATTRIBUTE_TAG_SIZE)
        memset(attributes, 0, _RENAME_ATTRIBUTE_TAG_SIZE)
        if GetFileInformationByHandleEx(handle, 9, attributes, _RENAME_ATTRIBUTE_TAG_SIZE) == 0:
            code: i64 = GetLastError()
            code = code & 4294967295
            store_i64(diagnostic, _RENAME_STAGE_OFFSET, _RENAME_SOURCE_INFORMATION)
            store_i64(diagnostic, _RENAME_WINERROR_OFFSET, code)
            status = error_code(code)
        elif load_i32(attributes, 0) & 1024:
            store_i64(diagnostic, _RENAME_STAGE_OFFSET, _RENAME_SOURCE_VALIDATION)
            status = -40
        if status == 0:
            # CreateFileW without OVERLAPPED returns a synchronous handle.
            # FileRenameInformationEx=65; unsupported filesystems fail, no fallback.
            io_status = stack_alloc(_RENAME_IO_STATUS_SIZE)
            memset(io_status, 0, _RENAME_IO_STATUS_SIZE)
            native_status: i64 = NtSetInformationFile(handle, io_status, information, size, 65)
            if native_status != 0:
                # NT calls do not set Win32 LastError. Preserve the mapped original
                # DWORD before errno conversion or CloseHandle can clobber TLS.
                code: i64 = RtlNtStatusToDosError(native_status)
                code = code & 4294967295
                store_i64(diagnostic, _RENAME_STAGE_OFFSET, _RENAME_RENAME)
                store_i64(diagnostic, _RENAME_WINERROR_OFFSET, code)
                status = error_code(code)
        if CloseHandle(handle) == 0:
            code: i64 = GetLastError()
            code = code & 4294967295
            store_i64(diagnostic, _RENAME_CLOSE_WINERROR_OFFSET, code)
            if status == 0:
                store_i64(diagnostic, _RENAME_STAGE_OFFSET, _RENAME_CLOSE)
            cleanup_error: i64 = 0 - error_code(code)
            status = (0 - status) * _RENAME_CLEANUP_ERROR_BASE + cleanup_error
    free(information)
    free(a)
    free(b)
    return status
