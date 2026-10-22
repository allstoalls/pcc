"""Local classes retain the enclosing cell, including its unbound state."""
import os
import subprocess

import pytest

from pcc.backend.owned_object_emit import emit_owned_object
from tests.python.test_closure_cell_boundness import _generate


def test_owned_capture_root_handoffs_consume_each_source_once():
    """The shared take helper, not each caller, consumes an owned source."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "pcc/frontends/python/codegen"
    # A local class method is a closure: the class statement publishes its
    # function object and each enclosing cell enters the captures tuple from
    # one slot-call operand root, which only the shared root release retires.
    tree = ast.parse((root / "user_function_lowering.py").read_text())
    builder = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                   and node.name == "_emit_rooted_native_callable")
    loop = next(node for node in ast.walk(builder) if isinstance(node, ast.For)
                and isinstance(node.iter, ast.Call)
                and isinstance(node.iter.func, ast.Name)
                and node.iter.func.id == "enumerate"
                and isinstance(node.iter.args[0], ast.Name)
                and node.iter.args[0].id == "capture_exprs")
    calls = [node for node in ast.walk(loop) if isinstance(node, ast.Call)
             and isinstance(node.func, ast.Attribute)]
    produced = [node for node in calls if node.func.attr == "_emit_slot_call_operand"]
    retired = [node for node in calls if node.func.attr == "_release_slot_call_roots"]
    released = [node for node in calls if node.func.attr == "_gc_release"]
    assert len(produced) == 1 and len(retired) == 1 and released == []
    # The removed instance-attribute capture handoff stays removed.
    call_lowering = (root / "call_expression_lowering.py").read_text()
    assert "class.captures.instance" not in call_lowering
    assert "cell_capture_key" not in call_lowering

    # Execute the production transfer helper with explicit reference counts.
    from pcc.frontends.python.codegen.extern_lowering import ExternScaffoldMixin

    class Value:
        references = 2  # original expression owner + root's retained owner

    value = Value()

    class Builder:
        def call(self, _function, _args, **_kwargs):
            return value

    class Host:
        current_func_def = object()
        _runtime_threads_enabled = True
        builder = Builder()
        runtime = {"pcc_gc_take_pinned_slot": object()}

        def _extern_load_root(self, _root):
            return value

        def _gc_release(self, obj, **_kwargs):
            obj.references -= 1
            assert obj.references > 0

        def _extern_repin_root(self, _root):
            assert value.references == 1

        def _as_gc_ptr(self, slot):
            return slot

        def _fresh(self, label):
            return label

    assert ExternScaffoldMixin._extern_take_root(Host(), ([value], True, 0)) is value
    assert value.references == 1


PROGRAM = '''import gc
value = 'module'
active_view = None
finalizer_events = []
class Tracked:
    def __del__(self):
        gc.collect()
        try:
            current = active_view.read()
        except UnboundLocalError:
            finalizer_events.append('wrong-local-error')
        except NameError:
            finalizer_events.append('unbound')
        else:
            finalizer_events.append(current)
        gc.collect()
def expect_free(instance):
    try:
        instance.read()
    except UnboundLocalError:
        raise AssertionError('method free read raised owning-local error')
    except NameError:
        return
    raise AssertionError('method read an empty cell')

def class_only():
    value: object
    class Reader:
        value = 'class'
        def read(self):
            return value
        def local(self):
            value = 'method'
            return value
        def parameter(self, value):
            return value
        def global_read(self):
            global value
            return value
    instance = Reader()
    expect_free(instance)
    value = None
    assert instance.read() is None
    value = [42]
    assert instance.read() == [42]
    assert Reader.value == 'class'
    assert instance.local() == 'method'
    assert instance.parameter(None) is None
    assert instance.global_read() == 'module'
    del value
    expect_free(instance)
    value = None
    assert instance.read() is None
    del value
    return instance

def shared_with_sibling():
    value = None
    def sibling():
        return value
    class Reader:
        def read(self):
            return value
    instance = Reader()
    assert sibling() is None
    assert instance.read() is None
    def replace(new):
        nonlocal value
        value = new
    def clear():
        nonlocal value
        del value
    return sibling, instance, replace, clear

def guarded_capture():
    events = []
    value = None
    class Collision:
        def __get__(self, instance, owner):
            events.append('descriptor-get')
            return 'spoof'
        def __set__(self, instance, replacement):
            events.append('descriptor-set')
    class Shielded:
        __pcc_cap_value = Collision()
        marker = 42
        def __getattribute__(self, name):
            if name.startswith('.pcc.capture.') or name.endswith('__pcc_cap_value'):
                events.append('user-get')
                return 'spoof'
            return object.__getattribute__(self, name)
        def __setattr__(self, name, replacement):
            if name.startswith('.pcc.capture.') or name.endswith('__pcc_cap_value'):
                events.append('user-set')
            object.__setattr__(self, name, replacement)
        def read(self):
            return value
        @classmethod
        def ordinary_class_method(cls):
            return cls.marker
        @staticmethod
        def ordinary_static_method(argument):
            return argument
    instance = Shielded()
    assert instance.read() is None
    value = [23]
    assert instance.read() == [23]
    assert events == []
    assert instance.ordinary_class_method() == 42
    assert instance.ordinary_static_method(None) is None
    del value
    expect_free(instance)
    assert events == []

def class_finalizers():
    global active_view
    value = None
    class View:
        def read(self):
            return value
    active_view = View()
    value = Tracked()
    value = [42]
    gc.collect()
    assert finalizer_events == [[42]]
    value = Tracked()
    del value
    gc.collect()
    assert finalizer_events == [[42], 'unbound']
    active_view = None
    gc.collect()

def main():
    expect_free(class_only())
    guarded_capture()
    class_finalizers()
    sibling, instance, replace, clear = shared_with_sibling()
    assert instance.read() is sibling()
    replace([1, 2])
    assert instance.read() is sibling()
    assert instance.read() == [1, 2]
    clear()
    expect_free(instance)
    replace(None)
    assert instance.read() is None
    print('CLASS_CLOSURE_CELLS_OK')
main()
'''


def test_class_cell_reference(capsys):
    exec(PROGRAM, {})
    assert capsys.readouterr().out == 'CLASS_CLOSURE_CELLS_OK\n'


@pytest.mark.parametrize('target', ('x86_64-unknown-linux-gnu', 'aarch64-unknown-linux-gnu'))
def test_class_cell_shapes_emit_owned_objects(target):
    assert emit_owned_object(_generate(PROGRAM, target), target)


@pytest.mark.integration
def test_class_cell_shapes_execute_all_collectors(
    tmp_path, monkeypatch, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / 'class_cells.py'
    source.write_text(PROGRAM)
    output = tmp_path / 'class_cells'
    monkeypatch.setenv('PCC_PYTHON_IR_PASSES', 'off')
    python_program_compiler(str(source), str(output), backend='self', libpython_mode='off',
                            ir_scaffold_mode='on', runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        result = subprocess.run([str(output)], capture_output=True, text=True, timeout=30,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend),
                                         PCC_GC_REFCOUNT_PROVENANCE_PROBE='2', PATH='/nonexistent'))
        assert (result.returncode, result.stdout, result.stderr) == (0, 'CLASS_CLOSURE_CELLS_OK\n', ''), (
            backend, result.returncode, result.stdout, result.stderr)
