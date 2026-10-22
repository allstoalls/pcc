from __future__ import annotations

"""Compiler-private dynamic scalar arena with a host oracle projection.

The self-hosted compiler needs variable-length, pointer-free payload tables;
``pcc.array[ValueClass, N]`` deliberately models only small fixed aggregates.
This arena is the dynamic companion for proven machine-range compiler IDs,
offsets, flags, and spans.

Under CPython, ``pcc.unsafe`` traps and the ordinary ``list[int]`` side is the
semantic oracle.  When pcc compiles this module, the unsafe operations lower to
raw allocation and i64 loads/stores.  The raw pointer is *never* stored in an
object/Dyn field: only its integer address is retained and converted back at
the exact intrinsic seam.  That is the static provenance proof which the old
global managed-refcount shortcut lacked.
"""

from pcc.unsafe import (
    free,
    int_to_ptr,
    load_i64,
    malloc,
    memcpy,
    memset,
    ptr_add,
    ptr_is_null,
    ptr_to_int,
    read as raw_read,
    realloc,
    store_i64,
    store_i32,
    write as raw_write,
)
from pcc.extern import c_int64, c_ptr, extern, c_obj


_py_bytes_new: "extern" = extern("py_bytes_new", (c_ptr, c_int64), c_obj)


def valueclass(cls):
    """Self-contained host marker for compiler-private value records.

    The frontend recognizes the decorator spelling and emits the aggregate
    projection.  Importing the public host oracle here would pull all of
    ``pcc.value_model`` into the self-host compiler closure.
    """

    from dataclasses import dataclass

    result = dataclass(frozen=True)(cls)
    result.__pcc_valueclass__ = True
    return result


@valueclass
class CompilerInt2:
    first: int
    second: int


@valueclass
class CompilerInt3:
    first: int
    second: int
    third: int


@valueclass
class CompilerInt4:
    first: int
    second: int
    third: int
    fourth: int


class CompilerIntArena:
    """One growable vector of signed 64-bit compiler-internal scalars."""

    __slots__ = (
        "_address",
        "_values",
        "_length",
        "_capacity",
        "_closed",
    )

    def __init__(self, initial_capacity: int = 8) -> None:
        if initial_capacity < 0:
            raise ValueError("compiler int arena capacity must be nonnegative")
        capacity = max(1, initial_capacity)
        self._address: int = 0
        self._values: list[int] = []
        self._length: int = 0
        self._capacity: int = 0
        self._closed: bool = False
        try:
            allocation = malloc(capacity * 8)
        except NotImplementedError:
            # CPython host oracle.  The compiler consumes the same source but
            # lowers malloc directly, so the compiled path never takes this.
            return
        address = ptr_to_int(allocation)
        if address == 0:
            raise MemoryError("compiler int arena allocation failed")
        self._address = address
        self._capacity = capacity

    def __len__(self) -> int:
        return self._length

    @property
    def uses_native_storage(self) -> bool:
        return self._address != 0

    def native_address(self) -> int:
        """Raw storage address as an integer; 0 while the CPython list oracle holds the values.

        Hot kernels that run the same loop shape tens of millions of times
        (the final stack-map record heapsort) read and write this storage
        directly instead of paying one out-of-line getter or setter call per
        scalar.  Callers must not append while holding the address.
        """
        return self._address

    def _grow(self, required: int) -> None:
        capacity = max(required, self._capacity * 2)
        allocation = realloc(int_to_ptr(self._address), capacity * 8)
        address = ptr_to_int(allocation)
        if address == 0:
            raise MemoryError("compiler int arena growth failed")
        self._address = address
        self._capacity = capacity

    def append(self, value: int) -> None:
        if self._address != 0:
            if self._length >= self._capacity:
                self._grow(self._length + 1)
            store_i64(int_to_ptr(self._address), self._length * 8, value)
        else:
            if self._closed:
                raise RuntimeError("compiler int arena is closed")
            self._values.append(value)
        self._length += 1

    def append2(self, first: int, second: int) -> None:
        if self._address != 0:
            required = self._length + 2
            if required > self._capacity:
                self._grow(required)
            address = int_to_ptr(self._address)
            store_i64(address, self._length * 8, first)
            store_i64(address, (self._length + 1) * 8, second)
        else:
            if self._closed:
                raise RuntimeError("compiler int arena is closed")
            self._values.append(first)
            self._values.append(second)
        self._length += 2

    def append3(self, first: int, second: int, third: int) -> None:
        if self._address != 0:
            required = self._length + 3
            if required > self._capacity:
                self._grow(required)
            address = int_to_ptr(self._address)
            store_i64(address, self._length * 8, first)
            store_i64(address, (self._length + 1) * 8, second)
            store_i64(address, (self._length + 2) * 8, third)
        else:
            if self._closed:
                raise RuntimeError("compiler int arena is closed")
            self._values.append(first)
            self._values.append(second)
            self._values.append(third)
        self._length += 3

    def append4(
        self,
        first: int,
        second: int,
        third: int,
        fourth: int,
    ) -> None:
        if self._address != 0:
            required = self._length + 4
            if required > self._capacity:
                self._grow(required)
            address = int_to_ptr(self._address)
            store_i64(address, self._length * 8, first)
            store_i64(address, (self._length + 1) * 8, second)
            store_i64(address, (self._length + 2) * 8, third)
            store_i64(address, (self._length + 3) * 8, fourth)
        else:
            if self._closed:
                raise RuntimeError("compiler int arena is closed")
            self._values.append(first)
            self._values.append(second)
            self._values.append(third)
            self._values.append(fourth)
        self._length += 4

    def append_zeros(self, count: int) -> None:
        """Append a zero-filled compiler-proven scalar span."""

        if count < 0:
            raise ValueError("compiler int arena zero span must be nonnegative")
        if self._address != 0:
            required = self._length + count
            if required > self._capacity:
                self._grow(required)
            if count:
                memset(
                    ptr_add(int_to_ptr(self._address), self._length * 8),
                    0,
                    count * 8,
                )
        else:
            if self._closed:
                raise RuntimeError("compiler int arena is closed")
            index = 0
            while index < count:
                self._values.append(0)
                index += 1
        self._length += count

    def get(self, index: int) -> int:
        if self._address != 0:
            if index < 0 or index >= self._length:
                raise IndexError("compiler int arena index out of range")
            return load_i64(int_to_ptr(self._address), index * 8)
        if self._closed:
            raise RuntimeError("compiler int arena is closed")
        if index < 0 or index >= self._length:
            raise IndexError("compiler int arena index out of range")
        return self._values[index]

    def get_unchecked(self, index: int) -> int:
        """Read one compiler-proven in-range index without semantic guards."""

        if self._address != 0:
            return load_i64(int_to_ptr(self._address), index * 8)
        return self._values[index]

    def get2_unchecked(self, record_index: int) -> CompilerInt2:
        index = record_index * 2
        if self._address != 0:
            address = int_to_ptr(self._address)
            return CompilerInt2(
                load_i64(address, index * 8),
                load_i64(address, (index + 1) * 8),
            )
        return CompilerInt2(self._values[index], self._values[index + 1])

    def get3_unchecked(self, record_index: int) -> CompilerInt3:
        index = record_index * 3
        if self._address != 0:
            address = int_to_ptr(self._address)
            return CompilerInt3(
                load_i64(address, index * 8),
                load_i64(address, (index + 1) * 8),
                load_i64(address, (index + 2) * 8),
            )
        return CompilerInt3(
            self._values[index],
            self._values[index + 1],
            self._values[index + 2],
        )

    def get4_unchecked(self, record_index: int) -> CompilerInt4:
        index = record_index * 4
        if self._address != 0:
            address = int_to_ptr(self._address)
            return CompilerInt4(
                load_i64(address, index * 8),
                load_i64(address, (index + 1) * 8),
                load_i64(address, (index + 2) * 8),
                load_i64(address, (index + 3) * 8),
            )
        return CompilerInt4(
            self._values[index],
            self._values[index + 1],
            self._values[index + 2],
            self._values[index + 3],
        )

    def set(self, index: int, value: int) -> None:
        if self._address != 0:
            if index < 0 or index >= self._length:
                raise IndexError("compiler int arena index out of range")
            store_i64(int_to_ptr(self._address), index * 8, value)
        else:
            if self._closed:
                raise RuntimeError("compiler int arena is closed")
            if index < 0 or index >= self._length:
                raise IndexError("compiler int arena index out of range")
            self._values[index] = value

    def set_unchecked(self, index: int, value: int) -> None:
        """Write one compiler-proven in-range index without semantic guards."""

        if self._address != 0:
            store_i64(int_to_ptr(self._address), index * 8, value)
        else:
            self._values[index] = value

    def set3_unchecked(
        self,
        index: int,
        first: int,
        second: int,
        third: int,
    ) -> None:
        """Overwrite three contiguous compiler-proven in-range scalars."""

        if self._address != 0:
            address = int_to_ptr(self._address)
            store_i64(address, index * 8, first)
            store_i64(address, (index + 1) * 8, second)
            store_i64(address, (index + 2) * 8, third)
        else:
            self._values[index] = first
            self._values[index + 1] = second
            self._values[index + 2] = third

    def set2_unchecked(self, index: int, first: int, second: int) -> None:
        """Overwrite two contiguous compiler-proven in-range scalars."""

        if self._address != 0:
            address = int_to_ptr(self._address)
            store_i64(address, index * 8, first)
            store_i64(address, (index + 1) * 8, second)
        else:
            self._values[index] = first
            self._values[index + 1] = second

    def clear(self) -> None:
        """Reuse allocated capacity while dropping every logical scalar."""

        if self._closed:
            raise RuntimeError("compiler int arena is closed")
        self._length = 0
        if self._address == 0:
            self._values.clear()

    def truncate(self, length: int) -> None:
        """Drop a proven suffix while retaining allocated native capacity."""

        if length < 0 or length > self._length:
            raise IndexError("compiler int arena truncate length out of range")
        if self._closed:
            raise RuntimeError("compiler int arena is closed")
        self._length = length
        if self._address == 0:
            del self._values[length:]

    def zero_prefix_unchecked(self, count: int) -> None:
        """Zero one compiler-proven prefix in a single arena call."""

        index = 0
        if self._address != 0:
            address = int_to_ptr(self._address)
            while index < count:
                store_i64(address, index * 8, 0)
                index += 1
            return
        while index < count:
            self._values[index] = 0
            index += 1

    def copy_prefix_from_unchecked(
        self,
        source: CompilerIntArena,
        source_start: int,
        count: int,
    ) -> None:
        """Copy a proven source span into this arena's prefix."""

        index = 0
        if self._address != 0:
            if source._address == 0:
                raise RuntimeError("compiler arena storage projections disagree")
            target_address = int_to_ptr(self._address)
            source_address = int_to_ptr(source._address)
            while index < count:
                store_i64(
                    target_address,
                    index * 8,
                    load_i64(source_address, (source_start + index) * 8),
                )
                index += 1
            return
        if source._address != 0:
            raise RuntimeError("compiler arena storage projections disagree")
        while index < count:
            self._values[index] = source._values[source_start + index]
            index += 1

    def or_prefix_from_unchecked(
        self,
        source: CompilerIntArena,
        source_start: int,
        count: int,
    ) -> None:
        """OR a proven source span into this arena's prefix."""

        index = 0
        if self._address != 0:
            if source._address == 0:
                raise RuntimeError("compiler arena storage projections disagree")
            target_address = int_to_ptr(self._address)
            source_address = int_to_ptr(source._address)
            while index < count:
                store_i64(
                    target_address,
                    index * 8,
                    load_i64(target_address, index * 8)
                    | load_i64(source_address, (source_start + index) * 8),
                )
                index += 1
            return
        if source._address != 0:
            raise RuntimeError("compiler arena storage projections disagree")
        while index < count:
            self._values[index] = (
                self._values[index] | source._values[source_start + index]
            )
            index += 1

    def converge_liveness_row_unchecked(
        self,
        uses: CompilerIntArena,
        definitions: CompilerIntArena,
        live_out: CompilerIntArena,
        live_in: CompilerIntArena,
        row_start: int,
        count: int,
        word_mask: int,
    ) -> bool:
        """Publish one liveness transfer row and report whether it changed."""

        changed = False
        index = 0
        if self._address != 0:
            if (
                uses._address == 0
                or definitions._address == 0
                or live_out._address == 0
                or live_in._address == 0
            ):
                raise RuntimeError("compiler arena storage projections disagree")
            scratch_address = int_to_ptr(self._address)
            uses_address = int_to_ptr(uses._address)
            definitions_address = int_to_ptr(definitions._address)
            live_out_address = int_to_ptr(live_out._address)
            live_in_address = int_to_ptr(live_in._address)
            while index < count:
                matrix_index = row_start + index
                outgoing = load_i64(scratch_address, index * 8)
                incoming = load_i64(uses_address, matrix_index * 8) | (
                    outgoing
                    & (
                        word_mask
                        ^ load_i64(definitions_address, matrix_index * 8)
                    )
                )
                if load_i64(live_out_address, matrix_index * 8) != outgoing:
                    store_i64(live_out_address, matrix_index * 8, outgoing)
                    changed = True
                if load_i64(live_in_address, matrix_index * 8) != incoming:
                    store_i64(live_in_address, matrix_index * 8, incoming)
                    changed = True
                index += 1
            return changed
        if (
            uses._address != 0
            or definitions._address != 0
            or live_out._address != 0
            or live_in._address != 0
        ):
            raise RuntimeError("compiler arena storage projections disagree")
        while index < count:
            matrix_index = row_start + index
            outgoing = self._values[index]
            incoming = uses._values[matrix_index] | (
                outgoing & (word_mask ^ definitions._values[matrix_index])
            )
            if live_out._values[matrix_index] != outgoing:
                live_out._values[matrix_index] = outgoing
                changed = True
            if live_in._values[matrix_index] != incoming:
                live_in._values[matrix_index] = incoming
                changed = True
            index += 1
        return changed

    def sort_nonnegative_radix(self) -> None:
        """Sort nonnegative machine-range keys in linear passes over raw bytes.

        The final Mach-O relocation order encodes (descending address,
        original index) into one nonnegative i64. Heapsort performs random
        loads O(N log N) on millions of those keys; an LSD radix pass keeps
        the exact numeric/stable-tie order with sequential raw accesses.
        """
        if self._closed:
            raise RuntimeError("compiler int arena is closed")
        length = self._length
        if length < 2:
            return
        if self._address == 0:
            for value in self._values:
                if value < 0:
                    raise ValueError("radix sort requires nonnegative keys")
            self._values.sort()
            return

        original = int_to_ptr(self._address)
        maximum = 0
        index = 0
        while index < length:
            value = load_i64(original, index * 8)
            if value < 0:
                raise ValueError("radix sort requires nonnegative keys")
            if value > maximum:
                maximum = value
            index += 1
        if maximum == 0:
            return

        scratch = malloc(length * 8)
        counts = malloc(256 * 8)
        if ptr_is_null(scratch) or ptr_is_null(counts):
            free(scratch)
            free(counts)
            raise MemoryError("compiler radix sort allocation failed")
        try:
            source = original
            destination = scratch
            shift = 0
            while (maximum >> shift) != 0:
                memset(counts, 0, 256 * 8)
                index = 0
                while index < length:
                    value = load_i64(source, index * 8)
                    bucket = (value >> shift) & 255
                    at = bucket * 8
                    store_i64(counts, at, load_i64(counts, at) + 1)
                    index += 1
                offset = 0
                bucket = 0
                while bucket < 256:
                    at = bucket * 8
                    count = load_i64(counts, at)
                    store_i64(counts, at, offset)
                    offset += count
                    bucket += 1
                index = 0
                while index < length:
                    value = load_i64(source, index * 8)
                    bucket = (value >> shift) & 255
                    at = bucket * 8
                    position = load_i64(counts, at)
                    store_i64(destination, position * 8, value)
                    store_i64(counts, at, position + 1)
                    index += 1
                previous = source
                source = destination
                destination = previous
                shift += 8
            if ptr_to_int(source) != self._address:
                memcpy(original, source, length * 8)
        finally:
            free(counts)
            free(scratch)

    def sort(self) -> None:
        """Sort the logical signed-i64 payload without object projection.

        CPython's list is only the semantic oracle, so its native sort keeps
        host pcc from paying a Python-level comparison loop.  A pcc-compiled
        arena always owns raw storage and executes the in-place heapsort below;
        no list or boxed record is reachable on that path.
        """

        length = self._length
        if length < 2:
            return
        if self._address == 0:
            if self._closed:
                raise RuntimeError("compiler int arena is closed")
            self._values.sort()
            return

        address = int_to_ptr(self._address)
        start = length // 2 - 1
        while start >= 0:
            root = start
            value = load_i64(address, root * 8)
            child = root * 2 + 1
            while child < length:
                right = child + 1
                if right < length and load_i64(
                    address, child * 8
                ) < load_i64(address, right * 8):
                    child = right
                child_value = load_i64(address, child * 8)
                if value >= child_value:
                    break
                store_i64(address, root * 8, child_value)
                root = child
                child = root * 2 + 1
            store_i64(address, root * 8, value)
            start -= 1

        end = length - 1
        while end > 0:
            tail_value = load_i64(address, end * 8)
            store_i64(address, end * 8, load_i64(address, 0))
            root = 0
            child = 1
            while child < end:
                right = child + 1
                if right < end and load_i64(
                    address, child * 8
                ) < load_i64(address, right * 8):
                    child = right
                child_value = load_i64(address, child * 8)
                if tail_value >= child_value:
                    break
                store_i64(address, root * 8, child_value)
                root = child
                child = root * 2 + 1
            store_i64(address, root * 8, tail_value)
            end -= 1

    def write_raw_fd(self, fd: int) -> None:
        """Write the native logical payload without scalar projection."""

        if self._closed or self._address == 0:
            raise RuntimeError("compiler arena has no native payload")
        total = self._length * 8
        offset = 0
        address = int_to_ptr(self._address)
        while offset < total:
            written = raw_write(fd, ptr_add(address, offset), total - offset)
            if written <= 0:
                raise OSError("compiler arena raw write failed")
            offset += written

    def read_raw_fd(self, fd: int, count: int) -> None:
        """Replace an empty native arena from exactly ``count`` i64 words."""

        if count < 0:
            raise ValueError("compiler arena raw count must be nonnegative")
        if self._closed or self._address == 0:
            raise RuntimeError("compiler arena has no native payload")
        if self._length != 0:
            raise RuntimeError("compiler arena raw read requires an empty arena")
        if count > self._capacity:
            self._grow(count)
        total = count * 8
        offset = 0
        address = int_to_ptr(self._address)
        while offset < total:
            consumed = raw_read(fd, ptr_add(address, offset), total - offset)
            if consumed <= 0:
                raise OSError("compiler arena raw read was truncated")
            offset += consumed
        self._length = count

    def pack_u32_bytes(self) -> bytes:
        """Pack proven uint32 words into one bytes allocation."""

        if self._closed:
            raise RuntimeError("compiler int arena is closed")
        if self._length == 0:
            return b""
        index = 0
        while index < self._length:
            value = self.get_unchecked(index)
            if value < 0 or value > 0xFFFFFFFF:
                raise ValueError("compiler word arena value is outside uint32")
            index += 1
        if self._address == 0:
            # Joining one bytes object per word retains both the entire
            # object list and join's per-item scratch beside the input arena.
            # Bound that scratch before joining the final compact payloads.
            chunks = []
            word_chunks = []
            host_pack_chunk_words = 1024
            index = 0
            while index < self._length:
                word_chunks.append(
                    self._values[index].to_bytes(4, "little")
                )
                index += 1
                if len(word_chunks) == host_pack_chunk_words:
                    chunks.append(b"".join(word_chunks))
                    word_chunks.clear()
            if word_chunks:
                chunks.append(b"".join(word_chunks))
            return b"".join(chunks)
        total = self._length * 4
        allocation = malloc(total)
        if ptr_is_null(allocation):
            raise MemoryError("compiler word arena pack allocation failed")
        index = 0
        while index < self._length:
            store_i32(
                allocation,
                index * 4,
                self.get_unchecked(index),
            )
            index += 1
        result = _py_bytes_new(allocation, total)
        free(allocation)
        if ptr_is_null(result):
            raise MemoryError("compiler word arena bytes allocation failed")
        return result

    def close(self) -> None:
        if self._closed:
            return
        if self._address != 0:
            free(int_to_ptr(self._address))
        self._address = 0
        self._capacity = 0
        self._length = 0
        self._values.clear()
        self._closed = True

    def diagnostic_values(self) -> list[int]:
        """Materialize the object projection only for tests/diagnostics."""

        if self._closed:
            raise RuntimeError("compiler int arena is closed")
        result: list[int] = []
        index = 0
        while index < self._length:
            result.append(self.get(index))
            index += 1
        return result


class CompilerRecordSpanArena:
    """Scope-owned integer sequences with identity-free, generation-checked keys.

    Span headers are mutable roots of immutable concatenation trees. Extending
    snapshots the source root, so later source appends and self-extension have
    ordinary sequence semantics. Nodes and traversal state contain only scalar
    IDs; the normal replay API never constructs Python per-record containers.
    """

    def __init__(self) -> None:
        self.nodes: CompilerIntArena = CompilerIntArena()
        try:
            self.spans: CompilerIntArena = CompilerIntArena()
        except Exception:
            self.nodes.close()
            raise
        self.generation: int = 1
        self.closed: bool = False
        self.projection_count: int = 0

    def _require_open(self) -> None:
        if self.closed:
            raise RuntimeError("compiler record span arena is closed")

    def _root(self, span: CompilerInt2) -> CompilerInt2:
        self._require_open()
        if span.second != self.generation:
            raise RuntimeError("compiler record span handle is stale")
        if span.first < 0 or span.first >= len(self.spans) // 2:
            raise IndexError("compiler record span handle is out of range")
        return self.spans.get2_unchecked(span.first)

    def new_span(self) -> CompilerInt2:
        self._require_open()
        index = len(self.spans) // 2
        self.spans.append2(-1, 0)
        return CompilerInt2(index, self.generation)

    def length(self, span: CompilerInt2) -> int:
        root: CompilerInt2 = self._root(span)
        return root.second

    def append(self, span: CompilerInt2, record_id: int) -> None:
        root: CompilerInt2 = self._root(span)
        if record_id < 0:
            raise ValueError("compiler record id must be nonnegative")
        if root.second >= 0x7FFFFFFF:
            raise OverflowError("compiler record span exceeds its length limit")
        leaf = len(self.nodes) // 3
        self.nodes.append3(-1, record_id, 1)
        next_root = leaf
        if root.first >= 0:
            next_root = len(self.nodes) // 3
            self.nodes.append3(root.first, leaf, root.second + 1)
        self.spans.set2_unchecked(span.first * 2, next_root, root.second + 1)

    def extend(self, destination: CompilerInt2, source: CompilerInt2) -> None:
        left: CompilerInt2 = self._root(destination)
        right: CompilerInt2 = self._root(source)
        if right.second == 0:
            return
        if left.second > 0x7FFFFFFF - right.second:
            raise OverflowError("compiler record span exceeds its length limit")
        next_root = right.first
        if left.first >= 0:
            next_root = len(self.nodes) // 3
            self.nodes.append3(left.first, right.first, left.second + right.second)
        self.spans.set2_unchecked(
            destination.first * 2, next_root, left.second + right.second,
        )

    def start_cursor(self, span: CompilerInt2, cursor: CompilerIntArena) -> None:
        root: CompilerInt2 = self._root(span)
        cursor.clear()
        cursor.append(self.generation)
        if root.first >= 0:
            cursor.append(root.first)

    def next_record(self, cursor: CompilerIntArena) -> int:
        self._require_open()
        if len(cursor) < 1 or cursor.get_unchecked(0) != self.generation:
            raise RuntimeError("compiler record span cursor is stale")
        while len(cursor) > 1:
            index = len(cursor) - 1
            node_id = cursor.get_unchecked(index)
            cursor.truncate(index)
            if node_id < 0 or node_id >= len(self.nodes) // 3:
                raise RuntimeError("compiler record span node is out of range")
            node: CompilerInt3 = self.nodes.get3_unchecked(node_id)
            if node.first == -1:
                if node.second < 0 or node.third != 1:
                    raise RuntimeError("compiler record span leaf is invalid")
                return node.second
            if (
                node.first < 0 or node.first >= node_id
                or node.second < 0 or node.second >= node_id
            ):
                raise RuntimeError("compiler record span tree is not acyclic")
            cursor.append2(node.second, node.first)
        return -1

    def reset(self) -> None:
        self._require_open()
        if self.generation >= 0x7FFFFFFF:
            raise OverflowError("compiler record span generation limit reached")
        self.nodes.clear()
        self.spans.clear()
        self.generation += 1

    def close(self) -> None:
        if self.closed:
            return
        self.nodes.close()
        self.spans.close()
        self.closed = True

    def diagnostic_values(self, span: CompilerInt2) -> list[int]:
        """Counted oracle projection; normal consumers replay into their owner."""
        self.projection_count += 1
        result: list[int] = []
        cursor = CompilerIntArena()
        try:
            self.start_cursor(span, cursor)
            value = self.next_record(cursor)
            while value >= 0:
                result.append(value)
                value = self.next_record(cursor)
            return result
        finally:
            cursor.close()


__all__ = [
    "CompilerInt2",
    "CompilerInt3",
    "CompilerInt4",
    "CompilerIntArena",
    "CompilerRecordSpanArena",
]
