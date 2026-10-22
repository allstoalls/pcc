"""Host-only input lifetime and linked-image parity; no compiler or execution."""

import weakref

import pytest

from pcc.backend import macho_exec, macho_link, macho_obj, macho_spec
from pcc.backend import precise_stackmap as wire
from pcc.backend.native_object import (
    NativeObject,
    decode_packed_native_object,
    encode_native_object,
)


def _encoded_input():
    payload = wire.encode_stack_map(wire.PreciseStackMap(
        arch=wire.ARCH_AARCH64,
        functions=(wire.FunctionStackMap(
            function_id=wire.function_id("_main"),
            function_address=0, code_size=64, frame_size=32,
            records=(wire.SafepointRecord(
                safepoint_id=1, instruction_offset=4,
                kind=wire.SAFEPOINT_ENTRY, locations=(),
            ),),
        ),),
    ))
    address_offset, = wire.function_address_offsets(payload)
    return encode_native_object(NativeObject.from_sections([
        macho_obj.Section(
            sectname="__text", segname="__TEXT",
            data=b"\xc0\x03\x5f\xd6" * 16,
            align_log2=2, flags=macho_obj.TEXT_SECTION_FLAGS,
            symbols=(macho_obj.TextSymbol("_main", 0),),
        ),
        macho_obj.Section(
            sectname="__pcc_stackmaps", segname="__DATA", data=payload,
            align_log2=3, flags=macho_obj.PCC_STACKMAP_SECTION_FLAGS,
            relocations=(macho_obj.Relocation(
                offset=address_offset, symbol="_main",
                type=macho_spec.ARM64_RELOC_UNSIGNED, pcrel=False, length=3,
            ),),
        ),
        # A final ordinary section returns a slice retaining its whole PCO.
        # Its symbol relocation keeps this fixture on the zero-copy path.
        macho_obj.Section(
            sectname="__data", segname="__DATA", data=b"\0" * 8,
            align_log2=3, flags=macho_obj.DATA_SECTION_FLAGS,
            relocations=(macho_obj.Relocation(
                offset=0, symbol="_main", type=macho_spec.ARM64_RELOC_UNSIGNED,
                pcrel=False, length=3,
            ),),
        ),
    ]))


def _budget():
    # Synthetic allocation profile tests ownership, not measured host RSS.
    return wire.CompactStackMapBudget(
        1_000_000, 100, set_header_bytes=10, set_entry_bytes=2,
        integer_bytes=3, metadata_entry_bytes=7, allocation_quantum=1,
    )


@pytest.mark.parametrize("consume_inputs", (False, True))
@pytest.mark.parametrize("budgeted", (False, True))
@pytest.mark.parametrize("merge_error", (False, True))
def test_last_packed_payload_view_retires_before_stackmap_merge(
    monkeypatch, consume_inputs, budgeted, merge_error,
):
    encoded = _encoded_input()
    expected = macho_exec.link_executable([decode_packed_native_object(encoded)])
    retired = []

    class TrackedEncodedBytes(bytes):
        def __del__(self):
            retired.append(True)

    inputs = [decode_packed_native_object(TrackedEncodedBytes(encoded))]
    input_ref = weakref.ref(inputs[0])
    original_merge = macho_link.merge_stack_map_payloads
    checked = []

    def check_merge(payloads, **kwargs):
        # Object retirement alone is insufficient: a memoryview can still
        # retain its encoded bytes after the PackedNativeObject is gone.
        assert (input_ref() is None) is consume_inputs
        assert bool(retired) is consume_inputs
        checked.append(True)
        if merge_error:
            raise wire.PreciseStackMapError("injected host merge failure")
        return original_merge(payloads, **kwargs)

    monkeypatch.setattr(macho_link, "merge_stack_map_payloads", check_merge)
    budget = _budget() if budgeted else None
    if merge_error:
        with pytest.raises(macho_link.LinkError, match="injected host merge failure"):
            macho_exec.prepare_executable_object(
                inputs, _consume_inputs=consume_inputs, _source_view=consume_inputs,
                compact_budget=budget,
            )
    else:
        view = macho_exec.prepare_executable_object(
            inputs, _consume_inputs=consume_inputs, _source_view=consume_inputs,
            compact_budget=budget,
        )
        assert macho_exec.link_prepared_executable(view, compact_budget=budget) == expected
    assert checked == [True]
    assert (inputs == []) is consume_inputs
    if budget is not None:
        assert budget.retained_bytes == 0
