"""Deterministic actual-body models of join, teardown and detach handoff.

The blocked teardown models a collector waiting for the joiner to stop. A
joiner waits on the handle's completion condition, which runs exited: the
collector counts it as stopped without a poll. A detacher contends through
the safepoint-aware state lock. These models are protocol evidence, not
native GC concurrency qualification.
"""

from __future__ import annotations

import threading
import time

import pytest

from test_runtime_log_thread_wiring import KernelModel


class ConcurrentKernelModel(KernelModel):
    @property
    def registered(self):
        return getattr(self.local, "registered", False)

    @registered.setter
    def registered(self, value):
        self.local.registered = value

    @property
    def locked(self):
        return getattr(self.local, "locked", False)

    @locked.setter
    def locked(self, value):
        self.local.locked = value

    def __init__(self, *, block_teardown=False, hold_final_lock=False,
                 pause_before_final_lock=False):
        self.local = threading.local()
        self.state_mutex = threading.Lock()
        self.teardown_entered = threading.Event()
        self.collector_released = threading.Event()
        self.retired = threading.Event()
        self.join_polled = threading.Event()
        self.join_exited = threading.Event()
        self.done_broadcast = threading.Event()
        self.final_lock_entered = threading.Event()
        self.final_lock_waiting = threading.Event()
        self.publish_allowed = threading.Event()
        self.detacher_contended = threading.Event()
        self.block_teardown = block_teardown
        self.hold_final_lock = hold_final_lock
        self.pause_before_final_lock = pause_before_final_lock
        self.errors = []
        self.disposals = []
        self.child = None
        super().__init__()
        self.namespace["sched_yield"] = lambda: time.sleep(0)

    def lock(self, pointer):
        assert self.registered and not self.locked and pointer not in self.freed
        while not self.state_mutex.acquire(blocking=False):
            if threading.current_thread().name == "detacher":
                self.detacher_contended.set()
            self.safepoint()
            time.sleep(0)
        self.locked = True
        self.actions.append(("raw_lock", pointer))
        return 0

    def raw_lock(self, pointer):
        assert not self.registered and not self.locked and pointer not in self.freed
        assert self.retired.is_set()
        self.final_lock_waiting.set()
        if self.pause_before_final_lock:
            assert self.publish_allowed.wait(2), "final handoff was not released"
        assert self.state_mutex.acquire(timeout=2), "raw completion lock stayed held"
        self.locked = True
        self.actions.append(("raw_lock", pointer))
        self.final_lock_entered.set()
        if self.hold_final_lock:
            assert self.publish_allowed.wait(2), "final handoff was not released"
        return 0

    def raw_unlock(self, pointer):
        assert self.locked and pointer not in self.freed
        self.locked = False
        self.actions.append(("raw_unlock", pointer))
        self.state_mutex.release()
        return 0

    def raw_broadcast(self, pointer):
        result = super().raw_broadcast(pointer)
        self.done_broadcast.set()
        return result

    def cond_wait(self, cond, mutex):
        # pcc_cond_wait runs exited: a collector counts this joiner as
        # stopped and walks its stack, so a teardown blocked on the stopped
        # world proceeds without the joiner reaching a poll. The kernel wait
        # releases the state lock and reacquires it after the broadcast.
        assert self.registered and self.locked
        self.join_exited.set()
        self.collector_released.set()
        self.locked = False
        self.actions.append(("raw_unlock", mutex))
        self.state_mutex.release()
        assert self.done_broadcast.wait(2), "completion broadcast never arrived"
        assert self.state_mutex.acquire(timeout=2), "state lock stayed held after completion"
        self.locked = True
        self.actions.append(("raw_lock", mutex))
        return 0

    def unregister(self):
        assert self.registered and not self.locked
        self.teardown_entered.set()
        if self.block_teardown:
            assert self.collector_released.wait(2), "collector never saw joiner's poll"
        super().unregister()
        self.retired.set()

    def safepoint(self):
        super().safepoint()
        if threading.current_thread() is threading.main_thread() and self.teardown_entered.is_set():
            self.join_polled.set()
            self.collector_released.set()

    def join(self, native_handle, out):
        # Calling the platform's non-polling join while teardown remains
        # registered is the old deadlock. Fail immediately in this model.
        assert self.retired.is_set(), "blocking join entered before teardown retired"
        assert self.child is not None
        self.child.join(timeout=2)
        assert not self.child.is_alive()
        assert self.errors == []
        return super().join(native_handle, out)

    def free(self, pointer):
        self.disposals.append((pointer, threading.current_thread().name))
        super().free(pointer)

    def launch_child(self):
        def run():
            self.registered = False
            try:
                assert self.namespace["_thread_trampoline"](self.start_record) == 77
                assert not self.registered and not self.locked
            except BaseException as error:
                self.errors.append(error)
        self.child = threading.Thread(target=run, name="worker", daemon=True)
        self.child.start()

    def finish_child(self):
        self.collector_released.set()
        self.publish_allowed.set()
        if self.child is not None:
            self.child.join(timeout=2)
            assert not self.child.is_alive()


def test_join_waits_exited_while_teardown_waits_for_stopped_world():
    model = ConcurrentKernelModel(block_teardown=True)
    assert model.start() == 0
    handle = model.load("out", 0)
    model.launch_child()
    try:
        assert model.teardown_entered.wait(2)
        assert model.namespace["pcc_thread_join"](handle, "result") == 0
        # The joiner released the stopped world by waiting exited, and its
        # platform join ran only after teardown retired (model.join).
        assert model.join_exited.is_set()
        assert model.load("result", 0) == 77
        assert model.freed == model.allocated
    finally:
        model.finish_child()
    assert model.errors == []


@pytest.mark.parametrize("boundary", ["blocked_teardown", "before_raw_lock", "after_completion"])
def test_detach_disposal_owner_is_unique_across_retirement(boundary):
    model = ConcurrentKernelModel(block_teardown=boundary == "blocked_teardown",
                                  pause_before_final_lock=boundary == "before_raw_lock")
    assert model.start() == 0
    handle = model.load("out", 0)
    state_lock = model.load(handle, 8)
    model.launch_child()
    try:
        if boundary == "blocked_teardown":
            assert model.teardown_entered.wait(2)
        elif boundary == "before_raw_lock":
            assert model.final_lock_waiting.wait(2)
        else:
            model.child.join(timeout=2)
            assert not model.child.is_alive()
        model.namespace["pcc_thread_detach"](handle)
        if boundary != "after_completion":
            assert handle not in model.freed and state_lock not in model.freed
    finally:
        model.finish_child()
    assert model.errors == []
    assert model.freed == model.allocated
    expected_owner = threading.current_thread().name if boundary == "after_completion" else "worker"
    assert [owner for pointer, owner in model.disposals if pointer == handle] == [expected_owner]
    assert [owner for pointer, owner in model.disposals if pointer == state_lock] == [expected_owner]


def test_detacher_contending_final_raw_lock_owns_completed_handle():
    model = ConcurrentKernelModel(hold_final_lock=True)
    assert model.start() == 0
    handle = model.load("out", 0)
    state_lock = model.load(handle, 8)
    model.launch_child()
    detacher = None
    try:
        assert model.final_lock_entered.wait(2)
        assert handle not in model.freed and state_lock not in model.freed
        def detach():
            model.registered = True
            try:
                model.namespace["pcc_thread_detach"](handle)
            except BaseException as error:
                model.errors.append(error)
        detacher = threading.Thread(target=detach, name="detacher", daemon=True)
        detacher.start()
        assert model.detacher_contended.wait(2)
        assert handle not in model.freed and state_lock not in model.freed
        model.publish_allowed.set()
        detacher.join(timeout=2)
        assert not detacher.is_alive()
    finally:
        model.finish_child()
        if detacher is not None:
            detacher.join(timeout=2)
            assert not detacher.is_alive()
    assert model.errors == []
    assert model.freed == model.allocated
    assert [owner for pointer, owner in model.disposals if pointer == handle] == ["detacher"]
    assert [owner for pointer, owner in model.disposals if pointer == state_lock] == ["detacher"]
