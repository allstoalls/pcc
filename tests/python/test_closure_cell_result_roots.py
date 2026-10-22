"""Compiler cell reads publish the list getter's actual NEW owner immediately."""
import re

import pytest

from tests.python.test_lambda_constructor_roots import emit


CASES = {
    'sorted_source': '''def probe(free):
    def replace(value):
        nonlocal free
        free = value
    return tuple(sorted(free))
''',
    'call_argument': '''def take(*, value, **extras):
    return value
def probe(value):
    def replace(other):
        nonlocal value
        value = other
    return take(value=value)
''',
    'default': '''def probe(value):
    def replace(other):
        nonlocal value
        value = other
    def kept(arg=value):
        return arg
    return kept
''',
    'class_capture': '''def probe(value):
    class Reader:
        def read(self):
            return value
    return Reader
''',
}


@pytest.mark.parametrize('case', CASES)
def test_cell_payload_is_published_before_error_probe_or_owner_cleanup(case):
    text = emit(CASES[case])
    matches = list(re.finditer(r'(%[-\w.]+) = call ptr[^\n]*@py_list_getitem\([^\n]*\n([^\n]+)', text))
    assert matches, case
    for produced in matches:
        assert 'store ptr ' + produced.group(1) in produced.group(2), case
    assert 'cell.bound.error' in text
    assert re.search(r'\bcall[^\n]*@pcc_gc_foreign_lease_acquire\(', text)
    assert re.search(r'\bcall[^\n]*@pcc_gc_foreign_lease_release\(', text)
    if case == 'class_capture':
        # The method is a closure: its function object passes the ``value``
        # cell as a trailing parameter, read through the published cell
        # payload above; no hidden instance attribute carries it.
        assert re.search(r'^define[^\n]*@[\w.]*Reader_read\(ptr %self, ptr %value\)', text, re.M)
        assert '.pcc.capture.' not in text
        assert not re.search(r'\bcall[^\n]*@py_instance_getattr_default\(', text)


def test_cell_error_discrimination_follows_published_result_and_runtime_error_check():
    text = emit('''def probe():
    value: object
    def read():
        return value
    return value, read
''')
    assert re.search(r'@py_exc_new\(i64 63,', text)
    assert re.search(r'@py_exc_new\(i64 10,', text)
    for body in re.findall(r'^define[^\n]*\([^\n]*\).*?^}', text, re.M | re.S):
        for result in re.finditer(r'(%[-\w.]+) = call ptr[^\n]*@py_list_getitem\([^\n]*\n([^\n]+)', body):
            assert 'store ptr ' + result.group(1) in result.group(2)
            rest = body[result.end():]
            assert rest.index('@py_err_occurred(') < rest.index('cell.bound')
