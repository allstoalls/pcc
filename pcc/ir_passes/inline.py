"""Function Inlining — IR-level (subset).

Upstream reference:

- ``/tmp/llvm-src/llvm-20.1.8.src/lib/Transforms/IPO/Inliner.cpp``
  implements :cpp:class:`llvm::InlinerPass`. It works on the CGSCC
  pass manager, uses a cost model
  (``/tmp/llvm-src/llvm-20.1.8.src/lib/Analysis/InlineCost.cpp``),
  and clones callee bodies into callers with SSA remapping.

Subset implemented here (labelled ``subset``):

- Inline only callees that are (a) internal linkage, (b) single
  basic block, (c) end in a ``ret`` of a single-value expression,
  or ``ret void``.
- Also inline one small multi-block shape: a tiny single-exit callee
  into a caller block that is exactly ``call`` followed by ``ret``.
  This covers the common "diamond + phi + ret" case without needing a
  full general-purpose CFG splicer.
- Replace the call site with the cloned body, rewriting operands so
  ``%x`` → the actual argument value, and the final ``ret %val``
  becomes an assignment of ``%val`` to the call's result name.

Full inlining (arbitrary call-site splitting, general multi-block CFG
splicing, nested calls) is deferred to the full implementation.
"""

from __future__ import annotations

import re

import llvmlite.binding as llvm

from .ir_mutator import MutableModule

from .instsimplify import simplify_module_text

from .manager import AnalysisManager, ModulePass, PreservedAnalyses

class InlinePass(ModulePass):
    name = "pcc-inline"

    def __init__(self) -> None:
        self.rewritten_ir: str | None = None

    def run(
        self,
        module: llvm.ModuleRef,
        am: AnalysisManager,
    ) -> PreservedAnalyses:
        self.rewritten_ir = None
        ir_text = str(module)
        new_text, changed = inline_module(ir_text)
        if not changed:
            return PreservedAnalyses.all()
        try:
            llvm.parse_assembly(new_text).verify()
        except RuntimeError:
            return PreservedAnalyses.all()
        self.rewritten_ir = new_text
        return PreservedAnalyses.none()

class AlwaysInlinePass(InlinePass):
    """Always-inline variant — same subset, same entry point."""

    name = "pcc-always-inline"

    def run(
        self,
        module: llvm.ModuleRef,
        am: AnalysisManager,
    ) -> PreservedAnalyses:
        self.rewritten_ir = None
        ir_text = str(module)
        new_text, changed = inline_module(ir_text, require_alwaysinline=True)
        if not changed:
            return PreservedAnalyses.all()
        try:
            llvm.parse_assembly(new_text).verify()
        except RuntimeError:
            return PreservedAnalyses.all()
        self.rewritten_ir = new_text
        return PreservedAnalyses.none()

from pcc.native_ir.inline import (
    inline_module,
    _first_define_line,
    _parse_attr_groups,
    _header_has_alwaysinline,
    _PASSTHROUGH_RET_TYPES,
    _CALL_RE_TEMPLATE,
    _VOID_CALL_RE_TEMPLATE,
    _NOOP_GEP_RE,
    _BARE_CALL_RE_TEMPLATE,
    _inline_calls,
    _inline_calls_in_function,
    _inline_multiblock_return_callers,
    _rename_cloned_blocks_for_inline,
    _rewrite_two_exit_inline_shape,
    _match_call_instruction,
    _apply_remap,
    llvm_line,
    _apply_remap_token,
    _apply_value_replacements,
    _replace_percent_names,
    _split_functions,
    _drop_dead_internal_callees,
)
