"""Narrow atomic words match independent ISA examples, without an assembler.

Arm 100076_0100: D3.13/14/18/95/99 specify W data/status registers and bare
Xn/SP addressing. Independent expected bytes come from LLVM's upstream
llvm/test/MC/AArch64/arm64-memory.s acquire/release instruction examples:
https://raw.githubusercontent.com/llvm/llvm-project/main/llvm/test/MC/AArch64/arm64-memory.s
No LLVM tool executes in these tests.
"""

import struct
import subprocess

import pytest

from pcc.backend.arm64_encode import EncodeError, assemble_text
from pcc.backend.self_backend_aarch64_darwin_mem import (
    DIRECT_INSTRUCTION_PLACEHOLDER,
    begin_direct_instruction_capture,
    borrow_direct_instruction_records,
    emitted_memory_instruction_line,
    end_direct_instruction_capture,
)


@pytest.fixture(autouse=True)
def no_external_assembler(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("external assembler process")
    monkeypatch.setattr(subprocess, "Popen", forbidden)


@pytest.mark.parametrize("instruction, encoding", [
    ("ldarb w4, [sp]", "e4ffdf08"),
    ("ldarh w4, [sp]", "e4ffdf48"),
    ("ldaxrh w2, [x4]", "82fc5f48"),
    ("stlrh w3, [x6]", "c3fc9f48"),
    ("stlxrh w8, w7, [x1]", "27fc0848"),
    ("ldaxrb w2, [x4]", "82fc5f08"),
    ("stlrb w3, [x6]", "c3fc9f08"),
    ("stlxrb w8, w7, [x1]", "27fc0808"),
])
def test_narrow_atomic_words_match_independent_reference(instruction, encoding):
    assembled = assemble_text(instruction)
    assert assembled.code == bytes.fromhex(encoding)
    assert assembled.relocations == []


@pytest.mark.parametrize("mnemonic", ["ldarb", "ldarh", "ldaxrh", "stlrh"])
@pytest.mark.parametrize("operands", ["x2, [x4]", "w2, [w4]", "w2, [x4, #1]", "w2, [x4]!"])
def test_narrow_atomic_operands_fail_closed(mnemonic, operands):
    with pytest.raises(EncodeError):
        assemble_text(mnemonic + " " + operands)


@pytest.mark.parametrize("operands", [
    "x8, w7, [x1]", "w8, x7, [x1]", "w8, w7, [w1]",
    "w8, w7, [x1, #2]", "w8, w7, [x1]!",
])
def test_halfword_exclusive_store_operands_fail_closed(operands):
    with pytest.raises(EncodeError):
        assemble_text("stlxrh " + operands)


@pytest.mark.parametrize("mnemonic", ["ldarb", "ldarh", "stlrb", "stlrh"])
@pytest.mark.parametrize("base", ["x6", "sp"])
def test_direct_narrow_atomic_word_matches_text_without_fallback(mnemonic, base):
    expected, = struct.unpack("<I", assemble_text(mnemonic + " w3, [" + base + "]").code)
    begin_direct_instruction_capture()
    try:
        line = emitted_memory_instruction_line(mnemonic, "w3", base)
        words = borrow_direct_instruction_records().diagnostic_values()
        assert line == DIRECT_INSTRUCTION_PLACEHOLDER
        assert len(words) == 4
        assert words[0] == expected
    finally:
        end_direct_instruction_capture()


@pytest.mark.parametrize("mnemonic", ["ldarb", "ldarh", "stlrb", "stlrh"])
def test_invalid_narrow_direct_register_cannot_publish_a_word(mnemonic):
    begin_direct_instruction_capture()
    try:
        line = emitted_memory_instruction_line(mnemonic, "x3", "x6")
        assert len(borrow_direct_instruction_records()) == 0
        with pytest.raises(EncodeError):
            assemble_text(line)
    finally:
        end_direct_instruction_capture()
