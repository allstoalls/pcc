from dataclasses import FrozenInstanceError
from unittest.mock import patch
import weakref

import pytest

from pcc.backend import x86_64_encode as encoder
from pcc.backend import x86_64_asm_driver as assembler
from pcc.backend.elf_x86_64 import emit_relocatable
from tests.python.test_x86_64_encode import _EMITTER_SHAPES


@pytest.mark.parametrize('text', [
    'rax', 'XMM3', 'r12d', '-9223372036854775808', '0xff', '.Lnext',
    'QWORD PTR [rbp - 8]', 'DWORD PTR [rax + rdx * 4 - 12]',
    'QWORD PTR data[rip]', 'QWORD PTR tls@gottpoff[rip]',
    'QWORD PTR fs:0', 'BYTE PTR [r12 + 128]',
])
def test_operand_cache_reuses_only_immutable_syntax(text):
    expected = encoder._parse_operand(text)
    cache = encoder._OperandParseCache()
    with patch.object(encoder, '_parse_operand', wraps=encoder._parse_operand) as parse:
        first = cache.parse(text)
        second = cache.parse(text)
        assert parse.call_count == 1
    assert first is second
    assert first == expected
    with pytest.raises(FrozenInstanceError):
        first.unexpected = 1


@pytest.mark.parametrize('line', [
    'call .Lnext', 'jmp .Lnext', 'jne .Lnext',
    'mov rax, QWORD PTR data[rip]', 'mov rax, QWORD PTR tls@gottpoff[rip]',
    'mov rax, QWORD PTR fs:0', 'lock xadd QWORD PTR [rbp - 8], rax',
    'mov rax, 255', 'add rax, rdx',
])
def test_cached_operands_preserve_pc_labels_sections_and_relocations(line):
    cache = encoder._OperandParseCache()
    for pc, labels, section in [
        (0, {}, '.text'), (128, {}, '.text'),
        (24, {'.Lnext': ('.text', 4)}, '.text'),
        (100, {'.Lnext': ('.text', 400)}, '.text'),
        (200, {'.Lnext': ('.data', 8)}, '.text'),
        (200, {'.Lnext': ('.data', 8)}, '.data'),
    ]:
        expected = encoder.encode_instruction(line, pc=pc, labels=labels, section_name=section)
        actual = encoder.encode_instruction(line, pc=pc, labels=labels, section_name=section, operand_cache=cache)
        assert actual == expected


def test_operand_cache_bounds_entries_and_key_storage(monkeypatch):
    monkeypatch.setattr(encoder, '_OPERAND_CACHE_MAX_ENTRIES', 2)
    monkeypatch.setattr(encoder, '_OPERAND_CACHE_MAX_KEY_BYTES', 24)
    cache = encoder._OperandParseCache()
    for text in ['rax', 'rbx', 'rcx', 'rdx', '1234567', 'rsi']:
        assert cache.parse(text) == encoder._parse_operand(text)
        assert len(cache.entries) <= 2
        assert cache.key_bytes == sum(4 * len(key) for key in cache.entries)
        assert cache.key_bytes <= 24
    assert '1234567' not in cache.entries


def test_operand_cache_does_not_retain_failures():
    cache = encoder._OperandParseCache()
    with patch.object(encoder, '_parse_operand', wraps=encoder._parse_operand) as parse:
        for _ in range(2):
            with pytest.raises(encoder.X86EncodeError, match='bad memory scale'):
                cache.parse('QWORD PTR [rax + rdx * 3]')
        assert parse.call_count == 2
    assert not cache.entries


def test_operand_cache_is_file_local_and_object_equal(monkeypatch):
    text = '''.intel_syntax noprefix
.text
.globl probe
.type probe, @function
probe:
  mov rax, QWORD PTR [rsp + 8]
  mov rdx, QWORD PTR [rsp + 8]
  call external
  jne .Lnext
  mov rax, QWORD PTR data[rip]
.Lnext:
  ret
.size probe, .-probe
.data
data:
  .quad 42
'''
    references = []
    real = assembler._OperandParseCache
    def create():
        value = real()
        references.append(weakref.ref(value))
        return value
    monkeypatch.setattr(assembler, '_OperandParseCache', create)
    cached = emit_relocatable(assembler.assemble_file(text))
    assert len(references) == 1 and references[0]() is None
    monkeypatch.setattr(encoder, '_OPERAND_CACHE_MAX_ENTRIES', 0)
    uncached = emit_relocatable(assembler.assemble_file(text))
    assert cached == uncached
    assert len(references) == 2 and references[1]() is None


@pytest.mark.parametrize('line', _EMITTER_SHAPES)
def test_operand_cache_preserves_every_existing_emitter_shape(line):
    cache = encoder._OperandParseCache()
    for pc, labels in ((0, {}), (127, {}), (64, {
        'external_function': ('.text', 16),
        'external_target': ('.text', 256),
    })):
        expected = encoder.encode_instruction(line, pc=pc, labels=labels, section_name='.text')
        assert encoder.encode_instruction(
            line, pc=pc, labels=labels, section_name='.text', operand_cache=cache,
        ) == expected


@pytest.mark.parametrize('line', [
    'mov ah, r10b', 'jmp 7', 'add r10, 0x100000000',
    'movsd xmm10, DWORD PTR [r11]', 'lock mov rax, rdx',
])
def test_operand_cache_preserves_instruction_validation_after_hits(line):
    cache = encoder._OperandParseCache()
    with pytest.raises(encoder.X86EncodeError) as baseline:
        encoder.encode_instruction(line, pc=16, labels={}, section_name='.text')
    for _ in range(2):
        with pytest.raises(encoder.X86EncodeError) as candidate:
            encoder.encode_instruction(
                line, pc=16, labels={}, section_name='.text', operand_cache=cache,
            )
        assert str(candidate.value) == str(baseline.value)


@pytest.mark.parametrize('method', ['__len__', '__hash__', '__eq__'])
def test_operand_cache_bypasses_hostile_string_subclasses(method):
    def forbidden(*args):
        raise AssertionError('cache-only subclass operation: ' + method)
    hostile_type = type('HostileOperand', (str,), {method: forbidden})
    text = hostile_type('rax')
    expected = encoder._parse_operand(text)
    cache = encoder._OperandParseCache()
    # An existing equal plain-string key must not pull subclasses into lookup.
    cache.parse('rax')
    entries_before = dict(cache.entries)
    bytes_before = cache.key_bytes
    with patch.object(encoder, '_parse_operand', wraps=encoder._parse_operand) as parse:
        assert cache.parse(text) == expected
        assert cache.parse(text) == expected
        assert parse.call_count == 2
    assert cache.entries == entries_before
    assert cache.key_bytes == bytes_before
