"""Typed numeric results own their return, unwind and cancellation paths."""
from __future__ import annotations

import inspect
import re

import pytest

from ir_pointer_aliases import (
    canonical_pointer,
    function_bodies,
    pointer_bitcast_aliases,
)
from pcc.frontends.python.codegen._l1_codegen_static_methods import (
    L1_CODEGEN_STATIC_METHODS,
)
from pcc.frontends.python.codegen.layer1 import (
    L1CodeGen,
)
from tests.python.test_shared_call_binding import (
    _emit,
)


RUNTIME = {'%': 'py_obj_mod', '/': 'py_obj_truediv', '//': 'py_obj_floordiv'}


def return_body(text):
    return next(body for name, body in function_bodies(text) if name == 'user_binding_probe')


def check_return_cfg(body):
    aliases = pointer_bitcast_aliases(body)
    canonical = lambda value: canonical_pointer(value, aliases)
    blocks = {}
    current = None
    for line in body.splitlines()[1:]:
        label = re.match(r'^([\w.$]+):', line)
        if label:
            current = label[1]
            blocks[current] = []
        elif current is not None:
            blocks[current].append(line.strip())
    assert blocks
    for name, lines in blocks.items():
        if not name.startswith(('finally.return.error.', 'return.cleanup.error.')):
            continue
        drops = []
        swaps = []
        clears = []
        for index, line in enumerate(lines):
            dropped = re.search(r'@pcc_gc_store_root\(ptr (%[^ ,]+), ptr null\)', line)
            if dropped and 'return.cleanup.slot' in canonical(dropped[1]):
                drops.append(index)
            if '@py_tls_exc_swap_slot(' in line:
                swaps.append(index)
            if '@py_clear_exception(' in line:
                clears.append(index)
        if drops:
            assert len(swaps) == 2 and len(clears) == 1, (name, 'unprotected pending exception')
            assert swaps[0] < min(drops) <= max(drops) < clears[0] < swaps[1]
    pending = [(next(iter(blocks)), (), 0)]
    visited = set()
    returns = 0
    handoffs = 0
    while pending:
        block, incoming, lock_depth = pending.pop()
        state = block, incoming, lock_depth
        if state in visited:
            continue
        visited.add(state)
        assert len(visited) <= len(blocks) * 12
        stack = list(incoming)
        successors = []
        lines = blocks[block]
        for index, line in enumerate(lines):
            enter = re.search(r'@pcc_gc_frame_enter_lifo\(ptr [^,]+, ptr (%[\w.$]+)\)', line)
            if enter:
                root = canonical(enter[1])
                assert root not in stack, ('double registration', block, root)
                stack.append(root)
            leave = re.search(r'@pcc_gc_frame_leave_lifo\(ptr (%[\w.$]+)\)', line)
            if leave:
                root = canonical(leave[1])
                assert stack and stack[-1] == root, ('non-LIFO return cleanup', block, root, stack)
                stack.pop()
            if '@pcc_py_gc_minor_graph_lock(' in line:
                lock_depth += 1
            if '@pcc_py_gc_minor_graph_unlock(' in line:
                assert lock_depth > 0
                lock_depth -= 1
            take = re.match(r'(%[^ ]+) = call ptr (?:\([^\n]*?\) )?@pcc_gc_take_pinned_slot\(', line)
            if take:
                # Function-root retro-patches and recursion accounting must
                # precede the terminal unpin/owner handoff on every return.
                assert lines[index + 1] == 'ret ptr ' + take[1], lines[index:]
                handoffs += 1
            if line.startswith('br '):
                successors = re.findall(r'label %([\w.$]+)', line)
            if line.startswith('ret '):
                assert not stack, ('live temporary frames at return', block, stack)
                assert lock_depth == 0, ('graph lock held at return', block)
                returns += 1
        for successor in successors:
            pending.append((successor, tuple(stack), lock_depth))
    assert returns and handoffs
    return handoffs


@pytest.mark.parametrize('operator', ('%', '/', '//'))
@pytest.mark.parametrize('shape', ('plain', 'finally', 'finally-error', 'finally-override', 'late-root', 'nested-finally'))
def test_typed_result_owner_survives_return_cleanup_and_finally(operator, shape, tmp_path):
    prefix = 'def cleanup():\n    pass\ndef fail():\n    raise KeyError("cleanup")\n'
    expression = 'left ' + operator + ' right'
    if shape == 'plain':
        body = '    return ' + expression + '\n'
    elif shape == 'late-root':
        body = '    if left:\n        return ' + expression + '\n    later = [left]\n    return later\n'
    elif shape == 'nested-finally':
        body = '    try:\n        try:\n            return ' + expression + '\n        finally:\n            cleanup()\n    finally:\n        cleanup()\n'
    else:
        final = {'finally': 'cleanup()', 'finally-error': 'fail()', 'finally-override': 'return replacement'}[shape]
        body = '    try:\n        return ' + expression + '\n    finally:\n        ' + final + '\n'
    text = _emit(prefix + 'def probe(left: int, right: int, replacement=None) -> object:\n' + body)
    function = return_body(text)
    producer = re.search(r'(%[^ ]+) = call ptr (?:\([^\n]*?\) )?@' + RUNTIME[operator] + r'\([^\n]+\)\n\s*store ptr \1, ptr (%[^\s,]+)', function)
    (tmp_path / "probe.ll").write_text(function)
    assert producer is not None, tmp_path / "probe.ll"
    aliases = pointer_bitcast_aliases(function)
    output = canonical_pointer(producer[2], aliases)
    moves = [(canonical_pointer(a, aliases), canonical_pointer(b, aliases)) for a, b in re.findall(r'@pcc_gc_root_move\(ptr (%[^ ,]+), ptr (%[^ ,)]+)\)', function)]
    assert any(source == output and 'return.cleanup.slot' in destination for destination, source in moves)
    assert '@py_int_to_i64_lane(' not in function
    assert '@py_float_to_f64(' not in function
    # Other paths can retain their own parameter or conditional-local loads.
    # Trace every retain to that actual load, never waive a second owner of
    # the binary result merely because another return shares the function.
    loads = dict(re.findall(r'(%[^ ]+) = load ptr, ptr (%[^ ,\n]+)', function))
    loads.update(re.findall(r'(%[^ ]+) = call ptr (?:\([^\n]*?\) )?@pcc_gc_load_(?:borrowed_)?ptr\(ptr null, ptr (%[^ ,)]+)\)', function))
    for retained in re.findall(r'@pcc_gc_retain\(ptr (%[^ ,)]+)\)', function):
        assert retained in loads
        assert canonical_pointer(loads[retained], aliases).startswith(('%left.addr.', '%right.addr.', '%replacement.addr.', '%later.addr.'))
    check_return_cfg(function)


@pytest.mark.parametrize('operator', ('%', '//'))
@pytest.mark.parametrize('annotation', ('', ' -> int'))
def test_inferred_and_declared_int_pointer_return_use_the_same_owned_protocol(operator, annotation):
    body = return_body(_emit('def probe(left: int, right: int)' + annotation + ':\n    return left ' + operator + ' right\n'))
    assert re.search(r'call ptr (?:\([^\n]*?\) )?@' + RUNTIME[operator] + r'\(', body)
    assert '@py_int_to_i64_lane(' not in body
    check_return_cfg(body)


@pytest.mark.parametrize('name', ('_emit_owned_return_through_finally', '_emit_cancel_pending_return_roots'))
def test_return_consumer_static_signature_matches_the_actual_implementation(name):
    method = getattr(L1CodeGen, name)
    signature = inspect.signature(method)
    entry = next(entry for entry in L1_CODEGEN_STATIC_METHODS if entry['name'] == method.__name__)
    assert [(parameter.name, parameter.default is not inspect.Parameter.empty) for parameter in signature.parameters.values()] == [(parameter['name'], parameter['has_default']) for parameter in entry['call_sig']]


class CancellationMemory:
    """Execute the real emission helper with reentrant runtime effects."""
    def __init__(self, effect, pending=True):
        self.original = object() if pending else None
        self.pending = self.original
        self.replacement = object()
        self.effect = effect
        self.events = []
        self.frames = []
        self.current_function = object()
        self.loop_stack = []
        self.roots = [{'value': 'first'}, {'value': 'second'}]
        self._return_cleanup_roots = [(self.current_function, root, 0) for root in self.roots]
        self.module = type('Module', (), {'globals': {'py_tls_exc_swap_slot': 'swap'}})()
        self.runtime = {'py_clear_exception': 'clear'}
        self.builder = self

    def _fresh(self, label):
        return label

    def _alloca_in_entry(self, ty, **options):
        assert options.get('init_null')
        return {'value': None}

    def _as_gc_ptr(self, slot):
        return slot

    def _gc_one_slot_frame_map(self):
        return 'owned-map'

    def _emit_current_gc_frame_enter_lifo(self, layout, slot):
        assert layout == 'owned-map' and slot['value'] is None
        self.frames.append(slot)
        self.events.append('register')

    def _emit_gc_frame_leave_lifo_for_slot(self, slot):
        assert self.frames and self.frames[-1] is slot
        assert slot['value'] is None
        self.frames.pop()
        self.events.append('unregister')

    def call(self, callee, arguments):
        if callee == 'swap':
            slot, = arguments
            assert any(root is slot for root in self.frames)
            self.pending, slot['value'] = slot['value'], self.pending
            self.events.append('swap')
        else:
            assert callee == 'clear' and arguments == []
            self.pending = None
            self.events.append('clear')

    def _clear_exception_selection_owner_slot(self, slot):
        value, slot['value'] = slot['value'], None
        if value is None:
            return
        self.events.append(('drop', value))
        if self.effect == 'replace':
            self.pending = self.replacement
        elif self.effect == 'clear':
            self.pending = None


@pytest.mark.parametrize('effect', ('replace', 'clear', 'unchanged'))
@pytest.mark.parametrize('pending', (True, False))
def test_pending_return_disposal_preserves_tls_through_reentrant_cleanup(effect, pending):
    memory = CancellationMemory(effect, pending)
    L1CodeGen._emit_cancel_pending_return_roots(memory)
    assert memory.pending is memory.original
    assert [event for event in memory.events if isinstance(event, tuple)] == [('drop', 'second'), ('drop', 'first')]
    assert not memory.frames and all(root['value'] is None for root in memory.roots)
    assert memory.events[0:2] == ['register', 'swap']
    assert memory.events[-3:] == ['clear', 'swap', 'unregister']


@pytest.mark.parametrize('selection', ('explicit', 'root-base', 'loop', 'empty'))
def test_pending_return_cancellation_selects_only_the_required_owners(selection):
    memory = CancellationMemory('replace')
    arguments = {}
    expected = []
    if selection == 'explicit':
        arguments['only_slot'] = memory.roots[0]
        expected = [('drop', 'first')]
    elif selection == 'root-base':
        arguments['root_base'] = 1
        expected = [('drop', 'second')]
    elif selection == 'loop':
        memory.loop_stack = [None]
        memory._return_cleanup_roots[1] = (memory.current_function, memory.roots[1], 2)
        arguments['loop_exit'] = True
        # An inner loop exit abandons returns begun inside that loop; an
        # outer pending return survives a loop contained in its finally.
        expected = [('drop', 'second')]
    else:
        memory._return_cleanup_roots = []
    L1CodeGen._emit_cancel_pending_return_roots(memory, **arguments)
    assert [event for event in memory.events if isinstance(event, tuple)] == expected
    assert memory.pending is memory.original
    assert not memory.frames
    if selection == 'empty':
        assert not memory.events


@pytest.mark.parametrize('expression,runtime', (
    ('(left // right) << 1', 'py_obj_floordiv'),
    ('(left / right) + right', 'py_obj_truediv'),
    ('(left % right) * right', 'py_obj_mod'),
    ('(left // right) & right', 'py_obj_floordiv'),
))
def test_outer_managed_binary_return_preserves_nested_numeric_producers(expression, runtime):
    body = return_body(_emit('def probe(left: int, right: int) -> object:\n    return ' + expression + '\n'))
    call = re.search(r'(%[^ ]+) = call ptr (?:\([^\n]*?\) )?@' + runtime + r'\([^\n]*\)\n', body)
    assert call is not None
    assert body[call.end():].splitlines()[0].lstrip().startswith('store ptr ' + call[1] + ',')
    assert '@py_int_to_i64_lane(' not in body
    check_return_cfg(body)
