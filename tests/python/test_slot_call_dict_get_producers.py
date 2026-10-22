"""Dictionary lookup owns its selected result across branch and cleanup paths."""
from __future__ import annotations

import ast
from pathlib import Path
import re
import textwrap

import pytest

from ir_pointer_aliases import (
    canonical_pointer,
    function_bodies,
    pointer_bitcast_aliases,
)

from pcc.runtime.py import py_abi_constants as abi
from tests.python.test_shared_call_binding import _emit
from tests.python.test_dict_super_slot_roots import DictMemory
from tests.python.test_set_call_slot_roots import Object
from tests.python.owned_regression_support import (
    assert_owned_program,
    explicit_owned_runtime,
)


@pytest.mark.parametrize('annotation', ('', ': dict'))
@pytest.mark.parametrize('arguments', ('key', 'key, fallback'))
@pytest.mark.parametrize('site', ('return', 'argument', 'default', 'binary'))
def test_dict_get_branches_publish_same_caller_owner(annotation, arguments, site, tmp_path):
    expression = 'mapping.get(' + arguments + ')'
    prefix = 'def take(*, value, **extras):\n    return value\n'
    if site == 'return':
        body = '    return ' + expression + '\n'
    elif site == 'default':
        body = '    def saved(value=' + expression + '):\n        return value\n    return saved()\n'
    elif site == 'binary':
        body = '    return ' + expression + ' + (1,)\n'
    else:
        body = '    return take(value=' + expression + ')\n'
    source = prefix + 'def probe(mapping' + annotation + ', key, fallback):\n' + body
    text = _emit(source)
    (tmp_path / 'input.py').write_text(source)
    (tmp_path / 'program.ll').write_text(text)
    checked = 0
    for name, body in function_bodies(text):
        native = re.findall(r'^  %dict\.get\.invoke[^ ]* = call [^\n]*@py_dict_get_default_slots\(([^\n]*)\)', body, re.M)
        generic = re.findall(r'^  %dict\.get\.generic\.invoke[^ ]* = call [^\n]*@py_obj_call_slots\(([^\n]*)\)', body, re.M)
        if not native and not generic:
            continue
        assert native and len(native) == len(generic), name
        aliases = pointer_bitcast_aliases(body)
        def owner(row):
            value = row.rsplit(',', 1)[-1].strip().rsplit(' ', 1)[-1]
            return canonical_pointer(value, aliases)
        assert [owner(row) for row in native] == [owner(row) for row in generic], name
        checked += len(native)
    assert checked
    assert not re.search(r'%dyn\.dict\.result[^\n]* = phi ptr', text)
    assert '@pcc_gc_root_copy_lease(' in text


class _GetMemory(DictMemory):
    def __init__(self, phase='register', **kwargs):
        super().__init__(phase, **kwargs)
        path = Path(__file__).parents[2] / 'pcc/runtime/py/py_dict.py'
        tree = ast.parse(path.read_text())
        for node in tree.body:
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id.startswith('_DICT_SLOT_')):
                self.ns[node.targets[0].id] = ast.literal_eval(node.value)
        names = {'_dict_slot_set_core', '_dict_slot_set_bound', 'py_dict_get_default_slots'}
        body = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        assert len(body) == 3
        for node in body:
            node.decorator_list = []
        exec(compile(ast.Module(body=body, type_ignores=[]), str(path), 'exec'), self.ns)

    def get(self, values, key, default):
        target, query, fallback = self.wrap(values), self.wrap(key), self.wrap(default)
        caller = self.input_roots((target, query, fallback, None))
        before = self.unwrap(self.read(caller, 0))
        status = self.ns['py_dict_get_default_slots'](caller, self.add(caller, 8), self.add(caller, 16), self.add(caller, 24))
        result = self.read(caller, 24)
        assert len(self.roots) == 4 and self.depth == 0
        assert all(obj.leases == (1000 if obj is self.none else 0) for obj in self.objects if obj.alive)
        assert self.unwrap(self.read(caller, 0)) == before
        return status, result, caller


@pytest.mark.parametrize('phase', ('register', 'copy', 'acquire', 'callback', 'release', 'move', 'drop', 'unregister', 'lock'))
@pytest.mark.parametrize('hit', (False, True))
def test_actual_get_result_owner_moves_without_mutation(phase, hit):
    memory = _GetMemory(phase)
    status, result, caller = memory.get({'present': 'selected'}, 'present' if hit else 'missing', 'fallback')
    assert status == 0 and memory.error is None
    assert memory.unwrap(result) == ('selected' if hit else 'fallback')
    assert isinstance(result, Object) and result.alive and result.refs > 0
    if not hit:
        assert result is memory.read(caller, 16)
    assert memory.collections > 0


@pytest.mark.parametrize('failure', (1, 2, 3))
def test_actual_get_copy_error_leaves_output_empty(failure):
    memory = _GetMemory('copy', fail_copy=failure)
    status, result, _ = memory.get({'present': 'selected'}, 'present', 'fallback')
    assert status == -1 and result is None and memory.error is not None


def test_actual_get_does_not_grow_full_storage_on_miss():
    memory = _GetMemory('none')
    target = memory.wrap({})
    target.fields[abi.PYDICTOBJECT_ENTRIES_USED_OFFSET] = target.fields[abi.PYDICTOBJECT_CAPACITY_OFFSET]
    caller = memory.input_roots((target, memory.wrap('missing'), memory.wrap('fallback'), None))
    memory.ns['_maybe_grow'] = lambda owner: pytest.fail('read-only get attempted growth')
    status = memory.ns['py_dict_get_default_slots'](caller, memory.add(caller, 8), memory.add(caller, 16), memory.add(caller, 24))
    assert status == 0 and memory.read(caller, 24) is memory.read(caller, 16)
    assert memory.unwrap(memory.read(caller, 0)) == {}


def test_actual_get_hash_error_preserves_error_and_empty_output():
    memory = _GetMemory('callback')
    memory.ns['py_obj_hash'] = lambda key: setattr(memory, 'error', (2, 'hash-error')) or 17
    status, result, _ = memory.get({}, 'key', 'fallback')
    assert status == -1 and result is None and memory.error == (2, 'hash-error')


PROGRAM = textwrap.dedent('''\
    import gc

    events = []

    class Token:
        def __init__(self, label):
            self.label = label
        def __del__(self):
            events.append('dispose:' + self.label)
            gc.collect()

    class BadHash:
        def __hash__(self):
            gc.collect()
            raise ValueError('hash-error')

    class Receiver:
        @property
        def get(self):
            events.append('lookup')
            gc.collect()
            return self.read
        def read(self, key, default):
            events.append('call')
            gc.collect()
            return default

    def marked(label, value):
        events.append(label)
        gc.collect()
        return value

    def take(*, value, later=None, **extras):
        gc.collect()
        return value

    def returned(mapping, key, fallback):
        return mapping.get(key, fallback)

    def later():
        events.append('later')
        raise TypeError('later-error')

    def main():
        chosen = Token('chosen')
        fallback = Token('fallback')
        mapping = {'present': chosen}
        result = take(value=mapping.get(marked('key', 'present'), marked('default', fallback)))
        assert result is chosen
        assert events == ['key', 'default']
        assert returned(mapping, 'missing', fallback) is fallback
        assert mapping.get('missing') is None
        assert len(mapping) == 1
        def saved(value=mapping.get('present')):
            gc.collect()
            return value
        assert saved() is chosen and saved() is saved()

        temporary = take(value={'key': Token('temporary')}.get('key'))
        gc.collect()
        assert temporary.label == 'temporary'
        assert events.count('dispose:temporary') == 0
        del temporary
        gc.collect()
        assert events.count('dispose:temporary') == 1

        events.clear()
        receiver = Receiver()
        assert returned(receiver, 'key', fallback) is fallback
        assert events == ['lookup', 'call']
        events.clear()
        assert take(value=receiver.get(marked('key', 'missing'), marked('default', fallback))) is fallback
        assert events == ['lookup', 'key', 'default', 'call']

        events.clear()
        try:
            take(value=mapping.get(BadHash(), Token('error-default')), later=later())
        except ValueError as error:
            assert str(error) == 'hash-error'
        else:
            raise AssertionError('hash failure was lost')
        gc.collect()
        assert events == ['dispose:error-default']

        events.clear()
        try:
            take(value={'key': Token('later-result')}.get('key'), later=later())
        except TypeError as error:
            assert str(error) == 'later-error'
        else:
            raise AssertionError('later argument failure was lost')
        gc.collect()
        assert events == ['later', 'dispose:later-result']
        print('DICT_GET_OWNERS_OK')

    main()
''')


@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_dict_get_owners_native_five_gc(python_program_compiler, request,
                                       explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(PROGRAM, 'DICT_GET_OWNERS_OK\n', tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime,
                         capfd, provenance_probe='2')
    assert (tmp_path / 'compiler-wrapper.stderr').read_text() == ''
