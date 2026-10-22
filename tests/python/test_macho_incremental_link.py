"""Focused contracts for content-addressed incremental Mach-O linking."""

from __future__ import annotations

import hashlib
from pathlib import Path

from pcc.backend import macho_spec as spec
from pcc.backend.macho_codesign import build_signature, parse_signature
from pcc.backend.macho_exec import (
    link_executable,
    link_prepared_executable,
    prepare_executable_object,
)
from pcc.backend.macho_incremental import IncrementalMachOLinker
from pcc.backend.macho_obj import (
    Relocation,
    Section,
    TEXT_SECTION_FLAGS,
    TextSymbol,
)
from pcc.backend.native_object import (
    NativeObject,
    decode_packed_native_object,
    encode_native_object,
)


_RET = b"\xc0\x03\x5f\xd6"
_NOP = b"\x1f\x20\x03\xd5"
_MOV_X0_X0 = b"\xe0\x03\x00\xaa"
_BL_PLACEHOLDER = b"\x00\x00\x00\x94"


def _caller() -> NativeObject:
    return NativeObject.from_sections(
        [Section(
            sectname="__text",
            segname="__TEXT",
            data=_BL_PLACEHOLDER + _RET,
            align_log2=2,
            flags=TEXT_SECTION_FLAGS,
            symbols=(TextSymbol("_main", 0),),
            relocations=(Relocation(
                offset=0,
                symbol="_helper",
                type=spec.ARM64_RELOC_BRANCH26,
                pcrel=True,
            ),),
        )],
        undefined=["_helper"],
    )


def _helper(prefix: bytes) -> NativeObject:
    return NativeObject.from_sections([Section(
        sectname="__text",
        segname="__TEXT",
        data=prefix + _RET,
        align_log2=2,
        flags=TEXT_SECTION_FLAGS,
        symbols=(TextSymbol("_helper", 0),),
    )])


def _validate_linked_image(image: bytes) -> None:
    signature = parse_signature(image)
    assert signature.identifier == b"pcc-linked"
    assert signature.dataoff + signature.datasize == len(image)
    assert image[signature.dataoff:] == build_signature(
        image[:signature.dataoff],
        identifier=signature.identifier,
        exec_seg_base=signature.exec_seg_base,
        exec_seg_limit=signature.exec_seg_limit,
        exec_seg_flags=signature.exec_seg_flags,
    )


def _rewrite_with_valid_checksum(path: Path, payload: bytes) -> None:
    path.write_bytes(payload)
    path.with_name(path.name + ".sha256").write_text(
        hashlib.sha256(payload).hexdigest() + "\n",
        encoding="ascii",
    )


def test_same_layout_edit_reuses_merged_state_and_is_cold_link_identical(
    tmp_path: Path,
) -> None:
    prepare_calls: list[tuple[NativeObject, ...]] = []

    def counted_prepare(objects, *, archives=()):
        prepare_calls.append(tuple(objects))
        return prepare_executable_object(objects, archives=archives)

    first_objects = [_caller(), _helper(_NOP)]
    first = IncrementalMachOLinker(tmp_path / "cache", "test-link-source-v1")
    first_image = first.link(
        first_objects,
        prepare=counted_prepare,
        finalize=link_prepared_executable,
        validate=_validate_linked_image,
    )
    assert first.stats.merged_misses == 1
    assert len(prepare_calls) == 1

    # The helper changes bytes but not section size, symbols, or relocations.
    # This must patch the cached merged object instead of preparing all inputs.
    edited_objects = [_caller(), _helper(_MOV_X0_X0)]
    edited = IncrementalMachOLinker(tmp_path / "cache", "test-link-source-v1")
    edited_image = edited.link(
        edited_objects,
        prepare=counted_prepare,
        finalize=link_prepared_executable,
        validate=_validate_linked_image,
    )
    cold_image = link_executable(edited_objects)

    assert edited.stats.image_hits == 0
    assert edited.stats.merged_hits == 1
    assert edited.stats.incremental_fallbacks == 0
    assert len(prepare_calls) == 1
    assert edited_image == cold_image
    assert edited_image != first_image

    # The exact edited action now bypasses both merge and finalization.
    exact = IncrementalMachOLinker(tmp_path / "cache", "test-link-source-v1")

    def unexpected(*_args, **_kwargs):
        raise AssertionError("exact incremental hit reached linker work")

    exact_image = exact.link(
        edited_objects,
        prepare=unexpected,
        finalize=unexpected,
        validate=_validate_linked_image,
    )
    assert exact.stats.image_hits == 1
    assert exact_image == cold_image


def test_packed_same_layout_edit_reuses_merged_state(
    tmp_path: Path,
) -> None:
    def packed(value: NativeObject):
        return decode_packed_native_object(encode_native_object(value))

    prepare_calls = 0

    def counted_prepare(objects, *, archives=()):
        nonlocal prepare_calls
        prepare_calls += 1
        return prepare_executable_object(objects, archives=archives)

    first_objects = [packed(_caller()), packed(_helper(_NOP))]
    first = IncrementalMachOLinker(
        tmp_path / "cache", "test-link-source-v1",
    )
    first.link(
        first_objects,
        prepare=counted_prepare,
        finalize=link_prepared_executable,
        validate=_validate_linked_image,
    )

    edited_objects = [packed(_caller()), packed(_helper(_MOV_X0_X0))]
    edited = IncrementalMachOLinker(
        tmp_path / "cache", "test-link-source-v1",
    )
    edited_image = edited.link(
        edited_objects,
        prepare=counted_prepare,
        finalize=link_prepared_executable,
        validate=_validate_linked_image,
    )

    assert edited.stats.merged_hits == 1
    assert edited.stats.incremental_fallbacks == 0
    assert prepare_calls == 1
    assert edited_image == link_executable(edited_objects)

def test_layout_change_is_a_cold_prepare_not_an_approximate_patch(
    tmp_path: Path,
) -> None:
    prepare_count = 0

    def counted_prepare(objects, *, archives=()):
        nonlocal prepare_count
        prepare_count += 1
        return prepare_executable_object(objects, archives=archives)

    cache_dir = tmp_path / "cache"
    IncrementalMachOLinker(cache_dir, "test-link-source-v1").link(
        [_caller(), _helper(_NOP)],
        prepare=counted_prepare,
        validate=_validate_linked_image,
    )
    longer = [_caller(), _helper(_NOP + _MOV_X0_X0)]
    incremental = IncrementalMachOLinker(cache_dir, "test-link-source-v1")
    image = incremental.link(
        longer,
        prepare=counted_prepare,
        validate=_validate_linked_image,
    )

    assert incremental.stats.merged_hits == 0
    assert incremental.stats.merged_misses == 1
    assert prepare_count == 2
    assert image == link_executable(longer)


def test_semantically_invalid_exact_image_cache_is_rebuilt(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    objects = [_caller(), _helper(_NOP)]
    first = IncrementalMachOLinker(cache_dir, "test-link-source-v1")
    expected = first.link(objects, validate=_validate_linked_image)
    image_path = next(cache_dir.rglob("*.macho"))
    signature = parse_signature(expected)
    corrupted = bytearray(expected)
    corrupted[signature.dataoff - 1] ^= 1
    _rewrite_with_valid_checksum(image_path, bytes(corrupted))

    prepare_count = 0
    finalize_count = 0

    def counted_prepare(values, *, archives=()):
        nonlocal prepare_count
        prepare_count += 1
        return prepare_executable_object(values, archives=archives)

    def counted_finalize(merged, **kwargs):
        nonlocal finalize_count
        finalize_count += 1
        return link_prepared_executable(merged, **kwargs)

    repaired = IncrementalMachOLinker(cache_dir, "test-link-source-v1")
    actual = repaired.link(
        objects,
        prepare=counted_prepare,
        finalize=counted_finalize,
        validate=_validate_linked_image,
    )

    assert prepare_count == 0
    assert finalize_count == 1
    assert repaired.stats.image_hits == 0
    assert repaired.stats.merged_hits == 1
    assert actual == expected
    assert image_path.read_bytes() == expected


def test_semantically_invalid_native_object_cache_is_reassembled(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    build_count = 0

    def assemble(_text: str) -> NativeObject:
        nonlocal build_count
        build_count += 1
        return _caller()

    IncrementalMachOLinker(
        cache_dir, "test-link-source-v1",
    ).native_object_from_assembly("caller", assemble)
    native_path = next(cache_dir.rglob("*.pco"))
    _rewrite_with_valid_checksum(native_path, b"not a native object")

    session = IncrementalMachOLinker(cache_dir, "test-link-source-v1")
    restored = session.native_object_from_assembly("caller", assemble)

    assert restored == _caller()
    assert build_count == 2
    assert session.stats.assembly_hits == 0
    assert session.stats.assembly_misses == 1


def test_assembly_cache_can_return_a_validated_packed_view(
    tmp_path: Path,
) -> None:
    from pcc.backend.native_object import PackedNativeObject

    cache_dir = tmp_path / "cache"
    first = IncrementalMachOLinker(cache_dir, "test-link-source-v1")
    first.native_object_from_assembly("caller", lambda _text: _caller())

    session = IncrementalMachOLinker(cache_dir, "test-link-source-v1")
    _path, cached, replace_valid = session.probe_assembly_cache(
        "caller", packed=True,
    )

    assert isinstance(cached, PackedNativeObject)
    assert not replace_valid
    assert session.stats.assembly_hits == 1


def test_semantic_identity_keys_exact_images_and_disables_layout_patch(
    tmp_path: Path,
) -> None:
    cache_dir = tmp_path / "cache"
    objects = [_caller(), _helper(_NOP)]
    prepare_calls = 0

    def counted_prepare(values, *, archives=()):
        nonlocal prepare_calls
        prepare_calls += 1
        return prepare_executable_object(values, archives=archives)

    first = IncrementalMachOLinker(cache_dir, "test-link-source-v1")
    first_image = first.link(
        objects,
        semantic_identity=b"frontend-policy:first",
        prepare=counted_prepare,
        validate=_validate_linked_image,
    )
    assert prepare_calls == 1
    assert first.stats.image_misses == 1
    assert first.stats.merged_hits == 0
    assert first.stats.merged_misses == 0

    changed = IncrementalMachOLinker(cache_dir, "test-link-source-v1")
    changed_image = changed.link(
        objects,
        semantic_identity=b"frontend-policy:second",
        prepare=counted_prepare,
        validate=_validate_linked_image,
    )
    assert changed_image == first_image
    assert prepare_calls == 2
    assert changed.stats.image_hits == 0
    assert changed.stats.merged_hits == 0
    assert changed.stats.merged_misses == 0

    exact = IncrementalMachOLinker(cache_dir, "test-link-source-v1")

    def unexpected(*_args, **_kwargs):
        raise AssertionError("semantic exact-image hit reached linker work")

    assert exact.link(
        objects,
        semantic_identity=b"frontend-policy:first",
        prepare=unexpected,
        finalize=unexpected,
        validate=_validate_linked_image,
    ) == first_image
    assert exact.stats.image_hits == 1


def _stack_mapped_caller() -> NativeObject:
    from pcc.backend import precise_stackmap as maps
    from pcc.backend.macho_obj import PCC_STACKMAP_SECTION_FLAGS

    payload = maps.encode_stack_map(maps.PreciseStackMap(
        arch=maps.ARCH_AARCH64,
        functions=(maps.FunctionStackMap(
            function_id=maps.function_id("_main"), function_address=0,
            code_size=16, frame_size=32,
            records=(maps.SafepointRecord(
                safepoint_id=maps.safepoint_id("_main", 0, maps.SAFEPOINT_CALL),
                instruction_offset=4,
                kind=maps.SAFEPOINT_CALL,
                locations=(maps.StackMapLocation(
                    maps.LOCATION_STACK_INDIRECT, maps.LOCATION_MANAGED,
                    register=29, offset=-16,
                ),),
            ),),
        ),),
    ))
    offset, = maps.function_address_offsets(payload)
    return NativeObject.from_sections(
        [
            Section(
                sectname="__text",
                segname="__TEXT",
                data=_BL_PLACEHOLDER + _NOP + _NOP + _RET,
                align_log2=2,
                flags=TEXT_SECTION_FLAGS,
                symbols=(TextSymbol("_main", 0),),
                relocations=(Relocation(
                    offset=0,
                    symbol="_helper",
                    type=spec.ARM64_RELOC_BRANCH26,
                    pcrel=True,
                ),),
            ),
            Section(
                sectname="__pcc_stackmaps",
                segname="__DATA",
                data=payload,
                flags=PCC_STACKMAP_SECTION_FLAGS,
                align_log2=3,
                relocations=(Relocation(
                    offset, "_main", spec.ARM64_RELOC_UNSIGNED, False, 3,
                ),),
            ),
        ],
        undefined=["_helper"],
    )


def _image_stack_map(image: bytes) -> bytes:
    for section in spec.parse_object(image).sections():
        if (section["segname_str"], section["sectname_str"]) == ("__DATA", "__pcc_stackmaps"):
            return image[section["offset"]:section["offset"] + section["size"]]
    raise AssertionError("image has no stack-map table")


def test_stack_map_inputs_are_never_layout_patched(tmp_path: Path, monkeypatch) -> None:
    # The merged table is rebuilt from the inputs' records, so copying an
    # input's bytes at a concatenation offset cannot reproduce a cold merge.
    monkeypatch.delenv("PCC_PRECISE_STACKMAP_FORMAT", raising=False)
    first = IncrementalMachOLinker(tmp_path / "cache", "test-link-source-v1")
    first.link(
        [_stack_mapped_caller(), _helper(_NOP)],
        prepare=prepare_executable_object,
        finalize=link_prepared_executable,
        validate=_validate_linked_image,
    )
    edited_objects = [_stack_mapped_caller(), _helper(_MOV_X0_X0)]
    edited = IncrementalMachOLinker(tmp_path / "cache", "test-link-source-v1")
    edited_image = edited.link(
        edited_objects,
        prepare=prepare_executable_object,
        finalize=link_prepared_executable,
        validate=_validate_linked_image,
    )
    assert (edited.stats.merged_hits, edited.stats.merged_misses) == (0, 0)
    assert edited_image == link_executable(edited_objects)


def test_exact_images_are_keyed_by_the_stack_map_format(tmp_path: Path, monkeypatch) -> None:
    from pcc.backend.precise_stackmap import RUNTIME_TABLE_VERSION, VERSION, validate_stack_map_payload

    images = {}
    for setting, version in (("", VERSION), ("3", RUNTIME_TABLE_VERSION)):
        monkeypatch.setenv("PCC_PRECISE_STACKMAP_FORMAT", setting)
        linker = IncrementalMachOLinker(tmp_path / "cache", "test-link-source-v1")
        image = linker.link(
            [_stack_mapped_caller(), _helper(_NOP)],
            prepare=prepare_executable_object,
            finalize=link_prepared_executable,
            validate=_validate_linked_image,
        )
        assert linker.stats.image_hits == 0
        assert image == link_executable([_stack_mapped_caller(), _helper(_NOP)])
        table = _image_stack_map(image)
        assert int.from_bytes(table[8:10], "little") == version
        validate_stack_map_payload(table, final_image=True)
        images[setting] = image
    assert len(_image_stack_map(images["3"])) < len(_image_stack_map(images[""]))
