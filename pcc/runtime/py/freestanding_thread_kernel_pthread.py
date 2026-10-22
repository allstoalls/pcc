"""pthread-backed runtime kernel authored in pcc-Python.

Selected only for an explicit ``PCC_WITH_THREADS=1`` runtime archive.  The
module calls the named pthread ABI directly and keeps the stop-the-world,
thread-registration, join/detach, mutex, condition, and atomic-refcount
contracts formerly mixed into ``pcc_threads.c``.
"""

__pcc_runtime_port__ = True

from pcc.extern import c_abi_export, c_int64, c_ptr, c_void, extern
from pcc.unsafe import (
    atomic_load_i64,
    atomic_load_i32,
    atomic_cas_i32,
    atomic_rmw_i64,
    atomic_store_i32,
    call_ptr1,
    define_global_i32,
    define_global_i64,
    define_global_ptr_null,
    define_thread_local_i32,
    define_thread_local_ptr_null,
    frame_address,
    free,
    function_addr,
    global_addr,
    global_load_ptr,
    global_store_ptr,
    int_to_ptr,
    load_i32,
    load_i64,
    load_ptr,
    malloc,
    null,
    ptr_is_null,
    ptr_to_int,
    stack_alloc,
    store_i32,
    store_i64,
    store_ptr,
)


define_global_i32("pcc_thread_stop_requested", 0)
define_thread_local_i32("pcc_native_thread_identity_token", 0)
define_thread_local_ptr_null("pcc_tls_thread_id_py")
define_thread_local_i32("pcc_tls_thread_parked_py", 0)
define_thread_local_i32("pcc_tls_no_park_depth_py", 0)
define_thread_local_i32("pcc_tls_scheduler_lock_held_py", 0)
define_thread_local_i32("pcc_tls_unregister_in_progress_py", 0)
define_thread_local_ptr_null("pcc_tls_parked_epoch_py")
define_global_ptr_null("pcc_world_lock_py")
define_global_ptr_null("pcc_world_cond_py")
define_global_i32("pcc_world_init_state_py", 0)
define_global_i64("pcc_next_thread_id_py", 1)
define_global_i64("pcc_live_thread_count_py", 0)
define_global_i64("pcc_parked_thread_count_py", 0)
define_global_i64("pcc_stop_owner_thread_id_py", 0)
define_global_i64("pcc_stop_epoch_py", 0)
define_global_i64("pcc_stop_depth_py", 0)
define_global_i64("pcc_registration_waiter_count_py", 0)
# Registered mutators that are blocked in a kernel wait ("exited"): a
# stop-the-world counts them as stopped, as Fil-C's handshakes and Go's
# syscall states do, instead of waiting for them to reach a poll.
define_global_i64("pcc_exited_thread_count_py", 0)
# One record per registered mutator, linked under the world lock:
#   0 next, 8 thread id, 16 state (0 running, 1 parked, 2 exited),
#   24 frame pointer whose callers a stack walk visits while not running,
#   32 outermost frame pointer seen at registration,
#   40 1 while parked at an application gate (a poll in compiled program
#      code, where every live managed reference is in a mapped or
#      registered root and is reloaded after the poll); 0 in runtime code.
define_global_ptr_null("pcc_thread_records_head_py")
define_thread_local_ptr_null("pcc_tls_thread_record_py")


pthread_create = extern(
    "pthread_create", (c_ptr, c_ptr, c_ptr, c_ptr), c_int64
)
pthread_join = extern("pthread_join", (c_ptr, c_ptr), c_int64)
pthread_detach = extern("pthread_detach", (c_ptr,), c_int64)
pthread_mutex_init = extern("pthread_mutex_init", (c_ptr, c_ptr), c_int64)
pthread_mutex_destroy = extern("pthread_mutex_destroy", (c_ptr,), c_int64)
pthread_mutex_trylock = extern("pthread_mutex_trylock", (c_ptr,), c_int64)
pthread_mutex_lock = extern("pthread_mutex_lock", (c_ptr,), c_int64)
pthread_mutex_unlock = extern("pthread_mutex_unlock", (c_ptr,), c_int64)
pthread_cond_init = extern("pthread_cond_init", (c_ptr, c_ptr), c_int64)
pthread_cond_destroy = extern("pthread_cond_destroy", (c_ptr,), c_int64)
pthread_cond_wait = extern("pthread_cond_wait", (c_ptr, c_ptr), c_int64)
pthread_cond_timedwait = extern(
    "pthread_cond_timedwait", (c_ptr, c_ptr, c_ptr), c_int64
)
pthread_cond_signal = extern("pthread_cond_signal", (c_ptr,), c_int64)
pthread_cond_broadcast = extern("pthread_cond_broadcast", (c_ptr,), c_int64)
sched_yield = extern("sched_yield", (), c_int64)
pcc_platform_wall_time_us = extern("pcc_platform_wall_time_us", (), c_int64)
pcc_platform_abort = extern("pcc_platform_abort", (), c_void)
pcc_gc_thread_unregister_buffers = extern(
    "pcc_gc_thread_unregister_buffers", (), c_void
)
py_clear_exception = extern("py_clear_exception", (), c_void)
pcc_diagnostics_runtime_log_event_code = extern(
    "pcc_diagnostics_runtime_log_event_code",
    (c_int64, c_int64, c_int64, c_int64, c_ptr),
    c_void,
)


pcc_diagnostics_runtime_log_suspension_pair = extern(
    "pcc_diagnostics_runtime_log_suspension_pair",
    (c_int64, c_int64, c_int64),
    c_void,
)


def _thread_log(event: int, status: int, handle) -> None:
    # Local raw constants avoid runtime module-initialization dependencies.
    # Never call this under world/state locks or after thread unregister.
    thread_category: int = 9
    pcc_diagnostics_runtime_log_event_code(
        thread_category, event, status, 0, handle
    )


def _thread_trace(event: int, value0: int, value1: int, pointer) -> None:
    # Sink activity is permitted only outside world/scheduler/state locks.
    pcc_diagnostics_runtime_log_event_code(9, event, value0, value1, pointer)


def _suspend_permitted(lock) -> int:
    # Recheck at the actual suspension boundary, before publishing parked.
    # A full logger here could deadlock. Release the world lock and fail-stop;
    # never clear a lease/ownership marker to make an unsafe suspension pass.
    if load_i32(global_addr("pcc_tls_scheduler_lock_held_py"), 0) != 0 or load_i32(
        global_addr("pcc_tls_no_park_depth_py"), 0
    ) != 0:
        pthread_mutex_unlock(lock)
        pcc_platform_abort()
        return 0
    return 1


def _flush_suspend_trace(records, count: int, last_epoch: int, thread_id: int) -> None:
    # Raw stack records contain epoch and actual condition-wait call count.
    # Emit only after unlocking, with explicit deferred names: log timestamps
    # describe delivery, not the earlier suspend/resume transition times.
    # These callers have returned from an allowed suspension, hold no world
    # lock, and have not unregistered. Pass their captured ID; the paired sink
    # must not register or independently drop the two halves under contention.
    index: int = 0
    while index < count and index < 8:
        epoch: int = load_i64(records, index * 16)
        waits: int = load_i64(records, index * 16 + 8)
        pcc_diagnostics_runtime_log_suspension_pair(thread_id, epoch, waits)
        index = index + 1
    if count > 8:
        _thread_trace(25, last_epoch, count - 8, null())


def _tls_i64(slot) -> int:
    return ptr_to_int(load_ptr(slot, 0))


def _tls_store_i64(slot, value: int) -> None:
    store_ptr(slot, 0, int_to_ptr(value))


def _world_i64(slot) -> int:
    return load_i64(slot, 0)


def _world_store_i64(slot, value: int) -> None:
    store_i64(slot, 0, value)


def _world_init() -> int:
    state = atomic_load_i32(
        global_addr("pcc_world_init_state_py"), 0, "acquire"
    )
    if state == 2:
        return 0
    observed = atomic_cas_i32(
        global_addr("pcc_world_init_state_py"), 0, 0, 1, "acq_rel", "acquire"
    )
    if observed == 0:
        mutex = pcc_mutex_new()
        cond = pcc_cond_new()
        if ptr_is_null(mutex) or ptr_is_null(cond):
            pcc_mutex_free(mutex)
            pcc_cond_free(cond)
            atomic_store_i32(
                global_addr("pcc_world_init_state_py"), 0, 0, "release"
            )
            return -1
        global_store_ptr("pcc_world_lock_py", mutex)
        global_store_ptr("pcc_world_cond_py", cond)
        atomic_store_i32(
            global_addr("pcc_world_init_state_py"), 0, 2, "release"
        )
        return 0
    while atomic_load_i32(
        global_addr("pcc_world_init_state_py"), 0, "acquire"
    ) == 1:
        sched_yield()
    if atomic_load_i32(
        global_addr("pcc_world_init_state_py"), 0, "acquire"
    ) == 2:
        return 0
    return -1


@c_abi_export("pcc_threads_enabled")
def pcc_threads_enabled() -> int:
    return 1


@c_abi_export("pcc_thread_live_count")
def pcc_thread_live_count() -> int:
    """Registered mutator threads (at least the caller once it is registered)."""
    if _world_init() != 0:
        return 1
    lock = global_load_ptr("pcc_world_lock_py")
    pthread_mutex_lock(lock)
    count = _world_i64(global_addr("pcc_live_thread_count_py"))
    pthread_mutex_unlock(lock)
    return count


@c_abi_export("pcc_thread_stop_requested_acquire")
def pcc_thread_stop_requested_acquire() -> int:
    return atomic_load_i32(
        global_addr("pcc_thread_stop_requested"), 0, "acquire"
    )


@c_abi_export("pcc_refcount_strategy")
def pcc_refcount_strategy() -> int:
    return 1


@c_abi_export("pcc_current_native_thread_token")
def pcc_current_native_thread_token():
    return global_addr("pcc_native_thread_identity_token")


@c_abi_export("pcc_refcount_incref")
def pcc_refcount_incref(slot) -> int:
    if ptr_is_null(slot):
        return 0
    return atomic_rmw_i64("add", slot, 0, 1, "relaxed") + 1


@c_abi_export("pcc_refcount_decref")
def pcc_refcount_decref(slot) -> int:
    if ptr_is_null(slot):
        return 0
    return atomic_rmw_i64("sub", slot, 0, 1, "acq_rel") - 1


@c_abi_export("pcc_refcount_load")
def pcc_refcount_load(slot) -> int:
    if ptr_is_null(slot):
        return 0
    return atomic_load_i64(slot, 0, "acquire")


@c_abi_export("pcc_refcount_forget")
def pcc_refcount_forget(slot) -> None:
    return


@c_abi_export("pcc_mutex_new")
def pcc_mutex_new():
    mutex = malloc(128)
    if ptr_is_null(mutex):
        return null()
    if pthread_mutex_init(mutex, null()) != 0:
        free(mutex)
        return null()
    return mutex


@c_abi_export("pcc_mutex_free")
def pcc_mutex_free(mutex) -> None:
    if ptr_is_null(mutex):
        return
    pthread_mutex_destroy(mutex)
    free(mutex)


@c_abi_export("pcc_mutex_lock")
def pcc_mutex_lock(mutex) -> int:
    if ptr_is_null(mutex):
        return -1
    while True:
        result = pthread_mutex_trylock(mutex)
        if result == 0:
            return 0
        if result != 16:
            return -1
        pcc_thread_safepoint()
        sched_yield()
    return -1


@c_abi_export("pcc_mutex_unlock")
def pcc_mutex_unlock(mutex) -> int:
    if ptr_is_null(mutex):
        return -1
    if pthread_mutex_unlock(mutex) == 0:
        return 0
    return -1


@c_abi_export("pcc_cond_new")
def pcc_cond_new():
    cond = malloc(128)
    if ptr_is_null(cond):
        return null()
    if pthread_cond_init(cond, null()) != 0:
        free(cond)
        return null()
    return cond


@c_abi_export("pcc_cond_free")
def pcc_cond_free(cond) -> None:
    if ptr_is_null(cond):
        return
    pthread_cond_destroy(cond)
    free(cond)


@c_abi_export("pcc_cond_wait")
def pcc_cond_wait(cond, mutex) -> int:
    if ptr_is_null(cond) or ptr_is_null(mutex):
        return -1
    entered: int = _enter_exited(frame_address())
    result = pthread_cond_wait(cond, mutex)
    _leave_exited(entered)
    if result == 0:
        return 0
    return -1


@c_abi_export("pcc_cond_timedwait_ms")
def pcc_cond_timedwait_ms(cond, mutex, timeout_ms: int) -> int:
    if ptr_is_null(cond) or ptr_is_null(mutex):
        return -1
    if timeout_ms < 0:
        timeout_ms = 0
    now = pcc_platform_wall_time_us()
    target_us = now + timeout_ms * 1000
    timespec = stack_alloc(16)
    store_i64(timespec, 0, target_us // 1000000)
    store_i64(timespec, 8, (target_us % 1000000) * 1000)
    entered: int = _enter_exited(frame_address())
    result = pthread_cond_timedwait(cond, mutex, timespec)
    _leave_exited(entered)
    if result == 0:
        return 0
    if result == 60 or result == 110:
        return 1
    return -1


@c_abi_export("pcc_cond_signal")
def pcc_cond_signal(cond) -> int:
    if ptr_is_null(cond) or pthread_cond_signal(cond) != 0:
        return -1
    return 0


@c_abi_export("pcc_cond_broadcast")
def pcc_cond_broadcast(cond) -> int:
    if ptr_is_null(cond) or pthread_cond_broadcast(cond) != 0:
        return -1
    return 0


@c_abi_export("pcc_current_thread_id")
def pcc_current_thread_id() -> int:
    thread_id = _tls_i64(global_addr("pcc_tls_thread_id_py"))
    if thread_id != 0:
        return thread_id
    if _world_init() != 0:
        # Registration is a lease in the stopped-world accounting.  Returning
        # a synthetic id here would let a caller run without joining the live
        # set and would make a no-park region fail open.
        pcc_platform_abort()
        return 0
    lock = global_load_ptr("pcc_world_lock_py")
    cond = global_load_ptr("pcc_world_cond_py")
    pthread_mutex_lock(lock)
    thread_id = _tls_i64(global_addr("pcc_tls_thread_id_py"))
    # A first-time raw/extension pthread is outside the stopped epoch's live
    # count.  It must not return into user code until the owner resumes.
    waiting_for_admission = 0
    while thread_id == 0 and load_i32(
        global_addr("pcc_thread_stop_requested"), 0
    ) != 0:
        if waiting_for_admission == 0:
            _world_store_i64(
                global_addr("pcc_registration_waiter_count_py"),
                _world_i64(global_addr("pcc_registration_waiter_count_py")) + 1,
            )
            waiting_for_admission = 1
        pthread_cond_wait(cond, lock)
        thread_id = _tls_i64(global_addr("pcc_tls_thread_id_py"))
    if waiting_for_admission != 0:
        _world_store_i64(
            global_addr("pcc_registration_waiter_count_py"),
            _world_i64(global_addr("pcc_registration_waiter_count_py")) - 1,
        )
    if thread_id == 0:
        thread_id = _world_i64(global_addr("pcc_next_thread_id_py"))
        _world_store_i64(global_addr("pcc_next_thread_id_py"), thread_id + 1)
        _world_store_i64(
            global_addr("pcc_live_thread_count_py"),
            _world_i64(global_addr("pcc_live_thread_count_py")) + 1,
        )
        _tls_store_i64(global_addr("pcc_tls_thread_id_py"), thread_id)
        _thread_record_link(thread_id)
        pcc_cond_broadcast(global_load_ptr("pcc_world_cond_py"))
    pthread_mutex_unlock(lock)
    return thread_id


def _outermost_frame_pointer() -> int:
    frame = frame_address()
    outer: int = ptr_to_int(frame)
    steps: int = 0
    walking: int = 1
    while walking != 0:
        caller = load_ptr(frame, 0)
        if ptr_is_null(caller) != 0 or steps >= 100000:
            walking = 0
        else:
            outer = ptr_to_int(caller)
            frame = caller
            steps = steps + 1
    return outer


def _thread_record_link(thread_id: int) -> None:
    # Caller holds the world lock and has just counted this thread live.
    record = malloc(48)
    if ptr_is_null(record):
        pcc_platform_abort()
        return
    store_ptr(record, 0, global_load_ptr("pcc_thread_records_head_py"))
    store_i64(record, 8, thread_id)
    store_i64(record, 16, 0)
    store_ptr(record, 24, null())
    store_i64(record, 32, _outermost_frame_pointer())
    store_i64(record, 40, 0)
    global_store_ptr("pcc_thread_records_head_py", record)
    global_store_ptr("pcc_tls_thread_record_py", record)


def _thread_record_unlink() -> None:
    # Caller holds the world lock and is leaving the live set.
    record = global_load_ptr("pcc_tls_thread_record_py")
    if ptr_is_null(record):
        return
    global_store_ptr("pcc_tls_thread_record_py", null())
    previous = null()
    node = global_load_ptr("pcc_thread_records_head_py")
    searching: int = 1
    while searching != 0 and ptr_is_null(node) == 0:
        if ptr_to_int(node) == ptr_to_int(record):
            if ptr_is_null(previous):
                global_store_ptr("pcc_thread_records_head_py", load_ptr(node, 0))
            else:
                store_ptr(previous, 0, load_ptr(node, 0))
            searching = 0
        else:
            previous = node
            node = load_ptr(node, 0)
    free(record)


def _thread_record_mark(state: int, frame) -> None:
    # Caller holds the world lock.
    record = global_load_ptr("pcc_tls_thread_record_py")
    if ptr_is_null(record):
        return
    store_i64(record, 16, state)
    store_ptr(record, 24, frame)


@c_abi_export("pcc_thread_safepoint_gate")
def pcc_thread_safepoint_gate() -> None:
    """The poll of compiled program code: a moving collector may relocate
    objects while this thread is parked here, because every managed
    reference it holds is rooted and reloaded after the poll."""
    record = global_load_ptr("pcc_tls_thread_record_py")
    if ptr_is_null(record):
        pcc_thread_safepoint()
        return
    store_i64(record, 40, 1)
    pcc_thread_safepoint()
    store_i64(record, 40, 0)


@c_abi_export("pcc_thread_all_stopped_at_gates")
def pcc_thread_all_stopped_at_gates() -> int:
    """1 when every other registered mutator is parked at an application gate.

    The caller owns a stopped world. A mutator parked or exited inside
    runtime code may hold raw object pointers in registers that no root
    describes: relocating then loses its writes or frees storage under it,
    so a moving collector relocates only when this answers 1.
    """
    if _world_init() != 0:
        return 0
    lock = global_load_ptr("pcc_world_lock_py")
    pthread_mutex_lock(lock)
    own = global_load_ptr("pcc_tls_thread_record_py")
    gated: int = 1
    node = global_load_ptr("pcc_thread_records_head_py")
    while ptr_is_null(node) == 0 and gated != 0:
        if ptr_to_int(node) != ptr_to_int(own):
            if load_i64(node, 16) != 1 or load_i64(node, 40) == 0:
                gated = 0
        node = load_ptr(node, 0)
    pthread_mutex_unlock(lock)
    return gated


@c_abi_export("pcc_thread_stack_high")
def pcc_thread_stack_high() -> int:
    """Outermost frame pointer of this registered thread (0 when unknown)."""
    record = global_load_ptr("pcc_tls_thread_record_py")
    if ptr_is_null(record):
        return 0
    return load_i64(record, 32)


@c_abi_export("pcc_thread_collect_stopped_frames")
def pcc_thread_collect_stopped_frames(frames, capacity: int) -> int:
    """Copy every other mutator's walk frame and outermost frame pointer.

    ``frames`` holds ``capacity`` (frame, high) pairs. Returns the pair
    count, or -1 when another registered mutator is running (it has no
    stable stack to walk) or there are more threads than ``capacity``.
    The caller owns a stopped world, so every other mutator is parked or
    exited and its frames stay put until the world resumes.
    """
    if _world_init() != 0:
        return -1
    lock = global_load_ptr("pcc_world_lock_py")
    pthread_mutex_lock(lock)
    own = global_load_ptr("pcc_tls_thread_record_py")
    count: int = 0
    status: int = 0
    node = global_load_ptr("pcc_thread_records_head_py")
    while ptr_is_null(node) == 0 and status == 0:
        if ptr_to_int(node) != ptr_to_int(own):
            frame = load_ptr(node, 24)
            if load_i64(node, 16) == 0 or ptr_is_null(frame) or count >= capacity:
                status = -1
            else:
                store_ptr(frames, count * 16, frame)
                store_i64(frames, count * 16 + 8, load_i64(node, 32))
                count = count + 1
        node = load_ptr(node, 0)
    pthread_mutex_unlock(lock)
    if status != 0:
        return -1
    return count


def _enter_exited(frame) -> int:
    # Returns 1 when this thread entered the exited state. A thread inside
    # a no-park scope, or one the kernel never registered, keeps running
    # state: the stop owner then waits for it as before.
    record = global_load_ptr("pcc_tls_thread_record_py")
    if ptr_is_null(record) or load_i32(global_addr("pcc_tls_no_park_depth_py"), 0) != 0:
        return 0
    if _world_init() != 0:
        return 0
    lock = global_load_ptr("pcc_world_lock_py")
    pthread_mutex_lock(lock)
    _thread_record_mark(2, frame)
    _world_store_i64(
        global_addr("pcc_exited_thread_count_py"),
        _world_i64(global_addr("pcc_exited_thread_count_py")) + 1,
    )
    pcc_cond_broadcast(global_load_ptr("pcc_world_cond_py"))
    pthread_mutex_unlock(lock)
    return 1


def _leave_exited(entered: int) -> None:
    # A thread returning from a blocking wait must not touch managed state
    # while another thread owns a stopped world: it stays exited until the
    # world resumes, then runs again.
    if entered == 0:
        return
    self_id: int = _tls_i64(global_addr("pcc_tls_thread_id_py"))
    lock = global_load_ptr("pcc_world_lock_py")
    cond = global_load_ptr("pcc_world_cond_py")
    pthread_mutex_lock(lock)
    while load_i32(global_addr("pcc_thread_stop_requested"), 0) != 0 and _world_i64(
        global_addr("pcc_stop_owner_thread_id_py")
    ) != self_id:
        pthread_cond_wait(cond, lock)
    _thread_record_mark(0, null())
    _world_store_i64(
        global_addr("pcc_exited_thread_count_py"),
        _world_i64(global_addr("pcc_exited_thread_count_py")) - 1,
    )
    pthread_mutex_unlock(lock)


@c_abi_export("pcc_thread_blocking_enter")
def pcc_thread_blocking_enter(frame) -> int:
    """Enter the exited state around a blocking platform call.

    ``frame`` is the caller's ``frame_address()``; it stays live until the
    matching leave, and a collector walks this thread's stack from it
    instead of waiting for a poll. The caller touches no managed object
    between enter and leave. Returns the token for the leave.
    """
    return _enter_exited(frame)


@c_abi_export("pcc_thread_blocking_leave")
def pcc_thread_blocking_leave(entered: int) -> None:
    """Leave the exited state; waits while another thread owns a stopped world."""
    _leave_exited(entered)


@c_abi_export("pcc_thread_scheduler_lock_acquired")
def pcc_thread_scheduler_lock_acquired() -> None:
    # Independent ownership evidence: a missing no-park lease must not turn
    # a scheduler-lock holder into a suspendable thread.
    if load_i32(global_addr("pcc_tls_scheduler_lock_held_py"), 0) != 0:
        pcc_platform_abort()
        return
    store_i32(global_addr("pcc_tls_scheduler_lock_held_py"), 0, 1)


@c_abi_export("pcc_thread_scheduler_lock_released")
def pcc_thread_scheduler_lock_released() -> None:
    if load_i32(global_addr("pcc_tls_scheduler_lock_held_py"), 0) != 1:
        pcc_platform_abort()
        return
    store_i32(global_addr("pcc_tls_scheduler_lock_held_py"), 0, 0)


@c_abi_export("pcc_thread_no_park_enter")
def pcc_thread_no_park_enter() -> None:
    # Registration is defensive; a raw newcomer must register before it
    # acquires any managed owner/slot.  Do not safepoint an already-registered
    # live caller here: it may have just canonicalized the owner, and
    # live/unparked accounting makes the STW owner wait for outer exit.
    depth = load_i32(global_addr("pcc_tls_no_park_depth_py"), 0)
    if depth < 0 or depth == 2147483647:
        pcc_platform_abort()
        return
    if depth != 0:
        store_i32(global_addr("pcc_tls_no_park_depth_py"), 0, depth + 1)
        return
    pcc_current_thread_id()
    if _tls_i64(global_addr("pcc_tls_thread_id_py")) == 0:
        # Defensive fail-stop: depth must never become nonzero unless this
        # pthread is represented in the live-thread count.
        pcc_platform_abort()
        return
    store_i32(global_addr("pcc_tls_no_park_depth_py"), 0, depth + 1)


@c_abi_export("pcc_thread_no_park_exit")
def pcc_thread_no_park_exit() -> None:
    depth = load_i32(global_addr("pcc_tls_no_park_depth_py"), 0)
    if depth <= 0:
        pcc_platform_abort()
        return
    depth = depth - 1
    store_i32(global_addr("pcc_tls_no_park_depth_py"), 0, depth)
    if depth == 0:
        # Use the real locked safepoint path instead of a racy stop-flag poll.
        pcc_thread_safepoint()


@c_abi_export("pcc_thread_no_park_depth")
def pcc_thread_no_park_depth() -> int:
    return load_i32(global_addr("pcc_tls_no_park_depth_py"), 0)


@c_abi_export("pcc_thread_safepoint")
def pcc_thread_safepoint() -> None:
    # A normal implicit poll inside a valid lease remains suppressed.  Check
    # independent lock ownership first so a broken/missing lease fails closed.
    if load_i32(global_addr("pcc_tls_scheduler_lock_held_py"), 0) != 0 and load_i32(
        global_addr("pcc_tls_no_park_depth_py"), 0
    ) <= 0:
        pcc_platform_abort()
        return
    if load_i32(global_addr("pcc_tls_no_park_depth_py"), 0) != 0:
        return
    if _world_init() != 0:
        pcc_platform_abort()
        return
    self_id = pcc_current_thread_id()
    lock = global_load_ptr("pcc_world_lock_py")
    cond = global_load_ptr("pcc_world_cond_py")
    trace_records = null()
    trace_count: int = 0
    trace_epoch: int = 0
    if pcc_thread_stop_requested_acquire() != 0:
        # Observation/intent only: owner polls and a racing resume need not
        # suspend. Actual waits are reported separately after world unlock.
        _thread_trace(13, 0, 0, null())
    pthread_mutex_lock(lock)
    while load_i32(global_addr("pcc_thread_stop_requested"), 0) != 0 and _world_i64(
        global_addr("pcc_stop_owner_thread_id_py")
    ) != self_id:
        if _suspend_permitted(lock) == 0:
            return
        epoch = _world_i64(global_addr("pcc_stop_epoch_py"))
        if trace_epoch != epoch:
            if ptr_is_null(trace_records):
                trace_records = stack_alloc(128)
            if trace_count < 8:
                store_i64(trace_records, trace_count * 16, epoch)
                store_i64(trace_records, trace_count * 16 + 8, 0)
            trace_count = trace_count + 1
            trace_epoch = epoch
        if load_i32(global_addr("pcc_tls_thread_parked_py"), 0) == 0 or _tls_i64(
            global_addr("pcc_tls_parked_epoch_py")
        ) != epoch:
            store_i32(global_addr("pcc_tls_thread_parked_py"), 0, 1)
            _tls_store_i64(global_addr("pcc_tls_parked_epoch_py"), epoch)
            _thread_record_mark(1, frame_address())
            _world_store_i64(
                global_addr("pcc_parked_thread_count_py"),
                _world_i64(global_addr("pcc_parked_thread_count_py")) + 1,
            )
            pcc_cond_broadcast(cond)
        if trace_count <= 8:
            offset: int = (trace_count - 1) * 16 + 8
            store_i64(trace_records, offset, load_i64(trace_records, offset) + 1)
        pthread_cond_wait(cond, lock)
    if load_i32(global_addr("pcc_thread_stop_requested"), 0) == 0:
        store_i32(global_addr("pcc_tls_thread_parked_py"), 0, 0)
        _tls_store_i64(global_addr("pcc_tls_parked_epoch_py"), 0)
        _thread_record_mark(0, null())
    pthread_mutex_unlock(lock)
    _flush_suspend_trace(trace_records, trace_count, trace_epoch, self_id)


@c_abi_export("pcc_thread_owns_stopped_world")
def pcc_thread_owns_stopped_world() -> int:
    if _world_init() != 0:
        return 0
    self_id = _tls_i64(global_addr("pcc_tls_thread_id_py"))
    if self_id == 0:
        return 0
    lock = global_load_ptr("pcc_world_lock_py")
    pthread_mutex_lock(lock)
    owns = 0
    if load_i32(global_addr("pcc_thread_stop_requested"), 0) != 0 and _world_i64(
        global_addr("pcc_stop_owner_thread_id_py")
    ) == self_id:
        owns = 1
    pthread_mutex_unlock(lock)
    return owns


@c_abi_export("pcc_thread_registration_waiter_count")
def pcc_thread_registration_waiter_count() -> int:
    # The diagnostic itself must not let a raw newcomer cross an active stop.
    pcc_current_thread_id()
    lock = global_load_ptr("pcc_world_lock_py")
    pthread_mutex_lock(lock)
    count = _world_i64(global_addr("pcc_registration_waiter_count_py"))
    pthread_mutex_unlock(lock)
    return count


@c_abi_export("pcc_stop_the_world")
def pcc_stop_the_world() -> int:
    # A scheduler-lock owner cannot wait for mutators that need this lock.
    if load_i32(global_addr("pcc_tls_scheduler_lock_held_py"), 0) != 0:
        pcc_platform_abort()
        return -1
    if _world_init() != 0:
        return -1
    self_id = pcc_current_thread_id()
    lock = global_load_ptr("pcc_world_lock_py")
    cond = global_load_ptr("pcc_world_cond_py")
    trace_records = null()
    trace_count: int = 0
    trace_epoch: int = 0
    _thread_trace(16, 0, 0, null())
    pthread_mutex_lock(lock)
    while load_i32(global_addr("pcc_thread_stop_requested"), 0) != 0 and _world_i64(
        global_addr("pcc_stop_owner_thread_id_py")
    ) != self_id:
        if _suspend_permitted(lock) == 0:
            return -1
        epoch = _world_i64(global_addr("pcc_stop_epoch_py"))
        if trace_epoch != epoch:
            if ptr_is_null(trace_records):
                trace_records = stack_alloc(128)
            if trace_count < 8:
                store_i64(trace_records, trace_count * 16, epoch)
                store_i64(trace_records, trace_count * 16 + 8, 0)
            trace_count = trace_count + 1
            trace_epoch = epoch
        if load_i32(global_addr("pcc_tls_thread_parked_py"), 0) == 0 or _tls_i64(
            global_addr("pcc_tls_parked_epoch_py")
        ) != epoch:
            store_i32(global_addr("pcc_tls_thread_parked_py"), 0, 1)
            _tls_store_i64(global_addr("pcc_tls_parked_epoch_py"), epoch)
            _thread_record_mark(1, frame_address())
            _world_store_i64(
                global_addr("pcc_parked_thread_count_py"),
                _world_i64(global_addr("pcc_parked_thread_count_py")) + 1,
            )
            pcc_cond_broadcast(cond)
        if trace_count <= 8:
            offset: int = (trace_count - 1) * 16 + 8
            store_i64(trace_records, offset, load_i64(trace_records, offset) + 1)
        pthread_cond_wait(cond, lock)
    if load_i32(global_addr("pcc_thread_stop_requested"), 0) == 0 and load_i32(
        global_addr("pcc_tls_thread_parked_py"), 0
    ) != 0:
        store_i32(global_addr("pcc_tls_thread_parked_py"), 0, 0)
        _tls_store_i64(global_addr("pcc_tls_parked_epoch_py"), 0)
        _thread_record_mark(0, null())
    if load_i32(global_addr("pcc_thread_stop_requested"), 0) != 0:
        _world_store_i64(
            global_addr("pcc_stop_depth_py"),
            _world_i64(global_addr("pcc_stop_depth_py")) + 1,
        )
        epoch = _world_i64(global_addr("pcc_stop_epoch_py"))
        depth = _world_i64(global_addr("pcc_stop_depth_py"))
        pthread_mutex_unlock(lock)
        _flush_suspend_trace(trace_records, trace_count, trace_epoch, self_id)
        _thread_trace(18, epoch, depth, null())
        return 0
    atomic_store_i32(
        global_addr("pcc_thread_stop_requested"), 0, 1, "release"
    )
    _world_store_i64(global_addr("pcc_stop_owner_thread_id_py"), self_id)
    _world_store_i64(global_addr("pcc_stop_depth_py"), 1)
    epoch = _world_i64(global_addr("pcc_stop_epoch_py")) + 1
    if epoch <= 0:
        epoch = 1
    _world_store_i64(global_addr("pcc_stop_epoch_py"), epoch)
    _world_store_i64(global_addr("pcc_parked_thread_count_py"), 0)
    pcc_cond_broadcast(cond)
    while _world_i64(global_addr("pcc_live_thread_count_py")) > 1 and _world_i64(
        global_addr("pcc_parked_thread_count_py")
    ) + _world_i64(global_addr("pcc_exited_thread_count_py")) < _world_i64(
        global_addr("pcc_live_thread_count_py")
    ) - 1:
        if _suspend_permitted(lock) == 0:
            return -1
        pthread_cond_wait(cond, lock)
    live = _world_i64(global_addr("pcc_live_thread_count_py"))
    pthread_mutex_unlock(lock)
    _flush_suspend_trace(trace_records, trace_count, trace_epoch, self_id)
    _thread_trace(17, epoch, live, null())
    return 0


@c_abi_export("pcc_resume_world")
def pcc_resume_world() -> int:
    if _world_init() != 0:
        return -1
    self_id = pcc_current_thread_id()
    lock = global_load_ptr("pcc_world_lock_py")
    cond = global_load_ptr("pcc_world_cond_py")
    _thread_trace(19, 0, 0, null())
    pthread_mutex_lock(lock)
    epoch = _world_i64(global_addr("pcc_stop_epoch_py"))
    if load_i32(global_addr("pcc_thread_stop_requested"), 0) == 0 or _world_i64(
        global_addr("pcc_stop_owner_thread_id_py")
    ) != self_id:
        pthread_mutex_unlock(lock)
        _thread_trace(23, epoch, -1, null())
        return -1
    depth = _world_i64(global_addr("pcc_stop_depth_py"))
    if depth > 1:
        _world_store_i64(global_addr("pcc_stop_depth_py"), depth - 1)
        pthread_mutex_unlock(lock)
        _thread_trace(21, epoch, depth - 1, null())
        return 0
    atomic_store_i32(
        global_addr("pcc_thread_stop_requested"), 0, 0, "release"
    )
    _world_store_i64(global_addr("pcc_stop_owner_thread_id_py"), 0)
    _world_store_i64(global_addr("pcc_stop_depth_py"), 0)
    _world_store_i64(global_addr("pcc_parked_thread_count_py"), 0)
    pcc_cond_broadcast(cond)
    pthread_mutex_unlock(lock)
    _thread_trace(20, epoch, 0, null())
    return 0


@c_abi_export("pcc_thread_unregister_current")
def pcc_thread_unregister_current() -> None:
    if load_i32(global_addr("pcc_tls_scheduler_lock_held_py"), 0) != 0:
        pcc_platform_abort()
        return
    if pcc_thread_no_park_depth() != 0:
        pcc_platform_abort()
        return
    if load_i32(global_addr("pcc_tls_unregister_in_progress_py"), 0) != 0:
        pcc_platform_abort()
        return
    if _tls_i64(global_addr("pcc_tls_thread_id_py")) == 0:
        return
    # The owner must finish the active stop before leaving the live set.  This
    # check precedes exception/buffer cleanup because either can decref and
    # reenter runtime code.
    if pcc_thread_owns_stopped_world() != 0:
        pcc_platform_abort()
        return
    store_i32(global_addr("pcc_tls_unregister_in_progress_py"), 0, 1)
    # The current-exception slot owns its reference and, in the pcc-Python
    # runtime, publishes its native-TLS address through the common GC root
    # registry.  Retire both before the pthread's TLS storage disappears.
    py_clear_exception()
    pcc_gc_thread_unregister_buffers()
    if _world_init() != 0:
        pcc_platform_abort()
        return
    lock = global_load_ptr("pcc_world_lock_py")
    pthread_mutex_lock(lock)
    self_id = _tls_i64(global_addr("pcc_tls_thread_id_py"))
    # Cleanup above can decref and reenter.  Revalidate depth and STW
    # ownership under the same lock that protects live/owner accounting.
    if pcc_thread_no_park_depth() != 0 or (
        self_id != 0
        and load_i32(global_addr("pcc_thread_stop_requested"), 0) != 0
        and _world_i64(global_addr("pcc_stop_owner_thread_id_py")) == self_id
    ):
        pthread_mutex_unlock(lock)
        pcc_platform_abort()
        return
    if self_id != 0:
        if load_i32(global_addr("pcc_tls_thread_parked_py"), 0) != 0:
            store_i32(global_addr("pcc_tls_thread_parked_py"), 0, 0)
            parked = _world_i64(global_addr("pcc_parked_thread_count_py"))
            if parked > 0:
                _world_store_i64(
                    global_addr("pcc_parked_thread_count_py"), parked - 1
                )
            _tls_store_i64(global_addr("pcc_tls_parked_epoch_py"), 0)
        _tls_store_i64(global_addr("pcc_tls_thread_id_py"), 0)
        _thread_record_unlink()
        live = _world_i64(global_addr("pcc_live_thread_count_py"))
        if live > 0:
            _world_store_i64(global_addr("pcc_live_thread_count_py"), live - 1)
        pcc_cond_broadcast(global_load_ptr("pcc_world_cond_py"))
    pthread_mutex_unlock(lock)
    store_i32(global_addr("pcc_tls_unregister_in_progress_py"), 0, 0)


@c_abi_export("pcc_thread_trampoline_py")
def _thread_trampoline(start):
    thread_enter_event: int = 2
    thread_exit_event: int = 3
    entry = load_ptr(start, 0)
    arg = load_ptr(start, 8)
    handle = load_ptr(start, 16)
    free(start)
    pcc_current_thread_id()
    pcc_thread_safepoint()
    _thread_log(thread_enter_event, 0, handle)
    result = call_ptr1(entry, arg)
    # Callback return precedes result publication and TLS teardown.
    _thread_log(thread_exit_event, 0, handle)
    state_lock = load_ptr(handle, 8)
    if pcc_mutex_lock(state_lock) != 0:
        pcc_platform_abort()
        return result
    store_ptr(handle, 24, result)
    if pcc_mutex_unlock(state_lock) != 0:
        pcc_platform_abort()
        return result
    # Keep done clear while cleanup can park: join must keep polling until
    # this pthread leaves the live set.  Publishing done earlier lets join
    # block in pthread_join while teardown and a collector wait for its poll.
    pcc_thread_unregister_current()
    # Final handoff uses only the raw platform lock and allocator.  No logger,
    # safepoint-aware mutex wrapper, callback or registration may run here.
    # A detacher that sees done == 0 leaves the handle for this tail; otherwise
    # the same lock transfers disposal to it.  Both cannot own the handle.
    if pthread_mutex_lock(state_lock) != 0:
        pcc_platform_abort()
        return result
    store_i32(handle, 16, 1)
    detached = load_i32(handle, 20)
    done_cond = load_ptr(handle, 32)
    # The joiner waits on done_cond under state_lock; broadcasting before the
    # unlock is the last touch of a joinable handle.
    pthread_cond_broadcast(done_cond)
    if pthread_mutex_unlock(state_lock) != 0:
        pcc_platform_abort()
        return result
    if detached != 0:
        pthread_cond_destroy(done_cond)
        free(done_cond)
        pthread_mutex_destroy(state_lock)
        free(state_lock)
        free(handle)
    return result


@c_abi_export("pcc_thread_start")
def pcc_thread_start(out, entry, arg) -> int:
    thread_start_event: int = 1
    thread_start_failed_event: int = 6
    invalid_argument: int = -1
    handle_allocation_failed: int = -2
    state_lock_allocation_failed: int = -3
    start_allocation_failed: int = -4
    done_cond_allocation_failed: int = -5
    if ptr_is_null(out) or ptr_is_null(entry):
        _thread_log(thread_start_failed_event, invalid_argument, null())
        return -1
    # Handle: pthread@0, state_lock@8, done@16 (i32), detached@20 (i32),
    # result@24, done_cond@32 (signalled under state_lock when done is set).
    handle = malloc(40)
    if ptr_is_null(handle):
        _thread_log(thread_start_failed_event, handle_allocation_failed, null())
        return -1
    state_lock = pcc_mutex_new()
    if ptr_is_null(state_lock):
        free(handle)
        _thread_log(thread_start_failed_event, state_lock_allocation_failed, null())
        return -1
    done_cond = pcc_cond_new()
    if ptr_is_null(done_cond):
        pcc_mutex_free(state_lock)
        free(handle)
        _thread_log(thread_start_failed_event, done_cond_allocation_failed, null())
        return -1
    store_ptr(handle, 0, null())
    store_ptr(handle, 8, state_lock)
    store_i32(handle, 16, 0)
    store_i32(handle, 20, 0)
    store_ptr(handle, 24, null())
    store_ptr(handle, 32, done_cond)
    start = malloc(24)
    if ptr_is_null(start):
        pcc_cond_free(done_cond)
        pcc_mutex_free(state_lock)
        free(handle)
        _thread_log(thread_start_failed_event, start_allocation_failed, null())
        return -1
    store_ptr(start, 0, entry)
    store_ptr(start, 8, arg)
    store_ptr(start, 16, handle)
    # Attempt is logged before creation, even when the child runs immediately.
    _thread_log(thread_start_event, 0, handle)
    status = pthread_create(
        handle, null(), function_addr("pcc_thread_trampoline_py"), start
    )
    if status != 0:
        _thread_log(thread_start_failed_event, status, handle)
        free(start)
        pcc_cond_free(done_cond)
        pcc_mutex_free(state_lock)
        free(handle)
        return -1
    store_ptr(out, 0, handle)
    return 0


@c_abi_export("pcc_thread_join")
def pcc_thread_join(handle, result_out) -> int:
    thread_join_event: int = 4
    thread_joined_event: int = 5
    thread_join_failed_event: int = 7
    invalid_argument: int = -1
    if ptr_is_null(handle):
        _thread_log(thread_join_failed_event, invalid_argument, null())
        return -1
    _thread_log(thread_join_event, 0, handle)
    state_lock = load_ptr(handle, 8)
    done_cond = load_ptr(handle, 32)
    if pcc_mutex_lock(state_lock) != 0:
        _thread_log(thread_join_failed_event, invalid_argument, handle)
        return -1
    # The condition wait runs exited: a collector walks this joiner's stack
    # instead of waiting for it, so the exiting thread's teardown may park.
    while load_i32(handle, 16) == 0:
        if pcc_cond_wait(done_cond, state_lock) != 0:
            pcc_mutex_unlock(state_lock)
            _thread_log(thread_join_failed_event, invalid_argument, handle)
            return -1
    local_result = load_ptr(handle, 24)
    pcc_mutex_unlock(state_lock)
    joined = stack_alloc(8)
    store_ptr(joined, 0, null())
    result = pthread_join(load_ptr(handle, 0), joined)
    if ptr_is_null(result_out) == 0:
        value = local_result
        if result == 0:
            value = load_ptr(joined, 0)
        store_ptr(result_out, 0, value)
    # Correlation handle remains valid until the following free.
    if result == 0:
        _thread_log(thread_joined_event, 0, handle)
    else:
        _thread_log(thread_join_failed_event, result, handle)
    pcc_cond_free(done_cond)
    pcc_mutex_free(state_lock)
    free(handle)
    if result == 0:
        return 0
    return -1


@c_abi_export("pcc_thread_detach")
def pcc_thread_detach(handle) -> None:
    thread_detach_event: int = 8
    if ptr_is_null(handle):
        return
    # This void API records a request; the child may free handle after unlock.
    _thread_log(thread_detach_event, 0, handle)
    pthread_detach(load_ptr(handle, 0))
    state_lock = load_ptr(handle, 8)
    pcc_mutex_lock(state_lock)
    store_i32(handle, 20, 1)
    done = load_i32(handle, 16)
    pcc_mutex_unlock(state_lock)
    if done != 0:
        pcc_cond_free(load_ptr(handle, 32))
        pcc_mutex_free(state_lock)
        free(handle)
