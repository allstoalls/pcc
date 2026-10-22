"""C-string globals retain their byte type without redundant SSA GEPs."""
from __future__ import annotations

import platform
import subprocess
from unittest.mock import patch

import pytest

from pcc.frontends.python.codegen.string_globals_lowering import StringGlobalsLoweringMixin
from pcc.ir import ir
from pcc.tools.ir_to_obj import emit_object


_I8 = ir.IntType(8)
_I32 = ir.IntType(32)
_TARGETS = (
    "x86_64-unknown-linux-gnu",
    "aarch64-unknown-linux-gnu",
    "aarch64-apple-darwin",
    "x86_64-pc-windows-msvc",
)


def _lowering():
    lower = StringGlobalsLoweringMixin()
    lower.module = ir.Module("string_global_pointers")
    lower._cstr_pool = {}
    lower._cstr_counter = 0
    function = ir.Function(lower.module, ir.FunctionType(_I32, []), "probe")
    lower.builder = ir.IRBuilder(function.append_basic_block("entry"))
    return lower


@pytest.mark.parametrize("payload", ["", "ASCII", "é☃😀", "a\0b", '"\\\n'])
def test_cstr_pointer_is_typed_global_operand(payload):
    lower = _lowering()
    global_ = lower._pooled_cstr_global(payload)
    global_.align = 16
    before = len(lower.builder.block.instructions)
    first = lower._pooled_cstr_ptr(payload)
    second = lower._pooled_cstr_ptr(payload)
    assert first.type == ir.PointerType(_I8)
    assert str(first) == str(second) == str(global_)
    assert len(lower.builder.block.instructions) == before
    assert lower._pooled_cstr_global(payload) is global_
    assert global_.align == 16
    assert global_.initializer.value == list(payload.encode("utf-8")) + [0]


@pytest.mark.parametrize("addrspace", [0, 3])
def test_generic_pointer_keeps_quoted_gep_and_address_space(addrspace):
    lower = _lowering()
    global_ = ir.GlobalVariable(lower.module, ir.ArrayType(_I8, 1),
                                name='"quoted space"', addrspace=addrspace)
    pointer = lower._ptr_to_cstr(global_)
    assert str(pointer).startswith("%")
    assert len(lower.builder.block.instructions) == 1
    assert '@"quoted space", i32 0, i32 0' in str(lower.module)
    if addrspace:
        assert 'ptr addrspace(3)' in str(lower.module)


def test_pool_collision_cannot_retype_an_unrelated_global():
    lower = _lowering()
    unrelated = ir.GlobalVariable(lower.module, _I32, name=".cstr.1", addrspace=3)
    unrelated.initializer = ir.Constant(_I32, 7)
    pointer = lower._pooled_cstr_ptr("owned")
    global_ = lower._cstr_pool["owned"]
    assert global_ is not unrelated
    assert global_.name == ".cstr.2"
    assert global_.addrspace == 0
    assert global_.value_type == ir.ArrayType(_I8, 6)
    assert str(pointer) == str(global_)
    assert pointer.type == ir.PointerType(_I8)


def test_nonbyte_global_retains_general_gep_type():
    lower = _lowering()
    global_ = ir.GlobalVariable(lower.module, ir.ArrayType(_I32, 2), name="ints")
    pointer = lower._ptr_to_cstr(global_)
    assert pointer.type == ir.PointerType(_I32)
    assert len(lower.builder.block.instructions) == 1
    assert "getelementptr inbounds [2 x i32]" in str(lower.module)


def test_cstr_pointer_is_valid_in_distinct_functions_and_blocks():
    lower = _lowering()
    first = lower._pooled_cstr_ptr("path", 'path/with"unsafe-name')
    global_ = lower._cstr_pool["path"]
    lower.builder.ret(ir.Constant(_I32, 0))
    function = ir.Function(lower.module, ir.FunctionType(ir.PointerType(_I8), []), "other")
    entry = function.append_basic_block("entry")
    left = function.append_basic_block("left")
    right = function.append_basic_block("right")
    lower.builder = ir.IRBuilder(entry)
    lower.builder.cbranch(ir.Constant(ir.IntType(1), 1), left, right)
    lower.builder.position_at_end(left)
    lower.builder.ret(first)
    lower.builder.position_at_end(right)
    lower.builder.ret(lower._pooled_cstr_ptr("path"))
    assert global_.name.startswith(".cstr.symbol.")
    assert "getelementptr" not in str(lower.module)
    from pcc.backend.self_backend_parse import parse_self_backend_module
    from pcc.backend.self_backend_verify import verify_parsed_module
    verify_parsed_module(parse_self_backend_module(str(lower.module)))


def _executable_module():
    lower = _lowering()
    errors = ir.Constant(_I32, 0)
    for payload in ("", "ASCII", "é☃😀", "a\0b", '"\\\n'):
        global_ = lower._pooled_cstr_global(payload)
        global_.align = 16
        pointer = lower._pooled_cstr_ptr(payload)
        for offset, expected in enumerate(list(payload.encode("utf-8")) + [0]):
            address = lower.builder.gep(pointer, [ir.Constant(_I32, offset)])
            actual = lower.builder.load(address)
            wrong = lower.builder.icmp_unsigned("!=", actual, ir.Constant(_I8, expected))
            errors = lower.builder.add(errors, lower.builder.zext(wrong, _I32))
        again = lower._pooled_cstr_ptr(payload)
        wrong = lower.builder.icmp_unsigned("!=", pointer, again)
        errors = lower.builder.add(errors, lower.builder.zext(wrong, _I32))
    lower.builder.ret(errors)
    return str(lower.module)


@pytest.mark.parametrize("target", _TARGETS)
def test_cstr_pointer_owned_object_emission(target):
    with patch.object(subprocess, "Popen", side_effect=AssertionError("external build process")):
        data = emit_object(_executable_module(), target_triple=target)
    assert data


# execution needs the x86-64 Linux host
@pytest.mark.pcc_gate(probe=lambda: not (platform.system() != "Linux" or platform.machine().lower() not in ("x86_64", "amd64")))
def test_cstr_pointer_owned_linux_executable(tmp_path):
    from pcc.backend.elf_x86_64 import link_static_executable, parse_relocatable
    from pcc.backend.x86_64_asm_driver import assemble_file

    startup = ".intel_syntax noprefix\n.text\n.globl _start\n_start:\n call probe\n mov edi, eax\n mov eax, 60\n syscall\n"
    with patch.object(subprocess, "Popen", side_effect=AssertionError("external build process")):
        data = emit_object(_executable_module(), target_triple=_TARGETS[0])
        image = link_static_executable([parse_relocatable(data), assemble_file(startup)])
    executable = tmp_path / "cstr_pointer"
    executable.write_bytes(image)
    executable.chmod(0o755)
    result = subprocess.run([str(executable)], capture_output=True, timeout=10, env={"PATH": ""})
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")
