"""Finite scalar sibling-call planning over indexed IR and precise root plans."""

from __future__ import annotations

from .self_backend_call_flags import CALL_FLAG_FRAME_PROTOCOL, CALL_FLAG_LLVM_INTRINSIC
from .self_backend_ir import (
    PARSED_INSTRUCTION_KIND_ALLOCA,
    PARSED_INSTRUCTION_KIND_CALL,
    PARSED_INSTRUCTION_KIND_RET,
    PARSED_INSTRUCTION_KIND_RET_VOID,
    ParsedFunction,
)
from .self_backend_kernel import TYPE_KIND_INT, TYPE_KIND_PTR, TYPE_KIND_VOID, get_indexed_function_kernel
from .self_backend_precise_stackmaps import FunctionStackMapPlan
from .self_backend_value_arena import CompilerInt4


def _scalar_register_type(header: CompilerInt4) -> bool:
    return header.first == TYPE_KIND_PTR or (
        header.first == TYPE_KIND_INT and header.second in (32, 64)
    )


def plan_aarch64_tail_calls(func: ParsedFunction, plan: FunctionStackMapPlan, *, enabled: bool) -> None:
    func.aarch64_tail_call_ids = []
    if not enabled or func.is_vararg or func.hidden_sret_slot is not None:
        return
    if any(arg.type.is_array or arg.type.is_struct for arg in func.args):
        return
    packed = plan.packed_records
    if packed is not None:
        for index in range(len(packed)):
            span: CompilerInt4 = packed.span(index)
            if span.second or span.fourth or packed.exceptional_block(index):
                return
    else:
        for record in plan.records:
            if record.locations or record.reloads or record.exceptional_block:
                return
    kernel = get_indexed_function_kernel(func)
    for block_id in range(len(kernel.block_names)):
        for index in range(kernel.instruction_count(block_id)):
            kind = kernel.instruction_kind_id(block_id, index)
            if kind == PARSED_INSTRUCTION_KIND_ALLOCA:
                return
            if kind == PARSED_INSTRUCTION_KIND_CALL:
                call_id = kernel.instruction_call_id(block_id, index)
                if kernel.call_flags(call_id) & (CALL_FLAG_FRAME_PROTOCOL | CALL_FLAG_LLVM_INTRINSIC):
                    return
    for block_id in range(len(kernel.block_names)):
        count = kernel.instruction_count(block_id)
        term: CompilerInt4 = kernel.terminator_header(block_id)
        if count == 0 or term.first not in (PARSED_INSTRUCTION_KIND_RET, PARSED_INSTRUCTION_KIND_RET_VOID):
            continue
        if kernel.instruction_kind_id(block_id, count - 1) != PARSED_INSTRUCTION_KIND_CALL:
            continue
        call_id = kernel.instruction_call_id(block_id, count - 1)
        header: CompilerInt4 = kernel.call_header(call_id)
        span: CompilerInt4 = kernel.call_span(call_id)
        # Exception polls, continuation/loop safepoints and frame protocols
        # have distinct metadata contracts; only ordinary direct calls enter
        # this first sibling-call tier.
        if header.third or span.first > 8:
            continue
        result: CompilerInt4 = kernel.type_header(header.first)
        if result.first != TYPE_KIND_VOID and not _scalar_register_type(result):
            continue
        if term.first == PARSED_INSTRUCTION_KIND_RET:
            if term.third != span.third or term.second != header.first:
                continue
        elif result.first != TYPE_KIND_VOID:
            continue
        supported = True
        for arg_index in range(span.first):
            arg: CompilerInt4 = kernel.call_arg(header.fourth + arg_index)
            if not _scalar_register_type(kernel.type_header(arg.first)):
                supported = False
                break
        if supported:
            func.aarch64_tail_call_ids.append(call_id)


def aarch64_tail_call_id_for_block(func: ParsedFunction, block_id: int) -> int:
    kernel = get_indexed_function_kernel(func)
    count = kernel.instruction_count(block_id)
    if not count or kernel.instruction_kind_id(block_id, count - 1) != PARSED_INSTRUCTION_KIND_CALL:
        return -1
    call_id = kernel.instruction_call_id(block_id, count - 1)
    return call_id if call_id in func.aarch64_tail_call_ids else -1
