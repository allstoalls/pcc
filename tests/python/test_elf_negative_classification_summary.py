"""Small normal-CI controls for the staged negative proof; never run ELF code.

No timing comparison, process, compiler, or profiler. These call the real host
parser/linker only in normal CI, never in source/fake local qualification.
"""

from dataclasses import replace
import struct
import weakref

import pytest

from pcc.backend import elf_x86_64 as elf
from pcc.backend import owned_elf_inputs as inputs
from pcc.backend.precise_stackmap import (
    ARCH_AARCH64, FunctionStackMap, PreciseStackMap, encode_stack_map,
    function_address_offsets, function_id,
)
from tests.elf_negative_classification_preimage import original_linker, original_validator
from tests.python.test_elf_packed_link_classification import (
    BASE, TARGET, _exception_chain, _fixture, _outcome,
)


class Evidence:
    def __init__(self, fail=False):
        self.rows = []
        self.fail = fail

    def begin(self, phase):
        return 0

    def complete(self, phase, started):
        pass

    def elf_classification_skips(self, *row):
        self.rows.append(row)
        if self.fail:
            raise OSError("authored evidence sink failure")
        return True


def _stage(tmp_path, obj, name="input.o"):
    path = tmp_path / name
    path.write_bytes(elf.emit_relocatable(obj))
    store = inputs.ElfInputStore(TARGET)
    return path, store, store.stage(str(path))


def _negative_object(count=1):
    sections = [elf.ElfSection(".text", 1, 6, 4, struct.pack("<I", 0xD65F03C0))]
    sections.extend(elf.ElfSection(".data." + str(index), 1, 3, 8, bytes(8),
                                  relocations=(elf.ElfRelocation(0, 1, 257),))
                    for index in range(count))
    return elf.ElfObject(tuple(sections), (
        elf.ElfSymbol.null(), elf.ElfSymbol("_start", 1, 0, 4, 1, elf.STT_FUNC),
    ), elf.EM_AARCH64)


def test_negative_summary_mixed_image_and_real_stackmap_match(tmp_path):
    obj, _text_rows, _data_rows = _fixture()
    payload = encode_stack_map(PreciseStackMap(ARCH_AARCH64, (
        FunctionStackMap(function_id("_start"), 0, 24, 0, ()),
    )))
    stackmap = elf.ElfSection(".pcc_stackmaps", 1, 2, 8, payload,
                             relocations=(elf.ElfRelocation(function_address_offsets(payload)[0], 1, 257),))
    obj = replace(obj, sections=obj.sections + (stackmap,))
    _path, store, staged = _stage(tmp_path, obj)
    proof = store.sources[0].classification_proof
    assert proof.section_bits == (False, True, True)
    evidence = Evidence()
    before = _outcome(original_linker(elf), staged, _input_store=store)
    after = _outcome(elf._link_static_executable_image, staged, _input_store=store,
                     coordinator_timing=evidence)
    assert after == before and after[0] == "image"
    assert evidence.rows[-1] == (3, 3, 2, 2)
    assert store.current is store.current_classification_payload is None
    store.verify()


def test_negative_summary_all_supported_types_and_default_parser_contract(tmp_path):
    kinds = (0, 257, 260, 258, 261, 274, 275, 277, 278, 282, 283, 284,
             285, 286, 299, 311, 312, 541, 542, 549, 551)
    obj = _negative_object(0)
    sections = obj.sections + tuple(
        elf.ElfSection(".kind." + str(kind), 1, 2, 8, bytes(8),
                       relocations=(elf.ElfRelocation(0, 1, kind, -(1 << 63)),))
        for kind in kinds)
    obj = replace(obj, sections=sections)
    _path, store, _staged = _stage(tmp_path, obj)
    assert store.sources[0].classification_proof.section_bits == (
        None, *(kind not in (283, 311, 312, 541, 542) for kind in kinds))
    encoded = elf.emit_relocatable(obj)
    ordinary, ranges = elf._parse_relocatable_indexed(encoded)
    assert len(ranges) == len(sections)
    assert all(section.relocations._classification_candidate is None
               for section in ordinary.sections if type(section.relocations) is elf._PackedElfRelocations)
    assert elf._validate_object(ordinary) is None
    summaries = []
    reusable, _ranges = elf._parse_relocatable_indexed(
        elf.emit_relocatable(_negative_object()), _classification_summaries=summaries)
    packed = reusable.sections[1].relocations
    cell = packed._classification_candidate
    assert cell == [True]
    class Offset(int):
        pass
    object.__setattr__(packed, "offset", Offset(packed.offset))
    assert elf._validate_object(reusable) is None and cell == [None]
    object.__setattr__(packed, "offset", int(packed.offset))
    assert elf._validate_object(reusable) is None and cell == [True]
    x86 = elf.ElfObject((elf.ElfSection(".text", 1, 6, 1, b"\xc3"),),
                       (elf.ElfSymbol.null(), elf.ElfSymbol("_start", 1, 0, 1, 1, elf.STT_FUNC)))
    _path, store, _staged = _stage(tmp_path, x86, "x86.o")
    assert store.sources[0].classification_proof is None


def test_negative_summary_publishes_only_after_full_symbol_and_stackmap_validation(monkeypatch):
    def rejected(data):
        results = []
        for validate in (original_validator(elf), elf._validate_object):
            summaries = []
            with monkeypatch.context() as patch:
                patch.setattr(elf, "_validate_object", validate)
                with pytest.raises(elf.ElfError) as caught:
                    elf._parse_relocatable_indexed(data, _classification_summaries=summaries)
            assert summaries == []
            results.append(_exception_chain(caught.value))
        assert results[0] == results[1]
        return str(caught.value)

    obj = _negative_object()
    encoded = bytearray(elf.emit_relocatable(obj))
    header = elf._ELF_HEADER.unpack_from(encoded)
    headers = [elf._SECTION_HEADER.unpack_from(encoded, header[6] + index * header[11])
               for index in range(header[12])]
    symtab = next(row for row in headers if row[1] == elf.SHT_SYMTAB)
    encoded[symtab[4] + elf._SYMBOL.size + 4] = 0x31  # Invalid binding, after row checks.
    assert "unsupported binding" in rejected(bytes(encoded))
    payload = encode_stack_map(PreciseStackMap(ARCH_AARCH64, ()))
    obj = replace(obj, sections=obj.sections + (elf.ElfSection(".pcc_stackmaps", 1, 2, 8, payload),))
    encoded = bytearray(elf.emit_relocatable(obj))
    parsed, ranges = elf._parse_relocatable_indexed(bytes(encoded))
    encoded[ranges[-1][0]] ^= 1
    assert "invalid .pcc_stackmaps" in rejected(bytes(encoded))


def test_negative_summary_same_point_source_identity_errors_match(tmp_path):
    obj, _text_rows, _data_rows = _fixture()
    for mode in ("append", "truncate", "identity"):
        path, store, staged = _stage(tmp_path, obj, mode + ".o")
        original = path.read_bytes()
        changed = (original + b"x" if mode == "append" else original[:-1]
                   if mode == "truncate" else original[:-1] + bytes((original[-1] ^ 1,)))
        path.write_bytes(changed)
        before = _outcome(original_linker(elf), staged, _input_store=store)
        after = _outcome(elf._link_static_executable_image, staged, _input_store=store)
        assert after == before and after[0] is elf.ElfError
        assert {"append": "size changed", "truncate": "truncated", "identity": "identity changed"}[mode] in after[1]


def test_negative_summary_requires_owner_range_proof_and_loaded_payload(tmp_path, monkeypatch):
    _path, store, owner = _stage(tmp_path, _negative_object())
    section = owner.sections[1]
    view = section.relocations
    check = lambda: inputs._classification_is_empty(store, owner, 2, section, view)
    assert check()
    assert not inputs._classification_is_empty(store, owner, 2, section, replace(view, size=0))
    class PackedSubclass(elf._PackedElfRelocations):
        pass
    assert not inputs._classification_is_empty(store, owner, 2, section,
                                               PackedSubclass(view.payload, view.offset, view.size))
    borrowed = elf.ElfObject(owner.sections, owner.symbols, owner.machine)
    assert not inputs._classification_is_empty(store, borrowed, 2, section, view)
    source = store.sources[0]
    store.sources[0] = replace(source, classification_proof=replace(source.classification_proof))
    assert not check()
    store.sources[0] = source
    current = store.current
    store.current = bytes(bytearray(current))
    assert not check()
    store.current = current
    assert check()
    changed = replace(source.classification_proof, section_ranges=tuple((*row[:3], row[3] + 24)
                                                                       for row in source.section_ranges))
    store.sources[0] = replace(source, classification_proof=changed)
    assert not check()
    store.sources[0] = source
    assert not elf._staged_classification_is_empty(store, [], 0, 2, section, view)
    assert not elf._staged_classification_is_empty(store, (owner,), 0, 2, section, view)
    with monkeypatch.context() as patch:
        def unavailable(*args):
            raise MemoryError("optional proof lookup")
        patch.setattr(inputs, "_classification_is_empty", unavailable)
        assert not elf._staged_classification_is_empty(store, [owner], 0, 2, section, view)
    store.clear()
    assert not check() and store.current_classification_payload is None


def test_negative_summary_retains_prior_custom_row_release_order(tmp_path, monkeypatch):
    def run(link, name):
        _path, store, staged = _stage(tmp_path, _negative_object(2), name)
        events = []
        original_post = elf._PackedElfRelocations.__post_init__

        def post(view):
            original_post(view)
            events.append("view")

        class Row:
            offset, symbol_index, type, addend = 0, 0, 0, 0

            def __del__(self):
                events.append("row released")

        class Rows:
            def __iter__(self):
                yield Row()

        custom = elf.ElfObject((elf.ElfSection(".custom", 1, 2, 1, b"x"),),
                               (elf.ElfSymbol.null(),), elf.EM_AARCH64)
        object.__setattr__(custom.sections[0], "relocations", Rows())
        evidence = Evidence()
        with monkeypatch.context() as patch:
            patch.setattr(elf._PackedElfRelocations, "__post_init__", post)
            result = bytes(link([custom, staged], base_address=BASE, _input_store=store,
                                coordinator_timing=evidence))
        assert events.index("view") < events.index("row released")
        assert events[:3] == ["view", "row released", "view"]
        return result, evidence.rows

    before, _old_rows = run(original_linker(elf), "old.o")
    after, rows = run(elf._link_static_executable_image, "new.o")
    assert before == after and rows[-1] == (4, 4, 1, 1)


def test_negative_summary_layout_failure_does_not_retain_temporary_view(tmp_path, monkeypatch):
    def run(link, name):
        _path, store, staged = _stage(tmp_path, _negative_object(), name)
        object.__setattr__(staged.sections[1], "flags", 7)
        views = []
        original_post = elf._PackedElfRelocations.__post_init__

        def post(view):
            original_post(view)
            views.append(weakref.ref(view))

        with monkeypatch.context() as patch:
            patch.setattr(elf._PackedElfRelocations, "__post_init__", post)
            with pytest.raises(elf.ElfError) as caught:
                link([staged], base_address=BASE, _input_store=store)
        assert caught.value.__traceback__ is not None
        assert views and all(view() is None for view in views)
        return type(caught.value), str(caught.value)

    assert run(original_linker(elf), "old.o") == run(elf._link_static_executable_image, "new.o")


def test_negative_summary_bounded_section_evidence_and_failure_stops(tmp_path):
    _path, store, staged = _stage(tmp_path, _negative_object(17))
    expected = _outcome(original_linker(elf), staged, _input_store=store)
    for evidence in (Evidence(), Evidence(fail=True)):
        actual = _outcome(elf._link_static_executable_image, staged, _input_store=store,
                          coordinator_timing=evidence)
        assert actual == expected and actual[0] == "image"
        if evidence.fail:
            assert len(evidence.rows) == 1
        else:
            assert len(evidence.rows) == 8
            assert evidence.rows[-1] == (18, 18, 17, 17)
            assert [row[0] for row in evidence.rows] == sorted({row[0] for row in evidence.rows})
