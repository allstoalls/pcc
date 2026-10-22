"""Win64 fixed scalar calls omit stores, not the ABI-owned home space.

Host checks cover selector instructions, owned COFF bytes, frames and roots. The
separate integration test executes a home-writing callee and managed callback
on native Windows with an explicitly selected source-matching runtime.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import sys
from types import SimpleNamespace

import pytest

from pcc.backend import BackendUnavailable
from pcc.backend import self_backend_win64_abi as win64
from pcc.backend import self_backend_x86_64_linux as emitter
from pcc.backend.self_backend_ir import AllocaInfo, SlotInfo, TypeDesc
from pcc.backend.self_backend_kernel import get_indexed_function_kernel
from pcc.backend.self_backend_prepare import prepare_module_for_target
from tests.python.owned_regression_support import explicit_owned_runtime
from tests.python.process_timeout import run_process_group_timeout


# Checks the serialized precise stack-map table, which objects carry only on request.
pytestmark = pytest.mark.usefixtures("precise_stackmap_tables")


TARGET = "x86_64-pc-windows-msvc"
I64, PTR, VOID = TypeDesc("int", 64), TypeDesc("ptr"), TypeDesc("void")
F64 = TypeDesc("fp", 64)
PAIR = TypeDesc("struct", fields=(I64, I64))
SMALL = TypeDesc("struct", fields=(I64,))


@pytest.fixture(autouse=True)
def _win64_context(monkeypatch):
    monkeypatch.setattr(emitter, "_WINDOWS_ABI", True)
    monkeypatch.setattr(emitter, "_TLS_GLOBALS", {})
    monkeypatch.setattr(emitter, "_MODULE_SYMBOLS", type(emitter._MODULE_SYMBOLS)(
        internal_prefix="", defined_symbols=frozenset(),
        internal_symbols=frozenset(), thread_local_symbols=frozenset(),
    ))
    monkeypatch.delenv("PCC_CODE_PROFILE", raising=False)
    monkeypatch.setenv("PCC_SELF_TARGET_PASSES", "none")
    monkeypatch.setenv("PCC_SELF_TARGET_PASS_TRANSPORT", "text")


def _frame():
    return SimpleNamespace(
        name="probe", frame_size=256, platform_frame_extra=240,
        alloca_slots={"root": AllocaInfo(24, PTR)},
        value_slots={
            "r9": SlotInfo(40, I64), "rcx": SlotInfo(48, I64),
            "r8": SlotInfo(56, I64), "rdx": SlotInfo(64, I64),
            "p": SlotInfo(72, PTR), "fn": SlotInfo(80, PTR),
            "fp": SlotInfo(88, F64), "pair": SlotInfo(112, PAIR),
            "small": SlotInfo(120, SMALL), "out": SlotInfo(144, PAIR),
        },
    )


def _call(function, args, *, result=VOID, dest=None, callee="callee",
          indirect=False, vararg=False):
    return win64.call(function, dest, result, callee, indirect, args, vararg)


# Independent ISA byte oracles, in RCX/RDX/R8/R9 order. Loads use REX.R
# for r8/r9; opcode-register immediates use REX.B. i1 has byte storage.
# Byte/word writes leave upper register bits unchanged; dword writes clear
# the upper 32 bits. The Win64 argument contract only consumes the low bits.
_LOCAL_LOAD_BYTES = {
    1: ("8a4dd8", "8a55d8", "448a45d8", "448a4dd8"),
    8: ("8a4dd8", "8a55d8", "448a45d8", "448a4dd8"),
    16: ("668b4dd8", "668b55d8", "66448b45d8", "66448b4dd8"),
    32: ("8b4dd8", "8b55d8", "448b45d8", "448b4dd8"),
    64: ("488b4dd8", "488b55d8", "4c8b45d8", "4c8b4dd8"),
}
_RETURN_SPILL_BYTES = {
    1: "888570ffffff", 8: "888570ffffff", 16: "66898570ffffff",
    32: "898570ffffff", 64: "48898570ffffff",
}
_IMMEDIATE_HEADS = {
    1: ("b1", "b2", "41b0", "41b1"),
    8: ("b1", "b2", "41b0", "41b1"),
    16: ("66b9", "66ba", "6641b8", "6641b9"),
    32: ("b9", "ba", "41b8", "41b9"),
    64: ("48b9", "48ba", "49b8", "49b9"),
}
_SIGNED_I64_IMMEDIATE_HEADS = ("48c7c1", "48c7c2", "49c7c0", "49c7c1")
# Literal little-endian payloads make the sign-extension boundary explicit:
# +0xffffffff needs imm64, while -1 may use sign-extended imm32.
_INTEGER_IMMEDIATE_BYTES = {
    1: (("0", "00"), ("1", "01")),
    8: (("0", "00"), ("-1", "ff"), ("127", "7f"), ("128", "80"), ("255", "ff")),
    16: (("0", "0000"), ("-1", "ffff"), ("4660", "3412"),
         ("32768", "0080"), ("65535", "ffff")),
    32: (("0", "00000000"), ("-1", "ffffffff"), ("305419896", "78563412"),
         ("2147483648", "00000080"), ("4294967295", "ffffffff")),
    64: (("0", "00000000"), ("-1", "ffffffff"),
         ("-2147483648", "00000080"), ("2147483647", "ffffff7f"),
         ("2147483648", "0000008000000000"), ("-2147483649", "ffffff7fffffffff"),
         ("4294967295", "ffffffff00000000"),
         ("81985529216486895", "efcdab8967452301"),
         ("9223372036854775807", "ffffffffffffff7f"),
         ("-9223372036854775808", "0000000000000080"),
         ("18446744073709551615", "ffffffffffffffff")),
}
_FIXED_I64_SETUP_BYTES = (
    "48c7c101000000", "48c7c2feffffff", "49c7c003000000", "49c7c104000000",
)


def _assert_owned_coff_call_bytes(lines, setup_hex, spill_hex):
    from pcc.backend import coff_x86_64 as coff

    # These are selector fragments, not runnable functions or native ABI
    # qualification. A nonzero prefix also exercises the call relocation PC.
    assembly = (
        ".intel_syntax noprefix\n.text\n.globl probe\n.type probe, @function\n"
        "probe:\n.byte 144, 144, 144\n" + "\n".join(lines)
        + "\n  ret\n.size probe, .-probe\n"
    )
    raw = coff.assemble_object(assembly)
    obj = coff.parse_object(raw)
    assert not any(item.name == ".rdata$got" for item in obj.sections)
    section = next(item for item in obj.sections if item.name == ".text")
    setup, spill = bytes.fromhex("".join(setup_hex)), bytes.fromhex(spill_hex)
    assert section.data == b"\x90" * 3 + setup + b"\xe8\0\0\0\0" + spill + b"\xc3"
    assert len(section.relocations) == 1
    relocation = section.relocations[0]
    symbol = obj.symbols[relocation.symbol]
    assert (relocation.offset, relocation.kind, symbol.name, symbol.section, symbol.value) == (
        3 + len(setup) + 1, 0x0004, "callee", 0, 0,
    )
    assert symbol.external
    assert coff.emit_object(obj) == raw


@pytest.mark.parametrize("position", range(4))
@pytest.mark.parametrize("width,memory,aliases", (
    (1, "BYTE", ("cl", "dl", "r8b", "r9b")),
    (8, "BYTE", ("cl", "dl", "r8b", "r9b")),
    (16, "WORD", ("cx", "dx", "r8w", "r9w")),
    (32, "DWORD", ("ecx", "edx", "r8d", "r9d")),
    (64, "QWORD", ("rcx", "rdx", "r8", "r9")),
))
@pytest.mark.parametrize("result", (VOID, PTR, I64), ids=("void", "pointer", "integer"))
def test_integer_width_position_matrix_is_destination_local(position, width, memory, aliases, result):
    function = _frame()
    narrow = TypeDesc("int", width)
    function.value_slots["r9"] = SlotInfo(40, narrow)
    function.value_slots["out"] = SlotInfo(144, result)
    args = [(I64, "1"), (I64, "-2"), (I64, "3"), (I64, "4")]
    args[position] = (narrow, "r9")
    lines = _call(function, args, result=result, dest="out")
    expected = ["  mov rcx, 1", "  mov rdx, -2", "  mov r8, 3", "  mov r9, 4"]
    expected[position] = f"  mov {aliases[position]}, {memory} PTR [rbp - 40]"
    expected.append("  call callee")
    if not result.is_void:
        expected.append("  mov QWORD PTR [rbp - 144], rax")
    assert lines == expected
    assert not any("[rsp" in line or "r10" in line or "r11" in line for line in lines)
    setup = list(_FIXED_I64_SETUP_BYTES)
    setup[position] = _LOCAL_LOAD_BYTES[width][position]
    _assert_owned_coff_call_bytes(lines, setup, "" if result.is_void else _RETURN_SPILL_BYTES[64])

    if result.is_int:
        # Keep the original scalar-return shape coverage, then cover each
        # accepted narrow return and its exact AL/AX/EAX/RAX memory store.
        function.value_slots["out"] = SlotInfo(144, narrow)
        lines = _call(function, args, result=narrow, dest="out")
        _assert_owned_coff_call_bytes(lines, setup, _RETURN_SPILL_BYTES[width])
        for literal, payload in _INTEGER_IMMEDIATE_BYTES[width]:
            immediate_args = list(args)
            immediate_args[position] = (narrow, literal)
            immediate_setup = list(_FIXED_I64_SETUP_BYTES)
            heads = (_SIGNED_I64_IMMEDIATE_HEADS if width == 64 and len(payload) == 8
                     else _IMMEDIATE_HEADS[width])
            immediate_setup[position] = heads[position] + payload
            lines = _call(function, immediate_args, result=narrow, dest="out")
            _assert_owned_coff_call_bytes(lines, immediate_setup, _RETURN_SPILL_BYTES[width])


@pytest.mark.parametrize("count", (1, 2, 3, 4))
def test_reversed_and_repeated_local_sources_do_not_need_parallel_copies(count):
    function = _frame()
    sources = ("r9", "rcx", "r9", "rdx")[:count]
    lines = _call(function, [(I64, value) for value in sources])
    offsets = (40, 48, 40, 64)
    assert lines == [
        f"  mov {win64.GPRS[index]}, QWORD PTR [rbp - {offsets[index]}]"
        for index in range(count)
    ] + ["  call callee"]


def test_pointer_local_alloca_null_and_high_integer_are_destination_local():
    assert _call(_frame(), [(PTR, "p"), (PTR, "root"), (PTR, "null"),
                            (I64, "9223372036854775807")]) == [
        "  mov rcx, QWORD PTR [rbp - 72]", "  lea rdx, [rbp - 24]",
        "  xor r8, r8", "  mov r9, 9223372036854775807", "  call callee",
    ]


@pytest.mark.parametrize("position", range(4))
def test_pointer_value_in_each_argument_position(position):
    args = [(PTR, "null")] * 4
    args[position] = (PTR, "p")
    expected = [f"  xor {reg}, {reg}" for reg in win64.GPRS]
    expected[position] = f"  mov {win64.GPRS[position]}, QWORD PTR [rbp - 72]"
    assert _call(_frame(), args) == expected + ["  call callee"]


@pytest.mark.parametrize("args,kwargs", (
    ([], {}), ([(I64, "1")] * 5, {}),
    ([(I64, "r9")], {"indirect": True, "callee": "fn"}),
    ([(I64, "r9")], {"vararg": True}),
    ([(F64, "fp")], {}), ([(F64, "fp")], {"vararg": True}),
    ([(I64, "r9")], {"result": F64, "dest": "fp"}),
    ([(SMALL, "small")], {}), ([(PAIR, "pair")], {}),
    ([(I64, "1")], {"result": PAIR, "dest": "out"}),
    ([(PTR, "@payload")], {}), ([(PTR, "inttoptrconst:64")], {}),
    ([(PTR, "gepconst:payload:8")], {}),
    ([(PTR, "poison")], {}), ([(PTR, "undef")], {}),
    ([(I64, "1")], {"callee": "llvm.example"}),
))
def test_complex_calls_keep_the_existing_staging_path(monkeypatch, args, kwargs):
    function = _frame()
    actual = _call(function, args, **kwargs)
    assert any("[rsp" in line for line in actual) or not args
    monkeypatch.setattr(win64, "_direct_scalar_call_eligible", lambda *_args: False)
    assert _call(function, args, **kwargs) == actual


def test_tls_address_keeps_the_existing_staging_path(monkeypatch):
    from pcc.backend.self_backend_parse import parse_self_backend_module

    module = parse_self_backend_module(
        f'target triple = "{TARGET}"\n@tls_payload = thread_local global i64 7\n',
    )
    monkeypatch.setattr(emitter, "_TLS_GLOBALS", {"tls_payload": module.globals_[0]})
    args = [(PTR, "p"), (PTR, "@tls_payload")]
    actual = _call(_frame(), args)
    assert any("[rsp" in line for line in actual)
    monkeypatch.setattr(win64, "_direct_scalar_call_eligible", lambda *_args: False)
    assert _call(_frame(), args) == actual


@pytest.mark.parametrize("value", (
    "poison", "undef", "gep0:payload", "gepconst:payload:8",
    "getelementptr (i8, ptr @payload, i64 1)", "inttoptrconst:64", "@payload",
))
def test_special_spelling_cannot_enter_via_slot_membership(value):
    function = _frame()
    function.value_slots[value] = SlotInfo(160, PTR)
    function.alloca_slots[value] = AllocaInfo(168, PTR)
    assert not win64._direct_scalar_call_eligible(
        function, None, VOID, "callee", False, [(PTR, value)], False,
    )


@pytest.mark.parametrize("width", (2, 7, 24, 48, 65, 128))
def test_unlisted_integer_widths_fail_closed_in_eligibility(width):
    function = _frame()
    ty = TypeDesc("int", width)
    assert not win64._direct_scalar_call_eligible(
        function, None, VOID, "callee", False, [(ty, "1")], False,
    )
    assert not win64._direct_scalar_call_eligible(
        function, "out", ty, "callee", False, [(I64, "1")], False,
    )


def test_integer_typed_alloca_and_unknown_operand_keep_failure_behavior(monkeypatch):
    for value in ("root", "missing"):
        function = _frame()
        assert not win64._direct_scalar_call_eligible(
            function, None, VOID, "callee", False, [(I64, value)], False,
        )
        with pytest.raises(BackendUnavailable) as actual:
            _call(function, [(I64, value)])
        with monkeypatch.context() as control:
            control.setattr(win64, "_direct_scalar_call_eligible", lambda *_args: False)
            with pytest.raises(BackendUnavailable) as expected:
                _call(function, [(I64, value)])
        assert str(actual.value) == str(expected.value)


@pytest.mark.parametrize("dest", (None, "missing"))
def test_hidden_result_storage_is_still_required(dest):
    with pytest.raises(BackendUnavailable, match="aggregate call needs result storage"):
        _call(_frame(), [(I64, "1")], result=PAIR, dest=dest)


def test_fixed_outgoing_capacity_check_precedes_direct_setup():
    function = _frame()
    function.platform_frame_extra = 96
    with pytest.raises(BackendUnavailable, match="exceed the planned fixed frame"):
        _call(function, [(I64, "1")])


def _prepared(source):
    return prepare_module_for_target(
        source, aggregate_returned_indirect=win64.indirect,
    ).functions[0]


@pytest.mark.parametrize("attribute", (
    "byval(i64)", "sret(i64)", "byref(i64)", "inalloca(i64)", "preallocated(i64)",
))
def test_attributed_pointers_preserve_the_current_parsed_boundary(attribute):
    # These attributes are currently discarded by the parser. This is not a
    # claim that the selector excludes attributed calls or implements byval.
    function = _prepared(f'''target triple = "{TARGET}"
declare void @callee(ptr)
define void @probe(ptr %p) {{
entry:
  call void @callee(ptr {attribute} %p)
  ret void
}}
''')
    win64.prepare_frame(function)
    kernel = get_indexed_function_kernel(function)
    data = kernel.diagnostic_call_data(0)
    dest, result, callee, indirect, args, _tail, vararg, _alignments = data
    assert len(args) == 1 and args[0][0].is_ptr and args[0][1] == "p"
    assert win64._direct_scalar_call_eligible(function, dest, result, callee, indirect, args, vararg)
    assert win64.call(function, dest, result, callee, indirect, args, vararg) == [
        f"  mov rcx, QWORD PTR [rbp - {function.value_slots['p'].offset}]",
        "  call callee",
    ]


def test_home_reservation_alignment_and_unwind_remain_fixed(monkeypatch):
    source = f'''target triple = "{TARGET}"
declare void @callee(i64, i64, i64, i64)
define void @probe() {{
entry:
  call void @callee(i64 1, i64 2, i64 3, i64 4)
  ret void
}}
'''
    function = _prepared(source)
    win64.prepare_frame(function)
    assert win64.outgoing_size(function) == 32
    assert win64.frame_size(function) % 16 == 0
    prologue, epilogue = win64.prologue(function), win64.epilogue(function)
    assert f"  .seh_stackalloc {win64.frame_size(function)}" in prologue
    monkeypatch.setattr(win64, "_direct_scalar_call_eligible", lambda *_args: False)
    win64.prepare_frame(function)
    assert win64.outgoing_size(function) == 32
    assert win64.prologue(function) == prologue
    assert win64.epilogue(function) == epilogue


def test_precise_root_remains_local_across_a_direct_scalar_call(monkeypatch):
    from pcc.backend import target_objects
    from pcc.backend.coff_x86_64 import parse_object
    from pcc.backend.owned_object_emit import emit_owned_object
    from pcc.backend.precise_stackmap import decode_stack_map

    source = f'''target triple = "{TARGET}"
@frame_map = internal constant i32 1
declare void @pcc_gc_frame_enter(ptr, ptr)
declare void @pcc_gc_frame_leave(ptr)
declare i64 @callee(ptr, i64, i64, i64)
define ptr @probe(ptr %obj) {{
entry:
  %root = alloca ptr, align 8
  store ptr %obj, ptr %root
  call void @pcc_gc_frame_enter(ptr @frame_map, ptr %root)
  %borrow = load ptr, ptr %root
  %result = call i64 @callee(ptr %borrow, i64 11, i64 22, i64 33)
  %after = load ptr, ptr %root
  call void @pcc_gc_frame_leave(ptr %root)
  ret ptr %after
}}
'''
    function = _prepared(source)
    win64.prepare_frame(function)
    root_offset = -function.alloca_slots["root"].offset
    assemblies = []
    real_encode = target_objects.encode_assembly_object

    def capture(assembly, *args, **kwargs):
        assemblies.append(assembly)
        return real_encode(assembly, *args, **kwargs)

    monkeypatch.setattr(target_objects, "encode_assembly_object", capture)
    obj = parse_object(emit_owned_object(source, TARGET))
    assert len(assemblies) == 1
    lines = assemblies[0].splitlines()
    index = lines.index("  call callee")
    instructions = [line for line in lines[:index] if line.startswith("  ")]
    setup = instructions[-4:]
    # rcx receives %borrow: reloaded from its slot, or copied from the
    # register the previous instruction stored to the value's home (its slot
    # or the callee-saved register x86_64_frame_registers keeps it in) or
    # loaded from %root, when nothing reads the home and its store is dead.
    borrow_slot = f"QWORD PTR [rbp - {function.value_slots['borrow'].offset}]"
    homes = (borrow_slot, "rbx", "rsi", "rdi", "r12", "r13", "r14", "r15")
    stored = instructions[-5]
    source = setup[0].rsplit(", ", 1)[1]
    assert setup[0] == f"  mov rcx, {borrow_slot}" or (
        setup[0] == "  mov rcx, " + source
        and (any(stored == f"  mov {home}, {source}" for home in homes)
             or stored == f"  mov {source}, QWORD PTR [rbp - {-root_offset}]")
    ), instructions[-5:]
    assert setup[1:] == ["  mov rdx, 11", "  mov r8, 22", "  mov r9, 33"]
    maps = decode_stack_map(next(s.data for s in obj.sections if s.name == ".pcc_stackmaps"))
    roots = [location for entry in maps.functions for record in entry.records for location in record.locations]
    assert roots and any(location.offset == root_offset for location in roots)
    assert all(location.register == 6 and -function.frame_size <= location.offset < 0
               for location in roots)
    outgoing_top = -win64.frame_size(function) + win64.outgoing_size(function)
    assert all(location.offset >= -function.frame_size > outgoing_top for location in roots)
    assert f"  mov r10, QWORD PTR [rbp - {-root_offset}]" in lines[index + 1:]
    assert any(section.name == ".xdata" and section.data for section in obj.sections)


# Independent ABI oracle. Incoming homes are writable by the callee at
# rsp+8..32. A 40-byte subtraction aligns a nested call and reserves its homes.
HOME_CALLBACK_ASM = '''.intel_syntax noprefix
.text
.globl pcc_win64_home_probe
.seh_proc pcc_win64_home_probe
pcc_win64_home_probe:
  mov QWORD PTR [rsp + 8], rcx
  mov QWORD PTR [rsp + 16], rdx
  mov QWORD PTR [rsp + 24], r8
  mov QWORD PTR [rsp + 32], r9
  sub rsp, 40
  .seh_stackalloc 40
  .seh_endprologue
  mov rax, QWORD PTR [rsp + 48]
  mov rcx, QWORD PTR [rsp + 56]
  mov rdx, QWORD PTR [rsp + 64]
  mov r8, QWORD PTR [rsp + 72]
  mov r9, 44
  call rax
  mov r10, QWORD PTR [rsp + 56]
  add rax, r10
  mov r10, QWORD PTR [rsp + 64]
  add rax, r10
  mov r10, QWORD PTR [rsp + 72]
  add rax, r10
  add rsp, 40
  ret
.seh_endproc
'''

MANAGED_CALLBACK_SOURCE = '''
import gc
import os
from pcc import i64
from pcc.extern import c_abi_typed_export, c_int64, c_ptr, extern
from pcc.unsafe import function_addr

probe = extern("pcc_win64_home_probe", (c_ptr, c_int64, c_int64, c_int64), c_int64)
collector = extern("pcc_gc_backend", (), c_int64)
live = []

@c_abi_typed_export("pcc_win64_managed_callback", "i64", ("i64", "i64", "i64", "i64"))
def callback(a: i64, b: i64, c: i64, d: i64) -> i64:
    assert a == 11 and b == 22 and c == 33 and d == 44
    payload = live[0]
    temporary = [payload[1]["answer"] + 1, "callback allocation"]
    gc.collect()
    assert temporary == [42, "callback allocation"]
    assert payload[0] == "caller root" and payload[1]["answer"] == 41
    return a + 3 * b + 5 * c + 7 * d

def main():
    assert collector() == int(os.environ["PCC_GC_BACKEND"])
    caller_only = [{"answer": 73}, "caller stack only"]
    payload = ["caller root", {"answer": 41}]
    live.append(payload)
    result = probe(function_addr("pcc_win64_managed_callback"), 11, 22, 33)
    assert result == 616
    assert payload is live[0]
    assert payload[1]["answer"] == 41
    assert caller_only == [{"answer": 73}, "caller stack only"]
    gc.collect()
    assert payload[0] == "caller root"
    assert caller_only[0]["answer"] == 73 and caller_only[1] == "caller stack only"
    print("WIN64_HOME_ROOT_CALLBACK_OK")

main()
'''


def test_native_fixture_compiles_and_encodes_without_a_runtime(tmp_path):
    from pcc.backend.coff_x86_64 import parse_object
    from pcc.backend.self_backend_prepare import prepare_parsed_module_for_target
    from pcc.backend.target_objects import encode_assembly_object
    from pcc.frontends.python.pipeline import compile_python
    from tests.owned_ir_validation import verify_ir_text

    source, output = tmp_path / "win64_home_callback.py", tmp_path / "win64_home_callback.ll"
    source.write_text(MANAGED_CALLBACK_SOURCE)
    compile_python(
        str(source), str(output), emit_llvm_only=True, backend="self",
        libpython_mode="off", ir_scaffold_mode="on", target_triple=TARGET,
    )
    ir = output.read_text()
    module = verify_ir_text(ir)
    assert "@pcc_win64_home_probe(" in ir
    assert "@pcc_win64_managed_callback(" in ir
    assert "@pcc_gc_store_root(" in ir and "@pcc_gc_load_ptr(" in ir
    prepared = prepare_parsed_module_for_target(
        module, aggregate_returned_indirect=win64.indirect,
    )
    probes = []
    for function in prepared.functions:
        kernel = get_indexed_function_kernel(function)
        for call_id in range(len(kernel.call_scalars) // 8):
            dest, result, callee, indirect, args, _tail, vararg, _align = kernel.diagnostic_call_data(call_id)
            if callee == "pcc_win64_home_probe":
                assert win64._direct_scalar_call_eligible(
                    function, dest, result, callee, indirect, args, vararg,
                )
                assert args[0][1] in function.value_slots
                win64.prepare_frame(function)
                probes.append(win64.call(function, dest, result, callee, indirect, args, vararg))
    assert len(probes) == 1
    assert "  call pcc_win64_home_probe" in probes[0]
    assert not any("[rsp" in line for line in probes[0])
    obj = parse_object(encode_assembly_object(HOME_CALLBACK_ASM, TARGET))
    assert any(symbol.name == "pcc_win64_home_probe" and symbol.section for symbol in obj.symbols)
    assert any(section.name == ".xdata" and section.data for section in obj.sections)


@pytest.mark.integration
@pytest.mark.pcc_gate(probe=lambda: sys.platform == "win32" and platform.machine().lower() in ("amd64", "x86_64"))
def test_native_win64_home_space_and_rooted_callback_five_collectors(tmp_path, explicit_owned_runtime, capfd):
    from pcc.backend.target_objects import encode_assembly_object
    from pcc.frontends.python.pipeline import compile_python

    def digest(path):
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    source, helper, executable = (tmp_path / name for name in (
        "win64_home_callback.py", "win64_home_callback.obj", "win64_home_callback.exe",
    ))
    source.write_text(MANAGED_CALLBACK_SOURCE)
    helper.write_bytes(encode_assembly_object(HOME_CALLBACK_ASM, TARGET))
    receipt_path = tmp_path / "win64-home-callback.json"
    assert not receipt_path.exists(), "refuse a stale Win64 callback receipt"
    record = {
        "schema": "pcc.win64-home-callback.v1", "status": "RUNNING",
        "run_id": os.environ.get("PCC_NATIVE_OBJECT_GATE_RUN_ID", ""),
        "compiler_mode": "host-pcc0", "backend": "self", "libpython": "off",
        "target": TARGET, "source_sha256": digest(source),
        "helper_assembly_sha256": hashlib.sha256(HOME_CALLBACK_ASM.encode()).hexdigest(),
        "helper_sha256": digest(helper),
        "runtime_archive": str(explicit_owned_runtime),
        "runtime_sha256": digest(explicit_owned_runtime),
        "binary_path": str(executable), "executions": [],
    }

    def save():
        receipt_path.write_text(json.dumps(record, indent=2) + "\n")

    save()
    try:
        try:
            compile_python(
                str(source), str(executable), backend="self", libpython_mode="off",
                ir_scaffold_mode="on", runtime_archive=str(explicit_owned_runtime),
                object_inputs=(str(helper),), target_triple=TARGET,
            )
        finally:
            captured = capfd.readouterr()
            (tmp_path / "compile.stdout").write_text(captured.out)
            (tmp_path / "compile.stderr").write_text(captured.err)
        record["binary_sha256"] = digest(executable)
        for backend in range(5):
            command = [str(executable)]
            result = run_process_group_timeout(
                command, env=dict(os.environ, PCC_GC_BACKEND=str(backend)), timeout=10,
            )
            record["executions"].append({
                "requested_gc_backend": backend, "command": command,
                "returncode": result.returncode, "stdout": result.stdout, "stderr": result.stderr,
            })
            save()
            assert (result.returncode, result.stdout, result.stderr) == (
                0, "WIN64_HOME_ROOT_CALLBACK_OK\n", "",
            ), record["executions"][-1]
        assert digest(source) == record["source_sha256"]
        assert digest(helper) == record["helper_sha256"]
        assert digest(executable) == record["binary_sha256"]
        assert digest(explicit_owned_runtime) == record["runtime_sha256"]
        record["status"] = "PASS"
    except BaseException as error:
        record.update(status="FAIL", error=type(error).__name__ + ": " + str(error))
        raise
    finally:
        save()
