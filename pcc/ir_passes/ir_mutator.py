"""Legacy external-verifier adapter for the shared owned mutable IR layer."""
from __future__ import annotations
import llvmlite.binding as llvm
from pcc.native_ir.ir_mutator import MutableModule as _OwnedMutableModule
from pcc.native_ir.ir_mutator import (
    _DEFINE_HEADER_RE,
    _DECLARE_RE,
    _BLOCK_LABEL_RE,
    _ASSIGN_RE,
    _OPCODE_RE,
    _TERMINATORS,
    Argument,
    Instruction,
    BasicBlock,
    Function,
    _parse_function,
    _split_args,
)

class MutableModule(_OwnedMutableModule):
    @classmethod
    def parse(cls, ir_text: str):
        module = _OwnedMutableModule.parse(ir_text)
        return cls(header_lines=module.header_lines, functions=module.functions,
                   declarations=module.declarations, globals_=module.globals_,
                   tail_lines=module.tail_lines)

    def verify_roundtrip(self) -> None:
        llvm.parse_assembly(self.serialize()).verify()


def _parse_module(ir_text: str):
    return MutableModule.parse(ir_text)
