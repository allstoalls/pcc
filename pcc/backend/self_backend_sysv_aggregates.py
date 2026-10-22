"""SysV AMD64 aggregate eightbyte classification for scalar IR members."""

from .self_backend_ir import _align_to


def _classify(ty, offset, classes):
    if offset % max(1, ty.align):
        return False
    if ty.is_struct:
        for index, member in enumerate(ty.fields):
            if not _classify(member, offset + ty.field_offset(index), classes):
                return False
        return True
    if ty.is_array:
        if ty.elem is None:
            return False
        stride = _align_to(ty.elem.slot_size, ty.elem.align)
        for index in range(ty.count):
            if not _classify(ty.elem, offset + index * stride, classes):
                return False
        return True
    if not (ty.is_int or ty.is_ptr or ty.is_fp) or ty.slot_size > 8:
        return False
    first = offset // 8
    last = (offset + ty.slot_size - 1) // 8
    if first < 0 or last >= len(classes):
        return False
    kind = "sse" if ty.is_fp else "integer"
    for index in range(first, last + 1):
        if classes[index] == "integer" or kind == "integer":
            classes[index] = "integer"
        else:
            classes[index] = "sse"
    return True


def classes(ty):
    if ty.slot_size <= 0 or ty.slot_size > 16:
        return ()
    result = ["none"] * ((ty.slot_size + 7) // 8)
    if not _classify(ty, 0, result):
        return ()
    return tuple("integer" if kind == "none" else kind for kind in result)


def assign(ty, gp_registers, fp_registers, gp, fp):
    layout = classes(ty)
    if not layout:
        return (), gp, fp
    need_gp = sum(1 for kind in layout if kind == "integer")
    need_fp = len(layout) - need_gp
    if gp + need_gp > len(gp_registers) or fp + need_fp > len(fp_registers):
        return (), gp, fp
    assignments = []
    for index, kind in enumerate(layout):
        if kind == "integer":
            register = gp_registers[gp]
            gp += 1
        else:
            register = fp_registers[fp]
            fp += 1
        assignments.append((register, index * 8, min(8, ty.slot_size - index * 8), kind))
    return tuple(assignments), gp, fp


def returned(ty):
    return assign(ty, ("rax", "rdx"), ("xmm0", "xmm1"), 0, 0)[0]


def store(slot, assignments):
    from .self_backend_x86_64_linux import _slot_addr, _store_gp_chunk_to_address
    lines = [f"  lea r10, {_slot_addr(slot)}"]
    for register, offset, size, kind in assignments:
        source = register
        if kind == "sse":
            lines.append(f"  movq r11, {register}")
            source = "r11"
        lines.extend(_store_gp_chunk_to_address(source, "r10", offset, size))
    return lines


def load(func, value, ty, assignments):
    from .self_backend_x86_64_linux import (
        _materialize_aggregate_value_address, _load_address_chunk_to_gp_reg,
    )
    from .self_backend_parse import is_aggregate_literal_value, aggregate_literal_to_bytes
    literal = None
    if value in ("zeroinitializer", "undef", "poison"):
        literal = b"\0" * ty.slot_size
    elif is_aggregate_literal_value(value):
        literal = aggregate_literal_to_bytes(ty, value, type_context=func.type_context)
    lines = [] if literal is not None else _materialize_aggregate_value_address(func, value, ty, "r10")
    ordered = sorted(assignments, key=lambda row: row[0] == "rax")
    for register, offset, size, kind in ordered:
        temporary = "rax" if kind == "sse" else register
        if literal is not None:
            bits = int.from_bytes(literal[offset:offset + size], "little")
            lines.append(f"  mov {temporary}, {bits}")
        else:
            lines.extend(_load_address_chunk_to_gp_reg("r10", offset, size, temporary))
        if kind == "sse":
            lines.append(f"  movq {register}, rax")
    return lines
