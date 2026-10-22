"""Passes 33-37: Value Propagation & Numbering.

  33. Global Value Numbering (GVN)       — cross-block CSE
  34. Local Value Numbering              — within-block CSE
  35. Expression Reassociation           — reorder for const folding / LICM
  36. Scalar Replacement of Aggregates   — struct fields → scalars
  37. Copy Propagation                   — a=b; use(a) → use(b)

At AST level, we focus on the patterns most impactful for codegen quality.
"""

from __future__ import annotations

from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.passes.ast_utils import ASTTransformer, collect_ids, get_safe_int_value, has_type_sensitive_introspection, has_unstructured_control_flow, is_side_effect_free, make_int_constant
from pcc.frontends.c.passes.base import ASTPass
from pcc.frontends.c.passes.context import PassContext


class _CopyPropagator(ASTTransformer):
    """Pass 37: Copy Propagation.

    Tracks simple assignments: `type x = y;` or `x = y;` where y is a
    simple ID, and replaces subsequent uses of x with y (when safe).
    """

    def __init__(self, ctx: PassContext):
        self.ctx = ctx
        self.changed = False
        self._current_func_name = None

    def _mark(self, action, node):
        self.changed = True
        coord = getattr(node, "coord", None)
        self.ctx.record("copy_prop", action, f"{coord}" if coord else "?")
        self.ctx.bump(f"copy_prop.{action}")

    def visit_FuncDef(self, node):
        """Apply copy propagation within each function."""
        if has_unstructured_control_flow(node):
            func_name = getattr(getattr(node, "decl", None), "name", "<anon>")
            self.ctx.record(
                "copy_prop", "skip_function", func_name,
                "unstructured control flow",
            )
            self.ctx.bump("copy_prop.skipped_functions")
            return node

        self._current_func_name = getattr(getattr(node, "decl", None), "name", None)
        self._visit_children(node)
        if isinstance(node.body, c_ast.Compound) and node.body.block_items:
            self._propagate_copies(node.body)
        self._current_func_name = None
        return node

    def _type_name(self, var_name: str) -> str:
        if not self._current_func_name:
            return ""
        func_info = self.ctx.functions.get(self._current_func_name)
        if func_info is None:
            return ""
        var_info = func_info.var_infos.get(var_name)
        if var_info is None:
            return ""
        return var_info.type_name or ""

    @staticmethod
    def _normalize_copy_type_name(type_name: str) -> str:
        tokens = [
            token
            for token in type_name.replace("\t", " ").split()
            if token not in {"const", "volatile", "restrict", "__restrict", "register"}
        ]
        return " ".join(tokens)

    def _is_copy_propagatable_type(self, type_name: str) -> bool:
        normalized = self._normalize_copy_type_name(type_name)
        if not normalized:
            return False
        if any(ch in normalized for ch in "*[]()"):
            return False
        if normalized.startswith(("struct ", "union ", "enum ")):
            return False
        return normalized in {
            "_Bool",
            "bool",
            "char",
            "signed char",
            "unsigned char",
            "short",
            "short int",
            "signed short",
            "signed short int",
            "unsigned short",
            "unsigned short int",
            "int",
            "signed",
            "signed int",
            "unsigned",
            "unsigned int",
            "long",
            "long int",
            "signed long",
            "signed long int",
            "unsigned long",
            "unsigned long int",
            "long long",
            "long long int",
            "signed long long",
            "signed long long int",
            "unsigned long long",
            "unsigned long long int",
            "float",
            "double",
            "long double",
        }

    def _propagate_copies(self, compound: c_ast.Compound):
        """Within a compound, propagate simple copies forward."""
        if not compound.block_items:
            return

        # Build copy map: var_name → source_name
        # Only for: int x = y; (where y is a simple ID, x is never reassigned or &-taken)
        copies: dict[str, str] = {}

        # First pass: find eligible copies
        assigned_more_than_once: set[str] = set()
        addr_taken: set[str] = set()
        seen_defs: set[str] = set()

        for item in compound.block_items:
            self._collect_reassigned(item, assigned_more_than_once, seen_defs)
            self._collect_addr_taken(item, addr_taken)

        for item in compound.block_items:
            # Pattern: int x = y;
            if isinstance(item, c_ast.Decl) and item.name and isinstance(item.init, c_ast.ID):
                src = item.init.name
                tgt = item.name
                tgt_type = self._type_name(tgt)
                src_type = self._type_name(src)
                if (
                    tgt not in assigned_more_than_once
                    and tgt not in addr_taken
                    and src not in addr_taken
                    and src not in assigned_more_than_once
                    and tgt_type == src_type
                    and self._is_copy_propagatable_type(tgt_type)
                ):
                    copies[tgt] = src

        if not copies:
            return

        # Second pass: substitute
        replacer = _IDReplacer(copies, self.ctx)
        for i, item in enumerate(compound.block_items):
            if isinstance(item, c_ast.Decl) and item.name in copies:
                continue  # skip the copy declaration itself
            compound.block_items[i] = replacer.visit(item)

        if replacer.count > 0:
            self.changed = True

    @staticmethod
    def _collect_reassigned(node, assigned: set, seen_decls: set):
        """Collect names assigned more than once (including init + later assign)."""
        if node is None:
            return
        if isinstance(node, c_ast.Decl) and node.name:
            if node.init is not None:
                seen_decls.add(node.name)
        if isinstance(node, c_ast.Assignment):
            for name in _CopyPropagator._assigned_object_names(node.lvalue):
                if name in seen_decls:
                    assigned.add(name)
                seen_decls.add(name)
        if isinstance(node, c_ast.UnaryOp) and node.op in ("++", "--", "p++", "p--"):
            for name in _CopyPropagator._assigned_object_names(node.expr):
                if name in seen_decls:
                    assigned.add(name)
                seen_decls.add(name)
        for _, child in node.children():
            if isinstance(child, c_ast.Node):
                _CopyPropagator._collect_reassigned(child, assigned, seen_decls)

    @staticmethod
    def _assigned_object_names(node) -> set[str]:
        if node is None:
            return set()
        if isinstance(node, c_ast.ID):
            return {node.name}
        if isinstance(node, c_ast.StructRef):
            return _CopyPropagator._assigned_object_names(node.name)
        if isinstance(node, c_ast.ArrayRef):
            return _CopyPropagator._assigned_object_names(node.name)
        if isinstance(node, c_ast.Cast):
            return _CopyPropagator._assigned_object_names(node.expr)
        return set()

    @staticmethod
    def _collect_addr_taken(node, taken: set):
        if node is None:
            return
        if isinstance(node, c_ast.UnaryOp) and node.op == "&":
            if isinstance(node.expr, c_ast.ID):
                taken.add(node.expr.name)
        for _, child in node.children():
            if isinstance(child, c_ast.Node):
                _CopyPropagator._collect_addr_taken(child, taken)


class _IDReplacer(ASTTransformer):
    """Replace ID nodes according to a substitution map."""

    def __init__(self, subs: dict[str, str], ctx: PassContext):
        self.subs = subs
        self.ctx = ctx
        self.count = 0

    def visit_ID(self, node):
        if node.name in self.subs:
            self.count += 1
            self.ctx.bump("copy_prop.substituted")
            node.name = self.subs[node.name]
        return node

    def visit_Assignment(self, node):
        # Never rewrite the assignment target. Replacing `x = y` with `a = y`
        # mutates a different object and turns a safe read-only substitution
        # into a semantic change.
        if node.rvalue is not None:
            node.rvalue = self.visit(node.rvalue)
        return node

    def visit_UnaryOp(self, node):
        # Address-of and increment/decrement operate on object identity or
        # mutate storage, so substituting the operand is not semantics-preserving.
        if node.op in ("&", "++", "--", "p++", "p--"):
            return node
        return super().visit_UnaryOp(node)


class _ExpressionReassociator(ASTTransformer):
    """Pass 35: Expression Reassociation.

    Reorder commutative/associative operations to group constants together,
    enabling subsequent constant folding.

    Example: (a + 1) + 2 → a + (1 + 2) → a + 3  (after const fold)
    """

    def __init__(self, ctx: PassContext):
        self.ctx = ctx
        self.changed = False

    _COMMUTATIVE_OPS = {"+", "*", "&", "|", "^"}

    def _mark(self, action, node):
        self.changed = True
        self.ctx.record("reassociate", action, "?")
        self.ctx.bump(f"reassociate.{action}")

    def visit_FuncDef(self, node):
        if has_type_sensitive_introspection(node):
            func_name = getattr(getattr(node, "decl", None), "name", "<anon>")
            self.ctx.record(
                "reassociate",
                "skip_function",
                func_name,
                "type-sensitive introspection",
            )
            self.ctx.bump("reassociate.skipped_functions")
            return node

        self._visit_children(node)
        return node

    def _rank(self, node) -> int:
        if isinstance(node, c_ast.Constant):
            return 0
        if isinstance(node, c_ast.ID):
            return 1
        if isinstance(node, c_ast.BinaryOp):
            return 1 + max(self._rank(node.left), self._rank(node.right))
        if isinstance(node, c_ast.UnaryOp):
            return 1 + self._rank(node.expr)
        if isinstance(node, c_ast.Cast):
            return 1 + self._rank(node.expr)
        return 1

    def _flatten_operands(self, node, op: str, out: list):
        if (
            isinstance(node, c_ast.BinaryOp)
            and node.op == op
            and is_side_effect_free(node.left)
            and is_side_effect_free(node.right)
        ):
            self._flatten_operands(node.left, op, out)
            self._flatten_operands(node.right, op, out)
            return
        out.append(node)

    @staticmethod
    def _combine_constants(op: str, values: list[int]) -> int | None:
        if not values:
            return None
        result = values[0]
        for value in values[1:]:
            if op == "+":
                result += value
            elif op == "*":
                result *= value
            elif op == "&":
                result &= value
            elif op == "|":
                result |= value
            elif op == "^":
                result ^= value
            else:
                return None
        return result

    @staticmethod
    def _is_identity_constant(op: str, value: int) -> bool:
        if op == "+":
            return value == 0
        if op == "*":
            return value == 1
        if op in {"|", "^"}:
            return value == 0
        return False

    def _rebuild_tree(self, op: str, operands: list, coord):
        if not operands:
            return None
        current = operands[0]
        for operand in operands[1:]:
            current = c_ast.BinaryOp(op, current, operand, coord=coord)
        return current

    def visit_BinaryOp(self, node):
        self._visit_children(node)

        if node.op not in self._COMMUTATIVE_OPS:
            return node
        if not is_side_effect_free(node):
            return node

        original_operands: list = []
        self._flatten_operands(node, node.op, original_operands)
        if len(original_operands) < 2:
            return node

        constants = []
        non_constants = []
        for operand in original_operands:
            value = get_safe_int_value(operand)
            if value is not None:
                constants.append(value)
            else:
                non_constants.append(operand)

        combined_constant = self._combine_constants(node.op, constants)
        if len(non_constants) > 1:
            non_constants = sorted(non_constants, key=self._rank, reverse=True)

        rebuilt_operands = list(non_constants)
        if combined_constant is not None and not (
            rebuilt_operands and self._is_identity_constant(node.op, combined_constant)
        ):
            rebuilt_operands.append(make_int_constant(combined_constant, coord=node.coord))

        if not rebuilt_operands:
            rebuilt_operands = [make_int_constant(combined_constant or 0, coord=node.coord)]

        if len(rebuilt_operands) == 1:
            if (
                combined_constant is not None
                and (len(constants) > 1 or len(original_operands) > 1)
            ):
                self._mark("fold_constants", node)
                return rebuilt_operands[0]
            return node

        reordered = rebuilt_operands != original_operands
        combined = combined_constant is not None and len(constants) > 1
        if not reordered and not combined:
            return node

        if combined:
            self._mark("combine_constants", node)
        elif reordered:
            self._mark("reorder_operands", node)

        return self._rebuild_tree(node.op, rebuilt_operands, node.coord)

        return node


class _LocalValueNumbering(ASTTransformer):
    """Pass 34: Local Value Numbering (within a compound block).

    Identifies repeated computations and replaces them with a reference
    to the first computation's result variable.

    This is a simplified version — only handles exact expression matches
    within the same compound block scope.
    """

    _SAFE_UNARY_OPS = {"+", "-", "~", "!"}

    def __init__(self, ctx: PassContext):
        self.ctx = ctx
        self.changed = False
        self._current_func_name = None

    def visit_FuncDef(self, node):
        self._current_func_name = getattr(getattr(node, "decl", None), "name", None)
        self._visit_children(node)
        self._current_func_name = None
        return node

    def visit_Compound(self, node):
        self._visit_children(node)
        if node.block_items:
            self._number_values(node)
        return node

    def _number_values(self, compound: c_ast.Compound):
        """Track expressions assigned to variables; replace later identical ones."""
        if not compound.block_items:
            return

        expr_to_var: dict[str, tuple[str, set[str]]] = {}

        for item in compound.block_items:
            # A call or indirect store may change a cached result variable or
            # one of its operands through an alias. This AST pass has no
            # memory-effect proof, including for calls nested in conditions.
            if self._may_write_aliased_memory(item):
                expr_to_var.clear()
                continue
            target_name, expr, assign_back = self._extract_assignment_like(item)
            if expr is not None:
                key = self._expr_key(expr)
                if key and self._is_lvn_safe_expr(expr):
                    cached = expr_to_var.get(key)
                    if (
                        cached is not None
                        and cached[0] != target_name
                        and self._have_compatible_types(cached[0], target_name)
                    ):
                        assign_back(c_ast.ID(cached[0], coord=getattr(expr, "coord", None)))
                        self.changed = True
                        self.ctx.bump("lvn.reused_expression")
                        self.ctx.record(
                            "local-value-numbering",
                            "reuse",
                            cached[0],
                            key,
                        )
                        expr = self._extract_assignment_like(item)[1]

            for assigned_name in self._assigned_names(item):
                self._invalidate_bindings(expr_to_var, assigned_name)

            if target_name and expr is not None:
                key = self._expr_key(expr)
                if key and self._is_lvn_safe_expr(expr):
                    expr_to_var[key] = (target_name, collect_ids(expr))

    def _type_name(self, var_name: str) -> str:
        if not self._current_func_name:
            return ""
        func_info = self.ctx.functions.get(self._current_func_name)
        if func_info is None:
            return ""
        var_info = func_info.var_infos.get(var_name)
        if var_info is None:
            return ""
        return var_info.type_name or ""

    def _have_compatible_types(self, cached_var: str, target_var: str) -> bool:
        cached_type = self._type_name(cached_var)
        target_type = self._type_name(target_var)
        if not cached_type or not target_type:
            return False
        # An array initializer is not an ordinary C value expression.  In
        # particular, replacing a repeated string-literal initializer with an
        # ID changes ``char b[] = "x"`` into aggregate initialization from a
        # different array, which codegen cannot (and C does not) treat as a
        # scalar copy.  Keep aggregate initialization out of source-level LVN.
        if "[]" in cached_type or "[]" in target_type:
            return False
        return cached_type == target_type

    @staticmethod
    def _extract_assignment_like(item):
        if isinstance(item, c_ast.Decl) and item.name and item.init is not None:
            return item.name, item.init, lambda new_expr: setattr(item, "init", new_expr)
        if (
            isinstance(item, c_ast.Assignment)
            and item.op == "="
            and isinstance(item.lvalue, c_ast.ID)
        ):
            return (
                item.lvalue.name,
                item.rvalue,
                lambda new_expr: setattr(item, "rvalue", new_expr),
            )
        return None, None, lambda new_expr: None

    @staticmethod
    def _may_write_aliased_memory(node) -> bool:
        if node is None:
            return False
        if isinstance(node, c_ast.FuncCall):
            return True
        if isinstance(node, c_ast.Assignment) and not isinstance(node.lvalue, c_ast.ID):
            return True
        if (
            isinstance(node, c_ast.UnaryOp)
            and node.op in ("++", "--", "p++", "p--")
            and not isinstance(node.expr, c_ast.ID)
        ):
            return True
        return any(
            _LocalValueNumbering._may_write_aliased_memory(child)
            for _, child in node.children()
            if isinstance(child, c_ast.Node)
        )

    @staticmethod
    def _assigned_names(node) -> set[str]:
        if node is None:
            return set()
        names = set()
        if isinstance(node, c_ast.Decl) and node.name:
            names.add(node.name)
        if isinstance(node, c_ast.Assignment):
            names.update(_LocalValueNumbering._assigned_object_names(node.lvalue))
        if isinstance(node, c_ast.UnaryOp) and node.op in ("++", "--", "p++", "p--"):
            names.update(_LocalValueNumbering._assigned_object_names(node.expr))
        for _, child in node.children():
            if isinstance(child, c_ast.Node):
                names.update(_LocalValueNumbering._assigned_names(child))
        return names

    @staticmethod
    def _assigned_object_names(node) -> set[str]:
        if node is None:
            return set()
        if isinstance(node, c_ast.ID):
            return {node.name}
        if isinstance(node, c_ast.StructRef):
            return _LocalValueNumbering._assigned_object_names(node.name)
        if isinstance(node, c_ast.ArrayRef):
            return _LocalValueNumbering._assigned_object_names(node.name)
        if isinstance(node, c_ast.Cast):
            return _LocalValueNumbering._assigned_object_names(node.expr)
        return set()

    @staticmethod
    def _invalidate_bindings(expr_to_var, assigned_name: str):
        doomed = [
            key
            for key, (_var_name, deps) in expr_to_var.items()
            if assigned_name in deps or assigned_name == _var_name
        ]
        for key in doomed:
            expr_to_var.pop(key, None)

    @staticmethod
    def _expr_key(node) -> str | None:
        """Generate a hashable key for simple expressions."""
        if isinstance(node, c_ast.BinaryOp):
            lk = _LocalValueNumbering._expr_key(node.left)
            rk = _LocalValueNumbering._expr_key(node.right)
            if lk and rk:
                return f"({lk}{node.op}{rk})"
        if isinstance(node, c_ast.UnaryOp):
            if node.op not in _LocalValueNumbering._SAFE_UNARY_OPS:
                return None
            ek = _LocalValueNumbering._expr_key(node.expr)
            if ek:
                return f"({node.op}{ek})"
        if isinstance(node, c_ast.ID):
            return f"id:{node.name}"
        if isinstance(node, c_ast.Constant):
            return f"const:{node.type}:{node.value}"
        return None

    @staticmethod
    def _is_lvn_safe_expr(node) -> bool:
        if isinstance(node, (c_ast.ID, c_ast.Constant)):
            return True
        if isinstance(node, c_ast.BinaryOp):
            return (
                is_side_effect_free(node)
                and _LocalValueNumbering._is_lvn_safe_expr(node.left)
                and _LocalValueNumbering._is_lvn_safe_expr(node.right)
            )
        if isinstance(node, c_ast.UnaryOp):
            return (
                node.op in _LocalValueNumbering._SAFE_UNARY_OPS
                and _LocalValueNumbering._is_lvn_safe_expr(node.expr)
            )
        return False


class CopyPropagationPass(ASTPass):
    """Pass 37: Copy Propagation."""
    name = "copy-propagation"

    def run(self, ast, ctx: PassContext):
        prop = _CopyPropagator(ctx)
        ast = prop.visit(ast)
        return ast if prop.changed else None


class ExpressionReassociationPass(ASTPass):
    """Pass 35: Expression Reassociation."""
    name = "expr-reassociation"

    def run(self, ast, ctx: PassContext):
        if ctx.opt_level == 0:
            ctx.record(
                self.name,
                "skip_opt_level",
                "O0",
                "disabled for O0 frontend pipeline",
            )
            ctx.bump("reassociate.skipped_o0")
            return None
        ra = _ExpressionReassociator(ctx)
        ast = ra.visit(ast)
        return ast if ra.changed else None


class LocalValueNumberingPass(ASTPass):
    """Pass 34: Local Value Numbering."""
    name = "local-value-numbering"

    def run(self, ast, ctx: PassContext):
        lvn = _LocalValueNumbering(ctx)
        ast = lvn.visit(ast)
        return ast if lvn.changed else None


class GVNPass(ASTPass):
    """Pass 33: Global Value Numbering.

    Full GVN requires SSA + dominator tree — at AST level, we delegate
    the cross-block CSE to LLVM's GVN pass and focus on local numbering.
    This pass records analysis info in PassContext for codegen to use.
    """
    name = "gvn"

    def run(self, ast, ctx: PassContext):
        # At AST level, GVN is approximated by LVN + copy propagation.
        # Real cross-block GVN is done by LLVM -O2.
        ctx.record(self.name, "delegated", "LLVM GVN handles cross-block CSE")
        return None


class SROAPass(ASTPass):
    """Pass 36: Scalar Replacement of Aggregates.

    Identifies struct variables where only individual fields are accessed
    (no whole-struct operations) and marks them for scalar replacement
    in PassContext. Codegen can then generate separate allocas per field.
    """
    name = "sroa"

    def run(self, ast, ctx: PassContext):
        if not isinstance(ast, c_ast.FileAST):
            return None

        for ext in ast.ext or []:
            if isinstance(ext, c_ast.FuncDef):
                self._analyze_function(ext, ctx)
        return None

    def _analyze_function(self, funcdef, ctx: PassContext):
        func_name = funcdef.decl.name
        if not funcdef.body:
            return

        # Find struct declarations and track field access patterns
        struct_vars: dict[str, set[str]] = {}  # var → set of accessed fields
        whole_struct_use: set[str] = set()  # vars used as whole struct

        self._collect_struct_decls(funcdef.body, struct_vars)
        self._analyze_struct_access(funcdef.body, struct_vars, whole_struct_use)

        # Vars that only have field access (no whole-struct use) are SROA candidates
        for var_name, fields in struct_vars.items():
            if var_name not in whole_struct_use and fields:
                var_info = ctx.get_var(func_name, var_name)
                if not var_info.escapes:
                    ctx.record(
                        self.name, "sroa_candidate",
                        f"{func_name}::{var_name}",
                        f"fields={fields}",
                    )
                    ctx.bump("sroa.candidates")

    def _collect_struct_decls(self, node, struct_vars):
        if node is None:
            return
        if isinstance(node, c_ast.Decl) and node.name:
            if isinstance(node.type, c_ast.TypeDecl):
                if isinstance(node.type.type, c_ast.Struct):
                    struct_vars[node.name] = set()
        for _, child in node.children():
            if isinstance(child, c_ast.Node):
                self._collect_struct_decls(child, struct_vars)

    def _analyze_struct_access(self, node, struct_vars, whole_use):
        if node is None:
            return
        # s.field or s->field
        if isinstance(node, c_ast.StructRef):
            if isinstance(node.name, c_ast.ID) and node.name.name in struct_vars:
                struct_vars[node.name.name].add(node.field.name)
                return  # don't count this as whole-struct use
        # ID reference to struct var (not through StructRef) = whole-struct use
        if isinstance(node, c_ast.ID) and node.name in struct_vars:
            whole_use.add(node.name)
            return
        for _, child in node.children():
            if isinstance(child, c_ast.Node):
                self._analyze_struct_access(child, struct_vars, whole_use)
