"""Host IR proofs for builtin tuple subclass isinstance; no native execution."""
from pathlib import Path
import re

import pcc
import pytest

from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.pipeline_context import build_closed_world_context
from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.type_infer import infer_module


def emit(source):
    module = infer_module(parse_and_lift(source, 'tuple_identity.py', 'tuple_identity'))
    generator = L1CodeGen(module, emit_cpy_main_exitcode=False, ir_scaffold_mode='on')
    generator._strict_no_libpython = True
    return str(generator.generate())


def check_body(text):
    return re.search(r'^define[^\n]*@user_tuple_identity_check\([^\n]*\) \{\n(.*?)^\}', text, re.M | re.S).group(1)


@pytest.mark.parametrize('source', [
    'class Record(tuple):\n    __slots__ = ()\ndef check(value: Record) -> bool:\n    return isinstance(value, tuple)\n',
    'class Record(tuple):\n    __slots__ = ()\ndef check() -> bool:\n    value = Record((1, 2))\n    return isinstance(value, tuple)\n',
    'class Record(tuple):\n    __slots__ = ()\nclass Derived(Record):\n    __slots__ = ()\ndef check(value: Derived) -> bool:\n    return isinstance(value, tuple)\n',
    'class Record(tuple):\n    __slots__ = ()\ndef check(value: Record) -> bool:\n    return isinstance(value, (str, tuple))\n',
    'class Record(tuple):\n    __slots__ = ()\ndef check(value: Record) -> bool:\n    return isinstance(value, (tuple, str))\n',
    'class Record(tuple):\n    __slots__ = ()\ndef check(value) -> bool:\n    return isinstance(value, tuple)\n',
], ids=['annotated', 'constructed', 'inherited', 'classinfo-last', 'classinfo-first', 'dynamic-control'])
def test_tuple_subclass_identity_uses_runtime_predicate(source):
    body = check_body(emit(source))
    assert re.search(r'call[^\n]*@py_tuple_check', body), body
    assert not re.search(r'call[^\n]*@py_cpy_', body)


def test_exact_tuple_keeps_constant_true():
    body = check_body(emit('def check() -> bool:\n    return isinstance((1, 2), tuple)\n'))
    assert 'icmp ne i1 1, 0' in body
    assert not re.search(r'call[^\n]*@py_tuple_check', body)


def test_string_subclass_keeps_runtime_predicate():
    body = check_body(emit('class Text(str):\n    pass\ndef check(value: Text) -> bool:\n    return isinstance(value, str)\n'))
    assert re.search(r'call[^\n]*@py_str_check', body)


def test_original_time_fixture_tuple_identity_uses_runtime_predicate():
    root = Path(pcc.__file__).resolve().parent.parent
    provider = root / 'pcc/stdlib/time.py'
    fixture = root / 'tests/fixtures/time_struct_sequence.py'
    modules, exports, derived = build_closed_world_context(
        [str(provider), str(fixture)], ['time', 'time_struct_sequence'])
    module = infer_module(modules[1], external_exports={'time': exports['time']},
                          derived_class_map=derived)
    generator = L1CodeGen(module, emit_cpy_main_exitcode=False, ir_scaffold_mode='on')
    generator._strict_no_libpython = True
    generator._native_module_exports = exports
    generator._prefer_native_callable_values = True
    generator._module_source_path = str(fixture)
    text = str(generator.generate())
    main = re.search(r'^define[^\n]*@user_time_struct_sequence_main\([^\n]*\) \{\n(.*?)^\}', text, re.M | re.S).group(1)
    assert len(re.findall(r'call[^\n]*@py_tuple_check', main)) == 1
    assert not re.search(r'call[^\n]*@py_cpy_', text)


def test_unrelated_class_identity_is_not_folded_true():
    body = check_body(emit('class Unrelated:\n    pass\ndef check(value: Unrelated) -> bool:\n    return isinstance(value, tuple)\n'))
    assert re.search(r'call[^\n]*@py_tuple_check', body)
    assert 'icmp ne i1 1, 0' not in body


@pytest.mark.parametrize('phase', ('register', 'copy', 'acquire', 'callback', 'release', 'drop', 'allocation', 'frame_enter', 'frame_leave'))
def test_tuple_identity_runtime_model_preserves_true_and_false(phase):
    from pcc.runtime.py import py_abi_constants as abi
    from test_structseq_slot_owners import StructSeqMemory

    memory = StructSeqMemory(phase)
    caller = memory.invoke_structseq(range(9), ('UTC', 0))
    record = memory.read(caller, 24)
    assert memory.ns['py_tuple_check'](record) == 1
    exact = memory.wrap((1, 2))
    assert memory.ns['py_tuple_check'](exact) == 1
    ordinary = memory.make('unrelated', abi.PY_TYPE_INSTANCE)
    ordinary.fields[abi.PYINSTANCEOBJECT_CLS_OFFSET] = memory.klass('Unrelated', [])
    assert memory.ns['py_tuple_check'](ordinary) == 0
    memory.assert_balanced()
