"""Private aggregate payload retirement and bounded CPython section windows.

One anonymous file owns every section's detached chunks before the first
joined backing is allocated. The public/native preparation path is unchanged.
"""

from contextlib import contextmanager
import hashlib
import io
import os
import sys
import tempfile

from .macho_host_join import _BLOCK, _FIXED, _join_instrumentation_absent
from .precise_stackmap import PreciseStackMapError


class _StagedSectionPayloads:
    """Retire exact owned chunks before borrowing or transferring backings.

    Admission callbacks are borrowed and must admit the complete live graph,
    including the requested backing and any cumulative public-copy charge.
    A window's consumer must retire every bytes/view/iterator alias before
    leaving it. Final transfers may consume descriptor indices in any order.
    """

    __slots__ = (
        "stream", "rows", "count", "appended", "consumed", "sealed",
        "budget", "logical_bytes", "read_bytes", "metadata_bytes", "_busy",
        "_replays", "_taken", "max_window_bytes", "public_copy_bytes",
        "active_bytes",
    )

    def __init__(self, count, compact_budget, admit):
        self.stream, self.rows, self._replays, self._taken = None, [], [], []
        try:
            if (type(count) is not int or count < 0 or compact_budget is None
                    or count > compact_budget.limit_bytes // 8):
                raise PreciseStackMapError("Mach-O staged payload descriptor budget exceeded")
            self.count, self.budget = count, compact_budget
            self.appended = self.consumed = self.logical_bytes = self.read_bytes = 0
            self.max_window_bytes = self.public_copy_bytes = self.active_bytes = 0
            self.sealed = self._busy = False
            rounded = compact_budget._rounded
            integer = max(compact_budget.integer_bytes,
                          rounded(sys.getsizeof((1 << 64) - 1)))
            # Fixed descriptor, replay and consumed pointer arrays; descriptors
            # own offset/size/part-count integers and a digest. Replay counters
            # add one bounded integer per section; consumed flags are shared
            # bools. Fixed workspace includes file/hash/BytesIO/view headers.
            self.metadata_bytes = (
                rounded(sys.getsizeof(self)) + 3 * rounded(sys.getsizeof([]))
                + 3 * rounded(8 * count) + count * (
                    rounded(sys.getsizeof((0, 0, 0, b"")))
                    + 4 * integer + rounded(sys.getsizeof(b"") + 32)
                ) + _FIXED
            )
            admit(self.metadata_bytes, label="Mach-O staged payload descriptors")
            if not _join_instrumentation_absent():
                raise PreciseStackMapError("Mach-O staged payloads require an uninstrumented host")
            self.rows = [None] * count
            self._replays = [0] * count
            self._taken = [False] * count
            self.stream = tempfile.TemporaryFile(mode="w+b", buffering=0)
        except BaseException as exc:
            self._abort(exc)
            if isinstance(exc, OSError):
                raise PreciseStackMapError("Mach-O staged payload storage failed: " + str(exc)) from exc
            raise
        finally:
            admit = None

    def write_owned_parts(self, parts):
        """Retire one section's chunks without constructing a joined backing."""
        payload = source = part = digest = None
        try:
            if (self.stream is None or self.sealed or self._busy
                    or self.appended >= self.count or type(parts) is not list):
                raise PreciseStackMapError("Mach-O staged payload write state changed")
            if not _join_instrumentation_absent():
                raise PreciseStackMapError("Mach-O staged payloads require an uninstrumented host")
            size = retired = 0
            # Explicit strong locals also work on borrowing CPython 3.14+.
            # Empty/one-byte bytes may be immortal shared singletons; only
            # their bounded per-part ledger charge is retired in that case.
            for index in range(len(parts)):
                payload = [parts[index]].pop()
                if type(payload) is not bytes:
                    raise PreciseStackMapError("Mach-O staged payload requires exact bytes parts")
                if len(payload) > 1 and sys.getrefcount([payload].pop()) != 3:
                    raise PreciseStackMapError("Mach-O staged payload requires exclusively owned bytes")
                size += len(payload)
                retired += self.budget._rounded(sys.getsizeof(b"") + len(payload))
                payload = None
            if size > self.budget.limit_bytes - self.logical_bytes:
                raise PreciseStackMapError("Mach-O staged payload storage budget exceeded")
            offset, part_count = self.logical_bytes, len(parts)
            if (os.fstat(self.stream.fileno()).st_size != offset
                    or self.stream.seek(offset) != offset):
                raise PreciseStackMapError("Mach-O staged payload extent changed")
            digest = hashlib.sha256()
            written = 0
            for index in range(len(parts)):
                payload = [parts[index]].pop()
                parts[index] = None
                if len(payload) > 1 and sys.getrefcount([payload].pop()) != 2:
                    raise PreciseStackMapError("Mach-O staged payload requires exclusively owned bytes")
                digest.update(payload)
                with memoryview(payload) as source:
                    cursor = 0
                    while cursor < len(payload):
                        with source[cursor:min(len(payload), cursor + _BLOCK)] as part:
                            amount = self.stream.write(part)
                            capacity = len(part)
                        if type(amount) is not int or not 0 < amount <= capacity:
                            raise PreciseStackMapError("short Mach-O staged payload write")
                        cursor += amount
                        written += amount
                if not _join_instrumentation_absent():
                    raise PreciseStackMapError("Mach-O staged payload instrumentation changed")
                if len(payload) > 1 and sys.getrefcount([payload].pop()) != 2:
                    raise PreciseStackMapError("Mach-O staged payload requires exclusively owned bytes")
                payload = None
            parts.clear()
            if written != size or os.fstat(self.stream.fileno()).st_size != offset + size:
                raise PreciseStackMapError("Mach-O staged payload extent changed")
            if not _join_instrumentation_absent():
                raise PreciseStackMapError("Mach-O staged payload instrumentation changed")
            self.rows[self.appended] = (offset, size, part_count, digest.digest())
            self.appended += 1
            self.logical_bytes += size
            self.max_window_bytes = max(
                self.max_window_bytes, self.budget._rounded(sys.getsizeof(b"") + size),
            )
            return retired
        except BaseException as exc:
            self._abort(exc)
            if isinstance(exc, OSError):
                raise PreciseStackMapError("Mach-O staged payload storage failed: " + str(exc)) from exc
            raise
        finally:
            if type(parts) is list:
                parts.clear()
            parts = payload = source = part = digest = None

    def descriptor(self, index):
        if (self.stream is None or not self.sealed or type(index) is not int
                or not 0 <= index < self.count or len(self.rows) != self.count):
            raise PreciseStackMapError("Mach-O staged payload read state changed")
        row = self.rows[index]
        if type(row) is not tuple or len(row) != 4:
            raise PreciseStackMapError("Mach-O staged payload descriptor changed")
        offset, size, part_count, digest = row
        if (any(type(value) is not int for value in (offset, size, part_count))
                or offset < 0 or size < 0 or part_count < 0
                or offset > self.logical_bytes or size > self.logical_bytes - offset
                or type(digest) is not bytes or len(digest) != 32):
            raise PreciseStackMapError("Mach-O staged payload extent changed")
        if ((index == 0 and offset != 0)
                or (index and offset != self.rows[index - 1][0] + self.rows[index - 1][1])
                or (index + 1 == self.count and offset + size != self.logical_bytes)
                or (index + 1 < self.count and offset + size != self.rows[index + 1][0])):
            raise PreciseStackMapError("Mach-O staged payload offset changed")
        return row

    def size(self, index):
        return self.descriptor(index)[1]

    def backing_bytes(self, index):
        return self.budget._rounded(sys.getsizeof(b"") + self.size(index))

    def _available(self, index):
        row = self.descriptor(index)
        if (len(self._taken) != self.count or len(self._replays) != self.count
                or self._taken[index] is not False):
            raise PreciseStackMapError("Mach-O staged payload index already consumed")
        return row

    def seal(self):
        try:
            if (self.stream is None or self.sealed or self._busy
                    or self.appended != self.count
                    or os.fstat(self.stream.fileno()).st_size != self.logical_bytes
                    or self.stream.seek(0, 2) != self.logical_bytes):
                raise PreciseStackMapError("Mach-O staged payload extent changed")
            self.sealed = True
            for index in range(self.count):
                self.descriptor(index)
            if not _join_instrumentation_absent():
                raise PreciseStackMapError("Mach-O staged payload instrumentation changed")
            return self
        except BaseException as exc:
            self._abort(exc)
            if isinstance(exc, OSError):
                raise PreciseStackMapError("Mach-O staged payload storage failed: " + str(exc)) from exc
            raise

    def _read(self, index, check, patch):
        """Read original file bytes once; patch only this fresh borrowed view."""
        buffer = view = part = result = closing = failure = None
        try:
            offset, size, part_count, digest = self._available(index)
            backing = self.budget._rounded(sys.getsizeof(b"") + size)
            if backing > self.max_window_bytes:
                raise PreciseStackMapError("Mach-O staged payload backing changed")
            check()
            if not _join_instrumentation_absent():
                raise PreciseStackMapError("Mach-O staged payloads require an uninstrumented host")
            if (self._available(index) != (offset, size, part_count, digest)
                    or os.fstat(self.stream.fileno()).st_size != self.logical_bytes
                    or self.stream.seek(0, 2) != self.logical_bytes
                    or self.stream.seek(offset) != offset):
                raise PreciseStackMapError("Mach-O staged payload extent changed")
            if not _join_instrumentation_absent():
                raise PreciseStackMapError("Mach-O staged payload instrumentation changed")
            self.active_bytes = backing
            buffer = io.BytesIO(b"\0" * size)
            view = buffer.getbuffer()
            cursor = 0
            while cursor < size:
                with view[cursor:min(size, cursor + _BLOCK)] as part:
                    count = self.stream.readinto(part)
                    capacity = len(part)
                if type(count) is not int or not 0 < count <= capacity:
                    raise PreciseStackMapError("short Mach-O staged payload read")
                cursor += count
            if os.fstat(self.stream.fileno()).st_size != self.logical_bytes:
                raise PreciseStackMapError("Mach-O staged payload extent changed")
            if hashlib.sha256(view).digest() != digest:
                raise PreciseStackMapError("Mach-O staged payload content changed")
            if not _join_instrumentation_absent():
                raise PreciseStackMapError("Mach-O staged payload instrumentation changed")
            if patch is not None:
                patch(view)
            if not _join_instrumentation_absent():
                raise PreciseStackMapError("Mach-O staged payload instrumentation changed")
            view.release()
            view = None
            buffer.truncate(size)  # escaped child exports must fail, never copy
            result = buffer.getvalue()
            closing, buffer = buffer, None
            try:
                closing.close()
            finally:
                closing = None
            if not _join_instrumentation_absent():
                raise PreciseStackMapError("Mach-O staged payload instrumentation changed")
            if (type(result) is not bytes or len(result) != size
                    or (size > 1 and sys.getrefcount([result].pop()) != 2)):
                raise PreciseStackMapError("Mach-O staged payload requires exclusively owned bytes")
            self.read_bytes += size
            return result
        except BaseException as exc:
            failure = exc
            self._abort(exc)
            if isinstance(exc, OSError):
                raise PreciseStackMapError("Mach-O staged payload storage failed: " + str(exc)) from exc
            raise
        finally:
            try:
                if view is not None:
                    view.release()
                if buffer is not None:
                    buffer.close()
            except BaseException:
                if failure is None:
                    raise
            finally:
                buffer = view = part = result = closing = patch = check = failure = None

    @contextmanager
    def window(self, index, *, check, patch=None):
        """Borrow one of at most eight exact immutable replays per descriptor."""
        payload = failure = None
        retired, acquired = True, False
        try:
            if self._busy:
                raise PreciseStackMapError("Mach-O staged payload window is already active")
            self._busy = acquired = True
            self._available(index)
            if self._replays[index] >= 8:
                raise PreciseStackMapError("Mach-O staged payload replay budget exceeded")
            self._replays[index] += 1
            retired = False
            payload = self._read(index, check, patch)
            check = patch = None
            try:
                yield payload
            except BaseException as exc:
                failure = exc
                raise
            finally:
                # Only this window's failure may suppress a secondary alias
                # error. An unrelated exception handled by its caller cannot.
                if failure is not None:
                    from .macho_host_prepare import _release_closed_tracebacks
                    _release_closed_tracebacks(failure)
                uninstrumented = _join_instrumentation_absent()
                exclusive = uninstrumented and (
                    len(payload) <= 1 or sys.getrefcount([payload].pop()) == 2
                )
                retired = exclusive
                if failure is None:
                    if not uninstrumented:
                        raise PreciseStackMapError("Mach-O staged payload instrumentation changed")
                    if not exclusive:
                        raise PreciseStackMapError("Mach-O staged payload window has escaped aliases")
        except BaseException as exc:
            if acquired:
                self._abort(exc)
            raise
        finally:
            payload = check = patch = failure = None
            if acquired:
                if retired:
                    self.active_bytes = 0
                self._busy = False

    def copy(self, index, *, check, patch=None):
        """Publish a fresh backing and keep its cumulative allocation charge."""
        payload = None
        acquired = False
        try:
            if self._busy:
                raise PreciseStackMapError("Mach-O staged payload window is already active")
            self._busy = acquired = True
            payload = self._read(index, check, patch)
            self.public_copy_bytes += self.active_bytes
            self.active_bytes = 0
            return payload
        except BaseException as exc:
            if acquired:
                self._abort(exc)
            raise
        finally:
            payload = check = patch = None
            if acquired:
                self._busy = False

    def take(self, index, *, check, patch=None):
        """Transfer a backing once by index, after admitting its retained owner.

        Prior public copies disqualify exclusive consumption of this store.
        Replay windows do not: each read starts with the original file bytes.
        """
        payload = None
        acquired = False
        try:
            if self._busy:
                raise PreciseStackMapError("Mach-O staged payload window is already active")
            self._busy = acquired = True
            if self.public_copy_bytes:
                raise PreciseStackMapError("Mach-O staged payload public copies prevent exclusive consume")
            payload = self._read(index, check, patch)
            self._taken[index] = True
            self.consumed += 1
            self.active_bytes = 0
            return payload
        except BaseException as exc:
            if acquired:
                self._abort(exc)
            raise
        finally:
            payload = check = patch = None
            if acquired:
                self._busy = False

    def read_owned(self, index, ledger, *, patch=None):
        """Transfer one arbitrary descriptor, retaining its ledger charge."""
        try:
            return self.take(index, check=lambda: ledger.keep(
                self.backing_bytes(index), label="Mach-O staged payload backing",
            ), patch=patch)
        finally:
            ledger = patch = None

    def _abort(self, error):
        from .macho_host_prepare import _release_closed_tracebacks
        try:
            _release_closed_tracebacks(error)
        finally:
            self.close(_error=error)

    def close(self, *, _error=None):
        stream, self.stream = self.stream, None
        self.rows.clear()
        self._replays.clear()
        self._taken.clear()
        try:
            if stream is not None:
                stream.close()
        except BaseException as exc:
            if _error is None:
                if isinstance(exc, OSError):
                    raise PreciseStackMapError("Mach-O staged payload storage failed: " + str(exc)) from exc
                raise
        finally:
            stream = _error = None
