"""Analysis-only SCCP pass over the internal bootstrap SSA form."""

from __future__ import annotations

from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.ssa import LatticeKind, SSASCCPAnalyzer
from pcc.frontends.c.passes.base import ASTPass
from pcc.frontends.c.passes.context import PassContext
from pcc.frontends.c.passes.ssa_bootstrap import SSABootstrapPass


class SSASCCPPass(ASTPass):
    """Run SCCP over supported functions lowered by the bootstrap SSA builder."""

    name = "ssa-sccp"

    def __init__(self):
        self._bootstrap = SSABootstrapPass()
        self._analyzer = SSASCCPAnalyzer()

    def run(self, ast, ctx: PassContext):
        if not isinstance(ast, c_ast.FileAST):
            return None

        if not ctx.ssa_functions:
            self._bootstrap.run(ast, ctx)

        ctx.ssa_sccp_results.clear()
        analyzed = 0
        constant_values = 0
        reachable_blocks = 0
        folded_branches = 0

        for func_name, ssa_func in ctx.ssa_functions.items():
            result = self._analyzer.analyze(ssa_func)
            ctx.ssa_sccp_results[func_name] = result
            analyzed += 1
            constant_values += sum(
                1
                for value in result.values.values()
                if value.kind == LatticeKind.CONSTANT
            )
            reachable_blocks += len(result.reachable_blocks)
            folded_branches += len(result.folded_branches)
            ctx.record(
                self.name,
                "analyzed",
                func_name,
                (
                    f"constants={len(result.constant_value_names())}, "
                    f"reachable_blocks={len(result.reachable_blocks)}, "
                    f"folded_branches={len(result.folded_branches)}"
                ),
            )

        if analyzed:
            ctx.bump("ssa.sccp.functions", analyzed)
        if constant_values:
            ctx.bump("ssa.sccp.constant_values", constant_values)
        if reachable_blocks:
            ctx.bump("ssa.sccp.reachable_blocks", reachable_blocks)
        if folded_branches:
            ctx.bump("ssa.sccp.folded_branches", folded_branches)
        return None
