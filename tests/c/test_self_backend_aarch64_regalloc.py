from __future__ import annotations
import sys
import platform

import pcc.backend.self_backend_aarch64_darwin_regalloc as aarch64_regalloc
from pcc.backend.self_backend_aarch64_darwin_abi import (
    aggregate_returned_indirect,
)
from pcc.backend.self_backend_aarch64_darwin_regalloc import (
    allocate_aarch64_block_registers,
)
from pcc.backend.self_backend_dispatch import emit_self_asm
from pcc.backend.self_backend_ir import (
    PhiIncoming,
    PhiInstr,
    ParsedInstr,
    TypeDesc,
    text_key_mapping_get,
)
from pcc.backend.self_backend_kernel import get_indexed_function_kernel
from pcc.backend.self_backend_prepare import prepare_module_for_target

import pytest


@pytest.fixture(autouse=True)
def _block_local_allocator(monkeypatch):
    # These cases pin the block-local x1-x8 allocator's exact shapes; the
    # default callee-saved allocator is covered by
    # tests/c/test_self_backend_aarch64_callee_saved.py.
    monkeypatch.setenv("PCC_SELF_CALLEE_SAVED_REGISTERS", "0")



_TRIPLE = 'target triple = "arm64-apple-darwin25.5.0"\n'


_HOT_BLOCK_IR = _TRIPLE + """
define i64 @hot(i64 %a, i64 %b) {
entry:
  %v0 = add i64 %a, %b
  %v1 = mul i64 %v0, 3
  %v2 = xor i64 %v1, %a
  %v3 = add i64 %v2, %v0
  ret i64 %v3
}
"""


def _prepared_function(ir_text: str):
    prepared = prepare_module_for_target(
        ir_text,
        aggregate_returned_indirect=aggregate_returned_indirect,
    )
    assert len(prepared.functions) == 1
    return prepared.functions[0]


def _register_index(func, value_name: str) -> int | None:
    kernel = get_indexed_function_kernel(func)
    value_id = kernel.value_id(value_name)
    return None if value_id < 0 else kernel.value_register(value_id)


def test_aarch64_block_local_linear_scan_reuses_expired_registers():
    func = _prepared_function(_HOT_BLOCK_IR)
    allocate_aarch64_block_registers(func)

    assert _register_index(func, "v0") == 1
    assert _register_index(func, "v1") == 2
    # v1 dies as v2 is defined, then v0/v2 both die as v3 is defined.
    assert _register_index(func, "v2") == 2
    assert _register_index(func, "v3") == 1
    # Slots remain present as the mandatory fail-closed representation.
    assert all(name in func.value_slots for name in ("v0", "v1", "v2", "v3"))


def test_aarch64_regalloc_reuses_indexed_stackprep_last_uses(monkeypatch):
    func = _prepared_function(_HOT_BLOCK_IR)
    # The dense kernel is authoritative; stackprep no longer publishes an
    # object/string-keyed compatibility projection on the normal path.
    assert func.block_local_last_uses is None
    original_collect = aarch64_regalloc.collect_block_local_last_uses

    def unexpected_recompute(_func):
        raise AssertionError("regalloc recomputed stackprep last-use analysis")

    monkeypatch.setattr(
        aarch64_regalloc,
        "collect_block_local_last_uses",
        unexpected_recompute,
    )
    allocate_aarch64_block_registers(func)

    direct_func = _prepared_function(_HOT_BLOCK_IR)
    direct_func.block_local_last_uses = None
    calls = 0

    def counted_collect(target):
        nonlocal calls
        calls += 1
        return original_collect(target)

    monkeypatch.setattr(
        aarch64_regalloc,
        "collect_block_local_last_uses",
        counted_collect,
    )
    allocate_aarch64_block_registers(direct_func)
    # Clearing the legacy dict projection no longer discards the authoritative
    # indexed analysis published by stackprep.
    assert calls == 0


def test_aarch64_hot_block_avoids_intermediate_slot_traffic():
    asm = emit_self_asm(_HOT_BLOCK_IR)
    assert "_hot:" in asm

    # The spill-only lowering has fourteen x29-relative scalar transfers for
    # this unoptimized IR (two argument stores, eight operand loads, four
    # result stores).  Block-local allocation leaves only argument traffic;
    # later peepholes may legitimately reduce that count further.
    stack_transfers = [
        line
        for line in asm.splitlines()
        if "[x29, #-" in line
        and line.strip().split(None, 1)[0] in {"ldr", "ldur", "str", "stur"}
    ]
    assert len(stack_transfers) <= 5, stack_transfers


def test_aarch64_call_barrier_spills_touching_values_but_allows_post_call_locals():
    ir_text = _TRIPLE + """
declare i64 @opaque(i64)

define i64 @with_call(i64 %arg) {
entry:
  %pre = add i64 %arg, 1
  %before = xor i64 %pre, 2
  %called = call i64 @opaque(i64 %before)
  %after = add i64 %called, %before
  ret i64 %after
}
"""
    func = _prepared_function(ir_text)
    allocate_aarch64_block_registers(func)

    # pre dies strictly before the call.  before is both a call operand and
    # live after the call; called is a call result.  The latter two stay in
    # their mandatory slots, while pre/after are on one side of the clobber.
    assert _register_index(func, "pre") == 1
    assert _register_index(func, "before") is None
    assert _register_index(func, "called") is None
    assert _register_index(func, "after") == 1
    asm = emit_self_asm(ir_text)
    assert "bl _opaque" in asm
    assert "[x29, #-" in asm


def test_aarch64_call_result_live_across_a_later_call_stays_spilled():
    func = _prepared_function(_TRIPLE + '''
declare i64 @opaque(i64)
define i64 @two_calls(i64 %arg) {
entry:
  %first = call i64 @opaque(i64 %arg)
  %second = call i64 @opaque(i64 7)
  %result = add i64 %first, %second
  ret i64 %result
}
''')
    allocate_aarch64_block_registers(func)
    assert _register_index(func, "first") is None
    assert _register_index(func, "second") is None


# AArch64 Darwin execution
@pytest.mark.pcc_gate(probe=lambda: not (sys.platform != "darwin" or platform.machine() != "arm64"))
@pytest.mark.parametrize("mode", ("default", "function", "local"))
def test_aarch64_call_result_executes_through_indexed_emission(tmp_path, monkeypatch, mode):
    import platform
    import subprocess
    import sys
    import pytest
    from pcc.backend.self_backend_aarch64_darwin import emit_aarch64_darwin_indexed_transport
    from pcc.backend.self_backend_parse import parse_self_backend_module
    from pcc.backend.native_object import NativeObject
    from pcc.backend.macho_exec import link_executable
    from pcc.backend.arm64_asm_driver import assemble_file

    from pcc.backend import self_backend_aarch64_darwin_prologue as prologue
    from tests.aarch64_regalloc_scan_reference import (
        old_scan_allocator, select_mode,
    )

    from tests.scalar_arena_projection_ci_reference import disable_scalar_projections
    from tests.python.test_aarch64_regalloc_single_seed import SOURCE, load_facts

    select_mode(monkeypatch, mode)
    source = _TRIPLE + '''
declare i64 @opaque(i64)
define i64 @two_calls(i64 %arg) {
entry:
  %first = call i64 @opaque(i64 %arg)
  %second = call i64 @opaque(i64 7)
  br label %tail
tail:
  %result = add i64 %first, %second
  ret i64 %result
}
define i64 @shared_homes(i64 %arg) {
entry:
  %left = add i64 %arg, 3
  %right = add i64 %arg, 5
  br label %loop
loop:
  %index = phi i64 [0, %entry], [%next, %body]
  %total = phi i64 [0, %entry], [%sum, %body]
  %more = icmp slt i64 %index, 3
  br i1 %more, label %body, label %done
body:
  %part = add i64 %left, %right
  %sum = add i64 %total, %part
  %next = add i64 %index, 1
  br label %loop
done:
  %tail = add i64 %left, %right
  %result = add i64 %total, %tail
  ret i64 %result
}
define i32 @main() {
entry:
  %actual = call i64 @two_calls(i64 11)
  %bad = icmp ne i64 %actual, 66
  %shared = call i64 @shared_homes(i64 7)
  %shared_bad = icmp ne i64 %shared, 88
  %either_bad = or i1 %bad, %shared_bad
  %status = zext i1 %either_bad to i32
  ret i32 %status
}
'''
    # Force the callee's scratch register to disagree with its return value;
    # two self-emitted functions could accidentally leave the result in x1.
    helper_sections, helper_undefined = assemble_file('''
.section __TEXT,__text,regular,pure_instructions
.globl _opaque
_opaque:
  movz x1, #99
  movz x0, #33
  ret
''')
    helper = NativeObject.from_sections(helper_sections, undefined=helper_undefined)
    for optimize in (False, True):
        outputs = []
        graph_counts = [dict(pops=0, edges=0, hits=0) for _ in range(2)]
        reference = old_scan_allocator(facts_counts=graph_counts[0])
        # Execute the exact current helper with its real production globals;
        # the finite harness contributes only AST operation counters here.
        counted_facts, summary_counts = load_facts(SOURCE, count_summary=True)
        counted_facts.__globals__.update(vars(aarch64_regalloc))
        for index, allocator in enumerate((reference, allocate_aarch64_block_registers)):
            with monkeypatch.context() as patch:
                patch.setattr(prologue, "allocate_aarch64_block_registers", allocator)
                if index == 0:
                    disable_scalar_projections(patch, allocator)
                if index == 1:
                    patch.setattr(aarch64_regalloc, "_function_level_facts", counted_facts)
                module = parse_self_backend_module(source)
                transport = emit_aarch64_darwin_indexed_transport(module, optimize=optimize)
            sections, undefined = transport.assemble_sections()
            outputs.append((sections, undefined))
            executable = tmp_path / ("call-results-" + str(optimize) + "-" + str(index))
            executable.write_bytes(link_executable([NativeObject.from_sections(sections, undefined=undefined), helper]))
            executable.chmod(0o755)
            if transport.encoded_line_records is not None:
                transport.encoded_line_records.close()
            result = subprocess.run([str(executable)], capture_output=True, timeout=10)
            assert result.returncode == 0, result.stderr
        assert outputs[0] == outputs[1]
        graph_counts[1] = {key: summary_counts[key] for key in ("pops", "edges", "hits")}
        if mode != "local":
            assert summary_counts["summary_nodes"] > 0
            assert summary_counts["summary_homes"] > 0
            assert graph_counts[1]["hits"] > 0
            assert graph_counts[1]["pops"] < graph_counts[0]["pops"]
            assert graph_counts[1]["edges"] < graph_counts[0]["edges"]
        else:
            assert summary_counts["summary_nodes"] == 0
            assert graph_counts == [dict(pops=0, edges=0, hits=0)] * 2


def test_aarch64_linear_scan_spills_farthest_interval_under_pressure():
    ir_text = _TRIPLE + """
define i64 @pressure(i64 %arg) {
entry:
  %v0 = add i64 %arg, 0
  %v1 = add i64 %arg, 1
  %v2 = add i64 %arg, 2
  %v3 = add i64 %arg, 3
  %v4 = add i64 %arg, 4
  %v5 = add i64 %arg, 5
  %v6 = add i64 %arg, 6
  %v7 = add i64 %arg, 7
  %v8 = add i64 %arg, 8
  %r0 = add i64 %v0, %v8
  %r1 = add i64 %v1, %r0
  %r2 = add i64 %v2, %r1
  %r3 = add i64 %v3, %r2
  %r4 = add i64 %v4, %r3
  %r5 = add i64 %v5, %r4
  %r6 = add i64 %v6, %r5
  %r7 = add i64 %v7, %r6
  ret i64 %r7
}
"""
    func = _prepared_function(ir_text)
    allocate_aarch64_block_registers(func)

    # Nine simultaneously-live values exceed the eight-register pool.  The
    # farthest-ending v7 interval loses its optional mapping and therefore
    # uses the stack slot that stack preparation already assigned.
    assert _register_index(func, "v7") is None
    assert _register_index(func, "v8") is not None
    assert "v7" in func.value_slots


def test_aarch64_phi_inputs_and_results_keep_stack_slots():
    ir_text = _TRIPLE + """
define i64 @with_phi(i64 %arg) {
entry:
  %seed = add i64 %arg, 1
  br label %merge
merge:
  %joined = phi i64 [ %seed, %entry ]
  %local = add i64 %joined, 2
  %result = xor i64 %local, 3
  ret i64 %result
}
"""
    func = _prepared_function(ir_text)
    allocate_aarch64_block_registers(func)

    assert _register_index(func, "seed") is None
    assert _register_index(func, "joined") is None
    assert _register_index(func, "local") == 1
    assert _register_index(func, "result") == 1
    assert "seed" in func.value_slots
    assert "joined" in func.value_slots


def test_aarch64_phi_input_set_survives_inconsistent_native_hashes():
    class DifferentHashText(str):
        def __hash__(self):
            return super().__hash__() ^ 1

    ir_text = _TRIPLE + """
define i64 @with_false_hash_phi(i64 %arg) {
entry:
  %seed = add i64 %arg, 1
  br label %merge
merge:
  %joined = phi i64 [ %seed, %entry ]
    ret i64 %joined
}
"""
    func = _prepared_function(ir_text)
    kernel = get_indexed_function_kernel(func)
    phi = kernel.diagnostic_phi(1, 0)
    incoming = phi.incoming[0]
    projected_phi = PhiInstr(
        phi.dest,
        phi.type,
        (
            PhiIncoming(
                DifferentHashText(incoming.value),
                incoming.label,
            ),
        ),
    )
    assert projected_phi.incoming[0].value == incoming.value
    assert hash(projected_phi.incoming[0].value) != hash(incoming.value)

    allocate_aarch64_block_registers(func)

    # The explicit diagnostic projection is allowed to carry an equal spelling
    # with a different hash, but it cannot mutate the authoritative indexed
    # PHI edge consumed by register allocation.
    assert _register_index(func, "seed") is None
    assert _register_index(func, "joined") is None
    assert text_key_mapping_get(func.value_slots, "seed") is not None


def test_aarch64_cross_block_values_spill_while_new_block_locals_allocate():
    ir_text = _TRIPLE + """
define i64 @cross_block(i64 %arg) {
entry:
  %seed = add i64 %arg, 1
  br label %next
next:
  %result = add i64 %seed, 2
  ret i64 %result
}
"""
    func = _prepared_function(ir_text)
    allocate_aarch64_block_registers(func)

    assert _register_index(func, "seed") is None
    assert _register_index(func, "result") == 1
    assert "seed" in func.value_slots


def test_aarch64_false_hash_cross_block_use_cannot_escape_to_register():
    class DifferentHashText(str):
        def __hash__(self):
            return super().__hash__() ^ 1

    ir_text = _TRIPLE + """
define i64 @false_hash_boundary(i64 %arg) {
entry:
  %seed = add i64 %arg, 1
  %local = xor i64 %seed, 2
  %sink = add i64 %local, 3
  br label %next
next:
  ret i64 %seed
}
"""
    func = _prepared_function(ir_text)
    kernel = get_indexed_function_kernel(func)
    # An explicitly projected spelling may have an inconsistent native hash,
    # but it cannot rewrite the indexed cross-block use already consumed by
    # register allocation.
    projected_term = ParsedInstr(
        "ret", (TypeDesc("int", 64), DifferentHashText("seed"))
    )
    indexed_name = kernel.terminator_value(kernel.terminator_header(1).third)
    assert projected_term.data[1] == indexed_name
    assert hash(projected_term.data[1]) != hash(indexed_name)
    allocate_aarch64_block_registers(func)

    assert _register_index(func, "seed") is None
    assert _register_index(func, "local") == 1
    assert text_key_mapping_get(func.value_slots, "seed") is not None


def test_aarch64_float_and_vector_ssa_keep_mandatory_spill_slots():
    float_ir = _TRIPLE + """
define double @float_lane(double %a, double %b) {
entry:
  %sum = fadd double %a, %b
  ret double %sum
}
"""
    float_func = _prepared_function(float_ir)
    allocate_aarch64_block_registers(float_func)

    vector_ir = _TRIPLE + """
define <2 x i64> @vector_lane(<2 x i64> %a, <2 x i64> %b) {
entry:
  %sum = add <2 x i64> %a, %b
  ret <2 x i64> %sum
}
"""
    vector_func = _prepared_function(vector_ir)
    allocate_aarch64_block_registers(vector_func)

    assert _register_index(float_func, "sum") is None
    assert _register_index(vector_func, "sum") is None
    assert "sum" in float_func.value_slots
    assert "sum" in vector_func.value_slots


def test_aarch64_integer_intervals_allocate_inside_mixed_float_block():
    ir_text = _TRIPLE + """
define double @mixed_lane(i64 %arg, double %a, double %b) {
entry:
  %count = add i64 %arg, 1
  %sum = fadd double %a, %b
  %next = xor i64 %count, 2
  %converted = sitofp i64 %next to double
  %result = fadd double %sum, %converted
  ret double %result
}
"""
    func = _prepared_function(ir_text)
    allocate_aarch64_block_registers(func)

    # Floating definitions retain slots without disabling unrelated scalar
    # intervals in the same block (the pinned value-array hot loop is mixed).
    assert _register_index(func, "count") == 1
    assert _register_index(func, "next") == 1
    assert _register_index(func, "sum") is None
    assert _register_index(func, "converted") is None
    assert _register_index(func, "result") is None
    assert all(
        text_key_mapping_get(func.value_slots, name) is not None
        for name in ("count", "next", "sum", "converted", "result")
    )


def test_aarch64_pointer_ssa_uses_the_same_block_local_register_class():
    ir_text = _TRIPLE + """
define i64 @pointer_lane(i64 %arg) {
entry:
  %slot = alloca i64
  %ptr = getelementptr i64, ptr %slot, i64 0
  store i64 %arg, ptr %ptr
  %loaded = load i64, ptr %ptr
  ret i64 %loaded
}
"""
    func = _prepared_function(ir_text)
    allocate_aarch64_block_registers(func)

    assert _register_index(func, "ptr") == 1
    assert _register_index(func, "loaded") == 1


# The same immutable instruction planes feed the original-scan control and
# candidate. Preparation and optional target planning happen before counting.
_SCAN_SHAPES = ("safe", "call", "loop", "empty", "syscall", "inline-error",
                "madd", "reload", "vararg", "invalid-low", "invalid-high")


def _scan_reuse_function(monkeypatch, shape):
    from pcc.backend.self_backend_aarch64_darwin_abi import (
        aggregate_returned_indirect_indexed,
    )
    from pcc.backend.self_backend_parse import parse_self_backend_module
    from pcc.backend.self_backend_prepare import prepare_parsed_module_for_target
    from pcc.backend.self_backend_target_passes import plan_aarch64_madd_fusions
    from pcc.ir import ir

    if shape in ("syscall", "inline-error"):
        monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
        monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1")
        monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_FUSE_USES", "1")
        monkeypatch.setenv("PCC_DIRECT_INLINE_ERROR_EDGE_CAPTURE", "1")
        source = ir.Module(name="regalloc-scan-reuse")
        source.triple = "arm64-apple-darwin23.6.0"
        i64 = ir.IntType(64)
        function = ir.Function(source, ir.FunctionType(i64, [i64]), name="probe")
        function.args[0].name = "arg"
        entry = function.append_basic_block("entry")
        tail = function.append_basic_block("tail")
        builder = ir.IRBuilder(entry)
        first = builder.add(function.args[0], ir.Constant(i64, 1), name="first")
        if shape == "syscall":
            zero = ir.Constant(i64, 0)
            builder.syscall6(first, zero, zero, zero, zero, zero, zero, name="called")
        else:
            condition = builder.icmp_signed("!=", first, ir.Constant(i64, 0), name="failed")
            assert ir.IRBuilder_try_inline_error_edge(builder, condition, tail, 42, 0)
        result = builder.add(first, ir.Constant(i64, 2), name="result")
        builder.ret(result)
        builder.position_at_end(tail)
        builder.ret(function.args[0])
        module = source.direct_indexed_module()
    else:
        body = {
            "safe": """define i64 @probe(i64 %arg) {
entry:
  %first = add i64 %arg, 1
  %second = xor i64 %arg, 2
  br label %tail
tail:
  %result = add i64 %first, %second
  ret i64 %result
}""",
            "call": """declare i64 @opaque(i64)
define i64 @probe(i64 %arg) {
entry:
  %first = add i64 %arg, 1
  %called = call i64 @opaque(i64 %first)
  %again = call i64 @opaque(i64 %called)
  %result = add i64 %first, %again
  ret i64 %result
}""",
            "loop": """declare i64 @opaque(i64)
define i64 @probe(i64 %arg) {
entry:
  %seed = add i64 %arg, 7
  br label %head
head:
  %index = phi i64 [0, %entry], [%next, %body]
  %total = phi i64 [0, %entry], [%sum, %body]
  %more = icmp slt i64 %index, %arg
  br i1 %more, label %body, label %done
body:
  %called = call i64 @opaque(i64 %index)
  %part = add i64 %called, %seed
  %sum = add i64 %total, %part
  %next = add i64 %index, 1
  br label %head
done:
  ret i64 %total
}""",
            "empty": """define i64 @probe(i64 %arg) {
entry:
  br label %tail
tail:
  ret i64 %arg
}""",
            "madd": """define i64 @probe(i64 %arg, i64 %other) {
entry:
  %left = add i64 %arg, 1
  %right = add i64 %other, 2
  %product = mul i64 %left, %right
  %extra = xor i64 %arg, 3
  %result = add i64 %product, %extra
  ret i64 %result
}""",
            "vararg": """define i64 @probe(i64 %arg, ...) {
entry:
  %result = add i64 %arg, 1
  ret i64 %result
}""",
        }
        if shape == "reload":
            from tests.python.test_precise_stackmap_abi import _stale_managed_ssa_ir
            text = _stale_managed_ssa_ir("arm64-apple-darwin23.6.0")
        else:
            text = _TRIPLE + body["safe" if shape.startswith("invalid-") else shape]
        module = parse_self_backend_module(text)
    prepared = prepare_parsed_module_for_target(
        module,
        aggregate_returned_indirect=aggregate_returned_indirect,
        aggregate_returned_indirect_indexed=aggregate_returned_indirect_indexed,
        materialize_legacy_slots=False,
    )
    func, = prepared.functions
    assert func.indexed_slot_projection
    kernel = get_indexed_function_kernel(func)
    if shape == "madd":
        plan_aarch64_madd_fusions(func, enabled=True)
        assert func.aarch64_madd_fusions
    if shape == "reload":
        from pcc.backend.self_backend_precise_stackmaps import build_stack_map_plans
        plans = build_stack_map_plans(prepared.functions, prepared.globals_, target="aarch64-darwin")
        try:
            aarch64_regalloc.note_aarch64_reload_destinations(func, plans[0])
            assert func.aarch64_reload_slot_offsets
        finally:
            for plan in plans:
                if plan.packed_records is not None:
                    plan.packed_records.close()
    if shape.startswith("invalid-"):
        kind = -1 if shape == "invalid-low" else len(aarch64_regalloc.PARSED_INSTRUCTION_KINDS)
        kernel.instruction_metadata.set_unchecked(0, kind)
    return func


@pytest.mark.parametrize("shape", _SCAN_SHAPES)
@pytest.mark.parametrize("mode", ("default", "function", "local"))
@pytest.mark.parametrize("call_results", ("0", "1"))
def test_aarch64_regalloc_reuses_function_facts_without_changing_state(
    monkeypatch, shape, mode, call_results,
):
    from tests.aarch64_regalloc_scan_reference import (
        allocation_state, counted_function_facts, old_scan_allocator,
        removed_metadata_reads, select_mode,
    )

    select_mode(monkeypatch, mode, call_results=call_results)
    graph_counts = [dict(pops=0, edges=0, hits=0) for _ in range(2)]
    reference = old_scan_allocator(facts_counts=graph_counts[0])
    counted_facts = counted_function_facts(aarch64_regalloc._function_level_facts, graph_counts[1])
    funcs = [_scan_reuse_function(monkeypatch, shape) for _ in range(2)]
    kernels = [get_indexed_function_kernel(func) for func in funcs]
    planes = ("instruction_metadata", "instruction_facts", "instruction_record_scalars",
              "block_facts", "phi_scalars", "phi_incoming_scalars", "error_edge_scalars")

    def instructions(kernel):
        return tuple(tuple(getattr(kernel, name).diagnostic_values()) for name in planes)

    before = instructions(kernels[0])
    assert instructions(kernels[1]) == before
    assert allocation_state(funcs[0]) == allocation_state(funcs[1])
    expected_removed = removed_metadata_reads(funcs[0]) if mode != "local" else 0
    from tests.scalar_arena_projection_ci_reference import disable_scalar_projections

    projected_instructions = (sum(kernels[0].instruction_count(block_id)
                                  for block_id in range(len(kernels[0].block_names)))
                              if mode != "local" and not funcs[0].is_vararg else 0)
    metadata = type(kernels[0]).instruction_metadata_by_id
    counts = [0, 0]
    payload_counts = [0, 0]
    payload = type(kernels[0]).instruction_payload_id_by_id

    def counted_payload(kernel, instruction_id):
        for index, target in enumerate(kernels):
            if kernel is target:
                payload_counts[index] += 1
                break
        return payload(kernel, instruction_id)

    def counted(kernel, instruction_id):
        for index, target in enumerate(kernels):
            if kernel is target:
                counts[index] += 1
                break
        return metadata(kernel, instruction_id)

    with monkeypatch.context() as patch:
        patch.setattr(type(kernels[0]), "instruction_metadata_by_id", counted)
        patch.setattr(type(kernels[0]), "instruction_payload_id_by_id", counted_payload)
        with patch.context() as snapshot_patch:
            disable_scalar_projections(snapshot_patch, reference)
            reference(funcs[0])
        patch.setattr(aarch64_regalloc, "_function_level_facts", counted_facts)
        allocate_aarch64_block_registers(funcs[1])
    assert graph_counts[1]["pops"] <= graph_counts[0]["pops"]
    assert graph_counts[1]["edges"] <= graph_counts[0]["edges"]
    if shape == "safe" and mode != "local":
        assert graph_counts[1]["hits"] > 0
        assert graph_counts[1]["pops"] < graph_counts[0]["pops"]
        assert graph_counts[1]["edges"] < graph_counts[0]["edges"]
    assert payload_counts[0] == payload_counts[1]
    assert counts[0] - counts[1] == (expected_removed + 2 * projected_instructions
                                      + payload_counts[0])
    if shape not in ("empty", "vararg") and mode != "local":
        assert expected_removed > 0
    if shape == "vararg":
        assert counts == [0, 0]
    assert allocation_state(funcs[0]) == allocation_state(funcs[1])
    assert all(instructions(kernel) == before for kernel in kernels)
    if shape in ("syscall", "invalid-low", "invalid-high"):
        block = kernels[1].block_fact(0)
        for index in range(block.second):
            dest = kernels[1].instruction_fact_by_id(block.first + index).first
            if dest >= 0:
                assert kernels[1].value_register(dest) is None
    if shape == "reload" and mode == "default":
        for value_id in range(len(kernels[1].value_names)):
            if kernels[1].value_slot_offset(value_id) in funcs[1].aarch64_reload_slot_offsets:
                assert kernels[1].value_register(value_id) is None


@pytest.mark.parametrize("mode", ("default", "function", "local"))
def test_aarch64_regalloc_scan_reuse_preserves_syscall_failure(monkeypatch, mode):
    from pcc.backend import BackendUnavailable
    from pcc.backend import self_backend_aarch64_darwin_prologue as prologue
    from tests.aarch64_regalloc_scan_reference import old_scan_allocator, select_mode
    from tests.python.test_unsafe_syscall6 import _build_syscall6_module

    select_mode(monkeypatch, mode)
    source = _build_syscall6_module(triple="arm64-apple-darwin")
    from tests.scalar_arena_projection_ci_reference import disable_scalar_projections

    failures = []
    for index, allocator in enumerate((old_scan_allocator(), allocate_aarch64_block_registers)):
        with monkeypatch.context() as patch:
            patch.setattr(prologue, "allocate_aarch64_block_registers", allocator)
            if index == 0:
                disable_scalar_projections(patch, allocator)
            with pytest.raises(BackendUnavailable, match="syscall6") as failure:
                emit_self_asm(source)
        failures.append((type(failure.value), str(failure.value)))
    assert failures[0] == failures[1]
