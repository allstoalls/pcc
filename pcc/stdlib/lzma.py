"""Native XZ/LZMA codec for pcc build-tool programs.

Compression and decompression each own a reusable ``lzma_stream`` and
materialize output in fixed 64 KiB fragments.  Custom filters and
FORMAT_ALONE/FORMAT_RAW encoding remain fail-closed instead of approximated.
"""
from __future__ import annotations

import io

try:
    from pcc.stdlib._compression_stream import CompressionWriter, DecompressReader
except ImportError:
    # pcc publishes this provider as the top-level stdlib module ``lzma``.
    from _compression_stream import CompressionWriter, DecompressReader

from pcc.extern import c_int64, c_ptr, c_rawptr, extern, c_obj
from pcc.unsafe import (
    call_i32_ptr1,
    call_i32_ptr_i32,
    call_i32_ptr_i32_i32,
    call_i64_ptr_i64_i64,
    call_void_ptr1,
    cstr,
    dynamic_library_close,
    dynamic_library_open,
    dynamic_library_symbol,
    free,
    int_to_ptr,
    load_i64,
    malloc,
    memset,
    null,
    ptr_add,
    ptr_is_null,
    ptr_to_int,
    realloc,
    store_i64,
    store_ptr,
)


# The data operand is a raw native buffer address, never an object.
_py_bytes_new: "extern" = extern("py_bytes_new", (c_rawptr, c_int64), c_obj)

FORMAT_AUTO = 0
FORMAT_XZ = 1
FORMAT_ALONE = 2
FORMAT_RAW = 3

CHECK_NONE = 0
CHECK_CRC32 = 1
CHECK_CRC64 = 4
CHECK_SHA256 = 10
CHECK_UNKNOWN = 16

FILTER_LZMA1 = 0x4000000000000001
FILTER_LZMA2 = 0x21
PRESET_DEFAULT = 6
PRESET_EXTREME = 1 << 31

_LZMA_OK = 0
_LZMA_STREAM_END = 1
_LZMA_GET_CHECK = 4
_LZMA_BUF_ERROR = 10
_LZMA_RUN = 0
_LZMA_FINISH = 3
_LZMA_TELL_ANY_CHECK = 4
_LZMA_STREAM_SIZE = 136
_MEMORY_LIMIT = 256 * 1024 * 1024
_STREAM_OUTPUT_CHUNK = 64 * 1024
_INITIAL_OUTPUT = 16384
_MAX_COMPRESSION_INPUT = 64 * 1024 * 1024
_MAX_COMPRESSED_OUTPUT = 128 * 1024 * 1024
_COMPRESSION_OUTPUT_SENTINEL_CAPACITY = _MAX_COMPRESSED_OUTPUT + 1


class LZMAError(Exception):
    pass


def _validate_decoder_options(format, memlimit, filters):
    if format != FORMAT_AUTO:
        raise NotImplementedError(
            "LZMADecompressor owns automatic XZ/LZMA detection only"
        )
    if filters is not None:
        raise NotImplementedError("custom lzma filters are not runtime-owned")
    if memlimit is not None and int(memlimit) != _MEMORY_LIMIT:
        raise NotImplementedError(
            "custom lzma decoder memory limits are not runtime-owned"
        )


def _open_liblzma() -> int:
    handle = dynamic_library_open(
        cstr("/usr/local/lib/liblzma.5.dylib"), "darwin"
    )
    if ptr_is_null(handle):
        handle = dynamic_library_open(
            cstr("/opt/homebrew/lib/liblzma.5.dylib"), "darwin"
        )
    if ptr_is_null(handle):
        handle = dynamic_library_open(
            cstr("/opt/homebrew/opt/xz/lib/liblzma.5.dylib"), "darwin"
        )
    if ptr_is_null(handle):
        handle = dynamic_library_open(
            cstr("/usr/local/opt/xz/lib/liblzma.5.dylib"), "darwin"
        )
    if ptr_is_null(handle):
        handle = dynamic_library_open(cstr("liblzma.5.dylib"), "darwin")
    if ptr_is_null(handle):
        handle = dynamic_library_open(cstr("liblzma.so.5"), "linux")
    return handle


def _decoder_symbols(handle: int) -> tuple[int, int, int, int]:
    init_fn = dynamic_library_symbol(handle, cstr("lzma_auto_decoder"))
    code_fn = dynamic_library_symbol(handle, cstr("lzma_code"))
    end_fn = dynamic_library_symbol(handle, cstr("lzma_end"))
    check_fn = dynamic_library_symbol(handle, cstr("lzma_get_check"))
    if (
        ptr_is_null(init_fn)
        or ptr_is_null(code_fn)
        or ptr_is_null(end_fn)
        or ptr_is_null(check_fn)
    ):
        raise LZMAError("system liblzma is missing the decoder ABI")
    return init_fn, code_fn, end_fn, check_fn


def _encoder_symbols(handle: int) -> tuple[int, int, int]:
    init_fn = dynamic_library_symbol(handle, cstr("lzma_easy_encoder"))
    code_fn = dynamic_library_symbol(handle, cstr("lzma_code"))
    end_fn = dynamic_library_symbol(handle, cstr("lzma_end"))
    if ptr_is_null(init_fn) or ptr_is_null(code_fn) or ptr_is_null(end_fn):
        raise LZMAError("system liblzma is missing the encoder ABI")
    return init_fn, code_fn, end_fn


def _bytes_data(data) -> int:
    return ptr_add(data, 24)


class LZMADecompressor:
    """One-member incremental decoder backed by an owned ``lzma_stream``."""

    def __init__(self, format=FORMAT_AUTO, memlimit=None, filters=None):
        _validate_decoder_options(format, memlimit, filters)

        self._handle = 0
        self._stream = 0
        self._code_fn = 0
        self._end_fn = 0
        self._check_fn = 0
        self._closed = False
        self._pending = b""
        self.eof = False
        self.unused_data = b""
        self.needs_input = True
        self.check = CHECK_UNKNOWN

        handle = _open_liblzma()
        if ptr_is_null(handle):
            raise LZMAError("system liblzma shared library is unavailable")
        stream = malloc(_LZMA_STREAM_SIZE)
        if ptr_is_null(stream):
            dynamic_library_close(handle)
            raise MemoryError("unable to allocate lzma decompression state")
        try:
            init_fn, code_fn, end_fn, check_fn = _decoder_symbols(handle)
            memset(stream, 0, _LZMA_STREAM_SIZE)
            status = call_i64_ptr_i64_i64(
                init_fn, stream, _MEMORY_LIMIT, _LZMA_TELL_ANY_CHECK
            )
            if status != _LZMA_OK:
                raise LZMAError(
                    "lzma decoder initialization failed: " + str(status)
                )
        except Exception:
            free(stream)
            dynamic_library_close(handle)
            raise
        self._handle = ptr_to_int(handle)
        self._stream = ptr_to_int(stream)
        self._code_fn = ptr_to_int(code_fn)
        self._end_fn = ptr_to_int(end_fn)
        self._check_fn = ptr_to_int(check_fn)

    def decompress(self, data, max_length=-1):
        if self._closed:
            raise LZMAError("lzma decompressor is closed")
        if self.eof:
            raise EOFError("Already at end of stream")
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("a bytes-like object is required")
        if not isinstance(max_length, int):
            raise TypeError("max_length must be an integer")
        if max_length < -1:
            raise ValueError("max_length must be non-negative")

        payload = self._pending + bytes(data)
        self._pending = b""
        if max_length == 0:
            self._pending = payload
            self.needs_input = not bool(self._pending)
            return b""

        stream = int_to_ptr(self._stream)
        code_fn = int_to_ptr(self._code_fn)
        input_size = len(payload)
        store_ptr(stream, 0, _bytes_data(payload) if input_size else null())
        store_i64(stream, 8, input_size)
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
                    raise MemoryError("unable to allocate lzma output chunk")
                try:
                    store_ptr(stream, 24, output)
                    store_i64(stream, 32, capacity)
                    status = call_i32_ptr_i32(code_fn, stream, _LZMA_RUN)
                    available = load_i64(stream, 32)
                    remaining = load_i64(stream, 8)
                    produced = capacity - available
                    if status in (_LZMA_GET_CHECK, _LZMA_STREAM_END):
                        self.check = call_i32_ptr1(
                            int_to_ptr(self._check_fn), stream
                        )
                    if produced:
                        chunk = _py_bytes_new(output, produced)
                        if ptr_is_null(chunk):
                            raise MemoryError(
                                "unable to allocate decompressed lzma bytes"
                            )
                        chunks.append(chunk)
                        produced_total += produced
                finally:
                    store_ptr(stream, 24, null())
                    store_i64(stream, 32, 0)
                    free(output)

                if status == _LZMA_STREAM_END:
                    self.eof = True
                    self.needs_input = False
                    if remaining:
                        self.unused_data = bytes(
                            payload[input_size - remaining :]
                        )
                    break
                if status not in (
                    _LZMA_OK,
                    _LZMA_GET_CHECK,
                    _LZMA_BUF_ERROR,
                ):
                    raise LZMAError(
                        "lzma decompression failed: " + str(status)
                    )
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
                        raise LZMAError("lzma decompressor made no progress")
                    self.needs_input = True
                    break
                previous_remaining = remaining

            remaining = load_i64(stream, 8)
            if not self.eof and remaining and not self._pending:
                self._pending = bytes(payload[input_size - remaining :])
                self.needs_input = False
            return b"".join(chunks)
        finally:
            store_ptr(stream, 0, null())
            store_i64(stream, 8, 0)

    def close(self):
        if self._closed:
            return
        if self._stream:
            call_void_ptr1(
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


def _new_lzma_decoder():
    return LZMADecompressor()


def decompress(data, format=FORMAT_AUTO, memlimit=None, filters=None):
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("a bytes-like object is required")
    if len(data) == 0:
        raise LZMAError("Compressed data ended before the end-of-stream marker")
    _validate_decoder_options(format, memlimit, filters)
    reader = DecompressReader(
        io.BytesIO(bytes(data)),
        _new_lzma_decoder,
        trailing_error=LZMAError,
    )
    try:
        return reader.read()
    except EOFError as exc:
        raise LZMAError(str(exc))
    finally:
        reader.close()


def _encoder_options(format, check, preset, filters):
    if format != FORMAT_XZ:
        raise NotImplementedError(
            "lzma compression currently owns FORMAT_XZ encoding only"
        )
    if filters is not None:
        raise NotImplementedError("custom lzma filters are not runtime-owned")
    if preset is None:
        selected_preset = PRESET_DEFAULT
    else:
        if not isinstance(preset, int):
            raise TypeError("preset must be an integer or None")
        selected_preset = preset
    base_preset = selected_preset & 0x7FFFFFFF
    if (
        selected_preset < 0
        or base_preset > 9
        or selected_preset
        not in (base_preset, base_preset | PRESET_EXTREME)
    ):
        raise LZMAError("Invalid or unsupported options")
    if not isinstance(check, int):
        raise TypeError("check must be an integer")
    selected_check = check
    if selected_check == -1:
        selected_check = CHECK_CRC64
    if selected_check not in (
        CHECK_NONE,
        CHECK_CRC32,
        CHECK_CRC64,
        CHECK_SHA256,
    ):
        raise LZMAError("Invalid or unsupported integrity check")
    return selected_preset, selected_check


def compress(data, format=FORMAT_XZ, check=-1, preset=None, filters=None):
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("a bytes-like object is required")
    selected_preset, selected_check = _encoder_options(
        format, check, preset, filters
    )
    input_size = len(data)
    if input_size > _MAX_COMPRESSION_INPUT:
        raise LZMAError("lzma compression input exceeds the 64 MiB limit")

    handle = _open_liblzma()
    if ptr_is_null(handle):
        raise LZMAError("system liblzma shared library is unavailable")
    stream = malloc(_LZMA_STREAM_SIZE)
    output = malloc(_INITIAL_OUTPUT)
    capacity = _INITIAL_OUTPUT
    initialized = False
    end_fn = null()
    if ptr_is_null(stream) or ptr_is_null(output):
        free(stream)
        free(output)
        dynamic_library_close(handle)
        raise MemoryError("unable to allocate lzma compression state")
    try:
        init_fn, code_fn, end_fn = _encoder_symbols(handle)
        memset(stream, 0, _LZMA_STREAM_SIZE)
        status = call_i32_ptr_i32_i32(
            init_fn, stream, selected_preset, selected_check
        )
        if status != _LZMA_OK:
            raise LZMAError(
                "lzma encoder initialization failed: " + str(status)
            )
        initialized = True
        store_ptr(stream, 0, _bytes_data(data))
        store_i64(stream, 8, input_size)
        store_ptr(stream, 24, output)
        store_i64(stream, 32, capacity)
        previous_total = -1
        previous_remaining = -1

        while True:
            status = call_i32_ptr_i32(code_fn, stream, _LZMA_FINISH)
            total = load_i64(stream, 40)
            if status == _LZMA_STREAM_END:
                if total > _MAX_COMPRESSED_OUTPUT:
                    raise LZMAError(
                        "compressed data exceeds the 128 MiB output limit"
                    )
                result = _py_bytes_new(output, total)
                if ptr_is_null(result):
                    raise MemoryError("unable to allocate compressed lzma bytes")
                return result
            available = load_i64(stream, 32)
            remaining = load_i64(stream, 8)
            if status in (_LZMA_OK, _LZMA_BUF_ERROR) and available == 0:
                new_capacity = capacity * 2
                if new_capacity > _COMPRESSION_OUTPUT_SENTINEL_CAPACITY:
                    new_capacity = _COMPRESSION_OUTPUT_SENTINEL_CAPACITY
                if new_capacity <= capacity:
                    raise LZMAError(
                        "compressed data exceeds the 128 MiB output limit"
                    )
                grown = realloc(output, new_capacity)
                if ptr_is_null(grown):
                    raise MemoryError("unable to grow lzma output buffer")
                output = grown
                capacity = new_capacity
                store_ptr(stream, 24, ptr_add(output, total))
                store_i64(stream, 32, capacity - total)
                previous_total = total
                previous_remaining = remaining
                continue
            if status == _LZMA_OK:
                if total == previous_total and remaining == previous_remaining:
                    raise LZMAError("lzma compressor made no progress")
                previous_total = total
                previous_remaining = remaining
                continue
            raise LZMAError("lzma compression failed: " + str(status))
    finally:
        if initialized:
            call_void_ptr1(end_fn, stream)
        free(output)
        free(stream)
        dynamic_library_close(handle)


class LZMACompressor:
    def __init__(self, format=FORMAT_XZ, check=-1, preset=None, filters=None):
        selected_preset, selected_check = _encoder_options(
            format, check, preset, filters
        )
        self._handle = 0
        self._stream = 0
        self._code_fn = 0
        self._end_fn = 0
        self._closed = False

        handle = _open_liblzma()
        if ptr_is_null(handle):
            raise LZMAError("system liblzma shared library is unavailable")
        stream = malloc(_LZMA_STREAM_SIZE)
        if ptr_is_null(stream):
            dynamic_library_close(handle)
            raise MemoryError("unable to allocate lzma compression state")
        try:
            init_fn, code_fn, end_fn = _encoder_symbols(handle)
            memset(stream, 0, _LZMA_STREAM_SIZE)
            status = call_i32_ptr_i32_i32(
                init_fn, stream, selected_preset, selected_check
            )
            if status != _LZMA_OK:
                raise LZMAError(
                    "lzma encoder initialization failed: " + str(status)
                )
        except Exception:
            free(stream)
            dynamic_library_close(handle)
            raise
        self._handle = ptr_to_int(handle)
        self._stream = ptr_to_int(stream)
        self._code_fn = ptr_to_int(code_fn)
        self._end_fn = ptr_to_int(end_fn)

    def _release(self):
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
        if not ptr_is_null(stream):
            if not ptr_is_null(end_fn):
                call_void_ptr1(end_fn, stream)
            free(stream)
        if not ptr_is_null(handle):
            dynamic_library_close(handle)

    def _run(self, data, action, finishing):
        if self._closed:
            raise ValueError("Compressor has been flushed")
        if not isinstance(data, (bytes, bytearray)):
            raise TypeError("a bytes-like object is required")
        input_size = len(data)
        if input_size == 0 and action == _LZMA_RUN:
            return b""

        stream = int_to_ptr(self._stream)
        code_fn = int_to_ptr(self._code_fn)
        store_ptr(stream, 0, _bytes_data(data) if input_size else null())
        store_i64(stream, 8, input_size)
        chunks = []
        previous_remaining = input_size + 1
        try:
            while True:
                output = malloc(_STREAM_OUTPUT_CHUNK)
                if ptr_is_null(output):
                    raise MemoryError("unable to allocate lzma output chunk")
                try:
                    store_ptr(stream, 24, output)
                    store_i64(stream, 32, _STREAM_OUTPUT_CHUNK)
                    status = call_i32_ptr_i32(code_fn, stream, action)
                    available = load_i64(stream, 32)
                    remaining = load_i64(stream, 8)
                    produced = _STREAM_OUTPUT_CHUNK - available
                    if produced:
                        chunk = _py_bytes_new(output, produced)
                        if ptr_is_null(chunk):
                            raise MemoryError(
                                "unable to allocate compressed lzma bytes"
                            )
                        chunks.append(chunk)
                finally:
                    store_ptr(stream, 24, null())
                    store_i64(stream, 32, 0)
                    free(output)

                if finishing and status == _LZMA_STREAM_END:
                    self._release()
                    return b"".join(chunks)
                if status != _LZMA_OK:
                    raise LZMAError("lzma compression failed: " + str(status))
                if not finishing and remaining == 0 and available > 0:
                    return b"".join(chunks)
                if produced == 0 and remaining == previous_remaining:
                    raise LZMAError("lzma compressor made no progress")
                previous_remaining = remaining
        except Exception:
            self._release()
            raise
        finally:
            if self._stream:
                active = int_to_ptr(self._stream)
                store_ptr(active, 0, null())
                store_i64(active, 8, 0)

    def compress(self, data):
        return self._run(data, _LZMA_RUN, False)

    def flush(self):
        return self._run(b"", _LZMA_FINISH, True)

    def close(self):
        self._release()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def open(filename, mode="rb", *, format=None, check=-1, preset=None,
         filters=None, encoding=None, errors=None, newline=None):
    normalized = mode.replace("t", "").replace("b", "")
    if (
        "t" in mode
        or encoding is not None
        or errors is not None
        or newline is not None
    ):
        raise NotImplementedError("lzma text mode is not yet runtime-owned")
    if normalized == "r":
        if format is not None and format != FORMAT_AUTO:
            raise NotImplementedError("forced lzma stream formats are not runtime-owned")
        if filters is not None:
            raise NotImplementedError("custom lzma filters are not runtime-owned")
        _validate_decoder_options(
            FORMAT_AUTO if format is None else format,
            None,
            filters,
        )
        return DecompressReader(
            filename,
            _new_lzma_decoder,
            trailing_error=LZMAError,
        )
    if normalized in ("w", "a", "x"):
        return LZMAFile(
            filename,
            normalized + "b",
            format=FORMAT_XZ if format is None else format,
            check=check,
            preset=preset,
            filters=filters,
        )
    raise ValueError("Invalid mode: " + str(mode))


class LZMAFile:
    def __init__(self, filename=None, mode="r", *, format=None, check=-1,
                 preset=None, filters=None):
        normalized = mode.replace("b", "")
        if normalized == "r":
            selected_format = FORMAT_AUTO if format is None else format
            _validate_decoder_options(selected_format, None, filters)
            self._stream = DecompressReader(
                filename,
                _new_lzma_decoder,
                trailing_error=LZMAError,
            )
            self._writing = False
        elif normalized in ("w", "a", "x"):
            selected_format = FORMAT_XZ if format is None else format
            self._stream = CompressionWriter(
                filename,
                LZMACompressor(
                    selected_format,
                    check,
                    preset,
                    filters,
                ),
                normalized + "b",
            )
            self._writing = True
        else:
            raise ValueError("Invalid mode: " + str(mode))

    def read(self, size=-1):
        if self._writing:
            raise OSError("read() on write-only LZMAFile object")
        return self._stream.read(size)

    def readline(self, size=-1):
        if self._writing:
            raise OSError("read() on write-only LZMAFile object")
        return self._stream.readline(size)

    def write(self, data):
        if not self._writing:
            raise OSError("write() on read-only LZMAFile object")
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
    "LZMAError",
    "LZMACompressor",
    "LZMADecompressor",
    "LZMAFile",
    "open",
    "compress",
    "decompress",
    "FORMAT_AUTO",
    "FORMAT_XZ",
    "FORMAT_ALONE",
    "FORMAT_RAW",
    "CHECK_NONE",
    "CHECK_CRC32",
    "CHECK_CRC64",
    "CHECK_SHA256",
    "CHECK_UNKNOWN",
    "FILTER_LZMA1",
    "FILTER_LZMA2",
    "PRESET_DEFAULT",
    "PRESET_EXTREME",
]
