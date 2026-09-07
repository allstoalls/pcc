from __future__ import annotations

"""Shared IR/data model layer for the self backend.

This module is target-neutral. It contains the parsed LLVM-IR-facing data model
used by target emitters, plus layout helpers that are independent of any
specific register set or calling convention.
"""

from dataclasses import dataclass, field

from . import BackendUnavailable
from .self_backend_call_flags import classify_call_flags
from .self_backend_value_arena import CompilerInt4, CompilerIntArena


def _dot_numeric_text_key_id(text: str) -> int:
    suffix = ""
    if len(text) > 2 and text.startswith("%."):
        suffix = text[2:]
    elif len(text) > 1 and text.startswith("."):
        suffix = text[1:]
    if suffix and suffix.isdigit():
        return int(suffix)
    return -1


def dot_numeric_text_key_id(text: str) -> int:
    """Public name for the dot-numeric id; callers key sets/dicts on it."""
    return _dot_numeric_text_key_id(text)


def text_key_names_equal(left: str, right: str) -> bool:
    if left == right:
        return True
    left_id = _dot_numeric_text_key_id(left)
    if left_id < 0:
        return False
    return left_id == _dot_numeric_text_key_id(right)


def text_collection_contains(values, key: str) -> bool:
    if key in values:
        return True
    key_id = _dot_numeric_text_key_id(key)
    if key_id < 0:
        return False
    for existing in values:
        if key_id == _dot_numeric_text_key_id(existing):
            return True
    return False


def _text_key_bucket_id(text: str) -> int:
    """Deterministic char-based hash; native str hashing may false-miss."""
    mask = 1099511627775
    value = 0
    index = 0
    while index < len(text):
        value = (value * 131 + ord(text[index])) & mask
        index += 1
    return value


_TEXT_KEY_INDEX_CACHE: dict[
    int, list
] = {}


def _text_key_index(mapping) -> tuple[dict[int, str], dict[int, list[str]]]:
    """(dot-id -> first key, char-hash bucket -> keys), cached per mapping.

    The linear fallback scan below this used to run on EVERY absent key;
    constant operands are absent by design, so a 72k-block module top paid
    O(lookups x mapping size) — 1e8 comparisons, minutes of pure memcmp.
    Both index tiers are keyed by ints we compute ourselves, so they stay
    correct even when the runtime's native str hashing is inconsistent
    (the reason the fallback exists at all).

    The mappings indexed here (e.g. ``value_slots``) grow interleaved with
    lookups, so the index is INCREMENTAL: a cursor records how many keys are
    already indexed and only newly appended keys are processed (Python dicts
    iterate in insertion order). A full per-miss rebuild was itself
    quadratic — 7,405 rebuilds x 64k keys on the same module top. The cache
    entry keeps a reference to the mapping, so an id() can never be recycled
    by a different dict while the entry is alive. A shrunken mapping (never
    expected; these are grow-only) triggers a fresh rebuild.
    """
    cache_key = id(mapping)
    entry = None
    if cache_key in _TEXT_KEY_INDEX_CACHE:
        candidate = _TEXT_KEY_INDEX_CACHE[cache_key]
        if candidate[0] is mapping and candidate[1] <= len(mapping):
            entry = candidate
    if entry is None:
        if len(_TEXT_KEY_INDEX_CACHE) > 4096:
            _TEXT_KEY_INDEX_CACHE.clear()
        entry = [mapping, 0, {}, {}]
        _TEXT_KEY_INDEX_CACHE[cache_key] = entry
    cursor = entry[1]
    if cursor == len(mapping):
        return entry[2], entry[3]
    by_id = entry[2]
    by_bucket = entry[3]
    # Walk the mapping directly instead of materialising `list(mapping)`:
    # this runs once per growth, and a full key list is a several-hundred-KB
    # allocation each time on a large module.  Dicts iterate in insertion
    # order, so skipping the first `cursor` keys yields exactly the new ones.
    index = 0
    for existing_key in mapping:
        if index < cursor:
            index += 1
            continue
        existing_id = _dot_numeric_text_key_id(existing_key)
        if existing_id >= 0 and existing_id not in by_id:
            by_id[existing_id] = existing_key
        bucket = _text_key_bucket_id(existing_key)
        if bucket in by_bucket:
            by_bucket[bucket].append(existing_key)
        else:
            by_bucket[bucket] = [existing_key]
        index += 1
    entry[1] = index
    return by_id, by_bucket


def _text_key_mapping_value_without_hash(mapping, existing_key):
    """Read an indexed key's current value without hashing that key again.

    Native bootstrap can corrupt/change a string's cached hash after the key
    entered a dict.  The stable text index still finds the original key by
    contents, but ``mapping[existing_key]`` would hash it again and false-miss.
    This linear scan runs only after the stable bucket has found a real text
    match; absent-key lookups keep the indexed O(1) path.
    """
    for candidate_key, candidate_value in mapping.items():
        if candidate_key is existing_key or text_key_names_equal(
            candidate_key, existing_key
        ):
            return candidate_value
    return None


def text_key_mapping_get(mapping, key: str):
    """Return a text-keyed mapping value despite a false hash miss."""
    result = mapping.get(key)
    if result is not None:
        return result
    key_id = _dot_numeric_text_key_id(key)
    if key_id >= 0:
        # Only ".N" and "%.N" spell a dot-numeric id, so spelling drift is
        # resolved by probing the one alternate spelling — no index needed.
        if key.startswith("%."):
            alternate = key[1:]
        else:
            alternate = "%" + key
        result = mapping.get(alternate)
        if result is not None:
            return result
    # Last resort: recover exact or dot-id matches that the native hash
    # probes above missed (inconsistent runtime hashing, zero-padded ids).
    by_id, by_bucket = _text_key_index(mapping)
    bucket = _text_key_bucket_id(key)
    if bucket in by_bucket:
        for existing_key in by_bucket[bucket]:
            if existing_key == key:
                return _text_key_mapping_value_without_hash(
                    mapping, existing_key
                )
    if key_id >= 0 and key_id in by_id:
        existing_key = by_id[key_id]
        return _text_key_mapping_value_without_hash(mapping, existing_key)
    return None


def _parsed_function_slot_bucket_get(buckets, key: str):
    bucket = buckets.get(_text_key_bucket_id(key))
    if bucket is None:
        return None
    for existing_name, slot in bucket:
        if text_key_names_equal(existing_name, key):
            return slot
    return None


def _parsed_function_slot_bucket_set(buckets, key: str, slot) -> None:
    bucket_id = _text_key_bucket_id(key)
    bucket = buckets.get(bucket_id)
    if bucket is None:
        bucket = []
        buckets[bucket_id] = bucket
    for index, entry in enumerate(bucket):
        if text_key_names_equal(entry[0], key):
            bucket[index] = (entry[0], slot)
            return
    bucket.append((key, slot))


def parsed_function_publish_value_slot(func, key: str, slot) -> None:
    func.value_slots[key] = slot
    _parsed_function_slot_bucket_set(func.value_slot_buckets, key, slot)


def parsed_function_publish_alloca_slot(func, key: str, slot) -> None:
    func.alloca_slots[key] = slot
    _parsed_function_slot_bucket_set(func.alloca_slot_buckets, key, slot)


def _parsed_function_value_slot_raw(func, key: str):
    direct = func.value_slots.get(key)
    if direct is not None:
        return direct
    slot = _parsed_function_slot_bucket_get(func.value_slot_buckets, key)
    if slot is not None:
        return slot
    return text_key_mapping_get(func.value_slots, key)


def _parsed_function_alloca_slot_raw(func, key: str):
    direct = func.alloca_slots.get(key)
    if direct is not None:
        return direct
    slot = _parsed_function_slot_bucket_get(func.alloca_slot_buckets, key)
    if slot is not None:
        return slot
    return text_key_mapping_get(func.alloca_slots, key)


def parsed_function_value_slot_id(func, key: str) -> int:
    if func.indexed_slot_projection:
        kernel = func.indexed_kernel
        value_id = kernel.value_id(key)
        return -1 if value_id < 0 else kernel.value_slot_id(value_id)
    raw = _parsed_function_value_slot_raw(func, key)
    if raw is None:
        return -1
    if func.indexed_slot_projection:
        return raw
    kernel = func.indexed_kernel
    value_id = kernel.value_id(key)
    return -1 if value_id < 0 else kernel.value_slot_id(value_id)


def parsed_function_value_slot_offset(func, key: str) -> int:
    if func.indexed_slot_projection:
        slot_id = parsed_function_value_slot_id(func, key)
        return -1 if slot_id < 0 else func.indexed_kernel.slot_offset(slot_id)
    raw = _parsed_function_value_slot_raw(func, key)
    if raw is None:
        return -1
    if func.indexed_slot_projection:
        return func.indexed_kernel.slot_offset(raw)
    return raw.offset


def parsed_function_value_slot_type(func, key: str):
    if func.indexed_slot_projection:
        slot_id = parsed_function_value_slot_id(func, key)
        if slot_id < 0:
            return None
        kernel = func.indexed_kernel
        return kernel.type_desc(kernel.slot_type_id(slot_id))
    raw = _parsed_function_value_slot_raw(func, key)
    if raw is None:
        return None
    if func.indexed_slot_projection:
        kernel = func.indexed_kernel
        return kernel.type_desc(kernel.slot_type_id(raw))
    return raw.type


def parsed_function_alloca_value_id(func, key: str) -> int:
    if func.indexed_slot_projection:
        value_id = func.indexed_kernel.value_id(key)
        if value_id < 0 or func.indexed_kernel.alloca_offset(value_id) < 0:
            return -1
        return value_id
    raw = _parsed_function_alloca_slot_raw(func, key)
    if raw is None:
        return -1
    if func.indexed_slot_projection:
        return raw
    return func.indexed_kernel.value_id(key)


def parsed_function_alloca_slot_offset(func, key: str) -> int:
    if func.indexed_slot_projection:
        value_id = parsed_function_alloca_value_id(func, key)
        return -1 if value_id < 0 else func.indexed_kernel.alloca_offset(value_id)
    raw = _parsed_function_alloca_slot_raw(func, key)
    if raw is None:
        return -1
    if func.indexed_slot_projection:
        return func.indexed_kernel.alloca_offset(raw)
    return raw.offset


def parsed_function_alloca_slot_type(func, key: str):
    if func.indexed_slot_projection:
        value_id = parsed_function_alloca_value_id(func, key)
        if value_id < 0:
            return None
        kernel = func.indexed_kernel
        return kernel.type_desc(kernel.alloca_type_id(value_id))
    raw = _parsed_function_alloca_slot_raw(func, key)
    if raw is None:
        return None
    if func.indexed_slot_projection:
        kernel = func.indexed_kernel
        return kernel.type_desc(kernel.alloca_type_id(raw))
    return raw.allocated_type


def parsed_function_value_slot(func, key: str):
    if func.indexed_slot_projection:
        slot_id = parsed_function_value_slot_id(func, key)
        if slot_id < 0:
            return None
        kernel = func.indexed_kernel
        kernel.legacy_slot_projections += 1
        return SlotInfo(
            kernel.slot_offset(slot_id),
            kernel.type_desc(kernel.slot_type_id(slot_id)),
        )
    raw = _parsed_function_value_slot_raw(func, key)
    return raw


def parsed_function_alloca_slot(func, key: str):
    if func.indexed_slot_projection:
        value_id = parsed_function_alloca_value_id(func, key)
        if value_id < 0:
            return None
        kernel = func.indexed_kernel
        kernel.legacy_slot_projections += 1
        return AllocaInfo(
            kernel.alloca_offset(value_id),
            kernel.type_desc(kernel.alloca_type_id(value_id)),
        )
    raw = _parsed_function_alloca_slot_raw(func, key)
    return raw


def parsed_function_has_value_slot(func, key: str) -> bool:
    if func.indexed_slot_projection:
        return parsed_function_value_slot_id(func, key) >= 0
    return _parsed_function_value_slot_raw(func, key) is not None


def parsed_function_has_alloca_slot(func, key: str) -> bool:
    if func.indexed_slot_projection:
        return parsed_function_alloca_value_id(func, key) >= 0
    return _parsed_function_alloca_slot_raw(func, key) is not None


def _align_to(value: int, alignment: int) -> int:
    if value == 0:
        return 0
    return ((value + alignment - 1) // alignment) * alignment


@dataclass(frozen=True)
class TypeDesc:
    kind: str
    width: int = 0
    pointee: "TypeDesc | None" = None
    count: int = 0
    elem: "TypeDesc | None" = None
    name: str = ""
    fields: tuple["TypeDesc", ...] = ()

    @property
    def is_void(self) -> bool:
        return self.kind == "void"

    @property
    def is_int(self) -> bool:
        return self.kind == "int"

    @property
    def is_fp(self) -> bool:
        return self.kind == "fp"

    @property
    def is_ptr(self) -> bool:
        return self.kind == "ptr"

    @property
    def is_array(self) -> bool:
        return self.kind == "array"

    @property
    def is_struct(self) -> bool:
        return self.kind == "struct"

    @property
    def bits(self) -> int:
        if self.is_ptr:
            return 64
        if self.is_int or self.is_fp:
            return self.width
        return 0

    @property
    def slot_size(self) -> int:
        if self.is_void:
            return 0
        if self.is_fp:
            return 4 if self.width <= 32 else 8
        if self.is_ptr or self.width > 32:
            return 8
        if self.is_array:
            elem: TypeDesc = self.elem
            assert elem is not None
            stride = _align_to(elem.slot_size, elem.align)
            return stride * self.count
        if self.is_struct:
            offset = 0
            max_align = 1
            for member in self.fields:
                offset = _align_to(offset, member.align)
                offset += member.slot_size
                max_align = max(max_align, member.align)
            return _align_to(offset, max_align)
        if self.width <= 8:
            return 1
        if self.width <= 16:
            return 2
        return 4

    @property
    def value_slot_size(self) -> int:
        if self.is_void:
            return 0
        if self.is_array or self.is_struct:
            return self.slot_size
        if self.is_fp:
            return 4 if self.width <= 32 else 8
        if self.is_ptr or self.width > 32:
            return 8
        return 4

    @property
    def align(self) -> int:
        if self.is_void:
            return 1
        if self.is_array:
            elem: TypeDesc = self.elem
            assert elem is not None
            return elem.align
        if self.is_struct:
            result = 1
            for member in self.fields:
                if member.align > result:
                    result = member.align
            return result
        if self.is_fp:
            return 4 if self.width <= 32 else 8
        if self.is_ptr or self.width > 32:
            return 8
        if self.width <= 8:
            return 1
        if self.width <= 16:
            return 2
        return 4

    @property
    def value_align(self) -> int:
        if self.is_void:
            return 1
        if self.is_array or self.is_struct:
            return self.align
        if self.is_fp:
            return 4 if self.width <= 32 else 8
        if self.is_ptr or self.width > 32:
            return 8
        return 4

    def ptr(self) -> "TypeDesc":
        return TypeDesc("ptr", pointee=self)

    def describe(self) -> str:
        if self.is_void:
            return "void"
        if self.is_int:
            return f"i{self.width}"
        if self.is_fp:
            return "float" if self.width <= 32 else "double"
        if self.is_array:
            elem: TypeDesc = self.elem
            assert elem is not None
            return f"[{self.count} x {elem.describe()}]"
        if self.is_struct:
            return self.name or "<anon-struct>"
        pointee: TypeDesc = self.pointee
        assert pointee is not None
        return pointee.describe() + "*"

    def field_offset(self, index: int) -> int:
        if not self.is_struct:
            raise BackendUnavailable(
                f"field_offset requested on non-struct {self.describe()}"
            )
        if index < 0 or index >= len(self.fields):
            raise BackendUnavailable(
                f"struct field index {index} out of range for {self.describe()}"
            )
        offset = 0
        for field_index, member in enumerate(self.fields):
            offset = _align_to(offset, member.align)
            if field_index == index:
                return offset
            offset += member.slot_size
        raise BackendUnavailable(
            f"struct field index {index} out of range for {self.describe()}"
        )

    def field_type(self, index: int) -> "TypeDesc":
        if not self.is_struct:
            raise BackendUnavailable(
                f"field_type requested on non-struct {self.describe()}"
            )
        return self.fields[index]


def aggregate_member_info(
    value_type: TypeDesc, indices: tuple[int, ...]
) -> tuple[TypeDesc, int]:
    current = value_type
    offset = 0
    for index in indices:
        if current.is_array:
            if index < 0 or index >= current.count:
                raise BackendUnavailable(
                    f"array index {index} out of range for {current.describe()}"
                )
            assert current.elem is not None
            stride = _align_to(current.elem.slot_size, current.elem.align)
            offset += index * stride
            current = current.elem
            continue
        if current.is_struct:
            offset += current.field_offset(index)
            current = current.field_type(index)
            continue
        raise BackendUnavailable(
            f"aggregate member requested on non-aggregate {current.describe()}"
        )
    return current, offset


@dataclass(frozen=True)
class ArgInfo:
    name: str
    type: TypeDesc


@dataclass(frozen=True)
class PhiIncoming:
    value: str
    label: str


@dataclass(frozen=True)
class PhiInstr:
    dest: str
    type: TypeDesc
    incoming: tuple[PhiIncoming, ...]


@dataclass(frozen=True)
class ParsedInstr:
    kind: str
    data: tuple
    # Keep source-level volatility after parsing.  Ordinary and relaxed-atomic
    # memory operations can otherwise collapse to the same AArch64 mnemonic,
    # which makes later target memory-combining passes unable to fail closed.
    is_volatile: bool = False
    # Integer wrap/poison flags such as ``nsw``/``nuw`` are semantically
    # relevant to target combines: replacing a separately flagged multiply
    # and add with one machine instruction can erase intermediate poison.
    # Most instructions leave this empty; binop parsing preserves every token
    # between the opcode and value type so target passes can reject them.
    arithmetic_flags: tuple[str, ...] = ()


# One stable ID table for the complete parse-boundary instruction vocabulary.
# IDs are persisted inside the indexed kernel and are the supported direct
# backend protocol.  ``ParsedInstr.kind`` remains the public/debug/legacy
# projection; hot indexed consumers must keep the integer instead of restoring
# the string merely to dispatch on it again.
PARSED_INSTRUCTION_KINDS = (
    "alloca",
    "atomicrmw",
    "binop",
    "br",
    "br_cond",
    "call",
    "cast",
    "cmpxchg",
    "extractelement",
    "extractvalue",
    "fbinop",
    "fcmp",
    "fence",
    "fneg",
    "freeze",
    "gep",
    "icmp",
    "insertelement",
    "insertvalue",
    "load",
    "load_atomic",
    "ret",
    "ret_void",
    "select",
    "shufflevector",
    "store",
    "store_atomic",
    "switch",
    "syscall6",
    "unreachable",
    "va_arg",
)
PARSED_INSTRUCTION_KIND_ALLOCA = 0
PARSED_INSTRUCTION_KIND_ATOMICRMW = 1
PARSED_INSTRUCTION_KIND_BINOP = 2
PARSED_INSTRUCTION_KIND_BR = 3
PARSED_INSTRUCTION_KIND_BR_COND = 4
PARSED_INSTRUCTION_KIND_CALL = 5
PARSED_INSTRUCTION_KIND_CAST = 6
PARSED_INSTRUCTION_KIND_CMPXCHG = 7
PARSED_INSTRUCTION_KIND_EXTRACTELEMENT = 8
PARSED_INSTRUCTION_KIND_EXTRACTVALUE = 9
PARSED_INSTRUCTION_KIND_FBINOP = 10
PARSED_INSTRUCTION_KIND_FCMP = 11
PARSED_INSTRUCTION_KIND_FENCE = 12
PARSED_INSTRUCTION_KIND_FNEG = 13
PARSED_INSTRUCTION_KIND_FREEZE = 14
PARSED_INSTRUCTION_KIND_GEP = 15
PARSED_INSTRUCTION_KIND_ICMP = 16
PARSED_INSTRUCTION_KIND_INSERTELEMENT = 17
PARSED_INSTRUCTION_KIND_INSERTVALUE = 18
PARSED_INSTRUCTION_KIND_LOAD = 19
PARSED_INSTRUCTION_KIND_LOAD_ATOMIC = 20
PARSED_INSTRUCTION_KIND_RET = 21
PARSED_INSTRUCTION_KIND_RET_VOID = 22
PARSED_INSTRUCTION_KIND_SELECT = 23
PARSED_INSTRUCTION_KIND_SHUFFLEVECTOR = 24
PARSED_INSTRUCTION_KIND_STORE = 25
PARSED_INSTRUCTION_KIND_STORE_ATOMIC = 26
PARSED_INSTRUCTION_KIND_SWITCH = 27
PARSED_INSTRUCTION_KIND_SYSCALL6 = 28
PARSED_INSTRUCTION_KIND_UNREACHABLE = 29
PARSED_INSTRUCTION_KIND_VA_ARG = 30
_PARSED_INSTRUCTION_KIND_IDS = {
    name: index for index, name in enumerate(PARSED_INSTRUCTION_KINDS)
}


class CompactParsedInstrView:
    """Read-only diagnostic/API projection of one dense arena record.

    The first four slots intentionally match ``ParsedInstr`` field order.
    Self-hosted callers were historically annotated with ``ParsedInstr`` and
    pcc can therefore lower their attribute reads to fixed field offsets.  A
    view with ``(_arena, _dense_id)`` in those slots made ``instr.kind`` read
    the arena pointer and eventually hash that unhashable object.  Keeping the
    transient view representation-compatible preserves the compact persistent
    arena without lying to compiled callers about the object projection.
    """

    __slots__ = (
        "kind",
        "data",
        "is_volatile",
        "arithmetic_flags",
        "_arena",
        "_dense_id",
    )

    def __init__(self, arena, dense_id: int):
        kind_id = arena._kind_ids[dense_id]
        if not 0 <= kind_id < len(PARSED_INSTRUCTION_KINDS):
            raise BackendUnavailable(
                f"corrupt parsed-instruction kind id {kind_id}"
            )
        self.kind = PARSED_INSTRUCTION_KINDS[kind_id]
        self.data = arena.instruction_data(dense_id)
        self.is_volatile = arena.is_volatile(dense_id)
        self.arithmetic_flags = arena.arithmetic_flags(dense_id)
        self._arena = arena
        self._dense_id = dense_id

    @property
    def dense_id(self) -> int:
        return self._dense_id

    @property
    def kind_id(self) -> int:
        return self._arena._kind_ids[self.dense_id]

    def materialize(self) -> ParsedInstr:
        """Create the old object projection only for diagnostics/debug APIs."""
        self._arena._materializations += 1
        return ParsedInstr(
            self.kind,
            self.data,
            is_volatile=self.is_volatile,
            arithmetic_flags=self.arithmetic_flags,
        )

    def __repr__(self) -> str:
        return repr(self.materialize())

    def __eq__(self, other) -> bool:
        if isinstance(other, CompactParsedInstrView):
            return (
                self.kind_id == other.kind_id
                and self.data == other.data
                and self.is_volatile == other.is_volatile
                and self.arithmetic_flags == other.arithmetic_flags
            )
        if isinstance(other, ParsedInstr):
            return self.materialize() == other
        return False

    def __hash__(self) -> int:
        return hash((
            self.kind_id,
            self.data,
            self.is_volatile,
            self.arithmetic_flags,
        ))


def _normalized_slice_bounds(index, length: int) -> tuple[int, int, int]:
    """Own ``slice.indices`` semantics inside the self-hosted backend.

    pcc's native slice object deliberately exposes ``start``/``stop``/``step``
    data but not the CPython convenience method.  Keeping normalization here
    lets arena slicing remain a normal Python projection without reintroducing
    libpython into the emitter.
    """

    raw_step = index.step
    step = 1 if raw_step is None else raw_step
    if not isinstance(step, int):
        raise TypeError("slice step must be an integer or None")
    if step == 0:
        raise ValueError("slice step cannot be zero")

    raw_start = index.start
    raw_stop = index.stop
    if raw_start is not None and not isinstance(raw_start, int):
        raise TypeError("slice start must be an integer or None")
    if raw_stop is not None and not isinstance(raw_stop, int):
        raise TypeError("slice stop must be an integer or None")

    if step > 0:
        start = 0 if raw_start is None else raw_start
        if start < 0:
            start += length
        if start < 0:
            start = 0
        elif start > length:
            start = length

        stop = length if raw_stop is None else raw_stop
        if stop < 0:
            stop += length
        if stop < 0:
            stop = 0
        elif stop > length:
            stop = length
        return start, stop, step

    start = length - 1 if raw_start is None else raw_start
    if start < 0:
        start += length
    if start < 0:
        start = -1
    elif start >= length:
        start = length - 1

    if raw_stop is None:
        stop = -1
    else:
        stop = raw_stop
        if stop < 0:
            stop += length
        if stop < 0:
            stop = -1
        elif stop >= length:
            stop = length - 1
    return start, stop, step


_EMPTY_SEQUENCE: tuple = ()


# Operand spellings repeat massively across a module: one 43 MB module held
# 682807 operand strings over only 192360 distinct values -- 655036 separate
# str objects where 192360 suffice, about 26 MB of pure duplication, and every
# one of them a separately tracked allocation.  The top entries are exactly what
# you would expect ("bitcast", "pcc_gc_unpin", "pcc_gc_release"), i.e. opcode
# and runtime-symbol names repeated once per use.  Interning at the single point
# where instructions enter the arena collapses them to one object per value.
#
# This is a private table rather than `sys.intern` so it can be dropped with the
# module and never grows across compilations of unrelated inputs.
_OPERAND_INTERN: dict = {}


def _interned_operands(data: tuple) -> tuple:
    out = []
    for item in data:
        if type(item) is str:
            # `in` + subscript, not `.get()`: dict.get mis-lowers in the
            # self-compiled frontend, and this runs inside pcc1's own backend.
            if item in _OPERAND_INTERN:
                out.append(_OPERAND_INTERN[item])
            else:
                _OPERAND_INTERN[item] = item
                out.append(item)
        else:
            out.append(item)
    return tuple(out)


def reset_operand_intern() -> None:
    """Drop the operand table between modules."""
    _OPERAND_INTERN.clear()


class IndexedCallPlane:
    """Parser-owned construction view of the kernel's final call arenas.

    The scalar layouts are already the layouts consumed by verification,
    stack maps and emit.  During parsing, text IDs temporarily occupy the
    destination/value slots whose dense value IDs do not exist yet.  Kernel
    construction resolves those fields in place and takes ownership of the
    same arenas; it never decodes or copies an intermediate call record.
    """

    records: CompilerIntArena
    args: CompilerIntArena
    texts: list[str]
    types: list[TypeDesc]
    text_identity_ids: dict[int, int]
    type_identity_ids: dict[int, tuple[int, TypeDesc]]
    diagnostic_projections: int

    __slots__ = (
        "records",
        "args",
        "texts",
        "types",
        "text_identity_ids",
        "type_identity_ids",
        "diagnostic_projections",
    )

    def __init__(self) -> None:
        self.records = CompilerIntArena()
        self.args = CompilerIntArena()
        self.texts: list[str] = []
        self.types: list[TypeDesc] = []
        self.text_identity_ids: dict[int, int] = {}
        self.type_identity_ids: dict[int, tuple[int, TypeDesc]] = {}
        self.diagnostic_projections = 0

    def intern_text(self, text: str) -> int:
        if text in _OPERAND_INTERN:
            canonical = _OPERAND_INTERN[text]
        else:
            _OPERAND_INTERN[text] = text
            canonical = text
        identity = id(canonical)
        if identity in self.text_identity_ids:
            return self.text_identity_ids[identity]
        text_id = len(self.texts)
        self.texts.append(canonical)
        self.text_identity_ids[identity] = text_id
        return text_id

    def intern_type(self, value_type: TypeDesc) -> int:
        identity = id(value_type)
        if identity in self.type_identity_ids:
            identity_entry = self.type_identity_ids[identity]
            if identity_entry[1] is value_type:
                return identity_entry[0]
        type_id = 0
        while type_id < len(self.types):
            if self.types[type_id] == value_type:
                # Do not cache a short-lived equal wrapper by id.  With no
                # entry there is nothing stale for a recycled address to hit;
                # only the canonical object receives an identity entry.
                return type_id
            type_id += 1
        type_id = len(self.types)
        self.types.append(value_type)
        self.type_identity_ids[identity] = (type_id, value_type)
        if value_type.is_ptr and value_type.pointee is not None:
            self.intern_type(value_type.pointee)
        elif value_type.is_array and value_type.elem is not None:
            self.intern_type(value_type.elem)
        elif value_type.is_struct:
            for field_type in value_type.fields:
                self.intern_type(field_type)
        return type_id

    def append_arg(
        self,
        value_type: TypeDesc,
        value: str,
        alignment: int,
    ) -> None:
        # Final layout: type ID, local value ID, cold text ID, alignment.  The
        # value is unresolved while parsing, so its text ID temporarily lives
        # in the cold slot and the kernel patches the local-ID slot in place.
        self.args.append4(
            self.intern_type(value_type),
            -1,
            self.intern_text(value),
            alignment,
        )

    def append_call(
        self,
        dest: str | None,
        ret_type: TypeDesc,
        callee: str,
        is_indirect: bool,
        arg_start: int,
        arg_count: int,
        fixed_arg_count: int,
        is_vararg: bool,
    ) -> int:
        call_id = len(self.records) // 8
        flags = classify_call_flags(callee, is_indirect, is_vararg)
        self.records.append4(
            self.intern_type(ret_type),
            self.intern_text(callee),
            flags,
            arg_start,
        )
        # Final layout: arg count, fixed count, destination value ID, liveness
        # state ID.  Before the definition pass, the destination field holds
        # its text ID and is resolved in place.
        self.records.append4(
            arg_count,
            fixed_arg_count,
            -1 if dest is None else self.intern_text(dest),
            0,
        )
        return call_id

    def append_parsed_call(
        self,
        arena: CompactParsedInstrArena,
        dest: str | None,
        ret_type: TypeDesc,
        callee: str,
        is_indirect: bool,
        arg_start: int,
        arg_count: int,
        fixed_arg_count: int,
        is_vararg: bool,
    ) -> None:
        call_id = self.append_call(
            dest,
            ret_type,
            callee,
            is_indirect,
            arg_start,
            arg_count,
            fixed_arg_count,
            is_vararg,
        )
        arena.append_indexed_call(call_id)

    def header(self, call_id: int) -> CompilerInt4:
        return self.records.get4_unchecked(call_id * 2)

    def span(self, call_id: int) -> CompilerInt4:
        return self.records.get4_unchecked(call_id * 2 + 1)

    def arg(self, arg_id: int) -> CompilerInt4:
        return self.args.get4_unchecked(arg_id)

    def diagnostic_call_data(self, call_id: int) -> tuple:
        self.diagnostic_projections += 1
        header: CompilerInt4 = self.header(call_id)
        span: CompilerInt4 = self.span(call_id)
        args = []
        alignments = []
        arg_index = 0
        while arg_index < span.first:
            raw: CompilerInt4 = self.arg(header.fourth + arg_index)
            args.append((self.types[raw.first], self.texts[raw.third]))
            alignments.append(raw.fourth)
            arg_index += 1
        return (
            None if span.third < 0 else self.texts[span.third],
            self.types[header.first],
            self.texts[header.second],
            bool(header.third & 1),
            tuple(args),
            span.second,
            bool(header.third & 2),
            tuple(alignments),
        )

    def release_construction_indexes(self) -> None:
        self.text_identity_ids.clear()
        self.type_identity_ids.clear()


class CompactParsedInstrArena:
    """Block-local structure-of-arrays store keyed by dense instruction ID."""

    __slots__ = (
        "_kind_ids",
        "_data",
        "_volatile",
        "_arithmetic_flags",
        "_arithmetic_flag_values",
        "_call_projector",
        "_indexed_call_plane",
        "_payload_start",
        "_materializations",
    )

    def __init__(self, values=()):
        self._kind_ids: bytearray = bytearray()
        self._data: list[tuple] = []
        self._volatile = bytearray()
        self._arithmetic_flags = bytearray()
        self._arithmetic_flag_values = None
        self._call_projector = None
        self._indexed_call_plane = None
        self._payload_start = -1
        self._materializations = 0
        for value in values:
            self.append(value)

    def append(self, value) -> None:
        if self._payload_start >= 0:
            raise BackendUnavailable(
                "cannot append to a frozen parsed-instruction arena"
            )
        if isinstance(value, CompactParsedInstrView):
            value = value.materialize()
        if not isinstance(value, ParsedInstr):
            raise BackendUnavailable(
                "parsed-instruction arena accepts ParsedInstr records only"
            )
        kind_id = _PARSED_INSTRUCTION_KIND_IDS.get(value.kind)
        if kind_id is None:
            raise BackendUnavailable(
                f"unknown parsed-instruction kind {value.kind!r}"
            )
        self._kind_ids.append(kind_id)
        self._data.append(_interned_operands(value.data))
        self._volatile.append(1 if value.is_volatile else 0)
        self._arithmetic_flags.append(1 if value.arithmetic_flags else 0)
        if value.arithmetic_flags:
            if self._arithmetic_flag_values is None:
                self._arithmetic_flag_values = {}
            self._arithmetic_flag_values[len(self._kind_ids) - 1] = (
                value.arithmetic_flags
            )

    def has_arithmetic_flags(self, index: int) -> bool:
        if self._payload_start >= 0 and not self._arithmetic_flags:
            metadata = self._call_projector.instruction_metadata_by_id(
                self._payload_start + index
            )
            return bool(metadata.fourth)
        return bool(self._arithmetic_flags[index])

    def is_volatile(self, index: int) -> bool:
        if self._payload_start >= 0 and not self._volatile:
            metadata = self._call_projector.instruction_metadata_by_id(
                self._payload_start + index
            )
            return bool(metadata.third)
        return bool(self._volatile[index])

    def instruction_data(self, index: int):
        if self._payload_start >= 0:
            payload_id = self._call_projector.instruction_payload_id_by_id(
                self._payload_start + index
            )
            if payload_id < 0 and self._data is _EMPTY_SEQUENCE:
                return self._call_projector.diagnostic_cold_instruction_data(
                    -payload_id - 1
                )
            raw = payload_id
        else:
            raw = self._data[index]
        kind_id = self._kind_ids[index]
        if kind_id == PARSED_INSTRUCTION_KIND_CALL:
            if not isinstance(raw, int) or self._call_projector is None:
                return raw
            return self._call_projector.diagnostic_call_data(raw)
        if kind_id == PARSED_INSTRUCTION_KIND_ALLOCA:
            if not isinstance(raw, int) or self._call_projector is None:
                return raw
            return self._call_projector.diagnostic_alloca_data(raw)
        if kind_id in (
            PARSED_INSTRUCTION_KIND_LOAD,
            PARSED_INSTRUCTION_KIND_STORE,
            PARSED_INSTRUCTION_KIND_CAST,
            PARSED_INSTRUCTION_KIND_ICMP,
            PARSED_INSTRUCTION_KIND_BINOP,
            PARSED_INSTRUCTION_KIND_SELECT,
        ):
            if not isinstance(raw, int) or self._call_projector is None:
                return raw
            return self._call_projector.diagnostic_fixed_instruction_data(
                kind_id,
                raw,
            )
        if kind_id == PARSED_INSTRUCTION_KIND_GEP:
            if not isinstance(raw, int) or self._call_projector is None:
                return raw
            return self._call_projector.diagnostic_gep_data(raw)
        return raw

    def attach_indexed_call_plane(self, plane: IndexedCallPlane) -> None:
        if self._kind_ids:
            raise BackendUnavailable(
                "indexed call plane must attach before instructions"
            )
        self._indexed_call_plane = plane
        self._call_projector = plane

    def attach_indexed_function_seed(self, seed, payload_start: int) -> None:
        """Attach parser-owned final metadata before publishing instructions."""
        if self._kind_ids:
            raise BackendUnavailable(
                "indexed function seed must attach before instructions"
            )
        self._indexed_call_plane = seed
        self._call_projector = seed
        self._payload_start = payload_start
        self._data = _EMPTY_SEQUENCE

    def append_indexed_call(self, call_id: int) -> None:
        if self._payload_start >= 0:
            raise BackendUnavailable(
                "cannot append to a frozen parsed-instruction arena"
            )
        self._kind_ids.append(_PARSED_INSTRUCTION_KIND_IDS["call"])
        self._data.append(call_id)
        self._volatile.append(0)
        self._arithmetic_flags.append(0)

    def freeze_payload_ids(self, projector, payload_start: int) -> None:
        self._call_projector = projector
        self._payload_start = payload_start
        self._data = _EMPTY_SEQUENCE

    def arithmetic_flags(self, index: int) -> tuple[str, ...]:
        if not self.has_arithmetic_flags(index):
            return ()
        if self._payload_start >= 0 and not self._arithmetic_flags:
            return self._call_projector.instruction_arithmetic_flag_values[
                self._payload_start + index
            ]
        values = self._arithmetic_flag_values
        if values is None or index not in values:
            raise BackendUnavailable("instruction arithmetic flags are corrupt")
        return values[index]

    def __len__(self) -> int:
        return len(self._kind_ids)

    def __bool__(self) -> bool:
        return bool(self._kind_ids)

    def __getitem__(self, index):
        if isinstance(index, slice):
            start, stop, step = _normalized_slice_bounds(index, len(self))
            return [CompactParsedInstrView(self, i) for i in range(start, stop, step)]
        if index < 0:
            index += len(self)
        if not 0 <= index < len(self):
            raise IndexError(index)
        return CompactParsedInstrView(self, index)

    def __iter__(self):
        for index in range(len(self)):
            yield CompactParsedInstrView(self, index)

    def __eq__(self, other) -> bool:
        if isinstance(other, CompactParsedInstrArena):
            if self._payload_start >= 0 or other._payload_start >= 0:
                return list(self) == list(other)
            return (
                self._kind_ids == other._kind_ids
                and self._data == other._data
                and self._volatile == other._volatile
                and self._arithmetic_flags == other._arithmetic_flags
                and self._arithmetic_flag_values
                == other._arithmetic_flag_values
            )
        if isinstance(other, (list, tuple)):
            return list(self) == list(other)
        return False

    def materialize(self) -> list[ParsedInstr]:
        return [value.materialize() for value in self]

    def profile_counters(self) -> dict[str, int]:
        return {
            "records": len(self),
            "kind_id_bytes": len(self._kind_ids),
            "volatile_bytes": len(self._volatile),
            "arithmetic_flag_bytes": len(self._arithmetic_flags),
            "diagnostic_materializations": self._materializations,
        }


# `slots=True`: one 43 MB module holds 59402 ParsedBlock objects, and a
# per-instance `__dict__` costs about 6 MB of pure bookkeeping on top of the
# fields themselves.  Every attribute is declared here, so the dict adds
# nothing but overhead.
@dataclass(slots=True)
class ParsedBlock:
    name: str
    raw_lines: list[str] = field(default_factory=list)
    phis: tuple[PhiInstr, ...] = ()
    instructions: CompactParsedInstrArena = field(
        default_factory=CompactParsedInstrArena,
    )
    terminator: ParsedInstr | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.instructions, CompactParsedInstrArena):
            self.instructions = CompactParsedInstrArena(self.instructions)


@dataclass(frozen=True)
class SlotInfo:
    offset: int
    type: TypeDesc


@dataclass(frozen=True)
class AllocaInfo:
    offset: int
    allocated_type: TypeDesc


@dataclass(frozen=True)
class GlobalDef:
    name: str
    type: TypeDesc
    initializer: str
    is_constant: bool
    is_internal: bool
    # Empty for ordinary globals.  ``default`` preserves a bare LLVM
    # ``thread_local`` spelling so each target can choose (or reject) the
    # finite TLS model it actually implements instead of silently treating
    # the symbol as process-global storage.
    tls_model: str = ""
    alignment: int = 0
    ir_prefix: str = ""
    trailing_attributes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ParsedModule:
    triple: str
    globals_: tuple[GlobalDef, ...]
    functions: tuple["ParsedFunction", ...]


@dataclass
class ParsedFunction:
    name: str
    ret_type: TypeDesc
    args: list[ArgInfo]
    is_global: bool
    is_vararg: bool
    blocks: list[ParsedBlock]
    value_types: dict[str, TypeDesc] = field(default_factory=dict)
    value_slots: dict[str, object] = field(default_factory=dict)
    value_slot_buckets: dict[int, list[tuple[str, object]]] = field(
        default_factory=dict
    )
    alloca_slots: dict[str, object] = field(default_factory=dict)
    alloca_slot_buckets: dict[int, list[tuple[str, object]]] = field(
        default_factory=dict
    )
    block_map: dict[str, ParsedBlock] = field(default_factory=dict)
    # Membership must be equality-based during native bootstrap.  A set lookup
    # can falsely miss an equal text key produced through another runtime path.
    used_values: list[str] = field(default_factory=list)
    # Target-neutral stack preparation and AArch64 register allocation consume
    # the same pure block-local last-use analysis.  Stackprep publishes its
    # result so the immediately following target pass does not rescan every
    # instruction; direct target-pass callers retain a compute-on-miss path.
    block_local_last_uses: dict[str, dict[str, int]] | None = None
    # Optional target-owned scalar register assignments.  Stack slots remain
    # authoritative storage unless a target emitter explicitly recognizes an
    # entry here; this keeps unsupported targets and instruction shapes on the
    # existing spill path.
    value_registers: dict[str, int] = field(default_factory=dict)
    # Target-owned, pre-emission combine plans.  The shared IR model keeps the
    # container deliberately opaque; only the AArch64 target pass, allocator,
    # and emitter interpret its entries, while other targets leave it empty.
    aarch64_madd_fusions: list = field(default_factory=list)
    # Target-owned block layout for the finite AArch64 cold-error-path pass.
    # An empty list means source order.  The companion edge triples mark only
    # canonical post-call py_err_occurred checks whose no-error successor was
    # proven to be the next emitted block; assembly peepholes must not infer
    # that policy from arbitrary conditional-branch text.
    aarch64_block_layout: list[ParsedBlock] = field(default_factory=list)
    aarch64_cold_fallthrough_edges: list[tuple[str, str, str]] = field(
        default_factory=list
    )
    hidden_sret_slot: SlotInfo | None = None
    frame_size: int = 0
    # Built lazily by the first indexed analysis consumer.  Kept at the end to
    # preserve the established positional construction interface, and typed as
    # object to avoid a module cycle: self_backend_kernel imports this IR
    # vocabulary plus the analysis schema used to construct the kernel.
    indexed_kernel: object | None = None
    # Parser-owned construction state for the final indexed kernel.  The
    # kernel adopts its arenas by identity and clears this field; unsupported
    # diagnostic shapes remain in the seed's explicit cold side table.
    indexed_seed: object | None = None
    indexed_slot_projection: bool = False
    # Target-owned finite sibling-call plan; never serialized as an IR promise.
    aarch64_tail_call_ids: list[int] = field(default_factory=list)


def parsed_module_instruction_arena_profile(module: ParsedModule) -> dict[str, int]:
    """Aggregate bounded representation counters without materializing rows."""
    counters = {
        "blocks": 0,
        "records": 0,
        "kind_id_bytes": 0,
        "volatile_bytes": 0,
        "arithmetic_flag_bytes": 0,
        "diagnostic_materializations": 0,
    }
    for function in module.functions:
        if not function.blocks and function.indexed_kernel is not None:
            kernel = function.indexed_kernel
            counters["blocks"] += len(kernel.block_names)
            counters["records"] += len(kernel.instruction_metadata) // 4
            counters["kind_id_bytes"] += len(kernel.instruction_kind_ids)
            continue
        if not function.blocks and function.indexed_seed is not None:
            seed = function.indexed_seed
            counters["blocks"] += len(seed.block_names)
            counters["records"] += len(seed.instruction_metadata) // 4
            counters["kind_id_bytes"] += len(seed.instruction_kind_ids)
            continue
        for block in function.blocks:
            counters["blocks"] += 1
            block_counters = block.instructions.profile_counters()
            for name in (
                "records",
                "kind_id_bytes",
                "volatile_bytes",
                "arithmetic_flag_bytes",
                "diagnostic_materializations",
            ):
                counters[name] += block_counters[name]
    return counters


I1 = TypeDesc("int", 1)


__all__ = [
    "aggregate_member_info",
    "ArgInfo",
    "AllocaInfo",
    "GlobalDef",
    "I1",
    "ParsedBlock",
    "CompactParsedInstrArena",
    "CompactParsedInstrView",
    "PARSED_INSTRUCTION_KIND_ALLOCA",
    "PARSED_INSTRUCTION_KIND_ATOMICRMW",
    "PARSED_INSTRUCTION_KIND_BINOP",
    "PARSED_INSTRUCTION_KIND_BR",
    "PARSED_INSTRUCTION_KIND_BR_COND",
    "PARSED_INSTRUCTION_KIND_CALL",
    "PARSED_INSTRUCTION_KIND_CAST",
    "PARSED_INSTRUCTION_KIND_CMPXCHG",
    "PARSED_INSTRUCTION_KIND_EXTRACTELEMENT",
    "PARSED_INSTRUCTION_KIND_EXTRACTVALUE",
    "PARSED_INSTRUCTION_KIND_FBINOP",
    "PARSED_INSTRUCTION_KIND_FCMP",
    "PARSED_INSTRUCTION_KIND_FENCE",
    "PARSED_INSTRUCTION_KIND_FNEG",
    "PARSED_INSTRUCTION_KIND_FREEZE",
    "PARSED_INSTRUCTION_KIND_GEP",
    "PARSED_INSTRUCTION_KIND_ICMP",
    "PARSED_INSTRUCTION_KIND_INSERTELEMENT",
    "PARSED_INSTRUCTION_KIND_INSERTVALUE",
    "PARSED_INSTRUCTION_KIND_LOAD",
    "PARSED_INSTRUCTION_KIND_LOAD_ATOMIC",
    "PARSED_INSTRUCTION_KIND_RET",
    "PARSED_INSTRUCTION_KIND_RET_VOID",
    "PARSED_INSTRUCTION_KIND_SELECT",
    "PARSED_INSTRUCTION_KIND_SHUFFLEVECTOR",
    "PARSED_INSTRUCTION_KIND_STORE",
    "PARSED_INSTRUCTION_KIND_STORE_ATOMIC",
    "PARSED_INSTRUCTION_KIND_SWITCH",
    "PARSED_INSTRUCTION_KIND_SYSCALL6",
    "PARSED_INSTRUCTION_KIND_UNREACHABLE",
    "PARSED_INSTRUCTION_KIND_VA_ARG",
    "PARSED_INSTRUCTION_KINDS",
    "ParsedFunction",
    "ParsedInstr",
    "PhiIncoming",
    "PhiInstr",
    "SlotInfo",
    "TypeDesc",
    "_align_to",
    "parsed_function_alloca_slot_offset",
    "parsed_function_alloca_slot_type",
    "parsed_function_alloca_value_id",
    "parsed_function_alloca_slot",
    "parsed_function_has_alloca_slot",
    "parsed_function_has_value_slot",
    "parsed_function_publish_alloca_slot",
    "parsed_function_publish_value_slot",
    "parsed_function_value_slot_id",
    "parsed_function_value_slot_offset",
    "parsed_function_value_slot_type",
    "parsed_function_value_slot",
    "parsed_module_instruction_arena_profile",
    "text_collection_contains",
    "dot_numeric_text_key_id",
    "text_key_mapping_get",
    "text_key_names_equal",
]
