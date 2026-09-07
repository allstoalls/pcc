"""Dead code elimination (DCE).

Upstream reference:

- ``/tmp/llvm-src/llvm-20.1.8.src/lib/Transforms/Scalar/DCE.cpp``
  implements :cpp:class:`llvm::DCEPass`. The algorithm is straight:
  iterate over every instruction, and remove any that has no uses
  and has no side effects. Iterate to a fixed point.

This pass mirrors that directly. An instruction is "safe to remove"
when:

- it defines a value (i.e. has a ``%name = ...`` form),
- it is not side-effecting (stores, terminators, most calls),
- or it is a direct call whose callee is explicitly marked
  ``readnone``/``memory(none)`` plus ``willreturn`` and ``nounwind``,
- it has zero uses (no other instruction names it as an operand).

We reuse the def-use index from :mod:`ssa_utils` rather than
re-scanning the IR. Labelled ``equivalent`` for this narrow subset.
"""

from __future__ import annotations

import re

import llvmlite.binding as llvm

from .manager import AnalysisManager, ModulePass, PreservedAnalyses

from .ssa_utils import build_def_use_index

class DCEPass(ModulePass):
    name = "pcc-dce"

    def __init__(self) -> None:
        self.rewritten_ir: str | None = None

    def run(
        self,
        module: llvm.ModuleRef,
        am: AnalysisManager,
    ) -> PreservedAnalyses:
        self.rewritten_ir = None
        ir_text = str(module)
        new_text, changed = dce_module_text(ir_text)
        if not changed:
            return PreservedAnalyses.all()
        llvm.parse_assembly(new_text).verify()
        self.rewritten_ir = new_text
        return PreservedAnalyses.none()

# Shared owned kernels; LLVM remains only in the legacy verification adapter.
from pcc.native_ir.dce import (
    _SIDE_EFFECTING_OPCODES,
    _ATTR_GROUP_RE,
    _FUNC_ATTR_RE,
    _ASSIGN_RE,
    _CALL_ASSIGN_RE,
    dce_module_text,
    _collect_function_attrs,
    _line_by_result,
    _is_trivially_dead_call,
    _one_dce_pass,
)
