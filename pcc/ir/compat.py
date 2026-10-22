"""Stable frontend imports for pcc's owned IR builder.

All frontend aliases refer to the same owned builder implementation.
"""
from __future__ import annotations

from . import ir

ir_py = ir
ir_c = ir
ir_passes = ir


def set_struct_body(struct_ty, body, packed: bool = False) -> None:
    struct_ty.set_body(body, packed=packed)


def add_raw_function_attribute(function, attribute: str) -> None:
    function.attributes.add(attribute)


__all__ = ["ir", "ir_py", "ir_c", "ir_passes", "set_struct_body", "add_raw_function_attribute"]
