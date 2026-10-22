"""Host-only archive-state parity and ownership; no external tools or execution."""

import copy
import hashlib
import struct
import weakref

import pytest

from pcc.backend import macho_exec, macho_spec as spec
from pcc.backend.ar_writer import write_archive
from pcc.backend.macho_archive import read_archive, select_members
from pcc.backend.macho_link import LinkError, _coerce_link_object
from pcc.backend.macho_obj import (
    DATA_SECTION_FLAGS,
    TEXT_SECTION_FLAGS,
    Relocation,
    Section,
    TextSymbol,
)
from pcc.backend.native_object import (
    NativeObject,
    PackedNativeObject,
    decode_packed_native_object,
    encode_native_object,
)


def _unit(name, references=(), *, weak=(), extra_symbols=()):
    sections = [Section(
        sectname="__text", segname="__TEXT", data=b"\xc0\x03\x5f\xd6",
        flags=TEXT_SECTION_FLAGS, align_log2=2,
        symbols=(TextSymbol(name, 0),) + tuple(extra_symbols),
    )]
    if references:
        sections.append(Section(
            sectname="__data", segname="__DATA", data=b"\0" * (8 * len(references)),
            flags=DATA_SECTION_FLAGS, align_log2=3,
            relocations=tuple(Relocation(
                offset=8 * index, symbol=symbol,
                type=spec.ARM64_RELOC_UNSIGNED, pcrel=False, length=3,
            ) for index, symbol in enumerate(references)),
        ))
    return NativeObject.from_sections(
        sections, undefined=tuple(references), weak_undefined=tuple(weak),
    )


def _represent(obj, representation):
    if representation == "native":
        return obj
    if representation == "packed":
        return decode_packed_native_object(encode_native_object(obj))
    if representation == "view":
        return obj.link_view()
    if representation == "raw":
        return obj.to_macho()
    assert representation == "parsed"
    return spec.parse_object(obj.to_macho())


def _symbols(value):
    obj = _coerce_link_object(value, 0)
    if isinstance(obj, (NativeObject, PackedNativeObject)):
        return obj.symbols
    return obj.symbols()


def _state_inputs():
    return [
        _unit("_main", ("_late", "_weak", "_both", "_missing"),
              weak=("_weak", "_both"), extra_symbols=(
                  TextSymbol("_private", 0, private_external=True),
                  TextSymbol("local", 0, external=False),
              )),
        _unit("_late", ("_both",)),
        _unit("_other", ("_late",)),
    ]


@pytest.mark.parametrize("representation", ("native", "packed", "view", "raw", "parsed"))
@pytest.mark.parametrize("window_size", (1, 2, 5))
@pytest.mark.parametrize("reverse", (False, True))
def test_symbol_state_accumulates_windows_without_changing_input_tables(
    representation, window_size, reverse,
):
    objects = [_represent(obj, representation) for obj in _state_inputs()]
    if reverse:
        objects.reverse()
    expected = ({"_main", "_private", "_late", "_other"}, {"_both", "_missing"})
    assert macho_exec._external_symbol_state(objects) == expected
    defined, undefined = set(), set()
    before = [copy.deepcopy(tuple(_symbols(obj))) for obj in objects]
    for start in range(0, len(objects), window_size):
        for obj in objects[start:start + window_size]:
            macho_exec._accumulate_external_symbol_state(_symbols(obj), defined, undefined)
    assert (defined, undefined - defined) == expected
    assert undefined == {"_late", "_both", "_missing"}
    assert [tuple(_symbols(obj)) for obj in objects] == before


def test_object_wrapper_calls_shared_helper_in_input_order(monkeypatch):
    objects = [_represent(obj, kind) for obj, kind in zip(
        _state_inputs(), ("packed", "native", "view"),
    )]
    tables = [_symbols(obj) for obj in objects]
    calls = []
    accumulate = macho_exec._accumulate_external_symbol_state

    def record(symbols, defined, undefined):
        assert symbols is tables[len(calls)]
        calls.append(symbols)
        return accumulate(symbols, defined, undefined)

    monkeypatch.setattr(macho_exec, "_accumulate_external_symbol_state", record)
    assert macho_exec._external_symbol_state(objects) == (
        {"_main", "_private", "_late", "_other"}, {"_both", "_missing"},
    )
    assert len(calls) == len(objects)


@pytest.mark.parametrize("representation", ("native", "packed", "view", "parsed"))
def test_symbol_state_keeps_table_iteration_and_set_update_order(representation):
    obj = _represent(_unit("_main", ("_z", "_a"), extra_symbols=(
        TextSymbol("_second", 0), TextSymbol("local", 0, external=False),
    )), representation)
    events = []

    class RecordedSet(set):
        def __init__(self, label):
            self.label = label

        def add(self, name):
            events.append((self.label, name))
            super().add(name)

    macho_exec._accumulate_external_symbol_state(
        _symbols(obj), RecordedSet("defined"), RecordedSet("undefined"),
    )
    assert events == [
        ("defined", "_main"), ("defined", "_second"),
        ("undefined", "_a"), ("undefined", "_z"),
    ]


def test_symbol_state_retains_no_input_or_payload_owner(monkeypatch):
    retired = []

    class TrackedBytes(bytes):
        def __del__(self):
            retired.append(True)

    def forbidden(*args, **kwargs):
        pytest.fail("symbol-table state inspected an object or payload")

    encoded = encode_native_object(_unit("_main", ("_missing",)))
    obj = decode_packed_native_object(TrackedBytes(encoded))
    reference = weakref.ref(obj)
    symbols = obj.symbols
    defined, undefined = set(), set()
    monkeypatch.setattr(macho_exec, "_coerce_link_object", forbidden)
    monkeypatch.setattr(PackedNativeObject, "section_data", forbidden)
    monkeypatch.setattr(PackedNativeObject, "relocation_fields", forbidden)
    macho_exec._accumulate_external_symbol_state(symbols, defined, undefined)
    del symbols, obj
    assert reference() is None and retired == [True]
    assert defined == {"_main"} and undefined == {"_missing"}


@pytest.mark.parametrize("representation", ("native", "packed", "view", "raw", "parsed"))
def test_archive_backreferences_and_extraction_match_window_state(representation):
    main = _unit("_main", ("_late", "_optional"), weak=("_optional",))
    early = _unit("_early", ("_main",))
    late = _unit("_late", ("_early", "_next"))
    following = _unit("_next", ("_early",))
    archives = [write_archive([
        ("early.o", early.to_macho()), ("late.o", late.to_macho()),
        ("duplicate.o", _unit("_main").to_macho()),
        ("optional.o", _unit("_optional", ("_unprovided",)).to_macho()),
        ("unused.o", _unit("_unused").to_macho()),
    ]), write_archive([
        ("duplicate-early.o", _unit("_early").to_macho()),
        ("next.o", following.to_macho()),
    ])]
    defined, undefined = set(), set()
    explicit = _represent(main, representation)
    macho_exec._accumulate_external_symbol_state(_symbols(explicit), defined, undefined)
    selected = []
    expected_receipt = []
    for archive_index, archive in enumerate(archives):
        members = []
        pulled, pending = select_members(
            read_archive(archive), undefined - defined,
            already_defined=defined, selected_members=members,
        )
        selected.extend(pulled)
        for member in members:
            macho_exec._accumulate_external_symbol_state(_symbols(member.data), defined, undefined)
            expected_receipt.append({
                "archive_index": archive_index, "member": member.name,
                "archive_sha256": hashlib.sha256(archive).hexdigest(),
                "member_sha256": hashlib.sha256(member.data).hexdigest(),
            })
        assert pending == undefined - defined
    assert [item["member"] for item in expected_receipt] == ["early.o", "late.o", "next.o"]
    assert undefined - defined == set()
    assert (defined, undefined - defined) == macho_exec._external_symbol_state([explicit] + selected)
    actual_receipt = []
    prepared = macho_exec.prepare_executable_object(
        [explicit], archives=archives, archive_selections=actual_receipt,
    )
    assert actual_receipt == expected_receipt
    assert macho_exec.link_prepared_executable(prepared) == macho_exec.link_executable(
        [main, early, late, following],
    )


@pytest.mark.parametrize("damage", ("encoded-pco", "load-command"))
def test_archive_state_does_not_bypass_input_validation(monkeypatch, damage):
    obj = _unit("_main", ("_missing",))
    if damage == "encoded-pco":
        bad = encode_native_object(obj)
        message = "decode it explicitly"
    else:
        bad = bytearray(obj.to_macho())
        build = spec.parse_object(bytes(bad)).command(spec.LC_BUILD_VERSION)
        assert build is not None
        struct.pack_into("<I", bad, build.offset, spec.LC_LINKER_OPTION)
        bad = bytes(bad)
        message = "unsupported load command"

    def forbidden(*args, **kwargs):
        pytest.fail("archive state ran before input validation")

    monkeypatch.setattr(macho_exec, "_accumulate_external_symbol_state", forbidden)
    with pytest.raises(LinkError, match=message):
        macho_exec.prepare_executable_object([bad], archives=[b"not an archive"])
