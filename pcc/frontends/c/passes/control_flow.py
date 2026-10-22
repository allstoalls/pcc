"""Passes 13-18: Control Flow Optimization.

  13. Conditional Elimination   — remove branches with known conditions
  14. Branch Simplification     — if(a) if(a) → if(a), simplify nested ifs
  15. Tail Duplication          — duplicate tail code to eliminate jumps
  16. Block Merging             — merge single-entry-single-exit blocks
  17. Jump Threading            — chain of gotos → direct jump
  18. If-Conversion             — simple if/else → ternary (help LLVM cmov)
"""

from __future__ import annotations

from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.passes.ast_utils import ASTTransformer, get_int_value, has_unstructured_control_flow, is_constant_int, nodes_equal, is_side_effect_free
from pcc.frontends.c.passes.base import ASTPass
from pcc.frontends.c.passes.context import PassContext


class _ControlFlowOptimizer(ASTTransformer):

    def __init__(self, ctx: PassContext):
        self.ctx = ctx
        self.changed = False

    def _mark(self, action, node):
        self.changed = True
        coord = getattr(node, "coord", None)
        self.ctx.record("control_flow", action, f"{coord}" if coord else "?")
        self.ctx.bump(f"control_flow.{action}")

    def visit_FuncDef(self, node):
        if has_unstructured_control_flow(node):
            func_name = getattr(getattr(node, "decl", None), "name", "<anon>")
            self.ctx.record(
                "control_flow", "skip_function", func_name,
                "unstructured control flow",
            )
            self.ctx.bump("control_flow.skipped_functions")
            return node

        self._visit_children(node)
        return node

    # ── 13. Conditional Elimination ─────────────────────────────────────

    def visit_If(self, node):
        self._visit_children(node)

        if self._is_empty_statement(node.iffalse):
            node.iffalse = None

        if (
            node.iffalse is not None
            and is_side_effect_free(node.cond)
            and self._same_simple_stmt(node.iftrue, node.iffalse)
        ):
            self._mark("identical_if_arms", node)
            return self._unwrap_single_stmt(node.iftrue) or node.iftrue

        # if (cond) ; else stmt  ->  if (!cond) stmt
        if self._is_empty_statement(node.iftrue) and node.iffalse is not None:
            self._mark("empty_then_negate", node)
            node.cond = c_ast.UnaryOp("!", node.cond, coord=node.coord)
            node.iftrue = node.iffalse
            node.iffalse = None
            return node

        # Already handled by DCE for constant conditions;
        # here we handle logical simplifications.

        # if(!cond) { A } else { B } → if(cond) { B } else { A }
        if (
            isinstance(node.cond, c_ast.UnaryOp)
            and node.cond.op == "!"
            and node.iffalse is not None
        ):
            self._mark("cond_elim_negate_swap", node)
            node.cond = node.cond.expr
            node.iftrue, node.iffalse = node.iffalse, node.iftrue
            return node

        # if (x) if (y) body;  ->  if (x && y) body;
        nested_if = self._unwrap_nested_if(node.iftrue)
        if (
            node.iffalse is None
            and nested_if is not None
            and nested_if.iffalse is None
            and self._same_simple_expr(node.cond, nested_if.cond)
        ):
            self._mark("dedupe_nested_if", node)
            node.iftrue = nested_if.iftrue
            return node

        if (
            node.iffalse is None
            and nested_if is not None
            and nested_if.iffalse is None
            and is_side_effect_free(node.cond)
            and is_side_effect_free(nested_if.cond)
        ):
            self._mark("merge_nested_if", node)
            node.cond = c_ast.BinaryOp("&&", node.cond, nested_if.cond, coord=node.coord)
            node.iftrue = nested_if.iftrue
            return node

        return node

    # ── 14. Branch Simplification ───────────────────────────────────────

    def visit_Compound(self, node):
        self._visit_children(node)
        if not node.block_items:
            return node

        new_items = []
        for item in node.block_items:
            # Flatten nested compounds: { { stmt; } } → { stmt; }
            if isinstance(item, c_ast.Compound) and item.block_items:
                # Only flatten if no declarations (to avoid scope issues)
                has_decls = any(
                    isinstance(s, c_ast.Decl) for s in item.block_items
                )
                if not has_decls:
                    self._mark("flatten_compound", item)
                    new_items.extend(item.block_items)
                    continue
            # Remove empty statements
            if isinstance(item, c_ast.EmptyStatement):
                self._mark("remove_empty_stmt", item)
                continue
            new_items.append(item)

        rewritten_items = []
        idx = 0
        while idx < len(new_items):
            converted, consumed = self._try_fallthrough_return_if_conversion(new_items, idx)
            if converted is None:
                consumed = 1
                converted = self._try_if_conversion(new_items, idx)
            if converted is None:
                converted = self._try_return_if_conversion(new_items, idx)
            rewritten_items.append(converted or new_items[idx])
            idx += consumed

        node.block_items = rewritten_items
        return node

    # ── 16. Block Merging ───────────────────────────────────────────────
    # (Handled by flatten_compound above)

    # ── 17. Jump Threading ──────────────────────────────────────────────

    def visit_Goto(self, node):
        # Jump threading requires full CFG analysis.
        # For AST level, we just pass through — LLVM handles this well.
        return node

    # ── 15. Tail Duplication ────────────────────────────────────────────
    # (Complex transform, better done at LLVM IR level)

    # ── 18. If-Conversion ───────────────────────────────────────────────

    # Convert simple if/else assignment to ternary (helps LLVM emit cmov)
    # if(c) x = a; else x = b; → x = c ? a : b;
    # Only when both branches are simple assignments to the same variable
    def _try_if_conversion(self, items, idx):
        """Try to convert if/else into ternary at position idx."""
        if idx >= len(items):
            return None
        node = items[idx]
        if not isinstance(node, c_ast.If) or node.iffalse is None:
            return None

        true_assign = self._extract_simple_assign(node.iftrue)
        false_assign = self._extract_simple_assign(node.iffalse)

        if true_assign is None or false_assign is None:
            return None

        true_target, true_val = true_assign
        false_target, false_val = false_assign

        # Must assign to same variable
        if not (isinstance(true_target, c_ast.ID) and isinstance(false_target, c_ast.ID)):
            return None
        if true_target.name != false_target.name:
            return None

        # Condition must be side-effect free
        if not is_side_effect_free(node.cond):
            return None

        self._mark("if_conversion", node)
        ternary = c_ast.TernaryOp(node.cond, true_val, false_val, coord=node.coord)
        return c_ast.Assignment("=", true_target, ternary, coord=node.coord)

    def _try_return_if_conversion(self, items, idx):
        """Try to convert if/else returns into a single ternary return."""
        if idx >= len(items):
            return None
        node = items[idx]
        if not isinstance(node, c_ast.If) or node.iffalse is None:
            return None

        true_expr = self._extract_simple_return(node.iftrue)
        false_expr = self._extract_simple_return(node.iffalse)
        if true_expr is None or false_expr is None:
            return None
        if not is_side_effect_free(node.cond):
            return None

        self._mark("if_return_conversion", node)
        return c_ast.Return(
            c_ast.TernaryOp(node.cond, true_expr, false_expr, coord=node.coord),
            coord=node.coord,
        )

    def _try_fallthrough_return_if_conversion(self, items, idx):
        """Try `if (c) return a; return b;` -> `return c ? a : b;`."""
        if idx + 1 >= len(items):
            return None, 1
        node = items[idx]
        if not isinstance(node, c_ast.If) or node.iffalse is not None:
            return None, 1

        true_expr = self._extract_simple_return(node.iftrue)
        false_expr = self._extract_simple_return(items[idx + 1])
        if true_expr is None or false_expr is None:
            return None, 1
        if not is_side_effect_free(node.cond):
            return None, 1

        self._mark("if_fallthrough_return_conversion", node)
        return (
            c_ast.Return(
                c_ast.TernaryOp(node.cond, true_expr, false_expr, coord=node.coord),
                coord=node.coord,
            ),
            2,
        )

    @staticmethod
    def _extract_simple_assign(stmt):
        """Extract (target, value) from a simple assignment statement."""
        if isinstance(stmt, c_ast.Assignment) and stmt.op == "=":
            return stmt.lvalue, stmt.rvalue
        if isinstance(stmt, c_ast.Compound) and stmt.block_items:
            if len(stmt.block_items) == 1:
                inner = stmt.block_items[0]
                if isinstance(inner, c_ast.Assignment) and inner.op == "=":
                    return inner.lvalue, inner.rvalue
        return None

    @staticmethod
    def _extract_simple_return(stmt):
        if isinstance(stmt, c_ast.Return):
            return stmt.expr
        if isinstance(stmt, c_ast.Compound) and stmt.block_items and len(stmt.block_items) == 1:
            inner = stmt.block_items[0]
            if isinstance(inner, c_ast.Return):
                return inner.expr
        return None

    @staticmethod
    def _is_empty_statement(stmt):
        if stmt is None:
            return True
        if isinstance(stmt, c_ast.EmptyStatement):
            return True
        if isinstance(stmt, c_ast.Compound):
            return not stmt.block_items
        return False

    @staticmethod
    def _unwrap_nested_if(stmt):
        if isinstance(stmt, c_ast.If):
            return stmt
        if isinstance(stmt, c_ast.Compound) and stmt.block_items and len(stmt.block_items) == 1:
            inner = stmt.block_items[0]
            if isinstance(inner, c_ast.If):
                return inner
        return None

    @staticmethod
    def _unwrap_single_stmt(stmt):
        if isinstance(stmt, c_ast.Compound) and stmt.block_items and len(stmt.block_items) == 1:
            return stmt.block_items[0]
        return stmt

    @staticmethod
    def _same_simple_expr(left, right):
        if left is None or right is None:
            return left is None and right is None
        if type(left) is not type(right):
            return False
        if isinstance(left, (c_ast.ID, c_ast.Constant)):
            return nodes_equal(left, right)
        if isinstance(left, c_ast.UnaryOp):
            return (
                left.op == right.op
                and is_side_effect_free(left)
                and is_side_effect_free(right)
                and _ControlFlowOptimizer._same_simple_expr(left.expr, right.expr)
            )
        if isinstance(left, c_ast.BinaryOp):
            return (
                left.op == right.op
                and is_side_effect_free(left)
                and is_side_effect_free(right)
                and _ControlFlowOptimizer._same_simple_expr(left.left, right.left)
                and _ControlFlowOptimizer._same_simple_expr(left.right, right.right)
            )
        return False

    @staticmethod
    def _same_simple_stmt(left, right):
        left = _ControlFlowOptimizer._unwrap_single_stmt(left)
        right = _ControlFlowOptimizer._unwrap_single_stmt(right)
        if type(left) is not type(right):
            return False
        if isinstance(left, c_ast.Assignment):
            return (
                left.op == right.op
                and left.op == "="
                and _ControlFlowOptimizer._same_simple_expr(left.lvalue, right.lvalue)
                and _ControlFlowOptimizer._same_simple_expr(left.rvalue, right.rvalue)
            )
        if isinstance(left, c_ast.Return):
            return _ControlFlowOptimizer._same_simple_expr(left.expr, right.expr)
        if isinstance(left, c_ast.EmptyStatement):
            return True
        return False


class ControlFlowPass(ASTPass):
    """Control flow optimization — combines passes 13-18."""

    name = "control-flow"

    def run(self, ast, ctx: PassContext):
        for _ in range(3):
            opt = _ControlFlowOptimizer(ctx)
            ast = opt.visit(ast)
            if not opt.changed:
                break
        return ast
