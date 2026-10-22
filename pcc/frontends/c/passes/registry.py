"""Source-backed registry for LLVM pass names with Python-side equivalents.

This module is the first step toward "all Python" LLVM-pass management:

- concrete LLVM pass names stay visible in the repository,
- known Python-side equivalents are registered explicitly,
- each mapping carries an upstream LLVM source path, and
- callers can expand one visible pass name into the actual pcc / LLVM pass
  names it should control.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from pcc.frontends.c.passes.llvm_builtin_registry import LLVM_DEFAULT_PROFILE_PASSES


#: Pass status taxonomy per docs/plans/all-pass-llvm-ir-1to1-master-plan.md.
#:
#: - ``equivalent``: LLVM-IR-level implementation whose behavior is intentionally
#:   aligned with the upstream pass and verified by the IR parity harness.
#: - ``subset``: LLVM-IR-level implementation of a documented, narrower subset
#:   of the upstream pass; the remaining gap is tracked as open work.
#: - ``migration-scaffold``: in-flight migration; temporary.
#: - ``fallback-only``: source-level pass kept only as a compatibility fallback
#:   when the IR version does not yet handle a shape.
#: - ``deprecated-source-approximation``: current source-level pass slated for
#:   removal once the IR-level implementation lands.
#: - ``missing``: no pcc-side implementation at all.
#:
#: Old ``full`` / ``partial`` labels are deprecated — they were used before the
#: move from source-level approximation to LLVM-IR-level 1:1 implementation,
#: and they conflated "has a story" with "matches upstream LLVM IR behavior".
STATUS_EQUIVALENT = "equivalent"
STATUS_SUBSET = "subset"
STATUS_MIGRATION_SCAFFOLD = "migration-scaffold"
STATUS_FALLBACK_ONLY = "fallback-only"
STATUS_DEPRECATED_SOURCE = "deprecated-source-approximation"
STATUS_MISSING = "missing"

#: Implementation tier taxonomy. Pass `implementation_tier` answers "what IR
#: does this pass operate on?" and is independent of `status` (which tracks
#: LLVM parity). A `deprecated-source-approximation` runs at `ast`; an
#: `equivalent` must run at `ir`.
TIER_IR = "ir"      # operates on LLVM IR via llvmlite (target for migration)
TIER_AST = "ast"    # operates on pcc C AST (source-level approximation)
TIER_MIXED = "mixed"  # operates on both, e.g. during migration


@dataclass(frozen=True)
class LLVMPythonTranslation:
    """One explicit LLVM-pass registration."""

    llvm_name: str
    python_passes: tuple[str, ...] = ()
    status: str = STATUS_MISSING
    upstream_sources: tuple[str, ...] = ()
    notes: str = ""
    implementation_tier: str = TIER_AST
    # Optional: import path of the IR-level pass class in
    # :mod:`pcc.ir_passes`. Populated for passes that now have an
    # IR-level implementation (``status`` ∈ {``equivalent``, ``subset``,
    # ``migration-scaffold``}).
    ir_pass_class: str | None = None


_TRANSLATION_OVERRIDES: dict[str, LLVMPythonTranslation] = {
    "aggressive-instcombine": LLVMPythonTranslation(
        llvm_name="aggressive-instcombine",
        python_passes=("canonicalize", "expr-reassociation", "copy-propagation"),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/AggressiveInstCombine/AggressiveInstCombine.cpp",),
        notes="pcc gives aggressive-instcombine the same explicit source-level combine boundary as instcombine by composing canonicalize, expr-reassociation, and copy-propagation for local algebraic cleanup, while LLVM retains the truly aggressive SSA- and poison-aware rewrites.",
    ),
    "adce": LLVMPythonTranslation(
        llvm_name="adce",
        python_passes=("ssa-adce", "dce"),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/Scalar/ADCE.cpp",),
        notes="pcc now combines a first SSA-backed local scalar ADCE pass with explicit source-level dead-code elimination, covering dead pure local bindings, unreachable tails, dead branches, dead locals, and side-effect-safe dead assignments, while leaving global SSA liveness reasoning to LLVM.",
    ),
    "bdce": LLVMPythonTranslation(
        llvm_name="bdce",
        python_passes=("ssa-adce", "dce"),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/Scalar/BDCE.cpp",),
        notes="pcc routes bdce through the same staged boundary as adce: bounded SSA-backed local scalar dead-binding elimination plus explicit source-level dce, without LLVM's demanded-bits reasoning.",
    ),
    "correlated-propagation": LLVMPythonTranslation(
        llvm_name="correlated-propagation",
        python_passes=("control-flow", "canonicalize", "dce"),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/Scalar/CorrelatedValuePropagation.cpp",),
        notes="pcc gives correlated-propagation an explicit source-level boundary by composing control-flow, canonicalize, and dce for local correlated branch cleanup such as deduping repeated guards and collapsing fallthrough returns, while full predicate reasoning remains LLVM territory.",
    ),
    "dse": LLVMPythonTranslation(
        llvm_name="dse",
        python_passes=("ssa-dse", "ssa-adce", "dce", "memory-opt-ir"),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/Scalar/DeadStoreElimination.cpp",),
        notes="pcc gives dse an explicit staged boundary by combining an SSA-backed dead-store consumer that preserves side-effecting RHS calls, a follow-on SSA local dead-binding cleanup, source-level dce, and bounded within-block IR memory rewrites for redundant overwritten stores plus store-load forwarding; the matching LLVM reference path still needs mem2reg before dse to expose wider stack-store cleanup, while MemorySSA-based cross-block reasoning remains LLVM territory.",
    ),
    "early-cse": LLVMPythonTranslation(
        llvm_name="early-cse",
        python_passes=("local-value-numbering", "copy-propagation"),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/Scalar/EarlyCSE.cpp",),
        notes="pcc performs a bounded source-level early CSE via local value numbering and copy propagation within straight-line block scope.",
    ),
    "function-attrs": LLVMPythonTranslation(
        llvm_name="function-attrs",
        python_passes=("func-attr",),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/IPO/FunctionAttrs.cpp",),
        notes="pcc infers a conservative source-level subset of function attributes and emits nounwind, nofree, and willreturn when proven.",
    ),
    "gvn": LLVMPythonTranslation(
        llvm_name="gvn",
        python_passes=(
            "ssa-gvn",
            "ssa-gvn-rewrite",
            "gvn",
            "local-value-numbering",
            "copy-propagation",
        ),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/Scalar/GVN.cpp",),
        notes="pcc now combines a first dominator-aware SSA GVN analysis, a bounded SSA-backed source rewrite for whole-expression reuse, the existing delegation marker, local value numbering, and copy propagation; full MemorySSA-aware cross-block elimination remains LLVM territory.",
    ),
    "indvars": LLVMPythonTranslation(
        llvm_name="indvars",
        python_passes=("indvars",),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/Scalar/IndVarSimplify.cpp",),
        notes="pcc normalizes a conservative subset of induction-variable step expressions in for-loops.",
    ),
    "infer-alignment": LLVMPythonTranslation(
        llvm_name="infer-alignment",
        python_passes=("align",),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/Scalar/InferAlignment.cpp",),
        notes="pcc lowers this to explicit IR-text alignment annotation on plain scalar loads and stores.",
    ),
    "instcombine": LLVMPythonTranslation(
        llvm_name="instcombine",
        python_passes=("canonicalize", "expr-reassociation", "copy-propagation"),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/InstCombine/InstructionCombining.cpp",),
        notes="pcc performs a conservative source-level instcombine subset by composing explicit canonicalization, reassociation, and copy-propagation rewrites.",
    ),
    "instsimplify": LLVMPythonTranslation(
        llvm_name="instsimplify",
        python_passes=("canonicalize",),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Analysis/InstructionSimplify.cpp",),
        notes="pcc routes instsimplify through explicit source-level canonicalization patterns covering constant folding, algebraic cleanup, identity elimination, and constant ternary pruning.",
    ),
    "jump-threading": LLVMPythonTranslation(
        llvm_name="jump-threading",
        python_passes=("control-flow",),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/Scalar/JumpThreading.cpp",),
        notes="pcc gives jump-threading an explicit source-level boundary via control-flow, collapsing repeated or empty-arm branch chains into simpler guarded returns without modeling full CFG-level edge threading.",
    ),
    "licm": LLVMPythonTranslation(
        llvm_name="licm",
        python_passes=("licm",),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/Scalar/LICM.cpp",),
        notes="pcc hoists a conservative subset of invariant scalar assignments before simple for-loops.",
    ),
    "loop-idiom": LLVMPythonTranslation(
        llvm_name="loop-idiom",
        python_passes=("loop-opt",),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/Scalar/LoopIdiomRecognize.cpp",),
        notes="pcc gives loop-idiom an explicit analysis-only boundary via loop-opt, recording conservative zero-fill and element-copy loop idiom candidates in PassContext while LLVM keeps the actual loop-to-memset/memcpy rewrites.",
    ),
    "loop-load-elim": LLVMPythonTranslation(
        llvm_name="loop-load-elim",
        python_passes=("memory-opt-ir",),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/Scalar/LoopLoadElimination.cpp",),
        notes="pcc gives loop-load-elim a bounded low-tier boundary via memory-opt-ir, which now rewrites simple within-block repeated loads and store-load pairs that survive into loop bodies, while true loop-carried elimination and MemorySSA-based rewriting remain LLVM territory.",
    ),
    "loop-rotate": LLVMPythonTranslation(
        llvm_name="loop-rotate",
        python_passes=("loop-rotate",),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/Scalar/LoopRotation.cpp",),
        notes="pcc rotates a conservative subset of while-loops into if+do-while form.",
    ),
    "loop-simplifycfg": LLVMPythonTranslation(
        llvm_name="loop-simplifycfg",
        python_passes=("control-flow", "dce"),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/Scalar/LoopSimplifyCFG.cpp",),
        notes="pcc gives loop-simplifycfg an explicit source-level boundary via loop-local control-flow cleanup and dead-arm elimination from control-flow plus dce, while full loop CFG canonicalization remains LLVM territory.",
    ),
    "memcpyopt": LLVMPythonTranslation(
        llvm_name="memcpyopt",
        python_passes=("memory-opt-ir",),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/Scalar/MemCpyOptimizer.cpp",),
        notes="pcc gives memcpyopt an explicit analysis-only boundary via memory-opt-ir, recording memcpy/memmove/memset-like calls and nearby load/store opportunities without performing LLVM's full memcpy folding or memset/store rewriting.",
    ),
    "mldst-motion": LLVMPythonTranslation(
        llvm_name="mldst-motion",
        python_passes=("memory-opt-ir",),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/Scalar/MergedLoadStoreMotion.cpp",),
        notes="pcc gives mldst-motion a bounded low-tier boundary via memory-opt-ir, which now performs straight-line repeated-load reuse and redundant overwritten-store cleanup inside a basic block, while true cross-block merged load/store motion remains LLVM territory.",
    ),
    "newgvn": LLVMPythonTranslation(
        llvm_name="newgvn",
        python_passes=(
            "ssa-gvn",
            "ssa-gvn-rewrite",
            "gvn",
            "local-value-numbering",
            "copy-propagation",
        ),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/Scalar/NewGVN.cpp",),
        notes="pcc gives newgvn the same staged boundary as gvn: a first dominator-aware SSA analysis, a bounded SSA-backed source rewrite, plus local value numbering and copy propagation, with full LLVM newgvn equivalence-class reasoning still delegated to LLVM.",
    ),
    "reassociate": LLVMPythonTranslation(
        llvm_name="reassociate",
        python_passes=("expr-reassociation",),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/Scalar/Reassociate.cpp",),
        notes="pcc performs explicit AST-level reassociation by flattening same-op expression trees, preserving side-effect order, and recombining safe integer constants.",
    ),
    "simplifycfg": LLVMPythonTranslation(
        llvm_name="simplifycfg",
        python_passes=("canonicalize", "control-flow", "dce"),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/Scalar/SimplifyCFGPass.cpp",),
        notes="pcc gives simplifycfg an explicit source-level boundary by composing canonicalize, control-flow, and dce for branch cleanup, if-conversion, and dead-arm removal, while full CFG folding and PHI-sensitive simplification remain LLVM territory.",
    ),
    "sroa": LLVMPythonTranslation(
        llvm_name="sroa",
        python_passes=("sroa",),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/Scalar/SROA.cpp",),
        notes="pcc gives sroa an explicit analysis-only boundary: it records field-only aggregate locals as scalarization candidates in PassContext, while true aggregate splitting and SSA promotion remain LLVM territory.",
    ),
    "rpo-function-attrs": LLVMPythonTranslation(
        llvm_name="rpo-function-attrs",
        python_passes=("func-attr",),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/IPO/AttributorAttributes.cpp",),
        notes="pcc routes this pass through the same conservative source-level function attribute inference used for function-attrs.",
    ),
    "sccp": LLVMPythonTranslation(
        llvm_name="sccp",
        python_passes=(
            "ssa-sccp",
            "ssa-sccp-rewrite",
            "ssa-branch-prune",
            "canonicalize",
            "dce",
        ),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/Scalar/SCCP.cpp",),
        notes="pcc gives sccp an explicit staged boundary: ssa-sccp runs a first sparse SSA lattice analysis over pcc's internal bootstrap SSA form for constant values, reachable blocks, and foldable branches; ssa-sccp-rewrite now consumes a bounded int-typed subset of those facts to rewrite whole-expression ID sites in decl/init, assignment, and return positions to constants; ssa-branch-prune cuts a bounded subset of source-level ifs; canonicalize and dce then clean up the resulting AST. Wider width-aware SSA rewriting and general codegen use of the remaining SCCP facts remain future work.",
    ),
    "simple-loop-unswitch": LLVMPythonTranslation(
        llvm_name="simple-loop-unswitch",
        python_passes=("simple-loop-unswitch",),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/Scalar/SimpleLoopUnswitch.cpp",),
        notes="pcc treats the explicit direct-pass boundary as a verified no-op for the currently scoped upstream neighborhood, matching bare `opt -passes=simple-loop-unswitch` unless earlier loop canonicalization has already staged a richer unswitch opportunity.",
    ),
    "tailcallelim": LLVMPythonTranslation(
        llvm_name="tailcallelim",
        python_passes=("tail-call",),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/Scalar/TailRecursionElimination.cpp",),
        notes="pcc marks eligible returned call sites as tail calls in LLVM IR text; LLVM still performs the actual tail-call elimination afterwards.",
    ),
    "always-inline": LLVMPythonTranslation(
        llvm_name="always-inline",
        python_passes=("inline-opt",),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/IPO/AlwaysInliner.cpp",),
        notes="pcc performs a conservative source-level wrapper-call rewrite for eta-reducible call sites with matching signatures.",
    ),
    "annotation2metadata": LLVMPythonTranslation(
        llvm_name="annotation2metadata",
        python_passes=("noalias", "align", "loop-metadata", "range-metadata"),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/IPO/Annotation2Metadata.cpp",),
        notes="pcc expands this leaf pass into explicit IR metadata annotation passes for restrict/noalias, alignment, loop metadata opportunities, and known range opportunities.",
    ),
    "argpromotion": LLVMPythonTranslation(
        llvm_name="argpromotion",
        python_passes=("inline-opt", "sroa", "alloc-decision"),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/IPO/ArgumentPromotion.cpp",),
        notes="pcc gives argpromotion an explicit conservative source-level boundary by combining inline-opt with sroa and alloc-decision to rewrite trivial wrappers and surface promotable aggregate/scalar argument shapes as source-side scalarization and allocation opportunities, without ABI-level signature rewriting.",
    ),
    "called-value-propagation": LLVMPythonTranslation(
        llvm_name="called-value-propagation",
        python_passes=("inline-opt", "copy-propagation"),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/IPO/CalledValuePropagation.cpp",),
        notes="pcc gives called-value-propagation an explicit conservative source-level boundary by combining inline-opt and copy-propagation to collapse trivial wrapper calls and propagate direct named callees/arguments at source level, while indirect-call devirtualization remains LLVM territory.",
    ),
    "callsite-splitting": LLVMPythonTranslation(
        llvm_name="callsite-splitting",
        python_passes=("inline-opt", "control-flow"),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/IPO/CallSiteSplitting.cpp",),
        notes="pcc gives callsite-splitting an explicit source-level boundary by combining inline-opt and control-flow to simplify branch-separated wrapper call sites and if/return call fanout, while real call cloning and splitting remain LLVM territory.",
    ),
    "constraint-elimination": LLVMPythonTranslation(
        llvm_name="constraint-elimination",
        python_passes=("control-flow", "canonicalize"),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/Scalar/ConstraintElimination.cpp",),
        notes="pcc gives constraint-elimination an explicit source-level boundary by merging simple related guards through control-flow and canonicalize, such as collapsing nested side-effect-free conditions into one return guard, while LLVM retains full predicate lattice reasoning.",
    ),
    "deadargelim": LLVMPythonTranslation(
        llvm_name="deadargelim",
        python_passes=("deadargelim-analysis", "inline-opt", "dce"),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/IPO/DeadArgumentElimination.cpp",),
        notes="pcc gives deadargelim an explicit source-level boundary by inventorying unused parameters with deadargelim-analysis and composing that with inline-opt and dce cleanup around thin wrappers, without ABI-level signature rewriting.",
    ),
    "elim-avail-extern": LLVMPythonTranslation(
        llvm_name="elim-avail-extern",
        python_passes=("elim-avail-extern-src",),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/IPO/ElimAvailExtern.cpp",),
        notes="pcc gives elim-avail-extern an explicit source-level boundary by removing unused file-scope extern declarations, while linkage-level available_externally elimination remains LLVM territory.",
    ),
    "forceattrs": LLVMPythonTranslation(
        llvm_name="forceattrs",
        python_passes=("func-attr",),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/IPO/ForceFunctionAttrs.cpp",),
        notes="pcc routes this pass through the same conservative source-level function attribute inference used for function-attrs.",
    ),
    "globaldce": LLVMPythonTranslation(
        llvm_name="globaldce",
        python_passes=("global-dce",),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/IPO/GlobalDCE.cpp",),
        notes="pcc performs conservative AST-level elimination of unreachable static functions and static declarations rooted from externally visible functions and global initializer references.",
    ),
    "globalopt": LLVMPythonTranslation(
        llvm_name="globalopt",
        python_passes=("canonicalize", "dce", "inline-opt"),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/IPO/GlobalOpt.cpp",),
        notes="pcc gives globalopt an explicit conservative source-level boundary by composing canonicalize, dce, and inline-opt for static wrapper cleanup, local dead state removal, and obvious source-visible global simplifications, while linkage and data-layout rewriting remain LLVM territory.",
    ),
    "inferattrs": LLVMPythonTranslation(
        llvm_name="inferattrs",
        python_passes=("func-attr",),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/IPO/InferFunctionAttrs.cpp",),
        notes="pcc routes this pass through the same conservative source-level function attribute inference used for function-attrs.",
    ),
    "inline": LLVMPythonTranslation(
        llvm_name="inline",
        python_passes=("inline-opt",),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/IPO/Inliner.cpp",),
        notes="pcc performs a conservative source-level wrapper-call rewrite for eta-reducible call sites with matching signatures.",
    ),
    "ipsccp": LLVMPythonTranslation(
        llvm_name="ipsccp",
        python_passes=("canonicalize", "dce", "inline-opt"),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/IPO/SCCP.cpp",),
        notes="pcc gives ipsccp an explicit conservative source-level boundary by composing inline-opt with canonicalize and dce for wrapper-call simplification, local constant cleanup, and dead-arm removal after source-visible simplification, while sparse interprocedural constant propagation remains LLVM territory.",
    ),
    "loop-deletion": LLVMPythonTranslation(
        llvm_name="loop-deletion",
        python_passes=("loop-deletion",),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/Scalar/LoopDeletion.cpp",),
        notes="pcc removes a conservative subset of empty counted for-loops with local induction variables.",
    ),
    "loop-instsimplify": LLVMPythonTranslation(
        llvm_name="loop-instsimplify",
        python_passes=("loop-opt", "canonicalize"),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/Scalar/LoopInstSimplify.cpp",),
        notes="pcc gives loop-instsimplify an explicit source-level boundary by composing loop-opt with canonicalize to simplify algebraic identities and constant conditions inside loop bodies, while full loop-aware value reasoning remains LLVM territory.",
    ),
    "loop-sink": LLVMPythonTranslation(
        llvm_name="loop-sink",
        python_passes=("loop-opt",),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/Scalar/LoopSink.cpp",),
        notes="pcc gives loop-sink an explicit analysis-only boundary via loop-opt, recording guarded loop-local value definitions that are only consumed on one branch while full SSA sinking and profitability decisions remain LLVM territory.",
    ),
    "loop-unroll": LLVMPythonTranslation(
        llvm_name="loop-unroll",
        python_passes=("loop-unroll",),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/Scalar/LoopUnrollPass.cpp",),
        notes="pcc unrolls a conservative subset of medium-sized counted for-loops.",
    ),
    "loop-unroll-full": LLVMPythonTranslation(
        llvm_name="loop-unroll-full",
        python_passes=("loop-unroll-full",),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/Scalar/LoopFullUnrollPass.cpp",),
        notes="pcc fully unrolls a conservative subset of very small counted for-loops.",
    ),
    "lower-expect": LLVMPythonTranslation(
        llvm_name="lower-expect",
        python_passes=("lower-expect",),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/Scalar/LowerExpectIntrinsic.cpp",),
        notes="pcc lowers __builtin_expect* wrappers to their guarded expression.",
    ),
    "mem2reg": LLVMPythonTranslation(
        llvm_name="mem2reg",
        python_passes=("alloc-decision", "sroa"),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/Utils/PromoteMemoryToRegister.cpp",),
        notes="pcc models mem2reg as an explicit staged boundary: alloc-decision and sroa still identify promotable/local-scalar shapes at source level, while direct SSA construction plus `_lower_ssa_function()` now perform a real but intentionally narrow top-down SSA rewrite for the structured scalar subset, including direct-ID calls, explicit scalar casts, pointer loads, read-only aggregate field chains/field-address expressions, and string literals flowing into calls; address-taken locals, globals, local arrays, variadics, and full promotable-allocation coverage remain LLVM territory.",
    ),
    "speculative-execution": LLVMPythonTranslation(
        llvm_name="speculative-execution",
        python_passes=("control-flow", "canonicalize"),
        status=STATUS_DEPRECATED_SOURCE,
        upstream_sources=("llvm/lib/Transforms/Scalar/SpeculativeExecution.cpp",),
        notes="pcc gives speculative-execution an explicit source-level boundary by composing control-flow and canonicalize for speculation-friendly simplifications such as if-converting side-effect-free fallthrough returns and cleaning pure guarded expressions, while true speculative hoisting remains LLVM territory.",
    ),
    "coro-early": LLVMPythonTranslation(
        llvm_name="coro-early",
        python_passes=("coro-early",),
        status=STATUS_DEPRECATED_SOURCE,
        notes="pcc treats coro-early as a source-level coroutine builtin inventory pass that records __builtin_coro* sites without mutating the AST.",
    ),
    "ee-instrument": LLVMPythonTranslation(
        llvm_name="ee-instrument",
        python_passes=("ee-instrument",),
        status=STATUS_DEPRECATED_SOURCE,
        notes="pcc records explicit instrumentation hook sites such as __cyg_profile_*, __llvm_profile_*, and sanitizer runtime calls.",
    ),
    "openmp-opt": LLVMPythonTranslation(
        llvm_name="openmp-opt",
        python_passes=("openmp-opt",),
        status=STATUS_DEPRECATED_SOURCE,
        notes="pcc records OpenMP source/runtime inventory through pragma sites when available and __kmpc_/omp_ runtime hooks otherwise.",
    ),
    "require": LLVMPythonTranslation(
        llvm_name="require",
        python_passes=("require",),
        status=STATUS_DEPRECATED_SOURCE,
        notes="pcc treats require as an explicit analysis barrier that records module prerequisites and keeps pass scheduling visible without mutating the AST.",
    ),
    "invalidate": LLVMPythonTranslation(
        llvm_name="invalidate",
        python_passes=("invalidate",),
        status=STATUS_DEPRECATED_SOURCE,
        notes="pcc treats invalidate as an explicit source-level invalidation barrier that records current tracked analysis state instead of mutating AST structure.",
    ),
    "libcalls-shrinkwrap": LLVMPythonTranslation(
        llvm_name="libcalls-shrinkwrap",
        python_passes=("libcalls-shrinkwrap",),
        status=STATUS_DEPRECATED_SOURCE,
        notes="pcc folds an explicit source-level subset of zero-length and obviously pure libcalls, including zero-sized memcpy/memmove/memset, zero-length memcmp, equal strcmp, and empty-string strlen.",
    ),
    "coro-elide": LLVMPythonTranslation(
        llvm_name="coro-elide",
        python_passes=("coro-elide",),
        status=STATUS_DEPRECATED_SOURCE,
        notes="pcc treats coro-elide as an explicit coroutine builtin inventory boundary without AST mutation.",
    ),
    "coro-split": LLVMPythonTranslation(
        llvm_name="coro-split",
        python_passes=("coro-split",),
        status=STATUS_DEPRECATED_SOURCE,
        notes="pcc treats coro-split as an explicit coroutine builtin inventory boundary without AST mutation.",
    ),
    "coro-annotation-elide": LLVMPythonTranslation(
        llvm_name="coro-annotation-elide",
        python_passes=("coro-annotation-elide",),
        status=STATUS_DEPRECATED_SOURCE,
        notes="pcc treats coro-annotation-elide as an explicit coroutine builtin inventory boundary without AST mutation.",
    ),
    "coro-cleanup": LLVMPythonTranslation(
        llvm_name="coro-cleanup",
        python_passes=("coro-cleanup",),
        status=STATUS_DEPRECATED_SOURCE,
        notes="pcc treats coro-cleanup as an explicit coroutine builtin inventory boundary without AST mutation.",
    ),
    "recompute-globalsaa": LLVMPythonTranslation(
        llvm_name="recompute-globalsaa",
        python_passes=("recompute-globalsaa",),
        status=STATUS_DEPRECATED_SOURCE,
        notes="pcc recomputes coarse file-scope alias buckets, constness, pointer/array shape, and address-taken global summaries at the source level.",
    ),
    "float2int": LLVMPythonTranslation(
        llvm_name="float2int",
        python_passes=("float2int",),
        status=STATUS_DEPRECATED_SOURCE,
        notes="pcc folds a small conservative subset of constant float-to-int casts.",
    ),
    "lower-constant-intrinsics": LLVMPythonTranslation(
        llvm_name="lower-constant-intrinsics",
        python_passes=("lower-constant-intrinsics",),
        status=STATUS_DEPRECATED_SOURCE,
        notes="pcc lowers __builtin_constant_p at the AST level.",
    ),
    "loop-distribute": LLVMPythonTranslation(
        llvm_name="loop-distribute",
        python_passes=("loop-distribute",),
        status=STATUS_DEPRECATED_SOURCE,
        notes="pcc treats the explicit direct-pass boundary as a verified no-op for the currently scoped upstream neighborhood, matching bare `opt -passes=loop-distribute` unless richer pipeline staging has already prepared a legal distribution opportunity.",
    ),
    "inject-tli-mappings": LLVMPythonTranslation(
        llvm_name="inject-tli-mappings",
        python_passes=("inject-tli-mappings",),
        status=STATUS_DEPRECATED_SOURCE,
        notes="pcc rewrites a conservative subset of builtin libc-style calls to their plain libc/TLI entry points.",
    ),
    "loop-vectorize": LLVMPythonTranslation(
        llvm_name="loop-vectorize",
        python_passes=("loop-vectorize",),
        status=STATUS_DEPRECATED_SOURCE,
        notes="pcc treats the explicit direct-pass boundary as a verified no-op for the currently scoped upstream neighborhood, matching bare `opt -passes=loop-vectorize` unless earlier legality / canonicalization stages have prepared a vectorizable loop.",
    ),
    "vector-combine": LLVMPythonTranslation(
        llvm_name="vector-combine",
        python_passes=("vector-combine",),
        status=STATUS_DEPRECATED_SOURCE,
        notes="pcc combines a conservative subset of nested pure bitwise constant chains at the source level.",
    ),
    "transform-warning": LLVMPythonTranslation(
        llvm_name="transform-warning",
        python_passes=("transform-warning",),
        status=STATUS_DEPRECATED_SOURCE,
        notes="pcc records source-level transform blockers such as goto/label/switch/indirect-call sites instead of emitting Python warnings.",
    ),
    "alignment-from-assumptions": LLVMPythonTranslation(
        llvm_name="alignment-from-assumptions",
        python_passes=("alignment-from-assumptions",),
        status=STATUS_DEPRECATED_SOURCE,
        notes="pcc lowers __builtin_assume_aligned wrappers to their underlying pointer expression.",
    ),
    "div-rem-pairs": LLVMPythonTranslation(
        llvm_name="div-rem-pairs",
        python_passes=("div-rem-pairs",),
        status=STATUS_DEPRECATED_SOURCE,
        notes="pcc rewrites a conservative subset of adjacent integer / and % pairs to reuse the computed quotient.",
    ),
    "constmerge": LLVMPythonTranslation(
        llvm_name="constmerge",
        python_passes=("constmerge",),
        status=STATUS_DEPRECATED_SOURCE,
        notes="pcc merges a conservative subset of duplicate file-scope static const scalar definitions.",
    ),
    "cg-profile": LLVMPythonTranslation(
        llvm_name="cg-profile",
        python_passes=("cg-profile",),
        status=STATUS_DEPRECATED_SOURCE,
        notes="pcc computes a source-level callgraph profile summary covering direct, internal, indirect, and recursive calls.",
    ),
    "rel-lookup-table-converter": LLVMPythonTranslation(
        llvm_name="rel-lookup-table-converter",
        python_passes=("rel-lookup-table-converter",),
        status=STATUS_DEPRECATED_SOURCE,
        notes="pcc records conservative file-scope lookup-table candidates whose array initializers are made of symbol/address-like entries.",
    ),
    "annotation-remarks": LLVMPythonTranslation(
        llvm_name="annotation-remarks",
        python_passes=("annotation-remarks",),
        status=STATUS_DEPRECATED_SOURCE,
        notes="pcc records source annotations and builtin hints such as pragma, restrict, __builtin_expect, __builtin_assume_aligned, and __builtin_constant_p sites.",
    ),
    "verify": LLVMPythonTranslation(
        llvm_name="verify",
        python_passes=("verify",),
        status=STATUS_DEPRECATED_SOURCE,
        notes="pcc runs a cheap source-level structural verifier for the translated AST.",
    ),
    "openmp-opt-cgscc": LLVMPythonTranslation(
        llvm_name="openmp-opt-cgscc",
        python_passes=("openmp-opt-cgscc",),
        status=STATUS_DEPRECATED_SOURCE,
        notes="pcc records the same OpenMP source/runtime inventory at the explicit CGSCC boundary without mutating the AST.",
    ),
    "extra-simple-loop-unswitch-passes": LLVMPythonTranslation(
        llvm_name="extra-simple-loop-unswitch-passes",
        python_passes=("extra-simple-loop-unswitch-passes",),
        status=STATUS_DEPRECATED_SOURCE,
        notes="pcc routes the explicit extra simple loop-unswitch leaf pass through the same verified direct-pass no-op boundary used by simple-loop-unswitch.",
    ),
    "move-auto-init": LLVMPythonTranslation(
        llvm_name="move-auto-init",
        python_passes=("move-auto-init",),
        status=STATUS_DEPRECATED_SOURCE,
        notes="pcc merges a conservative subset of adjacent local decl+store pairs into initialized declarations.",
    ),
    "slp-vectorizer": LLVMPythonTranslation(
        llvm_name="slp-vectorizer",
        python_passes=("slp-vectorizer",),
        status=STATUS_DEPRECATED_SOURCE,
        notes="pcc treats the explicit direct-pass boundary as a verified no-op for the currently scoped upstream neighborhood, matching bare `opt -passes=slp-vectorizer` unless surrounding pipeline staging has already exposed a profitable SLP pack.",
    ),
    "chr": LLVMPythonTranslation(
        llvm_name="chr",
        python_passes=("chr",),
        status=STATUS_DEPRECATED_SOURCE,
        notes="pcc factors a conservative subset of identical trailing if/else branch tails into post-branch code.",
    ),
}


def _all_default_profile_pass_names() -> tuple[str, ...]:
    names: list[str] = []
    for opt_level in sorted(LLVM_DEFAULT_PROFILE_PASSES):
        for pass_name in LLVM_DEFAULT_PROFILE_PASSES[opt_level]:
            if pass_name not in names:
                names.append(pass_name)
    for pass_name in _TRANSLATION_OVERRIDES:
        if pass_name not in names:
            names.append(pass_name)
    return tuple(names)


#: IR-level pass backings landed by the all-pass-llvm-ir-1to1-master-plan.
#:
#: Each entry maps an LLVM pass name to the tuple
#: ``(ir_pass_class_path, new_status, new_tier)`` where:
#:
#: - ``ir_pass_class_path`` is an import path (``pkg.mod:Class``) of the
#:   pcc IR-level pass implementation,
#: - ``new_status`` is one of the Phase-taxonomy statuses — ``equivalent``
#:   for verified parity, ``subset`` for a documented narrower subset,
#:   ``migration-scaffold`` for framework-ready placeholders,
#: - ``new_tier`` is ``mixed`` (source-level approximation + IR-level
#:   subset coexist) or ``ir`` (source-level has been retired).
#:
#: This map is applied on top of :data:`_TRANSLATION_OVERRIDES` at registry
#: load time, so the original source-level notes / python_passes remain
#: visible for diagnostic / fallback purposes.
_IR_PASS_BACKING: dict[str, tuple[str, str, str]] = {}


def _apply_ir_backing(
    entry: LLVMPythonTranslation,
    backing: tuple[str, str, str],
) -> LLVMPythonTranslation:
    """Return a new entry with IR-pass metadata merged in."""
    ir_class, new_status, new_tier = backing
    extra = f" IR-level backing: {ir_class} (status={new_status}, tier={new_tier})."
    return LLVMPythonTranslation(
        llvm_name=entry.llvm_name,
        python_passes=entry.python_passes,
        status=new_status,
        upstream_sources=entry.upstream_sources,
        notes=(entry.notes or "").rstrip() + extra,
        implementation_tier=new_tier,
        ir_pass_class=ir_class,
    )


@lru_cache(maxsize=1)
def llvm_python_translations() -> dict[str, LLVMPythonTranslation]:
    """Return the explicit LLVM-pass registry keyed by LLVM pass name."""
    registry: dict[str, LLVMPythonTranslation] = {}
    for pass_name in _all_default_profile_pass_names():
        registry[pass_name] = _TRANSLATION_OVERRIDES.get(
            pass_name,
            LLVMPythonTranslation(llvm_name=pass_name),
        )
    # Overlay IR-level backings for passes that now have an IR-level impl.
    for llvm_name, backing in _IR_PASS_BACKING.items():
        base = registry.get(llvm_name)
        if base is None:
            base = LLVMPythonTranslation(llvm_name=llvm_name)
        registry[llvm_name] = _apply_ir_backing(base, backing)
    return registry


def llvm_python_translation(pass_name: str) -> LLVMPythonTranslation | None:
    """Return one explicit LLVM-pass registration by name."""
    return llvm_python_translations().get(str(pass_name or "").strip())


def registered_llvm_alias_names(*, include_missing: bool = False) -> tuple[str, ...]:
    """Return LLVM names that are explicitly managed in the Python registry."""
    names: list[str] = []
    for pass_name, entry in llvm_python_translations().items():
        if not include_missing and not entry.python_passes:
            continue
        names.append(pass_name)
    return tuple(names)


def expand_registered_pass_name(pass_name: str) -> tuple[str, ...]:
    """Expand one visible pass name into concrete pcc / LLVM names.

    The original LLVM name is preserved so a single spelling can control both
    layers when a matching explicit LLVM textual pipeline is active.
    """
    requested = str(pass_name or "").strip()
    if not requested:
        return ()

    expanded: list[str] = []
    entry = llvm_python_translation(requested)
    if entry is not None:
        for mapped in entry.python_passes:
            if mapped not in expanded:
                expanded.append(mapped)
    if requested not in expanded:
        expanded.append(requested)
    return tuple(expanded)


def expand_registered_pass_names(pass_names) -> tuple[str, ...]:
    """Expand multiple visible pass names, preserving order and uniqueness."""
    expanded: list[str] = []
    for pass_name in pass_names:
        for mapped in expand_registered_pass_name(pass_name):
            if mapped not in expanded:
                expanded.append(mapped)
    return tuple(expanded)
