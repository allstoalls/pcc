"""Owned Linux sigaction adapter from the runtime's libc-shaped record."""

from pcc import i64
from pcc.extern import c_abi_export, c_int32, c_void, extern
from pcc.unsafe import (
    syscall6, target_platform_machine, stack_alloc, ptr_is_null, null,
    load_i8, load_i32, load_i64, load_ptr, store_i32, store_i64, store_ptr,
    memset, global_addr,
)

__pcc_freestanding__ = True

set_errno = extern("pcc_errno_set", (c_int32,), c_void)


_KERNEL_SIGNAL_MASK_BYTES = 8
_KERNEL_SIGNAL_ACTION_BYTES = 32


@c_abi_export("abort")
def abort() -> None:
    # Public C abort is distinct from the runtime's internal fatal primitive.
    # Deliver to this thread, including when the signal was initially blocked
    # or ignored. A returning handler is followed by the default disposition.
    arm: i64 = 1 if load_i8(target_platform_machine(), 0) == 97 else 0
    mask_call: i64 = 135 if arm else 14
    action_call: i64 = 134 if arm else 13
    pid_call: i64 = 172 if arm else 39
    tid_call: i64 = 178 if arm else 186
    send_call: i64 = 131 if arm else 234
    exit_call: i64 = 94 if arm else 231
    abort_signal: i64 = 6
    unblock: i64 = 1
    mask = stack_alloc(_KERNEL_SIGNAL_MASK_BYTES)
    store_i64(mask, 0, 1 << (abort_signal - 1))
    pid: i64 = syscall6(pid_call, 0, 0, 0, 0, 0, 0)
    tid: i64 = syscall6(tid_call, 0, 0, 0, 0, 0, 0)
    syscall6(mask_call, unblock, mask, null(), _KERNEL_SIGNAL_MASK_BYTES, 0, 0)
    syscall6(send_call, pid, tid, abort_signal, 0, 0, 0)
    action = stack_alloc(_KERNEL_SIGNAL_ACTION_BYTES)
    memset(action, 0, _KERNEL_SIGNAL_ACTION_BYTES)
    syscall6(action_call, abort_signal, action, null(), _KERNEL_SIGNAL_MASK_BYTES, 0, 0)
    syscall6(mask_call, unblock, mask, null(), _KERNEL_SIGNAL_MASK_BYTES, 0, 0)
    syscall6(send_call, pid, tid, abort_signal, 0, 0, 0)
    # If a concurrent disposition change prevents the second delivery from
    # terminating, POSIX still forbids returning. Exit the entire thread group;
    # repeat only if the kernel refuses that non-returning exit operation.
    while True:
        syscall6(exit_call, 134, 0, 0, 0, 0, 0)


@c_abi_export("sigemptyset")
def sigemptyset(mask) -> i64:
    if ptr_is_null(mask):
        set_errno(22)
        return -1
    memset(mask, 0, 128)
    return 0


@c_abi_export("sigaction")
def sigaction(number: i64, action, old_action) -> i64:
    native = stack_alloc(32)
    previous = stack_alloc(32)
    input_ptr = null()
    output_ptr = null()
    if not ptr_is_null(action):
        flags: i64 = load_i32(action, 136) & 4294967295
        restorer = load_ptr(action, 144)
        if not (flags & 67108864) or ptr_is_null(restorer):
            restorer = global_addr("pcc_linux_rt_sigreturn")
        store_ptr(native, 0, load_ptr(action, 0))
        store_i64(native, 8, flags | 67108864)
        store_ptr(native, 16, restorer)
        store_i64(native, 24, load_i64(action, 8))
        input_ptr = native
    if not ptr_is_null(old_action):
        output_ptr = previous
    arm: i64 = 1 if load_i8(target_platform_machine(), 0) == 97 else 0
    result: i64 = syscall6(134 if arm else 13, number, input_ptr, output_ptr, 8, 0, 0)
    if result < 0:
        set_errno(0 - result)
        return -1
    if not ptr_is_null(old_action):
        memset(old_action, 0, 152)
        store_ptr(old_action, 0, load_ptr(previous, 0))
        store_i64(old_action, 8, load_i64(previous, 24))
        store_i32(old_action, 136, load_i64(previous, 8))
        store_ptr(old_action, 144, load_ptr(previous, 16))
    return 0
