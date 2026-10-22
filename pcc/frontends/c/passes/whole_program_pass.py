"""Phase 6 whole-program analysis pass (analysis-only).

Intended to be run before per-TU compilation when multiple ASTs are
available. Records cross-TU facts in PassContext for downstream consumers
(specialization, dead-function removal, cross-TU constant propagation).
"""

from __future__ import annotations

from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.passes.base import ASTPass
from pcc.frontends.c.passes.context import PassContext
from pcc.frontends.c.passes.whole_program import WholeProgramAnalyzer


class WholeProgramAnalysisPass(ASTPass):
    """Analyze a multi-TU program and stash the result on `ctx`.

    Unlike most ASTPasses (which take a single AST), this one expects
    `ctx.whole_program_asts` to be a list of `(unit_name, ast)` tuples
    pre-populated by the driver. If absent, it degrades to analyzing
    only the AST that was passed in.
    """

    name = "whole-program-analysis"

    def __init__(self):
        self._analyzer = WholeProgramAnalyzer()

    def run(self, ast, ctx: PassContext):
        if not isinstance(ast, c_ast.FileAST):
            return None
        asts = getattr(ctx, "whole_program_asts", None)
        if not asts:
            asts = [("<anon>", ast)]
        result = self._analyzer.analyze(asts)
        ctx.whole_program_result = result

        if result.dead_internal_functions:
            ctx.bump(
                "whole_program.dead_internal_functions",
                len(result.dead_internal_functions),
            )
            for name in sorted(result.dead_internal_functions):
                ctx.record(self.name, "dead_internal", name, "no callers")

        if result.specialization_candidates:
            ctx.bump(
                "whole_program.specialization_candidates",
                len(result.specialization_candidates),
            )
            for func, cands in sorted(result.specialization_candidates.items()):
                detail = ", ".join(
                    f"arg{i}={v!r}" for i, v in sorted(cands.items())
                )
                ctx.record(self.name, "specializable", func, detail)

        ctx.bump("whole_program.functions", len(result.functions))
        ctx.bump("whole_program.call_sites", len(result.call_sites))
        return None
