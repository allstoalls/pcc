"""Transform pass that removes dead local scalar bindings proven by SSA ADCE."""

from __future__ import annotations

from dataclasses import dataclass

from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.ssa import SSAADCEAnalyzer
from pcc.frontends.c.passes.ast_utils import ASTTransformer, is_side_effect_free
from pcc.frontends.c.passes.base import ASTPass
from pcc.frontends.c.passes.context import PassContext
from pcc.frontends.c.passes.ssa_bootstrap import SSABootstrapPass


@dataclass(frozen=True)
class _BindingFlow:
    reads: bool
    live_fallthrough: bool


def _node_reads_name(node: c_ast.Node | None, target_name: str) -> bool:
    if node is None:
        return False
    if isinstance(node, c_ast.ID):
        return node.name == target_name
    for _, child in node.children():
        if isinstance(child, c_ast.Node):
            if _node_reads_name(child, target_name):
                return True
        elif isinstance(child, list):
            for item in child:
                if isinstance(item, c_ast.Node) and _node_reads_name(item, target_name):
                    return True
    return False


def _sequence_binding_flow(
    items: tuple[c_ast.Node | None, ...] | list[c_ast.Node | None],
    target_name: str,
) -> _BindingFlow:
    for stmt in items or ():
        flow = _stmt_binding_flow(stmt, target_name)
        if flow.reads:
            return flow
        if not flow.live_fallthrough:
            return flow
    return _BindingFlow(reads=False, live_fallthrough=True)


def _stmt_binding_flow(
    stmt: c_ast.Node | None,
    target_name: str,
) -> _BindingFlow:
    if stmt is None:
        return _BindingFlow(reads=False, live_fallthrough=True)

    if isinstance(stmt, c_ast.Compound):
        return _sequence_binding_flow(stmt.block_items or (), target_name)

    if isinstance(stmt, c_ast.If):
        if _node_reads_name(stmt.cond, target_name):
            return _BindingFlow(reads=True, live_fallthrough=False)
        true_flow = _stmt_binding_flow(stmt.iftrue, target_name)
        if true_flow.reads:
            return true_flow
        false_flow = _stmt_binding_flow(stmt.iffalse, target_name)
        if false_flow.reads:
            return false_flow
        return _BindingFlow(
            reads=False,
            live_fallthrough=true_flow.live_fallthrough or false_flow.live_fallthrough,
        )

    if isinstance(stmt, c_ast.Assignment):
        if isinstance(stmt.lvalue, c_ast.ID) and stmt.lvalue.name == target_name:
            if _node_reads_name(stmt.rvalue, target_name):
                return _BindingFlow(reads=True, live_fallthrough=False)
            return _BindingFlow(reads=False, live_fallthrough=False)
        if _node_reads_name(stmt, target_name):
            return _BindingFlow(reads=True, live_fallthrough=False)
        return _BindingFlow(reads=False, live_fallthrough=True)

    if isinstance(stmt, c_ast.Decl):
        init = getattr(stmt, "init", None)
        if init is not None and _node_reads_name(init, target_name):
            return _BindingFlow(reads=True, live_fallthrough=False)
        return _BindingFlow(reads=False, live_fallthrough=True)

    if isinstance(stmt, (c_ast.Return, c_ast.Break, c_ast.Continue, c_ast.Goto)):
        return _BindingFlow(
            reads=_node_reads_name(stmt, target_name),
            live_fallthrough=False,
        )

    if _node_reads_name(stmt, target_name):
        return _BindingFlow(reads=True, live_fallthrough=False)
    return _BindingFlow(reads=False, live_fallthrough=True)


def _binding_can_drop_from_source(
    continuation: tuple[c_ast.Node | None, ...],
    target_name: str,
) -> bool:
    return not _sequence_binding_flow(continuation, target_name).reads


class _SSAADCETransformer(ASTTransformer):
    def __init__(self, ctx: PassContext):
        self.ctx = ctx
        self.changed = False
        self._current_func_name: str | None = None
        self._current_types: dict[str, str] = {}
        self._dead_binding_kinds: dict[str, str] = {}

    def visit_FuncDef(self, node):
        func_name = getattr(getattr(node, "decl", None), "name", None)
        if not func_name:
            self._visit_children(node)
            return node

        old_name = self._current_func_name
        old_types = self._current_types
        old_dead = self._dead_binding_kinds

        self._current_func_name = func_name
        self._current_types = self._collect_function_types(node)
        self._dead_binding_kinds = self._decisions_for_function(func_name)
        if isinstance(node.body, c_ast.Compound):
            self._rewrite_compound(node.body, ())
        else:
            self._visit_children(node)

        self._current_func_name = old_name
        self._current_types = old_types
        self._dead_binding_kinds = old_dead
        return node

    def _rewrite_compound(
        self,
        compound: c_ast.Compound,
        continuation: tuple[c_ast.Node | None, ...],
    ) -> None:
        items = list(compound.block_items or ())
        rewritten: list[c_ast.Node] = []
        for index, stmt in enumerate(items):
            tail = tuple(items[index + 1:]) + tuple(continuation)
            new_stmt = self._rewrite_stmt(stmt, tail)
            if new_stmt is not None:
                rewritten.append(new_stmt)
        compound.block_items = rewritten

    def _rewrite_stmt(
        self,
        stmt: c_ast.Node | None,
        continuation: tuple[c_ast.Node | None, ...],
    ) -> c_ast.Node | None:
        if stmt is None:
            return None
        if isinstance(stmt, c_ast.Compound):
            self._rewrite_compound(stmt, continuation)
            return stmt
        if isinstance(stmt, c_ast.If):
            stmt.iftrue = self._rewrite_stmt(stmt.iftrue, continuation)
            stmt.iffalse = self._rewrite_stmt(stmt.iffalse, continuation)
            stmt.cond = self.visit(stmt.cond)
            return stmt
        if isinstance(stmt, c_ast.Decl):
            return self._rewrite_decl(stmt, continuation)
        if isinstance(stmt, c_ast.Assignment):
            return self._rewrite_assignment(stmt, continuation)
        return ASTTransformer.generic_visit(self, stmt)

    def _rewrite_decl(self, node, continuation):
        if node.init is not None:
            coord_key = str(node.coord) if getattr(node, "coord", None) else None
            if (
                coord_key is not None
                and self._dead_binding_kinds.get(coord_key) == "decl_init"
                and self._is_local_scalar_name(node.name)
                and is_side_effect_free(node.init)
                and _binding_can_drop_from_source(continuation, node.name)
            ):
                self.changed = True
                detail = f"{self._current_func_name or '<anon>'}:{node.name}"
                self.ctx.record("ssa-adce", "drop_init", coord_key, detail)
                self.ctx.bump("ssa_adce.drop_init")
                node.init = None
                return node
            node.init = self.visit(node.init)
        return node

    def _rewrite_assignment(self, node, continuation):
        if (
            node.op == "="
            and isinstance(node.lvalue, c_ast.ID)
            and node.rvalue is not None
        ):
            coord_key = str(node.coord) if getattr(node, "coord", None) else None
            if (
                coord_key is not None
                and self._dead_binding_kinds.get(coord_key) == "assign"
                and self._is_local_scalar_name(node.lvalue.name)
                and is_side_effect_free(node.rvalue)
                and _binding_can_drop_from_source(continuation, node.lvalue.name)
            ):
                self.changed = True
                detail = f"{self._current_func_name or '<anon>'}:{node.lvalue.name}"
                self.ctx.record("ssa-adce", "drop_assign", coord_key, detail)
                self.ctx.bump("ssa_adce.drop_assign")
                return None

        return ASTTransformer.generic_visit(self, node)

    def _decisions_for_function(self, func_name: str) -> dict[str, str]:
        result = self.ctx.ssa_adce_results.get(func_name)
        if result is None:
            return {}
        return dict(result.dead_bindings)

    def _is_local_scalar_name(self, name: str | None) -> bool:
        if not name:
            return False
        return self._is_scalar_type_name(self._current_types.get(name, ""))

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


class SSAADCEPass(ASTPass):
    """Remove dead local scalar bindings using bootstrap SSA liveness."""

    name = "ssa-adce"

    def __init__(self):
        self._bootstrap = SSABootstrapPass()
        self._analyzer = SSAADCEAnalyzer()

    def run(self, ast, ctx: PassContext):
        if not isinstance(ast, c_ast.FileAST):
            return None

        if not ctx.ssa_functions:
            self._bootstrap.run(ast, ctx)

        ctx.ssa_adce_results.clear()
        analyzed = 0
        live_values = 0
        dead_bindings = 0

        for func_name, ssa_func in ctx.ssa_functions.items():
            result = self._analyzer.analyze(ssa_func)
            ctx.ssa_adce_results[func_name] = result
            analyzed += 1
            live_values += len(result.live_value_names)
            dead_bindings += len(result.dead_bindings)
            ctx.record(
                self.name,
                "analyzed",
                func_name,
                (
                    f"live_values={len(result.live_value_names)}, "
                    f"dead_bindings={len(result.dead_bindings)}"
                ),
            )

        if analyzed:
            ctx.bump("ssa.adce.functions", analyzed)
        if live_values:
            ctx.bump("ssa.adce.live_values", live_values)
        if dead_bindings:
            ctx.bump("ssa.adce.dead_bindings", dead_bindings)

        transformer = _SSAADCETransformer(ctx)
        ast = transformer.visit(ast)
        if transformer.changed:
            # The AST has been rewritten (dead `int x = a + 1;` init
            # cleared, dead `x = ...;` assignment removed). The SSA IR
            # cached in `ctx.ssa_functions` is now stale — it still
            # contains the instructions that produced the now-removed
            # bindings, and SSA codegen would re-emit them verbatim.
            # Re-run bootstrap so downstream passes (codegen, sccp,
            # gvn) see the cleaned-up function body.
            self._bootstrap.run(ast, ctx)
            return ast
        return None
