"""Freestanding process wait/signal primitives authored in pcc-Python."""

from pcc import i64
from pcc.extern import c_abi_export
from pcc.unsafe import (
    target_sys_platform,
    access,
    cstr,
    free,
    getpid,
    kill,
    load_i8,
    load_ptr,
    logical_shift_right_i64,
    malloc,
    null,
    process_exit,
    ptr_add,
    ptr_is_null,
    spawn_process,
    store_i8,
    waitpid,
)


__pcc_freestanding__ = True


@c_abi_export("pcc_platform_waitpid")
def pcc_platform_waitpid(pid: i64, status, options: i64) -> i64:
    result = waitpid(pid, status, options)
    while result == -4:
        result = waitpid(pid, status, options)
    return result


@c_abi_export("pcc_platform_kill")
def pcc_platform_kill(pid: i64, signal_number: i64) -> i64:
    return kill(pid, signal_number)


@c_abi_export("pcc_platform_exit")
def pcc_platform_exit(status: i64) -> None:
    process_exit(status)


@c_abi_export("pcc_platform_abort")
def pcc_platform_abort() -> None:
    pcc_platform_kill(getpid(), 6)
    process_exit(134)


@c_abi_export("pcc_platform_process_cstr_len")
def _process_cstr_len(value, limit: i64) -> i64:
    if ptr_is_null(value):
        return -1
    offset: i64 = 0
    while offset < limit:
        if load_i8(value, offset) == 0:
            return offset
        offset = offset + 1
    return -1


@c_abi_export("pcc_platform_process_copy_cstr")
def _process_copy_cstr(value, length: i64):
    owned = malloc(length + 1)
    if ptr_is_null(owned):
        return owned
    offset: i64 = 0
    while offset <= length:
        store_i8(owned, offset, load_i8(value, offset))
        offset = offset + 1
    return owned


@c_abi_export("pcc_platform_process_find_path")
def _process_find_path(envp):
    windows: i64 = 1 if load_i8(target_sys_platform(), 0) == 119 else 0
    default = cstr(".") if windows else cstr("/usr/bin:/bin")
    if ptr_is_null(envp):
        return default
    index: i64 = 0
    while index < 1048576:
        entry = load_ptr(envp, index * 8)
        if ptr_is_null(entry):
            break
        if (
            (load_i8(entry, 0) & (223 if windows else 255)) == 80
            and (load_i8(entry, 1) & (223 if windows else 255)) == 65
            and (load_i8(entry, 2) & (223 if windows else 255)) == 84
            and (load_i8(entry, 3) & (223 if windows else 255)) == 72
            and load_i8(entry, 4) == 61
        ):
            return ptr_add(entry, 5)
        index = index + 1
    return default


@c_abi_export("pcc_platform_process_resolve")
def _process_resolve(argv, envp):
    if ptr_is_null(argv):
        return null()
    name = load_ptr(argv, 0)
    name_len = _process_cstr_len(name, 1048576)
    if name_len <= 0:
        return null()
    windows: i64 = 1 if load_i8(target_sys_platform(), 0) == 119 else 0
    delimiter: i64 = 59 if windows else 58
    offset: i64 = 0
    while offset < name_len:
        if load_i8(name, offset) == 47 or (windows and load_i8(name, offset) == 92):
            return _process_copy_cstr(name, name_len)
        offset = offset + 1

    search = _process_find_path(envp)
    search_len = _process_cstr_len(search, 1048576)
    if search_len < 0 or search_len + name_len + 3 > 2097152:
        return null()
    candidate = malloc(search_len + name_len + 7)
    if ptr_is_null(candidate):
        return candidate
    start: i64 = 0
    while start <= search_len:
        end = start
        while end < search_len and load_i8(search, end) != delimiter:
            end = end + 1
        position: i64 = 0
        if end == start:
            store_i8(candidate, position, 46)
            position = position + 1
        else:
            cursor = start
            while cursor < end:
                store_i8(candidate, position, load_i8(search, cursor))
                cursor = cursor + 1
                position = position + 1
        if position == 0 or load_i8(candidate, position - 1) != 47:
            store_i8(candidate, position, 47)
            position = position + 1
        name_offset: i64 = 0
        while name_offset <= name_len:
            store_i8(candidate, position + name_offset, load_i8(name, name_offset))
            name_offset = name_offset + 1
        if access(candidate, 1) == 0:
            return candidate
        if windows:
            suffix = cstr(".exe")
            extra: i64 = 0
            while extra < 5:
                store_i8(candidate, position + name_len + extra, load_i8(suffix, extra))
                extra = extra + 1
            if access(candidate, 1) == 0:
                return candidate
        if end >= search_len:
            break
        start = end + 1
    free(candidate)
    return null()


@c_abi_export("pcc_platform_spawnp")
def pcc_platform_spawnp(argv, envp, capture_output: i64) -> i64:
    path = _process_resolve(argv, envp)
    if ptr_is_null(path):
        return -2
    result = spawn_process(path, argv, envp, capture_output)
    free(path)
    return result


@c_abi_export("py_process_normalize_wait_status")
def py_process_normalize_wait_status(raw_status: i64) -> i64:
    if raw_status < 0:
        return 127
    low = raw_status & 127
    if low == 0:
        return logical_shift_right_i64(raw_status, 8) & 255
    if low != 127:
        return -low
    return 127
