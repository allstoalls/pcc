"""CRT-free Windows executable entry with UTF-8 argv and environment."""

from pcc import i64
from pcc.extern import c_abi_export, c_ptr, c_int, c_int32, c_void, extern
from pcc.unsafe import (
    malloc, free, null, ptr_is_null, load_i8, load_i32, load_i64, load_ptr, store_i8, store_ptr,
    stack_alloc, global_addr, function_addr, ptr_diff, ptr_to_int, int_to_ptr, cstr, call_void_ptr0,
)

__pcc_freestanding__ = True

GetCommandLineW = extern("GetCommandLineW", (), c_ptr)
CommandLineToArgvW = extern("CommandLineToArgvW", (c_ptr, c_ptr), c_ptr)
LocalFree = extern("LocalFree", (c_ptr,), c_ptr)
ExitProcess = extern("ExitProcess", (c_int,), c_void)
c_exit = extern("exit", (c_int32,), c_void)
utf8 = extern("pcc_win_utf8", (c_ptr,), c_ptr)
initial_environ = extern("pcc_win_initial_environ", (), c_ptr)
env_init = extern("pcc_platform_env_init", (c_ptr,), c_int)
main = extern("main", (c_int, c_ptr, c_ptr), c_int)
SetUnhandledExceptionFilter = extern("SetUnhandledExceptionFilter", (c_ptr,), c_ptr)
GetCurrentThreadStackLimits = extern("GetCurrentThreadStackLimits", (c_ptr, c_ptr), c_void)
GetModuleHandleW = extern("GetModuleHandleW", (c_ptr,), c_ptr)
GetStdHandle = extern("GetStdHandle", (c_int,), c_ptr)
WriteFile = extern("WriteFile", (c_ptr, c_ptr, c_int, c_ptr, c_ptr), c_int)


@c_abi_export("pcc_windows_fault_report_text")
def _report_text(buffer: c_ptr, position: i64, text: c_ptr) -> i64:
    index: i64 = 0
    while load_i8(text, index) != 0:
        store_i8(buffer, position + index, load_i8(text, index))
        index = index + 1
    return position + index


@c_abi_export("pcc_windows_fault_report_hex")
def _report_hex(buffer: c_ptr, position: i64, value: i64) -> i64:
    store_i8(buffer, position, 48)
    store_i8(buffer, position + 1, 120)
    shift: i64 = 60
    index: i64 = 2
    while shift >= 0:
        digit: i64 = (value >> shift) & 15
        if digit < 10:
            store_i8(buffer, position + index, 48 + digit)
        else:
            store_i8(buffer, position + index, 87 + digit)
        shift = shift - 4
        index = index + 1
    return position + index


@c_abi_export("pcc_windows_fault_report_write")
def _report_write(buffer: c_ptr, length: i64) -> None:
    written = stack_alloc(8)
    WriteFile(GetStdHandle(-12), buffer, length, written, null())


@c_abi_export("pcc_windows_unhandled_exception")
def pcc_windows_unhandled_exception(pointers: c_ptr) -> i64:
    """Report an unhandled SEH fault; Windows then ends the process as before.

    Owned executables have no CRT or debugger hook, so an access violation
    otherwise exits with its status and no location. Write only from stack
    storage: the heap or runtime may be the corrupted state. Addresses are
    also printed relative to the executable image so a link can be mapped.
    Frames follow the RBP chain the x86-64 backend keeps, bounded by the
    current thread's committed stack range.
    """
    buffer = stack_alloc(256)
    record = load_ptr(pointers, 0)
    context = load_ptr(pointers, 8)
    base: i64 = ptr_to_int(GetModuleHandleW(null()))
    rip: i64 = load_i64(context, 248)
    position: i64 = _report_text(buffer, 0, cstr("PCC_UNHANDLED_EXCEPTION code="))
    position = _report_hex(buffer, position, load_i32(record, 0) & 4294967295)
    position = _report_text(buffer, position, cstr(" rip_rva="))
    position = _report_hex(buffer, position, rip - base)
    position = _report_text(buffer, position, cstr(" image="))
    position = _report_hex(buffer, position, base)
    if load_i32(record, 24) >= 2:
        position = _report_text(buffer, position, cstr(" access="))
        position = _report_hex(buffer, position, load_i64(record, 32))
        position = _report_text(buffer, position, cstr(" target="))
        position = _report_hex(buffer, position, load_i64(record, 40))
    store_i8(buffer, position, 10)
    _report_write(buffer, position + 1)
    limits = stack_alloc(16)
    GetCurrentThreadStackLimits(limits, int_to_ptr(ptr_to_int(limits) + 8))
    low: i64 = load_i64(limits, 0)
    high: i64 = load_i64(limits, 8)
    frame: i64 = load_i64(context, 160)
    depth: i64 = 0
    while depth < 48 and frame >= low and frame + 16 <= high and (frame & 7) == 0:
        return_address: i64 = load_i64(int_to_ptr(frame), 8)
        position = _report_text(buffer, 0, cstr("PCC_UNHANDLED_FRAME "))
        position = _report_hex(buffer, position, depth)
        position = _report_text(buffer, position, cstr(" return_rva="))
        position = _report_hex(buffer, position, return_address - base)
        store_i8(buffer, position, 10)
        _report_write(buffer, position + 1)
        next_frame: i64 = load_i64(int_to_ptr(frame), 0)
        if next_frame <= frame:
            depth = 48
        frame = next_frame
        depth = depth + 1
    return 0


@c_abi_export("pcc_windows_start")
def pcc_windows_start() -> None:
    SetUnhandledExceptionFilter(function_addr("pcc_windows_unhandled_exception"))
    argc_slot = stack_alloc(8)
    wide_argv = CommandLineToArgvW(GetCommandLineW(), argc_slot)
    if ptr_is_null(wide_argv):
        ExitProcess(71)
        return
    argc: i64 = load_i32(argc_slot, 0)
    argv = malloc((argc + 1) * 8)
    if ptr_is_null(argv):
        LocalFree(wide_argv)
        ExitProcess(71)
        return
    index: i64 = 0
    while index < argc:
        value = utf8(load_ptr(wide_argv, index * 8))
        if ptr_is_null(value):
            ExitProcess(71)
            return
        store_ptr(argv, index * 8, value)
        index = index + 1
    store_ptr(argv, argc * 8, null())
    LocalFree(wide_argv)
    env = initial_environ()
    if ptr_is_null(env) or env_init(env) != 0:
        ExitProcess(71)
        return
    initializers = global_addr("__init_array_start")
    count: i64 = ptr_diff(global_addr("__init_array_end"), initializers) // 8
    index = 0
    while index < count:
        call_void_ptr0(load_ptr(initializers, index * 8))
        index = index + 1
    status: i64 = main(argc, argv, env)
    # Normal return shares the owned C lifecycle with an explicit exit call.
    # Startup failures above and the process_exit intrinsic stay immediate.
    c_exit(status)
