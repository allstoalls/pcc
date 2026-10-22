"""Emitted owned descriptor and NamedTemporaryFile lifetime contracts."""
import os
import subprocess

import pytest


PROGRAM = r'''
import gc
import os
import tempfile
from pathlib import Path
from os import open as open_fd, fdopen as adopt, close as close_fd
from tempfile import NamedTemporaryFile as make

def consume(*, stream, **extras):
    gc.collect()
    return stream.write('lease\n')

class Index:
    def __init__(self, value):
        self.value = value
    def __index__(self):
        gc.collect()
        return self.value

def integer_contract(root):
    path = root + '/integer-contract'
    for invalid in ('', 1.25):
        try:
            close_fd(invalid)
        except TypeError:
            pass
        else:
            raise AssertionError('invalid descriptor reached close')
        try:
            open_fd(path, invalid)
        except TypeError:
            pass
        else:
            raise AssertionError('invalid flags reached open')
    try:
        open_fd(path, 2 ** 100)
    except OverflowError:
        pass
    else:
        raise AssertionError('big flags were truncated')
    fd = open_fd(path, Index(os.O_RDWR | os.O_CREAT | os.O_EXCL), Index(0o600))
    try:
        adopt(Index(fd), 'rb')
    except TypeError:
        pass
    else:
        raise AssertionError('fdopen accepted an index-only descriptor')
    try:
        adopt(fd, 'rb', buffering='')
    except TypeError:
        pass
    else:
        raise AssertionError('invalid buffering was accepted')
    with adopt(fd, 'w+b', buffering=Index(-1)) as stream:
        stream.write(b'owned')
        stream.seek(Index(0))
        assert stream.read(Index(5)) == b'owned'
        try:
            stream.seek('')
        except TypeError:
            pass
        else:
            raise AssertionError('invalid seek performed IO')
    os.unlink(path)
    for invalid in ('', 1.25):
        try:
            make(dir=root, buffering=invalid)
        except TypeError:
            pass
        else:
            raise AssertionError('invalid temporary-file buffering created a file')
    prior = None
    for iteration in range(3):
        gc.collect()
        with make(dir=root) as stream:
            cls = type(stream)
            if prior is not None:
                assert cls is prior
            prior = cls
        with tempfile.TemporaryDirectory(dir=root) as directory:
            assert os.path.isdir(directory)

def main(root):
    integer_contract(root)
    path = root + '/lease'
    fd = open_fd(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with adopt(fd, 'w', encoding='utf-8') as stream:
        assert stream.name == fd
        assert consume(stream=stream) == 6
    assert stream.closed and stream.name == fd
    try:
        close_fd(fd)
    except OSError as error:
        assert error.errno == 9
    else:
        raise AssertionError('fdopen did not close its descriptor')
    with open(path, 'r', encoding='utf-8') as stream:
        assert stream.read() == 'lease\n'
    os.unlink(path)
    assert make is tempfile.NamedTemporaryFile
    with make(mode='w+', encoding='utf-8', prefix='owned_', suffix='_tail',
              dir=Path(root), delete=False) as stream:
        name = stream.name
        assert name.endswith('_tail')
        assert stream.__enter__() is stream
        stream.write('alpha\nbeta\n')
        stream.seek(0)
        assert list(stream) == ['alpha\n', 'beta\n']
    assert stream.closed and os.path.exists(name)
    os.unlink(name)
    manager = make(mode='w', dir=root, delete=True)
    name = manager.name
    writer = manager.write
    manager = None
    gc.collect()
    assert os.path.exists(name)
    assert writer('kept') == 4
    writer = None
    gc.collect()
    assert not os.path.exists(name)
    manager = make(mode='w', dir=root, delete=True, delete_on_close=False)
    name = manager.name
    with manager as stream:
        stream.close()
        assert os.path.exists(name)
    assert not os.path.exists(name)
    try:
        with make(mode='w', dir=root) as stream:
            name = stream.name
            raise ValueError('body')
    except ValueError as error:
        assert str(error) == 'body'
    else:
        raise AssertionError('context suppressed body exception')
    assert not os.path.exists(name)
    try:
        make(mode='rr', dir=root)
    except ValueError:
        pass
    else:
        raise AssertionError('invalid mode accepted')
    for on_close in (True, False):
        with make(dir=root, delete_on_close=on_close) as stream:
            os.unlink(stream.name)
            stream.close()
    print('OWNED_FILE_PROVIDERS_OK')

import sys
main(sys.argv[1])
'''


@pytest.mark.integration
def test_owned_file_providers_all_collectors(tmp_path,monkeypatch,
        pcc_runtime_archive,python_program_compiler):
    source=tmp_path/'file_providers.py';source.write_text(PROGRAM)
    binary=tmp_path/'file_providers'
    monkeypatch.setenv('PCC_TEST_NO_NATIVE_PROVISIONING','1')
    monkeypatch.setenv('PCC_NO_AUTO_PCC1','1')
    monkeypatch.setenv('PCC_PYTHON_IR_PASSES','off')
    python_program_compiler(str(source),str(binary),backend='self',libpython_mode='off',
        ir_scaffold_mode='on',runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        root=tmp_path/('gc'+str(backend));root.mkdir()
        result=subprocess.run([str(binary),str(root)],capture_output=True,text=True,
            timeout=30,env=dict(os.environ,PCC_GC_BACKEND=str(backend),
                               PCC_GC_REFCOUNT_PROVENANCE_PROBE='2'))
        assert (result.returncode,result.stdout,result.stderr)==(0,'OWNED_FILE_PROVIDERS_OK\n',''), (
            backend,result.returncode,result.stdout,result.stderr)
        assert list(root.iterdir())==[]
