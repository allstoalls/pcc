"""Emitted native execution of the public mkdtemp producer and binder."""
import os
import subprocess

import pytest


PROGRAM = r'''
import gc
import os
import tempfile
from tempfile import mkdtemp as make

events = []
def operand(label, value):
    gc.collect()
    events.append(label)
    return value

def take(*, value, later=None, **extras):
    gc.collect()
    return value

def main(root):
    assert make is tempfile.mkdtemp
    alias = make
    path = take(value=alias(suffix=operand('suffix', '_tail'),
                           prefix=operand('prefix', 'owned_'),
                           dir=operand('dir', root)))
    assert events == ['suffix', 'prefix', 'dir']
    assert isinstance(path, str)
    assert os.path.isabs(path) and os.path.isdir(path)
    assert os.path.basename(path).startswith('owned_') and path.endswith('_tail')
    gc.collect()
    assert os.path.isdir(path)
    os.rmdir(path)
    previous = ''
    for index in range(32):
        path = alias('', '', root)
        assert path != previous and os.path.isdir(path)
        previous = path
        os.rmdir(path)
    for mode in range(3):
        try:
            if mode == 0:
                alias('', suffix='_duplicate', dir=root)
            elif mode == 1:
                alias('', '', root, 'extra')
            else:
                alias(dir=root, unexpected=True)
        except TypeError:
            pass
        else:
            raise AssertionError('mkdtemp binding accepted invalid arguments')
    try:
        alias(prefix='bad\x00prefix', dir=root)
    except ValueError:
        pass
    else:
        raise AssertionError('embedded null was accepted')
    try:
        alias(dir=os.path.join(root, 'missing'))
    except FileNotFoundError as error:
        assert error.errno == 2
    else:
        raise AssertionError('missing parent was accepted')
    print('OWNED_MKDTEMP_OK')

import sys
main(sys.argv[1])
'''


@pytest.mark.integration
def test_mkdtemp_public_binding_all_collectors(tmp_path,monkeypatch,
        pcc_runtime_archive,python_program_compiler):
    source=tmp_path/'mkdtemp.py';source.write_text(PROGRAM)
    binary=tmp_path/'mkdtemp'
    monkeypatch.setenv('PCC_PYTHON_IR_PASSES','off')
    python_program_compiler(str(source),str(binary),backend='self',libpython_mode='off',
        ir_scaffold_mode='on',runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        root=tmp_path/('gc'+str(backend));root.mkdir()
        result=subprocess.run([str(binary),str(root)],capture_output=True,text=True,
            timeout=30,env=dict(os.environ,PCC_GC_BACKEND=str(backend),
                               PCC_GC_REFCOUNT_PROVENANCE_PROBE='2'))
        assert (result.returncode,result.stdout,result.stderr)==(0,'OWNED_MKDTEMP_OK\n','')
        assert list(root.iterdir())==[]
