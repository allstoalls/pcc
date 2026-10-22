"""Host checks for the shared metadata-only Mach-O section placement step."""

import hashlib

import pytest

from pcc.backend import macho_exec, macho_link, macho_obj as obj, macho_spec as spec
from pcc.backend import native_object as native
from pcc.backend.precise_stackmap import CompactStackMapBudget


def _budget():
    return CompactStackMapBudget(
        1_000_000, 100, set_header_bytes=10, set_entry_bytes=2,
        integer_bytes=3, metadata_entry_bytes=7, allocation_quantum=1,
    )


def _place_sequence(entries):
    merged, order = {}, []
    chunk_index, chunk_key = 0, macho_link._TEXT_KEY
    placements = []
    for key, flags, alignment, size in entries:
        destination, section, base, padding, chunk_index, chunk_key = (
            macho_link._place_merged_section(
                merged, order, key, flags, alignment, size,
                chunk_index, chunk_key,
            )
        )
        # The shared step cannot read a payload. Its caller commits the
        # actual size only after the payload read, as the merge always did.
        if flags & spec.SECTION_TYPE not in (spec.S_ZEROFILL, 0x12):
            section.data_size = base + size
        placements.append((destination, base, padding))
    return merged, order, placements, (chunk_index, chunk_key)


def test_shared_placement_uses_declared_sizes_before_alignment(monkeypatch):
    monkeypatch.setattr(macho_link, "_TEXT_CHUNK_BYTES", 8)
    key = macho_link._TEXT_KEY
    merged, order, placements, state = _place_sequence([
        (key, obj.TEXT_SECTION_FLAGS, 0, 5),
        (key, obj.TEXT_SECTION_FLAGS, 4, 3),
        (key, obj.TEXT_SECTION_FLAGS, 2, 4),
    ])
    assert placements == [(key, 0, 0), (key, 16, 11), (("__TEXT", "__text1"), 0, 0)]
    assert order == [key, ("__TEXT", "__text1")]
    assert merged[key].data_size == 19
    assert merged[key].data_in_code == [obj.DataInCodeRegion(5, 11)]
    assert state == (1, ("__TEXT", "__text1"))


@pytest.mark.parametrize("flags", (spec.S_ZEROFILL, 0x12))
def test_zerofill_placement_advances_vm_size_without_payload(flags):
    key = ("__DATA", "__bss")
    merged, order, placements, state = _place_sequence([
        (key, flags, 0, 5), (key, flags, 3, 7),
    ])
    assert placements == [(key, 0, 0), (key, 8, 0)]
    assert order == [key]
    assert merged[key].zerofill_size == 15
    assert merged[key].data_size == 0
    assert merged[key].data_in_code == []
    assert merged[key].align_log2 == 3
    assert state == (0, macho_link._TEXT_KEY)


@pytest.mark.parametrize("first_size,second_flags,regions", [
    (5, obj.TEXT_SECTION_FLAGS, [obj.DataInCodeRegion(5, 3)]),
    (4, obj.TEXT_SECTION_FLAGS, []),
    (5, obj.DATA_SECTION_FLAGS, []),
])
def test_attribute_union_controls_inline_padding(first_size, second_flags, regions):
    key = ("__TEXT", "__other")
    merged, order, placements, _state = _place_sequence([
        (key, obj.DATA_SECTION_FLAGS, 0, first_size),
        (key, second_flags, 3, 4),
    ])
    assert placements == [(key, 0, 0), (key, 8, 8 - first_size)]
    assert merged[key].flags == second_flags
    assert merged[key].data_in_code == regions
    assert order == [key]


@pytest.mark.parametrize("sizes,expected_names", [
    ((4, 4), ["__text", "__text"]),
    ((9, 1), ["__text", "__text1"]),
    ((0, 9), ["__text", "__text"]),
])
def test_chunk_boundary_and_oversized_first_input(monkeypatch, sizes, expected_names):
    monkeypatch.setattr(macho_link, "_TEXT_CHUNK_BYTES", 8)
    _merged, _order, placements, _state = _place_sequence([
        (macho_link._TEXT_KEY, obj.TEXT_SECTION_FLAGS, 0, size)
        for size in sizes
    ])
    assert [key[1] for key, _base, _padding in placements] == expected_names


def test_placement_does_not_commit_an_unread_payload():
    merged, order = {}, []
    _key, section, base, padding, _index, _chunk = macho_link._place_merged_section(
        merged, order, ("__DATA", "__data"), spec.S_REGULAR, 3, 123,
        0, macho_link._TEXT_KEY,
    )
    assert (base, padding, section.data_size) == (0, 0, 0)
    assert section.data_parts == []
    assert section.data == b""


def _inputs(representation):
    result = []
    for index in range(2):
        name = "_main" if index == 0 else "_helper"
        text = b"\xc0\x03\x5f\xd6" * 2
        regions = ()
        if index == 0:
            text += b"\x7f"
            regions = (obj.DataInCodeRegion(8, 1),)
        sections = [
            obj.Section(
                sectname="__text", segname="__TEXT", data=text,
                flags=obj.TEXT_SECTION_FLAGS, align_log2=2 if index == 0 else 4,
                symbols=(obj.TextSymbol(name, 0), obj.TextSymbol("Llocal", 4, external=False)),
                data_in_code=regions,
            ),
            obj.Section(
                sectname="__data", segname="__DATA", data=b"\0" * 8,
                flags=obj.DATA_SECTION_FLAGS, align_log2=3,
                relocations=(obj.Relocation(
                    0, "", spec.ARM64_RELOC_UNSIGNED, False, 3,
                    section=("__TEXT", "__text"), target_offset=0,
                ),),
            ),
            obj.Section(
                sectname="__bss", segname="__DATA", data=b"",
                flags=obj.ZEROFILL_SECTION_FLAGS, align_log2=3,
                zerofill_size=5 + index,
                symbols=(obj.TextSymbol("_bss" + str(index), 0),),
            ),
        ]
        value = native.NativeObject.from_sections(sections)
        if representation == "packed":
            value = native.decode_packed_native_object(native.encode_native_object(value))
        elif representation == "view":
            value = value.link_view()
        elif representation == "raw":
            value = value.to_macho()
        elif representation == "parsed":
            value = spec.parse_object(value.to_macho())
        result.append(value)
    return result


# Captured from the existing merge before extracting its placement step.
_MERGED_SHA256 = {
    8: "ebe840b295e1aae4c5bcc7268767b632b140ea133fa6b1b33e227c21aabe728d",
    64: "3f5e1b108fd403c842e1619920418c2f45e8bc6fc766a287e5cd227009897950",
}
_IMAGE_SHA256 = {
    8: "68b8777cc9b04786cc768b2c0dbe831ac2e288bd84ab8e3546e0e8102f03da83",
    64: "06d6d2f6733bb28ffdddce2f68357de960df703f4f1409af2be0621e596274ad",
}


@pytest.mark.parametrize("representation", ("native", "packed", "view", "raw", "parsed"))
@pytest.mark.parametrize("chunk_limit", (8, 64))
@pytest.mark.parametrize("owned,budgeted", ((False, False), (True, False), (True, True)))
def test_current_merge_calls_shared_placement_with_exact_output_parity(
    monkeypatch, representation, chunk_limit, owned, budgeted,
):
    monkeypatch.setattr(macho_link, "_TEXT_CHUNK_BYTES", chunk_limit)
    placement = macho_link._place_merged_section
    calls = []

    def record(*args):
        calls.append(args[2])
        return placement(*args)

    monkeypatch.setattr(macho_link, "_place_merged_section", record)
    budget = _budget() if budgeted else None
    inputs = _inputs(representation)
    merged = macho_exec.prepare_executable_object(
        inputs, _consume_inputs=owned, _source_view=owned, compact_budget=budget,
    )
    assert calls == [("__TEXT", "__text"), ("__DATA", "__data"), ("__DATA", "__bss")] * 2
    assert (inputs == []) is owned
    if not owned:
        assert hashlib.sha256(merged.to_macho()).hexdigest() == _MERGED_SHA256[chunk_limit]
    image = macho_exec.link_prepared_executable(merged, compact_budget=budget)
    assert hashlib.sha256(image).hexdigest() == _IMAGE_SHA256[chunk_limit]
    if budget is not None:
        assert budget.retained_bytes == 0


def test_conflicting_types_fail_before_reading_the_conflicting_payload(monkeypatch):
    first = native.NativeObject.from_sections([
        obj.Section("__data", "__DATA", data=b"\0" * 8, symbols=(obj.TextSymbol("_main", 0),)),
    ])
    second = native.NativeObject.from_sections([
        obj.Section(
            "__data", "__DATA", data=b"", flags=spec.S_ZEROFILL,
            zerofill_size=8, symbols=(obj.TextSymbol("_main", 0),),
        ),
    ])
    original = macho_link._section_payload
    calls = []

    def read(value, *args):
        calls.append(value)
        if value is second:
            raise AssertionError("conflicting input payload was read")
        return original(value, *args)

    monkeypatch.setattr(macho_link, "_section_payload", read)
    with pytest.raises(macho_link.LinkError, match="conflicting section TYPES"):
        macho_link.link_relocatable_native([first, second])
    assert calls == [first]
