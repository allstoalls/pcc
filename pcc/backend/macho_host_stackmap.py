"""Bounded CPython stack-map merge with file-owned construction bytes.

The windowed Mach-O caller transfers an exclusive list of detached bytes.
Functions and exact whole root vectors are spooled, not retained as heap
chunks. Integer descriptors and packed indexes survive between inputs.
Digest matches are always compared byte-for-byte before sharing storage.
The native and ordinary shared mergers do not use this implementation.
"""

import hashlib
import io
import sys
import tempfile

from . import precise_stackmap as wire
from .macho_host_stackmap_index import _IndexWork, _RootIndex, _SourceIndex
from .macho_host_stackmap_inputs import _StagedStackMapInputs


_BLOCK_BYTES = 64 * 1024
_FIXED_BYTES = 2 * _BLOCK_BYTES + 16384


class _SpoolWork:
    """Bound disk traffic independently of byte-owner admission."""

    def __init__(self, budget):
        # Every output byte is written once and read once. Collision checks
        # have a separate MAX_LOCATIONS allowance, charged before each read.
        self.write_limit = budget.limit_bytes
        self.read_limit = budget.limit_bytes + wire.MAX_LOCATIONS * wire.LOCATION_SIZE
        self.written = 0
        self.read = 0
        self.compared_locations = 0
        self.comparison_limit = 0

    def charge_comparison(self, count):
        # Bound collision work by both the unchanged absolute location cap
        # and a constant multiple of distinct source-range work admitted so
        # far. Even adversarial digests cannot turn U ranges into U**2 work.
        limit = min(wire.MAX_LOCATIONS, self.comparison_limit)
        if count > limit - self.compared_locations:
            raise wire.PreciseStackMapError("stack-map spool comparison work budget exceeded")
        self.compared_locations += count

    def write_all(self, stream, value):
        size = len(value)
        if size > self.write_limit - self.written:
            raise wire.PreciseStackMapError("stack-map spool write work budget exceeded")
        self.written += size
        with memoryview(value) as view:
            cursor = 0
            while cursor < size:
                with view[cursor:] as remaining:
                    count = stream.write(remaining)
                if count is None or count <= 0 or count > size - cursor:
                    raise wire.PreciseStackMapError("short stack-map spool write")
                cursor += count

    def read_exact(self, stream, view):
        size = len(view)
        if size > self.read_limit - self.read:
            raise wire.PreciseStackMapError("stack-map spool read work budget exceeded")
        self.read += size
        cursor = 0
        while cursor < size:
            with view[cursor:min(size, cursor + _BLOCK_BYTES)] as remaining:
                count = stream.readinto(remaining)
                capacity = len(remaining)
            if count is None or count <= 0 or count > capacity:
                raise wire.PreciseStackMapError("short stack-map spool read")
            cursor += count

    def equal(self, stream, index, candidate, scratch):
        count = len(candidate) // wire.LOCATION_SIZE
        self.charge_comparison(count)
        stream.seek(index * wire.LOCATION_SIZE)
        for cursor in range(0, len(candidate), _BLOCK_BYTES):
            size = min(_BLOCK_BYTES, len(candidate) - cursor)
            with memoryview(scratch)[:size] as actual:
                self.read_exact(stream, actual)
                with candidate[cursor:cursor + size] as expected:
                    if actual != expected:
                        return False
        return True


def merge_staged_stack_map_payloads(payloads, *, compact_budget, single_buffer, runtime_table=False):
    """Consume a private same-handle input spool, retaining one input window."""
    if (sys.implementation.name != "cpython" or compact_budget is None
            or type(payloads) is not _StagedStackMapInputs or not payloads.sealed):
        raise wire.PreciseStackMapError("staged stack-map merge requires owned host storage")
    try:
        return merge_owned_stack_map_payloads(
            payloads, compact_budget=compact_budget, single_buffer=single_buffer,
            _staged=True, runtime_table=runtime_table,
        )
    finally:
        try:
            payloads.close()
        finally:
            payloads = None


def merge_owned_stack_map_payloads(payloads, *, compact_budget, single_buffer,
                                   _staged=False, runtime_table=False):
    """Return the original merger's exact bytes and relocation offsets.

    ``single_buffer`` is supplied only by the caller's verified GIL CPython
    3.13--3.15/64-bit guard. Other hosts retain a two-buffer freeze admission.
    The ordinary list entry retires only completed input slots; its caller
    must clear remaining inputs on failure and charge any aliases. The private
    staged entry instead supplies one owned input window and closes storage.
    """
    if not payloads:
        raise wire.PreciseStackMapError("cannot merge an empty stack-map collection")
    if sys.implementation.name != "cpython" or compact_budget is None:
        raise wire.PreciseStackMapError("consuming stack-map merge requires owned host bytes")
    valid = (type(payloads) is _StagedStackMapInputs and payloads.sealed) if _staged else (
        type(payloads) is list and all(type(payload) is bytes for payload in payloads)
    )
    if not valid:
        raise wire.PreciseStackMapError("consuming stack-map merge requires owned host bytes")
    staged = _staged
    budget = compact_budget
    input_bytes = 0 if staged else sum(map(len, payloads))
    base_entries = len(payloads) + 16
    budget.check_peak(input_bytes + _FIXED_BYTES, entries=base_entries,
                      label="spool merge inputs")
    functions, seen_ids, root_index = [], set(), None
    scanned = source_index_cache = payload = blob = buffer = view = None
    result, closing, address_offsets = None, None, []
    scratch = bytearray(_BLOCK_BYTES)
    function_bytes = table_locations = merge_locations = unique_vectors = 0
    empty_shape_index, arch = -1, 0
    work = _SpoolWork(budget)

    def index_bytes():
        return ((root_index.owned_bytes() if root_index is not None else 0)
                + (source_index_cache.owned_bytes()
                   if source_index_cache is not None else 0))

    def admit(extra=0, *, pending_functions=0, label):
        # Function tuples/integers, seen-id set, sort pointers and scanner
        # descriptors retain the original model. Root/source metadata is now
        # owned by exact-size bytearrays, not per-entry dict/list/tuple graphs.
        # Existing fixed/base reserves cover bounded table objects/scalars.
        entries = (base_entries + 4 * (len(functions) + pending_functions)
                   + (len(scanned) if scanned is not None else 0))
        budget.check_peak(input_bytes + _FIXED_BYTES + index_bytes() + extra,
                          entries=entries, label=label)

    def admit_index(extra, label):
        # The replacing buffer is not allocated yet; current buffers remain
        # counted through index_bytes until the copying frame releases them.
        admit(extra, label=label)

    def output_bound(size, count):
        # A future single output, and therefore disk storage, must itself fit
        # the unchanged allowance. This never credits currently live inputs.
        budget.check_peak(size + _FIXED_BYTES, entries=base_entries + 5 * count,
                          label="spool output bound")

    try:
        index_work = _IndexWork(budget)
        root_index = _RootIndex(budget, index_work, admit_index)
        with tempfile.TemporaryFile(mode="w+b", buffering=0) as roots, \
                tempfile.TemporaryFile(mode="w+b", buffering=0) as records:
            for consumed_index in range(len(payloads)):
                if staged:
                    payload = payloads.read_next(
                        consumed_index, compact_budget=budget, admit=admit,
                        single_buffer=single_buffer,
                    )
                    input_bytes = budget._rounded(sys.getsizeof(payload))
                else:
                    payload = payloads[consumed_index]
                previous = budget.retained_bytes
                budget.retained_bytes += (
                    input_bytes - len(payload) + _FIXED_BYTES
                    + (base_entries + 4 * len(functions)) * budget.metadata_entry_bytes
                    + index_bytes()
                )
                try:
                    _count, scanned, table_start, table_count = wire._scan_stack_map_payload(
                        payload, compact_budget=budget,
                    )
                finally:
                    budget.retained_bytes = previous
                source_arch = payload[10]
                if arch and arch != source_arch:
                    raise wire.PreciseStackMapError("cannot merge stack maps for different targets")
                arch = source_arch
                source_index_cache = _SourceIndex(budget, index_work, admit_index)
                with memoryview(payload) as source:
                    for function_id, fn_start, fn_end in scanned:
                        if len(functions) >= wire.MAX_FUNCTIONS:
                            raise wire.PreciseStackMapError("too many stack-map functions")
                        if function_id in seen_ids:
                            raise wire.PreciseStackMapError(
                                f"duplicate stack-map function id {function_id}")
                        admit(pending_functions=1, label="spool merge function")
                        size = fn_end - fn_start
                        output_bound(wire.HEADER_SIZE + function_bytes + size
                                     + table_locations * wire.LOCATION_SIZE,
                                     len(functions) + 1)
                        seen_ids.add(function_id)
                        functions.append((function_id, function_bytes, size))
                        function_bytes += size
                        with source[fn_start:fn_start + wire.FUNCTION_SIZE] as header:
                            blob = bytearray(header)
                        blob[8:16] = b"\0" * 8
                        work.write_all(records, blob)
                        blob = None
                        record_count = (size - wire.FUNCTION_SIZE) // wire.RECORD_SIZE
                        budget.charge_records(record_count)
                        for start in range(fn_start + wire.FUNCTION_SIZE, fn_end, _BLOCK_BYTES):
                            with source[start:min(fn_end, start + _BLOCK_BYTES)] as block:
                                blob = bytearray(block)
                            for cursor in range(0, len(blob), wire.RECORD_SIZE):
                                count = blob[cursor + 20] | (blob[cursor + 21] << 8)
                                index = (blob[cursor + 28] | (blob[cursor + 29] << 8)
                                         | (blob[cursor + 30] << 16) | (blob[cursor + 31] << 24))
                                if index + count > table_count:
                                    raise wire.PreciseStackMapError(
                                        "stack-map record names locations outside its table")
                                source_key = (index << 16) | count
                                if count == 0:
                                    if empty_shape_index < 0:
                                        empty_shape_index = table_locations
                                    destination = empty_shape_index
                                else:
                                    destination, source_slot = source_index_cache.find(source_key)
                                if count and destination < 0:
                                    merge_locations += count
                                    if merge_locations > wire.MAX_LOCATIONS:
                                        raise wire.PreciseStackMapError("too much stack-map location merge work")
                                    work.comparison_limit = 4 * merge_locations
                                    admit(extra=2 * budget.metadata_entry_bytes,
                                          label="spool merge cache")
                                    offset = table_start + index * wire.LOCATION_SIZE
                                    with source[offset:offset + count * wire.LOCATION_SIZE] as candidate:
                                        digest = hashlib.sha256(candidate).digest()
                                        destination, root_slot = root_index.find(
                                            digest, count, candidate, roots, scratch, work,
                                        )
                                        if destination < 0:
                                            destination = table_locations
                                            if count > wire.MAX_LOCATIONS - table_locations:
                                                raise wire.PreciseStackMapError("too many stack-map locations")
                                            output_bound(wire.HEADER_SIZE + function_bytes
                                                         + (table_locations + count) * wire.LOCATION_SIZE,
                                                         len(functions))
                                            roots.seek(table_locations * wire.LOCATION_SIZE)
                                            work.write_all(roots, candidate)
                                            root_index.append(digest, count, destination, root_slot)
                                            table_locations += count
                                            unique_vectors += 1
                                        digest = None
                                    source_index_cache.append(source_key, destination, source_slot)
                                blob[cursor + 28:cursor + 32] = destination.to_bytes(4, "little")
                            work.write_all(records, blob)
                            blob = None
                # No view, chunk or cache owns input bytes past this point.
                retired = input_bytes if staged else len(payload)
                source_index_cache.close()
                scanned = source_index_cache = None
                if staged:
                    payloads.retire(consumed_index)
                else:
                    payloads[consumed_index] = None
                payload = None
                input_bytes -= retired

            if staged:
                payloads.close()
            root_index.close()
            seen_ids.clear()
            root_index = seen_ids = digest = None
            scratch = None
            if runtime_table:
                from .macho_host_runtime_stackmap import emit_runtime_table
                admit(label="runtime spool sort")
                functions.sort(key=lambda item: item[0])
                return emit_runtime_table(
                    functions, records, roots, table_locations, arch,
                    budget=budget, single_buffer=single_buffer, work=work,
                    base_entries=base_entries, index_work=index_work,
                )
            total = wire.HEADER_SIZE + function_bytes + table_locations * wire.LOCATION_SIZE
            # Admit before sorting, offset-list growth or output allocation.
            # The portable freeze keeps both allocations charged. The verified
            # BytesIO route keeps one exact bytes backing, never append growth.
            copies = 1 if single_buffer else 2
            budget.check_peak(copies * budget._rounded(sys.getsizeof(b"") + total)
                              + _FIXED_BYTES,
                              entries=base_entries + 5 * len(functions),
                              label="spool merge materialize")
            functions.sort(key=lambda item: item[0])
            buffer = io.BytesIO(b"\0" * total) if single_buffer else bytearray(total)
            view = buffer.getbuffer() if single_buffer else memoryview(buffer)
            view[:wire.HEADER_SIZE] = wire._HEADER.pack(
                wire.MAGIC, wire.VERSION, arch, wire.POINTER_SIZE,
                len(functions), table_locations, 0,
            )
            cursor = wire.HEADER_SIZE
            for function_id, offset, size in functions:
                if size > total - cursor:
                    raise wire.PreciseStackMapError("stack-map spool function outside output")
                records.seek(offset)
                with view[cursor:cursor + size] as target:
                    work.read_exact(records, target)
                address_offsets.append((function_id, cursor + 8))
                cursor += size
            roots.seek(0)
            with view[cursor:] as target:
                work.read_exact(roots, target)
            functions.clear()
            view.release()
            view = None
            result = buffer.getvalue() if single_buffer else bytes(buffer)
            if single_buffer:
                closing, buffer = buffer, None
                try:
                    closing.close()
                finally:
                    closing = None
            buffer = None
            return result, tuple(address_offsets)
    except OSError as exc:
        raise wire.PreciseStackMapError("stack-map temporary storage failed: " + str(exc)) from exc
    finally:
        try:
            if view is not None:
                view.release()
        finally:
            try:
                if single_buffer and buffer is not None:
                    buffer.close()
            finally:
                functions.clear()
                address_offsets.clear()
                if root_index is not None:
                    root_index.close()
                if source_index_cache is not None:
                    source_index_cache.close()
                if seen_ids is not None:
                    seen_ids.clear()
                scanned = source_index_cache = payload = blob = None
                buffer = view = scratch = result = closing = None


_ORIGINAL_BUDGET_TYPE = wire.CompactStackMapBudget
_ORIGINAL_BUDGET_BASES = _ORIGINAL_BUDGET_TYPE.__bases__
_ORIGINAL_BUDGET_ATTRIBUTES = tuple(vars(_ORIGINAL_BUDGET_TYPE).items())
_ORIGINAL_BUDGET_FUNCTIONS = tuple(
    (value, value.__code__, value.__defaults__, value.__kwdefaults__,
     tuple(value.__kwdefaults__.items()) if value.__kwdefaults__ is not None else ())
    for _, value in _ORIGINAL_BUDGET_ATTRIBUTES
    if callable(value) and hasattr(value, "__code__")
)
_BUDGET_INTEGER_FIELDS = (
    "limit_bytes", "record_visit_limit", "record_visits", "retained_bytes",
    "set_header_bytes", "set_entry_bytes", "integer_bytes", "metadata_entry_bytes",
    "allocation_quantum", "_last_records", "_last_set_bytes",
)
_ORIGINAL_TEMPORARY_FILE = tempfile.TemporaryFile
_ORIGINAL_BYTES_IO = io.BytesIO
_ORIGINAL_SPOOL_WORK = _SpoolWork
_ORIGINAL_SPOOL_METHODS = tuple(
    (name, getattr(_SpoolWork, name))
    for name in ("__init__", "charge_comparison", "write_all", "read_exact", "equal")
)
_ORIGINAL_STAGED_METHODS = tuple(
    (name, getattr(_StagedStackMapInputs, name))
    for name in ("read_next", "retire", "close")
)
_ORIGINAL_SCANNER = wire._scan_stack_map_payload
_ORIGINAL_OWNED_MERGER = merge_owned_stack_map_payloads


def runtime_spool_supported(budget):
    """Keep custom observers on the original late-format conversion path.

    A budget callback or scanner may change the format environment during
    merging. Such calls must still receive v2 input and run before the public
    formatter, including on exact budget instances with replaced methods.
    """
    if (wire.CompactStackMapBudget is not _ORIGINAL_BUDGET_TYPE
            or type(budget) is not _ORIGINAL_BUDGET_TYPE
            or tempfile.TemporaryFile is not _ORIGINAL_TEMPORARY_FILE
            or io.BytesIO is not _ORIGINAL_BYTES_IO
            or _SpoolWork is not _ORIGINAL_SPOOL_WORK
            or wire.PRECISE_STACKMAP_FORMAT_ENV != "PCC_PRECISE_STACKMAP_FORMAT"
            or wire._scan_stack_map_payload is not _ORIGINAL_SCANNER
            or merge_owned_stack_map_payloads is not _ORIGINAL_OWNED_MERGER):
        return False
    for owner, methods in ((_SpoolWork, _ORIGINAL_SPOOL_METHODS),
                           (_StagedStackMapInputs, _ORIGINAL_STAGED_METHODS)):
        if any(getattr(owner, name) is not original for name, original in methods):
            return False
    attributes = type.__getattribute__(_ORIGINAL_BUDGET_TYPE, "__dict__")
    # Check class provenance before any instance lookup: new descriptors or a
    # replaced __getattribute__ must never run just to select an optimization.
    if (type.__getattribute__(_ORIGINAL_BUDGET_TYPE, "__bases__") is not _ORIGINAL_BUDGET_BASES
            or len(attributes) != len(_ORIGINAL_BUDGET_ATTRIBUTES)
            or any(name not in attributes or attributes[name] is not original
                   for name, original in _ORIGINAL_BUDGET_ATTRIBUTES)):
        return False
    for function, code, defaults, keywords, items in _ORIGINAL_BUDGET_FUNCTIONS:
        if (function.__code__ is not code or function.__defaults__ is not defaults
                or function.__kwdefaults__ is not keywords):
            return False
        if keywords is not None and (
                len(keywords) != len(items)
                or any(name not in keywords or keywords[name] is not value
                       for name, value in items)):
            return False
    values = vars(budget)
    if set(values) != set(_BUDGET_INTEGER_FIELDS) | {"last_rejection"}:
        return False
    return (all(type(values[name]) is int for name in _BUDGET_INTEGER_FIELDS)
            and type(values["last_rejection"]) is str)
