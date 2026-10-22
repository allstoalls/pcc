"""Production RSS leaf, host-executed against the real Win64 filesystem APIs.

These bounded tests execute the unchanged freestanding Python function bodies
with explicit raw-memory/ctypes bindings. They do not compile or execute pcc1,
build a runtime archive or PE image, or establish generated-code correctness.
The fixture retains the directory owner's exact access/share contract. It never
changes a DACL, permissions, or the production owner/lifecycle implementation.
"""

from __future__ import annotations

import ast
from contextlib import contextmanager
import ctypes
from pathlib import Path
import struct
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.pcc_gate(probe=lambda: (
    sys.platform == "win32"
    and sys.implementation.name == "cpython"
    and ctypes.sizeof(ctypes.c_void_p) == 8
))

OWNER_ACCESS = 1114241  # DELETE | SYNCHRONIZE | READ_ATTRIBUTES | LIST_DIRECTORY.
OWNER_SHARE = 1        # FILE_SHARE_READ only, including throughout each rename.
INVALID_HANDLE = ctypes.c_void_p(-1).value


def _load_functions(path, names, namespace):
    """Strip export decorators only; execute the production bodies verbatim."""
    nodes = []
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            node.decorator_list = []
            nodes.append(node)
        elif isinstance(node, ast.Assign) and all(
            isinstance(target, ast.Name) and target.id.startswith("_RENAME_")
            for target in node.targets
        ):
            nodes.append(node)
    assert {node.name for node in nodes if isinstance(node, ast.FunctionDef)} == names
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec"), namespace)


class HostLeaf:
    """Only ABI adapters: no emulated WinAPI outcomes or copied rename logic."""

    def __init__(self):
        assert sys.platform == "win32" and ctypes.sizeof(ctypes.c_void_p) == 8
        self.memory = {}
        self.heap = set()
        self.handles = set()
        self.events = []
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        pointer, dword, signed = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int32

        def bind(library, name, result, arguments):
            function = getattr(library, name)
            function.restype, function.argtypes = result, arguments
            return function

        self.create_file = bind(self.kernel, "CreateFileW", pointer,
                                [pointer, dword, dword, pointer, dword, dword, pointer])
        self.close_handle = bind(self.kernel, "CloseHandle", signed, [pointer])
        self.set_information = bind(self.kernel, "SetFileInformationByHandle", signed,
                                    [pointer, signed, pointer, dword])
        self.nt_set_information = bind(self.ntdll, "NtSetInformationFile", signed,
                                       [pointer, pointer, pointer, dword, signed])
        self.get_information = bind(self.kernel, "GetFileInformationByHandleEx", signed,
                                    [pointer, signed, pointer, dword])
        self.read_file = bind(self.kernel, "ReadFile", signed,
                              [pointer, pointer, dword, pointer, pointer])
        self.namespace = {
            "i64": int, "c_ptr": int,
            "malloc": self.malloc, "free": self.free,
            "stack_alloc": self.allocate,
            "null": lambda: 0, "ptr_is_null": lambda address: not address,
            "int_to_ptr": int,
            "ptr_to_int": lambda address: -1 if address == INVALID_HANDLE else int(address or 0),
            "ptr_add": lambda address, offset: address + offset,
            "load_i8": lambda address, offset: self.read(address + offset, 1)[0],
            "load_i32": lambda address, offset: self.load(address + offset, 4),
            "load_ptr": lambda address, offset: self.load(address + offset, 8),
            "store_ptr": lambda address, offset, value: self.store(address + offset, value, 8),
            "store_i32": lambda address, offset, value: self.store(address + offset, value, 4),
            "store_i64": lambda address, offset, value: self.store(address + offset, value, 8),
            "memset": lambda address, value, size: self.write(address, bytes([value]) * size),
            "memcpy": lambda destination, source, size: self.write(destination, self.read(source, size)),
            "GetFullPathNameW": bind(self.kernel, "GetFullPathNameW", dword,
                                      [pointer, dword, pointer, pointer]),
            "MultiByteToWideChar": bind(self.kernel, "MultiByteToWideChar", signed,
                                         [dword, dword, pointer, signed, pointer, signed]),
            "GetLastError": ctypes.get_last_error,
            "RtlNtStatusToDosError": bind(self.ntdll, "RtlNtStatusToDosError", signed, [signed]),
            "CreateFileW": self.open_source, "CloseHandle": self.close_source,
            "GetFileInformationByHandleEx": self.source_information,
            "NtSetInformationFile": self.rename,
        }
        _load_functions(ROOT / "pcc/runtime/py/freestanding_windows.py",
                        {"utf16", "error_code"}, self.namespace)
        _load_functions(ROOT / "pcc/runtime/py/freestanding_windows_directory.py",
                        {"tempdir_component_valid", "tempdir_dos_leaf_valid"}, self.namespace)
        self.namespace["component_valid"] = self.namespace["tempdir_component_valid"]
        self.namespace["dos_leaf_valid"] = self.namespace["tempdir_dos_leaf_valid"]
        _load_functions(ROOT / "pcc/runtime/py/freestanding_windows_rss.py", {
            "_worker_resource_full_path", "_worker_resource_full_path_detailed",
            "pcc_worker_resource_replace", "pcc_worker_resource_replace_detailed",
        }, self.namespace)

    def allocate(self, size):
        assert 0 < size <= 65560
        buffer = ctypes.create_string_buffer(size)
        address = ctypes.addressof(buffer)
        assert address not in self.memory
        self.memory[address] = buffer
        return address

    def malloc(self, size):
        address = self.allocate(size)
        self.heap.add(address)
        return address

    def free(self, address):
        assert address in self.heap
        self.heap.remove(address)
        del self.memory[address]

    def bounds(self, address, size):
        assert size >= 0
        assert any(base <= address and address + size <= base + ctypes.sizeof(buffer)
                   for base, buffer in self.memory.items()), (address, size)

    def read(self, address, size):
        self.bounds(address, size)
        return ctypes.string_at(address, size)

    def write(self, address, value):
        self.bounds(address, len(value))
        ctypes.memmove(address, value, len(value))

    def load(self, address, size):
        return int.from_bytes(self.read(address, size), "little")

    def store(self, address, value, size):
        self.write(address, (value & ((1 << (8 * size)) - 1)).to_bytes(size, "little"))

    def c_string(self, text):
        encoded = str(text).encode("utf-8") + b"\0"
        address = self.allocate(len(encoded))
        self.write(address, encoded)
        return address

    def open_source(self, name, access, share, security, disposition, flags, template):
        assert (access, share, security, disposition, flags, template) == (65664, 7, 0, 3, 2097280, 0)
        handle = self.create_file(name, access, share, security, disposition, flags, template)
        self.events.append(("open", handle))
        if handle != INVALID_HANDLE:
            assert handle
            self.handles.add(handle)
        return handle

    def close_source(self, handle):
        assert handle in self.handles
        result = self.close_handle(handle)
        self.events.append(("close", handle, result))
        if result:
            self.handles.remove(handle)
        return result

    def source_information(self, handle, information_class, output, size):
        assert handle in self.handles and (information_class, size) == (9, 8)
        assert self.events[-1] == ("open", handle)
        self.bounds(output, size)
        result = self.get_information(handle, information_class, output, size)
        self.events.append(("query", handle, result))
        return result

    def rename(self, handle, status_block, information, size, information_class):
        assert handle in self.handles and information_class == 65
        assert self.events[-1][:2] == ("query", handle) and self.events[-1][2]
        self.bounds(status_block, 16)
        assert self.read(status_block, 16) == b"\0" * 16
        buffer = self.read(information, size)
        assert struct.unpack_from("<I", buffer)[0] == 3
        assert buffer[4:16] == b"\0" * 12  # Padding and NULL RootDirectory.
        length = struct.unpack_from("<I", buffer, 16)[0]
        assert length > 0 and length % 2 == 0 and size == 24 + length
        assert buffer[20 + length:] == b"\0" * 4
        leaf = buffer[20:20 + length].decode("utf-16-le")
        assert leaf == self.expected_leaf and not any(value in leaf for value in "\\/:")
        result = self.nt_set_information(handle, status_block, information, size, information_class)
        self.events.append(("rename", leaf, result))
        return result

    @contextmanager
    def handle(self, path, access, share, flags=128):
        spelling = ctypes.create_unicode_buffer(str(path))
        handle = self.create_file(ctypes.addressof(spelling), access, share, 0, 3, flags, 0)
        assert handle not in (None, INVALID_HANDLE), ctypes.get_last_error()
        self.handles.add(handle)
        try:
            yield handle
        finally:
            result = self.close_handle(handle)
            error = ctypes.get_last_error()
            if result:
                self.handles.remove(handle)
            assert result, error

    def assert_owner_live(self, handle):
        assert handle in self.handles
        information = ctypes.create_string_buffer(8)
        assert self.get_information(handle, 9, ctypes.addressof(information), 8), ctypes.get_last_error()
        attributes, _tag = struct.unpack("<II", information.raw)
        assert attributes & 16 and not attributes & 1024

    def read_snapshot(self, handle, expected):
        output = ctypes.create_string_buffer(len(expected) + 1)
        count = ctypes.c_uint32()
        assert self.read_file(handle, ctypes.addressof(output), len(output),
                              ctypes.byref(count), 0), ctypes.get_last_error()
        assert output.raw[:count.value] == expected

    def old_absolute_rename(self, source, destination, *, expected_winerror):
        """The pre-fix Win32 call is an owner-isolation control, not a fallback."""
        encoded = str(destination).encode("utf-16-le")
        information = ctypes.create_string_buffer(24 + len(encoded))
        struct.pack_into("<I4xQI", information, 0, 3, 0, len(encoded))
        information[20:20 + len(encoded)] = encoded
        with self.handle(source, 65536, 7) as handle:
            result = self.set_information(handle, 22, ctypes.addressof(information), len(information))
            error = ctypes.get_last_error()
            if expected_winerror:
                assert result == 0 and error == expected_winerror, (result, error)
            else:
                assert result != 0, error  # LastError is unspecified on success.

    def run(self, source, destination):
        self.events.clear()
        self.expected_leaf = destination.name
        retained_handles = set(self.handles)
        diagnostic = self.allocate(24)
        self.write(diagnostic, b"\xff" * 24)
        result = self.namespace["pcc_worker_resource_replace_detailed"](
            self.c_string(source), self.c_string(destination), diagnostic)
        assert not self.heap, "production leaf leaked raw heap allocations"
        assert self.handles == retained_handles, "production leaf leaked or closed an unrelated handle"
        opens = [row[1] for row in self.events if row[0] == "open" and row[1] != INVALID_HANDLE]
        closes = [row[1] for row in self.events if row[0] == "close" and row[2]]
        assert opens == closes
        return result, struct.unpack("<qqq", self.read(diagnostic, 24))


@pytest.fixture
def host_leaf():
    leaf = HostLeaf()
    try:
        yield leaf
    finally:
        leaked_handles, leaked_heap = set(leaf.handles), set(leaf.heap)
        close_errors = []
        for handle in leaked_handles:
            if not leaf.close_handle(handle):
                close_errors.append((handle, ctypes.get_last_error()))
        leaf.handles.clear()
        leaf.memory.clear()
        leaf.heap.clear()
        assert not close_errors, close_errors
        assert not leaked_handles, "test or production leaf leaked a Windows handle"
        assert not leaked_heap, "production leaf leaked raw heap allocations"


def _report_directory(tmp_path, monkeypatch):
    directory = tmp_path / "owned-reports"
    directory.mkdir()
    cwd = tmp_path / "different-cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    assert Path.cwd() != directory
    return directory


@pytest.mark.parametrize("name", ["worker.rss", "報告.rss", "report-😀.rss"])
def test_production_leaf_replaces_under_retained_directory_owner(host_leaf, tmp_path, monkeypatch, name):
    leaf = host_leaf
    directory = _report_directory(tmp_path, monkeypatch)
    destination, source = directory / name, directory / (name + ".tmp")
    old, new = b"old retained snapshot\n", b"new complete worker report\n"
    destination.write_bytes(old)
    source.write_bytes(new)  # Writer is closed before every rename attempt.
    # Positive control: the identical absolute rename and reader sharing work
    # without a directory owner. A filesystem lacking this API must fail here.
    with leaf.handle(destination, 0x80000000, 7) as reader:
        assert leaf.handles == {reader}
        leaf.old_absolute_rename(source, destination, expected_winerror=0)
        assert leaf.handles == {reader}
        assert destination.read_bytes() == new and not source.exists()
        leaf.read_snapshot(reader, old)
    assert not leaf.handles
    destination.write_bytes(old)
    source.write_bytes(new)
    # Only the retained owner changes for the negative control; it then stays
    # open unchanged while the candidate production leaf performs the rename.
    with leaf.handle(directory, OWNER_ACCESS, OWNER_SHARE, 35651584) as owner:
        with leaf.handle(destination, 0x80000000, 7) as reader:
            leaf.assert_owner_live(owner)
            leaf.old_absolute_rename(source, destination, expected_winerror=32)
            assert leaf.handles == {owner, reader}
            assert source.read_bytes() == new and destination.read_bytes() == old
            leaf.assert_owner_live(owner)
            assert leaf.run(source, destination) == (0, (0, 0, 0))
            assert [row[0] for row in leaf.events] == ["open", "query", "rename", "close"]
            assert [(row[1], row[2]) for row in leaf.events if row[0] == "rename"] == [(name, 0)]
            assert destination.read_bytes() == new and not source.exists()
            leaf.read_snapshot(reader, old)
            leaf.assert_owner_live(owner)
    assert not leaf.handles


@pytest.mark.parametrize("failure", ["missing-source", "directory-target", "wrong-suffix"])
def test_production_leaf_failure_keeps_owner_and_paths_intact(host_leaf, tmp_path, monkeypatch, failure):
    leaf = host_leaf
    directory = _report_directory(tmp_path, monkeypatch)
    destination = directory / "worker.rss"
    source = directory / ("worker.rss.other" if failure == "wrong-suffix" else "worker.rss.tmp")
    old, new = b"unchanged target\n", b"uncommitted source\n"
    if failure == "directory-target":
        destination.mkdir()
        (destination / "retained").write_bytes(old)
    else:
        destination.write_bytes(old)
    if failure != "missing-source":
        source.write_bytes(new)
    with leaf.handle(directory, OWNER_ACCESS, OWNER_SHARE, 35651584) as owner:
        status, diagnostic = leaf.run(source, destination)
        assert status < 0 and diagnostic[2] == 0
        if failure == "wrong-suffix":
            assert (status, diagnostic) == (-22, (3, 0, 0))
            assert not leaf.events
        elif failure == "missing-source":
            assert (status, diagnostic) == (-2, (5, 2, 0))
            assert [row[0] for row in leaf.events] == ["open"]
        else:
            assert diagnostic[0] == 6 and diagnostic[1] != 0
            assert [row[0] for row in leaf.events] == ["open", "query", "rename", "close"]
        leaf.assert_owner_live(owner)
        if failure == "missing-source":
            assert not source.exists()
        else:
            assert source.read_bytes() == new
        target = destination / "retained" if failure == "directory-target" else destination
        assert target.read_bytes() == old
    assert not leaf.handles


def test_production_leaf_rejects_source_reparse_without_touching_target(host_leaf, tmp_path, monkeypatch):
    leaf = host_leaf
    directory = _report_directory(tmp_path, monkeypatch)
    source, destination = directory / "worker.rss.tmp", directory / "worker.rss"
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_source, outside_report = outside / source.name, outside / destination.name
    destination.write_bytes(b"inside report remains unchanged\n")
    outside_source.write_bytes(b"outside source remains unchanged\n")
    outside_report.write_bytes(b"outside report remains unchanged\n")
    try:
        source.symlink_to(outside_source)
    except OSError as error:
        pytest.fail(f"Windows source-reparse regression requires file-symlink capability: {error}",
                    pytrace=False)
    assert source.is_symlink() and source.resolve() == outside_source.resolve()
    link_target = source.readlink()  # Windows may return an extended-path spelling.
    with leaf.handle(directory, OWNER_ACCESS, OWNER_SHARE, 35651584) as owner:
        leaf.assert_owner_live(owner)
        assert leaf.run(source, destination) == (-40, (3, 0, 0))
        assert [row[0] for row in leaf.events] == ["open", "query", "close"]
        assert source.is_symlink() and source.readlink() == link_target
        assert destination.read_bytes() == b"inside report remains unchanged\n"
        assert outside_source.read_bytes() == b"outside source remains unchanged\n"
        assert outside_report.read_bytes() == b"outside report remains unchanged\n"
        leaf.assert_owner_live(owner)
    assert not leaf.handles
