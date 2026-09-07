"""InstructionSimplify (subset) — IR-level pass.

Upstream reference:

- ``/tmp/llvm-src/llvm-20.1.8.src/lib/Analysis/InstructionSimplify.cpp``
  implements the full simplifier. The entry point
  :cpp:func:`llvm::simplifyInstruction` dispatches on opcode and
  calls per-op helpers (``SimplifyAddInst``, ``SimplifyAndInst``, ...),
  each returning a replacement ``Value *`` or nullptr. The pass
  wrapper :cpp:class:`llvm::InstSimplifyPass`
  (``.../Transforms/Scalar/InstSimplifyPass.cpp``) walks each block
  in postorder and replaces every instruction that simplifies.

The subset implemented here mirrors the pure-arithmetic identity
short-circuits upstream returns *early*, without requiring the
full recursive simplifier:

    Arithmetic: x+0, x-0, x-x, x*0, x*1, x*-1 (→ neg)
                constant/constant integer folding for the covered binops
    Bitwise:    x&x, x|x, x^x, x&-1, x&0, x|0, x|-1, x^-1 (→ ~x placeholder)
    Shifts:     x<<0, x>>0, 0<<x, 0>>x, and constant/constant shifts
                with W ≥ bit-width → poison
    Compares:   eq/ne on equal constants, eq on same SSA value, etc.
    Selects:    select true,x,y → x; select false,x,y → y;
                select c,x,x → x.

Passes that need the full simplifier (especially recursive,
canonicalization-aware rewrites) should continue to fall through to
upstream ``opt -passes=instsimplify``; this module is labelled
``subset`` in the registry.
"""

from __future__ import annotations

import re

import llvmlite.binding as llvm

from .manager import AnalysisManager, ModulePass, PreservedAnalyses

from .integer_fold_contract import (
    FOLD_CONSTANT,
    FOLD_POISON,
    fold_llvm_integer_binary,
    fold_llvm_integer_compare,
    signed_value,
)

class InstSimplifyPass(ModulePass):
    """Apply the instsimplify subset across the whole module."""

    name = "pcc-instsimplify"

    def __init__(self) -> None:
        self.rewritten_ir: str | None = None

    def run(
        self,
        module: llvm.ModuleRef,
        am: AnalysisManager,
    ) -> PreservedAnalyses:
        self.rewritten_ir = None
        ir_text = str(module)
        new_text, changed = simplify_module_text(ir_text)
        if not changed:
            return PreservedAnalyses.all()
        llvm.parse_assembly(new_text).verify()
        self.rewritten_ir = new_text
        return PreservedAnalyses.none()

# Shared owned kernels; LLVM remains only in the legacy verification adapter.
from pcc.native_ir.instsimplify import (
    _BINOP_RE,
    _ICMP_RE,
    _SELECT_RE,
    _bit_width,
    _is,
    _try_int,
    _normalize_unsigned,
    _normalize_signed,
    _is_neg_one,
    _fold_constant_binop,
    _simplify_binop,
    _simplify_icmp,
    _simplify_select,
    simplify_module_text,
    _rewrite_function_text,
    _one_pass,
)
