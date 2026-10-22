"""Execute the real getcwd error bodies over the existing owned ABI model.

These bounded host models prove error classification and balanced cleanup.
Actual target syscalls and emitted execution require a rebuilt owned runtime.
"""

import errno
import ast
import os
from pathlib import Path

import pytest

from tests.python.test_owned_fdopen_provider import FileRuntime
from tests.python.test_foreign_address_leases import _functions


ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "pcc/runtime/py"


def cwd_runtime():
    runtime = FileRuntime()
    runtime.load(RUNTIME / "py_os_substrate.py")
    runtime.load_selected(RUNTIME / "freestanding_errno.py", {"pcc_errno_exception_kind"})
    return runtime


@pytest.mark.parametrize("number,kind", ((errno.ENOENT, 34), (errno.EACCES, 36), (errno.ERANGE, 14)))
def test_platform_failure_keeps_errno_subclass_message_and_args(number, kind):
    runtime = cwd_runtime()
    runtime.errno = number
    runtime.ns["pcc_platform_getcwd"] = lambda buffer, size: None
    assert runtime.ns["py_os_getcwd_str"]() is None
    error = runtime.error
    assert error.value[0] == kind
    assert error.attrs["errno"] == number
    assert error.attrs["strerror"].value == os.strerror(number)
    assert error.attrs["args"].value[0] == number
    assert error.attrs["args"].value[1] is error.attrs["strerror"]
    assert not runtime.frames and not runtime.leases


def test_raw_buffer_allocation_failure_is_memory_error_not_stale_errno():
    runtime = cwd_runtime()
    runtime.errno = errno.ENOENT
    runtime.ns["malloc"] = lambda size: None
    def forbidden(buffer, size):
        raise AssertionError("platform must not receive a missing buffer")
    runtime.ns["pcc_platform_getcwd"] = forbidden
    assert runtime.ns["py_os_getcwd_str"]() is None
    assert runtime.error.value[0] == 19
    assert not runtime.frames and not runtime.leases


def successful_platform(runtime):
    def getcwd(buffer, size):
        value = b"/owned/current\0"
        assert size >= len(value)
        buffer.memory.data[buffer.offset:buffer.offset + len(value)] = value
        return buffer
    return getcwd


def test_success_returns_independent_boxed_strings_and_keeps_errno():
    runtime = cwd_runtime()
    runtime.errno = errno.EACCES
    runtime.ns["pcc_platform_getcwd"] = successful_platform(runtime)
    first = runtime.ns["py_os_getcwd_str"]()
    second = runtime.ns["py_os_getcwd_str"]()
    assert first.value == second.value == "/owned/current"
    assert first is not second
    assert runtime.errno == errno.EACCES and runtime.error is None
    assert not runtime.frames and not runtime.leases


def test_boxing_allocation_failure_has_a_pending_memory_error():
    runtime = cwd_runtime()
    runtime.ns["pcc_platform_getcwd"] = successful_platform(runtime)
    runtime.ns["py_str_new"] = lambda pointer, size: None
    assert runtime.ns["py_os_getcwd_str"]() is None
    assert runtime.error.value[0] == 19
    assert not runtime.frames and not runtime.leases


@pytest.mark.parametrize("allocation", ("py_exc_new", "py_str_new", "py_tuple_new"))
def test_error_construction_failure_releases_partial_owners(allocation):
    runtime = cwd_runtime()
    runtime.errno = errno.ENOENT
    runtime.ns["pcc_platform_getcwd"] = lambda buffer, size: None
    original = runtime.ns[allocation]
    first = True
    def fail_once(*args):
        nonlocal first
        if first:
            first = False
            return None
        return original(*args)
    runtime.ns[allocation] = fail_once
    assert runtime.ns["py_os_getcwd_str"]() is None
    assert runtime.error.value[0] == 19
    assert not runtime.frames and not runtime.leases


def test_platform_contract_failure_never_invents_errno():
    runtime = cwd_runtime()
    runtime.errno = 0
    runtime.ns["pcc_platform_getcwd"] = lambda buffer, size: None
    assert runtime.ns["py_os_getcwd_str"]() is None
    assert runtime.error.value[0] == 7
    assert "did not publish errno" in runtime.error.value[1]
    assert "errno" not in runtime.error.attrs
    assert not runtime.frames and not runtime.leases


@pytest.mark.parametrize("machine,number", (("x86_64", 79), ("aarch64", 17)))
@pytest.mark.parametrize("status", (-errno.ENOENT, -errno.EACCES, 14))
def test_linux_leaf_preserves_real_kernel_status(machine, number, status):
    calls, errors = [], []
    buffer = object()
    namespace = {
        "target_platform_machine": lambda: machine,
        "load_i8": lambda text, offset: ord(text[offset]),
        "syscall6": lambda *args: calls.append(args) or status,
        "pcc_errno_set": errors.append,
        "null": lambda: None,
    }
    _functions(RUNTIME / "freestanding_linux_libc.py", {"getcwd_c"}, namespace)
    result = namespace["getcwd_c"](buffer, 8192)
    assert result is (None if status < 0 else buffer)
    assert calls == [(number, buffer, 8192, 0, 0, 0, 0)]
    assert errors == ([-status] if status < 0 else [])


@pytest.mark.parametrize("scenario,counts,winerror,encoded,expected", (
    ("first-query", (0,), 5, 0, errno.EACCES),
    ("second-query", (5, 0), 3, 0, errno.ENOENT),
    ("wide-allocation", (5,), 5, 0, errno.ENOMEM),
    ("grown-buffer", (5, 6), 5, 0, errno.ERANGE),
    ("encoding-buffer", (5, 4), 122, 0, errno.ERANGE),
    ("encoding-api", (5, 4), 5, 0, errno.EACCES),
    ("success", (5, 4), 5, 4, None),
))
def test_windows_status_is_captured_before_free(scenario, counts, winerror, encoded, expected):
    queries = list(counts)
    errors, events = [], []
    last_error = [winerror]
    buffer, wide = object(), object()
    def free(value):
        assert value is wide
        events.append("free")
        last_error[0] = 87
    def get_error():
        events.append("GetLastError")
        return last_error[0]
    namespace = {
        "GetCurrentDirectoryW": lambda size, pointer: queries.pop(0),
        "GetLastError": get_error,
        "WideCharToMultiByte": lambda *args: encoded,
        "malloc": lambda size: None if scenario == "wide-allocation" else wide,
        "free": free, "pcc_errno_set": errors.append,
        "ptr_is_null": lambda value: value is None,
        "null": lambda: None,
    }
    _functions(RUNTIME / "freestanding_windows.py", {"error", "error_code", "getcwd", "getcwd_c"}, namespace)
    result = namespace["getcwd_c"](buffer, 8192)
    assert result is (buffer if expected is None else None)
    assert errors == ([] if expected is None else [expected])
    if "free" in events:
        assert "GetLastError" not in events[events.index("free") + 1:]


def test_platform_call_uses_named_standard_abi_and_matching_exports():
    path = RUNTIME / "freestanding_platform_fs.py"
    tree = ast.parse(path.read_text())
    unsafe_names = {alias.name for node in tree.body
                    if isinstance(node, ast.ImportFrom) and node.module == "pcc.unsafe"
                    for alias in node.names}
    assert "getcwd" not in unsafe_names
    declaration = next(node.value for node in tree.body
                       if isinstance(node, ast.Assign)
                       and any(isinstance(target, ast.Name) and target.id == "_getcwd_c"
                               for target in node.targets))
    assert ast.literal_eval(declaration.args[0]) == "getcwd"
    assert tuple(value.id for value in declaration.args[1].elts) == ("c_ptr", "c_size_t")
    assert declaration.args[2].id == "c_ptr"
    calls = []
    result = object()
    namespace = {"_getcwd_c": lambda *args: calls.append(args) or result}
    _functions(path, {"pcc_platform_getcwd"}, namespace)
    buffer = object()
    assert namespace["pcc_platform_getcwd"](buffer, 8192) is result
    assert calls == [(buffer, 8192)]
    for filename in ("freestanding_linux_libc.py", "freestanding_windows.py"):
        tree = ast.parse((RUNTIME / filename).read_text())
        leaf = next(node for node in tree.body
                    if isinstance(node, ast.FunctionDef) and node.name == "getcwd_c")
        exported = leaf.decorator_list[0]
        assert exported.func.id == "c_abi_typed_export"
        assert tuple(ast.literal_eval(value) for value in exported.args) == (
            "getcwd", "ptr", ("ptr", "u64"),
        )
