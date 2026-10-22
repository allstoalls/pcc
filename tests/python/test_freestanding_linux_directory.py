"""Source-extracted directory-open behavior with fake kernel/memory callbacks.

These host tests import no PCC module and perform no real syscall or FFI.
Native emitted execution is covered separately by the os/tempfile tests.
"""

import ast
from pathlib import Path

import pytest


_SOURCE = (
    Path(__file__).absolute().parents[2]
    / "pcc/runtime/py/freestanding_linux_directory.py"
)


@pytest.fixture(params=[
    ("x86_64", 257, 65536 | 524288),
    ("aarch64", 56, 16384 | 524288),
    ("arm64", 56, 16384 | 524288),
])
def target(request):
    return request.param


def _capture_directory_open(machine, fd, *, malloc_fails=False):
    module = ast.parse(_SOURCE.read_text(encoding="utf-8"), filename=str(_SOURCE))
    function = next(
        node for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "directory_open"
    )
    function.decorator_list = []
    events = []
    path = object()
    stream = object()
    null_pointer = object()
    allocated = null_pointer if malloc_fails else stream
    machine_bytes = machine.encode("ascii") + b"\0"

    def callback(name, result=None):
        def invoke(*args):
            events.append((name, *args))
            return result
        return invoke

    def load_i8(pointer, offset):
        events.append(("load_i8", pointer, offset))
        return pointer[offset]

    def ptr_is_null(pointer):
        events.append(("ptr_is_null", pointer))
        return pointer is null_pointer

    namespace = {
        "__builtins__": {},
        "c_ptr": object,
        "i64": int,
        "target_platform_machine": callback("target_platform_machine", machine_bytes),
        "load_i8": load_i8,
        "syscall6": callback("syscall6", fd),
        "pcc_errno_set": callback("pcc_errno_set"),
        "null": callback("null", null_pointer),
        "malloc": callback("malloc", allocated),
        "ptr_is_null": ptr_is_null,
        # A close failure must not overwrite the allocation failure's ENOMEM.
        "close": callback("close", -9),
        "store_i64": callback("store_i64"),
    }
    extracted = ast.Module(body=[function], type_ignores=[])
    exec(compile(extracted, str(_SOURCE), "exec"), namespace)
    return namespace["directory_open"], events, path, stream, null_pointer


def _open_events(target, path):
    machine, number, flags = target
    return [
        ("target_platform_machine",),
        ("load_i8", machine.encode("ascii") + b"\0", 0),
        ("syscall6", number, -100, path, flags, 0, 0, 0),
    ]


@pytest.mark.parametrize("fd", [0, 37])
def test_directory_open_stores_fd_and_returns_exact_stream(target, fd):
    function, events, path, stream, _null = _capture_directory_open(target[0], fd)

    assert function(path) is stream
    assert events == _open_events(target, path) + [
        ("malloc", 32800),
        ("ptr_is_null", stream),
        ("store_i64", stream, 0, fd),
        ("store_i64", stream, 8, 0),
        ("store_i64", stream, 16, 0),
        ("store_i64", stream, 24, 0),
    ]


@pytest.mark.parametrize("fd", [-2, -13, -22])
def test_directory_open_propagates_open_errno_without_allocation(target, fd):
    function, events, path, _stream, null_pointer = _capture_directory_open(target[0], fd)

    assert function(path) is null_pointer
    assert events == _open_events(target, path) + [
        ("pcc_errno_set", -fd),
        ("null",),
    ]


@pytest.mark.parametrize("fd", [0, 37])
def test_directory_open_closes_fd_before_reporting_allocation_failure(target, fd):
    function, events, path, _stream, null_pointer = _capture_directory_open(
        target[0], fd, malloc_fails=True,
    )

    assert function(path) is null_pointer
    assert events == _open_events(target, path) + [
        ("malloc", 32800),
        ("ptr_is_null", null_pointer),
        ("close", fd),
        ("pcc_errno_set", 12),
        ("null",),
    ]


def _assert_raw_flag_selection(statement):
    assert isinstance(statement, ast.AnnAssign)
    assert isinstance(statement.target, ast.Name) and statement.target.id == "flags"
    assert isinstance(statement.annotation, ast.Name) and statement.annotation.id == "i64"
    selection = statement.value
    assert isinstance(selection, ast.IfExp)
    assert isinstance(selection.test, ast.Name) and selection.test.id == "arm"
    assert isinstance(selection.body, ast.Constant) and type(selection.body.value) is int
    assert isinstance(selection.orelse, ast.Constant) and type(selection.orelse.value) is int
    assert selection.body.value == (16384 | 524288)
    assert selection.orelse.value == (65536 | 524288)


def test_directory_open_flags_keep_top_level_raw_literal_selection():
    # The raw annotation contextualizer handles a top-level conditional's
    # literal arms. A BinOp containing that conditional stays semantic int.
    # This is a source-shape guard, not a PCC compilation test.
    module = ast.parse(_SOURCE.read_text(encoding="utf-8"), filename=str(_SOURCE))
    function = next(
        node for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "directory_open"
    )
    flags = next(
        node for node in function.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name) and node.target.id == "flags"
    )
    _assert_raw_flag_selection(flags)
    rejected = ast.parse("flags: i64 = (16384 if arm else 65536) | 524288").body[0]
    with pytest.raises(AssertionError):
        _assert_raw_flag_selection(rejected)
