"""C aggregate-layout records and target scalar layout helpers."""

from __future__ import annotations

from dataclasses import dataclass

from pcc.frontends.c.c_abi_layout import floating_scalar_layout, integer_scalar_layout, pointer_scalar_layout
from pcc.ir.compat import ir_c as ir


@dataclass
class StructFieldLayout:
    name: object
    byte_offset: int
    semantic_ir_type: object
    decl_type: object
    is_bitfield: bool = False
    storage_byte_offset: int = 0
    storage_ir_type: object = None
    bit_offset: int = 0
    bit_width: int = 0
    is_unsigned: bool = False


@dataclass
class StructStorageSegment:
    kind: str
    byte_offset: int
    ir_type: object
    field_index: object = None
    bitfield_indices: tuple = ()


@dataclass
class BitFieldRef:
    container_ptr: object
    storage_ir_type: object
    bit_offset: int
    bit_width: int
    semantic_ir_type: object
    is_unsigned: bool


def is_floating_ir_type(ir_type) -> bool:
    return isinstance(ir_type, (ir.HalfType, ir.FloatType, ir.DoubleType)) or str(ir_type) in ("x86_fp80", "fp128")


def floating_ir_width(ir_type):
    """Semantic format width; x87's 80 value bits occupy 16 ABI bytes."""
    spelling = str(ir_type)
    if spelling == "half": return 16
    if spelling == "float": return 32
    if spelling == "double": return 64
    if spelling == "x86_fp80": return 80
    if spelling == "fp128": return 128
    raise ValueError("not a floating type: " + spelling)


def is_struct_ir_type(ir_type) -> bool:
    return isinstance(ir_type, (ir.LiteralStructType, ir.IdentifiedStructType))


def ir_type_align(ir_type):
    custom_align = getattr(ir_type, "custom_align", None)
    if custom_align is not None:
        return custom_align
    if isinstance(ir_type, ir.VoidType):
        return 1
    if isinstance(ir_type, ir.IntType):
        return integer_scalar_layout(ir_type.width).alignment
    if isinstance(ir_type, ir.HalfType):
        return floating_scalar_layout(16).alignment
    if isinstance(ir_type, ir.FloatType):
        return floating_scalar_layout(32).alignment
    if isinstance(ir_type, ir.DoubleType):
        return floating_scalar_layout(64).alignment
    if str(ir_type) in ("x86_fp80", "fp128"):
        return floating_scalar_layout(floating_ir_width(ir_type)).alignment
    if isinstance(ir_type, ir.PointerType):
        return pointer_scalar_layout().alignment
    if isinstance(ir_type, ir.ArrayType):
        return ir_type_align(ir_type.element)
    if is_struct_ir_type(ir_type):
        if not ir_type.elements:
            return 1
        return max(ir_type_align(element) for element in ir_type.elements)
    return 8


def ir_type_size(ir_type):
    custom_size = getattr(ir_type, "custom_size", None)
    if custom_size is not None:
        return custom_size
    if isinstance(ir_type, ir.IntType):
        return integer_scalar_layout(ir_type.width).size
    if isinstance(ir_type, ir.HalfType):
        return floating_scalar_layout(16).size
    if isinstance(ir_type, ir.FloatType):
        return floating_scalar_layout(32).size
    if isinstance(ir_type, ir.DoubleType):
        return floating_scalar_layout(64).size
    if str(ir_type) in ("x86_fp80", "fp128"):
        return floating_scalar_layout(floating_ir_width(ir_type)).size
    if isinstance(ir_type, ir.PointerType):
        return pointer_scalar_layout().size
    if isinstance(ir_type, ir.ArrayType):
        return int(ir_type.count) * ir_type_size(ir_type.element)
    if is_struct_ir_type(ir_type):
        offset = 0
        for element in ir_type.elements:
            alignment = ir_type_align(element)
            offset = (offset + alignment - 1) & ~(alignment - 1)
            offset += ir_type_size(element)
        struct_alignment = ir_type_align(ir_type)
        return (offset + struct_alignment - 1) & ~(struct_alignment - 1)
    return 8
