from __future__ import annotations

from . import BackendUnavailable
from .self_backend_aarch64_darwin_abi import (
    aggregate_fits_reg_abi,
    reg_name,
    reg_name_indexed,
)
from .self_backend_aarch64_darwin_materialize import (
    materialize_aggregate_storage_address,
    materialize_value,
    materialize_scalar_value_indexed,
    store_large_aggregate_literal_to_address,
)
from .self_backend_aarch64_darwin_mem import (
    emitted_memory_instruction_line,
    emitted_move_register_line,
)
from .self_backend_aarch64_darwin_regalloc import (
    allocated_scalar_register_indexed,
)
from .self_backend_aarch64_darwin_regs import emit_add_offset, emit_stack_adjust
from .self_backend_aarch64_darwin_slots import (
    copy_address_to_address,
    copy_address_to_slot,
    copy_address_to_value_slot,
    load_value_from_address,
    store_reg_to_slot,
    store_reg_to_value_slot,
    store_value_regs_to_slot,
    store_value_regs_to_value_slot,
    store_value_to_address,
    zero_address,
)
from .self_backend_ir import (
    PARSED_INSTRUCTION_KINDS,
    ParsedBlock,
    ParsedFunction,
    TypeDesc,
    _align_to,
    parsed_function_has_value_slot,
    parsed_function_value_slot_offset,
    text_key_mapping_get,
    text_key_names_equal,
)
from .self_backend_kernel import (
    _scalar_window_is_complete,
    IndexedFunctionKernel,
    TYPE_KIND_ARRAY,
    TYPE_KIND_FP,
    TYPE_KIND_INT,
    TYPE_KIND_PTR,
    TYPE_KIND_STRUCT,
    get_indexed_function_kernel,
)
from .self_backend_module_symbols import PreparedModuleSymbols
from .self_backend_parse import is_aggregate_literal_value
from .self_backend_value_arena import CompilerInt2, CompilerInt4


def _canonical_post_call_error_edge_indexed(
    func: ParsedFunction,
    block_id: int,
) -> tuple[str, str] | None:
    """Return ``(error, success)`` for the frontend's exact post-call check.

    Keep this recognizer intentionally structural and finite.  The canonical
    block tail is one possibly-raising call, a direct zero-argument
    ``py_err_occurred`` call, ``icmp ne i64 <result>, 0``, and a conditional
    branch on that comparison.  Merely seeing an equivalent-looking assembly
    branch is not enough: ordinary conditionals and hand-written status checks
    must retain source block order.
    """
    kernel = get_indexed_function_kernel(func)
    term_header: CompilerInt4 = kernel.terminator_header(block_id)
    term_span: CompilerInt4 = kernel.terminator_span(block_id)
    if (
        PARSED_INSTRUCTION_KINDS[term_header.first] != "br_cond"
        or kernel.instruction_count(block_id) < 3
    ):
        return None
    instruction_count = kernel.instruction_count(block_id)
    raising_index = instruction_count - 3
    err_index = instruction_count - 2
    compare_index = instruction_count - 1
    raising_kind = PARSED_INSTRUCTION_KINDS[
        kernel.instruction_kind_id(block_id, raising_index)
    ]
    err_kind = PARSED_INSTRUCTION_KINDS[
        kernel.instruction_kind_id(block_id, err_index)
    ]
    compare_kind = PARSED_INSTRUCTION_KINDS[
        kernel.instruction_kind_id(block_id, compare_index)
    ]
    if (
        raising_kind != "call"
        or err_kind != "call"
        or compare_kind != "icmp"
    ):
        return None
    compare_record: CompilerInt4 = kernel.instruction_record(
        kernel.instruction_record_id(block_id, compare_index)
    )
    raising_call_id = kernel.instruction_call_id(block_id, raising_index)
    raising_header: CompilerInt4 = kernel.call_header(raising_call_id)
    raising_callee = kernel.call_texts[raising_header.second]
    raising_is_indirect = bool(raising_header.third & 1)
    if not raising_is_indirect and raising_callee == "py_err_occurred":
        return None
    err_call_id = kernel.instruction_call_id(block_id, err_index)
    err_header: CompilerInt4 = kernel.call_header(err_call_id)
    err_span: CompilerInt4 = kernel.call_span(err_call_id)
    err_dest = (
        None if err_span.third < 0 else kernel.value_name(err_span.third)
    )
    err_ret_type: CompilerInt4 = kernel.type_header(err_header.first)
    err_callee = kernel.call_texts[err_header.second]
    err_is_indirect = bool(err_header.third & 1)
    err_is_vararg = bool(err_header.third & 2)
    if (
        err_dest is None
        or err_is_indirect
        or err_callee != "py_err_occurred"
        or err_span.first != 0
        or err_span.second != 0
        or err_is_vararg
        or err_ret_type.first != TYPE_KIND_INT
        or err_ret_type.second != 64
    ):
        return None

    predicate = kernel.call_texts[compare_record.first]
    compare_dest_id = kernel.defined_value_id(block_id, compare_index)
    value_type: CompilerInt4 = kernel.type_header(compare_record.second)
    lhs_id = compare_record.third
    rhs = (
        kernel.value_name(compare_record.fourth)
        if compare_record.fourth >= 0
        else kernel.call_texts[-compare_record.fourth - 1]
    )
    if (
        predicate != "ne"
        or value_type.first != TYPE_KIND_INT
        or value_type.second != 64
        or lhs_id != err_span.third
        or rhs != "0"
    ):
        return None

    if term_header.third != compare_dest_id:
        return None
    return (
        kernel.block_names[term_header.fourth],
        kernel.block_names[term_span.first],
    )


def _canonical_post_call_error_edge(
    block: ParsedBlock,
) -> tuple[str, str] | None:
    """Legacy diagnostic/test projection of the indexed recognizer contract."""

    term = block.terminator
    if (
        term is None
        or term.kind != "br_cond"
        or len(term.data) != 3
        or len(block.instructions) < 3
    ):
        return None
    raising_call = block.instructions[-3]
    err_call = block.instructions[-2]
    compare = block.instructions[-1]
    if (
        raising_call.kind != "call"
        or err_call.kind != "call"
        or compare.kind != "icmp"
        or len(raising_call.data) != 8
        or len(err_call.data) != 8
        or len(compare.data) != 5
    ):
        return None
    (
        _raising_dest,
        _raising_ret_type,
        raising_callee,
        raising_is_indirect,
        _raising_args,
        _raising_fixed_arg_count,
        _raising_is_vararg,
        _raising_arg_alignments,
    ) = raising_call.data
    if not raising_is_indirect and raising_callee == "py_err_occurred":
        return None
    (
        err_dest,
        err_ret_type,
        err_callee,
        err_is_indirect,
        err_args,
        err_fixed_arg_count,
        err_is_vararg,
        _err_arg_alignments,
    ) = err_call.data
    if (
        err_dest is None
        or err_is_indirect
        or err_callee != "py_err_occurred"
        or len(err_args) != 0
        or err_fixed_arg_count != 0
        or err_is_vararg
        or not err_ret_type.is_int
        or err_ret_type.width != 64
    ):
        return None
    predicate, compare_dest, value_type, lhs, rhs = compare.data
    if (
        predicate != "ne"
        or not value_type.is_int
        or value_type.width != 64
        or not text_key_names_equal(lhs, err_dest)
        or rhs != "0"
    ):
        return None
    cond_name, error_target, success_target = term.data
    if not text_key_names_equal(cond_name, compare_dest):
        return None
    return error_target, success_target


def plan_aarch64_canonical_error_fallthroughs(
    func: ParsedFunction,
    *,
    enabled: bool,
) -> None:
    """Place only canonical post-call success blocks after their checks."""
    func.aarch64_block_layout = []
    func.aarch64_cold_fallthrough_edges = []
    if not enabled:
        return

    kernel = get_indexed_function_kernel(func)
    kernel.reset_block_layout()
    block_count = len(kernel.block_names)
    if block_count == 0:
        return

    next_indices: list[int] = []
    previous_indices: list[int] = []
    processed: list[int] = [0] * block_count
    block_index = 0
    while block_index < block_count:
        next_indices.append(
            block_index + 1 if block_index + 1 < block_count else -1
        )
        previous_indices.append(block_index - 1)
        block_index += 1

    current_index = 0
    while current_index >= 0:
        edge = _canonical_post_call_error_edge_indexed(
            func, current_index
        )
        if edge is None:
            processed[current_index] = 1
            current_index = next_indices[current_index]
            continue
        error_target, success_target = edge

        target_index = kernel.block_id(success_target)
        # Do not pull a previously emitted/back-edge target across the source;
        # that would be global block placement rather than this finite pass.
        if (
            target_index < 0
            or target_index == current_index
            or processed[target_index] != 0
        ):
            processed[current_index] = 1
            current_index = next_indices[current_index]
            continue
        next_index = next_indices[current_index]
        if target_index != next_index:
            old_previous = previous_indices[target_index]
            old_next = next_indices[target_index]
            if old_previous >= 0:
                next_indices[old_previous] = old_next
            if old_next >= 0:
                previous_indices[old_next] = old_previous
            next_indices[current_index] = target_index
            previous_indices[target_index] = current_index
            next_indices[target_index] = next_index
            if next_index >= 0:
                previous_indices[next_index] = target_index
        func.aarch64_cold_fallthrough_edges.append(
            (
                kernel.block_names[current_index],
                error_target,
                success_target,
            )
        )
        processed[current_index] = 1
        current_index = next_indices[current_index]

    if func.aarch64_cold_fallthrough_edges:
        block_index = 0
        while block_index >= 0:
            kernel.block_layout_ids.append(block_index)
            block_index = next_indices[block_index]


def emit_bit_count_intrinsic_call(
    func: ParsedFunction,
    dest: str | None,
    ret_type: TypeDesc,
    callee: str,
    args: tuple[tuple[TypeDesc, str], ...],
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    if callee.startswith("llvm.ctpop."):
        if len(args) != 1:
            raise BackendUnavailable(
                f"self backend expects one arg for {callee!r} in {func.name!r}"
            )
    elif len(args) != 2:
        raise BackendUnavailable(
            f"self backend expects two args for {callee!r} in {func.name!r}"
        )
    if dest is None or not parsed_function_has_value_slot(func, dest):
        return []
    arg_type, value = args[0]
    if not arg_type.is_int or not ret_type.is_int or arg_type.width != ret_type.width:
        raise BackendUnavailable(
            f"self backend expects matching integer arg/ret types for {callee!r}, got {arg_type.describe()} -> {ret_type.describe()}"
        )
    if arg_type.width not in (32, 64):
        raise BackendUnavailable(
            f"self backend only supports ctlz/cttz on i32/i64 for now, got {arg_type.describe()}"
        )
    lines = materialize_value(func, value, arg_type, 9, module_symbols)
    src = reg_name(arg_type, 9)
    dst = reg_name(ret_type, 11)
    if callee.startswith("llvm.ctlz."):
        lines.append(f"  clz {dst}, {src}")
    elif callee.startswith("llvm.cttz."):
        lines.append(f"  rbit {dst}, {src}")
        lines.append(f"  clz {dst}, {dst}")
    elif callee.startswith("llvm.ctpop."):
        if arg_type.width == 32:
            lines.append(emitted_move_register_line("w10", "w9"))
            lines.append("  fmov d10, x10")
        else:
            lines.append("  fmov d10, x9")
        lines.append("  cnt v10.8b, v10.8b")
        lines.append("  addv b10, v10.8b")
        lines.append("  umov w11, v10.b[0]")
    else:
        raise BackendUnavailable(f"self backend does not support intrinsic {callee!r}")
    lines.extend(store_reg_to_value_slot(dst, func, dest))
    return lines


def _order_scalar_phi_copies(func, assignments):
    """Order scalar phi copies for safe direct slot-to-slot stores.

    A copy ``dest_i <- src_i`` must read ``slot(src_i)`` before any other copy
    overwrites that slot by writing its own destination. We emit a copy only
    once no *other* pending copy still reads the slot it is about to write;
    coalesced self-copies (source already in the destination slot) are dropped
    as no-ops. Returns the ordered ``(phi, match)`` list, or ``None`` if the
    copies form a cycle (a true swap) that no ordering can satisfy — the caller
    then falls back to the temp-buffered path.
    """
    copies = []
    for phi, match, _offset in assignments:
        dest_off = parsed_function_value_slot_offset(func, phi.dest)
        raw_src_off = parsed_function_value_slot_offset(func, match.value)
        src_off = raw_src_off if raw_src_off >= 0 else None
        if src_off == dest_off:
            continue  # coalesced self-copy: no instruction needed
        copies.append((phi, match, dest_off, src_off))

    ordered = []
    pending = list(copies)
    while pending:
        ready = [
            c
            for c in pending
            if not any(other is not c and other[3] == c[2] for other in pending)
        ]
        if not ready:
            return None  # cycle: caller uses the temp-buffered path
        for c in ready:
            ordered.append((c[0], c[1]))
            pending.remove(c)
    return ordered


def _indexed_phi_mem_op(type_header: CompilerInt4, *, load: bool) -> str:
    if type_header.first == TYPE_KIND_INT and type_header.second <= 8:
        return "ldrb" if load else "strb"
    if type_header.first == TYPE_KIND_INT and type_header.second <= 16:
        return "ldrh" if load else "strh"
    return "ldr" if load else "str"


def _indexed_phi_stack_store(
    kernel: IndexedFunctionKernel,
    slot_id: int,
    type_id: int,
    reg_index: int,
) -> list[str]:
    header: CompilerInt4 = kernel.type_header(type_id)
    if header.first == TYPE_KIND_INT and header.second <= 8:
        op = "sturb"
    elif header.first == TYPE_KIND_INT and header.second <= 16:
        op = "sturh"
    else:
        op = "stur"
    reg = reg_name_indexed(kernel, type_id, reg_index)
    offset = kernel.slot_offset(slot_id)
    if offset > 255:
        lines = emit_add_offset("x15", "x29", -offset)
        lines.append(emitted_memory_instruction_line(op, reg, "x15"))
        return lines
    return [emitted_memory_instruction_line(op, reg, "x29", -offset)]


# A phi copy's location key: a slot id, or this base plus a register index.
_PHI_REGISTER_KEY_BASE = 1 << 20
# Breaks a cycle of register/slot copies.  x10 is outside the allocation pools
# and no slot, constant or address helper used by the copies writes it (they
# use x9 and x12-x15).
_PHI_CYCLE_REGISTER = 10


def _phi_copy_to_register(
    func: ParsedFunction,
    kernel: IndexedFunctionKernel,
    type_id: int,
    dest_reg: int,
    src_reg: int,
    value_ref: int,
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    """Copy one incoming value into the register its phi was assigned."""
    dest = reg_name_indexed(kernel, type_id, dest_reg)
    if src_reg >= 0:
        if src_reg == dest_reg:
            return []
        return [
            emitted_move_register_line(
                dest, reg_name_indexed(kernel, type_id, src_reg)
            )
        ]
    lines = materialize_scalar_value_indexed(
        func,
        kernel,
        kernel.phi_incoming_value(value_ref),
        type_id,
        dest_reg,
        module_symbols,
        value_id=value_ref,
    )
    header: CompilerInt4 = kernel.type_header(type_id)
    if value_ref < 0 and header.first == TYPE_KIND_INT and header.second <= 16:
        # An allocated i1/i8/i16 holds its zero-extended value (the allocator
        # commits one with the same mask); a constant is materialised at
        # 32 bits.
        mask = 0xFF if header.second <= 8 else 0xFFFF
        lines.append(f"  and {dest}, {dest}, #0x{mask:x}")
    return lines


def _phi_copy_to_slot(
    func: ParsedFunction,
    kernel: IndexedFunctionKernel,
    type_id: int,
    dest_slot_id: int,
    src_reg: int,
    value_ref: int,
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    """Copy one incoming value into its phi's stack slot."""
    if src_reg >= 0:
        return _indexed_phi_stack_store(kernel, dest_slot_id, type_id, src_reg)
    lines = materialize_scalar_value_indexed(
        func,
        kernel,
        kernel.phi_incoming_value(value_ref),
        type_id,
        9,
        module_symbols,
        value_id=value_ref,
    )
    lines.extend(_indexed_phi_stack_store(kernel, dest_slot_id, type_id, 9))
    return lines


def _emit_phi_assignments_indexed(
    func: ParsedFunction,
    *,
    source_block_id: int,
    target_block_id: int,
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    """Emit the parallel copy for the edge into ``target_block_id``.

    A phi lives in its allocated register or its slot; an incoming value in
    its register, its slot, or is a constant.  A copy is emitted once no other
    pending copy still reads the location it writes, so every source is read
    before it is overwritten.  A cycle parks one integer source in x10; a
    cycle through a floating-point copy takes the stack-buffer path, which
    reads every source before writing any destination.
    """
    kernel = get_indexed_function_kernel(func)
    phi_fact: CompilerInt2 = kernel.block_phi_fact(target_block_id)
    phi_ids: list[int] = []
    incoming_refs: list[int] = []
    temp_offsets: list[int] = []
    dest_slot_ids: list[int] = []
    dest_regs: list[int] = []
    src_regs: list[int] = []
    dest_keys: list[int] = []
    src_keys: list[int] = []
    temp_offset = 0
    phi_index = 0
    while phi_index < phi_fact.second:
        phi_id = phi_fact.first + phi_index
        phi: CompilerInt4 = kernel.phi_record(phi_id)
        dest_slot_id = kernel.value_slot_id(phi.first)
        if dest_slot_id >= 0:
            incoming_ref = -1
            incoming_found = False
            incoming_index = 0
            while incoming_index < phi.fourth:
                incoming: CompilerInt2 = kernel.phi_incoming(
                    phi.third + incoming_index
                )
                if incoming.second == source_block_id:
                    incoming_ref = incoming.first
                    incoming_found = True
                    break
                incoming_index += 1
            if not incoming_found:
                raise BackendUnavailable(
                    "self backend could not resolve indexed phi incoming"
                )
            layout: CompilerInt4 = kernel.type_layout(phi.second)
            temp_align = max(1, min(layout.second, 8))
            temp_offset = _align_to(temp_offset, temp_align)
            dest_reg = allocated_scalar_register_indexed(kernel, phi.first, phi.second)
            src_reg = -1
            src_key = -1
            if incoming_ref >= 0:
                src_reg = allocated_scalar_register_indexed(
                    kernel, incoming_ref, phi.second
                )
                if src_reg >= 0:
                    src_key = _PHI_REGISTER_KEY_BASE + src_reg
                else:
                    src_key = kernel.value_slot_id(incoming_ref)
            phi_ids.append(phi_id)
            incoming_refs.append(incoming_ref)
            temp_offsets.append(temp_offset)
            dest_slot_ids.append(dest_slot_id)
            dest_regs.append(dest_reg)
            src_regs.append(src_reg)
            dest_keys.append(
                _PHI_REGISTER_KEY_BASE + dest_reg if dest_reg >= 0 else dest_slot_id
            )
            src_keys.append(src_key)
            phi_span: CompilerInt4 = kernel.type_span(phi.second)
            temp_offset += phi_span.third
        phi_index += 1

    if not phi_ids:
        return []

    pending: list[int] = []
    index = 0
    while index < len(phi_ids):
        if src_keys[index] != dest_keys[index]:
            pending.append(index)
        index += 1
    lines: list[str] = []
    while pending:
        ready: list[int] = []
        for candidate in pending:
            blocked = False
            for other in pending:
                if other != candidate and src_keys[other] == dest_keys[candidate]:
                    blocked = True
                    break
            if not blocked:
                ready.append(candidate)
        if not ready:
            victim = pending[0]
            victim_phi: CompilerInt4 = kernel.phi_record(phi_ids[victim])
            victim_header_is_scalar = False
            if type(kernel) is IndexedFunctionKernel:
                victim_header_scalar_id = victim_phi.second
                victim_header_is_scalar = _scalar_window_is_complete(
                    kernel.type_scalars, victim_header_scalar_id, 12, 0
                )
                if victim_header_is_scalar:
                    victim_header_scalar = kernel.type_scalars.get_unchecked(
                        victim_header_scalar_id * 12 + 0
                    )
                else:
                    victim_header: CompilerInt4 = kernel.type_header(victim_header_scalar_id)
            else:
                victim_header: CompilerInt4 = kernel.type_header(victim_phi.second)
            if (victim_header_scalar if victim_header_is_scalar else victim_header.first) == TYPE_KIND_FP:
                return _emit_phi_assignments_buffered(
                    func,
                    kernel,
                    phi_ids,
                    incoming_refs,
                    temp_offsets,
                    temp_offset,
                    dest_slot_ids,
                    dest_regs,
                    module_symbols,
                )
            lines.extend(
                _phi_copy_to_register(
                    func,
                    kernel,
                    victim_phi.second,
                    _PHI_CYCLE_REGISTER,
                    src_regs[victim],
                    incoming_refs[victim],
                    module_symbols,
                )
            )
            src_regs[victim] = _PHI_CYCLE_REGISTER
            src_keys[victim] = _PHI_REGISTER_KEY_BASE + _PHI_CYCLE_REGISTER
            continue
        for candidate in ready:
            phi = kernel.phi_record(phi_ids[candidate])
            if dest_regs[candidate] >= 0:
                lines.extend(
                    _phi_copy_to_register(
                        func,
                        kernel,
                        phi.second,
                        dest_regs[candidate],
                        src_regs[candidate],
                        incoming_refs[candidate],
                        module_symbols,
                    )
                )
            else:
                lines.extend(
                    _phi_copy_to_slot(
                        func,
                        kernel,
                        phi.second,
                        dest_slot_ids[candidate],
                        src_regs[candidate],
                        incoming_refs[candidate],
                        module_symbols,
                    )
                )
            pending.remove(candidate)
    return lines


def _emit_phi_assignments_buffered(
    func: ParsedFunction,
    kernel: IndexedFunctionKernel,
    phi_ids: list[int],
    incoming_refs: list[int],
    temp_offsets: list[int],
    temp_size: int,
    dest_slot_ids: list[int],
    dest_regs: list[int],
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    """Copy every incoming value to a stack buffer, then to its phi."""
    total_temp = _align_to(temp_size, 16)
    lines = emit_stack_adjust(-total_temp) if total_temp else []
    index = 0
    while index < len(phi_ids):
        phi: CompilerInt4 = kernel.phi_record(phi_ids[index])
        value_ref = incoming_refs[index]
        lines.extend(emit_add_offset("x13", "sp", temp_offsets[index]))
        lines.extend(
            materialize_scalar_value_indexed(
                func,
                kernel,
                kernel.phi_incoming_value(value_ref),
                phi.second,
                9,
                module_symbols,
                value_id=value_ref,
            )
        )
        type_header: CompilerInt4 = kernel.type_header(phi.second)
        lines.append(
            f"  {_indexed_phi_mem_op(type_header, load=False)} "
            f"{reg_name_indexed(kernel, phi.second, 9)}, [x13]"
        )
        index += 1
    index = 0
    while index < len(phi_ids):
        phi = kernel.phi_record(phi_ids[index])
        lines.extend(emit_add_offset("x13", "sp", temp_offsets[index]))
        type_header = kernel.type_header(phi.second)
        load_reg = dest_regs[index] if dest_regs[index] >= 0 else 9
        lines.append(
            f"  {_indexed_phi_mem_op(type_header, load=True)} "
            f"{reg_name_indexed(kernel, phi.second, load_reg)}, [x13]"
        )
        if dest_regs[index] < 0:
            lines.extend(
                _indexed_phi_stack_store(
                    kernel,
                    dest_slot_ids[index],
                    phi.second,
                    9,
                )
            )
        index += 1
    if total_temp:
        lines.extend(emit_stack_adjust(total_temp))
    return lines


def emit_phi_assignments(
    func: ParsedFunction,
    *,
    source_block: str,
    target_block: str,
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    kernel = get_indexed_function_kernel(func)
    source_block_id = kernel.block_id(source_block)
    target_block_id = kernel.block_id(target_block)
    if source_block_id >= 0 and target_block_id >= 0:
        phi_fact: CompilerInt2 = kernel.block_phi_fact(target_block_id)
        all_scalar = True
        phi_index = 0
        while phi_index < phi_fact.second:
            indexed_phi: CompilerInt4 = kernel.phi_record(
                phi_fact.first + phi_index
            )
            indexed_phi_type_is_scalar = False
            if type(kernel) is IndexedFunctionKernel:
                indexed_phi_type_scalar_id = indexed_phi.second
                indexed_phi_type_is_scalar = _scalar_window_is_complete(
                    kernel.type_scalars, indexed_phi_type_scalar_id, 12, 0
                )
                if indexed_phi_type_is_scalar:
                    indexed_phi_type_scalar = kernel.type_scalars.get_unchecked(
                        indexed_phi_type_scalar_id * 12 + 0
                    )
                else:
                    indexed_phi_type: CompilerInt4 = kernel.type_header(indexed_phi_type_scalar_id)
            else:
                indexed_phi_type: CompilerInt4 = kernel.type_header(indexed_phi.second)
            kind_id = (indexed_phi_type_scalar if indexed_phi_type_is_scalar else indexed_phi_type.first)
            if kind_id == TYPE_KIND_ARRAY or kind_id == TYPE_KIND_STRUCT:
                all_scalar = False
                break
            phi_index += 1
        if all_scalar:
            return _emit_phi_assignments_indexed(
                func,
                source_block_id=source_block_id,
                target_block_id=target_block_id,
                module_symbols=module_symbols,
            )
    target = text_key_mapping_get(func.block_map, target_block)
    if target is None and target_block_id >= 0:
        target = kernel.diagnostic_block(
            target_block_id,
            include_instructions=False,
        )
        func.block_map[target_block] = target
    if target is None:
        for block in func.blocks:
            if block.name == target_block:
                target = block
                break
    if target is None:
        raise BackendUnavailable(
            f"self backend branch targets unknown block {target_block!r} in {func.name!r}"
        )
    if not target.phis and target_block_id >= 0:
        phi_fact = kernel.block_phi_fact(target_block_id)
        projected = []
        phi_index = 0
        while phi_index < phi_fact.second:
            projected.append(kernel.diagnostic_phi(target_block_id, phi_index))
            phi_index += 1
        target.phis = tuple(projected)

    assignments = []
    temp_offset = 0
    for phi in target.phis:
        if not parsed_function_has_value_slot(func, phi.dest):
            continue
        match = None
        for incoming in phi.incoming:
            if incoming.label == source_block:
                match = incoming
                break
        if match is None:
            raise BackendUnavailable(
                f"self backend could not resolve phi incoming for {phi.dest!r} from {source_block!r}"
            )
        temp_align = max(1, min(phi.type.align, 8))
        temp_offset = _align_to(temp_offset, temp_align)
        assignments.append((phi, match, temp_offset))
        temp_offset += phi.type.slot_size

    if not assignments:
        return []

    # Scalar phi copies can be lowered as direct slot-to-slot stores when they
    # can be *ordered* so that no copy overwrites a slot another copy still
    # needs to read. Slot coalescing can put a phi source in the same physical
    # slot as a *different* phi's destination, so the safe order is computed
    # from slot offsets, not SSA names. When the copies form a cycle (a true
    # swap), no ordering is safe and we fall through to the temp-buffered path
    # below, which stages every source before writing any destination.
    if all(
        not phi.type.is_array and not phi.type.is_struct
        for phi, _match, _offset in assignments
    ):
        ordered = _order_scalar_phi_copies(func, assignments)
        if ordered is not None:
            lines: list[str] = []
            for phi, match in ordered:
                lines.extend(
                    materialize_value(func, match.value, phi.type, 9, module_symbols)
                )
                lines.extend(store_value_regs_to_value_slot(func, phi.dest, 9))
            return lines

    total_temp = _align_to(temp_offset, 16)
    lines: list[str] = []
    if total_temp:
        lines.extend(emit_stack_adjust(-total_temp))

    temp_addr_reg = "x13"

    def _emit_temp_addr(offset: int) -> list[str]:
        return emit_add_offset(temp_addr_reg, "sp", offset)

    for phi, match, offset in assignments:
        lines.extend(_emit_temp_addr(offset))
        if (phi.type.is_array or phi.type.is_struct) and not aggregate_fits_reg_abi(
            phi.type
        ):
            if match.value == "zeroinitializer":
                lines.extend(zero_address(temp_addr_reg, phi.type.slot_size))
            elif is_aggregate_literal_value(match.value):
                lines.extend(
                    store_large_aggregate_literal_to_address(
                        phi.type,
                        match.value,
                        temp_addr_reg,
                        module_symbols=module_symbols,
                    )
                )
            else:
                lines.extend(
                    materialize_aggregate_storage_address(
                        func, match.value, phi.type, "x14"
                    )
                )
                lines.extend(
                    copy_address_to_address("x14", temp_addr_reg, phi.type.slot_size)
                )
            continue
        lines.extend(materialize_value(func, match.value, phi.type, 9, module_symbols))
        lines.extend(store_value_to_address(temp_addr_reg, phi.type, 9))

    for phi, _match, offset in assignments:
        if (phi.type.is_array or phi.type.is_struct) and not aggregate_fits_reg_abi(
            phi.type
        ):
            lines.extend(_emit_temp_addr(offset))
            lines.extend(
                copy_address_to_value_slot(temp_addr_reg, func, phi.dest)
            )
            continue
        lines.extend(_emit_temp_addr(offset))
        lines.extend(load_value_from_address(temp_addr_reg, phi.type, 9))
        lines.extend(store_value_regs_to_value_slot(func, phi.dest, 9))

    if total_temp:
        lines.extend(emit_stack_adjust(total_temp))
    return lines
