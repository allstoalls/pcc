"""Private consuming CPython finalization with file-owned output assembly.

The ordinary/native bytes API and its serializer remain the reference. This
route takes an exclusive windowed source, patches its exact-sized backings,
and retires the preparation graph before allocating the final image.
"""

import io
import os
import struct
import sys
import tempfile

from . import macho_exec as executable
from . import macho_codesign as codesign
from . import precise_stackmap as stackmap
from .macho_host_prepare import (
    close_staged_source_rows,
    _patch_staged_payload,
    _release_closed_tracebacks,
    _single_buffer_window_supported,
)
from .macho_host_relocations import _model_bytes
from .macho_host_phase import emit_phase
from . import macho_host_validation as host_validation
from .macho_link import LinkError
from .native_object import OwnedMergedSourceView


_BLOCK = 64 * 1024
_FIXED = 16 * 1024
_BYTES_IO_HEADER = sys.getsizeof(io.BytesIO())
_VIEW_HEADER = sys.getsizeof(memoryview(b""))
_SOURCE_FIELDS = (
    "_source_sections", "_payloads", "_raw_relocations", "_sections",
    "_symbols", "_section_index", "_symbol_index",
)


class _FilePayload:
    """Size-only section owner until relocation, then one private file extent."""

    __slots__ = ("size", "offset")

    def __init__(self, size):
        self.size = size
        self.offset = None

    def __len__(self):
        return self.size


def _list_storage(count, budget, *, lists=1, sorting=False):
    # CPython append growth is (n + n//8 + 6)&~3. Count both the old
    # and replacement pointer arrays, even when realloc can grow in place.
    # key-sort additionally has n keys and up to n merge-scratch pointers.
    capacity = count + count // 8 + 6 * lists
    return (lists * budget._rounded(sys.getsizeof([]))
            + 2 * budget._rounded(capacity * 8)
            + (2 * count * 8 if sorting else 0))


def _dict_storage(count, budget):
    # Insert-only generic-key tables cover the smaller unicode-key tables.
    # CPython 3.13--3.15 uses 2/3 usable slots and doubles at used*3. Each
    # keys allocation includes its 32-byte header, indices and 3-word entries.
    # No borrowed key/value object is charged a second time here.
    header = budget._rounded(sys.getsizeof({}))
    if not count:
        return header
    capacity = 8
    while capacity * 2 // 3 < count:
        capacity *= 2

    def table(slots):
        width = 1 if slots <= 128 else 2 if slots <= 32768 else 4 if slots <= 1 << 31 else 8
        return budget._rounded(32 + slots * width + (slots * 2 // 3) * 24)

    return header + table(capacity) + (table(capacity // 2) if capacity > 8 else 0)


class _SetStorage:
    """Monotone table-only peak, O(1) amortized per admitted fixup."""

    def __init__(self, budget):
        self.budget = budget
        self.capacity = 8
        self.peak = budget._rounded(sys.getsizeof(set()))

    def reserve(self, count):
        budget = self.budget
        header = budget._rounded(sys.getsizeof(set()))
        while True:
            threshold = ((self.capacity - 1) * 3 + 4) // 5
            if threshold > count:
                break
            target = threshold * (2 if threshold > 50000 else 4)
            following = 8
            while following <= target:
                following *= 2
            self.peak = max(self.peak, header
                            + budget._rounded(self.capacity * 16)
                            + budget._rounded(following * 16))
            self.capacity = following
        self.peak = max(self.peak, header + budget._rounded(self.capacity * 16))
        return self.peak


def _symbol_projections(source, budget):
    """New shallow owners in _prepare_executable_image, excluding source data."""
    defined = sum((s["n_type"] & 0x0e) == 0x0e for s in source._symbols)
    imports = sum((s["n_type"] & 0x0e) == 0 for s in source._symbols)
    count = len(source._symbols)
    integer = budget._rounded(sys.getsizeof((1 << 64) - 1))
    pair = budget._rounded(sys.getsizeof((None, None)))
    # defined/thread_block_offset/sym_addr; veneer_index/veneer_used/
    # string_offsets; stub_addr/got_addr/import_ordinal, respectively.
    total = (3 * _dict_storage(defined, budget)
             + 3 * _dict_storage(count, budget)
             + 3 * _dict_storage(imports, budget))
    # weak/relocated/imports sets plus both unreferenced-import temporaries;
    # code_imports_set and branch_targets_set also contain defined names.
    total += (5 * _SetStorage(budget).reserve(imports)
              + 2 * _SetStorage(budget).reserve(count)
              + _SetStorage(budget).reserve(defined))  # dropped_symbols
    # names_all, branch_targets, string-table input/chunks and nlist_parts;
    # imports, code_imports, unreferenced/missing imports, import_entries;
    # defined_items, local/external_defs, ordered and input-name temporary.
    total += (5 * _list_storage(count, budget, sorting=True)
              + 5 * _list_storage(imports, budget, sorting=True)
              + 5 * _list_storage(defined, budget, sorting=True))
    # New map values, import entries/packing argument tuple, defined-item
    # pairs and the separate address/name key tuples used by symbol sorting.
    total += ((2 * defined + 3 * count + 4 * imports) * integer
              + 2 * defined * pair + imports * 8)
    names_bytes = 1 + sum(len(s["name"]) + 1 for s in source._symbols)
    import_bytes = 1 + sum(len(s["name"]) + 1 for s in source._symbols
                           if (s["n_type"] & 0x0e) == 0)
    # String-table chunks plus joined strtab, with one encoding temporary.
    total += (sum(budget._rounded(sys.getsizeof(b"") + len(s["name"]) + 1)
                  for s in source._symbols)
              + 2 * names_bytes + 16)
    # 16-byte nlist objects and their final joined wire bytes.
    total += count * (budget._rounded(sys.getsizeof(b"") + 16) + 16)
    # sym_blob's old/new overallocated bytearray, its bytes conversion and
    # chained-fixups concatenation intermediates. Page starts are separate.
    total += (2 * budget._rounded(import_bytes + import_bytes // 8 + 8)
              + import_bytes + 3 * (4 * imports + import_bytes + 64))
    return total


def _section_projections(count, budget):
    integer = budget._rounded(sys.getsizeof((1 << 64) - 1))
    # The managed-buffer struct is smaller than PyMemoryViewObject: it
    # contains the same Py_buffer plus fewer header/pointer fields. Use
    # another full view size. _BytesIOBuffer has object head, one pointer
    # to the BytesIO owner and the two-word GC preheader.
    buffer_headers = (budget._rounded(_BYTES_IO_HEADER)
                      + 2 * budget._rounded(_VIEW_HEADER)
                      + budget._rounded(sys.getsizeof(object()) + 3 * 8))
    # _Out + its ten-field attributes, derived address integers, BytesIO /
    # managed-buffer / primary-view headers, region triples/labels and up to
    # four simultaneous 80-byte section-command encodings. Source payloads
    # and source names themselves remain in the separate source model.
    per_section = (budget._rounded(sys.getsizeof(executable._Out("", "", 0, 0, None)))
                   + _dict_storage(10, budget) + 12 * integer
                   + buffer_headers + _list_storage(3, budget)
                   # Region sort's (offset, len(payload), label) key tuple;
                   # its new len integer is one of the twelve above.
                   + budget._rounded(sys.getsizeof((None, None, None)))
                   + budget._rounded(sys.getsizeof("") + 40) + 4 * 80)
    # Output lists and sorted/concatenated temporary lists, section_addr and
    # both private id-indexed owner maps. Each section_addr value is a pair.
    return (count * (per_section + budget._rounded(sys.getsizeof((None, None)))
                    + budget._rounded(sys.getsizeof(_FilePayload(0))) + 2 * integer)
            + 10 * _list_storage(count, budget, sorting=True)
            + 3 * _dict_storage(count, budget))


def _row_storage_bytes(source):
    storage = getattr(source, "_host_row_store", None)
    if storage is None:
        return 0
    # Reserve the largest single replay throughout preparation. Disk extent
    # is not resident storage, and active_bytes is covered by this window.
    return (storage.metadata_bytes + storage.max_window_bytes
            + storage.public_copy_bytes)


def _source_bytes(source, budget):
    # These are fixed source models, never _Out/fixup graphs whose repeated
    # references could multiply one backing by the relocation count.
    payloads = getattr(source, "_host_payload_store", None)
    return (sum(_model_bytes(getattr(source, name), budget) for name in _SOURCE_FIELDS)
            + _row_storage_bytes(source)
            + (payloads.metadata_bytes + payloads.public_copy_bytes
               + _model_bytes(source._host_payload_layout, budget) if payloads is not None else 0))


def _clear_source(source, *, _error=None):
    try:
        close_staged_source_rows(source, _error=_error)
    finally:
        if hasattr(source, "_host_payload_layout"):
            source._host_payload_layout = ()
        for name in _SOURCE_FIELDS:
            value = getattr(source, name)
            if type(value) in (dict, list):
                value.clear()
            setattr(source, name, () if type(value) is tuple else {} if type(value) is dict else [])


def _signature_workspace(size):
    # build_signature keeps a digest object/list slot per 4 KiB page while
    # joining, then hashes, cd_blob, blob and padded bytes. Eight complete
    # signature lengths cover both phases, including rounded digest headers
    # and simultaneous old/new list pointer arrays. No image copy is credited.
    return 8 * size + _FIXED


def _owned_output_supported():
    # Debug/profiling callbacks may take a frame-local reference between a
    # refcount check and the subsequent C operation. Reject instrumentation,
    # including monitoring tools with only local events (global mask zero).
    if (not _single_buffer_window_supported() or stackmap._NATIVE_RECORD_READS
            or sys.gettrace() is not None or sys.getprofile() is not None):
        return False
    return all(sys.monitoring.get_tool(tool) is None for tool in range(6))


def _require_uninstrumented_owner():
    if not _owned_output_supported():
        raise LinkError("owned output requires an uninstrumented verified CPython buffer model")


class _ValidationBudget:
    def __init__(self, budget, payload):
        if type(payload) is not bytes:
            raise LinkError("retained validation payload must be exact bytes")
        self.budget = budget
        self.payload = payload

    def unretained_payload_bytes(self, payload):
        return 0 if payload is self.payload else len(payload)

    def check_peak(self, *args, **kwargs):
        return self.budget.check_peak(*args, **kwargs)

    def charge_records(self, count):
        return self.budget.charge_records(count)

    def check_validation_cache(self, payload, records, functions, cache, locations):
        self.check_peak(
            self.unretained_payload_bytes(payload) + locations * self.budget.integer_bytes,
            records=records, entries=functions + len(cache) + 1,
            label="validation cache",
        )


# Capture only this exact private adapter after its definition, avoiding the
# output -> validation -> runtime helper import cycle. Reloads do not expand it.
host_validation.runtime_validation._register_owned_adapter(_ValidationBudget)


class _OwnedOutput:
    def __init__(self, source, budget, caller_owners, phase_callback=None):
        self.phase_callback = phase_callback
        self.budget = budget
        self.caller_bytes = _model_bytes(caller_owners, budget)
        self.source_bytes = _source_bytes(source, budget)
        self.buffers = {}
        self.views = {}
        self.regions = []
        self.extra_raw = 0
        self.order_raw = 0
        self.fixup_count = 0
        self.io_remaining = 0
        self.payload_file = None
        self.payload_extent = 0
        self.active_payload = None
        self.output_count = len(source._sections)
        self.symbol_raw = _symbol_projections(source, budget)
        self.section_raw = _section_projections(self.output_count, budget)
        self.fixup_raw = 0
        self.check(label="Mach-O owned preparation projections")

    def phase(self, event):
        emit_phase(self.phase_callback, event)

    def known_bytes(self):
        return (self.caller_bytes + self.source_bytes + self.extra_raw
                + self.symbol_raw + self.section_raw + self.fixup_raw
                + self.order_raw + _FIXED)

    def check(self, extra=0, *, label):
        self.budget.check_peak(self.known_bytes() + extra, label=label)

    def reserve_buffers(self, size, outputs):
        # The existing synthesized stubs/GOT/island constructors temporarily
        # hold their zero bytes and bytearray together. Keep their final bytes
        # until the preparation frame has actually returned.
        following = _section_projections(self.output_count + outputs, self.budget)
        self.check(2 * size + following - self.section_raw,
                   label="Mach-O owned synthesized sections")
        self.extra_raw += size
        self.output_count += outputs
        self.section_raw = following

    def reserve_fixup(self, count=1):
        total = self.fixup_count + count
        budget = self.budget
        integer = budget._rounded(sys.getsizeof((1 << 64) - 1))
        quad = budget._rounded(sys.getsizeof((None, None, None, None)))
        # Source-to-segment conversion retires each old tuple immediately
        # after appending its replacement. Across all four source lists and
        # both segments there are at most total + 1 live site tuples, each
        # with at most three owned integers (shared offsets count only once).
        # Retain both families' old/new pointer arrays conservatively. The
        # private segment encoder sorts in place, using only key/merge
        # scratch; it has no copied list, seen set or per-page site lists.
        following = ((total + 1) * (quad + 3 * integer)
                     + _list_storage(total, budget, lists=4)
                     + _list_storage(total, budget, lists=2, sorting=True))
        self.check(following - self.fixup_raw, label="Mach-O owned fixup projections")
        self.fixup_count += count
        self.fixup_raw = following

    def reserve_order(self, count):
        # Host CompilerIntArena is a list of encoded <=64-bit integers.
        # Four pointer words/entry cover list growth overlap and sort scratch.
        needed = (count * self.budget._rounded(self.budget.integer_bytes)
                  + _list_storage(count, self.budget, sorting=True))
        self.check(max(0, needed - self.order_raw), label="Mach-O owned relocation order")
        self.order_raw = max(self.order_raw, needed)

    def reserve_pages(self, count):
        # Only compact uint16 starts survive the private encoder. Twelve
        # bytes per possible page cover segment-info bytes, old/new starts
        # bytearray growth, its bytes conversion and chained-fixups concat
        # overlap. Fixed headers, alignment and the two segment owners fit
        # in _FIXED. Empty trailing pages allocate no Python objects.
        needed = 12 * count + _FIXED
        self.check(needed, label="Mach-O owned fixup page tables")
        self.extra_raw += needed

    def _build_seg_info(self, fixups, seg_vmaddr, seg_size, seg_file_off):
        """Consume a private site order without per-page or duplicate indices."""
        if not fixups:
            raise LinkError("chained-fixup segment has no sites")
        fixups.sort(key=lambda site: site[0])
        previous = None
        last_page = -1
        page_size = executable.PAGE
        # Validate the complete order before allocating starts or writing any
        # pointer. Sorting makes duplicates adjacent; empty pages need no
        # dict entry, list, integer, or tiny packed bytes object.
        for vmaddr, out, off, _base_word in fixups:
            if vmaddr == previous:
                raise LinkError(f"duplicate chained-fixup site at {vmaddr:#x}")
            previous = vmaddr
            if not seg_vmaddr <= vmaddr <= seg_vmaddr + seg_size - 8:
                raise LinkError(
                    f"chained-fixup site {vmaddr:#x} is outside its segment"
                )
            if vmaddr % 4 or off < 0 or off + 8 > len(out.data):
                raise LinkError(
                    f"chained-fixup site {vmaddr:#x} is not a complete, "
                    "four-byte-aligned pointer"
                )
            segment_offset = vmaddr - seg_vmaddr
            if segment_offset % page_size > page_size - 8:
                raise LinkError(
                    f"chained-fixup pointer at {vmaddr:#x} crosses a "
                    f"{page_size:#x}-byte page boundary"
                )
            last_page = segment_offset // page_size
        page_count = last_page + 1
        if page_count > 0xFFFF:
            raise LinkError(
                f"chained-fixup page count {page_count} exceeds uint16; "
                f"last fixup page {last_page} needs a different segment layout"
            )
        header = struct.pack(
            "<IHHQIH", 22 + page_count * 2, page_size,
            executable.DYLD_CHAINED_PTR_64_OFFSET, seg_file_off, 0, page_count,
        )
        info = bytearray(b"\xff") * (22 + 2 * page_count)
        info[:22] = header
        previous_page = -1
        for index, (vmaddr, out, off, base_word) in enumerate(fixups):
            segment_offset = vmaddr - seg_vmaddr
            page = segment_offset // page_size
            if page != previous_page:
                struct.pack_into("<H", info, 22 + 2 * page,
                                 segment_offset % page_size)
                previous_page = page
            nxt = 0
            if index + 1 < len(fixups):
                following = fixups[index + 1][0]
                if (following - seg_vmaddr) // page_size == page:
                    delta = following - vmaddr
                    if delta <= 0 or delta % 4:
                        raise LinkError(
                            "chained-fixup sites must increase in four-byte units"
                        )
                    nxt = delta // 4
                    if nxt > 0xFFF:
                        raise LinkError("fixup chain stride exceeds 12 bits")
            self.patch_word(out, off, base_word | (nxt << 51))
        return bytes(info)

    def row_window(self, source, section):
        """Return the private replay context, or keep the ordinary row route.

        The caller closes its iterator and clears its rows local before
        leaving this context. The store then checks that no bytes alias
        escaped; it never caches replay buffers between sections.
        """
        storage = getattr(source, "_host_row_store", None)
        if storage is None:
            return None
        _require_uninstrumented_owner()
        if storage.public_copy_bytes:
            raise LinkError("owned output cannot consume staged rows with public copies")
        index = section.get("_pcc_source_index")
        if type(index) is not int or not 0 <= index < len(source._sections):
            raise LinkError("section does not belong to this source view")
        return storage.window(
            index, check=lambda: self.check(label="Mach-O owned relocation window"),
        )

    def retire_relocations(self, source):
        before = (_model_bytes(source._raw_relocations, self.budget)
                  + _row_storage_bytes(source))
        storage = getattr(source, "_host_row_store", None)
        if storage is not None:
            storage.close()
            source._host_row_store = None
            storage = None
        source._raw_relocations.clear()
        self.source_bytes -= before - _model_bytes(source._raw_relocations, self.budget)
        self.order_raw = 0

    def retire_payloads(self, source):
        """Drop the source file after all used indices have transferred.

        Unwind-only indices are deliberately never consumed. The executable
        section walk has now visited every descriptor, so closing the store
        discards those extents without materializing their unused backings.
        """
        storage = getattr(source, "_host_payload_store", None)
        if storage is None:
            return
        before = (storage.metadata_bytes + storage.public_copy_bytes
                  + _model_bytes(source._host_payload_layout, self.budget)
                  + _model_bytes(source._payloads, self.budget))
        try:
            storage.close()
        finally:
            source._host_payload_store = None
            source._host_payload_layout = ()
            source._payloads.clear()
        self.source_bytes -= before - _model_bytes(source._payloads, self.budget)

    def layout_data(self, source, section, zerofill):
        """Keep staged sources file-owned until their relocation walk starts."""
        storage = getattr(source, "_host_payload_store", None)
        if storage is None or zerofill:
            return self.take(source, section, zerofill)
        index = section.get("_pcc_source_index")
        if type(index) is not int or not 0 <= index < len(source._sections):
            raise LinkError("section does not belong to this source view")
        return _FilePayload(storage.size(index))

    def begin_section(self, source, section, out):
        descriptor = out.data
        if type(descriptor) is not _FilePayload:
            return
        if self.active_payload is not None or descriptor.offset is not None:
            raise LinkError("owned output section transfer state changed")
        # Normalization in take may itself borrow the relocation-row store.
        # The executable opens its ordered row window only after this call.
        out.data = self.take(source, section, False)
        if len(out.data) != len(descriptor):
            raise LinkError("owned output section size changed")
        self.active_payload = (out, descriptor)

    def _file_extent(self, descriptor):
        _require_uninstrumented_owner()
        if (type(descriptor) is not _FilePayload
                or type(descriptor.offset) is not int or descriptor.offset < 0
                or descriptor.offset > self.payload_extent
                or len(descriptor) > self.payload_extent - descriptor.offset
                or self.payload_file is None
                or os.fstat(self.payload_file.fileno()).st_size != self.payload_extent):
            raise LinkError("owned output payload extent changed")

    def _release_payload(self, out, descriptor):
        view = out.data
        key = id(view)
        buffer = self.buffers[key]
        # Close rejects a retained child export; do not credit its backing
        # until the actual buffer has closed successfully.
        view.release()
        buffer.close()
        self.views.pop(key)
        self.buffers.pop(key)
        out.data = descriptor
        self.extra_raw -= self.budget._rounded(sys.getsizeof(b"") + len(descriptor))

    def end_section(self, out):
        if self.active_payload is None:
            return
        current, descriptor = self.active_payload
        if current is not out or descriptor.offset is not None:
            raise LinkError("owned output section transfer state changed")
        _require_uninstrumented_owner()
        if self.payload_file is None:
            self.payload_file = tempfile.TemporaryFile(mode="w+b", buffering=0)
        stream = self.payload_file
        if (os.fstat(stream.fileno()).st_size != self.payload_extent
                or stream.seek(self.payload_extent) != self.payload_extent):
            raise LinkError("owned output payload extent changed")
        size = len(descriptor)
        if size > self.budget.limit_bytes - self.payload_extent:
            raise LinkError("owned output payload storage exceeds byte budget")
        remaining = 2 * size + 2 * _BLOCK
        cursor = 0
        while cursor < size:
            end = min(size, cursor + _BLOCK)
            remaining -= end - cursor
            if remaining < 0:
                raise LinkError("owned output payload I/O work bound exceeded")
            with out.data[cursor:end] as part:
                count = stream.write(part)
            if type(count) is not int or not 0 < count <= end - cursor:
                raise LinkError("owned output payload made invalid write progress")
            cursor += count
        if os.fstat(stream.fileno()).st_size != self.payload_extent + size:
            raise LinkError("owned output payload extent changed")
        descriptor.offset = self.payload_extent
        self.payload_extent += size
        self._release_payload(out, descriptor)
        self.active_payload = None

    def patch_word(self, out, offset, word):
        """Patch a final chained word without rehydrating a whole section."""
        descriptor = out.data
        if type(descriptor) is not _FilePayload:
            struct.pack_into("<Q", descriptor, offset, word)
            return
        self._file_extent(descriptor)
        if type(offset) is not int or not 0 <= offset <= len(descriptor) - 8:
            raise LinkError("owned output chained pointer exceeds its section")
        position = descriptor.offset + offset
        if self.payload_file.seek(position) != position:
            raise LinkError("owned output payload seek changed")
        # Chained-fixup grouping and next-stride decisions remain global to
        # the segment/page in macho_exec, including cross-section chains.
        count = self.payload_file.write(struct.pack("<Q", word))
        if type(count) is not int or count != 8:
            raise LinkError("owned output chained pointer write was short")

    def _load_file_payload(self, out, descriptor):
        self._file_extent(descriptor)
        size = len(descriptor)
        backing = self.budget._rounded(sys.getsizeof(b"") + size)
        self.check(backing, label="Mach-O owned payload validation replay")
        _require_uninstrumented_owner()
        buffer = view = failure = None
        try:
            buffer = io.BytesIO(b"\0" * size)
            view = buffer.getbuffer()
            if self.payload_file.seek(descriptor.offset) != descriptor.offset:
                raise LinkError("owned output payload seek changed")
            cursor = 0
            remaining = 2 * size + 2 * _BLOCK
            while cursor < size:
                end = min(size, cursor + _BLOCK)
                remaining -= end - cursor
                if remaining < 0:
                    raise LinkError("owned output payload I/O work bound exceeded")
                with view[cursor:end] as part:
                    count = self.payload_file.readinto(part)
                if type(count) is not int or not 0 < count <= end - cursor:
                    raise LinkError("owned output payload made invalid read progress")
                cursor += count
            self._file_extent(descriptor)
            self.buffers[id(view)] = buffer
            self.views[id(view)] = view
            out.data = view
            self.extra_raw += backing
            buffer = view = None
        except BaseException as exc:
            failure = exc
            _release_closed_tracebacks(exc)
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
                buffer = view = failure = None

    def take(self, source, section, zerofill):
        payload = buffer = view = None
        try:
            _require_uninstrumented_owner()
            storage = getattr(source, "_host_payload_store", None)
            if storage is None:
                payload = OwnedMergedSourceView.take_section_data(source, section)
            else:
                index = section.get("_pcc_source_index")
                if type(index) is not int or not 0 <= index < len(source._sections):
                    raise LinkError("section does not belong to this source view")
                backing = storage.backing_bytes(index)
                def check():
                    self.check(backing, label="Mach-O owned source payload transfer")
                payload = storage.take(
                    index, check=check,
                    patch=lambda data: _patch_staged_payload(source, index, data, check),
                )
                # Each final backing now belongs to the output graph. Earlier
                # transfers remain charged while subsequent ones materialize.
                self.extra_raw += backing
            if type(payload) is not bytes:
                raise LinkError("owned output payload is not exact bytes")
            if zerofill:
                if payload:
                    raise LinkError("owned zerofill section has file bytes")
                return bytearray()
            # The caller never acquired the bytes. After taking its list slot,
            # only this local and the explicit strong temporary may reference
            # it. A direct getrefcount(local) can borrow on CPython 3.14+.
            # Shared/interned tiny bytes need at most one byte of copying.
            if len(payload) > 1 and sys.getrefcount([payload].pop()) != 2:
                raise LinkError("owned output section has an escaped bytes alias")
            buffer = io.BytesIO(payload)
            payload = None
            view = buffer.getbuffer()
            self.buffers[id(view)] = buffer
            self.views[id(view)] = view
            buffer = None
            return view
        finally:
            payload = storage = check = None
            if buffer is not None:
                if view is not None:
                    view.release()
                buffer.close()

    def validate(self, out):
        descriptor = out.data if type(out.data) is _FilePayload else None
        if descriptor is not None:
            self._load_file_payload(out, descriptor)
        self._validate_buffer(out)
        if descriptor is not None:
            self._release_payload(out, descriptor)

    def _validate_buffer(self, out):
        old_view = out.data
        size = len(old_view)
        buffer = self.buffers.pop(id(old_view))
        self.views.pop(id(old_view))
        snapshot = adapter = replacement = None
        previous = self.budget.retained_bytes
        try:
            # No subview is handed to callers. The original validator is
            # called only after the private writable export is gone.
            old_view.release()
            out.data = None
            # Same-length truncate is an export barrier, not a resize. Unlike
            # getvalue, it rejects a hidden child view before any copying.
            buffer.truncate(size)
            snapshot = buffer.getvalue()
            if len(snapshot) < 2:
                raise LinkError("owned stack-map snapshot is too short")
            self.check(label="Mach-O owned final stack-map validation")
            self.budget.retained_bytes = (
                previous + self.known_bytes()
            )
            adapter = _ValidationBudget(self.budget, snapshot)
            self.phase("stackmap_payload_validate_begin")
            host_validation.validate_stack_map_payload(
                snapshot, expected_arch=stackmap.ARCH_AARCH64,
                final_image=True, compact_budget=adapter,
            )
            self.phase("stackmap_payload_validate_end")
            # The adapter itself owns one reference; retire it before testing
            # buffer + local + explicit strong temporary. An escaping validator,
            # trace hook or future cache must refuse rather than cause COW.
            adapter.payload = None
            adapter = None
            self.phase("stackmap_thaw_begin")
            _require_uninstrumented_owner()
            if sys.getrefcount([snapshot].pop()) != 3:
                raise LinkError("owned stack-map validation retained a snapshot alias")
            snapshot = None
            replacement = buffer.getbuffer()
            self.buffers[id(replacement)] = buffer
            self.views[id(replacement)] = replacement
            out.data = replacement
            buffer = None
            self.phase("stackmap_thaw_end")
        except BaseException as exc:
            _release_closed_tracebacks(exc)
            raise
        finally:
            self.budget.retained_bytes = previous
            if adapter is not None:
                adapter.payload = None
            snapshot = adapter = old_view = replacement = None
            if buffer is not None:
                buffer.close()

    def plan(self, size, outputs, metadata, reserve, text_size):
        self.check(label="Mach-O owned region transfer")
        # Only called after every address and chained pointer has been patched.
        regions = self.regions
        for out in outputs:
            if not out.zerofill_size:
                regions.append([out.fileoff, out.data, out.segname + "," + out.sectname])
        for offset, payload, label in metadata:
            regions.append([offset, payload, label])
        header_size = len(metadata[0][1])
        self.parser_bytes = (
            3 * header_size + (header_size // 8 + 16) * self.budget.metadata_entry_bytes
        )
        regions.sort(key=lambda item: (item[0], len(item[1]), item[2]))
        end = 0
        for offset, payload, label in regions:
            if type(offset) is not int or offset < end or offset + len(payload) > size:
                raise LinkError("owned output region overlaps or exceeds image: " + label)
            if payload:
                end = offset + len(payload)
        self.budget.check_peak(size + reserve + self.caller_bytes + _FIXED,
                               label="Mach-O owned output wire extent")
        return size, reserve, text_size

    def charge_io(self, size):
        if size < 0 or size > self.io_remaining:
            raise LinkError("owned output spool I/O work bound exceeded")
        self.io_remaining -= size

    def spool(self, stream, source, size):
        # This method is entered only after _prepare_executable_image returns:
        # no _Out, relocation generator, rows or fixup closure survives there.
        _clear_source(source)
        self.source_bytes = 0
        self.symbol_raw = 0
        self.section_raw = 0
        self.fixup_raw = 0
        self.fixup_count = 0
        self.extra_raw = 0
        self.order_raw = 0
        raw = sum(len(item[1]) for item in self.regions)
        file_raw = sum(len(item[1]) for item in self.regions
                       if type(item[1]) is _FilePayload)
        scratch_bytes = (self.budget._rounded(sys.getsizeof(bytearray()) + _BLOCK + 1)
                         if file_raw else 0)
        # Bound requested transfers too, not only actual positive progress.
        # Limited short I/O is supported; pathological tiny progress refuses
        # after a linear amount of work, before issuing the next request.
        self.io_remaining = 2 * (file_raw + raw + size) + 4 * _BLOCK
        self.budget.check_peak(raw - file_raw + scratch_bytes + self.caller_bytes + _FIXED,
                               entries=4 * len(self.regions), label="Mach-O owned output spool")
        # A new anonymous inode, not an existing file with potentially stale
        # gap bytes. ftruncate sets the exact bounded extent; gaps read as zero.
        if os.fstat(stream.fileno()).st_size != 0:
            raise LinkError("owned output spool must be newly empty")
        if size > self.budget.limit_bytes:
            raise LinkError("owned output spool exceeds byte budget")
        os.ftruncate(stream.fileno(), size)
        written = 0
        payload = region = view = scratch = None
        try:
            if file_raw:
                scratch = bytearray(_BLOCK)
            for index in range(len(self.regions)):
                region = self.regions[index]
                offset, payload, _label = region
                stream.seek(offset)
                if type(payload) is _FilePayload:
                    self._file_extent(payload)
                    if self.payload_file.seek(payload.offset) != payload.offset:
                        raise LinkError("owned output payload seek changed")
                    cursor = 0
                    while cursor < len(payload):
                        wanted = min(_BLOCK, len(payload) - cursor)
                        self.charge_io(wanted)
                        with memoryview(scratch)[:wanted] as part:
                            count = self.payload_file.readinto(part)
                        if type(count) is not int or not 0 < count <= wanted:
                            raise LinkError("owned output payload made invalid read progress")
                        copied = 0
                        while copied < count:
                            self.charge_io(count - copied)
                            with memoryview(scratch)[copied:count] as part:
                                progress = stream.write(part)
                            if type(progress) is not int or not 0 < progress <= count - copied:
                                raise LinkError("owned output spool made invalid write progress")
                            copied += progress
                        cursor += count
                        written += count
                    self._file_extent(payload)
                    region[1] = None
                    self.regions[index] = None
                    region = payload = None
                    continue
                with memoryview(payload) as view:
                    cursor = 0
                    while cursor < len(view):
                        end = min(len(view), cursor + _BLOCK)
                        self.charge_io(end - cursor)
                        with view[cursor:end] as part:
                            count = stream.write(part)
                        if type(count) is not int or not 0 < count <= end - cursor:
                            raise LinkError("owned output spool made invalid write progress")
                        written += count
                        if written > raw:
                            raise LinkError("owned output spool write bound exceeded")
                        cursor += count
                key = id(payload)
                if key in self.views:
                    self.views.pop(key).release()
                    self.buffers.pop(key).close()
                region[1] = None
                self.regions[index] = None
                region = payload = view = None
            self.regions.clear()
            if written != raw:
                raise LinkError("owned output spool byte count changed")
            stream.seek(0)
            if self.payload_file is not None:
                self.payload_file.close()
                self.payload_file = None
        finally:
            payload = region = view = scratch = None

    def close(self, *, _error=None):
        failure = None
        try:
            for view in self.views.values():
                try:
                    view.release()
                except BaseException as exc:
                    _release_closed_tracebacks(exc)
                    if failure is None:
                        failure = exc.with_traceback(None)
        finally:
            self.views.clear()
            try:
                for buffer in self.buffers.values():
                    try:
                        buffer.close()
                    except BaseException as exc:
                        _release_closed_tracebacks(exc)
                        if failure is None:
                            failure = exc.with_traceback(None)
            finally:
                self.buffers.clear()
                self.regions.clear()
                self.active_payload = None
                view = buffer = None
                if self.payload_file is not None:
                    try:
                        self.payload_file.close()
                    except BaseException as exc:
                        _release_closed_tracebacks(exc)
                        if failure is None:
                            failure = exc.with_traceback(None)
                    finally:
                        self.payload_file = None
        if failure is not None and _error is None:
            raise failure


def link_owned_executable(source, *, compact_budget, phase_callback=None,
                          entry="_main", identifier=b"pcc-linked", minos=(12, 0),
                          caller_owners=(), _phase_callback=None):
    """Consume the private all-PCO source and return ordinary immutable bytes."""
    try:
        if type(source) is not OwnedMergedSourceView or source._source_sections:
            raise LinkError("owned output requires a windowed compact source")
        # CPython 3.14+ can borrow both a Python parameter and a getrefcount
        # argument. Give this local its own strong reference, then sample with
        # another known strong reference. A caller alias still raises the count.
        # Each singleton list is retired by pop; only bounded scratch is added.
        source = [source].pop()
        if sys.getrefcount([source].pop()) != 2:
            raise LinkError("owned output source must be exclusively transferred")
    except BaseException as exc:
        # Ordinary borrowed sources retain their original refusal behavior.
        # A staged source is private and must not leave its file open here.
        if type(source) is OwnedMergedSourceView:
            close_staged_source_rows(source, _error=exc)
        raise
    state = buffer = view = image = blob = storage = failure = None
    try:
        _require_uninstrumented_owner()
        storage = getattr(source, "_host_row_store", None)
        if storage is not None and storage.public_copy_bytes:
            raise LinkError("owned output cannot consume staged rows with public copies")
        payloads = getattr(source, "_host_payload_store", None)
        if payloads is not None and payloads.public_copy_bytes:
            raise LinkError("owned output cannot consume staged payloads with public copies")
        payloads = None
        storage = None
        emit_phase(_phase_callback, "output_model_begin")
        state = _OwnedOutput(source, compact_budget, caller_owners, _phase_callback)
        emit_phase(_phase_callback, "output_model_end")
        emit_phase(_phase_callback, "output_prepare_begin")
        plan = executable._prepare_executable_image(
            source, entry=entry, identifier=identifier, minos=minos,
            compact_budget=compact_budget, _owned_output=state,
        )
        emit_phase(_phase_callback, "output_prepare_end")
        size, reserve, text_size = plan
        with tempfile.TemporaryFile(mode="w+b", buffering=0) as stream:
            emit_phase(_phase_callback, "output_spool_begin")
            state.spool(stream, source, size)
            source = None
            emit_phase(_phase_callback, "output_spool_end")
            total = size + reserve
            compact_budget.check_peak(total + state.caller_bytes + _FIXED,
                                       label="Mach-O owned final backing")
            emit_phase(_phase_callback, "final_backing_begin")
            buffer = io.BytesIO(b"\0" * total)
            view = buffer.getbuffer()
            cursor = 0
            while cursor < size:
                end = min(size, cursor + _BLOCK)
                state.charge_io(end - cursor)
                with view[cursor:end] as part:
                    count = stream.readinto(part)
                if type(count) is not int or not 0 < count <= end - cursor:
                    raise LinkError("owned output spool made invalid read progress")
                cursor += count
            state.charge_io(1)
            if stream.read(1):
                raise LinkError("owned output spool extent changed")
        emit_phase(_phase_callback, "final_backing_end")
        compact_budget.check_peak(
            total + state.caller_bytes + state.parser_bytes + _signature_workspace(reserve),
            label="Mach-O owned signature workspace",
        )
        emit_phase(_phase_callback, "sign_begin")
        if phase_callback is not None:
            phase_callback("sign_begin")
        try:
            with view[:size] as prefix:
                blob = codesign.build_signature(
                    prefix, identifier=identifier, exec_seg_base=0,
                    exec_seg_limit=text_size, exec_seg_flags=1,
                )
        finally:
            if phase_callback is not None:
                phase_callback("sign_end")
        emit_phase(_phase_callback, "sign_end")
        if len(blob) != reserve:
            raise LinkError("owned output signature size changed")
        view[size:] = blob
        blob = None
        view.release()
        view = None
        buffer.truncate(total)
        image = buffer.getvalue()
        buffer.close()
        buffer = None
        return image
    except (stackmap.PreciseStackMapError, LinkError) as exc:
        failure = exc
        _release_closed_tracebacks(exc)
        sizes = compact_budget.last_rejection
        if sizes and (isinstance(exc, stackmap.PreciseStackMapError)
                      or isinstance(exc.__cause__, stackmap.PreciseStackMapError)):
            raise LinkError(
                "Mach-O compact stack-map budget rejected: " + str(exc) + "; " + sizes
            ) from exc
        raise
    except BaseException as exc:
        failure = exc
        _release_closed_tracebacks(exc)
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
                try:
                    try:
                        if source is not None:
                            _clear_source(source, _error=failure)
                    finally:
                        if state is not None:
                            state.close(_error=failure)
                finally:
                    source = state = buffer = view = image = blob = storage = payloads = failure = None


def check_signature_verification(image, signature, budget, *, caller_owners=()):
    """Account the unchanged public parser/re-hash path on verified CPython."""
    if not _single_buffer_window_supported():
        raise LinkError("owned signature verification requires verified CPython")
    header_size = 32 + struct.unpack_from("<I", image, 20)[0]
    try:
        budget.check_peak(
            len(image) + _signature_workspace(signature.datasize)
            + 3 * header_size + _model_bytes(caller_owners, budget),
            entries=header_size // 8 + 16,
            label="owned Mach-O signature verification workspace",
        )
    except stackmap.PreciseStackMapError as exc:
        raise LinkError(
            "Mach-O compact stack-map budget rejected: " + str(exc)
            + "; " + budget.last_rejection
        ) from exc
