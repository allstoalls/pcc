from __future__ import annotations

from . import BackendUnavailable
from .self_backend_aarch64_darwin_addr import materialize_global_address
from .self_backend_aarch64_darwin_mem import (
    emitted_addsub_register_line,
    emitted_memory_instruction_line,
    emitted_move_register_line,
    emitted_movewide_instruction_line,
)
from .self_backend_aarch64_darwin_regs import (
    emit_add_offset,
    emit_const_to_reg,
    emit_const_to_reg_bits,
    emit_fp_constant,
    pick_scratch_gpr,
)
from .self_backend_aarch64_darwin_regalloc import allocated_register_name
from .self_backend_float_bits import bits_to_float64, float32_to_bits
from .self_backend_aarch64_darwin_slots import (
    copy_slot_to_slot,
    copy_slot_to_slot_parts,
    emit_slot_base_address,
    emit_slot_base_address_parts,
    emit_value_slot_base_address,
    load_slot_to_value_regs,
    load_slot_to_value_regs_parts,
    load_value_slot_to_value_regs,
    store_to_address,
    zero_address,
    zero_slot,
    zero_value_slot,
)
from .self_backend_aarch64_darwin_abi import (
    abi_value_reg_names,
    aggregate_hfa_members,
    aggregate_reg_chunks,
    reg_name,
    reg_name_indexed,
)
from .self_backend_ir import (
    ParsedFunction,
    SlotInfo,
    TypeDesc,
    _align_to,
    parsed_function_alloca_value_id,
    parsed_function_alloca_slot_offset,
    parsed_function_alloca_slot_type,
    parsed_function_has_alloca_slot,
    parsed_function_has_value_slot,
    parsed_function_value_slot_offset,
    parsed_function_value_slot_id,
    parsed_function_value_slot_type,
    text_key_mapping_get,
)
from .self_backend_kernel import (
    TYPE_KIND_FP,
    TYPE_KIND_INT,
    TYPE_KIND_PTR,
    IndexedFunctionKernel,
    get_indexed_function_kernel,
)
from .self_backend_value_arena import CompilerInt4
from .self_backend_module_symbols import PreparedModuleSymbols
from .self_backend_parse import (
    aggregate_literal_to_bytes,
    const_int_from_value,
    decode_global_name,
    decode_value_token,
    extract_leading_type_token,
    is_aggregate_literal_value,
    is_float_literal,
    parse_constant_gep,
    parse_ir_type,
    split_top_level,
    split_top_level_keyword,
    strip_typed_initializer,
)

_CONSTANT_EXPR_PREFIX = "cexpr:"
_CONSTANT_EXPR_BINOPS = {"add", "sub", "mul", "and", "or", "xor", "shl", "lshr", "ashr"}
_CONSTANT_EXPR_CASTS = {"ptrtoint", "inttoptr", "trunc"}
_CONSTANT_EXPR_REG_POOL = (9, 10, 11, 12, 13, 14, 15, 16, 17)


def _strip_constant_expr_attrs(rest: str) -> str:
    text = rest.strip()
    while text and not text.startswith("("):
        pieces = text.split(None, 1)
        if len(pieces) != 2:
            raise BackendUnavailable(
                f"self backend cannot parse constant expression attributes in {rest!r}"
            )
        text = pieces[1].strip()
    return text


def _parse_constant_expr_cast(text: str):
    pieces = text.strip().split(None, 1)
    if len(pieces) != 2 or pieces[0] not in _CONSTANT_EXPR_CASTS:
        return None
    op, rest = pieces
    rest = rest.strip()
    if not (rest.startswith("(") and rest.endswith(")")):
        return None
    body = rest[1:-1].strip()
    src_type_text, remainder = extract_leading_type_token(body)
    value_text, dst_type_text = split_top_level_keyword(remainder, " to ")
    return op, parse_ir_type(src_type_text), value_text, parse_ir_type(dst_type_text)


def _parse_constant_expr_binop(text: str):
    pieces = text.strip().split(None, 1)
    if len(pieces) != 2 or pieces[0] not in _CONSTANT_EXPR_BINOPS:
        return None
    op, rest = pieces
    rest = _strip_constant_expr_attrs(rest)
    if not (rest.startswith("(") and rest.endswith(")")):
        return None
    body = rest[1:-1].strip()
    parts = split_top_level(body)
    if len(parts) != 2:
        return None
    lhs_type_text, lhs_value_text = extract_leading_type_token(parts[0])
    rhs_type_text, rhs_value_text = extract_leading_type_token(parts[1])
    value_type = parse_ir_type(lhs_type_text)
    rhs_type = parse_ir_type(rhs_type_text)
    if value_type.describe() != rhs_type.describe():
        raise BackendUnavailable(
            "self backend constant expression binop expected matching operand types, got "
            f"{value_type.describe()} and {rhs_type.describe()}"
        )
    return op, value_type, lhs_value_text, rhs_value_text


def _emit_constant_expr_binop(
    op: str, value_type: TypeDesc, dest_reg_index: int, rhs_reg_index: int
) -> list[str]:
    if not value_type.is_int:
        raise BackendUnavailable(
            f"self backend constant expression binop expected integer type, got {value_type.describe()}"
        )
    dest = reg_name(value_type, dest_reg_index)
    rhs = reg_name(value_type, rhs_reg_index)
    if op == "add" or op == "sub":
        return [emitted_addsub_register_line(op, dest, dest, rhs)]
    mapping = {
        "mul": f"  mul {dest}, {dest}, {rhs}",
        "and": f"  and {dest}, {dest}, {rhs}",
        "or": f"  orr {dest}, {dest}, {rhs}",
        "xor": f"  eor {dest}, {dest}, {rhs}",
        "shl": f"  lslv {dest}, {dest}, {rhs}",
        "lshr": f"  lsrv {dest}, {dest}, {rhs}",
        "ashr": f"  asrv {dest}, {dest}, {rhs}",
    }
    if op not in mapping:
        raise BackendUnavailable(
            f"self backend does not support constant expression binop {op!r}"
        )
    return [mapping[op]]


def _materialize_constant_expr_operand(
    func: ParsedFunction,
    value_text: str,
    value_type: TypeDesc,
    reg_index: int,
    module_symbols: PreparedModuleSymbols,
    available_regs: tuple[int, ...],
) -> list[str]:
    decoded_value = decode_value_token(value_text)
    if decoded_value.startswith(_CONSTANT_EXPR_PREFIX):
        return _materialize_constant_expr_to_reg(
            func,
            decoded_value[len(_CONSTANT_EXPR_PREFIX) :],
            value_type,
            reg_index,
            module_symbols,
            available_regs,
        )
    return materialize_value(func, decoded_value, value_type, reg_index, module_symbols)


def _materialize_constant_expr_to_reg(
    func: ParsedFunction,
    text: str,
    expected_type: TypeDesc,
    reg_index: int,
    module_symbols: PreparedModuleSymbols,
    available_regs: tuple[int, ...] = _CONSTANT_EXPR_REG_POOL,
) -> list[str]:
    if reg_index not in available_regs:
        available_regs = (reg_index, *available_regs)
    if cast := _parse_constant_expr_cast(text):
        op, src_type, value_text, dst_type = cast
        if dst_type.describe() != expected_type.describe():
            raise BackendUnavailable(
                "self backend constant expression cast type mismatch: "
                f"{dst_type.describe()} used as {expected_type.describe()}"
            )
        if op in {"ptrtoint", "inttoptr", "trunc"}:
            return _materialize_constant_expr_operand(
                func,
                value_text,
                src_type,
                reg_index,
                module_symbols,
                available_regs,
            )
    if binop := _parse_constant_expr_binop(text):
        op, value_type, lhs_value_text, rhs_value_text = binop
        if value_type.describe() != expected_type.describe():
            raise BackendUnavailable(
                "self backend constant expression binop type mismatch: "
                f"{value_type.describe()} used as {expected_type.describe()}"
            )
        scratch_regs = tuple(index for index in available_regs if index != reg_index)
        if not scratch_regs:
            raise BackendUnavailable(
                f"self backend ran out of registers for constant expression in {func.name!r}"
            )
        rhs_reg_index = scratch_regs[0]
        lines = _materialize_constant_expr_operand(
            func,
            lhs_value_text,
            value_type,
            reg_index,
            module_symbols,
            available_regs,
        )
        lines.extend(
            _materialize_constant_expr_operand(
                func,
                rhs_value_text,
                value_type,
                rhs_reg_index,
                module_symbols,
                scratch_regs,
            )
        )
        lines.extend(
            _emit_constant_expr_binop(op, value_type, reg_index, rhs_reg_index)
        )
        return lines
    raise BackendUnavailable(
        f"self backend cannot materialize constant expression in {func.name!r}: {text!r}"
    )


def materialize_indirect_aggregate_arg_pointer(
    func: ParsedFunction,
    value: str,
    value_type: TypeDesc,
    reg: str,
    *,
    value_id: int = -1,
) -> list[str]:
    if value_id >= 0 and func.indexed_slot_projection:
        kernel = get_indexed_function_kernel(func)
        slot_id = kernel.value_slot_id(value_id)
        if slot_id >= 0:
            return emit_slot_base_address_parts(
                kernel.slot_offset(slot_id),
                reg,
            )
        alloca_offset = kernel.alloca_offset(value_id)
        alloca_type_id = kernel.alloca_type_id(value_id)
        if (
            alloca_offset >= 0
            and alloca_type_id >= 0
            and kernel.type_desc(alloca_type_id).describe()
            == value_type.describe()
        ):
            return emit_add_offset(reg, "x29", -alloca_offset)
        raise BackendUnavailable(
            f"self backend can only pass large aggregate-by-value args from local storage right now in {func.name!r}: {value}"
        )
    if not parsed_function_has_value_slot(func, value) and not parsed_function_has_alloca_slot(func, value):
        raise BackendUnavailable(
            f"self backend can only pass large aggregate-by-value args from local storage right now in {func.name!r}: {value}"
        )
    if parsed_function_has_value_slot(func, value):
        return emit_value_slot_base_address(func, value, reg)
    if (
        parsed_function_alloca_slot_type(func, value).describe()
        == value_type.describe()
    ):
        return emit_add_offset(
            reg, "x29", -parsed_function_alloca_slot_offset(func, value)
        )
    raise BackendUnavailable(
        f"self backend can only pass large aggregate-by-value args from local storage right now in {func.name!r}: {value}"
    )


def materialize_aggregate_storage_address(
    func: ParsedFunction,
    value: str,
    value_type: TypeDesc,
    reg: str,
    *,
    value_id: int = -1,
) -> list[str]:
    if value_id >= 0 and func.indexed_slot_projection:
        kernel = get_indexed_function_kernel(func)
        slot_id = kernel.value_slot_id(value_id)
        if slot_id >= 0:
            return emit_slot_base_address_parts(
                kernel.slot_offset(slot_id),
                reg,
            )
        alloca_offset = kernel.alloca_offset(value_id)
        alloca_type_id = kernel.alloca_type_id(value_id)
        if (
            alloca_offset >= 0
            and alloca_type_id >= 0
            and kernel.type_desc(alloca_type_id).describe()
            == value_type.describe()
        ):
            return emit_add_offset(reg, "x29", -alloca_offset)
        raise BackendUnavailable(
            f"self backend can only materialize aggregate storage from local slots right now in {func.name!r}: {value}"
        )
    if parsed_function_has_value_slot(func, value):
        return emit_value_slot_base_address(func, value, reg)
    if (
        parsed_function_has_alloca_slot(func, value)
        and parsed_function_alloca_slot_type(func, value).describe()
        == value_type.describe()
    ):
        return emit_add_offset(
            reg, "x29", -parsed_function_alloca_slot_offset(func, value)
        )
    raise BackendUnavailable(
        f"self backend can only materialize aggregate storage from local slots right now in {func.name!r}: {value}"
    )


def store_large_aggregate_literal_to_address(
    value_type: TypeDesc,
    value: str,
    base_addr_reg: str,
    *,
    addr_scratch_reg: str = "x16",
    data_reg_64: str = "x14",
    data_reg_32: str = "w14",
    module_symbols: PreparedModuleSymbols | None = None,
) -> list[str]:
    try:
        literal_bytes = aggregate_literal_to_bytes(value_type, value)
    except BackendUnavailable:
        if module_symbols is None:
            raise
        lines = zero_address(base_addr_reg, value_type.slot_size)
        lines.extend(
            _store_symbolic_aggregate_literal_fields(
                value_type,
                value,
                base_addr_reg,
                0,
                module_symbols,
                addr_scratch_reg=addr_scratch_reg,
                data_reg_64=data_reg_64,
                data_reg_32=data_reg_32,
            )
        )
        return lines
    return _store_literal_bytes_to_address(
        literal_bytes,
        base_addr_reg,
        0,
        addr_scratch_reg=addr_scratch_reg,
        data_reg_64=data_reg_64,
        data_reg_32=data_reg_32,
    )


def _store_literal_bytes_to_address(
    literal_bytes: bytes,
    base_addr_reg: str,
    base_offset: int,
    *,
    addr_scratch_reg: str,
    data_reg_64: str,
    data_reg_32: str,
) -> list[str]:
    lines: list[str] = []
    offset = 0
    while offset < len(literal_bytes):
        remaining = len(literal_bytes) - offset
        chunk_size = (
            8 if remaining >= 8 else 4 if remaining >= 4 else 2 if remaining >= 2 else 1
        )
        chunk_value = int.from_bytes(
            literal_bytes[offset : offset + chunk_size], "little"
        )
        addr_reg = base_addr_reg
        absolute_offset = base_offset + offset
        if absolute_offset:
            lines.extend(
                emit_add_offset(addr_scratch_reg, base_addr_reg, absolute_offset)
            )
            addr_reg = addr_scratch_reg
        width = max(8, chunk_size * 8)
        data_reg = data_reg_64 if chunk_size > 4 else data_reg_32
        lines.extend(emit_const_to_reg(TypeDesc("int", width), data_reg, chunk_value))
        lines.extend(store_to_address(addr_reg, data_reg, TypeDesc("int", width)))
        offset += chunk_size
    return lines


def _store_symbolic_aggregate_literal_fields(
    value_type: TypeDesc,
    value: str,
    base_addr_reg: str,
    base_offset: int,
    module_symbols: PreparedModuleSymbols,
    *,
    addr_scratch_reg: str,
    data_reg_64: str,
    data_reg_32: str,
) -> list[str]:
    text = value.strip()
    if value_type.is_array:
        if text.startswith("<") and text.endswith(">"):
            text = "[" + text[1:-1].strip() + "]"
        if not (text.startswith("[") and text.endswith("]")):
            return _store_literal_bytes_to_address(
                aggregate_literal_to_bytes(value_type, value),
                base_addr_reg,
                base_offset,
                addr_scratch_reg=addr_scratch_reg,
                data_reg_64=data_reg_64,
                data_reg_32=data_reg_32,
            )
        assert value_type.elem is not None
        items = split_top_level(text[1:-1].strip())
        stride = _align_to(value_type.elem.slot_size, value_type.elem.align)
        lines: list[str] = []
        for index, item in enumerate(items):
            lines.extend(
                _store_symbolic_aggregate_literal_fields(
                    value_type.elem,
                    strip_typed_initializer(item),
                    base_addr_reg,
                    base_offset + index * stride,
                    module_symbols,
                    addr_scratch_reg=addr_scratch_reg,
                    data_reg_64=data_reg_64,
                    data_reg_32=data_reg_32,
                )
            )
        return lines
    if value_type.is_struct:
        if not (text.startswith("{") and text.endswith("}")):
            return _store_literal_bytes_to_address(
                aggregate_literal_to_bytes(value_type, value),
                base_addr_reg,
                base_offset,
                addr_scratch_reg=addr_scratch_reg,
                data_reg_64=data_reg_64,
                data_reg_32=data_reg_32,
            )
        items = split_top_level(text[1:-1].strip())
        lines: list[str] = []
        for index, (field_type, item) in enumerate(zip(value_type.fields, items)):
            lines.extend(
                _store_symbolic_aggregate_literal_fields(
                    field_type,
                    strip_typed_initializer(item),
                    base_addr_reg,
                    base_offset + value_type.field_offset(index),
                    module_symbols,
                    addr_scratch_reg=addr_scratch_reg,
                    data_reg_64=data_reg_64,
                    data_reg_32=data_reg_32,
                )
            )
        return lines
    if value_type.is_ptr and _is_symbolic_pointer_literal(text):
        return _store_symbolic_pointer_literal_to_address(
            text,
            base_addr_reg,
            base_offset,
            module_symbols,
            addr_scratch_reg=addr_scratch_reg,
            data_reg_64=data_reg_64,
        )
    return _store_literal_bytes_to_address(
        aggregate_literal_to_bytes(value_type, value),
        base_addr_reg,
        base_offset,
        addr_scratch_reg=addr_scratch_reg,
        data_reg_64=data_reg_64,
        data_reg_32=data_reg_32,
    )


def _is_symbolic_pointer_literal(value: str) -> bool:
    return value.startswith("@") or value.startswith(
        ("gep0:", "gepconst:", "getelementptr")
    )


def _store_symbolic_pointer_literal_to_address(
    value: str,
    base_addr_reg: str,
    base_offset: int,
    module_symbols: PreparedModuleSymbols,
    *,
    addr_scratch_reg: str,
    data_reg_64: str,
) -> list[str]:
    lines: list[str] = []
    decoded = decode_value_token(value)
    if decoded.startswith("gep0:"):
        lines.extend(
            materialize_global_address(
                decoded.split(":", 1)[1], data_reg_64, module_symbols
            )
        )
    elif decoded.startswith("gepconst:"):
        _tag, base, offset_text = decoded.split(":", 2)
        lines.extend(materialize_global_address(base, data_reg_64, module_symbols))
        offset = int(offset_text)
        if offset:
            lines.extend(emit_add_offset(data_reg_64, data_reg_64, offset))
    elif value.startswith("getelementptr"):
        base, offset = parse_constant_gep(value)
        lines.extend(materialize_global_address(base, data_reg_64, module_symbols))
        if offset:
            lines.extend(emit_add_offset(data_reg_64, data_reg_64, offset))
    elif decoded.startswith("@"):
        lines.extend(
            materialize_global_address(
                decode_global_name(decoded), data_reg_64, module_symbols
            )
        )
    else:
        raise BackendUnavailable(
            f"self backend cannot store symbolic pointer aggregate literal: {value!r}"
        )
    addr_reg = base_addr_reg
    if base_offset:
        lines.extend(emit_add_offset(addr_scratch_reg, base_addr_reg, base_offset))
        addr_reg = addr_scratch_reg
    lines.extend(
        store_to_address(
            addr_reg, data_reg_64, TypeDesc("ptr", pointee=TypeDesc("void"))
        )
    )
    return lines


def _materialize_symbolic_ptr_array_literal_to_regs(
    func: ParsedFunction,
    value_type: TypeDesc,
    value: str,
    regs: tuple[str, ...],
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    """Materialize a small `[N x ptr]` aggregate literal into ABI GPRs.

    `aggregate_literal_to_bytes()` intentionally cannot encode symbolic pointer
    lanes such as `@foo` or constant GEPs because relocation needs target symbol
    materialization.  Small pointer arrays still fit the self-backend aggregate
    register ABI (`[2 x ptr]` -> `xN`, `xN+1`), so lower each lane as a pointer
    scalar rather than trying to byte-pack the whole aggregate.
    """
    if not value_type.is_array or value_type.elem is None or not value_type.elem.is_ptr:
        raise BackendUnavailable(
            f"self backend symbolic aggregate register materialization currently expects ptr arrays, got {value_type.describe()}"
        )
    text = value.strip()
    if text.startswith("<") and text.endswith(">"):
        text = "[" + text[1:-1].strip() + "]"
    if not (text.startswith("[") and text.endswith("]")):
        raise BackendUnavailable(
            f"self backend expected ptr-array aggregate literal, got {value!r}"
        )
    items = split_top_level(text[1:-1].strip())
    if len(items) != value_type.count or len(regs) < value_type.count:
        raise BackendUnavailable(
            f"self backend ptr-array literal lane count mismatch for {value_type.describe()}: {value!r}"
        )
    lines: list[str] = []
    for item, reg in zip(items, regs):
        if not reg.startswith("x"):
            raise BackendUnavailable(
                f"self backend expected GPR for ptr lane, got {reg}"
            )
        lane_value = decode_value_token(strip_typed_initializer(item))
        lines.extend(
            materialize_value(
                func, lane_value, value_type.elem, int(reg[1:]), module_symbols
            )
        )
    return lines


def copy_large_aggregate_value_to_slot(
    func: ParsedFunction,
    value: str,
    value_type: TypeDesc,
    dest_slot: SlotInfo,
    *,
    module_symbols: PreparedModuleSymbols | None = None,
) -> list[str]:
    if value == "zeroinitializer":
        return zero_slot(dest_slot)
    if is_aggregate_literal_value(value):
        lines = emit_slot_base_address(dest_slot, "x15")
        lines.extend(
            store_large_aggregate_literal_to_address(
                value_type,
                value,
                "x15",
                module_symbols=module_symbols,
            )
        )
        return lines
    if parsed_function_has_value_slot(func, value):
        return copy_slot_to_slot_parts(
            parsed_function_value_slot_offset(func, value),
            parsed_function_value_slot_type(func, value),
            dest_slot.offset,
            dest_slot.type,
        )
    if (
        parsed_function_has_alloca_slot(func, value)
        and parsed_function_alloca_slot_type(func, value).describe()
        == value_type.describe()
    ):
        return copy_slot_to_slot_parts(
            parsed_function_alloca_slot_offset(func, value),
            value_type,
            dest_slot.offset,
            dest_slot.type,
        )
    raise BackendUnavailable(
        f"self backend can only move large aggregate SSA values from local storage right now in {func.name!r}: {value}"
    )


def copy_large_aggregate_value_to_value_slot(
    func: ParsedFunction,
    value: str,
    value_type: TypeDesc,
    dest_name: str,
    *,
    module_symbols: PreparedModuleSymbols | None = None,
) -> list[str]:
    if value == "zeroinitializer":
        return zero_value_slot(func, dest_name)
    if is_aggregate_literal_value(value):
        lines = emit_value_slot_base_address(func, dest_name, "x15")
        lines.extend(
            store_large_aggregate_literal_to_address(
                value_type,
                value,
                "x15",
                module_symbols=module_symbols,
            )
        )
        return lines
    dest_offset = parsed_function_value_slot_offset(func, dest_name)
    dest_type = parsed_function_value_slot_type(func, dest_name)
    if parsed_function_has_value_slot(func, value):
        return copy_slot_to_slot_parts(
            parsed_function_value_slot_offset(func, value),
            parsed_function_value_slot_type(func, value),
            dest_offset,
            dest_type,
        )
    if (
        parsed_function_has_alloca_slot(func, value)
        and parsed_function_alloca_slot_type(func, value).describe()
        == value_type.describe()
    ):
        return copy_slot_to_slot_parts(
            parsed_function_alloca_slot_offset(func, value),
            value_type,
            dest_offset,
            dest_type,
        )
    raise BackendUnavailable(
        f"self backend can only move large aggregate SSA values from local storage right now in {func.name!r}: {value}"
    )


def materialize_pointer(
    func: ParsedFunction,
    value: str,
    reg_index: int,
    module_symbols: PreparedModuleSymbols,
    *,
    value_id: int = -1,
) -> list[str]:
    if value in {"null", "poison", "undef"}:
        return materialize_value(
            func,
            value,
            TypeDesc("ptr", pointee=TypeDesc("void")),
            reg_index,
            module_symbols,
        )
    if (
        value.startswith("gepconst:")
        or value.startswith("gep0:")
        or value.startswith("inttoptrconst:")
        or value.startswith("@")
    ):
        return materialize_value(
            func,
            value,
            TypeDesc("ptr", pointee=TypeDesc("void")),
            reg_index,
            module_symbols,
        )
    if value.startswith("@"):
        return materialize_global_address(value[1:], f"x{reg_index}", module_symbols)
    if value_id >= 0 and func.indexed_slot_projection:
        kernel = get_indexed_function_kernel(func)
        alloca_type_id = kernel.alloca_type_id(value_id)
        if alloca_type_id >= 0:
            ptr_type = kernel.type_desc(alloca_type_id).ptr()
        else:
            type_id = kernel.value_type_id(value_id)
            ptr_type = None if type_id < 0 else kernel.type_desc(type_id)
        if ptr_type is None or not ptr_type.is_ptr:
            raise BackendUnavailable(
                f"self backend expected pointer value {value!r} in {func.name!r}"
            )
        return materialize_value(
            func,
            value,
            ptr_type,
            reg_index,
            module_symbols,
            value_id=value_id,
        )
    ptr_type = text_key_mapping_get(func.value_types, value)
    if ptr_type is None:
        ptr_type = parsed_function_value_slot_type(func, value)
    if parsed_function_has_alloca_slot(func, value):
        ptr_type = parsed_function_alloca_slot_type(func, value).ptr()
    if ptr_type is None or not ptr_type.is_ptr:
        raise BackendUnavailable(
            f"self backend expected pointer value {value!r} in {func.name!r}"
        )
    return materialize_value(func, value, ptr_type, reg_index, module_symbols)


def _indexed_stack_load_op(type_header: CompilerInt4) -> str:
    if (
        type_header.first == TYPE_KIND_PTR
        or type_header.first == TYPE_KIND_FP
        or (type_header.first == TYPE_KIND_INT and type_header.second > 16)
    ):
        return "ldur"
    if type_header.first == TYPE_KIND_INT and type_header.second <= 8:
        return "ldurb"
    if type_header.first == TYPE_KIND_INT and type_header.second <= 16:
        return "ldurh"
    return "ldur"


def _load_indexed_scalar_slot(
    kernel: IndexedFunctionKernel,
    slot_id: int,
    type_id: int,
    reg_index: int,
) -> list[str]:
    type_header: CompilerInt4 = kernel.type_header(type_id)
    reg = reg_name_indexed(kernel, type_id, reg_index)
    op = _indexed_stack_load_op(type_header)
    offset = kernel.slot_offset(slot_id)
    if offset > 255:
        scratch = pick_scratch_gpr(reg)
        lines = emit_slot_base_address_parts(offset, scratch)
        lines.append(emitted_memory_instruction_line(op, reg, scratch))
        return lines
    return [emitted_memory_instruction_line(op, reg, "x29", -offset)]


def materialize_scalar_value_indexed(
    func: ParsedFunction,
    kernel: IndexedFunctionKernel,
    value: str,
    type_id: int,
    reg_index: int,
    module_symbols: PreparedModuleSymbols,
    *,
    value_id: int = -1,
) -> list[str]:
    type_header: CompilerInt4 = kernel.type_header(type_id)
    if type_header.first not in (TYPE_KIND_INT, TYPE_KIND_FP, TYPE_KIND_PTR):
        return materialize_value(
            func,
            value,
            kernel.type_desc(type_id),
            reg_index,
            module_symbols,
            value_id=value_id,
        )
    reg = reg_name_indexed(kernel, type_id, reg_index)
    if value_id >= 0 and func.indexed_slot_projection:
        alloca_offset = kernel.alloca_offset(value_id)
        if alloca_offset >= 0:
            if type_header.first != TYPE_KIND_PTR:
                raise BackendUnavailable(
                    f"self backend cannot use alloca address {value!r} as non-pointer in {func.name!r}"
                )
            return emit_add_offset(reg, "x29", -alloca_offset)
        allocated_index = kernel.value_register(value_id)
        if allocated_index is not None:
            allocated_reg = reg_name_indexed(
                kernel,
                type_id,
                allocated_index,
            )
            if allocated_reg == reg:
                return []
            return [emitted_move_register_line(reg, allocated_reg)]
        slot_id = kernel.value_slot_id(value_id)
        if slot_id >= 0:
            return _load_indexed_scalar_slot(
                kernel,
                slot_id,
                kernel.slot_type_id(slot_id),
                reg_index,
            )

    if type_header.first == TYPE_KIND_FP:
        if value.startswith("0x"):
            # LLVM writes a floating constant with no exact decimal form as the
            # DOUBLE bit pattern -- for `float` operands too. This path used the
            # token as the value's own natural-width bits, so a 32-bit float
            # took the low half, and the low half of a quiet NaN's double
            # pattern is exactly zero. `NAN` from <math.h> therefore reached the
            # program as 0.0 rather than a NaN, silently. `emit_fp_hex_constant`
            # in ..._regs.py already reads the token correctly; this is the
            # indexed/kernel materializer and the two disagreed.
            fp_bits = int(value, 16)
            if type_header.second <= 32:
                fp_bits = float32_to_bits(bits_to_float64(fp_bits))
        elif value in {"poison", "undef", "zeroinitializer"} or is_float_literal(
            value
        ) and float(value) == 0.0:
            fp_bits = 0
        else:
            return materialize_value(
                func,
                value,
                kernel.type_desc(type_id),
                reg_index,
                module_symbols,
                value_id=value_id,
            )
        int_bits = 32 if type_header.second <= 32 else 64
        int_reg = "w12" if int_bits == 32 else "x12"
        lines = emit_const_to_reg_bits(int_bits, int_reg, fp_bits)
        lines.append(f"  fmov {reg}, {int_reg}")
        return lines

    if value == "null" or value in {"poison", "undef", "zeroinitializer"}:
        return [emitted_movewide_instruction_line("movz", reg, 0)]
    if value.startswith(_CONSTANT_EXPR_PREFIX):
        return materialize_value(
            func,
            value,
            kernel.type_desc(type_id),
            reg_index,
            module_symbols,
            value_id=value_id,
        )
    if value.startswith("inttoptrconst:"):
        if type_header.first != TYPE_KIND_PTR:
            raise BackendUnavailable(
                f"self backend cannot materialize inttoptr constant as non-pointer in {func.name!r}: {value}"
            )
        const_value = const_int_from_value(value.split(":", 1)[1])
        if const_value is None:
            raise BackendUnavailable(
                f"self backend expected integer constant inside inttoptr expression in {func.name!r}: {value}"
            )
        return emit_const_to_reg_bits(64, reg, const_value)
    if value.startswith("ptrtointconst:"):
        if type_header.first != TYPE_KIND_INT:
            raise BackendUnavailable(
                f"self backend cannot materialize ptrtoint constant as non-integer in {func.name!r}: {value}"
            )
        return materialize_value(
            func,
            value,
            kernel.type_desc(type_id),
            reg_index,
            module_symbols,
            value_id=value_id,
        )
    if value.startswith("negconst:"):
        if type_header.first != TYPE_KIND_INT:
            raise BackendUnavailable(
                f"self backend cannot materialize negated constant as non-integer in {func.name!r}: {value}"
            )
        inner_value = value.split(":", 1)[1]
        const_value = const_int_from_value(inner_value)
        if const_value is not None:
            bits = type_header.second if type_header.second <= 32 else 64
            return emit_const_to_reg_bits(bits, reg, -const_value)
        lines = materialize_scalar_value_indexed(
            func,
            kernel,
            inner_value,
            type_id,
            reg_index,
            module_symbols,
        )
        zero_reg = "wzr" if reg.startswith("w") else "xzr"
        lines.append(emitted_addsub_register_line("sub", reg, zero_reg, reg))
        return lines
    if value.startswith("addconst:"):
        if type_header.first != TYPE_KIND_INT:
            raise BackendUnavailable(
                f"self backend cannot materialize added constant as non-integer in {func.name!r}: {value}"
            )
        inner_value, offset_text = value.split(":", 1)[1].rsplit(":", 1)
        offset = const_int_from_value(offset_text)
        if offset is None:
            raise BackendUnavailable(
                f"self backend expected integer offset inside add constant expression in {func.name!r}: {value}"
            )
        lines = materialize_scalar_value_indexed(
            func,
            kernel,
            inner_value,
            type_id,
            reg_index,
            module_symbols,
        )
        if offset:
            x_reg = f"x{reg[1:]}" if reg.startswith("w") else reg
            lines.extend(emit_add_offset(x_reg, x_reg, offset))
        return lines
    if value.startswith("gepconst:"):
        if type_header.first != TYPE_KIND_PTR:
            raise BackendUnavailable(
                f"self backend cannot use gep constant {value!r} as non-pointer in {func.name!r}"
            )
        _tag, base, offset_text = value.split(":", 2)
        lines = materialize_global_address(base, reg, module_symbols)
        offset = int(offset_text)
        if offset:
            lines.extend(emit_add_offset(reg, reg, offset))
        return lines
    if value.startswith("gep0:"):
        if type_header.first != TYPE_KIND_PTR:
            raise BackendUnavailable(
                f"self backend cannot use gep constant {value!r} as non-pointer in {func.name!r}"
            )
        return materialize_global_address(value.split(":", 1)[1], reg, module_symbols)
    if value.startswith("@"):
        if type_header.first != TYPE_KIND_PTR:
            raise BackendUnavailable(
                f"self backend cannot use global symbol {value!r} as non-pointer in {func.name!r}"
            )
        return materialize_global_address(value[1:], reg, module_symbols)
    const_value = const_int_from_value(value)
    if const_value is not None:
        bits = 64 if type_header.first == TYPE_KIND_PTR else min(
            type_header.second,
            32,
        )
        if type_header.first == TYPE_KIND_INT and type_header.second > 32:
            bits = 64
        return emit_const_to_reg_bits(bits, reg, const_value)
    indexed_value_id = kernel.value_id(value)
    slot_id = (
        -1 if indexed_value_id < 0 else kernel.value_slot_id(indexed_value_id)
    )
    if slot_id >= 0:
        return _load_indexed_scalar_slot(
            kernel,
            slot_id,
            kernel.slot_type_id(slot_id),
            reg_index,
        )
    raise BackendUnavailable(
        f"self backend does not know how to materialize value {value!r} in {func.name!r}"
    )


def materialize_value(
    func: ParsedFunction,
    value: str,
    expected_type: TypeDesc,
    reg_index: int,
    module_symbols: PreparedModuleSymbols,
    *,
    value_id: int = -1,
) -> list[str]:
    is_aggregate = expected_type.is_array or expected_type.is_struct
    if is_aggregate:
        regs = list(abi_value_reg_names(expected_type, reg_index))
        reg = regs[0] if regs else ""
    else:
        regs = []
        reg = "" if expected_type.is_void else reg_name(expected_type, reg_index)
    raw_hfa = aggregate_hfa_members(expected_type)
    hfa = list(raw_hfa) if raw_hfa else None
    if value_id >= 0 and func.indexed_slot_projection:
        kernel = get_indexed_function_kernel(func)
        alloca_offset = kernel.alloca_offset(value_id)
        if alloca_offset >= 0:
            if not expected_type.is_ptr:
                raise BackendUnavailable(
                    f"self backend cannot use alloca address {value!r} as non-pointer in {func.name!r}"
                )
            return emit_add_offset(reg, "x29", -alloca_offset)
        allocated_index = kernel.value_register(value_id)
        if allocated_index is not None:
            allocated_reg = reg_name(expected_type, allocated_index)
            if allocated_reg == reg:
                return []
            return [emitted_move_register_line(reg, allocated_reg)]
        slot_id = kernel.value_slot_id(value_id)
        if slot_id >= 0:
            return load_slot_to_value_regs_parts(
                kernel.slot_offset(slot_id),
                kernel.type_desc(kernel.slot_type_id(slot_id)),
                reg_index,
            )
    if value == "null":
        return [emitted_movewide_instruction_line("movz", reg, 0)]
    if value in {"poison", "undef"}:
        if expected_type.is_fp:
            return emit_fp_constant(expected_type, reg, "0.000000e+00")
        if hfa is not None:
            return [
                f"  fmov {'s' if member_type.width <= 32 else 'd'}"
                f"{reg_index + member_index}, "
                f"{'wzr' if member_type.width <= 32 else 'xzr'}"
                for member_index, (member_type, _offset) in enumerate(hfa)
            ]
        if not is_aggregate:
            return [emitted_movewide_instruction_line("movz", reg, 0)]
        return [
            emitted_movewide_instruction_line("movz", zero_reg, 0)
            for zero_reg in regs
        ]
    if value.startswith(_CONSTANT_EXPR_PREFIX):
        if not (expected_type.is_int or expected_type.is_ptr):
            raise BackendUnavailable(
                f"self backend cannot materialize constant expression as {expected_type.describe()} in {func.name!r}: {value}"
            )
        return _materialize_constant_expr_to_reg(
            func,
            value[len(_CONSTANT_EXPR_PREFIX) :],
            expected_type,
            reg_index,
            module_symbols,
        )
    if value.startswith("inttoptrconst:"):
        if not expected_type.is_ptr:
            raise BackendUnavailable(
                f"self backend cannot materialize inttoptr constant as non-pointer in {func.name!r}: {value}"
            )
        const_value = const_int_from_value(value.split(":", 1)[1])
        if const_value is None:
            raise BackendUnavailable(
                f"self backend expected integer constant inside inttoptr expression in {func.name!r}: {value}"
            )
        return emit_const_to_reg(expected_type, reg, const_value)
    if value.startswith("ptrtointconst:"):
        if not expected_type.is_int:
            raise BackendUnavailable(
                f"self backend cannot materialize ptrtoint constant as non-integer in {func.name!r}: {value}"
            )
        ptr_value = value.split(":", 1)[1]
        return materialize_value(
            func,
            ptr_value,
            TypeDesc("ptr", pointee=TypeDesc("void")),
            reg_index,
            module_symbols,
        )
    if value.startswith("negconst:"):
        if not expected_type.is_int:
            raise BackendUnavailable(
                f"self backend cannot materialize negated constant as non-integer in {func.name!r}: {value}"
            )
        inner_value = value.split(":", 1)[1]
        const_value = const_int_from_value(inner_value)
        if const_value is not None:
            return emit_const_to_reg(expected_type, reg, -const_value)
        lines = materialize_value(
            func, inner_value, expected_type, reg_index, module_symbols
        )
        zero_reg = "wzr" if reg.startswith("w") else "xzr"
        lines.append(emitted_addsub_register_line("sub", reg, zero_reg, reg))
        return lines
    if value.startswith("addconst:"):
        if not expected_type.is_int:
            raise BackendUnavailable(
                f"self backend cannot materialize added constant as non-integer in {func.name!r}: {value}"
            )
        inner_value, offset_text = value.split(":", 1)[1].rsplit(":", 1)
        offset = const_int_from_value(offset_text)
        if offset is None:
            raise BackendUnavailable(
                f"self backend expected integer offset inside add constant expression in {func.name!r}: {value}"
            )
        lines = materialize_value(
            func, inner_value, expected_type, reg_index, module_symbols
        )
        if offset:
            x_reg = f"x{reg[1:]}" if reg.startswith("w") else reg
            lines.extend(emit_add_offset(x_reg, x_reg, offset))
        return lines
    if value == "zeroinitializer":
        if expected_type.is_fp:
            return emit_fp_constant(expected_type, reg, "0.000000e+00")
        if hfa is not None:
            return [
                f"  fmov {'s' if member_type.width <= 32 else 'd'}"
                f"{reg_index + member_index}, "
                f"{'wzr' if member_type.width <= 32 else 'xzr'}"
                for member_index, (member_type, _offset) in enumerate(hfa)
            ]
        if not is_aggregate:
            return [emitted_movewide_instruction_line("movz", reg, 0)]
        return [
            emitted_movewide_instruction_line("movz", zero_reg, 0)
            for zero_reg in regs
        ]
    if parsed_function_has_alloca_slot(func, value):
        if not expected_type.is_ptr:
            raise BackendUnavailable(
                f"self backend cannot use alloca address {value!r} as non-pointer in {func.name!r}"
            )
        if func.indexed_slot_projection:
            value_id = parsed_function_alloca_value_id(func, value)
            return emit_add_offset(
                reg, "x29", -func.indexed_kernel.alloca_offset(value_id)
            )
        return emit_add_offset(reg, "x29", -func.alloca_slots[value].offset)
    allocated_reg = allocated_register_name(func, value, expected_type)
    if allocated_reg is not None:
        if allocated_reg == reg:
            return []
        return [emitted_move_register_line(reg, allocated_reg)]
    # Most values use consistent native string hashes.  Take that O(1) path
    # before classifying literals, while reserving the linear false-hash
    # recovery below for values that are not known constants or globals.
    if func.indexed_slot_projection:
        slot_id = parsed_function_value_slot_id(func, value)
        if slot_id >= 0:
            kernel = func.indexed_kernel
            return load_slot_to_value_regs_parts(
                kernel.slot_offset(slot_id),
                kernel.type_desc(kernel.slot_type_id(slot_id)),
                reg_index,
            )
    else:
        slot = func.value_slots.get(value)
        if slot is not None:
            return load_slot_to_value_regs(slot, reg_index)
    if expected_type.is_fp and (value.startswith("0x") or is_float_literal(value)):
        return emit_fp_constant(expected_type, reg, value)
    const_value = const_int_from_value(value)
    if const_value is not None:
        if expected_type.is_fp:
            raise BackendUnavailable(
                f"self backend does not yet support immediate floating constants: {value!r}"
            )
        return emit_const_to_reg(expected_type, reg, const_value)
    if is_aggregate_literal_value(value):
        if not (expected_type.is_array or expected_type.is_struct):
            raise BackendUnavailable(
                f"self backend aggregate literal used as non-aggregate in {func.name!r}: {value!r}"
            )
        try:
            literal_bytes = aggregate_literal_to_bytes(expected_type, value)
        except BackendUnavailable:
            if module_symbols is None:
                raise
            return _materialize_symbolic_ptr_array_literal_to_regs(
                func, expected_type, value, regs, module_symbols
            )
        lines: list[str] = []
        if hfa is not None:
            for member_index, (member_type, member_offset) in enumerate(hfa):
                prefix = "s" if member_type.width <= 32 else "d"
                hfa_reg = f"{prefix}{reg_index + member_index}"
                member_size = member_type.slot_size
                member_bits = int.from_bytes(
                    literal_bytes[member_offset : member_offset + member_size],
                    "little",
                )
                int_type = TypeDesc("int", 32 if member_size <= 4 else 64)
                int_reg = "w12" if member_size <= 4 else "x12"
                lines.extend(emit_const_to_reg(int_type, int_reg, member_bits))
                lines.append(f"  fmov {hfa_reg}, {int_reg}")
            return lines
        offset = 0
        for chunk_reg, chunk_size in zip(regs, aggregate_reg_chunks(expected_type)):
            chunk_value = int.from_bytes(
                literal_bytes[offset : offset + chunk_size], "little"
            )
            lines.extend(
                emit_const_to_reg(
                    TypeDesc("int", 64 if chunk_size > 4 else 32),
                    chunk_reg,
                    chunk_value,
                )
            )
            offset += chunk_size
        return lines
    if value.startswith("gepconst:"):
        if not expected_type.is_ptr:
            raise BackendUnavailable(
                f"self backend cannot use gep constant {value!r} as non-pointer in {func.name!r}"
            )
        _tag, base, offset_text = value.split(":", 2)
        lines = materialize_global_address(base, reg, module_symbols)
        offset = int(offset_text)
        if offset:
            lines.extend(emit_add_offset(reg, reg, offset))
        return lines
    if value.startswith("gep0:"):
        if not expected_type.is_ptr:
            raise BackendUnavailable(
                f"self backend cannot use gep constant {value!r} as non-pointer in {func.name!r}"
            )
        return materialize_global_address(value.split(":", 1)[1], reg, module_symbols)
    if value.startswith("@"):
        if not expected_type.is_ptr:
            raise BackendUnavailable(
                f"self backend cannot use global symbol {value!r} as non-pointer in {func.name!r}"
            )
        return materialize_global_address(value[1:], reg, module_symbols)
    slot_id = parsed_function_value_slot_id(func, value)
    if slot_id >= 0:
        if func.indexed_slot_projection:
            kernel = func.indexed_kernel
            return load_slot_to_value_regs_parts(
                kernel.slot_offset(slot_id),
                kernel.type_desc(kernel.slot_type_id(slot_id)),
                reg_index,
            )
        return load_value_slot_to_value_regs(func, value, reg_index)
    indexed_kernel = get_indexed_function_kernel(func)
    indexed_value_id = indexed_kernel.value_id(value)
    value_is_used = (
        indexed_value_id >= 0
        and indexed_kernel.value_is_used(indexed_value_id)
    )
    value_has_type = text_key_mapping_get(func.value_types, value) is not None
    slot_count = (
        sum(1 for slot_id in indexed_kernel.value_slot_ids if slot_id >= 0)
        if func.indexed_slot_projection
        else len(func.value_slots)
    )
    raise BackendUnavailable(
        f"self backend does not know how to materialize value {value!r} in {func.name!r} "
        f"(used={value_is_used}, typed={value_has_type}, slots={slot_count})"
    )
