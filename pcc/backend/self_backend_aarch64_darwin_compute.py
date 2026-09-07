from __future__ import annotations

from . import BackendUnavailable
from .self_backend_aarch64_darwin_abi import (
    aggregate_fits_reg_abi,
    reg_name,
    reg_name_indexed,
)
from .self_backend_aarch64_darwin_addr import (
    emit_gep_offset,
    emit_gep_offset_indexed,
)
from .self_backend_aarch64_darwin_calls import (
    emit_call_instruction,
    emit_call_instruction_indexed,
    emit_va_arg,
)
from .self_backend_aarch64_darwin_materialize import (
    copy_large_aggregate_value_to_slot,
    copy_large_aggregate_value_to_value_slot,
    materialize_aggregate_storage_address,
    materialize_pointer,
    materialize_scalar_value_indexed,
    materialize_value,
    store_large_aggregate_literal_to_address,
)
from .self_backend_aarch64_darwin_mem import (
    emitted_addsub_immediate_line,
    emitted_compare_immediate_line,
    emitted_compare_register_line,
    emitted_cset_line,
    emitted_memory_instruction_line,
)
from .self_backend_aarch64_darwin_ops import (
    aarch64_cc,
    emit_aggregate_bitwise_binop,
    emit_binop,
    emit_binop_indexed,
    emit_cast,
    emit_cast_indexed,
    emit_fbinop,
    emit_fcmp_result,
    sign_extend_int_reg,
    sign_extend_int_reg_indexed,
)
from .self_backend_aarch64_darwin_regs import emit_add_offset, emit_const_to_reg
from .self_backend_aarch64_darwin_regalloc import (
    allocated_scalar_register_indexed,
    commit_allocated_scalar_result,
    commit_allocated_scalar_result_indexed,
)
from .self_backend_aarch64_darwin_slots import (
    copy_address_to_address,
    copy_address_to_slot,
    copy_address_to_value_slot,
    emit_slot_base_address,
    emit_slot_base_address_parts,
    emit_value_slot_base_address,
    load_value_from_address,
    store_reg_to_slot,
    store_reg_to_slot_parts,
    store_reg_to_value_slot,
    store_value_to_address,
    store_value_regs_to_slot,
    store_value_regs_to_value_slot,
    zero_address,
    zero_slot,
    zero_value_slot,
)
from .self_backend_ir import (
    I1,
    PARSED_INSTRUCTION_KIND_BINOP,
    PARSED_INSTRUCTION_KIND_CALL,
    PARSED_INSTRUCTION_KIND_CAST,
    PARSED_INSTRUCTION_KIND_EXTRACTELEMENT,
    PARSED_INSTRUCTION_KIND_EXTRACTVALUE,
    PARSED_INSTRUCTION_KIND_FBINOP,
    PARSED_INSTRUCTION_KIND_FCMP,
    PARSED_INSTRUCTION_KIND_FNEG,
    PARSED_INSTRUCTION_KIND_FREEZE,
    PARSED_INSTRUCTION_KIND_GEP,
    PARSED_INSTRUCTION_KIND_ICMP,
    PARSED_INSTRUCTION_KIND_INSERTELEMENT,
    PARSED_INSTRUCTION_KIND_INSERTVALUE,
    PARSED_INSTRUCTION_KIND_SELECT,
    PARSED_INSTRUCTION_KIND_SHUFFLEVECTOR,
    PARSED_INSTRUCTION_KIND_VA_ARG,
    ParsedFunction,
    TypeDesc,
    parsed_function_has_alloca_slot,
    parsed_function_has_value_slot,
    parsed_function_alloca_slot_offset,
    parsed_function_alloca_slot_type,
    parsed_function_value_slot_id,
    parsed_function_value_slot_offset,
    parsed_function_value_slot_type,
    _PARSED_INSTRUCTION_KIND_IDS,
)
from .self_backend_module_symbols import PreparedModuleSymbols
from .self_backend_parse import (
    aggregate_literal_to_bytes,
    const_int_from_value,
    decode_value_token,
    is_aggregate_literal_value,
    split_top_level,
    strip_typed_initializer,
)
from .self_backend_target_passes import (
    aarch64_madd_fusion_for_product,
    aarch64_madd_fusion_for_result,
)
from .self_backend_value_arena import CompilerInt4
from .self_backend_kernel import (
    TYPE_KIND_FP,
    TYPE_KIND_INT,
    TYPE_KIND_PTR,
    IndexedFunctionKernel,
)


def _commit_or_spill_scalar_result(
    func: ParsedFunction,
    dest: str,
    value_type: TypeDesc,
    source_reg: str,
    dest_value_id: int = -1,
) -> list[str]:
    allocated_lines = commit_allocated_scalar_result(
        func, dest, value_type, source_reg
    )
    if allocated_lines is not None:
        return allocated_lines
    if func.indexed_slot_projection:
        slot_id = (
            parsed_function_value_slot_id(func, dest)
            if dest_value_id < 0
            else func.indexed_kernel.value_slot_id(dest_value_id)
        )
        if slot_id < 0:
            raise BackendUnavailable(
                f"indexed value slot is missing for {dest!r} in {func.name!r}"
            )
        kernel = func.indexed_kernel
        return store_reg_to_slot_parts(
            source_reg,
            kernel.slot_offset(slot_id),
            kernel.type_desc(kernel.slot_type_id(slot_id)),
        )
    return store_reg_to_value_slot(source_reg, func, dest)


def _commit_or_spill_scalar_result_indexed(
    func: ParsedFunction,
    kernel: IndexedFunctionKernel,
    dest_value_id: int,
    type_id: int,
    source_reg: str,
) -> list[str]:
    allocated_lines = commit_allocated_scalar_result_indexed(
        func,
        dest_value_id,
        type_id,
        source_reg,
    )
    if allocated_lines is not None:
        return allocated_lines
    slot_id = kernel.value_slot_id(dest_value_id)
    if slot_id < 0:
        raise BackendUnavailable(
            "indexed value slot is missing for "
            + repr(kernel.value_name(dest_value_id))
            + " in "
            + repr(func.name)
        )
    header: CompilerInt4 = kernel.type_header(type_id)
    if header.first == TYPE_KIND_INT and header.second <= 8:
        op = "sturb"
    elif header.first == TYPE_KIND_INT and header.second <= 16:
        op = "sturh"
    else:
        op = "stur"
    offset = kernel.slot_offset(slot_id)
    if offset > 255:
        lines = emit_slot_base_address_parts(offset, "x15")
        lines.append(emitted_memory_instruction_line(op, source_reg, "x15"))
        return lines
    return [
        emitted_memory_instruction_line(op, source_reg, "x29", -offset)
    ]


def _vector_lane_stride(vector_type: TypeDesc) -> tuple[TypeDesc, int]:
    if not vector_type.is_array or vector_type.elem is None:
        raise BackendUnavailable(
            f"self backend vector lowering currently expects array vector lanes, got {vector_type.describe()}"
        )
    lane_type = vector_type.elem
    # Pointer-lane vectors are not arithmetic vectors for the self backend, but
    # they are valid aggregate values in LLVM IR.  Lua/NumPy-shaped C lowering
    # uses forms such as [2 x void*] for table/function dispatch data.  Treat
    # each lane as an 8-byte scalar for extract/insert/shuffle/select and
    # slot/memory copying.  Arithmetic/reduction intrinsics are still guarded by
    # their individual callers and remain int/fp-only.
    if not (lane_type.is_int or lane_type.is_fp or lane_type.is_ptr):
        raise BackendUnavailable(
            f"self backend vector lowering currently expects integer/fp/ptr vector lanes, got {vector_type.describe()}"
        )
    return lane_type, lane_type.slot_size


def _emit_vector_storage_address(
    func: ParsedFunction,
    value: str,
    value_type,
    reg: str,
    *,
    scratch_reg: str,
) -> list[str]:
    if parsed_function_has_value_slot(func, value):
        return emit_add_offset(
            reg,
            "x29",
            -parsed_function_value_slot_offset(func, value),
            scratch_reg=scratch_reg,
        )
    if (
        parsed_function_has_alloca_slot(func, value)
        and parsed_function_alloca_slot_type(func, value).describe()
        == value_type.describe()
    ):
        return emit_add_offset(
            reg,
            "x29",
            -parsed_function_alloca_slot_offset(func, value),
            scratch_reg=scratch_reg,
        )
    return materialize_aggregate_storage_address(func, value, value_type, reg)


def _emit_slot_base_address_nonclobbering(
    offset: int, reg: str, *, scratch_reg: str
) -> list[str]:
    return emit_add_offset(reg, "x29", -offset, scratch_reg=scratch_reg)


def _emit_vector_int_binop(
    func: ParsedFunction,
    op: str,
    dest: str,
    value_type,
    lhs: str,
    rhs: str,
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    lane_type, lane_stride = _vector_lane_stride(value_type)
    dest_offset = parsed_function_value_slot_offset(func, dest)
    lines: list[str] = []
    lhs_addr: str | None = None
    rhs_addr: str | None = None
    if parsed_function_has_value_slot(func, lhs) or parsed_function_has_alloca_slot(func, lhs):
        lines.extend(
            _emit_vector_storage_address(
                func, lhs, value_type, "x15", scratch_reg="x14"
            )
        )
        lhs_addr = "x15"
    if parsed_function_has_value_slot(func, rhs) or parsed_function_has_alloca_slot(func, rhs):
        lines.extend(
            _emit_vector_storage_address(
                func, rhs, value_type, "x16", scratch_reg="x14"
            )
        )
        rhs_addr = "x16"
    lines.extend(
        _emit_slot_base_address_nonclobbering(dest_offset, "x17", scratch_reg="x14")
    )
    for lane_index in range(value_type.count):
        dst_addr = "x17"
        if lane_index:
            offset = lane_index * lane_stride
            lines.extend(emit_add_offset("x14", "x17", offset))
            dst_addr = "x14"
        lines.extend(
            _emit_vector_lane_value(
                func,
                lhs,
                value_type,
                lane_type,
                lane_index,
                9,
                lhs_addr,
                module_symbols,
            )
        )
        lines.extend(
            _emit_vector_lane_value(
                func,
                rhs,
                value_type,
                lane_type,
                lane_index,
                10,
                rhs_addr,
                module_symbols,
            )
        )
        lines.extend(emit_binop(op, lane_type))
        lines.extend(store_value_to_address(dst_addr, lane_type, 11))
    return lines


def _emit_vector_lane_value(
    func: ParsedFunction,
    value: str,
    vector_type: TypeDesc,
    lane_type: TypeDesc,
    lane_index: int,
    dest_reg_index: int,
    addr_reg: str | None,
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    lane_reg = reg_name(lane_type, dest_reg_index)
    if addr_reg is not None:
        stride = _vector_lane_stride(vector_type)[1]
        source_addr = addr_reg
        if lane_index:
            temp_reg = "x12" if addr_reg != "x12" else "x13"
            lines = emit_add_offset(temp_reg, addr_reg, lane_index * stride)
            lines.extend(load_value_from_address(temp_reg, lane_type, dest_reg_index))
            return lines
        return load_value_from_address(source_addr, lane_type, dest_reg_index)
    if value == "zeroinitializer":
        return emit_const_to_reg(lane_type, lane_reg, 0)
    const_value = const_int_from_value(value)
    if const_value is not None:
        return emit_const_to_reg(lane_type, lane_reg, const_value)
    if is_aggregate_literal_value(value):
        stride = _vector_lane_stride(vector_type)[1]
        try:
            literal_bytes = aggregate_literal_to_bytes(vector_type, value)
            lane_bytes = literal_bytes[
                lane_index * stride : lane_index * stride + lane_type.slot_size
            ]
            lane_value = int.from_bytes(lane_bytes, "little")
            return emit_const_to_reg(lane_type, lane_reg, lane_value)
        except BackendUnavailable:
            lane_value = _aggregate_literal_lane_value(vector_type, value, lane_index)
            return materialize_value(
                func,
                decode_value_token(lane_value),
                lane_type,
                dest_reg_index,
                module_symbols,
            )
    raise BackendUnavailable(
        f"self backend cannot materialize vector lane value in {func.name!r}: {value}"
    )


def _aggregate_literal_lane_value(vector_type, value: str, lane_index: int) -> str:
    text = value.strip()
    if text.startswith("<") and text.endswith(">"):
        text = "[" + text[1:-1].strip() + "]"
    if not (text.startswith("[") and text.endswith("]")):
        raise BackendUnavailable(
            f"self backend expected vector literal for {vector_type.describe()}, got {value!r}"
        )
    items = split_top_level(text[1:-1].strip())
    if lane_index < 0 or lane_index >= len(items):
        raise BackendUnavailable(
            f"self backend vector literal lane {lane_index} out of range for {vector_type.describe()}: {value!r}"
        )
    return strip_typed_initializer(items[lane_index])


def _emit_insertelement(
    func: ParsedFunction,
    dest: str,
    vector_type,
    vector_value: str,
    elem_type,
    elem_value: str,
    index_value: str,
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    lane_type, lane_stride = _vector_lane_stride(vector_type)
    if elem_type.describe() != lane_type.describe():
        raise BackendUnavailable(
            f"self backend insertelement expects matching lane type in {func.name!r}: "
            f"{elem_type.describe()} into {vector_type.describe()}"
        )
    if not parsed_function_has_value_slot(func, dest):
        return []
    lane_index = int(index_value)
    if lane_index < 0 or lane_index >= vector_type.count:
        raise BackendUnavailable(
            f"self backend insertelement lane index out of range in {func.name!r}: {index_value}"
        )
    if vector_value in {"poison", "zeroinitializer"}:
        lines = zero_value_slot(func, dest)
    else:
        lines = copy_large_aggregate_value_to_value_slot(
            func, vector_value, vector_type, dest, module_symbols=module_symbols
        )
    # Materialize the new element BEFORE computing the dest address.
    # ``materialize_value`` may pick ``x15`` as a scratch GPR via
    # ``pick_scratch_gpr`` (e.g. when reading an SSA slot whose offset
    # exceeds 255 in a large frame like lua_newstate's). If we set
    # ``x15`` to the dest address first, that scratch use clobbers it
    # and the lane store ends up writing back into the *source* slot,
    # silently dropping the inserted element.
    lines.extend(materialize_value(func, elem_value, elem_type, 9, module_symbols))
    lines.extend(emit_value_slot_base_address(func, dest, "x15"))
    if lane_index:
        lines.extend(emit_add_offset("x15", "x15", lane_index * lane_stride))
    lines.extend(store_value_to_address("x15", elem_type, 9))
    return lines


def _emit_extractelement(
    func: ParsedFunction,
    dest: str,
    vector_type,
    vector_value: str,
    index_value: str,
    elem_type,
) -> list[str]:
    lane_type, lane_stride = _vector_lane_stride(vector_type)
    if elem_type.describe() != lane_type.describe():
        raise BackendUnavailable(
            f"self backend extractelement expects result type to match lane type in {func.name!r}: "
            f"{elem_type.describe()} from {vector_type.describe()}"
        )
    lane_index = int(index_value)
    if lane_index < 0 or lane_index >= vector_type.count:
        raise BackendUnavailable(
            f"self backend extractelement lane index out of range in {func.name!r}: {index_value}"
        )
    lines = materialize_aggregate_storage_address(
        func, vector_value, vector_type, "x15"
    )
    if lane_index:
        lines.extend(emit_add_offset("x15", "x15", lane_index * lane_stride))
    lines.extend(load_value_from_address("x15", elem_type, 10))
    lines.extend(store_reg_to_value_slot(reg_name(elem_type, 10), func, dest))
    return lines


def _emit_shufflevector(
    func: ParsedFunction,
    dest: str,
    vector_type: TypeDesc,
    lhs: str,
    rhs: str,
    mask_type: TypeDesc,
    mask_value: str,
) -> list[str]:
    lane_type, lane_stride = _vector_lane_stride(vector_type)
    source_type = func.value_types.get(lhs)
    if source_type is None or not source_type.is_array or source_type.elem is None:
        raise BackendUnavailable(
            f"self backend shufflevector expects materialized vector lhs in {func.name!r}: {lhs}"
        )
    source_elem: TypeDesc = source_type.elem
    if source_elem.describe() != lane_type.describe():
        raise BackendUnavailable(
            f"self backend shufflevector lane type mismatch in {func.name!r}: {source_type.describe()} -> {vector_type.describe()}"
        )
    rhs_type = None
    rhs_addr: str | None = None
    if rhs != "poison":
        rhs_type = func.value_types.get(rhs)
        if rhs_type is None or not rhs_type.is_array or rhs_type.elem is None:
            raise BackendUnavailable(
                f"self backend shufflevector expects materialized vector rhs in {func.name!r}: {rhs}"
            )
        rhs_elem: TypeDesc = rhs_type.elem
        if (
            rhs_elem.describe() != lane_type.describe()
            or rhs_type.count != source_type.count
        ):
            raise BackendUnavailable(
                f"self backend shufflevector rhs lane type/count mismatch in {func.name!r}: {rhs_type.describe()}"
            )
        if not parsed_function_has_value_slot(func, rhs) and not parsed_function_has_alloca_slot(func, rhs):
            raise BackendUnavailable(
                f"self backend shufflevector rhs form not translated yet in {func.name!r}: {rhs}"
            )
        rhs_addr = "x16"
    if not mask_type.is_array or mask_type.count != vector_type.count:
        raise BackendUnavailable(
            f"self backend shufflevector currently expects result lane count to match mask lane count in {func.name!r}"
        )
    if not parsed_function_has_value_slot(func, dest):
        return []
    dest_offset = parsed_function_value_slot_offset(func, dest)
    lines = _emit_vector_storage_address(
        func, lhs, source_type, "x15", scratch_reg="x14"
    )
    if rhs_addr is not None:
        assert rhs_type is not None
        lines.extend(
            _emit_vector_storage_address(
                func, rhs, rhs_type, rhs_addr, scratch_reg="x14"
            )
        )
    lines.extend(
        _emit_slot_base_address_nonclobbering(dest_offset, "x17", scratch_reg="x14")
    )
    if mask_value == "zeroinitializer":
        mask_indices = [0] * vector_type.count
    elif is_aggregate_literal_value(mask_value):
        mask_text = mask_value
        if mask_text.startswith("<") and mask_text.endswith(">"):
            mask_text = mask_text[1:-1].strip()
        elif mask_text.startswith("[") and mask_text.endswith("]"):
            mask_text = mask_text[1:-1].strip()
        else:
            raise BackendUnavailable(
                f"self backend shufflevector mask literal not translated yet in {func.name!r}: {mask_value}"
            )
        mask_indices = []
        for item in split_top_level(mask_text):
            index = const_int_from_value(strip_typed_initializer(item))
            if index is None:
                raise BackendUnavailable(
                    f"self backend shufflevector mask lane is not constant in {func.name!r}: {mask_value}"
                )
            mask_indices.append(index)
    else:
        raise BackendUnavailable(
            f"self backend shufflevector mask form not translated yet in {func.name!r}: {mask_value}"
        )
    for lane_index, selected_lane in enumerate(mask_indices):
        if selected_lane < 0:
            raise BackendUnavailable(
                f"self backend shufflevector negative mask lane not translated yet in {func.name!r}: {mask_value}"
            )
        if selected_lane < source_type.count:
            source_addr = "x15"
            source_lane = selected_lane
        elif rhs == "poison":
            raise BackendUnavailable(
                f"self backend shufflevector mask lane out of range for poison rhs in {func.name!r}: {mask_value}"
            )
        else:
            assert rhs_type is not None
            source_addr = rhs_addr
            source_lane = selected_lane - source_type.count
            if source_lane >= rhs_type.count:
                raise BackendUnavailable(
                    f"self backend shufflevector mask lane out of range in {func.name!r}: {mask_value}"
                )
        dst_addr = "x17"
        if lane_index:
            lines.extend(emit_add_offset("x14", "x17", lane_index * lane_stride))
            dst_addr = "x14"
        assert source_addr is not None
        src_addr = source_addr
        if source_lane:
            lines.extend(emit_add_offset("x12", source_addr, source_lane * lane_stride))
            src_addr = "x12"
        lines.extend(load_value_from_address(src_addr, lane_type, 9))
        lines.extend(store_value_to_address(dst_addr, lane_type, 9))
    return lines


def _emit_vector_cast(
    func: ParsedFunction,
    op: str,
    dest: str,
    src_type,
    value: str,
    dst_type,
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    src_lane_type, src_lane_stride = _vector_lane_stride(src_type)
    dst_lane_type, dst_lane_stride = _vector_lane_stride(dst_type)
    if src_type.count != dst_type.count:
        raise BackendUnavailable(
            f"self backend vector cast currently requires same lane count in {func.name!r}: "
            f"{src_type.describe()} -> {dst_type.describe()}"
        )
    dest_offset = parsed_function_value_slot_offset(func, dest)
    lines = _emit_vector_storage_address(
        func, value, src_type, "x15", scratch_reg="x14"
    )
    lines.extend(
        _emit_slot_base_address_nonclobbering(dest_offset, "x17", scratch_reg="x14")
    )
    for lane_index in range(src_type.count):
        src_addr = "x15"
        dst_addr = "x17"
        if lane_index:
            lines.extend(emit_add_offset("x12", "x15", lane_index * src_lane_stride))
            lines.extend(emit_add_offset("x14", "x17", lane_index * dst_lane_stride))
            src_addr = "x12"
            dst_addr = "x14"
        lines.extend(load_value_from_address(src_addr, src_lane_type, 9))
        lines.extend(emit_cast(op, src_lane_type, dst_lane_type))
        lines.extend(store_value_to_address(dst_addr, dst_lane_type, 10))
    return lines


def _emit_vector_select(
    func: ParsedFunction,
    dest: str,
    value_type,
    cond: str,
    true_value: str,
    false_value: str,
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    lane_type, lane_stride = _vector_lane_stride(value_type)
    cond_type = func.value_types.get(cond)
    if (
        cond_type is None
        or not cond_type.is_array
        or cond_type.elem is None
        or not cond_type.elem.is_int
        or cond_type.elem.width != 1
        or cond_type.count != value_type.count
    ):
        raise BackendUnavailable(
            f"self backend vector select expects matching vector-i1 condition in {func.name!r}: {cond}"
        )
    cond_stride = _vector_lane_stride(cond_type)[1]
    dest_offset = parsed_function_value_slot_offset(func, dest)
    lines = _emit_vector_storage_address(
        func, cond, cond_type, "x15", scratch_reg="x12"
    )
    true_addr: str | None = None
    false_addr: str | None = None
    if parsed_function_has_value_slot(func, true_value) or parsed_function_has_alloca_slot(func, true_value):
        lines.extend(
            _emit_vector_storage_address(
                func, true_value, value_type, "x16", scratch_reg="x12"
            )
        )
        true_addr = "x16"
    if parsed_function_has_value_slot(func, false_value) or parsed_function_has_alloca_slot(func, false_value):
        lines.extend(
            _emit_vector_storage_address(
                func, false_value, value_type, "x17", scratch_reg="x12"
            )
        )
        false_addr = "x17"
    lines.extend(
        _emit_slot_base_address_nonclobbering(dest_offset, "x14", scratch_reg="x12")
    )
    for lane_index in range(value_type.count):
        cond_addr = "x15"
        dst_addr = "x14"
        if lane_index:
            lines.extend(emit_add_offset("x11", "x15", lane_index * cond_stride))
            cond_addr = "x11"
            lines.extend(emit_add_offset("x13", "x14", lane_index * lane_stride))
            dst_addr = "x13"
        lines.extend(load_value_from_address(cond_addr, cond_type.elem, 9))
        lines.extend(
            _emit_vector_lane_value(
                func,
                true_value,
                value_type,
                lane_type,
                lane_index,
                10,
                true_addr,
                module_symbols,
            )
        )
        lines.extend(
            _emit_vector_lane_value(
                func,
                false_value,
                value_type,
                lane_type,
                lane_index,
                11,
                false_addr,
                module_symbols,
            )
        )
        lines.append(emitted_compare_immediate_line("w9", 0))
        lines.append(
            f"  csel {reg_name(lane_type, 12)}, {reg_name(lane_type, 10)}, {reg_name(lane_type, 11)}, ne"
        )
        lines.extend(store_value_to_address(dst_addr, lane_type, 12))
    return lines


def _emit_aggregate_select(
    func: ParsedFunction,
    dest: str,
    value_type,
    cond: str,
    true_value: str,
    false_value: str,
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    cond_type = func.value_types.get(cond, I1)
    if not cond_type.is_int:
        raise BackendUnavailable(
            f"self backend aggregate select expects scalar integer condition in {func.name!r}: {cond}"
        )
    lines = materialize_value(func, cond, cond_type, 9, module_symbols)
    lines.extend(
        materialize_aggregate_storage_address(func, true_value, value_type, "x10")
    )
    lines.extend(
        materialize_aggregate_storage_address(func, false_value, value_type, "x11")
    )
    lines.extend(emit_value_slot_base_address(func, dest, "x12"))
    lines.append(emitted_compare_immediate_line("w9", 0))
    lines.append("  csel x13, x10, x11, ne")
    lines.extend(copy_address_to_address("x13", "x12", value_type.slot_size))
    return lines


def _emit_vector_icmp(
    func: ParsedFunction,
    cond: str,
    dest: str,
    value_type,
    lhs: str,
    rhs: str,
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    lane_type, lane_stride = _vector_lane_stride(value_type)
    dest_type = func.value_types.get(dest)
    if (
        dest_type is None
        or not dest_type.is_array
        or dest_type.elem is None
        or not dest_type.elem.is_int
        or dest_type.elem.width != 1
        or dest_type.count != value_type.count
    ):
        raise BackendUnavailable(
            f"self backend vector icmp expects vector-i1 destination in {func.name!r}: {dest}"
        )
    lines: list[str] = []
    lhs_addr: str | None = None
    rhs_addr: str | None = None
    if parsed_function_has_value_slot(func, lhs) or parsed_function_has_alloca_slot(func, lhs):
        lines.extend(
            _emit_vector_storage_address(
                func, lhs, value_type, "x15", scratch_reg="x14"
            )
        )
        lhs_addr = "x15"
    if parsed_function_has_value_slot(func, rhs) or parsed_function_has_alloca_slot(func, rhs):
        lines.extend(
            _emit_vector_storage_address(
                func, rhs, value_type, "x16", scratch_reg="x14"
            )
        )
        rhs_addr = "x16"
    lines.extend(
        _emit_slot_base_address_nonclobbering(
            parsed_function_value_slot_offset(func, dest),
            "x17",
            scratch_reg="x14",
        )
    )
    for lane_index in range(value_type.count):
        dst_addr = "x17"
        if lane_index:
            lines.extend(emit_add_offset("x14", "x17", lane_index))
            dst_addr = "x14"
        lines.extend(
            _emit_vector_lane_value(
                func,
                lhs,
                value_type,
                lane_type,
                lane_index,
                9,
                lhs_addr,
                module_symbols,
            )
        )
        lines.extend(
            _emit_vector_lane_value(
                func,
                rhs,
                value_type,
                lane_type,
                lane_index,
                10,
                rhs_addr,
                module_symbols,
            )
        )
        if cond in {"slt", "sle", "sgt", "sge"}:
            lines.extend(sign_extend_int_reg(lane_type, reg_name(lane_type, 9)))
            lines.extend(sign_extend_int_reg(lane_type, reg_name(lane_type, 10)))
        lines.append(
            emitted_compare_register_line(
                reg_name(lane_type, 9),
                reg_name(lane_type, 10),
            )
        )
        lines.append(emitted_cset_line("w11", aarch64_cc(cond)))
        lines.extend(store_value_to_address(dst_addr, I1, 11))
    return lines


def _emit_vector_fcmp(
    func: ParsedFunction,
    cond: str,
    dest: str,
    value_type,
    lhs: str,
    rhs: str,
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    lane_type, _lane_stride = _vector_lane_stride(value_type)
    if not lane_type.is_fp:
        raise BackendUnavailable(
            f"self backend vector fcmp expects fp vector lanes in {func.name!r}: {value_type.describe()}"
        )
    dest_type = func.value_types.get(dest)
    if (
        dest_type is None
        or not dest_type.is_array
        or dest_type.elem is None
        or not dest_type.elem.is_int
        or dest_type.elem.width != 1
        or dest_type.count != value_type.count
    ):
        raise BackendUnavailable(
            f"self backend vector fcmp expects vector-i1 destination in {func.name!r}: {dest}"
        )
    lines: list[str] = []
    lhs_addr: str | None = None
    rhs_addr: str | None = None
    if parsed_function_has_value_slot(func, lhs) or parsed_function_has_alloca_slot(func, lhs):
        lines.extend(
            _emit_vector_storage_address(
                func, lhs, value_type, "x15", scratch_reg="x14"
            )
        )
        lhs_addr = "x15"
    if parsed_function_has_value_slot(func, rhs) or parsed_function_has_alloca_slot(func, rhs):
        lines.extend(
            _emit_vector_storage_address(
                func, rhs, value_type, "x16", scratch_reg="x14"
            )
        )
        rhs_addr = "x16"
    lines.extend(
        _emit_slot_base_address_nonclobbering(
            parsed_function_value_slot_offset(func, dest),
            "x17",
            scratch_reg="x14",
        )
    )
    for lane_index in range(value_type.count):
        dst_addr = "x17"
        if lane_index:
            lines.extend(emit_add_offset("x14", "x17", lane_index))
            dst_addr = "x14"
        lines.extend(
            _emit_vector_lane_value(
                func,
                lhs,
                value_type,
                lane_type,
                lane_index,
                9,
                lhs_addr,
                module_symbols,
            )
        )
        lines.extend(
            _emit_vector_lane_value(
                func,
                rhs,
                value_type,
                lane_type,
                lane_index,
                10,
                rhs_addr,
                module_symbols,
            )
        )
        lines.append(f"  fcmp {reg_name(lane_type, 9)}, {reg_name(lane_type, 10)}")
        lines.extend(emit_fcmp_result(cond))
        lines.extend(store_value_to_address(dst_addr, I1, 11))
    return lines


def _emit_insertvalue(
    func: ParsedFunction,
    dest: str,
    aggregate_type,
    aggregate_value: str,
    elem_type,
    elem_value: str,
    offset: int,
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    if aggregate_value in {"zeroinitializer", "poison", "undef"}:
        lines = zero_value_slot(func, dest)
    elif aggregate_fits_reg_abi(aggregate_type):
        lines = materialize_value(
            func, aggregate_value, aggregate_type, 9, module_symbols
        )
        lines.extend(store_value_regs_to_value_slot(func, dest, 9))
    elif is_aggregate_literal_value(aggregate_value):
        lines = emit_value_slot_base_address(func, dest, "x15")
        lines.extend(
            store_large_aggregate_literal_to_address(
                aggregate_type,
                aggregate_value,
                "x15",
                module_symbols=module_symbols,
            )
        )
    else:
        lines = copy_large_aggregate_value_to_value_slot(
            func,
            aggregate_value,
            aggregate_type,
            dest,
            module_symbols=module_symbols,
        )
    if elem_type.is_array or elem_type.is_struct:
        lines.extend(emit_value_slot_base_address(func, dest, "x15"))
        if offset:
            lines.extend(emit_add_offset("x15", "x15", offset))
        if elem_value in {"zeroinitializer", "poison", "undef"}:
            lines.extend(zero_address("x15", elem_type.slot_size))
            return lines
        if is_aggregate_literal_value(elem_value):
            lines.extend(
                store_large_aggregate_literal_to_address(
                    elem_type,
                    elem_value,
                    "x15",
                    module_symbols=module_symbols,
                )
            )
            return lines
        lines.extend(
            materialize_aggregate_storage_address(func, elem_value, elem_type, "x16")
        )
        lines.extend(copy_address_to_address("x16", "x15", elem_type.slot_size))
        return lines
    lines.extend(materialize_value(func, elem_value, elem_type, 9, module_symbols))
    lines.extend(emit_value_slot_base_address(func, dest, "x15"))
    if offset:
        lines.extend(emit_add_offset("x15", "x15", offset))
    lines.extend(store_value_to_address("x15", elem_type, 9))
    return lines


def emit_compute_instruction_by_id(
    func: ParsedFunction,
    kind_id: int,
    data: tuple,
    module_symbols: PreparedModuleSymbols,
    *,
    indexed_kernel: IndexedFunctionKernel | None = None,
    block_id: int = -1,
    instruction_index: int = -1,
    indexed_dest_id: int = -1,
    indexed_use_count: int = -1,
    indexed_use0: int = -1,
    indexed_use_tail: int = -1,
) -> list[str] | None:
    if (
        indexed_dest_id < 0
        and indexed_kernel is not None
        and block_id >= 0
        and instruction_index >= 0
    ):
        indexed_dest_id = indexed_kernel.defined_value_id(
            block_id, instruction_index
        )
    indexed_dest_has_slot = bool(
        indexed_kernel is not None
        and indexed_dest_id >= 0
        and indexed_kernel.value_slot_id(indexed_dest_id) >= 0
    )
    if kind_id == PARSED_INSTRUCTION_KIND_BINOP:
        if indexed_kernel is not None:
            binop_record: CompilerInt4 = indexed_kernel.instruction_record(data)
            op = indexed_kernel.call_texts[binop_record.first]
            dest = indexed_kernel.value_name(indexed_dest_id)
            value_type_id = binop_record.second
            value_type_header: CompilerInt4 = indexed_kernel.type_header(
                value_type_id
            )
            lhs = (
                indexed_kernel.value_name(binop_record.third)
                if binop_record.third >= 0
                else indexed_kernel.call_texts[-binop_record.third - 1]
            )
            rhs = (
                indexed_kernel.value_name(binop_record.fourth)
                if binop_record.fourth >= 0
                else indexed_kernel.call_texts[-binop_record.fourth - 1]
            )
            if value_type_header.first == TYPE_KIND_INT:
                if (
                    op == "mul"
                    and value_type_header.second == 64
                    and aarch64_madd_fusion_for_product(func, dest) is not None
                ):
                    return []
                if not indexed_dest_has_slot:
                    return []
                fusion = None
                if value_type_header.second == 64:
                    fusion = aarch64_madd_fusion_for_result(func, dest)
                if fusion is not None:
                    lines = materialize_scalar_value_indexed(
                        func,
                        indexed_kernel,
                        fusion.mul_lhs,
                        value_type_id,
                        9,
                        module_symbols,
                        value_id=indexed_kernel.value_id(fusion.mul_lhs),
                    )
                    lines.extend(
                        materialize_scalar_value_indexed(
                            func,
                            indexed_kernel,
                            fusion.mul_rhs,
                            value_type_id,
                            10,
                            module_symbols,
                            value_id=indexed_kernel.value_id(fusion.mul_rhs),
                        )
                    )
                    lines.extend(
                        materialize_scalar_value_indexed(
                            func,
                            indexed_kernel,
                            fusion.accumulator,
                            value_type_id,
                            12,
                            module_symbols,
                            value_id=indexed_kernel.value_id(
                                fusion.accumulator
                            ),
                        )
                    )
                    lines.append(
                        f"  {fusion.mnemonic} x11, x9, x10, x12"
                    )
                    lines.extend(
                        _commit_or_spill_scalar_result_indexed(
                            func,
                            indexed_kernel,
                            indexed_dest_id,
                            value_type_id,
                            "x11",
                        )
                    )
                    return lines
                lhs_register = 9
                rhs_register = 10
                result_register = 11
                select_registers = value_type_header.second in (32, 64) and (
                    binop_record.third >= 0 or lhs.lstrip("-").isdigit()
                    or lhs in ("undef", "poison", "zeroinitializer")
                ) and (
                    binop_record.fourth >= 0 or rhs.lstrip("-").isdigit()
                    or rhs in ("undef", "poison", "zeroinitializer")
                )
                if select_registers:
                    selected = allocated_scalar_register_indexed(
                        indexed_kernel, binop_record.third, value_type_id,
                    )
                    if selected >= 0:
                        lhs_register = selected
                    selected = allocated_scalar_register_indexed(
                        indexed_kernel, binop_record.fourth, value_type_id,
                    )
                    if selected >= 0:
                        rhs_register = selected
                    selected = allocated_scalar_register_indexed(
                        indexed_kernel, indexed_dest_id, value_type_id,
                    )
                    if selected >= 0:
                        result_register = selected
                lines = materialize_scalar_value_indexed(
                    func,
                    indexed_kernel,
                    lhs,
                    value_type_id,
                    lhs_register,
                    module_symbols,
                    value_id=binop_record.third,
                )
                if select_registers and op in ("add", "sub") and binop_record.fourth < 0 and len(rhs) <= 5:
                    immediate = const_int_from_value(rhs)
                    if immediate is not None and -4095 <= immediate <= 4095:
                        mnemonic = op
                        if immediate < 0:
                            mnemonic = "sub" if op == "add" else "add"
                            immediate = -immediate
                        result_name = reg_name_indexed(indexed_kernel, value_type_id, result_register)
                        lines.append(emitted_addsub_immediate_line(
                            mnemonic, result_name,
                            reg_name_indexed(indexed_kernel, value_type_id, lhs_register),
                            immediate,
                        ))
                        lines.extend(_commit_or_spill_scalar_result_indexed(
                            func, indexed_kernel, indexed_dest_id, value_type_id, result_name,
                        ))
                        return lines
                lines.extend(
                    materialize_scalar_value_indexed(
                        func,
                        indexed_kernel,
                        rhs,
                        value_type_id,
                        rhs_register,
                        module_symbols,
                        value_id=binop_record.fourth,
                    )
                )
                if op in {"sdiv", "srem", "ashr"}:
                    lines.extend(
                        sign_extend_int_reg_indexed(
                            indexed_kernel,
                            value_type_id,
                            reg_name_indexed(indexed_kernel, value_type_id, lhs_register),
                        )
                    )
                    lines.extend(
                        sign_extend_int_reg_indexed(
                            indexed_kernel,
                            value_type_id,
                            reg_name_indexed(indexed_kernel, value_type_id, rhs_register),
                        )
                    )
                lines.extend(
                    emit_binop_indexed(
                        indexed_kernel, op, value_type_id,
                        lhs_register, rhs_register, result_register,
                    )
                )
                lines.extend(
                    _commit_or_spill_scalar_result_indexed(
                        func,
                        indexed_kernel,
                        indexed_dest_id,
                        value_type_id,
                        reg_name_indexed(indexed_kernel, value_type_id, result_register),
                    )
                )
                return lines
            value_type = indexed_kernel.type_desc(value_type_id)
        else:
            op, dest, value_type, lhs, rhs = data
        if (
            op == "mul"
            and value_type.is_int
            and value_type.width == 64
            and aarch64_madd_fusion_for_product(func, dest) is not None
        ):
            # The single consumer emits the multiply and add/sub together.
            return []
        if not (indexed_dest_has_slot if indexed_kernel is not None else parsed_function_has_value_slot(func, dest)):
            return []
        fusion = None
        if value_type.is_int and value_type.width == 64:
            fusion = aarch64_madd_fusion_for_result(func, dest)
        if fusion is not None:
            lines = materialize_value(
                func, fusion.mul_lhs, value_type, 9, module_symbols
            )
            lines.extend(
                materialize_value(
                    func, fusion.mul_rhs, value_type, 10, module_symbols
                )
            )
            lines.extend(
                materialize_value(
                    func, fusion.accumulator, value_type, 12, module_symbols
                )
            )
            lines.append(f"  {fusion.mnemonic} x11, x9, x10, x12")
            lines.extend(
                _commit_or_spill_scalar_result(
                    func, dest, value_type, "x11", indexed_dest_id
                )
            )
            return lines
        if (
            value_type.is_array
            and value_type.elem is not None
            and value_type.elem.is_int
        ):
            return _emit_vector_int_binop(
                func, op, dest, value_type, lhs, rhs, module_symbols
            )
        if value_type.is_array or value_type.is_struct:
            lines = materialize_value(func, lhs, value_type, 9, module_symbols)
            lines.extend(materialize_value(func, rhs, value_type, 11, module_symbols))
            lines.extend(
                emit_aggregate_bitwise_binop(
                    op,
                    value_type,
                    lhs_start=9,
                    rhs_start=11,
                    dest_start=13,
                )
            )
            lines.extend(store_value_regs_to_value_slot(func, dest, 13))
            return lines
        lines = materialize_value(func, lhs, value_type, 9, module_symbols)
        lines.extend(materialize_value(func, rhs, value_type, 10, module_symbols))
        if value_type.is_int and op in {"sdiv", "srem", "ashr"}:
            lines.extend(sign_extend_int_reg(value_type, reg_name(value_type, 9)))
            lines.extend(sign_extend_int_reg(value_type, reg_name(value_type, 10)))
        lines.extend(emit_binop(op, value_type))
        lines.extend(
            _commit_or_spill_scalar_result(
                func,
                dest,
                value_type,
                reg_name(value_type, 11),
                indexed_dest_id,
            )
        )
        return lines

    if kind_id == PARSED_INSTRUCTION_KIND_FBINOP:
        op, dest, value_type, lhs, rhs = data
        if not (indexed_dest_has_slot if indexed_kernel is not None else parsed_function_has_value_slot(func, dest)):
            return []
        lines = materialize_value(func, lhs, value_type, 9, module_symbols)
        lines.extend(materialize_value(func, rhs, value_type, 10, module_symbols))
        lines.extend(emit_fbinop(op, value_type))
        lines.extend(
            store_reg_to_value_slot(reg_name(value_type, 11), func, dest)
        )
        return lines

    if kind_id == PARSED_INSTRUCTION_KIND_FNEG:
        dest, value_type, value = data
        if not (indexed_dest_has_slot if indexed_kernel is not None else parsed_function_has_value_slot(func, dest)):
            return []
        lines = materialize_value(func, value, value_type, 9, module_symbols)
        lines.append(f"  fneg {reg_name(value_type, 11)}, {reg_name(value_type, 9)}")
        lines.extend(
            store_reg_to_value_slot(reg_name(value_type, 11), func, dest)
        )
        return lines

    if kind_id == PARSED_INSTRUCTION_KIND_ICMP:
        if indexed_kernel is not None:
            icmp_record: CompilerInt4 = indexed_kernel.instruction_record(data)
            cond = indexed_kernel.call_texts[icmp_record.first]
            dest = indexed_kernel.value_name(indexed_dest_id)
            value_type_id = icmp_record.second
            value_type_header: CompilerInt4 = indexed_kernel.type_header(
                value_type_id
            )
            lhs = (
                indexed_kernel.value_name(icmp_record.third)
                if icmp_record.third >= 0
                else indexed_kernel.call_texts[-icmp_record.third - 1]
            )
            rhs = (
                indexed_kernel.value_name(icmp_record.fourth)
                if icmp_record.fourth >= 0
                else indexed_kernel.call_texts[-icmp_record.fourth - 1]
            )
            if value_type_header.first in (TYPE_KIND_INT, TYPE_KIND_PTR):
                if not indexed_dest_has_slot:
                    return []
                lines = materialize_scalar_value_indexed(
                    func,
                    indexed_kernel,
                    lhs,
                    value_type_id,
                    9,
                    module_symbols,
                    value_id=icmp_record.third,
                )
                lines.extend(
                    materialize_scalar_value_indexed(
                        func,
                        indexed_kernel,
                        rhs,
                        value_type_id,
                        10,
                        module_symbols,
                        value_id=icmp_record.fourth,
                    )
                )
                if (
                    value_type_header.first == TYPE_KIND_INT
                    and cond in {"slt", "sle", "sgt", "sge"}
                ):
                    lines.extend(
                        sign_extend_int_reg_indexed(
                            indexed_kernel,
                            value_type_id,
                            reg_name_indexed(indexed_kernel, value_type_id, 9),
                        )
                    )
                    lines.extend(
                        sign_extend_int_reg_indexed(
                            indexed_kernel,
                            value_type_id,
                            reg_name_indexed(indexed_kernel, value_type_id, 10),
                        )
                    )
                lines.append(
                    emitted_compare_register_line(
                        reg_name_indexed(indexed_kernel, value_type_id, 9),
                        reg_name_indexed(indexed_kernel, value_type_id, 10),
                    )
                )
                lines.append(emitted_cset_line("w11", aarch64_cc(cond)))
                result_type_id = indexed_kernel.value_type_id(indexed_dest_id)
                lines.extend(
                    _commit_or_spill_scalar_result_indexed(
                        func,
                        indexed_kernel,
                        indexed_dest_id,
                        result_type_id,
                        "w11",
                    )
                )
                return lines
            value_type = indexed_kernel.type_desc(value_type_id)
        else:
            cond, dest, value_type, lhs, rhs = data
        if not (indexed_dest_has_slot if indexed_kernel is not None else parsed_function_has_value_slot(func, dest)):
            return []
        if (
            value_type.is_array
            and value_type.elem is not None
            and value_type.elem.is_int
        ):
            return _emit_vector_icmp(
                func, cond, dest, value_type, lhs, rhs, module_symbols
            )
        lines = materialize_value(func, lhs, value_type, 9, module_symbols)
        lines.extend(materialize_value(func, rhs, value_type, 10, module_symbols))
        if value_type.is_int and cond in {"slt", "sle", "sgt", "sge"}:
            lines.extend(sign_extend_int_reg(value_type, reg_name(value_type, 9)))
            lines.extend(sign_extend_int_reg(value_type, reg_name(value_type, 10)))
        lines.append(
            emitted_compare_register_line(
                reg_name(value_type, 9),
                reg_name(value_type, 10),
            )
        )
        lines.append(emitted_cset_line("w11", aarch64_cc(cond)))
        lines.extend(
            _commit_or_spill_scalar_result(
                func, dest, I1, "w11", indexed_dest_id
            )
        )
        return lines

    if kind_id == PARSED_INSTRUCTION_KIND_FCMP:
        cond, dest, value_type, lhs, rhs = data
        if not (indexed_dest_has_slot if indexed_kernel is not None else parsed_function_has_value_slot(func, dest)):
            return []
        if (
            value_type.is_array
            and value_type.elem is not None
            and value_type.elem.is_fp
        ):
            return _emit_vector_fcmp(
                func, cond, dest, value_type, lhs, rhs, module_symbols
            )
        lines = materialize_value(func, lhs, value_type, 9, module_symbols)
        lines.extend(materialize_value(func, rhs, value_type, 10, module_symbols))
        lines.append(f"  fcmp {reg_name(value_type, 9)}, {reg_name(value_type, 10)}")
        lines.extend(emit_fcmp_result(cond))
        lines.extend(store_reg_to_value_slot("w11", func, dest))
        return lines

    if kind_id == PARSED_INSTRUCTION_KIND_CAST:
        if indexed_kernel is not None:
            cast_record: CompilerInt4 = indexed_kernel.instruction_record(data)
            op = indexed_kernel.call_texts[cast_record.first]
            dest = indexed_kernel.value_name(indexed_dest_id)
            src_type_id = cast_record.second
            value = (
                indexed_kernel.value_name(cast_record.third)
                if cast_record.third >= 0
                else indexed_kernel.call_texts[-cast_record.third - 1]
            )
            dst_type_id = cast_record.fourth
            src_header: CompilerInt4 = indexed_kernel.type_header(src_type_id)
            dst_header: CompilerInt4 = indexed_kernel.type_header(dst_type_id)
            if (
                src_header.first in (TYPE_KIND_INT, TYPE_KIND_FP, TYPE_KIND_PTR)
                and dst_header.first
                in (TYPE_KIND_INT, TYPE_KIND_FP, TYPE_KIND_PTR)
            ):
                if not indexed_dest_has_slot:
                    return []
                lines = materialize_scalar_value_indexed(
                    func,
                    indexed_kernel,
                    value,
                    src_type_id,
                    9,
                    module_symbols,
                    value_id=cast_record.third,
                )
                lines.extend(
                    emit_cast_indexed(
                        indexed_kernel,
                        op,
                        src_type_id,
                        dst_type_id,
                    )
                )
                lines.extend(
                    _commit_or_spill_scalar_result_indexed(
                        func,
                        indexed_kernel,
                        indexed_dest_id,
                        dst_type_id,
                        reg_name_indexed(indexed_kernel, dst_type_id, 10),
                    )
                )
                return lines
            src_type = indexed_kernel.type_desc(src_type_id)
            dst_type = indexed_kernel.type_desc(dst_type_id)
        else:
            op, dest, src_type, value, dst_type = data
        if not (indexed_dest_has_slot if indexed_kernel is not None else parsed_function_has_value_slot(func, dest)):
            return []
        if (
            src_type.is_array
            and dst_type.is_array
            and src_type.elem is not None
            and dst_type.elem is not None
            and (src_type.elem.is_int or src_type.elem.is_fp)
            and (dst_type.elem.is_int or dst_type.elem.is_fp)
        ):
            return _emit_vector_cast(
                func, op, dest, src_type, value, dst_type, module_symbols
            )
        lines = materialize_value(func, value, src_type, 9, module_symbols)
        lines.extend(emit_cast(op, src_type, dst_type))
        lines.extend(
            _commit_or_spill_scalar_result(
                func,
                dest,
                dst_type,
                reg_name(dst_type, 10),
                indexed_dest_id,
            )
        )
        return lines

    if kind_id == PARSED_INSTRUCTION_KIND_SELECT:
        if indexed_kernel is not None:
            select_record: CompilerInt4 = indexed_kernel.instruction_record(data)
            dest = indexed_kernel.value_name(indexed_dest_id)
            value_type_id = select_record.first
            value_type_header: CompilerInt4 = indexed_kernel.type_header(
                value_type_id
            )
            cond = (
                indexed_kernel.value_name(select_record.second)
                if select_record.second >= 0
                else indexed_kernel.call_texts[-select_record.second - 1]
            )
            true_value = (
                indexed_kernel.value_name(select_record.third)
                if select_record.third >= 0
                else indexed_kernel.call_texts[-select_record.third - 1]
            )
            false_value = (
                indexed_kernel.value_name(select_record.fourth)
                if select_record.fourth >= 0
                else indexed_kernel.call_texts[-select_record.fourth - 1]
            )
            if value_type_header.first in (
                TYPE_KIND_INT,
                TYPE_KIND_FP,
                TYPE_KIND_PTR,
            ):
                if not indexed_dest_has_slot:
                    return []
                cond_type_id = (
                    indexed_kernel.value_type_id(select_record.second)
                    if select_record.second >= 0
                    else indexed_kernel.intern_type(I1)
                )
                lines = materialize_scalar_value_indexed(
                    func,
                    indexed_kernel,
                    true_value,
                    value_type_id,
                    10,
                    module_symbols,
                    value_id=select_record.third,
                )
                lines.extend(
                    materialize_scalar_value_indexed(
                        func,
                        indexed_kernel,
                        false_value,
                        value_type_id,
                        11,
                        module_symbols,
                        value_id=select_record.fourth,
                    )
                )
                lines.extend(
                    materialize_scalar_value_indexed(
                        func,
                        indexed_kernel,
                        cond,
                        cond_type_id,
                        9,
                        module_symbols,
                        value_id=select_record.second,
                    )
                )
                lines.append(emitted_compare_immediate_line("w9", 0))
                mnemonic = (
                    "fcsel"
                    if value_type_header.first == TYPE_KIND_FP
                    else "csel"
                )
                lines.append(
                    f"  {mnemonic} "
                    f"{reg_name_indexed(indexed_kernel, value_type_id, 12)}, "
                    f"{reg_name_indexed(indexed_kernel, value_type_id, 10)}, "
                    f"{reg_name_indexed(indexed_kernel, value_type_id, 11)}, ne"
                )
                lines.extend(
                    _commit_or_spill_scalar_result_indexed(
                        func,
                        indexed_kernel,
                        indexed_dest_id,
                        value_type_id,
                        reg_name_indexed(indexed_kernel, value_type_id, 12),
                    )
                )
                return lines
            value_type = indexed_kernel.type_desc(value_type_id)
        else:
            dest, value_type, cond, true_value, false_value = data
        if not (indexed_dest_has_slot if indexed_kernel is not None else parsed_function_has_value_slot(func, dest)):
            return []
        cond_type = func.value_types.get(cond, I1)
        if (
            value_type.is_array
            and value_type.elem is not None
            and value_type.elem.is_int
            and cond_type.is_array
        ):
            return _emit_vector_select(
                func, dest, value_type, cond, true_value, false_value, module_symbols
            )
        if value_type.is_array or value_type.is_struct:
            return _emit_aggregate_select(
                func, dest, value_type, cond, true_value, false_value, module_symbols
            )
        lines = materialize_value(func, true_value, value_type, 10, module_symbols)
        lines.extend(
            materialize_value(func, false_value, value_type, 11, module_symbols)
        )
        lines.extend(materialize_value(func, cond, I1, 9, module_symbols))
        lines.append(emitted_compare_immediate_line("w9", 0))
        if value_type.is_fp:
            lines.append(
                f"  fcsel {reg_name(value_type, 12)}, {reg_name(value_type, 10)}, {reg_name(value_type, 11)}, ne"
            )
        else:
            lines.append(
                f"  csel {reg_name(value_type, 12)}, {reg_name(value_type, 10)}, {reg_name(value_type, 11)}, ne"
            )
        lines.extend(
            _commit_or_spill_scalar_result(
                func,
                dest,
                value_type,
                reg_name(value_type, 12),
                indexed_dest_id,
            )
        )
        return lines

    if kind_id == PARSED_INSTRUCTION_KIND_FREEZE:
        dest, value_type, value = data
        if not (indexed_dest_has_slot if indexed_kernel is not None else parsed_function_has_value_slot(func, dest)):
            return []
        if value_type.is_array or value_type.is_struct:
            raise BackendUnavailable(
                f"self backend freeze aggregate result not translated yet in {func.name!r}: "
                f"{value_type.describe()}"
            )
        lines = materialize_value(func, value, value_type, 10, module_symbols)
        lines.extend(
            _commit_or_spill_scalar_result(
                func,
                dest,
                value_type,
                reg_name(value_type, 10),
                indexed_dest_id,
            )
        )
        return lines

    if kind_id == PARSED_INSTRUCTION_KIND_INSERTELEMENT:
        dest, vector_type, vector_value, elem_type, elem_value, index_value = data
        return _emit_insertelement(
            func,
            dest,
            vector_type,
            vector_value,
            elem_type,
            elem_value,
            index_value,
            module_symbols,
        )

    if kind_id == PARSED_INSTRUCTION_KIND_SHUFFLEVECTOR:
        dest, vector_type, lhs, rhs, mask_type, mask_value = data
        return _emit_shufflevector(
            func, dest, vector_type, lhs, rhs, mask_type, mask_value
        )

    if kind_id == PARSED_INSTRUCTION_KIND_EXTRACTELEMENT:
        dest, vector_type, vector_value, index_value, elem_type = data
        if not (indexed_dest_has_slot if indexed_kernel is not None else parsed_function_has_value_slot(func, dest)):
            return []
        return _emit_extractelement(
            func, dest, vector_type, vector_value, index_value, elem_type
        )

    if kind_id == PARSED_INSTRUCTION_KIND_EXTRACTVALUE:
        dest, aggregate_type, value, _indices, result_type, offset = data
        if not (indexed_dest_has_slot if indexed_kernel is not None else parsed_function_has_value_slot(func, dest)):
            return []
        if is_aggregate_literal_value(value):
            literal_bytes = aggregate_literal_to_bytes(aggregate_type, value)
            field_bytes = literal_bytes[offset : offset + result_type.slot_size]
            field_value = str(int.from_bytes(field_bytes, "little"))
            lines = materialize_value(
                func, field_value, result_type, 10, module_symbols
            )
            lines.extend(store_value_regs_to_value_slot(func, dest, 10))
            return lines
        if not parsed_function_has_value_slot(func, value):
            raise BackendUnavailable(
                f"self backend extractvalue source not materialized in {func.name!r}: {value}"
            )
        lines = emit_value_slot_base_address(func, value, "x12")
        if offset:
            lines.extend(emit_add_offset("x12", "x12", offset))
        if result_type.is_array or result_type.is_struct:
            lines.extend(copy_address_to_value_slot("x12", func, dest))
            return lines
        lines.extend(load_value_from_address("x12", result_type, 10))
        lines.extend(store_value_regs_to_value_slot(func, dest, 10))
        return lines

    if kind_id == PARSED_INSTRUCTION_KIND_INSERTVALUE:
        (
            dest,
            aggregate_type,
            aggregate_value,
            elem_type,
            elem_value,
            _indices,
            offset,
        ) = data
        if not (indexed_dest_has_slot if indexed_kernel is not None else parsed_function_has_value_slot(func, dest)):
            return []
        return _emit_insertvalue(
            func,
            dest,
            aggregate_type,
            aggregate_value,
            elem_type,
            elem_value,
            offset,
            module_symbols,
        )

    if kind_id == PARSED_INSTRUCTION_KIND_VA_ARG:
        dest, ap_type, ap, value_type = data
        return emit_va_arg(func, dest, ap_type, ap, value_type, module_symbols)

    if kind_id == PARSED_INSTRUCTION_KIND_GEP:
        if indexed_kernel is not None:
            gep_header: CompilerInt4 = indexed_kernel.gep_header(data)
            gep_span: CompilerInt4 = indexed_kernel.gep_span(data)
            dest = indexed_kernel.value_name(gep_span.third)
            ptr_value = (
                indexed_kernel.value_name(gep_header.third)
                if gep_header.third >= 0
                else indexed_kernel.call_texts[-gep_header.third - 1]
            )
        else:
            dest, base_type, _ptr_type, ptr_value, indices = data
        if not (indexed_dest_has_slot if indexed_kernel is not None else parsed_function_has_value_slot(func, dest)):
            return []
        if indexed_kernel is not None:
            lines = materialize_scalar_value_indexed(
                func,
                indexed_kernel,
                ptr_value,
                gep_header.second,
                9,
                module_symbols,
                value_id=gep_header.third,
            )
            lines.extend(
                emit_gep_offset_indexed(
                    func,
                    indexed_kernel,
                    data,
                    module_symbols,
                )
            )
            lines.extend(
                _commit_or_spill_scalar_result_indexed(
                    func,
                    indexed_kernel,
                    indexed_dest_id,
                    gep_span.second,
                    "x11",
                )
            )
            return lines
        else:
            lines = materialize_pointer(func, ptr_value, 9, module_symbols)
            lines.extend(emit_gep_offset(func, base_type, indices, module_symbols))
        lines.extend(
            _commit_or_spill_scalar_result(
                func,
                dest,
                (
                    indexed_kernel.type_desc(
                        indexed_kernel.value_slot_type_id(indexed_dest_id)
                    )
                    if indexed_dest_id >= 0
                    else parsed_function_value_slot_type(func, dest)
                ),
                "x11",
                indexed_dest_id,
            )
        )
        return lines

    if kind_id == PARSED_INSTRUCTION_KIND_CALL:
        if indexed_kernel is not None:
            call_header: CompilerInt4 = indexed_kernel.call_header(data)
            indexed_callee = indexed_kernel.call_texts[call_header.second]
            if (call_header.third & 1) or not indexed_callee.startswith(
                "llvm."
            ):
                return emit_call_instruction_indexed(
                    func,
                    indexed_kernel,
                    data,
                    module_symbols,
                )
            data = indexed_kernel.diagnostic_call_data(data)
        (
            dest,
            ret_type,
            callee,
            is_indirect,
            args,
            fixed_arg_count,
            is_vararg_call,
            arg_alignments,
        ) = data
        return emit_call_instruction(
            func,
            dest,
            ret_type,
            callee,
            is_indirect,
            args,
            fixed_arg_count,
            is_vararg_call,
            arg_alignments,
            module_symbols,
            dest_value_id=indexed_dest_id,
            indexed_use_count=indexed_use_count,
            indexed_use0=indexed_use0,
            indexed_use_tail=indexed_use_tail,
        )

    return None


def emit_compute_instruction(
    func: ParsedFunction,
    kind: str,
    data: tuple,
    module_symbols: PreparedModuleSymbols,
    *,
    indexed_kernel: IndexedFunctionKernel | None = None,
    block_id: int = -1,
    instruction_index: int = -1,
    indexed_dest_id: int = -1,
    indexed_use_count: int = -1,
    indexed_use0: int = -1,
    indexed_use_tail: int = -1,
) -> list[str] | None:
    """Legacy/text API; indexed consumers call the integer-ID entry."""
    kind_id = _PARSED_INSTRUCTION_KIND_IDS.get(kind)
    if kind_id is None:
        return None
    return emit_compute_instruction_by_id(
        func,
        kind_id,
        data,
        module_symbols,
        indexed_kernel=indexed_kernel,
        block_id=block_id,
        instruction_index=instruction_index,
        indexed_dest_id=indexed_dest_id,
        indexed_use_count=indexed_use_count,
        indexed_use0=indexed_use0,
        indexed_use_tail=indexed_use_tail,
    )
