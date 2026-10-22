"""CPython packing of already-validated owned Mach-O relocation rows.

The known-owner estimate includes the constructor's source model, normalized
payloads, output metadata and earlier row buffers. It deliberately counts
aliases conservatively without allocating a second per-relocation identity
table. It is not a census of caller-frame metadata or RSS. The existing outer
admission and tree guard still apply, and prior retained_bytes is never credited
back. The source Relocation graph remains live throughout this operation.
"""

import struct
import sys

from .macho_obj import (
    DataInCodeRegion,
    Relocation,
    Section,
    TextSymbol,
    _append_final_link_relocation_fields,
)
from .native_object import (
    NativeObjectError,
    _FINAL_LINK_RELOCATION,
    _validate_final_link_relocation_count,
)


_MODEL_FIELDS = {
    Section: (
        "sectname", "segname", "data", "align_log2", "flags", "symbols",
        "relocations", "zerofill_size", "data_in_code",
    ),
    TextSymbol: ("name", "offset", "external", "private_external"),
    Relocation: (
        "offset", "symbol", "type", "pcrel", "length", "addend", "section",
        "minuend", "target_offset",
    ),
    DataInCodeRegion: ("offset", "length", "kind"),
}
_BYTES_HEADER = sys.getsizeof(b"")
_BYTEARRAY_HEADER = sys.getsizeof(bytearray())
_LIST_HEADER = sys.getsizeof([])
_WORD_BYTES = struct.calcsize("P")
_INTEGER_BYTES = sys.getsizeof((1 << 64) - 1)
# The supported CPython model has at most 30 shared instance keys. Reserve
# both embedded/detached values (32 order bytes plus 31 pointer slots each)
# and a dictionary header for the exact three-field DataInCodeRegion model.
# Reading each instance's __dict__ to measure it can itself materialize a
# dictionary; this fixed allowance avoids an O(region_count) measuring graph.
# https://github.com/python/cpython/blob/v3.13.0/Include/internal/pycore_dict.h
# https://github.com/python/cpython/blob/v3.15.0rc1/Include/internal/pycore_dict.h
_REGION_DICT_HEADER = sys.getsizeof({})
_REGION_VALUES_BYTES = 32 + 31 * _WORD_BYTES


def _model_bytes(value, budget):
    """Walk the validated fixed model without retaining an identity graph."""
    size = budget._rounded(sys.getsizeof(value))
    kind = type(value)
    if kind is bytearray:
        return (
            budget._rounded(_BYTEARRAY_HEADER)
            + budget._rounded(sys.getsizeof(value) - _BYTEARRAY_HEADER)
        )
    if kind in (str, bytes, int, bool, type(None)):
        return size
    if kind is memoryview:
        # A small slice can keep an arbitrarily larger exporter alive.
        return size + _model_bytes(value.obj, budget)
    if kind in (tuple, list, set, frozenset):
        for item in value:
            size += _model_bytes(item, budget)
        return size
    if kind is dict:
        for key, item in value.items():
            size += _model_bytes(key, budget) + _model_bytes(item, budget)
        return size
    fields = _MODEL_FIELDS.get(kind)
    if fields is None:
        raise NativeObjectError(
            "unsupported host relocation packing owner: " + kind.__name__
        )
    if kind is DataInCodeRegion:
        size += (
            budget._rounded(_REGION_DICT_HEADER)
            + 2 * budget._rounded(_REGION_VALUES_BYTES)
        )
        for name in fields:
            size += budget._rounded(sys.getsizeof(name))
    for name in fields:
        size += _model_bytes(getattr(value, name), budget)
    return size


class _FinalRowWriter:
    """The append4/append2 surface used by the existing scalar projector."""

    __slots__ = ("buffer", "cursor", "pending")

    def __init__(self, total):
        self.buffer = bytearray(total)
        self.cursor = 0
        self.pending = None

    def append4(self, address, symbolnum, pcrel, length):
        if self.pending is not None:
            raise NativeObjectError("incomplete final-link relocation row")
        if self.cursor + _FINAL_LINK_RELOCATION.size > len(self.buffer):
            raise NativeObjectError("merged relocation count changed")
        self.pending = (address, symbolnum, pcrel, length)

    def append2(self, external, relocation_type):
        pending = self.pending
        if pending is None:
            raise NativeObjectError("final-link relocation row has no prefix")
        if self.cursor + _FINAL_LINK_RELOCATION.size > len(self.buffer):
            raise NativeObjectError("merged relocation count changed")
        try:
            _FINAL_LINK_RELOCATION.pack_into(
                self.buffer, self.cursor,
                pending[0], pending[1], pending[2], pending[3],
                external, relocation_type,
            )
        except (struct.error, OverflowError) as exc:
            raise NativeObjectError("final-link relocation field is out of range") from exc
        self.cursor += _FINAL_LINK_RELOCATION.size
        self.pending = None

    def close(self):
        # Also release the large allocation when an exception traceback keeps
        # this writer/frame alive after a failed lookup, iterator or pack.
        self.buffer = None
        self.pending = None


def _scratch_bytes(budget):
    # One writer, its pending four fields, pack_into's argument tuple and
    # bounded cursor/length integer temporaries. No term grows per row.
    return (
        budget._rounded(sys.getsizeof(object.__new__(_FinalRowWriter)))
        + budget._rounded(sys.getsizeof((0,) * 4))
        + budget._rounded(sys.getsizeof((0,) * 8))
        + 8 * budget._rounded(_INTEGER_BYTES)
    )


def _pack_final_link_rows(
    relocations, count, symbol_index, section_index, budget, retained_bytes,
):
    """Pack a validated logical iterator without six retained scalar owners.

    The caller includes all still-live source owners in retained_bytes. Count
    is the expanded row count, including ADDEND and SUBTRACTOR companions.
    """
    if type(count) is not int:
        raise NativeObjectError("final-link relocation count must be an integer")
    _validate_final_link_relocation_count(count)
    total = count * _FINAL_LINK_RELOCATION.size
    scratch = _scratch_bytes(budget)
    mutable_bytes = (
        budget._rounded(_BYTEARRAY_HEADER) + budget._rounded(total + 1)
    )
    budget.check_peak(
        retained_bytes + scratch + mutable_bytes,
        label="Mach-O host relocation buffer",
    )
    writer = _FinalRowWriter(total)
    try:
        _append_final_link_relocation_fields(
            writer, relocations, symbol_index, section_index,
        )
        if writer.pending is not None or writer.cursor != total:
            raise NativeObjectError("merged relocation count changed")
        budget.check_peak(
            retained_bytes + scratch + mutable_bytes
            + budget._rounded(_BYTES_HEADER + total),
            label="Mach-O host relocation freeze",
        )
        return bytes(writer.buffer)
    finally:
        writer.close()


def pack_owned_final_link_rows(
    source_sections, payloads, section_rows, symbol_rows, symbol_index,
    section_index, section_addrs, undefined_names, weak_names, *,
    source_container, compact_budget,
):
    """Replace only host packing; semantic validation precedes this call."""
    budget = compact_budget
    scratch = _scratch_bytes(budget)
    budget.check_peak(scratch, label="Mach-O host relocation owner scan")
    retained = 0
    for owner in (
        source_sections, payloads, section_rows, symbol_rows, symbol_index,
        section_index, section_addrs, undefined_names, weak_names,
    ):
        retained += _model_bytes(owner, budget)
    # tuple(sections) retained these exact Section owners, but the caller's
    # original list/tuple can still be another live array of references.
    if source_container is not source_sections:
        retained += budget._rounded(sys.getsizeof(source_container))
    # Preallocate the outer table too, rather than leaving list growth and
    # old/new pointer arrays outside the packing reservation.
    row_table_bytes = (
        budget._rounded(_LIST_HEADER)
        + budget._rounded(len(source_sections) * _WORD_BYTES)
    )
    retained += row_table_bytes
    budget.check_peak(retained + scratch, label="Mach-O host relocation owners")
    output = [b""] * len(source_sections)
    try:
        for index, section in enumerate(source_sections):
            rows = _pack_final_link_rows(
                section.relocations, section_rows[index]["nreloc"],
                symbol_index, section_index, budget, retained,
            )
            output[index] = rows
            retained += budget._rounded(sys.getsizeof(rows))
        return output
    except BaseException:
        # A traceback may keep this frame alive too. Never retain successful
        # earlier tables after a later table failed admission or projection.
        output.clear()
        rows = None
        raise
