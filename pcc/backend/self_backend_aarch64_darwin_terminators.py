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
from .self_backend_aarch64_darwin_regs import (
    emit_const_to_reg,
    emit_const_to_reg_bits,
    emit_stack_adjust,
)
from .self_backend_aarch64_darwin_symbols import block_edge_label, block_label
from .self_backend_ir import I1, ParsedFunction, TypeDesc
from .self_backend_kernel import IndexedFunctionKernel
from .self_backend_module_symbols import PreparedModuleSymbols
from .self_backend_value_arena import CompilerInt2, CompilerInt4, CompilerIntArena


def emit_epilogue(func: ParsedFunction) -> list[str]:
    lines: list[str] = []
    if func.frame_size:
        lines.extend(emit_stack_adjust(func.frame_size))
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
    lines: list[str] = []
    if func.frame_size:
        lines.extend(emit_stack_adjust(func.frame_size))
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
) -> list[str]:
    lines = emit_phi_assignments(
        func,
        source_block=source_block,
        target_block=target,
        module_symbols=module_symbols,
    )
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
) -> list[str]:
    block_name = kernel.block_names[block_id]
    cond_name = kernel.terminator_value(condition_ref)
    true_target = kernel.block_names[true_target_id]
    false_target = kernel.block_names[false_target_id]
    condition_type_id = (
        kernel.value_type_id(condition_ref)
        if condition_ref >= 0
        else kernel.intern_type(I1)
    )
    false_prep = block_edge_label(func.name, block_name, false_target)
    lines = materialize_scalar_value_indexed(
        func,
        kernel,
        cond_name,
        condition_type_id,
        9,
        module_symbols,
        value_id=condition_ref,
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
