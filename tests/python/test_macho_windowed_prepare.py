"""Bounded host-only PCO window ownership and final-image parity checks."""

import struct
import sys

import pytest

from pcc.backend import macho_exec, macho_link, macho_obj as obj, macho_spec as spec
from pcc.backend import native_object as native, precise_stackmap as maps


def _budget(limit=1_400_000):
    word = struct.calcsize("P")
    return maps.CompactStackMapBudget(
        limit, 1_000_000, set_header_bytes=sys.getsizeof(set()),
        set_entry_bytes=2 * word, integer_bytes=sys.getsizeof((1 << 64) - 1),
        metadata_entry_bytes=64 * word + sys.getsizeof((0, 0, 0))
        + 3 * sys.getsizeof((1 << 64) - 1) + sys.getsizeof(b""),
        allocation_quantum=2 * word,
    )


def _write_inputs(tmp_path, count=32, relocations=1024):
    paths = []
    for index in range(count):
        name = "_main" if index == 0 else "_unit" + str(index)
        sections = [
            obj.Section(
                "__text", "__TEXT", data=b"\xc0\x03\x5f\xd6",
                flags=obj.TEXT_SECTION_FLAGS, align_log2=2,
                symbols=(obj.TextSymbol(name, 0),),
            ),
            obj.Section(
                "__data", "__DATA", data=b"\0" * (8 * relocations),
                relocations=tuple(obj.Relocation(
                    8 * row, "_main", spec.ARM64_RELOC_UNSIGNED, False, 3,
                ) for row in range(relocations)),
            ),
        ]
        if index == 0:
            payload = maps.encode_stack_map(maps.PreciseStackMap(
                arch=maps.ARCH_AARCH64,
                functions=(maps.FunctionStackMap(
                    function_id=maps.function_id(name), function_address=0,
                    code_size=4, frame_size=0, records=(),
                ),),
            ))
            offset, = maps.function_address_offsets(payload)
            sections.append(obj.Section(
                "__pcc_stackmaps", "__DATA", data=payload,
                relocations=(obj.Relocation(
                    offset, name, spec.ARM64_RELOC_UNSIGNED, False, 3,
                ),),
            ))
        value = native.NativeObject.from_sections(
            sections, undefined=() if index == 0 else ("_main",),
        )
        path = tmp_path / (str(index) + ".pco")
        path.write_bytes(native.encode_native_object(value))
        paths.append(str(path))
    return paths


def _write_symbol_inputs(tmp_path, count=4, symbols=256):
    paths = []
    for index in range(count):
        names = ["_main" if index == offset == 0 else f"_unit{index}_{offset}"
                 for offset in range(symbols)]
        value = native.NativeObject.from_sections([
            obj.Section(
                "__text", "__TEXT", data=b"\xc0\x03\x5f\xd6" * symbols,
                flags=obj.TEXT_SECTION_FLAGS, align_log2=2,
                symbols=tuple(obj.TextSymbol(name, offset * 4)
                              for offset, name in enumerate(names)),
            ),
            obj.Section(
                "__data", "__DATA", data=b"\0" * 8, align_log2=3,
                relocations=(obj.Relocation(
                    0, names[-1], spec.ARM64_RELOC_UNSIGNED, False, 3,
                ),),
            ),
        ])
        path = tmp_path / f"symbols{index}.pco"
        path.write_bytes(native.encode_native_object(value))
        paths.append(str(path))
    return paths


def test_single_buffer_second_pass_admits_and_emits_identical_image(tmp_path, monkeypatch):
    from pathlib import Path
    from pcc.backend import macho_host_prepare as host

    paths = _write_inputs(tmp_path, count=4, relocations=4096)
    supported = host._single_buffer_window_supported
    assert supported()
    budget = _budget(700_000)
    budget.retained_bytes = 1024
    monkeypatch.setattr(host, "_single_buffer_window_supported", lambda: False)
    with pytest.raises(macho_link.LinkError, match="Mach-O PCO read/freeze window"):
        host.prepare_executable_from_paths(paths, compact_budget=budget)
    assert budget.retained_bytes == 1024
    monkeypatch.setattr(host, "_single_buffer_window_supported", supported)
    prepared = host.prepare_executable_from_paths(paths, compact_budget=budget)
    actual = macho_exec.link_prepared_executable(prepared, compact_budget=budget)
    eager = macho_exec.prepare_executable_object(
        [native.decode_packed_native_object(Path(path).read_bytes()) for path in paths],
        _consume_inputs=True, _source_view=True, compact_budget=_budget(10_000_000),
    )
    expected = macho_exec.link_prepared_executable(eager, compact_budget=_budget(10_000_000))
    assert actual == expected
    assert spec.parse_object(actual).header["filetype"] == spec.MH_EXECUTE
    assert budget.retained_bytes == 1024


@pytest.mark.parametrize("mode", (
    "3.13", "3.14", "3.15", "old-version", "new-version", "non-cpython",
    "free-threaded-build", "unverified-build", "runtime-gil-disabled",
    "debug-build", "debug-allocator", "narrow-word",
))
def test_single_buffer_window_requires_verified_host(monkeypatch, mode):
    from types import SimpleNamespace
    from pcc.backend import macho_host_prepare as host

    runtime = SimpleNamespace(implementation=SimpleNamespace(name="cpython"),
                              version_info=(3, 13, 5), _is_gil_enabled=lambda: True)
    monkeypatch.setattr(host, "sys", runtime)
    monkeypatch.setattr(host, "_WORD", 8)
    monkeypatch.delenv("PYTHONMALLOC", raising=False)
    build_flag = 0
    if mode in ("3.13", "3.14", "3.15"):
        runtime.version_info = tuple(int(part) for part in mode.split(".")) + (0,)
    elif mode == "old-version":
        runtime.version_info = (3, 12, 0)
    elif mode == "new-version":
        runtime.version_info = (3, 16, 0)
    elif mode == "non-cpython":
        runtime.implementation.name = "pcc"
    elif mode == "free-threaded-build":
        build_flag = 1
    elif mode == "unverified-build":
        build_flag = None
    elif mode == "runtime-gil-disabled":
        runtime._is_gil_enabled = lambda: False
    elif mode == "debug-build":
        runtime.gettotalrefcount = lambda: 0
    elif mode == "debug-allocator":
        monkeypatch.setenv("PYTHONMALLOC", "malloc_debug")
    elif mode == "narrow-word":
        monkeypatch.setattr(host, "_WORD", 4)
    monkeypatch.setitem(sys.modules, "sysconfig", SimpleNamespace(
        get_config_var=lambda _name: build_flag,
    ))
    assert host._single_buffer_window_supported() == (mode in ("3.13", "3.14", "3.15"))


@pytest.mark.parametrize("single_buffer", (False, True))
def test_window_rejects_before_allocating_storage(tmp_path, monkeypatch, single_buffer):
    from pcc.backend import macho_host_prepare as host

    path = tmp_path / "window.pco"
    path.write_bytes(b"x" * 8192)
    ledger = host._Ledger(_budget(8192))
    monkeypatch.setattr(host, "_single_buffer_window_supported", lambda: single_buffer)

    def forbidden(*args, **kwargs):
        pytest.fail("window storage allocated before admission")

    monkeypatch.setattr(host.io, "BytesIO", forbidden)
    monkeypatch.setattr(host, "bytearray", forbidden, raising=False)
    with pytest.raises(maps.PreciseStackMapError, match="Mach-O PCO read/freeze window"):
        host._read_window(str(path), ledger)
    assert ledger.retained == ledger.budget.retained_bytes == 0


def test_single_buffer_window_has_no_full_size_freeze_copy(tmp_path, monkeypatch):
    import hashlib
    import tracemalloc
    from pcc.backend import macho_host_prepare as host

    path = tmp_path / "window.pco"
    size = 256 * 1024
    path.write_bytes(b"x" * size)
    supported = host._single_buffer_window_supported
    assert supported()
    peaks = {}
    for single_buffer in (False, True):
        monkeypatch.setattr(host, "_single_buffer_window_supported", lambda: single_buffer)
        ledger = host._Ledger(_budget(2 * size + 8192))
        tracemalloc.start()
        try:
            raw, digest = host._read_window(str(path), ledger)
            _current, peaks[single_buffer] = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert type(raw) is bytes and len(raw) == size
        assert digest == hashlib.sha256(raw).digest()
        assert raw == b"x" * size
        raw = None
    assert peaks[False] >= 2 * size
    assert peaks[True] < size + 8192


@pytest.mark.parametrize("failure", (None, "short", "grow", "read-error", "digest"))
def test_single_buffer_read_releases_all_views_and_storage(tmp_path, monkeypatch, failure):
    import hashlib
    import io
    from pcc.backend import macho_host_prepare as host

    path = tmp_path / "window.pco"
    original = b"abcdef" * 4096
    path.write_bytes(original)
    owners, spans, calls = [], [], []
    original_open, original_buffer = open, io.BytesIO
    assert host._single_buffer_window_supported()

    class TrackedBuffer(original_buffer):
        def __init__(self, *args):
            super().__init__(*args)
            owners.append(self)

    class Stream:
        def __enter__(self):
            self.file = original_open(path, "rb", buffering=0)
            return self

        def __exit__(self, *args):
            self.file.close()

        def fileno(self):
            return self.file.fileno()

        def readinto(self, span):
            spans.append(span)
            calls.append(len(span))
            if failure == "read-error":
                raise OSError("deliberate read error")
            if failure == "short" and len(calls) > 1:
                return 0
            # Exercise multiple short reads without changing file contents.
            with span[:257] as piece:
                return self.file.readinto(piece)

        def read(self, size):
            return b"x" if failure == "grow" else self.file.read(size)

    monkeypatch.setattr(host.io, "BytesIO", TrackedBuffer)
    monkeypatch.setattr(host, "open", lambda *args, **kwargs: Stream(), raising=False)
    digest = b"wrong" if failure == "digest" else hashlib.sha256(original).digest()
    if failure is None:
        raw, actual_digest = host._read_window(str(path), host._Ledger(_budget()), len(original), digest)
        assert raw == original and actual_digest == digest
        assert len(calls) > 1
    else:
        message = {"short": "became shorter", "grow": "became longer",
                   "read-error": "deliberate read error", "digest": "content changed"}[failure]
        with pytest.raises((macho_link.LinkError, OSError), match=message):
            host._read_window(str(path), host._Ledger(_budget()), len(original), digest)
    assert owners and all(owner.closed for owner in owners)
    for span in spans:
        with pytest.raises(ValueError, match="released memoryview"):
            len(span)


@pytest.mark.parametrize("failure", ("budget", "malformed", "record-work"))
def test_windowed_error_reports_only_current_budget_refusal(tmp_path, monkeypatch, failure):
    from pcc.backend import macho_host_prepare as host

    path = tmp_path / "invalid.pco"
    path.write_bytes(b"x" * 10_000)
    budget = _budget(10_000 if failure == "budget" else 1_000_000)
    budget.retained_bytes = 123
    budget.last_rejection = "stale rejection from an earlier operation"
    if failure == "record-work":
        def fail(*args, **kwargs):
            raise maps.PreciseStackMapError("compact stack-map record work budget exceeded")

        monkeypatch.setattr(host, "_merge_paths", fail)
    with pytest.raises(macho_link.LinkError) as caught:
        host.prepare_executable_from_paths([str(path)], compact_budget=budget)
    message = str(caught.value)
    assert "stale rejection" not in message
    if failure == "budget":
        assert "Mach-O PCO read/freeze window" in message
        assert budget.last_rejection in message
        assert "retained=123" in message and "limit=10000" in message
        assert " ledger_retained=" in message
    else:
        assert ("magic" if failure == "malformed" else "record work budget") in message
        assert "ledger_retained=" not in message
        assert budget.last_rejection == ""
    assert budget.retained_bytes == 123


def test_symbol_phase_retirement_admits_and_emits_identical_image(tmp_path):
    from pathlib import Path
    from pcc.backend import macho_host_prepare as host

    paths = _write_symbol_inputs(tmp_path)
    # The old simultaneous six-map reservation exceeds this allowance even
    # though the phase-separated owners and final validation fit it.
    budget = _budget(5_000_000)
    budget.retained_bytes = 1024
    prepared = host.prepare_executable_from_paths(paths, compact_budget=budget)
    assert budget.retained_bytes == 1024
    actual = macho_exec.link_prepared_executable(prepared, compact_budget=budget)
    eager = macho_exec.prepare_executable_object(
        [native.decode_packed_native_object(Path(path).read_bytes()) for path in paths],
        _consume_inputs=True, _source_view=True, compact_budget=_budget(20_000_000),
    )
    expected = macho_exec.link_prepared_executable(eager, compact_budget=_budget(20_000_000))
    assert actual == expected
    assert spec.parse_object(actual).header["filetype"] == spec.MH_EXECUTE
    assert budget.retained_bytes == 1024


def test_symbol_planning_owners_retire_before_payload_windows(tmp_path, monkeypatch):
    import weakref
    from pcc.backend import macho_host_prepare as host

    paths = _write_symbol_inputs(tmp_path, count=3, symbols=16)
    original_definitions = host.ShardedSymbolDefinitions
    original_read = host._read_window
    original_rows = native._final_symbol_rows
    definition_owners, row_calls, windows = [], [], []

    class TrackedDefinitions(original_definitions):
        def __init__(self):
            super().__init__()
            definition_owners.append(weakref.ref(self))

    def read(path, ledger, expected_size=None, expected_digest=None):
        if expected_digest is not None:
            assert definition_owners and all(owner() is None for owner in definition_owners)
            assert row_calls == []
            windows.append(path)
        return original_read(path, ledger, expected_size, expected_digest)

    def final_rows(*args, **kwargs):
        row_calls.append(len(windows))
        return original_rows(*args, **kwargs)

    monkeypatch.setattr(host, "ShardedSymbolDefinitions", TrackedDefinitions)
    monkeypatch.setattr(host, "_read_window", read)
    monkeypatch.setattr(native, "_final_symbol_rows", final_rows)
    host.prepare_executable_from_paths(paths, compact_budget=_budget())
    assert windows == paths
    assert row_calls == [len(paths)]


def test_canonical_symbol_index_admits_sort_and_preserves_alias_order(monkeypatch):
    from pcc.backend import macho_host_prepare as host

    key = ("__TEXT", "__text")
    symbols = [obj.TextSymbol("_late", 8), obj.TextSymbol("_local", 4, external=False),
               obj.TextSymbol("_first_alias", 0), obj.TextSymbol("_second_alias", 0)]
    merged = {key: macho_link._MergedSection(*key, obj.TEXT_SECTION_FLAGS, symbols=symbols)}
    unresolved = ["_z", "_a"]
    budget = _budget()
    budget.retained_bytes = 1234
    ledger = host._Ledger(budget, retained=5678)
    keyed_symbols = []

    def tracked_sorted(values, *, key=None):
        if key is None:
            return sorted(values)

        def tracked_key(symbol):
            keyed_symbols.append(symbol)
            return key(symbol)

        return sorted(values, key=tracked_key)

    monkeypatch.setattr(host, "sorted", tracked_sorted, raising=False)
    result = host._canonical_symbol_indices(merged, [key], unresolved, ledger)
    # The locals/externals passes must not sort the class they discard.
    # Check calls, not elapsed time: old code keys every symbol twice.
    assert sorted(map(id, keyed_symbols)) == sorted(map(id, symbols))
    expected = native._final_symbol_rows(
        [obj.Section("__text", "__TEXT", symbols=tuple(symbols))],
        unresolved, set(), [0],
    )
    assert result == {symbol["name"]: index for index, symbol in enumerate(expected)}
    assert merged[key].symbols == symbols
    assert [symbol.name for symbol in symbols] == ["_late", "_local", "_first_alias", "_second_alias"]
    for name in result:
        assert any(name is symbol.name for symbol in symbols) or any(name is value for value in unresolved)
    assert ledger.retained == 5678 + ledger.rounded(sys.getsizeof(result)) + ledger.integers(len(result))
    assert budget.retained_bytes == 1234

    for targets in ({1: "_first_alias", 2: "_local", 3: "_absent"},
                    dict.fromkeys(range(len(symbols) + len(unresolved)), "_first_alias")):
        keyed_symbols.clear()
        stack_ledger = host._Ledger(_budget(), retained=5678)
        stack_result = host._canonical_stack_symbol_indices(
            merged, [key], unresolved, targets, stack_ledger,
        )
        if len(targets) < len(result):
            assert stack_result == {name: result[name] for name in targets.values() if name in result}
        else:
            assert stack_result == result
        assert sorted(map(id, keyed_symbols)) == sorted(map(id, symbols))
        assert merged[key].symbols == symbols
        assert stack_ledger.retained == (5678 + stack_ledger.rounded(sys.getsizeof(stack_result))
                                         + stack_ledger.integers(len(stack_result)))

    def forbidden(*args, **kwargs):
        pytest.fail("canonical sorting began before its allocation was admitted")

    monkeypatch.setattr(host, "sorted", forbidden, raising=False)
    tiny = host._Ledger(_budget(1024))
    with pytest.raises(maps.PreciseStackMapError, match="Mach-O canonical symbol index"):
        host._canonical_symbol_indices(merged, [key], unresolved, tiny)
    assert tiny.retained == 0


def test_windowed_preparation_admits_more_than_aggregate_raw_bytes(tmp_path):
    from pcc.backend.macho_host_prepare import prepare_executable_from_paths

    paths = _write_inputs(tmp_path)
    from pathlib import Path
    assert sum(Path(path).stat().st_size for path in paths) > _budget().limit_bytes
    eager = [native.decode_packed_native_object(Path(path).read_bytes()) for path in paths]
    with pytest.raises(macho_link.LinkError, match="Mach-O input stack-map snapshot"):
        macho_exec.prepare_executable_object(
            eager, _consume_inputs=True, _source_view=True, compact_budget=_budget(),
        )
    del eager
    prepared = prepare_executable_from_paths(paths, compact_budget=_budget())
    assert isinstance(prepared, native.OwnedMergedSourceView)
    assert sum(len(rows) for rows in prepared._raw_relocations) == (32 * 1024 + 1) * 12


@pytest.mark.parametrize("change", ("same-size", "longer", "shorter"))
def test_second_window_checks_exact_input_bytes(tmp_path, monkeypatch, change):
    from pathlib import Path
    from pcc.backend import macho_host_prepare as host

    paths = _write_inputs(tmp_path, count=2, relocations=8)
    read = host._read_window
    mutated = False

    def mutate(path, ledger, expected_size=None, expected_digest=None):
        nonlocal mutated
        if expected_digest is not None and not mutated:
            value = Path(path).read_bytes()
            if change == "same-size":
                value = value[:-1] + bytes([value[-1] ^ 1])
            elif change == "longer":
                value += b"x"
            else:
                value = value[:-1]
            Path(path).write_bytes(value)
            mutated = True
        return read(path, ledger, expected_size, expected_digest)

    monkeypatch.setattr(host, "_read_window", mutate)
    with pytest.raises(macho_link.LinkError, match="input (content|size) changed"):
        host.prepare_executable_from_paths(paths, compact_budget=_budget())
    assert mutated


def test_each_packed_owner_retires_before_next_window(tmp_path, monkeypatch):
    import weakref
    from pcc.backend import macho_host_prepare as host

    paths = _write_inputs(tmp_path, count=5, relocations=64)
    owners, reads = [], []
    construct, read = native.PackedNativeObject.__init__, host._read_window

    def tracked(self, *args, **kwargs):
        construct(self, *args, **kwargs)
        owners.append(weakref.ref(self))

    def checked(*args, **kwargs):
        assert all(owner() is None for owner in owners)
        reads.append(args[0])
        return read(*args, **kwargs)

    monkeypatch.setattr(native.PackedNativeObject, "__init__", tracked)
    monkeypatch.setattr(host, "_read_window", checked)
    prepared = host.prepare_executable_from_paths(paths, compact_budget=_budget())
    assert len(reads) == len(paths) * 2
    assert len(owners) == len(paths) * 2
    assert all(owner() is None for owner in owners)
    assert prepared._source_sections == ()
    assert all(type(payload) is bytes for payload in prepared._payloads)
    assert all(type(rows) is bytes for rows in prepared._raw_relocations)


def _malformed_special(section_name, flags, payload):
    def name(value):
        value = value.encode("ascii")
        return native._U32.pack(len(value)) + value

    segment = "__LD" if section_name == "__compact_unwind" else "__DATA"
    return b"".join((
        native._HEADER.pack(native.MAGIC, 2, 1),
        name("_main"), native._SYMBOL.pack(1, 0, native._SYMBOL_EXTERNAL),
        name("__TEXT"), name("__text"),
        native._SECTION.pack(obj.TEXT_SECTION_FLAGS, 2, 0, 4, 0, 0),
        b"\xc0\x03\x5f\xd6",
        name(segment), name(section_name),
        native._SECTION.pack(flags, 3, 0, len(payload), 0, 0), payload,
    ))


@pytest.mark.parametrize("special", ("stackmaps", "mod-init", "unwind"))
def test_malformed_special_payload_owners_are_admitted_before_decoder(
    tmp_path, monkeypatch, special,
):
    from pcc.backend import macho_host_prepare as host

    if special == "stackmaps":
        payload = maps.encode_stack_map(maps.PreciseStackMap(
            arch=maps.ARCH_AARCH64,
            functions=tuple(maps.FunctionStackMap(
                function_id=index + 1, function_address=0, code_size=4,
                frame_size=0, records=(),
            ) for index in range(128)),
        ))
        name, flags = "__pcc_stackmaps", obj.PCC_STACKMAP_SECTION_FLAGS
    elif special == "mod-init":
        name, flags, payload = "__mod_init_func", obj.MOD_INIT_SECTION_FLAGS, b"\0" * 8192
    else:
        name, flags, payload = "__compact_unwind", obj.COMPACT_UNWIND_SECTION_FLAGS, b"\0" * 8192
    path = tmp_path / "malformed.pco"
    path.write_bytes(_malformed_special(name, flags, payload))
    budget = _budget(22_000)
    assert 2 * path.stat().st_size + 4096 < budget.limit_bytes

    def forbidden(*_args, **_kwargs):
        pytest.fail("decoder allocated malformed special-section owners before admission")

    monkeypatch.setattr(native, "decode_packed_native_object", forbidden)
    with pytest.raises(macho_link.LinkError, match="Mach-O PCO decode owners"):
        host.prepare_executable_from_paths([str(path)], compact_budget=budget)


def test_stack_function_descriptor_count_is_not_bounded_by_symbols(tmp_path):
    from pcc.backend import macho_host_prepare as host

    count = 128
    payload = maps.encode_stack_map(maps.PreciseStackMap(
        arch=maps.ARCH_AARCH64,
        functions=tuple(maps.FunctionStackMap(
            function_id=index + 1, function_address=0, code_size=4, frame_size=0, records=(),
        ) for index in range(count)),
    ))
    sections = [
        obj.Section("__text", "__TEXT", data=b"\xc0\x03\x5f\xd6",
                    align_log2=2, flags=obj.TEXT_SECTION_FLAGS,
                    symbols=(obj.TextSymbol("_main", 0),)),
        obj.Section("__pcc_stackmaps", "__DATA", data=payload, align_log2=3,
                    relocations=tuple(obj.Relocation(offset, "_main", spec.ARM64_RELOC_UNSIGNED, False, 3)
                                      for offset in maps.function_address_offsets(payload))),
    ]
    path = tmp_path / "functions.pco"
    path.write_bytes(native.encode_native_object(native.NativeObject.from_sections(sections)))
    ledger = host._Ledger(_budget())
    descriptor = host._read_descriptor(str(path), ledger)
    assert len(descriptor.symbols) == 1
    assert len(descriptor.stack_functions[1]) == count
    assert descriptor.metadata_bytes >= host._model_bytes(descriptor.stack_functions, ledger.budget)
    assert host.prepare_executable_from_paths([str(path)], compact_budget=_budget())


def test_failed_second_pass_drops_traceback_owned_windows(tmp_path, monkeypatch):
    import weakref
    from pcc.backend import macho_host_prepare as host

    paths = _write_inputs(tmp_path, count=3, relocations=256)
    owners = []
    construct = native.PackedNativeObject.__init__

    def tracked(self, *args, **kwargs):
        construct(self, *args, **kwargs)
        owners.append(weakref.ref(self))

    def broken(*_args, **_kwargs):
        raise native.NativeObjectError("deliberate row failure")

    monkeypatch.setattr(native.PackedNativeObject, "__init__", tracked)
    monkeypatch.setattr(host, "_pack_final_link_rows", broken)
    with pytest.raises(macho_link.LinkError, match="deliberate row failure") as error:
        host.prepare_executable_from_paths(paths, compact_budget=_budget())
    assert all(owner() is None for owner in owners)
    pending, seen = [error.value], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        trace = current.__traceback__
        while trace is not None:
            if trace.tb_frame.f_code.co_filename.endswith("macho_host_prepare.py"):
                assert not any(isinstance(value, (bytes, bytearray, memoryview)) and len(value) > 1024
                               for value in trace.tb_frame.f_locals.values())
            trace = trace.tb_next
        pending.extend(item for item in (current.__cause__, current.__context__) if item is not None)


def test_detached_sections_retire_before_next_second_pass_read(tmp_path, monkeypatch):
    import weakref
    from pcc.backend import macho_host_prepare as host

    paths = _write_inputs(tmp_path, count=4, relocations=16)
    original_section = native.PackedNativeSection
    original_descriptor, original_read = host._read_descriptor, host._read_window
    groups = []
    second_pass = 0

    class TrackedSection(original_section):
        pass

    def descriptor(*args):
        result = original_descriptor(*args)
        groups.append([weakref.ref(section) for section in result.sections])
        return result

    def read(path, ledger, expected_size=None, expected_digest=None):
        nonlocal second_pass
        if expected_digest is not None:
            for group in groups[:second_pass]:
                assert all(section() is None for section in group)
            second_pass += 1
        return original_read(path, ledger, expected_size, expected_digest)

    monkeypatch.setattr(native, "PackedNativeSection", TrackedSection)
    monkeypatch.setattr(host, "_read_descriptor", descriptor)
    monkeypatch.setattr(host, "_read_window", read)
    host.prepare_executable_from_paths(paths, compact_budget=_budget())
    assert second_pass == len(paths)
    assert all(section() is None for group in groups for section in group)


@pytest.mark.parametrize("phase", (
    "Mach-O compact relocation join", "Mach-O final section metadata",
    "Mach-O consuming join workspace", "Mach-O consuming join backing",
))
def test_output_failure_releases_owned_rows_and_payloads(tmp_path, monkeypatch, phase):
    from pcc.backend import macho_host_prepare as host

    paths = _write_inputs(tmp_path, count=4, relocations=512)
    check = host._Ledger.check

    def reject(self, extra=0, *, label="Mach-O input window"):
        if label == phase:
            raise maps.PreciseStackMapError("deliberate output failure")
        return check(self, extra, label=label)

    monkeypatch.setattr(host._Ledger, "check", reject)
    with pytest.raises(macho_link.LinkError, match="deliberate output failure") as caught:
        host.prepare_executable_from_paths(paths, compact_budget=_budget())
    seen = set()

    def no_large_buffers(value):
        if id(value) in seen:
            return
        seen.add(id(value))
        if type(value) in (bytes, bytearray, memoryview):
            assert len(value) <= 1024
        elif type(value) in (list, tuple, set, frozenset):
            for item in value:
                no_large_buffers(item)
        elif type(value) is dict:
            for key, item in value.items():
                no_large_buffers(key)
                no_large_buffers(item)
        elif isinstance(value, (obj.Section, macho_link._MergedSection)):
            no_large_buffers(value.data)
            if isinstance(value, macho_link._MergedSection):
                no_large_buffers(value.data_parts)

    pending = [caught.value]
    while pending:
        current = pending.pop()
        trace = current.__traceback__
        while trace is not None:
            if trace.tb_frame.f_code.co_filename.endswith("macho_host_prepare.py"):
                no_large_buffers(trace.tb_frame.f_locals)
            trace = trace.tb_next
        pending.extend(item for item in (current.__cause__, current.__context__) if item is not None)
