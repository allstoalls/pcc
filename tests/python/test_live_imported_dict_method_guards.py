"""Live imported computed dictionaries reuse ordinary dynamic method guards."""

import os
import subprocess
import sys

import pytest

from pcc.frontends.python.pipeline import compile_python
from tests.python.test_live_import_return_domain import _body, _codegen

PROVIDER = '''DATA = {name: value for name, value in [('first', [1]), ('second', [2])]}
class Box:
    def __init__(self):
        self.reads = 0
        self._mapping = {'value': [9]}
    @property
    def mapping(self):
        self.reads += 1
        return self._mapping

def make_box():
    return Box()
BOX = make_box()
'''
PROGRAM = '''from provider import DATA, BOX

events = []
class Replacement:
    @property
    def get(self):
        events.append('lookup')
        return self.read
    def read(self, key, default=None):
        if key == 'raise':
            raise ValueError('replacement failed')
        return default

def argument():
    events.append('argument')
    return 'missing'

def lookup(key, default=None):
    return DATA.get(key, default)

def replace_and_read():
    global DATA
    DATA = Replacement()
    sentinel = [17]
    assert DATA.get(argument(), sentinel) is sentinel
    assert events == ['lookup', 'argument'], events
    assert lookup('another', sentinel) is sentinel
    assert events == ['lookup', 'argument', 'lookup'], events
    try:
        lookup('raise')
    except ValueError as error:
        assert str(error) == 'replacement failed'
    else:
        raise AssertionError('replacement exception lost')

def main():
    assert DATA.get('missing') is None
    sentinel = [7]
    assert DATA.get('missing', sentinel) is sentinel
    first = DATA.get('first')
    assert first == [1]
    assert DATA.setdefault('first', sentinel) is first
    assert DATA.setdefault('third', sentinel) is sentinel
    assert list(DATA.keys()) == ['first', 'second', 'third']
    assert list(DATA.values()) == [[1], [2], [7]]
    assert list(DATA.items()) == [('first', [1]), ('second', [2]), ('third', [7])]
    copied = DATA.copy()
    assert copied['first'] is first
    copied['separate'] = 1
    assert DATA.get('separate') is None
    assert DATA.pop('second') == [2]
    assert DATA.pop('absent', sentinel) is sentinel
    DATA.update({'tail': [8]})
    assert DATA.get('tail') == [8]
    assert BOX.mapping.get('value') == [9]
    assert BOX.reads == 1, BOX.reads
    try:
        DATA.get([])
    except TypeError:
        pass
    else:
        raise AssertionError('unhashable key accepted')
    replace_and_read()
    print('LIVE_IMPORTED_DICT_METHODS_OK')
main()
'''

@pytest.mark.parametrize("expression,helper", (
    ("DATA.get(key)", "py_dict_get_default_slots"),
    ("DATA.setdefault(key, value)", "py_dict_setdefault_slots"),
    ("DATA.keys()", "py_dict_keys"),
    ("DATA.values()", "py_dict_values"),
    ("DATA.items()", "py_dict_items"),
    ("DATA.update(value)", "py_dict_update_slots"),
    ("DATA.copy()", "py_copy_copy"),
    ("DATA.pop(key, value)", "py_dict_get"),
))
def test_live_computed_import_retains_dictionary_guard(tmp_path, expression, helper):
    provider = tmp_path / "provider.py"
    provider.write_text("DATA = {name: index for index, name in enumerate(('first',))}\n")
    consumer = tmp_path / "consumer.py"
    consumer.write_text("from provider import DATA\ndef probe(key, value):\n    return " + expression + "\n")
    codegen, typed = _codegen([str(provider), str(consumer)], ["provider", "consumer"])
    text = str(codegen.generate(typed))
    function = _body(text, "user_consumer_probe")
    assert "@py_obj_type_tag" in function
    assert "@" + helper in function
    # Exact dictionaries use owned helpers. A replacement still resolves its
    # ordinary method, before evaluating arguments, through the guarded arm.
    assert "@py_obj_getattr" in function
    assert "@py_cpy_" not in function


def test_live_imported_dictionary_methods_execute_natively(tmp_path, pcc_runtime_archive):
    provider = tmp_path / "provider.py"
    provider.write_text(PROVIDER)
    source = tmp_path / "main.py"
    source.write_text(PROGRAM)
    reference = subprocess.run([sys.executable, str(source)], capture_output=True,
                               text=True, timeout=20)
    assert reference.returncode == 0, reference.stderr
    output = tmp_path / "program"
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   runtime_archive=str(pcc_runtime_archive))
    ran = subprocess.run([str(output)], capture_output=True, text=True, timeout=40,
                         env=dict(os.environ, PCC_GC_REFCOUNT_PROVENANCE_PROBE="2"))
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout == reference.stdout
    assert ran.stderr == ""
