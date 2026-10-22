"""Portable generator operand registration and per-cell ownership regressions.

The small Host cases exercise production emitters on synthetic IR. The source
regression below drives the normal frontend fixture. Neither executes native
code or qualifies collector behavior. Snapshot AST comparisons belong to the
change's review evidence, not to the repository's permanent dependencies.
"""
from types import SimpleNamespace
import math
import re

import pytest

from pcc.ir.compat import ir
from pcc.backend.self_backend_parse import decode_ssa_name
from pcc.frontends.python.codegen import generator_lowering as generator
from pcc.frontends.python.codegen.call_object_lowering import CallObjectLoweringMixin
from pcc.frontends.python.codegen.core_helpers import CoreHelperMixin
from pcc.frontends.python.codegen.errors import L1CodegenError
from pcc.frontends.python.codegen.ownership_lowering import OwnershipLoweringMixin
from tests.python.root_slot_contract import RootSlotContract


PTR = ir.IntType(8).as_pointer()
I64 = ir.IntType(64)
VOID = ir.VoidType()


class Host(CallObjectLoweringMixin, OwnershipLoweringMixin, CoreHelperMixin):
    """Production root emitters in an independent, bounded function shell."""
    def __init__(self, function_name="resume", with_error=False):
        self.module = ir.Module(name="group_model")
        self.module.triple = "x86_64-unknown-linux-gnu"
        self.serial = 0
        self.current_function = ir.Function(
            self.module, ir.FunctionType(PTR, [PTR, I64]), function_name,
        )
        self.current_func_def = SimpleNamespace(name=function_name)
        self.entry = self.current_function.append_basic_block("entry")
        self.dispatch = self.current_function.append_basic_block("dispatch")
        self.body = self.current_function.append_basic_block("body")
        self.builder = ir.IRBuilder(self.entry)
        self._current_entry_block = None
        self._entry_alloca_insert_before_function = None
        self._entry_alloca_insert_index = 0
        self._entry_inline_edge_anchor_function = None
        self._entry_inline_edge_anchor_record = None
        self._freestanding_module = False
        self._current_global_names = set()
        self._current_param_names = set()
        self._exact_int_env_flags = {}
        self._for_target_owned_names = set()
        self.env = {}
        self._owned_local_names = set()
        self._owned_local_has_value = set()
        self._owned_local_flag_slots = {}
        self._owned_local_flag_allocas = {}
        self._gc_rooted_local_names = set()
        self._gc_rooted_local_order = []
        self._borrowed_gc_rooted_local_names = set()
        self._pinned_gc_rooted_local_names = set()
        self._fn_gc_root_slot_registry = {}
        self._fn_gc_root_exit_sites = {}
        self._fn_err_exit_gc_root_slots = {}
        self._fn_err_exit_owned_slots = {}
        self._fn_err_exit_blocks = {}
        self._fn_err_exit_finish_blocks = {}
        self._return_handoff_sites = []
        self._slot_call_root_records = []
        self._slot_call_root_record_index = {}
        self._slot_call_result_sinks = []
        self.runtime = {}
        for name, ret, args in (
            ("pcc_gc_frame_enter", VOID, [PTR, PTR]),
            ("pcc_gc_frame_enter_lifo", VOID, [PTR, PTR]),
            ("pcc_gc_frame_leave", VOID, [PTR]),
            ("pcc_gc_frame_leave_lifo", VOID, [PTR]),
            ("pcc_gc_store_root", VOID, [PTR, PTR]),
            ("py_gen_frame_get", PTR, [PTR, I64]),
            ("py_gen_frame_set", VOID, [PTR, I64, PTR]),
        ):
            self.runtime[name] = ir.Function(self.module, ir.FunctionType(ret, args), name)
        frame_root = self._alloca_in_entry(PTR, name="frame.root", init_null=True)
        self.builder.store(self.current_function.args[0], frame_root)
        self.builder.branch(self.dispatch)
        self.builder.position_at_end(self.dispatch)
        switch = self.builder.switch(ir.Constant(I64, 0), self.body)
        self.builder.position_at_end(self.body)
        self.context = {
            "resume_function": self.current_function, "frame_root": frame_root,
            "frame": self.current_function.args[0], "frame_slots": {}, "frame_names": [],
            "dispatch_bb": self.dispatch, "switch": switch,
            "operand_root_groups": [], "operand_root_group_members": {},
        }
        self._generator_ctx_stack = [self.context]
        self.releases = []
        if with_error:
            self.add_error_exit()

    def _fresh(self, name):
        self.serial += 1
        return name + "." + str(self.serial)

    def _generator_frame_helper(self, operation):
        return self.runtime["py_gen_frame_" + operation]

    def _emit_owned_valueclass_cleanup(self, skipped):
        assert skipped is None

    def _emit_release_owned_local_if_flagged(self, name, slot):
        self.releases.append((name, slot))
        return super()._emit_release_owned_local_if_flagged(name, slot)

    def add_error_exit(self):
        # Count-only models may leave this disconnected. The reachable-CFG
        # regressions below explicitly connect both this error and later exits.
        saved = self.builder._block
        error = self.current_function.append_basic_block("err.exit")
        finish = self.current_function.append_basic_block("err.finish")
        self.builder.position_at_end(error)
        self.builder.branch(finish)
        self.builder.position_at_end(finish)
        self.builder.ret(ir.Constant(PTR, None))
        name = self.current_function.name
        self._fn_err_exit_blocks[name] = error
        self._fn_err_exit_finish_blocks[name] = finish
        for record in self._fn_gc_root_slot_registry.get(name, ()):
            self._patch_fn_err_exit_gc_root_leave(record[0], record[1])
        self.builder.position_at_end(saved)
        return error, finish

    def roots(self, count):
        return [self._new_slot_call_root("operand" + str(index)) for index in range(count)]

    def finish(self, skip_name=None):
        self._emit_owned_local_cleanup(skip_name=skip_name)
        self.builder.ret(ir.Constant(PTR, None))


def body_text(block):
    return "\n".join(str(item) for item in block._instrs)


def call_count(text, name):
    return len(re.findall(r"\bcall\b[^\n]*@" + name + r"\(", text))


@pytest.mark.parametrize("count", [0, 1, 15, 16, 17, 31, 32])
def test_chunk_boundaries_keep_exact_members_and_fixed_empty_padding(count):
    host = Host()
    slots = host.roots(count)
    groups = host.context["operand_root_groups"]
    assert len(groups) == math.ceil(count / 16)
    assert len({id(slot) for slot in slots}) == count
    assert len(host.context["frame_slots"]) == count
    assert len(host._fn_gc_root_slot_registry.get("resume", ())) == count
    assert len(host._fn_err_exit_owned_slots.get("resume", ())) == count
    assert all(flag is not None and not lifo for _, flag, lifo in host._slot_call_root_records)
    for index, group in enumerate(groups):
        assert group["registered"]
        assert str(group["map"].initializer) == "16"
        assert group["used"] == min(16, count - index * 16)
        members = [slot for slot in slots if generator.generator_operand_root_group(host, slot) is group]
        assert len(members) == group["used"]
        assert all(str(slot.type) == "ptr" for slot in members)
    entry = body_text(host.entry)
    # The map sees every padding cell. All 16 are explicitly NULL before
    # the registration; padding has no flag, heap-frame entry or release.
    assert len(re.findall(r"store ptr null, ptr %gen\.operand\.empty\.cell", entry)) == len(groups) * 16
    assert call_count(entry, "pcc_gc_frame_enter") == len(groups)
    host.finish()
    assert call_count(str(host.module), "pcc_gc_frame_leave") == len(groups)
    assert [name for name, _ in host.releases] == sorted(host._owned_local_names)
    assert len(host.releases) == count


@pytest.mark.parametrize("before, after", [(0, 1), (1, 15), (16, 1), (17, 14), (31, 1), (32, 1)])
def test_late_registration_patches_early_exit_once_per_new_chunk(before, after):
    host = Host()
    host.roots(before)
    host.finish()
    early = host._fn_gc_root_exit_sites["resume"][0]
    early_leaves = call_count(body_text(early), "pcc_gc_frame_leave")
    later = host.current_function.append_basic_block("later")
    host.builder.position_at_end(later)
    host.roots(after)
    expected = math.ceil((before + after) / 16)
    assert early_leaves == math.ceil(before / 16)
    assert call_count(body_text(early), "pcc_gc_frame_leave") == expected
    host.finish()
    assert len(host._fn_gc_root_slot_registry["resume"]) == before + after


@pytest.mark.parametrize("error_first", [False, True])
def test_error_path_keeps_every_owned_release_before_group_leaves(error_first):
    host = Host(with_error=error_first)
    slots = host.roots(17)
    if not error_first:
        host.add_error_exit()
    error = host._fn_err_exit_blocks["resume"]
    finish = host._fn_err_exit_finish_blocks["resume"]
    assert call_count(body_text(error), "pcc_gc_store_root") == 17
    assert call_count(body_text(error), "pcc_gc_frame_leave") == 0
    assert call_count(body_text(finish), "pcc_gc_frame_leave") == 2
    entries = host._fn_err_exit_owned_slots["resume"]
    assert [entry[1] for entry in entries] == slots
    assert all(entry[3] for entry in entries)
    # Repatching cannot drop a cell's release or duplicate a group leave.
    for name, slot in host._fn_gc_root_slot_registry["resume"]:
        host._patch_fn_err_exit_gc_root_leave(name, slot)
    assert call_count(body_text(error), "pcc_gc_store_root") == 17
    assert call_count(body_text(finish), "pcc_gc_frame_leave") == 2


def test_borrowed_special_and_lexical_roots_stay_outside_operand_groups():
    host = Host()
    operand = host.roots(1)[0]
    lexical = host._new_slot_call_root("completed.result", synchronous_lexical=True)
    borrowed = host._alloca_in_entry(PTR, "borrowed", init_null=True)
    host._ensure_borrowed_local_gc_root("borrowed", borrowed, PTR)
    special = host._alloca_in_entry(PTR, "special", init_null=True)
    host._ensure_owned_local_gc_root("special", special, PTR)
    assert generator.generator_operand_root_group(host, operand) is not None
    assert all(generator.generator_operand_root_group(host, slot) is None
               for slot in (lexical, borrowed, special))
    assert host._slot_call_root_record(lexical)[1:] == (None, True)
    assert call_count(body_text(host.entry), "pcc_gc_frame_enter") == 3
    assert call_count(body_text(host.body), "pcc_gc_frame_enter_lifo") == 1
    assert len(host.context["frame_slots"]) == 1


def test_current_function_and_distinct_contexts_do_not_share_chunks():
    host = Host()
    first = host.roots(17)
    first_groups = tuple(host.context["operand_root_groups"])
    original_function = host.current_function
    other = ir.Function(host.module, ir.FunctionType(PTR, []), "method_resume")
    host.current_function = other
    assert generator.generator_operand_root_group(host, first[0]) is None
    with pytest.raises(L1CodegenError, match="requires its resume function"):
        generator.allocate_generator_operand_root(host, "wrong")
    host.current_function = original_function
    assert generator.generator_operand_root_group(host, first[0]) is first_groups[0]
    second_host = Host(function_name="method_resume")
    second = second_host.roots(31)
    assert len(second_host.context["operand_root_groups"]) == 2
    assert all(generator.generator_operand_root_group(second_host, slot) is None for slot in first)
    assert all(generator.generator_operand_root_group(host, slot) is None for slot in second)
    assert [group["used"] for group in first_groups] == [16, 1]
    assert [group["used"] for group in second_host.context["operand_root_groups"]] == [16, 15]
    # Runtime instances use entry allocas, never global storage for cells.
    assert all(group["base"].type.pointee.count == 16 for group in first_groups)
    assert all(not isinstance(group["base"], ir.GlobalVariable) for group in first_groups)


def test_corrupted_identity_index_and_borrowed_registration_fail_closed():
    host = Host()
    slot = generator.allocate_generator_operand_root(host, "unregistered")
    group = generator.generator_operand_root_group(host, slot)
    wrong = host._alloca_in_entry(PTR, "wrong", init_null=True)
    host.context["operand_root_group_members"][id(slot)] = (wrong, group)
    with pytest.raises(L1CodegenError, match="identity mismatch"):
        generator.generator_operand_root_group(host, slot)
    host.context["operand_root_group_members"][id(slot)] = (slot, group)
    with pytest.raises(L1CodegenError, match="owning frame map"):
        host._ensure_borrowed_local_gc_root("bad", slot, PTR)
    assert not group["registered"]


@pytest.mark.parametrize("bad_index", [-1, 17])
def test_corrupted_chunk_index_fails_before_allocating_or_registering(bad_index):
    host = Host()
    slot = host.roots(1)[0]
    group = generator.generator_operand_root_group(host, slot)
    group["used"] = bad_index
    before = str(host.module)
    with pytest.raises(L1CodegenError, match="index out of range"):
        generator.allocate_generator_operand_root(host, "bad_index")
    assert str(host.module) == before



@pytest.mark.parametrize("count", [0, 16, 17, 31, 32])
def test_small_ir_groups_pass_unchanged_precise_root_verifier(count):
    from pcc.backend.self_backend_prepare import prepare_module_for_target
    from pcc.backend.self_backend_precise_stackmaps import build_stack_map_plans
    from pcc.backend.self_backend_x86_64_linux import _aggregate_returned_indirect

    host = Host(with_error=True)
    host.roots(count)
    host.finish()
    prepared = prepare_module_for_target(
        str(host.module), aggregate_returned_indirect=_aggregate_returned_indirect,
    )
    plans = build_stack_map_plans(prepared.functions, prepared.globals_, target="x86_64-linux")
    assert len(plans) == len(prepared.functions)


def test_group_map_and_null_padding_do_not_change_after_early_projection():
    host = Host()
    first = host.roots(1)[0]
    group = generator.generator_operand_root_group(host, first)
    map_before = str(group["map"])
    initial_before = group["map"].initializer
    host.roots(30)
    assert str(group["map"]) == map_before
    assert group["map"].initializer is initial_before
    assert all(str(chunk["map"].initializer) == "16"
               for chunk in host.context["operand_root_groups"])
    assert host.context["operand_root_groups"][1]["map"] is not group["map"]


def test_nested_function_context_temporarily_hides_outer_chunks():
    host = Host()
    outer_slot = host.roots(1)[0]
    outer = host.context
    inner = Host(function_name="method_generator")
    inner.roots(17)
    host._generator_ctx_stack.append(inner.context)
    assert generator.generator_operand_root_group(host, outer_slot) is None
    with pytest.raises(L1CodegenError, match="requires its resume function"):
        generator.allocate_generator_operand_root(host, "bad_nested")
    host._generator_ctx_stack.pop()
    assert host._generator_ctx_stack[-1] is outer
    host.roots(15)
    assert len(outer["operand_root_groups"]) == 1
    assert outer["operand_root_groups"][0]["used"] == 16
    assert [group["used"] for group in inner.context["operand_root_groups"]] == [16, 1]


def _parsed_function(text, name):
    from pcc.backend.self_backend_kernel import get_indexed_function_kernel
    from pcc.backend.self_backend_parse import parse_self_backend_module
    from pcc.backend.self_backend_verify import verify_parsed_module

    module = parse_self_backend_module(text)
    verify_parsed_module(module)
    function = next(fn for fn in module.functions if fn.name == name)
    blocks = get_indexed_function_kernel(function).materialize_legacy_blocks(function)
    return module, blocks


def _reachable_blocks(blocks):
    by_name = {block.name: block for block in blocks}
    seen, pending = set(), [blocks[0].name]
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        seen.add(name)
        term = by_name[name].terminator
        if term.kind == "br":
            pending.append(term.data[0])
        elif term.kind == "br_cond":
            pending.extend(term.data[1:])
        elif term.kind == "switch":
            pending.append(term.data[2])
            pending.extend(name for _, name in term.data[3])
    return seen


def _assert_precise_roots(text):
    from pcc.backend.self_backend_prepare import prepare_module_for_target
    from pcc.backend.self_backend_precise_stackmaps import build_stack_map_plans
    from pcc.backend.self_backend_x86_64_linux import _aggregate_returned_indirect

    prepared = prepare_module_for_target(
        text, aggregate_returned_indirect=_aggregate_returned_indirect,
    )
    plans = build_stack_map_plans(prepared.functions, prepared.globals_, target="x86_64-linux")
    assert len(plans) == len(prepared.functions)


@pytest.mark.parametrize("error_first", [False, True])
@pytest.mark.parametrize("before, after", [
    (0, 0), (0, 1), (0, 17), (1, 15), (15, 1), (16, 1), (17, 16), (31, 1), (32, 1),
])
def test_reachable_error_and_early_exit_keep_late_chunk_ownership(error_first, before, after):
    host = Host(with_error=error_first)
    slots = host.roots(before)
    early = host.current_function.append_basic_block("early")
    later = host.current_function.append_basic_block("later")
    choose_early = host.builder.icmp_signed("==", host.current_function.args[1], ir.Constant(I64, 0))
    host.builder.cbranch(choose_early, early, later)
    host.builder.position_at_end(early)
    host.finish()
    early_exit = host.builder._block
    host.builder.position_at_end(later)
    slots.extend(host.roots(after))
    if not error_first:
        host.add_error_exit()
    normal = host.current_function.append_basic_block("normal")
    choose_error = host.builder.icmp_signed("==", host.current_function.args[1], ir.Constant(I64, 1))
    host.builder.cbranch(choose_error, host._fn_err_exit_blocks["resume"], normal)
    host.builder.position_at_end(normal)
    host.finish()
    normal_exit = host.builder._block
    text = str(host.module)
    module, blocks = _parsed_function(text, "resume")
    assert _reachable_blocks(blocks) == {block.name for block in blocks}
    addresses = RootSlotContract(blocks, module.globals_)
    identities = addresses.require_distinct([decode_ssa_name(str(cell)) for cell in slots])
    assert len(identities) == before + after
    groups = {decode_ssa_name(str(group["base"])) for group in host.context["operand_root_groups"]}
    addresses.assert_frame_exits(groups)
    expected = math.ceil((before + after) / 16)
    for block in (early_exit, host._fn_err_exit_finish_blocks["resume"], normal_exit):
        assert call_count(body_text(block), "pcc_gc_frame_leave") == expected
    error = host._fn_err_exit_blocks["resume"]
    assert call_count(body_text(error), "pcc_gc_store_root") == before + after
    assert call_count(body_text(error), "pcc_gc_frame_leave") == 0
    assert all(entry[3] for entry in host._fn_err_exit_owned_slots.get("resume", ()))
    _assert_precise_roots(text)


def _root_contract_fixture(
    *, first=0, second=1, count=2, registration=True, initialized=2, overwrite=None,
):
    """Hand-written parsed IR controls are independent of generator lowering."""
    enter = "call void @pcc_gc_frame_enter(ptr @map, ptr %base)" if registration else ""
    initialization = "\n".join(
        f"%empty{index} = getelementptr [2 x ptr], ptr %base, i32 0, i32 {index}\n"
        f"store ptr null, ptr %empty{index}" for index in range(initialized)
    )
    pollution = ""
    if overwrite is not None:
        write_type, write_offset = overwrite
        pollution = (f"%write = getelementptr i8, ptr %base, i64 {write_offset}\n"
                     f"store {write_type} 1024, ptr %write")
    text = f'''target triple = "x86_64-unknown-linux-gnu"
@map = internal constant i32 {count}
declare void @pcc_gc_frame_enter(ptr, ptr)
define void @control(i32 %dynamic) {{
entry:
  %base = alloca [2 x ptr]
  {initialization}
  {pollution}
  %first = getelementptr [2 x ptr], ptr %base, i32 0, i32 {first}
  %second = getelementptr [2 x ptr], ptr %base, i32 0, i32 {second}
  %alias = bitcast ptr %second to ptr
  {enter}
  ret void
}}
'''
    module, blocks = _parsed_function(text, "control")
    return RootSlotContract(blocks, module.globals_)


def test_root_contract_accepts_group_geps_and_bitcasts_without_losing_cell_identity():
    addresses = _root_contract_fixture()
    roots = addresses.require_distinct(("first", "alias"))
    assert roots == (("base", 0), ("base", 8))
    assert addresses.slot("second") == addresses.slot("alias")


@pytest.mark.parametrize("options, diagnostic", [
    ({"second": 0}, "alias the same physical root cell"),
    ({"second": 2}, "root offset is outside"),
    ({"first": -1}, "not a pointer cell"),
    ({"second": "%dynamic"}, "constant byte offset"),
    ({"count": 3}, "owning map range is outside"),
    ({"count": -2}, "positive owning map"),
    ({"count": 0}, "positive owning map"),
    ({"registration": False}, "exactly one covering owning registration"),
    ({"initialized": 1}, "every mapped cell needs NULL initialization"),
    ({"overwrite": ("i64", 0)}, "every mapped cell needs NULL initialization"),
    ({"overwrite": ("i32", 4)}, "every mapped cell needs NULL initialization"),
])
def test_root_contract_rejects_alias_bounds_borrowing_and_missing_coverage(options, diagnostic):
    addresses = _root_contract_fixture(**options)
    with pytest.raises(AssertionError, match=diagnostic):
        addresses.require_distinct(("first", "alias"))


@pytest.mark.parametrize("lexical", [False, True])
def test_root_contract_accepts_standalone_alloca_and_audited_lexical_registration(lexical):
    runtime = "pcc_gc_frame_enter_lifo" if lexical else "pcc_gc_frame_enter"
    branch = "br label %body\nbody:" if lexical else ""
    text = f'''target triple = "x86_64-unknown-linux-gnu"
@map = internal constant i32 1
declare void @{runtime}(ptr, ptr)
define void @control() {{
entry:
  %owner = alloca ptr
  store ptr null, ptr %owner
  %alias = bitcast ptr %owner to ptr
  {branch}
  call void @{runtime}(ptr @map, ptr %alias)
  ret void
}}
'''
    module, blocks = _parsed_function(text, "control")
    addresses = RootSlotContract(blocks, module.globals_)
    assert addresses.slot("alias") == ("owner", 0)
    assert addresses.require_owning("alias", allow_lifo=lexical) == ("owner", 0, 1)
    if lexical:
        with pytest.raises(AssertionError, match="must not use a lexical root"):
            addresses.require_owning("alias")


def _source_with_repeated_generator_exits():
    # The normal source frontend fixture selects production operand ownership;
    # no alternate compiler/module identity is installed for this test.
    lines = ["def probe(choice):", "    value = slot_operand_probe([0])",
             "    if choice == 0:", "        return value", "    yield value"]
    for index in range(1, 19):
        lines.extend([f"    value = slot_operand_probe([{index}])",
                      f"    if choice == {index}:", "        return value"])
    lines.extend(["    yield value", "    return value"])
    return "\n".join(lines) + "\n"


@pytest.fixture(scope="module")
def source_generator_group_ir():
    from tests.python.test_slot_call_operand_roots import _emit

    text = _emit(_source_with_repeated_generator_exits())
    module, blocks = _parsed_function(text, "user_slot_operand_probe__gen_resume")
    return text, module, blocks


def test_source_generator_late_chunks_cover_repeated_early_exits(source_generator_group_ir):
    text, module, blocks = source_generator_group_ir
    addresses = RootSlotContract(blocks, module.globals_)
    groups = {name for name in addresses.allocas if name.startswith("gen.operand.roots.")}
    assert len(groups) >= 2, "source regression must actually cross a group boundary"
    operands = [name for name in addresses.geps if name.startswith("probe.operand")]
    assert len(operands) >= 19
    addresses.require_distinct(operands)
    returns = addresses.assert_frame_exits(groups)
    rooted_returns = {name for name, histories in returns.items() if any(histories)}
    assert len(rooted_returns) >= 20, "exercise early exits lowered before later chunks"
    _assert_precise_roots(text)


def _without_return_leaves(blocks, target):
    copied = [SimpleNamespace(name=block.name, instructions=list(block.instructions),
                              terminator=block.terminator) for block in blocks]
    changed = next(block for block in copied if block.name == target)
    assert changed.terminator.kind in ("ret", "ret_void")
    before = len(changed.instructions)
    changed.instructions = [ins for ins in changed.instructions
                            if not (RootSlotContract.called(ins, "pcc_gc_frame_leave")
                                    or RootSlotContract.called(ins, "pcc_gc_frame_leave_lifo"))]
    assert len(changed.instructions) < before, "missing-leaves mutation must actually apply"
    return copied


def test_source_generator_rejects_deleting_a_rooted_branch_all_leaves(source_generator_group_ir):
    _, module, blocks = source_generator_group_ir
    addresses = RootSlotContract(blocks, module.globals_)
    groups = {name for name in addresses.allocas if name.startswith("gen.operand.roots.")}
    assert len(groups) >= 2
    returns = addresses.assert_frame_exits(groups)  # Unmodified positive must pass first.
    rooted_returns = {name for name, histories in returns.items() if any(histories)}
    target = next(block.name for block in blocks if block.name in rooted_returns
                  and any(addresses.called(ins, "pcc_gc_frame_leave") for ins in block.instructions))
    mutated = _without_return_leaves(blocks, target)
    applied = [target]
    with pytest.raises(AssertionError, match="return reached with active root frames"):
        RootSlotContract(mutated, module.globals_).assert_frame_exits(groups)
    assert applied == [target], "the negative case must reach the CFG ownership validator"


def _frame_exit_fixture():
    # Twenty-two rooted returns deliberately retain more than twenty good
    # branches after deleting one branch's complete retirement sequence.
    cases = "\n".join(f"i32 {index}, label %exit{index}" for index in range(21))
    exits = "\n".join(f'''exit{index}:
  call void @pcc_gc_frame_leave_lifo(ptr %lexical)
  call void @pcc_gc_frame_leave(ptr %borrowed)
  call void @pcc_gc_frame_leave(ptr %base)
  ret void''' for index in range(22))
    text = f'''target triple = "x86_64-unknown-linux-gnu"
@group_map = internal constant i32 2
@borrowed_map = internal constant i32 -1
@lexical_map = internal constant i32 1
declare void @pcc_gc_frame_enter(ptr, ptr)
declare void @pcc_gc_frame_leave(ptr)
declare void @pcc_gc_frame_enter_lifo(ptr, ptr)
declare void @pcc_gc_frame_leave_lifo(ptr)
define void @control(i1 %activation_failed, i32 %choice) {{
entry:
  %base = alloca [2 x ptr]
  %borrowed = alloca ptr
  %lexical = alloca ptr
  %empty0 = getelementptr [2 x ptr], ptr %base, i32 0, i32 0
  %empty1 = getelementptr [2 x ptr], ptr %base, i32 0, i32 1
  store ptr null, ptr %empty0
  store ptr null, ptr %empty1
  store ptr null, ptr %borrowed
  store ptr null, ptr %lexical
  br i1 %activation_failed, label %activation_failure, label %registered
activation_failure:
  ret void
registered:
  call void @pcc_gc_frame_enter(ptr @group_map, ptr %base)
  call void @pcc_gc_frame_enter(ptr @borrowed_map, ptr %borrowed)
  call void @pcc_gc_frame_enter_lifo(ptr @lexical_map, ptr %lexical)
  switch i32 %choice, label %exit21 [
    {cases}
  ]
{exits}
}}
'''
    return _parsed_function(text, "control")


def test_frame_exit_contract_accepts_fast_return_borrowed_and_lexical_frames():
    module, blocks = _frame_exit_fixture()
    returns = RootSlotContract(blocks, module.globals_).assert_frame_exits({"base"})
    assert returns["activation_failure"] == {False}
    assert {name for name, states in returns.items() if states == {True}} == {
        f"exit{index}" for index in range(22)
    }


@pytest.mark.parametrize("target", ["exit0", "exit21"])
def test_frame_exit_contract_rejects_deleting_all_leaves_even_with_many_good_returns(target):
    module, blocks = _frame_exit_fixture()
    positive = RootSlotContract(blocks, module.globals_).assert_frame_exits({"base"})
    assert positive[target] == {True}
    mutated = _without_return_leaves(blocks, target)
    applied = [target]
    with pytest.raises(AssertionError, match="return reached with active root frames"):
        RootSlotContract(mutated, module.globals_).assert_frame_exits({"base"})
    assert applied == [target], "the negative case must reach the CFG ownership validator"


@pytest.mark.parametrize("owner", ["borrowed", "lexical"])
def test_frame_exit_contract_rejects_leaks_outside_the_operand_group(owner):
    module, blocks = _frame_exit_fixture()
    RootSlotContract(blocks, module.globals_).assert_frame_exits({"base"})
    copied = [SimpleNamespace(name=block.name, instructions=list(block.instructions),
                              terminator=block.terminator) for block in blocks]
    target = next(block for block in copied if block.name == "exit0")
    before = len(target.instructions)
    target.instructions = [ins for ins in target.instructions
                           if not (ins.kind == "call"
                                   and ins.data[2] in ("pcc_gc_frame_leave", "pcc_gc_frame_leave_lifo")
                                   and RootSlotContract.args(ins)[0] == owner)]
    assert len(target.instructions) == before - 1, "specific frame-leak mutation must apply"
    with pytest.raises(AssertionError, match="return reached with active root frames"):
        RootSlotContract(copied, module.globals_).assert_frame_exits({"base"})
