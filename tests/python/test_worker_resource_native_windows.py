"""Actual native RSS source against inert WinAPI/memory, plus bounded IR.

The deterministic API model checks the owned protocol, not Windows filesystem
semantics. No runtime, executable, native probe or real WinAPI is run here.
"""

import ast
import builtins
import ntpath
from pathlib import Path
import re
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
WINDOWS = ROOT / "pcc/runtime/py/freestanding_windows_rss.py"
PLATFORM = ROOT / "pcc/runtime/py/freestanding_platform_rss.py"
ERRORS = ROOT / "pcc/runtime/py/freestanding_windows.py"
DIRECTORY = ROOT / "pcc/runtime/py/freestanding_windows_directory.py"
POLICY = ROOT / "pcc/frontends/python/worker_resource_plan.py"


def _definitions(path, names, namespace):
    tree = ast.parse(path.read_text())
    body = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            node.decorator_list = []
            body.append(node)
        elif isinstance(node, ast.Assign) and all(
            isinstance(target, ast.Name) and target.id.startswith("_RENAME_")
            for target in node.targets
        ):
            body.append(node)
    assert {node.name for node in body if isinstance(node, ast.FunctionDef)} == set(names)
    exec(compile(ast.Module(body=body, type_ignores=[]), str(path), "exec"), namespace)


class Model:
    """Bounds-checked buffers and clobbering LastError, no native operations."""
    def __init__(self, *, open_error=0, rename_error=0, close_error=0,
                 full_failure=0, full_growth=0, query_size=0, malloc_failure=0,
                 utf16_failure=0, information_error=0, source_attributes=128):
        self.memory, self.heap, self.freed = {}, set(), set()
        self.events = []
        self.next_address = 4096
        self.last_error = 0
        self.open_error, self.rename_error, self.close_error = open_error, rename_error, close_error
        self.information_error, self.source_attributes = information_error, source_attributes
        self.full_failure, self.full_growth, self.query_size = full_failure, full_growth, query_size
        self.malloc_failure, self.utf16_failure = malloc_failure, utf16_failure
        self.malloc_count = self.utf16_count = self.full_count = 0
        self.committed = False
        self.handle = 0x123456789AB
        self.namespace = {
            "i64": int, "c_ptr": int, "malloc": self.malloc, "free": self.free,
            "stack_alloc": lambda size: self.allocate(size, heap=False),
            "null": lambda: 0, "ptr_add": lambda address, offset: address + offset,
            "ptr_is_null": lambda address: address == 0, "ptr_to_int": int, "int_to_ptr": int,
            "load_i8": lambda address, offset: self.read(address + offset, 1)[0],
            "load_i32": lambda address, offset: int.from_bytes(self.read(address + offset, 4), "little"),
            "load_ptr": lambda address, offset: int.from_bytes(self.read(address + offset, 8), "little"),
            "store_ptr": lambda address, offset, value: self.write(address + offset, value.to_bytes(8, "little")),
            "store_i32": lambda address, offset, value: self.write(address + offset, value.to_bytes(4, "little")),
            "store_i64": lambda address, offset, value: self.write(address + offset, value.to_bytes(8, "little")),
            "memset": lambda address, value, size: self.write(address, bytes([value]) * size),
            "memcpy": lambda destination, source, size: self.write(destination, self.read(source, size)),
            "utf16": self.utf16, "GetFullPathNameW": self.full_path,
            "CreateFileW": self.open, "NtSetInformationFile": self.rename,
            "GetFileInformationByHandleEx": self.information,
            "RtlNtStatusToDosError": self.status_to_dos_error,
            "CloseHandle": self.close, "GetLastError": self.get_last_error,
        }
        _definitions(ERRORS, {"error", "error_code"}, self.namespace)
        _definitions(DIRECTORY, {"tempdir_component_valid", "tempdir_dos_leaf_valid"}, self.namespace)
        self.namespace["component_valid"] = self.namespace["tempdir_component_valid"]
        self.namespace["dos_leaf_valid"] = self.namespace["tempdir_dos_leaf_valid"]
        _definitions(WINDOWS, {
            "_worker_resource_full_path", "_worker_resource_full_path_detailed",
            "pcc_worker_resource_replace", "pcc_worker_resource_replace_detailed",
        }, self.namespace)

    def allocate(self, size, *, heap=True):
        assert 0 < size <= 65560
        address = self.next_address
        self.next_address += size + 16
        self.memory[address] = bytearray(size)
        if heap:
            self.heap.add(address)
        return address

    def region(self, address, size):
        for base, buffer in self.memory.items():
            if base <= address and address + size <= base + len(buffer):
                assert base not in self.freed, "use after free"
                return buffer, address - base
        raise AssertionError(("out of bounds", address, size))

    def read(self, address, size):
        buffer, offset = self.region(address, size)
        return bytes(buffer[offset:offset + size])

    def write(self, address, data):
        buffer, offset = self.region(address, len(data))
        buffer[offset:offset + len(data)] = data

    def wide_text(self, address):
        size = 0
        while self.read(address + size, 2) != b"\0\0":
            size += 2
        return self.read(address, size).decode("utf-16-le")

    def malloc(self, size):
        self.malloc_count += 1
        if self.malloc_count == self.malloc_failure:
            return 0
        return self.allocate(size)

    def free(self, address):
        assert address in self.heap and address not in self.freed
        self.freed.add(address)
        self.events.append(("free", address))
        self.last_error = 112  # A cleanup must never replace the primary error.

    def utf16(self, text):
        self.utf16_count += 1
        if self.utf16_count == self.utf16_failure:
            return 0
        encoded = text.encode("utf-16-le") + b"\0\0"
        address = self.allocate(len(encoded))
        self.write(address, encoded)
        return address

    def full_path(self, address, capacity, output, file_part):
        assert file_part == 0
        self.full_count += 1
        self.events.append(("full", self.full_count, capacity))
        if self.full_count == self.full_failure:
            self.last_error = 5
            return 0
        absolute = ntpath.normpath(ntpath.join("C:\\work", self.wide_text(address)))
        encoded = absolute.encode("utf-16-le") + b"\0\0"
        if capacity == 0:
            assert output == 0
            return self.query_size or len(encoded) // 2
        if self.full_count == self.full_growth:
            return capacity + 1  # Deliberately leave output uninitialized.
        assert capacity * 2 >= len(encoded)
        self.write(output, encoded)
        return len(encoded) // 2 - 1

    def open(self, path, access, share, security, disposition, flags, template):
        assert (access, share, security, disposition, flags, template) == (65664, 7, 0, 3, 2097280, 0)
        self.events.append(("open", self.wide_text(path)))
        self.last_error = self.open_error
        return -1 if self.open_error else self.handle

    def information(self, handle, information_class, output, size):
        assert handle == self.handle and information_class == 9 and size == 8
        assert self.read(output, size) == bytes(size)
        self.events.append(("information", handle))
        self.last_error = self.information_error
        if self.information_error:
            return 0
        self.write(output, self.source_attributes.to_bytes(4, "little") + bytes(4))
        return 1

    def rename(self, handle, io_status, information, size, information_class):
        assert handle == self.handle and information_class == 65
        assert self.read(io_status, 16) == b"\0" * 16
        buffer = self.read(information, size)
        assert int.from_bytes(buffer[:4], "little") == 3
        assert buffer[4:16] == b"\0" * 12
        name_size = int.from_bytes(buffer[16:20], "little")
        assert size == 24 + name_size and name_size % 2 == 0
        assert buffer[20 + name_size:] == b"\0" * 4
        destination = buffer[20:20 + name_size].decode("utf-16-le")
        assert destination and not any(part in destination for part in ("/", "\\", ":"))
        self.events.append(("rename", destination, name_size))
        self.last_error = 112  # NTSTATUS, not TLS LastError, owns this failure.
        if self.rename_error:
            return -1073741790  # STATUS_ACCESS_DENIED; mapping is injected below.
        self.committed = True
        return 0

    def status_to_dos_error(self, status):
        assert status == -1073741790 and self.rename_error
        self.events.append(("nt_error", self.rename_error))
        return self.rename_error if self.rename_error < 2147483648 else self.rename_error - 4294967296

    def close(self, handle):
        assert handle == self.handle
        self.events.append(("close", handle))
        self.last_error = self.close_error or 87
        return 0 if self.close_error else 1

    def get_last_error(self):
        self.events.append(("error", self.last_error))
        # GetLastError is declared as the existing signed i32 WinAPI lane.
        return self.last_error if self.last_error < 2147483648 else self.last_error - 4294967296

    def run(self, source="worker.rss.tmp", destination="worker.rss", *, detailed=False):
        if detailed:
            diagnostic = self.allocate(24, heap=False)
            self.write(diagnostic, b"\xff" * 24)
            status = self.namespace["pcc_worker_resource_replace_detailed"](source, destination, diagnostic)
            self.diagnostic = tuple(int.from_bytes(self.read(diagnostic + offset, 8), "little")
                                    for offset in (0, 8, 16))
        else:
            status = self.namespace["pcc_worker_resource_replace"](source, destination)
        assert self.heap == self.freed, "raw heap leak"
        opened = bool([row for row in self.events if row[0] == "open"]) and not self.open_error
        assert len([row for row in self.events if row[0] == "close"]) == int(opened)
        return status


@pytest.mark.parametrize("name", ["worker.rss", "報告.rss", "report-😀.rss"])
def test_native_windows_report_uses_exact_extended_rename_layout(name):
    model = Model()
    assert model.run("sub/../" + name + ".tmp", name) == 0
    assert model.committed
    absolute = "C:\\work\\" + name
    assert ("open", absolute + ".tmp") in model.events
    assert ("rename", name, len(name.encode("utf-16-le"))) in model.events
    assert not [event for event in model.events if event[0] == "error"]


@pytest.mark.parametrize("source", ["other.rss.tmp", "other/worker.rss.tmp", "worker.rss", "worker.rss.tmpx"])
def test_native_windows_rejects_non_report_source_before_open(source):
    model = Model()
    assert model.run(source) == -22
    assert not model.committed and not any(event[0] == "open" for event in model.events)


@pytest.mark.parametrize("destination", [
    r"D:\reports\worker.rss", r"\\server\share\reports\worker.rss",
    "D:\\reports\\報告-😀.rss",
])
def test_native_windows_same_directory_rename_does_not_use_cwd(destination):
    model = Model()
    assert model.run(destination + ".tmp", destination, detailed=True) == 0
    leaf = ntpath.basename(destination)
    assert ("open", destination + ".tmp") in model.events
    assert ("rename", leaf, len(leaf.encode("utf-16-le"))) in model.events
    assert model.diagnostic == (0, 0, 0)


@pytest.mark.parametrize("destination", [
    "worker.rss.", "worker.rss ", "worker.rss:stream", "CON.rss", "NUL",
    "COM1.rss", "LPT².rss", "CONIN$.rss", "report?.rss", "report*.rss",
])
def test_native_windows_nt_leaf_never_expands_win32_namespace(destination):
    model = Model()
    assert model.run(destination + ".tmp", destination, detailed=True) == -22
    assert model.diagnostic == (3, 0, 0)
    assert not any(event[0] in ("open", "rename", "close", "error", "nt_error")
                   for event in model.events)


def test_native_windows_nt_rename_import_is_an_owned_named_system_call():
    from pcc.backend.pe_x86_64 import system_dll

    assert system_dll("NtSetInformationFile") == "NTDLL.dll"
    source = WINDOWS.read_text()
    assert 'extern("NtSetInformationFile", (c_ptr, c_ptr, c_ptr, c_int, c_int), c_int)' in source
    assert 'extern("RtlNtStatusToDosError", (c_int,), c_int)' in source
    assert 'CreateFileW(a, 65664, 7, null(), 3, 2097280, null())' in source
    assert 'NtCreateFile(output, 1114241,' in DIRECTORY.read_text()
    assert 'attributes, status_block, null(), 128, 1, 2 if create else 1,' in DIRECTORY.read_text()


@pytest.mark.parametrize("symbol,parameters,result", [
    ("NtSetInformationFile", "(c_ptr,c_ptr,c_ptr,c_int,c_int)", "c_int"),
    ("pcc_win_tempdir_component_valid", "(c_ptr,c_int64)", "c_int64"),
    ("pcc_win_tempdir_dos_leaf_valid", "(c_ptr,c_int64)", "c_int64"),
])
def test_native_windows_rename_machine_imports_require_exact_raw_signatures(symbol, parameters, result):
    from pcc.frontends.python.pipeline_freestanding import freestanding_allowed_external_symbols

    def admitted(name, args, returns):
        return freestanding_allowed_external_symbols(
            'call = extern("' + name + '", ' + args + ', ' + returns + ')\n')

    assert symbol in admitted(symbol, parameters, result)
    assert symbol not in admitted(symbol, parameters, "c_ptr")
    assert symbol not in admitted(symbol, parameters.replace("c_ptr", "c_int64", 1), result)
    assert symbol not in admitted(symbol, "()", result)
    assert symbol + "_other" not in admitted(symbol + "_other", parameters, result)


@pytest.mark.parametrize("attributes", [1024, 1024 | 128])
@pytest.mark.parametrize("close_error,cleanup", [(0, 0), (6, 9), (0xffffffff, 5)])
def test_native_windows_source_reparse_is_rejected_on_same_handle(attributes, close_error, cleanup):
    model = Model(source_attributes=attributes, close_error=close_error)
    assert model.run(detailed=True) == (40 * 65536 + cleanup if close_error else -40)
    assert model.diagnostic == (3, 0, close_error)
    assert not model.committed
    assert [event[0] for event in model.events if event[0] in ("open", "information", "rename", "close")] == [
        "open", "information", "close"]
    assert not any(event[0] == "nt_error" for event in model.events)


@pytest.mark.parametrize("code,primary", [(5, 13), (32, 13), (50, 5), (0x80000005, 5), (0xffffffff, 5)])
@pytest.mark.parametrize("close_error,cleanup", [(0, 0), (6, 9), (0xffffffff, 5)])
def test_native_windows_source_query_failure_preserves_both_errors(code, primary, close_error, cleanup):
    model = Model(information_error=code, close_error=close_error)
    assert model.run(detailed=True) == (primary * 65536 + cleanup if close_error else -primary)
    assert model.diagnostic == (8, code, close_error)
    assert not model.committed and not any(event[0] == "rename" for event in model.events)
    assert [event for event in model.events if event[0] == "error"] == (
        [("error", code)] + ([("error", close_error)] if close_error else []))


@pytest.mark.parametrize("site", ["open", "rename"])
@pytest.mark.parametrize("code,expected", [(2, 2), (5, 13), (32, 13), (33, 13), (50, 5), (87, 22), (4395, 40)])
def test_native_windows_primary_error_survives_clobbering_cleanup(site, code, expected):
    model = Model(**{site + "_error": code})
    assert model.run() == -expected
    assert not model.committed
    assert [event for event in model.events if event[0] in ("error", "nt_error")] == [
        ("nt_error" if site == "rename" else "error", code)]
    if site == "open":
        assert not any(event[0] == "rename" for event in model.events)


@pytest.mark.parametrize("rename_error,primary", [(0, 0), (5, 13), (50, 5)])
@pytest.mark.parametrize("close_error,cleanup", [(6, 9), (5, 13)])
def test_native_windows_close_failure_carries_commit_and_both_errors(rename_error, primary, close_error, cleanup):
    model = Model(rename_error=rename_error, close_error=close_error)
    assert model.run() == primary * 65536 + cleanup
    assert model.committed is (primary == 0)
    expected = ([] if not rename_error else [("nt_error", rename_error)]) + [("error", close_error)]
    assert [event for event in model.events if event[0] in ("error", "nt_error")] == expected


@pytest.mark.parametrize("options,expected", [
    ({"full_failure": 1}, -13), ({"full_failure": 2}, -13),
    ({"full_failure": 3}, -13), ({"full_failure": 4}, -13),
    ({"full_growth": 2}, -36), ({"full_growth": 4}, -36),
    ({"query_size": 32769}, -36), ({"query_size": 2147483647}, -36),
    ({"malloc_failure": 1}, -12), ({"malloc_failure": 2}, -12),
    ({"malloc_failure": 3}, -12), ({"utf16_failure": 1}, -22),
    ({"utf16_failure": 2}, -22),
])
def test_native_windows_setup_failure_never_opens_or_leaks(options, expected):
    model = Model(**options)
    assert model.run() == expected
    assert not any(event[0] in ("open", "rename", "close") for event in model.events)


def test_native_windows_real_error_mapping_fits_status_protocol():
    model = Model()
    function = next(node for node in ast.parse(ERRORS.read_text()).body
                    if isinstance(node, ast.FunctionDef) and node.name == "error_code")
    codes = {node.value for node in ast.walk(function)
             if isinstance(node, ast.Constant) and type(node.value) is int}
    for code in codes | {0, 1, 50, 65535, 0x7fffffff, -1}:
        assert 0 < -model.namespace["error_code"](code) < 65536


@pytest.mark.parametrize("status,committed,primary,cleanup", [
    (9, True, 0, 9), (13 * 65536 + 9, False, 13, 9), (5 * 65536 + 13, False, 5, 13),
])
def test_native_dispatch_cleanup_failure_is_nonretryable(monkeypatch, status, committed, primary, cleanup):
    namespace = {}
    _definitions(POLICY, {"_replace_worker_resource"}, namespace)
    original = builtins.__import__
    types = SimpleNamespace(c_int64=object(), c_obj=object(), c_ptr=object(), c_str=object())
    diagnostic = [7 if committed else 6, 0 if committed else 5 if primary == 13 else 50,
                  6 if cleanup == 9 else 5]
    def declare(symbol, arguments, result):
        assert symbol == "pcc_worker_resource_replace_detailed"
        assert arguments == (types.c_str, types.c_str, types.c_int64) and result is types.c_int64
        return lambda source, destination, output: status
    types.extern = declare
    def importing(name, *args, **kwargs):
        if name == "sys":
            return SimpleNamespace(implementation=SimpleNamespace(name="pcc"), platform="win32")
        if name == "pcc.extern":
            return types
        if name == "pcc.unsafe":
            return SimpleNamespace(stack_alloc=lambda size: diagnostic,
                                   load_i64=lambda buffer, offset: buffer[offset // 8])
        return original(name, *args, **kwargs)
    with monkeypatch.context() as selected:
        selected.setattr(builtins, "__import__", importing)
        with pytest.raises(RuntimeError) as caught:
            namespace["_replace_worker_resource"]("worker.rss.tmp", "worker.rss")
    assert not isinstance(caught.value, PermissionError)
    assert str(caught.value) == (
        "worker RSS handle cleanup failed: publication_committed=" + str(committed)
        + " primary_errno=" + str(primary) + " handle_cleanup_errno=" + str(cleanup)
        + " stage=" + ("close" if committed else "rename")
        + " winerror=" + str(diagnostic[1]) + " handle_cleanup_winerror=" + str(diagnostic[2]))
    function = next(node for node in ast.parse(POLICY.read_text()).body
                    if isinstance(node, ast.FunctionDef) and node.name == "_replace_worker_resource")
    assert not any(isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)
                   for node in ast.walk(function))


def test_nonwindows_stub_is_explicit_and_original_rss_owners_are_target_exclusive():
    from pcc.frontends.python.owned_runtime_build import runtime_modules
    from pcc.frontends.python.codegen.runtime_abi import RUNTIME_SIGNATURES, _I64, _CSTR
    assert RUNTIME_SIGNATURES["pcc_worker_resource_replace"] == (_I64, [_CSTR, _CSTR], False)
    assert RUNTIME_SIGNATURES["pcc_worker_resource_replace_detailed"] == (
        _I64, [_CSTR, _CSTR, _I64], False)
    assert "int64_t   pcc_worker_resource_replace(const char *source, const char *destination);" in (
        ROOT / "pcc/runtime/include/py_runtime.h").read_text()
    namespace = {"i64": int, "c_ptr": int, "int_to_ptr": lambda address: address,
                 "memset": lambda output, value, size: output.__setitem__(slice(0, size), bytes([value]) * size)}
    _definitions(PLATFORM, {"pcc_worker_resource_replace", "pcc_worker_resource_replace_detailed"}, namespace)
    assert namespace["pcc_worker_resource_replace"](0, 0) == -38
    output = bytearray(b"\xff" * 24)
    assert namespace["pcc_worker_resource_replace_detailed"](0, 0, output) == -38
    assert output == bytes(24)
    for target in ("x86_64-pc-windows-msvc", "aarch64-unknown-linux-gnu", "arm64-apple-darwin"):
        names = runtime_modules(str(ROOT / "pcc/runtime"), target)
        assert ("freestanding_windows_rss" in names) is ("windows" in target)
        assert ("freestanding_platform_rss" in names) is ("windows" not in target)


def _body(text, symbol):
    match = re.search(r'^define[^\n]*@"?' + re.escape(symbol) + r'"?\([^\n]*\).*?^}', text, re.M | re.S)
    assert match, symbol
    return match.group(0)


@pytest.mark.parametrize("target", [
    "x86_64-pc-windows-msvc", "aarch64-unknown-linux-gnu", "arm64-apple-darwin",
])
def test_rss_replace_runtime_module_has_verified_raw_ir(tmp_path, monkeypatch, target):
    from pcc.frontends.python.owned_runtime_build import _compile_runtime_module, runtime_ir_passes
    from pcc.ir.optimization.driver import optimize_ir
    from tests.owned_ir_validation import verify_ir_text
    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    monkeypatch.setenv("PCC_REFCOUNT_KIND", "atomic")
    source = WINDOWS if "windows" in target else PLATFORM
    output = tmp_path / "rss.ll"
    _compile_runtime_module(source.stem, str(source), str(output), target)
    raw = output.read_text()
    for text in (raw, optimize_ir(raw, runtime_ir_passes(str(ROOT / "pcc/runtime")))):
        parsed = verify_ir_text(text)
        expected = {"pcc_os_current_rss_bytes", "pcc_os_peak_rss_bytes", "pcc_worker_resource_replace",
                    "pcc_worker_resource_replace_detailed"}
        expected |= ({"pcc_windows_rss_bytes", "pcc_windows_worker_resource_full_path",
                      "pcc_windows_worker_resource_full_path_detailed"}
                     if "windows" in target else {"pcc_platform_rss_linux_proc_status_bytes"})
        assert {function.name for function in parsed.functions} == expected
        body = _body(text, "pcc_worker_resource_replace")
        assert re.search(r"^define external i64 @pcc_worker_resource_replace\(ptr \S+, ptr \S+\)", body)
        if "windows" in target:
            assert "@pcc_worker_resource_replace_detailed(" in body
        body = _body(text, "pcc_worker_resource_replace_detailed")
        assert re.search(r"^define external i64 @pcc_worker_resource_replace_detailed\(ptr \S+, ptr \S+, i64 \S+\)", body)
        raw_address = re.search(r"(%[\w.]+) = inttoptr i64 %diagnostic_address to ptr", body)
        assert raw_address
        assert re.search(r"@memset\(ptr " + re.escape(raw_address.group(1))
                         + r", i32 (?:0|%[\w.]+), i64 24\)", body)
        assert "thread_local" not in text
        assert not re.search(r"\bcall[^\n]*@(?:pcc_gc_|py_)", text)
        if "windows" in target:
            call = re.search(r"@NtSetInformationFile\(ptr [^,]+, ptr [^,]+, ptr [^,]+, i32 [^,]+, i32 ([^)]+)\)", body)
            assert call
            information_class = call.group(1)
            assert information_class == "65" or re.search(
                re.escape(information_class) + r" = trunc i64 65 to i32", body)
            assert "@RtlNtStatusToDosError(" in body
            assert "@pcc_win_tempdir_component_valid(" in body
            assert "@pcc_win_tempdir_dos_leaf_valid(" in body
            assert "@GetFileInformationByHandleEx(" in body
            assert "@SetFileInformationByHandle" not in text
            assert "@CloseHandle(" in body and "@CreateFileW(" in body
            assert "@MoveFileEx" not in text and "@SetFileAttributes" not in text
        else:
            assert "ret i64 -38" in body
            assert "@NtSetInformationFile" not in text


def _assert_diagnostic_address_lane(body):
    """Trace the actual buffer address through boxing, unboxing and the call.

    Application stack_alloc is an integer address. Merely finding an alloca
    and a c_ptr argument missed a write into the boxed integer object's pointer.
    """
    definitions = dict(re.findall(r"^  (%[\w.]+) = ([^\n]+)$", body, re.M))
    call = re.search(
        r"@pcc_worker_resource_replace_detailed\(ptr (%[\w.]+), ptr (%[\w.]+), i64 (%[\w.]+)\)", body)
    assert call, "diagnostic address must use the integer ABI lane"
    address = call.group(3)
    phi = re.fullmatch(r"phi i64 \[(%[\w.]+), %[^]]+\], \[(%[\w.]+), %[^]]+\]", definitions[address])
    assert phi, "address argument must be the checked integer unbox result"
    fast, slow = phi.groups()
    untag = re.fullmatch(r"ashr i64 (%[\w.]+), 1", definitions[fast])
    assert untag
    bits = untag.group(1)
    tagged = re.fullmatch(r"ptrtoint ptr (%[\w.]+) to i64", definitions[bits])
    assert tagged
    boxed = tagged.group(1)
    assert re.fullmatch(r"call i64 \(ptr, ptr\) @py_int_to_i64_lane\(ptr "
                        + re.escape(boxed) + r", ptr %[\w.]+\)", definitions[slow])
    load = re.fullmatch(r"call ptr \(ptr, ptr\) @pcc_gc_load_ptr\(ptr null, ptr (%[\w.]+)\)", definitions[boxed])
    assert load
    # The root slot is loaded directly, or through an identity pointer alias.
    root = load.group(1)
    root_cast = re.fullmatch(r"bitcast ptr (%[\w.]+) to ptr", definitions.get(root, ""))
    if root_cast:
        root = root_cast.group(1)
    assert definitions.get(root, "").startswith("alloca "), root
    aliases = {root} | {name for name, rhs in definitions.items() if rhs == "bitcast ptr " + root + " to ptr"}
    stores = [(slot, value) for slot, value in re.findall(
        r"@pcc_gc_store_root_take\(ptr (%[\w.]+), ptr (%[\w.]+)\)", body[:call.start()])
              if slot in aliases]
    assert len(stores) == 1
    integer_box = re.fullmatch(r"call ptr \(i64\) @py_int_from_i64\(i64 (%[\w.]+)\)", definitions[stores[0][1]])
    assert integer_box
    raw = re.fullmatch(r"ptrtoint ptr (%[\w.]+) to i64", definitions[integer_box.group(1)])
    assert raw
    buffer = re.fullmatch(
        r"getelementptr inbounds \[24 x i8\], ptr (%[\w.]+), i64 0, i64 0", definitions[raw.group(1)])
    assert buffer and definitions[buffer.group(1)] == "alloca [24 x i8]"
    assert "@llvm.stackrestore" not in body
    return call, bits


@pytest.mark.parametrize("target", [
    "x86_64-pc-windows-msvc", "aarch64-unknown-linux-gnu", "arm64-apple-darwin",
])
def test_actual_dispatch_ir_uses_counted_string_leases_and_original_posix_call(tmp_path, monkeypatch, target):
    from pcc.frontends.python.pipeline import compile_python
    from tests.owned_ir_validation import verify_ir_text
    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    dispatch = next(node for node in ast.parse(POLICY.read_text()).body
                    if isinstance(node, ast.FunctionDef) and node.name == "_replace_worker_resource")
    source = tmp_path / "rss_dispatch.py"
    source.write_text("import os\n" + ast.unparse(dispatch) + "\n")
    output = source.with_suffix(".ll")
    compile_python(str(source), str(output), emit_llvm_only=True, python_library=True,
                   libpython_mode="off", backend="self", target_triple=target)
    text = output.read_text()
    verify_ir_text(text)
    body = _body(text, "user_rss_dispatch__replace_worker_resource")
    # sys.platform remains a runtime condition here, so both call sites must
    # exist on every target. The dispatcher model separately verifies selection.
    assert "@py_os_replace(" in body
    foreign = body.index("@pcc_worker_resource_replace_detailed(")
    assert "alloca [24 x i8]" in body
    projection = body.index("@py_str_utf8(")
    leases = list(re.finditer(
        r"(%extern\.lease\.acquire[\w.]*) = call i64 \(ptr\) @pcc_gc_foreign_lease_acquire\(",
        body[:foreign]))
    assert len(leases) == 2 and max(match.start() for match in leases) < projection < foreign
    assert body[:foreign].count("@py_str_utf8(") == 2
    for lease in leases:
        assert re.search(r"@pcc_gc_foreign_lease_release\([^\n]*i64 "
                         + re.escape(lease.group(1)) + r"\)", body[foreign:])
    call = re.search(r"@pcc_worker_resource_replace_detailed\(ptr (%[\w.]+), ptr (%[\w.]+), i64 (%[\w.]+)\)", body)
    assert call
    traced_call, tagged_bits = _assert_diagnostic_address_lane(body)
    assert traced_call.group(0) == call.group(0)
    # Guard the guard: neither a c_ptr object address nor tagged integer bits
    # can replace the unboxed integer address at this boundary.
    for replacement in ("ptr " + call.group(3), "i64 " + tagged_bits):
        malformed = body.replace(call.group(0), call.group(0).replace(
            "i64 " + call.group(3) + ")", replacement + ")"))
        with pytest.raises(AssertionError):
            _assert_diagnostic_address_lane(malformed)
    for value in call.groups()[:2]:
        assert re.search(re.escape(value) + r" = call ptr \(ptr\) @py_str_utf8\(", body[:foreign])
    assert "extern.lease.acquire.error" in body and "extern.lease.cleanup.error" in body
    assert "@py_obj_setattr" not in body
    assert "worker_resource_host_windows" not in text


@pytest.mark.parametrize("site,stage", [("open", 5), ("rename", 6)])
@pytest.mark.parametrize("code", [5, 32, 33, 0x80000005, 0xffffffff])
def test_detailed_errors_keep_original_dword_and_failure_stage(site, stage, code):
    model = Model(**{site + "_error": code})
    expected = -13 if code in (5, 32, 33) else -5
    assert model.run(detailed=True) == expected
    assert model.diagnostic == (stage, code, 0)
    assert not model.committed
    assert [event for event in model.events if event[0] in ("error", "nt_error")] == [
        ("nt_error" if site == "rename" else "error", code)]


@pytest.mark.parametrize("primary", [0, 5, 32, 33])
@pytest.mark.parametrize("cleanup", [5, 6, 32, 33, 0xffffffff])
def test_detailed_close_error_never_overwrites_primary_or_commit(primary, cleanup):
    model = Model(rename_error=primary, close_error=cleanup)
    primary_errno = 13 if primary else 0
    cleanup_errno = 9 if cleanup == 6 else 5 if cleanup == 0xffffffff else 13
    assert model.run(detailed=True) == primary_errno * 65536 + cleanup_errno
    assert model.diagnostic == (6 if primary else 7, primary, cleanup)
    assert model.committed is (primary == 0)


@pytest.mark.parametrize("options,stage,raw", [
    ({}, 0, 0), ({"full_failure": 1}, 1, 5), ({"full_failure": 2}, 1, 5),
    ({"full_failure": 3}, 2, 5), ({"full_failure": 4}, 2, 5),
    ({"full_growth": 2}, 1, 0), ({"full_growth": 4}, 2, 0),
    ({"query_size": 32769}, 1, 0), ({"query_size": 2147483647}, 1, 0),
    ({"malloc_failure": 1}, 1, 0), ({"malloc_failure": 2}, 2, 0),
    ({"malloc_failure": 3}, 4, 0), ({"utf16_failure": 1}, 1, 0),
    ({"utf16_failure": 2}, 2, 0),
])
def test_detailed_record_is_initialized_on_every_setup_exit(options, stage, raw):
    legacy, detailed = Model(**options), Model(**options)
    assert detailed.run(detailed=True) == legacy.run()
    assert detailed.committed is legacy.committed
    assert detailed.diagnostic == (stage, raw, 0)
    assert detailed.events == legacy.events or [event[0] for event in detailed.events] == [
        event[0] for event in legacy.events]


def test_detailed_validation_does_not_report_stale_last_error():
    model = Model()
    model.last_error = 32
    assert model.run("other.rss.tmp", detailed=True) == -22
    assert model.diagnostic == (3, 0, 0)
    assert not any(event[0] in ("open", "error") for event in model.events)


def test_legacy_full_path_helper_retains_original_output_contract():
    model = Model()
    output = model.allocate(8, heap=False)
    length = model.namespace["_worker_resource_full_path"]("worker.rss", output)
    address = int.from_bytes(model.read(output, 8), "little")
    assert model.wide_text(address) == "C:\\work\\worker.rss"
    assert length == len("C:\\work\\worker.rss")
    model.free(address)
    assert model.heap == model.freed


def test_diagnostic_record_survives_cleanup_reentry_without_shared_state():
    model = Model(rename_error=32, close_error=6)
    original_close = model.namespace["CloseHandle"]
    nested = []
    def close(handle):
        original_options = (model.rename_error, model.close_error)
        model.rename_error = model.close_error = 0
        model.namespace["CloseHandle"] = original_close
        output = model.allocate(24, heap=False)
        nested.append(model.namespace["pcc_worker_resource_replace_detailed"](
            "nested.rss.tmp", "nested.rss", output))
        nested.append(tuple(int.from_bytes(model.read(output + offset, 8), "little")
                            for offset in (0, 8, 16)))
        model.rename_error, model.close_error = original_options
        return original_close(handle)
    model.namespace["CloseHandle"] = close
    # Two independent stack records remain live during the nested call.
    output = model.allocate(24, heap=False)
    assert model.namespace["pcc_worker_resource_replace_detailed"](
        "worker.rss.tmp", "worker.rss", output) == 13 * 65536 + 9
    assert nested == [0, (0, 0, 0)]
    assert tuple(int.from_bytes(model.read(output + offset, 8), "little")
                 for offset in (0, 8, 16)) == (6, 32, 6)
    assert model.heap == model.freed


@pytest.mark.parametrize("options,stage,primary,cleanup,exception", [
    ({"open_error": 5}, "open_source", 5, 0, PermissionError),
    ({"open_error": 32}, "open_source", 32, 0, PermissionError),
    ({"open_error": 33}, "open_source", 33, 0, PermissionError),
    ({"rename_error": 5}, "rename", 5, 0, PermissionError),
    ({"rename_error": 32}, "rename", 32, 0, PermissionError),
    ({"rename_error": 33}, "rename", 33, 0, PermissionError),
    ({"full_failure": 1}, "destination_full_path", 5, 0, PermissionError),
    ({"full_failure": 3}, "source_full_path", 5, 0, PermissionError),
    ({"close_error": 6}, "close", 0, 6, RuntimeError),
    ({"rename_error": 33, "close_error": 6}, "rename", 33, 6, RuntimeError),
    ({"information_error": 32}, "source_information", 32, 0, PermissionError),
    ({"information_error": 32, "close_error": 6}, "source_information", 32, 6, RuntimeError),
    ({"source_attributes": 1024}, "source_validation", 0, 0, OSError),
    ({"source_attributes": 1024, "close_error": 6}, "source_validation", 0, 6, RuntimeError),
])
def test_actual_detailed_leaf_reaches_dispatch_text_without_changing_retry_type(
    monkeypatch, options, stage, primary, cleanup, exception,
):
    model = Model(**options)
    namespace = {}
    _definitions(POLICY, {"_replace_worker_resource"}, namespace)
    original = builtins.__import__
    types = SimpleNamespace(c_int64=object(), c_obj=object(), c_ptr=object(), c_str=object())
    def declare(symbol, arguments, result):
        assert symbol == "pcc_worker_resource_replace_detailed"
        assert arguments == (types.c_str, types.c_str, types.c_int64)
        assert result is types.c_int64
        return model.namespace[symbol]
    types.extern = declare
    def importing(name, *args, **kwargs):
        if name == "sys":
            return SimpleNamespace(implementation=SimpleNamespace(name="pcc"), platform="win32")
        if name == "pcc.extern":
            return types
        if name == "pcc.unsafe":
            return SimpleNamespace(
                stack_alloc=lambda size: model.allocate(size, heap=False),
                load_i64=lambda address, offset: int.from_bytes(model.read(address + offset, 8), "little"),
            )
        return original(name, *args, **kwargs)
    with monkeypatch.context() as selected:
        selected.setattr(builtins, "__import__", importing)
        with pytest.raises(exception) as caught:
            namespace["_replace_worker_resource"]("worker.rss.tmp", "worker.rss")
    assert ("stage=" + stage + " winerror=" + str(primary)
            + " handle_cleanup_winerror=" + str(cleanup)) in str(caught.value)
    if exception is PermissionError:
        assert caught.value.errno == 13
        assert getattr(caught.value, "winerror", None) is None
        assert caught.value.filename == "worker.rss.tmp" and caught.value.filename2 == "worker.rss"
    elif exception is RuntimeError:
        assert not isinstance(caught.value, PermissionError)
        assert ("publication_committed=" + str(stage == "close")) in str(caught.value)
    else:
        assert exception is OSError and caught.value.errno == 40
        assert "publication_committed=" not in str(caught.value)
    assert model.heap == model.freed
