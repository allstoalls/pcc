from __future__ import annotations

"""Conservative block-local scalar register allocation for AArch64 Darwin.

The existing stack slot remains allocated for every value.  This pass only
adds an optional register projection for integer/pointer SSA values whose
entire lifetime is proven to stay inside one supported basic block.  Any shape
outside that proof keeps using its slot.

``x1`` through ``x8`` are caller-saved by AAPCS64 and are deliberately unused
by the scalar lowering covered here.  The established instruction emitters use
``x9`` through ``x17`` as scratch registers, while returns use ``x0``.
Float/vector SSA values never become candidates.  Calls are interval barriers:
call results, call operands, and values live across a call stay spilled, while
values whose complete lifetime is before or after the call remain eligible.
PHI values and PHI inputs stay spilled, but unrelated local definitions later
in a PHI block may still use this register projection.

This is a deliberately finite block-local subset of LLVM 20.1.8's
``RegAllocFast.cpp`` model: every value keeps its preassigned spill slot, and a
mapping is installed only when the whole interval is proven safe for x1-x8.
"""

from .self_backend_analysis import (
    collect_block_local_last_uses,  # compatibility seam; indexed path never calls it
    is_local_value_ref,
)
from .self_backend_aarch64_darwin_abi import reg_name_indexed
from .self_backend_aarch64_darwin_mem import emitted_move_register_line
from .self_backend_kernel import (
    IndexedFunctionKernel,
    TYPE_KIND_INT,
    TYPE_KIND_PTR,
    get_indexed_function_kernel,
)
from .self_backend_ir import (
    I1,
    PARSED_INSTRUCTION_KIND_ALLOCA,
    PARSED_INSTRUCTION_KIND_BINOP,
    PARSED_INSTRUCTION_KIND_BR,
    PARSED_INSTRUCTION_KIND_BR_COND,
    PARSED_INSTRUCTION_KIND_CALL,
    PARSED_INSTRUCTION_KIND_CAST,
    PARSED_INSTRUCTION_KIND_FREEZE,
    PARSED_INSTRUCTION_KIND_GEP,
    PARSED_INSTRUCTION_KIND_ICMP,
    PARSED_INSTRUCTION_KIND_LOAD,
    PARSED_INSTRUCTION_KIND_RET,
    PARSED_INSTRUCTION_KIND_RET_VOID,
    PARSED_INSTRUCTION_KIND_SELECT,
    PARSED_INSTRUCTION_KIND_SWITCH,
    PARSED_INSTRUCTION_KIND_SYSCALL6,
    PARSED_INSTRUCTION_KIND_UNREACHABLE,
    PARSED_INSTRUCTION_KINDS,
    ParsedBlock,
    ParsedFunction,
    TypeDesc,
    parsed_function_value_slot_offset,
    parsed_function_value_slot_type,
    text_key_mapping_get,
    text_key_names_equal,
)
from .self_backend_target_passes import (
    AArch64MaddFusion,
    aarch64_madd_fusion_for_product,
)
from .self_backend_value_arena import CompilerInt4


_REGISTER_POOL = (1, 2, 3, 4, 5, 6, 7, 8)

# Every instruction in an allocated block must be known not to clobber x1-x8.
# Non-candidate floating/vector/aggregate and atomic values still use their
# mandatory slots, but their current AArch64 emitters use only v-registers and
# x9-x17 scratch GPRs, so unrelated scalar intervals may cross them.  Calls
# (including intrinsic calls) are admitted only as clobber positions;
# intervals touching them are rejected below.  An unclassified future kind,
# and the x86_64-only syscall6 shape, reject the whole block.
_POOL_REJECTED_INSTRUCTION_KIND_IDS = (
    PARSED_INSTRUCTION_KIND_BR,
    PARSED_INSTRUCTION_KIND_BR_COND,
    PARSED_INSTRUCTION_KIND_RET,
    PARSED_INSTRUCTION_KIND_RET_VOID,
    PARSED_INSTRUCTION_KIND_SWITCH,
    PARSED_INSTRUCTION_KIND_SYSCALL6,
    PARSED_INSTRUCTION_KIND_UNREACHABLE,
)


def _is_scalar_register_type(value_type: TypeDesc) -> bool:
    return value_type.is_int or value_type.is_ptr


def _register_types_match(left: TypeDesc, right: TypeDesc) -> bool:
    # All pointer projections are one 64-bit GPR even when typed-pointer IR
    # spells different pointees at the definition and use sites.
    if left.is_ptr and right.is_ptr:
        return True
    return left.describe() == right.describe()


def _type_mapping_get(
    mapping: dict[str, TypeDesc], key: str
) -> TypeDesc | None:
    # Recover via the shared incremental text-key index instead of walking the
    # whole mapping: the index is O(1) amortised, keeps the mapping pinned so
    # an id() cannot be recycled under it, and covers both recovery cases the
    # scan covered (`by_id` for dot-numeric spellings, `by_bucket` for plain
    # equality after an inconsistent native hash).  The scan was 12.3 s of a
    # 120 s emit profile on one 10.4 MB module.
    return text_key_mapping_get(mapping, key)


def _candidate_definition_type(
    func: ParsedFunction,
    kind_id: int,
    data: tuple,
) -> TypeDesc | None:
    if kind_id == PARSED_INSTRUCTION_KIND_LOAD:
        _dest, value_type, _ptr_type, _ptr = data
        return value_type if _is_scalar_register_type(value_type) else None
    if kind_id == PARSED_INSTRUCTION_KIND_BINOP:
        _op, _dest, value_type, _lhs, _rhs = data
        return value_type if value_type.is_int else None
    if kind_id == PARSED_INSTRUCTION_KIND_ICMP:
        _cond, _dest, value_type, _lhs, _rhs = data
        return I1 if _is_scalar_register_type(value_type) else None
    if kind_id == PARSED_INSTRUCTION_KIND_CAST:
        _op, _dest, src_type, _value, dst_type = data
        if _is_scalar_register_type(src_type) and _is_scalar_register_type(dst_type):
            return dst_type
        return None
    if kind_id in (
        PARSED_INSTRUCTION_KIND_SELECT,
        PARSED_INSTRUCTION_KIND_FREEZE,
    ):
        dest = data[0]
        value_type = data[1]
        if not _is_scalar_register_type(value_type):
            return None
        recorded_type = _type_mapping_get(func.value_types, dest)
        if recorded_type is None or not _register_types_match(
            recorded_type, value_type
        ):
            return None
        return value_type
    if kind_id == PARSED_INSTRUCTION_KIND_GEP:
        dest = data[0]
        value_type = _type_mapping_get(func.value_types, dest)
        if value_type is not None and value_type.is_ptr:
            return value_type
    return None


def _candidate_indexed_definition_type_id(
    kernel: IndexedFunctionKernel,
    kind_id: int,
    record_id: int,
    dest_id: int,
) -> int:
    raw: CompilerInt4 = kernel.instruction_record(record_id)
    if kind_id == PARSED_INSTRUCTION_KIND_LOAD:
        header: CompilerInt4 = kernel.type_header(raw.first)
        return (
            raw.first
            if header.first == TYPE_KIND_INT or header.first == TYPE_KIND_PTR
            else -1
        )
    if kind_id == PARSED_INSTRUCTION_KIND_BINOP:
        binop_type: CompilerInt4 = kernel.type_header(raw.second)
        return (
            raw.second
            if binop_type.first == TYPE_KIND_INT
            else -1
        )
    if kind_id == PARSED_INSTRUCTION_KIND_ICMP:
        compared: CompilerInt4 = kernel.type_header(raw.second)
        if compared.first != TYPE_KIND_INT and compared.first != TYPE_KIND_PTR:
            return -1
        return -1 if dest_id < 0 else kernel.value_type_id(dest_id)
    if kind_id == PARSED_INSTRUCTION_KIND_CAST:
        src_header: CompilerInt4 = kernel.type_header(raw.second)
        dst_header: CompilerInt4 = kernel.type_header(raw.fourth)
        src_kind = src_header.first
        dst_kind = dst_header.first
        if (
            (src_kind == TYPE_KIND_INT or src_kind == TYPE_KIND_PTR)
            and (dst_kind == TYPE_KIND_INT or dst_kind == TYPE_KIND_PTR)
        ):
            return raw.fourth
        return -1
    result_header: CompilerInt4 = kernel.type_header(raw.first)
    result_kind = result_header.first
    return (
        raw.first
        if result_kind == TYPE_KIND_INT or result_kind == TYPE_KIND_PTR
        else -1
    )


def _register_type_ids_match(
    kernel: IndexedFunctionKernel, left_id: int, right_id: int
) -> bool:
    if left_id == right_id:
        return True
    left: CompilerInt4 = kernel.type_header(left_id)
    right: CompilerInt4 = kernel.type_header(right_id)
    return left.first == TYPE_KIND_PTR and right.first == TYPE_KIND_PTR


def _text_list_contains(values: list[str], value: str) -> bool:
    for existing in values:
        if text_key_names_equal(existing, value):
            return True
    return False


def _interval_touches_call(
    start: int,
    end: int,
    call_positions: list[int],
) -> bool:
    # A value used as a call operand ends at the call position and must remain
    # spilled: ABI argument setup may overwrite x1-x8 before every operand has
    # been copied.  Strictly pre-call and strictly post-call intervals are safe.
    for call_position in call_positions:
        if start <= call_position <= end:
            return True
    return False


def _extend_aarch64_madd_operand_liveness(
    func: ParsedFunction,
    block_id: int,
    block_name: str,
    override_ids: list[int],
    override_positions: list[int],
) -> None:
    """Keep delayed multiply inputs live until their fused consumer."""

    kernel = get_indexed_function_kernel(func)

    for fusion in func.aarch64_madd_fusions:
        if not text_key_names_equal(fusion.block_name, block_name):
            continue
        for operand in (fusion.mul_lhs, fusion.mul_rhs):
            operand_id = kernel.value_id(operand)
            if operand_id < 0:
                continue
            last_use = kernel.last_use(block_id, operand_id)
            override_index = 0
            while override_index < len(override_ids):
                if override_ids[override_index] == operand_id:
                    last_use = override_positions[override_index]
                    break
                override_index += 1
            if last_use is not None and last_use < fusion.consumer_index:
                if override_index < len(override_ids):
                    override_positions[override_index] = fusion.consumer_index
                else:
                    override_ids.append(operand_id)
                    override_positions.append(fusion.consumer_index)


def _slots_overlap(
    left_offset: int,
    left_size: int,
    right_offset: int,
    right_size: int,
) -> bool:
    left_start = left_offset - left_size
    right_start = right_offset - right_size
    return left_start < right_offset and right_start < left_offset


def _aarch64_madd_operand_survives_to_consumer(
    func: ParsedFunction,
    block_id: int,
    fusion: AArch64MaddFusion,
    operand: str,
) -> bool:
    # An allocated x1-x8 projection has had its interval extended above and is
    # therefore authoritative even if stack-slot preparation reused its old
    # spill slot after the original multiply.
    kernel = get_indexed_function_kernel(func)
    operand_id = kernel.value_id(operand)
    if operand_id >= 0 and kernel.value_register(operand_id) is not None:
        return True

    operand_offset = (
        -1 if operand_id < 0 else kernel.value_slot_offset(operand_id)
    )
    if operand_offset < 0:
        # Constants do not need storage.  An unresolved local-looking value is
        # not a constant proof and must fail closed.
        return not is_local_value_ref(operand)

    if block_id < 0:
        return False
    block_fact: CompilerInt4 = kernel.block_fact(block_id)
    index = fusion.producer_index
    while index < fusion.consumer_index:
        instruction_fact: CompilerInt4 = kernel.instruction_fact_by_id(
            block_fact.first + index
        )
        dest_id = instruction_fact.first
        dest = None if dest_id < 0 else kernel.value_name(dest_id)
        if (
            dest is not None
            and not text_key_names_equal(dest, operand)
            and not (
                index == fusion.producer_index
                and text_key_names_equal(dest, fusion.product)
            )
        ):
            dest_offset = kernel.value_slot_offset(dest_id)
            operand_layout: CompilerInt4 = kernel.type_layout(
                kernel.value_slot_type_id(operand_id)
            )
            dest_layout: CompilerInt4 = kernel.type_layout(
                kernel.value_slot_type_id(dest_id)
            )
            if dest_offset >= 0 and _slots_overlap(
                operand_offset,
                operand_layout.first,
                dest_offset,
                dest_layout.first,
            ):
                return False
        index += 1
    return True


def _aarch64_madd_fusion_storage_is_safe(
    func: ParsedFunction,
    fusion: AArch64MaddFusion,
) -> bool:
    kernel = get_indexed_function_kernel(func)
    block_id = kernel.block_id(fusion.block_name)
    if block_id < 0:
        return False
    return _aarch64_madd_operand_survives_to_consumer(
        func, block_id, fusion, fusion.mul_lhs
    ) and _aarch64_madd_operand_survives_to_consumer(
        func, block_id, fusion, fusion.mul_rhs
    )


def allocate_aarch64_block_registers(func: ParsedFunction) -> None:
    """Populate the indexed kernel with conservative linear-scan picks."""

    kernel = get_indexed_function_kernel(func)
    kernel.clear_value_registers()
    if func.is_vararg:
        return
    phi_input_ids: set[int] = set()
    block_id = 0
    while block_id < len(kernel.block_names):
        phi_fact = kernel.block_phi_fact(block_id)
        phi_index = 0
        while phi_index < phi_fact.second:
            phi = kernel.phi_record(phi_fact.first + phi_index)
            incoming_index = 0
            while incoming_index < phi.fourth:
                incoming = kernel.phi_incoming(phi.third + incoming_index)
                if incoming.first >= 0:
                    phi_input_ids.add(incoming.first)
                incoming_index += 1
            phi_index += 1
        block_id += 1

    for block_id in range(len(kernel.block_names)):
        block_name = kernel.block_names[block_id]
        block_fact: CompilerInt4 = kernel.block_fact(block_id)
        block_is_safe = True
        instruction_index = 0
        instruction_count = block_fact.second
        while instruction_index < instruction_count:
            metadata: CompilerInt4 = kernel.instruction_metadata_by_id(
                block_fact.first + instruction_index
            )
            if (
                not 0 <= metadata.first < len(PARSED_INSTRUCTION_KINDS)
                or metadata.first in _POOL_REJECTED_INSTRUCTION_KIND_IDS
            ):
                block_is_safe = False
                break
            instruction_index += 1
        if not block_is_safe:
            continue

        call_positions: list[int] = []
        instruction_index = 0
        while instruction_index < instruction_count:
            metadata: CompilerInt4 = kernel.instruction_metadata_by_id(
                block_fact.first + instruction_index
            )
            if metadata.first == PARSED_INSTRUCTION_KIND_CALL:
                call_positions.append(instruction_index)
            instruction_index += 1

        last_use_override_ids: list[int] = []
        last_use_override_positions: list[int] = []
        _extend_aarch64_madd_operand_liveness(
            func,
            block_id,
            block_name,
            last_use_override_ids,
            last_use_override_positions,
        )
        intervals: list[tuple[int, int, int]] = []
        position = 0
        while position < instruction_count:
            instruction_id = block_fact.first + position
            instruction_fact: CompilerInt4 = kernel.instruction_fact_by_id(
                instruction_id
            )
            dest_id = instruction_fact.first
            if dest_id < 0 or dest_id in phi_input_ids:
                position += 1
                continue
            dest = kernel.value_name(dest_id)
            if aarch64_madd_fusion_for_product(func, dest) is not None:
                # The product is never materialized separately, so assigning
                # it a register would only steal capacity from real values.
                position += 1
                continue
            metadata: CompilerInt4 = kernel.instruction_metadata_by_id(
                instruction_id
            )
            kind_id = metadata.first
            if kind_id == PARSED_INSTRUCTION_KIND_CALL:
                position += 1
                continue
            if (
                kind_id == PARSED_INSTRUCTION_KIND_LOAD
                or kind_id == PARSED_INSTRUCTION_KIND_BINOP
                or kind_id == PARSED_INSTRUCTION_KIND_ICMP
                or kind_id == PARSED_INSTRUCTION_KIND_CAST
                or kind_id == PARSED_INSTRUCTION_KIND_SELECT
            ):
                value_type_id = _candidate_indexed_definition_type_id(
                    kernel,
                    kind_id,
                    metadata.second,
                    dest_id,
                )
            elif kind_id == PARSED_INSTRUCTION_KIND_ALLOCA:
                value_type_id = -1
            elif kind_id == PARSED_INSTRUCTION_KIND_GEP:
                gep_span: CompilerInt4 = kernel.gep_span(
                    kernel.instruction_payload_id_by_id(instruction_id)
                )
                gep_type: CompilerInt4 = kernel.type_header(gep_span.second)
                value_type_id = (
                    gep_span.second
                    if gep_type.first == TYPE_KIND_PTR
                    else -1
                )
            else:
                data = kernel.instruction_data(block_id, position)
                value_type = _candidate_definition_type(func, kind_id, data)
                value_type_id = (
                    -1 if value_type is None else kernel.intern_type(value_type)
                )
            if value_type_id < 0:
                position += 1
                continue
            recorded_type_id = kernel.value_type_id(dest_id)
            if recorded_type_id < 0 or not _register_type_ids_match(
                kernel,
                recorded_type_id,
                value_type_id,
            ):
                position += 1
                continue
            if kernel.value_slot_offset(dest_id) < 0:
                # Allocation is only an optional projection.  Never create a
                # register-only value: every candidate must retain the
                # preassigned slot needed by pressure/type fallback paths.
                position += 1
                continue
            # Preserve the established compare/cset/byte-slot/branch peephole
            # for a boolean consumed directly by br_cond.  Other integer SSA
            # values, including booleans used by scalar instructions, remain
            # eligible for this block-local slice.
            value_type_header: CompilerInt4 = kernel.type_header(value_type_id)
            term_header: CompilerInt4 = kernel.terminator_header(block_id)
            if (
                value_type_header.first == TYPE_KIND_INT
                and value_type_header.second == 1
                and term_header.first == PARSED_INSTRUCTION_KIND_BR_COND
                and term_header.third == dest_id
            ):
                position += 1
                continue
            last_use = kernel.last_use(block_id, dest_id)
            override_index = 0
            while override_index < len(last_use_override_ids):
                if last_use_override_ids[override_index] == dest_id:
                    last_use = last_use_override_positions[override_index]
                    break
                override_index += 1
            if last_use is None or last_use < position:
                position += 1
                continue
            if _interval_touches_call(position, last_use, call_positions):
                position += 1
                continue
            intervals.append((position, last_use, dest_id))
            position += 1

        intervals.sort(key=lambda interval: (interval[0], interval[1], interval[2]))
        active: list[tuple[int, int, int]] = []
        free_registers = list(_REGISTER_POOL)
        for start, end, value_id in intervals:
            still_active: list[tuple[int, int, int]] = []
            for active_end, active_id, register_index in active:
                # Uses are materialized before the current definition is
                # committed, so an interval ending at this instruction can
                # safely donate its register to the new result.
                if active_end <= start:
                    free_registers.append(register_index)
                else:
                    still_active.append((active_end, active_id, register_index))
            active = still_active
            free_registers.sort()

            register_index: int | None = None
            if free_registers:
                register_index = free_registers.pop(0)
            elif active:
                spill_index = 0
                index = 1
                while index < len(active):
                    if active[index][0] > active[spill_index][0]:
                        spill_index = index
                    index += 1
                spill_end, spill_id, spill_register = active[spill_index]
                if spill_end > end:
                    # Allocation is finalized before emission.  Removing this
                    # mapping makes the displaced value's definition and uses
                    # take the pre-existing stack-slot path; no runtime spill
                    # instruction has to be synthesized here.
                    kernel.clear_value_register(spill_id)
                    active.pop(spill_index)
                    register_index = spill_register
            if register_index is None:
                continue
            kernel.set_value_register(value_id, register_index)
            active.append((end, value_id, register_index))

    # Stack slots were assigned before this target combine was planned.  When
    # an extended operand did not win a register, retain the plan only if no
    # intervening definition reuses/overlaps that operand's spill slot.
    func.aarch64_madd_fusions = [
        fusion
        for fusion in func.aarch64_madd_fusions
        if _aarch64_madd_fusion_storage_is_safe(func, fusion)
    ]


def allocated_scalar_register_indexed(
    kernel: IndexedFunctionKernel, value_id: int, type_id: int,
) -> int:
    """Return a type-checked existing scalar assignment, without projecting IR."""
    if value_id < 0 or kernel.alloca_offset(value_id) >= 0:
        return -1
    header: CompilerInt4 = kernel.type_header(type_id)
    if header.first != TYPE_KIND_PTR and not (
        header.first == TYPE_KIND_INT and header.second in (1, 8, 16, 32, 64)
    ):
        return -1
    register_index = kernel.value_register(value_id)
    if register_index is None or register_index not in _REGISTER_POOL:
        return -1
    recorded_type_id = kernel.value_type_id(value_id)
    if recorded_type_id < 0 or not _register_type_ids_match(kernel, recorded_type_id, type_id):
        return -1
    return register_index


def allocated_register_name(
    func: ParsedFunction, value_name: str, value_type: TypeDesc
) -> str | None:
    """Return the width-correct register alias for a proven assignment."""

    if not _is_scalar_register_type(value_type):
        return None
    kernel = get_indexed_function_kernel(func)
    value_id = kernel.value_id(value_name)
    register_index = (
        None if value_id < 0 else kernel.value_register(value_id)
    )
    if register_index is None or register_index not in _REGISTER_POOL:
        return None
    recorded_type_id = kernel.value_type_id(value_id)
    expected_type_id = kernel.intern_type(value_type)
    if recorded_type_id < 0 or not _register_type_ids_match(
        kernel,
        recorded_type_id,
        expected_type_id,
    ):
        return None
    prefix = "x" if value_type.is_ptr or value_type.width > 32 else "w"
    return f"{prefix}{register_index}"


def commit_allocated_scalar_result(
    func: ParsedFunction,
    value_name: str,
    value_type: TypeDesc,
    source_reg: str,
) -> list[str] | None:
    """Move a result into its assigned register, or request slot fallback."""

    dest_reg = allocated_register_name(func, value_name, value_type)
    if dest_reg is None:
        return None
    if value_type.is_int and value_type.width <= 16:
        # Mirror the pre-existing slot round trip: i1/i8 use a byte slot and
        # i16 a halfword slot.  Wider sub-i32 values already use a word slot.
        mask = 0xFF if value_type.width <= 8 else 0xFFFF
        return [f"  and {dest_reg}, {source_reg}, #0x{mask:x}"]
    if dest_reg == source_reg:
        return []
    return [emitted_move_register_line(dest_reg, source_reg)]


def commit_allocated_scalar_result_indexed(
    func: ParsedFunction,
    value_id: int,
    type_id: int,
    source_reg: str,
) -> list[str] | None:
    kernel = get_indexed_function_kernel(func)
    register_index = kernel.value_register(value_id)
    if register_index is None or register_index not in _REGISTER_POOL:
        return None
    recorded_type_id = kernel.value_type_id(value_id)
    if recorded_type_id < 0 or not _register_type_ids_match(
        kernel,
        recorded_type_id,
        type_id,
    ):
        return None
    dest_reg = reg_name_indexed(kernel, type_id, register_index)
    type_header: CompilerInt4 = kernel.type_header(type_id)
    if type_header.first == TYPE_KIND_INT and type_header.second <= 16:
        mask = 0xFF if type_header.second <= 8 else 0xFFFF
        return [f"  and {dest_reg}, {source_reg}, #0x{mask:x}"]
    if dest_reg == source_reg:
        return []
    return [emitted_move_register_line(dest_reg, source_reg)]


__all__ = [
    "allocate_aarch64_block_registers",
    "allocated_scalar_register_indexed",
    "allocated_register_name",
    "commit_allocated_scalar_result",
    "commit_allocated_scalar_result_indexed",
]
