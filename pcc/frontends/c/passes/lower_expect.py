"""Lower __builtin_expect-style wrappers to plain expressions."""

from __future__ import annotations

from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.passes.ast_utils import ASTTransformer, is_side_effect_free
from pcc.frontends.c.passes.base import ASTPass
from pcc.frontends.c.passes.context import PassContext


class _LowerExpectTransformer(ASTTransformer):
    def __init__(self, ctx: PassContext):
        self.ctx = ctx
        self.changed = False

    def _mark(self, action, node):
        self.changed = True
        coord = getattr(node, "coord", None)
        self.ctx.record("lower-expect", action, f"{coord}" if coord else "?")
        self.ctx.bump(f"lower_expect.{action}")

    def visit_FuncCall(self, node):
        self._visit_children(node)
        if not isinstance(node.name, c_ast.ID):
            return node
        if node.args is None or not hasattr(node.args, "exprs"):
            return node

        arg_nodes = list(node.args.exprs or [])
        if node.name.name == "__builtin_expect":
            if len(arg_nodes) == 2 and is_side_effect_free(arg_nodes[1]):
                self._mark("lowered", node)
                return arg_nodes[0]
            return node

        if node.name.name == "__builtin_expect_with_probability":
            if (
                len(arg_nodes) == 3
                and is_side_effect_free(arg_nodes[1])
                and is_side_effect_free(arg_nodes[2])
            ):
                self._mark("lowered", node)
                return arg_nodes[0]
        return node


class LowerExpectPass(ASTPass):
    """Source-level lowering for lower-expect style builtins."""

    name = "lower-expect"

    def run(self, ast, ctx: PassContext):
        transformer = _LowerExpectTransformer(ctx)
        ast = transformer.visit(ast)
        return ast if transformer.changed else None
