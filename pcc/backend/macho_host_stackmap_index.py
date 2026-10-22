"""Private packed metadata for the consuming CPython stack-map spool.

These tables own fixed-size bytearrays, not one Python graph per root vector.
Root rows retain insertion order; their sparse index contains only ordinals.
All equality decisions still belong to the spool's full byte comparison.
"""

import struct
import sys

from . import precise_stackmap as wire


_ROOT = struct.Struct("<QQQQIH2x")  # full SHA-256, destination, count
_SOURCE = struct.Struct("<QI4x")  # complete (index << 16) | count, destination
_WORDS = struct.Struct("<QQQQ")
_U32 = struct.Struct("<I")
_MASK64 = (1 << 64) - 1
_BYTEARRAY_HEADER = sys.getsizeof(bytearray())
_PROBES_PER_OPERATION = 16


def _buffer_bytes(size, budget):
    # Exact-size bytearray construction, including its trailing zero byte.
    return (budget._rounded(_BYTEARRAY_HEADER)
            + budget._rounded(size + 1))


def _mix64(value):
    # Mix index high bits too: count occupies the identical low 16 bits of
    # many source keys. Python's integer hash alone does not do this mixing.
    value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
    value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & _MASK64
    return (value ^ (value >> 31)) & _MASK64


def _root_hash(words):
    # Count is deliberately absent. Different-count, same-digest candidates
    # must still visit the original comparison-work check in FIFO order.
    return _mix64(words[0] ^ words[1] ^ words[2] ^ words[3])


class _IndexWork:
    """Bound new table work separately from location/record/spool work.

    Let R be nonempty record lookups, M distinct source ranges across inputs,
    and U unique vectors. Operations are at most R + 4M + 5U: source lookups,
    inserts and rehashes; root lookups, inserts, rehashes and dense-row copies.
    Doubling tables move fewer than twice their final inserted row count.
    M,U <= MAX_LOCATIONS and R <= the unchanged record-visit allowance.
    Each probe includes at most one fixed 40-byte root-row read. Empty source
    slot scans and dense-row copies are charged explicitly as well. The
    relative bound is an additional fail-closed adversarial-clustering limit,
    not a claim to preserve the old Python dict's whole acceptance domain.
    """

    __slots__ = ("operation_limit", "allocation_limit", "operations", "steps", "max_probe",
                 "source_scans", "root_copies", "rehash_entries", "allocated_bytes")

    def __init__(self, budget):
        self.operation_limit = budget.record_visit_limit + 9 * wire.MAX_LOCATIONS
        # All exact-size zero initialization, including retired replacements:
        # root rows/indexes <= 192U bytes, source tables <= 128M bytes. The
        # small initial capacities are included, even for a single insertion.
        self.allocation_limit = 320 * wire.MAX_LOCATIONS
        self.operations = self.steps = self.max_probe = 0
        self.source_scans = self.root_copies = self.rehash_entries = 0
        self.allocated_bytes = 0

    def begin(self, count=1):
        if count < 0 or count > self.operation_limit - self.operations:
            raise wire.PreciseStackMapError("stack-map index operation work budget exceeded")
        self.operations += count

    def charge(self, count=1):
        limit = _PROBES_PER_OPERATION * (self.operations + 1)
        if count < 0 or count > limit - self.steps:
            raise wire.PreciseStackMapError("stack-map index probe work budget exceeded")
        self.steps += count

    def probe(self, depth):
        self.charge()
        self.max_probe = max(self.max_probe, depth)

    def allocate(self, size):
        if size < 0 or size > self.allocation_limit - self.allocated_bytes:
            raise wire.PreciseStackMapError("stack-map index allocation work budget exceeded")
        self.allocated_bytes += size

    def copy_rows(self, count):
        self.begin(count)
        self.charge(count)
        self.root_copies += count

    def rehash(self, count):
        self.begin(count)
        self.rehash_entries += count

    def scan_source(self, count):
        self.charge(count)
        self.source_scans += count


class _RootIndex:
    """Append-only 40-byte rows and a half-full u32 ordinal+1 index."""

    __slots__ = ("rows", "index", "used", "capacity", "budget", "work", "admit")

    def __init__(self, budget, work, admit):
        self.rows = self.index = None
        self.used = self.capacity = 0
        self.budget, self.work, self.admit = budget, work, admit

    def owned_bytes(self):
        if self.rows is None:
            return 0
        size = _buffer_bytes(len(self.rows), self.budget)
        if self.index is not None:
            size += _buffer_bytes(len(self.index), self.budget)
        return size

    def find(self, digest, count, candidate, roots, scratch, spool_work):
        try:
            self.work.begin()
            words = _WORDS.unpack(digest)
            if self.index is None:
                return -1, -1
            capacity = len(self.index) // _U32.size
            mask = capacity - 1
            slot = _root_hash(words) & mask
            for depth in range(1, capacity + 1):
                self.work.probe(depth)
                ordinal = _U32.unpack_from(self.index, slot * _U32.size)[0]
                if ordinal == 0:
                    return -1, slot
                row = _ROOT.unpack_from(self.rows, (ordinal - 1) * _ROOT.size)
                if (row[0] == words[0] and row[1] == words[1]
                        and row[2] == words[2] and row[3] == words[3]):
                    if row[5] != count:
                        spool_work.charge_comparison(1)
                    elif spool_work.equal(roots, row[4], candidate, scratch):
                        return row[4], slot
                slot = (slot + 1) & mask
            raise wire.PreciseStackMapError("stack-map root index has no empty slot")
        finally:
            candidate = roots = scratch = spool_work = None

    def _place(self, target, words, encoded):
        # Already known to be a new row, or a rehash of an existing row.
        # Never call equal()/charge_comparison() a second time after a miss.
        try:
            capacity = len(target) // _U32.size
            mask = capacity - 1
            slot = _root_hash(words) & mask
            for depth in range(1, capacity + 1):
                self.work.probe(depth)
                if _U32.unpack_from(target, slot * _U32.size)[0] == 0:
                    _U32.pack_into(target, slot * _U32.size, encoded)
                    return
                slot = (slot + 1) & mask
            raise wire.PreciseStackMapError("stack-map root index has no empty slot")
        finally:
            # A saved exception must not keep the temporary replacement table.
            target = None

    def _grow_rows(self, capacity):
        old, new = self.rows, None
        try:
            self.admit(_buffer_bytes(capacity * _ROOT.size, self.budget),
                       "spool root rows grow")
            self.work.allocate(capacity * _ROOT.size)
            new = bytearray(capacity * _ROOT.size)
            self.work.copy_rows(self.used)
            if old is not None:
                with memoryview(old) as source, memoryview(new) as target:
                    with target[:self.used * _ROOT.size] as prefix:
                        prefix[:] = source
            self.rows, new = new, None
            self.capacity = capacity
        finally:
            old = new = None

    def _grow_index(self):
        old, new = self.index, None
        try:
            size = 2 * self.capacity * _U32.size
            self.admit(_buffer_bytes(size, self.budget), "spool root index grow")
            self.work.allocate(size)
            new = bytearray(size)
            self.work.rehash(self.used)
            # Ordinals are append order, regardless of physical slot wrap.
            # Thus every same-digest candidate retains its exact FIFO order.
            for ordinal in range(self.used):
                self.work.charge()  # fixed row read, in addition to placement
                words = _WORDS.unpack_from(self.rows, ordinal * _ROOT.size)
                self._place(new, words, ordinal + 1)
            self.index, new = new, None
        finally:
            old = new = None

    def append(self, digest, count, destination, empty_slot):
        if (not 0 < count <= 0xFFFF or not 0 <= destination <= 0xFFFFFFFF
                or self.used >= wire.MAX_LOCATIONS):
            raise wire.PreciseStackMapError("invalid packed stack-map root index field")
        self.work.begin()
        grew = self.used == self.capacity
        if grew:
            capacity = max(4, 2 * self.capacity)
            # Old/new rows overlap first; release old rows before allocating
            # the replacement sparse index. Both phases admit all live owners.
            self._grow_rows(capacity)
            self._grow_index()
        words = _WORDS.unpack(digest)
        _ROOT.pack_into(self.rows, self.used * _ROOT.size, *words, destination, count)
        if grew:
            self._place(self.index, words, self.used + 1)
        else:
            self.work.probe(1)  # one known-empty slot write, no duplicate lookup
            _U32.pack_into(self.index, empty_slot * _U32.size, self.used + 1)
        self.used += 1

    def close(self):
        self.rows = self.index = self.admit = None
        self.used = self.capacity = 0


class _SourceIndex:
    """Half-full 16-byte slots; key zero is empty (nonempty ranges only)."""

    __slots__ = ("buffer", "used", "budget", "work", "admit")

    def __init__(self, budget, work, admit):
        self.buffer = None
        self.used = 0
        self.budget, self.work, self.admit = budget, work, admit

    def owned_bytes(self):
        return 0 if self.buffer is None else _buffer_bytes(len(self.buffer), self.budget)

    def find(self, key):
        self.work.begin()
        if self.buffer is None:
            return -1, -1
        capacity = len(self.buffer) // _SOURCE.size
        mask = capacity - 1
        slot = _mix64(key) & mask
        for depth in range(1, capacity + 1):
            self.work.probe(depth)
            existing, destination = _SOURCE.unpack_from(self.buffer, slot * _SOURCE.size)
            if existing == 0:
                return -1, slot
            if existing == key:
                return destination, slot
            slot = (slot + 1) & mask
        raise wire.PreciseStackMapError("stack-map source index has no empty slot")

    def _place(self, target, key, destination):
        try:
            capacity = len(target) // _SOURCE.size
            mask = capacity - 1
            slot = _mix64(key) & mask
            for depth in range(1, capacity + 1):
                self.work.probe(depth)
                if _SOURCE.unpack_from(target, slot * _SOURCE.size)[0] == 0:
                    _SOURCE.pack_into(target, slot * _SOURCE.size, key, destination)
                    return
                slot = (slot + 1) & mask
            raise wire.PreciseStackMapError("stack-map source index has no empty slot")
        finally:
            target = None

    def _grow(self):
        old, new = self.buffer, None
        try:
            capacity = 0 if old is None else len(old) // _SOURCE.size
            size = max(8, 2 * capacity) * _SOURCE.size
            self.admit(_buffer_bytes(size, self.budget), "spool source index grow")
            self.work.allocate(size)
            new = bytearray(size)
            self.work.rehash(self.used)
            self.work.scan_source(capacity)  # empty slots are real work too
            for slot in range(capacity):
                key, destination = _SOURCE.unpack_from(old, slot * _SOURCE.size)
                if key:
                    self._place(new, key, destination)
            self.buffer, new = new, None
        finally:
            old = new = None

    def append(self, key, destination, empty_slot):
        if (not 0 < key <= 0xFFFFFFFFFFFF or key & 0xFFFF == 0
                or not 0 <= destination <= 0xFFFFFFFF):
            raise wire.PreciseStackMapError("invalid packed stack-map source index field")
        self.work.begin()
        if self.buffer is None or 2 * self.used >= len(self.buffer) // _SOURCE.size:
            self._grow()
            self._place(self.buffer, key, destination)
        else:
            self.work.probe(1)
            _SOURCE.pack_into(self.buffer, empty_slot * _SOURCE.size, key, destination)
        self.used += 1

    def close(self):
        self.buffer = self.admit = None
        self.used = 0
