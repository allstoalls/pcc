"""Explicit dynamic-loading boundary for the owned static Linux runtime.

Static compiler execution must not acquire libdl/libc just because the native
extension error path is linked. Shared ELF loading is not implemented here:
every request fails through dlerror, and no external loader is consulted.
This is a capability rejection, not a shared-library loading implementation.
"""

from pcc import i64
from pcc.extern import c_abi_export, c_ptr
from pcc.unsafe import define_thread_local_i32, global_addr, load_i32, store_i32, null, cstr

__pcc_freestanding__ = True

define_thread_local_i32("pcc_static_loader_error", 0)


@c_abi_export("dlopen")
def dlopen(path: c_ptr, flags: i64) -> c_ptr:
    store_i32(global_addr("pcc_static_loader_error"), 0, 1)
    return null()


@c_abi_export("dlsym")
def dlsym(handle: c_ptr, symbol: c_ptr) -> c_ptr:
    store_i32(global_addr("pcc_static_loader_error"), 0, 1)
    return null()


@c_abi_export("dlclose")
def dlclose(handle: c_ptr) -> i64:
    store_i32(global_addr("pcc_static_loader_error"), 0, 1)
    return -1


@c_abi_export("dlerror")
def dlerror() -> c_ptr:
    error: i64 = load_i32(global_addr("pcc_static_loader_error"), 0)
    store_i32(global_addr("pcc_static_loader_error"), 0, 0)
    if error:
        return cstr("pcc static Linux runtime: owned ELF shared-library loading is not implemented")
    return null()
