"""Native zlib compression for pcc build-tool programs.

The system zlib is resolved through the compiler-owned dynamic-library
boundary, so importing this module never adds a link-time libz dependency or
consults a host Python.  Incremental compression and decompression each own one
``z_stream`` and expose only fixed 64 KiB native output fragments.  Python
``bytes`` results still necessarily scale with the result of each public call,
but the native codec state does not retain the complete stream.
"""
from __future__ import annotations

import binascii

from pcc.extern import c_int64, c_ptr, c_rawptr, extern, c_obj
from pcc.unsafe import (
    call_i32_ptr1,
    call_i32_ptr_i32,
    call_i32_ptr_i32_i32_i32_i32_i32_ptr_i32,
    call_i64_ptr1,
    call_i64_ptr_i64,
    call_i64_ptr_i64_ptr_i64,
    call_ptr0,
    cstr,
    dynamic_library_close,
    dynamic_library_open,
    dynamic_library_symbol,
    free,
    int_to_ptr,
    load_i32,
    load_i64,
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


MAX_WBITS = 15
DEF_MEM_LEVEL = 8
Z_DEFAULT_COMPRESSION = -1
Z_DEFLATED = 8
Z_DEFAULT_STRATEGY = 0
Z_NO_FLUSH = 0
Z_PARTIAL_FLUSH = 1
Z_SYNC_FLUSH = 2
Z_FULL_FLUSH = 3
Z_FINISH = 4
Z_BLOCK = 5
Z_OK = 0
Z_STREAM_END = 1
Z_MEM_ERROR = -4
Z_BUF_ERROR = -5

_Z_STREAM_SIZE = 112
_MAX_INPUT = 4294967295
_STREAM_OUTPUT_CHUNK = 64 * 1024
_MAX_COMPRESSION_INPUT = 64 * 1024 * 1024
_MAX_COMPRESSED_OUTPUT = 128 * 1024 * 1024
_COMPRESSION_OUTPUT_SENTINEL_CAPACITY = _MAX_COMPRESSED_OUTPUT + 1
_INITIAL_COMPRESSION_OUTPUT = 16384


class error(Exception):
    pass


def _signed_i32(value):
    narrowed = value & 0xFFFFFFFF
    if narrowed >= 0x80000000:
        return narrowed - 0x100000000
    return narrowed


def _open_zlib() -> int:
    handle = dynamic_library_open(
        cstr("/usr/lib/libz.1.dylib"), "darwin"
    )
    if ptr_is_null(handle):
        handle = dynamic_library_open(
            cstr("/usr/lib/libz.dylib"), "darwin"
        )
    if ptr_is_null(handle):
        handle = dynamic_library_open(cstr("libz.so.1"), "linux")
    return handle


def _resolve_inflate_symbols(handle: int) -> tuple[int, int, int, int]:
    init_fn = dynamic_library_symbol(handle, cstr("inflateInit2_"))
    inflate_fn = dynamic_library_symbol(handle, cstr("inflate"))
    end_fn = dynamic_library_symbol(handle, cstr("inflateEnd"))
    version_fn = dynamic_library_symbol(handle, cstr("zlibVersion"))
    if (
        ptr_is_null(init_fn)
        or ptr_is_null(inflate_fn)
        or ptr_is_null(end_fn)
        or ptr_is_null(version_fn)
    ):
        raise error("system zlib is missing the inflate ABI")
    return init_fn, inflate_fn, end_fn, version_fn


def _resolve_deflate_symbols(handle: int) -> tuple[int, int, int, int]:
    init_fn = dynamic_library_symbol(handle, cstr("deflateInit2_"))
    deflate_fn = dynamic_library_symbol(handle, cstr("deflate"))
    end_fn = dynamic_library_symbol(handle, cstr("deflateEnd"))
    version_fn = dynamic_library_symbol(handle, cstr("zlibVersion"))
    if (
        ptr_is_null(init_fn)
        or ptr_is_null(deflate_fn)
        or ptr_is_null(end_fn)
        or ptr_is_null(version_fn)
    ):
        raise error("system zlib is missing the deflate ABI")
    return init_fn, deflate_fn, end_fn, version_fn


def _bytes_data(data) -> int:
    # pcc bytes and bytearray share byte_len@16 followed by inline data@24.
    return ptr_add(data, 24)


def _grow_compression_output(stream: int, output: int, capacity: int) -> tuple[int, int]:
    total = load_i64(stream, 40)
    new_capacity = capacity * 2
    if new_capacity > _COMPRESSION_OUTPUT_SENTINEL_CAPACITY:
        new_capacity = _COMPRESSION_OUTPUT_SENTINEL_CAPACITY
    if new_capacity <= capacity:
        return null(), capacity
    grown = realloc(output, new_capacity)
    if ptr_is_null(grown):
        return null(), capacity
    store_ptr(stream, 24, ptr_add(grown, total))
    store_i32(stream, 32, new_capacity - total)
    return grown, new_capacity


def crc32(data, value: int = 0) -> int:
    return binascii.crc32(data, value)


def adler32(data, value: int = 1) -> int:
    # zlib's Adler-32: sums modulo 65521, reduced every NMAX=5552 bytes (the
    # largest run that cannot overflow 32-bit accumulators in the C code).
    a = value & 0xFFFF
    b = (value >> 16) & 0xFFFF
    view = bytes(data)
    size = len(view)
    index = 0
    while index < size:
        end = index + 5552
        if end > size:
            end = size
        while index < end:
            a += view[index]
            b += a
            index += 1
        a %= 65521
        b %= 65521
    return (b << 16) | a


class Compress:
    """Owned incremental deflate state with bounded native working storage."""

    def __init__(
        self,
        level=Z_DEFAULT_COMPRESSION,
        method=Z_DEFLATED,
        wbits=MAX_WBITS,
        memLevel=DEF_MEM_LEVEL,
        strategy=Z_DEFAULT_STRATEGY,
        zdict=None,
    ):
        if not isinstance(level, int):
            raise TypeError("level must be an integer")
        if level < Z_DEFAULT_COMPRESSION or level > 9:
            raise ValueError("Invalid initialization option")
        if method != Z_DEFLATED:
            raise ValueError("Invalid initialization option")
        if not isinstance(wbits, int):
            raise TypeError("wbits must be an integer")
        if not (
            (-MAX_WBITS <= wbits <= -9)
            or (9 <= wbits <= MAX_WBITS)
            or (25 <= wbits <= 31)
        ):
            raise ValueError("Invalid initialization option")
        if not isinstance(memLevel, int):
            raise TypeError("memLevel must be an integer")
        if memLevel < 1 or memLevel > 9:
            raise ValueError("Invalid initialization option")
        if strategy not in (0, 1, 2, 3, 4):
            raise ValueError("Invalid initialization option")
        if zdict is not None:
            raise NotImplementedError("zlib preset dictionaries are not runtime-owned")

        self._handle = 0
        self._stream = 0
        self._deflate_fn = 0
        self._end_fn = 0
        self._closed = False

        handle = _open_zlib()
        if ptr_is_null(handle):
            raise error("system zlib shared library is unavailable")
        stream = malloc(_Z_STREAM_SIZE)
        if ptr_is_null(stream):
            dynamic_library_close(handle)
            raise MemoryError("unable to allocate zlib compression state")
        try:
            init_fn, deflate_fn, end_fn, version_fn = _resolve_deflate_symbols(
                handle
            )
            memset(stream, 0, _Z_STREAM_SIZE)
            version = call_ptr0(version_fn)
            status = _signed_i32(
                call_i32_ptr_i32_i32_i32_i32_i32_ptr_i32(
                    init_fn,
                    stream,
                    level,
                    method,
                    wbits,
                    memLevel,
                    strategy,
                    version,
                    _Z_STREAM_SIZE,
                )
            )
            if status == Z_MEM_ERROR:
                raise MemoryError(
                    "out of memory while initializing zlib compression"
                )
            if status != Z_OK:
                raise error("zlib deflate initialization failed: " + str(status))
        except Exception:
            free(stream)
            dynamic_library_close(handle)
            raise
        self._handle = ptr_to_int(handle)
        self._stream = ptr_to_int(stream)
        self._deflate_fn = ptr_to_int(deflate_fn)
        self._end_fn = ptr_to_int(end_fn)

    def _release(self, check_status=False):
        if self._closed:
            return
        stream = int_to_ptr(self._stream)
        handle = int_to_ptr(self._handle)
        end_fn = int_to_ptr(self._end_fn)
        self._stream = 0
        self._handle = 0
        self._deflate_fn = 0
        self._end_fn = 0
        self._closed = True
        end_status = Z_OK
        if not ptr_is_null(stream):
            if not ptr_is_null(end_fn):
                end_status = _signed_i32(call_i32_ptr1(end_fn, stream))
            free(stream)
        if not ptr_is_null(handle):
            dynamic_library_close(handle)
        if check_status and end_status != Z_OK:
            raise error("zlib deflate finalization failed: " + str(end_status))

    def _run(self, data, flush_mode, finishing):
        if self._closed:
            raise error("inconsistent stream state")
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("a bytes-like object is required")
        input_size = len(data)
        if input_size > _MAX_INPUT:
            raise error("zlib input exceeds the native uInt boundary")
        if input_size == 0 and flush_mode == Z_NO_FLUSH:
            return b""

        stream = int_to_ptr(self._stream)
        deflate_fn = int_to_ptr(self._deflate_fn)
        store_ptr(stream, 0, _bytes_data(data) if input_size else null())
        store_i32(stream, 8, input_size)
        chunks = []
        previous_remaining = input_size + 1
        try:
            while True:
                output = malloc(_STREAM_OUTPUT_CHUNK)
                if ptr_is_null(output):
                    raise MemoryError("unable to allocate zlib output chunk")
                try:
                    store_ptr(stream, 24, output)
                    store_i32(stream, 32, _STREAM_OUTPUT_CHUNK)
                    status = _signed_i32(
                        call_i32_ptr_i32(deflate_fn, stream, flush_mode)
                    )
                    available = load_i32(stream, 32) & 0xFFFFFFFF
                    remaining = load_i32(stream, 8) & 0xFFFFFFFF
                    produced = _STREAM_OUTPUT_CHUNK - available
                    if produced:
                        chunk = _py_bytes_new(output, produced)
                        if ptr_is_null(chunk):
                            raise MemoryError(
                                "unable to allocate compressed zlib bytes"
                            )
                        chunks.append(chunk)
                finally:
                    store_ptr(stream, 24, null())
                    store_i32(stream, 32, 0)
                    free(output)

                if finishing and status == Z_STREAM_END:
                    self._release(True)
                    return b"".join(chunks)
                if status not in (Z_OK, Z_BUF_ERROR):
                    raise error("zlib compression failed: " + str(status))
                if not finishing and remaining == 0 and available > 0:
                    return b"".join(chunks)
                if produced == 0 and remaining == previous_remaining:
                    if not finishing and remaining == 0:
                        return b"".join(chunks)
                    raise error("zlib compressor made no progress")
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
        return self._run(data, Z_NO_FLUSH, False)

    def flush(self, mode=Z_FINISH):
        if not isinstance(mode, int):
            raise TypeError("mode must be an integer")
        if mode not in (
            Z_NO_FLUSH,
            Z_PARTIAL_FLUSH,
            Z_SYNC_FLUSH,
            Z_FULL_FLUSH,
            Z_FINISH,
            Z_BLOCK,
        ):
            raise ValueError("Invalid flush option")
        return self._run(b"", mode, mode == Z_FINISH)

    def close(self):
        self._release(False)

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class Decompress:
    """Owned incremental inflate state for the supported zlib ABI surface."""

    def __init__(self, wbits=MAX_WBITS, zdict=None):
        if zdict is not None:
            raise NotImplementedError("zlib preset dictionaries are not runtime-owned")
        if not isinstance(wbits, int):
            raise TypeError("wbits must be an integer")
        if not (
            wbits == 0
            or (-MAX_WBITS <= wbits <= -8)
            or (8 <= wbits <= MAX_WBITS)
            or (24 <= wbits <= 31)
            or (40 <= wbits <= 47)
        ):
            raise ValueError("Invalid initialization option")

        self._handle = 0
        self._stream = 0
        self._inflate_fn = 0
        self._end_fn = 0
        self._closed = False
        self.eof = False
        self.unused_data = b""
        self.unconsumed_tail = b""
        self.total_in = 0
        self._needs_input = True

        handle = _open_zlib()
        if ptr_is_null(handle):
            raise error("system zlib shared library is unavailable")
        stream = malloc(_Z_STREAM_SIZE)
        if ptr_is_null(stream):
            dynamic_library_close(handle)
            raise MemoryError("unable to allocate zlib decompression state")
        try:
            init_fn, inflate_fn, end_fn, version_fn = _resolve_inflate_symbols(
                handle
            )
            memset(stream, 0, _Z_STREAM_SIZE)
            version = call_ptr0(version_fn)
            status = _signed_i32(
                call_i64_ptr_i64_ptr_i64(
                    init_fn,
                    stream,
                    int(wbits),
                    version,
                    _Z_STREAM_SIZE,
                )
            )
            if status != Z_OK:
                raise error("zlib inflate initialization failed: " + str(status))
        except Exception:
            free(stream)
            dynamic_library_close(handle)
            raise
        self._handle = ptr_to_int(handle)
        self._stream = ptr_to_int(stream)
        self._inflate_fn = ptr_to_int(inflate_fn)
        self._end_fn = ptr_to_int(end_fn)

    @property
    def needs_input(self):
        return self._needs_input

    def _decode(self, data, max_length, finish):
        if self._closed:
            raise error("inconsistent stream state")
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("a bytes-like object is required")
        if not isinstance(max_length, int):
            raise TypeError("max_length must be an integer")
        if max_length < 0:
            raise ValueError("max_length must be non-negative")
        if self.eof:
            if data:
                self.unused_data += bytes(data)
            self.unconsumed_tail = b""
            self._needs_input = False
            return b""
        input_size = len(data)
        if input_size > _MAX_INPUT:
            raise error("zlib input exceeds the native uInt boundary")

        stream = int_to_ptr(self._stream)
        inflate_fn = int_to_ptr(self._inflate_fn)
        store_ptr(stream, 0, _bytes_data(data) if input_size else null())
        store_i32(stream, 8, input_size)
        self.unconsumed_tail = b""
        chunks = []
        produced_total = 0
        previous_remaining = input_size + 1
        try:
            while True:
                capacity = _STREAM_OUTPUT_CHUNK
                if max_length > 0:
                    remaining_limit = max_length - produced_total
                    if remaining_limit <= 0:
                        self._needs_input = False
                        break
                    if remaining_limit < capacity:
                        capacity = remaining_limit
                output = malloc(capacity)
                if ptr_is_null(output):
                    raise MemoryError("unable to allocate zlib output chunk")
                try:
                    store_ptr(stream, 24, output)
                    store_i32(stream, 32, capacity)
                    status = _signed_i32(
                        call_i64_ptr_i64(
                            inflate_fn,
                            stream,
                            Z_FINISH if finish else Z_NO_FLUSH,
                        )
                    )
                    available = load_i32(stream, 32) & 0xFFFFFFFF
                    produced = capacity - available
                    remaining = load_i32(stream, 8) & 0xFFFFFFFF
                    if produced:
                        chunk = _py_bytes_new(output, produced)
                        if ptr_is_null(chunk):
                            raise MemoryError(
                                "unable to allocate decompressed zlib bytes"
                            )
                        chunks.append(chunk)
                        produced_total += produced
                finally:
                    store_ptr(stream, 24, null())
                    store_i32(stream, 32, 0)
                    free(output)

                self.total_in = load_i64(stream, 16)
                if status == Z_STREAM_END:
                    self.eof = True
                    self._needs_input = False
                    if remaining:
                        self.unused_data += bytes(
                            data[input_size - remaining :]
                        )
                    break
                if status not in (Z_OK, Z_BUF_ERROR):
                    raise error("zlib decompression failed: " + str(status))
                if max_length > 0 and produced_total >= max_length:
                    if remaining:
                        self.unconsumed_tail = bytes(
                            data[input_size - remaining :]
                        )
                    # A full output fragment can leave decoded bytes buffered
                    # even after all input bytes were consumed.  The bounded
                    # reader must drain that state before feeding more input.
                    self._needs_input = False
                    break
                if remaining == 0 and produced < capacity:
                    self._needs_input = True
                    if finish and status == Z_BUF_ERROR:
                        break
                    if not finish:
                        break
                if produced == 0 and remaining == previous_remaining:
                    if remaining:
                        raise error("zlib decompressor made no progress")
                    self._needs_input = True
                    break
                previous_remaining = remaining

            remaining = load_i32(stream, 8) & 0xFFFFFFFF
            if not self.eof and remaining and not self.unconsumed_tail:
                self.unconsumed_tail = bytes(
                    data[input_size - remaining :]
                )
                self._needs_input = False
            return b"".join(chunks)
        finally:
            # Never retain a pointer into a managed bytes object across calls,
            # including allocation and bytes-materialization failures.
            store_ptr(stream, 0, null())
            store_i32(stream, 8, 0)

    def decompress(self, data, max_length=0):
        return self._decode(data, max_length, False)

    def flush(self, length=_STREAM_OUTPUT_CHUNK):
        if not isinstance(length, int):
            raise TypeError("length must be an integer")
        if length < 1:
            raise ValueError("length must be greater than zero")
        # CPython treats length as an allocation hint, not an output limit.
        # This implementation already uses a fixed 64 KiB native chunk.
        result = self._decode(b"", 0, True)
        if not self.eof:
            raise error("incomplete or truncated stream")
        return result

    def close(self):
        if self._closed:
            return
        if self._stream:
            call_i64_ptr1(int_to_ptr(self._end_fn), int_to_ptr(self._stream))
            free(int_to_ptr(self._stream))
        if self._handle:
            dynamic_library_close(int_to_ptr(self._handle))
        self._stream = 0
        self._handle = 0
        self._closed = True

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def decompressobj(wbits=MAX_WBITS, zdict=None):
    return Decompress(wbits, zdict)


def _decompress_with_consumed(
    data, wbits: int = MAX_WBITS, bufsize: int = 16384
):
    """Return one decoded stream and the number of input bytes it consumed.

    The consumed count is intentionally kept as a private seam.  Container
    modules such as :mod:`gzip` need it to process concatenated members, while
    public ``zlib.decompress`` keeps CPython's one-stream return contract.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise NotImplementedError(
            "zlib.decompress currently owns bytes and bytearray inputs"
        )
    if int(bufsize) < 1:
        raise ValueError("bufsize must be positive")
    decoder = Decompress(wbits)
    try:
        decoded = decoder.decompress(data)
        if not decoder.eof:
            decoded += decoder.flush()
        if not decoder.eof:
            raise error("incomplete or truncated stream")
        consumed = len(data) - len(decoder.unused_data)
        return decoded, consumed
    finally:
        decoder.close()


def decompress(data, wbits: int = MAX_WBITS, bufsize: int = 16384):
    decoded, _consumed = _decompress_with_consumed(data, wbits, bufsize)
    return decoded


def compressobj(
    level: int = Z_DEFAULT_COMPRESSION,
    method: int = Z_DEFLATED,
    wbits: int = MAX_WBITS,
    memLevel: int = DEF_MEM_LEVEL,
    strategy: int = Z_DEFAULT_STRATEGY,
    zdict=None,
):
    return Compress(
        level,
        method,
        wbits,
        memLevel,
        strategy,
        zdict,
    )


def compress(data, level: int = -1, wbits: int = MAX_WBITS):
    if not isinstance(data, (bytes, bytearray)):
        raise NotImplementedError(
            "zlib.compress currently owns bytes and bytearray inputs"
        )
    if not isinstance(level, int):
        raise TypeError("level must be an integer")
    if level < Z_DEFAULT_COMPRESSION or level > 9:
        raise error("Bad compression level")
    if not isinstance(wbits, int):
        raise TypeError("wbits must be an integer")
    if not (
        (-MAX_WBITS <= wbits <= -9)
        or (9 <= wbits <= MAX_WBITS)
        or (25 <= wbits <= 31)
    ):
        # CPython's one-shot wrapper maps every Z_STREAM_ERROR from
        # deflateInit2() to zlib.error("Bad compression level"), including a
        # bad wbits.  (compressobj() uses ValueError for its distinct API.)
        raise error("Bad compression level")
    input_size = len(data)
    if input_size > _MAX_COMPRESSION_INPUT:
        raise error("zlib compression input exceeds the 64 MiB limit")

    handle = _open_zlib()
    if ptr_is_null(handle):
        raise error("system zlib shared library is unavailable")
    stream = malloc(_Z_STREAM_SIZE)
    output = malloc(_INITIAL_COMPRESSION_OUTPUT)
    capacity = _INITIAL_COMPRESSION_OUTPUT
    initialized = False
    end_fn = null()
    if ptr_is_null(stream) or ptr_is_null(output):
        free(stream)
        free(output)
        dynamic_library_close(handle)
        raise MemoryError("unable to allocate zlib compression state")
    try:
        init_fn, deflate_fn, end_fn, version_fn = _resolve_deflate_symbols(
            handle
        )
        memset(stream, 0, _Z_STREAM_SIZE)
        version = call_ptr0(version_fn)
        status = call_i32_ptr_i32_i32_i32_i32_i32_ptr_i32(
            init_fn,
            stream,
            level,
            Z_DEFLATED,
            wbits,
            DEF_MEM_LEVEL,
            Z_DEFAULT_STRATEGY,
            version,
            _Z_STREAM_SIZE,
        )
        if status == Z_MEM_ERROR:
            raise MemoryError("out of memory while initializing zlib compression")
        if status != Z_OK:
            raise error("zlib deflate initialization failed: " + str(status))
        initialized = True
        store_ptr(stream, 0, _bytes_data(data))
        store_i32(stream, 8, input_size)
        store_ptr(stream, 24, output)
        store_i32(stream, 32, capacity)

        while True:
            status = call_i32_ptr_i32(deflate_fn, stream, Z_FINISH)
            if status == Z_STREAM_END:
                size = load_i64(stream, 40)
                if size > _MAX_COMPRESSED_OUTPUT:
                    raise error(
                        "compressed data exceeds the 128 MiB output limit"
                    )
                end_status = call_i32_ptr1(end_fn, stream)
                initialized = False
                if end_status != Z_OK:
                    raise error(
                        "zlib deflate finalization failed: " + str(end_status)
                    )
                result = _py_bytes_new(output, size)
                if ptr_is_null(result):
                    raise MemoryError("unable to allocate compressed zlib bytes")
                return result
            available = load_i32(stream, 32) & 0xFFFFFFFF
            if available == 0 and status in (Z_OK, Z_BUF_ERROR):
                grown, new_capacity = _grow_compression_output(
                    stream, output, capacity
                )
                if ptr_is_null(grown):
                    raise error(
                        "compressed data exceeds the 128 MiB output limit"
                    )
                output = grown
                capacity = new_capacity
                continue
            raise error("zlib compression failed: " + str(status))
    finally:
        if initialized:
            call_i32_ptr1(end_fn, stream)
        free(output)
        free(stream)
        dynamic_library_close(handle)


__all__ = [
    "error",
    "crc32",
    "compress",
    "compressobj",
    "Compress",
    "decompressobj",
    "Decompress",
    "decompress",
    "MAX_WBITS",
    "DEF_MEM_LEVEL",
    "Z_DEFAULT_COMPRESSION",
    "Z_DEFLATED",
    "Z_DEFAULT_STRATEGY",
    "Z_NO_FLUSH",
    "Z_PARTIAL_FLUSH",
    "Z_SYNC_FLUSH",
    "Z_FULL_FLUSH",
    "Z_FINISH",
    "Z_BLOCK",
    "Z_OK",
    "Z_STREAM_END",
    "Z_MEM_ERROR",
    "Z_BUF_ERROR",
]
