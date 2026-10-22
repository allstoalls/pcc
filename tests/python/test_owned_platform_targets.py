"""Cross-host format/ABI regressions; target execution is a separate gate."""

import inspect
import struct

import pytest

from pcc.backend.self_backend_dispatch import emit_self_asm
from pcc.backend.self_backend_targets import resolve_self_backend_target
from pcc.ir import ir

TARGETS = (
    ("aarch64-unknown-linux-gnu", "self-aarch64-linux-v0"),
    ("x86_64-unknown-linux-gnu", "self-x86_64-linux-v0"),
    ("x86_64-pc-windows-msvc", "self-x86_64-windows-v0"),
)


@pytest.mark.parametrize("target,identity", TARGETS)
def test_owned_target_emits_real_object(target, identity):
    from pcc.tools.ir_to_obj import _emit_object_with_triple
    text = f'''target triple = "{target}"
define i64 @sum(i64 %a, i64 %b) {{
entry:
  %r = add i64 %a, %b
  ret i64 %r
}}
'''
    assert resolve_self_backend_target(target).identity == identity
    obj, actual, owner = _emit_object_with_triple(text, target_triple=target)
    assert actual == target
    assert owner == "pcc"
    if "windows" in target:
        from pcc.backend.coff_x86_64 import parse_object
        parsed = parse_object(obj)
        assert any(s.name == "sum" and s.section for s in parsed.symbols)
        assert any(s.name == ".pdata" and s.data for s in parsed.sections)
        assert any(s.name == ".xdata" and s.data for s in parsed.sections)
    else:
        from pcc.backend.elf_x86_64 import parse_relocatable
        parsed = parse_relocatable(obj)
        assert parsed.machine == (183 if target.startswith("aarch64") else 62)
        assert any(s.name == "sum" and s.section_index for s in parsed.symbols)


@pytest.mark.parametrize("target,opcode", [(TARGETS[0][0], "svc #0"), (TARGETS[1][0], "syscall")])
def test_syscall_builder_uses_module_target_without_new_scaffold_keywords(target, opcode):
    module = ir.Module(name="syscall_target")
    module.triple = target
    i64 = ir.IntType(64)
    fn = ir.Function(module, ir.FunctionType(i64, []), name="getpid")
    builder = ir.IRBuilder(fn.append_basic_block("entry"))
    zero = ir.Constant(i64, 0)
    number = ir.Constant(i64, 172 if target.startswith("aarch64") else 39)
    result = builder.syscall6(number, zero, zero, zero, zero, zero, zero, name="result")
    builder.ret(result)
    assert 'asm sideeffect "' + opcode + '"' in str(module)
    assert "arch" not in inspect.signature(ir.IRBuilder.syscall6).parameters
    assembly = emit_self_asm(str(module))
    assert "  " + opcode in assembly


def test_aarch64_elf_relocations_and_tls_layout():
    from pcc.backend.arm64_elf_driver import assemble_file
    from pcc.backend.elf_x86_64 import emit_relocatable, parse_relocatable, link_static_executable
    obj = assemble_file('''.section __DATA,__thread_data,thread_local_regular
.p2align 3
.globl tls_value
tls_value:
  .quad 37
.section __TEXT,__text,regular,pure_instructions
.globl _start
_start:
  adrp x9, tls_value@GOTPAGE
  ldr x9, [x9, tls_value@GOTPAGEOFF]
  mrs x10, tpidr_el0
  add x0, x9, x10
  mov x8, #94
  svc #0
''')
    parsed = parse_relocatable(emit_relocatable(obj))
    assert parsed.machine == 183
    assert {r.type for s in parsed.sections for r in s.relocations} == {541, 542}
    image = link_static_executable([parsed])
    phoff = struct.unpack_from("<Q", image, 32)[0]
    phnum = struct.unpack_from("<H", image, 56)[0]
    headers = [struct.unpack_from("<IIQQQQQQ", image, phoff + i * 56) for i in range(phnum)]
    tls = next(h for h in headers if h[0] == 7)
    assert tls[5:7] == (8, 8)
    assert all(h[0] in (1, 7) for h in headers)
    # The initial-exec GOT slot contains variant-I TCB size, not a VMA or
    # the negative offset used by x86-64 TLS.
    assert struct.pack("<Q", 16) in image[4096:]


@pytest.mark.parametrize("kind,target,place,mask", [
    (283, 0x1008, 0x1000, 0x94000002),
    (275, 0x4000, 0x1000, 0xF0000000),
    (277, 0x4ABC, 0x1000, 0x912AF000),
])
def test_aarch64_relocation_instruction_bits(kind, target, place, mask):
    from pcc.backend.elf_aarch64_relocations import apply_relocation
    initial = {283: 0x94000000, 275: 0x90000000, 277: 0x91000000}[kind]
    payload = bytearray(struct.pack("<I", initial))
    apply_relocation(payload, 0, kind, target, place)
    assert struct.unpack("<I", payload)[0] == mask


def test_pe_imports_are_names_and_loader_thunks_not_host_libraries():
    from pcc.backend.coff_x86_64 import assemble, emit_object, parse_object
    from pcc.backend.pe_x86_64 import link_executable
    text = '''target triple = "x86_64-pc-windows-msvc"
declare void @ExitProcess(i32)
define void @pcc_windows_start() {
entry:
  call void @ExitProcess(i32 23)
  ret void
}
'''
    obj = parse_object(emit_object(assemble(emit_self_asm(text))))
    image = link_executable([obj])
    assert image[:2] == b"MZ"
    pe = struct.unpack_from("<I", image, 60)[0]
    assert image[pe:pe + 4] == b"PE\0\0"
    assert struct.unpack_from("<H", image, pe + 4)[0] == 0x8664
    assert b"KERNEL32.dll\0" in image
    assert b"ExitProcess\0" in image
    assert b"msvcrt" not in image.lower()
    assert b"libpython" not in image.lower()
    optional = pe + 24
    assert struct.unpack_from("<II", image, optional + 112 + 8)[0] != 0
    assert struct.unpack_from("<II", image, optional + 112 + 3 * 8)[1] >= 12


def test_win64_mixed_arguments_use_shared_positions_and_home_space():
    text = '''target triple = "x86_64-pc-windows-msvc"
declare i64 @external(i64, double, i64, double, i64)
define i64 @caller(i64 %a, double %b, i64 %c, double %d, i64 %e) {
entry:
  %r = call i64 @external(i64 %a, double %b, i64 %c, double %d, i64 %e)
  ret i64 %r
}
'''
    assembly = emit_self_asm(text)
    assert "movsd xmm1" in assembly and "movsd xmm3" in assembly
    assert "mov rcx, QWORD PTR [rsp]" in assembly or "mov rcx, QWORD PTR [rsp + 0]" in assembly
    assert "mov r8, QWORD PTR [rsp + 16]" in assembly
    assert "QWORD PTR [rsp + 32]" in assembly
    assert "call external" in assembly


def test_win64_large_frame_uses_owned_probe_and_unwind():
    from pcc.backend.coff_x86_64 import assemble
    text = '''target triple = "x86_64-pc-windows-msvc"
define void @large() {
entry:
  %p = alloca [8192 x i8], align 16
  store i8 42, ptr %p
  ret void
}
'''
    assembly = emit_self_asm(text)
    assert "call __pcc_chkstk" in assembly
    obj = assemble(assembly)
    assert any(s.name == ".xdata" and s.data for s in obj.sections)


def test_mixed_elf_architectures_are_rejected():
    from pcc.backend.elf_x86_64 import ElfError, link_static_executable
    from pcc.backend.owned_elf_link import assemble
    a = assemble('.section __TEXT,__text,regular,pure_instructions\n.globl _start\n_start:\n  ret\n', TARGETS[0][0])
    b = assemble('.intel_syntax noprefix\n.text\n.globl other\nother:\n  ret\n', TARGETS[1][0])
    with pytest.raises(ElfError, match="mix ELF machines"):
        link_static_executable([a, b])
