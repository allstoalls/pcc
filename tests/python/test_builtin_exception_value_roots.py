"""Builtin exception values copy from their canonical mapped cache owner."""
import re

import pytest

from ir_pointer_aliases import canonical_pointer, function_bodies, pointer_bitcast_aliases
from tests.python.test_shared_call_binding import _emit
from tests.python.test_native_exception_class_expressions import NAME_ATTR_TUPLE_SOURCE
from tests.python.owned_regression_support import (
    assert_owned_program,
    explicit_owned_runtime,
)


@pytest.mark.parametrize('body', (
    '    return ValueError\n',
    '    return take(value=ValueError)\n',
    '    def saved(value=ValueError):\n        return value\n    return saved()\n',
    '    class Errors:\n        Error = ValueError\n    return Errors\n',
    '    return (ValueError, TypeError)\n',
))
def test_exception_value_is_copied_from_its_real_cache_slot(body):
    text = _emit('def take(*, value, **extras):\n    return value\ndef probe():\n' + body)
    checked = 0
    for _name, function in function_bodies(text):
        aliases = pointer_bitcast_aliases(function)
        cache_slots = re.findall(r'(%[\w.$]+) = call [^\n]*@py_subs_exc_cache_slot\(i64 \d+\)', function)
        copies = re.findall(r'\bcall [^\n]*@pcc_gc_root_copy_lease\(ptr (%[\w.$]+), ptr (%[\w.$]+)\)', function)
        sources = {canonical_pointer(source, aliases) for _target, source in copies}
        for source in cache_slots:
            assert source in sources
            checked += 1
    assert checked


@pytest.mark.parametrize('source', (
    'def probe(ValueError):\n    return take(value=ValueError)\n',
    'ValueError = "shadow"\ndef probe():\n    return take(value=ValueError)\n',
))
def test_shadowed_exception_spelling_uses_its_actual_binding(source):
    text = _emit('def take(*, value, **extras):\n    return value\n' + source)
    body = next(body for name, body in function_bodies(text) if name == 'user_binding_probe')
    assert not re.search(r'\bcall [^\n]*@py_subs_exc_cache_slot\(', body)
    assert re.search(r'\bcall [^\n]*@pcc_gc_root_copy(?:_borrowed)?_lease\(', body)


@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_original_name_attribute_tuple_native_five_gc(python_program_compiler, request,
                                                     explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(NAME_ATTR_TUPLE_SOURCE, 'EXCEPTION_CLASS_NAME_ATTR_TUPLE_OK\n', tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime,
                         capfd, provenance_probe='2')
    assert (tmp_path / 'compiler-wrapper.stderr').read_text() == ''
