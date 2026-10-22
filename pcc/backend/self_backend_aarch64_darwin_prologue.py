from __future__ import annotations

from . import BackendUnavailable
from .self_backend_target_match import is_aarch64_linux_triple
from .self_backend_aarch64_darwin_abi import (
    aggregate_passed_indirect,
    assign_abi_arg_regs,
    reg_name_indexed,
    stack_arg_offsets,
)
from .self_backend_aarch64_darwin_branch_protection import (
    branch_protection_enabled,
    prologue_sign_return_address,
)
from .self_backend_aarch64_darwin_calls import emit_fixed_stack_arg_load
from .self_backend_aarch64_darwin_mem import (
    emitted_fixed_instruction_line,
    emitted_frame_pair_line,
    emitted_move_register_line,
)
from .self_backend_aarch64_darwin_regalloc import (
    allocate_aarch64_block_registers,
    callee_saved_area_size,
    commit_allocated_scalar_result_indexed,
    emit_callee_saved_stores,
)
from .self_backend_aarch64_darwin_regs import emit_stack_adjust
from .self_backend_aarch64_darwin_slots import (
    copy_address_to_value_slot,
    store_reg_to_slot,
    store_reg_to_slot_parts,
    store_scalar_reg_to_value_slot_indexed,
    store_value_regs_to_value_slot,
)
from .self_backend_aarch64_darwin_symbols import asm_symbol
from .self_backend_dynamic_alloca import DYNAMIC_ALLOCA_INTRINSIC
from .self_backend_ir import (
    ParsedFunction,
    SlotInfo,
    parsed_function_has_value_slot,
)
from .self_backend_module_symbols import PreparedModuleSymbols
from .self_backend_kernel import (
    IndexedFunctionKernel,
    _scalar_window_is_complete,
    TYPE_KIND_FP,
    TYPE_KIND_INT,
    TYPE_KIND_PTR,
    get_indexed_function_kernel,
)


def function_uses_dynamic_alloca(func: ParsedFunction) -> bool:
    kernel = get_indexed_function_kernel(func)
    call_count = len(kernel.call_scalars) // 8
    call_id = 0
    while call_id < call_count:
        header = kernel.call_header(call_id)
        if not (header.third & 1) and kernel.call_texts[header.second] == DYNAMIC_ALLOCA_INTRINSIC:
            return True
        call_id += 1
    return False


def emit_function_prologue(
    func: ParsedFunction,
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    # Target-specific preparation belongs immediately before target emission.
    # The allocator is conservative and leaves every preassigned stack slot in
    # place, so an unsupported shape remains byte-for-byte on the spill path.
    allocate_aarch64_block_registers(func)
    # Source-debug builds keep an actual frame chain, including leaf calls.
    # Until full unwind programs cover every prologue/epilogue PC, omitting
    # a leaf frame makes the debugger's frame-pointer walk skip its caller.
    if (func.type_context is not None and func.type_context.debug_info is not None
            and func.name in func.type_context.debug_info.functions):
        func.aarch64_frameless = False
    symbol = asm_symbol(func.name, module_symbols)
    lines = ["", ".p2align 2"]
    if func.is_global:
        lines.append(f".globl {symbol}")
    lines.append(f"{symbol}:")
    linux = is_aarch64_linux_triple(module_symbols.target_triple)
    func.platform_frame_extra = 192 if linux and func.is_vararg else 0
    if linux and func.name == "_start":
        if len(func.args) != 1 or not func.args[0].type.is_ptr or not func.ret_type.is_void:
            raise ValueError("Linux _start requires void (ptr initial_stack)")
        lines.append("  mov x0, sp")
    if func.platform_frame_extra:
        func.aarch64_frameless = False
    func.aarch64_dynamic_stack = function_uses_dynamic_alloca(func)
    if func.aarch64_dynamic_stack:
        func.aarch64_frameless = False
        if func.platform_frame_extra:
            # The va_list register save area is addressed from SP.
            raise BackendUnavailable(
                f"self backend cannot combine a dynamic alloca with the Linux "
                f"variadic register save area in {func.name!r}"
            )
    # AArch64 branch protection (pac-ret + BTI). ``paciasp`` signs LR (x30) with
    # SP as the modifier *before* the frame save stores it, and doubles as a BTI
    # ``c`` landing pad for ``bl``/``blr`` callers. The matching ``autiasp`` is
    # emitted in the epilogue after LR is reloaded. See
    # ``self_backend_aarch64_darwin_branch_protection`` for the S-track rationale
    # (self backend must not depend on the LLVM path for CFI hardening).
    if func.aarch64_frameless:
        # No frame record and no stack: LR never leaves x30, so there is
        # nothing to sign; ``bti c`` keeps the indirect-call landing pad that
        # ``paciasp`` otherwise provides.
        if branch_protection_enabled():
            lines.append(emitted_fixed_instruction_line("bti c"))
    else:
        if branch_protection_enabled():
            lines.extend(prologue_sign_return_address(func))
        lines.extend(
            [
                emitted_frame_pair_line(False),
                emitted_move_register_line("x29", "sp"),
            ]
        )
        total_frame = func.frame_size + callee_saved_area_size(func) + func.platform_frame_extra
        if total_frame:
            lines.extend(emit_stack_adjust(-total_frame))
        # Save before any argument is committed into a callee-saved register.
        lines.extend(emit_callee_saved_stores(func))
    if func.platform_frame_extra:
        # Preserve incoming variadic registers before normal argument lowering.
        for index in range(8):
            lines.append(f"  str x{index}, [sp, #{index * 8}]")
            lines.append(f"  str q{index}, [sp, #{64 + index * 16}]")
    kernel = get_indexed_function_kernel(func)
    if func.indexed_slot_projection and kernel.hidden_sret_slot_id >= 0:
        hidden_slot_id = kernel.hidden_sret_slot_id
        lines.extend(
            store_reg_to_slot_parts(
                "x8",
                kernel.slot_offset(hidden_slot_id),
                kernel.type_desc(kernel.slot_type_id(hidden_slot_id)),
            )
        )
    elif func.hidden_sret_slot is not None:
        lines.extend(store_reg_to_slot("x8", func.hidden_sret_slot))

    arg_types = [arg.type for arg in func.args]
    arg_regs = assign_abi_arg_regs(arg_types, linux=linux)
    stack_offsets = stack_arg_offsets(arg_types, arg_regs, linux=linux)
    for arg, regs, stack_offset in zip(func.args, arg_regs, stack_offsets):
        if not parsed_function_has_value_slot(func, arg.name):
            continue
        if not regs:
            assert stack_offset is not None
            lines.extend(emit_fixed_stack_arg_load(func, arg, stack_offset))
            continue
        if aggregate_passed_indirect(arg.type):
            lines.extend(copy_address_to_value_slot(regs[0], func, arg.name))
            continue
        if func.indexed_slot_projection:
            arg_value_id = kernel.value_id(arg.name)
            arg_type_id = kernel.value_type_id(arg_value_id)
            arg_header_is_scalar = False
            if type(kernel) is IndexedFunctionKernel:
                arg_header_scalar_id = arg_type_id
                arg_header_is_scalar = _scalar_window_is_complete(
                    kernel.type_scalars, arg_header_scalar_id, 12, 0
                )
                if arg_header_is_scalar:
                    arg_header_scalar = kernel.type_scalars.get_unchecked(
                        arg_header_scalar_id * 12 + 0
                    )
                else:
                    arg_header = kernel.type_header(arg_header_scalar_id)
            else:
                arg_header = kernel.type_header(arg_type_id)
            if (arg_header_scalar if arg_header_is_scalar else arg_header.first) in (
                TYPE_KIND_INT,
                TYPE_KIND_FP,
                TYPE_KIND_PTR,
            ):
                allocated_lines = commit_allocated_scalar_result_indexed(
                    func,
                    arg_value_id,
                    arg_type_id,
                    reg_name_indexed(kernel, arg_type_id, int(regs[0][1:])),
                )
                if allocated_lines is not None:
                    lines.extend(allocated_lines)
                    continue
                lines.extend(
                    store_scalar_reg_to_value_slot_indexed(
                        func,
                        arg_value_id,
                        arg_type_id,
                        int(regs[0][1:]),
                    )
                )
                continue
        lines.extend(
            store_value_regs_to_value_slot(func, arg.name, int(regs[0][1:]))
        )
    return lines
