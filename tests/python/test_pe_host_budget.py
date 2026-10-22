"""Tiny host allocation/guard controls; the real Job test runs only on Windows."""

import json
from pathlib import Path
import struct
import sys
from types import SimpleNamespace

import pytest

from pcc.backend import pe_host_budget as host
from pcc.backend.precise_stackmap import PreciseStackMapError


MIB = 1024 * 1024
PROFILE = dict(set_header_bytes=10, set_entry_bytes=2, integer_bytes=3,
               metadata_entry_bytes=7, allocation_quantum=1)


def budget(limit=1_000_000, work=100):
    return host.PEHostBudget(limit, work, **PROFILE)


def receipt(**changes):
    return dict(schema=host.GUARD_SCHEMA, root_pid=11, root_creation_time=101,
                supervisor_pid=10, supervisor_creation_time=100,
                cap_bytes=1024 * MIB, deadline=200.0, **changes)


class Observation:
    def limits(self):
        return 0x2200, 1024 * MIB, 64 * MIB

    def pids(self):
        return (11, 12)

    def process(self, pid):
        return {10: (100, MIB, True), 11: (101, 16 * MIB, True),
                12: (102, 8 * MIB, True)}[pid]


def observe(value=None, api=None, **kwargs):
    return host._verified_allowance(
        receipt() if value is None else value, 1024 * MIB,
        kwargs.get("owner", 11), kwargs.get("parent", 10), api or Observation(),
        clock=kwargs.get("clock", lambda: 100.0),
    )


def test_verified_direct_root_uses_larger_peak_commit_and_current_tree_rss():
    assert observe() == (1024 - 64 - 128) * MIB
    api = Observation()
    api.process = lambda pid: (100 if pid == 10 else 101 if pid == 11 else 102,
                               100 * MIB if pid != 10 else MIB, True)
    assert observe(api=api) == (1024 - 200 - 128) * MIB


@pytest.mark.parametrize("field,value", (
    ("schema", "unknown"), ("root_pid", 12), ("root_pid", True),
    ("root_creation_time", 999), ("supervisor_pid", 99),
    ("supervisor_creation_time", 999), ("cap_bytes", 1025 * MIB),
    ("deadline", 100.0), ("deadline", float("nan")),
    ("deadline", float("inf")), ("deadline", "200"),
))
def test_invalid_or_different_launch_receipt_keeps_legacy_cap(field, value):
    evidence = receipt()
    evidence[field] = value
    assert observe(evidence) is None


def test_wrapper_or_descendant_is_not_admitted_as_the_direct_root():
    assert observe(owner=12) is None
    assert observe(parent=99) is None


@pytest.mark.parametrize("limits", (
    (0, 1024 * MIB, MIB), (0x2000, 1024 * MIB, MIB),
    (0x2200 | 0x800, 1024 * MIB, MIB), (0x2200 | 0x1000, 1024 * MIB, MIB),
    (0x2200 | 0x100, 1024 * MIB, MIB), (0x2200, 1025 * MIB, MIB),
    (0x2200, 1024 * MIB, 0), (0x2200, 1024 * MIB, 1024 * MIB),
))
def test_unknown_job_limits_or_no_headroom_keep_legacy_cap(limits):
    api = Observation()
    api.limits = lambda: limits
    assert observe(api=api) is None


@pytest.mark.parametrize("pids", ((), (12,), (11, 11), (11, -1)))
def test_incomplete_or_invalid_membership_is_never_zero_rss(pids):
    api = Observation()
    api.pids = lambda: pids
    assert observe(api=api) is None


@pytest.mark.parametrize("which", (1, 2))
def test_membership_change_in_either_bracket_is_rejected(which):
    api = Observation()
    calls = 0
    def pids():
        nonlocal calls
        calls += 1
        return (11,) if calls == which + 1 else (11, 12)
    api.pids = pids
    assert observe(api=api) is None


@pytest.mark.parametrize("fault", ("identity", "zero-rss", "exited", "supervisor-exited"))
def test_changed_process_identity_or_unmeasured_member_is_rejected(fault):
    api = Observation()
    original = api.process
    calls = {}
    def process(pid):
        calls[pid] = calls.get(pid, 0) + 1
        created, rss, alive = original(pid)
        if pid == 12:
            if fault == "identity" and calls[pid] == 2:
                created += 1
            if fault == "zero-rss":
                rss = 0
            if fault == "exited":
                alive = False
        if pid == 10 and fault == "supervisor-exited" and calls[pid] == 2:
            alive = False
        return created, rss, alive
    api.process = process
    assert observe(api=api) is None


def test_slow_observation_and_expired_original_deadline_are_rejected():
    moments = iter((100.0, 102.01))
    assert observe(clock=lambda: next(moments)) is None
    moments = iter((199.0, 200.0))
    assert observe(clock=lambda: next(moments)) is None


def test_missing_bad_or_failed_os_evidence_keeps_legacy_cap(tmp_path, monkeypatch):
    path = tmp_path / "guard.json"
    monkeypatch.setenv(host.GUARD_ENV, str(path))
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", str(1024 * MIB))
    assert host._windows_allowance() is None
    path.write_text("x" * 4097)
    assert host._windows_allowance() is None
    path.write_text(json.dumps(receipt()))
    monkeypatch.setattr(host, "_WindowsObservation", lambda: (_ for _ in ()).throw(OSError("denied")))
    assert host._windows_allowance() is None


@pytest.mark.parametrize("raw", ("²", "9" * 5000, "-1", "0", str(1 << 63), "true"),
                         ids=("non-ascii-digit", "overlong", "negative", "zero", "overflow", "text"))
def test_malformed_environment_cap_is_unavailable_not_an_exception(monkeypatch, raw):
    monkeypatch.setenv(host.GUARD_ENV, "unused-test-path")
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", raw)
    assert host._windows_allowance() is None


def test_deeply_nested_guard_json_is_unavailable(tmp_path, monkeypatch):
    path = tmp_path / "guard.json"
    path.write_text("[" * 1500 + "]" * 1500)
    monkeypatch.setenv(host.GUARD_ENV, str(path))
    monkeypatch.setenv("PCC_WORKER_TREE_BUDGET_BYTES", str(1024 * MIB))
    assert host._windows_allowance() is None


def test_factory_retains_legacy_route_on_non_windows_or_unsupported_runtime(monkeypatch):
    monkeypatch.setattr(host, "sys", SimpleNamespace(platform="linux"))
    assert host.host_pe_budget() is None
    monkeypatch.setattr(host, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(host, "_host_profile", lambda: None)
    assert host.host_pe_budget() is None


def test_factory_derives_pe_seven_visit_limit_without_raising_cap(monkeypatch):
    monkeypatch.setattr(host, "_host_profile", lambda: PROFILE)
    monkeypatch.setattr(host, "_windows_allowance", lambda: 100_000)
    monkeypatch.setattr(host.sys, "platform", "win32")
    result = host.host_pe_budget()
    assert result.limit_bytes == 100_000
    assert result.record_visit_limit == 7 * (100_000 // 32)


def test_provider_is_absent_from_native_dependency_scan():
    from pcc.backend import pe_x86_64
    from pcc.frontends.python.pipeline_import_scan import _without_inactive_implementation_imports
    source = Path(pe_x86_64.__file__).read_text()
    marker = "from pcc.backend.pe_host_budget import"
    assert marker in _without_inactive_implementation_imports(source, implementation="cpython")
    assert marker not in _without_inactive_implementation_imports(source, implementation="pcc")


@pytest.mark.parametrize("mode", (
    "native", "32-bit", "old-version", "debug-build", "debug", "pymalloc_debug",
    "malloc_debug", "mimalloc_debug", "free-threaded", "free-threaded-build", "unknown-build",
))
def test_unsupported_allocation_models_never_borrow_cpython_estimates(monkeypatch, mode):
    runtime = SimpleNamespace(implementation=SimpleNamespace(name="cpython"),
                              version_info=(3, 13, 0), getsizeof=sys.getsizeof,
                              _is_gil_enabled=lambda: True)
    monkeypatch.setattr(host, "sys", runtime)
    monkeypatch.setattr(host, "struct", SimpleNamespace(calcsize=lambda _format: 8))
    monkeypatch.setitem(sys.modules, "sysconfig", SimpleNamespace(get_config_var=lambda _name: 0))
    monkeypatch.delenv("PYTHONMALLOC", raising=False)
    if mode == "native":
        runtime.implementation.name = "pcc"
    elif mode == "32-bit":
        monkeypatch.setattr(host.struct, "calcsize", lambda _format: 4)
    elif mode == "old-version":
        runtime.version_info = (3, 12, 0)
    elif mode == "debug-build":
        runtime.gettotalrefcount = lambda: 0
    elif mode == "free-threaded":
        runtime._is_gil_enabled = lambda: False
    elif mode in ("free-threaded-build", "unknown-build"):
        flag = 1 if mode == "free-threaded-build" else None
        monkeypatch.setitem(sys.modules, "sysconfig", SimpleNamespace(get_config_var=lambda _name: flag))
    else:
        monkeypatch.setenv("PYTHONMALLOC", mode)
    assert host._host_profile() is None


def test_simulated_native_route_never_calls_host_provider(monkeypatch):
    from pcc.backend import pe_x86_64 as pe
    from pcc.backend.coff_x86_64 import CoffObject, CoffSection, CoffSymbol
    obj = CoffObject((CoffSection(".text", b"\xc3", 0x60000020, 16),),
                     (CoffSymbol("pcc_windows_start", 1),))
    observed = []
    def selected(*arguments):
        observed.append(arguments[-1])
        return b"native-route-control"
    monkeypatch.setattr(pe, "_link_selected_objects", selected)
    monkeypatch.setattr(host, "host_pe_budget", lambda: pytest.fail("native route reached host provider"))
    monkeypatch.setattr(sys, "implementation", SimpleNamespace(name="pcc"))
    assert pe.link_executable([obj]) == b"native-route-control"
    assert observed == [None]


@pytest.mark.parametrize("mode", ("venv", "native", "different-command", "base-python", "non-windows"))
def test_watchdog_uses_existing_cpython_venv_spawn_protocol_only_for_its_host(monkeypatch, mode):
    from scripts import platform_process_watchdog as watchdog
    runtime = SimpleNamespace(platform="win32", implementation=SimpleNamespace(name="cpython"),
                              executable="venv/python.exe", _base_executable="base/python.exe")
    monkeypatch.setattr(watchdog, "sys", runtime)
    command = [runtime.executable, "-m", "pcc"]
    environment = {"PATH": "original", "__PYVENV_LAUNCHER__": "inherited"}
    if mode == "different-command":
        command[0] = "other/python.exe"
    elif mode == "base-python":
        runtime._base_executable = runtime.executable
    elif mode == "non-windows":
        runtime.platform = "linux"
    actual, actual_env = watchdog._direct_host_launch(command, environment, native=mode == "native")
    if mode == "venv":
        assert actual == [runtime._base_executable, "-m", "pcc"]
        assert actual_env == dict(environment, __PYVENV_LAUNCHER__=runtime.executable)
    else:
        assert actual == command and actual_env == environment
    assert command[0] != "base/python.exe" and environment["__PYVENV_LAUNCHER__"] == "inherited"


@pytest.mark.parametrize("fault", ("none", "popen", "creation-time", "publication"))
def test_watchdog_publication_precedes_resume_and_every_failure_closes_job(tmp_path, monkeypatch, fault):
    from scripts import platform_process_watchdog as watchdog
    import os

    events = []
    original_job = watchdog.WindowsJob
    job = object.__new__(original_job)
    job.handle = 123
    def assigned(*_args):
        events.append("assigned")
        return 1
    def current():
        return 99
    job.kernel = SimpleNamespace(
        AssignProcessToJobObject=assigned, GetCurrentProcess=current,
        TerminateJobObject=lambda *_args: events.append("killed"),
        CloseHandle=lambda *_args: events.append("closed"),
    )
    job.ntdll = SimpleNamespace(NtResumeProcess=lambda *_args: events.append("resumed") or 0)
    def make_job(_limit):
        events.append("job-created")
        return job
    def creation(handle):
        events.append("creation-time")
        if fault == "creation-time":
            raise OSError("creation-time query failed")
        return handle + 1000
    def publish(source, destination):
        events.append("published")
        if fault == "publication":
            raise OSError("publication failed")
        os.replace(source, destination)
    process = SimpleNamespace(pid=11, _handle=12, returncode=0, poll=lambda: 0,
                              wait=lambda **_kwargs: events.append("waited"))
    def popen(*_args, **kwargs):
        events.append("popen")
        assert kwargs["creationflags"] == 0x204
        assert host.GUARD_ENV in kwargs["env"]
        if fault == "popen":
            raise OSError("Popen failed")
        return process
    monkeypatch.setattr(watchdog, "WindowsJob", make_job)
    monkeypatch.setattr(watchdog.ctypes, "WinDLL", lambda *_args, **_kwargs: job.kernel, raising=False)
    monkeypatch.setattr(watchdog, "_windows_creation_time", creation)
    monkeypatch.setattr(watchdog, "os", SimpleNamespace(
        name="nt", path=os.path, getpid=os.getpid, replace=publish,
    ))
    monkeypatch.setattr(watchdog.subprocess, "Popen", popen)
    def run():
        return watchdog.run([sys.executable, "-c", "pass"], cwd=tmp_path, env={},
                            log_path=tmp_path / "guard.log", timeout=20, rss_limit=1024 * MIB)
    if fault == "none":
        run()
        assert events.index("assigned") < events.index("published") < events.index("resumed")
        evidence = json.loads((tmp_path / "guard.windows-job.json").read_text())
        assert evidence["root_pid"] == 11 and evidence["root_creation_time"] == 1012
        assert evidence["supervisor_creation_time"] == 1099
    else:
        with pytest.raises(OSError):
            run()
        assert "resumed" not in events
    assert events.count("closed") == 1
    assert events[-2:] == ["killed", "closed"] if fault == "popen" else events[-3:] == ["killed", "closed", "waited"]
    assert job.handle is None


def test_buffer_ledger_is_cumulative_and_resize_reserves_old_and_new_storage():
    value = budget()
    value.retained_bytes = 17
    value.prepare((), ())
    metadata = value.retained_bytes
    first, second = bytearray(b"a" * 16), bytearray(b"b" * 16)
    value.retain(first)
    value.retain(second)
    assert value.retained_bytes == metadata + sys.getsizeof(first) + sys.getsizeof(second)
    following = 32
    extra = value.bytearray_header + max(following + 1, following + following // 8 + 6)
    value.limit_bytes = value.retained_bytes + extra
    value.check_growth(first, following)
    value.limit_bytes -= 1
    with pytest.raises(PreciseStackMapError, match="byte budget exceeded"):
        value.check_growth(first, following)
    assert first == b"a" * 16 and second == b"b" * 16
    value.finish()
    assert value.retained_bytes == 17


def test_buffer_ledger_keeps_exact_owner_alive_until_release():
    value = budget()
    payload = bytes(bytearray(b"immutable retained payload"))
    references = sys.getrefcount(payload)
    value.retain(payload)
    assert sys.getrefcount(payload) == references + 1
    assert value._buffers[id(payload)][0] is payload
    assert value.unretained_payload_bytes(payload) == 0
    mutable = bytearray(payload)
    value.retain(mutable)
    assert value.unretained_payload_bytes(mutable) == len(mutable)
    value.release(payload)
    assert sys.getrefcount(payload) == references
    assert value.unretained_payload_bytes(payload) == len(payload)
    assert value.retained_bytes == sys.getsizeof(mutable)
    value.release(mutable)
    assert value.retained_bytes == 0 and not value._buffers


def test_final_snapshot_counts_both_copies_with_all_sections_retained():
    value = budget()
    value.prepare((), ())
    section = bytearray(b"s" * 100)
    value.retain(section)
    extra = 2 * (20 + max(value.bytearray_header, value.bytes_header) + 1)
    value.limit_bytes = value.retained_bytes + extra
    value.check_copy(20, 2)
    value.limit_bytes -= 1
    with pytest.raises(PreciseStackMapError, match="byte budget exceeded"):
        value.check_copy(20, 2)
    value.finish()


def test_prepare_failure_restores_retained_and_does_not_refund_work():
    value = budget(limit=50)
    value.retained_bytes = 3
    value.charge_records(2)
    try:
        with pytest.raises(PreciseStackMapError):
            value.prepare((), ())
    finally:
        value.finish()
    assert value.retained_bytes == 3 and value.record_visits == 2


def _physical_budget(limit=1_000_000, work=100):
    profile = host._host_profile()
    # Callers carry the pcc_gate on a supported CPython allocation profile.
    assert profile is not None, "gate the test on host._host_profile()"
    return host.PEHostBudget(limit, work, **profile)


@pytest.mark.pcc_gate(probe=lambda: host._host_profile() is not None)
def test_metadata_uses_output_groups_and_actual_pdata_alignment():
    from pcc.backend.coff_x86_64 import CoffObject, CoffSection
    sections = (
        CoffSection(".text$a", b"a", 0x60000020, 16),
        CoffSection(".text$b", b"b", 0x60000020, 16),
        CoffSection(".rodata.long_suffix", b"c", 0x40000040, 8),
        CoffSection(".pdata$a", b"\0" * 12, 0x40000040, 4),
        CoffSection(".pdata$b", b"\0" * 24, 0x40000040, 4),
    )
    value = _physical_budget()
    value.prepare((CoffObject(sections, ()),), ())
    try:
        assert value.metadata_counts["sections"] == 5
        assert value.metadata_counts["groups"] == 4
        assert value.metadata_counts["pdata_records"] == 4
        assert value.metadata_owners["section groups and rows"] == 4 + 5 + 1
        assert value.metadata_owners["output rows and buffer ledger"] == 2 * (4 + 2)
        payload = bytearray()
        for section in sorted(sections, key=lambda row: row.name):
            if section.name.startswith(".pdata"):
                payload.extend(b"\0" * ((-len(payload)) & (section.align - 1)))
                payload.extend(section.data)
        assert len(payload) == 36
        assert len(payload) <= 12 * value.metadata_counts["pdata_records"] < 2 * 4095
    finally:
        value.finish()


@pytest.mark.pcc_gate(probe=lambda: host._host_profile() is not None)
@pytest.mark.parametrize("alignment", (1, 4, 16, 4096))
def test_pdata_record_bound_covers_padding_without_per_input_rounding(alignment):
    from pcc.backend.coff_x86_64 import CoffObject, CoffSection
    sections = tuple(CoffSection(".pdata$" + str(i), b"\0" * 12, 0x40000040, alignment)
                     for i in range(8))
    value = _physical_budget(limit=10_000_000)
    value.prepare((CoffObject(sections, ()),), ())
    try:
        bound = (sum(len(s.data) + s.align - 1 for s in sections) + 11) // 12
        assert value.metadata_counts["pdata_records"] == bound
        size = 0
        for section in sections:
            size += (-size) & (section.align - 1)
            size += len(section.data)
        assert size <= bound * 12
    finally:
        value.finish()


@pytest.mark.pcc_gate(probe=lambda: host._host_profile() is not None)
@pytest.mark.parametrize("stride", (8, 4096))
def test_fixup_storage_covers_real_containers_resize_overlap_and_sort_scratch(stride):
    value = _physical_budget(limit=10_000_000)
    fixups = []
    for index in range(2048):
        old_list = sys.getsizeof(fixups)
        address = 0x100000 + index * stride
        fixups.append(address)
        count = len(fixups)
        assert old_list + sys.getsizeof(fixups) <= value._list_bytes(count)
    # Match sorted(set(fixups)): all address integers are already retained
    # when the insertion-only set starts, including its resize overlap.
    seen = set()
    integer_owners = count * value.integer_bytes
    observed_set_peak = 0
    for address in fixups:
        old_set = sys.getsizeof(seen)
        seen.add(address)
        current_set = sys.getsizeof(seen)
        set_storage = old_set + current_set if old_set != current_set else current_set
        observed_set_peak = max(observed_set_peak, set_storage + integer_owners)
        assert (set_storage + integer_owners
                <= value._set_bytes(count) + integer_owners + value.set_header_bytes)
    assert observed_set_peak > value._set_bytes(count) + value.set_header_bytes
    ordered = sorted(seen)
    pages = {}
    for address in ordered:
        pages.setdefault(address & -4096, []).append(0xA000 | (address & 4095))
    page_rows = sorted(pages.items())
    for _page, entries in page_rows:
        if len(entries) & 1:
            entries.append(0)
    count, page_count = len(fixups), len(pages)
    assert (sys.getsizeof(ordered) + (count // 2) * struct.calcsize("P")
            <= value._list_bytes(count))
    assert (2 * sum(sys.getsizeof(entries) for entries in pages.values())
            <= value._list_bytes(count + page_count, lists=page_count))

    visited = set()
    def owned_bytes(item):
        if id(item) in visited:
            return 0
        visited.add(id(item))
        size = value._rounded(sys.getsizeof(item))
        if isinstance(item, dict):
            return size + sum(owned_bytes(k) + owned_bytes(v) for k, v in item.items())
        if isinstance(item, (tuple, list, set)):
            return size + sum(owned_bytes(v) for v in item)
        return size
    actual = sum(owned_bytes(item) for item in (fixups, seen, ordered, pages, page_rows))
    bound = (2 * value._list_bytes(count) + count * value.integer_bytes + value._set_bytes(count)
             + (count + page_count) * value.integer_bytes
             + value._list_bytes(count + page_count, lists=page_count)
             + 3 * page_count * value.metadata_entry_bytes)
    assert actual <= bound < 7 * count * value.metadata_entry_bytes


@pytest.mark.pcc_gate(probe=lambda: host._host_profile() is not None)
@pytest.mark.parametrize("with_map_tls", (False, True))
def test_dense_absolute_fixups_link_identically_inside_the_same_small_budget(with_map_tls):
    from pcc.backend import pe_x86_64 as pe
    from pcc.backend import precise_stackmap as wire
    from pcc.backend.coff_x86_64 import ADDR64, CoffObject, CoffRelocation, CoffSection, CoffSymbol
    count = 2048
    sections = [
        CoffSection(".text", b"\xc3", 0x60000020, 16),
        CoffSection(".rdata", b"\0" * (8 * count), 0x40000040, 8,
                    tuple(CoffRelocation(8 * i, 0, ADDR64) for i in range(count))),
    ]
    if with_map_tls:
        function = wire.FunctionStackMap(
            function_id=wire.function_id("pcc_windows_start"), function_address=0,
            code_size=1, frame_size=0, records=(wire.SafepointRecord(
                safepoint_id=1, instruction_offset=0, kind=wire.SAFEPOINT_ENTRY, locations=(),
            ),),
        )
        payload = wire.encode_stack_map(wire.PreciseStackMap(
            arch=wire.ARCH_X86_64, functions=(function,),
        ))
        assert len(payload) % 4096 != 0
        sections.extend((
            CoffSection(".pcc_stackmaps", payload, 0x40000040, 8,
                        (CoffRelocation(wire.HEADER_SIZE + 8, 0, ADDR64),)),
            CoffSection(".data$more", b"\0" * 8, 0xC0000040, 8,
                        (CoffRelocation(0, 0, ADDR64),)),
            CoffSection(".tls", b"\0" * 8, 0xC0000040, 8),
        ))
    obj = CoffObject(tuple(sections), (CoffSymbol("pcc_windows_start", 1),))
    value = _physical_budget()
    expected = pe.link_executable((obj,))
    image = pe.link_executable((obj,), compact_budget=value)
    assert image == expected
    assert 7 * (count + 3) * value.metadata_entry_bytes > value.limit_bytes
    assert value.metadata_counts["pages"] < value.metadata_counts["fixups"] // 100
    assert value.retained_bytes == 0 and value.record_visits == (8 if with_map_tls else 0)

    header = struct.unpack_from("<I", image, 60)[0]
    optional = header + 24
    reloc_rva, reloc_size = struct.unpack_from("<II", image, optional + 112 + 5 * 8)
    for index in range(struct.unpack_from("<H", image, header + 6)[0]):
        section = optional + 240 + 40 * index
        if image[section:section + 8].rstrip(b"\0") == b".reloc":
            assert struct.unpack_from("<I", image, section + 12)[0] == reloc_rva
            raw = struct.unpack_from("<I", image, section + 20)[0]
            break
    else:
        pytest.fail("missing relocation output")
    cursor = raw
    pages = []
    while cursor < raw + reloc_size:
        page, size = struct.unpack_from("<II", image, cursor)
        pages.append(page)
        cursor += size
    assert cursor == raw + reloc_size
    assert len(pages) == (7 if with_map_tls else 4)
    assert len(pages) <= value.metadata_counts["pages"]


@pytest.mark.pcc_gate(probe=lambda: host._host_profile() is not None)
@pytest.mark.parametrize("name,alignment", ((".too_long", 8), (".rdata", 8192)))
def test_invalid_section_scan_preserves_existing_retained_state(name, alignment):
    from pcc.backend.coff_x86_64 import CoffError, CoffObject, CoffSection
    value = _physical_budget()
    value.retained_bytes = 17
    value.charge_records(2)
    with pytest.raises(CoffError):
        value.prepare((CoffObject((CoffSection(name, b"a", 0x40000040, alignment),), ()),), ())
    assert value.retained_bytes == 17 and value.record_visits == 2
    assert value._initial_retained is None


@pytest.mark.pcc_gate(probe=lambda: host._host_profile() is not None)
def test_metadata_failure_reports_numerical_allowance_and_charge_without_refund():
    value = _physical_budget()
    value.retained_bytes = 17
    value.prepare((), ())
    required = value.retained_bytes
    value.finish()
    value.limit_bytes = required
    value.prepare((), ())
    value.finish()
    value.limit_bytes -= 1
    value.charge_records(2)
    with pytest.raises(PreciseStackMapError, match="PE future metadata") as caught:
        value.prepare((), ())
    message = str(caught.value)
    assert "allowance_bytes=" + str(required - 1) in message
    assert "retained_bytes=17" in message
    assert "future_metadata_bytes=" + str(required - 17) in message
    assert "'groups': 3" in message
    assert value.retained_bytes == 17 and value.record_visits == 2


@pytest.mark.pcc_gate(probe=lambda: host._host_profile() is not None)
@pytest.mark.parametrize("count,capacity,following", (
    (1, 8, 0), (4, 8, 0), (5, 8, 32), (6, 32, 0),
    (19, 32, 128), (78643, 131072, 262144), (157286, 262144, 524288),
))
def test_validation_cache_accounts_exact_next_set_table_without_large_allocations(count, capacity, following):
    value = _physical_budget()
    slot = 2 * struct.calcsize("P")
    allocated = value.validation_set_header + (capacity * slot if capacity > 8 else 0)
    keys = (count + 2) * (value.validation_key_bytes + 3 * value.integer_bytes)
    expected = value._rounded(allocated) + keys
    if following:
        expected += value._rounded(following * slot)
    assert value._validation_cache_bytes(count, allocated) == expected


@pytest.mark.pcc_gate(probe=lambda: host._host_profile() is not None)
def test_validation_cache_bound_covers_real_triples_and_set_resize_overlap():
    value = _physical_budget()
    cache = set()
    key_bytes = 0
    resizes = 0
    for index in range(2048):
        count = len(cache) + 1
        before = sys.getsizeof(cache)
        bound = value._validation_cache_bytes(count, before)
        key = (1000 + index, 3000 + index, 5000 + index)
        key_bytes += value._rounded(sys.getsizeof(key))
        key_bytes += sum(value._rounded(sys.getsizeof(part)) for part in key)
        cache.add(key)
        after = sys.getsizeof(cache)
        tables = after
        if before != after:
            resizes += 1
            tables += before - value.validation_set_header
        assert tables + key_bytes <= bound
    assert resizes >= 4
    assert bound < len(cache) * value.metadata_entry_bytes // 2


@pytest.mark.pcc_gate(probe=lambda: host._host_profile() is not None)
@pytest.mark.parametrize("existing", (0, 4, 18))
def test_validation_cache_has_exact_byte_boundary_with_pe_owners_retained(existing):
    value = _physical_budget()
    value.retained_bytes = 17
    value.prepare((), ())
    retained = bytearray(b"x" * 128)
    value.retain(retained)
    cache = {(index, 1, 32) for index in range(existing)}
    payload = b"p" * 10
    cache_bytes = value._validation_cache_bytes(existing + 1, sys.getsizeof(cache))
    scratch = value._list_bytes(3) + 3 * value.integer_bytes
    peak = value.retained_bytes + len(payload) + cache_bytes + scratch
    peak += value._set_bytes(7) + 2 * value.metadata_entry_bytes
    value.limit_bytes = peak
    value.check_validation_cache(payload, 7, 2, cache, 3)
    value.limit_bytes -= 1
    with pytest.raises(PreciseStackMapError, match="byte budget exceeded: validation cache") as caught:
        value.check_validation_cache(payload, 7, 2, cache, 3)
    assert "allowance_bytes=" + str(peak - 1) in str(caught.value)
    assert "retained_bytes=" + str(value.retained_bytes) in str(caught.value)
    assert "validation_cache_entries=" + str(existing + 1) in str(caught.value)
    assert "validation_cache_bytes=" + str(cache_bytes) in str(caught.value)
    assert len(cache) == existing and value.record_visits == 0
    value.finish()
    assert value.retained_bytes == 17


def _validation_payload(function_roots):
    from pcc.backend import precise_stackmap as wire
    functions = []
    record_id = 0
    for function_id, (frame_size, slices) in enumerate(function_roots, 1):
        records = []
        for index, offsets in enumerate(slices):
            record_id += 1
            records.append(wire.SafepointRecord(
                safepoint_id=record_id, instruction_offset=4 * index,
                kind=wire.SAFEPOINT_ENTRY,
                locations=tuple(wire.StackMapLocation(
                    kind=wire.LOCATION_STACK_INDIRECT,
                    flags=wire.LOCATION_MANAGED | wire.LOCATION_OWNED,
                    register=6, offset=offset,
                ) for offset in offsets),
            ))
        functions.append(wire.FunctionStackMap(
            function_id=function_id, function_address=0,
            code_size=4 * (len(records) + 1), frame_size=frame_size,
            records=tuple(records),
        ))
    return wire.encode_stack_map(wire.PreciseStackMap(
        arch=wire.ARCH_X86_64, functions=tuple(functions),
    ))


@pytest.mark.parametrize("roots", ((), (-8,)))
@pytest.mark.parametrize("final_image", (False, True))
def test_retained_validation_payload_has_one_owner_at_exact_budget_boundary(roots, final_image):
    from pcc.backend import precise_stackmap as wire

    count = 128
    payload = bytearray(_validation_payload(((16, (roots,) * count),)))
    if final_image:
        struct.pack_into("<Q", payload, wire.HEADER_SIZE + 8, 0x140001000)
    payload = bytes(payload)
    value = budget(work=2 * count)
    value.retained_bytes = 17
    value.prepare((), ())
    other = bytearray(b"separately retained output section")
    value.retain(other)
    value.retain(payload)
    retained = value.retained_bytes
    peak = retained + value._set_bytes(count) + value.metadata_entry_bytes
    if roots:
        peak += value._validation_cache_bytes(1, sys.getsizeof(set()))
        peak += value._list_bytes(1) + value.integer_bytes
    value.limit_bytes = peak
    wire.validate_stack_map_payload(payload, final_image=final_image, compact_budget=value)
    assert value.retained_bytes == retained and value.record_visits == 2 * count

    value.record_visit_limit += 2 * count
    value.limit_bytes -= 1
    label = "validation cache" if roots else "validation"
    with pytest.raises(PreciseStackMapError, match="byte budget exceeded: " + label):
        wire.validate_stack_map_payload(payload, final_image=final_image, compact_budget=value)
    assert value.retained_bytes == retained
    value.release(payload)
    assert value.retained_bytes == retained - sys.getsizeof(payload)
    value.finish()
    assert value.retained_bytes == 17 and not value._buffers


def test_equal_unretained_payload_is_still_charged_as_a_separate_owner():
    from pcc.backend import precise_stackmap as wire

    payload = _validation_payload(((0, ((),) * 128),))
    equal_payload = bytes(bytearray(payload))
    assert equal_payload == payload and equal_payload is not payload
    value = budget(work=512)
    value.retain(payload)
    value.limit_bytes = value.retained_bytes + value._set_bytes(128) + value.metadata_entry_bytes
    wire.validate_stack_map_payload(payload, compact_budget=value)
    with pytest.raises(PreciseStackMapError, match="byte budget exceeded: input"):
        wire.validate_stack_map_payload(equal_payload, compact_budget=value)
    assert value.record_visits == 256
    assert "raw=" + str(value.retained_bytes + len(equal_payload)) in value.last_rejection
    generic = wire.CompactStackMapBudget(value.limit_bytes, 512, **PROFILE)
    generic.retained_bytes = value.retained_bytes
    with pytest.raises(PreciseStackMapError, match="byte budget exceeded: input"):
        wire.validate_stack_map_payload(payload, compact_budget=generic)
    assert generic.record_visits == 0


@pytest.mark.parametrize("malformation,message", (
    ("frame", "exceeds frame size 0"),
    ("address", "final-image function address is unresolved"),
    ("reserved", "non-zero reserved stack-map field"),
))
def test_retained_payload_preserves_semantic_and_final_address_checks(malformation, message):
    from pcc.backend import precise_stackmap as wire

    payload = bytearray(_validation_payload(((16, ((-8,),)),)))
    if malformation == "frame":
        struct.pack_into("<I", payload, wire.HEADER_SIZE + 20, 0)
    elif malformation == "reserved":
        struct.pack_into("<H", payload, wire.HEADER_SIZE + wire.FUNCTION_SIZE + 22, 1)
    payload = bytes(payload)
    value = budget()
    value.retain(payload)
    with pytest.raises(PreciseStackMapError, match=message):
        wire.validate_stack_map_payload(payload, final_image=malformation == "address",
                                        compact_budget=value)
    assert value.retained_bytes == sys.getsizeof(payload)
    assert value.record_visits > 0


@pytest.mark.pcc_gate(probe=lambda: host._host_profile() is not None)
def test_validation_cache_admits_real_tuple_shapes_inside_unchanged_small_limit():
    from pcc.backend import precise_stackmap as wire
    slices = tuple((-(index + 1) * 8,) for index in range(256))
    payload = _validation_payload(((2048, slices),))
    value = _physical_budget(limit=150_000, work=512)
    generic = wire.CompactStackMapBudget(150_000, 512, **host._host_profile())
    with pytest.raises(PreciseStackMapError, match="byte budget exceeded: validation cache"):
        wire.validate_stack_map_payload(payload, compact_budget=generic)
    wire.validate_stack_map_payload(payload, compact_budget=value)
    assert value.limit_bytes == 150_000 and value.record_visits == 512


@pytest.mark.pcc_gate(probe=lambda: host._host_profile() is not None)
def test_validation_cache_releases_large_scratch_before_smaller_miss_and_reuses_hits(monkeypatch):
    from pcc.backend import precise_stackmap as wire
    slices = (tuple(-8 * index for index in range(1, 9)), (-8,), (-8,))
    payload = _validation_payload(((64, slices),))
    original = host.PEHostBudget.check_validation_cache
    misses = []
    def checked(self, payload, records, functions, cache, locations):
        assert "prior_flags" not in sys._getframe(1).f_locals
        misses.append((len(cache), locations))
        return original(self, payload, records, functions, cache, locations)
    monkeypatch.setattr(host.PEHostBudget, "check_validation_cache", checked)
    value = _physical_budget()
    wire.validate_stack_map_payload(payload, compact_budget=value)
    assert misses == [(0, 8), (1, 1)]
    assert value.record_visits == 6


@pytest.mark.pcc_gate(probe=lambda: host._host_profile() is not None)
def test_validation_cache_preserves_frame_dependent_checks():
    from pcc.backend import precise_stackmap as wire
    payload = bytearray(_validation_payload(((64, ((-48,),)), (64, ((-48,),)))))
    second_function = wire.HEADER_SIZE + wire.FUNCTION_SIZE + wire.RECORD_SIZE
    struct.pack_into("<I", payload, second_function + 20, 32)
    with pytest.raises(PreciseStackMapError, match="exceeds frame size 32"):
        wire.validate_stack_map_payload(bytes(payload), compact_budget=_physical_budget())


# requires an actual Windows Job
@pytest.mark.pcc_gate(probe=lambda: sys.platform == "win32")
def test_real_windows_direct_root_guard_admits_and_wrapper_does_not(tmp_path):
    # Small CPython children only. No compiler, runtime build or native pcc.
    from scripts.platform_process_watchdog import run
    root = Path(__file__).resolve().parents[2]
    script = (
        "import json,os,site,sys,pytest\n"
        "expected_python=json.loads(os.environ['PCC_TEST_EXPECTED_PYTHON'])\n"
        "actual_python=dict(executable=sys.executable,prefix=sys.prefix,base_prefix=sys.base_prefix,site_packages=site.getsitepackages(),pytest_file=pytest.__file__)\n"
        "assert actual_python==expected_python, (actual_python,expected_python)\n"
        "from pcc.backend.pe_host_budget import host_pe_budget\n"
        "b=host_pe_budget()\n"
        "assert b is not None, 'actual Windows direct-root admission failed'\n"
        "assert b.limit_bytes < 1073741824\n"
        "from pcc.backend import pe_x86_64 as pe, precise_stackmap as w, coff_x86_64 as c\n"
        "objects=[]\n"
        "for name in ('pcc_windows_start','helper'):\n"
        " r=w.SafepointRecord(safepoint_id=w.safepoint_id(name,0,w.SAFEPOINT_ENTRY),instruction_offset=0,kind=w.SAFEPOINT_ENTRY,locations=())\n"
        " f=w.FunctionStackMap(function_id=w.function_id(name),function_address=0,code_size=1,frame_size=0,records=(r,))\n"
        " payload=w.encode_stack_map(w.PreciseStackMap(arch=w.ARCH_X86_64,functions=(f,)))\n"
        " objects.append(c.CoffObject((c.CoffSection('.text',b'\\xc3',0x60000020,16),c.CoffSection('.pcc_stackmaps',payload,0x40000040,8,(c.CoffRelocation(w.HEADER_SIZE+8,0,c.ADDR64),))),(c.CoffSymbol(name,1),)))\n"
        "expected=pe.link_executable(objects)\n"
        "w.MAX_RECORDS=1\n"
        "assert pe.link_executable(objects)==expected, 'guarded automatic PE budget changed bytes'\n"
        "import subprocess,sys\n"
        "subprocess.run([sys.executable,'-c',"
        "'from pcc.backend.pe_host_budget import host_pe_budget; assert host_pe_budget() is None'],check=True)\n"
        "print('direct-root admitted; aggregate PE merged; descendant rejected')\n"
    )
    import os
    import site
    environment = dict(os.environ, PCC_WORKER_TREE_BUDGET_BYTES=str(1024 * MIB))
    environment["PCC_TEST_EXPECTED_PYTHON"] = json.dumps(dict(
        executable=sys.executable, prefix=sys.prefix, base_prefix=sys.base_prefix,
        site_packages=site.getsitepackages(), pytest_file=pytest.__file__,
    ))
    result = run([sys.executable, "-c", script], cwd=root, env=environment,
                 log_path=tmp_path / "guard.log", timeout=20, rss_limit=1024 * MIB)
    assert result["peak_tree_rss"] <= 1024 * MIB
    assert result["command"] == [sys.executable, "-c", script]
    expected_launch = getattr(sys, "_base_executable", "") or sys.executable
    assert result["launched_command"][0] == expected_launch
    assert "direct-root admitted; aggregate PE merged; descendant rejected" in (tmp_path / "guard.log").read_text()
    evidence = json.loads((tmp_path / "guard.windows-job.json").read_text())
    assert evidence["schema"] == host.GUARD_SCHEMA
    assert evidence["root_creation_time"] > 0 and evidence["supervisor_creation_time"] > 0
