"""Bounded host output payloads; full Mach-O bytes are the public oracle."""

import struct
import sys
import tracemalloc
import weakref
from pathlib import Path

import pytest

from pcc.backend import (
    macho_exec,
    macho_host_output as output,
    macho_host_prepare as host,
    macho_obj as obj,
    macho_spec as spec,
    native_object as native,
    precise_stackmap as wire,
)
from tests.python.test_macho_fixup_retirement import _fixup_input, _link
from tests.python.test_macho_owned_output import (
    _factory_budget,
    _stack_inputs,
    _tracked_buffers,
)
from tests.python.test_macho_payload_windows import _source, _stage, _signature_matches


@pytest.mark.parametrize("sizes, expected", (
    ((36, 36, 64, 33, 216), (48, 768, 224)),
    ((36, 36, 80, 33, 216), (48, 784, 224)),
    ((36, 49, 65, 49, 225), (64, 848, 240)),
))
def test_factory_budget_tracks_host_object_sizes_without_relaxing_admission(monkeypatch, sizes, expected):
    from tests.python import test_stackmap_host_budget as fixtures

    configure = fixtures.configured_host
    positive, negative, triple, empty_bytes, empty_set = sizes

    def configured(patch, **kwargs):
        environment, runtime = configure(patch, **kwargs)

        def getsizeof(value):
            if type(value) is int:
                assert value in ((1 << 64) - 1, -(1 << 63))
                return positive if value > 0 else negative
            if type(value) is tuple:
                assert value == (0, 0, 0)
                return triple
            if type(value) is bytes:
                assert value == b""
                return empty_bytes
            assert type(value) is set and not value
            return empty_set

        runtime.getsizeof = getsizeof
        return environment, runtime

    monkeypatch.setattr(fixtures, "configured_host", configured)
    metadata = expected[1]
    budget = _factory_budget(2 * metadata + 17)
    assert (budget.integer_bytes, budget.metadata_entry_bytes, budget.set_header_bytes) == expected
    assert budget.allocation_quantum == budget.set_entry_bytes == 16
    budget.retained_bytes = 17
    budget.check_peak(0, entries=2)
    with pytest.raises(wire.PreciseStackMapError, match="byte budget exceeded"):
        budget.check_peak(1, entries=2)


def _staged_link(paths, budget=None, **kwargs):
    budget = _factory_budget() if budget is None else budget
    return output.link_owned_executable(
        host.prepare_executable_from_paths(paths, compact_budget=budget, _staged_rows=True),
        compact_budget=budget, **kwargs,
    )


def test_ci_selects_staged_output_regressions_on_mac_with_default_workers():
    root = Path(__file__).resolve().parents[2]
    workflow = (root / ".github/workflows/pcc1-package-parity.yml").read_text()
    tests = (
        "tests/python/test_macho_output_payload_spool.py",
        "tests/python/test_macho_output_spool_failures.py",
        "tests/python/test_macho_payload_windows.py",
    )
    for path in tests:
        assert workflow.count('      - "' + path + '"') == 2
    selection = "${{ matrix.platform == 'macos-arm64' && '" + " ".join(tests) + "' || '' }}"
    command = next(line for line in workflow.splitlines()
                   if "--junitxml=build/worker-prior-focused-host.xml" in line)
    assert selection in command and "python -m pytest -x" in command
    assert "-n0" not in command and "--dist" not in command and "--noconftest" not in command
    assert '-n 6 --dist=loadgroup' in (root / "pyproject.toml").read_text()


def test_staged_output_fits_one_window_below_aggregate_preparation_peak(monkeypatch):
    # Symbol metadata remains resident during section transfer. The final
    # image fits, but retaining every payload beside that graph does not.
    def text():
        return obj.Section(
            "__text", "__TEXT", data=b"\xc0\x03\x5f\xd6", align_log2=2,
            flags=obj.TEXT_SECTION_FLAGS,
            symbols=(obj.TextSymbol("_main", 0),) + tuple(
                obj.TextSymbol("_symbol" + str(i), 0) for i in range(1000)
            ),
        )

    def data(index):
        return obj.Section(
            "__data" + str(index), "__DATA", data=b"\0" * 8 + bytes([index + 1]) * (131072 - 8),
            align_log2=3,
            relocations=(obj.Relocation(0, "_main", spec.ARM64_RELOC_UNSIGNED, False, 3),),
        )

    builders = (text,) + tuple(lambda i=i: data(i) for i in range(8))
    expected = macho_exec.link_prepared_executable(
        native.OwnedMergedSourceView(tuple(build() for build in builders)),
        compact_budget=_factory_budget(),
    )
    budget = _factory_budget()
    budget.retained_bytes = 137
    def row():
        return native._FINAL_LINK_RELOCATION.pack(0, 0, 0, 3, 1, spec.ARM64_RELOC_UNSIGNED)

    pending = [_source(_stage(builders, budget, (lambda: b"",) + (row,) * 8), budget)]
    source_ref = weakref.ref(pending[0])
    payload_store = pending[0]._host_payload_store
    aggregate = sum(payload_store.backing_bytes(i) for i in range(9))
    initial = output._OwnedOutput.__init__
    admission = budget.check_peak
    observed = {}
    transfers = []

    def limited(state, *args, **kwargs):
        initial(state, *args, **kwargs)
        known = state.known_bytes()
        budget.limit_bytes = known + 2 * 131072 + budget.retained_bytes
        observed.update(known=known, limit=budget.limit_bytes)
        assert len(expected) + output._FIXED + budget.retained_bytes < budget.limit_bytes
        assert known + aggregate + budget.retained_bytes > budget.limit_bytes

    def checked(raw, *, records=0, entries=0, label="stack-map"):
        if label == "Mach-O owned source payload transfer":
            transfers.append(raw + budget.retained_bytes)
        return admission(raw, records=records, entries=entries, label=label)

    monkeypatch.setattr(output._OwnedOutput, "__init__", limited)
    monkeypatch.setattr(budget, "check_peak", checked)
    actual = output.link_owned_executable(pending.pop(), compact_budget=budget)
    assert actual == expected
    assert source_ref() is None and payload_store.stream is None
    assert len(transfers) == 9 and max(transfers) <= observed["limit"]
    assert budget.retained_bytes == 137
    _signature_matches(actual, budget)


@pytest.mark.parametrize("tlv", (False, True))
@pytest.mark.parametrize("count", (8, 4096))
def test_staged_global_fixup_chains_match_public_across_sections_and_pages(tmp_path, monkeypatch, tlv, count):
    paths = _fixup_input(tmp_path, count=count, tlv=tlv)
    expected = _link(paths, "public")
    patched = []
    patch_word = output._OwnedOutput.patch_word

    def tracked(state, out, offset, word):
        if type(out.data) is output._FilePayload:
            patched.append((out.sectname, out.addr + offset, word))
        return patch_word(state, out, offset, word)

    monkeypatch.setattr(output._OwnedOutput, "patch_word", tracked)
    actual = _staged_link(paths)
    assert actual == expected
    assert {name for name, _, _ in patched} >= {"__data", "__const"}
    if count == 8:
        # The last __const pointer chains into __data within the same page.
        last_const = max((site for site in patched if site[0] == "__const"), key=lambda site: site[1])
        first_data = min((site for site in patched if site[0] == "__data"), key=lambda site: site[1])
        assert last_const[1] // macho_exec.PAGE == first_data[1] // macho_exec.PAGE
        assert (last_const[2] >> 51) & 0xFFF == (first_data[1] - last_const[1]) // 4
    else:
        assert len({address // macho_exec.PAGE for _, address, _ in patched}) > 2
    assert bool(spec.parse_object(actual).header["flags"] & 0x00800000) is tlv
    _signature_matches(actual, _factory_budget())


def _sparse_fixup(segment_offset, word, *, payload_offset=0, payload_size=8):
    # A virtual gap is metadata, not a request to allocate its backing bytes.
    out = macho_exec._Out(
        "__DATA", "__data", obj.DATA_SECTION_FLAGS, 2,
        bytearray(b"\xa5" * payload_size),
        addr=macho_exec.TEXT_BASE + segment_offset,
    )
    return out.addr, out, payload_offset, word


class _FixupWordSink:
    def __init__(self):
        self.writes = []

    def patch_word(self, out, offset, word):
        self.writes.append((out, offset, word))
        struct.pack_into("<Q", out.data, offset, word)


@pytest.mark.parametrize("positions", (
    (8,),
    (20, 4, 12),
    (2 * macho_exec.PAGE + 20, 12, 2 * macho_exec.PAGE + 4),
    (0xFFFF * macho_exec.PAGE - 8, 8),
))
def test_private_fixup_encoder_matches_public_without_reordering_public_input(positions):
    def sites():
        return [_sparse_fixup(offset, ((1 << 63) | 7) if i % 2 else 0x123456789)
                for i, offset in enumerate(positions)]

    public, owned = sites(), sites()
    before = list(public)
    size = (0x10000 + 64) * macho_exec.PAGE
    expected = macho_exec._build_seg_info(public, macho_exec.TEXT_BASE, size, macho_exec.PAGE)
    sink = _FixupWordSink()
    actual = output._OwnedOutput._build_seg_info(
        sink, owned, macho_exec.TEXT_BASE, size, macho_exec.PAGE,
    )
    assert actual == expected
    assert public == before
    assert [row[0] for row in owned] == sorted(row[0] for row in before)
    assert {row[0]: bytes(row[1].data) for row in owned} == {
        row[0]: bytes(row[1].data) for row in public
    }


@pytest.mark.parametrize("fault", (
    "empty", "duplicate", "outside", "unaligned", "short", "page-crossing", "65536-pages",
))
def test_private_fixup_encoder_keeps_error_order_and_validates_before_writes(fault):
    def sites():
        first = _sparse_fixup(8, 7)
        if fault == "empty":
            return []
        second = {
            "duplicate": lambda: _sparse_fixup(8, 9),
            "outside": lambda: _sparse_fixup(-4, 9),
            "unaligned": lambda: _sparse_fixup(18, 9),
            "short": lambda: _sparse_fixup(16, 9, payload_size=7),
            "page-crossing": lambda: _sparse_fixup(macho_exec.PAGE - 4, 9),
            "65536-pages": lambda: _sparse_fixup(0xFFFF * macho_exec.PAGE, 9),
        }[fault]()
        return [second, first]

    errors = []
    for private in (False, True):
        rows = sites()
        before = {id(row[1]): bytes(row[1].data) for row in rows}
        sink = _FixupWordSink()
        with pytest.raises(macho_exec.LinkError) as caught:
            if private:
                output._OwnedOutput._build_seg_info(
                    sink, rows, macho_exec.TEXT_BASE, 0x10000 * macho_exec.PAGE,
                    macho_exec.PAGE,
                )
            else:
                macho_exec._build_seg_info(
                    rows, macho_exec.TEXT_BASE, 0x10000 * macho_exec.PAGE,
                    macho_exec.PAGE, _owned_output=sink,
                )
        errors.append(str(caught.value))
        assert {id(row[1]): bytes(row[1].data) for row in rows} == before
        assert sink.writes == []
    assert errors[0] == errors[1]


def test_private_fixup_starts_workspace_is_packed_and_budgeted():
    # A huge numeric extent uses only two tiny payloads. A Python object per
    # empty page would exceed this bound by an order of magnitude.
    rows = [_sparse_fixup(8, 7), _sparse_fixup(0xFFFF * macho_exec.PAGE - 8, 9)]
    sink = _FixupWordSink()
    tracemalloc.start()
    try:
        info = output._OwnedOutput._build_seg_info(
            sink, rows, macho_exec.TEXT_BASE, 0xFFFF * macho_exec.PAGE, macho_exec.PAGE,
        )
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(info) == 22 + 2 * 0xFFFF
    assert peak < 6 * 0xFFFF + output._FIXED
    budget = _factory_budget()
    state = _projection_state(budget)
    initial = state.known_bytes()
    state.reserve_pages(0xFFFF)
    assert state.known_bytes() - initial >= peak
    assert state.known_bytes() - initial < 16 * 0xFFFF


def _projection_state(budget):
    source = native.OwnedMergedSourceView((obj.Section(
        "__text", "__TEXT", data=b"\xc0\x03\x5f\xd6", align_log2=2,
        flags=obj.TEXT_SECTION_FLAGS, symbols=(obj.TextSymbol("_main", 0),),
    ),))
    return output._OwnedOutput(source, budget, ())


def _legacy_fixup_projection(count, budget):
    integer = budget._rounded(sys.getsizeof((1 << 64) - 1))
    quad = budget._rounded(sys.getsizeof((None, None, None, None)))
    return (count * (2 * quad + 6 * integer)
            + output._list_storage(count, budget, lists=4)
            + output._list_storage(count, budget, lists=2)
            + output._list_storage(count, budget, sorting=True)
            + 2 * budget._rounded((count + count // 8) * 8)
            + output._SetStorage(budget).reserve(count))


def test_owned_fixups_fit_real_reduced_projection_and_keep_exact_budget_refusal(tmp_path, monkeypatch):
    paths = _fixup_input(tmp_path, count=4096)
    expected = _link(paths, "public")
    budget = _factory_budget()
    peaks, legacy = [], []
    check, reserve = budget.check_peak, output._OwnedOutput.reserve_fixup

    def measured(raw, *, records=0, entries=0, label="stack-map"):
        peaks.append(raw + budget.retained_bytes + entries * budget.metadata_entry_bytes
                     + (budget._set_bytes(records) if records else 0))
        return check(raw, records=records, entries=entries, label=label)

    def reserved(state, count=1):
        result = reserve(state, count)
        legacy.append(state.known_bytes() - state.fixup_raw
                      + _legacy_fixup_projection(state.fixup_count, state.budget))
        return result

    monkeypatch.setattr(budget, "check_peak", measured)
    with monkeypatch.context() as patch:
        patch.setattr(output._OwnedOutput, "reserve_fixup", reserved)
        assert _staged_link(paths, budget) == expected
    limit = max(peaks)
    assert max(legacy) > limit
    assert _staged_link(paths, _factory_budget(limit)) == expected

    budget = _factory_budget()
    state = _projection_state(budget)
    state.reserve_fixup(8192)
    limit = state.known_bytes()
    exact = _projection_state(_factory_budget(limit))
    exact.reserve_fixup(8192)
    short = _projection_state(_factory_budget(limit - 1))
    with pytest.raises(wire.PreciseStackMapError, match="Mach-O owned fixup projections"):
        short.reserve_fixup(8192)
    assert short.fixup_count == short.fixup_raw == 0


def _assert_segment_starts(info, starts, *, file_offset=macho_exec.PAGE):
    assert struct.unpack_from("<IHHQIH", info) == (
        22 + 2 * len(starts), macho_exec.PAGE,
        macho_exec.DYLD_CHAINED_PTR_64_OFFSET, file_offset, 0, len(starts),
    )
    assert len(info) == 22 + 2 * len(starts)
    assert struct.unpack_from(f"<{len(starts)}H", info, 22) == tuple(starts)


@pytest.mark.parametrize("owned", (False, True))
def test_chained_starts_accept_exact_uint16_page_count_with_tiny_payloads(owned):
    page_count = 0xFFFF
    sites = [
        _sparse_fixup(8, 0x123456789),
        _sparse_fixup(page_count * macho_exec.PAGE - 8, (1 << 63) | 7),
    ]
    sink = _FixupWordSink() if owned else None
    info = macho_exec._build_seg_info(
        sites, macho_exec.TEXT_BASE, page_count * macho_exec.PAGE,
        macho_exec.PAGE, _owned_output=sink,
    )
    _assert_segment_starts(
        info, [8] + [0xFFFF] * (page_count - 2) + [macho_exec.PAGE - 8],
    )
    assert sum(len(out.data) for _, out, _, _ in sites) == 16
    for _, out, offset, word in sites:
        assert out.data == struct.pack("<Q", word)
    if owned:
        assert sink.writes == [(out, offset, word) for _, out, offset, word in sites]


@pytest.mark.parametrize("owned", (False, True))
def test_chained_starts_refuse_65536_pages_before_patching_any_word(owned):
    sites = [_sparse_fixup(0, 11), _sparse_fixup(0xFFFF * macho_exec.PAGE, 29)]
    before = [bytes(out.data) for _, out, _, _ in sites]
    sink = _FixupWordSink() if owned else None
    with pytest.raises(macho_exec.LinkError) as caught:
        macho_exec._build_seg_info(
            sites, macho_exec.TEXT_BASE, 0x10000 * macho_exec.PAGE,
            macho_exec.PAGE, _owned_output=sink,
        )
    assert str(caught.value) == (
        "chained-fixup page count 65536 exceeds uint16; "
        "last fixup page 65535 needs a different segment layout"
    )
    assert [bytes(out.data) for _, out, _, _ in sites] == before
    if owned:
        assert sink.writes == []


@pytest.mark.parametrize("site_count", (0, 1, 2))
def test_chained_starts_ignore_gibibyte_file_tail_without_backing_allocation(site_count):
    sites = [_sparse_fixup(0, 17), _sparse_fixup(16, (1 << 63) | 3)][:site_count]
    segment_size = (0x10000 + 64) * macho_exec.PAGE
    if not sites:
        with pytest.raises(macho_exec.LinkError, match="^chained-fixup segment has no sites$"):
            macho_exec._build_seg_info(
                sites, macho_exec.TEXT_BASE, segment_size, macho_exec.PAGE,
            )
        return
    # More than 65,535 file-backed pages are valid when the fixups end early.
    info = macho_exec._build_seg_info(
        sites, macho_exec.TEXT_BASE, segment_size, macho_exec.PAGE,
    )
    _assert_segment_starts(info, [0])
    assert sum(len(out.data) for _, out, _, _ in sites) <= 16
    assert sites[0][1].data == struct.pack("<Q", 17 | ((4 if site_count == 2 else 0) << 51))
    if site_count == 2:
        assert sites[1][1].data == struct.pack("<Q", (1 << 63) | 3)


@pytest.mark.parametrize("owned", (False, True))
def test_chained_starts_preserve_holes_and_exact_rebase_bind_words(owned):
    rebase = (0xAB << 36) | 0x123456789
    bind = (1 << 63) | (0xD7 << 24) | 0x123456
    first = _sparse_fixup(12, 0x87654321)
    second = _sparse_fixup(2 * macho_exec.PAGE + 4, rebase)
    third = _sparse_fixup(2 * macho_exec.PAGE + 20, bind)
    sink = _FixupWordSink() if owned else None
    info = macho_exec._build_seg_info(
        [third, first, second], macho_exec.TEXT_BASE, 9 * macho_exec.PAGE,
        macho_exec.PAGE, _owned_output=sink,
    )
    _assert_segment_starts(info, [12, 0xFFFF, 4])
    expected = [first[3], rebase | (4 << 51), bind]
    for site, word in zip((first, second, third), expected):
        assert site[1].data == struct.pack("<Q", word)
    if owned:
        assert sink.writes == [
            (site[1], 0, word) for site, word in zip((first, second, third), expected)
        ]


@pytest.mark.parametrize("segment_offset, payload_offset, payload_size, segment_size, message", (
    (-4, 0, 8, macho_exec.PAGE, "outside its segment"),
    (macho_exec.PAGE - 4, 0, 8, macho_exec.PAGE, "outside its segment"),
    (2, 0, 8, 2 * macho_exec.PAGE, "not a complete, four-byte-aligned pointer"),
    (0, -1, 8, 2 * macho_exec.PAGE, "not a complete, four-byte-aligned pointer"),
    (0, 0, 7, 2 * macho_exec.PAGE, "not a complete, four-byte-aligned pointer"),
    (0, 1, 8, 2 * macho_exec.PAGE, "not a complete, four-byte-aligned pointer"),
    (macho_exec.PAGE - 4, 0, 8, 2 * macho_exec.PAGE,
     "crosses a 0x4000-byte page boundary"),
))
def test_chained_starts_validate_pointer_and_segment_bounds_before_patching(
    segment_offset, payload_offset, payload_size, segment_size, message,
):
    sites = [
        _sparse_fixup(8, 7),
        _sparse_fixup(segment_offset, 11, payload_offset=payload_offset, payload_size=payload_size),
    ]
    before = [bytes(out.data) for _, out, _, _ in sites]
    sink = _FixupWordSink()
    with pytest.raises(macho_exec.LinkError, match=message):
        macho_exec._build_seg_info(
            sites, macho_exec.TEXT_BASE, segment_size, macho_exec.PAGE,
            _owned_output=sink,
        )
    assert [bytes(out.data) for _, out, _, _ in sites] == before
    assert sink.writes == []


def test_staged_tls_descriptors_precede_data_and_keep_aligned_template_offsets(tmp_path):
    def relocation(offset, symbol):
        return obj.Relocation(offset, symbol, spec.ARM64_RELOC_UNSIGNED, False, 3)

    def descriptors(suffix, targets):
        return obj.Section(
            "__thread_vars_" + suffix, "__DATA", data=b"\0" * 48,
            align_log2=3, flags=0x13,
            relocations=tuple(
                relocation(index * 24 + offset, symbol)
                for index, target in enumerate(targets)
                for offset, symbol in ((0, "__tlv_bootstrap"), (16, target))
            ),
        )

    sections = [
        obj.Section(
            "__text", "__TEXT", data=struct.pack("<I", 0xD65F03C0),
            align_log2=2, flags=obj.TEXT_SECTION_FLAGS,
            symbols=(obj.TextSymbol("_main", 0),),
        ),
        obj.Section(
            "__data_a", "__DATA", data=b"\0" * 8, align_log2=4,
            relocations=(relocation(0, "_main"),),
        ),
        obj.Section(
            "__thread_data_a", "__DATA", data=struct.pack("<Q", 11),
            align_log2=3, flags=0x11, symbols=(obj.TextSymbol("_tls_data_a", 0),),
        ),
        obj.Section("__data_b", "__DATA", data=b"x" * (2 * macho_exec.PAGE + 16), align_log2=3),
        descriptors("a", ("_tls_data_a", "_tls_data_b")),
        obj.Section(
            "__thread_data_b", "__DATA", data=struct.pack("<Q", 22),
            align_log2=3, flags=0x11, symbols=(obj.TextSymbol("_tls_data_b", 0),),
        ),
        descriptors("b", ("_tls_bss_a", "_tls_bss_b")),
        obj.Section("__bss_a", "__DATA", flags=spec.S_ZEROFILL, zerofill_size=32, align_log2=5),
        obj.Section(
            "__thread_bss_a", "__DATA", flags=0x12, zerofill_size=16,
            align_log2=4, symbols=(obj.TextSymbol("_tls_bss_a", 0),),
        ),
        obj.Section("__bss_b", "__DATA", flags=spec.S_ZEROFILL, zerofill_size=8, align_log2=3),
        obj.Section(
            "__thread_bss_b", "__DATA", flags=0x12, zerofill_size=16,
            align_log2=4, symbols=(obj.TextSymbol("_tls_bss_b", 8),),
        ),
    ]
    path = tmp_path / "aligned-tls.pco"
    path.write_bytes(native.encode_native_object(native.NativeObject.from_sections(
        sections, undefined=("__tlv_bootstrap",), weak_undefined=("__tlv_bootstrap",),
    )))
    paths = [str(path)]
    expected = _link(paths, "public")
    actual = _staged_link(paths)
    assert actual == expected
    _signature_matches(actual, _factory_budget())
    parsed = spec.parse_object(actual)
    data_sections = [section for section in parsed.sections() if section["segname_str"] == "__DATA"]
    assert [section["sectname_str"] for section in data_sections] == [
        "__thread_vars_a", "__thread_vars_b", "__data_a", "__data_b",
        "__thread_data_a", "__thread_data_b", "__thread_bss_a", "__thread_bss_b",
        "__bss_a", "__bss_b",
    ]
    by_name = {section["sectname_str"]: section for section in parsed.sections()}
    template = by_name["__thread_data_a"]["addr"]
    assert template % 16 == 0
    assert by_name["__thread_data_a"]["align"] == 3
    assert by_name["__thread_data_b"]["addr"] == template + 8
    assert by_name["__thread_bss_a"]["addr"] == template + 16
    assert by_name["__thread_bss_b"]["addr"] == template + 32
    assert struct.unpack_from("<2Q", actual, by_name["__thread_data_a"]["offset"]) == (11, 22)
    for name in ("__thread_bss_a", "__thread_bss_b", "__bss_a", "__bss_b"):
        assert by_name[name]["offset"] == 0
    for suffix, offsets in (("a", (0, 8)), ("b", (16, 40))):
        descriptor = by_name["__thread_vars_" + suffix]
        for index, offset in enumerate(offsets):
            assert struct.unpack_from("<3Q", actual, descriptor["offset"] + index * 24) == (
                (1 << 63) | (6 << 51), 0, offset,
            )
    main_offset = by_name["__text"]["addr"] - macho_exec.TEXT_BASE
    assert struct.unpack_from("<Q", actual, by_name["__data_a"]["offset"]) == (main_offset,)
    assert parsed.header["flags"] & 0x00800000

    command = parsed.command(spec.LC_DYLD_CHAINED_FIXUPS)
    _, _, fixup_offset, fixup_size = struct.unpack("<4I", command.raw)
    fixups = actual[fixup_offset:fixup_offset + fixup_size]
    _, starts_offset = struct.unpack_from("<2I", fixups)
    segments = [command.body for command in parsed.commands if command.cmd == spec.LC_SEGMENT_64]
    data_index = next(index for index, segment in enumerate(segments) if segment["segname_str"] == "__DATA")
    relative_info, = struct.unpack_from("<I", fixups, starts_offset + 4 + 4 * data_index)
    assert relative_info
    info_at = starts_offset + relative_info
    info_size, = struct.unpack_from("<I", fixups, info_at)
    _assert_segment_starts(
        fixups[info_at:info_at + info_size], [0], file_offset=segments[data_index]["fileoff"],
    )
    assert segments[data_index]["filesize"] > 2 * macho_exec.PAGE


@pytest.mark.parametrize("fault, message", (
    ("addend", "does not fit a chained bind"),
    ("high-rebase", "exceeds 36 bits"),
    ("negative-rebase", "format requires"),
    ("unaligned", "four-byte-aligned pointer"),
    ("page-crossing", "crosses a 0x4000-byte page boundary"),
))
def test_staged_fixup_error_parity(tmp_path, fault, message):
    paths = _fixup_input(tmp_path, fault=fault)
    errors = []
    kind = struct.error if fault == "negative-rebase" else macho_exec.LinkError
    for link in (lambda: _link(paths, "public"), lambda: _staged_link(paths)):
        with pytest.raises(kind, match=message) as caught:
            link()
        errors.append((type(caught.value), str(caught.value)))
    assert errors[0] == errors[1]


def test_staged_payloads_retire_each_section_and_before_single_final_backing(tmp_path, monkeypatch):
    paths = _stack_inputs(tmp_path, count=3, records=512, data_bytes=65536)
    expected = _link(paths, "public")
    budget = _factory_budget()
    pending = [host.prepare_executable_from_paths(paths, compact_budget=budget, _staged_rows=True)]
    source_ref = weakref.ref(pending[0])
    source_files = (pending[0]._host_row_store.stream, pending[0]._host_payload_store.stream)
    owners = _tracked_buffers(monkeypatch)
    begin, end = output._OwnedOutput.begin_section, output._OwnedOutput.end_section
    open_sizes, retired, stages = [], [], []

    def starts(state, source, section, out):
        assert not state.views and not state.buffers and state.active_payload is None
        assert all(owner.closed for owner in owners)
        begin(state, source, section, out)
        assert len(state.views) == len(state.buffers) == 1
        assert sum(not owner.closed for owner in owners) == 1
        open_sizes.append(len(out.data))

    def ends(state, out):
        end(state, out)
        assert type(out.data) is output._FilePayload
        assert not state.views and not state.buffers and state.active_payload is None
        assert all(owner.closed for owner in owners)
        retired.append(out.sectname)
        if not stages:
            stages.append(state.payload_file)

    def signing(_event):
        assert source_ref() is None
        assert all(stream.closed for stream in source_files + tuple(stages))
        assert sum(not owner.closed for owner in owners) == 1

    monkeypatch.setattr(output._OwnedOutput, "begin_section", starts)
    monkeypatch.setattr(output._OwnedOutput, "end_section", ends)
    actual = output.link_owned_executable(pending.pop(), compact_budget=budget, phase_callback=signing)
    assert actual == expected
    assert retired == ["__text", "__data", "__pcc_stackmaps"]
    assert len(open_sizes) == 3 and all(owner.closed for owner in owners)
    assert all(len(set(owner.addresses)) <= 1 for owner in owners)
    assert len(actual) == owners[-1].size


@pytest.mark.parametrize("under", (0, 1))
def test_staged_validation_replay_exact_admission_and_one_byte_refusal(tmp_path, monkeypatch, under):
    paths = _stack_inputs(tmp_path, records=64, data_bytes=128)
    budget = _factory_budget()
    budget.retained_bytes = 137
    pending = [host.prepare_executable_from_paths(paths, compact_budget=budget, _staged_rows=True)]
    files = (pending[0]._host_row_store.stream, pending[0]._host_payload_store.stream)
    owners = _tracked_buffers(monkeypatch)
    check = budget.check_peak
    replay = []

    def boundary(raw, *, records=0, entries=0, label="stack-map"):
        if label == "Mach-O owned payload validation replay":
            previous = budget.limit_bytes
            budget.limit_bytes = raw + budget.retained_bytes - under
            replay.append((raw, budget.limit_bytes, len(owners)))
            try:
                return check(raw, records=records, entries=entries, label=label)
            finally:
                budget.limit_bytes = previous
        return check(raw, records=records, entries=entries, label=label)

    monkeypatch.setattr(budget, "check_peak", boundary)
    if under:
        with pytest.raises(output.LinkError, match="Mach-O owned payload validation replay"):
            output.link_owned_executable(pending.pop(), compact_budget=budget)
        assert replay[0][0] + 137 == replay[0][1] + 1
        assert len(owners) == replay[0][2]
    else:
        actual = output.link_owned_executable(pending.pop(), compact_budget=budget)
        assert replay[0][0] + 137 == replay[0][1]
        _signature_matches(actual, budget)
    assert len(replay) == 1 and all(owner.closed for owner in owners)
    assert all(stream.closed for stream in files)
    assert budget.retained_bytes == 137


# Construct wire fixtures directly: the public encoder rejects the malformed
# inputs whose diagnostic order the private publication validator must preserve.
def _validation_payload(functions, *, locations=None, arch=wire.ARCH_AARCH64):
    if locations is None:
        register = 29 if arch == wire.ARCH_AARCH64 else 6
        locations = ((wire.LOCATION_STACK_INDIRECT, wire.LOCATION_MANAGED,
                      wire.POINTER_SIZE, register, wire.NO_BASE, -8,
                      wire.POINTER_SIZE),)
    data = bytearray(wire._HEADER.pack(
        wire.MAGIC, wire.VERSION, arch, wire.POINTER_SIZE,
        len(functions), len(locations), 0,
    ))
    for index, function in enumerate(functions, 1):
        records = function["records"]
        data.extend(wire._FUNCTION.pack(
            function.get("id", index), function.get("address", 0x1000 + index * 256),
            function.get("size", max(4, len(records) * 4)), function.get("frame", 32),
            len(records), function.get("flags", 0),
        ))
        for position, record in enumerate(records):
            if type(record) is int:
                record = {"id": record}
            data.extend(wire._RECORD.pack(
                record["id"], record.get("pc", position * 4),
                record.get("exception", wire.NO_OFFSET), record.get("continuation", 0),
                record.get("count", 1 if locations else 0), record.get("reserved_count", 0),
                record.get("kind", wire.SAFEPOINT_CALL), record.get("flags", 0),
                record.get("reserved_short", 0), record.get("index", 0),
            ))
    for location in locations:
        data.extend(wire._LOCATION.pack(*location))
    return bytes(data)


def _external_validator(monkeypatch, *, small_runs=True):
    from pcc.backend import macho_host_validation as validation

    monkeypatch.setattr(validation, "_SMALL_PAYLOAD", 0)
    if small_runs:
        # Three merge generations with a tiny fixture exercise run boundaries
        # without constructing compiler-sized metadata or changing admission.
        monkeypatch.setattr(validation, "_CHUNK_ROWS", 2)
        monkeypatch.setattr(validation, "_FANIN", 2)
    return validation


def _validation_error_parity(validation, payload, *, budget_bytes=50_000_000, **kwargs):
    errors = []
    for validate in (wire.validate_stack_map_payload, validation.validate_stack_map_payload):
        with pytest.raises(wire.PreciseStackMapError) as caught:
            validate(payload, compact_budget=_factory_budget(budget_bytes), **kwargs)
        errors.append((type(caught.value), str(caught.value)))
    assert errors[0] == errors[1]
    return errors[0][1]


@pytest.mark.parametrize("arch", (wire.ARCH_AARCH64, wire.ARCH_X86_64))
def test_external_validation_preserves_valid_wire_semantics(monkeypatch, arch):
    validation = _external_validator(monkeypatch)
    register = 29 if arch == wire.ARCH_AARCH64 else 6
    managed = wire.LOCATION_MANAGED
    locations = (
        (wire.LOCATION_STACK_INDIRECT, managed, 8, register, wire.NO_BASE, -8, 8),
        (wire.LOCATION_STACK_DIRECT, managed, 8, register, wire.NO_BASE, -16, 8),
        (wire.LOCATION_REGISTER, managed | wire.LOCATION_DERIVED
         | wire.LOCATION_RELOAD_REQUIRED, 8, 0, 0, 0, 8),
    )
    payload = _validation_payload((
        {"records": tuple({"id": value, "count": 3} for value in range(1, 10))},
        {"records": (
            {"id": 10, "count": 2, "index": 1},
            {"id": 11, "count": 0, "index": len(locations)},
            {"id": (1 << 64) - 1, "count": 3, "kind": wire.SAFEPOINT_CONTINUATION,
             "flags": wire.RECORD_SUSPENDED | wire.RECORD_HAS_EXCEPTION_EDGE,
             "continuation": 1, "exception": 9},
        )},
    ), locations=locations, arch=arch)
    before = wire.decode_stack_map(payload, expected_arch=arch, final_image=True)
    budget = _factory_budget()
    assert wire.validate_stack_map_payload(
        payload, expected_arch=arch, final_image=True, compact_budget=_factory_budget(),
    ) is None
    assert validation.validate_stack_map_payload(
        payload, expected_arch=arch, final_image=True, compact_budget=budget,
    ) is None
    assert wire.decode_stack_map(payload, expected_arch=arch, final_image=True) == before
    assert budget.record_visits == 2 * 12


@pytest.mark.parametrize("table_format", (None, "2", "3"))
def test_external_validation_preserves_complete_staged_macho_bytes(tmp_path, monkeypatch, table_format):
    if table_format is None:
        monkeypatch.delenv(wire.PRECISE_STACKMAP_FORMAT_ENV, raising=False)
    else:
        monkeypatch.setenv(wire.PRECISE_STACKMAP_FORMAT_ENV, table_format)
    paths = _stack_inputs(tmp_path, records=16, data_bytes=32)
    expected = _link(paths, "public")
    validation = _external_validator(monkeypatch)
    validate = validation.validate_stack_map_payload
    seen = []

    def checked(payload, **kwargs):
        assert kwargs["final_image"] is True and type(payload) is bytes
        budget = kwargs["compact_budget"]
        assert budget.unretained_payload_bytes(payload) == 0
        version = wire._payload_version(payload)
        assert version == (wire.VERSION if table_format == "2" else wire.RUNTIME_TABLE_VERSION)
        if version == wire.RUNTIME_TABLE_VERSION:
            wire.validate_runtime_table(payload, final_image=True)
            functions = wire.decode_runtime_table(payload)
            seen.append(tuple((function[1], len(function[4])) for function in functions))
        else:
            functions = wire.decode_stack_map(payload, final_image=True).functions
            seen.append(tuple((function.function_address, len(function.records)) for function in functions))
        return validate(payload, **kwargs)

    monkeypatch.setattr(validation, "validate_stack_map_payload", checked)
    actual = _staged_link(paths)
    assert actual == expected
    assert len(seen) == 1 and len(seen[0]) == 2
    assert all(address != 0 and records == 16 for address, records in seen[0])
    _signature_matches(actual, _factory_budget())


@pytest.mark.parametrize("record_id", (17, (1 << 64) - 1))
@pytest.mark.parametrize("cross_function", (False, True))
def test_external_validation_rejects_duplicate_ids_across_runs(monkeypatch, record_id, cross_function):
    validation = _external_validator(monkeypatch)
    first = [record_id] + [value for value in range(100, 116)]
    functions = ({"records": first}, {"records": [record_id]}) if cross_function else (
        {"records": first + [record_id]},
    )
    payload = _validation_payload(functions)
    assert _validation_error_parity(validation, payload) == "duplicate safepoint id"


def test_external_validation_rejects_duplicate_at_real_chunk_boundary(monkeypatch):
    validation = _external_validator(monkeypatch, small_runs=False)
    assert validation._CHUNK_ROWS > 0 and validation._FANIN >= 2
    high_id = (1 << 64) - 1
    records = [high_id] + list(range(1, validation._CHUNK_ROWS)) + [high_id]
    payload = _validation_payload(({"records": records},), locations=())
    # This fixture admits the real fixed-size sort workspace; it does not
    # change the production cap or the exact-admission boundary tests below.
    assert _validation_error_parity(
        validation, payload, budget_bytes=100_000_000,
    ) == "duplicate safepoint id"


@pytest.mark.parametrize("first_error", ("root", "duplicate", "same-record"))
def test_external_validation_keeps_first_semantic_error(monkeypatch, first_error):
    validation = _external_validator(monkeypatch)
    roots = (
        (wire.LOCATION_STACK_INDIRECT, wire.LOCATION_MANAGED, 8, 29, wire.NO_BASE, -8, 8),
        (wire.LOCATION_STACK_INDIRECT, wire.LOCATION_MANAGED, 8, 29, wire.NO_BASE, -40, 8),
    )
    # Reverse root-index order deliberately: a sorted cache must restore first
    # occurrence before validating roots and choosing between root/ID failures.
    records = [{"id": value, "index": 0} for value in range(100, 114)]
    if first_error == "root":
        records[0]["index"] = 1
        records[-1]["id"] = records[1]["id"]
        expected = "stack location -40 exceeds frame size 32"
    elif first_error == "duplicate":
        records[1]["id"] = records[0]["id"]
        records[-1]["index"] = 1
        expected = "duplicate safepoint id"
    else:
        records[-1].update(id=records[0]["id"], index=1, kind=255)
        expected = "duplicate safepoint id"
    payload = _validation_payload(({"records": records[:7]}, {"records": records[7:]}), locations=roots)
    assert _validation_error_parity(validation, payload) == expected


def test_external_validation_shared_roots_count_once_across_functions(monkeypatch):
    validation = _external_validator(monkeypatch)
    payload = _validation_payload(tuple(
        {"records": list(range(index * 4 + 1, index * 4 + 5))}
        for index in range(10)
    ))
    monkeypatch.setattr(wire, "MAX_LOCATIONS", 1)
    for validate in (wire.validate_stack_map_payload, validation.validate_stack_map_payload):
        budget = _factory_budget()
        validate(payload, final_image=True, compact_budget=budget)
        assert budget.record_visits == 80


@pytest.mark.parametrize("semantic_failure", (False, True))
def test_external_validation_keeps_distinct_frame_root_checks(monkeypatch, semantic_failure):
    validation = _external_validator(monkeypatch)
    payload = _validation_payload((
        {"frame": 32, "records": list(range(1, 8))},
        {"frame": 0 if semantic_failure else 16, "records": list(range(8, 15))},
    ))
    if semantic_failure:
        expected = "stack location -8 exceeds frame size 0"
    else:
        monkeypatch.setattr(wire, "MAX_LOCATIONS", 1)
        expected = "too much stack-map location validation work"
    assert _validation_error_parity(validation, payload) == expected


@pytest.mark.parametrize("fault, expected", (
    ("header", "truncated stack-map header"),
    ("function", "truncated function record"),
    ("record", "truncated safepoint record"),
    ("table", "truncated stack-map location table"),
    ("trailing", "trailing bytes after pcc stack-map payload"),
    ("reserved-count", "non-zero reserved stack-map field"),
    ("reserved-short", "non-zero reserved stack-map field"),
    ("outside-table", "stack-map record names locations outside its table"),
    ("zero-id", "safepoint id is outside uint64"),
    ("kind", "unknown safepoint kind 255"),
    ("pc", "safepoints must have ordered in-function instruction offsets"),
    ("record-flags", "unknown stack-map record flags"),
    ("exception-size", "exceptional successor exceeds function"),
    ("exception-flags", "exception-edge flag and offset disagree"),
    ("continuation", "continuation record needs one non-zero continuation id"),
    ("suspended", "suspended flag is reserved for continuation records"),
    ("root-kind", "unknown location kind 255"),
    ("root-raw", "unclassified raw pointer is not a managed stack-map location"),
    ("root-derived", "derived location must name an earlier base location"),
    ("unresolved", "final-image function address is unresolved"),
    ("architecture", "stack-map architecture does not match target"),
))
def test_external_validation_preserves_malformed_wire_diagnostics(monkeypatch, fault, expected):
    validation = _external_validator(monkeypatch)
    record = {"id": 1}
    function = {"records": [record]}
    root = [wire.LOCATION_STACK_INDIRECT, wire.LOCATION_MANAGED, 8, 29, wire.NO_BASE, -8, 8]
    edits = {
        "reserved-count": ("reserved_count", 1), "reserved-short": ("reserved_short", 1),
        "outside-table": ("index", 1), "zero-id": ("id", 0), "kind": ("kind", 255),
        "pc": ("pc", 4), "record-flags": ("flags", 128), "exception-size": ("exception", 4),
        "exception-flags": ("exception", 1), "continuation": ("kind", wire.SAFEPOINT_CONTINUATION),
        "suspended": ("flags", wire.RECORD_SUSPENDED),
    }
    if fault in edits:
        key, value = edits[fault]
        record[key] = value
    if fault == "root-kind":
        root[0] = 255
    elif fault == "root-raw":
        root[1] = 0
    elif fault == "root-derived":
        root[1] |= wire.LOCATION_DERIVED | wire.LOCATION_RELOAD_REQUIRED
        root[4] = 0
    elif fault == "unresolved":
        function["address"] = 0
    payload = _validation_payload((function,), locations=(root,))
    cuts = {
        "header": wire._HEADER.size - 1,
        "function": wire._HEADER.size + wire._FUNCTION.size - 1,
        "record": wire._HEADER.size + wire._FUNCTION.size + wire._RECORD.size - 1,
        "table": len(payload) - 1,
    }
    if fault in cuts:
        payload = payload[:cuts[fault]]
    elif fault == "trailing":
        payload += b"x"
    kwargs = {"final_image": True}
    if fault == "architecture":
        kwargs["expected_arch"] = wire.ARCH_X86_64
    assert _validation_error_parity(validation, payload, **kwargs) == expected


def test_external_validation_structural_errors_precede_semantic_errors(monkeypatch):
    validation = _external_validator(monkeypatch)
    payload = _validation_payload((
        {"records": [{"id": 0, "kind": 255}, {"id": 1, "reserved_short": 1}]},
    ))
    assert _validation_error_parity(validation, payload) == "non-zero reserved stack-map field"


def test_external_validation_retains_original_two_record_visits(monkeypatch):
    validation = _external_validator(monkeypatch)
    payload = _validation_payload(({"records": list(range(1, 18))},))
    for under in (0, 1):
        budget = _factory_budget()
        budget.record_visit_limit = 34 - under
        if under:
            with pytest.raises(wire.PreciseStackMapError, match="record work budget exceeded"):
                validation.validate_stack_map_payload(payload, compact_budget=budget)
        else:
            validation.validate_stack_map_payload(payload, compact_budget=budget)
            assert budget.record_visits == 34


@pytest.mark.parametrize("retained_owner", (False, True))
def test_external_validation_exact_workspace_admission_and_one_byte_refusal(monkeypatch, retained_owner):
    validation = _external_validator(monkeypatch)
    payload = _validation_payload(({"records": list(range(1, 18))},))
    observed = []
    probe = _factory_budget()
    probe.retained_bytes = 137
    original_check = probe.check_peak

    def measured(raw, *, records=0, entries=0, label="stack-map"):
        estimate = raw + probe.retained_bytes + entries * probe.metadata_entry_bytes
        if records:
            estimate += probe._set_bytes(records)
        observed.append((estimate, label))
        return original_check(raw, records=records, entries=entries, label=label)

    monkeypatch.setattr(probe, "check_peak", measured)
    adapter = output._ValidationBudget(probe, payload) if retained_owner else probe
    validation.validate_stack_map_payload(payload, compact_budget=adapter)
    peak = max(estimate for estimate, _ in observed)
    assert (peak, "Mach-O owned validation workspace") in observed
    for under in (0, 1):
        budget = _factory_budget(peak - under)
        budget.retained_bytes = 137
        fixed_limit = budget.limit_bytes
        admitted = []
        check = budget.check_peak

        def fixed(raw, *, records=0, entries=0, label="stack-map"):
            assert budget.limit_bytes == fixed_limit
            admitted.append(label)
            return check(raw, records=records, entries=entries, label=label)

        monkeypatch.setattr(budget, "check_peak", fixed)
        adapter = output._ValidationBudget(budget, payload) if retained_owner else budget
        if under:
            with pytest.raises(wire.PreciseStackMapError, match="Mach-O owned validation workspace"):
                validation.validate_stack_map_payload(payload, compact_budget=adapter)
        else:
            validation.validate_stack_map_payload(payload, compact_budget=adapter)
            assert budget.record_visits == 34
        assert "Mach-O owned validation workspace" in admitted
        assert budget.limit_bytes == fixed_limit and budget.retained_bytes == 137


class _ValidationSpool:
    """Real anonymous file with partial-I/O faults at the private sort boundary."""

    def __init__(self, raw, run):
        self.raw, self.run = raw, run
        run["files"].append(self)

    @property
    def closed(self):
        return self.raw.closed

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.raw.close()
        if self.run["close_fault"]:
            self.run["hits"].append("close")
            raise OSError("deliberate validation close failure")

    def _request(self, operation, size):
        import errno

        assert 0 < size <= 64 * 1024
        self.run["requests"].append((operation, size))
        fault = self.run["fault"]
        if fault is not None and fault.startswith(operation + "-"):
            self.run["hits"].append(fault)
            fault = fault[len(operation) + 1:]
            if fault == "enospc":
                raise OSError(errno.ENOSPC, "deliberate validation storage exhaustion")
            if fault == "error":
                raise OSError(errno.EIO, "deliberate validation read failure")
            if fault == "zero":
                return 0
            if fault == "oversized":
                return size + 1
        if self.run["short_io"]:
            return max(1, size // 2)
        return size

    def write(self, span):
        size = self._request("write", len(span))
        if size == 0 or size > len(span):
            return size
        with memoryview(span)[:size] as part:
            return self.raw.write(part)

    def read(self, size):
        amount = self._request("read", size)
        if amount > size:
            return b"\0" * amount
        return self.raw.read(amount)

    def readinto(self, span):
        size = self._request("read", len(span))
        if size == 0 or size > len(span):
            return size
        with memoryview(span)[:size] as part:
            return self.raw.readinto(part)


def _validation_spools(monkeypatch, validation, *, fault=None, short_io=False, close_fault=False):
    run = {"fault": fault, "short_io": short_io, "close_fault": close_fault,
           "files": [], "hits": [], "requests": []}
    temporary = validation.tempfile.TemporaryFile

    def temporary_file(*args, **kwargs):
        return _ValidationSpool(temporary(*args, **kwargs), run)

    monkeypatch.setattr(validation.tempfile, "TemporaryFile", temporary_file)
    return run


def test_external_validation_short_io_is_bounded_and_closes_every_run(monkeypatch):
    validation = _external_validator(monkeypatch)
    payload = _validation_payload((
        {"records": list(range(1, 18))}, {"records": list(range(18, 35))},
    ))
    run = _validation_spools(monkeypatch, validation, short_io=True)
    budget = _factory_budget()
    budget.retained_bytes = 137
    validation.validate_stack_map_payload(payload, final_image=True, compact_budget=budget)
    assert budget.record_visits == 68 and budget.retained_bytes == 137
    assert run["files"] and all(stream.closed for stream in run["files"])
    assert {operation for operation, _ in run["requests"]} == {"read", "write"}
    assert max(size for _, size in run["requests"]) <= 64 * 1024
    wire.validate_stack_map_payload(payload, final_image=True, compact_budget=_factory_budget())


@pytest.mark.parametrize("fault", (
    "write-zero", "write-oversized", "write-enospc",
    "read-zero", "read-oversized", "read-error",
))
def test_external_validation_io_failure_closes_every_run(monkeypatch, fault):
    validation = _external_validator(monkeypatch)
    payload = _validation_payload(({"records": list(range(1, 18))},))
    run = _validation_spools(monkeypatch, validation, fault=fault)
    budget = _factory_budget()
    budget.retained_bytes = 137
    with pytest.raises((wire.PreciseStackMapError, OSError)) as retained:
        validation.validate_stack_map_payload(payload, compact_budget=budget)
    assert retained.value is not None and str(retained.value)
    assert fault in run["hits"]
    assert run["files"] and all(stream.closed for stream in run["files"])
    assert budget.retained_bytes == 137


def test_external_validation_close_failure_does_not_abandon_other_runs(monkeypatch):
    validation = _external_validator(monkeypatch)
    payload = _validation_payload(({"records": list(range(1, 18))},))
    run = _validation_spools(monkeypatch, validation, close_fault=True)
    budget = _factory_budget()
    budget.retained_bytes = 137
    with pytest.raises((wire.PreciseStackMapError, OSError)) as retained:
        validation.validate_stack_map_payload(payload, compact_budget=budget)
    assert "close" in str(retained.value)
    assert run["hits"] and all(hit == "close" for hit in run["hits"])
    assert run["files"] and all(stream.closed for stream in run["files"])
    assert budget.retained_bytes == 137


def test_external_validation_small_payload_keeps_public_validator_fastpath(monkeypatch):
    from pcc.backend import macho_host_validation as validation

    payload = _validation_payload(({"records": [1, (1 << 64) - 1]},))
    assert 0 < len(payload) < validation._SMALL_PAYLOAD == 1 << 20
    budget = _factory_budget()
    original = wire.validate_stack_map_payload
    calls = []

    def checked(value, **kwargs):
        calls.append((value is payload, kwargs))
        return original(value, **kwargs)

    def unexpected_spool(*args, **kwargs):
        raise AssertionError("small public validation must not create sort runs")

    monkeypatch.setattr(wire, "validate_stack_map_payload", checked)
    monkeypatch.setattr(validation.tempfile, "TemporaryFile", unexpected_spool)
    validation.validate_stack_map_payload(
        payload, expected_arch=wire.ARCH_AARCH64, final_image=True, compact_budget=budget,
    )
    assert calls == [(True, {"expected_arch": wire.ARCH_AARCH64,
                             "final_image": True, "compact_budget": budget})]
    assert budget.record_visits == 4


def test_external_validation_checks_root_errors_in_first_record_order(monkeypatch):
    validation = _external_validator(monkeypatch)
    roots = (
        (wire.LOCATION_STACK_INDIRECT, wire.LOCATION_MANAGED, 8, 29, wire.NO_BASE, -48, 8),
        (wire.LOCATION_STACK_INDIRECT, wire.LOCATION_MANAGED, 8, 29, wire.NO_BASE, -40, 8),
    )
    payload = _validation_payload((
        {"records": [{"id": value, "index": 1} for value in range(1, 10)]},
        {"records": [{"id": value, "index": 0} for value in range(10, 19)]},
    ), locations=roots)
    assert _validation_error_parity(validation, payload) == "stack location -40 exceeds frame size 32"


def test_external_validation_root_error_precedes_later_function_error(monkeypatch):
    validation = _external_validator(monkeypatch)
    payload = _validation_payload((
        {"frame": 0, "records": list(range(1, 10))},
        {"frame": 17, "records": list(range(10, 19))},
    ))
    assert _validation_error_parity(validation, payload) == "stack location -8 exceeds frame size 0"


def test_external_validation_root_count_is_part_of_exact_cache_key(monkeypatch):
    validation = _external_validator(monkeypatch)
    roots = (
        (wire.LOCATION_STACK_INDIRECT, wire.LOCATION_MANAGED, 8, 29, wire.NO_BASE, -8, 8),
        (wire.LOCATION_STACK_INDIRECT, wire.LOCATION_MANAGED, 8, 29, wire.NO_BASE, -16, 8),
    )
    payload = _validation_payload((
        {"records": [{"id": 1, "count": 1}, {"id": 2, "count": 2}]},
    ), locations=roots)
    monkeypatch.setattr(wire, "MAX_LOCATIONS", 2)
    assert _validation_error_parity(validation, payload) == "too much stack-map location validation work"


@pytest.mark.parametrize("fault, expected", (
    ("flags", "unknown stack-map location flags"),
    ("size", "managed locations must be one pointer wide"),
    ("extent", "managed locations must be one pointer wide"),
    ("register", "register 65535 is outside aarch64 ABI"),
    ("stack-register", "stack location must be relative to the target frame/stack register"),
    ("alignment", "managed stack location is not aligned"),
    ("register-offset", "register location cannot carry frame offset"),
    ("derived-base", "derived location base is not a base root"),
    ("derived-reload", "derived location must be reloaded after a relocating safepoint"),
    ("non-derived-base", "non-derived location carries a base index"),
    ("register-reload", "managed register location must reject stale post-safepoint SSA use"),
))
def test_external_validation_preserves_root_diagnostics(monkeypatch, fault, expected):
    validation = _external_validator(monkeypatch)
    root = [wire.LOCATION_STACK_INDIRECT, wire.LOCATION_MANAGED, 8, 29, wire.NO_BASE, -8, 8]
    roots = [root]
    if fault == "flags":
        root[1] |= 128
    elif fault == "size":
        root[2] = 4
    elif fault == "extent":
        root[6] = 16
    elif fault == "register":
        root[3] = 65535
    elif fault == "stack-register":
        root[3] = 0
    elif fault == "alignment":
        root[5] = -7
    elif fault in ("register-offset", "register-reload"):
        root[0], root[3] = wire.LOCATION_REGISTER, 0
        if fault == "register-offset":
            root[1] |= wire.LOCATION_RELOAD_REQUIRED
        else:
            root[5] = 0
    elif fault in ("derived-base", "derived-reload"):
        derived = [wire.LOCATION_STACK_INDIRECT, wire.LOCATION_MANAGED | wire.LOCATION_DERIVED,
                   8, 29, 0, -16, 8]
        roots.append(derived)
        if fault == "derived-base":
            derived[1] |= wire.LOCATION_RELOAD_REQUIRED
            roots.append([wire.LOCATION_STACK_INDIRECT, derived[1], 8, 29, 1, -24, 8])
    elif fault == "non-derived-base":
        root[4] = 0
    payload = _validation_payload((
        {"records": [{"id": value, "count": len(roots)} for value in range(1, 10)]},
    ), locations=roots)
    assert _validation_error_parity(validation, payload) == expected


@pytest.mark.parametrize("failure", ("root", "duplicate", "work"))
def test_external_validation_semantic_failure_releases_sort_files(monkeypatch, failure):
    validation = _external_validator(monkeypatch)
    records = list(range(1, 18))
    function = {"records": records}
    if failure == "root":
        function["frame"] = 0
    elif failure == "duplicate":
        records[-1] = records[0]
    payload = _validation_payload((function,))
    run = _validation_spools(monkeypatch, validation)
    budget = _factory_budget()
    budget.retained_bytes = 137
    if failure == "work":
        budget.record_visit_limit = len(records)
    with pytest.raises(wire.PreciseStackMapError) as retained:
        validation.validate_stack_map_payload(payload, compact_budget=budget)
    assert {"root": "stack location", "duplicate": "duplicate safepoint", "work": "record work"}[failure] in str(retained.value)
    assert run["files"] and all(stream.closed for stream in run["files"])
    assert budget.retained_bytes == 137


def test_external_validation_read_error_survives_close_failure(monkeypatch):
    import errno

    validation = _external_validator(monkeypatch)
    payload = _validation_payload(({"records": list(range(1, 18))},))
    run = _validation_spools(monkeypatch, validation, fault="read-error", close_fault=True)
    budget = _factory_budget()
    budget.retained_bytes = 137
    with pytest.raises(OSError, match="deliberate validation read failure") as retained:
        validation.validate_stack_map_payload(payload, compact_budget=budget)
    assert retained.value.errno == errno.EIO
    assert "read-error" in run["hits"] and "close" in run["hits"]
    assert run["files"] and all(stream.closed for stream in run["files"])
    assert budget.retained_bytes == 137


@pytest.mark.parametrize("change", ("truncate", "grow", "extent"))
def test_external_validation_rejects_spool_extent_changes(monkeypatch, change):
    validation = _external_validator(monkeypatch)
    payload = _validation_payload(({"records": list(range(1, 18))},))
    run = _validation_spools(monkeypatch, validation)
    original_check = validation._Runs._check_extent
    changed = []

    def corrupted(owner):
        if not changed:
            assert owner.extent > 0
            changed.append(owner.extent)
            if change == "truncate":
                owner.file.raw.truncate(owner.extent - 1)
            elif change == "grow":
                owner.file.raw.seek(owner.extent)
                assert owner.file.raw.write(b"x") == 1
            else:
                owner.extent += owner.codec.size
        return original_check(owner)

    monkeypatch.setattr(validation._Runs, "_check_extent", corrupted)
    budget = _factory_budget()
    budget.retained_bytes = 137
    with pytest.raises(wire.PreciseStackMapError, match="validation spool extent changed"):
        validation.validate_stack_map_payload(payload, compact_budget=budget)
    assert len(changed) == 1
    assert run["files"] and all(stream.closed for stream in run["files"])
    assert all(operation == "write" for operation, _ in run["requests"])
    assert budget.retained_bytes == 137
