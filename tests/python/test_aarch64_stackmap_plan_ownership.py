"""Packed plan ownership survives the optional stack-map table policy."""

from __future__ import annotations

import pytest

from pcc.backend import self_backend_aarch64_darwin as emitter
from pcc.backend.arm64_asm_driver import assemble_file
from pcc.backend.precise_stackmap import (
    PRECISE_STACKMAP_TABLES_ENV,
    PreciseStackMapError,
)
from pcc.backend.self_backend_kernel import (
    IndexedFunctionKernel,
    get_indexed_function_kernel,
)
from pcc.backend.self_backend_parse import parse_self_backend_module
from pcc.backend.self_backend_precise_stackmaps import PackedPlannedSafepoints
from pcc.backend.self_backend_value_arena import CompilerIntArena
from tests.python.test_precise_stackmap_abi import _stale_managed_ssa_ir


_TARGETS = ("arm64-apple-darwin23.6.0", "aarch64-unknown-linux-gnu")
_ROUTES = ("text", "structured-lines", "native-sink")


def _source(triple):
    return _stale_managed_ssa_ir(triple) + """

define i64 @after(i64 %value) {
entry:
  %result = add i64 %value, 1
  ret i64 %result
}
"""


def _arenas(packed):
    result = [
        getattr(packed, field)
        for field in packed.__slots__
        if isinstance(getattr(packed, field), CompilerIntArena)
    ]
    assert len(result) == 11
    return result


def _emit(source, route):
    if route == "text":
        # Darwin exercises the peepholes' final plan consumer; Linux's public
        # text wrapper selects the same emitter with optimization disabled.
        return assemble_file(emitter.emit_aarch64_darwin_asm(
            source, optimize="apple-darwin" in source,
        ))
    transport = emitter.emit_aarch64_darwin_indexed_transport(
        parse_self_backend_module(source), optimize=False,
        structured_instructions=route == "native-sink",
    )
    try:
        assert transport.native_finalized is (route == "native-sink")
        return transport.assemble_sections()
    finally:
        if transport.encoded_line_records is not None:
            transport.encoded_line_records.close()


@pytest.mark.parametrize("triple", _TARGETS)
@pytest.mark.parametrize("route", _ROUTES)
def test_table_policy_closes_real_packed_plans_and_preserves_code(
    monkeypatch, triple, route,
):
    build_plans = emitter.build_stack_map_plans
    emit_function = emitter._emit_function
    close_packed = PackedPlannedSafepoints.close
    results = []
    for setting in (None, "0", "1"):
        captured = []
        closed = []

        def observe_plans(*args, **kwargs):
            plans = build_plans(*args, **kwargs)
            captured.extend(plans)
            assert len(plans) == 2
            assert all(plan.packed_records is not None for plan in plans)
            assert any(len(plan.packed_records.reload_scalars) for plan in plans)
            return plans

        def observe_function(func, plan, **kwargs):
            assert all(not arena._closed for arena in _arenas(plan.packed_records))
            return emit_function(func, plan, **kwargs)

        def observe_close(packed):
            assert all(not arena._closed for arena in _arenas(packed))
            closed.append(packed)
            close_packed(packed)

        try:
            with monkeypatch.context() as patch:
                if setting is None:
                    patch.delenv(PRECISE_STACKMAP_TABLES_ENV, raising=False)
                else:
                    patch.setenv(PRECISE_STACKMAP_TABLES_ENV, setting)
                patch.setattr(emitter, "build_stack_map_plans", observe_plans)
                patch.setattr(emitter, "_emit_function", observe_function)
                patch.setattr(PackedPlannedSafepoints, "close", observe_close)
                sections, undefined = _emit(_source(triple), route)
            packed_plans = [plan.packed_records for plan in captured]
            assert len(closed) == len(packed_plans) == 2
            assert {id(packed) for packed in closed} == {
                id(packed) for packed in packed_plans
            }
            assert all(
                arena._closed for packed in packed_plans for arena in _arenas(packed)
            )
            tables = [section for section in sections if section.sectname == "__pcc_stackmaps"]
            # Darwin publishes tables by default; explicit 0/1 overrides the
            # target policy without changing packed-plan ownership or code.
            expected_tables = "apple-darwin" in triple if setting is None else setting == "1"
            assert bool(tables) is expected_tables
            results.append((
                [section for section in sections if section.sectname != "__pcc_stackmaps"],
                undefined,
            ))
        finally:
            # Keep a failing baseline control from retaining its test-owned plans.
            for plan in captured:
                packed = plan.packed_records
                if packed is not None and any(not arena._closed for arena in _arenas(packed)):
                    close_packed(packed)
    assert results[0] == results[1] == results[2]


@pytest.mark.parametrize("triple", _TARGETS)
@pytest.mark.parametrize("route", _ROUTES)
def test_invalid_table_policy_fails_before_packed_plan_allocation(
    monkeypatch, triple, route,
):
    def reject_planning(*args, **kwargs):
        raise AssertionError("invalid table policy must fail before allocating plans")

    monkeypatch.setenv(PRECISE_STACKMAP_TABLES_ENV, "invalid")
    monkeypatch.setattr(emitter, "build_stack_map_plans", reject_planning)
    with pytest.raises(PreciseStackMapError, match="must be 0 or 1"):
        _emit(_source(triple), route)
    assert not emitter._AARCH64_EMISSION_ACTIVE
    assert not emitter.direct_instruction_capture_active()


@pytest.mark.parametrize("route", _ROUTES)
def test_disabled_table_closes_plans_without_consuming_kernel_tables(monkeypatch, route):
    monkeypatch.setenv(PRECISE_STACKMAP_TABLES_ENV, "0")
    prepared = emitter.prepare_parsed_module_for_target(
        parse_self_backend_module(_source(_TARGETS[0])),
        aggregate_returned_indirect=emitter._aggregate_returned_indirect,
        aggregate_returned_indirect_indexed=emitter._aggregate_returned_indirect_indexed,
        materialize_legacy_slots=False,
    )
    kernels = [get_indexed_function_kernel(func) for func in prepared.functions]
    captured = []
    build_plans = emitter.build_stack_map_plans
    close_kernel = IndexedFunctionKernel.close_native_tables

    def observe_plans(*args, **kwargs):
        plans = build_plans(*args, **kwargs)
        captured.extend(plans)
        return plans

    def reject_kernel_close(kernel):
        raise AssertionError("close_native_tables=False must preserve kernel tables")

    kwargs = {}
    if route != "text":
        kwargs["structured_sections"] = []
    if route == "native-sink":
        kwargs.update(structured_counts=[], native_undefined=[], native_fallback_lines=[])
    try:
        with monkeypatch.context() as patch:
            patch.setattr(emitter, "build_stack_map_plans", observe_plans)
            patch.setattr(IndexedFunctionKernel, "close_native_tables", reject_kernel_close)
            emitter._emit_prepared_aarch64_darwin_lines(
                prepared, optimize=False, close_native_tables=False, **kwargs,
            )
        assert len(captured) == 2
        assert all(
            arena._closed for plan in captured for arena in _arenas(plan.packed_records)
        )
        assert all(not kernel.instruction_metadata._closed for kernel in kernels)
    finally:
        for plan in captured:
            packed = plan.packed_records
            if packed is not None and any(not arena._closed for arena in _arenas(packed)):
                packed.close()
        for kernel in kernels:
            close_kernel(kernel)


@pytest.mark.parametrize("triple", _TARGETS)
def test_routes_record_call_safepoints_at_the_same_offsets(monkeypatch, triple):
    # The text route records a call safepoint at the return address
    # (test_precise_stackmap_table_policy); the structured-line and
    # native-sink routes must write the same table.
    # The text route is unoptimized here too, so every route emits the same code.
    monkeypatch.setenv(PRECISE_STACKMAP_TABLES_ENV, "1")
    source = _source(triple)
    tables = {}
    for route in _ROUTES:
        if route == "text":
            sections, _undefined = assemble_file(
                emitter.emit_aarch64_darwin_asm(source, optimize=False)
            )
        else:
            sections, _undefined = _emit(source, route)
        tables[route] = [
            section for section in sections if section.sectname == "__pcc_stackmaps"
        ]
    assert tables["text"]
    assert tables["text"] == tables["structured-lines"] == tables["native-sink"]
