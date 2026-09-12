"""Versioned pcc-owned precise stack-map object-section ABI.

The runtime's existing registered-slot protocol remains authoritative.  This
module owns the *machine metadata* boundary needed to compare that protocol
with final self-backend locations without importing LLVM's stack-map parser.

All integers are little-endian.  Records are deliberately fixed-width except
for their location arrays, and every count is bounded before allocation.  A
relocatable object stores zero in ``function_address`` and carries one native
object relocation for that field; a final image must contain the resolved
address.  ``instruction_offset`` and ``exceptional_offset`` are always final
function-relative offsets, never assembler instruction ordinals.

Layout (version 1)::

    header      <8sHBBI     magic, version, arch, pointer-size, functions
    function    <QQIIII     stable-id, address, code-size, frame-size,
                            record-count, flags
    record      <QIIIHHBBHI stable-id, pc-offset, exceptional-offset,
                            continuation-id, location-count, reserved,
                            kind, flags, reserved, location-index
    location    <BBHHhiI    kind, flags, size, dwarf-register, base-index,
                            signed frame/register offset, extent

Version 2 interns the location lists.  Records no longer carry their
locations inline; each names ``location-index`` into one table that follows
every function, and identical lists share an entry.  A self-host link has
2,366,390 records that reference only 47,310 distinct root-set shapes, so
inline locations made the section 89.7% of the linked image (810 MB against
88 MB of code) — interning takes the same information to about 22 MB.  The
described root sets are unchanged; only their storage is shared.

The ABI does not contain a generic ``pointer`` location.  A location is
accepted only when it is explicitly marked managed; interior/derived values
must name an earlier base location.  This makes raw ``c_ptr`` ambiguity a
producer error instead of silently teaching a moving collector to rewrite it.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass


class PreciseStackMapError(Exception):
    """Malformed or semantically incomplete pcc stack-map metadata."""


MAGIC = b"PCCSMAP1"
VERSION = 2
POINTER_SIZE = 8

ARCH_AARCH64 = 1
ARCH_X86_64 = 2
ARCH_NAMES = {
    ARCH_AARCH64: "aarch64",
    ARCH_X86_64: "x86_64",
}

SAFEPOINT_ENTRY = 1
SAFEPOINT_LOOP = 2
SAFEPOINT_CALL = 3
SAFEPOINT_EXCEPTION = 4
SAFEPOINT_CONTINUATION = 5
SAFEPOINT_KINDS = {
    SAFEPOINT_ENTRY,
    SAFEPOINT_LOOP,
    SAFEPOINT_CALL,
    SAFEPOINT_EXCEPTION,
    SAFEPOINT_CONTINUATION,
}

LOCATION_STACK_INDIRECT = 1
LOCATION_REGISTER = 2
LOCATION_STACK_DIRECT = 3
LOCATION_KINDS = {
    LOCATION_STACK_INDIRECT,
    LOCATION_REGISTER,
    LOCATION_STACK_DIRECT,
}

LOCATION_MANAGED = 1 << 0
LOCATION_OWNED = 1 << 1
LOCATION_DERIVED = 1 << 2
LOCATION_RELOAD_REQUIRED = 1 << 3
LOCATION_FLAGS = (
    LOCATION_MANAGED
    | LOCATION_OWNED
    | LOCATION_DERIVED
    | LOCATION_RELOAD_REQUIRED
)

RECORD_HAS_EXCEPTION_EDGE = 1 << 0
RECORD_SUSPENDED = 1 << 1
RECORD_FLAGS = RECORD_HAS_EXCEPTION_EDGE | RECORD_SUSPENDED

NO_OFFSET = 0xFFFFFFFF
NO_BASE = -1
MAX_FUNCTIONS = 1_000_000
MAX_RECORDS = 8_000_000  # same rationale as MAX_LOCATIONS below: the pcc
                         # compiler closure is a legitimate payload and
                         # grew past 4M safepoint records when the C
                         # frontend joined it.  Not a format bound --
                         # the per-function count is a u32 field and the
                         # total is only summed here -- so this guards
                         # against absurd payloads, not legit closures.
MAX_LOCATIONS = 128_000_000  # merged pcc compiler closure exceeds 16M
                             # managed locations; the bound guards
                             # against absurd payloads, not legit closures

_HEADER = struct.Struct("<8sHBBIII")
_FUNCTION = struct.Struct("<QQIIII")
_RECORD = struct.Struct("<QIIIHHBBHI")
_LOCATION = struct.Struct("<BBHHhiI")

# Public numeric projection for freestanding consumers.  The struct codecs
# above remain the authority; downstream ABI tables import these derived
# values instead of copying their sizes and magic by hand.
MAGIC_I64 = int.from_bytes(MAGIC, "little")
HEADER_SIZE = _HEADER.size
FUNCTION_SIZE = _FUNCTION.size
RECORD_SIZE = _RECORD.size
LOCATION_SIZE = _LOCATION.size


@dataclass(frozen=True)
class StackMapLocation:
    kind: int
    flags: int
    size: int = POINTER_SIZE
    register: int = 0
    base_index: int = NO_BASE
    offset: int = 0
    extent: int = POINTER_SIZE


@dataclass(frozen=True)
class SafepointRecord:
    safepoint_id: int
    instruction_offset: int
    kind: int
    locations: tuple[StackMapLocation, ...]
    flags: int = 0
    exceptional_offset: int = NO_OFFSET
    continuation_id: int = 0


@dataclass(frozen=True)
class FunctionStackMap:
    function_id: int
    function_address: int
    code_size: int
    frame_size: int
    records: tuple[SafepointRecord, ...]
    flags: int = 0


@dataclass(frozen=True)
class PreciseStackMap:
    arch: int
    functions: tuple[FunctionStackMap, ...]


_STABLE_ID_INIT_HIGH = 0xCBF29CE4
_STABLE_ID_INIT_LOW = 0x84222325


def _stable_id_feed(payload: bytes, high: int, low: int) -> int:
    """Absorb *payload* into an FNV-1a state, returned packed as high<<32|low.

    FNV-1a is a streaming hash, so a caller that repeatedly hashes a long
    constant prefix followed by a few varying bytes can absorb the prefix once
    and resume from the returned state.  The result is bit-identical to hashing
    the concatenation in one pass — this only changes *when* the bytes are
    absorbed, never which bytes or their order.

    The state is packed into one int rather than returned as a tuple so the
    hot resume path allocates nothing.
    """
    for byte in payload:
        low ^= byte
        low_product = low * 0x1B3
        high = (
            high * 0x1B3
            + (low_product >> 32)
            + ((low << 8) & 0xFFFFFFFF)
        ) & 0xFFFFFFFF
        low = low_product & 0xFFFFFFFF
    return high * 0x100000000 + low


def stable_id_prefix_state(namespace: str, *parts: str) -> int:
    """Return the packed FNV state after ``namespace`` and *parts*.

    The caller resumes with :func:`stable_id_resume`, which appends the
    remaining ``\\0``-separated fields.  Splitting the hash this way is what
    keeps a per-safepoint identity from re-hashing its ~80-character function
    symbol on every record.
    """
    fields = (namespace,) + parts
    for field in fields:
        if not isinstance(field, str) or not field or "\0" in field:
            raise PreciseStackMapError(
                "stack-map identity fields must be non-empty and contain no NUL"
            )
    return _stable_id_feed(
        "\0".join(fields).encode("utf-8"),
        _STABLE_ID_INIT_HIGH,
        _STABLE_ID_INIT_LOW,
    )


def stable_id_prefix_limb(
    namespace: str,
    part: str,
    high_limb: bool,
) -> int:
    """Hash a two-field prefix and return one 32-bit FNV limb.

    The self-hosted compiler must not first pack these limbs into an unsigned
    64-bit Python int: when the high bit is set that crosses its bignum/value
    projection, and splitting the packed value later can lose both limbs.
    Recomputing this once-per-function prefix twice is cheap and keeps every
    intermediate below 2**42.
    """
    if (
        not isinstance(namespace, str)
        or not namespace
        or "\0" in namespace
        or not isinstance(part, str)
        or not part
        or "\0" in part
    ):
        raise PreciseStackMapError(
            "stack-map identity fields must be non-empty and contain no NUL"
        )
    high = _STABLE_ID_INIT_HIGH
    low = _STABLE_ID_INIT_LOW
    for byte in (namespace + "\0" + part).encode("utf-8"):
        low ^= byte
        low_product = low * 0x1B3
        high = (
            high * 0x1B3
            + (low_product >> 32)
            + ((low << 8) & 0xFFFFFFFF)
        ) & 0xFFFFFFFF
        low = low_product & 0xFFFFFFFF
    return high if high_limb else low


def stable_id_resume(state: int, *parts: str) -> int:
    """Finish an identity begun by :func:`stable_id_prefix_state`.

    Each part is preceded by the same ``\\0`` separator ``"\\0".join`` would
    have produced, so the absorbed byte stream is identical.

    CONTRACT: every part must be non-empty and free of NUL.  Unlike
    :func:`scoped_stable_id` this deliberately does **not** validate.  It runs
    once per safepoint, so the check is real per-record cost on the hottest
    path in stack-map planning — and the field validation itself misbehaved
    under a self-compiled pcc1, rejecting decimal digit strings that are
    plainly valid while the identical call succeeded on host CPython.  That is
    an unresolved pcc1 gap in ``isinstance(x, str)`` or ``"\\0" in x`` and
    deserves its own investigation; it is avoided here rather than diagnosed.
    Both callers pass ``str(...)`` of an integer validated upstream, so the
    contract holds by construction.  Keep it that way.
    """
    high = state >> 32
    low = state & 0xFFFFFFFF
    for part in parts:
        state = _stable_id_feed(("\0" + part).encode("utf-8"), high, low)
        high = state >> 32
        low = state & 0xFFFFFFFF
    value = (high & 0x7FFFFFFF) * 0x100000000 + low
    return value or 1


def _stable_id_bytes(payload: bytes) -> int:
    # Compute FNV-1a modulo 2**64 as two 32-bit limbs.  The emitter is part of
    # the pcc1 closure: spelling the usual ``value * prime & UINT64_MAX``
    # creates a bignum intermediate there, and a later native-i64 projection
    # would lose the high word.  With prime == 2**40 + 0x1b3, each limb
    # product stays below 2**42 and is therefore exact in both host Python and
    # pcc's proven native-int lane.
    high = 0xCBF29CE4
    low = 0x84222325
    for byte in payload:
        low ^= byte
        low_product = low * 0x1B3
        high = (
            high * 0x1B3
            + (low_product >> 32)
            + ((low << 8) & 0xFFFFFFFF)
        ) & 0xFFFFFFFF
        low = low_product & 0xFFFFFFFF
    # IDs cross the self-backend's signed-i64 ABI.  Keep the positive 63-bit
    # FNV projection rather than relying on an unsigned Python-int projection
    # that the native ABI cannot represent.  0 remains reserved as before.
    value = (high & 0x7FFFFFFF) * 0x100000000 + low
    return value or 1


def stable_id(text: str) -> int:
    """Return the stable positive 63-bit FNV-1a identity used by this ABI."""
    if not isinstance(text, str) or not text or "\0" in text:
        raise PreciseStackMapError(
            "stack-map identity text must be non-empty and contain no NUL"
        )
    return _stable_id_bytes(text.encode("utf-8"))


def scoped_stable_id(namespace: str, *parts: str) -> int:
    """Hash validated identity fields with an unambiguous NUL separator."""
    fields = (namespace,) + parts
    for field in fields:
        if not isinstance(field, str) or not field or "\0" in field:
            raise PreciseStackMapError(
                "stack-map identity fields must be non-empty and contain no NUL"
            )
    return _stable_id_bytes("\0".join(fields).encode("utf-8"))


def function_id(symbol: str) -> int:
    return scoped_stable_id("function", symbol)


def safepoint_id(symbol: str, ordinal: int, kind: int) -> int:
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 0:
        raise PreciseStackMapError("safepoint ordinal must be non-negative")
    if kind not in SAFEPOINT_KINDS:
        raise PreciseStackMapError(f"unknown safepoint kind {kind}")
    return scoped_stable_id("safepoint", symbol, str(ordinal), str(kind))


def safepoint_id_from_prefix_limbs(
    prefix_high: int,
    prefix_low: int,
    ordinal: int,
    kind: int,
) -> int:
    """Finish a safepoint FNV identity without string/bytes projection.

    ``prefix_high``/``prefix_low`` are the two 32-bit limbs returned by
    splitting ``stable_id_prefix_state("safepoint", symbol)``.  The remaining
    wire text is exactly ``NUL + decimal(ordinal) + NUL + decimal(kind)``.
    Feeding those ASCII digits numerically preserves the public stable ID but
    avoids two ``str`` objects, two separator concatenations and two UTF-8
    byte objects per compiler safepoint.
    """
    high = prefix_high
    low = prefix_low
    field_index = 0
    while field_index < 2:
        # NUL separator preceding each remaining identity field.
        low_product = low * 0x1B3
        high = (
            high * 0x1B3
            + (low_product >> 32)
            + ((low << 8) & 0xFFFFFFFF)
        ) & 0xFFFFFFFF
        low = low_product & 0xFFFFFFFF

        field_value = ordinal if field_index == 0 else kind
        divisor = 1
        while field_value >= divisor * 10:
            divisor *= 10
        while divisor > 0:
            digit = field_value // divisor
            byte = 48 + digit
            low ^= byte
            low_product = low * 0x1B3
            high = (
                high * 0x1B3
                + (low_product >> 32)
                + ((low << 8) & 0xFFFFFFFF)
            ) & 0xFFFFFFFF
            low = low_product & 0xFFFFFFFF
            field_value %= divisor
            divisor //= 10
        field_index += 1
    value = (high & 0x7FFFFFFF) * 0x100000000 + low
    return value or 1


def _check_uint(value: int, bits: int, label: str, *, nonzero: bool = False) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise PreciseStackMapError(f"{label} must be an integer")
    # These are the only integer widths in the stack-map wire format.  Keep
    # them literal: the dynamic ``(1 << bits) - 1`` expression produces a
    # bignum for uint64 in pcc1 and crosses an unnecessary representation
    # boundary inside this validator.
    if bits == 8:
        limit = 0xFF
    elif bits == 16:
        limit = 0xFFFF
    elif bits == 32:
        limit = 0xFFFFFFFF
    elif bits == 64:
        limit = ((0xFFFFFFFF << 32) | 0xFFFFFFFF)
    else:
        raise PreciseStackMapError(f"unsupported unsigned width {bits}")
    if value < (1 if nonzero else 0) or value > limit:
        raise PreciseStackMapError(f"{label} is outside uint{bits}")


def _register_limit(arch: int) -> int:
    # DWARF register numbers accepted by the initial ABI.  AArch64 x0..x30,
    # sp=31; x86-64 general registers 0..16.  Vector roots are deliberately
    # not admitted until a target-specific relocation gate owns them.
    return 31 if arch == ARCH_AARCH64 else 16


def _validate_location(
    location: StackMapLocation,
    *,
    arch: int,
    frame_size: int,
    index: int,
    prior: tuple[StackMapLocation, ...],
) -> None:
    if location.kind not in LOCATION_KINDS:
        raise PreciseStackMapError(f"unknown location kind {location.kind}")
    if location.flags & ~LOCATION_FLAGS:
        raise PreciseStackMapError("unknown stack-map location flags")
    if not location.flags & LOCATION_MANAGED:
        raise PreciseStackMapError(
            "unclassified raw pointer is not a managed stack-map location"
        )
    if location.size != POINTER_SIZE or location.extent != POINTER_SIZE:
        raise PreciseStackMapError("managed locations must be one pointer wide")
    _check_uint(location.register, 16, "location register")
    if (
        not isinstance(location.base_index, int)
        or isinstance(location.base_index, bool)
        or not -(1 << 15) <= location.base_index < (1 << 15)
    ):
        raise PreciseStackMapError("location base index is outside int16")
    if (
        not isinstance(location.offset, int)
        or isinstance(location.offset, bool)
        or not -(1 << 31) <= location.offset < (1 << 31)
    ):
        raise PreciseStackMapError("location offset is outside int32")
    if location.register > _register_limit(arch):
        raise PreciseStackMapError(
            f"register {location.register} is outside {ARCH_NAMES[arch]} ABI"
        )
    if location.kind in (LOCATION_STACK_INDIRECT, LOCATION_STACK_DIRECT):
        stack_registers = (29, 31) if arch == ARCH_AARCH64 else (6, 7)
        if location.register not in stack_registers:
            raise PreciseStackMapError(
                "stack location must be relative to the target frame/stack register"
            )
        if location.offset >= 0 or -location.offset > frame_size:
            raise PreciseStackMapError(
                f"stack location {location.offset} exceeds frame size {frame_size}"
            )
        if (-location.offset) % POINTER_SIZE:
            raise PreciseStackMapError("managed stack location is not aligned")
    elif location.offset != 0:
        raise PreciseStackMapError("register location cannot carry frame offset")

    is_derived = bool(location.flags & LOCATION_DERIVED)
    if is_derived:
        if not 0 <= location.base_index < index:
            raise PreciseStackMapError(
                "derived location must name an earlier base location"
            )
        base = prior[location.base_index]
        if not base.flags & LOCATION_MANAGED or base.flags & LOCATION_DERIVED:
            raise PreciseStackMapError("derived location base is not a base root")
        if not location.flags & LOCATION_RELOAD_REQUIRED:
            raise PreciseStackMapError(
                "derived location must be reloaded after a relocating safepoint"
            )
    elif location.base_index != NO_BASE:
        raise PreciseStackMapError("non-derived location carries a base index")

    if (
        location.kind == LOCATION_REGISTER
        and not location.flags & LOCATION_RELOAD_REQUIRED
    ):
        raise PreciseStackMapError(
            "managed register location must reject stale post-safepoint SSA use"
        )


def validate_stack_map(value: PreciseStackMap, *, final_image: bool = False) -> None:
    if value.arch not in ARCH_NAMES:
        raise PreciseStackMapError(f"unknown stack-map architecture {value.arch}")
    if len(value.functions) > MAX_FUNCTIONS:
        raise PreciseStackMapError("too many stack-map functions")
    previous_function_id = -1
    seen_safepoints: set[int] = set()
    total_records = 0
    total_locations = 0
    for function in value.functions:
        _check_uint(function.function_id, 64, "function id", nonzero=True)
        if function.function_id <= previous_function_id:
            raise PreciseStackMapError(
                "functions must be ordered by unique stable function id"
            )
        previous_function_id = function.function_id
        _check_uint(function.function_address, 64, "function address")
        if final_image and function.function_address == 0:
            raise PreciseStackMapError("final-image function address is unresolved")
        _check_uint(function.code_size, 32, "function code size", nonzero=True)
        _check_uint(function.frame_size, 32, "function frame size")
        if function.frame_size % 16:
            raise PreciseStackMapError("function frame size must be 16-byte aligned")
        _check_uint(function.flags, 32, "function flags")
        total_records += len(function.records)
        if total_records > MAX_RECORDS:
            raise PreciseStackMapError(
                "too many stack-map records: "
                + str(total_records)
                + " exceeds "
                + str(MAX_RECORDS)
            )
        previous_pc = -1
        for record in function.records:
            _check_uint(record.safepoint_id, 64, "safepoint id", nonzero=True)
            if record.safepoint_id in seen_safepoints:
                raise PreciseStackMapError("duplicate safepoint id")
            seen_safepoints.add(record.safepoint_id)
            if record.kind not in SAFEPOINT_KINDS:
                raise PreciseStackMapError(f"unknown safepoint kind {record.kind}")
            _check_uint(record.instruction_offset, 32, "instruction offset")
            if not previous_pc < record.instruction_offset < function.code_size:
                raise PreciseStackMapError(
                    "safepoints must have ordered in-function instruction offsets"
                )
            previous_pc = record.instruction_offset
            _check_uint(record.flags, 8, "record flags")
            if record.flags & ~RECORD_FLAGS:
                raise PreciseStackMapError("unknown stack-map record flags")
            has_exception = record.exceptional_offset != NO_OFFSET
            if has_exception:
                _check_uint(record.exceptional_offset, 32, "exceptional offset")
                if record.exceptional_offset >= function.code_size:
                    raise PreciseStackMapError("exceptional successor exceeds function")
            if has_exception != bool(record.flags & RECORD_HAS_EXCEPTION_EDGE):
                raise PreciseStackMapError("exception-edge flag and offset disagree")
            _check_uint(record.continuation_id, 32, "continuation id")
            is_continuation = record.kind == SAFEPOINT_CONTINUATION
            if is_continuation != bool(record.continuation_id):
                raise PreciseStackMapError(
                    "continuation record needs one non-zero continuation id"
                )
            if bool(record.flags & RECORD_SUSPENDED) != is_continuation:
                raise PreciseStackMapError(
                    "suspended flag is reserved for continuation records"
                )
            total_locations += len(record.locations)
            if total_locations > MAX_LOCATIONS:
                raise PreciseStackMapError("too many stack-map locations")
            # Inlined _validate_location: giant functions can carry millions
            # of managed locations, and decode_stack_map runs once per input
            # object in the link; the per-location function call + repeated
            # dataclass attribute reads dominated stack-map processing
            # (measured >10 s per 217 MB payload).  The checks are identical
            # to _validate_location and raise the same errors.
            frame_size = function.frame_size
            stack_registers = (29, 31) if value.arch == ARCH_AARCH64 else (6, 7)
            register_limit = _register_limit(value.arch)
            arch_name = ARCH_NAMES[value.arch]
            for index, location in enumerate(record.locations):
                kind = location.kind
                flags = location.flags
                if kind not in LOCATION_KINDS:
                    raise PreciseStackMapError(f"unknown location kind {kind}")
                if flags & ~LOCATION_FLAGS:
                    raise PreciseStackMapError(
                        "unknown stack-map location flags"
                    )
                if not flags & LOCATION_MANAGED:
                    raise PreciseStackMapError(
                        "unclassified raw pointer is not a managed "
                        "stack-map location"
                    )
                if (
                    location.size != POINTER_SIZE
                    or location.extent != POINTER_SIZE
                ):
                    raise PreciseStackMapError(
                        "managed locations must be one pointer wide"
                    )
                register = location.register
                _check_uint(register, 16, "location register")
                if (
                    not isinstance(location.base_index, int)
                    or isinstance(location.base_index, bool)
                    or not -(1 << 15) <= location.base_index < (1 << 15)
                ):
                    raise PreciseStackMapError(
                        "location base index is outside int16"
                    )
                offset = location.offset
                if (
                    not isinstance(offset, int)
                    or isinstance(offset, bool)
                    or not -(1 << 31) <= offset < (1 << 31)
                ):
                    raise PreciseStackMapError(
                        "location offset is outside int32"
                    )
                if register > register_limit:
                    raise PreciseStackMapError(
                        f"register {register} is outside {arch_name} ABI"
                    )
                if kind in (LOCATION_STACK_INDIRECT, LOCATION_STACK_DIRECT):
                    if register not in stack_registers:
                        raise PreciseStackMapError(
                            "stack location must be relative to the target "
                            "frame/stack register"
                        )
                    if offset >= 0 or -offset > frame_size:
                        raise PreciseStackMapError(
                            f"stack location {offset} exceeds frame size "
                            f"{frame_size}"
                        )
                    if (-offset) % POINTER_SIZE:
                        raise PreciseStackMapError(
                            "managed stack location is not aligned"
                        )
                elif offset != 0:
                    raise PreciseStackMapError(
                        "register location cannot carry frame offset"
                    )
                is_derived = bool(flags & LOCATION_DERIVED)
                if is_derived:
                    if not 0 <= location.base_index < index:
                        raise PreciseStackMapError(
                            "derived location must name an earlier base "
                            "location"
                        )
                    base = record.locations[location.base_index]
                    if (
                        not base.flags & LOCATION_MANAGED
                        or base.flags & LOCATION_DERIVED
                    ):
                        raise PreciseStackMapError(
                            "derived location base is not a base root"
                        )
                    if not flags & LOCATION_RELOAD_REQUIRED:
                        raise PreciseStackMapError(
                            "derived location must be reloaded after a "
                            "relocating safepoint"
                        )
                elif location.base_index != NO_BASE:
                    raise PreciseStackMapError(
                        "non-derived location carries a base index"
                    )
                if (
                    kind == LOCATION_REGISTER
                    and not flags & LOCATION_RELOAD_REQUIRED
                ):
                    raise PreciseStackMapError(
                        "managed register location must reject stale "
                        "post-safepoint SSA use"
                    )


def _location_key(location: "StackMapLocation") -> tuple:
    return (
        location.kind,
        location.flags,
        location.size,
        location.register,
        location.base_index,
        location.offset,
        location.extent,
    )


def encode_stack_map(value: PreciseStackMap, *, final_image: bool = False) -> bytes:
    validate_stack_map(value, final_image=final_image)
    table: list[tuple] = []
    table_index: dict[str, int] = {}
    body = bytearray()
    for function in value.functions:
        body += _FUNCTION.pack(
            function.function_id,
            function.function_address,
            function.code_size,
            function.frame_size,
            len(function.records),
            function.flags,
        )
        for record in function.records:
            # String key, not a tuple of tuples: this module is in the
            # self-host closure, so keep the hashed type to one that is
            # proven there.  `key in d` + subscript, never dict.get, which
            # mis-lowers under pcc1 into a raising getitem.
            entries = [_location_key(item) for item in record.locations]
            key = ";".join(
                ",".join(str(field) for field in entry) for entry in entries
            )
            if key in table_index:
                index = table_index[key]
            else:
                index = len(table)
                table_index[key] = index
                table.extend(entries)
            body += _RECORD.pack(
                record.safepoint_id,
                record.instruction_offset,
                record.exceptional_offset,
                record.continuation_id,
                len(record.locations),
                0,
                record.kind,
                record.flags,
                0,
                index,
            )
    output = bytearray(_HEADER.pack(
        MAGIC,
        VERSION,
        value.arch,
        POINTER_SIZE,
        len(value.functions),
        len(table),
        0,
    ))
    output += body
    for fields in table:
        output += _LOCATION.pack(*fields)
    return bytes(output)


def _take(payload: bytes, offset: int, shape: struct.Struct, label: str):
    end = offset + shape.size
    if end > len(payload):
        raise PreciseStackMapError(f"truncated {label}")
    return shape.unpack_from(payload, offset), end


def decode_stack_map(
    payload: bytes,
    *,
    expected_arch: int | None = None,
    final_image: bool = False,
) -> PreciseStackMap:
    if not isinstance(payload, bytes):
        raise PreciseStackMapError("stack-map payload must be immutable bytes")
    header, offset = _take(payload, 0, _HEADER, "stack-map header")
    magic, version, arch, pointer_size, function_count, table_count, _hres = header
    if magic != MAGIC:
        raise PreciseStackMapError("bad pcc stack-map magic")
    if version != VERSION:
        raise PreciseStackMapError(f"unsupported pcc stack-map version {version}")
    if pointer_size != POINTER_SIZE:
        raise PreciseStackMapError("unsupported stack-map pointer size")
    if arch not in ARCH_NAMES:
        raise PreciseStackMapError(f"unknown stack-map architecture {arch}")
    if expected_arch is not None and arch != expected_arch:
        raise PreciseStackMapError("stack-map architecture does not match target")
    if function_count > MAX_FUNCTIONS:
        raise PreciseStackMapError("too many stack-map functions")
    if table_count > MAX_LOCATIONS:
        raise PreciseStackMapError("too many stack-map locations")
    # Records name a shared table that follows every function, so collect the
    # (index, count) pairs first and materialize locations once the table has
    # been read.
    pending: list[tuple] = []
    total_records = 0
    total_locations = 0
    for _ in range(function_count):
        fields, offset = _take(payload, offset, _FUNCTION, "function record")
        function_id_value, address, code_size, frame_size, record_count, flags = fields
        total_records += record_count
        if total_records > MAX_RECORDS:
            raise PreciseStackMapError(
                "too many stack-map records: "
                + str(total_records)
                + " exceeds "
                + str(MAX_RECORDS)
            )
        raw_records: list[tuple] = []
        for _ in range(record_count):
            fields, offset = _take(payload, offset, _RECORD, "safepoint record")
            (
                record_id,
                instruction_offset,
                exceptional_offset,
                continuation_id,
                location_count,
                reserved_count,
                kind,
                record_flags,
                reserved_short,
                location_index,
            ) = fields
            if reserved_count or reserved_short:
                raise PreciseStackMapError("non-zero reserved stack-map field")
            total_locations += location_count
            if total_locations > MAX_LOCATIONS:
                raise PreciseStackMapError("too many stack-map locations")
            if location_index + location_count > table_count:
                raise PreciseStackMapError(
                    "stack-map record names locations outside its table"
                )
            raw_records.append((
                record_id,
                instruction_offset,
                exceptional_offset,
                continuation_id,
                location_index,
                location_count,
                kind,
                record_flags,
            ))
        pending.append((
            function_id_value, address, code_size, frame_size, flags, raw_records,
        ))
    table_end = offset + table_count * _LOCATION.size
    if table_end > len(payload):
        raise PreciseStackMapError("truncated stack-map location table")
    # Batch-decode the 16-byte entries in C (iter_unpack) rather than one
    # unpack_from per location.
    table = [
        StackMapLocation(*entry)
        for entry in _LOCATION.iter_unpack(payload[offset:table_end])
    ]
    offset = table_end
    functions: list[FunctionStackMap] = []
    for function_id_value, address, code_size, frame_size, flags, raw_records in pending:
        records = [
            SafepointRecord(
                safepoint_id=record_id,
                instruction_offset=instruction_offset,
                kind=kind,
                locations=tuple(table[index:index + count]),
                flags=record_flags,
                exceptional_offset=exceptional_offset,
                continuation_id=continuation_id,
            )
            for (
                record_id,
                instruction_offset,
                exceptional_offset,
                continuation_id,
                index,
                count,
                kind,
                record_flags,
            ) in raw_records
        ]
        functions.append(FunctionStackMap(
            function_id=function_id_value,
            function_address=address,
            code_size=code_size,
            frame_size=frame_size,
            records=tuple(records),
            flags=flags,
        ))
    if offset != len(payload):
        raise PreciseStackMapError("trailing bytes after pcc stack-map payload")
    result = PreciseStackMap(arch=arch, functions=tuple(functions))
    validate_stack_map(result, final_image=final_image)
    return result


def validate_stack_map_payload(
    payload: bytes,
    *,
    expected_arch: int | None = None,
    final_image: bool = False,
) -> None:
    """Validate one wire payload without materialising its decoded map.

    This is the executable-publication boundary for an already merged v2
    table.  It preserves every structural and semantic check performed by
    :func:`decode_stack_map`, but validates the shared location table through
    its record slices instead of constructing millions of frozen dataclasses
    and then walking them a second time.  A location slice's semantics depend
    only on its table range, target architecture, and owning frame size, so a
    repeated ``(index, count, frame_size)`` is checked once.
    """

    if not isinstance(payload, bytes):
        raise PreciseStackMapError("stack-map payload must be immutable bytes")
    header, cursor = _take(payload, 0, _HEADER, "stack-map header")
    magic, version, arch, pointer_size, function_count, table_count, _hres = header
    if magic != MAGIC:
        raise PreciseStackMapError("bad pcc stack-map magic")
    if version != VERSION:
        raise PreciseStackMapError(f"unsupported pcc stack-map version {version}")
    if pointer_size != POINTER_SIZE:
        raise PreciseStackMapError("unsupported stack-map pointer size")
    if arch not in ARCH_NAMES:
        raise PreciseStackMapError(f"unknown stack-map architecture {arch}")
    if expected_arch is not None and arch != expected_arch:
        raise PreciseStackMapError("stack-map architecture does not match target")
    if function_count > MAX_FUNCTIONS:
        raise PreciseStackMapError("too many stack-map functions")
    if table_count > MAX_LOCATIONS:
        raise PreciseStackMapError("too many stack-map locations")

    # Match decode_stack_map's fail-closed structural pass first.  Semantic
    # validation below may then index the shared table without turning a
    # malformed size/count into an accidental slice or unpack error.
    total_records = 0
    total_locations = 0
    for _ in range(function_count):
        function_fields, cursor = _take(
            payload, cursor, _FUNCTION, "function record"
        )
        record_count = function_fields[4]
        total_records += record_count
        if total_records > MAX_RECORDS:
            raise PreciseStackMapError(
                "too many stack-map records: "
                + str(total_records)
                + " exceeds "
                + str(MAX_RECORDS)
            )
        for _ in range(record_count):
            record_fields, cursor = _take(
                payload, cursor, _RECORD, "safepoint record"
            )
            location_count = record_fields[4]
            reserved_count = record_fields[5]
            reserved_short = record_fields[8]
            location_index = record_fields[9]
            if reserved_count or reserved_short:
                raise PreciseStackMapError("non-zero reserved stack-map field")
            total_locations += location_count
            if total_locations > MAX_LOCATIONS:
                raise PreciseStackMapError("too many stack-map locations")
            if location_index + location_count > table_count:
                raise PreciseStackMapError(
                    "stack-map record names locations outside its table"
                )
    table_start = cursor
    table_end = table_start + table_count * _LOCATION.size
    if table_end > len(payload):
        raise PreciseStackMapError("truncated stack-map location table")
    if table_end != len(payload):
        raise PreciseStackMapError("trailing bytes after pcc stack-map payload")

    cursor = _HEADER.size
    previous_function_id = -1
    seen_safepoints: set[int] = set()
    validated_location_slices: set[tuple[int, int, int]] = set()
    stack_registers = (29, 31) if arch == ARCH_AARCH64 else (6, 7)
    register_limit = _register_limit(arch)
    arch_name = ARCH_NAMES[arch]
    table_view = memoryview(payload)

    for _ in range(function_count):
        function_fields, cursor = _take(
            payload, cursor, _FUNCTION, "function record"
        )
        (
            function_id_value,
            function_address,
            code_size,
            frame_size,
            record_count,
            _function_flags,
        ) = function_fields
        if function_id_value == 0:
            raise PreciseStackMapError("function id is outside uint64")
        if function_id_value <= previous_function_id:
            raise PreciseStackMapError(
                "functions must be ordered by unique stable function id"
            )
        previous_function_id = function_id_value
        if final_image and function_address == 0:
            raise PreciseStackMapError("final-image function address is unresolved")
        if code_size == 0:
            raise PreciseStackMapError("function code size is outside uint32")
        if frame_size % 16:
            raise PreciseStackMapError(
                "function frame size must be 16-byte aligned"
            )

        previous_pc = -1
        for _ in range(record_count):
            record_fields, cursor = _take(
                payload, cursor, _RECORD, "safepoint record"
            )
            (
                record_id,
                instruction_offset,
                exceptional_offset,
                continuation_id,
                location_count,
                _reserved_count,
                kind,
                record_flags,
                _reserved_short,
                location_index,
            ) = record_fields
            if record_id == 0:
                raise PreciseStackMapError("safepoint id is outside uint64")
            if record_id in seen_safepoints:
                raise PreciseStackMapError("duplicate safepoint id")
            seen_safepoints.add(record_id)
            if kind not in SAFEPOINT_KINDS:
                raise PreciseStackMapError(f"unknown safepoint kind {kind}")
            if not previous_pc < instruction_offset < code_size:
                raise PreciseStackMapError(
                    "safepoints must have ordered in-function instruction offsets"
                )
            previous_pc = instruction_offset
            if record_flags & ~RECORD_FLAGS:
                raise PreciseStackMapError("unknown stack-map record flags")
            has_exception = exceptional_offset != NO_OFFSET
            if has_exception and exceptional_offset >= code_size:
                raise PreciseStackMapError("exceptional successor exceeds function")
            if has_exception != bool(
                record_flags & RECORD_HAS_EXCEPTION_EDGE
            ):
                raise PreciseStackMapError(
                    "exception-edge flag and offset disagree"
                )
            is_continuation = kind == SAFEPOINT_CONTINUATION
            if is_continuation != bool(continuation_id):
                raise PreciseStackMapError(
                    "continuation record needs one non-zero continuation id"
                )
            if bool(record_flags & RECORD_SUSPENDED) != is_continuation:
                raise PreciseStackMapError(
                    "suspended flag is reserved for continuation records"
                )

            slice_key = (location_index, location_count, frame_size)
            if slice_key in validated_location_slices:
                continue
            start = table_start + location_index * _LOCATION.size
            end = start + location_count * _LOCATION.size
            prior_flags: list[int] = []
            for index, location_fields in enumerate(
                _LOCATION.iter_unpack(table_view[start:end])
            ):
                (
                    location_kind,
                    location_flags,
                    size,
                    register,
                    base_index,
                    location_offset,
                    extent,
                ) = location_fields
                if location_kind not in LOCATION_KINDS:
                    raise PreciseStackMapError(
                        f"unknown location kind {location_kind}"
                    )
                if location_flags & ~LOCATION_FLAGS:
                    raise PreciseStackMapError(
                        "unknown stack-map location flags"
                    )
                if not location_flags & LOCATION_MANAGED:
                    raise PreciseStackMapError(
                        "unclassified raw pointer is not a managed "
                        "stack-map location"
                    )
                if size != POINTER_SIZE or extent != POINTER_SIZE:
                    raise PreciseStackMapError(
                        "managed locations must be one pointer wide"
                    )
                if register > register_limit:
                    raise PreciseStackMapError(
                        f"register {register} is outside {arch_name} ABI"
                    )
                if location_kind in (
                    LOCATION_STACK_INDIRECT,
                    LOCATION_STACK_DIRECT,
                ):
                    if register not in stack_registers:
                        raise PreciseStackMapError(
                            "stack location must be relative to the target "
                            "frame/stack register"
                        )
                    if location_offset >= 0 or -location_offset > frame_size:
                        raise PreciseStackMapError(
                            f"stack location {location_offset} exceeds frame "
                            f"size {frame_size}"
                        )
                    if (-location_offset) % POINTER_SIZE:
                        raise PreciseStackMapError(
                            "managed stack location is not aligned"
                        )
                elif location_offset != 0:
                    raise PreciseStackMapError(
                        "register location cannot carry frame offset"
                    )
                is_derived = bool(location_flags & LOCATION_DERIVED)
                if is_derived:
                    if not 0 <= base_index < index:
                        raise PreciseStackMapError(
                            "derived location must name an earlier base location"
                        )
                    base_flags = prior_flags[base_index]
                    if (
                        not base_flags & LOCATION_MANAGED
                        or base_flags & LOCATION_DERIVED
                    ):
                        raise PreciseStackMapError(
                            "derived location base is not a base root"
                        )
                    if not location_flags & LOCATION_RELOAD_REQUIRED:
                        raise PreciseStackMapError(
                            "derived location must be reloaded after a "
                            "relocating safepoint"
                        )
                elif base_index != NO_BASE:
                    raise PreciseStackMapError(
                        "non-derived location carries a base index"
                    )
                if (
                    location_kind == LOCATION_REGISTER
                    and not location_flags & LOCATION_RELOAD_REQUIRED
                ):
                    raise PreciseStackMapError(
                        "managed register location must reject stale "
                        "post-safepoint SSA use"
                    )
                prior_flags.append(location_flags)
            validated_location_slices.add(slice_key)

    if cursor != table_start:
        raise PreciseStackMapError("stack-map record layout changed during validation")


def function_address_offsets(payload: bytes) -> tuple[int, ...]:
    """Return byte offsets which must carry function-symbol relocations.

    Walks the payload with the same header/function/record/location layout
    without materializing a decoded map, so it does not double the cost of
    the link's per-input stack-map decode.
    """
    header, cursor = _take(payload, 0, _HEADER, "stack-map header")
    magic, version, arch, pointer_size, function_count, table_count, _hres = header
    if magic != MAGIC:
        raise PreciseStackMapError("bad pcc stack-map magic")
    if version != VERSION:
        raise PreciseStackMapError(f"unsupported pcc stack-map version {version}")
    if pointer_size != POINTER_SIZE:
        raise PreciseStackMapError("unsupported stack-map pointer size")
    if arch not in ARCH_NAMES:
        raise PreciseStackMapError(f"unknown stack-map architecture {arch}")
    offsets: list[int] = []
    total_records = 0
    total_locations = 0
    for _ in range(function_count):
        function_start = cursor
        function_fields, cursor = _take(
            payload, cursor, _FUNCTION, "function record"
        )
        _function_id, _address, _code_size, _frame_size, record_count, _flags = (
            function_fields
        )
        # _take has advanced past the header; the address field sits at
        # function_start + 8 (the function record's second 64-bit word).
        offsets.append(function_start + 8)
        total_records += record_count
        if total_records > MAX_RECORDS:
            raise PreciseStackMapError(
                "too many stack-map records: "
                + str(total_records)
                + " exceeds "
                + str(MAX_RECORDS)
            )
        for _ in range(record_count):
            record_fields, cursor = _take(
                payload, cursor, _RECORD, "safepoint record"
            )
            _rid, _io, _eo, _cid, location_count, _reserved, _kind, _rflags, _rs, _rl = (
                record_fields
            )
            total_locations += location_count
            if total_locations > MAX_LOCATIONS:
                raise PreciseStackMapError("too many stack-map locations")
            # v2: locations live in the shared table after every function.
    if cursor + table_count * _LOCATION.size != len(payload):
        raise PreciseStackMapError("stack-map size disagrees with decoded records")
    return tuple(offsets)


def _scan_stack_map_payload(payload: bytes):
    """Structurally scan one relocatable stack-map payload.

    Returns (function_count, [(function_id, fn_start, fn_end), ...],
    table_start, table_count).  The byte ranges cover each function record
    (header + safepoint records; v2 keeps locations in the shared table).  No per-location objects are materialized; the deep semantic
    validation still happens at assembly (object validator) and on the merged
    table before publication.
    """
    header, cursor = _take(payload, 0, _HEADER, "stack-map header")
    magic, version, arch, pointer_size, function_count, table_count, _hres = header
    if magic != MAGIC:
        raise PreciseStackMapError("bad pcc stack-map magic")
    if version != VERSION:
        raise PreciseStackMapError(f"unsupported pcc stack-map version {version}")
    if pointer_size != POINTER_SIZE:
        raise PreciseStackMapError("unsupported stack-map pointer size")
    if arch not in ARCH_NAMES:
        raise PreciseStackMapError(f"unknown stack-map architecture {arch}")
    functions: list[tuple[int, int, int]] = []
    total_records = 0
    total_locations = 0
    for _ in range(function_count):
        fn_start = cursor
        function_fields, cursor = _take(
            payload, cursor, _FUNCTION, "function record"
        )
        function_id, _addr, _code, _frame, record_count, _flags = function_fields
        total_records += record_count
        if total_records > MAX_RECORDS:
            raise PreciseStackMapError(
                "too many stack-map records: "
                + str(total_records)
                + " exceeds "
                + str(MAX_RECORDS)
            )
        for _ in range(record_count):
            record_fields, cursor = _take(
                payload, cursor, _RECORD, "safepoint record"
            )
            _rid, _io, _eo, _cid, location_count, _res, _k, _f, _rs, _rl = (
                record_fields
            )
            total_locations += location_count
            if total_locations > MAX_LOCATIONS:
                raise PreciseStackMapError("too many stack-map locations")
        functions.append((function_id, fn_start, cursor))
    table_start = cursor
    if table_start + table_count * _LOCATION.size != len(payload):
        raise PreciseStackMapError("stack-map size disagrees with decoded records")
    return function_count, functions, table_start, table_count


def merge_stack_map_payloads(
    payloads: tuple[bytes, ...],
) -> tuple[bytes, tuple[tuple[int, int], ...]]:
    """Merge relocatable stack-map payloads without materializing locations.

    Returns (merged_bytes, ((function_id, address_offset), ...)).  Each
    function record (header + safepoint records + locations) is copied
    verbatim, ordered by stable function id; the function-address fields are
    zeroed (the caller binds them with UNSIGNED relocations exactly like
    ``render_stack_map_assembly`` does).  A cold self-host link carries tens
    of millions of managed locations; the old decode->merge->encode path
    materialized them as Python objects several times (measured ~82% of link
    CPU in dataclass construction).  The merged table is still fully decoded
    and validated by the final link before publication.
    """
    if not payloads:
        raise PreciseStackMapError("cannot merge an empty stack-map collection")
    functions: list[tuple[int, bytes]] = []
    seen_ids: set[int] = set()
    table: list[bytes] = []
    table_index: dict[bytes, int] = {}
    table_locations = 0
    location_size = _LOCATION.size
    record_size = _RECORD.size
    function_size = _FUNCTION.size
    for payload in payloads:
        _count, scanned, table_start, table_count = _scan_stack_map_payload(payload)
        source_table = payload[
            table_start:table_start + table_count * location_size
        ]
        for function_id, fn_start, fn_end in scanned:
            if function_id in seen_ids:
                raise PreciseStackMapError(
                    f"duplicate stack-map function id {function_id}"
                )
            seen_ids.add(function_id)
            # Re-intern into the merged table.  Each input object interned
            # only against itself, so this pass is what collapses the whole
            # closure's records onto one shared set of root-set shapes; it
            # rewrites the 4-byte index in place and never materializes a
            # location object.
            blob = bytearray(payload[fn_start:fn_end])
            # Little-endian field reads/writes are open-coded rather than
            # routed through `struct`: `struct.pack_into` and friends have no
            # native lowering, so each one is a CPython fallback in the
            # self-compiled closure (this loop alone pushed this module past
            # its fallback ratchet), and the byte math is faster besides.
            # Offsets follow _FUNCTION ("<QQIIII", record count at 24) and
            # _RECORD ("<QIIIHHBBHI", location count at 20, index at 28).
            record_count = (
                blob[24]
                | (blob[25] << 8)
                | (blob[26] << 16)
                | (blob[27] << 24)
            )
            cursor = function_size
            for _ in range(record_count):
                count = blob[cursor + 20] | (blob[cursor + 21] << 8)
                index = (
                    blob[cursor + 28]
                    | (blob[cursor + 29] << 8)
                    | (blob[cursor + 30] << 16)
                    | (blob[cursor + 31] << 24)
                )
                start = index * location_size
                key = bytes(source_table[start:start + count * location_size])
                if key in table_index:
                    merged_index = table_index[key]
                else:
                    merged_index = table_locations
                    table_index[key] = merged_index
                    table.append(key)
                    table_locations += count
                blob[cursor + 28] = merged_index & 0xFF
                blob[cursor + 29] = (merged_index >> 8) & 0xFF
                blob[cursor + 30] = (merged_index >> 16) & 0xFF
                blob[cursor + 31] = (merged_index >> 24) & 0xFF
                cursor += record_size
            # The caller relocates this field. Zero it before the record is
            # frozen so the final table can be joined without growing and
            # repeatedly copying a mutable prefix.
            blob[8:16] = b"\0" * 8
            functions.append((function_id, bytes(blob)))
    functions.sort(key=lambda item: item[0])
    total_locations = table_locations
    header = _HEADER.pack(
        MAGIC,
        VERSION,
        ARCH_AARCH64,
        POINTER_SIZE,
        len(functions),
        total_locations,
        0,
    )
    chunks: list[bytes] = [header]
    merged_size = len(header)
    address_offsets: list[tuple[int, int]] = []
    for function_id, record in functions:
        address_offsets.append((function_id, merged_size + 8))
        chunks.append(record)
        merged_size += len(record)
    chunks.extend(table)
    return b"".join(chunks), tuple(address_offsets)


def merge_stack_maps(values: tuple[PreciseStackMap, ...]) -> PreciseStackMap:
    """Deterministically merge per-object maps without concatenating headers."""
    if not values:
        raise PreciseStackMapError("cannot merge an empty stack-map collection")
    arch = values[0].arch
    functions: list[FunctionStackMap] = []
    for value in values:
        validate_stack_map(value, final_image=False)
        if value.arch != arch:
            raise PreciseStackMapError("cannot merge stack maps for different targets")
        functions.extend(value.functions)
    functions.sort(key=lambda function: function.function_id)
    result = PreciseStackMap(arch=arch, functions=tuple(functions))
    validate_stack_map(result, final_image=False)
    return result


def render_stack_map_assembly(
    value: PreciseStackMap,
    function_symbols: tuple[str, ...],
    *,
    target: str,
) -> str:
    """Render one validated map as target assembler data directives.

    Function symbols are kept outside the binary codec because relocatable
    objects carry them in their native symbol table.  Their order must match
    ``value.functions``.  Every other field is emitted as literal bytes, so
    the Mach-O and ELF sections carry byte-identical ABI payloads.
    """
    payload = encode_stack_map(value, final_image=False)
    if len(function_symbols) != len(value.functions):
        raise PreciseStackMapError("function-symbol count does not match stack map")
    for symbol in function_symbols:
        if not isinstance(symbol, str) or not symbol or "\0" in symbol:
            raise PreciseStackMapError("bad stack-map function symbol")
    if target == "aarch64-darwin":
        lines = [".section __DATA,__pcc_stackmaps,regular", ".p2align 3"]
    elif target == "x86_64-linux":
        lines = ['.section .pcc_stackmaps,"a",@progbits', ".p2align 3"]
    else:
        raise PreciseStackMapError(f"unsupported stack-map target {target!r}")
    address_offsets = function_address_offsets(payload)
    symbols_by_offset = {
        offset: function_symbols[index]
        for index, offset in enumerate(address_offsets)
    }
    cursor = 0
    while cursor < len(payload):
        symbol = symbols_by_offset.get(cursor)
        if symbol is not None:
            lines.append(f"  .quad {symbol}")
            cursor += 8
            continue
        next_address = min(
            (offset for offset in address_offsets if offset > cursor),
            default=len(payload),
        )
        end = min(next_address, cursor + 16)
        values_text = ", ".join(str(byte) for byte in payload[cursor:end])
        lines.append(f"  .byte {values_text}")
        cursor = end
    return "\n".join(lines)


__all__ = [
    "ARCH_AARCH64",
    "ARCH_NAMES",
    "ARCH_X86_64",
    "FunctionStackMap",
    "LOCATION_DERIVED",
    "LOCATION_MANAGED",
    "LOCATION_OWNED",
    "LOCATION_REGISTER",
    "LOCATION_RELOAD_REQUIRED",
    "LOCATION_STACK_DIRECT",
    "LOCATION_STACK_INDIRECT",
    "LOCATION_SIZE",
    "MAGIC_I64",
    "NO_BASE",
    "NO_OFFSET",
    "POINTER_SIZE",
    "PreciseStackMap",
    "PreciseStackMapError",
    "RECORD_HAS_EXCEPTION_EDGE",
    "RECORD_SIZE",
    "RECORD_SUSPENDED",
    "SAFEPOINT_CALL",
    "SAFEPOINT_CONTINUATION",
    "SAFEPOINT_ENTRY",
    "SAFEPOINT_EXCEPTION",
    "SAFEPOINT_LOOP",
    "SafepointRecord",
    "StackMapLocation",
    "FUNCTION_SIZE",
    "HEADER_SIZE",
    "decode_stack_map",
    "encode_stack_map",
    "function_address_offsets",
    "function_id",
    "merge_stack_maps",
    "render_stack_map_assembly",
    "safepoint_id",
    "safepoint_id_from_prefix_limbs",
    "stable_id",
    "stable_id_prefix_limb",
    "validate_stack_map",
    "validate_stack_map_payload",
]
