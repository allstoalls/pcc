"""First-class positional OS bindings stay rooted and owned in emitted IR."""
import re
import pytest
from tests.python.test_shared_call_binding import _emit, _function

@pytest.mark.parametrize('name,args', [('pwrite', 'fd, data, offset'), ('ftruncate', 'fd, offset')])
@pytest.mark.parametrize('binding', ['module', 'module-alias', 'import-alias', 'value-alias'])
def test_positional_io_binding_stays_owned(name, args, binding, tmp_path):
    imports = {'module': 'import os\n', 'module-alias': 'import os as system\n',
               'import-alias': 'from os import '+name+' as call\n',
               'value-alias': 'import os\ncall = os.'+name+'\n'}
    callee = {'module': 'os.'+name, 'module-alias': 'system.'+name,
              'import-alias': 'call', 'value-alias': 'call'}[binding]
    text = _emit(imports[binding]+'def probe(fd, data, offset):\n    return '+callee+'('+args+')\n')
    (tmp_path / (name+'.ll')).write_text(text)
    assert '@py_obj_call_slots(' in _function(text)
    assert re.search(r'\bcall [^\n]*@py_os_'+name+r'_function\(', text)
    assert not re.search(r'\bcall [^\n]*@py_cpy_', text)
    assert 'strict.nolib.stub' not in text

@pytest.mark.parametrize('name', ['pwrite', 'ftruncate'])
def test_shadowed_positional_receiver_remains_dynamic(name):
    body = _function(_emit('def probe(os, *args):\n    return os.'+name+'(*args)\n'))
    assert '@py_os_'+name+'_function(' not in body
    assert '@py_obj_load_method(' in body


@pytest.mark.parametrize('target', [
    'x86_64-unknown-linux-gnu', 'aarch64-unknown-linux-gnu',
    'arm64-apple-macosx11.0', 'x86_64-pc-windows-msvc',
])
def test_positional_raw_intrinsics_target_ownership(tmp_path, target):
    from pcc.frontends.python.pipeline import compile_python
    from tests.owned_ir_validation import verify_ir_text
    source = tmp_path / 'positional_leaf.py'
    output = tmp_path / 'positional_leaf.ll'
    source.write_text('''from pcc import i64
from pcc.extern import c_abi_export, c_ptr
from pcc.unsafe import pwrite_file, truncate_file
__pcc_freestanding__ = True
@c_abi_export('probe_write')
def probe_write(fd: i64, data: c_ptr, size: i64, offset: i64) -> i64:
    return pwrite_file(fd, data, size, offset)
@c_abi_export('probe_resize')
def probe_resize(fd: i64, size: i64) -> i64:
    return truncate_file(fd, size)
''')
    compile_python(str(source), str(output), emit_llvm_only=True, python_library=True,
                   libpython_mode='off', target_triple=target)
    text = output.read_text()
    verify_ir_text(text)
    assert not re.search(r'\bcall[^\n]*@py_cpy_', text)
    if 'linux' in target:
        assert '@pwrite(' not in text and '@ftruncate(' not in text
        assert ('syscall' in text) if target.startswith('x86') else ('svc' in text)
        numbers = (18, 77) if target.startswith('x86') else (68, 46)
        for number in numbers:
            assert 'i64 '+str(number) in text
    elif 'apple' in target:
        assert '@pwrite(i32, ptr, i64, i64)' in text
        assert '@ftruncate(i32, i64)' in text
        assert '@__error()' in text
    else:
        assert '@pwrite(' not in text and '@ftruncate(' not in text
        assert 'ret i64 -38' in text
