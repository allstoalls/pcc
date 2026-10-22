"""Private consuming joins for detached CPython Mach-O section/row parts."""

from contextlib import contextmanager
import hashlib
import io
import os
import sys
import tempfile

from .precise_stackmap import PreciseStackMapError


_BLOCK = 64 * 1024
_FIXED = 8192


def _join_instrumentation_absent():
    # Frame hooks can retain a BytesIO snapshot before getbuffer() and cause
    # an unbudgeted copy-on-write. The caller also checks its verified host
    # model. Keep the ordinary conservative path when instrumentation exists.
    return (sys.gettrace() is None and sys.getprofile() is None
            and all(sys.monitoring.get_tool(tool) is None for tool in range(6)))


def _consume_parts(parts, ledger, *, patch=None):
    """Retire input chunks before constructing one immutable joined backing.

    The private caller has cleared any other lists containing the chunks.
    ``patch`` is an internal section-target callback borrowing the writable
    view only during this call. Neither the view nor BytesIO owner escapes.
    """
    if type(parts) is not list or any(type(part) is not bytes for part in parts):
        raise PreciseStackMapError("consuming Mach-O join requires owned bytes parts")
    buffer = view = payload = result = closing = stream = source = part = None
    fixed_kept = False
    written = read = 0
    try:
        if not _join_instrumentation_absent():
            raise PreciseStackMapError("consuming Mach-O join requires an uninstrumented host")
        total = sum(map(len, parts))
        if total > ledger.budget.limit_bytes:
            raise PreciseStackMapError("Mach-O consuming join storage budget exceeded")
        # All input bytes are still in ledger.retained. Only the fixed file/view
        # workspace is added now; the joined backing is admitted after retirement.
        ledger.keep(_FIXED, label="Mach-O consuming join workspace")
        fixed_kept = True
        with tempfile.TemporaryFile(mode="w+b", buffering=0) as stream:
            for index in range(len(parts)):
                payload = parts[index]
                size = len(payload)
                if size > total - written:
                    raise PreciseStackMapError("Mach-O consuming join extent changed")
                with memoryview(payload) as source:
                    cursor = 0
                    while cursor < size:
                        with source[cursor:min(size, cursor + _BLOCK)] as part:
                            count = stream.write(part)
                            capacity = len(part)
                        if type(count) is not int or not 0 < count <= capacity:
                            raise PreciseStackMapError("short Mach-O consuming join write")
                        cursor += count
                        written += count
                parts[index] = None
                payload = None
                ledger.retained -= ledger.immutable(size)
            parts.clear()
            if written != total or stream.seek(0, 2) != total or stream.seek(0) != 0:
                raise PreciseStackMapError("Mach-O consuming join extent changed")
            ledger.keep(ledger.immutable(total), label="Mach-O consuming join backing")
            if not _join_instrumentation_absent():
                raise PreciseStackMapError("consuming Mach-O join instrumentation changed")
            # No input chunk, source view or immutable output snapshot remains
            # at the allocation/export boundary. The exact temporary bytes
            # expression is transferred to BytesIO, as in the verified reader.
            buffer = io.BytesIO(b"\0" * total)
            view = buffer.getbuffer()
            while read < total:
                with view[read:min(total, read + _BLOCK)] as part:
                    count = stream.readinto(part)
                    capacity = len(part)
                if type(count) is not int or not 0 < count <= capacity:
                    raise PreciseStackMapError("short Mach-O consuming join read")
                read += count
        # Close storage before patching/freezing. A close failure cannot publish
        # a payload. Every byte has been written/read exactly once on success.
        if patch is not None:
            patch(view)
        if not _join_instrumentation_absent():
            raise PreciseStackMapError("consuming Mach-O join instrumentation changed")
        view.release()
        view = None
        buffer.truncate(total)  # reject any escaped child export, never copy
        result = buffer.getvalue()
        closing, buffer = buffer, None
        try:
            closing.close()
        finally:
            closing = None
        return result
    except BaseException as exc:
        # Closed callbacks may retain an exported child view in their traceback.
        # Retire those frames before closing the backing. The local import avoids
        # a module-initialization cycle with this helper's sole production caller.
        from .macho_host_prepare import _release_closed_tracebacks
        _release_closed_tracebacks(exc)
        if isinstance(exc, OSError):
            raise PreciseStackMapError("Mach-O consuming join storage failed: " + str(exc)) from exc
        raise
    finally:
        try:
            if view is not None:
                view.release()
        finally:
            try:
                if buffer is not None:
                    buffer.close()
            finally:
                parts.clear()
                buffer = view = payload = result = closing = patch = stream = source = part = None
                if fixed_kept:
                    ledger.retained -= _FIXED


class _StagedRelocationRows:
    """Private, bounded section-row replays from one anonymous file.

    Admission callbacks are borrowed, never retained. The consumer must clear
    every payload alias and close its row generators before leaving a window.
    Public copies instead retain a conservative cumulative allocation charge.
    """

    __slots__ = (
        "stream", "rows", "count", "appended", "sealed", "budget", "_busy",
        "_replays", "logical_bytes", "metadata_bytes", "max_window_bytes",
        "public_copy_bytes", "active_bytes",
    )

    def __init__(self, count, compact_budget, admit):
        self.stream, self.rows, self._replays = None, [], []
        try:
            if (type(count) is not int or count < 0 or compact_budget is None
                    or count > compact_budget.limit_bytes // 8):
                raise PreciseStackMapError("Mach-O staged row descriptor budget exceeded")
            self.count, self.budget = count, compact_budget
            self.appended = self.logical_bytes = self.max_window_bytes = 0
            self.public_copy_bytes = self.active_bytes = 0
            self.sealed = self._busy = False
            rounded = compact_budget._rounded
            integer = max(compact_budget.integer_bytes,
                          rounded(sys.getsizeof((1 << 64) - 1)))
            # Preallocated descriptor and replay pointer arrays; each descriptor
            # owns three bounded integers and one 32-byte digest. Fixed workspace
            # includes the file, hash state, BytesIO and bounded exported views.
            self.metadata_bytes = (
                rounded(sys.getsizeof(self)) + 2 * rounded(sys.getsizeof([]))
                + 2 * rounded(8 * count) + count * (
                    rounded(sys.getsizeof((0, 0, 0, b"")))
                    + 4 * integer + rounded(sys.getsizeof(b"") + 32)
                ) + _FIXED
            )
            compact_budget.check_peak(self.metadata_bytes, label="Mach-O staged row descriptors")
            admit(self.metadata_bytes, label="Mach-O staged row descriptors")
            self.rows = [None] * count
            self._replays = [0] * count
            self.stream = tempfile.TemporaryFile(mode="w+b", buffering=0)
        except BaseException as exc:
            self.close(_error=exc)
            if isinstance(exc, OSError):
                raise PreciseStackMapError("Mach-O staged row storage failed: " + str(exc)) from exc
            raise
        finally:
            admit = None

    def write_owned(self, payload):
        """Consume a sole exact-bytes argument; return its immutable charge."""
        from .native_object import (
            _FINAL_LINK_RELOCATION,
            _validate_final_link_relocation_count,
        )
        # Explicit strong references are required on CPython 3.14+ too; a
        # direct getrefcount local load may otherwise borrow the reference.
        payload = [payload].pop()
        source = part = None
        try:
            if (self.stream is None or self.sealed or self._busy
                    or self.appended >= self.count or type(payload) is not bytes):
                raise PreciseStackMapError("Mach-O staged row write state changed")
            if not _join_instrumentation_absent():
                raise PreciseStackMapError("Mach-O staged rows require an uninstrumented host")
            if payload and sys.getrefcount([payload].pop()) != 2:
                raise PreciseStackMapError("Mach-O staged rows require exclusively owned bytes")
            size = len(payload)
            if size % _FINAL_LINK_RELOCATION.size:
                raise PreciseStackMapError("Mach-O staged row extent changed")
            count = size // _FINAL_LINK_RELOCATION.size
            _validate_final_link_relocation_count(count)
            backing = self.budget._rounded(sys.getsizeof(b"") + size)
            if backing > self.budget.limit_bytes or size > 0x7FFFFFFFFFFFFFFF - self.logical_bytes:
                raise PreciseStackMapError("Mach-O staged row storage budget exceeded")
            offset = self.logical_bytes
            if (os.fstat(self.stream.fileno()).st_size != offset
                    or self.stream.seek(offset) != offset):
                raise PreciseStackMapError("Mach-O staged row extent changed")
            digest = hashlib.sha256(payload).digest()
            with memoryview(payload) as source:
                cursor = 0
                while cursor < size:
                    with source[cursor:min(size, cursor + _BLOCK)] as part:
                        written = self.stream.write(part)
                        capacity = len(part)
                    if type(written) is not int or not 0 < written <= capacity:
                        raise PreciseStackMapError("short Mach-O staged row write")
                    cursor += written
            if not _join_instrumentation_absent():
                raise PreciseStackMapError("Mach-O staged row instrumentation changed")
            if payload and sys.getrefcount([payload].pop()) != 2:
                raise PreciseStackMapError("Mach-O staged rows require exclusively owned bytes")
            self.rows[self.appended] = (offset, size, count, digest)
            self.appended += 1
            self.logical_bytes += size
            self.max_window_bytes = max(self.max_window_bytes, backing)
            return backing
        except BaseException as exc:
            self._abort(exc)
            if isinstance(exc, OSError):
                raise PreciseStackMapError("Mach-O staged row storage failed: " + str(exc)) from exc
            raise
        finally:
            payload = source = part = None

    def write_owned_parts(self, parts):
        """Consume ordered, record-aligned chunks without a joined backing.

        Return their retired immutable charge and whether the section needs
        target patching. Only the private preparation path transfers this list;
        all other part tables must already have released their references.
        """
        from .macho_spec import ARM64_RELOC_ADDEND
        from .native_object import (
            _FINAL_LINK_RELOCATION,
            _validate_final_link_relocation_count,
        )
        payload = source = part = digest = None
        try:
            if (self.stream is None or self.sealed or self._busy
                    or self.appended >= self.count or type(parts) is not list):
                raise PreciseStackMapError("Mach-O staged row write state changed")
            if not _join_instrumentation_absent():
                raise PreciseStackMapError("Mach-O staged rows require an uninstrumented host")
            size = retired = 0
            # Validate the complete transfer before writing or clearing slots.
            # A known strong local, one list slot and the sampled argument are
            # the only three references allowed, also on borrowing CPython.
            for index in range(len(parts)):
                payload = [parts[index]].pop()
                if type(payload) is not bytes or len(payload) % _FINAL_LINK_RELOCATION.size:
                    raise PreciseStackMapError("Mach-O staged row extent changed")
                if payload and sys.getrefcount([payload].pop()) != 3:
                    raise PreciseStackMapError("Mach-O staged rows require exclusively owned bytes")
                size += len(payload)
                retired += self.budget._rounded(sys.getsizeof(b"") + len(payload))
                payload = None
            count = size // _FINAL_LINK_RELOCATION.size
            _validate_final_link_relocation_count(count)
            backing = self.budget._rounded(sys.getsizeof(b"") + size)
            if backing > self.budget.limit_bytes or size > 0x7FFFFFFFFFFFFFFF - self.logical_bytes:
                raise PreciseStackMapError("Mach-O staged row storage budget exceeded")
            offset = self.logical_bytes
            if (os.fstat(self.stream.fileno()).st_size != offset
                    or self.stream.seek(offset) != offset):
                raise PreciseStackMapError("Mach-O staged row extent changed")
            digest = hashlib.sha256()
            has_targets = False
            written = 0
            for index in range(len(parts)):
                payload = [parts[index]].pop()
                parts[index] = None
                if payload and sys.getrefcount([payload].pop()) != 2:
                    raise PreciseStackMapError("Mach-O staged rows require exclusively owned bytes")
                if not has_targets:
                    has_targets = any(not row[4] and row[5] != ARM64_RELOC_ADDEND
                                      for row in _FINAL_LINK_RELOCATION.iter_unpack(payload))
                digest.update(payload)
                with memoryview(payload) as source:
                    cursor = 0
                    while cursor < len(payload):
                        with source[cursor:min(len(payload), cursor + _BLOCK)] as part:
                            amount = self.stream.write(part)
                            capacity = len(part)
                        if type(amount) is not int or not 0 < amount <= capacity:
                            raise PreciseStackMapError("short Mach-O staged row write")
                        cursor += amount
                        written += amount
                if not _join_instrumentation_absent():
                    raise PreciseStackMapError("Mach-O staged row instrumentation changed")
                if payload and sys.getrefcount([payload].pop()) != 2:
                    raise PreciseStackMapError("Mach-O staged rows require exclusively owned bytes")
                payload = None
            parts.clear()
            if written != size or os.fstat(self.stream.fileno()).st_size != offset + size:
                raise PreciseStackMapError("Mach-O staged row extent changed")
            if not _join_instrumentation_absent():
                raise PreciseStackMapError("Mach-O staged row instrumentation changed")
            self.rows[self.appended] = (offset, size, count, digest.digest())
            self.appended += 1
            self.logical_bytes += size
            self.max_window_bytes = max(self.max_window_bytes, backing)
            return retired, has_targets
        except BaseException as exc:
            self._abort(exc)
            if isinstance(exc, OSError):
                raise PreciseStackMapError("Mach-O staged row storage failed: " + str(exc)) from exc
            raise
        finally:
            if type(parts) is list:
                parts.clear()
            payload = source = part = digest = parts = None

    def _descriptor(self, index):
        from .native_object import _FINAL_LINK_RELOCATION
        if (self.stream is None or not self.sealed or type(index) is not int
                or not 0 <= index < self.count or len(self.rows) != self.count):
            raise PreciseStackMapError("Mach-O staged row read state changed")
        row = self.rows[index]
        if type(row) is not tuple or len(row) != 4:
            raise PreciseStackMapError("Mach-O staged row descriptor changed")
        offset, size, count, digest = row
        if (any(type(value) is not int for value in (offset, size, count))
                or offset < 0 or size < 0 or count < 0
                or size != count * _FINAL_LINK_RELOCATION.size
                or offset > self.logical_bytes or size > self.logical_bytes - offset
                or type(digest) is not bytes or len(digest) != 32):
            raise PreciseStackMapError("Mach-O staged row extent changed")
        if ((index == 0 and offset != 0)
                or (index and offset != self.rows[index - 1][0] + self.rows[index - 1][1])
                or (index + 1 == self.count and offset + size != self.logical_bytes)
                or (index + 1 < self.count and offset + size != self.rows[index + 1][0])):
            raise PreciseStackMapError("Mach-O staged row offset changed")
        return row

    def size(self, index):
        return self._descriptor(index)[1]

    def row_count(self, index):
        return self._descriptor(index)[2]

    def backing_bytes(self, index):
        return self.budget._rounded(sys.getsizeof(b"") + self.size(index))

    def seal(self):
        try:
            if (self.stream is None or self.sealed or self._busy
                    or self.appended != self.count
                    or os.fstat(self.stream.fileno()).st_size != self.logical_bytes
                    or self.stream.seek(0, 2) != self.logical_bytes):
                raise PreciseStackMapError("Mach-O staged row extent changed")
            self.sealed = True
            for index in range(self.count):
                self._descriptor(index)
            return self
        except BaseException as exc:
            self._abort(exc)
            if isinstance(exc, OSError):
                raise PreciseStackMapError("Mach-O staged row storage failed: " + str(exc)) from exc
            raise

    def _read(self, index, check):
        """Read a single backing while the caller owns the busy guard."""
        buffer = view = part = payload = closing = failure = None
        try:
            offset, size, _count, digest = self._descriptor(index)
            backing = self.budget._rounded(sys.getsizeof(b"") + size)
            if backing > self.max_window_bytes:
                raise PreciseStackMapError("Mach-O staged row backing changed")
            # The caller admits its complete live graph, including this
            # metadata and the pre-reserved single window. Adding them again
            # to budget.retained_bytes here would double-charge that owner.
            check()
            # User code can run at admission. Recheck both file and host before
            # constructing the only large in-memory allocation in this helper.
            if not _join_instrumentation_absent():
                raise PreciseStackMapError("Mach-O staged rows require an uninstrumented host")
            if (self._descriptor(index) != (offset, size, _count, digest)
                    or os.fstat(self.stream.fileno()).st_size != self.logical_bytes
                    or self.stream.seek(0, 2) != self.logical_bytes
                    or self.stream.seek(offset) != offset):
                raise PreciseStackMapError("Mach-O staged row extent changed")
            self.active_bytes = backing
            buffer = io.BytesIO(b"\0" * size)
            view = buffer.getbuffer()
            cursor = 0
            while cursor < size:
                with view[cursor:min(size, cursor + _BLOCK)] as part:
                    read = self.stream.readinto(part)
                    capacity = len(part)
                if type(read) is not int or not 0 < read <= capacity:
                    raise PreciseStackMapError("short Mach-O staged row read")
                cursor += read
            if os.fstat(self.stream.fileno()).st_size != self.logical_bytes:
                raise PreciseStackMapError("Mach-O staged row extent changed")
            if hashlib.sha256(view).digest() != digest:
                raise PreciseStackMapError("Mach-O staged row content changed")
            if not _join_instrumentation_absent():
                raise PreciseStackMapError("Mach-O staged row instrumentation changed")
            view.release()
            view = None
            buffer.truncate(size)  # escaped child exports must fail, never copy
            payload = buffer.getvalue()
            closing, buffer = buffer, None
            try:
                closing.close()
            finally:
                closing = None
            return payload
        except BaseException as exc:
            failure = exc
            self._abort(exc)
            if isinstance(exc, OSError):
                raise PreciseStackMapError("Mach-O staged row storage failed: " + str(exc)) from exc
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
                buffer = view = part = payload = closing = check = failure = None

    @contextmanager
    def window(self, index, *, check):
        payload = pending = None
        retired, acquired = True, False
        try:
            if self._busy:
                raise PreciseStackMapError("Mach-O staged row window is already active")
            self._busy = acquired = True
            self._descriptor(index)
            if self._replays[index] >= 8:
                raise PreciseStackMapError("Mach-O staged row replay budget exceeded")
            self._replays[index] += 1
            retired = False
            payload = self._read(index, check)
            check = None
            failure = None
            try:
                yield payload
            except BaseException as exc:
                failure = exc
                raise
            finally:
                # sys.exception() could be an unrelated exception already
                # handled by our caller. Only a failure thrown into this
                # particular window may suppress a secondary alias error.
                if failure is not None:
                    from .macho_host_prepare import _release_closed_tracebacks
                    _release_closed_tracebacks(failure)
                uninstrumented = _join_instrumentation_absent()
                exclusive = uninstrumented and (not payload or sys.getrefcount([payload].pop()) == 2)
                retired = exclusive
                if failure is None:
                    if not uninstrumented:
                        raise PreciseStackMapError("Mach-O staged row instrumentation changed")
                    if not exclusive:
                        raise PreciseStackMapError("Mach-O staged row window has escaped aliases")
                failure = None
        except BaseException as exc:
            if acquired:
                self._abort(exc)
            raise
        finally:
            payload = check = pending = None
            if acquired:
                if retired:
                    self.active_bytes = 0
                self._busy = False

    def copy(self, index, *, check):
        payload = None
        acquired = False
        try:
            if self._busy:
                raise PreciseStackMapError("Mach-O staged row window is already active")
            self._busy = acquired = True
            payload = self._read(index, check)
            self.public_copy_bytes += self.active_bytes
            self.active_bytes = 0
            return payload
        finally:
            payload = check = None
            if acquired:
                self._busy = False

    def _abort(self, error):
        from .macho_host_prepare import _release_closed_tracebacks
        try:
            _release_closed_tracebacks(error)
        finally:
            self.close(_error=error)

    def close(self, *, _error=None):
        """Release storage; only an explicitly supplied failure wins over close."""
        stream, self.stream = self.stream, None
        self.rows.clear()
        self._replays.clear()
        try:
            if stream is not None:
                stream.close()
        except BaseException as exc:
            if _error is None:
                if isinstance(exc, OSError):
                    raise PreciseStackMapError("Mach-O staged row storage failed: " + str(exc)) from exc
                raise
        finally:
            stream = _error = None
