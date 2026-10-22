"""Execute actual runtime bodies with checked allocation/lock/TLS models.

These models check lifecycle instrumentation, not native concurrency or GC.
"""

from __future__ import annotations

import __future__
import ast
import json
from pathlib import Path

import pytest


RUNTIME = Path(__file__).resolve().parents[2] / "pcc" / "runtime" / "py"


def _namespace(filename):
    tree = ast.parse((RUNTIME / filename).read_text())
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    for function in functions:
        function.decorator_list = []
    module = ast.Module(body=functions, type_ignores=[])
    namespace = {"null": lambda: None, "ptr_is_null": lambda value: value is None}
    exec(compile(module, filename, "exec", flags=__future__.annotations.compiler_flag), namespace)
    return namespace


def _logger(tokens):
    namespace = _namespace("py_runtime_log.py")
    state = {"pcc_log_init_state": 2, "pcc_diagnostics_runtime_log_fast_state": 1,
             "pcc_log_emitting": 0, "pcc_log_thread_trace_dropped": 0}
    registrations = []
    namespace.update(
        cstr=lambda text: text.encode() + b"\0",
        strlen=lambda text: len(bytes(text).split(b"\0", 1)[0]),
        load_i8=lambda pointer, offset: pointer[offset],
        ptr_add=lambda pointer, offset: pointer[offset:],
        global_addr=lambda name: name,
        load_i32=lambda name, offset: state[name],
        atomic_load_i32=lambda name, offset, order: state[name],
        pcc_current_thread_id=lambda: registrations.append("registered"),
    )
    state["pcc_log_mask"] = namespace["_parse_tokens"](tokens)
    return namespace, state, registrations


@pytest.mark.parametrize(
    "tokens,enabled",
    [(None, False), (b"\0", False), (b"gc\0", False), (b"threaded\0", False),
     (b"thread\0", True), (b"gc, thread\0", True), (b"thread, gc\0", True),
     (b"all\0", True), (b"gc,all\0", True), (b"1\0", True)],
)
def test_thread_category_filter_uses_existing_token_rules(tokens, enabled):
    namespace, _, registrations = _logger(tokens)
    assert bool(namespace["_code_enabled"](9)) is enabled
    assert bool(namespace["pcc_diagnostics_runtime_log_enabled"](b"thread\0")) is enabled
    assert namespace["_category_mask"](b"thread\0") == 256
    assert namespace["_category_from_code"](9) == b"thread\0"
    assert len(registrations) == 2


def test_disabled_coded_event_neither_initializes_nor_registers_or_writes():
    namespace, state, registrations = _logger(b"thread\0")
    state["pcc_diagnostics_runtime_log_fast_state"] = 0
    state["pcc_log_init_state"] = 1
    namespace["_init_once"] = lambda: pytest.fail("disabled event initialized logger")
    namespace["pcc_diagnostics_runtime_log_event"] = lambda *args: pytest.fail("disabled event wrote")
    namespace["pcc_diagnostics_runtime_log_event_code"](9, 1, 0, 0, None)
    assert registrations == []


def test_thread_events_have_stable_named_local_integer_codes():
    namespace, _, _ = _logger(None)
    names = ["start", "enter", "exit", "join", "joined", "start_failed", "join_failed", "detach"]
    for code, name in enumerate(names, 1):
        assert namespace["_event_from_code"](9, code) == name.encode() + b"\0"
    assert namespace["_event_from_code"](9, 999) == b"thread_event\0"
    tree = ast.parse((RUNTIME / "freestanding_thread_kernel_pthread.py").read_text())
    local_codes = {}
    for function in tree.body:
        if isinstance(function, ast.FunctionDef):
            for node in function.body:
                if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    if node.target.id.startswith("thread_") and node.target.id.endswith("_event"):
                        local_codes[node.target.id] = ast.literal_eval(node.value)
    assert local_codes == {"thread_" + name + "_event": code for code, name in enumerate(names, 1)}


class KernelModel:
    def __init__(self, allocation_failure=0, create_status=0, join_status=0):
        self.namespace = _namespace("freestanding_thread_kernel_pthread.py")
        self.memory = {}
        self.allocated = set()
        self.freed = set()
        self.allocation_count = 0
        self.allocation_failure = allocation_failure
        self.create_status = create_status
        self.join_status = join_status
        self.locked = False
        self.registered = True
        self.events = []
        self.actions = []
        self.start_record = None
        self.namespace.update(
            malloc=self.allocate, free=self.free, pcc_mutex_new=lambda: self.allocate(1),
            pcc_mutex_free=self.free, load_ptr=self.load, load_i32=self.load,
            store_ptr=self.store, store_i32=self.store,
            pcc_mutex_lock=self.lock, pcc_mutex_unlock=self.unlock,
            pthread_mutex_lock=self.raw_lock, pthread_mutex_unlock=self.raw_unlock,
            pthread_mutex_destroy=self.raw_destroy,
            pcc_cond_new=lambda: self.allocate(1), pcc_cond_free=self.free,
            pcc_cond_wait=self.cond_wait,
            pthread_cond_broadcast=self.raw_broadcast,
            pthread_cond_destroy=self.raw_destroy,
            function_addr=lambda name: name, pthread_create=self.create,
            pthread_join=self.join, pthread_detach=lambda handle: 0,
            stack_alloc=lambda size: "stack", call_ptr1=self.callback,
            pcc_current_thread_id=self.register,
            pcc_thread_unregister_current=self.unregister,
            pcc_thread_safepoint=self.safepoint,
            pcc_diagnostics_runtime_log_event_code=self.log,
            pcc_platform_abort=lambda: pytest.fail("unexpected abort"),
            sched_yield=lambda: pytest.fail("unexpected wait"),
        )

    def allocate(self, size):
        self.allocation_count += 1
        if self.allocation_count == self.allocation_failure:
            return None
        pointer = self.allocation_count
        self.allocated.add(pointer)
        return pointer

    def free(self, pointer):
        assert pointer in self.allocated and pointer not in self.freed
        self.freed.add(pointer)
        self.actions.append(("free", pointer))

    def load(self, pointer, offset):
        assert pointer not in self.freed
        return self.memory[pointer, offset]

    def store(self, pointer, offset, value):
        assert pointer not in self.freed
        self.memory[pointer, offset] = value

    def lock(self, pointer):
        assert self.registered
        return self.raw_lock(pointer)

    def raw_lock(self, pointer):
        assert pointer not in self.freed and not self.locked
        self.locked = True
        self.actions.append(("raw_lock", pointer))
        return 0

    def unlock(self, pointer):
        assert self.registered
        return self.raw_unlock(pointer)

    def raw_unlock(self, pointer):
        assert pointer not in self.freed and self.locked
        self.locked = False
        self.actions.append(("raw_unlock", pointer))
        return 0

    def raw_destroy(self, pointer):
        assert pointer not in self.freed and not self.locked
        self.actions.append(("raw_destroy", pointer))
        return 0

    def raw_broadcast(self, pointer):
        # The completion broadcast is part of the final handoff: it runs
        # under the raw state lock, after the thread left the live set.
        assert pointer not in self.freed and self.locked
        self.actions.append(("raw_broadcast", pointer))
        return 0

    def cond_wait(self, cond, mutex):
        # The sequential model joins a completed child, whose done flag is
        # already published: a join that waits here has lost the handoff.
        pytest.fail("join waited on a completed thread")

    def register(self):
        self.registered = True
        self.actions.append(("register",))
        return 1

    def unregister(self):
        assert self.registered and not self.locked
        self.registered = False
        self.actions.append(("unregister",))

    def safepoint(self):
        assert self.registered and not self.locked
        self.actions.append(("safepoint",))

    def log(self, category, event, status, reserved, pointer):
        assert category == 9 and reserved == 0
        assert self.registered and not self.locked and pointer not in self.freed
        self.events.append((event, status, pointer))
        self.actions.append(("log", event))

    def create(self, handle, attributes, entry, start):
        self.actions.append(("create",))
        self.start_record = start
        self.store(handle, 0, 1000)
        return self.create_status

    def callback(self, entry, arg):
        assert entry == "callback" and arg == "argument"
        self.actions.append(("callback",))
        return 77

    def join(self, native_handle, out):
        assert native_handle == 1000
        self.store(out, 0, 77)
        return self.join_status

    def start(self):
        return self.namespace["pcc_thread_start"]("out", "callback", "argument")

    def run_child(self):
        self.registered = False
        result = self.namespace["_thread_trampoline"](self.start_record)
        retired = max(index for index, action in enumerate(self.actions)
                      if action == ("unregister",))
        assert all(action[0] in {"raw_lock", "raw_unlock", "raw_broadcast", "raw_destroy", "free"}
                   for action in self.actions[retired + 1:])
        assert not self.registered
        self.registered = True  # Restore the separately registered caller.
        return result


# start() allocates the handle, its state lock, its completion condition
# and the start record, in that order; each failure has its own status.
@pytest.mark.parametrize("failed_allocation,status", [(1, -2), (2, -3), (3, -5), (4, -4)])
def test_start_allocation_failure_logs_after_cleanup(failed_allocation, status):
    model = KernelModel(allocation_failure=failed_allocation)
    assert model.start() == -1
    assert model.events == [(6, status, None)]
    assert model.freed == model.allocated
    assert ("create",) not in model.actions
    assert model.actions[-1] == ("log", 6)


def test_platform_create_failure_preserves_status_and_disposes_resources():
    model = KernelModel(create_status=11)
    assert model.start() == -1
    assert model.events == [(1, 0, 1), (6, 11, 1)]
    assert model.actions[:3] == [("log", 1), ("create",), ("log", 6)]
    assert model.freed == model.allocated


def test_invalid_start_join_and_null_detach_have_no_allocations():
    model = KernelModel()
    assert model.namespace["pcc_thread_start"](None, "callback", "argument") == -1
    assert model.namespace["pcc_thread_start"]("out", None, "argument") == -1
    assert model.namespace["pcc_thread_join"](None, "result") == -1
    model.namespace["pcc_thread_detach"](None)
    assert model.events == [(6, -1, None), (6, -1, None), (7, -1, None)]
    assert model.allocated == set()


@pytest.mark.parametrize("join_status,last_event", [(0, 5), (22, 7)])
def test_join_logs_outside_locks_before_disposal_and_never_after_unregister(join_status, last_event):
    model = KernelModel(join_status=join_status)
    assert model.start() == 0
    handle = model.load("out", 0)
    assert model.run_child() == 77
    assert model.namespace["pcc_thread_join"](handle, "result") == (0 if join_status == 0 else -1)
    assert model.load("result", 0) == 77
    assert model.events == [(1, 0, handle), (2, 0, handle), (3, 0, handle),
                            (4, 0, handle), (last_event, join_status, handle)]
    assert model.freed == model.allocated


@pytest.mark.parametrize("detach_before_return", [True, False])
def test_detach_logs_before_either_disposal_order(detach_before_return):
    model = KernelModel()
    assert model.start() == 0
    handle = model.load("out", 0)
    if detach_before_return:
        model.namespace["pcc_thread_detach"](handle)
    assert model.run_child() == 77
    if not detach_before_return:
        model.namespace["pcc_thread_detach"](handle)
    assert [event for event, _, _ in model.events] == ([1, 8, 2, 3] if detach_before_return else [1, 2, 3, 8])
    assert model.freed == model.allocated


def test_instrumentation_is_confined_to_lock_free_lifecycle_boundaries():
    tree = ast.parse((RUNTIME / "freestanding_thread_kernel_pthread.py").read_text())
    instrumented = set()
    for function in tree.body:
        if not isinstance(function, ast.FunctionDef):
            continue
        calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
        if any(isinstance(node.func, ast.Name) and node.func.id == "_thread_log" for node in calls):
            instrumented.add(function.name)
    assert instrumented == {"_thread_trampoline", "pcc_thread_start", "pcc_thread_join", "pcc_thread_detach"}


class SuspendTripwire(Exception):
    pass


def _tripwire_kernel(filename, held=0, depth=0):
    namespace = _namespace(filename)
    state = {"pcc_tls_scheduler_lock_held_py": held, "pcc_tls_no_park_depth_py": depth}

    def abort():
        raise SuspendTripwire

    namespace.update(
        global_addr=lambda name: name,
        load_i32=lambda name, offset: state[name],
        store_i32=lambda name, offset, value: state.__setitem__(name, value),
        pcc_platform_abort=abort,
        _world_init=lambda: pytest.fail("tripwire must precede world initialization"),
        pcc_current_thread_id=lambda: pytest.fail("tripwire must not register"),
    )
    return namespace, state


# The pthread kernel is the only thread kernel (560c23fdf removed the single one).
@pytest.mark.parametrize("filename", ["freestanding_thread_kernel_pthread.py"])
def test_scheduler_marker_detects_missing_lease_before_safepoint(filename):
    namespace, state = _tripwire_kernel(filename, held=1)
    with pytest.raises(SuspendTripwire):
        namespace["pcc_thread_safepoint"]()
    state["pcc_tls_no_park_depth_py"] = 1
    namespace["pcc_thread_safepoint"]()
    assert state["pcc_tls_scheduler_lock_held_py"] == 1
    assert state["pcc_tls_no_park_depth_py"] == 1


# The pthread kernel is the only thread kernel (560c23fdf removed the single one).
@pytest.mark.parametrize("filename", ["freestanding_thread_kernel_pthread.py"])
@pytest.mark.parametrize("depth", [0, 1])
def test_stop_world_never_waits_with_scheduler_lock_held(filename, depth):
    namespace, _ = _tripwire_kernel(filename, held=1, depth=depth)
    with pytest.raises(SuspendTripwire):
        namespace["pcc_stop_the_world"]()
    with pytest.raises(SuspendTripwire):
        namespace["pcc_thread_unregister_current"]()


# The pthread kernel is the only thread kernel (560c23fdf removed the single one).
@pytest.mark.parametrize("filename", ["freestanding_thread_kernel_pthread.py"])
def test_scheduler_markers_are_independent_nonparking_state(filename):
    namespace, state = _tripwire_kernel(filename)
    namespace["pcc_thread_scheduler_lock_acquired"]()
    assert state == {"pcc_tls_scheduler_lock_held_py": 1, "pcc_tls_no_park_depth_py": 0}
    with pytest.raises(SuspendTripwire):
        namespace["pcc_thread_scheduler_lock_acquired"]()
    namespace["pcc_thread_scheduler_lock_released"]()
    assert state == {"pcc_tls_scheduler_lock_held_py": 0, "pcc_tls_no_park_depth_py": 0}
    with pytest.raises(SuspendTripwire):
        namespace["pcc_thread_scheduler_lock_released"]()


@pytest.mark.parametrize("lock_status,unlock_status", [(0, 0), (-1, 0), (0, -1)])
def test_scheduler_tracks_ownership_only_after_successful_operations(lock_status, unlock_status):
    namespace = _namespace("py_virtual_thread_runtime.py")
    actions = []

    def abort():
        actions.append("abort")
        raise SuspendTripwire

    namespace.update(
        _scheduler_init=lambda: 0,
        _scheduler_mutex=lambda: 42,
        pcc_current_thread_id=lambda: actions.append("register"),
        pcc_mutex_lock=lambda mutex: actions.append("lock") or lock_status,
        pcc_mutex_unlock=lambda mutex: actions.append("unlock") or unlock_status,
        pcc_thread_scheduler_lock_acquired=lambda: actions.append("acquired"),
        pcc_thread_scheduler_lock_released=lambda: actions.append("released"),
        pcc_thread_no_park_enter=lambda: actions.append("no_park_enter"),
        pcc_thread_no_park_exit=lambda: actions.append("no_park_exit"),
        pcc_platform_abort=abort,
        pcc_diagnostics_runtime_log_event_code=lambda *args: actions.append(("trace", args[1])),
    )
    assert namespace["_scheduler_lock"]() == lock_status
    if lock_status != 0:
        assert actions == ["register", ("trace", 9), "lock", ("trace", 12)]
        return
    assert actions == ["register", ("trace", 9), "lock", "acquired", "no_park_enter"]
    if unlock_status == 0:
        namespace["_scheduler_unlock"]()
        assert actions[-5:] == ["unlock", "released", "no_park_exit", ("trace", 10), ("trace", 11)]
    else:
        with pytest.raises(SuspendTripwire):
            namespace["_scheduler_unlock"]()
        assert actions[-2:] == ["unlock", "abort"]


class RawLogModel:
    def __init__(self, format_name="json", path=b"trace.log\0", open_status=17):
        self.namespace, self.state, self.registrations = _logger(b"thread\0")
        self.state.update(pcc_log_json=int(format_name == "json"), pcc_log_write_lock=0)
        self.output = bytearray()
        self.opens = []
        self.closes = []
        self.write_results = []
        self.write_calls = []
        self.on_write = None
        self.depth = 0
        self.namespace.update(
            store_i32=self.store_i32,
            atomic_store_i32=lambda name, offset, value, order: self.state.__setitem__(name, value),
            atomic_cas_i32=self.cas,
            atomic_rmw_i64=self.rmw,
            global_load_ptr=lambda name: path,
            stack_alloc=lambda size: memoryview(bytearray(size)),
            load_i32=self.load_i32,
            load_i64=lambda pointer, offset: int.from_bytes(pointer[offset:offset + 8], "little", signed=True),
            store_i8=lambda pointer, offset, value: pointer.__setitem__(offset, value),
            store_ptr=lambda pointer, offset, value: pointer.__setitem__(slice(offset, offset + 8), int(value).to_bytes(8, "little")),
            pcc_current_thread_id=self.identity,
            pcc_thread_no_park_depth=lambda: self.depth,
            pcc_runtime_now_us=lambda: 123456700,
            unsigned_div_i64=lambda left, right: (left & ((1 << 64) - 1)) // right,
            unsigned_rem_i64=lambda left, right: (left & ((1 << 64) - 1)) % right,
            open_file=lambda name, access, disposition: self.opens.append((name, access, disposition)) or open_status,
            write=self.write,
            close=lambda fd: self.closes.append(fd),
            thread_safepoint=lambda: pytest.fail("trace path parked"),
        )

    def load_i32(self, pointer, offset):
        if isinstance(pointer, str):
            return self.state[pointer]
        return int.from_bytes(pointer[offset:offset + 4], "little", signed=True)

    def store_i32(self, pointer, offset, value):
        if isinstance(pointer, str):
            self.state[pointer] = value
        else:
            pointer[offset:offset + 4] = value.to_bytes(4, "little", signed=True)

    def cas(self, name, offset, expected, replacement, success, failure):
        previous = self.state[name]
        if previous == expected:
            self.state[name] = replacement
        return previous

    def rmw(self, op, name, offset, value, order):
        previous = self.state[name]
        self.state[name] = value if op == "xchg" else previous + value
        return previous

    def identity(self):
        assert self.state["pcc_log_write_lock"] == 0
        self.registrations.append("registered")
        return 7

    def write(self, fd, pointer, length):
        assert self.state["pcc_log_write_lock"] == 1
        self.write_calls.append((fd, bytes(pointer[:length])))
        if self.on_write is not None:
            callback, self.on_write = self.on_write, None
            callback()
        result = self.write_results.pop(0) if self.write_results else length
        if result > 0:
            self.output.extend(pointer[:result])
        return result

    def emit(self, event=13):
        self.namespace["pcc_diagnostics_runtime_log_event_code"](9, event, 11, 2, None)

    def records(self):
        return [json.loads(line) for line in self.output.decode().splitlines()]


@pytest.mark.parametrize("format_name", ["json", "text"])
def test_raw_sink_keeps_wire_values_and_append_open(format_name):
    model = RawLogModel(format_name)
    model.namespace["pcc_diagnostics_runtime_log_event"](b"thread\0", b"probe\0", -9, 2, 0x12)
    assert model.opens == [(b"trace.log\0", 1, 2)]
    assert model.closes == [17]
    if format_name == "json":
        assert model.records() == [{"schema": "pcc.diagnostics.runtime_log.v1", "ts": 123,
                                    "thread": 7, "category": "thread", "event": "probe",
                                    "value0": -9, "value1": 2, "ptr": "0x12"}]
    else:
        assert model.output.decode() == "[pcc.thread] ts=123 thread=7 event=probe value0=-9 value1=2 ptr=0x12\n"
    assert model.state["pcc_log_write_lock"] == model.state["pcc_log_emitting"] == 0


@pytest.mark.parametrize("path,open_status,opens", [(None, 17, 0), (b"\0", 17, 0), (b"-\0", 17, 0), (b"bad\0", -1, 1)])
def test_raw_sink_stderr_fallback_never_closes_stderr(path, open_status, opens):
    model = RawLogModel(path=path, open_status=open_status)
    model.emit()
    assert len(model.opens) == opens and model.closes == []
    assert {fd for fd, _ in model.write_calls} == {2}
    assert model.records()[0]["event"] == "safepoint_stop_observed"


def test_raw_sink_retries_interruptions_and_partial_writes():
    model = RawLogModel()
    model.write_results = [-4, 2, 3]
    model.emit()
    assert model.records()[0]["value0"] == 11
    assert model.write_calls[0][1] == model.write_calls[1][1]
    assert model.write_calls[2][1] == model.write_calls[1][1][2:]
    assert model.closes == [17]


@pytest.mark.parametrize("error", [0, -5])
def test_raw_sink_write_failure_is_bounded_and_releases_sink(error):
    model = RawLogModel()
    model.state["pcc_log_write_lock"] = 1
    model.write_results = [error]
    model.namespace["_write_text"](17, b"hello\0")
    assert len(model.write_calls) == 1 and model.output == b""
    model.state["pcc_log_write_lock"] = 0
    model.emit()
    assert model.state["pcc_log_write_lock"] == model.state["pcc_log_emitting"] == 0


@pytest.mark.parametrize("block", ["sink", "recursion", "no_park"])
def test_transition_sink_drops_without_parking_and_reports_loss(block):
    model = RawLogModel()
    if block == "sink":
        model.state["pcc_log_write_lock"] = 1
        model.namespace["pcc_current_thread_id"] = lambda: 7
    elif block == "recursion":
        model.state["pcc_log_emitting"] = 1
    else:
        model.depth = 1
    model.emit()
    assert model.output == b"" and model.state["pcc_log_thread_trace_dropped"] == 1
    model.state["pcc_log_write_lock"] = model.state["pcc_log_emitting"] = model.depth = 0
    model.emit()
    assert [row["event"] for row in model.records()] == ["safepoint_stop_observed", "trace_dropped"]
    assert model.records()[1]["value0"] == 1
    assert model.state["pcc_log_thread_trace_dropped"] == 0


def test_sink_recursion_never_reacquires_or_registers_under_sink_lock():
    model = RawLogModel()
    model.on_write = lambda: model.emit(14)
    model.namespace["pcc_diagnostics_runtime_log_event"](b"thread\0", b"probe\0", 0, 0, None)
    assert [row["event"] for row in model.records()] == ["probe", "trace_dropped"]
    assert len(model.registrations) == 2


class WorldTraceModel:
    def __init__(self, stopped=True, owner=2):
        self.namespace = _namespace("freestanding_thread_kernel_pthread.py")
        self.state = dict(pcc_tls_scheduler_lock_held_py=0, pcc_tls_no_park_depth_py=0,
                          pcc_thread_stop_requested=int(stopped), pcc_stop_owner_thread_id_py=owner,
                          pcc_stop_epoch_py=1, pcc_stop_depth_py=int(stopped),
                          pcc_tls_thread_parked_py=0, pcc_tls_parked_epoch_py=0,
                          pcc_parked_thread_count_py=0, pcc_live_thread_count_py=3,
                          # A stop counts exited threads (blocked in kernel
                          # waits) as stopped, with the parked ones.
                          pcc_exited_thread_count_py=0)
        self.memory = {}
        self.locked = False
        self.actions = []
        self.wait_callbacks = []
        self.on_event = None
        self.namespace.update(
            global_addr=lambda name: name,
            global_load_ptr=lambda name: name,
            load_i32=self.load, load_i64=self.load, load_ptr=self.load,
            store_i32=self.store, store_i64=self.store, store_ptr=self.store,
            atomic_load_i32=lambda name, offset, order: self.load(name, offset),
            atomic_store_i32=lambda name, offset, value, order: self.store(name, offset, value),
            ptr_to_int=lambda value: value, int_to_ptr=lambda value: value,
            _world_init=lambda: 0, pcc_current_thread_id=lambda: 1,
            pthread_mutex_lock=self.lock, pthread_mutex_unlock=self.unlock,
            # World waits use the raw condition wait (pcc_cond_wait runs
            # exited, which a thread inside the world protocol must not).
            pcc_cond_wait=self.wait, pthread_cond_wait=self.wait,
            pcc_cond_broadcast=lambda cond: 0,
            pcc_platform_abort=self.abort,
            pcc_diagnostics_runtime_log_event_code=self.log,
            pcc_diagnostics_runtime_log_suspension_pair=self.log_pair,
            stack_alloc=lambda size: object(),
            # Parking publishes the parked frame in the thread record.
            frame_address=lambda: "parked-frame",
        )

    def load(self, pointer, offset):
        return self.state[pointer] if isinstance(pointer, str) else self.memory[pointer, offset]

    def store(self, pointer, offset, value):
        if isinstance(pointer, str):
            self.state[pointer] = value
        else:
            self.memory[pointer, offset] = value

    def lock(self, lock):
        assert not self.locked
        self.locked = True
        self.actions.append(("lock",))
        return 0

    def unlock(self, lock):
        assert self.locked
        self.locked = False
        self.actions.append(("unlock",))
        return 0

    def wait(self, cond, lock):
        assert self.locked
        assert self.state["pcc_tls_scheduler_lock_held_py"] == self.state["pcc_tls_no_park_depth_py"] == 0
        self.actions.append(("wait", self.state["pcc_stop_epoch_py"]))
        assert self.wait_callbacks, "unexpected extra wait"
        self.wait_callbacks.pop(0)()
        return 0

    def resume(self):
        self.state["pcc_thread_stop_requested"] = 0
        self.state["pcc_stop_owner_thread_id_py"] = 0
        self.state["pcc_parked_thread_count_py"] = 0

    def log(self, category, event, first, second, pointer):
        assert category == 9 and not self.locked
        self.actions.append(("trace", event, first, second))
        if self.on_event:
            self.on_event(event)

    def log_pair(self, thread_id, epoch, waits):
        assert thread_id == 1 and not self.locked
        assert self.state["pcc_tls_no_park_depth_py"] == 0
        self.log(9, 14, epoch, waits, None)
        self.log(9, 15, epoch, waits, None)

    def abort(self):
        assert not self.locked
        raise SuspendTripwire

    def events(self):
        return [row[1:] for row in self.actions if row[0] == "trace"]


def test_safepoint_trace_distinguishes_observation_spurious_wait_and_resume():
    model = WorldTraceModel()
    model.wait_callbacks = [lambda: None, model.resume]
    model.namespace["pcc_thread_safepoint"]()
    assert model.events() == [(13, 0, 0), (14, 1, 2), (15, 1, 2)]
    assert model.actions[0] == ("trace", 13, 0, 0)
    assert model.actions[-3:] == [("unlock",), ("trace", 14, 1, 2), ("trace", 15, 1, 2)]
    assert model.state["pcc_tls_thread_parked_py"] == 0


def test_observed_stop_that_resumes_before_lock_is_not_reported_as_suspended():
    model = WorldTraceModel()
    model.on_event = lambda event: model.resume() if event == 13 else None
    model.namespace["pcc_thread_safepoint"]()
    assert model.events() == [(13, 0, 0)]
    assert not [row for row in model.actions if row[0] == "wait"]


@pytest.mark.parametrize("epochs", [2, 10])
def test_deferred_safepoint_trace_keeps_epoch_identity_and_reports_bounded_loss(epochs):
    model = WorldTraceModel()
    def advance():
        model.state["pcc_stop_epoch_py"] += 1
        model.state["pcc_parked_thread_count_py"] = 0
    model.wait_callbacks = [advance] * (epochs - 1) + [model.resume]
    model.namespace["pcc_thread_safepoint"]()
    expected = [(13, 0, 0)]
    for epoch in range(1, min(epochs, 8) + 1):
        expected.extend([(14, epoch, 1), (15, epoch, 1)])
    if epochs > 8:
        expected.append((25, epochs, epochs - 8))
    assert model.events() == expected


def test_stw_trace_records_real_stop_nested_depth_and_resume_outside_world_lock():
    model = WorldTraceModel(stopped=False, owner=0)
    model.wait_callbacks = [lambda: model.state.__setitem__("pcc_parked_thread_count_py", 2)]
    assert model.namespace["pcc_stop_the_world"]() == 0
    assert model.namespace["pcc_stop_the_world"]() == 0
    assert model.namespace["pcc_resume_world"]() == 0
    assert model.namespace["pcc_resume_world"]() == 0
    assert model.namespace["pcc_resume_world"]() == -1
    assert model.events() == [(16, 0, 0), (17, 2, 3), (16, 0, 0), (18, 2, 2),
                              (19, 0, 0), (21, 2, 1), (19, 0, 0), (20, 2, 0),
                              (19, 0, 0), (23, 2, -1)]


def test_competing_stw_owner_reports_waited_epoch_before_new_stop():
    model = WorldTraceModel()
    model.wait_callbacks = [model.resume, lambda: model.state.__setitem__("pcc_parked_thread_count_py", 2)]
    assert model.namespace["pcc_stop_the_world"]() == 0
    assert model.events() == [(16, 0, 0), (14, 1, 1), (15, 1, 1), (17, 2, 3)]


def test_actual_suspend_boundary_rechecks_no_park_and_unlocks_before_abort():
    model = WorldTraceModel()
    model.state["pcc_tls_no_park_depth_py"] = 1
    model.namespace["_thread_trace"] = lambda *args: None
    with pytest.raises(SuspendTripwire):
        model.namespace["pcc_stop_the_world"]()
    assert model.actions == [("lock",), ("unlock",)]
    assert model.state["pcc_tls_thread_parked_py"] == 0


def test_raw_sink_transitive_helpers_have_no_allocations_or_safepoints():
    tree = ast.parse((RUNTIME / "py_runtime_log.py").read_text())
    functions = {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}
    pending = ["_write_event_unlocked", "_write_thread_batch_unlocked"]
    seen = set()
    calls = set()
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        for node in ast.walk(functions[name]):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                calls.add(node.func.id)
                if node.func.id in functions:
                    pending.append(node.func.id)
    assert not calls.intersection({"malloc", "calloc", "fopen", "fwrite", "fflush", "fclose",
                                  "thread_safepoint", "pcc_current_thread_id", "_init_once",
                                  "pcc_thread_no_park_enter", "pcc_thread_no_park_exit"})
    assert {"open_file", "write", "close", "stack_alloc"} <= calls


def _forbid_pair_registration_and_init(model):
    for name in ("pcc_current_thread_id", "_init_once", "_code_enabled"):
        model.namespace[name] = lambda *args, name=name: pytest.fail("pair called " + name)


def test_completed_suspension_pair_keeps_one_sink_acquisition_and_captured_identity():
    model = RawLogModel()
    _forbid_pair_registration_and_init(model)
    acquires = []
    releases = []
    original_cas = model.namespace["atomic_cas_i32"]
    original_release = model.namespace["_write_lock_release"]

    def acquire(*args):
        acquires.append("try")
        return original_cas(*args)

    def release():
        # A competing writer can take the sink immediately after release;
        # both records must already exist before that ownership gap opens.
        releases.append([row["event"] for row in model.records()])
        original_release()
        model.state["pcc_log_write_lock"] = 1

    model.namespace["atomic_cas_i32"] = acquire
    model.namespace["_write_lock_release"] = release
    model.namespace["pcc_diagnostics_runtime_log_suspension_pair"](41, 17, 3)
    assert acquires == ["try"]
    assert releases == [["safepoint_suspend_deferred", "safepoint_resume_deferred"]]
    assert [(row["thread"], row["value0"], row["value1"]) for row in model.records()] == [(41, 17, 3)] * 2
    assert model.state["pcc_log_emitting"] == 0
    assert model.registrations == []


def test_pair_waits_outside_sink_and_reentrant_suspension_drops_both_records():
    model = RawLogModel()
    _forbid_pair_registration_and_init(model)
    model.state["pcc_log_write_lock"] = 1
    polls = []

    def safepoint():
        assert model.state["pcc_log_emitting"] == 1
        assert model.state["pcc_log_write_lock"] == 1  # Other writer owns it.
        assert model.depth == 0 and model.output == b""
        polls.append("allowed unlocked wait")
        model.namespace["pcc_diagnostics_runtime_log_suspension_pair"](41, 18, 1)
        assert model.state["pcc_log_thread_trace_dropped"] == 2
        assert model.output == b""  # Nested call cannot take the sink or recurse.
        model.state["pcc_log_write_lock"] = 0

    model.namespace["thread_safepoint"] = safepoint
    model.namespace["pcc_diagnostics_runtime_log_suspension_pair"](41, 17, 3)
    assert polls == ["allowed unlocked wait"]
    rows = model.records()
    assert [row["event"] for row in rows] == ["safepoint_suspend_deferred", "safepoint_resume_deferred", "trace_dropped"]
    assert [row["value0"] for row in rows] == [17, 17, 2]
    assert model.state["pcc_log_write_lock"] == model.state["pcc_log_emitting"] == 0
    assert model.registrations == []


@pytest.mark.parametrize("block", ["recursion", "no_park", "missing_identity", "invalid_epoch", "no_wait"])
def test_forbidden_pair_never_waits_and_loss_is_counted_as_two_records(block):
    model = RawLogModel()
    _forbid_pair_registration_and_init(model)
    arguments = [41, 17, 3]
    if block == "recursion":
        model.state["pcc_log_emitting"] = 1
    elif block == "no_park":
        model.depth = 1
    elif block == "missing_identity":
        arguments[0] = 0
    elif block == "invalid_epoch":
        arguments[1] = 0
    else:
        arguments[2] = 0
    model.namespace["pcc_diagnostics_runtime_log_suspension_pair"](*arguments)
    assert model.output == b"" and model.state["pcc_log_write_lock"] == 0
    assert model.state["pcc_log_thread_trace_dropped"] == 2
    model.state["pcc_log_emitting"] = model.depth = 0
    model.namespace["pcc_diagnostics_runtime_log_suspension_pair"](41, 17, 3)
    assert [row["event"] for row in model.records()] == ["safepoint_suspend_deferred", "safepoint_resume_deferred", "trace_dropped"]
    assert model.records()[-1]["value0"] == 2


@pytest.mark.parametrize("fast,initialized,mask", [(0, 2, 256), (1, 1, 256), (-1, 0, 0), (1, 2, 0)])
def test_disabled_or_uninitialized_pair_never_initializes_registers_or_takes_sink(fast, initialized, mask):
    model = RawLogModel()
    _forbid_pair_registration_and_init(model)
    model.state.update(pcc_diagnostics_runtime_log_fast_state=fast,
                       pcc_log_init_state=initialized, pcc_log_mask=mask)
    model.namespace["atomic_cas_i32"] = lambda *args: pytest.fail("disabled pair took sink")
    model.namespace["pcc_diagnostics_runtime_log_suspension_pair"](41, 17, 3)
    assert model.output == b"" and model.registrations == []
    assert model.state["pcc_log_emitting"] == model.state["pcc_log_thread_trace_dropped"] == 0


def test_pair_sink_recursion_does_not_split_delivered_records():
    model = RawLogModel()
    _forbid_pair_registration_and_init(model)
    model.on_write = lambda: model.namespace["pcc_diagnostics_runtime_log_suspension_pair"](41, 18, 1)
    model.namespace["pcc_diagnostics_runtime_log_suspension_pair"](41, 17, 3)
    assert [(row["event"], row["value0"]) for row in model.records()] == [
        ("safepoint_suspend_deferred", 17), ("safepoint_resume_deferred", 17)]
    # Loss arriving during the raw write stays counted for the next complete
    # batch, rather than opening a trailing fragment at process exit.
    assert model.state["pcc_log_thread_trace_dropped"] == 2
    model.namespace["pcc_diagnostics_runtime_log_suspension_pair"](41, 19, 1)
    assert [(row["event"], row["value0"]) for row in model.records()][-3:] == [
        ("safepoint_suspend_deferred", 19), ("safepoint_resume_deferred", 19), ("trace_dropped", 2)]
    assert model.state["pcc_log_thread_trace_dropped"] == 0
    assert model.state["pcc_log_emitting"] == model.state["pcc_log_write_lock"] == 0


def test_kernel_pair_flush_uses_captured_identity_without_individual_trace_attempts():
    namespace = _namespace("freestanding_thread_kernel_pthread.py")
    memory = {0: 17, 8: 3, 16: 18, 24: 1}
    pairs = []
    namespace.update(
        load_i64=lambda record, offset: memory[offset],
        pcc_current_thread_id=lambda: pytest.fail("flush re-registered"),
        _thread_trace=lambda *args: pytest.fail("pair split into individual trace attempts"),
        pcc_diagnostics_runtime_log_suspension_pair=lambda *args: pairs.append(args),
    )
    namespace["_flush_suspend_trace"]("records", 2, 18, 41)
    assert pairs == [(41, 17, 3), (41, 18, 1)]
