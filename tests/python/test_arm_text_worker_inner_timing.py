"""Real entry selectors with parsing, preparation and code emission mocked."""

from __future__ import annotations

import ast
import inspect
import sys

import pytest

from pcc.backend import worker_phase_observer as observer
from pcc.frontends.python import pipeline_self_backend_emit as emit
from tests.python.test_self_backend_worker_phase_timing import (
    ASSEMBLY,
    SOURCE,
    _Harness,
    _forbidden,
    _mock_only,
)


pytestmark = pytest.mark.xdist_group("arm-text-worker-inner-timing")
PARSED = object()
PREPARED = object()
INNER = ("arm_parse", "arm_prepare", "arm_module_emit")
# The shared fixture blocks the public selector for the old callback-only tests.
# This suite calls its saved body only after replacing all compiler work below.
_REAL_EMIT_IN_PROCESS = emit.emit_in_process


@pytest.fixture(autouse=True)
def _observer_isolation(monkeypatch):
    for name, value in (
        ("active", False), ("_context_id", 0), ("_context_pid", -1),
        ("_context_item", -1), ("_context_phase", ""), ("_next_context_id", 0),
    ):
        monkeypatch.setattr(observer, name, value)
    yield
    assert not observer.active
    assert observer._context_phase == ""


def _state():
    return (observer.active, observer._context_id, observer._context_pid,
            observer._context_item, observer._context_phase)


def _arm_harness(monkeypatch, *, flag="1", failure_at="", failure=None):
    # Import once on this loadgroup worker, before installing fake assemblers.
    # Only entry selectors below execute; all compiler work is replaced.
    from pcc.backend import self_backend_aarch64_darwin as arm
    from pcc.backend import self_backend_aarch64_linux as linux
    from pcc.backend import self_backend_prepare as prepare
    from pcc.backend import self_backend_targets

    harness = _Harness(monkeypatch, "absent", flag, failure_at, failure)

    def parse(text):
        harness._step("parse", text)
        return PARSED

    def prepare_parsed(module, **kwargs):
        harness._step("prepare", module, kwargs)
        return PREPARED

    def module_emit(prepared, optimize, **kwargs):
        harness._step("module_emit", prepared, optimize, kwargs)
        return ASSEMBLY

    def trace(message):
        harness._step("trace_end" if message == "prepare end" else "trace_begin")

    monkeypatch.setattr(prepare, "parse_self_backend_module", parse)
    monkeypatch.setattr(prepare, "prepare_parsed_module_for_target", prepare_parsed)
    monkeypatch.setattr(arm, "prepare_module_for_target", prepare.prepare_module_for_target)
    monkeypatch.setattr(arm, "_emit_prepared_aarch64_darwin_module", module_emit)
    monkeypatch.setattr(arm, "_emit_trace", trace)
    monkeypatch.setattr(linux, "parse_self_backend_target_triple", lambda _: "aarch64-unknown-linux-gnu")

    def native(text):
        harness._step("native_enter", text)
        result = _REAL_EMIT_IN_PROCESS(
            text, parse_target_triple=lambda _: "aarch64-unknown-linux-gnu",
            host_target_triple=_forbidden, target_supported=lambda _: False,
            emit_asm=_forbidden,
        )
        harness._step("native_return")
        return result

    harness._emit = native
    return harness, arm, prepare


def _inner(harness):
    return [row for row in harness.sink.records() if row["phase"] in INNER]


def test_real_linux_entry_preserves_callbacks_bytes_and_phase_arguments(monkeypatch):
    cold, _arm, _prepare = _arm_harness(monkeypatch, flag="0")
    assert cold.run() == 0
    warm, arm, _prepare = _arm_harness(monkeypatch)
    assert warm.run() == 0
    assert warm.calls == cold.calls
    assert warm.outputs == cold.outputs
    rows = _inner(warm)
    assert [(row["event"], row["phase"]) for row in rows] == [
        (event, phase) for phase in INNER for event in ("begin", "complete")
    ]
    assert all(row["pid"] == "7422" and row["item"] == "1" and row["parent"] == "emit" for row in rows)
    assert all("private" not in line for line in warm.sink.lines)
    preparation = next(args for name, args in warm.calls if name == "prepare")
    assert preparation == (PARSED, {
        "aggregate_returned_indirect": arm._aggregate_returned_indirect,
        "aggregate_returned_indirect_indexed": arm._aggregate_returned_indirect_indexed,
        "materialize_legacy_slots": False, "phase_timing": None,
    })
    emission = next(args for name, args in warm.calls if name == "module_emit")
    assert emission == (PREPARED, False, {"profile_ir_text": SOURCE})
    assert not observer.active


@pytest.mark.parametrize("operation,phase", [
    ("parse", "arm_parse"), ("prepare", "arm_prepare"),
    ("module_emit", "arm_module_emit"), ("native_return", "arm_module_emit"),
])
def test_work_failure_marks_only_the_entered_inner_phase(monkeypatch, operation, phase):
    harness, _arm, _prepare = _arm_harness(monkeypatch, failure_at=operation)
    assert harness.run() == 1
    rows = _inner(harness)
    assert rows[-1]["event"] == "failed" and rows[-1]["phase"] == phase
    assert not any(row["event"] == "complete" and row["phase"] == phase for row in rows)
    assert not harness.outputs
    assert harness.sink.records()[-1]["phase"] == "emit"
    assert harness.sink.records()[-1]["event"] == "failed"


@pytest.mark.parametrize("operation", ["parse", "prepare", "module_emit"])
def test_base_exception_identity_and_cleanup(monkeypatch, operation):
    original = KeyboardInterrupt("private work exception")
    harness, _arm, _prepare = _arm_harness(monkeypatch, failure_at=operation, failure=original)
    with pytest.raises(KeyboardInterrupt) as caught:
        harness.run()
    assert caught.value is original
    assert _inner(harness)[-1]["event"] == "failed"
    assert not observer.active


def test_exception_between_prepare_and_emission_does_not_invent_entry(monkeypatch):
    harness, _arm, _prepare = _arm_harness(monkeypatch, failure_at="trace_end")
    assert harness.run() == 1
    assert [(row["event"], row["phase"]) for row in _inner(harness)] == [
        ("begin", "arm_parse"), ("complete", "arm_parse"),
        ("begin", "arm_prepare"), ("complete", "arm_prepare"),
    ]


@pytest.mark.parametrize("diagnostic_error", [OSError("sink"), KeyboardInterrupt(), SystemExit(9)])
def test_writer_exceptions_do_not_change_work_or_leave_context(monkeypatch, diagnostic_error):
    def broken_writer(*_args):
        raise diagnostic_error

    monkeypatch.setattr(observer, "write_event", broken_writer)
    for operation in ("", "parse", "prepare", "module_emit"):
        harness, _arm, _prepare = _arm_harness(monkeypatch, failure_at=operation)
        assert harness.run() == (1 if operation else 0)
        assert not observer.active and observer._context_phase == ""


def test_writer_failure_cannot_replace_original_base_exception(monkeypatch):
    original = SystemExit(23)
    harness, _arm, _prepare = _arm_harness(monkeypatch, failure_at="prepare", failure=original)
    monkeypatch.setattr(observer, "write_event", _forbidden)
    with pytest.raises(SystemExit) as caught:
        harness.run()
    assert caught.value is original


def test_clock_failure_preserves_transitions(monkeypatch):
    harness, _arm, _prepare = _arm_harness(monkeypatch)
    monkeypatch.setattr(observer.time, "monotonic", _forbidden)
    assert harness.run() == 0
    assert len(_inner(harness)) == 6
    assert all(row["clock_ok"] == "0" for row in _inner(harness))


@pytest.mark.parametrize("operation", ["", "parse", "prepare", "module_emit"])
def test_nested_worker_restores_enclosing_scalar_scope(monkeypatch, operation):
    harness, _arm, _prepare = _arm_harness(monkeypatch, failure_at=operation)
    outer = observer.open_worker(901, 7)
    scope = observer.arm_text()
    assert observer.begin_parse() == scope
    expected = _state()
    assemble = sys.modules["pcc.backend.owned_elf_link"].assemble

    def checked_assemble(*args):
        assert _state() == expected  # Restore before later object assembly.
        return assemble(*args)

    monkeypatch.setattr(sys.modules["pcc.backend.owned_elf_link"], "assemble", checked_assemble)
    assert harness.run() == (1 if operation else 0)
    assert _state() == expected
    observer.parsed(scope)
    observer.prepared(scope)
    observer.begin_module_emit(scope)
    observer.close_worker(outer, False)
    assert not observer.active


def test_refused_nested_entries_cannot_advance_owner_phase(monkeypatch):
    harness, _arm, _prepare = _arm_harness(monkeypatch)
    saved = observer.open_worker(5, 8)
    arm_scope = observer.arm_text()
    parse_scope = observer.begin_parse()
    assert arm_scope == parse_scope > 0
    expected = _state()
    assert observer.arm_text() == observer.begin_parse() == 0
    observer.parsed(0)
    observer.prepared(0)
    observer.begin_module_emit(0)
    assert _state() == expected
    assert [(r["event"], r["phase"]) for r in _inner(harness)] == [("begin", "arm_parse")]
    observer.close_worker(saved, True)


@pytest.mark.parametrize("stage", ["parse", "prepare"])
def test_normal_return_never_promotes_an_unfinished_inner_phase(monkeypatch, stage):
    harness, _arm, _prepare = _arm_harness(monkeypatch)
    saved = observer.open_worker(5, 8)
    observer.arm_text()
    scope = observer.begin_parse()
    if stage == "prepare":
        observer.parsed(scope)
    before = list(_inner(harness))
    observer.close_worker(saved, False)
    assert _inner(harness) == before


def test_outside_worker_and_disabled_worker_are_cold(monkeypatch):
    harness, arm, _prepare = _arm_harness(monkeypatch, flag="0")
    for name in ("open_worker", "mask_worker", "arm_text", "begin_parse", "parsed", "prepared", "begin_module_emit", "close_worker", "write_event"):
        monkeypatch.setattr(observer, name, _forbidden)
    monkeypatch.setattr(observer.time, "monotonic", _forbidden)
    monkeypatch.setattr(emit.os, "getpid", _forbidden)
    assert harness.run() == 0
    assert arm.emit_aarch64_darwin_asm(SOURCE, False) == ASSEMBLY
    assert harness.sink.lines == []


def test_non_arm_shared_prepare_has_no_inner_events(monkeypatch):
    harness, _arm, prepare = _arm_harness(monkeypatch)

    def native(text):
        assert prepare.prepare_module_for_target(text, aggregate_returned_indirect=None) is PREPARED
        return "self-x86_64-linux-v0", ASSEMBLY

    harness._emit = native
    assert harness.run() == 0
    assert _inner(harness) == []


@pytest.mark.parametrize("operation,error_type", [
    ("", None), ("normalize", RuntimeError), ("parse", RuntimeError),
    ("prepare", KeyboardInterrupt), ("module_emit", SystemExit),
])
def test_disabled_nested_worker_is_silent_and_restores_owner(monkeypatch, operation, error_type):
    original = error_type("private work failure") if error_type else None
    harness, _arm, _prepare = _arm_harness(
        monkeypatch, flag="0", failure_at=operation, failure=original,
    )
    outer = observer.open_worker(901, 7)
    expected = _state()
    step = harness._step

    def checked_step(name, *args):
        # The mask covers read/normalize, emission, assembly, and writes.
        assert _state() == (False, 0, -1, -1, "")
        step(name, *args)

    harness._step = checked_step
    if error_type in (KeyboardInterrupt, SystemExit):
        with pytest.raises(error_type) as caught:
            harness.run()
        assert caught.value is original
    else:
        assert harness.run() == (1 if operation else 0)
    assert harness.sink.records() == []
    assert harness.clock_calls == harness.pid_calls == 0
    assert _state() == expected
    # The enclosing owner can still claim and finish its own phase afterward.
    assert observer.arm_text() == expected[1]
    assert observer.begin_parse() == expected[1]
    observer.close_worker(outer, True)


@pytest.mark.parametrize("phase", ["arm_parse", "arm_prepare", "arm_module_emit"])
def test_disabled_nested_worker_suspends_live_owner_tokens(monkeypatch, phase):
    harness, _arm, _prepare = _arm_harness(monkeypatch, flag="0")
    outer = observer.open_worker(901, 7)
    scope = observer.arm_text()
    assert observer.begin_parse() == scope
    if phase != "arm_parse":
        observer.parsed(scope)
    if phase == "arm_module_emit":
        observer.prepared(scope)
        observer.begin_module_emit(scope)
    expected = _state()
    before = harness.sink.records()
    clocks = harness.clock_calls
    step = harness._step

    def checked_step(name, *args):
        observer.parsed(scope)
        observer.prepared(scope)
        observer.begin_module_emit(scope)
        assert _state() == (False, 0, -1, -1, "")
        step(name, *args)

    harness._step = checked_step
    assert harness.run() == 0
    assert _state() == expected
    assert harness.sink.records() == before
    assert harness.clock_calls == clocks and harness.pid_calls == 0
    observer.parsed(scope)
    observer.prepared(scope)
    observer.begin_module_emit(scope)
    observer.close_worker(outer, False)


@pytest.mark.parametrize("operation", ["", "prepare"])
def test_enabled_grandchild_restores_disabled_mask_then_outer_owner(monkeypatch, operation):
    harness, _arm, _prepare = _arm_harness(monkeypatch, flag="0")
    outer = observer.open_worker(901, 7)
    expected = _state()
    step = harness._step
    grandchildren = []

    def checked_step(name, *args):
        assert _state() == (False, 0, -1, -1, "")
        if name == "normalize":
            with monkeypatch.context() as child_patch:
                enabled, _child_arm, _child_prepare = _arm_harness(
                    child_patch, failure_at=operation,
                )
                assert enabled.run() == (1 if operation else 0)
                assert _inner(enabled)
                grandchildren.append(enabled)
            assert _state() == (False, 0, -1, -1, "")
        step(name, *args)

    harness._step = checked_step
    assert harness.run() == 0
    assert len(grandchildren) == 1
    assert harness.sink.records() == []
    assert harness.clock_calls == harness.pid_calls == 0
    assert _state() == expected
    assert observer.arm_text() == expected[1]
    assert observer.begin_parse() == expected[1]
    observer.close_worker(outer, True)


def test_backend_operations_and_direct_returns_match_frozen_preimages():
    from pcc.backend import self_backend_aarch64_darwin as arm
    from pcc.backend import self_backend_prepare as prepare

    class RemoveDiagnostics(ast.NodeTransformer):
        def visit_If(self, node):
            if isinstance(node.test, ast.Name) and node.test.id.startswith("timing_"):
                return None
            if (isinstance(node.test, ast.Attribute) and isinstance(node.test.value, ast.Name)
                    and node.test.value.id == "_worker_phase_observer"):
                return None
            return self.generic_visit(node)

        def visit_Assign(self, node):
            if all(isinstance(target, ast.Name) and target.id.startswith("timing_") for target in node.targets):
                return None
            return self.generic_visit(node)

    for function, preimage in (
        (prepare.prepare_module_for_target, _PREPARE_PREIMAGE),
        (arm.emit_aarch64_darwin_asm, _ARM_PREIMAGE),
    ):
        stripped = RemoveDiagnostics().visit(ast.parse(inspect.getsource(function)))
        assert ast.dump(stripped, include_attributes=False) == ast.dump(ast.parse(preimage), include_attributes=False)


# Frozen974 original function; parsed as an oracle, never executed.
_PREPARE_PREIMAGE = 'def prepare_module_for_target(\n    ir_text: str,\n    *,\n    aggregate_returned_indirect,\n    aggregate_returned_indirect_indexed=None,\n    materialize_legacy_slots: bool = True,\n    phase_timing=None,\n) -> PreparedSelfBackendModule:\n    module = parse_self_backend_module(ir_text)\n    return prepare_parsed_module_for_target(\n        module,\n        aggregate_returned_indirect=aggregate_returned_indirect,\n        aggregate_returned_indirect_indexed=aggregate_returned_indirect_indexed,\n        materialize_legacy_slots=materialize_legacy_slots,\n        phase_timing=phase_timing,\n    )\n'


# Frozen974 original function; parsed as an oracle, never executed.
_ARM_PREIMAGE = 'def emit_aarch64_darwin_asm(ir_text: str, optimize: bool = True) -> str:\n    # ``ir_text`` is a borrowed function parameter.  The self-compiled path\n    # forwards it through the prepare/parser stack.  Keep this wrapper short:\n    # the prepared module crosses the next call as an owned return value, while\n    # the large source string no longer shares a frame with every emit pass.\n    owned_ir_text = ir_text + ""\n    _emit_trace("prepare begin bytes=" + str(len(owned_ir_text)))\n    prepared = prepare_module_for_target(\n        owned_ir_text,\n        aggregate_returned_indirect=_aggregate_returned_indirect,\n        aggregate_returned_indirect_indexed=(\n            _aggregate_returned_indirect_indexed\n        ),\n        materialize_legacy_slots=False,\n    )\n    _emit_trace("prepare end")\n    return _emit_prepared_aarch64_darwin_module(\n        prepared,\n        optimize,\n        profile_ir_text=owned_ir_text,\n    )\n'
