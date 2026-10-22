"""Passes 25-32: Loop Optimization.

  25. Loop Invariant Code Motion (LICM)  — move invariant computations out
  26. Loop Peeling                       — peel first iteration
  27. Loop Unswitching                   — hoist invariant conditions
  28. Loop Full Unroll                   — unroll small constant loops
  29. Loop Partial Unroll                — unroll N times
  30. Loop Rotation                      — while → do-while
  31. Induction Variable Simplification  — simplify IV expressions
  32. Loop Strength Reduction            — i*4 → inc by 4

AST-level analysis for codegen hints + simple transforms.
Complex loop transforms (unrolling, rotation) are better done by LLVM.
"""

from __future__ import annotations

from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.passes.ast_utils import ASTTransformer, collect_ids, get_int_value, is_constant_int, is_side_effect_free, make_int_constant
from pcc.frontends.c.passes.base import ASTPass
from pcc.frontends.c.passes.context import PassContext


class _LoopAnalyzer(ASTTransformer):
    """Analyze loops for optimization opportunities."""

    def __init__(self, ctx: PassContext):
        self.ctx = ctx
        self.changed = False
        self._current_func = None

    def _mark(self, action, node):
        self.changed = True
        coord = getattr(node, "coord", None)
        self.ctx.record("loop_opt", action, f"{coord}" if coord else "?")
        self.ctx.bump(f"loop_opt.{action}")

    def visit_FuncDef(self, node):
        self._current_func = node.decl.name
        self._visit_children(node)
        self._current_func = None
        return node

    # ── 25. LICM (Loop Invariant Code Motion) ──────────────────────────

    def visit_For(self, node):
        self._visit_children(node)
        self._analyze_licm(node)
        self._analyze_loop_idiom(node)
        self._analyze_loop_sink(node)
        self._analyze_unroll(node)
        self._analyze_unswitching(node)
        self._analyze_strength_reduction(node)
        return node

    def visit_While(self, node):
        self._visit_children(node)
        return node

    def _analyze_licm(self, for_node: c_ast.For):
        """Identify loop-invariant expressions in the body.

        An expression is loop-invariant if it doesn't reference any
        variable that's modified within the loop body.
        """
        # Collect variables modified in the loop
        modified_in_loop = self._collect_modified_vars(for_node.stmt)
        if for_node.next:
            modified_in_loop.update(self._collect_modified_vars(for_node.next))

        # Check each statement in loop body for invariance
        if isinstance(for_node.stmt, c_ast.Compound) and for_node.stmt.block_items:
            invariant_count = 0
            for item in for_node.stmt.block_items:
                if isinstance(item, c_ast.Decl) and item.name and item.init:
                    used = collect_ids(item.init)
                    if not used.intersection(modified_in_loop):
                        if is_side_effect_free(item.init):
                            invariant_count += 1
                            self.ctx.record(
                                "loop_opt", "licm_candidate",
                                f"{self._current_func}::{item.name}",
                            )
            if invariant_count > 0:
                self.ctx.bump("loop_opt.licm_candidates", invariant_count)

    # ── 27. Loop Unswitching ───────────────────────────────────────────

    def _analyze_unswitching(self, for_node: c_ast.For):
        """Find if-conditions inside loops that are loop-invariant."""
        modified = self._collect_modified_vars(for_node.stmt)

        if isinstance(for_node.stmt, c_ast.Compound) and for_node.stmt.block_items:
            for item in for_node.stmt.block_items:
                if isinstance(item, c_ast.If):
                    cond_ids = collect_ids(item.cond)
                    if not cond_ids.intersection(modified):
                        self.ctx.record(
                            "loop_opt", "unswitch_candidate",
                            f"{self._current_func}",
                        )
                        self.ctx.bump("loop_opt.unswitch_candidates")

    # ── 28. Loop Full Unroll ───────────────────────────────────────────

    def _analyze_unroll(self, for_node: c_ast.For):
        """Detect small constant-trip-count loops eligible for full unroll."""
        trip = self._compute_trip_count(for_node)
        if trip is not None and 0 < trip <= 16:
            self.ctx.record(
                "loop_opt", "full_unroll_candidate",
                f"{self._current_func}", f"trip_count={trip}",
            )
            self.ctx.bump("loop_opt.unroll_candidates")

    def _compute_trip_count(self, for_node: c_ast.For) -> int | None:
        """Compute trip count for: for(i=A; i<B; i+=C) patterns."""
        # Parse init
        init_var, init_val = self._parse_init(for_node.init)
        if init_var is None or init_val is None:
            return None

        # Parse condition
        if not isinstance(for_node.cond, c_ast.BinaryOp):
            return None
        if not (isinstance(for_node.cond.left, c_ast.ID)
                and for_node.cond.left.name == init_var):
            return None
        bound = get_int_value(for_node.cond.right)
        if bound is None:
            return None

        # Parse step
        step = self._parse_step(for_node.next, init_var)
        if step is None or step == 0:
            return None

        op = for_node.cond.op
        if op == "<" and step > 0:
            return max(0, (bound - init_val + step - 1) // step)
        if op == "<=" and step > 0:
            return max(0, (bound - init_val + step) // step)
        if op == ">" and step < 0:
            return max(0, (init_val - bound - step - 1) // (-step))
        if op == ">=" and step < 0:
            return max(0, (init_val - bound - step) // (-step))

        return None

    # ── 32. Loop Strength Reduction ────────────────────────────────────

    def _analyze_strength_reduction(self, for_node: c_ast.For):
        """Detect i*const patterns in loop body that could be incremented."""
        init_var, _ = self._parse_init(for_node.init)
        if init_var is None:
            return

        if isinstance(for_node.stmt, c_ast.Compound) and for_node.stmt.block_items:
            for item in for_node.stmt.block_items:
                self._find_iv_multiply(item, init_var)

    def _find_iv_multiply(self, node, iv_name: str):
        """Find patterns like arr[i * stride] or expressions involving i * const."""
        if node is None:
            return
        if isinstance(node, c_ast.BinaryOp) and node.op == "*":
            if isinstance(node.left, c_ast.ID) and node.left.name == iv_name:
                if is_constant_int(node.right):
                    self.ctx.record(
                        "loop_opt", "strength_reduction_candidate",
                        f"iv={iv_name}",
                    )
                    self.ctx.bump("loop_opt.strength_reduction_candidates")
                    return
            if isinstance(node.right, c_ast.ID) and node.right.name == iv_name:
                if is_constant_int(node.left):
                    self.ctx.record(
                        "loop_opt", "strength_reduction_candidate",
                        f"iv={iv_name}",
                    )
                    self.ctx.bump("loop_opt.strength_reduction_candidates")
                    return
        for _, child in node.children():
            if isinstance(child, c_ast.Node):
                self._find_iv_multiply(child, iv_name)

    # ── Loop Idiom Recognition (analysis only) ────────────────────────

    def _analyze_loop_idiom(self, for_node: c_ast.For):
        """Record very small source-level loop idiom candidates.

        We intentionally keep this conservative and analysis-only:
        - zero-fill loops like `p[i] = 0`
        - element copy loops like `dst[i] = src[i]`
        """
        iv_name, _ = self._parse_init(for_node.init)
        if iv_name is None:
            return

        body_stmt = self._single_body_statement(for_node.stmt)
        if not isinstance(body_stmt, c_ast.Assignment) or body_stmt.op != "=":
            return

        dst_base = self._array_ref_base_with_iv_index(body_stmt.lvalue, iv_name)
        if dst_base is None:
            return

        zero_value = get_int_value(body_stmt.rvalue)
        if zero_value == 0:
            self.ctx.record(
                "loop_opt", "memset_idiom_candidate",
                f"{self._current_func}::{dst_base}",
            )
            self.ctx.bump("loop_opt.memset_idiom_candidates")
            return

        src_base = self._array_ref_base_with_iv_index(body_stmt.rvalue, iv_name)
        if src_base is None or src_base == dst_base:
            return

        self.ctx.record(
            "loop_opt", "memcpy_idiom_candidate",
            f"{self._current_func}::{dst_base}",
            f"src={src_base}",
        )
        self.ctx.bump("loop_opt.memcpy_idiom_candidates")

    def _analyze_loop_sink(self, for_node: c_ast.For):
        """Record guarded loop-local values that could be sunk into one branch."""
        if not isinstance(for_node.stmt, c_ast.Compound) or not for_node.stmt.block_items:
            return

        items = for_node.stmt.block_items
        for idx in range(len(items) - 1):
            var_name, expr = self._loop_local_value_def(items[idx])
            if var_name is None or expr is None or not is_side_effect_free(expr):
                continue

            guard = items[idx + 1]
            if not isinstance(guard, c_ast.If):
                continue
            if var_name in collect_ids(guard.cond):
                continue

            true_uses = var_name in collect_ids(guard.iftrue)
            false_uses = var_name in collect_ids(guard.iffalse)
            if true_uses == false_uses:
                continue

            if any(var_name in collect_ids(item) for item in items[idx + 2 :]):
                continue

            branch_name = "iftrue" if true_uses else "iffalse"
            self.ctx.record(
                "loop_opt", "sink_candidate",
                f"{self._current_func}::{var_name}",
                branch_name,
            )
            self.ctx.bump("loop_opt.sink_candidates")

    # ── 31. Induction Variable Simplification ──────────────────────────
    # (Analysis only — recorded in PassContext for codegen)

    # ── 30. Loop Rotation (while → do-while) ──────────────────────────
    # (Better done at LLVM level — LLVM's LoopRotate pass handles this)

    # ── 26. Loop Peeling ──────────────────────────────────────────────
    # (Better done at LLVM level)

    # ── 29. Loop Partial Unroll ───────────────────────────────────────
    # (Better done at LLVM level)

    # ── Helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _parse_init(init):
        if isinstance(init, c_ast.DeclList):
            for d in init.decls or []:
                if isinstance(d, c_ast.Decl) and d.init and d.name:
                    v = get_int_value(d.init)
                    if v is not None:
                        return d.name, v
        if isinstance(init, c_ast.Assignment) and init.op == "=":
            if isinstance(init.lvalue, c_ast.ID):
                v = get_int_value(init.rvalue)
                if v is not None:
                    return init.lvalue.name, v
        return None, None

    @staticmethod
    def _parse_step(next_expr, var_name) -> int | None:
        if isinstance(next_expr, c_ast.UnaryOp):
            if isinstance(next_expr.expr, c_ast.ID) and next_expr.expr.name == var_name:
                if next_expr.op in ("++", "p++"):
                    return 1
                if next_expr.op in ("--", "p--"):
                    return -1
        if isinstance(next_expr, c_ast.Assignment):
            if isinstance(next_expr.lvalue, c_ast.ID) and next_expr.lvalue.name == var_name:
                if next_expr.op == "+=" and is_constant_int(next_expr.rvalue):
                    return get_int_value(next_expr.rvalue)
                if next_expr.op == "-=" and is_constant_int(next_expr.rvalue):
                    v = get_int_value(next_expr.rvalue)
                    return -v if v is not None else None
        return None

    @staticmethod
    def _collect_modified_vars(node) -> set[str]:
        """Collect all variable names that are modified (assigned/incremented)."""
        modified: set[str] = set()
        if node is None:
            return modified
        if isinstance(node, c_ast.Assignment):
            if isinstance(node.lvalue, c_ast.ID):
                modified.add(node.lvalue.name)
        if isinstance(node, c_ast.UnaryOp) and node.op in ("++", "--", "p++", "p--"):
            if isinstance(node.expr, c_ast.ID):
                modified.add(node.expr.name)
        if isinstance(node, c_ast.Decl) and node.name:
            modified.add(node.name)
        for _, child in node.children():
                if isinstance(child, c_ast.Node):
                    modified.update(_LoopAnalyzer._collect_modified_vars(child))
        return modified

    @staticmethod
    def _single_body_statement(stmt):
        if isinstance(stmt, c_ast.Compound):
            if not stmt.block_items or len(stmt.block_items) != 1:
                return None
            return stmt.block_items[0]
        return stmt

    @staticmethod
    def _array_ref_base_with_iv_index(node, iv_name: str) -> str | None:
        if not isinstance(node, c_ast.ArrayRef):
            return None
        if not isinstance(node.subscript, c_ast.ID) or node.subscript.name != iv_name:
            return None
        if isinstance(node.name, c_ast.ID):
            return node.name.name
        return None

    @staticmethod
    def _loop_local_value_def(stmt):
        if isinstance(stmt, c_ast.Decl) and stmt.name and stmt.init is not None:
            return stmt.name, stmt.init
        if isinstance(stmt, c_ast.Assignment) and stmt.op == "=":
            if isinstance(stmt.lvalue, c_ast.ID):
                return stmt.lvalue.name, stmt.rvalue
        return None, None


class LoopOptPass(ASTPass):
    """Loop optimization pass — combines passes 25-32.

    At AST level, primarily analysis + hints for LLVM.
    Complex transforms delegated to LLVM's loop optimization passes.
    """
    name = "loop-opt"

    def run(self, ast, ctx: PassContext):
        analyzer = _LoopAnalyzer(ctx)
        ast = analyzer.visit(ast)
        return ast if analyzer.changed else None
