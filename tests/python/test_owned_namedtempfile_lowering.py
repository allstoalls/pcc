"""NamedTemporaryFile uses native managers, name attributes and callable aliases."""
import re

import pytest

from tests.python.test_shared_call_binding import _emit, _function


@pytest.mark.parametrize('binding',('module','module-alias','import-alias','value-alias'))
@pytest.mark.parametrize('site',('with','assignment','return'))
def test_namedtempfile_binding_stays_native(binding,site,tmp_path):
    imports={'module':'import tempfile\n','module-alias':'import tempfile as temp\n',
             'import-alias':'from tempfile import NamedTemporaryFile as make\n',
             'value-alias':'import tempfile\nmake = tempfile.NamedTemporaryFile\n'}
    callee={'module':'tempfile.NamedTemporaryFile','module-alias':'temp.NamedTemporaryFile',
            'import-alias':'make','value-alias':'make'}[binding]
    producer=callee+'(mode="w", encoding="utf-8", dir=path.parent, delete=False)'
    bodies={'with':'    with '+producer+' as stream:\n        consume(value=stream.name)\n        stream.write("value")\n',
            'assignment':'    stream = '+producer+'\n    return consume(value=stream.name)\n',
            'return':'    return '+producer+'\n'}
    text=_emit(imports[binding]+'def consume(*, value, **extras):\n    return value\n'
               +'def probe(path):\n'+bodies[site])
    (tmp_path/'program.ll').write_text(text)
    assert '@py_obj_call_slots(' in _function(text)
    assert re.search(r'\bcall [^\n]*@py_namedtempfile_function\(',text)
    assert not re.search(r'\bcall [^\n]*@py_cpy_',text)
    assert 'strict.nolib.stub' not in text
