"""Source checks and real emitted witnesses for ordinary OS/path providers.

The host ABI shims exercise provider adaptation and Python call semantics.
They are not runtime implementation or emitted-execution qualification. The
integration case requires a separately admitted source-matched owned archive.
"""

from __future__ import annotations

import gc
import hashlib
import importlib.util
import json
import operator
import os
from pathlib import Path
import posixpath
import sys
from types import SimpleNamespace

import pytest

from tests.python.owned_regression_support import (
    _record_execution,
    explicit_owned_runtime,
)
from tests.python.test_native_namespace_projection_behaviors import (
    _native_format,
    _observed_collectors,
)
from tests.python.process_timeout import run_process_group_timeout


ROOT = Path(__file__).resolve().parents[2]
PROVIDER = ROOT / "pcc/stdlib/os.py"
CONTROL = ROOT / "tests/fixtures/native/owned_os_path_provider.py"
EXPECTED = "OWNED_OS_PATH_PROVIDER_OK\n"


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_provider(*, native):
    provider = load_module("owned_os_path_provider", PROVIDER)
    provider._getpid = os.getpid
    provider._access = lambda path, mode: 0 if os.access(path, mode) else -1
    if not native:
        return provider
    provider._native_sys = SimpleNamespace(
        implementation=SimpleNamespace(name="pcc"), platform=sys.platform,
    )
    provider._error_pending = lambda: False
    provider.ptr_is_null = lambda value: value is None
    provider._index_value = operator.index
    provider.stack_alloc = bytearray
    provider.store_i32 = lambda b, o, v: b.__setitem__(slice(o, o + 4), v.to_bytes(4, "little", signed=True))
    provider.load_i32 = lambda b, o: int.from_bytes(b[o:o + 4], "little", signed=True)

    def integer(value, overflow):
        result = int.__index__(value)
        if result < -(2 ** 63) or result >= 2 ** 63:
            provider.store_i32(overflow, 0, 1)
            return 0
        return result

    provider._fd_integer = integer
    # Snapshot actual host callables: provider namespace replacement must not
    # turn a shim into a lookup through the namespace under examination.
    for name in ("getcwd", "cpu_count", "uname", "listdir", "makedirs", "kill",
                 "access", "chmod", "write", "unlink", "rmdir", "replace",
                 "urandom", "putenv"):
        if hasattr(os, name):
            setattr(provider, "_os_" + name, getattr(os, name))
    provider._environ_unset = os.unsetenv
    provider._path_islink_result = os.path.islink
    provider._file_digest = lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
    def bounded_digest(path, limit):
        data = Path(path).read_bytes()
        return hashlib.sha256(data).hexdigest() if 0 < limit and len(data) <= limit else ""
    provider._file_digest_bounded = bounded_digest
    provider._path_join = lambda values: os.path.join(*values)
    for name in ("basename", "dirname", "split", "exists", "isabs", "isfile",
                 "isdir", "getmtime", "getsize", "abspath",
                 "commonpath", "commonprefix", "splitext", "normcase",
                 "normpath", "splitdrive", "expanduser", "expandvars",
                 "relpath", "realpath"):
        setattr(provider, "_path_" + name, getattr(os.path, name))
    return provider


def test_reference_provider_witness(tmp_path):
    result = _record_execution(
        tmp_path, "reference", [sys.executable, "-B", str(CONTROL), str(tmp_path)],
        dict(os.environ, PYTHONDONTWRITEBYTECODE="1",
             PYTHONWARNINGS="ignore::DeprecationWarning"),
    )
    assert (result["returncode"], result["stdout"], result["stderr"]) == (0, EXPECTED, "")


@pytest.mark.pcc_gate(probe=lambda: os.name == "posix")
def test_reference_removed_cwd_witness(tmp_path):
    working = tmp_path / "removed-cwd"
    working.mkdir()
    result = run_process_group_timeout(
        [sys.executable, "-B", str(CONTROL), "--cwd-error", str(working)],
        cwd=working, timeout=10, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, "OWNED_OS_CWD_ERROR_OK\n", "")
    assert not working.exists()


@pytest.mark.parametrize("native", (False, True), ids=("host-provider", "modeled-owned-abi"))
def test_source_provider_runs_identical_witness(tmp_path, native, capsys):
    provider = load_provider(native=native)
    control = load_module("owned_os_path_control", CONTROL)
    control.os = provider
    if native:
        control.sys = SimpleNamespace(implementation=SimpleNamespace(name="pcc"), argv=sys.argv)
    control.main(str(tmp_path))
    assert capsys.readouterr().out == EXPECTED


def test_native_signature_boundaries_are_explicit(tmp_path):
    provider = load_provider(native=True)
    value = str(tmp_path)
    with pytest.raises(NotImplementedError, match="bytes paths"):
        provider.path.abspath(value.encode())
    with pytest.raises(NotImplementedError, match="bytes paths"):
        provider.listdir(value.encode())
    with pytest.raises(NotImplementedError, match="directory descriptors"):
        provider.listdir(0)
    with pytest.raises(NotImplementedError, match="file descriptors"):
        provider.path.isfile(0)
    with pytest.raises(NotImplementedError, match="strict"):
        provider.path.realpath(value, strict=True)
    with pytest.raises(NotImplementedError, match="dir_fd"):
        provider.unlink("missing", dir_fd=0)
    with pytest.raises(NotImplementedError, match="directory descriptors"):
        provider.replace("missing", "destination", src_dir_fd=0)
    with pytest.raises(NotImplementedError, match="default path lookup"):
        provider.access(value, 0, follow_symlinks=False)
    # This helper already has the full bytes boundary; preserve it.
    assert provider.path.islink(value.encode()) is False


def test_path_protocol_runs_once_and_preserves_failure(tmp_path):
    provider = load_provider(native=True)
    calls = []
    class PathValue:
        def __fspath__(self):
            calls.append("fspath")
            gc.collect()
            return str(tmp_path)
        def __str__(self):
            raise AssertionError("unexpected path stringification")
    assert provider.path.abspath(PathValue()) == str(tmp_path)
    assert calls == ["fspath"]
    failure = LookupError("original path failure")
    class FailingPath:
        def __fspath__(self):
            raise failure
    with pytest.raises(LookupError) as captured:
        provider.path.abspath(FailingPath())
    assert captured.value is failure


def test_original_dirname_contract_is_preserved():
    provider = load_provider(native=False)
    for value in ("", "a", "/", "//", "///", "/a//b", "//a/b", "//a/", "a///b/", "/a/b///"):
        assert provider.path.dirname(value) == posixpath.dirname(value)


def test_original_join_and_normpath_limits_remain_explicit():
    provider = load_provider(native=False)
    assert provider.path.join("a", "b", "c") == "a/b/c"
    assert provider.path.join("a", "/b", "c") == "/b/c"
    assert provider.path.join("a/", "b") == "a/b"
    assert provider.path.normpath("a/./b/../c") == "a/c"
    assert provider.path.normpath("/a/../../b") == "/b"
    # These are retained source-provider differences, not changes introduced
    # by the missing-entry completion. They require their own repair scope.
    assert provider.path.join() == ""
    with pytest.raises(TypeError):
        posixpath.join()
    assert provider.path.join("", "child") == "/child"
    assert posixpath.join("", "child") == "child"
    assert provider.path.join("base", "") == "base"
    assert posixpath.join("base", "") == "base/"
    assert provider.path.normpath("//a//b") == "/a/b"
    assert posixpath.normpath("//a//b") == "//a/b"
    assert provider.path.commonpath(["/root", "relative"]) == ""
    with pytest.raises(ValueError):
        posixpath.commonpath(["/root", "relative"])


def test_scalar_width_checks_run_before_helpers():
    provider = load_provider(native=True)
    seen = []
    provider._os_write = lambda fd, data: seen.append((fd, data)) or len(data)
    class Descriptor:
        def __index__(self):
            seen.append("index")
            return 31
    assert provider.write(Descriptor(), b"ok") == 2
    assert seen == ["index", (31, b"ok")]
    seen.clear()
    for invalid in (2 ** 100, -(2 ** 100)):
        with pytest.raises(OverflowError):
            provider.write(invalid, b"never")
    with pytest.raises(TypeError):
        provider.write(3.5, b"never")
    assert seen == []


def test_owned_runtime_result_error_is_not_rewritten(tmp_path):
    provider = load_provider(native=True)
    failure = OSError(5, "original helper failure")
    def failed(path):
        raise failure
    provider._path_getsize = failed
    with pytest.raises(OSError) as captured:
        provider.path.getsize(str(tmp_path))
    assert captured.value is failure
    provider._path_islink_result = lambda path: failure
    with pytest.raises(OSError) as captured:
        provider.path.islink(str(tmp_path))
    assert captured.value is failure
    provider._os_cpu_count = lambda: 0
    assert provider.cpu_count() is None


def test_existing_download_abi_keeps_arguments_and_status(tmp_path):
    provider = load_provider(native=True)
    seen = []
    provider._http_download = lambda url, path: seen.append((url, path)) or -7
    target = tmp_path / "download"
    assert provider._pcc_http_download_to_file("https://example.invalid/test", target) == -7
    assert seen == [("https://example.invalid/test", str(target))]


@pytest.mark.parametrize("helper,public,args", (
    ("_os_getcwd", "getcwd", ()),
    ("_path_abspath", "abspath", (".",)),
    ("_path_relpath", "relpath", ("file",)),
    ("_path_realpath", "realpath", ("file",)),
))
def test_unclassified_null_result_never_becomes_success(helper, public, args):
    provider = load_provider(native=True)
    setattr(provider, helper, lambda *values: None)
    target = provider if public == "getcwd" else provider.path
    message = "NULL without an exception" if public == "getcwd" else "without an OS or allocation error status"
    with pytest.raises(RuntimeError, match=message):
        getattr(target, public)(*args)


def test_allocation_null_is_distinct_from_none_success():
    provider = load_provider(native=True)
    provider._path_split = lambda path: None
    with pytest.raises(MemoryError, match="result allocation failed"):
        provider.path.split("path")
    provider._os_kill = lambda pid, signal: None
    assert provider.kill(1, 0) is None


@pytest.mark.parametrize("platform", ("linux", "darwin", "win32"))
def test_write_adapts_target_errno_and_retries_without_reconverting(platform):
    provider = load_provider(native=True)
    provider._platform_name = platform
    events = []
    class Descriptor:
        def __index__(self):
            events.append("index")
            return 31
    values = [-1 if platform == "darwin" else -4, 2]
    provider._errno_get = lambda: 4
    provider._thread_safepoint = lambda: events.append("safepoint")
    def write(fd, payload):
        events.append((fd, payload))
        return values.pop(0)
    provider._os_write = write
    assert provider.write(Descriptor(), b"ok") == 2
    assert events == ["index", (31, b"ok"), "safepoint", (31, b"ok")]
    def failed(number):
        raise OSError(number, "target error")
    provider._os_error = failed
    provider._os_write = lambda fd, payload: -1 if platform == "darwin" else -9
    provider._errno_get = lambda: 9
    with pytest.raises(OSError) as captured:
        provider.write(31, b"x")
    assert captured.value.errno == 9


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_owned_os_path_provider_native_five_gc(
    tmp_path, request, monkeypatch, explicit_owned_runtime,
    python_program_compiler, capfd,
):
    """Run the exact fixture with actual files and signals under every GC."""
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    binary = tmp_path / "owned-os-path-provider.out"
    receipt = {
        "status": "RUNNING",
        "compiler_fixture_parameter": request.node.callspec.params["python_program_compiler"],
        "compiler_mode": ("pcc1-routed" if python_program_compiler.__module__ == "tests.pcc1_route"
                          else request.node.callspec.params["python_program_compiler"]),
        "backend": "self", "libpython": "off",
        "source_sha256": hashlib.sha256(CONTROL.read_bytes()).hexdigest(),
        "provider_sha256": hashlib.sha256(PROVIDER.read_bytes()).hexdigest(),
        "runtime_archive": str(explicit_owned_runtime),
        "runtime_sha256": hashlib.sha256(explicit_owned_runtime.read_bytes()).hexdigest(),
        "executions": [],
    }
    record = tmp_path / "owned-os-path-provider.json"
    record.write_text(json.dumps(receipt, indent=2) + "\n")
    try:
        python_program_compiler(
            str(CONTROL), str(binary), backend="self", libpython_mode="off",
            ir_scaffold_mode="on", runtime_archive=str(explicit_owned_runtime),
        )
    except Exception as error:
        receipt["status"] = "COMPILE_FAILED"
        receipt["error"] = type(error).__name__ + ": " + str(error)
        record.write_text(json.dumps(receipt, indent=2) + "\n")
        raise
    finally:
        captured = capfd.readouterr()
        (tmp_path / "compiler.stdout").write_text(captured.out)
        (tmp_path / "compiler.stderr").write_text(captured.err)
    receipt["binary_sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
    receipt["binary_format"] = _native_format(binary)
    assert receipt["binary_format"] is not None, "Compiler output is not a native executable"
    for backend in range(5):
        root = tmp_path / ("gc" + str(backend))
        root.mkdir()
        log_path = root / "gc.jsonl"
        environment = dict(os.environ, PCC_GC_BACKEND=str(backend),
                           PCC_GC_REFCOUNT_PROVENANCE_PROBE="2", PATH="",
                           PCC_HOST_PYTHON="/nonexistent/host-python",
                           PCC_HOST_PCC="/nonexistent/host-pcc",
                           PCC_LOG="gc", PCC_LOG_FORMAT="json", PCC_LOG_FILE=str(log_path))
        result = _record_execution(
            root, "native", [str(binary), str(root)], environment,
        )
        observed = _observed_collectors(log_path)
        receipt["executions"].append({"gc_backend": backend, "observed_gc": observed,
                                      "gc_log_sha256": hashlib.sha256(log_path.read_bytes()).hexdigest(),
                                      **result})
        passed = (result["returncode"], result["stdout"], result["stderr"]) == (0, EXPECTED, "")
        passed = passed and observed == [backend]
        if not passed:
            receipt["status"] = "NATIVE_EXECUTION_FAILED"
        record.write_text(json.dumps(receipt, indent=2) + "\n")
        assert passed, (backend, result)
        if os.name == "posix":
            working = root / "removed-cwd"
            working.mkdir()
            error_log = root / "gc-cwd-error.jsonl"
            error_environment = dict(environment, PCC_LOG_FILE=str(error_log))
            command = [str(binary), "--cwd-error", str(working)]
            outcome = run_process_group_timeout(command, cwd=working, timeout=10,
                                                env=error_environment)
            row = {"gc_backend": backend, "scenario": "removed-cwd", "command": command,
                   "returncode": outcome.returncode, "stdout": outcome.stdout,
                   "stderr": outcome.stderr, "observed_gc": _observed_collectors(error_log),
                   "gc_log_sha256": hashlib.sha256(error_log.read_bytes()).hexdigest()}
            receipt["executions"].append(row)
            error_passed = (outcome.returncode, outcome.stdout, outcome.stderr) == (
                0, "OWNED_OS_CWD_ERROR_OK\n", "",
            ) and row["observed_gc"] == [backend]
            if not error_passed:
                receipt["status"] = "NATIVE_EXECUTION_FAILED"
            record.write_text(json.dumps(receipt, indent=2) + "\n")
            assert error_passed, row
    receipt["status"] = "PASS"
    record.write_text(json.dumps(receipt, indent=2) + "\n")
