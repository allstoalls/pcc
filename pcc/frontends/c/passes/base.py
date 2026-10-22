"""Pass base classes and pipeline — Nanopass-inspired framework.

Each pass is small, single-purpose, and composable.
The pipeline manages ordering across three tiers.
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod

from pcc.frontends.c.passes.context import PassContext

_logger = logging.getLogger("pcc.frontends.c.passes")
class Pass(ABC):
    """Base class for all passes."""

    name: str = "unnamed"

    @abstractmethod
    def run(self, input_data, ctx: PassContext):
        """Execute this pass. Returns transformed data or None for analysis-only."""
        ...

    def __repr__(self):
        return f"<{self.__class__.__name__} '{self.name}'>"


class ASTPass(Pass):
    """A pass that operates on the pycparser AST.

    Two flavors:
      - Analysis pass: reads AST, writes to PassContext, returns None
      - Transform pass: reads+modifies AST, returns modified AST
    """

    @abstractmethod
    def run(self, ast, ctx: PassContext):
        """Analyze or transform the AST. Return None for analysis-only."""
        ...


class IRPass(Pass):
    """A pass that operates on LLVM IR text after codegen.

    Receives IR text string, returns modified IR text string.
    """

    @abstractmethod
    def run(self, ir_text: str, ctx: PassContext) -> str:
        """Transform IR text. Must return the (possibly modified) IR text."""
        ...


class PassPipeline:
    """Manages and executes passes across tiers.

    Three tiers:
      high_tier:  ASTPass instances — run before codegen
      mid_tier:   (not passes — codegen reads PassContext directly)
      low_tier:   IRPass instances — run after codegen

    Inspired by:
      - Chez Scheme nanopass: many small composable passes
      - Graal tiered architecture: HighTier / MidTier / LowTier
      - Graal OptimizationLog: track what each pass does
    """

    def __init__(self):
        self.high_tier: list[ASTPass] = []
        self.low_tier: list[IRPass] = []
        self._enabled = True

    @property
    def enabled(self):
        return self._enabled

    @enabled.setter
    def enabled(self, value):
        self._enabled = value

    def add_high(self, pass_: ASTPass) -> PassPipeline:
        """Add an AST-level pass (runs before codegen)."""
        self.high_tier.append(pass_)
        return self

    def add_low(self, pass_: IRPass) -> PassPipeline:
        """Add an IR-level pass (runs after codegen)."""
        self.low_tier.append(pass_)
        return self

    def run_high_tier(self, ast, ctx: PassContext):
        """Run all HighTier passes on the AST. Returns (possibly transformed) AST."""
        if not self._enabled or not ctx.enabled:
            return ast

        for p in self.high_tier:
            if not ctx.is_pass_enabled(p.name):
                ctx.note_pass_skip(p.name, "high", "disabled")
                continue

            t0 = time.monotonic()
            try:
                result = p.run(ast, ctx)
            except Exception as exc:
                ctx.note_pass_failure(p.name, "high", exc)
                if not ctx.fail_open:
                    raise
                continue
            elapsed = time.monotonic() - t0
            _logger.debug(
                "HighTier pass '%s' completed in %.3fms", p.name, elapsed * 1000
            )
            elapsed_ms = round(elapsed * 1000, 3)
            ctx.bump(f"pass.{p.name}.time_ms", elapsed_ms)
            ctx.note_pass_run(p.name, "high", elapsed_ms)
            if result is not None:
                if p.name != "ssa-bootstrap":
                    ctx.clear_ssa_artifacts(reason=f"{p.name} rewrote AST")
                ast = result  # transform pass returned modified AST

        return ast

    def run_low_tier(self, ir_text: str, ctx: PassContext) -> str:
        """Run all LowTier passes on LLVM IR text. Returns modified IR."""
        if not self._enabled or not ctx.enabled:
            return ir_text

        for p in self.low_tier:
            if not ctx.is_pass_enabled(p.name):
                ctx.note_pass_skip(p.name, "low", "disabled")
                continue

            t0 = time.monotonic()
            try:
                ir_text = p.run(ir_text, ctx)
            except Exception as exc:
                ctx.note_pass_failure(p.name, "low", exc)
                if not ctx.fail_open:
                    raise
                continue
            elapsed = time.monotonic() - t0
            _logger.debug(
                "LowTier pass '%s' completed in %.3fms", p.name, elapsed * 1000
            )
            elapsed_ms = round(elapsed * 1000, 3)
            ctx.bump(f"pass.{p.name}.time_ms", elapsed_ms)
            ctx.note_pass_run(p.name, "low", elapsed_ms)

        return ir_text




    @classmethod
    def default(cls) -> PassPipeline:
        """Create the default optimization pipeline.

        Inspired by:
          - Graal's three-tier architecture (HighTier/MidTier/LowTier)
          - Chez Scheme's nanopass framework (small composable passes)
          - Graal's CanonicalizerPhase (iterative canonicalization)

        Pass ordering follows dataflow: analysis → transform → metadata.
        """
        # ── HighTier imports ────────────────────────────────────────────
        from pcc.frontends.c.passes.escape_analysis import EscapeAnalysisPass
        from pcc.frontends.c.passes.alloc_decision import AllocDecisionPass
        from pcc.frontends.c.passes.nsw_inference import NSWInferencePass
        from pcc.frontends.c.passes.canonicalize import CanonicalizerPass
        from pcc.frontends.c.passes.dce import DCEPass
        from pcc.frontends.c.passes.control_flow import ControlFlowPass
        from pcc.frontends.c.passes.global_dce import GlobalDCEPass
        from pcc.frontends.c.passes.lower_expect import LowerExpectPass
        from pcc.frontends.c.passes.propagation import CopyPropagationPass, ExpressionReassociationPass, LocalValueNumberingPass, GVNPass, SROAPass
        from pcc.frontends.c.passes.ssa_bootstrap import SSABootstrapPass
        from pcc.frontends.c.passes.ssa_adce import SSAADCEPass
        from pcc.frontends.c.passes.ssa_branch_prune import SSABranchPrunePass
        from pcc.frontends.c.passes.ssa_dse import SSADSEPass
        from pcc.frontends.c.passes.ssa_gvn import SSAGVNPass
        from pcc.frontends.c.passes.ssa_gvn_rewrite import SSAGVNRewritePass
        from pcc.frontends.c.passes.ssa_loop_phi import SSALoopPhiPass
        from pcc.frontends.c.passes.ssa_sccp import SSASCCPPass
        from pcc.frontends.c.passes.ssa_sccp_rewrite import SSASCCPRewritePass
        from pcc.frontends.c.passes.loop_opt import LoopOptPass
        from pcc.frontends.c.passes.llvm_loop_explicit import IndVarsPass, LICMPass, LoopDeletionPass, LoopFullUnrollPass, LoopRotatePass, LoopUnrollPass, SimpleLoopUnswitchPass
        from pcc.frontends.c.passes.inline_opt import InlineOptPass
        from pcc.frontends.c.passes.ipo_boundary import DeadArgElimAnalysisPass, ElimAvailExternPass
        from pcc.frontends.c.passes.llvm_explicit import preanalysis_explicit_llvm_passes, precanonicalize_explicit_llvm_passes, late_explicit_llvm_passes
        from pcc.frontends.c.passes.chez_transforms import LetElevationPass, PrimitiveSpecializationPass, RecursiveUnrollingPass, AssignmentConversionPass, LoopRecognitionPass, SCCAnalysisPass, ClosureLiftingPass, RedundantCheckPass, FloatUnboxingPass

        # ── LowTier imports ────────────────────────────────────────────
        from pcc.frontends.c.passes.ir_metadata import NoaliasPass, AlignPass, NSWAnnotationPass, LoopMetadataPass, FuncAttrPass, RangeMetadataPass
        from pcc.frontends.c.passes.memory_opt import MemoryOptIRPass
        from pcc.frontends.c.passes.clang_compat import TailCallPass, NoundefPass

        pipeline = cls()

        # ════════════════════════════════════════════════════════════════
        # HighTier Phase 1: AST Analysis (populate PassContext)
        #
        # Run analysis on the original AST before transform passes start
        # deleting or rewriting source-level variables. This keeps
        # PassContext stable for codegen and pass diagnostics.
        # ════════════════════════════════════════════════════════════════
        pipeline.add_high(LowerExpectPass())           # LLVM lower-expect builtin lowering — must run before SSABootstrapPass so the SSA builder sees plain expressions, not unlinked __builtin_expect FuncCalls
        pipeline.add_high(EscapeAnalysisPass())        # 41: variable escape analysis
        pipeline.add_high(AllocDecisionPass())         # 42-43: alloca vs SSA decision
        pipeline.add_high(NSWInferencePass())          # 59: overflow flag inference
        pipeline.add_high(LoopOptPass())               # 25-32: loop analysis + hints
        pipeline.add_high(InlineOptPass())             # 38-40, 44-45, 53: inline analysis
        pipeline.add_high(DeadArgElimAnalysisPass())   # explicit dead-arg boundary
        pipeline.add_high(GlobalDCEPass())             # IPO/global dead code cleanup
        pipeline.add_high(ElimAvailExternPass())       # explicit unused extern cleanup
        pipeline.add_high(SROAPass())                  # 36: scalar replacement analysis
        pipeline.add_high(LocalValueNumberingPass())   # 34: local CSE
        pipeline.add_high(GVNPass())                   # 33: global VN (delegates to LLVM)
        pipeline.add_high(SSABootstrapPass())          # experimental internal SSA bootstrap
        pipeline.add_high(SSAGVNPass())                # experimental SSA GVN analysis
        pipeline.add_high(SSASCCPPass())               # experimental SSA SCCP analysis
        pipeline.add_high(SSALoopPhiPass())            # Phase 5: SSA loop-phi classifier (induction/reduction/dead/invariant)
        for pass_ in preanalysis_explicit_llvm_passes():
            pipeline.add_high(pass_)

        # ════════════════════════════════════════════════════════════════
        # HighTier Phase 2: Chez Scheme Nanopass-Inspired Analysis
        # ════════════════════════════════════════════════════════════════
        pipeline.add_high(SCCAnalysisPass())           # 51: Tarjan SCC on call graph
        pipeline.add_high(LoopRecognitionPass())       # 50: tail-recursive loop detection
        pipeline.add_high(RecursiveUnrollingPass())    # 48: recursive unroll candidates
        pipeline.add_high(AssignmentConversionPass())  # 49: mutability classification
        pipeline.add_high(LetElevationPass())          # 46: binding elevation candidates
        pipeline.add_high(PrimitiveSpecializationPass())  # 47: known-type specialization
        pipeline.add_high(ClosureLiftingPass())        # 52: function pointer devirt
        pipeline.add_high(RedundantCheckPass())        # 54: null guard analysis
        pipeline.add_high(FloatUnboxingPass())         # 55: FP register candidates

        # ════════════════════════════════════════════════════════════════
        # HighTier Phase 3: AST Transforms
        #
        # These run after analysis so PassContext still sees original source
        # structure even when transforms are conservative and decide to elide
        # temporary variables or branches.
        # ════════════════════════════════════════════════════════════════
        pipeline.add_high(SSABranchPrunePass())        # prune branches proven constant by SSA SCCP
        pipeline.add_high(SSASCCPRewritePass())        # rewrite bounded int ID sites to SSA-proven constants
        pipeline.add_high(SSAGVNRewritePass())         # reuse dominating SSA-proven scalar values
        pipeline.add_high(SSADSEPass())                # preserve side effects while dropping dead SSA-backed local stores
        pipeline.add_high(SSAADCEPass())               # remove dead SSA-proven local scalar bindings
        for pass_ in precanonicalize_explicit_llvm_passes():
            pipeline.add_high(pass_)
        pipeline.add_high(LICMPass())                  # explicit LICM lowering
        pipeline.add_high(IndVarsPass())              # explicit induction-var simplification
        pipeline.add_high(LoopRotatePass())           # explicit loop rotation
        pipeline.add_high(SimpleLoopUnswitchPass())    # explicit loop unswitching
        pipeline.add_high(LoopDeletionPass())          # explicit loop deletion
        pipeline.add_high(LoopUnrollPass())           # explicit medium small-loop unroll
        pipeline.add_high(LoopFullUnrollPass())        # explicit small-loop full unroll
        pipeline.add_high(CanonicalizerPass())         # 1-8: const fold, strength red, algebraic
        pipeline.add_high(DCEPass())                   # 9-12: dead code, unreachable, dead branch
        pipeline.add_high(ControlFlowPass())           # 13-18: cond elim, branch simplify, if-conv
        pipeline.add_high(ExpressionReassociationPass())  # 35: reassociate for const folding
        pipeline.add_high(CopyPropagationPass())       # 37: a=b; use(a) → use(b)
        pipeline.add_high(CanonicalizerPass())         # 2nd round
        pipeline.add_high(DCEPass())                   # 2nd round — clean up after propagation
        for pass_ in late_explicit_llvm_passes():
            pipeline.add_high(pass_)

        # ════════════════════════════════════════════════════════════════
        # LowTier: IR Post-Processing (add metadata for LLVM)
        # ════════════════════════════════════════════════════════════════
        pipeline.add_low(TailCallPass())               # clang: tail call annotation
        pipeline.add_low(NoundefPass())                # clang: noundef on params
        pipeline.add_low(MemoryOptIRPass())            # 19-24: memory opt analysis
        pipeline.add_low(NoaliasPass())                # 57: restrict → noalias
        pipeline.add_low(AlignPass())                  # 58: alignment hints
        pipeline.add_low(NSWAnnotationPass())          # 59: nsw/nuw opportunities
        pipeline.add_low(FuncAttrPass())               # 61: nounwind, readonly, etc.
        pipeline.add_low(LoopMetadataPass())           # 60: !llvm.loop hints
        pipeline.add_low(RangeMetadataPass())          # 62: !range metadata

        return pipeline

    @classmethod
    def minimal(cls) -> PassPipeline:
        """Minimal pipeline: only analysis, no AST transforms."""
        from pcc.frontends.c.passes.escape_analysis import EscapeAnalysisPass
        from pcc.frontends.c.passes.alloc_decision import AllocDecisionPass
        from pcc.frontends.c.passes.nsw_inference import NSWInferencePass

        pipeline = cls()
        pipeline.add_high(EscapeAnalysisPass())
        pipeline.add_high(AllocDecisionPass())
        pipeline.add_high(NSWInferencePass())
        return pipeline

    @classmethod
    def with_tbaa(cls) -> PassPipeline:
        """Full pipeline with TBAA metadata injection (experimental)."""
        from pcc.frontends.c.passes.tbaa import TBAAPass

        pipeline = cls.default()
        pipeline.add_low(TBAAPass())
        return pipeline

    def describe(self) -> str:
        lines = ["PassPipeline:"]
        lines.append("  HighTier (AST → PassContext):")
        for i, p in enumerate(self.high_tier, 1):
            lines.append(f"    {i}. {p.name}")
        lines.append("  MidTier (codegen reads PassContext)")
        lines.append("  LowTier (IR → IR):")
        for i, p in enumerate(self.low_tier, 1):
            lines.append(f"    {i}. {p.name}")
        return "\n".join(lines)
