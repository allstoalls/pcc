"""Finite real-host image oracle controls for normal CI only; never run ELF code."""

from dataclasses import replace
import weakref

import pytest

from pcc.backend import elf_x86_64 as elf
from pcc.backend import owned_elf_inputs as inputs
from tests.elf_relocation_progress_preimage import original_linker
from tests.python.test_elf_negative_classification_summary import _negative_object, _stage
from tests.python.test_elf_packed_link_classification import BASE, TARGET, _fixture, _outcome, _packed_object


class Evidence:
    def __init__(self, fault=None):
        self.rows = []
        self.fault = fault
    def begin(self, phase):
        return 0
    def complete(self, phase, started):
        pass
    def elf_classification_skips(self, *values):
        return True
    def elf_relocation_progress(self, *values):
        self.rows.append(values)
        if values[0] == self.fault:
            raise BaseException('authored optional evidence failure')
        return True


def test_relocation_progress_real_staged_packed_and_tuple_images_match(tmp_path):
    obj, text, data = _fixture()
    encoded = elf.emit_relocatable(obj)
    _path, store, staged = _stage(tmp_path, obj)
    packed = elf.parse_relocatable(encoded, compact_relocations=True)
    for owner, options in ((obj, {}), (packed, {}), (staged, {'_input_store': store})):
        expected = _outcome(original_linker(elf), owner, **options)
        evidence = Evidence()
        actual = _outcome(elf._link_static_executable_image, owner,
                          coordinator_timing=evidence, **options)
        assert actual == expected and actual[0] == 'image'
        assert evidence.rows[0] == ('scale', 0, 2, 0, len(text) + len(data))
        assert evidence.rows[1][0] == 'got_complete'
        assert evidence.rows[-1][3] == len(text) + len(data)
    store.verify()


def test_relocation_progress_none_rows_are_counted_and_zero_is_not_divided(tmp_path):
    for count in (0, 1, 17):
        obj = _negative_object(count)
        obj = replace(obj, sections=tuple(replace(section, relocations=tuple(
            replace(row, type=0) for row in section.relocations)) for section in obj.sections))
        _path, store, staged = _stage(tmp_path, obj, str(count) + '.o')
        expected = _outcome(original_linker(elf), staged, _input_store=store)
        evidence = Evidence()
        actual = _outcome(elf._link_static_executable_image, staged,
                          _input_store=store, coordinator_timing=evidence)
        assert actual == expected and actual[0] == 'image'
        assert evidence.rows[0][-1] == count
        snapshots = [row for row in evidence.rows if row[0] == 'sections']
        assert len(snapshots) <= 8
        if count:
            assert snapshots[-1][3:] == (count, count)
        else:
            assert snapshots == []


def test_relocation_progress_sink_failure_preserves_image_and_stops(tmp_path):
    _path, store, staged = _stage(tmp_path, _negative_object(17))
    expected = _outcome(original_linker(elf), staged, _input_store=store)
    for fault in ('scale', 'got_complete', 'sections'):
        evidence = Evidence(fault)
        assert _outcome(elf._link_static_executable_image, staged, _input_store=store,
                        coordinator_timing=evidence) == expected
        assert evidence.rows[-1][0] == fault
        assert len(evidence.rows) == {'scale': 1, 'got_complete': 2, 'sections': 3}[fault]


def test_relocation_progress_first_row_error_matches_frozen_oracle():
    for rows in (((0, 2, 283, 2), (4, 99, 283, 0)),
                 ((0, 99, 311, 0), (4, 2, 283, 2))):
        obj = _packed_object(rows)
        expected = _outcome(original_linker(elf), obj)
        evidence = Evidence()
        actual = _outcome(elf._link_static_executable_image, obj, coordinator_timing=evidence)
        assert actual == expected and actual[0] != 'image'
        assert not any(row[0] == 'sections' for row in evidence.rows)


def test_relocation_progress_staged_identity_failures_match(tmp_path):
    for change in ('append', 'truncate', 'rewrite'):
        path, store, staged = _stage(tmp_path, _negative_object(2), change + '.o')
        data = path.read_bytes()
        path.write_bytes(data + b'x' if change == 'append' else data[:-1] if change == 'truncate'
                         else data[:-1] + bytes((data[-1] ^ 1,)))
        expected = _outcome(original_linker(elf), staged, _input_store=store)
        evidence = Evidence()
        actual = _outcome(elf._link_static_executable_image, staged,
                          _input_store=store, coordinator_timing=evidence)
        assert actual == expected and actual[0] is elf.ElfError
        assert evidence.rows == []


def test_relocation_progress_custom_iterable_keeps_order_and_unknown_scale():
    def run(link):
        events = []
        class Row:
            offset, symbol_index, type, addend = 0, 0, 0, 0
            def __del__(self):
                events.append('row released')
        class Rows:
            def __iter__(self):
                events.append('iter')
                yield Row()
        obj = _negative_object(0)
        object.__setattr__(obj.sections[0], 'relocations', Rows())
        evidence = Evidence()
        result = _outcome(link, obj, coordinator_timing=evidence)
        return result, events, evidence.rows
    before, events, _ = run(original_linker(elf))
    after, actual_events, records = run(elf._link_static_executable_image)
    assert after == before and actual_events == events
    assert records == [('scale_unknown', 0, 0, 0, 0), ('got_complete', 0, 0, 0, 0)]

    def fresh(link, failure):
        events, views, row_refs = [], [], []
        class Row:
            offset, symbol_index, type, addend = 0, 0, 0, 0
            def __del__(self):
                events.append('row released')
        class Cursor:
            def __init__(self, fail):
                self.fail, self.done = fail, False
            def __iter__(self):
                raise AssertionError('second iterator request')
            def __next__(self):
                events.append('next')
                if self.fail:
                    raise ValueError('original iterator failure')
                if self.done:
                    raise StopIteration
                self.done = True
                row = Row()
                row_refs.append(weakref.ref(row))
                return row
        class Rows:
            def __init__(self, fail):
                self.fail = fail
            def __iter__(self):
                events.append('iter')
                return Cursor(self.fail)
            def __del__(self):
                events.append('view released')
        class FreshSection:
            name, type, flags, align, size = '.text', 1, 6, 4, 4
            data = b'\xc0\x03\x5f\xd6'
            reads = 0
            @property
            def relocations(self):
                self.reads += 1
                events.append('get rows')
                rows = Rows(failure and self.reads == 2)
                views.append(weakref.ref(rows))
                return rows
        obj = _negative_object(0)
        object.__setattr__(obj, 'sections', (FreshSection(),))
        evidence = Evidence()
        try:
            value = bytes(link([obj], base_address=BASE, coordinator_timing=evidence))
        except ValueError as error:
            # Inspect while the original traceback still retains its locals.
            value = (type(error), str(error), tuple(ref() is not None for ref in row_refs))
            assert error.__traceback__ is not None
        assert all(ref() is None for ref in views)
        return value, events
    for failure in (False, True):
        assert fresh(elf._link_static_executable_image, failure) == fresh(original_linker(elf), failure)


def test_relocation_progress_disabled_skips_metadata_and_output(monkeypatch):
    obj = _negative_object(1)
    expected = _outcome(original_linker(elf), obj)
    def forbidden(*args):
        raise AssertionError('disabled observation ran')
    monkeypatch.setattr(elf, '_elf_relocation_progress_counts', forbidden)
    monkeypatch.setattr(elf, '_report_relocation_progress', forbidden)
    assert _outcome(elf._link_static_executable_image, obj) == expected


def test_relocation_progress_retains_no_staged_owner_or_payload(tmp_path):
    _path, store, owner = _stage(tmp_path, _negative_object(2))
    assert store.current is None
    reference = weakref.ref(owner)
    section_reference = weakref.ref(owner.sections[0])
    counts = elf._elf_relocation_progress_counts([(0, i, section)
             for i, section in enumerate(owner.sections, 1)])
    assert counts == [2, 0, 0, 0, 0, 1, 1]
    assert all(type(value) is int for value in counts) and store.current is None
    del owner
    assert reference() is None and section_reference() is None


def test_relocation_progress_mutated_later_tuple_uses_explicit_initial_weights():
    def run(link):
        obj = _negative_object(1)
        later = obj.sections[1]
        class Row:
            offset, symbol_index, addend = 0, 0, 0
            reads = 0
            @property
            def type(self):
                self.reads += 1
                # Two generic classification reads; the third starts application.
                if self.reads == 3:
                    object.__setattr__(later, 'relocations', ())
                return 0
        object.__setattr__(obj.sections[0], 'relocations', (Row(),))
        evidence = Evidence()
        outcome = _outcome(link, obj, coordinator_timing=evidence)
        return outcome, evidence.rows
    expected, _ = run(original_linker(elf))
    actual, observations = run(elf._link_static_executable_image)
    assert actual == expected and actual[0] == 'image'
    # Initial scale is two. The completed section has its original weight,
    # although the second section executes zero rows. Sink fields explicitly
    # call this initial resident weight, never an actual applied-row counter.
    assert observations[0] == ('scale', 0, 2, 0, 2)
    assert observations[-1] == ('sections', 2, 2, 2, 2)
