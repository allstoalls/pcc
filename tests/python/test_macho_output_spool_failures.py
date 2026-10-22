"""Staged host-output failure ownership; no native provisioning or execution."""

import errno
import sys
import weakref

import pytest

from pcc.backend import macho_codesign as codesign
from pcc.backend import macho_exec
from pcc.backend import macho_host_output as output
from pcc.backend import macho_host_prepare as host
from pcc.backend import macho_host_validation as validation_boundary
from pcc.backend import precise_stackmap as maps
from tests.python.test_macho_owned_output import _stack_inputs, _tracked_buffers
from tests.python.test_macho_windowed_prepare import _budget


class _BoundedFile:
    """Real anonymous storage with faults at specific output transfer lanes."""

    def __init__(self, raw, role, run):
        self.raw, self.role, self.run = raw, role, run
        run.files.append(self)

    @property
    def closed(self):
        return self.raw.closed

    def fileno(self):
        return self.raw.fileno()

    def seek(self, *args):
        return self.raw.seek(*args)

    def close(self):
        self.raw.close()
        if self.role == "output-payload" and self.run.close_fault:
            self.run.close_hits.append(self.role)
            raise OSError(errno.EBADF, "deliberate payload close failure")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def read(self, size):
        assert 0 <= size <= 64 * 1024
        self.run.requests.append((self.role, "read", size))
        return self.raw.read(size)

    def _transfer(self, span, operation):
        assert 0 < len(span) <= 64 * 1024
        if self.role == "output-payload":
            if operation == "write":
                lane = "chain-write" if type(span) is bytes else "spill-write"
            else:
                lane = ("spool-payload-read" if self.run.phase == "output_spool_begin"
                        else "replay-read")
        elif self.role == "final-spool":
            lane = "spool-write" if operation == "write" else "final-read"
        else:
            lane = self.role + "-" + operation
        self.run.requests.append((self.role, lane, len(span)))
        if lane == "chain-write":
            # This fixture has no synthesized sections. Every ordinary
            # backing must already be file-owned, even for chained patching.
            assert len(span) == 8
            assert all(owner.closed or owner.size == 0 for owner in self.run.owners)
        fault = self.run.fault if lane == self.run.target else None
        if fault is not None:
            self.run.hits.append(lane)
        if fault == "zero":
            return 0
        if fault == "oversized":
            return len(span) + 1
        if fault == "enospc":
            raise OSError(errno.ENOSPC, "deliberate output storage exhaustion")
        if fault == "child-error":
            # Keep the export only through a closed callback's chained
            # tracebacks. Cleanup must clear these before closing BytesIO.
            child = span[:1]
            self.run.children.append(weakref.ref(child))
            try:
                raise OSError(errno.EIO, "deliberate replay read failure")
            except OSError as cause:
                raise ValueError("replay callback failed") from cause
        count = len(span)
        if fault in ("work", "short-word"):
            count = 1 if fault == "work" else 4
        elif self.run.short_io and lane != "chain-write":
            count = max(1, (count + 1) // 2)
        with memoryview(span)[:count] as part:
            return getattr(self.raw, operation)(part)

    def write(self, span):
        return self._transfer(span, "write")

    def readinto(self, span):
        return self._transfer(span, "readinto")


class _StagedRun:
    def __init__(self, tmp_path, monkeypatch, *, target=None, fault=None,
                 short_io=False, reference=False, close_fault=False):
        paths = _stack_inputs(tmp_path, records=64, data_bytes=64 * 1024 + 24)
        self.expected = None
        if reference:
            self.expected = macho_exec.link_prepared_executable(
                host.prepare_executable_from_paths(paths, compact_budget=_budget(50_000_000)),
                compact_budget=_budget(50_000_000),
            )
        self.budget = _budget(50_000_000)
        self.budget.retained_bytes = 137
        self.pending = [host.prepare_executable_from_paths(
            paths, compact_budget=self.budget, _staged_rows=True,
        )]
        self.source = weakref.ref(self.pending[0])
        self.payloads = self.pending[0]._host_payload_store
        self.rows = self.pending[0]._host_row_store
        assert self.payloads is not None and self.rows is not None
        self.files, self.requests, self.hits, self.children = [], [], [], []
        self.close_fault, self.close_hits = close_fault, []
        self.phase, self.events = None, []
        self.target, self.fault, self.short_io = target, fault, short_io
        self.payloads.stream = _BoundedFile(self.payloads.stream, "source-payload", self)
        self.rows.stream = _BoundedFile(self.rows.stream, "source-rows", self)
        self.owners = _tracked_buffers(monkeypatch)
        temporary = output.tempfile.TemporaryFile
        self.created = []

        def temporary_file(*args, **kwargs):
            assert len(self.created) < 2
            role = "output-payload" if not self.created else "final-spool"
            result = _BoundedFile(temporary(*args, **kwargs), role, self)
            self.created.append(result)
            return result

        monkeypatch.setattr(output.tempfile, "TemporaryFile", temporary_file)

    def observe_phase(self, event):
        self.phase = event
        self.events.append(event)
        if event == "output_spool_begin":
            assert self.payloads.stream is None and self.rows.stream is None
        if event in ("final_backing_begin", "sign_begin"):
            assert self.source() is None
            assert self.created[0].closed

    def link(self):
        return output.link_owned_executable(
            self.pending.pop(), compact_budget=self.budget,
            _phase_callback=self.observe_phase,
        )

    def assert_retired(self, *, escaped_export=False):
        # These assertions run while pytest still owns the original exception.
        assert not self.pending and self.source() is None
        assert self.payloads.stream is None and self.rows.stream is None
        assert self.payloads.rows == self.rows.rows == []
        assert all(stream.closed for stream in self.files)
        if not escaped_export:
            assert self.owners and all(owner.closed for owner in self.owners)
        assert self.budget.retained_bytes == 137
        assert self.requests and max(size for _, _, size in self.requests) <= 64 * 1024
        # Exact-sized allocations may be large; getvalue/getbuffer must never
        # replace one of those real allocations with a whole-backing copy.
        assert all(len(set(owner.addresses)) <= 1 for owner in self.owners)


def test_staged_short_io_is_bounded_and_byte_identical(tmp_path, monkeypatch):
    run = _StagedRun(tmp_path, monkeypatch, short_io=True, reference=True)
    actual = run.link()
    assert type(actual) is bytes and actual == run.expected
    signature = codesign.parse_signature(actual)
    output.check_signature_verification(actual, signature, run.budget)
    assert {lane for _, lane, _ in run.requests} >= {
        "spill-write", "chain-write", "replay-read", "spool-payload-read",
        "spool-write", "final-read", "source-payload-readinto", "source-rows-readinto",
    }
    assert max(size for _, _, size in run.requests) == 64 * 1024
    assert len(run.created) == 2
    run.assert_retired()


@pytest.mark.parametrize("failure", ("validator", "signer"))
@pytest.mark.parametrize("table_format", (None, "2", "3"))
def test_staged_retained_callback_error_releases_every_owner(tmp_path, monkeypatch, table_format, failure):
    if table_format is None:
        monkeypatch.delenv(maps.PRECISE_STACKMAP_FORMAT_ENV, raising=False)
    else:
        monkeypatch.setenv(maps.PRECISE_STACKMAP_FORMAT_ENV, table_format)
    run = _StagedRun(tmp_path, monkeypatch)

    def validation(payload, **kwargs):
        assert kwargs["final_image"] is True and type(payload) is bytes
        child = memoryview(payload)[:1]
        run.children.append(weakref.ref(child))
        raise ValueError("deliberate final validator failure")

    def signature(payload, **kwargs):
        child = payload[:1]
        run.children.append(weakref.ref(child))
        raise ValueError("deliberate final signer failure")

    if failure == "validator":
        monkeypatch.setattr(validation_boundary, "validate_stack_map_payload", validation)
    else:
        monkeypatch.setattr(codesign, "build_signature", signature)
    with pytest.raises(ValueError, match="deliberate final") as retained:
        run.link()
    assert retained.value is not None
    assert run.children and all(child() is None for child in run.children)
    assert len(run.created) == (1 if failure == "validator" else 2)
    run.assert_retired()


@pytest.mark.parametrize("table_format", (None, "2", "3"))
def test_staged_validator_snapshot_escape_refuses_before_thaw(tmp_path, monkeypatch, table_format):
    if table_format is None:
        monkeypatch.delenv(maps.PRECISE_STACKMAP_FORMAT_ENV, raising=False)
    else:
        monkeypatch.setenv(maps.PRECISE_STACKMAP_FORMAT_ENV, table_format)
    run = _StagedRun(tmp_path, monkeypatch)
    escaped = []
    validate = validation_boundary.validate_stack_map_payload

    def validation(payload, **kwargs):
        result = validate(payload, **kwargs)
        escaped.append(payload)
        return result

    monkeypatch.setattr(validation_boundary, "validate_stack_map_payload", validation)
    with pytest.raises(output.LinkError, match="snapshot alias") as retained:
        run.link()
    assert retained.value is not None and len(escaped) == 1
    assert "stackmap_thaw_begin" in run.events and "stackmap_thaw_end" not in run.events
    assert "final_backing_begin" not in run.events
    assert max(len(owner.addresses) for owner in run.owners) == 3
    run.assert_retired()
    validate(escaped[0], final_image=True, compact_budget=_budget(50_000_000))


@pytest.mark.parametrize("instrument", ("trace", "profile", "monitoring"))
@pytest.mark.parametrize("table_format", (None, "2", "3"))
def test_staged_validator_instrumentation_refuses_before_thaw(tmp_path, monkeypatch, table_format, instrument):
    if table_format is None:
        monkeypatch.delenv(maps.PRECISE_STACKMAP_FORMAT_ENV, raising=False)
    else:
        monkeypatch.setenv(maps.PRECISE_STACKMAP_FORMAT_ENV, table_format)
    run = _StagedRun(tmp_path, monkeypatch)
    validate = validation_boundary.validate_stack_map_payload
    prior_trace, prior_profile = sys.gettrace(), sys.getprofile()
    monitoring_started = False
    events = []

    def hook(frame, event, arg):
        events.append(event)
        return hook

    def validation(payload, **kwargs):
        nonlocal monitoring_started
        result = validate(payload, **kwargs)
        if instrument == "trace":
            sys.settrace(hook)
        elif instrument == "profile":
            sys.setprofile(hook)
        else:
            assert sys.monitoring.get_tool(5) is None
            sys.monitoring.use_tool_id(5, "staged-output-validator-test")
            monitoring_started = True
            # A reserved tool with no global events still invalidates the
            # exclusive-owner assumption; it can enable local events later.
            assert sys.monitoring.get_events(5) == 0
        return result

    monkeypatch.setattr(validation_boundary, "validate_stack_map_payload", validation)
    try:
        with pytest.raises(output.LinkError, match="uninstrumented") as retained:
            run.link()
    finally:
        sys.settrace(prior_trace)
        sys.setprofile(prior_profile)
        if monitoring_started:
            sys.monitoring.free_tool_id(5)
    assert retained.value is not None
    if instrument == "monitoring":
        assert monitoring_started
    else:
        assert events
    assert "stackmap_thaw_end" not in run.events and "final_backing_begin" not in run.events
    assert max(len(owner.addresses) for owner in run.owners) == 3
    run.assert_retired()


@pytest.mark.parametrize("target,fault,diagnostic", (
    ("spill-write", "zero", "invalid write progress"),
    ("spill-write", "oversized", "invalid write progress"),
    ("spill-write", "work", "I/O work bound"),
    ("spill-write", "enospc", "storage exhaustion"),
    ("replay-read", "zero", "invalid read progress"),
    ("replay-read", "oversized", "invalid read progress"),
    ("replay-read", "work", "I/O work bound"),
    ("spool-write", "zero", "invalid write progress"),
    ("spool-write", "oversized", "invalid write progress"),
    ("final-read", "zero", "invalid read progress"),
))
def test_staged_io_faults_refuse_and_release_retained_owners(
        tmp_path, monkeypatch, target, fault, diagnostic):
    run = _StagedRun(tmp_path, monkeypatch, target=target, fault=fault)
    with pytest.raises((output.LinkError, OSError), match=diagnostic) as retained:
        run.link()
    assert retained.value is not None and run.hits
    if fault == "enospc":
        assert retained.value.errno == errno.ENOSPC
    if fault == "work":
        assert len(run.hits) > 1
    assert "sign_begin" not in run.events
    run.assert_retired()


def test_staged_replay_read_traceback_child_is_retired_before_buffer_close(tmp_path, monkeypatch):
    run = _StagedRun(tmp_path, monkeypatch, target="replay-read", fault="child-error")
    with pytest.raises(ValueError, match="replay callback failed") as retained:
        run.link()
    assert isinstance(retained.value.__cause__, OSError)
    assert retained.value.__cause__.errno == errno.EIO
    assert run.children and all(child() is None for child in run.children)
    assert len(run.created) == 1 and "stackmap_payload_validate_begin" not in run.events
    run.assert_retired()


@pytest.mark.parametrize("instrument", ("trace", "profile", "monitoring"))
def test_staged_replay_admission_instrumentation_refuses_before_allocation(
        tmp_path, monkeypatch, instrument):
    run = _StagedRun(tmp_path, monkeypatch)
    check = run.budget.check_peak
    admitted = []
    prior_trace, prior_profile = sys.gettrace(), sys.getprofile()
    monitoring_started = False

    def hook(frame, event, arg):
        return hook

    def checking(raw, *, records=0, entries=0, label="stack-map"):
        nonlocal monitoring_started
        result = check(raw, records=records, entries=entries, label=label)
        if label == "Mach-O owned payload validation replay":
            admitted.append(len(run.owners))
            if instrument == "trace":
                sys.settrace(hook)
            elif instrument == "profile":
                sys.setprofile(hook)
            else:
                assert sys.monitoring.get_tool(5) is None
                sys.monitoring.use_tool_id(5, "staged-output-replay-admission-test")
                monitoring_started = True
                assert sys.monitoring.get_events(5) == 0
        return result

    monkeypatch.setattr(run.budget, "check_peak", checking)
    try:
        with pytest.raises(output.LinkError, match="uninstrumented") as retained:
            run.link()
    finally:
        sys.settrace(prior_trace)
        sys.setprofile(prior_profile)
        if monitoring_started:
            sys.monitoring.free_tool_id(5)
    assert retained.value is not None
    assert admitted == [len(run.owners)]
    assert all(lane != "replay-read" for _, lane, _ in run.requests)
    assert "stackmap_payload_validate_begin" not in run.events
    run.assert_retired()


@pytest.mark.parametrize("target,fault", (
    ("spill-write", "enospc"),
    ("replay-read", "child-error"),
))
def test_staged_primary_io_failure_survives_payload_close_failure(
        tmp_path, monkeypatch, target, fault):
    run = _StagedRun(tmp_path, monkeypatch, target=target, fault=fault, close_fault=True)
    expected = OSError if fault == "enospc" else ValueError
    message = "storage exhaustion" if fault == "enospc" else "replay callback failed"
    with pytest.raises(expected, match=message) as retained:
        run.link()
    if fault == "enospc":
        assert retained.value.errno == errno.ENOSPC
    else:
        assert isinstance(retained.value.__cause__, OSError)
        assert retained.value.__cause__.errno == errno.EIO
        assert run.children and all(child() is None for child in run.children)
    assert run.hits and run.close_hits == ["output-payload"]
    run.assert_retired()


def test_staged_chained_word_short_write_refuses_without_section_rehydration(tmp_path, monkeypatch):
    run = _StagedRun(tmp_path, monkeypatch, target="chain-write", fault="short-word")
    with pytest.raises(output.LinkError, match="chained pointer write was short") as retained:
        run.link()
    assert retained.value is not None and run.hits == ["chain-write"]
    assert all(size == 8 for _, lane, size in run.requests if lane == "chain-write")
    assert "output_spool_begin" not in run.events
    run.assert_retired()


def test_staged_source_child_export_refuses_before_transfer_credit(tmp_path, monkeypatch):
    run = _StagedRun(tmp_path, monkeypatch)
    escaped, charged = [], []
    patch = output._patch_staged_payload
    take = output._OwnedOutput.take

    def patch_and_escape(source, index, data, check):
        patch(source, index, data, check)
        escaped.append(data[:1])

    def observe_credit(self, *args, **kwargs):
        previous = self.extra_raw
        try:
            return take(self, *args, **kwargs)
        except BufferError:
            charged.append((previous, self.extra_raw))
            raise

    monkeypatch.setattr(output, "_patch_staged_payload", patch_and_escape)
    monkeypatch.setattr(output._OwnedOutput, "take", observe_credit)
    try:
        with pytest.raises(BufferError) as retained:
            run.link()
        assert retained.value is not None and len(escaped) == 1
        assert charged and all(before == after for before, after in charged)
        assert run.payloads.consumed == run.payloads.public_copy_bytes == 0
        assert run.payloads.active_bytes > 0
        assert run.created == []
        run.assert_retired(escaped_export=True)
        active = [owner for owner in run.owners if not owner.closed]
        assert len(active) == 1 and len(active[0].addresses) == 2
    finally:
        for child in escaped:
            child.release()
        for owner in run.owners:
            owner.close()
    assert all(owner.closed for owner in run.owners)
