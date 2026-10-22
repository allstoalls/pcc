"""Canonical object construction and its native base/subclass boundaries."""
import ast
from pathlib import Path
import re
import textwrap

import pytest

from pcc.runtime.py import py_abi_constants as abi
from tests.python.test_foreign_address_leases import _functions
from tests.python.test_shared_call_binding import _emit
from tests.python.owned_regression_support import assert_owned_program, explicit_owned_runtime

RUNTIME = Path(__file__).resolve().parents[2] / 'pcc/runtime/py'


@pytest.mark.parametrize('body', (
    '    return object()\n',
    '    return [object()]\n',
    '    return (object(),)\n',
    '    return take(value=object())\n',
    '    def nested(value=object()):\n        return value\n    return nested()\n',
    '    return take(value=object().__class__)\n',
))
def test_object_constructor_publishes_actual_new_result_before_cleanup(body):
    text = _emit('def take(*, value, **extras):\n    return value\ndef probe():\n' + body)
    assert 'object.sentinel' not in text
    assert '@py_builtin_type_for_tag(i64 -1)' in text
    assert re.search(r'(%[^ ]+) = call [^\n]*@py_instance_new\([^\n]*\)\n\s+store ptr \1, ptr ', text)
    assert '@pcc_gc_foreign_lease_acquire(' in text


def test_canonical_object_class_is_slots_only_before_publication():
    cls = object()
    events = []
    globals_ = {}
    ns = {name: getattr(abi, name) for name in dir(abi) if name.isupper()}
    ns.update(null=lambda: None, cstr=lambda value: value,
              ptr_is_null=lambda value: int(value is None),
              global_load_ptr=lambda name: globals_.get(name),
              global_store_ptr=lambda name, value: (events.append('publish'), globals_.__setitem__(name, value)),
              py_subs_object_root=lambda: events.append('root') or cls,
              atomic_rmw_i32=lambda op, value, offset, bits, order: events.append(('slots', value, offset, bits)),
              py_incref=lambda value: events.append(('retain', value)))
    _functions(RUNTIME / 'py_obj_ops_dispatch.py', {'_builtin_type_class_for_tag', '_return_builtin_type'}, ns)
    assert ns['_builtin_type_class_for_tag'](-1) is cls
    assert events == ['root', ('slots', cls, abi.PYOBJECTHEADER_FLAGS_OFFSET, 2), 'publish', ('retain', cls)]
    assert ns['_builtin_type_class_for_tag'](-1) is cls
    assert events[-1] == ('retain', cls) and events.count('root') == 1


@pytest.mark.parametrize('kind', ('base', 'subclass', 'non-instance', 'extension'))
@pytest.mark.parametrize('relocate', (False, True))
def test_weakref_exact_base_check_uses_one_relocation_epoch(kind, relocate):
    state = {'depth': 0, 'base': 2000, 'target': 1000, 'class': 2000 if kind == 'base' else 3000}
    tag = abi.PY_TYPE_STR if kind == 'non-instance' else 65536 if kind == 'extension' else abi.PY_TYPE_USER_CLASS_START
    reads = []
    def lock():
        if relocate:
            state['target'] = 1100
            state['base'] = 2200
            if kind == 'base':
                state['class'] = 2200
        state['depth'] += 1
    def unlock():
        state['depth'] -= 1
    def load(owner, slot):
        assert state['depth'] == 1
        reads.append(slot)
        if slot == 'py_object_root_cache':
            return state['base']
        assert slot == state['target'] + abi.PYINSTANCEOBJECT_CLS_OFFSET
        return state['class']
    ns = {name: getattr(abi, name) for name in dir(abi) if name.isupper()}
    ns.update(load_i32=lambda value, offset: tag,
              pcc_capi_is_cext_type_tag=lambda value: int(kind == 'extension'),
              pcc_py_gc_minor_graph_lock=lock, pcc_py_gc_minor_graph_unlock=unlock,
              pcc_gc_note_relocation_read=lambda value: state['target'],
              pcc_gc_load_ptr=load, ptr_add=lambda value, offset: value + offset,
              global_addr=lambda name: name, null=lambda: None,
              ptr_is_null=lambda value: int(value is None), ptr_eq=lambda a, b: int(a == b))
    _functions(RUNTIME / 'py_weakref.py', {'_weakref_target_is_base_object'}, ns)
    assert ns['_weakref_target_is_base_object'](1000) == (kind == 'base')
    assert state['depth'] == 0
    assert len(reads) == (2 if kind in ('base', 'subclass') else 0)


PROGRAM = textwrap.dedent('''\
    import gc
    import weakref
    class Child(object):
        pass
    def take(*, value, **extras):
        gc.collect()
        return value
    def main():
        first = object()
        second = object()
        assert first is not second
        assert type(first) is object
        assert bool(first)
        assert hash(first) == hash(first)
        assert take(value=first) is first
        assert type(take(value=object())) is object
        def saved(value=object()):
            gc.collect()
            return value
        assert saved() is saved()
        assert type(saved()) is object
        try:
            first.field = 1
        except AttributeError:
            pass
        else:
            raise AssertionError('base object accepted an attribute')
        try:
            first.__dict__
        except AttributeError:
            pass
        else:
            raise AssertionError('base object exposed an instance dict')
        try:
            weakref.ref(first)
        except TypeError:
            pass
        else:
            raise AssertionError('base object accepted a weak reference')
        child = Child()
        child.field = first
        assert child.field is first
        reference = weakref.ref(child)
        assert reference() is child
        del child
        gc.collect()
        assert reference() is None
        print('CANONICAL_OBJECT_OK')
    main()
''')


@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_canonical_object_native_five_gc(python_program_compiler, request,
                                       explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(PROGRAM, 'CANONICAL_OBJECT_OK\n', tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime,
                         capfd, provenance_probe='2')
    assert (tmp_path / 'compiler-wrapper.stderr').read_text() == ''
