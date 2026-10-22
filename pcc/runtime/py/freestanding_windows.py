"""Windows platform primitives over named system DLL ABIs, without a CRT.

Public pcc paths remain UTF-8. Only the platform boundary converts to UTF-16.
File descriptors above the three standard streams are native HANDLE values.
"""

from pcc import i64
from pcc.extern import (
    c_abi_export, c_abi_typed_export, c_int, c_int32, c_int64, c_ptr, c_void, extern,
)
from pcc.unsafe import (
    int_to_ptr, ptr_to_int, ptr_add, ptr_is_null, null, stack_alloc,
    load_i8, load_i32, load_i64, load_ptr, store_i8, store_i32, store_i64,
    store_ptr, malloc, free, cstr,
    define_global_ptr_null, define_global_i64, define_global_i8,
    define_global_i32, define_global_null_ptr_array,
    define_thread_local_i32, define_thread_local_ptr_null,
    global_addr, global_load_ptr, global_store_ptr,
    atomic_load_i64, atomic_store_i64, atomic_test_and_set, atomic_clear,
    atomic_cas_i32, atomic_store_i32, call_void_ptr0, ptr_diff,
    f64_div,
    i64_to_float,
)

__pcc_freestanding__ = True

VirtualAlloc = extern("VirtualAlloc", (c_ptr, c_int64, c_int, c_int), c_ptr)
VirtualFree = extern("VirtualFree", (c_ptr, c_int64, c_int), c_int)
GetLastError = extern("GetLastError", (), c_int)
FormatMessageW = extern("FormatMessageW", (c_int, c_ptr, c_int, c_int, c_ptr, c_int, c_ptr), c_int)
GetConsoleMode = extern("GetConsoleMode", (c_ptr, c_ptr), c_int)
BCryptGenRandom = extern("BCryptGenRandom", (c_ptr, c_ptr, c_int, c_int), c_int)
platform_abort = extern("pcc_platform_abort", (), c_void)
pcc_errno_set = extern("pcc_errno_set", (c_int32,), c_void)
GetStdHandle = extern("GetStdHandle", (c_int,), c_ptr)
ReadFile = extern("ReadFile", (c_ptr, c_ptr, c_int, c_ptr, c_ptr), c_int)
WriteFile = extern("WriteFile", (c_ptr, c_ptr, c_int, c_ptr, c_ptr), c_int)
CloseHandle = extern("CloseHandle", (c_ptr,), c_int)
closesocket = extern("closesocket", (c_int64,), c_int)
SetHandleInformation = extern("SetHandleInformation", (c_ptr, c_int, c_int), c_int)
GetFileType = extern("GetFileType", (c_ptr,), c_int)
PeekNamedPipe = extern("PeekNamedPipe", (c_ptr, c_ptr, c_int, c_ptr, c_ptr, c_ptr), c_int)
GetTickCount64 = extern("GetTickCount64", (), c_int64)
socket_nonblocking = extern("pcc_win_socket_nonblocking", (c_int64, c_int64), c_int64)
socket_poll = extern("pcc_win_socket_poll", (c_int64, c_int64, c_int64), c_int64)
CreateFileW = extern("CreateFileW", (c_ptr, c_int, c_int, c_ptr, c_int, c_int, c_ptr), c_ptr)
SetFileTime = extern("SetFileTime", (c_ptr, c_ptr, c_ptr, c_ptr), c_int)
SetFilePointerEx = extern("SetFilePointerEx", (c_ptr, c_int64, c_ptr, c_int), c_int)
FlushFileBuffers = extern("FlushFileBuffers", (c_ptr,), c_int)
GetCurrentProcessId = extern("GetCurrentProcessId", (), c_int)
GetFileAttributesW = extern("GetFileAttributesW", (c_ptr,), c_int)
SetFileAttributesW = extern("SetFileAttributesW", (c_ptr, c_int), c_int)
GetFileAttributesExW = extern("GetFileAttributesExW", (c_ptr, c_int, c_ptr), c_int)
MoveFileExW = extern("MoveFileExW", (c_ptr, c_ptr, c_int), c_int)
CreateDirectoryW = extern("CreateDirectoryW", (c_ptr, c_ptr), c_int)
RemoveDirectoryW = extern("RemoveDirectoryW", (c_ptr,), c_int)
DeleteFileW = extern("DeleteFileW", (c_ptr,), c_int)
GetCurrentDirectoryW = extern("GetCurrentDirectoryW", (c_int, c_ptr), c_int)
MultiByteToWideChar = extern("MultiByteToWideChar", (c_int, c_int, c_ptr, c_int, c_ptr, c_int), c_int)
WideCharToMultiByte = extern("WideCharToMultiByte", (c_int, c_int, c_ptr, c_int, c_ptr, c_int, c_ptr, c_ptr), c_int)
GetSystemTimeAsFileTime = extern("GetSystemTimeAsFileTime", (c_ptr,), c_void)
QueryPerformanceCounter = extern("QueryPerformanceCounter", (c_ptr,), c_int)
QueryPerformanceFrequency = extern("QueryPerformanceFrequency", (c_ptr,), c_int)
Sleep = extern("Sleep", (c_int,), c_void)
ExitProcess = extern("ExitProcess", (c_int,), c_void)
GetActiveProcessorCount = extern("GetActiveProcessorCount", (c_int,), c_int)
LoadLibraryW = extern("LoadLibraryW", (c_ptr,), c_ptr)
GetProcAddress = extern("GetProcAddress", (c_ptr, c_ptr), c_ptr)
FreeLibrary = extern("FreeLibrary", (c_ptr,), c_int)
GetEnvironmentStringsW = extern("GetEnvironmentStringsW", (), c_ptr)
FreeEnvironmentStringsW = extern("FreeEnvironmentStringsW", (c_ptr,), c_int)
c_fflush = extern("fflush", (c_ptr,), c_int32)


# Match the owned Linux C lifecycle; no CRT participates on Windows.
# atexit node: next@0, callback@8, dynamically_allocated@16; 24-byte x64 layout.
# C requires at least 32 registrations: provide that many without allocation.
define_global_null_ptr_array("pcc_c_atexit_reserve", 96)
define_global_i64("pcc_c_atexit_reserve_used", 0)
define_global_ptr_null("pcc_c_atexit_head")
define_global_i32("pcc_c_atexit_lock", 0)
define_global_i32("pcc_c_exit_started", 0)


@c_abi_export("pcc_win_error")
def error() -> i64:
    value: i64 = GetLastError()
    return error_code(value)


@c_abi_export("pcc_win_error_code")
def error_code(value: i64) -> i64:
    # NT calls return NTSTATUS without setting Win32 last-error. Their callers
    # pass RtlNtStatusToDosError's result here instead of reading stale TLS.
    if value == 2 or value == 3:
        return -2
    if value == 5 or value == 32 or value == 33:
        return -13
    if value == 6:
        return -9
    if value == 8 or value == 14:
        return -12
    if value == 80 or value == 183:
        return -17
    if value == 87:
        return -22
    if value == 109:
        return -32
    if value == 112:
        return -28
    if value == 145:
        return -39
    if value == 206:
        return -36
    if value == 267:
        return -20
    if value == 681 or value == 1921 or value == 4395:
        return -40
    return -5


@c_abi_export("pcc_win_utf16")
def utf16(text: c_ptr) -> c_ptr:
    if ptr_is_null(text):
        return null()
    count: i64 = MultiByteToWideChar(65001, 8, text, -1, null(), 0)
    if count <= 0:
        return null()
    result = malloc(count * 2)
    if ptr_is_null(result):
        return null()
    if MultiByteToWideChar(65001, 8, text, -1, result, count) == 0:
        free(result)
        return null()
    return result


@c_abi_export("pcc_win_utf8")
def utf8(text: c_ptr) -> c_ptr:
    count: i64 = WideCharToMultiByte(65001, 128, text, -1, null(), 0, null(), null())
    if count <= 0:
        return null()
    result = malloc(count)
    if ptr_is_null(result):
        return null()
    if WideCharToMultiByte(65001, 128, text, -1, result, count, null(), null()) == 0:
        free(result)
        return null()
    return result


define_global_ptr_null("pcc_win_descriptors")
define_global_i64("pcc_win_next_descriptor", 3)
define_global_i8("pcc_win_descriptor_lock", 0)


@c_abi_export("pcc_windows_fd_lock")
def fd_lock() -> None:
    while atomic_test_and_set(global_addr("pcc_win_descriptor_lock"), 0, "acquire"):
        pass


@c_abi_export("pcc_windows_fd_unlock")
def fd_unlock() -> None:
    atomic_clear(global_addr("pcc_win_descriptor_lock"), 0, "release")


@c_abi_export("pcc_windows_close_raw")
def close_raw(value: c_ptr, kind: i64) -> i64:
    if kind:
        return closesocket(ptr_to_int(value))
    if CloseHandle(value):
        return 0
    return -1


@c_abi_export("pcc_windows_fd_register_value")
def fd_register_value(value: c_ptr, kind: i64) -> i64:
    record = malloc(48)
    if ptr_is_null(record):
        close_raw(value, kind)
        return -12
    fd_lock()
    fd: i64 = atomic_load_i64(global_addr("pcc_win_next_descriptor"), 0, "relaxed")
    if fd >= 2147483647:
        fd_unlock()
        close_raw(value, kind)
        free(record)
        return -24
    atomic_store_i64(global_addr("pcc_win_next_descriptor"), 0, fd + 1, "relaxed")
    store_i64(record, 8, fd)
    store_ptr(record, 16, value)
    store_i64(record, 24, kind)
    store_i64(record, 32, 0)
    store_i64(record, 40, 1)
    store_ptr(record, 0, global_load_ptr("pcc_win_descriptors"))
    global_store_ptr("pcc_win_descriptors", record)
    fd_unlock()
    return fd


@c_abi_export("pcc_win_fd_register")
def fd_register(value: c_ptr) -> i64:
    return fd_register_value(value, 0)


@c_abi_export("pcc_win_socket_register")
def socket_register(value: i64) -> i64:
    return fd_register_value(int_to_ptr(value), 1)


@c_abi_export("pcc_win_fd_handle")
def handle(fd: i64) -> c_ptr:
    if fd >= 0 and fd <= 2:
        return GetStdHandle(-10 - fd)
    fd_lock()
    record = global_load_ptr("pcc_win_descriptors")
    result = int_to_ptr(-1)
    while not ptr_is_null(record):
        if load_i64(record, 8) == fd:
            result = load_ptr(record, 16)
            break
        record = load_ptr(record, 0)
    fd_unlock()
    return result


@c_abi_export("pcc_win_page_alloc")
def page_alloc(size: i64) -> c_ptr:
    return VirtualAlloc(null(), size, 12288, 4)


@c_abi_export("pcc_win_page_free")
def page_free(ptr: c_ptr, size: i64) -> i64:
    if VirtualFree(ptr, 0, 32768) == 0:
        return error()
    return 0


@c_abi_export("pcc_win_read")
def read(fd: i64, buffer: c_ptr, size: i64) -> i64:
    count = stack_alloc(8)
    if size < 0:
        return -22
    if size > 2147483647:
        size = 2147483647
    if ReadFile(handle(fd), buffer, size, count, null()) == 0:
        if GetLastError() == 109:
            return 0
        return error()
    return load_i32(count, 0)


@c_abi_export("pcc_win_write")
def write(fd: i64, buffer: c_ptr, size: i64) -> i64:
    count = stack_alloc(8)
    if size < 0:
        return -22
    if size > 2147483647:
        size = 2147483647
    if WriteFile(handle(fd), buffer, size, count, null()) == 0:
        return error()
    return load_i32(count, 0)


@c_abi_export("pcc_win_close")
def close(fd: i64) -> i64:
    if 0 <= fd <= 2:
        if CloseHandle(GetStdHandle(-10 - fd)):
            return 0
        return error()
    fd_lock()
    previous = null()
    record = global_load_ptr("pcc_win_descriptors")
    while not ptr_is_null(record) and load_i64(record, 8) != fd:
        previous = record
        record = load_ptr(record, 0)
    if ptr_is_null(record):
        fd_unlock()
        return -9
    value = load_ptr(record, 16)
    kind: i64 = load_i64(record, 24)
    following = load_ptr(record, 0)
    if ptr_is_null(previous):
        global_store_ptr("pcc_win_descriptors", following)
    else:
        store_ptr(previous, 0, following)
    free(record)
    result: i64 = 0 if close_raw(value, kind) == 0 else error()
    fd_unlock()
    return result


@c_abi_export("pcc_win_seek_file")
def seek_file(fd: i64, offset: i64, whence: i64) -> i64:
    result = stack_alloc(8)
    if whence < 0 or whence > 2:
        return -22
    if SetFilePointerEx(handle(fd), offset, result, whence) == 0:
        return error()
    return load_i64(result, 0)


@c_abi_export("pcc_win_open_file")
def open_file(path: c_ptr, access_mode: i64, disposition: i64) -> i64:
    if access_mode < 0 or access_mode > 2 or disposition < 0 or disposition > 3:
        return -22
    wide = utf16(path)
    if ptr_is_null(wide):
        return -22
    access: i64 = 2147483648
    if access_mode == 1:
        access = 1073741824
    elif access_mode == 2:
        access = 3221225472
    create: i64 = 3
    if disposition == 1:
        create = 2
    elif disposition == 2:
        create = 4
        access = 4 if access_mode == 1 else 2147483652
    elif disposition == 3:
        create = 1
    result = CreateFileW(wide, access, 7, null(), create, 128, null())
    status: i64 = ptr_to_int(result)
    if status == -1:
        status = error()
    else:
        status = fd_register(result)
    free(wide)
    return status


@c_abi_export("pcc_win_open_readonly")
def open_readonly(path: c_ptr) -> i64:
    return open_file(path, 0, 0)


@c_abi_export("pcc_win_sync_file")
def sync_file(fd: i64) -> i64:
    if FlushFileBuffers(handle(fd)) == 0:
        return error()
    return 0


@c_abi_export("pcc_win_getpid")
def getpid() -> i64:
    return GetCurrentProcessId()


@c_abi_export("pcc_win_stat_kind")
def stat_kind(path: c_ptr) -> i64:
    wide = utf16(path)
    if ptr_is_null(wide):
        return 0
    attributes: i64 = GetFileAttributesW(wide)
    free(wide)
    if attributes == -1:
        return 0
    if attributes & 16:
        return 2
    return 1


@c_abi_export("pcc_win_stat_mtime")
def stat_mtime(path: c_ptr) -> float:
    wide = utf16(path)
    if ptr_is_null(wide):
        return f64_div(0.0, 0.0)
    data = stack_alloc(40)
    ok: i64 = GetFileAttributesExW(wide, 0, data)
    free(wide)
    if ok == 0:
        return f64_div(0.0, 0.0)
    ticks: i64 = load_i64(data, 20)
    return f64_div(i64_to_float(ticks - 116444736000000000), 10000000.0)


@c_abi_export("pcc_win_access")
def access(path: c_ptr, mode: i64) -> i64:
    wide = utf16(path)
    if ptr_is_null(wide):
        return -22
    attrs: i64 = GetFileAttributesW(wide)
    status: i64 = 0
    if attrs == -1:
        status = error()
    elif mode & 2 and attrs & 1:
        status = -13
    free(wide)
    return status


@c_abi_export("pcc_win_mkdir")
def mkdir(path: c_ptr, mode: i64) -> i64:
    wide = utf16(path)
    if ptr_is_null(wide):
        return -22
    status: i64 = 0
    if CreateDirectoryW(wide, null()) == 0:
        status = error()
    free(wide)
    return status


@c_abi_export("pcc_win_unlinkat")
def unlinkat(path: c_ptr, directory: i64) -> i64:
    wide = utf16(path)
    if ptr_is_null(wide):
        return -22
    attributes: i64 = GetFileAttributesW(wide)
    directory_link: i64 = 0
    if attributes != -1 and (attributes & 16) != 0 and (attributes & 1024) != 0:
        directory_link = 1
    status: i64 = 0
    if directory != 0 or directory_link != 0:
        status = RemoveDirectoryW(wide)
    else:
        status = DeleteFileW(wide)
    if status != 0:
        status = 0
    else:
        status = error()
    free(wide)
    return status


@c_abi_export("pcc_win_rename_file")
def rename_file(source: c_ptr, destination: c_ptr) -> i64:
    a = utf16(source)
    b = utf16(destination)
    status: i64 = -22
    if not ptr_is_null(a) and not ptr_is_null(b):
        status = 0 if MoveFileExW(a, b, 1) else error()
    free(a)
    free(b)
    return status


@c_abi_export("pcc_win_chmod_file")
def chmod_file(path: c_ptr, mode: i64) -> i64:
    wide = utf16(path)
    if ptr_is_null(wide):
        return -22
    attrs: i64 = GetFileAttributesW(wide)
    status: i64 = 0
    if attrs == -1:
        status = error()
    else:
        attrs = attrs & -2 if mode & 128 else attrs | 1
        if SetFileAttributesW(wide, attrs) == 0:
            status = error()
    free(wide)
    return status


@c_abi_export("pcc_win_filetime_from_seconds")
def filetime_from_seconds(seconds: i64) -> i64:
    # This leaf owns positive signed-i64 FILETIME ticks, not the entire
    # unsigned FILETIME domain. Check before either fixed-width operation.
    # floor((I64_MAX - 116444736000000000) / 10000000) = 910692730085.
    # The 1601 epoch itself gives zero, SetFileTime's do-not-change sentinel.
    if seconds <= -11644473600 or seconds > 910692730085:
        return -75
    return seconds * 10000000 + 116444736000000000


@c_abi_export("pcc_win_utime_file")
def utime_file(path: c_ptr, times: c_ptr) -> i64:
    # Internal tar-reader ABI: two explicit signed-i64 Unix seconds, as on
    # the existing LP64 POSIX path. No CRT, NULL/current-time or public
    # os.utime surface is provided. The string caller rejects embedded NUL.
    # 0 = success; -1 + errno = failure; 1 + errno = timestamps committed,
    # but CloseHandle failed. The caller must not describe that as unchanged.
    if ptr_is_null(path) or ptr_is_null(times):
        pcc_errno_set(22)
        return -1
    access_ticks: i64 = filetime_from_seconds(load_i64(times, 0))
    write_ticks: i64 = filetime_from_seconds(load_i64(times, 8))
    if access_ticks < 0 or write_ticks < 0:
        pcc_errno_set(75)
        return -1
    wide = utf16(path)
    if ptr_is_null(wide):
        pcc_errno_set(22)
        return -1
    # Metadata-only, shared, noninheritable existing-file/directory handle.
    # BACKUP_SEMANTICS permits directories; no privileges are enabled here.
    metadata_handle = CreateFileW(wide, 256, 7, null(), 3, 33554432, null())
    if ptr_to_int(metadata_handle) == -1:
        status: i64 = error()
        free(wide)
        pcc_errno_set(0 - status)
        return -1
    free(wide)
    # FILETIME has low/high DWORDs, not a guaranteed aligned i64 pointer.
    filetimes = stack_alloc(16)
    store_i32(filetimes, 0, access_ticks & 4294967295)
    store_i32(filetimes, 4, access_ticks >> 32)
    store_i32(filetimes, 8, write_ticks & 4294967295)
    store_i32(filetimes, 12, write_ticks >> 32)
    status: i64 = 0
    if SetFileTime(metadata_handle, null(), filetimes, ptr_add(filetimes, 8)) == 0:
        status = error()
    closed: i64 = CloseHandle(metadata_handle)
    if status != 0:
        # Preserve the primary SetFileTime error even if close also failed.
        pcc_errno_set(0 - status)
        return -1
    if closed == 0:
        status = error()
        pcc_errno_set(0 - status)
        return 1
    return 0


@c_abi_export("pcc_win_getcwd")
def getcwd(buffer: c_ptr, size: i64) -> c_ptr:
    count: i64 = GetCurrentDirectoryW(0, null())
    if count <= 0:
        pcc_errno_set(0 - error())
        return null()
    wide = malloc(count * 2)
    if ptr_is_null(wide):
        pcc_errno_set(12)  # ENOMEM in the owned cross-platform errno namespace.
        return null()
    got: i64 = GetCurrentDirectoryW(count, wide)
    ok: i64 = 0
    failure: i64 = 0
    if got <= 0:
        failure = 0 - error()
    elif got >= count:
        # A concurrent cwd change grew the required buffer between queries.
        failure = 34  # ERANGE, not a stale GetLastError value on success.
    else:
        ok = WideCharToMultiByte(65001, 128, wide, -1, buffer, size, null(), null())
        if not ok:
            failure = 34 if GetLastError() == 122 else 0 - error()
    # Capture the API status before freeing the conversion buffer.
    free(wide)
    if ok:
        return buffer
    pcc_errno_set(failure)
    return null()


@c_abi_typed_export("getcwd", "ptr", ("ptr", "u64"))
def getcwd_c(buffer: c_ptr, size: i64) -> c_ptr:
    return getcwd(buffer, size)


@c_abi_export("pcc_win_clock_gettime")
def clock_gettime(kind: i64, buffer: c_ptr) -> i64:
    temp = stack_alloc(16)
    seconds: i64 = 0
    nanos: i64 = 0
    if kind == 0:
        GetSystemTimeAsFileTime(temp)
        ticks: i64 = load_i64(temp, 0) - 116444736000000000
        seconds = ticks // 10000000
        nanos = (ticks % 10000000) * 100
    elif kind == 1:
        if QueryPerformanceCounter(temp) == 0 or QueryPerformanceFrequency(ptr_add(temp, 8)) == 0:
            return -5
        ticks: i64 = load_i64(temp, 0)
        frequency: i64 = load_i64(temp, 8)
        seconds = ticks // frequency
        nanos = ((ticks % frequency) * 1000000000) // frequency
    else:
        return -22
    store_i64(buffer, 0, seconds)
    store_i64(buffer, 8, nanos)
    return 0


@c_abi_export("pcc_win_nanosleep")
def nanosleep(request: c_ptr, remaining: c_ptr) -> i64:
    seconds: i64 = load_i64(request, 0)
    nanos: i64 = load_i64(request, 8)
    if seconds < 0 or nanos < 0 or nanos >= 1000000000:
        return -22
    milliseconds: i64 = seconds * 1000 + (nanos + 999999) // 1000000
    while milliseconds > 2147483647:
        Sleep(2147483647)
        milliseconds = milliseconds - 2147483647
    Sleep(milliseconds)
    if not ptr_is_null(remaining):
        store_i64(remaining, 0, 0)
        store_i64(remaining, 8, 0)
    return 0


@c_abi_export("pcc_win_cpu_query")
def cpu_query(buffer: c_ptr, size: i64) -> i64:
    count: i64 = GetActiveProcessorCount(65535)
    return 0 - count


@c_abi_export("pcc_win_process_exit")
def process_exit(status: i64) -> None:
    ExitProcess(status)


@c_abi_typed_export("_Exit", "void", ("i32",))
def immediate_exit(status: i64) -> None:
    # Immediate exit bypasses callbacks, finalizers and owned stdio buffers.
    while True:
        process_exit(status)


@c_abi_typed_export("_exit", "void", ("i32",))
def posix_immediate_exit(status: i64) -> None:
    immediate_exit(status)


@c_abi_export("pcc_c_atexit_acquire")
def _atexit_acquire() -> None:
    lock = global_addr("pcc_c_atexit_lock")
    while atomic_cas_i32(lock, 0, 0, 1, "acquire", "relaxed") != 0:
        pass


@c_abi_export("pcc_c_atexit_release")
def _atexit_release() -> None:
    atomic_store_i32(global_addr("pcc_c_atexit_lock"), 0, 0, "release")


@c_abi_typed_export("atexit", "i32", ("ptr",))
def atexit_c(callback: c_ptr) -> i64:
    # Callbacks are void(void) native function pointers. Registration is
    # serialized, and callbacks execute outside this lock so they may register
    # another callback, which becomes the next callback to run.
    _atexit_acquire()
    used: i64 = load_i64(global_addr("pcc_c_atexit_reserve_used"), 0)
    dynamic: i64 = 0
    if used < 32:
        node = ptr_add(global_addr("pcc_c_atexit_reserve"), used * 24)
        store_i64(global_addr("pcc_c_atexit_reserve_used"), 0, used + 1)
    else:
        node = malloc(24)
        if ptr_is_null(node):
            _atexit_release()
            return -1
        dynamic = 1
    store_ptr(node, 0, load_ptr(global_addr("pcc_c_atexit_head"), 0))
    store_ptr(node, 8, callback)
    store_i64(node, 16, dynamic)
    store_ptr(global_addr("pcc_c_atexit_head"), 0, node)
    _atexit_release()
    return 0


@c_abi_export("pcc_c_run_exit_callbacks")
def _run_exit_callbacks() -> None:
    while True:
        _atexit_acquire()
        node = load_ptr(global_addr("pcc_c_atexit_head"), 0)
        if ptr_is_null(node):
            _atexit_release()
            break
        store_ptr(global_addr("pcc_c_atexit_head"), 0, load_ptr(node, 0))
        callback = load_ptr(node, 8)
        dynamic: i64 = load_i64(node, 16)
        _atexit_release()
        if dynamic != 0:
            free(node)
        call_void_ptr0(callback)


@c_abi_typed_export("exit", "void", ("i32",))
def exit_c(status: i64) -> None:
    # A second/recursive exit is outside this C lifecycle contract; ensure it
    # cannot run the same callback twice or deadlock in a callback-held lock.
    if atomic_cas_i32(global_addr("pcc_c_exit_started"), 0, 0, 1,
                      "acq_rel", "acquire") != 0:
        immediate_exit(status)
    _run_exit_callbacks()
    # The owned PE linker supplies these bounds, including for empty arrays.
    # Both direct exit and return from C main reach this same finalizer path.
    finalizers = global_addr("__fini_array_start")
    remaining: i64 = ptr_diff(global_addr("__fini_array_end"), finalizers) // 8
    while remaining > 0:
        remaining = remaining - 1
        call_void_ptr0(load_ptr(finalizers, remaining * 8))
    # A finalizer may register an atexit callback, just like an exit callback.
    _run_exit_callbacks()
    c_fflush(null())
    # ExitProcess closes native handles; flushing owned FILE buffers is
    # part of normal exit only. A flush error does not change the status.
    immediate_exit(status)


define_thread_local_i32("pcc_win_loader_error", 0)
define_thread_local_ptr_null("pcc_win_loader_message")


@c_abi_export("pcc_win_dynamic_library_open")
def dynamic_library_open(path: c_ptr) -> c_ptr:
    wide = utf16(path)
    if ptr_is_null(wide):
        return null()
    result = LoadLibraryW(wide)
    error_code: i64 = 0
    if ptr_is_null(result):
        error_code = GetLastError()
    store_i32(global_addr("pcc_win_loader_error"), 0, error_code)
    free(wide)
    return result


@c_abi_export("pcc_win_dynamic_library_symbol")
def dynamic_library_symbol(library: c_ptr, name: c_ptr) -> c_ptr:
    result = GetProcAddress(library, name)
    error_code: i64 = 0
    if ptr_is_null(result):
        error_code = GetLastError()
    store_i32(global_addr("pcc_win_loader_error"), 0, error_code)
    return result


@c_abi_export("pcc_win_dynamic_library_close")
def dynamic_library_close(library: c_ptr) -> i64:
    status: i64 = FreeLibrary(library)
    error_code: i64 = 0
    if status == 0:
        error_code = GetLastError()
    store_i32(global_addr("pcc_win_loader_error"), 0, error_code)
    if status:
        return 0
    return -1


@c_abi_export("dlerror")
def dlerror() -> c_ptr:
    code: i64 = load_i32(global_addr("pcc_win_loader_error"), 0)
    store_i32(global_addr("pcc_win_loader_error"), 0, 0)
    if code == 0:
        return null()
    wide = stack_alloc(4096)
    if FormatMessageW(4608, null(), code, 0, wide, 2048, null()) == 0:
        return cstr("Windows system loader rejected the library or symbol")
    text = utf8(wide)
    if ptr_is_null(text):
        return cstr("Windows loader error: message conversion failed")
    old = global_load_ptr("pcc_win_loader_message")
    global_store_ptr("pcc_win_loader_message", text)
    free(old)
    return text


@c_abi_export("pcc_win_initial_environ")
def initial_environ() -> c_ptr:
    block = GetEnvironmentStringsW()
    if ptr_is_null(block):
        return null()
    count: i64 = 0
    offset: i64 = 0
    while load_i8(block, offset) != 0 or load_i8(block, offset + 1) != 0:
        count = count + 1
        while load_i8(block, offset) != 0 or load_i8(block, offset + 1) != 0:
            offset = offset + 2
        offset = offset + 2
    result = malloc((count + 1) * 8)
    if ptr_is_null(result):
        FreeEnvironmentStringsW(block)
        return null()
    offset = 0
    index: i64 = 0
    while index < count:
        text = utf8(ptr_add(block, offset))
        if ptr_is_null(text):
            cursor: i64 = 0
            while cursor < index:
                free(load_ptr(result, cursor * 8))
                cursor = cursor + 1
            free(result)
            FreeEnvironmentStringsW(block)
            return null()
        store_ptr(result, index * 8, text)
        while load_i8(block, offset) != 0 or load_i8(block, offset + 1) != 0:
            offset = offset + 2
        offset = offset + 2
        index = index + 1
    store_ptr(result, count * 8, null())
    FreeEnvironmentStringsW(block)
    return result


@c_abi_export("pcc_windows_descriptor_field")
def descriptor_field(fd: i64, offset: i64) -> i64:
    fd_lock()
    record = global_load_ptr("pcc_win_descriptors")
    result: i64 = -9
    while not ptr_is_null(record):
        if load_i64(record, 8) == fd:
            result = load_i64(record, offset)
            break
        record = load_ptr(record, 0)
    fd_unlock()
    return result


@c_abi_export("pcc_win_fd_control")
def fd_control(fd: i64, command: i64, value: i64) -> i64:
    if command == 1 or command == 3:
        field_offset: i64 = 32
        if command == 1:
            field_offset = 40
        return descriptor_field(fd, field_offset)
    if command != 2 and command != 4:
        return -22
    kind: i64 = descriptor_field(fd, 24)
    if kind < 0:
        return kind
    if command == 4 and kind:
        status: i64 = socket_nonblocking(fd, 1 if value & 2048 else 0)
        if status < 0:
            return status
    if command == 2:
        if SetHandleInformation(handle(fd), 1, 0 if value & 1 else 1) == 0:
            return error()
    fd_lock()
    record = global_load_ptr("pcc_win_descriptors")
    while not ptr_is_null(record):
        if load_i64(record, 8) == fd:
            store_i64(record, 40 if command == 2 else 32, value)
            fd_unlock()
            return 0
        record = load_ptr(record, 0)
    fd_unlock()
    return -9


@c_abi_export("pcc_win_poll_fd")
def poll_fd(fd: i64, events: i64, timeout: i64) -> i64:
    kind: i64 = 0 if fd <= 2 else descriptor_field(fd, 24)
    if kind < 0:
        return 32
    if kind:
        return socket_poll(fd, events, timeout)
    value = handle(fd)
    if GetFileType(value) != 3:
        return events & 5
    count = stack_alloc(4)
    started: i64 = GetTickCount64()
    while True:
        if PeekNamedPipe(value, null(), 0, null(), count, null()) == 0:
            if GetLastError() == 109:
                return 16
            return error()
        result: i64 = events & 4
        if load_i32(count, 0) > 0:
            result = result | (events & 1)
        if result or timeout == 0:
            return result
        if timeout > 0 and GetTickCount64() - started >= timeout:
            return 0
        Sleep(1)


@c_abi_export("pcc_win_poll_readable_pair")
def poll_readable_pair(fd0: i64, fd1: i64, timeout: i64) -> i64:
    started: i64 = GetTickCount64()
    while True:
        first: i64 = poll_fd(fd0, 1, 0)
        second: i64 = poll_fd(fd1, 1, 0)
        if first < 0:
            return first
        if second < 0:
            return second
        ready: i64 = 0
        if first & 57:
            ready = ready | 1
        if second & 57:
            ready = ready | 2
        if ready or timeout == 0:
            return ready
        if timeout > 0 and GetTickCount64() - started >= timeout:
            return 0
        Sleep(1)


@c_abi_export("mkdir")
def mkdir_c(path: c_ptr, mode: i64) -> i64:
    return mkdir(path, mode)


@c_abi_export("isatty")
def isatty(fd: i64) -> i64:
    mode = stack_alloc(4)
    if GetConsoleMode(handle(fd), mode):
        return 1
    return 0


@c_abi_export("arc4random_buf")
def arc4random_buf(output: c_ptr, size: i64) -> None:
    offset: i64 = 0
    while offset < size:
        chunk: i64 = size - offset
        if chunk > 2147483647:
            chunk = 2147483647
        if BCryptGenRandom(null(), ptr_add(output, offset), chunk, 2) < 0:
            platform_abort()
            return
        offset = offset + chunk
