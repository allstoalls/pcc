"""Frozen fc32a1f facts oracle; never imports PCC in pure host controls."""

from __future__ import annotations

from types import FunctionType


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
            metadata: CompilerInt4 = kernel.instruction_metadata_by_id(
                block_fact.first + instruction_index
            )
            if (
                not 0 <= metadata.first < len(PARSED_INSTRUCTION_KINDS)
                or metadata.first in _POOL_REJECTED_INSTRUCTION_KIND_IDS
            ):
                safe = False
            instruction_index += 1
        block_safe.append(safe)
        instruction_index = 0
        while instruction_index < block_fact.second:
            metadata = kernel.instruction_metadata_by_id(
                block_fact.first + instruction_index
            )
            if not safe or metadata.first == PARSED_INSTRUCTION_KIND_CALL:
                barriers.append(position)
                if not safe:
                    hard_barriers.append(position)
                else:
                    call_positions.append(position)
            instruction_fact: CompilerInt4 = kernel.instruction_fact_by_id(
                block_fact.first + instruction_index
            )
            destination = instruction_fact.first
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
    first_live: dict[int, int] = {}
    visited: list[int] = [-1] * len(successors)
    pending: list[int] = []
    for value_id in use_blocks:
        home = def_block.get(value_id, -1)
        lowest = -1
        highest = last_use[value_id]
        for block_id in use_blocks[value_id]:
            if block_id != home and visited[block_id] != value_id:
                visited[block_id] = value_id
                pending.append(block_id)
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
        last_use[value_id] = highest
        if lowest >= 0:
            first_live[value_id] = lowest
    return (
        block_base,
        last_use,
        barriers,
        block_safe,
        call_positions,
        hard_barriers,
        first_live,
    )



def old_function_level_facts(regalloc):
    return FunctionType(
        _function_level_facts.__code__, dict(vars(regalloc)),
        name="_function_level_facts",
    )
