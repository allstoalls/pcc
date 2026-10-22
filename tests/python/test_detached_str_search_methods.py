"""First-class str search methods: CPython, strict IR, and admitted five-GC gates."""
from __future__ import annotations
import ast
from pathlib import Path
import re
import pytest
from tests.python.owned_regression_support import (
    assert_owned_program, assert_reference_program, explicit_owned_runtime,
)
from tests.python.test_shared_call_binding import _emit
from tests.owned_ir_validation import verify_ir_text

ROOT = Path(__file__).resolve().parents[2]
PROGRAMS = {
    'search': r'''
import gc
events = []
class Bound:
    def __init__(self, name, value):
        self.name = name
        self.value = value
    def __index__(self):
        events.append(self.name)
        gc.collect()
        return self.value
class Broken:
    def __index__(self):
        events.append('broken')
        gc.collect()
        raise ValueError('bound failed')
def operand(name, value):
    events.append(name)
    gc.collect()
    return value
def main():
    text = 'héllo🙂'
    start = text.startswith
    end = getattr(text, 'endswith')
    count = text.count
    assert start.__self__ is text and end.__self__ is text and count.__self__ is text
    assert start.__name__ == 'startswith' and end.__name__ == 'endswith'
    del text
    gc.collect()
    assert start('hé') and end('🙂') and count('l') == 2
    assert start(('no', 'é'), 1, 2)
    assert end(('no', 'é'), 0, 2)
    assert start(('h', 42)) and end(('🙂', 42))
    assert not start(()) and not end(())
    assert start('', 6) and not start('', 7) and not end('', 7)
    assert not start('', 2, 1) and not end('', 2, 1)
    assert start('h', None, None) and end('🙂', None, None)
    assert not start('', 10 ** 100)
    assert end('🙂', -(10 ** 100), 10 ** 100)
    assert count('l', -(10 ** 100), 10 ** 100) == 2
    assert start(operand('prefix', 'é'), operand('start', Bound('index-start', 1)), operand('end', Bound('index-end', 2)))
    assert events == ['prefix', 'start', 'end', 'index-start', 'index-end']
    events.clear()
    try:
        start(42, Bound('start', 0), Bound('end', 6))
    except TypeError:
        pass
    else:
        raise AssertionError('invalid prefix accepted')
    assert events == ['start', 'end']
    events.clear()
    try:
        count(42, Bound('not called', 0))
    except TypeError:
        pass
    else:
        raise AssertionError('invalid count argument accepted')
    assert events == []
    try:
        end('x', Broken(), Bound('not called', 6))
    except ValueError as error:
        assert str(error) == 'bound failed'
    else:
        raise AssertionError('index exception lost')
    assert events == ['broken']
    for method in (start, end, count):
        for args in ((), ('h', 0, 1, 2)):
            try:
                method(*args)
            except TypeError:
                pass
            else:
                raise AssertionError('arity error lost')
        try:
            method('h', start=0)
        except TypeError:
            pass
        else:
            raise AssertionError('keyword accepted')
    for prefix in (42, b'h', (42,), (('h',),), ('no', 42)):
        try:
            start(prefix)
        except TypeError:
            pass
        else:
            raise AssertionError('prefix type error lost')
    canonical = str.startswith
    alias = str
    assert canonical is str.startswith
    assert alias.startswith('hé', 'é', 1)
    assert canonical('hé', 'é', 1)
    assert str.startswith('hé', 'é', 1)
    assert str.endswith('hé', 'é')
    assert str.count('hé', 'é') == 1
    print('DETACHED_STR_OK')
main()
''',
    'subclass': r'''
import gc
events = []
class Text(str):
    def __del__(self):
        events.append('retired')
class Prefix(str):
    def __str__(self):
        raise AssertionError('prefix override must not run')
class Override(str):
    def startswith(self, value):
        gc.collect()
        return 'overridden:' + value
class Hook(str):
    def __getattribute__(self, name):
        if name == 'endswith':
            events.append('lookup')
        return object.__getattribute__(self, name)
def main():
    value = Text('héllo')
    ident = id(value)
    method = value.startswith
    suffix = value.endswith
    counter = value.count
    del value
    gc.collect()
    assert events == []
    assert id(method.__self__) == ident
    assert method(Prefix('é'), 1, 2)
    assert suffix(Prefix('llo')) and counter(Prefix('l')) == 2
    assert str.startswith(method.__self__, 'h')
    assert str.endswith(method.__self__, 'o')
    overridden = Override('abc').startswith
    assert overridden('x') == 'overridden:x'
    hook = Hook('abc')
    hooked = hook.endswith
    assert events == ['lookup']
    assert hooked('c')
    del method
    del suffix
    gc.collect()
    assert events == ['lookup']
    del counter
    gc.collect()
    assert events == ['lookup', 'retired']
    print('DETACHED_STR_OK')
main()
''',
}

@pytest.mark.parametrize('case', PROGRAMS)
def test_detached_search_reference(case, tmp_path):
    assert_reference_program(PROGRAMS[case], 'DETACHED_STR_OK\n', tmp_path)

@pytest.mark.parametrize('case', PROGRAMS)
def test_detached_search_owned_ir(case):
    text = _emit(PROGRAMS[case])
    verify_ir_text(text)
    assert '@py_obj_getattr(' in text and '@py_obj_call_slots(' in text
    has_stub = 'strict.nolib.stub' in text
    assert not has_stub, 'strict runtime stub emitted'
    assert not re.search(r'\bcall\b[^\n]*@py_cpy_', text)

@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
@pytest.mark.parametrize('case', PROGRAMS)
def test_detached_search_native_five_gc(case, python_program_compiler, request,
                                      explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(PROGRAMS[case], 'DETACHED_STR_OK\n', tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime, capfd)


def test_string_descriptor_runtime_strict_ir(tmp_path):
    from pcc.frontends.python.pipeline import compile_python
    source = ROOT / 'pcc/runtime/py/py_class.py'
    tree = ast.parse(source.read_text())
    functions = {node.name:node for node in tree.body if isinstance(node,ast.FunctionDef)}
    selected = {name for name in functions if name.startswith(('_str_method_', '_str_type_'))}
    selected.update(('py_str_getattr','py_str_type_new','_special_bind_attribute',
                     'py_instance_bind_method','_instance_bind_method_body','_instance_bound_method_entry'))
    # Preserve the actual changed body and private-helper closure. Other public
    # ABI calls are declared with the same signatures and resolved by the real
    # runtime in the later native gate; this test emits and verifies IR only.
    exports = {}
    for name,node in functions.items():
        for decorator in node.decorator_list:
            if isinstance(decorator,ast.Call) and isinstance(decorator.func,ast.Name) and decorator.func.id=='c_abi_export':
                exports[name] = decorator.args[0].value
    aliases = {
        'py_class_new':'extern("py_class_new", (c_ptr, c_ptr, c_int32, c_ptr, c_int32), c_ptr)',
        'py_class_add_method':'extern("py_class_add_method", (c_ptr, c_ptr, c_ptr), c_void)',
        'py_class_write_namespace_slots':'extern("py_class_write_namespace_slots", (c_ptr, c_ptr, c_ptr, c_int64), c_int64)',
        'py_obj_special_call_slots':'extern("py_obj_special_call_slots", (c_ptr, c_ptr, c_ptr, c_ptr, c_ptr, c_ptr), c_int64)',
        'py_class_is_str_subclass':'extern("py_class_is_str_subclass", (c_ptr,), c_int64)',
    }
    todo=list(selected)
    while todo:
        node=functions[todo.pop()]
        for call in ast.walk(node):
            if isinstance(call,ast.Call) and isinstance(call.func,ast.Name):
                name=call.func.id
                if name in functions and name not in selected and name not in aliases:
                    selected.add(name);todo.append(name)
    tree.body=[n for n in tree.body if not isinstance(n,ast.FunctionDef) or n.name in selected]
    tree.body.extend(ast.parse('\n'.join(name+' = '+value for name,value in aliases.items())).body)
    scoped=tmp_path/'py_runtime_str_descriptor'/'py'/'str_descriptor.py'
    scoped.parent.mkdir(parents=True)
    scoped.write_text(ast.unparse(tree))
    output=tmp_path/'str_descriptor.ll'
    compile_python(str(scoped), str(output), backend='self', libpython_mode='off',
                   emit_llvm_only=True, python_library=True)
    text=output.read_text()
    verify_ir_text(text)
    has_stub = 'strict.nolib.stub' in text
    assert not has_stub, 'strict runtime stub emitted'
    assert not re.search(r'\bcall\b[^\n]*@py_cpy_', text)
    assert re.search(r'^define[^\n]*@py_str_getattr\(ptr[^,]*, ptr[^)]*\)',text,re.M)
    assert re.search(r'^define[^\n]*@py_str_type_new\(\)',text,re.M)
    for symbol in ('py_slice_index_i64','py_str_tailmatch_range','pcc_gc_root_copy_lease',
                   'pcc_gc_foreign_lease_release','pcc_gc_take_pinned_slot'):
        assert re.search(r'\bcall\b[^\n]*@'+symbol+r'\(',text),symbol


@pytest.mark.parametrize('expression', (
    'str.startswith("a")', 'str.endswith("a")', 'str.count("a")',
))
def test_shadowed_str_keeps_ordinary_receiver_lookup(expression):
    source = 'def invoke(str):\n    return '+expression+'\n'
    text = _emit(source)
    verify_ir_text(text)
    has_cpy = bool(re.search(r'\bcall\b[^\n]*@py_cpy_', text))
    assert not has_cpy
    assert '@py_obj_getattr(' in text


def test_full_class_runtime_strict_ir(tmp_path):
    from pcc.frontends.python.pipeline import compile_python
    output=tmp_path/'py_class.ll'
    compile_python(str(ROOT/'pcc/runtime/py/py_class.py'),str(output),
                   backend='self',libpython_mode='off',emit_llvm_only=True,python_library=True)
    text=output.read_text()
    verify_ir_text(text)
    has_stub='strict.nolib.stub' in text
    has_cpy=bool(re.search(r'\bcall\b[^\n]*@py_cpy_', text))
    assert not has_stub and not has_cpy
    assert re.search(r'^define[^\n]*@py_str_getattr\(ptr[^,]*, ptr[^)]*\)',text,re.M)
    assert re.search(r'^define[^\n]*@py_instance_bind_method\(ptr[^,]*, ptr[^,]*, ptr[^)]*\)',text,re.M)


def test_unbound_descriptor_lookup_precedes_operands():
    text = _emit('def operand():\n    return "x"\ndef probe():\n    return str.startswith(operand(), operand())\n')
    body=re.search(r'^define[^\n]*@user_binding_probe\([^\n]*\).*?^}',text,re.M|re.S).group(0)
    lookup=re.search(r'\bcall\b[^\n]*@py_obj_getattr\(',body)
    operands=list(re.finditer(r'\bcall\b[^\n]*@user_binding_operand\(',body))
    assert lookup is not None and len(operands)==2
    assert lookup.start()<operands[0].start()<operands[1].start()
    assert '@py_obj_call_slots(' in body
    assert '@py_str_startswith(' not in body


def test_full_dispatch_runtime_strict_ir(tmp_path):
    from pcc.frontends.python.pipeline import compile_python
    output=tmp_path/'py_obj_ops_dispatch.ll'
    compile_python(str(ROOT/'pcc/runtime/py/py_obj_ops_dispatch.py'),str(output),
                   backend='self',libpython_mode='off',emit_llvm_only=True,python_library=True)
    text=output.read_text()
    verify_ir_text(text)
    has_stub='strict.nolib.stub' in text
    has_cpy=bool(re.search(r'\bcall\b[^\n]*@py_cpy_', text))
    assert not has_stub and not has_cpy
    assert re.search(r'\bcall\b[^\n]*@py_str_getattr\(ptr[^,]*, ptr[^)]*\)',text)
    assert re.search(r'\bcall\b[^\n]*@py_str_type_new\(\)',text)
