"""Integer and memory intrinsics shared by SysV and Win64 emission."""

from . import BackendUnavailable
from .self_backend_ir import TypeDesc


def emit(func, dest, ret_type, callee, args, windows=False):
    from .self_backend_x86_64_linux import (
        _materialize_value, _reg_alias, _store_reg_to_slot, _slot_addr,
        _value_label, _reg_name,
    )
    from .self_backend_x86_float_intrinsics import emit as emit_float
    result = emit_float(func, dest, ret_type, callee, args, windows=windows)
    if result is not None:
        return result
    from .self_backend_x86_integer_intrinsics import emit as emit_integer
    result = emit_integer(func, dest, ret_type, callee, args)
    if result is not None:
        return result
    if callee == "llvm.assume":
        return []
    if callee.startswith("llvm.frameaddress."):
        # Every prologue pushes the caller's rbp and points rbp at it.
        if len(args) != 1 or args[0][1] != "0":
            raise BackendUnavailable("llvm.frameaddress supports only depth 0")
        if dest is None or dest not in func.value_slots:
            return []
        return _store_reg_to_slot("rbp", func.value_slots[dest].offset, ret_type)
    if callee.startswith(("llvm.memcpy.", "llvm.memmove.", "llvm.memset.")):
        if len(args) < 4:
            raise BackendUnavailable("memory intrinsic requires four operands")
        tag = _value_label(func.name, args[0][1] + "_" + args[2][1], callee.replace(".", "_"))
        # One instruction may be repeated with identical operands; use the
        # per-function monotonic label counter, not operand identity.
        func.platform_intrinsic_labels += 1
        tag += "_" + str(func.platform_intrinsic_labels)
        lines = _materialize_value(func, args[0][1], args[0][0], "r11")
        lines.extend(_materialize_value(func, args[2][1], args[2][0], _reg_alias("rcx", min(8, args[2][0].slot_size))))
        lines.extend(["  test rcx, rcx", f"  je {tag}_end"])
        if callee.startswith("llvm.memset."):
            lines.extend(_materialize_value(func, args[1][1], args[1][0], "al"))
            lines.extend([f"{tag}_forward:", "  mov BYTE PTR [r11], al",
                          "  add r11, 1", "  sub rcx, 1", f"  jne {tag}_forward", f"{tag}_end:"])
            return lines
        lines.extend(_materialize_value(func, args[1][1], args[1][0], "r10"))
        if callee.startswith("llvm.memmove."):
            lines.extend(["  cmp r11, r10", f"  jbe {tag}_forward", "  add r10, rcx", "  add r11, rcx",
                          f"{tag}_backward:", "  sub r10, 1", "  sub r11, 1",
                          "  mov al, BYTE PTR [r10]", "  mov BYTE PTR [r11], al",
                          "  sub rcx, 1", f"  jne {tag}_backward", f"  jmp {tag}_end"])
        lines.extend([f"{tag}_forward:", "  mov al, BYTE PTR [r10]", "  mov BYTE PTR [r11], al",
                      "  add r10, 1", "  add r11, 1", "  sub rcx, 1", f"  jne {tag}_forward", f"{tag}_end:"])
        return lines
    if callee.startswith(("llvm.sadd.with.overflow.", "llvm.ssub.with.overflow.",
                          "llvm.uadd.with.overflow.", "llvm.usub.with.overflow.",
                          "llvm.umul.with.overflow.")):
        if len(args) != 2 or not ret_type.is_struct or len(ret_type.fields) != 2:
            raise BackendUnavailable("malformed overflow intrinsic")
        ty = args[0][0]
        if ty.width not in (8, 16, 32, 64):
            raise BackendUnavailable("unsupported overflow integer width")
        accumulator = _reg_name(ty, 0)
        rhs = _reg_name(ty, 10)
        lines = _materialize_value(func, args[0][1], ty, accumulator)
        lines.extend(_materialize_value(func, args[1][1], ty, rhs))
        multiply = callee.startswith("llvm.umul.")
        operation = "mul" if multiply else "sub" if ".ssub." in callee or ".usub." in callee else "add"
        lines.append(f"  mul {rhs}" if multiply else f"  {operation} {accumulator}, {rhs}")
        condition = "o" if callee.startswith(("llvm.sadd", "llvm.ssub")) else "b"
        lines.append(f"  set{condition} r11b")
        if dest is not None and dest in func.value_slots:
            slot = func.value_slots[dest]
            lines.extend(_store_reg_to_slot(accumulator, slot.offset, ty))
            lines.extend(_store_reg_to_slot("r11b", slot.offset - ret_type.field_offset(1), TypeDesc("int", 1)))
        return lines
    if callee.startswith(("llvm.ctlz.", "llvm.cttz.", "llvm.ctpop.")):
        ty, value = args[0]
        if ty.width not in (8, 16, 32, 64):
            raise BackendUnavailable("unsupported bit-count width")
        func.platform_intrinsic_labels += 1
        tag = _value_label(func.name, dest or "count", "bits_" + str(func.platform_intrinsic_labels))
        source = _reg_name(ty, 10)
        lines = _materialize_value(func, value, ty, source)
        lines.append("  xor eax, eax")
        if callee.startswith("llvm.ctpop."):
            lines.extend([f"  mov ecx, {ty.width}", f"{tag}_loop:", f"  mov {_reg_name(ty, 11)}, {source}",
                          "  and r11d, 1", "  add eax, r11d", f"  shr {source}, 1",
                          "  sub ecx, 1", f"  jne {tag}_loop"])
        else:
            lines.extend([f"  test {source}, {source}", f"  jne {tag}_loop",
                          f"  mov eax, {ty.width}", f"  jmp {tag}_end", f"{tag}_loop:"])
            if callee.startswith("llvm.ctlz."):
                lines.extend([f"  test {source}, {source}", f"  js {tag}_end", f"  shl {source}, 1"])
            else:
                lines.extend([f"  shr {source}, 1", f"  jb {tag}_end"])
            lines.extend(["  add eax, 1", f"  jmp {tag}_loop", f"{tag}_end:"])
        if dest is not None and dest in func.value_slots:
            lines.extend(_store_reg_to_slot(_reg_name(ret_type, 0), func.value_slots[dest].offset, ret_type))
        return lines
    return None
