"""Transform pass that consumes SSA dead bindings while preserving RHS effects."""

from __future__ import annotations

from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.ssa import SSAADCEAnalyzer
from pcc.frontends.c.passes.ast_utils import is_side_effect_free
from pcc.frontends.c.passes.base import ASTPass
from pcc.frontends.c.passes.context import PassContext
from pcc.frontends.c.passes.ssa_adce import _binding_can_drop_from_source
from pcc.frontends.c.passes.ssa_bootstrap import SSABootstrapPass


class _SSADSERewriter:
    def __init__(
        self,
        ctx: PassContext,
        dead_binding_kinds: dict[str, dict[str, str]],
    ):
        self.ctx = ctx
        self.dead_binding_kinds = dead_binding_kinds
        self.changed = False
        self._current_func_name: str | None = None
        self._current_types: dict[str, str] = {}
        self._current_dead_bindings: dict[str, str] = {}

    def run(self, ast: c_ast.FileAST) -> bool:
        for ext in ast.ext or ():
            if isinstance(ext, c_ast.FuncDef):
                self._rewrite_function(ext)
        return self.changed

    def _rewrite_function(self, funcdef: c_ast.FuncDef) -> None:
        func_name = getattr(getattr(funcdef, "decl", None), "name", None)
        if not func_name or not isinstance(funcdef.body, c_ast.Compound):
            return

        old_name = self._current_func_name
        old_types = self._current_types
        old_dead_bindings = self._current_dead_bindings

        self._current_func_name = func_name
        self._current_types = self._collect_function_types(funcdef)
        self._current_dead_bindings = self.dead_binding_kinds.get(func_name, {})
        self._rewrite_compound(funcdef.body)

        self._current_func_name = old_name
        self._current_types = old_types
        self._current_dead_bindings = old_dead_bindings

    def _rewrite_compound(
        self,
        compound: c_ast.Compound,
        continuation: tuple[c_ast.Node | None, ...] = (),
    ) -> None:
        new_items: list[c_ast.Node] = []
        items = list(compound.block_items or ())
        for index, stmt in enumerate(items):
            tail = tuple(items[index + 1:]) + tuple(continuation)
            new_items.extend(self._rewrite_stmt(stmt, tail))
        compound.block_items = new_items

    def _rewrite_stmt(
        self,
        stmt: c_ast.Node | None,
        continuation: tuple[c_ast.Node | None, ...],
    ) -> list[c_ast.Node]:
        if stmt is None:
            return []
        if isinstance(stmt, c_ast.Compound):
            self._rewrite_compound(stmt, continuation)
            return [stmt]
        if isinstance(stmt, c_ast.If):
            stmt.iftrue = self._rewrite_branch_stmt(stmt.iftrue, continuation)
            stmt.iffalse = self._rewrite_branch_stmt(stmt.iffalse, continuation)
            return [stmt]
        if isinstance(stmt, c_ast.Decl):
            return self._rewrite_decl(stmt, continuation)
        if isinstance(stmt, c_ast.Assignment):
            return self._rewrite_assignment(stmt, continuation)
        return [stmt]

    def _rewrite_branch_stmt(
        self,
        stmt: c_ast.Node | None,
        continuation: tuple[c_ast.Node | None, ...],
    ) -> c_ast.Node | None:
        rewritten = self._rewrite_stmt(stmt, continuation)
        if not rewritten:
            return None
        if len(rewritten) == 1:
            return rewritten[0]
        coord = getattr(stmt, "coord", None)
        return c_ast.Compound(block_items=rewritten, coord=coord)

    def _rewrite_decl(
        self,
        node: c_ast.Decl,
        continuation: tuple[c_ast.Node | None, ...],
    ) -> list[c_ast.Node]:
        if node.init is None:
            return [node]

        coord_key = self._coord_key(node)
        if (
            coord_key is None
            or self._current_dead_bindings.get(coord_key) != "decl_init"
            or not self._is_local_scalar_name(node.name)
            or not _binding_can_drop_from_source(continuation, node.name)
        ):
            return [node]

        detail = f"{self._current_func_name or '<anon>'}:{node.name}"
        if is_side_effect_free(node.init):
            return [node]

        effect_expr = node.init
        node.init = None
        self.changed = True
        self.ctx.record("ssa-dse", "preserve_effect_init", coord_key, detail)
        self.ctx.bump("ssa_dse.preserve_effect_init")
        return [node, effect_expr]

    def _rewrite_assignment(
        self,
        node: c_ast.Assignment,
        continuation: tuple[c_ast.Node | None, ...],
    ) -> list[c_ast.Node]:
        if (
            node.op != "="
            or not isinstance(node.lvalue, c_ast.ID)
            or node.rvalue is None
        ):
            return [node]

        coord_key = self._coord_key(node)
        if (
            coord_key is None
            or self._current_dead_bindings.get(coord_key) != "assign"
            or not self._is_local_scalar_name(node.lvalue.name)
            or not _binding_can_drop_from_source(continuation, node.lvalue.name)
        ):
            return [node]

        detail = f"{self._current_func_name or '<anon>'}:{node.lvalue.name}"
        if is_side_effect_free(node.rvalue):
            return [node]

        self.changed = True
        self.ctx.record("ssa-dse", "preserve_effect_assign", coord_key, detail)
        self.ctx.bump("ssa_dse.preserve_effect_assign")
        return [node.rvalue]

    def _is_local_scalar_name(self, name: str | None) -> bool:
        if not name:
            return False
        return self._is_scalar_type_name(self._current_types.get(name, ""))

    @staticmethod
    def _coord_key(node) -> str | None:
        coord = getattr(node, "coord", None)
        return str(coord) if coord is not None else None

    @staticmethod
    def _normalize_type_name(type_name: str) -> str:
        tokens = [
            token
            for token in type_name.replace("\t", " ").split()
            if token not in {"const", "volatile", "restrict", "__restrict", "register"}
        ]
        return " ".join(tokens)

    @classmethod
    def _is_scalar_type_name(cls, type_name: str) -> bool:
        normalized = cls._normalize_type_name(type_name)
        if not normalized:
            return False
        if any(ch in normalized for ch in "*[]()"):
            return False
        if normalized.startswith(("struct ", "union ", "enum ")):
            return False
        return True

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


class SSADSEPass(ASTPass):
    """Delete dead local scalar stores while preserving side-effecting RHS code."""

    name = "ssa-dse"

    def __init__(self):
        self._bootstrap = SSABootstrapPass()
        self._analyzer = SSAADCEAnalyzer()

    def run(self, ast, ctx: PassContext):
        if not isinstance(ast, c_ast.FileAST):
            return None

        if not ctx.ssa_functions:
            self._bootstrap.run(ast, ctx)

        dead_binding_kinds: dict[str, dict[str, str]] = {}
        analyzed = 0
        dead_bindings = 0

        for func_name, ssa_func in ctx.ssa_functions.items():
            result = self._analyzer.analyze(ssa_func)
            dead_binding_kinds[func_name] = dict(result.dead_bindings)
            analyzed += 1
            dead_bindings += len(result.dead_bindings)
            ctx.record(
                self.name,
                "analyzed",
                func_name,
                f"dead_bindings={len(result.dead_bindings)}",
            )

        if analyzed:
            ctx.bump("ssa.dse.functions", analyzed)
        if dead_bindings:
            ctx.bump("ssa.dse.dead_bindings", dead_bindings)

        rewriter = _SSADSERewriter(ctx, dead_binding_kinds)
        rewriter.run(ast)
        return ast if rewriter.changed else None
