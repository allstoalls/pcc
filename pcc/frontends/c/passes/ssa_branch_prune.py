"""Transform pass that prunes branches proven constant by SSA SCCP."""

from __future__ import annotations

from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.ssa import SSABranch
from pcc.frontends.c.passes.ast_utils import ASTTransformer
from pcc.frontends.c.passes.base import ASTPass
from pcc.frontends.c.passes.context import PassContext
from pcc.frontends.c.passes.ssa_sccp import SSASCCPPass


class _SSABranchPruner(ASTTransformer):
    def __init__(self, ctx: PassContext):
        self.ctx = ctx
        self.changed = False
        self._current_func_name: str | None = None
        self._coord_decisions: dict[str, bool] = {}

    def visit_FuncDef(self, node):
        func_name = getattr(getattr(node, "decl", None), "name", None)
        if not func_name:
            self._visit_children(node)
            return node

        old_name = self._current_func_name
        old_decisions = self._coord_decisions
        self._current_func_name = func_name
        self._coord_decisions = self._decisions_for_function(func_name)
        self._visit_children(node)
        self._current_func_name = old_name
        self._coord_decisions = old_decisions
        return node

    def visit_If(self, node):
        self._visit_children(node)

        coord_key = str(node.coord) if getattr(node, "coord", None) else None
        if coord_key is None:
            return node

        decision = self._coord_decisions.get(coord_key)
        if decision is None:
            return node

        action = "fold_true" if decision else "fold_false"
        detail = self._current_func_name or "<anon>"
        self.ctx.record("ssa-branch-prune", action, coord_key, detail)
        self.ctx.bump(f"ssa_branch_prune.{action}")
        self.changed = True

        if decision:
            return node.iftrue if node.iftrue is not None else c_ast.EmptyStatement(coord=node.coord)
        if node.iffalse is not None:
            return node.iffalse
        return None

    def _decisions_for_function(self, func_name: str) -> dict[str, bool]:
        decisions: dict[str, bool] = {}
        result = self.ctx.ssa_sccp_results.get(func_name)
        ssa_func = self.ctx.ssa_functions.get(func_name)
        if result is None or ssa_func is None:
            return decisions

        for block_name, chosen_target in result.folded_branches.items():
            try:
                block = ssa_func.block(block_name)
            except KeyError:
                continue
            term = block.terminator
            if not isinstance(term, SSABranch) or not term.source_coord:
                continue
            decisions[term.source_coord] = chosen_target == term.true_target
        return decisions


class SSABranchPrunePass(ASTPass):
    """Prune constant branches proven by the internal SSA SCCP analysis."""

    name = "ssa-branch-prune"

    def __init__(self):
        self._sccp = SSASCCPPass()

    def run(self, ast, ctx: PassContext):
        if not isinstance(ast, c_ast.FileAST):
            return None

        if not ctx.ssa_sccp_results:
            self._sccp.run(ast, ctx)

        pruner = _SSABranchPruner(ctx)
        ast = pruner.visit(ast)
        return ast if pruner.changed else None
