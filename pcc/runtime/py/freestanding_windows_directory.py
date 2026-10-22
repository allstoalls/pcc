"""Owned Windows directory streams and handle-anchored temporary directories."""

from pcc import i64
from pcc.extern import c_abi_export, c_ptr, c_int, c_int64, extern
from pcc.unsafe import (
    malloc, free, null, ptr_is_null, ptr_to_int, ptr_add, load_i8, load_i64,
    load_ptr, store_i8, store_i64, store_ptr, load_i32, store_i32,
    stack_alloc, memset, windows_full_path, cstr,
)

__pcc_freestanding__ = True

FindFirstFileW = extern("FindFirstFileW", (c_ptr, c_ptr), c_ptr)
FindNextFileW = extern("FindNextFileW", (c_ptr, c_ptr), c_int)
FindClose = extern("FindClose", (c_ptr,), c_int)
GetLastError = extern("GetLastError", (), c_int)
GetFileAttributesW = extern("GetFileAttributesW", (c_ptr,), c_int)
utf16 = extern("pcc_win_utf16", (c_ptr,), c_ptr)
utf8 = extern("pcc_win_utf8", (c_ptr,), c_ptr)
error = extern("pcc_win_error", (), c_int64)
error_code = extern("pcc_win_error_code", (c_int64,), c_int64)
CreateFileW = extern("CreateFileW", (c_ptr, c_int, c_int, c_ptr, c_int, c_int, c_ptr), c_ptr)
CloseHandle = extern("CloseHandle", (c_ptr,), c_int)
GetFileInformationByHandleEx = extern("GetFileInformationByHandleEx", (c_ptr, c_int, c_ptr, c_int), c_int)
SetFileInformationByHandle = extern("SetFileInformationByHandle", (c_ptr, c_int, c_ptr, c_int), c_int)
NtCreateFile = extern("NtCreateFile", (c_ptr, c_int, c_ptr, c_ptr, c_ptr, c_int, c_int, c_int, c_int, c_ptr, c_int), c_int)
RtlNtStatusToDosError = extern("RtlNtStatusToDosError", (c_int,), c_int)
ConvertStringSecurityDescriptorToSecurityDescriptorW = extern(
    "ConvertStringSecurityDescriptorToSecurityDescriptorW", (c_ptr, c_int, c_ptr, c_ptr), c_int)
LocalFree = extern("LocalFree", (c_ptr,), c_ptr)

# x64 system ABI layouts, not Python-object layouts.
_TD_UNICODE_SIZE = 16
_TD_OBJECT_ATTRIBUTES_SIZE = 48
_TD_IO_STATUS_SIZE = 16
_TD_DIRECTORY_BUFFER_SIZE = 65536
_TD_DIRECTORY_HEADER_SIZE = 68
_TD_DIRECTORY_NAME_LENGTH = 60
_TD_FILE_ATTRIBUTE_DIRECTORY = 16
_TD_FILE_ATTRIBUTE_REPARSE_POINT = 1024
_TD_MAX_DEPTH = 256


@c_abi_export("pcc_win_tempdir_component_valid")
def tempdir_component_valid(name: c_ptr, length: i64) -> i64:
    # A length-delimited UNICODE_STRING must name precisely one child. Never
    # permit a directory entry to turn RootDirectory into a path traversal.
    if length <= 0 or length > 65534 or length & 1:
        return 0
    if load_i8(name, 0) == 46 and load_i8(name, 1) == 0:
        if length == 2 or (length == 4 and load_i8(name, 2) == 46 and load_i8(name, 3) == 0):
            return 0
    offset: i64 = 0
    while offset < length:
        low: i64 = load_i8(name, offset) & 255
        high: i64 = load_i8(name, offset + 1) & 255
        if high == 0 and (low == 0 or low == 47 or low == 92 or low == 58):
            return 0
        offset = offset + 2
    return 1


@c_abi_export("pcc_win_tempdir_dos_leaf_valid")
def tempdir_dos_leaf_valid(name: c_ptr, length: i64) -> i64:
    # The public name uses the Win32 namespace. NT-only trailing-dot/space and
    # DOS device aliases must not create a different object behind that name.
    if load_i8(name, length - 1) == 0 and (load_i8(name, length - 2) == 46 or load_i8(name, length - 2) == 32):
        return 0
    value: i64 = 0
    count: i64 = 0
    offset: i64 = 0
    base: i64 = 1
    ascii_base: i64 = 1
    while offset < length:
        low: i64 = load_i8(name, offset) & 255
        high: i64 = load_i8(name, offset + 1) & 255
        if high == 0 and (low == 34 or low == 60 or low == 62 or low == 124 or low == 63 or low == 42 or low < 32):
            return 0
        if high == 0 and low == 46:
            base = 0
        if base:
            if high != 0:
                ascii_base = 0
            if low >= 97 and low <= 122:
                low = low - 32
            if count < 7:
                value = value * 256 + low
            count = count + 1
        offset = offset + 2
    if not ascii_base:
        return 1
    if count == 3 and (value == 4411214 or value == 5263950 or value == 4281688 or value == 5133644):
        return 0  # CON, PRN, AUX, NUL.
    if count == 4 and (value // 256 == 4411213 or value // 256 == 5001300):
        last: i64 = value & 255
        if last >= 49 and last <= 57 or last == 178 or last == 179 or last == 185:
            return 0  # COM/LPT, including the three DOS superscript digits.
    if count == 6 and value == 74007894904356 or count == 7 and value == 18946021196649508:
        return 0  # CONIN$, CONOUT$.
    return 1


@c_abi_export("pcc_win_tempdir_open_relative")
def tempdir_open_relative(parent: c_ptr, name: c_ptr, length: i64, create: i64, output: c_ptr) -> i64:
    store_ptr(output, 0, null())
    if not tempdir_component_valid(name, length):
        return -22
    text = stack_alloc(_TD_UNICODE_SIZE)
    attributes = stack_alloc(_TD_OBJECT_ATTRIBUTES_SIZE)
    status_block = stack_alloc(_TD_IO_STATUS_SIZE)
    memset(text, 0, _TD_UNICODE_SIZE)
    memset(attributes, 0, _TD_OBJECT_ATTRIBUTES_SIZE)
    memset(status_block, 0, _TD_IO_STATUS_SIZE)
    # Two USHORTs packed in one DWORD; the string need not be NUL terminated.
    store_i32(text, 0, length | (length * 65536))
    store_ptr(text, 8, name)
    store_i32(attributes, 0, _TD_OBJECT_ATTRIBUTES_SIZE)
    store_ptr(attributes, 8, parent)
    store_ptr(attributes, 16, text)
    # OBJ_CASE_INSENSITIVE | OBJ_DONT_REPARSE: fail if name resolution meets a
    # reparse point. The name is one component relative to the held handle.
    store_i32(attributes, 24, 4160)
    descriptor = stack_alloc(8)
    store_ptr(descriptor, 0, null())
    if create:
        # The same protected, inheritable owner/SYSTEM/Administrators DACL as
        # CPython's Windows mkdir(0700). Apply it atomically to this NEW object
        # only; never repair or change an existing path's permissions.
        sddl = utf16(cstr("D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)(A;OICI;FA;;;OW)"))
        if ptr_is_null(sddl):
            return -12
        success: i64 = ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, descriptor, null())
        failure: i64 = 0 if success else error()
        free(sddl)
        if failure:
            return failure
        if ptr_is_null(load_ptr(descriptor, 0)):
            return -5  # Never silently fall back to inherited permissions.
        store_ptr(attributes, 32, load_ptr(descriptor, 0))
    options: i64 = 2097184  # OPEN_REPARSE_POINT | SYNCHRONOUS_IO_NONALERT.
    if create:
        options = options | 1  # FILE_DIRECTORY_FILE, FILE_CREATE only.
    # DELETE | SYNCHRONIZE | FILE_READ_ATTRIBUTES | FILE_LIST_DIRECTORY.
    # No SHARE_DELETE prevents the opened entry being replaced by pathname.
    # Share mode does NOT prohibit reparse-attribute changes. DONT_REPARSE
    # rejects reparses encountered during single-component name resolution;
    # enumeration/deletion use the retained object handle, never its pathname.
    status: i64 = NtCreateFile(output, 1114241,
        attributes, status_block, null(), 128, 1, 2 if create else 1,
        options, null(), 0)
    release_failed: i64 = 0
    if not ptr_is_null(load_ptr(descriptor, 0)):
        release_failed = 0 if ptr_is_null(LocalFree(load_ptr(descriptor, 0))) else 1
    if status < 0:
        store_ptr(output, 0, null())
        return error_code(RtlNtStatusToDosError(status))
    # Synchronous calls must have completed and FILE_CREATE must have created
    # a new owner. Never accept a success-like status as ownership evidence.
    if status != 0 or ptr_is_null(load_ptr(output, 0)) or ptr_to_int(load_ptr(output, 0)) == -1:
        if not ptr_is_null(load_ptr(output, 0)) and ptr_to_int(load_ptr(output, 0)) != -1:
            CloseHandle(load_ptr(output, 0))
        store_ptr(output, 0, null())
        return -5
    if create and load_i64(status_block, 8) != 2:
        CloseHandle(load_ptr(output, 0))
        store_ptr(output, 0, null())
        return -5
    if release_failed:
        # Keep the proven-created handle visible to the caller's rollback.
        return -5
    return 0


@c_abi_export("pcc_win_tempdir_create")
def tempdir_create(path: c_ptr, output: c_ptr) -> i64:
    store_ptr(output, 0, null())
    absolute = malloc(131072)
    if ptr_is_null(absolute):
        return -12
    if ptr_is_null(windows_full_path(path, absolute, 131072)):
        free(absolute)
        return -22
    if load_i8(absolute, 0) == 92 and load_i8(absolute, 1) == 92 and (load_i8(absolute, 2) == 63 or load_i8(absolute, 2) == 46) and load_i8(absolute, 3) == 92:
        free(absolute)
        return -22  # Explicit device/extended namespaces are not this API.
    wide = utf16(absolute)
    free(absolute)
    if ptr_is_null(wide):
        return -22
    length: i64 = 0
    split: i64 = -1
    while load_i8(wide, length) != 0 or load_i8(wide, length + 1) != 0:
        if load_i8(wide, length) == 92 and load_i8(wide, length + 1) == 0:
            split = length
        length = length + 2
    if split < 0 or not tempdir_component_valid(ptr_add(wide, split + 2), length - split - 2):
        free(wide)
        return -22
    # Native NT names with these endings are distinct from ordinary Win32
    # path spellings; do not create an object the returned name cannot address.
    if not tempdir_dos_leaf_valid(ptr_add(wide, split + 2), length - split - 2):
        free(wide)
        return -22
    # Only a drive/share root retains its separator. A trailing separator on
    # an ordinary parent could turn its final reparse point into a traversal.
    parent_end: i64 = split
    if split == 4 and load_i8(wide, 2) == 58 and load_i8(wide, 3) == 0:
        parent_end = split + 2
    elif load_i8(wide, 0) == 92 and load_i8(wide, 2) == 92:
        cursor: i64 = 4
        separators: i64 = 0
        while cursor < split:
            if load_i8(wide, cursor) == 92 and load_i8(wide, cursor + 1) == 0:
                separators = separators + 1
            cursor = cursor + 2
        if separators == 1:
            parent_end = split + 2
    first: i64 = load_i8(wide, parent_end)
    second: i64 = load_i8(wide, parent_end + 1)
    store_i8(wide, parent_end, 0)
    store_i8(wide, parent_end + 1, 0)
    parent = CreateFileW(wide, 160, 1, null(), 3, 35651584, null())
    failure: i64 = error() if ptr_to_int(parent) == -1 else 0
    store_i8(wide, parent_end, first)
    store_i8(wide, parent_end + 1, second)
    if failure == 0:
        info = stack_alloc(8)
        if not GetFileInformationByHandleEx(parent, 9, info, 8):
            failure = error()
        elif load_i32(info, 0) & _TD_FILE_ATTRIBUTE_REPARSE_POINT:
            failure = -40
        elif not (load_i32(info, 0) & _TD_FILE_ATTRIBUTE_DIRECTORY):
            failure = -20
        else:
            failure = tempdir_open_relative(parent, ptr_add(wide, split + 2),
                                            length - split - 2, 1, output)
        closed: i64 = 0 if CloseHandle(parent) else error()
        if failure == 0:
            failure = closed
    free(wide)
    return failure


@c_abi_export("pcc_win_tempdir_remove_owned")
def tempdir_remove_owned(owner: c_ptr) -> i64:
    return tempdir_remove_at_depth(owner, 0)


@c_abi_export("pcc_win_tempdir_remove_at_depth")
def tempdir_remove_at_depth(owner: c_ptr, depth: i64) -> i64:
    # All traversal is relative to handles, including the original root.
    # Nothing here re-opens a caller-controlled name, follows a reparse target,
    # or changes ACLs, modes, read-only attributes or other security settings.
    if depth >= _TD_MAX_DEPTH:
        return -36
    info = stack_alloc(8)
    if not GetFileInformationByHandleEx(owner, 9, info, 8):
        return error()
    attributes: i64 = load_i32(info, 0)
    result: i64 = 0
    if (attributes & _TD_FILE_ATTRIBUTE_DIRECTORY) and not (attributes & _TD_FILE_ATTRIBUTE_REPARSE_POINT):
        buffer = malloc(_TD_DIRECTORY_BUFFER_SIZE)
        if ptr_is_null(buffer):
            return -12
        restart: i64 = 1
        child = stack_alloc(8)
        while result == 0:
            if not GetFileInformationByHandleEx(owner, 15 if restart else 14,
                                                buffer, _TD_DIRECTORY_BUFFER_SIZE):
                result = 0 if GetLastError() == 18 else error()
                break
            restart = 0
            offset: i64 = 0
            while True:
                if offset > _TD_DIRECTORY_BUFFER_SIZE - _TD_DIRECTORY_HEADER_SIZE:
                    result = -5
                    break
                entry = ptr_add(buffer, offset)
                following: i64 = load_i32(entry, 0) & 4294967295
                length: i64 = load_i32(entry, _TD_DIRECTORY_NAME_LENGTH) & 4294967295
                available: i64 = _TD_DIRECTORY_BUFFER_SIZE - offset
                if following:
                    if following & 7 or following < _TD_DIRECTORY_HEADER_SIZE or following > available - _TD_DIRECTORY_HEADER_SIZE:
                        result = -5
                        break
                    available = following
                if length > available - _TD_DIRECTORY_HEADER_SIZE or length & 1 or length == 0:
                    result = -5
                    break
                name = ptr_add(entry, _TD_DIRECTORY_HEADER_SIZE)
                dot: i64 = 0
                if load_i8(name, 0) == 46 and load_i8(name, 1) == 0:
                    if length == 2 or (length == 4 and load_i8(name, 2) == 46 and load_i8(name, 3) == 0):
                        dot = 1
                if not dot:
                    result = tempdir_open_relative(owner, name, length, 0, child)
                    if result == 0:
                        # Query this actual handle's attributes, not the stale
                        # directory enumeration record. Every tag is a leaf.
                        result = tempdir_remove_at_depth(load_ptr(child, 0), depth + 1)
                        close_result: i64 = 0 if CloseHandle(load_ptr(child, 0)) else error()
                        if result == 0:
                            result = close_result
                    elif result == -2:
                        result = 0  # A child removed concurrently is harmless.
                    if result < 0:
                        break
                if following == 0:
                    break
                offset = offset + following
        free(buffer)
        if result < 0:
            return result
    disposition = stack_alloc(1)
    store_i8(disposition, 0, 1)
    if not SetFileInformationByHandle(owner, 4, disposition, 1):
        return error()
    return 0


@c_abi_export("pcc_win_tempdir_close_owned")
def tempdir_close_owned(owner: c_ptr) -> i64:
    return 0 if CloseHandle(owner) else error()


@c_abi_export("pcc_win_directory_open")
def directory_open(path: c_ptr) -> c_ptr:
    check = utf16(path)
    if ptr_is_null(check):
        return null()
    attributes: i64 = GetFileAttributesW(check)
    free(check)
    if attributes == -1 or not (attributes & 16):
        return null()
    length: i64 = 0
    while load_i8(path, length) != 0:
        length = length + 1
    pattern = malloc(length + 3)
    stream = malloc(624)
    if ptr_is_null(pattern) or ptr_is_null(stream):
        free(pattern)
        free(stream)
        return null()
    index: i64 = 0
    while index < length:
        store_i8(pattern, index, load_i8(path, index))
        index = index + 1
    if length and load_i8(pattern, length - 1) != 47 and load_i8(pattern, length - 1) != 92:
        store_i8(pattern, length, 92)
        length = length + 1
    store_i8(pattern, length, 42)
    store_i8(pattern, length + 1, 0)
    wide = utf16(pattern)
    free(pattern)
    if ptr_is_null(wide):
        free(stream)
        return null()
    handle = FindFirstFileW(wide, ptr_add(stream, 32))
    failure: i64 = GetLastError()
    free(wide)
    empty: i64 = 0
    if ptr_to_int(handle) == -1:
        if failure != 2:
            free(stream)
            return null()
        empty = 1
    store_ptr(stream, 0, handle)
    store_i64(stream, 8, 2 if empty else 1)
    store_ptr(stream, 16, null())
    store_i64(stream, 24, 0)
    return stream


@c_abi_export("pcc_win_directory_next")
def directory_next(stream: c_ptr) -> c_ptr:
    if load_i64(stream, 8) == 2:
        return null()
    if load_i64(stream, 8) == 1:
        store_i64(stream, 8, 0)
    elif not FindNextFileW(load_ptr(stream, 0), ptr_add(stream, 32)):
        store_i64(stream, 24, 0 if GetLastError() == 18 else error())
        return null()
    previous = load_ptr(stream, 16)
    text = utf8(ptr_add(stream, 76))  # WIN32_FIND_DATAW.cFileName is byte 44.
    store_ptr(stream, 16, text)
    free(previous)
    if ptr_is_null(text):
        store_i64(stream, 24, -12)
    return text


@c_abi_export("pcc_win_directory_error")
def directory_error(stream: c_ptr) -> i64:
    return load_i64(stream, 24)


@c_abi_export("pcc_win_directory_close")
def directory_close(stream: c_ptr) -> i64:
    result: i64 = 0
    if ptr_to_int(load_ptr(stream, 0)) != -1:
        result = 0 if FindClose(load_ptr(stream, 0)) else error()
    free(load_ptr(stream, 16))
    free(stream)
    return result
