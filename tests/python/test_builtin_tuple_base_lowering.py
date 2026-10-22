"""Prove builtin tuple is a physical native base; no runtime or native gate."""
import ast
from pathlib import Path
import re

import pcc
import pytest

from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.type_infer import infer_module


def _emit(source, tmp_path):
    module = infer_module(parse_and_lift(source, 'tuple_base.py', 'tuple_base'))
    generator = L1CodeGen(module, emit_cpy_main_exitcode=False, ir_scaffold_mode='on')
    generator._strict_no_libpython = True
    text = str(generator.generate())
    (tmp_path / 'tuple_base.ll').write_text(text)
    assert not re.search(r'call[^\n]*@py_cpy_', text)
    return text


def _class_calls(text, name):
    return re.findall(r'^\s*%class\.' + re.escape(name) + r'\.[^\n]*@py_class_new\([^\n]*', text, re.M)


def _assert_physical_tuple_base(text, name):
    calls = _class_calls(text, name)
    assert calls
    # Both executable and imported module initializers must carry the base.
    assert all(re.search(r', ptr %[^,]+, i32 1, ptr ', call) for call in calls), calls
    providers = re.findall(r'^\s*(%[^ ]+) = call[^\n]*@py_builtin_type_for_tag\(i64 7\)', text, re.M)
    assert len(providers) == len(calls)
    for value in providers:
        assert re.search(r'store ptr ' + re.escape(value) + r', ptr %\.baseslot\.0\.', text)
    assert len(re.findall(r'\.bases\.' + re.escape(name) + r'\.[^\n]* = alloca \[1 x ptr\]', text)) == len(calls)


@pytest.mark.parametrize('marker', [False, True])
def test_tuple_base_is_published_to_every_class_initializer(tmp_path, marker):
    source = 'class Record(tuple):\n    __slots__ = ()\n'
    if marker:
        source += '    __pcc_struct_sequence__ = True\n'
    text = _emit(source, tmp_path)
    _assert_physical_tuple_base(text, 'Record')
    seals = re.findall(r'call[^\n]*@py_class_mark_structseq', text)
    assert len(seals) == (len(_class_calls(text, 'Record')) if marker else 0)


def test_original_time_struct_time_publishes_tuple_base(tmp_path):
    path = Path(pcc.__file__).parent / 'stdlib/time.py'
    source = path.read_text()
    node = next(node for node in ast.parse(source).body
                if isinstance(node, ast.ClassDef) and node.name == 'struct_time')
    original = ast.get_source_segment(source, node)
    text = _emit(original, tmp_path)
    _assert_physical_tuple_base(text, 'struct_time')
    assert len(re.findall(r'call[^\n]*@py_class_mark_structseq', text)) == len(_class_calls(text, 'struct_time'))


def test_user_class_named_tuple_remains_the_actual_base(tmp_path):
    text = _emit('class tuple:\n    pass\nclass Child(tuple):\n    pass\n', tmp_path)
    assert not re.search(r'call[^\n]*@py_builtin_type_for_tag\(i64 7\)', text)
    calls = _class_calls(text, 'Child')
    assert calls and all(re.search(r', ptr %[^,]+, i32 1, ptr ', call) for call in calls)
    assert re.search(r'load ptr, ptr @\.class\.tuple_base\.tuple', text)


def test_non_tuple_structseq_marker_does_not_synthesize_a_base(tmp_path):
    text = _emit('class Invalid:\n    __slots__ = ()\n    __pcc_struct_sequence__ = True\n', tmp_path)
    assert not re.search(r'call[^\n]*@py_builtin_type_for_tag\(i64 7\)', text)
    calls = _class_calls(text, 'Invalid')
    assert calls and all(', ptr null, i32 0, ptr null, i32 0)' in call for call in calls)
    assert len(re.findall(r'call[^\n]*@py_class_mark_structseq', text)) == len(calls)


def test_existing_string_base_keeps_its_provider(tmp_path):
    text = _emit('class Text(str):\n    pass\n', tmp_path)
    providers = re.findall(r'call[^\n]*@py_builtin_type_for_tag\(i64 4\)', text)
    assert len(providers) == len(_class_calls(text, 'Text')) > 0


@pytest.mark.parametrize('binding', [
    'tuple = 1',
    'tuple = object',
    'def tuple():\n    return object',
    'from builtins import list as tuple',
])
def test_rebound_tuple_name_is_an_explicit_boundary(tmp_path, binding):
    from pcc.frontends.python.codegen.class_gen import ClassLoweringError

    with pytest.raises(ClassLoweringError, match="base 'tuple' is rebound"):
        _emit(binding + '\nclass Child(tuple):\n    pass\n', tmp_path)


def test_imported_class_named_tuple_keeps_its_registered_base(tmp_path):
    from pcc.frontends.python.pipeline_context import build_closed_world_context

    provider = tmp_path / 'base_provider.py'
    entry = tmp_path / 'entry.py'
    provider.write_text('class Parent:\n    pass\n')
    entry.write_text('from base_provider import Parent as tuple\nclass Child(tuple):\n    pass\n')
    modules, exports, _ = build_closed_world_context(
        [str(provider), str(entry)], ['base_provider', 'entry'])
    module = next(module for module in modules if module.name == 'entry')
    typed = infer_module(module, external_exports={key: value for key, value in exports.items() if key != 'entry'})
    generator = L1CodeGen(typed, emit_cpy_main_exitcode=False, ir_scaffold_mode='on')
    generator._strict_no_libpython = True
    generator._native_module_exports = exports
    text = str(generator.generate())
    (tmp_path / 'entry.ll').write_text(text)
    assert not re.search(r'call[^\n]*@py_cpy_', text)
    assert not re.search(r'call[^\n]*@py_builtin_type_for_tag\(i64 7\)', text)
    calls = _class_calls(text, 'Child')
    assert calls and all(re.search(r', ptr %[^,]+, i32 1, ptr ', call) for call in calls)
    assert re.search(r'load ptr, ptr @\.class\.base_provider\.Parent', text)


def test_tuple_structseq_reaches_owned_object(tmp_path, monkeypatch):
    from pcc.backend.owned_object_emit import emit_owned_object
    from pcc.frontends.python.pipeline import compile_python

    monkeypatch.setenv('PCC_PYTHON_IR_PASSES', 'off')
    source = tmp_path / 'tuple_base.py'
    source.write_text('class Record(tuple):\n    __slots__ = ()\n    __pcc_struct_sequence__ = True\ndef main():\n    print("entered")\nmain()\n')
    output = tmp_path / 'tuple_base.ll'
    compile_python(str(source), str(output), emit_llvm_only=True,
                   backend='self', libpython_mode='off', ir_scaffold_mode='on',
                   target_triple='x86_64-unknown-linux-gnu')
    text = output.read_text()
    _assert_physical_tuple_base(text, 'Record')
    assert not re.search(r'call[^\n]*@py_cpy_', text)
    payload = emit_owned_object(text, 'x86_64-unknown-linux-gnu')
    assert len(payload) > 64 and payload[:4] == b'\x7fELF'
    (tmp_path / 'tuple_base.o').write_bytes(payload)


def test_structseq_marker_still_rejects_an_ordinary_class():
    from test_structseq_slot_owners import StructSeqMemory

    memory = StructSeqMemory()
    ordinary = memory.klass('ordinary', [])
    ordinary.fields[12] = 2
    memory.ns['py_class_mark_structseq'](ordinary)
    assert memory.error.value == (3, 'struct sequence requires a tuple subtype')
    assert not ordinary.fields[12] & memory.ns['_STRUCTSEQ_CLASS_FLAG']
