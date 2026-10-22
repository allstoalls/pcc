"""Linux x86_64 process entry authored in freestanding pcc-Python.

The self backend supplies the kernel's original stack pointer as the sole
argument.  This source decodes ``argc``/``argv[0]`` directly and terminates via
raw Linux syscalls; it does not use a C/assembly startup object or libc.
"""

from pcc import i64
from pcc.extern import c_abi_export, c_ptr
from pcc.unsafe import cstr, load_i64, load_ptr, process_exit, ptr_is_null, write

__pcc_freestanding__ = True

@c_abi_export("_start")
def pcc_linux_start(initial_stack: c_ptr) -> None:
    argc: i64 = load_i64(initial_stack, 0)
    argv0 = load_ptr(initial_stack, 8)
    status: i64 = 0
    if argc < 1 or ptr_is_null(argv0):
        status: i64 = 64

    message = cstr("pcc zero-libc ok\n")
    if write(1, message, 17) != 17:
        status: i64 = 74
    process_exit(status)
