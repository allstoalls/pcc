from __future__ import annotations


from . import BackendUnavailable
from .self_backend_analysis import is_local_value_ref
from .self_backend_aarch64_darwin_abi import (
    abi_register_code_indexed,
    abi_value_reg_names,
    aggregate_hfa_members,
    aggregate_returned_indirect,
    aggregate_returned_indirect_indexed,
    aggregate_passed_indirect,
    aggregate_passed_indirect_indexed,
    assign_abi_arg_regs,
    reg_name,
    stack_arg_offsets,
    stack_arg_storage_size,
    stack_arg_storage_size_indexed,
    variadic_stack_arg_storage_size,
    variadic_stack_arg_storage_size_indexed,
    reg_name_indexed,
)
from .self_backend_aarch64_darwin_flow import emit_bit_count_intrinsic_call
from .self_backend_aarch64_darwin_materialize import (
    materialize_aggregate_storage_address,
    materialize_indirect_aggregate_arg_pointer,
    materialize_pointer,
    materialize_scalar_value_indexed,
    materialize_value,
    store_large_aggregate_literal_to_address,
)
from .self_backend_aarch64_darwin_mem import (
    aggregate_copy_chunks,
    chunk_store_op,
    emitted_addsub_immediate_line,
    emitted_addsub_register_line,
    emitted_compare_immediate_line,
    emitted_compare_register_line,
    emitted_cset_line,
    emitted_direct_call_line,
    emitted_memory_instruction_line,
    emitted_movewide_instruction_line,
)
from .self_backend_aarch64_darwin_regs import emit_add_offset, emit_const_to_reg
from .self_backend_aarch64_darwin_ops import sign_extend_int_reg
from .self_backend_aarch64_darwin_slots import (
    copy_address_to_address,
    emit_slot_base_address,
    copy_address_to_slot,
    copy_address_to_value_slot,
    emit_slot_base_address_parts,
    emit_value_slot_base_address,
    load_value_from_address,
    store_value_regs_to_slot,
    store_value_regs_to_slot_parts,
    store_value_regs_to_value_slot,
    store_value_to_address,
)
from .self_backend_aarch64_darwin_symbols import (
    asm_symbol,
    asm_symbol_prevalidated,
)
from .self_backend_ir import (
    ArgInfo,
    ParsedFunction,
    TypeDesc,
    _align_to,
    parsed_function_has_alloca_slot,
    parsed_function_has_value_slot,
    parsed_function_alloca_slot_offset,
    parsed_function_alloca_slot_type,
    parsed_function_value_slot_id,
    parsed_function_value_slot_offset,
)
from .self_backend_kernel import (
    TYPE_KIND_ARRAY,
    TYPE_KIND_FP,
    TYPE_KIND_INT,
    TYPE_KIND_PTR,
    TYPE_KIND_STRUCT,
    TYPE_KIND_VOID,
    IndexedFunctionKernel,
    get_indexed_function_kernel,
)
from .self_backend_value_arena import CompilerInt4
from .self_backend_module_symbols import PreparedModuleSymbols
from .self_backend_parse import (
    aggregate_literal_to_bytes,
    const_int_from_value,
    is_aggregate_literal_value,
)


_SIMD_BLOCK_SIZES = frozenset((32, 64, 128))


def _normalized_call_arg_alignments(
    args: tuple[tuple[TypeDesc, str], ...],
    arg_alignments: tuple[int, ...],
    *,
    function_name: str,
) -> tuple[int, ...]:
    if not arg_alignments:
        return (0,) * len(args)
    if len(arg_alignments) != len(args):
        raise BackendUnavailable(
            "self backend call argument alignment count does not match "
            f"operands in {function_name!r}"
        )
    return arg_alignments


def _aarch64_simd_block_size(
    size_value: str,
    volatile_value: str,
) -> int | None:
    size = const_int_from_value(size_value)
    is_volatile = const_int_from_value(volatile_value)
    if size not in _SIMD_BLOCK_SIZES or is_volatile != 0:
        return None
    return size


def _emit_aligned_simd_block_copy(
    func: ParsedFunction,
    dst_value: str,
    src_value: str,
    size: int,
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    lines = materialize_pointer(func, dst_value, 9, module_symbols)
    lines.extend(materialize_pointer(func, src_value, 10, module_symbols))
    offset = 0
    while offset < size:
        address_suffix = "" if offset == 0 else f", #{offset}"
        lines.append(f"  ldr q0, [x10{address_suffix}]")
        lines.append(f"  str q0, [x9{address_suffix}]")
        offset += 16
    return lines


def _emit_aligned_simd_block_zero(
    func: ParsedFunction,
    dst_value: str,
    size: int,
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    lines = materialize_pointer(func, dst_value, 9, module_symbols)
    lines.append("  movi v0.16b, #0")
    offset = 0
    while offset < size:
        address_suffix = "" if offset == 0 else f", #{offset}"
        lines.append(f"  str q0, [x9{address_suffix}]")
        offset += 16
    return lines


def _vector_lane_stride(vector_type: TypeDesc):
    if (
        not vector_type.is_array
        or vector_type.elem is None
        or not vector_type.elem.is_int
    ):
        raise BackendUnavailable(
            f"self backend vector intrinsic currently expects integer vector lanes, got {vector_type.describe()}"
        )
    return vector_type.elem, vector_type.elem.slot_size


def emit_vararg_start(
    func: ParsedFunction,
    ap_ptr: str,
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    if not func.is_vararg:
        raise BackendUnavailable(
            f"self backend saw llvm.va_start in non-variadic function {func.name!r}"
        )
    arg_types = [arg.type for arg in func.args]
    assignments = assign_abi_arg_regs(arg_types)
    stack_offsets = stack_arg_offsets(arg_types, assignments)
    fixed_stack_offsets = [offset for offset in stack_offsets if offset is not None]
    first_vararg_offset = 16 if not fixed_stack_offsets else fixed_stack_offsets[-1] + 8
    lines = materialize_pointer(func, ap_ptr, 9, module_symbols)
    lines.append(
        emitted_addsub_immediate_line(
            "add",
            "x10",
            "x29",
            first_vararg_offset,
        )
    )
    lines.append(emitted_memory_instruction_line("str", "x10", "x9"))
    return lines


def emit_va_arg(
    func: ParsedFunction,
    dest: str,
    ap_type: TypeDesc,
    ap: str,
    value_type: TypeDesc,
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    if not ap_type.is_ptr:
        raise BackendUnavailable(
            f"self backend va_arg expects pointer va_list storage, got {ap_type.describe()}"
        )
    if value_type.is_void or value_type.is_array or value_type.is_struct:
        raise BackendUnavailable(
            f"self backend va_arg only supports scalar results for now, got {value_type.describe()}"
        )
    lines = materialize_pointer(func, ap, 9, module_symbols)
    lines.append(emitted_memory_instruction_line("ldr", "x10", "x9"))
    lines.extend(load_value_from_address("x10", value_type, 11))
    lines.append(emitted_addsub_immediate_line("add", "x10", "x10", 8))
    lines.append(emitted_memory_instruction_line("str", "x10", "x9"))
    if parsed_function_has_value_slot(func, dest):
        lines.extend(store_value_regs_to_value_slot(func, dest, 11))
    return lines


def _can_spill_aggregate_constant(value: str) -> bool:
    return value in {
        "zeroinitializer",
        "poison",
        "undef",
    } or is_aggregate_literal_value(value)


def emit_vararg_stack_arg(
    func: ParsedFunction,
    arg_type: TypeDesc,
    value: str,
    slot_offset: int,
    module_symbols: PreparedModuleSymbols,
    *,
    value_id: int = -1,
) -> list[str]:
    if arg_type.is_array or arg_type.is_struct:
        addr_reg = "sp"
        lines: list[str] = []
        if slot_offset:
            lines.extend(emit_add_offset("x14", "sp", slot_offset))
            addr_reg = "x14"
        if _can_spill_aggregate_constant(value):
            lines.extend(
                store_large_aggregate_literal_to_address(
                    arg_type,
                    value,
                    addr_reg,
                    data_reg_64="x12",
                    data_reg_32="w12",
                    module_symbols=module_symbols,
                )
            )
            return lines
        lines.extend(
            materialize_aggregate_storage_address(func, value, arg_type, "x12")
        )
        lines.extend(copy_address_to_address("x12", addr_reg, arg_type.slot_size))
        return lines
    if arg_type.is_void:
        raise BackendUnavailable(
            f"self backend only supports scalar variadic stack args for now, got {arg_type.describe()}"
        )
    lines = materialize_value(
        func,
        value,
        arg_type,
        12,
        module_symbols,
        value_id=value_id,
    )
    addr_reg = "sp"
    if slot_offset:
        lines.extend(emit_add_offset("x14", "sp", slot_offset))
        addr_reg = "x14"
    lines.extend(store_value_to_address(addr_reg, arg_type, 12))
    return lines


def emit_fixed_stack_arg_load(
    func: ParsedFunction,
    arg: ArgInfo,
    stack_offset: int,
) -> list[str]:
    if aggregate_passed_indirect(arg.type):
        if not parsed_function_has_value_slot(func, arg.name):
            return []
        lines = emit_add_offset("x12", "x29", stack_offset)
        lines.append(emitted_memory_instruction_line("ldr", "x12", "x12"))
        lines.extend(copy_address_to_value_slot("x12", func, arg.name))
        return lines
    if arg.type.is_array or arg.type.is_struct:
        lines = emit_add_offset("x12", "x29", stack_offset)
        lines.extend(load_value_from_address("x12", arg.type, 11))
        lines.extend(store_value_regs_to_value_slot(func, arg.name, 11))
        return lines
    lines = emit_add_offset("x12", "x29", stack_offset)
    lines.extend(load_value_from_address("x12", arg.type, 11))
    lines.extend(store_value_regs_to_value_slot(func, arg.name, 11))
    return lines


def emit_minmax_intrinsic_call(
    func: ParsedFunction,
    dest: str | None,
    ret_type: TypeDesc,
    callee: str,
    args: tuple[tuple[TypeDesc, str], ...],
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    if dest is None or not parsed_function_has_value_slot(func, dest):
        return []
    if len(args) != 2:
        raise BackendUnavailable(
            f"self backend {callee} intrinsic expects 2 args in {func.name!r}"
        )
    lhs_type, lhs = args[0]
    rhs_type, rhs = args[1]
    if (
        lhs_type.describe() != rhs_type.describe()
        or lhs_type.describe() != ret_type.describe()
    ):
        raise BackendUnavailable(
            f"self backend {callee} intrinsic type mismatch in {func.name!r}"
        )
    cond = {
        "llvm.smax.": "ge",
        "llvm.smin.": "le",
        "llvm.umax.": "hs",
        "llvm.umin.": "ls",
    }
    selected_cc: str | None = None
    for prefix, cc in cond.items():
        if callee.startswith(prefix):
            selected_cc = cc
            break
    if selected_cc is None:
        raise BackendUnavailable(
            f"self backend intrinsic not translated yet in {func.name!r}: {callee}"
        )
    is_signed = callee.startswith(("llvm.smax.", "llvm.smin."))
    if lhs_type.is_array and lhs_type.elem is not None and lhs_type.elem.is_int:
        return _emit_vector_minmax_intrinsic_call(
            func,
            dest,
            ret_type,
            lhs,
            rhs,
            selected_cc,
            is_signed,
            module_symbols,
        )
    if not lhs_type.is_int:
        raise BackendUnavailable(
            f"self backend {callee} intrinsic only supports integer results right now in {func.name!r}"
        )
    lines = materialize_value(func, lhs, lhs_type, 9, module_symbols)
    lines.extend(materialize_value(func, rhs, rhs_type, 10, module_symbols))
    lhs_reg = f"x9" if lhs_type.width > 32 else "w9"
    rhs_reg = f"x10" if lhs_type.width > 32 else "w10"
    dst_reg = f"x11" if lhs_type.width > 32 else "w11"
    if is_signed:
        lines.extend(sign_extend_int_reg(lhs_type, lhs_reg))
        lines.extend(sign_extend_int_reg(rhs_type, rhs_reg))
    lines.append(emitted_compare_register_line(lhs_reg, rhs_reg))
    lines.append(f"  csel {dst_reg}, {lhs_reg}, {rhs_reg}, {selected_cc}")
    lines.extend(store_value_regs_to_value_slot(func, dest, 11))
    return lines


def _emit_vector_minmax_intrinsic_call(
    func: ParsedFunction,
    dest: str,
    ret_type: TypeDesc,
    lhs: str,
    rhs: str,
    selected_cc: str,
    is_signed: bool,
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    lane_type = ret_type.elem
    if lane_type is None or not lane_type.is_int:
        raise BackendUnavailable(
            f"self backend vector min/max expects integer vector lanes in {func.name!r}: {ret_type.describe()}"
        )
    lane_stride = _align_to(lane_type.slot_size, lane_type.align)
    lines: list[str] = []
    lhs_addr: str | None = None
    rhs_addr: str | None = None
    if parsed_function_has_value_slot(func, lhs):
        lines.extend(
            emit_add_offset(
                "x15", "x29", -parsed_function_value_slot_offset(func, lhs), scratch_reg="x14"
            )
        )
        lhs_addr = "x15"
    if parsed_function_has_value_slot(func, rhs):
        lines.extend(
            emit_add_offset(
                "x16", "x29", -parsed_function_value_slot_offset(func, rhs), scratch_reg="x14"
            )
        )
        rhs_addr = "x16"
    lines.extend(
        emit_add_offset(
            "x17",
            "x29",
            -parsed_function_value_slot_offset(func, dest),
            scratch_reg="x14",
        )
    )
    for lane_index in range(ret_type.count):
        dst_addr = "x17"
        if lane_index:
            lines.extend(emit_add_offset("x14", "x17", lane_index * lane_stride))
            dst_addr = "x14"
        lines.extend(
            _emit_vector_minmax_lane_value(
                func, lhs, ret_type, lane_type, lane_index, 9, lhs_addr, module_symbols
            )
        )
        lines.extend(
            _emit_vector_minmax_lane_value(
                func, rhs, ret_type, lane_type, lane_index, 10, rhs_addr, module_symbols
            )
        )
        lhs_reg = reg_name(lane_type, 9)
        rhs_reg = reg_name(lane_type, 10)
        dst_reg = reg_name(lane_type, 11)
        if is_signed:
            lines.extend(sign_extend_int_reg(lane_type, lhs_reg))
            lines.extend(sign_extend_int_reg(lane_type, rhs_reg))
        lines.append(emitted_compare_register_line(lhs_reg, rhs_reg))
        lines.append(f"  csel {dst_reg}, {lhs_reg}, {rhs_reg}, {selected_cc}")
        lines.extend(store_value_to_address(dst_addr, lane_type, 11))
    return lines


def _emit_vector_minmax_lane_value(
    func: ParsedFunction,
    value: str,
    vector_type: TypeDesc,
    lane_type: TypeDesc,
    lane_index: int,
    dest_reg_index: int,
    addr_reg: str | None,
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    if addr_reg is not None:
        source_addr = addr_reg
        if lane_index:
            temp_reg = "x12" if addr_reg != "x12" else "x13"
            lines = emit_add_offset(
                temp_reg,
                addr_reg,
                lane_index * _align_to(lane_type.slot_size, lane_type.align),
            )
            lines.extend(load_value_from_address(temp_reg, lane_type, dest_reg_index))
            return lines
        return load_value_from_address(source_addr, lane_type, dest_reg_index)
    const_value = const_int_from_value(value)
    if const_value is not None:
        return emit_const_to_reg(
            lane_type, reg_name(lane_type, dest_reg_index), const_value
        )
    if value == "zeroinitializer":
        return emit_const_to_reg(lane_type, reg_name(lane_type, dest_reg_index), 0)
    if is_aggregate_literal_value(value):
        literal_bytes = aggregate_literal_to_bytes(vector_type, value)
        stride = _align_to(lane_type.slot_size, lane_type.align)
        lane_bytes = literal_bytes[
            lane_index * stride : lane_index * stride + lane_type.slot_size
        ]
        lane_value = int.from_bytes(lane_bytes, "little")
        return emit_const_to_reg(
            lane_type, reg_name(lane_type, dest_reg_index), lane_value
        )
    return materialize_value(func, value, lane_type, dest_reg_index, module_symbols)


def _emit_vector_bswap_intrinsic_call(
    func: ParsedFunction,
    dest: str,
    ret_type: TypeDesc,
    value: str,
) -> list[str]:
    lane_type = ret_type.elem
    if lane_type is None or not lane_type.is_int:
        raise BackendUnavailable(
            f"self backend vector bswap expects integer vector lanes in {func.name!r}: {ret_type.describe()}"
        )
    if lane_type.width not in (16, 32, 64):
        raise BackendUnavailable(
            f"self backend vector bswap currently only supports i16/i32/i64 lanes in {func.name!r}: {ret_type.describe()}"
        )
    lane_stride = _align_to(lane_type.slot_size, lane_type.align)
    lines = materialize_aggregate_storage_address(func, value, ret_type, "x15")
    lines.extend(
        emit_add_offset(
            "x17",
            "x29",
            -parsed_function_value_slot_offset(func, dest),
            scratch_reg="x14",
        )
    )
    for lane_index in range(ret_type.count):
        src_addr = "x15"
        dst_addr = "x17"
        if lane_index:
            offset = lane_index * lane_stride
            lines.extend(emit_add_offset("x12", "x15", offset))
            lines.extend(emit_add_offset("x14", "x17", offset))
            src_addr = "x12"
            dst_addr = "x14"
        lines.extend(load_value_from_address(src_addr, lane_type, 9))
        if lane_type.width == 16:
            lines.append("  rev16 w10, w9")
        elif lane_type.width == 32:
            lines.append("  rev w10, w9")
        else:
            lines.append("  rev x10, x9")
        lines.extend(store_value_to_address(dst_addr, lane_type, 10))
    return lines


def _emit_vector_storage_address_with_scratch(
    func: ParsedFunction,
    value: str,
    value_type: TypeDesc,
    reg: str,
) -> list[str]:
    if parsed_function_has_value_slot(func, value):
        return emit_add_offset(
            reg, "x29", -parsed_function_value_slot_offset(func, value), scratch_reg="x14"
        )
    if (
        parsed_function_has_alloca_slot(func, value)
        and parsed_function_alloca_slot_type(func, value).describe() == value_type.describe()
    ):
        return emit_add_offset(
            reg, "x29", -parsed_function_alloca_slot_offset(func, value), scratch_reg="x14"
        )
    raise BackendUnavailable(
        f"self backend can only materialize vector storage from local slots right now in {func.name!r}: {value}"
    )


def emit_bswap_intrinsic_call(
    func: ParsedFunction,
    dest: str | None,
    ret_type: TypeDesc,
    callee: str,
    args: tuple[tuple[TypeDesc, str], ...],
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    if dest is None or not parsed_function_has_value_slot(func, dest):
        return []
    if len(args) != 1:
        raise BackendUnavailable(
            f"self backend {callee} intrinsic expects 1 arg in {func.name!r}"
        )
    arg_type, value = args[0]
    if arg_type.describe() != ret_type.describe():
        raise BackendUnavailable(
            f"self backend {callee} intrinsic only supports same-type results in {func.name!r}"
        )
    if arg_type.is_array and arg_type.elem is not None and arg_type.elem.is_int:
        return _emit_vector_bswap_intrinsic_call(func, dest, ret_type, value)
    if not arg_type.is_int:
        raise BackendUnavailable(
            f"self backend {callee} intrinsic only supports integer same-type results in {func.name!r}"
        )
    lines = materialize_value(func, value, arg_type, 9, module_symbols)
    if arg_type.width == 16:
        lines.append("  rev16 w10, w9")
    elif arg_type.width == 32:
        lines.append("  rev w10, w9")
    elif arg_type.width == 64:
        lines.append("  rev x10, x9")
    else:
        raise BackendUnavailable(
            f"self backend {callee} intrinsic currently only supports i16/i32/i64 in {func.name!r}"
        )
    lines.extend(store_value_regs_to_value_slot(func, dest, 10))
    return lines


def emit_memcpy_intrinsic_call(
    func: ParsedFunction,
    args: tuple[tuple[TypeDesc, str], ...],
    arg_alignments: tuple[int, ...],
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    if len(args) < 4:
        raise BackendUnavailable(
            f"self backend memcpy intrinsic expects at least 4 args in {func.name!r}"
        )
    dst_type, dst_value = args[0]
    src_type, src_value = args[1]
    size_type, size_value = args[2]
    _isvolatile_type, isvolatile_value = args[3]
    if not (dst_type.is_ptr and src_type.is_ptr and size_type.is_int):
        raise BackendUnavailable(
            f"self backend memcpy intrinsic arg types not translated yet in {func.name!r}"
        )
    alignments = _normalized_call_arg_alignments(
        args, arg_alignments, function_name=func.name
    )
    simd_size = _aarch64_simd_block_size(size_value, isvolatile_value)
    if simd_size is not None and alignments[0] >= 16 and alignments[1] >= 16:
        return _emit_aligned_simd_block_copy(
            func, dst_value, src_value, simd_size, module_symbols
        )
    lines = materialize_pointer(func, dst_value, 0, module_symbols)
    lines.extend(materialize_pointer(func, src_value, 1, module_symbols))
    lines.extend(materialize_value(func, size_value, size_type, 2, module_symbols))
    lines.append(emitted_direct_call_line(asm_symbol("memcpy", module_symbols)))
    return lines


def emit_memmove_intrinsic_call(
    func: ParsedFunction,
    args: tuple[tuple[TypeDesc, str], ...],
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    if len(args) < 4:
        raise BackendUnavailable(
            f"self backend memmove intrinsic expects at least 4 args in {func.name!r}"
        )
    dst_type, dst_value = args[0]
    src_type, src_value = args[1]
    size_type, size_value = args[2]
    _isvolatile_type, _isvolatile_value = args[3]
    if not (dst_type.is_ptr and src_type.is_ptr and size_type.is_int):
        raise BackendUnavailable(
            f"self backend memmove intrinsic arg types not translated yet in {func.name!r}"
        )
    lines = materialize_pointer(func, dst_value, 0, module_symbols)
    lines.extend(materialize_pointer(func, src_value, 1, module_symbols))
    lines.extend(materialize_value(func, size_value, size_type, 2, module_symbols))
    lines.append(emitted_direct_call_line(asm_symbol("memmove", module_symbols)))
    return lines


def emit_memset_intrinsic_call(
    func: ParsedFunction,
    args: tuple[tuple[TypeDesc, str], ...],
    arg_alignments: tuple[int, ...],
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    if len(args) < 4:
        raise BackendUnavailable(
            f"self backend memset intrinsic expects at least 4 args in {func.name!r}"
        )
    dst_type, dst_value = args[0]
    value_type, value = args[1]
    size_type, size_value = args[2]
    _isvolatile_type, isvolatile_value = args[3]
    if not (dst_type.is_ptr and value_type.is_int and size_type.is_int):
        raise BackendUnavailable(
            f"self backend memset intrinsic arg types not translated yet in {func.name!r}"
        )
    alignments = _normalized_call_arg_alignments(
        args, arg_alignments, function_name=func.name
    )
    simd_size = _aarch64_simd_block_size(size_value, isvolatile_value)
    fill_value = const_int_from_value(value)
    if simd_size is not None and fill_value == 0 and alignments[0] >= 16:
        return _emit_aligned_simd_block_zero(
            func, dst_value, simd_size, module_symbols
        )
    constant_size = const_int_from_value(size_value)
    if (
        fill_value == 0
        and value_type.width == 8
        and isvolatile_value in ("0", "false")
        and constant_size is not None
        and 0 <= constant_size <= 128
    ):
        if constant_size == 0:
            return []
        # Small normal-memory stores support alignment 1 on AArch64. Exact
        # chunking preserves the end boundary; the volatile path stays out.
        lines = materialize_pointer(func, dst_value, 9, module_symbols)
        for offset, chunk_size in aggregate_copy_chunks(constant_size):
            lines.append(emitted_memory_instruction_line(
                chunk_store_op(chunk_size, stack=False),
                "xzr" if chunk_size == 8 else "wzr", "x9", offset,
            ))
        return lines
    lines = materialize_pointer(func, dst_value, 0, module_symbols)
    lines.extend(materialize_value(func, value, value_type, 1, module_symbols))
    lines.extend(materialize_value(func, size_value, size_type, 2, module_symbols))
    lines.append(emitted_direct_call_line(asm_symbol("memset", module_symbols)))
    return lines


def emit_vector_reduce_add_intrinsic_call(
    func: ParsedFunction,
    dest: str | None,
    ret_type: TypeDesc,
    callee: str,
    args: tuple[tuple[TypeDesc, str], ...],
) -> list[str]:
    if dest is None or not parsed_function_has_value_slot(func, dest):
        return []
    if len(args) != 1:
        raise BackendUnavailable(
            f"self backend {callee} intrinsic expects 1 arg in {func.name!r}"
        )
    vector_type, value = args[0]
    if (
        not vector_type.is_array
        or vector_type.elem is None
        or not vector_type.elem.is_int
    ):
        raise BackendUnavailable(
            f"self backend {callee} intrinsic currently only supports integer vectors in {func.name!r}"
        )
    lane_type = vector_type.elem
    if lane_type.describe() != ret_type.describe():
        raise BackendUnavailable(
            f"self backend {callee} intrinsic expects lane type to match return type in {func.name!r}"
        )
    lane_stride = _align_to(lane_type.slot_size, lane_type.align)
    lines = materialize_aggregate_storage_address(func, value, vector_type, "x15")
    acc_reg = "x11" if ret_type.width > 32 else "w11"
    lines.append(emitted_movewide_instruction_line("movz", acc_reg, 0))
    for lane_index in range(vector_type.count):
        addr_reg = "x15"
        if lane_index:
            lines.extend(emit_add_offset("x14", "x15", lane_index * lane_stride))
            addr_reg = "x14"
        lines.extend(load_value_from_address(addr_reg, lane_type, 10))
        lane_reg = "x10" if lane_type.width > 32 else "w10"
        lines.append(
            emitted_addsub_register_line(
                "add",
                acc_reg,
                acc_reg,
                lane_reg,
            )
        )
    lines.extend(store_value_regs_to_value_slot(func, dest, 11))
    return lines


def emit_vector_reduce_mul_intrinsic_call(
    func: ParsedFunction,
    dest: str | None,
    ret_type: TypeDesc,
    callee: str,
    args: tuple[tuple[TypeDesc, str], ...],
) -> list[str]:
    if dest is None or not parsed_function_has_value_slot(func, dest):
        return []
    if len(args) != 1:
        raise BackendUnavailable(
            f"self backend {callee} intrinsic expects 1 arg in {func.name!r}"
        )
    vector_type, value = args[0]
    if (
        not vector_type.is_array
        or vector_type.elem is None
        or not vector_type.elem.is_int
    ):
        raise BackendUnavailable(
            f"self backend {callee} intrinsic currently only supports integer vectors in {func.name!r}"
        )
    lane_type = vector_type.elem
    if lane_type.describe() != ret_type.describe():
        raise BackendUnavailable(
            f"self backend {callee} intrinsic expects lane type to match return type in {func.name!r}"
        )
    lane_stride = _align_to(lane_type.slot_size, lane_type.align)
    lines = materialize_aggregate_storage_address(func, value, vector_type, "x15")
    acc_reg = "x11" if ret_type.width > 32 else "w11"
    lines.append(emitted_movewide_instruction_line("movz", acc_reg, 1))
    for lane_index in range(vector_type.count):
        addr_reg = "x15"
        if lane_index:
            lines.extend(emit_add_offset("x14", "x15", lane_index * lane_stride))
            addr_reg = "x14"
        lines.extend(load_value_from_address(addr_reg, lane_type, 10))
        lane_reg = "x10" if lane_type.width > 32 else "w10"
        lines.append(f"  mul {acc_reg}, {acc_reg}, {lane_reg}")
    lines.extend(store_value_regs_to_value_slot(func, dest, 11))
    return lines


def emit_vector_reduce_or_intrinsic_call(
    func: ParsedFunction,
    dest: str | None,
    ret_type: TypeDesc,
    callee: str,
    args: tuple[tuple[TypeDesc, str], ...],
) -> list[str]:
    if dest is None or not parsed_function_has_value_slot(func, dest):
        return []
    if len(args) != 1:
        raise BackendUnavailable(
            f"self backend {callee} intrinsic expects 1 arg in {func.name!r}"
        )
    vector_type, value = args[0]
    if (
        not vector_type.is_array
        or vector_type.elem is None
        or not vector_type.elem.is_int
    ):
        raise BackendUnavailable(
            f"self backend {callee} intrinsic currently only supports integer vectors in {func.name!r}"
        )
    lane_type = vector_type.elem
    if lane_type.describe() != ret_type.describe():
        raise BackendUnavailable(
            f"self backend {callee} intrinsic expects lane type to match return type in {func.name!r}"
        )
    lane_stride = _align_to(lane_type.slot_size, lane_type.align)
    lines = materialize_aggregate_storage_address(func, value, vector_type, "x15")
    acc_reg = reg_name(ret_type, 11)
    lane_reg = reg_name(lane_type, 10)
    lines.append(emitted_movewide_instruction_line("movz", acc_reg, 0))
    for lane_index in range(vector_type.count):
        addr_reg = "x15"
        if lane_index:
            lines.extend(emit_add_offset("x14", "x15", lane_index * lane_stride))
            addr_reg = "x14"
        lines.extend(load_value_from_address(addr_reg, lane_type, 10))
        lines.append(f"  orr {acc_reg}, {acc_reg}, {lane_reg}")
    lines.extend(store_value_regs_to_value_slot(func, dest, 11))
    return lines


def emit_vector_reduce_umax_intrinsic_call(
    func: ParsedFunction,
    dest: str | None,
    ret_type: TypeDesc,
    callee: str,
    args: tuple[tuple[TypeDesc, str], ...],
) -> list[str]:
    if dest is None or not parsed_function_has_value_slot(func, dest):
        return []
    if len(args) != 1:
        raise BackendUnavailable(
            f"self backend {callee} intrinsic expects 1 arg in {func.name!r}"
        )
    vector_type, value = args[0]
    if (
        not vector_type.is_array
        or vector_type.elem is None
        or not vector_type.elem.is_int
    ):
        raise BackendUnavailable(
            f"self backend {callee} intrinsic currently only supports integer vectors in {func.name!r}"
        )
    lane_type = vector_type.elem
    if lane_type.describe() != ret_type.describe():
        raise BackendUnavailable(
            f"self backend {callee} intrinsic expects lane type to match return type in {func.name!r}"
        )
    lane_stride = _align_to(lane_type.slot_size, lane_type.align)
    lines = materialize_aggregate_storage_address(func, value, vector_type, "x15")
    acc_reg = reg_name(ret_type, 11)
    lane_reg = reg_name(lane_type, 10)
    lines.append(emitted_movewide_instruction_line("movz", acc_reg, 0))
    for lane_index in range(vector_type.count):
        addr_reg = "x15"
        if lane_index:
            lines.extend(emit_add_offset("x14", "x15", lane_index * lane_stride))
            addr_reg = "x14"
        lines.extend(load_value_from_address(addr_reg, lane_type, 10))
        lines.append(emitted_compare_register_line(acc_reg, lane_reg))
        lines.append(f"  csel {acc_reg}, {acc_reg}, {lane_reg}, hs")
    lines.extend(store_value_regs_to_value_slot(func, dest, 11))
    return lines


def emit_ucmp_intrinsic_call(
    func: ParsedFunction,
    dest: str | None,
    ret_type: TypeDesc,
    callee: str,
    args: tuple[tuple[TypeDesc, str], ...],
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    if dest is None or not parsed_function_has_value_slot(func, dest):
        return []
    if len(args) != 2:
        raise BackendUnavailable(
            f"self backend {callee} intrinsic expects 2 args in {func.name!r}"
        )
    lhs_type, lhs = args[0]
    rhs_type, rhs = args[1]
    if (
        lhs_type.describe() != rhs_type.describe()
        or not lhs_type.is_int
        or not ret_type.is_int
        or ret_type.width > 32
    ):
        raise BackendUnavailable(
            f"self backend {callee} intrinsic expects integer operands and <=i32 result in {func.name!r}"
        )
    lines = materialize_value(func, lhs, lhs_type, 9, module_symbols)
    lines.extend(materialize_value(func, rhs, rhs_type, 10, module_symbols))
    lines.append(
        emitted_compare_register_line(
            reg_name(lhs_type, 9),
            reg_name(rhs_type, 10),
        )
    )
    lines.append(emitted_cset_line("w11", "hi"))
    lines.append(emitted_cset_line("w12", "lo"))
    lines.append(emitted_addsub_register_line("sub", "w11", "w11", "w12"))
    lines.extend(store_value_regs_to_value_slot(func, dest, 11))
    return lines


def emit_usub_sat_intrinsic_call(
    func: ParsedFunction,
    dest: str | None,
    ret_type: TypeDesc,
    callee: str,
    args: tuple[tuple[TypeDesc, str], ...],
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    if dest is None or not parsed_function_has_value_slot(func, dest):
        return []
    if len(args) != 2:
        raise BackendUnavailable(
            f"self backend {callee} intrinsic expects 2 args in {func.name!r}"
        )
    lhs_type, lhs = args[0]
    rhs_type, rhs = args[1]
    if (
        lhs_type.describe() != rhs_type.describe()
        or lhs_type.describe() != ret_type.describe()
    ):
        raise BackendUnavailable(
            f"self backend {callee} intrinsic type mismatch in {func.name!r}"
        )
    if ret_type.is_int:
        lines = materialize_value(func, lhs, lhs_type, 9, module_symbols)
        lines.extend(materialize_value(func, rhs, rhs_type, 10, module_symbols))
        dst_reg = "x11" if ret_type.width > 32 else "w11"
        zero_reg = "xzr" if ret_type.width > 32 else "wzr"
        lines.append(
            f"  subs {dst_reg}, {reg_name(ret_type, 9)}, {reg_name(ret_type, 10)}"
        )
        lines.append(f"  csel {dst_reg}, {dst_reg}, {zero_reg}, hs")
        lines.extend(store_value_regs_to_value_slot(func, dest, 11))
        return lines
    lane_type, lane_stride = _vector_lane_stride(ret_type)
    lines = materialize_aggregate_storage_address(func, lhs, lhs_type, "x15")
    lines.extend(materialize_aggregate_storage_address(func, rhs, rhs_type, "x16"))
    lines.extend(emit_value_slot_base_address(func, dest, "x17"))
    dst_reg = "x11" if lane_type.width > 32 else "w11"
    zero_reg = "xzr" if lane_type.width > 32 else "wzr"
    for lane_index in range(ret_type.count):
        lhs_addr = "x15"
        rhs_addr = "x16"
        dst_addr = "x17"
        if lane_index:
            lines.extend(emit_add_offset("x12", "x15", lane_index * lane_stride))
            lines.extend(emit_add_offset("x13", "x16", lane_index * lane_stride))
            lines.extend(emit_add_offset("x14", "x17", lane_index * lane_stride))
            lhs_addr = "x12"
            rhs_addr = "x13"
            dst_addr = "x14"
        lines.extend(load_value_from_address(lhs_addr, lane_type, 9))
        lines.extend(load_value_from_address(rhs_addr, lane_type, 10))
        lines.append(
            f"  subs {dst_reg}, {reg_name(lane_type, 9)}, {reg_name(lane_type, 10)}"
        )
        lines.append(f"  csel {dst_reg}, {dst_reg}, {zero_reg}, hs")
        lines.extend(store_value_to_address(dst_addr, lane_type, 11))
    return lines


def emit_uadd_sat_intrinsic_call(
    func: ParsedFunction,
    dest: str | None,
    ret_type: TypeDesc,
    callee: str,
    args: tuple[tuple[TypeDesc, str], ...],
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    if dest is None or not parsed_function_has_value_slot(func, dest):
        return []
    if len(args) != 2:
        raise BackendUnavailable(
            f"self backend {callee} intrinsic expects 2 args in {func.name!r}"
        )
    lhs_type, lhs = args[0]
    rhs_type, rhs = args[1]
    if (
        lhs_type.describe() != rhs_type.describe()
        or lhs_type.describe() != ret_type.describe()
    ):
        raise BackendUnavailable(
            f"self backend {callee} intrinsic type mismatch in {func.name!r}"
        )
    if ret_type.is_int:
        lines = materialize_value(func, lhs, lhs_type, 9, module_symbols)
        lines.extend(materialize_value(func, rhs, rhs_type, 10, module_symbols))
        dst_reg = "x11" if ret_type.width > 32 else "w11"
        zero_reg = "xzr" if ret_type.width > 32 else "wzr"
        lines.append(
            f"  adds {dst_reg}, {reg_name(ret_type, 9)}, {reg_name(ret_type, 10)}"
        )
        lines.append(f"  csinv {dst_reg}, {dst_reg}, {zero_reg}, lo")
        lines.extend(store_value_regs_to_value_slot(func, dest, 11))
        return lines
    lane_type, lane_stride = _vector_lane_stride(ret_type)
    lines = materialize_aggregate_storage_address(func, lhs, lhs_type, "x15")
    lines.extend(materialize_aggregate_storage_address(func, rhs, rhs_type, "x16"))
    lines.extend(emit_value_slot_base_address(func, dest, "x17"))
    dst_reg = "x11" if lane_type.width > 32 else "w11"
    zero_reg = "xzr" if lane_type.width > 32 else "wzr"
    for lane_index in range(ret_type.count):
        lhs_addr = "x15"
        rhs_addr = "x16"
        dst_addr = "x17"
        if lane_index:
            lines.extend(emit_add_offset("x12", "x15", lane_index * lane_stride))
            lines.extend(emit_add_offset("x13", "x16", lane_index * lane_stride))
            lines.extend(emit_add_offset("x14", "x17", lane_index * lane_stride))
            lhs_addr = "x12"
            rhs_addr = "x13"
            dst_addr = "x14"
        lines.extend(load_value_from_address(lhs_addr, lane_type, 9))
        lines.extend(load_value_from_address(rhs_addr, lane_type, 10))
        lines.append(
            f"  adds {dst_reg}, {reg_name(lane_type, 9)}, {reg_name(lane_type, 10)}"
        )
        lines.append(f"  csinv {dst_reg}, {dst_reg}, {zero_reg}, lo")
        lines.extend(store_value_to_address(dst_addr, lane_type, 11))
    return lines


def emit_umul_overflow_intrinsic_call(
    func: ParsedFunction,
    dest: str | None,
    ret_type: TypeDesc,
    callee: str,
    args: tuple[tuple[TypeDesc, str], ...],
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    if dest is None or not parsed_function_has_value_slot(func, dest):
        return []
    if len(args) != 2:
        raise BackendUnavailable(
            f"self backend {callee} intrinsic expects 2 args in {func.name!r}"
        )
    lhs_type, lhs = args[0]
    rhs_type, rhs = args[1]
    if lhs_type.describe() != rhs_type.describe() or not lhs_type.is_int:
        raise BackendUnavailable(
            f"self backend {callee} intrinsic currently expects same-width integer args in {func.name!r}"
        )
    if lhs_type.width not in (32, 64):
        raise BackendUnavailable(
            f"self backend {callee} intrinsic currently only supports i32/i64 args in {func.name!r}"
        )
    if not ret_type.is_struct or len(ret_type.fields) != 2:
        raise BackendUnavailable(
            f"self backend {callee} intrinsic expected {{ value, overflow }} return in {func.name!r}"
        )
    value_type, overflow_type = ret_type.fields
    if (
        value_type.describe() != lhs_type.describe()
        or not overflow_type.is_int
        or overflow_type.width != 1
    ):
        raise BackendUnavailable(
            f"self backend {callee} intrinsic return shape mismatch in {func.name!r}: {ret_type.describe()}"
        )
    lines = materialize_value(func, lhs, lhs_type, 9, module_symbols)
    lines.extend(materialize_value(func, rhs, rhs_type, 10, module_symbols))
    if lhs_type.width == 64:
        lines.append("  mul x11, x9, x10")
        lines.append("  umulh x12, x9, x10")
        lines.append(emitted_compare_immediate_line("x12", 0))
        lines.append(emitted_cset_line("w12", "ne"))
    else:
        lines.append("  umull x11, w9, w10")
        lines.append("  lsr x12, x11, #32")
        lines.append(emitted_compare_immediate_line("x12", 0))
        lines.append(emitted_cset_line("w12", "ne"))
        lines.append("  uxtw x11, w11")
        lines.append("  orr x11, x11, x12, lsl #32")
    lines.extend(store_value_regs_to_value_slot(func, dest, 11))
    return lines


def emit_uadd_overflow_intrinsic_call(
    func: ParsedFunction,
    dest: str | None,
    ret_type: TypeDesc,
    callee: str,
    args: tuple[tuple[TypeDesc, str], ...],
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    if dest is None or not parsed_function_has_value_slot(func, dest):
        return []
    if len(args) != 2:
        raise BackendUnavailable(
            f"self backend {callee} intrinsic expects 2 args in {func.name!r}"
        )
    lhs_type, lhs = args[0]
    rhs_type, rhs = args[1]
    if (
        lhs_type.describe() != rhs_type.describe()
        or not lhs_type.is_int
        or lhs_type.width not in (32, 64)
    ):
        raise BackendUnavailable(
            f"self backend {callee} intrinsic currently expects i32/i64 same-width integer args in {func.name!r}"
        )
    if not ret_type.is_struct or len(ret_type.fields) != 2:
        raise BackendUnavailable(
            f"self backend {callee} intrinsic expected {{ value, overflow }} return in {func.name!r}"
        )
    value_type, overflow_type = ret_type.fields
    if (
        value_type.describe() != lhs_type.describe()
        or not overflow_type.is_int
        or overflow_type.width != 1
    ):
        raise BackendUnavailable(
            f"self backend {callee} intrinsic return shape mismatch in {func.name!r}: {ret_type.describe()}"
        )
    lines = materialize_value(func, lhs, lhs_type, 9, module_symbols)
    lines.extend(materialize_value(func, rhs, rhs_type, 10, module_symbols))
    if lhs_type.width == 64:
        lines.append("  adds x11, x9, x10")
        lines.append(emitted_cset_line("w12", "hs"))
    else:
        lines.append("  adds w11, w9, w10")
        lines.append(emitted_cset_line("w12", "hs"))
        lines.append("  orr x11, x11, x12, lsl #32")
    lines.extend(store_value_regs_to_value_slot(func, dest, 11))
    return lines


def emit_signed_addsub_overflow_intrinsic_call(
    func: ParsedFunction,
    dest: str | None,
    ret_type: TypeDesc,
    callee: str,
    args: tuple[tuple[TypeDesc, str], ...],
    module_symbols: PreparedModuleSymbols,
    *,
    mnemonic: str,
) -> list[str]:
    """Lower ``llvm.s{add,sub}.with.overflow.{i32,i64}``.

    ``adds``/``subs`` set the AArch64 V flag on signed overflow, so the
    overflow element is ``cset vs``; the unsigned pair above uses the carry
    flag (``hs``) and is otherwise identical. The i32 form packs the i1 into
    the high half of x11 so the ``{i32, i1}`` value slot stores in one chunk.
    """
    if dest is None or not parsed_function_has_value_slot(func, dest):
        return []
    if len(args) != 2:
        raise BackendUnavailable(
            f"self backend {callee} intrinsic expects 2 args in {func.name!r}"
        )
    lhs_type, lhs = args[0]
    rhs_type, rhs = args[1]
    if (
        lhs_type.describe() != rhs_type.describe()
        or not lhs_type.is_int
        or lhs_type.width not in (32, 64)
    ):
        raise BackendUnavailable(
            f"self backend {callee} intrinsic currently expects i32/i64 same-width integer args in {func.name!r}"
        )
    if not ret_type.is_struct or len(ret_type.fields) != 2:
        raise BackendUnavailable(
            f"self backend {callee} intrinsic expected {{ value, overflow }} return in {func.name!r}"
        )
    value_type, overflow_type = ret_type.fields
    if (
        value_type.describe() != lhs_type.describe()
        or not overflow_type.is_int
        or overflow_type.width != 1
    ):
        raise BackendUnavailable(
            f"self backend {callee} intrinsic return shape mismatch in {func.name!r}: {ret_type.describe()}"
        )
    lines = materialize_value(func, lhs, lhs_type, 9, module_symbols)
    lines.extend(materialize_value(func, rhs, rhs_type, 10, module_symbols))
    if lhs_type.width == 64:
        lines.append(f"  {mnemonic} x11, x9, x10")
        lines.append(emitted_cset_line("w12", "vs"))
    else:
        lines.append(f"  {mnemonic} w11, w9, w10")
        lines.append(emitted_cset_line("w12", "vs"))
        lines.append("  orr x11, x11, x12, lsl #32")
    lines.extend(store_value_regs_to_value_slot(func, dest, 11))
    return lines


def emit_sadd_overflow_intrinsic_call(
    func: ParsedFunction,
    dest: str | None,
    ret_type: TypeDesc,
    callee: str,
    args: tuple[tuple[TypeDesc, str], ...],
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    return emit_signed_addsub_overflow_intrinsic_call(
        func, dest, ret_type, callee, args, module_symbols, mnemonic="adds"
    )


def emit_ssub_overflow_intrinsic_call(
    func: ParsedFunction,
    dest: str | None,
    ret_type: TypeDesc,
    callee: str,
    args: tuple[tuple[TypeDesc, str], ...],
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    return emit_signed_addsub_overflow_intrinsic_call(
        func, dest, ret_type, callee, args, module_symbols, mnemonic="subs"
    )


def emit_smul_overflow_intrinsic_call(
    func: ParsedFunction,
    dest: str | None,
    ret_type: TypeDesc,
    callee: str,
    args: tuple[tuple[TypeDesc, str], ...],
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    if dest is None or not parsed_function_has_value_slot(func, dest):
        return []
    if len(args) != 2:
        raise BackendUnavailable(
            f"self backend {callee} intrinsic expects 2 args in {func.name!r}"
        )
    lhs_type, lhs = args[0]
    rhs_type, rhs = args[1]
    if (
        lhs_type.describe() != rhs_type.describe()
        or not lhs_type.is_int
        or lhs_type.width not in (32, 64)
    ):
        raise BackendUnavailable(
            f"self backend {callee} intrinsic currently expects i32/i64 same-width integer args in {func.name!r}"
        )
    if not ret_type.is_struct or len(ret_type.fields) != 2:
        raise BackendUnavailable(
            f"self backend {callee} intrinsic expected {{ value, overflow }} return in {func.name!r}"
        )
    value_type, overflow_type = ret_type.fields
    if (
        value_type.describe() != lhs_type.describe()
        or not overflow_type.is_int
        or overflow_type.width != 1
    ):
        raise BackendUnavailable(
            f"self backend {callee} intrinsic return shape mismatch in {func.name!r}: {ret_type.describe()}"
        )
    lines = materialize_value(func, lhs, lhs_type, 9, module_symbols)
    lines.extend(materialize_value(func, rhs, rhs_type, 10, module_symbols))
    if lhs_type.width == 64:
        lines.append("  mul x11, x9, x10")
        lines.append("  smulh x12, x9, x10")
        lines.append("  asr x13, x11, #63")
        lines.append(emitted_compare_register_line("x12", "x13"))
        lines.append(emitted_cset_line("w12", "ne"))
    else:
        lines.append("  smull x11, w9, w10")
        lines.append("  sxtw x13, w11")
        lines.append(emitted_compare_register_line("x11", "x13"))
        lines.append(emitted_cset_line("w12", "ne"))
        lines.append("  uxtw x11, w11")
        lines.append("  orr x11, x11, x12, lsl #32")
    lines.extend(store_value_regs_to_value_slot(func, dest, 11))
    return lines


def emit_fshl_intrinsic_call(
    func: ParsedFunction,
    dest: str | None,
    ret_type: TypeDesc,
    callee: str,
    args: tuple[tuple[TypeDesc, str], ...],
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    if dest is None or not parsed_function_has_value_slot(func, dest):
        return []
    if len(args) != 3:
        raise BackendUnavailable(
            f"self backend {callee} intrinsic expects 3 args in {func.name!r}"
        )
    lhs_type, lhs = args[0]
    rhs_type, rhs = args[1]
    shift_type, shift = args[2]
    if lhs_type.is_array and lhs_type.elem is not None and lhs_type.elem.is_int:
        return _emit_vector_funnel_shift_intrinsic_call(
            func,
            dest,
            ret_type,
            lhs_type,
            lhs,
            rhs_type,
            rhs,
            shift_type,
            shift,
            "fshl",
            module_symbols,
        )
    if (
        lhs_type.describe() != rhs_type.describe()
        or lhs_type.describe() != ret_type.describe()
        or not lhs_type.is_int
        or not shift_type.is_int
        or lhs_type.width not in (8, 16, 32, 64)
    ):
        raise BackendUnavailable(
            f"self backend {callee} intrinsic currently expects i8/i16/i32/i64 funnel-shift operands in {func.name!r}"
        )
    width_mask = lhs_type.width - 1
    width_reg = "x" if lhs_type.width == 64 else "w"
    dst_reg = reg_name(ret_type, 14)
    lhs_reg = reg_name(lhs_type, 9)
    rhs_reg = reg_name(rhs_type, 10)
    shift_reg = reg_name(
        (
            shift_type
            if shift_type.width <= lhs_type.width
            else TypeDesc("int", lhs_type.width)
        ),
        11,
    )
    lines = materialize_value(func, lhs, lhs_type, 9, module_symbols)
    lines.extend(materialize_value(func, rhs, rhs_type, 10, module_symbols))
    lines.extend(materialize_value(func, shift, shift_type, 11, module_symbols))
    lines.append(f"  and {shift_reg}, {shift_reg}, #{width_mask}")
    lines.append(f"  neg {width_reg}12, {shift_reg}")
    lines.append(f"  and {width_reg}12, {width_reg}12, #{width_mask}")
    lines.append(f"  lslv {dst_reg}, {lhs_reg}, {shift_reg}")
    lines.append(f"  lsrv {width_reg}13, {rhs_reg}, {width_reg}12")
    lines.append(f"  orr {dst_reg}, {dst_reg}, {width_reg}13")
    lines.extend(store_value_regs_to_value_slot(func, dest, 14))
    return lines


def _emit_vector_funnel_shift_intrinsic_call(
    func: ParsedFunction,
    dest: str,
    ret_type: TypeDesc,
    lhs_type: TypeDesc,
    lhs: str,
    rhs_type: TypeDesc,
    rhs: str,
    shift_type: TypeDesc,
    shift: str,
    direction: str,
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    lane_type = ret_type.elem
    shift_lane_type = shift_type.elem if shift_type.is_array else shift_type
    if (
        lane_type is None
        or not lane_type.is_int
        or lane_type.width not in (8, 16, 32, 64)
        or lhs_type.describe() != rhs_type.describe()
        or lhs_type.describe() != ret_type.describe()
        or shift_lane_type is None
        or not shift_lane_type.is_int
        or (shift_type.is_array and shift_type.count != ret_type.count)
    ):
        raise BackendUnavailable(
            f"self backend llvm.{direction} vector intrinsic currently expects matching i8/i16/i32/i64 vector operands in {func.name!r}"
        )
    lane_stride = _align_to(lane_type.slot_size, lane_type.align)
    width_mask = lane_type.width - 1
    width_reg = "x" if lane_type.width == 64 else "w"
    dst_reg = reg_name(lane_type, 13)
    lhs_addr: str | None = None
    rhs_addr: str | None = None
    shift_addr: str | None = None
    lines: list[str] = []
    if parsed_function_has_value_slot(func, lhs) or parsed_function_has_alloca_slot(func, lhs):
        lines.extend(
            _emit_vector_storage_address_with_scratch(func, lhs, lhs_type, "x15")
        )
        lhs_addr = "x15"
    if parsed_function_has_value_slot(func, rhs) or parsed_function_has_alloca_slot(func, rhs):
        lines.extend(
            _emit_vector_storage_address_with_scratch(func, rhs, rhs_type, "x16")
        )
        rhs_addr = "x16"
    if parsed_function_has_value_slot(func, shift) or parsed_function_has_alloca_slot(func, shift):
        lines.extend(
            _emit_vector_storage_address_with_scratch(func, shift, shift_type, "x8")
        )
        shift_addr = "x8"
    lines.extend(
        emit_add_offset("x17", "x29", -parsed_function_value_slot_offset(func, dest), scratch_reg="x14")
    )
    for lane_index in range(ret_type.count):
        dst_addr = "x17"
        if lane_index:
            lines.extend(emit_add_offset("x14", "x17", lane_index * lane_stride))
            dst_addr = "x14"
        lines.extend(
            _emit_vector_minmax_lane_value(
                func, lhs, lhs_type, lane_type, lane_index, 9, lhs_addr, module_symbols
            )
        )
        lines.extend(
            _emit_vector_minmax_lane_value(
                func, rhs, rhs_type, lane_type, lane_index, 10, rhs_addr, module_symbols
            )
        )
        lines.extend(
            _emit_vector_minmax_lane_value(
                func,
                shift,
                shift_type if shift_type.is_array else ret_type,
                shift_lane_type,
                lane_index,
                11,
                shift_addr,
                module_symbols,
            )
        )
        shift_reg = reg_name(
            shift_lane_type if shift_lane_type.width <= lane_type.width else lane_type,
            11,
        )
        lhs_reg = reg_name(lane_type, 9)
        rhs_reg = reg_name(lane_type, 10)
        lines.append(f"  and {shift_reg}, {shift_reg}, #{width_mask}")
        lines.append(f"  neg {width_reg}12, {shift_reg}")
        lines.append(f"  and {width_reg}12, {width_reg}12, #{width_mask}")
        if direction == "fshl":
            lines.append(f"  lslv {dst_reg}, {lhs_reg}, {shift_reg}")
            lines.append(f"  lsrv {width_reg}12, {rhs_reg}, {width_reg}12")
        else:
            lines.append(f"  lsrv {dst_reg}, {rhs_reg}, {shift_reg}")
            lines.append(f"  lslv {width_reg}12, {lhs_reg}, {width_reg}12")
        lines.append(f"  orr {dst_reg}, {dst_reg}, {width_reg}12")
        lines.extend(store_value_to_address(dst_addr, lane_type, 13))
    return lines


def emit_fshr_intrinsic_call(
    func: ParsedFunction,
    dest: str | None,
    ret_type: TypeDesc,
    callee: str,
    args: tuple[tuple[TypeDesc, str], ...],
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    if dest is None or not parsed_function_has_value_slot(func, dest):
        return []
    if len(args) != 3:
        raise BackendUnavailable(
            f"self backend {callee} intrinsic expects 3 args in {func.name!r}"
        )
    lhs_type, lhs = args[0]
    rhs_type, rhs = args[1]
    shift_type, shift = args[2]
    if lhs_type.is_array and lhs_type.elem is not None and lhs_type.elem.is_int:
        return _emit_vector_funnel_shift_intrinsic_call(
            func,
            dest,
            ret_type,
            lhs_type,
            lhs,
            rhs_type,
            rhs,
            shift_type,
            shift,
            "fshr",
            module_symbols,
        )
    if (
        lhs_type.describe() != rhs_type.describe()
        or lhs_type.describe() != ret_type.describe()
        or not lhs_type.is_int
        or not shift_type.is_int
        or lhs_type.width not in (8, 16, 32, 64)
    ):
        raise BackendUnavailable(
            f"self backend {callee} intrinsic currently expects i8/i16/i32/i64 funnel-shift operands in {func.name!r}"
        )
    width_mask = lhs_type.width - 1
    width_reg = "x" if lhs_type.width == 64 else "w"
    dst_reg = reg_name(ret_type, 14)
    lhs_reg = reg_name(lhs_type, 9)
    rhs_reg = reg_name(rhs_type, 10)
    shift_reg = reg_name(
        (
            shift_type
            if shift_type.width <= lhs_type.width
            else TypeDesc("int", lhs_type.width)
        ),
        11,
    )
    lines = materialize_value(func, lhs, lhs_type, 9, module_symbols)
    lines.extend(materialize_value(func, rhs, rhs_type, 10, module_symbols))
    lines.extend(materialize_value(func, shift, shift_type, 11, module_symbols))
    lines.append(f"  and {shift_reg}, {shift_reg}, #{width_mask}")
    lines.append(f"  neg {width_reg}12, {shift_reg}")
    lines.append(f"  and {width_reg}12, {width_reg}12, #{width_mask}")
    lines.append(f"  lsrv {dst_reg}, {rhs_reg}, {shift_reg}")
    lines.append(f"  lslv {width_reg}13, {lhs_reg}, {width_reg}12")
    lines.append(f"  orr {dst_reg}, {dst_reg}, {width_reg}13")
    lines.extend(store_value_regs_to_value_slot(func, dest, 14))
    return lines


def emit_abs_intrinsic_call(
    func: ParsedFunction,
    dest: str | None,
    ret_type: TypeDesc,
    callee: str,
    args: tuple[tuple[TypeDesc, str], ...],
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    if dest is None or not parsed_function_has_value_slot(func, dest):
        return []
    if len(args) != 2:
        raise BackendUnavailable(
            f"self backend {callee} intrinsic expects 2 args in {func.name!r}"
        )
    value_type, value = args[0]
    _poison_type, _poison_flag = args[1]
    if (
        value_type.describe() != ret_type.describe()
        or not value_type.is_int
        or value_type.width not in (8, 16, 32, 64)
    ):
        raise BackendUnavailable(
            f"self backend {callee} intrinsic currently expects i8/i16/i32/i64 same-type results in {func.name!r}"
        )
    src_reg = reg_name(value_type, 9)
    dst_reg = reg_name(ret_type, 10)
    lines = materialize_value(func, value, value_type, 9, module_symbols)
    lines.extend(sign_extend_int_reg(value_type, src_reg))
    lines.append(emitted_compare_immediate_line(src_reg, 0))
    lines.append(f"  cneg {dst_reg}, {src_reg}, mi")
    lines.extend(store_value_regs_to_value_slot(func, dest, 10))
    return lines


def emit_copysign_intrinsic_call(
    func: ParsedFunction,
    dest: str | None,
    ret_type: TypeDesc,
    callee: str,
    args: tuple[tuple[TypeDesc, str], ...],
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    if dest is None or not parsed_function_has_value_slot(func, dest):
        return []
    if len(args) != 2:
        raise BackendUnavailable(
            f"self backend {callee} intrinsic expects 2 args in {func.name!r}"
        )
    lhs_type, lhs = args[0]
    rhs_type, rhs = args[1]
    if (
        lhs_type.describe() != rhs_type.describe()
        or lhs_type.describe() != ret_type.describe()
    ):
        raise BackendUnavailable(
            f"self backend {callee} intrinsic type mismatch in {func.name!r}"
        )
    if not lhs_type.is_fp or lhs_type.width not in (32, 64):
        raise BackendUnavailable(
            f"self backend {callee} intrinsic currently only supports float/double in {func.name!r}"
        )
    lines = materialize_value(func, lhs, lhs_type, 9, module_symbols)
    lines.extend(materialize_value(func, rhs, rhs_type, 10, module_symbols))
    if lhs_type.width == 32:
        lines.extend(
            [
                "  fmov w11, s9",
                "  fmov w12, s10",
                "  and w11, w11, #0x7fffffff",
                "  and w12, w12, #0x80000000",
                "  orr w11, w11, w12",
                "  fmov s11, w11",
            ]
        )
    else:
        lines.extend(
            [
                "  fmov x11, d9",
                "  fmov x12, d10",
                "  and x11, x11, #0x7fffffffffffffff",
                "  and x12, x12, #0x8000000000000000",
                "  orr x11, x11, x12",
                "  fmov d11, x11",
            ]
        )
    lines.extend(store_value_regs_to_value_slot(func, dest, 11))
    return lines


def emit_fabs_intrinsic_call(
    func: ParsedFunction,
    dest: str | None,
    ret_type: TypeDesc,
    callee: str,
    args: tuple[tuple[TypeDesc, str], ...],
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    if dest is None or not parsed_function_has_value_slot(func, dest):
        return []
    if len(args) != 1:
        raise BackendUnavailable(
            f"self backend {callee} intrinsic expects 1 arg in {func.name!r}"
        )
    value_type, value = args[0]
    if (
        value_type.describe() != ret_type.describe()
        or not value_type.is_fp
        or value_type.width not in (32, 64)
    ):
        raise BackendUnavailable(
            f"self backend {callee} intrinsic currently only supports float/double in {func.name!r}"
        )
    lines = materialize_value(func, value, value_type, 9, module_symbols)
    lines.append(f"  fabs {reg_name(ret_type, 11)}, {reg_name(value_type, 9)}")
    lines.extend(store_value_regs_to_value_slot(func, dest, 11))
    return lines


def emit_floor_intrinsic_call(
    func: ParsedFunction,
    dest: str | None,
    ret_type: TypeDesc,
    callee: str,
    args: tuple[tuple[TypeDesc, str], ...],
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    return _emit_frint_intrinsic_call(
        func, dest, ret_type, callee, args, module_symbols, "frintm"
    )


def emit_ceil_intrinsic_call(
    func: ParsedFunction,
    dest: str | None,
    ret_type: TypeDesc,
    callee: str,
    args: tuple[tuple[TypeDesc, str], ...],
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    return _emit_frint_intrinsic_call(
        func, dest, ret_type, callee, args, module_symbols, "frintp"
    )


def emit_trunc_intrinsic_call(
    func: ParsedFunction,
    dest: str | None,
    ret_type: TypeDesc,
    callee: str,
    args: tuple[tuple[TypeDesc, str], ...],
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    return _emit_frint_intrinsic_call(
        func, dest, ret_type, callee, args, module_symbols, "frintz"
    )


def emit_rint_intrinsic_call(
    func: ParsedFunction,
    dest: str | None,
    ret_type: TypeDesc,
    callee: str,
    args: tuple[tuple[TypeDesc, str], ...],
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    # llvm.rint / llvm.nearbyint round-to-nearest-even.
    return _emit_frint_intrinsic_call(
        func, dest, ret_type, callee, args, module_symbols, "frintn"
    )


def _emit_frint_intrinsic_call(
    func: ParsedFunction,
    dest: str | None,
    ret_type: TypeDesc,
    callee: str,
    args: tuple[tuple[TypeDesc, str], ...],
    module_symbols: PreparedModuleSymbols,
    asm_op: str,
) -> list[str]:
    if dest is None or not parsed_function_has_value_slot(func, dest):
        return []
    if len(args) != 1:
        raise BackendUnavailable(
            f"self backend {callee} intrinsic expects 1 arg in {func.name!r}"
        )
    value_type, value = args[0]
    if (
        value_type.describe() != ret_type.describe()
        or not value_type.is_fp
        or value_type.width not in (32, 64)
    ):
        raise BackendUnavailable(
            f"self backend {callee} intrinsic currently only supports float/double in {func.name!r}"
        )
    lines = materialize_value(func, value, value_type, 9, module_symbols)
    lines.append(f"  {asm_op} {reg_name(ret_type, 11)}, {reg_name(value_type, 9)}")
    lines.extend(store_value_regs_to_value_slot(func, dest, 11))
    return lines


def emit_sqrt_intrinsic_call(
    func: ParsedFunction,
    dest: str | None,
    ret_type: TypeDesc,
    callee: str,
    args: tuple[tuple[TypeDesc, str], ...],
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    if dest is None or not parsed_function_has_value_slot(func, dest):
        return []
    if len(args) != 1:
        raise BackendUnavailable(
            f"self backend {callee} intrinsic expects 1 arg in {func.name!r}"
        )
    value_type, value = args[0]
    if (
        value_type.describe() != ret_type.describe()
        or not value_type.is_fp
        or value_type.width not in (32, 64)
    ):
        raise BackendUnavailable(
            f"self backend {callee} intrinsic currently only supports float/double in {func.name!r}"
        )
    lines = materialize_value(func, value, value_type, 9, module_symbols)
    lines.append(f"  fsqrt {reg_name(ret_type, 11)}, {reg_name(value_type, 9)}")
    lines.extend(store_value_regs_to_value_slot(func, dest, 11))
    return lines


def emit_is_fpclass_intrinsic_call(
    func: ParsedFunction,
    dest: str | None,
    ret_type: TypeDesc,
    callee: str,
    args: tuple[tuple[TypeDesc, str], ...],
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    if dest is None or not parsed_function_has_value_slot(func, dest):
        return []
    if len(args) != 2:
        raise BackendUnavailable(
            f"self backend {callee} intrinsic expects 2 args in {func.name!r}"
        )
    value_type, value = args[0]
    mask_type, mask_value = args[1]
    mask = const_int_from_value(mask_value)
    if not ret_type.is_int or ret_type.width != 1:
        raise BackendUnavailable(
            f"self backend {callee} intrinsic expects i1 result in {func.name!r}"
        )
    if not value_type.is_fp or value_type.width not in (32, 64) or not mask_type.is_int:
        raise BackendUnavailable(
            f"self backend {callee} intrinsic currently supports float/double with integer mask in {func.name!r}"
        )
    if mask is None:
        raise BackendUnavailable(
            f"self backend {callee} intrinsic expects constant fpclass mask in {func.name!r}"
        )
    int_type = TypeDesc("int", value_type.width)
    bits_reg = "x12" if value_type.width > 32 else "w12"
    mask_reg = "x13" if value_type.width > 32 else "w13"
    tmp_reg = "x15" if value_type.width > 32 else "w15"
    tmp2_reg = "x16" if value_type.width > 32 else "w16"
    sign_bit = 1 << (value_type.width - 1)
    exponent_mask = ((0x7FF00000 << 32) | 0x0) if value_type.width > 32 else 0x7F800000
    fraction_mask = 0x000FFFFFFFFFFFFF if value_type.width > 32 else 0x007FFFFF
    quiet_nan_bit = 0x0008000000000000 if value_type.width > 32 else 0x00400000
    lines = materialize_value(func, value, value_type, 9, module_symbols)
    lines.append(f"  fmov {bits_reg}, {reg_name(value_type, 9)}")
    lines.append(emitted_movewide_instruction_line("movz", "w11", 0))
    handled_mask = 0

    def _or_eq_const(value_bits: int) -> None:
        if value_bits == 0:
            lines.append(emitted_compare_immediate_line(bits_reg, 0))
        else:
            lines.extend(emit_const_to_reg(int_type, mask_reg, value_bits))
            lines.append(emitted_compare_register_line(bits_reg, mask_reg))
        lines.append(emitted_cset_line("w14", "eq"))
        lines.append("  orr w11, w11, w14")

    def _start_sign_predicate(negative: bool) -> None:
        lines.extend(emit_const_to_reg(int_type, mask_reg, sign_bit))
        lines.append(f"  tst {bits_reg}, {mask_reg}")
        lines.append(emitted_cset_line("w14", "ne" if negative else "eq"))

    def _and_exponent_zero(expected_zero: bool) -> None:
        lines.extend(emit_const_to_reg(int_type, mask_reg, exponent_mask))
        lines.append(f"  and {tmp_reg}, {bits_reg}, {mask_reg}")
        lines.append(emitted_compare_immediate_line(tmp_reg, 0))
        lines.append(emitted_cset_line("w15", "eq" if expected_zero else "ne"))
        lines.append("  and w14, w14, w15")

    def _and_exponent_all(expected_all: bool) -> None:
        lines.extend(emit_const_to_reg(int_type, mask_reg, exponent_mask))
        lines.append(f"  and {tmp_reg}, {bits_reg}, {mask_reg}")
        lines.append(emitted_compare_register_line(tmp_reg, mask_reg))
        lines.append(emitted_cset_line("w15", "eq" if expected_all else "ne"))
        lines.append("  and w14, w14, w15")

    def _and_fraction_zero(expected_zero: bool) -> None:
        lines.extend(emit_const_to_reg(int_type, mask_reg, fraction_mask))
        lines.append(f"  and {tmp2_reg}, {bits_reg}, {mask_reg}")
        lines.append(emitted_compare_immediate_line(tmp2_reg, 0))
        lines.append(emitted_cset_line("w15", "eq" if expected_zero else "ne"))
        lines.append("  and w14, w14, w15")

    def _and_quiet_nan_bit_set(expected_set: bool) -> None:
        lines.extend(emit_const_to_reg(int_type, mask_reg, quiet_nan_bit))
        lines.append(f"  tst {bits_reg}, {mask_reg}")
        lines.append(emitted_cset_line("w15", "ne" if expected_set else "eq"))
        lines.append("  and w14, w14, w15")

    def _or_normal(negative: bool) -> None:
        _start_sign_predicate(negative)
        _and_exponent_zero(False)
        _and_exponent_all(False)
        lines.append("  orr w11, w11, w14")

    def _or_subnormal(negative: bool) -> None:
        _start_sign_predicate(negative)
        _and_exponent_zero(True)
        _and_fraction_zero(False)
        lines.append("  orr w11, w11, w14")

    def _or_nan(signaling: bool) -> None:
        # LLVM defines these mask bits in llvm/ADT/FloatingPointMode.h's
        # FPClassTest enum; keep this expansion aligned with that source.
        lines.append(emitted_movewide_instruction_line("movz", "w14", 1))
        _and_exponent_all(True)
        if signaling:
            _and_fraction_zero(False)
            _and_quiet_nan_bit_set(False)
        else:
            _and_quiet_nan_bit_set(True)
        lines.append("  orr w11, w11, w14")

    if mask & 0x1:
        _or_nan(signaling=True)
        handled_mask |= 0x1
    if mask & 0x2:
        _or_nan(signaling=False)
        handled_mask |= 0x2
    if mask & 0x4:
        _or_eq_const(sign_bit | exponent_mask)
        handled_mask |= 0x4
    if mask & 0x8:
        _or_normal(negative=True)
        handled_mask |= 0x8
    if mask & 0x10:
        _or_subnormal(negative=True)
        handled_mask |= 0x10
    if mask & 0x20:
        _or_eq_const(sign_bit)
        handled_mask |= 0x20
    if mask & 0x40:
        _or_eq_const(0)
        handled_mask |= 0x40
    if mask & 0x80:
        _or_subnormal(negative=False)
        handled_mask |= 0x80
    if mask & 0x100:
        _or_normal(negative=False)
        handled_mask |= 0x100
    if mask & 0x200:
        _or_eq_const(exponent_mask)
        handled_mask |= 0x200
    if mask & ~handled_mask:
        raise BackendUnavailable(
            f"self backend {callee} intrinsic mask not translated yet in {func.name!r}: {mask}"
        )
    lines.extend(store_value_regs_to_value_slot(func, dest, 11))
    return lines


def _indexed_scalar_mem_store_op(kind_id: int, width: int) -> str:
    if kind_id == TYPE_KIND_PTR:
        return "str"
    if kind_id == TYPE_KIND_INT and width <= 8:
        return "strb"
    if kind_id == TYPE_KIND_INT and width <= 16:
        return "strh"
    return "str"


def _indexed_scalar_stack_store_op(kind_id: int, width: int) -> str:
    if kind_id == TYPE_KIND_INT and width <= 8:
        return "sturb"
    if kind_id == TYPE_KIND_INT and width <= 16:
        return "sturh"
    return "stur"


def _emit_indexed_scalar_stack_arg(
    func: ParsedFunction,
    kernel: IndexedFunctionKernel,
    value: str,
    value_id: int,
    type_id: int,
    slot_offset: int,
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    lines = materialize_scalar_value_indexed(
        func,
        kernel,
        value,
        type_id,
        12,
        module_symbols,
        value_id=value_id,
    )
    addr_reg = "sp"
    if slot_offset:
        lines.extend(emit_add_offset("x14", "sp", slot_offset))
        addr_reg = "x14"
    kind_id = kernel.type_kind_id(type_id)
    width = kernel.type_width(type_id)
    lines.append(
        emitted_memory_instruction_line(
            _indexed_scalar_mem_store_op(kind_id, width),
            reg_name_indexed(kernel, type_id, 12),
            addr_reg,
        )
    )
    return lines


def _store_indexed_scalar_result(
    kernel: IndexedFunctionKernel,
    slot_id: int,
    type_id: int,
) -> list[str]:
    reg = reg_name_indexed(kernel, type_id, 0)
    op = _indexed_scalar_stack_store_op(
        kernel.type_kind_id(type_id),
        kernel.type_width(type_id),
    )
    offset = kernel.slot_offset(slot_id)
    if offset > 255:
        lines = emit_slot_base_address_parts(offset, "x15")
        lines.append(emitted_memory_instruction_line(op, reg, "x15"))
        return lines
    return [emitted_memory_instruction_line(op, reg, "x29", -offset)]


def emit_call_instruction_indexed(
    func: ParsedFunction,
    kernel: IndexedFunctionKernel,
    call_id: int,
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    header: CompilerInt4 = kernel.call_header(call_id)
    span: CompilerInt4 = kernel.call_span(call_id)
    ret_type_id = header.first
    ret_kind_id = kernel.type_kind_id(ret_type_id)
    ret_is_aggregate = (
        ret_kind_id == TYPE_KIND_ARRAY
        or ret_kind_id == TYPE_KIND_STRUCT
    )
    ret_is_indirect = bool(
        ret_is_aggregate
        and aggregate_returned_indirect_indexed(kernel, ret_type_id)
    )
    callee = kernel.call_texts[header.second]
    is_indirect = bool(header.third & 1)
    is_vararg_call = bool(header.third & 2)
    arg_count = span.first
    fixed_arg_count = span.second
    dest_value_id = span.third
    dest = None if dest_value_id < 0 else kernel.value_name(dest_value_id)
    if not is_indirect and callee.startswith("llvm."):
        raise BackendUnavailable(
            "indexed regular-call lowering received an LLVM intrinsic"
        )
    if is_vararg_call and fixed_arg_count > arg_count:
        raise BackendUnavailable(
            f"self backend saw malformed variadic call in {func.name!r}: fixed args exceed actual args"
        )

    fixed_count = fixed_arg_count if is_vararg_call else arg_count
    stack_arg_entries: list[tuple[int, int]] = []
    indirect_stack_ptr_entries: list[tuple[int, int]] = []
    pending_indirect_stack_literals: list[tuple[int, int]] = []
    pending_indirect_reg_literals: list[tuple[int, str]] = []
    lines: list[str] = []
    stack_offset = 0
    gpr_index = 0
    fpr_index = 0
    arg_index = 0
    while arg_index < fixed_count:
        raw: CompilerInt4 = kernel.call_arg(header.fourth + arg_index)
        arg_type_id = raw.first
        arg_kind_id = kernel.type_kind_id(arg_type_id)
        arg_is_aggregate = (
            arg_kind_id == TYPE_KIND_ARRAY
            or arg_kind_id == TYPE_KIND_STRUCT
        )
        arg_is_indirect = bool(
            arg_is_aggregate
            and aggregate_passed_indirect_indexed(kernel, arg_type_id)
        )
        value = (
            kernel.value_name(raw.second)
            if raw.second >= 0
            else kernel.call_texts[raw.third]
        )
        if arg_kind_id == TYPE_KIND_VOID:
            register_code = 0
        elif arg_kind_id == TYPE_KIND_FP:
            register_code = 17
        elif not arg_is_aggregate:
            register_code = 9
        else:
            register_code = abi_register_code_indexed(kernel, arg_type_id)
        register_class = register_code // 8
        register_count = register_code % 8
        first_register_index = -1
        if register_class == 2:
            if fpr_index + register_count > 8:
                if arg_is_aggregate:
                    fpr_index = 8
                register_count = 0
            else:
                first_register_index = fpr_index
                fpr_index += register_count
        elif register_class == 1:
            if gpr_index + register_count > 8:
                register_count = 0
            else:
                first_register_index = gpr_index
                gpr_index += register_count
        if register_count == 0:
            if arg_is_indirect:
                ptr_offset = stack_offset
                stack_offset += 8
                if _can_spill_aggregate_constant(value):
                    pending_indirect_stack_literals.append(
                        (ptr_offset, arg_index)
                    )
                else:
                    indirect_stack_ptr_entries.append(
                        (ptr_offset, arg_index)
                    )
            else:
                stack_arg_entries.append((stack_offset, arg_index))
                stack_offset += (
                    stack_arg_storage_size_indexed(kernel, arg_type_id)
                    if arg_is_aggregate
                    else 8
                )
            arg_index += 1
            continue
        if arg_is_indirect:
            first_register = "x" + str(first_register_index)
            if _can_spill_aggregate_constant(value):
                pending_indirect_reg_literals.append(
                    (arg_index, first_register)
                )
            else:
                arg_type = kernel.type_desc(arg_type_id)
                lines.extend(
                    materialize_indirect_aggregate_arg_pointer(
                        func,
                        value,
                        arg_type,
                        first_register,
                        value_id=raw.second,
                    )
                )
            arg_index += 1
            continue
        if arg_is_aggregate:
            lines.extend(
                materialize_value(
                    func,
                    value,
                    kernel.type_desc(arg_type_id),
                    first_register_index,
                    module_symbols,
                    value_id=raw.second,
                )
            )
        else:
            lines.extend(
                materialize_scalar_value_indexed(
                    func,
                    kernel,
                    value,
                    arg_type_id,
                    first_register_index,
                    module_symbols,
                    value_id=raw.second,
                )
            )
        arg_index += 1

    while arg_index < arg_count:
        raw: CompilerInt4 = kernel.call_arg(header.fourth + arg_index)
        stack_arg_entries.append((stack_offset, arg_index))
        variadic_kind_id = kernel.type_kind_id(raw.first)
        stack_offset += (
            variadic_stack_arg_storage_size_indexed(kernel, raw.first)
            if variadic_kind_id == TYPE_KIND_ARRAY
            or variadic_kind_id == TYPE_KIND_STRUCT
            else 8
        )
        arg_index += 1

    indirect_reg_literal_entries: list[tuple[int, int, str]] = []
    for indexed_arg, reg in pending_indirect_reg_literals:
        raw: CompilerInt4 = kernel.call_arg(header.fourth + indexed_arg)
        arg_span: CompilerInt4 = kernel.type_span(raw.first)
        stack_offset = _align_to(stack_offset, arg_span.fourth)
        temp_offset = stack_offset
        stack_offset += arg_span.third
        indirect_reg_literal_entries.append((temp_offset, indexed_arg, reg))
    indirect_stack_literal_entries: list[tuple[int, int, int]] = []
    for ptr_offset, indexed_arg in pending_indirect_stack_literals:
        raw: CompilerInt4 = kernel.call_arg(header.fourth + indexed_arg)
        arg_span: CompilerInt4 = kernel.type_span(raw.first)
        stack_offset = _align_to(stack_offset, arg_span.fourth)
        temp_offset = stack_offset
        stack_offset += arg_span.third
        indirect_stack_literal_entries.append(
            (ptr_offset, temp_offset, indexed_arg)
        )
    stack_size = _align_to(stack_offset, 16) if stack_offset else 0
    if ret_is_indirect:
        dest_offset = (
            -1
            if dest_value_id < 0
            else kernel.value_slot_offset(dest_value_id)
        )
        if dest_offset < 0:
            raise BackendUnavailable(
                f"self backend needs a materialized destination slot for large aggregate return in {func.name!r}"
            )
        lines.extend(emit_slot_base_address_parts(dest_offset, "x8"))
    if stack_size:
        lines.extend(emit_add_offset("sp", "sp", -stack_size, scratch_reg="x15"))
        for temp_offset, indexed_arg, reg in indirect_reg_literal_entries:
            raw: CompilerInt4 = kernel.call_arg(header.fourth + indexed_arg)
            arg_type = kernel.type_desc(raw.first)
            value = (
                kernel.value_name(raw.second)
                if raw.second >= 0
                else kernel.call_texts[raw.third]
            )
            lines.extend(emit_add_offset(reg, "sp", temp_offset, scratch_reg="x15"))
            lines.extend(
                store_large_aggregate_literal_to_address(
                    arg_type,
                    value,
                    reg,
                    data_reg_64="x12",
                    data_reg_32="w12",
                    module_symbols=module_symbols,
                )
            )
        for ptr_offset, temp_offset, indexed_arg in indirect_stack_literal_entries:
            raw: CompilerInt4 = kernel.call_arg(header.fourth + indexed_arg)
            arg_type = kernel.type_desc(raw.first)
            value = (
                kernel.value_name(raw.second)
                if raw.second >= 0
                else kernel.call_texts[raw.third]
            )
            lines.extend(emit_add_offset("x12", "sp", temp_offset, scratch_reg="x15"))
            lines.extend(
                store_large_aggregate_literal_to_address(
                    arg_type,
                    value,
                    "x12",
                    data_reg_64="x13",
                    data_reg_32="w13",
                    module_symbols=module_symbols,
                )
            )
            if ptr_offset:
                lines.extend(emit_add_offset("x14", "sp", ptr_offset, scratch_reg="x15"))
                lines.append(emitted_memory_instruction_line("str", "x12", "x14"))
            else:
                lines.append(emitted_memory_instruction_line("str", "x12", "sp"))
        for ptr_offset, indexed_arg in indirect_stack_ptr_entries:
            raw: CompilerInt4 = kernel.call_arg(header.fourth + indexed_arg)
            arg_type = kernel.type_desc(raw.first)
            value = (
                kernel.value_name(raw.second)
                if raw.second >= 0
                else kernel.call_texts[raw.third]
            )
            lines.extend(
                materialize_indirect_aggregate_arg_pointer(
                    func,
                    value,
                    arg_type,
                    "x12",
                    value_id=raw.second,
                )
            )
            if ptr_offset:
                lines.extend(emit_add_offset("x14", "sp", ptr_offset, scratch_reg="x15"))
                lines.append(emitted_memory_instruction_line("str", "x12", "x14"))
            else:
                lines.append(emitted_memory_instruction_line("str", "x12", "sp"))
        for offset, indexed_arg in stack_arg_entries:
            raw: CompilerInt4 = kernel.call_arg(header.fourth + indexed_arg)
            emit_kind_id = kernel.type_kind_id(raw.first)
            emit_value = (
                kernel.value_name(raw.second)
                if raw.second >= 0
                else kernel.call_texts[raw.third]
            )
            if (
                emit_kind_id == TYPE_KIND_ARRAY
                or emit_kind_id == TYPE_KIND_STRUCT
            ):
                lines.extend(
                    emit_vararg_stack_arg(
                        func,
                        kernel.type_desc(raw.first),
                        emit_value,
                        offset,
                        module_symbols,
                        value_id=raw.second,
                    )
                )
            else:
                lines.extend(
                    _emit_indexed_scalar_stack_arg(
                        func,
                        kernel,
                        emit_value,
                        raw.second,
                        raw.first,
                        offset,
                        module_symbols,
                    )
                )
    if call_id in func.aarch64_tail_call_ids:
        if is_indirect or is_vararg_call or stack_size or ret_is_aggregate:
            raise BackendUnavailable("invalid ABI in planned AArch64 scalar tail call")
        # The return terminator owns the frame restore and sibling transfer.
        # This jump has no ordinary post-call return PC in the current frame.
        return lines
    if is_indirect:
        callee_value_id = kernel.value_id(callee)
        lines.extend(
            materialize_pointer(
                func,
                callee,
                12,
                module_symbols,
                value_id=callee_value_id,
            )
        )
        lines.append("  blr x12")
    else:
        lines.append(
            emitted_direct_call_line(
                asm_symbol_prevalidated(callee, module_symbols)
            )
        )
    if stack_size:
        lines.extend(emit_add_offset("sp", "sp", stack_size, scratch_reg="x15"))
    dest_offset = (
        -1 if dest_value_id < 0 else kernel.value_slot_offset(dest_value_id)
    )
    if dest_offset >= 0 and not ret_is_indirect:
        slot_id = kernel.value_slot_id(dest_value_id)
        if slot_id < 0:
            raise BackendUnavailable(
                f"indexed call result slot is missing for {dest!r}"
            )
        slot_type_id = kernel.slot_type_id(slot_id)
        slot_header: CompilerInt4 = kernel.type_header(slot_type_id)
        if (
            slot_header.first == TYPE_KIND_ARRAY
            or slot_header.first == TYPE_KIND_STRUCT
        ):
            lines.extend(
                store_value_regs_to_slot_parts(
                    kernel.slot_offset(slot_id),
                    kernel.type_desc(slot_type_id),
                    0,
                )
            )
        else:
            lines.extend(
                _store_indexed_scalar_result(
                    kernel,
                    slot_id,
                    slot_type_id,
                )
            )
    return lines


def emit_call_instruction(
    func: ParsedFunction,
    dest: str | None,
    ret_type: TypeDesc,
    callee: str,
    is_indirect: bool,
    args: tuple[tuple[TypeDesc, str], ...],
    fixed_arg_count: int,
    is_vararg_call: bool,
    arg_alignments: tuple[int, ...] | PreparedModuleSymbols = (),
    module_symbols: PreparedModuleSymbols | None = None,
    *,
    dest_value_id: int = -1,
    indexed_use_count: int = -1,
    indexed_use0: int = -1,
    indexed_use_tail: int = -1,
) -> list[str]:
    # Keep the old direct-helper test/API call shape source-compatible while
    # parsed calls carry their exact call-site alignments as the eighth data
    # field.  Only parsed, explicitly aligned intrinsics can enter SIMD.
    if module_symbols is None:
        if not isinstance(arg_alignments, PreparedModuleSymbols):
            raise BackendUnavailable(
                f"self backend call in {func.name!r} is missing module symbols"
            )
        module_symbols = arg_alignments
        normalized_arg_alignments: tuple[int, ...] = ()
    else:
        if isinstance(arg_alignments, PreparedModuleSymbols):
            raise BackendUnavailable(
                f"self backend call in {func.name!r} has duplicate module symbols"
            )
        normalized_arg_alignments = arg_alignments
    if is_vararg_call and fixed_arg_count > len(args):
        raise BackendUnavailable(
            f"self backend saw malformed variadic call in {func.name!r}: fixed args exceed actual args"
        )
    if not is_indirect and callee.startswith(
        ("llvm.ctlz.", "llvm.cttz.", "llvm.ctpop.")
    ):
        return emit_bit_count_intrinsic_call(
            func, dest, ret_type, callee, args, module_symbols
        )
    if not is_indirect and callee.startswith("llvm.trap"):
        if args:
            raise BackendUnavailable(
                f"self backend llvm.trap expects 0 args in {func.name!r}"
            )
        return ["  brk #0"]
    if not is_indirect and callee.startswith("llvm.bswap."):
        return emit_bswap_intrinsic_call(
            func, dest, ret_type, callee, args, module_symbols
        )
    if not is_indirect and callee.startswith("llvm.memcpy."):
        return emit_memcpy_intrinsic_call(
            func, args, normalized_arg_alignments, module_symbols
        )
    if not is_indirect and callee.startswith("llvm.memmove."):
        return emit_memmove_intrinsic_call(func, args, module_symbols)
    if not is_indirect and callee.startswith("llvm.memset."):
        return emit_memset_intrinsic_call(
            func, args, normalized_arg_alignments, module_symbols
        )
    if not is_indirect and callee.startswith("llvm.vector.reduce.add."):
        return emit_vector_reduce_add_intrinsic_call(func, dest, ret_type, callee, args)
    if not is_indirect and callee.startswith("llvm.vector.reduce.mul."):
        return emit_vector_reduce_mul_intrinsic_call(func, dest, ret_type, callee, args)
    if not is_indirect and callee.startswith("llvm.vector.reduce.or."):
        return emit_vector_reduce_or_intrinsic_call(func, dest, ret_type, callee, args)
    if not is_indirect and callee.startswith("llvm.vector.reduce.umax."):
        return emit_vector_reduce_umax_intrinsic_call(
            func, dest, ret_type, callee, args
        )
    if not is_indirect and callee.startswith("llvm.ucmp."):
        return emit_ucmp_intrinsic_call(
            func, dest, ret_type, callee, args, module_symbols
        )
    if not is_indirect and callee.startswith("llvm.usub.sat."):
        return emit_usub_sat_intrinsic_call(
            func, dest, ret_type, callee, args, module_symbols
        )
    if not is_indirect and callee.startswith("llvm.uadd.sat."):
        return emit_uadd_sat_intrinsic_call(
            func, dest, ret_type, callee, args, module_symbols
        )
    if not is_indirect and callee.startswith("llvm.umul.with.overflow."):
        return emit_umul_overflow_intrinsic_call(
            func, dest, ret_type, callee, args, module_symbols
        )
    if not is_indirect and callee.startswith("llvm.uadd.with.overflow."):
        return emit_uadd_overflow_intrinsic_call(
            func, dest, ret_type, callee, args, module_symbols
        )
    if not is_indirect and callee.startswith("llvm.sadd.with.overflow."):
        return emit_sadd_overflow_intrinsic_call(
            func, dest, ret_type, callee, args, module_symbols
        )
    if not is_indirect and callee.startswith("llvm.ssub.with.overflow."):
        return emit_ssub_overflow_intrinsic_call(
            func, dest, ret_type, callee, args, module_symbols
        )
    if not is_indirect and callee.startswith("llvm.smul.with.overflow."):
        return emit_smul_overflow_intrinsic_call(
            func, dest, ret_type, callee, args, module_symbols
        )
    if not is_indirect and callee.startswith("llvm.fshl."):
        return emit_fshl_intrinsic_call(
            func, dest, ret_type, callee, args, module_symbols
        )
    if not is_indirect and callee.startswith("llvm.fshr."):
        return emit_fshr_intrinsic_call(
            func, dest, ret_type, callee, args, module_symbols
        )
    if not is_indirect and callee.startswith("llvm.abs."):
        return emit_abs_intrinsic_call(
            func, dest, ret_type, callee, args, module_symbols
        )
    if not is_indirect and callee.startswith("llvm.copysign."):
        return emit_copysign_intrinsic_call(
            func, dest, ret_type, callee, args, module_symbols
        )
    if not is_indirect and callee.startswith("llvm.fabs."):
        return emit_fabs_intrinsic_call(
            func, dest, ret_type, callee, args, module_symbols
        )
    if not is_indirect and callee.startswith("llvm.floor."):
        return emit_floor_intrinsic_call(
            func, dest, ret_type, callee, args, module_symbols
        )
    if not is_indirect and callee.startswith("llvm.ceil."):
        return emit_ceil_intrinsic_call(
            func, dest, ret_type, callee, args, module_symbols
        )
    if not is_indirect and callee.startswith("llvm.trunc."):
        return emit_trunc_intrinsic_call(
            func, dest, ret_type, callee, args, module_symbols
        )
    if not is_indirect and callee.startswith(("llvm.rint.", "llvm.nearbyint.")):
        return emit_rint_intrinsic_call(
            func, dest, ret_type, callee, args, module_symbols
        )
    if not is_indirect and callee.startswith("llvm.sqrt."):
        return emit_sqrt_intrinsic_call(
            func, dest, ret_type, callee, args, module_symbols
        )
    if not is_indirect and callee.startswith("llvm.is.fpclass."):
        return emit_is_fpclass_intrinsic_call(
            func, dest, ret_type, callee, args, module_symbols
        )
    if not is_indirect and callee.startswith(
        ("llvm.smax.", "llvm.smin.", "llvm.umax.", "llvm.umin.")
    ):
        return emit_minmax_intrinsic_call(
            func, dest, ret_type, callee, args, module_symbols
        )
    if (not is_indirect) and callee.startswith("llvm.va_start"):
        if len(args) != 1:
            raise BackendUnavailable(
                f"self backend llvm.va_start expects 1 arg in {func.name!r}"
            )
        _arg_type, ap_ptr = args[0]
        return emit_vararg_start(func, ap_ptr, module_symbols)
    if (not is_indirect) and callee.startswith("llvm.va_end"):
        return []
    if (not is_indirect) and callee.startswith("llvm.assume"):
        return []
    if (not is_indirect) and callee.startswith(
        ("llvm.lifetime.start", "llvm.lifetime.end")
    ):
        return []

    lines: list[str] = []
    indexed_kernel = get_indexed_function_kernel(func)
    indexed_use_index = 0
    indirect_callee_value_id = -1
    if (
        indexed_use_count >= 0
        and is_indirect
        and is_local_value_ref(callee)
    ):
        if indexed_use_count < 1:
            raise BackendUnavailable(
                f"indexed call use facts omit indirect callee {callee!r}"
            )
        indirect_callee_value_id = indexed_use0
        indexed_use_index = 1
    fixed_args = args[:fixed_arg_count] if is_vararg_call else args
    vararg_args = args[fixed_arg_count:] if is_vararg_call else ()
    fixed_types = [arg_type for arg_type, _value in fixed_args]
    arg_regs = assign_abi_arg_regs(fixed_types)
    stack_arg_entries: list[tuple[int, TypeDesc, str, int]] = []
    indirect_stack_ptr_entries: list[tuple[int, TypeDesc, str, int]] = []
    pending_indirect_stack_literals: list[tuple[int, TypeDesc, str]] = []
    pending_indirect_reg_literals: list[tuple[TypeDesc, str, str]] = []
    stack_offset = 0
    for regs, (arg_type, value) in zip(arg_regs, fixed_args):
        arg_value_id = -1
        if indexed_use_count >= 0 and is_local_value_ref(value):
            if indexed_use_index >= indexed_use_count:
                raise BackendUnavailable(
                    f"indexed call use facts end before argument {value!r}"
                )
            if indexed_use_index == 0:
                arg_value_id = indexed_use0
            elif indexed_use_count == 2:
                arg_value_id = indexed_use_tail
            else:
                overflow_start = -indexed_use_tail - 2
                arg_value_id = (
                    indexed_kernel.instruction_overflow_use_ids.get_unchecked(
                        overflow_start + indexed_use_index - 1
                    )
                )
            indexed_use_index += 1
        if not regs:
            if aggregate_passed_indirect(arg_type):
                ptr_offset = stack_offset
                stack_offset += stack_arg_storage_size(arg_type)
                if _can_spill_aggregate_constant(value):
                    pending_indirect_stack_literals.append(
                        (ptr_offset, arg_type, value)
                    )
                else:
                    indirect_stack_ptr_entries.append(
                        (ptr_offset, arg_type, value, arg_value_id)
                    )
                continue
            stack_arg_entries.append(
                (stack_offset, arg_type, value, arg_value_id)
            )
            stack_offset += stack_arg_storage_size(arg_type)
            continue
        if aggregate_passed_indirect(arg_type):
            if _can_spill_aggregate_constant(value):
                pending_indirect_reg_literals.append((arg_type, value, regs[0]))
                continue
            lines.extend(
                materialize_indirect_aggregate_arg_pointer(
                    func,
                    value,
                    arg_type,
                    regs[0],
                    value_id=arg_value_id,
                )
            )
            continue
        lines.extend(
            materialize_value(
                func,
                value,
                arg_type,
                int(regs[0][1:]),
                module_symbols,
                value_id=arg_value_id,
            )
        )
    for arg_type, value in vararg_args:
        arg_value_id = -1
        if indexed_use_count >= 0 and is_local_value_ref(value):
            if indexed_use_index >= indexed_use_count:
                raise BackendUnavailable(
                    f"indexed call use facts end before variadic argument {value!r}"
                )
            if indexed_use_index == 0:
                arg_value_id = indexed_use0
            elif indexed_use_count == 2:
                arg_value_id = indexed_use_tail
            else:
                overflow_start = -indexed_use_tail - 2
                arg_value_id = (
                    indexed_kernel.instruction_overflow_use_ids.get_unchecked(
                        overflow_start + indexed_use_index - 1
                    )
                )
            indexed_use_index += 1
        stack_arg_entries.append(
            (stack_offset, arg_type, value, arg_value_id)
        )
        stack_offset += variadic_stack_arg_storage_size(arg_type)
    if indexed_use_count >= 0 and indexed_use_index != indexed_use_count:
        raise BackendUnavailable(
            "indexed call use facts do not match local call operands"
        )
    indirect_reg_literal_entries: list[tuple[int, TypeDesc, str, str]] = []
    for arg_type, value, reg in pending_indirect_reg_literals:
        stack_offset = _align_to(stack_offset, arg_type.align)
        temp_offset = stack_offset
        stack_offset += arg_type.slot_size
        indirect_reg_literal_entries.append((temp_offset, arg_type, value, reg))
    indirect_stack_literal_entries: list[tuple[int, int, TypeDesc, str]] = []
    for ptr_offset, arg_type, value in pending_indirect_stack_literals:
        stack_offset = _align_to(stack_offset, arg_type.align)
        temp_offset = stack_offset
        stack_offset += arg_type.slot_size
        indirect_stack_literal_entries.append(
            (ptr_offset, temp_offset, arg_type, value)
        )
    stack_size = _align_to(stack_offset, 16) if stack_offset else 0
    if aggregate_returned_indirect(ret_type):
        dest_offset = (
            -1
            if dest is None
            else (
                func.indexed_kernel.value_slot_offset(dest_value_id)
                if dest_value_id >= 0
                else parsed_function_value_slot_offset(func, dest)
            )
        )
        if dest_offset < 0:
            raise BackendUnavailable(
                f"self backend needs a materialized destination slot for large aggregate return in {func.name!r}"
            )
        lines.extend(emit_slot_base_address_parts(dest_offset, "x8"))
    if stack_size:
        lines.extend(emit_add_offset("sp", "sp", -stack_size, scratch_reg="x15"))
        for temp_offset, arg_type, value, reg in indirect_reg_literal_entries:
            lines.extend(emit_add_offset(reg, "sp", temp_offset, scratch_reg="x15"))
            lines.extend(
                store_large_aggregate_literal_to_address(
                    arg_type,
                    value,
                    reg,
                    data_reg_64="x12",
                    data_reg_32="w12",
                    module_symbols=module_symbols,
                )
            )
        for ptr_offset, temp_offset, arg_type, value in indirect_stack_literal_entries:
            lines.extend(emit_add_offset("x12", "sp", temp_offset, scratch_reg="x15"))
            lines.extend(
                store_large_aggregate_literal_to_address(
                    arg_type,
                    value,
                    "x12",
                    data_reg_64="x13",
                    data_reg_32="w13",
                    module_symbols=module_symbols,
                )
            )
            if ptr_offset:
                lines.extend(
                    emit_add_offset("x14", "sp", ptr_offset, scratch_reg="x15")
                )
                lines.append(emitted_memory_instruction_line("str", "x12", "x14"))
            else:
                lines.append(emitted_memory_instruction_line("str", "x12", "sp"))
        for ptr_offset, arg_type, value, value_id in indirect_stack_ptr_entries:
            lines.extend(
                materialize_indirect_aggregate_arg_pointer(
                    func,
                    value,
                    arg_type,
                    "x12",
                    value_id=value_id,
                )
            )
            if ptr_offset:
                lines.extend(
                    emit_add_offset("x14", "sp", ptr_offset, scratch_reg="x15")
                )
                lines.append(emitted_memory_instruction_line("str", "x12", "x14"))
            else:
                lines.append(emitted_memory_instruction_line("str", "x12", "sp"))
        for offset, arg_type, value, value_id in stack_arg_entries:
            lines.extend(
                emit_vararg_stack_arg(
                    func,
                    arg_type,
                    value,
                    offset,
                    module_symbols,
                    value_id=value_id,
                )
            )
    if is_indirect:
        lines.extend(
            materialize_pointer(
                func,
                callee,
                12,
                module_symbols,
                value_id=indirect_callee_value_id,
            )
        )
        lines.append("  blr x12")
    else:
        lines.append(
            emitted_direct_call_line(
                asm_symbol_prevalidated(callee, module_symbols)
            )
        )
    if stack_size:
        lines.extend(emit_add_offset("sp", "sp", stack_size, scratch_reg="x15"))
    dest_offset = (
        -1
        if dest is None
        else (
            func.indexed_kernel.value_slot_offset(dest_value_id)
            if dest_value_id >= 0
            else parsed_function_value_slot_offset(func, dest)
        )
    )
    if dest_offset >= 0 and not aggregate_returned_indirect(ret_type):
        if func.indexed_slot_projection:
            slot_id = (
                parsed_function_value_slot_id(func, dest)
                if dest_value_id < 0
                else func.indexed_kernel.value_slot_id(dest_value_id)
            )
            if slot_id < 0:
                raise BackendUnavailable(
                    f"indexed call result slot is missing for {dest!r}"
                )
            kernel = func.indexed_kernel
            lines.extend(
                store_value_regs_to_slot_parts(
                    kernel.slot_offset(slot_id),
                    kernel.type_desc(kernel.slot_type_id(slot_id)),
                    0,
                )
            )
        else:
            lines.extend(store_value_regs_to_value_slot(func, dest, 0))
    return lines
