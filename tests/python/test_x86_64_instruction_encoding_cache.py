"""Bounded file-local machine-byte reuse must preserve every encoder boundary."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
import weakref

import pytest

from pcc.backend import x86_64_encode as encoder
from pcc.backend import x86_64_asm_driver as assembler
from pcc.backend.elf_x86_64 import emit_relocatable
from tests.python.test_x86_64_encode import _EMITTER_SHAPES


_VARIANTS = {
    "mov": ("mov rax, 42", "mov eax, ebx", "mov rax, QWORD PTR [rbp - 8]", "mov QWORD PTR [r11], rax"),
    "test": ("test rax, rbx", "test eax, eax"),
    "lea": ("lea rax, [rbp - 8]", "lea r10, [r11 + r10*8]"),
    "imul": ("imul rax, rbx", "imul r10, r11, 257"),
    "movzx": ("movzx rax, bl", "movzx eax, WORD PTR [rbp - 8]"),
    "movsx": ("movsx rax, bx", "movsx eax, BYTE PTR [rbp - 8]"),
    "movsxd": ("movsxd rax, ebx", "movsxd r10, r11d"),
    "push": ("push rbp", "push r12"),
    "pop": ("pop rbp", "pop r12"),
}
for _mnemonic in ("add", "or", "and", "sub", "xor", "cmp"):
    _VARIANTS[_mnemonic] = (
        _mnemonic + " rax, rbx", _mnemonic + " eax, 17",
        _mnemonic + " rax, QWORD PTR [rbp - 8]",
    )
for _mnemonic in ("neg", "mul", "div", "idiv"):
    _VARIANTS[_mnemonic] = (_mnemonic + " rax", _mnemonic + " r10d")
for _mnemonic in ("shl", "shr", "sar"):
    _VARIANTS[_mnemonic] = (_mnemonic + " rax, 3", _mnemonic + " r10, cl")
for _mnemonic in ("xchg", "xadd", "cmpxchg"):
    _VARIANTS[_mnemonic] = (
        _mnemonic + " QWORD PTR [r11], r10",
        _mnemonic + " DWORD PTR [r11], r10d",
    )
for _mnemonic in ("nop", "cdq", "cqo", "syscall", "mfence", "ret", "ud2"):
    _VARIANTS[_mnemonic] = (_mnemonic, _mnemonic.upper())

_CONTEXTS = (
    (0, {}, ".text"), (127, {}, ".text"),
    (32, {".Lnext": (".text", 400), "external_function": (".text", 16)}, ".text"),
    (200, {".Lnext": (".data", 8)}, ".text"),
    (200, {".Lnext": (".data", 8)}, ".data"),
    (1 << 40, {".Lnext": (".text", 0)}, ".text"),
)


def _result(call, line, pc, labels, section, cache):
    try:
        return call(line, pc=pc, labels=labels, section_name=section, operand_cache=cache)
    except encoder.X86EncodeError as error:
        return type(error), str(error)


def test_instruction_cache_allowlist_has_audited_operand_variants():
    assert set(_VARIANTS) == set(encoder._PC_INDEPENDENT_INSTRUCTION_MNEMONICS)


@pytest.mark.parametrize("mnemonic", tuple(_VARIANTS))
def test_instruction_cache_matches_real_pc_encoding_for_every_allowed_family(mnemonic):
    cache = encoder._OperandParseCache()
    for line in _VARIANTS[mnemonic]:
        for pc, labels, section in _CONTEXTS:
            expected = encoder._encode_instruction_uncached(
                line, pc=pc, labels=labels, section_name=section,
            )
            assert encoder.encode_instruction(
                line, pc=pc, labels=labels, section_name=section, operand_cache=cache,
            ) == expected


@pytest.mark.parametrize("line", _EMITTER_SHAPES)
def test_instruction_cache_preserves_the_complete_existing_encoder_corpus(line):
    cache = encoder._OperandParseCache()
    for pc, labels, section in _CONTEXTS:
        assert _result(encoder.encode_instruction, line, pc, labels, section, cache) == _result(
            encoder._encode_instruction_uncached, line, pc, labels, section, None,
        )


@pytest.mark.parametrize("line", (
    "call .Lnext", "jmp .Lnext", "jne .Lnext", "call rax",
    "jrcxz .Lnext", "loop .Lnext", "loope .Lnext", "loopne .Lnext",
    "mov rax, QWORD PTR data[rip]", "mov DWORD PTR data[rip], 24",
    "mov rax, QWORD PTR tls@gottpoff[rip]", "lock xadd QWORD PTR data[rip], rax",
))
def test_instruction_cache_never_reuses_branches_or_relocation_offsets(line):
    cache = encoder._OperandParseCache()
    for pc, labels, section in _CONTEXTS:
        assert _result(encoder.encode_instruction, line, pc, labels, section, cache) == _result(
            encoder._encode_instruction_uncached, line, pc, labels, section, None,
        )
        assert line not in cache.instruction_encodings


def test_instruction_cache_hits_skip_encoding_but_return_fresh_immutable_records(monkeypatch):
    calls = []
    original = encoder._encode_instruction_uncached

    def observed(*args, **kwargs):
        calls.append(kwargs["pc"])
        return original(*args, **kwargs)

    monkeypatch.setattr(encoder, "_encode_instruction_uncached", observed)
    cache = encoder._OperandParseCache()
    line = "mov rax, QWORD PTR [rbp - 8]"
    results = [encoder.encode_instruction(
        line, pc=index * 97, labels={}, section_name=".text", operand_cache=cache,
    ) for index in range(128)]
    assert calls == [0]
    assert all(result == results[0] for result in results)
    assert results[0] is not results[1]
    assert all(type(code) is bytes for code in cache.instruction_encodings.values())
    with pytest.raises(FrozenInstanceError):
        results[0].code = b"changed"
    reference = weakref.ref(cache)
    del cache
    assert reference() is None


@pytest.mark.parametrize("line", (
    "mov ah, r10b", "mov QWORD PTR [r11], 2147483648", "lock ret",
    "faddp rax", "fxch st(0)", "fstp BYTE PTR [rbp - 8]",
))
def test_instruction_cache_never_retains_failures(line, monkeypatch):
    calls = []
    original = encoder._encode_instruction_uncached

    def observed(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    expected = _result(original, line, 3, {}, ".text", None)
    assert isinstance(expected, tuple) and expected[0] is encoder.X86EncodeError
    monkeypatch.setattr(encoder, "_encode_instruction_uncached", observed)
    cache = encoder._OperandParseCache()
    for _ in range(2):
        assert _result(encoder.encode_instruction, line, 3, {}, ".text", cache) == expected
    assert calls == [line, line]
    assert not cache.instruction_encodings


def test_instruction_cache_bounds_accounted_storage_and_resets_on_eviction(monkeypatch):
    monkeypatch.setattr(encoder, "_INSTRUCTION_ENCODING_CACHE_MAX_ENTRIES", 2)
    monkeypatch.setattr(encoder, "_INSTRUCTION_ENCODING_CACHE_MAX_BYTES", 128)
    cache = encoder._OperandParseCache()
    for immediate in range(20):
        line = "mov rax, " + str(immediate)
        encoder.encode_instruction(line, pc=0, labels={}, section_name=".text", operand_cache=cache)
        assert len(cache.instruction_encodings) <= 2
        assert cache.instruction_encoding_bytes == sum(
            4 * len(key) + len(value) for key, value in cache.instruction_encodings.items()
        )
        assert cache.instruction_encoding_bytes <= 128
    line = "mov rax, 7" + " " * 1000
    encoder.encode_instruction(line, pc=0, labels={}, section_name=".text", operand_cache=cache)
    assert line not in cache.instruction_encodings


def test_instruction_cache_byte_bound_evicts_before_the_entry_bound(monkeypatch):
    monkeypatch.setattr(encoder, "_INSTRUCTION_ENCODING_CACHE_MAX_ENTRIES", 4096)
    monkeypatch.setattr(encoder, "_INSTRUCTION_ENCODING_CACHE_MAX_BYTES", 80)
    cache = encoder._OperandParseCache()
    for line in ("mov rax, 0", "mov rax, 1"):
        encoder.encode_instruction(line, pc=0, labels={}, section_name=".text", operand_cache=cache)
    assert set(cache.instruction_encodings) == {"mov rax, 1"}
    assert cache.instruction_encoding_bytes == 4 * len(line) + len(cache.instruction_encodings[line])


@pytest.mark.parametrize("limit", (
    "_INSTRUCTION_ENCODING_CACHE_MAX_ENTRIES", "_INSTRUCTION_ENCODING_CACHE_MAX_BYTES",
))
def test_instruction_cache_can_be_disabled_without_changing_results(limit, monkeypatch):
    monkeypatch.setattr(encoder, limit, 0)
    cache = encoder._OperandParseCache()
    for line in ("mov rax, 42", "ret"):
        assert encoder.encode_instruction(
            line, pc=0, labels={}, section_name=".text", operand_cache=cache,
        ) == encoder._encode_instruction_uncached(line, pc=0, labels={}, section_name=".text")
    assert not cache.instruction_encodings


def test_instruction_cache_preserves_string_and_cache_subclass_protocols():
    class HostileString(str):
        def __hash__(self):
            raise AssertionError("cache-only string-subclass hash")

    class CustomCache(encoder._OperandParseCache):
        def __init__(self):
            super().__init__()
            self.parse_calls = 0

        def parse(self, text):
            self.parse_calls += 1
            return super().parse(text)

    line = HostileString("mov rax, 42")
    cache = encoder._OperandParseCache()
    assert encoder.encode_instruction(
        line, pc=0, labels={}, section_name=".text", operand_cache=cache,
    ) == encoder._encode_instruction_uncached(line, pc=0, labels={}, section_name=".text")
    assert not cache.instruction_encodings
    custom = CustomCache()
    for _ in range(2):
        encoder.encode_instruction("mov rax, 42", pc=0, labels={}, section_name=".text", operand_cache=custom)
    assert custom.parse_calls == 4 and not custom.instruction_encodings


def test_instruction_cache_is_independent_between_concurrent_file_owners():
    def job(index):
        cache = encoder._OperandParseCache()
        for pc in range(index * 100, index * 100 + 20):
            for line in ("mov rax, QWORD PTR [rbp - 8]", "add rax, 17", "call .Lnext", "mov rax, QWORD PTR data[rip]"):
                labels = {".Lnext": (".text", index * 200 + 4)}
                assert encoder.encode_instruction(
                    line, pc=pc, labels=labels, section_name=".text", operand_cache=cache,
                ) == encoder._encode_instruction_uncached(line, pc=pc, labels=labels, section_name=".text")
        return cache

    with ThreadPoolExecutor(max_workers=4) as executor:
        caches = list(executor.map(job, range(8)))
    assert len({id(cache) for cache in caches}) == 8
    assert len({id(cache.instruction_encodings) for cache in caches}) == 8


def test_instruction_cache_preserves_assembled_object_bytes_and_file_lifetime(monkeypatch):
    source = """.intel_syntax noprefix
.text
.globl probe
.type probe, @function
probe:
  mov rax, QWORD PTR [rsp + 8]
  mov rax, QWORD PTR [rsp + 8]
  call external
  jne .Lnext
  mov rax, QWORD PTR data[rip]
.Lnext:
  ret
.size probe, .-probe
.data
data:
  .quad 42
"""
    references = []
    original = assembler._OperandParseCache

    def create():
        cache = original()
        references.append(weakref.ref(cache))
        return cache

    monkeypatch.setattr(assembler, "_OperandParseCache", create)
    cached = emit_relocatable(assembler.assemble_file(source))
    assert len(references) == 1 and references[0]() is None
    monkeypatch.setattr(encoder, "_INSTRUCTION_ENCODING_CACHE_MAX_ENTRIES", 0)
    uncached = emit_relocatable(assembler.assemble_file(source))
    assert cached == uncached
    assert len(references) == 2 and references[1]() is None
