"""Passes 1-8: Canonicalization — Graal CanonicalizerPhase equivalent.

Combines in a single AST walk:
  1. Constant Folding         — 2+3 → 5
  2. Strength Reduction       — x*2 → x<<1, x/4 → x>>2
  3. Algebraic Simplification — x+0 → x, x*1 → x
  4. Identity Elimination     — x-x → 0, x^x → 0, x&x → x
  5. Absorbing Element        — x&0 → 0, x|~0 → ~0
  6. Double Negation          — -(-x) → x, ~(~x) → x
  7. Commutative Reordering   — const to RHS for pattern matching
  8. Associativity Exploit    — (a+C1)+C2 → a+(C1+C2)

Runs iteratively until fixpoint (like Graal).
"""

from __future__ import annotations

import math
import operator

from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.passes.ast_utils import ASTTransformer, has_type_sensitive_introspection, is_constant_float, get_safe_int_value, is_plain_decimal_int_constant, make_int_constant, make_float_constant, nodes_equal, is_side_effect_free
from pcc.frontends.c.passes.base import ASTPass
from pcc.frontends.c.passes.context import PassContext


_INT_OPS = {
    "+": operator.add,
    "-": operator.sub,
    "*": operator.mul,
    "/": lambda a, b: int(a / b) if b != 0 else None,
    "%": lambda a, b: a % b if b != 0 else None,
    "<<": lambda a, b: a << b if 0 <= b < 64 else None,
    ">>": lambda a, b: a >> b if 0 <= b < 64 else None,
    "&": operator.and_,
    "|": operator.or_,
    "^": operator.xor,
    "&&": lambda a, b: int(bool(a) and bool(b)),
    "||": lambda a, b: int(bool(a) or bool(b)),
    "<": lambda a, b: int(a < b),
    ">": lambda a, b: int(a > b),
    "<=": lambda a, b: int(a <= b),
    ">=": lambda a, b: int(a >= b),
    "==": lambda a, b: int(a == b),
    "!=": lambda a, b: int(a != b),
}

_FLOAT_OPS = {
    "+": operator.add,
    "-": operator.sub,
    "*": operator.mul,
    "/": lambda a, b: a / b if b != 0.0 else None,
}

# Powers of 2 for strength reduction
_POW2 = {1 << i: i for i in range(64)}


class _Canonicalizer(ASTTransformer):
    """Single-pass bottom-up canonicalization."""

    def __init__(self, ctx: PassContext):
        self.ctx = ctx
        self.changed = False

    def _mark(self, action, node):
        self.changed = True
        coord = getattr(node, "coord", None)
        loc = f"{coord}" if coord else "?"
        self.ctx.record("canonicalize", action, loc)
        self.ctx.bump(f"canonicalize.{action}")

    def visit_FuncDef(self, node):
        if has_type_sensitive_introspection(node):
            func_name = getattr(getattr(node, "decl", None), "name", "<anon>")
            self.ctx.record(
                "canonicalize", "skip_function", func_name,
                "type-sensitive introspection",
            )
            self.ctx.bump("canonicalize.skipped_functions")
            return node

        self._visit_children(node)
        return node

    def visit_StaticAssert(self, node):
        # Only the typed evaluator can prove an integer constant expression.
        # Untyped identities/folds can erase nonconstant operands or change
        # signedness before the assertion's semantic constraints are checked.
        return node

    # ── 1. Constant Folding ─────────────────────────────────────────────

    def _try_fold_binary_int(self, node):
        lv = get_safe_int_value(node.left)
        rv = get_safe_int_value(node.right)
        if lv is None or rv is None:
            return None
        fn = _INT_OPS.get(node.op)
        if fn is None:
            return None
        result = fn(lv, rv)
        if result is None:
            return None
        # Clamp to 64-bit signed range for sanity
        if isinstance(result, int) and (result > 2**63 - 1 or result < -(2**63)):
            return None
        self._mark("const_fold", node)
        return make_int_constant(result, coord=node.coord)

    def _try_fold_binary_float(self, node):
        if not (is_constant_float(node.left) and is_constant_float(node.right)):
            return None
        try:
            lv = float(node.left.value)
            rv = float(node.right.value)
        except (ValueError, TypeError):
            return None
        fn = _FLOAT_OPS.get(node.op)
        if fn is None:
            return None
        result = fn(lv, rv)
        if result is None or math.isnan(result) or math.isinf(result):
            return None
        self._mark("const_fold_float", node)
        return make_float_constant(result, coord=node.coord)

    # ── 2. Strength Reduction ───────────────────────────────────────────

    def _try_strength_reduce(self, node):
        # Strength reduction from arithmetic to shifts is not type-safe at the
        # AST level. Rewriting `2 * x` to `x << 1` breaks floating-point code
        # and can also change integer semantics before signedness is known.
        return None

    # ── 3. Algebraic Simplification ─────────────────────────────────────

    def _try_algebraic(self, node):
        lv = get_safe_int_value(node.left)
        rv = get_safe_int_value(node.right)

        # x + 0 → x, 0 + x → x
        if node.op == "+":
            if rv == 0:
                self._mark("alg_add_zero", node)
                return node.left
            if lv == 0:
                self._mark("alg_add_zero", node)
                return node.right

        # x - 0 → x
        if node.op == "-" and rv == 0:
            self._mark("alg_sub_zero", node)
            return node.left

        # x * 1 → x, 1 * x → x
        if node.op == "*":
            if rv == 1:
                self._mark("alg_mul_one", node)
                return node.left
            if lv == 1:
                self._mark("alg_mul_one", node)
                return node.right

        # x * 0 → 0, 0 * x → 0 (only if side-effect free)
        if node.op == "*":
            if rv == 0 and is_side_effect_free(node.left):
                self._mark("alg_mul_zero", node)
                return make_int_constant(0, node.coord)
            if lv == 0 and is_side_effect_free(node.right):
                self._mark("alg_mul_zero", node)
                return make_int_constant(0, node.coord)

        # x / 1 → x
        if node.op == "/" and rv == 1:
            self._mark("alg_div_one", node)
            return node.left

        # x % 1 → 0 (if side-effect free)
        if node.op == "%" and rv == 1 and is_side_effect_free(node.left):
            self._mark("alg_mod_one", node)
            return make_int_constant(0, node.coord)

        # x << 0 → x, x >> 0 → x
        if node.op in ("<<", ">>") and rv == 0:
            self._mark("alg_shift_zero", node)
            return node.left

        # x | 0 → x, 0 | x → x
        if node.op == "|":
            if rv == 0:
                self._mark("alg_or_zero", node)
                return node.left
            if lv == 0:
                self._mark("alg_or_zero", node)
                return node.right

        # x & ~0 → x (~0 = -1 in two's complement)
        if node.op == "&":
            if rv == -1:
                self._mark("alg_and_allones", node)
                return node.left
            if lv == -1:
                self._mark("alg_and_allones", node)
                return node.right

        # x ^ 0 → x, 0 ^ x → x
        if node.op == "^":
            if rv == 0:
                self._mark("alg_xor_zero", node)
                return node.left
            if lv == 0:
                self._mark("alg_xor_zero", node)
                return node.right

        return None

    # ── 4. Identity Elimination ─────────────────────────────────────────

    def _try_identity(self, node):
        if not nodes_equal(node.left, node.right):
            return None

        # `x - x` is not type-safe at the AST level.
        # For floating-point values it breaks valid IEEE cases such as
        # `inf - inf`, which must evaluate to NaN instead of 0.

        # x ^ x → 0
        if node.op == "^" and is_side_effect_free(node.left):
            self._mark("identity_xor_self", node)
            return make_int_constant(0, node.coord)

        # x & x → x
        if node.op == "&":
            self._mark("identity_and_self", node)
            return node.left

        # x | x → x
        if node.op == "|":
            self._mark("identity_or_self", node)
            return node.left

        # Self-comparison folds are not type-safe at the AST level.
        # They break valid floating-point cases such as NaN != NaN.

        return None

    # ── 5. Absorbing Element ────────────────────────────────────────────

    def _try_absorbing(self, node):
        rv = get_safe_int_value(node.right)
        lv = get_safe_int_value(node.left)

        # x & 0 → 0
        if node.op == "&":
            if (rv == 0 and is_side_effect_free(node.left)):
                self._mark("absorb_and_zero", node)
                return make_int_constant(0, node.coord)
            if (lv == 0 and is_side_effect_free(node.right)):
                self._mark("absorb_and_zero", node)
                return make_int_constant(0, node.coord)

        # x || 1 → 1 (short-circuit, but if lhs is side-effect free)
        if node.op == "||":
            if rv and rv != 0 and is_side_effect_free(node.left):
                self._mark("absorb_or_true", node)
                return make_int_constant(1, node.coord)
            if lv and lv != 0 and is_side_effect_free(node.right):
                self._mark("absorb_or_true", node)
                return make_int_constant(1, node.coord)

        # x && 0 → 0
        if node.op == "&&":
            if rv == 0 and is_side_effect_free(node.left):
                self._mark("absorb_and_false", node)
                return make_int_constant(0, node.coord)
            if lv == 0 and is_side_effect_free(node.right):
                self._mark("absorb_and_false", node)
                return make_int_constant(0, node.coord)

        return None

    # ── 7. Commutative Reordering ───────────────────────────────────────

    def _try_commutative_reorder(self, node):
        """Move constants to RHS for commutative ops — enables other patterns."""
        if node.op in ("+", "*", "&", "|", "^", "==", "!="):
            if is_plain_decimal_int_constant(node.left) and not is_plain_decimal_int_constant(node.right):
                self._mark("commutative_reorder", node)
                node.left, node.right = node.right, node.left
                return node
        return None

    # ── 8. Associativity Exploit ────────────────────────────────────────

    def _try_associativity(self, node):
        """(a + C1) + C2 → a + (C1 + C2), etc."""
        if node.op not in ("+", "*", "&", "|", "^"):
            return None
        rv = get_safe_int_value(node.right)
        if rv is None:
            return None
        if not isinstance(node.left, c_ast.BinaryOp) or node.left.op != node.op:
            return None
        inner_rv = get_safe_int_value(node.left.right)
        if inner_rv is None:
            return None
        fn = _INT_OPS.get(node.op)
        if fn is None:
            return None
        combined = fn(inner_rv, rv)
        if combined is None:
            return None
        self._mark("associativity", node)
        return c_ast.BinaryOp(
            node.op,
            node.left.left,
            make_int_constant(combined, node.coord),
            coord=node.coord,
        )

    # ── 6. Double Negation ──────────────────────────────────────────────

    def visit_UnaryOp(self, node):
        self._visit_children(node)

        # -(-x) → x
        if node.op == "-":
            if isinstance(node.expr, c_ast.UnaryOp) and node.expr.op == "-":
                self._mark("double_neg", node)
                return node.expr.expr

        # ~(~x) → x
        if node.op == "~":
            if isinstance(node.expr, c_ast.UnaryOp) and node.expr.op == "~":
                self._mark("double_bitnot", node)
                return node.expr.expr

        # Fold logical not on constant: !0 → 1, !5 → 0
        if node.op == "!" and is_plain_decimal_int_constant(node.expr):
            v = get_safe_int_value(node.expr)
            if v is not None:
                self._mark("const_fold_unary", node)
                return make_int_constant(int(v == 0), node.coord)

        # Integer unary folds like `-C` and `~C` are not type-safe at the AST
        # level. Replacing them with a bare `Constant("int", "...")` loses the
        # original literal width/signedness, which later truncates large values
        # in globals and constant initializers.

        return node

    # ── Combined BinaryOp handler ───────────────────────────────────────

    def visit_BinaryOp(self, node):
        self._visit_children(node)

        # Try each canonicalization in order of specificity
        for fn in (
            self._try_fold_binary_int,
            self._try_fold_binary_float,
            self._try_identity,
            self._try_absorbing,
            self._try_algebraic,
            self._try_strength_reduce,
            self._try_associativity,
            self._try_commutative_reorder,
        ):
            result = fn(node)
            if result is not None:
                return result

        return node

    def visit_TernaryOp(self, node):
        self._visit_children(node)
        # The result has the common type of both arms even with a constant
        # condition. Keep that conversion for typed lowering (e.g. 1 ? -1 : 1U)
        # and fold only when it is the identity: two constants of one type.
        cv = get_safe_int_value(node.cond)
        if (
            cv is not None
            and isinstance(node.iftrue, c_ast.Constant)
            and isinstance(node.iffalse, c_ast.Constant)
            and node.iftrue.type == node.iffalse.type
        ):
            if cv != 0:
                self._mark("ternary_true", node)
                return node.iftrue
            self._mark("ternary_false", node)
            return node.iffalse
        return node

    # ── Cast: fold cast of constant ─────────────────────────────────────

    def visit_Cast(self, node):
        self._visit_children(node)
        if is_plain_decimal_int_constant(node.expr):
            # cast to same-ish type → just keep the constant
            # (conservative: only fold obvious cases)
            pass
        return node


class CanonicalizerPass(ASTPass):
    """Graal-style CanonicalizerPhase: runs to fixpoint."""

    name = "canonicalize"

    MAX_ITERATIONS = 10

    def run(self, ast, ctx: PassContext):
        for i in range(self.MAX_ITERATIONS):
            transformer = _Canonicalizer(ctx)
            ast = transformer.visit(ast)
            if not transformer.changed:
                ctx.record(self.name, "fixpoint", f"iter={i+1}")
                break
        else:
            ctx.record(self.name, "max_iterations", f"{self.MAX_ITERATIONS}")
        return ast
