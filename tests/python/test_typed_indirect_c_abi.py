"""Generic raw C ABI calls through the owned frontend/emitter/linker only."""
from __future__ import annotations

import platform
import re
import struct
import subprocess

import pytest

from pcc.backend.elf_x86_64 import link_static_executable, parse_relocatable, parse_static_executable
from pcc.backend.macho_spec import CPU_TYPE_ARM64, MH_OBJECT, parse_object
from pcc.backend.owned_object_emit import emit_owned_object
from pcc.backend.self_backend import emit_aarch64_darwin_asm
from pcc.backend.target_objects import encode_assembly_object
from pcc.frontends.python import pipeline

LINUX = "x86_64-unknown-linux-gnu"
DARWIN = "arm64-apple-darwin"
HEADER = '''from pcc import i64
from pcc.extern import c_abi_export, c_abi_typed_export, c_ptr
from pcc.unsafe import call_c_abi, c_abi_sizeof, c_abi_alignof, global_addr, stack_alloc, ptr_add, ptr_to_int, store_i64, syscall6
__pcc_freestanding__ = True
'''


def _compile(tmp_path, body, target=LINUX):
    source = tmp_path / "typed_call.py"
    output = tmp_path / "typed_call.ll"
    source.write_text(HEADER + body)
    pipeline.compile_python(str(source), str(output), emit_llvm_only=True,
        python_library=True, libpython_mode="off", backend="self", target_triple=target)
    return output.read_text()


@pytest.mark.parametrize("expression,diagnostic", [
    ('call_c_abi(fn, kind, (), (), None)', "type must be a string literal"),
    ('call_c_abi(fn, "mystery", (), (), None)', "unsupported typed C ABI type"),
    ('call_c_abi(fn, "{}", (), (), result)', "aggregate cannot be empty"),
    ('call_c_abi(fn, "{f64,void}", (), (), result)', "void typed C ABI aggregate field"),
    ('call_c_abi(fn, "{f64,}", (), (), result)', "empty typed C ABI aggregate field"),
    ('call_c_abi(fn, "void", types, (), None)', "argtypes must be a tuple literal"),
    ('call_c_abi(fn, "void", ("void",), (1,), None)', "arguments cannot be void"),
    ('call_c_abi(fn, "void", ("i64",), (), None)', "values must be a tuple literal matching argtypes"),
    ('call_c_abi(fn, "void", ("{f64,f64}",), ((1.0,),), None)', "matching nested tuple literal"),
    ('call_c_abi(fn, "void", ("{{f64,f64},f64}",), ((1.0,2.0,3.0),), None)', "matching nested tuple literal"),
    ('call_c_abi(fn, "void", ("i64",), ((1,),), None)', "scalar argument cannot be a tuple"),
    ('call_c_abi(fn, "void", ("i64",), ("oops",), None)', "incompatible value type"),
    ('call_c_abi(fn, "void", (), (), result)', "void result requires None storage"),
    ('call_c_abi(fn, "f64", (), (), None)', "non-void result requires storage"),
    ('call_c_abi(fn, "void", (), ())', "expects 5 positional args"),
    ('call_c_abi(fn, "void", (), (), result=None)', "expects 5 positional args"),
    ('c_abi_sizeof("void")', "void has no storage"),
    ('c_abi_alignof("{}")', "aggregate cannot be empty"),
    ('c_abi_sizeof(kind)', "type must be a string literal"),
], ids=["dynamic-return", "unknown-return", "empty-aggregate", "void-field", "trailing-field",
        "dynamic-argtypes", "void-argument", "wrong-count", "wrong-shape", "wrong-nesting",
        "tuple-scalar", "string-integer", "void-storage", "missing-storage", "wrong-arity",
        "keyword", "sizeof-void", "alignof-empty", "dynamic-sizeof"])
def test_typed_call_rejects_invalid_schema(tmp_path, expression, diagnostic):
    source = '\n@c_abi_export("probe")\ndef probe(fn, result, kind, types) -> None:\n    ' + expression + '\n'
    with pytest.raises((ValueError, NotImplementedError), match=re.escape(diagnostic)):
        _compile(tmp_path, source)


CALLEES = '''
define double @ffi_mix(ptr %out, i32 %count, float %small, double %large) {
entry:
  %n = sext i32 %count to i64
  store i64 %n, ptr %out
  %wide = fpext float %small to double
  %sum = fadd double %wide, %large
  ret double %sum
}
define {double,double} @ffi_pair({double,double} %value) {
entry:
  %a = extractvalue {double,double} %value, 0
  %b = extractvalue {double,double} %value, 1
  %x = fadd double %a, 1.0
  %y = fsub double %b, 2.0
  %r0 = insertvalue {double,double} undef, double %x, 0
  %r1 = insertvalue {double,double} %r0, double %y, 1
  ret {double,double} %r1
}
define {{double,double},{double,double}} @ffi_rect({{double,double},{double,double}} %value) {
entry:
  %left = extractvalue {{double,double},{double,double}} %value, 0
  %right = extractvalue {{double,double},{double,double}} %value, 1
  %r0 = insertvalue {{double,double},{double,double}} undef, {double,double} %right, 0
  %r1 = insertvalue {{double,double},{double,double}} %r0, {double,double} %left, 1
  ret {{double,double},{double,double}} %r1
}
define {i8,double} @ffi_mixed({i8,double} %value) {
entry:
  ret {i8,double} %value
}
define ptr @ffi_pointer(ptr %value) {
entry:
  ret ptr %value
}
define void @ffi_sink(ptr %out, i64 %value) {
entry:
  store i64 %value, ptr %out
  ret void
}
'''

CALLER = '''
@c_abi_export("raw_address")
def raw_address(value: c_ptr) -> c_ptr:
    return value

@c_abi_export("probe")
def probe(output: c_ptr) -> None:
    call_c_abi(raw_address(global_addr("ffi_mix")), "f64", ("ptr", "i32", "f32", "f64"), (raw_address(output), 7, 1.5, 2.25), raw_address(ptr_add(output, 8)))
    call_c_abi(global_addr("ffi_pair"), "{f64,f64}", ("{f64,f64}",), ((3.5, 8.25),), ptr_add(output, 16))
    call_c_abi(global_addr("ffi_rect"), "{{f64,f64},{f64,f64}}", ("{{f64,f64},{f64,f64}}",), (((1.0, 2.0), (3.0, 4.0)),), ptr_add(output, 32))
    call_c_abi(global_addr("ffi_mixed"), "{i8,f64}", ("{i8,f64}",), ((9, 12.5),), ptr_add(output, 64))
    call_c_abi(global_addr("ffi_pointer"), "ptr", ("ptr",), (output,), ptr_add(output, 80))
    call_c_abi(global_addr("ffi_sink"), "void", ("ptr", "i64"), (ptr_add(output, 88), 123), None)
    store_i64(output, 96, c_abi_sizeof("{i8,f64}"))
    store_i64(output, 104, c_abi_alignof("{i8,f64}"))
    store_i64(output, 112, c_abi_sizeof("{{f64,f64},{f64,f64}}"))
    store_i64(output, 120, c_abi_alignof("{{f64,f64},{f64,f64}}"))
'''


def _check_linux_execution(tmp_path, sysv_oracle=False):
    body = CALLER + '''
@c_abi_export("_start")
def start(initial_stack) -> None:
    output = stack_alloc(144)
    probe(output)
    store_i64(output, 128, ptr_to_int(output))
    store_i64(output, 136, ptr_to_int(output) % c_abi_alignof("{i8,f64}"))
    syscall6(1, 1, output, 144, 0, 0, 0)
    syscall6(60, 0, 0, 0, 0, 0, 0)
'''
    text = _compile(tmp_path, body)
    caller = emit_owned_object(text, LINUX)
    callee_text = 'target triple = "' + LINUX + '"\n' + CALLEES
    callee = (encode_assembly_object(SYSV_CALLEES, LINUX) if sysv_oracle
              else emit_owned_object(callee_text, LINUX))
    if sysv_oracle:
        (tmp_path / "sysv_reference.s").write_text(SYSV_CALLEES)
    (tmp_path / "typed_call.o").write_bytes(caller)
    (tmp_path / "callees.ll").write_text(callee_text)
    (tmp_path / "callees.o").write_bytes(callee)
    image = link_static_executable([parse_relocatable(caller), parse_relocatable(callee)])
    parse_static_executable(image)
    executable = tmp_path / "typed-call"
    executable.write_bytes(image)
    executable.chmod(0o755)
    result = subprocess.run([str(executable)], capture_output=True, timeout=15)
    (tmp_path / "native.stdout").write_bytes(result.stdout)
    assert result.returncode == 0, (result.returncode, result.stderr)
    assert len(result.stdout) == 144
    data = result.stdout
    pair = (8.25, 3.5) if sysv_oracle else (4.5, 6.25)
    assert struct.unpack_from("<qddd", data) == (7, 3.75, *pair)
    assert struct.unpack_from("<dddd", data, 32) == (3.0, 4.0, 1.0, 2.0)
    assert data[64] == 9
    assert struct.unpack_from("<d", data, 72) == (12.5,)
    assert struct.unpack_from("<Q", data, 80) == struct.unpack_from("<Q", data, 128)
    assert struct.unpack_from("<qqqqq", data, 88) == (123, 16, 8, 32, 8)
    assert struct.unpack_from("<q", data, 136) == (0,)
    calls = [line for line in text.splitlines() if " call " in line]
    assert any("call double (ptr, i32, float, double) %" in line for line in calls)
    assert any("call { { double, double }, { double, double } } (" in line for line in calls)
    assert "@py_" not in "\n".join(line for line in text.splitlines() if " call " in line)


@pytest.mark.pcc_gate(probe=lambda: platform.system() == "Linux" and platform.machine() == "x86_64")
def test_owned_linux_executes_typed_indirect_scalar_and_structural_abi(tmp_path):
    _check_linux_execution(tmp_path)


@pytest.mark.pcc_gate(probe=lambda: platform.system() == "Linux" and platform.machine() == "x86_64")
def test_owned_linux_call_matches_independent_sysv_register_oracle(tmp_path):
    _check_linux_execution(tmp_path, sysv_oracle=True)


def test_owned_darwin_object_uses_indirect_hfa_and_mixed_scalar_registers(tmp_path):
    text = _compile(tmp_path, CALLER, DARWIN)
    asm = emit_aarch64_darwin_asm(text)
    data = emit_owned_object(text, DARWIN)
    obj = parse_object(data)
    (tmp_path / "typed_call.o").write_bytes(data)
    (tmp_path / "typed_call.s").write_text(asm)
    assert obj.header["cputype"] == CPU_TYPE_ARM64
    assert obj.header["filetype"] == MH_OBJECT
    assert "blr " in asm
    text_section = next(section for section in obj.sections() if section["sectname"].rstrip(b"\0") == b"__text")
    code = data[text_section["offset"]:text_section["offset"] + text_section["size"]]
    words = [word[0] for word in struct.iter_unpack("<I", code)]
    assert sum(word & 0xFFFFFC1F == 0xD63F0000 for word in words) == 6
    # CGRect's recursively nested four f64 lanes go through d0..d3, including
    # the return, rather than four integer coordinate parameters or an sret.
    for index in range(4):
        assert "ldr d" + str(index) in asm
        assert "str d" + str(index) in asm
    mixed_call = asm.split("_ffi_mix@GOTPAGE", 1)[1].split("blr ", 1)[0]
    assert re.search(r"ldu?r s0,", mixed_call) is not None
    assert "fmov d1," in mixed_call
    calls = [line for line in text.splitlines() if " call " in line]
    assert any("call { { double, double }, { double, double } } (" in line for line in calls)
    (tmp_path / "typed_call.s").write_text(asm)


def test_typed_call_keeps_managed_operands_rooted_through_callback_and_store(tmp_path):
    source = tmp_path / "managed_call.py"
    output = tmp_path / "managed_call.ll"
    source.write_text('''from pcc import i64
from pcc.unsafe import call_c_abi

def probe(fn: i64, destination: i64, obj, make) -> None:
    call_c_abi(fn, "i64", ("ptr", "{ptr,i64}"), (obj, (make(), 7)), destination)
''')
    pipeline.compile_python(str(source), str(output), emit_llvm_only=True,
        python_library=True, libpython_mode="off", backend="self", target_triple=LINUX)
    text = output.read_text()
    body = next(part for part in text.split("define ") if "@user_managed_call_probe(" in part).split("\n}", 1)[0]
    lines = body.splitlines()
    foreign = next(i for i, line in enumerate(lines) if "call i64 (ptr, { ptr, i64 }) %" in line)
    producer = next(i for i, line in enumerate(lines) if "@py_obj_call(" in line)
    assert any("@pcc_gc_store_root(" in line for line in lines[:producer])
    assert any("@pcc_gc_load_ptr(" in line for line in lines[producer:foreign])
    assert any("@pcc_gc_pin(" in line for line in lines[producer:foreign])
    store = next(i for i in range(foreign + 1, len(lines)) if "store i64 %abi.result" in lines[i])
    unpin = next(i for i in range(foreign + 1, len(lines)) if "@pcc_gc_unpin(" in lines[i])
    assert foreign < store < unpin
    assert "abi.argument.cleanup" in body
    assert "abi.argument" in body


# Independent SysV ABI oracle: register names, stack offsets and hidden-sret
# are written directly from the ABI, not classified by PCC's IR call backend.
# PCC's own assembler/object writer still owns all emitted bytes.
SYSV_CALLEES = """
.intel_syntax noprefix
.text
.globl ffi_mix
ffi_mix:
  movsxd rax, esi
  mov QWORD PTR [rdi], rax
  cvtss2sd xmm0, xmm0
  addsd xmm0, xmm1
  ret
.globl ffi_pair
ffi_pair:
  movsd xmm2, xmm0
  movsd xmm0, xmm1
  movsd xmm1, xmm2
  ret
.globl ffi_rect
ffi_rect:
  mov rax, QWORD PTR [rsp + 24]
  mov QWORD PTR [rdi], rax
  mov rax, QWORD PTR [rsp + 32]
  mov QWORD PTR [rdi + 8], rax
  mov rax, QWORD PTR [rsp + 8]
  mov QWORD PTR [rdi + 16], rax
  mov rax, QWORD PTR [rsp + 16]
  mov QWORD PTR [rdi + 24], rax
  mov rax, rdi
  ret
.globl ffi_mixed
ffi_mixed:
  mov rax, rdi
  ret
.globl ffi_pointer
ffi_pointer:
  mov rax, rdi
  ret
.globl ffi_sink
ffi_sink:
  mov QWORD PTR [rdi], rsi
  ret
.section .note.GNU-stack,"",@progbits
"""


@pytest.mark.parametrize("mode", ["freestanding", "runtime_port"])
@pytest.mark.parametrize("marker,alias", [("c_ptr", "c_ptr"), ("c_ptr", "raw_pointer"), ("c_rawptr", "raw_pointer")], ids=["direct", "alias", "rawptr"])
def test_typed_call_accepts_imported_pointer_annotations(tmp_path, mode, marker, alias):
    source = tmp_path / "annotated_pointer.py"
    output = tmp_path / "annotated_pointer.ll"
    source.write_text(f'''from pcc.extern import {marker} as {alias}, c_abi_export
from pcc.unsafe import call_c_abi
__pcc_{mode}__ = True

@c_abi_export("address")
def address(value: {alias}) -> {alias}:
    return value

@c_abi_export("probe")
def probe(fn: {alias}, value: {alias}, result: {alias}) -> None:
    call_c_abi(address(fn), "ptr", ("ptr", "{{ptr,i64}}"), (address(value), (value, 1)), address(result))
''')
    pipeline.compile_python(str(source), str(output), emit_llvm_only=True,
        python_library=True, libpython_mode="off", backend="self", target_triple=LINUX)
    calls = [line for line in output.read_text().splitlines() if " call " in line]
    assert any("call ptr (ptr, { ptr, i64 }) %" in line for line in calls)
    assert not any("@pcc_gc_pin(" in line for line in calls)


@pytest.mark.parametrize("annotation", ["float", "bool", "str", "Unrelated"], ids=["float", "bool", "str", "class"])
def test_typed_call_rejects_incompatible_pointer_annotations(tmp_path, annotation):
    body = f'''
@c_abi_export("probe")
def probe(fn: {annotation}) -> None:
    call_c_abi(fn, "void", (), (), None)
'''
    with pytest.raises(ValueError, match="incompatible value type"):
        _compile(tmp_path, body)
