"""Object subscript producers publish before cleanup and default insertion."""
from __future__ import annotations
import re
import textwrap
import pytest
from tests.python.test_slot_call_operand_roots import _emit as _emit_operand_probe
from tests.python.test_shared_call_binding import _emit as _emit_binding
from tests.python.owned_regression_support import assert_owned_program, explicit_owned_runtime


def _assert_immediate_publication(text, runtime):
    calls = list(re.finditer(r'^  (%[^ ]+) = call [^\n]*@' + runtime + r'\([^\n]*\)\n', text, re.M))
    assert calls, runtime
    for match in calls:
        following = text[match.end():].splitlines()[0]
        assert following.lstrip().startswith('store ptr ' + match.group(1) + ','), following
    assert '@pcc_gc_foreign_lease_acquire(' in text
    assert '@pcc_gc_foreign_lease_release(' in text


def _assert_default_root_survives_metadata(text):
    publication = re.search(r'(%[^ ]+) = call [^\n]*@py_obj_subscript\([^\n]*\)\n'
                            r'  store ptr \1, ptr (%[^ ,\n]+)', text)
    assert publication is not None
    root = publication.group(2)
    later = text[publication.end():]
    kind = later.index('func.sig.kind')
    insertion = None
    for load in re.finditer(r'(%[^ ]+) = load ptr, ptr ' + re.escape(root) + r'(?:,|\n)', later):
        use = re.search(r'call void[^\n]*@py_tuple_set_item\([^\n]*' + re.escape(load.group(1)) + r'\)', later[load.end():])
        if use is not None:
            insertion = (load.start(), load.end() + use.end())
            break
    assert insertion is not None
    assert kind < insertion[0]
    assert '@pcc_gc_foreign_lease_acquire(' in later[kind:insertion[0]]
    assert '@pcc_gc_take_pinned_slot(' not in later[:insertion[1]]


@pytest.mark.parametrize('receiver,key', [
    ('items', 'index'), ('[value]', '0'), ('(value,)', '0'),
    ("{'key': value}", "'key'"), ("'text'", '1'), ("b'bytes'", '1'),
    ('items', 'start:stop:step'), ('items', ':'), ('items', 'lookup()[0]'),
])
def test_subscript_object_operand_publishes_immediately(receiver, key):
    source = ('def lookup():\n    return [0]\n'
              'def probe(items, index, value, start, stop, step):\n'
              '    return slot_operand_probe(' + receiver + '[' + key + '])\n')
    text = _emit_operand_probe(source)
    _assert_immediate_publication(text, 'py_obj_subscript')
    if ':' in key:
        _assert_immediate_publication(text, 'py_slice_new')


@pytest.mark.parametrize('site', ('argument', 'default', 'return', 'later-error'))
@pytest.mark.parametrize('producer', ('__file__', 'values[index]', 'make()'))
def test_name_subscript_call_producer_context_matrix(site, producer):
    prelude = ('def make():\n    return {"result": 42}\n'
               'def take(*, value, later=None, **extras):\n    return value\n'
               'def fail():\n    raise ValueError("later")\n')
    if site == 'default':
        body = '    def target(value=' + producer + '):\n        return value\n    return target()\n'
    elif site == 'return':
        body = '    return ' + producer + '\n'
    elif site == 'later-error':
        body = '    return take(value=' + producer + ', later=fail())\n'
    else:
        body = '    return take(value=' + producer + ')\n'
    text = _emit_binding(prelude + 'def probe(values, index):\n' + body)
    assert 'define' in text
    if producer == 'values[index]' and site != 'return':
        _assert_immediate_publication(text, 'py_obj_subscript')
        if site == 'default':
            _assert_default_root_survives_metadata(text)


PROGRAMS = {
    'containers': textwrap.dedent('''\
        import gc
        def take(*, value, **extras):
            gc.collect()
            return value
        def main():
            state = {'answer': 42}
            values = [state, state]
            assert take(value=values[0]) is state
            assert take(value=(state,)[0]) is state
            assert take(value={'key': state}['key']) is state
            assert take(value='hello'[1]) == 'e'
            assert take(value=b'hello'[1]) == 101
            assert take(value=bytearray(b'ab')[0]) == 97
            assert take(value=values[:]) == values
            assert take(value=values[0:2:1])[0] is state
            assert take(value='hello'[1:4]) == 'ell'
            assert take(value=b'hello'[1:4]) == b'ell'
            assert take(value=values[[0][0]]) is state
            print('SUBSCRIPT_CONTAINERS_OK')
        main()
    '''),
    'callbacks': textwrap.dedent('''\
        import gc
        events = []
        disposed = []
        state = {'answer': 42}
        class Token:
            def __del__(self):
                disposed.append(1)
        class Temporary:
            def __getitem__(self, key):
                gc.collect()
                return Token()
        class Receiver:
            def __getitem__(self, key):
                gc.collect()
                events.append('get')
                if key == 'raise':
                    raise KeyError('lookup')
                return state
        def receiver():
            events.append('receiver')
            return Receiver()
        def key():
            gc.collect()
            events.append('key')
            return 'ok'
        def take(*, value, later=None, **extras):
            gc.collect()
            return value
        def later():
            gc.collect()
            raise ValueError('later')
        def returned(obj):
            return obj['ok']
        def main():
            assert take(value=receiver()[key()]) is state
            assert events == ['receiver', 'key', 'get']
            obj = Receiver()
            assert take(value=returned(obj)) is state
            def target(value=obj['ok']):
                gc.collect()
                return value
            assert target() is state
            try:
                take(value=obj['raise'])
            except KeyError:
                pass
            else:
                raise AssertionError('missing lookup exception')
            try:
                take(value=obj['ok'], later=later())
            except ValueError:
                pass
            else:
                raise AssertionError('missing later exception')
            try:
                take(value=Temporary()[0], later=later())
            except ValueError:
                pass
            else:
                raise AssertionError('missing temporary cleanup exception')
            gc.collect()
            assert disposed == [1]
            print('SUBSCRIPT_CALLBACKS_OK')
        main()
    '''),
}
EXPECTED = {'containers': 'SUBSCRIPT_CONTAINERS_OK\n', 'callbacks': 'SUBSCRIPT_CALLBACKS_OK\n'}


@pytest.mark.integration
@pytest.mark.parametrize('case', PROGRAMS)
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_subscript_producer_native_five_gc(case, python_program_compiler, request,
                                          explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(PROGRAMS[case], EXPECTED[case], tmp_path, python_program_compiler,
                         mode, explicit_owned_runtime, capfd, provenance_probe='2')
    assert (tmp_path/'compiler-wrapper.stderr').read_text() == ''
