from __future__ import annotations

"""Indexed native data plane for one parsed self-backend function.

The parser's block arenas already own opcode IDs and operand tuples.  This
module adds the function-wide facts that several hot passes otherwise recover
independently through strings, dicts, views, and dataclasses: stable block and
value IDs, per-instruction def/use spans, terminator uses, block-local last
uses, and a canonical type table.

The kernel is not a second IR.  It references the authoritative block arenas
and exposes diagnostic object projection only through an explicit method.
Unsupported consumers keep using ``ParsedFunction`` until migrated; migrated
consumers never need ``CompactParsedInstrView`` in their normal path.
"""

from . import BackendUnavailable
from .self_backend_call_flags import classify_call_flags
from .self_backend_analysis import (
    _instruction_defined_value_from_id_parts,
    _instruction_defined_value_from_parts,
    _instruction_used_values_from_id_parts,
    _instruction_used_values_from_parts,
    _stable_text_bucket_key,
    is_local_value_ref,
    terminator_used_values,
)
from .self_backend_ir import (
    CompactParsedInstrArena,
    I1,
    IndexedCallPlane,
    PARSED_INSTRUCTION_KIND_ALLOCA,
    PARSED_INSTRUCTION_KIND_BINOP,
    PARSED_INSTRUCTION_KIND_BR,
    PARSED_INSTRUCTION_KIND_BR_COND,
    PARSED_INSTRUCTION_KIND_CALL,
    PARSED_INSTRUCTION_KIND_CAST,
    PARSED_INSTRUCTION_KIND_GEP,
    PARSED_INSTRUCTION_KIND_ICMP,
    PARSED_INSTRUCTION_KIND_LOAD,
    PARSED_INSTRUCTION_KIND_RET,
    PARSED_INSTRUCTION_KIND_RET_VOID,
    PARSED_INSTRUCTION_KIND_SELECT,
    PARSED_INSTRUCTION_KIND_STORE,
    PARSED_INSTRUCTION_KIND_SWITCH,
    PARSED_INSTRUCTION_KIND_UNREACHABLE,
    PARSED_INSTRUCTION_KINDS,
    ParsedBlock,
    ParsedFunction,
    ParsedInstr,
    PhiIncoming,
    PhiInstr,
    TypeDesc,
    _OPERAND_INTERN,
    _PARSED_INSTRUCTION_KIND_IDS,
    _EMPTY_SEQUENCE,
    text_key_mapping_get,
    text_key_names_equal,
)
from .self_backend_value_arena import CompilerInt2, CompilerInt4, CompilerIntArena


TYPE_KIND_VOID = 0
TYPE_KIND_INT = 1
TYPE_KIND_FP = 2
TYPE_KIND_PTR = 3
TYPE_KIND_ARRAY = 4
TYPE_KIND_STRUCT = 5

# One direct/no-text exceptional edge is six native scalars:
# source block, trigger instruction, condition value, error block, source
# line, cleanup-plan ID.  The trigger is the instruction after which the
# condition is consumed; normal execution remains in the same logical block.
# source block, trigger, condition, target, source line, cleanup plan,
# landing payload index (-1 when the target is not a shared frame landing),
# reserved.
INLINE_ERROR_EDGE_WIDTH = 8

_PACKED_FIXED_KIND_IDS = (
    PARSED_INSTRUCTION_KIND_LOAD,
    PARSED_INSTRUCTION_KIND_STORE,
    PARSED_INSTRUCTION_KIND_CAST,
    PARSED_INSTRUCTION_KIND_ICMP,
    PARSED_INSTRUCTION_KIND_BINOP,
    PARSED_INSTRUCTION_KIND_SELECT,
)

_SUPPORTED_SCALAR_TERMINATOR_KIND_IDS = (
    PARSED_INSTRUCTION_KIND_RET,
    PARSED_INSTRUCTION_KIND_RET_VOID,
    PARSED_INSTRUCTION_KIND_BR,
    PARSED_INSTRUCTION_KIND_BR_COND,
    PARSED_INSTRUCTION_KIND_UNREACHABLE,
)

_SUPPORTED_SCALAR_INSTRUCTION_KIND_IDS = (
    PARSED_INSTRUCTION_KIND_ALLOCA,
    PARSED_INSTRUCTION_KIND_LOAD,
    PARSED_INSTRUCTION_KIND_STORE,
    PARSED_INSTRUCTION_KIND_CAST,
    PARSED_INSTRUCTION_KIND_ICMP,
    PARSED_INSTRUCTION_KIND_BINOP,
    PARSED_INSTRUCTION_KIND_SELECT,
    PARSED_INSTRUCTION_KIND_GEP,
    PARSED_INSTRUCTION_KIND_CALL,
)


def _text_id_index_capacity(size: int) -> int:
    capacity = 8
    required = max(1, size * 2)
    while capacity < required:
        capacity *= 2
    return capacity


def _new_text_id_index(size: int) -> tuple[CompilerIntArena, int]:
    capacity = _text_id_index_capacity(size)
    index = CompilerIntArena(capacity * 2)
    index.append_zeros(capacity * 2)
    return index, capacity


def _text_id_index_start_slot(key: int, capacity: int) -> int:
    # Numeric suffix keys are dense, and different mem2reg PHI bases may
    # share an ordinal. Spread adjacent keys before linear probing so those
    # collisions do not scan a contiguous run of ordinary SSA names. The
    # odd multiplier permutes power-of-two slots; keys/equality stay intact.
    return (key * 131) & (capacity - 1)


def _text_id_index_lookup(
    index: CompilerIntArena,
    capacity: int,
    names: list[str],
    name: str,
) -> int:
    key = _stable_text_bucket_key(name)
    slot = _text_id_index_start_slot(key, capacity)
    probes = 0
    while probes < capacity:
        entry: CompilerInt2 = index.get2_unchecked(slot)
        if entry.second == 0:
            return -1
        name_id = entry.second - 1
        if entry.first == key and text_key_names_equal(names[name_id], name):
            return name_id
        slot = (slot + 1) & (capacity - 1)
        probes += 1
    return -1


def _text_id_index_insert(
    index: CompilerIntArena,
    capacity: int,
    names: list[str],
    name_id: int,
) -> None:
    name = names[name_id]
    key = _stable_text_bucket_key(name)
    slot = _text_id_index_start_slot(key, capacity)
    probes = 0
    while probes < capacity:
        entry: CompilerInt2 = index.get2_unchecked(slot)
        if entry.second == 0:
            index.set2_unchecked(slot * 2, key, name_id + 1)
            return
        existing_id = entry.second - 1
        if entry.first == key and text_key_names_equal(
            names[existing_id],
            name,
        ):
            return
        slot = (slot + 1) & (capacity - 1)
        probes += 1
    raise BackendUnavailable("indexed text table has no empty slot")


def _rebuilt_text_id_index(
    names: list[str],
    minimum_size: int,
) -> tuple[CompilerIntArena, int]:
    index, capacity = _new_text_id_index(minimum_size)
    name_id = 0
    while name_id < len(names):
        _text_id_index_insert(index, capacity, names, name_id)
        name_id += 1
    return index, capacity


class IndexedFunctionSeed(IndexedCallPlane):
    """Definitions-first parser owner of the kernel's final hot records.

    Block and value IDs are fixed before instruction payload parsing.  The
    parser can therefore publish final integer operands plus definition facts
    in one pass.  The kernel adopts these exact arenas and computes shared use
    facts once in instruction order, without decoding a tuple/list graph.
    """

    block_names: list[str]
    block_name_index: CompilerIntArena
    block_name_index_capacity: int
    value_names: list[str]
    value_name_index: CompilerIntArena
    value_name_index_capacity: int
    value_name_index_active: bool
    definition_blocks: list[int]
    definition_positions: list[int]
    first_duplicate_definition_value_id: int
    value_type_ids: list[int]
    alloca_type_ids: list[int]
    value_is_used_flags: list[bool]
    value_last_use_positions: list[int]
    used_value_ids_in_order: list[int]
    block_facts: CompilerIntArena
    instruction_facts: CompilerIntArena
    instruction_kind_ids: CompilerIntArena
    instruction_metadata: CompilerIntArena
    instruction_record_dest_ids: CompilerIntArena
    instruction_record_scalars: CompilerIntArena
    gep_index_scalars: CompilerIntArena
    gep_scalars: CompilerIntArena
    instruction_overflow_use_ids: CompilerIntArena
    instruction_use_total: int
    instruction_arithmetic_flag_values: dict[int, tuple[str, ...]]
    cold_instruction_data: list[tuple]
    terminator_case_scalars: CompilerIntArena
    terminator_scalars: CompilerIntArena
    terminator_records_complete: bool
    block_phi_facts: CompilerIntArena
    phi_incoming_scalars: CompilerIntArena
    phi_scalars: CompilerIntArena
    error_edge_scalars: CompilerIntArena
    error_edge_spans: CompilerIntArena
    error_landing_scalars: CompilerIntArena
    phi_records_complete: bool
    use_records_complete: bool
    complete: bool

    __slots__ = (
        "block_names",
        "block_name_index",
        "block_name_index_capacity",
        "value_names",
        "value_name_index",
        "value_name_index_capacity",
        "value_name_index_active",
        "definition_blocks",
        "definition_positions",
        "first_duplicate_definition_value_id",
        "value_type_ids",
        "alloca_type_ids",
        "value_is_used_flags",
        "value_last_use_positions",
        "used_value_ids_in_order",
        "block_facts",
        "instruction_facts",
        "instruction_kind_ids",
        "instruction_metadata",
        "instruction_record_dest_ids",
        "instruction_record_scalars",
        "gep_index_scalars",
        "gep_scalars",
        "instruction_overflow_use_ids",
        "instruction_use_total",
        "instruction_arithmetic_flag_values",
        "cold_instruction_data",
        "terminator_case_scalars",
        "terminator_scalars",
        "terminator_records_complete",
        "block_phi_facts",
        "phi_incoming_scalars",
        "phi_scalars",
        "error_edge_scalars",
        "error_edge_spans",
        "error_landing_scalars",
        "phi_records_complete",
        "use_records_complete",
        "complete",
    )

    def __init__(
        self,
        block_capacity_hint: int = 0,
        value_capacity_hint: int = 0,
    ) -> None:
        IndexedCallPlane.__init__(self)
        self.block_names: list[str] = []
        (
            self.block_name_index,
            self.block_name_index_capacity,
        ) = _new_text_id_index(block_capacity_hint)
        self.value_names: list[str] = []
        (
            self.value_name_index,
            self.value_name_index_capacity,
        ) = _new_text_id_index(value_capacity_hint)
        self.value_name_index_active = False
        self.definition_blocks: list[int] = []
        # Construction index only.  The kernel freezes this column into a
        # CompilerIntArena before verification; -3 is undefined, -2 marks a
        # duplicate, -1 is an argument/PHI, and >=0 is an instruction index.
        self.definition_positions: list[int] = []
        self.first_duplicate_definition_value_id = -1
        self.value_type_ids: list[int] = []
        self.alloca_type_ids: list[int] = []
        self.value_is_used_flags: list[bool] = []
        self.value_last_use_positions: list[int] = []
        self.used_value_ids_in_order: list[int] = []
        self.block_facts = CompilerIntArena()
        self.instruction_facts = CompilerIntArena()
        self.instruction_kind_ids = CompilerIntArena()
        self.instruction_metadata = CompilerIntArena()
        self.instruction_record_dest_ids = CompilerIntArena()
        self.instruction_record_scalars = CompilerIntArena()
        self.gep_index_scalars = CompilerIntArena()
        self.gep_scalars = CompilerIntArena()
        self.instruction_overflow_use_ids = CompilerIntArena()
        self.instruction_use_total = 0
        self.instruction_arithmetic_flag_values: dict[
            int, tuple[str, ...]
        ] = {}
        self.cold_instruction_data: list[tuple] = []
        self.terminator_case_scalars = CompilerIntArena()
        self.terminator_scalars = CompilerIntArena()
        self.terminator_records_complete = False
        self.block_phi_facts = CompilerIntArena()
        self.phi_incoming_scalars = CompilerIntArena()
        self.phi_scalars = CompilerIntArena()
        # Final fixed-width inline edges plus one start/count span per block.
        # Direct construction resolves block names and record positions before
        # the kernel adopts this plane.
        self.error_edge_scalars = CompilerIntArena()
        self.error_edge_spans = CompilerIntArena()
        # (landing block ID, i32 alloca value ID) pairs: blocks that read the
        # payload index an inline error edge's cold stub stored.
        self.error_landing_scalars = CompilerIntArena()
        self.phi_records_complete = False
        self.use_records_complete = False
        self.complete = False

    def _canonical_value_name(self, name: str) -> str:
        if name in _OPERAND_INTERN:
            return _OPERAND_INTERN[name]
        _OPERAND_INTERN[name] = name
        return name

    def register_block(self, name: str) -> int:
        block_id = len(self.block_names)
        canonical = self._canonical_value_name(name)
        if (block_id + 1) * 2 >= self.block_name_index_capacity:
            old_index = self.block_name_index
            (
                self.block_name_index,
                self.block_name_index_capacity,
            ) = _rebuilt_text_id_index(
                self.block_names,
                (block_id + 1) * 2,
            )
            old_index.close()
        self.block_names.append(canonical)
        _text_id_index_insert(
            self.block_name_index,
            self.block_name_index_capacity,
            self.block_names,
            block_id,
        )
        return block_id

    def block_id(self, name: str) -> int:
        return _text_id_index_lookup(
            self.block_name_index,
            self.block_name_index_capacity,
            self.block_names,
            name,
        )

    def _append_value_columns(self, canonical: str) -> int:
        value_id = len(self.value_names)
        self.value_names.append(canonical)
        self.definition_blocks.append(-1)
        self.definition_positions.append(-3)
        self.value_type_ids.append(-1)
        self.alloca_type_ids.append(-1)
        self.value_is_used_flags.append(False)
        return value_id

    def _ensure_value_name_index(self) -> None:
        if self.value_name_index_active:
            return
        old_index = self.value_name_index
        (
            self.value_name_index,
            self.value_name_index_capacity,
        ) = _rebuilt_text_id_index(
            self.value_names,
            max(1, (len(self.value_names) + 1) * 2),
        )
        old_index.close()
        self.value_name_index_active = True

    def _append_value(self, canonical: str) -> int:
        value_id = len(self.value_names)
        if (value_id + 1) * 2 >= self.value_name_index_capacity:
            old_index = self.value_name_index
            (
                self.value_name_index,
                self.value_name_index_capacity,
            ) = _rebuilt_text_id_index(
                self.value_names,
                (value_id + 1) * 2,
            )
            old_index.close()
        self._append_value_columns(canonical)
        _text_id_index_insert(
            self.value_name_index,
            self.value_name_index_capacity,
            self.value_names,
            value_id,
        )
        return value_id

    def intern_value(self, name: str) -> int:
        canonical = self._canonical_value_name(name)
        self._ensure_value_name_index()
        value_id = _text_id_index_lookup(
            self.value_name_index,
            self.value_name_index_capacity,
            self.value_names,
            canonical,
        )
        if value_id >= 0:
            return value_id
        return self._append_value(canonical)

    def append_proven_new_value(self, name: str) -> int:
        """Append a builder-proven unique SSA value without a miss probe.

        llvm_capi assigns each Value its final function-local name before the
        direct publisher calls this method.  The ordinary parser and every
        unproven string boundary continue through ``intern_value``.
        """
        canonical = self._canonical_value_name(name)
        if self.value_name_index_active:
            return self._append_value(canonical)
        return self._append_value_columns(canonical)

    def value_id(self, name: str) -> int:
        canonical = self._canonical_value_name(name)
        self._ensure_value_name_index()
        return _text_id_index_lookup(
            self.value_name_index,
            self.value_name_index_capacity,
            self.value_names,
            canonical,
        )

    def define_value(
        self,
        name: str,
        block_id: int,
        value_type: TypeDesc | None = None,
        position: int = -1,
    ) -> int:
        value_id = self.intern_value(name)
        self.define_value_id(
            value_id,
            block_id,
            value_type,
            position,
        )
        return value_id

    def define_value_id(
        self,
        value_id: int,
        block_id: int,
        value_type: TypeDesc | None = None,
        position: int = -1,
    ) -> None:
        """Publish a definition for an already-interned dense value ID."""
        # Preserve the old last-definition marker; the verifier owns the
        # stable duplicate-definition diagnostic.
        if self.definition_positions[value_id] != -3:
            if self.first_duplicate_definition_value_id < 0:
                self.first_duplicate_definition_value_id = value_id
            self.definition_positions[value_id] = -2
        else:
            self.definition_positions[value_id] = position
        self.definition_blocks[value_id] = block_id
        if value_type is not None:
            self.value_type_ids[value_id] = self.intern_type(value_type)

    def publish_value_type_id(self, value_id: int, type_id: int) -> None:
        if value_id >= 0:
            self.value_type_ids[value_id] = type_id

    def publish_alloca_type_id(self, value_id: int, type_id: int) -> None:
        if value_id >= 0:
            self.alloca_type_ids[value_id] = type_id

    def append_parsed_call(
        self,
        dest: str | None,
        ret_type: TypeDesc,
        callee: str,
        is_indirect: bool,
        arg_start: int,
        arg_count: int,
        fixed_arg_count: int,
        is_vararg: bool,
    ) -> None:
        dest_value_id = -1 if dest is None else self.value_id(dest)
        if dest is not None and dest_value_id < 0:
            raise BackendUnavailable(
                "indexed function seed omitted a call destination"
            )
        ret_type_id = self.intern_type(ret_type)
        call_id = len(self.records) // 8
        flags = classify_call_flags(callee, is_indirect, is_vararg)
        self.records.append4(
            ret_type_id,
            self.intern_text(callee),
            flags,
            arg_start,
        )
        self.records.append4(
            arg_count,
            fixed_arg_count,
            dest_value_id,
            0,
        )
        if dest_value_id >= 0:
            self.value_type_ids[dest_value_id] = ret_type_id
        instruction_id = len(self.instruction_metadata) // 4
        self.instruction_facts.append4(dest_value_id, 0, -1, -1)
        call_kind_id = _PARSED_INSTRUCTION_KIND_IDS["call"]
        self.instruction_metadata.append4(
            call_kind_id,
            call_id,
            0,
            0,
        )
        if len(self.instruction_kind_ids) != instruction_id:
            raise BackendUnavailable(
                "indexed function seed call order is inconsistent"
            )
        self.instruction_kind_ids.append(call_kind_id)

    def operand_ref(self, text: str) -> int:
        if not is_local_value_ref(text):
            return -self.intern_text(text) - 1
        return self.intern_value(text)

    def append_instruction(
        self,
        kind: str,
        payload_id: int,
        dest_value_id: int,
        is_volatile: bool = False,
        arithmetic_flags: tuple[str, ...] = (),
    ) -> None:
        kind_id = _PARSED_INSTRUCTION_KIND_IDS.get(kind)
        if kind_id is None:
            raise BackendUnavailable(f"unknown indexed instruction kind {kind!r}")
        self.append_instruction_kind_id(
            kind_id,
            payload_id,
            dest_value_id,
            is_volatile,
            arithmetic_flags,
        )

    def append_instruction_kind_id(
        self,
        kind_id: int,
        payload_id: int,
        dest_value_id: int,
        is_volatile: bool = False,
        arithmetic_flags: tuple[str, ...] = (),
    ) -> None:
        """Append an already-proven opcode ID without string projection."""
        if not 0 <= kind_id < len(PARSED_INSTRUCTION_KINDS):
            raise BackendUnavailable(
                "unknown indexed instruction kind id " + str(kind_id)
            )
        instruction_id = len(self.instruction_metadata) // 4
        self.instruction_facts.append4(
            dest_value_id,
            0,
            -1,
            -1,
        )
        self.instruction_metadata.append4(
            kind_id,
            payload_id,
            1 if is_volatile else 0,
            1 if arithmetic_flags else 0,
        )
        if arithmetic_flags:
            self.instruction_arithmetic_flag_values[instruction_id] = (
                arithmetic_flags
            )
        if len(self.instruction_kind_ids) != instruction_id:
            raise BackendUnavailable(
                "indexed function seed instruction order is inconsistent"
            )
        # One function-level scalar lane replaces one arena object and one
        # Python kind container per block while preserving the cheap hot
        # stackprep traversal that the old kind mirror existed to serve.
        self.instruction_kind_ids.append(kind_id)

    def append_block_fact(
        self,
        instruction_start: int,
        instruction_count: int,
        terminator_use_count: int,
        terminator_use_id: int,
    ) -> None:
        self.block_facts.append4(
            instruction_start,
            instruction_count,
            terminator_use_count,
            terminator_use_id,
        )

    def finish(self) -> None:
        self._ensure_value_name_index()
        self.complete = True

    def append_cold_instruction_data(self, data: tuple) -> int:
        cold_id = len(self.cold_instruction_data)
        self.cold_instruction_data.append(data)
        return -cold_id - 1

    def instruction_payload_id_by_id(self, instruction_id: int) -> int:
        metadata: CompilerInt4 = self.instruction_metadata.get4_unchecked(
            instruction_id
        )
        return metadata.second

    def instruction_metadata_by_id(self, instruction_id: int) -> CompilerInt4:
        return self.instruction_metadata.get4_unchecked(instruction_id)

    def instruction_record(self, record_id: int) -> CompilerInt4:
        return self.instruction_record_scalars.get4_unchecked(record_id)

    def terminator_used_value(self, block_id: int) -> str | None:
        header: CompilerInt4 = self.terminator_scalars.get4_unchecked(
            block_id * 2
        )
        if (
            header.first != PARSED_INSTRUCTION_KIND_RET
            and header.first != PARSED_INSTRUCTION_KIND_BR_COND
            and header.first != PARSED_INSTRUCTION_KIND_SWITCH
        ) or header.third < 0:
            return None
        return self.value_names[header.third]

    def gep_header(self, record_id: int) -> CompilerInt4:
        return self.gep_scalars.get4_unchecked(record_id * 2)

    def gep_span(self, record_id: int) -> CompilerInt4:
        return self.gep_scalars.get4_unchecked(record_id * 2 + 1)

    def gep_index(self, index_id: int) -> CompilerInt2:
        return self.gep_index_scalars.get2_unchecked(index_id)

    def _operand_text(self, operand: int) -> str:
        if operand >= 0:
            return self.value_names[operand]
        return self.texts[-operand - 1]

    def diagnostic_call_data(self, call_id: int) -> tuple:
        self.diagnostic_projections += 1
        header: CompilerInt4 = self.header(call_id)
        span: CompilerInt4 = self.span(call_id)
        args = []
        alignments = []
        arg_index = 0
        while arg_index < span.first:
            raw: CompilerInt4 = self.arg(header.fourth + arg_index)
            value = (
                self.value_names[raw.second]
                if raw.second >= 0
                else self.texts[raw.third]
            )
            args.append((self.types[raw.first], value))
            alignments.append(raw.fourth)
            arg_index += 1
        return (
            None if span.third < 0 else self.value_names[span.third],
            self.types[header.first],
            self.texts[header.second],
            bool(header.third & 1),
            tuple(args),
            span.second,
            bool(header.third & 2),
            tuple(alignments),
        )

    def diagnostic_fixed_instruction_data(
        self,
        kind_id: int,
        record_id: int,
    ) -> tuple:
        self.diagnostic_projections += 1
        raw: CompilerInt4 = self.instruction_record(record_id)
        dest_id = self.instruction_record_dest_ids.get_unchecked(record_id)
        dest = None if dest_id < 0 else self.value_names[dest_id]
        if kind_id == PARSED_INSTRUCTION_KIND_LOAD:
            return (
                dest,
                self.types[raw.first],
                self.types[raw.second],
                self._operand_text(raw.third),
            )
        if kind_id == PARSED_INSTRUCTION_KIND_STORE:
            return (
                self.types[raw.first],
                self._operand_text(raw.second),
                self.types[raw.third],
                self._operand_text(raw.fourth),
            )
        if kind_id == PARSED_INSTRUCTION_KIND_CAST:
            return (
                self.texts[raw.first],
                dest,
                self.types[raw.second],
                self._operand_text(raw.third),
                self.types[raw.fourth],
            )
        if (
            kind_id == PARSED_INSTRUCTION_KIND_ICMP
            or kind_id == PARSED_INSTRUCTION_KIND_BINOP
        ):
            return (
                self.texts[raw.first],
                dest,
                self.types[raw.second],
                self._operand_text(raw.third),
                self._operand_text(raw.fourth),
            )
        return (
            dest,
            self.types[raw.first],
            self._operand_text(raw.second),
            self._operand_text(raw.third),
            self._operand_text(raw.fourth),
        )

    def diagnostic_alloca_data(self, value_id: int) -> tuple:
        self.diagnostic_projections += 1
        return (
            self.value_names[value_id],
            self.types[self.alloca_type_ids[value_id]],
        )

    def diagnostic_gep_data(self, record_id: int) -> tuple:
        self.diagnostic_projections += 1
        header: CompilerInt4 = self.gep_header(record_id)
        span: CompilerInt4 = self.gep_span(record_id)
        indices: list[tuple[TypeDesc, str]] = []
        index = 0
        while index < span.first:
            raw: CompilerInt2 = self.gep_index(header.fourth + index)
            indices.append((self.types[raw.first], self._operand_text(raw.second)))
            index += 1
        return (
            self.value_names[span.third],
            self.types[header.first],
            self.types[header.second],
            self._operand_text(header.third),
            tuple(indices),
        )

    def diagnostic_cold_instruction_data(self, cold_id: int) -> tuple:
        self.diagnostic_projections += 1
        return self.cold_instruction_data[cold_id]

    def release_construction_indexes(self) -> None:
        IndexedCallPlane.release_construction_indexes(self)


class IndexedFunctionKernel:
    """One function's stable IDs and shared indexed analysis."""

    instruction_arenas: list[CompactParsedInstrArena]
    block_facts: CompilerIntArena
    block_name_index: CompilerIntArena
    block_name_index_capacity: int
    call_arg_scalars: CompilerIntArena
    call_scalars: CompilerIntArena
    call_diagnostic_projections: int
    instruction_kind_ids: CompilerIntArena
    instruction_metadata: CompilerIntArena
    instruction_facts: CompilerIntArena
    instruction_record_dest_ids: CompilerIntArena
    instruction_record_scalars: CompilerIntArena
    gep_index_scalars: CompilerIntArena
    gep_scalars: CompilerIntArena
    instruction_overflow_use_ids: CompilerIntArena
    instruction_use_total: int
    type_field_ids: CompilerIntArena
    type_scalars: CompilerIntArena
    terminator_case_scalars: CompilerIntArena
    terminator_scalars: CompilerIntArena
    block_phi_facts: CompilerIntArena
    phi_incoming_scalars: CompilerIntArena
    phi_scalars: CompilerIntArena
    error_edge_scalars: CompilerIntArena
    error_edge_spans: CompilerIntArena
    error_landing_scalars: CompilerIntArena
    value_scalars: CompilerIntArena
    definition_positions: CompilerIntArena
    definition_position_values: list[int]
    first_duplicate_definition_value_id: int
    used_value_ids: CompilerIntArena
    slot_scalars: CompilerIntArena
    block_layout_ids: CompilerIntArena
    value_name_index: CompilerIntArena
    value_name_index_capacity: int

    __slots__ = (
        "block_names",
        "block_name_buckets",
        "block_name_index",
        "block_name_index_capacity",
        "instruction_arenas",
        "indexed_call_plane",
        "instruction_kind_ids",
        "instruction_metadata",
        "instruction_arithmetic_flag_values",
        "cold_instruction_data",
        "call_arg_scalars",
        "call_scalars",
        "call_texts",
        "call_diagnostic_projections",
        "value_names",
        "value_name_buckets",
        "value_name_index",
        "value_name_index_capacity",
        "definition_blocks",
        "definition_position_values",
        "definition_positions",
        "first_duplicate_definition_value_id",
        "block_facts",
        "instruction_facts",
        "instruction_record_dest_ids",
        "instruction_record_scalars",
        "gep_index_scalars",
        "gep_scalars",
        "instruction_overflow_use_ids",
        "instruction_use_total",
        "used_value_ids_in_order",
        "value_is_used_flags",
        "value_last_use_positions",
        "types",
        "type_identity_ids",
        "pointer_type_ids",
        "type_field_ids",
        "type_scalars",
        "type_object_projections",
        "terminator_case_scalars",
        "terminator_scalars",
        "terminator_diagnostic_projections",
        "block_phi_facts",
        "phi_incoming_scalars",
        "phi_scalars",
        "error_edge_scalars",
        "error_edge_spans",
        "error_landing_scalars",
        "phi_diagnostic_projections",
        "value_scalars",
        "used_value_ids",
        "slot_scalars",
        "block_layout_ids",
        "value_type_ids",
        "slot_offsets",
        "slot_type_ids",
        "slot_ids_by_offset",
        "hidden_sret_slot_id",
        "value_slot_ids",
        "alloca_offsets",
        "alloca_type_ids",
        "value_registers",
        "diagnostic_projections",
        "block_diagnostic_projections",
        "instruction_arena_diagnostic_projections",
        "legacy_slot_projections",
    )

    def __init__(self, func: ParsedFunction) -> None:
        candidate_seed = func.indexed_seed
        seed = (
            candidate_seed
            if isinstance(candidate_seed, IndexedFunctionSeed)
            else None
        )
        if candidate_seed is not None and seed is None:
            raise BackendUnavailable("parsed function has an invalid indexed seed")
        if seed is not None and not seed.complete:
            raise BackendUnavailable("parsed function has an incomplete indexed seed")

        self.block_names: list[str] = [] if seed is None else seed.block_names
        self.block_name_buckets: dict[int, list[tuple[str, int]]] = {}
        self.block_name_index_capacity = (
            0 if seed is None else seed.block_name_index_capacity
        )
        if seed is not None:
            self.block_name_index = seed.block_name_index
        self.instruction_arenas = []
        self.indexed_call_plane = seed
        if seed is None and func.blocks:
            candidate_call_plane = func.blocks[0].instructions._indexed_call_plane
            if isinstance(candidate_call_plane, IndexedCallPlane):
                self.indexed_call_plane = candidate_call_plane
        # One raw fixed-width record per instruction:
        # kind ID, packed payload ID, volatile bit, arithmetic-flags bit.
        # A function-wide bytearray is not viable under pcc: bytearray.append
        # rebuilds the inline payload, so N instructions retain/copy O(N^2)
        # bytes.  CompilerIntArena gives this compiler-private scalar plane a
        # true growable native representation.
        self.instruction_kind_ids = (
            CompilerIntArena() if seed is None else seed.instruction_kind_ids
        )
        self.instruction_metadata = (
            CompilerIntArena() if seed is None else seed.instruction_metadata
        )
        self.instruction_arithmetic_flag_values: dict[int, tuple[str, ...]] = (
            {} if seed is None else seed.instruction_arithmetic_flag_values
        )
        self.cold_instruction_data: list[tuple] = (
            [] if seed is None else seed.cold_instruction_data
        )
        if self.indexed_call_plane is None:
            self.call_arg_scalars = CompilerIntArena()
            self.call_scalars = CompilerIntArena()
            self.call_texts: list[str] = []
        else:
            self.call_arg_scalars = self.indexed_call_plane.args
            self.call_scalars = self.indexed_call_plane.records
            self.call_texts = self.indexed_call_plane.texts
        self.call_diagnostic_projections = 0
        self.value_names: list[str] = [] if seed is None else seed.value_names
        self.value_name_buckets: dict[int, list[tuple[str, int]]] = {}
        self.value_name_index_capacity = (
            0 if seed is None else seed.value_name_index_capacity
        )
        if seed is not None:
            self.value_name_index = seed.value_name_index
        self.definition_blocks: list[int] = (
            [] if seed is None else seed.definition_blocks
        )
        self.definition_position_values: list[int] = (
            [] if seed is None else seed.definition_positions
        )
        self.definition_positions = CompilerIntArena()
        self.first_duplicate_definition_value_id = (
            -1 if seed is None else seed.first_duplicate_definition_value_id
        )
        # block_facts: instruction start/count, terminator use count/id.
        # instruction_facts: destination ID, use count, first use ID, and
        # either the second use ID or ``-(overflow start)-2`` for >2 uses.
        # Both are fixed-width value records.  Only the uncommon >2-use tail
        # reaches the scalar overflow arena.
        self.block_facts = (
            CompilerIntArena(max(1, len(func.blocks) * 4))
            if seed is None
            else seed.block_facts
        )
        self.instruction_facts = (
            CompilerIntArena() if seed is None else seed.instruction_facts
        )
        self.instruction_record_dest_ids = (
            CompilerIntArena()
            if seed is None
            else seed.instruction_record_dest_ids
        )
        self.instruction_record_scalars = (
            CompilerIntArena()
            if seed is None
            else seed.instruction_record_scalars
        )
        self.gep_index_scalars = (
            CompilerIntArena() if seed is None else seed.gep_index_scalars
        )
        self.gep_scalars = (
            CompilerIntArena() if seed is None else seed.gep_scalars
        )
        self.instruction_overflow_use_ids = (
            CompilerIntArena()
            if seed is None
            else seed.instruction_overflow_use_ids
        )
        self.instruction_use_total = (
            0 if seed is None else seed.instruction_use_total
        )
        self.used_value_ids_in_order: list[int] = (
            [] if seed is None else seed.used_value_ids_in_order
        )
        self.value_is_used_flags: list[bool] = (
            [] if seed is None else seed.value_is_used_flags
        )
        self.value_last_use_positions: list[int] = (
            [] if seed is None else seed.value_last_use_positions
        )
        self.types: list[TypeDesc] = (
            []
            if self.indexed_call_plane is None
            else self.indexed_call_plane.types
        )
        self.type_identity_ids: dict[int, tuple[int, TypeDesc]] = {}
        self.pointer_type_ids: dict[int, int] = {}
        self.type_field_ids = CompilerIntArena()
        self.type_scalars = CompilerIntArena()
        self.type_object_projections = 0
        if self.indexed_call_plane is not None:
            self._adopt_indexed_call_types()
        self.terminator_case_scalars = (
            CompilerIntArena()
            if seed is None
            else seed.terminator_case_scalars
        )
        self.terminator_scalars = (
            CompilerIntArena() if seed is None else seed.terminator_scalars
        )
        self.terminator_diagnostic_projections = 0
        self.block_phi_facts = (
            CompilerIntArena() if seed is None else seed.block_phi_facts
        )
        self.phi_incoming_scalars = (
            CompilerIntArena() if seed is None else seed.phi_incoming_scalars
        )
        self.phi_scalars = (
            CompilerIntArena() if seed is None else seed.phi_scalars
        )
        self.error_edge_scalars = (
            CompilerIntArena() if seed is None else seed.error_edge_scalars
        )
        self.error_edge_spans = (
            CompilerIntArena() if seed is None else seed.error_edge_spans
        )
        self.error_landing_scalars = (
            CompilerIntArena() if seed is None else seed.error_landing_scalars
        )
        self.phi_diagnostic_projections = 0
        self.value_scalars = CompilerIntArena()
        self.used_value_ids = CompilerIntArena()
        self.slot_scalars = CompilerIntArena()
        self.block_layout_ids = CompilerIntArena()
        self.value_type_ids: list[int] = (
            [] if seed is None else seed.value_type_ids
        )
        self.slot_offsets: list[int] = []
        self.slot_type_ids: list[int] = []
        self.slot_ids_by_offset: dict[int, dict[int, int]] = {}
        self.hidden_sret_slot_id = -1
        value_count = len(self.value_names)
        self.value_slot_ids: list[int] = [-1] * value_count
        self.alloca_offsets: list[int] = [-1] * value_count
        self.alloca_type_ids: list[int] = (
            [] if seed is None else seed.alloca_type_ids
        )
        self.value_registers: list[int] = [-1] * value_count
        self.diagnostic_projections = 0
        self.block_diagnostic_projections = 0
        self.instruction_arena_diagnostic_projections = 0
        self.legacy_slot_projections = 0

        for block_id, block in enumerate(func.blocks):
            self.instruction_arenas.append(block.instructions)
            if seed is None:
                self.block_names.append(block.name)
                bucket_key = _stable_text_bucket_key(block.name)
                self.block_name_buckets.setdefault(bucket_key, []).append(
                    (block.name, block_id)
                )
            elif not text_key_names_equal(self.block_names[block_id], block.name):
                raise BackendUnavailable(
                    "indexed function seed block order is inconsistent"
                )
        if seed is None or (
            len(self.error_edge_scalars) == 0
            and len(self.error_edge_spans) == 0
        ):
            block_id = 0
            while block_id < len(self.block_names):
                self.error_edge_spans.append2(0, 0)
                block_id += 1
        elif len(self.error_edge_spans) != len(self.block_names) * 2:
            raise BackendUnavailable(
                "indexed inline error-edge spans do not match block count"
            )
        self._freeze_block_name_index()

        if seed is None:
            self._index_function_definitions(func)
            self._index_function_uses(func)
        elif not seed.use_records_complete:
            self._index_seed_function_uses(func)

        self._freeze_value_name_index()
        if seed is None or not seed.terminator_records_complete:
            self._freeze_terminator_records(func)
        if seed is None or not seed.phi_records_complete:
            self._freeze_phi_records(func)

        # Publish any types the parser already knows. Stack preparation fills
        # the remaining result types through ``publish_value_type``.
        value_id = 0
        while value_id < len(self.value_names):
            # Definitions-first seeds already own authoritative type IDs for
            # arguments, PHIs and supported instruction results.  Re-hashing
            # every spelling through the compatibility ``value_types`` map is
            # both redundant and a large direct-capture owner.  Unknown/cold
            # definitions retain the original lookup and stackprep fallback.
            if self.value_type_ids[value_id] < 0:
                value_type = text_key_mapping_get(
                    func.value_types, self.value_names[value_id]
                )
                if value_type is not None:
                    self.publish_value_type(value_id, value_type)
            value_id += 1
        self._freeze_call_records()
        self._freeze_alloca_records()
        self._freeze_fixed_instruction_records()
        self._freeze_gep_records()
        self._freeze_instruction_payload_ids()
        self._freeze_value_scalar_columns()
        func.indexed_seed = None

    def _intern_build_value(self, name: str) -> int:
        if self.value_name_index_capacity > 0:
            existing_id = _text_id_index_lookup(
                self.value_name_index,
                self.value_name_index_capacity,
                self.value_names,
                name,
            )
            if existing_id >= 0:
                return existing_id
            value_id = len(self.value_names)
            if (value_id + 1) * 2 >= self.value_name_index_capacity:
                old_index = self.value_name_index
                (
                    self.value_name_index,
                    self.value_name_index_capacity,
                ) = _rebuilt_text_id_index(
                    self.value_names,
                    (value_id + 1) * 2,
                )
                old_index.close()
            self.value_names.append(name)
            _text_id_index_insert(
                self.value_name_index,
                self.value_name_index_capacity,
                self.value_names,
                value_id,
            )
        else:
            bucket_key = _stable_text_bucket_key(name)
            bucket = self.value_name_buckets.setdefault(bucket_key, [])
            for existing_name, existing_id in bucket:
                if text_key_names_equal(existing_name, name):
                    return existing_id
            value_id = len(self.value_names)
            self.value_names.append(name)
            bucket.append((name, value_id))
        self.definition_blocks.append(-1)
        self.definition_position_values.append(-3)
        self.value_is_used_flags.append(False)
        self.value_type_ids.append(-1)
        self.value_slot_ids.append(-1)
        self.alloca_offsets.append(-1)
        self.alloca_type_ids.append(-1)
        self.value_registers.append(-1)
        return value_id

    def _define_build_value(
        self,
        name: str,
        block_id: int,
        position: int = -1,
    ) -> int:
        value_id = self._intern_build_value(name)
        # Keep malformed duplicates for the fail-closed verifier's stable
        # ``ssa-definition`` diagnostic.
        if self.definition_position_values[value_id] != -3:
            if self.first_duplicate_definition_value_id < 0:
                self.first_duplicate_definition_value_id = value_id
            self.definition_position_values[value_id] = -2
        else:
            self.definition_position_values[value_id] = position
        self.definition_blocks[value_id] = block_id
        return value_id

    def _index_function_definitions(self, func: ParsedFunction) -> None:
        for arg in func.args:
            value_id = self._define_build_value(arg.name, -2, -1)
            self.publish_value_type(value_id, arg.type)

        for block_id, block in enumerate(func.blocks):
            for phi in block.phis:
                value_id = self._define_build_value(phi.dest, block_id, -1)
                self.publish_value_type(value_id, phi.type)
            arena = block.instructions
            kind_ids = arena._kind_ids
            data_rows = arena._data
            instruction_index = 0
            while instruction_index < len(kind_ids):
                kind_id = kind_ids[instruction_index]
                if not 0 <= kind_id < len(PARSED_INSTRUCTION_KINDS):
                    raise BackendUnavailable(
                        f"corrupt parsed-instruction kind id {kind_id}"
                    )
                data = data_rows[instruction_index]
                call_id = -1
                if (
                    kind_id == PARSED_INSTRUCTION_KIND_CALL
                    and self.indexed_call_plane is not None
                ):
                    call_id = int(data)
                    call_span: CompilerInt4 = self.call_span(call_id)
                    dest = (
                        None
                        if call_span.third < 0
                        else self.call_texts[call_span.third]
                    )
                else:
                    dest = _instruction_defined_value_from_id_parts(
                        kind_id,
                        data,
                    )
                dest_value_id = (
                    -1
                    if dest is None
                    else self._define_build_value(
                        dest,
                        block_id,
                        instruction_index,
                    )
                )
                self.instruction_facts.append4(
                    dest_value_id,
                    0,
                    -1,
                    -1,
                )
                if call_id >= 0:
                    call_header: CompilerInt4 = self.call_header(call_id)
                    if dest_value_id >= 0:
                        self.value_type_ids[dest_value_id] = call_header.first
                    self.call_scalars.set_unchecked(
                        call_id * 8 + 6,
                        dest_value_id,
                    )
                if (
                    kind_id == PARSED_INSTRUCTION_KIND_ALLOCA
                    and dest_value_id >= 0
                ):
                    self.alloca_type_ids[dest_value_id] = self.intern_type(
                        data[1]
                    )
                instruction_index += 1

    def _index_function_uses(self, func: ParsedFunction) -> None:
        first_use_block = [-1] * len(self.value_names)
        last_use_position = [-1] * len(self.value_names)
        use_crosses_blocks = [False] * len(self.value_names)

        def ensure_use_capacity() -> None:
            missing = len(self.value_names) - len(first_use_block)
            if missing <= 0:
                return
            first_use_block.extend([-1] * missing)
            last_use_position.extend([-1] * missing)
            use_crosses_blocks.extend([False] * missing)

        def record_use(name: str, block_id: int, position: int) -> int:
            value_id = self._intern_build_value(name)
            ensure_use_capacity()
            self.value_is_used_flags[value_id] = True
            self.used_value_ids_in_order.append(value_id)
            previous_block = first_use_block[value_id]
            if previous_block < 0:
                first_use_block[value_id] = block_id
                last_use_position[value_id] = position
            elif previous_block != block_id:
                use_crosses_blocks[value_id] = True
            elif position > last_use_position[value_id]:
                last_use_position[value_id] = position
            return value_id

        global_instruction_id = 0
        for block_id, block in enumerate(func.blocks):
            for phi in block.phis:
                for incoming in phi.incoming:
                    if not is_local_value_ref(incoming.value):
                        continue
                    predecessor = self.block_id(incoming.label)
                    if predecessor < 0:
                        raise BackendUnavailable(
                            f"unknown phi predecessor {incoming.label!r}"
                        )
                    record_use(
                        incoming.value,
                        predecessor,
                        len(self.instruction_arenas[predecessor]),
                    )

            arena = block.instructions
            kind_ids = arena._kind_ids
            data_rows = arena._data
            block_instruction_start = global_instruction_id
            instruction_index = 0
            while instruction_index < len(kind_ids):
                kind_id = kind_ids[instruction_index]
                data = data_rows[instruction_index]
                use_count = 0
                first_use_id = -1
                second_use_or_overflow = -1
                if (
                    kind_id == PARSED_INSTRUCTION_KIND_CALL
                    and self.indexed_call_plane is not None
                ):
                    call_id = int(data)
                    call_header: CompilerInt4 = self.call_header(call_id)
                    call_span: CompilerInt4 = self.call_span(call_id)
                    candidate_index = -1 if call_header.third & 1 else 0
                    while candidate_index < call_span.first:
                        arg_id = -1
                        if candidate_index < 0:
                            call_use_text = self.call_texts[call_header.second]
                        else:
                            arg_id = call_header.fourth + candidate_index
                            call_arg: CompilerInt4 = self.call_arg(arg_id)
                            call_use_text = self.call_texts[call_arg.third]
                        candidate_index += 1
                        if not is_local_value_ref(call_use_text):
                            continue
                        indexed_use_id = record_use(
                            call_use_text, block_id, instruction_index
                        )
                        if arg_id >= 0:
                            self.call_arg_scalars.set2_unchecked(
                                arg_id * 4 + 1,
                                indexed_use_id,
                                -1,
                            )
                        if use_count == 0:
                            first_use_id = indexed_use_id
                        elif use_count == 1:
                            second_use_or_overflow = indexed_use_id
                        elif use_count == 2:
                            overflow_start = len(self.instruction_overflow_use_ids)
                            self.instruction_overflow_use_ids.append(
                                second_use_or_overflow
                            )
                            self.instruction_overflow_use_ids.append(indexed_use_id)
                            second_use_or_overflow = -overflow_start - 2
                        else:
                            self.instruction_overflow_use_ids.append(indexed_use_id)
                        use_count += 1
                else:
                    for ordinary_use_text in _instruction_used_values_from_id_parts(
                        kind_id,
                        data,
                    ):
                        indexed_use_id = record_use(
                            ordinary_use_text, block_id, instruction_index
                        )
                        if use_count == 0:
                            first_use_id = indexed_use_id
                        elif use_count == 1:
                            second_use_or_overflow = indexed_use_id
                        elif use_count == 2:
                            overflow_start = len(self.instruction_overflow_use_ids)
                            self.instruction_overflow_use_ids.append(
                                second_use_or_overflow
                            )
                            self.instruction_overflow_use_ids.append(indexed_use_id)
                            second_use_or_overflow = -overflow_start - 2
                        else:
                            self.instruction_overflow_use_ids.append(indexed_use_id)
                        use_count += 1
                self.instruction_facts.set3_unchecked(
                    global_instruction_id * 4 + 1,
                    use_count,
                    first_use_id,
                    second_use_or_overflow,
                )
                self.instruction_use_total += use_count
                instruction_index += 1
                global_instruction_id += 1

            terminator_use_count = 0
            terminator_use_id = -1
            term_position = len(arena)
            for value in terminator_used_values(block.terminator):
                if terminator_use_count != 0:
                    raise BackendUnavailable(
                        "indexed terminator has more than one SSA use"
                    )
                terminator_use_id = record_use(value, block_id, term_position)
                terminator_use_count = 1
            self.block_facts.append4(
                block_instruction_start,
                len(arena),
                terminator_use_count,
                terminator_use_id,
            )

        ensure_use_capacity()
        self.value_last_use_positions = [-1] * len(self.value_names)
        value_id = 0
        while value_id < len(self.value_names):
            use_block = first_use_block[value_id]
            if (
                use_block >= 0
                and not use_crosses_blocks[value_id]
                and self.definition_blocks[value_id] == use_block
            ):
                self.value_last_use_positions[value_id] = last_use_position[value_id]
            value_id += 1

    def _index_seed_function_uses(self, func: ParsedFunction) -> None:
        """Build shared use facts once from parser-owned final records."""

        first_use_block = [-1] * len(self.value_names)
        last_use_position = [-1] * len(self.value_names)
        use_crosses_blocks = [False] * len(self.value_names)

        def ensure_use_capacity() -> None:
            missing = len(self.value_names) - len(first_use_block)
            if missing <= 0:
                return
            first_use_block.extend([-1] * missing)
            last_use_position.extend([-1] * missing)
            use_crosses_blocks.extend([False] * missing)

        def record_use_id(value_id: int, block_id: int, position: int) -> int:
            ensure_use_capacity()
            self.value_is_used_flags[value_id] = True
            self.used_value_ids_in_order.append(value_id)
            previous_block = first_use_block[value_id]
            if previous_block < 0:
                first_use_block[value_id] = block_id
                last_use_position[value_id] = position
            elif previous_block != block_id:
                use_crosses_blocks[value_id] = True
            elif position > last_use_position[value_id]:
                last_use_position[value_id] = position
            return value_id

        def record_use_text(name: str, block_id: int, position: int) -> int:
            value_id = self._intern_build_value(name)
            return record_use_id(value_id, block_id, position)

        global_instruction_id = 0
        block_id = 0
        while block_id < len(self.block_names):
            block_fact: CompilerInt4 = self.block_fact(block_id)
            phi_fact: CompilerInt2 = self.block_phi_fact(block_id)
            phi_index = 0
            while phi_index < phi_fact.second:
                phi: CompilerInt4 = self.phi_record(
                    phi_fact.first + phi_index
                )
                incoming_index = 0
                while incoming_index < phi.fourth:
                    incoming: CompilerInt2 = self.phi_incoming(
                        phi.third + incoming_index
                    )
                    if incoming.first >= 0:
                        predecessor = incoming.second
                        if predecessor < 0:
                            raise BackendUnavailable(
                                "indexed phi has an unknown predecessor"
                            )
                        record_use_id(
                            incoming.first,
                            predecessor,
                            self.block_facts.get_unchecked(
                                predecessor * 4 + 1
                            ),
                        )
                    incoming_index += 1
                phi_index += 1

            instruction_index = 0
            while instruction_index < block_fact.second:
                metadata: CompilerInt4 = self.instruction_metadata_by_id(
                    global_instruction_id
                )
                kind_id = metadata.first
                use_count = 0
                first_use_id = -1
                second_use_or_overflow = -1

                if kind_id == PARSED_INSTRUCTION_KIND_CALL:
                    call_id = metadata.second
                    call_header: CompilerInt4 = self.call_header(call_id)
                    call_span: CompilerInt4 = self.call_span(call_id)
                    callee_text = self.call_texts[call_header.second]
                    candidate_index = -1 if call_header.third & 1 else 0
                    while candidate_index < call_span.first:
                        arg_id = -1
                        call_use_text = callee_text
                        direct_use_id = -1
                        if candidate_index >= 0:
                            arg_id = call_header.fourth + candidate_index
                            call_arg: CompilerInt4 = self.call_arg(arg_id)
                            if call_arg.second >= 0:
                                direct_use_id = call_arg.second
                            else:
                                call_use_text = self.call_texts[call_arg.third]
                        candidate_index += 1
                        if direct_use_id >= 0:
                            indexed_use_id = record_use_id(
                                direct_use_id,
                                block_id,
                                instruction_index,
                            )
                        else:
                            if not is_local_value_ref(call_use_text):
                                continue
                            indexed_use_id = record_use_text(
                                call_use_text,
                                block_id,
                                instruction_index,
                            )
                        if arg_id >= 0 and direct_use_id < 0:
                            self.call_arg_scalars.set2_unchecked(
                                arg_id * 4 + 1,
                                indexed_use_id,
                                -1,
                            )
                        if use_count == 0:
                            first_use_id = indexed_use_id
                        elif use_count == 1:
                            second_use_or_overflow = indexed_use_id
                        elif use_count == 2:
                            overflow_start = len(self.instruction_overflow_use_ids)
                            self.instruction_overflow_use_ids.append(
                                second_use_or_overflow
                            )
                            self.instruction_overflow_use_ids.append(indexed_use_id)
                            second_use_or_overflow = -overflow_start - 2
                        else:
                            self.instruction_overflow_use_ids.append(indexed_use_id)
                        use_count += 1
                elif kind_id == PARSED_INSTRUCTION_KIND_GEP:
                    gep_header: CompilerInt4 = self.gep_header(metadata.second)
                    gep_span: CompilerInt4 = self.gep_span(metadata.second)
                    candidate_index = -1
                    while candidate_index < gep_span.first:
                        if candidate_index < 0:
                            value_ref = gep_header.third
                        else:
                            gep_index: CompilerInt2 = self.gep_index(
                                gep_header.fourth + candidate_index
                            )
                            value_ref = gep_index.second
                        candidate_index += 1
                        if value_ref < 0:
                            continue
                        indexed_use_id = record_use_id(
                            value_ref,
                            block_id,
                            instruction_index,
                        )
                        if use_count == 0:
                            first_use_id = indexed_use_id
                        elif use_count == 1:
                            second_use_or_overflow = indexed_use_id
                        elif use_count == 2:
                            overflow_start = len(self.instruction_overflow_use_ids)
                            self.instruction_overflow_use_ids.append(
                                second_use_or_overflow
                            )
                            self.instruction_overflow_use_ids.append(indexed_use_id)
                            second_use_or_overflow = -overflow_start - 2
                        else:
                            self.instruction_overflow_use_ids.append(indexed_use_id)
                        use_count += 1
                elif kind_id in _PACKED_FIXED_KIND_IDS:
                    record: CompilerInt4 = self.instruction_record(metadata.second)
                    if (
                        kind_id == PARSED_INSTRUCTION_KIND_LOAD
                        or kind_id == PARSED_INSTRUCTION_KIND_CAST
                    ):
                        candidate_count = 1
                        candidate0 = record.third
                        candidate1 = -1
                        candidate2 = -1
                    elif kind_id == PARSED_INSTRUCTION_KIND_STORE:
                        candidate_count = 2
                        candidate0 = record.second
                        candidate1 = record.fourth
                        candidate2 = -1
                    elif (
                        kind_id == PARSED_INSTRUCTION_KIND_ICMP
                        or kind_id == PARSED_INSTRUCTION_KIND_BINOP
                    ):
                        candidate_count = 2
                        candidate0 = record.third
                        candidate1 = record.fourth
                        candidate2 = -1
                    else:
                        candidate_count = 3
                        candidate0 = record.second
                        candidate1 = record.third
                        candidate2 = record.fourth
                    candidate_index = 0
                    while candidate_index < candidate_count:
                        if candidate_index == 0:
                            value_ref = candidate0
                        elif candidate_index == 1:
                            value_ref = candidate1
                        else:
                            value_ref = candidate2
                        candidate_index += 1
                        if value_ref < 0:
                            continue
                        indexed_use_id = record_use_id(
                            value_ref,
                            block_id,
                            instruction_index,
                        )
                        if use_count == 0:
                            first_use_id = indexed_use_id
                        elif use_count == 1:
                            second_use_or_overflow = indexed_use_id
                        elif use_count == 2:
                            overflow_start = len(self.instruction_overflow_use_ids)
                            self.instruction_overflow_use_ids.append(
                                second_use_or_overflow
                            )
                            self.instruction_overflow_use_ids.append(indexed_use_id)
                            second_use_or_overflow = -overflow_start - 2
                        else:
                            self.instruction_overflow_use_ids.append(indexed_use_id)
                        use_count += 1
                elif kind_id != PARSED_INSTRUCTION_KIND_ALLOCA:
                    cold_data = self.cold_instruction_data[-metadata.second - 1]
                    for cold_use_text in _instruction_used_values_from_id_parts(
                        kind_id,
                        cold_data,
                    ):
                        indexed_use_id = record_use_text(
                            cold_use_text,
                            block_id,
                            instruction_index,
                        )
                        if use_count == 0:
                            first_use_id = indexed_use_id
                        elif use_count == 1:
                            second_use_or_overflow = indexed_use_id
                        elif use_count == 2:
                            overflow_start = len(self.instruction_overflow_use_ids)
                            self.instruction_overflow_use_ids.append(
                                second_use_or_overflow
                            )
                            self.instruction_overflow_use_ids.append(indexed_use_id)
                            second_use_or_overflow = -overflow_start - 2
                        else:
                            self.instruction_overflow_use_ids.append(indexed_use_id)
                        use_count += 1

                self.instruction_facts.set3_unchecked(
                    global_instruction_id * 4 + 1,
                    use_count,
                    first_use_id,
                    second_use_or_overflow,
                )
                self.instruction_use_total += use_count
                instruction_index += 1
                global_instruction_id += 1

            terminator_use_count = 0
            terminator_use_id = -1
            term_position = block_fact.second
            term_header: CompilerInt4 = self.terminator_header(block_id)
            if (
                (
                    term_header.first == PARSED_INSTRUCTION_KIND_RET
                    or term_header.first == PARSED_INSTRUCTION_KIND_BR_COND
                    or term_header.first == PARSED_INSTRUCTION_KIND_SWITCH
                )
                and term_header.third >= 0
            ):
                terminator_use_id = record_use_id(
                    term_header.third,
                    block_id,
                    term_position,
                )
                terminator_use_count = 1
            self.block_facts.set2_unchecked(
                block_id * 4 + 2,
                terminator_use_count,
                terminator_use_id,
            )
            edge_span: CompilerInt2 = self.inline_error_edge_span(block_id)
            edge_offset = 0
            while edge_offset < edge_span.second:
                edge_id = edge_span.first + edge_offset
                record_use_id(
                    self.inline_error_edge_condition(edge_id),
                    block_id,
                    self.inline_error_edge_trigger(edge_id) + 1,
                )
                self.instruction_use_total += 1
                edge_offset += 1
            block_id += 1

        ensure_use_capacity()
        self.value_last_use_positions = [-1] * len(self.value_names)
        value_id = 0
        while value_id < len(self.value_names):
            use_block = first_use_block[value_id]
            if (
                use_block >= 0
                and not use_crosses_blocks[value_id]
                and self.definition_blocks[value_id] == use_block
            ):
                self.value_last_use_positions[value_id] = (
                    last_use_position[value_id]
                )
            value_id += 1

    def _freeze_value_scalar_columns(self) -> None:
        value_id = 0
        while value_id < len(self.value_names):
            self.value_scalars.append4(
                self.definition_blocks[value_id],
                self.value_type_ids[value_id],
                self.value_slot_ids[value_id],
                self.alloca_offsets[value_id],
            )
            self.value_scalars.append4(
                self.alloca_type_ids[value_id],
                self.value_registers[value_id],
                self.value_last_use_positions[value_id],
                1 if self.value_is_used_flags[value_id] else 0,
            )
            self.definition_positions.append(
                self.definition_position_values[value_id]
            )
            value_id += 1
        for used_value_id in self.used_value_ids_in_order:
            self.used_value_ids.append(used_value_id)
        self.definition_blocks = _EMPTY_SEQUENCE
        self.definition_position_values = _EMPTY_SEQUENCE
        self.value_type_ids = _EMPTY_SEQUENCE
        self.value_slot_ids = _EMPTY_SEQUENCE
        self.alloca_offsets = _EMPTY_SEQUENCE
        self.alloca_type_ids = _EMPTY_SEQUENCE
        self.value_registers = _EMPTY_SEQUENCE
        self.value_last_use_positions = _EMPTY_SEQUENCE
        self.value_is_used_flags = _EMPTY_SEQUENCE
        self.used_value_ids_in_order = _EMPTY_SEQUENCE
        self.slot_offsets = _EMPTY_SEQUENCE
        self.slot_type_ids = _EMPTY_SEQUENCE

    def value_header(self, value_id: int) -> CompilerInt4:
        return self.value_scalars.get4_unchecked(value_id * 2)

    def value_state(self, value_id: int) -> CompilerInt4:
        return self.value_scalars.get4_unchecked(value_id * 2 + 1)

    def definition_position(self, value_id: int) -> int:
        return self.definition_positions.get_unchecked(value_id)

    def slot_record(self, slot_id: int) -> CompilerInt2:
        return self.slot_scalars.get2_unchecked(slot_id)

    def reset_block_layout(self) -> None:
        self.block_layout_ids.close()
        self.block_layout_ids = CompilerIntArena()

    def _freeze_phi_records(self, func: ParsedFunction) -> None:
        text_ids: dict[str, int] = {}
        text_id = 0
        while text_id < len(self.call_texts):
            text_ids[self.call_texts[text_id]] = text_id
            text_id += 1

        def intern_text(text: str) -> int:
            existing = text_ids.get(text)
            if existing is not None:
                return existing
            result = len(self.call_texts)
            self.call_texts.append(text)
            text_ids[text] = result
            return result

        def operand_ref(text: str) -> int:
            value_id = self.value_id(text)
            if value_id >= 0:
                return value_id
            return -intern_text(text) - 1

        block_id = 0
        while block_id < len(func.blocks):
            block = func.blocks[block_id]
            phi_start = len(self.phi_scalars) // 4
            phi_count = 0
            all_scalar = True
            for phi in block.phis:
                incoming_start = len(self.phi_incoming_scalars) // 2
                incoming_count = 0
                for incoming in phi.incoming:
                    self.phi_incoming_scalars.append2(
                        operand_ref(incoming.value),
                        self.block_id(incoming.label),
                    )
                    incoming_count += 1
                phi_type_id = self.intern_type(phi.type)
                phi_type_header: CompilerInt4 = self.type_header(phi_type_id)
                phi_kind = phi_type_header.first
                if phi_kind == TYPE_KIND_ARRAY or phi_kind == TYPE_KIND_STRUCT:
                    all_scalar = False
                self.phi_scalars.append4(
                    self.value_id(phi.dest),
                    phi_type_id,
                    incoming_start,
                    incoming_count,
                )
                phi_count += 1
            self.block_phi_facts.append2(phi_start, phi_count)
            block_id += 1

    def release_scalar_phi_projections(self, func: ParsedFunction) -> None:
        block_id = 0
        while block_id < len(func.blocks):
            phi_fact: CompilerInt2 = self.block_phi_fact(block_id)
            all_scalar = True
            phi_index = 0
            while phi_index < phi_fact.second:
                phi: CompilerInt4 = self.phi_record(
                    phi_fact.first + phi_index
                )
                phi_type_header: CompilerInt4 = self.type_header(phi.second)
                kind_id = phi_type_header.first
                if kind_id == TYPE_KIND_ARRAY or kind_id == TYPE_KIND_STRUCT:
                    all_scalar = False
                    break
                phi_index += 1
            if all_scalar:
                func.blocks[block_id].phis = ()
            block_id += 1

    def block_phi_fact(self, block_id: int) -> CompilerInt2:
        return self.block_phi_facts.get2_unchecked(block_id)

    def phi_record(self, phi_id: int) -> CompilerInt4:
        return self.phi_scalars.get4_unchecked(phi_id)

    def phi_incoming(self, incoming_id: int) -> CompilerInt2:
        return self.phi_incoming_scalars.get2_unchecked(incoming_id)

    def phi_incoming_value(self, value_ref: int) -> str:
        if value_ref >= 0:
            return self.value_name(value_ref)
        return self.call_texts[-value_ref - 1]

    def diagnostic_phi(self, block_id: int, phi_index: int) -> PhiInstr:
        self.phi_diagnostic_projections += 1
        block_fact: CompilerInt2 = self.block_phi_fact(block_id)
        raw: CompilerInt4 = self.phi_record(block_fact.first + phi_index)
        incoming: list[PhiIncoming] = []
        index = 0
        while index < raw.fourth:
            item: CompilerInt2 = self.phi_incoming(raw.third + index)
            incoming.append(
                PhiIncoming(
                    self.phi_incoming_value(item.first),
                    self.block_names[item.second],
                )
            )
            index += 1
        return PhiInstr(
            self.value_name(raw.first),
            self.type_desc(raw.second),
            tuple(incoming),
        )

    def _freeze_terminator_records(self, func: ParsedFunction) -> None:
        text_ids: dict[str, int] = {}
        text_id = 0
        while text_id < len(self.call_texts):
            text_ids[self.call_texts[text_id]] = text_id
            text_id += 1

        def intern_text(text: str) -> int:
            existing = text_ids.get(text)
            if existing is not None:
                return existing
            result = len(self.call_texts)
            self.call_texts.append(text)
            text_ids[text] = result
            return result

        def operand_ref(text: str) -> int:
            value_id = self.value_id(text)
            if value_id >= 0:
                return value_id
            return -intern_text(text) - 1

        def block_ref(text: str) -> int:
            block_id = self.block_id(text)
            if block_id >= 0:
                return block_id
            return -intern_text(text) - 1

        block_id = 0
        while block_id < len(func.blocks):
            term = func.blocks[block_id].terminator
            if term is None:
                raise BackendUnavailable("indexed block has no terminator")
            kind_id = _PARSED_INSTRUCTION_KIND_IDS.get(term.kind)
            if kind_id is None:
                raise BackendUnavailable(
                    f"unknown parsed terminator kind {term.kind!r}"
                )
            type_id = -1
            value_ref = -1
            target0 = -1
            target1 = -1
            case_start = len(self.terminator_case_scalars) // 2
            case_count = 0
            if term.kind == "ret":
                value_type, value = term.data
                type_id = self.intern_type(value_type)
                value_ref = operand_ref(value)
            elif term.kind == "br":
                target0 = block_ref(term.data[0])
            elif term.kind == "br_cond":
                condition, true_target, false_target = term.data
                value_ref = operand_ref(condition)
                target0 = block_ref(true_target)
                target1 = block_ref(false_target)
            elif term.kind == "switch":
                value_type, value, default_target, cases = term.data
                type_id = self.intern_type(value_type)
                value_ref = operand_ref(value)
                target0 = block_ref(default_target)
                for case_value, case_target in cases:
                    self.terminator_case_scalars.append2(
                        case_value,
                        block_ref(case_target),
                    )
                    case_count += 1
            self.terminator_scalars.append4(
                kind_id,
                type_id,
                value_ref,
                target0,
            )
            self.terminator_scalars.append4(
                target1,
                case_start,
                case_count,
                0,
            )
            block_id += 1

    def terminator_header(self, block_id: int) -> CompilerInt4:
        return self.terminator_scalars.get4_unchecked(block_id * 2)

    def terminator_span(self, block_id: int) -> CompilerInt4:
        return self.terminator_scalars.get4_unchecked(block_id * 2 + 1)

    def terminator_case(self, case_id: int) -> CompilerInt2:
        return self.terminator_case_scalars.get2_unchecked(case_id)

    def terminator_value(self, value_ref: int) -> str:
        if value_ref >= 0:
            return self.value_name(value_ref)
        return self.call_texts[-value_ref - 1]

    def inline_error_edge_span(self, block_id: int) -> CompilerInt2:
        return self.error_edge_spans.get2_unchecked(block_id)

    def inline_error_edge_source_block(self, edge_id: int) -> int:
        return self.error_edge_scalars.get_unchecked(
            edge_id * INLINE_ERROR_EDGE_WIDTH
        )

    def inline_error_edge_trigger(self, edge_id: int) -> int:
        return self.error_edge_scalars.get_unchecked(
            edge_id * INLINE_ERROR_EDGE_WIDTH + 1
        )

    def inline_error_edge_condition(self, edge_id: int) -> int:
        return self.error_edge_scalars.get_unchecked(
            edge_id * INLINE_ERROR_EDGE_WIDTH + 2
        )

    def inline_error_edge_target(self, edge_id: int) -> int:
        return self.error_edge_scalars.get_unchecked(
            edge_id * INLINE_ERROR_EDGE_WIDTH + 3
        )

    def inline_error_edge_source_line(self, edge_id: int) -> int:
        return self.error_edge_scalars.get_unchecked(
            edge_id * INLINE_ERROR_EDGE_WIDTH + 4
        )

    def inline_error_edge_cleanup_plan(self, edge_id: int) -> int:
        return self.error_edge_scalars.get_unchecked(
            edge_id * INLINE_ERROR_EDGE_WIDTH + 5
        )

    def inline_error_edge_payload(self, edge_id: int) -> int:
        """Landing payload index, or -1 when the target keeps no payload."""
        return self.error_edge_scalars.get_unchecked(
            edge_id * INLINE_ERROR_EDGE_WIDTH + 6
        )

    def inline_error_landing_slot(self, block_id: int) -> int:
        """The i32 alloca value a shared frame landing reads, or -1."""
        index = 0
        count = len(self.error_landing_scalars) // 2
        while index < count:
            pair: CompilerInt2 = self.error_landing_scalars.get2_unchecked(index)
            if pair.first == block_id:
                return pair.second
            index += 1
        return -1

    def cfg_successor_count(self, block_id: int) -> int:
        edge_span: CompilerInt2 = self.inline_error_edge_span(block_id)
        return self.terminator_successor_count(block_id) + edge_span.second

    def cfg_successor_id(self, block_id: int, successor_index: int) -> int:
        terminator_count = self.terminator_successor_count(block_id)
        if successor_index < terminator_count:
            return self.terminator_successor_id(block_id, successor_index)
        edge_span: CompilerInt2 = self.inline_error_edge_span(block_id)
        edge_offset = successor_index - terminator_count
        if edge_offset < 0 or edge_offset >= edge_span.second:
            raise IndexError(successor_index)
        return self.inline_error_edge_target(edge_span.first + edge_offset)

    def terminator_successor_count(self, block_id: int) -> int:
        header: CompilerInt4 = self.terminator_header(block_id)
        if header.first == PARSED_INSTRUCTION_KIND_BR:
            return 1
        if header.first == PARSED_INSTRUCTION_KIND_BR_COND:
            return 2
        if header.first == PARSED_INSTRUCTION_KIND_SWITCH:
            span: CompilerInt4 = self.terminator_span(block_id)
            return 1 + span.third
        return 0

    def terminator_successor_id(
        self,
        block_id: int,
        successor_index: int,
    ) -> int:
        header: CompilerInt4 = self.terminator_header(block_id)
        span: CompilerInt4 = self.terminator_span(block_id)
        kind_id = header.first
        if successor_index == 0 and (
            kind_id == PARSED_INSTRUCTION_KIND_BR
            or kind_id == PARSED_INSTRUCTION_KIND_BR_COND
            or kind_id == PARSED_INSTRUCTION_KIND_SWITCH
        ):
            return header.fourth
        if successor_index == 1 and kind_id == PARSED_INSTRUCTION_KIND_BR_COND:
            return span.first
        if (
            kind_id == PARSED_INSTRUCTION_KIND_SWITCH
            and 1 <= successor_index <= span.third
        ):
            case: CompilerInt2 = self.terminator_case(
                span.second + successor_index - 1
            )
            return case.second
        raise IndexError(successor_index)

    def terminator_successor_name(
        self,
        block_id: int,
        successor_index: int,
    ) -> str:
        target_ref = self.terminator_successor_id(block_id, successor_index)
        if target_ref >= 0:
            return self.block_names[target_ref]
        return self.call_texts[-target_ref - 1]

    def diagnostic_terminator(self, block_id: int):
        self.terminator_diagnostic_projections += 1
        header: CompilerInt4 = self.terminator_header(block_id)
        span: CompilerInt4 = self.terminator_span(block_id)
        kind = PARSED_INSTRUCTION_KINDS[header.first]
        data: tuple = ()
        if kind == "ret":
            data = (
                self.type_desc(header.second),
                self.terminator_value(header.third),
            )
        elif kind == "br":
            data = (self.block_names[header.fourth],)
        elif kind == "br_cond":
            data = (
                self.terminator_value(header.third),
                self.block_names[header.fourth],
                self.block_names[span.first],
            )
        elif kind == "switch":
            cases: list[tuple[int, str]] = []
            case_index = 0
            while case_index < span.third:
                case: CompilerInt2 = self.terminator_case(
                    span.second + case_index
                )
                cases.append((case.first, self.block_names[case.second]))
                case_index += 1
            data = (
                self.type_desc(header.second),
                self.terminator_value(header.third),
                self.block_names[header.fourth],
                tuple(cases),
            )
        return ParsedInstr(kind, data)

    def release_terminator_projections(self, func: ParsedFunction) -> None:
        block_id = 0
        while block_id < len(func.blocks):
            func.blocks[block_id].terminator = None
            block_id += 1

    def supported_object_projection_is_closed(
        self,
        func: ParsedFunction,
    ) -> bool:
        scalar_kinds = (TYPE_KIND_VOID, TYPE_KIND_INT, TYPE_KIND_FP, TYPE_KIND_PTR)
        return_type_id = -1
        type_id = 0
        while type_id < len(self.types):
            if self.types[type_id] == func.ret_type:
                return_type_id = type_id
                break
            type_id += 1
        if return_type_id < 0:
            return False
        return_type_header: CompilerInt4 = self.type_header(return_type_id)
        if return_type_header.first not in scalar_kinds:
            return False
        for arg in func.args:
            value_id = self.value_id(arg.name)
            arg_type_id = -1 if value_id < 0 else self.value_type_id(value_id)
            if arg_type_id < 0:
                return False
            arg_type_header: CompilerInt4 = self.type_header(arg_type_id)
            if arg_type_header.first not in scalar_kinds:
                return False
        block_id = 0
        while block_id < len(self.block_names):
            block_fact: CompilerInt4 = self.block_fact(block_id)
            phi_fact: CompilerInt2 = self.block_phi_fact(block_id)
            phi_index = 0
            while phi_index < phi_fact.second:
                phi: CompilerInt4 = self.phi_record(
                    phi_fact.first + phi_index
                )
                phi_type_header: CompilerInt4 = self.type_header(phi.second)
                if phi_type_header.first not in scalar_kinds:
                    return False
                phi_index += 1
            term_header: CompilerInt4 = self.terminator_header(block_id)
            if term_header.first not in _SUPPORTED_SCALAR_TERMINATOR_KIND_IDS:
                return False
            instruction_index = 0
            while instruction_index < block_fact.second:
                metadata: CompilerInt4 = self.instruction_metadata_by_id(
                    block_fact.first + instruction_index
                )
                if metadata.first not in _SUPPORTED_SCALAR_INSTRUCTION_KIND_IDS:
                    return False
                instruction_index += 1
            block_id += 1
        return True

    def _freeze_instruction_payload_ids(self) -> None:
        if isinstance(self.indexed_call_plane, IndexedFunctionSeed):
            expected_instruction_id = len(self.instruction_kind_ids)
            if expected_instruction_id * 4 != len(self.instruction_metadata):
                raise BackendUnavailable(
                    "indexed function seed metadata count is inconsistent"
                )
            instruction_id = 0
            while instruction_id < expected_instruction_id:
                if self.instruction_kind_ids.get_unchecked(instruction_id) != (
                    self.instruction_metadata.get_unchecked(instruction_id * 4)
                ):
                    raise BackendUnavailable(
                        "indexed function seed kind lane is inconsistent"
                    )
                instruction_id += 1
            self._finalize_indexed_call_plane()
            return
        block_id = 0
        while block_id < len(self.instruction_arenas):
            arena = self.instruction_arenas[block_id]
            payload_start = len(self.instruction_metadata) // 4
            all_packed = True
            instruction_index = 0
            while instruction_index < len(arena._kind_ids):
                raw = arena._data[instruction_index]
                kind_id = arena._kind_ids[instruction_index]
                volatile = arena._volatile[instruction_index]
                arithmetic_flags = arena._arithmetic_flags[instruction_index]
                if arena.has_arithmetic_flags(instruction_index):
                    self.instruction_arithmetic_flag_values[
                        payload_start + instruction_index
                    ] = arena.arithmetic_flags(instruction_index)
                if isinstance(raw, int):
                    payload_id = raw
                else:
                    payload_id = -1
                    all_packed = False
                self.instruction_metadata.append4(
                    kind_id,
                    payload_id,
                    volatile,
                    arithmetic_flags,
                )
                instruction_index += 1
            if all_packed:
                arena.freeze_payload_ids(self, payload_start)
            block_id += 1

    def instruction_metadata_by_id(self, instruction_id: int) -> CompilerInt4:
        return self.instruction_metadata.get4_unchecked(instruction_id)

    def instruction_payload_id_by_id(self, instruction_id: int) -> int:
        metadata: CompilerInt4 = self.instruction_metadata_by_id(instruction_id)
        return metadata.second

    def _freeze_gep_records(self) -> None:
        if isinstance(self.indexed_call_plane, IndexedFunctionSeed):
            return
        from .self_backend_parse import gep_result_type

        text_ids: dict[str, int] = {}
        text_id = 0
        while text_id < len(self.call_texts):
            text_ids[self.call_texts[text_id]] = text_id
            text_id += 1

        def intern_text(text: str) -> int:
            existing = text_ids.get(text)
            if existing is not None:
                return existing
            result = len(self.call_texts)
            self.call_texts.append(text)
            text_ids[text] = result
            return result

        def operand_ref(text: str) -> int:
            value_id = self.value_id(text)
            if value_id >= 0:
                return value_id
            return -intern_text(text) - 1

        block_id = 0
        while block_id < len(self.instruction_arenas):
            arena = self.instruction_arenas[block_id]
            instruction_index = 0
            while instruction_index < len(arena._kind_ids):
                kind_id = arena._kind_ids[instruction_index]
                if kind_id != PARSED_INSTRUCTION_KIND_GEP:
                    instruction_index += 1
                    continue
                data = arena._data[instruction_index]
                _dest, base_type, ptr_type, ptr, indices = data
                record_id = len(self.gep_scalars) // 8
                index_start = len(self.gep_index_scalars) // 2
                index_count = 0
                for index_type, index_value in indices:
                    self.gep_index_scalars.append2(
                        self.intern_type(index_type),
                        operand_ref(index_value),
                    )
                    index_count += 1
                dest_id = self.defined_value_id(block_id, instruction_index)
                result_type_id = self.intern_type(
                    gep_result_type(base_type, indices)
                )
                self.gep_scalars.append4(
                    self.intern_type(base_type),
                    self.intern_type(ptr_type),
                    operand_ref(ptr),
                    index_start,
                )
                self.gep_scalars.append4(
                    index_count,
                    result_type_id,
                    dest_id,
                    0,
                )
                if dest_id >= 0:
                    self.value_type_ids[dest_id] = result_type_id
                arena._data[instruction_index] = record_id
                arena._call_projector = self
                instruction_index += 1
            block_id += 1

    def gep_header(self, record_id: int) -> CompilerInt4:
        return self.gep_scalars.get4_unchecked(record_id * 2)

    def gep_span(self, record_id: int) -> CompilerInt4:
        return self.gep_scalars.get4_unchecked(record_id * 2 + 1)

    def gep_index(self, index_id: int) -> CompilerInt2:
        return self.gep_index_scalars.get2_unchecked(index_id)

    def diagnostic_gep_data(self, record_id: int) -> tuple:
        self.diagnostic_projections += 1
        header: CompilerInt4 = self.gep_header(record_id)
        span: CompilerInt4 = self.gep_span(record_id)
        ptr = (
            self.value_name(header.third)
            if header.third >= 0
            else self.call_texts[-header.third - 1]
        )
        indices: list[tuple[TypeDesc, str]] = []
        index = 0
        while index < span.first:
            raw: CompilerInt2 = self.gep_index(header.fourth + index)
            value = (
                self.value_name(raw.second)
                if raw.second >= 0
                else self.call_texts[-raw.second - 1]
            )
            indices.append((self.type_desc(raw.first), value))
            index += 1
        return (
            self.value_name(span.third),
            self.type_desc(header.first),
            self.type_desc(header.second),
            ptr,
            tuple(indices),
        )

    def _freeze_fixed_instruction_records(self) -> None:
        if isinstance(self.indexed_call_plane, IndexedFunctionSeed):
            return
        text_ids: dict[str, int] = {}
        text_id = 0
        while text_id < len(self.call_texts):
            text_ids[self.call_texts[text_id]] = text_id
            text_id += 1

        def intern_text(text: str) -> int:
            existing = text_ids.get(text)
            if existing is not None:
                return existing
            result = len(self.call_texts)
            self.call_texts.append(text)
            text_ids[text] = result
            return result

        def operand_ref(text: str) -> int:
            value_id = self.value_id(text)
            if value_id >= 0:
                return value_id
            return -intern_text(text) - 1

        block_id = 0
        while block_id < len(self.instruction_arenas):
            arena = self.instruction_arenas[block_id]
            instruction_index = 0
            while instruction_index < len(arena._kind_ids):
                kind_id = arena._kind_ids[instruction_index]
                if kind_id not in _PACKED_FIXED_KIND_IDS:
                    instruction_index += 1
                    continue
                data = arena._data[instruction_index]
                record_id = len(self.instruction_record_scalars) // 4
                indexed_block: CompilerInt4 = self.block_fact(block_id)
                instruction_id = indexed_block.first + instruction_index
                indexed_instruction: CompilerInt4 = (
                    self.instruction_fact_by_id(instruction_id)
                )
                dest_id = indexed_instruction.first
                result_type_id = -1
                if kind_id == PARSED_INSTRUCTION_KIND_LOAD:
                    _dest, value_type, ptr_type, ptr = data
                    result_type_id = self.intern_type(value_type)
                    self.instruction_record_scalars.append4(
                        result_type_id,
                        self.intern_type(ptr_type),
                        operand_ref(ptr),
                        0,
                    )
                elif kind_id == PARSED_INSTRUCTION_KIND_STORE:
                    value_type, value, ptr_type, ptr = data
                    self.instruction_record_scalars.append4(
                        self.intern_type(value_type),
                        operand_ref(value),
                        self.intern_type(ptr_type),
                        operand_ref(ptr),
                    )
                elif kind_id == PARSED_INSTRUCTION_KIND_CAST:
                    op, _dest, src_type, source, dst_type = data
                    result_type_id = self.intern_type(dst_type)
                    self.instruction_record_scalars.append4(
                        intern_text(op),
                        self.intern_type(src_type),
                        operand_ref(source),
                        result_type_id,
                    )
                elif (
                    kind_id == PARSED_INSTRUCTION_KIND_ICMP
                    or kind_id == PARSED_INSTRUCTION_KIND_BINOP
                ):
                    op, _dest, value_type, lhs, rhs = data
                    value_type_id = self.intern_type(value_type)
                    result_type_id = value_type_id
                    if kind_id == PARSED_INSTRUCTION_KIND_ICMP:
                        result_type_id = self.intern_type(
                            TypeDesc(
                                "array",
                                count=value_type.count,
                                elem=I1,
                            )
                            if value_type.is_array
                            and value_type.elem is not None
                            else I1
                        )
                    self.instruction_record_scalars.append4(
                        intern_text(op),
                        value_type_id,
                        operand_ref(lhs),
                        operand_ref(rhs),
                    )
                else:
                    (
                        _dest,
                        result_type,
                        condition,
                        true_value,
                        false_value,
                    ) = data
                    result_type_id = self.intern_type(result_type)
                    self.instruction_record_scalars.append4(
                        result_type_id,
                        operand_ref(condition),
                        operand_ref(true_value),
                        operand_ref(false_value),
                    )
                self.instruction_record_dest_ids.append(dest_id)
                if dest_id >= 0 and result_type_id >= 0:
                    self.value_type_ids[dest_id] = result_type_id
                arena._data[instruction_index] = record_id
                arena._call_projector = self
                instruction_index += 1
            block_id += 1

    def instruction_record(self, record_id: int) -> CompilerInt4:
        return self.instruction_record_scalars.get4_unchecked(record_id)

    def diagnostic_fixed_instruction_data(
        self,
        kind_id: int,
        record_id: int,
    ) -> tuple:
        self.diagnostic_projections += 1
        raw: CompilerInt4 = self.instruction_record(record_id)
        dest_id = self.instruction_record_dest_ids.get_unchecked(record_id)
        dest = None if dest_id < 0 else self.value_name(dest_id)

        def operand_text(operand: int) -> str:
            if operand >= 0:
                return self.value_name(operand)
            return self.call_texts[-operand - 1]

        if kind_id == PARSED_INSTRUCTION_KIND_LOAD:
            return (
                dest,
                self.type_desc(raw.first),
                self.type_desc(raw.second),
                operand_text(raw.third),
            )
        if kind_id == PARSED_INSTRUCTION_KIND_STORE:
            return (
                self.type_desc(raw.first),
                operand_text(raw.second),
                self.type_desc(raw.third),
                operand_text(raw.fourth),
            )
        if kind_id == PARSED_INSTRUCTION_KIND_CAST:
            return (
                self.call_texts[raw.first],
                dest,
                self.type_desc(raw.second),
                operand_text(raw.third),
                self.type_desc(raw.fourth),
            )
        if (
            kind_id == PARSED_INSTRUCTION_KIND_ICMP
            or kind_id == PARSED_INSTRUCTION_KIND_BINOP
        ):
            return (
                self.call_texts[raw.first],
                dest,
                self.type_desc(raw.second),
                operand_text(raw.third),
                operand_text(raw.fourth),
            )
        return (
            dest,
            self.type_desc(raw.first),
            operand_text(raw.second),
            operand_text(raw.third),
            operand_text(raw.fourth),
        )

    def _freeze_alloca_records(self) -> None:
        if isinstance(self.indexed_call_plane, IndexedFunctionSeed):
            return
        block_id = 0
        while block_id < len(self.instruction_arenas):
            arena = self.instruction_arenas[block_id]
            instruction_index = 0
            while instruction_index < len(arena._kind_ids):
                kind_id = arena._kind_ids[instruction_index]
                if kind_id == PARSED_INSTRUCTION_KIND_ALLOCA:
                    indexed_block: CompilerInt4 = self.block_fact(block_id)
                    instruction_id = indexed_block.first + instruction_index
                    indexed_instruction: CompilerInt4 = (
                        self.instruction_fact_by_id(instruction_id)
                    )
                    dest_id = indexed_instruction.first
                    allocated_type_id = self.alloca_type_id(dest_id)
                    self.value_type_ids[dest_id] = self.intern_type(
                        self.types[allocated_type_id].ptr()
                    )
                    arena._data[instruction_index] = dest_id
                    arena._call_projector = self
                instruction_index += 1
            block_id += 1

    def diagnostic_alloca_data(self, value_id: int) -> tuple:
        self.diagnostic_projections += 1
        return (
            self.value_name(value_id),
            self.type_desc(self.alloca_type_id(value_id)),
        )

    def _freeze_call_records(self) -> None:
        if isinstance(self.indexed_call_plane, IndexedFunctionSeed):
            return
        if self.indexed_call_plane is not None:
            self._finalize_indexed_call_plane()
            return
        text_ids: dict[str, int] = {}

        def intern_text(text: str) -> int:
            existing = text_ids.get(text)
            if existing is not None:
                return existing
            text_id = len(self.call_texts)
            self.call_texts.append(text)
            text_ids[text] = text_id
            return text_id

        block_id = 0
        while block_id < len(self.instruction_arenas):
            arena = self.instruction_arenas[block_id]
            instruction_index = 0
            while instruction_index < len(arena._kind_ids):
                kind_id = arena._kind_ids[instruction_index]
                if kind_id != PARSED_INSTRUCTION_KIND_CALL:
                    instruction_index += 1
                    continue
                data = arena._data[instruction_index]
                (
                    _dest,
                    ret_type,
                    callee,
                    is_indirect,
                    args,
                    fixed_arg_count,
                    is_vararg,
                    arg_alignments,
                ) = data
                if len(args) != len(arg_alignments):
                    raise BackendUnavailable(
                        "call argument alignment count does not match arguments"
                    )
                call_id = len(self.call_scalars) // 8
                arg_start = len(self.call_arg_scalars) // 4
                arg_index = 0
                while arg_index < len(args):
                    arg_type, arg_value = args[arg_index]
                    local_value_id = self.value_id(arg_value)
                    text_id = (
                        -1
                        if local_value_id >= 0
                        else intern_text(arg_value)
                    )
                    self.call_arg_scalars.append4(
                        self.intern_type(arg_type),
                        local_value_id,
                        text_id,
                        arg_alignments[arg_index],
                    )
                    arg_index += 1
                indexed_block: CompilerInt4 = self.block_fact(block_id)
                instruction_id = indexed_block.first + instruction_index
                indexed_instruction: CompilerInt4 = self.instruction_fact_by_id(
                    instruction_id
                )
                dest_value_id = indexed_instruction.first
                flags = classify_call_flags(callee, is_indirect, is_vararg)
                ret_type_id = self.intern_type(ret_type)
                self.call_scalars.append4(
                    ret_type_id,
                    intern_text(callee),
                    flags,
                    arg_start,
                )
                self.call_scalars.append4(
                    len(args),
                    fixed_arg_count,
                    dest_value_id,
                    0,
                )
                if dest_value_id >= 0:
                    self.value_type_ids[dest_value_id] = ret_type_id
                arena._data[instruction_index] = call_id
                arena._call_projector = self
                instruction_index += 1
            block_id += 1

    def _finalize_indexed_call_plane(self) -> None:
        plane = self.indexed_call_plane
        if plane is None:
            return
        if isinstance(plane, IndexedFunctionSeed):
            plane.release_construction_indexes()
            self.indexed_call_plane = None
            return
        for arena in self.instruction_arenas:
            if arena._indexed_call_plane is not plane:
                raise BackendUnavailable(
                    "parsed function owns inconsistent indexed call planes"
                )
            arena._indexed_call_plane = None
            arena._call_projector = self
        plane.release_construction_indexes()
        self.indexed_call_plane = None

    def call_header(self, call_id: int) -> CompilerInt4:
        return self.call_scalars.get4_unchecked(call_id * 2)

    def call_span(self, call_id: int) -> CompilerInt4:
        return self.call_scalars.get4_unchecked(call_id * 2 + 1)

    def call_flags(self, call_id: int) -> int:
        return self.call_scalars.get_unchecked(call_id * 8 + 2)

    def call_aux_state_id(self, call_id: int) -> int:
        """Return the call plane's kind-specific shared analysis state.

        Frame-protocol calls store their post-transition root-state ID.  Other
        calls store their managed live-after state ID.  The flag lane makes the
        two meanings disjoint, so one existing scalar carries both analyses.
        """

        return self.call_scalars.get_unchecked(call_id * 8 + 7)

    def publish_call_root_state_id(self, call_id: int, state_id: int) -> None:
        self.call_scalars.set_unchecked(call_id * 8 + 7, state_id)

    def publish_call_liveness_state_id(
        self, call_id: int, state_id: int
    ) -> None:
        self.call_scalars.set_unchecked(call_id * 8 + 7, state_id)

    def call_arg(self, arg_id: int) -> CompilerInt4:
        return self.call_arg_scalars.get4_unchecked(arg_id)

    def call_arg_value(self, arg_id: int) -> str:
        raw: CompilerInt4 = self.call_arg(arg_id)
        if raw.second >= 0:
            return self.value_name(raw.second)
        return self.call_texts[raw.third]

    def diagnostic_call_data(self, call_id: int) -> tuple:
        self.call_diagnostic_projections += 1
        header: CompilerInt4 = self.call_header(call_id)
        span: CompilerInt4 = self.call_span(call_id)
        args = []
        alignments = []
        arg_index = 0
        while arg_index < span.first:
            raw: CompilerInt4 = self.call_arg(header.fourth + arg_index)
            args.append(
                (
                    self.type_desc(raw.first),
                    self.call_arg_value(header.fourth + arg_index),
                )
            )
            alignments.append(raw.fourth)
            arg_index += 1
        dest = None if span.third < 0 else self.value_name(span.third)
        return (
            dest,
            self.type_desc(header.first),
            self.call_texts[header.second],
            bool(header.third & 1),
            tuple(args),
            span.second,
            bool(header.third & 2),
            tuple(alignments),
        )

    def _freeze_block_name_index(self) -> None:
        if self.block_name_index_capacity > 0:
            self.block_name_buckets.clear()
            return
        from .self_backend_analysis import _stable_text_bucket_key

        capacity = _text_id_index_capacity(len(self.block_names))
        index = CompilerIntArena(capacity * 2)
        index.append_zeros(capacity * 2)
        name_id = 0
        while name_id < len(self.block_names):
            key = _stable_text_bucket_key(self.block_names[name_id])
            slot = _text_id_index_start_slot(key, capacity)
            while True:
                entry: CompilerInt2 = index.get2_unchecked(slot)
                if entry.second == 0:
                    index.set2_unchecked(slot * 2, key, name_id + 1)
                    break
                slot = (slot + 1) & (capacity - 1)
            name_id += 1
        self.block_name_index = index
        self.block_name_index_capacity = capacity
        self.block_name_buckets.clear()

    def _freeze_value_name_index(self) -> None:
        if self.value_name_index_capacity > 0:
            self.value_name_buckets.clear()
            return
        from .self_backend_analysis import _stable_text_bucket_key

        capacity = _text_id_index_capacity(len(self.value_names))
        index = CompilerIntArena(capacity * 2)
        index.append_zeros(capacity * 2)
        name_id = 0
        while name_id < len(self.value_names):
            key = _stable_text_bucket_key(self.value_names[name_id])
            slot = _text_id_index_start_slot(key, capacity)
            while True:
                entry: CompilerInt2 = index.get2_unchecked(slot)
                if entry.second == 0:
                    index.set2_unchecked(slot * 2, key, name_id + 1)
                    break
                slot = (slot + 1) & (capacity - 1)
            name_id += 1
        self.value_name_index = index
        self.value_name_index_capacity = capacity
        self.value_name_buckets.clear()

    def block_id(self, name: str) -> int:
        from .self_backend_analysis import _stable_text_bucket_key

        key = _stable_text_bucket_key(name)
        slot = _text_id_index_start_slot(key, self.block_name_index_capacity)
        while True:
            entry: CompilerInt2 = self.block_name_index.get2_unchecked(slot)
            if entry.second == 0:
                return -1
            indexed_block_id = entry.second - 1
            if entry.first == key and text_key_names_equal(
                self.block_names[indexed_block_id], name
            ):
                return indexed_block_id
            slot = (slot + 1) & (self.block_name_index_capacity - 1)

    def value_id(self, name: str) -> int:
        from .self_backend_analysis import _stable_text_bucket_key

        key = _stable_text_bucket_key(name)
        slot = _text_id_index_start_slot(key, self.value_name_index_capacity)
        while True:
            entry: CompilerInt2 = self.value_name_index.get2_unchecked(slot)
            if entry.second == 0:
                return -1
            indexed_value_id = entry.second - 1
            if entry.first == key and text_key_names_equal(
                self.value_names[indexed_value_id], name
            ):
                return indexed_value_id
            slot = (slot + 1) & (self.value_name_index_capacity - 1)

    def value_name(self, value_id: int) -> str:
        return self.value_names[value_id]

    def instruction_count(self, block_id: int) -> int:
        return self.block_facts.get_unchecked(block_id * 4 + 1)

    def block_fact(self, block_id: int) -> CompilerInt4:
        return self.block_facts.get4_unchecked(block_id)

    def instruction_fact(
        self, block_id: int, instruction_index: int
    ) -> CompilerInt4:
        block: CompilerInt4 = self.block_fact(block_id)
        return self.instruction_facts.get4_unchecked(
            block.first + instruction_index
        )

    def instruction_fact_by_id(self, instruction_id: int) -> CompilerInt4:
        return self.instruction_facts.get4_unchecked(instruction_id)

    def instruction_kind_id(self, block_id: int, instruction_index: int) -> int:
        instruction_start = self.block_facts.get_unchecked(block_id * 4)
        return self.instruction_kind_id_by_id(
            instruction_start + instruction_index
        )

    def instruction_kind_id_by_id(self, instruction_id: int) -> int:
        if instruction_id < len(self.instruction_kind_ids):
            return self.instruction_kind_ids.get_unchecked(instruction_id)
        return self.instruction_metadata.get_unchecked(instruction_id * 4)

    def instruction_data(self, block_id: int, instruction_index: int) -> tuple:
        block: CompilerInt4 = self.block_fact(block_id)
        instruction_id = block.first + instruction_index
        metadata: CompilerInt4 = self.instruction_metadata_by_id(instruction_id)
        kind_id = metadata.first
        raw = metadata.second
        if raw < 0:
            if not self.instruction_arenas:
                return self.cold_instruction_data[-raw - 1]
            arena = self.instruction_arenas[block_id]
            if arena._data is _EMPTY_SEQUENCE:
                return self.cold_instruction_data[-raw - 1]
            raw = arena._data[instruction_index]
        if kind_id in (
            PARSED_INSTRUCTION_KIND_CALL,
            PARSED_INSTRUCTION_KIND_ALLOCA,
        ) and isinstance(raw, int):
            if kind_id == PARSED_INSTRUCTION_KIND_CALL:
                return self.diagnostic_call_data(raw)
            return self.diagnostic_alloca_data(raw)
        if kind_id in _PACKED_FIXED_KIND_IDS and isinstance(raw, int):
            return self.diagnostic_fixed_instruction_data(
                kind_id,
                raw,
            )
        if kind_id == PARSED_INSTRUCTION_KIND_GEP and isinstance(raw, int):
            return self.diagnostic_gep_data(raw)
        return raw

    def instruction_call_id(self, block_id: int, instruction_index: int) -> int:
        return self.instruction_record_id(block_id, instruction_index)

    def instruction_record_id(self, block_id: int, instruction_index: int) -> int:
        """Return an already-proven packed payload ID without projection."""
        instruction_start = self.block_facts.get_unchecked(block_id * 4)
        return self.instruction_metadata.get_unchecked(
            (instruction_start + instruction_index) * 4 + 1
        )

    def instruction_is_volatile(
        self, block_id: int, instruction_index: int
    ) -> bool:
        instruction_start = self.block_facts.get_unchecked(block_id * 4)
        return self.instruction_is_volatile_by_id(
            instruction_start + instruction_index
        )

    def instruction_is_volatile_by_id(self, instruction_id: int) -> bool:
        return bool(
            self.instruction_metadata.get_unchecked(instruction_id * 4 + 2)
        )

    def instruction_arithmetic_flags(
        self, block_id: int, instruction_index: int
    ) -> tuple[str, ...]:
        instruction_start = self.block_facts.get_unchecked(block_id * 4)
        instruction_id = instruction_start + instruction_index
        if not self.instruction_metadata.get_unchecked(instruction_id * 4 + 3):
            return ()
        return self.instruction_arithmetic_flag_values[instruction_id]

    def instruction_has_arithmetic_flags(
        self, block_id: int, instruction_index: int
    ) -> bool:
        instruction_start = self.block_facts.get_unchecked(block_id * 4)
        instruction_id = instruction_start + instruction_index
        return self.instruction_has_arithmetic_flags_by_id(instruction_id)

    def instruction_has_arithmetic_flags_by_id(
        self, instruction_id: int
    ) -> bool:
        return bool(
            self.instruction_metadata.get_unchecked(instruction_id * 4 + 3)
        )

    def defined_value_id(self, block_id: int, instruction_index: int) -> int:
        instruction_start = self.block_facts.get_unchecked(block_id * 4)
        return self.instruction_facts.get_unchecked(
            (instruction_start + instruction_index) * 4
        )

    def instruction_use_count(
        self, block_id: int, instruction_index: int
    ) -> int:
        instruction_start = self.block_facts.get_unchecked(block_id * 4)
        return self.instruction_facts.get_unchecked(
            (instruction_start + instruction_index) * 4 + 1
        )

    def instruction_use_id(
        self,
        block_id: int,
        instruction_index: int,
        use_index: int,
    ) -> int:
        instruction_start = self.block_facts.get_unchecked(block_id * 4)
        fact_start = (instruction_start + instruction_index) * 4
        use_count = self.instruction_facts.get_unchecked(fact_start + 1)
        if not 0 <= use_index < use_count:
            raise IndexError(use_index)
        if use_index == 0:
            return self.instruction_facts.get_unchecked(fact_start + 2)
        second_or_overflow = self.instruction_facts.get_unchecked(
            fact_start + 3
        )
        if use_count == 2:
            return second_or_overflow
        overflow_start = -second_or_overflow - 2
        return self.instruction_overflow_use_ids.get_unchecked(
            overflow_start + use_index - 1
        )

    def terminator_use_count(self, block_id: int) -> int:
        return self.block_facts.get_unchecked(block_id * 4 + 2)

    def terminator_use_id(self, block_id: int, use_index: int) -> int:
        use_count = self.block_facts.get_unchecked(block_id * 4 + 2)
        if use_index != 0 or use_count != 1:
            raise IndexError(use_index)
        return self.block_facts.get_unchecked(block_id * 4 + 3)

    def value_is_used(self, value_id: int) -> bool:
        if not 0 <= value_id < len(self.value_names):
            return False
        return bool(self.value_scalars.get_unchecked(value_id * 8 + 7))

    def last_use(self, block_id: int, value_id: int) -> int | None:
        if not 0 <= value_id < len(self.value_names):
            return None
        if self.value_scalars.get_unchecked(value_id * 8) != block_id:
            return None
        position = self.value_scalars.get_unchecked(value_id * 8 + 6)
        return None if position < 0 else position

    def intern_type(self, value_type: TypeDesc) -> int:
        identity = id(value_type)
        if identity in self.type_identity_ids:
            identity_entry = self.type_identity_ids[identity]
            if identity_entry[1] is value_type:
                return identity_entry[0]

        pointee_type_id = -1
        if value_type.is_ptr and value_type.pointee is not None:
            pointee_type_id = self.intern_type(value_type.pointee)
            if pointee_type_id in self.pointer_type_ids:
                type_id = self.pointer_type_ids[pointee_type_id]
                return type_id

        type_id = 0
        while type_id < len(self.types):
            if self.types[type_id] == value_type:
                if pointee_type_id >= 0:
                    self.pointer_type_ids[pointee_type_id] = type_id
                return type_id
            type_id += 1
        self.types.append(value_type)
        type_id = len(self.types) - 1
        self.type_identity_ids[identity] = (type_id, value_type)
        if pointee_type_id >= 0:
            self.pointer_type_ids[pointee_type_id] = type_id
        self.type_scalars.append_zeros(12)
        self._publish_type_record(type_id, value_type, pointee_type_id)
        return type_id

    def _adopt_indexed_call_types(self) -> None:
        type_id = 0
        while type_id < len(self.types):
            value_type = self.types[type_id]
            self.type_identity_ids[id(value_type)] = (type_id, value_type)
            type_id += 1
        adopted_count = len(self.types)
        self.type_scalars.append_zeros(adopted_count * 12)
        type_id = 0
        while type_id < adopted_count:
            value_type = self.types[type_id]
            pointee_type_id = -1
            if value_type.is_ptr and value_type.pointee is not None:
                pointee_type_id = self.intern_type(value_type.pointee)
                self.pointer_type_ids[pointee_type_id] = type_id
            self._publish_type_record(type_id, value_type, pointee_type_id)
            type_id += 1
        if len(self.types) != adopted_count:
            raise BackendUnavailable(
                "indexed call plane omitted a nested type record"
            )

    def _publish_type_record(
        self,
        type_id: int,
        value_type: TypeDesc,
        pointee_type_id: int,
    ) -> None:
        kind_id = TYPE_KIND_VOID
        child_type_id = -1
        if value_type.is_int:
            kind_id = TYPE_KIND_INT
        elif value_type.is_fp:
            kind_id = TYPE_KIND_FP
        elif value_type.is_ptr:
            kind_id = TYPE_KIND_PTR
            child_type_id = pointee_type_id
        elif value_type.is_array:
            kind_id = TYPE_KIND_ARRAY
            if value_type.elem is not None:
                child_type_id = self.intern_type(value_type.elem)
        elif value_type.is_struct:
            kind_id = TYPE_KIND_STRUCT

        field_start = len(self.type_field_ids)
        field_count = 0
        if value_type.is_struct:
            for field_type in value_type.fields:
                self.type_field_ids.append(self.intern_type(field_type))
                field_count += 1

        record_start = type_id * 12
        self.type_scalars.set3_unchecked(
            record_start,
            kind_id,
            value_type.width,
            value_type.count,
        )
        self.type_scalars.set3_unchecked(
            record_start + 3,
            child_type_id,
            field_start,
            field_count,
        )
        self.type_scalars.set3_unchecked(
            record_start + 6,
            value_type.slot_size,
            value_type.align,
            value_type.value_slot_size,
        )
        self.type_scalars.set3_unchecked(
            record_start + 9,
            value_type.value_align,
            value_type.bits,
            0,
        )

    def type_header(self, type_id: int) -> CompilerInt4:
        return self.type_scalars.get4_unchecked(type_id * 3)

    def type_kind_id(self, type_id: int) -> int:
        return self.type_scalars.get_unchecked(type_id * 12)

    def type_width(self, type_id: int) -> int:
        return self.type_scalars.get_unchecked(type_id * 12 + 1)

    def type_child_id(self, type_id: int) -> int:
        return self.type_scalars.get_unchecked(type_id * 12 + 3)

    def type_span(self, type_id: int) -> CompilerInt4:
        return self.type_scalars.get4_unchecked(type_id * 3 + 1)

    def type_field_start(self, type_id: int) -> int:
        return self.type_scalars.get_unchecked(type_id * 12 + 4)

    def type_field_count(self, type_id: int) -> int:
        return self.type_scalars.get_unchecked(type_id * 12 + 5)

    def type_slot_size(self, type_id: int) -> int:
        return self.type_scalars.get_unchecked(type_id * 12 + 6)

    def type_align(self, type_id: int) -> int:
        return self.type_scalars.get_unchecked(type_id * 12 + 7)

    def type_layout(self, type_id: int) -> CompilerInt4:
        return self.type_scalars.get4_unchecked(type_id * 3 + 2)

    def publish_value_type(self, value_id: int, value_type: TypeDesc) -> int:
        type_id = self.intern_type(value_type)
        if len(self.value_scalars) == 0:
            self.value_type_ids[value_id] = type_id
        else:
            self.value_scalars.set_unchecked(value_id * 8 + 1, type_id)
        return type_id

    def publish_value_type_id(self, value_id: int, type_id: int) -> None:
        if len(self.value_scalars) == 0:
            self.value_type_ids[value_id] = type_id
        else:
            self.value_scalars.set_unchecked(value_id * 8 + 1, type_id)

    def value_type_id(self, value_id: int) -> int:
        if len(self.value_scalars) == 0:
            return self.value_type_ids[value_id]
        return self.value_scalars.get_unchecked(value_id * 8 + 1)

    def type_desc(self, type_id: int) -> TypeDesc:
        self.type_object_projections += 1
        return self.types[type_id]

    def intern_slot(self, offset: int, value_type: TypeDesc) -> int:
        type_id = self.intern_type(value_type)
        return self.intern_slot_type_id(offset, type_id)

    def intern_slot_type_id(self, offset: int, type_id: int) -> int:
        if offset in self.slot_ids_by_offset:
            by_type = self.slot_ids_by_offset[offset]
        else:
            by_type = {}
            self.slot_ids_by_offset[offset] = by_type
        if type_id in by_type:
            return by_type[type_id]
        slot_id = len(self.slot_scalars) // 2
        self.slot_scalars.append2(offset, type_id)
        by_type[type_id] = slot_id
        return slot_id

    def publish_value_slot(
        self, value_id: int, offset: int, value_type: TypeDesc
    ) -> int:
        slot_id = self.intern_slot(offset, value_type)
        self.publish_value_slot_id(value_id, slot_id)
        return slot_id

    def publish_value_slot_id(self, value_id: int, slot_id: int) -> None:
        self.value_scalars.set_unchecked(value_id * 8 + 2, slot_id)

    def publish_alloca_type_id(
        self,
        value_id: int,
        offset: int,
        type_id: int,
    ) -> None:
        self.value_scalars.set_unchecked(value_id * 8 + 3, offset)
        self.value_scalars.set_unchecked(value_id * 8 + 4, type_id)

    def finish_slot_interning(self) -> None:
        """Drop the construction index after dense slot IDs are frozen."""
        self.slot_ids_by_offset.clear()
        self.type_identity_ids.clear()
        self.pointer_type_ids.clear()

    def value_slot_id(self, value_id: int) -> int:
        header: CompilerInt4 = self.value_header(value_id)
        return header.third

    def value_slot_offset(self, value_id: int) -> int:
        header: CompilerInt4 = self.value_header(value_id)
        slot_id = header.third
        if slot_id < 0:
            return -1
        slot: CompilerInt2 = self.slot_record(slot_id)
        return slot.first

    def value_slot_type_id(self, value_id: int) -> int:
        header: CompilerInt4 = self.value_header(value_id)
        slot_id = header.third
        if slot_id < 0:
            return -1
        slot: CompilerInt2 = self.slot_record(slot_id)
        return slot.second

    def slot_offset(self, slot_id: int) -> int:
        slot: CompilerInt2 = self.slot_record(slot_id)
        return slot.first

    def slot_type_id(self, slot_id: int) -> int:
        slot: CompilerInt2 = self.slot_record(slot_id)
        return slot.second

    def publish_alloca(
        self, value_id: int, offset: int, allocated_type: TypeDesc
    ) -> None:
        self.publish_alloca_type_id(
            value_id,
            offset,
            self.intern_type(allocated_type),
        )

    def alloca_offset(self, value_id: int) -> int:
        if len(self.value_scalars) == 0:
            return self.alloca_offsets[value_id]
        header: CompilerInt4 = self.value_header(value_id)
        return header.fourth

    def alloca_type_id(self, value_id: int) -> int:
        if len(self.value_scalars) == 0:
            return self.alloca_type_ids[value_id]
        state: CompilerInt4 = self.value_state(value_id)
        return state.first

    def clear_value_registers(self) -> None:
        index = 0
        while index < len(self.value_names):
            self.value_scalars.set_unchecked(index * 8 + 5, -1)
            index += 1

    def set_value_register(self, value_id: int, register_index: int) -> None:
        self.value_scalars.set_unchecked(value_id * 8 + 5, register_index)

    def clear_value_register(self, value_id: int) -> None:
        self.value_scalars.set_unchecked(value_id * 8 + 5, -1)

    def value_register(self, value_id: int) -> int | None:
        state: CompilerInt4 = self.value_state(value_id)
        register_index = state.second
        return None if register_index < 0 else register_index

    def legacy_value_registers(self) -> dict[str, int]:
        result: dict[str, int] = {}
        value_id = 0
        while value_id < len(self.value_names):
            state: CompilerInt4 = self.value_state(value_id)
            register_index = state.second
            if register_index >= 0:
                result[self.value_names[value_id]] = register_index
            value_id += 1
        return result

    def diagnostic_cold_instruction_data(self, cold_id: int) -> tuple:
        self.diagnostic_projections += 1
        return self.cold_instruction_data[cold_id]

    def diagnostic_instruction(self, block_id: int, instruction_index: int):
        # Packed records count when the arena projects their payload.  Legacy
        # records have no projector, so count the explicit diagnostic API here
        # only when constructing the view did not already do so.
        projections_before = self.diagnostic_projections
        kind = PARSED_INSTRUCTION_KINDS[
            self.instruction_kind_id(block_id, instruction_index)
        ]
        result = ParsedInstr(
            kind,
            self.instruction_data(block_id, instruction_index),
            self.instruction_is_volatile(block_id, instruction_index),
            self.instruction_arithmetic_flags(block_id, instruction_index),
        )
        if self.diagnostic_projections == projections_before:
            self.diagnostic_projections += 1
        return result

    def diagnostic_block(
        self,
        block_id: int,
        include_instructions: bool = True,
    ) -> ParsedBlock:
        instructions = CompactParsedInstrArena()
        if include_instructions:
            instruction_index = 0
            instruction_count = self.instruction_count(block_id)
            while instruction_index < instruction_count:
                instructions.append(
                    self.diagnostic_instruction(block_id, instruction_index)
                )
                instruction_index += 1
            self.instruction_arena_diagnostic_projections += 1
        phi_fact: CompilerInt2 = self.block_phi_fact(block_id)
        phis: list[PhiInstr] = []
        phi_index = 0
        while phi_index < phi_fact.second:
            phis.append(self.diagnostic_phi(block_id, phi_index))
            phi_index += 1
        self.block_diagnostic_projections += 1
        return ParsedBlock(
            name=self.block_names[block_id],
            raw_lines=[],
            phis=tuple(phis),
            instructions=instructions,
            terminator=self.diagnostic_terminator(block_id),
        )

    def materialize_legacy_blocks(self, func: ParsedFunction) -> list[ParsedBlock]:
        if func.blocks:
            return list(func.blocks)
        blocks: list[ParsedBlock] = []
        block_id = 0
        while block_id < len(self.block_names):
            blocks.append(self.diagnostic_block(block_id))
            block_id += 1
        func.blocks = blocks
        func.block_map = {block.name: block for block in blocks}
        return blocks

    def release_block_projections(self, func: ParsedFunction) -> None:
        self.instruction_arenas = _EMPTY_SEQUENCE
        func.blocks = _EMPTY_SEQUENCE

    def legacy_used_values(self) -> list[str]:
        result: list[str] = []
        index = 0
        while index < len(self.used_value_ids):
            indexed_value_id = self.used_value_ids.get_unchecked(index)
            result.append(self.value_name(indexed_value_id))
            index += 1
        return result

    def legacy_block_last_uses(self) -> dict[str, dict[str, int]]:
        result: dict[str, dict[str, int]] = {}
        value_id = 0
        while value_id < len(self.value_names):
            value_header: CompilerInt4 = self.value_header(value_id)
            value_state: CompilerInt4 = self.value_state(value_id)
            position = value_state.third
            if position < 0:
                value_id += 1
                continue
            block_id = value_header.first
            block_name = self.block_names[block_id]
            if block_name in result:
                projected = result[block_name]
            else:
                projected = {}
                result[block_name] = projected
            projected[self.value_name(value_id)] = position
            value_id += 1
        return result

    def profile_counters(self) -> dict[str, int]:
        return {
            "blocks": len(self.block_names),
            "values": len(self.value_names),
            "block_name_index_capacity": self.block_name_index_capacity,
            "value_name_index_capacity": self.value_name_index_capacity,
            "instructions": len(self.instruction_metadata) // 4,
            "uses": self.instruction_use_total,
            "types": len(self.types),
            "slots": len(self.slot_scalars) // 2,
            "value_slot_bindings": sum(
                1
                for value_id in range(len(self.value_names))
                if self.value_slot_id(value_id) >= 0
            ),
            "alloca_bindings": sum(
                1
                for value_id in range(len(self.value_names))
                if self.alloca_offset(value_id) >= 0
            ),
            "diagnostic_projections": self.diagnostic_projections,
            "block_diagnostic_projections": self.block_diagnostic_projections,
            "instruction_arena_diagnostic_projections": (
                self.instruction_arena_diagnostic_projections
            ),
            "call_diagnostic_projections": self.call_diagnostic_projections,
            "legacy_slot_projections": self.legacy_slot_projections,
            "type_object_projections": self.type_object_projections,
            "phi_diagnostic_projections": self.phi_diagnostic_projections,
            "terminator_diagnostic_projections": (
                self.terminator_diagnostic_projections
            ),
            "inline_error_edges": len(self.error_edge_scalars)
            // INLINE_ERROR_EDGE_WIDTH,
        }

    def close_native_tables(self) -> None:
        self.block_name_index.close()
        self.block_facts.close()
        self.call_arg_scalars.close()
        self.call_scalars.close()
        self.instruction_facts.close()
        self.instruction_kind_ids.close()
        self.instruction_metadata.close()
        self.instruction_record_dest_ids.close()
        self.instruction_record_scalars.close()
        self.gep_index_scalars.close()
        self.gep_scalars.close()
        self.type_field_ids.close()
        self.type_scalars.close()
        self.terminator_case_scalars.close()
        self.terminator_scalars.close()
        self.block_phi_facts.close()
        self.phi_incoming_scalars.close()
        self.phi_scalars.close()
        self.error_edge_scalars.close()
        self.error_edge_spans.close()
        self.error_landing_scalars.close()
        self.value_scalars.close()
        self.definition_positions.close()
        self.used_value_ids.close()
        self.slot_scalars.close()
        self.block_layout_ids.close()
        self.instruction_overflow_use_ids.close()
        self.value_name_index.close()


def get_indexed_function_kernel(func: ParsedFunction) -> IndexedFunctionKernel:
    kernel = func.indexed_kernel
    if kernel is None:
        kernel = IndexedFunctionKernel(func)
        func.indexed_kernel = kernel
    if not isinstance(kernel, IndexedFunctionKernel):
        raise BackendUnavailable("parsed function has an invalid indexed kernel")
    return kernel


__all__ = [
    "INLINE_ERROR_EDGE_WIDTH",
    "IndexedFunctionKernel",
    "get_indexed_function_kernel",
]
