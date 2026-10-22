"""Native, no-libpython TemporaryDirectory public ownership contracts."""
from __future__ import annotations

import os
import subprocess

import pytest


NATIVE_SOURCE = r'''
import gc
import os
import tempfile
from pathlib import Path
from tempfile import TemporaryDirectory as TD

def create_manager(root):
    temp_dir = tempfile.TemporaryDirectory(prefix='pcc_build_tools_', dir=root)
    tool_root = Path(temp_dir.name)
    return temp_dir

def main(root):
    manager = create_manager(root)
    name = manager.name
    assert os.path.isdir(name)
    assert isinstance(manager, tempfile.TemporaryDirectory)
    assert manager.__enter__() is name
    with manager as entered:
        assert entered is name
    assert manager.name is name
    assert not os.path.exists(name)
    manager.cleanup()
    os.mkdir(name)
    manager.cleanup()
    assert not os.path.exists(name)
    constructor = TD
    retained = constructor('_tail', '', root, False, delete=False)
    saved_name = retained.name
    with retained as entered:
        assert entered is saved_name
    assert os.path.isdir(saved_name)
    retained.cleanup()
    assert not os.path.exists(saved_name)
    owned = TD(dir=root)
    owned_name = owned.name
    bound_cleanup = owned.cleanup
    owned = None
    gc.collect()
    assert os.path.isdir(owned_name)
    bound_cleanup()
    assert not os.path.exists(owned_name)
    finalizable = TD(dir=root)
    implicit_name = finalizable.name
    finalizable = None
    gc.collect()
    assert not os.path.exists(implicit_name)
    manager = TD(dir=root)
    exceptional_name = manager.name
    try:
        with manager as entered:
            raise ValueError('body error')
    except ValueError as error:
        assert str(error) == 'body error'
    else:
        raise AssertionError('context suppressed body exception')
    assert not os.path.exists(exceptional_name)
    try:
        TD('', '', root, False, False)
    except TypeError:
        pass
    else:
        raise AssertionError('delete must be keyword-only')
    try:
        TD(dir=root, unexpected=True)
    except TypeError:
        pass
    else:
        raise AssertionError('unknown keyword accepted')
    print('OWNED_TEMPDIR_OK')

import sys
main(sys.argv[1])
'''


@pytest.mark.integration
def test_tempdir_first_class_ownership_all_collectors(tmp_path, monkeypatch,
        pcc_runtime_archive, python_program_compiler):
    source = tmp_path / 'owned_tempdir.py'
    source.write_text(NATIVE_SOURCE)
    binary = tmp_path / 'owned_tempdir'
    monkeypatch.setenv('PCC_PYTHON_IR_PASSES', 'off')
    python_program_compiler(str(source), str(binary), backend='self',
        libpython_mode='off', ir_scaffold_mode='on',
        runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        root = tmp_path / ('backend_' + str(backend))
        root.mkdir()
        result = subprocess.run([str(binary), str(root)], capture_output=True,
            text=True, timeout=30,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend),
                     PCC_GC_REFCOUNT_PROVENANCE_PROBE='2'))
        assert result.returncode == 0, (backend, result.returncode, result.stdout, result.stderr)
        assert result.stdout == 'OWNED_TEMPDIR_OK\n', (backend, result.stdout)
        assert result.stderr == '', (backend, result.stderr)
        assert list(root.iterdir()) == [], (backend, list(root.iterdir()))
