from __future__ import annotations

from . import BackendUnavailable
from .self_backend_aarch64_darwin_abi import reg_name
from .self_backend_aarch64_darwin_branch_protection import (
    branch_protection_enabled,
    epilogue_authenticate_and_return,
)
from .self_backend_aarch64_darwin_flow import emit_phi_assignments
from .self_backend_aarch64_darwin_materialize import materialize_value
from .self_backend_aarch64_darwin_materialize import materialize_scalar_value_indexed
from .self_backend_aarch64_darwin_mem import (
    emitted_branch_line,
    emitted_compare_register_line,
    emitted_fixed_instruction_line,
    emitted_frame_pair_line,
)
from .self_backend_aarch64_darwin_regalloc import (
    callee_saved_area_size,
    emit_callee_saved_loads,
)
from .self_backend_aarch64_darwin_ops import aarch64_cc, aarch64_inverse_cc
from .self_backend_aarch64_darwin_regs import (
    emit_add_offset,
    emit_const_to_reg,
    emit_const_to_reg_bits,
    emit_stack_adjust,
)
from .self_backend_aarch64_darwin_symbols import block_edge_label, block_label
from .self_backend_ir import I1, ParsedFunction, TypeDesc
from .self_backend_kernel import IndexedFunctionKernel
from .self_backend_module_symbols import PreparedModuleSymbols
from .self_backend_value_arena import CompilerInt2, CompilerInt4, CompilerIntArena


def _restore_fixed_frame_sp(func: ParsedFunction, total_frame: int) -> list[str]:
    """Undo run-time SP moves (dynamic allocas): SP = x29 - fixed frame."""
    if not func.aarch64_dynamic_stack:
        return []
    return emit_add_offset("sp", "x29", -total_frame, scratch_reg="x9")


def emit_epilogue(func: ParsedFunction) -> list[str]:
    if func.aarch64_frameless:
        return [emitted_fixed_instruction_line("ret")]
    total_frame = func.frame_size + callee_saved_area_size(func) + func.platform_frame_extra
    lines = _restore_fixed_frame_sp(func, total_frame)
    lines.extend(emit_callee_saved_loads(func))
    if total_frame:
        lines.extend(emit_stack_adjust(total_frame))
    lines.append(emitted_frame_pair_line(True))
    # Reverse-edge protection: authenticate the signed LR before returning. When
    # branch protection is on this emits ``autiasp`` + ``ret`` (SP is the same
    # modifier used by the prologue ``paciasp`` because the frame save/restore is
    # symmetric). With protection off it is a plain ``ret``.
    if branch_protection_enabled():
        lines.extend(epilogue_authenticate_and_return(func))
    else:
        lines.append(emitted_fixed_instruction_line("ret"))
    return lines


def emit_tail_epilogue(func: ParsedFunction, target: str) -> list[str]:
    """Restore the caller's frame/return address before a direct sibling jump."""
    if func.aarch64_frameless:
        return [emitted_branch_line("b", target)]
    total_frame = func.frame_size + callee_saved_area_size(func) + func.platform_frame_extra
    lines = _restore_fixed_frame_sp(func, total_frame)
    lines.extend(emit_callee_saved_loads(func))
    if total_frame:
        lines.extend(emit_stack_adjust(total_frame))
    lines.append(emitted_frame_pair_line(True))
    if branch_protection_enabled():
        lines.append(emitted_fixed_instruction_line("autiasp"))
    lines.append(emitted_branch_line("b", target))
    return lines


def emit_branch_terminator(
    func: ParsedFunction,
    *,
    source_block: str,
    target: str,
    module_symbols: PreparedModuleSymbols,
    falls_through: bool = False,
) -> list[str]:
    lines = emit_phi_assignments(
        func,
        source_block=source_block,
        target_block=target,
        module_symbols=module_symbols,
    )
    # ``falls_through``: the target is laid out immediately after this block.
    if not falls_through:
        lines.append(emitted_branch_line("b", block_label(func.name, target)))
    return lines


def emit_cond_branch_terminator(
    func: ParsedFunction,
    *,
    block_name: str,
    cond_name: str,
    true_target: str,
    false_target: str,
    module_symbols: PreparedModuleSymbols,
    value_id: int = -1,
) -> list[str]:
    false_prep = block_edge_label(func.name, block_name, false_target)
    lines = materialize_value(
        func,
        cond_name,
        I1,
        9,
        module_symbols,
        value_id=value_id,
    )
    lines.append(emitted_branch_line("cbz", false_prep, "w9"))
    lines.extend(
        emit_phi_assignments(
            func,
            source_block=block_name,
            target_block=true_target,
            module_symbols=module_symbols,
        )
    )
    lines.append(emitted_branch_line("b", block_label(func.name, true_target)))
    lines.append(f"{false_prep}:")
    lines.extend(
        emit_phi_assignments(
            func,
            source_block=block_name,
            target_block=false_target,
            module_symbols=module_symbols,
        )
    )
    lines.append(emitted_branch_line("b", block_label(func.name, false_target)))
    return lines


def emit_cond_branch_terminator_indexed(
    func: ParsedFunction,
    *,
    kernel: IndexedFunctionKernel,
    block_id: int,
    condition_ref: int,
    true_target_id: int,
    false_target_id: int,
    module_symbols: PreparedModuleSymbols,
    next_block_id: int = -1,
    direct_targets: bool = False,
) -> list[str]:
    """Branch on an i1, choosing the branch polarity from the block layout.

    ``next_block_id`` is the block laid out right after this one (-1 when
    unknown); an edge into it with no PHI moves falls through instead of
    branching.  ``direct_targets`` lets the conditional branch name a block
    label directly: the caller sets it only when the whole function is
    small enough for the +/-1 MiB conditional-branch range.  Without it
    the conditional branch keeps targeting the local edge label that
    immediately follows, and far targets stay on unconditional branches.
    """

    block_name = kernel.block_names[block_id]
    cond_name = kernel.terminator_value(condition_ref)
    true_target = kernel.block_names[true_target_id]
    false_target = kernel.block_names[false_target_id]
    condition_type_id = (
        kernel.value_type_id(condition_ref)
        if condition_ref >= 0
        else kernel.intern_type(I1)
    )
    true_label = block_label(func.name, true_target)
    false_label = block_label(func.name, false_target)
    false_prep = block_edge_label(func.name, block_name, false_target)
    fused_predicate = ""
    if condition_ref >= 0 and condition_ref in func.aarch64_fused_branch_values:
        # The block ends with the compare that defines the condition; it left
        # its result in the flags (see _fusable_branch_conditions).
        block_fact: CompilerInt4 = kernel.block_fact(block_id)
        metadata: CompilerInt4 = kernel.instruction_metadata_by_id(
            block_fact.first + block_fact.second - 1
        )
        icmp: CompilerInt4 = kernel.instruction_record(metadata.second)
        fused_predicate = kernel.call_texts[icmp.first]
        lines: list[str] = []
    else:
        lines = materialize_scalar_value_indexed(
            func,
            kernel,
            cond_name,
            condition_type_id,
            9,
            module_symbols,
            value_id=condition_ref,
        )
    # Direct instruction capture records each emitted instruction when its
    # helper is called and the native sink consumes those records in line
    # order, so every helper below is called in output order.  The shape is
    # chosen from whether a target has PHIs (without PHIs an edge moves
    # nothing), never by emitting an edge's moves ahead of the branch.
    true_phi_fact: CompilerInt2 = kernel.block_phi_fact(true_target_id)
    false_phi_fact: CompilerInt2 = kernel.block_phi_fact(false_target_id)
    true_has_phis = true_phi_fact.second > 0
    false_has_phis = false_phi_fact.second > 0

    if direct_targets and true_target_id == false_target_id:
        # Both edges enter one block with the same incoming values.
        lines.extend(
            emit_phi_assignments(
                func,
                source_block=block_name,
                target_block=true_target,
                module_symbols=module_symbols,
            )
        )
        if true_target_id != next_block_id:
            lines.append(emitted_branch_line("b", true_label))
        return lines
    if direct_targets and not true_has_phis and not false_has_phis:
        if true_target_id == next_block_id:
            lines.append(_cond_branch_line(fused_predicate, False, false_label))
        elif false_target_id == next_block_id:
            lines.append(_cond_branch_line(fused_predicate, True, true_label))
        else:
            lines.append(_cond_branch_line(fused_predicate, False, false_label))
            lines.append(emitted_branch_line("b", true_label))
        return lines
    if direct_targets and not false_has_phis:
        lines.append(_cond_branch_line(fused_predicate, False, false_label))
        lines.extend(
            emit_phi_assignments(
                func,
                source_block=block_name,
                target_block=true_target,
                module_symbols=module_symbols,
            )
        )
        if true_target_id != next_block_id:
            lines.append(emitted_branch_line("b", true_label))
        return lines
    if direct_targets and not true_has_phis:
        lines.append(_cond_branch_line(fused_predicate, True, true_label))
        lines.extend(
            emit_phi_assignments(
                func,
                source_block=block_name,
                target_block=false_target,
                module_symbols=module_symbols,
            )
        )
        if false_target_id != next_block_id:
            lines.append(emitted_branch_line("b", false_label))
        return lines
    if true_target_id == next_block_id and true_target_id != false_target_id:
        # Put the true edge last so its moves fall into the true block.  The
        # conditional branch targets the local edge label right after the
        # false edge, which is in range for a function of any size.
        true_prep = block_edge_label(func.name, block_name, true_target)
        lines.append(_cond_branch_line(fused_predicate, True, true_prep))
        lines.extend(
            emit_phi_assignments(
                func,
                source_block=block_name,
                target_block=false_target,
                module_symbols=module_symbols,
            )
        )
        lines.append(emitted_branch_line("b", false_label))
        lines.append(f"{true_prep}:")
        lines.extend(
            emit_phi_assignments(
                func,
                source_block=block_name,
                target_block=true_target,
                module_symbols=module_symbols,
            )
        )
        return lines
    lines.append(_cond_branch_line(fused_predicate, False, false_prep))
    lines.extend(
        emit_phi_assignments(
            func,
            source_block=block_name,
            target_block=true_target,
            module_symbols=module_symbols,
        )
    )
    lines.append(emitted_branch_line("b", true_label))
    lines.append(f"{false_prep}:")
    lines.extend(
        emit_phi_assignments(
            func,
            source_block=block_name,
            target_block=false_target,
            module_symbols=module_symbols,
        )
    )
    if false_target_id != next_block_id:
        lines.append(emitted_branch_line("b", false_label))
    return lines


def _cond_branch_line(fused_predicate: str, branch_when: bool, label: str) -> str:
    """Branch to ``label`` when the condition equals ``branch_when``.

    A fused compare left its predicate in the flags; otherwise the i1 was
    materialized in w9.
    """
    if fused_predicate:
        if branch_when:
            return emitted_branch_line("b." + aarch64_cc(fused_predicate), label)
        return emitted_branch_line(
            "b." + aarch64_inverse_cc(fused_predicate), label
        )
    if branch_when:
        return emitted_branch_line("cbnz", label, "w9")
    return emitted_branch_line("cbz", label, "w9")


def emit_inline_error_edge_indexed(
    func: ParsedFunction,
    *,
    kernel: IndexedFunctionKernel,
    edge_id: int,
    module_symbols: PreparedModuleSymbols,
    cold_lines: list[str] | None = None,
    defer_cold_stub: bool = False,
) -> list[str]:
    """Emit a true-is-error branch while normal execution falls through.

    An edge into a shared frame landing branches to a per-edge cold stub that
    stores the edge's payload index into the landing's slot and then jumps to
    the landing; the stub is appended to ``cold_lines`` so it lands after the
    function's blocks instead of splitting the hot path.
    """

    condition_ref = kernel.inline_error_edge_condition(edge_id)
    error_target_id = kernel.inline_error_edge_target(edge_id)
    condition_type_id = kernel.value_type_id(condition_ref)
    lines = materialize_scalar_value_indexed(
        func,
        kernel,
        kernel.value_name(condition_ref),
        condition_type_id,
        9,
        module_symbols,
        value_id=condition_ref,
    )
    target_label = block_label(func.name, kernel.block_names[error_target_id])
    payload = kernel.inline_error_edge_payload(edge_id)
    landing_slot = kernel.inline_error_landing_slot(error_target_id)
    if payload >= 0 and landing_slot >= 0:
        if cold_lines is None and not defer_cold_stub:
            raise BackendUnavailable(
                "inline error edge into a frame landing needs a cold stub sink"
            )
        stub_label = block_label(
            func.name,
            kernel.block_names[error_target_id] + ".edge." + str(edge_id),
        )
        lines.append(emitted_branch_line("cbnz", stub_label, "w9"))
        if not defer_cold_stub:
            cold_lines.extend(emit_inline_error_stub_indexed(func, kernel, edge_id))
        return lines
    # Other targets contain no PHIs (the verifier enforces this), so the
    # exceptional edge needs no target-specific move sequence.
    lines.append(emitted_branch_line("cbnz", target_label, "w9"))
    return lines


def emit_inline_error_stub_indexed(
    func: ParsedFunction,
    kernel: IndexedFunctionKernel,
    edge_id: int,
) -> list[str]:
    from .self_backend_aarch64_darwin_memory import _indexed_scalar_slot_access

    target_id = kernel.inline_error_edge_target(edge_id)
    payload = kernel.inline_error_edge_payload(edge_id)
    landing_slot = kernel.inline_error_landing_slot(target_id)
    if payload < 0 or landing_slot < 0:
        return []
    target_name = kernel.block_names[target_id]
    stub_label = block_label(func.name, target_name + ".edge." + str(edge_id))
    lines = [stub_label + ":"]
    lines.extend(emit_const_to_reg_bits(32, "w9", payload))
    lines.extend(_indexed_scalar_slot_access(
        kernel,
        kernel.alloca_type_id(landing_slot),
        kernel.alloca_offset(landing_slot),
        9,
        store=True,
    ))
    lines.append(emitted_branch_line("b", block_label(func.name, target_name)))
    return lines


def emit_switch_terminator(
    func: ParsedFunction,
    *,
    block_name: str,
    value_type: TypeDesc,
    value: str,
    default_target: str,
    cases: tuple[tuple[int, str], ...],
    module_symbols: PreparedModuleSymbols,
    value_id: int = -1,
) -> list[str]:
    value_reg = reg_name(value_type, 9)
    case_reg = reg_name(value_type, 10)
    lines = materialize_value(
        func,
        value,
        value_type,
        9,
        module_symbols,
        value_id=value_id,
    )
    edge_targets: list[str] = []
    for case_value, case_target in cases:
        lines.extend(emit_const_to_reg(value_type, case_reg, case_value))
        lines.append(emitted_compare_register_line(value_reg, case_reg))
        lines.append(
            emitted_branch_line(
                "b.eq",
                block_edge_label(func.name, block_name, case_target),
            )
        )
        if case_target not in edge_targets:
            edge_targets.append(case_target)
    lines.append(
        emitted_branch_line(
            "b",
            block_edge_label(func.name, block_name, default_target),
        )
    )
    if default_target not in edge_targets:
        edge_targets.append(default_target)
    for target in edge_targets:
        edge_label = block_edge_label(func.name, block_name, target)
        lines.append(f"{edge_label}:")
        lines.extend(
            emit_phi_assignments(
                func,
                source_block=block_name,
                target_block=target,
                module_symbols=module_symbols,
            )
        )
        lines.append(emitted_branch_line("b", block_label(func.name, target)))
    return lines


def emit_switch_terminator_indexed(
    func: ParsedFunction,
    *,
    kernel: IndexedFunctionKernel,
    block_id: int,
    value_type_id: int,
    value_ref: int,
    default_target_id: int,
    case_start: int,
    case_count: int,
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    value_type = kernel.types[value_type_id]
    value_reg = reg_name(value_type, 9)
    case_reg = reg_name(value_type, 10)
    value_name = kernel.terminator_value(value_ref)
    block_name = kernel.block_names[block_id]
    lines = materialize_scalar_value_indexed(
        func,
        kernel,
        value_name,
        value_type_id,
        9,
        module_symbols,
        value_id=value_ref,
    )
    edge_target_ids = CompilerIntArena()

    def append_unique(target_id: int) -> None:
        index = 0
        while index < len(edge_target_ids):
            if edge_target_ids.get_unchecked(index) == target_id:
                return
            index += 1
        edge_target_ids.append(target_id)

    case_index = 0
    while case_index < case_count:
        case: CompilerInt2 = kernel.terminator_case(case_start + case_index)
        case_target = kernel.block_names[case.second]
        lines.extend(emit_const_to_reg(value_type, case_reg, case.first))
        lines.append(emitted_compare_register_line(value_reg, case_reg))
        lines.append(
            emitted_branch_line(
                "b.eq",
                block_edge_label(func.name, block_name, case_target),
            )
        )
        append_unique(case.second)
        case_index += 1
    default_target = kernel.block_names[default_target_id]
    lines.append(
        emitted_branch_line(
            "b",
            block_edge_label(func.name, block_name, default_target),
        )
    )
    append_unique(default_target_id)
    edge_index = 0
    while edge_index < len(edge_target_ids):
        target_id = edge_target_ids.get_unchecked(edge_index)
        target = kernel.block_names[target_id]
        lines.append(f"{block_edge_label(func.name, block_name, target)}:")
        lines.extend(
            emit_phi_assignments(
                func,
                source_block=block_name,
                target_block=target,
                module_symbols=module_symbols,
            )
        )
        lines.append(emitted_branch_line("b", block_label(func.name, target)))
        edge_index += 1
    edge_target_ids.close()
    return lines


def emit_unreachable_terminator() -> list[str]:
    return ["  brk #0"]
