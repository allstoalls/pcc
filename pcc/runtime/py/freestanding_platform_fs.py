"""Freestanding path-query primitives authored in pcc-Python.

Darwin lowers these helpers to the explicitly named libSystem ABI. Linux
x86_64 lowers the same source to raw syscalls, so the generated object has no
libc dependency there.
"""

from pcc import i64
from pcc.extern import c_abi_export, c_ptr, c_size_t, extern
from pcc.unsafe import (
    windows_real_path, target_sys_platform,
    windows_tempdir_create, windows_tempdir_remove_owned, windows_tempdir_close_owned,
    directory_open, directory_next, directory_error, directory_close,
    chmod_file, darwin_errno_location, load_i32,
    malloc, free, unlinkat,
    access,
    atomic_rmw_i64,
    define_global_i64,
    getpid,
    open_file_flags,
    global_addr,
    load_i8,
    logical_shift_right_i64,
    mkdir,
    null,
    ptr_add,
    ptr_is_null,
    readlink,
    stack_alloc,
    stat_kind,
    stat_mtime,
    store_i8,
    store_ptr,
)

__pcc_freestanding__ = True

_getcwd_c = extern("getcwd", (c_ptr, c_size_t), c_ptr)


define_global_i64("pcc_platform_mkdtemp_counter", 0)



@c_abi_export("pcc_platform_access")
def pcc_platform_access(path, mode: i64) -> i64:
    return access(path, mode)


@c_abi_export("pcc_platform_getcwd")
def pcc_platform_getcwd(buffer, size: i64):
    # A named standard ABI call reaches the owned Linux/Windows leaf or
    # libSystem on Darwin. Its NULL result has a real errno status.
    return _getcwd_c(buffer, size)


@c_abi_export("pcc_platform_stat_kind")
def pcc_platform_stat_kind(path) -> i64:
    return stat_kind(path)


@c_abi_export("pcc_platform_stat_mtime")
def pcc_platform_stat_mtime(path) -> float:
    return stat_mtime(path)


@c_abi_export("pcc_platform_fs_bounded_cstr_len")
def _bounded_cstr_len(value, limit: i64) -> i64:
    offset: i64 = 0
    while offset < limit:
        if load_i8(value, offset) == 0:
            return offset
        offset = offset + 1
    return -1


@c_abi_export("pcc_platform_fs_copy_cstr")
def _copy_cstr(output, value, capacity: i64) -> i64:
    if capacity <= 0:
        return -1
    offset: i64 = 0
    while offset + 1 < capacity:
        byte = load_i8(value, offset)
        store_i8(output, offset, byte)
        if byte == 0:
            return offset
        offset = offset + 1
    store_i8(output, capacity - 1, 0)
    return -1


@c_abi_export("pcc_platform_fs_pop_component")
def _pop_component(output, length: i64) -> i64:
    if length <= 1:
        store_i8(output, 1, 0)
        return 1
    offset = length - 1
    while offset > 0 and load_i8(output, offset) != 47:
        offset = offset - 1
    if offset <= 0:
        offset: i64 = 1
    store_i8(output, offset, 0)
    return offset


@c_abi_export("pcc_platform_realpath")
def pcc_platform_realpath(path, output, size: i64):
    if load_i8(target_sys_platform(), 0) == 119:
        return windows_real_path(path, output, size)
    # Walk components and follow relative or absolute symlink targets with
    # readlink(2). Keep the fixed 40-link POSIX loop bound and fail instead of
    # truncating the caller's buffer.
    if ptr_is_null(path) or ptr_is_null(output) or size < 2:
        return null()
    if load_i8(path, 0) == 0:
        return null()

    pending_a = stack_alloc(8192)
    pending_b = stack_alloc(8192)
    pending = pending_a
    scratch = pending_b
    if load_i8(path, 0) == 47:
        if _copy_cstr(pending, path, 8192) < 0:
            return null()
    else:
        if ptr_is_null(_getcwd_c(pending, 8192)):
            return null()
        cwd_len = _bounded_cstr_len(pending, 8192)
        if cwd_len < 0 or cwd_len + 2 >= 8192:
            return null()
        if cwd_len > 1 and load_i8(pending, cwd_len - 1) != 47:
            store_i8(pending, cwd_len, 47)
            cwd_len = cwd_len + 1
        source_offset: i64 = 0
        while True:
            byte = load_i8(path, source_offset)
            if cwd_len + source_offset + 1 >= 8192:
                return null()
            store_i8(pending, cwd_len + source_offset, byte)
            if byte == 0:
                break
            source_offset = source_offset + 1

    store_i8(output, 0, 47)
    store_i8(output, 1, 0)
    resolved_len: i64 = 1
    cursor: i64 = 0
    link_count: i64 = 0

    while cursor < 8192:
        while load_i8(pending, cursor) == 47:
            cursor = cursor + 1
        if load_i8(pending, cursor) == 0:
            return output

        component_start = cursor
        while load_i8(pending, cursor) != 0 and load_i8(pending, cursor) != 47:
            cursor = cursor + 1
        component_len = cursor - component_start

        if component_len == 1 and load_i8(pending, component_start) == 46:
            continue
        if (
            component_len == 2
            and load_i8(pending, component_start) == 46
            and load_i8(pending, component_start + 1) == 46
        ):
            resolved_len = _pop_component(output, resolved_len)
            continue

        parent_len = resolved_len
        if resolved_len > 1:
            if resolved_len + 1 >= size:
                return null()
            store_i8(output, resolved_len, 47)
            resolved_len = resolved_len + 1
        copy_offset: i64 = 0
        while copy_offset < component_len:
            if resolved_len + 1 >= size:
                return null()
            store_i8(
                output,
                resolved_len,
                load_i8(pending, component_start + copy_offset),
            )
            resolved_len = resolved_len + 1
            copy_offset = copy_offset + 1
        store_i8(output, resolved_len, 0)

        link_len = readlink(output, scratch, 8191)
        if link_len >= 0:
            if link_len >= 8191 or link_count >= 40:
                return null()
            store_i8(scratch, link_len, 0)
            absolute_target = load_i8(scratch, 0) == 47
            if absolute_target:
                resolved_len: i64 = 1
                store_i8(output, 1, 0)
            else:
                resolved_len = parent_len
                store_i8(output, resolved_len, 0)

            new_len = link_len
            remainder = cursor
            while load_i8(pending, remainder) != 0:
                if new_len + 1 >= 8192:
                    return null()
                store_i8(scratch, new_len, load_i8(pending, remainder))
                new_len = new_len + 1
                remainder = remainder + 1
            store_i8(scratch, new_len, 0)

            old_pending = pending
            pending = scratch
            scratch = old_pending
            cursor: i64 = 0
            link_count = link_count + 1

    return null()


@c_abi_export("pcc_platform_create_temporary_suffix")
def _create_temporary_suffix(path_template, suffix_length: i64, regular_file: i64, owner) -> i64:
    """Replace the six-X stem and create one exclusive filesystem owner."""
    if ptr_is_null(path_template):
        return -22
    length = _bounded_cstr_len(path_template, 1048576)
    if length < 6 or suffix_length < 0 or suffix_length > length - 6:
        return -22
    end: i64 = length - suffix_length
    check: i64 = 0
    while check < 6:
        if load_i8(path_template, end - 1 - check) != 88:
            return -22
        check = check + 1
    ticket = atomic_rmw_i64(
        "add", global_addr("pcc_platform_mkdtemp_counter"), 0, 1, "relaxed",
    )
    seed = getpid() * 1103515245 + ticket * 2654435761
    attempt: i64 = 0
    while attempt < 256:
        value = seed + attempt * 114007148193231
        digit_offset: i64 = 0
        while digit_offset < 6:
            digit = value & 31
            byte = digit + 48
            if digit >= 10:
                byte = digit + 87
            store_i8(path_template, end - 1 - digit_offset, byte)
            value = logical_shift_right_i64(value, 5)
            digit_offset = digit_offset + 1
        status: i64 = -38
        if regular_file != 0:
            platform_byte = load_i8(target_sys_platform(), 0)
            if platform_byte == 108:  # Linux openat flags and AT_FDCWD.
                status = open_file_flags(path_template, 2 | 64 | 128 | 524288, 384, -100)
            elif platform_byte == 100:  # Darwin O_RDWR|O_CREAT|O_EXCL|O_CLOEXEC.
                status = open_file_flags(path_template, 2 | 512 | 2048 | 16777216, 384, -2)
            if status >= 0:
                return status
        else:
            if load_i8(target_sys_platform(), 0) == 119 and not ptr_is_null(owner):
                status = windows_tempdir_create(path_template, owner)
            else:
                status = mkdir(path_template, 448)
            if status == 0:
                return 0
            if load_i8(target_sys_platform(), 0) == 100:
                status = 0 - load_i32(darwin_errno_location(), 0)
        if status != -17:
            return status
        attempt = attempt + 1
    return -17


@c_abi_export("pcc_platform_mkdtemp_suffix")
def pcc_platform_mkdtemp_suffix(path_template, suffix_length: i64) -> i64:
    """Create a 0700 directory; return zero or negative errno."""
    return _create_temporary_suffix(path_template, suffix_length, 0, null())


@c_abi_export("pcc_platform_tempdir_create_owned")
def pcc_platform_tempdir_create_owned(path_template, suffix_length: i64, owner) -> i64:
    store_ptr(owner, 0, null())
    return _create_temporary_suffix(path_template, suffix_length, 0, owner)


@c_abi_export("pcc_platform_tempdir_remove_owned")
def pcc_platform_tempdir_remove_owned(owner) -> i64:
    if load_i8(target_sys_platform(), 0) == 119:
        return windows_tempdir_remove_owned(owner)
    return -38


@c_abi_export("pcc_platform_tempdir_close_owned")
def pcc_platform_tempdir_close_owned(owner) -> i64:
    if load_i8(target_sys_platform(), 0) == 119:
        return windows_tempdir_close_owned(owner)
    return -38


@c_abi_export("pcc_platform_mkstemp_suffix")
def pcc_platform_mkstemp_suffix(path_template, suffix_length: i64) -> i64:
    """Create a 0600 exclusive, close-on-exec file; return fd or negative errno."""
    return _create_temporary_suffix(path_template, suffix_length, 1, null())


@c_abi_export("pcc_platform_mkdtemp")
def pcc_platform_mkdtemp(path_template):
    if pcc_platform_mkdtemp_suffix(path_template, 0) < 0:
        return null()
    return path_template


@c_abi_export("pcc_platform_remove_tree")
def pcc_platform_remove_tree(path) -> i64:
    return _remove_tree(path, 0)


@c_abi_export("pcc_platform_tempdir_remove_tree")
def pcc_platform_tempdir_remove_tree(path) -> i64:
    probe = stack_alloc(1)
    if ptr_is_null(path) or load_i8(path, 0) == 0:
        return -22
    if readlink(path, probe, 1) >= 0:
        return -20
    if stat_kind(path) != 2:
        status: i64 = access(path, 0)
        if status < 0:
            if load_i8(target_sys_platform(), 0) == 100:
                status = 0 - load_i32(darwin_errno_location(), 0)
            return status
        return -20
    return _remove_tree(path, 1)


@c_abi_export("pcc_platform_remove_tree_options")
def _remove_tree(path, repair: i64) -> i64:
    if ptr_is_null(path) or load_i8(path, 0) == 0:
        return -22
    probe = stack_alloc(1)
    if readlink(path, probe, 1) >= 0:
        return unlinkat(path, 0)
    kind: i64 = stat_kind(path)
    if kind == 0:
        return -2
    if kind != 2:
        return unlinkat(path, 0)
    stream = directory_open(path)
    if ptr_is_null(stream) and repair:
        chmod_file(path, 448)
        stream = directory_open(path)
    if ptr_is_null(stream):
        return -13
    path_size: i64 = _bounded_cstr_len(path, 1048576)
    if path_size < 0:
        directory_close(stream)
        return -36
    result: i64 = 0
    while True:
        name = directory_next(stream)
        if ptr_is_null(name):
            result = directory_error(stream)
            break
        if load_i8(name, 0) == 46:
            if load_i8(name, 1) == 0 or (load_i8(name, 1) == 46 and load_i8(name, 2) == 0):
                continue
        size: i64 = _bounded_cstr_len(name, 1048576)
        if size < 0:
            result = -36
            break
        child = malloc(path_size + size + 2)
        if ptr_is_null(child):
            result = -12
            break
        index: i64 = 0
        while index < path_size:
            store_i8(child, index, load_i8(path, index))
            index = index + 1
        store_i8(child, path_size, 47)
        index = 0
        while index <= size:
            store_i8(child, path_size + 1 + index, load_i8(name, index))
            index = index + 1
        result = _remove_tree(child, repair)
        if repair and (result == -13 or result == -1):
            chmod_file(path, 448)
            if readlink(child, probe, 1) < 0:
                chmod_file(child, 448 if stat_kind(child) == 2 else 384)
            result = _remove_tree(child, 0)
        if repair and result == -2:
            result = 0
        free(child)
        if result < 0:
            break
    directory_close(stream)
    if result < 0:
        return result
    return unlinkat(path, 1)
