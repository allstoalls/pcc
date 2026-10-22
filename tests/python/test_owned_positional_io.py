"""Actual positional provider bodies over the existing bounded file ABI model.

Host syscall oracles and an immutable-buffer model isolate protocol ordering,
errors, descriptor lifetime and rooted syscall payloads. Emitted execution is
separately required; these models are not native or moving-GC qualification.
"""
import os
from pathlib import Path

import pytest

from pcc.runtime.py import py_abi_constants as abi
from tests.python.test_owned_fdopen_provider import FileRuntime
from tests.python.test_owned_tempfile_provider import Obj


class PositionalRuntime(FileRuntime):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.snapshots = []
        self.interrupts = 0
        self.short = None
        self.safepoints = 0
        self.ns.update(pcc_platform_pwrite=self.pwrite,
                       pcc_platform_ftruncate=self.ftruncate,
                       py_bytes_from_obj=self.snapshot,
                       pcc_thread_safepoint=self.safepoint)

    def read(self, value, offset):
        if isinstance(value, Obj) and offset == abi.PYOBJECTHEADER_TYPE_TAG_OFFSET:
            if value.kind == 'bytearray': return abi.PY_TYPE_BYTEARRAY
            if value.kind == 'memoryview': return abi.PY_TYPE_MEMORYVIEW
        return super().read(value, offset)

    def snapshot(self, value):
        assert self.frames and value.pins > 0
        assert value.kind == 'bytes'
        out = value
        self.snapshots.append(out)
        return out

    def safepoint(self):
        self.safepoints += 1
        assert self.frames
        snapshot = self.read(self.frames[-1], self.ns["_OS_POSITIONAL_SNAPSHOT"] * 8)
        if snapshot is not None:
            assert snapshot.pins > 0

    def pwrite(self, fd, payload, length, offset):
        assert self.frames and self.snapshots[-1].pins > 0
        data = self.raw_count(payload, length)
        self.calls.append(('pwrite', fd, data, offset))
        if self.interrupts:
            self.interrupts -= 1
            return -4
        if self.short is not None: data = data[:self.short]
        try: return os.pwrite(fd, data, offset)
        except OSError as error: return -error.errno

    def ftruncate(self, fd, length):
        assert self.frames
        self.calls.append(('ftruncate', fd, length))
        if self.interrupts:
            self.interrupts -= 1
            return -4
        try: os.ftruncate(fd, length); return 0
        except OSError as error: return -error.errno

    def box(self, value):
        if isinstance(value, Obj): return value
        if isinstance(value, bytes): return Obj('bytes', value)
        if isinstance(value, bytearray): return Obj('bytearray', value)
        if isinstance(value, str): return self.string(value)
        if isinstance(value, float): return Obj('float', value)
        if value is None: return self.none
        return self.integer(value)

    def invoke(self, kind, *args):
        return self.ns['_os_' + kind + '_entry'](
            None, Obj('tuple', [self.box(arg) for arg in args]))

    def clean(self):
        assert not self.frames and not self.leases
        assert all(snapshot.pins == 0 for snapshot in self.snapshots)


def test_positional_provider_exact_bytes_cursor_and_snapshot_lifetime(tmp_path):
    runtime = PositionalRuntime()
    argument = runtime.box(b'XYZ')
    path = tmp_path / 'owned'
    with path.open('w+b', buffering=0) as stream:
        stream.write(b'abcdefgh')
        descriptor = stream.fileno()
        assert runtime.invoke('pwrite', descriptor, argument, 2) == 3
        assert stream.tell() == 8 and not stream.closed
        assert runtime.invoke('ftruncate', descriptor, 12) is runtime.none
        assert stream.tell() == 8 and os.fstat(descriptor).st_size == 12
        stream.seek(0)
        assert stream.read() == b'abXYZfgh\0\0\0\0'
        assert runtime.invoke('ftruncate', descriptor, 4) is runtime.none
        assert os.fstat(descriptor).st_size == 4
    assert path.read_bytes() == b'abXY'
    assert runtime.error is None
    runtime.clean()


def test_positional_provider_eintr_retries_and_short_success_is_observable(tmp_path):
    runtime = PositionalRuntime()
    runtime.interrupts = 2
    runtime.short = 2
    with (tmp_path / 'short').open('w+b', buffering=0) as stream:
        assert runtime.invoke('pwrite', stream.fileno(), b'abcdef', 3) == 2
        assert stream.tell() == 0
        stream.seek(0)
        assert stream.read() == b'\0\0\0ab'
        runtime.interrupts = 1
        assert runtime.invoke('ftruncate', stream.fileno(), 2) is runtime.none
    assert runtime.safepoints == 3
    assert len(runtime.calls) == 5
    runtime.clean()


@pytest.mark.parametrize('kind,argument', [('pwrite', 0), ('pwrite', 2), ('ftruncate', 0), ('ftruncate', 1)])
@pytest.mark.parametrize('value', ['', 1.5, 1 << 100, -(1 << 100)])
def test_positional_provider_bad_integer_precedes_io(kind, argument, value):
    runtime = PositionalRuntime()
    args = [-1, b'data', 0] if kind == 'pwrite' else [-1, 0]
    args[argument] = value
    assert runtime.invoke(kind, *args) is None
    assert runtime.error.value[0] == (3 if isinstance(value, (str, float)) else 15)
    assert runtime.calls == []
    runtime.clean()


def test_positional_provider_invalid_buffer_precedes_offset_callback():
    runtime = PositionalRuntime()
    events = []
    offset = runtime.index_object(0, callback=lambda: events.append('offset'))
    assert runtime.invoke('pwrite', -1, 'text', offset) is None
    assert runtime.error.value[0] == 3 and events == [] and runtime.calls == []
    runtime.clean()


def test_positional_provider_index_order_snapshot_and_original_callback_error(tmp_path):
    runtime = PositionalRuntime()
    events = []
    data = b'abc'
    with (tmp_path / 'callbacks').open('w+b', buffering=0) as stream:
        descriptor = runtime.index_object(stream.fileno(), callback=lambda: events.append('fd'))
        def update():
            events.append('offset')
        offset = runtime.index_object(1, callback=update)
        assert runtime.invoke('pwrite', descriptor, data, offset) == 3
        assert events == ['fd', 'offset']
        stream.seek(0)
        assert stream.read() == b'\0abc'
        error = Obj('exception', (2, 'callback failed'))
        invalid = runtime.index_object(0, error=error)
        assert runtime.invoke('ftruncate', stream.fileno(), invalid) is None
        assert runtime.error is error and os.fstat(stream.fileno()).st_size == 4
    runtime.clean()


@pytest.mark.parametrize('kind', ['pwrite', 'ftruncate'])
def test_positional_provider_error_metadata_and_descriptor_survive(tmp_path, kind):
    runtime = PositionalRuntime()
    path = tmp_path / 'negative'
    with path.open('w+b', buffering=0) as stream:
        stream.write(b'keep')
        args = [stream.fileno(), b'X', -1] if kind == 'pwrite' else [stream.fileno(), -1]
        assert runtime.invoke(kind, *args) is None
        assert runtime.error.attrs['errno'] == 22
        assert not stream.closed and stream.tell() == 4
    assert path.read_bytes() == b'keep'
    runtime.clean()


def test_file_flush_error_is_reported_before_publication(tmp_path):
    runtime = PositionalRuntime()
    descriptor = os.open(tmp_path / 'flush', os.O_RDWR | os.O_CREAT, 0o600)
    try:
        file = runtime.ns['py_file_fdopen_options'](
            descriptor, runtime.string('w+b'), runtime.none, runtime.none, runtime.none, 0, 0)
        runtime.errno = 5
        runtime.ns['fflush'] = lambda stream: -1
        assert runtime.ns['py_file_flush'](file) is None
        assert runtime.error.attrs['errno'] == 5
        assert os.fstat(descriptor).st_size == 0
        runtime.error = None
        runtime.ns['py_file_close'](file)
    finally:
        os.close(descriptor)


def _owned_fcntl(runtime):
    import ast
    import operator
    import sys
    root = Path(__file__).resolve().parents[2]
    runtime.load(root / 'pcc/runtime/py/py_file_lock.py')
    source = root / 'pcc/stdlib/fcntl.py'
    functions = [node for node in ast.parse(source.read_text()).body
                 if isinstance(node, ast.FunctionDef) and node.name == 'fcntl']
    namespace = {'operator': operator, 'sys': sys, 'F_GETFL': 3,
                 '_native_getfl': runtime.ns['py_fcntl_getfl']}
    exec(compile(ast.Module(functions, type_ignores=[]), str(source), 'exec'), namespace)
    return namespace['fcntl']


def test_actual_getfl_provider_preserves_append_cursor_and_owner(tmp_path):
    runtime = PositionalRuntime()
    query = _owned_fcntl(runtime)
    descriptor = os.open(tmp_path / 'flags', os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
    owner = Obj('file-wrapper')
    owner.fileno = lambda: descriptor
    # stdlib wrapper uses ordinary Python fileno; the runtime models the owner.
    try:
        os.lseek(descriptor, 3, os.SEEK_SET)
        original = runtime.ns['fd_control']
        def observed(fd, command, arg):
            assert any(runtime.read(slot, 0) is owner for slot in runtime.handles.values())
            return original(fd, command, arg)
        runtime.ns['fd_control'] = observed
        assert query(owner, 3) & os.O_APPEND
        assert os.lseek(descriptor, 0, os.SEEK_CUR) == 3
        assert os.fstat(descriptor).st_size == 0
    finally:
        os.close(descriptor)
    runtime.clean()
    assert not runtime.handles


def test_getfl_unsupported_command_and_invalid_descriptor_precede_query():
    runtime = PositionalRuntime()
    query = _owned_fcntl(runtime)
    runtime.ns['fd_control'] = lambda *args: pytest.fail('invalid query reached syscall')
    with pytest.raises(NotImplementedError, match='only F_GETFL'): query(0, 4)
    with pytest.raises(NotImplementedError, match='buffer'): query(0, 3, b'')
    with pytest.raises(ValueError): query(-1, 3)
    with pytest.raises(OverflowError): query(1 << 100, 3)
    with pytest.raises(TypeError): query(0, '3')
    runtime.clean()


def test_getfl_eintr_then_errno_preserves_owner_and_error():
    runtime = PositionalRuntime()
    query = _owned_fcntl(runtime)
    attempts = []
    def failed(fd, command, arg):
        attempts.append(fd)
        return -4 if len(attempts) == 1 else -9
    runtime.ns['fd_control'] = failed
    with pytest.raises(OSError) as error: query(12345, 3)
    assert error.value.errno == 9 and attempts == [12345, 12345]
    runtime.clean()
    assert not runtime.handles


@pytest.mark.parametrize('kind', ['bytearray', 'memoryview'])
def test_mutable_buffer_export_gap_is_explicit_before_offset_callback(kind):
    runtime = PositionalRuntime()
    value = Obj('bytearray', bytearray(b'abc'))
    if kind == 'memoryview': value = Obj('memoryview', value)
    events = []
    offset = runtime.index_object(0, callback=lambda: events.append('offset'))
    assert runtime.invoke('pwrite', -1, value, offset) is None
    assert runtime.error.value[0] == 11
    assert 'buffer' in runtime.error.value[1]
    assert events == [] and runtime.calls == []
    runtime.clean()


def test_positional_provider_preserves_full_signed_offset_width():
    runtime = PositionalRuntime()
    observed = []
    runtime.ns['pcc_platform_pwrite'] = lambda fd, data, size, offset: observed.append(offset) or 1
    runtime.ns['pcc_platform_ftruncate'] = lambda fd, length: observed.append(length) or 0
    assert runtime.invoke('pwrite', 7, b'x', (1 << 40) + 3) == 1
    assert runtime.invoke('ftruncate', 7, (1 << 40) + 9) is runtime.none
    assert observed == [(1 << 40) + 3, (1 << 40) + 9]
    runtime.clean()


@pytest.mark.parametrize('name,names', [
    ('pwrite', ['fd', 'buffer', 'offset']), ('ftruncate', ['fd', 'length'])])
def test_positional_provider_cached_callable_positional_signature(name, names):
    runtime = PositionalRuntime()
    factory = runtime.ns['py_os_' + name + '_function']
    first = factory()
    second = factory()
    assert first is second
    entry, captures = first.value
    signature = captures.value[1].value
    assert signature[0].value == '__pcc_func_signature_v1__'
    assert [item.value for item in signature[1].value] == names
    assert signature[2].value == [1] * len(names)
    assert signature[3].value == [runtime.false] * len(names)
    assert len(runtime.handles) == 1
    runtime.clean()
