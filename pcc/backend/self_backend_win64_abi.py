"""Microsoft x64 ABI boundary for the shared x86 instruction selector.

References: LLVM X86CallingConv.td and MCWin64EH.cpp. The four argument
positions are shared by GPR/XMM classes; every call reserves home space.
"""

from .self_backend_ir import TypeDesc, _align_to
from .self_backend_literals import const_int_from_value

GPRS = ("rcx", "rdx", "r8", "r9")

# The platform frame area above the outgoing arguments: saved rbx/rsi/rdi
# (0..23), transient r10 (24), saved xmm10/xmm11 (32..63), transient r11 (64),
# the nearbyint MXCSR pair (72..79) and the r12-r15 that
# x86_64_frame_registers keeps values in (80..111).
_TRANSIENT_R10_OFFSET = 24
_TRANSIENT_R11_OFFSET = 64
MXCSR_OFFSET = 72
PLATFORM_AREA_SIZE = 112


def indirect(ty) -> bool:
    return (ty.is_struct or ty.is_array) and ty.slot_size not in (1, 2, 4, 8)


def prepare_frame(func) -> None:
    from .self_backend_kernel import get_indexed_function_kernel
    kernel = get_indexed_function_kernel(func)
    outgoing = max(32, func.frame_size)  # also bounds parallel phi scratch
    for call_id in range(len(kernel.call_scalars) // 8):
        header = kernel.call_header(call_id)
        span = kernel.call_span(call_id)
        hidden = 1 if indirect(kernel.type_desc(header.first)) else 0
        size = _align_to(max(32, (span.first + hidden) * 8), 16)
        for index in range(span.first):
            arg = kernel.call_arg(header.fourth + index)
            ty = kernel.type_desc(arg.first)
            if indirect(ty):
                size += _align_to(ty.slot_size, 16)
        outgoing = max(outgoing, size)
    func.platform_frame_extra = _align_to(outgoing, 16) + PLATFORM_AREA_SIZE


def outgoing_size(func) -> int:
    return func.platform_frame_extra - PLATFORM_AREA_SIZE


def frame_size(func) -> int:
    return func.frame_size + func.platform_frame_extra


def prologue(func):
    from .self_backend_x86_64_linux import (
        _asm_symbol, _store_reg_to_slot, _reg_alias, _copy_address_to_address,
        _slot_addr, _load_address_chunk_to_gp_reg,
    )
    symbol = _asm_symbol(func.name)
    size = frame_size(func)
    lines = ["", ".text", ".p2align 4, 0x90"]
    if func.is_global:
        lines.append(".globl " + symbol)
    lines.extend([f".type {symbol}, @function", f".seh_proc {symbol}", symbol + ":",
                  "  push rbp", "  .seh_pushreg rbp", "  mov rbp, rsp"])
    if size >= 4096:
        lines.extend([f"  mov eax, {size}", "  call __pcc_chkstk", "  sub rsp, rax"])
    else:
        lines.append(f"  sub rsp, {size}")
    lines.append(f"  .seh_stackalloc {size}")
    saved = outgoing_size(func)
    for reg, offset in (("rbx", 0), ("rsi", 8), ("rdi", 16)):
        lines.extend([f"  mov QWORD PTR [rsp + {saved + offset}], {reg}", f"  .seh_savereg {reg}, {saved + offset}"])
    for reg, offset in (("xmm10", 32), ("xmm11", 48)):
        lines.extend([f"  movdqu XMMWORD PTR [rsp + {saved + offset}], {reg}", f"  .seh_savexmm {reg}, {saved + offset}"])
    lines.append("  .seh_endprologue")
    if func.is_vararg:
        for index, reg in enumerate(GPRS):
            lines.append(f"  mov QWORD PTR [rbp + {16 + index * 8}], {reg}")
    start = 1 if func.hidden_sret_slot is not None else 0
    if start:
        slot = func.hidden_sret_slot
        lines.extend(_store_reg_to_slot("rcx", slot.offset, slot.type))
    for index, arg in enumerate(func.args, start):
        if arg.name not in func.value_slots:
            continue
        slot = func.value_slots[arg.name]
        aggregate = arg.type.is_array or arg.type.is_struct
        if indirect(arg.type):
            if index < 4:
                source = GPRS[index]
            else:
                lines.append(f"  mov r10, QWORD PTR [rbp + {16 + index * 8}]")
                source = "r10"
            lines.extend([f"  lea r11, {_slot_addr(slot.offset)}"])
            lines.extend(_copy_address_to_address(source, "r11", arg.type.slot_size))
        elif aggregate:
            if index < 4:
                reg = GPRS[index]
            else:
                lines.append(f"  lea r11, [rbp + {16 + index * 8}]")
                lines.extend(_load_address_chunk_to_gp_reg("r11", 0, arg.type.slot_size, "r10"))
                reg = "r10"
            scalar = TypeDesc("int", arg.type.slot_size * 8)
            lines.extend(_store_reg_to_slot(_reg_alias(reg, arg.type.slot_size), slot.offset, scalar))
        elif index < 4:
            reg = f"xmm{index}" if arg.type.is_fp else _reg_alias(GPRS[index], min(8, arg.type.slot_size))
            lines.extend(_store_reg_to_slot(reg, slot.offset, arg.type))
        else:
            from .self_backend_x86_64_linux import _load_from_address
            lines.append(f"  lea r11, [rbp + {16 + index * 8}]")
            reg = "xmm10" if arg.type.is_fp else _reg_alias("r10", min(8, arg.type.slot_size))
            lines.extend(_load_from_address("r11", reg, arg.type))
            lines.extend(_store_reg_to_slot(reg, slot.offset, arg.type))
    return lines


def epilogue(func):
    lines = []
    saved = outgoing_size(func)
    for reg, offset in (("xmm10", 32), ("xmm11", 48)):
        lines.append(f"  movdqu {reg}, XMMWORD PTR [rsp + {saved + offset}]")
    for reg, offset in (("rbx", 0), ("rsi", 8), ("rdi", 16)):
        lines.append(f"  mov {reg}, QWORD PTR [rsp + {saved + offset}]")
    lines.extend([f"  add rsp, {frame_size(func)}", "  pop rbp", "  ret"])
    return lines


def _direct_scalar_call_eligible(func, dest, ret_type, callee, is_indirect, args, is_vararg):
    from .self_backend_x86_64_linux import _is_constant_gep_value

    # Only destination-local materialization: no argument-register source or
    # scratch-borrowing expression may enter this path. Home space stays
    # reserved; the Win64 callee owns it and spills its registers if needed.
    if is_indirect or is_vararg or not 1 <= len(args) <= 4:
        return False
    if callee.startswith('llvm.'):
        return False
    if not (ret_type.is_void or ret_type.is_ptr or
            (ret_type.is_int and ret_type.width in (1, 8, 16, 32, 64))):
        return False
    for ty, value in args:
        if not (ty.is_ptr or (ty.is_int and ty.width in (1, 8, 16, 32, 64))):
            return False
        # Do not accept globals, TLS, GEPs, constant casts, poison/undef,
        # aggregate temporaries or arbitrary materializer fallback routes.
        if (value in ('poison', 'undef') or _is_constant_gep_value(value) or
                value.startswith('@') or value.startswith('inttoptrconst:')):
            return False
        if not ((ty.is_ptr and value in func.alloca_slots) or
                value in func.value_slots or
                value == 'null' or const_int_from_value(value) is not None):
            return False
    return True


def call(func, dest, ret_type, callee, is_indirect, args, is_vararg):
    from .self_backend_x86_64_linux import (
        _slot_addr, _materialize_value, _materialize_aggregate_value_address,
        _copy_address_to_address, _reg_alias, _store_reg_to_slot, _asm_symbol,
        _load_address_chunk_to_gp_reg,
    )
    from . import BackendUnavailable
    hidden = indirect(ret_type)
    if hidden and (dest is None or dest not in func.value_slots):
        raise BackendUnavailable("Win64 aggregate call needs result storage")
    first = 1 if hidden else 0
    argument_bytes = max(32, (len(args) + first) * 8)
    cursor = _align_to(argument_bytes, 16)
    copies = {}
    for index, (ty, value) in enumerate(args, first):
        if indirect(ty):
            copies[index] = cursor
            cursor += _align_to(ty.slot_size, 16)
    # Keep the complete outgoing frame even when scalar setup needs no staging.
    size = _align_to(cursor, 16)
    if size > outgoing_size(func):
        raise BackendUnavailable("Win64 outgoing arguments exceed the planned fixed frame")
    lines = []
    if _direct_scalar_call_eligible(
        func, dest, ret_type, callee, is_indirect, args, is_vararg,
    ):
        for index, (ty, value) in enumerate(args):
            register = _reg_alias(GPRS[index], min(8, ty.slot_size))
            lines.extend(_materialize_value(func, value, ty, register))
        lines.append("  call " + _asm_symbol(callee))
        if dest is not None and dest in func.value_slots and not ret_type.is_void:
            slot = func.value_slots[dest]
            register = _reg_alias("rax", min(8, ret_type.slot_size))
            lines.extend(_store_reg_to_slot(register, slot.offset, ret_type))
        return lines
    # Complex forms stage before filling argument registers: aggregate copies
    # and constant expressions may borrow rdx/rcx themselves.
    if hidden:
        lines.extend([f"  lea r10, {_slot_addr(func.value_slots[dest].offset)}", "  mov QWORD PTR [rsp], r10"])
    for index, (ty, value) in enumerate(args, first):
        if indirect(ty):
            lines.extend(_materialize_aggregate_value_address(func, value, ty, "r10"))
            lines.append(f"  lea r11, [rsp + {copies[index]}]")
            lines.extend(_copy_address_to_address("r10", "r11", ty.slot_size))
            lines.append(f"  lea r10, [rsp + {copies[index]}]")
        elif ty.is_struct or ty.is_array:
            lines.extend(_materialize_aggregate_value_address(func, value, ty, "r11"))
            lines.extend(_load_address_chunk_to_gp_reg("r11", 0, ty.slot_size, "r10"))
        elif ty.is_fp:
            lines.extend(_materialize_value(func, value, ty, "xmm10"))
            lines.append("  movq r10, xmm10" if ty.width == 64 else "  movd r10d, xmm10")
        else:
            lines.extend(_materialize_value(func, value, ty, _reg_alias("r10", min(8, ty.slot_size))))
        lines.append(f"  mov QWORD PTR [rsp + {index * 8}], r10")
    if hidden:
        lines.append("  mov rcx, QWORD PTR [rsp]")
    for index, (ty, value) in enumerate(args, first):
        if index >= 4:
            break
        if ty.is_fp:
            lines.append(f"  {'movsd' if ty.width == 64 else 'movss'} xmm{index}, {'QWORD' if ty.width == 64 else 'DWORD'} PTR [rsp + {index * 8}]")
        if not ty.is_fp or is_vararg:
            lines.append(f"  mov {GPRS[index]}, QWORD PTR [rsp + {index * 8}]")
    if is_indirect:
        lines.extend(_materialize_value(func, callee, TypeDesc("ptr"), "r11"))
        lines.append("  call r11")
    else:
        lines.append("  call " + _asm_symbol(callee))
    if dest is not None and dest in func.value_slots and not ret_type.is_void and not hidden:
        slot = func.value_slots[dest]
        if ret_type.is_struct or ret_type.is_array:
            lines.extend(_store_reg_to_slot(_reg_alias("rax", ret_type.slot_size), slot.offset,
                                            TypeDesc("int", ret_type.slot_size * 8)))
        else:
            reg = "xmm0" if ret_type.is_fp else _reg_alias("rax", min(8, ret_type.slot_size))
            lines.extend(_store_reg_to_slot(reg, slot.offset, ret_type))
    return lines


def vararg_start(func, ap):
    from .self_backend_x86_64_linux import _materialize_value
    first = len(func.args) + (1 if func.hidden_sret_slot is not None else 0)
    lines = _materialize_value(func, ap, TypeDesc("ptr"), "r10")
    lines.extend([f"  lea r11, [rbp + {16 + first * 8}]",
                  "  mov QWORD PTR [r10], r11"])
    return lines


def global_memory_access(name, reg, type_desc, scratch_base, store):
    from .self_backend_x86_64_linux import (
        _materialize_global_address, _load_from_address, _store_to_address,
    )

    # Unlike RIP-relative loads/stores, address materialization needs a GP
    # scratch. Preserve it, including when the value itself is an r11 alias.
    address_reg = "r10" if reg in ("r11", "r11d", "r11w", "r11b") else "r11"
    offset = _TRANSIENT_R10_OFFSET if address_reg == "r10" else _TRANSIENT_R11_OFFSET
    slot = scratch_base + offset
    lines = [f"  mov QWORD PTR [rsp + {slot}], {address_reg}"]
    lines.extend(_materialize_global_address(name, address_reg))
    if store:
        lines.extend(_store_to_address(address_reg, reg, type_desc))
    else:
        lines.extend(_load_from_address(address_reg, reg, type_desc))
    lines.append(f"  mov {address_reg}, QWORD PTR [rsp + {slot}]")
    return lines


def tls_address(symbol, reg, scratch_base):
    first = scratch_base + _TRANSIENT_R10_OFFSET
    second = scratch_base + _TRANSIENT_R11_OFFSET
    lines = [f"  mov QWORD PTR [rsp + {first}], r10", f"  mov QWORD PTR [rsp + {second}], r11",
             "  mov r10, QWORD PTR gs:88", "  movabs r11, OFFSET FLAT:_tls_index",
             "  mov r11d, DWORD PTR [r11]", "  mov r10, QWORD PTR [r10 + r11*8]",
             f"  mov r11d, OFFSET FLAT:{symbol}@TPOFF",
             "  add r10, r11"]
    if reg != "r10":
        lines.append(f"  mov {reg}, r10")
    if reg != "r10":
        lines.append(f"  mov r10, QWORD PTR [rsp + {first}]")
    if reg != "r11":
        lines.append(f"  mov r11, QWORD PTR [rsp + {second}]")
    return lines
