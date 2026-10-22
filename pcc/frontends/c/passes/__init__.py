"""PCC Pass Framework — Nanopass-inspired, Graal-tiered optimization pipeline.

Four tiers of optimization plumbing, unified by PassContext:

  HighTier  (AST-level analysis)   — read-only, collect info into PassContext
  MidTier   (Codegen enhancement)  — codegen reads PassContext to emit better IR
  LowTier   (IR post-processing)   — mutate LLVM IR text to add metadata
  BackendTier (LLVM module pipeline) — run LLVM O1/O2/O3 or a custom bundle

Usage:
    from pcc.frontends.c.passes import PassPipeline, PassContext

    ctx = PassContext()
    pipeline = PassPipeline.default()
    pipeline.run(ast, ctx)           # HighTier: analyze AST
    codegen.generate_code(ast, ctx)  # MidTier: codegen uses ctx
    ir_text = pipeline.run_low_tier(ir_text, ctx)  # LowTier: post-process IR
    pipeline.run_backend_tier(...)           # BackendTier: LLVM pipeline
"""

from pcc.frontends.c.passes.context import PassContext, VarInfo, FuncInfo
from pcc.frontends.c.passes.base import Pass, ASTPass, IRPass, PassPipeline
from pcc.frontends.c.passes.groups import default_pass_groups, pass_group_names, passes_for_group, disable_pass_group, llvm_default_pass_names, registered_llvm_alias_names, unique_default_pass_names, unique_managed_pass_names, validate_default_pass_groups
from pcc.frontends.c.passes.registry import LLVMPythonTranslation, expand_registered_pass_name, expand_registered_pass_names, llvm_python_translation, llvm_python_translations

__all__ = [
    "PassContext",
    "VarInfo",
    "FuncInfo",
    "Pass",
    "ASTPass",
    "IRPass",
    "PassPipeline",
    "default_pass_groups",
    "pass_group_names",
    "passes_for_group",
    "disable_pass_group",
    "llvm_default_pass_names",
    "registered_llvm_alias_names",
    "unique_default_pass_names",
    "unique_managed_pass_names",
    "validate_default_pass_groups",
    "LLVMPythonTranslation",
    "expand_registered_pass_name",
    "expand_registered_pass_names",
    "llvm_python_translation",
    "llvm_python_translations",
]
