"""Owned descriptor values must exist in the actual compiled os namespace."""
from pathlib import Path
import re
import shutil

import pytest

from pcc.frontends.python.pipeline import compile_python_multi
from pcc.frontends.python.codegen.native_os import (
    NATIVE_OS_OWNED_CALLABLES,
    NATIVE_OS_DESCRIPTOR_CONSTANTS,
    native_os_descriptor_constant,
)

ROOT=Path(__file__).resolve().parents[2]


def emit(tmp_path,monkeypatch,program,*,port=None,target=None):
    monkeypatch.setenv('PYTHONPATH',str(ROOT))
    monkeypatch.setenv('PCC_PY_FRONTEND_JOBS','1')
    monkeypatch.setenv('PCC_WORKER_TREE_BUDGET_BYTES','536870912')
    monkeypatch.setenv('PCC_PYTHON_IR_PASSES','off')
    source=tmp_path/'consumer.py';source.write_text(program)
    output=tmp_path/'consumer.ll';profile={}
    compile_python_multi([str(source),str(port or ROOT/'pcc/stdlib/os.py')],str(output),
        module_names=['consumer','os'],entry_module='consumer',backend='self',
        libpython_mode='off',ir_scaffold_mode='on',emit_llvm_only=True,
        target_triple=target,profile=profile)
    # The two supplied modules can discover further native dependencies
    # (for example os.fsync imports warnings). Count the input contract and
    # verify that every admitted module has one emitted initializer.
    assert profile['counters']['multi_input_files'] == 2
    text = output.read_text()
    initializers = re.findall(r'^define [^\n]*@_pcc_py_module_top_([^ (]+)\(', text, re.M)
    assert initializers.count('os') == 1
    assert len(re.findall(r'^define [^\n]*@main\(', text, re.M)) == 1
    assert len(initializers) == len(set(initializers)) == profile['counters']['multi_files'] - 1
    assert profile['counters']['multi_ir_modules'] == profile['counters']['multi_files']
    return text


def body(text,name):
    match=re.search(r'^define [^\n]*@'+re.escape(name)+r'\([^\n]*\) \{\n(.*?)^\}',text,re.M|re.S)
    assert match,name
    return match.group(1)


@pytest.mark.parametrize('target,platform',[(None,'linux'),('arm64-apple-darwin','darwin')])
def test_real_os_initializer_publishes_owned_callables_and_target_constants(tmp_path,monkeypatch,target,platform):
    text=emit(tmp_path,monkeypatch,'''import os
from os import open as create, fdopen as adopt, close as finish, O_CREAT, O_EXCL
def probe(path):
    return create(path, O_CREAT | O_EXCL), os.open, adopt, finish
''',target=target)
    initializer=body(text,'_pcc_py_module_top_os')
    for member,producer in NATIVE_OS_OWNED_CALLABLES:
        assert len(re.findall(r'\bcall ptr \(\) @'+producer+r'\(',initializer))==1
        assert re.search(r'(%[\w.]+) = call ptr \(\) @'+producer+r'\(\)\n  store ptr \1, ptr %owned.module.member\.'+member,initializer)
        assert re.search(r'call i64 \(ptr, ptr, ptr\) @py_module_attr_set[^\n]*@.pyattr\.'+member+r'[, ]',initializer)
    for member in NATIVE_OS_DESCRIPTOR_CONSTANTS:
        assert native_os_descriptor_constant(member,platform) is not None
        assert re.search(r'call i64 \(ptr, ptr, ptr\) @py_module_attr_set[^\n]*@.pyattr\.'+member+r'[, ]',initializer)
    assert 'pcc_gc_foreign_lease_acquire' in initializer and 'pcc_gc_foreign_lease_release' in initializer
    assert 'strict.nolib.stub' not in text and not re.search(r'\bcall [^\n]*@py_cpy_',text)


def test_compiled_context_value_and_getattr_reads_use_live_namespace(tmp_path,monkeypatch):
    text=emit(tmp_path,monkeypatch,'''import os
from os import open as imported
def probe():
    first = os.open
    second = getattr(os, 'open')
    return first, second, imported
''')
    probe=body(text,'user_consumer_probe')
    assert '@py_module_attr_get' in probe
    assert not re.search(r'\bcall [^\n]*@py_os_open_function\(',probe)
    assert not re.search(r'\bcall [^\n]*@py_cpy_',text)


def test_user_module_named_os_keeps_its_own_definitions(tmp_path,monkeypatch):
    port=tmp_path/'os.py';port.write_text('def open():\n    return 17\n')
    text=emit(tmp_path,monkeypatch,'import os\ndef probe():\n    return os.open()\n',port=port)
    initializer=body(text,'_pcc_py_module_top_os')
    assert not re.search(r'\bcall [^\n]*@py_os_open_function\(',initializer)
    assert '@py_func_new_named' in initializer
    assert 'strict.nolib.stub' not in text and not re.search(r'\bcall [^\n]*@py_cpy_',text)


def test_module_initializer_helper_is_in_native_method_contract():
    from pcc.frontends.python.codegen.host_contract import L1_CODEGEN_HOST_METHODS
    from pcc.frontends.python.codegen._l1_codegen_static_methods import L1_CODEGEN_STATIC_METHODS
    assert '_emit_owned_stdlib_module_bindings' in L1_CODEGEN_HOST_METHODS
    assert any(row['name']=='_emit_owned_stdlib_module_bindings' for row in L1_CODEGEN_STATIC_METHODS)


def test_mkdir_alias_attribute_and_keyword_calls_use_callable_binding(tmp_path, monkeypatch):
    text = emit(tmp_path, monkeypatch, '''import os
from os import mkdir as create
def invoke(provider, path, mode, directory):
    return provider(path, mode=mode, dir_fd=directory)
def probe(path):
    provider = os.mkdir
    create(path, mode=448)
    os.mkdir(path, dir_fd=None)
    return invoke(provider, path, 511, None)
''')
    probe = body(text, 'user_consumer_probe')
    assert bool('@py_obj_call_slots' in probe), 'mkdir must retain the normal callable binder'
    assert bool('@py_module_attr_get' in probe), 'mkdir must read its published module owner'
    assert not bool('strict.nolib.stub' in text), 'mkdir produced a strict stub'
    assert not bool(re.search(r'\bcall [^\n]*@py_cpy_', text)), 'mkdir used a CPython bridge'


@pytest.mark.parametrize('setting', ['PCC_PY_STDLIB_ROOT', 'PCC_REPO_ROOT'])
def test_configured_native_provider_root_publishes_owned_values(tmp_path, monkeypatch, setting):
    configured = tmp_path / 'configured'
    port = configured / 'pcc/stdlib/os.py'
    port.parent.mkdir(parents=True)
    (port.parent / '__init__.py').write_text('')
    shutil.copy2(ROOT / 'pcc/stdlib/os.py', port)
    monkeypatch.setenv(setting, str(configured))
    text = emit(tmp_path, monkeypatch, 'import os\ndef probe():\n    return os.open, os.mkdir\n', port=port)
    initializer = body(text, '_pcc_py_module_top_os')
    assert bool('@py_os_open_function' in initializer), 'configured provider lost open'
    assert bool('@py_os_mkdir_function' in initializer), 'configured provider lost mkdir'


def test_codegen_uses_retained_source_identity_with_synthetic_file(monkeypatch):
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.codegen import module_lifecycle_lowering
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.type_infer import infer_module
    monkeypatch.setattr(module_lifecycle_lowering, '__file__', '<compiled-bootstrap>')
    module = infer_module(parse_and_lift('', '<retained-os>', 'os'))
    codegen = L1CodeGen(module, ir_scaffold_mode='on')
    codegen._strict_no_libpython = True
    codegen._skip_program_main = True
    codegen._module_source_path = str(ROOT / 'pcc/stdlib/os.py')
    text = str(codegen.generate(module))
    assert bool('@py_os_open_function' in body(text, '_pcc_py_module_top_os'))
    assert not bool('strict.nolib.stub' in text)
