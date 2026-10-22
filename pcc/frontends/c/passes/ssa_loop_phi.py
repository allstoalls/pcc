"""Phase 5 SSA-backed loop-phi analysis pass (analysis-only)."""

from __future__ import annotations

from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.ssa import LoopPhiKind, SSALoopPhiAnalyzer
from pcc.frontends.c.passes.base import ASTPass
from pcc.frontends.c.passes.context import PassContext
from pcc.frontends.c.passes.ssa_bootstrap import SSABootstrapPass


class SSALoopPhiPass(ASTPass):
    """Classify loop-header phi nodes into dead/invariant/induction/
    reduction/other, recording counts in PassContext.

    This pass is analysis-only for now. Downstream consumers can use the
    recorded `ssa.loop_phi.*` counters for vectorization hints and
    reduction-aware rewrites.
    """

    name = "ssa-loop-phi"

    def __init__(self):
        self._bootstrap = SSABootstrapPass()
        self._analyzer = SSALoopPhiAnalyzer()

    def run(self, ast, ctx: PassContext):
        if not isinstance(ast, c_ast.FileAST):
            return None
        if not ctx.ssa_functions:
            self._bootstrap.run(ast, ctx)

        total_counts = {kind: 0 for kind in LoopPhiKind}
        for func_name, ssa_func in ctx.ssa_functions.items():
            result = self._analyzer.analyze(ssa_func)
            counts = result.counts()
            for kind, n in counts.items():
                total_counts[kind] += n
            if result.classifications:
                ctx.record(
                    self.name,
                    "classified",
                    func_name,
                    (
                        f"dead={counts[LoopPhiKind.DEAD]} "
                        f"invariant={counts[LoopPhiKind.INVARIANT]} "
                        f"induction={counts[LoopPhiKind.INDUCTION]} "
                        f"reduction={counts[LoopPhiKind.REDUCTION]} "
                        f"other={counts[LoopPhiKind.OTHER]}"
                    ),
                )
            ctx.ssa_loop_phi_results = getattr(ctx, "ssa_loop_phi_results", {})
            ctx.ssa_loop_phi_results[func_name] = result

        for kind, n in total_counts.items():
            if n:
                ctx.bump(f"ssa.loop_phi.{kind.value}", n)
        return None
