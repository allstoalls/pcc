"""Reusing a validated relocatable stack-map table's verdict for its linked image.

``validate_stack_map_payload`` remembers the tables it accepted, keyed by the
expected architecture and every byte except the function-address words.  The
ELF link validates each input table and then the same table after relocation
has filled those words, so the second validation can reuse the first verdict.
A reused verdict must equal a fresh validation for every input; these controls
compare the two over single-byte corruptions of a linked table.
"""
from __future__ import annotations

import struct

import pytest

from pcc.backend import precise_stackmap as wire
from pcc.backend.precise_stackmap import (
    ARCH_AARCH64,
    ARCH_X86_64,
    FunctionStackMap,
    LOCATION_DERIVED,
    LOCATION_MANAGED,
    LOCATION_OWNED,
    LOCATION_REGISTER,
    LOCATION_RELOAD_REQUIRED,
    LOCATION_STACK_INDIRECT,
    NO_BASE,
    NO_OFFSET,
    PreciseStackMap,
    PreciseStackMapError,
    RECORD_HAS_EXCEPTION_EDGE,
    RECORD_SUSPENDED,
    SAFEPOINT_CALL,
    SAFEPOINT_CONTINUATION,
    SAFEPOINT_ENTRY,
    SafepointRecord,
    StackMapLocation,
    encode_stack_map,
    function_address_offsets,
    function_id,
    safepoint_id,
    validate_stack_map_payload,
)


def _function(arch: int, symbol: str, frame_size: int) -> FunctionStackMap:
    base = StackMapLocation(
        kind=LOCATION_STACK_INDIRECT,
        flags=LOCATION_MANAGED | LOCATION_OWNED,
        register=29 if arch == ARCH_AARCH64 else 6,
        base_index=NO_BASE,
        offset=-8,
    )
    derived = StackMapLocation(
        kind=LOCATION_REGISTER,
        flags=LOCATION_MANAGED | LOCATION_DERIVED | LOCATION_RELOAD_REQUIRED,
        register=9 if arch == ARCH_AARCH64 else 3,
        base_index=0,
    )
    return FunctionStackMap(
        function_id=function_id(symbol),
        function_address=0,
        code_size=48,
        frame_size=frame_size,
        records=(
            SafepointRecord(
                safepoint_id=safepoint_id(symbol, 0, SAFEPOINT_ENTRY),
                instruction_offset=4,
                kind=SAFEPOINT_ENTRY,
                locations=(base,),
            ),
            SafepointRecord(
                safepoint_id=safepoint_id(symbol, 1, SAFEPOINT_CALL),
                instruction_offset=16,
                kind=SAFEPOINT_CALL,
                locations=(base, derived),
                flags=RECORD_HAS_EXCEPTION_EDGE,
                exceptional_offset=24,
            ),
            SafepointRecord(
                safepoint_id=safepoint_id(symbol, 2, SAFEPOINT_CONTINUATION),
                instruction_offset=32,
                kind=SAFEPOINT_CONTINUATION,
                locations=(base,),
                flags=RECORD_SUSPENDED,
                exceptional_offset=NO_OFFSET,
                continuation_id=1,
            ),
        ),
    )


def _tables(arch: int) -> tuple[bytes, bytes]:
    """(relocatable table, the same table after address relocation)."""
    functions = sorted(
        (_function(arch, "_reuse_a", 32), _function(arch, "_reuse_b", 48)),
        key=lambda function: function.function_id,
    )
    relocatable = encode_stack_map(PreciseStackMap(arch=arch, functions=tuple(functions)))
    linked = bytearray(relocatable)
    for index, offset in enumerate(function_address_offsets(relocatable)):
        struct.pack_into("<Q", linked, offset, 0x401000 + 0x40 * index)
    return relocatable, bytes(linked)


def _outcome(payload: bytes, **kwargs):
    try:
        validate_stack_map_payload(payload, **kwargs)
    except PreciseStackMapError as exc:
        return str(exc)
    return None


@pytest.fixture
def isolated_memo(monkeypatch):
    memo: dict[str, bool] = {}
    monkeypatch.setattr(wire, "_VALIDATED_ADDRESS_FREE_TABLES", memo)
    return memo


def _fresh(payload: bytes, **kwargs):
    saved = dict(wire._VALIDATED_ADDRESS_FREE_TABLES)
    wire._VALIDATED_ADDRESS_FREE_TABLES.clear()
    try:
        return _outcome(payload, **kwargs)
    finally:
        wire._VALIDATED_ADDRESS_FREE_TABLES.clear()
        wire._VALIDATED_ADDRESS_FREE_TABLES.update(saved)


def _forbid_full_validation(monkeypatch) -> None:
    def take(*_args, **_kwargs):
        raise AssertionError("full stack-map validation ran")

    monkeypatch.setattr(wire, "_take", take)


@pytest.mark.parametrize("arch", [ARCH_AARCH64, ARCH_X86_64])
def test_linked_table_reuses_the_relocatable_verdict(arch, isolated_memo, monkeypatch):
    relocatable, linked = _tables(arch)
    last_address = function_address_offsets(relocatable)[-1]
    validate_stack_map_payload(relocatable, expected_arch=arch, final_image=False)
    assert len(isolated_memo) == 1

    _forbid_full_validation(monkeypatch)
    validate_stack_map_payload(linked, expected_arch=arch, final_image=True)
    validate_stack_map_payload(linked, expected_arch=arch, final_image=False)
    # An address left unresolved by the link still fails the final image.
    unresolved = bytearray(linked)
    struct.pack_into("<Q", unresolved, last_address, 0)
    with pytest.raises(PreciseStackMapError, match="final-image function address is unresolved"):
        validate_stack_map_payload(bytes(unresolved), expected_arch=arch, final_image=True)


@pytest.mark.parametrize("arch", [ARCH_AARCH64, ARCH_X86_64])
@pytest.mark.parametrize("final_image", [True, False])
def test_reused_verdict_equals_fresh_validation_for_single_byte_corruptions(
    arch, final_image, isolated_memo,
):
    relocatable, linked = _tables(arch)
    rejected = 0
    for position in range(len(linked)):
        for replacement in {linked[position] ^ 0x01, linked[position] ^ 0x80, 0x00, 0xFF}:
            if replacement == linked[position]:
                continue
            corrupted = bytearray(linked)
            corrupted[position] = replacement
            corrupted = bytes(corrupted)
            expected = _fresh(corrupted, expected_arch=arch, final_image=final_image)
            isolated_memo.clear()
            assert _outcome(relocatable, expected_arch=arch, final_image=False) is None
            actual = _outcome(corrupted, expected_arch=arch, final_image=final_image)
            assert actual == expected, (position, replacement)
            rejected += expected is not None
    # The corpus must exercise the rejection paths, not only address words.
    assert rejected > len(linked)


def test_reuse_is_scoped_to_the_expected_architecture(isolated_memo):
    relocatable, linked = _tables(ARCH_AARCH64)
    validate_stack_map_payload(relocatable, expected_arch=ARCH_AARCH64)
    for expected_arch in (ARCH_X86_64, None):
        assert _outcome(linked, expected_arch=expected_arch, final_image=True) == _fresh(
            linked, expected_arch=expected_arch, final_image=True,
        )
    with pytest.raises(PreciseStackMapError, match="does not match target"):
        validate_stack_map_payload(linked, expected_arch=ARCH_X86_64, final_image=True)


@pytest.mark.parametrize("limit, message", [
    ("MAX_FUNCTIONS", "too many stack-map functions"),
    ("MAX_RECORDS", "too many stack-map records"),
    ("MAX_LOCATIONS", "too many stack-map locations"),
])
def test_reuse_is_scoped_to_the_resource_limits(limit, message, isolated_memo, monkeypatch):
    relocatable, linked = _tables(ARCH_AARCH64)
    validate_stack_map_payload(relocatable, expected_arch=ARCH_AARCH64)
    monkeypatch.setattr(wire, limit, 1)
    assert _fresh(linked, expected_arch=ARCH_AARCH64, final_image=True).startswith(message)
    with pytest.raises(PreciseStackMapError, match=message):
        validate_stack_map_payload(linked, expected_arch=ARCH_AARCH64, final_image=True)


def test_budgeted_validation_does_not_reuse_or_record(isolated_memo, monkeypatch):
    relocatable, linked = _tables(ARCH_AARCH64)
    budget = wire.CompactStackMapBudget(
        1_000_000, 100, set_header_bytes=10, set_entry_bytes=2, integer_bytes=3,
        metadata_entry_bytes=7, allocation_quantum=1,
    )
    validate_stack_map_payload(relocatable, expected_arch=ARCH_AARCH64, compact_budget=budget)
    assert isolated_memo == {}
    charged = budget.record_visits
    validate_stack_map_payload(relocatable, expected_arch=ARCH_AARCH64)
    assert len(isolated_memo) == 1
    validate_stack_map_payload(
        linked, expected_arch=ARCH_AARCH64, final_image=True, compact_budget=budget,
    )
    # The budgeted call did the full work again and was charged for it.
    assert budget.record_visits == 2 * charged


def test_remembered_tables_are_bounded(isolated_memo, monkeypatch):
    monkeypatch.setattr(wire, "_VALIDATED_ADDRESS_FREE_TABLE_LIMIT", 1)
    aarch64, _ = _tables(ARCH_AARCH64)
    x86_64, _ = _tables(ARCH_X86_64)
    validate_stack_map_payload(aarch64, expected_arch=ARCH_AARCH64)
    validate_stack_map_payload(x86_64, expected_arch=ARCH_X86_64)
    assert len(isolated_memo) == 1
