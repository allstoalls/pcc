"""Bounded host reader controls; WinAPI/CRT are mocks, with no PCC imports.

Load only the three leaf sources by file, never the pcc package or a compiler.
The fd lifetime cases use ordinary CPython temporary-file I/O, not FFI.
"""

import ast
import builtins
import errno
import importlib.util
import io
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = "pcc/frontends/python/worker_resource_plan.py"
HOST_PATH = "pcc/frontends/python/worker_resource_host_windows.py"
HOST_MODULE = "pcc.frontends.python.worker_resource_host_windows"
SCAN_PATH = "pcc/frontends/python/pipeline_import_scan.py"


def _load_leaf(path):
    spec = importlib.util.spec_from_file_location("_rss_reader_control", ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_winapi(monkeypatch, module, *, handle=0x123456789AB, fd=41,
                 conversion_error=None, close_error=None, winerror=5):
    calls = []
    failure = OSError(errno.EACCES, "CreateFileW failed")
    failure.winerror = winerror

    def create_file(*arguments):
        calls.append(("CreateFileW", arguments))
        return handle

    def close_handle(value):
        calls.append(("CloseHandle", value))
        if close_error is not None:
            raise close_error
        return 1

    kernel = SimpleNamespace(CreateFileW=create_file, CloseHandle=close_handle)

    def library(name, **options):
        assert name == "kernel32" and options == {"use_last_error": True}
        return kernel

    def open_osfhandle(value, flags):
        calls.append(("open_osfhandle", value, flags))
        if conversion_error is not None:
            raise conversion_error
        return fd

    pointer = lambda value: SimpleNamespace(value=(1 << 64) - 1 if value == -1 else value)
    ctypes = SimpleNamespace(WinDLL=library, c_void_p=pointer, c_wchar_p=object(),
                             c_uint32=object(), c_int32=object(),
                             get_last_error=lambda: winerror,
                             WinError=lambda code: failure if code == winerror else None)
    monkeypatch.setitem(sys.modules, "ctypes", ctypes)
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(open_osfhandle=open_osfhandle))
    monkeypatch.setattr(module, "os", SimpleNamespace(O_RDONLY=0, O_BINARY=0x8000, O_NOINHERIT=0x80))
    return calls, kernel, ctypes, failure


def _assert_open_contract(calls, kernel, ctypes, path):
    assert calls[0] == ("CreateFileW", (path, 0x80000000, 7, None, 3, 0x80, None))
    assert kernel.CreateFileW.argtypes == [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    assert kernel.CreateFileW.restype is ctypes.c_void_p
    assert kernel.CloseHandle.argtypes == [ctypes.c_void_p]
    assert kernel.CloseHandle.restype is ctypes.c_int32


@pytest.mark.parametrize("code", [2, 5])
def test_create_failure_never_transfers_or_closes_invalid_handle(monkeypatch, code):
    host = _load_leaf(HOST_PATH)
    calls, kernel, ctypes, failure = _fake_winapi(
        monkeypatch, host, handle=(1 << 64) - 1, winerror=code,
    )
    with pytest.raises(OSError) as caught:
        host._open_worker_resource_fd("snapshot.rss", 0)
    assert caught.value is failure
    assert len(calls) == 1
    _assert_open_contract(calls, kernel, ctypes, "snapshot.rss")


@pytest.mark.parametrize("interrupted", [False, True])
@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_conversion_failure_closes_handle_once_and_preserves_error(monkeypatch, interrupted, cleanup_fails):
    host = _load_leaf(HOST_PATH)
    failure = KeyboardInterrupt() if interrupted else OSError("fd conversion failed")
    calls, kernel, ctypes, _ = _fake_winapi(
        monkeypatch, host, conversion_error=failure,
        close_error=OSError("cleanup failed") if cleanup_fails else None,
    )
    with pytest.raises(type(failure)) as caught:
        host._open_worker_resource_fd("snapshot.rss", 0)
    assert caught.value is failure
    assert calls[1:] == [("open_osfhandle", 0x123456789AB, 0x8080),
                         ("CloseHandle", 0x123456789AB)]
    _assert_open_contract(calls, kernel, ctypes, "snapshot.rss")


@pytest.mark.parametrize("outcome", ["success", "wrap-error", "read-error"])
def test_builtin_open_owns_fd_after_transfer(tmp_path, monkeypatch, outcome):
    host = _load_leaf(HOST_PATH)
    path = tmp_path / "snapshot.rss"
    payload = b"old complete snapshot\n" if outcome != "read-error" else b"\xff\n"
    path.write_bytes(payload)
    fd = os.open(path, os.O_RDONLY)
    calls, kernel, ctypes, _ = _fake_winapi(monkeypatch, host, fd=fd)
    if outcome == "wrap-error":
        # FileIO has adopted the opener's fd before builtin open rejects an
        # unbuffered text stream. The builtin error path must close it.
        def unbuffered_open(path, mode, **options):
            return builtins.open(path, mode, buffering=0, **options)
        monkeypatch.setattr(host, "open", unbuffered_open, raising=False)
    try:
        if outcome == "wrap-error":
            with pytest.raises(ValueError, match="unbuffered text"):
                host.open_worker_resource(str(path))
        elif outcome == "read-error":
            with pytest.raises(UnicodeDecodeError):
                with host.open_worker_resource(str(path)) as stream:
                    stream.read()
        else:
            with host.open_worker_resource(str(path)) as stream:
                assert not os.get_inheritable(stream.fileno())
                assert stream.read() == payload.decode("utf-8")
            assert stream.closed
        with pytest.raises(OSError) as caught:
            os.fstat(fd)
        assert caught.value.errno == errno.EBADF
        assert calls[1:] == [("open_osfhandle", 0x123456789AB, 0x8080)]
        _assert_open_contract(calls, kernel, ctypes, str(path))
    finally:
        # Test cleanup only: a failed assertion must not leak its temporary fd.
        try:
            os.close(fd)
        except OSError:
            pass


@pytest.mark.parametrize("implementation,platform", [
    ("cpython", "win32"), ("cpython", "linux"), ("pcc", "win32"), ("pypy", "win32"),
])
def test_only_windows_cpython_dispatches_to_host_reader(monkeypatch, implementation, platform):
    policy = _load_leaf(POLICY_PATH)
    original_import = builtins.__import__
    imports = []
    calls = []
    host_stream = io.StringIO("host snapshot")
    ordinary_stream = io.StringIO("ordinary snapshot")

    def host_open(path):
        calls.append(("host", path))
        return host_stream

    def ordinary_open(path, mode, **options):
        calls.append(("ordinary", path, mode, options))
        return ordinary_stream

    def importing(name, *args, **options):
        imports.append(name)
        if name == "sys":
            return SimpleNamespace(implementation=SimpleNamespace(name=implementation), platform=platform)
        if name == HOST_MODULE:
            return SimpleNamespace(open_worker_resource=host_open)
        assert name not in ("ctypes", "msvcrt") and not name.startswith("pcc.")
        return original_import(name, *args, **options)

    monkeypatch.setattr(policy, "open", ordinary_open, raising=False)
    with monkeypatch.context() as selected:
        selected.setattr(builtins, "__import__", importing)
        result = policy._open_worker_resource("snapshot.rss")
    if (implementation, platform) == ("cpython", "win32"):
        assert result is host_stream and calls == [("host", "snapshot.rss")]
        assert imports == ["sys", HOST_MODULE]
    else:
        assert result is ordinary_stream
        assert calls == [("ordinary", "snapshot.rss", "r", {"encoding": "utf-8"})]
        assert imports == ["sys"]


@pytest.mark.parametrize("shape", ["valid", "wrong-pid", "wrong-token", "partial", "zero-rss", "low-peak", "missing"])
def test_reader_preserves_snapshot_validation_and_closes_stream(monkeypatch, shape):
    policy = _load_leaf(POLICY_PATH)
    rows = [policy.RESOURCE_REPORT_SCHEMA, "123", "complete", "100", "120", "attempt"]
    if shape == "wrong-pid": rows[1] = "124"
    elif shape == "wrong-token": rows[5] = "old-attempt"
    elif shape == "partial": rows.pop()
    elif shape == "zero-rss": rows[3] = "0"
    elif shape == "low-peak": rows[4] = "99"
    stream = io.StringIO("\n".join(rows) + "\n")

    def opening(path):
        assert path == "snapshot.rss"
        if shape == "missing":
            raise FileNotFoundError(path)
        return stream

    monkeypatch.setattr(policy, "_open_worker_resource", opening)
    result = policy.read_worker_resource("snapshot.rss", 123, "attempt")
    assert result == (("complete", 100, 120) if shape == "valid" else None)
    assert stream.closed == (shape != "missing")


def test_actual_dispatcher_keeps_windows_adapter_outside_native_import_closure():
    scan = _load_leaf(SCAN_PATH)
    source = (ROOT / POLICY_PATH).read_text()
    native = scan._without_inactive_runtime_imports(source)
    host = scan._without_inactive_implementation_imports(source, implementation="cpython")
    assert HOST_MODULE not in native and HOST_MODULE in host
    assert "import ctypes" not in native and "import msvcrt" not in native
    assert 'return open(path, "r", encoding="utf-8")' in native
    tree = ast.parse(source)
    dispatch = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "_open_worker_resource")
    assert ast.unparse(dispatch.body[0]) == "import sys as execution_runtime"
    assert ast.unparse(dispatch.body[1].test) == "execution_runtime.implementation.name == 'cpython'"
    assert ast.unparse(dispatch.body[1].body[0].test) == "execution_runtime.platform == 'win32'"
    reader = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                  and node.name == "read_worker_resource")
    assert any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
               and node.func.id == "_open_worker_resource" for node in ast.walk(reader))


@pytest.mark.parametrize("change", ["compound", "rebound", "global"])
def test_unproven_dispatcher_guard_keeps_host_dependency(change):
    scan = _load_leaf(SCAN_PATH)
    source = (ROOT / POLICY_PATH).read_text()
    if change == "compound":
        source = source.replace('if execution_runtime.implementation.name == "cpython":',
                                'if execution_runtime.implementation.name == "cpython" and enabled:')
    elif change == "rebound":
        source = source.replace("    import sys as execution_runtime\n",
                                "    import sys as execution_runtime\n    execution_runtime = other\n")
    else:
        source = "import sys as execution_runtime\n" + source.replace("    import sys as execution_runtime\n", "")
    assert HOST_MODULE in scan._without_inactive_runtime_imports(source)
