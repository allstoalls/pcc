"""Native gzip codec for pcc-native build tools.

Reading, one-shot decompression and deterministic one-shot compression are
backed by the pcc zlib port.  Both one-shot and writable streams write the RFC
1952 header/trailer themselves and request raw DEFLATE, so header metadata and
incremental CRC/size accounting do not depend on the system zlib defaults.
"""
from __future__ import annotations

import io
import time
import zlib

try:
    from pcc.stdlib._compression_stream import CompressionWriter, DecompressReader
except ImportError:
    # pcc publishes this provider as the top-level stdlib module ``gzip``.
    from _compression_stream import CompressionWriter, DecompressReader



class BadGzipFile(OSError):
    pass


def _gzip_header(compresslevel, mtime):
    if not isinstance(compresslevel, int):
        raise TypeError("compresslevel must be an integer")
    if compresslevel < 0 or compresslevel > 9:
        raise zlib.error("Bad compression level")
    if mtime is None:
        selected_mtime = int(time.time())
    else:
        if not isinstance(mtime, int):
            raise TypeError("mtime must be an integer or None")
        selected_mtime = mtime
    if selected_mtime < 0 or selected_mtime > 0xFFFFFFFF:
        raise ValueError("mtime must fit in an unsigned 32-bit field")
    extra_flags = 0
    if compresslevel == 9:
        extra_flags = 2
    elif compresslevel == 1:
        extra_flags = 4
    return (
        b"\x1f\x8b\x08\x00"
        + selected_mtime.to_bytes(4, "little")
        + bytes([extra_flags, 255])
    )


class _GzipCompressor:
    def __init__(self, compresslevel, mtime):
        self._compressor = zlib.compressobj(
            compresslevel,
            zlib.Z_DEFLATED,
            -zlib.MAX_WBITS,
        )
        self._header = _gzip_header(compresslevel, mtime)
        self._checksum = 0
        self._size = 0
        self._closed = False

    def _take_header(self):
        header = self._header
        self._header = b""
        return header

    def compress(self, data):
        if self._closed:
            raise ValueError("I/O operation on closed gzip stream")
        self._checksum = zlib.crc32(data, self._checksum) & 0xFFFFFFFF
        self._size = (self._size + len(data)) & 0xFFFFFFFF
        return self._take_header() + self._compressor.compress(data)

    def flush(self, mode=zlib.Z_FINISH):
        if self._closed:
            raise ValueError("I/O operation on closed gzip stream")
        encoded = self._take_header() + self._compressor.flush(mode)
        if mode == zlib.Z_FINISH:
            encoded += self._checksum.to_bytes(4, "little")
            encoded += self._size.to_bytes(4, "little")
            self._closed = True
        return encoded

    def close(self):
        if self._closed:
            return
        self._compressor.close()
        self._closed = True


def _new_gzip_decoder():
    return zlib.decompressobj(16 + zlib.MAX_WBITS)


class _GzipReader(DecompressReader):
    def __init__(self, source):
        DecompressReader.__init__(
            self,
            source,
            _new_gzip_decoder,
            allow_zero_padding=True,
        )

    def _produce(self):
        try:
            return DecompressReader._produce(self)
        except (EOFError, zlib.error) as exc:
            raise BadGzipFile(str(exc))


def decompress(data):
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("a bytes-like object is required")
    reader = _GzipReader(io.BytesIO(bytes(data)))
    try:
        return reader.read()
    except EOFError as exc:
        raise BadGzipFile(str(exc))
    except zlib.error as exc:
        raise BadGzipFile(str(exc))
    finally:
        reader.close()


def compress(data, compresslevel=9, *, mtime=0):
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("a bytes-like object is required")
    if mtime is None:
        raise NotImplementedError(
            "gzip.compress mtime=None is not deterministic runtime-owned state"
        )
    header = _gzip_header(compresslevel, mtime)
    payload = zlib.compress(data, compresslevel, -zlib.MAX_WBITS)
    checksum = zlib.crc32(data) & 0xFFFFFFFF
    trailer = checksum.to_bytes(4, "little") + (
        len(data) & 0xFFFFFFFF
    ).to_bytes(4, "little")
    return header + payload + trailer


def open(
    filename,
    mode="rb",
    compresslevel=9,
    encoding=None,
    errors=None,
    newline=None,
):
    normalized = mode.replace("t", "").replace("b", "")
    if (
        "t" in mode
        or encoding is not None
        or errors is not None
        or newline is not None
    ):
        raise NotImplementedError("gzip text mode is not yet runtime-owned")
    if normalized == "r":
        return _GzipReader(filename)
    if normalized in ("w", "a", "x"):
        return GzipFile(
            filename=filename,
            mode=normalized + "b",
            compresslevel=compresslevel,
        )
    raise ValueError("Invalid mode: " + str(mode))


class GzipFile:
    def __init__(
        self,
        filename=None,
        mode=None,
        compresslevel=9,
        fileobj=None,
        mtime=None,
    ):
        selected_mode = mode or getattr(fileobj, "mode", "rb")
        normalized = selected_mode.replace("b", "")
        if normalized == "r":
            self._stream = _GzipReader(fileobj if fileobj is not None else filename)
            self._writing = False
        elif normalized in ("w", "a", "x"):
            destination = fileobj if fileobj is not None else filename
            if destination is None:
                raise TypeError("filename or fileobj must be supplied")
            compressor = _GzipCompressor(compresslevel, mtime)
            self._stream = CompressionWriter(
                destination,
                compressor,
                normalized + "b",
            )
            self._writing = True
        else:
            raise ValueError("Invalid mode: " + str(selected_mode))

    def read(self, size=-1):
        if self._writing:
            raise OSError("read() on write-only GzipFile object")
        return self._stream.read(size)

    def readline(self, size=-1):
        if self._writing:
            raise OSError("read() on write-only GzipFile object")
        return self._stream.readline(size)

    def write(self, data):
        if not self._writing:
            raise OSError("write() on read-only GzipFile object")
        return self._stream.write(data)

    def flush(self, zlib_mode=zlib.Z_SYNC_FLUSH):
        if self._writing:
            return self._stream._sync_flush(zlib_mode)
        return None

    def seek(self, offset, whence=0):
        return self._stream.seek(offset, whence)

    def tell(self):
        return self._stream.tell()

    @property
    def closed(self):
        return self._stream.closed

    def close(self):
        return self._stream.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


__all__ = ["BadGzipFile", "GzipFile", "open", "compress", "decompress"]
