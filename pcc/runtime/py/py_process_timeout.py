"""pcc-Python owner of subprocess timeout and process-group cleanup."""

__pcc_runtime_port__ = True

from pcc.extern import c_abi_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    close,
    free,
    load_i32,
    load_i64,
    load_i8,
    load_ptr,
    malloc,
    memcpy,
    memset,
    null,
    open_readonly,
    ptr_is_null,
    seek_file,
    stack_alloc,
    store_i32,
    store_i64,
    store_i8,
    store_ptr,
    strlen,
    unsigned_div_i64,
)


py_decref = extern("py_decref", (c_ptr,), c_void)
py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
py_int_to_i64 = extern("py_int_to_i64", (c_ptr, c_ptr), c_int64)
py_obj_getitem = extern("py_obj_getitem", (c_ptr, c_ptr), c_ptr)
py_obj_len = extern("py_obj_len", (c_ptr,), c_int64)
py_obj_str = extern("py_obj_str", (c_ptr,), c_ptr)
py_str_utf8 = extern("py_str_utf8", (c_ptr,), c_ptr)

platform_env_snapshot = extern("pcc_platform_env_snapshot", (), c_ptr)
platform_env_snapshot_free = extern(
    "pcc_platform_env_snapshot_free", (c_ptr,), c_void
)
platform_spawnp = extern(
    "pcc_platform_spawnp", (c_ptr, c_ptr, c_int64), c_int64
)
platform_waitpid = extern(
    "pcc_platform_waitpid", (c_int64, c_ptr, c_int64), c_int64
)
platform_kill = extern("pcc_platform_kill", (c_int64, c_int64), c_int64)
platform_monotonic_us = extern("pcc_platform_monotonic_us", (), c_int64)
platform_sleep_ns = extern("pcc_platform_sleep_ns", (c_int64,), c_int64)
normalize_wait_status = extern(
    "py_process_normalize_wait_status", (c_int64,), c_int64
)


@c_abi_export("pcc_process_free_exec_argv")
def _free_exec_argv(items, count: int) -> None:
    if ptr_is_null(items):
        return
    index = 0
    while index < count:
        free(load_ptr(items, index * 8))
        index = index + 1
    free(items)


@c_abi_export("pcc_process_build_exec_argv")
def _build_exec_argv(argv):
    count = py_obj_len(argv)
    if count <= 0 or count > 1048576:
        return null()
    items = malloc((count + 1) * 8)
    if ptr_is_null(items):
        return null()
    index = 0
    while index < count:
        py_index = py_int_from_i64(index)
        item = py_obj_getitem(argv, py_index)
        py_decref(py_index)
        text = py_obj_str(item)
        py_decref(item)
        if ptr_is_null(text):
            _free_exec_argv(items, index)
            return null()
        raw = py_str_utf8(text)
        size = 0
        if not ptr_is_null(raw):
            size = strlen(raw)
        owned = malloc(size + 1)
        if ptr_is_null(owned):
            py_decref(text)
            _free_exec_argv(items, index)
            return null()
        if size > 0:
            memcpy(owned, raw, size)
        store_i8(owned, size, 0)
        store_ptr(items, index * 8, owned)
        py_decref(text)
        index = index + 1
    store_ptr(items, count * 8, null())
    return items


def _monotonic_millis() -> int:
    now_us = platform_monotonic_us()
    if now_us <= 0:
        return -1
    return unsigned_div_i64(now_us, 1000)


def _wait_for_exit(pid: int, status, deadline_ms: int) -> int:
    while True:
        waited = platform_waitpid(pid, status, 1)
        if waited == pid:
            return 1
        if waited < 0:
            return -1
        now_ms = _monotonic_millis()
        if now_ms < 0 or now_ms >= deadline_ms:
            return 0
        # Runtime-library modules are linked without running their Python
        # module initializer, so ABI constants must remain literal here rather
        # than being loaded from uninitialized module-global storage.
        platform_sleep_ns(10000000)


def _terminate_process_group(pid: int, status) -> None:
    if platform_kill(-pid, 15) != 0:
        platform_kill(pid, 15)
    now_ms = _monotonic_millis()
    deadline_ms = 0
    if now_ms >= 0:
        deadline_ms = now_ms + 200
    waited = _wait_for_exit(pid, status, deadline_ms)
    if waited == 1 or waited < 0:
        return
    if platform_kill(-pid, 9) != 0:
        platform_kill(pid, 9)
    platform_waitpid(pid, status, 0)


@c_abi_export("py_subprocess_run_timeout")
def py_subprocess_run_timeout(
    argv, capture_output: int, timeout_ms: int
) -> int:
    if timeout_ms <= 0:
        return 127
    count = py_obj_len(argv)
    items = _build_exec_argv(argv)
    if ptr_is_null(items):
        return 127
    child_env = platform_env_snapshot()
    if ptr_is_null(child_env):
        _free_exec_argv(items, count)
        return 127
    # Allocate before spawning so allocation failure cannot orphan a child.
    status = malloc(4)
    if ptr_is_null(status):
        platform_env_snapshot_free(child_env)
        _free_exec_argv(items, count)
        return 127
    store_i32(status, 0, 0)
    pid = platform_spawnp(items, child_env, capture_output)
    platform_env_snapshot_free(child_env)
    _free_exec_argv(items, count)
    if pid <= 0:
        free(status)
        return 127

    # waitpid writes one C ``int``.  Keep the raw slot at the platform ABI
    # width instead of over-allocating it as though it were an int64 result.
    start_ms = _monotonic_millis()
    if start_ms < 0:
        _terminate_process_group(pid, status)
        free(status)
        return 127
    waited = _wait_for_exit(pid, status, start_ms + timeout_ms)
    if waited == 1:
        result = normalize_wait_status(load_i32(status, 0))
        free(status)
        return result
    if waited < 0:
        free(status)
        return 127
    _terminate_process_group(pid, status)
    free(status)
    return -124


@c_abi_export("pcc_worker_process_pool")
def pcc_worker_process_pool(specs, width: int) -> int:
    """Run argv/env-vector pairs with bounded concurrency and group cleanup.

    Zero means success. A failure packs (command index + 1) in the high
    32 bits and the signed return code in the low 32 bits.
    """
    count = py_obj_len(specs)
    if count <= 0:
        return 0
    if width < 1:
        width = 1
    if width > count:
        width = count
    slots = malloc(width * 16)
    status = malloc(4)
    if ptr_is_null(slots) or ptr_is_null(status):
        free(slots)
        free(status)
        return 4294967423
    slot = 0
    while slot < width:
        store_i64(slots, slot * 16, 0)
        slot += 1
    next_index = 0
    live = 0
    failure = 0
    while next_index < count or live > 0:
        slot = 0
        while slot < width:
            pid = load_i64(slots, slot * 16)
            if pid > 0:
                waited = platform_waitpid(pid, status, 1)
                if waited != 0:
                    rc = 127
                    if waited == pid:
                        rc = normalize_wait_status(load_i32(status, 0))
                    # A completed worker cannot leave a helper behind.
                    platform_kill(-pid, 9)
                    store_i64(slots, slot * 16, 0)
                    live -= 1
                    if rc != 0:
                        index = load_i64(slots, slot * 16 + 8)
                        failure = ((index + 1) << 32) | (rc & 4294967295)
                        break
            slot += 1
        if failure != 0:
            break
        slot = 0
        while next_index < count and live < width:
            while load_i64(slots, slot * 16) != 0:
                slot += 1
            py_index = py_int_from_i64(next_index)
            spec = py_obj_getitem(specs, py_index)
            py_decref(py_index)
            zero = py_int_from_i64(0)
            one = py_int_from_i64(1)
            argv = py_obj_getitem(spec, zero)
            env = py_obj_getitem(spec, one)
            py_decref(zero)
            py_decref(one)
            argc = py_obj_len(argv)
            envc = py_obj_len(env)
            items = _build_exec_argv(argv)
            envp = _build_exec_argv(env)
            pid = -1
            if ptr_is_null(items) == 0 and ptr_is_null(envp) == 0:
                pid = platform_spawnp(items, envp, 0)
            _free_exec_argv(items, argc)
            _free_exec_argv(envp, envc)
            py_decref(argv)
            py_decref(env)
            py_decref(spec)
            if pid <= 0:
                failure = ((next_index + 1) << 32) | 127
                break
            store_i64(slots, slot * 16, pid)
            store_i64(slots, slot * 16 + 8, next_index)
            next_index += 1
            live += 1
            slot += 1
        if failure != 0:
            break
        if live > 0:
            platform_sleep_ns(10000000)
    if failure != 0:
        slot = 0
        while slot < width:
            pid = load_i64(slots, slot * 16)
            if pid > 0:
                platform_kill(-pid, 15)
            slot += 1
        platform_sleep_ns(200000000)
        slot = 0
        while slot < width:
            pid = load_i64(slots, slot * 16)
            if pid > 0:
                platform_kill(-pid, 9)
                platform_waitpid(pid, status, 0)
            slot += 1
    free(status)
    free(slots)
    return failure


@c_abi_export("pcc_weighted_worker_process_pool")
def pcc_weighted_worker_process_pool(
    specs, weights, width: int, budget: int,
) -> int:
    """Run independent workers as memory reservations become available.

    ``weights`` are conservative byte reservations, not live-RSS guesses.
    A job larger than the budget runs alone under the caller's tree watchdog.
    The return value uses the same packed failing-index/return-code ABI as the
    fixed-width pool.
    """
    count = py_obj_len(specs)
    if count <= 0:
        return 0
    if count > 1048576 or py_obj_len(weights) != count or budget <= 0:
        return 4294967423
    if width < 1:
        width = 1
    if width > count:
        width = count
    slots = malloc(width * 24)
    reservations = malloc(count * 8)
    started = malloc(count)
    status = malloc(4)
    overflow = malloc(4)
    if (
        ptr_is_null(slots) or ptr_is_null(reservations)
        or ptr_is_null(started) or ptr_is_null(status)
        or ptr_is_null(overflow)
    ):
        free(slots)
        free(reservations)
        free(started)
        free(status)
        free(overflow)
        return 4294967423
    memset(slots, 0, width * 24)
    memset(started, 0, count)
    failure = 0
    index = 0
    while index < count:
        py_index = py_int_from_i64(index)
        item = py_obj_getitem(weights, py_index)
        py_decref(py_index)
        store_i32(overflow, 0, 0)
        value = py_int_to_i64(item, overflow)
        py_decref(item)
        if load_i32(overflow, 0) != 0 or value <= 0:
            failure = ((index + 1) << 32) | 127
            break
        store_i64(reservations, index * 8, value)
        index += 1
    live = 0
    completed = 0
    available = budget
    while failure == 0 and completed < count:
        slot = 0
        while slot < width:
            pid = load_i64(slots, slot * 24)
            if pid > 0:
                waited = platform_waitpid(pid, status, 1)
                if waited != 0:
                    rc = 127
                    if waited == pid:
                        rc = normalize_wait_status(load_i32(status, 0))
                    platform_kill(-pid, 9)
                    available += load_i64(slots, slot * 24 + 16)
                    store_i64(slots, slot * 24, 0)
                    live -= 1
                    completed += 1
                    if rc != 0:
                        failed_index = load_i64(slots, slot * 24 + 8)
                        failure = ((failed_index + 1) << 32) | (rc & 4294967295)
                        break
            slot += 1
        if failure != 0:
            break
        while live < width and completed + live < count:
            selected = -1
            index = 0
            while index < count:
                if load_i8(started, index) == 0:
                    weight = load_i64(reservations, index * 8)
                    if weight <= available or live == 0:
                        selected = index
                        break
                index += 1
            if selected < 0:
                break
            slot = 0
            while load_i64(slots, slot * 24) != 0:
                slot += 1
            py_index = py_int_from_i64(selected)
            spec = py_obj_getitem(specs, py_index)
            py_decref(py_index)
            zero = py_int_from_i64(0)
            one = py_int_from_i64(1)
            argv = py_obj_getitem(spec, zero)
            env = py_obj_getitem(spec, one)
            py_decref(zero)
            py_decref(one)
            argc = py_obj_len(argv)
            envc = py_obj_len(env)
            items = _build_exec_argv(argv)
            envp = _build_exec_argv(env)
            pid = -1
            if ptr_is_null(items) == 0 and ptr_is_null(envp) == 0:
                pid = platform_spawnp(items, envp, 0)
            _free_exec_argv(items, argc)
            _free_exec_argv(envp, envc)
            py_decref(argv)
            py_decref(env)
            py_decref(spec)
            if pid <= 0:
                failure = ((selected + 1) << 32) | 127
                break
            store_i8(started, selected, 1)
            store_i64(slots, slot * 24, pid)
            store_i64(slots, slot * 24 + 8, selected)
            weight = load_i64(reservations, selected * 8)
            store_i64(slots, slot * 24 + 16, weight)
            available -= weight
            live += 1
        if live > 0 and failure == 0:
            platform_sleep_ns(10000000)
    if failure != 0:
        slot = 0
        while slot < width:
            pid = load_i64(slots, slot * 24)
            if pid > 0:
                platform_kill(-pid, 15)
            slot += 1
        platform_sleep_ns(200000000)
        slot = 0
        while slot < width:
            pid = load_i64(slots, slot * 24)
            if pid > 0:
                platform_kill(-pid, 9)
                platform_waitpid(pid, status, 0)
            slot += 1
    free(overflow)
    free(status)
    free(started)
    free(reservations)
    free(slots)
    return failure


def _spawn_worker_spec(specs, index: int) -> int:
    py_index = py_int_from_i64(index)
    spec = py_obj_getitem(specs, py_index)
    py_decref(py_index)
    if ptr_is_null(spec) != 0:
        return -1
    zero = py_int_from_i64(0)
    one = py_int_from_i64(1)
    argv = py_obj_getitem(spec, zero)
    env = py_obj_getitem(spec, one)
    py_decref(zero)
    py_decref(one)
    argc = py_obj_len(argv)
    envc = py_obj_len(env)
    items = _build_exec_argv(argv)
    envp = _build_exec_argv(env)
    pid = -1
    if ptr_is_null(items) == 0 and ptr_is_null(envp) == 0:
        pid = platform_spawnp(items, envp, 0)
    _free_exec_argv(items, argc)
    _free_exec_argv(envp, envc)
    py_decref(argv)
    py_decref(env)
    py_decref(spec)
    return pid


@c_abi_export("pcc_worker_process_start")
def pcc_worker_process_start(specs, index: int) -> int:
    """Expose the existing owned spawn boundary to the shared scheduler."""
    if index < 0 or index >= py_obj_len(specs):
        return -1
    return _spawn_worker_spec(specs, index)


@c_abi_export("pcc_worker_process_poll")
def pcc_worker_process_poll(pid: int) -> int:
    if pid <= 0:
        return 127
    status = stack_alloc(4)
    waited = platform_waitpid(pid, status, 1)
    if waited == 0:
        return 2147483647
    if waited != pid:
        return 127
    return normalize_wait_status(load_i32(status, 0))


@c_abi_export("pcc_worker_process_stop")
def pcc_worker_process_stop(pid: int) -> int:
    if pid <= 0:
        return -1
    status = stack_alloc(4)
    # The shared controller calls this only for its still-owned, unreaped
    # child. Kill before waitpid releases the PID, never afterwards.
    platform_kill(-pid, 9)
    platform_waitpid(pid, status, 0)
    return 0


def _followup_file_size(paths, index: int) -> int:
    py_index = py_int_from_i64(index)
    item = py_obj_getitem(paths, py_index)
    py_decref(py_index)
    if ptr_is_null(item) != 0:
        return -1
    text = py_obj_str(item)
    py_decref(item)
    if ptr_is_null(text) != 0:
        return -1
    raw = py_str_utf8(text)
    size = -1
    if ptr_is_null(raw) == 0:
        fd: int = open_readonly(raw)
        if fd >= 0:
            end: int = seek_file(fd, 0, 2)
            if end >= 0:
                size = end
            close(fd)
    py_decref(text)
    return size


@c_abi_export("pcc_chained_worker_process_pool")
def pcc_chained_worker_process_pool(
    specs, weights, followups, followup_paths, followup_floor,
    width: int, budget: int,
) -> int:
    """A weighted pool whose job ``i`` has one follow-up ``count + i``.

    The follow-up becomes ready when job ``i`` exits 0; its reservation is
    known only then, from the size of ``followup_paths[i]`` that job ``i``
    wrote: ``min(cap, base + size * per_mb // 1000000)``.  Primaries keep
    priority; a ready follow-up takes a slot only when no remaining primary
    fits, so follow-ups fill the primaries' ramp and tail instead of
    displacing them.  Failure packing matches the other pools, follow-up
    indices after the primaries.
    """
    count = py_obj_len(specs)
    if count <= 0:
        return 0
    if (
        count > 524288
        or py_obj_len(weights) != count
        or py_obj_len(followups) != count
        or py_obj_len(followup_paths) != count
        or py_obj_len(followup_floor) != 3
        or budget <= 0
    ):
        return 4294967423
    overflow = malloc(4)
    floor_values = malloc(24)
    if ptr_is_null(overflow) or ptr_is_null(floor_values):
        free(overflow)
        free(floor_values)
        return 4294967423
    index = 0
    while index < 3:
        py_index = py_int_from_i64(index)
        item = py_obj_getitem(followup_floor, py_index)
        py_decref(py_index)
        store_i32(overflow, 0, 0)
        value = py_int_to_i64(item, overflow)
        py_decref(item)
        if load_i32(overflow, 0) != 0 or value < 0:
            free(overflow)
            free(floor_values)
            return 4294967423
        store_i64(floor_values, index * 8, value)
        index += 1
    floor_base = load_i64(floor_values, 0)
    floor_per_mb = load_i64(floor_values, 8)
    floor_cap = load_i64(floor_values, 16)
    free(floor_values)
    total = count * 2
    if width < 1:
        width = 1
    if width > total:
        width = total
    slots = malloc(width * 24)
    reservations = malloc(total * 8)
    # 0: waits for its primary, 1: ready, 2: started
    state = malloc(total)
    status = malloc(4)
    if (
        ptr_is_null(slots) or ptr_is_null(reservations)
        or ptr_is_null(state) or ptr_is_null(status)
    ):
        free(slots)
        free(reservations)
        free(state)
        free(status)
        free(overflow)
        return 4294967423
    memset(slots, 0, width * 24)
    memset(state, 0, total)
    failure = 0
    index = 0
    while index < count:
        py_index = py_int_from_i64(index)
        item = py_obj_getitem(weights, py_index)
        py_decref(py_index)
        store_i32(overflow, 0, 0)
        value = py_int_to_i64(item, overflow)
        py_decref(item)
        if load_i32(overflow, 0) != 0 or value <= 0:
            failure = ((index + 1) << 32) | 127
            break
        store_i64(reservations, index * 8, value)
        store_i8(state, index, 1)
        index += 1
    live = 0
    completed = 0
    available = budget
    while failure == 0 and completed < total:
        slot = 0
        while slot < width:
            pid = load_i64(slots, slot * 24)
            if pid > 0:
                waited = platform_waitpid(pid, status, 1)
                if waited != 0:
                    rc = 127
                    if waited == pid:
                        rc = normalize_wait_status(load_i32(status, 0))
                    platform_kill(-pid, 9)
                    available += load_i64(slots, slot * 24 + 16)
                    store_i64(slots, slot * 24, 0)
                    live -= 1
                    completed += 1
                    finished = load_i64(slots, slot * 24 + 8)
                    if rc != 0:
                        failure = ((finished + 1) << 32) | (rc & 4294967295)
                        break
                    if finished < count:
                        size = _followup_file_size(followup_paths, finished)
                        if size < 0:
                            failure = ((count + finished + 1) << 32) | 127
                            break
                        reservation = floor_base + size * floor_per_mb // 1000000
                        if reservation > floor_cap:
                            reservation = floor_cap
                        if reservation < 1:
                            reservation = 1
                        store_i64(reservations, (count + finished) * 8, reservation)
                        store_i8(state, count + finished, 1)
            slot += 1
        if failure != 0:
            break
        while live < width:
            selected = -1
            index = 0
            while index < count and selected < 0:
                if load_i8(state, index) == 1:
                    weight = load_i64(reservations, index * 8)
                    if weight <= available or live == 0:
                        selected = index
                index += 1
            index = count
            while index < total and selected < 0:
                if load_i8(state, index) == 1:
                    weight = load_i64(reservations, index * 8)
                    if weight <= available or live == 0:
                        selected = index
                index += 1
            if selected < 0:
                break
            slot = 0
            while load_i64(slots, slot * 24) != 0:
                slot += 1
            pid = -1
            if selected < count:
                pid = _spawn_worker_spec(specs, selected)
            else:
                pid = _spawn_worker_spec(followups, selected - count)
            if pid <= 0:
                failure = ((selected + 1) << 32) | 127
                break
            store_i8(state, selected, 2)
            store_i64(slots, slot * 24, pid)
            store_i64(slots, slot * 24 + 8, selected)
            weight = load_i64(reservations, selected * 8)
            store_i64(slots, slot * 24 + 16, weight)
            available -= weight
            live += 1
        if live > 0 and failure == 0:
            platform_sleep_ns(10000000)
    if failure != 0:
        slot = 0
        while slot < width:
            pid = load_i64(slots, slot * 24)
            if pid > 0:
                platform_kill(-pid, 15)
            slot += 1
        platform_sleep_ns(200000000)
        slot = 0
        while slot < width:
            pid = load_i64(slots, slot * 24)
            if pid > 0:
                platform_kill(-pid, 9)
                platform_waitpid(pid, status, 0)
            slot += 1
    free(overflow)
    free(status)
    free(state)
    free(reservations)
    free(slots)
    return failure
