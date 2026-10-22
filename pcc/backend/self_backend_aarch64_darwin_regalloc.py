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

``PCC_SELF_CALLEE_SAVED_REGISTERS`` adds a second pool, ``x19``-``x28``, which
AAPCS64 makes callee-saved and which no emitter touches.  A value in that pool
survives calls, so intervals that cross a call, end at a call operand, or
belong to a scalar function argument may be allocated there; the prologue saves
exactly the pool registers the function uses and every epilogue restores them.
Intervals are function-wide (see ``_function_level_facts``).  In this mode
scalar PHI values and PHI inputs are candidates too: an incoming value is used
at its predecessor's last position, a PHI's interval starts just before its
block (``_append_phi_intervals``), and the edge copy moves between registers
and slots in parallel.  Values a stack-map reload rewrites after a safepoint
stay in their slots (``note_aarch64_reload_destinations``).
"""

import os

from .self_backend_analysis import (
    collect_block_local_last_uses,  # compatibility seam; indexed path never calls it
    is_local_value_ref,
)
from .self_backend_aarch64_darwin_abi import (
    aggregate_passed_indirect,
    assign_abi_arg_regs,
    reg_name_indexed,
)
from .self_backend_aarch64_darwin_mem import (
    emitted_memory_instruction_line,
    emitted_move_register_line,
)
from .self_backend_kernel import (
    _scalar_window_is_complete,
    IndexedFunctionKernel,
    TYPE_KIND_ARRAY,
    TYPE_KIND_INT,
    TYPE_KIND_PTR,
    TYPE_KIND_STRUCT,
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
    PARSED_INSTRUCTION_KIND_LOAD_ATOMIC,
    PARSED_INSTRUCTION_KIND_RET,
    PARSED_INSTRUCTION_KIND_RET_VOID,
    PARSED_INSTRUCTION_KIND_SELECT,
    PARSED_INSTRUCTION_KIND_STORE,
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
from .self_backend_value_arena import CompilerInt2, CompilerInt3, CompilerInt4


_REGISTER_POOL = (1, 2, 3, 4, 5, 6, 7, 8)
# Callee-saved by AAPCS64 (x18 is the Darwin platform register and x29/x30 are
# the frame record), so a value here survives every call.  No emitter uses
# these as scratch; the prologue saves the ones a function is assigned.
_CALLEE_SAVED_POOL = (19, 20, 21, 22, 23, 24, 25, 26, 27, 28)
# x0 is assignable only to argument 0 (see _append_argument_intervals).
_ASSIGNABLE_REGISTERS = (0,) + _REGISTER_POOL + _CALLEE_SAVED_POOL

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
        header_is_scalar = False
        if type(kernel) is IndexedFunctionKernel:
            header_scalar_id = raw.first
            header_is_scalar = _scalar_window_is_complete(
                kernel.type_scalars, header_scalar_id, 12, 0
            )
            if header_is_scalar:
                header_scalar = kernel.type_scalars.get_unchecked(
                    header_scalar_id * 12 + 0
                )
            else:
                header: CompilerInt4 = kernel.type_header(header_scalar_id)
        else:
            header: CompilerInt4 = kernel.type_header(raw.first)
        return (
            raw.first
            if (header_scalar if header_is_scalar else header.first) == TYPE_KIND_INT or (header_scalar if header_is_scalar else header.first) == TYPE_KIND_PTR
            else -1
        )
    if kind_id == PARSED_INSTRUCTION_KIND_BINOP:
        binop_type_is_scalar = False
        if type(kernel) is IndexedFunctionKernel:
            binop_type_scalar_id = raw.second
            binop_type_is_scalar = _scalar_window_is_complete(
                kernel.type_scalars, binop_type_scalar_id, 12, 0
            )
            if binop_type_is_scalar:
                binop_type_scalar = kernel.type_scalars.get_unchecked(
                    binop_type_scalar_id * 12 + 0
                )
            else:
                binop_type: CompilerInt4 = kernel.type_header(binop_type_scalar_id)
        else:
            binop_type: CompilerInt4 = kernel.type_header(raw.second)
        return (
            raw.second
            if (binop_type_scalar if binop_type_is_scalar else binop_type.first) == TYPE_KIND_INT
            else -1
        )
    if kind_id == PARSED_INSTRUCTION_KIND_ICMP:
        compared_is_scalar = False
        if type(kernel) is IndexedFunctionKernel:
            compared_scalar_id = raw.second
            compared_is_scalar = _scalar_window_is_complete(
                kernel.type_scalars, compared_scalar_id, 12, 0
            )
            if compared_is_scalar:
                compared_scalar = kernel.type_scalars.get_unchecked(
                    compared_scalar_id * 12 + 0
                )
            else:
                compared: CompilerInt4 = kernel.type_header(compared_scalar_id)
        else:
            compared: CompilerInt4 = kernel.type_header(raw.second)
        if (compared_scalar if compared_is_scalar else compared.first) != TYPE_KIND_INT and (compared_scalar if compared_is_scalar else compared.first) != TYPE_KIND_PTR:
            return -1
        return -1 if dest_id < 0 else kernel.value_type_id(dest_id)
    if kind_id == PARSED_INSTRUCTION_KIND_CAST:
        src_header_is_scalar = False
        if type(kernel) is IndexedFunctionKernel:
            src_header_scalar_id = raw.second
            src_header_is_scalar = _scalar_window_is_complete(
                kernel.type_scalars, src_header_scalar_id, 12, 0
            )
            if src_header_is_scalar:
                src_header_scalar = kernel.type_scalars.get_unchecked(
                    src_header_scalar_id * 12 + 0
                )
            else:
                src_header: CompilerInt4 = kernel.type_header(src_header_scalar_id)
        else:
            src_header: CompilerInt4 = kernel.type_header(raw.second)
        dst_header_is_scalar = False
        if type(kernel) is IndexedFunctionKernel:
            dst_header_scalar_id = raw.fourth
            dst_header_is_scalar = _scalar_window_is_complete(
                kernel.type_scalars, dst_header_scalar_id, 12, 0
            )
            if dst_header_is_scalar:
                dst_header_scalar = kernel.type_scalars.get_unchecked(
                    dst_header_scalar_id * 12 + 0
                )
            else:
                dst_header: CompilerInt4 = kernel.type_header(dst_header_scalar_id)
        else:
            dst_header: CompilerInt4 = kernel.type_header(raw.fourth)
        src_kind = (src_header_scalar if src_header_is_scalar else src_header.first)
        dst_kind = (dst_header_scalar if dst_header_is_scalar else dst_header.first)
        if (
            (src_kind == TYPE_KIND_INT or src_kind == TYPE_KIND_PTR)
            and (dst_kind == TYPE_KIND_INT or dst_kind == TYPE_KIND_PTR)
        ):
            return raw.fourth
        return -1
    result_header_is_scalar = False
    if type(kernel) is IndexedFunctionKernel:
        result_header_scalar_id = raw.first
        result_header_is_scalar = _scalar_window_is_complete(
            kernel.type_scalars, result_header_scalar_id, 12, 0
        )
        if result_header_is_scalar:
            result_header_scalar = kernel.type_scalars.get_unchecked(
                result_header_scalar_id * 12 + 0
            )
        else:
            result_header: CompilerInt4 = kernel.type_header(result_header_scalar_id)
    else:
        result_header: CompilerInt4 = kernel.type_header(raw.first)
    result_kind = (result_header_scalar if result_header_is_scalar else result_header.first)
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
    left_is_scalar = False
    if type(kernel) is IndexedFunctionKernel:
        left_scalar_id = left_id
        left_is_scalar = _scalar_window_is_complete(
            kernel.type_scalars, left_scalar_id, 12, 0
        )
        if left_is_scalar:
            left_scalar = kernel.type_scalars.get_unchecked(
                left_scalar_id * 12 + 0
            )
        else:
            left: CompilerInt4 = kernel.type_header(left_scalar_id)
    else:
        left: CompilerInt4 = kernel.type_header(left_id)
    right_is_scalar = False
    if type(kernel) is IndexedFunctionKernel:
        right_scalar_id = right_id
        right_is_scalar = _scalar_window_is_complete(
            kernel.type_scalars, right_scalar_id, 12, 0
        )
        if right_is_scalar:
            right_scalar = kernel.type_scalars.get_unchecked(
                right_scalar_id * 12 + 0
            )
        else:
            right: CompilerInt4 = kernel.type_header(right_scalar_id)
    else:
        right: CompilerInt4 = kernel.type_header(right_id)
    return (left_scalar if left_is_scalar else left.first) == TYPE_KIND_PTR and (right_scalar if right_is_scalar else right.first) == TYPE_KIND_PTR


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
    #
    # Positions are generated in increasing order, so the first position at or
    # after ``start`` decides.  A linear scan here made function-wide
    # allocation quadratic (every interval against every call) on the largest
    # functions.
    low = 0
    high = len(call_positions)
    while low < high:
        middle = (low + high) // 2
        if call_positions[middle] < start:
            low = middle + 1
        else:
            high = middle
    return low < len(call_positions) and call_positions[low] <= end


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


_FUNCTION_INTERVALS_ENV = "PCC_SELF_FUNCTION_LIVE_INTERVALS"
_CALL_RESULTS_ENV = "PCC_SELF_CALL_RESULT_REGISTERS"


def call_result_registers_enabled() -> bool:
    """Admit scalar call results as register candidates.

    A call result arrives in ``x0``.  Today every one is stored to its slot and
    reloaded at each use, even when nothing between the call and the last use
    can clobber x1-x8.  With this on, a result whose interval after the call
    touches no other call is committed with one ``mov`` into its pool register
    and its uses read that register; the slot stays allocated but is not
    written.  Results that cross another call, aggregate/indirect returns,
    planned tail calls and functions outside the indexed slot projection keep
    the slot path unchanged.

    Off by default while it is being qualified: unset, the emitter takes the
    byte-identical slot path.
    """
    value = str(os.environ.get(_CALL_RESULTS_ENV, "") or "").strip().lower()
    return value in ("1", "true", "yes", "on")


_CALLEE_SAVED_ENV = "PCC_SELF_CALLEE_SAVED_REGISTERS"


def callee_saved_registers_enabled() -> bool:
    """Allocate function-wide intervals over x1-x8 and callee-saved x19-x28.

    On by default.  ``PCC_SELF_CALLEE_SAVED_REGISTERS=0`` restores the
    block-local x1-x8 allocator selected by the two flags above, byte for
    byte (the emitter's operand selection keyed on this mode is off too).
    """
    value = str(os.environ.get(_CALLEE_SAVED_ENV, "") or "").strip().lower()
    return value not in ("0", "false", "no", "off")


def function_live_intervals_enabled() -> bool:
    """Widen live intervals from one block to the whole function.

    The candidate filter below is unchanged in either mode; only the interval
    each candidate is given differs.  Block-locally a value is live from its
    definition to its last use inside the defining block, and any use in
    another block disqualifies it.  Function-level, the interval runs to the
    last use anywhere, positions are numbered across the whole function, and a
    call anywhere in that span is still a barrier.  SSA guarantees a definition
    dominates every non-PHI use, so no dominator tree is needed; layout order
    over-approximates the paths in between, which can only reject.

    Off by default while it is being qualified: unset, the emitter takes the
    byte-identical block-local path.
    """

    value = str(os.environ.get(_FUNCTION_INTERVALS_ENV, "") or "").strip().lower()
    return value in ("1", "true", "yes", "on")


def _note_use_block(use_blocks: dict[int, list[int]], value_id: int, block_id: int) -> None:
    blocks = use_blocks.get(value_id)
    if blocks is None:
        use_blocks[value_id] = [block_id]
    elif blocks[len(blocks) - 1] != block_id:
        blocks.append(block_id)


# Host-only diagnostic seam. The adapter owns installation and restoration;
# the standalone implementation guard is also the native pruning boundary.
import sys
if sys.implementation.name == "cpython":
    _HOST_FACTS_STRUCTURE_OBSERVER = None


def _function_level_facts(
    kernel,
) -> tuple[
    list[int], dict[int, int], list[int], list[bool], list[int], list[int],
    dict[int, int],
]:
    """Global positions, function-wide last uses and barriers.

    Each block owns ``count + 1`` positions: its instructions, then its
    terminator.  A block the block-local scan would skip (an unclassified or
    rejected instruction kind) contributes every one of its positions as a
    barrier, so no interval may span it.  Besides the merged barrier list, the
    call positions and those unsafe-block ("hard") positions are returned
    separately: a callee-saved register survives a call but not an
    unclassified instruction.

    The last use is *not* the last textual use.  A value defined before a loop
    and used inside it is live at every point of the loop, including positions
    after its last textual use, because the back edge will bring control to
    that use again; a layout-order range ended there, let another value take
    the register in the tail of the loop, and left the value's stack slot
    unwritten at a safepoint the stack map believed it was live at -- the
    collector then read garbage (SIGBUS in ``freestanding_allocator``).  So the
    range end is the maximum of the last textual use and the end of every block
    the value is live-out of, from the standard backward dataflow
    ``live_in = upward_exposed | (live_out - defs)``; ``live_out = union of
    successors' live_in``.  This is what LLVM's ``liveintervals`` computes.
    """

    block_base: list[int] = []
    block_end: list[int] = []
    last_use: dict[int, int] = {}
    barriers: list[int] = []
    call_positions: list[int] = []
    hard_barriers: list[int] = []
    block_safe: list[bool] = []
    # SSA: each value has one defining block, so liveness is computed per
    # value by walking back from its uses (below) rather than as per-block
    # live sets.  Per-block sets held every value live across every block --
    # O(blocks x values) -- which pushed a Stage2 emit worker past its memory
    # share on the module initializers, the largest functions pcc emits.
    def_block: dict[int, int] = {}
    use_blocks: dict[int, list[int]] = {}
    position = 0
    block_id = 0
    while block_id < len(kernel.block_names):
        block_fact: CompilerInt4 = kernel.block_fact(block_id)
        block_base.append(position)
        safe = True
        instruction_index = 0
        while instruction_index < block_fact.second:
            metadata_is_scalar = False
            if type(kernel) is IndexedFunctionKernel:
                metadata_scalar_id = block_fact.first + instruction_index
                metadata_is_scalar = _scalar_window_is_complete(
                    kernel.instruction_metadata, metadata_scalar_id, 4, 0
                )
                if metadata_is_scalar:
                    metadata_scalar = kernel.instruction_metadata.get_unchecked(
                        metadata_scalar_id * 4 + 0
                    )
                else:
                    metadata: CompilerInt4 = kernel.instruction_metadata_by_id(metadata_scalar_id)
            else:
                metadata: CompilerInt4 = kernel.instruction_metadata_by_id(block_fact.first + instruction_index)
            if (
                not 0 <= (metadata_scalar if metadata_is_scalar else metadata.first) < len(PARSED_INSTRUCTION_KINDS)
                or (metadata_scalar if metadata_is_scalar else metadata.first) in _POOL_REJECTED_INSTRUCTION_KIND_IDS
            ):
                safe = False
            instruction_index += 1
        block_safe.append(safe)
        instruction_index = 0
        while instruction_index < block_fact.second:
            metadata_is_scalar = False
            if type(kernel) is IndexedFunctionKernel:
                metadata_scalar_id = block_fact.first + instruction_index
                metadata_is_scalar = _scalar_window_is_complete(
                    kernel.instruction_metadata, metadata_scalar_id, 4, 0
                )
                if metadata_is_scalar:
                    metadata_scalar = kernel.instruction_metadata.get_unchecked(
                        metadata_scalar_id * 4 + 0
                    )
                else:
                    metadata = kernel.instruction_metadata_by_id(metadata_scalar_id)
            else:
                metadata = kernel.instruction_metadata_by_id(block_fact.first + instruction_index)
            if not safe or (metadata_scalar if metadata_is_scalar else metadata.first) == PARSED_INSTRUCTION_KIND_CALL:
                barriers.append(position)
                if not safe:
                    hard_barriers.append(position)
                else:
                    call_positions.append(position)
            instruction_fact_is_scalar = False
            if type(kernel) is IndexedFunctionKernel:
                instruction_fact_scalar_id = block_fact.first + instruction_index
                instruction_fact_is_scalar = _scalar_window_is_complete(
                    kernel.instruction_facts, instruction_fact_scalar_id, 4, 0
                )
                if instruction_fact_is_scalar:
                    instruction_fact_scalar = kernel.instruction_facts.get_unchecked(
                        instruction_fact_scalar_id * 4 + 0
                    )
                else:
                    instruction_fact: CompilerInt4 = kernel.instruction_fact_by_id(instruction_fact_scalar_id)
            else:
                instruction_fact: CompilerInt4 = kernel.instruction_fact_by_id(block_fact.first + instruction_index)
            destination = (instruction_fact_scalar if instruction_fact_is_scalar else instruction_fact.first)
            if destination >= 0:
                def_block[destination] = block_id
            use_index = 0
            use_count = kernel.instruction_use_count(block_id, instruction_index)
            while use_index < use_count:
                used = kernel.instruction_use_id(block_id, instruction_index, use_index)
                last_use[used] = position
                _note_use_block(use_blocks, used, block_id)
                use_index += 1
            position += 1
            instruction_index += 1
        if not safe:
            barriers.append(position)
            hard_barriers.append(position)
        try:
            used = kernel.terminator_use_id(block_id, 0)
            last_use[used] = position
            _note_use_block(use_blocks, used, block_id)
        except IndexError:
            pass
        block_end.append(position)
        position += 1
        block_id += 1

    # A phi is defined on entry to its block, and each incoming value is read
    # by the edge copy emitted with its predecessor's terminator, so that
    # value is used at the predecessor's last position.
    block_id = 0
    while block_id < len(kernel.block_names):
        phi_fact = kernel.block_phi_fact(block_id)
        phi_index = 0
        while phi_index < phi_fact.second:
            phi: CompilerInt4 = kernel.phi_record(phi_fact.first + phi_index)
            if phi.first >= 0:
                def_block[phi.first] = block_id
            incoming_index = 0
            while incoming_index < phi.fourth:
                incoming = kernel.phi_incoming(phi.third + incoming_index)
                if incoming.first >= 0 and 0 <= incoming.second < len(block_end):
                    end = block_end[incoming.second]
                    if last_use.get(incoming.first, -1) < end:
                        last_use[incoming.first] = end
                    _note_use_block(use_blocks, incoming.first, incoming.second)
                incoming_index += 1
            phi_index += 1
        block_id += 1

    # Successors from the indexed terminators: br/br_cond targets and the
    # switch default plus its cases.
    successors: list[list[int]] = []
    block_id = 0
    while block_id < len(kernel.block_names):
        header: CompilerInt4 = kernel.terminator_header(block_id)
        span: CompilerInt4 = kernel.terminator_span(block_id)
        targets: list[int] = []
        if header.fourth >= 0:
            targets.append(header.fourth)
        if span.first >= 0:
            targets.append(span.first)
        case_index = 0
        while case_index < span.third:
            case = kernel.terminator_case(span.second + case_index)
            if case.second >= 0:
                targets.append(case.second)
            case_index += 1
        # An inline error edge leaves the block mid-way to its error target.
        # Treating the target as a block successor over-approximates the
        # values live at the edge, which can only lengthen intervals.
        error_span = kernel.inline_error_edge_span(block_id)
        error_index = 0
        while error_index < error_span.second:
            error_target = kernel.inline_error_edge_target(
                error_span.first + error_index
            )
            if error_target >= 0:
                targets.append(error_target)
            error_index += 1
        successors.append(targets)
        block_id += 1

    predecessors: list[list[int]] = [[] for _ in successors]
    block_id = 0
    while block_id < len(successors):
        for target in successors[block_id]:
            if 0 <= target < len(predecessors):
                predecessors[target].append(block_id)
        block_id += 1

    # Backward liveness per value.  A use outside the defining block makes the
    # value live into that block (SSA: a use in the defining block follows the
    # definition); a block the value is live into makes it live out of every
    # predecessor, and live into each predecessor that is not the defining
    # block.  This is the fixpoint ``live_in = upward_exposed |
    # (live_out - defs)``, ``live_out = union of successors' live_in``, solved
    # one value at a time.  A value live out of a block is live through that
    # block's last position (for a loop latch that extends the range across
    # the back edge); a value live into a block is live from its first
    # position -- layout order need not follow dominance, so such a block may
    # precede the definition.
    # The -1 value ID aliases the unvisited stamp and depends on previous
    # values' walks.  Custom IDs/seeds can expose hash, equality or indexing
    # calls.  In either case retain the original traversal for every value.
    cache_enabled = len(use_blocks) > 1
    if cache_enabled:
        for value_id in def_block:
            if type(value_id) is not int:
                cache_enabled = False
                break
        for value_id in use_blocks:
            if type(value_id) is not int or value_id == -1:
                cache_enabled = False
                break
    if cache_enabled:
        for value_id in use_blocks:
            for seed in use_blocks[value_id]:
                if type(seed) is not int:
                    cache_enabled = False
                    break
    # Home is -1 or a block index, so this pair has a collision-free integer
    # key: (home + 1) * block_count + seed.  Keep at most block_count entries;
    # once full, new keys take the old walk without evicting useful summaries.
    graph_low: dict[int, int] = {}
    graph_high: dict[int, int] = {}
    first_live: dict[int, int] = {}
    visited: list[int] = [-1] * len(successors)
    pending: list[int] = []
    facts_observe = False
    summary_enabled = cache_enabled and type(kernel) is IndexedFunctionKernel
    import sys
    if sys.implementation.name == "cpython":
        _facts_observer = _HOST_FACTS_STRUCTURE_OBSERVER
        _facts_sample = None
        if _facts_observer is not None:
            # The observer counts the established value walks and memo drains.
            # Keep that contract even when begin declines or observation fails.
            summary_enabled = False
            try:
                _facts_sample = _facts_observer.begin(
                    def_block, use_blocks, predecessors, block_base, block_end
                )
                facts_observe = _facts_sample is not None
            except BaseException:
                try:
                    _facts_observer.fail()
                except BaseException:
                    pass
    # Only exact production kernels with canonical, nonnegative values may
    # reorder private graph work. Collection and every accessor above remain
    # in their original order; custom/negative IDs and seeds retain that path.
    if summary_enabled:
        for value_id in use_blocks:
            if value_id < 0:
                summary_enabled = False
                break
            for seed in use_blocks[value_id]:
                if not 0 <= seed < len(predecessors):
                    summary_enabled = False
                    break

    summary_low: dict[int, int] = {}
    summary_high: dict[int, int] = {}
    if summary_enabled:
        # Group only value IDs, never per-value reachability sets. Identical
        # sole-seed queries keep the existing bounded memo. A shared home with
        # differing/multiple seeds offers reuse, but need not have overlapping
        # closures: in the zero-overlap case Tarjan adds bounded linear work.
        home_values: dict[int, list[int]] = {}
        home_seed: dict[int, int] = {}
        summary_index: list[int] = []
        summary_link: list[int] = []
        summary_component: list[int] = []
        summary_touched: list[int] = []
        summary_active: list[int] = []
        summary_nodes: list[int] = []
        summary_edges: list[int] = []
        summary_members: list[int] = []
        component_low: list[int] = []
        component_high: list[int] = []
        group_values: list[int] = []
        summary_complete = False
        try:
            for value_id in use_blocks:
                home = def_block.get(value_id, -1)
                query_seed = -1
                query_multiple = False
                for seed in use_blocks[value_id]:
                    if seed != home:
                        if query_seed < 0:
                            query_seed = seed
                        elif seed != query_seed:
                            query_multiple = True
                if query_seed >= 0:
                    if home not in home_values:
                        home_values[home] = []
                        home_seed[home] = query_seed
                    home_values[home].append(value_id)
                    if query_multiple or query_seed != home_seed[home]:
                        home_seed[home] = -2
            for home in home_values:
                group_values = home_values[home]
                if len(group_values) < 2 or home_seed[home] != -2:
                    continue
                if not summary_index:
                    summary_index = [-1] * len(predecessors)
                    summary_link = [-1] * len(predecessors)
                    summary_component = [-1] * len(predecessors)
                # Iterative Tarjan on inverse CFG edges with home removed.
                # Arrays are allocated once; only touched vertices are reset
                # between homes. The active/DFS/member stacks total O(B).
                for value_id in group_values:
                    for seed in use_blocks[value_id]:
                        if seed == home or summary_index[seed] >= 0:
                            continue
                        summary_index[seed] = len(summary_touched)
                        summary_link[seed] = summary_index[seed]
                        summary_touched.append(seed)
                        summary_active.append(seed)
                        summary_nodes.append(seed)
                        summary_edges.append(0)
                        while summary_nodes:
                            node_id = summary_nodes[-1]
                            edge_index = summary_edges[-1]
                            if edge_index < len(predecessors[node_id]):
                                predecessor = predecessors[node_id][edge_index]
                                summary_edges[-1] = edge_index + 1
                                if predecessor == home:
                                    continue
                                if summary_index[predecessor] < 0:
                                    summary_index[predecessor] = len(summary_touched)
                                    summary_link[predecessor] = summary_index[predecessor]
                                    summary_touched.append(predecessor)
                                    summary_active.append(predecessor)
                                    summary_nodes.append(predecessor)
                                    summary_edges.append(0)
                                elif summary_component[predecessor] < 0:
                                    if summary_index[predecessor] < summary_link[node_id]:
                                        summary_link[node_id] = summary_index[predecessor]
                                continue
                            summary_nodes.pop()
                            summary_edges.pop()
                            if summary_link[node_id] == summary_index[node_id]:
                                component_id = len(component_low)
                                # Assign every member before folding edges,
                                # so an internal cycle never reads an unfinished
                                # component's extrema. Other targets are done.
                                while True:
                                    member_id = summary_active.pop()
                                    summary_component[member_id] = component_id
                                    summary_members.append(member_id)
                                    if member_id == node_id:
                                        break
                                lowest = -1
                                highest = -1
                                for member_id in summary_members:
                                    base = block_base[member_id]
                                    if lowest < 0 or base < lowest:
                                        lowest = base
                                    for predecessor in predecessors[member_id]:
                                        # Even an edge cut at home makes the
                                        # value live through home's block_end.
                                        end = block_end[predecessor]
                                        if end > highest:
                                            highest = end
                                        if predecessor != home:
                                            target_component = summary_component[predecessor]
                                            if target_component != component_id:
                                                low = component_low[target_component]
                                                high = component_high[target_component]
                                                if low < lowest:
                                                    lowest = low
                                                if high > highest:
                                                    highest = high
                                component_low.append(lowest)
                                component_high.append(highest)
                                summary_members.clear()
                            if summary_nodes:
                                parent_id = summary_nodes[-1]
                                if summary_link[node_id] < summary_link[parent_id]:
                                    summary_link[parent_id] = summary_link[node_id]
                    lowest = -1
                    highest = -1
                    for seed in use_blocks[value_id]:
                        if seed != home:
                            component_id = summary_component[seed]
                            low = component_low[component_id]
                            high = component_high[component_id]
                            if lowest < 0 or low < lowest:
                                lowest = low
                            if high > highest:
                                highest = high
                    summary_low[value_id] = lowest
                    summary_high[value_id] = highest
                for member_id in summary_touched:
                    summary_index[member_id] = -1
                    summary_link[member_id] = -1
                    summary_component[member_id] = -1
                summary_touched.clear()
                component_low.clear()
                component_high.clear()
            summary_complete = True
        finally:
            # Release graph scratch on success and on failure, including a
            # partially discovered SCC. Only O(V) scalar results survive to
            # the original value-order publication below.
            group_values.clear()
            home_values.clear()
            home_seed.clear()
            summary_index.clear()
            summary_link.clear()
            summary_component.clear()
            summary_touched.clear()
            summary_active.clear()
            summary_nodes.clear()
            summary_edges.clear()
            summary_members.clear()
            component_low.clear()
            component_high.clear()
            if not summary_complete:
                summary_low.clear()
                summary_high.clear()

    for value_id in use_blocks:
        home = def_block.get(value_id, -1)
        lowest = -1
        textual_highest = last_use[value_id]
        if summary_enabled and value_id in summary_low:
            # Publish all dictionaries in the original use/value order; a
            # graph maximum must never absorb another value's textual use.
            lowest = summary_low[value_id]
            highest = summary_high[value_id]
            if textual_highest > highest:
                highest = textual_highest
            last_use[value_id] = highest
            if lowest >= 0:
                first_live[value_id] = lowest
            continue
        highest = textual_highest
        if cache_enabled:
            highest = -1
        for block_id in use_blocks[value_id]:
            if block_id != home and visited[block_id] != value_id:
                visited[block_id] = value_id
                pending.append(block_id)
        cache_key = -1
        cache_miss = False
        if cache_enabled and len(pending) == 1:
            seed = pending[0]
            cache_key = (home + 1) * len(successors) + seed
            cached_low = graph_low.get(cache_key, -1)
            if cached_low >= 0:
                lowest = cached_low
                highest = graph_high[cache_key]
                pending.pop()
            elif len(graph_low) < len(successors):
                cache_miss = True
        # Disabled observation pays only one local predicate per value.
        # Enabled observation retains the explicit native pruning guard and
        # executes the identical business body before recording a whole row.
        if facts_observe:
            import sys
            if sys.implementation.name == "cpython":
                if _facts_sample is not None:
                    _facts_query = False
                    try:
                        _facts_query = _facts_observer.query(
                            _facts_sample, home, bool(pending),
                            cache_enabled and cache_key >= 0 and cached_low >= 0,
                        )
                    except BaseException:
                        _facts_sample = None
                        facts_observe = False
                        try:
                            _facts_observer.fail()
                        except BaseException:
                            pass
                    if _facts_query:
                        while pending:
                            block_id = pending.pop()
                            base = block_base[block_id]
                            if lowest < 0 or base < lowest:
                                lowest = base
                            for predecessor in predecessors[block_id]:
                                end = block_end[predecessor]
                                if end > highest:
                                    highest = end
                                if predecessor != home and visited[predecessor] != value_id:
                                    visited[predecessor] = value_id
                                    pending.append(predecessor)
                            try:
                                _facts_more = _facts_observer.row(
                                    _facts_sample, block_id, len(predecessors[block_id])
                                )
                            except BaseException:
                                _facts_more = False
                                _facts_sample = None
                                try:
                                    _facts_observer.fail()
                                except BaseException:
                                    pass
                            if not _facts_more:
                                facts_observe = False
                                break
        while pending:
            block_id = pending.pop()
            base = block_base[block_id]
            if lowest < 0 or base < lowest:
                lowest = base
            for predecessor in predecessors[block_id]:
                end = block_end[predecessor]
                if end > highest:
                    highest = end
                if predecessor != home and visited[predecessor] != value_id:
                    visited[predecessor] = value_id
                    pending.append(predecessor)
        if cache_miss:
            # Cache only graph extrema: different values at this same seed
            # can have different textual or PHI last-use positions.
            graph_low[cache_key] = lowest
            graph_high[cache_key] = highest
        if cache_enabled and textual_highest > highest:
            highest = textual_highest
        last_use[value_id] = highest
        if lowest >= 0:
            first_live[value_id] = lowest
    summary_low.clear()
    summary_high.clear()
    facts_result = (
        block_base,
        last_use,
        barriers,
        block_safe,
        call_positions,
        hard_barriers,
        first_live,
    )
    import sys
    if sys.implementation.name == "cpython":
        if _facts_sample is not None:
            try:
                _facts_observer.complete(_facts_sample)
            except BaseException:
                try:
                    _facts_observer.fail()
                except BaseException:
                    pass
    return facts_result


def _linear_scan_assign(kernel, intervals: list[tuple[int, int, int]]) -> None:
    """Assign pool registers to sorted intervals, evicting the longest."""

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


def _linear_scan_assign_pools(
    kernel,
    intervals: list[tuple[int, int, int, bool]],
    fixed_registers: dict[int, int],
) -> list[int]:
    """Two-pool linear scan; return the callee-saved registers it assigned.

    An interval flagged ``needs_callee_saved`` (it crosses a call, ends at a
    call operand, or is a function argument) may only take x19-x28.  Every
    other interval prefers x1-x8, which cost nothing to preserve, and falls
    back to x19-x28.  Eviction follows ``_linear_scan_assign``: the
    furthest-ending active interval holding a register this interval may use
    is displaced to its stack slot when it outlives this one.
    """

    intervals.sort(key=lambda interval: (interval[0], interval[1], interval[2]))
    active: list[tuple[int, int, int]] = []
    free_caller = list(_REGISTER_POOL)
    free_callee = list(_CALLEE_SAVED_POOL)
    for start, end, value_id, needs_callee_saved in intervals:
        still_active: list[tuple[int, int, int]] = []
        for active_end, active_id, register_index in active:
            # Uses are materialized before the current definition is
            # committed, so an interval ending at this instruction can
            # safely donate its register to the new result.
            if active_end <= start:
                if register_index == 0:
                    # x0 belongs to argument 0 only; it is never pooled.
                    continue
                if register_index >= _CALLEE_SAVED_POOL[0]:
                    free_callee.append(register_index)
                else:
                    free_caller.append(register_index)
            else:
                still_active.append((active_end, active_id, register_index))
        active = still_active
        free_caller.sort()
        free_callee.sort()

        register_index = -1
        fixed = fixed_registers.get(value_id, -1)
        if fixed == 0 and not needs_callee_saved:
            register_index = 0
        elif fixed > 0 and not needs_callee_saved and fixed in free_caller:
            free_caller.remove(fixed)
            register_index = fixed
        elif not needs_callee_saved and free_caller:
            register_index = free_caller.pop(0)
        elif free_callee:
            register_index = free_callee.pop(0)
        else:
            spill_index = -1
            index = 0
            while index < len(active):
                if (
                    active[index][2] != 0
                    and (
                        not needs_callee_saved
                        or active[index][2] >= _CALLEE_SAVED_POOL[0]
                    )
                ) and (
                    spill_index < 0 or active[index][0] > active[spill_index][0]
                ):
                    spill_index = index
                index += 1
            if spill_index >= 0 and active[spill_index][0] > end:
                # As in ``_linear_scan_assign``: the displaced value takes the
                # stack-slot path for its definition and every use.
                spill_end, spill_id, spill_register = active.pop(spill_index)
                kernel.clear_value_register(spill_id)
                register_index = spill_register
        if register_index < 0:
            continue
        kernel.set_value_register(value_id, register_index)
        active.append((end, value_id, register_index))

    used = [False] * len(_CALLEE_SAVED_POOL)
    for _start, _end, value_id, _needs in intervals:
        register_index = kernel.value_register(value_id)
        if register_index is not None and register_index >= _CALLEE_SAVED_POOL[0]:
            used[register_index - _CALLEE_SAVED_POOL[0]] = True
    return [
        _CALLEE_SAVED_POOL[index]
        for index in range(len(_CALLEE_SAVED_POOL))
        if used[index]
    ]


def _is_noop_word_cast(kernel, cast_record: CompilerInt4) -> bool:
    """A cast whose 64-bit result has exactly its source's bits."""

    op = kernel.call_texts[cast_record.first]
    if op != "ptrtoint" and op != "inttoptr" and op != "bitcast":
        return False
    for type_id in (cast_record.second, cast_record.fourth):
        header: CompilerInt4 = kernel.type_header(type_id)
        if header.first == TYPE_KIND_PTR:
            continue
        if header.first == TYPE_KIND_INT and header.second == 64:
            continue
        return False
    return True


def _alias_root(parent: dict[int, int], value_id: int) -> int:
    root = value_id
    while parent.get(root, root) != root:
        root = parent[root]
    return root


def _merge_alias_groups(
    intervals: list[tuple[int, int, int, bool]],
    alias_pairs: list[tuple[int, int]],
) -> tuple[list[tuple[int, int, int, bool]], dict[int, list[int]]]:
    """Coalesce no-op casts with their sources into one register group.

    SSA values are never modified, and a no-op word cast's result holds its
    source's bits, so the pair may share one register for the union of their
    intervals.  Each group is keyed by its smallest value id; only candidates
    (values with an interval) are merged.
    """

    has_interval: set[int] = set()
    for _start, _end, value_id, _needs in intervals:
        has_interval.add(value_id)
    parent: dict[int, int] = {}
    for dest_id, source_id in alias_pairs:
        if dest_id not in has_interval or source_id not in has_interval:
            continue
        dest_root = _alias_root(parent, dest_id)
        source_root = _alias_root(parent, source_id)
        if dest_root == source_root:
            continue
        if dest_root < source_root:
            parent[source_root] = dest_root
        else:
            parent[dest_root] = source_root

    group_start: dict[int, int] = {}
    group_end: dict[int, int] = {}
    group_needs: dict[int, bool] = {}
    members: dict[int, list[int]] = {}
    order: list[int] = []
    for start, end, value_id, needs in intervals:
        root = _alias_root(parent, value_id)
        if root not in members:
            members[root] = []
            order.append(root)
            group_start[root] = start
            group_end[root] = end
            group_needs[root] = needs
        else:
            if start < group_start[root]:
                group_start[root] = start
            if end > group_end[root]:
                group_end[root] = end
            if needs:
                group_needs[root] = True
        members[root].append(value_id)
    merged: list[tuple[int, int, int, bool]] = []
    for root in order:
        merged.append((group_start[root], group_end[root], root, group_needs[root]))
    return merged, members


def _publish_group_registers(
    kernel,
    group_intervals: list[tuple[int, int, int, bool]],
    members: dict[int, list[int]],
) -> list[int]:
    """Give every group member its root's register; return used x19-x28."""

    used = [False] * len(_CALLEE_SAVED_POOL)
    for _start, _end, root, _needs in group_intervals:
        register_index = kernel.value_register(root)
        for value_id in members[root]:
            if register_index is None:
                kernel.clear_value_register(value_id)
            else:
                kernel.set_value_register(value_id, register_index)
        if register_index is not None and register_index >= _CALLEE_SAVED_POOL[0]:
            used[register_index - _CALLEE_SAVED_POOL[0]] = True
    return [
        _CALLEE_SAVED_POOL[index]
        for index in range(len(_CALLEE_SAVED_POOL))
        if used[index]
    ]


def _append_argument_intervals(
    func: ParsedFunction,
    kernel,
    phi_input_ids: set[int],
    global_last_use: dict[int, int],
    hard_barriers: list[int],
    call_barriers: list[int],
    reload_offsets: set[int],
    intervals: list[tuple[int, int, int, bool]],
    fixed_registers: dict[int, int],
) -> None:
    """Add register-passed scalar arguments as candidates.

    An argument is defined on entry (position -1) and is committed by the
    prologue.  One whose interval crosses a call is restricted to x19-x28,
    so that its commit can never overwrite another incoming argument
    register that the prologue has yet to read.  Any other argument stays in
    the register it arrived in (``fixed_registers``): its commit is no move
    at all, and x0, which only calls and returns write, is used for nothing
    else.
    """

    arg_types = [arg.type for arg in func.args]
    arg_regs = assign_abi_arg_regs(arg_types)
    arg_index = 0
    while arg_index < len(func.args):
        arg = func.args[arg_index]
        regs = arg_regs[arg_index]
        arg_index += 1
        if len(regs) != 1 or regs[0][0] not in ("x", "w"):
            continue
        if aggregate_passed_indirect(arg.type):
            continue
        value_id = kernel.value_id(arg.name)
        if value_id < 0 or value_id in phi_input_ids:
            continue
        if kernel.alloca_offset(value_id) >= 0:
            continue
        type_id = kernel.value_type_id(value_id)
        if type_id < 0:
            continue
        header: CompilerInt4 = kernel.type_header(type_id)
        if header.first != TYPE_KIND_PTR and not (
            header.first == TYPE_KIND_INT
            and header.second in (1, 8, 16, 32, 64)
        ):
            continue
        slot_offset = kernel.value_slot_offset(value_id)
        if slot_offset < 0 or slot_offset in reload_offsets:
            continue
        last_use = global_last_use.get(value_id)
        if last_use is None or last_use < 0:
            continue
        if _interval_touches_call(0, last_use, hard_barriers):
            continue
        crosses_call = _interval_touches_call(0, last_use, call_barriers)
        if not crosses_call:
            fixed_registers[value_id] = int(regs[0][1:])
        intervals.append((-1, last_use, value_id, crosses_call))


def _append_phi_intervals(
    kernel,
    block_id: int,
    block_base: list[int],
    global_last_use: dict[int, int],
    first_live: dict[int, int],
    hard_barriers: list[int],
    call_barriers: list[int],
    reload_offsets: set[int],
    intervals: list[tuple[int, int, int, bool]],
) -> None:
    """Add a block's scalar phi results as candidates.

    A phi is written by the edge copies at the end of each predecessor, so
    its interval starts one position before its block.  Every value live
    into the block is defined earlier and so overlaps it; a value whose last
    use is a predecessor's final position may donate its register, which
    makes that edge copy a no-op.  Clobbers are scanned from the block on:
    the copies run after the predecessor's last call.
    """

    phi_fact = kernel.block_phi_fact(block_id)
    phi_index = 0
    while phi_index < phi_fact.second:
        phi: CompilerInt4 = kernel.phi_record(phi_fact.first + phi_index)
        header_is_scalar = False
        if type(kernel) is IndexedFunctionKernel:
            header_scalar_id = phi.second
            header_is_scalar = _scalar_window_is_complete(
                kernel.type_scalars, header_scalar_id, 12, 0
            )
            if header_is_scalar:
                header_scalar = kernel.type_scalars.get_unchecked(
                    header_scalar_id * 12 + 0
                )
            else:
                header: CompilerInt4 = kernel.type_header(header_scalar_id)
        else:
            header: CompilerInt4 = kernel.type_header(phi.second)
        if (header_scalar if header_is_scalar else header.first) == TYPE_KIND_ARRAY or (header_scalar if header_is_scalar else header.first) == TYPE_KIND_STRUCT:
            # An aggregate phi sends the whole edge down the slot-only path.
            return
        phi_index += 1
    base = block_base[block_id]
    start = base - 1
    phi_index = 0
    while phi_index < phi_fact.second:
        phi = kernel.phi_record(phi_fact.first + phi_index)
        phi_index += 1
        value_id = phi.first
        if value_id < 0:
            continue
        header = kernel.type_header(phi.second)
        if header.first != TYPE_KIND_PTR and not (
            header.first == TYPE_KIND_INT and header.second in (1, 8, 16, 32, 64)
        ):
            continue
        recorded_type_id = kernel.value_type_id(value_id)
        if recorded_type_id < 0 or not _register_type_ids_match(
            kernel, recorded_type_id, phi.second
        ):
            continue
        slot_offset = kernel.value_slot_offset(value_id)
        if slot_offset < 0 or slot_offset in reload_offsets:
            continue
        last_use = global_last_use.get(value_id)
        if last_use is None or last_use < base:
            continue
        if first_live.get(value_id, start) < start:
            continue
        if _interval_touches_call(base, last_use, hard_barriers):
            continue
        intervals.append(
            (
                start,
                last_use,
                value_id,
                _interval_touches_call(base, last_use, call_barriers),
            )
        )


def _fusable_branch_conditions(kernel) -> dict[int, int]:
    """Compares a block's ``br_cond`` can take from the flags.

    An integer or pointer icmp that is its block's last instruction, and
    whose only use in the function is that block's ``br_cond``, needs no i1:
    the compare sets NZCV and the terminator branches on it.  Nothing is
    emitted between the two but a safepoint label and its ``nop``; the edge
    copies after the branch move values without touching the flags.
    Returns ``{icmp value id: block id}``.
    """

    candidates: dict[int, int] = {}
    block_id = 0
    while block_id < len(kernel.block_names):
        header: CompilerInt4 = kernel.terminator_header(block_id)
        block_fact: CompilerInt4 = kernel.block_fact(block_id)
        if (
            header.first == PARSED_INSTRUCTION_KIND_BR_COND
            and header.third >= 0
            and block_fact.second > 0
        ):
            last_id = block_fact.first + block_fact.second - 1
            metadata: CompilerInt4 = kernel.instruction_metadata_by_id(last_id)
            fact: CompilerInt4 = kernel.instruction_fact_by_id(last_id)
            if (
                metadata.first == PARSED_INSTRUCTION_KIND_ICMP
                and fact.first == header.third
            ):
                icmp: CompilerInt4 = kernel.instruction_record(metadata.second)
                type_header: CompilerInt4 = kernel.type_header(icmp.second)
                if type_header.first == TYPE_KIND_PTR or (
                    type_header.first == TYPE_KIND_INT
                    and type_header.second in (1, 8, 16, 32, 64)
                ):
                    candidates[fact.first] = block_id
        block_id += 1
    if not candidates:
        return candidates

    # Every use other than the terminator that owns the compare disqualifies
    # it: instruction operands, other terminators, phi inputs and the
    # conditions of inline error edges all read the materialised i1.
    rejected: dict[int, bool] = {}
    block_id = 0
    while block_id < len(kernel.block_names):
        block_fact = kernel.block_fact(block_id)
        instruction_index = 0
        while instruction_index < block_fact.second:
            use_count = kernel.instruction_use_count(block_id, instruction_index)
            use_index = 0
            while use_index < use_count:
                used = kernel.instruction_use_id(block_id, instruction_index, use_index)
                if used in candidates:
                    rejected[used] = True
                use_index += 1
            instruction_index += 1
        if kernel.terminator_use_count(block_id) == 1:
            used = kernel.terminator_use_id(block_id, 0)
            if used in candidates and candidates[used] != block_id:
                rejected[used] = True
        phi_fact = kernel.block_phi_fact(block_id)
        phi_index = 0
        while phi_index < phi_fact.second:
            phi: CompilerInt4 = kernel.phi_record(phi_fact.first + phi_index)
            incoming_index = 0
            while incoming_index < phi.fourth:
                incoming = kernel.phi_incoming(phi.third + incoming_index)
                if incoming.first in candidates:
                    rejected[incoming.first] = True
                incoming_index += 1
            phi_index += 1
        error_span = kernel.inline_error_edge_span(block_id)
        error_index = 0
        while error_index < error_span.second:
            condition = kernel.inline_error_edge_condition(
                error_span.first + error_index
            )
            if condition in candidates:
                rejected[condition] = True
            error_index += 1
        block_id += 1
    fused: dict[int, int] = {}
    for value_id in candidates:
        if value_id not in rejected:
            fused[value_id] = candidates[value_id]
    return fused


_FRAMELESS_INSTRUCTION_KIND_IDS = (
    PARSED_INSTRUCTION_KIND_BINOP,
    PARSED_INSTRUCTION_KIND_CAST,
    PARSED_INSTRUCTION_KIND_FREEZE,
    PARSED_INSTRUCTION_KIND_GEP,
    PARSED_INSTRUCTION_KIND_ICMP,
    PARSED_INSTRUCTION_KIND_LOAD,
    PARSED_INSTRUCTION_KIND_SELECT,
    PARSED_INSTRUCTION_KIND_STORE,
)
_FRAMELESS_TERMINATOR_KIND_IDS = (
    PARSED_INSTRUCTION_KIND_BR,
    PARSED_INSTRUCTION_KIND_BR_COND,
    PARSED_INSTRUCTION_KIND_RET,
    PARSED_INSTRUCTION_KIND_RET_VOID,
    PARSED_INSTRUCTION_KIND_SWITCH,
    PARSED_INSTRUCTION_KIND_UNREACHABLE,
)


def _value_needs_slot(func: ParsedFunction, kernel, value_id: int) -> bool:
    """True when emitting ``value_id`` touches its stack slot."""

    if value_id < 0 or kernel.value_slot_id(value_id) < 0:
        return False
    if allocated_scalar_register_indexed(
        kernel, value_id, kernel.value_type_id(value_id)
    ) >= 0:
        return False
    if value_id in func.aarch64_fused_branch_values:
        return False
    return aarch64_madd_fusion_for_product(func, kernel.value_name(value_id)) is None


def _is_frameless_leaf(func: ParsedFunction, kernel) -> bool:
    """Whether the function can run without a frame record or stack.

    Only integer/pointer loads, stores and arithmetic with branches and
    returns, and every argument, phi and result in a register: then nothing
    addresses a slot through x29, no call needs LR saved, and no callee-saved
    register was assigned.  Anything else keeps the frame.
    """

    if func.aarch64_callee_saved or func.is_vararg:
        return False
    if kernel.hidden_sret_slot_id >= 0:
        return False
    arg_types = [arg.type for arg in func.args]
    arg_regs = assign_abi_arg_regs(arg_types)
    arg_index = 0
    while arg_index < len(func.args):
        arg = func.args[arg_index]
        regs = arg_regs[arg_index]
        arg_index += 1
        value_id = kernel.value_id(arg.name)
        if value_id < 0 or kernel.value_slot_id(value_id) < 0:
            continue
        if len(regs) != 1 or aggregate_passed_indirect(arg.type):
            return False
        if _value_needs_slot(func, kernel, value_id):
            return False
    block_id = 0
    while block_id < len(kernel.block_names):
        header: CompilerInt4 = kernel.terminator_header(block_id)
        if header.first not in _FRAMELESS_TERMINATOR_KIND_IDS:
            return False
        error_edge_span: CompilerInt2 = kernel.inline_error_edge_span(block_id)
        if error_edge_span.second:
            return False
        phi_fact = kernel.block_phi_fact(block_id)
        phi_index = 0
        while phi_index < phi_fact.second:
            phi: CompilerInt4 = kernel.phi_record(phi_fact.first + phi_index)
            if _value_needs_slot(func, kernel, phi.first):
                return False
            phi_index += 1
        block_fact: CompilerInt4 = kernel.block_fact(block_id)
        instruction_index = 0
        while instruction_index < block_fact.second:
            instruction_id = block_fact.first + instruction_index
            metadata: CompilerInt4 = kernel.instruction_metadata_by_id(instruction_id)
            if metadata.first not in _FRAMELESS_INSTRUCTION_KIND_IDS:
                return False
            fact: CompilerInt4 = kernel.instruction_fact_by_id(instruction_id)
            if _value_needs_slot(func, kernel, fact.first):
                return False
            instruction_index += 1
        block_id += 1
    return True


def note_aarch64_reload_destinations(func: ParsedFunction, plan) -> None:
    """Record the spill slots the stack map rewrites after a safepoint.

    A managed value live across a safepoint is reloaded from its root into
    its spill slot once the call returns (a relocating collector may have
    moved the object).  The reload writes only the slot, so such a value
    must be read from the slot: in x19-x28 it would keep the pre-move
    address.
    """

    offsets: list[int] = []
    # Keep the public first-seen order, but do not rescan every earlier slot
    # for each safepoint's repeated reload. Only membership uses this set.
    seen: set[int] = set()
    records = getattr(plan, "packed_records", None)
    if records is not None:
        index = 0
        count = len(records.reload_scalars) // 3
        while index < count:
            # One name per type: pcc types a local once per function.
            packed: CompilerInt3 = records.reload_scalars.get3_unchecked(index)
            offset = -packed.second
            if offset not in seen:
                seen.add(offset)
                offsets.append(offset)
            index += 1
    else:
        for record in getattr(plan, "records", ()):
            for planned in record.reloads:
                offset = -planned.destination_offset
                if offset not in seen:
                    seen.add(offset)
                    offsets.append(offset)
    func.aarch64_reload_slot_offsets = offsets


def allocate_aarch64_block_registers(func: ParsedFunction) -> None:
    """Populate the indexed kernel with conservative linear-scan picks."""

    kernel = get_indexed_function_kernel(func)
    kernel.clear_value_registers()
    func.aarch64_callee_saved = []
    func.aarch64_fused_branch_values = {}
    func.aarch64_frameless = False
    if func.is_vararg:
        return
    function_level = function_live_intervals_enabled()
    call_results = call_result_registers_enabled() and bool(
        getattr(func, "indexed_slot_projection", False)
    )
    # The callee-saved mode needs whole-function intervals (a value that
    # survives a call almost always spans blocks) and call results.
    callee_saved = callee_saved_registers_enabled() and bool(
        getattr(func, "indexed_slot_projection", False)
    )
    if callee_saved:
        function_level = True
        call_results = True
        func.aarch64_fused_branch_values = _fusable_branch_conditions(kernel)
    pool_intervals: list[tuple[int, int, int, bool]] = []
    alias_pairs: list[tuple[int, int]] = []
    call_barriers: list[int] = []
    hard_barriers: list[int] = []
    first_live: dict[int, int] = {}
    # The callee-saved mode allocates phis and their inputs (the edge copies
    # move between registers and slots); the block-local modes keep both in
    # their slots.
    phi_input_ids: set[int] = set()
    # The list remains the externally visible, deterministic first-seen
    # projection. This function-local index is never used for iteration or
    # allocation order and is not cached on a reused ParsedFunction.
    reload_offsets: set[int] = set()
    if callee_saved:
        for reload_offset in func.aarch64_reload_slot_offsets:
            reload_offsets.add(reload_offset)
    block_id = 0
    while not callee_saved and block_id < len(kernel.block_names):
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

    block_base: list[int] = []
    global_last_use: dict[int, int] = {}
    global_barriers: list[int] = []
    block_safe: list[bool] = []
    function_intervals: list[tuple[int, int, int]] = []
    if function_level:
        (
            block_base,
            global_last_use,
            global_barriers,
            block_safe,
            call_barriers,
            hard_barriers,
            first_live,
        ) = _function_level_facts(kernel)
        # A fused multiply reads its inputs at the consumer, not at the mul.
        # The per-block overrides below cover instruction results; arguments
        # and phis take their intervals straight from these last uses.
        for fusion in func.aarch64_madd_fusions:
            fusion_block_id = kernel.block_id(fusion.block_name)
            if fusion_block_id < 0:
                continue
            consumer_position = block_base[fusion_block_id] + fusion.consumer_index
            for operand in (fusion.mul_lhs, fusion.mul_rhs):
                operand_id = kernel.value_id(operand)
                if operand_id < 0:
                    continue
                known = global_last_use.get(operand_id)
                if known is None or known < consumer_position:
                    global_last_use[operand_id] = consumer_position

    for block_id in range(len(kernel.block_names)):
        block_name = kernel.block_names[block_id]
        block_fact: CompilerInt4 = kernel.block_fact(block_id)
        instruction_count = block_fact.second
        if function_level:
            block_is_safe = block_safe[block_id]
        else:
            block_is_safe = True
            instruction_index = 0
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
        if callee_saved:
            _append_phi_intervals(
                kernel,
                block_id,
                block_base,
                global_last_use,
                first_live,
                hard_barriers,
                call_barriers,
                reload_offsets,
                pool_intervals,
            )

        call_positions: list[int] = []
        if not function_level:
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
        base = block_base[block_id] if function_level else 0
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
            is_call_result = False
            if kind_id == PARSED_INSTRUCTION_KIND_CALL:
                if not call_results:
                    position += 1
                    continue
                call_id = metadata.second
                if call_id in func.aarch64_tail_call_ids:
                    # The return terminator consumes x0 directly; there is no
                    # post-call point at which to commit the result.
                    position += 1
                    continue
                call_header: CompilerInt4 = kernel.call_header(call_id)
                if not (call_header.third & 1) and kernel.call_texts[
                    call_header.second
                ].startswith("llvm."):
                    # Intrinsics are lowered inline by emitters that store
                    # their result to its slot themselves; only the generic
                    # call path commits through the allocator.
                    position += 1
                    continue
                ret_header: CompilerInt4 = kernel.type_header(call_header.first)
                if not (
                    ret_header.first == TYPE_KIND_PTR
                    or (
                        ret_header.first == TYPE_KIND_INT
                        and ret_header.second in (1, 8, 16, 32, 64)
                    )
                ):
                    position += 1
                    continue
                value_type_id = call_header.first
                is_call_result = True
            if is_call_result:
                pass
            elif kind_id == PARSED_INSTRUCTION_KIND_LOAD_ATOMIC:
                # Atomic loads remain outside this allocator's existing
                # candidate set. Their fixed record needs no diagnostic row.
                value_type_id = -1
            elif (
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
                gep_type_is_scalar = False
                if type(kernel) is IndexedFunctionKernel:
                    gep_type_scalar_id = gep_span.second
                    gep_type_is_scalar = _scalar_window_is_complete(
                        kernel.type_scalars, gep_type_scalar_id, 12, 0
                    )
                    if gep_type_is_scalar:
                        gep_type_scalar = kernel.type_scalars.get_unchecked(
                            gep_type_scalar_id * 12 + 0
                        )
                    else:
                        gep_type: CompilerInt4 = kernel.type_header(gep_type_scalar_id)
                else:
                    gep_type: CompilerInt4 = kernel.type_header(gep_span.second)
                value_type_id = (
                    gep_span.second
                    if (gep_type_scalar if gep_type_is_scalar else gep_type.first) == TYPE_KIND_PTR
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
            if kernel.value_slot_offset(dest_id) in reload_offsets:
                # A safepoint reload rewrites this slot; see
                # ``note_aarch64_reload_destinations``.
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
            if function_level:
                last_use = global_last_use.get(dest_id)
            else:
                last_use = kernel.last_use(block_id, dest_id)
            override_index = 0
            while override_index < len(last_use_override_ids):
                if last_use_override_ids[override_index] == dest_id:
                    override_last_use = last_use_override_positions[override_index] + base
                    if last_use is None or override_last_use > last_use:
                        last_use = override_last_use
                    break
                override_index += 1
            start = position + base
            if last_use is None or last_use < start:
                position += 1
                continue
            # A call result is defined by the call itself: the clobber it
            # represents happens before the value exists, so its barrier scan
            # starts one position later.  The interval still begins at the
            # call so no other value may hold the register across it.
            barrier_start = start + 1 if is_call_result else start
            live_start = start
            if function_level and first_live.get(dest_id, start) < start:
                # A block the value is live into is laid out before its
                # definition.  Such a value is live *before* the position its
                # interval would start at, which the scan's register donation
                # at an interval boundary does not model; keep it in its slot.
                position += 1
                continue
            if callee_saved:
                # Nothing survives an unclassified instruction.  A call (or a
                # use as a call operand, which ends at the call) only rules
                # out the caller-saved pool.
                if _interval_touches_call(barrier_start, last_use, hard_barriers):
                    position += 1
                    continue
                pool_intervals.append(
                    (
                        live_start,
                        last_use,
                        dest_id,
                        _interval_touches_call(
                            barrier_start, last_use, call_barriers
                        ),
                    )
                )
                if kind_id == PARSED_INSTRUCTION_KIND_CAST:
                    cast_record: CompilerInt4 = kernel.instruction_record(
                        metadata.second
                    )
                    if cast_record.third >= 0 and _is_noop_word_cast(
                        kernel, cast_record
                    ):
                        alias_pairs.append((dest_id, cast_record.third))
            elif function_level:
                if _interval_touches_call(barrier_start, last_use, global_barriers):
                    position += 1
                    continue
                function_intervals.append((live_start, last_use, dest_id))
            else:
                if _interval_touches_call(
                    position + 1 if is_call_result else position,
                    last_use,
                    call_positions,
                ):
                    position += 1
                    continue
                intervals.append((position, last_use, dest_id))
            position += 1

        if not function_level:
            _linear_scan_assign(kernel, intervals)

    if callee_saved:
        fixed_registers: dict[int, int] = {}
        _append_argument_intervals(
            func, kernel, phi_input_ids, global_last_use, hard_barriers,
            call_barriers, reload_offsets, pool_intervals, fixed_registers,
        )
        # All reload membership consumers are finished before alias grouping.
        reload_offsets.clear()
        group_intervals, group_members = _merge_alias_groups(
            pool_intervals, alias_pairs
        )
        # A group that must survive a call keeps x19-x28 for all members.
        fixed_roots: dict[int, int] = {}
        for _start, _end, root, needs in group_intervals:
            if needs:
                continue
            for member in group_members[root]:
                if member in fixed_registers:
                    fixed_roots[root] = fixed_registers[member]
        _linear_scan_assign_pools(kernel, group_intervals, fixed_roots)
        func.aarch64_callee_saved = _publish_group_registers(
            kernel, group_intervals, group_members
        )
    elif function_level:
        _linear_scan_assign(kernel, function_intervals)

    # Stack slots were assigned before this target combine was planned.  When
    # an extended operand did not win a register, retain the plan only if no
    # intervening definition reuses/overlaps that operand's spill slot.
    func.aarch64_madd_fusions = [
        fusion
        for fusion in func.aarch64_madd_fusions
        if _aarch64_madd_fusion_storage_is_safe(func, fusion)
    ]
    if callee_saved:
        # Decided last: a dropped madd plan above materialises its product.
        func.aarch64_frameless = _is_frameless_leaf(func, kernel)


def callee_saved_area_size(func: ParsedFunction) -> int:
    """Bytes below the slot frame that hold the saved callee-saved registers.

    The area sits at ``[sp, #0]`` upward once the prologue has lowered sp by
    ``frame_size`` plus this size.  Slot offsets are x29-relative and are not
    moved, so nothing that addresses a slot changes.  (Compact unwind keeps
    describing a plain frame; nothing in pcc restores registers by unwinding.)
    """
    count = len(func.aarch64_callee_saved)
    return (count * 8 + 15) // 16 * 16


def emit_callee_saved_stores(func: ParsedFunction) -> list[str]:
    lines: list[str] = []
    index = 0
    while index < len(func.aarch64_callee_saved):
        lines.append(
            emitted_memory_instruction_line(
                "str",
                "x" + str(func.aarch64_callee_saved[index]),
                "sp",
                func.platform_frame_extra + index * 8,
            )
        )
        index += 1
    return lines


def emit_callee_saved_loads(func: ParsedFunction) -> list[str]:
    lines: list[str] = []
    index = 0
    while index < len(func.aarch64_callee_saved):
        lines.append(
            emitted_memory_instruction_line(
                "ldr",
                "x" + str(func.aarch64_callee_saved[index]),
                "sp",
                func.platform_frame_extra + index * 8,
            )
        )
        index += 1
    return lines


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
    if register_index is None or register_index not in _ASSIGNABLE_REGISTERS:
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
    if register_index is None or register_index not in _ASSIGNABLE_REGISTERS:
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
    if register_index is None or register_index not in _ASSIGNABLE_REGISTERS:
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
    "call_result_registers_enabled",
    "allocated_scalar_register_indexed",
    "allocated_register_name",
    "commit_allocated_scalar_result",
    "commit_allocated_scalar_result_indexed",
]
