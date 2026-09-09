from __future__ import annotations

from pcc.backend.self_backend_analysis import (
    collect_block_local_last_uses,
    collect_used_values,
)
import pcc.backend.self_backend_aarch64_darwin_calls as aarch64_calls
import pcc.backend.self_backend_aarch64_darwin_materialize as aarch64_materialize
import pcc.backend.self_backend_stackprep as self_backend_stackprep
from pcc.backend.self_backend_aarch64_darwin_abi import (
    aggregate_returned_indirect,
)
from pcc.backend.self_backend_kernel import (
    IndexedFunctionKernel,
    get_indexed_function_kernel,
)
from pcc.backend.self_backend_dispatch import emit_self_asm
from pcc.backend.self_backend_ir import (
    CompactParsedInstrView,
    TypeDesc,
    parsed_function_alloca_slot,
    parsed_function_alloca_value_id,
    parsed_function_value_slot,
    parsed_function_value_slot_id,
)
from pcc.backend.self_backend_parse import parse_self_backend_module
from pcc.backend.self_backend_prepare import prepare_parsed_function
from pcc.backend.self_backend_precise_stackmaps import PackedPlannedSafepoints
from pcc.backend.self_backend_stackprep import assign_stack_slots


_IR = """
target triple = "arm64-apple-darwin23.6.0"

define i64 @main(i64 %x) {
entry:
  %a = add i64 %x, 1
  %b = mul i64 %a, 2
  ret i64 %b
}
""".strip()

_MADD_IR = """
target triple = "arm64-apple-darwin23.6.0"

define i64 @fused(i64 %x, i64 %y, i64 %z) {
entry:
  %product = mul i64 %x, %y
  %result = add i64 %product, %z
  ret i64 %result
}
""".strip()

_REPEATED_POINTER_RESULT_IR = """
target triple = "arm64-apple-darwin23.6.0"

define i64 @loads() {
entry:
  %left.addr = alloca i64
  %right.addr = alloca i64
  %left = load i64, ptr %left.addr
  %right = load i64, ptr %right.addr
  %result = add i64 %left, %right
  ret i64 %result
}
""".strip()

_SLOT_REUSE_IR = """
target triple = "arm64-apple-darwin23.6.0"

define i64 @reuse() {
entry:
  %first = add i64 1, 2
  %discard = add i64 %first, 3
  %second = add i64 4, 5
  ret i64 %second
}
""".strip()

_DIRECT_CALL_RESULT_IR = """
target triple = "arm64-apple-darwin23.6.0"

declare i64 @callee(i64)

define i64 @caller(i64 %value) {
entry:
  %result = call i64 @callee(i64 %value)
  %adjusted = add i64 %result, 1
  ret i64 %adjusted
}
""".strip()

_DIRECT_CALL_ARG_IR = """
target triple = "arm64-apple-darwin23.6.0"

declare void @callee(i64)

define i64 @caller(i64 %value) {
entry:
  call void @callee(i64 %value)
  ret i64 0
}
""".strip()

_FIXED_INSTRUCTION_RECORD_IR = """
target triple = "arm64-apple-darwin23.6.0"

define i64 @fixed_records(i64 %value) {
entry:
  %slot = alloca i64
  %element = getelementptr i64, ptr %slot, i64 0
  store i64 %value, ptr %element
  %loaded = load i64, ptr %element
  %sum = add i64 %loaded, 1
  %positive = icmp sgt i64 %sum, 0
  %chosen = select i1 %positive, i64 %sum, i64 %loaded
  %pointer = inttoptr i64 %chosen to ptr
  %result = ptrtoint ptr %pointer to i64
  ret i64 %result
}
""".strip()

def _function():
    module = parse_self_backend_module(_IR)
    function = module.functions[0]
    prepare_parsed_function(function)
    return function


def test_indexed_function_kernel_matches_legacy_def_use_and_last_use() -> None:
    function = _function()
    expected_used = collect_used_values(function)
    expected_last_uses = collect_block_local_last_uses(function)

    kernel = get_indexed_function_kernel(function)

    assert kernel.legacy_used_values() == expected_used
    assert kernel.legacy_block_last_uses() == expected_last_uses
    assert kernel.block_names == ["entry"]
    assert kernel.instruction_count(0) == 2
    assert function.blocks == []
    assert len(kernel.instruction_kind_ids) == 2
    assert kernel.block_diagnostic_projections == 0
    assert kernel.instruction_arena_diagnostic_projections == 0
    assert [kernel.value_name(index) for index in range(len(kernel.value_names))] == [
        "x",
        "a",
        "b",
    ]
    assert kernel.defined_value_id(0, 0) == kernel.value_id("a")
    assert kernel.defined_value_id(0, 1) == kernel.value_id("b")
    assert kernel.definition_position(kernel.value_id("x")) == -1
    assert kernel.definition_position(kernel.value_id("a")) == 0
    assert kernel.definition_position(kernel.value_id("b")) == 1
    assert kernel.first_duplicate_definition_value_id == -1
    assert kernel.instruction_use_id(0, 0, 0) == kernel.value_id("x")
    assert kernel.instruction_use_id(0, 1, 0) == kernel.value_id("a")
    assert kernel.last_use(0, kernel.value_id("x")) is None
    assert kernel.last_use(0, kernel.value_id("a")) == 1
    assert kernel.last_use(0, kernel.value_id("b")) == 2
    assert kernel.profile_counters()["diagnostic_projections"] == 0
    assert kernel.diagnostic_instruction(0, 0).kind == "binop"
    assert kernel.profile_counters()["diagnostic_projections"] == 1
    assert kernel.profile_counters()["diagnostic_projections"] == 1
    block_fact = kernel.block_fact(0)
    assert (block_fact.first, block_fact.second) == (0, 2)
    assert block_fact.third == 1
    assert kernel.terminator_use_id(0, 0) == kernel.value_id("b")
    assert kernel.instruction_facts.diagnostic_values() == [
        kernel.value_id("a"),
        1,
        kernel.value_id("x"),
        -1,
        kernel.value_id("b"),
        1,
        kernel.value_id("a"),
        -1,
    ]
    first_metadata = kernel.instruction_metadata_by_id(0)
    second_metadata = kernel.instruction_metadata_by_id(1)
    assert first_metadata.second == 0
    assert second_metadata.second == 1
    assert (first_metadata.third, first_metadata.fourth) == (0, 0)
    assert (second_metadata.third, second_metadata.fourth) == (0, 0)
    assert [
        kernel.instruction_kind_id_by_id(index) for index in range(2)
    ] == [first_metadata.first, second_metadata.first]
    assert not hasattr(kernel, "instruction_volatile_flags")
    assert not hasattr(kernel, "instruction_arithmetic_flag_bits")


def test_indexed_function_kernel_is_deterministic_and_projection_is_explicit() -> None:
    first = get_indexed_function_kernel(_function())
    second = get_indexed_function_kernel(_function())

    assert first.block_names == second.block_names
    assert first.value_names == second.value_names
    assert first.block_name_buckets == {}
    assert first.value_name_buckets == {}
    assert first.block_name_index.diagnostic_values() == (
        second.block_name_index.diagnostic_values()
    )
    assert first.value_name_index.diagnostic_values() == (
        second.value_name_index.diagnostic_values()
    )
    assert first.block_facts.diagnostic_values() == (
        second.block_facts.diagnostic_values()
    )
    assert first.instruction_facts.diagnostic_values() == (
        second.instruction_facts.diagnostic_values()
    )
    assert first.instruction_overflow_use_ids.diagnostic_values() == (
        second.instruction_overflow_use_ids.diagnostic_values()
    )
    assert [
        first.value_type_id(value_id)
        for value_id in range(len(first.value_names))
    ] == [
        second.value_type_id(value_id)
        for value_id in range(len(second.value_names))
    ]
    assert first.profile_counters()["diagnostic_projections"] == 0

    projected = first.diagnostic_instruction(0, 0)
    assert projected.kind == "binop"
    assert first.profile_counters()["diagnostic_projections"] == 1


def test_type_interning_canonicalizes_distinct_pointer_wrappers_by_pointee() -> None:
    kernel = get_indexed_function_kernel(_function())
    first = TypeDesc("ptr", pointee=TypeDesc("int", 37))
    second = TypeDesc("ptr", pointee=TypeDesc("int", 37))

    first_id = kernel.intern_type(first)
    second_id = kernel.intern_type(second)

    assert first_id == second_id
    assert kernel.type_identity_ids[id(first)][1] is first
    assert id(second) not in kernel.type_identity_ids


def test_stackprep_publishes_one_canonical_type_object_per_type_id() -> None:
    module = parse_self_backend_module(_REPEATED_POINTER_RESULT_IR)
    function = module.functions[0]
    prepare_parsed_function(function)
    kernel = get_indexed_function_kernel(function)
    assign_stack_slots(
        function,
        aggregate_returned_indirect=aggregate_returned_indirect,
    )

    left_type = function.value_types["left.addr"]
    right_type = function.value_types["right.addr"]
    assert left_type is right_type
    assert kernel.type_desc(kernel.value_type_id(kernel.value_id("left.addr"))) is (
        left_type
    )
    assert kernel.type_desc(kernel.value_type_id(kernel.value_id("right.addr"))) is (
        left_type
    )
    left_id = kernel.value_id("left.addr")
    right_id = kernel.value_id("right.addr")
    assert kernel.alloca_offset(left_id) == function.alloca_slots["left.addr"].offset
    assert kernel.alloca_offset(right_id) == function.alloca_slots["right.addr"].offset
    assert kernel.type_desc(kernel.alloca_type_id(left_id)) is (
        function.alloca_slots["left.addr"].allocated_type
    )


def test_stackprep_interns_identity_free_reused_slot_records() -> None:
    module = parse_self_backend_module(_SLOT_REUSE_IR)
    function = module.functions[0]
    prepare_parsed_function(function)
    get_indexed_function_kernel(function)
    assign_stack_slots(
        function,
        aggregate_returned_indirect=aggregate_returned_indirect,
    )

    assert function.value_slots["first"].offset == function.value_slots["second"].offset
    assert function.value_slots["first"] is function.value_slots["second"]
    kernel = get_indexed_function_kernel(function)
    first_id = kernel.value_id("first")
    second_id = kernel.value_id("second")
    assert kernel.value_slot_id(first_id) == kernel.value_slot_id(second_id)
    assert kernel.value_slot_offset(first_id) == function.value_slots["first"].offset
    assert kernel.value_slot_type_id(first_id) == kernel.value_type_id(first_id)


def test_indexed_stackprep_keeps_legacy_slot_maps_empty() -> None:
    module = parse_self_backend_module(_REPEATED_POINTER_RESULT_IR)
    function = module.functions[0]
    prepare_parsed_function(function)
    kernel = get_indexed_function_kernel(function)
    assign_stack_slots(
        function,
        aggregate_returned_indirect=aggregate_returned_indirect,
        materialize_legacy_slots=False,
    )

    assert function.value_slots == {}
    assert function.value_slot_buckets == {}
    assert function.alloca_slots == {}
    assert function.alloca_slot_buckets == {}
    left_id = kernel.value_id("left.addr")
    loaded_id = kernel.value_id("left")
    assert parsed_function_alloca_value_id(function, "left.addr") == left_id
    assert parsed_function_value_slot_id(function, "left") == (
        kernel.value_slot_id(loaded_id)
    )
    assert parsed_function_alloca_slot(function, "left.addr").offset == (
        kernel.alloca_offset(left_id)
    )
    assert parsed_function_value_slot(function, "left").offset == (
        kernel.value_slot_offset(loaded_id)
    )
    assert kernel.legacy_slot_projections == 2


def test_aarch64_indexed_route_never_projects_instruction_views(monkeypatch) -> None:
    def unexpected_projection(_self, _arena, _dense_id):
        raise AssertionError("supported AArch64 emit projected an instruction view")

    def unexpected_safepoint_projection(_self, _record_index):
        raise AssertionError("supported AArch64 emit projected a safepoint record")

    monkeypatch.setattr(
        CompactParsedInstrView,
        "__init__",
        unexpected_projection,
    )
    monkeypatch.setattr(
        PackedPlannedSafepoints,
        "materialize",
        unexpected_safepoint_projection,
    )
    monkeypatch.setattr(
        IndexedFunctionKernel,
        "diagnostic_fixed_instruction_data",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("indexed fixed instruction projected tuple data")
        ),
    )
    monkeypatch.setattr(
        IndexedFunctionKernel,
        "diagnostic_alloca_data",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("indexed alloca projected tuple data")
        ),
    )
    monkeypatch.setattr(
        IndexedFunctionKernel,
        "diagnostic_gep_data",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("indexed getelementptr projected tuple data")
        ),
    )
    monkeypatch.setattr(
        self_backend_stackprep,
        "SlotInfo",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("indexed stackprep allocated a SlotInfo projection")
        ),
    )
    monkeypatch.setattr(
        self_backend_stackprep,
        "AllocaInfo",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("indexed stackprep allocated an AllocaInfo projection")
        ),
    )

    assembly = emit_self_asm(_IR)
    fused_assembly = emit_self_asm(_MADD_IR)
    fixed_assembly = emit_self_asm(_FIXED_INSTRUCTION_RECORD_IR)
    assert "_main:" in assembly
    assert "madd" in fused_assembly
    assert "_fixed_records:" in fixed_assembly


def test_aarch64_indexed_call_result_uses_dense_dest_id(monkeypatch) -> None:
    def unexpected_name_lookup(_func, _name):
        raise AssertionError("indexed call result recovered its slot by name")

    monkeypatch.setattr(
        aarch64_calls,
        "parsed_function_value_slot_offset",
        unexpected_name_lookup,
    )

    assembly = emit_self_asm(_DIRECT_CALL_RESULT_IR)
    assert "bl _callee" in assembly


def test_call_record_carries_managed_liveness_state_id():
    module = parse_self_backend_module(_DIRECT_CALL_RESULT_IR)
    function = module.functions[0]
    prepare_parsed_function(function)
    kernel = get_indexed_function_kernel(function)
    call_id = kernel.instruction_call_id(0, 0)

    assert kernel.call_span(call_id).fourth == 0
    kernel.publish_call_liveness_state_id(call_id, 37)
    assert kernel.call_span(call_id).fourth == 37


def test_aarch64_indexed_call_argument_uses_shared_use_id(monkeypatch) -> None:
    original_has_alloca = aarch64_materialize.parsed_function_has_alloca_slot
    original_slot_id = aarch64_materialize.parsed_function_value_slot_id

    def unexpected_alloca_lookup(func, name):
        if name == "value":
            raise AssertionError("indexed call argument recovered alloca by name")
        return original_has_alloca(func, name)

    def unexpected_slot_lookup(func, name):
        if name == "value":
            raise AssertionError("indexed call argument recovered slot by name")
        return original_slot_id(func, name)

    monkeypatch.setattr(
        aarch64_materialize,
        "parsed_function_has_alloca_slot",
        unexpected_alloca_lookup,
    )
    monkeypatch.setattr(
        aarch64_materialize,
        "parsed_function_value_slot_id",
        unexpected_slot_lookup,
    )
    monkeypatch.setattr(
        IndexedFunctionKernel,
        "diagnostic_call_data",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("indexed regular call projected tuple data")
        ),
    )

    assembly = emit_self_asm(_DIRECT_CALL_ARG_IR)
    assert "bl _callee" in assembly
