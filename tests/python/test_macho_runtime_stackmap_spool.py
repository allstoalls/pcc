"""Finite host-only parity and allocation contracts for runtime spool output."""

import pytest

from pcc.backend import precise_stackmap as wire
from pcc.backend.macho_host_stackmap import merge_owned_stack_map_payloads
from tests.python.test_macho_stackmap_consumption import _payload
from tests.python.test_macho_windowed_prepare import _budget
from tests.python.test_precise_stackmap_runtime_table import _map


def _merge(payloads, runtime, single):
    return merge_owned_stack_map_payloads(
        list(payloads), compact_budget=_budget(100_000_000),
        single_buffer=single, runtime_table=runtime,
    )


@pytest.mark.parametrize("single", [False, True])
@pytest.mark.parametrize("arch", [wire.ARCH_AARCH64, wire.ARCH_X86_64])
@pytest.mark.parametrize("records", [0, 4, 2100])
@pytest.mark.parametrize("empty", [False, True])
def test_runtime_spool_exact_generic_bytes_and_offsets(single, arch, records, empty):
    payloads = [_payload((20, 40), arch=arch, records=records, empty_roots=empty),
                _payload((10, 30), arch=arch, records=records, empty_roots=empty)]
    v2, offsets = _merge(payloads, False, single)
    expected = wire.transcode_runtime_table(v2, offsets)
    actual = _merge(payloads, True, single)
    assert actual == expected
    wire.validate_runtime_table(actual[0], expected_arch=arch)


@pytest.mark.parametrize("single", [False, True])
@pytest.mark.parametrize("arch,register", [(wire.ARCH_AARCH64, 29), (wire.ARCH_X86_64, 6)])
def test_runtime_spool_exception_continuation_and_owned_roots(single, arch, register):
    payload = wire.encode_stack_map(_map(arch, register))
    v2, offsets = _merge([payload], False, single)
    assert _merge([payload], True, single) == wire.transcode_runtime_table(v2, offsets)


def test_runtime_spool_skips_v2_materialization_and_admits_freeze():
    budget = _budget(100_000_000)
    labels = []
    check = budget.check_peak
    def tracked(raw, **kw):
        labels.append(kw.get("label"))
        return check(raw, **kw)
    budget.check_peak = tracked
    actual = merge_owned_stack_map_payloads(
        [_payload((1, 2), records=20)], compact_budget=budget,
        single_buffer=False, runtime_table=True)
    assert wire._payload_version(actual[0]) == wire.RUNTIME_TABLE_VERSION
    assert "runtime spool materialize" in labels
    assert "spool merge materialize" not in labels


def test_runtime_spool_admission_fails_before_output_allocation(monkeypatch):
    from pcc.backend import macho_host_runtime_stackmap as runtime
    budget = _budget(100_000_000)
    check = budget.check_peak
    def refuse(raw, **kw):
        if kw.get("label") == "runtime spool materialize":
            raise wire.PreciseStackMapError("test admission refusal")
        return check(raw, **kw)
    budget.check_peak = refuse
    def forbidden(*args, **kwargs):
        pytest.fail("allocated output before admission")
    monkeypatch.setattr(runtime.io, "BytesIO", forbidden)
    with pytest.raises(wire.PreciseStackMapError, match="test admission refusal"):
        merge_owned_stack_map_payloads(
            [_payload((1,), records=8)], compact_budget=budget,
            single_buffer=True, runtime_table=True)


def test_runtime_spool_packed_index_growth_preserves_first_use_order():
    functions = tuple(wire.FunctionStackMap(
        function_id=fid, function_address=0, code_size=512, frame_size=1024,
        records=tuple(wire.SafepointRecord(
            fid * 1000 + i, i * 4, wire.SAFEPOINT_CALL,
            (wire.StackMapLocation(wire.LOCATION_STACK_INDIRECT,
                                  wire.LOCATION_MANAGED, register=29,
                                  offset=-8 * (i + 1)),),
        ) for i in range(96)),
    ) for fid in (9, 2))
    payloads = [wire.encode_stack_map(wire.PreciseStackMap(wire.ARCH_AARCH64, (f,)))
                for f in functions]
    v2, offsets = _merge(payloads, False, False)
    assert _merge(payloads, True, False) == wire.transcode_runtime_table(v2, offsets)


def test_runtime_spool_closes_all_storage_on_conversion_refusal(monkeypatch):
    from pcc.backend import macho_host_runtime_stackmap as runtime
    original = runtime.tempfile.TemporaryFile
    opened = []
    def tracked(*args, **kwargs):
        stream = original(*args, **kwargs)
        opened.append(stream)
        return stream
    def refuse(*args):
        raise wire.PreciseStackMapError("injected runtime location refusal")
    payload = _payload((1,), records=8)
    monkeypatch.setattr(runtime.tempfile, "TemporaryFile", tracked)
    monkeypatch.setattr(wire, "_runtime_location_word", refuse)
    with pytest.raises(wire.PreciseStackMapError, match="injected runtime location refusal"):
        _merge([payload], True, False)
    assert len(opened) == 7 and all(stream.closed for stream in opened)


def test_host_runtime_spool_executes_and_custom_transcoder_falls_back(tmp_path, monkeypatch):
    from pcc.backend import macho_exec, macho_host_prepare as host
    from tests.python.test_macho_stack_symbol_index import _write_sparse_stack_inputs
    paths = _write_sparse_stack_inputs(tmp_path)
    from pcc.backend import macho_host_runtime_stackmap as runtime
    budget = _budget(20_000_000)
    calls_to_spool = []
    emit = runtime.emit_runtime_table
    def tracked(*args, **kw):
        calls_to_spool.append(True)
        return emit(*args, **kw)
    monkeypatch.setattr(runtime, "emit_runtime_table", tracked)
    prepared = host.prepare_executable_from_paths(paths, compact_budget=budget)
    actual = macho_exec.link_prepared_executable(prepared, compact_budget=budget)
    assert calls_to_spool == [True]
    original = host.transcode_runtime_table
    calls = []
    def observer(*args):
        calls.append(len(args[0]))
        return original(*args)
    monkeypatch.setattr(host, "transcode_runtime_table", observer)
    fallback_budget = _budget(20_000_000)
    fallback = host.prepare_executable_from_paths(paths, compact_budget=fallback_budget)
    expected = macho_exec.link_prepared_executable(fallback, compact_budget=fallback_budget)
    assert actual == expected and len(calls) == 1
    assert calls_to_spool == [True]


@pytest.mark.parametrize("mutation", ["2", "invalid"])
@pytest.mark.parametrize("callback", ["check_peak", "charge_records", "unretained_payload_bytes", "scanner", "temporary_file"])
def test_custom_merge_callbacks_keep_late_format_semantics(tmp_path, monkeypatch, mutation, callback):
    from pcc.backend import macho_exec, macho_host_prepare as host, macho_link
    from pcc.backend import macho_host_runtime_stackmap as runtime
    from tests.python.test_macho_stack_symbol_index import _write_sparse_stack_inputs
    paths = _write_sparse_stack_inputs(tmp_path)
    monkeypatch.setenv("PCC_PRECISE_STACKMAP_FORMAT", mutation)
    if mutation == "2":
        expected_budget = _budget(20_000_000)
        expected = host.prepare_executable_from_paths(paths, compact_budget=expected_budget)
        expected_image = macho_exec.link_prepared_executable(expected, compact_budget=expected_budget)
    monkeypatch.setenv("PCC_PRECISE_STACKMAP_FORMAT", "3")
    budget = _budget(20_000_000)
    armed = False
    mutations = []
    def phase(event, **kw):
        nonlocal armed
        if event == "stackmap_merge_begin":
            armed = True
    def mutate():
        if armed:
            mutations.append(True)
            monkeypatch.setenv("PCC_PRECISE_STACKMAP_FORMAT", mutation)
    def no_spool(*args, **kw):
        pytest.fail("custom callback entered runtime spool")
    monkeypatch.setattr(runtime, "emit_runtime_table", no_spool)
    if callback == "temporary_file":
        original = runtime.tempfile.TemporaryFile
        def observed(*args, **kw):
            mutate()
            return original(*args, **kw)
        monkeypatch.setattr(runtime.tempfile, "TemporaryFile", observed)
    elif callback == "scanner":
        original = wire._scan_stack_map_payload
        def observed(*args, **kw):
            mutate()
            return original(*args, **kw)
        monkeypatch.setattr(wire, "_scan_stack_map_payload", observed)
    else:
        original = getattr(budget, callback)
        def observed(*args, **kw):
            mutate()
            return original(*args, **kw)
        setattr(budget, callback, observed)
    if mutation == "invalid":
        with pytest.raises(macho_link.LinkError, match="must be 2 or 3"):
            host.prepare_executable_from_paths(paths, compact_budget=budget, _phase_callback=phase)
    else:
        actual = host.prepare_executable_from_paths(paths, compact_budget=budget, _phase_callback=phase)
        assert macho_exec.link_prepared_executable(actual, compact_budget=budget) == expected_image
    assert armed and mutations


@pytest.mark.parametrize("customization", ["descriptor", "keyword_value", "defaults", "code"])
def test_custom_budget_provenance_uses_actual_fallback(tmp_path, monkeypatch, customization):
    from pcc.backend import macho_exec, macho_host_prepare as host
    from pcc.backend import macho_host_runtime_stackmap as runtime
    from tests.python.test_macho_windowed_prepare import _budget, _write_inputs

    monkeypatch.setenv("PCC_PRECISE_STACKMAP_FORMAT", "3")
    paths = _write_inputs(tmp_path)
    expected_budget = _budget(20_000_000)
    expected_prepared = host.prepare_executable_from_paths(paths, compact_budget=expected_budget)
    expected = macho_exec.link_prepared_executable(expected_prepared, compact_budget=expected_budget)
    budget = _budget(20_000_000)
    if customization == "descriptor":
        def forbidden_descriptor(self):
            raise AssertionError("eligibility invoked custom descriptor")
        monkeypatch.setattr(type(budget), "custom_observer", property(forbidden_descriptor), raising=False)
    elif customization == "keyword_value":
        monkeypatch.setitem(type(budget).check_peak.__kwdefaults__, "label", "custom-stack-map")
    elif customization == "defaults":
        monkeypatch.setattr(type(budget)._rounded, "__defaults__", (0,))
    else:
        function = type(budget).check_peak
        monkeypatch.setattr(function, "__code__", function.__code__.replace(co_name="custom_check_peak"))
    def forbidden_spool(*args, **kwargs):
        raise AssertionError("custom budget provenance entered runtime spool")
    monkeypatch.setattr(runtime, "emit_runtime_table", forbidden_spool)
    actual_prepared = host.prepare_executable_from_paths(paths, compact_budget=budget)
    actual = macho_exec.link_prepared_executable(actual_prepared, compact_budget=budget)
    assert actual == expected
