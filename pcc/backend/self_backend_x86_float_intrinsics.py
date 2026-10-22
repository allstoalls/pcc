"""SSE2/bitwise floating intrinsics shared by both owned x86-64 targets."""

from . import BackendUnavailable
from .self_backend_ir import TypeDesc
from .self_backend_parse import const_int_from_value


def emit(func, dest, ret_type, callee, args, windows=False):
    prefixes = ("llvm.fabs.", "llvm.copysign.", "llvm.ceil.", "llvm.floor.",
                "llvm.trunc.", "llvm.rint.", "llvm.nearbyint.", "llvm.is.fpclass.")
    if not callee.startswith(prefixes):
        return None
    expected = 2 if callee.startswith(("llvm.copysign.", "llvm.is.fpclass.")) else 1
    if len(args) != expected:
        raise BackendUnavailable("floating intrinsic operand count mismatch: " + callee)
    if dest is None or dest not in func.value_slots:
        return []
    from .self_backend_x86_64_linux import _materialize_value, _store_reg_to_slot, _value_label
    ty, value = args[0]
    if not ty.is_fp or ty.width not in (32, 64):
        raise BackendUnavailable("x86 floating intrinsic requires f32/f64")
    single = ty.width == 32
    move = "movd" if single else "movq"
    accumulator = "eax" if single else "rax"
    scratch = "r10d" if single else "r10"
    mask = (1 << (ty.width - 1)) - 1
    lines = _materialize_value(func, value, ty, "xmm0")
    if callee.startswith("llvm.is.fpclass."):
        return lines + classify(func, dest, ty, args)
    if callee.startswith("llvm.copysign."):
        if len(args) != 2 or args[1][0].describe() != ty.describe():
            raise BackendUnavailable("copysign operand types differ")
        lines.extend(_materialize_value(func, args[1][1], ty, "xmm1"))
    if callee.startswith(("llvm.fabs.", "llvm.copysign.")):
        lines.extend([f"  {move} {accumulator}, xmm0", f"  mov {scratch}, {mask}",
                      f"  and {accumulator}, {scratch}"])
        if callee.startswith("llvm.copysign."):
            bits = "r11d" if single else "r11"
            lines.extend([f"  {move} {bits}, xmm1", f"  shr {bits}, {ty.width - 1}",
                          f"  shl {bits}, {ty.width - 1}", f"  or {accumulator}, {bits}"])
        lines.append(f"  {move} xmm0, {accumulator}")
    elif callee.startswith("llvm.trunc."):
        fraction = 23 if single else 52
        bias = 127 if single else 1023
        exponent = 255 if single else 2047
        func.platform_intrinsic_labels += 1
        label = _value_label(func.name, dest, "trunc_" + str(func.platform_intrinsic_labels))
        bits = "r11d" if single else "r11"
        lines.extend([f"  {move} {accumulator}, xmm0", f"  mov {scratch}, {accumulator}",
                      f"  shr {scratch}, {fraction}", f"  and r10d, {exponent}",
                      f"  sub r10d, {bias}", f"  cmp r10d, {fraction}", f"  jge {label}_end",
                      "  cmp r10d, 0", f"  jl {label}_zero", f"  mov ecx, {fraction}",
                      "  sub ecx, r10d", f"  mov {bits}, 1", f"  shl {bits}, cl",
                      f"  sub {bits}, 1", f"  xor {bits}, -1", f"  and {accumulator}, {bits}",
                      f"  jmp {label}_bits", f"{label}_zero:",
                      f"  shr {accumulator}, {ty.width - 1}", f"  shl {accumulator}, {ty.width - 1}",
                      f"{label}_bits:", f"  {move} xmm0, {accumulator}", f"{label}_end:"])
    else:
        if single:
            lines.append("  cvtss2sd xmm0, xmm0")
        ceil = callee.startswith("llvm.ceil.")
        nearby = callee.startswith("llvm.nearbyint.")
        if ceil:
            lines.extend(["  movq rax, xmm0", "  mov r10, 9223372036854775808", "  xor rax, r10", "  movq xmm0, rax"])
        if nearby:
            if windows:
                from .self_backend_win64_abi import MXCSR_OFFSET, outgoing_size
                save = outgoing_size(func) + MXCSR_OFFSET
            else:
                save = 184 if func.is_vararg else 8
            lines.append(f"  stmxcsr DWORD PTR [rsp + {save}]")
        # These functions are authored in freestanding_libc_numeric.py; no
        # external libm is introduced. Win64 home space is part of the frame.
        lines.append("  call rint" if callee.startswith(("llvm.rint.", "llvm.nearbyint.")) else "  call floor")
        if nearby:
            lines.extend([f"  stmxcsr DWORD PTR [rsp + {save + 4}]",
                          f"  mov eax, DWORD PTR [rsp + {save}]", "  and eax, 32",
                          f"  mov r10d, DWORD PTR [rsp + {save + 4}]", "  and r10d, -33",
                          "  or r10d, eax", f"  mov DWORD PTR [rsp + {save + 4}], r10d",
                          f"  ldmxcsr DWORD PTR [rsp + {save + 4}]"])
        if ceil:
            lines.extend(["  movq rax, xmm0", "  mov r10, 9223372036854775808", "  xor rax, r10", "  movq xmm0, rax"])
        if single:
            lines.append("  cvtsd2ss xmm0, xmm0")
    lines.extend(_store_reg_to_slot("xmm0", func.value_slots[dest].offset, ret_type))
    return lines


def classify(func, dest, ty, args):
    from .self_backend_x86_64_linux import _store_reg_to_slot, _value_label
    if len(args) != 2:
        raise BackendUnavailable("is.fpclass requires value and constant mask")
    mask = const_int_from_value(args[1][1])
    if mask is None or mask < 0 or mask > 1023:
        raise BackendUnavailable("is.fpclass mask must be a ten-bit constant")
    width = ty.width
    fraction = 23 if width == 32 else 52
    exp_mask = 255 if width == 32 else 2047
    func.platform_intrinsic_labels += 1
    label = _value_label(func.name, dest, "fpclass_" + str(func.platform_intrinsic_labels))
    lines = ["  movd eax, xmm0" if width == 32 else "  movq rax, xmm0",
             "  mov rdx, rax", f"  shr rdx, {width - 1}",
             "  mov r10, rax", f"  shr r10, {fraction}", f"  and r10d, {exp_mask}",
             f"  mov r11, {(1 << fraction) - 1}", "  and r11, rax",
             f"  cmp r10d, {exp_mask}", f"  je {label}_special",
             "  test r10d, r10d", f"  jne {label}_normal", "  test r11, r11",
             f"  jne {label}_subnormal", "  mov eax, 64", "  mov ecx, 32", f"  jmp {label}_sign",
             f"{label}_normal:", "  mov eax, 256", "  mov ecx, 8", f"  jmp {label}_sign",
             f"{label}_subnormal:", "  mov eax, 128", "  mov ecx, 16", f"  jmp {label}_sign",
             f"{label}_special:", "  test r11, r11", f"  jne {label}_nan",
             "  mov eax, 512", "  mov ecx, 4", f"  jmp {label}_sign",
             f"{label}_nan:", f"  shr r11, {fraction - 1}", "  mov eax, 1",
             "  add eax, r11d", f"  jmp {label}_mask",
             f"{label}_sign:", "  test edx, edx", "  cmovne eax, ecx",
             f"{label}_mask:", f"  and eax, {mask}", "  setne al"]
    lines.extend(_store_reg_to_slot("al", func.value_slots[dest].offset, TypeDesc("int", 1)))
    return lines
