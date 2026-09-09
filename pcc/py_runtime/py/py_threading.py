"""pcc-Python mirror of py_threading.c.

This module keeps the no-C runtime archive linkable.  The heavy lifting stays
in pcc_threads.c through extern wrappers; Python-level stdlib shims call these
ABI symbols rather than embedding pthread details in layer1.
"""

__pcc_runtime_port__ = True

from pcc.py_runtime.py.py_abi_constants import (
    PY_TYPE_THREAD_CONDITION,
    PY_TYPE_THREAD_EVENT,
    PY_TYPE_THREAD_LOCK,
    PY_TYPE_THREAD_SEMAPHORE,
)

from pcc.extern import c_abi_export, c_int32, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    atomic_cas_i64,
    atomic_load_i64,
    define_global_i64,
    define_global_ptr_null,
    free,
    global_addr,
    global_load_ptr,
    global_store_ptr,
    int_to_ptr,
    load_i64,
    load_ptr,
    malloc,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    ptr_to_int,
    store_i64,
    store_ptr,
)


# Waiter nodes use the same raw layout and bounded pool as the C oracle:
# rooted-vthread@0, next@8, root-handle@16, pool-entry@24 (32 bytes).
# The per-object mutex protects each FIFO; this small independent mutex protects
# the shared freelist across objects/carriers.  Both are machine primitives,
# while allocation, rooting, queue policy and lifecycle remain pcc-Python owned.
define_global_ptr_null("pcc_threading_vthread_waiter_free_py")
define_global_i64("pcc_threading_vthread_waiter_free_count_py", 0)
define_global_i64("pcc_threading_vthread_waiter_mutex_bits_py", 0)

_VTHREAD_WAITER_POOL_LIMIT = 4096

pcc_current_thread_id = extern("pcc_current_thread_id", (), c_int64)
pcc_threads_enabled = extern("pcc_threads_enabled", (), c_int64)
pcc_mutex_new = extern("pcc_mutex_new", (), c_ptr)
pcc_mutex_free = extern("pcc_mutex_free", (c_ptr,), c_void)
pcc_mutex_lock = extern("pcc_mutex_lock", (c_ptr,), c_int64)
pcc_mutex_unlock = extern("pcc_mutex_unlock", (c_ptr,), c_int64)
pcc_cond_new = extern("pcc_cond_new", (), c_ptr)
pcc_cond_free = extern("pcc_cond_free", (c_ptr,), c_void)
pcc_cond_wait = extern("pcc_cond_wait", (c_ptr, c_ptr), c_int64)
pcc_cond_signal = extern("pcc_cond_signal", (c_ptr,), c_int64)
pcc_cond_broadcast = extern("pcc_cond_broadcast", (c_ptr,), c_int64)
py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
py_obj_call = extern("py_obj_call", (c_ptr, c_ptr, c_ptr), c_ptr)
py_incref_extern = extern("py_incref", (c_ptr,), c_void)
py_decref_extern = extern("py_decref", (c_ptr,), c_void)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_store_ptr = extern("pcc_gc_store_ptr", (c_ptr, c_ptr, c_ptr), c_void)
pcc_gc_store_root = extern("pcc_gc_store_root", (c_ptr, c_ptr), c_void)
pcc_gc_scheduler_root_register_handle = extern(
    "pcc_gc_scheduler_root_register_handle", (c_ptr,), c_ptr
)
pcc_gc_scheduler_root_unregister_handle = extern(
    "pcc_gc_scheduler_root_unregister_handle", (c_ptr,), c_void
)
pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_ptr)
pcc_gc_free_object_memory = extern("pcc_gc_free_object_memory", (c_ptr,), c_void)
py_virtual_thread_current = extern("py_virtual_thread_current", (), c_ptr)
py_virtual_thread_park = extern("py_virtual_thread_park", (c_ptr,), c_int64)
py_virtual_thread_unpark = extern("py_virtual_thread_unpark", (c_ptr,), c_int64)
pcc_vthread_effect_note_waiter_root_enter = extern(
    "pcc_vthread_effect_note_waiter_root_enter", (), c_void
)
pcc_vthread_effect_note_waiter_root_leave = extern(
    "pcc_vthread_effect_note_waiter_root_leave", (), c_void
)
pcc_vthread_waiter_pool_note_allocation = extern(
    "pcc_vthread_waiter_pool_note_allocation", (), c_void
)
pcc_vthread_waiter_pool_note_reuse = extern(
    "pcc_vthread_waiter_pool_note_reuse", (), c_void
)
pcc_vthread_waiter_pool_note_cached = extern(
    "pcc_vthread_waiter_pool_note_cached", (c_int64,), c_void
)


def _waiter_pool_mutex():
    slot = global_addr("pcc_threading_vthread_waiter_mutex_bits_py")
    bits = atomic_load_i64(slot, 0, "acquire")
    if bits != 0:
        return int_to_ptr(bits)
    candidate = pcc_mutex_new()
    if ptr_is_null(candidate):
        return null()
    candidate_bits = ptr_to_int(candidate)
    installed = atomic_cas_i64(
        slot, 0, 0, candidate_bits, "acq_rel", "acquire"
    )
    if installed != 0:
        pcc_mutex_free(candidate)
        return int_to_ptr(installed)
    return candidate


def _waiter_clear(node) -> None:
    if ptr_is_null(node):
        return
    # Fresh malloc storage has no old reference to consume. Pooled nodes have
    # already released/unregistered their root in _waiter_pop; enqueue failure
    # recycles a still-empty node. Match the C mirror's raw initialization.
    store_ptr(node, 0, null())
    store_ptr(node, 8, null())
    store_ptr(node, 16, null())
    store_i64(node, 24, 0)


def _waiter_alloc():
    node = null()
    mutex = _waiter_pool_mutex()
    if ptr_is_null(mutex) == 0 and pcc_mutex_lock(mutex) == 0:
        node = global_load_ptr("pcc_threading_vthread_waiter_free_py")
        if ptr_is_null(node) == 0:
            global_store_ptr(
                "pcc_threading_vthread_waiter_free_py", load_ptr(node, 8)
            )
            count_slot = global_addr(
                "pcc_threading_vthread_waiter_free_count_py"
            )
            count = load_i64(count_slot, 0)
            if count > 0:
                count = count - 1
                store_i64(count_slot, 0, count)
            pcc_vthread_waiter_pool_note_reuse()
            pcc_vthread_waiter_pool_note_cached(count)
        pcc_mutex_unlock(mutex)
    if ptr_is_null(node):
        node = malloc(32)
        if ptr_is_null(node) == 0:
            pcc_vthread_waiter_pool_note_allocation()
    _waiter_clear(node)
    return node


def _waiter_recycle(node) -> None:
    if ptr_is_null(node):
        return
    _waiter_clear(node)
    mutex = _waiter_pool_mutex()
    if ptr_is_null(mutex) or pcc_mutex_lock(mutex) != 0:
        free(node)
        return
    count_slot = global_addr("pcc_threading_vthread_waiter_free_count_py")
    count = load_i64(count_slot, 0)
    if count >= _VTHREAD_WAITER_POOL_LIMIT:
        pcc_mutex_unlock(mutex)
        free(node)
        return
    store_i64(node, 24, 1)
    store_ptr(
        node,
        8,
        global_load_ptr("pcc_threading_vthread_waiter_free_py"),
    )
    global_store_ptr("pcc_threading_vthread_waiter_free_py", node)
    count = count + 1
    store_i64(count_slot, 0, count)
    pcc_vthread_waiter_pool_note_cached(count)
    pcc_mutex_unlock(mutex)


def _waiter_enqueue(owner, head_offset: int, tail_offset: int, vthread) -> int:
    if ptr_is_null(vthread) or ptr_eq(vthread, global_load_ptr("py_None")):
        return -1
    node = _waiter_alloc()
    if ptr_is_null(node):
        return -1
    handle = pcc_gc_scheduler_root_register_handle(node)
    if ptr_is_null(handle):
        _waiter_recycle(node)
        return -1
    store_ptr(node, 16, handle)
    pcc_vthread_effect_note_waiter_root_enter()
    pcc_gc_store_root(node, vthread)
    tail = load_ptr(owner, tail_offset)
    if ptr_is_null(tail):
        store_ptr(owner, head_offset, node)
    else:
        store_ptr(tail, 8, node)
    store_ptr(owner, tail_offset, node)
    return 0


def _waiter_pop(owner, head_offset: int, tail_offset: int):
    while ptr_is_null(load_ptr(owner, head_offset)) == 0:
        node = load_ptr(owner, head_offset)
        after = load_ptr(node, 8)
        store_ptr(owner, head_offset, after)
        if ptr_is_null(after):
            store_ptr(owner, tail_offset, null())
        vthread = pcc_gc_load_ptr(null(), node)
        py_incref_extern(vthread)
        handle = load_ptr(node, 16)
        pcc_gc_scheduler_root_unregister_handle(handle)
        pcc_vthread_effect_note_waiter_root_leave()
        store_ptr(node, 16, null())
        pcc_gc_store_root(node, null())
        _waiter_recycle(node)
        if ptr_is_null(vthread) == 0:
            return vthread
        py_decref_extern(vthread)
    return null()


def _waiter_wake_one(owner, head_offset: int, tail_offset: int) -> int:
    vthread = _waiter_pop(owner, head_offset, tail_offset)
    if ptr_is_null(vthread):
        return 0
    result = py_virtual_thread_unpark(vthread)
    py_decref_extern(vthread)
    return 1 if result == 0 else -1


def _waiter_wake_all(owner, head_offset: int, tail_offset: int) -> int:
    count = 0
    while True:
        vthread = _waiter_pop(owner, head_offset, tail_offset)
        if ptr_is_null(vthread):
            return count
        if py_virtual_thread_unpark(vthread) == 0:
            count = count + 1
        py_decref_extern(vthread)


def _waiters_clear(owner, head_offset: int, tail_offset: int) -> None:
    while True:
        vthread = _waiter_pop(owner, head_offset, tail_offset)
        if ptr_is_null(vthread):
            return
        py_decref_extern(vthread)


def _current_vthread():
    vthread = py_virtual_thread_current()
    if ptr_is_null(vthread) or ptr_eq(
        vthread, global_load_ptr("py_None")
    ):
        py_decref_extern(vthread)
        return null()
    return vthread


def _alloc_obj(type_tag: int, size: int):
    return pcc_gc_alloc(size, type_tag, 0)


@c_abi_export("py_threading_get_ident")
def py_threading_get_ident() -> int:
    return pcc_current_thread_id()


@c_abi_export("py_threading_current_thread")
def py_threading_current_thread():
    return py_int_from_i64(pcc_current_thread_id())


@c_abi_export("py_threading_lock_new")
def py_threading_lock_new():
    # header, mutex, cond, held, waiter-head, waiter-tail
    o = _alloc_obj(PY_TYPE_THREAD_LOCK, 56)
    if ptr_is_null(o):
        return o
    m = pcc_mutex_new()
    c = pcc_cond_new()
    if ptr_is_null(m) or ptr_is_null(c):
        pcc_cond_free(c)
        pcc_mutex_free(m)
        pcc_gc_free_object_memory(o)
        return null()
    store_ptr(o, 16, m)
    store_ptr(o, 24, c)
    store_i64(o, 32, 0)
    store_ptr(o, 40, null())
    store_ptr(o, 48, null())
    return o


@c_abi_export("py_threading_lock_acquire")
def py_threading_lock_acquire(lock) -> int:
    if ptr_is_null(lock):
        return -1
    m = load_ptr(lock, 16)
    c = load_ptr(lock, 24)
    if pcc_mutex_lock(m) != 0:
        return -1
    result = 0
    while load_i64(lock, 32) != 0:
        if pcc_cond_wait(c, m) != 0:
            result = -1
            break
    if result == 0:
        store_i64(lock, 32, 1)
    if pcc_mutex_unlock(m) != 0 and result == 0:
        result = -1
    return result


@c_abi_export("py_threading_lock_acquire_vthread")
def py_threading_lock_acquire_vthread(lock) -> int:
    if ptr_is_null(lock):
        return -1
    vthread = _current_vthread()
    if ptr_is_null(vthread):
        return py_threading_lock_acquire(lock)
    m = load_ptr(lock, 16)
    if pcc_mutex_lock(m) != 0:
        py_decref_extern(vthread)
        return -1
    if load_i64(lock, 32) == 0:
        store_i64(lock, 32, 1)
        pcc_mutex_unlock(m)
        py_decref_extern(vthread)
        return 0
    if _waiter_enqueue(lock, 40, 48, vthread) != 0:
        pcc_mutex_unlock(m)
        py_decref_extern(vthread)
        return -1
    result = py_virtual_thread_park(vthread)
    pcc_mutex_unlock(m)
    py_decref_extern(vthread)
    return 1 if result == 0 else -1


@c_abi_export("py_threading_lock_release")
def py_threading_lock_release(lock) -> int:
    if ptr_is_null(lock):
        return -1
    m = load_ptr(lock, 16)
    if pcc_mutex_lock(m) != 0:
        return -1
    if load_i64(lock, 32) == 0:
        pcc_mutex_unlock(m)
        return -1
    woken = _waiter_wake_one(lock, 40, 48)
    if woken < 0:
        pcc_mutex_unlock(m)
        return -1
    if woken == 0:
        store_i64(lock, 32, 0)
        pcc_cond_signal(load_ptr(lock, 24))
    else:
        # Ownership transfers directly to the oldest parked vthread.
        store_i64(lock, 32, 1)
    return pcc_mutex_unlock(m)


@c_abi_export("py_dealloc_thread_lock")
def py_dealloc_thread_lock(lock) -> None:
    if ptr_is_null(lock):
        return
    _waiters_clear(lock, 40, 48)
    pcc_cond_free(load_ptr(lock, 24))
    pcc_mutex_free(load_ptr(lock, 16))
    pcc_gc_free_object_memory(lock)


@c_abi_export("py_threading_rlock_new")
def py_threading_rlock_new():
    return py_threading_lock_new()


@c_abi_export("py_threading_rlock_acquire")
def py_threading_rlock_acquire(lock) -> int:
    return py_threading_lock_acquire(lock)


@c_abi_export("py_threading_rlock_release")
def py_threading_rlock_release(lock) -> int:
    return py_threading_lock_release(lock)


@c_abi_export("py_dealloc_thread_rlock")
def py_dealloc_thread_rlock(lock) -> None:
    py_dealloc_thread_lock(lock)


@c_abi_export("py_threading_event_new")
def py_threading_event_new():
    # header, mutex, cond, flag, waiter-head, waiter-tail
    o = _alloc_obj(PY_TYPE_THREAD_EVENT, 56)
    if ptr_is_null(o):
        return o
    m = pcc_mutex_new()
    c = pcc_cond_new()
    if ptr_is_null(m) or ptr_is_null(c):
        pcc_mutex_free(m)
        pcc_cond_free(c)
        pcc_gc_free_object_memory(o)
        return null()
    store_ptr(o, 16, m)
    store_ptr(o, 24, c)
    store_i64(o, 32, 0)
    store_ptr(o, 40, null())
    store_ptr(o, 48, null())
    return o


@c_abi_export("py_threading_event_set")
def py_threading_event_set(event) -> int:
    if ptr_is_null(event):
        return -1
    m = load_ptr(event, 16)
    c = load_ptr(event, 24)
    if pcc_mutex_lock(m) != 0:
        return -1
    store_i64(event, 32, 1)
    _waiter_wake_all(event, 40, 48)
    pcc_cond_broadcast(c)
    return pcc_mutex_unlock(m)


@c_abi_export("py_threading_event_clear")
def py_threading_event_clear(event) -> int:
    if ptr_is_null(event):
        return -1
    m = load_ptr(event, 16)
    if pcc_mutex_lock(m) != 0:
        return -1
    store_i64(event, 32, 0)
    return pcc_mutex_unlock(m)


@c_abi_export("py_threading_event_is_set")
def py_threading_event_is_set(event) -> int:
    if ptr_is_null(event):
        return 0
    return 1 if load_i64(event, 32) != 0 else 0


@c_abi_export("py_threading_event_wait")
def py_threading_event_wait(event) -> int:
    if ptr_is_null(event):
        return -1
    m = load_ptr(event, 16)
    c = load_ptr(event, 24)
    if pcc_mutex_lock(m) != 0:
        return -1
    while load_i64(event, 32) == 0:
        if pcc_cond_wait(c, m) != 0:
            break
    return pcc_mutex_unlock(m)


@c_abi_export("py_threading_event_wait_vthread")
def py_threading_event_wait_vthread(event) -> int:
    if ptr_is_null(event):
        return -1
    vthread = _current_vthread()
    if ptr_is_null(vthread):
        return py_threading_event_wait(event)
    m = load_ptr(event, 16)
    if pcc_mutex_lock(m) != 0:
        py_decref_extern(vthread)
        return -1
    if load_i64(event, 32) != 0:
        pcc_mutex_unlock(m)
        py_decref_extern(vthread)
        return 0
    if _waiter_enqueue(event, 40, 48, vthread) != 0:
        pcc_mutex_unlock(m)
        py_decref_extern(vthread)
        return -1
    result = py_virtual_thread_park(vthread)
    pcc_mutex_unlock(m)
    py_decref_extern(vthread)
    return 1 if result == 0 else -1


@c_abi_export("py_dealloc_thread_event")
def py_dealloc_thread_event(event) -> None:
    if ptr_is_null(event):
        return
    _waiters_clear(event, 40, 48)
    pcc_cond_free(load_ptr(event, 24))
    pcc_mutex_free(load_ptr(event, 16))
    pcc_gc_free_object_memory(event)


@c_abi_export("py_threading_condition_new")
def py_threading_condition_new(lock):
    # Condition owns its mutex in the current native threading ABI.  The lock
    # argument is accepted for Python surface parity and intentionally ignored,
    # matching the C oracle until shared-lock Conditions are implemented.
    o = _alloc_obj(PY_TYPE_THREAD_CONDITION, 48)
    if ptr_is_null(o):
        return o
    m = pcc_mutex_new()
    c = pcc_cond_new()
    if ptr_is_null(m) or ptr_is_null(c):
        pcc_mutex_free(m)
        pcc_cond_free(c)
        pcc_gc_free_object_memory(o)
        return null()
    store_ptr(o, 16, m)
    store_ptr(o, 24, c)
    store_ptr(o, 32, null())
    store_ptr(o, 40, null())
    return o


@c_abi_export("py_threading_condition_acquire")
def py_threading_condition_acquire(cond) -> int:
    if ptr_is_null(cond):
        return -1
    return pcc_mutex_lock(load_ptr(cond, 16))


@c_abi_export("py_threading_condition_release")
def py_threading_condition_release(cond) -> int:
    if ptr_is_null(cond):
        return -1
    return pcc_mutex_unlock(load_ptr(cond, 16))


@c_abi_export("py_threading_condition_wait")
def py_threading_condition_wait(cond) -> int:
    if ptr_is_null(cond):
        return -1
    return pcc_cond_wait(load_ptr(cond, 24), load_ptr(cond, 16))


@c_abi_export("py_threading_condition_wait_vthread")
def py_threading_condition_wait_vthread(cond) -> int:
    if ptr_is_null(cond):
        return -1
    vthread = _current_vthread()
    if ptr_is_null(vthread):
        return py_threading_condition_wait(cond)
    if _waiter_enqueue(cond, 32, 40, vthread) != 0:
        py_decref_extern(vthread)
        return -1
    result = py_virtual_thread_park(vthread)
    if pcc_mutex_unlock(load_ptr(cond, 16)) != 0 and result == 0:
        result = -1
    py_decref_extern(vthread)
    return 1 if result == 0 else -1


@c_abi_export("py_threading_condition_notify")
def py_threading_condition_notify(cond) -> int:
    if ptr_is_null(cond):
        return -1
    woken = _waiter_wake_one(cond, 32, 40)
    if woken < 0:
        return -1
    return pcc_cond_signal(load_ptr(cond, 24))


@c_abi_export("py_dealloc_thread_condition")
def py_dealloc_thread_condition(cond) -> None:
    if ptr_is_null(cond):
        return
    _waiters_clear(cond, 32, 40)
    pcc_cond_free(load_ptr(cond, 24))
    pcc_mutex_free(load_ptr(cond, 16))
    pcc_gc_free_object_memory(cond)


@c_abi_export("py_threading_semaphore_new")
def py_threading_semaphore_new(initial: int):
    if initial < 0:
        initial = 0
    o = _alloc_obj(PY_TYPE_THREAD_SEMAPHORE, 56)
    if ptr_is_null(o):
        return o
    m = pcc_mutex_new()
    c = pcc_cond_new()
    if ptr_is_null(m) or ptr_is_null(c):
        pcc_mutex_free(m)
        pcc_cond_free(c)
        pcc_gc_free_object_memory(o)
        return null()
    store_ptr(o, 16, m)
    store_ptr(o, 24, c)
    store_i64(o, 32, initial)
    store_ptr(o, 40, null())
    store_ptr(o, 48, null())
    return o


@c_abi_export("py_threading_semaphore_acquire")
def py_threading_semaphore_acquire(sem) -> int:
    if ptr_is_null(sem):
        return -1
    m = load_ptr(sem, 16)
    c = load_ptr(sem, 24)
    if pcc_mutex_lock(m) != 0:
        return -1
    while load_i64(sem, 32) <= 0:
        if pcc_cond_wait(c, m) != 0:
            break
    v: int = load_i64(sem, 32)
    if v > 0:
        store_i64(sem, 32, v - 1)
    return pcc_mutex_unlock(m)


@c_abi_export("py_threading_semaphore_acquire_vthread")
def py_threading_semaphore_acquire_vthread(sem) -> int:
    if ptr_is_null(sem):
        return -1
    vthread = _current_vthread()
    if ptr_is_null(vthread):
        return py_threading_semaphore_acquire(sem)
    m = load_ptr(sem, 16)
    if pcc_mutex_lock(m) != 0:
        py_decref_extern(vthread)
        return -1
    value = load_i64(sem, 32)
    if value > 0:
        store_i64(sem, 32, value - 1)
        pcc_mutex_unlock(m)
        py_decref_extern(vthread)
        return 0
    if _waiter_enqueue(sem, 40, 48, vthread) != 0:
        pcc_mutex_unlock(m)
        py_decref_extern(vthread)
        return -1
    result = py_virtual_thread_park(vthread)
    pcc_mutex_unlock(m)
    py_decref_extern(vthread)
    return 1 if result == 0 else -1


@c_abi_export("py_threading_semaphore_release")
def py_threading_semaphore_release(sem) -> int:
    if ptr_is_null(sem):
        return -1
    m = load_ptr(sem, 16)
    c = load_ptr(sem, 24)
    if pcc_mutex_lock(m) != 0:
        return -1
    woken = _waiter_wake_one(sem, 40, 48)
    if woken < 0:
        pcc_mutex_unlock(m)
        return -1
    if woken == 0:
        store_i64(sem, 32, load_i64(sem, 32) + 1)
    pcc_cond_signal(c)
    return pcc_mutex_unlock(m)


@c_abi_export("py_dealloc_thread_semaphore")
def py_dealloc_thread_semaphore(sem) -> None:
    if ptr_is_null(sem):
        return
    _waiters_clear(sem, 40, 48)
    pcc_cond_free(load_ptr(sem, 24))
    pcc_mutex_free(load_ptr(sem, 16))
    pcc_gc_free_object_memory(sem)


@c_abi_export("py_threading_thread_new")
def py_threading_thread_new(callable, args):
    # Layout mirrors the C PyThreadObject enough for the no-C archive:
    # header, handle, callable, args, result, started, joined, finished.
    o = _alloc_obj(27, 72)
    if ptr_is_null(o):
        return o
    store_ptr(o, 16, null())
    store_ptr(o, 24, null())
    store_ptr(o, 32, null())
    store_ptr(o, 40, null())
    pcc_gc_store_ptr(o, ptr_add(o, 24), callable)
    pcc_gc_store_ptr(o, ptr_add(o, 32), args)
    store_i64(o, 48, 0)
    store_i64(o, 56, 0)
    store_i64(o, 64, 0)
    return o


@c_abi_export("py_threading_thread_start")
def py_threading_thread_start(thread) -> int:
    if ptr_is_null(thread):
        return -1
    # No real pthread dispatch in the pcc-Python archive; run the target
    # synchronously so simple Thread programs work in libpy_runtime_pcc_py.a.
    store_i64(thread, 48, 1)
    callable_obj = pcc_gc_load_ptr(thread, ptr_add(thread, 24))
    args_obj = pcc_gc_load_ptr(thread, ptr_add(thread, 32))
    if ptr_is_null(callable_obj) == 0:
        result = py_obj_call(callable_obj, args_obj, null())
        pcc_gc_store_ptr(thread, ptr_add(thread, 40), result)
        py_decref_extern(result)
    store_i64(thread, 56, 1)
    store_i64(thread, 64, 1)
    return 0


@c_abi_export("py_threading_thread_join")
def py_threading_thread_join(thread) -> int:
    return 0 if ptr_is_null(thread) == 0 else -1


@c_abi_export("py_threading_thread_is_alive")
def py_threading_thread_is_alive(thread) -> int:
    if ptr_is_null(thread):
        return 0
    return 1 if load_i64(thread, 48) != 0 and load_i64(thread, 56) == 0 and load_i64(thread, 64) == 0 else 0


@c_abi_export("py_dealloc_thread_thread")
def py_dealloc_thread_thread(thread) -> None:
    if ptr_is_null(thread):
        return
    py_decref_extern(pcc_gc_load_ptr(thread, ptr_add(thread, 24)))  # callable
    py_decref_extern(pcc_gc_load_ptr(thread, ptr_add(thread, 32)))  # args
    py_decref_extern(pcc_gc_load_ptr(thread, ptr_add(thread, 40)))  # result
    pcc_gc_free_object_memory(thread)
