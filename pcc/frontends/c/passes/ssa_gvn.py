"""Analysis-only GVN pass over the internal bootstrap SSA form."""

from __future__ import annotations

from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.ssa import SSAGVNAnalyzer
from pcc.frontends.c.passes.base import ASTPass
from pcc.frontends.c.passes.context import PassContext
from pcc.frontends.c.passes.ssa_bootstrap import SSABootstrapPass


class SSAGVNPass(ASTPass):
    """Run dominator-aware value numbering on supported bootstrap SSA."""

    name = "ssa-gvn"

    def __init__(self):
        self._bootstrap = SSABootstrapPass()
        self._analyzer = SSAGVNAnalyzer()

    def run(self, ast, ctx: PassContext):
        if not isinstance(ast, c_ast.FileAST):
            return None

        if not ctx.ssa_functions:
            self._bootstrap.run(ast, ctx)

        ctx.ssa_gvn_results.clear()
        analyzed = 0
        redundant_values = 0
        expressions_seen = 0

        for func_name, ssa_func in ctx.ssa_functions.items():
            result = self._analyzer.analyze(ssa_func)
            ctx.ssa_gvn_results[func_name] = result
            analyzed += 1
            redundant_values += len(result.redundant_values)
            expressions_seen += result.expressions_seen
            ctx.record(
                self.name,
                "analyzed",
                func_name,
                (
                    f"expressions={result.expressions_seen}, "
                    f"redundant={len(result.redundant_values)}"
                ),
            )

        if analyzed:
            ctx.bump("ssa.gvn.functions", analyzed)
        if expressions_seen:
            ctx.bump("ssa.gvn.expressions", expressions_seen)
        if redundant_values:
            ctx.bump("ssa.gvn.redundant_values", redundant_values)
        return None
