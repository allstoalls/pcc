"""Owned thread-kernel ABI adapters over Windows threads, SRW locks and CVs.

The shared GC thread-registration/safepoint kernel remains in
freestanding_thread_kernel_pthread; its small pthread-shaped substrate is
implemented here. No pthread DLL or C runtime is linked.
"""

from pcc import i64
from pcc.extern import c_abi_export, c_int, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    malloc, free, null, ptr_is_null, load_ptr, load_i32, load_i64, store_ptr, store_i64,
    function_addr, call_ptr1, atomic_rmw_i64, stack_alloc,
)

__pcc_freestanding__ = True

CreateThread = extern("CreateThread", (c_ptr, c_int64, c_ptr, c_ptr, c_int, c_ptr), c_ptr)
WaitForSingleObject = extern("WaitForSingleObject", (c_ptr, c_int), c_int)
CloseHandle = extern("CloseHandle", (c_ptr,), c_int)
AcquireSRWLockExclusive = extern("AcquireSRWLockExclusive", (c_ptr,), c_void)
TryAcquireSRWLockExclusive = extern("TryAcquireSRWLockExclusive", (c_ptr,), c_int)
ReleaseSRWLockExclusive = extern("ReleaseSRWLockExclusive", (c_ptr,), c_void)
SleepConditionVariableSRW = extern("SleepConditionVariableSRW", (c_ptr, c_ptr, c_int, c_int), c_int)
WakeConditionVariable = extern("WakeConditionVariable", (c_ptr,), c_void)
WakeAllConditionVariable = extern("WakeAllConditionVariable", (c_ptr,), c_void)
Sleep = extern("Sleep", (c_int,), c_void)
GetLastError = extern("GetLastError", (), c_int)
wall_time_us = extern("pcc_platform_wall_time_us", (), c_int64)


@c_abi_export("pcc_windows_threads_release_record")
def release_record(record: c_ptr) -> None:
    if atomic_rmw_i64("add", record, 32, -1, "acq_rel") == 1:
        free(record)


@c_abi_export("pcc_win_thread_entry")
def thread_entry(record: c_ptr) -> i64:
    result = call_ptr1(load_ptr(record, 8), load_ptr(record, 16))
    store_ptr(record, 24, result)
    release_record(record)
    return 0


@c_abi_export("pthread_create")
def pthread_create(output: c_ptr, attrs: c_ptr, function: c_ptr, argument: c_ptr) -> i64:
    if not ptr_is_null(attrs):
        return 22
    record = malloc(40)
    if ptr_is_null(record):
        return 12
    store_ptr(record, 8, function)
    store_ptr(record, 16, argument)
    store_ptr(record, 24, null())
    store_i64(record, 32, 2)
    handle = CreateThread(null(), 0, function_addr("pcc_win_thread_entry"), record, 0, null())
    if ptr_is_null(handle):
        free(record)
        return 11
    store_ptr(record, 0, handle)
    store_ptr(output, 0, record)
    return 0


@c_abi_export("pthread_join")
def pthread_join(record: c_ptr, output: c_ptr) -> i64:
    if ptr_is_null(record):
        return 22
    if WaitForSingleObject(load_ptr(record, 0), -1) != 0:
        return 22
    if not ptr_is_null(output):
        store_ptr(output, 0, load_ptr(record, 24))
    CloseHandle(load_ptr(record, 0))
    release_record(record)
    return 0


@c_abi_export("pthread_detach")
def pthread_detach(record: c_ptr) -> i64:
    if ptr_is_null(record):
        return 22
    CloseHandle(load_ptr(record, 0))
    release_record(record)
    return 0


@c_abi_export("pthread_mutex_init")
def mutex_init(lock: c_ptr, attrs: c_ptr) -> i64:
    if not ptr_is_null(attrs):
        return 22
    store_i64(lock, 0, 0)
    return 0


@c_abi_export("pthread_mutex_destroy")
def mutex_destroy(lock: c_ptr) -> i64:
    return 0


@c_abi_export("pthread_mutex_lock")
def mutex_lock(lock: c_ptr) -> i64:
    AcquireSRWLockExclusive(lock)
    return 0


@c_abi_export("pthread_mutex_trylock")
def mutex_trylock(lock: c_ptr) -> i64:
    if TryAcquireSRWLockExclusive(lock):
        return 0
    return 16


@c_abi_export("pthread_mutex_unlock")
def mutex_unlock(lock: c_ptr) -> i64:
    ReleaseSRWLockExclusive(lock)
    return 0


@c_abi_export("pthread_cond_init")
def cond_init(condition: c_ptr, attrs: c_ptr) -> i64:
    if not ptr_is_null(attrs):
        return 22
    store_i64(condition, 0, 0)
    return 0


@c_abi_export("pthread_cond_destroy")
def cond_destroy(condition: c_ptr) -> i64:
    return 0


@c_abi_export("pthread_cond_wait")
def cond_wait(condition: c_ptr, lock: c_ptr) -> i64:
    if SleepConditionVariableSRW(condition, lock, -1, 0):
        return 0
    return 22


@c_abi_export("pthread_cond_timedwait")
def cond_timedwait(condition: c_ptr, lock: c_ptr, timeout: c_ptr) -> i64:
    # Windows C timespec uses a 64-bit time_t and 32-bit long tv_nsec;
    # the four padding bytes must not participate in the timeout value.
    nanos: i64 = load_i32(timeout, 8)
    if nanos < 0 or nanos >= 1000000000:
        return 22
    deadline: i64 = load_i64(timeout, 0) * 1000000 + nanos // 1000
    remaining: i64 = deadline - wall_time_us()
    if remaining < 0:
        remaining = 0
    milliseconds: i64 = (remaining + 999) // 1000
    if milliseconds > 4294967294:
        milliseconds = 4294967294
    if SleepConditionVariableSRW(condition, lock, milliseconds, 0):
        return 0
    if GetLastError() == 1460:
        return 110
    return 22


@c_abi_export("pthread_cond_signal")
def cond_signal(condition: c_ptr) -> i64:
    WakeConditionVariable(condition)
    return 0


@c_abi_export("pthread_cond_broadcast")
def cond_broadcast(condition: c_ptr) -> i64:
    WakeAllConditionVariable(condition)
    return 0


@c_abi_export("sched_yield")
def sched_yield() -> i64:
    Sleep(0)
    return 0
