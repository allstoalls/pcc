"""File-scope declaration state records for the C frontend."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FileScopeObjectState:
    type_key: str
    linkage: str
    definition_kind: str
    symbol_name: str
    ir_type: object


@dataclass
class FileScopeFunctionState:
    type_key: str
    function_type: object
    linkage: str
    defined: bool
    symbol_name: str
    # True when the record came from a call with no visible prototype rather
    # than from a declaration in the source. Such a record carries no linkage
    # intent, so a later definition settles the linkage instead of conflicting
    # with the ``extern`` an implicit declaration has to assume.
    implicit: bool = False


class CodegenError(Exception):
    """Raised for a C construct that cannot be lowered faithfully."""


class ExternGlobalRef:
    def __init__(self, symbol_name, ir_type) -> None:
        self.symbol_name = symbol_name
        self.ir_type = ir_type
