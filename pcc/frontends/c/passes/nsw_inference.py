"""HighTier Pass 3: NSW/NUW Flag Inference.

Analyzes arithmetic operations to determine where overflow flags can be
safely added to LLVM IR instructions:

  nsw (no signed wrap):   proven that signed overflow cannot occur
  nuw (no unsigned wrap): proven that unsigned overflow cannot occur

Strategy (conservative — only flag what we can prove):
  1. Variables with known range (e.g., loop counters with bounded iterations)
  2. Unsigned operations (nuw is always safe for add/sub if result >= 0)
  3. Small-width operations promoted to larger types (e.g., char + char as int)

Inspired by Graal's Range Analysis + Canonicalization passes.
"""

from __future__ import annotations

from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.passes.base import ASTPass
from pcc.frontends.c.passes.context import OverflowFlag, PassContext


class NSWInferencePass(ASTPass):
    name = "nsw-inference"

    def run(self, ast, ctx: PassContext):
        if not isinstance(ast, c_ast.FileAST):
            return None

        for ext in ast.ext or []:
            if isinstance(ext, c_ast.FuncDef):
                self._analyze_function(ext, ctx)

        return None

    def _analyze_function(self, funcdef: c_ast.FuncDef, ctx: PassContext):
        func_name = funcdef.decl.name
        func_info = ctx.get_func(func_name)

        # Infer ranges for variables with simple patterns
        for var_info in func_info.var_infos.values():
            self._infer_variable_range(var_info, funcdef, ctx)

        # Analyze for-loop induction variables
        if funcdef.body:
            self._analyze_loops(funcdef.body, func_name, ctx)

    def _infer_variable_range(self, var_info, funcdef, ctx):
        """Infer value range for simple patterns."""
        # Unsigned types: range is [0, 2^width - 1]
        if var_info.is_unsigned and var_info.bit_width > 0:
            var_info.range_min = 0
            var_info.range_max = (1 << var_info.bit_width) - 1
            ctx.record(
                self.name, "range_from_type",
                f"{var_info.func_name}::{var_info.name}",
                f"[{var_info.range_min}, {var_info.range_max}]",
            )

    def _analyze_loops(self, node, func_name, ctx):
        """Find for-loop patterns and infer induction variable ranges."""
        if node is None:
            return

        if isinstance(node, c_ast.For):
            self._analyze_for_loop(node, func_name, ctx)

        for _, child in node.children():
            if not isinstance(child, c_ast.FuncDef):
                self._analyze_loops(child, func_name, ctx)

    def _analyze_for_loop(self, for_node: c_ast.For, func_name, ctx):
        """Analyze: for (int i = LOW; i < HIGH; i++) pattern.

        If we can determine LOW and HIGH, the induction variable has a known
        range, and the increment operation is nsw-safe.
        """
        init_var, init_val = self._parse_for_init(for_node.init)
        if init_var is None:
            return

        cond_var, cond_op, cond_bound = self._parse_for_cond(for_node.cond)
        if cond_var != init_var:
            return

        next_var, next_delta = self._parse_for_next(for_node.next)
        if next_var != init_var:
            return

        # We have: for (init_var = init_val; init_var <op> bound; init_var += delta)
        var_info = ctx.get_var(func_name, init_var)

        if init_val is not None and cond_bound is not None:
            if cond_op in ("<", "<=") and next_delta is not None and next_delta > 0:
                var_info.range_min = init_val
                var_info.range_max = cond_bound
                ctx.record(
                    self.name, "loop_induction",
                    f"{func_name}::{init_var}",
                    f"range [{init_val}, {cond_bound}] step {next_delta}",
                )
                ctx.bump("nsw_inference.loop_vars_bounded")

        # Recurse into loop body
        self._analyze_loops(for_node.stmt, func_name, ctx)

    @staticmethod
    def _parse_for_init(init):
        """Parse 'int i = 0' or 'i = 0' from for-init."""
        if isinstance(init, c_ast.DeclList):
            for decl in init.decls or []:
                if isinstance(decl, c_ast.Decl) and decl.init:
                    if isinstance(decl.init, c_ast.Constant):
                        try:
                            return decl.name, int(decl.init.value, 0)
                        except (ValueError, TypeError):
                            pass
            return None, None

        if isinstance(init, c_ast.Assignment) and init.op == "=":
            if isinstance(init.lvalue, c_ast.ID) and isinstance(
                init.rvalue, c_ast.Constant
            ):
                try:
                    return init.lvalue.name, int(init.rvalue.value, 0)
                except (ValueError, TypeError):
                    pass

        return None, None

    @staticmethod
    def _parse_for_cond(cond):
        """Parse 'i < N' from for-condition."""
        if isinstance(cond, c_ast.BinaryOp) and cond.op in ("<", "<=", ">", ">="):
            if isinstance(cond.left, c_ast.ID):
                bound = None
                if isinstance(cond.right, c_ast.Constant):
                    try:
                        bound = int(cond.right.value, 0)
                    except (ValueError, TypeError):
                        pass
                return cond.left.name, cond.op, bound
        return None, None, None

    @staticmethod
    def _parse_for_next(next_expr):
        """Parse 'i++' or 'i += 1' from for-next."""
        if isinstance(next_expr, c_ast.UnaryOp):
            if next_expr.op in ("++", "p++") and isinstance(
                next_expr.expr, c_ast.ID
            ):
                return next_expr.expr.name, 1
            if next_expr.op in ("--", "p--") and isinstance(
                next_expr.expr, c_ast.ID
            ):
                return next_expr.expr.name, -1

        if isinstance(next_expr, c_ast.Assignment):
            if next_expr.op == "+=" and isinstance(next_expr.lvalue, c_ast.ID):
                if isinstance(next_expr.rvalue, c_ast.Constant):
                    try:
                        return next_expr.lvalue.name, int(next_expr.rvalue.value, 0)
                    except (ValueError, TypeError):
                        pass
            if next_expr.op == "-=" and isinstance(next_expr.lvalue, c_ast.ID):
                if isinstance(next_expr.rvalue, c_ast.Constant):
                    try:
                        return next_expr.lvalue.name, -int(next_expr.rvalue.value, 0)
                    except (ValueError, TypeError):
                        pass

        return None, None
