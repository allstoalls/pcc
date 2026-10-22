"""Condition notification tokens survive exited kernel waits, STW and VM timers."""

from __future__ import annotations

import ast
from pathlib import Path
import re
import subprocess
import threading
import time

import pytest

from pcc.runtime.py.py_abi_constants import (
    PYOBJECTHEADER_FLAGS_OFFSET, PY_FLAG_GC_PINNED, PY_TYPE_BOOL,
    PY_TYPE_FLOAT, PY_TYPE_INT, PY_TYPE_THREAD_CONDITION,
)


PORT = Path(__file__).resolve().parents[2] / "pcc/runtime/py/py_threading.py"


class _Raw:
    def __init__(self):
        self.slots = {}
        self.alive = True


class _Mutex:
    held = False


class _ConditionOracle:
    """Execute actual port functions; replace platform/raw GC intrinsics.

    The clock and kernel wakeups are deterministic. A kernel condition wait
    runs exited: it releases the primitive mutex, another party may act once
    per 1 ms tick (``on_poll``, holding the mutex), a broadcast ends the wait
    early, and a moving collector may relocate the receiver and poison its
    old address, rewriting only the registered borrowed frame and scheduler
    roots. Queue operations cannot read that stale address, and safepoints
    cannot hold the primitive mutex.
    """

    def __init__(self):
        self.now = 10_000_000
        self.ticks = 0
        self.relocations = 0
        self.on_exit = lambda: None
        self.frames = {}
        self.roots = {}
        self.pointers = {}
        self.vm_roots = 0
        self.polls = 0
        self.safepoints = 0
        self.broadcasts = 0
        self.recycle_states = []
        self.current = None
        self.carrier_pins = 0
        self.on_poll = lambda: None
        self.relocate = False
        self.timers = {}
        self.unparked = []
        self.error = None
        self.globals = {"py_None": object(),
                        "pcc_threading_condition_borrowed_frame_map": -3}
        for name in ("pcc_threading_vthread_waiter_free_py",
                     "pcc_threading_vthread_waiter_free_count_py",
                     "pcc_threading_vthread_waiter_mutex_bits_py"):
            value = _Raw()
            value.slots[0] = None if name.endswith("free_py") else 0
            self.globals[name] = value
        self.condition = _Raw()
        self.mutex = _Mutex()
        self.condition.slots = {8: PY_TYPE_THREAD_CONDITION, 12: 0,
                                16: self.mutex, 24: object(), 32: None, 40: None}
        self.namespace = self._namespace()
        parsed = ast.parse(PORT.read_text(), filename=str(PORT))
        functions = [node for node in parsed.body if isinstance(node, ast.FunctionDef)]
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(PORT), "exec"),
             self.namespace)

    def load(self, value, offset):
        assert value.alive, "read from stale relocated receiver"
        return value.slots.get(offset)

    def store(self, value, offset, item):
        assert value.alive, "write to stale relocated receiver"
        value.slots[offset] = item

    def pointer(self, value):
        if value is None:
            return 0
        self.pointers[id(value)] = value
        return id(value)

    def gc_load(self, _owner, slot):
        if isinstance(slot, tuple):
            return self.load(*slot)
        return self.load(slot, 0)

    def root_register(self, slot):
        handle = object()
        self.roots[handle] = slot
        return handle

    def root_unregister(self, handle):
        del self.roots[handle]

    def frame_enter(self, frame_map, slots):
        assert frame_map == -3
        self.frames[id(slots)] = slots

    def frame_leave(self, slots):
        del self.frames[id(slots)]

    def pin(self, value):
        if isinstance(value, _Raw):
            self.store(value, 12, (self.load(value, 12) or 0) | PY_FLAG_GC_PINNED)

    def unpin(self, value):
        if isinstance(value, _Raw):
            self.store(value, 12, (self.load(value, 12) or 0) & ~PY_FLAG_GC_PINNED)

    def lock(self, mutex):
        assert not mutex.held
        mutex.held = True
        return 0

    def unlock(self, mutex):
        assert mutex.held
        mutex.held = False
        return 0

    def kernel_wait(self, mutex, interval):
        """pthread_cond_(timed)wait, which the kernel runs exited.

        The mutex is released for the wait; a collector may run (and move
        the receiver) because the thread is exited; another party may act
        once per 1 ms tick while holding the mutex; any broadcast or signal
        since the wait began wakes it. Returns 0 when woken and 1 when
        ``interval`` ms (None: no deadline) passed, with the mutex held.
        """
        assert mutex.held and (interval is None or interval > 0)
        self.polls += 1
        start = self.broadcasts
        self.unlock(mutex)
        self.exited()
        self.lock(mutex)
        try:
            self.on_exit()
        finally:
            self.unlock(mutex)
        elapsed = 0
        while self.broadcasts == start and (interval is None or elapsed < interval):
            assert interval is not None or elapsed < 100_000, "untimed wait was never woken"
            elapsed += 1
            self.ticks += 1
            self.now += 1000
            self.lock(mutex)
            try:
                self.on_poll()
            finally:
                self.unlock(mutex)
        self.lock(mutex)
        return 0 if self.broadcasts != start else 1

    def cond_wait(self, _cond, mutex):
        return self.kernel_wait(mutex, None)

    def timedwait(self, _cond, mutex, interval):
        return self.kernel_wait(mutex, interval)

    def exited(self):
        """A collector acts while this thread is exited in a kernel wait."""
        assert not self.mutex.held, "exited while holding Condition"
        if self.carrier_pins:
            assert self.current.slots[12] & PY_FLAG_GC_PINNED
        if self.relocate:
            self._relocate()

    def _relocate(self):
        old = self.condition
        assert not self.load(old, 12) & PY_FLAG_GC_PINNED
        replacement = _Raw()
        replacement.slots = old.slots.copy()
        for frame in self.frames.values():
            for offset, item in frame.slots.items():
                if item is old:
                    frame.slots[offset] = replacement
        for slot in self.roots.values():
            if slot.slots[0] is old:
                slot.slots[0] = replacement
        old.alive = False
        self.condition = replacement
        self.relocations += 1

    def greater(self, first, second):
        # py_obj_gt over the oracle's numbers; any raw object is unorderable.
        if isinstance(first, _Raw) or isinstance(second, _Raw):
            self.error = (3, "'>' not supported")
            return 0
        return 1 if first > second else 0

    def safepoint(self):
        assert not self.mutex.held, "STW acknowledged while holding Condition"
        self.safepoints += 1
        if self.carrier_pins:
            assert self.current.slots[12] & PY_FLAG_GC_PINNED
        if self.relocate:
            self._relocate()

    def broadcast(self, _cond):
        self.broadcasts += 1
        return 0

    def vm_sleep(self, vm, delay):
        self.timers[vm] = self.now + delay * 1000
        return 0

    def vm_unpark(self, vm):
        self.timers.pop(vm, None)
        self.unparked.append(vm)
        return 0

    def note_vm(self, delta):
        self.vm_roots += delta
        assert self.vm_roots >= 0

    def carrier_pin(self, delta):
        self.carrier_pins += delta
        assert self.carrier_pins >= 0
        return 0

    def _namespace(self):
        def cas(value, offset, expected, desired, *_orders):
            old = self.load(value, offset)
            if old == expected:
                self.store(value, offset, desired)
            return old

        return {
            "c_abi_export": lambda _name: lambda function: function,
            "PY_TYPE_INT": PY_TYPE_INT, "PY_TYPE_FLOAT": PY_TYPE_FLOAT,
            "PY_TYPE_BOOL": PY_TYPE_BOOL, "PY_FLAG_GC_PINNED": PY_FLAG_GC_PINNED,
            "PYOBJECTHEADER_FLAGS_OFFSET": PYOBJECTHEADER_FLAGS_OFFSET,
            "_VTHREAD_WAITER_POOL_LIMIT": 4096,
            "null": lambda: None, "ptr_is_null": lambda value: value is None,
            "ptr_eq": lambda first, second: first is second,
            "is_tagged_int": lambda value: isinstance(value, (int, float)),
            "load_ptr": self.load, "load_i64": self.load, "load_i32": self.load,
            "store_ptr": self.store, "store_i64": self.store,
            "malloc": lambda _size: _Raw(), "free": lambda _value: None,
            "stack_alloc": lambda _size: _Raw(),
            "memset": lambda value, _byte, size: value.slots.update(
                {offset: None for offset in range(0, size, 8)}),
            "ptr_add": lambda value, offset: (value, offset),
            "ptr_to_int": self.pointer,
            "int_to_ptr": lambda value: None if value == 0 else self.pointers[value],
            "global_addr": self.globals.__getitem__,
            "global_load_ptr": lambda name: self.load(self.globals[name], 0)
                if name.endswith("free_py") else self.globals[name],
            "global_store_ptr": lambda name, value: self.store(self.globals[name], 0, value),
            "atomic_load_i64": lambda value, offset, _order: self.load(value, offset),
            "atomic_store_i64": lambda value, offset, item, _order: self.store(value, offset, item),
            "atomic_cas_i64": cas,
            "pcc_mutex_new": _Mutex, "pcc_mutex_free": lambda _value: None,
            "pcc_mutex_lock": self.lock, "pcc_mutex_unlock": self.unlock,
            "pcc_cond_wait": self.cond_wait,
            "pcc_cond_timedwait_ms": self.timedwait,
            "pcc_cond_signal": self.broadcast, "pcc_cond_broadcast": self.broadcast,
            "pcc_thread_safepoint": self.safepoint,
            "pcc_runtime_monotonic_us": lambda: self.now,
            "pcc_gc_frame_enter": self.frame_enter, "pcc_gc_frame_leave": self.frame_leave,
            "pcc_gc_scheduler_root_register_handle": self.root_register,
            "pcc_gc_scheduler_root_unregister_handle": self.root_unregister,
            "pcc_gc_store_root": lambda slot, value: self.store(slot, 0, value),
            "pcc_gc_load_ptr": self.gc_load,
            "pcc_gc_pin": self.pin, "pcc_gc_unpin": self.unpin,
            "py_incref_extern": lambda _value: None, "py_decref_extern": lambda _value: None,
            "py_virtual_thread_current": lambda: self.current,
            "py_virtual_thread_pin_enter": lambda _vm, _reason: self.carrier_pin(1),
            "py_virtual_thread_pin_leave": lambda _vm: self.carrier_pin(-1),
            "py_virtual_thread_park": lambda _vm: 0,
            "py_virtual_thread_sleep": self.vm_sleep,
            "py_virtual_thread_unpark": self.vm_unpark,
            "py_virtual_thread_cancel_timer": lambda vm: self.timers.pop(vm, None) is not None,
            "pcc_vthread_effect_note_waiter_root_enter": lambda: self.note_vm(1),
            "pcc_vthread_effect_note_waiter_root_leave": lambda: self.note_vm(-1),
            "pcc_vthread_waiter_pool_note_allocation": lambda: None,
            "pcc_vthread_waiter_pool_note_reuse": lambda: None,
            "pcc_vthread_waiter_pool_note_cached": lambda _count: None,
            "py_float_to_f64": float, "py_err_occurred": lambda: self.error is not None,
            "py_obj_gt": self.greater, "py_int_from_i64": lambda value: value,
            "untag_int": lambda value: value,
            "store_f64": self.store, "load_f64": self.load,
            "py_exc_new": lambda tag, message: (tag, message),
            "py_raise_owned": lambda value: setattr(self, "error", value),
            "cstr": lambda value: value,
        }

    def wait(self, timeout):
        self.lock(self.mutex)
        result = self.namespace["py_threading_condition_wait_timeout"](self.condition, timeout)
        assert self.mutex.held
        self.unlock(self.mutex)
        assert not self.frames and not self.roots and self.vm_roots == 0
        assert self.load(self.condition, 32) is None and self.load(self.condition, 40) is None
        return result


def test_native_poll_never_succeeds_without_notify_and_heals_receiver():
    memory = _ConditionOracle()
    memory.relocate = True

    def notify_fourth_poll():
        if memory.ticks == 4:
            assert memory.namespace["py_threading_condition_notify"](memory.condition) == 0
            # The notifier removes the FIFO entry but cannot recycle its
            # native token before the waiting thread observes notification.
            assert len(memory.roots) == 1
            assert next(iter(memory.roots.values())).slots[24] == -2

    memory.on_poll = notify_fourth_poll
    assert memory.wait(None) == 0
    # One exited kernel wait, woken by the notify; the receiver moved while
    # the waiter was exited and every later access reloaded its root.
    assert memory.polls == 1 and memory.ticks == 4 and memory.relocations == 1


# A timeout that is not positive tries once without a kernel wait; a
# positive one is a single exited wait for its whole (rounded-up) duration.
@pytest.mark.parametrize("timeout,polls", [(0, 0), (-1, 0), (False, 0),
                                         (float("nan"), 0), (0.0001, 1), (0.013, 1)])
def test_native_real_timeout_is_false_and_returns_with_mutex(timeout, polls):
    memory = _ConditionOracle()
    assert memory.wait(timeout) == 2
    assert memory.polls == polls


def test_notify_before_deadline_returns_true():
    memory = _ConditionOracle()
    memory.on_poll = lambda: memory.namespace["py_threading_condition_notify"](memory.condition)
    assert memory.wait(1.0) == 0
    # The notify wakes the wait at its first tick, not at the deadline.
    assert memory.now == 10_001_000


def test_notify_as_the_kernel_wait_begins_is_published_without_lost_wakeup():
    memory = _ConditionOracle()
    # The waiter enqueued its token under the mutex; the kernel wait
    # releases the mutex atomically, so a notify in that instant is kept.
    memory.on_exit = lambda: memory.namespace["py_threading_condition_notify"](memory.condition)
    assert memory.wait(None) == 0
    assert memory.polls == 1 and memory.ticks == 0


def test_notify_after_native_deadline_cannot_turn_elapsed_wait_into_success():
    memory = _ConditionOracle()
    memory.on_poll = lambda: memory.namespace["py_threading_condition_notify"](memory.condition)
    assert memory.wait(0.001) == 2
    assert memory.now == 10_001_000


@pytest.mark.parametrize("prior_pin", [0, PY_FLAG_GC_PINNED])
def test_native_fallback_roots_and_pins_current_vm_until_carrier_wait_returns(prior_pin):
    memory = _ConditionOracle()
    vm = _Raw()
    vm.slots[12] = prior_pin
    memory.current = vm
    memory.relocate = True
    assert memory.wait(0.002) == 2
    assert not memory.carrier_pins and vm.slots[12] == prior_pin


def test_positive_infinite_timeout_reports_overflow_without_releasing_lock():
    memory = _ConditionOracle()
    assert memory.wait(float("inf")) == -1
    assert memory.error[0] == 15
    assert memory.polls == 0


class _ConcurrentMutex:
    def __init__(self):
        self.lock = threading.Lock()
        self.owner = None


class _InjectedPollOracle(_ConditionOracle):
    """Force the actual emitted loop-latch poll while one waiter owns mutex.

    A kernel Condition wait cannot return until it reacquires that mutex,
    so while condition-owner parks at its latch holding it, the three
    followers stay inside their kernel waits. Those waits run exited: each
    follower counts as stopped the moment it enters, so the stop completes
    without them reaching a poll.
    """

    def __init__(self):
        self.followers = set()
        self.parked = set()
        self.started = threading.Event()
        self.release_stop = threading.Event()
        self.all_parked = threading.Event()
        super().__init__()
        self.mutex = _ConcurrentMutex()
        self.condition.slots[16] = self.mutex
        self.kernel_condition = threading.Condition(self.mutex.lock)
        self.namespace["pcc_cond_wait"] = self.cond_wait
        self.namespace["implicit_safepoint"] = self.implicit_safepoint
        parsed = ast.parse(PORT.read_text(), filename=str(PORT))
        native_wait = next(node for node in parsed.body
                           if isinstance(node, ast.FunctionDef) and node.name == "_condition_native_wait")
        for loop in ast.walk(native_wait):
            if isinstance(loop, ast.While):
                loop.body.append(ast.parse("implicit_safepoint()").body[0])
        ast.fix_missing_locations(native_wait)
        exec(compile(ast.Module(body=[native_wait], type_ignores=[]), str(PORT), "exec"),
             self.namespace)

    def _namespace(self):
        result = super()._namespace()
        result["pcc_mutex_new"] = _ConcurrentMutex
        return result

    def lock(self, mutex):
        while not mutex.lock.acquire(blocking=False):
            self.safepoint()
            time.sleep(0.0001)
        mutex.owner = threading.current_thread().name
        return 0

    def unlock(self, mutex):
        assert mutex.owner == threading.current_thread().name
        mutex.owner = None
        mutex.lock.release()
        return 0

    def _stopped(self, name):
        self.parked.add(name)
        if len(self.parked) == 4:
            self.all_parked.set()

    def safepoint(self):
        name = threading.current_thread().name
        if self.started.is_set() and not self.release_stop.is_set() and name != "MainThread":
            assert self.mutex.owner != name
            self._stopped(name)
            assert self.release_stop.wait(timeout=2)

    def implicit_safepoint(self):
        name = threading.current_thread().name
        if name == "condition-owner" and not self.started.is_set():
            assert self.mutex.owner == name
            assert len(self.followers) == 3
            self._stopped(name)
            self.started.set()
            assert self.release_stop.wait(timeout=2)

    def cond_wait(self, _condition, mutex):
        name = threading.current_thread().name
        assert mutex is self.mutex and mutex.owner == name
        if name != "condition-owner":
            self.followers.add(name)
            # The kernel wait runs exited: a stop counts it as stopped.
            self._stopped(name)
            self.kernel_condition.notify_all()
        mutex.owner = None
        # The real pthread_cond_wait releases the mutex and reacquires it
        # before returning: once condition-owner parks holding it, followers
        # cannot return, and stay exited.
        if name == "condition-owner":
            self.kernel_condition.wait(timeout=0.001)
            while len(self.followers) != 3:
                self.kernel_condition.wait(timeout=0.001)
        else:
            while not self.started.is_set():
                self.kernel_condition.wait(timeout=0.001)
            self.kernel_condition.wait(timeout=0.001)
        mutex.owner = name
        return 0

    def broadcast(self, _condition):
        assert self.mutex.owner == threading.current_thread().name
        self.kernel_condition.notify_all()
        return 0


def test_emitted_latch_poll_holding_condition_cannot_strand_other_native_waiters():
    memory = _InjectedPollOracle()
    results, errors = [], []

    def worker():
        try:
            memory.lock(memory.mutex)
            results.append(memory.namespace["py_threading_condition_wait"](memory.condition))
            memory.unlock(memory.mutex)
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=worker, name=name) for name in
               ("condition-owner", "condition-follower-1", "condition-follower-2", "condition-follower-3")]
    for thread in threads:
        thread.start()
    try:
        assert memory.started.wait(timeout=1)
        progressed = memory.all_parked.wait(timeout=0.1)
    finally:
        memory.release_stop.set()
        memory.lock(memory.mutex)
        memory.namespace["py_threading_condition_notify_all"](memory.condition)
        memory.unlock(memory.mutex)
        for thread in threads:
            thread.join(timeout=1)
    assert not any(thread.is_alive() for thread in threads)
    assert not errors, errors
    assert results == [0] * 4
    assert not memory.frames and not memory.roots
    assert progressed, "three kernel waiters could not acknowledge STW while the fourth held Condition"


def test_vm_timer_and_spurious_resume_do_not_report_notification():
    memory = _ConditionOracle()
    vm = _Raw()
    vm.slots[12] = PY_FLAG_GC_PINNED
    memory.current = vm
    memory.lock(memory.mutex)
    wait = memory.namespace["py_threading_condition_wait_vthread_timeout"]
    resume = memory.namespace["py_threading_condition_wait_resume"]
    assert wait(memory.condition, 0.015) == 1
    assert not memory.mutex.held and memory.vm_roots == 1
    memory.lock(memory.mutex)
    memory.now += 1000
    assert resume(memory.condition) == 1
    assert not memory.mutex.held and memory.vm_roots == 1
    memory.lock(memory.mutex)
    memory.now += 20_000
    assert resume(memory.condition) == 2
    assert memory.mutex.held and memory.vm_roots == 0
    assert not memory.roots and not memory.timers
    assert vm.slots[12] == PY_FLAG_GC_PINNED  # Existing pins are retained.


def test_mixed_fifo_notify_all_keeps_native_tokens_and_retires_vm_nodes():
    memory = _ConditionOracle()
    cond_root = _Raw()
    cond_root.slots[0] = memory.condition
    vm_root = _Raw()
    vm_root.slots[0] = _Raw()
    vm_root.slots[0].slots[12] = 0
    memory.lock(memory.mutex)
    enqueue = memory.namespace["_condition_enqueue"]
    first = enqueue(cond_root, None, -1)
    second = enqueue(cond_root, vm_root, 0)
    third = enqueue(cond_root, None, -1)
    assert memory.namespace["py_threading_condition_notify"](memory.condition) == 0
    assert first.slots[24] == -2 and len(memory.roots) == 3
    assert memory.load(memory.condition, 32) is second
    assert memory.namespace["py_threading_condition_notify_all"](memory.condition) == 0
    assert third.slots[24] == -2 and len(memory.roots) == 2
    assert memory.vm_roots == 0 and memory.unparked == [vm_root.slots[0]]
    assert memory.load(memory.condition, 32) is None
    memory.namespace["_condition_retire"](first)
    memory.namespace["_condition_retire"](third)
    assert not memory.roots


def test_expired_vm_does_not_consume_notification_and_cancel_cleans_timer():
    memory = _ConditionOracle()
    vm = _Raw()
    vm.slots[12] = 0
    memory.current = vm
    memory.lock(memory.mutex)
    assert memory.namespace["py_threading_condition_wait_vthread_timeout"](memory.condition, 0.001) == 1
    memory.lock(memory.mutex)
    memory.now += 2000
    assert memory.namespace["py_threading_condition_notify"](memory.condition) == 0
    assert not memory.unparked and len(memory.roots) == 1
    memory.unlock(memory.mutex)
    assert memory.namespace["py_threading_condition_wait_cancel"](memory.condition) == 0
    assert not memory.roots and not memory.timers and memory.vm_roots == 0
    assert memory.mutex.held


@pytest.mark.parametrize("triple", [
    "arm64-apple-darwin", "aarch64-unknown-linux-gnu",
    "x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc",
])
def test_condition_token_protocol_reaches_owned_emitter(tmp_path, monkeypatch, triple):
    from pcc.backend.owned_object_emit import emit_owned_object
    from pcc.frontends.python.owned_runtime_build import runtime_ir_passes
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "py_runtime_condition" / "py" / "py_threading.py"
    source.parent.mkdir(parents=True)
    source.write_text(PORT.read_text())
    output = tmp_path / "py_threading.ll"
    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", runtime_ir_passes(str(PORT.parent.parent)))
    compile_python(str(source), str(output), emit_llvm_only=True, python_library=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on",
                   target_triple=triple)
    text = output.read_text()
    # This is the changed runtime unit's actual IR, not a native call stub.
    assert "@py_threading_condition_wait_timeout" in text
    assert "@py_threading_condition_wait_resume" in text
    native_wait = re.search(
        r"^define [^\n]*@user_py_threading__condition_native_wait\([^\n]*\n(.*?)^}",
        text, re.M | re.S,
    )
    assert native_wait is not None
    body = native_wait.group(1)
    # No independent sleep polling: the native waiter blocks in the exited
    # kernel waits of _wait_until, which a notify ends at once.
    assert "@pcc_platform_sleep_ns(" not in body
    assert "while.latch" in body and "thread.safepoint" in body
    wait_until = re.search(
        r"^define [^\n]*@user_py_threading__wait_until\([^\n]*\n(.*?)^}",
        text, re.M | re.S,
    )
    assert wait_until is not None
    assert "@pcc_cond_wait(" in wait_until.group(1)
    assert "@pcc_cond_timedwait_ms(" in wait_until.group(1)
    # Follow the actual generated CFG, including inlined helper branches.
    # A kernel condition wait requires the primitive mutex: every wait is
    # entered holding it (the kernel releases it atomically while exited).
    blocks = {}
    label = None
    for line in body.splitlines():
        block_label = re.fullmatch(r"([-.\w]+):", line.strip())
        if block_label:
            label = block_label.group(1)
            blocks[label] = []
        elif label and line.strip():
            blocks[label].append(line)
    pending = [("entry", True)]
    visited = set()
    waits = 0
    while pending:
        label, held = pending.pop()
        if (label, held) in visited:
            continue
        visited.add((label, held))
        for line in blocks[label]:
            if re.search(r"\bcall [^\n]*@pcc_mutex_unlock\(", line):
                held = False
            elif re.search(r"\bcall [^\n]*@pcc_mutex_lock\(", line):
                held = True
            elif re.search(r"\bcall [^\n]*@(user_py_threading__wait_until|pcc_cond_wait|pcc_cond_timedwait_ms)\(", line):
                assert held, label
                waits += 1
        successors = re.findall(r"\blabel %([-.\w]+)", blocks[label][-1])
        pending.extend((successor, held) for successor in successors)
    assert waits > 0
    assert re.search(r"store atomic i64 -2, ptr [^\n]* release", text)
    payload = emit_owned_object(text, triple)
    assert len(payload) > 64
    (tmp_path / "py_threading.o").write_bytes(payload)


def test_condition_timeout_and_resume_use_owned_runtime_entries(tmp_path):
    from pcc.backend.owned_object_emit import emit_owned_object
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "condition_calls.py"
    source.write_text("""from threading import Condition
condition = Condition()
def timed():
    return condition.wait(timeout=0.001)
def untimed():
    return condition.wait()
def dynamic(receiver):
    return receiver.wait(timeout=0.001)
def main():
    condition.acquire()
    result = timed()
    condition.notify_all()
    condition.release()
main()
""")
    output = tmp_path / "condition_calls.ll"
    compile_python(str(source), str(output), emit_llvm_only=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on")
    text = output.read_text()
    calls = set(re.findall(r"\bcall i64[^\n]*@([^(\s]+)\(", text))
    for name in ("py_threading_condition_wait_vthread_timeout",
                 "py_threading_condition_wait_vthread",
                 "py_threading_condition_wait_resume",
                 "py_threading_condition_wait_cancel",
                 "py_threading_condition_notify_all"):
        assert name in calls, name
    assert len(emit_owned_object(text, "arm64-apple-darwin")) > 64


def _load_dynamic_condition_functions(memory):
    source = PORT.with_name("py_obj_ops_dispatch.py")
    parsed = ast.parse(source.read_text(), filename=str(source))
    selected = [
        node for node in parsed.body
        if (isinstance(node, ast.FunctionDef)
            and (node.name.startswith("_py_sync_") or node.name == "_py_condition_wait_captures"))
        or (isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id.startswith("_SYNC_")
                    for target in node.targets))
    ]
    namespace = memory.namespace.copy()
    memory.globals.update({"py_True": object(), "py_False": object()})
    released = []

    def tuple_new(size):
        value = _Raw()
        value.items = [None] * size
        value.slots[12] = 0
        return value

    def function_new(entry, captures, _name, _self):
        value = _Raw()
        value.slots[12] = 0
        value.entry, value.captures = entry, captures
        return value

    namespace.update({
        "PY_TYPE_THREAD_CONDITION": PY_TYPE_THREAD_CONDITION,
        "PY_TYPE_THREAD_EVENT": -101, "PY_TYPE_THREAD_SEMAPHORE": -102,
        "py_tuple_new": tuple_new,
        "py_tuple_get": lambda value, index: value.items[index],
        "py_tuple_set_item": lambda value, index, item: value.items.__setitem__(index, item),
        "py_str_new": lambda value, _length: value,
        "py_int_from_i64": int,
        "py_func_new_bound": function_new,
        "py_decref": released.append,
        "strcmp": lambda first, second: 0 if first == second else 1,
        "_cstr_is_dunder_enter": lambda value: value == "__enter__",
        "_cstr_is_dunder_exit": lambda value: value == "__exit__",
        "_cstr_is_acquire": lambda value: value == "acquire",
        "_cstr_is_release": lambda value: value == "release",
    })
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(source), "exec"), namespace)
    return namespace, released


def test_dynamic_wait_uses_standard_signature_and_preserves_receiver_ownership():
    memory = _ConditionOracle()
    namespace, released = _load_dynamic_condition_functions(memory)
    fn = namespace["_py_sync_method_bound"](memory.condition, PY_TYPE_THREAD_CONDITION, "wait")
    captures, signature = fn.captures.items
    magic, names, kinds, has_defaults, defaults = signature.items
    assert magic == "__pcc_func_signature_v1__"
    assert names.items == ["timeout"] and kinds.items == [0]
    assert has_defaults.items == [memory.globals["py_True"]]
    assert defaults.items == [memory.globals["py_None"]]
    args = _Raw()
    args.items = [0.002]
    memory.lock(memory.mutex)
    result = fn.entry(captures, args)
    assert result is memory.globals["py_False"] and memory.mutex.held
    assert released.count(memory.condition) == 1
    assert not memory.frames and not memory.roots
    assert memory.condition.slots[12] == 0


def test_dynamic_notify_all_selects_distinct_entry_and_retires_every_native_token():
    memory = _ConditionOracle()
    namespace, _released = _load_dynamic_condition_functions(memory)
    fn = namespace["_py_sync_method_bound"](memory.condition, PY_TYPE_THREAD_CONDITION, "notify_all")
    assert fn.entry is namespace["_py_sync_notify_all_entry"]
    cond_root = _Raw()
    cond_root.slots[0] = memory.condition
    memory.lock(memory.mutex)
    first = memory.namespace["_condition_enqueue"](cond_root, None, -1)
    second = memory.namespace["_condition_enqueue"](cond_root, None, -1)
    assert fn.entry(fn.captures, None) is memory.globals["py_None"]
    assert first.slots[24] == second.slots[24] == -2
    assert len(memory.roots) == 2  # Still reclaimed by their waiting owners.
    memory.namespace["_condition_retire"](first)
    memory.namespace["_condition_retire"](second)
    assert not memory.roots


def test_dynamic_wait_preserves_timeout_type_error_and_restores_pins():
    memory = _ConditionOracle()
    namespace, released = _load_dynamic_condition_functions(memory)
    fn = namespace["_py_sync_method_bound"](memory.condition, PY_TYPE_THREAD_CONDITION, "wait")
    invalid = _Raw()
    invalid.slots.update({8: 4, 12: 0})
    args = _Raw()
    args.items = [invalid]
    memory.lock(memory.mutex)
    assert fn.entry(fn.captures.items[0], args) is None
    assert memory.error[0] == 3
    assert memory.mutex.held and memory.polls == 0
    assert released.count(memory.condition) == released.count(invalid) == 1
    assert invalid.slots[12] == memory.condition.slots[12] == 0


def test_dynamic_condition_dispatch_reaches_owned_emitter(tmp_path, monkeypatch):
    from pcc.backend.owned_object_emit import emit_owned_object
    from pcc.frontends.python.owned_runtime_build import runtime_ir_passes
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "py_runtime_dispatch" / "py" / "py_obj_ops_dispatch.py"
    source.parent.mkdir(parents=True)
    source.write_text(PORT.with_name("py_obj_ops_dispatch.py").read_text())
    output = tmp_path / "py_obj_ops_dispatch.ll"
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", runtime_ir_passes(str(PORT.parent.parent)))
    compile_python(str(source), str(output), emit_llvm_only=True, python_library=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on",
                   target_triple="arm64-apple-darwin")
    text = output.read_text()
    assert re.search(r"\bcall i64[^\n]*@py_threading_condition_wait_timeout\(", text)
    assert re.search(r"\bcall i64[^\n]*@py_threading_condition_notify_all\(", text)
    assert len(emit_owned_object(text, "arm64-apple-darwin")) > 64


def test_owned_condition_provider_forwards_optional_timeout(monkeypatch):
    import pcc.stdlib.threading as owned
    pointer = object()
    calls = []
    monkeypatch.setattr(owned, "_condition_new", lambda _lock: pointer)
    monkeypatch.setattr(owned, "_condition_wait_timeout",
                        lambda receiver, timeout: calls.append((receiver, timeout)) or 2)
    condition = owned.Condition()
    for timeout in (None, 0, -1, 0.02):
        assert condition.wait(timeout=timeout) is False
    assert calls == [(pointer, timeout) for timeout in (None, 0, -1, 0.02)]


PROGRAM = '''import gc
from threading import Condition, Thread
from pcc.extern import extern, c_int64
from pcc.unsafe import malloc, free, atomic_load_i64, atomic_store_i64, atomic_rmw_i64

backend = extern("pcc_gc_backend", (), c_int64)
condition = Condition()
# enrolled count@0, notify issued@8, good return count@16, bad return count@24.
flags = malloc(32)
atomic_store_i64(flags, 0, 0, "release")
atomic_store_i64(flags, 8, 0, "release")
atomic_store_i64(flags, 16, 0, "release")
atomic_store_i64(flags, 24, 0, "release")

def waiter():
    condition.acquire()
    atomic_rmw_i64("add", flags, 0, 1, "acq_rel")
    notified = condition.wait()
    if notified and atomic_load_i64(flags, 8, "acquire") == 1:
        atomic_rmw_i64("add", flags, 16, 1, "acq_rel")
    else:
        atomic_rmw_i64("add", flags, 24, 1, "acq_rel")
    condition.release()

def timed_waiter():
    condition.acquire()
    atomic_store_i64(flags, 0, 1, "release")
    notified = condition.wait(timeout=2.0)
    if notified and atomic_load_i64(flags, 8, "acquire") == 1:
        atomic_store_i64(flags, 16, 1, "release")
    else:
        atomic_store_i64(flags, 24, 1, "release")
    condition.release()

def dynamic_wait(receiver, timeout):
    return receiver.wait(timeout=timeout)

def dynamic_notify_all(receiver):
    receiver.notify_all()

def reset():
    atomic_store_i64(flags, 0, 0, "release")
    atomic_store_i64(flags, 8, 0, "release")
    atomic_store_i64(flags, 16, 0, "release")
    atomic_store_i64(flags, 24, 0, "release")

def collect_then_notify(all_waiters):
    wanted = 4 if all_waiters else 1
    threads = []
    index = 0
    while index < wanted:
        thread = Thread(target=waiter)
        threads.append(thread)
        thread.start()
        index = index + 1
    while atomic_load_i64(flags, 0, "acquire") < wanted:
        pass
    # The last enrollment happened under this mutex. Its acquisition proves
    # every waiter has entered wait and atomically released the primitive.
    condition.acquire()
    assert atomic_load_i64(flags, 16, "acquire") == 0
    assert atomic_load_i64(flags, 24, "acquire") == 0
    condition.release()
    gc.collect()
    condition.acquire()
    # Internal bounded polls must not make Condition.wait return early.
    assert atomic_load_i64(flags, 16, "acquire") == 0
    assert atomic_load_i64(flags, 24, "acquire") == 0
    atomic_store_i64(flags, 8, 1, "release")
    if all_waiters:
        dynamic_notify_all(condition)
    else:
        condition.notify()
    condition.release()
    for thread in threads:
        thread.join()
    assert atomic_load_i64(flags, 16, "acquire") == wanted
    assert atomic_load_i64(flags, 24, "acquire") == 0
    reset()

def main():
    collect_then_notify(False)
    collect_then_notify(True)
    condition.acquire()
    assert condition.wait(0) is False
    assert condition.wait(timeout=-0.1) is False
    assert condition.wait(timeout=0.02) is False
    assert dynamic_wait(condition, 0.02) is False
    try:
        condition.wait(timeout="invalid")
        assert False
    except TypeError:
        pass
    try:
        dynamic_wait(condition, "invalid")
        assert False
    except TypeError:
        pass
    condition.release()
    thread = Thread(target=timed_waiter)
    thread.start()
    while atomic_load_i64(flags, 0, "acquire") == 0:
        pass
    condition.acquire()
    assert atomic_load_i64(flags, 16, "acquire") == 0
    atomic_store_i64(flags, 8, 1, "release")
    condition.notify()
    condition.release()
    thread.join()
    assert atomic_load_i64(flags, 16, "acquire") == 1
    assert atomic_load_i64(flags, 24, "acquire") == 0
    free(flags)
    print("CONDITION_STW_TIMEOUT_OK", backend())
main()
'''


def test_condition_semantic_program_reaches_owned_emitter(tmp_path):
    from pcc.backend.owned_object_emit import emit_owned_object
    from pcc.frontends.python.pipeline import compile_python
    source = tmp_path / "condition_semantics.py"
    source.write_text(PROGRAM)
    output = tmp_path / "condition_semantics.ll"
    compile_python(str(source), str(output), emit_llvm_only=True,
                   backend="self", libpython_mode="off", ir_scaffold_mode="on")
    assert len(emit_owned_object(output.read_text(), "arm64-apple-darwin")) > 64


@pytest.mark.integration
def test_native_condition_collect_notify_all_and_timeouts_all_gcs(
    tmp_path, monkeypatch, threaded_pcc_runtime_archive, python_program_compiler,
):
    import os
    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    source = tmp_path / "condition_semantics.py"
    source.write_text(PROGRAM)
    binary = tmp_path / "condition_semantics"
    python_program_compiler(str(source), str(binary), backend="self", libpython_mode="off",
                            ir_scaffold_mode="on", runtime_archive=str(threaded_pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=30,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend)))
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stderr == "", (backend, result.stderr)
        assert result.stdout == f"CONDITION_STW_TIMEOUT_OK {backend}\n"
