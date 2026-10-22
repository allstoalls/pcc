"""Aggregate host payload retirement, exact bytes, and fail-closed storage."""

import errno
import sys

import pytest

from pcc.backend import macho_host_payload as payloads, macho_host_prepare as host
from pcc.backend import macho_exec, macho_host_output as output
from pcc.backend import precise_stackmap as maps
from tests.python.test_macho_consuming_join import _track_buffers, _section_targets
from tests.python.test_macho_owned_output import _factory_budget, _stack_inputs
from tests.python.test_macho_windowed_parity import _prepared_state, _write_pcos


def _parts(sizes, seed=1):
    return [bytes([seed + index]) * size for index, size in enumerate(sizes)]


def _store(groups, limit=20_000_000):
    budget = _factory_budget(limit)
    budget.retained_bytes = 137
    ledger = host._Ledger(budget)
    ledger.retained = 251 + sum(ledger.immutable(len(part)) for group in groups for part in group)
    store = payloads._StagedSectionPayloads(len(groups), budget, ledger.keep)
    return store, ledger


def _stage(store, ledger, groups):
    for parts in groups:
        retired = store.write_owned_parts(parts)
        ledger.retained -= retired
    store.seal()


def _files(monkeypatch, mode=None, failure=0):
    original = payloads.tempfile.TemporaryFile
    files = []

    class File:
        def __init__(self):
            self.inner = original(mode="w+b", buffering=0)
            self.calls, self.mode = [], mode
            files.append(self)
        def fileno(self):
            return self.inner.fileno()
        def seek(self, *args):
            result = self.inner.seek(*args)
            return result + 1 if self.mode == "seek" else result
        def write(self, value):
            self.calls.append(("write", len(value)))
            if self.mode == "write":
                if failure == "raise":
                    child = value[:]
                    raise OSError(errno.ENOSPC, "injected payload write")
                return len(value) + 1 if failure == "long" else failure
            with value[:97] as part:
                return self.inner.write(part)
        def readinto(self, value):
            self.calls.append(("read", len(value)))
            if self.mode in ("read", "read-close"):
                if failure == "raise" or self.mode == "read-close":
                    child = value[:]
                    raise OSError(errno.EIO, "injected payload read")
                return len(value) + 1 if failure == "long" else failure
            with value[:83] as part:
                return self.inner.readinto(part)
        def close(self):
            self.inner.close()
            if self.mode in ("close", "read-close"):
                raise OSError(errno.EIO, "injected payload close")

    monkeypatch.setattr(payloads.tempfile, "TemporaryFile", lambda **_kwargs: File())
    return files


def _assert_retired(error):
    pending, seen = [error], set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        trace = current.__traceback__
        while trace is not None:
            if trace.tb_frame.f_code.co_filename == payloads.__file__:
                local = trace.tb_frame.f_locals
                for name in ("buffer", "view", "payload", "result", "closing", "patch", "check", "admit", "ledger", "stream", "source", "part", "parts"):
                    if name in local:
                        assert local[name] is None
            trace = trace.tb_next
        pending.extend(other for other in (current.__cause__, current.__context__) if other is not None)


@pytest.mark.parametrize("sizes", ((), (0,), (1,), (65539,), (65539, 3, 99999)))
def test_all_chunks_retire_before_first_backing_and_exact_sequential_reads(monkeypatch, sizes):
    groups = [_parts(sizes, seed=index + 1) for index in range(3)]
    files = _files(monkeypatch)
    owners = _track_buffers(monkeypatch)
    store, ledger = _store(groups)
    _stage(store, ledger, groups)
    assert not owners and all(group == [] for group in groups)
    assert ledger.retained == 251 + store.metadata_bytes
    for index in range(3):
        expected = b"".join(_parts(sizes, seed=index + 1))
        assert store.descriptor(index)[1:3] == (len(expected), len(sizes))
        actual = store.read_owned(index, ledger)
        assert type(actual) is bytes and actual == expected
    assert store.consumed == 3 and store.read_bytes == store.logical_bytes
    store.close()
    assert all(owner.closed and len(set(owner.addresses)) <= 1 for owner in owners)
    assert all(file.inner.closed for file in files)
    assert max((size for file in files for _, size in file.calls), default=0) <= payloads._BLOCK
    assert ledger.budget.retained_bytes == 137


@pytest.mark.parametrize("kind", ("bytearray", "view", "alias", "repeated"))
def test_invalid_or_aliased_chunks_fail_before_any_write(monkeypatch, kind):
    groups = [_parts((103,))]
    files = _files(monkeypatch)
    store, ledger = _store(groups)
    held = None
    if kind == "bytearray":
        groups[0][0] = bytearray(groups[0][0])
    elif kind == "view":
        held = memoryview(groups[0][0])
        groups[0][0] = held
    elif kind == "alias":
        held = groups[0][0]
    else:
        groups[0].append(groups[0][0])
    with pytest.raises(maps.PreciseStackMapError, match="exact bytes|exclusively owned") as caught:
        store.write_owned_parts(groups[0])
    assert store.stream is None and groups == [[]]
    assert files[0].calls == [] and files[0].inner.closed
    _assert_retired(caught.value)
    if kind == "view":
        held.release()


@pytest.mark.parametrize("mode", ("write", "read"))
@pytest.mark.parametrize("failure", (0, -1, True, None, "long", "raise"))
def test_invalid_partial_io_releases_chunks_views_and_file(monkeypatch, mode, failure):
    groups = [_parts((1001, 1003))]
    files = _files(monkeypatch, mode=mode, failure=failure)
    owners = _track_buffers(monkeypatch)
    store, ledger = _store(groups)
    with pytest.raises(maps.PreciseStackMapError) as caught:
        _stage(store, ledger, groups)
        store.read_owned(0, ledger)
    assert store.stream is None and groups == [[]]
    assert all(file.inner.closed for file in files) and all(owner.closed for owner in owners)
    _assert_retired(caught.value)


@pytest.mark.parametrize("damage", ("short", "long", "content", "offset", "duplicate_index", "repeat"))
def test_corruption_extent_and_replay_refuse_without_publishing(monkeypatch, damage):
    groups = [_parts((1001,)), _parts((1003,), seed=4)]
    store, ledger = _store(groups)
    _stage(store, ledger, groups)
    if damage == "short":
        store.stream.truncate(store.logical_bytes - 1)
    elif damage == "long":
        store.stream.seek(0, 2)
        store.stream.write(b"x")
    elif damage == "content":
        store.stream.seek(0)
        store.stream.write(b"x")
    elif damage == "offset":
        offset, size, count, digest = store.rows[0]
        store.rows[0] = (offset + 1, size, count, digest)
    elif damage == "repeat":
        store.read_owned(0, ledger)
    elif damage == "duplicate_index":
        store.read_owned(1, ledger)
    with pytest.raises(maps.PreciseStackMapError, match="extent|content|offset|already consumed") as caught:
        store.read_owned(1 if damage == "duplicate_index" else 0, ledger)
    assert store.stream is None
    _assert_retired(caught.value)


@pytest.mark.parametrize("delta", (-1, 0, 1))
def test_exact_backing_admission_happens_after_aggregate_retirement(monkeypatch, delta):
    groups = [_parts((1001,)), _parts((1003,), seed=4)]
    store, ledger = _store(groups)
    _stage(store, ledger, groups)
    ledger.budget.limit_bytes = ledger.retained + ledger.immutable(1001) + 137 + delta
    owners = _track_buffers(monkeypatch)
    try:
        if delta < 0:
            with pytest.raises(maps.PreciseStackMapError, match="Mach-O staged payload backing"):
                store.read_owned(0, ledger)
            assert not owners and store.stream is None
        else:
            assert len(store.read_owned(0, ledger)) == 1001
    finally:
        store.close()


@pytest.mark.parametrize("delta", (-1, 0, 1))
def test_descriptor_admission_precedes_file_open(monkeypatch, delta):
    store, ledger = _store([[]])
    amount = ledger.retained + ledger.budget.retained_bytes
    store.close()
    files = _files(monkeypatch)
    if delta < 0:
        with pytest.raises(maps.PreciseStackMapError, match="Mach-O staged payload descriptors"):
            _store([[]], limit=amount + delta)
        assert files == []
    else:
        store, ledger = _store([[]], limit=amount + delta)
        store.close()
        assert len(files) == 1 and files[0].inner.closed


def test_storage_cap_and_open_failure_fail_without_allocating_backing(monkeypatch):
    groups = [_parts((1001,))]
    owners = _track_buffers(monkeypatch)
    store, ledger = _store(groups)
    ledger.budget.limit_bytes = 1000
    with pytest.raises(maps.PreciseStackMapError, match="storage budget"):
        store.write_owned_parts(groups[0])
    assert not owners and store.stream is None and groups == [[]]
    def fail(**_kwargs):
        raise OSError(errno.ENOSPC, "injected payload open")
    monkeypatch.setattr(payloads.tempfile, "TemporaryFile", fail)
    with pytest.raises(maps.PreciseStackMapError, match="injected payload open") as caught:
        _store([[]])
    _assert_retired(caught.value)


def test_instrumentation_during_last_seek_refuses_before_backing(monkeypatch):
    groups = [_parts((1001,))]
    files = _files(monkeypatch)
    store, ledger = _store(groups)
    _stage(store, ledger, groups)
    owners = _track_buffers(monkeypatch)
    seek = files[0].seek
    def instrument(*args):
        result = seek(*args)
        if args == (0,):
            monkeypatch.setattr(payloads, "_join_instrumentation_absent", lambda: False)
        return result
    files[0].seek = instrument
    with pytest.raises(maps.PreciseStackMapError, match="instrumentation changed"):
        store.read_owned(0, ledger)
    assert not owners and store.stream is None and files[0].inner.closed


@pytest.mark.parametrize("point", ("admit", "patch", "seal"))
def test_instrumentation_changes_fail_before_publishing(monkeypatch, point):
    groups = [_parts((1001,))]
    store, ledger = _store(groups)
    retired = store.write_owned_parts(groups[0])
    ledger.retained -= retired
    owners = _track_buffers(monkeypatch)
    def enable(*_args):
        monkeypatch.setattr(payloads, "_join_instrumentation_absent", lambda: False)
    if point == "seal":
        enable()
    else:
        store.seal()
        if point == "admit":
            check = ledger.check
            def checked(extra=0, *, label="Mach-O input window"):
                check(extra, label=label)
                enable()
            monkeypatch.setattr(ledger, "check", checked)
    with pytest.raises(maps.PreciseStackMapError, match="instrument") as caught:
        if point == "seal":
            store.seal()
        else:
            store.read_owned(0, ledger, patch=enable if point == "patch" else None)
    assert store.stream is None and all(owner.closed for owner in owners)
    _assert_retired(caught.value)


def test_read_error_survives_close_failure_and_patch_failure_releases_child_view(monkeypatch):
    groups = [_parts((1001,))]
    files = _files(monkeypatch, mode="read-close")
    owners = _track_buffers(monkeypatch)
    store, ledger = _store(groups)
    _stage(store, ledger, groups)
    with pytest.raises(maps.PreciseStackMapError, match="injected payload read") as caught:
        store.read_owned(0, ledger)
    assert all(file.inner.closed for file in files) and all(owner.closed for owner in owners)
    _assert_retired(caught.value)
    files[0].mode = None
    groups = [_parts((1001,))]
    store, ledger = _store(groups)
    files[-1].mode = None
    _stage(store, ledger, groups)
    def fail(view):
        child = view[:]
        raise ValueError("injected patch")
    with pytest.raises(ValueError, match="injected patch") as caught:
        store.read_owned(0, ledger, patch=fail)
    assert all(file.inner.closed for file in files) and all(owner.closed for owner in owners)
    _assert_retired(caught.value)


def test_production_retires_every_section_before_first_allocation_and_keeps_image(tmp_path, monkeypatch):
    paths = _stack_inputs(tmp_path, count=3, records=32, data_bytes=65537)
    expected = macho_exec.link_prepared_executable(
        host.prepare_executable_from_paths(paths, compact_budget=_factory_budget()),
        compact_budget=_factory_budget(),
    )
    original = host._StagedSectionPayloads._read
    reads, phases, takes = [], [], []
    def read(self, index, check, patch):
        assert self.sealed and self.appended == self.count
        assert any(event == "source_view_begin" for event, _ in phases)
        assert self.active_bytes == 0
        reads.append(index)
        return original(self, index, check, patch)
    take = host._StagedSectionPayloads.take
    def consume(self, index, **kwargs):
        takes.append(index)
        return take(self, index, **kwargs)
    monkeypatch.setattr(host._StagedSectionPayloads, "_read", read)
    monkeypatch.setattr(host._StagedSectionPayloads, "take", consume)
    budget = _factory_budget()
    pending = [host.prepare_executable_from_paths(
        paths, compact_budget=budget, _staged_rows=True,
        _phase_callback=lambda event, **fields: phases.append((event, fields)),
    )]
    assert reads == list(range(len(reads))) and reads
    assert takes == [] and pending[0]._payloads == [b""] * len(reads)
    assert pending[0]._host_payload_store.active_bytes == 0
    ends = [(index, fields) for index, (event, fields) in enumerate(phases)
            if event == "section_payload_spool_end"]
    joins = [(index, fields) for index, (event, fields) in enumerate(phases)
             if event == "section_payload_begin"]
    assert len(ends) == len(joins) == len(reads) and ends[-1][0] < joins[0][0]
    assert [fields for _, fields in ends] == [fields for _, fields in joins]
    actual = output.link_owned_executable(pending.pop(), compact_budget=budget)
    assert actual == expected
    assert takes == list(range(len(takes))) and len(reads) == 2 * len(takes)


def test_section_targets_after_stackmap_resize_keep_prepared_bytes(tmp_path):
    paths = _write_pcos(tmp_path, _section_targets())
    expected = _prepared_state(host.prepare_executable_from_paths(paths, compact_budget=_factory_budget()))
    source = host.prepare_executable_from_paths(paths, compact_budget=_factory_budget(), _staged_rows=True)
    try:
        assert _prepared_state(source) == expected
    finally:
        host.close_staged_source_rows(source)


def test_payload_store_close_failure_cannot_publish_final_image(tmp_path, monkeypatch):
    paths = _stack_inputs(tmp_path, count=2, records=8, data_bytes=64)
    original = host._StagedSectionPayloads.close
    stores, phases = [], []
    def close(self, *, _error=None):
        stores.append(self)
        original(self, _error=_error)
        if _error is None:
            raise OSError("injected final payload close")
    monkeypatch.setattr(host._StagedSectionPayloads, "close", close)
    budget = _factory_budget()
    pending = [host.prepare_executable_from_paths(
        paths, compact_budget=budget, _staged_rows=True,
        _phase_callback=lambda event, **fields: phases.append(event),
    )]
    assert stores == [] and pending[0]._host_payload_store.stream is not None
    rows = pending[0]._host_row_store
    with pytest.raises(OSError, match="injected final payload close") as caught:
        output.link_owned_executable(pending.pop(), compact_budget=budget,
                                     _phase_callback=lambda event, **fields: phases.append(event))
    assert stores and all(store.stream is None for store in stores)
    assert rows.stream is None
    assert "source_view_end" in phases and "final_backing_begin" not in phases
    _assert_retired(caught.value)


@pytest.mark.parametrize("point", ("section_payload_spool_begin", "section_payload_spool_end", "section_payload_begin"))
@pytest.mark.parametrize("instrument", ("trace", "profile"))
def test_production_phase_instrumentation_fails_closed(tmp_path, monkeypatch, point, instrument):
    paths = _stack_inputs(tmp_path, count=2, records=8, data_bytes=64)
    stores, phases = [], []
    create = host._StagedSectionPayloads
    def capture(*args, **kwargs):
        store = create(*args, **kwargs)
        stores.append(store)
        return store
    monkeypatch.setattr(host, "_StagedSectionPayloads", capture)
    setter = getattr(sys, "set" + instrument)
    previous = getattr(sys, "get" + instrument)()
    def hook(*_args):
        return hook
    def phase(event, **fields):
        phases.append(event)
        if event == point:
            setter(hook)
    try:
        with pytest.raises(macho_exec.LinkError, match="instrument"):
            host.prepare_executable_from_paths(
                paths, compact_budget=_factory_budget(), _staged_rows=True, _phase_callback=phase,
            )
    finally:
        setter(previous)
    assert stores and all(store.stream is None for store in stores)
    assert "section_joins_end" not in phases


def test_escaped_patch_export_refuses_without_copy(monkeypatch):
    groups = [_parts((1001,))]
    store, ledger = _store(groups)
    _stage(store, ledger, groups)
    owners = _track_buffers(monkeypatch)
    escaped = []
    try:
        with pytest.raises(BufferError) as caught:
            store.read_owned(0, ledger, patch=lambda view: escaped.append(view[:]))
        assert store.stream is None
        assert len(owners) == 1 and len(owners[0].addresses) == 2
        _assert_retired(caught.value)
    finally:
        for view in escaped:
            view.release()
        for owner in owners:
            owner.close()


@pytest.mark.parametrize("sizes", ((), (0,), (1,), (65539, 3, 99999)))
def test_payload_windows_retire_each_backing_and_bound_replayed_io(monkeypatch, sizes):
    groups = [_parts(sizes, seed=index + 1) for index in range(3)]
    files = _files(monkeypatch)
    owners = _track_buffers(monkeypatch)
    store, ledger = _store(groups)
    _stage(store, ledger, groups)
    size = sum(sizes)
    backing = ledger.immutable(size)
    assert store.max_window_bytes == backing
    admissions = []
    for index in (2, 0, 2, 1):
        def check():
            assert store.active_bytes == 0
            assert all(owner.closed for owner in owners)
            admissions.append(index)
            ledger.check(backing)
        with store.window(index, check=check) as raw:
            assert type(raw) is bytes and raw == b"".join(_parts(sizes, seed=index + 1))
            assert store.size(index) == size
            assert store.active_bytes == store.backing_bytes(index) == backing
            raw = None
        assert store.active_bytes == 0
    assert admissions == [2, 0, 2, 1]
    assert store._replays == [1, 1, 2]
    assert store.consumed == store.public_copy_bytes == 0
    assert store.read_bytes == 4 * size
    assert all(owner.closed and len(set(owner.addresses)) <= 1 for owner in owners)
    assert max((size for file in files for _, size in file.calls), default=0) <= payloads._BLOCK
    store.close()
    assert files[0].inner.closed and store._replays == [] and store._taken == []
    assert ledger.budget.retained_bytes == 137


@pytest.mark.parametrize("consume", ("take", "read_owned"))
def test_payload_consume_allows_arbitrary_indices_and_dropped_sections(consume):
    groups = [_parts((1001 + index,), seed=index + 1) for index in range(4)]
    store, ledger = _store(groups)
    _stage(store, ledger, groups)
    retained = ledger.retained
    results = []
    for index in (3, 0, 2):
        if consume == "take":
            result = store.take(index, check=lambda: ledger.keep(store.backing_bytes(index)))
        else:
            result = store.read_owned(index, ledger)
        assert result == bytes([index + 1]) * (1001 + index)
        results.append(result)
        assert store.active_bytes == 0
    assert store._taken == [True, False, True, True]
    assert store.consumed == 3 and store.read_bytes == 1004 + 1001 + 1003
    assert ledger.retained == retained + sum(ledger.immutable(len(raw)) for raw in results)
    store.close()


def test_payload_replays_and_final_take_patch_fresh_original_bytes_once():
    groups = [_parts((1001,))]
    store, ledger = _store(groups)
    _stage(store, ledger, groups)
    calls = []
    def patch(view):
        calls.append(view[0])
        view[0] += 1
    for _ in range(3):
        with store.window(0, check=lambda: ledger.check(store.max_window_bytes), patch=patch) as raw:
            assert raw == b"\x02" + b"\x01" * 1000
            raw = None
    result = store.read_owned(0, ledger, patch=patch)
    assert result == b"\x02" + b"\x01" * 1000
    assert calls == [1, 1, 1, 1]
    assert store.read_bytes == 4004 and store._replays == [3] and store.consumed == 1
    store.close()


def test_payload_public_copies_charge_each_backing_and_disqualify_consume():
    groups = [_parts((1001,)), _parts((1003,), seed=4)]
    store, ledger = _store(groups)
    _stage(store, ledger, groups)
    backing = store.backing_bytes(0)
    admissions, patches = [], []
    def check():
        admissions.append(store.public_copy_bytes)
        ledger.check(store.public_copy_bytes + backing)
    def patch(view):
        patches.append(view[0])
        view[0] += 1
    first = store.copy(0, check=check, patch=patch)
    second = store.copy(0, check=check, patch=patch)
    assert type(first) is bytes and first == second == b"\x02" + b"\x01" * 1000
    assert first is not second and patches == [1, 1]
    assert admissions == [0, backing]
    assert store.public_copy_bytes == 2 * backing and store.active_bytes == 0
    assert store._replays == [0, 0] and store.read_bytes == 2002
    with pytest.raises(maps.PreciseStackMapError, match="public copies") as caught:
        store.read_owned(1, ledger)
    assert store.stream is None and store.public_copy_bytes == 2 * backing
    _assert_retired(caught.value)


def test_payload_windows_enforce_eight_replays_without_allocating_ninth(monkeypatch):
    groups = [_parts((1001,))]
    store, ledger = _store(groups)
    _stage(store, ledger, groups)
    owners = _track_buffers(monkeypatch)
    for _ in range(8):
        with store.window(0, check=lambda: ledger.check(store.max_window_bytes)) as raw:
            assert len(raw) == 1001
            raw = None
    with pytest.raises(maps.PreciseStackMapError, match="replay budget") as caught:
        with store.window(0, check=lambda: None):
            pytest.fail("ninth payload replay was admitted")
    assert len(owners) == 8 and all(owner.closed for owner in owners)
    assert store.read_bytes == 8008 and store.active_bytes == 0 and store.stream is None
    _assert_retired(caught.value)


def test_payload_final_consume_remains_available_after_eight_windows():
    groups = [_parts((1001,))]
    store, ledger = _store(groups)
    _stage(store, ledger, groups)
    for _ in range(8):
        with store.window(0, check=lambda: ledger.check(store.max_window_bytes)) as raw:
            raw = None
    assert store.read_owned(0, ledger) == b"\x01" * 1001
    assert store.consumed == 1 and store.read_bytes == 9009
    with pytest.raises(maps.PreciseStackMapError, match="already consumed"):
        with store.window(0, check=lambda: None):
            pytest.fail("consumed descriptor was replayed")
    assert store.stream is None


def test_payload_replay_verifies_original_digest_after_each_window(monkeypatch):
    groups = [_parts((1001,))]
    store, ledger = _store(groups)
    _stage(store, ledger, groups)
    owners = _track_buffers(monkeypatch)
    with store.window(0, check=lambda: ledger.check(store.max_window_bytes)) as raw:
        raw = None
    store.stream.seek(0)
    store.stream.write(b"x")
    with pytest.raises(maps.PreciseStackMapError, match="content changed") as caught:
        with store.window(0, check=lambda: ledger.check(store.max_window_bytes)):
            pytest.fail("modified payload was replayed")
    assert store.stream is None and store.read_bytes == 1001
    assert len(owners) == 2 and all(owner.closed for owner in owners)
    _assert_retired(caught.value)


@pytest.mark.parametrize("alias_kind", ("bytes", "view", "iterator"))
def test_payload_windows_fail_closed_on_escaped_aliases(alias_kind):
    groups = [_parts((1001,))]
    store, ledger = _store(groups)
    _stage(store, ledger, groups)
    aliases = []
    with pytest.raises(maps.PreciseStackMapError, match="escaped aliases") as caught:
        with store.window(0, check=lambda: ledger.check(store.max_window_bytes)) as raw:
            aliases.append(raw if alias_kind == "bytes" else (
                memoryview(raw) if alias_kind == "view" else iter(raw)
            ))
            raw = None
    assert store.stream is None and store.active_bytes > 0
    _assert_retired(caught.value)
    aliases.clear()
    store.close()
    assert store.active_bytes > 0


def test_payload_window_original_error_survives_escaped_alias_and_close_failure(monkeypatch):
    groups = [_parts((1001,))]
    files = _files(monkeypatch)
    store, ledger = _store(groups)
    _stage(store, ledger, groups)
    original = ValueError("original payload consumer error")
    aliases = []
    with pytest.raises(ValueError) as caught:
        with store.window(0, check=lambda: ledger.check(store.max_window_bytes)) as raw:
            aliases.append(raw)
            raw = None
            files[0].mode = "close"
            raise original
    assert caught.value is original and files[0].inner.closed
    assert store.stream is None and store.active_bytes > 0
    _assert_retired(caught.value)
    aliases.clear()


def test_payload_window_failure_clears_traceback_held_views_before_retirement():
    groups = [_parts((1001,))]
    store, ledger = _store(groups)
    _stage(store, ledger, groups)
    original = ValueError("original payload decoder error")
    def decode(raw):
        child = memoryview(raw)
        raise original
    with pytest.raises(ValueError) as caught:
        with store.window(0, check=lambda: ledger.check(store.max_window_bytes)) as raw:
            try:
                decode(raw)
            finally:
                raw = None
    assert caught.value is original and store.active_bytes == 0 and store.stream is None
    _assert_retired(caught.value)


@pytest.mark.parametrize("mode", ("window", "copy", "take"))
@pytest.mark.parametrize("delta", (-1, 0, 1))
def test_payload_window_admission_precedes_allocation_at_exact_cap(monkeypatch, mode, delta):
    groups = [_parts((1001,))]
    store, ledger = _store(groups)
    _stage(store, ledger, groups)
    backing = store.backing_bytes(0)
    ledger.budget.limit_bytes = ledger.retained + ledger.budget.retained_bytes + backing + delta
    owners = _track_buffers(monkeypatch)
    def check():
        ledger.check(backing, label="payload window admission")
    def read():
        if mode == "window":
            with store.window(0, check=check) as raw:
                assert type(raw) is bytes and len(raw) == 1001
                raw = None
        else:
            assert len(getattr(store, mode)(0, check=check)) == 1001
    try:
        if delta < 0:
            with pytest.raises(maps.PreciseStackMapError, match="payload window admission") as caught:
                read()
            assert not owners and store.active_bytes == 0 and store.stream is None
            _assert_retired(caught.value)
        else:
            read()
            assert len(owners) == 1 and owners[0].closed
    finally:
        store.close()


def test_payload_copy_refusal_preserves_prior_cumulative_charge(monkeypatch):
    groups = [_parts((1001,))]
    store, ledger = _store(groups)
    _stage(store, ledger, groups)
    first = store.copy(0, check=lambda: ledger.check(store.max_window_bytes))
    prior = store.public_copy_bytes
    owners = _track_buffers(monkeypatch)
    original = maps.PreciseStackMapError("payload public copy budget exceeded")
    def check():
        assert store.public_copy_bytes == prior
        raise original
    with pytest.raises(maps.PreciseStackMapError) as caught:
        store.copy(0, check=check)
    assert caught.value is original and first == b"\x01" * 1001
    assert store.public_copy_bytes == prior and store.active_bytes == 0 and not owners
    assert store.stream is None
    _assert_retired(caught.value)


def test_payload_copy_cumulative_charge_refuses_second_backing_at_cap(monkeypatch):
    groups = [_parts((1001,))]
    store, ledger = _store(groups)
    _stage(store, ledger, groups)
    backing = store.backing_bytes(0)
    ledger.budget.limit_bytes = ledger.retained + ledger.budget.retained_bytes + 2 * backing - 1
    owners = _track_buffers(monkeypatch)
    def check():
        ledger.check(store.public_copy_bytes + backing, label="payload public copies")
    first = store.copy(0, check=check)
    with pytest.raises(maps.PreciseStackMapError, match="payload public copies"):
        store.copy(0, check=check)
    assert first == b"\x01" * 1001 and len(owners) == 1 and owners[0].closed
    assert store.public_copy_bytes == backing and store.active_bytes == 0
    assert store.stream is None


def test_payload_reentrant_reads_leave_outer_window_intact():
    groups = [_parts((1001,))]
    store, ledger = _store(groups)
    _stage(store, ledger, groups)
    errors = []
    def reject_nested():
        for mode in ("window", "copy", "take"):
            with pytest.raises(maps.PreciseStackMapError, match="already active") as caught:
                if mode == "window":
                    with store.window(0, check=lambda: None):
                        pytest.fail("reentrant window admitted")
                else:
                    getattr(store, mode)(0, check=lambda: None)
            errors.append(caught.value)
    def check():
        ledger.check(store.max_window_bytes)
        reject_nested()
    with store.window(0, check=check) as raw:
        reject_nested()
        raw = None
    assert store.stream is not None and store.active_bytes == 0
    for error in errors:
        _assert_retired(error)
    store.close()


@pytest.mark.parametrize("instrument", ("trace", "profile"))
def test_payload_window_retirement_rechecks_instrumentation(monkeypatch, instrument):
    groups = [_parts((1001,))]
    store, ledger = _store(groups)
    _stage(store, ledger, groups)
    with pytest.raises(maps.PreciseStackMapError, match="instrumentation changed") as caught:
        with store.window(0, check=lambda: ledger.check(store.max_window_bytes)) as raw:
            monkeypatch.setattr(payloads.sys, "get" + instrument, lambda: object())
            raw = None
    assert store.stream is None and store.active_bytes > 0
    _assert_retired(caught.value)


def test_payload_window_outer_handled_error_does_not_hide_alias_refusal():
    groups = [_parts((1001,))]
    store, ledger = _store(groups)
    _stage(store, ledger, groups)
    aliases = []
    try:
        raise ValueError("unrelated handled error")
    except ValueError:
        with pytest.raises(maps.PreciseStackMapError, match="escaped aliases"):
            with store.window(0, check=lambda: ledger.check(store.max_window_bytes)) as raw:
                aliases.append(raw)
                raw = None
    assert store.stream is None and store.active_bytes > 0
    aliases.clear()


def test_payload_patch_error_wins_over_escaped_export_cleanup(monkeypatch):
    groups = [_parts((1001,))]
    store, ledger = _store(groups)
    _stage(store, ledger, groups)
    owners = _track_buffers(monkeypatch)
    escaped = []
    original = ValueError("original payload patch error")
    def patch(view):
        escaped.append(view[:])
        raise original
    try:
        with pytest.raises(ValueError) as caught:
            with store.window(0, check=lambda: ledger.check(store.max_window_bytes), patch=patch):
                pytest.fail("failed patch was published")
        assert caught.value is original and store.stream is None and store.active_bytes > 0
        _assert_retired(caught.value)
    finally:
        for view in escaped:
            view.release()
        for owner in owners:
            owner.close()


def test_payload_freeze_refuses_hidden_immutable_snapshot_alias(monkeypatch):
    groups = [_parts((1001,))]
    store, ledger = _store(groups)
    _stage(store, ledger, groups)
    original = payloads.io.BytesIO
    aliases, owners = [], []
    class Escaping(original):
        def __init__(self, value):
            super().__init__(value)
            owners.append(self)
        def getvalue(self):
            result = super().getvalue()
            aliases.append(result)
            return result
    monkeypatch.setattr(payloads.io, "BytesIO", Escaping)
    with pytest.raises(maps.PreciseStackMapError, match="exclusively owned bytes") as caught:
        store.take(0, check=lambda: ledger.check(store.max_window_bytes))
    assert store.stream is None and store.active_bytes > 0
    assert len(owners) == 1 and owners[0].closed
    _assert_retired(caught.value)
    aliases.clear()
