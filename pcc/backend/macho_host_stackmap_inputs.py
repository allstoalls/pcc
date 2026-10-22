"""Private bounded storage for stack-map inputs detached from PCO windows.

Only descriptors survive between windows. The same anonymous, unbuffered
handle supplies each input exactly once; no pathname is reopened or mmap used.
The ordinary list-of-bytes merger and native path keep their own contracts.
"""

import io
import sys
import tempfile

from .precise_stackmap import PreciseStackMapError


_BLOCK = 64 * 1024
_BYTEARRAY_HEADER = sys.getsizeof(bytearray())


class _StagedStackMapInputs:
    __slots__ = (
        "stream", "rows", "count", "total", "written", "read", "appended",
        "consumed", "sealed", "metadata_bytes",
    )

    def __init__(self, count, total, *, compact_budget, admit):
        if (type(count) is not int or type(total) is not int
                or count <= 0 or total < 0 or total > compact_budget.limit_bytes):
            raise PreciseStackMapError("stack-map input staging work budget exceeded")
        self.stream, self.rows = None, []
        self.count, self.total = count, total
        self.written = self.read = self.appended = self.consumed = 0
        self.sealed = False
        rounded = compact_budget._rounded
        # Fixed-size pointer array, one pair and two bounded integers per
        # input. The fixed allowance covers FileIO and bounded view/IO scratch.
        self.metadata_bytes = (
            rounded(sys.getsizeof(self)) + rounded(sys.getsizeof([]))
            + rounded(8 * count) + count * (
                rounded(sys.getsizeof((0, 0)))
                + 2 * max(compact_budget.integer_bytes,
                          rounded(sys.getsizeof((1 << 64) - 1)))
            ) + 4096
        )
        admit(self.metadata_bytes, label="Mach-O staged stack-map descriptors")
        try:
            self.rows = [None] * count
            self.stream = tempfile.TemporaryFile(mode="w+b", buffering=0)
        except OSError as exc:
            self.rows.clear()
            raise PreciseStackMapError("stack-map input storage failed: " + str(exc)) from exc

    def __len__(self):
        return self.count

    def append(self, raw, start, size):
        if (self.stream is None or self.sealed or self.appended >= self.count
                or type(raw) is not bytes or type(start) is not int
                or type(size) is not int or start < 0 or size < 0
                or start > len(raw) or size > len(raw) - start
                or size > self.total - self.written):
            raise PreciseStackMapError("stack-map input staging extent changed")
        offset = self.written
        try:
            with memoryview(raw) as source:
                cursor = 0
                while cursor < size:
                    with source[start + cursor:start + min(size, cursor + _BLOCK)] as part:
                        count = self.stream.write(part)
                        capacity = len(part)
                    if type(count) is not int or not 0 < count <= capacity:
                        raise PreciseStackMapError("short stack-map input staging write")
                    cursor += count
                    self.written += count
            self.rows[self.appended] = (offset, size)
            self.appended += 1
        except OSError as exc:
            raise PreciseStackMapError("stack-map input storage failed: " + str(exc)) from exc
        finally:
            raw = None

    def seal(self):
        if (self.stream is None or self.sealed or self.appended != self.count
                or self.written != self.total):
            raise PreciseStackMapError("stack-map input staging extent changed")
        try:
            if self.stream.seek(0, 2) != self.total:
                raise PreciseStackMapError("stack-map input staging extent changed")
        except OSError as exc:
            raise PreciseStackMapError("stack-map input storage failed: " + str(exc)) from exc
        self.sealed = True

    def read_next(self, index, *, compact_budget, admit, single_buffer):
        if (self.stream is None or not self.sealed or index != self.consumed
                or not 0 <= index < self.count):
            raise PreciseStackMapError("stack-map input staging order changed")
        offset, size = self.rows[index]
        if (offset != self.read or size > self.total - self.read
                or offset < 0 or size < 0):
            raise PreciseStackMapError("stack-map input staging extent changed")
        rounded = compact_budget._rounded
        backing = rounded(sys.getsizeof(b"") + size)
        if not single_buffer:
            backing += rounded(_BYTEARRAY_HEADER) + rounded(size + 1)
        admit(backing, label="spool staged input window")
        buffer = view = result = closing = None
        try:
            if self.stream.seek(offset) != offset:
                raise PreciseStackMapError("stack-map input staging seek changed")
            buffer = io.BytesIO(b"\0" * size) if single_buffer else bytearray(size)
            view = buffer.getbuffer() if single_buffer else memoryview(buffer)
            cursor = 0
            while cursor < size:
                with view[cursor:min(size, cursor + _BLOCK)] as part:
                    count = self.stream.readinto(part)
                    capacity = len(part)
                if type(count) is not int or not 0 < count <= capacity:
                    raise PreciseStackMapError("short stack-map input staging read")
                cursor += count
                self.read += count
            view.release()
            view = None
            # A hidden export must fail rather than make getvalue copy the
            # whole backing. There is no immutable snapshot before this point.
            if single_buffer:
                buffer.truncate(size)
            result = buffer.getvalue() if single_buffer else bytes(buffer)
            if single_buffer:
                closing, buffer = buffer, None
                try:
                    closing.close()
                finally:
                    closing = None
            buffer = None
            return result
        except OSError as exc:
            raise PreciseStackMapError("stack-map input storage failed: " + str(exc)) from exc
        finally:
            try:
                if view is not None:
                    view.release()
            finally:
                try:
                    if single_buffer and buffer is not None:
                        buffer.close()
                finally:
                    buffer = view = result = closing = None

    def retire(self, index):
        if index != self.consumed or not 0 <= index < self.count:
            raise PreciseStackMapError("stack-map input staging order changed")
        self.rows[index] = None
        self.consumed += 1

    def close(self):
        stream, self.stream = self.stream, None
        self.rows.clear()
        try:
            if stream is not None:
                stream.close()
        except OSError as exc:
            raise PreciseStackMapError("stack-map input storage failed: " + str(exc)) from exc
        finally:
            stream = None
