"""Public Linux abort semantics, separate from the internal fatal primitive."""

from __future__ import annotations

import ast
import copy
from pathlib import Path
import platform
import signal
import subprocess
import sys
from unittest.mock import patch

import pytest

from pcc import build
from pcc.backend.elf_x86_64 import STB_GLOBAL, parse_relocatable
from pcc.backend.owned_object_emit import emit_owned_object
from pcc.frontends.python.pipeline import compile_python


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "pcc/runtime/py/freestanding_linux_signal.py"
TARGETS = ("x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu")
_LINUX_HOST = sys.platform == "linux" and platform.machine().lower() in (
    "x86_64", "amd64", "aarch64", "arm64",
)
_native_linux = pytest.mark.pcc_gate(unavailable=None if _LINUX_HOST else "needs Linux x86_64 or AArch64 execution")


@pytest.mark.parametrize("machine, calls", [
    (b"x86_64", (39, 186, 14, 234, 13, 231)),
    (b"aarch64", (172, 178, 135, 131, 134, 94)),
])
def test_abort_source_model_targets_calling_thread_and_never_returns(machine, calls):
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "abort")
    assert len(function.decorator_list) == 1
    decorator = function.decorator_list[0]
    assert isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Name)
    assert decorator.func.id == "c_abi_export" and ast.literal_eval(decorator.args[0]) == "abort"
    function = copy.deepcopy(function)
    function.decorator_list = []
    constants = [node for node in tree.body if isinstance(node, ast.Assign)
                 and any(isinstance(target, ast.Name) and target.id in
                         ("_KERNEL_SIGNAL_MASK_BYTES", "_KERNEL_SIGNAL_ACTION_BYTES")
                         for target in node.targets)]
    assert len(constants) == 2
    unit = ast.fix_missing_locations(ast.Module(body=constants + [function], type_ignores=[]))
    observed = []
    pid_call, tid_call, mask_call, send_call, action_call, exit_call = calls

    class ExitReached(Exception):
        pass

    def syscall(number, *arguments):
        saved = tuple(bytes(value) if isinstance(value, bytearray) else value for value in arguments)
        observed.append((number, saved))
        if number == pid_call:
            return 401
        if number == tid_call:
            return 409
        if number == exit_call:
            raise ExitReached
        return 0

    def store_i64(buffer, offset, value):
        buffer[offset:offset + 8] = value.to_bytes(8, "little")

    def memset(buffer, value, count):
        buffer[:count] = bytes([value]) * count

    namespace = {"i64": int, "stack_alloc": bytearray, "load_i8": lambda buf, at: buf[at],
                 "target_platform_machine": lambda: machine, "store_i64": store_i64,
                 "syscall6": syscall, "null": lambda: None, "memset": memset}
    exec(compile(unit, str(SOURCE), "exec"), namespace)
    with pytest.raises(ExitReached):
        namespace["abort"]()
    mask = (32).to_bytes(8, "little")
    assert observed == [
        (pid_call, (0, 0, 0, 0, 0, 0)),
        (tid_call, (0, 0, 0, 0, 0, 0)),
        (mask_call, (1, mask, None, 8, 0, 0)),
        (send_call, (401, 409, 6, 0, 0, 0)),
        (action_call, (6, bytes(32), None, 8, 0, 0)),
        (mask_call, (1, mask, None, 8, 0, 0)),
        (send_call, (401, 409, 6, 0, 0, 0)),
        (exit_call, (134, 0, 0, 0, 0, 0)),
    ]


@pytest.mark.parametrize("target", TARGETS)
def test_whole_linux_signal_module_emits_owned_abort_export(tmp_path, target):
    ir_path = tmp_path / "signal.ll"
    with patch.object(subprocess, "Popen", side_effect=AssertionError("external compiler invoked")):
        compile_python(str(SOURCE), str(ir_path), emit_llvm_only=True, libpython_mode="off",
                       python_library=True, backend="self", target_triple=target)
        ir = ir_path.read_text(encoding="utf-8")
        image = emit_owned_object(ir, target)
    (tmp_path / "signal.o").write_bytes(image)
    headers = [line for line in ir.splitlines() if line.startswith("define ") and "@abort(" in line]
    assert len(headers) == 1 and headers[0].startswith("define external void @abort()"), headers
    assert "@sigaction(" in ir and "@sigemptyset(" in ir
    parsed = parse_relocatable(image)
    assert any(symbol.name == "abort" and symbol.section_index != 0 and symbol.binding == STB_GLOBAL
               for symbol in parsed.symbols)


@_native_linux
@pytest.mark.parametrize("blocked, ignored, handler", [
    (False, False, False), (True, False, False),
    (False, True, False), (True, True, False),
    (False, False, True), (True, False, True),
])
def test_owned_abort_terminates_with_sigabrt(
    tmp_path, monkeypatch, pcc_runtime_archive, blocked, ignored, handler,
):
    # The strict fixture validates current target/source/codegen/configuration.
    # Automatic archive construction is separately controlled by the suite.
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))
    source = tmp_path / "abort.c"
    source.write_text(r"""
#include <signal.h>
extern void abort(void);
extern long pcc_platform_write(long, const void *, long);
enum { linux_sigabrt = 6 };
void returning_handler(int number) {
    char marker = number == linux_sigabrt ? 'H' : 'X';
    pcc_platform_write(1, &marker, 1);
}
int main(void) {
    if (INSTALL_HANDLER) {
        struct sigaction action = {0};
        action.sa_handler = returning_handler;
        if (sigaction(linux_sigabrt, &action, 0)) return 40;
    }
    abort();
    return 41;
}
""".replace("INSTALL_HANDLER", "1" if handler else "0"), encoding="utf-8")
    with patch.object(subprocess, "Popen", side_effect=AssertionError("external compiler invoked")):
        artifact = build(source, kind="exe", backend="self", optimize=0,
                         out_dir=tmp_path / "output", use_compile_cache=False)
    assert artifact.backend == "self"

    def child_signal_state():
        import resource
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        signal.signal(signal.SIGABRT, signal.SIG_IGN if ignored else signal.SIG_DFL)
        signal.pthread_sigmask(signal.SIG_SETMASK, {signal.SIGABRT} if blocked else set())

    result = subprocess.run([artifact.output_path], capture_output=True, text=True,
                            timeout=10, preexec_fn=child_signal_state)
    assert result.returncode == -signal.SIGABRT, (result.returncode, result.stdout, result.stderr)
    assert result.stdout == ("H" if handler else "")
    assert result.stderr == ""
