"""Analysis-only pass that builds a restricted internal SSA model."""

from __future__ import annotations

from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.ssa import SSAConstructionError, SSABuilder
from pcc.frontends.c.passes.base import ASTPass
from pcc.frontends.c.passes.context import PassContext


class SSABootstrapPass(ASTPass):
    """Lower supported functions into the internal SSA bootstrap form."""

    name = "ssa-bootstrap"

    def __init__(self):
        self._builder = SSABuilder()

    def run(self, ast, ctx: PassContext):
        if not isinstance(ast, c_ast.FileAST):
            return None

        self._builder.target_triple = ctx.target_triple
        ctx.ssa_functions.clear()
        self._builder.index_file_scope(ast)
        built = 0
        skipped = 0

        for ext in ast.ext or []:
            if not isinstance(ext, c_ast.FuncDef):
                continue

            func_name = ext.decl.name
            try:
                ssa_func = self._builder.build_function(ext)
            except SSAConstructionError as exc:
                skipped += 1
                ctx.record(self.name, "skip_function", func_name, str(exc))
                continue

            ctx.ssa_functions[func_name] = ssa_func
            ctx.record(
                self.name,
                "built",
                func_name,
                f"blocks={len(ssa_func.blocks)}",
            )
            built += 1

        if built:
            ctx.bump("ssa.bootstrap.success", built)
        if skipped:
            ctx.bump("ssa.bootstrap.skipped", skipped)
        return None
