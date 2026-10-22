"""Initializer presence is syntax, independent of annotations and None values."""
from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys

import pytest

from pcc.frontends.python import parser
from pcc.frontends.python.pipeline_ast_wire import _py_ast_from_wire, _py_ast_to_wire
from pcc.frontends.python.py_ast import Assign, NoneLit
from pcc.frontends.python.py_lift import parse_and_lift


@pytest.mark.parametrize('native', [False, True])
def test_both_lifts_preserve_initializer_presence(native):
    source = 'declared: object\ninitialized: object = None\nordinary = None\n'
    module = (parse_and_lift(source, 'presence.py', 'presence') if native
              else parser.parse(source, 'presence.py'))
    assert [stmt.has_value for stmt in module.body] == [False, True, True]
    assert all(isinstance(stmt.value, NoneLit) for stmt in module.body)
    assert module.body[0].annotation is not None
    assert _py_ast_from_wire(json.loads(json.dumps(_py_ast_to_wire(module)))) == module


def test_old_assign_wire_remains_a_real_value_store():
    statement = parse_and_lift('value: object = None\n', 'old.py', 'old').body[0]
    wire = _py_ast_to_wire(statement)
    assert wire['fields']['has_value'] is True
    del wire['fields']['has_value']
    recovered = _py_ast_from_wire(wire)
    assert isinstance(recovered, Assign)
    assert recovered.has_value is True
    assert isinstance(recovered.value, NoneLit)
    assert dataclasses.replace(recovered, has_value=False) != recovered


def test_closed_world_exports_do_not_bind_bare_annotations(tmp_path):
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    source = tmp_path / 'annotation_provider.py'
    source.write_text('''from dataclasses import dataclass
absent: object
present: object = None
kept = 17
kept: object
@dataclass
class Record:
    required: object
    optional: object = None
class Bare:
    def __init__(self):
        self.absent: object
''')
    _, exports, _ = build_closed_world_context([str(source)], ['annotation_provider'])
    values = exports['annotation_provider']
    assert 'absent' not in values
    assert values['present']['value_kind'] == 'none'
    assert values['kept']['value'] == 17
    init = next(method for method in values['Record']['methods'] if method['name'] == '__init__')
    assert [(arg['name'], arg['has_default']) for arg in init['call_sig']] == [
        ('self', False), ('required', False), ('optional', True)]
    assert 'absent' not in values['Bare']['field_names']


BINDING_SOURCE = '''module_absent: object
module_none: object = None
module_kept = 17
module_kept: object
shadow = 'global'
class Declared:
    value: object
class Initialized:
    value: object = None
class BareInstance:
    def __init__(self):
        self.value: object

def read(value):
    return value.value

def local():
    shadow: object
    try:
        return shadow
    except UnboundLocalError:
        pass
    initialized: object = None
    assert initialized is None
    kept = 'kept'
    kept: object
    assert kept == 'kept'
    return 'unbound'

def main():
    try:
        print(module_absent)
        raise AssertionError('bare module annotation bound a value')
    except NameError:
        pass
    assert module_none is None
    assert module_kept == 17
    assert local() == 'unbound'
    declared = Declared()
    initialized = Initialized()
    assert not hasattr(Declared, 'value')
    assert not hasattr(declared, 'value')
    assert not hasattr(BareInstance(), 'value')
    try:
        read(declared)
        raise AssertionError('bare class annotation bound a value')
    except AttributeError:
        pass
    assert Initialized.value is None
    assert read(initialized) is None
    initialized.value = 'instance'
    assert read(initialized) == 'instance'
    assert Initialized.value is None
    del initialized.value
    assert read(initialized) is None
    del Initialized.value
    assert not hasattr(initialized, 'value')
    Declared.value = 'late'
    assert read(declared) == 'late'
    del Declared.value
    assert not hasattr(declared, 'value')
    print('ANNOTATION_BINDINGS_OK')
main()
'''

TARGET_SOURCE = '''events = []
class Box:
    pass
box = Box()
items = []
def receiver():
    events.append('receiver')
    return box
def sequence():
    events.append('sequence')
    return items
def key():
    events.append('key')
    return 100
def main():
    receiver().missing: object
    sequence()[key()]: object
    sequence()[key():key():key()]: object
    assert events == ['receiver', 'sequence', 'key', 'sequence', 'key', 'key', 'key']
    assert not hasattr(box, 'missing')
    assert len(items) == 0
    print('ANNOTATION_TARGETS_OK')
main()
'''

DATACLASS_SOURCE = '''from dataclasses import dataclass
@dataclass
class Required:
    value: object
@dataclass
class Defaulted:
    value: object = None
@dataclass
class Mixed:
    required: object
    optional: object = None
def main():
    constructors = [Required, Defaulted]
    outcomes = []
    for constructor in constructors:
        try:
            instance = constructor()
            assert instance.value is None
            outcomes.append('default')
        except TypeError:
            outcomes.append('required')
    assert outcomes == ['required', 'default']
    assert Required('value').value == 'value'
    mixed = Mixed('required')
    assert mixed.required == 'required'
    assert mixed.optional is None
    print('ANNOTATION_DATACLASSES_OK')
main()
'''


FIELD_SOURCE = '''import gc
from dataclasses import dataclass
module_scalar: int = None
class DeclaredField:
    value: object
    def set(self, value):
        self.value = value
class ObjectDefault:
    value: object = None
    def set(self, value):
        self.value = value
class ScalarDefault:
    value: int = None
    def __init__(self):
        self.value = 9
    def set(self, value):
        self.value = value
@dataclass
class ScalarRecord:
    value: int = None
class Descriptor:
    def __get__(self, obj, owner):
        gc.collect()
        return 42
class BrokenDescriptor:
    def __get__(self, obj, owner):
        gc.collect()
        raise LookupError('descriptor')
def read_declared(value: DeclaredField):
    return value.value
def read_object(value: ObjectDefault):
    return value.value
def read_scalar(value: ScalarDefault):
    return value.value
def main():
    scalar: int = None
    assert scalar is None
    assert module_scalar is None
    assert ScalarRecord().value is None
    missing = DeclaredField()
    for trial in range(3):
        assert not hasattr(missing, 'value')
        try:
            read_declared(missing)
            raise AssertionError('missing field did not raise')
        except AttributeError:
            pass
    missing.set('bound')
    assert read_declared(missing) == 'bound'
    del missing.value
    assert not hasattr(missing, 'value')
    value = ObjectDefault()
    for trial in range(3):
        assert hasattr(value, 'value')
        assert read_object(value) is None
    value.set('instance')
    assert read_object(value) == 'instance'
    del value.value
    assert read_object(value) is None
    number = ScalarDefault()
    assert read_scalar(number) == 9
    del number.value
    assert ScalarDefault.value is None
    assert read_scalar(number) is None
    ScalarDefault.value = 23
    assert read_scalar(number) == 23
    ScalarDefault.value = Descriptor()
    assert read_scalar(number) == 42
    ScalarDefault.value = BrokenDescriptor()
    try:
        read_scalar(number)
        raise AssertionError('descriptor error ignored')
    except LookupError as error:
        assert str(error) == 'descriptor'
    del ScalarDefault.value
    assert not hasattr(number, 'value')
    try:
        read_scalar(number)
        raise AssertionError('deleted class fallback survived')
    except AttributeError:
        pass
    print('ANNOTATION_FIELDS_OK')
main()
'''


def test_scalar_none_binding_keeps_annotation_and_dynamic_storage():
    from pcc.frontends.python.py_ast import DynType, IntType, assignment_storage_annotation
    from pcc.frontends.python.type_infer import infer_module
    statement = infer_module(parse_and_lift('value: int = None\n', 'scalar.py', 'scalar')).body[0]
    assert isinstance(statement.annotation, IntType)
    assert isinstance(statement.targets[0].ty, DynType)
    raw = IntType(name='i64')
    assert assignment_storage_annotation(raw, statement.value, True) is raw


DEFAULT_PUBLICATION_SOURCE = '''from dataclasses import dataclass, field
events = []
factory_events = []
class Token:
    pass
def make(label):
    events.append(label)
    return Token()
def make_factory():
    factory_events.append('factory')
    return []
@dataclass
class Shared:
    required: object
    optional: object = None
    token: object = make('token')
    wrapped: object = field(default=make('wrapped'))
    factory: list = field(default_factory=list)
    custom_factory: list = field(default_factory=make_factory)
def main():
    assert events == ['token', 'wrapped']
    assert factory_events == []
    assert not hasattr(Shared, 'required')
    assert not hasattr(Shared, 'factory')
    assert Shared.optional is None
    first = Shared('first')
    second = Shared('second')
    assert first.token is Shared.token
    assert second.token is Shared.token
    assert first.wrapped is Shared.wrapped
    assert second.wrapped is Shared.wrapped
    assert first.factory is not second.factory
    assert first.custom_factory is not second.custom_factory
    original = Shared.token
    Shared.token = Token()
    later = Shared('later')
    assert later.token is original
    del first.token
    assert first.token is Shared.token
    del first.optional
    assert first.optional is None
    assert events == ['token', 'wrapped']
    assert factory_events == ['factory', 'factory', 'factory']
    print('ANNOTATION_DEFAULT_PUBLICATION_OK')
main()
'''


@pytest.mark.parametrize('source, expected', [
    pytest.param(BINDING_SOURCE, 'ANNOTATION_BINDINGS_OK\n', id='bindings'),
    pytest.param(TARGET_SOURCE, 'ANNOTATION_TARGETS_OK\n', id='targets'),
    pytest.param(DATACLASS_SOURCE, 'ANNOTATION_DATACLASSES_OK\n', id='dataclasses'),
    pytest.param(FIELD_SOURCE, 'ANNOTATION_FIELDS_OK\n', id='fields'),
    pytest.param(DEFAULT_PUBLICATION_SOURCE, 'ANNOTATION_DEFAULT_PUBLICATION_OK\n', id='default_publication'),
])
def test_annotation_assignment_native(source, expected, tmp_path, pcc_runtime_archive, python_program_compiler):
    path = tmp_path / 'annotation_program.py'
    output = tmp_path / 'annotation_program'
    path.write_text(source)
    reference = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, timeout=20)
    assert reference.returncode == 0, reference.stderr
    assert reference.stdout == expected
    python_program_compiler(str(path), str(output), backend='self',
                            libpython_mode='off', ir_scaffold_mode='on',
                            runtime_archive=str(pcc_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, (gc, result.stdout, result.stderr)
        assert result.stdout == expected, (gc, result.stdout, result.stderr)
        assert result.stderr == ''


def test_annotation_only_qualified_module_access(tmp_path, pcc_runtime_archive):
    from pcc.frontends.python.pipeline import compile_python_multi
    provider = tmp_path / 'annotation_provider.py'
    provider.write_text('absent: object\npresent: object = None\nkept = 17\nkept: object\n')
    consumer = tmp_path / 'annotation_consumer.py'
    consumer.write_text('''import annotation_provider as provider

def main():
    try:
        print(provider.absent)
        raise AssertionError('annotation exported a value')
    except AttributeError:
        pass
    assert provider.present is None
    assert provider.kept == 17
    print('ANNOTATION_IMPORTS_OK')
main()
''')
    expected = subprocess.run([sys.executable, str(consumer)], capture_output=True, text=True, timeout=20)
    assert expected.returncode == 0, expected.stderr
    assert expected.stdout == 'ANNOTATION_IMPORTS_OK\n'
    output = tmp_path / 'annotation_imports'
    compile_python_multi([str(consumer), str(provider)], str(output),
                         module_names=['annotation_consumer', 'annotation_provider'],
                         entry_module='annotation_consumer', backend='self',
                         libpython_mode='off', ir_scaffold_mode='on',
                         runtime_archive=str(pcc_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, (gc, result.stderr)
        assert result.stdout == expected.stdout, (gc, result.stdout, result.stderr)
        assert result.stderr == ''


@pytest.mark.parametrize('source', [BINDING_SOURCE, TARGET_SOURCE, DATACLASS_SOURCE, FIELD_SOURCE, DEFAULT_PUBLICATION_SOURCE],
                         ids=['bindings', 'targets', 'dataclasses', 'fields', 'default_publication'])
def test_annotation_assignment_owned_object(source, tmp_path):
    from pcc.backend.owned_object_emit import emit_owned_object
    from pcc.frontends.python.pipeline import compile_python
    path = tmp_path / 'annotation_object.py'
    output = tmp_path / 'annotation_object.ll'
    path.write_text(source)
    compile_python(str(path), str(output), backend='self', libpython_mode='off',
                   ir_scaffold_mode='on', emit_llvm_only=True,
                   target_triple='x86_64-unknown-linux-gnu')
    assert emit_owned_object(output.read_text(), 'x86_64-unknown-linux-gnu')[:4] == b'\x7fELF'


def test_bare_raw_annotation_retains_lane_without_binding():
    from pcc.frontends.python.py_ast import IntType
    from pcc.frontends.python.type_infer import infer_module, PyFrontendError
    declaration = infer_module(parse_and_lift('def f():\n    value: i64\n', 'raw.py', 'raw')).body[0].body[0]
    assert not declaration.has_value
    assert isinstance(declaration.targets[0].ty, IntType)
    assert declaration.targets[0].ty.name == 'pcc.i64'
    with pytest.raises(PyFrontendError, match='does not fit pcc.i64'):
        infer_module(parse_and_lift('def f():\n    value: i64\n    value = 9223372036854775808\n    return value\n', 'raw.py', 'raw'))


def test_native_static_ast_mirrors_keep_initializer_presence():
    from pcc.frontends.python.codegen import layer1_support, hoist_analysis, import_lowering, generator_lowering
    from pcc.frontends.python.py_ast_contract import PY_AST_FIELD_NAME_OVERRIDES
    from pcc.frontends.python import py_parse
    statement = parse_and_lift('value: object\n', 'static.py', 'static').body[0]
    expected = PY_AST_FIELD_NAME_OVERRIDES['Assign']
    assert layer1_support._PY_AST_STATIC_CLASS_FIELDS['Assign'] == expected
    assert layer1_support._dataclass_field_names(statement) == expected
    assert hoist_analysis._dataclass_field_names(statement) == expected
    assert import_lowering._dataclass_field_names(statement) == expected
    assert generator_lowering._dataclass_field_names(statement) == expected
    exports = layer1_support._PCC_FRONTEND_STATIC_NATIVE_EXPORTS
    assert exports['pcc.frontends.python.py_parse']['_Assign']['field_names'] == tuple(f.name for f in dataclasses.fields(py_parse._Assign))
    helper = exports['pcc.frontends.python.py_ast']['assignment_storage_annotation']
    assert helper['kind'] == 'function'
    assert [item['name'] for item in helper['call_sig']] == ['annotation', 'value', 'has_value']


def test_dataclass_default_values_publish_once_and_capture_class_names(tmp_path):
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.type_infer import infer_module
    from pcc.frontends.python.py_ast import ClassDef, FuncDef, Name, Call
    module = infer_module(parse_and_lift(DEFAULT_PUBLICATION_SOURCE, 'defaults.py', 'defaults'))
    codegen = L1CodeGen(module, emit_cpy_main_exitcode=False, ir_scaffold_mode='on')
    definition = next(item for item in module.body if isinstance(item, ClassDef) and item.name == 'Shared')
    expanded = codegen.class_lowering._maybe_expand_dataclass(definition)
    assignments = {item.targets[0].ident: item for item in expanded.body if isinstance(item, Assign)}
    assert set(assignments) == {'optional', 'token', 'wrapped'}
    assert isinstance(assignments['optional'].value, NoneLit)
    assert assignments['token'].value.func.ident == 'make'
    assert assignments['wrapped'].value.func.ident == 'make'
    init = next(item for item in expanded.body if isinstance(item, FuncDef) and item.name == '__init__')
    defaults = {arg.name: arg.default for arg in init.args}
    for name in ('optional', 'token', 'wrapped'):
        assert isinstance(defaults[name], Name) and defaults[name].ident == name
    assert isinstance(defaults['factory'], Call)
    from pcc.frontends.python.pipeline_context import build_closed_world_context
    from pcc.frontends.python.pipeline_exports import _native_export_to_wire, _native_export_from_wire
    provider = tmp_path / 'defaults.py'
    provider.write_text(DEFAULT_PUBLICATION_SOURCE)
    _, exports, _ = build_closed_world_context([str(provider)], ['defaults'])
    restored = _native_export_from_wire(json.loads(json.dumps(_native_export_to_wire(exports))))
    init_export = next(item for item in restored['defaults']['Shared']['methods'] if item['name'] == '__init__')
    signature = {item['name']: item for item in init_export['call_sig']}
    for name in ('token', 'wrapped'):
        assert signature[name]['has_default']
        assert signature[name]['default_native_global'] == {'owning_module': 'defaults', 'name': 'Shared', 'attrs': ('token',) if name == 'token' else ('wrapped',)}
    assert signature['factory']['default_factory'] == 'list'


IMPORTED_DEFAULT_CONSUMER = '''import defaults_provider as model
def main():
    first = model.Shared('first')
    second = model.Shared('second')
    assert first.token is model.Shared.token
    assert second.token is first.token
    assert first.wrapped is model.Shared.wrapped
    assert first.factory is not second.factory
    assert model.events == ['token', 'wrapped']
    assert model.factory_events == ['factory', 'factory']
    print('ANNOTATION_IMPORTED_DEFAULTS_OK')
main()
'''


def _write_imported_default_program(tmp_path):
    provider = tmp_path / 'defaults_provider.py'
    provider.write_text(DEFAULT_PUBLICATION_SOURCE.split('def main():', 1)[0])
    consumer = tmp_path / 'defaults_consumer.py'
    consumer.write_text(IMPORTED_DEFAULT_CONSUMER)
    return provider, consumer


def test_imported_dataclass_defaults_reach_owned_objects(tmp_path):
    import re
    from pcc.frontends.python.pipeline import compile_python_multi
    from pcc.backend.owned_object_emit import emit_owned_object
    provider, consumer = _write_imported_default_program(tmp_path)
    output = tmp_path / 'defaults.ll'
    compile_python_multi([str(consumer), str(provider)], str(output),
                         module_names=['defaults_consumer', 'defaults_provider'],
                         entry_module='defaults_consumer', emit_llvm_only=True,
                         backend='self', libpython_mode='off', ir_scaffold_mode='on',
                         target_triple='x86_64-unknown-linux-gnu')
    text = output.read_text()
    caller = re.search(r'define[^\n]*@user_defaults_consumer_main\([^\n]*\) \{(.*?)\n\}', text, re.S).group(1)
    assert '@user_defaults_provider_make(' not in caller
    assert '@user_defaults_provider_Shared___init__(' not in caller
    assert 'name.dynamic.__pcc_dataclass_factory_default' not in caller
    for name in ('_pcc_py_module_init_defaults_provider', '_pcc_py_module_top_defaults_provider'):
        body = re.search(r'define[^\n]*@' + name + r'\([^\n]*\) \{(.*?)\n\}', text, re.S).group(1)
        assert len(re.findall(r'call [^\n]*@user_defaults_provider_make\(', body)) == 2
        assert '@user_defaults_provider_make_factory(' not in body
    parts = re.split(r'^; ---- module: [^\n]* ----\n', text, flags=re.M)
    count = 0
    for module_ir in parts:
        if 'define ' not in module_ir:
            continue
        assert emit_owned_object(module_ir, 'x86_64-unknown-linux-gnu')[:4] == b'\x7fELF'
        count += 1
    assert count == 2


def test_imported_dataclass_defaults_native(tmp_path, pcc_runtime_archive, python_program_compiler):
    provider, consumer = _write_imported_default_program(tmp_path)
    reference = subprocess.run([sys.executable, str(consumer)], capture_output=True,
                               text=True, timeout=20)
    assert reference.returncode == 0, reference.stderr
    assert reference.stdout == 'ANNOTATION_IMPORTED_DEFAULTS_OK\n'
    output = tmp_path / 'imported_defaults'
    python_program_compiler(str(consumer), str(output), backend='self',
                            libpython_mode='off', ir_scaffold_mode='on',
                            runtime_archive=str(pcc_runtime_archive))
    for gc in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(gc)))
        assert result.returncode == 0, (gc, result.stdout, result.stderr)
        assert result.stdout == reference.stdout, (gc, result.stdout, result.stderr)
        assert result.stderr == ''
