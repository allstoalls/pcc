"""Branch syntax may be reused, but never its PC/symbol-dependent encoding."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
import weakref

import pytest

from pcc.backend import x86_64_asm_driver as assembler
from pcc.backend import x86_64_encode as encoder
from pcc.backend.elf_x86_64 import emit_relocatable
from pcc.backend.target_objects import emit_indexed_assembly, encode_assembly_object
from tests.python.test_x86_64_emission_lifetime import ROOTED_PROGRAM, TARGET, _indexed_module


def _encode(line, cache, pc=0, labels=None, section=".text"):
    return encoder.encode_instruction(
        line, pc=pc, labels={} if labels is None else labels,
        section_name=section, operand_cache=cache,
    )


def _charge(line, syntax):
    return encoder._BRANCH_SYNTAX_ENTRY_BYTES + 4 * (
        len(line) + len(syntax.mnemonic) + len(syntax.target.name)
    )


def _check_bounds(cache):
    assert cache.branch_syntax_bytes == sum(
        _charge(line, syntax) for line, syntax in cache.branch_syntaxes.items()
    )
    assert cache.instruction_encoding_bytes == sum(
        4 * len(line) + len(code) for line, code in cache.instruction_encodings.items()
    )
    assert len(cache.branch_syntaxes) + len(cache.instruction_encodings) <= encoder._INSTRUCTION_ENCODING_CACHE_MAX_ENTRIES
    assert cache.branch_syntax_bytes + cache.instruction_encoding_bytes <= encoder._INSTRUCTION_ENCODING_CACHE_MAX_BYTES
    assert not cache.branch_syntaxes.keys() & cache.instruction_encodings.keys()


@pytest.mark.parametrize("mnemonic", ["call", "jmp"] + ["j" + name for name in encoder._SET_CONDITIONS])
def test_branch_syntax_rechecks_all_pc_label_section_and_rel32_contexts(mnemonic):
    cache = encoder._OperandParseCache()
    line = mnemonic + " target"
    contexts = (
        (0, {}, ".text"), (127, {}, ".text"),
        (17, {"target": (".text", 4096)}, ".text"),
        (200, {"target": (".data", 8)}, ".text"),
        (200, {"target": (".data", 8)}, ".data"),
        (1 << 40, {"target": (".text", 0)}, ".text"),
        (8, {"target": (".text", 99)}, ".text"),
    )
    first = _encode(line, cache)
    syntax = cache.branch_syntaxes[line]
    assert type(syntax.target) is encoder._Target
    with pytest.raises(FrozenInstanceError):
        syntax.mnemonic = "nop"
    with pytest.raises(FrozenInstanceError):
        syntax.target.name = "wrong"
    for pc, labels, section in contexts:
        try:
            expected = _encode(line, None, pc, labels, section)
        except encoder.X86EncodeError as error:
            with pytest.raises(type(error)) as observed:
                _encode(line, cache, pc, labels, section)
            assert str(observed.value) == str(error)
        else:
            actual = _encode(line, cache, pc, labels, section)
            assert actual == expected
            assert actual is not first
        assert cache.branch_syntaxes[line] is syntax
        assert line not in cache.instruction_encodings
        _check_bounds(cache)


@pytest.mark.parametrize("line", ["call r11", "jmp rax", "  JNE   target  ", "jmp 目标"])
def test_indirect_case_whitespace_and_unicode_keep_the_original_encoder(line):
    cache = encoder._OperandParseCache()
    for pc in (0, 17, 1 << 40):
        assert _encode(line, cache, pc) == _encode(line, None, pc)
    assert line in cache.branch_syntaxes
    _check_bounds(cache)


def test_small_working_set_measure_and_final_reuses_split_but_reencodes_every_branch(monkeypatch):
    cache = encoder._OperandParseCache()
    splits = []
    branches = []
    original_split = encoder._split_operands
    original_branch = encoder._branch

    def split(text):
        splits.append(text)
        return original_split(text)

    def branch(mnemonic, operands, **kwargs):
        assert type(operands) is list
        branches.append((mnemonic, kwargs["pc"], dict(kwargs["labels"]), kwargs["section_name"]))
        return original_branch(mnemonic, operands, **kwargs)

    monkeypatch.setattr(encoder, "_split_operands", split)
    monkeypatch.setattr(encoder, "_branch", branch)
    lines = ("call external", "jmp target", "jne target")
    for line in lines:
        _encode(line, cache)
    for pc in range(20):
        for line in lines:
            _encode(line, cache, pc, {"target": (".text", 512)})
    assert splits == ["external", "target", "target"]
    assert len(branches) == 63
    assert branches[-1] == ("jne", 19, {"target": (".text", 512)}, ".text")
    _check_bounds(cache)


@pytest.mark.parametrize("line", ["jmp", "jmp r8d", "jne rax", "call 4", "jmp a, b", "loop target", "jmp [rbp + rax*3]"])
def test_invalid_branches_are_not_retained_and_repeat_the_same_error(line):
    cache = encoder._OperandParseCache()
    for _ in range(2):
        with pytest.raises(encoder.X86EncodeError) as expected:
            _encode(line, None)
        with pytest.raises(encoder.X86EncodeError) as actual:
            _encode(line, cache)
        assert str(actual.value) == str(expected.value)
        assert cache.branch_syntaxes == {}
        assert cache.branch_syntax_bytes == 0


@pytest.mark.parametrize("entries,byte_limit", [(3, 100_000), (100, 2300)])
def test_mixed_churn_shares_old_bounds_and_preserves_byte_cache_priority(monkeypatch, entries, byte_limit):
    monkeypatch.setattr(encoder, "_INSTRUCTION_ENCODING_CACHE_MAX_ENTRIES", entries)
    monkeypatch.setattr(encoder, "_INSTRUCTION_ENCODING_CACHE_MAX_BYTES", byte_limit)
    actual = encoder._OperandParseCache()
    byte_only = encoder._OperandParseCache()
    for index in range(40):
        for line in ("mov rax, " + str(index % 5), "call target_" + str(index), "jne local_" + str(index)):
            encoded = _encode(line, actual, index)
            assert encoded == _encode(line, None, index)
            if line.startswith("mov"):
                _encode(line, byte_only, index)
            assert actual.instruction_encodings == byte_only.instruction_encodings
            assert actual.instruction_encoding_bytes == byte_only.instruction_encoding_bytes
            _check_bounds(actual)
    _encode("call retained", actual)
    old = dict(actual.branch_syntaxes)
    _encode("call " + "x" * byte_limit, actual)
    assert actual.branch_syntaxes == old
    _check_bounds(actual)


@pytest.mark.parametrize("entries,byte_limit", [(0, 4096), (4, 0), (4, 32)])
def test_disabled_or_tiny_shared_budget_admits_no_syntax(monkeypatch, entries, byte_limit):
    monkeypatch.setattr(encoder, "_INSTRUCTION_ENCODING_CACHE_MAX_ENTRIES", entries)
    monkeypatch.setattr(encoder, "_INSTRUCTION_ENCODING_CACHE_MAX_BYTES", byte_limit)
    cache = encoder._OperandParseCache()
    for pc in (0, 99):
        assert _encode("jmp target", cache, pc) == _encode("jmp target", None, pc)
    assert cache.branch_syntaxes == {}
    assert cache.branch_syntax_bytes == 0


def test_uncached_probe_does_not_double_account_syntax():
    cache = encoder._OperandParseCache()
    for pc in (0, 10, 50):
        encoder._encode_instruction_uncached("jmp target", pc=pc, labels={}, section_name=".text", operand_cache=cache)
        _check_bounds(cache)
    assert len(cache.branch_syntaxes) == 1


def test_nonexact_string_cache_and_operand_protocols_are_not_retained():
    class Hostile(str):
        def __hash__(self):
            raise AssertionError("cache hashed a string subclass")

    class Cache(encoder._OperandParseCache):
        def parse(self, text):
            self.calls += 1
            return super().parse(text)

    class Target(encoder._Target):
        pass

    class Duck:
        def __init__(self):
            self.calls = 0

        def parse(self, text):
            self.calls += 1
            return encoder._parse_encoding_operand(text)

    exact = encoder._OperandParseCache()
    _encode(Hostile("jmp target"), exact)
    assert not exact.branch_syntaxes
    subclass = Cache()
    subclass.calls = 0
    for pc in (0, 20):
        assert _encode("jmp target", subclass, pc) == _encode("jmp target", None, pc)
    assert subclass.calls == 2
    assert not subclass.branch_syntaxes
    duck = Duck()
    for pc in (0, 20):
        assert _encode("jmp target", duck, pc) == _encode("jmp target", None, pc)
    assert duck.calls == 2
    exact.remember_branch_syntax("jmp target", "jmp", [Target("target")])
    assert not exact.branch_syntaxes


@pytest.mark.parametrize("line", [
    "mov DWORD PTR value[rip], 9", "mov rax, QWORD PTR slot@gottpoff[rip]",
    "mov eax, OFFSET FLAT:tls@TPOFF", "lock xadd QWORD PTR value[rip], rax",
    "fchs", "fxch st(1)",
])
def test_other_relocation_and_prefix_paths_stay_outside_branch_cache(line):
    cache = encoder._OperandParseCache()
    for pc in (0, 127, 1000):
        assert _encode(line, cache, pc) == _encode(line, None, pc)
    assert not cache.branch_syntaxes


def test_real_assembler_file_reuses_repeated_branch_syntax_and_releases_cache(monkeypatch):
    # The file is encoded in one pass, so syntax is reused by a repeated
    # branch line (runtime calls, shared landing jumps), never by a re-encode.
    source = '''.intel_syntax noprefix
.text
.globl probe
.type probe, @function
probe:
  call external
  jne .Lend
  jmp .Lend
  call external
  jne .Lend
  jmp .Lend
.Lend:
  ret
.size probe, .-probe
'''
    references = []
    hit_count = 0
    original_lookup = encoder._OperandParseCache.branch_syntax

    def lookup(cache, line):
        nonlocal hit_count
        if not references:
            references.append(weakref.ref(cache))
        result = original_lookup(cache, line)
        hit_count += result is not None
        return result

    monkeypatch.setattr(encoder._OperandParseCache, "branch_syntax", lookup)
    cached = emit_relocatable(assembler.assemble_file(source))
    assert hit_count == 3
    assert all(reference() is None for reference in references)
    monkeypatch.setattr(encoder._OperandParseCache, "remember_branch_syntax", lambda *_args: None)
    assert emit_relocatable(assembler.assemble_file(source)) == cached


def test_production_indexed_packed_object_is_byte_identical_without_syntax_cache(tmp_path, monkeypatch):
    plans = []
    assembly = emit_indexed_assembly(_indexed_module(tmp_path, ROOTED_PROGRAM), stack_map_plans_out=plans)
    cached = encode_assembly_object(assembly, TARGET, stack_map_plans=plans)
    monkeypatch.setattr(encoder._OperandParseCache, "remember_branch_syntax", lambda *_args: None)
    oracle = encode_assembly_object(assembly, TARGET, stack_map_plans=plans)
    assert cached == oracle
    (tmp_path / "cached.pco").write_bytes(cached)
    (tmp_path / "uncached.pco").write_bytes(oracle)
