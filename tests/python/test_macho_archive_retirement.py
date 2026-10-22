"""Bounded host archive ownership and complete arm64 image parity.

These fixtures inspect emitted native bytes; they do not run arm64 code or
qualify the full bootstrap closure.
"""

from dataclasses import fields
import gc
import inspect
from pathlib import Path
import sys
import weakref

import pytest

from pcc.backend import (
    macho_archive,
    macho_exec,
    macho_host_prepare as host,
    macho_obj as obj,
    macho_spec as spec,
    native_object as native,
    precise_stackmap as maps,
)
from pcc.backend.ar_writer import write_archive
from tests.python.test_macho_archive_symbol_state import _unit
from tests.python.test_macho_section_target_index import _budget
from tests.python.test_macho_windowed_prepare import _write_inputs


def _member(name, references=()):
    payload = maps.encode_stack_map(maps.PreciseStackMap(
        arch=maps.ARCH_AARCH64,
        functions=(maps.FunctionStackMap(
            function_id=maps.function_id(name), function_address=0,
            code_size=16, frame_size=0, records=(),
        ),),
    ))
    address, = maps.function_address_offsets(payload)
    sections = [
        obj.Section(
            "__text", "__TEXT", data=b"\xc0\x03\x5f\xd6" + b"\x11" * 8 + b"\xc0\x03\x5f\xd6",
            flags=obj.TEXT_SECTION_FLAGS, align_log2=2,
            symbols=(obj.TextSymbol(name, 0), obj.TextSymbol("_shared_local", 0, external=False)),
            data_in_code=(obj.DataInCodeRegion(4, 8, 1),),
        ),
        obj.Section(
            "__pcc_stackmaps", "__DATA", data=payload, align_log2=3,
            relocations=(obj.Relocation(address, name, spec.ARM64_RELOC_UNSIGNED, False, 3),),
        ),
    ]
    if references:
        sections.append(obj.Section(
            "__data", "__DATA", data=b"\0" * (8 * len(references)), align_log2=3,
            relocations=tuple(obj.Relocation(i * 8, name, spec.ARM64_RELOC_UNSIGNED, False, 3)
                              for i, name in enumerate(references)),
        ))
    return native.NativeObject.from_sections(sections, undefined=references).to_macho()


def _fixture(tmp_path, selected=True):
    main = _unit("_main", ("_needed",) if selected else (), extra_symbols=(
        obj.TextSymbol("_shared_local", 0, external=False),
    ))
    pco = tmp_path / "main.pco"
    pco.write_bytes(native.encode_native_object(main))
    members = [
        ("needed.o", _member("_needed", ("_dependency",))),
        ("unused.o", _unit("_unused").to_macho()),
    ]
    archives = [write_archive(members), write_archive([("dependency.o", _member("_dependency"))])]
    paths = []
    for i, data in enumerate(archives):
        path = tmp_path / f"runtime-{i}.a"
        path.write_bytes(data)
        paths.append(str(path))
    return [str(pco)], archives, paths


def test_reader_owns_independent_exact_member_bytes(tmp_path):
    _paths, archives, _archive_paths = _fixture(tmp_path)
    expected = (_member("_needed", ("_dependency",)), _unit("_unused").to_macho())
    members = macho_archive.read_archive(archives[0])
    assert tuple(member.data for member in members) == expected
    for member in members:
        assert type(member.data) is bytes
        assert member.data is not archives[0]
        assert len(member.data) < len(archives[0])
    pulled, pending = macho_archive.select_members(members, {"_needed"})
    assert pulled == [expected[0]] and pending == {"_dependency"}
    assert pulled[0] is members[0].data


def test_fixed_archive_census_covers_every_declared_field():
    assert {item.name for item in fields(host._InputDescriptor)} == (
        set(host._ARCHIVE_DESCRIPTOR_FIELDS) | {"metadata_bytes", "archive_object"}
    )
    assert {item.name for item in fields(spec.LoadCommand)} == set(host._ARCHIVE_COMMAND_FIELDS)
    assert {item.name for item in fields(spec.MachOObject)} == {"header", "commands", "data"}


def test_retired_unused_archive_admits_payload_windows_under_same_budget(tmp_path):
    paths = _write_inputs(tmp_path, count=4, relocations=4096)
    archive = write_archive([("unused.o", obj.emit_object([
        obj.Section("__text", "__TEXT", data=b"\xc0\x03\x5f\xd6", flags=obj.TEXT_SECTION_FLAGS,
                    symbols=(obj.TextSymbol("_unused", 0),), align_log2=2),
        obj.Section("__data", "__DATA", data=b"\0" * (256 * 1024), align_log2=3),
    ]))])
    path = tmp_path / "unused.a"
    path.write_bytes(archive)
    # The former retained parser/archive reservation rejects the first
    # second-pass PCO read at 974,784 bytes against this unchanged allowance.
    budget = _budget(900_000)
    prepared = host.prepare_executable_from_paths(paths, archive_paths=[str(path)], compact_budget=budget)
    actual = macho_exec.link_prepared_executable(prepared, compact_budget=budget)
    expected = macho_exec.link_executable(
        [native.decode_packed_native_object(Path(path).read_bytes()) for path in paths], archives=[archive],
    )
    assert actual == expected
    assert spec.parse_object(actual).header["filetype"] == spec.MH_EXECUTE


@pytest.mark.parametrize("mode", ("paths", "bytes"))
@pytest.mark.parametrize("selected", (False, True))
def test_archive_owners_retire_before_pco_windows_and_preserve_image(tmp_path, monkeypatch, mode, selected):
    paths, archives, archive_paths = _fixture(tmp_path, selected)
    args = {"archive_paths": archive_paths} if mode == "paths" else {"archives": archives}
    expected = macho_exec.link_executable(
        [native.decode_packed_native_object(Path(path).read_bytes()) for path in paths], archives=archives,
    )
    original_read, original_descriptor = host._read_window, host._archive_descriptor
    full_archives, descriptor_refs, object_refs, observations = [], [], [], []

    def descriptor(*args):
        result = original_descriptor(*args)
        # The census includes stack-map functions, data-in-code, local names,
        # parsed load commands and their detached native member bytes.
        assert result.metadata_bytes == host._archive_descriptor_bytes(result, args[-1])
        assert any(result.stack_functions) and any(result.regions)
        object_refs.append(weakref.ref(result.archive_object))
        descriptor_refs.append(result.metadata_bytes)
        return result

    def read(path, ledger, expected_size=None, expected_digest=None):
        if expected_digest is not None:
            merge_frame = sys._getframe(1)
            assert merge_frame.f_code is host._merge_paths.__code__
            state = merge_frame.f_locals
            assert state["archive_buffers"] == []
            assert all(state[name] is None for name in (
                "members", "pulled", "member", "_pending", "already_defined", "undefined", "raw",
            ))
            selected_descriptors = [item for item in state["descriptors"] if item.archive_object is not None]
            assert len(selected_descriptors) == (2 if selected else 0)
            for item in selected_descriptors:
                assert type(item.archive_object.data) is bytes
                assert all(item.archive_object.data is not raw for raw in archives + full_archives)
            if mode == "paths":
                # Only this test's reference remains: no refcount/GC credit
                # is taken while a merge frame still owns the aggregate.
                # Check every merge local by identity before ignoring frames
                # in the GC census: borrowed stack references differ across
                # Python versions, and a new local alias must not be hidden.
                for raw in full_archives:
                    local_holders = [name for name, value in state.items() if value is raw]
                    assert local_holders == [], local_holders
                    holders = [holder for holder in gc.get_referrers(raw)
                               if holder is not full_archives and not inspect.isframe(holder)]
                    assert holders == [], [type(holder).__name__ for holder in holders]
            else:
                assert ledger.caller_bytes >= sum(sys.getsizeof(raw) for raw in archives)
            observations.append(ledger.retained)
        result = original_read(path, ledger, expected_size, expected_digest)
        if path in archive_paths:
            full_archives.append(result[0])
        return result

    monkeypatch.setattr(host, "_archive_descriptor", descriptor)
    monkeypatch.setattr(host, "_read_window", read)
    was_enabled = gc.isenabled()
    gc.disable()
    budget = _budget()
    budget.retained_bytes = 1234
    try:
        prepared = host.prepare_executable_from_paths(paths, compact_budget=budget, **args)
    finally:
        if was_enabled:
            gc.enable()
    assert observations and all(reference() is None for reference in object_refs)
    actual = macho_exec.link_prepared_executable(prepared, compact_budget=budget)
    assert actual == expected
    assert spec.parse_object(actual).header["filetype"] == spec.MH_EXECUTE
    names = {symbol["name"] for symbol in prepared.symbols()}
    assert "_unused" not in names
    assert ("_needed" in names) is selected
    if selected:
        assert "_dependency" in names and "_shared_local$link1" in names
        assert len(descriptor_refs) == 2
    assert budget.retained_bytes == 1234


@pytest.mark.parametrize("phase", (
    "Mach-O archive descriptor census scratch", "Mach-O archive retained ownership transition",
))
def test_archive_transition_refusal_keeps_reservation_and_releases_owners(tmp_path, monkeypatch, phase):
    paths, _archives, archive_paths = _fixture(tmp_path)
    original_descriptor, original_check = host._archive_descriptor, host._Ledger.check
    owners, refused = [], []

    def descriptor(*args):
        result = original_descriptor(*args)
        owners.append(weakref.ref(result.archive_object))
        return result

    def check(self, extra=0, *, label="Mach-O input window"):
        if label == phase:
            state = sys._getframe(1).f_locals
            if "archive_buffers" in state:
                assert state["archive_buffers"]
                assert self.retained >= state["archive_reservation"]
            refused.append((self.retained, extra))
            raise maps.PreciseStackMapError("deliberate archive ownership refusal")
        return original_check(self, extra, label=label)

    monkeypatch.setattr(host, "_archive_descriptor", descriptor)
    monkeypatch.setattr(host._Ledger, "check", check)
    budget = _budget()
    budget.retained_bytes = 1234
    with pytest.raises(Exception, match="deliberate archive ownership refusal") as caught:
        host.prepare_executable_from_paths(paths, archive_paths=archive_paths, compact_budget=budget)
    assert refused and all(reference() is None for reference in owners)
    error = caught.value
    while error is not None:
        trace = error.__traceback__
        while trace is not None:
            if trace.tb_frame.f_code.co_filename == host.__file__:
                assert not any(isinstance(value, (bytes, bytearray, memoryview)) and len(value) > 1024
                               for value in trace.tb_frame.f_locals.values())
            trace = trace.tb_next
        error = error.__cause__
    assert budget.retained_bytes == 1234


def test_archive_census_admits_scratch_before_walking_model(tmp_path, monkeypatch):
    paths, _archives, archive_paths = _fixture(tmp_path)
    original_check = host._Ledger.check

    def forbidden(*args):
        pytest.fail("archive model walked before its scratch was admitted")

    def check(self, extra=0, *, label="Mach-O input window"):
        if label == "Mach-O archive descriptor census scratch":
            raise maps.PreciseStackMapError("denied census scratch")
        return original_check(self, extra, label=label)

    monkeypatch.setattr(host, "_archive_descriptor_bytes", forbidden)
    monkeypatch.setattr(host._Ledger, "check", check)
    with pytest.raises(Exception, match="denied census scratch"):
        host.prepare_executable_from_paths(paths, archive_paths=archive_paths, compact_budget=_budget())


def test_selected_member_dies_before_its_retained_charge_is_debited(tmp_path, monkeypatch):
    paths, _archives, archive_paths = _fixture(tmp_path)
    original_descriptor = host._archive_descriptor
    original_setattr = host._Ledger.__setattr__
    owners, debits = [], []

    def descriptor(*args):
        result = original_descriptor(*args)
        owners.append(weakref.ref(result.archive_object))
        return result

    def assign(self, name, value):
        if name == "retained" and hasattr(self, name) and value < self.retained:
            state = sys._getframe(1).f_locals
            if "retired_bytes" in state and value == self.retained - state["retired_bytes"]:
                assert state["raw"] is state["packed"] is state["descriptor"] is None
                assert state["descriptors"][state["index"]] is None
                retired_archive_count = max(0, state["index"] - len(paths) + 1)
                assert all(reference() is None for reference in owners[:retired_archive_count])
                debits.append(state["index"])
        return original_setattr(self, name, value)

    monkeypatch.setattr(host, "_archive_descriptor", descriptor)
    monkeypatch.setattr(host._Ledger, "__setattr__", assign)
    host.prepare_executable_from_paths(paths, archive_paths=archive_paths, compact_budget=_budget())
    assert debits == [0, 1, 2]


def test_oversized_archive_survivor_census_fails_before_retirement(tmp_path, monkeypatch):
    paths, _archives, archive_paths = _fixture(tmp_path)
    original_check = host._Ledger.check
    transitions = []

    def check(self, extra=0, *, label="Mach-O input window"):
        if label == "Mach-O archive retained ownership transition":
            state = sys._getframe(1).f_locals
            assert state["archive_buffers"]
            assert self.retained >= state["archive_reservation"]
            transitions.append(extra)
        return original_check(self, extra, label=label)

    monkeypatch.setattr(host, "_archive_descriptor_bytes", lambda *args: 1 << 64)
    monkeypatch.setattr(host._Ledger, "check", check)
    budget = _budget()
    budget.retained_bytes = 1234
    with pytest.raises(Exception, match="invalid compact stack-map size"):
        host.prepare_executable_from_paths(paths, archive_paths=archive_paths, compact_budget=budget)
    assert transitions and budget.retained_bytes == 1234
