"""Native lambda constructors publish captures and functions before cleanup."""
import re

import pytest

from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.type_infer import infer_module


CASES = {
    'identity': '''def take(*, value, **extras):
    return value
def probe():
    return take(value=lambda x: x)
''',
    'subscript': '''def take(*, value, **extras):
    return value
def probe():
    return take(value=lambda x: x[0])
''',
    'negative': '''def take(*, value, **extras):
    return value
def probe():
    return take(value=lambda x: -x)
''',
    'capture': '''def take(*, value, **extras):
    return value
def probe(kept):
    return take(value=lambda x: (x, kept))
''',
    'indexed_default': '''def take(*, value, **extras):
    return value
def probe(values):
    return take(value=lambda x=values[0]: x)
''',
    'late_default_error': '''def take(*, value, **extras):
    return value
def make():
    return []
def fail():
    raise ValueError('later default')
def probe():
    return take(value=lambda x=make(), y=fail(): x)
''',
    'ordinary_assignment': '''def probe(kept):
    result = lambda x: (x, kept)
    return result
''',
    'direct_return': '''def probe(kept):
    return lambda x: (x, kept)
''',
    'nested_lambda': '''def take(*, value, **extras):
    return value
def probe(kept):
    return take(value=lambda x: (lambda y: (x, y, kept)))
''',
}


def emit(source):
    module = infer_module(parse_and_lift(source, 'lambda_owned.py', 'lambda_owned'))
    codegen = L1CodeGen(module, ir_scaffold_mode='on')
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    return str(codegen.generate(module))


def immediate_store(text, callee):
    count = 0
    lines = text.splitlines()
    for index, line in enumerate(lines):
        found = re.search(r'(%[-\w.]+) = call ptr[^\n]*@' + callee + r'\(', line)
        if not found:
            continue
        following = lines[index + 1].strip()
        assert re.match(r'store ptr ' + re.escape(found.group(1)) + r', ptr %[-\w.]+', following), (line, following)
        count += 1
    return count


@pytest.mark.parametrize('case', CASES)
def test_native_lambda_constructor_has_immediate_authoritative_result(case):
    text = emit(CASES[case])
    if case == 'ordinary_assignment':
        # Assignment is hoisted to the existing named-function constructor.
        # This is a compatibility control for that actual rooted route: the
        # constructor publishes into its output slot, reads the captures from
        # their operand root and reports a checked status.
        selected = re.search(r'^define[^\n]*@user_lambda_owned_probe\([^\n]*\).*?^}', text, re.M | re.S)
        assert selected is not None
        checked = selected.group(0)
        constructions = re.findall(
            r'(%[-\w.]+) = call i64 [^\n]*@py_func_new_signature_slots\(ptr (%[-\w.]+)', checked,
        )
        assert constructions, case
        for status, captures in constructions:
            # The capture operand root is passed directly, or through an identity alias.
            assert (re.fullmatch(r'%function\.captures\.operand[-\w.]*', captures)
                    or re.search(re.escape(captures) + r' = bitcast ptr %function\.captures\.operand[-\w.]* to ptr', checked))
            assert re.search(r'icmp slt i64 ' + re.escape(status) + r', 0', checked)
        assert re.search(r'\bcall[^\n]*@py_cpy_', text) is None
        return
    else:
        checked = text
        created = immediate_store(checked, 'py_func_new')
        capture_prefix = 'lambda.native'
    assert created >= 1, case
    # Check capture tuple publication in the actual selected constructor.
    lines = checked.splitlines()
    captures = 0
    for index, line in enumerate(lines):
        found = re.search(r'(%' + re.escape(capture_prefix) + r'\.captures\.new[-\w.]*) = call ptr[^\n]*@py_tuple_new\(', line)
        if found:
            assert re.match(r'\s*store ptr ' + re.escape(found.group(1)) + r', ptr %[-\w.]+', lines[index + 1])
            captures += 1
    assert captures >= 1
    cpy_call = re.search(r'\bcall[^\n]*@py_cpy_', text)
    assert cpy_call is None
