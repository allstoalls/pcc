"""Host-only contracts for canonical section-validation payload windows."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from pcc.backend import macho_obj as obj, macho_spec as spec
from pcc.backend import native_object as native, precise_stackmap as maps
from pcc.backend.macho_host_output import _ValidationBudget
from pcc.backend.macho_link import LinkError


def _section(**changes):
    section = obj.Section(
        "__data", "__DATA", data=b"\0" * 16,
        symbols=(obj.TextSymbol("_main", 0),),
    )
    return replace(section, **changes)


def _relocation(**changes):
    relocation = obj.Relocation(0, "_main", spec.ARM64_RELOC_UNSIGNED, False, 3)
    return replace(relocation, **changes)


def _metadata(section):
    # The metadata phase has no payload property to consult accidentally.
    return SimpleNamespace(
        segname=section.segname, sectname=section.sectname,
        symbols=section.symbols, is_zerofill=section.is_zerofill,
        vm_size=section.vm_size, flags=section.flags,
    )


def test_name_helpers_need_only_metadata_and_complete_the_name_domain():
    seen = set()
    zerofill = set()
    sections = (
        _section(),
        _section(sectname="__bss", data=b"", zerofill_size=16,
                 flags=obj.ZEROFILL_SECTION_FLAGS,
                 symbols=(obj.TextSymbol("_bss", 0),)),
        _section(segname="__OTHER", symbols=(obj.TextSymbol("_other", 0),)),
    )
    for section in sections:
        native._validate_source_section_names(_metadata(section), seen, zerofill)
    native._validate_source_undefined_names(("_external",), seen)
    assert seen == {"_main", "_bss", "_other", "_external"}
    assert zerofill == {"__DATA"}


@pytest.mark.parametrize("sections, message", (
    ((_section(), _section(sectname="__other")), "duplicate symbol '_main'"),
    ((_section(symbols=(obj.TextSymbol("_bad\0name", 0),)),),
     "symbol name '_bad\\x00name' contains NUL"),
    ((_section(sectname="__bss", data=b"", zerofill_size=16,
               flags=obj.ZEROFILL_SECTION_FLAGS),
      _section(symbols=(obj.TextSymbol("_other", 0),))),
     "content section __data after a zerofill section"),
))
def test_name_helpers_preserve_canonical_errors(sections, message):
    seen = set()
    zerofill = set()
    with pytest.raises(obj.MachOEmitError) as direct:
        for section in sections:
            native._validate_source_section_names(_metadata(section), seen, zerofill)
    with pytest.raises(native.NativeObjectError) as ordinary:
        native._validate_source_sections(sections, ())
    assert str(direct.value) == str(ordinary.value) == message


@pytest.mark.parametrize("defined, undefined, message", (
    (set(), ("_external",), "at least one defined symbol is required"),
    ({"_main"}, (None,), "bad undefined symbol name None"),
    ({"_main"}, ("",), "bad undefined symbol name ''"),
    ({"_main"}, ("_\u00e9",), "bad undefined symbol name '_\u00e9'"),
    ({"_main"}, ("_bad\0name",), "symbol name '_bad\\x00name' contains NUL"),
    ({"_main"}, ("_main",), "symbol '_main' both defined and undefined"),
    ({"_main"}, ("_external", "_external"),
     "symbol '_external' both defined and undefined"),
))
def test_undefined_helper_preserves_canonical_errors(defined, undefined, message):
    with pytest.raises(obj.MachOEmitError) as direct:
        native._validate_source_undefined_names(undefined, set(defined))
    section = _section(symbols=tuple(obj.TextSymbol(name, 0) for name in defined))
    with pytest.raises(native.NativeObjectError) as ordinary:
        native._validate_source_sections((section,), undefined)
    assert str(direct.value) == str(ordinary.value) == message


def test_ordinary_validation_keeps_interleaved_section_error_order(monkeypatch):
    calls = []
    for name in (
        "_validate_section", "_validate_source_section_names",
        "_validate_source_undefined_names", "_validate_source_section_relocations",
    ):
        original = getattr(native, name)

        def tracked(*args, _name=name, _original=original, **kwargs):
            calls.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(native, name, tracked)
    native._validate_source_sections((
        _section(), _section(sectname="__other", symbols=()),
    ), ("_external",))
    assert calls == [
        "_validate_section", "_validate_source_section_names",
        "_validate_section", "_validate_source_section_names",
        "_validate_source_undefined_names", "_validate_source_section_relocations",
        "_validate_source_section_relocations",
    ]
    malformed = _section(align_log2=-1, symbols=(obj.TextSymbol("_bad\0", 0),))
    with pytest.raises(native.NativeObjectError, match="align_log2 must be 0..31"):
        native._validate_source_sections((malformed,), ())


@pytest.mark.parametrize("relocations", (
    (_relocation(symbol="_missing"),),
    (_relocation(offset=16),),
    (_relocation(type=255),),
    (_relocation(), _relocation()),
    (_relocation(symbol="", section=("__DATA", "__target"), target_offset=8),),
    (_relocation(symbol="", section=("__DATA", "__missing"), target_offset=0),),
))
def test_relocation_helper_preserves_canonical_errors(relocations):
    source = _section(relocations=relocations)
    target = _section(sectname="__target", data=b"\0" * 8, symbols=())
    metadata = {(section.segname, section.sectname): _metadata(section)
                for section in (source, target)}
    with pytest.raises(obj.MachOEmitError) as direct:
        native._validate_source_section_relocations(
            source, {"_main": 0}, metadata, iter(relocations), len(relocations),
        )
    with pytest.raises(native.NativeObjectError) as ordinary:
        native._validate_source_sections((source, target), ())
    assert str(direct.value) == str(ordinary.value)


def test_relocation_helper_uses_cross_section_vm_size_and_flags():
    relocation = _relocation(symbol="", section=("__DATA", "__target"), target_offset=15)
    source = _section(relocations=(relocation,))
    target = _metadata(_section(sectname="__target", symbols=()))
    native._validate_source_section_relocations(
        source, {"_main": 0}, {("__DATA", "__target"): target}, (relocation,), 1,
    )
    target.flags = obj.CSTRING_SECTION_FLAGS
    with pytest.raises(obj.MachOEmitError, match="linker-special section"):
        native._validate_source_section_relocations(
            source, {"_main": 0}, {("__DATA", "__target"): target}, (relocation,), 1,
        )


@pytest.mark.parametrize("size", (16, 4096))
@pytest.mark.parametrize("proven", (0, 1, 2))
def test_relocation_helper_keeps_dense_and_sparse_duplicate_claims(size, proven):
    relocations = (_relocation(), _relocation())
    section = _section(data=b"\0" * size, relocations=relocations)
    with pytest.raises(obj.MachOEmitError, match="multiple relocation requests at offset 0"):
        native._validate_source_section_relocations(
            section, {"_main": 0}, {}, relocations, len(relocations), proven,
        )


def test_proven_prefix_keeps_bounds_and_validates_every_remaining_record():
    relocations = (_relocation(symbol="_already_proven"), _relocation(offset=8))
    section = _section(relocations=relocations)
    native._validate_source_section_relocations(section, {"_main": 0}, {}, relocations, 2, 1)
    native._validate_source_sections((section,), (), proven_relocations=(1,))
    for rows, proven, message in (
        ((_relocation(offset=16),), 1, "relocation offset 16 outside"),
        (relocations, 3, "proven relocation prefix exceeds"),
        (relocations, -1, "proven relocation prefix exceeds"),
        ((relocations[0], _relocation(offset=8, symbol="_missing")), 1,
         "relocation targets unknown symbol '_missing'"),
    ):
        with pytest.raises(obj.MachOEmitError, match=message):
            native._validate_source_section_relocations(
                section, {"_main": 0}, {}, rows, len(rows), proven,
            )


def test_final_section_rows_explicit_sizes_preserve_layout_without_payloads():
    sections = (
        _section(data=b"abc", align_log2=0),
        _section(sectname="__other", data=b"12345", relocations=(
            _relocation(addend=4),
            _relocation(type=spec.ARM64_RELOC_SUBTRACTOR, minuend="_other"),
        )),
        _section(sectname="__bss", data=b"", zerofill_size=32,
                 flags=obj.ZEROFILL_SECTION_FLAGS),
        _section(sectname="__tail", data=b"x", align_log2=4),
    )
    payloads = (b"abcdefg", b"12345", b"", b"x")
    addresses = (0, 8, 16, 48)
    expected = native._final_section_rows(sections, payloads, addresses)
    actual = native._final_section_rows(
        sections, None, addresses, payload_sizes=tuple(map(len, payloads)),
    )
    assert actual == expected
    assert [row["offset"] for row in actual] == [0, 8, 0, 16]
    assert [row["nreloc"] for row in actual] == [0, 4, 0, 0]
    assert native._final_section_rows(
        sections, None, addresses, (1, 2, 3, 4),
        payload_sizes=tuple(map(len, payloads)),
    ) == [dict(row, nreloc=index + 1) for index, row in enumerate(expected)]


class _RecordingBudget(maps.CompactStackMapBudget):
    def __init__(self, limit=1_000_000):
        super().__init__(
            limit, 100, set_header_bytes=10, set_entry_bytes=2, integer_bytes=3,
            metadata_entry_bytes=7, allocation_quantum=1,
        )
        self.admissions = []

    def check_peak(self, raw_bytes, *, records=0, entries=0, label="stack-map"):
        self.admissions.append((label, raw_bytes, records, entries))
        return super().check_peak(raw_bytes, records=records, entries=entries, label=label)


class _BytesSubclass(bytes):
    pass


def _payload():
    return maps.encode_stack_map(maps.PreciseStackMap(
        arch=maps.ARCH_AARCH64,
        functions=(maps.FunctionStackMap(
            function_id=1, function_address=0, code_size=4, frame_size=0, records=(),
        ),),
    ))


@pytest.mark.parametrize("convert", (bytes, _BytesSubclass, bytearray, memoryview))
@pytest.mark.parametrize("retained", (False, True))
def test_scanners_charge_unknown_owners_in_full(convert, retained):
    payload = _payload()
    data = convert(payload)
    ledger = _RecordingBudget()
    budget = _ValidationBudget(ledger, payload) if retained else ledger
    expected = maps._scan_stack_map_payload(payload)
    assert maps._scan_stack_map_payload(data, compact_budget=budget) == expected
    raw_bytes = 0 if retained and data is payload else len(data)
    assert ledger.admissions == [("input", raw_bytes, 0, 1)]
    ledger.admissions.clear()
    assert obj._scan_stack_map_section(data, compact_budget=budget) == (1, [32])
    assert ledger.admissions == [
        ("Mach-O section stack-map snapshot", 2 * raw_bytes, 0, 0),
        ("input", raw_bytes, 0, 1),
    ]


def test_retained_identity_does_not_credit_equal_unknown_bytes():
    payload = _payload()
    unknown = bytes(bytearray(payload))
    assert unknown == payload and unknown is not payload
    ledger = _RecordingBudget()
    budget = _ValidationBudget(ledger, payload)
    obj._scan_stack_map_section(unknown, compact_budget=budget)
    assert ledger.admissions == [
        ("Mach-O section stack-map snapshot", 2 * len(payload), 0, 0),
        ("input", len(payload), 0, 1),
    ]


@pytest.mark.parametrize("convert", (_BytesSubclass, bytearray, memoryview))
def test_validation_adapter_rejects_nonexact_retained_payloads(convert):
    with pytest.raises(LinkError, match="retained validation payload must be exact bytes"):
        _ValidationBudget(_RecordingBudget(), convert(_payload()))


def test_retained_payload_remains_charged_and_metadata_admission_is_not_waived():
    payload = _payload()
    ledger = _RecordingBudget(len(payload) + 7)
    ledger.retained_bytes = len(payload)
    budget = _ValidationBudget(ledger, payload)
    assert obj._scan_stack_map_section(payload, compact_budget=budget) == (1, [32])
    ledger.limit_bytes -= 1
    with pytest.raises(maps.PreciseStackMapError, match="budget exceeded: input"):
        obj._scan_stack_map_section(payload, compact_budget=budget)
    assert "retained=" + str(len(payload)) in ledger.last_rejection
    with pytest.raises(maps.PreciseStackMapError, match="Mach-O section stack-map snapshot"):
        obj._scan_stack_map_section(payload, compact_budget=ledger)


@pytest.mark.parametrize("change, message", (
    (lambda payload: b"badmagic" + payload[8:], "bad pcc stack-map magic"),
    (lambda payload: payload[:-1], "truncated function record"),
    (lambda payload: payload + b"\0", "size disagrees with decoded records"),
))
def test_retained_scanner_keeps_structural_rejections(change, message):
    payload = change(_payload())
    budget = _ValidationBudget(_RecordingBudget(), payload)
    for scan in (maps._scan_stack_map_payload, obj._scan_stack_map_section):
        with pytest.raises(maps.PreciseStackMapError, match=message):
            scan(payload, compact_budget=budget)
