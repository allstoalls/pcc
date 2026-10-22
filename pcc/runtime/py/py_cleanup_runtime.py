"""Small synchronous cleanup programs with an authoritative exception owner.

Primitive runtime lowering and the no-implicit-poll build rule keep this
error-edge protocol synchronous. Disposal remains an ordinary
safepoint-capable call and may execute reentrant finalizers or weakref callbacks.
"""

from pcc import i64
from pcc.extern import c_abi_typed_export, c_int64, c_ptr, c_void, extern
from pcc.runtime.py.py_abi_constants import C_POINTER_SIZE
from pcc.unsafe import (
    define_global_i32,
    global_addr,
    null,
    stack_alloc,
    store_ptr,
)

__pcc_runtime_port__ = True

pcc_gc_frame_enter_lifo = extern("pcc_gc_frame_enter_lifo", (c_ptr, c_ptr), c_void)
pcc_gc_frame_leave_lifo = extern("pcc_gc_frame_leave_lifo", (c_ptr,), c_void)
pcc_gc_store_root = extern("pcc_gc_store_root", (c_ptr, c_ptr), c_void)
pcc_gc_foreign_lease_release = extern("pcc_gc_foreign_lease_release", (c_ptr, c_int64), c_int64)
py_tls_exc_swap_slot = extern("py_tls_exc_swap_slot", (c_ptr,), c_void)
py_clear_exception = extern("py_clear_exception", (), c_void)

define_global_i32("pcc_cleanup_exception_map", 1)


@c_abi_typed_export("py_cleanup_one_root_preserving_exception", "void", ("ptr",))
def py_cleanup_one_root_preserving_exception(slot: c_ptr) -> None:
    error = stack_alloc(C_POINTER_SIZE)
    store_ptr(error, 0, null())
    pcc_gc_frame_enter_lifo(global_addr("pcc_cleanup_exception_map"), error)
    py_tls_exc_swap_slot(error)
    # Clear before decref; keep the caller's operand frame registered.
    pcc_gc_store_root(slot, null())
    py_clear_exception()
    py_tls_exc_swap_slot(error)
    # The original owner is back in TLS, so this frame is empty.
    pcc_gc_frame_leave_lifo(error)


@c_abi_typed_export("py_cleanup_two_roots_preserving_exception", "void", ("ptr", "ptr"))
def py_cleanup_two_roots_preserving_exception(first: c_ptr, second: c_ptr) -> None:
    error = stack_alloc(C_POINTER_SIZE)
    store_ptr(error, 0, null())
    pcc_gc_frame_enter_lifo(global_addr("pcc_cleanup_exception_map"), error)
    py_tls_exc_swap_slot(error)
    # Keep both caller frames registered and read each owner from its slot
    # only when clearing it: the second owner's callback can move the first.
    # Preserve intermediate callback errors until both releases have finished.
    pcc_gc_store_root(second, null())
    pcc_gc_store_root(first, null())
    py_clear_exception()
    py_tls_exc_swap_slot(error)
    pcc_gc_frame_leave_lifo(error)


@c_abi_typed_export("py_cleanup_one_lease_preserving_exception", "void", ("ptr", "i64"))
def py_cleanup_one_lease_preserving_exception(slot: c_ptr, acquired: i64) -> None:
    error = stack_alloc(C_POINTER_SIZE)
    store_ptr(error, 0, null())
    pcc_gc_frame_enter_lifo(global_addr("pcc_cleanup_exception_map"), error)
    py_tls_exc_swap_slot(error)
    # Match the existing cleanup edge: preserve the token and ignore status.
    # The caller still owns its slot and frame after this lease is released.
    pcc_gc_foreign_lease_release(slot, acquired)
    py_clear_exception()
    py_tls_exc_swap_slot(error)
    pcc_gc_frame_leave_lifo(error)
