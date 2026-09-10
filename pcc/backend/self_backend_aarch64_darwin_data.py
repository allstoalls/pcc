from __future__ import annotations

"""AArch64 Darwin global/data emission helpers for the self backend."""

import re

from . import BackendUnavailable
from .self_backend_aarch64_darwin_regs import align_pow2
from .self_backend_aarch64_darwin_symbols import asm_symbol
from .self_backend_ir import GlobalDef, TypeDesc, _align_to
from .self_backend_float_bits import bits_to_float64, float32_to_bits
from .self_backend_module_symbols import PreparedModuleSymbols
from .self_backend_parse import (
    decode_global_name,
    decode_value_token,
    decode_llvm_c_string,
    is_hex_literal,
    parse_constant_gep,
    split_top_level,
    strip_typed_initializer,
)


_GLOBAL_CTORS_NAME = "llvm.global_ctors"


def _emit_global_ctors(
    global_: GlobalDef,
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    """Lower LLVM's appending ctor array to Mach-O initializer pointers."""
    ty = global_.type
    if (
        not ty.is_array
        or ty.elem is None
        or not ty.elem.is_struct
        or len(ty.elem.fields) != 3
        or not ty.elem.fields[0].is_int
        or ty.elem.fields[0].width != 32
        or not ty.elem.fields[1].is_ptr
        or not ty.elem.fields[2].is_ptr
    ):
        raise BackendUnavailable(
            "self backend expected llvm.global_ctors as "
            "[N x { i32, ptr, ptr }]"
        )

    initializer = strip_typed_initializer(global_.initializer)
    if initializer == "zeroinitializer":
        return []
    if not (initializer.startswith("[") and initializer.endswith("]")):
        raise BackendUnavailable(
            "self backend expected an array initializer for llvm.global_ctors"
        )
    body = initializer[1:-1].strip()
    raw_entries = [] if not body else split_top_level(body)
    if len(raw_entries) != ty.count:
        raise BackendUnavailable(
            "self backend llvm.global_ctors count does not match its type"
        )

    entries: list[tuple[int, int, str]] = []
    for ordinal, raw_entry in enumerate(raw_entries):
        record = strip_typed_initializer(raw_entry)
        if not (record.startswith("{") and record.endswith("}")):
            raise BackendUnavailable(
                f"bad llvm.global_ctors record {raw_entry!r}"
            )
        fields = split_top_level(record[1:-1].strip())
        if len(fields) != 3:
            raise BackendUnavailable(
                f"bad llvm.global_ctors record {raw_entry!r}"
            )
        priority_text = decode_value_token(strip_typed_initializer(fields[0]))
        target = decode_value_token(strip_typed_initializer(fields[1]))
        associated = decode_value_token(strip_typed_initializer(fields[2]))
        try:
            priority = int(priority_text, 0)
        except ValueError as exc:
            raise BackendUnavailable(
                f"non-integer llvm.global_ctors priority {priority_text!r}"
            ) from exc
        if not -(1 << 31) <= priority < (1 << 32):
            raise BackendUnavailable(
                f"llvm.global_ctors priority outside i32: {priority_text!r}"
            )
        priority &= 0xFFFFFFFF
        if not target.startswith("@"):
            raise BackendUnavailable(
                f"non-symbol llvm.global_ctors target {target!r}"
            )
        if associated != "null":
            raise BackendUnavailable(
                "associated-data llvm.global_ctors entries are not proven"
            )
        entries.append((priority, ordinal, target[1:]))

    entries.sort(key=lambda entry: (entry[0], entry[1]))
    if not entries:
        return []
    lines = [
        ".section __DATA,__mod_init_func,mod_init_funcs",
        ".p2align 3",
    ]
    for _priority, _ordinal, target_name in entries:
        lines.append(f"  .quad {asm_symbol(target_name, module_symbols)}")
    lines.append("")
    return lines


def emit_globals(
    globals_: list[GlobalDef],
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    lines: list[str] = []
    for global_ in globals_:
        if global_.name == _GLOBAL_CTORS_NAME:
            lines.extend(_emit_global_ctors(global_, module_symbols))
            continue
        section = (
            ".section __DATA,__const"
            if global_.is_constant
            else ".section __DATA,__data"
        )
        lines.append(section)
        lines.append(f".p2align {align_pow2(global_.type.align)}")
        if not global_.is_internal:
            lines.append(f".globl {asm_symbol(global_.name, module_symbols)}")
        lines.append(f"{asm_symbol(global_.name, module_symbols)}:")
        lines.append(emit_global_initializer(global_, module_symbols))
        lines.append("")
    return lines


def emit_byte_data(data: bytes) -> list[str]:
    if not data:
        return []
    lines: list[str] = []
    for start in range(0, len(data), 16):
        chunk = ", ".join(str(byte) for byte in data[start : start + 16])
        lines.append(f"  .byte {chunk}")
    return lines


def emit_zero_fill(size: int) -> list[str]:
    if size <= 0:
        return []
    return [f"  .space {size}"]


def emit_scalar_initializer(
    ty: TypeDesc,
    init: str,
    global_name: str,
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    if init in {"null", "poison", "undef", "false"}:
        init = "0"
    elif init == "true":
        init = "1"
    if ty.is_ptr:
        if init.startswith("gep0:"):
            return [f"  .quad {asm_symbol(init.split(':', 1)[1], module_symbols)}"]
        if init.startswith("gepconst:"):
            base, offset_text = init.split(":", 2)[1:]
            offset = int(offset_text)
            suffix = "" if offset == 0 else f"+{offset}"
            return [f"  .quad {asm_symbol(base, module_symbols)}{suffix}"]
        if init.startswith("getelementptr"):
            base, offset = parse_constant_gep(init)
            suffix = "" if offset == 0 else f"+{offset}"
            return [f"  .quad {asm_symbol(base, module_symbols)}{suffix}"]
        if init.startswith("inttoptr"):
            decoded = decode_value_token(init)
            if decoded.startswith("inttoptrconst:"):
                return [f"  .quad {int(decoded.split(':', 1)[1])}"]
            raise BackendUnavailable(
                f"self backend does not support non-constant inttoptr global initializer for {global_name!r}: {init!r}"
            )
        if init.startswith("inttoptrconst:"):
            return [f"  .quad {int(init.split(':', 1)[1])}"]
        if init.startswith("bitcast"):
            # A pointer-to-pointer bitcast is the identity, and C static
            # initializers produce it constantly (`PyObject *const py_None =
            # (PyObject *)&py_none_storage;`). This emitter receives the raw
            # initializer text, so it has to decode before deciding, the way
            # the inttoptr case above does; otherwise the token fell through
            # to `int(init)` and the whole translation unit failed with
            # "invalid literal for int()".
            decoded = decode_value_token(init)
            if decoded.startswith("@"):
                return [f"  .quad {asm_symbol(decoded[1:], module_symbols)}"]
            if decoded.startswith("gep0:"):
                return [
                    f"  .quad {asm_symbol(decoded.split(':', 1)[1], module_symbols)}"
                ]
            if decoded.startswith("gepconst:"):
                base, offset_text = decoded.split(":", 2)[1:]
                offset = int(offset_text)
                suffix = "" if offset == 0 else f"+{offset}"
                return [
                    f"  .quad {asm_symbol(base, module_symbols)}{suffix}"
                ]
            if decoded.startswith("inttoptrconst:"):
                return [f"  .quad {int(decoded.split(':', 1)[1])}"]
            raise BackendUnavailable(
                "self backend does not support this bitcast global initializer "
                f"for {global_name!r}: {init!r}"
            )
        if init.startswith("@"):
            return [f"  .quad {asm_symbol(decode_global_name(init), module_symbols)}"]
        return [f"  .quad {int(init)}"]
    if ty.is_int:
        if ty.width <= 8:
            return [f"  .byte {int(init)}"]
        if ty.width <= 16:
            return [f"  .short {int(init)}"]
        if ty.width <= 32:
            return [f"  .long {int(init)}"]
        return [f"  .quad {int(init)}"]
    if ty.is_fp:
        if is_hex_literal(init):
            bits = int(init, 16)
            if ty.width <= 32:
                value = bits_to_float64(bits)
                fp32_bits = float32_to_bits(value)
                return [f"  .long {fp32_bits}"]
            if ty.width <= 64:
                return [f"  .quad {bits}"]
        return [f"  .float {init}" if ty.width <= 32 else f"  .double {init}"]
    raise BackendUnavailable(
        f"self backend does not support scalar global initializer for {global_name!r}: {ty.describe()}"
    )


def emit_typed_initializer(
    ty: TypeDesc,
    init: str,
    global_name: str,
    module_symbols: PreparedModuleSymbols,
) -> list[str]:
    init = init.strip()
    init = re.sub(r",\s*align\s+\d+$", "", init)
    if init == "zeroinitializer":
        return emit_zero_fill(ty.slot_size)
    if init in {"poison", "undef"} and (ty.is_array or ty.is_struct):
        return emit_zero_fill(ty.slot_size)
    if ty.is_array:
        assert ty.elem is not None
        if ty.elem.is_int and ty.elem.width == 8 and init.startswith('c"'):
            data = decode_llvm_c_string(init)
            if len(data) != ty.count:
                raise BackendUnavailable(
                    f"self backend expected {ty.count} bytes in c-string initializer for {global_name!r}, got {len(data)}"
                )
            return emit_byte_data(data)
        if not (init.startswith("[") and init.endswith("]")):
            raise BackendUnavailable(
                f"self backend expected array initializer for {global_name!r}, got {init!r}"
            )
        body = init[1:-1].strip()
        items = [] if not body else split_top_level(body)
        if len(items) != ty.count:
            raise BackendUnavailable(
                f"self backend expected {ty.count} array items for {global_name!r}, got {len(items)}"
            )
        lines: list[str] = []
        stride = _align_to(ty.elem.slot_size, ty.elem.align)
        for item in items:
            lines.extend(
                emit_typed_initializer(
                    ty.elem, strip_typed_initializer(item), global_name, module_symbols
                )
            )
            lines.extend(emit_zero_fill(stride - ty.elem.slot_size))
        return lines
    if ty.is_struct:
        if not (init.startswith("{") and init.endswith("}")):
            raise BackendUnavailable(
                f"self backend expected struct initializer for {global_name!r}, got {init!r}"
            )
        body = init[1:-1].strip()
        items = [] if not body else split_top_level(body)
        if len(items) != len(ty.fields):
            raise BackendUnavailable(
                f"self backend expected {len(ty.fields)} struct fields for {global_name!r}, got {len(items)}"
            )
        lines: list[str] = []
        offset = 0
        for index, (field_ty, item) in enumerate(zip(ty.fields, items)):
            field_offset = ty.field_offset(index)
            lines.extend(emit_zero_fill(field_offset - offset))
            lines.extend(
                emit_typed_initializer(
                    field_ty, strip_typed_initializer(item), global_name, module_symbols
                )
            )
            offset = field_offset + field_ty.slot_size
        lines.extend(emit_zero_fill(ty.slot_size - offset))
        return lines
    return emit_scalar_initializer(ty, init, global_name, module_symbols)


def emit_global_initializer(
    global_: GlobalDef, module_symbols: PreparedModuleSymbols
) -> str:
    return "\n".join(
        emit_typed_initializer(
            global_.type, global_.initializer, global_.name, module_symbols
        )
    )
