"""SimplifyCFG (subset) — IR-level control-flow cleanup.

Upstream reference:

- ``/private/tmp/llvm-src/llvm-project-20.1.8.src/llvm/lib/Transforms/Utils/SimplifyCFG.cpp``
- ``/private/tmp/llvm-src/llvm-project-20.1.8.src/llvm/lib/Transforms/Scalar/SimplifyCFGPass.cpp``

Upstream implements many transforms. The subset here now covers a
useful family of local, single-entry CFG cleanups that are tractable
with textual rewriting:

- fold ``br i1 true/false`` to the chosen arm,
- collapse conditional branches whose two arms both return,
- rewrite "branch to two returns" into ``select + ret``,
- thread through empty unconditional forwarders,
- collapse two-arm forwarders into a ``phi + ret`` merge block into
  ``select + ret``,
- remove dead control-flow arms by rebuilding the whole function body,
- run local DCE on the rebuilt function text to drop dead branch
  conditions that become unused.

This is still a subset. It does not attempt the wider upstream
surface such as switch lowering, speculative hoisting, sink/common-tail
synthesis across arbitrary blocks, or large PHI surgery.
"""

from __future__ import annotations

import re

from dataclasses import dataclass

import llvmlite.binding as llvm

from .dce import dce_module_text

from .manager import AnalysisManager, ModulePass, PreservedAnalyses

class SimplifyCFGPass(ModulePass):
    name = "pcc-simplifycfg"

    def __init__(self) -> None:
        self.rewritten_ir: str | None = None

    def run(
        self,
        module: llvm.ModuleRef,
        am: AnalysisManager,
    ) -> PreservedAnalyses:
        self.rewritten_ir = None
        ir_text = str(module)
        new_text, changed = simplify_cfg_text(ir_text)
        if not changed:
            return PreservedAnalyses.all()
        llvm.parse_assembly(new_text).verify()
        self.rewritten_ir = new_text
        return PreservedAnalyses.none()

# Shared owned kernels; LLVM remains only in the legacy verification adapter.
from pcc.native_ir.simplifycfg import (
    _DEFINE_HEADER_RE,
    _BLOCK_LABEL_RE,
    _COND_BR_RE,
    _BR_RE,
    _RET_RE,
    _PHI_HEAD_RE,
    _PHI_INCOMING_RE,
    _SSA_NAME_RE,
    _ASSIGN_RE,
    _PURE_OP_HEAD_RE,
    _Block,
    _split_functions,
    _module_context_without_functions,
    _function_chunk_module,
    _function_name_from_chunk,
    _function_declaration_from_chunk,
    _module_context_for_function,
    _parse_blocks,
    _join_function,
    _resolve_forwarder,
    _terminator_targets,
    _predecessor_counts,
    _canonicalize_hoisted_chain,
    _rewrite_label_refs,
    _drop_unreachable_blocks,
    _prune_invalid_phi_incomings,
    _merge_linear_successors,
    _drop_raw_terminator_line,
    _direct_ret,
    _simple_ret,
    _pure_chain_ret,
    _phi_ret,
    _entry_available_phi_value,
    _phi_single_use_chain_ret,
    _phi_chain_ret,
    _rewrite_phi_chain_operand,
    _ssa_use_count,
    _parse_phi,
    _unique_temp_name,
    _unique_block_label,
    _cleanup_function_locally,
    _is_trivial_unreachable,
    _prefix_inst_lines,
    _rewrite_simple_conditional_blocks,
    simplify_cfg_text,
)
