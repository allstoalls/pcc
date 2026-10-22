"""Bounded x86 assembly line splitting preserves the owned parser contract."""

import sys
import platform
from dataclasses import FrozenInstanceError, replace
import re
import builtins
import weakref

import pytest

from pcc.backend import elf_x86_64 as elf
from pcc.backend import x86_64_asm_driver as assembler
from pcc.backend.x86_64_encode import X86EncodeError


# Checks the serialized precise stack-map table, which objects carry only on request.
pytestmark = pytest.mark.usefixtures("precise_stackmap_tables")


LINE_ENDINGS = (
    "\n", "\r", "\r\n", "\v", "\f", "\x1c", "\x1d", "\x1e",
    "\x85", "\u2028", "\u2029",
)
LINE_ENDING_NAMES = (
    "lf", "cr", "crlf", "vt", "ff", "file-separator", "group-separator",
    "record-separator", "next-line", "line-separator", "paragraph-separator",
)
VALID_LINES = (
    ".intel_syntax noprefix",
    ".data",
    ".globl payload",
    ".type payload, @object",
    "payload:",
    "  .byte 1, 2, 255",
    "  .short 4660",
    "  .long -1",
    "  .quad probe+8",
    ".size payload, .-payload",
    ".text",
    ".globl probe",
    ".type probe, @function",
    ".p2align 4, 0x90",
    "probe:",
    "  mov eax, 42",
    "  jmp .Ldone",
    ".Ldone:",
    "  ret",
    ".size probe, .-probe",
    '.section .note.GNU-stack,"",@progbits',
)


def _mixed_lines(lines):
    # Include leading and adjacent boundaries: the parser skips empty lines.
    result = ["\r\n", "\u2028"]
    for index, line in enumerate(lines):
        ending = LINE_ENDINGS[index % len(LINE_ENDINGS)]
        result.extend((line, ending))
        if index % 3 == 0:
            result.append(ending)
    return "".join(result)


@pytest.mark.parametrize("chunk_size", [1, 2, 7, 32])
def test_line_iterator_matches_splitlines_empty_adjacent_and_trailing(
    monkeypatch, chunk_size,
):
    monkeypatch.setattr(assembler, "_ASSEMBLY_LINE_CHUNK_CHARS", chunk_size)
    cases = [
        "", "one", " \t\x00", "\r\r\n", "\r\n\r", "\r\n\n",
        "".join(LINE_ENDINGS),
        "alpha" + "".join(LINE_ENDINGS) + "omega",
    ]
    for ending in LINE_ENDINGS:
        cases.extend((
            ending,
            ending * 2,
            "one" + ending,
            ending + "one",
            "one" + ending + "two",
            "one" + ending * 2 + "two" + ending * 2,
            "  one\t " + ending + " two\x00 ",
        ))
    for text in cases:
        assert list(assembler._iter_assembly_lines(text)) == text.splitlines(), repr(text)


@pytest.mark.parametrize("ending", LINE_ENDINGS, ids=LINE_ENDING_NAMES)
def test_line_endings_at_and_across_chunk_boundaries(monkeypatch, ending):
    chunk_size = 8
    monkeypatch.setattr(assembler, "_ASSEMBLY_LINE_CHUNK_CHARS", chunk_size)
    for prefix_length in (0, 1, 7, 8, 9, 15, 16, 17):
        for suffix in ("", "tail", ending, "tail" + ending):
            text = "x" * prefix_length + ending + suffix
            assert list(assembler._iter_assembly_lines(text)) == text.splitlines(), (
                prefix_length, repr(ending), repr(suffix),
            )


def test_long_line_spans_multiple_chunks_without_content_changes():
    width = assembler._ASSEMBLY_LINE_CHUNK_CHARS
    long_line = " \t" + "x" * (width * 3 + 19) + "\x00\t "
    text = "head\r\n" + long_line + "\u2028" + long_line + "\r\n"
    assert list(assembler._iter_assembly_lines(text)) == ["head", long_line, long_line]
    assert list(assembler._iter_assembly_lines(long_line)) == [long_line]


class _CRLFOnlyText(str):
    """Host simulation of a runtime whose splitlines recognizes CR/LF only."""

    def __getitem__(self, key):
        # Preserve the simulated behavior for both chunks and line[-1], which
        # the iterator can use to query that runtime's separator semantics.
        return type(self)(super().__getitem__(key))

    def splitlines(self, keepends=False):
        result = []
        position = 0
        for match in re.finditer(r"\r\n|\r|\n", self):
            end = match.end() if keepends else match.start()
            result.append(self[position:end])
            position = match.end()
        if position < len(self):
            result.append(self[position:])
        return result


@pytest.mark.parametrize(
    "character", LINE_ENDINGS[3:], ids=LINE_ENDING_NAMES[3:],
)
def test_host_simulated_crlf_splitter_keeps_other_boundary_characters(
    monkeypatch, character,
):
    """This simulation checks compatibility, and makes no native-run claim."""
    monkeypatch.setattr(assembler, "_ASSEMBLY_LINE_CHUNK_CHARS", 8)
    for prefix_length in (7, 15):
        for suffix in ("", "tail", "tail\r\nend\n"):
            text = _CRLFOnlyText("x" * prefix_length + character + suffix)
            expected = text.splitlines()
            assert character in expected[0]
            assert list(assembler._iter_assembly_lines(text)) == expected


class _MeasuredAssemblyText(str):
    """Observe eager splitting while preserving observation through slices."""

    def __new__(cls, value, split_lengths):
        result = super().__new__(cls, value)
        result.split_lengths = split_lengths
        return result

    def __getitem__(self, key):
        result = super().__getitem__(key)
        if isinstance(key, slice):
            return type(self)(result, self.split_lengths)
        return result

    def splitlines(self, keepends=False):
        self.split_lengths.append(len(self))
        return super().splitlines(keepends)


def _many_instruction_lines():
    # Keep the public-parser regression runnable against the original driver,
    # which has neither the new helper nor its chunk-size constant.
    width = getattr(assembler, "_ASSEMBLY_LINE_CHUNK_CHARS", 65536)
    count = (width * 3) // len("  nop\r\n") + 1
    text = ".intel_syntax noprefix\r\n.text\r\nprobe:\r\n"
    text += "  nop\r\n" * count
    text += "  ret\r\n.size probe, .-probe\r\n"
    return text, count


def test_public_assembler_never_eagerly_splits_the_whole_assembly():
    text, count = _many_instruction_lines()
    split_lengths = []
    measured = _MeasuredAssemblyText(text, split_lengths)
    obj = assembler.assemble_file(measured)
    # This reaches the actual single pass. The previous asm_text.splitlines()
    # implementation records one call of len(text), failing the bound.
    assert split_lengths
    bound = getattr(assembler, "_ASSEMBLY_LINE_CHUNK_CHARS", 65536) + 1
    assert max(split_lengths) <= bound
    assert sum(split_lengths) <= len(text) * 2
    assert len(split_lengths) > 1
    assert [section.name for section in obj.sections] == [".text"]
    assert obj.sections[0].data == b"\x90" * count + b"\xc3"
    assert "probe" in [symbol.name for symbol in obj.symbols]
    assert elf.emit_relocatable(obj) == elf.emit_relocatable(assembler.assemble_file(text))


def test_line_iterator_does_not_split_later_chunks_before_consumption():
    text, _count = _many_instruction_lines()
    split_lengths = []
    iterator = assembler._iter_assembly_lines(_MeasuredAssemblyText(text, split_lengths))
    assert iter(iterator) is iterator
    assert split_lengths == []
    assert next(iterator) == ".intel_syntax noprefix"
    assert split_lengths
    assert sum(split_lengths) < len(text)
    assert max(split_lengths) <= assembler._ASSEMBLY_LINE_CHUNK_CHARS + 1


@pytest.mark.parametrize("chunk_size", [1, 7, 64])
def test_mixed_separators_preserve_lines_and_complete_object_bytes(
    monkeypatch, chunk_size,
):
    normal = "\n".join(VALID_LINES) + "\n"
    expected_object = elf.emit_relocatable(assembler.assemble_file(normal))
    monkeypatch.setattr(assembler, "_ASSEMBLY_LINE_CHUNK_CHARS", chunk_size)
    mixed = _mixed_lines(VALID_LINES)
    assert list(assembler._iter_assembly_lines(mixed)) == mixed.splitlines()
    actual_object = elf.emit_relocatable(assembler.assemble_file(mixed))
    assert actual_object == expected_object
    obj = elf.parse_relocatable(actual_object)
    data = next(section for section in obj.sections if section.name == ".data")
    assert data.data[:9] == b"\x01\x02\xff\x34\x12\xff\xff\xff\xff"
    assert len(data.relocations) == 1


@pytest.mark.parametrize("lines,expected", [
    ((".text", "probe:", "  ret"),
     "owned x86 assembly must begin with .intel_syntax noprefix"),
    ((".intel_syntax noprefix", ".data", "  nop"),
     "instruction outside .text: 'nop'"),
    ((".intel_syntax noprefix", ".text", ".p2align 17"),
     ".p2align outside finite range: '.p2align 17'"),
    ((".intel_syntax noprefix", ".data", ".byte 256"),
     ".byte value 256 does not fit 8 bits"),
    ((".intel_syntax noprefix", ".text", ".unknown 7"),
     "directive not proven: '.unknown 7'"),
    ((".intel_syntax noprefix", ".text", "probe:", "  nop", "probe:", "  ret"),
     "duplicate assembly label 'probe'"),
])
def test_invalid_mixed_separators_preserve_exact_exception_and_diagnostic(
    monkeypatch, lines, expected,
):
    monkeypatch.setattr(assembler, "_ASSEMBLY_LINE_CHUNK_CHARS", 7)
    errors = []
    for text in ("\n".join(lines) + "\n", _mixed_lines(lines)):
        with pytest.raises(X86EncodeError) as caught:
            assembler.assemble_file(text)
        errors.append((type(caught.value), str(caught.value)))
    assert errors == [(X86EncodeError, expected), (X86EncodeError, expected)]


@pytest.mark.parametrize("field,changed", [("left", "other"), ("width", 8)])
def test_compact_pending_difference_preserves_frozen_value_contract(field, changed):
    record = assembler._PendingDifference(4, "end", "start", 4)
    equivalent = assembler._PendingDifference(offset=4, left="end", right="start", width=4)
    assert record == equivalent
    assert hash(record) == hash(equivalent)
    different = replace(record, **{field: changed})
    assert different != record and getattr(different, field) == changed
    assert not hasattr(record, "__dict__")
    with pytest.raises(FrozenInstanceError):
        setattr(record, field, changed)
    assert (record.offset, record.left, record.right, record.width) == (4, "end", "start", 4)


def test_compact_symbol_metadata_preserves_mutable_fields_and_equality():
    record = assembler._SymbolMeta()
    assert record == assembler._SymbolMeta(False, elf.STT_NOTYPE, None)
    assert not hasattr(record, "__dict__")
    record.global_ = True
    record.type = elf.STT_FUNC
    record.size = 6
    assert record == assembler._SymbolMeta(True, elf.STT_FUNC, 6)
    assert replace(record, size=9) == assembler._SymbolMeta(True, elf.STT_FUNC, 9)
    with pytest.raises(TypeError):
        hash(record)
    with pytest.raises(AttributeError):
        record.unplanned_field = 1


def test_single_pass_releases_each_source_line_after_encoding(monkeypatch):
    text = '''.intel_syntax noprefix
.text
.globl probe
.type probe, @function
probe:
  call external
  jmp .Ldone
.Ldone:
  ret
.size probe, .-probe
.data
payload:
  .quad probe
'''
    expected = elf.emit_relocatable(assembler.assemble_file(text))
    references = []
    class ObservedLine(str):
        pass
    lines = assembler._iter_assembly_lines
    def observed_lines(source):
        for line in lines(source):
            record = ObservedLine(line)
            references.append(weakref.ref(record))
            yield record
            del record
    encode = assembler.encode_instruction
    calls = 0
    def observed_encode(*args, **kwargs):
        nonlocal calls
        # Only the line being encoded is alive; no earlier source line or
        # record of it survives into the rest of the pass.
        assert all(reference() is None for reference in references[:-1])
        calls += 1
        return encode(*args, **kwargs)
    monkeypatch.setattr(assembler, '_iter_assembly_lines', observed_lines)
    monkeypatch.setattr(assembler, 'encode_instruction', observed_encode)
    actual = elf.emit_relocatable(assembler.assemble_file(text))
    assert calls == 3
    assert all(reference() is None for reference in references)
    assert actual == expected


def test_owned_encoding_releases_copied_stack_map_payload(monkeypatch):
    from pcc.backend import x86_64_asm_driver as assembler
    from pcc.backend import self_backend_precise_stackmaps as maps
    from pcc.backend import self_backend_x86_64_linux as emitter
    from pcc.backend.self_backend_parse import parse_self_backend_module
    from pcc.backend.target_objects import emit_indexed_assembly
    from pcc.backend.elf_x86_64 import emit_relocatable
    from tests.python.test_precise_stackmap_abi import _target_final_stackmap_ir

    plans = []
    text = emit_indexed_assembly(
        parse_self_backend_module(_target_final_stackmap_ir('x86_64-unknown-linux-gnu')),
        stack_map_plans_out=plans,
    )
    keywords = {'function_symbol': emitter._asm_symbol, 'block_label': emitter._block_label}
    expected = emit_relocatable(assembler.assemble_file_with_stack_maps(text, plans, **keywords))
    released = []
    payload_sizes = []
    class TrackedPayload(bytes):
        def __del__(self):
            released.append(True)
    build = maps.build_x86_64_stack_map_payload
    def tracked_build(*args, **kwargs):
        packed, relocations = build(*args, **kwargs)
        payload_sizes.append(len(packed))
        return TrackedPayload(packed), relocations
    make_object = assembler.ElfObject
    def checked_object(*args, **kwargs):
        # Only copied immutable section bytes are needed for object validation;
        # the original packed bytes and the consumed _Data must be retired.
        assert released == [True]
        return make_object(*args, **kwargs)
    def checked_bytes(value):
        if isinstance(value, bytearray) and payload_sizes and len(value) == payload_sizes[0]:
            assert released == [True], 'packed payload overlaps its final section copy'
        return bytes(value)
    monkeypatch.setattr(assembler, 'bytes', checked_bytes, raising=False)
    monkeypatch.setattr(maps, 'build_x86_64_stack_map_payload', tracked_build)
    monkeypatch.setattr(assembler, 'ElfObject', checked_object)
    actual = emit_relocatable(assembler.assemble_file_with_stack_maps(text, plans, **keywords))
    assert actual == expected


def test_compact_pending_relocation_preserves_frozen_value_contract():
    record = assembler._PendingRelocation('.text', 4, 'target', 2, -4)
    assert record == assembler._PendingRelocation('.text', 4, 'target', 2, -4)
    assert hash(record) == hash(assembler._PendingRelocation('.text', 4, 'target', 2, -4))
    assert replace(record, addend=8) == assembler._PendingRelocation('.text', 4, 'target', 2, 8)
    assert not hasattr(record, '__dict__')
    with pytest.raises(FrozenInstanceError):
        record.offset = 9
    assert record.offset == 4


def test_undefined_symbol_discovery_does_not_copy_all_defined_names(monkeypatch):
    text = '.intel_syntax noprefix\n.text\n.globl probe\n.type probe, @function\nprobe:\n'
    text += ''.join('.Llabel_' + str(index) + ':\n' for index in range(1024))
    text += '  call external_z\n  ret\n.size probe, .-probe\n.type external_a, @function\n'
    expected = elf.emit_relocatable(assembler.assemble_file(text))
    def bounded_set(values=()):
        # Publication should allocate for the few undefined names, never copy
        # the complete parser metadata or measured-label dictionary into a set.
        if isinstance(values, dict) and len(values) >= 128:
            raise AssertionError('copied complete defined-symbol map of ' + str(len(values)))
        return builtins.set(values)
    monkeypatch.setattr(assembler, 'set', bounded_set, raising=False)
    obj = assembler.assemble_file(text)
    assert [symbol.name for symbol in obj.symbols if symbol.name and symbol.section_index == 0] == [
        'external_a', 'external_z',
    ]
    assert elf.emit_relocatable(obj) == expected


def test_temporary_labels_do_not_allocate_default_symbol_metadata():
    count = 4096
    text = '.intel_syntax noprefix\n.text\n.globl probe\n.type probe, @function\nprobe:\n'
    text += ''.join('.Ltemporary_' + str(index) + ':\n' for index in range(count))
    text += '  ret\n.size probe, .-probe\n'
    sections, _order, symbols, labels = assembler._encode_file(text, assembler._OperandParseCache())
    # Metadata should scale with declarations, while every label must remain
    # available for duplicate checks and final branch/map resolution.
    metadata_count = len(symbols)
    assert metadata_count == 1
    assert set(symbols) == {'probe'}
    assert len(labels) == count + 1
    assert sections['.text'].size == 1
    assert symbols['probe'] == assembler._SymbolMeta(True, elf.STT_FUNC, 1)
    obj = assembler.assemble_file(text)
    assert [symbol.name for symbol in obj.symbols] == ['', 'probe']


@pytest.mark.parametrize('declarations_first', [True, False])
def test_sparse_label_metadata_preserves_declarations_and_publication(declarations_first):
    declarations = '''.type local_fn, @function
.size local_fn, 1
.globl global_fn
.type global_fn, @function
.size global_fn, 1
.globl entry
.type entry, @function
.size sized_target, 8
.type typed_target, @object
.type external_called, @function
.globl external_declared
.type external_typed, @function
'''
    body = '''.text
local_fn:
  ret
global_fn:
  ret
entry:
  call local_fn
  call global_fn
  call external_called
  jmp .Lend
.Lend:
  ret
.size entry, .-entry
.data
plain_target:
  .quad 0
sized_target:
  .quad 0
typed_target:
  .quad 0
references:
  .quad plain_target
  .quad external_data
'''
    text = '.intel_syntax noprefix\n.text\n'
    text += declarations + body if declarations_first else body + declarations
    _sections, _order, metadata, _labels = assembler._encode_file(text, assembler._OperandParseCache())
    assert set(metadata) == {
        'local_fn', 'global_fn', 'entry', 'sized_target', 'typed_target',
        'external_called', 'external_declared', 'external_typed',
    }
    obj = assembler.assemble_file(text)
    symbols = {symbol.name: symbol for symbol in obj.symbols if symbol.name}
    assert set(symbols) == set(metadata) | {'plain_target', 'external_data'}
    assert symbols['local_fn'].binding == elf.STB_LOCAL
    assert symbols['local_fn'].type == elf.STT_FUNC
    assert symbols['local_fn'].size == 1
    assert symbols['global_fn'].binding == elf.STB_GLOBAL
    assert symbols['entry'].size == 21
    assert symbols['sized_target'].size == 8
    for name in ('plain_target', 'sized_target', 'typed_target'):
        assert symbols[name].type == elf.STT_OBJECT
        assert symbols[name].binding == elf.STB_LOCAL
    for name in ('external_called', 'external_declared', 'external_typed', 'external_data'):
        assert symbols[name].section_index == 0
        assert symbols[name].binding == elf.STB_GLOBAL
    assert symbols['external_called'].type == elf.STT_FUNC
    assert symbols['external_typed'].type == elf.STT_FUNC
    text_section = next(section for section in obj.sections if section.name == '.text')
    assert [obj.symbols[relocation.symbol_index].name for relocation in text_section.relocations] == [
        'global_fn', 'external_called',
    ]
    kept = assembler.assemble_file_keeping_labels(text, ('.Lend',))
    assert {symbol.name for symbol in kept.symbols if symbol.name} == set(symbols) | {'.Lend'}
    assert elf.parse_relocatable(elf.emit_relocatable(obj)) == obj


@pytest.mark.parametrize(('body', 'diagnostic'), [
    ('.text\n.Lsame:\n.Lsame:\n', "duplicate assembly label '.Lsame'"),
    ('.text\nsame:\n.data\nsame:\n', "duplicate assembly label 'same'"),
    ('.text\n.size missing, 1\n', "assembly metadata names symbols without definitions: ['missing']"),
    ('.text\n.size missing, .-missing\n', ".size references non-local symbol 'missing'"),
    ('.text\nprobe:\n ret\n.size probe, 1\n.size probe, 2\n', "conflicting .size directives for 'probe'"),
])
def test_sparse_label_metadata_preserves_definition_errors(body, diagnostic):
    with pytest.raises(X86EncodeError) as error:
        assembler.assemble_file('.intel_syntax noprefix\n' + body)
    assert str(error.value) == diagnostic


def test_single_pass_encodes_branches_and_relocations_at_final_offsets(monkeypatch):
    text = '''.intel_syntax noprefix
.text
.globl probe
.type probe, @function
probe:
.Lback:
  call external
  call external
  jmp .Lforward
  jmp .Lforward
.Lforward:
  jne .Lback
  jne .Lback
  ret
.size probe, .-probe
'''
    original = assembler.encode_instruction
    calls = []
    def tracked(line, *, pc, labels, section_name, operand_cache=None):
        calls.append((line, pc, bool(labels)))
        return original(
            line, pc=pc, labels=labels, section_name=section_name,
            operand_cache=operand_cache,
        )
    monkeypatch.setattr(assembler, 'encode_instruction', tracked)
    first = assembler.assemble_file(text)
    section = next(section for section in first.sections if section.name == '.text')
    assert section.data.hex() == (
        'e800000000e800000000e905000000e900000000'
        '0f85e6ffffff0f85e0ffffffc3'
    )
    assert [(rel.offset, first.symbols[rel.symbol_index].name, rel.addend)
            for rel in section.relocations] == [(1, 'external', -4), (6, 'external', -4)]
    # Labels are patched after the pass: no encoding consults a label table.
    assert [pc for line, pc, _labels in calls if line == 'jmp .Lforward'] == [10, 15]
    assert [pc for line, pc, _labels in calls if line == 'jne .Lback'] == [20, 26]
    assert len(calls) == 7 and not any(call[2] for call in calls)
    calls.clear()
    second = assembler.assemble_file(text)
    assert elf.emit_relocatable(second) == elf.emit_relocatable(first)
    assert len(calls) == 7


def test_far_branch_validation_precedes_deferred_zero_fill(monkeypatch):
    # Branches are patched after the pass, and a large zero fill stays a
    # length until then: the far jump fails before 2 GiB are allocated.
    materialized = []
    def checked_bytes(value=b''):
        if isinstance(value, int):
            materialized.append(value)
        return builtins.bytes(value)
    monkeypatch.setattr(assembler, 'bytes', checked_bytes, raising=False)
    text = '''.intel_syntax noprefix
.text
probe:
  jne .Lfar
  jne .Lfar
  .zero 2147483648
.Lfar:
  ret
'''
    with pytest.raises(X86EncodeError, match='outside rel32 range'):
        assembler.assemble_file(text)
    assert materialized == []


def test_sizing_memo_repeated_invalid_instruction_still_fails_first(monkeypatch):
    original = assembler.encode_instruction
    invalid_calls = []
    def tracked(line, **kwargs):
        if line == 'vzeroupper':
            invalid_calls.append(line)
        return original(line, **kwargs)
    monkeypatch.setattr(assembler, 'encode_instruction', tracked)
    text = '.intel_syntax noprefix\n.text\nprobe:\n ret\n ret\n vzeroupper\n vzeroupper\n'
    for expected_count in (1, 2):
        with pytest.raises(X86EncodeError, match='not proven'):
            assembler.assemble_file(text)
        assert len(invalid_calls) == expected_count


@pytest.mark.parametrize('instructions', [
    ['mov eax, 1', 'mov eax, 2', 'mov eax, 1', 'mov eax, 3', 'mov eax, 2'],
    ['call α', 'call β', 'call α', 'call γ', 'call α'],
    ['call α', 'call α', 'ret', 'ret'],
])
def test_single_pass_encodes_each_line_once_including_repeats_and_unicode(
    monkeypatch, instructions,
):
    original = assembler.encode_instruction
    calls = []
    def tracked(line, **kwargs):
        calls.append(line)
        return original(line, **kwargs)
    monkeypatch.setattr(assembler, 'encode_instruction', tracked)
    text = '.intel_syntax noprefix\n.text\n' + '\n'.join(instructions) + '\n'
    sections, _order, _symbols, _labels = assembler._encode_file(text, assembler._OperandParseCache())
    assert calls == instructions
    expected_size = sum(len(original(line, pc=0, labels={}, section_name='.text').code)
                        for line in instructions)
    assert sections['.text'].size == expected_size


def test_oversized_instruction_result_is_not_retained_after_placement(monkeypatch):
    giant = 'call ' + 'x' * (65536 + 17)
    original = assembler.encode_instruction
    giant_results = []
    giant_calls = []
    def tracked(line, **kwargs):
        if line == 'mov eax, 42':
            assert giant_results and all(ref() is None for ref in giant_results)
        result = original(line, **kwargs)
        if line == giant:
            giant_calls.append(line)
            giant_results.append(weakref.ref(result))
        return result
    monkeypatch.setattr(assembler, 'encode_instruction', tracked)
    text = '.intel_syntax noprefix\n.text\nret\n' + giant + '\nret\nret\n' + giant + '\nret\nmov eax, 42\n'
    sections, _order, _symbols, _labels = assembler._encode_file(text, assembler._OperandParseCache())
    assert sections['.text'].size == 19
    assert len(giant_calls) == 2
    assert all(ref() is None for ref in giant_results)


def test_blank_lines_and_string_subclass_sources_never_become_memo_keys(monkeypatch):
    class ObservedText(str):
        def __hash__(self):
            raise AssertionError('source text must not become a memo key')
    text = ObservedText('.intel_syntax noprefix\n.text\n\n   \n\t\nret\nret\n')
    original = assembler.encode_instruction
    blank_calls = []
    def tracked(line, **kwargs):
        if not line.strip():
            blank_calls.append(line)
        return original(line, **kwargs)
    monkeypatch.setattr(assembler, 'encode_instruction', tracked)
    obj = assembler.assemble_file(text)
    assert obj.sections[0].data == b'\xc3\xc3'
    assert blank_calls == []


# native x86_64 Linux execution required
@pytest.mark.pcc_gate(probe=lambda: sys.platform == 'linux' and platform.machine().lower() in ('x86_64', 'amd64'))
def test_sizing_memo_owned_native_calls_and_backward_branch_execute(tmp_path):
    import platform
    import subprocess
    import sys

    entry = assembler.assemble_file('''.intel_syntax noprefix
.text
.globl _start
.type _start, @function
_start:
  xor edi, edi
  call increment
  call increment
  mov ecx, 2
.Lloop:
  add edi, 1
  sub ecx, 1
  jne .Lloop
  add edi, 38
  mov eax, 60
  syscall
.size _start, .-_start
''')
    function = assembler.assemble_file('''.intel_syntax noprefix
.text
.globl increment
.type increment, @function
increment:
  add edi, 1
  ret
.size increment, .-increment
''')
    image = elf.link_static_executable([entry, function])
    elf.parse_static_executable(image)
    executable = tmp_path / 'sizing-memo-native'
    executable.write_bytes(image)
    executable.chmod(0o755)
    completed = subprocess.run([str(executable)], capture_output=True, timeout=5)
    assert completed.returncode == 42, (completed.returncode, completed.stdout, completed.stderr)


def test_single_pass_payload_chunks_bound_storage_and_preserve_order():
    text, count = _many_instruction_lines()
    sections, order, _symbols, labels = assembler._encode_file(text, assembler._OperandParseCache())
    assert order == ['.text']
    payload = sections['.text'].payload
    chunk = assembler._SECTION_PAYLOAD_CHUNK_BYTES
    assert payload.chunks and all(len(run) == chunk for run in payload.chunks)
    assert 0 <= payload.pending_size < chunk
    assert sections['.text'].size == count + 1
    assert labels['probe'] == ('.text', 0)
    assert payload.finish() == b'\x90' * count + b'\xc3'


@pytest.mark.parametrize('text', ['\n'.join(VALID_LINES), _mixed_lines(VALID_LINES)])
def test_single_pass_object_matches_two_pass_driver_on_same_input(text):
    from tests.python.test_x86_64_parse_buffer_publication import _two_pass

    actual = elf.emit_relocatable(assembler.assemble_file(text))
    assert actual == elf.emit_relocatable(_two_pass().assemble_file(text))


def test_single_pass_retires_payload_chunks_when_publishing(monkeypatch):
    monkeypatch.setattr(assembler, '_SECTION_PAYLOAD_CHUNK_BYTES', 5)
    text = '.intel_syntax noprefix\n.text\nprobe:\n' + '  nop\n' * 19 + '  ret\n'
    finished = []
    real_finish = assembler._finish_section
    def tracked_finish(section, labels, symbols):
        assert len(section.payload.chunks) == 4 and not section.payload.pending
        result = real_finish(section, labels, symbols)
        assert section.payload.chunks == [] and section.payload.pending == []
        finished.append(section.name)
        return result
    monkeypatch.setattr(assembler, '_finish_section', tracked_finish)
    obj = assembler.assemble_file(text)
    assert finished == ['.text']
    assert obj.sections[0].data == b'\x90' * 19 + b'\xc3'


def test_reference_validation_precedes_deferred_zero_fills(monkeypatch):
    materialized = []
    def checked_bytes(value=b''):
        if isinstance(value, int):
            materialized.append(value)
        return builtins.bytes(value)
    monkeypatch.setattr(assembler, 'bytes', checked_bytes, raising=False)
    text = '.intel_syntax noprefix\n.text\n.Lstart:\n  .zero 70000\n.Lend:\n  .byte .Lend-.Lstart\n  ret\n'
    with pytest.raises(X86EncodeError, match='does not fit 8 bits'):
        assembler.assemble_file(text)
    assert materialized == []
    obj = assembler.assemble_file(text.replace('.byte', '.long'))
    assert materialized == [70000]
    assert obj.sections[0].data == bytes(70000) + (70000).to_bytes(4, 'little') + b'\xc3'


def test_local_branch_patches_preserve_signed_rel32_values():
    text = '''.intel_syntax noprefix
.text
.Lself:
  jmp .Lself
  jmp .Lnext
.Lnext:
  jne .Lself
  call .Lnext
'''
    obj = assembler.assemble_file(text)
    assert obj.sections[0].data == bytes.fromhex(
        'e9 fb ff ff ff' 'e9 00 00 00 00' '0f 85 f0 ff ff ff' 'e8 f5 ff ff ff'
    )
    assert obj.sections[0].relocations == ()


# Runs the gated native x86_64 Linux witness below.
@pytest.mark.pcc_gate(probe=lambda: sys.platform == 'linux' and platform.machine().lower() in ('x86_64', 'amd64'))
def test_small_payload_chunks_native_calls_and_branches_execute(tmp_path, monkeypatch):
    # Exercise many small payload chunks in the existing owned-link witness,
    # including external calls and a patched backward conditional branch.
    monkeypatch.setattr(assembler, '_SECTION_PAYLOAD_CHUNK_BYTES', 5)
    test_sizing_memo_owned_native_calls_and_backward_branch_execute(tmp_path)


@pytest.mark.parametrize('run_length', [1, 2, 3, 4, 5, 1024, 1025, 1026])
def test_label_dense_sections_match_two_pass_driver(run_length):
    from tests.python.test_x86_64_parse_buffer_publication import _two_pass

    text = '.intel_syntax noprefix\n.text\n'
    for index in range(12):
        text += '.Lblock' + str(index) + ':\n' + '  nop\n' * run_length
        text += '  jmp .Lblock' + str((index + 5) % 12) + '\n'
    sections, _order, _symbols, labels = assembler._encode_file(text, assembler._OperandParseCache())
    assert len(labels) == 12 and len(sections['.text'].branches) == 12
    expected = elf.emit_relocatable(_two_pass().assemble_file(text))
    assert elf.emit_relocatable(assembler.assemble_file(text)) == expected
