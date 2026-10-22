"""A guarded count result has one owner across either receiver cleanup path."""
import re

import pytest

from tests.python.test_lambda_adapter_scope_lifetime import check_frames
from tests.python.test_lambda_constructor_roots import emit


CONTEXTS = {
    'argument': '    return take(value=value.count(item))\n',
    'default': '    def kept(result=value.count(item)):\n        return result\n    return kept\n',
    'tuple': '    return (value.count(item),)\n',
    'subtraction': '    return value.count("(") - value.count(")")\n',
    'lambda': '    return lambda value: value.count(item)\n',
    'return': '    return value.count(item)\n',
    'temporary': '    return (value,)[0].count((item,)[0])\n',
}


@pytest.mark.parametrize('context', CONTEXTS)
def test_guarded_count_publishes_both_results_before_cleanup(context):
    text = emit('def take(*, value, **extras):\n    return value\ndef make(value):\n    return value\ndef probe(value, item):\n' + CONTEXTS[context])
    bodies = re.findall(r'^define[^\n]*\{\n.*?^}', text, re.M | re.S)
    selected = [body for body in bodies if re.search(r'\bcall[^\n]*@py_list_count\(', body)]
    assert selected
    for body in selected:
        original_body = body
        aliases = dict(re.findall(r'(%[-\w.]+) = bitcast ptr (%[-\w.]+) to ptr', body))
        def physical(match):
            value = match.group(0)
            seen = set()
            while value in aliases:
                assert value not in seen
                seen.add(value)
                value = aliases[value]
            return value
        body = re.sub(r'%[-\w.]+', physical, body)
        assert 'dyn.list.result' not in body
        counts = list(re.finditer(r'(%[-\w.]+) = call i64[^\n]*@py_list_count\(', body))
        for count in counts:
            box = re.search(r'(%[-\w.]+) = call ptr[^\n]*@py_int_from_i64\(i64 ' + re.escape(count.group(1)) + r'\)[^\n]*\n([^\n]+)', body[count.end():])
            assert box is not None
            stored = re.search(r'store ptr ' + re.escape(box.group(1)) + r', ptr (%[-\w.]+)', box.group(2))
            assert stored is not None
            output = stored.group(1)
            assert re.search(r'@py_obj_call_slots\(ptr [^,]+, ptr [^,]+, ptr [^,]+, ptr ' + re.escape(output) + r'\)', body)
        for lookup in re.finditer(r'(%[-\w.]+) = call ptr[^\n]*@py_obj_getattr\([^\n]*\n([^\n]+)', body):
            assert 'store ptr ' + lookup.group(1) in lookup.group(2)
        assert re.search(r'@pcc_gc_foreign_lease_acquire\(ptr %dyn.count.receiver', body)
        # The common root is reloaded only after receiver retirement; a pointer
        # PHI from the two branches would cross that fallible cleanup interval.
        assert re.search(r'@pcc_gc_store_root\(ptr %dyn.count.receiver[^\n]*ptr null\)', body)
        if '@user_lambda_owned__native_lambda_' in body.splitlines()[0]:
            check_frames(original_body)


def test_count_original_string_semantics_reference():
    calls = []

    class Custom:
        @property
        def count(self):
            calls.append('lookup')
            def bound(value):
                calls.append(('invoke', value))
                return 19
            return bound

    def receiver():
        calls.append('receiver')
        return Custom()

    def argument():
        calls.append('argument')
        return 'x'

    assert receiver().count(argument()) == 19
    assert calls == ['receiver', 'lookup', 'argument', ('invoke', 'x')]
    assert '(()'.count('(') - '(()'.count(')') == 1
    assert [1, 2, 1].count(1) == 2
    with pytest.raises(TypeError):
        'abc'.count(None)
