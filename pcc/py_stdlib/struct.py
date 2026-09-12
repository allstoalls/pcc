"""Finite ``struct`` provider for pcc-owned compiler/runtime metadata.

The owned surface is the explicit-standard-layout integer/bytes subset used by
pcc's native object, ELF and precise-stackmap codecs.  Native alignment,
pointer-sized fields and floating-point formats are deliberately rejected
until their target ABI and shared float-bit implementation are wired into this
provider.  Unsupported shapes fail before producing partial bytes.
"""
from __future__ import annotations

from pcc.unsafe import abi_constant, load_i8, load_i32, load_i64, null, ptr_is_null


class error(Exception):
    pass


# Field plan kinds.  A ``Struct`` resolves its format once into
# ``(kind, width, signed, count)`` integer rows so the hot unpack loop never
# re-dispatches on format characters.
_KIND_INT = 0
_KIND_PAD = 1
_KIND_BYTES = 2
_KIND_CHAR = 3
_KIND_BOOL = 4


def _native_payload_reads_available() -> bool:
    """True only when pcc lowered ``pcc.unsafe`` for this module.

    CPython is the source-level oracle for this provider: its ``pcc.unsafe``
    stubs raise, so the host keeps the generic byte-slice decode while the
    compiled module reads immutable ``bytes`` payloads in place.
    """
    try:
        return ptr_is_null(null()) != 0
    except NotImplementedError:
        return False


_NATIVE_PAYLOAD_READS = _native_payload_reads_available()


_SIZES = {
    "x": 1,
    "c": 1,
    "b": 1,
    "B": 1,
    "?": 1,
    "h": 2,
    "H": 2,
    "i": 4,
    "I": 4,
    "l": 4,
    "L": 4,
    "q": 8,
    "Q": 8,
    "s": 1,
}


def _parse_format(fmt: str):
    if not isinstance(fmt, str):
        raise TypeError("Struct() argument 1 must be a str")
    if fmt == "":
        return "little", []
    prefix = fmt[0]
    if prefix == "<" or prefix == "=" :
        byteorder = "little"
        index = 1
    elif prefix == ">" or prefix == "!":
        byteorder = "big"
        index = 1
    elif prefix == "@":
        raise NotImplementedError(
            "native-aligned struct layouts are not owned by pcc.py_stdlib.struct"
        )
    else:
        raise NotImplementedError(
            "struct formats must use an explicit '<', '>', '!', or '=' prefix"
        )

    fields = []
    repeat = 0
    while index < len(fmt):
        ch = fmt[index]
        index += 1
        if ch == " ":
            continue
        if ch >= "0" and ch <= "9":
            repeat = repeat * 10 + (ord(ch) - ord("0"))
            continue
        if ch not in _SIZES:
            if ch in "efdPnN":
                raise NotImplementedError(
                    "struct format code " + repr(ch) + " is not owned yet"
                )
            raise error("bad char in struct format: " + repr(ch))
        count = repeat if repeat != 0 else 1
        fields.append((ch, count))
        repeat = 0
    if repeat != 0:
        raise error("repeat count given without format specifier")
    return byteorder, fields


def _format_size(fields) -> int:
    total = 0
    for ch, count in fields:
        total += _SIZES[ch] * count
    return total


def _integer_shape(ch: str):
    if ch == "b":
        return 1, True
    if ch == "B" or ch == "?":
        return 1, False
    if ch == "h":
        return 2, True
    if ch == "H":
        return 2, False
    if ch == "i" or ch == "l":
        return 4, True
    if ch == "I" or ch == "L":
        return 4, False
    if ch == "q":
        return 8, True
    if ch == "Q":
        return 8, False
    raise error("not an integer format code: " + repr(ch))


def _pack_fields(byteorder: str, fields, values) -> bytes:
    expected_values = 0
    for ch, count in fields:
        if ch == "x":
            continue
        if ch == "s":
            expected_values += 1
        else:
            expected_values += count
    if len(values) != expected_values:
        raise error(
            "pack expected "
            + str(expected_values)
            + " items for packing (got "
            + str(len(values))
            + ")"
        )

    out = b""
    value_index = 0
    for ch, count in fields:
        if ch == "x":
            out += b"\x00" * count
            continue
        if ch == "s":
            raw = values[value_index]
            value_index += 1
            if not isinstance(raw, (bytes, bytearray)):
                raise error("argument for 's' must be a bytes object")
            raw = bytes(raw)
            out += raw[:count]
            if len(raw) < count:
                out += b"\x00" * (count - len(raw))
            continue
        item_index = 0
        while item_index < count:
            value = values[value_index]
            value_index += 1
            item_index += 1
            if ch == "c":
                if not isinstance(value, (bytes, bytearray)) or len(value) != 1:
                    raise error("char format requires a bytes object of length 1")
                out += bytes(value)
                continue
            width, signed = _integer_shape(ch)
            if ch == "?":
                numeric = 1 if bool(value) else 0
            else:
                try:
                    numeric = int(value)
                except (TypeError, ValueError) as exc:
                    raise error("required argument is not an integer") from exc
            if width == 1:
                modulus = 0x100
            elif width == 2:
                modulus = 0x10000
            elif width == 4:
                modulus = 0x100000000
            else:
                modulus = 0x10000000000000000
            if signed:
                minimum = -(modulus // 2)
                maximum = modulus // 2 - 1
            else:
                minimum = 0
                maximum = modulus - 1
            if numeric < minimum or numeric > maximum:
                raise error("argument out of range")
            # The owned int.to_bytes lowering is the unsigned two-argument
            # form.  Convert an admitted negative signed value to its finite
            # two's-complement residue first; this also keeps signed packing
            # independent of a CPython keyword-call fallback.
            encoded = numeric + modulus if numeric < 0 else numeric
            out += encoded.to_bytes(width, byteorder)
    return out


def _normalize_offset(buffer_len: int, offset: int) -> int:
    offset = int(offset)
    if offset < 0:
        offset += buffer_len
    if offset < 0 or offset > buffer_len:
        raise error("offset out of range")
    return offset


def _build_plan(fields) -> list:
    """Resolve parsed fields into ``(kind, width, signed, count)`` rows."""
    plan = []
    for ch, count in fields:
        if ch == "x":
            plan.append((_KIND_PAD, 1, 0, count))
        elif ch == "s":
            plan.append((_KIND_BYTES, 1, 0, count))
        elif ch == "c":
            plan.append((_KIND_CHAR, 1, 0, count))
        elif ch == "?":
            plan.append((_KIND_BOOL, 1, 0, count))
        else:
            width, signed = _integer_shape(ch)
            plan.append((_KIND_INT, width, 1 if signed else 0, count))
    return plan


def _unpack_generic(byteorder: str, plan, raw, offset: int) -> tuple:
    """Byte-slice decode shared by CPython and by big-endian layouts."""
    values = []
    cursor = offset
    for kind, width, signed, count in plan:
        if kind == _KIND_PAD:
            cursor += count
            continue
        if kind == _KIND_BYTES:
            values.append(bytes(raw[cursor : cursor + count]))
            cursor += count
            continue
        item_index = 0
        while item_index < count:
            item_index += 1
            if kind == _KIND_CHAR:
                values.append(bytes(raw[cursor : cursor + 1]))
                cursor += 1
                continue
            numeric = int.from_bytes(raw[cursor : cursor + width], byteorder)
            if signed != 0:
                if width == 1:
                    modulus = 0x100
                elif width == 2:
                    modulus = 0x10000
                elif width == 4:
                    modulus = 0x100000000
                else:
                    modulus = 0x10000000000000000
                if numeric >= modulus // 2:
                    numeric -= modulus
            cursor += width
            values.append(bool(numeric) if kind == _KIND_BOOL else numeric)
    return tuple(values)


def _unpack_native_little(plan, raw, offset: int) -> tuple:
    """Decode little-endian integer fields straight from a ``bytes`` payload.

    Only reached when pcc lowered ``pcc.unsafe`` for this module and the caller
    already normalized and bounds-checked ``offset`` against ``len(raw)``, so
    every load stays inside the immutable payload.  ``raw`` is re-read from
    its rooted local on every access rather than cached as a derived address,
    which keeps the loop correct under the moving collectors.  The result is
    value-identical to ``_unpack_generic``: signed widths come back
    sign-extended, unsigned widths are lifted to their non-negative Python
    ``int`` and ``?`` fields become ``bool``.
    """
    # The payload offset is a compiler-provided freestanding ABI constant so
    # the cursor stays an exact machine integer; importing the same value from
    # the runtime port module makes it a dynamic global whose i64 conversion
    # would pull a CPython bridge into this strict no-libpython module.
    data_offset = abi_constant("object.bytes.data_offset")
    values = []
    cursor = data_offset + offset
    for kind, width, signed, count in plan:
        if kind == _KIND_PAD:
            cursor += count
            continue
        if kind == _KIND_BYTES:
            start = cursor - data_offset
            values.append(bytes(raw[start : start + count]))
            cursor += count
            continue
        item_index = 0
        while item_index < count:
            item_index += 1
            if kind == _KIND_CHAR:
                start = cursor - data_offset
                values.append(bytes(raw[start : start + 1]))
                cursor += 1
                continue
            if width == 8:
                numeric = load_i64(raw, cursor)
                if signed == 0 and numeric < 0:
                    numeric = numeric + 0x10000000000000000
            elif width == 4:
                numeric = load_i32(raw, cursor)
                if signed == 0 and numeric < 0:
                    numeric = numeric + 0x100000000
            elif width == 2:
                low = load_i8(raw, cursor) & 0xFF
                numeric = (load_i8(raw, cursor + 1) << 8) | low
                if signed == 0 and numeric < 0:
                    numeric = numeric + 0x10000
            else:
                numeric = load_i8(raw, cursor)
                if signed == 0 and numeric < 0:
                    numeric = numeric + 0x100
            cursor += width
            values.append(bool(numeric) if kind == _KIND_BOOL else numeric)
    return tuple(values)


def _unpack_plan(byteorder: str, plan, size: int, buffer, offset: int) -> tuple:
    # Bytes and bytearray buffers are read in place. Copying either made every
    # unpack_from O(len(buffer)) -- the self-backend stack-map validator calls
    # unpack_from tens of thousands of times on one multi-hundred-KB payload,
    # and under pcc1 that copy loop was 32% of a codegen worker's samples.
    # Both share the native payload layout. 's'/'c' copy only their field and
    # always return bytes, including when the input is mutable.
    raw = buffer if isinstance(buffer, (bytes, bytearray)) else bytes(buffer)
    offset = _normalize_offset(len(raw), offset)
    if offset + size > len(raw):
        raise error(
            "unpack_from requires a buffer of at least "
            + str(offset + size)
            + " bytes"
        )
    if _NATIVE_PAYLOAD_READS and byteorder == "little":
        return _unpack_native_little(plan, raw, offset)
    return _unpack_generic(byteorder, plan, raw, offset)


def _unpack_fields(byteorder: str, fields, buffer, offset: int):
    return _unpack_plan(
        byteorder, _build_plan(fields), _format_size(fields), buffer, offset
    )


class Struct:
    def __init__(self, fmt: str):
        self.format = fmt
        self._byteorder, self._fields = _parse_format(fmt)
        self.size = _format_size(self._fields)
        self._plan = _build_plan(self._fields)

    def pack(self, *values) -> bytes:
        return _pack_fields(self._byteorder, self._fields, values)

    def unpack(self, buffer) -> tuple:
        raw = bytes(buffer)
        if len(raw) != self.size:
            raise error(
                "unpack requires a buffer of " + str(self.size) + " bytes"
            )
        return _unpack_plan(self._byteorder, self._plan, self.size, raw, 0)

    def unpack_from(self, buffer, offset: int = 0) -> tuple:
        return _unpack_plan(
            self._byteorder, self._plan, self.size, buffer, offset
        )

    def iter_unpack(self, buffer):
        """Yield one tuple per fixed-size chunk of ``buffer``.

        CPython validates the buffer length when ``iter_unpack`` is *called*
        and only then returns a lazy iterator, so the size check lives here
        and the walk lives in the generator below.  Without this method a
        `Struct.iter_unpack` call fell back to CPython, which fail-closed
        stubbed `precise_stackmap.validate_stack_map_payload` -- the final
        stack-map check on every self-hosted link.
        """
        if self.size <= 0:
            raise error("cannot iter_unpack from struct of size 0")
        total = len(buffer)
        if total % self.size != 0:
            raise error(
                "iter_unpack requires a buffer of a multiple of "
                + str(self.size)
                + " bytes"
            )
        return self._iter_unpack_chunks(buffer, total)

    def _iter_unpack_chunks(self, buffer, total: int):
        offset = 0
        while offset < total:
            yield _unpack_plan(
                self._byteorder, self._plan, self.size, buffer, offset
            )
            offset = offset + self.size

    def pack_into(self, buffer, offset: int, *values) -> None:
        payload = self.pack(*values)
        offset = _normalize_offset(len(buffer), offset)
        if offset + len(payload) > len(buffer):
            raise error(
                "pack_into requires a buffer of at least "
                + str(offset + len(payload))
                + " bytes"
            )
        index = 0
        while index < len(payload):
            buffer[offset + index] = payload[index]
            index += 1


_FORMAT_CACHE = {}


def _cached_struct(fmt: str) -> Struct:
    # Module-level operations share a bounded immutable format plan, just as
    # an explicit Struct does. Rebuilding it for every record dominated native
    # object decoding even though the format strings were identical.
    if not isinstance(fmt, str):
        return Struct(fmt)
    layout = _FORMAT_CACHE.get(fmt)
    if layout is not None:
        return layout
    layout = Struct(fmt)
    if len(_FORMAT_CACHE) >= 100:
        _FORMAT_CACHE.clear()
    _FORMAT_CACHE[fmt] = layout
    return layout


def _clearcache() -> None:
    _FORMAT_CACHE.clear()


def calcsize(fmt: str) -> int:
    return _cached_struct(fmt).size


def pack(fmt: str, *values) -> bytes:
    return _cached_struct(fmt).pack(*values)


def unpack(fmt: str, data: bytes) -> tuple:
    return _cached_struct(fmt).unpack(data)


def pack_into(fmt: str, buffer, offset: int, *values) -> None:
    _cached_struct(fmt).pack_into(buffer, offset, *values)


def unpack_from(fmt: str, buffer, offset: int = 0) -> tuple:
    return _cached_struct(fmt).unpack_from(buffer, offset)


def iter_unpack(fmt: str, buffer):
    return _cached_struct(fmt).iter_unpack(buffer)
