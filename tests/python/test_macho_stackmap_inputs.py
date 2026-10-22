"""Bounded host proofs for file-owned Mach-O stack-map input windows."""

import ctypes
import errno
import io
import struct
import sys
import tracemalloc
from types import SimpleNamespace

import pytest

from pcc.backend import macho_host_prepare as host
from pcc.backend import macho_host_stackmap as merge
from pcc.backend import macho_host_stackmap_inputs as staging
from pcc.backend import precise_stackmap as wire
from tests.python.test_macho_stackmap_consumption import _payload
from tests.python.test_macho_windowed_prepare import _budget
from tests.python.test_pe_stackmap_suffix_sharing import _payload as _vectors, _root


def _stage(payloads, budget, *, seal=True):
    ledger = host._Ledger(budget)
    source = staging._StagedStackMapInputs(
        len(payloads), sum(map(len, payloads)), compact_budget=budget, admit=ledger.keep,
    )
    try:
        for payload in payloads:
            source.append(payload, 0, len(payload))
        if seal:
            source.seal()
        return source
    except BaseException:
        source.close()
        raise


def _merge(source, budget, single_buffer=True):
    previous = budget.retained_bytes
    budget.retained_bytes += source.metadata_bytes
    try:
        return merge.merge_staged_stack_map_payloads(
            source, compact_budget=budget, single_buffer=single_buffer,
        )
    finally:
        budget.retained_bytes = previous


@pytest.mark.parametrize("arch", (wire.ARCH_AARCH64, wire.ARCH_X86_64))
@pytest.mark.parametrize("single_buffer", (False, True))
@pytest.mark.parametrize("records", (0, 4, 4097))
def test_staged_bytes_offsets_record_work_and_both_buffer_models(arch, single_buffer, records):
    payloads = [_payload(ids, arch=arch, records=records) for ids in ((3,), (), (1, 2))]
    expected_budget = _budget(20_000_000)
    expected = merge.merge_owned_stack_map_payloads(
        list(payloads), compact_budget=expected_budget, single_buffer=single_buffer,
    )
    budget = _budget(20_000_000)
    budget.retained_bytes = 137
    source = _stage(payloads, budget)
    stream = source.stream
    assert _merge(source, budget, single_buffer) == expected
    assert budget.record_visits == expected_budget.record_visits == 3 * records
    assert source.read == source.written == source.total == sum(map(len, payloads))
    assert source.consumed == len(payloads) and source.rows == []
    assert source.stream is None and stream.closed
    assert budget.retained_bytes == 137


def test_staged_full_vectors_keep_gc_fields_collisions_and_all_work_counts(monkeypatch):
    vectors = tuple(tuple(_root(value) for value in range(1, count + 1))
                    for count in range(1, 33))
    payloads = [_vectors(vectors, fid=3), _vectors(vectors[::-1], fid=1),
                _vectors(((_root(1), _root(99, base=0)), ()), fid=2)]
    monkeypatch.setattr(merge.hashlib, "sha256", lambda _value: SimpleNamespace(digest=lambda: b"x" * 32))
    receipts = []
    original_work, original_index = merge._SpoolWork, merge._IndexWork

    class Work(original_work):
        def __init__(self, budget):
            super().__init__(budget)
            self.comparisons = []
            receipts.append([self])

        def charge_comparison(self, count):
            self.comparisons.append(count)
            return super().charge_comparison(count)

    class Index(original_index):
        def __init__(self, budget):
            super().__init__(budget)
            receipts[-1].append(self)

    monkeypatch.setattr(merge, "_SpoolWork", Work)
    monkeypatch.setattr(merge, "_IndexWork", Index)
    budget = _budget(20_000_000)
    expected = merge.merge_owned_stack_map_payloads(list(payloads), compact_budget=budget, single_buffer=True)
    source = _stage(payloads, _budget(20_000_000))
    actual = _merge(source, _budget(20_000_000))
    assert actual == expected
    first, second = receipts
    for name in ("written", "read", "compared_locations", "comparison_limit", "comparisons"):
        assert getattr(first[0], name) == getattr(second[0], name)
    for name in ("operations", "steps", "allocated_bytes"):
        assert getattr(first[1], name) == getattr(second[1], name)


def test_previous_window_and_indexes_retire_before_next_read_and_final_backing(monkeypatch):
    payloads = [_payload((fid,), records=4097) for fid in (1, 2, 3)]
    budget = _budget(20_000_000)
    source = _stage(payloads, budget)
    original = source.read_next.__func__
    kept = []

    def read(self, index, **kwargs):
        frame = sys._getframe(1)
        state = frame.f_locals
        assert state["input_bytes"] == 0
        assert all(state[name] is None for name in ("payload", "scanned", "source_index_cache", "blob"))
        if kept:
            # Only this test's list and this explicitly strong temporary own
            # the preceding bytes; no production frame or view retains it.
            references = sys.getrefcount([kept[-1]].pop())
            assert references == 2
        assert all(row is None for row in self.rows[:index])
        del state, frame
        result = original(self, index, **kwargs)
        kept.append(result)
        return result

    original_check = budget.check_peak

    def check(raw, *, records=0, entries=0, label="stack-map"):
        if label == "spool merge materialize":
            assert source.stream is None and source.rows == []
            references = sys.getrefcount([kept[-1]].pop())
            assert references == 2
        return original_check(raw, records=records, entries=entries, label=label)

    monkeypatch.setattr(staging._StagedStackMapInputs, "read_next", read)
    budget.check_peak = check
    _merge(source, budget)


@pytest.mark.parametrize("single_buffer", (False, True))
def test_input_window_allocation_is_admitted_before_any_buffer(monkeypatch, single_buffer):
    budget = _budget(20_000_000)
    source = _stage([_payload((1,))], budget)

    def refuse(extra, *, label):
        assert label == "spool staged input window"
        assert extra >= len(_payload((1,)))
        raise wire.PreciseStackMapError("refused before input allocation")

    def forbidden(*args):
        pytest.fail("input buffer allocated before admission")

    monkeypatch.setattr(staging.io, "BytesIO", forbidden)
    monkeypatch.setattr(staging, "bytearray", forbidden, raising=False)
    with pytest.raises(wire.PreciseStackMapError, match="before input allocation"):
        source.read_next(0, compact_budget=budget, admit=refuse, single_buffer=single_buffer)
    assert source.read == 0
    source.close()


def test_descriptor_and_total_work_limits_precede_file_creation(monkeypatch):
    calls = []
    monkeypatch.setattr(staging.tempfile, "TemporaryFile", lambda **kw: calls.append(kw))
    budget = _budget(100)
    with pytest.raises(wire.PreciseStackMapError, match="staging work budget"):
        staging._StagedStackMapInputs(1, 101, compact_budget=budget, admit=lambda *a, **kw: None)
    with pytest.raises(wire.PreciseStackMapError, match="byte budget"):
        staging._StagedStackMapInputs(1, 100, compact_budget=budget, admit=host._Ledger(budget).keep)
    assert calls == []


@pytest.mark.parametrize("failure", ("truncated", "duplicate", "mixed-arch", "bad-index"))
def test_staged_rejections_match_original_and_close_accepted_storage(failure):
    payloads = [_payload((1,)), _payload((2,))]
    if failure == "truncated":
        payloads[1] = payloads[1][:12]
    elif failure == "duplicate":
        payloads[1] = payloads[0]
    elif failure == "mixed-arch":
        payloads[1] = _payload((2,), arch=wire.ARCH_X86_64)
    else:
        raw = bytearray(payloads[1])
        struct.pack_into("<I", raw, wire.HEADER_SIZE + wire.FUNCTION_SIZE + 28, 0xFFFFFFFF)
        payloads[1] = bytes(raw)
    with pytest.raises(wire.PreciseStackMapError) as expected:
        merge.merge_owned_stack_map_payloads(list(payloads), compact_budget=_budget(), single_buffer=True)
    source = _stage(payloads, _budget())
    stream = source.stream
    with pytest.raises(wire.PreciseStackMapError) as actual:
        _merge(source, _budget())
    assert str(actual.value) == str(expected.value)
    assert source.consumed == 1 and source.rows == [] and source.stream is None and stream.closed


@pytest.mark.parametrize("method,failure", [(method, failure)
    for method in ("write", "readinto") for failure in ("zero", "oversize", "bool", "oserror")]
    + [("close", "oserror")])
def test_staging_io_failures_close_and_leave_no_buffer_owners(monkeypatch, method, failure):
    original = staging.tempfile.TemporaryFile
    opened = []

    class Stream:
        def __init__(self):
            self.inner = original(mode="w+b", buffering=0)
            opened.append(self)

        def fail(self, value):
            if failure == "oserror":
                raise OSError(errno.ENOSPC if method == "write" else errno.EIO, "injected staging IO failure")
            return 0 if failure == "zero" else True if failure == "bool" else len(value) + 1

        def write(self, value):
            return self.fail(value) if method == "write" and self is opened[0] else self.inner.write(value)

        def readinto(self, value):
            return self.fail(value) if method == "readinto" and self is opened[0] else self.inner.readinto(value)

        def seek(self, *args):
            return self.inner.seek(*args)

        def close(self):
            self.inner.close()
            if method == "close" and self is opened[0]:
                self.fail(b"")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    monkeypatch.setattr(staging.tempfile, "TemporaryFile", lambda **kw: Stream())
    with pytest.raises(wire.PreciseStackMapError) as caught:
        source = _stage([_payload((1,), records=4097)], _budget())
        _merge(source, _budget())
    assert opened and all(stream.inner.closed for stream in opened)
    trace = caught.value.__traceback__
    while trace is not None:
        frame = trace.tb_frame
        if frame.f_code is staging._StagedStackMapInputs.read_next.__code__:
            assert all(frame.f_locals[name] is None for name in ("buffer", "view", "result", "closing"))
        if frame.f_code is staging._StagedStackMapInputs.append.__code__:
            assert frame.f_locals["raw"] is None
        trace = trace.tb_next


def test_single_backing_identity_read_freeze_and_no_full_read(monkeypatch):
    payload = _payload((1,), records=4097)
    budget = _budget()
    source = _stage([payload], budget)
    original = io.BytesIO
    addresses = []

    class Buffer:
        def __init__(self, value):
            self.inner = original(value)

        def getbuffer(self):
            view = self.inner.getbuffer()
            addresses.append(ctypes.addressof(ctypes.c_char.from_buffer(view)))
            return view

        def truncate(self, size):
            return self.inner.truncate(size)

        def getvalue(self):
            value = self.inner.getvalue()
            address = ctypes.pythonapi.PyBytes_AsString
            address.argtypes, address.restype = [ctypes.py_object], ctypes.c_void_p
            addresses.append(address(value))
            return value

        def close(self):
            return self.inner.close()

    monkeypatch.setattr(staging.io, "BytesIO", Buffer)
    actual = source.read_next(0, compact_budget=budget, admit=lambda *a, **kw: None, single_buffer=True)
    assert actual == payload and addresses[0] == addresses[1]
    assert source.read == len(payload)
    source.close()


@pytest.mark.parametrize("single_buffer", (False, True))
@pytest.mark.parametrize("delta", (-1, 0, 1))
def test_new_admission_labels_refuse_at_exact_minus_one(single_buffer, delta):
    payload = _payload((1,), records=4097)
    probe = _budget(20_000_000)
    source = _stage([payload], probe)
    metadata = source.metadata_bytes
    source.close()
    budget = _budget(metadata + delta)
    if delta < 0:
        with pytest.raises(wire.PreciseStackMapError, match="staged stack-map descriptors"):
            staging._StagedStackMapInputs(1, 1, compact_budget=budget, admit=host._Ledger(budget).keep)
    else:
        staging._StagedStackMapInputs(1, 1, compact_budget=budget, admit=host._Ledger(budget).keep).close()
    budget = _budget(20_000_000)
    source = _stage([payload], budget)
    backing = budget._rounded(sys.getsizeof(payload))
    if not single_buffer:
        backing += budget._rounded(staging._BYTEARRAY_HEADER) + budget._rounded(len(payload) + 1)
    budget.limit_bytes = backing + delta
    if delta < 0:
        with pytest.raises(wire.PreciseStackMapError, match="spool staged input window"):
            source.read_next(0, compact_budget=budget, admit=budget.check_peak, single_buffer=single_buffer)
        assert source.read == 0
    else:
        assert source.read_next(0, compact_budget=budget, admit=budget.check_peak,
                                single_buffer=single_buffer) == payload
    source.close()


def test_append_has_no_payload_copy_and_read_peak_is_one_or_two_backings():
    payload = _payload((1,), records=32768)
    budget = _budget(20_000_000)
    source = staging._StagedStackMapInputs(1, len(payload), compact_budget=budget,
                                         admit=host._Ledger(budget).keep)
    tracemalloc.start()
    try:
        source.append(payload, 0, len(payload))
        _, append_peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert append_peak < 8192
    source.seal()
    peaks = {}
    for single_buffer in (True, False):
        if not single_buffer:
            source.close()
            source = _stage([payload], budget)
        tracemalloc.start()
        try:
            actual = source.read_next(0, compact_budget=budget, admit=budget.check_peak,
                                      single_buffer=single_buffer)
            _, peaks[single_buffer] = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert actual == payload
        actual = None
    source.close()
    assert peaks[True] < len(payload) + 8192
    assert 2 * len(payload) <= peaks[False] < 2 * len(payload) + 8192


def test_staging_extents_order_and_total_traffic_are_not_reusable():
    budget = _budget()
    payload = _payload((1,))
    source = staging._StagedStackMapInputs(1, len(payload), compact_budget=budget,
                                         admit=host._Ledger(budget).keep)
    try:
        with pytest.raises(wire.PreciseStackMapError, match="extent changed"):
            source.append(payload, 0, len(payload) + 1)
        with pytest.raises(wire.PreciseStackMapError, match="extent changed"):
            source.seal()
        assert source.written == source.read == 0
        source.append(payload, 0, len(payload))
        source.seal()
        with pytest.raises(wire.PreciseStackMapError, match="extent changed"):
            source.append(payload, 0, len(payload))
        with pytest.raises(wire.PreciseStackMapError, match="order changed"):
            source.read_next(1, compact_budget=budget, admit=budget.check_peak, single_buffer=True)
        assert source.read_next(0, compact_budget=budget, admit=budget.check_peak,
                                single_buffer=True) == payload
        with pytest.raises(wire.PreciseStackMapError, match="extent changed"):
            source.read_next(0, compact_budget=budget, admit=budget.check_peak, single_buffer=True)
        assert source.read == source.written == len(payload)
    finally:
        source.close()


def test_positive_short_io_completes_without_repeating_or_skipping_bytes(monkeypatch):
    original = staging.tempfile.TemporaryFile
    calls = []

    class ShortFile:
        def __init__(self):
            self.inner = original(mode="w+b", buffering=0)

        def write(self, value):
            calls.append(("write", len(value)))
            with value[:min(97, len(value))] as part:
                return self.inner.write(part)

        def readinto(self, value):
            calls.append(("read", len(value)))
            with value[:min(83, len(value))] as part:
                return self.inner.readinto(part)

        def seek(self, *args):
            return self.inner.seek(*args)

        def close(self):
            self.inner.close()

    monkeypatch.setattr(staging.tempfile, "TemporaryFile", lambda **kw: ShortFile())
    payload = _payload((1,), records=4097)
    budget = _budget()
    source = _stage([payload], budget)
    assert source.read_next(0, compact_budget=budget, admit=budget.check_peak,
                            single_buffer=True) == payload
    assert source.read == source.written == source.total == len(payload)
    assert len(calls) > 100 and max(size for _, size in calls) <= staging._BLOCK
    source.close()


def test_semantic_error_and_close_failure_preserve_context_and_retire_storage(monkeypatch):
    budget = _budget()
    source = _stage([_payload((1,))], budget)
    stream = source.stream

    class BadClose:
        def readinto(self, value):
            return stream.readinto(value)

        def seek(self, *args):
            return stream.seek(*args)

        def close(self):
            stream.close()
            raise OSError(errno.EIO, "injected close after semantic error")

    source.stream = BadClose()

    def refuse(*args, **kwargs):
        raise wire.PreciseStackMapError("injected original semantic error")

    monkeypatch.setattr(wire, "_scan_stack_map_payload", refuse)
    with pytest.raises(wire.PreciseStackMapError, match="close after semantic error") as caught:
        _merge(source, budget)
    assert stream.closed and source.stream is None and source.rows == []
    pending, messages, seen = [caught.value], [], set()
    while pending:
        error = pending.pop()
        if id(error) in seen:
            continue
        seen.add(id(error))
        messages.append(str(error))
        pending.extend(other for other in (error.__cause__, error.__context__) if other is not None)
    assert "injected original semantic error" in messages
