"""Small host-only relocation/layout contracts; never execute an ELF image.

Huge-section cases are scalar address plans. Real compilation, ABI execution,
Stage1, and its time/RSS limits remain separate normal-CI qualification.
"""

from dataclasses import replace
import struct

import pytest

from pcc.backend import elf_aarch64_relocations as arm
from pcc.backend import elf_x86_64 as elf


BL = 0x94000000
B = 0x14000000
RET = 0xD65F03C0
NOP = 0xD503201F
BASE = 0x40000000
FAR = 0x123456788


def _words(*words):
    return b"".join(struct.pack("<I", word) for word in words)


def _branch_target(image, offset, place):
    word = struct.unpack_from("<I", image, offset)[0]
    immediate = word & 0x03FFFFFF
    if immediate & (1 << 25):
        immediate -= 1 << 26
    return place + immediate * 4


def _object(target=FAR, *, addend=0, kind=283, symbol_type=elf.STT_FUNC,
            word=BL, align=8):
    return elf.ElfObject(
        (elf.ElfSection(".text", 1, 6, align, _words(word, RET),
                        relocations=(elf.ElfRelocation(0, 2, kind, addend),)),),
        (elf.ElfSymbol.null(),
         elf.ElfSymbol("_start", 1, 0, 8, elf.STB_GLOBAL, elf.STT_FUNC),
         elf.ElfSymbol("target", elf.SHN_ABS, target, 0,
                       elf.STB_GLOBAL, symbol_type)),
        elf.EM_AARCH64,
    )


def _link(obj, base=BASE):
    image = elf.link_static_executable([obj], base_address=base)
    entry = elf.parse_static_executable(image)["entry"]
    return image, entry - base, entry


@pytest.mark.parametrize("delta", [-(1 << 27), (1 << 27) - 4, -4, 0, 4])
def test_call26_direct_signed_boundaries_keep_opcode_and_unused_slots(delta):
    # One key reserves 24 bytes before this eight-byte section.
    place = BASE + 0x1018
    image, offset, entry = _link(_object(place + delta))
    assert entry == place
    assert _branch_target(image, offset, entry) == place + delta
    assert struct.unpack_from("<I", image, offset)[0] & 0xFC000000 == BL
    assert image[0x1008:0x1018] == bytes(16)
    assert image[offset + 4:offset + 8] == _words(RET)


@pytest.mark.parametrize("delta", [-(1 << 27) - 4, 1 << 27])
@pytest.mark.parametrize("addend", [-12, 0, 20])
def test_call26_one_instruction_overflow_uses_full_effective_target(delta, addend):
    place = BASE + 0x1018
    target = place + delta
    image, offset, entry = _link(_object(target - addend, addend=addend))
    veneer_address = _branch_target(image, offset, entry)
    assert veneer_address == BASE + 0x1008
    assert struct.unpack_from("<IIQ", image, 0x1008) == (
        0x58000050, 0xD61F0200, target,
    )
    assert struct.unpack_from("<I", image, offset)[0] & 0xFC000000 == BL
    # BR does not write LR: the BL at the original site still supplies P+4.
    assert image[offset + 4:offset + 8] == _words(RET)
    assert len(image) == 0x1038


@pytest.mark.parametrize("target", [1 << 32, (1 << 64) - 4])
def test_veneer_literal_is_unsigned_full_64_bits(target):
    image, offset, entry = _link(_object(target))
    veneer = _branch_target(image, offset, entry) - BASE
    assert struct.unpack_from("<Q", image, veneer + 8)[0] == target


@pytest.mark.parametrize("target,addend", [(-4, 0), (1 << 64, 0),
                                          ((1 << 64) - 4, 4), (0, -4)])
def test_far_call_effective_address_overflow_is_not_rescued(target, addend):
    with pytest.raises(elf.ElfError, match="outside unsigned 64-bit range"):
        _link(_object(target, addend=addend))


@pytest.mark.parametrize("word", [B, NOP, 0, 0xD63F0200])
def test_far_call_rejects_non_bl_opcode(word):
    with pytest.raises(elf.ElfError, match="requires a BL instruction"):
        _link(_object(word=word))


@pytest.mark.parametrize("target,addend", [(FAR + 1, 0), (FAR, 2)])
def test_far_call_rejects_original_target_misalignment(target, addend):
    with pytest.raises(elf.ElfError, match="unaligned AArch64 branch target"):
        _link(_object(target, addend=addend))


@pytest.mark.parametrize("offset,place,diagnostic", [
    (-4, BASE, "outside image"),
    (32, BASE, "outside image"),
    (2, BASE, "unaligned AArch64 instruction"),
    (0, BASE + 2, "unaligned AArch64 branch"),
])
def test_original_site_validation_precedes_veneer_write(offset, place, diagnostic):
    image = bytearray(_words(BL) + bytes(28))
    original = bytes(image)
    with pytest.raises(ValueError, match=diagnostic):
        arm.apply_call26_with_veneer(image, offset, FAR, place, (16,))
    assert bytes(image) == original


def test_reserved_slot_bounds_are_checked_before_mutation():
    image = bytearray(_words(BL) + bytes(28))
    original = bytes(image)
    with pytest.raises(ValueError, match="invalid AArch64 reserved veneer slot"):
        arm.apply_call26_with_veneer(image, 0, FAR, BASE, (24,))
    assert bytes(image) == original


@pytest.mark.parametrize("kind,symbol_type,word", [
    (282, elf.STT_FUNC, B),
    (283, elf.STT_NOTYPE, BL),
    (283, elf.STT_SECTION, BL),
    (283, elf.STT_OBJECT, BL),
])
def test_unproven_far_branch_shapes_keep_signed26_failure(kind, symbol_type, word):
    with pytest.raises(elf.ElfError, match="signed_bits=26"):
        _link(_object(kind=kind, symbol_type=symbol_type, word=word))


@pytest.mark.parametrize("kind,symbol_type,word", [
    (282, elf.STT_FUNC, B),
    (283, elf.STT_NOTYPE, BL),
    (283, elf.STT_SECTION, BL),
])
def test_unproven_direct_branch_shapes_keep_original_opcode(kind, symbol_type, word):
    image, offset, entry = _link(_object(BASE + 0x1020, kind=kind,
                                       symbol_type=symbol_type, word=word))
    assert _branch_target(image, offset, entry) == BASE + 0x1020
    assert struct.unpack_from("<I", image, offset)[0] & 0xFC000000 == word


def test_resolved_definition_type_controls_undefined_notype_reference():
    caller = _object()
    caller = replace(caller, symbols=caller.symbols[:2] + (
        elf.ElfSymbol("target", elf.SHN_UNDEF, 0, 0, 1, elf.STT_NOTYPE),
    ))
    provider = elf.ElfObject((), (elf.ElfSymbol.null(),
        elf.ElfSymbol("target", elf.SHN_ABS, FAR, 0, 1, elf.STT_FUNC)), elf.EM_AARCH64)
    image = elf.link_static_executable([caller, provider], base_address=BASE)
    assert struct.unpack_from("<IIQ", image, 0x1008) == (0x58000050, 0xD61F0200, FAR)


def test_pool_guards_and_alignment_preserve_fallthrough():
    image, offset, entry = _link(_object(align=64))
    assert offset == 0x1040
    assert _branch_target(image, 0x1000, BASE + 0x1000) == BASE + 0x1018
    assert image[0x1018:offset] == _words(*([NOP] * 10))
    after = offset + 8
    assert _branch_target(image, after, BASE + after) == BASE + after + 24
    assert struct.unpack_from("<I", image, after)[0] & 0xFC000000 == B


def test_multiple_pool_chunks_have_range_checked_branch_around(monkeypatch):
    # Shrink chunk capacity only for the byte-level fixture, never allocate a
    # large image to exercise a numeric branch-range boundary.
    monkeypatch.setattr(arm, "_VENEERS_PER_CHUNK", 2)
    image = bytearray(128)
    arm.initialize_veneer_pool(image, 64, 3)
    assert arm.veneer_pool_end(64, 3) == 128
    assert [arm.veneer_slot_offset(64, i) for i in range(3)] == [72, 88, 112]
    assert _branch_target(image, 64, 64) == 104
    assert _branch_target(image, 104, 104) == 128
    assert struct.unpack_from("<I", image, 68)[0] == NOP


def test_large_chunk_slot_offsets_are_numeric_only():
    count = 65536
    assert arm.veneer_slot_offset(0, 65535) == 65535 * 16 + 16
    assert arm.veneer_pool_end(0, count) == count * 16 + 16
    assert arm.veneer_pool_end(8, 0) == 8


def test_numeric_planner_checks_exact_slot_not_reachable_pool_base():
    place = 1 << 30
    pool = place + (1 << 27) - 8
    slot = arm.veneer_slot_offset(pool, 0)
    assert arm.branch26_fits(pool, place)
    assert not arm.branch26_fits(slot, place)
    assert arm.branch26_fits(place - (1 << 27), place)
    assert not arm.branch26_fits(place - (1 << 27) - 4, place)


def test_numeric_giant_section_keeps_direct_fit_and_fails_only_actual_far_call():
    before = 0x1000
    section_start = arm.veneer_pool_end(before, 1)
    section_size = 1 << 29
    after = (section_start + section_size + 7) & ~7
    place = section_start + section_size // 2
    slots = tuple(arm.veneer_slot_offset(pool, 0) for pool in (before, after))
    assert not any(arm.branch26_fits(slot, place) for slot in slots)
    image = bytearray(_words(BL, RET))
    # No huge allocation: a direct call is independent of unusable boundaries.
    arm.apply_call26_with_veneer(image, 0, place + 4, place,
                                tuple(slot - place for slot in slots))
    assert _branch_target(image, 0, place) == place + 4
    with pytest.raises(ValueError, match="signed_bits=26"):
        arm.apply_call26_with_veneer(image, 0, place + (1 << 28), place,
                                    tuple(slot - place for slot in slots))


def test_preencoded_local_branch_and_recursion_bytes_do_not_move_in_section():
    obj = _object()
    original = _words(B | 2, BL, BL | 0x03FFFFFE, RET)
    section = replace(obj.sections[0], data=original,
                      relocations=(elf.ElfRelocation(4, 2, 283),))
    obj = replace(obj, sections=(section,))
    image, offset, entry = _link(obj)
    assert image[offset:offset + 4] == original[:4]
    assert image[offset + 8:offset + 16] == original[8:]
    assert _branch_target(image, offset, entry) == entry + 8
    assert _branch_target(image, offset + 8, entry + 8) == entry


def test_same_symbol_addends_share_only_matching_reserved_slot():
    obj = _object()
    section = replace(obj.sections[0], data=_words(BL, BL, BL, RET), relocations=(
        elf.ElfRelocation(0, 2, 283, 0),
        elf.ElfRelocation(4, 2, 283, 8),
        elf.ElfRelocation(8, 2, 283, 0),
    ))
    image, offset, entry = _link(replace(obj, sections=(section,)))
    addresses = [_branch_target(image, offset + i * 4, entry + i * 4) for i in range(3)]
    assert addresses[0] == addresses[2] != addresses[1]
    assert [struct.unpack_from("<Q", image, address - BASE + 8)[0]
            for address in addresses] == [FAR, FAR + 8, FAR]


def test_same_name_local_symbols_from_distinct_objects_never_alias():
    objects = []
    for i, target in enumerate((FAR, FAR + 0x100)):
        symbols = (elf.ElfSymbol.null(),
                   elf.ElfSymbol("same", elf.SHN_ABS, target, 0, 0, elf.STT_FUNC),
                   elf.ElfSymbol("_start" if not i else "other", 1, 0, 8, 1, elf.STT_FUNC))
        section = elf.ElfSection(".text", 1, 6, 8, _words(BL, RET),
                                relocations=(elf.ElfRelocation(0, 1, 283),))
        objects.append(elf.ElfObject((section,), symbols, elf.EM_AARCH64))
    image = elf.link_static_executable(objects, base_address=BASE)
    assert struct.unpack_from("<Q", image, 0x1010)[0] == FAR
    assert struct.unpack_from("<Q", image, 0x1048)[0] == FAR + 0x100
    assert len(image) == 0x1070


def test_later_bad_symbol_is_not_resolved_during_candidate_classification():
    obj = _object(FAR + 1)
    text = replace(obj.sections[0], data=_words(BL, BL, RET), relocations=(
        elf.ElfRelocation(0, 2, 283), elf.ElfRelocation(4, 3, 283),
    ))
    comment = elf.ElfSection(".comment", 1, 0, 1, b"x")
    obj = replace(obj, sections=(text, comment), symbols=obj.symbols + (
        elf.ElfSymbol("later", 2, 0, 0, 1, elf.STT_FUNC),
    ))
    with pytest.raises(elf.ElfError, match="unaligned AArch64 branch target") as failure:
        _link(obj)
    assert "relocation_offset=0" in str(failure.value)
    assert "symbol='target'" in str(failure.value)


def test_original_section_payload_bounds_are_not_rescued():
    obj = _object()
    bad = replace(obj.sections[0], relocations=(elf.ElfRelocation(8, 2, 283),))
    with pytest.raises(elf.ElfError, match="outside its payload"):
        replace(obj, sections=(bad,))


def test_packed_rela_and_retained_tuple_inputs_have_identical_images():
    obj = _object(addend=-8)
    rela = struct.pack("<QQq", 0, (2 << 32) | 283, -8)
    section = replace(obj.sections[0], relocations=elf._PackedElfRelocations(rela))
    assert _link(obj)[0] == _link(replace(obj, sections=(section,)))[0]


def test_source_backed_staged_inputs_match_retained_and_packed(tmp_path):
    from pcc.backend.owned_elf_inputs import ElfInputStore

    obj = _object(addend=12)
    path = tmp_path / "tiny.o"
    path.write_bytes(elf.emit_relocatable(obj))
    store = ElfInputStore("aarch64-unknown-linux-gnu")
    staged = store.stage(str(path))
    image = elf._link_static_executable_image([staged], base_address=BASE,
                                              _input_store=store)
    assert bytes(image) == _link(obj)[0]
    assert store.current is None and store.current_index == -1


def test_all_segment_layout_is_reserved_before_first_relocation(monkeypatch):
    obj = _object()
    text = replace(obj.sections[0], data=_words(BL, 0x90000000, RET), relocations=(
        elf.ElfRelocation(0, 2, 283), elf.ElfRelocation(4, 3, 311),
    ))
    sections = (text,
        elf.ElfSection(".data", 1, 3, 8, bytes(8),
                       relocations=(elf.ElfRelocation(0, 1, 257),)),
        elf.ElfSection(".tdata", 1, 3 | elf.SHF_TLS, 16, bytes(8)),
        elf.ElfSection(".tbss", 8, 3 | elf.SHF_TLS, 16, mem_size=32),
        elf.ElfSection(".bss", 8, 3, 32, mem_size=64),
        elf.ElfSection(".pcc_stackmaps", 1, 2, 8,
                       struct.pack("<8sHBBIII", b"PCCSMAP1", 2, 1, 8, 0, 0, 0)),
    )
    obj = replace(obj, sections=sections, symbols=obj.symbols + (
        elf.ElfSymbol("data", 2, 0, 8, 1, elf.STT_OBJECT),
    ))
    real = arm.apply_relocation
    seen = []

    def observe(image, offset, kind, target, place):
        seen.append((len(image), offset, kind, target, place))
        return real(image, offset, kind, target, place)

    monkeypatch.setattr(arm, "apply_relocation", observe)
    image, offset, entry = _link(obj)
    assert seen and {row[0] for row in seen} == {len(image)}
    header = elf._ELF_HEADER.unpack_from(image)
    headers = [elf._PROGRAM_HEADER.unpack_from(image, header[5] + 56 * i)
               for i in range(header[10])]
    assert len(headers) == 4
    rx, rw, tls, metadata = headers
    assert rx[1] == 5 and rw[1] == 6 and tls[0] == 7 and metadata[1] == 4
    assert rw[2] >= rx[2] + rx[5]
    assert tls[2] >= rw[2]
    assert metadata[3] >= rw[3] + rw[6]
    assert metadata[2] + metadata[5] == len(image)
    assert struct.unpack_from("<Q", image, rw[2])[0] == entry
    got = next(row[3] for row in seen if row[2] == 311)
    assert struct.unpack_from("<Q", image, got - BASE)[0] == BASE + rw[2]


def test_no_branch_candidates_keep_original_small_image_layout():
    obj = _object(kind=282, word=B, target=BASE + 0x1004)
    image, offset, entry = _link(obj)
    assert offset == 0x1000 and len(image) == 0x1008
    assert image[offset:] == _words(B | 1, RET)


def test_unresolved_weak_function_is_not_a_proven_veneer_target():
    obj = _object(0)
    obj = replace(obj, symbols=obj.symbols[:2] + (
        elf.ElfSymbol("target", elf.SHN_UNDEF, 0, 0, elf.STB_WEAK, elf.STT_FUNC),
    ))
    with pytest.raises(elf.ElfError, match="signed_bits=26"):
        _link(obj)


def test_near_malformed_call_opcode_retains_existing_direct_behavior():
    # This candidate validates the original opcode only before far rescue.
    # It must not silently broaden unrelated direct-relocation validation.
    place = BASE + 0x1018
    image, offset, entry = _link(_object(place + 4, word=B))
    assert _branch_target(image, offset, entry) == entry + 4
    assert struct.unpack_from("<I", image, offset)[0] == B | 1


def test_distinct_symbol_identities_at_same_address_keep_separate_slots():
    obj = _object()
    text = replace(obj.sections[0], data=_words(BL, BL, RET), relocations=(
        elf.ElfRelocation(0, 2, 283), elf.ElfRelocation(4, 3, 283),
    ))
    obj = replace(obj, sections=(text,), symbols=obj.symbols + (
        elf.ElfSymbol("alias", elf.SHN_ABS, FAR, 0, 1, elf.STT_FUNC),
    ))
    image, offset, entry = _link(obj)
    first = _branch_target(image, offset, entry) - BASE
    second = _branch_target(image, offset + 4, entry + 4) - BASE
    assert first != second
    assert struct.unpack_from("<Q", image, first + 8)[0] == FAR
    assert struct.unpack_from("<Q", image, second + 8)[0] == FAR


def test_numeric_far_call_can_use_suffix_when_prefix_slot_is_unreachable():
    before = 0x1000
    section_start = arm.veneer_pool_end(before, 1)
    section_size = (1 << 27) + 64
    after = (section_start + section_size + 7) & ~7
    place = section_start + section_size - 4
    prefix = arm.veneer_slot_offset(before, 0)
    suffix = arm.veneer_slot_offset(after, 0)
    assert not arm.branch26_fits(prefix, place)
    assert arm.branch26_fits(suffix, place)


def test_far_call_selects_second_slot_after_unreachable_first_slot():
    # The first candidate is a scalar unreachable boundary, not an allocated
    # 128 MiB gap. The second candidate is backed by this tiny image.
    image = bytearray(_words(BL, RET) + bytes(24))
    arm.apply_call26_with_veneer(image, 0, FAR, BASE, (-(1 << 27) - 8, 16))
    assert _branch_target(image, 0, BASE) == BASE + 16
    assert struct.unpack_from("<IIQ", image, 16) == (0x58000050, 0xD61F0200, FAR)
    assert image[4:8] == _words(RET)
