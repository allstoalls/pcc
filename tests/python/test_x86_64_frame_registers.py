"""x86-64 keeps scalar frame slots in callee-saved registers.

The instruction selector stores every SSA value to its frame slot and reloads
it at each use. ``keep_frame_slots_in_registers`` rewrites a finished function
so a slot that only plain ``mov``s touch, whose address is never taken and
that no stack map lists, lives in rbx/r12-r15 (plus rsi/rdi under Win64)
instead; the prologue saves and each epilogue restores the r12-r15 it uses.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from pcc.backend.self_backend_ir import SlotInfo, TypeDesc
from pcc.backend.self_backend_precise_stackmaps import (
    PackedPlannedSafepoints,
    PlannedManagedReload,
    PlannedRootLocation,
    PlannedSafepoint,
)
from pcc.backend.x86_64_frame_registers import keep_frame_slots_in_registers

I64 = TypeDesc("int", 64)
I8 = TypeDesc("int", 8)
PTR = TypeDesc("ptr", pointee=TypeDesc("void"))
F64 = TypeDesc("fp", 64)
PROLOGUE = ["", ".text", ".p2align 4, 0x90", ".globl f", ".type f, @function", "f:",
            "  push rbp", "  mov rbp, rsp", "  sub rsp, 80", "  mov QWORD PTR [rsp + 0], rbx"]
EPILOGUE = ["  mov rbx, QWORD PTR [rsp + 0]", "  add rsp, 80", "  pop rbp", "  ret"]


def _func(*slots, windows=False):
    value_slots = {f"%v{index}": SlotInfo(offset, ty) for index, (offset, ty) in enumerate(slots)}
    return SimpleNamespace(value_slots=value_slots, is_vararg=False,
                           platform_frame_extra=(32 + 112 if windows else 48))


def _plan(*root_offsets):
    locations = tuple(PlannedRootLocation(-offset, True) for offset in root_offsets)
    record = PlannedSafepoint(1, ".Lsmap_1", 0, locations)
    return SimpleNamespace(packed_records=None, records=(record,))


def _promote(body, *slots, plan=None, windows=False):
    lines = PROLOGUE + body + EPILOGUE
    return keep_frame_slots_in_registers(
        lines, _func(*slots), plan if plan is not None else _plan(), windows=windows,
    )


def test_values_across_calls_live_in_callee_saved_registers():
    body = [
        "  mov QWORD PTR [rbp - 8], rdi",
        "  call g",
        ".Lsmap_1:",
        "  mov QWORD PTR [rbp - 16], rax",
        "  mov rdi, QWORD PTR [rbp - 8]",
        "  mov rsi, QWORD PTR [rbp - 16]",
        "  call h",
        "  mov rax, QWORD PTR [rbp - 16]",
    ]
    out = _promote(body, (8, I64), (16, PTR))
    # The heavier slot (three accesses) takes rbx, which every frame already
    # saves; the slot live across its store takes r12, saved at
    # platform_frame_extra - 32 and restored before each return.
    assert out == PROLOGUE + ["  mov QWORD PTR [rsp + 16], r12"] + [
        "  mov r12, rdi",
        "  call g",
        ".Lsmap_1:",
        "  mov rbx, rax",
        "  mov rdi, r12",
        "  mov rsi, rbx",
        "  call h",
        "  mov rax, rbx",
    ] + EPILOGUE[:1] + ["  mov r12, QWORD PTR [rsp + 16]"] + EPILOGUE[1:]


def test_disjoint_values_share_one_register_and_widths_keep_their_alias():
    body = [
        "  mov BYTE PTR [rbp - 1], al",
        "  mov r10b, BYTE PTR [rbp - 1]",
        "  mov DWORD PTR [rbp - 12], -7",
        "  mov r10d, DWORD PTR [rbp - 12]",
        "  mov QWORD PTR [rbp - 24], 5",
        "  mov rax, QWORD PTR [rbp - 24]",
    ]
    out = _promote(body, (1, I8), (12, TypeDesc("int", 32)), (24, I64))
    assert out == PROLOGUE + [
        "  mov bl, al", "  mov r10b, bl",
        "  mov ebx, -7", "  mov r10d, ebx",
        "  mov rbx, 5", "  mov rax, rbx",
    ] + EPILOGUE


@pytest.mark.parametrize("body, slots, plan", [
    # The address escapes, so a pointer may read or write the slot.
    (["  mov QWORD PTR [rbp - 8], rax", "  lea rdi, [rbp - 8]", "  call g",
      "  mov rax, QWORD PTR [rbp - 8]"], ((8, I64),), None),
    # An address inside the slot escapes.
    (["  mov QWORD PTR [rbp - 8], rax", "  lea rdi, [rbp - 4]", "  call g",
      "  mov rax, QWORD PTR [rbp - 8]"], ((8, I64),), None),
    # A stack map lists it: the collector reads and relocates it in memory.
    (["  mov QWORD PTR [rbp - 8], rax", "  call g", "  mov rax, QWORD PTR [rbp - 8]"],
     ((8, PTR),), _plan(8)),
    # Two widths touch it.
    (["  mov QWORD PTR [rbp - 8], rax", "  mov eax, DWORD PTR [rbp - 8]"], ((8, I64),), None),
    # Something other than a general mov reads it.
    (["  mov QWORD PTR [rbp - 8], rax", "  cmp QWORD PTR [rbp - 8], 0"], ((8, I64),), None),
    (["  movsd QWORD PTR [rbp - 8], xmm0", "  movsd xmm1, QWORD PTR [rbp - 8]"], ((8, F64),), None),
    # It is read before any store on some path into the function.
    (["  mov rax, QWORD PTR [rbp - 8]", "  mov QWORD PTR [rbp - 8], rdi",
      "  mov rdx, QWORD PTR [rbp - 8]"], ((8, I64),), None),
    # A wider access elsewhere covers its bytes.
    (["  mov QWORD PTR [rbp - 24], rax", "  movdqu XMMWORD PTR [rbp - 32], xmm0",
      "  mov rax, QWORD PTR [rbp - 24]"], ((24, I64),), None),
    # It is not a scalar SSA value slot.
    (["  mov QWORD PTR [rbp - 8], rax", "  mov rax, QWORD PTR [rbp - 8]"], (), None),
])
def test_slots_that_stay_in_memory(body, slots, plan):
    assert _promote(body, *slots, plan=plan) == PROLOGUE + body + EPILOGUE


def test_stores_nothing_reads_are_dropped_in_registers_and_in_memory():
    body = [
        "  mov QWORD PTR [rbp - 8], rax",
        "  mov QWORD PTR [rbp - 8], rdi",
        "  mov rax, QWORD PTR [rbp - 8]",
        # Read before its stores, so it stays in memory; the first of its
        # two stores is still dead.
        "  mov rcx, QWORD PTR [rbp - 16]",
        "  mov QWORD PTR [rbp - 16], rdi",
        "  mov QWORD PTR [rbp - 16], rsi",
        "  mov rdx, QWORD PTR [rbp - 16]",
        "  mov BYTE PTR [rbp - 17], al",
    ]
    out = _promote(body, (8, I64), (16, I64), (17, I8))
    assert out == PROLOGUE + ["  mov rbx, rdi", "  mov rax, rbx", body[3], body[5], body[6]] + EPILOGUE


def test_packed_stack_map_roots_and_reload_sources_stay_in_memory():
    packed = PackedPlannedSafepoints(1)
    packed.append(1, ".Lsmap_1", 0, (PlannedRootLocation(-8, True),), 0, "", 0,
                  (PlannedManagedReload(-16, -24),))
    plan = SimpleNamespace(packed_records=packed, records=())
    body = [
        "  mov QWORD PTR [rbp - 8], rax", "  mov QWORD PTR [rbp - 16], rax",
        "  call g", ".Lsmap_1:",
        "  mov r11, QWORD PTR [rbp - 16]", "  mov QWORD PTR [rbp - 24], r11",
        "  mov rax, QWORD PTR [rbp - 8]", "  mov rdx, QWORD PTR [rbp - 24]",
    ]
    out = _promote(body, (8, PTR), (16, PTR), (24, PTR), plan=plan)
    # The reload destination is an ordinary value slot.
    assert out == PROLOGUE + body[:5] + ["  mov rbx, r11", body[6], "  mov rdx, rbx"] + EPILOGUE


def test_registers_the_body_names_are_not_used():
    body = [
        "  mov QWORD PTR [rbp - 8], rdi",
        "  mov rbx, rax", "  sar rbx, 63",
        "  call g",
        "  mov rax, QWORD PTR [rbp - 8]",
    ]
    out = _promote(body, (8, I64))
    assert out == PROLOGUE + ["  mov QWORD PTR [rsp + 16], r12", "  mov r12, rdi",
                              *body[1:4], "  mov rax, r12"] + EPILOGUE[:1] + [
        "  mov r12, QWORD PTR [rsp + 16]"] + EPILOGUE[1:]


def test_loop_carried_value_interferes_across_the_back_edge():
    # [rbp - 8] carries the sum around the loop, so it is live where each
    # iteration stores [rbp - 16]; they need two registers.
    body = [
        "  mov QWORD PTR [rbp - 8], 0",
        ".Lf_head:",
        "  call g",
        "  mov QWORD PTR [rbp - 16], rax",
        "  mov r10, QWORD PTR [rbp - 8]",
        "  mov r11, QWORD PTR [rbp - 16]",
        "  add r10, r11",
        "  mov QWORD PTR [rbp - 8], r10",
        "  cmp r10, 100",
        "  jl .Lf_head",
        "  mov rax, QWORD PTR [rbp - 8]",
    ]
    out = _promote(body, (8, I64), (16, I64))
    assert out == PROLOGUE + ["  mov QWORD PTR [rsp + 16], r12"] + [
        "  mov rbx, 0",
        ".Lf_head:",
        "  call g",
        "  mov r12, rax",
        "  mov r10, rbx",
        "  mov r11, r12",
        "  add r10, r11",
        "  mov rbx, r10",
        "  cmp r10, 100",
        "  jl .Lf_head",
        "  mov rax, rbx",
    ] + EPILOGUE[:1] + ["  mov r12, QWORD PTR [rsp + 16]"] + EPILOGUE[1:]


def test_value_dead_between_its_load_and_next_store_shares_its_register():
    # The loop reads [rbp - 8] and stores it again only after [rbp - 16]'s
    # last use, so the two never hold values at the same point.
    body = [
        "  mov QWORD PTR [rbp - 8], 0",
        ".Lf_head:",
        "  mov r10, QWORD PTR [rbp - 8]",
        "  mov QWORD PTR [rbp - 16], r10",
        "  call g",
        "  mov r10, QWORD PTR [rbp - 16]",
        "  add r10, 1",
        "  mov QWORD PTR [rbp - 8], r10",
        "  cmp r10, 10",
        "  jl .Lf_head",
        "  mov rax, QWORD PTR [rbp - 8]",
    ]
    out = _promote(body, (8, I64), (16, I64))
    assert out == PROLOGUE + [line.replace("QWORD PTR [rbp - 8]", "rbx").replace(
        "QWORD PTR [rbp - 16]", "rbx") for line in body] + EPILOGUE


def test_windows_saves_are_described_for_unwinding():
    outgoing = 32
    prologue = ["", ".text", ".p2align 4, 0x90", ".globl f", ".type f, @function", ".seh_proc f", "f:",
                "  push rbp", "  .seh_pushreg rbp", "  mov rbp, rsp", "  sub rsp, 176",
                "  .seh_stackalloc 176",
                f"  mov QWORD PTR [rsp + {outgoing}], rbx", f"  .seh_savereg rbx, {outgoing}",
                f"  mov QWORD PTR [rsp + {outgoing + 8}], rsi", f"  .seh_savereg rsi, {outgoing + 8}",
                f"  mov QWORD PTR [rsp + {outgoing + 16}], rdi", f"  .seh_savereg rdi, {outgoing + 16}",
                "  .seh_endprologue"]
    epilogue = [f"  mov rbx, QWORD PTR [rsp + {outgoing}]", f"  mov rsi, QWORD PTR [rsp + {outgoing + 8}]",
                f"  mov rdi, QWORD PTR [rsp + {outgoing + 16}]", "  add rsp, 176", "  pop rbp", "  ret"]
    # Four values live across the call: rbx, rsi and rdi are already saved;
    # the fourth takes r12, saved at the top of the 112-byte platform area.
    body = [f"  mov QWORD PTR [rbp - {8 * index}], rcx" for index in range(1, 5)]
    body += ["  call g"]
    body += [f"  mov rdx, QWORD PTR [rbp - {8 * index}]" for index in range(1, 5)]
    slots = [(8 * index, I64) for index in range(1, 5)]
    out = keep_frame_slots_in_registers(prologue + body + epilogue, _func(*slots, windows=True),
                                        _plan(), windows=True)
    area = outgoing + 80
    registers = ("rbx", "rsi", "rdi", "r12")
    assert out == prologue[:-1] + [
        f"  mov QWORD PTR [rsp + {area}], r12", f"  .seh_savereg r12, {area}", "  .seh_endprologue",
    ] + [f"  mov {register}, rcx" for register in registers] + ["  call g"] + [
        f"  mov rdx, {register}" for register in registers
    ] + epilogue[:3] + [f"  mov r12, QWORD PTR [rsp + {area}]"] + epilogue[3:]


@pytest.mark.parametrize("body", [
    # Control leaves the function text other than through ret.
    ["  mov QWORD PTR [rbp - 8], rax", "  jmp g", "  mov rax, QWORD PTR [rbp - 8]"],
    ["  mov QWORD PTR [rbp - 8], rax", "  jmp rax"],
    # A frame address form the pass does not model.
    ["  mov QWORD PTR [rbp - 8], rax", "  mov rax, QWORD PTR [rbp - 8 + r10]"],
])
def test_text_the_pass_cannot_follow_is_left_alone(body):
    assert _promote(body, (8, I64)) == PROLOGUE + body + EPILOGUE


def test_return_without_the_frame_restore_is_left_alone():
    body = ["  mov QWORD PTR [rbp - 8], rdi", "  call g", "  mov QWORD PTR [rbp - 16], rax",
            "  mov rdi, QWORD PTR [rbp - 8]", "  mov rsi, QWORD PTR [rbp - 16]", "  call h",
            "  mov rax, QWORD PTR [rbp - 16]", "  ret"]
    lines = PROLOGUE + body + EPILOGUE
    assert keep_frame_slots_in_registers(
        lines, _func((8, I64), (16, I64)), _plan(), windows=False) == lines
