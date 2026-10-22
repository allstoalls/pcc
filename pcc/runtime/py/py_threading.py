"""Owned Python threading objects over the platform thread/kernel ABI.

Rooting, waiter queues, notification and timeout policy live in this module.
The kernel ABI supplies mutexes, condition variables and safepoints.
"""

__pcc_runtime_port__ = True

from pcc.runtime.py.py_abi_constants import PY_TYPE_THREAD, PY_TYPE_THREAD_CONDITION, PY_TYPE_THREAD_EVENT, PY_TYPE_THREAD_LOCK, PY_TYPE_THREAD_RLOCK, PY_TYPE_THREAD_SEMAPHORE, PYOBJECTHEADER_TYPE_TAG_OFFSET, PY_TYPE_BOOL, PY_TYPE_FLOAT, PY_TYPE_INT, PYOBJECTHEADER_FLAGS_OFFSET, PY_FLAG_GC_PINNED, PYVIRTUALTHREADOBJECT_IDENTITY_OFFSET

from pcc.extern import c_abi_export, c_int32, c_int64, c_ptr, c_void, c_double, extern
from pcc.unsafe import (
    atomic_cas_i64,
    atomic_rmw_i32,
    atomic_load_i64,
    atomic_store_i64,
    cstr,
    define_global_i64,
    define_global_i32,
    stack_alloc,
    memset,
    define_global_ptr_null,
    free,
    function_addr,
    global_addr,
    global_load_ptr,
    global_store_ptr,
    int_to_ptr,
    is_tagged_int,
    load_f64,
    load_i32,
    load_i64,
    load_ptr,
    malloc,
    null,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    ptr_to_int,
    store_f64,
    store_i64,
    store_ptr,
    untag_int,
)


# Waiter nodes reuse the existing raw layout and bounded pool:
# rooted-object@0, next@8, root-handle@16, state@24 (32 bytes).
# State 0 is an untimed vthread, 1 a pooled node, -1/-2 a native pending/
# notified waiter. Timed states encode a deadline in us: +deadline+2 for a
# vthread, -deadline-3 for a native waiter.
# The per-object mutex protects each FIFO; this small independent mutex protects
# the shared freelist across objects/carriers.  Both are machine primitives,
# while allocation, rooting, queue policy and lifecycle remain pcc-Python owned.
define_global_ptr_null("pcc_threading_vthread_waiter_free_py")
define_global_i64("pcc_threading_vthread_waiter_free_count_py", 0)
define_global_i64("pcc_threading_vthread_waiter_mutex_bits_py", 0)

define_global_i32("pcc_threading_invoke_frame_map", 4)
define_global_i32("pcc_threading_condition_borrowed_frame_map", -3)
pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
pcc_gc_frame_leave = extern("pcc_gc_frame_leave", (c_ptr,), c_void)
pcc_gc_note_write_barrier = extern("pcc_gc_note_write_barrier", (c_ptr, c_ptr), c_void)

_VTHREAD_WAITER_POOL_LIMIT = 4096

pcc_current_thread_id = extern("pcc_current_thread_id", (), c_int64)
pcc_threads_enabled = extern("pcc_threads_enabled", (), c_int64)
pcc_thread_start = extern("pcc_thread_start", (c_ptr, c_ptr, c_ptr), c_int64)
pcc_thread_join = extern("pcc_thread_join", (c_ptr, c_ptr), c_int64)
pcc_thread_detach = extern("pcc_thread_detach", (c_ptr,), c_void)
pcc_mutex_new = extern("pcc_mutex_new", (), c_ptr)
pcc_mutex_free = extern("pcc_mutex_free", (c_ptr,), c_void)
pcc_mutex_lock = extern("pcc_mutex_lock", (c_ptr,), c_int64)
pcc_mutex_unlock = extern("pcc_mutex_unlock", (c_ptr,), c_int64)
pcc_cond_new = extern("pcc_cond_new", (), c_ptr)
pcc_cond_free = extern("pcc_cond_free", (c_ptr,), c_void)
pcc_cond_wait = extern("pcc_cond_wait", (c_ptr, c_ptr), c_int64)
pcc_cond_timedwait_ms = extern(
    "pcc_cond_timedwait_ms", (c_ptr, c_ptr, c_int64), c_int64
)
pcc_platform_sleep_ns = extern("pcc_platform_sleep_ns", (c_int64,), c_int64)
pcc_thread_safepoint = extern("pcc_thread_safepoint", (), c_void)
pcc_platform_abort = extern("pcc_platform_abort", (), c_void)
pcc_cond_signal = extern("pcc_cond_signal", (c_ptr,), c_int64)
pcc_cond_broadcast = extern("pcc_cond_broadcast", (c_ptr,), c_int64)
py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
py_obj_call = extern("py_obj_call", (c_ptr, c_ptr, c_ptr), c_ptr)
py_obj_call_sync = extern("py_obj_call_sync", (c_ptr, c_ptr, c_ptr), c_ptr)
py_incref_extern = extern("py_incref", (c_ptr,), c_void)
py_decref_extern = extern("py_decref", (c_ptr,), c_void)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
pcc_gc_unpin = extern("pcc_gc_unpin", (c_ptr,), c_void)
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
py_virtual_thread_pin_enter = extern("py_virtual_thread_pin_enter", (c_ptr, c_ptr), c_int64)
py_virtual_thread_pin_leave = extern("py_virtual_thread_pin_leave", (c_ptr,), c_int64)
py_virtual_thread_park = extern("py_virtual_thread_park", (c_ptr,), c_int64)
py_virtual_thread_unpark = extern("py_virtual_thread_unpark", (c_ptr,), c_int64)
py_virtual_thread_sleep = extern("py_virtual_thread_sleep", (c_ptr, c_int64), c_int64)
py_virtual_thread_cancel_timer = extern("py_virtual_thread_cancel_timer", (c_ptr,), c_int64)
py_virtual_thread_current_identity = extern(
    "py_virtual_thread_current_identity", (), c_int64
)
pcc_runtime_monotonic_us = extern("pcc_runtime_monotonic_us", (), c_int64)
py_float_to_f64 = extern("py_float_to_f64", (c_ptr,), c_double)
py_err_occurred = extern("py_err_occurred", (), c_int64)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
py_obj_truthy = extern("py_obj_truthy", (c_ptr,), c_int64)
py_obj_gt = extern("py_obj_gt", (c_ptr, c_ptr), c_int64)
py_obj_lt = extern("py_obj_lt", (c_ptr, c_ptr), c_int64)
py_obj_add = extern("py_obj_add", (c_ptr, c_ptr), c_ptr)
py_float_from_f64 = extern("py_float_from_f64", (c_double,), c_ptr)
py_user_special_dispatch = extern(
    "py_user_special_dispatch",
    (c_ptr, c_ptr, c_ptr, c_ptr, c_int64, c_int64, c_ptr, c_ptr), c_ptr,
)
py_raise_type_named_error = extern(
    "py_raise_type_named_error", (c_ptr, c_ptr, c_ptr), c_ptr
)
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
    """Unpark the oldest waiter that can still run: 1, or 0 when none waits.

    A waiter whose virtual thread finished or was cancelled while queued
    refuses the unpark; it is dropped and the next one takes the handoff, so
    a release never fails or loses its unit to a waiter that left.
    """
    woken: int = 0
    done: int = 0
    while done == 0:
        vthread = _waiter_pop(owner, head_offset, tail_offset)
        if ptr_is_null(vthread):
            done = 1
        else:
            if py_virtual_thread_unpark(vthread) == 0:
                woken = 1
                done = 1
            py_decref_extern(vthread)
    return woken


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


def _pin_current_vthread(reason):
    """Pin a virtual thread to its carrier across a blocking OS-level wait.

    Returns the owned vthread to hand to _unpin_current_vthread, or null when
    the caller is not a virtual thread (or the pin was refused)."""
    vthread = _current_vthread()
    if ptr_is_null(vthread):
        return vthread
    if py_virtual_thread_pin_enter(vthread, reason) < 0:
        py_decref_extern(vthread)
        return null()
    return vthread


def _unpin_current_vthread(vthread) -> None:
    if ptr_is_null(vthread):
        return
    py_virtual_thread_pin_leave(vthread)
    py_decref_extern(vthread)


def _wait_until(cond, mutex, deadline: int) -> int:
    """Wait on ``cond`` (``mutex`` held) for a wakeup or until ``deadline``.

    ``deadline`` is a monotonic time in us; -1 waits without one and 0 (any
    past time) does not wait. The kernel wait runs exited: a collector walks
    this thread's stack from the waiting frame instead of waiting for it,
    and a thread leaving the wait during a stopped world stays parked until
    the world resumes; the safepoint-aware mutex lock of every other party
    parks too, so the primitive's mutex never blocks a collection. Returns 0
    after a wakeup (the caller rechecks its predicate) or 1 once the
    deadline passed, both with ``mutex`` held, or -1 with it released.
    """
    if deadline < 0:
        if pcc_cond_wait(cond, mutex) != 0:
            pcc_mutex_unlock(mutex)
            return -1
        return 0
    remaining: int = deadline - pcc_runtime_monotonic_us()
    if remaining <= 0:
        return 1
    if pcc_cond_timedwait_ms(cond, mutex, (remaining + 999) // 1000) < 0:
        pcc_mutex_unlock(mutex)
        return -1
    return 0


# Timeout arguments follow CPython: a real number of seconds, converted as
# _PyTime_FromSecondsObject(_PyTime_ROUND_TIMEOUT) to int64 nanoseconds
# (rounded away from zero), then rounded up to the microsecond deadline the
# waits use. A NULL argument object is an omitted argument.


def _no_timeout_ns() -> int:
    # Lock.acquire's ``timeout=-1``: CPython compares the converted value.
    return -1000000000


def _is_real_number(value) -> int:
    if is_tagged_int(value):
        return 1
    tag: int = load_i32(value, PYOBJECTHEADER_TYPE_TAG_OFFSET)
    if tag == PY_TYPE_FLOAT or tag == PY_TYPE_INT or tag == PY_TYPE_BOOL:
        return 1
    return 0


def _real_seconds(value, out) -> int:
    """PyFloat_AsDouble: out[0] = float(value) for an int, bool or float, or
    through __float__/__index__; 0, or -1 with TypeError raised."""
    if is_tagged_int(value):
        store_f64(out, 0, float(untag_int(value)))
        return 0
    if _is_real_number(value) != 0:
        store_f64(out, 0, py_float_to_f64(value))
        return 0
    handled = stack_alloc(8)
    store_i64(handled, 0, 0)
    store_f64(out, 0, 0.0)
    # Conversion 8: __float__, falling back to __index__, into an f64.
    py_user_special_dispatch(value, cstr("__float__"), null(), null(), 0, 8, out, handled)
    if py_err_occurred() != 0:
        return -1
    if load_i64(handled, 0) == 0:
        py_raise_type_named_error(cstr("must be real number, not "), value, cstr(""))
        return -1
    return 0


def _seconds_ns(seconds: float, out) -> int:
    """out[0] = ``seconds`` in ns rounded away from zero; 0, or -1 with
    ValueError (NaN) or OverflowError (outside int64 ns) raised."""
    if seconds != seconds:
        py_raise_owned(py_exc_new(2, cstr("Invalid value NaN (not a number)")))
        return -1
    scaled: float = seconds * 1000000000.0
    if scaled < -9223372036854775808.0 or scaled >= 9223372036854775808.0:
        py_raise_owned(py_exc_new(15, cstr("timestamp out of range for C PyTime_t")))
        return -1
    ns: int = int(scaled)
    if scaled > 0.0 and float(ns) < scaled:
        ns = ns + 1
    if scaled < 0.0 and float(ns) > scaled:
        ns = ns - 1
    store_i64(out, 0, ns)
    return 0


def _timeout_ns(value, out) -> int:
    """out[0] = the timeout object ``value`` in ns; 0, or -1 with the error raised."""
    seconds = stack_alloc(8)
    if _real_seconds(value, seconds) != 0:
        return -1
    return _seconds_ns(load_f64(seconds, 0), out)


def _deadline_after_ns(ns: int) -> int:
    """The monotonic deadline (us) ``ns`` from now, rounded up; 0 when ``ns`` is not positive."""
    if ns <= 0:
        return 0
    return pcc_runtime_monotonic_us() + (ns + 999) // 1000


def _blocking_flag(blocking) -> int:
    """``bool(blocking)``, True when omitted; -1 with the __bool__ error raised."""
    if ptr_is_null(blocking):
        return 1
    truth: int = py_obj_truthy(blocking)
    if py_err_occurred() != 0:
        return -1
    if truth != 0:
        return 1
    return 0


def _is_none_or_omitted(value) -> int:
    if ptr_is_null(value) or ptr_eq(value, global_load_ptr("py_None")):
        return 1
    return 0


def _lock_acquire_deadline(blocking, timeout, out) -> int:
    """Lock/RLock.acquire(blocking=True, timeout=-1), as CPython's
    lock_acquire_parse_args: out[0] = -1 (no deadline), 0 (try once) or the
    monotonic deadline in us. 0, or -1 with the error raised."""
    wait: int = _blocking_flag(blocking)
    if wait < 0:
        return -1
    ns: int = _no_timeout_ns()
    if ptr_is_null(timeout) == 0:
        converted = stack_alloc(8)
        if _timeout_ns(timeout, converted) != 0:
            return -1
        ns = load_i64(converted, 0)
    if wait == 0 and ns != _no_timeout_ns():
        py_raise_owned(py_exc_new(2, cstr("can't specify a timeout for a non-blocking call")))
        return -1
    if ns < 0 and ns != _no_timeout_ns():
        py_raise_owned(py_exc_new(2, cstr("timeout value must be a non-negative number")))
        return -1
    if wait == 0:
        store_i64(out, 0, 0)
    elif ns == _no_timeout_ns():
        store_i64(out, 0, -1)
    else:
        store_i64(out, 0, _deadline_after_ns(ns))
    return 0


def _condition_append(cond, node) -> None:
    tail = load_ptr(cond, 40)
    if ptr_is_null(tail):
        store_ptr(cond, 32, node)
    else:
        store_ptr(tail, 8, node)
    store_ptr(cond, 40, node)


def _condition_unlink(cond, target) -> int:
    previous = null()
    node = load_ptr(cond, 32)
    while ptr_is_null(node) == 0:
        after = load_ptr(node, 8)
        if ptr_eq(node, target):
            if ptr_is_null(previous):
                store_ptr(cond, 32, after)
            else:
                store_ptr(previous, 8, after)
            if ptr_eq(load_ptr(cond, 40), node):
                store_ptr(cond, 40, previous)
            store_ptr(node, 8, null())
            return 1
        previous = node
        node = after
    return 0


def _condition_retire(node) -> None:
    handle = load_ptr(node, 16)
    pcc_gc_scheduler_root_unregister_handle(handle)
    if load_i64(node, 24) >= 0:
        pcc_vthread_effect_note_waiter_root_leave()
    store_ptr(node, 16, null())
    pcc_gc_store_root(node, null())
    _waiter_recycle(node)


def _condition_enqueue(cond_root, vthread_root, state: int):
    node = _waiter_alloc()
    if ptr_is_null(node):
        return null()
    handle = pcc_gc_scheduler_root_register_handle(node)
    if ptr_is_null(handle):
        _waiter_recycle(node)
        return null()
    store_ptr(node, 16, handle)
    store_i64(node, 24, state)
    if state >= 0:
        pcc_vthread_effect_note_waiter_root_enter()
        pcc_gc_store_root(node, pcc_gc_load_ptr(null(), vthread_root))
    else:
        # Native waiters retain the healable receiver, not a carrier/task.
        pcc_gc_store_root(node, pcc_gc_load_ptr(null(), cond_root))
    _condition_append(pcc_gc_load_ptr(null(), cond_root), node)
    return node


def _condition_pin(value) -> int:
    prior: int = PY_FLAG_GC_PINNED
    if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        prior = load_i32(value, PYOBJECTHEADER_FLAGS_OFFSET) & PY_FLAG_GC_PINNED
        if prior == 0:
            pcc_gc_pin(value)
    return prior


def _condition_unpin(value, prior: int) -> None:
    if prior == 0:
        pcc_gc_unpin(value)


def _condition_deadline(timeout) -> int:
    """Condition.wait(timeout=None) as CPython: None waits without a deadline
    (-1); ``timeout > 0`` waits that long (waiter.acquire(True, timeout));
    any other timeout tries once (0). -2 with the comparison or conversion
    error raised."""
    if _is_none_or_omitted(timeout) != 0:
        return -1
    prior: int = _condition_pin(timeout)
    positive: int = py_obj_gt(timeout, py_int_from_i64(0))
    deadline: int = -2
    if py_err_occurred() == 0:
        deadline = 0
        if positive != 0:
            converted = stack_alloc(8)
            if _timeout_ns(timeout, converted) == 0:
                deadline = _deadline_after_ns(load_i64(converted, 0))
            else:
                deadline = -2
    _condition_unpin(timeout, prior)
    return deadline


def _condition_native_wait(cond_root, deadline: int) -> int:
    state: int = -1 if deadline < 0 else 0 - deadline - 3
    node = _condition_enqueue(cond_root, null(), state)
    if ptr_is_null(node):
        return -1
    result: int = 0
    if deadline >= 0 and pcc_runtime_monotonic_us() >= deadline:
        # A wait that tries once still releases and reacquires the lock.
        cond = pcc_gc_load_ptr(null(), node)
        mutex = load_ptr(cond, 16)
        pcc_mutex_unlock(mutex)
        if pcc_mutex_lock(mutex) != 0:
            # A broken platform mutex cannot protect a queue mutation. Keep
            # its registered token alive rather than racing notify/recycling.
            return -1
    while load_i64(node, 24) != -2 and result == 0:
        if deadline >= 0 and pcc_runtime_monotonic_us() >= deadline:
            result = 2
        else:
            # notify marks this node under the Condition's mutex and then
            # broadcasts, so the wait cannot miss it; the node's root heals
            # the receiver while its unmanaged mutex and cond stay put.
            cond = pcc_gc_load_ptr(null(), node)
            if _wait_until(load_ptr(cond, 24), load_ptr(cond, 16), deadline) < 0:
                return -1
    cond = pcc_gc_load_ptr(null(), node)
    _condition_unlink(cond, node)
    _condition_retire(node)
    return result


def _condition_vm_node(cond, vthread):
    node = load_ptr(cond, 32)
    while ptr_is_null(node) == 0:
        if load_i64(node, 24) >= 0:
            if ptr_eq(pcc_gc_load_ptr(null(), node), vthread):
                return node
        node = load_ptr(node, 8)
    return null()


def _condition_vm_park(vthread, deadline: int) -> int:
    if deadline < 0:
        return py_virtual_thread_park(vthread)
    remaining: int = deadline - pcc_runtime_monotonic_us()
    delay: int = 1 if remaining <= 0 else (remaining + 999) // 1000
    return py_virtual_thread_sleep(vthread, delay)


def _condition_wait(cond, timeout, allow_vthread: int) -> int:
    if ptr_is_null(cond) or is_tagged_int(cond):
        return -1
    cond_root = stack_alloc(24)
    memset(cond_root, 0, 24)
    store_ptr(cond_root, 0, cond)
    store_ptr(cond_root, 8, timeout)
    pcc_gc_frame_enter(global_addr("pcc_threading_condition_borrowed_frame_map"), cond_root)
    deadline: int = _condition_deadline(pcc_gc_load_ptr(null(), ptr_add(cond_root, 8)))
    result: int = -1
    if deadline != -2:
        vthread = _current_vthread() if allow_vthread != 0 else null()
        vthread_prior: int = _condition_pin(vthread)
        store_ptr(cond_root, 16, vthread)
        if ptr_is_null(vthread):
            pinned = _pin_current_vthread(cstr("threading.Condition.wait"))
            pinned_prior: int = _condition_pin(pinned)
            store_ptr(cond_root, 16, pinned)
            result = _condition_native_wait(cond_root, deadline)
            store_ptr(cond_root, 16, null())
            if ptr_is_null(pinned) == 0:
                py_virtual_thread_pin_leave(pinned)
                _condition_unpin(pinned, pinned_prior)
                py_decref_extern(pinned)
        elif deadline >= 0 and deadline <= pcc_runtime_monotonic_us():
            # A nonblocking wait still releases/restores the primitive.
            cond = pcc_gc_load_ptr(null(), cond_root)
            mutex = load_ptr(cond, 16)
            pcc_mutex_unlock(mutex)
            pcc_thread_safepoint()
            result = 2 if pcc_mutex_lock(mutex) == 0 else -1
        else:
            state: int = 0 if deadline < 0 else deadline + 2
            node = _condition_enqueue(cond_root, ptr_add(cond_root, 16), state)
            if ptr_is_null(node) == 0:
                rc: int = _condition_vm_park(vthread, deadline)
                cond = pcc_gc_load_ptr(null(), cond_root)
                if rc == 0 and pcc_mutex_unlock(load_ptr(cond, 16)) == 0:
                    result = 1
                else:
                    _condition_unlink(cond, node)
                    _condition_retire(node)
        if ptr_is_null(vthread) == 0:
            store_ptr(cond_root, 16, null())
            _condition_unpin(vthread, vthread_prior)
            py_decref_extern(vthread)
    pcc_gc_frame_leave(cond_root)
    return result


def _condition_wake_one(cond) -> int:
    node = load_ptr(cond, 32)
    while ptr_is_null(node) == 0:
        state: int = load_i64(node, 24)
        after = load_ptr(node, 8)
        if (state > 1 and pcc_runtime_monotonic_us() >= state - 2) or (state < -2 and pcc_runtime_monotonic_us() >= 0 - state - 3):
            # Expired owners consume their nodes when they resume/relock.
            # Do not spend a notification on an already elapsed wait.
            node = after
            continue
        _condition_unlink(cond, node)
        if state < 0:
            # The waiter can inspect its own still-rooted token outside the
            # primitive. Release/acquire publication cannot lose a notify.
            atomic_store_i64(node, 24, -2, "release")
            return 1 if pcc_cond_broadcast(load_ptr(cond, 24)) == 0 else -1
        vthread = pcc_gc_load_ptr(null(), node)
        prior: int = _condition_pin(vthread)
        py_incref_extern(vthread)
        _condition_retire(node)
        if ptr_is_null(vthread) == 0:
            result: int = py_virtual_thread_unpark(vthread)
            _condition_unpin(vthread, prior)
            py_decref_extern(vthread)
            return 1 if result == 0 else -1
        node = after
    return 0


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


def _lock_acquire_until(lock, deadline: int) -> int:
    """Take a Lock: 0 acquired, 1 when ``deadline`` passed first, -1 failure."""
    m = load_ptr(lock, 16)
    c = load_ptr(lock, 24)
    vthread = _pin_current_vthread(cstr("threading.Lock.acquire"))
    if pcc_mutex_lock(m) != 0:
        _unpin_current_vthread(vthread)
        return -1
    result: int = 0
    while load_i64(lock, 32) != 0 and result == 0:
        result = _wait_until(c, m, deadline)
    if result == 0:
        store_i64(lock, 32, 1)
    if result >= 0 and pcc_mutex_unlock(m) != 0:
        result = -1
    _unpin_current_vthread(vthread)
    return result


@c_abi_export("py_threading_lock_acquire")
def py_threading_lock_acquire(lock) -> int:
    if ptr_is_null(lock) or is_tagged_int(lock):
        return -1
    return _lock_acquire_until(lock, -1)


def _is_lock_object(lock) -> int:
    if ptr_is_null(lock) or is_tagged_int(lock):
        return 0
    tag: int = load_i32(lock, PYOBJECTHEADER_TYPE_TAG_OFFSET)
    if tag == PY_TYPE_THREAD_LOCK or tag == PY_TYPE_THREAD_RLOCK:
        return 1
    return 0


@c_abi_export("py_threading_lock_acquire_args")
def py_threading_lock_acquire_args(lock, blocking, timeout) -> int:
    """Lock.acquire / RLock.acquire(blocking=True, timeout=-1): 0 acquired,
    1 not acquired, -1 with the exception raised."""
    if _is_lock_object(lock) == 0:
        py_raise_owned(py_exc_new(3, cstr("acquire() requires a threading lock")))
        return -1
    # Argument conversion may run __bool__/__float__ and collect: the pins
    # keep these raw operands in place until they are no longer read.
    prior: int = _condition_pin(lock)
    blocking_prior: int = _condition_pin(blocking)
    timeout_prior: int = _condition_pin(timeout)
    deadline = stack_alloc(8)
    converted: int = _lock_acquire_deadline(blocking, timeout, deadline)
    _condition_unpin(timeout, timeout_prior)
    _condition_unpin(blocking, blocking_prior)
    result: int = -1
    if converted == 0:
        if load_i32(lock, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_THREAD_RLOCK:
            result = _rlock_acquire_until(lock, load_i64(deadline, 0))
        else:
            result = _lock_acquire_until(lock, load_i64(deadline, 0))
    _condition_unpin(lock, prior)
    return result


@c_abi_export("py_threading_lock_locked")
def py_threading_lock_locked(lock) -> int:
    """Lock.locked() and RLock.locked(): 1 while held, 0 when free, -1 on error.

    Offset 32 is a Lock's held flag and an RLock's recursion count; both are
    written under the mutex at offset 16.
    """
    if ptr_is_null(lock) or is_tagged_int(lock):
        return -1
    m = load_ptr(lock, 16)
    if pcc_mutex_lock(m) != 0:
        return -1
    held: int = load_i64(lock, 32)
    if pcc_mutex_unlock(m) != 0:
        return -1
    return 1 if held != 0 else 0


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
        py_raise_owned(py_exc_new(7, cstr("release unlocked lock")))
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


# RLock (PyThreadRLockObject): header, mutex@16, owner@24, recursion
# depth@32, cond@40, parked virtual-thread FIFO head@48/tail@56 (64 bytes).
# Owner and depth change only under the mutex; a depth above zero means
# held. The final release hands the lock to the oldest parked virtual thread
# (owner and depth set for it) or else clears the owner and signals ``cond``.


def _owner_identity() -> int:
    """The RLock owner of the running code: a virtual thread's negated
    identity, else the OS thread id.

    Carriers are shared and a virtual thread may resume on another one, so
    the carrier's thread id names neither the holder nor the releaser.
    """
    virtual: int = py_virtual_thread_current_identity()
    if virtual != 0:
        return 0 - virtual
    return pcc_current_thread_id()


@c_abi_export("py_threading_rlock_new")
def py_threading_rlock_new():
    lock = _alloc_obj(PY_TYPE_THREAD_RLOCK, 64)
    if ptr_is_null(lock):
        return lock
    mutex = pcc_mutex_new()
    cond = pcc_cond_new()
    if ptr_is_null(mutex) or ptr_is_null(cond):
        pcc_cond_free(cond)
        pcc_mutex_free(mutex)
        pcc_gc_free_object_memory(lock)
        return null()
    store_ptr(lock, 16, mutex)
    store_i64(lock, 24, 0)
    store_i64(lock, 32, 0)
    store_ptr(lock, 40, cond)
    store_ptr(lock, 48, null())
    store_ptr(lock, 56, null())
    return lock


def _rlock_acquire_until(lock, deadline: int) -> int:
    """Take an RLock: 0 acquired (or re-entered), 1 when ``deadline`` passed
    first, -1 on a primitive failure."""
    owner: int = _owner_identity()
    m = load_ptr(lock, 16)
    c = load_ptr(lock, 40)
    vthread = _pin_current_vthread(cstr("threading.RLock.acquire"))
    if pcc_mutex_lock(m) != 0:
        _unpin_current_vthread(vthread)
        return -1
    result: int = 0
    if load_i64(lock, 32) > 0 and load_i64(lock, 24) == owner:
        store_i64(lock, 32, load_i64(lock, 32) + 1)
    else:
        while load_i64(lock, 32) > 0 and result == 0:
            result = _wait_until(c, m, deadline)
        if result == 0:
            store_i64(lock, 24, owner)
            store_i64(lock, 32, 1)
    if result >= 0 and pcc_mutex_unlock(m) != 0:
        result = -1
    _unpin_current_vthread(vthread)
    return result


@c_abi_export("py_threading_rlock_acquire")
def py_threading_rlock_acquire(lock) -> int:
    if ptr_is_null(lock) or is_tagged_int(lock):
        return -1
    return _rlock_acquire_until(lock, -1)


@c_abi_export("py_threading_rlock_release")
def py_threading_rlock_release(lock) -> int:
    """RLock.release(): 0, or -1 with RuntimeError raised unless the calling
    thread owns the lock."""
    if ptr_is_null(lock) or is_tagged_int(lock):
        return -1
    owner: int = _owner_identity()
    m = load_ptr(lock, 16)
    if pcc_mutex_lock(m) != 0:
        return -1
    depth: int = load_i64(lock, 32)
    if depth <= 0 or load_i64(lock, 24) != owner:
        pcc_mutex_unlock(m)
        py_raise_owned(py_exc_new(7, cstr("cannot release un-acquired lock")))
        return -1
    depth = depth - 1
    store_i64(lock, 32, depth)
    if depth == 0 and _rlock_hand_off(lock) == 0:
        store_i64(lock, 24, 0)
        pcc_cond_signal(load_ptr(lock, 40))
    return pcc_mutex_unlock(m)


def _rlock_hand_off(lock) -> int:
    """Give a released RLock (mutex held) to its oldest parked virtual thread
    that can still run: 1 when one took it, 0 when none waits."""
    handed: int = 0
    done: int = 0
    while done == 0:
        vthread = _waiter_pop(lock, 48, 56)
        if ptr_is_null(vthread):
            done = 1
        else:
            store_i64(lock, 24, 0 - load_i64(vthread, PYVIRTUALTHREADOBJECT_IDENTITY_OFFSET))
            store_i64(lock, 32, 1)
            if py_virtual_thread_unpark(vthread) == 0:
                handed = 1
                done = 1
            else:
                store_i64(lock, 32, 0)
            py_decref_extern(vthread)
    return handed


@c_abi_export("py_threading_rlock_acquire_vthread")
def py_threading_rlock_acquire_vthread(lock) -> int:
    """RLock.acquire() in a resumable frame: 0 acquired or re-entered, 1
    parked until a release hands this virtual thread the lock, -1 failure.

    Outside a virtual thread it is the blocking acquire.
    """
    if ptr_is_null(lock) or is_tagged_int(lock):
        return -1
    vthread = _current_vthread()
    if ptr_is_null(vthread):
        return py_threading_rlock_acquire(lock)
    me: int = 0 - load_i64(vthread, PYVIRTUALTHREADOBJECT_IDENTITY_OFFSET)
    m = load_ptr(lock, 16)
    if pcc_mutex_lock(m) != 0:
        py_decref_extern(vthread)
        return -1
    result: int = 0
    depth: int = load_i64(lock, 32)
    if depth > 0 and load_i64(lock, 24) == me:
        store_i64(lock, 32, depth + 1)
    elif depth == 0:
        store_i64(lock, 24, me)
        store_i64(lock, 32, 1)
    elif _waiter_enqueue(lock, 48, 56, vthread) != 0:
        result = -1
    elif py_virtual_thread_park(vthread) == 0:
        result = 1
    else:
        # A park the scheduler refuses (cancelled task) leaves the node for
        # the releaser's handoff, which skips a waiter that cannot resume.
        result = -1
    pcc_mutex_unlock(m)
    py_decref_extern(vthread)
    return result


@c_abi_export("py_dealloc_thread_rlock")
def py_dealloc_thread_rlock(lock) -> None:
    if ptr_is_null(lock) or is_tagged_int(lock):
        return
    _waiters_clear(lock, 48, 56)
    pcc_cond_free(load_ptr(lock, 40))
    pcc_mutex_free(load_ptr(lock, 16))
    pcc_gc_free_object_memory(lock)


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


def _event_wait_until(event, deadline: int) -> int:
    """0 once the flag is set, 1 when ``deadline`` passed first, -1 failure."""
    m = load_ptr(event, 16)
    c = load_ptr(event, 24)
    vthread = _pin_current_vthread(cstr("threading.Event.wait"))
    if pcc_mutex_lock(m) != 0:
        _unpin_current_vthread(vthread)
        return -1
    result: int = 0
    while load_i64(event, 32) == 0 and result == 0:
        result = _wait_until(c, m, deadline)
    if result >= 0 and pcc_mutex_unlock(m) != 0:
        result = -1
    _unpin_current_vthread(vthread)
    return result


@c_abi_export("py_threading_event_wait")
def py_threading_event_wait(event) -> int:
    if ptr_is_null(event) or is_tagged_int(event):
        return -1
    return _event_wait_until(event, -1)


@c_abi_export("py_threading_event_wait_args")
def py_threading_event_wait_args(event, timeout) -> int:
    """Event.wait(timeout=None): 0 when the flag is set, 1 when the timeout
    passed first, -1 with the exception raised."""
    if ptr_is_null(event) or is_tagged_int(event):
        return -1
    # CPython returns a set flag before it looks at the timeout.
    if atomic_load_i64(event, 32, "acquire") != 0:
        return 0
    prior: int = _condition_pin(event)
    deadline: int = _condition_deadline(timeout)
    result: int = -1
    if deadline != -2:
        result = _event_wait_until(event, deadline)
    _condition_unpin(event, prior)
    return result


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
    if ptr_is_null(cond) or is_tagged_int(cond):
        return -1
    mutex = load_ptr(cond, 16)
    vthread = _pin_current_vthread(cstr("threading.Condition.acquire"))
    result: int = pcc_mutex_lock(mutex)
    _unpin_current_vthread(vthread)
    return result


@c_abi_export("py_threading_condition_release")
def py_threading_condition_release(cond) -> int:
    if ptr_is_null(cond):
        return -1
    return pcc_mutex_unlock(load_ptr(cond, 16))


@c_abi_export("py_threading_condition_wait")
def py_threading_condition_wait(cond) -> int:
    return _condition_wait(cond, global_load_ptr("py_None"), 0)


@c_abi_export("py_threading_condition_wait_timeout")
def py_threading_condition_wait_timeout(cond, timeout) -> int:
    return _condition_wait(cond, timeout, 0)


@c_abi_export("py_threading_condition_wait_vthread")
def py_threading_condition_wait_vthread(cond) -> int:
    return _condition_wait(cond, global_load_ptr("py_None"), 1)


@c_abi_export("py_threading_condition_wait_vthread_timeout")
def py_threading_condition_wait_vthread_timeout(cond, timeout) -> int:
    return _condition_wait(cond, timeout, 1)


@c_abi_export("py_threading_condition_wait_resume")
def py_threading_condition_wait_resume(cond) -> int:
    # Called with Condition reacquired by the lifted caller. The timer cannot
    # masquerade as notify: an outstanding FIFO entry is still our waiter.
    cond_prior: int = _condition_pin(cond)
    vthread = _current_vthread()
    if ptr_is_null(vthread):
        _condition_unpin(cond, cond_prior)
        return -1
    vthread_prior: int = _condition_pin(vthread)
    node = _condition_vm_node(cond, vthread)
    if ptr_is_null(node):
        _condition_unpin(vthread, vthread_prior)
        py_decref_extern(vthread)
        _condition_unpin(cond, cond_prior)
        return 0
    state: int = load_i64(node, 24)
    deadline: int = -1 if state == 0 else state - 2
    if deadline >= 0 and pcc_runtime_monotonic_us() >= deadline:
        _condition_unlink(cond, node)
        _condition_retire(node)
        py_virtual_thread_cancel_timer(vthread)
        _condition_unpin(vthread, vthread_prior)
        py_decref_extern(vthread)
        _condition_unpin(cond, cond_prior)
        return 2
    rc: int = _condition_vm_park(vthread, deadline)
    if rc != 0:
        _condition_unlink(cond, node)
        _condition_retire(node)
        py_virtual_thread_cancel_timer(vthread)
    _condition_unpin(vthread, vthread_prior)
    py_decref_extern(vthread)
    result: int = 1 if rc == 0 and pcc_mutex_unlock(load_ptr(cond, 16)) == 0 else -1
    _condition_unpin(cond, cond_prior)
    return result


@c_abi_export("py_threading_condition_wait_cancel")
def py_threading_condition_wait_cancel(cond) -> int:
    cond_prior: int = _condition_pin(cond)
    vthread = _current_vthread()
    if ptr_is_null(vthread):
        _condition_unpin(cond, cond_prior)
        return -1
    vthread_prior: int = _condition_pin(vthread)
    mutex = load_ptr(cond, 16)
    if pcc_mutex_lock(mutex) != 0:
        _condition_unpin(vthread, vthread_prior)
        py_decref_extern(vthread)
        _condition_unpin(cond, cond_prior)
        return -1
    node = _condition_vm_node(cond, vthread)
    if ptr_is_null(node) == 0:
        _condition_unlink(cond, node)
        _condition_retire(node)
        py_virtual_thread_cancel_timer(vthread)
    _condition_unpin(vthread, vthread_prior)
    py_decref_extern(vthread)
    _condition_unpin(cond, cond_prior)
    # Condition.wait restores the lock even when an injected exception ends
    # the wait; the caller's normal with/finally path owns its release.
    return 0


@c_abi_export("py_threading_condition_notify")
def py_threading_condition_notify(cond) -> int:
    if ptr_is_null(cond):
        return -1
    prior: int = _condition_pin(cond)
    woken = _condition_wake_one(cond)
    result: int = -1
    if woken >= 0:
        result = 0 if woken != 0 else pcc_cond_signal(load_ptr(cond, 24))
    _condition_unpin(cond, prior)
    return result


@c_abi_export("py_threading_condition_notify_all")
def py_threading_condition_notify_all(cond) -> int:
    if ptr_is_null(cond) or is_tagged_int(cond):
        return -1
    prior: int = _condition_pin(cond)
    woken: int = 1
    while woken > 0:
        woken = _condition_wake_one(cond)
    result: int = pcc_cond_broadcast(load_ptr(cond, 24)) if woken == 0 else -1
    _condition_unpin(cond, prior)
    return result


@c_abi_export("py_dealloc_thread_condition")
def py_dealloc_thread_condition(cond) -> None:
    if ptr_is_null(cond):
        return
    _waiters_clear(cond, 32, 40)
    pcc_cond_free(load_ptr(cond, 24))
    pcc_mutex_free(load_ptr(cond, 16))
    pcc_gc_free_object_memory(cond)


# Semaphore and BoundedSemaphore: header, mutex@16, cond@24, value@32,
# waiter-head@40, waiter-tail@48, bound@56 (the BoundedSemaphore initial
# value, -1 when unbounded; 64 bytes).


def _semaphore_alloc(initial: int, bound: int):
    o = _alloc_obj(PY_TYPE_THREAD_SEMAPHORE, 64)
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
    store_i64(o, 56, bound)
    return o


@c_abi_export("py_threading_semaphore_new")
def py_threading_semaphore_new(initial: int):
    if initial < 0:
        initial = 0
    return _semaphore_alloc(initial, -1)


def _semaphore_count(value, out) -> int:
    """out[0] = the int count ``value``; 0, or -1 with TypeError raised."""
    if is_tagged_int(value):
        store_i64(out, 0, untag_int(value))
        return 0
    if load_i32(value, PYOBJECTHEADER_TYPE_TAG_OFFSET) == PY_TYPE_BOOL:
        store_i64(out, 0, 1 if ptr_eq(value, global_load_ptr("py_True")) else 0)
        return 0
    py_raise_type_named_error(
        cstr("threading semaphore counts are machine ints in pcc, not "), value, cstr("")
    )
    return -1


@c_abi_export("py_threading_semaphore_new_args")
def py_threading_semaphore_new_args(value, bounded: int):
    """Semaphore(value=1) / BoundedSemaphore(value=1): a NEW semaphore, or
    NULL with ValueError for a negative value (CPython's ``value < 0``)."""
    initial: int = 1
    if ptr_is_null(value) == 0:
        negative: int = py_obj_lt(value, py_int_from_i64(0))
        if py_err_occurred() != 0:
            return null()
        if negative != 0:
            py_raise_owned(py_exc_new(2, cstr("semaphore initial value must be >= 0")))
            return null()
        count = stack_alloc(8)
        if _semaphore_count(value, count) != 0:
            return null()
        initial = load_i64(count, 0)
    bound: int = -1
    if bounded != 0:
        bound = initial
    return _semaphore_alloc(initial, bound)


def _semaphore_acquire_until(sem, deadline: int) -> int:
    """Take one unit: 0 acquired, 1 when ``deadline`` passed first, -1 failure."""
    m = load_ptr(sem, 16)
    c = load_ptr(sem, 24)
    vthread = _pin_current_vthread(cstr("threading.Semaphore.acquire"))
    if pcc_mutex_lock(m) != 0:
        _unpin_current_vthread(vthread)
        return -1
    result: int = 0
    while load_i64(sem, 32) <= 0 and result == 0:
        result = _wait_until(c, m, deadline)
    if result == 0:
        store_i64(sem, 32, load_i64(sem, 32) - 1)
    if result >= 0 and pcc_mutex_unlock(m) != 0:
        result = -1
    _unpin_current_vthread(vthread)
    return result


@c_abi_export("py_threading_semaphore_acquire")
def py_threading_semaphore_acquire(sem) -> int:
    if ptr_is_null(sem) or is_tagged_int(sem):
        return -1
    return _semaphore_acquire_until(sem, -1)


def _semaphore_deadline(timeout) -> int:
    """Semaphore.acquire's ``endtime = _time() + timeout`` as a monotonic
    deadline in us (0 when already elapsed), or -2 with the error raised."""
    now_us: int = pcc_runtime_monotonic_us()
    ns = stack_alloc(8)
    if _is_real_number(timeout) != 0:
        if _timeout_ns(timeout, ns) != 0:
            return -2
        return _deadline_after_ns(load_i64(ns, 0))
    now_seconds: float = float(now_us) / 1000000.0
    start = py_float_from_f64(now_seconds)
    if ptr_is_null(start):
        return -2
    end = py_obj_add(start, timeout)
    py_decref_extern(start)
    if ptr_is_null(end):
        return -2
    seconds = stack_alloc(8)
    converted: int = _real_seconds(end, seconds)
    py_decref_extern(end)
    if converted != 0:
        return -2
    if _seconds_ns(load_f64(seconds, 0) - now_seconds, ns) != 0:
        return -2
    return _deadline_after_ns(load_i64(ns, 0))


@c_abi_export("py_threading_semaphore_acquire_args")
def py_threading_semaphore_acquire_args(sem, blocking, timeout) -> int:
    """Semaphore.acquire(blocking=True, timeout=None): 0 acquired, 1 not
    acquired, -1 with the exception raised."""
    if ptr_is_null(sem) or is_tagged_int(sem):
        return -1
    prior: int = _condition_pin(sem)
    blocking_prior: int = _condition_pin(blocking)
    timeout_prior: int = _condition_pin(timeout)
    result: int = -1
    wait: int = _blocking_flag(blocking)
    timed: int = 1 - _is_none_or_omitted(timeout)
    if wait == 0 and timed != 0:
        py_raise_owned(py_exc_new(2, cstr("can't specify timeout for non-blocking acquire")))
    elif wait == 0:
        result = _semaphore_acquire_until(sem, 0)
    elif wait > 0 and timed == 0:
        result = _semaphore_acquire_until(sem, -1)
    elif wait > 0:
        # CPython computes the end time only once the value is zero.
        result = _semaphore_acquire_until(sem, 0)
        if result == 1:
            deadline: int = _semaphore_deadline(timeout)
            result = -1
            if deadline != -2:
                result = _semaphore_acquire_until(sem, deadline)
    _condition_unpin(timeout, timeout_prior)
    _condition_unpin(blocking, blocking_prior)
    _condition_unpin(sem, prior)
    return result


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


def _semaphore_release_count(sem, n: int) -> int:
    """Release ``n`` units: parked virtual threads take theirs directly.
    0, or -1 with ValueError raised past a BoundedSemaphore's bound."""
    m = load_ptr(sem, 16)
    c = load_ptr(sem, 24)
    if pcc_mutex_lock(m) != 0:
        return -1
    bound: int = load_i64(sem, 56)
    if bound >= 0 and load_i64(sem, 32) + n > bound:
        pcc_mutex_unlock(m)
        py_raise_owned(py_exc_new(2, cstr("Semaphore released too many times")))
        return -1
    released: int = 0
    while released < n:
        woken = _waiter_wake_one(sem, 40, 48)
        if woken < 0:
            pcc_mutex_unlock(m)
            return -1
        if woken == 0:
            store_i64(sem, 32, load_i64(sem, 32) + 1)
        released = released + 1
    if n == 1:
        pcc_cond_signal(c)
    else:
        pcc_cond_broadcast(c)
    return pcc_mutex_unlock(m)


@c_abi_export("py_threading_semaphore_release")
def py_threading_semaphore_release(sem) -> int:
    if ptr_is_null(sem):
        return -1
    return _semaphore_release_count(sem, 1)


@c_abi_export("py_threading_semaphore_release_args")
def py_threading_semaphore_release_args(sem, n) -> int:
    """Semaphore.release(n=1): 0, or -1 with ValueError raised for n < 1."""
    if ptr_is_null(sem) or is_tagged_int(sem):
        return -1
    count: int = 1
    if ptr_is_null(n) == 0:
        # ``n < 1`` may run __lt__ and collect: keep the raw receiver in place.
        prior: int = _condition_pin(sem)
        below: int = py_obj_lt(n, py_int_from_i64(1))
        _condition_unpin(sem, prior)
        if py_err_occurred() != 0:
            return -1
        if below != 0:
            py_raise_owned(py_exc_new(2, cstr("n must be one or more")))
            return -1
        converted = stack_alloc(8)
        if _semaphore_count(n, converted) != 0:
            return -1
        count = load_i64(converted, 0)
    return _semaphore_release_count(sem, count)


@c_abi_export("py_dealloc_thread_semaphore")
def py_dealloc_thread_semaphore(sem) -> None:
    if ptr_is_null(sem):
        return
    _waiters_clear(sem, 40, 48)
    pcc_cond_free(load_ptr(sem, 24))
    pcc_mutex_free(load_ptr(sem, 16))
    pcc_gc_free_object_memory(sem)


# PyThreadObject: header@0, handle@16, callable@24, args@32, result@40,
# started@48, joined@56 (0, 2 while the first joiner reaps, 1), finished@64,
# done mutex@72, done cond@80, ident@88, handoff pin@96 (the header pin bit
# before start(); 104 bytes). The thread running the target sets
# ``finished`` under the done mutex and broadcasts the cond; is_alive()
# reads it on any thread.


def _is_thread(thread) -> int:
    if ptr_is_null(thread) or is_tagged_int(thread):
        return 0
    if load_i32(thread, PYOBJECTHEADER_TYPE_TAG_OFFSET) != PY_TYPE_THREAD:
        return 0
    return 1


@c_abi_export("py_threading_thread_new")
def py_threading_thread_new(callable, args):
    o = _alloc_obj(PY_TYPE_THREAD, 104)
    if ptr_is_null(o):
        return o
    done_mutex = pcc_mutex_new()
    done_cond = pcc_cond_new()
    if ptr_is_null(done_mutex) or ptr_is_null(done_cond):
        pcc_cond_free(done_cond)
        pcc_mutex_free(done_mutex)
        pcc_gc_free_object_memory(o)
        return null()
    none = global_load_ptr("py_None")
    callable_value = callable
    if ptr_is_null(callable_value):
        callable_value = none
    args_value = args
    if ptr_is_null(args_value):
        args_value = none
    store_ptr(o, 16, null())
    store_ptr(o, 24, null())
    store_ptr(o, 32, null())
    store_ptr(o, 40, null())
    store_i64(o, 48, 0)
    store_i64(o, 56, 0)
    store_i64(o, 64, 0)
    store_ptr(o, 72, done_mutex)
    store_ptr(o, 80, done_cond)
    store_i64(o, 88, 0)
    store_i64(o, 96, 0)
    pcc_gc_store_ptr(o, ptr_add(o, 24), callable_value)
    pcc_gc_store_ptr(o, ptr_add(o, 32), args_value)
    return o


def _thread_invoke_clear(slots, pins, offset: int) -> None:
    value = load_ptr(slots, offset)
    if ptr_is_null(value) == 0 and is_tagged_int(value) == 0:
        pcc_gc_unpin(value)
        if load_i64(pins, offset) != 0:
            atomic_rmw_i32("or", value, 12, 64, "relaxed")
    pcc_gc_store_root(ptr_add(slots, offset), null())


def _thread_mark_finished(thread) -> None:
    m = load_ptr(thread, 72)
    if pcc_mutex_lock(m) != 0:
        # Joiners wait on this cond; a broken mutex must not strand them.
        pcc_platform_abort()
        return
    atomic_store_i64(thread, 64, 1, "release")
    pcc_cond_broadcast(load_ptr(thread, 80))
    pcc_mutex_unlock(m)


def _thread_invoke(thread) -> None:
    if atomic_load_i64(thread, 64, "acquire") != 0:
        if load_i64(thread, 96) == 0:
            pcc_gc_unpin(thread)
        return
    store_i64(thread, 88, pcc_current_thread_id())
    # Nested calls can clear the single physical pin bit. Every operand that
    # survives the target body also has a healable root and is reloaded.
    slots = stack_alloc(32)
    pins = stack_alloc(32)
    memset(slots, 0, 32)
    memset(pins, 0, 32)
    # start() pinned the Thread for the handoff; its pre-start pin state is
    # the one to restore once the root below owns the object.
    store_i64(pins, 0, load_i64(thread, 96))
    pcc_gc_pin(thread)
    pcc_gc_frame_enter(global_addr("pcc_threading_invoke_frame_map"), slots)
    pcc_gc_store_root(slots, thread)
    callable_obj = pcc_gc_load_ptr(load_ptr(slots, 0), ptr_add(load_ptr(slots, 0), 24))
    if ptr_is_null(callable_obj) == 0 and is_tagged_int(callable_obj) == 0:
        store_i64(pins, 8, load_i32(callable_obj, 12) & 64)
        pcc_gc_pin(callable_obj)
    pcc_gc_store_root(ptr_add(slots, 8), callable_obj)
    args_obj = pcc_gc_load_ptr(load_ptr(slots, 0), ptr_add(load_ptr(slots, 0), 32))
    if ptr_is_null(args_obj) == 0 and is_tagged_int(args_obj) == 0:
        store_i64(pins, 16, load_i32(args_obj, 12) & 64)
        pcc_gc_pin(args_obj)
    pcc_gc_store_root(ptr_add(slots, 16), args_obj)
    callable_obj = load_ptr(slots, 8)
    if ptr_is_null(callable_obj) == 0 and ptr_eq(callable_obj, global_load_ptr("py_None")) == 0:
        result = py_obj_call_sync(callable_obj, load_ptr(slots, 16), null())
        store_ptr(slots, 24, result)
        if ptr_is_null(result) == 0 and is_tagged_int(result) == 0:
            store_i64(pins, 24, load_i32(result, 12) & 64)
            pcc_gc_pin(result)
            pcc_gc_note_write_barrier(null(), result)
        thread = load_ptr(slots, 0)
        atomic_rmw_i32("or", thread, 12, 64, "relaxed")
        pcc_gc_store_ptr(thread, ptr_add(thread, 40), load_ptr(slots, 24))
    else:
        pcc_gc_store_ptr(load_ptr(slots, 0), ptr_add(load_ptr(slots, 0), 40), global_load_ptr("py_None"))
    # Result is now owned by Thread, before its returned reference is released.
    # Reverse lease order preserves original pin state when values alias.
    _thread_invoke_clear(slots, pins, 24)
    _thread_invoke_clear(slots, pins, 16)
    _thread_invoke_clear(slots, pins, 8)
    _thread_mark_finished(load_ptr(slots, 0))
    _thread_invoke_clear(slots, pins, 0)
    pcc_gc_frame_leave(slots)


@c_abi_export("py_threading_thread_main_py")
def py_threading_thread_main(thread):
    if ptr_is_null(thread):
        return null()
    _thread_invoke(thread)
    # Release the start-handoff reference taken before pcc_thread_start.
    # Read the result first: this decref may free the Thread wrapper when
    # the program dropped its last reference right after start().
    result = pcc_gc_load_ptr(thread, ptr_add(thread, 40))
    py_decref_extern(thread)
    return result


@c_abi_export("py_threading_thread_start")
def py_threading_thread_start(thread) -> int:
    """Thread.start(): 0, or -1 with RuntimeError raised."""
    if _is_thread(thread) == 0:
        return -1
    if load_i64(thread, 48) != 0:
        py_raise_owned(py_exc_new(7, cstr("threads can only be started once")))
        return -1
    # The new thread receives a borrowed pointer, so take an owned handoff
    # reference before it starts: ``t = Thread(...); t.start(); t = None``
    # must not free the wrapper before the thread body runs.
    py_incref_extern(thread)
    # The pointer is also raw until the new thread registers and roots it.
    # A moving collector decides from the registered threads only, so it
    # could relocate the Thread meanwhile and leave the new thread reading
    # the old copy (its args, its locks). The pin holds the object in place
    # across the handoff; _thread_invoke restores the pre-start state.
    prior: int = load_i32(thread, PYOBJECTHEADER_FLAGS_OFFSET) & PY_FLAG_GC_PINNED
    store_i64(thread, 96, prior)
    if prior == 0:
        pcc_gc_pin(thread)
    if pcc_thread_start(
        ptr_add(thread, 16),
        function_addr("py_threading_thread_main_py"),
        thread,
    ) != 0:
        if prior == 0:
            pcc_gc_unpin(thread)
        py_decref_extern(thread)
        py_raise_owned(py_exc_new(7, cstr("can't start new thread")))
        return -1
    store_i64(thread, 48, 1)
    return 0


def _thread_wait_finished(thread, deadline: int) -> int:
    """0 once the target finished, 1 when ``deadline`` passed first, -1 failure."""
    m = load_ptr(thread, 72)
    c = load_ptr(thread, 80)
    vthread = _pin_current_vthread(cstr("threading.Thread.join"))
    if pcc_mutex_lock(m) != 0:
        _unpin_current_vthread(vthread)
        return -1
    result: int = 0
    while atomic_load_i64(thread, 64, "acquire") == 0 and result == 0:
        result = _wait_until(c, m, deadline)
    if result >= 0 and pcc_mutex_unlock(m) != 0:
        result = -1
    _unpin_current_vthread(vthread)
    return result


def _thread_reap(thread) -> int:
    """Join the finished platform thread exactly once (the first joiner)."""
    if atomic_cas_i64(thread, 56, 0, 2, "acq_rel", "acquire") != 0:
        return 0
    status: int = 0
    handle = load_ptr(thread, 16)
    if ptr_is_null(handle) == 0:
        vthread = _pin_current_vthread(cstr("threading.Thread.join"))
        status = pcc_thread_join(handle, null())
        _unpin_current_vthread(vthread)
        store_ptr(thread, 16, null())
    atomic_store_i64(thread, 56, 1, "release")
    if status != 0:
        return -1
    return 0


@c_abi_export("py_threading_thread_join_args")
def py_threading_thread_join_args(thread, timeout) -> int:
    """Thread.join(timeout=None): 0 once joined or when the timeout passed
    first (is_alive() tells which), -1 with the exception raised."""
    if _is_thread(thread) == 0:
        py_raise_owned(py_exc_new(3, cstr("join() requires a threading.Thread")))
        return -1
    if load_i64(thread, 48) == 0:
        py_raise_owned(py_exc_new(7, cstr("cannot join thread before it is started")))
        return -1
    if atomic_load_i64(thread, 64, "acquire") == 0 and load_i64(thread, 88) == pcc_current_thread_id():
        py_raise_owned(py_exc_new(7, cstr("cannot join current thread")))
        return -1
    prior: int = _condition_pin(thread)
    timeout_prior: int = _condition_pin(timeout)
    deadline: int = -1
    if _is_none_or_omitted(timeout) == 0:
        # CPython: ``timeout = max(timeout, 0)``, then the handle's timed join.
        negative: int = py_obj_gt(py_int_from_i64(0), timeout)
        if py_err_occurred() != 0:
            deadline = -2
        elif negative != 0:
            deadline = 0
        else:
            converted = stack_alloc(8)
            if _timeout_ns(timeout, converted) != 0:
                deadline = -2
            else:
                deadline = _deadline_after_ns(load_i64(converted, 0))
    _condition_unpin(timeout, timeout_prior)
    result: int = -1
    if deadline != -2:
        result = _thread_wait_finished(thread, deadline)
        if result == 0:
            result = _thread_reap(thread)
        elif result == 1:
            result = 0
        if result < 0 and py_err_occurred() == 0:
            py_raise_owned(py_exc_new(7, cstr("thread join failed")))
    _condition_unpin(thread, prior)
    return result


@c_abi_export("py_threading_thread_join")
def py_threading_thread_join(thread) -> int:
    return py_threading_thread_join_args(thread, null())


@c_abi_export("py_threading_thread_is_alive")
def py_threading_thread_is_alive(thread) -> int:
    if _is_thread(thread) == 0:
        return 0
    if load_i64(thread, 48) == 0 or load_i64(thread, 56) != 0:
        return 0
    return 1 if atomic_load_i64(thread, 64, "acquire") == 0 else 0


@c_abi_export("py_dealloc_thread_thread")
def py_dealloc_thread_thread(thread) -> None:
    if ptr_is_null(thread):
        return
    handle = load_ptr(thread, 16)
    if ptr_is_null(handle) == 0 and load_i64(thread, 56) == 0:
        pcc_thread_detach(handle)
        store_ptr(thread, 16, null())
    py_decref_extern(pcc_gc_load_ptr(thread, ptr_add(thread, 24)))  # callable
    py_decref_extern(pcc_gc_load_ptr(thread, ptr_add(thread, 32)))  # args
    py_decref_extern(pcc_gc_load_ptr(thread, ptr_add(thread, 40)))  # result
    pcc_cond_free(load_ptr(thread, 80))
    pcc_mutex_free(load_ptr(thread, 72))
    pcc_gc_free_object_memory(thread)
