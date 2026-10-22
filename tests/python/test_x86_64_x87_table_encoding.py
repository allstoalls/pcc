"""Real encoder controls for x87 tables and the operand-whitespace fast path.

Whitespace controls extend the existing nodes and bounded CI invocation.
"""
from __future__ import annotations

from unittest.mock import patch

from pcc.backend import x86_64_encode as encoder
from pcc.backend import x86_64_asm_driver as assembler
from pcc.backend.elf_x86_64 import emit_relocatable, parse_relocatable
from tests.x86_64_x87_reference import old_encoder
from tests.x86_64_operand_whitespace import (
    old_operand_parser,
    record_whitespace_operation_ab,
    target_cases,
)


# Independent frozen byte expectations cover every literal moved to globals.
CASES = (
    ("faddp", b"\xde\xc1"), ("fsubp", b"\xde\xe9"),
    ("fmulp", b"\xde\xc9"), ("fdivp", b"\xde\xf9"),
    ("fchs", b"\xd9\xe0"), ("fabs", b"\xd9\xe1"),
    ("fsqrt", b"\xd9\xfa"), ("fprem", b"\xd9\xf8"),
    ("frndint", b"\xd9\xfc"), ("fldz", b"\xd9\xee"),
    ("fxch st(1)", b"\xd9\xc9"), ("fld st(0)", b"\xd9\xc0"),
    ("fstp st(0)", b"\xdd\xd8"), ("fstp st(1)", b"\xdd\xd9"),
    ("fucomip st(0), st(1)", b"\xdf\xe9"), ("fnstsw ax", b"\xdf\xe0"),
)
CONTEXTS = (
    (0, {}, ".text"), (127, {}, ".text"),
    (32, {"target": (".text", 400)}, ".text"),
    (200, {"target": (".data", 8)}, ".text"),
    (200, {"target": (".data", 8)}, ".data"),
    (1 << 40, {"target": (".text", 0)}, ".text"),
)


def _result(call, line, pc, labels, section, cache=None):
    try:
        value = call(line, pc=pc, labels=labels, section_name=section, operand_cache=cache)
        return value.code, value.relocations
    except Exception as error:
        return type(error), str(error)


def _operand_result(call, text):
    try:
        return ("return", call(text))
    except Exception as error:
        context = error.__context__
        cause = error.__cause__
        return ("raise", type(error), str(error),
                type(context), str(context), type(cause), str(cause),
                error.__suppress_context__)


def _check_operand_whitespace_results():
    before = old_operand_parser(vars(encoder))
    cases = target_cases() + [
        "rax", "XMM3", "-17", "0xff", "0b101", "3.14", "nan",
        "QWORD PTR [rbp - 8]", "QWORD PTR data[rip]", "QWORD PTR fs:0",
        "QWORD PTR [rax + rdx * 3]", "[]", "[rax + rbx + rcx]",
    ]
    for text in cases:
        assert _operand_result(encoder._parse_operand, text) == _operand_result(before, text)
    # Whitespace removal must not substitute a split piece for the target name.
    text = "ordinary_target_" + "x" * 257
    assert encoder._parse_operand(text).name is text
    lines = ("call target", "jmp .Ltarget", "jne target", "call left right",
             "call left\u2003right", "call \u2003target\u2003", "call 7",
             "mov rax, QWORD PTR data[rip]", "lock ret")
    for line in lines:
        for pc, labels, section in CONTEXTS:
            for cached in (False, True):
                actual = _result(encoder.encode_instruction, line, pc, labels, section,
                                 encoder._OperandParseCache() if cached else None)
                with patch.object(encoder, "_parse_operand", before):
                    expected = _result(encoder.encode_instruction, line, pc, labels, section,
                                       encoder._OperandParseCache() if cached else None)
                assert actual == expected


def _check_operand_whitespace_protocols():
    before = old_operand_parser(vars(encoder))
    for mode in ("untrimmed", "plain", "self", "iter_error", "space_error", "short_circuit", "falsy"):
        observed = []
        for call in (before, encoder._parse_operand):
            events = []

            class Character:
                def __init__(self, value):
                    self.value = value

                def isspace(self):
                    events.append(("isspace", self.value))
                    if self.value == "error":
                        raise LookupError("character whitespace failure")
                    return self.value

            class Name(str):
                def __len__(self):
                    events.append("name.len")
                    return 0 if mode == "falsy" else str.__len__(self)

                def split(self, *args):
                    raise AssertionError("subclass name must not use split")

                def __iter__(self):
                    events.append("name.iter")
                    if mode in ("iter_error", "falsy"):
                        raise RuntimeError("name iteration failure")
                    if mode == "space_error":
                        return iter((Character("error"),))
                    if mode == "short_circuit":
                        return iter((Character(False), Character(True), Character("error")))
                    return iter((Character(False),))

            name = " target " if mode == "untrimmed" else "target" if mode == "plain" else Name("target")

            class Input(str):
                def strip(self):
                    events.append("input.strip")
                    return name if events.count("input.strip") == 4 else "target"

                def __hash__(self):
                    raise AssertionError("operand input must not be hashed")

                def __repr__(self):
                    events.append("input.repr")
                    return "<operand-input>"

            result = _operand_result(call, Input("target"))
            assert events.count("input.strip") == 4
            if result[0] == "return":
                assert result[1].name is name
            else:
                # The failed int conversion stays the implicit exception context.
                assert result[3] is ValueError
            if mode == "short_circuit":
                assert ("isspace", "error") not in events
            if mode == "falsy":
                assert "name.iter" not in events
            if mode == "untrimmed":
                assert result[0:3] == ("raise", encoder.X86EncodeError,
                                       "bad operand <operand-input>")
            observed.append((result, events))
        assert observed[0] == observed[1]

    # A stateful original subclass returning itself must also retain all calls.
    for call in (before, encoder._parse_operand):
        events = []

        class SelfReturning(str):
            def strip(self):
                events.append("strip")
                return self

            def split(self, *args):
                raise AssertionError("original subclass must not use split")

        text = SelfReturning("target")
        assert call(text).name is text
        assert events == ["strip"] * 4


def _check_operand_whitespace_object_bytes():
    assembly = (
        ".intel_syntax noprefix\n.text\n.globl probe\n.type probe, @function\n"
        "probe:\n  call external\n  jne .Ltarget\n  jmp .L目标\n"
        ".Ltarget:\n  mov rax, QWORD PTR data[rip]\n"
        ".L目标:\n  ret\n.size probe, .-probe\n.data\ndata:\n  .quad 42\n"
    )
    actual = emit_relocatable(assembler.assemble_file(assembly))
    before = old_operand_parser(vars(encoder))
    with patch.object(encoder, "_parse_operand", before):
        expected = emit_relocatable(assembler.assemble_file(assembly))
    assert actual == expected
    obj = parse_relocatable(actual)
    section = next(section for section in obj.sections if section.name == ".text")
    assert {obj.symbols[item.symbol_index].name for item in section.relocations} == {"external", "data"}


def test_x87_all_sixteen_forms_have_exact_bytes_and_fresh_results():
    cache = encoder._OperandParseCache()
    for line, expected in CASES:
        for pc, labels, section in CONTEXTS:
            first = encoder.encode_instruction(line, pc=pc, labels=labels,
                                               section_name=section, operand_cache=cache)
            second = encoder.encode_instruction(line, pc=pc + 19, labels=labels,
                                                section_name=section, operand_cache=cache)
            assert first.code == second.code == expected
            assert first.relocations == second.relocations == ()
            assert first is not second
            assert line not in cache.instruction_encodings


def test_x87_real_encoding_matches_frozen_preimage_results_and_errors(tmp_path):
    _check_operand_whitespace_results()
    before = old_encoder(vars(encoder))
    lines = [line for line, _ in CASES]
    lines += ["  " + line.partition(" ")[0].upper() + (" " + line.partition(" ")[2] if " " in line else "") + "  "
              for line, _ in CASES]
    lines += ["", "  ", "faddp rax", "faddp [", "fxch st(0)", "FXCH ST(1)",
              "fucomip st(0),st(1)", "fxch\tst(1)", "fnstsw eax",
              "fstp BYTE PTR [rbp - 8]", "fld TBYTE PTR [rbp - 16]",
              "mov rax, 42", "mov QWORD PTR [r11], 2147483648", "lock ret",
              "lock xadd QWORD PTR data[rip], rax", "mov rax, QWORD PTR data[rip]",
              "mov rax, QWORD PTR tls@gottpoff[rip]", "call target", "jmp target", "jne target"]
    for line in lines:
        for pc, labels, section in CONTEXTS:
            for cache_type in (lambda: None, encoder._OperandParseCache):
                assert _result(before, line, pc, labels, section, cache_type()) == _result(
                    encoder._encode_instruction_uncached, line, pc, labels, section, cache_type(),
                )
    record_whitespace_operation_ab(tmp_path)


def test_x87_real_error_order_and_custom_cache_protocols():
    _check_operand_whitespace_protocols()
    class CustomCache:
        def __init__(self):
            self.calls = []

        def parse(self, item):
            self.calls.append(item)
            raise LookupError("operand boundary " + item)

    class HostileString(str):
        def __hash__(self):
            raise AssertionError("line must not enter cache hash")

    before = old_encoder(vars(encoder))
    for line, expected in CASES:
        custom = CustomCache()
        assert _result(encoder.encode_instruction, HostileString(line), 3, {}, ".text", custom) == (expected, ())
        assert custom.calls == []
    for call in (before, encoder._encode_instruction_uncached):
        custom = CustomCache()
        assert _result(call, "faddp [", 3, {}, ".text", custom) == (
            encoder.X86EncodeError, "faddp takes no operands in the owned dialect",
        )
        assert custom.calls == []
        assert _result(call, "fxch st(0)", 3, {}, ".text", custom) == (
            LookupError, "operand boundary st(0)",
        )
        assert custom.calls == ["st(0)"]


def test_x87_assembled_object_matches_preimage_and_contains_exact_x87_bytes(monkeypatch):
    _check_operand_whitespace_object_bytes()
    assembly = ".intel_syntax noprefix\n.text\n.globl probe\n.type probe, @function\nprobe:\n"
    assembly += "\n".join("  " + line for line, _ in CASES)
    assembly += "\n  mov rax, 42\n  call external\n  jne .Lnext\n  mov rax, QWORD PTR data[rip]\n.Lnext:\n  ret\n.size probe, .-probe\n.data\ndata:\n  .quad 42\n"
    actual = emit_relocatable(assembler.assemble_file(assembly))
    before = old_encoder(vars(encoder))
    monkeypatch.setattr(encoder, "_encode_instruction_uncached", before)
    expected = emit_relocatable(assembler.assemble_file(assembly))
    assert actual == expected
    obj = parse_relocatable(actual)
    section = next(section for section in obj.sections if section.name == ".text")
    assert section.data[:32] == b"".join(code for _, code in CASES)
    assert {obj.symbols[item.symbol_index].name for item in section.relocations} == {"external", "data"}


def test_x87_hoist_keeps_ordinary_branch_pc_section_and_rip_offsets_uncached():
    before = old_encoder(vars(encoder))
    cache = encoder._OperandParseCache()
    for line in ("call target", "jmp target", "jne target", "mov rax, QWORD PTR data[rip]",
                 "lock xadd QWORD PTR data[rip], rax"):
        for pc, labels, section in CONTEXTS:
            assert _result(encoder.encode_instruction, line, pc, labels, section, cache) == _result(
                before, line, pc, labels, section,
            )
            assert line not in cache.instruction_encodings
    labels = {"target": (".text", 40)}
    assert _result(encoder.encode_instruction, "jne target", 0, labels, ".text", cache)[0] == b"\x0f\x85\x22\x00\x00\x00"
    assert _result(encoder.encode_instruction, "jne target", 7, labels, ".text", cache)[0] == b"\x0f\x85\x1b\x00\x00\x00"
    first = encoder.encode_instruction("mov rax, QWORD PTR data[rip]", pc=0, labels={}, section_name=".text", operand_cache=cache)
    later = encoder.encode_instruction("mov rax, QWORD PTR data[rip]", pc=11, labels={}, section_name=".text", operand_cache=cache)
    assert first.code == later.code
    assert later.relocations[0].offset == first.relocations[0].offset + 11
