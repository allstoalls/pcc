"""Native string results remain owned across argument and receiver retirement."""
import re

import pytest

from tests.python.test_lambda_adapter_scope_lifetime import check_frames
from tests.python.test_lambda_constructor_roots import emit


CASES = {
    'lower': ('value.lower()', 'py_str_lower'),
    'strip': ('value.strip()', 'py_str_strip'),
    'strip_argument': ('value.strip(make("x"))', 'py_str_strip_chars'),
    'rstrip': ('value.rstrip()', 'py_str_rstrip'),
    'split': ('value.split()', 'py_str_split'),
    'split_argument': ('value.split(make("x"))', 'py_str_split'),
    'split_limit': ('value.split(make("x"), 1)', 'py_str_split_maxsplit'),
    'rsplit_limit': ('value.rsplit(make("x"), 1)', 'py_str_rsplit_maxsplit'),
    'replace': ('value.replace(make("x"), make("y"))', 'py_str_replace'),
    'replace_limit': ('value.replace(make("x"), make("y"), 1)', 'py_str_replace_count'),
    'temporary_receiver': ('make(value).strip(make("x"))', 'py_str_strip_chars'),
    'subscript_consumer': ('value.split("x")[0]', 'py_str_split'),
    'concatenation_consumer': ('"[" + value.strip() + "]"', 'py_str_strip'),
    'fstring_consumer': ('f"{value.lower()}"', 'py_str_lower'),
    'chained_methods': ('value.replace(".", "_").replace("-", "_")', 'py_str_replace'),
    'late_argument_error': ('value.replace(make("x"), fail())', 'py_str_replace'),
}


@pytest.mark.parametrize('case', CASES)
def test_string_result_publishes_before_all_operand_cleanup(case):
    expression, runtime = CASES[case]
    text = emit('''def take(*, value, **extras):
    return value
def make(value):
    return value
def fail():
    raise ValueError('later argument')
def probe(value):
    return take(value=''' + expression + ')\n')
    bodies = re.findall(r'^define[^\n]*\{\n.*?^}', text, re.M | re.S)
    body = next(body for body in bodies if '@user_lambda_owned_probe(' in body.splitlines()[0])
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
    calls = list(re.finditer(r'(%[-\w.]+) = call ptr[^\n]*@' + runtime + r'\([^\n]*\n([^\n]+)', body))
    assert calls, runtime
    for call in calls:
        stored = re.search(r'store ptr ' + re.escape(call.group(1)) + r', ptr (%[-\w.]+)', call.group(2))
        assert stored is not None
        after = body[call.end():]
        # Publication itself leases the output, reloads it for the slot write
        # barrier and releases that lease; the operand leases are released
        # before the consumer reloads the output.
        output = re.escape(stored.group(1))
        released = re.search(r'@pcc_gc_foreign_lease_release\(ptr (?!' + output + r'[,)])', after)
        assert released is not None
        output_reload = re.search(r'%(?!call\.slot\.published)[-\w.]+ = load ptr, ptr ' + output + r'\b', after)
        assert output_reload is not None
        assert released.start() < output_reload.start()
    # An untyped receiver's strip/split family uses its guarded route, whose
    # receiver root is named after the method (strip.receiver, ...).
    assert re.search(r'@pcc_gc_foreign_lease_acquire\(ptr %[\w.]*receiver', body)
    assert re.search(r'@pcc_gc_store_root\(ptr %[\w.]*receiver[^\n]*ptr null\)', body)
    assert 'str.result.current' not in body
    if 'limit' in case:
        assert re.search(r'@py_index_i64_checked_slots\(ptr %str.method.argument', body)


@pytest.mark.parametrize('expression', ['value.strip()', 'value.split("x", 1)', 'value.replace("x", "y", 1)', 'make(value).rstrip()'])
def test_string_result_module_lambda_normal_and_error_frames_balance(expression):
    text = emit('def make(value):\n    return value\ndef probe():\n    return lambda value: ' + expression + '\n')
    body = next(body for body in re.findall(r'^define[^\n]*\{\n.*?^}', text, re.M | re.S)
                if re.search(r'@user_lambda_owned__native_lambda_\d+\(', body.splitlines()[0]))
    check_frames(body)


@pytest.mark.parametrize('expression,runtime', [('" A ".strip()', 'py_str_strip'), ('"a,b".split(",", 1)', 'py_str_split_maxsplit'), ('"aba".replace("a", "x", 1)', 'py_str_replace_count')])
def test_static_string_literals_use_the_same_owner_contract(expression, runtime):
    text = emit('def take(*, value, **extras):\n    return value\ndef probe():\n    return take(value=' + expression + ')\n')
    call = re.search(r'(%[-\w.]+) = call ptr[^\n]*@' + runtime + r'\([^\n]*\n([^\n]+)', text)
    assert call is not None
    assert 'store ptr ' + call.group(1) in call.group(2)


def test_string_argument_order_and_errors_reference():
    order = []
    class Limit:
        def __index__(self):
            order.append('index')
            return 1
    def make(label, value):
        order.append(label)
        return value
    assert make('receiver', 'aba').replace(make('old', 'a'), make('new', 'x'), make('count', Limit())) == 'xba'
    assert order == ['receiver', 'old', 'new', 'count', 'index']
    assert ' a b '.split(None, 1) == ['a', 'b ']
    assert 'a,b,c'.rsplit(',', 1) == ['a,b', 'c']
    with pytest.raises(ValueError):
        'abc'.split('')
    with pytest.raises(TypeError):
        'abc'.replace('a', None)
