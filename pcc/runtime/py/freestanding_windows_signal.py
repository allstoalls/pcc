"""Console signal adapters using the Windows system handler ABI."""

from pcc import i64
from pcc.extern import c_abi_export, c_ptr, c_int, c_int32, c_void, extern
from pcc.unsafe import (
    define_global_i64_array, define_global_i32, global_addr, atomic_load_i64, atomic_rmw_i64,
    atomic_load_i32, atomic_store_i32, atomic_cas_i32, load_i32, load_i64,
    function_addr, load_ptr, store_ptr, ptr_is_null, ptr_to_int, int_to_ptr,
    call_void_i32, memset,
)

__pcc_freestanding__ = True

SetConsoleCtrlHandler = extern("SetConsoleCtrlHandler", (c_ptr, c_int), c_int)
set_errno = extern("pcc_errno_set", (c_int32,), c_void)

define_global_i64_array("pcc_win_console_signals", 0, 0, 0)
define_global_i32("pcc_win_console_initialized", 0)


@c_abi_export("pcc_win_console_handler")
def console_handler(event: i64) -> i64:
    index: i64 = -1
    number: i64 = 0
    if event == 0:
        index = 0
        number = 2
    elif event == 1:
        index = 2
        number = 21
    elif event == 2 or event == 5 or event == 6:
        index = 1
        number = 15
    if index < 0:
        return 0
    bits: i64 = atomic_load_i64(global_addr("pcc_win_console_signals"), index * 8, "acquire")
    if bits == 0:
        return 0
    if bits != 1:
        call_void_i32(int_to_ptr(bits), number)
    return 1


@c_abi_export("pcc_windows_signal_ensure_handler")
def ensure_handler() -> i64:
    state = global_addr("pcc_win_console_initialized")
    while True:
        value: i64 = atomic_load_i32(state, 0, "acquire")
        if value == 2:
            return 0
        if value == 0 and atomic_cas_i32(state, 0, 0, 1, "acq_rel", "acquire") == 0:
            ok: i64 = SetConsoleCtrlHandler(function_addr("pcc_win_console_handler"), 1)
            atomic_store_i32(state, 0, 2 if ok else 0, "release")
            if ok:
                return 0
            return -1
    return -1


@c_abi_export("sigemptyset")
def sigemptyset(mask: c_ptr) -> i64:
    if ptr_is_null(mask):
        set_errno(22)
        return -1
    memset(mask, 0, 128)
    return 0


@c_abi_export("sigaction")
def sigaction(number: i64, action: c_ptr, old_action: c_ptr) -> i64:
    index: i64 = 0 if number == 2 else 1 if number == 15 else 2 if number == 21 else -1
    if index < 0:
        set_errno(38)
        return -1
    handlers = global_addr("pcc_win_console_signals")
    previous: i64 = atomic_load_i64(handlers, index * 8, "acquire")
    if not ptr_is_null(action):
        if load_i32(action, 136) != 0:
            set_errno(38)
            return -1
        offset: i64 = 8
        while offset < 136:
            if load_i64(action, offset) != 0:
                set_errno(38)
                return -1
            offset = offset + 8
        if ensure_handler() != 0:
            set_errno(22)
            return -1
        previous = atomic_rmw_i64("xchg", handlers, index * 8, ptr_to_int(load_ptr(action, 0)), "acq_rel")
    if not ptr_is_null(old_action):
        memset(old_action, 0, 152)
        store_ptr(old_action, 0, int_to_ptr(previous))
    return 0
