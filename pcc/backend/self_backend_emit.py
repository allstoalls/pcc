from __future__ import annotations

from typing import Any, Callable

from . import BackendUnavailable
from .self_backend_analysis import _stable_text_bucket_key
from .self_backend_ir import (
    PARSED_INSTRUCTION_KIND_ALLOCA,
    PARSED_INSTRUCTION_KIND_BINOP,
    PARSED_INSTRUCTION_KIND_CALL,
    PARSED_INSTRUCTION_KIND_CAST,
    PARSED_INSTRUCTION_KIND_GEP,
    PARSED_INSTRUCTION_KIND_ICMP,
    PARSED_INSTRUCTION_KIND_LOAD,
    PARSED_INSTRUCTION_KIND_SELECT,
    PARSED_INSTRUCTION_KIND_STORE,
    ParsedBlock,
    ParsedFunction,
    ParsedInstr,
    text_key_names_equal,
)
from .self_backend_kernel import IndexedFunctionKernel, get_indexed_function_kernel
from .self_backend_precise_stackmaps import (
    FunctionStackMapPlan,
    PackedPlannedSafepoints,
)
from .self_backend_value_arena import CompilerInt2, CompilerInt4


_INDEXED_FIXED_PAYLOAD_KIND_IDS = (
    PARSED_INSTRUCTION_KIND_CALL,
    PARSED_INSTRUCTION_KIND_ALLOCA,
    PARSED_INSTRUCTION_KIND_LOAD,
    PARSED_INSTRUCTION_KIND_STORE,
    PARSED_INSTRUCTION_KIND_CAST,
    PARSED_INSTRUCTION_KIND_ICMP,
    PARSED_INSTRUCTION_KIND_BINOP,
    PARSED_INSTRUCTION_KIND_SELECT,
    PARSED_INSTRUCTION_KIND_GEP,
)


_CALL_INSTRUCTION_PREFIXES = ("bl ", "blr ", "call ")


def _record_call_safepoint_at_return(
    lines: list[str], instruction_start: int, suffix_start: int,
) -> None:
    """Put a call safepoint's labels right after the call instruction.

    The suffix (labels, then reloads) follows the whole lowered call, after
    its result moves; a stack walk finds the record by the return address the
    callee's frame record holds, so the labels move to just after the call.
    The reloads, and the result moves before them, keep their order.
    """
    if suffix_start >= len(lines):
        return
    call_line = suffix_start - 1
    while call_line >= instruction_start:
        if lines[call_line].lstrip().startswith(_CALL_INSTRUCTION_PREFIXES):
            break
        call_line -= 1
    if call_line < instruction_start or call_line == suffix_start - 1:
        return
    suffix = lines[suffix_start:]
    labels = [line for line in suffix if line.endswith(":")]
    if not labels:
        return
    reloads = [line for line in suffix if not line.endswith(":")]
    moves = lines[call_line + 1:suffix_start]
    del lines[call_line + 1:]
    lines.extend(labels)
    lines.extend(moves)
    lines.extend(reloads)


def _block_line_index_get(index: dict, block_name: str):
    for existing_name, value in index.get(
        _stable_text_bucket_key(block_name), []
    ):
        if text_key_names_equal(existing_name, block_name):
            return value
    return None


def emit_function_blocks(
    func: ParsedFunction,
    *,
    block_label: Callable[[str, str], str],
    emit_instruction: Callable[[ParsedFunction, ParsedBlock, ParsedInstr], list[str]],
    emit_terminator: Callable[[ParsedFunction, ParsedBlock, ParsedInstr], list[str]],
    blocks: list[ParsedBlock] | None = None,
    emit_block_prefix: Callable[[ParsedFunction, ParsedBlock], list[str]] | None = None,
    emit_instruction_suffix: Callable[
        [ParsedFunction, ParsedBlock, int, ParsedInstr], list[str]
    ] | None = None,
    emit_terminator_prefix: Callable[
        [ParsedFunction, ParsedBlock, ParsedInstr], list[str]
    ] | None = None,
    stack_map_plan: Any = None,
    indexed_kernel: IndexedFunctionKernel | None = None,
    emit_indexed_instruction: Callable[
        [
            ParsedFunction,
            ParsedBlock,
            Any,
            int,
            int,
            int,
            int,
            int | tuple,
            bool,
        ],
        list[str],
    ] | None = None,
    emit_indexed_terminator: Callable[
        [ParsedFunction, ParsedBlock, ParsedInstr, int], list[str]
    ] | None = None,
    emit_indexed_error_edge=None,
) -> list[str]:
    lines: list[str] = []
    cold_lines: list[str] = []
    entry_index: dict = {}
    suffix_index: dict = {}
    term_index: dict = {}
    if stack_map_plan is not None:
        # Per-call plan lookups scan every label; indexing once keeps huge
        # generated module tops (72k blocks) linear instead of quadratic.
        entry_index, suffix_index, term_index = stack_map_plan.build_line_index()
    if blocks is None and not func.blocks:
        if indexed_kernel is None:
            indexed_kernel = get_indexed_function_kernel(func)
        emitted_blocks = indexed_kernel.materialize_legacy_blocks(func)
    else:
        emitted_blocks = func.blocks if blocks is None else blocks
    for index, block in enumerate(emitted_blocks):
        if index == 0:
            lines.append(block_label(func.name, block.name) + ":")
        else:
            lines.append("")
            lines.append(block_label(func.name, block.name) + ":")
        if emit_block_prefix is not None:
            lines.extend(emit_block_prefix(func, block))
        elif stack_map_plan is not None:
            block_entry_lines = _block_line_index_get(
                entry_index, block.name
            )
            if block_entry_lines is not None:
                lines.extend(block_entry_lines)
        block_suffix_index = (
            _block_line_index_get(suffix_index, block.name)
            if stack_map_plan is not None
            else None
        )
        block_id = (
            indexed_kernel.block_id(block.name)
            if indexed_kernel is not None else -1
        )
        block_instruction_start = -1
        terminator_use_count = 0
        terminator_use_id = -1
        instruction_index = 0
        # Inline error edges live only in the kernel: a block that has them
        # carries no compare-and-branch for those checks, so a target that
        # cannot emit them must refuse instead of dropping the checks.
        error_edge_first = 0
        error_edge_count = 0
        error_edge_offset = 0
        if block_id >= 0:
            error_span: CompilerInt2 = indexed_kernel.inline_error_edge_span(block_id)
            error_edge_first = error_span.first
            error_edge_count = error_span.second
            if error_edge_count and emit_indexed_error_edge is None:
                raise BackendUnavailable(
                    "target does not support inline error edges"
                )
        if block_id >= 0 and emit_indexed_instruction is not None:
            indexed_block: CompilerInt4 = indexed_kernel.block_fact(block_id)
            block_instruction_start = indexed_block.first
            instruction_count = indexed_block.second
            terminator_use_count = indexed_block.third
            terminator_use_id = indexed_block.fourth
        else:
            instruction_count = len(block.instructions)
        while instruction_index < instruction_count:
            instr = None
            instruction_start = len(lines)
            if block_id >= 0 and emit_indexed_instruction is not None:
                kind_id = indexed_kernel.instruction_kind_id(
                    block_id, instruction_index
                )
                if kind_id in _INDEXED_FIXED_PAYLOAD_KIND_IDS:
                    instruction_data = indexed_kernel.instruction_record_id(
                        block_id,
                        instruction_index,
                    )
                else:
                    instruction_data = indexed_kernel.instruction_data(
                        block_id,
                        instruction_index,
                    )
                lines.extend(emit_indexed_instruction(
                    func,
                    block,
                    indexed_kernel,
                    block_id,
                    instruction_index,
                    block_instruction_start + instruction_index,
                    kind_id,
                    instruction_data,
                    indexed_kernel.instruction_is_volatile(
                        block_id, instruction_index
                    ),
                ))
            else:
                instr = block.instructions[instruction_index]
                lines.extend(emit_instruction(func, block, instr))
            suffix_start = len(lines)
            if emit_instruction_suffix is not None:
                if instr is None:
                    instr = indexed_kernel.diagnostic_instruction(
                        block_id, instruction_index
                    )
                lines.extend(
                    emit_instruction_suffix(
                        func, block, instruction_index, instr
                    )
                )
            elif (
                block_suffix_index is not None
                and instruction_index in block_suffix_index
            ):
                lines.extend(block_suffix_index[instruction_index])
            _record_call_safepoint_at_return(lines, instruction_start, suffix_start)
            while error_edge_offset < error_edge_count:
                edge_id = error_edge_first + error_edge_offset
                trigger = indexed_kernel.inline_error_edge_trigger(edge_id)
                if trigger > instruction_index:
                    break
                if trigger < instruction_index:
                    raise BackendUnavailable(
                        "inline error edges are not trigger-ordered"
                    )
                lines.extend(
                    emit_indexed_error_edge(
                        func,
                        indexed_kernel,
                        edge_id,
                        cold_lines,
                    )
                )
                error_edge_offset += 1
            instruction_index += 1
        if error_edge_offset != error_edge_count:
            raise BackendUnavailable(
                "inline error edge trigger exceeds source block"
            )
        term = block.terminator
        if emit_terminator_prefix is not None:
            if term is None and indexed_kernel is None:
                indexed_kernel = get_indexed_function_kernel(func)
                block_id = indexed_kernel.block_id(block.name)
            if term is None and indexed_kernel is not None and block_id >= 0:
                term = indexed_kernel.diagnostic_terminator(block_id)
            assert term is not None
            lines.extend(emit_terminator_prefix(func, block, term))
        elif stack_map_plan is not None:
            block_term_lines = _block_line_index_get(term_index, block.name)
            if block_term_lines is not None:
                lines.extend(block_term_lines)
        if block_id >= 0 and emit_indexed_terminator is not None:
            term_use_id = -1
            if terminator_use_count:
                term_use_id = terminator_use_id
            lines.extend(
                emit_indexed_terminator(
                    func,
                    block,
                    term,
                    term_use_id,
                )
            )
        else:
            if term is None and indexed_kernel is None:
                indexed_kernel = get_indexed_function_kernel(func)
                block_id = indexed_kernel.block_id(block.name)
            if term is None and indexed_kernel is not None and block_id >= 0:
                # The x86/legacy target keeps an explicit lazy object bridge;
                # AArch64's supported normal path emits indexed terminators.
                term = indexed_kernel.diagnostic_terminator(block_id)
            assert term is not None
            lines.extend(emit_terminator(func, block, term))
    lines.extend(cold_lines)
    return lines


def emit_indexed_function_blocks(
    func: ParsedFunction,
    *,
    indexed_kernel: IndexedFunctionKernel,
    block_label: Callable[[str, str], str],
    emit_indexed_instruction,
    emit_indexed_terminator,
    emit_indexed_error_edge=None,
    stack_map_plan: FunctionStackMapPlan | None = None,
) -> list[str]:
    """Emit a fully indexed function without ParsedBlock/Instr projection."""

    lines: list[str] = []
    cold_lines: list[str] = []
    entry_index: dict = {}
    suffix_index: dict = {}
    term_index: dict = {}
    packed_plan: FunctionStackMapPlan | None = None
    packed_records: PackedPlannedSafepoints | None = None
    if stack_map_plan is not None and stack_map_plan.packed_records is not None:
        packed_plan = stack_map_plan
        packed_records = stack_map_plan.packed_records
    packed_stack_map = packed_records is not None
    if stack_map_plan is not None and not packed_stack_map:
        entry_index, suffix_index, term_index = stack_map_plan.build_line_index()
    layout_count = len(indexed_kernel.block_layout_ids)
    block_position = 0
    block_count = (
        layout_count if layout_count else len(indexed_kernel.block_names)
    )
    while block_position < block_count:
        block_id = (
            indexed_kernel.block_layout_ids.get_unchecked(block_position)
            if layout_count
            else block_position
        )
        block_name = indexed_kernel.block_names[block_id]
        if block_position == 0:
            lines.append(block_label(func.name, block_name) + ":")
        else:
            lines.append("")
            lines.append(block_label(func.name, block_name) + ":")
        if stack_map_plan is not None:
            if packed_stack_map:
                stack_map_plan.append_packed_entry_lines(lines, block_id)
            else:
                block_entry_lines = _block_line_index_get(entry_index, block_name)
                if block_entry_lines is not None:
                    lines.extend(block_entry_lines)
        block_suffix_index = (
            _block_line_index_get(suffix_index, block_name)
            if stack_map_plan is not None and not packed_stack_map
            else None
        )
        indexed_block: CompilerInt4 = indexed_kernel.block_fact(block_id)
        error_span: CompilerInt2 = indexed_kernel.inline_error_edge_span(block_id)
        error_edge_offset = 0
        suffix_route_index = 0
        suffix_route_end = 0
        if packed_records is not None:
            span_offset = block_id * 2
            suffix_route_start = packed_records.suffix_route_spans.get_unchecked(
                span_offset
            )
            suffix_route_count = packed_records.suffix_route_spans.get_unchecked(
                span_offset + 1
            )
            if suffix_route_start >= 0:
                suffix_route_index = suffix_route_start
                suffix_route_end = suffix_route_start + suffix_route_count
        instruction_index = 0
        while instruction_index < indexed_block.second:
            instruction_id = indexed_block.first + instruction_index
            metadata: CompilerInt4 = indexed_kernel.instruction_metadata_by_id(
                instruction_id
            )
            kind_id = metadata.first
            if kind_id in _INDEXED_FIXED_PAYLOAD_KIND_IDS:
                instruction_data = metadata.second
            else:
                instruction_data = indexed_kernel.instruction_data(
                    block_id,
                    instruction_index,
                )
            instruction_start = len(lines)
            lines.extend(
                emit_indexed_instruction(
                    func,
                    indexed_kernel,
                    block_id,
                    instruction_index,
                    instruction_id,
                    kind_id,
                    instruction_data,
                    bool(metadata.third),
                )
            )
            suffix_start = len(lines)
            if (
                block_suffix_index is not None
                and instruction_index in block_suffix_index
            ):
                lines.extend(block_suffix_index[instruction_index])
            elif packed_records is not None and packed_plan is not None:
                while suffix_route_index < suffix_route_end:
                    route_scalar = suffix_route_index * 3
                    route_instruction = packed_records.suffix_routes.get_unchecked(
                        route_scalar + 1
                    )
                    if route_instruction > instruction_index:
                        break
                    if route_instruction == instruction_index:
                        record_index = packed_records.suffix_routes.get_unchecked(
                            route_scalar + 2
                        )
                        packed_plan.append_packed_record_lines(
                            lines,
                            record_index,
                        )
                    suffix_route_index += 1
            _record_call_safepoint_at_return(lines, instruction_start, suffix_start)
            while error_edge_offset < error_span.second:
                edge_id = error_span.first + error_edge_offset
                trigger = indexed_kernel.inline_error_edge_trigger(edge_id)
                if trigger > instruction_index:
                    break
                if trigger < instruction_index:
                    raise BackendUnavailable(
                        "inline error edges are not trigger-ordered"
                    )
                if emit_indexed_error_edge is None:
                    raise BackendUnavailable(
                        "target does not support inline error edges"
                    )
                lines.extend(
                    emit_indexed_error_edge(
                        func,
                        indexed_kernel,
                        edge_id,
                        cold_lines,
                    )
                )
                error_edge_offset += 1
            instruction_index += 1
        if error_edge_offset != error_span.second:
            raise BackendUnavailable(
                "inline error edge trigger exceeds source block"
            )
        if stack_map_plan is not None:
            if packed_stack_map:
                stack_map_plan.append_packed_terminator_lines(lines, block_id)
            else:
                block_term_lines = _block_line_index_get(term_index, block_name)
                if block_term_lines is not None:
                    lines.extend(block_term_lines)
        term_use_id = -1
        if indexed_block.third:
            term_use_id = indexed_block.fourth
        lines.extend(
            emit_indexed_terminator(
                func,
                indexed_kernel,
                block_id,
                term_use_id,
            )
        )
        block_position += 1
    lines.extend(cold_lines)
    return lines
