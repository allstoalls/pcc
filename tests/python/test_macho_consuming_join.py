"""Bounded host ownership and byte parity for consuming Mach-O final joins."""

import ctypes
import errno
import io
import sys
from types import SimpleNamespace

import pytest

from pcc.backend import macho_host_join as join, macho_host_prepare as host
from pcc.backend import macho_exec, macho_host_output as output, macho_codesign as codesign
from pcc.backend import macho_link, macho_obj as obj, macho_spec as spec
from pcc.backend import native_object as native, precise_stackmap as maps
from tests.python.test_macho_owned_output import _factory_budget, _stack_inputs
from tests.python.test_macho_windowed_parity import _prepared_state, _stackmap_section, _write_pcos
from tests.python.test_macho_windowed_prepare import _write_inputs


def _ledger(parts, limit=20_000_000):
    budget = _factory_budget(limit)
    budget.retained_bytes = 137
    ledger = host._Ledger(budget)
    ledger.retained = 251 + sum(ledger.immutable(len(part)) for part in parts)
    return ledger


def _track_buffers(monkeypatch):
    owners = []
    bytes_address = ctypes.pythonapi.PyBytes_AsString
    bytes_address.argtypes = [ctypes.py_object]
    bytes_address.restype = ctypes.c_void_p
    original = io.BytesIO

    class Tracked(original):
        def __init__(self, value):
            super().__init__(value)
            self.addresses = [bytes_address(value)] if len(value) > 1 else []
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

    monkeypatch.setattr(join.io, "BytesIO", Tracked)
    return owners


def _files(monkeypatch, mode="short", failure=None):
    original = join.tempfile.TemporaryFile
    files = []

    class File:
        def __init__(self):
            self.inner = original(mode="w+b", buffering=0)
            self.calls = []
            files.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.inner.close()
            if mode == "close":
                raise OSError(errno.EIO, "injected close")

        def write(self, value):
            self.calls.append(("write", len(value)))
            if mode in ("write", "write-close"):
                if failure == "raise" or mode == "write-close":
                    raise OSError(errno.ENOSPC, "injected write")
                return len(value) + 1 if failure == "long" else failure
            count = min(97, len(value)) if mode == "short" else len(value)
            with value[:count] as part:
                return self.inner.write(part)

        def readinto(self, value):
            self.calls.append(("read", len(value)))
            if mode == "read":
                if failure == "raise":
                    child = value[:]  # closed failing frame must release this export
                    raise OSError(errno.EIO, "injected read")
                return len(value) + 1 if failure == "long" else failure
            count = min(83, len(value)) if mode == "short" else len(value)
            with value[:count] as part:
                return self.inner.readinto(part)

        def seek(self, *args):
            value = self.inner.seek(*args)
            return value + 1 if mode == "seek" else value

    if mode == "write-close":
        def exit(self, *_args):
            self.inner.close()
            raise OSError(errno.EIO, "injected close")
        File.__exit__ = exit
    monkeypatch.setattr(join.tempfile, "TemporaryFile", lambda **_kwargs: File())
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
            if trace.tb_frame.f_code.co_filename.endswith("macho_host_join.py"):
                local = trace.tb_frame.f_locals
                for name in ("buffer", "view", "payload", "result", "closing", "patch", "stream", "source", "part"):
                    assert local[name] is None
                assert local["parts"] == []
            trace = trace.tb_next
        pending.extend(other for other in (current.__cause__, current.__context__) if other is not None)


@pytest.mark.parametrize("sizes", ((), (0,), (1,), (100001,), (65537, 3, 99999)))
def test_exact_join_bytes_and_one_backing_with_partial_io(monkeypatch, sizes):
    parts = [bytes([index + 1]) * size for index, size in enumerate(sizes)]
    expected = b"".join(parts)
    ledger = _ledger(parts)
    owners = _track_buffers(monkeypatch)
    files = _files(monkeypatch)
    actual = join._consume_parts(parts, ledger)
    assert type(actual) is bytes and actual == expected and parts == []
    assert ledger.retained == 251 + ledger.immutable(len(actual))
    assert ledger.budget.retained_bytes == 137
    assert all(owner.closed and len(set(owner.addresses)) <= 1 for owner in owners)
    assert all(file.inner.closed for file in files)
    assert max((size for file in files for _, size in file.calls), default=0) <= join._BLOCK


def test_previous_chunk_retires_before_next_write_and_final_admission(monkeypatch):
    kept = [b"a" * 65539, b"b" * 65541, b"c" * 65543]
    parts = list(kept)
    ledger = _ledger(parts)
    original = join.tempfile.TemporaryFile
    count = 0

    class File:
        def __init__(self):
            self.inner = original(mode="w+b", buffering=0)
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            self.inner.close()
        def write(self, value):
            nonlocal count
            if len(value) > join._BLOCK - 1:
                if count:
                    references = sys.getrefcount([kept[count - 1]].pop())
                    assert references == 2  # test list + explicitly strong temporary
                    assert parts[count - 1] is None
                count += 1
            return self.inner.write(value)
        def readinto(self, value):
            return self.inner.readinto(value)
        def seek(self, *args):
            return self.inner.seek(*args)

    check = ledger.check
    def checked(extra=0, *, label="Mach-O input window"):
        if label == "Mach-O consuming join backing":
            assert parts == []
            for index in range(len(kept)):
                references = sys.getrefcount([kept[index]].pop())
                assert references == 2
            assert ledger.retained == 251 + join._FIXED
        return check(extra, label=label)
    monkeypatch.setattr(ledger, "check", checked)
    monkeypatch.setattr(join.tempfile, "TemporaryFile", lambda **_kwargs: File())
    assert join._consume_parts(parts, ledger) == b"".join(kept)
    assert count == 3


@pytest.mark.parametrize("mode", ("write", "read"))
@pytest.mark.parametrize("failure", (0, -1, True, None, "long", "raise"))
def test_failed_io_retires_views_chunks_and_storage(monkeypatch, mode, failure):
    parts = [b"a" * 100001, b"b" * 100003]
    ledger = _ledger(parts)
    owners = _track_buffers(monkeypatch)
    files = _files(monkeypatch, mode, failure)
    with pytest.raises(maps.PreciseStackMapError) as caught:
        join._consume_parts(parts, ledger)
    assert parts == [] and all(file.inner.closed for file in files)
    assert all(owner.closed for owner in owners)
    assert ledger.budget.retained_bytes == 137
    _assert_retired(caught.value)


@pytest.mark.parametrize("mode", ("seek", "close", "write-close"))
def test_extent_close_and_combined_errors_fail_closed(monkeypatch, mode):
    parts = [b"x" * 100001]
    owners = _track_buffers(monkeypatch)
    files = _files(monkeypatch, mode)
    with pytest.raises(maps.PreciseStackMapError) as caught:
        join._consume_parts(parts, _ledger(parts))
    assert all(file.inner.closed for file in files)
    assert all(owner.closed for owner in owners)
    _assert_retired(caught.value)
    if mode == "write-close":
        assert "injected write" in str(caught.value.__cause__.__context__)


@pytest.mark.parametrize("label", ("Mach-O consuming join workspace", "Mach-O consuming join backing"))
@pytest.mark.parametrize("delta", (-1, 0, 1))
def test_each_new_admission_boundary_exact_minus_one_zero_plus_one(monkeypatch, label, delta):
    parts = [b"a" * 65539, b"b" * 65541]
    ledger = _ledger(parts)
    original = ledger.check
    calls = []
    def checked(extra=0, *, label="Mach-O input window"):
        previous = ledger.budget.limit_bytes
        if label == target:
            ledger.budget.limit_bytes = ledger.retained + extra + ledger.budget.retained_bytes + delta
            calls.append(label)
        try:
            return original(extra, label=label)
        finally:
            ledger.budget.limit_bytes = previous
    target = label
    monkeypatch.setattr(ledger, "check", checked)
    if delta < 0:
        with pytest.raises(maps.PreciseStackMapError, match=label) as caught:
            join._consume_parts(parts, ledger)
        _assert_retired(caught.value)
    else:
        assert len(join._consume_parts(parts, ledger)) == 131080
    assert calls == [label] and parts == [] and ledger.budget.retained_bytes == 137


def test_storage_cap_and_open_failure_precede_output_allocation(monkeypatch):
    parts = [b"a" * 1024]
    ledger = _ledger(parts, limit=1023)
    def unexpected(**_kwargs):
        raise AssertionError("must not open spool")
    monkeypatch.setattr(join.tempfile, "TemporaryFile", unexpected)
    with pytest.raises(maps.PreciseStackMapError, match="storage budget") as caught:
        join._consume_parts(parts, ledger)
    _assert_retired(caught.value)
    def fail(**_kwargs):
        raise OSError(errno.ENOSPC, "injected open")
    monkeypatch.setattr(join.tempfile, "TemporaryFile", fail)
    parts = [b"a" * 1024]
    with pytest.raises(maps.PreciseStackMapError, match="injected open") as caught:
        join._consume_parts(parts, _ledger(parts))
    _assert_retired(caught.value)


@pytest.mark.parametrize("point", ("admission", "patch"))
def test_callback_enabled_instrumentation_refuses_before_snapshot(monkeypatch, point):
    parts = [b"a" * 100001]
    ledger = _ledger(parts)
    owners = _track_buffers(monkeypatch)
    original = ledger.check
    def enable():
        monkeypatch.setattr(join.sys, "gettrace", lambda: object())
    def check(extra=0, *, label="Mach-O input window"):
        original(extra, label=label)
        if label == "Mach-O consuming join backing":
            enable()
    if point == "admission":
        monkeypatch.setattr(ledger, "check", check)
    with pytest.raises(maps.PreciseStackMapError, match="instrumentation changed") as caught:
        join._consume_parts(parts, ledger, patch=(lambda value: enable()) if point == "patch" else None)
    assert (len(owners) == 0) if point == "admission" else all(owner.closed for owner in owners)
    _assert_retired(caught.value)


def test_failed_patch_frame_child_export_is_released_before_close(monkeypatch):
    owners = _track_buffers(monkeypatch)
    def fail(view):
        child = view[:]
        raise ValueError("injected patch")
    parts = [b"a" * 100001]
    with pytest.raises(ValueError, match="injected patch") as caught:
        join._consume_parts(parts, _ledger(parts), patch=fail)
    assert all(owner.closed for owner in owners)
    _assert_retired(caught.value)


def test_external_child_export_refuses_freeze_without_copy(monkeypatch):
    owners = _track_buffers(monkeypatch)
    escaped = []
    parts = [b"a" * 100001]
    try:
        with pytest.raises(BufferError) as caught:
            join._consume_parts(parts, _ledger(parts), patch=lambda value: escaped.append(value[:]))
        assert len(owners) == 1 and len(owners[0].addresses) == 2
        assert not owners[0].closed
        _assert_retired(caught.value)
    finally:
        for view in escaped:
            view.release()
        for owner in owners:
            owner.close()


@pytest.mark.parametrize("instrument", ("trace", "profile", "monitoring", "unsupported"))
def test_prepare_falls_back_when_host_is_instrumented_or_unsupported(tmp_path, monkeypatch, instrument):
    paths = _write_inputs(tmp_path, count=3, relocations=16)
    expected = _prepared_state(host.prepare_executable_from_paths(paths, compact_budget=_factory_budget()))
    def unexpected(*_args, **_kwargs):
        raise AssertionError("consuming join must remain host-private")
    monkeypatch.setattr(host, "_consume_parts", unexpected)
    if instrument == "unsupported":
        monkeypatch.setattr(host, "_single_buffer_window_supported", lambda: False)
    else:
        fake = SimpleNamespace(gettrace=lambda: None, getprofile=lambda: None,
                               monitoring=SimpleNamespace(get_tool=lambda tool: None))
        if instrument == "monitoring":
            fake.monitoring.get_tool = lambda tool: "test" if tool == 3 else None
        else:
            setattr(fake, "get" + instrument, lambda: object())
        monkeypatch.setattr(join, "sys", fake)
    assert _prepared_state(host.prepare_executable_from_paths(paths, compact_budget=_factory_budget())) == expected


def test_prepare_preserves_single_exact_part_identity(tmp_path, monkeypatch):
    paths = _write_inputs(tmp_path, count=1, relocations=16)
    def unexpected(*_args, **_kwargs):
        raise AssertionError("one exact bytes part already joins without copying")
    monkeypatch.setattr(host, "_consume_parts", unexpected)
    prepared = host.prepare_executable_from_paths(paths, compact_budget=_factory_budget())
    assert prepared.sections()


def _section_targets():
    count = 2
    objects = []
    for index in range(count):
        name = "_main" if index == 0 else "_helper" + str(index)
        sections = [
            obj.Section("__text", "__TEXT", data=b"\xc0\x03\x5f\xd6" * 4,
                        align_log2=2, flags=obj.TEXT_SECTION_FLAGS, symbols=(obj.TextSymbol(name, 0),)),
            _stackmap_section(name),
            obj.Section("__target", "__DATA", flags=obj.ZEROFILL_SECTION_FLAGS,
                        zerofill_size=16, align_log2=3),
        ]
        if index == 0:
            sections.insert(2, obj.Section("__data", "__DATA", data=b"\0" * 16, align_log2=3,
                relocations=(obj.Relocation(0, "", spec.ARM64_RELOC_UNSIGNED, False, 3,
                                           section=("__DATA", "__target"), target_offset=1),
                             obj.Relocation(8, "_main", spec.ARM64_RELOC_PAGEOFF12, False, 2, 4))))
        objects.append(native.NativeObject.from_sections(sections))
    return objects


def test_unresolved_section_target_after_stackmap_resize_matches_eager_and_rejection(tmp_path, monkeypatch):
    paths = _write_pcos(tmp_path, _section_targets())
    budgets = [_factory_budget(), _factory_budget()]
    calls = []
    original = host._consume_parts
    def consume(parts, ledger, *, patch=None):
        calls.append((len(parts), patch is not None))
        return original(parts, ledger, patch=patch)
    monkeypatch.setattr(host, "_consume_parts", consume)
    current = host.prepare_executable_from_paths(paths, compact_budget=budgets[0])
    objects = [native.decode_packed_native_object(__import__("pathlib").Path(path).read_bytes()) for path in paths]
    eager = macho_exec.prepare_executable_object(objects, _consume_inputs=True, _source_view=True,
                                                compact_budget=budgets[1])
    state = _prepared_state(current)
    assert state == _prepared_state(eager)
    assert any(patch for _, patch in calls)
    data = next(index for index, section in enumerate(state[0]) if section["sectname_str"] == "__data")
    target = next(section for section in state[0] if section["sectname_str"] == "__target")
    assert int.from_bytes(state[2][data][:8], "little") == target["addr"] + 1
    assert any(not row[4] for row in native._FINAL_LINK_RELOCATION.iter_unpack(state[3][data]))
    stack = next(section for section in state[0] if section["sectname_str"] == "__pcc_stackmaps")
    assert target["addr"] >= stack["addr"] + stack["size"]
    errors = []
    for prepared, budget in ((eager, budgets[1]), (current, budgets[0])):
        with pytest.raises(macho_link.LinkError, match="must be normalized to a defined symbol") as caught:
            macho_exec.link_prepared_executable(prepared, compact_budget=budget)
        errors.append(str(caught.value))
    assert errors[0] == errors[1] and budgets[0].record_visits == budgets[1].record_visits


def test_defensive_section_target_overflow_retires_join_backing(tmp_path, monkeypatch):
    paths = _write_pcos(tmp_path, _section_targets())
    owners = _track_buffers(monkeypatch)
    consume = host._consume_parts
    invoked = []
    def inject(parts, ledger, *, patch=None):
        if patch is None:
            return consume(parts, ledger)
        def overflow(view):
            # Checked inputs only allow unsigned 8-byte section targets inside
            # uint64 layout. Inject an invalid field to reach the unchanged
            # defensive addition guard without constructing an invalid PCO.
            invoked.append(True)
            view[:8] = b"\xff" * 8
            patch(view)
        return consume(parts, ledger, patch=overflow)
    monkeypatch.setattr(host, "_consume_parts", inject)
    with pytest.raises(macho_link.LinkError, match="section-target value does not fit relocation width") as caught:
        host.prepare_executable_from_paths(paths, compact_budget=_factory_budget())
    assert invoked == [True] and all(owner.closed for owner in owners)
    assert caught.value.__cause__ is not None


def test_real_prepare_and_owned_output_keep_all_records_and_growth(tmp_path):
    paths = _stack_inputs(tmp_path, count=3, records=1024, data_bytes=65536)
    budget = _factory_budget()
    image = output.link_owned_executable(host.prepare_executable_from_paths(paths, compact_budget=budget),
                                         compact_budget=budget)
    assert budget.record_visits == 3 * 3 * 1024
    signature = codesign.parse_signature(image)
    output.check_signature_verification(image, signature, budget)
    with memoryview(image) as whole, whole[:signature.dataoff] as prefix:
        assert codesign.build_signature(prefix, identifier=signature.identifier,
            exec_seg_base=signature.exec_seg_base, exec_seg_limit=signature.exec_seg_limit,
            exec_seg_flags=signature.exec_seg_flags) == image[signature.dataoff:]
    parsed = spec.parse_object(image)
    stack = next(section for section in parsed.sections() if section["sectname_str"] == "__pcc_stackmaps")
    payload = image[stack["offset"]:stack["offset"] + stack["size"]]
    decoded = maps.decode_stack_map(payload, final_image=True)
    assert len(decoded.functions) == 3 and sum(len(fn.records) for fn in decoded.functions) == 3072


def test_production_row_alias_tables_are_empty_before_consumption(tmp_path, monkeypatch):
    paths = _write_inputs(tmp_path, count=3, relocations=16)
    original = host._consume_parts
    rows_seen = []
    def consume(parts, ledger, **kwargs):
        frame = sys._getframe(1)
        local = frame.f_locals
        if parts is local["row_parts"]:
            key = local["key"]
            assert local["normal_parts"][key] == local["rebase_parts"][key] == []
            rows_seen.append(len(parts))
        del frame, local
        return original(parts, ledger, **kwargs)
    monkeypatch.setattr(host, "_consume_parts", consume)
    host.prepare_executable_from_paths(paths, compact_budget=_factory_budget())
    assert rows_seen == [3]


@pytest.mark.parametrize("kind", ("trace", "profile"))
def test_real_active_frame_hook_refuses_and_clears_consumed_parts(kind):
    parts = [b"a" * 100001]
    ledger = _ledger(parts)
    setter = getattr(sys, "set" + kind)
    previous = getattr(sys, "get" + kind)()
    def hook(*_args):
        return hook
    caught = None
    try:
        setter(hook)
        try:
            join._consume_parts(parts, ledger)
        except maps.PreciseStackMapError as error:
            caught = error
    finally:
        setter(previous)
    assert caught is not None and "uninstrumented host" in str(caught)
    _assert_retired(caught)


def test_backing_close_failure_cannot_publish_or_retain_result(monkeypatch):
    original = io.BytesIO
    owners = []
    class FailingClose(original):
        def __init__(self, value):
            super().__init__(value)
            owners.append(self)
        def close(self):
            super().close()
            raise OSError(errno.EIO, "injected backing close")
    monkeypatch.setattr(join.io, "BytesIO", FailingClose)
    parts = [b"a" * 100001]
    with pytest.raises(maps.PreciseStackMapError, match="injected backing close") as caught:
        join._consume_parts(parts, _ledger(parts))
    assert all(owner.closed for owner in owners)
    _assert_retired(caught.value)
