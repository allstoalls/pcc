from __future__ import annotations

"""Target-final precise stack-map planning for the owned self backends.

The frontend's registered-slot protocol remains the source of truth.  This
module does not infer managed pointers from LLVM ``ptr`` values: it follows
only explicit ``pcc_gc_frame_enter{,_lifo}`` calls, resolves their frame-map
global and slot address, and rejects an unresolved stack-derived address.

Plans are attached to labels after target allocation.  AArch64 resolves those
labels to final numeric offsets after peepholes; x86 emits assembler label
differences so the variable-length encoder owns the final PCs.
"""

from dataclasses import dataclass

from pcc.extern import c_int64, c_ptr, extern, c_obj
from pcc.unsafe import int_to_ptr as _record_int_to_ptr
from pcc.unsafe import load_i64 as _record_load_i64
from pcc.unsafe import store_i64 as _record_store_i64
from pcc.unsafe import (
    free,
    malloc,
    memset,
    ptr_is_null,
    store_i8,
    store_i32,
    store_i64,
)

from . import BackendUnavailable
from . import macho_spec as _macho_spec
from .macho_obj import (
    PCC_STACKMAP_SECTION_FLAGS,
    Relocation,
    Section,
)
from .precise_stackmap import (
    ARCH_AARCH64,
    ARCH_X86_64,
    FunctionStackMap,
    LOCATION_MANAGED,
    LOCATION_OWNED,
    LOCATION_STACK_INDIRECT,
    MAGIC,
    NO_BASE,
    NO_OFFSET,
    POINTER_SIZE,
    PreciseStackMap,
    RECORD_HAS_EXCEPTION_EDGE,
    RECORD_SUSPENDED,
    SAFEPOINT_CALL,
    SAFEPOINT_CONTINUATION,
    SAFEPOINT_ENTRY,
    SAFEPOINT_EXCEPTION,
    SAFEPOINT_KINDS,
    SAFEPOINT_LOOP,
    SafepointRecord,
    StackMapLocation,
    VERSION,
    function_id,
    render_stack_map_assembly,
    safepoint_id_from_prefix_limbs,
    stable_id_prefix_limb,
    scoped_stable_id,
    validate_stack_map,
    _FUNCTION as _STACK_MAP_FUNCTION_CODEC,
    _HEADER as _STACK_MAP_HEADER_CODEC,
    _LOCATION as _STACK_MAP_LOCATION_CODEC,
    _RECORD as _STACK_MAP_RECORD_CODEC,
)
from .self_backend_aarch64_fragments import AArch64EmissionFragments
from .self_backend_aarch64_darwin_regs import append_add_offset, emit_add_offset
from .self_backend_aarch64_darwin_slots import (
    append_load_slot_to_reg_parts,
    append_store_reg_to_slot_parts,
    load_slot_to_reg,
    load_slot_to_reg_parts,
    store_reg_to_slot,
    store_reg_to_slot_parts,
)
from .self_backend_call_flags import (
    CALL_FLAG_CONTINUATION,
    CALL_FLAG_EXCEPTION_POLL,
    CALL_FLAG_FRAME_ENTER,
    CALL_FLAG_FRAME_LEAVE,
    CALL_FLAG_FRAME_PROTOCOL,
    CALL_FLAG_LOOP_SAFEPOINT,
    CALL_FLAG_STACKMAP_SKIP,
)
from .self_backend_ir import (
    GlobalDef,
    PARSED_INSTRUCTION_KIND_BR,
    PARSED_INSTRUCTION_KIND_BR_COND,
    PARSED_INSTRUCTION_KIND_CALL,
    PARSED_INSTRUCTION_KIND_CAST,
    PARSED_INSTRUCTION_KIND_FREEZE,
    PARSED_INSTRUCTION_KIND_GEP,
    PARSED_INSTRUCTION_KIND_ICMP,
    PARSED_INSTRUCTION_KIND_LOAD,
    PARSED_INSTRUCTION_KIND_LOAD_ATOMIC,
    PARSED_INSTRUCTION_KIND_SELECT,
    PARSED_INSTRUCTION_KIND_SWITCH,
    ParsedBlock,
    ParsedFunction,
    ParsedInstr,
    SlotInfo,
    TypeDesc,
    _align_to,
    parsed_function_alloca_slot,
    parsed_function_alloca_slot_offset,
    parsed_function_alloca_slot_type,
    parsed_function_value_slot,
    parsed_function_value_slot_offset,
    parsed_function_value_slot_type,
    text_key_mapping_get,
    text_key_names_equal,
)
from .self_backend_kernel import get_indexed_function_kernel
from .self_backend_kernel import (
    IndexedFunctionKernel,
    TYPE_KIND_ARRAY,
    TYPE_KIND_PTR,
    TYPE_KIND_STRUCT,
)
from .self_backend_value_arena import (
    CompilerInt2,
    CompilerInt3,
    CompilerInt4,
    CompilerIntArena,
)
from .self_backend_parse import const_int_from_value
from .self_backend_analysis import (
    _stable_text_bucket_key,
    instruction_defined_value,
    instruction_used_values,
    terminator_used_values,
)


_STACK_MAP_RECORD_SCALAR_COUNT = 10
_py_bytes_new: "extern" = extern("py_bytes_new", (c_ptr, c_int64), c_obj)


_FRAME_ENTER = frozenset(("pcc_gc_frame_enter", "pcc_gc_frame_enter_lifo"))
_FRAME_LEAVE = frozenset(("pcc_gc_frame_leave", "pcc_gc_frame_leave_lifo"))
_FRAME_PROTOCOL = _FRAME_ENTER | _FRAME_LEAVE

# Shared empty answer for instruction slots with no managed live-out.  It is
# immutable and read-only, so one instance serves every slot.


@dataclass(frozen=True)
class PlannedRootLocation:
    offset: int
    owned: bool


@dataclass(frozen=True)
class PlannedManagedReload:
    source_offset: int
    destination_offset: int
    derived_offset: int = 0


@dataclass(frozen=True)
class PlannedSafepoint:
    safepoint_id: int
    label: str
    kind: int
    locations: tuple[PlannedRootLocation, ...]
    flags: int = 0
    exceptional_block: str = ""
    continuation_id: int = 0
    reloads: tuple[PlannedManagedReload, ...] = ()


class PackedPlannedSafepoints:
    """AArch64 safepoint records with unboxed scalar projection.

    Pointer-bearing spellings and the already-interned location/reload tuples
    stay in traced side tables.  Four scalar fields are appended/read as one
    value projection, never as four per-scalar arena method calls.
    """

    scalars: CompilerIntArena
    spans: CompilerIntArena
    labels: list[str]
    exceptional_blocks: list[str]
    location_scalars: CompilerIntArena
    location_group_spans: CompilerIntArena
    location_group_keys: list[str]
    reload_scalars: CompilerIntArena
    block_names: list[str]
    entry_routes: CompilerIntArena
    suffix_routes: CompilerIntArena
    terminator_routes: CompilerIntArena
    entry_route_spans: CompilerIntArena
    suffix_route_spans: CompilerIntArena
    terminator_route_spans: CompilerIntArena
    location_identity_index: dict[int, tuple]
    location_content_index: dict[str, int]
    diagnostic_projections: int

    __slots__ = (
        "scalars",
        "spans",
        "labels",
        "exceptional_blocks",
        "location_scalars",
        "location_group_spans",
        "location_group_keys",
        "reload_scalars",
        "block_names",
        "entry_routes",
        "suffix_routes",
        "terminator_routes",
        "entry_route_spans",
        "suffix_route_spans",
        "terminator_route_spans",
        "location_identity_index",
        "location_content_index",
        "diagnostic_projections",
    )

    def __init__(self, capacity: int = 0, block_names=()) -> None:
        self.scalars = CompilerIntArena(max(1, capacity * 4))
        self.spans = CompilerIntArena(max(1, capacity * 4))
        self.labels: list[str] = []
        self.exceptional_blocks: list[str] = []
        self.location_scalars = CompilerIntArena(max(1, capacity * 2))
        self.location_group_spans = CompilerIntArena(max(1, capacity * 2))
        self.location_group_keys: list[str] = []
        self.reload_scalars = CompilerIntArena(max(1, capacity * 3))
        self.block_names = block_names
        self.entry_routes = CompilerIntArena()
        self.suffix_routes = CompilerIntArena()
        self.terminator_routes = CompilerIntArena()
        self.entry_route_spans = CompilerIntArena()
        self.suffix_route_spans = CompilerIntArena()
        self.terminator_route_spans = CompilerIntArena()
        self.location_identity_index: dict[int, tuple] = {}
        self.location_content_index: dict[str, int] = {}
        self.diagnostic_projections = 0

    def __len__(self) -> int:
        return len(self.labels)

    def append(
        self,
        record_id: int,
        label: str,
        kind: int,
        locations: tuple[PlannedRootLocation, ...],
        flags: int,
        exceptional_block: str,
        continuation_id: int,
        reloads: tuple[PlannedManagedReload, ...],
    ) -> int:
        record_index = len(self.labels)
        self.scalars.append4(record_id, kind, flags, continuation_id)
        identity_key = id(locations)
        location_entry = None
        if identity_key in self.location_identity_index:
            candidate = self.location_identity_index[identity_key]
            if candidate[0] is locations:
                location_entry = candidate
        if location_entry is None:
            key_parts: list[str] = []
            for location in locations:
                key_parts.append(str(location.offset))
                key_parts.append("1" if location.owned else "0")
            content_key = ",".join(key_parts)
            if content_key in self.location_content_index:
                location_group_id = self.location_content_index[content_key]
                location_group: CompilerInt2 = self.location_group_spans.get2_unchecked(
                    location_group_id
                )
                location_start = location_group.first
                location_count = location_group.second
            else:
                location_start = len(self.location_scalars) // 2
                location_count = len(locations)
                location_group_id = len(self.location_group_keys)
                self.location_content_index[content_key] = location_group_id
                self.location_group_spans.append2(
                    location_start,
                    location_count,
                )
                self.location_group_keys.append(content_key)
                for location in locations:
                    self.location_scalars.append2(
                        location.offset,
                        1 if location.owned else 0,
                    )
            location_entry = (
                locations,
                location_group_id,
            )
            self.location_identity_index[identity_key] = location_entry
        else:
            location_group_id = location_entry[1]
            location_group: CompilerInt2 = self.location_group_spans.get2_unchecked(
                location_group_id
            )
            location_count = location_group.second
        reload_start = len(self.reload_scalars) // 3
        for reload in reloads:
            self.reload_scalars.append3(
                reload.source_offset,
                reload.destination_offset,
                reload.derived_offset,
            )
        self.spans.append4(
            location_group_id,
            location_count,
            reload_start,
            len(reloads),
        )
        self.labels.append(label)
        self.exceptional_blocks.append(exceptional_block)
        return record_index

    def adopt_root_state_locations(
        self,
        root_states: PackedRootStatePlane,
    ) -> None:
        self.location_scalars.close()
        self.location_group_spans.close()
        self.location_scalars = root_states.state_locations
        self.location_group_spans = root_states.state_location_spans
        root_states.owns_state_locations = False

    def append_native(
        self,
        record_id: int,
        label: str,
        kind: int,
        root_states: PackedRootStatePlane,
        state_id: int,
        flags: int,
        exceptional_block: str,
        continuation_id: int,
        reloads: PackedReloadScratch,
    ) -> int:
        record_index = len(self.labels)
        self.scalars.append4(record_id, kind, flags, continuation_id)
        root_states.materialize_state_locations(state_id)
        locations: CompilerInt2 = root_states.state_location_spans.get2_unchecked(
            state_id
        )
        reload_start = len(self.reload_scalars) // 3
        reload_index = 0
        reload_count = len(reloads.records) // 3
        while reload_index < reload_count:
            reload: CompilerInt3 = reloads.records.get3_unchecked(reload_index)
            self.reload_scalars.append3(
                reload.first,
                reload.second,
                reload.third,
            )
            reload_index += 1
        self.spans.append4(
            state_id,
            locations.second,
            reload_start,
            reload_count,
        )
        self.labels.append(label)
        self.exceptional_blocks.append(exceptional_block)
        return record_index

    def scalar(self, record_index: int) -> CompilerInt4:
        return self.scalars.get4_unchecked(record_index)

    def span(self, record_index: int) -> CompilerInt4:
        return self.spans.get4_unchecked(record_index)

    def label(self, record_index: int) -> str:
        return self.labels[record_index]

    def record_locations(
        self, record_index: int
    ) -> tuple[PlannedRootLocation, ...]:
        span: CompilerInt4 = self.span(record_index)
        group: CompilerInt2 = self.location_group_spans.get2_unchecked(
            span.first
        )
        locations: list[PlannedRootLocation] = []
        location_index = 0
        while location_index < span.second:
            raw: CompilerInt2 = self.location_scalars.get2_unchecked(
                group.first + location_index
            )
            raw_offset: int = raw.first
            raw_owned: int = raw.second
            locations.append(PlannedRootLocation(
                raw_offset,
                raw_owned != 0,
            ))
            location_index += 1
        return tuple(locations)

    def exceptional_block(self, record_index: int) -> str:
        return self.exceptional_blocks[record_index]

    def record_reloads(
        self, record_index: int
    ) -> tuple[PlannedManagedReload, ...]:
        span: CompilerInt4 = self.span(record_index)
        reloads: list[PlannedManagedReload] = []
        reload_index = 0
        while reload_index < span.fourth:
            raw: CompilerInt3 = self.reload_scalars.get3_unchecked(
                span.third + reload_index
            )
            raw_source: int = raw.first
            raw_destination: int = raw.second
            raw_derived: int = raw.third
            reloads.append(PlannedManagedReload(
                raw_source,
                raw_destination,
                raw_derived,
            ))
            reload_index += 1
        return tuple(reloads)

    def reload_scalar(
        self, record_index: int, reload_index: int
    ) -> CompilerInt3:
        span: CompilerInt4 = self.span(record_index)
        return self.reload_scalars.get3_unchecked(
            span.third + reload_index
        )

    def location_scalar(
        self, location_index: int
    ) -> CompilerInt2:
        return self.location_scalars.get2_unchecked(location_index)

    def location_count(self) -> int:
        return len(self.location_scalars) // 2

    def location_group(self, record_index: int) -> CompilerInt2:
        span: CompilerInt4 = self.span(record_index)
        return self.location_group_spans.get2_unchecked(span.first)

    def location_group_key(self, group_id: int) -> str:
        while len(self.location_group_keys) <= group_id:
            self.location_group_keys.append("")
        existing = self.location_group_keys[group_id]
        if existing:
            return existing
        group: CompilerInt2 = self.location_group_spans.get2_unchecked(group_id)
        parts: list[str] = []
        index = 0
        while index < group.second:
            location: CompilerInt2 = self.location_scalars.get2_unchecked(
                group.first + index
            )
            parts.append(str(location.first))
            parts.append("1" if location.second else "0")
            index += 1
        result = ",".join(parts)
        self.location_group_keys[group_id] = result
        return result

    def add_entry_route(self, block_id: int, record_index: int) -> None:
        self.entry_routes.append2(block_id, record_index)

    def add_suffix_route(
        self,
        block_id: int,
        instruction_index: int,
        record_index: int,
    ) -> None:
        self.suffix_routes.append3(
            block_id,
            instruction_index,
            record_index,
        )

    def add_terminator_route(
        self,
        block_id: int,
        record_index: int,
        needs_separator: bool,
    ) -> None:
        self.terminator_routes.append3(
            block_id,
            record_index,
            1 if needs_separator else 0,
        )

    def entry_route(self, route_index: int) -> CompilerInt2:
        return self.entry_routes.get2_unchecked(route_index)

    def suffix_route(self, route_index: int) -> CompilerInt3:
        return self.suffix_routes.get3_unchecked(route_index)

    def terminator_route(self, route_index: int) -> CompilerInt3:
        return self.terminator_routes.get3_unchecked(route_index)

    def finish_build(self) -> None:
        self._build_route_spans(self.entry_routes, 2, self.entry_route_spans)
        self._build_route_spans(self.suffix_routes, 3, self.suffix_route_spans)
        self._build_route_spans(
            self.terminator_routes,
            3,
            self.terminator_route_spans,
        )
        self.location_identity_index.clear()
        self.location_content_index.clear()

    def _build_route_spans(
        self,
        routes: CompilerIntArena,
        stride: int,
        spans: CompilerIntArena,
    ) -> None:
        block_count = len(self.block_names)
        block_id = 0
        while block_id < block_count:
            spans.append2(-1, 0)
            block_id += 1
        route_count = len(routes) // stride
        route_index = 0
        while route_index < route_count:
            route_block_id = routes.get_unchecked(route_index * stride)
            span_offset = route_block_id * 2
            if spans.get_unchecked(span_offset) < 0:
                spans.set_unchecked(span_offset, route_index)
            spans.set_unchecked(
                span_offset + 1,
                spans.get_unchecked(span_offset + 1) + 1,
            )
            route_index += 1

    def entry_route_span(self, block_id: int) -> CompilerInt2:
        return self.entry_route_spans.get2_unchecked(block_id)

    def suffix_route_span(self, block_id: int) -> CompilerInt2:
        return self.suffix_route_spans.get2_unchecked(block_id)

    def terminator_route_span(self, block_id: int) -> CompilerInt2:
        return self.terminator_route_spans.get2_unchecked(block_id)

    def materialize(self, record_index: int) -> PlannedSafepoint:
        self.diagnostic_projections += 1
        scalar: CompilerInt4 = self.scalar(record_index)
        record_id: int = scalar.first
        record_kind: int = scalar.second
        record_flags: int = scalar.third
        continuation_id: int = scalar.fourth
        return PlannedSafepoint(
            record_id,
            self.label(record_index),
            record_kind,
            self.record_locations(record_index),
            record_flags,
            self.exceptional_block(record_index),
            continuation_id,
            self.record_reloads(record_index),
        )

    def __getitem__(self, record_index: int) -> PlannedSafepoint:
        if record_index < 0:
            record_index += len(self)
        if record_index < 0 or record_index >= len(self):
            raise IndexError(record_index)
        return self.materialize(record_index)

    def __iter__(self):
        record_index = 0
        while record_index < len(self):
            yield self.materialize(record_index)
            record_index += 1

    def diagnostic_records(self) -> tuple[PlannedSafepoint, ...]:
        return tuple(self)

    def close(self) -> None:
        self.scalars.close()
        self.spans.close()
        self.location_scalars.close()
        self.location_group_spans.close()
        self.reload_scalars.close()
        self.entry_routes.close()
        self.suffix_routes.close()
        self.terminator_routes.close()
        self.entry_route_spans.close()
        self.suffix_route_spans.close()
        self.terminator_route_spans.close()
        self.location_identity_index.clear()
        self.location_content_index.clear()


class PackedManagedLiveness:
    """Batch managed live-after states referenced by packed call records.

    Managed liveness is extremely sparse in the compiler workload: item311
    tracks 518 values but no live-after set contains more than two. A dense
    block-by-value bit matrix would therefore spend memory and scan work on
    zeros. Each mutation publishes one four-i64 state record containing count,
    first value ID, second value ID or encoded overflow start, and a reserved
    word. A call record stores its state ID in an existing reserved scalar;
    the common empty state needs no additional record read. Uncommon tails
    above two values use one scalar overflow arena.
    """

    states: CompilerIntArena
    overflow_ids: CompilerIntArena

    __slots__ = (
        "states",
        "overflow_ids",
    )

    def __init__(self) -> None:
        self.states = CompilerIntArena()
        self.states.append4(0, -1, -1, 0)
        self.overflow_ids = CompilerIntArena()

    def append_state(self, live_values: set[int]) -> int:
        ordered = sorted(live_values)
        count = len(ordered)
        if count == 0:
            return 0
        state_id = len(self.states) // 4
        first = ordered[0]
        if count == 1:
            second_or_overflow = -1
        elif count == 2:
            second_or_overflow = ordered[1]
        else:
            overflow_start = len(self.overflow_ids)
            index = 1
            while index < count:
                self.overflow_ids.append(ordered[index])
                index += 1
            second_or_overflow = -overflow_start - 2
        self.states.append4(count, first, second_or_overflow, 0)
        return state_id

    def append_state_words(
        self,
        words: CompilerIntArena,
        tracked_values: CompilerIntArena,
        word_count: int,
    ) -> int:
        count = 0
        first = -1
        second_or_overflow = -1
        overflow_start = len(self.overflow_ids)
        tracked_count = len(tracked_values)
        word_index = 0
        while word_index < word_count:
            word = words.get_unchecked(word_index)
            bit_index = 0
            while word:
                if word & 1:
                    tracked_index = word_index * 30 + bit_index
                    if tracked_index >= tracked_count:
                        break
                    value_id = tracked_values.get_unchecked(tracked_index)
                    if count == 0:
                        first = value_id
                    elif count == 1:
                        second_or_overflow = value_id
                    else:
                        if count == 2:
                            self.overflow_ids.append(second_or_overflow)
                        self.overflow_ids.append(value_id)
                        second_or_overflow = -overflow_start - 2
                    count += 1
                word >>= 1
                bit_index += 1
            word_index += 1
        if count == 0:
            return 0
        state_id = len(self.states) // 4
        self.states.append4(count, first, second_or_overflow, 0)
        return state_id

    def record(self, state_id: int) -> CompilerInt4:
        return self.states.get4_unchecked(state_id)

    def close(self) -> None:
        self.states.close()
        self.overflow_ids.close()


_INDEX_MASK = (1 << 40) - 1


def _native_index_capacity(size_hint: int) -> int:
    capacity = 8
    required = max(1, size_hint * 2)
    while capacity < required:
        capacity *= 2
    return capacity


def _native_pair_hash(first: int, second: int) -> int:
    left = (first + 0x1F123BB5) & _INDEX_MASK
    right = (second + 0x2C1B3C6D) & _INDEX_MASK
    return ((left * 1315423911) ^ (right * 2654435761)) & _INDEX_MASK


def _native_triple_hash(first: int, second: int, third: int) -> int:
    return _native_pair_hash(_native_pair_hash(first, second), third)


class PackedPointerAliases:
    """Dense value-ID pointer provenance; text refs remain negative IDs."""

    __slots__ = ("records", "result", "value_count")

    def __init__(self, value_count: int) -> None:
        self.value_count = value_count
        self.records = CompilerIntArena(max(1, value_count * 3))
        self.records.append_zeros(value_count * 3)
        self.result = CompilerIntArena(2)
        self.result.append_zeros(2)

    def set_alias(self, value_id: int, base_ref: int, offset: int) -> None:
        if value_id < 0 or value_id >= self.value_count:
            return
        self.records.set3_unchecked(value_id * 3, 1, base_ref, offset)

    def resolve(self, base_ref: int) -> None:
        current = base_ref
        offset = 0
        steps = 0
        while current >= 0 and current < self.value_count:
            record_start = current * 3
            if self.records.get_unchecked(record_start) == 0:
                break
            current = self.records.get_unchecked(record_start + 1)
            offset += self.records.get_unchecked(record_start + 2)
            steps += 1
            if steps > self.value_count:
                raise BackendUnavailable("pointer alias cycle")
        self.result.set2_unchecked(0, current, offset)

    def close(self) -> None:
        self.records.close()
        self.result.close()


class PackedRootStatePlane:
    """Canonical root groups/states and CFG entry state IDs in raw arenas."""

    __slots__ = (
        "group_keys",
        "group_location_spans",
        "group_locations",
        "group_index",
        "group_index_capacity",
        "registered_roots",
        "registered_index",
        "registered_index_capacity",
        "state_spans",
        "state_group_ids",
        "state_index",
        "state_index_capacity",
        "transition_index",
        "transition_index_capacity",
        "state_location_spans",
        "state_locations",
        "entry_state_ids",
        "queue",
        "scratch",
        "owns_state_locations",
    )

    def __init__(
        self,
        block_count: int,
        protocol_hint: int,
    ) -> None:
        group_capacity = _native_index_capacity(protocol_hint + 1)
        self.group_keys = CompilerIntArena()
        self.group_location_spans = CompilerIntArena()
        self.group_locations = CompilerIntArena()
        self.group_index_capacity = group_capacity
        self.group_index = CompilerIntArena(group_capacity * 2)
        self.group_index.append_zeros(group_capacity * 2)
        registered_capacity = _native_index_capacity(protocol_hint * 4 + 1)
        self.registered_roots = CompilerIntArena()
        self.registered_index_capacity = registered_capacity
        self.registered_index = CompilerIntArena(registered_capacity * 2)
        self.registered_index.append_zeros(registered_capacity * 2)
        state_capacity = _native_index_capacity(block_count + protocol_hint + 1)
        self.state_spans = CompilerIntArena()
        self.state_group_ids = CompilerIntArena()
        self.state_index_capacity = state_capacity
        self.state_index = CompilerIntArena(state_capacity * 2)
        self.state_index.append_zeros(state_capacity * 2)
        transition_capacity = _native_index_capacity(protocol_hint + 1)
        self.transition_index_capacity = transition_capacity
        self.transition_index = CompilerIntArena(transition_capacity * 4)
        self.transition_index.append_zeros(transition_capacity * 4)
        self.state_location_spans = CompilerIntArena()
        self.state_locations = CompilerIntArena()
        self.entry_state_ids = CompilerIntArena(max(1, block_count))
        block_index = 0
        while block_index < block_count:
            self.entry_state_ids.append(-1)
            block_index += 1
        self.queue = CompilerIntArena(max(1, block_count))
        self.scratch = CompilerIntArena()
        self.owns_state_locations = True
        self.state_spans.append4(0, 0, 0, 0)
        self.state_location_spans.append2(-1, 0)
        self._insert_state_index(0, 0)

    def _insert_state_index(self, state_hash: int, state_id: int) -> None:
        slot = state_hash & (self.state_index_capacity - 1)
        probes = 0
        while probes < self.state_index_capacity:
            offset = slot * 2
            if self.state_index.get_unchecked(offset + 1) == 0:
                self.state_index.set2_unchecked(
                    offset,
                    state_hash,
                    state_id + 1,
                )
                return
            slot = (slot + 1) & (self.state_index_capacity - 1)
            probes += 1
        raise BackendUnavailable("root state index is full")

    def _cached_transition(
        self,
        state_id: int,
        group_id: int,
        add: bool,
    ) -> int:
        operation = 1 if add else 0
        key_hash = _native_triple_hash(state_id, group_id, operation)
        slot = key_hash & (self.transition_index_capacity - 1)
        probes = 0
        while probes < self.transition_index_capacity:
            offset = slot * 4
            encoded = self.transition_index.get_unchecked(offset + 3)
            if encoded == 0:
                return -1
            if (
                self.transition_index.get_unchecked(offset) == state_id
                and self.transition_index.get_unchecked(offset + 1) == group_id
                and self.transition_index.get_unchecked(offset + 2) == operation
            ):
                return encoded - 1
            slot = (slot + 1) & (self.transition_index_capacity - 1)
            probes += 1
        return -1

    def _insert_transition(
        self,
        state_id: int,
        group_id: int,
        add: bool,
        result_state_id: int,
    ) -> None:
        operation = 1 if add else 0
        key_hash = _native_triple_hash(state_id, group_id, operation)
        slot = key_hash & (self.transition_index_capacity - 1)
        probes = 0
        while probes < self.transition_index_capacity:
            offset = slot * 4
            encoded = self.transition_index.get_unchecked(offset + 3)
            if encoded == 0:
                self.transition_index.set_unchecked(offset, state_id)
                self.transition_index.set_unchecked(offset + 1, group_id)
                self.transition_index.set_unchecked(offset + 2, operation)
                self.transition_index.set_unchecked(
                    offset + 3,
                    result_state_id + 1,
                )
                return
            if (
                self.transition_index.get_unchecked(offset) == state_id
                and self.transition_index.get_unchecked(offset + 1) == group_id
                and self.transition_index.get_unchecked(offset + 2) == operation
            ):
                if encoded - 1 != result_state_id:
                    raise BackendUnavailable(
                        "root transition cache has conflicting results"
                    )
                return
            slot = (slot + 1) & (self.transition_index_capacity - 1)
            probes += 1
        raise BackendUnavailable("root transition index is full")

    def _group_id(self, base_ref: int, offset: int) -> int:
        key_hash = _native_pair_hash(base_ref, offset)
        slot = key_hash & (self.group_index_capacity - 1)
        probes = 0
        while probes < self.group_index_capacity:
            index_offset = slot * 2
            encoded = self.group_index.get_unchecked(index_offset + 1)
            if encoded == 0:
                return -1
            group_id = encoded - 1
            key_offset = group_id * 2
            if (
                self.group_index.get_unchecked(index_offset) == key_hash
                and self.group_keys.get_unchecked(key_offset) == base_ref
                and self.group_keys.get_unchecked(key_offset + 1) == offset
            ):
                return group_id
            slot = (slot + 1) & (self.group_index_capacity - 1)
            probes += 1
        return -1

    def _insert_group_index(
        self,
        base_ref: int,
        offset: int,
        group_id: int,
    ) -> None:
        key_hash = _native_pair_hash(base_ref, offset)
        slot = key_hash & (self.group_index_capacity - 1)
        probes = 0
        while probes < self.group_index_capacity:
            index_offset = slot * 2
            if self.group_index.get_unchecked(index_offset + 1) == 0:
                self.group_index.set2_unchecked(
                    index_offset,
                    key_hash,
                    group_id + 1,
                )
                return
            slot = (slot + 1) & (self.group_index_capacity - 1)
            probes += 1
        raise BackendUnavailable("root group index is full")

    def _register_root(
        self,
        base_ref: int,
        byte_offset: int,
        frame_offset: int,
    ) -> None:
        key_hash = _native_pair_hash(base_ref, byte_offset)
        slot = key_hash & (self.registered_index_capacity - 1)
        probes = 0
        while probes < self.registered_index_capacity:
            index_offset = slot * 2
            encoded = self.registered_index.get_unchecked(index_offset + 1)
            if encoded == 0:
                root_id = len(self.registered_roots) // 3
                self.registered_roots.append3(
                    base_ref,
                    byte_offset,
                    frame_offset,
                )
                self.registered_index.set2_unchecked(
                    index_offset,
                    key_hash,
                    root_id + 1,
                )
                return
            root_id = encoded - 1
            root_offset = root_id * 3
            if (
                self.registered_index.get_unchecked(index_offset) == key_hash
                and self.registered_roots.get_unchecked(root_offset) == base_ref
                and self.registered_roots.get_unchecked(root_offset + 1)
                == byte_offset
            ):
                if self.registered_roots.get_unchecked(root_offset + 2) != frame_offset:
                    raise BackendUnavailable("managed root has conflicting frame offsets")
                return
            slot = (slot + 1) & (self.registered_index_capacity - 1)
            probes += 1
        raise BackendUnavailable("registered root index is full")

    def registered_root_offset(self, base_ref: int, byte_offset: int) -> int:
        key_hash = _native_pair_hash(base_ref, byte_offset)
        slot = key_hash & (self.registered_index_capacity - 1)
        probes = 0
        while probes < self.registered_index_capacity:
            index_offset = slot * 2
            encoded = self.registered_index.get_unchecked(index_offset + 1)
            if encoded == 0:
                return NO_OFFSET
            root_id = encoded - 1
            root_offset = root_id * 3
            if (
                self.registered_index.get_unchecked(index_offset) == key_hash
                and self.registered_roots.get_unchecked(root_offset) == base_ref
                and self.registered_roots.get_unchecked(root_offset + 1)
                == byte_offset
            ):
                return self.registered_roots.get_unchecked(root_offset + 2)
            slot = (slot + 1) & (self.registered_index_capacity - 1)
            probes += 1
        return NO_OFFSET

    def intern_group(
        self,
        base_ref: int,
        origin_offset: int,
        count: int,
        owned: bool,
        alloca_offset: int,
        frame_size: int,
    ) -> int:
        existing = self._group_id(base_ref, origin_offset)
        if existing >= 0:
            span: CompilerInt2 = self.group_location_spans.get2_unchecked(
                existing
            )
            if span.second != count:
                raise BackendUnavailable(
                    "managed slot group changes frame-map width"
                )
            index = 0
            while index < count:
                location: CompilerInt2 = self.group_locations.get2_unchecked(
                    span.first + index
                )
                expected_offset = (
                    -alloca_offset + origin_offset + index * POINTER_SIZE
                )
                if (
                    location.first != expected_offset
                    or bool(location.second) != owned
                ):
                    raise BackendUnavailable(
                        "managed slot group changes location ownership"
                    )
                index += 1
            return existing
        group_id = len(self.group_keys) // 2
        location_start = len(self.group_locations) // 2
        location_index = 0
        while location_index < count:
            byte_offset = origin_offset + location_index * POINTER_SIZE
            frame_offset = -alloca_offset + byte_offset
            if (
                frame_offset >= 0
                or -frame_offset > frame_size
                or (-frame_offset) % POINTER_SIZE
            ):
                raise BackendUnavailable("managed slot is outside the final frame")
            self.group_locations.append2(
                frame_offset,
                1 if owned else 0,
            )
            self._register_root(base_ref, byte_offset, frame_offset)
            location_index += 1
        self.group_keys.append2(base_ref, origin_offset)
        self.group_location_spans.append2(location_start, count)
        self._insert_group_index(base_ref, origin_offset, group_id)
        return group_id

    def group_id(self, base_ref: int, origin_offset: int) -> int:
        return self._group_id(base_ref, origin_offset)

    def state_contains(self, state_id: int, group_id: int) -> bool:
        span: CompilerInt4 = self.state_spans.get4_unchecked(state_id)
        word_index = group_id // 30
        if word_index >= span.second:
            return False
        word = self.state_group_ids.get_unchecked(span.first + word_index)
        return bool(word & (1 << (group_id % 30)))

    def _find_scratch_state(self, state_hash: int) -> int:
        slot = state_hash & (self.state_index_capacity - 1)
        probes = 0
        while probes < self.state_index_capacity:
            index_offset = slot * 2
            encoded = self.state_index.get_unchecked(index_offset + 1)
            if encoded == 0:
                return -1
            candidate_id = encoded - 1
            candidate: CompilerInt4 = self.state_spans.get4_unchecked(candidate_id)
            if (
                self.state_index.get_unchecked(index_offset) == state_hash
                and candidate.second == len(self.scratch)
            ):
                matched = True
                item_index = 0
                while item_index < candidate.second:
                    if self.state_group_ids.get_unchecked(
                        candidate.first + item_index
                    ) != self.scratch.get_unchecked(item_index):
                        matched = False
                        break
                    item_index += 1
                if matched:
                    return candidate_id
            slot = (slot + 1) & (self.state_index_capacity - 1)
            probes += 1
        return -1

    def transition(self, state_id: int, group_id: int, add: bool) -> int:
        cached = self._cached_transition(state_id, group_id, add)
        if cached >= 0:
            return cached
        present = self.state_contains(state_id, group_id)
        if add and present:
            raise BackendUnavailable("managed slot is registered twice")
        if not add and not present:
            raise BackendUnavailable("managed slot leaves without an active enter")
        self.scratch.clear()
        source: CompilerInt4 = self.state_spans.get4_unchecked(state_id)
        word_index = 0
        while word_index < source.second:
            self.scratch.append(
                self.state_group_ids.get_unchecked(source.first + word_index)
            )
            word_index += 1
        target_word = group_id // 30
        while len(self.scratch) <= target_word:
            self.scratch.append(0)
        bit = 1 << (group_id % 30)
        word = self.scratch.get_unchecked(target_word)
        if add:
            self.scratch.set_unchecked(target_word, word | bit)
        else:
            self.scratch.set_unchecked(
                target_word,
                word & (((1 << 30) - 1) ^ bit),
            )
            while (
                len(self.scratch) > 0
                and self.scratch.get_unchecked(len(self.scratch) - 1) == 0
            ):
                self.scratch.truncate(len(self.scratch) - 1)
        result_count = source.fourth + (1 if add else -1)
        state_hash = (
            source.third
            ^ source.fourth
            ^ result_count
            ^ _native_pair_hash(group_id, 0x35A19D7)
        ) & _INDEX_MASK
        existing = self._find_scratch_state(state_hash)
        if existing >= 0:
            self._insert_transition(state_id, group_id, add, existing)
            return existing
        result = len(self.state_spans) // 4
        group_start = len(self.state_group_ids)
        index = 0
        while index < len(self.scratch):
            self.state_group_ids.append(self.scratch.get_unchecked(index))
            index += 1
        self.state_spans.append4(
            group_start,
            len(self.scratch),
            state_hash,
            result_count,
        )
        self.state_location_spans.append2(-1, 0)
        self._insert_state_index(state_hash, result)
        self._insert_transition(state_id, group_id, add, result)
        return result

    def materialize_state_locations(self, state_id: int) -> None:
        span_offset = state_id * 2
        existing_start = self.state_location_spans.get_unchecked(span_offset)
        if existing_start >= 0:
            return
        self.scratch.clear()
        state: CompilerInt4 = self.state_spans.get4_unchecked(state_id)
        word_index = 0
        while word_index < state.second:
            word = self.state_group_ids.get_unchecked(state.first + word_index)
            bit_index = 0
            while word:
                if word & 1:
                    group_id = word_index * 30 + bit_index
                    group: CompilerInt2 = self.group_location_spans.get2_unchecked(
                        group_id
                    )
                    location_index = 0
                    while location_index < group.second:
                        location: CompilerInt2 = self.group_locations.get2_unchecked(
                            group.first + location_index
                        )
                        encoded = location.first * 2 + (
                            0 if location.second else 1
                        )
                        self.scratch.append(encoded)
                        location_index += 1
                word >>= 1
                bit_index += 1
            word_index += 1
        self.scratch.sort()
        location_start = len(self.state_locations) // 2
        index = 0
        while index < len(self.scratch):
            encoded = self.scratch.get_unchecked(index)
            self.state_locations.append2(
                encoded // 2,
                1 if encoded % 2 == 0 else 0,
            )
            index += 1
        self.state_location_spans.set2_unchecked(
            span_offset,
            location_start,
            len(self.scratch),
        )

    def ensure_state_locations(self, state_id: int) -> CompilerInt2:
        self.materialize_state_locations(state_id)
        return self.state_location_spans.get2_unchecked(state_id)

    def has_location_offset(self, state_id: int, offset: int) -> bool:
        self.materialize_state_locations(state_id)
        locations: CompilerInt2 = self.state_location_spans.get2_unchecked(
            state_id
        )
        low = 0
        high = locations.second
        while low < high:
            middle = (low + high) // 2
            current = self.state_locations.get_unchecked(
                (locations.first + middle) * 2
            )
            if current < offset:
                low = middle + 1
            else:
                high = middle
        if low >= locations.second:
            return False
        return self.state_locations.get_unchecked(
            (locations.first + low) * 2
        ) == offset

    def close(self) -> None:
        self.group_keys.close()
        self.group_location_spans.close()
        self.group_locations.close()
        self.group_index.close()
        self.registered_roots.close()
        self.registered_index.close()
        self.state_spans.close()
        self.state_group_ids.close()
        self.state_index.close()
        self.transition_index.close()
        if self.owns_state_locations:
            self.state_location_spans.close()
            self.state_locations.close()
        self.entry_state_ids.close()
        self.queue.close()
        self.scratch.close()


_ORIGIN_UNKNOWN = 0
_ORIGIN_RAW = 1
_ORIGIN_MANAGED = 2
_ORIGIN_AMBIGUOUS = 3
_TRANSFER_NONE = 0
_TRANSFER_COPY = 1
_TRANSFER_GEP = 2
_TRANSFER_JOIN = 3


class PackedManagedOrigins:
    """Dense managed-pointer provenance lattice indexed by value ID."""

    __slots__ = (
        "states",
        "transfers",
        "transfer_ids",
        "proposal",
        "value_count",
    )

    def __init__(self, value_count: int) -> None:
        self.value_count = value_count
        self.states = CompilerIntArena(max(1, value_count * 3))
        self.states.append_zeros(value_count * 3)
        self.transfers = CompilerIntArena(max(1, value_count * 4))
        self.transfers.append_zeros(value_count * 4)
        self.transfer_ids = CompilerIntArena()
        self.proposal = CompilerIntArena(3)
        self.proposal.append_zeros(3)

    def set_state(
        self,
        value_id: int,
        kind: int,
        root_offset: int = 0,
        derived_offset: int = 0,
    ) -> None:
        self.states.set3_unchecked(
            value_id * 3,
            kind,
            root_offset,
            derived_offset,
        )

    def state(self, value_id: int) -> CompilerInt3:
        if value_id < 0 or value_id >= self.value_count:
            return CompilerInt3(_ORIGIN_RAW, 0, 0)
        return self.states.get3_unchecked(value_id)

    def set_transfer(
        self,
        value_id: int,
        kind: int,
        first: int,
        second: int = 0,
    ) -> None:
        self.transfers.set3_unchecked(
            value_id * 4,
            kind,
            first,
            second,
        )

    def set_join(self, value_id: int, values: CompilerIntArena) -> None:
        start = len(self.transfer_ids)
        index = 0
        while index < len(values):
            self.transfer_ids.append(values.get_unchecked(index))
            index += 1
        self.transfers.set3_unchecked(
            value_id * 4,
            _TRANSFER_JOIN,
            start,
            len(values),
        )

    def _write_state(self, value_id: int) -> None:
        if value_id < 0 or value_id >= self.value_count:
            self.proposal.set3_unchecked(0, _ORIGIN_RAW, 0, 0)
            return
        offset = value_id * 3
        self.proposal.set3_unchecked(
            0,
            self.states.get_unchecked(offset),
            self.states.get_unchecked(offset + 1),
            self.states.get_unchecked(offset + 2),
        )

    def _write_join_sources(self, start: int, count: int) -> None:
        saw_raw = False
        have_managed = False
        managed_root = 0
        managed_derived = 0
        index = 0
        while index < count:
            source_id = self.transfer_ids.get_unchecked(start + index)
            if source_id < 0 or source_id >= self.value_count:
                source_kind = _ORIGIN_RAW
                source_root = 0
                source_derived = 0
            else:
                source_offset = source_id * 3
                source_kind = self.states.get_unchecked(source_offset)
                source_root = self.states.get_unchecked(source_offset + 1)
                source_derived = self.states.get_unchecked(source_offset + 2)
            if source_kind == _ORIGIN_AMBIGUOUS:
                self.proposal.set3_unchecked(0, _ORIGIN_AMBIGUOUS, 0, 0)
                return
            if source_kind == _ORIGIN_RAW:
                saw_raw = True
            elif source_kind == _ORIGIN_MANAGED:
                if not have_managed:
                    have_managed = True
                    managed_root = source_root
                    managed_derived = source_derived
                elif (
                    managed_root != source_root
                    or managed_derived != source_derived
                ):
                    self.proposal.set3_unchecked(0, _ORIGIN_AMBIGUOUS, 0, 0)
                    return
            index += 1
        if have_managed and saw_raw:
            self.proposal.set3_unchecked(0, _ORIGIN_AMBIGUOUS, 0, 0)
            return
        if have_managed:
            self.proposal.set3_unchecked(
                0,
                _ORIGIN_MANAGED,
                managed_root,
                managed_derived,
            )
            return
        self.proposal.set3_unchecked(
            0,
            _ORIGIN_RAW if saw_raw else _ORIGIN_UNKNOWN,
            0,
            0,
        )

    def write_proposed(self, value_id: int) -> None:
        transfer_offset = value_id * 4
        kind = self.transfers.get_unchecked(transfer_offset)
        first = self.transfers.get_unchecked(transfer_offset + 1)
        second = self.transfers.get_unchecked(transfer_offset + 2)
        if kind == _TRANSFER_COPY:
            self._write_state(first)
            return
        if kind == _TRANSFER_GEP:
            self._write_state(first)
            source_kind = self.proposal.get_unchecked(0)
            if source_kind != _ORIGIN_MANAGED:
                return
            if second == NO_OFFSET:
                self.proposal.set3_unchecked(0, _ORIGIN_AMBIGUOUS, 0, 0)
                return
            self.proposal.set3_unchecked(
                0,
                _ORIGIN_MANAGED,
                self.proposal.get_unchecked(1),
                self.proposal.get_unchecked(2) + second,
            )
            return
        if kind == _TRANSFER_JOIN:
            self._write_join_sources(first, second)
            return
        self._write_state(value_id)

    def converge(self) -> None:
        changed = True
        while changed:
            changed = False
            value_id = 0
            while value_id < self.value_count:
                transfer_kind = self.transfers.get_unchecked(value_id * 4)
                if transfer_kind == _TRANSFER_NONE:
                    value_id += 1
                    continue
                state_offset = value_id * 3
                old_kind = self.states.get_unchecked(state_offset)
                old_root = self.states.get_unchecked(state_offset + 1)
                old_derived = self.states.get_unchecked(state_offset + 2)
                if old_kind == _ORIGIN_AMBIGUOUS:
                    value_id += 1
                    continue
                self.write_proposed(value_id)
                proposed_kind = self.proposal.get_unchecked(0)
                proposed_root = self.proposal.get_unchecked(1)
                proposed_derived = self.proposal.get_unchecked(2)
                if proposed_kind == _ORIGIN_UNKNOWN:
                    value_id += 1
                    continue
                if (
                    old_kind == proposed_kind
                    and old_root == proposed_root
                    and old_derived == proposed_derived
                ):
                    value_id += 1
                    continue
                if old_kind == _ORIGIN_UNKNOWN:
                    self.set_state(
                        value_id,
                        proposed_kind,
                        proposed_root,
                        proposed_derived,
                    )
                else:
                    self.set_state(value_id, _ORIGIN_AMBIGUOUS)
                changed = True
                value_id += 1
        value_id = 0
        while value_id < self.value_count:
            if (
                self.transfers.get_unchecked(value_id * 4) != _TRANSFER_NONE
                and self.states.get_unchecked(value_id * 3) == _ORIGIN_UNKNOWN
            ):
                self.set_state(value_id, _ORIGIN_AMBIGUOUS)
            value_id += 1

    def is_tracked(self, value_id: int) -> bool:
        kind = self.states.get_unchecked(value_id * 3)
        return kind == _ORIGIN_MANAGED or kind == _ORIGIN_AMBIGUOUS

    def is_ambiguous(self, value_id: int) -> bool:
        return self.states.get_unchecked(value_id * 3) == _ORIGIN_AMBIGUOUS

    def close(self) -> None:
        self.states.close()
        self.transfers.close()
        self.transfer_ids.close()
        self.proposal.close()


class PackedReloadScratch:
    """Reusable sorted reload triples; no per-safepoint objects or dict."""

    __slots__ = ("records",)

    def __init__(self) -> None:
        self.records = CompilerIntArena()

    def clear(self) -> None:
        self.records.clear()

    def add(self, source: int, destination: int, derived: int) -> None:
        if destination == source and derived == 0:
            return
        index = 0
        count = len(self.records) // 3
        while index < count:
            existing: CompilerInt3 = self.records.get3_unchecked(index)
            if existing.second == destination:
                if existing.first != source or existing.third != derived:
                    raise BackendUnavailable(
                        "live managed SSA values share one spill offset"
                    )
                return
            index += 1
        self.records.append3(source, destination, derived)

    def sort(self) -> None:
        count = len(self.records) // 3
        index = 1
        while index < count:
            value: CompilerInt3 = self.records.get3_unchecked(index)
            cursor = index
            while cursor > 0:
                previous: CompilerInt3 = self.records.get3_unchecked(cursor - 1)
                previous_before = (
                    previous.second < value.second
                    or (
                        previous.second == value.second
                        and previous.first < value.first
                    )
                    or (
                        previous.second == value.second
                        and previous.first == value.first
                        and previous.third <= value.third
                    )
                )
                if previous_before:
                    break
                self.records.set3_unchecked(
                    cursor * 3,
                    previous.first,
                    previous.second,
                    previous.third,
                )
                cursor -= 1
            self.records.set3_unchecked(
                cursor * 3,
                value.first,
                value.second,
                value.third,
            )
            index += 1

    def close(self) -> None:
        self.records.close()


@dataclass(frozen=True)
class FunctionStackMapPlan:
    function_name: str
    function_id: int
    frame_size: int
    end_label: str
    records: tuple[PlannedSafepoint, ...]
    block_entry_labels: tuple[tuple[str, str], ...]
    instruction_suffix_labels: tuple[tuple[str, int, str], ...]
    terminator_prefix_labels: tuple[tuple[str, str, bool], ...]
    target: str
    packed_records: PackedPlannedSafepoints | None = None

    def diagnostic_records(self) -> tuple[PlannedSafepoint, ...]:
        if self.packed_records is not None:
            return self.packed_records.diagnostic_records()
        return self.records

    def block_entry_lines(self, block: ParsedBlock) -> list[str]:
        if self.packed_records is not None:
            records = self.packed_records
            lines: list[str] = []
            route_index = 0
            while route_index < len(records.entry_routes) // 2:
                route_offset = route_index * 2
                route_block_id = records.entry_routes.get_unchecked(
                    route_offset
                )
                route_record_index = records.entry_routes.get_unchecked(
                    route_offset + 1
                )
                if records.block_names[route_block_id] == block.name:
                    lines.append(records.label(route_record_index) + ":")
                route_index += 1
            return lines
        return [
            label + ":"
            for block_name, label in self.block_entry_labels
            if block_name == block.name
        ]

    def _reload_asm_lines_from(
        self, reloads: tuple[PlannedManagedReload, ...]
    ) -> list[str]:
        lines: list[str] = []
        for reload in reloads:
            if self.target == "aarch64-darwin":
                pointer_type = TypeDesc(
                    "ptr", pointee=TypeDesc("void")
                )
                lines.extend(
                    load_slot_to_reg_parts(
                        -reload.source_offset, pointer_type, "x16"
                    )
                )
                lines.extend(emit_add_offset(
                    "x16", "x16", reload.derived_offset,
                ))
                lines.extend(
                    store_reg_to_slot_parts(
                        "x16", -reload.destination_offset, pointer_type
                    )
                )
            else:
                source = f"[rbp - {-reload.source_offset}]"
                destination = f"[rbp - {-reload.destination_offset}]"
                lines.append(f"  mov r11, QWORD PTR {source}")
                if reload.derived_offset:
                    lines.append(f"  add r11, {reload.derived_offset}")
                lines.append(f"  mov QWORD PTR {destination}, r11")
        return lines

    def _reload_asm_lines_packed(
        self,
        records: PackedPlannedSafepoints,
        record_index: int,
    ) -> list[str]:
        lines: list[str] = []
        span: CompilerInt4 = records.span(record_index)
        reload_index = 0
        while reload_index < span.fourth:
            reload: CompilerInt3 = records.reload_scalar(
                record_index, reload_index
            )
            source_offset: int = reload.first
            destination_offset: int = reload.second
            derived_offset: int = reload.third
            if self.target == "aarch64-darwin":
                pointer_type = TypeDesc(
                    "ptr", pointee=TypeDesc("void")
                )
                lines.extend(
                    load_slot_to_reg_parts(-source_offset, pointer_type, "x16")
                )
                lines.extend(emit_add_offset(
                    "x16", "x16", derived_offset,
                ))
                lines.extend(
                    store_reg_to_slot_parts(
                        "x16", -destination_offset, pointer_type
                    )
                )
            else:
                source = f"[rbp - {-source_offset}]"
                destination = f"[rbp - {-destination_offset}]"
                lines.append(f"  mov r11, QWORD PTR {source}")
                if derived_offset:
                    lines.append(f"  add r11, {derived_offset}")
                lines.append(f"  mov QWORD PTR {destination}, r11")
            reload_index += 1
        return lines

    def _reload_asm_lines(self, record: PlannedSafepoint) -> list[str]:
        return self._reload_asm_lines_from(record.reloads)

    def _append_reload_span_packed(
        self,
        owner: AArch64EmissionFragments,
        fragment: CompilerInt2,
        records: PackedPlannedSafepoints,
        record_index: int,
    ) -> None:
        if self.target != "aarch64-darwin":
            raise BackendUnavailable("native reload fragments require AArch64")
        span: CompilerInt4 = records.span(record_index)
        reload_index = 0
        while reload_index < span.fourth:
            reload: CompilerInt3 = records.reload_scalars.get3_unchecked(
                span.third + reload_index
            )
            source_offset: int = reload.first
            destination_offset: int = reload.second
            derived_offset: int = reload.third
            append_load_slot_to_reg_parts(
                owner, fragment, -source_offset, False, 64, "x16",
            )
            append_add_offset(owner, fragment, "x16", "x16", derived_offset)
            append_store_reg_to_slot_parts(
                owner, fragment, "x16", -destination_offset, False, 64,
            )
            reload_index += 1

    def instruction_suffix_lines(
        self, block: ParsedBlock, instruction_index: int
    ) -> list[str]:
        if self.packed_records is not None:
            records = self.packed_records
            lines: list[str] = []
            route_index = 0
            while route_index < len(records.suffix_routes) // 3:
                route_offset = route_index * 3
                route_block_id = records.suffix_routes.get_unchecked(
                    route_offset
                )
                route_instruction_index = records.suffix_routes.get_unchecked(
                    route_offset + 1
                )
                route_record_index = records.suffix_routes.get_unchecked(
                    route_offset + 2
                )
                if (
                    records.block_names[route_block_id] == block.name
                    and route_instruction_index == instruction_index
                ):
                    lines.append(records.label(route_record_index) + ":")
                    lines.extend(
                        self._reload_asm_lines_packed(
                            records,
                            route_record_index,
                        )
                    )
                route_index += 1
            return lines
        lines: list[str] = []
        for block_name, index, label in self.instruction_suffix_labels:
            if block_name != block.name or index != instruction_index:
                continue
            lines.append(label + ":")
            if self.packed_records is not None:
                record_index = 0
                while self.packed_records.label(record_index) != label:
                    record_index += 1
                lines.extend(self._reload_asm_lines_packed(
                    self.packed_records,
                    record_index,
                ))
            else:
                record = next(
                    item for item in self.records if item.label == label
                )
                lines.extend(self._reload_asm_lines(record))
        return lines

    def append_packed_entry_lines(
        self,
        lines: list[str],
        block_id: int,
    ) -> None:
        records = self.packed_records
        if records is None:
            return
        span: CompilerInt2 = records.entry_route_spans.get2_unchecked(block_id)
        route_offset = 0
        while route_offset < span.second:
            route: CompilerInt2 = records.entry_routes.get2_unchecked(
                span.first + route_offset
            )
            lines.append(records.label(route.second) + ":")
            route_offset += 1

    def append_packed_suffix_lines(
        self,
        lines: list[str],
        block_id: int,
        instruction_index: int,
    ) -> None:
        records = self.packed_records
        if records is None:
            return
        span: CompilerInt2 = records.suffix_route_spans.get2_unchecked(block_id)
        route_offset = 0
        while route_offset < span.second:
            route_index = span.first + route_offset
            route_scalar = route_index * 3
            route_instruction = records.suffix_routes.get_unchecked(
                route_scalar + 1
            )
            route_record = records.suffix_routes.get_unchecked(route_scalar + 2)
            if route_instruction == instruction_index:
                self.append_packed_record_lines(lines, route_record)
            route_offset += 1

    def append_packed_record_lines(
        self,
        lines: list[str],
        record_index: int,
    ) -> None:
        records = self.packed_records
        if records is None:
            return
        lines.append(records.label(record_index) + ":")
        lines.extend(self._reload_asm_lines_packed(records, record_index))

    def append_packed_terminator_lines(
        self,
        lines: list[str],
        block_id: int,
    ) -> None:
        records = self.packed_records
        if records is None:
            return
        span: CompilerInt2 = records.terminator_route_spans.get2_unchecked(
            block_id
        )
        route_offset = 0
        while route_offset < span.second:
            route: CompilerInt3 = records.terminator_routes.get3_unchecked(
                span.first + route_offset
            )
            if route.third:
                lines.append("  nop")
            lines.append(records.label(route.second) + ":")
            route_offset += 1

    def append_packed_entry_span(
        self,
        owner: AArch64EmissionFragments,
        fragment: CompilerInt2,
        block_id: int,
    ) -> None:
        if self.target != "aarch64-darwin":
            raise BackendUnavailable("native stack-map fragments require AArch64")
        records = self.packed_records
        if records is None:
            return
        span: CompilerInt2 = records.entry_route_spans.get2_unchecked(block_id)
        route_offset = 0
        while route_offset < span.second:
            route: CompilerInt2 = records.entry_routes.get2_unchecked(
                span.first + route_offset
            )
            owner.append_label(fragment, records.label(route.second))
            route_offset += 1

    def append_packed_record_span(
        self,
        owner: AArch64EmissionFragments,
        fragment: CompilerInt2,
        record_index: int,
    ) -> None:
        if self.target != "aarch64-darwin":
            raise BackendUnavailable("native stack-map fragments require AArch64")
        records = self.packed_records
        if records is None:
            return
        owner.append_label(fragment, records.label(record_index))
        self._append_reload_span_packed(owner, fragment, records, record_index)

    def append_packed_terminator_span(
        self,
        owner: AArch64EmissionFragments,
        fragment: CompilerInt2,
        block_id: int,
    ) -> None:
        if self.target != "aarch64-darwin":
            raise BackendUnavailable("native stack-map fragments require AArch64")
        records = self.packed_records
        if records is None:
            return
        span: CompilerInt2 = records.terminator_route_spans.get2_unchecked(block_id)
        route_offset = 0
        while route_offset < span.second:
            route: CompilerInt3 = records.terminator_routes.get3_unchecked(
                span.first + route_offset
            )
            if route.third:
                owner.append_nop(fragment)
            owner.append_label(fragment, records.label(route.second))
            route_offset += 1

    def build_line_index(
        self,
    ) -> tuple[dict, dict, dict]:
        """One-pass per-block/per-instruction emission-line index.

        The per-call methods above scan every label on every call; over a
        72k-block generated module top that is O(instructions x labels) and
        dominates the whole emit. Emit loops build this index once per
        function; line order matches the per-call methods exactly, so the
        emitted text is byte-identical.
        """
        entry: dict[int, list[tuple[str, list[str]]]] = {}
        packed_records = self.packed_records
        if packed_records is not None:
            entry_route_index = 0
            while entry_route_index < len(packed_records.entry_routes) // 2:
                route_offset = entry_route_index * 2
                route_block_id = packed_records.entry_routes.get_unchecked(
                    route_offset
                )
                route_record_index = packed_records.entry_routes.get_unchecked(
                    route_offset + 1
                )
                _block_line_index_append(
                    entry,
                    packed_records.block_names[route_block_id],
                    packed_records.label(route_record_index) + ":",
                )
                entry_route_index += 1
            suffix: dict[int, list[tuple[str, dict[int, list[str]]]]] = {}
            suffix_route_index = 0
            while suffix_route_index < len(packed_records.suffix_routes) // 3:
                route_offset = suffix_route_index * 3
                route_block_id = packed_records.suffix_routes.get_unchecked(
                    route_offset
                )
                route_instruction_index = packed_records.suffix_routes.get_unchecked(
                    route_offset + 1
                )
                route_record_index = packed_records.suffix_routes.get_unchecked(
                    route_offset + 2
                )
                lines = [packed_records.label(route_record_index) + ":"]
                lines.extend(
                    self._reload_asm_lines_packed(
                        packed_records,
                        route_record_index,
                    )
                )
                block_name = packed_records.block_names[route_block_id]
                per_block = _block_line_index_get(suffix, block_name)
                if per_block is None:
                    per_block = {}
                    _block_line_index_set(suffix, block_name, per_block)
                if route_instruction_index in per_block:
                    per_block[route_instruction_index].extend(lines)
                else:
                    per_block[route_instruction_index] = lines
                suffix_route_index += 1
            term: dict[int, list[tuple[str, list[str]]]] = {}
            term_route_index = 0
            while (
                term_route_index
                < len(packed_records.terminator_routes) // 3
            ):
                route_offset = term_route_index * 3
                route_block_id = packed_records.terminator_routes.get_unchecked(
                    route_offset
                )
                route_record_index = packed_records.terminator_routes.get_unchecked(
                    route_offset + 1
                )
                route_needs_separator = (
                    packed_records.terminator_routes.get_unchecked(
                        route_offset + 2
                    )
                )
                if route_needs_separator:
                    _block_line_index_append(
                        term,
                        packed_records.block_names[route_block_id],
                        "  nop",
                    )
                _block_line_index_append(
                    term,
                    packed_records.block_names[route_block_id],
                    packed_records.label(route_record_index) + ":",
                )
                term_route_index += 1
            return entry, suffix, term
        for block_name, label in self.block_entry_labels:
            _block_line_index_append(entry, block_name, label + ":")
        packed_index_by_label: dict[str, int] = {}
        reloads_by_label: dict[str, tuple[PlannedManagedReload, ...]] = {}
        for item in self.records:
            reloads_by_label[item.label] = item.reloads
        suffix: dict[int, list[tuple[str, dict[int, list[str]]]]] = {}
        for block_name, index, label in self.instruction_suffix_labels:
            lines = [label + ":"]
            if packed_records is not None:
                typed_records: PackedPlannedSafepoints = packed_records
                lines.extend(self._reload_asm_lines_packed(
                    typed_records,
                    packed_index_by_label[label],
                ))
            else:
                lines.extend(self._reload_asm_lines_from(reloads_by_label[label]))
            per_block = _block_line_index_get(suffix, block_name)
            if per_block is None:
                per_block = {}
                _block_line_index_set(suffix, block_name, per_block)
            if index in per_block:
                per_block[index].extend(lines)
            else:
                per_block[index] = lines
        term: dict[int, list[tuple[str, list[str]]]] = {}
        for block_name, label, needs_separator in self.terminator_prefix_labels:
            lines = []
            if needs_separator:
                lines.append("  nop")
            lines.append(label + ":")
            for line in lines:
                _block_line_index_append(term, block_name, line)
        return entry, suffix, term

    def terminator_prefix_lines(self, block: ParsedBlock) -> list[str]:
        if self.packed_records is not None:
            records = self.packed_records
            lines: list[str] = []
            route_index = 0
            while route_index < len(records.terminator_routes) // 3:
                route_offset = route_index * 3
                route_block_id = records.terminator_routes.get_unchecked(
                    route_offset
                )
                route_record_index = records.terminator_routes.get_unchecked(
                    route_offset + 1
                )
                route_needs_separator = records.terminator_routes.get_unchecked(
                    route_offset + 2
                )
                if records.block_names[route_block_id] == block.name:
                    if route_needs_separator:
                        lines.append("  nop")
                    lines.append(records.label(route_record_index) + ":")
                route_index += 1
            return lines
        lines: list[str] = []
        for block_name, label, needs_separator in self.terminator_prefix_labels:
            if block_name != block.name:
                continue
            if needs_separator:
                lines.append("  nop")
            lines.append(label + ":")
        return lines


@dataclass(frozen=True)
class _PointerOrigin:
    base: str
    offset: int


@dataclass(frozen=True)
class _RootGroup:
    key: str
    locations: tuple[PlannedRootLocation, ...]


@dataclass(frozen=True)
class _ManagedValueOrigin:
    root_offset: int
    derived_offset: int = 0


_RAW_POINTER = "raw-pointer"
_AMBIGUOUS_POINTER = "ambiguous-managed-pointer"


def _block_line_index_get(index: dict, block_name: str):
    for existing_name, value in index.get(
        _stable_text_bucket_key(block_name), []
    ):
        if text_key_names_equal(existing_name, block_name):
            return value
    return None


def _block_line_index_set(index: dict, block_name: str, value) -> None:
    key = _stable_text_bucket_key(block_name)
    bucket = index.get(key)
    if bucket is None:
        bucket = []
        index[key] = bucket
    for offset, entry in enumerate(bucket):
        if text_key_names_equal(entry[0], block_name):
            bucket[offset] = (entry[0], value)
            return
    bucket.append((block_name, value))


def _block_line_index_append(
    index: dict, block_name: str, line: str
) -> None:
    lines = _block_line_index_get(index, block_name)
    if lines is None:
        lines = []
        _block_line_index_set(index, block_name, lines)
    lines.append(line)


def _fail(func: ParsedFunction, detail: str) -> None:
    raise BackendUnavailable(
        f"self precise stack-map analysis in {func.name!r}: {detail}"
    )


def _successors(term: ParsedInstr) -> tuple[str, ...]:
    if term.kind == "br":
        return (term.data[0],)
    if term.kind == "br_cond":
        return (term.data[1], term.data[2])
    if term.kind == "switch":
        return (
            term.data[2],
            *(target for _case_value, target in term.data[3]),
        )
    return ()


def _block_index_buckets(
    blocks: list[ParsedBlock],
) -> dict[int, list[int]]:
    buckets: dict[int, list[int]] = {}
    for index, block in enumerate(blocks):
        key = _stable_text_bucket_key(block.name)
        bucket = buckets.get(key)
        if bucket is None:
            bucket = []
            buckets[key] = bucket
        bucket.append(index)
    return buckets


def _block_index_from_buckets(
    blocks: list[ParsedBlock],
    buckets: dict[int, list[int]],
    name: str,
) -> int:
    for index in buckets.get(_stable_text_bucket_key(name), []):
        if text_key_names_equal(blocks[index].name, name):
            return index
    return -1


def _constant_gep_offset(base_type, indices) -> int | None:
    if not indices:
        return None
    first = const_int_from_value(indices[0][1])
    if first is None:
        return None
    offset = first * base_type.slot_size
    current = base_type
    for _index_type, index_value in indices[1:]:
        index = const_int_from_value(index_value)
        if index is None:
            return None
        if current.is_array:
            if current.elem is None:
                return None
            stride = _align_to(current.elem.slot_size, current.elem.align)
            offset += index * stride
            current = current.elem
        elif current.is_struct:
            offset += current.field_offset(index)
            current = current.field_type(index)
        else:
            return None
    return offset


def _constant_gep_offset_indexed(
    kernel: IndexedFunctionKernel, record_id: int
) -> int | None:
    header: CompilerInt4 = kernel.gep_header(record_id)
    span: CompilerInt4 = kernel.gep_span(record_id)
    if span.first <= 0:
        return None
    first_raw: CompilerInt2 = kernel.gep_index(header.fourth)
    first_text = (
        kernel.value_name(first_raw.second)
        if first_raw.second >= 0
        else kernel.call_texts[-first_raw.second - 1]
    )
    first = const_int_from_value(first_text)
    if first is None:
        return None
    base_span: CompilerInt4 = kernel.type_span(header.first)
    offset = first * base_span.third
    current_type_id = header.first
    index = 1
    while index < span.first:
        raw: CompilerInt2 = kernel.gep_index(header.fourth + index)
        index_text = (
            kernel.value_name(raw.second)
            if raw.second >= 0
            else kernel.call_texts[-raw.second - 1]
        )
        value = const_int_from_value(index_text)
        if value is None:
            return None
        current_header: CompilerInt4 = kernel.type_header(current_type_id)
        if current_header.first == TYPE_KIND_ARRAY:
            if current_header.fourth < 0:
                return None
            child_span: CompilerInt4 = kernel.type_span(
                current_header.fourth
            )
            stride = _align_to(child_span.third, child_span.fourth)
            offset += value * stride
            current_type_id = current_header.fourth
        elif current_header.first == TYPE_KIND_STRUCT:
            current_span: CompilerInt4 = kernel.type_span(current_type_id)
            if value < 0 or value >= current_span.second:
                return None
            field_offset = 0
            field_index = 0
            field_type_id = -1
            while field_index <= value:
                candidate_type_id = kernel.type_field_ids.get_unchecked(
                    current_span.first + field_index
                )
                candidate_span: CompilerInt4 = kernel.type_span(
                    candidate_type_id
                )
                field_offset = _align_to(
                    field_offset,
                    candidate_span.fourth,
                )
                if field_index == value:
                    field_type_id = candidate_type_id
                    break
                field_offset += candidate_span.third
                field_index += 1
            if field_type_id < 0:
                return None
            offset += field_offset
            current_type_id = field_type_id
        else:
            return None
        index += 1
    return offset


def _pointer_alias_set(
    aliases: dict[int, list[tuple[str, _PointerOrigin]]],
    name: str,
    origin: _PointerOrigin,
) -> None:
    key = _stable_text_bucket_key(name)
    bucket = aliases.get(key)
    if bucket is None:
        bucket = []
        aliases[key] = bucket
    for index, entry in enumerate(bucket):
        if text_key_names_equal(entry[0], name):
            bucket[index] = (entry[0], origin)
            return
    bucket.append((name, origin))


def _pointer_alias_get(
    aliases: dict[int, list[tuple[str, _PointerOrigin]]],
    name: str,
) -> _PointerOrigin | None:
    for existing_name, origin in aliases.get(
        _stable_text_bucket_key(name), []
    ):
        if text_key_names_equal(existing_name, name):
            return origin
    return None


def _pointer_aliases(
    func: ParsedFunction,
) -> dict[int, list[tuple[str, _PointerOrigin]]]:
    kernel = get_indexed_function_kernel(func)
    aliases: dict[int, list[tuple[str, _PointerOrigin]]] = {}
    for block_id in range(len(kernel.block_names)):
        block_fact: CompilerInt4 = kernel.block_fact(block_id)
        instruction_index = 0
        while instruction_index < block_fact.second:
            instruction_id = block_fact.first + instruction_index
            metadata: CompilerInt4 = kernel.instruction_metadata_by_id(
                instruction_id
            )
            kind_id = metadata.first
            if (
                kind_id != PARSED_INSTRUCTION_KIND_CAST
                and kind_id != PARSED_INSTRUCTION_KIND_GEP
            ):
                instruction_index += 1
                continue
            if kind_id == PARSED_INSTRUCTION_KIND_CAST:
                raw: CompilerInt4 = kernel.instruction_record(
                    metadata.second
                )
                instruction_fact: CompilerInt4 = kernel.instruction_fact_by_id(
                    instruction_id
                )
                dest_id = instruction_fact.first
                dest = kernel.value_name(dest_id)
                source = (
                    kernel.value_name(raw.third)
                    if raw.third >= 0
                    else kernel.call_texts[-raw.third - 1]
                )
                src_header: CompilerInt4 = kernel.type_header(raw.second)
                dst_header: CompilerInt4 = kernel.type_header(raw.fourth)
                if (
                    src_header.first == TYPE_KIND_PTR
                    and dst_header.first == TYPE_KIND_PTR
                ):
                    _pointer_alias_set(
                        aliases, dest, _PointerOrigin(source, 0)
                    )
            elif kind_id == PARSED_INSTRUCTION_KIND_GEP:
                record_id = kernel.instruction_payload_id_by_id(
                    instruction_id
                )
                header: CompilerInt4 = kernel.gep_header(record_id)
                span: CompilerInt4 = kernel.gep_span(record_id)
                dest = kernel.value_name(span.third)
                source = (
                    kernel.value_name(header.third)
                    if header.third >= 0
                    else kernel.call_texts[-header.third - 1]
                )
                offset = _constant_gep_offset_indexed(kernel, record_id)
                if offset is not None:
                    _pointer_alias_set(
                        aliases, dest, _PointerOrigin(source, offset)
                    )
            instruction_index += 1
    return aliases


def _resolve_pointer(
    func: ParsedFunction,
    aliases: dict[int, list[tuple[str, _PointerOrigin]]],
    value: str,
) -> _PointerOrigin:
    current = value
    offset = 0
    seen: list[str] = []
    while True:
        alias = _pointer_alias_get(aliases, current)
        if alias is None:
            break
        for seen_name in seen:
            if text_key_names_equal(seen_name, current):
                _fail(func, f"pointer alias cycle for {value!r}")
        seen.append(current)
        current = alias.base
        offset += alias.offset
    return _PointerOrigin(current, offset)


def _first_i32_initializer(global_: GlobalDef) -> int | None:
    text = global_.initializer.strip()
    direct = const_int_from_value(text)
    if direct is not None:
        return direct
    # Root maps may grow descriptor words after the signed root-count word.
    # The runtime contract reads the first i32, so accept only that explicit
    # typed prefix instead of guessing from arbitrary aggregate text.
    if text.startswith("[") or text.startswith("{"):
        marker = "i32 "
        index = text.find(marker)
        if index >= 0:
            tail = text[index + len(marker) :]
            token = tail.split(",", 1)[0].split("]", 1)[0].split("}", 1)[0]
            return const_int_from_value(token.strip())
    return None


def _frame_map_count(
    func: ParsedFunction,
    globals_by_name: dict[str, GlobalDef],
    aliases: dict[int, list[tuple[str, _PointerOrigin]]],
    value: str,
) -> tuple[int, bool]:
    origin = _resolve_pointer(func, aliases, value)
    if origin.offset != 0 or not origin.base.startswith("@"):
        _fail(func, f"frame map {value!r} is not one direct global")
    name = origin.base[1:]
    global_ = globals_by_name.get(name)
    if global_ is None:
        _fail(func, f"frame map global {name!r} is unavailable")
    count = _first_i32_initializer(global_)
    if count is None:
        _fail(func, f"frame map global {name!r} has no signed i32 count")
    if abs(count) > 0xFFFF:
        _fail(func, f"frame map global {name!r} exceeds the ABI location bound")
    return abs(count), count > 0


def _frame_map_is_registry_owned(
    func: ParsedFunction,
    aliases: dict[int, list[tuple[str, _PointerOrigin]]],
    frame_map_value: str,
) -> bool:
    """Is this frame the runtime registry's rather than this stack map's?

    Every frame map this compiler emits is an internal constant global -- see
    ``ownership_lowering._gc_one_slot_frame_map`` and its siblings, which build
    an ``ir.GlobalVariable`` and pass it directly.  A frame map computed at run
    time therefore cannot be compiler-generated: it can only come from a
    program that calls ``pcc_gc_frame_enter`` itself through ``pcc.extern``,
    with a map it allocated.  Such a frame is owned by the runtime's root
    registry, exactly like the global/heap/continuation slot arrays the group
    builder below already declines to model, and this function's machine stack
    map has nothing to say about it.

    Keying the decision on the map rather than on the slots is deliberate: it
    leaves every compiler-emitted frame on the unchanged fail-closed path, so
    a slots pointer the alias resolver cannot trace back to its alloca still
    stops the build instead of silently losing its roots.
    """

    origin = _resolve_pointer(func, aliases, frame_map_value)
    return origin.offset != 0 or not origin.base.startswith("@")


def _origin_is_registry_owned(
    func: ParsedFunction,
    origin: _PointerOrigin,
) -> bool:
    """Is this slots array outside this function's machine frame?

    ``null``, a module global and a caller-supplied argument were already
    recognized.  A slots array produced by a call inside this function -- a
    ``malloc`` through ``pcc.unsafe``, say -- is just as much heap memory and
    just as absent from the machine frame, but it used to reach the
    fail-closed "leaves without an active enter" diagnostic instead.
    """

    if origin.base == "null" or origin.base.startswith("@"):
        return True
    for arg in func.args:
        if arg.name == origin.base:
            return True
    kernel = get_indexed_function_kernel(func)
    value_id = kernel.value_id(origin.base)
    if value_id < 0:
        return True
    return kernel.alloca_offset(value_id) < 0


def _root_group(
    func: ParsedFunction,
    globals_by_name: dict[str, GlobalDef],
    aliases: dict[int, list[tuple[str, _PointerOrigin]]],
    frame_map_value: str,
    slots_value: str,
) -> _RootGroup:
    kernel = get_indexed_function_kernel(func)
    count, owned = _frame_map_count(
        func, globals_by_name, aliases, frame_map_value
    )
    origin = _resolve_pointer(func, aliases, slots_value)
    key = f"{origin.base}@{origin.offset}"
    if count == 0 or origin.base == "null":
        return _RootGroup(key, ())
    alloca_value_id = kernel.value_id(origin.base)
    alloca_offset = (
        -1
        if alloca_value_id < 0
        else kernel.alloca_offset(alloca_value_id)
    )
    if alloca_offset < 0:
        if origin.base.startswith("@") or any(
            arg.name == origin.base for arg in func.args
        ):
            # Explicit global/heap/continuation slot arrays are owned by the
            # existing registry, not by this function's machine stack map.
            return _RootGroup(key, ())
        _fail(
            func,
            f"managed slot address {slots_value!r} cannot be resolved to "
            "one stack alloca or explicit non-stack owner",
        )
    byte_count = count * POINTER_SIZE
    alloca_type_id = kernel.alloca_type_id(alloca_value_id)
    alloca_span: CompilerInt4 = kernel.type_span(alloca_type_id)
    if origin.offset < 0 or origin.offset + byte_count > alloca_span.third:
        _fail(func, f"managed slot range {slots_value!r} exceeds its alloca")
    locations: list[PlannedRootLocation] = []
    for index in range(count):
        frame_offset = -alloca_offset + origin.offset + index * POINTER_SIZE
        if (
            frame_offset >= 0
            or -frame_offset > func.frame_size
            or (-frame_offset) % POINTER_SIZE
        ):
            _fail(func, f"managed slot {slots_value!r} is outside the final frame")
        locations.append(PlannedRootLocation(frame_offset, owned))
    return _RootGroup(key, tuple(locations))


def _direct_call_parts(kind: str, data: tuple) -> tuple[str, tuple] | None:
    if kind != "call":
        return None
    (
        _dest,
        _ret_type,
        callee,
        is_indirect,
        args,
        _fixed_arg_count,
        _is_vararg,
        _arg_alignments,
    ) = data
    if is_indirect:
        return "", args
    return callee, args


def _direct_call(instr: ParsedInstr) -> tuple[str, tuple] | None:
    return _direct_call_parts(instr.kind, instr.data)


def _apply_frame_protocol_parts(
    func: ParsedFunction,
    globals_by_name: dict[str, GlobalDef],
    aliases: dict[int, list[tuple[str, _PointerOrigin]]],
    active: dict[str, _RootGroup],
    kind: str,
    data: tuple,
) -> bool:
    call = _direct_call_parts(kind, data)
    if call is None:
        return False
    callee, args = call
    if callee in _FRAME_ENTER:
        if len(args) != 2:
            _fail(func, f"{callee} has the wrong argument count")
        if _frame_map_is_registry_owned(func, aliases, args[0][1]):
            return True
        group = _root_group(
            func,
            globals_by_name,
            aliases,
            args[0][1],
            args[1][1],
        )
        slots_origin = _resolve_pointer(func, aliases, args[1][1])
        if (
            slots_origin.base == "null"
            or slots_origin.base.startswith("@")
            or any(arg.name == slots_origin.base for arg in func.args)
        ):
            # Global, heap/continuation and caller-owned slot arrays are
            # registered roots, but they are not locations in this function's
            # machine frame.  Their lifetime may deliberately cross this
            # function (module globals do), so including them in the local
            # control-flow state creates a false join mismatch on an
            # already-initialized fast path.
            return True
        if group.key in active:
            _fail(func, f"managed slot {group.key!r} is registered twice")
        active[group.key] = group
        return True
    if callee in _FRAME_LEAVE:
        if len(args) != 1:
            _fail(func, f"{callee} has the wrong argument count")
        origin = _resolve_pointer(func, aliases, args[0][1])
        if _origin_is_registry_owned(func, origin):
            return True
        key = f"{origin.base}@{origin.offset}"
        if key not in active:
            _fail(func, f"managed slot {key!r} leaves without an active enter")
        del active[key]
        return True
    return False


def _apply_frame_protocol(
    func: ParsedFunction,
    globals_by_name: dict[str, GlobalDef],
    aliases: dict[int, list[tuple[str, _PointerOrigin]]],
    active: dict[str, _RootGroup],
    instr: ParsedInstr,
) -> bool:
    return _apply_frame_protocol_parts(
        func, globals_by_name, aliases, active, instr.kind, instr.data
    )


def _apply_frame_protocol_indexed(
    func: ParsedFunction,
    globals_by_name: dict[str, GlobalDef],
    aliases: dict[int, list[tuple[str, _PointerOrigin]]],
    active: dict[str, _RootGroup],
    kernel: IndexedFunctionKernel,
    call_id: int,
) -> bool:
    header: CompilerInt4 = kernel.call_header(call_id)
    span: CompilerInt4 = kernel.call_span(call_id)
    if not header.third & CALL_FLAG_FRAME_PROTOCOL:
        return False
    callee = kernel.call_texts[header.second]
    if header.third & CALL_FLAG_FRAME_ENTER:
        if span.first != 2:
            _fail(func, f"{callee} has the wrong argument count")
        first: CompilerInt4 = kernel.call_arg(header.fourth)
        second: CompilerInt4 = kernel.call_arg(header.fourth + 1)
        frame_map_value = (
            kernel.value_name(first.second)
            if first.second >= 0
            else kernel.call_texts[first.third]
        )
        slots_value = (
            kernel.value_name(second.second)
            if second.second >= 0
            else kernel.call_texts[second.third]
        )
        if _frame_map_is_registry_owned(func, aliases, frame_map_value):
            return True
        group = _root_group(
            func,
            globals_by_name,
            aliases,
            frame_map_value,
            slots_value,
        )
        slots_origin = _resolve_pointer(func, aliases, slots_value)
        if _origin_is_registry_owned(func, slots_origin):
            return True
        if group.key in active:
            _fail(func, f"managed slot {group.key!r} is registered twice")
        active[group.key] = group
        return True
    if header.third & CALL_FLAG_FRAME_LEAVE:
        if span.first != 1:
            _fail(func, f"{callee} has the wrong argument count")
        first: CompilerInt4 = kernel.call_arg(header.fourth)
        origin = _resolve_pointer(
            func,
            aliases,
            (
                kernel.value_name(first.second)
                if first.second >= 0
                else kernel.call_texts[first.third]
            ),
        )
        if _origin_is_registry_owned(func, origin):
            return True
        key = f"{origin.base}@{origin.offset}"
        if key not in active:
            _fail(func, f"managed slot {key!r} leaves without an active enter")
        del active[key]
        return True
    return False


def _location_sort_key(location: PlannedRootLocation) -> int:
    """Single-int stand-in for the ordering ``(offset, not owned)``.

    Ordering is identical: doubling the offset leaves a gap of at least two
    between distinct offsets, so the low bit can carry the owned-first tie
    break without ever reaching the next offset.  Negative offsets are fine for
    the same reason.

    The point is the *type*: a tuple key allocates one 2-tuple per element, and
    `list.sort` calls the key once per element.  Merging ~354 roots 12186 times
    for one function meant ~4.3 million tuple allocations, every one of which
    enters the managed-pointer index under pcc1.  An int key stays in the
    tagged lane and allocates nothing.
    """
    if location.owned:
        return location.offset * 2
    return location.offset * 2 + 1


def _state(active: dict[str, _RootGroup]) -> tuple[_RootGroup, ...]:
    return tuple(active[key] for key in sorted(active))


def _locations(
    active_groups: tuple[_RootGroup, ...],
) -> tuple[PlannedRootLocation, ...]:
    locations = [
        location
        for group in active_groups
        for location in group.locations
    ]
    locations.sort(key=_location_sort_key)
    return tuple(locations)


def _block_entry_states(
    func: ParsedFunction,
    globals_by_name: dict[str, GlobalDef],
    aliases: dict[int, list[tuple[str, _PointerOrigin]]],
) -> list[tuple[_RootGroup, ...] | None]:
    kernel = get_indexed_function_kernel(func)
    block_count = len(kernel.block_names)
    if block_count == 0:
        return []
    entries: list[tuple[_RootGroup, ...] | None] = [None] * block_count
    entries[0] = ()
    queue = [0]
    queue_index = 0
    while queue_index < len(queue):
        block_index = queue[queue_index]
        queue_index += 1
        block_name = kernel.block_names[block_index]
        entry_state = entries[block_index]
        if entry_state is None:
            _fail(func, f"missing CFG entry state for {block_name!r}")
        active = None
        block_fact: CompilerInt4 = kernel.block_fact(block_index)
        instruction_index = 0
        while instruction_index < block_fact.second:
            instruction_id = block_fact.first + instruction_index
            metadata: CompilerInt4 = kernel.instruction_metadata_by_id(
                instruction_id
            )
            if metadata.first == PARSED_INSTRUCTION_KIND_CALL:
                call_header: CompilerInt4 = kernel.call_header(metadata.second)
                if call_header.third & CALL_FLAG_FRAME_PROTOCOL:
                    if active is None:
                        active = {
                            group.key: group for group in entry_state
                        }
                    _apply_frame_protocol_indexed(
                        func,
                        globals_by_name,
                        aliases,
                        active,
                        kernel,
                        metadata.second,
                    )
            instruction_index += 1
        # `_state` sorts every active key (string compares) and builds a fresh
        # tuple, per block edge -- yet in the huge shard functions thousands
        # of interior blocks contain no frame enter/leave at all, so `active`
        # is exactly the mapping `entry_state` was built from and the reused
        # tuple is value-identical by construction. Active is materialized
        # only after a frame-protocol callee is proven, so ordinary call-heavy
        # blocks do not allocate and rebuild a dictionary at all.
        outgoing = _state(active) if active is not None else entry_state
        successor_position = 0
        while successor_position < kernel.terminator_successor_count(
            block_index
        ):
            successor_index = kernel.terminator_successor_id(
                block_index,
                successor_position,
            )
            if successor_index < 0:
                _fail(func, f"unknown CFG successor id {successor_index}")
            successor = kernel.block_names[successor_index]
            previous = entries[successor_index]
            if previous is None:
                entries[successor_index] = outgoing
                queue.append(successor_index)
            elif previous != outgoing:
                _fail(
                    func,
                    f"managed root state disagrees at block join {successor!r}",
                )
            successor_position += 1
    return entries


def _registered_stack_root_offsets(
    func: ParsedFunction,
    globals_by_name: dict[str, GlobalDef],
    aliases: dict[int, list[tuple[str, _PointerOrigin]]],
) -> dict[tuple[str, int], int]:
    """Map each explicitly registered stack slot to its final FP offset."""

    kernel = get_indexed_function_kernel(func)
    roots: dict[tuple[str, int], int] = {}
    for block_id in range(len(kernel.block_names)):
        block_fact: CompilerInt4 = kernel.block_fact(block_id)
        instruction_index = 0
        while instruction_index < block_fact.second:
            instruction_id = block_fact.first + instruction_index
            metadata: CompilerInt4 = kernel.instruction_metadata_by_id(
                instruction_id
            )
            if metadata.first != PARSED_INSTRUCTION_KIND_CALL:
                instruction_index += 1
                continue
            call_id = metadata.second
            header: CompilerInt4 = kernel.call_header(call_id)
            span: CompilerInt4 = kernel.call_span(call_id)
            instruction_index += 1
            if not header.third & CALL_FLAG_FRAME_ENTER:
                continue
            callee = kernel.call_texts[header.second]
            if span.first != 2:
                _fail(func, f"{callee} has the wrong argument count")
            first: CompilerInt4 = kernel.call_arg(header.fourth)
            second: CompilerInt4 = kernel.call_arg(header.fourth + 1)
            frame_map_value = (
                kernel.value_name(first.second)
                if first.second >= 0
                else kernel.call_texts[first.third]
            )
            slots_value = (
                kernel.value_name(second.second)
                if second.second >= 0
                else kernel.call_texts[second.third]
            )
            if _frame_map_is_registry_owned(func, aliases, frame_map_value):
                continue
            count, _owned = _frame_map_count(
                func, globals_by_name, aliases, frame_map_value
            )
            group = _root_group(
                func,
                globals_by_name,
                aliases,
                frame_map_value,
                slots_value,
            )
            # Global, heap, and continuation slot arrays remain registry
            # roots.  Only an actual machine-frame location can refresh a
            # spilled SSA value after a moving-collector safepoint.
            if count == 0 or len(group.locations) == 0:
                continue
            if len(group.locations) != count:
                _fail(func, f"managed slot group {group.key!r} is incomplete")
            origin = _resolve_pointer(func, aliases, slots_value)
            for index, location in enumerate(group.locations):
                key = (origin.base, origin.offset + index * POINTER_SIZE)
                previous = roots.get(key)
                if previous is not None and previous != location.offset:
                    _fail(func, f"managed slot {key!r} has conflicting offsets")
                roots[key] = location.offset
    return roots


def _join_pointer_states(states: list):
    origins: list[_ManagedValueOrigin] = []
    saw_raw = False
    saw_known = False
    for state in states:
        if state is None:
            continue
        saw_known = True
        if state == _AMBIGUOUS_POINTER:
            return _AMBIGUOUS_POINTER
        if state == _RAW_POINTER:
            saw_raw = True
            continue
        if state not in origins:
            origins.append(state)
    if len(origins) > 1 or (origins and saw_raw):
        return _AMBIGUOUS_POINTER
    if origins:
        return origins[0]
    if saw_raw:
        return _RAW_POINTER
    if saw_known:
        return _RAW_POINTER
    return None


def _managed_value_origins(
    func: ParsedFunction,
    globals_by_name: dict[str, GlobalDef],
    aliases: dict[int, list[tuple[str, _PointerOrigin]]],
) -> tuple[dict[int, _ManagedValueOrigin], frozenset[int]]:
    """Classify root-derived pointer SSA without treating every ``ptr`` as GC."""

    kernel = get_indexed_function_kernel(func)
    registered = _registered_stack_root_offsets(
        func, globals_by_name, aliases
    )
    states: dict[int, object | None] = {}
    transfers: dict[int, tuple[str, tuple]] = {}

    for arg in func.args:
        if arg.type.is_ptr:
            value_id = kernel.value_id(arg.name)
            if value_id >= 0:
                states[value_id] = _RAW_POINTER
    for block_id in range(len(kernel.block_names)):
        block_fact: CompilerInt4 = kernel.block_fact(block_id)
        phi_fact: CompilerInt2 = kernel.block_phi_fact(block_id)
        phi_index = 0
        while phi_index < phi_fact.second:
            phi: CompilerInt4 = kernel.phi_record(phi_fact.first + phi_index)
            phi_type: CompilerInt4 = kernel.type_header(phi.second)
            if phi_type.first == TYPE_KIND_PTR:
                states[phi.first] = None
                incoming_ids: list[int] = []
                incoming_index = 0
                while incoming_index < phi.fourth:
                    incoming: CompilerInt2 = kernel.phi_incoming(
                        phi.third + incoming_index
                    )
                    incoming_ids.append(
                        incoming.first if incoming.first >= 0 else -1
                    )
                    incoming_index += 1
                transfers[phi.first] = ("join", tuple(incoming_ids))
            phi_index += 1
        instruction_index = 0
        while instruction_index < block_fact.second:
            instruction_id = block_fact.first + instruction_index
            instruction_fact: CompilerInt4 = kernel.instruction_fact_by_id(
                instruction_id
            )
            dest_id = instruction_fact.first
            if dest_id < 0:
                instruction_index += 1
                continue
            type_id = kernel.value_type_id(dest_id)
            if type_id < 0:
                instruction_index += 1
                continue
            value_type: CompilerInt4 = kernel.type_header(type_id)
            if value_type.first != TYPE_KIND_PTR:
                instruction_index += 1
                continue
            metadata: CompilerInt4 = kernel.instruction_metadata_by_id(
                instruction_id
            )
            kind_id = metadata.first
            payload_id = metadata.second
            if kind_id not in (
                PARSED_INSTRUCTION_KIND_LOAD,
                PARSED_INSTRUCTION_KIND_LOAD_ATOMIC,
                PARSED_INSTRUCTION_KIND_CAST,
                PARSED_INSTRUCTION_KIND_FREEZE,
                PARSED_INSTRUCTION_KIND_GEP,
                PARSED_INSTRUCTION_KIND_SELECT,
            ):
                states[dest_id] = _RAW_POINTER
                instruction_index += 1
                continue
            if kind_id == PARSED_INSTRUCTION_KIND_LOAD:
                load_record: CompilerInt4 = kernel.instruction_record(
                    payload_id
                )
                pointer_value = (
                    kernel.value_name(load_record.third)
                    if load_record.third >= 0
                    else kernel.call_texts[-load_record.third - 1]
                )
                pointer = _resolve_pointer(func, aliases, pointer_value)
                root_offset = registered.get((pointer.base, pointer.offset))
                if root_offset is None:
                    states[dest_id] = _RAW_POINTER
                else:
                    states[dest_id] = _ManagedValueOrigin(root_offset)
                instruction_index += 1
                continue
            data = None
            if kind_id == PARSED_INSTRUCTION_KIND_LOAD_ATOMIC:
                data = kernel.instruction_data(block_id, instruction_index)
                pointer_value = data[3]
                pointer = _resolve_pointer(func, aliases, pointer_value)
                root_offset = registered.get((pointer.base, pointer.offset))
                if root_offset is None:
                    states[dest_id] = _RAW_POINTER
                else:
                    states[dest_id] = _ManagedValueOrigin(root_offset)
                instruction_index += 1
                continue
            if kind_id == PARSED_INSTRUCTION_KIND_CAST:
                cast_record: CompilerInt4 = kernel.instruction_record(
                    payload_id
                )
                source_id = cast_record.third
                cast_source_type: CompilerInt4 = kernel.type_header(
                    cast_record.second
                )
                if cast_source_type.first == TYPE_KIND_PTR:
                    states[dest_id] = None
                    transfers[dest_id] = (
                        "copy", (source_id if source_id >= 0 else -1,)
                    )
                else:
                    states[dest_id] = _RAW_POINTER
                instruction_index += 1
                continue
            if kind_id == PARSED_INSTRUCTION_KIND_FREEZE:
                data = kernel.instruction_data(block_id, instruction_index)
                states[dest_id] = None
                transfers[dest_id] = (
                    "copy", (kernel.value_id(data[2]),)
                )
                instruction_index += 1
                continue
            if kind_id == PARSED_INSTRUCTION_KIND_GEP:
                record_id = payload_id
                gep_header: CompilerInt4 = kernel.gep_header(record_id)
                states[dest_id] = None
                transfers[dest_id] = (
                    "gep",
                    (
                        gep_header.third if gep_header.third >= 0 else -1,
                        _constant_gep_offset_indexed(kernel, record_id),
                    ),
                )
                instruction_index += 1
                continue
            if kind_id == PARSED_INSTRUCTION_KIND_SELECT:
                select_record: CompilerInt4 = kernel.instruction_record(
                    payload_id
                )
                states[dest_id] = None
                transfers[dest_id] = (
                    "join",
                    (
                        select_record.third
                        if select_record.third >= 0
                        else -1,
                        select_record.fourth
                        if select_record.fourth >= 0
                        else -1,
                    ),
                )
                instruction_index += 1
                continue
            # Calls, allocas, aggregate projections, and inttoptr values have
            # no explicit root provenance.  They remain raw rather than being
            # guessed managed solely from LLVM's opaque pointer type.
            states[dest_id] = _RAW_POINTER
            instruction_index += 1

    def state_for(value_id: int):
        if value_id < 0:
            return _RAW_POINTER
        return states.get(value_id, _RAW_POINTER)

    def transferred(kind: str, data: tuple):
        if kind == "copy":
            return state_for(data[0])
        if kind == "gep":
            source, offset = data
            source_state = state_for(source)
            if source_state is None:
                return None
            if source_state == _AMBIGUOUS_POINTER:
                return _AMBIGUOUS_POINTER
            if source_state == _RAW_POINTER:
                return _RAW_POINTER
            if offset is None:
                return _AMBIGUOUS_POINTER
            return _ManagedValueOrigin(
                source_state.root_offset,
                source_state.derived_offset + offset,
            )
        return _join_pointer_states([state_for(value_id) for value_id in data])

    def converge() -> None:
        changed = True
        while changed:
            changed = False
            for value_id, (kind, data) in transfers.items():
                old = states[value_id]
                if old == _AMBIGUOUS_POINTER:
                    continue
                proposed = transferred(kind, data)
                if proposed is None or proposed == old:
                    continue
                if old is None:
                    states[value_id] = proposed
                else:
                    # The lattice is monotonic: once two incompatible raw or
                    # managed explanations reach a join, never pick one based
                    # on traversal order.
                    states[value_id] = _AMBIGUOUS_POINTER
                changed = True

    converge()
    for value_id, state in tuple(states.items()):
        if state is None:
            states[value_id] = _AMBIGUOUS_POINTER
    converge()
    origins = {
        value_id: state
        for value_id, state in states.items()
        if isinstance(state, _ManagedValueOrigin)
    }
    ambiguous = frozenset(
        value_id for value_id, state in states.items()
        if state == _AMBIGUOUS_POINTER
    )
    return origins, ambiguous


def _managed_live_after(
    func: ParsedFunction,
    tracked: frozenset[int],
) -> PackedManagedLiveness:
    """Return managed/ambiguous SSA values live after each instruction.

    Indexed ``instruction_id -> state_id -> sorted value-ID span``. This was a
    ``dict[tuple[int, int], frozenset[str]]``, which cost one fresh tuple key
    per instruction here and a second fresh tuple key per safepoint in
    `build_function_stack_map_plan`, plus a tuple hash (bignum multiplies) and
    a dict probe on each.  Under pcc1 allocation count dominates, and the
    consumer's `dict.get(..., frozenset())` also built a throwaway empty
    frozenset on every call because Python evaluates that default eagerly.
    raw sparse plane: repeated slots reuse one state ID and the consumer walks
    an already-sorted integer span with no per-slot object allocation.
    """

    kernel = get_indexed_function_kernel(func)
    block_count = len(kernel.block_names)
    uses: list[set[int]] = []
    definitions: list[set[int]] = []
    for block_index in range(block_count):
        block_fact: CompilerInt4 = kernel.block_fact(block_index)
        defined: set[int] = set()
        phi_fact: CompilerInt2 = kernel.block_phi_fact(block_index)
        phi_index = 0
        while phi_index < phi_fact.second:
            phi: CompilerInt4 = kernel.phi_record(phi_fact.first + phi_index)
            if phi.first in tracked:
                defined.add(phi.first)
            phi_index += 1
        used: set[int] = set()
        instruction_index = 0
        while instruction_index < block_fact.second:
            instruction_id = block_fact.first + instruction_index
            instruction_fact: CompilerInt4 = kernel.instruction_fact_by_id(
                instruction_id
            )
            use_index = 0
            use_count = instruction_fact.second
            while use_index < use_count:
                if use_index == 0:
                    value_id = instruction_fact.third
                elif use_count == 2:
                    value_id = instruction_fact.fourth
                else:
                    overflow_start = -instruction_fact.fourth - 2
                    value_id = kernel.instruction_overflow_use_ids.get_unchecked(
                        overflow_start + use_index - 1
                    )
                if value_id in tracked and value_id not in defined:
                    used.add(value_id)
                use_index += 1
            dest_id = instruction_fact.first
            if dest_id in tracked:
                defined.add(dest_id)
            instruction_index += 1
        term_use_index = 0
        while term_use_index < block_fact.third:
            value_id = block_fact.fourth
            if value_id in tracked and value_id not in defined:
                used.add(value_id)
            term_use_index += 1
        uses.append(used)
        definitions.append(defined)

    live_in: list[set[int]] = [set() for _index in range(block_count)]
    live_out: list[set[int]] = [set() for _index in range(block_count)]
    changed = True
    while changed:
        changed = False
        block_index = block_count - 1
        while block_index >= 0:
            outgoing: set[int] = set()
            term_header: CompilerInt4 = kernel.terminator_header(block_index)
            term_span: CompilerInt4 = kernel.terminator_span(block_index)
            successor_ids: list[int] = []
            if term_header.first == PARSED_INSTRUCTION_KIND_BR:
                successor_ids.append(term_header.fourth)
            elif term_header.first == PARSED_INSTRUCTION_KIND_BR_COND:
                successor_ids.append(term_header.fourth)
                successor_ids.append(term_span.first)
            elif term_header.first == PARSED_INSTRUCTION_KIND_SWITCH:
                successor_ids.append(term_header.fourth)
                case_index = 0
                while case_index < term_span.third:
                    case: CompilerInt2 = kernel.terminator_case(
                        term_span.second + case_index
                    )
                    if case.second not in successor_ids:
                        successor_ids.append(case.second)
                    case_index += 1
            for successor_index in successor_ids:
                if successor_index < 0 or successor_index >= block_count:
                    _fail(func, f"unknown CFG successor id {successor_index}")
                outgoing.update(live_in[successor_index])
                successor_phi_fact: CompilerInt2 = kernel.block_phi_fact(
                    successor_index
                )
                phi_index = 0
                while phi_index < successor_phi_fact.second:
                    phi: CompilerInt4 = kernel.phi_record(
                        successor_phi_fact.first + phi_index
                    )
                    incoming_index = 0
                    while incoming_index < phi.fourth:
                        incoming: CompilerInt2 = kernel.phi_incoming(
                            phi.third + incoming_index
                        )
                        if (
                            incoming.second == block_index
                            and incoming.first in tracked
                        ):
                            outgoing.add(incoming.first)
                        incoming_index += 1
                    phi_index += 1
            incoming_live = uses[block_index] | (
                outgoing - definitions[block_index]
            )
            if outgoing != live_out[block_index]:
                live_out[block_index] = outgoing
                changed = True
            if incoming_live != live_in[block_index]:
                live_in[block_index] = incoming_live
                changed = True
            block_index -= 1

    result = PackedManagedLiveness()
    for block_index in range(block_count):
        block_fact: CompilerInt4 = kernel.block_fact(block_index)
        live = set(live_out[block_index])
        term_use_index = 0
        while term_use_index < block_fact.third:
            value_id = block_fact.fourth
            if value_id in tracked:
                live.add(value_id)
            term_use_index += 1
        index = block_fact.second - 1
        # `live` only changes at a def or a use of a tracked value, so long
        # runs of consecutive slots share one answer -- and 74% of measured
        # safepoints have an EMPTY live set (item 311 count), for which the
        # module-level singleton serves every slot.  Building one frozenset
        # per slot allocated a GC-registered object per instruction; reusing
        # the previous object when nothing mutated is value-identical by
        # construction.  The mutation flag is exact (set on an actual discard
        # or add), not a len() compare, which would miss a discard+add pair.
        live_state_id = result.append_state(live)
        while index >= 0:
            instruction_id = block_fact.first + index
            if live_state_id != 0:
                metadata: CompilerInt4 = kernel.instruction_metadata_by_id(
                    instruction_id
                )
                if metadata.first == PARSED_INSTRUCTION_KIND_CALL:
                    kernel.publish_call_liveness_state_id(
                        metadata.second,
                        live_state_id,
                    )
            mutated = False
            instruction_fact: CompilerInt4 = kernel.instruction_fact_by_id(
                instruction_id
            )
            dest_id = instruction_fact.first
            if dest_id in tracked and dest_id in live:
                live.discard(dest_id)
                mutated = True
            use_index = 0
            use_count = instruction_fact.second
            while use_index < use_count:
                if use_index == 0:
                    value_id = instruction_fact.third
                elif use_count == 2:
                    value_id = instruction_fact.fourth
                else:
                    overflow_start = -instruction_fact.fourth - 2
                    value_id = kernel.instruction_overflow_use_ids.get_unchecked(
                        overflow_start + use_index - 1
                    )
                if value_id in tracked and value_id not in live:
                    live.add(value_id)
                    mutated = True
                use_index += 1
            if mutated:
                live_state_id = result.append_state(live)
            index -= 1
    return result


def _planned_managed_reloads(
    func: ParsedFunction,
    kernel: IndexedFunctionKernel,
    active_offsets: set,
    live_after: PackedManagedLiveness,
    live_state_id: int,
    origins: dict[int, _ManagedValueOrigin],
    ambiguous: frozenset[int],
    target: str,
) -> tuple[PlannedManagedReload, ...]:
    # `active_offsets` is supplied by the caller rather than rebuilt here.  It
    # is a function of `active` alone, which only changes at a block boundary or
    # on a frame-protocol instruction, while this runs once per safepoint — so
    # rebuilding it here allocated a set sized by the live-root count for every
    # record.  The caller caches it on the same version counter that guards
    # `_locations`.
    if live_state_id == 0:
        return ()
    live_state: CompilerInt4 = live_after.record(live_state_id)
    reloads: list[PlannedManagedReload] = []
    destinations: dict[int, PlannedManagedReload] = {}
    live_index = 0
    while live_index < live_state.first:
        if live_index == 0:
            value_id = live_state.second
        elif live_state.first == 2:
            value_id = live_state.third
        else:
            overflow_start = -live_state.third - 2
            value_id = live_after.overflow_ids.get_unchecked(
                overflow_start + live_index - 1
            )
        live_index += 1
        name = kernel.value_name(value_id)
        if value_id in ambiguous:
            _fail(
                func,
                f"stale managed SSA value {name!r} has ambiguous root provenance",
            )
        origin = origins.get(value_id)
        if origin is None:
            continue
        if origin.root_offset not in active_offsets:
            # The bare form of this message named the value and nothing else,
            # which is unactionable: it does not say which root the value
            # wanted, which roots were live, or how many roots the safepoint
            # had.  Every one of those is already in hand here.
            live_names = []
            diagnostic_index = 0
            while diagnostic_index < live_state.first:
                if diagnostic_index == 0:
                    diagnostic_value_id = live_state.second
                elif live_state.first == 2:
                    diagnostic_value_id = live_state.third
                else:
                    diagnostic_overflow_start = -live_state.third - 2
                    diagnostic_value_id = live_after.overflow_ids.get_unchecked(
                        diagnostic_overflow_start + diagnostic_index - 1
                    )
                live_names.append(
                    kernel.value_name(diagnostic_value_id)
                )
                diagnostic_index += 1
            _fail(
                func,
                f"stale managed SSA value {name!r} outlives its active root: "
                f"wants root_offset={origin.root_offset}, "
                f"active_offsets={sorted(active_offsets)}, "
                "live_values=" + repr(live_names),
            )
        slot_id = kernel.value_slot_id(value_id)
        slot_offset = -1 if slot_id < 0 else kernel.slot_offset(slot_id)
        slot_type_id = -1 if slot_id < 0 else kernel.slot_type_id(slot_id)
        if slot_offset < 0 or slot_type_id < 0:
            _fail(func, f"managed SSA value {name!r} has no pointer spill slot")
        slot_type: CompilerInt4 = kernel.type_header(slot_type_id)
        if slot_type.first != TYPE_KIND_PTR:
            _fail(func, f"managed SSA value {name!r} has no pointer spill slot")
        destination = -slot_offset
        if (
            destination >= 0
            or -destination > func.frame_size
            or (-destination) % POINTER_SIZE
        ):
            _fail(func, f"managed SSA value {name!r} has an invalid spill slot")
        if target == "x86_64-linux" and not (
            -(1 << 31) <= origin.derived_offset < (1 << 31)
        ):
            _fail(func, f"derived managed SSA value {name!r} exceeds x86 imm32")
        # Positional in declaration order: same reason as the PlannedSafepoint
        # construction below — this is on the per-call-site reload path.
        reload = PlannedManagedReload(
            origin.root_offset,
            destination,
            origin.derived_offset,
        )
        if destination == origin.root_offset and origin.derived_offset == 0:
            continue
        previous = destinations.get(destination)
        if previous is not None and previous != reload:
            _fail(func, f"live managed SSA values share spill offset {destination}")
        if previous is None:
            destinations[destination] = reload
            reloads.append(reload)
    reloads.sort(key=lambda item: (
        item.destination_offset, item.source_offset, item.derived_offset,
    ))
    return tuple(reloads)


def _exception_successor(
    block: ParsedBlock, instruction_index: int, call_dest: str | None
) -> str:
    if call_dest is None or block.terminator is None or block.terminator.kind != "br_cond":
        return ""
    branch_value, true_target, false_target = block.terminator.data
    for instr in block.instructions[instruction_index + 1 :]:
        if instr.kind != "icmp":
            continue
        condition, dest, _value_type, lhs, rhs = instr.data
        if dest != branch_value:
            continue
        if lhs == call_dest and const_int_from_value(rhs) == 0:
            error_when_true = condition in ("ne", "ugt", "sgt")
            no_error_when_true = condition in ("eq", "ule", "sle")
        elif rhs == call_dest and const_int_from_value(lhs) == 0:
            error_when_true = condition in ("ne", "ult", "slt")
            no_error_when_true = condition in ("eq", "uge", "sge")
        else:
            continue
        if error_when_true:
            return true_target
        if no_error_when_true:
            return false_target
    return ""


def _exception_successor_indexed(
    func: ParsedFunction,
    block_id: int,
    instruction_index: int,
    call_dest: str | None,
) -> str:
    kernel = get_indexed_function_kernel(func)
    if call_dest is None:
        return ""
    call_dest_id = kernel.value_id(call_dest)
    edge_span: CompilerInt2 = kernel.inline_error_edge_span(block_id)
    edge_offset = 0
    while edge_offset < edge_span.second:
        edge_id = edge_span.first + edge_offset
        compare_index = kernel.inline_error_edge_trigger(edge_id)
        if compare_index <= instruction_index:
            edge_offset += 1
            continue
        if (
            kernel.instruction_kind_id(block_id, compare_index)
            != PARSED_INSTRUCTION_KIND_ICMP
        ):
            edge_offset += 1
            continue
        compare_record: CompilerInt4 = kernel.instruction_record(
            kernel.instruction_record_id(block_id, compare_index)
        )
        if (
            kernel.defined_value_id(block_id, compare_index)
            != kernel.inline_error_edge_condition(edge_id)
        ):
            edge_offset += 1
            continue
        condition = kernel.call_texts[compare_record.first]
        lhs_id = compare_record.third
        rhs_id = compare_record.fourth
        lhs = (
            kernel.value_name(lhs_id)
            if lhs_id >= 0
            else kernel.call_texts[-lhs_id - 1]
        )
        rhs = (
            kernel.value_name(rhs_id)
            if rhs_id >= 0
            else kernel.call_texts[-rhs_id - 1]
        )
        error_when_true = False
        if lhs_id == call_dest_id and const_int_from_value(rhs) == 0:
            error_when_true = condition in ("ne", "ugt", "sgt")
        elif rhs_id == call_dest_id and const_int_from_value(lhs) == 0:
            error_when_true = condition in ("ne", "ult", "slt")
        if error_when_true:
            return kernel.block_names[kernel.inline_error_edge_target(edge_id)]
        edge_offset += 1

    term_header: CompilerInt4 = kernel.terminator_header(block_id)
    term_span: CompilerInt4 = kernel.terminator_span(block_id)
    if term_header.first != PARSED_INSTRUCTION_KIND_BR_COND:
        return ""
    branch_value_id = term_header.third
    true_target = kernel.block_names[term_header.fourth]
    false_target = kernel.block_names[term_span.first]
    index = instruction_index + 1
    while index < kernel.instruction_count(block_id):
        kind_id = kernel.instruction_kind_id(block_id, index)
        if kind_id != PARSED_INSTRUCTION_KIND_ICMP:
            index += 1
            continue
        compare_record: CompilerInt4 = kernel.instruction_record(
            kernel.instruction_record_id(block_id, index)
        )
        dest_id = kernel.defined_value_id(block_id, index)
        if dest_id != branch_value_id:
            index += 1
            continue
        condition = kernel.call_texts[compare_record.first]
        lhs_id = compare_record.third
        rhs_id = compare_record.fourth
        lhs = (
            kernel.value_name(lhs_id)
            if lhs_id >= 0
            else kernel.call_texts[-lhs_id - 1]
        )
        rhs = (
            kernel.value_name(rhs_id)
            if rhs_id >= 0
            else kernel.call_texts[-rhs_id - 1]
        )
        if lhs_id == call_dest_id and const_int_from_value(rhs) == 0:
            error_when_true = condition in ("ne", "ugt", "sgt")
            no_error_when_true = condition in ("eq", "ule", "sle")
        elif rhs_id == call_dest_id and const_int_from_value(lhs) == 0:
            error_when_true = condition in ("ne", "ult", "slt")
            no_error_when_true = condition in ("eq", "uge", "sge")
        else:
            index += 1
            continue
        if error_when_true:
            return true_target
        if no_error_when_true:
            return false_target
        index += 1
    return ""


def _record_kind(
    block: ParsedBlock, instruction_index: int, instr: ParsedInstr
) -> tuple[int, str, int, int] | None:
    call = _direct_call(instr)
    if call is None:
        return None
    callee, _args = call
    if callee in _FRAME_PROTOCOL or callee.startswith("llvm."):
        return None
    dest = instr.data[0]
    if callee == "py_err_occurred":
        exceptional = _exception_successor(block, instruction_index, dest)
        return (
            SAFEPOINT_EXCEPTION,
            exceptional,
            RECORD_HAS_EXCEPTION_EDGE if exceptional else 0,
            0,
        )
    if "continuation" in callee or "__gen_resume" in callee or "__vthread_resume" in callee:
        continuation = scoped_stable_id(
            "continuation", callee or "indirect"
        ) & 0xFFFFFFFF
        return (
            SAFEPOINT_CONTINUATION,
            "",
            RECORD_SUSPENDED,
            continuation or 1,
        )
    if callee in ("pcc_thread_safepoint", "pcc_gc_safepoint"):
        return SAFEPOINT_LOOP, "", 0, 0
    return SAFEPOINT_CALL, "", 0, 0


def _record_kind_indexed(
    func: ParsedFunction,
    block_id: int,
    instruction_index: int,
    kernel: IndexedFunctionKernel,
    call_id: int,
) -> tuple[int, str, int, int, int] | None:
    header: CompilerInt4 = kernel.call_header(call_id)
    span: CompilerInt4 = kernel.call_span(call_id)
    if header.third & CALL_FLAG_STACKMAP_SKIP:
        return None
    if header.third & CALL_FLAG_EXCEPTION_POLL:
        dest = None if span.third < 0 else kernel.value_name(span.third)
        exceptional = _exception_successor_indexed(
            func, block_id, instruction_index, dest
        )
        return (
            SAFEPOINT_EXCEPTION,
            exceptional,
            RECORD_HAS_EXCEPTION_EDGE if exceptional else 0,
            0,
            span.fourth,
        )
    if header.third & CALL_FLAG_CONTINUATION:
        callee = kernel.call_texts[header.second]
        continuation = scoped_stable_id(
            "continuation", callee or "indirect"
        ) & 0xFFFFFFFF
        return (
            SAFEPOINT_CONTINUATION,
            "",
            RECORD_SUSPENDED,
            continuation or 1,
            span.fourth,
        )
    if header.third & CALL_FLAG_LOOP_SAFEPOINT:
        return SAFEPOINT_LOOP, "", 0, 0, span.fourth
    return SAFEPOINT_CALL, "", 0, 0, span.fourth


def _local_label(function_value: int, ordinal: int, target: str) -> str:
    # `target` is deliberately positional, not keyword-only.  A call that
    # must pass a keyword goes through the generic `py_func_call_kwargs`
    # path (build a kwargs dict, resolve each name against the signature),
    # and this is called once per safepoint from `add_record`, which is
    # ~98% of emitting an oversized module.
    prefix = "L_pcc_smap" if target == "aarch64-darwin" else ".Lpcc_smap"
    return f"{prefix}_{function_value:016x}_{ordinal}"


def _native_ref_text(kernel: IndexedFunctionKernel, value_ref: int) -> str:
    if value_ref >= 0:
        return kernel.value_name(value_ref)
    return kernel.call_texts[-value_ref - 1]


def _native_call_arg_ref(raw: CompilerInt4) -> int:
    if raw.second >= 0:
        return raw.second
    return -raw.third - 1


def _native_pointer_aliases(
    kernel: IndexedFunctionKernel,
) -> PackedPointerAliases:
    aliases = PackedPointerAliases(len(kernel.value_names))
    block_id = 0
    while block_id < len(kernel.block_names):
        block: CompilerInt4 = kernel.block_fact(block_id)
        instruction_index = 0
        while instruction_index < block.second:
            instruction_id = block.first + instruction_index
            metadata: CompilerInt4 = kernel.instruction_metadata_by_id(
                instruction_id
            )
            kind_id = metadata.first
            if kind_id == PARSED_INSTRUCTION_KIND_CAST:
                raw: CompilerInt4 = kernel.instruction_record(metadata.second)
                fact: CompilerInt4 = kernel.instruction_fact_by_id(instruction_id)
                src_type: CompilerInt4 = kernel.type_header(raw.second)
                dst_type: CompilerInt4 = kernel.type_header(raw.fourth)
                if (
                    fact.first >= 0
                    and src_type.first == TYPE_KIND_PTR
                    and dst_type.first == TYPE_KIND_PTR
                ):
                    aliases.set_alias(fact.first, raw.third, 0)
            elif kind_id == PARSED_INSTRUCTION_KIND_GEP:
                header: CompilerInt4 = kernel.gep_header(metadata.second)
                span: CompilerInt4 = kernel.gep_span(metadata.second)
                offset = _constant_gep_offset_indexed(kernel, metadata.second)
                if span.third >= 0 and offset is not None:
                    aliases.set_alias(span.third, header.third, offset)
            instruction_index += 1
        block_id += 1
    return aliases


def _native_ref_is_nonstack(
    kernel: IndexedFunctionKernel,
    base_ref: int,
) -> bool:
    if base_ref < 0:
        text = _native_ref_text(kernel, base_ref)
        return text == "null" or text.startswith("@")
    if base_ref >= len(kernel.value_names):
        return True
    value_header: CompilerInt4 = kernel.value_header(base_ref)
    return value_header.first == -2


def _native_ref_is_registry_owned(
    kernel: IndexedFunctionKernel,
    base_ref: int,
) -> bool:
    """``_origin_is_registry_owned`` over the packed value plane."""

    if _native_ref_is_nonstack(kernel, base_ref):
        return True
    if base_ref < 0 or base_ref >= len(kernel.value_names):
        return True
    return kernel.alloca_offset(base_ref) < 0


def _native_frame_map_is_registry_owned(
    aliases: PackedPointerAliases,
    kernel: IndexedFunctionKernel,
    value_ref: int,
) -> bool:
    """``_frame_map_is_registry_owned`` over the packed value plane."""

    aliases.resolve(value_ref)
    origin_base = aliases.result.get_unchecked(0)
    origin_offset = aliases.result.get_unchecked(1)
    text = _native_ref_text(kernel, origin_base)
    return origin_offset != 0 or not text.startswith("@")


def _native_frame_map_count(
    func: ParsedFunction,
    globals_by_name: dict[str, GlobalDef],
    aliases: PackedPointerAliases,
    kernel: IndexedFunctionKernel,
    value_ref: int,
) -> int:
    aliases.resolve(value_ref)
    origin_base = aliases.result.get_unchecked(0)
    origin_offset = aliases.result.get_unchecked(1)
    text = _native_ref_text(kernel, origin_base)
    if origin_offset != 0 or not text.startswith("@"):
        _fail(func, f"frame map {text!r} is not one direct global")
    name = text[1:]
    global_ = globals_by_name.get(name)
    if global_ is None:
        _fail(func, f"frame map global {name!r} is unavailable")
    count = _first_i32_initializer(global_)
    if count is None:
        _fail(func, f"frame map global {name!r} has no signed i32 count")
    if abs(count) > 0xFFFF:
        _fail(func, f"frame map global {name!r} exceeds the ABI location bound")
    return count


def _native_frame_group_id(
    func: ParsedFunction,
    globals_by_name: dict[str, GlobalDef],
    aliases: PackedPointerAliases,
    roots: PackedRootStatePlane,
    kernel: IndexedFunctionKernel,
    frame_map_ref: int,
    slots_ref: int,
) -> int:
    signed_count = _native_frame_map_count(
        func,
        globals_by_name,
        aliases,
        kernel,
        frame_map_ref,
    )
    aliases.resolve(slots_ref)
    origin_base = aliases.result.get_unchecked(0)
    origin_offset = aliases.result.get_unchecked(1)
    if _native_ref_is_nonstack(kernel, origin_base):
        return -1
    alloca_offset = kernel.alloca_offset(origin_base)
    if alloca_offset < 0:
        _fail(
            func,
            "managed slot address cannot be resolved to one stack alloca",
        )
    count = abs(signed_count)
    byte_count = count * POINTER_SIZE
    alloca_type_id = kernel.alloca_type_id(origin_base)
    alloca_span: CompilerInt4 = kernel.type_span(alloca_type_id)
    if (
        origin_offset < 0
        or origin_offset + byte_count > alloca_span.third
    ):
        _fail(func, "managed slot range exceeds its alloca")
    return roots.intern_group(
        origin_base,
        origin_offset,
        count,
        signed_count > 0,
        alloca_offset,
        func.frame_size,
    )


def _native_protocol_transition(
    func: ParsedFunction,
    globals_by_name: dict[str, GlobalDef],
    aliases: PackedPointerAliases,
    roots: PackedRootStatePlane,
    kernel: IndexedFunctionKernel,
    state_id: int,
    call_id: int,
    block_name: str = "",
) -> int:
    header: CompilerInt4 = kernel.call_header(call_id)
    span: CompilerInt4 = kernel.call_span(call_id)
    callee = kernel.call_texts[header.second]
    if header.third & CALL_FLAG_FRAME_ENTER:
        if span.first != 2:
            _fail(func, f"{callee} has the wrong argument count")
        first: CompilerInt4 = kernel.call_arg(header.fourth)
        second: CompilerInt4 = kernel.call_arg(header.fourth + 1)
        if _native_frame_map_is_registry_owned(
            aliases,
            kernel,
            _native_call_arg_ref(first),
        ):
            return state_id
        group_id = _native_frame_group_id(
            func,
            globals_by_name,
            aliases,
            roots,
            kernel,
            _native_call_arg_ref(first),
            _native_call_arg_ref(second),
        )
        if group_id < 0:
            return state_id
        try:
            return roots.transition(state_id, group_id, True)
        except BackendUnavailable as exc:
            _fail(func, f"managed slot is registered twice: {exc}")
    if header.third & CALL_FLAG_FRAME_LEAVE:
        if span.first != 1:
            _fail(func, f"{callee} has the wrong argument count")
        first: CompilerInt4 = kernel.call_arg(header.fourth)
        aliases.resolve(_native_call_arg_ref(first))
        origin_base = aliases.result.get_unchecked(0)
        origin_offset = aliases.result.get_unchecked(1)
        if _native_ref_is_registry_owned(kernel, origin_base):
            return state_id
        group_id = roots.group_id(origin_base, origin_offset)
        leave_context = (
            " in block "
            + repr(block_name)
            + " via "
            + callee
            + " on "
            + repr(kernel.value_name(origin_base))
            + "+"
            + str(origin_offset)
        )
        if group_id < 0:
            _fail(
                func,
                "managed slot leaves without an active enter" + leave_context,
            )
        try:
            return roots.transition(state_id, group_id, False)
        except BackendUnavailable as exc:
            _fail(
                func,
                "managed slot leaves without an active enter"
                + leave_context
                + ": "
                + str(exc),
            )
    return state_id


def _native_root_states(
    func: ParsedFunction,
    globals_by_name: dict[str, GlobalDef],
    aliases: PackedPointerAliases,
    kernel: IndexedFunctionKernel,
) -> PackedRootStatePlane:
    protocol_hint = 0
    block_id = 0
    while block_id < len(kernel.block_names):
        block: CompilerInt4 = kernel.block_fact(block_id)
        edge_span: CompilerInt2 = kernel.inline_error_edge_span(block_id)
        edge_offset = 0
        instruction_index = 0
        while instruction_index < block.second:
            metadata: CompilerInt4 = kernel.instruction_metadata_by_id(
                block.first + instruction_index
            )
            if metadata.first == PARSED_INSTRUCTION_KIND_CALL:
                header: CompilerInt4 = kernel.call_header(metadata.second)
                if header.third & CALL_FLAG_FRAME_PROTOCOL:
                    kernel.publish_call_root_state_id(metadata.second, -1)
                    protocol_hint += 1
            instruction_index += 1
        block_id += 1
    roots = PackedRootStatePlane(
        len(kernel.block_names),
        protocol_hint,
    )
    if not kernel.block_names:
        return roots
    roots.entry_state_ids.set_unchecked(0, 0)
    roots.queue.append(0)
    queue_index = 0
    while queue_index < len(roots.queue):
        block_id = roots.queue.get_unchecked(queue_index)
        queue_index += 1
        state_id = roots.entry_state_ids.get_unchecked(block_id)
        if state_id < 0:
            _fail(func, "missing CFG entry root state")
        block: CompilerInt4 = kernel.block_fact(block_id)
        # Per block: the edges published from this block, consumed in
        # trigger order.  Reusing the previous block's span here once left
        # every edge-only successor without an entry state.
        edge_span = kernel.inline_error_edge_span(block_id)
        edge_offset = 0
        instruction_index = 0
        while instruction_index < block.second:
            metadata: CompilerInt4 = kernel.instruction_metadata_by_id(
                block.first + instruction_index
            )
            if metadata.first == PARSED_INSTRUCTION_KIND_CALL:
                header: CompilerInt4 = kernel.call_header(metadata.second)
                if header.third & CALL_FLAG_FRAME_PROTOCOL:
                    state_id = _native_protocol_transition(
                        func,
                        globals_by_name,
                        aliases,
                        roots,
                        kernel,
                        state_id,
                        metadata.second,
                        kernel.block_names[block_id],
                    )
                    kernel.publish_call_root_state_id(
                        metadata.second,
                        state_id,
                    )
            while edge_offset < edge_span.second:
                edge_id = edge_span.first + edge_offset
                edge_trigger = kernel.inline_error_edge_trigger(edge_id)
                if edge_trigger > instruction_index:
                    break
                if edge_trigger < instruction_index:
                    _fail(func, "inline error edges are not trigger-ordered")
                successor = kernel.inline_error_edge_target(edge_id)
                previous = roots.entry_state_ids.get_unchecked(successor)
                if previous < 0:
                    roots.entry_state_ids.set_unchecked(successor, state_id)
                    roots.queue.append(successor)
                elif previous != state_id:
                    _fail(
                        func,
                        "managed root state disagrees at inline error join "
                        + repr(kernel.block_names[successor]),
                    )
                edge_offset += 1
            instruction_index += 1
        if edge_offset != edge_span.second:
            _fail(func, "inline error edge trigger exceeds source block")
        successor_position = 0
        successor_count = kernel.terminator_successor_count(block_id)
        while successor_position < successor_count:
            successor = kernel.terminator_successor_id(
                block_id,
                successor_position,
            )
            if successor < 0:
                _fail(func, f"unknown CFG successor id {successor}")
            previous = roots.entry_state_ids.get_unchecked(successor)
            if previous < 0:
                roots.entry_state_ids.set_unchecked(successor, state_id)
                roots.queue.append(successor)
            elif previous != state_id:
                _fail(
                    func,
                    "managed root state disagrees at block join "
                    + repr(kernel.block_names[successor]),
                )
            successor_position += 1
    # Every block the kernel CFG (terminators plus inline error edges) can
    # reach must own an entry state.  A block silently left without one is
    # skipped by safepoint planning, which drops its stack maps instead of
    # failing; that is how an edge-only cleanup block once lost its records.
    reachable = CompilerIntArena(len(kernel.block_names))
    reachable.append_zeros(len(kernel.block_names))
    reachable.set_unchecked(0, 1)
    pending = CompilerIntArena()
    pending.append(0)
    pending_index = 0
    while pending_index < len(pending):
        block_id = pending.get_unchecked(pending_index)
        pending_index += 1
        if roots.entry_state_ids.get_unchecked(block_id) < 0:
            _fail(
                func,
                "reachable block "
                + repr(kernel.block_names[block_id])
                + " has no entry root state",
            )
        successor_position = 0
        successor_count = kernel.cfg_successor_count(block_id)
        while successor_position < successor_count:
            successor = kernel.cfg_successor_id(block_id, successor_position)
            if reachable.get_unchecked(successor) == 0:
                reachable.set_unchecked(successor, 1)
                pending.append(successor)
            successor_position += 1
    return roots


def _native_managed_origins(
    func: ParsedFunction,
    aliases: PackedPointerAliases,
    roots: PackedRootStatePlane,
    kernel: IndexedFunctionKernel,
) -> PackedManagedOrigins:
    origins = PackedManagedOrigins(len(kernel.value_names))
    join_values = CompilerIntArena()
    for arg in func.args:
        if arg.type.is_ptr:
            value_id = kernel.value_id(arg.name)
            if value_id >= 0:
                origins.set_state(value_id, _ORIGIN_RAW)
    block_id = 0
    while block_id < len(kernel.block_names):
        block: CompilerInt4 = kernel.block_fact(block_id)
        phi_fact: CompilerInt2 = kernel.block_phi_fact(block_id)
        phi_index = 0
        while phi_index < phi_fact.second:
            phi: CompilerInt4 = kernel.phi_record(phi_fact.first + phi_index)
            phi_type: CompilerInt4 = kernel.type_header(phi.second)
            if phi_type.first == TYPE_KIND_PTR:
                join_values.clear()
                incoming_index = 0
                while incoming_index < phi.fourth:
                    incoming: CompilerInt2 = kernel.phi_incoming(
                        phi.third + incoming_index
                    )
                    join_values.append(incoming.first)
                    incoming_index += 1
                origins.set_join(phi.first, join_values)
            phi_index += 1
        instruction_index = 0
        while instruction_index < block.second:
            instruction_id = block.first + instruction_index
            fact: CompilerInt4 = kernel.instruction_fact_by_id(instruction_id)
            dest_id = fact.first
            if dest_id < 0:
                instruction_index += 1
                continue
            type_id = kernel.value_type_id(dest_id)
            if type_id < 0:
                instruction_index += 1
                continue
            value_type_header: CompilerInt4 = kernel.type_header(type_id)
            if value_type_header.first != TYPE_KIND_PTR:
                instruction_index += 1
                continue
            metadata: CompilerInt4 = kernel.instruction_metadata_by_id(
                instruction_id
            )
            kind_id = metadata.first
            payload_id = metadata.second
            if kind_id == PARSED_INSTRUCTION_KIND_LOAD:
                load_record: CompilerInt4 = kernel.instruction_record(payload_id)
                aliases.resolve(load_record.third)
                pointer_base = aliases.result.get_unchecked(0)
                pointer_offset = aliases.result.get_unchecked(1)
                root_offset = roots.registered_root_offset(
                    pointer_base,
                    pointer_offset,
                )
                if root_offset == NO_OFFSET:
                    origins.set_state(dest_id, _ORIGIN_RAW)
                else:
                    origins.set_state(
                        dest_id,
                        _ORIGIN_MANAGED,
                        root_offset,
                        0,
                    )
            elif kind_id == PARSED_INSTRUCTION_KIND_CAST:
                cast_record: CompilerInt4 = kernel.instruction_record(payload_id)
                cast_source_type: CompilerInt4 = kernel.type_header(
                    cast_record.second
                )
                if cast_source_type.first == TYPE_KIND_PTR:
                    origins.set_transfer(
                        dest_id,
                        _TRANSFER_COPY,
                        cast_record.third,
                    )
                else:
                    origins.set_state(dest_id, _ORIGIN_RAW)
            elif kind_id == PARSED_INSTRUCTION_KIND_GEP:
                header: CompilerInt4 = kernel.gep_header(payload_id)
                offset = _constant_gep_offset_indexed(kernel, payload_id)
                origins.set_transfer(
                    dest_id,
                    _TRANSFER_GEP,
                    header.third,
                    NO_OFFSET if offset is None else offset,
                )
            elif kind_id == PARSED_INSTRUCTION_KIND_SELECT:
                select_record: CompilerInt4 = kernel.instruction_record(payload_id)
                join_values.clear()
                join_values.append(select_record.third)
                join_values.append(select_record.fourth)
                origins.set_join(dest_id, join_values)
            else:
                # Cold/unsupported pointer-producing shapes stay raw; they do
                # not gain managed provenance merely from LLVM's opaque ptr.
                origins.set_state(dest_id, _ORIGIN_RAW)
            instruction_index += 1
        block_id += 1
    join_values.close()
    origins.converge()
    return origins


def _native_managed_liveness(
    func: ParsedFunction,
    origins: PackedManagedOrigins,
    kernel: IndexedFunctionKernel,
) -> PackedManagedLiveness:
    value_count = len(kernel.value_names)
    overflow_use_ids: CompilerIntArena = kernel.instruction_overflow_use_ids
    tracked_values = CompilerIntArena()
    tracked_index = CompilerIntArena(max(1, value_count))
    value_id = 0
    while value_id < value_count:
        tracked_index.append(-1)
        if origins.is_tracked(value_id):
            tracked_index.set_unchecked(value_id, len(tracked_values))
            tracked_values.append(value_id)
        value_id += 1
    word_count = (len(tracked_values) + 29) // 30
    block_count = len(kernel.block_names)
    matrix_size = block_count * word_count
    uses = CompilerIntArena(max(1, matrix_size))
    definitions = CompilerIntArena(max(1, matrix_size))
    live_in = CompilerIntArena(max(1, matrix_size))
    live_out = CompilerIntArena(max(1, matrix_size))
    uses.append_zeros(matrix_size)
    definitions.append_zeros(matrix_size)
    live_in.append_zeros(matrix_size)
    live_out.append_zeros(matrix_size)
    scratch = CompilerIntArena(max(1, word_count))
    scratch.append_zeros(word_count)

    def set_bit(arena: CompilerIntArena, row: int, tracked_id: int) -> None:
        if tracked_id < 0:
            return
        offset = row * word_count + tracked_id // 30
        arena.set_unchecked(
            offset,
            arena.get_unchecked(offset) | (1 << (tracked_id % 30)),
        )

    def has_bit(arena: CompilerIntArena, row: int, tracked_id: int) -> bool:
        if tracked_id < 0:
            return False
        offset = row * word_count + tracked_id // 30
        return bool(arena.get_unchecked(offset) & (1 << (tracked_id % 30)))

    block_id = 0
    while block_id < block_count:
        block: CompilerInt4 = kernel.block_fact(block_id)
        phi_fact: CompilerInt2 = kernel.block_phi_fact(block_id)
        phi_index = 0
        while phi_index < phi_fact.second:
            phi: CompilerInt4 = kernel.phi_record(phi_fact.first + phi_index)
            if phi.first >= 0:
                set_bit(
                    definitions,
                    block_id,
                    tracked_index.get_unchecked(phi.first),
                )
            phi_index += 1
        instruction_index = 0
        while instruction_index < block.second:
            fact: CompilerInt4 = kernel.instruction_fact_by_id(
                block.first + instruction_index
            )
            use_index = 0
            while use_index < fact.second:
                if use_index == 0:
                    used_value_id = fact.third
                elif fact.second == 2:
                    used_value_id = fact.fourth
                else:
                    overflow_start = -fact.fourth - 2
                    used_value_id = overflow_use_ids.get_unchecked(
                        overflow_start + use_index - 1
                    )
                tracked_id = (
                    -1
                    if used_value_id < 0
                    else tracked_index.get_unchecked(used_value_id)
                )
                if tracked_id >= 0 and not has_bit(
                    definitions,
                    block_id,
                    tracked_id,
                ):
                    set_bit(uses, block_id, tracked_id)
                use_index += 1
            if fact.first >= 0:
                set_bit(
                    definitions,
                    block_id,
                    tracked_index.get_unchecked(fact.first),
                )
            instruction_index += 1
        if block.third and block.fourth >= 0:
            tracked_id = tracked_index.get_unchecked(block.fourth)
            if tracked_id >= 0 and not has_bit(
                definitions,
                block_id,
                tracked_id,
            ):
                set_bit(uses, block_id, tracked_id)
        block_id += 1

    changed = True
    word_mask = (1 << 30) - 1
    while changed:
        changed = False
        block_id = block_count - 1
        while block_id >= 0:
            scratch.zero_prefix_unchecked(word_count)
            successor_position = 0
            successor_count = kernel.cfg_successor_count(block_id)
            while successor_position < successor_count:
                successor = kernel.cfg_successor_id(
                    block_id,
                    successor_position,
                )
                if successor < 0 or successor >= block_count:
                    _fail(func, f"unknown CFG successor id {successor}")
                scratch.or_prefix_from_unchecked(
                    live_in,
                    successor * word_count,
                    word_count,
                )
                phi_fact = kernel.block_phi_fact(successor)
                phi_index = 0
                while phi_index < phi_fact.second:
                    phi = kernel.phi_record(phi_fact.first + phi_index)
                    incoming_index = 0
                    while incoming_index < phi.fourth:
                        incoming = kernel.phi_incoming(
                            phi.third + incoming_index
                        )
                        if incoming.second == block_id and incoming.first >= 0:
                            tracked_id = tracked_index.get_unchecked(incoming.first)
                            if tracked_id >= 0:
                                word_offset = tracked_id // 30
                                scratch.set_unchecked(
                                    word_offset,
                                    scratch.get_unchecked(word_offset)
                                    | (1 << (tracked_id % 30)),
                                )
                        incoming_index += 1
                    phi_index += 1
                successor_position += 1
            if scratch.converge_liveness_row_unchecked(
                uses,
                definitions,
                live_out,
                live_in,
                block_id * word_count,
                word_count,
                word_mask,
            ):
                changed = True
            block_id -= 1

    result: PackedManagedLiveness = PackedManagedLiveness()
    current_live_state = CompilerIntArena(1)
    current_live_state.append(0)
    block_id = 0
    while block_id < block_count:
        block: CompilerInt4 = kernel.block_fact(block_id)
        # The block-level fixpoint above treats inline error-edge targets as
        # successors, which is exact for live-in but places every edge
        # target's live-in at the END of the source block.  A value the
        # cleanup target reads (an unpin operand) would then look live past
        # a root leave emitted after the trigger and be reported stale.  Seed
        # the backward scan from the terminator successors only and inject
        # each edge target's live-in exactly at its trigger below.
        scratch.zero_prefix_unchecked(word_count)
        successor_position = 0
        successor_count = kernel.terminator_successor_count(block_id)
        while successor_position < successor_count:
            successor = kernel.terminator_successor_id(
                block_id,
                successor_position,
            )
            scratch.or_prefix_from_unchecked(
                live_in,
                successor * word_count,
                word_count,
            )
            phi_fact = kernel.block_phi_fact(successor)
            phi_index = 0
            while phi_index < phi_fact.second:
                phi = kernel.phi_record(phi_fact.first + phi_index)
                incoming_index = 0
                while incoming_index < phi.fourth:
                    incoming = kernel.phi_incoming(phi.third + incoming_index)
                    if incoming.second == block_id and incoming.first >= 0:
                        tracked_id = tracked_index.get_unchecked(incoming.first)
                        if tracked_id >= 0:
                            word_offset = tracked_id // 30
                            scratch.set_unchecked(
                                word_offset,
                                scratch.get_unchecked(word_offset)
                                | (1 << (tracked_id % 30)),
                            )
                    incoming_index += 1
                phi_index += 1
            successor_position += 1
        if block.third and block.fourth >= 0:
            tracked_id = tracked_index.get_unchecked(block.fourth)
            if tracked_id >= 0:
                word_offset = tracked_id // 30
                scratch.set_unchecked(
                    word_offset,
                    scratch.get_unchecked(word_offset)
                    | (1 << (tracked_id % 30)),
                )
        edge_span: CompilerInt2 = kernel.inline_error_edge_span(block_id)
        edge_offset = edge_span.second - 1
        current_live_state.set_unchecked(0, 0)
        live_state_dirty = True
        instruction_index = block.second - 1
        while instruction_index >= 0:
            instruction_id = block.first + instruction_index
            metadata: CompilerInt4 = kernel.instruction_metadata_by_id(
                instruction_id
            )
            # An edge fires after its trigger instruction, so its target's
            # live-in is live after the trigger and at every earlier point.
            while edge_offset >= 0:
                edge_id = edge_span.first + edge_offset
                edge_trigger = kernel.inline_error_edge_trigger(edge_id)
                if edge_trigger < instruction_index:
                    break
                if edge_trigger == instruction_index:
                    scratch.or_prefix_from_unchecked(
                        live_in,
                        kernel.inline_error_edge_target(edge_id) * word_count,
                        word_count,
                    )
                    live_state_dirty = True
                edge_offset -= 1
            if metadata.first == PARSED_INSTRUCTION_KIND_CALL and not (
                kernel.call_flags(metadata.second) & CALL_FLAG_FRAME_PROTOCOL
            ):
                if live_state_dirty:
                    current_live_state.set_unchecked(
                        0,
                        result.append_state_words(
                            scratch,
                            tracked_values,
                            word_count,
                        ),
                    )
                    live_state_dirty = False
                if current_live_state.get_unchecked(0) != 0:
                    kernel.publish_call_liveness_state_id(
                        metadata.second,
                        current_live_state.get_unchecked(0),
                    )
            mutated = False
            fact: CompilerInt4 = kernel.instruction_fact_by_id(instruction_id)
            if fact.first >= 0:
                tracked_id = tracked_index.get_unchecked(fact.first)
                if tracked_id >= 0:
                    word_offset = tracked_id // 30
                    bit = 1 << (tracked_id % 30)
                    old_word = scratch.get_unchecked(word_offset)
                    if old_word & bit:
                        scratch.set_unchecked(word_offset, old_word & (word_mask ^ bit))
                        mutated = True
            use_index = 0
            while use_index < fact.second:
                if use_index == 0:
                    used_value_id = fact.third
                elif fact.second == 2:
                    used_value_id = fact.fourth
                else:
                    overflow_start = -fact.fourth - 2
                    used_value_id = overflow_use_ids.get_unchecked(
                        overflow_start + use_index - 1
                    )
                tracked_id = (
                    -1
                    if used_value_id < 0
                    else tracked_index.get_unchecked(used_value_id)
                )
                if tracked_id >= 0:
                    word_offset = tracked_id // 30
                    bit = 1 << (tracked_id % 30)
                    old_word = scratch.get_unchecked(word_offset)
                    if not old_word & bit:
                        scratch.set_unchecked(word_offset, old_word | bit)
                        mutated = True
                use_index += 1
            if mutated:
                live_state_dirty = True
            instruction_index -= 1
        block_id += 1

    tracked_values.close()
    tracked_index.close()
    uses.close()
    definitions.close()
    live_in.close()
    live_out.close()
    scratch.close()
    current_live_state.close()
    return result


def _native_record_kind(
    kernel: IndexedFunctionKernel,
    call_id: int,
) -> CompilerInt4:
    header: CompilerInt4 = kernel.call_header(call_id)
    span: CompilerInt4 = kernel.call_span(call_id)
    if header.third & CALL_FLAG_STACKMAP_SKIP:
        return CompilerInt4(-1, 0, 0, 0)
    if header.third & CALL_FLAG_EXCEPTION_POLL:
        return CompilerInt4(SAFEPOINT_EXCEPTION, 0, 0, span.fourth)
    if header.third & CALL_FLAG_CONTINUATION:
        callee = kernel.call_texts[header.second]
        continuation = scoped_stable_id(
            "continuation", callee or "indirect"
        ) & 0xFFFFFFFF
        return CompilerInt4(
            SAFEPOINT_CONTINUATION,
            RECORD_SUSPENDED,
            continuation or 1,
            span.fourth,
        )
    if header.third & CALL_FLAG_LOOP_SAFEPOINT:
        return CompilerInt4(SAFEPOINT_LOOP, 0, 0, span.fourth)
    return CompilerInt4(SAFEPOINT_CALL, 0, 0, span.fourth)


def _native_plan_reloads(
    func: ParsedFunction,
    kernel: IndexedFunctionKernel,
    roots: PackedRootStatePlane,
    root_state_id: int,
    live_after: PackedManagedLiveness,
    live_state_id: int,
    origins: PackedManagedOrigins,
    reloads: PackedReloadScratch,
    context: str = "",
) -> None:
    reloads.clear()
    if live_state_id == 0:
        return
    live_state: CompilerInt4 = live_after.states.get4_unchecked(live_state_id)
    live_index = 0
    while live_index < live_state.first:
        if live_index == 0:
            value_id = live_state.second
        elif live_state.first == 2:
            value_id = live_state.third
        else:
            overflow_start = -live_state.third - 2
            value_id = live_after.overflow_ids.get_unchecked(
                overflow_start + live_index - 1
            )
        live_index += 1
        name = kernel.value_name(value_id)
        origin_offset = value_id * 3
        origin_kind = origins.states.get_unchecked(origin_offset)
        root_offset = origins.states.get_unchecked(origin_offset + 1)
        derived_offset = origins.states.get_unchecked(origin_offset + 2)
        if origin_kind == _ORIGIN_AMBIGUOUS:
            _fail(
                func,
                f"stale managed SSA value {name!r} has ambiguous root provenance",
            )
        if origin_kind != _ORIGIN_MANAGED:
            continue
        if not roots.has_location_offset(root_state_id, root_offset):
            _fail(
                func,
                f"stale managed SSA value {name!r}{context} outlives root_offset="
                f"{root_offset}",
            )
        slot_id = kernel.value_slot_id(value_id)
        slot_offset = -1 if slot_id < 0 else kernel.slot_offset(slot_id)
        slot_type_id = -1 if slot_id < 0 else kernel.slot_type_id(slot_id)
        if slot_offset < 0 or slot_type_id < 0:
            _fail(func, f"managed SSA value {name!r} has no pointer spill slot")
        slot_type: CompilerInt4 = kernel.type_header(slot_type_id)
        if slot_type.first != TYPE_KIND_PTR:
            _fail(func, f"managed SSA value {name!r} has no pointer spill slot")
        destination = -slot_offset
        if (
            destination >= 0
            or -destination > func.frame_size
            or (-destination) % POINTER_SIZE
        ):
            _fail(func, f"managed SSA value {name!r} has an invalid spill slot")
        reloads.add(root_offset, destination, derived_offset)
    reloads.sort()


def _build_function_stack_map_plan_native(
    func: ParsedFunction,
    globals_: list[GlobalDef],
    identity_name: str,
) -> FunctionStackMapPlan:
    kernel = get_indexed_function_kernel(func)
    globals_by_name = {global_.name: global_ for global_ in globals_}
    aliases = _native_pointer_aliases(kernel)
    roots = _native_root_states(func, globals_by_name, aliases, kernel)
    origins = _native_managed_origins(func, aliases, roots, kernel)
    live_after = _native_managed_liveness(func, origins, kernel)
    identity = identity_name or func.name
    fid = function_id(identity)
    prefix_high = stable_id_prefix_limb("safepoint", identity, True)
    prefix_low = stable_id_prefix_limb("safepoint", identity, False)
    packed = PackedPlannedSafepoints(block_names=kernel.block_names)
    packed.adopt_root_state_locations(roots)
    reloads = PackedReloadScratch()
    ordinal = 0
    last_record_kind = -1
    block_id = 0
    while block_id < len(kernel.block_names):
        root_state_id = roots.entry_state_ids.get_unchecked(block_id)
        if root_state_id < 0:
            block_id += 1
            continue
        if block_id == 0:
            reloads.clear()
            record_id = safepoint_id_from_prefix_limbs(
                prefix_high,
                prefix_low,
                ordinal,
                SAFEPOINT_ENTRY,
            )
            label = _local_label(fid, ordinal, "aarch64-darwin")
            record_index = packed.append_native(
                record_id,
                label,
                SAFEPOINT_ENTRY,
                roots,
                root_state_id,
                0,
                "",
                0,
                reloads,
            )
            packed.add_entry_route(block_id, record_index)
            last_record_kind = SAFEPOINT_ENTRY
            ordinal += 1
        block: CompilerInt4 = kernel.block_fact(block_id)
        instruction_index = 0
        last_instruction_has_record = False
        while instruction_index < block.second:
            instruction_id = block.first + instruction_index
            metadata: CompilerInt4 = kernel.instruction_metadata_by_id(
                instruction_id
            )
            if metadata.first != PARSED_INSTRUCTION_KIND_CALL:
                last_instruction_has_record = False
                instruction_index += 1
                continue
            call_id = metadata.second
            if call_id in func.aarch64_tail_call_ids:
                # Proven sibling transfers have no return PC in this frame.
                # AArch64's planner permits this only with no root locations,
                # reloads or exceptional state. Preserve entry/other records;
                # do not invent an ordinary post-call safepoint for the jump.
                last_instruction_has_record = False
                instruction_index += 1
                continue
            call_header: CompilerInt4 = kernel.call_header(call_id)
            if call_header.third & CALL_FLAG_FRAME_PROTOCOL:
                root_state_id = kernel.call_aux_state_id(call_id)
                if root_state_id < 0:
                    _fail(func, "missing shared frame-protocol root state")
                last_instruction_has_record = False
                instruction_index += 1
                continue
            kind_info: CompilerInt4 = _native_record_kind(kernel, call_id)
            if kind_info.first < 0:
                last_instruction_has_record = False
                instruction_index += 1
                continue
            exceptional = ""
            flags = kind_info.second
            if kind_info.first == SAFEPOINT_EXCEPTION:
                call_span: CompilerInt4 = kernel.call_span(call_id)
                dest = (
                    None
                    if call_span.third < 0
                    else kernel.value_name(call_span.third)
                )
                exceptional = _exception_successor_indexed(
                    func,
                    block_id,
                    instruction_index,
                    dest,
                )
                if exceptional:
                    flags |= RECORD_HAS_EXCEPTION_EDGE
            _native_plan_reloads(
                func,
                kernel,
                roots,
                root_state_id,
                live_after,
                kind_info.fourth,
                origins,
                reloads,
                " at safepoint "
                + repr(kernel.block_names[block_id])
                + "["
                + str(instruction_index)
                + "]",
            )
            record_id = safepoint_id_from_prefix_limbs(
                prefix_high,
                prefix_low,
                ordinal,
                kind_info.first,
            )
            label = _local_label(fid, ordinal, "aarch64-darwin")
            record_index = packed.append_native(
                record_id,
                label,
                kind_info.first,
                roots,
                root_state_id,
                flags,
                exceptional,
                kind_info.third,
                reloads,
            )
            packed.add_suffix_route(block_id, instruction_index, record_index)
            last_record_kind = kind_info.first
            last_instruction_has_record = instruction_index == block.second - 1
            ordinal += 1
            instruction_index += 1
        is_backedge = False
        successor_position = 0
        successor_count = kernel.terminator_successor_count(block_id)
        while successor_position < successor_count:
            target_id = kernel.terminator_successor_id(
                block_id,
                successor_position,
            )
            if 0 <= target_id <= block_id:
                is_backedge = True
                break
            successor_position += 1
        already_loop = bool(
            last_record_kind == SAFEPOINT_LOOP
            and last_instruction_has_record
        )
        if is_backedge and not already_loop:
            reloads.clear()
            record_id = safepoint_id_from_prefix_limbs(
                prefix_high,
                prefix_low,
                ordinal,
                SAFEPOINT_LOOP,
            )
            label = _local_label(fid, ordinal, "aarch64-darwin")
            record_index = packed.append_native(
                record_id,
                label,
                SAFEPOINT_LOOP,
                roots,
                root_state_id,
                0,
                "",
                0,
                reloads,
            )
            packed.add_terminator_route(block_id, record_index, True)
            last_record_kind = SAFEPOINT_LOOP
            ordinal += 1
        block_id += 1

    packed.finish_build()
    aliases.close()
    origins.close()
    live_after.close()
    reloads.close()
    roots.close()
    return FunctionStackMapPlan(
        function_name=func.name,
        function_id=fid,
        frame_size=func.frame_size,
        end_label=f"L_pcc_smap_end_{fid:016x}",
        records=(),
        block_entry_labels=(),
        instruction_suffix_labels=(),
        terminator_prefix_labels=(),
        target="aarch64-darwin",
        packed_records=packed,
    )


def build_function_stack_map_plan(
    func: ParsedFunction,
    globals_: list[GlobalDef],
    *,
    target: str,
    identity_name: str = "",
) -> FunctionStackMapPlan:
    if target not in ("aarch64-darwin", "x86_64-linux"):
        _fail(func, f"unsupported stack-map target {target!r}")
    if target == "aarch64-darwin":
        return _build_function_stack_map_plan_native(
            func,
            globals_,
            identity_name,
        )
    kernel = get_indexed_function_kernel(func)
    globals_by_name = {global_.name: global_ for global_ in globals_}
    aliases = _pointer_aliases(func)
    entries = _block_entry_states(func, globals_by_name, aliases)
    managed_origins, ambiguous_managed = _managed_value_origins(
        func, globals_by_name, aliases
    )
    managed_names = frozenset(managed_origins) | ambiguous_managed
    live_after = _managed_live_after(func, managed_names)
    identity = identity_name or func.name
    fid = function_id(identity)
    safepoint_prefix_high = stable_id_prefix_limb(
        "safepoint", identity, True
    )
    safepoint_prefix_low = stable_id_prefix_limb(
        "safepoint", identity, False
    )
    # NOTE: do not "optimize" this by streaming the identity hash from a
    # cached prefix state (`stable_id_prefix_state` / `stable_id_resume`).
    # That is bit-identical and 40% faster on host CPython, but it was a large
    # NET LOSS under a self-compiled pcc1: resuming needs `("\0" + part)` plus
    # an `encode`, i.e. three fresh objects per safepoint, and every one of
    # them enters the global managed-pointer index.  A smoke input that builds
    # in seconds took >26 minutes at 15.6 GB, with 59% of the profile in
    # `pcc_gc_managed_pointer_find_slot`.  `safepoint_id` allocates once via
    # `"\0".join`, and under pcc1 allocation count dominates interpreter-loop
    # count.  Optimize this file against pcc1 measurements, never against host
    # CPython measurements.
    # `_locations(active_groups)` rebuilds and re-sorts the whole live-root set on
    # every safepoint, and consecutive safepoints overwhelmingly share one set
    # (the encoded stack maps measured 34x location redundancy).  `active` is
    # only ever replaced at a block boundary or mutated by
    # `_apply_frame_protocol`, and the protocol branch never emits a record,
    # so a version counter bumped at exactly those two points is enough to
    # reuse the previous tuple.  This removes both the sort and one allocation
    # per safepoint, and with them the GC index insert/probe traffic that made
    # `pcc_gc_managed_pointer_find_slot` 41% of the emit.
    active_version = 0
    cached_locations_version = -1
    cached_locations: tuple[PlannedRootLocation, ...] = ()
    # Distinct location tuples are far rarer than the states that produce
    # them: one oversized shard had 38540 records over 1446 distinct
    # tuples, and even after the version memo removes 68% of the calls,
    # 12186 merges still produced only 2465 distinct answers.
    # `_RootGroup` objects are SHARED -- `_block_entry_states` stores one
    # tuple of groups per block and the `active` dict only re-references
    # them -- so the sorted ids of the active groups are a valid
    # fingerprint costing one small int tuple, instead of flattening and
    # merge-sorting ~354 roots again.
    #
    # An id()-keyed cache is only sound while every keyed object stays
    # alive: a freed group's address can be handed to a different group,
    # and the stale fingerprint would then HIT and return a wrong
    # location tuple rather than miss.  So each entry keeps the groups it
    # was keyed on alive alongside the answer.  That is 2465 small tuples
    # for the oversized shard, against 12186 avoided merges.
    interned_locations: dict = {}
    # No.76 made ordinary blocks reuse their immutable entry-state tuple, but
    # add_record still re-walked every group to rebuild the XOR fingerprint at
    # the first safepoint of each block.  Item311 has 9,474 block entries but
    # only 854 tuple identities, so retain an identity-checked mapping from
    # the shared state object to its already validated location tuple.  The
    # stored state keeps an id() key alive; an identity mismatch is a miss and
    # falls through to the collision-checked content cache below.
    locations_by_state_identity: dict = {}
    offsets_by_state_identity: dict = {}
    cached_offsets_version = -1
    cached_active_offsets: set = set()
    records: list[PlannedSafepoint] = []
    packed_records = (
        PackedPlannedSafepoints(block_names=kernel.block_names)
        if target == "aarch64-darwin"
        else None
    )
    block_labels: list[tuple[str, str]] = []
    instruction_labels: list[tuple[str, int, str]] = []
    terminator_labels: list[tuple[str, str, bool]] = []
    ordinal = 0
    last_record_kind = -1

    def add_record(
        kind: int,
        active_groups: tuple[_RootGroup, ...],
        exceptional_block: str = "",
        flags: int = 0,
        continuation_id: int = 0,
        reloads: tuple[PlannedManagedReload, ...] = (),
    ) -> str:
        nonlocal ordinal, cached_locations_version, cached_locations
        nonlocal last_record_kind
        # Positional, in declaration order, deliberately.  A keyword call goes
        # through the generic `py_func_call_kwargs` path — build a kwargs
        # dict, then resolve every name against the signature — and this runs
        # once per safepoint.  Emitting one oversized shard spent 70% of
        # `add_record` in that path alone, and `add_record` was 98.8% of the
        # whole emit.  Keep these positional and in sync with the
        # `PlannedSafepoint` field order.
        if cached_locations_version != active_version:
            # The key is an XOR of the active groups' ids, not a sorted tuple
            # of them.  XOR is order-independent and self-inverse, which is
            # exactly the set semantics wanted here, so the sort disappears
            # with it and the key stays one integer in the tagged lane: no
            # list, no tuple, no element-wise tuple hash, no allocation, and a
            # dict probe on an int rather than on a tuple.  The tuple version
            # of this key was 27.5% of the oversized emit worker -- more than
            # the `_locations` merge it exists to avoid.
            fingerprint = 0
            for group in active_groups:
                fingerprint = fingerprint ^ id(group)
            # XOR admits collisions, so the key alone cannot carry
            # correctness.  Every entry already stores the groups it was keyed
            # on (the `id()`-keyed-cache rule: a freed key's address can be
            # reused, so the keyed objects must stay alive).  That stored
            # tuple now also does the disambiguating: verify it by identity
            # before trusting the cached locations, and treat a mismatch as a
            # miss.  A collision becomes a slow path, never a wrong answer.
            # `in` + subscript, not `.get()`: dict.get mis-lowers in the
            # self-compiled frontend, and this runs inside pcc1's own
            # backend.
            entry = None
            if fingerprint in interned_locations:
                candidate = interned_locations[fingerprint]
                keyed_groups = candidate[1]
                matched = len(keyed_groups) == len(active_groups)
                if matched:
                    index = 0
                    for group in active_groups:
                        if keyed_groups[index] is not group:
                            matched = False
                        index = index + 1
                if matched:
                    entry = candidate
            if entry is None:
                entry = (_locations(active_groups), active_groups)
                interned_locations[fingerprint] = entry
            cached_locations = entry[0]
            state_identity = id(active_groups)
            locations_by_state_identity[state_identity] = (
                active_groups,
                cached_locations,
            )
            cached_locations_version = active_version
        record_id = safepoint_id_from_prefix_limbs(
            safepoint_prefix_high,
            safepoint_prefix_low,
            ordinal,
            kind,
        )
        label = _local_label(fid, ordinal, target)
        if packed_records is not None:
            packed_records.append(
                record_id,
                label,
                kind,
                cached_locations,
                flags,
                exceptional_block,
                continuation_id,
                reloads,
            )
        else:
            records.append(PlannedSafepoint(
                record_id,
                label,
                kind,
                cached_locations,
                flags,
                exceptional_block,
                continuation_id,
                reloads,
            ))
        last_record_kind = kind
        ordinal += 1
        return label

    def active_offsets_for_version() -> set:
        nonlocal cached_offsets_version, cached_active_offsets
        if cached_offsets_version != active_version:
            cached_active_offsets = {
                location.offset
                for group in active_groups
                for location in group.locations
            }
            state_identity = id(active_groups)
            offsets_by_state_identity[state_identity] = (
                active_groups,
                cached_active_offsets,
            )
            cached_offsets_version = active_version
        return cached_active_offsets

    for block_index in range(len(kernel.block_names)):
        block_name = kernel.block_names[block_index]
        entry_state = entries[block_index]
        if entry_state is None:
            continue
        # Entry states are already immutable, canonical group tuples. Only
        # frame enter/leave mutates the mapping, and sizing on item311 found
        # that only 1,278 of 9,474 blocks execute that protocol. Keep the
        # shared tuple for ordinary blocks; materialize the string-keyed dict
        # only at the first mutation in a protocol block.
        active = None
        active_groups = entry_state
        active_groups_dirty = False
        active_version += 1
        state_identity = id(active_groups)
        state_entry = None
        if state_identity in locations_by_state_identity:
            candidate = locations_by_state_identity[state_identity]
            if candidate[0] is active_groups:
                state_entry = candidate
        if state_entry is not None:
            cached_locations = state_entry[1]
            cached_locations_version = active_version
        offset_entry = None
        if state_identity in offsets_by_state_identity:
            candidate = offsets_by_state_identity[state_identity]
            if candidate[0] is active_groups:
                offset_entry = candidate
        if offset_entry is not None:
            cached_active_offsets = offset_entry[1]
            cached_offsets_version = active_version
        if block_index == 0:
            entry_label = add_record(SAFEPOINT_ENTRY, active_groups)
            if packed_records is not None:
                packed_records.add_entry_route(
                    block_index,
                    len(packed_records) - 1,
                )
            else:
                block_labels.append((block_name, entry_label))
        last_instruction_has_record = False
        block_fact: CompilerInt4 = kernel.block_fact(block_index)
        instruction_index = 0
        instruction_count = block_fact.second
        while instruction_index < instruction_count:
            instruction_id = block_fact.first + instruction_index
            metadata: CompilerInt4 = kernel.instruction_metadata_by_id(
                instruction_id
            )
            if metadata.first != PARSED_INSTRUCTION_KIND_CALL:
                last_instruction_has_record = False
                instruction_index += 1
                continue
            call_id = metadata.second
            call_header: CompilerInt4 = kernel.call_header(call_id)
            if call_header.third & CALL_FLAG_FRAME_PROTOCOL:
                if active is None:
                    active = {
                        group.key: group for group in active_groups
                    }
                if _apply_frame_protocol_indexed(
                    func,
                    globals_by_name,
                    aliases,
                    active,
                    kernel,
                    call_id,
                ):
                    active_groups_dirty = True
                    active_version += 1
                    last_instruction_has_record = False
                    instruction_index += 1
                    continue
            kind_info = _record_kind_indexed(
                func,
                block_index,
                instruction_index,
                kernel,
                call_id,
            )
            if kind_info is None:
                last_instruction_has_record = False
                instruction_index += 1
                continue
            kind, exceptional, flags, continuation, live_state_id = kind_info
            if active_groups_dirty:
                if active is None:
                    _fail(func, "missing mutable frame-protocol state")
                active_groups = tuple(active.values())
                active_groups_dirty = False
            try:
                planned_reloads = _planned_managed_reloads(
                    func,
                    kernel,
                    active_offsets_for_version(),
                    live_after,
                    live_state_id,
                    managed_origins,
                    ambiguous_managed,
                    target,
                )
            except BackendUnavailable as exc:
                # The reload planner knows the value and the offsets but not
                # where in the CFG it is, and "which block, reached from
                # where" is the first question anyone asks.  Add it here,
                # where it is free, rather than threading indices through the
                # planner's signature.
                # A group that is registered but contributes no machine
                # location is the shape that makes this failure confusing:
                # the root IS active, it just has nowhere to be.  Diagnose
                # those here from data already in hand, rather than leaving
                # the reader to guess which of `_root_group`'s three
                # empty-locations paths fired.
                empty_groups = []
                active_group_keys = sorted(
                    group.key for group in active_groups
                )
                for group in active_groups:
                    group_key = group.key
                    if group.locations:
                        continue
                    base = group_key
                    at_index = base.rfind("@")
                    if at_index > 0:
                        base = base[:at_index]
                    slot_offset = parsed_function_alloca_slot_offset(func, base)
                    arg_escape = False
                    for arg in func.args:
                        if arg.name == base:
                            arg_escape = True
                    empty_groups.append(
                        f"{group_key}(alloca={'found' if slot_offset >= 0 else 'MISSING'},"
                        f"arg_escape={arg_escape},"
                        f"global={base.startswith('@')})"
                    )
                raise BackendUnavailable(
                    f"{exc} [at block #{block_index} {block_name!r} "
                    f"instr #{instruction_index}, "
                    f"active_groups={active_group_keys}, "
                    f"groups_without_locations={empty_groups}]"
                ) from exc
            record_label = add_record(
                kind,
                active_groups,
                exceptional,
                flags,
                continuation,
                planned_reloads,
            )
            if packed_records is not None:
                packed_records.add_suffix_route(
                    block_index,
                    instruction_index,
                    len(packed_records) - 1,
                )
            else:
                instruction_labels.append(
                    (block_name, instruction_index, record_label)
                )
            last_instruction_has_record = instruction_index == instruction_count - 1
            instruction_index += 1
        is_backedge = False
        successor_position = 0
        while successor_position < kernel.terminator_successor_count(
            block_index
        ):
            target_index = kernel.terminator_successor_id(
                block_index,
                successor_position,
            )
            if 0 <= target_index <= block_index:
                is_backedge = True
                break
            successor_position += 1
        already_loop = bool(
            last_record_kind == SAFEPOINT_LOOP
            and last_instruction_has_record
        )
        if is_backedge and not already_loop:
            if active_groups_dirty:
                if active is None:
                    _fail(func, "missing mutable frame-protocol state")
                active_groups = tuple(active.values())
                active_groups_dirty = False
            loop_label = add_record(SAFEPOINT_LOOP, active_groups)
            # Give every loop record a dedicated machine PC.  An empty
            # latch/merge can become fallthrough after target peepholes, so a
            # call record in its predecessor would otherwise alias this loop
            # label even though the two records have distinct stable IDs and
            # kinds.  The one-byte/x86 or one-instruction/AArch64 separator is
            # executable on the backedge and keeps the final map strictly
            # ordered without dropping either logical safepoint.
            needs_separator = True
            if packed_records is not None:
                packed_records.add_terminator_route(
                    block_index,
                    len(packed_records) - 1,
                    needs_separator,
                )
            else:
                terminator_labels.append(
                    (block_name, loop_label, needs_separator)
                )

    live_after.close()
    end_prefix = "L_pcc_smap_end" if target == "aarch64-darwin" else ".Lpcc_smap_end"
    if packed_records is not None:
        packed_records.finish_build()
    return FunctionStackMapPlan(
        function_name=func.name,
        function_id=fid,
        frame_size=func.frame_size,
        end_label=f"{end_prefix}_{fid:016x}",
        records=tuple(records),
        block_entry_labels=tuple(block_labels),
        instruction_suffix_labels=tuple(instruction_labels),
        terminator_prefix_labels=tuple(terminator_labels),
        target=target,
        packed_records=packed_records,
    )


def build_stack_map_plans(
    functions: list[ParsedFunction],
    globals_: list[GlobalDef],
    *,
    target: str,
    function_symbol=None,
) -> tuple[FunctionStackMapPlan, ...]:
    return tuple(
        build_function_stack_map_plan(
            func,
            globals_,
            target=target,
            identity_name=(
                function_symbol(func.name)
                if function_symbol is not None
                else func.name
            ),
        )
        for func in functions
    )


def _aarch64_text_label_offsets(lines: list[str]) -> dict[str, int]:
    offsets: dict[str, int] = {}
    offset = 0
    in_text = False
    for raw in lines:
        # Emitter-owned ordinary instructions are always two-space indented,
        # while labels are unindented and directives begin with a dot.  Avoid
        # allocating/normalizing text and testing every directive shape for
        # the overwhelmingly common fixed-width instruction line.
        if in_text and raw.startswith("  ") and not raw.startswith("  ."):
            offset += 4
            continue
        line = raw.strip()
        if line.startswith(".section "):
            in_text = line[len(".section ") :].startswith("__TEXT,__text,")
            continue
        if not in_text or not line:
            continue
        if line.endswith(":"):
            label = line[:-1]
            if label in offsets:
                raise BackendUnavailable(
                    f"duplicate target-final stack-map label {label!r}"
                )
            offsets[label] = offset
            continue
        if line.startswith(".p2align "):
            power = int(line.split()[1], 0)
            alignment = 1 << power
            offset = _align_to(offset, alignment)
            continue
        if line.startswith((".data_region", ".end_data_region")):
            continue
        widths = ((".byte ", 1), (".short ", 2), (".long ", 4), (".quad ", 8))
        matched = False
        for prefix, width in widths:
            if line.startswith(prefix):
                offset += width * len([item for item in line[len(prefix) :].split(",") if item.strip()])
                matched = True
                break
        if matched:
            continue
        if line.startswith(".space "):
            offset += int(line.split()[1], 0)
            continue
        if line.startswith("."):
            continue
        offset += 4
    return offsets


def _stack_locations(
    locations: tuple[PlannedRootLocation, ...], *, arch: int
) -> tuple[StackMapLocation, ...]:
    register = 29 if arch == ARCH_AARCH64 else 6
    materialized: list[StackMapLocation] = []
    for location in locations:
        materialized.append(StackMapLocation(
            LOCATION_STACK_INDIRECT,
            LOCATION_MANAGED | (LOCATION_OWNED if location.owned else 0),
            POINTER_SIZE,
            register,
            NO_BASE,
            location.offset,
            POINTER_SIZE,
        ))
    return tuple(materialized)


def _validate_planned_location(
    location: PlannedRootLocation, *, frame_size: int
) -> None:
    _validate_planned_location_offset(
        location.offset,
        frame_size=frame_size,
    )


def _validate_planned_location_offset(
    offset: int, *, frame_size: int
) -> None:
    if offset >= 0 or -offset > frame_size:
        raise BackendUnavailable(
            f"planned managed stack location {offset} exceeds "
            f"frame size {frame_size}"
        )
    if (-offset) % POINTER_SIZE:
        raise BackendUnavailable(
            f"planned managed stack location {offset} is not aligned"
        )


def _packed_location_words_from_fields(
    kind: int,
    flags: int,
    size: int,
    register: int,
    base_index: int,
    offset: int,
    extent: int,
) -> tuple[int, int, int, int]:
    """Pack one 16-byte stack-map location into four little-endian i32 words.

    Layout is [kind:u8][flags:u8][size:u16][reg:u16][base:u16][offset:i32]
    [extent:i32].  Writing each field on its own line made the stack-map
    section dominate self-backend output (measured: one module's .s was
    98.7% data directives, ~7 asm lines per 16-byte location record).  The
    assembler writes each .long value little-endian, so packing the record
    into four words on one directive line produces byte-identical data with
    a fraction of the lines.
    """
    word0 = kind | (flags << 8) | (size << 16)
    word1 = (register & 0xFFFF) | ((base_index & 0xFFFF) << 16)
    return word0, word1, offset, extent


_LOCATIONS_PER_LINE = 8


def _append_packed_location_lines(
    lines: list[str], locations, *, arch: int
) -> None:
    """Append packed .long lines for many locations (8 records per line).

    Accepts both PlannedRootLocation (planned path: only offset/owned, the
    remaining fields are derived per target) and the materialized
    StackMapLocation (x86_64 path: all seven fields present).
    """
    words: list[int] = []
    for location in locations:
        if hasattr(location, "owned"):
            register = 29 if arch == ARCH_AARCH64 else 6
            flags = LOCATION_MANAGED | (
                LOCATION_OWNED if location.owned else 0
            )
            words.extend(_packed_location_words_from_fields(
                LOCATION_STACK_INDIRECT,
                flags,
                POINTER_SIZE,
                register,
                NO_BASE,
                location.offset,
                POINTER_SIZE,
            ))
        else:
            words.extend(_packed_location_words_from_fields(
                location.kind,
                location.flags,
                location.size,
                location.register,
                location.base_index,
                location.offset,
                location.extent,
            ))
    per_line = _LOCATIONS_PER_LINE * 4
    for start in range(0, len(words), per_line):
        chunk = words[start:start + per_line]
        lines.append("  .long " + ", ".join(str(word) for word in chunk))


def _append_packed_aarch64_location_arenas(
    lines: list[str],
    locations: CompilerIntArena,
) -> None:
    words: list[int] = []
    location_index = 0
    while location_index < len(locations) // 2:
        location: CompilerInt2 = locations.get2_unchecked(location_index)
        flags = LOCATION_MANAGED | (
            LOCATION_OWNED if location.second != 0 else 0
        )
        words.extend(_packed_location_words_from_fields(
            LOCATION_STACK_INDIRECT,
            flags,
            POINTER_SIZE,
            29,
            NO_BASE,
            location.first,
            POINTER_SIZE,
        ))
        location_index += 1
    per_line = _LOCATIONS_PER_LINE * 4
    for start in range(0, len(words), per_line):
        chunk = words[start:start + per_line]
        lines.append("  .long " + ", ".join(str(word) for word in chunk))


def _append_planned_location(
    lines: list[str], location: PlannedRootLocation, *, arch: int
) -> None:
    """Emit one planned stack-map location as a packed single .long line."""
    register = 29 if arch == ARCH_AARCH64 else 6
    flags = LOCATION_MANAGED | (LOCATION_OWNED if location.owned else 0)
    lines.append(
        "  .long " + ", ".join(
            str(word) for word in _packed_location_words_from_fields(
                LOCATION_STACK_INDIRECT,
                flags,
                POINTER_SIZE,
                register,
                NO_BASE,
                location.offset,
                POINTER_SIZE,
            )
        )
    )


def render_aarch64_stack_map_section(
    lines: list[str],
    plans: tuple[FunctionStackMapPlan, ...],
    *,
    function_symbol,
    block_label,
) -> list[str]:
    offsets = _aarch64_text_label_offsets(lines)
    ordered_plans = sorted(plans, key=lambda item: item.function_id)
    section = [".section __DATA,__pcc_stackmaps,regular", ".p2align 3"]
    # v2 interns the location lists into one table emitted after every
    # function, so the header carries its length and records name an index.
    # Repeating locations inline made this section 89.7% of a linked pcc1.
    global_locations = CompilerIntArena()
    global_location_content_index: dict[str, int] = {}
    _append_bytes(section, MAGIC)
    section.extend((
        f"  .short {VERSION}",
        f"  .byte {ARCH_AARCH64}",
        f"  .byte {POINTER_SIZE}",
        f"  .long {len(ordered_plans)}",
        "  .long 0",  # patched below with the interned location count
        "  .long 0",
    ))
    location_count_line = len(section) - 2
    previous_function_id = -1
    seen_safepoints: set[int] = set()
    for plan in ordered_plans:
        if plan.function_id <= previous_function_id:
            raise BackendUnavailable(
                "stack-map functions need unique ordered stable ids"
            )
        previous_function_id = plan.function_id
        symbol = function_symbol(plan.function_name)
        if not symbol or "\0" in symbol:
            raise BackendUnavailable("stack-map function symbol is invalid")
        start = offsets.get(symbol)
        end = offsets.get(plan.end_label)
        if start is None or end is None or end <= start:
            raise BackendUnavailable(
                f"target-final stack-map range missing for {plan.function_name!r}"
            )
        code_size = end - start
        if plan.frame_size < 0 or plan.frame_size % 16:
            raise BackendUnavailable(
                f"stack-map frame size is invalid for {plan.function_name!r}"
            )
        maybe_packed_records = plan.packed_records
        if maybe_packed_records is None:
            raise BackendUnavailable(
                "AArch64 stack-map plan is missing packed records"
            )
        packed_records: PackedPlannedSafepoints = maybe_packed_records
        local_location_indices: dict[int, int] = {}
        records: list[tuple[int, int, int, int]] = []
        record_index = 0
        while record_index < len(packed_records):
            scalar: CompilerInt4 = packed_records.scalar(record_index)
            label = packed_records.label(record_index)
            pc = offsets.get(label)
            if pc is None:
                raise BackendUnavailable(
                    f"target-final safepoint label missing: {label!r}"
                )
            exceptional_offset = NO_OFFSET
            exceptional_block = packed_records.exceptional_block(
                record_index
            )
            if exceptional_block:
                exceptional_pc = offsets.get(
                    block_label(plan.function_name, exceptional_block)
                )
                if exceptional_pc is None:
                    raise BackendUnavailable(
                        "target-final exception successor label missing for "
                        f"{plan.function_name!r}/{exceptional_block!r}"
                    )
                exceptional_offset = exceptional_pc - start
            records.append((
                pc - start,
                scalar.first,
                record_index,
                exceptional_offset,
            ))
            record_index += 1
        records.sort(key=lambda item: (item[0], item[1]))
        section.extend((
            f"  .quad {plan.function_id}",
            f"  .quad {symbol}",
            f"  .long {code_size}",
            f"  .long {plan.frame_size}",
            f"  .long {len(records)}",
            "  .long 0",
        ))
        previous_pc = -1
        for instruction_offset, record_id, record_index, exceptional_offset in records:
            scalar: CompilerInt4 = packed_records.scalar(record_index)
            span: CompilerInt4 = packed_records.span(record_index)
            record_kind = scalar.second
            record_flags = scalar.third
            continuation_id = scalar.fourth
            if record_id <= 0 or record_id in seen_safepoints:
                raise BackendUnavailable("stack-map safepoint id is invalid or duplicate")
            seen_safepoints.add(record_id)
            if not previous_pc < instruction_offset < code_size:
                raise BackendUnavailable(
                    "stack-map safepoints are not ordered inside the function"
                )
            previous_pc = instruction_offset
            has_exception = exceptional_offset != NO_OFFSET
            if has_exception != bool(record_flags & RECORD_HAS_EXCEPTION_EDGE):
                raise BackendUnavailable(
                    "stack-map exception-edge flag and offset disagree"
                )
            is_continuation = record_kind == SAFEPOINT_CONTINUATION
            if is_continuation != bool(continuation_id):
                raise BackendUnavailable(
                    "stack-map continuation needs one non-zero continuation id"
                )
            if bool(record_flags & RECORD_SUSPENDED) != is_continuation:
                raise BackendUnavailable(
                    "stack-map suspended flag is reserved for continuations"
                )
            location_group_id = span.first
            location_group: CompilerInt2 = packed_records.location_group(
                record_index
            )
            if location_group_id in local_location_indices:
                location_index = local_location_indices[location_group_id]
            else:
                # A group is immutable and shared by every record that names
                # it. Validate its offsets once when the local group is first
                # published, not once per safepoint (item311 otherwise reread
                # 3.5M location pairs for 128k distinct pairs).
                location_offset = 0
                while location_offset < location_group.second:
                    location: CompilerInt2 = packed_records.location_scalar(
                        location_group.first + location_offset
                    )
                    _validate_planned_location_offset(
                        location.first,
                        frame_size=plan.frame_size,
                    )
                    location_offset += 1
                content_key = packed_records.location_group_key(
                    location_group_id
                )
                if content_key in global_location_content_index:
                    location_index = global_location_content_index[content_key]
                else:
                    location_index = len(global_locations) // 2
                    global_location_content_index[content_key] = location_index
                    location_offset = 0
                    while location_offset < location_group.second:
                        global_location: CompilerInt2 = packed_records.location_scalar(
                            location_group.first + location_offset
                        )
                        global_locations.append2(
                            global_location.first,
                            global_location.second,
                        )
                        location_offset += 1
                local_location_indices[location_group_id] = location_index
            section.extend((
                f"  .quad {record_id}",
                f"  .long {instruction_offset}",
                f"  .long {exceptional_offset}",
                f"  .long {continuation_id}",
                f"  .short {location_group.second}",
                "  .short 0",
                f"  .byte {record_kind}",
                f"  .byte {record_flags}",
                "  .short 0",
                f"  .long {location_index}",
            ))
    location_count = len(global_locations) // 2
    section[location_count_line] = f"  .long {location_count}"
    if location_count:
        _append_packed_aarch64_location_arenas(section, global_locations)
    global_locations.close()
    for plan in ordered_plans:
        maybe_packed_records = plan.packed_records
        assert maybe_packed_records is not None
        packed_records: PackedPlannedSafepoints = maybe_packed_records
        packed_records.close()
    return section


def _final_stack_map_record_greater(
    records: CompilerIntArena,
    left: int,
    right: int,
) -> bool:
    left_offset = left * 4
    right_offset = right * 4
    left_pc = records.get_unchecked(left_offset)
    right_pc = records.get_unchecked(right_offset)
    if left_pc != right_pc:
        return left_pc > right_pc
    return records.get_unchecked(left_offset + 1) > records.get_unchecked(
        right_offset + 1
    )


def _swap_final_stack_map_records(
    records: CompilerIntArena,
    left: int,
    right: int,
) -> None:
    if left == right:
        return
    left_offset = left * 4
    right_offset = right * 4
    left_pc = records.get_unchecked(left_offset)
    left_id = records.get_unchecked(left_offset + 1)
    left_index = records.get_unchecked(left_offset + 2)
    left_exception = records.get_unchecked(left_offset + 3)
    records.set_unchecked(
        left_offset,
        records.get_unchecked(right_offset),
    )
    records.set_unchecked(
        left_offset + 1,
        records.get_unchecked(right_offset + 1),
    )
    records.set_unchecked(
        left_offset + 2,
        records.get_unchecked(right_offset + 2),
    )
    records.set_unchecked(
        left_offset + 3,
        records.get_unchecked(right_offset + 3),
    )
    records.set_unchecked(right_offset, left_pc)
    records.set_unchecked(right_offset + 1, left_id)
    records.set_unchecked(right_offset + 2, left_index)
    records.set_unchecked(right_offset + 3, left_exception)


def _swap_final_stack_map_records_native(
    address: int, left_offset: int, right_offset: int
) -> None:
    """Swap two four-word records held at byte offsets of native arena storage.

    ``address`` is the arena storage address as an exact integer.  A raw
    pointer must never travel through a Python parameter here: the frontend
    treats an unannotated parameter as an object and brackets it with
    ``pcc_gc_pin``/``pcc_gc_unpin``, which write the PINNED flag into byte 12
    of whatever the pointer addresses.  For a record arena that byte is bit 38
    of record zero's safepoint id, and the heapsort then carries the stray bit
    around with every swap (observed as ``id | 2**38`` in a replayed module).
    Converting the integer inside each intrinsic argument keeps the address
    an integer at every call boundary.
    """
    first = _record_load_i64(_record_int_to_ptr(address), left_offset)
    second = _record_load_i64(_record_int_to_ptr(address), left_offset + 8)
    third = _record_load_i64(_record_int_to_ptr(address), left_offset + 16)
    fourth = _record_load_i64(_record_int_to_ptr(address), left_offset + 24)
    _record_store_i64(_record_int_to_ptr(address), left_offset, _record_load_i64(_record_int_to_ptr(address), right_offset))
    _record_store_i64(_record_int_to_ptr(address), left_offset + 8, _record_load_i64(_record_int_to_ptr(address), right_offset + 8))
    _record_store_i64(_record_int_to_ptr(address), left_offset + 16, _record_load_i64(_record_int_to_ptr(address), right_offset + 16))
    _record_store_i64(_record_int_to_ptr(address), left_offset + 24, _record_load_i64(_record_int_to_ptr(address), right_offset + 24))
    _record_store_i64(_record_int_to_ptr(address), right_offset, first)
    _record_store_i64(_record_int_to_ptr(address), right_offset + 8, second)
    _record_store_i64(_record_int_to_ptr(address), right_offset + 16, third)
    _record_store_i64(_record_int_to_ptr(address), right_offset + 24, fourth)


def _sift_down_final_stack_map_records_native(address: int, root: int, limit: int) -> None:
    """Heap sift-down over four-word records in native arena storage.

    Same comparison (final PC, then safepoint id) and swap sequence as
    ``_final_stack_map_record_greater`` / ``_swap_final_stack_map_records``,
    so the resulting order and therefore the emitted stack-map bytes are
    identical.  Raw loads and stores replace the out-of-line arena getter and
    setter calls, which profiled at about 9% of a pcc1 emit worker because
    the heapsort performs tens of millions of them per large module.  See
    ``_swap_final_stack_map_records_native`` for why ``address`` is an int.
    """
    while root * 2 + 1 < limit:
        child = root * 2 + 1
        child_offset = child * 32
        if child + 1 < limit:
            sibling_offset = child_offset + 32
            child_pc = _record_load_i64(_record_int_to_ptr(address), child_offset)
            sibling_pc = _record_load_i64(_record_int_to_ptr(address), sibling_offset)
            if sibling_pc != child_pc:
                if sibling_pc > child_pc:
                    child += 1
                    child_offset = sibling_offset
            elif _record_load_i64(_record_int_to_ptr(address), sibling_offset + 8) > _record_load_i64(
                _record_int_to_ptr(address), child_offset + 8
            ):
                child += 1
                child_offset = sibling_offset
        root_offset = root * 32
        child_pc = _record_load_i64(_record_int_to_ptr(address), child_offset)
        root_pc = _record_load_i64(_record_int_to_ptr(address), root_offset)
        if child_pc != root_pc:
            if child_pc <= root_pc:
                return
        elif _record_load_i64(_record_int_to_ptr(address), child_offset + 8) <= _record_load_i64(
            _record_int_to_ptr(address), root_offset + 8
        ):
            return
        _swap_final_stack_map_records_native(address, root_offset, child_offset)
        root = child


def _sort_final_stack_map_records(records: CompilerIntArena) -> None:
    """Sort four-scalar records by final PC then stable safepoint id.

    The text oracle historically built one Python tuple per safepoint and
    sorted those tuples.  The structured path keeps the same order in native
    scalar storage so it does not recreate the record projection it removes.
    When the arena holds native storage (pcc1) the heapsort runs on the raw
    words; the CPython list oracle below keeps the arena-method form.
    """

    count = len(records) // 4
    native_address = records.native_address()
    if native_address != 0:
        start = count // 2 - 1
        while start >= 0:
            _sift_down_final_stack_map_records_native(native_address, start, count)
            start -= 1
        end = count - 1
        while end > 0:
            _swap_final_stack_map_records_native(native_address, 0, end * 32)
            _sift_down_final_stack_map_records_native(native_address, 0, end)
            end -= 1
        return
    start = count // 2 - 1
    while start >= 0:
        root = start
        while root * 2 + 1 < count:
            child = root * 2 + 1
            if (
                child + 1 < count
                and _final_stack_map_record_greater(
                    records,
                    child + 1,
                    child,
                )
            ):
                child += 1
            if not _final_stack_map_record_greater(records, child, root):
                break
            _swap_final_stack_map_records(records, root, child)
            root = child
        start -= 1

    end = count - 1
    while end > 0:
        _swap_final_stack_map_records(records, 0, end)
        root = 0
        while root * 2 + 1 < end:
            child = root * 2 + 1
            if (
                child + 1 < end
                and _final_stack_map_record_greater(
                    records,
                    child + 1,
                    child,
                )
            ):
                child += 1
            if not _final_stack_map_record_greater(records, child, root):
                break
            _swap_final_stack_map_records(records, root, child)
            root = child
        end -= 1


def _pack_stack_map_record_arena(records: CompilerIntArena) -> bytes:
    """Pack final stack-map records without one bytes object per row."""

    if len(records) % _STACK_MAP_RECORD_SCALAR_COUNT:
        raise BackendUnavailable("stack-map record scalar count is malformed")
    record_count = len(records) // _STACK_MAP_RECORD_SCALAR_COUNT
    if not records.uses_native_storage:
        chunks: list[bytes] = []
        record_index = 0
        while record_index < record_count:
            offset = record_index * _STACK_MAP_RECORD_SCALAR_COUNT
            chunks.append(_STACK_MAP_RECORD_CODEC.pack(
                records.get_unchecked(offset),
                records.get_unchecked(offset + 1),
                records.get_unchecked(offset + 2),
                records.get_unchecked(offset + 3),
                records.get_unchecked(offset + 4),
                records.get_unchecked(offset + 5),
                records.get_unchecked(offset + 6),
                records.get_unchecked(offset + 7),
                records.get_unchecked(offset + 8),
                records.get_unchecked(offset + 9),
            ))
            record_index += 1
        return b"".join(chunks)

    total = record_count * _STACK_MAP_RECORD_CODEC.size
    if total == 0:
        return b""
    allocation = malloc(total)
    if ptr_is_null(allocation):
        raise MemoryError("stack-map record pack allocation failed")
    memset(allocation, 0, total)
    record_index = 0
    while record_index < record_count:
        source = record_index * _STACK_MAP_RECORD_SCALAR_COUNT
        destination = record_index * _STACK_MAP_RECORD_CODEC.size
        store_i64(allocation, destination, records.get_unchecked(source))
        store_i32(allocation, destination + 8, records.get_unchecked(source + 1))
        store_i32(allocation, destination + 12, records.get_unchecked(source + 2))
        store_i32(allocation, destination + 16, records.get_unchecked(source + 3))
        store_i32(allocation, destination + 20, records.get_unchecked(source + 4))
        store_i8(allocation, destination + 24, records.get_unchecked(source + 6))
        store_i8(allocation, destination + 25, records.get_unchecked(source + 7))
        store_i32(allocation, destination + 28, records.get_unchecked(source + 9))
        record_index += 1
    result = _py_bytes_new(allocation, total)
    free(allocation)
    if ptr_is_null(result):
        raise MemoryError("stack-map record bytes allocation failed")
    return result


def build_aarch64_stack_map_section(
    lines: list[str],
    plans: tuple[FunctionStackMapPlan, ...],
    *,
    function_symbol,
    block_label,
    target_offsets: dict[str, int] | None = None,
) -> Section:
    """Build the final Mach-O stack-map section without text projection.

    The public ABI codecs in ``precise_stackmap`` remain the byte-layout
    authority.  Packed plans stay in compiler-native scalar arenas; the only
    per-record object produced here is its final immutable byte chunk.
    """

    offsets = (
        _aarch64_text_label_offsets(lines)
        if target_offsets is None else target_offsets
    )
    ordered_plans = sorted(plans, key=lambda item: item.function_id)
    chunks: list[bytes] = [b""]
    relocations: list[Relocation] = []
    global_locations = CompilerIntArena()
    global_location_content_index: dict[str, int] = {}
    payload_size = _STACK_MAP_HEADER_CODEC.size
    previous_function_id = -1
    seen_safepoints: set[int] = set()
    packed_record_scalars = CompilerIntArena()
    packed_record_path = packed_record_scalars.uses_native_storage

    for plan in ordered_plans:
        if plan.function_id <= previous_function_id:
            raise BackendUnavailable(
                "stack-map functions need unique ordered stable ids"
            )
        previous_function_id = plan.function_id
        symbol = function_symbol(plan.function_name)
        if not symbol or "\0" in symbol:
            raise BackendUnavailable("stack-map function symbol is invalid")
        start = offsets.get(symbol)
        end = offsets.get(plan.end_label)
        if start is None or end is None or end <= start:
            raise BackendUnavailable(
                f"target-final stack-map range missing for {plan.function_name!r}"
            )
        code_size = end - start
        if plan.frame_size < 0 or plan.frame_size % 16:
            raise BackendUnavailable(
                f"stack-map frame size is invalid for {plan.function_name!r}"
            )
        maybe_packed_records = plan.packed_records
        if maybe_packed_records is None:
            raise BackendUnavailable(
                "AArch64 stack-map plan is missing packed records"
            )
        packed_records: PackedPlannedSafepoints = maybe_packed_records
        final_records = CompilerIntArena(max(1, len(packed_records) * 4))
        record_index = 0
        while record_index < len(packed_records):
            scalar: CompilerInt4 = packed_records.scalar(record_index)
            label = packed_records.label(record_index)
            pc = offsets.get(label)
            if pc is None:
                raise BackendUnavailable(
                    f"target-final safepoint label missing: {label!r}"
                )
            exceptional_offset = NO_OFFSET
            exceptional_block = packed_records.exceptional_block(record_index)
            if exceptional_block:
                exceptional_pc = offsets.get(
                    block_label(plan.function_name, exceptional_block)
                )
                if exceptional_pc is None:
                    raise BackendUnavailable(
                        "target-final exception successor label missing for "
                        f"{plan.function_name!r}/{exceptional_block!r}"
                    )
                exceptional_offset = exceptional_pc - start
            final_records.append4(
                pc - start,
                scalar.first,
                record_index,
                exceptional_offset,
            )
            record_index += 1
        _sort_final_stack_map_records(final_records)

        relocations.append(Relocation(
            payload_size + 8,
            symbol,
            _macho_spec.ARM64_RELOC_UNSIGNED,
            pcrel=False,
            length=3,
        ))
        function_chunk = _STACK_MAP_FUNCTION_CODEC.pack(
            plan.function_id,
            0,
            code_size,
            plan.frame_size,
            len(packed_records),
            0,
        )
        chunks.append(function_chunk)
        payload_size += len(function_chunk)

        local_location_indices: dict[int, int] = {}
        previous_pc = -1
        final_record_index = 0
        while final_record_index < len(final_records) // 4:
            final_offset = final_record_index * 4
            instruction_offset = final_records.get_unchecked(final_offset)
            record_id = final_records.get_unchecked(final_offset + 1)
            record_index = final_records.get_unchecked(final_offset + 2)
            exceptional_offset = final_records.get_unchecked(final_offset + 3)
            scalar: CompilerInt4 = packed_records.scalar(record_index)
            span: CompilerInt4 = packed_records.span(record_index)
            record_kind = scalar.second
            record_flags = scalar.third
            continuation_id = scalar.fourth
            if record_id <= 0 or record_id in seen_safepoints:
                raise BackendUnavailable(
                    "stack-map safepoint id is invalid or duplicate"
                )
            seen_safepoints.add(record_id)
            if not previous_pc < instruction_offset < code_size:
                raise BackendUnavailable(
                    "stack-map safepoints are not ordered inside the function"
                )
            previous_pc = instruction_offset
            has_exception = exceptional_offset != NO_OFFSET
            if has_exception != bool(record_flags & RECORD_HAS_EXCEPTION_EDGE):
                raise BackendUnavailable(
                    "stack-map exception-edge flag and offset disagree"
                )
            is_continuation = record_kind == SAFEPOINT_CONTINUATION
            if is_continuation != bool(continuation_id):
                raise BackendUnavailable(
                    "stack-map continuation needs one non-zero continuation id"
                )
            if bool(record_flags & RECORD_SUSPENDED) != is_continuation:
                raise BackendUnavailable(
                    "stack-map suspended flag is reserved for continuations"
                )
            location_group_id = span.first
            location_group: CompilerInt2 = packed_records.location_group(
                record_index
            )
            if location_group_id in local_location_indices:
                location_index = local_location_indices[location_group_id]
            else:
                location_offset = 0
                while location_offset < location_group.second:
                    location: CompilerInt2 = packed_records.location_scalar(
                        location_group.first + location_offset
                    )
                    _validate_planned_location_offset(
                        location.first,
                        frame_size=plan.frame_size,
                    )
                    location_offset += 1
                content_key = packed_records.location_group_key(
                    location_group_id
                )
                if content_key in global_location_content_index:
                    location_index = global_location_content_index[content_key]
                else:
                    location_index = len(global_locations) // 2
                    global_location_content_index[content_key] = location_index
                    location_offset = 0
                    while location_offset < location_group.second:
                        global_location: CompilerInt2 = (
                            packed_records.location_scalar(
                                location_group.first + location_offset
                            )
                        )
                        global_locations.append2(
                            global_location.first,
                            global_location.second,
                        )
                        location_offset += 1
                local_location_indices[location_group_id] = location_index
            if packed_record_path:
                packed_record_scalars.append4(
                    record_id,
                    instruction_offset,
                    exceptional_offset,
                    continuation_id,
                )
                packed_record_scalars.append4(
                    location_group.second,
                    0,
                    record_kind,
                    record_flags,
                )
                packed_record_scalars.append2(0, location_index)
            else:
                chunks.append(_STACK_MAP_RECORD_CODEC.pack(
                    record_id,
                    instruction_offset,
                    exceptional_offset,
                    continuation_id,
                    location_group.second,
                    0,
                    record_kind,
                    record_flags,
                    0,
                    location_index,
                ))
            payload_size += _STACK_MAP_RECORD_CODEC.size
            final_record_index += 1
        if packed_record_path and len(packed_record_scalars):
            chunks.append(_pack_stack_map_record_arena(packed_record_scalars))
            packed_record_scalars.clear()
        final_records.close()

    location_count = len(global_locations) // 2
    chunks[0] = _STACK_MAP_HEADER_CODEC.pack(
        MAGIC,
        VERSION,
        ARCH_AARCH64,
        POINTER_SIZE,
        len(ordered_plans),
        location_count,
        0,
    )
    location_index = 0
    while location_index < location_count:
        location: CompilerInt2 = global_locations.get2_unchecked(location_index)
        flags = LOCATION_MANAGED | (
            LOCATION_OWNED if location.second != 0 else 0
        )
        chunks.append(_STACK_MAP_LOCATION_CODEC.pack(
            LOCATION_STACK_INDIRECT,
            flags,
            POINTER_SIZE,
            29,
            NO_BASE,
            location.first,
            POINTER_SIZE,
        ))
        location_index += 1
    global_locations.close()
    packed_record_scalars.close()
    for plan in ordered_plans:
        maybe_packed_records = plan.packed_records
        assert maybe_packed_records is not None
        packed_records: PackedPlannedSafepoints = maybe_packed_records
        packed_records.close()
    return Section(
        sectname="__pcc_stackmaps",
        segname="__DATA",
        data=b"".join(chunks),
        align_log2=3,
        flags=PCC_STACKMAP_SECTION_FLAGS,
        relocations=tuple(relocations),
    )


def _append_bytes(lines: list[str], values: bytes) -> None:
    for start in range(0, len(values), 16):
        lines.append("  .byte " + ", ".join(
            str(value) for value in values[start : start + 16]
        ))


def _validate_symbolic_plans(
    plans: tuple[FunctionStackMapPlan, ...], *, arch: int
) -> None:
    functions: list[FunctionStackMap] = []
    for plan in sorted(plans, key=lambda item: item.function_id):
        records: list[SafepointRecord] = []
        for index, record in enumerate(plan.records):
            records.append(SafepointRecord(
                safepoint_id=record.safepoint_id,
                instruction_offset=index,
                kind=record.kind,
                locations=_stack_locations(record.locations, arch=arch),
                flags=record.flags,
                exceptional_offset=0 if record.exceptional_block else NO_OFFSET,
                continuation_id=record.continuation_id,
            ))
        functions.append(FunctionStackMap(
            function_id=plan.function_id,
            function_address=0,
            code_size=max(1, len(records) + 1),
            frame_size=plan.frame_size,
            records=tuple(records),
        ))
    validate_stack_map(PreciseStackMap(arch=arch, functions=tuple(functions)))


def render_x86_64_stack_map_section(
    emitted_lines: list[str],
    plans: tuple[FunctionStackMapPlan, ...],
    *,
    function_symbol,
    block_label,
) -> list[str]:
    _validate_symbolic_plans(plans, arch=ARCH_X86_64)
    label_order: dict[str, int] = {}
    for index, raw in enumerate(emitted_lines):
        line = raw.strip()
        if line.endswith(":"):
            label_order[line[:-1]] = index
    ordered_plans = sorted(plans, key=lambda item: item.function_id)
    lines = ['.section .pcc_stackmaps,"a",@progbits', ".p2align 3"]
    _append_bytes(lines, MAGIC)
    lines.extend((
        f"  .short {VERSION}",
        f"  .byte {ARCH_X86_64}",
        f"  .byte {POINTER_SIZE}",
        f"  .long {len(ordered_plans)}",
    ))
    for plan in ordered_plans:
        symbol = function_symbol(plan.function_name)
        records = sorted(
            plan.records,
            key=lambda record: (
                label_order.get(record.label, 1 << 60),
                record.safepoint_id,
            ),
        )
        for required in (symbol, plan.end_label, *(record.label for record in records)):
            if required not in label_order:
                raise BackendUnavailable(
                    f"target-final x86 stack-map label missing: {required!r}"
                )
        lines.extend((
            f"  .quad {plan.function_id}",
            f"  .quad {symbol}",
            f"  .long {plan.end_label} - {symbol}",
            f"  .long {plan.frame_size}",
            f"  .long {len(records)}",
            "  .long 0",
        ))
        for record in records:
            exceptional = str(NO_OFFSET)
            if record.exceptional_block:
                target = block_label(plan.function_name, record.exceptional_block)
                if target not in label_order:
                    raise BackendUnavailable(
                        f"target-final x86 exception label missing: {target!r}"
                    )
                exceptional = f"{target} - {symbol}"
            lines.extend((
                f"  .quad {record.safepoint_id}",
                f"  .long {record.label} - {symbol}",
                f"  .long {exceptional}",
                f"  .long {record.continuation_id}",
                f"  .short {len(record.locations)}",
                "  .short 0",
                f"  .byte {record.kind}",
                f"  .byte {record.flags}",
                "  .short 0",
                "  .long 0",
            ))
            packed_locations = _stack_locations(
                record.locations, arch=ARCH_X86_64
            )
            if packed_locations:
                _append_packed_location_lines(
                    lines, packed_locations, arch=ARCH_X86_64
                )
    return lines


__all__ = [
    "FunctionStackMapPlan",
    "PlannedManagedReload",
    "PlannedRootLocation",
    "PlannedSafepoint",
    "build_function_stack_map_plan",
    "build_stack_map_plans",
    "build_aarch64_stack_map_section",
    "render_aarch64_stack_map_section",
    "render_x86_64_stack_map_section",
]
