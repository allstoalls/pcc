"""Packed link rows keep their nine-field contract without keyword binding."""

from dataclasses import fields, replace
import struct

import pytest

from pcc.backend import macho_link as link, macho_spec as spec
from pcc.backend.macho_obj import (
    DATA_SECTION_FLAGS,
    TEXT_SECTION_FLAGS,
    Relocation,
    Section,
    TextSymbol,
    emit_object,
)
from pcc.backend.native_object import (
    NativeObjectError,
    decode_packed_native_object,
    encode_native_object_from_sections,
)


_FIELDS = (
    "offset", "symbol", "type", "pcrel", "length", "addend", "section",
    "minuend", "target_offset",
)
_ORDERS = (
    (1, (0, 1, 2, 3, 4, 5, 6)),
    (-1, (6, 5, 4, 3, 2, 1, 0)),
    (0, (2, 6, 0, 4, 1, 5, 3)),
)


def _packed_input(indices=_ORDERS[0][1]):
    text = Section(
        "__text", "__TEXT", bytes.fromhex("40058052c0035fd6"), 2,
        TEXT_SECTION_FLAGS,
        (TextSymbol("_base", 0, False), TextSymbol("_target", 4, False)),
    )
    rows = (
        Relocation(0, "_base", spec.ARM64_RELOC_UNSIGNED, False, 3),
        Relocation(8, "_base", spec.ARM64_RELOC_PAGE21, True, 2, 7),
        Relocation(12, "_target", spec.ARM64_RELOC_PAGEOFF12, False, 2, 0x7FFFFF),
        Relocation(16, "_base", spec.ARM64_RELOC_SUBTRACTOR, False, 3,
                   minuend="_target"),
        Relocation(24, "", spec.ARM64_RELOC_UNSIGNED, False, 3,
                   section=("__TEXT", "__text"), target_offset=4),
        Relocation(32, "", spec.ARM64_RELOC_UNSIGNED, False, 3,
                   section=("__TEXT", "__text")),
        Relocation(40, "_external", spec.ARM64_RELOC_UNSIGNED, False, 3),
    )
    data = Section(
        "__data", "__DATA", b"\0" * 48, 3, DATA_SECTION_FLAGS,
        relocations=tuple(rows[index] for index in indices),
    )
    encoded = encode_native_object_from_sections([text, data], undefined=["_external"])
    return text, data, decode_packed_native_object(encoded)


@pytest.mark.parametrize("order,indices", _ORDERS, ids=["ascending", "descending", "mixed"])
@pytest.mark.parametrize("cached_names", [False, True], ids=["resolve", "cached"])
def test_packed_rows_use_positional_fields_and_preserve_macho_bytes(
    monkeypatch, order, indices, cached_names,
):
    text, data, packed = _packed_input(indices)
    assert packed.sections[1].relocation_order == order
    assert tuple(field.name for field in fields(Relocation)) == _FIELDS
    before = tuple(packed.decoded_relocations(1))
    rename = {"_base": "_base_copy", "_target": "_target_copy"}
    symbol_names = (
        [rename.get(symbol.name, symbol.name) for symbol in packed.symbols]
        if cached_names else None
    )
    bias = 16
    expected = [
        replace(
            row, offset=row.offset + bias,
            symbol=rename.get(row.symbol, row.symbol),
            minuend=rename.get(row.minuend, row.minuend),
        )
        for row in sorted(data.relocations, key=lambda row: row.offset, reverse=True)
    ]
    calls = []

    def positional_record(*args, **kwargs):
        assert kwargs == {}
        assert len(args) == 9
        row = Relocation(*args)
        assert all(getattr(row, name) is value for name, value in zip(_FIELDS, args))
        calls.append(row)
        return row

    monkeypatch.setattr(link, "new_relocation", positional_record)
    actual = link._read_relocations(
        packed, packed.sections[1], packed.symbols, rename,
        offset_bias=bias, symbol_names=symbol_names,
    )
    assert actual == expected == calls
    assert all(type(row) is Relocation for row in actual)
    assert len({id(row) for row in actual}) == len(actual)
    assert tuple(packed.decoded_relocations(1)) == before
    assert [row.offset for row in data.relocations] == [
        (0, 8, 12, 16, 24, 32, 40)[index] for index in indices
    ]

    output_text = replace(text, symbols=tuple(
        replace(symbol, name=rename[symbol.name]) for symbol in text.symbols
    ))
    output_data = replace(data, data=b"\0" * bias + data.data)
    actual_bytes = emit_object(
        [output_text, replace(output_data, relocations=tuple(actual))],
        undefined=["_external"],
    )
    expected_bytes = emit_object(
        [output_text, replace(output_data, relocations=tuple(expected))],
        undefined=["_external"],
    )
    assert actual_bytes == expected_bytes
    parsed = spec.parse_object(actual_bytes)
    assert len(parsed.relocations(parsed.sections()[1])) == 10


def test_packed_name_failure_precedes_bias_and_record_construction(monkeypatch):
    _text, _data, packed = _packed_input()
    events = []

    class Names:
        def __len__(self):
            return len(packed.symbols)

        def __getitem__(self, index):
            events.append(("name", index))
            raise LookupError("name lookup first")

    class Bias:
        def __radd__(self, value):
            pytest.fail("offset bias evaluated before name resolution")

    def unexpected_record(*args, **kwargs):
        pytest.fail("record constructed before name resolution")

    monkeypatch.setattr(link, "new_relocation", unexpected_record)
    with pytest.raises(LookupError, match="name lookup first"):
        link._read_relocations(
            packed, packed.sections[1], packed.symbols, {},
            offset_bias=Bias(), symbol_names=Names(),
        )
    assert len(events) == 1


def test_packed_decode_rejects_pcrel_before_symbol_index_and_construction(monkeypatch):
    _text, _data, packed = _packed_input()
    malformed = bytearray(packed.encoded)
    start = packed.sections[1].relocation_offset
    struct.pack_into("<I", malformed, start + 8, 0xFFFFFFFE)
    malformed[start + 16] = 2

    def unexpected_record(*args, **kwargs):
        pytest.fail("record constructed from rejected packed bytes")

    monkeypatch.setattr(link, "new_relocation", unexpected_record)
    with pytest.raises(NativeObjectError, match="relocation pcrel byte is not boolean"):
        decode_packed_native_object(bytes(malformed))
