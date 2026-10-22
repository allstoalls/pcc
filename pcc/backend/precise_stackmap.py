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

import hashlib
import os
import struct
from dataclasses import dataclass

from pcc.unsafe import abi_constant, load_i32, load_i64, null, ptr_is_null


class PreciseStackMapError(Exception):
    """Malformed or semantically incomplete pcc stack-map metadata."""


PRECISE_STACKMAP_TABLES_ENV = "PCC_PRECISE_STACKMAP_TABLES"
# C name of the linked table's start. Only a final executable link defines it
# (at the merged section's first byte); the GC runtime references it weakly.
PRECISE_STACKMAP_TABLE_SYMBOL = "pcc_precise_stackmap_table"


def precise_stackmap_target_publishes_tables(target: str | None = None) -> bool:
    """Whether a final executable for ``target`` links and publishes a table.

    The Mach-O link defines ``pcc_precise_stackmap_table`` and the program's
    main publishes it before any frame registers; ELF and PE links do not
    define it yet, so their frames keep registering.
    """
    text = str(target or "")
    if text == "" or text.startswith("unknown"):
        # Module IR without a triple is emitted for the host.
        from pcc.frontends.python.pipeline_targets import host_target_triple

        text = host_target_triple()
    return "apple" in text or "darwin" in text


def precise_stackmap_tables_enabled(target: str | None = None) -> bool:
    """Whether emitted objects carry the serialized precise stack-map table.

    The precise stack walk is the GC root source for every stack frame of
    every mutator thread wherever the executable publishes the table (see
    ``precise_stackmap_target_publishes_tables``): there tables are on by
    default, for programs and for the runtime archive alike, and compiled
    stack frames register through the ``_mapped`` frame entries, which the
    runtime skips while the walk is active. ``PCC_PRECISE_STACKMAP_TABLES``
    1/0 forces the choice (0 keeps registered slots everywhere).
    """
    raw = str(os.environ.get(PRECISE_STACKMAP_TABLES_ENV, "") or "").strip()
    if raw == "":
        return precise_stackmap_target_publishes_tables(target)
    if raw == "0":
        return False
    if raw == "1":
        return True
    raise PreciseStackMapError(PRECISE_STACKMAP_TABLES_ENV + " must be 0 or 1")


MAGIC = b"PCCSMAP1"
VERSION = 2
# A final executable may carry the compact runtime form (see
# transcode_runtime_table); objects and links always use VERSION.
RUNTIME_TABLE_VERSION = 3
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
# Resource budget, not a wire-format bound. Materialized maps charge every
# record's locations; compact consumers and explicit shared immutable views
# charge physical storage and actual unique-slice/frame validation work.
MAX_LOCATIONS = 128_000_000

_HEADER = struct.Struct("<8sHBBIII")
_FUNCTION = struct.Struct("<QQIIII")
_RECORD = struct.Struct("<QIIIHHBBHI")
_LOCATION = struct.Struct("<BBHHhiI")
_MAGIC_VERSION = struct.Struct("<8sH")


def _payload_version(payload) -> int:
    """The payload's table version, read through the buffer, or -1."""
    if len(payload) < _MAGIC_VERSION.size:
        return -1
    magic, version = _MAGIC_VERSION.unpack_from(payload, 0)
    return version if magic == MAGIC else -1


class CompactStackMapBudget:
    """Explicit host allocation estimate under an unchanged external guard.

    The model describes CPython insertion-only sets, not native sets or RSS.
    Callers provide the verified run allowance and host object-size estimates.
    No absent budget, allocation estimate or work limit removes a wire check.
    """

    def __init__(self, limit_bytes, record_visit_limit, *, set_header_bytes,
                 set_entry_bytes, integer_bytes, metadata_entry_bytes,
                 allocation_quantum):
        values = (limit_bytes, record_visit_limit, set_header_bytes,
                  set_entry_bytes, integer_bytes, metadata_entry_bytes,
                  allocation_quantum)
        for value in values:
            if type(value) is not int or value <= 0 or value > 0x7FFFFFFFFFFFFFFF:
                raise PreciseStackMapError("invalid compact stack-map budget")
        self.limit_bytes = limit_bytes
        self.record_visit_limit = record_visit_limit
        self.record_visits = 0
        self.retained_bytes = 0
        # The sizes behind the last refused check; callers add them to their
        # link error so a CI log shows how far an estimate was over.
        self.last_rejection = ""
        self.set_header_bytes = set_header_bytes
        self.set_entry_bytes = set_entry_bytes
        self.integer_bytes = integer_bytes
        self.metadata_entry_bytes = metadata_entry_bytes
        self.allocation_quantum = allocation_quantum
        self._last_records = -1
        self._last_set_bytes = 0

    def _rounded(self, size):
        quantum = self.allocation_quantum
        return ((size + quantum - 1) // quantum) * quantum

    def _set_bytes(self, records):
        if records == self._last_records:
            return self._last_set_bytes
        # CPython 3.13 insertion-only growth: the first used count satisfying
        # used*5 >= (capacity-1)*3 grows to a power of two STRICTLY above
        # used*(4 if used<=50000 else 2). Include both tables at every resize.
        capacity = 8
        peak = self.set_header_bytes + records * self.integer_bytes
        while True:
            threshold = ((capacity - 1) * 3 + 4) // 5
            if threshold > records:
                break
            target = threshold * (2 if threshold > 50000 else 4)
            following = 8
            while following <= target:
                following *= 2
            peak = max(peak, self.set_header_bytes
                       + threshold * self.integer_bytes
                       + self._rounded(capacity * self.set_entry_bytes)
                       + self._rounded(following * self.set_entry_bytes))
            capacity = following
        peak = max(peak, self.set_header_bytes + records * self.integer_bytes
                   + self._rounded(capacity * self.set_entry_bytes))
        self._last_records = records
        self._last_set_bytes = peak
        return peak

    def check_peak(self, raw_bytes, *, records=0, entries=0, label="stack-map"):
        for value in (raw_bytes, records, entries, self.retained_bytes):
            if type(value) is not int or value < 0 or value > 0x7FFFFFFFFFFFFFFF:
                raise PreciseStackMapError("invalid compact stack-map size")
        raw_bytes += self.retained_bytes
        # This check is before potentially large integer products or a set
        # capacity walk, including malformed caller-supplied counts.
        if raw_bytes > self.limit_bytes or records > self.limit_bytes // self.integer_bytes:
            self._note_rejection(label, raw_bytes, records, entries, -1)
            raise PreciseStackMapError("compact stack-map byte budget exceeded: " + label)
        if entries > self.limit_bytes // self.metadata_entry_bytes:
            self._note_rejection(label, raw_bytes, records, entries, -1)
            raise PreciseStackMapError("compact stack-map metadata budget exceeded: " + label)
        estimate = raw_bytes + entries * self.metadata_entry_bytes
        if records:
            estimate += self._set_bytes(records)
        if estimate > self.limit_bytes:
            self._note_rejection(label, raw_bytes, records, entries, estimate)
            raise PreciseStackMapError("compact stack-map byte budget exceeded: " + label)

    def _note_rejection(self, label, raw_bytes, records, entries, estimate):
        self.last_rejection = (
            "label=" + label + " estimate=" + str(estimate)
            + " raw=" + str(raw_bytes) + " records=" + str(records)
            + " entries=" + str(entries) + " retained=" + str(self.retained_bytes)
            + " limit=" + str(self.limit_bytes)
        )

    def charge_records(self, count):
        if type(count) is not int or count < 0 or count > self.record_visit_limit - self.record_visits:
            raise PreciseStackMapError("compact stack-map record work budget exceeded")
        self.record_visits += count

    def unretained_payload_bytes(self, payload):
        # Generic callers do not prove ownership of any particular buffer.
        # A host ledger may override this only for an exact retained owner.
        return len(payload)

    def check_validation_cache(self, payload, records, functions, cache, locations):
        self.check_peak(
            self.unretained_payload_bytes(payload) + locations * self.integer_bytes,
            records=records, entries=functions + len(cache) + 1,
            label="validation cache",
        )


def _native_record_reads_available() -> bool:
    try:
        return ptr_is_null(null()) != 0
    except NotImplementedError:
        return False


_NATIVE_RECORD_READS = _native_record_reads_available()

# Public numeric projection for freestanding consumers.  The struct codecs
# above remain the authority; downstream ABI tables import these derived
# values instead of copying their sizes and magic by hand.
MAGIC_I64 = int.from_bytes(MAGIC, "little")
HEADER_SIZE = _HEADER.size
FUNCTION_SIZE = _FUNCTION.size
RECORD_SIZE = _RECORD.size
LOCATION_SIZE = _LOCATION.size

# Tables that passed validate_stack_map_payload, keyed by expected architecture,
# the resource limits in force and the SHA-256 of every byte except the
# function-address words.  No check except the final image's unresolved-address
# check reads those words, and the ELF input validator lets relocations write
# nothing else in the section, so a linked table whose other bytes match a
# validated relocatable one has the same verdict apart from that check.  The
# link validated each table on input and again after relocation; the second
# pass repeated identical work.
_VALIDATED_ADDRESS_FREE_TABLES: dict[str, bool] = {}
_VALIDATED_ADDRESS_FREE_TABLE_LIMIT = 65536


def _address_free_table_key(payload: bytes, expected_arch) -> str:
    """Key for _VALIDATED_ADDRESS_FREE_TABLES, or "" for a malformed layout.

    Walks only the header and function records; the full validator reports
    every malformed layout this walk rejects.
    """
    if len(payload) < HEADER_SIZE:
        return ""
    function_count = _HEADER.unpack_from(payload, 0)[4]
    if function_count > MAX_FUNCTIONS:
        return ""
    digest = hashlib.sha256()
    cursor = HEADER_SIZE
    copied = 0
    for _ in range(function_count):
        if cursor + FUNCTION_SIZE > len(payload):
            return ""
        record_count = _FUNCTION.unpack_from(payload, cursor)[4]
        address_offset = cursor + 8
        digest.update(payload[copied:address_offset])
        copied = address_offset + 8
        cursor += FUNCTION_SIZE + record_count * RECORD_SIZE
    if cursor > len(payload):
        return ""
    digest.update(payload[copied:])
    return (
        str(expected_arch) + ":" + str(MAX_FUNCTIONS) + ":" + str(MAX_RECORDS)
        + ":" + str(MAX_LOCATIONS) + ":" + str(len(payload)) + ":" + digest.hexdigest()
    )


def _require_resolved_function_addresses(payload: bytes) -> None:
    """The final-image address check of an already validated table layout."""
    function_count = _HEADER.unpack_from(payload, 0)[4]
    cursor = HEADER_SIZE
    for _ in range(function_count):
        function_fields = _FUNCTION.unpack_from(payload, cursor)
        if function_fields[1] == 0:
            raise PreciseStackMapError("final-image function address is unresolved")
        cursor += FUNCTION_SIZE + function_fields[4] * RECORD_SIZE


def _remember_validated_table(key: str) -> None:
    if len(_VALIDATED_ADDRESS_FREE_TABLES) >= _VALIDATED_ADDRESS_FREE_TABLE_LIMIT:
        _VALIDATED_ADDRESS_FREE_TABLES.clear()
    _VALIDATED_ADDRESS_FREE_TABLES[key] = True


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


def validate_stack_map(
    value: PreciseStackMap, *, final_image: bool = False,
    shared_locations: bool = False,
) -> None:
    """Validate materialized maps or an explicitly shared immutable view.

    Materialized consumers retain their logical-allocation budget. A shared
    view charges each tuple/frame validation once, matching v2's unique-slice
    work budget. All function and record checks still run for every owner.
    """
    if value.arch not in ARCH_NAMES:
        raise PreciseStackMapError(f"unknown stack-map architecture {value.arch}")
    if len(value.functions) > MAX_FUNCTIONS:
        raise PreciseStackMapError("too many stack-map functions")
    previous_function_id = -1
    seen_safepoints: set[int] = set()
    total_records = 0
    total_locations = 0
    validated_locations: dict[tuple[int, int], tuple] = {}
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
            _check_uint(len(record.locations), 16, "location count")
            # Retain the exact immutable owner, and include the frame because
            # a valid root offset in one function may exceed another's frame.
            # Lists and tuple subclasses never take the shared fast path.
            location_key = None
            if (
                shared_locations
                and type(record.locations) is tuple
                and type(function.frame_size) is int
            ):
                location_key = (id(record.locations), function.frame_size)
                if location_key in validated_locations:
                    if validated_locations[location_key] is record.locations:
                        continue
            total_locations += len(record.locations)
            if total_locations > MAX_LOCATIONS:
                if shared_locations:
                    raise PreciseStackMapError(
                        "too much stack-map location validation work"
                    )
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
            cacheable_locations = location_key is not None
            for index, location in enumerate(record.locations):
                # The public model also accepts structurally compatible
                # values. Mutable lookalikes/subclasses must not borrow the
                # immutable dataclass fast path merely by sharing a tuple.
                if cacheable_locations and (
                    type(location) is not StackMapLocation
                    or type(location.kind) is not int
                    or type(location.flags) is not int
                    or type(location.size) is not int
                    or type(location.register) is not int
                    or type(location.base_index) is not int
                    or type(location.offset) is not int
                    or type(location.extent) is not int
                ):
                    cacheable_locations = False
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
            if cacheable_locations:
                validated_locations[location_key] = record.locations


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
    validate_stack_map(value, final_image=final_image, shared_locations=True)
    table: list[tuple] = []
    table_index: dict[str, int] = {}
    location_identities: dict[int, tuple] = {}
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
            identity = id(record.locations)
            cached = None
            if identity in location_identities:
                cached = location_identities[identity]
            if cached is not None and cached[0] is record.locations:
                index = cached[1]
            else:
                entries = []
                cacheable = type(record.locations) is tuple
                for item in record.locations:
                    fields = _location_key(item)
                    entries.append(fields)
                    if type(item) is not StackMapLocation:
                        cacheable = False
                    for field in fields:
                        if type(field) is not int:
                            cacheable = False
                key = ";".join(
                    ",".join(str(field) for field in entry) for entry in entries
                )
                if key in table_index:
                    index = table_index[key]
                else:
                    index = len(table)
                    if index + len(entries) > MAX_LOCATIONS:
                        raise PreciseStackMapError("too many stack-map locations")
                    table_index[key] = index
                    table.extend(entries)
                if cacheable:
                    location_identities[identity] = (record.locations, index)
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
    compact_budget=None,
) -> None:
    """Validate one wire payload without materialising its decoded map.

    This is the executable-publication boundary for an already merged v2
    table. It preserves the structural and semantic checks performed by
    :func:`decode_stack_map`, while budgeting the work it actually performs
    instead of that decoder's expanded allocation. It validates the table through
    its record slices instead of constructing millions of frozen dataclasses
    and then walking them a second time.  A location slice's semantics depend
    only on its table range, target architecture, and owning frame size, so a
    repeated ``(index, count, frame_size)`` is checked once.
    """

    if not isinstance(payload, bytes):
        raise PreciseStackMapError("stack-map payload must be immutable bytes")
    if _payload_version(payload) == RUNTIME_TABLE_VERSION:
        validate_runtime_table(payload, expected_arch=expected_arch, final_image=final_image)
        return
    # A budgeted caller is charged for the work below, so only unbudgeted
    # validation reuses an earlier verdict on the same address-free bytes.
    table_key = ""
    if compact_budget is None:
        table_key = _address_free_table_key(payload, expected_arch)
        if table_key and table_key in _VALIDATED_ADDRESS_FREE_TABLES:
            if final_image:
                _require_resolved_function_addresses(payload)
            return
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
    if compact_budget is not None:
        compact_budget.check_peak(
            compact_budget.unretained_payload_bytes(payload),
            entries=function_count, label="input",
        )

    # Complete the fail-closed structural pass first. Semantic
    # validation below may then index the shared table without turning a
    # malformed size/count into an accidental slice or unpack error.
    total_records = 0
    for _ in range(function_count):
        function_fields, cursor = _take(
            payload, cursor, _FUNCTION, "function record"
        )
        record_count = function_fields[4]
        total_records += record_count
        if compact_budget is None and total_records > MAX_RECORDS:
            raise PreciseStackMapError(
                "too many stack-map records: "
                + str(total_records)
                + " exceeds "
                + str(MAX_RECORDS)
            )
        if compact_budget is not None:
            compact_budget.charge_records(record_count)
        for _ in range(record_count):
            if _NATIVE_RECORD_READS:
                if cursor + RECORD_SIZE > len(payload):
                    raise PreciseStackMapError("truncated safepoint record")
                raw = abi_constant("object.bytes.data_offset") + cursor
                counts = load_i32(payload, raw + 20) & 0xFFFFFFFF
                kind_flags = load_i32(payload, raw + 24) & 0xFFFFFFFF
                location_count = counts & 0xFFFF
                reserved_count = counts >> 16
                reserved_short = kind_flags >> 16
                location_index = load_i32(payload, raw + 28) & 0xFFFFFFFF
                cursor += RECORD_SIZE
            else:
                record_fields, cursor = _take(
                    payload, cursor, _RECORD, "safepoint record"
                )
                location_count = record_fields[4]
                reserved_count = record_fields[5]
                reserved_short = record_fields[8]
                location_index = record_fields[9]
            if reserved_count or reserved_short:
                raise PreciseStackMapError("non-zero reserved stack-map field")
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

    if compact_budget is not None:
        compact_budget.check_peak(
            compact_budget.unretained_payload_bytes(payload),
            records=total_records, entries=function_count, label="validation",
        )
    cursor = _HEADER.size
    previous_function_id = -1
    seen_safepoints: set[int] = set()
    validated_location_slices: set[tuple[int, int, int]] = set()
    validation_locations = 0
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
        if compact_budget is not None:
            compact_budget.charge_records(record_count)
        for _ in range(record_count):
            if _NATIVE_RECORD_READS:
                if cursor + RECORD_SIZE > table_start:
                    raise PreciseStackMapError("truncated safepoint record")
                raw = abi_constant("object.bytes.data_offset") + cursor
                record_id = load_i64(payload, raw)
                instruction_offset = load_i32(payload, raw + 8) & 0xFFFFFFFF
                exceptional_offset = load_i32(payload, raw + 12) & 0xFFFFFFFF
                continuation_id = load_i32(payload, raw + 16) & 0xFFFFFFFF
                counts = load_i32(payload, raw + 20) & 0xFFFFFFFF
                kind_flags = load_i32(payload, raw + 24) & 0xFFFFFFFF
                location_count = counts & 0xFFFF
                kind = kind_flags & 0xFF
                record_flags = (kind_flags >> 8) & 0xFF
                location_index = load_i32(payload, raw + 28) & 0xFFFFFFFF
                cursor += RECORD_SIZE
            else:
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

            # Empty roots retain every record/ID/PC check, but have no root
            # semantics requiring one cache entry per source index/frame.
            if compact_budget is not None and location_count == 0:
                continue
            slice_key = (location_index, location_count, frame_size)
            if slice_key in validated_location_slices:
                continue
            # Overlapping slices or one range used with different frames can
            # require more work than the physical table. Bound that work before
            # iterating or allocating prior_flags; never omit semantic checks.
            validation_locations += location_count
            if validation_locations > MAX_LOCATIONS:
                raise PreciseStackMapError("too much stack-map location validation work")
            if compact_budget is not None:
                compact_budget.check_validation_cache(
                    payload, total_records, function_count,
                    validated_location_slices, location_count,
                )
            start = table_start + location_index * _LOCATION.size
            end = start + location_count * _LOCATION.size
            prior_flags: list[int] = []
            for index in range(location_count):
                if _NATIVE_RECORD_READS:
                    raw = (
                        abi_constant("object.bytes.data_offset")
                        + start + index * LOCATION_SIZE
                    )
                    kind_flags_size = load_i32(payload, raw) & 0xFFFFFFFF
                    register_base = load_i32(payload, raw + 4) & 0xFFFFFFFF
                    location_kind = kind_flags_size & 0xFF
                    location_flags = (kind_flags_size >> 8) & 0xFF
                    size = kind_flags_size >> 16
                    register = register_base & 0xFFFF
                    base_index = register_base >> 16
                    if base_index >= 0x8000:
                        base_index -= 0x10000
                    location_offset = load_i32(payload, raw + 8)
                    extent = load_i32(payload, raw + 12) & 0xFFFFFFFF
                else:
                    (
                        location_kind,
                        location_flags,
                        size,
                        register,
                        base_index,
                        location_offset,
                        extent,
                    ) = _LOCATION.unpack_from(table_view, start + index * LOCATION_SIZE)
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
            if compact_budget is not None:
                # Scratch belongs to this miss only. Do not keep a large
                # prior slice alive through hits or the next smaller miss.
                del prior_flags

    if cursor != table_start:
        raise PreciseStackMapError("stack-map record layout changed during validation")
    if table_key:
        _remember_validated_table(table_key)


def _skip_safepoint_records(
    payload: bytes, cursor: int, record_count: int,
) -> int:
    """Bound the fixed-width record range without expanding shared locations.

    Callers bound aggregate record/function counts and the physical table.
    No locations are visited or allocated here; semantic validation and merge
    separately bound unique-slice work. A logical reference count would charge
    repeatedly for roots these structural scans do not even read.
    """
    end = cursor + record_count * _RECORD.size
    if end > len(payload):
        raise PreciseStackMapError("truncated safepoint record")
    return end


def function_address_offsets(payload: bytes, *, compact_budget=None) -> tuple[int, ...]:
    """Return byte offsets which must carry function-symbol relocations.

    Walks the payload with the same header/function/record/location layout
    without materializing a decoded map, so it does not double the cost of
    the link's per-input stack-map decode.
    """
    if _payload_version(payload) == RUNTIME_TABLE_VERSION:
        return tuple(start + 8 for _fid, start, _end in _scan_runtime_table(payload)[1])
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
    if function_count > MAX_FUNCTIONS:
        raise PreciseStackMapError("too many stack-map functions")
    if table_count > MAX_LOCATIONS:
        raise PreciseStackMapError("too many stack-map locations")
    if compact_budget is not None:
        compact_budget.check_peak(len(payload), entries=function_count, label="input")
    offsets: list[int] = []
    total_records = 0
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
        if compact_budget is None and total_records > MAX_RECORDS:
            raise PreciseStackMapError(
                "too many stack-map records: "
                + str(total_records)
                + " exceeds "
                + str(MAX_RECORDS)
            )
        cursor = _skip_safepoint_records(payload, cursor, record_count)
    if cursor + table_count * _LOCATION.size != len(payload):
        raise PreciseStackMapError("stack-map size disagrees with decoded records")
    return tuple(offsets)


def _scan_runtime_table(payload) -> tuple:
    """Structural scan of a version 3 table: header, total size and the
    function entries' ranges, as ``_scan_stack_map_payload`` returns them."""
    header, cursor = _take(payload, 0, _RUNTIME_HEADER, "runtime stack-map header")
    (magic, version, arch, pointer_size, function_count, record_count, live_set_count,
     location_count, rare_count, _reserved_a, _reserved_b) = header
    if magic != MAGIC or version != RUNTIME_TABLE_VERSION or pointer_size != POINTER_SIZE:
        raise PreciseStackMapError("not a version 3 pcc runtime stack map")
    if arch not in ARCH_NAMES or function_count > MAX_FUNCTIONS:
        raise PreciseStackMapError("invalid runtime stack-map header")
    size = (
        _RUNTIME_HEADER.size
        + function_count * _RUNTIME_FUNCTION.size
        + record_count * _RUNTIME_RECORD.size
        + live_set_count * _RUNTIME_LIVE_SET.size
        + location_count * _RUNTIME_LOCATION.size
        + rare_count * _RUNTIME_RARE.size
    )
    if size != len(payload):
        raise PreciseStackMapError("runtime stack-map size disagrees with its counts")
    functions = []
    for _function in range(function_count):
        start = cursor
        fields, cursor = _take(payload, cursor, _RUNTIME_FUNCTION, "runtime function")
        functions.append((fields[0], start, cursor))
    return function_count, functions, 0, 0


def _scan_stack_map_payload(payload: bytes, *, compact_budget=None):
    """Structurally scan one relocatable stack-map payload.

    Returns (function_count, [(function_id, fn_start, fn_end), ...],
    table_start, table_count).  The byte ranges cover each function record
    (header + safepoint records; v2 keeps locations in the shared table).  No per-location objects are materialized; the deep semantic
    validation still happens at assembly (object validator) and on the merged
    table before publication.
    """
    if _payload_version(payload) == RUNTIME_TABLE_VERSION:
        return _scan_runtime_table(payload)
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
    if function_count > MAX_FUNCTIONS:
        raise PreciseStackMapError("too many stack-map functions")
    if table_count > MAX_LOCATIONS:
        raise PreciseStackMapError("too many stack-map locations")
    if compact_budget is not None:
        compact_budget.check_peak(
            compact_budget.unretained_payload_bytes(payload),
            entries=function_count, label="input",
        )
    functions: list[tuple[int, int, int]] = []
    total_records = 0
    for _ in range(function_count):
        fn_start = cursor
        function_fields, cursor = _take(
            payload, cursor, _FUNCTION, "function record"
        )
        function_id, _addr, _code, _frame, record_count, _flags = function_fields
        total_records += record_count
        if compact_budget is None and total_records > MAX_RECORDS:
            raise PreciseStackMapError(
                "too many stack-map records: "
                + str(total_records)
                + " exceeds "
                + str(MAX_RECORDS)
            )
        cursor = _skip_safepoint_records(payload, cursor, record_count)
        functions.append((function_id, fn_start, cursor))
    table_start = cursor
    if table_start + table_count * _LOCATION.size != len(payload):
        raise PreciseStackMapError("stack-map size disagrees with decoded records")
    return function_count, functions, table_start, table_count


def merge_stack_map_payloads(
    payloads: tuple[bytes, ...],
    *,
    compact_budget=None,
    _consume_payloads=False,
    _share_suffixes=False,
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

    The private CPython-only consuming mode transfers an exact list of
    detached bytes from its exclusive owner. The caller must charge any
    separately retained aliases. Completed entries become None even if a
    later input fails; the caller must clear the remaining list on failure.
    Ordinary tuple callers keep their inputs and the original behavior.
    Budgeted host PE callers may additionally share exact root-vector
    suffixes. Its per-input planning pass and scratch are explicitly charged;
    other callers retain the original whole-vector implementation.
    """
    if not payloads:
        raise PreciseStackMapError("cannot merge an empty stack-map collection")
    if _share_suffixes:
        import sys as execution_runtime

        if (execution_runtime.implementation.name != "cpython"
                or compact_budget is None or _consume_payloads
                or any(type(payload) is not bytes for payload in payloads)):
            raise PreciseStackMapError("suffix stack-map merge requires a non-consuming host budget")
    construction_copies = 2
    if _consume_payloads:
        import sys as execution_runtime

        if (execution_runtime.implementation.name != "cpython"
                or compact_budget is None or type(payloads) is not list
                or any(type(payload) is not bytes for payload in payloads)):
            raise PreciseStackMapError("consuming stack-map merge requires owned host bytes")
        # During construction, functions/table own one detached copy of each
        # accumulated chunk. The joined output does not exist yet. Consuming
        # inputs retire before the separate two-copy merge-join admission, so
        # its future output cannot overlap these input/cache owners. Keep the
        # ordinary caller's existing reservation and every scratch charge.
        construction_copies = 1
    consumed_index = 0
    scan_entries = 0
    input_bytes = 0
    if compact_budget is not None:
        input_bytes = sum(len(payload) for payload in payloads)
        compact_budget.check_peak(input_bytes, entries=len(payloads), label="merge inputs")
    function_bytes = 0
    empty_shape_index = -1
    arch = 0
    functions: list[tuple[int, bytes]] = []
    seen_ids: set[int] = set()
    table: list[bytes] = []
    table_index: dict[bytes, int] = {}
    table_locations = 0
    merge_locations = 0
    total_records = 0
    location_size = _LOCATION.size
    record_size = _RECORD.size
    function_size = _FUNCTION.size
    for payload in payloads:
        if compact_budget is None:
            _count, scanned, table_start, table_count = _scan_stack_map_payload(payload)
        elif _consume_payloads or _share_suffixes:
            # The scanner admits its current payload and complete function
            # list. Carry all other live merge owners into that admission;
            # the previous input's scan/cache have already been retired.
            previous_retained = compact_budget.retained_bytes
            compact_budget.retained_bytes += (
                input_bytes - len(payload) + function_bytes
                + table_locations * location_size
                + (4 * len(functions) + len(table_index))
                * compact_budget.metadata_entry_bytes
            )
            try:
                _count, scanned, table_start, table_count = _scan_stack_map_payload(
                    payload, compact_budget=compact_budget,
                )
            finally:
                compact_budget.retained_bytes = previous_retained
            scan_entries = len(scanned)
        else:
            _count, scanned, table_start, table_count = _scan_stack_map_payload(
                payload, compact_budget=compact_budget,
            )
        source_arch = payload[10]
        if arch and source_arch != arch:
            raise PreciseStackMapError("cannot merge stack maps for different targets")
        arch = source_arch
        # The legacy path retains its original table snapshot. The explicitly
        # budgeted path slices only unique keys from the immutable payload;
        # native memoryview slices copy too and would not remove this copy.
        source_table = None
        if compact_budget is None:
            source_table = payload[
                table_start:table_start + table_count * location_size
            ]
        # The source table is immutable for this payload. A record's (index,
        # count) therefore names the same byte range every time it occurs.
        # Real compiler PCOs repeat these ranges at many safepoints; avoid
        # rebuilding and hashing the same location bytes for each occurrence.
        source_index_cache: dict[int, int] = {}
        if _share_suffixes:
            import sys as execution_runtime
            if execution_runtime.implementation.name == "cpython":
                from .stackmap_host_suffix import plan_suffixes
                scan_entries = len(scanned)
                source_index_cache, table_locations, merge_locations = plan_suffixes(
                    payload, scanned, table_start, table_count, table,
                    table_index, table_locations, merge_locations,
                    budget=compact_budget, input_bytes=input_bytes,
                    function_bytes=function_bytes, function_count=len(functions),
                )
        for function_id, fn_start, fn_end in scanned:
            if len(functions) >= MAX_FUNCTIONS:
                raise PreciseStackMapError("too many stack-map functions")
            # The structural scan established these fixed-width boundaries.
            total_records += (fn_end - fn_start - function_size) // record_size
            if compact_budget is None and total_records > MAX_RECORDS:
                raise PreciseStackMapError("too many stack-map records")
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
            function_bytes += fn_end - fn_start
            if compact_budget is not None:
                compact_budget.check_peak(
                    input_bytes + construction_copies * (
                        _HEADER.size + function_bytes + table_locations * location_size)
                    + 2 * (fn_end - fn_start),
                    entries=4 * (len(functions) + 1) + len(source_index_cache) + len(table_index) + scan_entries,
                    label="merge function",
                )
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
            if compact_budget is not None:
                compact_budget.charge_records(record_count)
            for _ in range(record_count):
                count = blob[cursor + 20] | (blob[cursor + 21] << 8)
                index = (
                    blob[cursor + 28]
                    | (blob[cursor + 29] << 8)
                    | (blob[cursor + 30] << 16)
                    | (blob[cursor + 31] << 24)
                )
                if index + count > table_count:
                    raise PreciseStackMapError(
                        "stack-map record names locations outside its table"
                    )
                source_key = (index << 16) | count
                if compact_budget is not None and count == 0:
                    if empty_shape_index < 0:
                        empty_shape_index = table_locations
                    merged_index = empty_shape_index
                elif source_key in source_index_cache:
                    merged_index = source_index_cache[source_key]
                else:
                    # Charge each distinct source range before copying/hashing.
                    # Partially overlapping ranges must not bypass the work cap.
                    merge_locations += count
                    if merge_locations > MAX_LOCATIONS:
                        raise PreciseStackMapError("too much stack-map location merge work")
                    if compact_budget is not None:
                        compact_budget.check_peak(
                            input_bytes + construction_copies * (
                                _HEADER.size + function_bytes
                                + (table_locations + count) * location_size)
                            + 2 * len(blob) + count * location_size,
                            entries=4 * (len(functions) + 1) + len(source_index_cache) + len(table_index) + scan_entries + 2,
                            label="merge cache",
                        )
                    start = index * location_size
                    if compact_budget is None:
                        key = bytes(source_table[start:start + count * location_size])
                    else:
                        start += table_start
                        key = bytes(payload[start:start + count * location_size])
                    if key in table_index:
                        merged_index = table_index[key]
                    else:
                        merged_index = table_locations
                        table_index[key] = merged_index
                        table.append(key)
                        table_locations += count
                        if table_locations > MAX_LOCATIONS:
                            raise PreciseStackMapError("too many stack-map locations")
                    source_index_cache[source_key] = merged_index
                    if compact_budget is not None:
                        # A duplicate equal key is not owned by table_index.
                        # Retire its transient bytes before the next record.
                        key = None
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
            if compact_budget is not None:
                del blob
        if _consume_payloads:
            # This private CPython path transfers an owned list. All surviving
            # function chunks and root keys are detached bytes; the scan and
            # source-index cache hold only integers. Retire the actual slot
            # and loop reference before reducing the remaining raw-input term.
            # Keep input headers, list storage and caller retention charged.
            if execution_runtime.implementation.name == "cpython":
                retired_size = len(payload)
                scanned = None
                source_index_cache = None
                scan_entries = 0
                payloads[consumed_index] = None
                payload = None
                input_bytes -= retired_size
                consumed_index += 1
        elif _share_suffixes:
            # The next scanner/planner must not overlap this input's range
            # cache or descriptor list. Output chunks remain independently
            # owned by functions/table and are charged at the next scan.
            scanned = None
            source_index_cache = None
            scan_entries = 0
    if compact_budget is not None:
        # These per-input owners are no longer needed at the joined-output
        # high-water point; root chunks remain owned by table/table_index.
        source_index_cache = None
        scanned = None
        key = None
        seen_ids = None
        compact_budget.check_peak(
            input_bytes + 2 * (_HEADER.size + function_bytes + table_locations * location_size),
            entries=5 * len(functions) + len(table_index), label="merge join",
        )
    functions.sort(key=lambda item: item[0])
    total_locations = table_locations
    header = _HEADER.pack(
        MAGIC,
        VERSION,
        arch,
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


# Runtime table (version 3), stack-map step 1. Objects and links keep version
# 2; a final executable can carry this compact form instead. A record is a pc
# and a live-set index, live sets are deduplicated location lists, and a
# location is one int32 (frame-pointer offset, multiple of 8, with the owned
# bit in bit 0) because every producer emits the same managed, pointer-wide,
# frame-relative shape. Exception and continuation records keep their extra
# fields in a rare table. Function entries keep version 2's leading id and
# address words, so the address relocation still sits at entry start + 8.
#
#   header    <8sHBBIIIIIII magic, 3, arch, pointer size, functions, records,
#                           live sets, locations, rare entries, 2 reserved
#   function  <QQIIII       stable id, address, code size, frame size,
#                           first record, record count
#   record    <II           pc offset, live set
#   live set  <II           first location, location count
#   location  <i            offset | owned
#   rare      <IBBHII       record index, kind, flags, 0, exceptional offset,
#                           continuation id
PRECISE_STACKMAP_FORMAT_ENV = "PCC_PRECISE_STACKMAP_FORMAT"
_RUNTIME_HEADER = struct.Struct("<8sHBBIIIIIII")
_RUNTIME_FUNCTION = struct.Struct("<QQIIII")
_RUNTIME_RECORD = struct.Struct("<II")
_RUNTIME_LIVE_SET = struct.Struct("<II")
_RUNTIME_LOCATION = struct.Struct("<i")
_RUNTIME_RARE = struct.Struct("<IBBHII")
RUNTIME_HEADER_SIZE = _RUNTIME_HEADER.size
RUNTIME_FUNCTION_SIZE = _RUNTIME_FUNCTION.size
RUNTIME_RECORD_SIZE = _RUNTIME_RECORD.size
RUNTIME_LIVE_SET_SIZE = _RUNTIME_LIVE_SET.size
RUNTIME_LOCATION_SIZE = _RUNTIME_LOCATION.size
RUNTIME_RARE_SIZE = _RUNTIME_RARE.size


def precise_stackmap_runtime_format() -> int:
    """Final-executable table format: 3 (compact, the default) or 2."""
    raw = str(os.environ.get(PRECISE_STACKMAP_FORMAT_ENV, "") or "").strip()
    if raw == "" or raw == "3":
        return RUNTIME_TABLE_VERSION
    if raw == "2":
        return VERSION
    raise PreciseStackMapError(
        f"{PRECISE_STACKMAP_FORMAT_ENV} must be 2 or 3, got {raw!r}"
    )


def _runtime_location_word(fields: tuple, arch: int) -> int:
    kind, flags, size, register, base_index, offset, extent = fields
    frame_register = 29 if arch == ARCH_AARCH64 else 6
    if (
        kind != LOCATION_STACK_INDIRECT
        or flags not in (LOCATION_MANAGED, LOCATION_MANAGED | LOCATION_OWNED)
        or size != POINTER_SIZE
        or extent != POINTER_SIZE
        or register != frame_register
        or base_index != NO_BASE
        or offset >= 0
        or (-offset) % POINTER_SIZE
    ):
        raise PreciseStackMapError(
            "a version 3 runtime table carries only managed frame-relative slots"
        )
    return offset | (1 if flags & LOCATION_OWNED else 0)


def transcode_runtime_table(
    payload: bytes, address_offsets: tuple[tuple[int, int], ...],
) -> tuple[bytes, tuple[tuple[int, int], ...]]:
    """Rewrite a merged version 2 table as a version 3 runtime table.

    ``address_offsets`` pairs each function id with its address field offset
    in ``payload``; the result pairs it with the field's offset in the new
    table. Function order, ids, code and frame sizes, record pcs, root sets and
    exception/continuation fields are preserved; safepoint ids are dropped.
    """
    header, cursor = _take(payload, 0, _HEADER, "stack-map header")
    magic, version, arch, pointer_size, function_count, table_count, _reserved = header
    if magic != MAGIC or version != VERSION or pointer_size != POINTER_SIZE:
        raise PreciseStackMapError("transcoding needs a version 2 pcc stack map")
    if arch not in ARCH_NAMES:
        raise PreciseStackMapError(f"unknown stack-map architecture {arch}")
    table_base = len(payload) - table_count * _LOCATION.size
    functions: list[tuple[int, int, int, int, int, int]] = []
    records: list[tuple[int, int]] = []
    rare: list[tuple[int, int, int, int, int]] = []
    live_sets: list[tuple[int, int]] = []
    live_set_index: dict[tuple[int, int], int] = {}
    locations: list[int] = []
    for _function in range(function_count):
        fields, cursor = _take(payload, cursor, _FUNCTION, "function record")
        function_id, address, code_size, frame_size, record_count, _flags = fields
        functions.append((function_id, address, code_size, frame_size, len(records), record_count))
        for _record in range(record_count):
            record, cursor = _take(payload, cursor, _RECORD, "safepoint record")
            (_safepoint_id, pc, exceptional, continuation, count, _r1, kind, flags,
             _r2, location_index) = record
            key = (location_index, count)
            if key in live_set_index:
                live_set = live_set_index[key]
            else:
                if location_index + count > table_count:
                    raise PreciseStackMapError("safepoint locations exceed the table")
                live_set = len(live_sets)
                live_set_index[key] = live_set
                live_sets.append((len(locations), count))
                for position in range(count):
                    location = _LOCATION.unpack_from(
                        payload, table_base + (location_index + position) * _LOCATION.size,
                    )
                    locations.append(_runtime_location_word(location, arch))
            if kind not in (SAFEPOINT_ENTRY, SAFEPOINT_LOOP, SAFEPOINT_CALL) or flags or exceptional != NO_OFFSET or continuation:
                rare.append((len(records), kind, flags, exceptional, continuation))
            records.append((pc, live_set))
    if cursor != table_base:
        raise PreciseStackMapError("stack-map size disagrees with decoded records")
    out = bytearray(_RUNTIME_HEADER.pack(
        MAGIC, RUNTIME_TABLE_VERSION, arch, POINTER_SIZE, len(functions),
        len(records), len(live_sets), len(locations), len(rare), 0, 0,
    ))
    new_offsets: dict[int, int] = {}
    for function_id, address, code_size, frame_size, first, count in functions:
        new_offsets[function_id] = len(out) + 8
        out += _RUNTIME_FUNCTION.pack(function_id, address, code_size, frame_size, first, count)
    for pc, live_set in records:
        out += _RUNTIME_RECORD.pack(pc, live_set)
    for first, count in live_sets:
        out += _RUNTIME_LIVE_SET.pack(first, count)
    for word in locations:
        out += _RUNTIME_LOCATION.pack(word)
    for record_index, kind, flags, exceptional, continuation in rare:
        out += _RUNTIME_RARE.pack(record_index, kind, flags, 0, exceptional, continuation)
    remapped = tuple((function_id, new_offsets[function_id]) for function_id, _old in address_offsets)
    return bytes(out), remapped


def decode_runtime_table(payload: bytes, *, expected_arch: int | None = None) -> tuple:
    """Validate a version 3 table and return its functions.

    Each function is (id, address, code size, frame size, records); each record
    is (pc offset, ((offset, owned), ...), kind, flags, exceptional offset,
    continuation id), with kind 0 for a plain call/loop/entry record.
    """
    return _walk_runtime_table(payload, expected_arch, False, True)


def validate_runtime_table(
    payload, *, expected_arch: int | None = None, final_image: bool = False,
) -> None:
    """Check every invariant of a version 3 table without per-record objects;
    a final image must also carry every function address."""
    _walk_runtime_table(bytes(payload), expected_arch, final_image, False)


def _walk_runtime_table(payload, expected_arch, final_image, materialize):
    header, cursor = _take(payload, 0, _RUNTIME_HEADER, "runtime stack-map header")
    (magic, version, arch, pointer_size, function_count, record_count, live_set_count,
     location_count, rare_count, reserved_a, reserved_b) = header
    if magic != MAGIC or version != RUNTIME_TABLE_VERSION:
        raise PreciseStackMapError("not a version 3 pcc runtime stack map")
    if pointer_size != POINTER_SIZE or reserved_a or reserved_b:
        raise PreciseStackMapError("invalid runtime stack-map header")
    if arch not in ARCH_NAMES or (expected_arch is not None and arch != expected_arch):
        raise PreciseStackMapError(f"unexpected stack-map architecture {arch}")
    if (
        function_count > MAX_FUNCTIONS
        or record_count > MAX_LOCATIONS
        or location_count > MAX_LOCATIONS
        or live_set_count > record_count
        or rare_count > record_count
    ):
        raise PreciseStackMapError("runtime stack-map counts exceed their bounds")
    size = (
        _RUNTIME_HEADER.size
        + function_count * _RUNTIME_FUNCTION.size
        + record_count * _RUNTIME_RECORD.size
        + live_set_count * _RUNTIME_LIVE_SET.size
        + location_count * _RUNTIME_LOCATION.size
        + rare_count * _RUNTIME_RARE.size
    )
    if size != len(payload):
        raise PreciseStackMapError("runtime stack-map size disagrees with its counts")
    records_base = _RUNTIME_HEADER.size + function_count * _RUNTIME_FUNCTION.size
    live_sets_base = records_base + record_count * _RUNTIME_RECORD.size
    locations_base = live_sets_base + live_set_count * _RUNTIME_LIVE_SET.size
    rare_base = locations_base + location_count * _RUNTIME_LOCATION.size
    live_sets = []
    for index in range(live_set_count):
        first, count = _RUNTIME_LIVE_SET.unpack_from(payload, live_sets_base + index * _RUNTIME_LIVE_SET.size)
        if first + count > location_count:
            raise PreciseStackMapError("runtime live set exceeds the location table")
        words = [
            _RUNTIME_LOCATION.unpack_from(payload, locations_base + (first + position) * _RUNTIME_LOCATION.size)[0]
            for position in range(count)
        ]
        live_sets.append(tuple((word & ~1, bool(word & 1)) for word in words))
    rare = {}
    previous_rare = -1
    for index in range(rare_count):
        record_index, kind, flags, reserved, exceptional, continuation = _RUNTIME_RARE.unpack_from(
            payload, rare_base + index * _RUNTIME_RARE.size,
        )
        if record_index <= previous_rare or record_index >= record_count or reserved:
            raise PreciseStackMapError("runtime rare records are not ordered record indices")
        if kind not in SAFEPOINT_KINDS or flags & ~RECORD_FLAGS:
            raise PreciseStackMapError("runtime rare record has an unknown kind or flag")
        if bool(flags & RECORD_HAS_EXCEPTION_EDGE) != (exceptional != NO_OFFSET):
            raise PreciseStackMapError("runtime exception edge and flag disagree")
        if (kind == SAFEPOINT_CONTINUATION) != bool(continuation) or bool(flags & RECORD_SUSPENDED) != (kind == SAFEPOINT_CONTINUATION):
            raise PreciseStackMapError("runtime continuation fields disagree")
        previous_rare = record_index
        rare[record_index] = (kind, flags, exceptional, continuation)
    functions = []
    checked_frames: set[tuple[int, int]] = set()
    expected_first = 0
    previous_id = -1
    for _function in range(function_count):
        fields, cursor = _take(payload, cursor, _RUNTIME_FUNCTION, "runtime function")
        function_id, address, code_size, frame_size, first, count = fields
        if function_id <= previous_id or function_id == 0:
            raise PreciseStackMapError("runtime functions are not ordered by stable id")
        if code_size <= 0 or frame_size % 16 or first != expected_first:
            raise PreciseStackMapError("invalid runtime function entry")
        if final_image and address == 0:
            raise PreciseStackMapError("final runtime stack map has an unresolved function address")
        previous_id = function_id
        expected_first = first + count
        if expected_first > record_count:
            raise PreciseStackMapError("runtime function records exceed the table")
        decoded_records = []
        previous_pc = -1
        for index in range(first, first + count):
            pc, live_set = _RUNTIME_RECORD.unpack_from(payload, records_base + index * _RUNTIME_RECORD.size)
            if not previous_pc < pc < code_size or live_set >= live_set_count:
                raise PreciseStackMapError("invalid runtime safepoint record")
            previous_pc = pc
            if (live_set, frame_size) not in checked_frames:
                for offset, _owned in live_sets[live_set]:
                    if offset >= 0 or -offset > frame_size or (-offset) % POINTER_SIZE:
                        raise PreciseStackMapError("runtime location exceeds its frame")
                checked_frames.add((live_set, frame_size))
            kind, flags, exceptional, continuation = rare.get(index, (0, 0, NO_OFFSET, 0))
            if exceptional != NO_OFFSET and exceptional >= code_size:
                raise PreciseStackMapError("runtime exception edge is outside its function")
            if materialize:
                decoded_records.append((pc, live_sets[live_set], kind, flags, exceptional, continuation))
        if materialize:
            functions.append((function_id, address, code_size, frame_size, tuple(decoded_records)))
    if expected_first != record_count:
        raise PreciseStackMapError("runtime records are not all owned by functions")
    return tuple(functions)


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
