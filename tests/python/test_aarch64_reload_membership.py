"""Host equivalence for function-local reload membership indexes.

The controls restore the prior list operations, not allocator decisions or
stack-map planning. Native execution remains a separate qualification.
"""

import ast
import inspect
from types import SimpleNamespace
import traceback
import weakref

import pytest

from pcc.backend import self_backend_aarch64_darwin_regalloc as regalloc
from pcc.backend.self_backend_kernel import get_indexed_function_kernel
from pcc.backend.self_backend_precise_stackmaps import PackedPlannedSafepoints
from tests.aarch64_regalloc_scan_reference import allocation_state, select_mode
from tests.c.test_self_backend_aarch64_regalloc import (
    _SCAN_SHAPES,
    _scan_reuse_function,
)


def _list_note(func, plan):
    """The pre-index first-seen algorithm, including the packed projection."""
    offsets = []
    records = getattr(plan, "packed_records", None)
    if records is not None:
        index = 0
        count = len(records.reload_scalars) // 3
        while index < count:
            packed = records.reload_scalars.get3_unchecked(index)
            offset = -packed.second
            if offset not in offsets:
                offsets.append(offset)
            index += 1
    else:
        for record in getattr(plan, "records", ()):
            for planned in record.reloads:
                offset = -planned.destination_offset
                if offset not in offsets:
                    offsets.append(offset)
    func.aarch64_reload_slot_offsets = offsets


def _list_allocator():
    """Undo exactly this change, keeping all real analysis and allocation.

    This is distinct from old_scan_allocator, which restores older metadata
    scans but would otherwise inherit the new membership index.
    """
    tree = ast.parse(inspect.getsource(regalloc.allocate_aarch64_block_registers))

    class Restore(ast.NodeTransformer):
        counts = [0, 0, 0]

        def visit_AnnAssign(self, node):
            if isinstance(node.target, ast.Name) and node.target.id == "reload_offsets":
                assert ast.unparse(node.annotation) == "set[int]"
                assert ast.unparse(node.value) == "set()"
                self.counts[0] += 1
                return ast.parse(
                    "reload_offsets: list[int] = "
                    "func.aarch64_reload_slot_offsets if callee_saved else []"
                ).body[0]
            return self.generic_visit(node)

        def visit_If(self, node):
            if (ast.unparse(node.test) == "callee_saved" and len(node.body) == 1
                    and isinstance(node.body[0], ast.For)
                    and ast.unparse(node.body[0].target) == "reload_offset"):
                assert ast.unparse(node.body[0].iter) == "func.aarch64_reload_slot_offsets"
                assert [ast.unparse(item) for item in node.body[0].body] == [
                    "reload_offsets.add(reload_offset)"
                ]
                assert not node.orelse
                self.counts[1] += 1
                return None
            return self.generic_visit(node)

        def visit_Expr(self, node):
            if ast.unparse(node) == "reload_offsets.clear()":
                self.counts[2] += 1
                return None
            return self.generic_visit(node)

    restore = Restore()
    tree = restore.visit(tree)
    assert restore.counts == [1, 1, 1]
    namespace = dict(vars(regalloc))
    exec(compile(ast.fix_missing_locations(tree), "<original-reload-membership>", "exec"), namespace)
    return namespace["allocate_aarch64_block_registers"]


def _plan(offsets, packed):
    if packed:
        records = PackedPlannedSafepoints()
        for index, offset in enumerate(offsets):
            records.reload_scalars.append3(-8, -offset, index % 3 * 8)
        return SimpleNamespace(packed_records=records), records
    records = tuple(SimpleNamespace(reloads=(
        SimpleNamespace(destination_offset=-offset),
    )) for offset in offsets)
    return SimpleNamespace(records=records), None


@pytest.mark.parametrize("packed", (False, True))
@pytest.mark.parametrize("offsets", (
    (), (8,), (32, 8, 32, 16, 8, 24),
    (8, 2**31 - 8, 16, 2**31 - 8, 8),
    (0, -8, 8, 0, -8), (-1, -2, -1),
))
def test_first_seen_reload_projection_matches_list(offsets, packed, monkeypatch):
    plan, owner = _plan(offsets, packed)
    refs = []
    counts = {"contains": 0, "add": 0}

    class MembershipOnlySet(set):
        def __init__(self):
            super().__init__()
            refs.append(weakref.ref(self))

        def __contains__(self, value):
            counts["contains"] += 1
            return super().__contains__(value)

        def add(self, value):
            counts["add"] += 1
            return super().add(value)

        def __iter__(self):
            raise AssertionError("membership must never determine first-seen order")

    try:
        before = tuple(owner.reload_scalars.diagnostic_values()) if owner else None
        expected, actual = SimpleNamespace(), SimpleNamespace()
        _list_note(expected, plan)
        monkeypatch.setattr(regalloc, "set", MembershipOnlySet, raising=False)
        regalloc.note_aarch64_reload_destinations(actual, plan)
        assert type(actual.aarch64_reload_slot_offsets) is list
        assert actual.aarch64_reload_slot_offsets == expected.aarch64_reload_slot_offsets
        assert counts == {"contains": len(offsets), "add": len(set(offsets))}
        assert len(refs) == 1 and refs[0]() is None
        if owner:
            assert tuple(owner.reload_scalars.diagnostic_values()) == before
    finally:
        if owner:
            owner.close()


@pytest.mark.parametrize("shape", _SCAN_SHAPES)
@pytest.mark.parametrize("mode", ("default", "function", "local"))
def test_reload_index_preserves_complete_allocator_state(monkeypatch, shape, mode):
    select_mode(monkeypatch, mode)
    old, new = [_scan_reuse_function(monkeypatch, shape) for _ in range(2)]
    before = list(new.aarch64_reload_slot_offsets)
    _list_allocator()(old)
    regalloc.allocate_aarch64_block_registers(new)
    assert allocation_state(new) == allocation_state(old)
    assert new.aarch64_reload_slot_offsets == before


@pytest.mark.parametrize("shape", ("safe", "loop", "call", "reload"))
def test_reload_index_excludes_argument_phi_and_ssa_slots(monkeypatch, shape):
    select_mode(monkeypatch, "default")
    old, new = [_scan_reuse_function(monkeypatch, shape) for _ in range(2)]
    kernel = get_indexed_function_kernel(old)
    offsets = [kernel.value_slot_offset(value_id)
               for value_id in range(len(kernel.value_names))
               if kernel.value_slot_offset(value_id) >= 0]
    assert offsets
    offsets = offsets[::-1] + offsets
    old.aarch64_reload_slot_offsets = list(offsets)
    new.aarch64_reload_slot_offsets = list(offsets)
    _list_allocator()(old)
    regalloc.allocate_aarch64_block_registers(new)
    assert allocation_state(new) == allocation_state(old)
    assert new.aarch64_reload_slot_offsets == offsets
    kernel = get_indexed_function_kernel(new)
    for value_id in range(len(kernel.value_names)):
        if kernel.value_slot_offset(value_id) in offsets:
            assert kernel.value_register(value_id) is None


def test_reload_index_retires_before_alias_groups_and_is_not_cached(monkeypatch):
    select_mode(monkeypatch, "default")
    func = _scan_reuse_function(monkeypatch, "reload")
    source_offsets = list(func.aarch64_reload_slot_offsets)
    assert source_offsets
    refs = []
    arguments, groups = regalloc._append_argument_intervals, regalloc._merge_alias_groups

    def observe_arguments(*args):
        index = args[6]
        assert type(index) is set
        assert index == set(source_offsets)
        refs.append(weakref.ref(index))
        return arguments(*args)

    def observe_groups(*args):
        assert refs[-1]() is not None and len(refs[-1]()) == 0
        return groups(*args)

    monkeypatch.setattr(regalloc, "_append_argument_intervals", observe_arguments)
    monkeypatch.setattr(regalloc, "_merge_alias_groups", observe_groups)
    regalloc.allocate_aarch64_block_registers(func)
    assert refs[0]() is None
    assert func.aarch64_reload_slot_offsets == source_offsets
    # Reusing the function must read the current list, never a stale index.
    func.aarch64_reload_slot_offsets = []
    source_offsets.clear()
    regalloc.allocate_aarch64_block_registers(func)
    assert len(refs) == 2 and refs[1]() is None


def test_retained_error_has_only_function_local_index_lifetime(monkeypatch):
    select_mode(monkeypatch, "default")
    func = _scan_reuse_function(monkeypatch, "reload")
    offsets = list(func.aarch64_reload_slot_offsets)

    def fail(_kernel):
        raise RuntimeError("injected facts failure")

    monkeypatch.setattr(regalloc, "_function_level_facts", fail)
    with pytest.raises(RuntimeError, match="injected facts failure") as caught:
        regalloc.allocate_aarch64_block_registers(func)
    tb = caught.value.__traceback__
    while tb.tb_frame.f_code is not regalloc.allocate_aarch64_block_registers.__code__:
        tb = tb.tb_next
    index_ref = weakref.ref(tb.tb_frame.f_locals["reload_offsets"])
    assert index_ref() == set(offsets)
    assert func.aarch64_reload_slot_offsets == offsets
    # This explicitly preserves the existing traceback contract: retaining an
    # error keeps the local set; clearing its frame releases the added owner.
    traceback.clear_frames(caught.value.__traceback__)
    assert index_ref() is None


@pytest.mark.parametrize("shape", ("calls-phi", "managed-derived"))
@pytest.mark.parametrize("route", ("assembly", "structured-pco"))
@pytest.mark.parametrize("mode", ("default", "function", "local"))
def test_reload_index_preserves_emitted_bytes_and_frames(monkeypatch, shape, route, mode):
    from pcc.backend import self_backend_aarch64_darwin as backend
    from pcc.backend import self_backend_aarch64_darwin_prologue as prologue
    from pcc.backend.native_object import encode_native_object_from_sections
    from pcc.backend.self_backend_parse import parse_self_backend_module
    from tests.c.test_self_backend_aarch64_callee_saved import IR
    from tests.python.test_precise_stackmap_abi import _stale_managed_ssa_ir

    select_mode(monkeypatch, mode)
    source = IR if shape == "calls-phi" else _stale_managed_ssa_ir("arm64-apple-darwin")
    results = []
    for note, allocator in (
        (_list_note, _list_allocator()),
        (regalloc.note_aarch64_reload_destinations, regalloc.allocate_aarch64_block_registers),
    ):
        states = {}

        def allocate(func):
            allocator(func)
            states[func.name] = allocation_state(func)

        with monkeypatch.context() as patch:
            patch.setattr(backend, "note_aarch64_reload_destinations", note)
            patch.setattr(prologue, "allocate_aarch64_block_registers", allocate)
            if route == "assembly":
                output = backend.emit_aarch64_darwin_asm(source, optimize=False)
            else:
                transport = backend.emit_aarch64_darwin_indexed_transport(
                    parse_self_backend_module(source), optimize=False, structured_instructions=True,
                )
                sections, undefined = transport.assemble_sections()
                output = encode_native_object_from_sections(sections, undefined=undefined)
        results.append((output, states))
    assert results[0] == results[1]
    if shape == "managed-derived":
        assert results[1][1]["refresh"]["reload_offsets"]
