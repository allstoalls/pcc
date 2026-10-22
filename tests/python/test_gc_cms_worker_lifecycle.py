"""Deterministic host execution of production CMS lifecycle function bodies.

Intrinsics/thread operations are controlled doubles, not native GC witnesses.
Interleavings are injected at exact source calls; no real threads or sleeps.
"""
import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
STATE = "pcc_gc_cms_worker_started"
STOP = "pcc_gc_cms_worker_stop_requested"
HANDLE = "pcc_gc_cms_worker_handle"
BACKEND = "pcc_gc_backend_selected"
TLS = "pcc_gc_cms_worker_current"


def _body(module, names):
    path = ROOT / "pcc/runtime/py" / (module + ".py")
    tree = ast.parse(path.read_text())
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in nodes} == set(names)
    for node in nodes:
        node.decorator_list = []
    return ast.Module(body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes], type_ignores=[])


class Runtime:
    def __init__(self, backend=2, state=0):
        self.memory = {BACKEND: backend, STATE: state, STOP: 0, HANDLE: 17 if state == 1 else 0}
        self.events = []
        self.hooks = {}
        self.locked = False
        self.start_result = 0
        self.join_result = 0
        self.live = 1
        modules = [
            _body("freestanding_gc_public_collection", ["pcc_gc_maybe_start_cms_worker", "pcc_gc_is_cms_worker", "pcc_gc_cms_worker_main_py"]),
            _body("freestanding_gc_incremental_concurrent_scheduler", ["pcc_gc_cms_stop_worker"]),
            _body("py_gc_backend", ["pcc_gc_set_backend", "_release_cms_worker_pause", "_release_cms_worker_pause_unless_kept"]),
        ]
        calls = {node.func.id for tree in modules for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        self.ns = {name: lambda *args: 0 for name in calls}
        self.ns.update({
            "global_addr": lambda name: name,
            "function_addr": lambda name: name,
            "null": lambda: 0,
            "ptr_is_null": lambda ptr: int(ptr == 0),
            "load_i32": self.load,
            "load_i64": self.load,
            "load_ptr": self.load,
            "atomic_load_i32": self.load,
            "store_i32": self.store,
            "store_i64": self.store,
            "atomic_store_i32": self.store,
            "atomic_rmw_i32": self.rmw,
            "atomic_cas_i32": self.cas,
            "global_load_ptr": lambda name: self.load(name, 0),
            "global_store_ptr": lambda name, value: self.store(name, 0, value),
            "pcc_threads_enabled": lambda: 1,
            "pcc_thread_start": self.start,
            "pcc_thread_join": self.join,
            "pcc_thread_live_count": self.live_count,
            "_object_graph_lock": self.lock,
            "_object_graph_unlock": self.unlock,
            "_tracing_cycle_epoch_advance_unlocked": lambda: 1,
        })
        for tree in modules:
            exec(compile(ast.fix_missing_locations(tree), "production-source-body", "exec"), self.ns)
        self.ns["_stop_cms_worker"] = self.ns["pcc_gc_cms_stop_worker"]
        self.ns["_maybe_start_cms_worker"] = self.ns["pcc_gc_maybe_start_cms_worker"]
        self.ns["_is_cms_worker"] = self.ns["pcc_gc_is_cms_worker"]

    def event(self, kind, *args):
        self.events.append((kind, *args))
        hook = self.hooks.pop((kind, *args), None)
        if hook:
            hook()

    def load(self, key, offset, *args):
        value = self.memory.get(key, 0)
        self.event("load", key)
        return value

    def store(self, key, offset, value, *args):
        self.memory[key] = value
        self.event("store", key, value)

    def cas(self, key, offset, expected, value, *args):
        self.event("before_cas", key, expected, value)
        previous = self.memory.get(key, 0)
        if previous == expected:
            self.memory[key] = value
        self.event("cas", key, previous, value)
        return previous

    def rmw(self, operation, key, offset, value, *args):
        previous = self.memory.get(key, 0)
        self.memory[key] = previous + value
        return previous

    def start(self, out, entry, arg):
        self.event("start")
        if self.start_result == 0:
            self.memory[out] = 17
        return self.start_result

    def join(self, handle, result):
        assert not self.locked
        assert handle == 17
        self.event("join")
        return self.join_result

    def live_count(self):
        self.event("live_count")
        return self.live

    def lock(self):
        assert not self.locked
        self.locked = True

    def unlock(self):
        assert self.locked
        self.locked = False

    def call(self, name, *args):
        return self.ns[name](*args)

    def start_worker(self):
        self.call("pcc_gc_maybe_start_cms_worker")

    def stop_worker(self):
        return self.call("pcc_gc_cms_stop_worker")

    def set_backend(self, backend):
        return self.call("pcc_gc_set_backend", backend)


@pytest.mark.parametrize("state", [1, 2, 3, 4])
def test_unadmitted_start_never_clears_stop(state):
    rt = Runtime(state=state)
    rt.memory[STOP] = 1
    rt.start_worker()
    assert rt.memory[STOP] == 1
    assert ("start",) not in rt.events


def test_stale_selected_read_is_rechecked_after_start_claim():
    rt = Runtime()
    rt.hooks[("before_cas", STATE, 0, 2)] = lambda: rt.memory.update({BACKEND: 4})
    rt.start_worker()
    assert rt.memory[STATE] == 0
    assert ("start",) not in rt.events


def test_start_publishes_handle_before_running_and_refuses_switch():
    rt = Runtime()
    def during_start():
        assert rt.memory[STATE] == 2
        assert rt.memory[HANDLE] == 0
        assert rt.set_backend(4) == -1
    rt.hooks[("start",)] = during_start
    rt.start_worker()
    assert rt.memory[STATE] == 1
    assert rt.memory[HANDLE] == 17
    assert rt.memory["pcc_gc_cms_worker_starts"] == 1
    assert ("join",) not in rt.events


def test_create_failure_releases_start_claim():
    rt = Runtime()
    rt.start_result = -1
    rt.start_worker()
    assert rt.memory[STATE] == 0
    assert rt.memory[HANDLE] == 0
    assert rt.memory.get("pcc_gc_cms_worker_starts", 0) == 0


def test_join_has_single_owner_and_stop_cannot_be_revoked():
    rt = Runtime(state=1)
    def during_join():
        assert rt.memory[STATE] == 3
        rt.start_worker()
        assert rt.memory[STOP] == 1
        assert rt.stop_worker() == -1
        assert rt.set_backend(4) == -1
    rt.hooks[("join",)] = during_join
    assert rt.stop_worker() == 0
    assert rt.memory[STATE] == 4
    assert rt.memory[HANDLE] == 0
    assert rt.events.count(("join",)) == 1


@pytest.mark.parametrize("state", [0, 1])
@pytest.mark.parametrize("backend", [0, 1, 3, 4])
def test_switch_keeps_pause_through_admission_and_publication(state, backend):
    rt = Runtime(state=state)
    def cannot_restart():
        assert rt.memory[STATE] == 4
        rt.start_worker()
        assert rt.memory[STATE] == 4
    rt.hooks[("live_count",)] = cannot_restart
    rt.hooks[("store", BACKEND, backend)] = cannot_restart
    assert rt.set_backend(backend) == 0
    assert rt.memory[BACKEND] == backend
    assert rt.memory[STATE] == (1 if backend in (1, 2) else 0)
    assert not rt.locked
    assert rt.events.count(("join",)) == state


@pytest.mark.parametrize("state", [0, 1])
@pytest.mark.parametrize("backend", [1, 2])
def test_same_backend_tracing_reset_keeps_the_collector_without_a_pause(state, backend):
    # The graph lock and the cycle epoch order the reset against the
    # collector's steps, so it needs no join and a bounded no-park scope may
    # reset (the tracing finish-claim windows do).
    rt = Runtime(backend=backend, state=state)
    rt.ns["pcc_thread_no_park_depth"] = lambda: 1
    assert rt.set_backend(backend) == 0
    assert rt.memory[BACKEND] == backend
    assert ("join",) not in rt.events
    assert ("store", STATE, 4) not in rt.events
    assert rt.memory[STATE] == 1
    assert not rt.locked


def test_same_backend_reset_loses_to_a_concurrent_switch():
    rt = Runtime(backend=1, state=1)
    def switch_wins():
        rt.memory[BACKEND] = 4
    rt.hooks[("load", BACKEND)] = switch_wins
    assert rt.set_backend(1) == -1
    assert rt.memory[BACKEND] == 4
    assert rt.memory[STATE] == 1
    assert ("join",) not in rt.events
    assert not rt.locked


def test_collector_cannot_reset_its_own_backend():
    rt = Runtime(backend=1, state=1)
    rt.memory[TLS] = 1
    assert rt.set_backend(1) == -1
    assert rt.memory[STATE] == 1
    assert ("join",) not in rt.events


@pytest.mark.parametrize("state", [2, 3, 4])
def test_busy_stop_refuses_without_state_change(state):
    rt = Runtime(state=state)
    assert rt.set_backend(4) == -1
    assert rt.memory[STATE] == state
    assert rt.memory[BACKEND] == 2
    assert ("join",) not in rt.events


def test_join_failure_quarantines_without_reusing_or_touching_handle():
    rt = Runtime(state=1)
    rt.join_result = -1
    assert rt.set_backend(4) == -1
    assert rt.memory[STATE] == 3
    assert rt.memory[HANDLE] == 17
    assert rt.memory[STOP] == 1
    rt.start_worker()
    assert rt.set_backend(0) == -1
    assert rt.events.count(("join",)) == 1
    assert rt.memory[BACKEND] == 2


def test_running_without_handle_fails_closed():
    rt = Runtime(state=1)
    rt.memory[HANDLE] = 0
    assert rt.set_backend(4) == -1
    assert rt.memory[STATE] == 3
    assert ("join",) not in rt.events


@pytest.mark.parametrize("state", [0, 1, 2])
def test_collector_self_call_refused_before_claim(state):
    rt = Runtime(state=state)
    rt.memory[TLS] = 1
    assert rt.set_backend(4) == -1
    assert rt.memory[STATE] == state
    assert not any(event[0] == "cas" for event in rt.events)
    assert ("join",) not in rt.events


@pytest.mark.parametrize("guard", ["pcc_py_gc_minor_graph_lock_depth", "pcc_thread_no_park_depth", "pcc_thread_owns_stopped_world"])
def test_unsafe_scope_refuses_before_stop(guard):
    rt = Runtime(state=1)
    rt.ns[guard] = lambda: 1
    assert rt.set_backend(0) == -1
    assert rt.memory[STATE] == 1
    assert ("join",) not in rt.events


@pytest.mark.parametrize("reason", ["live", "lease", "auto", "roots", "remap", "forwarding", "epoch", "migration"])
def test_every_post_pause_refusal_releases_and_restarts_if_needed(reason):
    rt = Runtime(state=1)
    target = 4
    if reason == "live":
        rt.live = 2
    elif reason == "lease":
        rt.memory["pcc_gc_foreign_lease_active"] = 1
    elif reason == "auto":
        rt.memory[BACKEND] = 4
        rt.memory["pcc_gc_in_auto_step"] = 1
    elif reason == "roots":
        rt.memory["pcc_gc_trace_extension_roots_pending"] = 4
    elif reason == "remap":
        rt.memory["pcc_gc_backend4_remap_active"] = 1
    elif reason == "forwarding":
        rt.memory["pcc_gc_forwarding_population"] = 1
    elif reason == "epoch":
        rt.ns["_tracing_cycle_epoch_advance_unlocked"] = lambda: 0
    elif reason == "migration":
        target = 0
        rt.ns["_object_head"] = lambda: 29
        rt.ns["pcc_gc_managed_pointer_index_insert"] = lambda obj: -1
    original = rt.memory[BACKEND]
    assert rt.set_backend(target) == -1
    assert rt.memory[BACKEND] == original
    assert rt.memory[STATE] == (1 if original in (1, 2) else 0)
    assert not rt.locked
    assert rt.events.count(("join",)) == 1


def test_worker_tls_identity_is_set_during_callbacks_and_cleared_on_exit():
    rt = Runtime(state=1)
    rt.memory["py_gc_enabled"] = 0
    def sleep(_):
        assert rt.call("pcc_gc_is_cms_worker") == 1
        assert rt.set_backend(4) == -1
        rt.memory[STOP] = 1
    rt.ns["pcc_platform_sleep_ns"] = sleep
    assert rt.call("pcc_gc_cms_worker_main_py", 0) == 0
    assert rt.call("pcc_gc_is_cms_worker") == 0
    assert rt.memory["pcc_gc_cms_worker_stops"] == 1
    assert ("join",) not in rt.events


def test_post_worker_tls_cleanup_callback_cannot_join_again():
    rt = Runtime(state=3)
    rt.memory[STOP] = 1
    assert rt.call("pcc_gc_cms_worker_main_py", 0) == 0
    assert rt.memory[TLS] == 0
    assert rt.set_backend(4) == -1
    assert rt.memory[STATE] == 3
    assert ("join",) not in rt.events


@pytest.mark.parametrize("old,claimed,competitor", [(0, 4, 2), (1, 3, 3)])
def test_stop_cas_loser_has_no_handle_or_stop_ownership(old, claimed, competitor):
    rt = Runtime(state=old)
    rt.memory[STOP] = 1
    rt.hooks[("before_cas", STATE, old, claimed)] = lambda: rt.memory.update({STATE: competitor})
    assert rt.stop_worker() == -1
    assert rt.memory[STATE] == competitor
    assert rt.memory[STOP] == 1
    assert ("load", HANDLE) not in rt.events
    assert ("join",) not in rt.events


@pytest.mark.parametrize("backend", [-1, 5])
def test_invalid_backend_does_not_stop_collector(backend):
    rt = Runtime(state=1)
    assert rt.set_backend(backend) == -1
    assert rt.memory[STATE] == 1
    assert ("join",) not in rt.events


def test_threads_disabled_switch_releases_idle_pause_without_worker():
    rt = Runtime()
    rt.ns["pcc_threads_enabled"] = lambda: 0
    assert rt.set_backend(1) == 0
    assert rt.memory[BACKEND] == 1
    assert rt.memory[STATE] == 0
    assert ("start",) not in rt.events


def test_lifecycle_exports_match_the_extern_contract_registry():
    path = ROOT / "pcc/frontends/python/codegen/runtime_abi.py"
    tree = ast.parse(path.read_text())
    entries = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value in {"pcc_gc_cms_stop_worker", "pcc_gc_is_cms_worker"}:
                    entries[key.value] = ast.literal_eval(value)
    assert entries == {"pcc_gc_cms_stop_worker": ((), "c_int64"), "pcc_gc_is_cms_worker": ((), "c_int64")}


def test_collector_identity_getter_uses_owned_tls_in_host_ir(tmp_path):
    from pcc.frontends.python import pipeline

    output = tmp_path / "cms_public.ll"
    pipeline.compile_python(
        str(ROOT / "pcc/runtime/py/freestanding_gc_public_collection.py"),
        str(output),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    from pcc.frontends.python.codegen.runtime_abi import FREESTANDING_GC_THREAD_LOCAL_GLOBALS

    assert TLS in FREESTANDING_GC_THREAD_LOCAL_GLOBALS
    text = output.read_text()
    assert re.search(r'@"?pcc_gc_cms_worker_current"? = thread_local global i32 0', text)
    match = re.search(r'define (?:external )?i64 @"?pcc_gc_is_cms_worker"?\(\)[^{]*\{([^}]+)\}', text)
    assert match is not None
    getter = match.group(1)
    assert "pcc_gc_cms_worker_current" in getter
    assert 'load i32' in getter
