"""Bounded deterministic parallel primitives for pcc's Mach-O linker.

LINK-P3-PARALLEL follows the useful part of mold's concurrency model: finish
semantic decisions and output layout first, then run data-parallel work whose
ownership is fixed before any worker starts.  This module deliberately does
not expose a shared append buffer or a scheduling-dependent work queue.

``ordered_parallel_map`` assigns stable contiguous input ranges to workers and
stores every result in its original index.  If several tasks fail, the lowest
input index is reported regardless of which thread happened to fail first.

``materialize_output`` validates a complete set of non-overlapping output
regions, preallocates the exact image size, splits large regions at fixed byte
boundaries, and lets workers write only their assigned slices.  Gaps are
zero-filled by construction.  Layout, ordering, and bytes therefore do not
depend on worker scheduling.  Unset configuration chooses bounded automatic
parallelism above a minimum work size; ``off`` forces serial execution and a
positive ``PCC_MACHO_LINK_JOBS`` value supplies an explicit worker bound.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import BinaryIO, Callable, Sequence, TypeVar, cast

PARALLEL_JOBS_ENV = "PCC_MACHO_LINK_JOBS"
_OUTER_PARALLELISM_ENV = "PCC_OUTER_PARALLELISM"

# The v50 source-frozen Stage2 input set proves eight host assembler workers at
# 4.84 GiB tree peak under the shared 8 GiB breaker, with byte-identical output;
# keep the independent linker class wider than multi-GiB pcc1 codegen workers.
_DEFAULT_MAX_JOBS = 8
_HARD_MAX_JOBS = 32
_DEFAULT_PARALLEL_MIN_BYTES = 256 * 1024
_OUTPUT_CHUNK_BYTES = 1024 * 1024
_FALSE_VALUES = frozenset({
    "0", "false", "no", "off", "disable", "disabled",
})

_InputT = TypeVar("_InputT")
_ResultT = TypeVar("_ResultT")


class ParallelLinkError(RuntimeError):
    """Parallel configuration or output ownership is invalid."""


@dataclass(frozen=True)
class OutputRegion:
    """One immutable byte contribution at an already-computed file offset."""

    offset: int
    data: bytes
    label: str = ""


@dataclass(frozen=True, order=True)
class SymbolDefinition:
    """Stable provenance for one externally visible definition."""

    input_index: int
    symbol_index: int


@dataclass(frozen=True)
class _OutputChunk:
    destination: int
    source: bytes
    source_start: int
    source_end: int


class ShardedSymbolDefinitions:
    """Concurrent symbol-definition collector with deterministic ownership.

    Names choose a shard through a process-independent hash.  Threads append
    provenance under only that shard's lock; lookup sorts provenance instead
    of treating insertion order as a winner, so scheduling can never select a
    different definition.
    """

    _SHARD_COUNT = 64

    def __init__(self) -> None:
        self._locks = [threading.Lock() for _index in range(self._SHARD_COUNT)]
        self._buckets: list[dict[str, list[SymbolDefinition]]] = [
            {} for _index in range(self._SHARD_COUNT)
        ]
        self._frozen = False

    @classmethod
    def _shard_index(cls, name: str) -> int:
        # FNV-1a over Unicode scalar values.  Python's randomized ``hash`` is
        # deliberately avoided because shard ownership is part of the proof.
        # Every consumer uses this value only modulo 64.  Multiplication mod
        # 2**64 keeps the low six bits equal to (low & 63) * (m & 63) mod 64,
        # so the full-width big-integer product is pure CPython overhead and
        # this accumulator reaches the identical shard with machine-word
        # arithmetic.  FNV prime 1099511628211 & 63 == 51.
        low = 0xCBF29CE484222325 & 63
        for character in name:
            low = ((low ^ ord(character)) * 51) & 63
        return low

    def add(self, name: str, definition: SymbolDefinition) -> None:
        if not isinstance(name, str) or not name:
            raise ParallelLinkError("symbol definition name must be non-empty text")
        if not isinstance(definition, SymbolDefinition):
            raise ParallelLinkError("symbol definition provenance is invalid")
        if (
            not isinstance(definition.input_index, int)
            or isinstance(definition.input_index, bool)
            or definition.input_index < 0
            or not isinstance(definition.symbol_index, int)
            or isinstance(definition.symbol_index, bool)
            or definition.symbol_index < 0
        ):
            raise ParallelLinkError("symbol definition indices must be non-negative")
        shard = self._shard_index(name)
        lock = self._locks[shard]
        with lock:
            if self._frozen:
                raise ParallelLinkError(
                    "symbol definitions are frozen for deterministic lookup"
                )
            self._buckets[shard].setdefault(name, []).append(definition)

    def freeze(self) -> None:
        """Close the parallel collection phase before any resolution begins.

        Acquiring every shard lock is the phase barrier: an add that already
        owns a shard completes first, while an add arriving afterwards sees
        ``_frozen`` and fails.  Resolution can therefore never observe a
        scheduling-dependent prefix of the definition set.
        """

        acquired: list[threading.Lock] = []
        try:
            for lock in self._locks:
                lock.acquire()
                acquired.append(lock)
            self._frozen = True
        finally:
            for lock in reversed(acquired):
                lock.release()

    def definitions(self, name: str) -> tuple[SymbolDefinition, ...]:
        if not isinstance(name, str) or not name:
            return ()
        shard = self._shard_index(name)
        lock = self._locks[shard]
        with lock:
            if not self._frozen:
                raise ParallelLinkError(
                    "symbol definitions must be frozen before lookup"
                )
            values = tuple(sorted(self._buckets[shard].get(name, ())))
        return values

    def owner(self, name: str) -> SymbolDefinition | None:
        definitions = self.definitions(name)
        return definitions[0] if definitions else None


def _validate_nonnegative_integer(value: object, context: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ParallelLinkError(f"{context} must be a non-negative integer")
    return value


def _requested_jobs_from_environment() -> int | None:
    raw = str(os.environ.get(PARALLEL_JOBS_ENV, "") or "").strip().lower()
    if not raw:
        return None
    if raw == "auto":
        return None
    if raw in _FALSE_VALUES:
        return 1
    if not raw.isdecimal() or int(raw) <= 0:
        raise ParallelLinkError(
            f"{PARALLEL_JOBS_ENV} must be 'auto', 'off', or a positive integer"
        )
    return int(raw)


def _automatic_cpu_budget() -> int:
    """Use this process's fair CPU share when pytest/another build is outermost."""

    cpu_count = max(1, os.cpu_count() or 1)
    raw = str(os.environ.get(_OUTER_PARALLELISM_ENV, "") or "").strip()
    try:
        outer_parallelism = int(raw) if raw else 1
    except ValueError:
        outer_parallelism = 1
    return max(1, cpu_count // max(1, outer_parallelism))


def resolve_link_jobs(
    task_count: int,
    total_bytes: int,
    *,
    requested: int | None = None,
) -> int:
    """Resolve a bounded worker count without making it part of output state."""

    task_count = _validate_nonnegative_integer(task_count, "task count")
    total_bytes = _validate_nonnegative_integer(total_bytes, "total byte count")
    configured = (
        _requested_jobs_from_environment()
        if requested is None else requested
    )
    if configured is not None:
        if (
            not isinstance(configured, int)
            or isinstance(configured, bool)
            or configured <= 0
        ):
            raise ParallelLinkError("requested link jobs must be a positive integer")
    if task_count <= 1:
        return 1
    if configured is None:
        if total_bytes < _DEFAULT_PARALLEL_MIN_BYTES:
            return 1
        configured = min(_automatic_cpu_budget(), _DEFAULT_MAX_JOBS)
    return max(1, min(task_count, configured, _HARD_MAX_JOBS))


def ordered_parallel_map(
    items: Sequence[_InputT],
    function: Callable[[_InputT], _ResultT],
    *,
    total_bytes: int = 0,
    jobs: int | None = None,
) -> list[_ResultT]:
    """Map independent inputs in parallel and return exact input order.

    Work ownership is a stable contiguous partition, not a shared queue.  The
    lowest failing input index is re-raised after every started worker joins,
    so diagnostics are also independent of scheduling.
    """

    # Freeze the input collection as well as its partition.  A caller mutating
    # a list while workers read it cannot make task ownership schedule-shaped.
    stable_items = tuple(items)
    count = len(stable_items)
    worker_count = resolve_link_jobs(
        count,
        total_bytes,
        requested=jobs,
    )
    if count == 0:
        return []
    if worker_count == 1:
        return [function(item) for item in stable_items]

    results: list[object] = [None] * count
    completed = [False] * count
    failures: list[BaseException | None] = [None] * count

    def run_worker(worker_index: int) -> None:
        begin = count * worker_index // worker_count
        end = count * (worker_index + 1) // worker_count
        for item_index in range(begin, end):
            try:
                results[item_index] = function(stable_items[item_index])
                completed[item_index] = True
            except BaseException as exc:
                failures[item_index] = exc
                break

    threads: list[threading.Thread] = []
    try:
        for worker_index in range(1, worker_count):
            thread = threading.Thread(
                target=run_worker,
                args=(worker_index,),
                name=f"pcc-macho-link-{worker_index}",
                daemon=False,
            )
            thread.start()
            threads.append(thread)
    except BaseException as exc:
        for thread in threads:
            thread.join()
        raise ParallelLinkError("failed to start a Mach-O link worker") from exc

    run_worker(0)
    for thread in threads:
        thread.join()

    for failure in failures:
        if failure is not None:
            raise failure
    if not all(completed):
        raise ParallelLinkError("Mach-O link worker returned without a result")
    return cast(list[_ResultT], results)


def _validated_regions(
    size: int,
    regions: Sequence[OutputRegion],
) -> list[OutputRegion]:
    size = _validate_nonnegative_integer(size, "output size")
    validated: list[OutputRegion] = []
    stable_regions = tuple(regions)
    for index, region in enumerate(stable_regions):
        if not isinstance(region, OutputRegion):
            raise ParallelLinkError(f"output region {index} has an invalid type")
        if (
            not isinstance(region.offset, int)
            or isinstance(region.offset, bool)
            or region.offset < 0
        ):
            raise ParallelLinkError(
                f"output region {index} has an invalid offset"
            )
        if not isinstance(region.data, bytes):
            raise ParallelLinkError(
                f"output region {index} payload must be bytes"
            )
        if not isinstance(region.label, str):
            raise ParallelLinkError(
                f"output region {index} label must be text"
            )
        end = region.offset + len(region.data)
        if end > size:
            label = region.label or str(index)
            raise ParallelLinkError(
                f"output region {label!r} ends past the image size"
            )
        if region.data:
            validated.append(region)

    validated.sort(
        key=lambda region: (region.offset, len(region.data), region.label)
    )
    previous_end = 0
    previous_label = "image start"
    for index, region in enumerate(validated):
        if region.offset < previous_end:
            label = region.label or str(index)
            raise ParallelLinkError(
                f"output region {label!r} overlaps {previous_label!r}"
            )
        previous_end = region.offset + len(region.data)
        previous_label = region.label or str(index)
    return validated


def _output_chunks(regions: Sequence[OutputRegion]) -> list[_OutputChunk]:
    chunks: list[_OutputChunk] = []
    for region in regions:
        for source_start in range(0, len(region.data), _OUTPUT_CHUNK_BYTES):
            source_end = min(
                source_start + _OUTPUT_CHUNK_BYTES,
                len(region.data),
            )
            chunks.append(_OutputChunk(
                destination=region.offset + source_start,
                source=region.data,
                source_start=source_start,
                source_end=source_end,
            ))
    return chunks


def _write_output_chunks(
    destination: bytearray,
    chunks: Sequence[_OutputChunk],
    *,
    total_bytes: int,
    jobs: int | None,
) -> None:
    def write_chunk(chunk: _OutputChunk) -> None:
        length = chunk.source_end - chunk.source_start
        start = chunk.destination
        destination[start:start + length] = chunk.source[
            chunk.source_start:chunk.source_end
        ]

    ordered_parallel_map(
        chunks,
        write_chunk,
        total_bytes=total_bytes,
        jobs=jobs,
    )


def materialize_output_buffer(
    size: int,
    regions: Sequence[OutputRegion],
    *,
    jobs: int | None = None,
    reserve: int = 0,
) -> bytearray:
    """Write a frozen layout into one mutable buffer and hand it over.

    ``reserve`` extends the buffer past ``size`` with zero bytes the caller
    fills afterwards -- the code signature goes there.  Returning the buffer
    instead of an immutable copy is what keeps a 200 MiB image from existing
    twice at the moment it is finalized.
    """

    validated = _validated_regions(size, regions)
    chunks = _output_chunks(validated)
    image = bytearray(size + reserve)
    _write_output_chunks(image, chunks, total_bytes=size, jobs=jobs)
    return image


def materialize_output(
    size: int,
    regions: Sequence[OutputRegion],
    *,
    jobs: int | None = None,
) -> bytes:
    """Write a frozen layout through deterministic, disjoint byte ownership."""

    return bytes(materialize_output_buffer(size, regions, jobs=jobs))


def write_file_output(
    file: BinaryIO,
    size: int,
    regions: Sequence[OutputRegion],
    *,
    jobs: int | None = None,
) -> None:
    """Patch disjoint regions through bounded POSIX positional writes.

    Validate layout, scheduling and descriptor flags before mutation. The
    caller retains the descriptor and its cursor; bytes outside the regions
    survive resizing, with newly extended gaps supplied as zeroes by the OS.
    The descriptor must be seekable and open for read/write without O_APPEND.
    Workers use the same frozen contiguous partition as buffer publication,
    with at most two 1 MiB slices per active worker and no image-sized copy.
    """
    import fcntl

    validated = _validated_regions(size, regions)
    chunks = _output_chunks(validated)
    worker_count = resolve_link_jobs(len(chunks), size, requested=jobs)
    try:
        descriptor = file.fileno()
        flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
        if flags & os.O_APPEND:
            raise ParallelLinkError("file-backed output cannot use O_APPEND")
        if flags & os.O_ACCMODE != os.O_RDWR:
            raise ParallelLinkError("file-backed output requires a read/write descriptor")
        # Synchronize buffered seed bytes before resizing. In particular,
        # flushing after a shrink could otherwise put the old tail back.
        file.flush()
        os.ftruncate(descriptor, size)
    except (OSError, ValueError) as exc:
        raise ParallelLinkError(
            "could not prepare the file-backed Mach-O output file"
        ) from exc

    def write_chunk(chunk: _OutputChunk) -> None:
        payload = chunk.source[chunk.source_start:chunk.source_end]
        written = 0
        while written < len(payload):
            count = os.pwrite(descriptor, payload[written:], chunk.destination + written)
            if count <= 0 or count > len(payload) - written:
                raise ParallelLinkError("file-backed output write made invalid progress")
            written += count

    try:
        ordered_parallel_map(
            chunks,
            write_chunk,
            total_bytes=size,
            jobs=worker_count,
        )
        # Retain mmap.flush's synchronous publication boundary. The caller
        # still decides whether and how to atomically publish this inode.
        os.fsync(descriptor)
    except (OSError, ValueError) as exc:
        raise ParallelLinkError(
            "could not write the file-backed Mach-O output file"
        ) from exc


def write_mmap_output(
    file: BinaryIO,
    size: int,
    regions: Sequence[OutputRegion],
    *,
    jobs: int | None = None,
) -> None:
    """Compatibility entry for the owned positional file-region writer."""
    write_file_output(file, size, regions, jobs=jobs)


__all__ = [
    "OutputRegion",
    "PARALLEL_JOBS_ENV",
    "ParallelLinkError",
    "ShardedSymbolDefinitions",
    "SymbolDefinition",
    "materialize_output",
    "ordered_parallel_map",
    "resolve_link_jobs",
    "write_file_output",
    "write_mmap_output",
]
