"""Passes 9-12: Dead Code Elimination.

  9.  Dead Code Elimination     — remove unused variable decls & computations
  10. Unreachable Code Elim     — remove statements after return/break/continue/goto
  11. Dead Store Elimination    — remove stores to variables never read afterwards
  12. Dead Branch Elimination   — if(0){...} → remove, if(1){A}else{B} → A

Combines all four in a single AST walk + analysis.
"""

from __future__ import annotations

from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.passes.ast_utils import ASTTransformer, collect_ids, get_int_value, has_unstructured_control_flow, is_side_effect_free
from pcc.frontends.c.passes.base import ASTPass
from pcc.frontends.c.passes.context import PassContext


class _DeadCodeEliminator(ASTTransformer):
    """Bottom-up dead code elimination."""

    _SAFE_DEAD_STORE_UNARY_OPS = {"+", "-", "!", "~"}

    def __init__(self, ctx: PassContext):
        self.ctx = ctx
        self.changed = False
        self._current_func_name = None
        self._compound_depth = 0

    def _mark(self, action, node):
        self.changed = True
        coord = getattr(node, "coord", None)
        self.ctx.record("dce", action, f"{coord}" if coord else "?")
        self.ctx.bump(f"dce.{action}")

    # ── 10. Unreachable Code Elimination ────────────────────────────────

    def visit_Compound(self, node):
        self._compound_depth += 1
        self._visit_children(node)
        if not node.block_items:
            self._compound_depth -= 1
            return node

        new_items = []
        terminated = False
        for item in node.block_items:
            if terminated:
                self._mark("unreachable_after_terminator", item)
                self.changed = True
                continue
            new_items.append(item)
            if isinstance(item, (c_ast.Return, c_ast.Break, c_ast.Continue, c_ast.Goto)):
                terminated = True

        if self._compound_depth == 1:
            new_items = self._remove_dead_stores_within_block(new_items)

        if len(new_items) != len(node.block_items):
            node.block_items = new_items

        self._compound_depth -= 1
        return node

    # ── 12. Dead Branch Elimination ─────────────────────────────────────

    def visit_If(self, node):
        self._visit_children(node)

        cv = get_int_value(node.cond)
        if cv is not None:
            if cv != 0:
                # if(1) { A } else { B } → A
                self._mark("dead_branch_true", node)
                return node.iftrue if node.iftrue else c_ast.EmptyStatement(coord=node.coord)
            else:
                # if(0) { A } else { B } → B
                self._mark("dead_branch_false", node)
                if node.iffalse:
                    return node.iffalse
                return None  # remove entirely (only valid in list context)

        # Empty if body
        if node.iftrue is None or (
            isinstance(node.iftrue, c_ast.Compound)
            and not node.iftrue.block_items
        ):
            if node.iffalse is None or (
                isinstance(node.iffalse, c_ast.Compound)
                and not node.iffalse.block_items
            ):
                # if(cond) {} else {} — only keep if cond has side effects
                if is_side_effect_free(node.cond):
                    self._mark("dead_empty_if", node)
                    return None

        return node

    # ── Dead while(0) / for(;0;) ────────────────────────────────────────

    def visit_While(self, node):
        self._visit_children(node)
        cv = get_int_value(node.cond)
        if cv == 0:
            self._mark("dead_while_false", node)
            return None
        return node

    def visit_For(self, node):
        self._visit_children(node)
        if node.cond is not None:
            cv = get_int_value(node.cond)
            if cv == 0:
                # for(init; 0; ...) — keep init (may have side effects), drop rest
                self._mark("dead_for_false", node)
                if node.init is not None:
                    return node.init
                return None
        return node

    # ── 9 & 11. Dead Variable / Dead Store ──────────────────────────────

    def visit_FuncDef(self, node):
        """After visiting the body, remove unused local declarations."""
        if has_unstructured_control_flow(node):
            func_name = getattr(getattr(node, "decl", None), "name", "<anon>")
            self.ctx.record(
                "dce", "skip_function", func_name, "unstructured control flow",
            )
            self.ctx.bump("dce.skipped_functions")
            return node

        self._current_func_name = getattr(getattr(node, "decl", None), "name", None)
        self._visit_children(node)

        if not isinstance(node.body, c_ast.Compound) or not node.body.block_items:
            self._current_func_name = None
            return node

        # Iteratively remove dead variables
        for _ in range(5):
            removed = self._remove_dead_locals(node.body)
            if not removed:
                break

        self._current_func_name = None
        return node

    def _remove_dead_stores_within_block(self, items):
        if not items:
            return items

        live_names: set[str] = set()
        kept: list[c_ast.Node] = []

        for item in reversed(items):
            if self._can_remove_dead_store(item, live_names):
                self._mark("dead_store", item)
                continue

            self._update_live_names(item, live_names)
            kept.append(item)

        kept.reverse()
        return kept

    def _can_remove_dead_store(self, item, live_names: set[str]) -> bool:
        if not isinstance(item, c_ast.Assignment) or item.op != "=":
            return False
        if not isinstance(item.lvalue, c_ast.ID):
            return False
        var_info = self._local_var_info(item.lvalue.name)
        if var_info is None:
            return False
        if item.lvalue.name in live_names:
            return False
        if var_info.escapes or var_info.address_taken or var_info.passed_to_call:
            return False
        return self._is_dead_store_safe_expr(item.rvalue)

    def _update_live_names(self, item, live_names: set[str]):
        if isinstance(item, c_ast.Assignment) and item.op == "=" and isinstance(item.lvalue, c_ast.ID):
            live_names.discard(item.lvalue.name)
            live_names.update(collect_ids(item.rvalue))
            return
        if isinstance(item, c_ast.Decl) and item.name:
            live_names.discard(item.name)
            if item.init is not None:
                live_names.update(collect_ids(item.init))
            return
        live_names.update(collect_ids(item))

    def _local_var_info(self, var_name: str):
        if not self._current_func_name:
            return None
        func_info = self.ctx.functions.get(self._current_func_name)
        if func_info is None:
            return None
        var_info = func_info.var_infos.get(var_name)
        if var_info is None:
            return None
        if not (var_info.is_param or var_info.type_name):
            return None
        return var_info

    def _type_name_for_id(self, var_name: str) -> str:
        var_info = self._local_var_info(var_name)
        if var_info is None:
            return ""
        return var_info.type_name or ""

    @staticmethod
    def _is_scalar_type_name(type_name: str) -> bool:
        if not type_name:
            return False
        normalized = type_name.strip()
        if normalized.startswith(("struct ", "union ")):
            return False
        if normalized.endswith("[]"):
            return False
        return True

    def _is_dead_store_safe_expr(self, node) -> bool:
        if isinstance(node, c_ast.Constant):
            return True
        if isinstance(node, c_ast.ID):
            return self._is_scalar_type_name(self._type_name_for_id(node.name))
        if isinstance(node, c_ast.BinaryOp):
            return (
                is_side_effect_free(node)
                and self._is_dead_store_safe_expr(node.left)
                and self._is_dead_store_safe_expr(node.right)
            )
        if isinstance(node, c_ast.UnaryOp):
            return (
                node.op in self._SAFE_DEAD_STORE_UNARY_OPS
                and self._is_dead_store_safe_expr(node.expr)
            )
        if isinstance(node, c_ast.TernaryOp):
            return (
                is_side_effect_free(node)
                and self._is_dead_store_safe_expr(node.cond)
                and self._is_dead_store_safe_expr(node.iftrue)
                and self._is_dead_store_safe_expr(node.iffalse)
            )
        return False

    def _remove_dead_locals(self, compound: c_ast.Compound) -> bool:
        """Remove Decl nodes for variables that are never used in the body."""
        if not compound.block_items:
            return False

        # Collect all declared local names and all used names
        declared = {}  # name → index in block_items
        for i, item in enumerate(compound.block_items):
            if isinstance(item, c_ast.Decl) and item.name:
                # Don't remove function declarations or extern/static
                if isinstance(item.type, c_ast.FuncDecl):
                    continue
                if item.storage and any(
                    s in item.storage for s in ("extern", "static")
                ):
                    continue
                declared[item.name] = i

        if not declared:
            return False

        # Collect all ID references in the body (excluding the decl itself)
        used_names: set[str] = set()
        for i, item in enumerate(compound.block_items):
            if isinstance(item, c_ast.Decl) and item.name in declared:
                # Only count IDs in the initializer as uses of OTHER variables
                if item.init:
                    used_names.update(collect_ids(item.init))
            else:
                used_names.update(collect_ids(item))

        # Remove declarations of unused variables (if init is side-effect free)
        removed = False
        new_items = []
        for i, item in enumerate(compound.block_items):
            if (
                isinstance(item, c_ast.Decl)
                and item.name in declared
                and item.name not in used_names
            ):
                # Only remove if initializer has no side effects
                if item.init is None or is_side_effect_free(item.init):
                    self._mark("dead_variable", item)
                    removed = True
                    continue
            new_items.append(item)

        compound.block_items = new_items
        return removed


class DCEPass(ASTPass):
    """Dead Code Elimination pass — combines passes 9-12."""

    name = "dce"

    MAX_ITERATIONS = 5

    def run(self, ast, ctx: PassContext):
        for i in range(self.MAX_ITERATIONS):
            elim = _DeadCodeEliminator(ctx)
            ast = elim.visit(ast)
            if not elim.changed:
                ctx.record(self.name, "fixpoint", f"iter={i+1}")
                break
        return ast
