"""Bounded host-only validation of the complete runtime table wire format.

The generic decoder remains the oracle and instrumentation fallback. Packed
range maxima avoid expanding shared/overlapping root vectors into Python
objects. Invalid roots are rejected only at their first record reference.
"""

import struct
import sys

from . import precise_stackmap as wire


_BLOCK_LOCATIONS = 256
_SUMMARY = struct.Struct("<Q")
_INVALID = 1 << 32
_BYTEARRAY_HEADER = sys.getsizeof(bytearray())
_ORIGINAL_BUDGET_TYPE = wire.CompactStackMapBudget
def _function_state(function):
    keywords = function.__kwdefaults__
    return (function, function.__code__, function.__defaults__, keywords,
            tuple(keywords.items()) if keywords is not None else ())


def _function_unchanged(state):
    function, code, defaults, keywords, items = state
    if (function.__code__ is not code or function.__defaults__ is not defaults
            or function.__kwdefaults__ is not keywords):
        return False
    return keywords is None or (
        len(keywords) == len(items)
        and all(name in keywords and keywords[name] is value for name, value in items)
    )


def _class_state(owner):
    attributes = type.__getattribute__(owner, "__dict__")
    return (owner, type.__getattribute__(owner, "__bases__"),
            tuple(attributes.items()), tuple(
                _function_state(value) for value in attributes.values()
                if callable(value) and hasattr(value, "__code__")
            ))


_BUDGET_CLASS = _class_state(_ORIGINAL_BUDGET_TYPE)
_WIRE_FUNCTIONS = tuple(
    (name, _function_state(getattr(wire, name)))
    for name in ("validate_runtime_table", "_walk_runtime_table", "_take", "_payload_version")
)
_WIRE_CODECS = tuple(
    (name, getattr(wire, name))
    for name in ("_RUNTIME_HEADER", "_RUNTIME_FUNCTION", "_RUNTIME_RECORD",
                 "_RUNTIME_LIVE_SET", "_RUNTIME_LOCATION", "_RUNTIME_RARE")
)
_WIRE_CONSTANTS = tuple(
    (name, getattr(wire, name))
    for name in ("MAGIC", "RUNTIME_TABLE_VERSION", "POINTER_SIZE", "MAX_FUNCTIONS",
                 "MAX_LOCATIONS", "RECORD_FLAGS", "RECORD_HAS_EXCEPTION_EDGE",
                 "NO_OFFSET", "SAFEPOINT_CONTINUATION", "RECORD_SUSPENDED")
)
_ORIGINAL_ARCH_NAMES = wire.ARCH_NAMES.copy()
_ORIGINAL_SAFEPOINT_KINDS = frozenset(wire.SAFEPOINT_KINDS)
_ADAPTER = None


def _register_owned_adapter(adapter_type):
    """Capture the private output adapter after its import-cycle-safe definition."""
    global _ADAPTER
    if _ADAPTER is not None:
        # A reloaded output module receives the unchanged generic fallback;
        # it cannot extend this single captured provenance to another class.
        return
    _ADAPTER = _class_state(adapter_type)


def _unchanged_class(state):
    owner, bases, attributes, functions = state
    current = type.__getattribute__(owner, "__dict__")
    # Check the entire class BEFORE touching any instance, so newly inserted
    # properties, __getattribute__, or field descriptors cannot run here.
    if (type.__getattribute__(owner, "__bases__") is not bases
            or len(current) != len(attributes)
            or any(name not in current or current[name] is not original
                   for name, original in attributes)):
        return False
    return all(_function_unchanged(function) for function in functions)


def available(payload, compact_budget):
    # Do not invoke user-supplied budget callbacks, including descriptors, to
    # decide eligibility. In particular object() was ignored by old v3 dispatch.
    if (type(payload) is not bytes or sys.implementation.name != "cpython"
            or sys.gettrace() is not None or sys.getprofile() is not None):
        return False
    monitoring = getattr(sys, "monitoring", None)
    if monitoring is None or any(monitoring.get_tool(tool) is not None for tool in range(6)):
        return False
    for name, state in _WIRE_FUNCTIONS:
        if getattr(wire, name) is not state[0] or not _function_unchanged(state):
            return False
    if any(getattr(wire, name) is not original for name, original in _WIRE_CODECS):
        return False
    if any(getattr(wire, name) != original for name, original in _WIRE_CONSTANTS):
        return False
    if (wire.ARCH_NAMES != _ORIGINAL_ARCH_NAMES
            or frozenset(wire.SAFEPOINT_KINDS) != _ORIGINAL_SAFEPOINT_KINDS):
        return False
    budget = compact_budget
    if _ADAPTER is not None and type(budget) is _ADAPTER[0]:
        if (not _unchanged_class(_ADAPTER)
                or set(vars(budget)) != {"budget", "payload"}
                or vars(budget)["payload"] is not payload):
            return False
        budget = vars(budget)["budget"]
    if (wire.CompactStackMapBudget is not _ORIGINAL_BUDGET_TYPE
            or type(budget) is not _ORIGINAL_BUDGET_TYPE
            or not _unchanged_class(_BUDGET_CLASS)):
        return False
    values = vars(budget)
    integer_fields = ("limit_bytes", "record_visit_limit", "record_visits", "retained_bytes",
                      "set_header_bytes", "set_entry_bytes", "integer_bytes",
                      "metadata_entry_bytes", "allocation_quantum", "_last_records",
                      "_last_set_bytes")
    if set(values) != set(integer_fields) | {"last_rejection"}:
        return False
    return (all(type(values[name]) is int for name in integer_fields)
            and type(values["last_rejection"]) is str)


class _Summaries:
    def __init__(self, payload, sets_base, locations_base, sets, locations,
                 records, rare, functions, compact_budget):
        blocks = (locations + _BLOCK_LOCATIONS - 1) // _BLOCK_LOCATIONS
        leaves = 1 << max(0, (max(1, blocks) - 1).bit_length())
        budget = getattr(compact_budget, "budget", compact_budget)
        # Both fixed bytearrays coexist. No list growth, root tuples, rare
        # mapping, or frame-key set. Reserve covers scalar/codec temporaries.
        workspace = (budget._rounded(_BYTEARRAY_HEADER + 8 * sets + 1)
                     + budget._rounded(_BYTEARRAY_HEADER + 16 * leaves + 1)
                     + 65536)
        compact_budget.check_peak(
            compact_budget.unretained_payload_bytes(payload) + workspace,
            label="Mach-O runtime validation workspace",
        )
        # A pre-admitted finite operation bound, including malformed overlap:
        # scan locations once, build tree, <=510 edge words and <=2*height
        # tree nodes per set; then two rare scans and one record/function scan.
        self.remaining = (locations + leaves + sets * (2 * _BLOCK_LOCATIONS
                          + 2 * leaves.bit_length() + 1)
                          + 2 * rare + records + functions)
        self.rows = bytearray(8 * sets)
        tree = bytearray(16 * leaves)
        for index in range(locations):
            self._visit()
            value = self._word(payload, locations_base, index)
            position = leaves + index // _BLOCK_LOCATIONS
            previous = _SUMMARY.unpack_from(tree, 8 * position)[0]
            if value > previous:
                _SUMMARY.pack_into(tree, 8 * position, value)
        for position in range(leaves - 1, 0, -1):
            self._visit()
            _SUMMARY.pack_into(tree, 8 * position, max(
                _SUMMARY.unpack_from(tree, 16 * position)[0],
                _SUMMARY.unpack_from(tree, 16 * position + 8)[0],
            ))
        for index in range(sets):
            self._visit()
            first, count = wire._RUNTIME_LIVE_SET.unpack_from(
                payload, sets_base + index * wire._RUNTIME_LIVE_SET.size,
            )
            end = first + count
            value = 0
            while first < end and first % _BLOCK_LOCATIONS:
                self._visit()
                value = max(value, self._word(payload, locations_base, first))
                first += 1
            while first < end and end % _BLOCK_LOCATIONS:
                self._visit()
                end -= 1
                value = max(value, self._word(payload, locations_base, end))
            left = leaves + first // _BLOCK_LOCATIONS
            right = leaves + end // _BLOCK_LOCATIONS
            while left < right:
                if left & 1:
                    self._visit()
                    value = max(value, _SUMMARY.unpack_from(tree, 8 * left)[0])
                    left += 1
                if right & 1:
                    self._visit()
                    right -= 1
                    value = max(value, _SUMMARY.unpack_from(tree, 8 * right)[0])
                left //= 2
                right //= 2
            _SUMMARY.pack_into(self.rows, 8 * index, value)
        # Tree is retired here, before any record visits.

    def _visit(self):
        if self.remaining <= 0:
            raise wire.PreciseStackMapError("runtime validation work budget exceeded")
        self.remaining -= 1

    @staticmethod
    def _word(payload, base, index):
        word = wire._RUNTIME_LOCATION.unpack_from(
            payload, base + index * wire._RUNTIME_LOCATION.size,
        )[0]
        offset = word & ~1
        if offset >= 0 or (-offset) % wire.POINTER_SIZE:
            return _INVALID
        return -offset

    def get(self, index):
        self._visit()
        return _SUMMARY.unpack_from(self.rows, 8 * index)[0]


def validate_runtime_table(payload, *, expected_arch=None, final_image=False, compact_budget):
    header, cursor = wire._take(payload, 0, wire._RUNTIME_HEADER, "runtime stack-map header")
    (magic, version, arch, pointer_size, function_count, record_count, live_set_count,
     location_count, rare_count, reserved_a, reserved_b) = header
    if magic != wire.MAGIC or version != wire.RUNTIME_TABLE_VERSION:
        raise wire.PreciseStackMapError("not a version 3 pcc runtime stack map")
    if pointer_size != wire.POINTER_SIZE or reserved_a or reserved_b:
        raise wire.PreciseStackMapError("invalid runtime stack-map header")
    if arch not in wire.ARCH_NAMES or (expected_arch is not None and arch != expected_arch):
        raise wire.PreciseStackMapError(f"unexpected stack-map architecture {arch}")
    if (
        function_count > wire.MAX_FUNCTIONS
        or record_count > wire.MAX_LOCATIONS
        or location_count > wire.MAX_LOCATIONS
        or live_set_count > record_count
        or rare_count > record_count
    ):
        raise wire.PreciseStackMapError("runtime stack-map counts exceed their bounds")
    size = (
        wire._RUNTIME_HEADER.size
        + function_count * wire._RUNTIME_FUNCTION.size
        + record_count * wire._RUNTIME_RECORD.size
        + live_set_count * wire._RUNTIME_LIVE_SET.size
        + location_count * wire._RUNTIME_LOCATION.size
        + rare_count * wire._RUNTIME_RARE.size
    )
    if size != len(payload):
        raise wire.PreciseStackMapError("runtime stack-map size disagrees with its counts")
    records_base = wire._RUNTIME_HEADER.size + function_count * wire._RUNTIME_FUNCTION.size
    live_sets_base = records_base + record_count * wire._RUNTIME_RECORD.size
    locations_base = live_sets_base + live_set_count * wire._RUNTIME_LIVE_SET.size
    rare_base = locations_base + location_count * wire._RUNTIME_LOCATION.size
    # Preserve range-error precedence, even for unused or overlapping sets.
    for index in range(live_set_count):
        first, count = wire._RUNTIME_LIVE_SET.unpack_from(
            payload, live_sets_base + index * wire._RUNTIME_LIVE_SET.size,
        )
        if first + count > location_count:
            raise wire.PreciseStackMapError("runtime live set exceeds the location table")
    summaries = _Summaries(
        payload, live_sets_base, locations_base, live_set_count,
        location_count, record_count, rare_count, function_count, compact_budget,
    )
    previous_rare = -1
    for index in range(rare_count):
        summaries._visit()
        record_index, kind, flags, reserved, exceptional, continuation = wire._RUNTIME_RARE.unpack_from(
            payload, rare_base + index * wire._RUNTIME_RARE.size,
        )
        if record_index <= previous_rare or record_index >= record_count or reserved:
            raise wire.PreciseStackMapError("runtime rare records are not ordered record indices")
        if kind not in wire.SAFEPOINT_KINDS or flags & ~wire.RECORD_FLAGS:
            raise wire.PreciseStackMapError("runtime rare record has an unknown kind or flag")
        if bool(flags & wire.RECORD_HAS_EXCEPTION_EDGE) != (exceptional != wire.NO_OFFSET):
            raise wire.PreciseStackMapError("runtime exception edge and flag disagree")
        if (kind == wire.SAFEPOINT_CONTINUATION) != bool(continuation) or bool(flags & wire.RECORD_SUSPENDED) != (kind == wire.SAFEPOINT_CONTINUATION):
            raise wire.PreciseStackMapError("runtime continuation fields disagree")
        previous_rare = record_index
    rare_cursor = 0
    expected_first = 0
    previous_id = -1
    for _function in range(function_count):
        summaries._visit()
        fields, cursor = wire._take(payload, cursor, wire._RUNTIME_FUNCTION, "runtime function")
        function_id, address, code_size, frame_size, first, count = fields
        if function_id <= previous_id or function_id == 0:
            raise wire.PreciseStackMapError("runtime functions are not ordered by stable id")
        if code_size <= 0 or frame_size % 16 or first != expected_first:
            raise wire.PreciseStackMapError("invalid runtime function entry")
        if final_image and address == 0:
            raise wire.PreciseStackMapError("final runtime stack map has an unresolved function address")
        previous_id = function_id
        expected_first = first + count
        if expected_first > record_count:
            raise wire.PreciseStackMapError("runtime function records exceed the table")
        previous_pc = -1
        for index in range(first, first + count):
            pc, live_set = wire._RUNTIME_RECORD.unpack_from(payload, records_base + index * wire._RUNTIME_RECORD.size)
            if not previous_pc < pc < code_size or live_set >= live_set_count:
                raise wire.PreciseStackMapError("invalid runtime safepoint record")
            previous_pc = pc
            if summaries.get(live_set) > frame_size:
                raise wire.PreciseStackMapError("runtime location exceeds its frame")
            exceptional = wire.NO_OFFSET
            if rare_cursor < rare_count:
                rare_row = wire._RUNTIME_RARE.unpack_from(
                    payload, rare_base + rare_cursor * wire._RUNTIME_RARE.size,
                )
                if rare_row[0] == index:
                    summaries._visit()
                    exceptional = rare_row[4]
                    rare_cursor += 1
            if exceptional != wire.NO_OFFSET and exceptional >= code_size:
                raise wire.PreciseStackMapError("runtime exception edge is outside its function")
    if expected_first != record_count:
        raise wire.PreciseStackMapError("runtime records are not all owned by functions")
    return None
