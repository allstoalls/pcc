"""Host-only byte, owner and work proofs for the windowed Mach-O spool."""

import ctypes
from dataclasses import replace
import errno
import io
from pathlib import Path
import random
import struct
import sys
from types import SimpleNamespace

import pytest

from pcc.backend import macho_host_prepare as host
from pcc.backend import macho_host_stackmap as spool
from pcc.backend import precise_stackmap as wire
from tests.python.test_macho_stackmap_consumption import _payload
from tests.python.test_macho_windowed_prepare import _budget
from tests.python.test_pe_stackmap_suffix_sharing import (
    _assert_same_records,
    _payload as _vectors_payload,
    _root,
)


def _merge(payloads, *, limit=100_000_000, retained=137, single_buffer=True,
           inspect=False):
    budget = _budget(limit)
    budget.retained_bytes = retained
    owned = [bytes(memoryview(value)) for value in payloads]
    checks = []
    original = budget.check_peak

    def check(raw, *, records=0, entries=0, label="stack-map"):
        if inspect and label == "input":
            scanner = sys._getframe(1)
            state = scanner.f_back.f_locals
            assert state["scanned"] is state["source_index_cache"] is None
            assert state["input_bytes"] == sum(len(value) for value in owned if value is not None)
            assert all(all(type(field) is int for field in row) for row in state["functions"])
            root_index = state["root_index"]
            assert type(root_index) is spool._RootIndex
            assert root_index.rows is None or type(root_index.rows) is bytearray
            assert root_index.index is None or type(root_index.index) is bytearray
            assert not hasattr(root_index, "__dict__")
            assert "table" not in state and "chunks" not in state
            del scanner, state, root_index
        if inspect and label == "spool merge materialize":
            state = sys._getframe(1).f_locals
            assert owned == [None] * len(owned)
            assert state["input_bytes"] == 0
            assert all(state[name] is None for name in (
                "payload", "blob", "scanned", "source_index_cache", "root_index",
                "seen_ids", "digest", "scratch", "buffer", "view",
            ))
            assert state["work"].written == state["total"] - wire.HEADER_SIZE
            assert state["work"].written <= budget.limit_bytes
            for name in ("source", "candidate", "header", "block"):
                view = state.get(name)
                if view is not None:
                    with pytest.raises(ValueError, match="released memoryview"):
                        _ = view.obj
            del state, view
        estimate = raw + budget.retained_bytes + entries * budget.metadata_entry_bytes
        if records:
            estimate += budget._set_bytes(records)
        checks.append((label, raw, entries, estimate))
        return original(raw, records=records, entries=entries, label=label)

    budget.check_peak = check
    result = spool.merge_owned_stack_map_payloads(
        owned, compact_budget=budget, single_buffer=single_buffer,
    )
    assert budget.retained_bytes == retained
    assert owned == [None] * len(owned)
    return result, checks, budget


@pytest.mark.parametrize("arch", (wire.ARCH_AARCH64, wire.ARCH_X86_64))
@pytest.mark.parametrize("records", (0, 4, 4097))
@pytest.mark.parametrize("empty_roots", (False, True))
@pytest.mark.parametrize("single_buffer", (False, True))
def test_spool_preserves_every_byte_offset_record_and_large_function(
    arch, records, empty_roots, single_buffer,
):
    payloads = tuple(_payload(ids, arch=arch, records=records, empty_roots=empty_roots)
                     for ids in ((3, 5), (), (1, 2, 4)))
    expected = wire.merge_stack_map_payloads(payloads, compact_budget=_budget(100_000_000))
    actual, _, budget = _merge(payloads, single_buffer=single_buffer, inspect=True)
    assert actual == expected
    assert budget.record_visits == 5 * records
    decoded = wire.decode_stack_map(actual[0])
    assert [function.function_id for function in decoded.functions] == [1, 2, 3, 4, 5]
    assert all(function.function_address == 0 for function in decoded.functions)


@pytest.mark.parametrize("seed", range(8))
def test_varied_vectors_preserve_gc_fields_and_exact_whole_vector_layout(seed):
    rng = random.Random(seed)
    roots = tuple(_root(number) for number in range(1, 9))
    vectors = [(), (roots[0], roots[1], _root(10, base=0)),
               (roots[1], _root(10, base=0))]
    for _ in range(64):
        value = tuple(rng.choice(roots) for _ in range(rng.randrange(1, 9)))
        vectors.extend((value, value[rng.randrange(len(value)):], value))
    rng.shuffle(vectors)
    payloads = (_vectors_payload(vectors, fid=2), _vectors_payload(vectors[::-1], fid=1))
    expected = wire.merge_stack_map_payloads(payloads, compact_budget=_budget(100_000_000))
    actual, _, _ = _merge(payloads, inspect=True)
    assert actual == expected
    _assert_same_records(expected[0], actual[0])


def test_digest_collisions_are_compared_and_never_share_unequal_vectors(monkeypatch):
    payloads = tuple(_vectors_payload(((_root(1),), (_root(2),), (_root(3), _root(4)),
                                      (_root(1),), ()), fid=fid) for fid in (3, 1, 2))
    expected = wire.merge_stack_map_payloads(payloads, compact_budget=_budget())
    monkeypatch.setattr(spool.hashlib, "sha256", lambda value: SimpleNamespace(digest=lambda: b"x" * 32))
    actual, _, _ = _merge(payloads)
    assert actual == expected


def test_digest_bucket_work_refuses_before_unbounded_collisions(monkeypatch):
    payload = _vectors_payload(tuple((_root(number),) for number in range(1, 7)))
    monkeypatch.setattr(wire, "MAX_LOCATIONS", 6)
    monkeypatch.setattr(spool.hashlib, "sha256", lambda value: SimpleNamespace(digest=lambda: b"x" * 32))
    with pytest.raises(wire.PreciseStackMapError, match="comparison work budget exceeded"):
        _merge((payload,))


def test_relative_collision_limit_prevents_quadratic_work_below_absolute_cap(monkeypatch):
    payload = _vectors_payload(tuple((_root(number),) for number in range(1, 33)))
    monkeypatch.setattr(spool.hashlib, "sha256", lambda value: SimpleNamespace(digest=lambda: b"x" * 32))
    original = spool._SpoolWork.charge_comparison
    refused = []

    def charge(work, count):
        try:
            return original(work, count)
        except wire.PreciseStackMapError:
            refused.append((work.compared_locations, work.comparison_limit))
            raise

    monkeypatch.setattr(spool._SpoolWork, "charge_comparison", charge)
    with pytest.raises(wire.PreciseStackMapError, match="comparison work budget exceeded"):
        _merge((payload,))
    assert len(refused) == 1
    compared, relative_limit = refused[0]
    assert compared == relative_limit < wire.MAX_LOCATIONS
    assert relative_limit < 32 * 4


def test_collision_comparison_reads_the_final_block_of_long_vectors(monkeypatch):
    first = (_root(1),) * (spool._BLOCK_BYTES // wire.LOCATION_SIZE + 1)
    second = first[:-1] + (_root(2),)
    payloads = (_vectors_payload((first, second), fid=2),
                _vectors_payload((second, first), fid=1))
    expected = wire.merge_stack_map_payloads(payloads, compact_budget=_budget(100_000_000))
    monkeypatch.setattr(spool.hashlib, "sha256", lambda value: SimpleNamespace(digest=lambda: b"x" * 32))
    actual, _, _ = _merge(payloads)
    assert actual == expected
    assert int.from_bytes(actual[0][16:20], "little") == 2 * len(first)


def test_original_distinct_range_work_and_record_caps_are_not_raised(monkeypatch):
    payloads = tuple(_vectors_payload(((_root(1), _root(2)), (_root(2),)), fid=fid)
                     for fid in (1, 2))
    monkeypatch.setattr(wire, "MAX_LOCATIONS", 5)
    with pytest.raises(wire.PreciseStackMapError, match="location merge work"):
        _merge(payloads)
    monkeypatch.setattr(wire, "MAX_LOCATIONS", 6)
    actual, _, budget = _merge(payloads)
    assert budget.record_visits == 4
    assert actual == wire.merge_stack_map_payloads(payloads, compact_budget=_budget())
    budget = _budget()
    budget.record_visit_limit = 3
    with pytest.raises(wire.PreciseStackMapError, match="record work budget exceeded"):
        spool.merge_owned_stack_map_payloads(list(payloads), compact_budget=budget, single_buffer=True)
    assert budget.record_visit_limit == 3 and budget.record_visits == 2


@pytest.mark.parametrize("failure", ("truncated", "mixed-arch", "duplicate", "bad-index"))
def test_failed_input_preserves_error_and_only_retires_completed_slots(failure):
    first, second = _payload((1,)), _payload((2,))
    if failure == "truncated":
        second = second[:12]
    elif failure == "mixed-arch":
        second = _payload((2,), arch=wire.ARCH_X86_64)
    elif failure == "duplicate":
        second = _payload((1,))
    elif failure == "bad-index":
        value = bytearray(second)
        struct.pack_into("<I", value, wire.HEADER_SIZE + wire.FUNCTION_SIZE + 28, 0xFFFFFFFF)
        second = bytes(value)
    errors = []
    for merge in (lambda owned, budget: wire.merge_stack_map_payloads(
            owned, compact_budget=budget, _consume_payloads=True),
            lambda owned, budget: spool.merge_owned_stack_map_payloads(
                owned, compact_budget=budget, single_buffer=True)):
        owned = [bytes(memoryview(first)), bytes(memoryview(second))]
        budget = _budget()
        budget.retained_bytes = 137
        with pytest.raises(wire.PreciseStackMapError) as caught:
            merge(owned, budget)
        assert owned == [None, second] and budget.retained_bytes == 137
        errors.append(str(caught.value))
    assert errors[0] == errors[1]


def test_actual_input_references_retire_before_next_scan(monkeypatch):
    kept = _payload((1,))
    owned = [kept, _payload((2,))]
    references = sys.getrefcount(kept)
    original = wire._scan_stack_map_payload
    scans = 0

    def scan(value, **kwargs):
        nonlocal scans
        scans += 1
        if scans == 2:
            assert owned[0] is None
            assert sys.getrefcount(kept) == references - 1
        return original(value, **kwargs)

    monkeypatch.setattr(wire, "_scan_stack_map_payload", scan)
    budget = _budget()
    budget.retained_bytes = sys.getsizeof(kept)
    spool.merge_owned_stack_map_payloads(owned, compact_budget=budget, single_buffer=True)
    assert sys.getrefcount(kept) == references - 1


def test_verified_bytesio_keeps_the_same_backing_through_write_and_freeze(monkeypatch):
    assert host._single_buffer_window_supported()
    address = ctypes.pythonapi.PyBytes_AsString
    address.argtypes = [ctypes.py_object]
    address.restype = ctypes.c_void_p
    addresses = []
    original = io.BytesIO

    class ObservedBytesIO:
        def __init__(self, initial):
            addresses.append(("initial", address(initial)))
            self.inner = original(initial)

        def getbuffer(self):
            view = self.inner.getbuffer()
            addresses.append(("writable", ctypes.addressof(ctypes.c_char.from_buffer(view))))
            return view

        def getvalue(self):
            value = self.inner.getvalue()
            addresses.append(("frozen", address(value)))
            return value

        def close(self):
            self.inner.close()

    monkeypatch.setattr(spool.io, "BytesIO", ObservedBytesIO)
    payloads = (_payload((1,), records=4097), _payload((2,), records=4097))
    actual, _, _ = _merge(payloads)
    assert actual == wire.merge_stack_map_payloads(payloads, compact_budget=_budget(100_000_000))
    assert [name for name, _ in addresses] == ["initial", "writable", "frozen"]
    assert len({value for _, value in addresses}) == 1


@pytest.mark.parametrize("single_buffer", (False, True))
def test_exact_materialization_boundary_and_portable_two_copy_freeze(single_buffer):
    payloads = tuple(_payload((number,), records=1024, empty_roots=True) for number in range(1, 25))
    expected, checks, _ = _merge(payloads, single_buffer=single_buffer, inspect=True)
    limit = max(value for _, _, _, value in checks)
    assert _merge(payloads, limit=limit, single_buffer=single_buffer)[0] == expected
    with pytest.raises(wire.PreciseStackMapError, match="byte budget exceeded"):
        _merge(payloads, limit=limit - 1, single_buffer=single_buffer)
    materialize, = [row for row in checks if row[0] == "spool merge materialize"]
    copies = 1 if single_buffer else 2
    assert materialize[1] == copies * _budget()._rounded(sys.getsizeof(b"") + len(expected[0])) + spool._FIXED_BYTES
    if single_buffer:
        budget = _budget(limit)
        budget.retained_bytes = 137
        with pytest.raises(wire.PreciseStackMapError, match="byte budget exceeded"):
            wire.merge_stack_map_payloads([bytes(memoryview(value)) for value in payloads],
                                         compact_budget=budget, _consume_payloads=True)


@pytest.mark.parametrize("failure", ("none", "short-progress", "write-zero", "read-zero", "disk-full", "create-second", "close"))
def test_spools_close_on_success_partial_io_and_every_failure(monkeypatch, failure):
    original = spool.tempfile.TemporaryFile
    opened = []

    class Wrapped:
        def __init__(self):
            self.inner = original(mode="w+b", buffering=0)
            opened.append(self.inner)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.inner.close()
            if failure == "close":
                raise OSError(errno.EIO, "injected file close failure")

        def seek(self, *args):
            return self.inner.seek(*args)

        def write(self, value):
            if failure == "write-zero":
                return 0
            if failure == "disk-full":
                raise OSError(errno.ENOSPC, "injected full temporary disk")
            return self.inner.write(value[:7] if failure == "short-progress" else value)

        def readinto(self, value):
            if failure == "read-zero":
                return 0
            return self.inner.readinto(value[:7] if failure == "short-progress" else value)

    def temporary(**kwargs):
        assert kwargs == {"mode": "w+b", "buffering": 0}
        if failure == "create-second" and opened:
            raise OSError(errno.EMFILE, "injected second file creation failure")
        return Wrapped()

    monkeypatch.setattr(spool.tempfile, "TemporaryFile", temporary)
    payloads = (_payload((1,)), _payload((2,)))
    if failure in ("none", "short-progress"):
        assert _merge(payloads)[0] == wire.merge_stack_map_payloads(payloads, compact_budget=_budget())
    else:
        with pytest.raises(wire.PreciseStackMapError, match="spool|temporary storage"):
            _merge(payloads)
    assert opened and all(stream.closed for stream in opened)


@pytest.mark.parametrize("when", ("getbuffer", "getvalue", "close"))
def test_buffer_failure_traceback_retains_no_input_or_output_bytes(monkeypatch, when):
    original = io.BytesIO
    closed = []

    class FailingBuffer:
        def __init__(self, value):
            self.inner = original(value)

        def getbuffer(self):
            if when == "getbuffer":
                raise OSError(errno.EIO, "injected buffer failure")
            return self.inner.getbuffer()

        def getvalue(self):
            if when == "getvalue":
                raise OSError(errno.EIO, "injected buffer failure")
            return self.inner.getvalue()

        def close(self):
            self.inner.close()
            closed.append(True)
            if when == "close":
                raise OSError(errno.EIO, "injected buffer failure")

    monkeypatch.setattr(spool.io, "BytesIO", FailingBuffer)
    owned = [_payload((1,), records=4097)]
    with pytest.raises(wire.PreciseStackMapError, match="injected buffer failure") as caught:
        spool.merge_owned_stack_map_payloads(owned, compact_budget=_budget(), single_buffer=True)
    assert owned == [None] and closed == [True]
    trace = caught.value.__traceback__
    while trace is not None:
        if trace.tb_frame.f_code is spool.merge_owned_stack_map_payloads.__code__:
            state = trace.tb_frame.f_locals
            assert all(state[name] is None for name in (
                "payload", "blob", "scanned", "source_index_cache", "buffer", "view", "scratch", "result", "closing",
            ))
            assert state["functions"] == state["address_offsets"] == []
        trace = trace.tb_next


def test_disk_read_and_write_work_limits_fail_before_io():
    work = spool._SpoolWork(_budget(100))
    stream = io.BytesIO()
    with pytest.raises(wire.PreciseStackMapError, match="write work budget"):
        work.write_all(stream, b"x" * 101)
    assert stream.tell() == 0 and work.written == 0
    work.read_limit = 3
    with memoryview(bytearray(4)) as view:
        with pytest.raises(wire.PreciseStackMapError, match="read work budget"):
            work.read_exact(stream, view)
    assert stream.tell() == 0 and work.read == 0


def test_spool_helper_is_excluded_from_native_dependency_roots():
    from pcc.frontends.python import pipeline_import_scan as scan
    from pcc.frontends.python.pipeline_dependency_closure import _package_import_targets

    root = Path(__file__).resolve().parents[2]
    for relative, module in (("pcc/backend/native_object.py", "pcc.backend.native_object"),
                             ("pcc/frontends/python/pipeline_self_backend_link.py",
                              "pcc.frontends.python.pipeline_self_backend_link")):
        path = root / relative
        source = path.read_text()
        native = scan._without_inactive_implementation_imports(source, implementation="pcc")
        assert "from .macho_host_prepare import" not in native
        assert "from pcc.backend.macho_host_prepare import" not in native
        targets = _package_import_targets(str(path), module, root_dir=str(root))
        assert "pcc.backend.macho_host_prepare" not in {name for _, name in targets}


@pytest.mark.parametrize("backend", range(5))
@pytest.mark.parametrize("mode", (1, 3))
def test_runtime_consumes_original_slots_pcs_and_backend_contract(monkeypatch, backend, mode):
    from pcc.frontends.python.codegen.freestanding_abi_constants import ABI_CONSTANTS
    from pcc.runtime.py import freestanding_gc_mapped_roots as runtime

    owned_root = _root(1)
    borrowed_root = replace(_root(2), flags=wire.LOCATION_MANAGED)
    payloads = tuple(_vectors_payload(((owned_root, borrowed_root), (), (borrowed_root,)), fid=fid)
                     for fid in (2, 1))
    (merged, offsets), _, _ = _merge(payloads)
    table = bytearray(merged)
    for fid, offset in offsets:
        struct.pack_into("<Q", table, offset, 4096 * fid)
    table = bytes(table)
    decoded = wire.decode_stack_map(table, final_image=True)
    visited = []
    frame = object()
    monkeypatch.setattr(runtime, "abi_constant", ABI_CONSTANTS.__getitem__)
    monkeypatch.setattr(runtime, "gc_backend_current", lambda: backend)
    monkeypatch.setattr(runtime, "null", lambda: None)
    monkeypatch.setattr(runtime, "ptr_is_null", lambda value: int(value is None))
    monkeypatch.setattr(runtime, "ptr_add", lambda pointer, offset: (pointer[0], pointer[1] + offset))
    monkeypatch.setattr(runtime, "load_i32", lambda pointer, offset: struct.unpack_from(
        "<i", pointer[0], pointer[1] + offset)[0])
    monkeypatch.setattr(runtime, "load_i64", lambda pointer, offset: struct.unpack_from(
        "<q", pointer[0], pointer[1] + offset)[0])
    monkeypatch.setattr(runtime, "unsigned_div_i64", lambda a, b: (
        (a & ((1 << 64) - 1)) // (b & ((1 << 64) - 1))))

    def visit(base, offset, stable, borrowed, operation, resolve):
        visited.append((base, offset, stable, borrowed, operation, resolve))
        return 1 if operation == 3 else 0

    monkeypatch.setattr(runtime, "pcc_gc_visit_mapped_root_slot", visit)
    for function in decoded.functions:
        for record in function.records:
            pc = function.function_address + record.instruction_offset
            visited.clear()
            result = runtime.pcc_gc_consume_precise_stackmap(
                (table, 0), len(table), pc, frame, wire.ARCH_X86_64, mode,
            )
            if backend < 3:
                assert result == 0 and visited == []
            else:
                assert result == len(record.locations)
                assert visited == [(frame, root.offset, None,
                                    int(not bool(root.flags & wire.LOCATION_OWNED)), mode, 0)
                                   for root in record.locations]
            visited.clear()
            result = runtime.pcc_gc_consume_precise_stackmap(
                (table, 0), len(table), pc + 1, frame, wire.ARCH_X86_64, mode,
            )
            assert result == (0 if backend < 3 else -13) and visited == []
