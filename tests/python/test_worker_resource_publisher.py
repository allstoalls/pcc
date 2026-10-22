"""Pure Windows RSS publisher ABI, ownership, dispatch and closure controls.

No PCC package, native code, real ctypes, msvcrt or WinAPI is imported or executed.
Only the three named source leaves are loaded; WinAPI calls see inert fakes.
"""

import ast
import builtins
import errno
import importlib.util
import ntpath
from pathlib import Path
import struct
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
HOST_PATH = "pcc/frontends/python/worker_resource_host_windows.py"
HOST_MODULE = "pcc.frontends.python.worker_resource_host_windows"
POLICY_PATH = "pcc/frontends/python/worker_resource_plan.py"
SCAN_PATH = "pcc/frontends/python/pipeline_import_scan.py"


def _load_leaf(path):
    spec = importlib.util.spec_from_file_location("_rss_publisher_control", ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _absolute(path):
    return ntpath.normpath(path if ntpath.isabs(path) else ntpath.join("C:\\work", path))


def _fake_kernel(monkeypatch, host, *, open_error=None, rename_error=None,
                 close_failure=None, setup_error=None, interrupt_after_commit=None):
    calls = []
    state = SimpleNamespace(last_error=None, committed=False)
    handle = 0x123456789AB

    def create_file(*arguments):
        calls.append(("create", arguments))
        if open_error is not None:
            state.last_error = open_error
            return (1 << 64) - 1
        return handle

    def rename(opened, information_class, buffer, size):
        calls.append(("rename", opened, information_class, buffer, size))
        if rename_error is not None:
            if interrupt_after_commit is not None:
                state.committed = interrupt_after_commit
                raise rename_error
            if isinstance(rename_error, OSError):
                state.last_error = rename_error
                return 0
            raise rename_error
        state.committed = True
        return 1

    def close(opened):
        calls.append(("close", opened))
        if close_failure is not None:
            if isinstance(close_failure, OSError):
                state.last_error = close_failure
                return 0
            raise close_failure
        return 1

    kernel = SimpleNamespace(CreateFileW=create_file, SetFileInformationByHandle=rename, CloseHandle=close)

    def library(name, **options):
        assert name == "kernel32" and options == {"use_last_error": True}
        return kernel

    def buffer(payload):
        calls.append(("buffer", payload))
        if setup_error is not None:
            raise setup_error
        return payload + b"\0"

    def windows_error(code):
        assert code == state.last_error.winerror
        return state.last_error

    pointer = lambda value: SimpleNamespace(value=(1 << 64) - 1 if value == -1 else value)
    ctypes = SimpleNamespace(
        WinDLL=library, create_string_buffer=buffer, c_void_p=pointer,
        c_wchar_p=object(), c_uint32=object(), c_int32=object(),
        get_last_error=lambda: state.last_error.winerror, WinError=windows_error,
    )
    monkeypatch.setitem(sys.modules, "ctypes", ctypes)
    monkeypatch.setattr(host, "os", SimpleNamespace(path=SimpleNamespace(abspath=_absolute)))
    return calls, state, kernel, ctypes, handle


def _error(code):
    error = OSError(errno.EACCES, "mock Windows failure " + str(code))
    error.winerror = code
    return error


def _assert_signatures(kernel, ctypes):
    assert kernel.CreateFileW.argtypes == [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    assert kernel.CreateFileW.restype is ctypes.c_void_p
    assert kernel.SetFileInformationByHandle.argtypes == [
        ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p, ctypes.c_uint32,
    ]
    assert kernel.SetFileInformationByHandle.restype is ctypes.c_int32
    assert kernel.CloseHandle.argtypes == [ctypes.c_void_p]
    assert kernel.CloseHandle.restype is ctypes.c_int32


@pytest.mark.parametrize("pointer_bytes", [4, 8])
@pytest.mark.parametrize("name", ["plain.rss", "報告.rss", "report-😀.rss", "report-\ud800.rss"])
def test_rename_information_matches_sdk_layout_and_utf16_length(pointer_bytes, name):
    host = _load_leaf(HOST_PATH)
    path = "C:\\work\\" + name
    payload = host._worker_resource_rename_information(path, pointer_bytes)
    filename = path.encode("utf-16-le", "surrogatepass")
    handle_offset, length_offset, filename_offset, structure_size = (
        (8, 16, 20, 24) if pointer_bytes == 8 else (4, 8, 12, 16)
    )
    assert int.from_bytes(payload[:4], "little") == 3
    assert payload[4:handle_offset] == b"\0" * (handle_offset - 4)
    assert payload[handle_offset:handle_offset + pointer_bytes] == b"\0" * pointer_bytes
    assert int.from_bytes(payload[length_offset:length_offset + 4], "little") == len(filename)
    assert payload[filename_offset:filename_offset + len(filename)] == filename
    assert payload[filename_offset + len(filename):] == b"\0" * 4
    assert len(payload) == structure_size + len(filename)


@pytest.mark.parametrize("path,pointer_bytes", [("C:\\bad\0name", 8), ("C:\\ok", 2), ("C:\\ok", 16)])
def test_rename_information_rejects_invalid_inputs(path, pointer_bytes):
    host = _load_leaf(HOST_PATH)
    with pytest.raises(ValueError):
        host._worker_resource_rename_information(path, pointer_bytes)


def test_extended_rename_uses_only_closed_temporary_delete_access_and_exact_buffer(monkeypatch):
    host = _load_leaf(HOST_PATH)
    calls, state, kernel, ctypes, handle = _fake_kernel(monkeypatch, host)
    assert host.replace_worker_resource("sub\\..\\報告-😀.rss.tmp", "報告-😀.rss") is None
    assert state.committed
    information = host._worker_resource_rename_information("C:\\work\\報告-😀.rss", struct.calcsize("P"))
    assert calls == [
        ("buffer", information),
        ("create", ("C:\\work\\報告-😀.rss.tmp", 0x00010000, 7, None, 3, 0x80, None)),
        ("rename", handle, 22, information + b"\0", len(information)),
        ("close", handle),
    ]
    _assert_signatures(kernel, ctypes)


@pytest.mark.parametrize("temporary,path", [
    ("other.rss.tmp", "worker.rss"), ("other\\worker.rss.tmp", "worker.rss"),
    ("worker.rss.tmp\0tail", "worker.rss"), ("worker.rss.tmp", "worker.rss\0tail"),
])
def test_invalid_publication_paths_never_open_a_handle(monkeypatch, temporary, path):
    host = _load_leaf(HOST_PATH)
    calls, state, *_ = _fake_kernel(monkeypatch, host)
    with pytest.raises(ValueError):
        host.replace_worker_resource(temporary, path)
    assert calls == [] and not state.committed


@pytest.mark.parametrize("code", [2, 5])
def test_temporary_open_failure_preserves_error_without_rename_or_close(monkeypatch, code):
    host = _load_leaf(HOST_PATH)
    failure = _error(code)
    calls, state, *_ = _fake_kernel(monkeypatch, host, open_error=failure)
    with pytest.raises(OSError) as caught:
        host.replace_worker_resource("worker.rss.tmp", "worker.rss")
    assert caught.value is failure and not state.committed
    assert [row[0] for row in calls] == ["buffer", "create"]


@pytest.mark.parametrize("code", [5, 32, 50, 87])
@pytest.mark.parametrize("cleanup", ["success", "false", "exception"])
def test_rename_failure_preserves_original_error_and_closes_once(monkeypatch, code, cleanup):
    host = _load_leaf(HOST_PATH)
    failure = _error(code)
    closing = None if cleanup == "success" else _error(6) if cleanup == "false" else RuntimeError("close raised")
    calls, state, _, _, handle = _fake_kernel(monkeypatch, host, rename_error=failure, close_failure=closing)
    with pytest.raises(OSError) as caught:
        host.replace_worker_resource("worker.rss.tmp", "worker.rss")
    assert caught.value is failure and caught.value.winerror == code
    assert not state.committed
    assert caught.value.publication_committed is False
    assert caught.value.handle_close_attempted is True
    assert caught.value.handle_close_confirmed is (cleanup == "success")
    if cleanup != "success":
        assert caught.value.handle_cleanup_error is closing
    else:
        assert not hasattr(caught.value, "handle_cleanup_error")
    assert [row[0] for row in calls] == ["buffer", "create", "rename", "close"]
    assert calls[-1] == ("close", handle)


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("kind", ["interrupt", "permission"])
def test_rename_interruption_keeps_outcome_unknown_and_closes_once(monkeypatch, committed, kind):
    host = _load_leaf(HOST_PATH)
    failure = KeyboardInterrupt() if kind == "interrupt" else _error(5)
    calls, state, *_ = _fake_kernel(monkeypatch, host, rename_error=failure,
                                   interrupt_after_commit=committed)
    with pytest.raises(RuntimeError, match="rename outcome unknown after API interruption") as caught:
        host.replace_worker_resource("worker.rss.tmp", "worker.rss")
    assert caught.value.__cause__ is failure and state.committed is committed
    assert caught.value.publication_committed is None
    assert caught.value.handle_close_attempted is True
    assert caught.value.handle_close_confirmed is True
    assert not isinstance(caught.value, PermissionError)
    assert [row[0] for row in calls] == ["buffer", "create", "rename", "close"]


@pytest.mark.parametrize("cleanup", ["false", "exception"])
def test_postcommit_close_failure_is_explicit_and_cannot_be_retried_as_contention(monkeypatch, cleanup):
    host = _load_leaf(HOST_PATH)
    failure = _error(5) if cleanup == "false" else RuntimeError("close raised")
    calls, state, *_ = _fake_kernel(monkeypatch, host, close_failure=failure)
    with pytest.raises(RuntimeError, match="rename committed but handle cleanup failed") as caught:
        host.replace_worker_resource("worker.rss.tmp", "worker.rss")
    assert state.committed and caught.value.__cause__ is failure
    assert caught.value.publication_committed is True
    assert caught.value.handle_close_attempted is True
    assert caught.value.handle_close_confirmed is False
    assert not isinstance(caught.value, PermissionError)
    assert [row[0] for row in calls] == ["buffer", "create", "rename", "close"]


def test_buffer_setup_failure_precedes_handle_open(monkeypatch):
    host = _load_leaf(HOST_PATH)
    failure = MemoryError("buffer allocation")
    calls, state, *_ = _fake_kernel(monkeypatch, host, setup_error=failure)
    with pytest.raises(MemoryError) as caught:
        host.replace_worker_resource("worker.rss.tmp", "worker.rss")
    assert caught.value is failure and not state.committed
    assert [row[0] for row in calls] == ["buffer"]


@pytest.mark.parametrize("implementation,platform", [
    ("cpython", "win32"), ("cpython", "linux"), ("pcc", "win32"), ("pypy", "win32"),
])
def test_host_and_native_windows_dispatch_to_their_extended_rename(monkeypatch, implementation, platform):
    policy = _load_leaf(POLICY_PATH)
    original_import = builtins.__import__
    imports, calls = [], []

    def rename(source, destination):
        calls.append(("host", source, destination))

    def ordinary(source, destination):
        calls.append(("ordinary", source, destination))

    def native(source, destination, diagnostic=None):
        calls.append(("native", source, destination))
        return 0

    native_types = SimpleNamespace(c_obj=object(), c_int64=object(), c_ptr=object(), c_str=object())

    def declare(symbol, arguments, result):
        if symbol == "pcc_worker_resource_replace_detailed":
            assert arguments == (native_types.c_str, native_types.c_str, native_types.c_int64)
            assert result is native_types.c_int64
            return native
        if symbol == "py_os_replace":
            assert arguments == (native_types.c_obj, native_types.c_obj)
            assert result is native_types.c_obj
            return native
        assert symbol == "py_err_occurred" and arguments == ()
        assert result is native_types.c_int64
        return lambda: 0

    native_types.extern = declare

    def importing(name, *args, **options):
        imports.append(name)
        if name == "sys":
            return SimpleNamespace(implementation=SimpleNamespace(name=implementation), platform=platform)
        if name == HOST_MODULE:
            return SimpleNamespace(replace_worker_resource=rename)
        if name == "pcc.extern":
            return native_types
        if name == "pcc.unsafe":
            return SimpleNamespace(stack_alloc=lambda size: [0, 0, 0],
                                   load_i64=lambda output, offset: output[offset // 8])
        assert name != "ctypes" and not name.startswith("pcc.")
        return original_import(name, *args, **options)

    monkeypatch.setattr(policy, "os", SimpleNamespace(replace=ordinary))
    with monkeypatch.context() as selected:
        selected.setattr(builtins, "__import__", importing)
        assert policy._replace_worker_resource("worker.rss.tmp", "worker.rss") is None
    host = (implementation, platform) == ("cpython", "win32")
    native = implementation == "pcc"
    assert calls == [("host" if host else "native" if native else "ordinary", "worker.rss.tmp", "worker.rss")]
    assert imports == (["sys", HOST_MODULE] if host else ["sys", "pcc.extern", "pcc.unsafe"] if native else ["sys"])


def test_actual_publisher_dispatcher_preserves_native_import_boundary():
    scan = _load_leaf(SCAN_PATH)
    source = (ROOT / POLICY_PATH).read_text()
    native = scan._without_inactive_runtime_imports(source)
    host = scan._without_inactive_implementation_imports(source, implementation="cpython")
    assert HOST_MODULE not in native and HOST_MODULE in host
    assert "import ctypes" not in native and "FileRenameInfoEx" not in native
    tree = ast.parse(source)
    dispatch = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "_replace_worker_resource")
    assert ast.unparse(dispatch.body[0]) == "import sys as execution_runtime"
    assert ast.unparse(dispatch.body[1].test) == "execution_runtime.implementation.name == 'cpython'"
    assert ast.unparse(dispatch.body[1].body[0].test) == "execution_runtime.platform == 'win32'"
    ordinary = dispatch.body[2]
    assert ast.unparse(ordinary.test) == "execution_runtime.implementation.name != 'pcc'"
    assert ast.unparse(ordinary.body[0]) == "os.replace(temporary, path)"
    assert isinstance(ordinary.body[1], ast.Return)
    native_import = next(node for node in dispatch.body if isinstance(node, ast.ImportFrom))
    assert native_import.module == "pcc.extern"
    assert [name.name for name in native_import.names] == ["c_int64", "c_obj", "c_str", "extern"]
    assert all(isinstance(node, ast.If) for node in dispatch.body[3:5])
    assert dispatch.body[4].end_lineno < native_import.lineno
    native_windows = dispatch.body[-6]
    assert ast.unparse(native_windows.test) == "execution_runtime.platform == 'win32'"
    declaration = next(node for node in native_windows.body if isinstance(node, ast.Assign)
                       and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "native_publish")
    assert ast.unparse(declaration) == (
        "native_publish = extern('pcc_worker_resource_replace_detailed', (c_str, c_str, c_int64), c_int64)")
    assert isinstance(native_windows.body[-1], ast.Return)
    tail = [ast.unparse(node) for node in dispatch.body[-5:]]
    assert tail == [
        "native_replace = extern('py_os_replace', (c_obj, c_obj), c_obj)",
        "native_error_pending = extern('py_err_occurred', (), c_int64)",
        "result = native_replace(temporary, path)",
        "if native_error_pending():\n    raise", "del result",
    ]
    publisher = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name == "publish_worker_resource")
    write = next(node for node in publisher.body if isinstance(node, ast.With))
    call = next(node for node in ast.walk(publisher) if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name) and node.func.id == "_replace_worker_resource")
    assert write.end_lineno < call.lineno
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                   and node.func.id == "_replace_worker_resource" for node in ast.walk(write))
    assert "deadline = time.monotonic() + 1.0" in source
    assert 'winerror = getattr(error, "winerror", None)' in source
    assert 'if winerror is None and execution_runtime.implementation.name == "pcc":' in source
    assert "retryable = winerror in (5, 32)" in source
    assert 'retryable = execution_runtime.platform == "win32" and error.errno == 13' in source


def _native_import_control(policy, events, *, platform="linux", pending=False, call_error=None):
    """Inert declaration provider; never imports PCC or calls a native symbol."""
    original_import = builtins.__import__
    types = SimpleNamespace(c_obj=object(), c_int64=object(), c_ptr=object(), c_str=object())

    class Result:
        def __del__(self):
            events.append(("release",))

    def replace(source, destination, diagnostic=None):
        events.append(("replace", source, destination))
        if call_error is not None:
            raise call_error
        if platform == "win32":
            diagnostic[:] = [5, 32, 0] if pending else [0, 0, 0]
            return -13 if pending else 0
        return Result()

    def error_pending():
        events.append(("pending",))
        assert ("release",) not in events
        return int(pending)

    def declare(symbol, arguments, result):
        events.append(("declare", symbol))
        if symbol == "pcc_worker_resource_replace_detailed":
            assert arguments == (types.c_str, types.c_str, types.c_int64) and result is types.c_int64
            return replace
        if symbol == "py_os_replace":
            assert arguments == (types.c_obj, types.c_obj) and result is types.c_obj
            return replace
        assert symbol == "py_err_occurred" and arguments == () and result is types.c_int64
        return error_pending

    types.extern = declare

    def importing(name, *args, **options):
        events.append(("import", name))
        if name == "sys":
            return SimpleNamespace(implementation=SimpleNamespace(name="pcc"), platform=platform)
        if name == "pcc.extern":
            return types
        if name == "pcc.unsafe":
            return SimpleNamespace(stack_alloc=lambda size: [0, 0, 0],
                                   load_i64=lambda output, offset: output[offset // 8])
        assert name != "ctypes" and not name.startswith("pcc.")
        return original_import(name, *args, **options)

    return importing


@pytest.mark.parametrize("platform", ["linux", "win32", "darwin"])
@pytest.mark.parametrize("pending", [False, True])
def test_native_report_abi_preserves_platform_error_and_ownership_contract(monkeypatch, platform, pending):
    policy = _load_leaf(POLICY_PATH)
    events = []
    importing = _native_import_control(policy, events, platform=platform, pending=pending)
    failure = OSError(errno.EACCES, "mock pending native error")
    if pending:
        # CPython's active exception models the native pending-error register
        # for this bare-raise control. No real TLS/FFI operation is performed.
        with pytest.raises(OSError) as caught:
            try:
                raise failure
            except OSError:
                with monkeypatch.context() as selected:
                    selected.setattr(builtins, "__import__", importing)
                    policy._replace_worker_resource("worker.rss.tmp", "worker.rss")
        if platform == "win32":
            assert isinstance(caught.value, PermissionError) and caught.value.errno == 13
            assert caught.value.filename == "worker.rss.tmp" and caught.value.filename2 == "worker.rss"
            assert "stage=open_source winerror=32 handle_cleanup_winerror=0" in str(caught.value)
            assert getattr(caught.value, "winerror", None) is None
        else:
            assert caught.value is failure
    else:
        with monkeypatch.context() as selected:
            selected.setattr(builtins, "__import__", importing)
            assert policy._replace_worker_resource("worker.rss.tmp", "worker.rss") is None
        if platform != "win32":
            assert events[-1] == ("release",) and events.count(("release",)) == 1
    if platform == "win32":
        assert events == [("import", "sys"), ("import", "pcc.extern"), ("import", "pcc.unsafe"),
                          ("declare", "pcc_worker_resource_replace_detailed"),
                          ("replace", "worker.rss.tmp", "worker.rss")]
        return
    assert events[:6] == [
        ("import", "sys"), ("import", "pcc.extern"),
        ("declare", "py_os_replace"), ("declare", "py_err_occurred"),
        ("replace", "worker.rss.tmp", "worker.rss"), ("pending",),
    ]


@pytest.mark.parametrize("temporary,path,error", [
    (b"worker.rss.tmp", "worker.rss", TypeError),
    ("worker.rss.tmp", b"worker.rss", TypeError),
    (None, "worker.rss", TypeError), ("worker.rss.tmp", None, TypeError),
    ("worker\0.rss.tmp", "worker.rss", ValueError),
    ("worker.rss.tmp", "worker\0.rss", ValueError),
])
def test_native_report_invalid_paths_fail_before_declaration_or_rename(monkeypatch, temporary, path, error):
    policy = _load_leaf(POLICY_PATH)
    events = []
    importing = _native_import_control(policy, events)
    with pytest.raises(error):
        with monkeypatch.context() as selected:
            selected.setattr(builtins, "__import__", importing)
            policy._replace_worker_resource(temporary, path)
    assert events == [("import", "sys")]


def test_native_report_raised_call_is_not_retried_or_replaced(monkeypatch):
    policy = _load_leaf(POLICY_PATH)
    events = []
    failure = PermissionError(errno.EACCES, "mock rename failure")
    importing = _native_import_control(policy, events, call_error=failure)
    with pytest.raises(PermissionError) as caught:
        with monkeypatch.context() as selected:
            selected.setattr(builtins, "__import__", importing)
            policy._replace_worker_resource("worker.rss.tmp", "worker.rss")
    assert caught.value is failure
    assert events == [
        ("import", "sys"), ("import", "pcc.extern"),
        ("declare", "py_os_replace"), ("declare", "py_err_occurred"),
        ("replace", "worker.rss.tmp", "worker.rss"),
    ]
