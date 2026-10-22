from pathlib import Path
import subprocess
import sys
import textwrap
import pytest
from tests.python.test_shared_call_binding import _emit
from tests.python.owned_regression_support import (
    assert_owned_program,
    explicit_owned_runtime,
)

@pytest.mark.parametrize('expression', (
    'mapping.setdefault(key)',
    'mapping.setdefault(key, fallback)',
    'mapping.setdefault(key, set()).update(values)',
))
@pytest.mark.parametrize('annotation', ('', ': dict'))
def test_original_setdefault_result_reaches_strict_owned_context(expression, annotation):
    program = 'def take(value):\n    return value\ndef probe(mapping' + annotation + ', key, fallback, values):\n    return take(' + expression + ')\n'
    result = _emit(program)
    assert 'strict.nolib.stub' not in result

PROGRAM = textwrap.dedent('''\
import gc

events = []

class Key:
    def __hash__(self):
        gc.collect()
        return 11
    def __eq__(self, other):
        gc.collect()
        return True

class Missing:
    def __hash__(self):
        gc.collect()
        raise ValueError('hash failed')

class Box:
    def __init__(self, tag):
        self.tag = tag
    def __del__(self):
        events.append(self.tag)

def mark(name, value):
    events.append(name)
    gc.collect()
    return value

class Receiver:
    def __getattribute__(self, name):
        if name == 'setdefault':
            events.append('lookup')
        return object.__getattribute__(self, name)
    def setdefault(self, key, default):
        events.append('call')
        gc.collect()
        return default

def main():
    key = Key()
    mapping = {}
    fallback = Box('fallback')
    assert mapping.setdefault(key, fallback) is fallback
    assert mapping[key] is fallback
    assert mapping.setdefault(Key(), mark('default evaluated', Box('unused'))) is fallback
    gc.collect()
    assert events == ['default evaluated', 'unused']
    assert {}.setdefault('absent') is None
    table = {}
    table.setdefault('base', set()).update(('left', 'right'))
    assert table['base'] == {'left', 'right'}
    try:
        mapping.setdefault(Missing(), fallback)
    except ValueError as error:
        assert str(error) == 'hash failed'
    else:
        raise AssertionError('missing hash error')
    assert len(mapping) == 1
    gc.collect()
    assert mapping[key] is fallback
    events.clear()
    receiver = Receiver()
    assert receiver.setdefault(mark('key', 1), mark('value', fallback)) is fallback
    assert events == ['lookup', 'key', 'value', 'call']
    print('SETDEFAULT_OWNERS_OK')

main()
''')

def test_setdefault_behavior_reference(tmp_path):
    p=tmp_path/'program.py';p.write_text(PROGRAM)
    run=subprocess.run([sys.executable,'-B',str(p)],capture_output=True,text=True,timeout=30)
    assert (run.returncode,run.stdout,run.stderr)==(0,'SETDEFAULT_OWNERS_OK\n','')

def test_setdefault_behavior_compiles_without_stub():
    assert 'strict.nolib.stub' not in _emit(PROGRAM)


@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_setdefault_owners_native_five_gc(python_program_compiler, request,
                                          explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(PROGRAM, 'SETDEFAULT_OWNERS_OK\n', tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime,
                         capfd, provenance_probe='2')
    assert (tmp_path / 'compiler-wrapper.stderr').read_text() == ''
