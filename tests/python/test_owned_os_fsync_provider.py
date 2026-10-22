"""Reference semantics for the owned os.fsync source provider.

The native test executes the same control under all collectors with an
explicit source-matched archive. Host shims model only owned machine intrinsics;
they never enter production or count as emitted-execution qualification.
"""
from __future__ import annotations

import gc
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import warnings

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONTROL = ROOT / 'tests/fixtures/owned_output_writer/native_fsync_provider.py'


def load_provider():
    spec = importlib.util.spec_from_file_location('owned_os_fsync', ROOT / 'pcc/stdlib/os.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.stack_alloc = bytearray
    module.store_i32 = lambda buffer, offset, value: buffer.__setitem__(slice(offset, offset + 4), value.to_bytes(4, 'little', signed=True))
    module.load_i32 = lambda buffer, offset: int.from_bytes(buffer[offset:offset + 4], 'little', signed=True)
    module.strlen = lambda buffer: buffer.index(0)
    module._new_text = lambda buffer, length: bytes(buffer[:length]).decode()
    module._thread_safepoint = gc.collect
    module._error_pending = lambda: False

    def integer(value, overflow):
        number = int.__index__(value)
        if number < -(2 ** 63) or number >= 2 ** 63:
            module.store_i32(overflow, 0, 1)
            return 0
        return number

    def errno_message(number, buffer, capacity):
        data = os.strerror(number).encode() + b'\0'
        if len(data) > capacity:
            return -1
        buffer[:len(data)] = data
        return 0

    def sync(fd):
        try:
            os.fsync(fd)
        except OSError as error:
            return -error.errno
        return 0

    module._fd_integer = integer
    module._errno_message = errno_message
    module.sync_file = sync
    return module


def test_owned_source_matches_reference_control(tmp_path, monkeypatch):
    provider = load_provider()
    spec = importlib.util.spec_from_file_location('fsync_control', CONTROL)
    control = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(control)
    control.main(str(tmp_path / 'reference'))
    control.saved_sync = provider.fsync
    # Preserve real os.fsync in the syscall shim while exposing the source
    # wrapper through the control's module-value and fd= call sites.
    class OsView:
        fsync = staticmethod(provider.fsync)
        unlink = staticmethod(os.unlink)
    control.os = OsView
    control.main(str(tmp_path / 'owned'))


def test_fsync_bool_warning_and_fileno_bool(tmp_path):
    provider = load_provider()
    seen = []
    provider.sync_file = lambda descriptor: seen.append(descriptor) or 0
    for sync in (os.fsync, provider.fsync):
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter('always')
            try:
                sync(False)
            except OSError:
                pass
        assert len(captured) == 1
        assert captured[0].category is RuntimeWarning
        assert str(captured[0].message) == 'bool is used as a file descriptor'
    class BooleanFileno:
        def fileno(self):
            return False
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter('always')
        provider.fsync(BooleanFileno())
    assert not captured
    assert seen == [0, 0]
    with warnings.catch_warnings():
        warnings.simplefilter('error', RuntimeWarning)
        with pytest.raises(RuntimeWarning, match='bool is used as a file descriptor'):
            provider.fsync(False)
    assert seen == [0, 0]


def test_fsync_does_not_call_integer_subclass_overrides(tmp_path):
    provider = load_provider()
    seen = []
    provider.sync_file = lambda descriptor: seen.append(descriptor) or 0
    class Integer(int):
        def __int__(self):
            raise AssertionError('__int__ must not run')
        def __index__(self):
            raise AssertionError('__index__ must not run')
        def __lt__(self, other):
            raise AssertionError('__lt__ must not run')
        def __gt__(self, other):
            raise AssertionError('__gt__ must not run')
    assert provider.fsync(Integer(31)) is None
    assert seen == [31]


def test_fsync_retries_interrupted_syscall_with_original_owner(tmp_path):
    provider = load_provider()
    events = []
    class Descriptor:
        def fileno(self):
            events.append('fileno')
            return 31
        def __del__(self):
            events.append('release')
    def sync(fd):
        assert fd == 31
        assert 'release' not in events
        events.append('sync')
        return -4 if events.count('sync') == 1 else 0
    provider.sync_file = sync
    assert provider.fsync(Descriptor()) is None
    assert events == ['fileno', 'sync', 'sync', 'release']


@pytest.mark.integration
def test_owned_fsync_provider_native_all_collectors(
        tmp_path, monkeypatch, pcc_runtime_archive, python_program_compiler):
    monkeypatch.setenv('PCC_TEST_NO_NATIVE_PROVISIONING', '1')
    monkeypatch.setenv('PCC_NO_AUTO_PCC1', '1')
    monkeypatch.setenv('PCC_PYTHON_IR_PASSES', 'off')
    binary = tmp_path / 'native_fsync_provider'
    python_program_compiler(str(CONTROL), str(binary), backend='self', libpython_mode='off',
                            ir_scaffold_mode='on', runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        path = tmp_path / ('gc' + str(backend))
        result = subprocess.run([str(binary), str(path)], capture_output=True, text=True,
                                timeout=30, env=dict(os.environ, PCC_GC_BACKEND=str(backend),
                                PCC_GC_REFCOUNT_PROVENANCE_PROBE='2'))
        assert (result.returncode, result.stdout, result.stderr) == (0, 'OWNED_FSYNC_PROVIDER_OK\n', ''), (
            backend, result.returncode, result.stdout, result.stderr)
        assert not path.exists()
