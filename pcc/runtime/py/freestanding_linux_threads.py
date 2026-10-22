"""Zero-libc Linux thread/mutex/condition substrate using clone and futex.

The shared GC registration and stop-the-world kernel retains its existing
pthread-shaped internal ABI. These are pcc-owned implementations, with private
TLS copied from PT_TLS and compiler-owned clone/exit machine boundaries.
"""

from pcc import i64
from pcc.extern import c_abi_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    page_alloc, page_free, malloc, free, null, ptr_is_null, ptr_add,
    load_i8, load_i32, load_i64, load_ptr, store_i32, store_i64, store_ptr,
    atomic_load_i32, atomic_store_i32, atomic_cas_i32, atomic_rmw_i32,
    atomic_rmw_i64, function_addr, call_ptr1, syscall6, target_platform_machine,
    stack_alloc, clock_gettime,
)

__pcc_freestanding__ = True

allocate_tls = extern("pcc_linux_allocate_tls", (c_ptr,), c_ptr)
clone_start = extern("pcc_linux_thread_spawn", (c_ptr, c_ptr, c_ptr, c_ptr, c_ptr), c_int64)
exit_free = extern("pcc_linux_thread_exit_free", (c_ptr, c_int64, c_ptr, c_int64), c_void)


@c_abi_export("pcc_linux_threads_arm64")
def arm64() -> i64:
    if load_i8(target_platform_machine(), 0) == 97:
        return 1
    return 0


@c_abi_export("pcc_linux_threads_futex")
def futex(address: c_ptr, operation: i64, value: i64, timeout: c_ptr) -> i64:
    number: i64 = 202
    if arm64():
        number = 98
    return syscall6(number, address, operation, value, timeout, 0, 0)


@c_abi_export("pcc_linux_threads_dispose")
def dispose(record: c_ptr) -> None:
    page_free(load_ptr(record, 16), load_i64(record, 24))
    page_free(load_ptr(record, 32), load_i64(record, 40))
    free(record)


@c_abi_export("pcc_linux_thread_entry")
def thread_entry(record: c_ptr) -> c_ptr:
    result = call_ptr1(load_ptr(record, 56), load_ptr(record, 64))
    store_ptr(record, 8, result)
    if atomic_rmw_i64("sub", record, 72, 1, "acq_rel") == 1:
        # Detached and still on this stack: stop the kernel from writing the
        # soon-to-be-freed TID slot, then enter a nonreturning register-only
        # routine that unmaps this thread's TLS/stack and calls SYS_exit.
        syscall6(96 if arm64() else 218, 0, 0, 0, 0, 0, 0)
        stack = load_ptr(record, 16)
        stack_size: i64 = load_i64(record, 24)
        tls = load_ptr(record, 32)
        tls_size: i64 = load_i64(record, 40)
        free(record)
        exit_free(stack, stack_size, tls, tls_size)
    return result


@c_abi_export("pthread_create")
def pthread_create(output: c_ptr, attrs: c_ptr, function: c_ptr, argument: c_ptr) -> i64:
    if not ptr_is_null(attrs):
        return 22
    record = malloc(80)
    if ptr_is_null(record):
        return 12
    store_i32(record, 0, 0)
    store_ptr(record, 8, null())
    stack_size: i64 = 8388608 + 65536
    stack = page_alloc(stack_size)
    if ptr_is_null(stack):
        free(record)
        return 12
    if syscall6(226 if arm64() else 10, stack, 65536, 0, 0, 0, 0) != 0:
        page_free(stack, stack_size)
        free(record)
        return 12
    store_ptr(record, 16, stack)
    store_i64(record, 24, stack_size)
    tls = allocate_tls(ptr_add(record, 32))
    if ptr_is_null(tls):
        page_free(stack, stack_size)
        free(record)
        return 12
    store_ptr(record, 48, tls)
    store_ptr(record, 56, function)
    store_ptr(record, 64, argument)
    store_i64(record, 72, 2)
    tid: i64 = clone_start(ptr_add(stack, stack_size), tls,
                           function_addr("pcc_linux_thread_entry"), record, record)
    if tid < 0:
        dispose(record)
        return 0 - tid
    store_ptr(output, 0, record)
    return 0


@c_abi_export("pthread_join")
def pthread_join(record: c_ptr, output: c_ptr) -> i64:
    if ptr_is_null(record):
        return 22
    while True:
        tid: i64 = atomic_load_i32(record, 0, "acquire")
        if tid == 0:
            break
        # Linux's CHILD_CLEARTID wake uses the shared futex key, even though
        # the compiler's ordinary mutexes use FUTEX_PRIVATE_FLAG.
        status: i64 = futex(record, 0, tid, null())
        if status < 0 and status != -4 and status != -11:
            return 0 - status
    if not ptr_is_null(output):
        store_ptr(output, 0, load_ptr(record, 8))
    dispose(record)
    return 0


@c_abi_export("pthread_detach")
def pthread_detach(record: c_ptr) -> i64:
    if ptr_is_null(record):
        return 22
    if atomic_rmw_i64("sub", record, 72, 1, "acq_rel") == 1:
        return pthread_join(record, null())
    return 0


@c_abi_export("pthread_mutex_init")
def mutex_init(lock: c_ptr, attrs: c_ptr) -> i64:
    if not ptr_is_null(attrs):
        return 22
    atomic_store_i32(lock, 0, 0, "relaxed")
    return 0


@c_abi_export("pthread_mutex_destroy")
def mutex_destroy(lock: c_ptr) -> i64:
    if atomic_load_i32(lock, 0, "relaxed") == 0:
        return 0
    return 16


@c_abi_export("pthread_mutex_trylock")
def mutex_trylock(lock: c_ptr) -> i64:
    if atomic_cas_i32(lock, 0, 0, 1, "acquire", "relaxed") == 0:
        return 0
    return 16


@c_abi_export("pthread_mutex_lock")
def mutex_lock(lock: c_ptr) -> i64:
    if mutex_trylock(lock) == 0:
        return 0
    while True:
        old: i64 = atomic_rmw_i32("xchg", lock, 0, 2, "acquire")
        if old == 0:
            return 0
        status: i64 = futex(lock, 128, 2, null())
        if status < 0 and status != -4 and status != -11:
            return 0 - status
    return 0


@c_abi_export("pthread_mutex_unlock")
def mutex_unlock(lock: c_ptr) -> i64:
    previous: i64 = atomic_rmw_i32("xchg", lock, 0, 0, "release")
    if previous == 2:
        futex(lock, 129, 1, null())
    return 0


@c_abi_export("pthread_cond_init")
def cond_init(condition: c_ptr, attrs: c_ptr) -> i64:
    if not ptr_is_null(attrs):
        return 22
    atomic_store_i32(condition, 0, 0, "relaxed")
    return 0


@c_abi_export("pthread_cond_destroy")
def cond_destroy(condition: c_ptr) -> i64:
    return 0


@c_abi_export("pthread_cond_wait")
def cond_wait(condition: c_ptr, lock: c_ptr) -> i64:
    generation: i64 = atomic_load_i32(condition, 0, "acquire")
    mutex_unlock(lock)
    status: i64 = futex(condition, 128, generation, null())
    mutex_lock(lock)
    if status >= 0 or status == -4 or status == -11:
        return 0
    return 0 - status


@c_abi_export("pthread_cond_timedwait")
def cond_timedwait(condition: c_ptr, lock: c_ptr, deadline: c_ptr) -> i64:
    generation: i64 = atomic_load_i32(condition, 0, "acquire")
    now = stack_alloc(16)
    if clock_gettime(0, now) != 0:
        return 22
    seconds: i64 = load_i64(deadline, 0) - load_i64(now, 0)
    nanos: i64 = load_i64(deadline, 8) - load_i64(now, 8)
    if nanos < 0:
        seconds = seconds - 1
        nanos = nanos + 1000000000
    if seconds < 0:
        return 110
    store_i64(now, 0, seconds)
    store_i64(now, 8, nanos)
    mutex_unlock(lock)
    status: i64 = futex(condition, 128, generation, now)
    mutex_lock(lock)
    if status >= 0 or status == -4 or status == -11:
        return 0
    return 0 - status


@c_abi_export("pthread_cond_signal")
def cond_signal(condition: c_ptr) -> i64:
    atomic_rmw_i32("add", condition, 0, 1, "release")
    futex(condition, 129, 1, null())
    return 0


@c_abi_export("pthread_cond_broadcast")
def cond_broadcast(condition: c_ptr) -> i64:
    atomic_rmw_i32("add", condition, 0, 1, "release")
    futex(condition, 129, 2147483647, null())
    return 0


@c_abi_export("sched_yield")
def sched_yield() -> i64:
    selected_arg: i64 = 24
    if arm64():
        selected_arg = 124
    return syscall6(selected_arg, 0, 0, 0, 0, 0, 0)
