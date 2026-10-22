"""Small RFC 4122 UUID surface used by native build tooling.

The implementation is intentionally platform independent except for
``uuid4()``, whose entropy comes from the compiler-owned ``os.urandom``
primitive.  It covers the UUID operations used by Meson: construction from
text/bytes/an integer, the common value attributes, random UUIDs, and
deterministic SHA-1 namespace UUIDs.

Time-based UUIDs and MD5 namespace UUIDs remain fail-closed.  The native
stdlib does not yet own the host-node discovery/clock-state contract needed
by UUID1, and its current MD5 compatibility surface is not a cryptographically
correct MD5 implementation.
"""
from __future__ import annotations

import hashlib
import os


RESERVED_NCS = "reserved for NCS compatibility"
RFC_4122 = "specified in RFC 4122"
RESERVED_MICROSOFT = "reserved for Microsoft compatibility"
RESERVED_FUTURE = "reserved for future definition"


def _hex_digit_value(character):
    if character >= "0" and character <= "9":
        return ord(character) - ord("0")
    if character >= "a" and character <= "f":
        return ord(character) - ord("a") + 10
    if character >= "A" and character <= "F":
        return ord(character) - ord("A") + 10
    return -1


def _parse_hex(text):
    value = 0
    for character in text:
        digit = _hex_digit_value(character)
        if digit < 0:
            raise ValueError("badly formed hexadecimal UUID string")
        value = (value << 4) | digit
    return value


def _format_hex(value):
    digits = "0123456789abcdef"
    out = ""
    shift = 124
    while shift >= 0:
        out = out + digits[(value >> shift) & 15]
        shift -= 4
    return out


def _normalize_hex(text):
    text = str(text)
    text = text.replace("urn:", "").replace("uuid:", "")
    if len(text) >= 2 and text[0] == "{" and text[-1] == "}":
        text = text[1:-1]
    text = text.replace("-", "")
    if len(text) != 32:
        raise ValueError("badly formed hexadecimal UUID string")
    return text


def _bytes_to_int(value, argument_name):
    # Keep the builtin names lexically visible here.  UUID.__init__ mirrors
    # CPython's parameter names (``bytes`` and ``int``), which intentionally
    # shadow those builtins inside that method.
    if not isinstance(value, bytes):
        raise TypeError(argument_name + " is not a 16-char string")
    if len(value) != 16:
        raise ValueError(argument_name + " is not a 16-char string")
    return int.from_bytes(value, "big")


def _swap_little_endian_fields(value):
    _bytes_to_int(value, "bytes_le")
    return bytes(
        [
            value[3],
            value[2],
            value[1],
            value[0],
            value[5],
            value[4],
            value[7],
            value[6],
            value[8],
            value[9],
            value[10],
            value[11],
            value[12],
            value[13],
            value[14],
            value[15],
        ]
    )


class UUID:
    """An immutable-value-compatible 128-bit UUID representation.

    The pcc object model does not yet enforce CPython's ``__slots__`` based
    immutability, but equality, hashing, formatting, and all exposed value
    attributes follow CPython for the supported constructor forms.
    """

    def __init__(
        self,
        hex=None,
        bytes=None,
        bytes_le=None,
        fields=None,
        int=None,
        version=None,
        is_safe=None,
    ):
        supplied = 0
        if hex is not None:
            supplied += 1
        if bytes is not None:
            supplied += 1
        if bytes_le is not None:
            supplied += 1
        if fields is not None:
            supplied += 1
        if int is not None:
            supplied += 1
        if supplied != 1:
            raise TypeError(
                "one of the hex, bytes, bytes_le, fields, or int arguments "
                "must be given"
            )

        value = 0
        if hex is not None:
            value = _parse_hex(_normalize_hex(hex))
        elif bytes is not None:
            value = _bytes_to_int(bytes, "bytes")
        elif bytes_le is not None:
            reordered = _swap_little_endian_fields(bytes_le)
            value = _bytes_to_int(reordered, "bytes_le")
        elif fields is not None:
            if len(fields) != 6:
                raise ValueError("fields is not a 6-tuple")
            time_low = fields[0]
            time_mid = fields[1]
            time_hi_version = fields[2]
            clock_seq_hi_variant = fields[3]
            clock_seq_low = fields[4]
            node = fields[5]
            limits = [1 << 32, 1 << 16, 1 << 16, 1 << 8, 1 << 8, 1 << 48]
            index = 0
            while index < 6:
                if fields[index] < 0 or fields[index] >= limits[index]:
                    raise ValueError("field out of range")
                index += 1
            value = (
                (time_low << 96)
                | (time_mid << 80)
                | (time_hi_version << 64)
                | (clock_seq_hi_variant << 56)
                | (clock_seq_low << 48)
                | node
            )
        else:
            value = int

        if value < 0 or value >= (1 << 128):
            raise ValueError("int is out of range (need a 128-bit value)")
        if version is not None:
            if version < 1 or version > 5:
                raise ValueError("illegal version number")
            value &= ~(0xC000 << 48)
            value |= 0x8000 << 48
            value &= ~(0xF000 << 64)
            value |= version << 76

        self.int = value
        self.is_safe = is_safe

    def __eq__(self, other):
        if isinstance(other, UUID):
            return self.int == other.int
        return NotImplemented

    def __lt__(self, other):
        if isinstance(other, UUID):
            return self.int < other.int
        return NotImplemented

    def __hash__(self):
        return hash(self.int)

    def __int__(self):
        return self.int

    def __repr__(self):
        return "UUID('" + str(self) + "')"

    def __str__(self):
        text = self.hex
        return (
            text[:8]
            + "-"
            + text[8:12]
            + "-"
            + text[12:16]
            + "-"
            + text[16:20]
            + "-"
            + text[20:]
        )

    @property
    def bytes(self):
        return self.int.to_bytes(16, "big")

    @property
    def bytes_le(self):
        return _swap_little_endian_fields(self.bytes)

    @property
    def fields(self):
        return (
            self.time_low,
            self.time_mid,
            self.time_hi_version,
            self.clock_seq_hi_variant,
            self.clock_seq_low,
            self.node,
        )

    @property
    def time_low(self):
        return self.int >> 96

    @property
    def time_mid(self):
        return (self.int >> 80) & 0xFFFF

    @property
    def time_hi_version(self):
        return (self.int >> 64) & 0xFFFF

    @property
    def clock_seq_hi_variant(self):
        return (self.int >> 56) & 0xFF

    @property
    def clock_seq_low(self):
        return (self.int >> 48) & 0xFF

    @property
    def node(self):
        return self.int & 0xFFFFFFFFFFFF

    @property
    def time(self):
        return (
            ((self.time_hi_version & 0x0FFF) << 48)
            | (self.time_mid << 32)
            | self.time_low
        )

    @property
    def clock_seq(self):
        return ((self.clock_seq_hi_variant & 0x3F) << 8) | self.clock_seq_low

    @property
    def hex(self):
        return _format_hex(self.int)

    @property
    def urn(self):
        return "urn:uuid:" + str(self)

    @property
    def variant(self):
        if not (self.int & (0x8000 << 48)):
            return RESERVED_NCS
        if not (self.int & (0x4000 << 48)):
            return RFC_4122
        if not (self.int & (0x2000 << 48)):
            return RESERVED_MICROSOFT
        return RESERVED_FUTURE

    @property
    def version(self):
        if self.variant == RFC_4122:
            return (self.int >> 76) & 0xF
        return None


def uuid4():
    """Generate a random RFC 4122 version-4 UUID."""
    return UUID(bytes=os.urandom(16), version=4)


def uuid5(namespace, name):
    """Generate an RFC 4122 version-5 UUID from a namespace and name."""
    if not isinstance(namespace, UUID):
        raise TypeError("namespace must be a UUID")
    if not isinstance(name, str):
        raise TypeError("name must be a string")
    digest = hashlib.sha1(namespace.bytes + name.encode("utf-8")).digest()
    return UUID(bytes=digest[:16], version=5)


def uuid1(node=None, clock_seq=None):
    raise NotImplementedError(
        "uuid1 requires native node discovery and persistent clock state"
    )


def uuid3(namespace, name):
    raise NotImplementedError("uuid3 requires a correct native MD5 implementation")


def getnode():
    raise NotImplementedError("getnode requires native hardware-address discovery")


NAMESPACE_DNS = UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
NAMESPACE_URL = UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")
NAMESPACE_OID = UUID("6ba7b812-9dad-11d1-80b4-00c04fd430c8")
NAMESPACE_X500 = UUID("6ba7b814-9dad-11d1-80b4-00c04fd430c8")
