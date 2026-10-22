"""Windows path metadata, reparse points and OS identity."""

from pcc import i64
from pcc.extern import c_abi_export, c_ptr, c_int, c_int64, extern
from pcc.unsafe import (
    malloc, free, null, ptr_is_null, ptr_to_int, ptr_add, stack_alloc,
    load_i8, load_i32, load_i64, store_i8, store_i32, cstr,
)

__pcc_freestanding__ = True

CreateFileW = extern("CreateFileW", (c_ptr, c_int, c_int, c_ptr, c_int, c_int, c_ptr), c_ptr)
CloseHandle = extern("CloseHandle", (c_ptr,), c_int)
DeviceIoControl = extern("DeviceIoControl", (c_ptr, c_int, c_ptr, c_int, c_ptr, c_int, c_ptr, c_ptr), c_int)
GetComputerNameW = extern("GetComputerNameW", (c_ptr, c_ptr), c_int)
RtlGetVersion = extern("RtlGetVersion", (c_ptr,), c_int)
GetFinalPathNameByHandleW = extern("GetFinalPathNameByHandleW", (c_ptr, c_ptr, c_int, c_int), c_int)
GetLastError = extern("GetLastError", (), c_int)
GetFileAttributesW = extern("GetFileAttributesW", (c_ptr,), c_int)
GetFileInformationByHandleEx = extern("GetFileInformationByHandleEx", (c_ptr, c_int, c_ptr, c_int), c_int)
GetFullPathNameW = extern("GetFullPathNameW", (c_ptr, c_int, c_ptr, c_ptr), c_int)
utf16 = extern("pcc_win_utf16", (c_ptr,), c_ptr)
utf8 = extern("pcc_win_utf8", (c_ptr,), c_ptr)
error = extern("pcc_win_error", (), c_int64)


@c_abi_export("pcc_win_stat_size")
def stat_size(path: c_ptr) -> i64:
    if ptr_is_null(path):
        return -14
    wide = utf16(path)
    if ptr_is_null(wide):
        return -22
    # FILE_READ_ATTRIBUTES, full sharing, OPEN_EXISTING, BACKUP_SEMANTICS.
    # No OPEN_REPARSE_POINT: stat/getsize must follow symbolic links.
    handle = CreateFileW(wide, 128, 7, null(), 3, 33554432, null())
    failure: i64 = error() if ptr_to_int(handle) == -1 else 0
    free(wide)
    if failure:
        return failure
    info = stack_alloc(24)
    status: i64 = GetFileInformationByHandleEx(handle, 1, info, 24)
    result: i64 = 0
    if status == 0:
        result = error()
    elif load_i8(info, 21) == 0:
        # FILE_STANDARD_INFO.EndOfFile; directory st_size is zero on Windows.
        result = load_i64(info, 8)
    CloseHandle(handle)
    return result


@c_abi_export("pcc_win_is_symlink")
def is_symlink(path: c_ptr) -> i64:
    return 1 if lstat_kind(path) == 3 else 0


@c_abi_export("pcc_win_lstat_kind")
def lstat_kind(path: c_ptr) -> i64:
    if ptr_is_null(path):
        return 0
    wide = utf16(path)
    if ptr_is_null(wide):
        return 0
    # OPEN_REPARSE_POINT + BACKUP_SEMANTICS opens the link even if dangling.
    handle = CreateFileW(wide, 128, 7, null(), 3, 35651584, null())
    failed: i64 = 1 if ptr_to_int(handle) == -1 else 0
    free(wide)
    if failed:
        return 0
    info = stack_alloc(8)
    status: i64 = GetFileInformationByHandleEx(handle, 9, info, 8)
    result: i64 = 0
    if status != 0:
        # FILE_ATTRIBUTE_TAG_INFO.ReparseTag == IO_REPARSE_TAG_SYMLINK.
        # Junctions and other reparse points do not satisfy os.path.islink.
        if (load_i32(info, 4) & 4294967295) == 2684354572:
            result = 3
        elif load_i32(info, 0) & 16:
            result = 2
        else:
            result = 1
    CloseHandle(handle)
    return result


@c_abi_export("pcc_windows_system_u16")
def u16(buffer: c_ptr, offset: i64) -> i64:
    return (load_i8(buffer, offset) & 255) | ((load_i8(buffer, offset + 1) & 255) * 256)


@c_abi_export("pcc_windows_system_copy")
def copy(out: c_ptr, text: c_ptr, capacity: i64) -> i64:
    index: i64 = 0
    while index < capacity:
        value: i64 = load_i8(text, index)
        store_i8(out, index, value)
        if value == 0:
            return index
        index = index + 1
    return -34


@c_abi_export("pcc_win_readlink")
def readlink(path: c_ptr, output: c_ptr, capacity: i64) -> i64:
    wide = utf16(path)
    if ptr_is_null(wide):
        return -22
    handle = CreateFileW(wide, 0, 7, null(), 3, 35651584, null())
    failure: i64 = error() if ptr_to_int(handle) == -1 else 0
    free(wide)
    if failure:
        return failure
    buffer = malloc(16384)
    if ptr_is_null(buffer):
        CloseHandle(handle)
        return -12
    count = stack_alloc(4)
    status: i64 = DeviceIoControl(handle, 589992, null(), 0, buffer, 16384, count, null())
    if status == 0:
        free(buffer)
        CloseHandle(handle)
        return -22
    CloseHandle(handle)
    tag: i64 = load_i32(buffer, 0) & 4294967295
    base: i64 = 0
    if tag == 2684354572:
        base = 20
    elif tag == 2684354563:
        base = 16
    else:
        free(buffer)
        return -22
    offset: i64 = u16(buffer, 12)
    size: i64 = u16(buffer, 14)
    if size == 0:
        offset = u16(buffer, 8)
        size = u16(buffer, 10)
    if base + offset + size > load_i32(count, 0) or size & 1:
        free(buffer)
        return -5
    text = malloc(size + 2)
    if ptr_is_null(text):
        free(buffer)
        return -12
    index: i64 = 0
    while index < size:
        store_i8(text, index, load_i8(buffer, base + offset + index))
        index = index + 1
    store_i8(text, size, 0)
    store_i8(text, size + 1, 0)
    converted = utf8(text)
    free(text)
    free(buffer)
    if ptr_is_null(converted):
        return -22
    index = 0
    while index < capacity and load_i8(converted, index) != 0:
        store_i8(output, index, load_i8(converted, index))
        index = index + 1
    free(converted)
    return index


@c_abi_export("pcc_win_full_path")
def full_path(path: c_ptr, output: c_ptr, capacity: i64) -> c_ptr:
    wide = utf16(cstr(".") if load_i8(path, 0) == 0 else path)
    if ptr_is_null(wide):
        return null()
    size: i64 = GetFullPathNameW(wide, 0, null(), null())
    if size <= 0:
        free(wide)
        return null()
    buffer = malloc((size + 1) * 2)
    if ptr_is_null(buffer):
        free(wide)
        return null()
    actual: i64 = GetFullPathNameW(wide, size + 1, buffer, null())
    free(wide)
    if actual <= 0 or actual > size:
        free(buffer)
        return null()
    text = utf8(buffer)
    free(buffer)
    if ptr_is_null(text):
        return null()
    result: i64 = copy(output, text, capacity)
    free(text)
    if result >= 0:
        return output
    return null()


@c_abi_export("pcc_windows_system_decimal")
def decimal(output: c_ptr, value: i64) -> i64:
    temporary = stack_alloc(32)
    count: i64 = 0
    while True:
        store_i8(temporary, count, 48 + value % 10)
        value = value // 10
        count = count + 1
        if value == 0:
            break
    index: i64 = 0
    while index < count:
        store_i8(output, index, load_i8(temporary, count - index - 1))
        index = index + 1
    store_i8(output, count, 0)
    return count


@c_abi_export("pcc_win_uname")
def uname(buffer: c_ptr) -> i64:
    index: i64 = 0
    while index < 325:
        store_i8(buffer, index, 0)
        index = index + 1
    copy(buffer, cstr("Windows"), 65)
    copy(ptr_add(buffer, 260), cstr("x86_64"), 65)
    wide = stack_alloc(512)
    size = stack_alloc(4)
    store_i32(size, 0, 256)
    if GetComputerNameW(wide, size):
        text = utf8(wide)
        if not ptr_is_null(text):
            copy(ptr_add(buffer, 65), text, 65)
            free(text)
    version = stack_alloc(284)
    index = 0
    while index < 284:
        store_i8(version, index, 0)
        index = index + 1
    store_i32(version, 0, 284)
    if RtlGetVersion(version) != 0:
        return -5
    release = ptr_add(buffer, 130)
    offset: i64 = decimal(release, load_i32(version, 4))
    store_i8(release, offset, 46)
    offset = offset + 1
    offset = offset + decimal(ptr_add(release, offset), load_i32(version, 8))
    store_i8(release, offset, 46)
    decimal(ptr_add(release, offset + 1), load_i32(version, 12))
    copy(ptr_add(buffer, 195), release, 65)
    return 0


@c_abi_export("pcc_win_uname_field")
def uname_field(buffer: c_ptr, index: i64) -> c_ptr:
    if index < 0 or index >= 5:
        return null()
    return ptr_add(buffer, index * 65)


@c_abi_export("pcc_windows_system_final_path")
def final_path(handle: c_ptr, output: c_ptr, capacity: i64, keep_prefix: i64) -> c_ptr:
    """Write an opened file's resolved path; the caller owns the handle."""
    size: i64 = GetFinalPathNameByHandleW(handle, null(), 0, 0)
    if size <= 0:
        return null()
    buffer = malloc((size + 1) * 2)
    if ptr_is_null(buffer):
        return null()
    actual: i64 = GetFinalPathNameByHandleW(handle, buffer, size + 1, 0)
    if actual <= 0 or actual > size:
        free(buffer)
        return null()
    text = utf8(buffer)
    free(buffer)
    if ptr_is_null(text):
        return null()
    start: i64 = 0
    prefix: i64 = 0
    if not keep_prefix and load_i8(text, 0) == 92 and load_i8(text, 1) == 92 and load_i8(text, 2) == 63 and load_i8(text, 3) == 92:
        start = 4
        if load_i8(text, 4) == 85 and load_i8(text, 5) == 78 and load_i8(text, 6) == 67 and load_i8(text, 7) == 92:
            start = 8
            prefix = 2
    if capacity <= prefix:
        free(text)
        return null()
    if prefix:
        store_i8(output, 0, 92)
        store_i8(output, 1, 92)
    result: i64 = copy(ptr_add(output, prefix), ptr_add(text, start), capacity - prefix)
    free(text)
    if result >= 0:
        return output
    return null()


@c_abi_export("pcc_windows_system_path_root_length")
def path_root_length(path: c_ptr, length: i64) -> i64:
    """Keep drive and UNC share roots intact while walking up ancestors."""
    start: i64 = 0
    if length >= 4 and load_i8(path, 0) == 92 and load_i8(path, 1) == 92:
        if (load_i8(path, 2) == 63 or load_i8(path, 2) == 46) and load_i8(path, 3) == 92:
            start = 4
            if length >= 8 and (load_i8(path, 4) | 32) == 117 and (load_i8(path, 5) | 32) == 110 and (load_i8(path, 6) | 32) == 99 and load_i8(path, 7) == 92:
                start = 8
    if length >= start + 3 and load_i8(path, start + 1) == 58 and load_i8(path, start + 2) == 92:
        return start + 3
    if length >= 2 and load_i8(path, 0) == 92 and load_i8(path, 1) == 92:
        if start == 0:
            start = 2
        cursor: i64 = start
        components: i64 = 0
        while cursor < length:
            if load_i8(path, cursor) == 92:
                components = components + 1
                if components == 2:
                    return cursor + 1
            cursor = cursor + 1
        return length
    if length and load_i8(path, 0) == 92:
        return 1
    return 0


@c_abi_export("pcc_windows_system_resolve_reparse_tail")
def resolve_reparse_tail(path: c_ptr, end: i64, length: i64, output: c_ptr, capacity: i64, links: i64, keep_prefix: i64) -> c_ptr:
    # An existing reparse point may have a missing target, so opening it is
    # insufficient. Resolve the stored target and append the unresolved tail.
    target = malloc(131072)
    if ptr_is_null(target):
        return null()
    saved: i64 = load_i8(path, end)
    store_i8(path, end, 0)
    target_len: i64 = readlink(path, target, 131071)
    store_i8(path, end, saved)
    if target_len <= 0 or target_len >= 131071:
        free(target)
        return null()
    store_i8(target, target_len, 0)
    if target_len >= 4 and load_i8(target, 0) == 92 and load_i8(target, 1) == 63 and load_i8(target, 2) == 63 and load_i8(target, 3) == 92:
        store_i8(target, 1, 92)  # NT substitute name: \??\ -> \\?\
    prefix: i64 = end
    while prefix > 0 and load_i8(path, prefix - 1) != 92:
        prefix = prefix - 1
    if target_len >= 2 and (load_i8(target, 1) == 58 or (load_i8(target, 0) == 92 and load_i8(target, 1) == 92)):
        prefix = 0
    elif load_i8(target, 0) == 92 or load_i8(target, 0) == 47:
        # A rooted target keeps the link's drive/share, not the process drive.
        root: i64 = path_root_length(path, length)
        prefix = root - 1 if root > 0 else 0
    tail: i64 = end
    while tail < length and load_i8(path, tail) == 92:
        tail = tail + 1
    joined_len: i64 = prefix + target_len + (1 if tail < length else 0) + length - tail
    if joined_len >= 131072:
        free(target)
        return null()
    joined = malloc(joined_len + 1)
    if ptr_is_null(joined):
        free(target)
        return null()
    index: i64 = 0
    while index < prefix:
        store_i8(joined, index, load_i8(path, index))
        index = index + 1
    cursor: i64 = 0
    while cursor < target_len:
        byte: i64 = load_i8(target, cursor)
        store_i8(joined, index, 92 if byte == 47 else byte)
        index = index + 1
        cursor = cursor + 1
    free(target)
    if tail < length:
        store_i8(joined, index, 92)
        index = index + 1
        while tail < length:
            store_i8(joined, index, load_i8(path, tail))
            index = index + 1
            tail = tail + 1
    store_i8(joined, index, 0)
    result = realpath_impl(joined, output, capacity, links + 1, keep_prefix)
    free(joined)
    return result


@c_abi_export("pcc_windows_system_realpath_impl")
def realpath_impl(path: c_ptr, output: c_ptr, capacity: i64, links: i64, keep_prefix: i64) -> c_ptr:
    # GetFullPathNameW supplies drive-relative/current-directory semantics.
    # Keep its complete path while trying successively shorter ancestors:
    # resolving only the original missing path loses symlinks in its parents.
    absolute = malloc(131072)
    if ptr_is_null(absolute):
        return null()
    if ptr_is_null(full_path(path, absolute, 131072)):
        free(absolute)
        return null()
    length: i64 = 0
    while load_i8(absolute, length) != 0:
        length = length + 1
    root: i64 = path_root_length(absolute, length)
    end: i64 = length
    while end >= root and end > 0:
        saved: i64 = load_i8(absolute, end)
        store_i8(absolute, end, 0)
        wide = utf16(absolute)
        store_i8(absolute, end, saved)
        if ptr_is_null(wide):
            free(absolute)
            return null()
        handle = CreateFileW(wide, 0, 7, null(), 3, 33554432, null())
        free(wide)
        if ptr_to_int(handle) != -1:
            resolved = final_path(handle, output, capacity, keep_prefix)
            CloseHandle(handle)
            if not ptr_is_null(resolved):
                used: i64 = 0
                while load_i8(output, used) != 0:
                    used = used + 1
                tail: i64 = end
                while tail < length and load_i8(absolute, tail) == 92:
                    tail = tail + 1
                if tail < length:
                    if used > 0 and load_i8(output, used - 1) != 92:
                        if used + 1 >= capacity:
                            free(absolute)
                            return null()
                        store_i8(output, used, 92)
                        used = used + 1
                    if copy(ptr_add(output, used), ptr_add(absolute, tail), capacity - used) < 0:
                        free(absolute)
                        return null()
                free(absolute)
                return output
        # Windows follows at most 63 reparse points. A non-strict resolution
        # retains the unresolved spelling when a chain cannot be followed.
        if links < 63:
            resolved = resolve_reparse_tail(absolute, end, length, output, capacity, links, keep_prefix)
            if not ptr_is_null(resolved):
                free(absolute)
                return output
        if end == root:
            break
        while end > root and load_i8(absolute, end - 1) == 92:
            end = end - 1
        while end > root and load_i8(absolute, end - 1) != 92:
            end = end - 1
        if end > root:
            end = end - 1
    # Non-strict realpath also preserves paths on inaccessible/missing drives.
    result: i64 = copy(output, absolute, capacity)
    free(absolute)
    if result >= 0:
        return output
    return null()


@c_abi_export("pcc_win_realpath")
def realpath(path: c_ptr, output: c_ptr, capacity: i64) -> c_ptr:
    keep_prefix: i64 = 0
    if load_i8(path, 0) == 92 and load_i8(path, 1) == 92 and load_i8(path, 2) == 63 and load_i8(path, 3) == 92:
        keep_prefix = 1
    return realpath_impl(path, output, capacity, 0, keep_prefix)
