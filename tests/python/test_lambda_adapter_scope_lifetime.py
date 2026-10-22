"""Exact adapter slot identity and balanced emitted lexical lifetimes."""
from collections import deque
import re

import pytest

from pcc.frontends.python.codegen.call_object_lowering import CallObjectLoweringMixin
from pcc.frontends.python.codegen.errors import L1CodegenError
from pcc.frontends.python.codegen.lambda_helpers_lowering import LambdaHelperLoweringMixin
from pcc.frontends.python.py_ast import DynType, Name, SourceSpan
from tests.python.test_lambda_constructor_roots import (
    CASES,
    emit,
    immediate_store,
)


class ScopeHost:
    _slot_call_root_record = CallObjectLoweringMixin._slot_call_root_record

    def __init__(self):
        self.current_function = object()
        self.source, self.output = object(), object()
        self.expr = Name(span=SourceSpan('scope.py', 1, 1, 1, 2), ty=DynType(name='dyn'), ident='kept')
        self.env = {'kept': (self.source, object(), self.expr.ty)}
        self._lambda_adapter_root_scope = (self.current_function, (('kept', self.source),))
        self._slot_call_root_records = [(self.source, None, True)]
        self._try_err_block = None
        self._cpy_operand_cleanup_block = 'original'
        self.copies = []

    def _new_slot_call_root(self, label):
        self._slot_call_root_records.append((self.output, None, True))
        return self.output

    def _current_try_err_block(self):
        return self._try_err_block

    def _ensure_fn_err_exit(self):
        return 'error'

    def _slot_call_cleanup_block(self, roots, target):
        return ('cleanup', roots, target)

    def _slot_call_copy_source(self, destination, source, borrowed, span):
        self.copies.append((destination, source, borrowed, span))


@pytest.mark.parametrize('fault', ['inactive', 'other-function', 'rebound-slot', 'other-name'])
def test_adapter_scope_does_not_admit_unproven_names(fault):
    host = ScopeHost()
    if fault == 'inactive':
        host._lambda_adapter_root_scope = None
    elif fault == 'other-function':
        host.current_function = object()
    elif fault == 'rebound-slot':
        host.env['kept'] = (object(), object(), host.expr.ty)
    else:
        host._lambda_adapter_root_scope = (host.current_function, (('other', host.source),))
    assert LambdaHelperLoweringMixin._emit_lambda_adapter_name_root(host, host.expr, 'probe') is None
    assert host.copies == []


def test_adapter_scope_still_rejects_an_unregistered_physical_slot():
    host = ScopeHost()
    host._slot_call_root_records = []
    with pytest.raises(L1CodegenError, match='not registered'):
        LambdaHelperLoweringMixin._emit_lambda_adapter_name_root(host, host.expr, 'probe')


def test_adapter_scope_copies_one_independent_owner_and_restores_cleanup():
    host = ScopeHost()
    result = LambdaHelperLoweringMixin._emit_lambda_adapter_name_root(host, host.expr, 'probe')
    assert result is host.output
    assert host.copies == [(host.output, host.source, False, host.expr.span)]
    assert host._try_err_block is None
    assert host._cpy_operand_cleanup_block == 'original'


def check_frames(body):
    aliases = dict(re.findall(r'(%[-\w.]+) = bitcast ptr (%[-\w.]+) to ptr', body))

    def physical(value):
        seen = set()
        while value in aliases:
            assert value not in seen
            seen.add(value)
            value = aliases[value]
        return value

    blocks = {}
    current = None
    for line in body.splitlines()[1:-1]:
        label = re.match(r'^([-\w.$]+):', line)
        if label:
            current = label.group(1)
            blocks[current] = []
        elif current is not None:
            blocks[current].append(line.strip())
    pending = deque([(next(iter(blocks)), ())])
    seen = set()
    returns = 0
    while pending:
        block, stack = pending.popleft()
        state = (block, stack)
        if state in seen:
            continue
        seen.add(state)
        stack = list(stack)
        successors = []
        for line in blocks[block]:
            entered = re.search(r'@pcc_gc_frame_enter_lifo\(ptr [^,]+, ptr (%[-\w.]+)\)', line)
            if entered:
                slot = physical(entered.group(1))
                assert slot not in stack, (block, slot, stack)
                stack.append(slot)
            copied = re.search(r'@pcc_gc_root_(?:copy_lease|move)\(ptr (%[-\w.]+), ptr (%[-\w.]+)\)', line)
            if copied:
                assert all(physical(slot) in stack for slot in copied.groups()), (block, line, stack)
            left = re.search(r'@pcc_gc_frame_leave_lifo\(ptr (%[-\w.]+)\)', line)
            if left:
                slot = physical(left.group(1))
                assert stack and stack[-1] == slot, (block, slot, stack)
                stack.pop()
            if line.startswith('ret '):
                assert not stack, (block, stack)
                returns += 1
            if line.startswith('br '):
                successors.extend(re.findall(r'label %([-\w.$]+)', line))
        pending.extend((label, tuple(stack)) for label in successors)
    assert returns >= 2, 'normal and error returns must both be inspected'
    return returns


@pytest.mark.parametrize('case', ['identity', 'capture', 'indexed_default', 'late_default_error', 'direct_return', 'nested_lambda'])
def test_actual_adapter_gets_publish_before_calls_and_frames_balance_on_every_exit(case):
    text = emit(CASES[case])
    adapters = [body for body in re.findall(r'^define [^\n]*\{\n.*?^}', text, re.M | re.S)
                if re.search(r'@user_lambda_owned__native_lambda_\d+\(', body.splitlines()[0])]
    assert adapters, case
    for body in adapters:
        assert immediate_store(body, 'py_tuple_get') >= 1
        check_frames(body)
