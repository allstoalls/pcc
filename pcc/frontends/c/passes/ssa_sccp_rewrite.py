"""Transform pass that rewrites int-valued ID sites using SSA SCCP facts."""

from __future__ import annotations

from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.ssa import LatticeKind, SSAReturn
from pcc.frontends.c.passes.ast_utils import ASTTransformer
from pcc.frontends.c.passes.base import ASTPass
from pcc.frontends.c.passes.context import PassContext
from pcc.frontends.c.passes.ssa_sccp import SSASCCPPass


class _SSASCCPRewriter(ASTTransformer):
    _INT_TYPES = frozenset({"int", "signed", "signed int"})
    _INT_MIN = -(2 ** 31)
    _INT_MAX = 2 ** 31 - 1

    def __init__(self, ctx: PassContext):
        self.ctx = ctx
        self.changed = False
        self._current_func_name: str | None = None
        self._current_return_type: str = ""
        self._current_types: dict[str, str] = {}
        self._decl_constants: dict[str, list[int | None]] = {}
        self._assign_constants: dict[str, list[int | None]] = {}
        self._return_constants: dict[str, list[int | None]] = {}

    def visit_FuncDef(self, node):
        func_name = getattr(getattr(node, "decl", None), "name", None)
        if not func_name:
            self._visit_children(node)
            return node

        old_name = self._current_func_name
        old_return_type = self._current_return_type
        old_types = self._current_types
        old_decls = self._decl_constants
        old_assigns = self._assign_constants
        old_returns = self._return_constants

        self._current_func_name = func_name
        self._current_return_type = self._decl_type_name(
            getattr(getattr(node, "decl", None), "type", None)
        )
        self._current_types = self._collect_function_types(node)
        decisions = self._decisions_for_function(func_name)
        self._decl_constants = decisions["decl_init"]
        self._assign_constants = decisions["assign"]
        self._return_constants = decisions["return"]
        self._visit_children(node)

        self._current_func_name = old_name
        self._current_return_type = old_return_type
        self._current_types = old_types
        self._decl_constants = old_decls
        self._assign_constants = old_assigns
        self._return_constants = old_returns
        return node

    def visit_Decl(self, node):
        if node.init is not None:
            replacement = self._replacement_for_site(
                coord_key=self._coord_key(node),
                expr=node.init,
                action="rewrite_decl",
                type_name=self._type_name(node.name),
                decisions=self._decl_constants,
            )
            if replacement is not None:
                node.init = replacement
                return node
            node.init = self.visit(node.init)
        return node

    def visit_Assignment(self, node):
        if node.lvalue is not None:
            node.lvalue = self.visit(node.lvalue)

        if (
            node.op == "="
            and isinstance(node.lvalue, c_ast.ID)
            and node.rvalue is not None
        ):
            replacement = self._replacement_for_site(
                coord_key=self._coord_key(node),
                expr=node.rvalue,
                action="rewrite_assign",
                type_name=self._type_name(node.lvalue.name),
                decisions=self._assign_constants,
            )
            if replacement is not None:
                node.rvalue = replacement
                return node

        if node.rvalue is not None:
            node.rvalue = self.visit(node.rvalue)
        return node

    def visit_Return(self, node):
        if node.expr is not None:
            replacement = self._replacement_for_site(
                coord_key=self._coord_key(node),
                expr=node.expr,
                action="rewrite_return",
                type_name=self._current_return_type,
                decisions=self._return_constants,
            )
            if replacement is not None:
                node.expr = replacement
                return node
            node.expr = self.visit(node.expr)
        return node

    def _replacement_for_site(
        self,
        *,
        coord_key: str | None,
        expr,
        action: str,
        type_name: str,
        decisions: dict[str, list[int | None]],
    ):
        if coord_key is None:
            return None
        queue = decisions.get(coord_key)
        if not queue:
            return None
        constant = queue.pop(0)
        if constant is None:
            return None
        if not isinstance(expr, c_ast.ID):
            return None
        if not self._is_safe_target_type(type_name):
            return None
        if not self._is_safe_int_constant(constant):
            return None

        replacement = self._make_int_expr(constant, expr.coord)
        if self._expr_already_matches(expr, replacement):
            return None

        self.changed = True
        detail = f"{self._current_func_name or '<anon>'}:{constant}"
        self.ctx.record("ssa-sccp-rewrite", action, coord_key, detail)
        self.ctx.bump(f"ssa_sccp_rewrite.{action}")
        return replacement

    def _decisions_for_function(
        self,
        func_name: str,
    ) -> dict[str, dict[str, list[int | None]]]:
        decisions = {"decl_init": {}, "assign": {}, "return": {}}
        result = self.ctx.ssa_sccp_results.get(func_name)
        ssa_func = self.ctx.ssa_functions.get(func_name)
        if result is None or ssa_func is None:
            return decisions

        reachable = result.reachable_blocks
        for binding in ssa_func.bindings:
            if binding.source_coord is None or binding.kind not in {"decl_init", "assign"}:
                continue
            lattice = result.lattice_for(binding.value)
            constant = (
                lattice.constant
                if binding.block_name in reachable and self._is_safe_constant_lattice(lattice)
                else None
            )
            decisions[binding.kind].setdefault(binding.source_coord, []).append(constant)

        for block in ssa_func.blocks:
            term = block.terminator
            if not isinstance(term, SSAReturn) or term.source_coord is None:
                continue
            lattice = result.lattice_for(term.value)
            constant = (
                lattice.constant
                if block.name in reachable and self._is_safe_constant_lattice(lattice)
                else None
            )
            decisions["return"].setdefault(term.source_coord, []).append(constant)

        return decisions

    @classmethod
    def _normalize_type_name(cls, type_name: str) -> str:
        tokens = [
            token
            for token in type_name.replace("\t", " ").split()
            if token not in {"const", "volatile", "restrict", "__restrict", "register"}
        ]
        return " ".join(tokens)

    @classmethod
    def _is_safe_target_type(cls, type_name: str) -> bool:
        return cls._normalize_type_name(type_name) in cls._INT_TYPES

    @classmethod
    def _is_safe_constant_lattice(cls, lattice) -> bool:
        return (
            lattice.kind == LatticeKind.CONSTANT
            and lattice.constant is not None
            and lattice.is_safe
            and cls._is_safe_int_constant(lattice.constant)
        )

    @classmethod
    def _is_safe_int_constant(cls, value: int) -> bool:
        return cls._INT_MIN <= value <= cls._INT_MAX

    @staticmethod
    def _make_int_expr(value: int, coord):
        if value >= 0:
            return c_ast.Constant("int", str(value), coord=coord)
        return c_ast.UnaryOp(
            "-",
            c_ast.Constant("int", str(abs(value)), coord=coord),
            coord=coord,
        )

    @staticmethod
    def _expr_already_matches(expr, replacement) -> bool:
        if isinstance(expr, c_ast.Constant) and isinstance(replacement, c_ast.Constant):
            return expr.type == replacement.type and expr.value == replacement.value
        if isinstance(expr, c_ast.UnaryOp) and isinstance(replacement, c_ast.UnaryOp):
            return (
                expr.op == replacement.op
                and isinstance(expr.expr, c_ast.Constant)
                and isinstance(replacement.expr, c_ast.Constant)
                and expr.expr.type == replacement.expr.type
                and expr.expr.value == replacement.expr.value
            )
        return False

    @staticmethod
    def _coord_key(node) -> str | None:
        coord = getattr(node, "coord", None)
        return str(coord) if coord is not None else None

    def _type_name(self, var_name: str | None) -> str:
        if not var_name:
            return ""
        type_name = self._current_types.get(var_name, "")
        if type_name:
            return type_name
        if not self._current_func_name:
            return ""
        func_info = self.ctx.functions.get(self._current_func_name)
        if func_info is None:
            return ""
        var_info = func_info.var_infos.get(var_name)
        if var_info is None:
            return ""
        return self._normalize_type_name(var_info.type_name or "")

    def _decl_type_name(self, node) -> str:
        if isinstance(node, c_ast.FuncDecl):
            return self._decl_type_name(node.type)
        if isinstance(node, c_ast.TypeDecl):
            return self._decl_type_name(node.type)
        if isinstance(node, c_ast.IdentifierType):
            return self._normalize_type_name(" ".join(node.names))
        return ""

    def _collect_function_types(self, funcdef) -> dict[str, str]:
        types: dict[str, str] = {}
        func_type = getattr(getattr(funcdef, "decl", None), "type", None)
        params = getattr(getattr(func_type, "args", None), "params", None) or ()
        for param in params:
            if isinstance(param, c_ast.Decl) and param.name:
                type_name = self._decl_type_name(param.type)
                if type_name:
                    types[param.name] = type_name

        def _walk(node):
            if node is None:
                return
            if isinstance(node, c_ast.Decl) and node.name:
                type_name = self._decl_type_name(node.type)
                if type_name:
                    types.setdefault(node.name, type_name)
            for _, child in node.children():
                if isinstance(child, c_ast.Node):
                    _walk(child)
                elif isinstance(child, list):
                    for item in child:
                        if isinstance(item, c_ast.Node):
                            _walk(item)

        _walk(getattr(funcdef, "body", None))
        return types


class SSASCCPRewritePass(ASTPass):
    """Rewrite bounded int-typed ID sites to SCCP-proven constants."""

    name = "ssa-sccp-rewrite"

    def __init__(self):
        self._sccp = SSASCCPPass()

    def run(self, ast, ctx: PassContext):
        if not isinstance(ast, c_ast.FileAST):
            return None

        if not ctx.ssa_sccp_results:
            self._sccp.run(ast, ctx)

        rewriter = _SSASCCPRewriter(ctx)
        ast = rewriter.visit(ast)
        return ast if rewriter.changed else None
