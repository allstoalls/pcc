"""Passes 46-52, 54-55: Chez Scheme Nanopass-Inspired Transforms.

  46. Let Binding Elevation              — hoist inner bindings outward
  47. Primitive Specialization           — specialize ops for known types
  48. Recursive Unrolling                — unroll recursive calls N layers
  49. Assignment Conversion              — track mutability for SSA
  50. Loop Recognition                   — recognize tail-recursive patterns
  51. SCC Analysis (Tarjan)              — identify mutual recursion groups
  52. Closure Lifting / Alloc Elim       — lift non-escaping func ptrs
  54. Suppress Redundant Checks          — remove checks proven safe
  55. Float Unboxing                     — keep FP values in registers

Adapted from Chez Scheme's cpnanopass.ss to C semantics.
"""

from __future__ import annotations

from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.passes.ast_utils import ASTTransformer, collect_ids, get_int_value, is_side_effect_free
from pcc.frontends.c.passes.base import ASTPass
from pcc.frontends.c.passes.context import PassContext


# ── 46. Let Binding Elevation ───────────────────────────────────────────

class _LetElevator(ASTTransformer):
    """Move declarations closer to their use, or hoist to wider scope
    when this enables further optimization.

    In C, this maps to: if a variable is declared in an inner block but
    only used after the block, it could be declared outside.
    For now, we identify nested declarations that could be hoisted.
    """

    def __init__(self, ctx: PassContext):
        self.ctx = ctx
        self.changed = False

    def visit_Compound(self, node):
        self._visit_children(node)
        if not node.block_items:
            return node

        # Identify declarations that are used in later statements but
        # defined in inner scopes — these are elevation candidates.
        # (Analysis only for now.)
        for i, item in enumerate(node.block_items):
            if isinstance(item, c_ast.Compound) and item.block_items:
                for inner in item.block_items:
                    if isinstance(inner, c_ast.Decl) and inner.name:
                        # Check if name is referenced after this compound
                        for later in node.block_items[i + 1:]:
                            if inner.name in collect_ids(later):
                                self.ctx.record(
                                    "chez_let_elevate", "candidate",
                                    inner.name,
                                )
                                self.ctx.bump("chez.let_elevation_candidates")
                                break
        return node


class LetElevationPass(ASTPass):
    name = "let-elevation"

    def run(self, ast, ctx: PassContext):
        elev = _LetElevator(ctx)
        ast = elev.visit(ast)
        return None  # analysis-only


# ── 47. Primitive Specialization ────────────────────────────────────────

class PrimitiveSpecializationPass(ASTPass):
    """Specialize known library calls based on argument types.

    Examples:
    - abs(x) where x is unsigned → x (unsigned can't be negative)
    - memset(p, 0, n) → __builtin_memset (hint for LLVM)
    - strlen("literal") → constant
    """
    name = "primitive-specialization"

    def run(self, ast, ctx: PassContext):
        if not isinstance(ast, c_ast.FileAST):
            return None

        for ext in ast.ext or []:
            if isinstance(ext, c_ast.FuncDef):
                self._analyze(ext, ctx)
        return None

    def _analyze(self, funcdef, ctx):
        self._walk(funcdef.body, ctx, funcdef.decl.name)

    def _walk(self, node, ctx, func_name):
        if node is None:
            return
        if isinstance(node, c_ast.FuncCall) and isinstance(node.name, c_ast.ID):
            callee = node.name.name
            # strlen of string literal
            if callee == "strlen" and node.args and hasattr(node.args, "exprs"):
                args = node.args.exprs
                if len(args) == 1 and isinstance(args[0], c_ast.Constant):
                    if args[0].type == "string":
                        ctx.record(
                            self.name, "strlen_literal",
                            f"{func_name}: strlen can be folded",
                        )
                        ctx.bump("primitive_spec.strlen_literal")
            # abs of unsigned
            if callee == "abs":
                ctx.record(self.name, "abs_candidate", func_name)
                ctx.bump("primitive_spec.abs_candidates")

        for _, child in node.children():
            if isinstance(child, c_ast.Node):
                self._walk(child, ctx, func_name)


# ── 48. Recursive Unrolling ─────────────────────────────────────────────

class RecursiveUnrollingPass(ASTPass):
    """Detect self-recursive functions and mark them for unrolling.

    Chez CP0 unrolls recursive procedures by expanding N layers.
    At C level, we identify candidates (small self-recursive functions).
    """
    name = "recursive-unrolling"

    def run(self, ast, ctx: PassContext):
        if not isinstance(ast, c_ast.FileAST):
            return None

        for ext in ast.ext or []:
            if isinstance(ext, c_ast.FuncDef):
                name = ext.decl.name
                if self._is_self_recursive(ext.body, name):
                    body_size = self._count_nodes(ext.body)
                    if body_size <= 50:
                        ctx.record(
                            self.name, "unroll_candidate",
                            name, f"size={body_size}",
                        )
                        ctx.bump("recursive_unroll.candidates")
        return None

    @staticmethod
    def _is_self_recursive(node, func_name) -> bool:
        if node is None:
            return False
        if isinstance(node, c_ast.FuncCall):
            if isinstance(node.name, c_ast.ID) and node.name.name == func_name:
                return True
        for _, child in node.children():
            if isinstance(child, c_ast.Node):
                if RecursiveUnrollingPass._is_self_recursive(child, func_name):
                    return True
        return False

    @staticmethod
    def _count_nodes(node) -> int:
        if node is None:
            return 0
        count = 1
        for _, child in node.children():
            if isinstance(child, c_ast.Node):
                count += RecursiveUnrollingPass._count_nodes(child)
        return count


# ── 49. Assignment Conversion (SSA prep) ────────────────────────────────

class AssignmentConversionPass(ASTPass):
    """Chez's np-convert-assignments: identify mutable vs immutable bindings.

    In C, all variables are mutable. This pass classifies which variables
    are effectively immutable (single assignment) to help codegen emit
    SSA-friendly IR. Results are stored in PassContext.
    (Overlaps with escape_analysis — extends it with flow sensitivity.)
    """
    name = "assignment-conversion"

    def run(self, ast, ctx: PassContext):
        # This analysis is already done by EscapeAnalysisPass (single_def tracking).
        # Here we just verify and record additional info.
        for func_info in ctx.functions.values():
            immutable = sum(1 for v in func_info.var_infos.values() if v.single_def)
            mutable = len(func_info.var_infos) - immutable
            if func_info.var_infos:
                ctx.record(
                    self.name, "classification",
                    func_info.name,
                    f"immutable={immutable} mutable={mutable}",
                )
        return None


# ── 50. Loop Recognition ───────────────────────────────────────────────

class LoopRecognitionPass(ASTPass):
    """Chez's np-recognize-loops: identify loop patterns.

    In C, loops are explicit (for/while/do-while), but we also detect
    tail-recursive functions that are effectively loops.
    """
    name = "loop-recognition"

    def run(self, ast, ctx: PassContext):
        if not isinstance(ast, c_ast.FileAST):
            return None

        for ext in ast.ext or []:
            if isinstance(ext, c_ast.FuncDef):
                name = ext.decl.name
                if self._is_tail_recursive_loop(ext):
                    ctx.record(
                        self.name, "tail_recursive_loop",
                        name,
                    )
                    ctx.bump("loop_recognition.tail_recursive")
        return None

    @staticmethod
    def _is_tail_recursive_loop(funcdef: c_ast.FuncDef) -> bool:
        """Check if function is tail-recursive (could be converted to loop)."""
        name = funcdef.decl.name
        body = funcdef.body
        if not isinstance(body, c_ast.Compound) or not body.block_items:
            return False

        # Look for: if(base_case) return X; else return f(modified_args);
        for item in body.block_items:
            if isinstance(item, c_ast.If) and item.iffalse:
                if LoopRecognitionPass._returns_self_call(item.iftrue, name):
                    return True
                if LoopRecognitionPass._returns_self_call(item.iffalse, name):
                    return True
            if isinstance(item, c_ast.Return) and item.expr:
                if isinstance(item.expr, c_ast.FuncCall):
                    if isinstance(item.expr.name, c_ast.ID):
                        if item.expr.name.name == name:
                            return True
        return False

    @staticmethod
    def _returns_self_call(node, func_name) -> bool:
        if isinstance(node, c_ast.Return) and node.expr:
            if isinstance(node.expr, c_ast.FuncCall):
                if isinstance(node.expr.name, c_ast.ID):
                    return node.expr.name.name == func_name
        if isinstance(node, c_ast.Compound) and node.block_items:
            for item in node.block_items:
                if LoopRecognitionPass._returns_self_call(item, func_name):
                    return True
        return False


# ── 51. SCC Analysis (Tarjan) ──────────────────────────────────────────

class SCCAnalysisPass(ASTPass):
    """Tarjan's algorithm on call graph to find mutual recursion groups.

    Chez's np-identify-scc: identifies strongly connected components
    in the call graph to enable better optimization of recursive groups.
    """
    name = "scc-analysis"

    def run(self, ast, ctx: PassContext):
        if not isinstance(ast, c_ast.FileAST):
            return None

        # Build call graph
        call_graph: dict[str, set[str]] = {}
        defined_funcs: set[str] = set()

        for ext in ast.ext or []:
            if isinstance(ext, c_ast.FuncDef):
                name = ext.decl.name
                defined_funcs.add(name)
                callees = self._collect_callees(ext.body)
                call_graph[name] = callees.intersection(defined_funcs)

        # Run Tarjan's SCC
        sccs = self._tarjan(call_graph)

        for scc in sccs:
            if len(scc) > 1:
                ctx.record(
                    self.name, "mutual_recursion",
                    str(scc),
                )
                ctx.bump("scc.mutual_recursion_groups")
            elif len(scc) == 1:
                name = next(iter(scc))
                if name in call_graph.get(name, set()):
                    ctx.record(self.name, "self_recursive", name)
                    ctx.bump("scc.self_recursive")

        return None

    @staticmethod
    def _collect_callees(node) -> set[str]:
        callees: set[str] = set()
        if node is None:
            return callees
        if isinstance(node, c_ast.FuncCall):
            if isinstance(node.name, c_ast.ID):
                callees.add(node.name.name)
        for _, child in node.children():
            if isinstance(child, c_ast.Node):
                callees.update(SCCAnalysisPass._collect_callees(child))
        return callees

    @staticmethod
    def _tarjan(graph: dict[str, set[str]]) -> list[set[str]]:
        """Tarjan's SCC algorithm."""
        index_counter = [0]
        stack = []
        on_stack = set()
        index = {}
        lowlink = {}
        result = []

        def strongconnect(v):
            index[v] = index_counter[0]
            lowlink[v] = index_counter[0]
            index_counter[0] += 1
            stack.append(v)
            on_stack.add(v)

            for w in graph.get(v, set()):
                if w not in index:
                    strongconnect(w)
                    lowlink[v] = min(lowlink[v], lowlink[w])
                elif w in on_stack:
                    lowlink[v] = min(lowlink[v], index[w])

            if lowlink[v] == index[v]:
                scc = set()
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    scc.add(w)
                    if w == v:
                        break
                result.append(scc)

        for v in graph:
            if v not in index:
                strongconnect(v)

        return result


# ── 52. Closure Lifting / Allocation Elimination ───────────────────────

class ClosureLiftingPass(ASTPass):
    """Chez's np-lift-well-known-closures adapted for C.

    In C, "closures" = function pointers + context. This pass identifies
    function pointers that always point to the same function (known target)
    and marks them for direct call optimization.
    """
    name = "closure-lifting"

    def run(self, ast, ctx: PassContext):
        if not isinstance(ast, c_ast.FileAST):
            return None

        for ext in ast.ext or []:
            if isinstance(ext, c_ast.FuncDef):
                self._analyze(ext, ctx)
        return None

    def _analyze(self, funcdef, ctx):
        """Find function pointer variables always assigned the same function."""
        func_name = funcdef.decl.name
        # Track: fp_var → set of assigned function names
        fp_assignments: dict[str, set[str]] = {}
        self._collect_fp_assignments(funcdef.body, fp_assignments)

        for var, targets in fp_assignments.items():
            if len(targets) == 1:
                target = next(iter(targets))
                ctx.record(
                    self.name, "known_fp_target",
                    f"{func_name}::{var} → {target}",
                )
                ctx.bump("closure_lifting.known_targets")

    def _collect_fp_assignments(self, node, fp_map):
        if node is None:
            return
        # Pattern: fptr = func_name;
        if isinstance(node, c_ast.Assignment) and node.op == "=":
            if isinstance(node.lvalue, c_ast.ID) and isinstance(node.rvalue, c_ast.ID):
                fp_map.setdefault(node.lvalue.name, set()).add(node.rvalue.name)
        # Pattern: type (*fptr)(...) = func_name;
        if isinstance(node, c_ast.Decl) and node.name and isinstance(node.init, c_ast.ID):
            if isinstance(node.type, c_ast.PtrDecl):
                fp_map.setdefault(node.name, set()).add(node.init.name)
        for _, child in node.children():
            if isinstance(child, c_ast.Node):
                self._collect_fp_assignments(child, fp_map)


# ── 54. Suppress Redundant Checks ──────────────────────────────────────

class RedundantCheckPass(ASTPass):
    """Chez's np-suppress-procedure-checks adapted for C.

    Identifies redundant null/bounds checks that are provably safe.
    E.g., if(p != NULL) { ... *p ... } → the deref inside is safe.
    """
    name = "redundant-check"

    def run(self, ast, ctx: PassContext):
        if not isinstance(ast, c_ast.FileAST):
            return None

        for ext in ast.ext or []:
            if isinstance(ext, c_ast.FuncDef):
                self._analyze(ext.body, ctx, ext.decl.name)
        return None

    def _analyze(self, node, ctx, func_name):
        if node is None:
            return
        # if(p != NULL) { ... } — inside the true branch, p is known non-null
        if isinstance(node, c_ast.If):
            guarded = self._extract_null_guard(node.cond)
            if guarded:
                ctx.record(
                    self.name, "null_guarded",
                    f"{func_name}::{guarded}",
                )
                ctx.bump("redundant_check.null_guards")
        for _, child in node.children():
            if isinstance(child, c_ast.Node):
                self._analyze(child, ctx, func_name)

    @staticmethod
    def _extract_null_guard(cond) -> str | None:
        """Extract variable name from `p != NULL` or `p` (truthy check)."""
        if isinstance(cond, c_ast.ID):
            return cond.name
        if isinstance(cond, c_ast.BinaryOp) and cond.op == "!=":
            if isinstance(cond.left, c_ast.ID):
                if isinstance(cond.right, c_ast.Constant) and cond.right.value == "0":
                    return cond.left.name
                if isinstance(cond.right, c_ast.ID) and cond.right.name == "NULL":
                    return cond.left.name
        return None


# ── 55. Float Unboxing ─────────────────────────────────────────────────

class FloatUnboxingPass(ASTPass):
    """Chez's np-unbox-fp-vars: keep FP values in registers.

    Identifies float/double variables that can stay in FP registers
    without being stored to memory. Marks them in PassContext for codegen.
    """
    name = "float-unboxing"

    def run(self, ast, ctx: PassContext):
        for func_info in ctx.functions.values():
            for var_info in func_info.var_infos.values():
                if var_info.type_name in ("float", "double", "long double"):
                    if not var_info.escapes:
                        ctx.record(
                            self.name, "fp_register_candidate",
                            f"{func_info.name}::{var_info.name}",
                        )
                        ctx.bump("float_unboxing.candidates")
        return None
