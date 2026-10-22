"""A completed continuation's temporary result has a synchronous lexical owner."""
import re

from pcc.backend.self_backend_precise_stackmaps import build_stack_map_plans
from pcc.backend.self_backend_prepare import prepare_module_for_target
from pcc.backend.self_backend_x86_64_linux import _aggregate_returned_indirect
from pcc.frontends.python.codegen import generator_lowering, native_virtual_thread
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.py_ast import Name
from pcc.frontends.python.type_infer import infer_module


PROGRAM = '''import pcc.virtual_thread as vt
def child(value):
    vt.yield_now()
    return [value]
def parent(value):
    if vt.call(child, value):
        value = value + 1
    result = force_sink(vt.call(child, value))
    vt.yield_now()
    return result
thread = vt.spawn(parent, 1)
vt.run(1, 128)
print(vt.result(thread))
'''


class SinkProbeCodegen(L1CodeGen):
    """Select the production operand-result-sink route deterministically."""
    def _emit_call(self, expr):
        if isinstance(expr.func, Name) and expr.func.ident == "force_sink":
            slot = self._emit_slot_call_operand(expr.args[0], "test.result.sink")
            return self._take_slot_call_root(slot)
        return super()._emit_call(expr)


def test_result_handoff_uses_lexical_root_without_changing_child_or_sink(monkeypatch):
    original = generator_lowering.take_generator_frame_result
    original_yield = L1CodeGen._emit_generator_yield_value
    active = []
    records = []
    lexical_slots = []
    branch_records = {"completed": [], "direct": []}

    def reject_suspension(host, *args, **kwargs):
        assert not active, "result-publication region acquired a suspension edge"
        return original_yield(host, *args, **kwargs)

    def inspect_region(branch, host, expr, child_slot):
        ctx = host._generator_ctx_stack[-1]
        sink = host._slot_call_result_sink(expr)
        old_records = len(host._slot_call_root_records)
        state = ctx["next_state"]
        frame_before = tuple(ctx["frame_slots"].values())
        sink_record = host._slot_call_root_record(sink) if sink is not None else None
        active.append(True)
        try:
            value = original(host, expr, child_slot)
        finally:
            active.pop()
        assert ctx["next_state"] == state
        assert tuple(ctx["frame_slots"].values()) == frame_before
        assert any(entry[1] is child_slot for entry in ctx["frame_slots"].values())
        added = host._slot_call_root_records[old_records:]
        if sink is None:
            assert len(added) == 1
            slot, flag, lifo = added[0]
            assert flag is None and lifo
            lexical_slots.append(str(slot))
            assert not any(entry[1] is slot for entry in ctx["frame_slots"].values())
            assert not any(host.env[name][0] is slot for name in host._owned_local_names if name in host.env)
        else:
            assert not added
            assert host._slot_call_root_record(sink) is sink_record
            assert any(entry[1] is sink for entry in ctx["frame_slots"].values())
        records.append(sink is not None)
        branch_records[branch].append(sink is not None)
        return value

    def completed_region(host, expr, child_slot):
        return inspect_region("completed", host, expr, child_slot)

    def direct_region(host, expr, child_slot):
        return inspect_region("direct", host, expr, child_slot)

    monkeypatch.setattr(generator_lowering, "take_generator_frame_result", completed_region)
    monkeypatch.setattr(native_virtual_thread, "take_generator_frame_result", direct_region)
    monkeypatch.setattr(L1CodeGen, "_emit_generator_yield_value", reject_suspension)
    module = infer_module(parse_and_lift(PROGRAM, "lexical_result.py", "lexical_result"))
    codegen = SinkProbeCodegen(module, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    text = 'target triple = "x86_64-unknown-linux-gnu"\n' + str(codegen.generate(module))
    assert False in records and True in records, "fixture must execute both output-owner paths"
    assert all(branch_records.values()), "fixture must lower both result branches"
    for branch, sinks in branch_records.items():
        assert False in sinks and True in sinks, branch
    for slot in lexical_slots:
        _assert_exact_slot_protocol(text, slot)
    # Validation follows actual exceptional successors as well as success.
    prepared = prepare_module_for_target(text, aggregate_returned_indirect=_aggregate_returned_indirect)
    plans = build_stack_map_plans(prepared.functions, prepared.globals_, target="x86_64-linux")
    assert len(plans) == len(prepared.functions)


def _assert_exact_slot_protocol(text, slot):
    """Check actual calls on this slot, not declarations or other roots."""
    bodies = re.findall(r"^define[^\n]*\{\n(.*?)^}", text, re.M | re.S)
    body = next(body for body in bodies if slot + " = alloca ptr" in body)
    aliases = {slot}
    casts = re.findall(r"(%[\w.]+) = bitcast ptr (%[\w.]+) to ptr", body)
    changed = True
    while changed:
        changed = False
        for target, source in casts:
            if source in aliases and target not in aliases:
                aliases.add(target)
                changed = True
    pieces = re.split(r"^([\w.]+):\n", body, flags=re.M)
    blocks = dict(zip(pieces[1::2], pieces[2::2]))
    calls = []
    for label, block in blocks.items():
        for match in re.finditer(r"^([^\n]*\bcall\b[^\n]*@([\w.]+)\(([^\n]*)\))", block, re.M):
            arguments = re.findall(r"ptr (%[\w.]+)", match.group(3))
            if any(argument in aliases for argument in arguments):
                calls.append((match.group(2), label, match.start(), match.group(1)))
    def named(name):
        return [call for call in calls if call[0] == name]
    enters = named("pcc_gc_frame_enter_lifo")
    leaves = named("pcc_gc_frame_leave_lifo")
    errors = named("py_cleanup_one_root_preserving_exception")
    takes = named("pcc_gc_take_pinned_slot")
    assert len(enters) == 1, (slot, enters)
    assert len(leaves) == 2, (slot, leaves)
    assert len(errors) == len(takes) == 1, (slot, errors, takes)
    assert not named("pcc_gc_frame_enter") and not named("pcc_gc_frame_leave")
    error, take = errors[0], takes[0]
    error_leaves = [leave for leave in leaves if leave[1] == error[1]]
    success_leaves = [leave for leave in leaves if leave[1] == take[1]]
    assert len(error_leaves) == len(success_leaves) == 1
    assert error[2] < error_leaves[0][2]
    assert success_leaves[0][2] < take[2]
    assert error[1] != take[1]
    # Both lease-copy and lease-release status failures reach this same
    # exception-preserving cleanup block, directly or via error synthesis
    # (a shared status reporter may sit between them).
    successors = {label: re.findall(r"label %([\w.]+)", text) for label, text in blocks.items()}

    def reaches(start, goal):
        pending, seen = [start], set()
        while pending:
            label = pending.pop()
            if label == goal:
                return True
            if label in seen or label not in successors:
                continue
            seen.add(label)
            pending.extend(successors[label])
        return False

    lease_sites = [call for call in calls if call[0] in (
        "pcc_gc_root_copy_lease", "pcc_gc_root_copy_borrowed_lease",
        "pcc_gc_foreign_lease_acquire", "pcc_gc_foreign_lease_release")]
    assert len(lease_sites) >= 2, (slot, lease_sites)
    for _name, label, position, _line in lease_sites:
        branch = re.search(r"br i1 [^,]+, label %([\w.]+), label %[\w.]+", blocks[label][position:])
        assert branch is not None, (slot, label)
        assert reaches(branch.group(1), error[1]), (slot, label, branch.group(1))
    error_tail = blocks[error[1]][error_leaves[0][2]:]
    assert re.search(r"br label %", error_tail)
    # The exact root is loaded and pinned before its success-side leave;
    # the owned take remains the final handoff after retirement.
    loads = named("pcc_gc_load_ptr")
    assert len(loads) == 1
    loaded = re.search(r"(%[\w.]+) = call", loads[0][3]).group(1)
    success = blocks[take[1]]
    pin = re.search(r"call[^\n]*@pcc_gc_pin\(ptr " + re.escape(loaded) + r"\)", success)
    assert pin and pin.start() < success_leaves[0][2]
