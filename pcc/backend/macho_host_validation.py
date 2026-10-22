"""Private host validation with file-owned global identities and root keys.

The public/native validator remains the oracle. Large owned snapshots keep
that same one bytes backing, but do not grow a Python set per safepoint or
root slice. Fixed-size sorted runs preserve complete uint64 identities and
the first-use order of exact (location index, count, frame) keys.
"""

import heapq
import os
import struct
import sys
import tempfile

from . import precise_stackmap as wire
from . import macho_host_runtime_validation as runtime_validation


_SMALL_PAYLOAD = 1 << 20
_CHUNK_ROWS = 32768
_FANIN = 32
_BLOCK = 64 * 1024


def _workspace_bytes(records, locations, budget):
    """One pre-admitted upper bound for every simultaneously live owner.

    At most two chunk lists coexist. A metadata charge covers each three-int
    tuple, its list slot, list-growth overlap and timsort scratch. Three run
    descriptor lists cover the pending other sorter plus old/new merge runs.
    Heap rows/readers have a separate per-fan-in allowance. The fixed reserve
    covers file/iterator/codec objects and scalar parsing temporaries.
    """
    rows = min(records, _CHUNK_ROWS)
    runs = (records + _CHUNK_ROWS - 1) // _CHUNK_ROWS
    entries = 2 * rows + 3 * runs + 4 * _FANIN + 64
    # Reader buffers, one packing buffer, and bytearray construction overlap.
    buffers = (_FANIN + 2) * budget._rounded(sys.getsizeof(bytearray()) + _BLOCK + 1)
    buffers += 2 * budget._rounded(sys.getsizeof(bytearray()) + 24 * rows + 1)
    # The original one-slice flags list, including append growth overlap.
    flags = (locations * budget.integer_bytes
             + budget._rounded(sys.getsizeof([]))
             + 2 * budget._rounded((locations + locations // 8 + 6) * 8))
    return entries * budget.metadata_entry_bytes + buffers + flags + 65536


class _Work:
    def __init__(self, records):
        runs = (records + _CHUNK_ROWS - 1) // _CHUNK_ROWS
        passes = 0
        while runs > 1:
            runs = (runs + _FANIN - 1) // _FANIN
            passes += 1
        # Three streams have widths 16, 24, 24. Initial write, final read,
        # and each merge's read/write total <= 128*N*(passes+1) bytes.
        # Factor two admits bounded partial progress, without quadratic I/O.
        self.remaining = 256 * records * (passes + 1) + 16 * _BLOCK
        # Each of three streams visits at most N rows per merge generation
        # and once for its final scan. Initial chunk sorts are capped at
        # _CHUNK_ROWS and heap comparisons at _FANIN, independent of N.
        self.rows_remaining = 3 * records * (passes + 1)
        self.record_limit = records

    def visit(self):
        if self.rows_remaining == 0:
            raise wire.PreciseStackMapError("stack-map validation sort work budget exceeded")
        self.rows_remaining -= 1

    def charge(self, size):
        if size < 0 or size > self.remaining:
            raise wire.PreciseStackMapError("stack-map validation I/O work budget exceeded")
        self.remaining -= size

    def write(self, stream, position, value):
        if stream.seek(position) != position:
            raise wire.PreciseStackMapError("stack-map validation spool seek changed")
        with memoryview(value) as view:
            cursor = 0
            while cursor < len(view):
                end = min(len(view), cursor + _BLOCK)
                self.charge(end - cursor)
                with view[cursor:end] as part:
                    count = stream.write(part)
                if type(count) is not int or not 0 < count <= end - cursor:
                    raise wire.PreciseStackMapError("stack-map validation spool made invalid write progress")
                cursor += count

    def read(self, stream, position, view):
        if stream.seek(position) != position:
            raise wire.PreciseStackMapError("stack-map validation spool seek changed")
        cursor = 0
        while cursor < len(view):
            self.charge(len(view) - cursor)
            with view[cursor:] as part:
                count = stream.readinto(part)
            if type(count) is not int or not 0 < count <= len(view) - cursor:
                raise wire.PreciseStackMapError("stack-map validation spool made invalid read progress")
            cursor += count


class _Reader:
    def __init__(self, owner, start, count):
        if (start < 0 or start % owner.codec.size or count < 0
                or count > owner.work.record_limit
                or start + count * owner.codec.size > owner.extent):
            raise wire.PreciseStackMapError("stack-map validation spool run changed")
        self.owner = owner
        self.position = start
        self.remaining = count
        self.buffer = bytearray((_BLOCK // owner.codec.size) * owner.codec.size)
        self.cursor = self.end = 0

    def next(self):
        if self.remaining == 0:
            return None
        self.owner.work.visit()
        if self.cursor == self.end:
            size = min(len(self.buffer), self.remaining * self.owner.codec.size)
            with memoryview(self.buffer)[:size] as target:
                self.owner.work.read(self.owner.file, self.position, target)
            self.position += size
            self.cursor, self.end = 0, size
        row = self.owner.codec.unpack_from(self.buffer, self.cursor)
        self.cursor += self.owner.codec.size
        self.remaining -= 1
        return row


class _Runs:
    def __init__(self, work, width, dedup=0):
        self.work = work
        self.codec = struct.Struct("<" + "Q" * width)
        self.dedup = dedup
        self.rows = []
        self.runs = []
        self.count = self.extent = 0
        self.file = tempfile.TemporaryFile(mode="w+b", buffering=0)

    def _write_rows(self, stream, position, rows):
        blob = bytearray(len(rows) * self.codec.size)
        for index, row in enumerate(rows):
            self.codec.pack_into(blob, index * self.codec.size, *row)
        self.work.write(stream, position, blob)
        return len(blob)

    def append(self, row):
        if self.count >= self.work.record_limit:
            raise wire.PreciseStackMapError("stack-map validation spool row bound exceeded")
        self.rows.append(row)
        self.count += 1
        if len(self.rows) == _CHUNK_ROWS:
            self.flush()

    def flush(self):
        if not self.rows:
            return
        self.rows.sort()
        if self.dedup:
            previous = None
            used = 0
            for row in self.rows:
                key = row[:self.dedup]
                if key != previous:
                    self.rows[used] = row
                    used += 1
                    previous = key
            del self.rows[used:]
        count = len(self.rows)
        size = self._write_rows(self.file, self.extent, self.rows)
        self.runs.append((self.extent, count))
        self.extent += size
        self.rows.clear()

    def _check_extent(self):
        expected = 0
        for start, count in self.runs:
            if start != expected or count <= 0 or count > self.work.record_limit:
                raise wire.PreciseStackMapError("stack-map validation spool run changed")
            expected += count * self.codec.size
        if (expected != self.extent or self.extent > self.count * self.codec.size
                or (not self.dedup and self.extent != self.count * self.codec.size)):
            raise wire.PreciseStackMapError("stack-map validation spool extent changed")
        if os.fstat(self.file.fileno()).st_size != self.extent:
            raise wire.PreciseStackMapError("stack-map validation spool extent changed")

    def finish(self):
        self.flush()
        self._check_extent()
        while len(self.runs) > 1:
            following = tempfile.TemporaryFile(mode="w+b", buffering=0)
            following_runs = []
            extent = 0
            readers = heap = rows = None
            failed = False
            try:
                for start in range(0, len(self.runs), _FANIN):
                    readers = [_Reader(self, offset, count)
                               for offset, count in self.runs[start:start + _FANIN]]
                    heap = []
                    for index, reader in enumerate(readers):
                        row = reader.next()
                        if row is not None:
                            heapq.heappush(heap, (row, index))
                    run_start, count = extent, 0
                    previous = None
                    rows = []
                    while heap:
                        row, index = heapq.heappop(heap)
                        key = row[:self.dedup] if self.dedup else None
                        if not self.dedup or key != previous:
                            rows.append(row)
                            count += 1
                            previous = key
                            if len(rows) == _CHUNK_ROWS:
                                extent += self._write_rows(following, extent, rows)
                                rows.clear()
                        row = readers[index].next()
                        if row is not None:
                            heapq.heappush(heap, (row, index))
                    if rows:
                        extent += self._write_rows(following, extent, rows)
                        rows.clear()
                    following_runs.append((run_start, count))
                    readers = heap = rows = row = reader = None
                if (extent > self.extent or (not self.dedup and extent != self.extent)
                        or os.fstat(following.fileno()).st_size != extent):
                    raise wire.PreciseStackMapError("stack-map validation spool extent changed")
                self._check_extent()
                self.file.close()
                self.file, following = following, None
                self.extent = extent
                self.runs.clear()
                self.runs, following_runs = following_runs, None
                self._check_extent()
            except BaseException:
                failed = True
                raise
            finally:
                readers = heap = rows = row = reader = None
                if following is not None:
                    try:
                        following.close()
                    except BaseException:
                        if not failed:
                            raise

    def ordered(self):
        self.finish()
        if not self.runs:
            return
        reader = _Reader(self, *self.runs[0])
        try:
            while True:
                row = reader.next()
                if row is None:
                    break
                yield row
            self._check_extent()
        finally:
            reader = row = None

    def close(self):
        self.rows.clear()
        self.runs.clear()
        self.file.close()


def _function_error(fields, previous, final_image):
    identity, address, code_size, frame, _count, _flags = fields
    if identity == 0:
        return "function id is outside uint64"
    if identity <= previous:
        return "functions must be ordered by unique stable function id"
    if final_image and address == 0:
        return "final-image function address is unresolved"
    if code_size == 0:
        return "function code size is outside uint32"
    if frame % 16:
        return "function frame size must be 16-byte aligned"
    return ""


def _record_error(fields, previous, code_size):
    _identity, pc, exceptional, continuation, _count, _reserved, kind, flags, _short, _index = fields
    if kind not in wire.SAFEPOINT_KINDS:
        return f"unknown safepoint kind {kind}"
    if not previous < pc < code_size:
        return "safepoints must have ordered in-function instruction offsets"
    if flags & ~wire.RECORD_FLAGS:
        return "unknown stack-map record flags"
    has_exception = exceptional != wire.NO_OFFSET
    if has_exception and exceptional >= code_size:
        return "exceptional successor exceeds function"
    if has_exception != bool(flags & wire.RECORD_HAS_EXCEPTION_EDGE):
        return "exception-edge flag and offset disagree"
    continuation_kind = kind == wire.SAFEPOINT_CONTINUATION
    if continuation_kind != bool(continuation):
        return "continuation record needs one non-zero continuation id"
    if bool(flags & wire.RECORD_SUSPENDED) != continuation_kind:
        return "suspended flag is reserved for continuation records"
    return ""


def _validate_locations(payload, table_start, index, count, frame, arch):
    registers = (29, 31) if arch == wire.ARCH_AARCH64 else (6, 7)
    register_limit = wire._register_limit(arch)
    prior_flags = []
    for ordinal in range(count):
        kind, flags, size, register, base, offset, extent = wire._LOCATION.unpack_from(
            payload, table_start + (index + ordinal) * wire.LOCATION_SIZE,
        )
        if kind not in wire.LOCATION_KINDS:
            raise wire.PreciseStackMapError(f"unknown location kind {kind}")
        if flags & ~wire.LOCATION_FLAGS:
            raise wire.PreciseStackMapError("unknown stack-map location flags")
        if not flags & wire.LOCATION_MANAGED:
            raise wire.PreciseStackMapError("unclassified raw pointer is not a managed stack-map location")
        if size != wire.POINTER_SIZE or extent != wire.POINTER_SIZE:
            raise wire.PreciseStackMapError("managed locations must be one pointer wide")
        if register > register_limit:
            raise wire.PreciseStackMapError(f"register {register} is outside {wire.ARCH_NAMES[arch]} ABI")
        if kind in (wire.LOCATION_STACK_INDIRECT, wire.LOCATION_STACK_DIRECT):
            if register not in registers:
                raise wire.PreciseStackMapError("stack location must be relative to the target frame/stack register")
            if offset >= 0 or -offset > frame:
                raise wire.PreciseStackMapError(f"stack location {offset} exceeds frame size {frame}")
            if (-offset) % wire.POINTER_SIZE:
                raise wire.PreciseStackMapError("managed stack location is not aligned")
        elif offset != 0:
            raise wire.PreciseStackMapError("register location cannot carry frame offset")
        if flags & wire.LOCATION_DERIVED:
            if not 0 <= base < ordinal:
                raise wire.PreciseStackMapError("derived location must name an earlier base location")
            base_flags = prior_flags[base]
            if not base_flags & wire.LOCATION_MANAGED or base_flags & wire.LOCATION_DERIVED:
                raise wire.PreciseStackMapError("derived location base is not a base root")
            if not flags & wire.LOCATION_RELOAD_REQUIRED:
                raise wire.PreciseStackMapError("derived location must be reloaded after a relocating safepoint")
        elif base != wire.NO_BASE:
            raise wire.PreciseStackMapError("non-derived location carries a base index")
        if kind == wire.LOCATION_REGISTER and not flags & wire.LOCATION_RELOAD_REQUIRED:
            raise wire.PreciseStackMapError("managed register location must reject stale post-safepoint SSA use")
        prior_flags.append(flags)


def validate_stack_map_payload(payload, *, expected_arch=None, final_image=False, compact_budget):
    if not isinstance(payload, bytes):
        raise wire.PreciseStackMapError("stack-map payload must be immutable bytes")
    if wire._payload_version(payload) == wire.RUNTIME_TABLE_VERSION:
        if runtime_validation.available(payload, compact_budget):
            return runtime_validation.validate_runtime_table(
                payload, expected_arch=expected_arch, final_image=final_image,
                compact_budget=compact_budget,
            )
        return wire.validate_runtime_table(
            payload, expected_arch=expected_arch, final_image=final_image,
        )
    if len(payload) < _SMALL_PAYLOAD:
        return wire.validate_stack_map_payload(
            payload, expected_arch=expected_arch, final_image=final_image,
            compact_budget=compact_budget,
        )
    if _CHUNK_ROWS < 1 or _FANIN < 2:
        raise wire.PreciseStackMapError("invalid stack-map validation spool configuration")
    header, cursor = wire._take(payload, 0, wire._HEADER, "stack-map header")
    magic, version, arch, pointer_size, function_count, table_count, _reserved = header
    if magic != wire.MAGIC:
        raise wire.PreciseStackMapError("bad pcc stack-map magic")
    if version != wire.VERSION:
        raise wire.PreciseStackMapError(f"unsupported pcc stack-map version {version}")
    if pointer_size != wire.POINTER_SIZE:
        raise wire.PreciseStackMapError("unsupported stack-map pointer size")
    if arch not in wire.ARCH_NAMES:
        raise wire.PreciseStackMapError(f"unknown stack-map architecture {arch}")
    if expected_arch is not None and arch != expected_arch:
        raise wire.PreciseStackMapError("stack-map architecture does not match target")
    if function_count > wire.MAX_FUNCTIONS:
        raise wire.PreciseStackMapError("too many stack-map functions")
    if table_count > wire.MAX_LOCATIONS:
        raise wire.PreciseStackMapError("too many stack-map locations")
    unretained = compact_budget.unretained_payload_bytes(payload)
    compact_budget.check_peak(unretained, entries=function_count, label="input")
    total_records = max_locations = 0
    for _ in range(function_count):
        fields, cursor = wire._take(payload, cursor, wire._FUNCTION, "function record")
        total_records += fields[4]
        compact_budget.charge_records(fields[4])
        for _ in range(fields[4]):
            row, cursor = wire._take(payload, cursor, wire._RECORD, "safepoint record")
            if row[5] or row[8]:
                raise wire.PreciseStackMapError("non-zero reserved stack-map field")
            if row[9] + row[4] > table_count:
                raise wire.PreciseStackMapError("stack-map record names locations outside its table")
            max_locations = max(max_locations, row[4])
    table_start = cursor
    table_end = table_start + table_count * wire.LOCATION_SIZE
    if table_end > len(payload):
        raise wire.PreciseStackMapError("truncated stack-map location table")
    if table_end != len(payload):
        raise wire.PreciseStackMapError("trailing bytes after pcc stack-map payload")
    budget = getattr(compact_budget, "budget", compact_budget)
    compact_budget.check_peak(
        unretained + _workspace_bytes(total_records, max_locations, budget),
        label="Mach-O owned validation workspace",
    )
    work = _Work(total_records)
    ids = roots = ordered_roots = iterator = None
    failure = None
    try:
        ids = _Runs(work, 2)
        roots = _Runs(work, 3, dedup=2)
        cursor = wire.HEADER_SIZE
        previous_function = -1
        boundary = None
        for _ in range(function_count):
            position = cursor
            fields, cursor = wire._take(payload, cursor, wire._FUNCTION, "function record")
            message = _function_error(fields, previous_function, final_image)
            if message:
                boundary = (position, message)
                break
            previous_function, _address, code_size, frame, count, _flags = fields
            try:
                compact_budget.charge_records(count)
            except wire.PreciseStackMapError as exc:
                boundary = (position, str(exc))
                break
            previous_pc = -1
            for _ in range(count):
                position = cursor
                row, cursor = wire._take(payload, cursor, wire._RECORD, "safepoint record")
                if row[0] == 0:
                    boundary = (position, "safepoint id is outside uint64")
                    break
                ids.append((row[0], position))
                message = _record_error(row, previous_pc, code_size)
                if message:
                    boundary = (position, message)
                    break
                previous_pc = row[1]
                if row[4]:
                    roots.append(((row[9] << 16) | row[4], frame, position))
            if boundary is not None:
                break
        previous_id = None
        iterator = ids.ordered()
        for identity, position in iterator:
            if identity == previous_id and (boundary is None or position <= boundary[0]):
                boundary = (position, "duplicate safepoint id")
            previous_id = identity
        iterator.close()
        iterator = None
        ids.close()
        ids = None
        ordered_roots = _Runs(work, 3)
        previous_key = None
        iterator = roots.ordered()
        for key, frame, position in iterator:
            current = (key, frame)
            if current != previous_key:
                ordered_roots.append((position, key, frame))
                previous_key = current
        iterator.close()
        iterator = None
        roots.close()
        roots = None
        locations = 0
        iterator = ordered_roots.ordered()
        for position, key, frame in iterator:
            # The oracle checks duplicate IDs and scalar record fields before
            # that record's roots. A later error must not mask earlier roots.
            if boundary is not None and position >= boundary[0]:
                break
            count, index = key & 0xFFFF, key >> 16
            locations += count
            if locations > wire.MAX_LOCATIONS:
                raise wire.PreciseStackMapError("too much stack-map location validation work")
            _validate_locations(payload, table_start, index, count, frame, arch)
        iterator.close()
        iterator = None
        if boundary is not None:
            raise wire.PreciseStackMapError(boundary[1])
        if cursor != table_start:
            raise wire.PreciseStackMapError("stack-map record layout changed during validation")
    except BaseException as exc:
        failure = exc
        raise
    finally:
        if iterator is not None:
            iterator.close()
        close_error = None
        for owner in (ids, roots, ordered_roots):
            if owner is not None:
                try:
                    owner.close()
                except BaseException as exc:
                    if close_error is None:
                        close_error = exc
        ids = roots = ordered_roots = iterator = owner = None
        if failure is None and close_error is not None:
            raise close_error
