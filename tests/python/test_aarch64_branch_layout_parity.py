"""Layout-aware conditional branches emit the same code through both paths.

The terminator emitter chooses a conditional branch's polarity from the block
laid out next and lets an edge without PHI moves fall through.  Under direct
instruction capture every emitted instruction is recorded when its helper is
called, and the native sink consumes those records in line order; emitting an
edge's PHI moves before the branch that precedes them in the output swapped
instructions in the native object only (a Stage1 pcc1 then looped on a
``cbz`` that targeted itself).  These shapes cover every branch form: no PHIs
with either edge falling through, PHIs on one edge, PHIs on both edges, a
loop back edge, and fused and materialized conditions.
"""

from __future__ import annotations

import pytest

from pcc.backend import self_backend_aarch64_darwin as darwin_backend
from pcc.backend.arm64_asm_driver import assemble_file, assemble_lines
from pcc.backend.self_backend_aarch64_darwin import (
    emit_aarch64_darwin_indexed_module,
    emit_aarch64_darwin_indexed_transport,
)
from pcc.ir import ir


# Checks the serialized precise stack-map table, which objects carry only on request.
pytestmark = pytest.mark.usefixtures("precise_stackmap_tables")


def _function(module, name, arg_count):
    i64 = ir.IntType(64)
    function = ir.Function(
        module, ir.FunctionType(i64, [i64] * arg_count), name=name
    )
    for index, arg in enumerate(function.args):
        arg.name = "a" + str(index)
    return function


def _build_shapes():
    module = ir.Module(name="branch-layout-shapes")
    module.triple = "arm64-apple-darwin23.6.0"
    i64 = ir.IntType(64)

    # No PHIs; the true block is laid out next and falls through.
    function = _function(module, "true_next", 2)
    entry = function.append_basic_block("entry")
    left = function.append_basic_block("left")
    right = function.append_basic_block("right")
    builder = ir.IRBuilder(entry)
    a, b = function.args
    builder.cbranch(builder.icmp_signed(">", a, b, name="gt"), left, right)
    builder.position_at_end(left)
    builder.ret(a)
    builder.position_at_end(right)
    builder.ret(b)

    # No PHIs; the false block is laid out next, and the condition is also
    # used as a value so it is materialized rather than fused.
    function = _function(module, "false_next", 2)
    entry = function.append_basic_block("entry")
    right = function.append_basic_block("right")
    left = function.append_basic_block("left")
    builder = ir.IRBuilder(entry)
    a, b = function.args
    condition = builder.icmp_signed("<", a, b, name="lt")
    flag = builder.zext(condition, i64, name="flag")
    builder.cbranch(condition, left, right)
    builder.position_at_end(right)
    builder.ret(builder.add(b, flag, name="r"))
    builder.position_at_end(left)
    builder.ret(builder.sub(a, flag, name="l"))

    # PHI only on the true edge; the false block is next.
    function = _function(module, "true_phi", 2)
    entry = function.append_basic_block("entry")
    other = function.append_basic_block("other")
    join = function.append_basic_block("join")
    builder = ir.IRBuilder(entry)
    a, b = function.args
    builder.cbranch(builder.icmp_signed(">", a, b, name="gt"), join, other)
    builder.position_at_end(other)
    summed = builder.add(a, b, name="sum")
    builder.branch(join)
    builder.position_at_end(join)
    phi = builder.phi(i64, name="p")
    ir.IRBuilder_add_incoming(phi, a, entry)
    ir.IRBuilder_add_incoming(phi, summed, other)
    builder.ret(phi)

    # PHI only on the false edge; the true block is next.
    function = _function(module, "false_phi", 2)
    entry = function.append_basic_block("entry")
    other = function.append_basic_block("other")
    join = function.append_basic_block("join")
    builder = ir.IRBuilder(entry)
    a, b = function.args
    builder.cbranch(builder.icmp_signed("==", a, b, name="eq"), other, join)
    builder.position_at_end(other)
    difference = builder.sub(a, b, name="diff")
    builder.branch(join)
    builder.position_at_end(join)
    phi = builder.phi(i64, name="p")
    ir.IRBuilder_add_incoming(phi, difference, other)
    ir.IRBuilder_add_incoming(phi, b, entry)
    builder.ret(phi)

    # PHIs on both edges; the true block is next.
    function = _function(module, "both_phi", 2)
    entry = function.append_basic_block("entry")
    first = function.append_basic_block("first")
    second = function.append_basic_block("second")
    builder = ir.IRBuilder(entry)
    a, b = function.args
    builder.cbranch(builder.icmp_signed(">=", a, b, name="ge"), first, second)
    builder.position_at_end(first)
    first_phi = builder.phi(i64, name="p1")
    ir.IRBuilder_add_incoming(first_phi, b, entry)
    builder.ret(builder.add(first_phi, a, name="f"))
    builder.position_at_end(second)
    second_phi = builder.phi(i64, name="p2")
    ir.IRBuilder_add_incoming(second_phi, a, entry)
    builder.ret(builder.sub(second_phi, b, name="s"))

    # A counted loop: PHIs carried around the back edge, exit not next.
    function = _function(module, "loop_sum", 1)
    entry = function.append_basic_block("entry")
    loop = function.append_basic_block("loop")
    body = function.append_basic_block("body")
    done = function.append_basic_block("done")
    builder = ir.IRBuilder(entry)
    (count,) = function.args
    builder.branch(loop)
    builder.position_at_end(loop)
    index = builder.phi(i64, name="i")
    total = builder.phi(i64, name="acc")
    builder.cbranch(builder.icmp_signed("<", index, count, name="more"), body, done)
    builder.position_at_end(body)
    next_total = builder.add(total, index, name="acc2")
    next_index = builder.add(index, ir.Constant(i64, 1), name="i2")
    builder.branch(loop)
    ir.IRBuilder_add_incoming(index, ir.Constant(i64, 0), entry)
    ir.IRBuilder_add_incoming(index, next_index, body)
    ir.IRBuilder_add_incoming(total, ir.Constant(i64, 0), entry)
    ir.IRBuilder_add_incoming(total, next_total, body)
    builder.position_at_end(done)
    builder.ret(total)
    return module.direct_indexed_module()


def test_branch_layout_shapes_match_between_asm_and_native_transport(monkeypatch):
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1")
    expected_asm = emit_aarch64_darwin_indexed_module(_build_shapes(), optimize=False)
    expected = assemble_file(expected_asm)
    transport = emit_aarch64_darwin_indexed_transport(_build_shapes(), optimize=False)
    try:
        actual = assemble_lines(
            transport.line_chunks, transport.structured_sections,
            transport.encoded_line_records, transport.structured_symbol_names,
        )
    finally:
        assert transport.native_finalized
    assert transport.fallback_instruction_count == 0
    assert actual == expected

    # The fallthrough edge carries no branch: the entry block of true_next is
    # one conditional branch to the false block and nothing else.
    body = expected_asm.split("_true_next:", 1)[1].split("_false_next:", 1)[0]
    entry = body.split("L_true_next_left:", 1)[0]
    branches = [
        line.split()[0]
        for line in entry.splitlines()
        if line.strip().startswith(("b ", "b.", "cbz ", "cbnz "))
    ]
    assert branches == ["b.le"], entry


def test_no_suffix_parts_are_not_sliced_or_extended_twice(monkeypatch):
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1")
    expected = assemble_file(
        emit_aarch64_darwin_indexed_module(_build_shapes(), optimize=False)
    )

    class UnslicedParts(list):
        def __getitem__(self, index):
            if isinstance(index, slice):
                raise AssertionError("no-suffix instruction parts were sliced")
            return super().__getitem__(index)

    original_parts = darwin_backend._emit_dense_indexed_instruction_parts
    original_extend = darwin_backend._NativeAArch64Emission.extend
    counts = {"parts": 0, "native_extends": 0}
    plan_type = darwin_backend.FunctionStackMapPlan
    original_entry = plan_type.append_packed_entry_span
    original_terminator = plan_type.append_packed_terminator_span
    fragment_counts = {"entry": 0, "terminator": 0}

    def nonempty_entry(plan, owner, fragment, block_id):
        assert plan.packed_records.entry_route_spans.get_unchecked(block_id * 2 + 1) > 0
        fragment_counts["entry"] += 1
        return original_entry(plan, owner, fragment, block_id)

    def nonempty_terminator(plan, owner, fragment, block_id):
        assert plan.packed_records.terminator_route_spans.get_unchecked(block_id * 2 + 1) > 0
        fragment_counts["terminator"] += 1
        return original_terminator(plan, owner, fragment, block_id)

    # Like empty suffix lists, absent block routes must not create/publish an
    # empty fragment. Nonempty routes still use the canonical producers, and
    # the complete assembly oracle below preserves labels, NOPs and metadata.
    monkeypatch.setattr(plan_type, "append_packed_entry_span", nonempty_entry)
    monkeypatch.setattr(plan_type, "append_packed_terminator_span", nonempty_terminator)

    def tracked_parts(*args, **kwargs):
        parts = UnslicedParts(original_parts(*args, **kwargs))
        counts["parts"] += 1
        return parts

    def tracked_native_extend(self, parts):
        if isinstance(parts, UnslicedParts):
            counts["native_extends"] += 1
        return original_extend(self, parts)

    monkeypatch.setattr(
        darwin_backend, "_emit_dense_indexed_instruction_parts", tracked_parts
    )
    monkeypatch.setattr(
        darwin_backend._NativeAArch64Emission, "extend", tracked_native_extend
    )
    buffered = emit_aarch64_darwin_indexed_module(_build_shapes(), optimize=False)
    assert counts["parts"] > 0
    assert assemble_file(buffered) == expected
    counts["parts"] = 0
    transport = emit_aarch64_darwin_indexed_transport(_build_shapes(), optimize=False)
    actual = assemble_lines(
        transport.line_chunks, transport.structured_sections,
        transport.encoded_line_records, transport.structured_symbol_names,
    )
    assert transport.native_finalized
    assert transport.fallback_instruction_count == 0
    assert counts["parts"] > 0
    assert counts["native_extends"] == counts["parts"]
    assert fragment_counts["entry"] > 0
    assert fragment_counts["terminator"] > 0
    assert actual == expected


@pytest.mark.parametrize("shape", ("branches", "root-reloads"))
@pytest.mark.parametrize("mode", ("default", "function", "local"))
def test_regalloc_scan_reuse_preserves_assembly_native_sections_and_stackmaps(
    monkeypatch, shape, mode,
):
    from pcc.backend import self_backend_aarch64_darwin_prologue as prologue
    from pcc.backend import self_backend_aarch64_darwin_regalloc as regalloc
    from pcc.backend.native_object import encode_native_object_from_sections
    from pcc.backend.precise_stackmap import decode_stack_map
    from pcc.backend.self_backend_parse import parse_self_backend_module
    from tests.aarch64_regalloc_scan_reference import (
        allocation_state, old_scan_allocator, select_mode,
    )
    from tests.python.test_precise_stackmap_abi import _stale_managed_ssa_ir

    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1")
    select_mode(monkeypatch, mode)

    def module():
        if shape == "branches":
            return _build_shapes()
        return parse_self_backend_module(_stale_managed_ssa_ir("arm64-apple-darwin23.6.0"))

    from tests.scalar_arena_projection_ci_reference import disable_scalar_projections

    outputs = []
    for index, allocator in enumerate((old_scan_allocator(), regalloc.allocate_aarch64_block_registers)):
        states = []

        def allocate(func):
            allocator(func)
            states.append((func.name, allocation_state(func)))

        with monkeypatch.context() as patch:
            patch.setattr(prologue, "allocate_aarch64_block_registers", allocate)
            if index == 0:
                disable_scalar_projections(patch, allocator)
            assembly = emit_aarch64_darwin_indexed_module(module(), optimize=False)
            assembly_states = list(states)
            states.clear()
            transport = emit_aarch64_darwin_indexed_transport(module(), optimize=False)
        sections, undefined = transport.assemble_sections()
        assert transport.native_finalized
        assert transport.fallback_instruction_count == 0
        assert states == assembly_states
        assert (sections, undefined) == assemble_file(assembly)
        encoded = encode_native_object_from_sections(sections, undefined=undefined)
        if shape == "root-reloads":
            stackmap = next(section for section in sections if section.sectname == "__pcc_stackmaps")
            decoded = decode_stack_map(stackmap.data)
            assert any(record.locations for function in decoded.functions for record in function.records)
            assert any(state["reload_offsets"] for _, state in states)
            assert undefined
        outputs.append((assembly, states, sections, undefined, encoded))
    # Complete sections include instruction words, symbols, relocations, unwind
    # and stackmaps. This compares two allocators, not just two emitter routes.
    assert outputs[0] == outputs[1]
