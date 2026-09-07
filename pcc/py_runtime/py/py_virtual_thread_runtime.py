"""Virtual-thread scheduler authored in pcc-Python.

This is the production owner for the bounded carrier scheduler.  Queue, timer,
and IO nodes are raw allocation records whose object slots are registered with
the shared GC root registry.  The module reuses the pcc-Python thread kernel,
waitset, and platform clock ABIs; no host Python or C scheduler owns the
policy.

The scheduler keeps a baseline carrier count of one for the calling thread,
matching the C oracle ABI.  A native pool adds at most 64 carrier threads.
Ready work is distributed over fixed per-carrier queues, and an empty carrier
checks each peer at most once before consulting the injection queue.  The
bounded scan makes work stealing observable without allowing an unbounded
search under the scheduler mutex.
"""

__pcc_runtime_port__ = True

from pcc.extern import c_abi_export, c_int32, c_int64, c_ptr, c_void, extern
from pcc.py_runtime.py.py_abi_constants import (
    PCC_VTHREAD_CHANNEL_KIND_CORE,
    PCC_VTHREAD_CHANNEL_KIND_RECEIVER,
    PCC_VTHREAD_CHANNEL_KIND_SENDER,
    PCC_VTHREAD_CHANNEL_MAX_CAPACITY,
    PCC_VTHREAD_CHANNEL_MODE_MPSC,
    PCC_VTHREAD_CHANNEL_MODE_ONESHOT,
    PCC_VTHREAD_CHANNEL_RECV_RECEIVER_CLOSED,
    PCC_VTHREAD_CHANNEL_RECV_SENDER_CLOSED,
    PCC_VTHREAD_CHANNEL_RECV_VALUE,
    PCC_VTHREAD_WAIT_CHANNEL_RECV,
    PCC_VTHREAD_WAIT_CHANNEL_SELECT2,
    PCC_VTHREAD_WAIT_CHANNEL_SEND,
    PY_TYPE_CONTINUATION,
    PY_TYPE_GEN,
    PY_TYPE_VTHREAD_CHANNEL,
    PY_TYPE_VIRTUAL_THREAD,
)
from pcc.unsafe import (
    atomic_cas_i64,
    atomic_load_i64,
    atomic_rmw_i64,
    atomic_store_i64,
    call_i64_i64_i64_ptr,
    call_i64_ptr2,
    call_void_ptr0,
    calloc,
    cstr,
    define_global_i32,
    define_global_i64,
    define_global_i64_array,
    define_global_null_ptr_array,
    define_global_ptr_null,
    define_thread_local_i32,
    define_thread_local_ptr_null,
    free,
    function_addr,
    global_addr,
    global_load_ptr,
    global_store_ptr,
    int_to_ptr,
    is_tagged_int,
    load_i8,
    load_i32,
    load_i64,
    load_ptr,
    malloc,
    null,
    page_alloc,
    poll_fd,
    ptr_add,
    ptr_eq,
    ptr_is_null,
    ptr_to_int,
    stack_alloc,
    store_i32,
    store_i64,
    store_ptr,
)


# Scheduler state. Raw node layouts are documented at each allocator below.
define_global_ptr_null("pcc_vthread_ready_head_py")
define_global_ptr_null("pcc_vthread_ready_tail_py")
define_global_ptr_null("pcc_vthread_ready_free_py")
define_global_ptr_null("pcc_vthread_timer_head_py")
define_global_ptr_null("pcc_vthread_timer_free_py")
define_global_ptr_null("pcc_vthread_join_free_py")
define_global_i64("pcc_vthread_join_free_count_py", 0)
define_global_ptr_null("pcc_vthread_channel_free_py")
define_global_i64("pcc_vthread_channel_free_count_py", 0)
define_global_ptr_null("pcc_vthread_io_head_py")
define_global_ptr_null("pcc_vthread_io_free_py")
define_global_ptr_null("pcc_vthread_io_resource_head_py")
define_global_i64("pcc_vthread_io_resource_generation_py", 0)
define_global_i64("pcc_vthread_ready_count_py", 0)
define_global_i64("pcc_vthread_io_count_py", 0)
define_global_i64("pcc_vthread_ready_alloc_py", 0)
define_global_i64("pcc_vthread_ready_reuse_py", 0)
define_global_i64("pcc_vthread_ready_cached_py", 0)
define_global_i64("pcc_vthread_timer_alloc_py", 0)
define_global_i64("pcc_vthread_timer_reuse_py", 0)
define_global_i64("pcc_vthread_timer_cached_py", 0)
define_global_i64("pcc_vthread_io_alloc_py", 0)
define_global_i64("pcc_vthread_io_reuse_py", 0)
define_global_i64("pcc_vthread_io_cached_py", 0)
define_global_i64("pcc_vthread_waiter_alloc_py", 0)
define_global_i64("pcc_vthread_waiter_reuse_py", 0)
define_global_i64("pcc_vthread_waiter_cached_py", 0)
define_global_i64("pcc_vthread_pin_depth_py", 0)
define_global_i64("pcc_vthread_pin_events_py", 0)
define_global_i64("pcc_vthread_pin_reason_dropped_py", 0)
define_global_i64_array(
    "pcc_vthread_pin_reason_hashes_py",
    0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0,
)
define_global_i64_array(
    "pcc_vthread_pin_reason_events_py",
    0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0,
    0, 0, 0, 0, 0, 0, 0, 0,
)
define_global_ptr_null("pcc_vthread_effect_buffer_py")
define_global_i64("pcc_vthread_effect_count_py", 0)
define_global_i64("pcc_vthread_effect_dropped_py", 0)
define_thread_local_ptr_null("pcc_current_virtual_thread_py")
define_thread_local_i32("pcc_current_virtual_thread_carrier_py", -1)
define_global_i64_array(
    "pcc_vthread_waitset_py", 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0
)
define_global_i32("pcc_vthread_waitset_ready_py", 0)
define_global_i32("pcc_vthread_waitset_backend_py", 0)
define_global_i32("pcc_vthread_wait_active_py", 0)

# Scheduler synchronization and bounded carrier state.  Pointer-valued
# one-time initialization uses integer bits so pcc-Python can install the
# mutex/condition with an atomic CAS without a C-owned initializer.
define_global_i64("pcc_vthread_scheduler_mutex_bits_py", 0)
define_global_i64("pcc_vthread_scheduler_cond_bits_py", 0)
define_global_null_ptr_array("pcc_vthread_carrier_heads_py", 64)
define_global_null_ptr_array("pcc_vthread_carrier_tails_py", 64)
define_global_i64("pcc_vthread_carrier_queue_count_py", 0)
define_global_i64("pcc_vthread_next_carrier_enqueue_py", 0)
define_global_i64("pcc_vthread_carrier_count_py", 1)
define_global_i64("pcc_vthread_carrier_steal_count_py", 0)
define_global_i64("pcc_vthread_carrier_failures_py", 0)
define_global_i64("pcc_vthread_bounded_pool_running_py", 0)
define_global_i64("pcc_vthread_persistent_pool_running_py", 0)
define_global_i64("pcc_vthread_persistent_pool_stop_py", 0)
define_global_i64("pcc_vthread_persistent_pool_failures_py", 0)
define_global_i64("pcc_vthread_persistent_carrier_count_py", 0)
define_global_i64("pcc_vthread_persistent_joined_count_py", 0)
define_global_i64("pcc_vthread_persistent_cleanup_active_py", 0)
define_global_ptr_null("pcc_vthread_persistent_handles_py")
define_global_ptr_null("pcc_vthread_persistent_args_py")


pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_ptr)
pcc_gc_free_object_memory = extern(
    "pcc_gc_free_object_memory", (c_ptr,), c_void
)
pcc_gc_load_ptr = extern("pcc_gc_load_ptr", (c_ptr, c_ptr), c_ptr)
pcc_gc_store_ptr = extern("pcc_gc_store_ptr", (c_ptr, c_ptr, c_ptr), c_void)
pcc_gc_store_root = extern("pcc_gc_store_root", (c_ptr, c_ptr), c_void)
pcc_gc_note_relocation_read = extern(
    "pcc_gc_note_relocation_read", (c_ptr,), c_ptr
)
pcc_gc_scheduler_root_register_handle = extern(
    "pcc_gc_scheduler_root_register_handle", (c_ptr,), c_ptr
)
pcc_gc_scheduler_root_unregister_handle = extern(
    "pcc_gc_scheduler_root_unregister_handle", (c_ptr,), c_void
)
pcc_gc_pin = extern("pcc_gc_pin", (c_ptr,), c_void)
pcc_gc_unpin = extern("pcc_gc_unpin", (c_ptr,), c_void)
py_gc_track = extern("py_gc_track", (c_ptr,), c_void)
py_incref = extern("py_incref", (c_ptr,), c_void)
py_decref = extern("py_decref", (c_ptr,), c_void)
py_exc_new = extern("py_exc_new", (c_int64, c_ptr), c_ptr)
py_raise = extern("py_raise", (c_ptr,), c_void)
# py_raise increfs; a caller that created the exception must release it.
py_raise_owned = extern("py_raise_owned", (c_ptr,), c_void)
pcc_runtime_monotonic_us = extern("pcc_runtime_monotonic_us", (), c_int64)
pcc_threads_enabled = extern("pcc_threads_enabled", (), c_int64)
pcc_thread_start = extern("pcc_thread_start", (c_ptr, c_ptr, c_ptr), c_int64)
pcc_thread_join = extern("pcc_thread_join", (c_ptr, c_ptr), c_int64)
pcc_thread_safepoint = extern("pcc_thread_safepoint", (), c_void)
pcc_platform_getenv = extern("pcc_platform_getenv", (c_ptr,), c_ptr)
pcc_mutex_new = extern("pcc_mutex_new", (), c_ptr)
pcc_mutex_free = extern("pcc_mutex_free", (c_ptr,), c_void)
pcc_mutex_lock = extern("pcc_mutex_lock", (c_ptr,), c_int64)
pcc_mutex_unlock = extern("pcc_mutex_unlock", (c_ptr,), c_int64)
pcc_cond_new = extern("pcc_cond_new", (), c_ptr)
pcc_cond_free = extern("pcc_cond_free", (c_ptr,), c_void)
pcc_cond_signal = extern("pcc_cond_signal", (c_ptr,), c_int64)
pcc_cond_broadcast = extern("pcc_cond_broadcast", (c_ptr,), c_int64)
pcc_cond_timedwait_ms = extern(
    "pcc_cond_timedwait_ms", (c_ptr, c_ptr, c_int64), c_int64
)

pcc_io_waitset_init = extern("pcc_io_waitset_init", (c_ptr, c_int64), c_int64)
pcc_io_waitset_dispose = extern("pcc_io_waitset_dispose", (c_ptr,), c_void)
pcc_io_waitset_add = extern(
    "pcc_io_waitset_add", (c_ptr, c_int64, c_int64, c_int64, c_int64), c_int64
)
pcc_io_waitset_remove = extern(
    "pcc_io_waitset_remove", (c_ptr, c_int64), c_int64
)
pcc_io_waitset_set_ready = extern(
    "pcc_io_waitset_set_ready", (c_ptr, c_int64, c_int64), c_void
)
pcc_io_waitset_wait = extern(
    "pcc_io_waitset_wait", (c_ptr, c_int64, c_ptr), c_int64
)
pcc_io_waitset_wait_until = extern(
    "pcc_io_waitset_wait_until",
    (c_ptr, c_int64, c_int64, c_ptr),
    c_int64,
)
pcc_io_waitset_interrupt = extern(
    "pcc_io_waitset_interrupt", (c_ptr,), c_int64
)
pcc_io_waitset_wait_prepare = extern(
    "pcc_io_waitset_wait_prepare",
    (c_ptr, c_int64, c_int64, c_ptr),
    c_int64,
)
pcc_io_waitset_wait_block = extern(
    "pcc_io_waitset_wait_block", (c_ptr, c_ptr), c_int64
)
pcc_io_waitset_wait_finish = extern(
    "pcc_io_waitset_wait_finish", (c_ptr, c_ptr, c_ptr), c_int64
)
pcc_io_waitset_wait_discard = extern(
    "pcc_io_waitset_wait_discard", (c_ptr,), c_void
)
pcc_io_waitset_kqueue_available = extern(
    "pcc_io_waitset_kqueue_available", (), c_int32
)
pcc_io_waitset_epoll_available = extern(
    "pcc_io_waitset_epoll_available", (), c_int32
)

py_continuation_resume_pc = extern(
    "py_continuation_resume_pc", (c_ptr,), c_ptr
)
py_continuation_resume_abi = extern(
    "py_continuation_resume_abi", (c_ptr,), c_int64
)
py_continuation_get_slot = extern(
    "py_continuation_get_slot", (c_ptr, c_int64), c_ptr
)
py_gen_next = extern("py_gen_next", (c_ptr,), c_ptr)
py_gen_state = extern("py_gen_state", (c_ptr,), c_int64)
py_gen_set_done = extern("py_gen_set_done", (c_ptr,), c_void)
py_gen_close = extern("py_gen_close", (c_ptr,), c_ptr)
py_current_exception = extern("py_current_exception", (), c_ptr)
py_exc_builtin_class = extern("py_exc_builtin_class", (c_int64,), c_ptr)
py_exc_matches = extern("py_exc_matches", (c_ptr, c_ptr), c_int64)
py_exc_get_message = extern("py_exc_get_message", (c_ptr,), c_ptr)
py_clear_exception = extern("py_clear_exception", (), c_void)
py_int_from_i64 = extern("py_int_from_i64", (c_int64,), c_ptr)
py_tuple_new = extern("py_tuple_new", (c_int64,), c_ptr)
py_tuple_set_item = extern(
    "py_tuple_set_item", (c_ptr, c_int64, c_ptr), c_void
)


# Channel object kinds and task wait/result states use the imported generated
# ABI constants directly.  Freestanding library objects export their functions
# without running this module's top-level initializer, so a module-level alias
# would remain zero-initialized and corrupt wait-kind/endpoint discrimination.


def _counter(slot) -> int:
    return atomic_load_i64(slot, 0, "acquire")


def _counter_add(slot, value: int) -> int:
    old = atomic_rmw_i64("add", slot, 0, value, "acq_rel")
    return old + value


def _counter_set(slot, value: int) -> None:
    atomic_store_i64(slot, 0, value, "release")


def _install_scheduler_primitive(slot, candidate, is_mutex: int):
    if ptr_is_null(candidate):
        return null()
    candidate_bits = ptr_to_int(candidate)
    installed = atomic_cas_i64(
        slot,
        0,
        0,
        candidate_bits,
        "acq_rel",
        "acquire",
    )
    if installed != 0:
        if is_mutex != 0:
            pcc_mutex_free(candidate)
        else:
            pcc_cond_free(candidate)
        return int_to_ptr(installed)
    return candidate


def _scheduler_init() -> int:
    mutex_bits = _counter(global_addr("pcc_vthread_scheduler_mutex_bits_py"))
    if mutex_bits == 0:
        mutex = _install_scheduler_primitive(
            global_addr("pcc_vthread_scheduler_mutex_bits_py"),
            pcc_mutex_new(),
            1,
        )
        if ptr_is_null(mutex):
            return -1
    cond_bits = _counter(global_addr("pcc_vthread_scheduler_cond_bits_py"))
    if cond_bits == 0:
        cond = _install_scheduler_primitive(
            global_addr("pcc_vthread_scheduler_cond_bits_py"),
            pcc_cond_new(),
            0,
        )
        if ptr_is_null(cond):
            return -1
    return 0


def _scheduler_mutex():
    return int_to_ptr(
        _counter(global_addr("pcc_vthread_scheduler_mutex_bits_py"))
    )


def _scheduler_cond():
    return int_to_ptr(
        _counter(global_addr("pcc_vthread_scheduler_cond_bits_py"))
    )


def _scheduler_lock() -> int:
    if _scheduler_init() != 0:
        return -1
    return pcc_mutex_lock(_scheduler_mutex())


def _scheduler_unlock() -> None:
    pcc_mutex_unlock(_scheduler_mutex())


def _scheduler_signal() -> None:
    cond = _scheduler_cond()
    if ptr_is_null(cond) == 0:
        pcc_cond_signal(cond)


def _checked(vthread):
    if ptr_is_null(vthread) or is_tagged_int(vthread):
        py_raise_owned(py_exc_new(3, cstr("object is not a virtual thread")))
        return null()
    resolved = pcc_gc_note_relocation_read(vthread)
    if ptr_is_null(resolved) == 0:
        vthread = resolved
    if load_i32(vthread, 8) != PY_TYPE_VIRTUAL_THREAD:
        py_raise_owned(py_exc_new(3, cstr("object is not a virtual thread")))
        return null()
    return vthread


def _now_ms() -> int:
    return pcc_runtime_monotonic_us() // 1000


def _effect_buffer():
    buffer = global_load_ptr("pcc_vthread_effect_buffer_py")
    if ptr_is_null(buffer):
        buffer = page_alloc(131072)
        if ptr_is_null(buffer) == 0:
            global_store_ptr("pcc_vthread_effect_buffer_py", buffer)
    return buffer


def _effect(kind: int, detail: int, root_delta: int, state: int) -> None:
    buffer = _effect_buffer()
    index = atomic_rmw_i64(
        "add", global_addr("pcc_vthread_effect_count_py"), 0, 1, "acq_rel"
    )
    if index < 0 or index >= 4096 or ptr_is_null(buffer):
        _counter_add(global_addr("pcc_vthread_effect_dropped_py"), 1)
        return
    event = ptr_add(buffer, index * 32)
    store_i64(event, 0, kind)
    store_i64(event, 8, detail)
    store_i64(event, 16, root_delta)
    store_i64(event, 24, state)


def _effect_field(index: int, field: int) -> int:
    count = _counter(global_addr("pcc_vthread_effect_count_py"))
    buffer = global_load_ptr("pcc_vthread_effect_buffer_py")
    if index < 0 or index >= count or index >= 4096 or ptr_is_null(buffer):
        return -1
    if field < 0 or field > 3:
        return -1
    return load_i64(buffer, index * 32 + field * 8)


@c_abi_export("py_virtual_thread_effect_reset")
def py_virtual_thread_effect_reset() -> int:
    _effect_buffer()
    _counter_set(global_addr("pcc_vthread_effect_count_py"), 0)
    _counter_set(global_addr("pcc_vthread_effect_dropped_py"), 0)
    return 0


@c_abi_export("py_virtual_thread_effect_count")
def py_virtual_thread_effect_count() -> int:
    count = _counter(global_addr("pcc_vthread_effect_count_py"))
    if count > 4096:
        return 4096
    return count


@c_abi_export("py_virtual_thread_effect_dropped")
def py_virtual_thread_effect_dropped() -> int:
    return _counter(global_addr("pcc_vthread_effect_dropped_py"))


@c_abi_export("py_virtual_thread_effect_kind_at")
def py_virtual_thread_effect_kind_at(index: int) -> int:
    return _effect_field(index, 0)


@c_abi_export("py_virtual_thread_effect_detail_at")
def py_virtual_thread_effect_detail_at(index: int) -> int:
    return _effect_field(index, 1)


@c_abi_export("py_virtual_thread_effect_root_delta_at")
def py_virtual_thread_effect_root_delta_at(index: int) -> int:
    return _effect_field(index, 2)


@c_abi_export("py_virtual_thread_effect_state_at")
def py_virtual_thread_effect_state_at(index: int) -> int:
    return _effect_field(index, 3)


@c_abi_export("pcc_vthread_effect_note_waiter_root_enter")
def pcc_vthread_effect_note_waiter_root_enter() -> None:
    _effect(1, 1, 1, -1)


@c_abi_export("pcc_vthread_effect_note_waiter_root_leave")
def pcc_vthread_effect_note_waiter_root_leave() -> None:
    _effect(2, 1, -1, -1)


@c_abi_export("pcc_vthread_waiter_pool_note_allocation")
def pcc_vthread_waiter_pool_note_allocation() -> None:
    _counter_add(global_addr("pcc_vthread_waiter_alloc_py"), 1)


@c_abi_export("pcc_vthread_waiter_pool_note_reuse")
def pcc_vthread_waiter_pool_note_reuse() -> None:
    _counter_add(global_addr("pcc_vthread_waiter_reuse_py"), 1)


@c_abi_export("pcc_vthread_waiter_pool_note_cached")
def pcc_vthread_waiter_pool_note_cached(count: int) -> None:
    _counter_set(global_addr("pcc_vthread_waiter_cached_py"), count)


# Ready node: thread@0, next@8, root_handle@16, pool_kind@24 (32 bytes).
def _ready_alloc():
    node = global_load_ptr("pcc_vthread_ready_free_py")
    if ptr_is_null(node) == 0:
        global_store_ptr("pcc_vthread_ready_free_py", load_ptr(node, 8))
        _counter_add(global_addr("pcc_vthread_ready_cached_py"), -1)
        _counter_add(global_addr("pcc_vthread_ready_reuse_py"), 1)
    else:
        node = malloc(32)
        if ptr_is_null(node) == 0:
            _counter_add(global_addr("pcc_vthread_ready_alloc_py"), 1)
    if ptr_is_null(node) == 0:
        store_ptr(node, 0, null())
        store_ptr(node, 8, null())
        store_ptr(node, 16, null())
        store_i64(node, 24, 1)
    return node


# Channel waiter group (80 bytes): the ready prefix owns the one scheduler
# root for the selecting task.  arm0 is next@32/prev@40/group@48 and arm1 is
# next@56/prev@64/group@72.  Links only name stable raw arms (never an address
# inside a relocatable core), giving select-loser removal O(1) safely.
def _channel_group_alloc():
    group = global_load_ptr("pcc_vthread_channel_free_py")
    if ptr_is_null(group) == 0:
        global_store_ptr("pcc_vthread_channel_free_py", load_ptr(group, 8))
        _counter_add(global_addr("pcc_vthread_channel_free_count_py"), -1)
        pcc_vthread_waiter_pool_note_reuse()
    else:
        group = malloc(80)
        if ptr_is_null(group) == 0:
            pcc_vthread_waiter_pool_note_allocation()
    if ptr_is_null(group) == 0:
        store_ptr(group, 0, null())
        store_ptr(group, 8, null())
        store_ptr(group, 16, null())
        store_i64(group, 24, 3)
        store_ptr(group, 32, null())
        store_ptr(group, 40, null())
        store_ptr(group, 48, group)
        store_ptr(group, 56, null())
        store_ptr(group, 64, null())
        store_ptr(group, 72, group)
    pcc_vthread_waiter_pool_note_cached(
        _counter(global_addr("pcc_vthread_channel_free_count_py"))
    )
    return group


def _channel_group_recycle(group) -> None:
    store_ptr(group, 16, null())
    store_i64(group, 24, 3)
    store_ptr(group, 32, null())
    store_ptr(group, 40, null())
    store_ptr(group, 48, group)
    store_ptr(group, 56, null())
    store_ptr(group, 64, null())
    store_ptr(group, 72, group)
    cached = _counter(global_addr("pcc_vthread_channel_free_count_py"))
    if cached >= 4096:
        free(group)
        return
    store_ptr(group, 8, global_load_ptr("pcc_vthread_channel_free_py"))
    global_store_ptr("pcc_vthread_channel_free_py", group)
    _counter_add(global_addr("pcc_vthread_channel_free_count_py"), 1)
    pcc_vthread_waiter_pool_note_cached(cached + 1)


def _channel_group_release(group) -> None:
    handle = load_ptr(group, 16)
    if ptr_is_null(handle) == 0:
        pcc_gc_scheduler_root_unregister_handle(handle)
        pcc_vthread_effect_note_waiter_root_leave()
    pcc_gc_store_root(group, null())
    _channel_group_recycle(group)


def _channel_group_reserve(vthread):
    group = _channel_group_alloc()
    if ptr_is_null(group):
        return null()
    handle = pcc_gc_scheduler_root_register_handle(group)
    if ptr_is_null(handle):
        _channel_group_release(group)
        return null()
    store_ptr(group, 16, handle)
    pcc_vthread_effect_note_waiter_root_enter()
    pcc_gc_store_root(group, vthread)
    return group


def _ready_release(node) -> None:
    pool_kind = load_i64(node, 24)
    handle = load_ptr(node, 16)
    if ptr_is_null(handle) == 0:
        pcc_gc_scheduler_root_unregister_handle(handle)
        if pool_kind == 2:
            _effect(2, 1, -1, -1)
        elif pool_kind == 3:
            pcc_vthread_effect_note_waiter_root_leave()
        else:
            _effect(2, 0, -1, -1)
    pcc_gc_store_root(node, null())
    if pool_kind == 2:
        _join_recycle(node)
        return
    if pool_kind == 3:
        _channel_group_recycle(node)
        return
    store_ptr(node, 8, global_load_ptr("pcc_vthread_ready_free_py"))
    store_ptr(node, 16, null())
    store_i64(node, 24, 1)
    global_store_ptr("pcc_vthread_ready_free_py", node)
    _counter_add(global_addr("pcc_vthread_ready_cached_py"), 1)


# Join node shares the ready prefix and carries pool_kind=2 at offset 24.
def _join_alloc():
    node = global_load_ptr("pcc_vthread_join_free_py")
    if ptr_is_null(node) == 0:
        global_store_ptr("pcc_vthread_join_free_py", load_ptr(node, 8))
        _counter_add(global_addr("pcc_vthread_join_free_count_py"), -1)
        pcc_vthread_waiter_pool_note_reuse()
    else:
        node = malloc(32)
        if ptr_is_null(node) == 0:
            pcc_vthread_waiter_pool_note_allocation()
    if ptr_is_null(node) == 0:
        store_ptr(node, 0, null())
        store_ptr(node, 8, null())
        store_ptr(node, 16, null())
        store_i64(node, 24, 2)
    pcc_vthread_waiter_pool_note_cached(
        _counter(global_addr("pcc_vthread_join_free_count_py"))
    )
    return node


def _join_recycle(node) -> None:
    store_ptr(node, 16, null())
    store_i64(node, 24, 2)
    cached = _counter(global_addr("pcc_vthread_join_free_count_py"))
    if cached >= 4096:
        free(node)
        return
    store_ptr(node, 8, global_load_ptr("pcc_vthread_join_free_py"))
    global_store_ptr("pcc_vthread_join_free_py", node)
    _counter_add(global_addr("pcc_vthread_join_free_count_py"), 1)
    pcc_vthread_waiter_pool_note_cached(cached + 1)


def _join_release(node) -> None:
    handle = load_ptr(node, 16)
    if ptr_is_null(handle) == 0:
        pcc_gc_scheduler_root_unregister_handle(handle)
        pcc_vthread_effect_note_waiter_root_leave()
    pcc_gc_store_root(node, null())
    _join_recycle(node)


def _queue_push(head_slot, tail_slot, node) -> None:
    store_ptr(node, 8, null())
    tail = load_ptr(tail_slot, 0)
    if ptr_is_null(tail):
        store_ptr(head_slot, 0, node)
    else:
        store_ptr(tail, 8, node)
    store_ptr(tail_slot, 0, node)


def _carrier_head_slot(index: int):
    return ptr_add(global_addr("pcc_vthread_carrier_heads_py"), index * 8)


def _carrier_tail_slot(index: int):
    return ptr_add(global_addr("pcc_vthread_carrier_tails_py"), index * 8)


def _push_ready_node(node) -> None:
    carrier_count = _counter(
        global_addr("pcc_vthread_carrier_queue_count_py")
    )
    if carrier_count > 0:
        sequence = _counter_add(
            global_addr("pcc_vthread_next_carrier_enqueue_py"), 1
        ) - 1
        index = sequence % carrier_count
        _queue_push(
            _carrier_head_slot(index), _carrier_tail_slot(index), node
        )
        return
    _queue_push(
        global_addr("pcc_vthread_ready_head_py"),
        global_addr("pcc_vthread_ready_tail_py"),
        node,
    )


def _carrier_queues_open(carrier_count: int) -> int:
    if carrier_count <= 0:
        return 0
    if carrier_count > 64:
        carrier_count = 64
    existing = _counter(global_addr("pcc_vthread_carrier_queue_count_py"))
    if existing > 0:
        if existing == carrier_count:
            return 0
        return -1
    index = 0
    while index < carrier_count:
        store_ptr(_carrier_head_slot(index), 0, null())
        store_ptr(_carrier_tail_slot(index), 0, null())
        index = index + 1
    _counter_set(global_addr("pcc_vthread_next_carrier_enqueue_py"), 0)
    _counter_set(
        global_addr("pcc_vthread_carrier_queue_count_py"), carrier_count
    )

    # Work may have been submitted before the carrier pool was opened. Move
    # the injection queue into the same round-robin ownership policy without
    # touching the live ready count or the node/root ownership.
    node = global_load_ptr("pcc_vthread_ready_head_py")
    global_store_ptr("pcc_vthread_ready_head_py", null())
    global_store_ptr("pcc_vthread_ready_tail_py", null())
    while ptr_is_null(node) == 0:
        after = load_ptr(node, 8)
        _push_ready_node(node)
        node = after
    return 0


def _carrier_queues_close() -> None:
    carrier_count = _counter(
        global_addr("pcc_vthread_carrier_queue_count_py")
    )
    index = 0
    while index < carrier_count:
        head_slot = _carrier_head_slot(index)
        tail_slot = _carrier_tail_slot(index)
        head = load_ptr(head_slot, 0)
        tail = load_ptr(tail_slot, 0)
        if ptr_is_null(head) == 0:
            global_tail = global_load_ptr("pcc_vthread_ready_tail_py")
            if ptr_is_null(global_tail):
                global_store_ptr("pcc_vthread_ready_head_py", head)
            else:
                store_ptr(global_tail, 8, head)
            global_store_ptr("pcc_vthread_ready_tail_py", tail)
        store_ptr(head_slot, 0, null())
        store_ptr(tail_slot, 0, null())
        index = index + 1
    _counter_set(global_addr("pcc_vthread_carrier_queue_count_py"), 0)
    _counter_set(global_addr("pcc_vthread_next_carrier_enqueue_py"), 0)


def _enqueue(vthread) -> int:
    if load_i64(vthread, 40) != 0 or load_i64(vthread, 32) != 1:
        return 0
    node = _ready_alloc()
    if ptr_is_null(node):
        return -1
    handle = pcc_gc_scheduler_root_register_handle(node)
    if ptr_is_null(handle):
        free(node)
        return -1
    store_ptr(node, 16, handle)
    pcc_gc_store_root(node, vthread)
    _push_ready_node(node)
    store_i64(vthread, 40, 1)
    _counter_add(global_addr("pcc_vthread_ready_count_py"), 1)
    _effect(1, 0, 1, -1)
    _effect(3, 0, 0, 1)
    _scheduler_signal()
    return 0


def _ready_reserve(vthread):
    """Allocate and root a ready node without publishing it yet."""
    node = _ready_alloc()
    if ptr_is_null(node):
        return null()
    handle = pcc_gc_scheduler_root_register_handle(node)
    if ptr_is_null(handle):
        _ready_release(node)
        return null()
    store_ptr(node, 16, handle)
    pcc_gc_store_root(node, vthread)
    _effect(1, 0, 1, -1)
    return node


def _ready_commit(vthread, node) -> None:
    """Publish a previously rooted ready or join node."""
    store_i64(vthread, 32, 1)
    store_i64(vthread, 120, 0)
    _push_ready_node(node)
    store_i64(vthread, 40, 1)
    _counter_add(global_addr("pcc_vthread_ready_count_py"), 1)
    _effect(3, 0, 0, 1)
    _scheduler_signal()


def _make_ready(vthread) -> int:
    if ptr_is_null(vthread) or load_i64(vthread, 32) == 4:
        return 0
    store_i64(vthread, 32, 1)
    return _enqueue(vthread)


def _join_enqueue(target, waiter) -> int:
    node = _join_alloc()
    if ptr_is_null(node):
        return -1
    handle = pcc_gc_scheduler_root_register_handle(node)
    if ptr_is_null(handle):
        _join_release(node)
        return -1
    store_ptr(node, 16, handle)
    pcc_vthread_effect_note_waiter_root_enter()
    pcc_gc_store_root(node, waiter)
    tail = load_ptr(target, 96)
    if ptr_is_null(tail):
        store_ptr(target, 88, node)
    else:
        store_ptr(tail, 8, node)
    store_ptr(target, 96, node)
    store_ptr(waiter, 104, node)
    store_i64(waiter, 120, 4)
    store_i64(waiter, 40, 0)
    store_i64(waiter, 32, 3)
    _effect(5, 1, 0, 3)
    return 0


def _join_wake_all(target) -> int:
    while ptr_is_null(load_ptr(target, 88)) == 0:
        node = load_ptr(target, 88)
        waiter = pcc_gc_load_ptr(null(), node)
        valid = (
            ptr_is_null(waiter) == 0
            and is_tagged_int(waiter) == 0
            and load_i32(waiter, 8) == PY_TYPE_VIRTUAL_THREAD
            and ptr_eq(load_ptr(waiter, 104), node) != 0
            and load_i64(waiter, 32) == 3
            and load_i64(waiter, 40) == 0
        )
        if valid:
            after = load_ptr(node, 8)
            store_ptr(target, 88, after)
            if ptr_is_null(after):
                store_ptr(target, 96, null())
            store_ptr(waiter, 104, null())
            store_i64(waiter, 120, 0)
            store_i64(waiter, 32, 1)
            _push_ready_node(node)
            store_i64(waiter, 40, 1)
            _counter_add(global_addr("pcc_vthread_ready_count_py"), 1)
            _effect(3, 0, 0, 1)
            _scheduler_signal()
            _effect(6, 1, 0, load_i64(waiter, 32))
            continue
        after = load_ptr(node, 8)
        store_ptr(target, 88, after)
        if ptr_is_null(after):
            store_ptr(target, 96, null())
        _join_release(node)
    return 0


def _join_unlink_waiter(waiter):
    """Detach waiter's join node, retaining its scheduler-root ownership."""
    target_node = load_ptr(waiter, 104)
    if ptr_is_null(target_node):
        return null()
    target = pcc_gc_load_ptr(waiter, ptr_add(waiter, 112))
    if (
        ptr_is_null(target)
        or is_tagged_int(target)
        or load_i32(target, 8) != PY_TYPE_VIRTUAL_THREAD
    ):
        return null()
    previous = null()
    node = load_ptr(target, 88)
    while ptr_is_null(node) == 0 and ptr_eq(node, target_node) == 0:
        previous = node
        node = load_ptr(node, 8)
    if ptr_is_null(node):
        return null()
    after = load_ptr(node, 8)
    if ptr_is_null(previous):
        store_ptr(target, 88, after)
    else:
        store_ptr(previous, 8, after)
    if ptr_eq(load_ptr(target, 96), node) != 0:
        store_ptr(target, 96, previous)
    store_ptr(node, 8, null())
    store_ptr(waiter, 104, null())
    pcc_gc_store_ptr(waiter, ptr_add(waiter, 112), null())
    store_i64(waiter, 120, 0)
    return node


# Channel cores keep both waiter queues as raw arm pointers.  Each arm has raw
# next/previous links, so a select loser can be removed without walking either
# FIFO.  All helpers below run under the scheduler mutex.
def _channel_queue_push(core, head_offset: int, tail_offset: int, arm) -> None:
    tail = load_ptr(core, tail_offset)
    store_ptr(arm, 0, null())
    store_ptr(arm, 8, tail)
    if ptr_is_null(tail):
        store_ptr(core, head_offset, arm)
    else:
        store_ptr(tail, 0, arm)
    store_ptr(core, tail_offset, arm)


def _channel_queue_unlink(core, tail_offset: int, arm) -> int:
    previous = load_ptr(arm, 8)
    after = load_ptr(arm, 0)
    if ptr_is_null(previous):
        if ptr_eq(load_ptr(core, tail_offset - 8), arm) == 0:
            return 0
        store_ptr(core, tail_offset - 8, after)
    else:
        if ptr_eq(load_ptr(previous, 0), arm) == 0:
            return 0
        store_ptr(previous, 0, after)
    if ptr_is_null(after):
        store_ptr(core, tail_offset, previous)
    else:
        store_ptr(after, 8, previous)
    store_ptr(arm, 0, null())
    store_ptr(arm, 8, null())
    return 1


def _channel_queue_pop(core, head_offset: int, tail_offset: int):
    arm = load_ptr(core, head_offset)
    if ptr_is_null(arm):
        return null()
    _channel_queue_unlink(core, tail_offset, arm)
    return arm


def _channel_arm_group(arm):
    return load_ptr(arm, 16)


def _channel_arm_index(group, arm) -> int:
    if ptr_eq(arm, ptr_add(group, 56)) != 0:
        return 1
    return 0


def _channel_valid_object(obj, kind: int):
    if ptr_is_null(obj) or is_tagged_int(obj):
        return null()
    obj = pcc_gc_note_relocation_read(obj)
    if ptr_is_null(obj) or load_i32(obj, 8) != PY_TYPE_VTHREAD_CHANNEL:
        return null()
    if load_i64(obj, 16) != kind:
        return null()
    return obj


def _channel_core(endpoint, kind: int):
    endpoint = _channel_valid_object(endpoint, kind)
    if ptr_is_null(endpoint):
        return null()
    core = pcc_gc_load_ptr(endpoint, ptr_add(endpoint, 24))
    return _channel_valid_object(core, PCC_VTHREAD_CHANNEL_KIND_CORE)


def _channel_ring_slot(core, index: int):
    return ptr_add(core, 128 + index * 8)


def _channel_ring_push(core, value) -> None:
    tail = load_i64(core, 48)
    pcc_gc_store_ptr(core, _channel_ring_slot(core, tail), value)
    tail = tail + 1
    if tail == load_i64(core, 24):
        tail = 0
    store_i64(core, 48, tail)
    store_i64(core, 32, load_i64(core, 32) + 1)


def _channel_ring_pop(core):
    head = load_i64(core, 40)
    slot = _channel_ring_slot(core, head)
    value = pcc_gc_load_ptr(core, slot)
    if ptr_is_null(value) == 0:
        py_incref(value)
    pcc_gc_store_ptr(core, slot, null())
    head = head + 1
    if head == load_i64(core, 24):
        head = 0
    store_i64(core, 40, head)
    store_i64(core, 32, load_i64(core, 32) - 1)
    return value


def _channel_ring_clear(core) -> None:
    while load_i64(core, 32) > 0:
        value = _channel_ring_pop(core)
        if ptr_is_null(value) == 0:
            py_decref(value)


def _channel_consumer_busy(core) -> int:
    return load_i64(core, 120) & 1


def _channel_consumer_acquire(core) -> int:
    flags = load_i64(core, 120)
    if flags & 1 != 0:
        return -1
    store_i64(core, 120, flags | 1)
    return 0


def _channel_consumer_release(core) -> None:
    flags = load_i64(core, 120)
    store_i64(core, 120, flags & -2)


def _channel_send_lease_acquire(core) -> None:
    store_i64(core, 120, load_i64(core, 120) + 2)


def _channel_send_lease_release(core) -> None:
    flags = load_i64(core, 120)
    if flags >= 2:
        store_i64(core, 120, flags - 2)


def _channel_send_leases(core) -> int:
    return load_i64(core, 120) // 2


def _channel_task_clear(vthread, clear_value: int) -> None:
    pcc_gc_store_ptr(vthread, ptr_add(vthread, 136), null())
    pcc_gc_store_ptr(vthread, ptr_add(vthread, 144), null())
    if clear_value != 0:
        pcc_gc_store_ptr(vthread, ptr_add(vthread, 168), null())
    store_ptr(vthread, 152, null())
    store_ptr(vthread, 160, null())
    store_i64(vthread, 120, 0)


def _channel_publish_ready(vthread, group, status: int, index: int, value) -> None:
    if ptr_is_null(value):
        value = global_load_ptr("py_None")
    pcc_gc_store_ptr(vthread, ptr_add(vthread, 168), value)
    store_i64(vthread, 176, status)
    store_i64(vthread, 184, index)
    _channel_task_clear(vthread, 0)
    _ready_commit(vthread, group)


def _channel_detach_wait_locked(vthread):
    wait_kind = load_i64(vthread, 120)
    if (
        wait_kind != PCC_VTHREAD_WAIT_CHANNEL_SEND
        and wait_kind != PCC_VTHREAD_WAIT_CHANNEL_RECV
        and wait_kind != PCC_VTHREAD_WAIT_CHANNEL_SELECT2
    ):
        return null()
    arm0 = load_ptr(vthread, 152)
    arm1 = load_ptr(vthread, 160)
    core0 = pcc_gc_load_ptr(vthread, ptr_add(vthread, 136))
    core1 = pcc_gc_load_ptr(vthread, ptr_add(vthread, 144))
    group = null()
    if ptr_is_null(arm0) == 0:
        group = _channel_arm_group(arm0)
        if ptr_is_null(core0) == 0:
            if wait_kind == PCC_VTHREAD_WAIT_CHANNEL_SEND:
                if _channel_queue_unlink(core0, 96, arm0) != 0:
                    _channel_send_lease_release(core0)
            else:
                if _channel_queue_unlink(core0, 112, arm0) != 0:
                    _channel_consumer_release(core0)
    if ptr_is_null(arm1) == 0:
        if ptr_is_null(group):
            group = _channel_arm_group(arm1)
        if ptr_is_null(core1) == 0:
            if _channel_queue_unlink(core1, 112, arm1) != 0:
                _channel_consumer_release(core1)
    if wait_kind == PCC_VTHREAD_WAIT_CHANNEL_SEND and ptr_is_null(core0) == 0:
        _channel_maybe_wake_sender_closed(core0)
    _channel_task_clear(vthread, 1)
    store_i64(vthread, 176, -1)
    store_i64(vthread, 184, -1)
    return group


def _channel_terminal_cleanup_locked(vthread) -> int:
    wait_kind = load_i64(vthread, 120)
    if (
        wait_kind != PCC_VTHREAD_WAIT_CHANNEL_SEND
        and wait_kind != PCC_VTHREAD_WAIT_CHANNEL_RECV
        and wait_kind != PCC_VTHREAD_WAIT_CHANNEL_SELECT2
    ):
        return 0
    group = _channel_detach_wait_locked(vthread)
    if ptr_is_null(group):
        return -1
    _channel_group_release(group)
    return 1


def _channel_wake_recv_arm(core, arm, status: int, value) -> int:
    group = _channel_arm_group(arm)
    vthread = pcc_gc_load_ptr(null(), group)
    if ptr_is_null(vthread) or load_i32(vthread, 8) != PY_TYPE_VIRTUAL_THREAD:
        _channel_consumer_release(core)
        _channel_group_release(group)
        return -1
    index = _channel_arm_index(group, arm)
    if index == 0:
        other_arm = load_ptr(vthread, 160)
        other_core = pcc_gc_load_ptr(vthread, ptr_add(vthread, 144))
    else:
        other_arm = load_ptr(vthread, 152)
        other_core = pcc_gc_load_ptr(vthread, ptr_add(vthread, 136))
    if ptr_is_null(other_arm) == 0 and ptr_is_null(other_core) == 0:
        if _channel_queue_unlink(other_core, 112, other_arm) != 0:
            _channel_consumer_release(other_core)
    _channel_consumer_release(core)
    if (
        status == PCC_VTHREAD_CHANNEL_RECV_VALUE
        and load_i64(core, 72) == PCC_VTHREAD_CHANNEL_MODE_ONESHOT
    ):
        store_i64(core, 80, 2)
    _channel_publish_ready(vthread, group, status, index, value)
    return 0


def _channel_wake_sender_arm(core, arm, accepted: int) -> int:
    group = _channel_arm_group(arm)
    vthread = pcc_gc_load_ptr(null(), group)
    if ptr_is_null(vthread) or load_i32(vthread, 8) != PY_TYPE_VIRTUAL_THREAD:
        _channel_send_lease_release(core)
        _channel_group_release(group)
        return -1
    if accepted != 0:
        value = pcc_gc_load_ptr(vthread, ptr_add(vthread, 168))
        if ptr_is_null(value):
            _channel_send_lease_release(core)
            _channel_group_release(group)
            return -1
        _channel_ring_push(core, value)
    _channel_send_lease_release(core)
    pcc_gc_store_ptr(vthread, ptr_add(vthread, 168), null())
    store_i64(vthread, 176, accepted)
    _channel_task_clear(vthread, 0)
    _ready_commit(vthread, group)
    return 0


def _channel_take_sender(core, arm):
    group = _channel_arm_group(arm)
    vthread = pcc_gc_load_ptr(null(), group)
    if ptr_is_null(vthread) or load_i32(vthread, 8) != PY_TYPE_VIRTUAL_THREAD:
        _channel_send_lease_release(core)
        _channel_group_release(group)
        return null()
    value = pcc_gc_load_ptr(vthread, ptr_add(vthread, 168))
    if ptr_is_null(value):
        _channel_send_lease_release(core)
        _channel_group_release(group)
        return null()
    py_incref(value)
    _channel_send_lease_release(core)
    pcc_gc_store_ptr(vthread, ptr_add(vthread, 168), null())
    store_i64(vthread, 176, 1)
    _channel_task_clear(vthread, 0)
    _ready_commit(vthread, group)
    return value


def _channel_fill_from_sender(core) -> int:
    if load_i64(core, 64) != 0:
        return 0
    if load_i64(core, 32) >= load_i64(core, 24):
        return 0
    arm = _channel_queue_pop(core, 88, 96)
    if ptr_is_null(arm):
        return 0
    return _channel_wake_sender_arm(core, arm, 1)


def _channel_maybe_wake_sender_closed(core) -> int:
    if (
        load_i64(core, 64) == 0
        and load_i64(core, 56) == 0
        and _channel_send_leases(core) == 0
        and load_i64(core, 32) == 0
        and ptr_is_null(load_ptr(core, 88)) != 0
    ):
        recv_arm = _channel_queue_pop(core, 104, 112)
        if ptr_is_null(recv_arm) == 0:
            return _channel_wake_recv_arm(
                core,
                recv_arm,
                PCC_VTHREAD_CHANNEL_RECV_SENDER_CLOSED,
                null(),
            )
    return 0


def _channel_raise(message) -> None:
    exception = py_exc_new(7, message)
    py_raise(exception)
    py_decref(exception)


def _channel_endpoint_new(core, kind: int):
    root_slot = stack_alloc(8)
    store_ptr(root_slot, 0, null())
    handle = pcc_gc_scheduler_root_register_handle(root_slot)
    if ptr_is_null(handle):
        return null()
    pcc_gc_store_root(root_slot, core)
    endpoint = pcc_gc_alloc(40, PY_TYPE_VTHREAD_CHANNEL, 0)
    if ptr_is_null(endpoint):
        pcc_gc_scheduler_root_unregister_handle(handle)
        pcc_gc_store_root(root_slot, null())
        return null()
    core = pcc_gc_load_ptr(null(), root_slot)
    store_i64(endpoint, 16, kind)
    store_ptr(endpoint, 24, null())
    store_i64(endpoint, 32, 0)
    pcc_gc_store_ptr(endpoint, ptr_add(endpoint, 24), core)
    py_gc_track(endpoint)
    pcc_gc_scheduler_root_unregister_handle(handle)
    pcc_gc_store_root(root_slot, null())
    return endpoint


def _channel_pair_new(capacity: int, mode: int):
    if capacity <= 0 or capacity > PCC_VTHREAD_CHANNEL_MAX_CAPACITY:
        _channel_raise(cstr("channel capacity must be between 1 and 1048576"))
        return null()
    core = pcc_gc_alloc(
        128 + capacity * 8,
        PY_TYPE_VTHREAD_CHANNEL,
        0,
    )
    if ptr_is_null(core):
        return null()
    store_i64(core, 16, PCC_VTHREAD_CHANNEL_KIND_CORE)
    store_i64(core, 24, capacity)
    store_i64(core, 32, 0)
    store_i64(core, 40, 0)
    store_i64(core, 48, 0)
    store_i64(core, 56, 1)
    store_i64(core, 64, 0)
    store_i64(core, 72, mode)
    store_i64(core, 80, 0)
    store_ptr(core, 88, null())
    store_ptr(core, 96, null())
    store_ptr(core, 104, null())
    store_ptr(core, 112, null())
    store_i64(core, 120, 0)
    index = 0
    while index < capacity:
        store_ptr(core, 128 + index * 8, null())
        index = index + 1
    py_gc_track(core)

    root_slots = stack_alloc(24)
    store_ptr(root_slots, 0, null())
    store_ptr(root_slots, 8, null())
    store_ptr(root_slots, 16, null())
    core_handle = pcc_gc_scheduler_root_register_handle(root_slots)
    if ptr_is_null(core_handle):
        py_decref(core)
        return null()
    pcc_gc_store_root(root_slots, core)
    sender = _channel_endpoint_new(core, PCC_VTHREAD_CHANNEL_KIND_SENDER)
    core = pcc_gc_load_ptr(null(), root_slots)
    if ptr_is_null(sender):
        pcc_gc_scheduler_root_unregister_handle(core_handle)
        pcc_gc_store_root(root_slots, null())
        py_decref(core)
        return null()
    sender_handle = pcc_gc_scheduler_root_register_handle(ptr_add(root_slots, 8))
    if ptr_is_null(sender_handle):
        pcc_gc_scheduler_root_unregister_handle(core_handle)
        pcc_gc_store_root(root_slots, null())
        py_decref(sender)
        py_decref(core)
        return null()
    pcc_gc_store_root(ptr_add(root_slots, 8), sender)
    receiver = _channel_endpoint_new(core, PCC_VTHREAD_CHANNEL_KIND_RECEIVER)
    sender = pcc_gc_load_ptr(null(), ptr_add(root_slots, 8))
    core = pcc_gc_load_ptr(null(), root_slots)
    if ptr_is_null(receiver):
        pcc_gc_scheduler_root_unregister_handle(sender_handle)
        pcc_gc_scheduler_root_unregister_handle(core_handle)
        pcc_gc_store_root(ptr_add(root_slots, 8), null())
        pcc_gc_store_root(root_slots, null())
        py_decref(sender)
        py_decref(core)
        return null()
    receiver_handle = pcc_gc_scheduler_root_register_handle(
        ptr_add(root_slots, 16)
    )
    if ptr_is_null(receiver_handle):
        py_decref(receiver)
        pcc_gc_scheduler_root_unregister_handle(sender_handle)
        pcc_gc_scheduler_root_unregister_handle(core_handle)
        pcc_gc_store_root(ptr_add(root_slots, 8), null())
        pcc_gc_store_root(root_slots, null())
        py_decref(sender)
        py_decref(core)
        return null()
    pcc_gc_store_root(ptr_add(root_slots, 16), receiver)
    pair = py_tuple_new(2)
    sender = pcc_gc_load_ptr(null(), ptr_add(root_slots, 8))
    receiver = pcc_gc_load_ptr(null(), ptr_add(root_slots, 16))
    core = pcc_gc_load_ptr(null(), root_slots)
    if ptr_is_null(pair) == 0:
        py_tuple_set_item(pair, 0, sender)
        py_tuple_set_item(pair, 1, receiver)
    pcc_gc_scheduler_root_unregister_handle(receiver_handle)
    pcc_gc_scheduler_root_unregister_handle(sender_handle)
    pcc_gc_scheduler_root_unregister_handle(core_handle)
    pcc_gc_store_root(ptr_add(root_slots, 16), null())
    pcc_gc_store_root(ptr_add(root_slots, 8), null())
    pcc_gc_store_root(root_slots, null())
    py_decref(receiver)
    py_decref(sender)
    py_decref(core)
    return pair


@c_abi_export("py_virtual_thread_channel_mpsc")
def py_virtual_thread_channel_mpsc(capacity: int):
    return _channel_pair_new(capacity, PCC_VTHREAD_CHANNEL_MODE_MPSC)


@c_abi_export("py_virtual_thread_channel_oneshot")
def py_virtual_thread_channel_oneshot():
    return _channel_pair_new(1, PCC_VTHREAD_CHANNEL_MODE_ONESHOT)


@c_abi_export("py_virtual_thread_channel_sender_clone")
def py_virtual_thread_channel_sender_clone(sender):
    core = _channel_core(sender, PCC_VTHREAD_CHANNEL_KIND_SENDER)
    if ptr_is_null(core):
        _channel_raise(cstr("sender_clone requires a channel sender"))
        return null()
    if load_i64(core, 72) == PCC_VTHREAD_CHANNEL_MODE_ONESHOT:
        _channel_raise(cstr("oneshot senders cannot be cloned"))
        return null()
    root_slot = stack_alloc(8)
    store_ptr(root_slot, 0, null())
    handle = pcc_gc_scheduler_root_register_handle(root_slot)
    if ptr_is_null(handle):
        return null()
    pcc_gc_store_root(root_slot, core)
    clone = _channel_endpoint_new(core, PCC_VTHREAD_CHANNEL_KIND_SENDER)
    core = pcc_gc_load_ptr(null(), root_slot)
    if ptr_is_null(clone) == 0:
        if _scheduler_lock() != 0:
            py_decref(clone)
            clone = null()
        else:
            sender = _channel_valid_object(sender, PCC_VTHREAD_CHANNEL_KIND_SENDER)
            if (
                ptr_is_null(sender)
                or load_i64(sender, 32) != 0
                or load_i64(core, 64) != 0
            ):
                py_decref(clone)
                clone = null()
            else:
                store_i64(core, 56, load_i64(core, 56) + 1)
            _scheduler_unlock()
    pcc_gc_scheduler_root_unregister_handle(handle)
    pcc_gc_store_root(root_slot, null())
    if ptr_is_null(clone):
        _channel_raise(cstr("cannot clone a closed channel sender"))
    return clone


def _channel_current_running(vthread) -> int:
    current = global_load_ptr("pcc_current_virtual_thread_py")
    current = pcc_gc_note_relocation_read(current)
    if ptr_is_null(current) or ptr_eq(current, vthread) == 0:
        return 0
    if load_i64(vthread, 32) != 2 or load_i64(vthread, 120) != 0:
        return 0
    if ptr_is_null(load_ptr(vthread, 152)) == 0:
        return 0
    if ptr_is_null(load_ptr(vthread, 160)) == 0:
        return 0
    if load_i64(vthread, 176) != -1:
        return 0
    return 1


def _channel_store_immediate(vthread, status: int, index: int, value) -> None:
    if ptr_is_null(value):
        value = global_load_ptr("py_None")
    pcc_gc_store_ptr(vthread, ptr_add(vthread, 168), value)
    store_i64(vthread, 176, status)
    store_i64(vthread, 184, index)


def _channel_consume_oneshot_sender(sender, core) -> None:
    store_i64(sender, 32, 1)
    if load_i64(core, 56) > 0:
        store_i64(core, 56, load_i64(core, 56) - 1)
    store_i64(core, 80, 1)


def _channel_recv_ready_status(core) -> int:
    if load_i64(core, 64) != 0:
        return PCC_VTHREAD_CHANNEL_RECV_RECEIVER_CLOSED
    if (
        load_i64(core, 72) == PCC_VTHREAD_CHANNEL_MODE_ONESHOT
        and load_i64(core, 80) == 2
    ):
        return PCC_VTHREAD_CHANNEL_RECV_RECEIVER_CLOSED
    if load_i64(core, 32) > 0:
        return PCC_VTHREAD_CHANNEL_RECV_VALUE
    if ptr_is_null(load_ptr(core, 88)) == 0:
        return PCC_VTHREAD_CHANNEL_RECV_VALUE
    if load_i64(core, 56) == 0:
        return PCC_VTHREAD_CHANNEL_RECV_SENDER_CLOSED
    return 0


def _channel_recv_now(core, vthread, index: int) -> int:
    status = _channel_recv_ready_status(core)
    if status == 0:
        return 0
    if status == PCC_VTHREAD_CHANNEL_RECV_VALUE:
        if load_i64(core, 32) > 0:
            value = _channel_ring_pop(core)
            if load_i64(core, 72) == PCC_VTHREAD_CHANNEL_MODE_ONESHOT:
                store_i64(core, 80, 2)
            _channel_fill_from_sender(core)
        else:
            arm = _channel_queue_pop(core, 88, 96)
            value = _channel_take_sender(core, arm)
        _channel_store_immediate(vthread, status, index, value)
        if ptr_is_null(value) == 0:
            py_decref(value)
    else:
        _channel_store_immediate(vthread, status, index, null())
    return 1


@c_abi_export("py_virtual_thread_channel_send_begin")
def py_virtual_thread_channel_send_begin(vthread, sender, value) -> int:
    vthread = _checked(vthread)
    core = _channel_core(sender, PCC_VTHREAD_CHANNEL_KIND_SENDER)
    if ptr_is_null(vthread) or ptr_is_null(core) or ptr_is_null(value):
        return -1
    if _scheduler_lock() != 0:
        return -1
    sender = _channel_valid_object(sender, PCC_VTHREAD_CHANNEL_KIND_SENDER)
    if ptr_is_null(sender):
        _scheduler_unlock()
        return -1
    core = _channel_core(sender, PCC_VTHREAD_CHANNEL_KIND_SENDER)
    if ptr_is_null(core) or _channel_current_running(vthread) == 0:
        _scheduler_unlock()
        return -1
    if load_i64(sender, 32) != 0:
        _scheduler_unlock()
        return -1
    _channel_send_lease_acquire(core)
    if load_i64(core, 64) != 0:
        if (
            load_i64(core, 72) == PCC_VTHREAD_CHANNEL_MODE_ONESHOT
            and load_i64(core, 80) == 0
        ):
            _channel_consume_oneshot_sender(sender, core)
        _channel_store_immediate(vthread, 0, -1, null())
        _channel_send_lease_release(core)
        _scheduler_unlock()
        return 1
    if load_i64(core, 72) == PCC_VTHREAD_CHANNEL_MODE_ONESHOT and load_i64(core, 80) != 0:
        _channel_send_lease_release(core)
        _scheduler_unlock()
        return -1
    recv_arm = _channel_queue_pop(core, 104, 112)
    if ptr_is_null(recv_arm) == 0:
        if load_i64(core, 72) == PCC_VTHREAD_CHANNEL_MODE_ONESHOT:
            _channel_consume_oneshot_sender(sender, core)
        if _channel_wake_recv_arm(
            core, recv_arm, PCC_VTHREAD_CHANNEL_RECV_VALUE, value
        ) != 0:
            _channel_send_lease_release(core)
            _scheduler_unlock()
            return -1
        _channel_store_immediate(vthread, 1, -1, null())
        _channel_send_lease_release(core)
        _scheduler_unlock()
        return 1
    if load_i64(core, 32) < load_i64(core, 24):
        if load_i64(core, 72) == PCC_VTHREAD_CHANNEL_MODE_ONESHOT:
            _channel_consume_oneshot_sender(sender, core)
        _channel_ring_push(core, value)
        _channel_store_immediate(vthread, 1, -1, null())
        _channel_send_lease_release(core)
        _scheduler_unlock()
        return 1
    group = _channel_group_reserve(vthread)
    if ptr_is_null(group):
        _channel_send_lease_release(core)
        _scheduler_unlock()
        return -1
    arm = ptr_add(group, 32)
    pcc_gc_store_ptr(vthread, ptr_add(vthread, 136), core)
    pcc_gc_store_ptr(vthread, ptr_add(vthread, 168), value)
    store_ptr(vthread, 152, arm)
    store_i64(vthread, 176, -1)
    store_i64(vthread, 184, -1)
    store_i64(vthread, 120, PCC_VTHREAD_WAIT_CHANNEL_SEND)
    store_i64(vthread, 40, 0)
    store_i64(vthread, 32, 3)
    _channel_queue_push(core, 88, 96, arm)
    _scheduler_unlock()
    return 0


@c_abi_export("py_virtual_thread_channel_send_result")
def py_virtual_thread_channel_send_result(vthread) -> int:
    vthread = _checked(vthread)
    if ptr_is_null(vthread):
        return -1
    if _scheduler_lock() != 0:
        return -1
    status = load_i64(vthread, 176)
    if load_i64(vthread, 120) != 0 or (status != 0 and status != 1):
        _scheduler_unlock()
        return -1
    store_i64(vthread, 176, -1)
    store_i64(vthread, 184, -1)
    pcc_gc_store_ptr(vthread, ptr_add(vthread, 168), null())
    if load_i64(vthread, 128) == 2:
        store_i64(vthread, 128, 1)
    _scheduler_unlock()
    return status


@c_abi_export("py_virtual_thread_channel_recv_begin")
def py_virtual_thread_channel_recv_begin(vthread, receiver) -> int:
    vthread = _checked(vthread)
    core = _channel_core(receiver, PCC_VTHREAD_CHANNEL_KIND_RECEIVER)
    if ptr_is_null(vthread) or ptr_is_null(core):
        return -1
    if _scheduler_lock() != 0:
        return -1
    receiver = _channel_valid_object(receiver, PCC_VTHREAD_CHANNEL_KIND_RECEIVER)
    if ptr_is_null(receiver):
        _scheduler_unlock()
        return -1
    core = _channel_core(receiver, PCC_VTHREAD_CHANNEL_KIND_RECEIVER)
    if ptr_is_null(core) or _channel_current_running(vthread) == 0:
        _scheduler_unlock()
        return -1
    if load_i64(receiver, 32) != 0 or load_i64(core, 64) != 0:
        _channel_store_immediate(
            vthread, PCC_VTHREAD_CHANNEL_RECV_RECEIVER_CLOSED, 0, null()
        )
        _scheduler_unlock()
        return 1
    if _channel_consumer_busy(core) != 0:
        _scheduler_unlock()
        return -1
    if _channel_consumer_acquire(core) != 0:
        _scheduler_unlock()
        return -1
    if _channel_recv_now(core, vthread, 0) != 0:
        _channel_consumer_release(core)
        _scheduler_unlock()
        return 1
    group = _channel_group_reserve(vthread)
    if ptr_is_null(group):
        _channel_consumer_release(core)
        _scheduler_unlock()
        return -1
    arm = ptr_add(group, 32)
    pcc_gc_store_ptr(vthread, ptr_add(vthread, 136), core)
    store_ptr(vthread, 152, arm)
    store_i64(vthread, 176, -1)
    store_i64(vthread, 184, -1)
    store_i64(vthread, 120, PCC_VTHREAD_WAIT_CHANNEL_RECV)
    store_i64(vthread, 40, 0)
    store_i64(vthread, 32, 3)
    _channel_queue_push(core, 104, 112, arm)
    _scheduler_unlock()
    return 0


def _channel_result_tuple(vthread, select_result: int):
    root_slots = stack_alloc(24)
    store_ptr(root_slots, 0, null())
    store_ptr(root_slots, 8, null())
    store_ptr(root_slots, 16, null())
    vthread_handle = pcc_gc_scheduler_root_register_handle(root_slots)
    if ptr_is_null(vthread_handle):
        return null()
    pcc_gc_store_root(root_slots, vthread)
    tuple_size = 2
    if select_result != 0:
        tuple_size = 3
    result = py_tuple_new(tuple_size)
    if ptr_is_null(result):
        pcc_gc_scheduler_root_unregister_handle(vthread_handle)
        pcc_gc_store_root(root_slots, null())
        return null()
    result_handle = pcc_gc_scheduler_root_register_handle(
        ptr_add(root_slots, 8)
    )
    value_handle = pcc_gc_scheduler_root_register_handle(
        ptr_add(root_slots, 16)
    )
    if ptr_is_null(result_handle) or ptr_is_null(value_handle):
        if ptr_is_null(result_handle) == 0:
            pcc_gc_scheduler_root_unregister_handle(result_handle)
        if ptr_is_null(value_handle) == 0:
            pcc_gc_scheduler_root_unregister_handle(value_handle)
        py_decref(result)
        pcc_gc_scheduler_root_unregister_handle(vthread_handle)
        pcc_gc_store_root(root_slots, null())
        return null()
    pcc_gc_store_root(ptr_add(root_slots, 8), result)
    vthread = pcc_gc_load_ptr(null(), root_slots)
    if _scheduler_lock() != 0:
        pcc_gc_scheduler_root_unregister_handle(value_handle)
        pcc_gc_scheduler_root_unregister_handle(result_handle)
        pcc_gc_store_root(ptr_add(root_slots, 8), null())
        py_decref(result)
        pcc_gc_scheduler_root_unregister_handle(vthread_handle)
        pcc_gc_store_root(root_slots, null())
        return null()
    status = load_i64(vthread, 176)
    index = load_i64(vthread, 184)
    if (
        load_i64(vthread, 120) != 0
        or status < PCC_VTHREAD_CHANNEL_RECV_VALUE
        or status > PCC_VTHREAD_CHANNEL_RECV_RECEIVER_CLOSED
    ):
        _scheduler_unlock()
        pcc_gc_scheduler_root_unregister_handle(value_handle)
        pcc_gc_scheduler_root_unregister_handle(result_handle)
        pcc_gc_store_root(ptr_add(root_slots, 8), null())
        py_decref(result)
        pcc_gc_scheduler_root_unregister_handle(vthread_handle)
        pcc_gc_store_root(root_slots, null())
        return null()
    value = pcc_gc_load_ptr(vthread, ptr_add(vthread, 168))
    if ptr_is_null(value):
        value = global_load_ptr("py_None")
    pcc_gc_store_root(ptr_add(root_slots, 16), value)
    pcc_gc_store_ptr(vthread, ptr_add(vthread, 168), null())
    store_i64(vthread, 176, -1)
    store_i64(vthread, 184, -1)
    if load_i64(vthread, 128) == 2:
        store_i64(vthread, 128, 1)
    _scheduler_unlock()
    status_obj = py_int_from_i64(status)
    result = pcc_gc_load_ptr(null(), ptr_add(root_slots, 8))
    value = pcc_gc_load_ptr(null(), ptr_add(root_slots, 16))
    if select_result != 0:
        index_obj = py_int_from_i64(index)
        result = pcc_gc_load_ptr(null(), ptr_add(root_slots, 8))
        value = pcc_gc_load_ptr(null(), ptr_add(root_slots, 16))
        py_tuple_set_item(result, 0, index_obj)
        py_tuple_set_item(result, 1, status_obj)
        py_tuple_set_item(result, 2, value)
        py_decref(index_obj)
    else:
        py_tuple_set_item(result, 0, status_obj)
        py_tuple_set_item(result, 1, value)
    py_decref(status_obj)
    result = pcc_gc_load_ptr(null(), ptr_add(root_slots, 8))
    pcc_gc_scheduler_root_unregister_handle(value_handle)
    pcc_gc_store_root(ptr_add(root_slots, 16), null())
    pcc_gc_scheduler_root_unregister_handle(result_handle)
    pcc_gc_store_root(ptr_add(root_slots, 8), null())
    pcc_gc_scheduler_root_unregister_handle(vthread_handle)
    pcc_gc_store_root(root_slots, null())
    return result


@c_abi_export("py_virtual_thread_channel_recv_result")
def py_virtual_thread_channel_recv_result(vthread):
    vthread = _checked(vthread)
    if ptr_is_null(vthread):
        return null()
    return _channel_result_tuple(vthread, 0)


@c_abi_export("py_virtual_thread_channel_close_sender")
def py_virtual_thread_channel_close_sender(sender) -> int:
    core = _channel_core(sender, PCC_VTHREAD_CHANNEL_KIND_SENDER)
    if ptr_is_null(core):
        return -1
    if _scheduler_lock() != 0:
        return -1
    sender = _channel_valid_object(sender, PCC_VTHREAD_CHANNEL_KIND_SENDER)
    if ptr_is_null(sender):
        _scheduler_unlock()
        return -1
    if load_i64(sender, 32) != 0:
        _scheduler_unlock()
        return 0
    store_i64(sender, 32, 1)
    count = load_i64(core, 56)
    if count > 0:
        count = count - 1
        store_i64(core, 56, count)
    if count == 0:
        _channel_maybe_wake_sender_closed(core)
    _scheduler_unlock()
    return 1


@c_abi_export("py_virtual_thread_channel_close_receiver")
def py_virtual_thread_channel_close_receiver(receiver) -> int:
    core = _channel_core(receiver, PCC_VTHREAD_CHANNEL_KIND_RECEIVER)
    if ptr_is_null(core):
        return -1
    if _scheduler_lock() != 0:
        return -1
    receiver = _channel_valid_object(receiver, PCC_VTHREAD_CHANNEL_KIND_RECEIVER)
    if ptr_is_null(receiver):
        _scheduler_unlock()
        return -1
    if load_i64(receiver, 32) != 0:
        _scheduler_unlock()
        return 0
    store_i64(receiver, 32, 1)
    store_i64(core, 64, 1)
    _channel_ring_clear(core)
    recv_arm = _channel_queue_pop(core, 104, 112)
    if ptr_is_null(recv_arm) == 0:
        _channel_wake_recv_arm(
            core, recv_arm, PCC_VTHREAD_CHANNEL_RECV_RECEIVER_CLOSED, null()
        )
    while ptr_is_null(load_ptr(core, 88)) == 0:
        send_arm = _channel_queue_pop(core, 88, 96)
        _channel_wake_sender_arm(core, send_arm, 0)
    _scheduler_unlock()
    return 1


@c_abi_export("py_virtual_thread_channel_select2_begin")
def py_virtual_thread_channel_select2_begin(
    vthread, left_receiver, right_receiver
) -> int:
    vthread = _checked(vthread)
    left_core = _channel_core(left_receiver, PCC_VTHREAD_CHANNEL_KIND_RECEIVER)
    right_core = _channel_core(right_receiver, PCC_VTHREAD_CHANNEL_KIND_RECEIVER)
    if (
        ptr_is_null(vthread)
        or ptr_is_null(left_core)
        or ptr_is_null(right_core)
        or ptr_eq(left_core, right_core) != 0
    ):
        return -1
    if _scheduler_lock() != 0:
        return -1
    left_receiver = _channel_valid_object(
        left_receiver, PCC_VTHREAD_CHANNEL_KIND_RECEIVER
    )
    right_receiver = _channel_valid_object(
        right_receiver, PCC_VTHREAD_CHANNEL_KIND_RECEIVER
    )
    if (
        ptr_is_null(left_receiver)
        or ptr_is_null(right_receiver)
    ):
        _scheduler_unlock()
        return -1
    left_core = _channel_core(left_receiver, PCC_VTHREAD_CHANNEL_KIND_RECEIVER)
    right_core = _channel_core(right_receiver, PCC_VTHREAD_CHANNEL_KIND_RECEIVER)
    if (
        ptr_is_null(left_core)
        or ptr_is_null(right_core)
        or ptr_eq(left_core, right_core) != 0
        or _channel_current_running(vthread) == 0
    ):
        _scheduler_unlock()
        return -1
    if (
        _channel_consumer_busy(left_core) != 0
        or _channel_consumer_busy(right_core) != 0
    ):
        _scheduler_unlock()
        return -1
    if load_i64(left_receiver, 32) != 0:
        _channel_store_immediate(
            vthread, PCC_VTHREAD_CHANNEL_RECV_RECEIVER_CLOSED, 0, null()
        )
        _scheduler_unlock()
        return 1
    if _channel_consumer_acquire(left_core) != 0:
        _scheduler_unlock()
        return -1
    if _channel_consumer_acquire(right_core) != 0:
        _channel_consumer_release(left_core)
        _scheduler_unlock()
        return -1
    if _channel_recv_now(left_core, vthread, 0) != 0:
        _channel_consumer_release(right_core)
        _channel_consumer_release(left_core)
        _scheduler_unlock()
        return 1
    if load_i64(right_receiver, 32) != 0:
        _channel_store_immediate(
            vthread, PCC_VTHREAD_CHANNEL_RECV_RECEIVER_CLOSED, 1, null()
        )
        _channel_consumer_release(right_core)
        _channel_consumer_release(left_core)
        _scheduler_unlock()
        return 1
    if _channel_recv_now(right_core, vthread, 1) != 0:
        _channel_consumer_release(right_core)
        _channel_consumer_release(left_core)
        _scheduler_unlock()
        return 1
    group = _channel_group_reserve(vthread)
    if ptr_is_null(group):
        _channel_consumer_release(right_core)
        _channel_consumer_release(left_core)
        _scheduler_unlock()
        return -1
    left_arm = ptr_add(group, 32)
    right_arm = ptr_add(group, 56)
    pcc_gc_store_ptr(vthread, ptr_add(vthread, 136), left_core)
    pcc_gc_store_ptr(vthread, ptr_add(vthread, 144), right_core)
    store_ptr(vthread, 152, left_arm)
    store_ptr(vthread, 160, right_arm)
    store_i64(vthread, 176, -1)
    store_i64(vthread, 184, -1)
    store_i64(vthread, 120, PCC_VTHREAD_WAIT_CHANNEL_SELECT2)
    store_i64(vthread, 40, 0)
    store_i64(vthread, 32, 3)
    _channel_queue_push(left_core, 104, 112, left_arm)
    _channel_queue_push(right_core, 104, 112, right_arm)
    _scheduler_unlock()
    return 0


@c_abi_export("py_virtual_thread_channel_select2_result")
def py_virtual_thread_channel_select2_result(vthread):
    vthread = _checked(vthread)
    if ptr_is_null(vthread):
        return null()
    return _channel_result_tuple(vthread, 1)


def _dequeue_from(head_slot, tail_slot):
    while True:
        node = load_ptr(head_slot, 0)
        if ptr_is_null(node):
            return null()
        next_node = load_ptr(node, 8)
        store_ptr(head_slot, 0, next_node)
        if ptr_is_null(next_node):
            store_ptr(tail_slot, 0, null())
        _counter_add(global_addr("pcc_vthread_ready_count_py"), -1)
        thread = pcc_gc_load_ptr(null(), node)
        if ptr_is_null(thread) == 0:
            py_incref(thread)
        _ready_release(node)
        if (
            ptr_is_null(thread) == 0
            and is_tagged_int(thread) == 0
            and load_i32(thread, 8) == PY_TYPE_VIRTUAL_THREAD
        ):
            store_i64(thread, 40, 0)
            if load_i64(thread, 32) == 1:
                store_i64(thread, 32, 2)
                _effect(7, 0, 0, 2)
                return thread
        if ptr_is_null(thread) == 0:
            py_decref(thread)


def _dequeue():
    carrier_count = _counter(
        global_addr("pcc_vthread_carrier_queue_count_py")
    )
    own = load_i32(
        global_addr("pcc_current_virtual_thread_carrier_py"), 0
    )
    if carrier_count > 0 and own >= 0 and own < carrier_count:
        thread = _dequeue_from(
            _carrier_head_slot(own), _carrier_tail_slot(own)
        )
        if ptr_is_null(thread) == 0:
            return thread
        offset = 1
        while offset < carrier_count:
            victim = (own + offset) % carrier_count
            thread = _dequeue_from(
                _carrier_head_slot(victim), _carrier_tail_slot(victim)
            )
            if ptr_is_null(thread) == 0:
                _counter_add(
                    global_addr("pcc_vthread_carrier_steal_count_py"), 1
                )
                return thread
            offset = offset + 1
    elif carrier_count > 0:
        index = 0
        while index < carrier_count:
            thread = _dequeue_from(
                _carrier_head_slot(index), _carrier_tail_slot(index)
            )
            if ptr_is_null(thread) == 0:
                return thread
            index = index + 1
    return _dequeue_from(
        global_addr("pcc_vthread_ready_head_py"),
        global_addr("pcc_vthread_ready_tail_py"),
    )


# Timer node: thread@0, deadline@8, next@16, root_handle@24 (32 bytes).
def _timer_alloc():
    node = global_load_ptr("pcc_vthread_timer_free_py")
    if ptr_is_null(node) == 0:
        global_store_ptr("pcc_vthread_timer_free_py", load_ptr(node, 16))
        _counter_add(global_addr("pcc_vthread_timer_cached_py"), -1)
        _counter_add(global_addr("pcc_vthread_timer_reuse_py"), 1)
    else:
        node = malloc(32)
        if ptr_is_null(node) == 0:
            _counter_add(global_addr("pcc_vthread_timer_alloc_py"), 1)
    if ptr_is_null(node) == 0:
        store_ptr(node, 0, null())
        store_i64(node, 8, 0)
        store_ptr(node, 16, null())
        store_ptr(node, 24, null())
    return node


def _timer_release(node) -> None:
    handle = load_ptr(node, 24)
    if ptr_is_null(handle) == 0:
        pcc_gc_scheduler_root_unregister_handle(handle)
        _effect(2, 2, -1, -1)
    pcc_gc_store_root(node, null())
    store_ptr(node, 16, global_load_ptr("pcc_vthread_timer_free_py"))
    store_ptr(node, 24, null())
    global_store_ptr("pcc_vthread_timer_free_py", node)
    _counter_add(global_addr("pcc_vthread_timer_cached_py"), 1)


def _timer_cancel(vthread) -> int:
    target = load_ptr(vthread, 56)
    if ptr_is_null(target):
        if load_i64(vthread, 120) == 1:
            store_i64(vthread, 120, 0)
        return 0
    previous = null()
    node = global_load_ptr("pcc_vthread_timer_head_py")
    while ptr_is_null(node) == 0:
        if ptr_eq(node, target):
            after = load_ptr(node, 16)
            if ptr_is_null(previous):
                global_store_ptr("pcc_vthread_timer_head_py", after)
            else:
                store_ptr(previous, 16, after)
            store_ptr(vthread, 56, null())
            if load_i64(vthread, 120) == 1:
                store_i64(vthread, 120, 0)
            _timer_release(node)
            _effect(12, 2, 0, load_i64(vthread, 32))
            return 1
        previous = node
        node = load_ptr(node, 16)
    store_ptr(vthread, 56, null())
    if load_i64(vthread, 120) == 1:
        store_i64(vthread, 120, 0)
    return 0


def _timer_add(vthread, deadline: int) -> int:
    _timer_cancel(vthread)
    node = _timer_alloc()
    if ptr_is_null(node):
        return -1
    handle = pcc_gc_scheduler_root_register_handle(node)
    if ptr_is_null(handle):
        free(node)
        return -1
    store_ptr(node, 24, handle)
    store_i64(node, 8, deadline)
    pcc_gc_store_root(node, vthread)
    previous = null()
    current = global_load_ptr("pcc_vthread_timer_head_py")
    while ptr_is_null(current) == 0 and load_i64(current, 8) <= deadline:
        previous = current
        current = load_ptr(current, 16)
    store_ptr(node, 16, current)
    if ptr_is_null(previous):
        global_store_ptr("pcc_vthread_timer_head_py", node)
    else:
        store_ptr(previous, 16, node)
    store_ptr(vthread, 56, node)
    store_i64(vthread, 120, 1)
    _effect(1, 2, 1, -1)
    _effect(8, 2, 0, load_i64(vthread, 32))
    return 0


# IO node: thread@0, fd@8, events@16, deadline@24, next@32, root@40.
def _io_alloc():
    node = global_load_ptr("pcc_vthread_io_free_py")
    if ptr_is_null(node) == 0:
        global_store_ptr("pcc_vthread_io_free_py", load_ptr(node, 32))
        _counter_add(global_addr("pcc_vthread_io_cached_py"), -1)
        _counter_add(global_addr("pcc_vthread_io_reuse_py"), 1)
    else:
        node = malloc(48)
        if ptr_is_null(node) == 0:
            _counter_add(global_addr("pcc_vthread_io_alloc_py"), 1)
    if ptr_is_null(node) == 0:
        store_ptr(node, 0, null())
        store_i64(node, 8, -1)
        store_i64(node, 16, 0)
        store_i64(node, 24, -1)
        store_ptr(node, 32, null())
        store_ptr(node, 40, null())
    return node


def _io_release(node) -> None:
    handle = load_ptr(node, 40)
    if ptr_is_null(handle) == 0:
        pcc_gc_scheduler_root_unregister_handle(handle)
        _effect(2, 3, -1, -1)
    pcc_gc_store_root(node, null())
    store_ptr(node, 32, global_load_ptr("pcc_vthread_io_free_py"))
    store_ptr(node, 40, null())
    global_store_ptr("pcc_vthread_io_free_py", node)
    _counter_add(global_addr("pcc_vthread_io_cached_py"), 1)


def _waitset_dispose_locked() -> int:
    if load_i32(global_addr("pcc_vthread_wait_active_py"), 0) != 0:
        return -1
    if load_i32(global_addr("pcc_vthread_waitset_ready_py"), 0) != 0:
        pcc_io_waitset_dispose(global_addr("pcc_vthread_waitset_py"))
    store_i32(global_addr("pcc_vthread_waitset_ready_py"), 0, 0)
    store_i32(global_addr("pcc_vthread_waitset_backend_py"), 0, 0)
    store_i32(global_addr("pcc_vthread_wait_active_py"), 0, 0)
    return 0


def _waitset_init() -> int:
    if load_i32(global_addr("pcc_vthread_waitset_ready_py"), 0) != 0:
        return 0
    requested = pcc_platform_getenv(cstr("PCC_VTHREAD_IO_BACKEND"))
    force_poll = _cstr_equals(requested, cstr("poll"))
    force_kqueue = _cstr_equals(requested, cstr("kqueue"))
    force_epoll = _cstr_equals(requested, cstr("epoll"))
    backend = 0
    if force_kqueue != 0 and pcc_io_waitset_kqueue_available() != 0:
        backend = 1
    elif force_epoll != 0 and pcc_io_waitset_epoll_available() != 0:
        backend = 2
    elif (
        force_poll == 0
        and force_kqueue == 0
        and force_epoll == 0
        and pcc_io_waitset_kqueue_available() != 0
    ):
        backend = 1
    elif (
        force_poll == 0
        and force_kqueue == 0
        and force_epoll == 0
        and pcc_io_waitset_epoll_available() != 0
    ):
        backend = 2
    ws = global_addr("pcc_vthread_waitset_py")
    if pcc_io_waitset_init(ws, backend) != 0:
        backend = 0
        if pcc_io_waitset_init(ws, 0) != 0:
            return -1
    store_i32(global_addr("pcc_vthread_waitset_backend_py"), 0, backend)
    store_i32(global_addr("pcc_vthread_wait_active_py"), 0, 0)
    store_i32(global_addr("pcc_vthread_waitset_ready_py"), 0, 1)
    # The scheduler's GC-rooted IO list survives a carrier-pool stop.  Rebuild
    # the disposed kernel registrations before the restarted pool can run.
    node = global_load_ptr("pcc_vthread_io_head_py")
    while ptr_is_null(node) == 0:
        if _io_refresh_registered(load_i64(node, 8)) != 0:
            _waitset_dispose_locked()
            return -1
        node = load_ptr(node, 32)
    return 0


def _cstr_equals(left, right) -> int:
    if ptr_is_null(left) or ptr_is_null(right):
        return 0
    index = 0
    while index < 16:
        lhs = load_i8(left, index)
        rhs = load_i8(right, index)
        if lhs != rhs:
            return 0
        if lhs == 0:
            return 1
        index = index + 1
    return 0


def _io_interrupt_locked() -> int:
    if load_i32(global_addr("pcc_vthread_wait_active_py"), 0) == 0:
        return 0
    if load_i32(global_addr("pcc_vthread_waitset_ready_py"), 0) == 0:
        return 0
    return pcc_io_waitset_interrupt(global_addr("pcc_vthread_waitset_py"))


def _io_refresh_registered(fd: int) -> int:
    interest = 0
    deadline = -1
    node = global_load_ptr("pcc_vthread_io_head_py")
    while ptr_is_null(node) == 0:
        if load_i64(node, 8) == fd:
            events = load_i64(node, 16)
            if events == 0:
                events = 1
            interest = interest | events
            candidate = load_i64(node, 24)
            if candidate >= 0 and (deadline < 0 or candidate < deadline):
                deadline = candidate
        node = load_ptr(node, 32)
    ws = global_addr("pcc_vthread_waitset_py")
    pcc_io_waitset_remove(ws, fd)
    result = 0
    if interest == 0:
        result = 0
    else:
        result = pcc_io_waitset_add(ws, fd, interest, deadline, 0)
    if _io_interrupt_locked() != 0:
        result = -1
    return result


def _io_refresh(fd: int) -> int:
    if _waitset_init() != 0:
        return -1
    return _io_refresh_registered(fd)


def _io_cancel(vthread) -> int:
    target = load_ptr(vthread, 64)
    if ptr_is_null(target):
        wait_kind = load_i64(vthread, 120)
        if wait_kind == 2 or wait_kind == 3:
            store_i64(vthread, 120, 0)
        return 0
    fd = load_i64(target, 8)
    previous = null()
    node = global_load_ptr("pcc_vthread_io_head_py")
    while ptr_is_null(node) == 0:
        if ptr_eq(node, target):
            after = load_ptr(node, 32)
            if ptr_is_null(previous):
                global_store_ptr("pcc_vthread_io_head_py", after)
            else:
                store_ptr(previous, 32, after)
            store_ptr(vthread, 64, null())
            wait_kind = load_i64(vthread, 120)
            if wait_kind == 2 or wait_kind == 3:
                store_i64(vthread, 120, 0)
            _counter_add(global_addr("pcc_vthread_io_count_py"), -1)
            _io_release(node)
            refresh_result = _io_refresh(fd)
            _effect(13, 3, 0, load_i64(vthread, 32))
            if refresh_result != 0:
                return -1
            return 1
        previous = node
        node = load_ptr(node, 32)
    store_ptr(vthread, 64, null())
    wait_kind = load_i64(vthread, 120)
    if wait_kind == 2 or wait_kind == 3:
        store_i64(vthread, 120, 0)
    return 0


def _io_resource_find(fd: int):
    node = global_load_ptr("pcc_vthread_io_resource_head_py")
    while ptr_is_null(node) == 0:
        if load_i64(node, 0) == fd:
            return node
        node = load_ptr(node, 16)
    return null()


def _io_close_waiters_locked(fd: int) -> int:
    """Reserve every ready root before retiring waiters for a closing fd."""
    reserved = null()
    reserved_tail = null()
    node = global_load_ptr("pcc_vthread_io_head_py")
    while ptr_is_null(node) == 0:
        thread = pcc_gc_load_ptr(null(), node)
        valid = (
            load_i64(node, 8) == fd
            and ptr_is_null(thread) == 0
            and is_tagged_int(thread) == 0
            and load_i32(thread, 8) == PY_TYPE_VIRTUAL_THREAD
            and ptr_eq(load_ptr(thread, 64), node) != 0
            and load_i64(thread, 32) == 3
            and (load_i64(thread, 120) == 2 or load_i64(thread, 120) == 3)
        )
        if valid:
            ready = _ready_reserve(thread)
            if ptr_is_null(ready):
                while ptr_is_null(reserved) == 0:
                    after = load_ptr(reserved, 8)
                    _ready_release(reserved)
                    reserved = after
                return -1
            store_ptr(ready, 8, null())
            if ptr_is_null(reserved_tail):
                reserved = ready
            else:
                store_ptr(reserved_tail, 8, ready)
            reserved_tail = ready
        node = load_ptr(node, 32)

    previous = null()
    node = global_load_ptr("pcc_vthread_io_head_py")
    while ptr_is_null(node) == 0:
        after = load_ptr(node, 32)
        if load_i64(node, 8) != fd:
            previous = node
            node = after
            continue
        thread = pcc_gc_load_ptr(null(), node)
        valid = (
            ptr_is_null(thread) == 0
            and is_tagged_int(thread) == 0
            and load_i32(thread, 8) == PY_TYPE_VIRTUAL_THREAD
            and ptr_eq(load_ptr(thread, 64), node) != 0
            and load_i64(thread, 32) == 3
            and (load_i64(thread, 120) == 2 or load_i64(thread, 120) == 3)
        )
        if ptr_is_null(previous):
            global_store_ptr("pcc_vthread_io_head_py", after)
        else:
            store_ptr(previous, 32, after)
        _counter_add(global_addr("pcc_vthread_io_count_py"), -1)
        if valid:
            ready = reserved
            reserved = load_ptr(ready, 8)
            store_ptr(ready, 8, null())
            store_ptr(thread, 64, null())
            store_i64(thread, 120, 0)
            _ready_commit(thread, ready)
            _effect(11, 3, 0, load_i64(thread, 32))
        elif (
            ptr_is_null(thread) == 0
            and is_tagged_int(thread) == 0
            and load_i32(thread, 8) == PY_TYPE_VIRTUAL_THREAD
            and ptr_eq(load_ptr(thread, 64), node) != 0
        ):
            store_ptr(thread, 64, null())
            wait_kind = load_i64(thread, 120)
            if wait_kind == 2 or wait_kind == 3:
                store_i64(thread, 120, 0)
        _io_release(node)
        node = after
    while ptr_is_null(reserved) == 0:
        after = load_ptr(reserved, 8)
        _ready_release(reserved)
        reserved = after
    if load_i32(global_addr("pcc_vthread_waitset_ready_py"), 0) != 0:
        pcc_io_waitset_remove(global_addr("pcc_vthread_waitset_py"), fd)
        _io_interrupt_locked()
    return 0


def _io_add(vthread, fd: int, events: int, deadline: int) -> int:
    node = _io_alloc()
    if ptr_is_null(node):
        return -1
    handle = pcc_gc_scheduler_root_register_handle(node)
    if ptr_is_null(handle):
        free(node)
        return -1
    store_ptr(node, 40, handle)
    pcc_gc_store_root(node, vthread)
    store_i64(node, 8, fd)
    store_i64(node, 16, events)
    store_i64(node, 24, deadline)
    store_ptr(node, 32, global_load_ptr("pcc_vthread_io_head_py"))
    global_store_ptr("pcc_vthread_io_head_py", node)
    store_ptr(vthread, 64, node)
    if events & 4 != 0:
        store_i64(vthread, 120, 3)
    else:
        store_i64(vthread, 120, 2)
    _counter_add(global_addr("pcc_vthread_io_count_py"), 1)
    if _io_refresh(fd) != 0:
        _io_cancel(vthread)
        return -1
    _effect(1, 3, 1, -1)
    _effect(10, 3, 0, load_i64(vthread, 32))
    return 0


@c_abi_export("py_virtual_thread_new")
def py_virtual_thread_new(continuation):
    vthread = pcc_gc_alloc(192, PY_TYPE_VIRTUAL_THREAD, 0)
    if ptr_is_null(vthread):
        return null()
    store_ptr(vthread, 16, null())
    store_ptr(vthread, 24, null())
    store_i64(vthread, 32, 0)
    store_i64(vthread, 40, 0)
    store_i64(vthread, 48, 0)
    store_ptr(vthread, 56, null())
    store_ptr(vthread, 64, null())
    store_ptr(vthread, 72, null())
    store_i64(vthread, 80, 0)
    store_ptr(vthread, 88, null())
    store_ptr(vthread, 96, null())
    store_ptr(vthread, 104, null())
    store_ptr(vthread, 112, null())
    store_i64(vthread, 120, 0)
    store_i64(vthread, 128, 0)
    store_ptr(vthread, 136, null())
    store_ptr(vthread, 144, null())
    store_ptr(vthread, 152, null())
    store_ptr(vthread, 160, null())
    store_ptr(vthread, 168, null())
    store_i64(vthread, 176, -1)
    store_i64(vthread, 184, -1)
    if ptr_is_null(continuation):
        continuation = global_load_ptr("py_None")
    pcc_gc_store_ptr(vthread, ptr_add(vthread, 16), continuation)
    py_gc_track(vthread)
    return vthread


@c_abi_export("py_virtual_thread_start")
def py_virtual_thread_start(vthread) -> int:
    vthread = _checked(vthread)
    if ptr_is_null(vthread) or load_i64(vthread, 32) == 4:
        return -1
    if _scheduler_lock() != 0:
        return -1
    if load_i64(vthread, 32) == 4:
        _scheduler_unlock()
        return -1
    if load_i64(vthread, 120) >= 4 or ptr_is_null(load_ptr(vthread, 104)) == 0:
        _scheduler_unlock()
        return -1
    _timer_cancel(vthread)
    _io_cancel(vthread)
    state = load_i64(vthread, 32)
    if state == 0 or state == 3:
        store_i64(vthread, 32, 1)
    result = _enqueue(vthread)
    if result == 0:
        _effect(4, 0, 0, load_i64(vthread, 32))
    _scheduler_unlock()
    return result


@c_abi_export("py_virtual_thread_park")
def py_virtual_thread_park(vthread) -> int:
    vthread = _checked(vthread)
    if ptr_is_null(vthread) or load_i64(vthread, 32) == 4:
        return -1
    if _scheduler_lock() != 0:
        return -1
    if load_i64(vthread, 32) == 4:
        _scheduler_unlock()
        return -1
    if load_i64(vthread, 120) != 0:
        _scheduler_unlock()
        return -1
    store_i64(vthread, 32, 3)
    _effect(5, 0, 0, 3)
    _scheduler_unlock()
    return 0


@c_abi_export("py_virtual_thread_unpark")
def py_virtual_thread_unpark(vthread) -> int:
    vthread = _checked(vthread)
    if ptr_is_null(vthread) or load_i64(vthread, 32) == 4:
        return -1
    if _scheduler_lock() != 0:
        return -1
    if load_i64(vthread, 32) == 4:
        _scheduler_unlock()
        return -1
    if load_i64(vthread, 120) >= 4 or ptr_is_null(load_ptr(vthread, 104)) == 0:
        _scheduler_unlock()
        return -1
    _timer_cancel(vthread)
    _io_cancel(vthread)
    result = _make_ready(vthread)
    if result == 0:
        _effect(6, 0, 0, load_i64(vthread, 32))
    _scheduler_unlock()
    return result


@c_abi_export("py_virtual_thread_sleep")
def py_virtual_thread_sleep(vthread, delay_ms: int) -> int:
    vthread = _checked(vthread)
    if ptr_is_null(vthread) or load_i64(vthread, 32) == 4:
        return -1
    if _scheduler_lock() != 0:
        return -1
    if load_i64(vthread, 32) == 4:
        _scheduler_unlock()
        return -1
    if load_i64(vthread, 120) >= 4 or ptr_is_null(load_ptr(vthread, 104)) == 0:
        _scheduler_unlock()
        return -1
    _io_cancel(vthread)
    if delay_ms <= 0:
        _timer_cancel(vthread)
        result = _make_ready(vthread)
        if result == 0:
            _effect(6, 0, 0, load_i64(vthread, 32))
        _scheduler_unlock()
        return result
    store_i64(vthread, 32, 3)
    result = _timer_add(vthread, _now_ms() + delay_ms)
    _scheduler_unlock()
    return result


@c_abi_export("py_virtual_thread_cancel_timer")
def py_virtual_thread_cancel_timer(vthread) -> int:
    vthread = _checked(vthread)
    if ptr_is_null(vthread):
        return -1
    if _scheduler_lock() != 0:
        return -1
    result = _timer_cancel(vthread)
    _scheduler_unlock()
    return result


@c_abi_export("py_virtual_thread_poll_timers")
def py_virtual_thread_poll_timers() -> int:
    if _scheduler_lock() != 0:
        return -1
    now = _now_ms()
    woken = 0
    allocation_failed = 0
    while True:
        node = global_load_ptr("pcc_vthread_timer_head_py")
        if ptr_is_null(node) or load_i64(node, 8) > now:
            break
        thread = pcc_gc_load_ptr(null(), node)
        valid = (
            ptr_is_null(thread) == 0
            and is_tagged_int(thread) == 0
            and load_i32(thread, 8) == PY_TYPE_VIRTUAL_THREAD
            and ptr_eq(load_ptr(thread, 56), node) != 0
            and load_i64(thread, 32) == 3
            and load_i64(thread, 120) == 1
        )
        ready_node = null()
        if valid:
            # Keep the timer node/root linked until the replacement ready root
            # exists. On OOM the old wait remains live for the next poll.
            ready_node = _ready_reserve(thread)
            if ptr_is_null(ready_node):
                allocation_failed = 1
                break
        global_store_ptr("pcc_vthread_timer_head_py", load_ptr(node, 16))
        if valid:
            store_ptr(thread, 56, null())
            store_i64(thread, 120, 0)
            _ready_commit(thread, ready_node)
            woken = woken + 1
            _effect(9, 2, 0, load_i64(thread, 32))
        elif (
            ptr_is_null(thread) == 0
            and is_tagged_int(thread) == 0
            and load_i32(thread, 8) == PY_TYPE_VIRTUAL_THREAD
            and ptr_eq(load_ptr(thread, 56), node) != 0
        ):
            store_ptr(thread, 56, null())
            if load_i64(thread, 120) == 1:
                store_i64(thread, 120, 0)
        _timer_release(node)
    _scheduler_unlock()
    if allocation_failed != 0:
        return -1
    return woken


@c_abi_export("py_virtual_thread_timer_count")
def py_virtual_thread_timer_count() -> int:
    if _scheduler_lock() != 0:
        return -1
    count = 0
    node = global_load_ptr("pcc_vthread_timer_head_py")
    while ptr_is_null(node) == 0:
        count = count + 1
        node = load_ptr(node, 16)
    _scheduler_unlock()
    return count


@c_abi_export("py_virtual_thread_io_resource_register")
def py_virtual_thread_io_resource_register(fd: int) -> int:
    if fd < 0 or _scheduler_lock() != 0:
        return -1
    if ptr_is_null(_io_resource_find(fd)) == 0:
        _scheduler_unlock()
        return -1
    generation = _counter(
        global_addr("pcc_vthread_io_resource_generation_py")
    )
    if generation >= 9223372036854775807:
        _scheduler_unlock()
        return -1
    generation = generation + 1
    node = malloc(24)
    if ptr_is_null(node):
        _scheduler_unlock()
        return -1
    store_i64(node, 0, fd)
    store_i64(node, 8, generation)
    store_ptr(node, 16, global_load_ptr("pcc_vthread_io_resource_head_py"))
    global_store_ptr("pcc_vthread_io_resource_head_py", node)
    _counter_set(
        global_addr("pcc_vthread_io_resource_generation_py"), generation
    )
    _scheduler_unlock()
    return generation


@c_abi_export("py_virtual_thread_io_resource_generation")
def py_virtual_thread_io_resource_generation(fd: int) -> int:
    if fd < 0 or _scheduler_lock() != 0:
        py_raise_owned(py_exc_new(14, cstr("TCP descriptor is not open")))
        return -1
    node = _io_resource_find(fd)
    generation = -1
    if ptr_is_null(node) == 0:
        generation = load_i64(node, 8)
    _scheduler_unlock()
    if generation < 0:
        py_raise_owned(py_exc_new(14, cstr("TCP descriptor is not open")))
    return generation


@c_abi_export("py_virtual_thread_io_resource_operation_begin")
def py_virtual_thread_io_resource_operation_begin(
    fd: int, generation: int
) -> int:
    if fd < 0 or generation <= 0 or _scheduler_lock() != 0:
        py_raise_owned(py_exc_new(14, cstr("TCP descriptor was closed")))
        return -1
    node = _io_resource_find(fd)
    if ptr_is_null(node) or load_i64(node, 8) != generation:
        _scheduler_unlock()
        py_raise_owned(py_exc_new(14, cstr("TCP descriptor was closed")))
        return -1
    # Success deliberately retains the scheduler mutex through one
    # nonblocking observation syscall.
    return 0


@c_abi_export("py_virtual_thread_io_resource_operation_end")
def py_virtual_thread_io_resource_operation_end() -> None:
    _scheduler_unlock()


@c_abi_export("py_virtual_thread_io_resource_close_begin")
def py_virtual_thread_io_resource_close_begin(fd: int) -> int:
    if fd < 0 or _scheduler_lock() != 0:
        return -1
    previous = null()
    node = global_load_ptr("pcc_vthread_io_resource_head_py")
    while ptr_is_null(node) == 0 and load_i64(node, 0) != fd:
        previous = node
        node = load_ptr(node, 16)
    if ptr_is_null(node):
        _scheduler_unlock()
        return -1
    if _io_close_waiters_locked(fd) != 0:
        _scheduler_unlock()
        return -1
    after = load_ptr(node, 16)
    if ptr_is_null(previous):
        global_store_ptr("pcc_vthread_io_resource_head_py", after)
    else:
        store_ptr(previous, 16, after)
    free(node)
    # Success deliberately retains the scheduler mutex until close completes.
    return 0


@c_abi_export("py_virtual_thread_block_on_fd_generation")
def py_virtual_thread_block_on_fd_generation(
    vthread,
    fd: int,
    generation: int,
    events: int,
    timeout_ms: int,
) -> int:
    vthread = _checked(vthread)
    if (
        ptr_is_null(vthread)
        or load_i64(vthread, 32) == 4
        or fd < 0
        or generation <= 0
    ):
        return -1
    if events == 0:
        events = 1
    if _scheduler_lock() != 0:
        return -1
    resource = _io_resource_find(fd)
    if ptr_is_null(resource) or load_i64(resource, 8) != generation:
        _scheduler_unlock()
        py_raise_owned(py_exc_new(14, cstr("TCP descriptor was closed")))
        return -1
    if load_i64(vthread, 32) == 4:
        _scheduler_unlock()
        return -1
    if load_i64(vthread, 120) >= 4 or ptr_is_null(load_ptr(vthread, 104)) == 0:
        _scheduler_unlock()
        return -1
    ready = poll_fd(fd, events, 0)
    if ready < 0:
        _scheduler_unlock()
        return -1
    _timer_cancel(vthread)
    _io_cancel(vthread)
    if ready != 0:
        if load_i64(vthread, 32) == 2:
            _scheduler_unlock()
            return 1
        if _make_ready(vthread) != 0:
            _scheduler_unlock()
            return -1
        _scheduler_unlock()
        return 1
    deadline = -1
    if timeout_ms >= 0:
        deadline = _now_ms() + timeout_ms
    store_i64(vthread, 32, 3)
    result = _io_add(vthread, fd, events, deadline)
    _scheduler_unlock()
    return result


@c_abi_export("py_virtual_thread_block_on_fd")
def py_virtual_thread_block_on_fd(
    vthread, fd: int, events: int, timeout_ms: int
) -> int:
    vthread = _checked(vthread)
    if ptr_is_null(vthread) or load_i64(vthread, 32) == 4 or fd < 0:
        return -1
    if events == 0:
        events = 1
    if _scheduler_lock() != 0:
        return -1
    if load_i64(vthread, 32) == 4:
        _scheduler_unlock()
        return -1
    if load_i64(vthread, 120) >= 4 or ptr_is_null(load_ptr(vthread, 104)) == 0:
        _scheduler_unlock()
        return -1
    ready = poll_fd(fd, events, 0)
    if ready < 0:
        _scheduler_unlock()
        return -1
    _timer_cancel(vthread)
    _io_cancel(vthread)
    if ready != 0:
        if load_i64(vthread, 32) == 2:
            _scheduler_unlock()
            return 1
        if _make_ready(vthread) != 0:
            _scheduler_unlock()
            return -1
        _scheduler_unlock()
        return 1
    deadline = -1
    if timeout_ms >= 0:
        deadline = _now_ms() + timeout_ms
    store_i64(vthread, 32, 3)
    result = _io_add(vthread, fd, events, deadline)
    _scheduler_unlock()
    return result


def _result_bits(result, fd: int) -> int:
    ready = load_ptr(result, 0)
    length = load_i64(result, 8)
    bits = 0
    index = 0
    while index < length:
        entry = ptr_add(ready, index * 16)
        if load_i64(entry, 0) == fd:
            bits = bits | load_i64(entry, 8)
        index = index + 1
    return bits


@c_abi_export("py_virtual_thread_poll_io")
def py_virtual_thread_poll_io(timeout_ms: int) -> int:
    if _scheduler_lock() != 0:
        return -1
    # Ready-only work has no fd state to observe. Check under the same lock
    # used by registration; a later registration is seen by the next step or
    # the idle driver's blocking poll. Avoid allocating/polling an empty OS
    # waitset on every virtual-thread resume.
    if timeout_ms == 0 and ptr_is_null(global_load_ptr("pcc_vthread_io_head_py")):
        _scheduler_unlock()
        return 0
    if _waitset_init() != 0:
        _scheduler_unlock()
        return -1
    backend = load_i32(global_addr("pcc_vthread_waitset_backend_py"), 0)
    result = stack_alloc(32)
    now = _now_ms()
    if backend == 0:
        node = global_load_ptr("pcc_vthread_io_head_py")
        used_timeout = 0
        while ptr_is_null(node) == 0:
            wait = 0
            if used_timeout == 0 and timeout_ms > 0:
                wait = timeout_ms
                used_timeout = 1
            revents = poll_fd(load_i64(node, 8), load_i64(node, 16), wait)
            if revents < 0:
                _scheduler_unlock()
                return -1
            pcc_io_waitset_set_ready(
                global_addr("pcc_vthread_waitset_py"), load_i64(node, 8), revents
            )
            node = load_ptr(node, 32)
        if pcc_io_waitset_wait_until(
            global_addr("pcc_vthread_waitset_py"), now, now, result
        ) != 0:
            _scheduler_unlock()
            return -1
    else:
        if load_i32(global_addr("pcc_vthread_wait_active_py"), 0) != 0:
            _scheduler_unlock()
            return 0
        wait_deadline = now
        if timeout_ms < 0:
            wait_deadline = -1
        else:
            wait_deadline = now + timeout_ms
        batch = stack_alloc(64)
        if pcc_io_waitset_wait_prepare(
            global_addr("pcc_vthread_waitset_py"),
            now,
            wait_deadline,
            batch,
        ) != 0:
            _scheduler_unlock()
            return -1
        store_i32(global_addr("pcc_vthread_wait_active_py"), 0, 1)
        _scheduler_unlock()

        pcc_io_waitset_wait_block(
            global_addr("pcc_vthread_waitset_py"), batch
        )

        if _scheduler_lock() != 0:
            store_i32(global_addr("pcc_vthread_wait_active_py"), 0, 0)
            pcc_io_waitset_wait_discard(batch)
            return -1
        store_i32(global_addr("pcc_vthread_wait_active_py"), 0, 0)
        if pcc_io_waitset_wait_finish(
            global_addr("pcc_vthread_waitset_py"), batch, result
        ) != 0:
            _scheduler_unlock()
            return -1
    now = _now_ms()
    woken = 0
    refresh_failed = 0
    allocation_failed = 0
    previous = null()
    node = global_load_ptr("pcc_vthread_io_head_py")
    while ptr_is_null(node) == 0:
        after = load_ptr(node, 32)
        bits = _result_bits(result, load_i64(node, 8))
        interest = load_i64(node, 16)
        if interest == 0:
            interest = 1
        hit = bits & (interest | 56)
        expired = load_i64(node, 24) >= 0 and load_i64(node, 24) <= now
        if hit != 0 or expired:
            thread = pcc_gc_load_ptr(null(), node)
            valid = (
                ptr_is_null(thread) == 0
                and is_tagged_int(thread) == 0
                and load_i32(thread, 8) == PY_TYPE_VIRTUAL_THREAD
                and ptr_eq(load_ptr(thread, 64), node) != 0
                and load_i64(thread, 32) == 3
                and (
                    load_i64(thread, 120) == 2
                    or load_i64(thread, 120) == 3
                )
            )
            ready_node = null()
            if valid:
                # Reserve allocation and root ownership before unlinking this
                # IO node. If reservation fails, re-arm the still-live wait.
                ready_node = _ready_reserve(thread)
                if ptr_is_null(ready_node):
                    allocation_failed = 1
                    previous = node
                    node = after
                    continue
            if ptr_is_null(previous):
                global_store_ptr("pcc_vthread_io_head_py", after)
            else:
                store_ptr(previous, 32, after)
            _counter_add(global_addr("pcc_vthread_io_count_py"), -1)
            if valid:
                store_ptr(thread, 64, null())
                store_i64(thread, 120, 0)
                _ready_commit(thread, ready_node)
                woken = woken + 1
                _effect(11, 3, 0, load_i64(thread, 32))
            elif (
                ptr_is_null(thread) == 0
                and is_tagged_int(thread) == 0
                and load_i32(thread, 8) == PY_TYPE_VIRTUAL_THREAD
                and ptr_eq(load_ptr(thread, 64), node) != 0
            ):
                store_ptr(thread, 64, null())
                wait_kind = load_i64(thread, 120)
                if wait_kind == 2 or wait_kind == 3:
                    store_i64(thread, 120, 0)
            _io_release(node)
        else:
            previous = node
        node = after
    # wait_finish consumes aggregate one-shot registrations. Re-arm every fd
    # only after the result scratch has been fully read; refresh may grow or
    # compact waitset storage and must not invalidate the arrays mid-walk.
    ready_events = load_ptr(result, 0)
    ready_length = load_i64(result, 8)
    index = 0
    while index < ready_length:
        if _io_refresh(load_i64(ready_events, index * 16)) != 0:
            refresh_failed = 1
        index = index + 1
    timed_out = load_ptr(result, 16)
    timeout_length = load_i64(result, 24)
    index = 0
    while index < timeout_length:
        if _io_refresh(load_i64(timed_out, index * 8)) != 0:
            refresh_failed = 1
        index = index + 1
    _scheduler_unlock()
    if refresh_failed != 0 or allocation_failed != 0:
        return -1
    return woken


@c_abi_export("py_virtual_thread_io_wait_count")
def py_virtual_thread_io_wait_count() -> int:
    if _scheduler_lock() != 0:
        return -1
    count = _counter(global_addr("pcc_vthread_io_count_py"))
    _scheduler_unlock()
    return count


@c_abi_export("py_virtual_thread_io_wait_active")
def py_virtual_thread_io_wait_active() -> int:
    if _scheduler_lock() != 0:
        return 0
    active = load_i32(global_addr("pcc_vthread_wait_active_py"), 0)
    _scheduler_unlock()
    if active != 0:
        return 1
    return 0


@c_abi_export("py_virtual_thread_io_backend")
def py_virtual_thread_io_backend() -> int:
    if _scheduler_lock() != 0:
        return -1
    if _waitset_init() != 0:
        _scheduler_unlock()
        return -1
    backend = load_i32(global_addr("pcc_vthread_waitset_backend_py"), 0)
    _scheduler_unlock()
    return backend


@c_abi_export("py_virtual_thread_poll_ready")
def py_virtual_thread_poll_ready():
    if _scheduler_lock() != 0:
        return null()
    ready = _dequeue()
    _scheduler_unlock()
    return ready


@c_abi_export("py_virtual_thread_ready_count")
def py_virtual_thread_ready_count() -> int:
    if _scheduler_lock() != 0:
        return -1
    count = _counter(global_addr("pcc_vthread_ready_count_py"))
    _scheduler_unlock()
    return count


@c_abi_export("py_virtual_thread_node_pool_stat")
def py_virtual_thread_node_pool_stat(family: int, metric: int) -> int:
    if metric < 0 or metric > 2:
        return -1
    if family == 0:
        if metric == 0:
            return _counter(global_addr("pcc_vthread_ready_alloc_py"))
        if metric == 1:
            return _counter(global_addr("pcc_vthread_ready_reuse_py"))
        return _counter(global_addr("pcc_vthread_ready_cached_py"))
    elif family == 1:
        if metric == 0:
            return _counter(global_addr("pcc_vthread_waiter_alloc_py"))
        if metric == 1:
            return _counter(global_addr("pcc_vthread_waiter_reuse_py"))
        return _counter(global_addr("pcc_vthread_waiter_cached_py"))
    elif family == 2:
        if metric == 0:
            return _counter(global_addr("pcc_vthread_timer_alloc_py"))
        if metric == 1:
            return _counter(global_addr("pcc_vthread_timer_reuse_py"))
        return _counter(global_addr("pcc_vthread_timer_cached_py"))
    elif family == 3:
        if metric == 0:
            return _counter(global_addr("pcc_vthread_io_alloc_py"))
        if metric == 1:
            return _counter(global_addr("pcc_vthread_io_reuse_py"))
        return _counter(global_addr("pcc_vthread_io_cached_py"))
    else:
        return -1


@c_abi_export("py_virtual_thread_carrier_count")
def py_virtual_thread_carrier_count() -> int:
    return _counter(global_addr("pcc_vthread_carrier_count_py"))


@c_abi_export("py_virtual_thread_carrier_steal_count")
def py_virtual_thread_carrier_steal_count() -> int:
    return _counter(global_addr("pcc_vthread_carrier_steal_count_py"))


@c_abi_export("py_virtual_thread_carrier_queue_count")
def py_virtual_thread_carrier_queue_count() -> int:
    return _counter(global_addr("pcc_vthread_carrier_queue_count_py"))


@c_abi_export("py_virtual_thread_carrier_queue_depth")
def py_virtual_thread_carrier_queue_depth(index: int) -> int:
    if _scheduler_lock() != 0:
        return -1
    carrier_count = _counter(
        global_addr("pcc_vthread_carrier_queue_count_py")
    )
    if index < 0 or index >= carrier_count:
        _scheduler_unlock()
        return -1
    depth = 0
    node = load_ptr(_carrier_head_slot(index), 0)
    while ptr_is_null(node) == 0:
        depth = depth + 1
        node = load_ptr(node, 8)
    _scheduler_unlock()
    return depth


@c_abi_export("py_virtual_thread_carrier_failure_count")
def py_virtual_thread_carrier_failure_count() -> int:
    return _counter(global_addr("pcc_vthread_carrier_failures_py"))


@c_abi_export("py_virtual_thread_run_once")
def py_virtual_thread_run_once() -> int:
    py_virtual_thread_poll_timers()
    py_virtual_thread_poll_io(0)
    ready = py_virtual_thread_poll_ready()
    if ptr_is_null(ready):
        return 0
    continuation = pcc_gc_load_ptr(ready, ptr_add(ready, 16))
    continuation_kind: int = 0
    if ptr_is_null(continuation) == 0 and is_tagged_int(continuation) == 0:
        continuation_kind = load_i32(continuation, 8)
    if continuation_kind == PY_TYPE_CONTINUATION or continuation_kind == PY_TYPE_GEN:
        resume = null()
        if continuation_kind == PY_TYPE_CONTINUATION:
            resume = py_continuation_resume_pc(continuation)
        if continuation_kind == PY_TYPE_GEN or ptr_is_null(resume) == 0:
            saved = global_load_ptr("pcc_current_virtual_thread_py")
            global_store_ptr("pcc_current_virtual_thread_py", ready)
            rc = 0
            if continuation_kind == PY_TYPE_GEN:
                # Match the owned reference previously returned by slot zero.
                py_incref(continuation)
                rc = _resume_generator_owned(ready, continuation)
            elif py_continuation_resume_abi(continuation) == 1:
                rc = call_i64_ptr2(resume, ready, continuation)
            else:
                call_void_ptr0(resume)
            global_store_ptr("pcc_current_virtual_thread_py", saved)
            failure = py_current_exception()
            if ptr_is_null(failure) == 0:
                fail_result = py_virtual_thread_fail(ready, failure)
                if fail_result == 0:
                    py_clear_exception()
                py_decref(ready)
                if fail_result == 0:
                    return 1
                return -1
            if rc != 0:
                py_decref(ready)
                return -1
    if load_i64(ready, 32) == 2:
        if py_virtual_thread_complete(ready, global_load_ptr("py_None")) != 0:
            py_decref(ready)
            return -1
    py_decref(ready)
    return 1


@c_abi_export("py_virtual_thread_run_until_idle")
def py_virtual_thread_run_until_idle(max_steps: int) -> int:
    if max_steps <= 0:
        return 0
    ran = 0
    steps = 0
    while steps < max_steps:
        step = py_virtual_thread_run_once()
        if step < 0:
            return -1
        if step == 0:
            # Park the carrier in the existing kqueue/epoll owner when only
            # fd-blocked sequential work remains.  This is not a sleep scan:
            # the waitset owns the interrupt wake and the earliest deadline.
            if py_virtual_thread_io_wait_count() <= 0:
                break
            if py_virtual_thread_poll_io(-1) < 0:
                return -1
            continue
        ran = ran + step
        steps = steps + 1
    return ran


def _carrier_pool_claim(run) -> int:
    maximum = atomic_load_i64(run, 0, "acquire")
    claimed = atomic_load_i64(run, 8, "acquire")
    while claimed < maximum:
        observed = atomic_cas_i64(
            run, 8, claimed, claimed + 1, "acq_rel", "acquire"
        )
        if observed == claimed:
            return 1
        claimed = observed
    return 0


@c_abi_export("pcc_vthread_carrier_pool_worker_py")
def _carrier_pool_worker(worker):
    run = load_ptr(worker, 0)
    carrier_index = load_i64(worker, 8)
    saved_carrier = load_i32(
        global_addr("pcc_current_virtual_thread_carrier_py"), 0
    )
    store_i32(
        global_addr("pcc_current_virtual_thread_carrier_py"),
        0,
        carrier_index,
    )
    _counter_add(global_addr("pcc_vthread_carrier_count_py"), 1)
    _counter_add(ptr_add(run, 40), 1)
    while atomic_load_i64(run, 48, "acquire") == 0:
        pcc_thread_safepoint()
    while _carrier_pool_claim(run) != 0:
        step = py_virtual_thread_run_once()
        if step < 0:
            _counter_add(ptr_add(run, 24), 1)
            _counter_add(global_addr("pcc_vthread_carrier_failures_py"), 1)
            break
        if step == 0:
            break
        _counter_add(ptr_add(run, 16), step)
    _counter_add(global_addr("pcc_vthread_carrier_count_py"), -1)
    store_i32(
        global_addr("pcc_current_virtual_thread_carrier_py"),
        0,
        saved_carrier,
    )
    return null()


@c_abi_export("py_virtual_thread_run_carrier_pool")
def py_virtual_thread_run_carrier_pool(carrier_count: int, max_steps: int) -> int:
    if max_steps <= 0:
        return 0
    if carrier_count <= 1 or pcc_threads_enabled() == 0:
        return py_virtual_thread_run_until_idle(max_steps)
    if carrier_count > 64:
        carrier_count = 64
    handles = calloc(carrier_count, 8)
    workers = calloc(carrier_count, 16)
    run = calloc(1, 56)
    if ptr_is_null(handles) or ptr_is_null(workers) or ptr_is_null(run):
        free(run)
        free(workers)
        free(handles)
        return -1
    store_i64(run, 0, max_steps)
    store_i64(run, 32, carrier_count)

    if _scheduler_lock() != 0:
        free(run)
        free(workers)
        free(handles)
        return -1
    if (
        _counter(global_addr("pcc_vthread_persistent_pool_running_py")) != 0
        or _counter(global_addr("pcc_vthread_bounded_pool_running_py")) != 0
        or _counter(
            global_addr("pcc_vthread_persistent_cleanup_active_py")
        ) != 0
    ):
        _scheduler_unlock()
        free(run)
        free(workers)
        free(handles)
        return -1
    _counter_set(global_addr("pcc_vthread_bounded_pool_running_py"), 1)
    opened = _carrier_queues_open(carrier_count)
    _scheduler_unlock()
    if opened != 0:
        _counter_set(global_addr("pcc_vthread_bounded_pool_running_py"), 0)
        free(run)
        free(workers)
        free(handles)
        return -1

    started = 0
    index = 0
    while index < carrier_count:
        worker = ptr_add(workers, index * 16)
        store_ptr(worker, 0, run)
        store_i64(worker, 8, index)
        if pcc_thread_start(
            ptr_add(handles, index * 8),
            function_addr("pcc_vthread_carrier_pool_worker_py"),
            worker,
        ) != 0:
            _counter_add(ptr_add(run, 24), 1)
            _counter_add(global_addr("pcc_vthread_carrier_failures_py"), 1)
            break
        started = started + 1
        index = index + 1

    # Release the start barrier only after all possible carriers have been
    # created, so one early carrier cannot drain the bounded run by itself.
    atomic_store_i64(run, 32, started, "release")
    while atomic_load_i64(run, 40, "acquire") < started:
        pcc_thread_safepoint()
    atomic_store_i64(run, 48, 1, "release")
    index = 0
    while index < started:
        if pcc_thread_join(load_ptr(handles, index * 8), null()) != 0:
            _counter_add(ptr_add(run, 24), 1)
            _counter_add(global_addr("pcc_vthread_carrier_failures_py"), 1)
        index = index + 1

    if _scheduler_lock() == 0:
        _carrier_queues_close()
        _counter_set(global_addr("pcc_vthread_bounded_pool_running_py"), 0)
        _scheduler_unlock()
    else:
        _counter_set(global_addr("pcc_vthread_bounded_pool_running_py"), 0)
    failures = atomic_load_i64(run, 24, "acquire")
    ran = atomic_load_i64(run, 16, "acquire")
    free(run)
    free(workers)
    free(handles)
    if started == 0 or failures != 0:
        return -1
    return ran


@c_abi_export("pcc_vthread_persistent_carrier_worker_py")
def _persistent_carrier_worker(arg):
    carrier_index = load_i64(arg, 0)
    saved_carrier = load_i32(
        global_addr("pcc_current_virtual_thread_carrier_py"), 0
    )
    store_i32(
        global_addr("pcc_current_virtual_thread_carrier_py"),
        0,
        carrier_index,
    )
    _counter_add(global_addr("pcc_vthread_carrier_count_py"), 1)
    while (
        _counter(global_addr("pcc_vthread_persistent_pool_stop_py")) == 0
    ):
        step = py_virtual_thread_run_once()
        if step < 0:
            _counter_add(
                global_addr("pcc_vthread_persistent_pool_failures_py"), 1
            )
            _counter_add(global_addr("pcc_vthread_carrier_failures_py"), 1)
            break
        if step == 0 and _scheduler_lock() == 0:
            if (
                _counter(
                    global_addr("pcc_vthread_persistent_pool_stop_py")
                ) == 0
                and _counter(global_addr("pcc_vthread_ready_count_py")) == 0
            ):
                pcc_cond_timedwait_ms(
                    _scheduler_cond(), _scheduler_mutex(), 1
                )
            _scheduler_unlock()
            pcc_thread_safepoint()
    _counter_add(global_addr("pcc_vthread_carrier_count_py"), -1)
    store_i32(
        global_addr("pcc_current_virtual_thread_carrier_py"),
        0,
        saved_carrier,
    )
    return null()


@c_abi_export("py_virtual_thread_carrier_pool_start")
def py_virtual_thread_carrier_pool_start(carrier_count: int) -> int:
    if carrier_count <= 0 or pcc_threads_enabled() == 0:
        return 0
    if carrier_count > 64:
        carrier_count = 64
    handles = calloc(carrier_count, 8)
    args = calloc(carrier_count, 8)
    if ptr_is_null(handles) or ptr_is_null(args):
        free(args)
        free(handles)
        return -1
    if _scheduler_lock() != 0:
        free(args)
        free(handles)
        return -1
    if _counter(global_addr("pcc_vthread_persistent_pool_running_py")) != 0:
        existing = _counter(
            global_addr("pcc_vthread_persistent_carrier_count_py")
        )
        _scheduler_unlock()
        free(args)
        free(handles)
        return existing
    if _counter(global_addr("pcc_vthread_bounded_pool_running_py")) != 0:
        _scheduler_unlock()
        free(args)
        free(handles)
        return -1
    if (
        _counter(global_addr("pcc_vthread_persistent_cleanup_active_py"))
        != 0
    ):
        _scheduler_unlock()
        free(args)
        free(handles)
        return -1
    _counter_set(
        global_addr("pcc_vthread_persistent_cleanup_active_py"), 1
    )
    if _waitset_init() != 0:
        _counter_set(
            global_addr("pcc_vthread_persistent_cleanup_active_py"), 0
        )
        _scheduler_unlock()
        free(args)
        free(handles)
        return -1
    if _carrier_queues_open(carrier_count) != 0:
        _waitset_dispose_locked()
        _counter_set(
            global_addr("pcc_vthread_persistent_cleanup_active_py"), 0
        )
        _scheduler_unlock()
        free(args)
        free(handles)
        return -1
    global_store_ptr("pcc_vthread_persistent_handles_py", handles)
    global_store_ptr("pcc_vthread_persistent_args_py", args)
    _counter_set(global_addr("pcc_vthread_persistent_pool_stop_py"), 0)
    _counter_set(global_addr("pcc_vthread_persistent_pool_failures_py"), 0)
    _counter_set(global_addr("pcc_vthread_persistent_carrier_count_py"), 0)
    _counter_set(global_addr("pcc_vthread_persistent_joined_count_py"), 0)
    _counter_set(global_addr("pcc_vthread_persistent_pool_running_py"), 1)
    _scheduler_unlock()

    started = 0
    index = 0
    while index < carrier_count:
        arg = ptr_add(args, index * 8)
        store_i64(arg, 0, index)
        if pcc_thread_start(
            ptr_add(handles, index * 8),
            function_addr("pcc_vthread_persistent_carrier_worker_py"),
            arg,
        ) != 0:
            _counter_add(global_addr("pcc_vthread_carrier_failures_py"), 1)
            break
        started = started + 1
        _counter_set(
            global_addr("pcc_vthread_persistent_carrier_count_py"), started
        )
        index = index + 1
    if started == carrier_count:
        _counter_set(
            global_addr("pcc_vthread_persistent_cleanup_active_py"), 0
        )
        return started

    _counter_set(global_addr("pcc_vthread_persistent_pool_stop_py"), 1)
    if _scheduler_lock() != 0:
        _counter_set(
            global_addr("pcc_vthread_persistent_cleanup_active_py"), 0
        )
        return -1
    interrupt_result = _io_interrupt_locked()
    pcc_cond_broadcast(_scheduler_cond())
    _scheduler_unlock()
    if interrupt_result != 0:
        _counter_set(
            global_addr("pcc_vthread_persistent_cleanup_active_py"), 0
        )
        return -1
    index = 0
    join_failed = 0
    while index < started:
        if pcc_thread_join(load_ptr(handles, index * 8), null()) == 0:
            store_ptr(handles, index * 8, null())
            _counter_add(
                global_addr("pcc_vthread_persistent_joined_count_py"), 1
            )
        else:
            _counter_add(global_addr("pcc_vthread_carrier_failures_py"), 1)
            join_failed = 1
        index = index + 1
    if join_failed != 0:
        _counter_set(
            global_addr("pcc_vthread_persistent_cleanup_active_py"), 0
        )
        return -1
    if _scheduler_lock() != 0:
        _counter_set(
            global_addr("pcc_vthread_persistent_cleanup_active_py"), 0
        )
        return -1
    if _waitset_dispose_locked() != 0:
        _counter_set(
            global_addr("pcc_vthread_persistent_cleanup_active_py"), 0
        )
        _scheduler_unlock()
        return -1
    _carrier_queues_close()
    global_store_ptr("pcc_vthread_persistent_handles_py", null())
    global_store_ptr("pcc_vthread_persistent_args_py", null())
    _counter_set(global_addr("pcc_vthread_persistent_carrier_count_py"), 0)
    _counter_set(global_addr("pcc_vthread_persistent_pool_running_py"), 0)
    _counter_set(global_addr("pcc_vthread_persistent_pool_stop_py"), 0)
    _counter_set(global_addr("pcc_vthread_persistent_pool_failures_py"), 0)
    _counter_set(global_addr("pcc_vthread_persistent_joined_count_py"), 0)
    _counter_set(
        global_addr("pcc_vthread_persistent_cleanup_active_py"), 0
    )
    _scheduler_unlock()
    free(args)
    free(handles)
    return -1


@c_abi_export("py_virtual_thread_carrier_pool_stop")
def py_virtual_thread_carrier_pool_stop() -> int:
    if _counter(global_addr("pcc_vthread_persistent_pool_running_py")) == 0:
        return 0
    cleanup_observed = atomic_cas_i64(
        global_addr("pcc_vthread_persistent_cleanup_active_py"),
        0,
        0,
        1,
        "acq_rel",
        "acquire",
    )
    if cleanup_observed != 0:
        return -1
    _counter_set(global_addr("pcc_vthread_persistent_pool_stop_py"), 1)
    if _scheduler_lock() != 0:
        _counter_set(
            global_addr("pcc_vthread_persistent_cleanup_active_py"), 0
        )
        return -1
    interrupt_result = _io_interrupt_locked()
    pcc_cond_broadcast(_scheduler_cond())
    _scheduler_unlock()
    if interrupt_result != 0:
        _counter_set(
            global_addr("pcc_vthread_persistent_cleanup_active_py"), 0
        )
        return -1
    handles = global_load_ptr("pcc_vthread_persistent_handles_py")
    args = global_load_ptr("pcc_vthread_persistent_args_py")
    count = _counter(
        global_addr("pcc_vthread_persistent_carrier_count_py")
    )
    joined = 0
    join_failed = 0
    index = 0
    while index < count:
        if ptr_is_null(handles) == 0:
            handle = load_ptr(handles, index * 8)
            if ptr_is_null(handle) == 0:
                if pcc_thread_join(handle, null()) == 0:
                    store_ptr(handles, index * 8, null())
                    joined = joined + 1
                else:
                    _counter_add(
                        global_addr("pcc_vthread_carrier_failures_py"), 1
                    )
                    join_failed = 1
        index = index + 1
    _counter_add(
        global_addr("pcc_vthread_persistent_joined_count_py"), joined
    )
    if join_failed != 0:
        _counter_set(
            global_addr("pcc_vthread_persistent_cleanup_active_py"), 0
        )
        return -1
    if _scheduler_lock() != 0:
        _counter_set(
            global_addr("pcc_vthread_persistent_cleanup_active_py"), 0
        )
        return -1
    if _waitset_dispose_locked() != 0:
        _counter_set(
            global_addr("pcc_vthread_persistent_cleanup_active_py"), 0
        )
        _scheduler_unlock()
        return -1
    _carrier_queues_close()
    joined_total = _counter(
        global_addr("pcc_vthread_persistent_joined_count_py")
    )
    pool_failures = _counter(
        global_addr("pcc_vthread_persistent_pool_failures_py")
    )
    global_store_ptr("pcc_vthread_persistent_handles_py", null())
    global_store_ptr("pcc_vthread_persistent_args_py", null())
    _counter_set(global_addr("pcc_vthread_persistent_carrier_count_py"), 0)
    _counter_set(global_addr("pcc_vthread_persistent_pool_running_py"), 0)
    _counter_set(global_addr("pcc_vthread_persistent_pool_stop_py"), 0)
    _counter_set(global_addr("pcc_vthread_persistent_pool_failures_py"), 0)
    _counter_set(global_addr("pcc_vthread_persistent_joined_count_py"), 0)
    _counter_set(
        global_addr("pcc_vthread_persistent_cleanup_active_py"), 0
    )
    _scheduler_unlock()
    free(args)
    free(handles)
    if pool_failures != 0:
        return -1
    return joined_total


@c_abi_export("py_virtual_thread_current")
def py_virtual_thread_current():
    current = global_load_ptr("pcc_current_virtual_thread_py")
    if ptr_is_null(current):
        current = global_load_ptr("py_None")
    py_incref(current)
    return current


@c_abi_export("py_virtual_thread_cancel_requested")
def py_virtual_thread_cancel_requested(vthread) -> int:
    vthread = _checked(vthread)
    if ptr_is_null(vthread):
        return -1
    if _scheduler_lock() != 0:
        return -1
    requested = load_i64(vthread, 128)
    _scheduler_unlock()
    return requested


@c_abi_export("py_virtual_thread_cancel")
def py_virtual_thread_cancel(vthread) -> int:
    vthread = _checked(vthread)
    if ptr_is_null(vthread):
        return -1
    if _scheduler_lock() != 0:
        return -1
    state = load_i64(vthread, 32)
    if state == 4 or load_i64(vthread, 128) != 0:
        _scheduler_unlock()
        return 0
    if (
        state == 1
        and load_i64(vthread, 40) != 0
        and load_i64(vthread, 120) == 0
        and load_i64(vthread, 176) >= 0
    ):
        # A channel winner has already transferred its single rooted group to
        # the ready queue.  Preserve that committed result for one resume;
        # the result accessor promotes this deferred request to an ordinary
        # cooperative cancellation immediately after consuming the value.
        store_i64(vthread, 128, 2)
        _scheduler_unlock()
        return 1

    join_node = null()
    channel_group = null()
    ready_node = null()
    needs_ready = (
        state == 0
        or state == 3
        or (state == 1 and load_i64(vthread, 40) == 0)
    )
    wait_kind = load_i64(vthread, 120)
    if (
        state == 3
        and (
            wait_kind == PCC_VTHREAD_WAIT_CHANNEL_SEND
            or wait_kind == PCC_VTHREAD_WAIT_CHANNEL_RECV
            or wait_kind == PCC_VTHREAD_WAIT_CHANNEL_SELECT2
        )
    ):
        channel_group = _channel_detach_wait_locked(vthread)
        if ptr_is_null(channel_group):
            _scheduler_unlock()
            return -1
        needs_ready = False
    elif state == 3 and ptr_is_null(load_ptr(vthread, 104)) == 0:
        join_node = _join_unlink_waiter(vthread)
        if ptr_is_null(join_node):
            _scheduler_unlock()
            return -1
        needs_ready = False
    elif needs_ready:
        # Reserve both allocation and scheduler-root ownership before removing
        # a timer/IO registration. Once those roots are retired there is no
        # future event that could recover an OOM-lost wakeup.
        ready_node = _ready_reserve(vthread)
        if ptr_is_null(ready_node):
            _scheduler_unlock()
            return -1

    store_i64(vthread, 128, 1)
    if ptr_is_null(channel_group) == 0:
        _ready_commit(vthread, channel_group)
        _effect(6, 4, 0, load_i64(vthread, 32))
    elif ptr_is_null(join_node) == 0:
        # Join and ready nodes share their rooted prefix. Hand the existing
        # node directly to the ready queue without allocation or root churn.
        _ready_commit(vthread, join_node)
        _effect(6, 1, 0, load_i64(vthread, 32))
    elif ptr_is_null(ready_node) == 0:
        _timer_cancel(vthread)
        _io_cancel(vthread)
        # A waitset refresh can fail after _io_cancel has already retired this
        # task's node/root. The cancellation and ready handoff are still
        # committed: a stale aggregate kernel registration has no live task
        # node and a later refresh will remove it.
        _ready_commit(vthread, ready_node)
        _effect(6, 0, 0, load_i64(vthread, 32))
    _scheduler_unlock()
    return 1


def _cancel_publish_locked(vthread) -> int:
    pcc_gc_store_ptr(
        vthread,
        ptr_add(vthread, 24),
        global_load_ptr("py_None"),
    )
    pcc_gc_store_ptr(vthread, ptr_add(vthread, 72), null())
    pcc_gc_store_ptr(vthread, ptr_add(vthread, 112), null())
    _channel_task_clear(vthread, 1)
    store_i64(vthread, 176, -1)
    store_i64(vthread, 184, -1)
    store_ptr(vthread, 104, null())
    store_i64(vthread, 80, 3)
    store_i64(vthread, 32, 4)
    store_i64(vthread, 40, 0)
    store_i64(vthread, 120, 0)
    store_i64(vthread, 128, 0)
    if _join_wake_all(vthread) != 0:
        return -1
    _effect(16, 0, 0, 4)
    return 0


@c_abi_export("py_virtual_thread_cancel_complete")
def py_virtual_thread_cancel_complete(vthread) -> int:
    vthread = _checked(vthread)
    if ptr_is_null(vthread):
        return -1
    if _scheduler_lock() != 0:
        return -1
    if load_i64(vthread, 32) == 4:
        already_cancelled = load_i64(vthread, 80) == 3
        _scheduler_unlock()
        if already_cancelled:
            return 0
        return -1

    if _channel_terminal_cleanup_locked(vthread) < 0:
        _scheduler_unlock()
        return -1
    join_node = null()
    if ptr_is_null(load_ptr(vthread, 104)) == 0:
        join_node = _join_unlink_waiter(vthread)
        if ptr_is_null(join_node):
            _scheduler_unlock()
            return -1
    if ptr_is_null(join_node) == 0:
        _join_release(join_node)
    _timer_cancel(vthread)
    _io_cancel(vthread)
    result = _cancel_publish_locked(vthread)
    _scheduler_unlock()
    return result


@c_abi_export("py_virtual_thread_resume_generator")
def py_virtual_thread_resume_generator(vthread, continuation) -> int:
    generator = py_continuation_get_slot(continuation, 0)
    return _resume_generator_owned(vthread, generator)


def _resume_generator_owned(vthread, generator) -> int:
    """Consume one owned generator reference through the common task protocol."""
    if ptr_is_null(generator):
        return -1
    cancel_pending = py_virtual_thread_cancel_requested(vthread)
    if cancel_pending < 0:
        py_decref(generator)
        return -1
    if cancel_pending == 1:
        generator_state = py_gen_state(generator)
        if generator_state < 0:
            py_decref(generator)
            return -1
        if generator_state == 0:
            # Closing an unstarted generator must not enter its body; marking
            # it done directly preserves Python's no-finally-before-entry rule.
            py_gen_set_done(generator)
            result = py_virtual_thread_cancel_complete(vthread)
            py_decref(generator)
            return result
        closed = py_gen_close(generator)
        if ptr_is_null(closed):
            # A cleanup exception remains in task-local TLS for run_once to
            # publish as RAISED. Yielding/parking from close is rejected by
            # py_gen_close as the same synchronous-cleanup failure.
            py_decref(generator)
            return -1
        py_decref(closed)
        result = py_virtual_thread_cancel_complete(vthread)
        py_decref(generator)
        return result
    yielded = py_gen_next(generator)
    if ptr_is_null(yielded) == 0:
        py_decref(yielded)
        result = 0
        state = py_virtual_thread_state(vthread)
        cancel_pending = py_virtual_thread_cancel_requested(vthread)
        if state < 0 or cancel_pending < 0:
            py_decref(generator)
            return -1
        if cancel_pending == 1:
            # The request may have raced with this generator's transition from
            # RUNNING into a timer/IO/join park. We still own the active carrier,
            # so close synchronously here instead of canceling the registration
            # and allocating another ready node (which could lose the wake on
            # OOM). cancel_complete/fail retires the registration afterwards.
            closed = py_gen_close(generator)
            if ptr_is_null(closed):
                py_decref(generator)
                return -1
            py_decref(closed)
            result = py_virtual_thread_cancel_complete(vthread)
            py_decref(generator)
            return result
        if state == 2:
            result = py_virtual_thread_unpark(vthread)
        py_decref(generator)
        return result
    current = py_current_exception()
    if py_exc_matches(current, py_exc_builtin_class(8)) != 0:
        value = py_exc_get_message(current)
        if ptr_is_null(value):
            value = global_load_ptr("py_None")
        py_incref(value)
        py_clear_exception()
        result = py_virtual_thread_complete(vthread, value)
        py_decref(value)
        py_decref(generator)
        return result
    py_decref(generator)
    return -1


@c_abi_export("py_virtual_thread_join")
def py_virtual_thread_join(vthread, target) -> int:
    waiter = _checked(vthread)
    joined = _checked(target)
    if ptr_is_null(waiter) or ptr_is_null(joined) or ptr_eq(waiter, joined):
        return -1
    current = global_load_ptr("pcc_current_virtual_thread_py")
    if ptr_is_null(current):
        return -1
    resolved = pcc_gc_note_relocation_read(current)
    if ptr_is_null(resolved) == 0:
        current = resolved
    if ptr_eq(current, waiter) == 0:
        return -1
    if _scheduler_lock() != 0:
        return -1
    if (
        load_i64(waiter, 32) != 2
        or ptr_is_null(load_ptr(waiter, 104)) == 0
        or ptr_is_null(pcc_gc_load_ptr(waiter, ptr_add(waiter, 112))) == 0
    ):
        _scheduler_unlock()
        return -1
    pcc_gc_store_ptr(waiter, ptr_add(waiter, 112), joined)
    if load_i64(joined, 32) == 4:
        store_i64(waiter, 120, 0)
        _scheduler_unlock()
        return 1
    if _join_enqueue(joined, waiter) != 0:
        pcc_gc_store_ptr(waiter, ptr_add(waiter, 112), null())
        _scheduler_unlock()
        return -1
    _scheduler_unlock()
    return 0


@c_abi_export("py_virtual_thread_join_result")
def py_virtual_thread_join_result(vthread):
    waiter = _checked(vthread)
    if ptr_is_null(waiter):
        return null()
    if _scheduler_lock() != 0:
        return null()
    target = pcc_gc_load_ptr(waiter, ptr_add(waiter, 112))
    if (
        ptr_is_null(target)
        or is_tagged_int(target)
        or load_i32(target, 8) != PY_TYPE_VIRTUAL_THREAD
    ):
        _scheduler_unlock()
        exception = py_exc_new(7, cstr("join has no target"))
        py_raise(exception)
        py_decref(exception)
        return null()
    if load_i64(target, 32) != 4:
        _scheduler_unlock()
        exception = py_exc_new(7, cstr("join target is not done"))
        py_raise(exception)
        py_decref(exception)
        return null()
    outcome = load_i64(target, 80)
    if outcome == 2:
        payload = pcc_gc_load_ptr(target, ptr_add(target, 72))
    else:
        payload = pcc_gc_load_ptr(target, ptr_add(target, 24))
    if ptr_is_null(payload):
        payload = global_load_ptr("py_None")
    py_incref(payload)
    pcc_gc_store_ptr(waiter, ptr_add(waiter, 112), null())
    store_ptr(waiter, 104, null())
    store_i64(waiter, 120, 0)
    _scheduler_unlock()
    if outcome == 1:
        return payload
    if outcome == 2:
        py_raise(payload)
        py_decref(payload)
        return null()
    py_decref(payload)
    if outcome == 3:
        exception = py_exc_new(7, cstr("virtual thread cancelled"))
    else:
        exception = py_exc_new(7, cstr("join target has no outcome"))
    py_raise(exception)
    py_decref(exception)
    return null()


@c_abi_export("py_virtual_thread_state")
def py_virtual_thread_state(vthread) -> int:
    vthread = _checked(vthread)
    if ptr_is_null(vthread):
        return -1
    if _scheduler_lock() != 0:
        return -1
    state = load_i64(vthread, 32)
    _scheduler_unlock()
    return state


@c_abi_export("py_virtual_thread_complete")
def py_virtual_thread_complete(vthread, result) -> int:
    vthread = _checked(vthread)
    if ptr_is_null(vthread):
        return -1
    if _scheduler_lock() != 0:
        return -1
    if load_i64(vthread, 32) == 4 and load_i64(vthread, 80) != 0:
        already_returned = load_i64(vthread, 80) == 1
        _scheduler_unlock()
        if already_returned:
            return 0
        return -1
    if _channel_terminal_cleanup_locked(vthread) < 0:
        _scheduler_unlock()
        return -1
    join_node = null()
    if ptr_is_null(load_ptr(vthread, 104)) == 0:
        join_node = _join_unlink_waiter(vthread)
        if ptr_is_null(join_node):
            _scheduler_unlock()
            return -1
    if ptr_is_null(join_node) == 0:
        _join_release(join_node)
    if _timer_cancel(vthread) < 0 or _io_cancel(vthread) < 0:
        _scheduler_unlock()
        return -1
    if load_i64(vthread, 128) != 0:
        cancelled = _cancel_publish_locked(vthread)
        _scheduler_unlock()
        return cancelled
    if ptr_is_null(result):
        result = global_load_ptr("py_None")
    pcc_gc_store_ptr(vthread, ptr_add(vthread, 24), result)
    pcc_gc_store_ptr(vthread, ptr_add(vthread, 72), null())
    pcc_gc_store_ptr(vthread, ptr_add(vthread, 112), null())
    _channel_task_clear(vthread, 1)
    store_i64(vthread, 176, -1)
    store_i64(vthread, 184, -1)
    store_ptr(vthread, 104, null())
    store_i64(vthread, 80, 1)
    store_i64(vthread, 32, 4)
    store_i64(vthread, 40, 0)
    store_i64(vthread, 120, 0)
    store_i64(vthread, 128, 0)
    if _join_wake_all(vthread) != 0:
        _scheduler_unlock()
        return -1
    _effect(14, 0, 0, 4)
    _scheduler_unlock()
    return 0


@c_abi_export("py_virtual_thread_fail")
def py_virtual_thread_fail(vthread, exception) -> int:
    vthread = _checked(vthread)
    if ptr_is_null(vthread) or ptr_is_null(exception):
        return -1
    if _scheduler_lock() != 0:
        return -1
    outcome = load_i64(vthread, 80)
    if load_i64(vthread, 32) == 4:
        already_raised = outcome == 2
        _scheduler_unlock()
        if already_raised:
            return 0
        return -1
    if _channel_terminal_cleanup_locked(vthread) < 0:
        _scheduler_unlock()
        return -1
    join_node = null()
    if ptr_is_null(load_ptr(vthread, 104)) == 0:
        join_node = _join_unlink_waiter(vthread)
        if ptr_is_null(join_node):
            _scheduler_unlock()
            return -1
    if ptr_is_null(join_node) == 0:
        _join_release(join_node)
    if _timer_cancel(vthread) < 0 or _io_cancel(vthread) < 0:
        _scheduler_unlock()
        return -1
    pcc_gc_store_ptr(
        vthread,
        ptr_add(vthread, 24),
        global_load_ptr("py_None"),
    )
    pcc_gc_store_ptr(vthread, ptr_add(vthread, 72), exception)
    pcc_gc_store_ptr(vthread, ptr_add(vthread, 112), null())
    _channel_task_clear(vthread, 1)
    store_i64(vthread, 176, -1)
    store_i64(vthread, 184, -1)
    store_ptr(vthread, 104, null())
    store_i64(vthread, 80, 2)
    store_i64(vthread, 32, 4)
    store_i64(vthread, 40, 0)
    store_i64(vthread, 120, 0)
    store_i64(vthread, 128, 0)
    if _join_wake_all(vthread) != 0:
        _scheduler_unlock()
        return -1
    _effect(15, 0, 0, 4)
    _scheduler_unlock()
    return 0


@c_abi_export("py_virtual_thread_result")
def py_virtual_thread_result(vthread):
    vthread = _checked(vthread)
    if ptr_is_null(vthread):
        return null()
    if _scheduler_lock() != 0:
        return null()
    result = pcc_gc_load_ptr(vthread, ptr_add(vthread, 24))
    if ptr_is_null(result):
        result = global_load_ptr("py_None")
    py_incref(result)
    _scheduler_unlock()
    return result


@c_abi_export("py_virtual_thread_exception")
def py_virtual_thread_exception(vthread):
    vthread = _checked(vthread)
    if ptr_is_null(vthread):
        return null()
    if _scheduler_lock() != 0:
        return null()
    exception = pcc_gc_load_ptr(vthread, ptr_add(vthread, 72))
    if ptr_is_null(exception):
        exception = global_load_ptr("py_None")
    py_incref(exception)
    _scheduler_unlock()
    return exception


@c_abi_export("py_virtual_thread_outcome")
def py_virtual_thread_outcome(vthread) -> int:
    vthread = _checked(vthread)
    if ptr_is_null(vthread):
        return -1
    if _scheduler_lock() != 0:
        return -1
    outcome = load_i64(vthread, 80)
    _scheduler_unlock()
    return outcome


def _pin_reason_hash(reason) -> int:
    if ptr_is_null(reason):
        return 1
    value = 5381
    index = 0
    while index < 63:
        byte = load_i8(reason, index)
        if byte == 0:
            break
        if byte < 0:
            byte = byte + 256
        value = ((value * 33) ^ byte) & 2147483647
        index = index + 1
    if value == 0:
        return 1
    return value


def _pin_reason_note(reason) -> None:
    reason_hash = _pin_reason_hash(reason)
    hashes = global_addr("pcc_vthread_pin_reason_hashes_py")
    events = global_addr("pcc_vthread_pin_reason_events_py")
    offset = 0
    while offset < 32:
        index = (reason_hash + offset) % 32
        existing = load_i64(hashes, index * 8)
        if existing == 0:
            store_i64(hashes, index * 8, reason_hash)
            store_i64(events, index * 8, 1)
            return
        if existing == reason_hash:
            store_i64(events, index * 8, load_i64(events, index * 8) + 1)
            return
        offset = offset + 1
    _counter_add(global_addr("pcc_vthread_pin_reason_dropped_py"), 1)


@c_abi_export("py_virtual_thread_pin_reason_event_count")
def py_virtual_thread_pin_reason_event_count(reason) -> int:
    reason_hash = _pin_reason_hash(reason)
    if _scheduler_lock() != 0:
        return -1
    hashes = global_addr("pcc_vthread_pin_reason_hashes_py")
    events = global_addr("pcc_vthread_pin_reason_events_py")
    offset = 0
    while offset < 32:
        index = (reason_hash + offset) % 32
        existing = load_i64(hashes, index * 8)
        if existing == 0:
            _scheduler_unlock()
            return 0
        if existing == reason_hash:
            count = load_i64(events, index * 8)
            _scheduler_unlock()
            return count
        offset = offset + 1
    _scheduler_unlock()
    return 0


@c_abi_export("py_virtual_thread_pin_reason_dropped_count")
def py_virtual_thread_pin_reason_dropped_count() -> int:
    return _counter(global_addr("pcc_vthread_pin_reason_dropped_py"))


@c_abi_export("py_virtual_thread_pin_enter")
def py_virtual_thread_pin_enter(vthread, reason) -> int:
    vthread = _checked(vthread)
    if ptr_is_null(vthread) or load_i64(vthread, 32) == 4:
        return -1
    if _scheduler_lock() != 0:
        return -1
    if load_i64(vthread, 32) == 4:
        _scheduler_unlock()
        return -1
    pinned = load_i64(vthread, 48)
    if pinned == 0:
        pcc_gc_pin(vthread)
    pinned = pinned + 1
    store_i64(vthread, 48, pinned)
    _counter_add(global_addr("pcc_vthread_pin_depth_py"), 1)
    _counter_add(global_addr("pcc_vthread_pin_events_py"), 1)
    _pin_reason_note(reason)
    _scheduler_unlock()
    return pinned


@c_abi_export("py_virtual_thread_pin_leave")
def py_virtual_thread_pin_leave(vthread) -> int:
    vthread = _checked(vthread)
    if ptr_is_null(vthread):
        return -1
    if _scheduler_lock() != 0:
        return -1
    pinned = load_i64(vthread, 48)
    if pinned > 0:
        pinned = pinned - 1
        store_i64(vthread, 48, pinned)
        if _counter(global_addr("pcc_vthread_pin_depth_py")) > 0:
            _counter_add(global_addr("pcc_vthread_pin_depth_py"), -1)
        if pinned == 0:
            pcc_gc_unpin(vthread)
    _scheduler_unlock()
    return pinned


@c_abi_export("py_virtual_thread_pin_count")
def py_virtual_thread_pin_count(vthread) -> int:
    vthread = _checked(vthread)
    if ptr_is_null(vthread):
        return -1
    if _scheduler_lock() != 0:
        return -1
    pinned = load_i64(vthread, 48)
    _scheduler_unlock()
    return pinned


@c_abi_export("py_virtual_thread_pinned_count")
def py_virtual_thread_pinned_count() -> int:
    return _counter(global_addr("pcc_vthread_pin_depth_py"))


@c_abi_export("py_virtual_thread_pin_event_count")
def py_virtual_thread_pin_event_count() -> int:
    return _counter(global_addr("pcc_vthread_pin_events_py"))


@c_abi_export("py_dealloc_virtual_thread")
def py_dealloc_virtual_thread(vthread) -> None:
    if ptr_is_null(vthread):
        return
    continuation = pcc_gc_load_ptr(vthread, ptr_add(vthread, 16))
    result = pcc_gc_load_ptr(vthread, ptr_add(vthread, 24))
    exception = pcc_gc_load_ptr(vthread, ptr_add(vthread, 72))
    join_target = pcc_gc_load_ptr(vthread, ptr_add(vthread, 112))
    channel_owner_a = pcc_gc_load_ptr(vthread, ptr_add(vthread, 136))
    channel_owner_b = pcc_gc_load_ptr(vthread, ptr_add(vthread, 144))
    channel_value = pcc_gc_load_ptr(vthread, ptr_add(vthread, 168))
    if ptr_is_null(continuation) == 0:
        py_decref(continuation)
    if ptr_is_null(result) == 0:
        py_decref(result)
    if ptr_is_null(exception) == 0:
        py_decref(exception)
    if ptr_is_null(join_target) == 0:
        py_decref(join_target)
    if ptr_is_null(channel_owner_a) == 0:
        py_decref(channel_owner_a)
    if ptr_is_null(channel_owner_b) == 0:
        py_decref(channel_owner_b)
    if ptr_is_null(channel_value) == 0:
        py_decref(channel_value)
    pcc_gc_free_object_memory(vthread)


@c_abi_export("py_dealloc_vthread_channel")
def py_dealloc_vthread_channel(channel) -> None:
    if ptr_is_null(channel):
        return
    kind = load_i64(channel, 16)
    if kind == PCC_VTHREAD_CHANNEL_KIND_CORE:
        # Live waiter links/leases own scheduler roots which keep the core
        # reachable.  A nonzero value here therefore denotes corrupted
        # teardown; preserve memory rather than freeing raw nodes underneath
        # another carrier.
        if (
            load_i64(channel, 120) != 0
            or ptr_is_null(load_ptr(channel, 88)) == 0
            or ptr_is_null(load_ptr(channel, 96)) == 0
            or ptr_is_null(load_ptr(channel, 104)) == 0
            or ptr_is_null(load_ptr(channel, 112)) == 0
        ):
            return
        capacity = load_i64(channel, 24)
        if capacity < 0 or capacity > PCC_VTHREAD_CHANNEL_MAX_CAPACITY:
            return
        index = 0
        while index < capacity:
            slot = _channel_ring_slot(channel, index)
            value = pcc_gc_load_ptr(channel, slot)
            store_ptr(slot, 0, null())
            if ptr_is_null(value) == 0:
                py_decref(value)
            index = index + 1
    elif kind == PCC_VTHREAD_CHANNEL_KIND_SENDER or kind == PCC_VTHREAD_CHANNEL_KIND_RECEIVER:
        core = pcc_gc_load_ptr(channel, ptr_add(channel, 24))
        store_ptr(channel, 24, null())
        if ptr_is_null(core) == 0:
            py_decref(core)
    else:
        return
    pcc_gc_free_object_memory(channel)


# --- effect-handler dispatch ------------------------------------------
# Effect-handler dispatch (Handler.Dispatch model): an effect operation
# performed by a virtual thread can be intercepted by a registered handler
# which decides whether to resume the computation (continue=1) or
# short-circuit it (continue=0).  The internal scheduling-audit effect
# buffer (_effect / pcc_vthread_effect_*) is left untouched; this is a
# separate user-visible effect layer.

define_global_null_ptr_array("pcc_vthread_effect_handlers", 512)
define_global_null_ptr_array("pcc_vthread_effect_handler_ctxs", 512)
define_global_i64_array("pcc_vthread_effect_handler_count", 0)



@c_abi_export("py_vthread_effect_set_handler")
def py_vthread_effect_set_handler(kind: int, fn_ptr, ctx) -> int:
    if kind < 0 or kind >= (64):
        return -1
    if ptr_is_null(fn_ptr):
        return -1
    table = global_addr("pcc_vthread_effect_handlers")
    ctxs = global_addr("pcc_vthread_effect_handler_ctxs")
    store_ptr(table, kind * 8, fn_ptr)
    store_ptr(ctxs, kind * 8, ctx)
    _counter_set(global_addr("pcc_vthread_effect_handler_count"), kind + 1)
    return 0


@c_abi_export("py_vthread_effect_clear_handler")
def py_vthread_effect_clear_handler(kind: int) -> int:
    if kind < 0 or kind >= (64):
        return -1
    table = global_addr("pcc_vthread_effect_handlers")
    store_ptr(table, kind * 8, null())
    return 0


@c_abi_export("py_vthread_effect_perform")
def py_vthread_effect_perform(kind: int, detail: int) -> int:
    """Perform an effect operation; the registered handler (if any) decides
    whether the computation continues (1) or short-circuits (0).  Returns -1
    when no handler is registered (unhandled effect)."""
    if kind < 0 or kind >= (64):
        return -1
    table = global_addr("pcc_vthread_effect_handlers")
    fn = load_ptr(table, kind * 8)
    if ptr_is_null(fn):
        return -1
    ctxs = global_addr("pcc_vthread_effect_handler_ctxs")
    ctx = load_ptr(ctxs, kind * 8)
    if ptr_is_null(ctx):
        ctx = global_load_ptr("py_None")
    result = call_i64_i64_i64_ptr(fn, kind, detail, ctx)
    if result == 0:
        return 0
    return 1


@c_abi_export("py_vthread_effect_handled_count")
def py_vthread_effect_handled_count() -> int:
    return _counter(global_addr("pcc_vthread_effect_handler_count"))
