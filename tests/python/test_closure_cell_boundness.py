"""Unbound closure payloads are distinct from None and obey lexical errors."""
import os
import re
import subprocess

import pytest

from pcc.backend.owned_object_emit import emit_owned_object
from pcc.frontends.python import type_infer
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_lift import parse_and_lift


SEMANTICS = '''value = 'global'
def expect_free(reader):
    try:
        reader()
    except UnboundLocalError:
        raise AssertionError('free read raised owning-local error')
    except NameError as error:
        assert 'free variable' in str(error)
        return
    raise AssertionError('free read returned a value')

def annotation_only():
    value: object
    def read():
        return value
    expect_free(read)
    try:
        value
    except UnboundLocalError as error:
        assert 'local variable' in str(error)
    else:
        raise AssertionError('owning read returned a value')
    value = None
    assert read() is None
    del value
    expect_free(read)
    try:
        del value
    except UnboundLocalError:
        pass
    else:
        raise AssertionError('repeated local delete succeeded')
    value = False
    assert read() is False
    return read

def nonlocal_operations():
    value: object
    def read():
        return value
    def clear():
        nonlocal value
        del value
    def put(new):
        nonlocal value
        value = new
    expect_free(read)
    expect_free(clear)
    put(None)
    assert read() is None
    clear()
    expect_free(read)
    put([1])
    assert read() == [1]
    return read

def pure_lambda():
    value: object
    return lambda: value

def parameter_delete(value):
    def read():
        try:
            return value
        except UnboundLocalError:
            raise AssertionError('parameter free read had wrong error')
        except NameError:
            return 'empty'
    assert read() is None
    del value
    assert read() == 'empty'
    value = None
    assert read() is None

def direct_call(flag):
    value: object
    def read(use):
        if use:
            return value
        return 'unused'
    def catch_inside():
        try:
            return value
        except UnboundLocalError:
            raise AssertionError('wrong child exception')
        except NameError:
            return 'caught-inside'
    assert catch_inside() == 'caught-inside'
    assert read(False) == 'unused'
    try:
        read(True)
    except UnboundLocalError:
        raise AssertionError('direct capture evaluated eagerly')
    except NameError:
        pass
    else:
        raise AssertionError('direct free read returned a value')
    if flag:
        value = None
        assert read(True) is None

def assignment_shapes():
    value: object
    read = lambda: value
    expect_free(read)
    value, other = None, 1
    assert read() is None and other == 1
    value = other = [2]
    assert read() is other
    assert (value := 3) == 3 and read() == 3
    value += 4
    assert read() == 7
    del value
    touched = []
    def rhs():
        touched.append(True)
        return 1
    try:
        value += rhs()
    except UnboundLocalError:
        pass
    else:
        raise AssertionError('unbound augmented assignment succeeded')
    assert touched == []
    for value in []:
        pass
    expect_free(read)
    for value in [None]:
        assert read() is None
    assert read() is None

def delete_target_shapes():
    value = None
    other = None
    read_value = lambda: value
    read_other = lambda: other
    del (value, other)
    expect_free(read_value)
    expect_free(read_other)
    value = None
    other = None
    del [value, other]
    expect_free(read_value)
    expect_free(read_other)
    other = 5
    try:
        del (value, other)
    except UnboundLocalError:
        pass
    else:
        raise AssertionError('nested empty delete succeeded')
    assert read_other() == 5
    value = None
    del ((value,), [other])
    expect_free(read_value)
    expect_free(read_other)

def handler_clear(mode):
    def read():
        return error
    try:
        try:
            raise ValueError('handled')
        except ValueError as error:
            assert str(read()) == 'handled'
            if mode == 1:
                return read
            if mode == 2:
                raise RuntimeError('leaving')
            if mode == 3:
                del error
    except RuntimeError:
        assert mode == 2
    expect_free(read)
    return read

def list_index_errors():
    for items, index in [([], 0), ([None], 1)]:
        try:
            items[index]
        except IndexError:
            pass
        except NameError:
            raise AssertionError('ordinary missing index became cell unbound')
        else:
            raise AssertionError('missing list index returned a payload')
    assert [None][0] is None

def shadow_and_defaults():
    value: object
    read = lambda: value
    expect_free(read)
    try:
        f = lambda arg=value: arg
    except UnboundLocalError:
        pass
    else:
        raise AssertionError('default read was not local')
    value = None
    f = lambda value=23: value
    assert f() == 23
    def global_read():
        global value
        return value
    assert global_read() == 'global'
    assert read() is None

def main():
    assert annotation_only()() is False
    assert nonlocal_operations()() == [1]
    expect_free(pure_lambda())
    parameter_delete(None)
    direct_call(False)
    direct_call(True)
    assignment_shapes()
    delete_target_shapes()
    for mode in range(4):
        expect_free(handler_clear(mode))
    shadow_and_defaults()
    list_index_errors()
    print('CLOSURE_CELL_BOUNDNESS_OK')
main()
'''

FINALIZERS = '''import gc
import weakref
reader = None
events = []
references = []
class Tracked:
    def __init__(self, label):
        self.label = label
        references.append(weakref.ref(self))
    def __del__(self):
        gc.collect()
        try:
            current = reader()
        except UnboundLocalError:
            events.append('wrong-local-error')
        except NameError:
            events.append(self.label + ':unbound')
        else:
            events.append(self.label + ':' + str(current))
        gc.collect()

def exercise():
    global reader
    value: object
    def read():
        return value
    reader = read
    value = Tracked('replace')
    value = None
    gc.collect()
    assert events == ['replace:None']
    assert references[0]() is None
    value = Tracked('delete')
    del value
    gc.collect()
    assert events == ['replace:None', 'delete:unbound']
    assert references[1]() is None
    value = [42]
    gc.collect()
    assert read() == [42]
    value = None
    gc.collect()
    assert read() is None
    del value
    for count in range(4):
        gc.collect()
    try:
        read()
    except NameError:
        pass
    else:
        raise AssertionError('empty cell became bound during collection')
    reader = None

def main():
    exercise()
    gc.collect()
    print('CLOSURE_CELL_FINALIZERS_OK')
main()
'''

ANNOTATION_ORIGINAL = '''value = 'global'
def missing():
    value: object
    def read():
        return value
    try:
        read()
    except NameError:
        return 'unbound'
    return 'returned'
def initialized():
    value: object = None
    def read():
        return value
    return read() is None
def main():
    print(missing())
    print(initialized())
main()
'''

PROGRAMS = {
    'annotation_original': (ANNOTATION_ORIGINAL, 'unbound\nTrue\n'),
    'semantics': (SEMANTICS, 'CLOSURE_CELL_BOUNDNESS_OK\n'),
    'finalizers': (FINALIZERS, 'CLOSURE_CELL_FINALIZERS_OK\n'),
}


def _generate(source, target="x86_64-unknown-linux-gnu"):
    module = type_infer.infer_module(parse_and_lift(source, '<closure-cells>', 'closure_cells'))
    codegen = L1CodeGen(module, ir_scaffold_mode='on')
    codegen._target_triple = target
    codegen._strict_no_libpython = True
    return str(codegen.generate(module))


@pytest.mark.parametrize('name', PROGRAMS)
def test_closure_cell_reference(capsys, name):
    source, expected = PROGRAMS[name]
    exec(source, {})
    assert capsys.readouterr().out == expected


def test_cell_read_uses_lexical_error_and_null_payload():
    text = _generate(SEMANTICS)
    assert 'cell.bound.error' in text
    assert re.search(r'@py_list_append\(ptr [^,]+, ptr null\)', text)
    assert re.search(r'@py_exc_new\(i64 63,', text)  # UnboundLocalError
    assert ''.join('\\%02X' % byte for byte in b'cannot access free variable') in text


@pytest.mark.parametrize('name', PROGRAMS)
@pytest.mark.parametrize('target', ('x86_64-unknown-linux-gnu', 'aarch64-unknown-linux-gnu'))
def test_closure_cell_shapes_emit_owned_objects(name, target):
    assert emit_owned_object(_generate(PROGRAMS[name][0], target), target)


@pytest.mark.integration
@pytest.mark.parametrize('name', PROGRAMS)
def test_closure_cell_shapes_execute_all_collectors(
    tmp_path, monkeypatch, pcc_runtime_archive, python_program_compiler, name,
):
    source, expected = PROGRAMS[name]
    path = tmp_path / (name + '.py')
    path.write_text(source)
    output = tmp_path / name
    monkeypatch.setenv('PCC_PYTHON_IR_PASSES', 'off')
    python_program_compiler(str(path), str(output), backend='self', libpython_mode='off',
                            ir_scaffold_mode='on', runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=30,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend),
                                         PCC_GC_REFCOUNT_PROVENANCE_PROBE='2', PATH='/nonexistent'))
        assert (result.returncode, result.stdout, result.stderr) == (0, expected, ''), (
            backend, result.returncode, result.stdout, result.stderr)


def test_cell_markers_are_unspellable_and_roundtrip_typed_ast_wire():
    from pcc.frontends.python.codegen.hoist_boxing import CELL_CAPTURE, CELL_READ, CELL_UNBOUND, _cell_read, _cell_unbound, cell_capture_key
    from pcc.frontends.python.pipeline_ast_wire import _py_ast_from_wire, _py_ast_to_wire
    from pcc.frontends.python.py_ast import SourceSpan

    assert not CELL_READ.isidentifier() and not CELL_UNBOUND.isidentifier()
    assert not CELL_CAPTURE.isidentifier() and not cell_capture_key('value').isidentifier()
    span = SourceSpan(file='<closure-cell>', line=1, col=0, end_line=1, end_col=1)
    for operation in (_cell_unbound(span), _cell_read('value', span, free=True)):
        assert _py_ast_from_wire(_py_ast_to_wire(operation)) == operation


def test_cell_lowering_uses_registered_host_calls(tmp_path):
    import importlib.util
    from pathlib import Path
    from pcc.frontends.python.pipeline import compile_contextual_per_module_fallback_counts

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location('cell_host_probe', root / 'scripts/probe_stage1_closure.py')
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    module = 'pcc.frontends.python.codegen.closure_cell_lowering'
    # Include the real host and its mixin exports. A helper-only import
    # closure has no L1CodeGen class metadata and legitimately uses dynamic
    # dispatch, so it cannot qualify the native compiler's host-call ABI.
    sources, modules = [], []
    for leaf in ('closure_cell_lowering.py', 'layer1.py', 'layer1_mixins.py'):
        paths, names = probe._tightened_closure(str(root / 'pcc/frontends/python/codegen' / leaf))
        for path, name in zip(paths, names):
            if name not in modules:
                sources.append(path)
                modules.append(name)
    assert module in modules
    counts = compile_contextual_per_module_fallback_counts(
        sources, modules, {module}, ir_scaffold_mode='on', strict_no_libpython=True,
        emit_ir_dir=str(tmp_path),
    )
    assert counts == {module: 0}
    text = (tmp_path / 'pcc_frontends_python_codegen_closure_cell_lowering.ll').read_text()
    assert 'strict.nolib.stub:' not in text
    for method in ('_owned_release_needed', '_extern_enter_root', '_extern_take_root',
                   '_emit_builtin_exception_and_branch'):
        indirect = [line for line in text.splitlines()
                    if '@py_obj_load_method(' in line and method in line]
        assert not indirect, (method, indirect)
