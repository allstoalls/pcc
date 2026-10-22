"""Public fdopen bindings publish real native file owners."""
import re

import pytest

from tests.python.test_shared_call_binding import _emit, _function


@pytest.mark.parametrize('binding',('module','module-alias','import-alias','value-alias'))
@pytest.mark.parametrize('site',('with','assignment','return'))
def test_fdopen_binding_stays_native(binding,site,tmp_path):
    imports={'module':'import os\n','module-alias':'import os as system\n',
             'import-alias':'from os import fdopen as make\n',
             'value-alias':'import os\nmake = os.fdopen\n'}
    callee={'module':'os.fdopen','module-alias':'system.fdopen',
            'import-alias':'make','value-alias':'make'}[binding]
    producer=callee+'(fd, "w", encoding="utf-8")'
    bodies={'with':'    with '+producer+' as stream:\n        consume(value=stream)\n',
            'assignment':'    stream = '+producer+'\n    return consume(value=stream)\n',
            'return':'    return '+producer+'\n'}
    text=_emit(imports[binding]+'def consume(*, value, **extras):\n    return value\n'
               +'def probe(fd):\n'+bodies[site])
    (tmp_path/'program.ll').write_text(text)
    assert '@py_obj_call_slots(' in _function(text)
    assert re.search(r'\bcall [^\n]*@py_file_fdopen_function\(',text)
    assert not re.search(r'\bcall [^\n]*@py_cpy_',text)
    assert 'strict.nolib.stub' not in text


def test_fdopen_shadowed_receiver_preserves_dynamic_dispatch():
    body=_function(_emit('def probe(os, fd):\n    return os.fdopen(fd)\n'))
    assert '@py_file_fdopen_function(' not in body
    assert '@py_obj_load_method(' in body
