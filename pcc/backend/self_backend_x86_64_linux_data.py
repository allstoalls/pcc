from __future__ import annotations

"""x86_64 Linux global/data emission helpers for the self backend."""

import re

from . import BackendUnavailable
from .self_backend_ir import GlobalDef, TypeDesc, _align_to
from .self_backend_literals import fp_bitcast_initializer_bits, wide_int_quad_lines
from .self_backend_module_symbols import PreparedModuleSymbols
from .wide_float import encode_float_bits, float_bytes
from .self_backend_parse import (
    decode_global_name,
    decode_value_token,
    decode_llvm_c_string,
    is_hex_literal,
    parse_constant_gep,
    split_top_level,
    strip_typed_initializer,
)

_RESERVED_ASM_SYMBOLS = frozenset(
    {
        "al",
        "ah",
        "ax",
        "eax",
        "rax",
        "bl",
        "bh",
        "bx",
        "ebx",
        "rbx",
        "cl",
        "ch",
        "cx",
        "ecx",
        "rcx",
        "dl",
        "dh",
        "dx",
        "edx",
        "rdx",
        "si",
        "esi",
        "rsi",
        "di",
        "edi",
        "rdi",
        "bp",
        "ebp",
        "rbp",
        "sp",
        "esp",
        "rsp",
        "fs",
        "gs",
        "eq",
        "ne",
        "lt",
        "le",
        "gt",
        "ge",
        "and",
        "or",
        "not",
        "mod",
        "shl",
        "shr",
    }
    | {f"r{i}" for i in range(8, 16)}
    | {f"r{i}d" for i in range(8, 16)}
    | {f"r{i}w" for i in range(8, 16)}
    | {f"r{i}b" for i in range(8, 16)}
    | {f"xmm{i}" for i in range(32)}
)

_X86_TLS_DIAGNOSTIC_PREFIX = "self-x86_64-linux ELF TLS lowering"
_SUPPORTED_TLS_MODELS = frozenset({"default", "initialexec"})
_SUPPORTED_TLS_PREFIX_WORDS = frozenset(
    {"external", "dso_local", "internal", "private", "local_unnamed_addr", "unnamed_addr"}
)
_THREAD_LOCAL_PREFIX_RE = re.compile(r"\bthread_local(?:\([^()]*\))?")


def asm_symbol(name: str, module_symbols: PreparedModuleSymbols) -> str:
    if name in module_symbols.internal_symbols:
        return f"{module_symbols.internal_prefix}{name}"
    if (name in module_symbols.defined_symbols or name in module_symbols.thread_local_symbols) and name.lower() in _RESERVED_ASM_SYMBOLS:
        return f"__pcc_sym_{name}"
    return name


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
        if init.startswith("bitcast"):
            decoded = decode_value_token(init, type_context=module_symbols.type_context)
            if decoded == init or decoded.startswith("cexpr:"):
                raise BackendUnavailable(
                    f"x86_64 self backend does not support pointer bitcast global initializer for {global_name!r}: {init!r}"
                )
            return emit_scalar_initializer(ty, decoded, global_name, module_symbols)
        if init.startswith("gep0:"):
            return [f"  .quad {asm_symbol(init.split(':', 1)[1], module_symbols)}"]
        if init.startswith("gepconst:"):
            base, offset_text = init.split(":", 2)[1:]
            offset = int(offset_text)
            suffix = "" if offset == 0 else f"+{offset}"
            return [f"  .quad {asm_symbol(base, module_symbols)}{suffix}"]
        if init.startswith("getelementptr"):
            base, offset = parse_constant_gep(init, type_context=module_symbols.type_context)
            suffix = "" if offset == 0 else f"+{offset}"
            return [f"  .quad {asm_symbol(base, module_symbols)}{suffix}"]
        if init.startswith("@"):
            return [f"  .quad {asm_symbol(decode_global_name(init), module_symbols)}"]
        if init.startswith("inttoptrconst:"):
            return [f"  .quad {int(init.split(':', 1)[1])}"]
        if init.startswith("inttoptr"):
            decoded = decode_value_token(init, type_context=module_symbols.type_context)
            if decoded.startswith("inttoptrconst:"):
                return [f"  .quad {int(decoded.split(':', 1)[1])}"]
            raise BackendUnavailable(
                f"x86_64 self backend does not support non-constant inttoptr global initializer for {global_name!r}: {init!r}"
            )
        return [f"  .quad {int(init)}"]
    if ty.is_int:
        if ty.width <= 8:
            return [f"  .byte {int(init)}"]
        if ty.width <= 16:
            return [f"  .short {int(init)}"]
        if ty.width <= 32:
            return [f"  .long {int(init)}"]
        if ty.width > 64:
            return wide_int_quad_lines(init, ty.slot_size)
        return [f"  .quad {int(init)}"]
    if ty.is_fp:
        if ty.width == 80:
            return emit_byte_data(float_bytes(init, 80))
        if is_hex_literal(init):
            bits = int(init, 16)
            if ty.width <= 32:
                fp32_bits = encode_float_bits(init, 32)
                return [f"  .long {fp32_bits}"]
            if ty.width <= 64:
                return [f"  .quad {bits}"]
        bitcast = fp_bitcast_initializer_bits(init)
        if bitcast is not None:
            width, bits = bitcast
            if width == 32 and ty.width <= 32:
                return [f"  .long {bits}"]
            if width == 64 and ty.width == 64:
                return [f"  .quad {bits}"]
        if ty.width <= 32:
            return [f"  .float {init}"]
        return [f"  .double {init}"]
    raise BackendUnavailable(
        f"self backend does not support scalar global initializer for {global_name!r}: {ty.describe()}"
    )


def emit_zero_fill(size: int) -> list[str]:
    if size <= 0:
        return []
    return [f"  .zero {size}"]


def emit_byte_data(data: bytes) -> list[str]:
    if not data:
        return []
    lines: list[str] = []
    for start in range(0, len(data), 16):
        chunk = ", ".join(str(byte) for byte in data[start : start + 16])
        lines.append(f"  .byte {chunk}")
    return lines


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
                    f"x86_64 self backend expected {ty.count} bytes in c-string initializer for {global_name!r}, got {len(data)}"
                )
            return emit_byte_data(data)
        # LLVM vector types (`<N x T>`) are parsed into array TypeDescs, but
        # their constant initializers use angle-bracket delimiters
        # (`<i32 1, i32 2, ...>`) rather than the square-bracket array form.
        # Normalize a top-level vector literal to the array literal form so the
        # element loop below emits each lane exactly like an array element.
        # This mirrors aggregate_literal_to_bytes() in self_backend_parse.py.
        if init.startswith("<") and init.endswith(">"):
            init = "[" + init[1:-1].strip() + "]"
        if not (init.startswith("[") and init.endswith("]")):
            raise BackendUnavailable(
                f"x86_64 self backend expected array initializer for {global_name!r}, got {init!r}"
            )
        body = init[1:-1].strip()
        items = [] if not body else split_top_level(body)
        if len(items) != ty.count:
            raise BackendUnavailable(
                f"x86_64 self backend expected {ty.count} array items for {global_name!r}, got {len(items)}"
            )
        lines: list[str] = []
        stride = _align_to(ty.elem.slot_size, ty.elem.align)
        for item in items:
            lines.extend(emit_typed_initializer(ty.elem, strip_typed_initializer(item, type_context=module_symbols.type_context), global_name, module_symbols))
            lines.extend(emit_zero_fill(stride - ty.elem.slot_size))
        return lines
    if ty.is_struct:
        if not (init.startswith("{") and init.endswith("}")):
            raise BackendUnavailable(
                f"x86_64 self backend expected struct initializer for {global_name!r}, got {init!r}"
            )
        body = init[1:-1].strip()
        items = [] if not body else split_top_level(body)
        if len(items) != len(ty.fields):
            raise BackendUnavailable(
                f"x86_64 self backend expected {len(ty.fields)} struct fields for {global_name!r}, got {len(items)}"
            )
        lines: list[str] = []
        offset = 0
        for index, (field_ty, item) in enumerate(zip(ty.fields, items)):
            field_offset = ty.field_offset(index)
            lines.extend(emit_zero_fill(field_offset - offset))
            lines.extend(emit_typed_initializer(field_ty, strip_typed_initializer(item, type_context=module_symbols.type_context), global_name, module_symbols))
            offset = field_offset + field_ty.slot_size
        lines.extend(emit_zero_fill(ty.slot_size - offset))
        return lines
    return emit_scalar_initializer(ty, init, global_name, module_symbols)


def emit_global_initializer(global_: GlobalDef, module_symbols: PreparedModuleSymbols) -> str:
    return "\n".join(
        emit_typed_initializer(global_.type, global_.initializer, global_.name, module_symbols)
    )


def _tls_initializer_is_zero(global_: GlobalDef) -> bool:
    init = global_.initializer.strip()
    if global_.type.is_int:
        if init in {"false", "zeroinitializer"}:
            return True
        if init in {"true", "poison", "undef"}:
            return False
        try:
            return int(init) == 0
        except ValueError:
            return False
    if global_.type.is_ptr:
        if init in {"null", "zeroinitializer"}:
            return True
        if init.startswith("inttoptrconst:"):
            try:
                return int(init.split(":", 1)[1]) == 0
            except ValueError:
                return False
    return False


def validate_x86_tls_global(global_: GlobalDef) -> None:
    if not global_.tls_model:
        return
    if global_.tls_model not in _SUPPORTED_TLS_MODELS:
        supported = ", ".join(sorted(_SUPPORTED_TLS_MODELS))
        raise BackendUnavailable(
            f"{_X86_TLS_DIAGNOSTIC_PREFIX} does not support model "
            f"{global_.tls_model!r} for {global_.name!r}; supported models: {supported}"
        )
    remaining_prefix = _THREAD_LOCAL_PREFIX_RE.sub(" ", global_.ir_prefix)
    unsupported_prefix = sorted(
        word
        for word in remaining_prefix.split()
        if word not in _SUPPORTED_TLS_PREFIX_WORDS
    )
    if unsupported_prefix:
        raise BackendUnavailable(
            f"{_X86_TLS_DIAGNOSTIC_PREFIX} does not support global prefix "
            f"{unsupported_prefix!r} for {global_.name!r}"
        )
    unsupported_attributes = [
        attribute
        for attribute in global_.trailing_attributes
        if not re.fullmatch(r"align\s+\d+", attribute)
    ]
    if unsupported_attributes:
        raise BackendUnavailable(
            f"{_X86_TLS_DIAGNOSTIC_PREFIX} does not support attributes "
            f"{unsupported_attributes!r} for {global_.name!r}"
        )
    align_attributes = [
        attribute
        for attribute in global_.trailing_attributes
        if re.fullmatch(r"align\s+\d+", attribute)
    ]
    if len(align_attributes) > 1:
        raise BackendUnavailable(
            f"{_X86_TLS_DIAGNOSTIC_PREFIX} found duplicate align attributes "
            f"for {global_.name!r}"
        )
    alignment = global_.alignment or global_.type.align
    if alignment <= 0 or alignment > 8 or alignment & (alignment - 1):
        raise BackendUnavailable(
            f"{_X86_TLS_DIAGNOSTIC_PREFIX} requires power-of-two alignment at "
            f"most 8 for {global_.name!r}, got {alignment}"
        )
    if global_.is_constant:
        raise BackendUnavailable(
            f"{_X86_TLS_DIAGNOSTIC_PREFIX} does not support thread-local "
            f"constants for {global_.name!r}"
        )
    if not global_.initializer:
        return
    if global_.type.is_int:
        if global_.type.width <= 0 or global_.type.width > 64:
            raise BackendUnavailable(
                f"{_X86_TLS_DIAGNOSTIC_PREFIX} supports integer widths up to "
                f"64 bits for {global_.name!r}, got {global_.type.describe()}"
            )
        try:
            if global_.initializer not in {"true", "false", "zeroinitializer"}:
                int(global_.initializer)
        except ValueError as exc:
            raise BackendUnavailable(
                f"{_X86_TLS_DIAGNOSTIC_PREFIX} requires a concrete integer "
                f"initializer for {global_.name!r}, got {global_.initializer!r}"
            ) from exc
        return
    if global_.type.is_ptr and _tls_initializer_is_zero(global_):
        return
    raise BackendUnavailable(
        f"{_X86_TLS_DIAGNOSTIC_PREFIX} supports scalar integers and null "
        f"pointers only for {global_.name!r}, got {global_.type.describe()} "
        f"initialized by {global_.initializer!r}"
    )


def validate_x86_tls_globals(globals_: list[GlobalDef]) -> None:
    for global_ in globals_:
        validate_x86_tls_global(global_)


def emit_globals(
    globals_: list[GlobalDef], module_symbols: PreparedModuleSymbols
) -> list[str]:
    lines: list[str] = []
    for global_ in globals_:
        if not global_.initializer:
            validate_x86_tls_global(global_)
            symbol = asm_symbol(global_.name, module_symbols)
            lines.extend([f".globl {symbol}", f".type {symbol}, @tls_object"])
            continue
        if global_.name in ("llvm.global_ctors", "llvm.global_dtors"):
            from .self_backend_aarch64_darwin_data import global_ctor_entries
            family = "fini" if global_.name == "llvm.global_dtors" else "init"
            for priority, ordinal, target in global_ctor_entries(global_):
                lines.extend([f".section .{family}_array.{priority:010d}", ".p2align 3",
                              "  .quad " + asm_symbol(target, module_symbols)])
            continue
        if not (
            global_.type.is_int
            or global_.type.is_ptr
            or global_.type.is_fp
            or global_.type.is_array
            or global_.type.is_struct
        ):
            raise BackendUnavailable(
                f"x86_64 self backend global type not translated yet for {global_.name!r}: {global_.type.describe()}"
            )
        validate_x86_tls_global(global_)
        tls_zero = bool(global_.tls_model) and _tls_initializer_is_zero(global_)
        if global_.tls_model:
            section = (
                '.section .tbss,"awT",@nobits'
                if tls_zero
                else '.section .tdata,"awT",@progbits'
            )
        else:
            section = ".section .rodata" if global_.is_constant else ".data"
        lines.append(section)
        alignment = (
            global_.alignment or global_.type.align
            if global_.tls_model
            else global_.type.align
        )
        lines.append(f".p2align {max(alignment.bit_length() - 1, 0)}")
        symbol = asm_symbol(global_.name, module_symbols)
        if not global_.is_internal:
            lines.append(f".globl {symbol}")
        lines.append(f".type {symbol}, @object")
        lines.append(f".size {symbol}, {global_.type.slot_size}")
        lines.append(f"{symbol}:")
        if tls_zero:
            lines.extend(emit_zero_fill(global_.type.slot_size))
        else:
            lines.append(emit_global_initializer(global_, module_symbols))
        if global_.type.slot_size == 0:
            # An empty struct/union object still needs a distinct address.
            lines.append("  .zero 1")
        lines.append("")
    return lines
