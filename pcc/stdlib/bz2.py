"""Native bzip2 codec for pcc build-tool programs.

The implementation resolves the stable ``BZ2_bzDecompress*`` and
``BZ2_bzCompress*`` ABIs from the system library.  Incremental compression and
decompression each retain one native ``bz_stream`` while materializing output
in fixed 64 KiB fragments.  No codec object accumulates the complete logical
stream between calls.
"""
from __future__ import annotations

import io

try:
    from pcc.stdlib._compression_stream import CompressionWriter, DecompressReader
except ImportError:
    # pcc publishes this provider as the top-level stdlib module ``bz2``.
    from _compression_stream import CompressionWriter, DecompressReader

from pcc.extern import c_int64, c_ptr, c_rawptr, extern, c_obj
from pcc.unsafe import (
    call_i32_ptr1,
    call_i32_ptr_i32,
    call_i32_ptr_i32_i32_i32,
    call_i64_ptr_i64_i64,
    cstr,
    dynamic_library_close,
    dynamic_library_open,
    dynamic_library_symbol,
    free,
    int_to_ptr,
    load_i32,
    malloc,
    memset,
    null,
    ptr_add,
    ptr_is_null,
    ptr_to_int,
    realloc,
    store_i32,
    store_ptr,
)


# The data operand is a raw native buffer address, never an object.
_py_bytes_new: "extern" = extern("py_bytes_new", (c_rawptr, c_int64), c_obj)

_BZ_STREAM_SIZE = 80
_MAX_NATIVE_INPUT = 0xFFFFFFFF
_STREAM_OUTPUT_CHUNK = 64 * 1024
_INITIAL_OUTPUT = 16384
_MAX_COMPRESSION_INPUT = 64 * 1024 * 1024
_MAX_COMPRESSED_OUTPUT = 128 * 1024 * 1024
_COMPRESSION_OUTPUT_SENTINEL_CAPACITY = _MAX_COMPRESSED_OUTPUT + 1

BZ_OK = 0
BZ_RUN_OK = 1
BZ_FLUSH_OK = 2
BZ_FINISH_OK = 3
BZ_STREAM_END = 4
BZ_RUN = 0
BZ_FLUSH = 1
BZ_FINISH = 2


def _signed_i32(value):
    narrowed = value & 0xFFFFFFFF
    if narrowed >= 0x80000000:
        return narrowed - 0x100000000
    return narrowed


def _open_libbz2() -> int:
    handle = dynamic_library_open(
        cstr("/usr/lib/libbz2.1.0.dylib"), "darwin"
    )
    if ptr_is_null(handle):
        handle = dynamic_library_open(
            cstr("/usr/lib/libbz2.dylib"), "darwin"
        )
    if ptr_is_null(handle):
        handle = dynamic_library_open(cstr("libbz2.so.1.0"), "linux")
    if ptr_is_null(handle):
        handle = dynamic_library_open(cstr("libbz2.so.1"), "linux")
    return handle


def _decompress_symbols(handle: int) -> tuple[int, int, int]:
    init_fn = dynamic_library_symbol(handle, cstr("BZ2_bzDecompressInit"))
    code_fn = dynamic_library_symbol(handle, cstr("BZ2_bzDecompress"))
    end_fn = dynamic_library_symbol(handle, cstr("BZ2_bzDecompressEnd"))
    if ptr_is_null(init_fn) or ptr_is_null(code_fn) or ptr_is_null(end_fn):
        raise OSError("system libbz2 is missing the decompression ABI")
    return init_fn, code_fn, end_fn


def _compress_symbols(handle: int) -> tuple[int, int, int]:
    init_fn = dynamic_library_symbol(handle, cstr("BZ2_bzCompressInit"))
    code_fn = dynamic_library_symbol(handle, cstr("BZ2_bzCompress"))
    end_fn = dynamic_library_symbol(handle, cstr("BZ2_bzCompressEnd"))
    if ptr_is_null(init_fn) or ptr_is_null(code_fn) or ptr_is_null(end_fn):
        raise OSError("system libbz2 is missing the compression ABI")
    return init_fn, code_fn, end_fn


def _bytes_data(data) -> int:
    return ptr_add(data, 24)


def _total_out(stream: int) -> int:
    low = load_i32(stream, 36) & 0xFFFFFFFF
    high = load_i32(stream, 40) & 0xFFFFFFFF
    return low + (high << 32)


class BZ2Decompressor:
    """One-member incremental decoder backed by an owned ``bz_stream``."""

    def __init__(self):
        self._handle = 0
        self._stream = 0
        self._code_fn = 0
        self._end_fn = 0
        self._closed = False
        self._pending = b""
        self.eof = False
        self.unused_data = b""
        self.needs_input = True

        handle = _open_libbz2()
        if ptr_is_null(handle):
            raise OSError("system libbz2 shared library is unavailable")
        stream = malloc(_BZ_STREAM_SIZE)
        if ptr_is_null(stream):
            dynamic_library_close(handle)
            raise MemoryError("unable to allocate bzip2 decompression state")
        try:
            init_fn, code_fn, end_fn = _decompress_symbols(handle)
            memset(stream, 0, _BZ_STREAM_SIZE)
            status = _signed_i32(
                call_i64_ptr_i64_i64(init_fn, stream, 0, 0)
            )
            if status != BZ_OK:
                raise OSError(
                    "bzip2 decompressor initialization failed: " + str(status)
                )
        except Exception:
            free(stream)
            dynamic_library_close(handle)
            raise
        self._handle = ptr_to_int(handle)
        self._stream = ptr_to_int(stream)
        self._code_fn = ptr_to_int(code_fn)
        self._end_fn = ptr_to_int(end_fn)

    def decompress(self, data, max_length=-1):
        if self._closed:
            raise OSError("bzip2 decompressor is closed")
        if self.eof:
            raise EOFError("End of stream already reached")
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("a bytes-like object is required")
        if not isinstance(max_length, int):
            raise TypeError("max_length must be an integer")
        if max_length < -1:
            raise ValueError("max_length must be non-negative")

        incoming = bytes(data)
        payload = self._pending + incoming
        self._pending = b""
        if len(payload) > _MAX_NATIVE_INPUT:
            raise OSError("bzip2 input exceeds the native unsigned-int boundary")
        if max_length == 0:
            self._pending = payload
            self.needs_input = not bool(self._pending)
            return b""

        stream = int_to_ptr(self._stream)
        code_fn = int_to_ptr(self._code_fn)
        input_size = len(payload)
        store_ptr(stream, 0, _bytes_data(payload) if input_size else null())
        store_i32(stream, 8, input_size)
        chunks = []
        produced_total = 0
        previous_remaining = input_size + 1
        try:
            while True:
                capacity = _STREAM_OUTPUT_CHUNK
                if max_length >= 0:
                    remaining_limit = max_length - produced_total
                    if remaining_limit <= 0:
                        self.needs_input = False
                        break
                    if remaining_limit < capacity:
                        capacity = remaining_limit
                output = malloc(capacity)
                if ptr_is_null(output):
                    raise MemoryError("unable to allocate bzip2 output chunk")
                try:
                    store_ptr(stream, 24, output)
                    store_i32(stream, 32, capacity)
                    status = _signed_i32(call_i32_ptr1(code_fn, stream))
                    available = load_i32(stream, 32) & 0xFFFFFFFF
                    remaining = load_i32(stream, 8) & 0xFFFFFFFF
                    produced = capacity - available
                    if produced:
                        chunk = _py_bytes_new(output, produced)
                        if ptr_is_null(chunk):
                            raise MemoryError(
                                "unable to allocate decompressed bzip2 bytes"
                            )
                        chunks.append(chunk)
                        produced_total += produced
                finally:
                    store_ptr(stream, 24, null())
                    store_i32(stream, 32, 0)
                    free(output)

                if status == BZ_STREAM_END:
                    self.eof = True
                    self.needs_input = False
                    if remaining:
                        self.unused_data = bytes(
                            payload[input_size - remaining :]
                        )
                    break
                if status != BZ_OK:
                    raise OSError("Invalid data stream: " + str(status))
                if max_length >= 0 and produced_total >= max_length:
                    if remaining:
                        self._pending = bytes(
                            payload[input_size - remaining :]
                        )
                    self.needs_input = False
                    break
                if remaining == 0 and produced < capacity:
                    self.needs_input = True
                    break
                if produced == 0 and remaining == previous_remaining:
                    if remaining:
                        raise OSError("bzip2 decompressor made no progress")
                    self.needs_input = True
                    break
                previous_remaining = remaining

            remaining = load_i32(stream, 8) & 0xFFFFFFFF
            if not self.eof and remaining and not self._pending:
                self._pending = bytes(payload[input_size - remaining :])
                self.needs_input = False
            return b"".join(chunks)
        finally:
            store_ptr(stream, 0, null())
            store_i32(stream, 8, 0)

    def close(self):
        if self._closed:
            return
        if self._stream:
            call_i32_ptr1(
                int_to_ptr(self._end_fn), int_to_ptr(self._stream)
            )
            free(int_to_ptr(self._stream))
        if self._handle:
            dynamic_library_close(int_to_ptr(self._handle))
        self._stream = 0
        self._handle = 0
        self._pending = b""
        self._closed = True

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def _new_bz2_decoder():
    return BZ2Decompressor()


def decompress(data):
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("a bytes-like object is required")
    if len(data) == 0:
        return b""
    reader = DecompressReader(
        io.BytesIO(bytes(data)),
        _new_bz2_decoder,
        trailing_error=OSError,
    )
    try:
        return reader.read()
    except EOFError as exc:
        raise ValueError(str(exc))
    finally:
        reader.close()


def compress(data, compresslevel=9):
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("a bytes-like object is required")
    if not isinstance(compresslevel, int):
        raise TypeError("compresslevel must be an integer")
    if compresslevel < 1 or compresslevel > 9:
        raise ValueError("compresslevel must be between 1 and 9")
    input_size = len(data)
    if input_size > _MAX_COMPRESSION_INPUT:
        raise OSError("bzip2 compression input exceeds the 64 MiB limit")

    handle = _open_libbz2()
    if ptr_is_null(handle):
        raise OSError("system libbz2 shared library is unavailable")
    stream = malloc(_BZ_STREAM_SIZE)
    output = malloc(_INITIAL_OUTPUT)
    capacity = _INITIAL_OUTPUT
    initialized = False
    end_fn = null()
    if ptr_is_null(stream) or ptr_is_null(output):
        free(stream)
        free(output)
        dynamic_library_close(handle)
        raise MemoryError("unable to allocate bzip2 compression state")
    try:
        init_fn, code_fn, end_fn = _compress_symbols(handle)
        memset(stream, 0, _BZ_STREAM_SIZE)
        status = call_i32_ptr_i32_i32_i32(
            init_fn, stream, compresslevel, 0, 0
        )
        if status != BZ_OK:
            raise OSError(
                "bzip2 compressor initialization failed: " + str(status)
            )
        initialized = True
        store_ptr(stream, 0, _bytes_data(data))
        store_i32(stream, 8, input_size)
        store_ptr(stream, 24, output)
        store_i32(stream, 32, capacity)
        previous_total = -1
        previous_remaining = -1

        while True:
            status = call_i32_ptr_i32(code_fn, stream, BZ_FINISH)
            total = _total_out(stream)
            if status == BZ_STREAM_END:
                if total > _MAX_COMPRESSED_OUTPUT:
                    raise OSError(
                        "compressed data exceeds the 128 MiB output limit"
                    )
                end_status = call_i32_ptr1(end_fn, stream)
                initialized = False
                if end_status != BZ_OK:
                    raise OSError(
                        "bzip2 compressor finalization failed: "
                        + str(end_status)
                    )
                result = _py_bytes_new(output, total)
                if ptr_is_null(result):
                    raise MemoryError("unable to allocate compressed bzip2 bytes")
                return result
            available = load_i32(stream, 32) & 0xFFFFFFFF
            remaining = load_i32(stream, 8) & 0xFFFFFFFF
            if status == BZ_FINISH_OK and available == 0:
                new_capacity = capacity * 2
                if new_capacity > _COMPRESSION_OUTPUT_SENTINEL_CAPACITY:
                    new_capacity = _COMPRESSION_OUTPUT_SENTINEL_CAPACITY
                if new_capacity <= capacity:
                    raise OSError(
                        "compressed data exceeds the 128 MiB output limit"
                    )
                grown = realloc(output, new_capacity)
                if ptr_is_null(grown):
                    raise MemoryError("unable to grow bzip2 output buffer")
                output = grown
                capacity = new_capacity
                store_ptr(stream, 24, ptr_add(output, total))
                store_i32(stream, 32, capacity - total)
                previous_total = total
                previous_remaining = remaining
                continue
            if status == BZ_FINISH_OK:
                if total == previous_total and remaining == previous_remaining:
                    raise OSError("bzip2 compressor made no progress")
                previous_total = total
                previous_remaining = remaining
                continue
            raise OSError("bzip2 compression failed: " + str(status))
    finally:
        if initialized:
            call_i32_ptr1(end_fn, stream)
        free(output)
        free(stream)
        dynamic_library_close(handle)


class BZ2Compressor:
    """Owned incremental bzip2 encoder with bounded native output chunks."""

    def __init__(self, compresslevel=9):
        if not isinstance(compresslevel, int):
            raise TypeError("compresslevel must be an integer")
        if compresslevel < 1 or compresslevel > 9:
            raise ValueError("compresslevel must be between 1 and 9")

        self._handle = 0
        self._stream = 0
        self._code_fn = 0
        self._end_fn = 0
        self._closed = False

        handle = _open_libbz2()
        if ptr_is_null(handle):
            raise OSError("system libbz2 shared library is unavailable")
        stream = malloc(_BZ_STREAM_SIZE)
        if ptr_is_null(stream):
            dynamic_library_close(handle)
            raise MemoryError("unable to allocate bzip2 compression state")
        try:
            init_fn, code_fn, end_fn = _compress_symbols(handle)
            memset(stream, 0, _BZ_STREAM_SIZE)
            status = _signed_i32(
                call_i32_ptr_i32_i32_i32(
                    init_fn, stream, compresslevel, 0, 0
                )
            )
            if status != BZ_OK:
                raise OSError(
                    "bzip2 compressor initialization failed: " + str(status)
                )
        except Exception:
            free(stream)
            dynamic_library_close(handle)
            raise
        self._handle = ptr_to_int(handle)
        self._stream = ptr_to_int(stream)
        self._code_fn = ptr_to_int(code_fn)
        self._end_fn = ptr_to_int(end_fn)

    def _release(self, check_status=False):
        if self._closed:
            return
        stream = int_to_ptr(self._stream)
        handle = int_to_ptr(self._handle)
        end_fn = int_to_ptr(self._end_fn)
        self._stream = 0
        self._handle = 0
        self._code_fn = 0
        self._end_fn = 0
        self._closed = True
        end_status = BZ_OK
        if not ptr_is_null(stream):
            if not ptr_is_null(end_fn):
                end_status = _signed_i32(call_i32_ptr1(end_fn, stream))
            free(stream)
        if not ptr_is_null(handle):
            dynamic_library_close(handle)
        if check_status and end_status != BZ_OK:
            raise OSError(
                "bzip2 compressor finalization failed: " + str(end_status)
            )

    def _run(self, data, action, finishing):
        if self._closed:
            raise ValueError("Compressor has been flushed")
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("a bytes-like object is required")
        input_size = len(data)
        if input_size > _MAX_NATIVE_INPUT:
            raise OSError("bzip2 input exceeds the native unsigned-int boundary")
        if input_size == 0 and action == BZ_RUN:
            return b""

        stream = int_to_ptr(self._stream)
        code_fn = int_to_ptr(self._code_fn)
        store_ptr(stream, 0, _bytes_data(data) if input_size else null())
        store_i32(stream, 8, input_size)
        chunks = []
        previous_remaining = input_size + 1
        try:
            while True:
                output = malloc(_STREAM_OUTPUT_CHUNK)
                if ptr_is_null(output):
                    raise MemoryError("unable to allocate bzip2 output chunk")
                try:
                    store_ptr(stream, 24, output)
                    store_i32(stream, 32, _STREAM_OUTPUT_CHUNK)
                    status = _signed_i32(call_i32_ptr_i32(code_fn, stream, action))
                    available = load_i32(stream, 32) & 0xFFFFFFFF
                    remaining = load_i32(stream, 8) & 0xFFFFFFFF
                    produced = _STREAM_OUTPUT_CHUNK - available
                    if produced:
                        chunk = _py_bytes_new(output, produced)
                        if ptr_is_null(chunk):
                            raise MemoryError(
                                "unable to allocate compressed bzip2 bytes"
                            )
                        chunks.append(chunk)
                finally:
                    store_ptr(stream, 24, null())
                    store_i32(stream, 32, 0)
                    free(output)

                if finishing and status == BZ_STREAM_END:
                    self._release(True)
                    return b"".join(chunks)
                expected = BZ_FINISH_OK if finishing else BZ_RUN_OK
                if status != expected:
                    raise OSError("bzip2 compression failed: " + str(status))
                if not finishing and remaining == 0 and available > 0:
                    return b"".join(chunks)
                if produced == 0 and remaining == previous_remaining:
                    raise OSError("bzip2 compressor made no progress")
                previous_remaining = remaining
        except Exception:
            self._release(False)
            raise
        finally:
            if self._stream:
                active = int_to_ptr(self._stream)
                store_ptr(active, 0, null())
                store_i32(active, 8, 0)

    def compress(self, data):
        return self._run(data, BZ_RUN, False)

    def flush(self):
        return self._run(b"", BZ_FINISH, True)

    def close(self):
        self._release(False)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def open(filename, mode="rb", *, compresslevel=9, encoding=None, errors=None,
         newline=None):
    normalized = mode.replace("t", "").replace("b", "")
    if (
        "t" in mode
        or encoding is not None
        or errors is not None
        or newline is not None
    ):
        raise NotImplementedError("bz2 text mode is not yet runtime-owned")
    if normalized == "r":
        return DecompressReader(
            filename,
            _new_bz2_decoder,
            trailing_error=OSError,
        )
    if normalized in ("w", "a", "x"):
        return BZ2File(filename, normalized + "b", compresslevel=compresslevel)
    raise ValueError("Invalid mode: " + str(mode))


class BZ2File:
    def __init__(self, filename, mode="r", *, compresslevel=9):
        normalized = mode.replace("b", "")
        if normalized == "r":
            self._stream = DecompressReader(
                filename,
                _new_bz2_decoder,
                trailing_error=OSError,
            )
            self._writing = False
        elif normalized in ("w", "a", "x"):
            self._stream = CompressionWriter(
                filename,
                BZ2Compressor(compresslevel),
                normalized + "b",
            )
            self._writing = True
        else:
            raise ValueError("Invalid mode: " + str(mode))

    def read(self, size=-1):
        if self._writing:
            raise OSError("read() on write-only BZ2File object")
        return self._stream.read(size)

    def readline(self, size=-1):
        if self._writing:
            raise OSError("read() on write-only BZ2File object")
        return self._stream.readline(size)

    def write(self, data):
        if not self._writing:
            raise OSError("write() on read-only BZ2File object")
        return self._stream.write(data)

    def flush(self):
        return self._stream.flush()

    def seek(self, offset, whence=0):
        return self._stream.seek(offset, whence)

    def tell(self):
        return self._stream.tell()

    def close(self):
        return self._stream.close()

    @property
    def closed(self):
        return self._stream.closed

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


__all__ = [
    "BZ2Compressor",
    "BZ2Decompressor",
    "BZ2File",
    "open",
    "compress",
    "decompress",
]
