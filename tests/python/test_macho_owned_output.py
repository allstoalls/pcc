"""Private host output ownership; no native compiler or emitted execution."""

import ctypes
import gc
import io
import struct
import sys
import tracemalloc
import weakref

import pytest

from pcc.backend import macho_exec, macho_host_output as output
from pcc.backend import macho_host_validation as validation_boundary
from pcc.backend import macho_host_prepare as host, macho_codesign as codesign
from pcc.backend import macho_obj as obj, macho_spec as spec
from pcc.backend import native_object as native, precise_stackmap as maps
from tests.python.test_macho_stackmap_consumption import _payload
from tests.python.test_macho_windowed_prepare import _budget, _write_inputs


def _factory_budget(limit=50_000_000):
    from pcc.backend import stackmap_host_budget
    from tests.python.test_stackmap_host_budget import configured_host

    # Actual production factory; tree/RSS observations are a unit-test
    # fixture, not a claimed real Mac guard or an increased production cap.
    with pytest.MonkeyPatch.context() as patch:
        configured_host(patch, raw=str(limit + 11000))
        value = stackmap_host_budget.host_compact_stackmap_budget()
        word = stackmap_host_budget.struct.calcsize("P")
        quantum = 2 * word
        sizeof = stackmap_host_budget.sys.getsizeof

        def rounded(size):
            return ((size + quantum - 1) // quantum) * quantum

        # Supported CPython releases can have different object headers. Keep
        # the exact conservative formula, rather than a fixed runtime's sizes.
        integer = rounded(max(sizeof((1 << 64) - 1), sizeof(-(1 << 63))))
        metadata = 64 * word + rounded(sizeof((0, 0, 0))) + 3 * integer + rounded(sizeof(b""))
        header = rounded(sizeof(set()))
    assert value.limit_bytes == limit
    assert (value.integer_bytes, value.metadata_entry_bytes, value.set_header_bytes) == (
        integer, metadata, header,
    )
    assert value.allocation_quantum == value.set_entry_bytes == quantum
    assert value.record_visit_limit == 3 * (value.limit_bytes // 32)
    return value


def _stack_inputs(tmp_path, *, count=2, records=2048, data_bytes=32768):
    paths = []
    for index in range(count):
        name = "_main" if index == 0 else "_function" + str(index)
        # The fixture helper spaces IDs by 10000; keep large-function IDs
        # disjoint as well instead of silently introducing duplicate records.
        number = (index + 1) * (1 + records // 10000)
        payload = bytearray(_payload((number,), records=records))
        address, = maps.function_address_offsets(bytes(payload))
        payload[address:address + 8] = b"\0" * 8
        value = native.NativeObject.from_sections([
            obj.Section("__text", "__TEXT", data=b"\xc0\x03\x5f\xd6" * records,
                        flags=obj.TEXT_SECTION_FLAGS, align_log2=2,
                        symbols=(obj.TextSymbol(name, 0),)),
            obj.Section("__data", "__DATA", data=b"\0" * data_bytes, align_log2=3,
                        relocations=(obj.Relocation(0, "_main", spec.ARM64_RELOC_UNSIGNED, False, 3),)),
            obj.Section("__pcc_stackmaps", "__DATA", data=bytes(payload), align_log2=3,
                        relocations=(obj.Relocation(address, name, spec.ARM64_RELOC_UNSIGNED, False, 3),)),
        ], undefined=() if index == 0 else ("_main",))
        path = tmp_path / (str(index) + ".pco")
        path.write_bytes(native.encode_native_object(value))
        paths.append(str(path))
    return paths


def _tracked_buffers(monkeypatch):
    owners = []
    bytes_address = ctypes.pythonapi.PyBytes_AsString
    bytes_address.argtypes = [ctypes.py_object]
    bytes_address.restype = ctypes.c_void_p

    class TrackedBuffer(io.BytesIO):
        def __init__(self, initial):
            super().__init__(initial)
            self.size = len(initial)
            self.addresses = [bytes_address(initial)] if len(initial) > 1 else []
            owners.append(self)

        def getbuffer(self):
            view = super().getbuffer()
            if len(view) > 1:
                self.addresses.append(ctypes.addressof(ctypes.c_char.from_buffer(view)))
            return view

        def getvalue(self):
            value = super().getvalue()
            if len(value) > 1:
                self.addresses.append(bytes_address(value))
            return value

    monkeypatch.setattr(output.io, "BytesIO", TrackedBuffer)
    return owners


def test_owned_output_matches_public_bytes_and_signature(tmp_path):
    paths = _write_inputs(tmp_path, count=2, relocations=16)
    expected = macho_exec.link_prepared_executable(
        host.prepare_executable_from_paths(paths, compact_budget=_budget(20_000_000)),
        compact_budget=_budget(20_000_000),
    )
    budget = _budget(20_000_000)
    budget.retained_bytes = 137
    actual = output.link_owned_executable(
        host.prepare_executable_from_paths(paths, compact_budget=budget),
        compact_budget=budget,
    )
    assert type(actual) is bytes
    assert actual == expected
    assert budget.retained_bytes == 137
    signature = codesign.parse_signature(actual)
    output.check_signature_verification(actual, signature, budget)
    with memoryview(actual) as whole, whole[:signature.dataoff] as prefix:
        assert codesign.build_signature(
            prefix, identifier=signature.identifier,
            exec_seg_base=signature.exec_seg_base,
            exec_seg_limit=signature.exec_seg_limit,
            exec_seg_flags=signature.exec_seg_flags,
        ) == actual[signature.dataoff:]


def test_all_buffer_addresses_are_preserved_and_sources_retire_before_sign(tmp_path, monkeypatch):
    paths = _stack_inputs(tmp_path)
    budget = _budget(50_000_000)
    pending = [host.prepare_executable_from_paths(paths, compact_budget=budget)]
    source = weakref.ref(pending[0])
    owners = _tracked_buffers(monkeypatch)
    validated = []
    validate = validation_boundary.validate_stack_map_payload
    cache_before = dict(maps._VALIDATED_ADDRESS_FREE_TABLES)

    def checked_validate(payload, **kwargs):
        assert type(payload) is bytes
        assert kwargs["compact_budget"] is not None
        if maps._payload_version(payload) == maps.RUNTIME_TABLE_VERSION:
            maps.validate_runtime_table(payload, final_image=True)
            functions = maps.decode_runtime_table(payload)
            validated.append(tuple((fn[0], fn[1], fn[4]) for fn in functions))
        else:
            decoded = maps.decode_stack_map(payload, final_image=True)
            validated.append(tuple((fn.function_id, fn.function_address, fn.records)
                                   for fn in decoded.functions))
        return validate(payload, **kwargs)

    monkeypatch.setattr(validation_boundary, "validate_stack_map_payload", checked_validate)
    events = []

    def phase(event):
        events.append(event)
        assert source() is None
        assert len([owner for owner in owners if not owner.closed]) == 1

    actual = output.link_owned_executable(pending.pop(), compact_budget=budget, phase_callback=phase)
    assert events == ["sign_begin", "sign_end"]
    assert validated and sum(len(fn[2]) for fn in validated[0]) == 4096
    symbols = {row["name"]: row["n_value"] for row in spec.parse_object(actual).symbols()}
    assert tuple(fn[1] for fn in validated[0]) == (
        symbols["_main"] - macho_exec.TEXT_BASE,
        symbols["_function1"] - macho_exec.TEXT_BASE,
    )
    assert maps._VALIDATED_ADDRESS_FREE_TABLES == cache_before
    assert all(owner.closed for owner in owners)
    assert all(len(set(owner.addresses)) <= 1 for owner in owners)
    assert sum(len(owner.addresses) >= 4 for owner in owners) == 1  # stack freeze/thaw
    assert len(actual) == owners[-1].size


def test_owned_output_preserves_record_flags_and_record_relative_derived_roots(tmp_path, monkeypatch):
    # Version 2 retains record-relative derived roots and continuation metadata;
    # the default v3 runtime table intentionally cannot represent this fixture.
    monkeypatch.setenv(maps.PRECISE_STACKMAP_FORMAT_ENV, "2")
    base = maps.StackMapLocation(
        maps.LOCATION_STACK_INDIRECT, maps.LOCATION_MANAGED | maps.LOCATION_OWNED,
        register=29, offset=-8,
    )
    alternate = maps.StackMapLocation(
        maps.LOCATION_STACK_INDIRECT, maps.LOCATION_MANAGED,
        register=29, offset=-16,
    )
    derived = maps.StackMapLocation(
        maps.LOCATION_STACK_INDIRECT,
        maps.LOCATION_MANAGED | maps.LOCATION_OWNED | maps.LOCATION_DERIVED
        | maps.LOCATION_RELOAD_REQUIRED, register=29, offset=-24, base_index=0,
    )
    records = tuple(maps.SafepointRecord(
        i + 1, i * 4, maps.SAFEPOINT_CONTINUATION, locations,
        flags=maps.RECORD_HAS_EXCEPTION_EDGE | maps.RECORD_SUSPENDED,
        exceptional_offset=i * 4 + 1, continuation_id=i + 1,
    ) for i, locations in enumerate(((base, alternate, derived), (alternate, derived), ())))
    payload = maps.encode_stack_map(maps.PreciseStackMap(
        maps.ARCH_AARCH64, (maps.FunctionStackMap(123, 0, 64, 4096, records),),
    ))
    offset, = maps.function_address_offsets(payload)
    value = native.NativeObject.from_sections([
        obj.Section("__text", "__TEXT", data=b"\xc0\x03\x5f\xd6" * 16,
                    flags=obj.TEXT_SECTION_FLAGS, align_log2=2,
                    symbols=(obj.TextSymbol("_main", 0),)),
        obj.Section("__pcc_stackmaps", "__DATA", data=payload, align_log2=3,
                    relocations=(obj.Relocation(offset, "_main", spec.ARM64_RELOC_UNSIGNED, False, 3),)),
    ])
    path = tmp_path / "roots.pco"
    path.write_bytes(native.encode_native_object(value))
    validate = validation_boundary.validate_stack_map_payload
    observed = []

    def checked(payload, **kwargs):
        if kwargs.get("final_image"):
            decoded = maps.decode_stack_map(payload, final_image=True)
            observed.append(decoded.functions[0].records)
        return validate(payload, **kwargs)

    monkeypatch.setattr(validation_boundary, "validate_stack_map_payload", checked)
    pending = [host.prepare_executable_from_paths([str(path)], compact_budget=_budget(20_000_000))]
    expected = macho_exec.link_prepared_executable(pending.pop(), compact_budget=_budget(20_000_000))
    pending = [host.prepare_executable_from_paths([str(path)], compact_budget=_budget(20_000_000))]
    actual = output.link_owned_executable(pending.pop(), compact_budget=_budget(20_000_000))
    assert actual == expected
    assert observed and all(observation == records for observation in observed)
    assert records[0].locations[derived.base_index] == base
    assert records[1].locations[derived.base_index] == alternate


@pytest.mark.parametrize("failure", ("validator", "snapshot-escape", "sign", "sign-begin", "sign-end", "signature-size", "close", "final-close"))
def test_errors_release_backings_even_when_exception_is_retained(tmp_path, monkeypatch, failure):
    paths = _stack_inputs(tmp_path, records=64)
    budget = _budget(50_000_000)
    budget.retained_bytes = 137
    pending = [host.prepare_executable_from_paths(paths, compact_budget=budget)]
    source = weakref.ref(pending[0])
    owners = _tracked_buffers(monkeypatch)
    escaped = []
    validate = validation_boundary.validate_stack_map_payload
    sign = codesign.build_signature

    def validation(payload, **kwargs):
        if failure == "validator":
            view = memoryview(payload)
            raise ValueError("deliberate validator failure")
        result = validate(payload, **kwargs)
        if failure == "snapshot-escape":
            escaped.append(payload)
        return result

    def signature(payload, **kwargs):
        if failure == "sign":
            view = payload[:1]
            raise ValueError("deliberate signer failure")
        value = sign(payload, **kwargs)
        return value[:-1] if failure == "signature-size" else value

    def phase(event):
        if failure == event.replace("_", "-"):
            raise ValueError("deliberate callback failure")

    if failure in ("close", "final-close"):
        close = output.io.BytesIO.close
        def failed_close(self):
            close(self)
            if failure == "close" or (len(owners) >= 4 and self is owners[-1]):
                raise OSError("deliberate close failure")
        monkeypatch.setattr(output.io.BytesIO, "close", failed_close)
    monkeypatch.setattr(validation_boundary, "validate_stack_map_payload", validation)
    monkeypatch.setattr(codesign, "build_signature", signature)
    with pytest.raises((ValueError, output.LinkError, OSError)) as retained:
        output.link_owned_executable(pending.pop(), compact_budget=budget, phase_callback=phase)
    assert retained.value is not None
    assert all(owner.closed for owner in owners)
    assert source() is None
    assert budget.retained_bytes == 137
    if failure == "snapshot-escape":
        assert "snapshot alias" in str(retained.value)
        # The intentionally external immutable alias is refused, not mutated.
        validation_boundary.validate_stack_map_payload = validate
        validate(escaped[0], final_image=True, compact_budget=_budget(50_000_000))


@pytest.mark.parametrize("failure", (None, "write-zero", "read-zero", "write-error", "read-error", "write-large", "read-large", "close", "not-empty", "work"))
def test_spool_is_bounded_and_closed_on_all_paths(tmp_path, monkeypatch, failure):
    paths = _stack_inputs(tmp_path, records=64)
    budget = _budget(50_000_000)
    pending = [host.prepare_executable_from_paths(paths, compact_budget=budget)]
    owners = _tracked_buffers(monkeypatch)
    opened, requested = [], []
    temporary = output.tempfile.TemporaryFile

    class Spool:
        def __init__(self, *args, **kwargs):
            self.file = temporary(*args, **kwargs)
            opened.append(self.file)
            if failure == "not-empty":
                self.file.write(b"old gap")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.file.close()
            if failure == "close":
                raise OSError("deliberate spool close failure")

        def fileno(self):
            return self.file.fileno()

        def seek(self, *args):
            return self.file.seek(*args)

        def read(self, *args):
            return self.file.read(*args)

        def write(self, span):
            requested.append(len(span))
            if failure == "write-zero":
                return 0
            if failure == "write-error":
                raise OSError("deliberate ENOSPC")
            if failure == "write-large":
                return len(span) + 1
            if failure == "work":
                return self.file.write(span[:1])
            with span[:max(1, (len(span) + 1) // 2)] as part:
                return self.file.write(part)

        def readinto(self, span):
            requested.append(len(span))
            if failure == "read-zero":
                return 0
            if failure == "read-error":
                raise OSError("deliberate read failure")
            if failure == "read-large":
                return len(span) + 1
            with span[:max(1, (len(span) + 1) // 2)] as part:
                return self.file.readinto(part)

    monkeypatch.setattr(output.tempfile, "TemporaryFile", Spool)
    if failure is None:
        result = output.link_owned_executable(pending.pop(), compact_budget=budget)
        assert codesign.parse_signature(result).identifier == b"pcc-linked"
    else:
        with pytest.raises((output.LinkError, OSError)):
            output.link_owned_executable(pending.pop(), compact_budget=budget)
    assert all(file.closed for file in opened)
    assert all(owner.closed for owner in owners)
    assert all(size <= output._BLOCK for size in requested)


def test_external_source_or_payload_alias_is_refused_before_mutation(tmp_path):
    paths = _write_inputs(tmp_path, count=1, relocations=1)
    budget = _budget(20_000_000)
    source = host.prepare_executable_from_paths(paths, compact_budget=budget)
    with pytest.raises(output.LinkError, match="exclusively transferred"):
        output.link_owned_executable(source, compact_budget=budget)
    pending, source = [source], None
    payload = pending[0]._payloads[0]
    original = bytes(memoryview(payload))
    with pytest.raises(output.LinkError, match="escaped bytes alias"):
        output.link_owned_executable(pending.pop(), compact_budget=budget)
    assert payload == original


def test_escaped_signer_child_view_refuses_before_getvalue_copy(tmp_path, monkeypatch):
    paths = _stack_inputs(tmp_path, records=64)
    budget = _budget(50_000_000)
    pending = [host.prepare_executable_from_paths(paths, compact_budget=budget)]
    owners = _tracked_buffers(monkeypatch)
    escaped = []
    sign = codesign.build_signature

    def signature(payload, **kwargs):
        escaped.append(payload[:1])
        return sign(payload, **kwargs)

    monkeypatch.setattr(codesign, "build_signature", signature)
    with pytest.raises(BufferError):
        output.link_owned_executable(pending.pop(), compact_budget=budget)
    # A deliberately external active export cannot be revoked by its owner.
    # It must prevent getvalue; no image-sized copied result is ever created.
    assert len(owners[-1].addresses) == 2
    escaped[0].release()
    owners[-1].close()
    assert all(owner.closed for owner in owners)


def test_full_prepare_and_link_peak_removes_section_and_image_copies(tmp_path):
    paths = _stack_inputs(tmp_path, records=1024, data_bytes=2 * 1024 * 1024)
    measured, images, visits = {}, {}, {}
    for mode in ("public", "owned"):
        budget = _budget(100_000_000)
        gc.collect()
        tracemalloc.start()
        pending = [host.prepare_executable_from_paths(paths, compact_budget=budget)]
        if mode == "public":
            images[mode] = macho_exec.link_prepared_executable(pending.pop(), compact_budget=budget)
        else:
            images[mode] = output.link_owned_executable(pending.pop(), compact_budget=budget)
        measured[mode] = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        visits[mode] = budget.record_visits
    assert images["public"] == images["owned"]
    assert visits["public"] == visits["owned"]
    # Preparation itself now dominates this fixture. Include it rather than
    # comparing only new allocations after reusable backings already exist.
    assert measured["owned"] < measured["public"] * 0.85


@pytest.mark.parametrize("value", (bytearray(b"x"), memoryview(b"x")))
def test_public_validator_still_refuses_nonimmutable_payload(value):
    with pytest.raises(maps.PreciseStackMapError, match="immutable bytes"):
        maps.validate_stack_map_payload(value)


def test_owned_output_stays_out_of_native_import_closure():
    from pathlib import Path
    from pcc.frontends.python import pipeline_self_backend_link as pipeline
    from pcc.frontends.python.pipeline_dependency_closure import _package_import_targets
    from pcc.frontends.python.pipeline_import_scan import _without_inactive_implementation_imports

    path = Path(pipeline.__file__)
    source = path.read_text()
    marker = "from pcc.backend.macho_host_output import"
    assert marker in source
    assert marker not in _without_inactive_implementation_imports(source, implementation="pcc")
    assert marker in _without_inactive_implementation_imports(source, implementation="cpython")
    dependencies = {name for _path, name in _package_import_targets(
        str(path), pipeline.__name__, root_dir=str(Path(__file__).resolve().parents[2]),
    )}
    assert "pcc.backend.macho_host_output" not in dependencies


def test_exact_admission_boundary_and_rejection_preserves_sizes(tmp_path):
    paths = _stack_inputs(tmp_path, records=64)
    budget = _factory_budget()
    budget.retained_bytes = 137
    pending = [host.prepare_executable_from_paths(paths, compact_budget=budget)]
    checks = []
    check = budget.check_peak

    def measured(raw, *, records=0, entries=0, label="stack-map"):
        estimate = (raw + budget.retained_bytes + entries * budget.metadata_entry_bytes
                    + (budget._set_bytes(records) if records else 0))
        checks.append((estimate, label))
        return check(raw, records=records, entries=entries, label=label)

    budget.check_peak = measured
    expected = output.link_owned_executable(pending.pop(), compact_budget=budget)
    required, label = max(checks)
    for delta in (0, -1):
        budget = _factory_budget()
        budget.retained_bytes = 137
        pending = [host.prepare_executable_from_paths(paths, compact_budget=budget)]
        source = weakref.ref(pending[0])
        budget.limit_bytes = required + delta
        if delta == 0:
            actual = output.link_owned_executable(pending.pop(), compact_budget=budget)
            assert actual == expected
        else:
            with pytest.raises(output.LinkError) as refusal:
                output.link_owned_executable(pending.pop(), compact_budget=budget)
            text = str(refusal.value)
            assert label in text
            assert all(field in text for field in (
                "raw=", "estimate=", "entries=", "retained=", "limit=",
            ))
        assert source() is None
        assert budget.retained_bytes == 137


@pytest.mark.parametrize("label", (
    "Mach-O owned preparation projections", "Mach-O owned synthesized sections",
    "Mach-O owned fixup page tables", "Mach-O owned relocation order",
    "Mach-O owned fixup projections", "Mach-O owned final stack-map validation",
    "Mach-O owned region transfer", "Mach-O owned output wire extent",
    "Mach-O owned output spool", "Mach-O owned final backing",
    "Mach-O owned signature workspace", "owned Mach-O signature verification workspace",
))
def test_every_owned_label_exact_admission_and_one_byte_refusal(tmp_path, label):
    paths = _stack_inputs(tmp_path, records=16, data_bytes=8)
    for delta in (0, -1):
        budget = _factory_budget()
        budget.retained_bytes = 137
        pending = [host.prepare_executable_from_paths(paths, compact_budget=budget)]
        source = weakref.ref(pending[0])
        check = budget.check_peak
        hits = []

        def boundary(raw, *, records=0, entries=0, label="stack-map", selected=label):
            previous = budget.limit_bytes
            if label == selected:
                estimate = (raw + budget.retained_bytes + entries * budget.metadata_entry_bytes
                            + (budget._set_bytes(records) if records else 0))
                budget.limit_bytes = estimate + delta
                hits.append(estimate)
            try:
                return check(raw, records=records, entries=entries, label=label)
            finally:
                budget.limit_bytes = previous

        budget.check_peak = boundary
        def finish():
            image = output.link_owned_executable(pending.pop(), compact_budget=budget)
            signature = codesign.parse_signature(image)
            output.check_signature_verification(image, signature, budget)
            return image

        if delta == 0:
            actual = finish()
            assert type(actual) is bytes
        else:
            with pytest.raises(output.LinkError) as refusal:
                finish()
            assert label in str(refusal.value)
            assert "limit=" + str(hits[-1] - 1) in str(refusal.value)
        assert hits
        assert source() is None
        assert budget.retained_bytes == 137


def test_public_prepared_entry_keeps_default_hook_and_budget_failure(tmp_path, monkeypatch):
    paths = _write_inputs(tmp_path, count=1, relocations=4)
    prepare = macho_exec._prepare_executable_image
    observed = []

    def checked(*args, **kwargs):
        observed.append(kwargs.get("_owned_output"))
        return prepare(*args, **kwargs)

    monkeypatch.setattr(macho_exec, "_prepare_executable_image", checked)
    pending = [host.prepare_executable_from_paths(paths, compact_budget=_budget(20_000_000))]
    expected = macho_exec.link_prepared_executable(pending.pop())
    pending = [host.prepare_executable_from_paths(paths, compact_budget=_budget(20_000_000))]
    assert macho_exec.link_prepared_executable(pending.pop(), compact_budget=_budget(20_000_000)) == expected
    pending = [host.prepare_executable_from_paths(paths, compact_budget=_budget(20_000_000))]
    with pytest.raises(output.LinkError, match="Mach-O mutable section output"):
        macho_exec.link_prepared_executable(pending.pop(), compact_budget=_budget(1))
    assert observed == [None, None, None]


def test_shallow_container_bounds_cover_growth_without_key_double_counting():
    budget = _budget(50_000_000)
    mapping, values, sequence = {}, set(), []
    set_model = output._SetStorage(budget)
    for count in range(1, 5001):
        key = (1 << 40) + count
        mapping[key] = key
        values.add(key)
        sequence.append(key)
        assert output._dict_storage(count, budget) >= sys.getsizeof(mapping)
        assert set_model.reserve(count) >= sys.getsizeof(values)
        assert output._list_storage(count, budget) >= sys.getsizeof(sequence)
    # Larger transitions, without a repeated full-model walk at every insert:
    # dict index width 2 -> 4 and set's used > 50000 growth-rate branch.
    for count in (21845, 21846, 78642, 78643):
        mapping = dict.fromkeys(range(count))
        values = set(range(count))
        assert output._dict_storage(count, budget) >= sys.getsizeof(mapping)
        assert set_model.reserve(count) >= sys.getsizeof(values)


@pytest.mark.parametrize("instrument", ("trace", "profile", "monitoring"))
def test_instrumented_entry_is_refused_before_backing_transfer(tmp_path, monkeypatch, instrument):
    paths = _write_inputs(tmp_path, count=1, relocations=1)
    budget = _budget(20_000_000)
    pending = [host.prepare_executable_from_paths(paths, compact_budget=budget)]
    source = weakref.ref(pending[0])
    owners = _tracked_buffers(monkeypatch)
    prior_trace, prior_profile = sys.gettrace(), sys.getprofile()
    hook = lambda *args: hook
    try:
        if instrument == "trace":
            sys.settrace(hook)
        elif instrument == "profile":
            sys.setprofile(hook)
        else:
            sys.monitoring.use_tool_id(5, "owned-output-local-events-test")
            assert sys.monitoring.get_events(5) == 0
        with pytest.raises(output.LinkError, match="uninstrumented") as retained:
            output.link_owned_executable(pending.pop(), compact_budget=budget)
    finally:
        sys.settrace(prior_trace)
        sys.setprofile(prior_profile)
        if instrument == "monitoring":
            sys.monitoring.free_tool_id(5)
    assert owners == []
    assert retained.value is not None
    assert source() is None


@pytest.mark.parametrize("instrument", ("trace", "profile"))
def test_validator_enabling_instrumentation_cannot_trigger_thaw_copy(tmp_path, monkeypatch, instrument):
    paths = _stack_inputs(tmp_path, count=1, records=64)
    budget = _budget(20_000_000)
    pending = [host.prepare_executable_from_paths(paths, compact_budget=budget)]
    owners = _tracked_buffers(monkeypatch)
    validate = validation_boundary.validate_stack_map_payload
    prior_trace, prior_profile = sys.gettrace(), sys.getprofile()
    events = []

    def hook(frame, event, arg):
        events.append(event)
        return hook

    def validation(*args, **kwargs):
        result = validate(*args, **kwargs)
        if instrument == "trace":
            sys.settrace(hook)
        else:
            sys.setprofile(hook)
        return result

    monkeypatch.setattr(validation_boundary, "validate_stack_map_payload", validation)
    try:
        with pytest.raises(output.LinkError, match="uninstrumented"):
            output.link_owned_executable(pending.pop(), compact_budget=budget)
    finally:
        sys.settrace(prior_trace)
        sys.setprofile(prior_profile)
    assert events
    assert all(owner.closed for owner in owners)
    assert all(len(set(owner.addresses)) <= 1 for owner in owners)
    assert max(map(lambda owner: len(owner.addresses), owners)) == 3  # no thaw


def test_unsupported_private_output_uses_existing_public_pipeline(tmp_path, monkeypatch):
    from pcc.backend import stackmap_host_budget
    from pcc.frontends.python import pipeline_self_backend_link as pipeline

    paths = _write_inputs(tmp_path, count=1, relocations=4)
    pending = [host.prepare_executable_from_paths(paths, compact_budget=_budget(20_000_000))]
    expected = macho_exec.link_prepared_executable(pending.pop())
    public = macho_exec.link_prepared_executable
    calls = []

    def checked(*args, **kwargs):
        calls.append(True)
        return public(*args, **kwargs)

    def forbidden(*args, **kwargs):
        pytest.fail("unsupported host entered private output or external delegate")

    monkeypatch.setattr(output, "_owned_output_supported", lambda: False)
    monkeypatch.setattr(output, "link_owned_executable", forbidden)
    monkeypatch.setattr(stackmap_host_budget, "host_compact_stackmap_budget", lambda: _budget(20_000_000))
    monkeypatch.setattr(macho_exec, "link_prepared_executable", checked)
    monkeypatch.setattr(pipeline.subprocess, "run", forbidden)
    destination = tmp_path / "program"
    pipeline._owned_macho_link_in_process(
        asm_path=None, pcc_asm_inputs=(), pcc_native_object_inputs=tuple(paths),
        runtime_archive=None, extra_link_inputs=(), tmp_out_path=str(destination),
    )
    assert calls == [True]
    assert destination.read_bytes() == expected
