"""Owned Python recursion accounting for active native function calls.

The shared limit is enforced against thread-local active depth. Frontends must
pair each successful enter with exactly one leave, including exceptional exits.
Suspended generators do not consume active depth; resume activations do.
Runtime substrate functions must not recursively enter this guard themselves.
"""

__pcc_runtime_port__ = True

from pcc.extern import c_abi_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    atomic_load_i32,
    atomic_store_i32,
    cstr,
    define_global_i32,
    define_thread_local_i32,
    global_addr,
    load_i32,
    store_i32,
)


py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)

define_global_i32("pcc_py_recursion_limit", 1000)
define_thread_local_i32("pcc_py_recursion_depth", 0)


@c_abi_export("py_sys_getrecursionlimit")
def py_sys_getrecursionlimit() -> int:
    return atomic_load_i32(global_addr("pcc_py_recursion_limit"), 0, "acquire")


@c_abi_export("py_recursion_depth")
def py_recursion_depth() -> int:
    return load_i32(global_addr("pcc_py_recursion_depth"), 0)


@c_abi_export("py_recursion_enter")
def py_recursion_enter() -> int:
    depth_address = global_addr("pcc_py_recursion_depth")
    depth: int = load_i32(depth_address, 0)
    limit: int = atomic_load_i32(global_addr("pcc_py_recursion_limit"), 0, "acquire")
    if depth >= limit:
        # 56 is the owned exception ABI's RecursionError code.
        py_raise_owned(py_exc_new(56, cstr("maximum recursion depth exceeded")))
        return 0
    store_i32(depth_address, 0, depth + 1)
    return 1


@c_abi_export("py_recursion_leave")
def py_recursion_leave(entered: int) -> None:
    if entered != 1:
        return
    depth_address = global_addr("pcc_py_recursion_depth")
    depth: int = load_i32(depth_address, 0)
    if depth <= 0:
        py_raise_owned(py_exc_new(7, cstr("pcc recursion: leave without an active entry")))
        return
    store_i32(depth_address, 0, depth - 1)


@c_abi_export("py_sys_setrecursionlimit")
def py_sys_setrecursionlimit(limit: int) -> int:
    """Raw ABI setter; public Python callers also need index conversion."""
    if limit > 2147483647 or limit < -2147483648:
        py_raise_owned(py_exc_new(15, cstr("Python int too large to convert to C int")))
        return 0
    if limit <= 0:
        py_raise_owned(py_exc_new(2, cstr("recursion limit must be greater or equal than 1")))
        return 0
    depth: int = load_i32(global_addr("pcc_py_recursion_depth"), 0)
    if limit <= depth:
        py_raise_owned(py_exc_new(56, cstr("cannot set the recursion limit below the current recursion depth")))
        return 0
    atomic_store_i32(global_addr("pcc_py_recursion_limit"), 0, limit, "release")
    return 1
