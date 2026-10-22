"""An ordinary timer must bound a quiet IO wait in the real scheduler.

Host tests execute selected production function bodies with raw-slot stubs;
the integration test executes the unchanged public-vthread reproducer with
current compiler/runtime artifacts. No host model is native liveness evidence.
"""

import ast
import os
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "pcc/runtime/py/py_virtual_thread_runtime.py"


def production_function(name, namespace, source=SOURCE):
    module = ast.parse(source.read_text())
    function = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == name)
    function.decorator_list = []
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace[name]


@pytest.mark.parametrize("timeout,timer,expected", [
    (-1, None, -1), (-1, 105, 105), (0, None, 100), (0, 105, 100),
    (20, 105, 105), (2, 105, 102), (20, 95, 95), (-1, 95, 95),
])
def test_io_wait_deadline_includes_sleep_timer(timeout, timer, expected):
    head = None if timer is None else {8: timer}
    namespace = {"global_load_ptr": lambda name: head,
                 "ptr_is_null": lambda value: int(value is None),
                 "load_i64": lambda node, offset: node[offset]}
    deadline = production_function("_scheduler_wait_deadline_locked", namespace)
    assert deadline(100, timeout) == expected


def timer_model(existing_deadline=None, interrupt_status=0, previous_timer=False, allocation_failure=False, root_failure=False, wait_active=True):
    task = {32: 2, 56: None, 64: "old-io", 104: None, 120: 2}
    head = None if existing_deadline is None else {8: existing_deadline, 16: None, 24: "old-root"}
    if previous_timer:
        task.update({32: 3, 56: head, 64: None, 120: 1})
        head[0] = task
    slots = {"pcc_vthread_timer_head_py": head, "pcc_vthread_wait_active_py": int(wait_active),
             "pcc_vthread_waitset_ready_py": 1}
    node = {}
    events = []
    original_task = dict(task)
    def interrupt(waitset):
        # Notification is retained, and the scheduler mutex keeps wait_finish
        # behind the forthcoming commit. Failure must leave caller state alone.
        assert task == original_task
        assert slots["pcc_vthread_timer_head_py"] is head
        assert node == {}
        assert waitset == "pcc_vthread_waitset_py"
        events.append("interrupt")
        return interrupt_status
    def release(value):
        assert value[24] is not None
        value[24] = None
        value[0] = None
        events.append("release")
        events.append((2, 2, -1, -1))
    def cancel_io(task):
        events.append("cancel-io")
        task[64] = None
        return 1
    namespace = {
        "_timer_alloc": lambda: None if allocation_failure else node,
        "_timer_release": release,
        "pcc_gc_scheduler_root_register_handle": lambda node: None if root_failure else "root",
        "ptr_is_null": lambda value: int(value is None), "free": lambda node: events.append("free"),
        "ptr_eq": lambda left, right: int(left is right),
        "store_ptr": lambda node, offset, value: node.__setitem__(offset, value),
        "store_i64": lambda node, offset, value: node.__setitem__(offset, value),
        "load_ptr": lambda node, offset: node[offset],
        "load_i64": lambda node, offset: node[offset],
        "pcc_gc_store_root": lambda node, value: node.__setitem__(0, value),
        "global_load_ptr": lambda name: slots[name],
        "global_addr": lambda name: name, "load_i32": lambda slot, offset: slots[slot],
        "global_store_ptr": lambda name, value: slots.__setitem__(name, value),
        "null": lambda: None, "_effect": lambda *values: events.append(values),
        "pcc_io_waitset_interrupt": interrupt,
        "_checked": lambda value: value, "_scheduler_lock": lambda: 0,
        "_scheduler_unlock": lambda: events.append("unlock"),
        "_now_ms": lambda: 100, "_io_cancel": cancel_io,
        "pcc_runtime_monotonic_us": lambda: 100000,
    }
    production_function("_io_interrupt_locked", namespace)
    production_function("_timer_cancel", namespace)
    add = production_function("_timer_add", namespace)
    sleep = production_function("py_virtual_thread_sleep", namespace)
    namespace["py_virtual_thread_sleep"] = sleep
    condition_park = production_function("_condition_vm_park", namespace,
        ROOT / "pcc/runtime/py/py_threading.py")
    return add, sleep, condition_park, task, node, slots, events


def test_earlier_timer_interrupts_before_commit_under_scheduler_lock():
    for old_deadline in (None, 200):
        for wait_active in (False, True):
            add, sleep, condition_park, task, node, slots, events = timer_model(
                old_deadline, wait_active=wait_active)
            assert sleep(task, 5) == 0
            assert slots["pcc_vthread_timer_head_py"] is node
            if wait_active:
                assert events.index("interrupt") < events.index((1, 2, 1, -1))
            else:
                # A future preparation observes the new head under the lock;
                # there is no live OS wait to interrupt yet.
                assert "interrupt" not in events
            assert task[32] == 3 and task[120] == 1 and task[56] is node
            assert events[-2:] == ["cancel-io", "unlock"]
    for old_deadline in (100, 105):
        add, sleep, condition_park, task, node, slots, events = timer_model(old_deadline)
        assert sleep(task, 5) == 0
        assert slots["pcc_vthread_timer_head_py"] is not node
        assert "interrupt" not in events


def test_timer_interrupt_error_leaves_sleep_caller_unchanged():
    for previous_timer in (False, True):
        for failure in ("interrupt", "allocation", "root"):
            add, sleep, condition_park, task, node, slots, events = timer_model(
                existing_deadline=200, interrupt_status=-1 if failure == "interrupt" else 0,
                previous_timer=previous_timer, allocation_failure=failure == "allocation",
                root_failure=failure == "root")
            before = dict(task)
            original_head = slots["pcc_vthread_timer_head_py"]
            # Execute the actual timed-Condition-to-sleep ABI edge. Its caller
            # retires its FIFO entry on failure and may catch the exception.
            assert condition_park(task, 105000) == -1
            assert task == before
            assert slots["pcc_vthread_timer_head_py"] is original_head
            assert "cancel-io" not in events and "release" not in events
            assert not any(isinstance(event, tuple) for event in events)
            assert events.count("free") == (1 if failure == "root" else 0)
            assert events[0] == "interrupt"
            assert events[-1] == "unlock"


def test_deadline_snapshot_and_wait_ownership_share_the_scheduler_lock():
    source = SOURCE.read_text()
    poll = source.split("def py_virtual_thread_poll_io(timeout_ms: int) -> int:", 1)[1].split(
        '@c_abi_export("py_virtual_thread_io_wait_count")', 1)[0]
    snapshot = poll.index("wait_deadline = _scheduler_wait_deadline_locked(now, timeout_ms)")
    prepared = poll.index("pcc_io_waitset_wait_prepare(")
    claimed = poll.index('store_i32(global_addr("pcc_vthread_wait_active_py"), 0, 1)')
    unlocked = poll.index("_scheduler_unlock()", claimed)
    blocked = poll.index("pcc_io_waitset_wait_block(")
    assert snapshot < prepared < claimed < unlocked < blocked
    # Error branches may unlock and return; the successful preparation cannot.
    assert "wait = wait_deadline - now" in poll[snapshot:prepared]


PROBE = '''import pcc.virtual_thread as vt

def quiet_reader(fd: int):
    vt.readable(fd)
    return 1

def timer_cancel(reader):
    vt.sleep_current(5)
    vt.cancel(reader)
    return 42

def main():
    listener = vt.tcp_listen("127.0.0.1", 0, 16)
    reader = vt.spawn(quiet_reader, listener)
    timer = vt.spawn(timer_cancel, reader)
    try:
        attempts = 0
        while vt.outcome(timer) == vt.OUTCOME_PENDING and attempts < 20:
            vt.run(1, 100)
            attempts += 1
        if vt.outcome(timer) != vt.OUTCOME_RETURNED or vt.result(timer) != 42:
            raise RuntimeError("timer did not progress through a quiet IO wait")
        if vt.outcome(reader) != vt.OUTCOME_CANCELLED:
            raise RuntimeError("quiet reader was not cancelled")
        print("PCC_MIXED_TIMER_IO_OK")
    finally:
        vt.tcp_close(listener)

main()
'''


@pytest.mark.integration
def test_native_sleep_timer_progresses_past_quiet_fd(
    tmp_path, python_program_compiler, pcc_runtime_archive,
):
    from tests.python.process_timeout import run_process_group_timeout
    source = tmp_path / "mixed_timer_io.py"
    output = tmp_path / "mixed_timer_io"
    source.write_text(PROBE)
    python_program_compiler(str(source), str(output), backend="self",
        libpython_mode="off", ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        environment = dict(os.environ, PCC_GC_BACKEND=str(backend))
        result = run_process_group_timeout([str(output)], env=environment, timeout=5)
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout == "PCC_MIXED_TIMER_IO_OK\n" and result.stderr == ""
