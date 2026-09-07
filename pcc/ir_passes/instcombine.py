"""InstructionCombining (subset) — local peephole rewrites.

Upstream reference:

- ``/private/tmp/llvm-src/llvm-project-20.1.8.src/llvm/lib/Transforms/InstCombine/InstructionCombining.cpp``
- ``/private/tmp/llvm-src/llvm-project-20.1.8.src/llvm/lib/Transforms/InstCombine/InstCombineAddSub.cpp``
- ``/private/tmp/llvm-src/llvm-project-20.1.8.src/llvm/lib/Transforms/InstCombine/InstCombineMulDivRem.cpp``

The subset here focuses on purely local arithmetic/extension
canonicalizations that can be expressed textually:

- ``add x, x`` → ``shl x, 1``
- ``add x, (sub 0, x)`` / ``add (sub 0, x), x`` → ``0``
- ``add x, (sub C, x)`` / ``add (sub C, x), x`` → ``C``
- ``sub x, -c`` → ``add x, c``
- ``sub 0, (sub 0, x)`` → ``x``
- ``sub (shl x, 1), x`` → ``x``
- ``mul x, 2^k`` / ``mul 2^k, x`` → ``shl x, k``
- ``mul x, -1`` / ``mul -1, x`` → ``sub 0, x``
- ``add (shl x, 1), x`` → ``mul x, 3``
- ``add (shl x, N), 1`` / ``add 1, (shl x, N)`` → ``or disjoint (shl x, N), 1`` for ``N > 0``
- ``add (sub x, C1), C2`` / ``add C2, (sub x, C1)`` → ``add x, (C2-C1)``
- ``add (sub C1, x), C2`` / ``add C2, (sub C1, x)`` → ``sub (C1+C2), x``
- ``sub (add x, C1), C2`` → ``add x, (C1-C2)``
- ``zext i1 true/false`` → ``1/0``
- ``sext i1 true/false`` → ``-1/0``

As in upstream, this pass runs after a simplifier phase and then
cleans up dead now-unused local instructions. It is still a subset;
the real InstCombine is far larger and fixed-point driven.
"""

from __future__ import annotations

import re

import llvmlite.binding as llvm

from .dce import dce_module_text

from .instsimplify import simplify_module_text

from .manager import AnalysisManager, ModulePass, PreservedAnalyses

from .integer_fold_contract import (
    FOLD_CONSTANT,
    FOLD_POISON,
    fold_llvm_integer_binary,
    signed_value,
)

from .simplifycfg import _function_chunk_module, _module_context_for_function

class InstCombinePass(ModulePass):
    name = "pcc-instcombine"

    def __init__(self) -> None:
        self.rewritten_ir: str | None = None

    def run(
        self,
        module: llvm.ModuleRef,
        am: AnalysisManager,
    ) -> PreservedAnalyses:
        self.rewritten_ir = None
        ir_text = str(module)
        after_simplify, simplify_changed = simplify_module_text(ir_text)
        combined_text, combine_changed = instcombine_text(after_simplify)
        changed = simplify_changed or combine_changed
        if not changed:
            return PreservedAnalyses.all()
        llvm.parse_assembly(combined_text).verify()
        self.rewritten_ir = combined_text
        return PreservedAnalyses.none()

# Shared owned kernels; LLVM remains only in the legacy verification adapter.
from pcc.native_ir.instcombine import (
    _BINOP_RE,
    _ZEXT_CONST_RE,
    _SEXT_CONST_RE,
    _SSA_NAME_RE,
    _split_functions,
    _try_int,
    _int_width,
    _fold_const_binop,
    _is_pow2,
    _is_neg_one,
    _format_line,
    _format_scaled_value,
    _scaled_operand,
    _base_plus_const,
    _bitnot_base,
    _unique_ssa_name,
    _rewrite_function,
    instcombine_text,
)
