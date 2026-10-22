"""Finite host differential controls, not native RSS or Stage1 evidence."""
import random
from unittest.mock import patch
import pytest
from tests.python import test_managed_liveness_dirty_rows as c
from tests.managed_liveness_plane_reference import function


def paired(blocks, count, tracked=None, variant=None):
    with patch.object(c, '_function', lambda original=False: function()):
        before = c._run(blocks, count, tracked_count=tracked, variant=variant)
    after = c._run(blocks, count, tracked_count=tracked, variant=variant)
    assert before.result is not None, before.outcome
    assert after.result is not None, after.outcome
    assert after.outcome == before.outcome
    for name in ('definitions', 'live_out'):
        assert after.events.closed_values[name] == before.events.closed_values[name]
    if 'live_in' not in after.events.labels:
        assert after.events.closed_values['uses'] == before.events.closed_values['live_in']
        assert sum(name == 'uses' for name, _, _ in after.events.closes) == 1
        assert after.events.checkpoints[0]['uses'][0] is False
        assert before.events.checkpoints[0]['uses'][0] is True
        words = ((count if tracked is None else tracked) + 29) // 30
        assert len(before.events.closed_values['uses']) == len(blocks) * words
        assert set(before.events.labels) - set(after.events.labels) == {'live_in'}
    else:
        assert after.events.closed_values['live_in'] == before.events.closed_values['live_in']
    return before, after


@pytest.mark.parametrize('shape', c.SHAPES)
@pytest.mark.parametrize('count', [0, 1, 31, 65])
def test_complete_states(shape, count):
    total = max(3, count) if shape.startswith('phi') else count
    paired(c._shape(shape, total), total, count)


@pytest.mark.parametrize('seed', range(24))
def test_cyclic_kill_error_cfg(seed):
    rng = random.Random(seed)
    blocks = []
    for b in range(7):
        ins = [(-1, [], True, 0)]
        for _ in range(3):
            ins.append((rng.randrange(35), rng.sample(range(35), 3), False, 0))
        ins.append((-1, rng.sample(range(35), 4), True, 0))
        blocks.append(dict(instructions=ins, edges=rng.sample(range(7), rng.randrange(4)),
                           errors=[(1, rng.randrange(7))] if b % 2 else []))
    paired(blocks, 35)


@pytest.mark.parametrize('variant', ['kernel-subclass', 'arena-subclass', 'list-subclass',
    'mutable-callback', 'int-subclass', 'bool-payload', 'length-subclass',
    'address-subclass', 'capacity-subclass'])
def test_preserved_fallback(variant):
    before, after = paired(c._shape('reverse', 1), 1, variant=c._fallback(variant))
    assert before.events.cfg == after.events.cfg
    assert before.events.rows == after.events.rows


def test_kill_only():
    paired([dict(instructions=[(0, [], False, 0)], edges=[1]),
            dict(instructions=[(-1, [], True, 0), (-1, [0], False, 0)], edges=[0])], 1)
