"""``struct`` provider for pcc-owned code and compiled programs.

Standard layouts (``<``, ``>``, ``!``, ``=``) cover every integer, bytes,
bool and IEEE float (``e``/``f``/``d``) code.  ``@`` and a format with no
prefix use the LP64 native layout of pcc's little-endian targets: ``l``/``L``,
``n``/``N`` and ``P`` are 8 bytes and every field is aligned to its size.
Unsupported shapes fail before producing partial bytes.
"""
from __future__ import annotations

try:
    from pcc.stdlib._pcc_float_bits import _bits_to_float16, _bits_to_float32, _bits_to_float64, _float16_to_bits, _float32_to_bits, _float64_to_bits
except ImportError:
    # pcc publishes this provider as the top-level stdlib module ``struct``.
    from _pcc_float_bits import (
        _bits_to_float16,
        _bits_to_float32,
        _bits_to_float64,
        _float16_to_bits,
        _float32_to_bits,
        _float64_to_bits,
    )
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
_KIND_FLOAT = 5


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
    "e": 2,
    "f": 4,
    "d": 8,
    "s": 1,
}


def _parse_format(fmt: str):
    if not isinstance(fmt, str):
        raise TypeError("Struct() argument 1 must be a str")
    if fmt == "":
        return "little", []
    prefix = fmt[0]
    native = False
    if prefix == "<" or prefix == "=" :
        byteorder = "little"
        index = 1
    elif prefix == ">" or prefix == "!":
        byteorder = "big"
        index = 1
    elif prefix == "@":
        byteorder = "little"
        native = True
        index = 1
    else:
        # No prefix is ``@``: native sizes and alignment.
        byteorder = "little"
        native = True
        index = 0

    fields = []
    repeat = -1
    while index < len(fmt):
        ch = fmt[index]
        index += 1
        if ch == " ":
            continue
        if ch >= "0" and ch <= "9":
            if repeat < 0:
                repeat = 0
            repeat = repeat * 10 + (ord(ch) - ord("0"))
            continue
        if ch not in _SIZES and not (native and ch in "nNP"):
            raise error("bad char in struct format")
        count = repeat if repeat >= 0 else 1
        fields.append((ch, count))
        repeat = -1
    if repeat >= 0:
        raise error("repeat count given without format specifier")
    if native:
        fields = _native_layout(fields)
    return byteorder, fields


# LP64 native spellings of the 8-byte integer codes.
_NATIVE_WIDE = {"l": "q", "L": "Q", "n": "q", "N": "Q", "P": "Q"}


def _native_layout(fields):
    """``@`` fields: 8-byte ``l``/``L``/``n``/``N``/``P`` and size alignment.

    Each field starts at a multiple of its item size (bytes, chars and pads
    are unaligned); the struct is not padded at its end, as in CPython.
    """
    out = []
    offset = 0
    for ch, count in fields:
        ch = _NATIVE_WIDE.get(ch, ch)
        size = _SIZES[ch]
        if ch != "x" and ch != "s" and ch != "c" and size > 1 and count > 0:
            padding = (-offset) % size
            if padding:
                out.append(("x", padding))
                offset += padding
        out.append((ch, count))
        offset += size * count
    return out


def _float_width_bits(ch: str, value: float):
    """IEEE bits of ``value`` for float code ``ch``; finite overflow raises.

    Unannotated result, like ``_float64_to_bits``: a negative double's pattern
    is >= 2**63, which an ``-> int`` i64 lane cannot carry in pcc's own build.
    """
    if ch == "d":
        return _float64_to_bits(value)
    bits = _float32_to_bits(value) if ch == "f" else _float16_to_bits(value)
    exponent_mask = 0x7F800000 if ch == "f" else 0x7C00
    if (bits & exponent_mask) == exponent_mask and value == value:
        if value - value == 0.0:
            raise OverflowError("float too large to pack with " + ch + " format")
    return bits


def _float_from_bits(width: int, bits) -> float:
    if width == 8:
        return _bits_to_float64(bits)
    if width == 4:
        return _bits_to_float32(bits)
    return _bits_to_float16(bits)


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


def _pack_int(numeric, width: int, signed: int, byteorder: str) -> bytes:
    """Range-check and encode one integer without bigint constants.

    A 64-bit bound such as ``2**64`` is a bigint in pcc's own build, so the
    check uses shifts and the value is written as two 32-bit halves: an i64
    field never leaves the machine-integer lane.
    """
    if width == 8:
        top = numeric >> 63
        if signed:
            if top != 0 and top != -1:
                raise error("argument out of range")
        elif numeric < 0 or top > 1:
            raise error("argument out of range")
        low = (numeric & 0xFFFFFFFF).to_bytes(4, byteorder)
        high = ((numeric >> 32) & 0xFFFFFFFF).to_bytes(4, byteorder)
        if byteorder == "little":
            return low + high
        return high + low
    bits = width * 8
    if signed:
        limit = 1 << (bits - 1)
        if numeric < -limit or numeric >= limit:
            raise error("argument out of range")
        if numeric < 0:
            numeric += 1 << bits
    elif numeric < 0 or numeric >= (1 << bits):
        raise error("argument out of range")
    return numeric.to_bytes(width, byteorder)


def _plan_value_count(plan) -> int:
    total = 0
    for kind, _width, _signed, count in plan:
        if kind == _KIND_BYTES:
            total += 1
        elif kind != _KIND_PAD:
            total += count
    return total


def _pack_plan(byteorder: str, plan, value_count: int, values) -> bytes:
    if len(values) != value_count:
        raise error(
            "pack expected "
            + str(value_count)
            + " items for packing (got "
            + str(len(values))
            + ")"
        )
    parts = []
    value_index = 0
    for kind, width, signed, count in plan:
        if kind == _KIND_PAD:
            parts.append(b"\x00" * count)
            continue
        if kind == _KIND_BYTES:
            raw = values[value_index]
            value_index += 1
            if not isinstance(raw, (bytes, bytearray)):
                raise error("argument for 's' must be a bytes object")
            raw = bytes(raw)
            parts.append(raw[:count])
            if len(raw) < count:
                parts.append(b"\x00" * (count - len(raw)))
            continue
        item_index = 0
        while item_index < count:
            value = values[value_index]
            value_index += 1
            item_index += 1
            if kind == _KIND_CHAR:
                if not isinstance(value, (bytes, bytearray)) or len(value) != 1:
                    raise error("char format requires a bytes object of length 1")
                parts.append(bytes(value))
            elif kind == _KIND_FLOAT:
                try:
                    number = float(value)
                except (TypeError, ValueError) as exc:
                    raise error("required argument is not a float") from exc
                code = "d" if width == 8 else ("f" if width == 4 else "e")
                parts.append(_float_width_bits(code, number).to_bytes(width, byteorder))
            elif kind == _KIND_BOOL:
                parts.append(b"\x01" if bool(value) else b"\x00")
            else:
                try:
                    numeric = int(value)
                except (TypeError, ValueError) as exc:
                    raise error("required argument is not an integer") from exc
                parts.append(_pack_int(numeric, width, signed, byteorder))
    return b"".join(parts)


def _normalize_offset(buffer_len: int, offset: int) -> int:
    offset = int(offset)
    if offset < 0:
        offset += buffer_len
    if offset < 0 or offset > buffer_len:
        raise error("offset out of range")
    return offset


def _build_plan(fields) -> list[tuple[int, int, int, int]]:
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
        elif ch == "e" or ch == "f" or ch == "d":
            plan.append((_KIND_FLOAT, _SIZES[ch], 0, count))
        else:
            width, signed = _integer_shape(ch)
            plan.append((_KIND_INT, width, 1 if signed else 0, count))
    return plan


def _unpack_generic(
    byteorder: str, plan: list[tuple[int, int, int, int]], raw, offset: int
) -> tuple:
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
            if kind == _KIND_FLOAT:
                cursor += width
                values.append(_float_from_bits(width, numeric))
                continue
            if signed != 0:
                # Derive both bounds from ``width`` with one shift each.
                #
                # The replaced form chose ``modulus`` through an if/elif/else
                # over 0x100 / 0x10000 / 0x100000000 / 0x10000000000000000 and
                # tested ``numeric >= modulus // 2``.  Compiled, that returned
                # every 8-byte signed big-endian field exactly one modulus low
                # -- ">q" and ">qq" decoded 806238318454171918 as
                # -17640505755255379698 -- while widths 1/2/4 and every
                # little-endian form were correct.  The same shape in an
                # isolated probe answers correctly, so the trigger is
                # something about this function, not the branch chain itself;
                # it is not diagnosed.  This form is differentially checked
                # against CPython across the signed/unsigned and
                # little/big-endian matrix in
                # tests/python/test_native_isinstance_and_struct_boundaries.py.
                sign_bit = 1 << (width * 8 - 1)
                if numeric >= sign_bit:
                    numeric -= sign_bit * 2
            cursor += width
            values.append(bool(numeric) if kind == _KIND_BOOL else numeric)
    return tuple(values)


def _unpack_native_little(
    plan: list[tuple[int, int, int, int]], raw, offset: int
) -> tuple:
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
    if _NATIVE_PAYLOAD_READS and byteorder == "little" and not _plan_has_float(plan):
        return _unpack_native_little(plan, raw, offset)
    return _unpack_generic(byteorder, plan, raw, offset)


def _plan_has_float(plan) -> bool:
    for kind, _width, _signed, _count in plan:
        if kind == _KIND_FLOAT:
            return True
    return False


def _unpack_fields(byteorder: str, fields, buffer, offset: int):
    return _unpack_plan(
        byteorder, _build_plan(fields), _format_size(fields), buffer, offset
    )


def _single_int_shape(plan, byteorder: str):
    """``(width, byteorder, sign_bit, modulus)`` for a one-integer format.

    ``"<Q"``/``"<I"``/``"<i"`` and friends are nearly every call the compiled
    linker makes: the Mach-O relocation reader and the stack-map validator do
    nothing else, tens of thousands of times per link.  Reading the bytes is
    not what costs -- 200k compiled ``<Q`` reads take 0.197s through
    ``int.from_bytes`` and 1.724s through the generic plan -- so the plan walk,
    its values list and its result tuple are the 8.8x.

    ``sign_bit``/``modulus`` are precomputed because ``int.from_bytes`` cannot
    take ``signed=`` here: that keyword leaves a ``py_cpy_*`` call in the IR and
    ``--python-libpython=off`` then replaces the whole function with a
    fail-closed stub.  A literal or dynamic ``byteorder`` argument stays native.
    """
    if len(plan) != 1:
        return None
    kind, width, signed, count = plan[0]
    if kind != _KIND_INT or count != 1:
        return None
    if signed != 0:
        return (width, byteorder, 1 << (width * 8 - 1), 1 << (width * 8))
    return (width, byteorder, 0, 0)


class Struct:
    def __init__(self, fmt: str):
        self.format = fmt
        self._byteorder, self._fields = _parse_format(fmt)
        self.size = _format_size(self._fields)
        self._plan = _build_plan(self._fields)
        self._value_count = _plan_value_count(self._plan)
        self._single_int = _single_int_shape(self._plan, self._byteorder)

    def pack(self, *values) -> bytes:
        return _pack_plan(self._byteorder, self._plan, self._value_count, values)

    def unpack(self, buffer) -> tuple:
        raw = bytes(buffer)
        if len(raw) != self.size:
            raise error(
                "unpack requires a buffer of " + str(self.size) + " bytes"
            )
        return _unpack_plan(self._byteorder, self._plan, self.size, raw, 0)

    def unpack_from(self, buffer, offset: int = 0) -> tuple:
        shape = self._single_int
        if shape is not None:
            width, byteorder, sign_bit, modulus = shape
            raw = (
                buffer
                if isinstance(buffer, (bytes, bytearray))
                else bytes(buffer)
            )
            start = _normalize_offset(len(raw), offset)
            if start + width > len(raw):
                raise error(
                    "unpack_from requires a buffer of at least "
                    + str(start + width)
                    + " bytes"
                )
            value = int.from_bytes(raw[start : start + width], byteorder)
            if sign_bit and value >= sign_bit:
                value = value - modulus
            return (value,)
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
        if type(buffer) is bytearray:
            buffer[offset:offset + len(payload)] = payload
            return
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
