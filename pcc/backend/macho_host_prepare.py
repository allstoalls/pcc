"""Budgeted CPython preparation with one immutable PCO input window.

The public/native object route is unchanged. This private path owns only
file-path inputs and retains validated descriptors, never a packed input or
an encoded-buffer view. Counts admit each allocation phase before it starts;
the process-tree guard remains authoritative for actual RSS.
"""

from dataclasses import dataclass, replace
import hashlib
import io
import os
import struct
import sys
import traceback

from . import macho_link as link, macho_spec as spec, native_object as native
from .macho_host_relocations import _model_bytes, _pack_final_link_rows
from .macho_host_join import _StagedRelocationRows, _consume_parts, _join_instrumentation_absent
from .macho_host_payload import _StagedSectionPayloads
from .macho_host_phase import emit_phase, emit_section_phase
from .macho_host_stackmap import merge_staged_stack_map_payloads, runtime_spool_supported
from .macho_host_stackmap_inputs import _StagedStackMapInputs
from .macho_obj import DataInCodeRegion, Relocation, Section, TextSymbol
from .macho_parallel import ShardedSymbolDefinitions, SymbolDefinition
from .precise_stackmap import (
    RUNTIME_TABLE_VERSION,
    PreciseStackMapError,
    _scan_stack_map_payload,
    precise_stackmap_runtime_format,
    transcode_runtime_table,
    _FUNCTION as _STACK_FUNCTION,
    _HEADER as _STACK_HEADER,
)


_ORIGINAL_STACK_MERGER = merge_staged_stack_map_payloads
_ORIGINAL_RUNTIME_TRANSCODER = transcode_runtime_table
_ORIGINAL_RUNTIME_FORMAT = precise_stackmap_runtime_format


_WORD = struct.calcsize("P")
_INT = sys.getsizeof((1 << 64) - 1)
_BYTES = sys.getsizeof(b"")
_BYTEARRAY = sys.getsizeof(bytearray())
_LIST = sys.getsizeof([])
_TUPLE = sys.getsizeof(())
_STRING = sys.getsizeof("")
_STACK_KEY = ("__DATA", "__pcc_stackmaps")
_ARCHIVE_DESCRIPTOR_FIELDS = (
    "path", "size", "digest", "sections", "symbols", "targeted",
    "section_addrs", "stack_functions", "row_counts", "target_counts", "regions",
)
_ARCHIVE_COMMAND_FIELDS = ("cmd", "cmdsize", "offset", "raw", "body", "sections")
_INSTANCE_VALUES_BYTES = 32 + 31 * _WORD


class _Ledger:
    """Reservations describe live owners; no RSS or compression credit."""

    def __init__(self, budget, retained=0):
        if budget is None or sys.implementation.name != "cpython":
            raise link.LinkError("windowed preparation requires an explicit CPython budget")
        self.budget = budget
        self.retained = retained

    def rounded(self, value):
        return self.budget._rounded(value)

    def check(self, extra=0, *, label="Mach-O input window"):
        self.budget.check_peak(self.retained + extra, label=label)

    def keep(self, amount, *, label="Mach-O retained preparation owners"):
        self.check(amount, label=label)
        self.retained += amount

    def sequence(self, count):
        # Both the old and new pointer arrays at list growth, then a tuple
        # snapshot if one is made.  Fixed-size arrays need less than this.
        return self.rounded(_LIST) + self.rounded(_TUPLE) + 4 * self.rounded(count * _WORD)

    def integers(self, count):
        return count * self.rounded(_INT)

    def mapping(self, count):
        # Existing supported-host budget's insertion-only table allowance
        # includes old/new table overlap, keys and maximum-width integers.
        # A table entry estimate never replaces separately owned string bytes.
        return self.rounded(sys.getsizeof({})) + count * max(
            self.budget.metadata_entry_bytes,
            64 * _WORD + self.rounded(sys.getsizeof((0, 0, 0)))
            + 3 * self.rounded(_INT) + self.rounded(_BYTES),
        )

    def immutable(self, size):
        return self.rounded(_BYTES + size)

    def mutable(self, size):
        return self.rounded(_BYTEARRAY) + self.rounded(size + 1)


@dataclass(frozen=True, slots=True)
class _InputDescriptor:
    path: str
    size: int
    digest: bytes
    sections: tuple
    symbols: tuple
    targeted: frozenset
    section_addrs: tuple
    stack_functions: tuple
    metadata_bytes: int
    row_counts: tuple
    target_counts: tuple
    regions: tuple
    archive_object: object = None


@dataclass(frozen=True, slots=True)
class _StagedSourceSection:
    """Section metadata whose file bytes belong only to the payload store.

    This is not a Section with an empty stand-in payload. Canonical validators
    receive a real Section constructed inside its admitted immutable window.
    Cross-section targets need only this descriptor's flags and virtual size.
    """

    sectname: str
    segname: str
    file_size: int
    align_log2: int
    flags: int
    symbols: tuple
    data_in_code: tuple
    zerofill_size: int
    section_targets: bool

    @property
    def is_zerofill(self):
        return (self.flags & spec.SECTION_TYPE) in (spec.S_ZEROFILL, 0x12)

    @property
    def vm_size(self):
        return self.zerofill_size if self.is_zerofill else self.file_size

    def materialize(self, payload):
        if type(payload) is not bytes or len(payload) != self.file_size:
            raise native.NativeObjectError("staged source payload size changed")
        return Section(
            self.sectname, self.segname, payload, self.align_log2, self.flags,
            self.symbols, (), self.zerofill_size, self.data_in_code,
        )


def _staged_section_metadata_bytes(sections, budget):
    total = budget._rounded(sys.getsizeof(sections))
    for section in sections:
        total += budget._rounded(sys.getsizeof(section))
        for name in _StagedSourceSection.__slots__:
            total += _model_bytes(getattr(section, name), budget)
    return total


def _release_closed_tracebacks(error):
    """A retained exception must not retain a failed decoder's PCO window."""
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        traceback.clear_frames(current.__traceback__)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)


def _single_buffer_window_supported():
    """The exact-size BytesIO ownership transfer is verified on these hosts."""
    if (sys.implementation.name != "cpython"
            or sys.version_info[:2] not in ((3, 13), (3, 14), (3, 15))
            or _WORD != 8
            or hasattr(sys, "gettotalrefcount")
            or os.environ.get("PYTHONMALLOC", "") == "debug"
            or os.environ.get("PYTHONMALLOC", "").endswith("_debug")):
        return False
    import sysconfig
    if sysconfig.get_config_var("Py_GIL_DISABLED") != 0:
        return False
    return not hasattr(sys, "_is_gil_enabled") or sys._is_gil_enabled()


def _read_window(path, ledger, expected_size=None, expected_digest=None):
    """Read exactly the admitted opened file; hash the bytes actually used."""
    buffer = view = remaining = raw = None
    single_buffer = _single_buffer_window_supported()
    try:
        with open(path, "rb", buffering=0) as stream:
            size = os.fstat(stream.fileno()).st_size
            if size < 0 or (expected_size is not None and size != expected_size):
                raise link.LinkError("PCO input size changed: " + path)
            ledger.check(
                ledger.immutable(size) + (0 if single_buffer else ledger.mutable(size)) + 4096,
                label="Mach-O PCO read/freeze window",
            )
            # Exact bytes initialization takes ownership without copying on
            # the supported GIL CPython builds. The temporary bytes expression
            # has no surviving reference, so getbuffer() need not detach it.
            # Keep its size unchanged, release every exported view, then
            # getvalue() returns the same immutable backing allocation. The
            # fixed reservation also covers BytesIO, views and I/O scratch.
            buffer = io.BytesIO(b"\0" * size) if single_buffer else bytearray(size)
            view = buffer.getbuffer() if single_buffer else memoryview(buffer)
            cursor = 0
            while cursor < size:
                with view[cursor:] as remaining:
                    count = stream.readinto(remaining)
                if not count:
                    raise link.LinkError("PCO input became shorter: " + path)
                cursor += count
            if stream.read(1):
                raise link.LinkError("PCO input became longer: " + path)
            view.release()
            view = None
            raw = buffer.getvalue() if single_buffer else bytes(buffer)
            if single_buffer:
                buffer.close()
            buffer = None
        digest = hashlib.sha256(raw).digest()
        if expected_digest is not None and digest != expected_digest:
            raise link.LinkError("PCO input content changed: " + path)
        return raw, digest
    finally:
        if view is not None:
            view.release()
        if single_buffer and buffer is not None:
            buffer.close()
        view = buffer = remaining = raw = None



def _stack_count(raw, start, size):
    # A malformed table can append at most this many complete minimum-sized
    # function records before the canonical scanner fails. Huge claimed
    # counts therefore never become a speculative allocation reservation.
    if size < _STACK_HEADER.size:
        return 0
    fields = _STACK_HEADER.unpack_from(raw, start)
    return min(fields[4], (size - _STACK_HEADER.size) // _STACK_FUNCTION.size)


def _special_scratch(key, flags, size, count, ledger, *, function_count=0, source_rows=False):
    if key == _STACK_KEY:
        triples = ledger.sequence(function_count) + function_count * (
            ledger.rounded(sys.getsizeof((0, 0, 0))) + ledger.integers(3)
        )
        addresses = ledger.sequence(function_count) + ledger.integers(function_count)
        values = count * (ledger.rounded(sys.getsizeof(object.__new__(Relocation))) + ledger.integers(7)) if source_rows else ledger.integers(count)
        return (triples + addresses + ledger.mapping(count) + values
                + ledger.budget._set_bytes(count) + ledger.budget._set_bytes(function_count))
    if flags & spec.SECTION_TYPE == spec.S_MOD_INIT_FUNC_POINTERS:
        return ledger.budget._set_bytes(count) + ledger.budget._set_bytes(size // 8)
    if key == ("__LD", "__compact_unwind"):
        return ledger.budget._set_bytes(count) + ledger.budget._set_bytes(size // 32)
    return 0

def _pco_allocation_bounds(raw, ledger):
    """Structural span census only; the canonical decoder still validates.

    No names, relocation records or per-section census arrays are retained.
    Count products follow checked spans, so corrupt lengths cannot trigger a
    large allocation before admission.
    """
    size = len(raw)
    cursor = 0

    def take(count):
        nonlocal cursor
        if count < 0 or count > size - cursor:
            raise native.NativeObjectError("truncated pcc-native object")
        start = cursor
        cursor += count
        return start

    def name():
        count, = native._U32.unpack_from(raw, take(4))
        if not 0 < count <= native._MAX_NAME_BYTES:
            raise native.NativeObjectError("invalid pcc-native name length")
        start = take(count)
        return start, count, ledger.rounded(_STRING + count) + ledger.immutable(count)

    magic, section_count, symbol_count = native._HEADER.unpack_from(raw, take(native._HEADER.size))
    if magic != native.MAGIC:
        raise native.NativeObjectError("bad pcc-native object magic/version")
    native._validate_count(section_count, "section")
    native._validate_count(symbol_count, "symbol")
    metadata = ledger.sequence(symbol_count) + ledger.sequence(section_count)
    metadata += symbol_count * ledger.rounded(sys.getsizeof(object.__new__(native.PackedNativeSymbol)))
    metadata += ledger.integers(3 * symbol_count + 10 * section_count)
    metadata += section_count * ledger.rounded(sys.getsizeof(object.__new__(native.PackedNativeSection)))
    for _ in range(symbol_count):
        metadata += name()[2]
        take(native._SYMBOL.size)
    largest_scratch = 0
    for _ in range(section_count):
        segment_start, segment_size, segment_bytes = name()
        section_start, section_size, section_bytes = name()
        metadata += segment_bytes + section_bytes
        flags, _align, _zero, data_size, count, regions = native._SECTION.unpack_from(
            raw, take(native._SECTION.size),
        )
        native._validate_count(count, "relocation", allow_zero=True)
        native._validate_count(regions, "data-in-code", allow_zero=True)
        data_start = take(data_size)
        take(count * native._RELOCATION.size)
        take(regions * native._DATA_IN_CODE.size)
        metadata += ledger.sequence(regions) + regions * ledger.rounded(sys.getsizeof((0, 0, 0)))
        metadata += ledger.integers(3 * regions)
        bitmap = (data_size + 7) // 8
        offsets = (
            ledger.mutable(bitmap) if count and bitmap <= max(64, count * 8)
            else ledger.budget._set_bytes(count)
        )
        stackmap = section_size == 15 and raw.startswith(b"__pcc_stackmaps", section_start, section_start + section_size)
        compact_unwind = section_size == 16 and raw.startswith(b"__compact_unwind", section_start, section_start + section_size)
        key = _STACK_KEY if stackmap else ("__LD", "__compact_unwind") if compact_unwind else ("", "")
        function_count = _stack_count(raw, data_start, data_size) if stackmap else 0
        if stackmap:
            metadata += ledger.sequence(function_count) + function_count * (
                ledger.rounded(sys.getsizeof((0, 0))) + ledger.integers(2)
            )
        special = _special_scratch(key, flags, data_size, count, ledger, function_count=function_count)
        largest_scratch = max(largest_scratch, offsets + special)
    if cursor != size:
        raise native.NativeObjectError("trailing bytes after pcc-native object")
    metadata += ledger.budget._set_bytes(symbol_count) + 5 * ledger.sequence(section_count)
    metadata += ledger.integers(3 * section_count) + 1024
    scratch = largest_scratch + 3 * ledger.budget._set_bytes(symbol_count)
    scratch += ledger.sequence(symbol_count) + ledger.sequence(section_count)
    scratch += ledger.budget._set_bytes(section_count) + 8192
    return metadata, scratch


def _read_descriptor(path, ledger):
    raw = packed = payload = scanned = by_offset = None
    try:
        raw, digest = _read_window(path, ledger)
        metadata, scratch = _pco_allocation_bounds(raw, ledger)
        ledger.check(ledger.immutable(len(raw)) + metadata + scratch, label="Mach-O PCO decode owners")
        packed = native.decode_packed_native_object(raw)
        stack_functions, row_counts, target_counts = [], [], []
        for index, section in enumerate(packed.sections):
            rows = targets = 0
            for row in native._packed_relocations_in_storage_order(packed, section):
                rows += 1 + bool(row[5]) + (row[2] == spec.ARM64_RELOC_SUBTRACTOR)
                targets += row[6] != native._NONE_INDEX
            row_counts.append(rows)
            target_counts.append(targets)
            functions = ()
            if (section.segname, section.sectname) == _STACK_KEY:
                payload = packed.section_data(index)
                _count, scanned, _start, _locations = _scan_stack_map_payload(payload)
                by_offset = {}
                for row in native._packed_relocations_in_storage_order(packed, section):
                    by_offset[row[0]] = row[1]
                functions = tuple((function_id, by_offset[start + 8]) for function_id, start, _end in scanned)
                payload = scanned = by_offset = None
            stack_functions.append(functions)
        result = _InputDescriptor(
            path, len(raw), digest, packed.sections, packed.symbols,
            packed.relocation_target_indices, link._native_section_addrs(packed),
            tuple(stack_functions), metadata, tuple(row_counts), tuple(target_counts),
            tuple(section.data_in_code for section in packed.sections),
        )
        ledger.keep(metadata)
        return result
    except BaseException as exc:
        _release_closed_tracebacks(exc)
        raise
    finally:
        raw = packed = payload = scanned = by_offset = None


def _raw_row_dicts(rows):
    for address, symbol, pcrel, length, external, kind in native._FINAL_LINK_RELOCATION.iter_unpack(rows):
        if kind == spec.ARM64_RELOC_SUBTRACTOR and pcrel:
            raise native.NativeObjectError("SUBTRACTOR relocation must be non-pcrel")
        if pcrel not in (0, 1) or external not in (0, 1):
            raise native.NativeObjectError("final-link relocation flags must be boolean")
        yield {
            "r_address": address, "r_symbolnum": symbol, "r_pcrel": pcrel,
            "r_length": length, "r_extern": external, "r_type": kind,
        }


def _initialize_checked_compact_view_inner(view, sections, undefined, weak_undefined, rows, budget, _row_storage=None):
    """Validate each immutable row window through the ordinary source rules."""
    ledger = _Ledger(budget)
    staged = _row_storage is not None
    if type(sections) not in (list, tuple):
        raise native.NativeObjectError("compact source tables must be lists or tuples")
    if staged:
        if type(_row_storage) is not _StagedRelocationRows or rows is not None:
            raise native.NativeObjectError("compact source row storage is not privately owned")
        if _row_storage.count != len(sections):
            raise native.NativeObjectError("compact row section count changed")
    elif type(rows) not in (list, tuple):
        raise native.NativeObjectError("compact source tables must be lists or tuples")
    elif len(sections) != len(rows):
        raise native.NativeObjectError("compact row section count changed")
    for index, section in enumerate(sections):
        if type(section) is not Section or type(section.data) is not bytes:
            raise native.NativeObjectError("compact source payloads must be immutable sections")
        if type(section.align_log2) is not int or not 0 <= section.align_log2 <= 31:
            raise native.NativeObjectError("compact section alignment must be 0..31")
        if section.relocations:
            raise native.NativeObjectError("compact source also contains object relocations")
        if staged:
            count = _row_storage.row_count(index)
        else:
            data = rows[index]
            if type(data) is not bytes or len(data) % native._FINAL_LINK_RELOCATION.size:
                raise native.NativeObjectError("compact relocation rows are not complete immutable records")
            count = len(data) // native._FINAL_LINK_RELOCATION.size
        native._validate_final_link_relocation_count(count)
    retained = _model_bytes(sections, budget)
    if staged:
        # Logical file bytes are not resident. Reserve the complete descriptor
        # owner and the largest single immutable replay before any read.
        retained += _row_storage.metadata_bytes + _row_storage.max_window_bytes
        retained += ledger.sequence(len(sections))  # public table placeholders
    else:
        retained += _model_bytes(rows, budget)
    retained += _model_bytes(undefined, budget) + _model_bytes(weak_undefined, budget)
    symbols_count = sum(len(section.symbols) for section in sections) + len(undefined)
    ledger.keep(retained + 4 * ledger.mapping(symbols_count) + ledger.mapping(len(sections)))
    scratch = 8192
    for index, section in enumerate(sections):
        count = _row_storage.row_count(index) if staged else len(rows[index]) // 12
        bitmap = (len(section.data) + 7) // 8
        offsets = ledger.mutable(bitmap) if count and bitmap <= max(64, count * 8) else ledger.budget._set_bytes(count)
        special = 0
        if ((section.segname, section.sectname) in (_STACK_KEY, ("__LD", "__compact_unwind"))
                or section.flags & spec.SECTION_TYPE == spec.S_MOD_INIT_FUNC_POINTERS):
            special = _special_scratch(
                (section.segname, section.sectname), section.flags,
                len(section.data), count, ledger, source_rows=True,
                function_count=_stack_count(section.data, 0, len(section.data)),
            )
        scratch = max(scratch, offsets + special + 8192)
    ledger.check(scratch, label="Mach-O compact row validation scratch")
    source_sections = tuple(sections)
    undefined_names = tuple(undefined)
    weak_names = set(weak_undefined)
    if not weak_names.issubset(undefined_names):
        raise native.NativeObjectError("weak references must name undefined symbols")
    addrs, cursor = [], 0
    for section in source_sections:
        cursor = native._align_up(cursor, section.align_log2)
        addrs.append(cursor)
        cursor += section.vm_size
    payloads = [section.data for section in source_sections]
    row_counts = [_row_storage.row_count(index) for index in range(len(sections))] if staged else [len(data) // 12 for data in rows]
    section_rows = native._final_section_rows(source_sections, payloads, addrs, row_counts)
    symbol_rows = native._final_symbol_rows(source_sections, undefined_names, weak_names, addrs)

    def logical(index):
        return link._iter_folded_relocation_rows(
            _raw_row_dicts(rows[index]), section_rows, symbol_rows, {},
        )

    if staged:
        def logical(index):
            # Opening inside the generator matters: ordinary section shape
            # validation need not iterate its relocation source at all.
            with _row_storage.window(index, check=lambda: budget.check_peak(
                    scratch, label="Mach-O compact row validation replay")) as data:
                raw = folded = None
                try:
                    raw = _raw_row_dicts(data)
                    folded = link._iter_folded_relocation_rows(raw, section_rows, symbol_rows, {})
                    yield from folded
                finally:
                    if folded is not None:
                        folded.close()
                    if raw is not None:
                        raw.close()
                    folded = raw = data = None

    logical_counts = []
    previous = budget.retained_bytes
    if staged:
        budget.retained_bytes = previous + ledger.retained
    try:
        for index in range(len(source_sections)):
            count = 0
            for relocation in logical(index):
                count += 1
            logical_counts.append(count)
        if not staged:
            budget.retained_bytes = previous + ledger.retained
        try:
            native._validate_source_sections(
                source_sections, undefined_names, relocation_source=logical,
                relocation_counts=logical_counts, compact_budget=budget,
            )
        finally:
            if not staged:
                budget.retained_bytes = previous
        for index in range(len(source_sections)):
            for relocation in logical(index):
                if relocation.section is not None:
                    target_index = next(i for i, sec in enumerate(source_sections) if (sec.segname, sec.sectname) == relocation.section)
                    width = 1 << relocation.length
                    start = relocation.offset
                    stored = int.from_bytes(payloads[index][start:start + width], "little")
                    normalized = link._normalized_section_target_value(
                        stored, None, addrs[target_index], source_sections[target_index].vm_size,
                        width, target_index + 1,
                    )
                    if normalized != stored:
                        raise native.NativeObjectError("compact section target is not object-relative")
        view._source_sections = ()
        view._payloads = payloads
        view._section_index = {(section.segname, section.sectname): index for index, section in enumerate(source_sections, 1)}
        view._sections = section_rows
        view._symbols = symbol_rows
        view._symbol_index = {symbol["name"]: index for index, symbol in enumerate(symbol_rows)}
        view._raw_relocations = [b""] * len(sections) if staged else list(rows)
        if staged:
            view._host_row_copy_bound = ledger.retained
            view._host_row_store = _row_storage
    except BaseException as exc:
        _release_closed_tracebacks(exc)
        payloads.clear()
        raise
    finally:
        budget.retained_bytes = previous


def close_staged_source_rows(view, *, _error=None):
    """Retire both private stores; ordinary views have neither owner."""
    failure = _error
    try:
        for attribute, table in (("_host_payload_store", "_payloads"),
                                 ("_host_row_store", "_raw_relocations")):
            storage = getattr(view, attribute, None)
            if storage is not None:
                try:
                    storage.close(_error=failure)
                except BaseException as exc:
                    if failure is None:
                        failure = exc
                finally:
                    # Placeholders cannot become an apparently valid ordinary
                    # source after their private backing file has retired.
                    getattr(view, table).clear()
                    setattr(view, attribute, None)
        if _error is None and failure is not None:
            raise failure
    finally:
        failure = storage = _error = None


def copy_staged_relocation_rows(view, index):
    """Public accessor copies belong to the caller, never a section cache.

    Their cumulative backing charge deliberately cannot be retired by this
    private view: it cannot prove when a caller releases an escaped copy.
    """
    storage = view._host_row_store
    def check():
        payloads = getattr(view, "_host_payload_store", None)
        storage.budget.check_peak(
            view._host_row_copy_bound + storage.public_copy_bytes + storage.backing_bytes(index)
            + (payloads.public_copy_bytes if payloads is not None else 0),
            label="Mach-O public staged row copy",
        )
    try:
        return storage.copy(index, check=check)
    except BaseException as exc:
        close_staged_source_rows(view, _error=exc)
        raise
    finally:
        view = storage = check = None


def _initialize_checked_compact_view(view, sections, undefined, weak_undefined, rows, budget,
                                     _row_storage=None, _payload_storage=None):
    try:
        if _payload_storage is not None:
            if rows is not None:
                raise native.NativeObjectError("staged payloads require privately staged rows")
            return _initialize_checked_payload_view(
                view, sections, undefined, weak_undefined, budget,
                _row_storage, _payload_storage,
            )
        return _initialize_checked_compact_view_inner(
            view, sections, undefined, weak_undefined, rows, budget, _row_storage,
        )
    except BaseException as exc:
        _release_closed_tracebacks(exc)
        if _row_storage is not None:
            _row_storage.close(_error=exc)
            view._host_row_store = None
        if _payload_storage is not None:
            _payload_storage.close(_error=exc)
            view._host_payload_store = None
        for name in ("_payloads", "_raw_relocations", "_sections", "_symbols"):
            if hasattr(view, name):
                setattr(view, name, [])
        raise
    finally:
        sections = undefined = weak_undefined = rows = _row_storage = _payload_storage = None


def _logical_staged_rows(data, section_rows, symbol_rows, symbol_names=None):
    raw = folded = None
    try:
        raw = _raw_row_dicts(data)
        folded = link._iter_folded_relocation_rows(
            raw, section_rows, symbol_rows, {}, symbol_names=symbol_names,
        )
        yield from folded
    finally:
        if folded is not None:
            folded.close()
        if raw is not None:
            raw.close()
        data = raw = folded = None
        section_rows = symbol_rows = symbol_names = None


def _semantic_rows_with_deferred_target_check(
    data, section_rows, symbol_rows, payload, sections, addrs, section_index,
    first_target_error, symbol_names=None,
):
    """Validate targets after each yielded row, without changing error priority.

    The caller's canonical relocation validator must finish this section before
    a target error is raised. Yield first so it checks each row and claims its
    offset before we inspect the corresponding payload field. Keep only the
    first target error's message, never a relocation table or traceback.
    """
    folded = _logical_staged_rows(data, section_rows, symbol_rows, symbol_names)
    relocation = None
    try:
        for relocation in folded:
            yield relocation
            if first_target_error[0] is not None or relocation.section is None:
                continue
            target_index = section_index[relocation.section] - 1
            width = 1 << relocation.length
            start = relocation.offset
            stored = int.from_bytes(payload[start:start + width], "little")
            try:
                normalized = link._normalized_section_target_value(
                    stored, None, addrs[target_index], sections[target_index].vm_size,
                    width, target_index + 1,
                )
            except native.NativeObjectError as exc:
                first_target_error[0] = str(exc)
            else:
                if normalized != stored:
                    first_target_error[0] = "compact section target is not object-relative"
    finally:
        folded.close()
        folded = relocation = data = payload = sections = addrs = section_index = None
        section_rows = symbol_rows = symbol_names = first_target_error = None


def _initialize_checked_payload_view(view, sections, undefined, weak_undefined, budget,
                                     row_storage, payload_storage):
    """Publish metadata, validating one exact payload and row replay at a time.

    The construction allowance is unchanged. Once its temporary name sets have
    actually retired, recount the surviving metadata before opening any payload
    window. The canonical section/relocation validators still see all bytes.
    """
    from .macho_host_output import _ValidationBudget, _require_uninstrumented_owner, _source_bytes
    from .macho_host_payload import _StagedSectionPayloads as payload_type
    from .macho_obj import MachOEmitError

    _require_uninstrumented_owner()
    ledger = _Ledger(budget)
    if (type(sections) not in (list, tuple)
            or type(row_storage) is not _StagedRelocationRows
            or type(payload_storage) is not payload_type
            or row_storage.budget is not budget or payload_storage.budget is not budget
            or row_storage.count != len(sections) or payload_storage.count != len(sections)
            or row_storage.public_copy_bytes or payload_storage.public_copy_bytes
            or payload_storage.consumed):
        raise native.NativeObjectError("staged source storage is not privately owned")
    for index, metadata in enumerate(sections):
        if type(metadata) is not _StagedSourceSection:
            raise native.NativeObjectError("staged source requires file-owned section metadata")
        if metadata.file_size != payload_storage.size(index):
            raise native.NativeObjectError("staged source payload size changed")
        if type(metadata.align_log2) is not int or not 0 <= metadata.align_log2 <= 31:
            raise native.NativeObjectError("compact section alignment must be 0..31")
        native._validate_final_link_relocation_count(row_storage.row_count(index))
    retained = (_staged_section_metadata_bytes(sections, budget)
                + _model_bytes(undefined, budget) + _model_bytes(weak_undefined, budget)
                + row_storage.metadata_bytes + payload_storage.metadata_bytes
                + row_storage.max_window_bytes + 2 * ledger.sequence(len(sections)))
    symbols_count = sum(len(section.symbols) for section in sections) + len(undefined)
    ledger.keep(retained + 4 * ledger.mapping(symbols_count) + ledger.mapping(len(sections)))
    previous = budget.retained_bytes
    payload = raw_rows = real_section = adapter = records = patch = None
    section_rows = symbol_rows = symbol_names = first_target_error = None
    seen = seen_zerofill = weak_names = None
    try:
        # This phase owns no payload. Preserve canonical names, duplicate and
        # zerofill-order checks before constructing the final reusable index.
        if not sections:
            raise MachOEmitError("at least one section is required")
        if len({(s.segname, s.sectname) for s in sections}) != len(sections):
            raise MachOEmitError("duplicate section names")
        seen, seen_zerofill = set(), set()
        for metadata in sections:
            native._validate_source_section_names(metadata, seen, seen_zerofill)
        undefined_names = tuple(undefined)
        native._validate_source_undefined_names(undefined_names, seen)
        weak_names = set(weak_undefined)
        if not weak_names.issubset(undefined_names):
            raise native.NativeObjectError("weak references must name undefined symbols")
        addrs, cursor = [], 0
        for metadata in sections:
            cursor = native._align_up(cursor, metadata.align_log2)
            addrs.append(cursor)
            cursor += metadata.vm_size
        sizes = tuple(section.file_size for section in sections)
        counts = tuple(row_storage.row_count(index) for index in range(len(sections)))
        section_rows = native._final_section_rows(
            sections, None, addrs, counts, payload_sizes=sizes,
        )
        symbol_rows = native._final_symbol_rows(sections, undefined_names, weak_names, addrs)
        symbol_index = {symbol["name"]: index for index, symbol in enumerate(symbol_rows)}
        section_by_name = {(section.segname, section.sectname): section for section in sections}
        section_index = {key: index for index, key in enumerate(section_by_name, 1)}
        order = tuple(section_by_name)
        addresses = {key: addrs[index] for index, key in enumerate(order)}
        targets = tuple(section.section_targets for section in sections)
        layout = (targets, order, addresses)
        # No validation frame retains seen/weak_names while the largest payload
        # is read. The numeric index now serves canonical membership validation
        # and is the exact table published in the final view.
        seen = seen_zerofill = weak_names = None
        ledger.retained = retained + sum(_model_bytes(owner, budget) for owner in (
            undefined_names, addrs, sizes, counts, section_rows, symbol_rows,
            symbol_index, section_index, layout,
        )) + ledger.mapping(len(sections)) + 8192
        ledger.check(label="Mach-O staged source surviving metadata")
        # Reuse canonical names for every replay. Only the tuple's pointer
        # storage is new: the strings already belong to symbol_rows. Admit
        # construction before allocation, then retain the exact tuple charge.
        ledger.check(
            ledger.sequence(len(symbol_rows)), label="Mach-O staged symbol-name cache",
        )
        symbol_names = tuple(symbol["name"] for symbol in symbol_rows)
        ledger.retained += ledger.rounded(sys.getsizeof(symbol_names))
        for index, metadata in enumerate(sections):
            backing = payload_storage.backing_bytes(index)
            with row_storage.window(index, check=lambda: ledger.check(
                    label="Mach-O compact row validation replay")) as raw_rows:
                try:
                    records = _logical_staged_rows(
                        raw_rows, section_rows, symbol_rows, symbol_names,
                    )
                    count = sum(1 for _ in records)
                    records.close()
                    records = None
                    if targets[index]:
                        def patch(data):
                            _patch_section_targets(data, raw_rows, addresses, order)
                    else:
                        patch = None
                    with payload_storage.window(index, check=lambda: ledger.check(
                            backing, label="Mach-O source payload validation replay"), patch=patch) as payload:
                        try:
                            bitmap = (len(payload) + 7) // 8
                            offsets = (ledger.mutable(bitmap) if count and bitmap <= max(64, count * 8)
                                       else budget._set_bytes(count))
                            scratch = offsets + _special_scratch(
                                (metadata.segname, metadata.sectname), metadata.flags,
                                len(payload), count, ledger, source_rows=True,
                                function_count=(_stack_count(payload, 0, len(payload))
                                                if (metadata.segname, metadata.sectname) == _STACK_KEY else 0),
                            ) + 8192
                            ledger.check(backing + scratch, label="Mach-O compact row validation scratch")
                            budget.retained_bytes = previous + ledger.retained + backing
                            real_section = metadata.materialize(payload)
                            adapter = _ValidationBudget(budget, payload)
                            records = _logical_staged_rows(
                                raw_rows, section_rows, symbol_rows, symbol_names,
                            )
                            native._validate_section(
                                real_section, relocations=records, relocation_count=count,
                                compact_budget=adapter,
                            )
                            records.close()
                            first_target_error = [None]
                            records = _semantic_rows_with_deferred_target_check(
                                raw_rows, section_rows, symbol_rows, payload, sections,
                                addrs, section_index, first_target_error, symbol_names,
                            )
                            native._validate_source_section_relocations(
                                real_section, symbol_index, section_by_name, records, count,
                            )
                            records.close()
                            records = None
                            if first_target_error[0] is not None:
                                raise native.NativeObjectError(first_target_error[0])
                        except BaseException as exc:
                            _release_closed_tracebacks(exc)
                            raise
                        finally:
                            budget.retained_bytes = previous
                            if records is not None:
                                records.close()
                            if adapter is not None:
                                adapter.payload = None
                            records = real_section = adapter = payload = patch = None
                finally:
                    if records is not None:
                        records.close()
                    records = raw_rows = patch = None
        view._source_sections = ()
        view._payloads = [b""] * len(sections)
        view._raw_relocations = [b""] * len(sections)
        view._sections, view._symbols = section_rows, symbol_rows
        view._section_index, view._symbol_index = section_index, symbol_index
        view._host_payload_store, view._host_row_store = payload_storage, row_storage
        view._host_payload_layout = layout
        # Public copies start after the constructor's metadata temporaries have
        # retired; this is the surviving source graph, plus its one row replay.
        view._host_row_copy_bound = view._host_payload_copy_bound = _source_bytes(view, budget)
        _require_uninstrumented_owner()
    except MachOEmitError as exc:
        _release_closed_tracebacks(exc)
        raise native.NativeObjectError(str(exc)) from exc
    finally:
        budget.retained_bytes = previous
        payload = raw_rows = real_section = adapter = records = patch = None
        section_rows = symbol_rows = symbol_names = first_target_error = None
        seen = seen_zerofill = weak_names = None


def _patch_staged_payload(view, index, data, check):
    targets, order, addresses = view._host_payload_layout
    if targets[index]:
        with view._host_row_store.window(index, check=check) as rows:
            try:
                _patch_section_targets(data, rows, addresses, order)
            finally:
                rows = None


def copy_staged_section_data(view, index):
    """Publish an independent, cumulatively charged normalized exact copy."""
    storage = view._host_payload_store
    rows = view._host_row_store
    def check():
        storage.budget.check_peak(
            view._host_payload_copy_bound + storage.public_copy_bytes
            + rows.public_copy_bytes + storage.backing_bytes(index),
            label="Mach-O public staged payload copy",
        )
    try:
        return storage.copy(index, check=check,
                            patch=lambda data: _patch_staged_payload(view, index, data, check))
    except BaseException as exc:
        close_staged_source_rows(view, _error=exc)
        raise
    finally:
        view = storage = rows = check = None


def _archive_bounds(raw, ledger):
    """Count archive/parser owners without constructing a parser graph.

    The ordinary archive reader remains the format/selection authority. This
    census also charges repeated references to one string-table span once per
    symbol, since the parser decodes a fresh name for each occurrence.
    """
    from . import macho_archive as archive

    if type(raw) is not bytes or not raw.startswith(archive.ARMAG):
        raise link.LinkError("not an ar archive (bad magic)")
    cursor, total = len(archive.ARMAG), 8192
    while cursor < len(raw):
        if len(raw) - cursor < archive._HEADER_SIZE:
            raise link.LinkError("truncated archive member header")
        header = raw[cursor:cursor + archive._HEADER_SIZE]
        try:
            size = int(header[48:58].decode().strip())
            name_field = header[:16].rstrip()
            name_size = int(name_field[3:]) if name_field.startswith(b"#1/") else 0
        except (ValueError, UnicodeError) as exc:
            raise link.LinkError("invalid archive member size") from exc
        start = cursor + archive._HEADER_SIZE
        if size < 0 or name_size < 0 or name_size > size or size > len(raw) - start:
            raise link.LinkError("invalid archive member span")
        payload_start, payload_size = start + name_size, size - name_size
        total += ledger.immutable(payload_size) + 2 * ledger.immutable(name_size)
        total += ledger.rounded(_STRING + 4 * (name_size or len(name_field))) + 1024
        ledger.check(total, label="Mach-O archive member owners")
        cursor = start + size + (size % 2)
        if (raw.startswith(b"__.SYMDEF", start, start + name_size) if name_size else name_field.startswith(b"__.SYMDEF")):
            continue
        if payload_size < spec.MACH_HEADER_64.size:
            raise link.LinkError("archive member is shorter than a Mach-O header")
        head = spec.MACH_HEADER_64.unpack(raw, payload_start)
        command_start = payload_start + spec.MACH_HEADER_64.size
        command_end = command_start + head["sizeofcmds"]
        payload_end = payload_start + payload_size
        if command_end > payload_end or head["ncmds"] > head["sizeofcmds"] // spec.LOAD_COMMAND.size:
            raise link.LinkError("archive load-command span is invalid")
        total += ledger.mapping(12) + ledger.sequence(head["ncmds"])
        for _ in range(head["ncmds"]):
            if command_start + spec.LOAD_COMMAND.size > command_end:
                raise link.LinkError("archive load-command span is invalid")
            cmd, cmdsize = struct.unpack_from("<II", raw, command_start)
            if cmdsize < spec.LOAD_COMMAND.size or cmdsize > command_end - command_start:
                raise link.LinkError("archive load-command size is invalid")
            total += ledger.immutable(cmdsize) + ledger.mapping(30) + 1024
            if cmd == spec.LC_SEGMENT_64:
                if cmdsize < spec.SEGMENT_COMMAND_64.size:
                    raise link.LinkError("short archive segment command")
                segment = spec.SEGMENT_COMMAND_64.unpack(raw, command_start)
                count = segment["nsects"]
                if count > (cmdsize - spec.SEGMENT_COMMAND_64.size) // spec.SECTION_64.size:
                    raise link.LinkError("archive section table exceeds its command")
                total += ledger.sequence(count) + ledger.mapping(24 * count)
                offset = command_start + spec.SEGMENT_COMMAND_64.size
                for _ in range(count):
                    section = spec.SECTION_64.unpack(raw, offset)
                    offset += spec.SECTION_64.size
                    relocations = section["nreloc"]
                    if section["reloff"] > payload_size or relocations > (payload_size - section["reloff"]) // 8:
                        raise link.LinkError("archive relocation table exceeds member")
                    # Validation/selection can retain parser dictionaries;
                    # relocation projection below is streaming, never a list.
                    total += ledger.sequence(3) + 2048
                    if (section["segname"].rstrip(b"\0") == b"__DATA"
                            and section["sectname"].rstrip(b"\0") == b"__pcc_stackmaps"):
                        data_offset, data_size = section["offset"], section["size"]
                        if data_offset > payload_size or data_size > payload_size - data_offset:
                            raise link.LinkError("archive stack-map payload exceeds member")
                        functions = _stack_count(raw, payload_start + data_offset, data_size)
                        # Selected members retain this detached function/index
                        # table after their scan; functions may outnumber all
                        # symbols, including repeated references to one name.
                        total += ledger.sequence(functions) + functions * (
                            ledger.rounded(sys.getsizeof((0, 0))) + ledger.integers(2)
                        )
                        ledger.check(total, label="Mach-O archive stack-map descriptors")
            elif cmd == spec.LC_SYMTAB:
                if cmdsize < spec.SYMTAB_COMMAND.size:
                    raise link.LinkError("short archive symbol command")
                table = spec.SYMTAB_COMMAND.unpack(raw, command_start)
                nsyms, symoff = table["nsyms"], table["symoff"]
                stroff, strsize = table["stroff"], table["strsize"]
                if (symoff > payload_size or nsyms > (payload_size - symoff) // spec.NLIST_64.size
                        or stroff > payload_size or strsize > payload_size - stroff):
                    raise link.LinkError("archive symbol table exceeds member")
                total += ledger.sequence(nsyms) + ledger.mapping(8 * nsyms) + ledger.immutable(strsize)
                ledger.check(total, label="Mach-O archive symbol owners")
                for index in range(nsyms):
                    string_index, = struct.unpack_from("<I", raw, payload_start + symoff + index * spec.NLIST_64.size)
                    if string_index >= strsize:
                        raise link.LinkError("archive symbol name exceeds string table")
                    name_start = payload_start + stroff + string_index
                    name_end = raw.find(b"\0", name_start, payload_start + stroff + strsize)
                    if name_end < 0:
                        raise link.LinkError("archive symbol name is unterminated")
                    total += 4 * ledger.rounded(_STRING + 4 * (name_end - name_start))
                    ledger.check(total, label="Mach-O archive symbol names")
            elif cmd == spec.LC_BUILD_VERSION:
                if cmdsize < spec.BUILD_VERSION_COMMAND.size:
                    raise link.LinkError("short archive build-version command")
                count = spec.BUILD_VERSION_COMMAND.unpack(raw, command_start)["ntools"]
                if count > (cmdsize - spec.BUILD_VERSION_COMMAND.size) // spec.BUILD_TOOL_VERSION.size:
                    raise link.LinkError("archive build tools exceed command")
                total += ledger.mapping(4 * count)
            elif cmd == spec.LC_DATA_IN_CODE:
                if cmdsize >= spec.LINKEDIT_DATA_COMMAND.size:
                    table = spec.LINKEDIT_DATA_COMMAND.unpack(raw, command_start)
                    if table["dataoff"] > payload_size or table["datasize"] > payload_size - table["dataoff"]:
                        raise link.LinkError("archive data-in-code exceeds member")
                    total += ledger.mapping(8 * (table["datasize"] // spec.DATA_IN_CODE_ENTRY.size))
            command_start += cmdsize
        ledger.check(total, label="Mach-O archive parser owners")
    return total


def _archive_raw_rows(obj, section):
    for index in range(section["nreloc"]):
        address, info = spec._RELOCATION_READER.unpack_from(
            obj.data, section["reloff"] + index * spec.RELOCATION_INFO.size,
        )
        row = spec.unpack_relocation(info, address)
        if row["r_type"] == spec.ARM64_RELOC_SUBTRACTOR and row["r_pcrel"]:
            raise link.LinkError("SUBTRACTOR relocation must be non-pcrel")
        yield row


def _archive_descriptor_bytes(descriptor, ledger):
    """Conservatively census the selected, detached archive member model.

    No identity graph or payload copy is created. Aliased section dictionaries
    are deliberately counted from both owners. The ordinary dataclasses use
    the same supported-host embedded/detached instance-values allowance as
    the host relocation model; inspecting __dict__ could materialize it.
    """
    total = ledger.rounded(sys.getsizeof(descriptor)) + ledger.integers(1)
    for name in _ARCHIVE_DESCRIPTOR_FIELDS:
        total += _model_bytes(getattr(descriptor, name), ledger.budget)
    obj = descriptor.archive_object
    instance_extra = ledger.rounded(sys.getsizeof({})) + 2 * ledger.rounded(_INSTANCE_VALUES_BYTES)
    total += ledger.rounded(sys.getsizeof(obj)) + instance_extra
    for name in ("header", "data"):
        total += _model_bytes(name, ledger.budget)
        total += _model_bytes(getattr(obj, name), ledger.budget)
    total += _model_bytes("commands", ledger.budget)
    total += ledger.rounded(sys.getsizeof(obj.commands))
    for command in obj.commands:
        total += ledger.rounded(sys.getsizeof(command)) + instance_extra
        for name in _ARCHIVE_COMMAND_FIELDS:
            total += _model_bytes(name, ledger.budget)
            total += _model_bytes(getattr(command, name), ledger.budget)
    return total


def _archive_descriptor(raw, index, ledger):
    from .macho_exec import _validate_input_load_commands

    obj = link._coerce_link_object(raw, index)
    _validate_input_load_commands([obj])
    if obj.header["filetype"] != spec.MH_OBJECT or obj.header["cputype"] != spec.CPU_TYPE_ARM64 or obj.header["cpusubtype"] != spec.CPU_SUBTYPE_ARM64_ALL:
        raise link.LinkError("archive input is not an arm64-all MH_OBJECT")
    sections, symbols = tuple(obj.sections()), tuple(obj.symbols())
    for symbol in symbols:
        if symbol["n_type"] & spec.N_TYPE == spec.N_UNDF and (
            not symbol["n_type"] & spec.N_EXT or symbol["n_sect"] or symbol["n_value"]
        ):
            raise link.LinkError("archive uses an unsupported local/common undefined shape")
    regions = [[] for _ in sections]
    for region in obj.data_in_code():
        start, end = region["offset"], region["offset"] + region["length"]
        owners = [i for i, section in enumerate(sections) if section["addr"] <= start and end <= section["addr"] + section["size"]]
        if len(owners) != 1:
            raise link.LinkError("data-in-code range does not belong to exactly one section")
        owner = owners[0]
        regions[owner].append((start - sections[owner]["addr"], region["length"], region["kind"]))
    targeted, counts, targets, functions = set(), [], [], []
    for section in sections:
        key = link._section_key(section)
        if type(section["align"]) is not int or not 0 <= section["align"] <= 31:
            raise link.LinkError("archive section alignment exceeds 31")
        if not link._is_zerofill(section) and (section["offset"] > len(raw) or section["size"] > len(raw) - section["offset"]):
            raise link.LinkError("archive section payload exceeds member")
        rows = target_count = 0
        for row in _archive_raw_rows(obj, section):
            if row["r_extern"] and row["r_type"] != spec.ARM64_RELOC_ADDEND:
                link._relocation_symbol_name(symbols, row["r_symbolnum"], {}, context="archive relocation")
                targeted.add(row["r_symbolnum"])
        for relocation in link._iter_folded_relocation_rows(_archive_raw_rows(obj, section), sections, symbols, {}):
            rows += 1 + bool(relocation.addend) + (relocation.type == spec.ARM64_RELOC_SUBTRACTOR)
            target_count += relocation.section is not None
        counts.append(rows)
        targets.append(target_count)
        current = ()
        if key == _STACK_KEY:
            size, start = section["size"], section["offset"]
            ledger.check(_special_scratch(key, section["flags"], size, section["nreloc"], ledger,
                                         function_count=_stack_count(raw, start, size), source_rows=True)
                         + ledger.immutable(size), label="Mach-O archive stack-map scan")
            payload = raw[start:start + size]
            _count, scanned, _start, _locations = _scan_stack_map_payload(payload)
            by_offset = {r.offset: r for r in link._iter_folded_relocation_rows(_archive_raw_rows(obj, section), sections, symbols, {})}
            if len(by_offset) != len(scanned):
                raise link.LinkError("input stack-map function addresses and relocations disagree")
            current = []
            symbol_indices = {symbol["name"]: i for i, symbol in enumerate(symbols)}
            for function_id, start, _end in scanned:
                relocation = by_offset.get(start + 8)
                if (relocation is None or relocation.type != spec.ARM64_RELOC_UNSIGNED or relocation.pcrel
                        or relocation.length != 3 or relocation.section is not None or relocation.minuend is not None
                        or relocation.addend or payload[start + 8:start + 16] != b"\0" * 8):
                    raise link.LinkError("stack-map address relocation is not plain UNSIGNED64")
                current.append((function_id, symbol_indices[relocation.symbol]))
            if any(symbol["n_sect"] == sections.index(section) + 1 for symbol in symbols):
                raise link.LinkError("stack-map section must not define symbols")
            current = tuple(current)
        functions.append(current)
    result = _InputDescriptor("", len(raw), b"", sections, symbols, frozenset(targeted), (),
                              tuple(functions), 0, tuple(counts), tuple(targets),
                              tuple(tuple(rows) for rows in regions), obj)
    # The parser reservation remains live during the bounded census and the
    # old/new descriptor overlap. It is replaced only after archive selection
    # and all associated transient owners have reached their last use.
    ledger.check(8192, label="Mach-O archive descriptor census scratch")
    retained = _archive_descriptor_bytes(result, ledger)
    return replace(result, metadata_bytes=retained)


def _input_relocations(descriptor, packed, section_index, local_rename, base):
    section = descriptor.sections[section_index]
    if descriptor.archive_object is not None:
        yield from link._iter_folded_relocation_rows(
            _archive_raw_rows(descriptor.archive_object, section), descriptor.sections,
            descriptor.symbols, local_rename, offset_bias=base,
        )
        return
    for fields in packed.relocation_fields(section_index):
        offset, symbol, kind, pcrel, length, addend, target, minuend, target_offset = fields
        section_key = None if target == native._NONE_INDEX else link._section_key(descriptor.sections[target - 1])
        symbol_name = "" if symbol == native._NONE_INDEX else link._relocation_symbol_name(descriptor.symbols, symbol, local_rename, context="packed native relocation")
        minuend_name = None if minuend == native._NONE_INDEX else link._relocation_symbol_name(descriptor.symbols, minuend, local_rename, context="packed native SUBTRACTOR minuend relocation")
        yield Relocation(offset + base, symbol_name, kind, bool(pcrel), length, addend,
                         section_key, minuend_name, None if target_offset == -1 else target_offset)


def _canonical_symbol_indices(merged, order, unresolved, ledger):
    """Number names without constructing a second symbol-row object graph.

    Match native._final_symbol_rows: stable per-section offset order, locals
    before externals, then sorted undefineds. Sorting must not mutate the
    original symbol order, which selects the first alias for section targets.
    """
    count = sum(len(merged[key].symbols) for key in order) + len(unresolved)
    retained = ledger.mapping(count)
    # One sorted pointer array, key array and Timsort merge scratch. The
    # source names/offsets are retained by the already charged merged model.
    ledger.check(retained + ledger.sequence(count), label="Mach-O canonical symbol index")
    result = {}
    for external in (False, True):
        for key in order:
            # Each binding class retains its stable offset order; sorting
            # the other class only to discard it repeats all of its keys.
            selected = (symbol for symbol in merged[key].symbols
                        if symbol.external == external)
            for symbol in sorted(selected, key=lambda symbol: symbol.offset):
                result[symbol.name] = len(result)
    for name in sorted(unresolved):
        result[name] = len(result)
    # The completed dict borrows every key from merged/unresolved, both of
    # which stay charged and live for its entire lifetime. Charge its table
    # and new index integers; resize/sort scratch has actually retired.
    ledger.retained += ledger.rounded(sys.getsizeof(result)) + ledger.integers(len(result))
    return result


def _canonical_stack_symbol_indices(merged, order, unresolved, stack_symbols, ledger):
    """Keep canonical stack indices, using a sparse table when its bound wins."""
    count = sum(len(merged[key].symbols) for key in order) + len(unresolved)
    # Many function IDs may alias fewer canonical names. In that case the
    # original full table preserves its smaller pre-allocation admission.
    if len(stack_symbols) >= count:
        return _canonical_symbol_indices(merged, order, unresolved, ledger)
    # Multiple function IDs can target one name. The input count bounds the
    # sparse table without allocating a separate wanted-name set. Sorting
    # still visits the full symbol universe and keeps its full scratch charge.
    ledger.check(ledger.mapping(len(stack_symbols)) + ledger.sequence(count),
                 label="Mach-O canonical stack symbol index")
    result = dict.fromkeys(stack_symbols.values(), -1)
    index = 0
    for external in (False, True):
        for key in order:
            # Each binding class retains its stable offset order; sorting
            # the other class only to discard it repeats all of its keys.
            selected = (symbol for symbol in merged[key].symbols
                        if symbol.external == external)
            for symbol in sorted(selected, key=lambda symbol: symbol.offset):
                if symbol.name in result:
                    result[symbol.name] = index
                index += 1
    for name in sorted(unresolved):
        if name in result:
            result[name] = index
        index += 1
    # Keep an absent target absent, preserving the existing packing lookup's
    # KeyError rather than passing a sentinel as a relocation index. Revisit
    # the still-live source values instead of materializing a second table.
    for name in stack_symbols.values():
        if result.get(name) == -1:
            del result[name]
    # Keys borrow stack_symbols values; that mapping and all merged source
    # owners remain live and charged. Only this dict and its indices are new.
    ledger.retained += ledger.rounded(sys.getsizeof(result)) + ledger.integers(len(result))
    return result


def _section_target_symbol_index(merged, ledger):
    """Keep the first alias at each address, borrowing charged source owners."""
    reserved = ledger.mapping(sum(len(target.symbols) for target in merged.values()))
    ledger.keep(reserved, label="Mach-O section-target symbol index")
    result = {}
    for key, target in merged.items():
        for symbol in target.symbols:
            result.setdefault((key, symbol.offset), symbol.name)
    # The completed table owns only its storage and one two-element key
    # tuple per distinct address. Its section keys, offsets and names are
    # borrowed from order/merged, which stay charged and live until it retires.
    ledger.retained += (ledger.rounded(sys.getsizeof(result))
                        + len(result) * ledger.rounded(sys.getsizeof((None, None)))
                        - reserved)
    return result


def _patch_section_targets(data, rows, addresses, order):
    for offset, section_number, _pcrel, length, external, kind in native._FINAL_LINK_RELOCATION.iter_unpack(rows):
        if external or kind == spec.ARM64_RELOC_ADDEND:
            continue
        width = 1 << length
        # A subview must not survive an overflow exception and keep the
        # consuming helper's mutable backing exported.
        with memoryview(data)[offset:offset + width] as field:
            value = int.from_bytes(field, "little") + addresses[order[section_number - 1]]
        if value >= 1 << (8 * width):
            raise native.NativeObjectError("section-target value does not fit relocation width")
        data[offset:offset + width] = value.to_bytes(width, "little")


def _merge_paths(paths, archives, archive_paths, ledger, _phase_callback=None, *, _staged_rows=False):
    """Return only detached final sections/rows; retire every input owner."""
    from .macho_archive import ArchiveError, read_archive, select_members
    from .macho_exec import _accumulate_external_symbol_state

    descriptors, archive_buffers, members = [], [], []
    archive_reservation = archive_survivors = 0
    merged, order, plans = {}, [], []
    normal_parts, rebase_parts, stack_payloads = {}, {}, None
    raw = packed = payload = descriptor = section = target = part = None
    out_sections, output_rows = [], []
    row_storage = row_targets = payload_storage = failure = None
    try:
        emit_phase(_phase_callback, "input_scan_begin")
        ledger.keep(ledger.sequence(len(paths)) + ledger.sequence(len(archives) + len(archive_paths)))
        for path in paths:
            descriptors.append(_read_descriptor(path, ledger))
        if not descriptors:
            raise link.LinkError("nothing to link")

        symbol_count = sum(len(item.symbols) for item in descriptors)
        archive_symbol_bytes = 3 * ledger.budget._set_bytes(symbol_count)
        ledger.keep(archive_symbol_bytes, label="Mach-O archive symbol state")
        already_defined, undefined = set(), set()
        for descriptor in descriptors:
            _accumulate_external_symbol_state(descriptor.symbols, already_defined, undefined)
        for archive_index in range(len(archives) + len(archive_paths)):
            if archive_index < len(archives):
                raw = archives[archive_index]
                if type(raw) is not bytes:
                    raise link.LinkError("archive inputs must be immutable bytes")
            else:
                raw, _digest = _read_window(archive_paths[archive_index - len(archives)], ledger)
            bounds = _archive_bounds(raw, ledger)
            reservation = bounds + ledger.immutable(len(raw))
            ledger.keep(reservation, label="Mach-O archive parser owners")
            archive_reservation += reservation
            archive_buffers.append(raw)
            try:
                members = read_archive(raw)
            except (ArchiveError, spec.MachOFormatError) as exc:
                raise link.LinkError(f"archive {archive_index} is outside the proven subset: {exc}") from exc
            pulled, _pending = select_members(members, undefined - already_defined, already_defined=already_defined)
            for member in pulled:
                descriptor = _archive_descriptor(member, len(descriptors), ledger)
                descriptors.append(descriptor)
                archive_survivors += descriptor.metadata_bytes
                _accumulate_external_symbol_state(descriptor.symbols, already_defined, undefined)
            raw = None
        members = pulled = member = _pending = None
        already_defined = undefined = None
        # read_archive slices every member out of its larger archive bytes.
        # Selected descriptors own those independent bytes and parsed models;
        # no second-pass input or index borrows the whole archive. All archive
        # symbol-set growth remains covered until selection of every archive
        # is complete, including backreferences into preceding archives.
        if archive_reservation:
            # Archive selection can grow this pointer table beyond len(paths).
            # Keep its construction allowance until all descriptors retire.
            archive_survivors += ledger.sequence(len(descriptors))
            ledger.check(max(0, archive_survivors - archive_reservation) + 8192,
                         label="Mach-O archive retained ownership transition")
            archive_buffers.clear()
            raw = None
        ledger.retained += archive_survivors - archive_reservation - archive_symbol_bytes
        descriptor_bytes = ledger.retained
        emit_phase(_phase_callback, "input_scan_end")
        emit_phase(_phase_callback, "placement_begin")

        symbol_count = sum(len(item.symbols) for item in descriptors)
        section_count = sum(len(item.sections) for item in descriptors)
        function_count = sum(len(functions) for item in descriptors for functions in item.stack_functions)
        region_count = sum(len(regions) for item in descriptors for regions in item.regions)
        # During placement only provenance, local renames and duplicate
        # definitions need symbol-count mappings. Canonical rows are never
        # constructed here; final-index/address lookups start after the
        # provenance and duplicate/reference owners have actually retired.
        symbol_metadata = 3 * ledger.mapping(symbol_count) + 5 * ledger.budget._set_bytes(symbol_count)
        symbol_metadata += ledger.sequence(symbol_count) + symbol_count * 256
        # Every occurrence can get a distinct collision suffix. Do not use
        # a distinct-name census to charge repeated source name ownership.
        for item in descriptors:
            for symbol in item.symbols:
                symbol_metadata += ledger.rounded(_STRING + len(link._symbol_name(symbol)) + 64)
        plan_metadata = 6 * ledger.sequence(section_count) + ledger.integers(6 * section_count)
        plan_metadata += ledger.mapping(section_count) + 4096 * len(descriptors)
        plan_metadata += ledger.mapping(function_count) + ledger.sequence(function_count)
        plan_metadata += region_count * 1024 + 16384
        ledger.keep(symbol_metadata + plan_metadata, label="Mach-O detached merge metadata")
        definitions = ShardedSymbolDefinitions()
        for index, descriptor in enumerate(descriptors):
            for symbol_index, symbol in enumerate(descriptor.symbols):
                if link._symbol_type(symbol) & spec.N_TYPE == spec.N_SECT and link._symbol_type(symbol) & spec.N_EXT:
                    definitions.add(link._symbol_name(symbol), SymbolDefinition(index, symbol_index))
        definitions.freeze()
        local_renames = link._local_symbol_renames([item.symbols for item in descriptors])
        defined, referenced = {}, set()
        weak_references, strong_references = set(), set()
        stack_symbols = {}
        text_index, text_key = 0, link._TEXT_KEY
        for index, descriptor in enumerate(descriptors):
            bases, input_keys, placements = {}, {}, []
            for section_index, section in enumerate(descriptor.sections, 1):
                key = link._section_key(section)
                if key == _STACK_KEY:
                    if link._section_flags(section) != spec.S_REGULAR or link._section_align(section) < 3:
                        raise link.LinkError("input stack-map section has invalid flags/alignment")
                    placements.append(None)
                    for function_id, symbol_index in descriptor.stack_functions[section_index - 1]:
                        if function_id in stack_symbols:
                            raise link.LinkError("duplicate stable function id in stack-map inputs")
                        name = link._relocation_symbol_name(descriptor.symbols, symbol_index, local_renames[index], context="stack-map function")
                        stack_symbols[function_id] = name
                        referenced.add(name)
                    continue
                merged_key, target, base, padding, text_index, text_key = link._place_merged_section(
                    merged, order, key, link._section_flags(section), link._section_align(section),
                    link._section_size(section), text_index, text_key,
                )
                if not link._is_zerofill(section):
                    target.data_size = base + link._section_size(section)
                bases[section_index], input_keys[key] = base, merged_key
                placements.append((merged_key, base, padding))
                target.data_in_code.extend(DataInCodeRegion(base + region[0], region[1], region[2])
                                           for region in descriptor.regions[section_index - 1])
            plans.append(tuple(placements))
            link._merge_input_symbols(
                index, descriptor.symbols, descriptor.targeted, descriptor.sections,
                descriptor.section_addrs, local_renames[index], bases, input_keys,
                definitions, merged, defined, referenced, weak_references, strong_references,
            )
            for symbol_index in descriptor.targeted:
                referenced.add(link._relocation_symbol_name(descriptor.symbols, symbol_index, local_renames[index], context="input relocation"))
        if any(any(functions is not None and link._section_key(section) == _STACK_KEY
                   for section, functions in zip(item.sections, item.stack_functions)) for item in descriptors):
            merged[_STACK_KEY] = link._MergedSection(_STACK_KEY[0], _STACK_KEY[1], spec.S_REGULAR, align_log2=3)
            order.append(_STACK_KEY)
        unresolved = sorted(referenced - set(defined))
        weak_unresolved = tuple(sorted(set(unresolved) & (weak_references - strong_references)))
        appearance = {key: index for index, key in enumerate(order)}
        rank = {"__TEXT": 0, "__DATA_CONST": 1, "__DATA": 2}
        order.sort(key=lambda key: (rank.get(key[0], 3), bool(merged[key].zerofill_size and not merged[key].data_size), appearance[key]))
        definitions = defined = referenced = weak_references = strong_references = None
        appearance = bases = input_keys = placements = descriptor = section = symbol = None
        # Replace the now-retired placement reservation with a conservative
        # census of the surviving fixed models. Keep every input descriptor
        # and archive-parser charge, including the caller's prior retention.
        ledger.retained = descriptor_bytes
        for owner in (order, plans, local_renames, stack_symbols, unresolved,
                      weak_unresolved, normal_parts, rebase_parts):
            ledger.retained += _model_bytes(owner, ledger.budget)
        ledger.retained += ledger.mapping(len(merged))
        for target in merged.values():
            ledger.retained += 1024 + _model_bytes(target.symbols, ledger.budget)
            ledger.retained += _model_bytes(target.data_parts, ledger.budget)
            ledger.retained += _model_bytes(target.data_in_code, ledger.budget)
        # Future appends can own two payload parts (padding and data), an
        # ordinary row part, a rebased row part and a stack-map snapshot per
        # input section. Keep old/new list-growth arrays admitted as well as
        # one current input's placement lookup during the second pass.
        ledger.keep(5 * ledger.sequence(section_count)
                    + ledger.mapping(max(len(item.sections) for item in descriptors))
                    + 16384, label="Mach-O detached part-table growth")
        ledger.check(label="Mach-O surviving merge metadata")
        # Section indices, addresses and the two part tables are keyed by
        # output sections, never by input symbols or relocation records.
        ledger.keep(4 * ledger.mapping(len(order)), label="Mach-O detached section indexes")
        section_indices = {key: index for index, key in enumerate(order, 1)}
        addresses, cursor = {}, 0
        for key in order:
            target = merged[key]
            cursor = native._align_up(cursor, target.align_log2)
            addresses[key] = cursor
            cursor += target.zerofill_size or target.data_size
            if cursor > native._UINT64_MAX:
                raise link.LinkError("merged virtual layout exceeds uint64")
            normal_parts[key], rebase_parts[key] = [], []
        # Canonical symbol indices depend on section/symbol ordering only.
        # Their addresses are recomputed after the merged stack-map size is
        # known; zerofill and later segments can follow that table.
        symbol_indices = _canonical_symbol_indices(merged, order, unresolved, ledger)
        if any(any(item.target_counts) for item in descriptors):
            symbol_at = _section_target_symbol_index(merged, ledger)
        else:
            symbol_at = {}

        stack_count = sum(1 for item in descriptors for section in item.sections
                          if link._section_key(section) == _STACK_KEY)
        if stack_count:
            stack_size = sum(link._section_size(section) for item in descriptors
                             for section in item.sections
                             if link._section_key(section) == _STACK_KEY)
            stack_payloads = _StagedStackMapInputs(
                stack_count, stack_size, compact_budget=ledger.budget, admit=ledger.keep,
            )
        item = symbol = None
        # enumerate caches its last result tuple and would keep a retired
        # descriptor alive across the debit at the bottom of this loop.
        emit_phase(_phase_callback, "placement_end")
        emit_phase(_phase_callback, "input_detach_begin")
        for index in range(len(descriptors)):
            descriptor = descriptors[index]
            if descriptor.archive_object is None:
                raw, _digest = _read_window(descriptor.path, ledger, descriptor.size, descriptor.digest)
                raw_bytes = ledger.immutable(len(raw))
                ledger.keep(raw_bytes)
                packed = native.PackedNativeObject(raw, descriptor.sections, descriptor.symbols)
            else:
                packed, raw_bytes = None, 0
                raw = descriptor.archive_object.data
            input_plan = plans[index]
            input_keys = {link._section_key(section): plan for section, plan in zip(descriptor.sections, input_plan) if plan is not None}
            debug_count = sum(1 for candidate in descriptor.sections
                              if link._section_key(candidate)[0] == "__DWARF")
            debug_scratch = 2 * ledger.mapping(debug_count) if debug_count else 0
            if debug_scratch:
                ledger.keep(debug_scratch, label="Mach-O DWARF source section indexes")
            debug_bases, debug_sizes = {}, {}
            debug_abbreviations = None
            for debug_section in descriptor.sections:
                debug_key = link._section_key(debug_section)
                if debug_key[0] != "__DWARF":
                    continue
                debug_bases[debug_key[1]] = input_keys[debug_key][1]
                debug_sizes[debug_key[1]] = link._section_size(debug_section)
                if debug_key[1] == "__debug_abbrev":
                    debug_start = debug_section.data_offset if packed is not None else debug_section["offset"]
                    debug_abbreviations = memoryview(raw)[debug_start:debug_start + debug_sizes[debug_key[1]]]
            for section_index, section in enumerate(descriptor.sections):
                key = link._section_key(section)
                size = link._section_size(section) if not link._is_zerofill(section) else 0
                if key == _STACK_KEY:
                    start = section.data_offset if packed is not None else section["offset"]
                    stack_payloads.append(raw, start, size)
                    continue
                merged_key, base, padding = input_plan[section_index]
                target = merged[merged_key]
                if padding:
                    ledger.keep(ledger.immutable(padding), label="Mach-O detached alignment padding")
                    target.data_parts.append(b"\0" * padding)
                targets = descriptor.target_counts[section_index]
                start = section.data_offset if packed is not None else section["offset"]
                patch_debug = key == ("__DWARF", "__debug_info")
                mutable_payload = bool(targets) or patch_debug
                payload_bytes = ledger.mutable(size) if mutable_payload else ledger.immutable(size)
                ledger.keep(payload_bytes, label="Mach-O detached payload")
                payload = bytearray(memoryview(raw)[start:start + size]) if mutable_payload else bytes(memoryview(raw)[start:start + size])
                if patch_debug:
                    from .macho_dwarf import DwarfLinkError, patch_dwarf_info
                    if debug_abbreviations is None:
                        raise link.LinkError("DWARF info has no input abbreviation section")
                    ledger.check(len(debug_abbreviations) * 1024 + 8192, label="Mach-O DWARF abbreviation scratch")
                    try:
                        patch_dwarf_info(payload, debug_abbreviations, debug_bases, debug_sizes)
                    except DwarfLinkError as error:
                        raise link.LinkError("DWARF merge rejected: " + str(error)) from error
                count = descriptor.row_counts[section_index]
                sort_scratch = 0
                if packed is not None and section.relocation_order == 0:
                    # sorted(range(...), key=offset) owns input integers,
                    # key integers, pointer arrays and Timsort merge scratch.
                    sort_scratch = ledger.sequence(section.relocation_count) + ledger.integers(2 * section.relocation_count)
                ledger.check(sort_scratch + 8192, label="Mach-O relocation ordering scratch")

                def ordinary():
                    for relocation in _input_relocations(descriptor, packed, section_index, local_renames[index], base):
                        if relocation.section is None:
                            yield relocation

                if count - targets:
                    part = _pack_final_link_rows(ordinary(), count - targets, symbol_indices, section_indices,
                                                ledger.budget, ledger.retained + sort_scratch)
                    normal_parts[merged_key].append(part)
                    ledger.keep(ledger.immutable(len(part)))
                    part = None

                def rebased():
                    for relocation in _input_relocations(descriptor, packed, section_index, local_renames[index], base):
                        if relocation.section is None:
                            continue
                        target_plan = input_keys.get(relocation.section)
                        if target_plan is None:
                            raise link.LinkError("section-target relocation names a section absent from its input")
                        target_key, new_base, _padding = target_plan
                        source_target_index = next(i for i, sec in enumerate(descriptor.sections) if link._section_key(sec) == relocation.section)
                        target_section = descriptor.sections[source_target_index]
                        old_addr = link._section_addr(target_section, source_target_index + 1, descriptor.section_addrs)
                        local_offset = relocation.offset - base
                        width = 1 << relocation.length
                        stored = int.from_bytes(payload[local_offset:local_offset + width], "little")
                        if packed is not None:
                            stored = link._normalized_section_target_value(stored, relocation.target_offset, old_addr,
                                                                          link._section_size(target_section), width,
                                                                          source_target_index + 1)
                        value = (stored - old_addr + new_base) & link._U64_MASK
                        owner = None if key[0] == "__DWARF" else symbol_at.get((target_key, value))
                        if owner is not None:
                            payload[local_offset:local_offset + width] = b"\0" * width
                            yield Relocation(relocation.offset, owner, relocation.type, relocation.pcrel, relocation.length)
                        else:
                            if not 0 <= value < (merged[target_key].zerofill_size or merged[target_key].data_size):
                                raise native.NativeObjectError("rebased section target is outside merged section")
                            # Keep the merged-section-relative offset until
                            # the merged stack-map payload fixes final layout.
                            payload[local_offset:local_offset + width] = value.to_bytes(width, "little")
                            yield Relocation(relocation.offset, "", relocation.type, relocation.pcrel,
                                             relocation.length, section=target_key)

                if targets:
                    part = _pack_final_link_rows(rebased(), targets, symbol_indices, section_indices,
                                                ledger.budget, ledger.retained + sort_scratch)
                    rebase_parts[merged_key].append(part)
                    ledger.keep(ledger.immutable(len(part)))
                    part = None
                if mutable_payload:
                    ledger.check(ledger.immutable(size), label="Mach-O detached payload freeze")
                    frozen = bytes(payload)
                    ledger.retained += ledger.immutable(size) - payload_bytes
                    payload = frozen
                    frozen = None
                target.data_parts.append(payload)
                payload = None
            debug_abbreviations = debug_section = None
            debug_bases = debug_sizes = None
            ledger.retained -= debug_scratch
            raw = packed = section = input_keys = None
            retired_bytes = raw_bytes + descriptor.metadata_bytes
            descriptors[index] = None
            descriptor = None
            ledger.retained -= retired_bytes
            local_renames[index].clear()

        # All input bytes, parser graphs and placement/provenance scratch
        # have reached their last use. Recount the exact detached owners,
        # preserving the original caller charge and prior budget retention.
        archive_buffers.clear()
        item = member = None
        descriptors.clear()
        plans.clear()
        definitions = local_renames = defined = referenced = weak_references = strong_references = None
        symbol_indices = symbol_at = None
        input_plan = input_keys = section = target = bases = placements = None
        appearance = _pending = symbol = None
        emit_phase(_phase_callback, "input_detach_end")
        emit_phase(_phase_callback, "merge_index_begin")
        ledger.retained = ledger.caller_bytes
        for owner in (normal_parts, rebase_parts, stack_symbols,
                      unresolved, weak_unresolved, order, section_indices, addresses):
            ledger.retained += _model_bytes(owner, ledger.budget)
        ledger.retained += ledger.mapping(len(merged))
        if stack_payloads is not None:
            ledger.retained += stack_payloads.metadata_bytes
        for target in merged.values():
            ledger.retained += 1024 + _model_bytes(target.data_parts, ledger.budget)
            ledger.retained += _model_bytes(target.symbols, ledger.budget)
            ledger.retained += _model_bytes(target.data_in_code, ledger.budget)
        # Prefer the canonical entries consumed by stack rows; dense targets
        # retain the full index and its original allocation bound.
        symbol_indices = {}
        if stack_payloads:
            symbol_indices = _canonical_stack_symbol_indices(
                merged, order, unresolved, stack_symbols, ledger,
            )
        emit_phase(_phase_callback, "merge_index_end")
        if stack_payloads:
            stack_payloads.seal()
            previous = ledger.budget.retained_bytes
            # Detached inputs are file-owned. The merger charges its one
            # materialized window; all other live preparation owners remain.
            ledger.budget.retained_bytes = previous + ledger.retained
            try:
                emit_phase(_phase_callback, "stackmap_merge_begin")
                runtime_spool = (
                    precise_stackmap_runtime_format is _ORIGINAL_RUNTIME_FORMAT
                    and str(os.environ.get("PCC_PRECISE_STACKMAP_FORMAT", "") or "").strip() in ("", "3")
                    and merge_staged_stack_map_payloads is _ORIGINAL_STACK_MERGER
                    and transcode_runtime_table is _ORIGINAL_RUNTIME_TRANSCODER
                    and runtime_spool_supported(ledger.budget)
                    and _single_buffer_window_supported()
                    and _join_instrumentation_absent()
                )
                if runtime_spool:
                    stack_payload, stack_offsets = merge_staged_stack_map_payloads(
                        stack_payloads, compact_budget=ledger.budget,
                        single_buffer=True, runtime_table=True,
                    )
                else:
                    stack_payload, stack_offsets = merge_staged_stack_map_payloads(
                        stack_payloads, compact_budget=ledger.budget,
                        single_buffer=_single_buffer_window_supported(),
                    )
            finally:
                ledger.budget.retained_bytes = previous
            # This route always publishes an executable: no later merge or
            # semantic layout reads the table, so it takes the runtime format.
            if not runtime_spool and precise_stackmap_runtime_format() == RUNTIME_TABLE_VERSION:
                ledger.budget.check_peak(
                    ledger.retained + 4 * len(stack_payload),
                    entries=len(stack_offsets),
                    label="Mach-O runtime stack-map table",
                )
                stack_payload, stack_offsets = transcode_runtime_table(stack_payload, stack_offsets)
            # The merger closes input storage before output materialization.
            ledger.retained -= stack_payloads.metadata_bytes
            stack_payloads = None
            ledger.keep(ledger.immutable(len(stack_payload)) + _model_bytes(stack_offsets, ledger.budget))
            merged[_STACK_KEY].data_parts.append(stack_payload)
            merged[_STACK_KEY].data_size = len(stack_payload)
            stack_payload = None
            rows = (Relocation(offset, stack_symbols[function_id], spec.ARM64_RELOC_UNSIGNED, False, 3)
                    for function_id, offset in stack_offsets)
            part = _pack_final_link_rows(rows, len(stack_offsets), symbol_indices, section_indices,
                                        ledger.budget, ledger.retained)
            normal_parts[_STACK_KEY].append(part)
            ledger.keep(ledger.immutable(len(part)))
            part = rows = None
            emit_phase(_phase_callback, "stackmap_merge_end")
        # The final stack-map rows no longer borrow these construction indexes.
        # Drop only our references: names/integers may still belong to output
        # models. Keep the existing ledger charge conservatively, without a
        # second model walk or claiming that malloc has returned physical pages.
        stack_offsets = stack_symbols = symbol_indices = section_indices = None
        emit_phase(_phase_callback, "final_layout_begin")
        cursor = 0
        for key in order:
            target = merged[key]
            cursor = native._align_up(cursor, target.align_log2)
            addresses[key] = cursor
            cursor += target.zerofill_size or target.data_size
            if cursor > native._UINT64_MAX:
                raise link.LinkError("merged virtual layout exceeds uint64")
        ledger.keep(2 * ledger.sequence(len(order)), label="Mach-O final output tables")
        consuming_join = _single_buffer_window_supported() and _join_instrumentation_absent()
        emit_phase(_phase_callback, "final_layout_end")
        emit_phase(_phase_callback, "section_joins_begin")
        if _staged_rows:
            row_storage = _StagedRelocationRows(len(order), ledger.budget, ledger.keep)
            ledger.keep(ledger.sequence(len(order)), label="Mach-O staged row target flags")
            row_targets = [False] * len(order)
            # Retire the complete row-part graph before any payload join.
            # No section needs a joined relocation backing merely to persist
            # it. A section-target patch later borrows one verified replay.
            for join_index in range(len(order)):
                key = order[join_index]
                ledger.check(ledger.sequence(len(normal_parts[key]) + len(rebase_parts[key])),
                             label="Mach-O compact row join pointers")
                row_parts = normal_parts[key] + rebase_parts[key]
                normal_parts[key].clear()
                rebase_parts[key].clear()
                row_size = sum(map(len, row_parts))
                row_part_count = len(row_parts)
                emit_section_phase(_phase_callback, "section_rows_begin", join_index, key,
                                   row_size, row_part_count, True)
                retired, row_targets[join_index] = row_storage.write_owned_parts(row_parts)
                ledger.retained -= retired
                row_parts = None
                emit_section_phase(_phase_callback, "section_rows_end", join_index, key,
                                   row_size, row_part_count, True)
            from .macho_host_output import _require_uninstrumented_owner
            _require_uninstrumented_owner()
            row_storage.seal()
            payload_storage = _StagedSectionPayloads(len(order), ledger.budget, ledger.keep)
            # Retire the aggregate chunk graph, including single-part sections,
            # before creating any final section backing. Per-section consuming
            # joins otherwise overlap completed backings with all later chunks
            # and with allocator-resident pages from earlier chunk retirement.
            for join_index in range(len(order)):
                key = order[join_index]
                target = merged[key]
                size = sum(map(len, target.data_parts))
                payload_part_count = len(target.data_parts)
                emit_section_phase(_phase_callback, "section_payload_spool_begin", join_index, key,
                                   size, payload_part_count, True)
                ledger.retained -= payload_storage.write_owned_parts(target.data_parts)
                emit_section_phase(_phase_callback, "section_payload_spool_end", join_index, key,
                                   size, payload_part_count, True)
            payload_storage.seal()
        for join_index in range(len(order)):
            key = order[join_index]
            target = merged[key]
            ledger.keep(ledger.sequence(len(target.symbols)) + ledger.sequence(len(target.data_in_code)) + 256,
                        label="Mach-O final section metadata")
            if row_storage is not None:
                has_section_targets = row_targets[join_index]
            else:
                ledger.check(ledger.sequence(len(normal_parts[key]) + len(rebase_parts[key])), label="Mach-O compact row join pointers")
                row_parts = normal_parts[key] + rebase_parts[key]
                size = sum(len(part) for part in row_parts)
                row_part_count = len(row_parts)
                consume_rows = consuming_join and row_part_count > 1
                emit_section_phase(_phase_callback, "section_rows_begin", join_index, key,
                                   size, row_part_count, consume_rows)
                if consume_rows:
                    # Clear both original tables before debiting any chunks.
                    normal_parts[key].clear()
                    rebase_parts[key].clear()
                    rows = _consume_parts(row_parts, ledger)
                else:
                    ledger.check(ledger.immutable(size) + ledger.sequence(len(row_parts)), label="Mach-O compact relocation join")
                    rows = b"".join(row_parts)
                    ledger.retained += ledger.immutable(size) - sum(ledger.immutable(len(part)) for part in row_parts)
                    row_parts.clear()
                    normal_parts[key].clear()
                    rebase_parts[key].clear()
                emit_section_phase(_phase_callback, "section_rows_end", join_index, key,
                                   size, row_part_count, consume_rows)
                has_section_targets = any(not row[4] and row[5] != spec.ARM64_RELOC_ADDEND
                                          for row in native._FINAL_LINK_RELOCATION.iter_unpack(rows))
            if payload_storage is not None:
                _offset, size, payload_part_count, _digest = payload_storage.descriptor(join_index)
            else:
                size = sum(len(part) for part in target.data_parts)
                payload_part_count = len(target.data_parts)
            def patch_targets(data):
                if row_storage is None:
                    _patch_section_targets(data, rows, addresses, order)
                else:
                    with row_storage.window(join_index, check=lambda: ledger.check(
                            row_storage.backing_bytes(join_index),
                            label="Mach-O section-target row replay")) as staged:
                        try:
                            _patch_section_targets(data, staged, addresses, order)
                        finally:
                            staged = None

            consume_payload = payload_storage is not None or (
                consuming_join and (payload_part_count > 1 or has_section_targets))
            emit_section_phase(_phase_callback, "section_payload_begin", join_index, key,
                               size, payload_part_count, consume_payload)
            if payload_storage is not None:
                # Keep every backing file-owned through source-view metadata
                # construction. Validation and final output replay the original
                # bytes, applying section-target normalization exactly once to
                # each fresh window, never to an already normalized backing.
                _require_uninstrumented_owner()
                payload = None
            elif consume_payload:
                payload = _consume_parts(target.data_parts, ledger,
                                         patch=patch_targets if has_section_targets else None)
            else:
                # Zero/one exact immutable part already has a no-copy join;
                # unsupported/instrumented hosts retain the original two-owner
                # mutable/freeze admission and behavior.
                joined_size = ledger.mutable(size) if has_section_targets else ledger.immutable(size)
                ledger.check(joined_size, label="Mach-O detached payload join")
                payload = bytearray().join(target.data_parts) if has_section_targets else b"".join(target.data_parts)
                ledger.retained += joined_size - sum(ledger.immutable(len(part)) for part in target.data_parts)
                target.data_parts.clear()
                if has_section_targets:
                    patch_targets(payload)
                    ledger.check(ledger.immutable(size), label="Mach-O final section-target freeze")
                    frozen = bytes(payload)
                    payload = frozen
                    frozen = None
                    ledger.retained += ledger.immutable(size) - joined_size
            if payload_storage is not None:
                out_sections.append(_StagedSourceSection(
                    target.sectname, target.segname, size, target.align_log2, target.flags,
                    tuple(sorted(target.symbols, key=lambda symbol: symbol.offset)),
                    tuple(sorted(target.data_in_code, key=lambda region: region.offset)),
                    target.zerofill_size if not size else 0, has_section_targets,
                ))
            else:
                out_sections.append(Section(target.sectname, target.segname, data=payload,
                                            align_log2=target.align_log2, flags=target.flags,
                                            symbols=tuple(sorted(target.symbols, key=lambda symbol: symbol.offset)),
                                            data_in_code=tuple(sorted(target.data_in_code, key=lambda region: region.offset)),
                                            zerofill_size=target.zerofill_size if not payload else 0))
            if row_storage is None:
                output_rows.append(rows)
            payload = rows = patch_targets = None
            emit_section_phase(_phase_callback, "section_payload_end", join_index, key,
                               size, payload_part_count, consume_payload)
        if payload_storage is not None:
            _require_uninstrumented_owner()
        emit_phase(_phase_callback, "section_joins_end")
        if row_storage is None:
            return out_sections, output_rows, unresolved, weak_unresolved, None
        # The boundary callback may have changed host eligibility after the
        # final write. Do not hand a live file to an instrumented return path.
        from .macho_host_output import _require_uninstrumented_owner
        _require_uninstrumented_owner()
        result = (out_sections, row_storage, unresolved, weak_unresolved, payload_storage)
        row_storage = payload_storage = None  # transfer only after result exists
        return result
    except BaseException as exc:
        failure = exc
        _release_closed_tracebacks(exc)
        for target in merged.values():
            target.data_parts.clear()
            target.data = b""
        normal_parts.clear()
        rebase_parts.clear()
        out_sections.clear()
        output_rows.clear()
        raise
    finally:
        try:
            try:
                try:
                    if payload_storage is not None:
                        payload_storage.close(_error=failure)
                finally:
                    if row_storage is not None:
                        row_storage.close(_error=failure)
            finally:
                if stack_payloads is not None:
                    stack_payloads.close()
        finally:
            row_storage = row_targets = payload_storage = failure = None
            stack_payloads = None
            descriptors.clear()
            archive_buffers.clear()
            raw = packed = payload = descriptor = section = target = part = members = None
            pulled = member = item = row_parts = rows = frozen = stack_payload = None
            stack_offsets = _pending = patch_targets = None
            definitions = local_renames = defined = referenced = weak_references = strong_references = None
            symbol_indices = symbol_at = input_keys = input_plan = None
            appearance = symbol = owner = None


def prepare_executable_from_paths(paths, *, archives=(), archive_paths=(), compact_budget=None, caller_owners=(), _phase_callback=None, _staged_rows=False):
    """Private all-PCO path entry; no eager aggregate encoded-input owner."""
    if _staged_rows:
        from .macho_host_output import _require_uninstrumented_owner
        _require_uninstrumented_owner()
    ledger = _Ledger(compact_budget)
    # Reused budgets can carry an earlier byte refusal into a later wire or
    # record-work error. Only this operation's refusal belongs in its error.
    compact_budget.last_rejection = ""
    if type(paths) not in (list, tuple) or type(archives) not in (list, tuple) or type(archive_paths) not in (list, tuple):
        raise link.LinkError("windowed input tables must be lists or tuples")
    if any(type(path) is not str for values in (paths, archive_paths) for path in values):
        raise link.LinkError("windowed input paths must be strings")
    external = sum(_model_bytes(owner, compact_budget) for owner in (paths, archives, archive_paths, caller_owners))
    ledger.keep(external, label="Mach-O retained caller metadata")
    ledger.caller_bytes = external
    sections = rows = undefined = weak = row_storage = payload_storage = failure = result = None
    try:
        sections, rows, undefined, weak, payload_storage = _merge_paths(
            paths, archives, archive_paths, ledger, _phase_callback,
            _staged_rows=_staged_rows,
        )
        if _staged_rows:
            row_storage, rows = rows, None
        previous = compact_budget.retained_bytes
        compact_budget.retained_bytes = previous + external
        try:
            emit_phase(_phase_callback, "source_view_begin")
            result = native.OwnedMergedSourceView(sections, undefined=undefined, weak_undefined=weak,
                                                 _compact_rows=rows, compact_budget=compact_budget,
                                                 _row_storage=row_storage, _payload_storage=payload_storage)
            if row_storage is not None:
                result._host_row_copy_bound += external
            if payload_storage is not None:
                result._host_payload_copy_bound += external
            # Retain our cleanup owner until after the last callback and
            # eligibility check. A profile return hook can escape emit_phase's
            # own fail-open try without ever delivering this source view.
            emit_phase(_phase_callback, "source_view_end")
            if row_storage is not None:
                _require_uninstrumented_owner()
            row_storage = payload_storage = None  # only the returned view owns them
            return result
        finally:
            compact_budget.retained_bytes = previous
    except (native.NativeObjectError, PreciseStackMapError, spec.MachOFormatError) as exc:
        failure = exc
        _release_closed_tracebacks(exc)
        sizes = getattr(compact_budget, "last_rejection", "") if isinstance(exc, PreciseStackMapError) else ""
        detail = "; " + sizes + " ledger_retained=" + str(ledger.retained) if sizes else ""
        raise link.LinkError("windowed merged object is outside the proven subset: " + str(exc) + detail) from exc
    except BaseException as exc:
        failure = exc
        _release_closed_tracebacks(exc)
        raise
    finally:
        try:
            try:
                if payload_storage is not None:
                    payload_storage.close(_error=failure)
            finally:
                if row_storage is not None:
                    row_storage.close(_error=failure)
        finally:
            if sections is not None:
                sections.clear()
            if rows is not None:
                rows.clear()
            sections = rows = undefined = weak = row_storage = payload_storage = failure = result = None
