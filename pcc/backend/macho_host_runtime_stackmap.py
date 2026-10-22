"""Bounded runtime-table emission from private, already validated v2 spools.

Only the owned host merger calls this helper. Public transcoding stays the
byte oracle. No merged v2 backing or per-location Python graph is created.
"""

from contextlib import ExitStack
import io
import sys
import tempfile

from . import precise_stackmap as wire
from .macho_host_stackmap_index import _SourceIndex

_BLOCK = 64 * 1024
# Five buffered temporary files, input/conversion windows and scalar/codec
# objects. File buffers can coexist with a write argument and flush scratch.
_FIXED_SCALARS = 16384


def _records(stream, count, work, chunk):
    while count:
        number = min(count, len(chunk) // wire.RECORD_SIZE)
        with memoryview(chunk)[:number * wire.RECORD_SIZE] as source:
            work.read_exact(stream, source)
            for position in range(number):
                yield wire._RECORD.unpack_from(source, position * wire.RECORD_SIZE)
        count -= number


def emit_runtime_table(functions, records, roots, table_locations, arch, *,
                       budget, single_buffer, work, base_entries, index_work):
    """Consume sorted descriptors and return exact generic-transcoder bytes.

    The caller keeps all other owners in budget.retained_bytes. Its function
    list remains charged until cleared; offset tuple/list freeze overlap is
    covered by five metadata entries per function, as in the v2 merger.
    Existing spool read/write limits apply unchanged, including prior work.
    """
    index = None
    buffer = view = closing = result = chunk = converted = record_chunk = None
    offsets = []
    count_functions = len(functions)
    entries = base_entries + 5 * count_functions
    source_size = sum(item[2] for item in functions) + table_locations * wire.LOCATION_SIZE
    block = min(_BLOCK, max(512, (source_size // 16 // 32) * 32))
    fixed = 16 * block + _FIXED_SCALARS

    def admit(extra=0, label="runtime spool workspace"):
        budget.check_peak(fixed + (index.owned_bytes() if index else 0) + extra,
                          entries=entries, label=label)

    admit()
    try:
        index = _SourceIndex(budget, index_work, admit)
        with ExitStack() as files:
            outputs = [files.enter_context(tempfile.TemporaryFile(
                mode="w+b", buffering=block)) for _ in range(5)]
            fn_out, rec_out, set_out, loc_out, rare_out = outputs
            record_count = set_count = location_count = rare_count = 0
            empty_set = -1
            chunk = bytearray(block)
            record_chunk = bytearray(block)
            converted = bytearray(block // wire.LOCATION_SIZE * wire.RUNTIME_LOCATION_SIZE)
            for ordinal, (function_id, start, size) in enumerate(functions):
                records.seek(start)
                with memoryview(chunk)[:wire.FUNCTION_SIZE] as target:
                    work.read_exact(records, target)
                    fields = wire._FUNCTION.unpack(target)
                fid, address, code_size, frame_size, count, _flags = fields
                if fid != function_id or size != wire.FUNCTION_SIZE + count * wire.RECORD_SIZE:
                    raise wire.PreciseStackMapError("runtime spool function descriptor disagrees")
                offsets.append((fid, wire.RUNTIME_HEADER_SIZE + ordinal * wire.RUNTIME_FUNCTION_SIZE + 8))
                work.write_all(fn_out, wire._RUNTIME_FUNCTION.pack(
                    fid, address, code_size, frame_size, record_count, count))
                # Separate windows keep prefetched records alive while roots
                # are read, without one syscall per safepoint or root.
                for row in _records(records, count, work, record_chunk):
                    (_safe, pc, exceptional, continuation, n, _r1,
                     kind, flags, _r2, location_index) = row
                    if location_index + n > table_locations:
                        raise wire.PreciseStackMapError("safepoint locations exceed the table")
                    if n:
                        key = (location_index << 16) | n
                        live_set, slot = index.find(key)
                    else:
                        live_set, slot = empty_set, -1
                    if live_set < 0:
                        live_set = set_count
                        set_count += 1
                        if n:
                            index.append(key, live_set, slot)
                        else:
                            empty_set = live_set
                        work.write_all(set_out, wire._RUNTIME_LIVE_SET.pack(location_count, n))
                        roots.seek(location_index * wire.LOCATION_SIZE)
                        remaining = n
                        while remaining:
                            number = min(remaining, block // wire.LOCATION_SIZE)
                            with memoryview(chunk)[:number * wire.LOCATION_SIZE] as source:
                                work.read_exact(roots, source)
                                for position in range(number):
                                    location = wire._LOCATION.unpack_from(source, position * wire.LOCATION_SIZE)
                                    word = wire._runtime_location_word(location, arch)
                                    wire._RUNTIME_LOCATION.pack_into(converted, position * wire.RUNTIME_LOCATION_SIZE, word)
                            with memoryview(converted)[:number * wire.RUNTIME_LOCATION_SIZE] as value:
                                work.write_all(loc_out, value)
                            remaining -= number
                        location_count += n
                    if (kind not in (wire.SAFEPOINT_ENTRY, wire.SAFEPOINT_LOOP, wire.SAFEPOINT_CALL)
                            or flags or exceptional != wire.NO_OFFSET or continuation):
                        work.write_all(rare_out, wire._RUNTIME_RARE.pack(
                            record_count, kind, flags, 0, exceptional, continuation))
                        rare_count += 1
                    work.write_all(rec_out, wire._RUNTIME_RECORD.pack(pc, live_set))
                    record_count += 1
            index.close()
            index = None
            functions.clear()
            chunk = converted = record_chunk = None
            header = wire._RUNTIME_HEADER.pack(
                wire.MAGIC, wire.RUNTIME_TABLE_VERSION, arch, wire.POINTER_SIZE,
                count_functions, record_count, set_count, location_count, rare_count, 0, 0)
            sizes = (count_functions * wire.RUNTIME_FUNCTION_SIZE,
                     record_count * wire.RUNTIME_RECORD_SIZE,
                     set_count * wire.RUNTIME_LIVE_SET_SIZE,
                     location_count * wire.RUNTIME_LOCATION_SIZE,
                     rare_count * wire.RUNTIME_RARE_SIZE)
            total = len(header) + sum(sizes)
            copies = 1 if single_buffer else 2
            admit(copies * budget._rounded(sys.getsizeof(b"") + total),
                  "runtime spool materialize")
            buffer = io.BytesIO(b"\0" * total) if single_buffer else bytearray(total)
            view = buffer.getbuffer() if single_buffer else memoryview(buffer)
            view[:len(header)] = header
            cursor = len(header)
            for source, size in zip(outputs, sizes):
                source.flush()
                source.seek(0)
                with view[cursor:cursor + size] as target:
                    work.read_exact(source, target)
                cursor += size
            view.release()
            view = None
            result = buffer.getvalue() if single_buffer else bytes(buffer)
            if single_buffer:
                closing, buffer = buffer, None
                closing.close()
                closing = None
            buffer = None
            return result, tuple(offsets)
    finally:
        if view is not None:
            view.release()
        if closing is not None:
            closing.close()
        if single_buffer and buffer is not None:
            buffer.close()
        if index is not None:
            index.close()
        offsets.clear()
        chunk = converted = record_chunk = buffer = result = view = closing = None
