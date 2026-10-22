from __future__ import annotations

from . import BackendUnavailable
from .self_backend_aarch64_darwin_abi import (
    aggregate_returned_indirect,
    aggregate_returned_indirect_indexed,
)
from .self_backend_aarch64_darwin_materialize import (
    materialize_aggregate_storage_address,
    materialize_value,
    materialize_scalar_value_indexed,
    store_large_aggregate_literal_to_address,
)
from .self_backend_aarch64_darwin_slots import (
    copy_address_to_address,
    load_slot_to_reg,
    load_slot_to_reg_parts,
    zero_address,
)
from .self_backend_aarch64_darwin_terminators import emit_epilogue
from .self_backend_ir import ParsedFunction, TypeDesc
from .self_backend_module_symbols import PreparedModuleSymbols
from .self_backend_parse import is_aggregate_literal_value
from .self_backend_kernel import (
    IndexedFunctionKernel,
    TYPE_KIND_FP,
    TYPE_KIND_INT,
    TYPE_KIND_PTR,
    _scalar_window_is_complete,
    get_indexed_function_kernel,
)
from .self_backend_value_arena import CompilerInt4


def emit_return_terminator(
    func: ParsedFunction,
    *,
    ret_type: TypeDesc,
    value: str,
    module_symbols: PreparedModuleSymbols,
    value_id: int = -1,
) -> list[str]:
    if aggregate_returned_indirect(ret_type):
        kernel = get_indexed_function_kernel(func)
        hidden_slot_id = kernel.hidden_sret_slot_id
        if hidden_slot_id < 0 and func.hidden_sret_slot is None:
            raise BackendUnavailable(
                f"self backend missing hidden sret slot for large aggregate return in {func.name!r}"
            )
        if func.indexed_slot_projection and hidden_slot_id >= 0:
            lines = load_slot_to_reg_parts(
                kernel.slot_offset(hidden_slot_id),
                kernel.type_desc(kernel.slot_type_id(hidden_slot_id)),
                "x12",
            )
        else:
            lines = load_slot_to_reg(func.hidden_sret_slot, "x12")
        if value == "zeroinitializer":
            lines.extend(zero_address("x12", ret_type.slot_size))
            lines.extend(emit_epilogue(func))
            return lines
        if is_aggregate_literal_value(value):
            lines.extend(
                store_large_aggregate_literal_to_address(
                    ret_type,
                    value,
                    "x12",
                    module_symbols=module_symbols,
                )
            )
            lines.extend(emit_epilogue(func))
            return lines
        lines.extend(
            materialize_aggregate_storage_address(
                func,
                value,
                ret_type,
                "x13",
                value_id=value_id,
            )
        )
        lines.extend(copy_address_to_address("x13", "x12", ret_type.slot_size))
        lines.extend(emit_epilogue(func))
        return lines
    lines = materialize_value(
        func,
        value,
        ret_type,
        0,
        module_symbols,
        value_id=value_id,
    )
    lines.extend(emit_epilogue(func))
    return lines


def emit_return_terminator_indexed(
    func: ParsedFunction,
    *,
    kernel: IndexedFunctionKernel,
    type_id: int,
    value_ref: int,
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    value = kernel.terminator_value(value_ref)
    header_is_scalar = False
    if type(kernel) is IndexedFunctionKernel:
        header_scalar_id = type_id
        header_is_scalar = _scalar_window_is_complete(
            kernel.type_scalars, header_scalar_id, 12, 0
        )
        if header_is_scalar:
            header_scalar = kernel.type_scalars.get_unchecked(
                header_scalar_id * 12 + 0
            )
        else:
            header: CompilerInt4 = kernel.type_header(header_scalar_id)
    else:
        header: CompilerInt4 = kernel.type_header(type_id)
    if aggregate_returned_indirect_indexed(kernel, type_id):
        return emit_return_terminator(
            func,
            ret_type=kernel.type_desc(type_id),
            value=value,
            module_symbols=module_symbols,
            value_id=value_ref,
        )
    if (header_scalar if header_is_scalar else header.first) in (TYPE_KIND_INT, TYPE_KIND_FP, TYPE_KIND_PTR):
        lines = materialize_scalar_value_indexed(
            func,
            kernel,
            value,
            type_id,
            0,
            module_symbols,
            value_id=value_ref,
        )
        lines.extend(emit_epilogue(func))
        return lines
    return emit_return_terminator(
        func,
        ret_type=kernel.type_desc(type_id),
        value=value,
        module_symbols=module_symbols,
        value_id=value_ref,
    )
