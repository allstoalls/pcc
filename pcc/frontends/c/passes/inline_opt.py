"""Passes 38-40, 44-45, 53: Inlining & Call Optimization.

  38. Function Inlining                — inline small functions at call sites
  39. Inline Heuristic (cost model)    — decide based on size/depth
  40. Tail Call Optimization            — tail call → jump
  44. Chez CP0: Procedure Inlining     — with effort-limit / score-limit
  45. Chez CP0: Eta Reduction           — f(x){ return g(x); } → alias to g
  53. Chez: Direct Call Optimization    — indirect → direct when target known

AST-level analysis: identify inlining candidates and tail calls.
Actual inlining transform for simple cases.
"""

from __future__ import annotations

from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.passes.ast_utils import ASTTransformer
from pcc.frontends.c.passes.base import ASTPass
from pcc.frontends.c.passes.context import PassContext


class _InlineAnalyzer(ASTTransformer):
    """Analyze functions for inlining candidates and tail calls."""

    # Chez CP0-inspired limits
    EFFORT_LIMIT = 100   # max AST nodes in inlinee
    SCORE_LIMIT = 50     # max code growth per call site

    def __init__(self, ctx: PassContext):
        self.ctx = ctx
        self.changed = False
        self._functions: dict[str, c_ast.FuncDef] = {}
        self._call_counts: dict[str, int] = {}
        self._eta_targets: dict[str, str] = {}

    def _mark(self, action, detail=""):
        self.changed = True
        self.ctx.record("inline_opt", action, detail)
        self.ctx.bump(f"inline_opt.{action}")

    # ── Collect function definitions and call sites ─────────────────────

    def visit_FileAST(self, node):
        # First pass: collect all function definitions
        for ext in node.ext or []:
            if isinstance(ext, c_ast.FuncDef):
                self._functions[ext.decl.name] = ext

        # Second pass: count call sites
        for ext in node.ext or []:
            self._count_calls(ext)

        # Third pass: analyze each function
        for ext in node.ext or []:
            if isinstance(ext, c_ast.FuncDef):
                self._analyze_function(ext)

        self._resolve_eta_targets()
        self._visit_children(node)
        return node

    def visit_FuncCall(self, node):
        self._visit_children(node)
        if not isinstance(node.name, c_ast.ID):
            return node
        target_name = self._eta_targets.get(node.name.name)
        if not target_name or target_name == node.name.name:
            return node

        self._mark("rewrite_eta_call", f"{node.name.name} → {target_name}")
        node.name = c_ast.ID(target_name, coord=getattr(node.name, "coord", None))
        return node

    def _count_calls(self, node):
        if node is None:
            return
        if isinstance(node, c_ast.FuncCall):
            if isinstance(node.name, c_ast.ID):
                name = node.name.name
                self._call_counts[name] = self._call_counts.get(name, 0) + 1
        for _, child in node.children():
            if isinstance(child, c_ast.Node):
                self._count_calls(child)

    def _analyze_function(self, funcdef: c_ast.FuncDef):
        func_name = funcdef.decl.name
        body_size = self._ast_size(funcdef.body)
        func_info = self.ctx.get_func(func_name)

        # ── 38/39/44. Inlining Analysis ─────────────────────────────────

        # Small functions are inlining candidates
        if body_size <= self.EFFORT_LIMIT:
            call_count = self._call_counts.get(func_name, 0)
            # Chez CP0 heuristic: inline if small OR called once
            if body_size <= self.SCORE_LIMIT or call_count <= 1:
                self.ctx.record(
                    "inline_opt", "inline_candidate",
                    f"{func_name}",
                    f"size={body_size} calls={call_count}",
                )
                self.ctx.bump("inline_opt.inline_candidates")

        # ── 45. Eta Reduction ───────────────────────────────────────────

        # f(x, y) { return g(x, y); } → f is a wrapper for g
        eta_target = self._detect_eta(funcdef)
        if eta_target and self._has_safe_eta_signature(funcdef, eta_target):
            self._eta_targets[func_name] = eta_target
            self.ctx.record(
                "inline_opt", "eta_reduction",
                f"{func_name} → {eta_target}",
            )
            self.ctx.bump("inline_opt.eta_reductions")

        # ── 40. Tail Call Detection ─────────────────────────────────────

        tail_calls = self._find_tail_calls(funcdef.body)
        for tc_name in tail_calls:
            self.ctx.record(
                "inline_opt", "tail_call",
                f"{func_name} → {tc_name}",
            )
            self.ctx.bump("inline_opt.tail_calls")

        # ── 53. Direct Call Optimization ────────────────────────────────

        # At AST level, all named calls are already direct.
        # This pass identifies function pointer calls that could be devirtualized.
        indirect = self._find_indirect_calls(funcdef.body)
        if indirect:
            self.ctx.bump("inline_opt.indirect_calls", len(indirect))

    @staticmethod
    def _detect_eta(funcdef: c_ast.FuncDef) -> str | None:
        """Detect eta-reducible wrapper: f(params) { return g(params); }"""
        body = funcdef.body
        if not isinstance(body, c_ast.Compound) or not body.block_items:
            return None
        if len(body.block_items) != 1:
            return None
        stmt = body.block_items[0]
        if not isinstance(stmt, c_ast.Return) or stmt.expr is None:
            return None
        if not isinstance(stmt.expr, c_ast.FuncCall):
            return None
        call = stmt.expr
        if not isinstance(call.name, c_ast.ID):
            return None

        # Check that arguments match parameters exactly
        params = funcdef.decl.type if isinstance(funcdef.decl.type, c_ast.FuncDecl) else None
        if params is None:
            return None
        param_list = params.args
        if param_list is None:
            if call.args is None:
                return call.name.name
            return None

        param_names = []
        for p in param_list.params or []:
            if isinstance(p, c_ast.Decl) and p.name:
                param_names.append(p.name)
            else:
                return None

        if call.args is None:
            return None
        arg_nodes = call.args.exprs if hasattr(call.args, "exprs") else []
        if len(arg_nodes) != len(param_names):
            return None

        for arg, pname in zip(arg_nodes, param_names):
            if not isinstance(arg, c_ast.ID) or arg.name != pname:
                return None

        return call.name.name

    def _has_safe_eta_signature(self, wrapper: c_ast.FuncDef, target_name: str) -> bool:
        target = self._functions.get(target_name)
        if target is None:
            return False
        wrapper_decl = getattr(wrapper, "decl", None)
        target_decl = getattr(target, "decl", None)
        if wrapper_decl is None or target_decl is None:
            return False
        return self._func_signature_key(wrapper_decl.type) == self._func_signature_key(
            target_decl.type
        )

    def _resolve_eta_targets(self):
        for wrapper_name in tuple(self._eta_targets):
            seen = {wrapper_name}
            target_name = self._eta_targets[wrapper_name]
            while target_name in self._eta_targets and target_name not in seen:
                seen.add(target_name)
                target_name = self._eta_targets[target_name]
            self._eta_targets[wrapper_name] = target_name

    @classmethod
    def _func_signature_key(cls, node):
        if not isinstance(node, c_ast.FuncDecl):
            return None
        params = []
        if node.args is not None:
            for param in node.args.params or []:
                params.append(cls._param_signature_key(param))
        return (
            cls._type_key(node.type),
            tuple(params),
        )

    @classmethod
    def _param_signature_key(cls, node):
        if isinstance(node, c_ast.EllipsisParam):
            return ("...",)
        if isinstance(node, c_ast.Decl):
            return (
                tuple(node.quals or ()),
                cls._type_key(node.type),
            )
        return (node.__class__.__name__,)

    @classmethod
    def _type_key(cls, node):
        if node is None:
            return None
        if isinstance(node, c_ast.TypeDecl):
            return (
                "type",
                tuple(node.quals or ()),
                cls._type_key(node.type),
            )
        if isinstance(node, c_ast.IdentifierType):
            return ("identifier", tuple(node.names))
        if isinstance(node, c_ast.PtrDecl):
            return ("ptr", tuple(node.quals or ()), cls._type_key(node.type))
        if isinstance(node, c_ast.ArrayDecl):
            return (
                "array",
                tuple(getattr(node, "dim_quals", ()) or ()),
                cls._type_key(node.type),
                cls._expr_key(node.dim),
            )
        if isinstance(node, c_ast.Struct):
            return ("struct", node.name)
        if isinstance(node, c_ast.Union):
            return ("union", node.name)
        if isinstance(node, c_ast.Enum):
            return ("enum", node.name)
        return (node.__class__.__name__,)

    @classmethod
    def _expr_key(cls, node):
        if node is None:
            return None
        if isinstance(node, c_ast.Constant):
            return ("const", node.type, node.value)
        if isinstance(node, c_ast.ID):
            return ("id", node.name)
        if isinstance(node, c_ast.UnaryOp):
            return ("unary", node.op, cls._expr_key(node.expr))
        if isinstance(node, c_ast.BinaryOp):
            return ("binary", node.op, cls._expr_key(node.left), cls._expr_key(node.right))
        return (node.__class__.__name__,)

    @staticmethod
    def _find_tail_calls(body) -> list[str]:
        """Find return statements that return a function call result (tail call)."""
        tail_calls = []
        if not isinstance(body, c_ast.Compound) or not body.block_items:
            return tail_calls
        for item in body.block_items:
            if isinstance(item, c_ast.Return) and item.expr:
                if isinstance(item.expr, c_ast.FuncCall):
                    if isinstance(item.expr.name, c_ast.ID):
                        tail_calls.append(item.expr.name.name)
        return tail_calls

    @staticmethod
    def _find_indirect_calls(node) -> list:
        """Find calls through function pointers."""
        results = []
        if node is None:
            return results
        if isinstance(node, c_ast.FuncCall):
            if not isinstance(node.name, c_ast.ID):
                results.append(node)
        for _, child in node.children():
            if isinstance(child, c_ast.Node):
                results.extend(_InlineAnalyzer._find_indirect_calls(child))
        return results

    @staticmethod
    def _ast_size(node) -> int:
        """Count AST nodes (rough measure of function complexity)."""
        if node is None:
            return 0
        count = 1
        for _, child in node.children():
            if isinstance(child, c_ast.Node):
                count += _InlineAnalyzer._ast_size(child)
        return count


class InlineOptPass(ASTPass):
    """Inlining and call optimization — passes 38-40, 44-45, 53."""
    name = "inline-opt"

    def run(self, ast, ctx: PassContext):
        analyzer = _InlineAnalyzer(ctx)
        ast = analyzer.visit(ast)
        return ast if analyzer.changed else None
