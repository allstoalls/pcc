"""pcc-Python replacement for py_runtime/src/py_process.c.

This module is used by the pcc-Python runtime archive. It also defines
the argv globals consumed by the optional py_libpython bridge archive.
"""

__pcc_runtime_port__ = True

from pcc.extern import extern, c_abi_export, c_int, c_int32, c_ptr, c_void
from pcc.unsafe import (
    call_void_ptr0,
    cstr,
    define_global_i32,
    define_global_ptr_null,
    global_addr,
    global_load_ptr,
    global_store_ptr,
    load_i32,
    load_ptr,
    null,
    ptr_add,
    ptr_is_null,
    store_i32,
)


fflush = extern("fflush", (c_ptr,), c_int32)
strcmp = extern("strcmp", (c_ptr, c_ptr), c_int32)
platform_exit = extern("pcc_platform_exit", (c_int,), c_void)


define_global_i32("py_runtime_program_argc", 0)
define_global_i32("py_runtime_program_mode", 0)
define_global_ptr_null("py_runtime_program_argv")
define_global_ptr_null("py_runtime_program_executable")
define_global_ptr_null("py_runtime_program_args_hook")


@c_abi_export("py_set_program_args")
def py_set_program_args(argc: int, argv: object) -> None:
    executable = null()
    if argc > 0 and not ptr_is_null(argv):
        executable = load_ptr(argv, 0)
    global_store_ptr("py_runtime_program_executable", executable)
    mode_value: int = 0
    if argc >= 4 and not ptr_is_null(argv):
        marker = load_ptr(argv, 8)
        if not ptr_is_null(marker) and strcmp(
            marker, cstr("--pcc-internal-python-argv0-v1")
        ) == 0:
            mode = load_ptr(argv, 16)
            if not ptr_is_null(mode):
                if strcmp(mode, cstr("script")) == 0:
                    mode_value = 1
                elif strcmp(mode, cstr("module")) == 0:
                    mode_value = 2
                elif strcmp(mode, cstr("command")) == 0:
                    mode_value = 3
                elif strcmp(mode, cstr("stdin")) == 0:
                    mode_value = 4
            if mode_value != 0:
                argc -= 3
                argv = ptr_add(argv, 24)
    argc_slot = global_addr("py_runtime_program_argc")
    if argc > 0:
        store_i32(argc_slot, 0, argc)
    else:
        store_i32(argc_slot, 0, 0)
    global_store_ptr("py_runtime_program_argv", argv)
    store_i32(global_addr("py_runtime_program_mode"), 0, mode_value)
    hook = global_load_ptr("py_runtime_program_args_hook")
    if not ptr_is_null(hook):
        call_void_ptr0(hook)


@c_abi_export("py_program_executable")
def py_program_executable():
    value = global_load_ptr("py_runtime_program_executable")
    if ptr_is_null(value):
        return cstr("")
    return value


@c_abi_export("py_program_mode")
def py_program_mode() -> int:
    return load_i32(global_addr("py_runtime_program_mode"), 0)


@c_abi_export("py_program_argc")
def py_program_argc() -> int:
    argc: int = load_i32(global_addr("py_runtime_program_argc"), 0)
    if argc > 0:
        return argc
    return 0


@c_abi_export("py_program_argv")
def py_program_argv(index: int):
    argc: int = load_i32(global_addr("py_runtime_program_argc"), 0)
    argv = global_load_ptr("py_runtime_program_argv")
    if index < 0:
        return null()
    if index >= argc:
        return null()
    if ptr_is_null(argv):
        return null()
    arg = load_ptr(argv, index * 8)
    if ptr_is_null(arg):
        return cstr("")
    return arg


@c_abi_export("py_process_exit")
def py_process_exit(code: int) -> None:
    fflush(null())
    platform_exit(code)
