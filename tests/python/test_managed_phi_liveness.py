"""PHI edge liveness agrees with the independent set-based implementation."""
from __future__ import annotations

import pytest

from pcc.backend import self_backend_precise_stackmaps as stackmaps
from pcc.backend.self_backend_parse import parse_self_backend_module
from pcc.backend.self_backend_kernel import get_indexed_function_kernel
from pcc.backend.self_backend_ir import PARSED_INSTRUCTION_KIND_CALL


def _phi_module(fanin, loop):
    cases = '\n'.join(f'    i32 {i}, label %case{i}' for i in range(1, fanin))
    blocks = ['entry:\n  switch i32 %which, label %case0 [\n' + cases + '\n  ]']
    for i in range(fanin):
        blocks.append(f'case{i}:\n  %p{i} = call ptr @allocate()\n  call void @tick()\n  br label %join')
    incoming = [f'[ %p{i}, %case{i} ]' for i in range(fanin)]
    if loop:
        incoming.append('[ %chosen, %repeat ]')
    blocks.append('join:\n  %chosen = phi ptr ' + ', '.join(incoming)
                  + '\n  call void @tick()\n'
                  + ('  br i1 %again, label %repeat, label %exit' if loop else '  br label %exit'))
    if loop:
        blocks.append('repeat:\n  call void @tick()\n  br label %join')
    blocks.append('exit:\n  call void @tick()\n  ret ptr %chosen')
    return 'target triple = "arm64-apple-darwin23.6.0"\ndeclare ptr @allocate()\ndeclare void @tick()\ndefine ptr @test(i32 %which, i1 %again) {\n' + '\n'.join(blocks) + '\n}\n'


def _call_states(kernel, states):
    result = []
    for block_id in range(len(kernel.block_names)):
        fact = kernel.block_fact(block_id)
        for position in range(fact.second):
            metadata = kernel.instruction_metadata_by_id(fact.first + position)
            if metadata.first != PARSED_INSTRUCTION_KIND_CALL:
                continue
            row = states.record(kernel.call_span(metadata.second).fourth)
            values = []
            if row.first:
                values.append(row.second)
            if row.first == 2:
                values.append(row.third)
            elif row.first > 2:
                start = -row.third - 2
                values.extend(states.overflow_ids.get_unchecked(start + i) for i in range(row.first - 1))
            result.append((kernel.block_names[block_id], position, tuple(kernel.value_name(v) for v in values)))
    return result


@pytest.mark.parametrize('fanin', [2, 9, 65])
@pytest.mark.parametrize('loop', [False, True])
@pytest.mark.parametrize('selection', ['all', 'alternating', 'none'])
def test_phi_liveness_matches_set_oracle_without_repeated_fixpoint_scans(monkeypatch, tmp_path, fanin, loop, selection):
    source = _phi_module(fanin, loop)
    expected_func = parse_self_backend_module(source).functions[0]
    expected_kernel = get_indexed_function_kernel(expected_func)
    names = ['p' + str(i) for i in range(fanin)] + ['chosen']
    if selection == 'alternating':
        names = names[::2]
    elif selection == 'none':
        names = []
    expected_ids = frozenset(expected_kernel.value_id(name) for name in names)
    assert all(value_id >= 0 for value_id in expected_ids)
    expected = stackmaps._managed_live_after(expected_func, expected_ids)
    wanted = _call_states(expected_kernel, expected)
    func = parse_self_backend_module(source).functions[0]
    kernel = get_indexed_function_kernel(func)
    origins = stackmaps.PackedManagedOrigins(len(kernel.value_names))
    for name in names:
        origins.set_state(kernel.value_id(name), stackmaps._ORIGIN_MANAGED)
    calls = []
    original = type(kernel).phi_incoming

    def counted(self, index):
        calls.append(index)
        return original(self, index)

    monkeypatch.setattr(type(kernel), 'phi_incoming', counted)
    # Existing real parser/kernel oracle: observe the actual host arenas at
    # result construction, after the final matrix consumer has returned.
    original_arena = stackmaps.CompilerIntArena
    original_packed = stackmaps.PackedManagedLiveness
    created = []
    checkpoints = []

    class TrackedArena(original_arena):
        def __init__(self, initial_capacity=8):
            super().__init__(initial_capacity)
            self.close_calls = 0
            self.releases = 0
            created.append(self)

        def close(self):
            self.close_calls += 1
            if not self._closed:
                self.releases += 1
            super().close()

    class CheckedPacked(original_packed):
        def __init__(self):
            # tracked_values/index, uses, definitions, live_out, scratch, live_in.
            assert len(created) == 7
            checkpoints.append(tuple(arena._closed for arena in created))
            assert checkpoints[-1] == (False, False, True, True, True, False, False)
            super().__init__()

    monkeypatch.setattr(stackmaps, "CompilerIntArena", TrackedArena)
    monkeypatch.setattr(stackmaps, "PackedManagedLiveness", CheckedPacked)
    actual = stackmaps._native_managed_liveness(func, origins, kernel)
    assert len(checkpoints) == 1
    assert all(created[index].close_calls == 2 and created[index].releases == 1
               for index in (2, 3, 4))
    assert _call_states(kernel, actual) == wanted
    incoming_count = fanin + int(loop)
    # One seed scan, plus the existing final backwards scan per predecessor.
    # Fixed-point iterations must not re-scan every incoming PHI edge.
    assert len(calls) <= incoming_count * (incoming_count + 1)
    actual.close()
    expected.close()
    origins.close()
    kernel.close_native_tables()
    expected_kernel.close_native_tables()

    if fanin == 65 and loop and selection == 'all':
        # Preserve the existing 18 oracle cases and their seven-arena checks.
        # One existing CI call additionally observes truly ordinary kernels;
        # the allocation spies above intentionally force the fallback gate.
        monkeypatch.setattr(type(kernel), 'phi_incoming', original)
        monkeypatch.setattr(stackmaps, "CompilerIntArena", original_arena)
        monkeypatch.setattr(stackmaps, "PackedManagedLiveness", original_packed)
        from tests.managed_liveness_dirty_rows_ci_reference import record_real_kernel_overhead
        record_real_kernel_overhead(tmp_path, source)
