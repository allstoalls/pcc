"""Alternative binary returns share owners; pending nested returns do not."""
from __future__ import annotations

import re

import pytest

from ir_pointer_aliases import (
    canonical_pointer,
    function_bodies,
    pointer_bitcast_aliases,
)
from pcc.frontends.python.codegen.layer1 import (
    L1CodeGen,
)
from pcc.frontends.python.py_lift import (
    parse_and_lift,
)
from pcc.frontends.python.type_infer import (
    infer_module,
)
from tests.python.test_numeric_return_owners import (
    check_return_cfg,
)


class ObservedReturns(L1CodeGen):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.return_slots = {}

    def _emit_owned_return_through_finally(self, value, stmt, source_slot=None):
        super()._emit_owned_return_through_finally(value, stmt, source_slot)
        for name, entry in self.env.items():
            if name.startswith('.pcc.return.cleanup.owner.'):
                slot = entry[0]
                flag = self._owned_local_flag_for(name, slot)
                self.return_slots[str(slot)] = str(flag)


def emit(source):
    module = infer_module(parse_and_lift(source, 'growth.py', 'growth'))
    codegen = ObservedReturns(module, ir_scaffold_mode='on')
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    text = str(codegen.generate(module))
    body = next(body for name, body in function_bodies(text) if name == 'user_growth_probe')
    return body, codegen.return_slots


def metrics(body):
    blocks = re.split(r'^([\w.$]+):\n', body, flags=re.M)
    instructions = sum(len([line for line in block.splitlines() if line.strip() and line.strip() != '}']) for block in blocks[2::2])
    return {
        'permanent_roots': body.count('@pcc_gc_frame_enter('),
        'frame_leaves': body.count('@pcc_gc_frame_leave('),
        'root_stores': body.count('@pcc_gc_store_root('),
        'blocks': (len(blocks) - 1) // 2,
        'instructions': instructions,
    }


def assert_each_acquisition_sets_its_own_cleanup_flag(body, slots):
    aliases = pointer_bitcast_aliases(body)
    canonical = lambda value: canonical_pointer(value, aliases)
    blocks = re.split(r'^[\w.$]+:\n', body, flags=re.M)[1:]
    acquisitions = 0
    for block in blocks:
        latest = {}
        for line in block.splitlines():
            stored = re.search(r'store i1 ([01]), ptr (%[^ ,]+)', line)
            if stored:
                latest[canonical(stored[2])] = stored[1]
            move = re.search(r'@pcc_gc_root_move\(ptr (%[^ ,]+), ptr (%[^ ,)]+)\)', line)
            if move:
                target = canonical(move[1])
                if target in slots:
                    assert latest.get(canonical(slots[target])) == '1', (target, block)
                    acquisitions += 1
    assert acquisitions
    return acquisitions


@pytest.mark.parametrize('shape', ('flat', 'nested-finally'))
def test_return_owner_and_cleanup_structure_grows_linearly(shape):
    rows = []
    for count in (10, 20, 40):
        body = ''.join('    if selector == ' + str(index) + ':\n        return left + right\n' for index in range(count - 1))
        body += '    return left + right\n'
        if shape == 'nested-finally':
            body = '    try:\n        try:\n' + ''.join('        ' + line + '\n' for line in body.splitlines()) + '        finally:\n            cleanup()\n    finally:\n        cleanup()\n'
        source = 'def cleanup():\n    pass\ndef probe(selector: int, left, right):\n' + body
        text, slots = emit(source)
        assert len(slots) == 1
        assert assert_each_acquisition_sets_its_own_cleanup_flag(text, slots) == count
        check_return_cfg(text)
        rows.append(metrics(text))
    assert rows[0]['permanent_roots'] == rows[1]['permanent_roots'] == rows[2]['permanent_roots']
    for name in ('frame_leaves', 'root_stores', 'blocks', 'instructions'):
        assert rows[2][name] - rows[1][name] == 2 * (rows[1][name] - rows[0][name]), (name, rows)


def test_overriding_return_uses_an_independent_pending_depth_owner():
    text, slots = emit('''def probe(left, right, replacement):
    try:
        return left % right
    finally:
        return replacement // right
''')
    assert len(slots) == 2
    assert len(set(slots.values())) == 2
    assert assert_each_acquisition_sets_its_own_cleanup_flag(text, slots) >= 2
    check_return_cfg(text)
