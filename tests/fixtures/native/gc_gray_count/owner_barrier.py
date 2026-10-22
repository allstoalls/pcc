"""GC1/2 native counter regression: ordinary raw-scaffold diagnostic source.
Use native pcc1 -o, explicit candidate runtime (always threaded). Each shape
runs in a fresh process. No collector threshold or gray counter is overwritten.
All allocation precedes the state window. A raw status write/exit avoids any
managed allocation after a failing counter check. 000/exit0 means pass.
"""
from pcc.extern import extern, c_int64, c_ptr, c_rawptr, c_void
from pcc.unsafe import global_addr, load_i32, load_i64, ptr_is_null, store_i32, stack_alloc, null, store_ptr, store_i8
pcc_gc_backend = extern("pcc_gc_backend", (), c_int64)
pcc_threads_enabled = extern("pcc_threads_enabled", (), c_int64)
py_list_new = extern("py_list_new", (c_int64,), c_rawptr)
py_decref = extern("py_decref", (c_ptr,), c_void)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
pcc_gc_unpin = extern("pcc_gc_unpin", (c_ptr,), c_void)
pcc_gc_mark_root_gray_if_known = extern("pcc_gc_mark_root_gray_if_known", (c_ptr,), c_void)
pcc_gc_object_is_known_no_lock = extern("pcc_gc_object_is_known_no_lock", (c_ptr,), c_int64)
pcc_gc_gray_count_load_acquire = extern("pcc_gc_gray_count_load_acquire", (), c_int64)
pcc_gc_note_slot_write_barrier = extern("pcc_gc_note_slot_write_barrier", (c_ptr, c_ptr, c_ptr), c_void)
pcc_py_gc_minor_graph_lock = extern("pcc_py_gc_minor_graph_lock", (), c_void)
pcc_py_gc_minor_graph_unlock = extern("pcc_py_gc_minor_graph_unlock", (), c_void)
pcc_platform_write = extern("pcc_platform_write", (c_int64, c_rawptr, c_int64), c_int64)
pcc_platform_exit = extern("pcc_platform_exit", (c_int64,), c_void)


def probe() -> int:
    backend: int = int(pcc_gc_backend())
    if backend != 1 and backend != 2:
        return 100
    if pcc_threads_enabled() != 0:
        return 101
    owner = py_list_new(0)
    if ptr_is_null(owner) != 0:
        return 102
    # Protect the first object while the second constructor may assist GC2.
    pcc_gc_pin(owner)
    obj = py_list_new(0)
    if ptr_is_null(obj) != 0:
        return 102
    pcc_gc_unpin(owner)
    auto_cell = global_addr("pcc_gc_in_auto_step")
    active_cell = global_addr("pcc_gc_mark_active")
    saved_auto: int = int(load_i32(auto_cell, 0))
    saved_active: int = int(load_i32(active_cell, 0))
    # No allocation from this point until process exit. GC2 does not generally
    # treat in_auto_step as suppression; the absence of allocations is essential.
    store_i32(auto_cell, 0, 1)
    slot = stack_alloc(8)
    store_ptr(slot, 0, null())
    pcc_py_gc_minor_graph_lock()
    flags: int = int(load_i32(obj, 12))
    valid: int = int(pcc_gc_object_is_known_no_lock(obj))
    if valid != 1 or (flags & 665617) != 0 or load_i64(obj, 0) != 1:
        pcc_py_gc_minor_graph_unlock()
        store_i32(active_cell, 0, saved_active)
        store_i32(auto_cell, 0, saved_auto)
        return 103
    owner_flags: int = int(load_i32(owner, 12))
    if pcc_gc_object_is_known_no_lock(owner) != 1 or (owner_flags & 665617) != 0 or load_i64(owner, 0) != 1:
        pcc_py_gc_minor_graph_unlock()
        store_i32(active_cell, 0, saved_active)
        store_i32(auto_cell, 0, saved_auto)
        return 103
    store_i32(owner, 12, (owner_flags & ~56) | 32)
    # Change only non-gray color bits to establish the tested white input.
    store_i32(obj, 12, (flags & ~56) | 8)
    count_before: int = int(pcc_gc_gray_count_load_acquire())
    status: int = 0
    store_i32(active_cell, 0, 1)
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_note_slot_write_barrier(owner, slot, obj)
    pcc_py_gc_minor_graph_lock()
    if pcc_gc_gray_count_load_acquire() != count_before + 1:
        status = 10
    elif (load_i32(obj, 12) & 16) == 0:
        status = 14
    pcc_py_gc_minor_graph_unlock()
    pcc_gc_note_slot_write_barrier(owner, slot, obj)
    pcc_py_gc_minor_graph_lock()
    if status == 0 and pcc_gc_gray_count_load_acquire() != count_before + 1:
        status = 11
    pcc_py_gc_minor_graph_unlock()
    py_decref(obj)
    py_decref(owner)
    pcc_py_gc_minor_graph_lock()
    if status == 0 and pcc_gc_gray_count_load_acquire() != count_before:
        status = 12
    if status == 0 and pcc_gc_object_is_known_no_lock(obj) != 0:
        status = 13
    pcc_py_gc_minor_graph_unlock()
    store_i32(active_cell, 0, saved_active)
    store_i32(auto_cell, 0, saved_auto)
    return status


def main() -> int:
    status: int = probe()
    # Function-local explicit raw int address avoids module-global boxing.
    # c_rawptr also decodes a dynamic boxed address; c_ptr preserves PyObject*.
    output: int = stack_alloc(4)
    store_i8(output, 0, 48 + status // 100)
    store_i8(output, 1, 48 + (status // 10) % 10)
    store_i8(output, 2, 48 + status % 10)
    store_i8(output, 3, 10)
    pcc_platform_write(1, output, 4)
    pcc_platform_exit(status)
    return status


if __name__ == "__main__":
    main()
