"""Ordinary C calls must resolve to owned runtime exports without host tools.

Object controls are bounded frontend/ABI evidence. Execution controls require
an explicitly admitted source-matching runtime; they never provision one.
"""
from __future__ import annotations

import builtins
import os
import platform
import re
from pathlib import Path
import subprocess
import sys

import pytest

from pcc.backend.elf_x86_64 import ElfError, link_static_executable, parse_relocatable
from pcc.runtime.freestanding_abi_spec import ABI_SPEC
from pcc.backend.owned_object_emit import emit_owned_object
from pcc.driver.project import TranslationUnit
from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
from pcc.frontends.python import pipeline, owned_runtime_build

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).parent / "fixtures/owned_linux_c_exports"
TARGETS = ("x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu")


@pytest.fixture
def deny_external_compilers(monkeypatch):
    original = builtins.__import__
    def checked(name, *args, **kwargs):
        if name == "llvmlite" or name.startswith("llvmlite."):
            raise AssertionError("owned C control imported " + name)
        return original(name, *args, **kwargs)
    def forbidden(*args, **kwargs):
        raise AssertionError("owned control attempted an external process")
    monkeypatch.setattr(builtins, "__import__", checked)
    monkeypatch.setattr(subprocess, "Popen", forbidden)


def _units(source, target):
    evaluator = CEvaluator(backend="self", target_triple=target)
    return evaluator, evaluator.compile_translation_units(
        [TranslationUnit("control.c", "control.c", source)],
        use_system_cpp=False, use_compile_cache=False,
        include_dirs=[str(ROOT / "utils/fake_libc_include")],
    )


@pytest.mark.parametrize("target", TARGETS)
def test_linux_export_exact_abi_object(tmp_path, target, deny_external_compilers):
    output = tmp_path / "linux_libc.ll"
    source = ROOT / "pcc/runtime/py/freestanding_linux_libc.py"
    # Private leaf copies must still enter the same runtime-library ABI mode.
    if not pipeline._is_py_runtime_library_source(str(source)):
        directory = tmp_path / "py_runtime_linux_c_control/py"
        directory.mkdir(parents=True)
        materialized = directory / source.name
        materialized.write_bytes(source.read_bytes())
        source = materialized
    assert pipeline._is_py_runtime_library_source(str(source))
    assert pipeline._source_declares_freestanding_module(source.read_text())
    owned_runtime_build._compile_runtime_module(
        "freestanding_linux_libc", str(source), str(output), target,
    )
    from pcc.ir.optimization.driver import optimize_ir
    text = optimize_ir(output.read_text(), owned_runtime_build.runtime_ir_passes(pipeline._PY_RUNTIME_DIR))
    output.write_text(text)
    for signature in (
        "void @_Exit(i32 ", "void @_exit(i32 ", "i32 @close(i32 ",
        "i32 @fcntl(i32 ", "void @exit(i32 ", "i32 @atexit(ptr ",
        "i32 @setpgid(i32 ",
    ):
        assert signature in text
    object_bytes = emit_owned_object(text, target)
    output.with_suffix(".o").write_bytes(object_bytes)
    obj = parse_relocatable(object_bytes)
    definitions = {s.name for s in obj.symbols if s.section_index != 0}
    assert {"_Exit", "_exit", "close", "fcntl", "exit", "atexit", "setpgid"} <= definitions
    undefined = {s.name for s in obj.symbols if s.section_index == 0 and s.name}
    assert undefined <= {"pcc_errno_set", "pcc_platform_abort", "malloc", "free", "fflush",
                         "__fini_array_start", "__fini_array_end"}
    assert not re.search(r"call[^\n]*@(py_box_|py_int_)", text)


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("name", ("arguments_stdout", "descriptor_control", "immediate_exit",
                                   "exit_lifecycle", "flush_all", "immediate_bypass", "main_no_return_c89_2"))
def test_original_c_export_controls_emit_object(target, name, deny_external_compilers):
    _evaluator, units = _units((FIXTURES / (name + ".c")).read_text(), target)
    assert emit_owned_object(units[0][1], target)


@pytest.mark.parametrize("target", TARGETS)
def test_unknown_c_symbol_stays_fail_closed(target, deny_external_compilers):
    _evaluator, units = _units(
        "extern int pcc_deliberately_unknown_c_symbol(void); "
        "int main(void) { return pcc_deliberately_unknown_c_symbol(); }", target,
    )
    obj = parse_relocatable(emit_owned_object(units[0][1], target))
    with pytest.raises(ElfError, match="undefined static ELF symbols:.*pcc_deliberately_unknown_c_symbol"):
        link_static_executable([obj], entry="main")


@pytest.mark.integration
@pytest.mark.pcc_gate(probe=lambda: sys.platform == "linux" and platform.machine() == "x86_64")
@pytest.mark.parametrize("name,args,status,stdout", (
    ("arguments_stdout", ["40"], 42, b"visible output\n\n"),
    ("descriptor_control", [], 0, b""),
    ("immediate_exit", [], 19, b""),
    ("immediate_exit", ["posix"], 237, b""),
    ("main_no_return_c89_2", [], 1, b""),
))
def test_original_c_export_controls_execute(tmp_path, monkeypatch, name, args, status, stdout):
    # Required rather than optional: avoid accidental runtime regeneration or
    # qualification against the old archive that motivated these controls.
    from tests.runtime_fixture_provenance import _verified_test_runtime_archive
    explicit = os.environ.get("PCC_RUNTIME_ARCHIVE")
    assert explicit, "provide a source-matching PCC_RUNTIME_ARCHIVE; this test does not build one"
    archive, _manifest = _verified_test_runtime_archive(explicit)
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(archive))
    binary = tmp_path / name
    with monkeypatch.context() as owned:
        def forbidden(*args, **kwargs):
            raise AssertionError("owned C emit/link attempted an external process")
        owned.setattr(subprocess, "Popen", forbidden)
        evaluator, units = _units((FIXTURES / (name + ".c")).read_text(), TARGETS[0])
        evaluator.emit_executable(units, str(binary))
    run = subprocess.run([str(binary), *args], capture_output=True, timeout=10)
    assert (run.returncode, run.stdout, run.stderr) == (status, stdout, b"")


def _model(module, names, **bindings):
    """Run unchanged definitions with explicit machine-boundary test doubles."""
    import ast
    tree = ast.parse((ROOT / "pcc/runtime/py" / (module + ".py")).read_text())
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    assert {node.name for node in functions} == set(names)
    namespace = {
        "i64": int, "c_ptr": object,
        "c_abi_export": lambda *args: lambda function: function,
        "c_abi_typed_export": lambda *args: lambda function: function,
        "c_abi_variadic_export": lambda *args: lambda function: function,
        **bindings,
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), module, "exec"), namespace)
    return namespace


def test_atoi_c_locale_int_boundaries_and_prefixes():
    ns = _model("freestanding_libc_numeric", ("_is_space", "atoi"),
                load_i8=lambda text, offset: text[offset])
    for text, value in ((b"", 0), (b"-", 0), (b"word", 0), (b"0017", 17),
                        (b" +42!", 42), (b"2147483647", 2147483647),
                        (b" \t\r\n\v\f-2147483648", -2147483648)):
        assert ns["atoi"](text + b"\0") == value


def test_close_publishes_errno_without_retrying_eintr():
    calls, errors = [], []
    def raw_close(fd):
        calls.append(fd)
        return -4
    ns = _model("freestanding_linux_libc", ("close_c",),
                close=raw_close, pcc_errno_set=errors.append)
    assert ns["close_c"](123) == -1
    assert calls == [123] and errors == [4]
    ns["close"] = lambda fd: 0
    assert ns["close_c"](123) == 0 and errors == [4]


@pytest.mark.parametrize("machine,number", ((b"x86_64", 72), (b"aarch64", 25)))
def test_fcntl_getters_never_consume_varargs_and_preserve_negative_owner(machine, number):
    calls, errors = [], []
    def absent_argument(*args):
        raise AssertionError("getter attempted to consume an absent vararg")
    def syscall(*args):
        calls.append(args)
        return 0
    ns = _model("freestanding_linux_libc", ("fcntl_c",),
                syscall6=syscall, target_platform_machine=lambda: machine,
                load_i8=lambda data, offset: data[offset], pcc_errno_set=errors.append,
                va_start=absent_argument, stack_alloc=lambda size: (2, 321),
                load_i32=lambda owner, offset: owner[offset // 4])
    for command in (1, 3, 11, 1025, 1028, 1032, 1034):
        assert ns["fcntl_c"](3, command) == 0
        assert calls[-1] == (number, 3, command, 0, 0, 0, 0)
    assert ns["fcntl_c"](3, 9) == -321
    assert calls[-1][2] == 16 and errors == []
    assert ns["fcntl_c"](3, 999999) == -1 and errors == [22]


def test_fcntl_integer_pointer_and_error_contract():
    calls, errors, reads = [], [], []
    ns = _model("freestanding_linux_libc", ("fcntl_c",),
                syscall6=lambda *args: calls.append(args) or -4,
                target_platform_machine=lambda: b"x86_64",
                load_i8=lambda data, offset: data[offset], pcc_errno_set=errors.append,
                va_start=lambda: "cursor", va_end=lambda cursor: reads.append("end"),
                va_arg_i32=lambda cursor: reads.append("i32") or -1,
                va_arg_i64=lambda cursor: reads.append("i64") or 2147483648,
                va_arg_ptr=lambda cursor: reads.append("ptr") or "flock")
    for command, kind, value in ((8, "i32", -1), (1026, "i64", 2147483648), (5, "ptr", "flock")):
        assert ns["fcntl_c"](3, command) == -1
        assert calls[-1] == (72, 3, command, value, 0, 0, 0)
        assert reads[-2:] == [kind, "end"]
    assert len(calls) == 3 and errors == [4, 4, 4]


def test_puts_appends_exactly_one_newline_and_stops_on_write_error():
    writes = []
    ns = _model("freestanding_stdio", ("puts",),
                load_ptr=lambda *args: "stdout", global_addr=lambda symbol: symbol,
                target_sys_platform=lambda: b"linux", load_i8=lambda text, offset: text[offset],
                cstr=lambda text: text.encode(),
                fwrite=lambda text, size, count, stream: writes.append(text[:count]) or count)
    assert ns["puts"](b"hello\0ignored") == 0
    assert writes == [b"hello", b"\n"]
    writes.clear()
    ns["fwrite"] = lambda text, size, count, stream: writes.append(text[:count]) or 0
    assert ns["puts"](b"hello\0") == -1
    assert writes == [b"hello"]


@pytest.mark.integration
@pytest.mark.pcc_gate(probe=lambda: sys.platform == "linux" and platform.machine() == "x86_64")
@pytest.mark.parametrize("name,extra,status,stdout,contents", (
    ("exit_lifecycle", [], 37, b"last\nlate\nfirst\n", b"MBCA"),
    ("exit_lifecycle", ["direct"], 37, b"last\nlate\nfirst\n", b"MBCA"),
    ("flush_all", [], 0, b"", b"saved"),
    ("immediate_bypass", [], 29, b"", b""),
    ("immediate_bypass", ["posix"], 29, b"", b""),
))
def test_original_c_lifecycle_execute(tmp_path, monkeypatch, name, extra, status, stdout, contents):
    from tests.runtime_fixture_provenance import _verified_test_runtime_archive
    explicit = os.environ.get("PCC_RUNTIME_ARCHIVE")
    assert explicit, "provide a source-matching PCC_RUNTIME_ARCHIVE; this test does not build one"
    archive, _manifest = _verified_test_runtime_archive(explicit)
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(archive))
    binary, output = tmp_path / name, tmp_path / "output"
    with monkeypatch.context() as owned:
        def forbidden(*args, **kwargs):
            raise AssertionError("owned C emit/link attempted an external process")
        owned.setattr(subprocess, "Popen", forbidden)
        evaluator, units = _units((FIXTURES / (name + ".c")).read_text(), TARGETS[0])
        evaluator.emit_executable(units, str(binary))
    run = subprocess.run([str(binary), str(output), *extra], capture_output=True, timeout=10)
    assert (run.returncode, run.stdout, run.stderr) == (status, stdout, b"")
    assert output.read_bytes() == contents


class _RawMemory:
    """Deterministic byte-offset memory boundary for lifecycle model controls."""
    def __init__(self):
        self.cells = {}
        self.symbols = {}
        self.next = 4096
        self.allocations = []
        self.freed = []
        self.fail_allocation = None
        self.locked = False

    def global_addr(self, name):
        if name not in self.symbols:
            self.symbols[name] = self.next
            self.next += 4096
        return self.symbols[name]

    def load(self, pointer, offset):
        return self.cells.get(pointer + offset, 0)

    def store(self, pointer, offset, value):
        self.cells[pointer + offset] = value

    def malloc(self, size):
        self.allocations.append(size)
        if len(self.allocations) == self.fail_allocation:
            return 0
        pointer = self.next
        self.next += 4096
        return pointer

    def acquire(self):
        assert not self.locked
        self.locked = True

    def release(self):
        assert self.locked
        self.locked = False

    def cas(self, pointer, offset, expected, desired, *orders):
        old = self.load(pointer, offset)
        if old == expected:
            self.store(pointer, offset, desired)
        return old

    def bindings(self):
        return dict(global_addr=self.global_addr, load_ptr=self.load, load_i64=self.load,
                    store_ptr=self.store, store_i64=self.store, malloc=self.malloc,
                    free=self.freed.append, ptr_add=lambda pointer, offset: pointer + offset,
                    ptr_diff=lambda left, right: left - right, null=lambda: 0,
                    ptr_is_null=lambda pointer: pointer == 0, atomic_cas_i32=self.cas)


def test_atexit_reserve_failure_and_callback_registration_during_teardown():
    memory = _RawMemory()
    calls = []
    ns = _model("freestanding_linux_libc", ("atexit_c", "_run_exit_callbacks"),
                **memory.bindings(), _atexit_acquire=memory.acquire,
                _atexit_release=memory.release,
                call_void_ptr0=lambda callback: callback())
    def callback(value):
        def run():
            assert not memory.locked
            calls.append(value)
        return run
    for index in range(32):
        assert ns["atexit_c"](callback(index)) == 0
    assert memory.allocations == []
    memory.fail_allocation = 1
    assert ns["atexit_c"](callback(999)) == -1
    assert not memory.locked
    memory.fail_allocation = None
    def registering_callback():
        calls.append(32)
        assert ns["atexit_c"](callback(33)) == 0
    assert ns["atexit_c"](registering_callback) == 0
    ns["_run_exit_callbacks"]()
    assert calls == [32, 33, *range(31, -1, -1)]
    assert len(memory.freed) == 2 and not memory.locked
    assert memory.load(memory.global_addr("pcc_c_atexit_head"), 0) == 0


def test_exit_runs_callbacks_reverse_finalizers_new_callbacks_then_flush_even_on_error():
    memory = _RawMemory()
    events = []
    class Exited(Exception):
        pass
    def terminate(status):
        events.append(("exit", status))
        raise Exited
    def flush(stream):
        assert stream == 0
        events.append("flush-error")
        return -1
    ns = _model("freestanding_linux_libc", ("atexit_c", "_run_exit_callbacks", "exit_c"),
                **memory.bindings(), _atexit_acquire=memory.acquire,
                _atexit_release=memory.release, call_void_ptr0=lambda callback: callback(),
                c_fflush=flush, immediate_exit=terminate)
    def finalizer_one():
        events.append("fini-first")
        ns["atexit_c"](lambda: events.append("registered-by-fini"))
    begin = memory.global_addr("__fini_array_start")
    memory.symbols["__fini_array_end"] = begin + 16
    memory.store(begin, 0, finalizer_one)
    memory.store(begin, 8, lambda: events.append("fini-second"))
    ns["atexit_c"](lambda: events.append("first"))
    ns["atexit_c"](lambda: events.append("second"))
    with pytest.raises(Exited):
        ns["exit_c"](-19)
    assert events == ["second", "first", "fini-second", "fini-first",
                      "registered-by-fini", "flush-error", ("exit", -19)]
    with pytest.raises(Exited):
        ns["exit_c"](42)
    assert events[-1] == ("exit", 42) and events.count("first") == 1


# Every stdio layout/flag constant, from the runtime's ABI table, so a new
# flag consulted by the modelled functions cannot drift out of this model.
_STDIO_CONSTANTS = {
    name: value for name, value in ABI_SPEC.items() if name.startswith("stdio.")
}


def _stdio_registry_model(memory, flush):
    return _model("freestanding_stdio", ("_stream_new", "_stream_unregister", "fflush"),
                  **memory.bindings(), abi_constant=_STDIO_CONSTANTS.__getitem__,
                  _registry_acquire=memory.acquire, _registry_release=memory.release,
                  _flush_output=flush, target_sys_platform=lambda: b"linux",
                  load_i8=lambda data, offset: data[offset])


def test_flush_all_keeps_traversing_after_error_and_ignores_removed_stream():
    memory = _RawMemory()
    flushed = []
    fail_stream = []
    def flush(stream):
        flushed.append(stream)
        return -1 if stream in fail_stream else 0
    ns = _stdio_registry_model(memory, flush)
    first = ns["_stream_new"](10, 2, 0)
    removed = ns["_stream_new"](11, 2, 0)
    last = ns["_stream_new"](12, 2, 0)
    ns["_stream_unregister"](removed)
    fail_stream.append(last)
    assert ns["fflush"](0) == -1
    assert flushed == [0, 0, last, first]
    assert removed not in flushed and len(memory.freed) == 1 and not memory.locked


def test_stream_registration_allocation_failure_unwinds_buffer_and_file():
    memory = _RawMemory()
    memory.fail_allocation = 3  # FILE succeeded, buffer succeeded, node failed.
    ns = _stdio_registry_model(memory, lambda stream: 0)
    assert ns["_stream_new"](10, 2, 0) == 0
    assert memory.allocations == [64, 4096, 16]
    assert len(memory.freed) == 2 and not memory.locked
    assert memory.load(memory.global_addr("pcc_stdio_registry_head"), 0) == 0


@pytest.mark.parametrize("target", TARGETS)
@pytest.mark.parametrize("module,signature", (
    ("freestanding_libc_numeric", "i32 @atoi(ptr "),
    ("freestanding_stdio", "i32 @puts(ptr "),
    ("freestanding_c_linux_start", "void @_start(ptr "),
))
def test_affected_runtime_modules_use_production_abi(tmp_path, target, module, signature, deny_external_compilers):
    from pcc.ir.optimization.driver import optimize_ir
    source = ROOT / "pcc/runtime/py" / (module + ".py")
    if not pipeline._is_py_runtime_library_source(str(source)):
        directory = tmp_path / "py_runtime_linux_c_control/py"
        directory.mkdir(parents=True)
        materialized = directory / source.name
        materialized.write_bytes(source.read_bytes())
        source = materialized
    assert pipeline._is_py_runtime_library_source(str(source))
    assert pipeline._source_declares_freestanding_module(source.read_text())
    output = tmp_path / (module + ".ll")
    owned_runtime_build._compile_runtime_module(module, str(source), str(output), target)
    text = optimize_ir(output.read_text(), owned_runtime_build.runtime_ir_passes(pipeline._PY_RUNTIME_DIR))
    output.write_text(text)
    assert signature in text
    if module == "freestanding_stdio":
        assert "i32 @fflush(ptr " in text
    if module == "freestanding_c_linux_start":
        assert re.search(r"call void[^\n]*@exit\(i32 ", text)
    data = emit_owned_object(text, target)
    output.with_suffix(".o").write_bytes(data)
    obj = parse_relocatable(data)
    undefined = {symbol.name for symbol in obj.symbols if symbol.section_index == 0 and symbol.name}
    assert not {name for name in undefined if name.startswith(("py_", "pcc_gc_"))}
    assert not re.search(r"call[^\n]*@(py_box_|py_int_)", text)


def test_public_fclose_and_pclose_remove_registry_nodes():
    memory = _RawMemory()
    constants = dict(_STDIO_CONSTANTS, **{"stdio.flag.standard": 16})
    ns = _model("freestanding_stdio", ("_stream_new", "_stream_unregister", "_stream_release_buffer", "fclose", "pclose", "fflush"),
                **memory.bindings(), abi_constant=constants.__getitem__,
                _registry_acquire=memory.acquire, _registry_release=memory.release,
                _flush_output=lambda stream: 0, close=lambda fd: 0,
                target_sys_platform=lambda: b"linux", load_i8=lambda data, offset: data[offset],
                stack_alloc=memory.malloc, load_i32=memory.load,
                waitpid=lambda pid, status, options: memory.store(status, 0, 0) or pid)
    ordinary = ns["_stream_new"](10, 2, 0)
    child = ns["_stream_new"](11, 2, 123)
    assert ns["fclose"](ordinary) == 0
    assert ns["pclose"](child) == 0
    assert memory.load(memory.global_addr("pcc_stdio_registry_head"), 0) == 0
    assert len(memory.freed) == 6 and not memory.locked


def test_flush_output_retries_eintr_retains_short_write_tail_and_sets_errno():
    memory = _RawMemory()
    constants = dict(_STDIO_CONSTANTS, **{"stdio.flag.error": 4, "stdio.flag.append": 32})
    stream, buffer = memory.malloc(64), memory.malloc(8)
    memory.store(stream, 8, 10)
    memory.store(stream, 16, 2)
    memory.store(stream, 32, buffer)
    memory.store(stream, 48, 5)
    for offset, byte in enumerate(b"ABCDE"):
        memory.store(buffer, offset, byte)
    outcomes = iter((-4, 2, -28))
    writes, errors = [], []
    def write(fd, pointer, size):
        writes.append((fd, pointer, size))
        return next(outcomes)
    def load_byte(pointer, offset):
        return pointer[offset] if isinstance(pointer, bytes) else memory.load(pointer, offset)
    ns = _model("freestanding_stdio", ("_flush_output",), **memory.bindings(),
                abi_constant=constants.__getitem__, write=write, load_i8=load_byte,
                store_i8=memory.store, pcc_errno_set=errors.append,
                target_sys_platform=lambda: b"linux")
    assert ns["_flush_output"](stream) == -1
    assert writes == [(10, buffer, 5), (10, buffer, 5), (10, buffer + 2, 3)]
    assert bytes(memory.load(buffer, offset) for offset in range(3)) == b"CDE"
    assert memory.load(stream, 48) == 3 and memory.load(stream, 16) == 6
    assert errors == [28]
