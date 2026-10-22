"""Portable native process-group boundary for the resource fixture.

Cross-target emission is a component check. Linux execution changes only the
new test child's group; Darwin execution requires a real Darwin runner.
"""

import ast
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import sys

import pytest

from pcc.frontends.python import owned_runtime_build as owned, pipeline
from pcc.backend.owned_object_emit import emit_owned_object
from tests.python.owned_regression_support import explicit_owned_runtime


ROOT = Path(__file__).resolve().parents[2]
DRIVER = ROOT / "tests/fixtures/native/worker_resource_admission.py"
LIBC = ROOT / "pcc/runtime/py/freestanding_linux_libc.py"
TARGETS = ("x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu", "arm64-apple-darwin")


def _function_source(path, name):
    text = path.read_text()
    function = next(node for node in ast.parse(text).body
                    if isinstance(node, ast.FunctionDef) and node.name == name)
    start = min([function.lineno] + [node.lineno for node in function.decorator_list])
    return "".join(text.splitlines(keepends=True)[start - 1:function.end_lineno])


@pytest.mark.parametrize("target", TARGETS)
def test_original_resource_join_function_uses_named_signed_c_abi(tmp_path, target):
    # Compile the original complete helper with its original owner predicate
    # and literal extern declaration, not a hand-rewritten nearby call shape.
    tree = ast.parse(DRIVER.read_text())
    binding = next(node for node in tree.body if isinstance(node, ast.Assign)
                   and any(isinstance(name, ast.Name) and name.id == "_setpgid"
                           for name in node.targets))
    source = "import os\nimport sys\nfrom pcc.extern import c_int, extern\n"
    source += ast.get_source_segment(DRIVER.read_text(), binding) + "\n"
    for name in ("owner_name", "native_owner", "join_process_group"):
        source += "\n" + _function_source(DRIVER, name) + "\n"
    input_path, output = tmp_path / "join.py", tmp_path / "join.ll"
    input_path.write_text(source)
    pipeline.compile_python(
        str(input_path), str(output), emit_llvm_only=True, python_library=True,
        libpython_mode="off", backend="self", target_triple=target,
    )
    text = output.read_text()
    calls = [line for line in text.splitlines() if "call " in line and "@setpgid(" in line]
    assert len(calls) == 1, calls
    assert re.search(r"call i32(?: \(i32, i32\))? @setpgid\(i32 [^,]+, i32 ", calls[0])
    body = re.search(r"^define[^\n]*@user_join_join_process_group\([^\n]*\{\n(.*?)^}", text, re.M | re.S)
    assert body is not None
    assert "strict.nolib.stub" not in body.group(1)
    assert "@py_cpy_" not in body.group(1)
    assert "syscall" not in text
    data = emit_owned_object(text, target)
    output.with_suffix(".o").write_bytes(data)
    if target == TARGETS[2]:
        from pcc.backend import macho_spec as spec
        symbols = spec.parse_object(data).symbols()
        symbol = next(row for row in symbols if row["name"] == "_setpgid")
        assert symbol["n_type"] & spec.N_TYPE == spec.N_UNDF


@pytest.mark.parametrize("machine,number", ((b"x86_64", 109), (b"aarch64", 154)))
@pytest.mark.parametrize("raw,expected,error", ((0, 0, None), (-22, -1, 22), (-3, -1, 3)))
def test_owned_linux_setpgid_numbers_signed_arguments_and_errno(machine, number, raw, expected, error):
    from pcc.frontends.python.codegen.linux_syscalls import _DIRECT

    function = ast.parse(_function_source(LIBC, "setpgid_c")).body[0]
    function.decorator_list = []
    calls, errors = [], []
    namespace = {
        "i64": int, "target_platform_machine": lambda: machine,
        "load_i8": lambda data, index: data[index], "pcc_errno_set": errors.append,
        "syscall6": lambda *args: calls.append(args) or raw,
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(LIBC), "exec"), namespace)
    assert namespace["setpgid_c"](-19, -27) == expected
    assert calls == [(number, -19, -27, 0, 0, 0, 0)]
    assert errors == ([] if error is None else [error])
    assert _DIRECT[109] == 154


@pytest.mark.pcc_gate(probe=lambda: sys.platform.startswith("linux") and platform.machine() in ("x86_64", "amd64"))
def test_owned_linux_setpgid_changes_only_own_child_group(tmp_path):
    from pcc.backend.elf_x86_64 import link_static_executable, parse_relocatable
    from pcc.backend.x86_64_asm_driver import assemble_file
    from pcc.ir.optimization.driver import optimize_ir

    source = tmp_path / "setpgid_leaf.py"
    # Retain the exact production leaf. The errno sink isolates its output;
    # no process is forked and no unrelated PID or process group is changed.
    source.write_text(
        "from pcc import i64\n"
        "from pcc.extern import c_abi_typed_export, c_int32, c_void, extern\n"
        "from pcc.unsafe import syscall6, target_platform_machine, load_i8\n"
        "__pcc_freestanding__ = True\n"
        'pcc_errno_set = extern("pcc_errno_set", (c_int32,), c_void)\n\n'
        + _function_source(LIBC, "setpgid_c") + "\n",
    )
    output = tmp_path / "setpgid_leaf.ll"
    owned._compile_runtime_module(source.stem, str(source), str(output), TARGETS[0])
    text = optimize_ir(output.read_text(), owned.runtime_ir_passes(str(ROOT / "pcc/runtime")))
    assert "define external i32 @setpgid(i32 %pid, i32 %group)" in text
    # Kernel getpid/getpgrp observe this executable's own group. Start without
    # a new session, so setpgid(0,0) is legal for the freshly spawned child.
    entry = assemble_file(""".intel_syntax noprefix
.text
.globl _start
_start:
 mov edi, 0
 mov esi, 0
 call setpgid
 test eax, eax
 jne fail
 mov eax, 39
 syscall
 mov r12, rax
 mov eax, 111
 syscall
 cmp rax, r12
 jne fail
 mov edi, 0
 mov esi, -1
 call setpgid
 cmp eax, -1
 jne fail
 mov edx, dword ptr test_errno[rip]
 cmp edx, 22
 jne fail
 xor edi, edi
 jmp finish
fail:
 mov edi, 1
finish:
 mov eax, 60
 syscall
.globl pcc_errno_set
pcc_errno_set:
 mov dword ptr test_errno[rip], edi
 ret
.data
test_errno:
 .long 0
""")
    data = emit_owned_object(text, TARGETS[0])
    image = link_static_executable([entry, parse_relocatable(data)])
    binary = tmp_path / "setpgid-linux"
    binary.write_bytes(image)
    binary.chmod(0o755)
    import subprocess
    result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10,
                            env=dict(os.environ, PATH=""))
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")


@pytest.mark.integration
@pytest.mark.pcc_gate(probe=lambda: sys.platform == "darwin" and platform.machine() == "arm64")
def test_darwin_resource_fixture_handles_execute(
    tmp_path, explicit_owned_runtime, python_program_compiler, request, capfd,
):
    from tests.python.test_worker_resource_plan import _execute_resource_driver

    binary = tmp_path / "resource-admission"
    receipt = {
        "status": "COMPILING", "backend": "self", "libpython": "off",
        "compiler_parameter": request.node.callspec.params["python_program_compiler"],
        "compiler_module": python_program_compiler.__module__,
        "source": str(DRIVER),
        "source_sha256": hashlib.sha256(DRIVER.read_bytes()).hexdigest(),
        "runtime_archive": str(explicit_owned_runtime),
        "runtime_sha256": hashlib.sha256(explicit_owned_runtime.read_bytes()).hexdigest(),
        "qualification_scope": "Darwin native resource handles component; GC0",
        "pcc1_stage2_worker_dispatch_proved": False,
    }
    receipt_path = tmp_path / "darwin-resource-handles.json"

    def save():
        receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")

    save()
    try:
        python_program_compiler(
            str(DRIVER), str(binary), backend="self", libpython_mode="off",
            ir_scaffold_mode="on", runtime_archive=str(explicit_owned_runtime),
        )
    except Exception as error:
        receipt.update(status="COMPILE_FAILED", error=type(error).__name__ + ": " + str(error))
        save()
        raise
    finally:
        captured = capfd.readouterr()
        (tmp_path / "compile.stdout").write_text(captured.out)
        (tmp_path / "compile.stderr").write_text(captured.err)
    assert binary.read_bytes()[:4] == b"\xcf\xfa\xed\xfe"
    receipt.update(status="RUNNING", binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest())
    save()
    try:
        receipt["execution"] = _execute_resource_driver(
            [str(binary)], "handles", tmp_path / "handles", "pcc", 0,
        )
    except BaseException as error:
        receipt.update(status="NATIVE_EXECUTION_FAILED", error=type(error).__name__ + ": " + str(error))
        save()
        raise
    receipt["status"] = "PASS"
    save()
