"""Bounded stream plumbing shared by native compression ports.

The codec modules own their native codec state.  This module owns only the
file-like policy: compressed input is read in fixed-size chunks, decoded bytes
are retained only until the caller consumes them, backwards seeks replay the
source, and encoder output is forwarded after every write instead of retained
for the lifetime of the stream.
"""
from __future__ import annotations

# ``open`` below is the builtin: this module defines no ``open`` of its own,
# and a plain name keeps the file path native (``builtins.open`` would bind
# CPython's ``builtins`` module at import time).


_COMPRESSED_CHUNK = 64 * 1024
_DECODED_CHUNK = 64 * 1024


def _write_all(sink, payload):
    offset = 0
    size = len(payload)
    while offset < size:
        written = sink.write(payload[offset:])
        if written is None:
            return
        count = int(written)
        if count <= 0:
            raise OSError("compressed output sink made no progress")
        offset += count


class CompressionWriter:
    """File policy for an owned incremental compressor.

    The compressor must expose ``compress(data)``, ``flush()`` and an
    idempotent ``close()``.  ``flush()`` on this wrapper deliberately flushes
    only the destination; formats without a sync-flush operation must not be
    finalized before ``close()``.
    """

    def __init__(self, destination, compressor, mode="wb"):
        self._owns_destination = not hasattr(destination, "write")
        self._destination = None
        self._compressor = compressor
        self._position = 0
        self._closed = False
        self.mode = mode
        self.name = None
        if self._owns_destination:
            self._destination = open(destination, mode)
        else:
            self._destination = destination
        self.name = getattr(self._destination, "name", None)

    def _ensure_open(self):
        if self._closed:
            raise ValueError("I/O operation on closed compressed file")

    def _abort(self):
        if self._closed:
            return
        compressor = self._compressor
        self._compressor = None
        self._closed = True
        if compressor is not None:
            try:
                compressor.close()
            except Exception:
                pass
        if self._owns_destination and self._destination is not None:
            try:
                self._destination.close()
            except Exception:
                pass

    def _write_encoded(self, encoded):
        if encoded:
            _write_all(self._destination, encoded)

    def write(self, data):
        self._ensure_open()
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("a bytes-like object is required")
        try:
            self._write_encoded(self._compressor.compress(data))
        except Exception:
            self._abort()
            raise
        self._position += len(data)
        return len(data)

    def flush(self):
        self._ensure_open()
        self._destination.flush()

    def _sync_flush(self, mode):
        self._ensure_open()
        try:
            self._write_encoded(self._compressor.flush(mode))
            self._destination.flush()
        except Exception:
            self._abort()
            raise

    def close(self):
        if self._closed:
            return
        compressor = self._compressor
        self._compressor = None
        try:
            self._write_encoded(compressor.flush())
            self._destination.flush()
        finally:
            try:
                compressor.close()
            finally:
                self._closed = True
                if self._owns_destination and self._destination is not None:
                    self._destination.close()

    @property
    def closed(self):
        return self._closed

    def tell(self):
        self._ensure_open()
        return self._position

    def writable(self):
        return not self._closed

    def readable(self):
        return False

    def seekable(self):
        return False

    def __enter__(self):
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            self._abort()


class DecompressReader:
    def __init__(
        self,
        source,
        decoder_factory,
        *,
        allow_zero_padding=False,
        trailing_error=None,
    ):
        self._owns_source = not hasattr(source, "read")
        if self._owns_source:
            self._source = open(source, "rb")
        else:
            self._source = source
        self._decoder_factory = decoder_factory
        self._decoder = None
        self._compressed_tail = b""
        self._buffer = b""
        self._position = 0
        self._source_start = 0
        self._source_seekable = False
        self._source_exhausted = False
        self._decoded_eof = False
        self._between_members = False
        self._allow_zero_padding = bool(allow_zero_padding)
        self._trailing_error = trailing_error
        self._completed_members = 0
        self._member_started = False
        self._closed = False
        if hasattr(self._source, "tell") and hasattr(self._source, "seek"):
            try:
                self._source_start = int(self._source.tell())
                self._source_seekable = True
            except (OSError, ValueError):
                self._source_start = 0

    def _ensure_open(self):
        if self._closed:
            raise ValueError("I/O operation on closed compressed file")

    def _close_decoder(self):
        decoder = self._decoder
        self._decoder = None
        if decoder is not None and hasattr(decoder, "close"):
            decoder.close()

    def _next_compressed_chunk(self):
        while True:
            if self._compressed_tail:
                data = self._compressed_tail
                self._compressed_tail = b""
            else:
                if self._source_exhausted:
                    return b""
                data = self._source.read(_COMPRESSED_CHUNK)
                if not data:
                    self._source_exhausted = True
                    return b""
            if self._allow_zero_padding and self._between_members:
                offset = 0
                while offset < len(data) and data[offset] == 0:
                    offset += 1
                if offset == len(data):
                    continue
                data = data[offset:]
            self._between_members = False
            return data

    def _produce(self):
        """Append one bounded decoded fragment, or mark logical EOF."""
        if self._decoded_eof:
            return
        while True:
            if self._decoder is None:
                data = self._next_compressed_chunk()
                if not data:
                    self._decoded_eof = True
                    return
                self._decoder = self._decoder_factory()
                self._member_started = False
            elif getattr(self._decoder, "unconsumed_tail", b""):
                data = self._decoder.unconsumed_tail
            elif getattr(self._decoder, "needs_input", True):
                data = self._next_compressed_chunk()
                if not data:
                    if getattr(self._decoder, "eof", False):
                        self._close_decoder()
                        self._decoded_eof = True
                        return
                    raise EOFError(
                        "compressed file ended before the end-of-stream marker"
                    )
            else:
                data = b""

            try:
                decoded = self._decoder.decompress(data, _DECODED_CHUNK)
            except Exception as exc:
                if (
                    self._completed_members > 0
                    and not self._member_started
                    and self._trailing_error is not None
                    and isinstance(exc, self._trailing_error)
                ):
                    self._close_decoder()
                    self._decoded_eof = True
                    return
                raise
            self._member_started = True
            if decoded:
                self._buffer += decoded

            if getattr(self._decoder, "eof", False):
                unused = getattr(self._decoder, "unused_data", b"")
                self._close_decoder()
                if unused:
                    self._compressed_tail = unused + self._compressed_tail
                self._between_members = True
                self._completed_members += 1
                if decoded:
                    return
                continue
            if decoded:
                return
            if not getattr(self._decoder, "needs_input", True):
                if not data:
                    raise OSError("compressed decoder made no progress")
                continue
            if self._source_exhausted:
                raise EOFError(
                    "compressed file ended before the end-of-stream marker"
                )

    def read(self, size=-1):
        self._ensure_open()
        if size is None or int(size) < 0:
            chunks = []
            if self._buffer:
                chunks.append(self._buffer)
                self._position += len(self._buffer)
                self._buffer = b""
            while not self._decoded_eof:
                self._produce()
                if self._buffer:
                    chunks.append(self._buffer)
                    self._position += len(self._buffer)
                    self._buffer = b""
            return b"".join(chunks)

        wanted = int(size)
        if wanted < 0:
            wanted = 0
        while len(self._buffer) < wanted and not self._decoded_eof:
            self._produce()
        result = self._buffer[:wanted]
        self._buffer = self._buffer[wanted:]
        self._position += len(result)
        return result

    def readline(self, size=-1):
        self._ensure_open()
        limit = int(size)
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                end = newline + 1
                if limit >= 0 and end > limit:
                    end = limit
                break
            if limit >= 0 and len(self._buffer) >= limit:
                end = limit
                break
            if self._decoded_eof:
                end = len(self._buffer)
                break
            self._produce()
        result = self._buffer[:end]
        self._buffer = self._buffer[end:]
        self._position += len(result)
        return result

    def tell(self):
        self._ensure_open()
        return self._position

    def readable(self):
        return not self._closed

    def seekable(self):
        return not self._closed and self._source_seekable

    def _rewind(self):
        if not self._source_seekable:
            raise OSError("compressed source is not seekable")
        self._close_decoder()
        self._source.seek(self._source_start, 0)
        self._compressed_tail = b""
        self._buffer = b""
        self._position = 0
        self._source_exhausted = False
        self._decoded_eof = False
        self._between_members = False
        self._completed_members = 0
        self._member_started = False

    def seek(self, offset, whence=0):
        self._ensure_open()
        delta = int(offset)
        if whence == 0:
            target = delta
        elif whence == 1:
            target = self._position + delta
        elif whence == 2:
            # Establish the uncompressed length without retaining it.  A
            # negative SEEK_END then replays from the start, keeping memory
            # bounded independently of file size.
            while not self._decoded_eof:
                chunk = self.read(_DECODED_CHUNK)
                if not chunk and not self._decoded_eof:
                    raise OSError("compressed seek made no progress")
            target = self._position + delta
        else:
            raise ValueError("invalid whence")
        if target < 0:
            raise ValueError("negative seek position")
        if target < self._position:
            self._rewind()
        remaining = target - self._position
        while remaining > 0:
            chunk_size = _DECODED_CHUNK
            if remaining < chunk_size:
                chunk_size = remaining
            chunk = self.read(chunk_size)
            if not chunk:
                break
            remaining -= len(chunk)
        return self._position

    def close(self):
        if self._closed:
            return
        self._close_decoder()
        if self._owns_source:
            self._source.close()
        self._closed = True

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    @property
    def closed(self):
        return self._closed

    def __enter__(self):
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


__all__ = ["CompressionWriter", "DecompressReader"]
