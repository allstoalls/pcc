"""Native mkdtemp is an ordinary callable with authoritative call slots."""
import re

import pytest

from tests.python.test_shared_call_binding import _emit, _function


@pytest.mark.parametrize('binding', ('module','module-alias','import-alias','value-alias'))
@pytest.mark.parametrize('site', ('assignment','nested','return'))
def test_mkdtemp_uses_native_callable_and_slot_result(binding,site,tmp_path):
    imports={
        'module':'import tempfile\n',
        'module-alias':'import tempfile as temporary\n',
        'import-alias':'from tempfile import mkdtemp as make\n',
        'value-alias':'import tempfile\nmake = tempfile.mkdtemp\n',
    }
    callee={'module':'tempfile.mkdtemp','module-alias':'temporary.mkdtemp',
            'import-alias':'make','value-alias':'make'}[binding]
    producer=callee+"(prefix='owned_', suffix='_tail', dir=root)"
    bodies={
        'assignment':'    tmpdir = '+producer+'\n    return take(value=tmpdir)\n',
        'nested':'    return take(value='+producer+')\n',
        'return':'    return '+producer+'\n',
    }
    text=_emit(imports[binding]+'def take(*, value, **extras):\n    return value\n'
               +'def probe(root):\n'+bodies[site])
    (tmp_path/'program.ll').write_text(text)
    body=_function(text)
    assert '@py_obj_call_slots(' in body
    assert 'strict.nolib.stub' not in text
    assert not re.search(r'\bcall [^\n]*@py_cpy_',text)
    assert re.search(r'\bcall [^\n]*@py_tempfile_mkdtemp_function\(',text)


def test_mkdtemp_shadowed_receiver_is_runtime_bound():
    body=_function(_emit('def probe(tempfile, root):\n    return tempfile.mkdtemp(dir=root)\n'))
    assert '@py_tempfile_mkdtemp_function(' not in body
    assert '@py_obj_getattr(' in body
    assert '@py_obj_call(' in body


def test_mkdtemp_native_safety_program_lowers_without_foreign_calls(tmp_path):
    from tests.python.test_owned_mkdtemp_native import PROGRAM
    text=_emit(PROGRAM)
    (tmp_path/'native-safety.ll').write_text(text)
    assert 'strict.nolib.stub' not in text
    assert not re.search(r'\bcall [^\n]*@py_cpy_',text)
