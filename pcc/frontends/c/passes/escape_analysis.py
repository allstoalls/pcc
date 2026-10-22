"""HighTier Pass 1: Variable Escape Analysis.

Walks the AST to determine, for each local variable in each function:
  - address_taken: does &var appear?
  - def_count / use_count: how many assignments and reads?
  - single_def: exactly one assignment (including initializer)?
  - is_param: is this a function parameter?
  - escapes: conservative union of escape conditions

Inspired by Graal's Partial Escape Analysis (simplified for C):
  - In Graal, PEA is control-flow-sensitive (per-branch decisions)
  - Here we start with function-wide analysis (conservative but simple)
  - Future: extend to per-branch analysis for if/else paths
"""

from __future__ import annotations

from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.c_abi_layout import builtin_integer_is_unsigned, builtin_scalar_layout, pointer_scalar_layout
from pcc.frontends.c.passes.base import ASTPass
from pcc.frontends.c.passes.context import PassContext


class EscapeAnalysisPass(ASTPass):
    name = "escape-analysis"

    def run(self, ast, ctx: PassContext):
        if not isinstance(ast, c_ast.FileAST):
            return None

        for ext in ast.ext or []:
            if isinstance(ext, c_ast.FuncDef):
                self._analyze_function(ext, ctx)

        return None  # analysis-only, no AST modification

    def _analyze_function(self, funcdef: c_ast.FuncDef, ctx: PassContext):
        func_name = funcdef.decl.name
        func_info = ctx.get_func(func_name)

        # Phase 1: Collect all local variable declarations
        self._collect_declarations(funcdef, func_name, ctx)

        # Phase 2: Collect function parameters
        self._collect_params(funcdef, func_name, ctx)

        # Phase 3: Walk the body to analyze usage
        if funcdef.body:
            self._analyze_node(funcdef.body, func_name, ctx)

        # Phase 4: Compute derived properties
        for var_info in func_info.var_infos.values():
            var_info.escapes = var_info.address_taken or var_info.passed_to_call
            var_info.single_def = var_info.def_count == 1

            ctx.record(
                self.name,
                "analyzed",
                f"{func_name}::{var_info.name}",
                f"defs={var_info.def_count} uses={var_info.use_count} "
                f"escapes={var_info.escapes} single_def={var_info.single_def}",
            )

        ctx.bump("escape_analysis.functions_analyzed")
        ctx.bump(
            "escape_analysis.vars_analyzed",
            len(func_info.var_infos),
        )

    def _collect_declarations(self, funcdef, func_name, ctx):
        """Walk function body to find all local Decl nodes."""
        self._walk_for_decls(funcdef.body, func_name, ctx)

    def _walk_for_decls(self, node, func_name, ctx):
        if node is None:
            return
        if isinstance(node, c_ast.Decl) and node.name:
            # Only local variables (not function declarations)
            if not isinstance(node.type, (c_ast.FuncDecl,)):
                var = ctx.get_var(func_name, node.name)
                var.type_name = self._extract_type_name(node.type)
                var.is_unsigned = builtin_integer_is_unsigned(var.type_name.split(), ctx.target_triple)
                var.bit_width = self._infer_bit_width(var.type_name, ctx.target_triple)
                if node.init is not None:
                    var.def_count += 1
        for _, child in node.children():
            # Don't recurse into nested function definitions
            if isinstance(child, c_ast.FuncDef):
                continue
            self._walk_for_decls(child, func_name, ctx)

    def _collect_params(self, funcdef, func_name, ctx):
        """Register function parameters."""
        if funcdef.decl and isinstance(funcdef.decl.type, c_ast.FuncDecl):
            params = funcdef.decl.type.args
            if params:
                for param in params.params or []:
                    if isinstance(param, c_ast.Decl) and param.name:
                        var = ctx.get_var(func_name, param.name)
                        var.is_param = True
                        var.def_count += 1  # parameter = one definition
                        var.type_name = self._extract_type_name(param.type)
                        var.is_unsigned = builtin_integer_is_unsigned(var.type_name.split(), ctx.target_triple)
                        var.bit_width = self._infer_bit_width(var.type_name, ctx.target_triple)

                        # Check for restrict qualifier
                        if param.quals and "restrict" in param.quals:
                            func_info = ctx.get_func(func_name)
                            func_info.restrict_params.add(param.name)

    def _analyze_node(self, node, func_name, ctx):
        """Recursively analyze AST nodes for variable usage patterns."""
        if node is None:
            return

        # UnaryOp with '&' => address taken
        if isinstance(node, c_ast.UnaryOp) and node.op == "&":
            if isinstance(node.expr, c_ast.ID):
                var = ctx.get_var(func_name, node.expr.name)
                var.address_taken = True
                ctx.record(
                    self.name, "address_taken",
                    f"{func_name}::{node.expr.name}",
                )
            # Still recurse into the expression
            self._analyze_node(node.expr, func_name, ctx)
            return

        # Assignment => def_count on lhs, use_count on rhs
        if isinstance(node, c_ast.Assignment):
            if isinstance(node.lvalue, c_ast.ID):
                var = ctx.get_var(func_name, node.lvalue.name)
                var.def_count += 1
            else:
                self._analyze_node(node.lvalue, func_name, ctx)
            self._analyze_node(node.rvalue, func_name, ctx)
            return

        # UnaryOp with ++/-- => both def and use
        if isinstance(node, c_ast.UnaryOp) and node.op in ("++", "--", "p++", "p--"):
            if isinstance(node.expr, c_ast.ID):
                var = ctx.get_var(func_name, node.expr.name)
                var.def_count += 1
                var.use_count += 1
            else:
                self._analyze_node(node.expr, func_name, ctx)
            return

        # ID => use_count
        if isinstance(node, c_ast.ID):
            var = ctx.get_var(func_name, node.name)
            var.use_count += 1
            return

        # FuncCall => check if any arg is a local variable pointer
        if isinstance(node, c_ast.FuncCall):
            func_info = ctx.get_func(func_name)
            func_info.is_leaf = False
            # Check for special functions
            if isinstance(node.name, c_ast.ID):
                callee = node.name.name
                if callee in ("setjmp", "_setjmp", "sigsetjmp"):
                    func_info.has_setjmp = True
                elif callee == "alloca":
                    func_info.has_alloca_call = True
            # Analyze arguments — address-of in args means escape
            if node.args:
                for _, arg in node.args.children():
                    if isinstance(arg, c_ast.UnaryOp) and arg.op == "&":
                        if isinstance(arg.expr, c_ast.ID):
                            var = ctx.get_var(func_name, arg.expr.name)
                            var.passed_to_call = True
                    self._analyze_node(arg, func_name, ctx)
            # Analyze the function name expression too
            self._analyze_node(node.name, func_name, ctx)
            return

        # Goto
        if isinstance(node, c_ast.Goto):
            func_info = ctx.get_func(func_name)
            func_info.has_goto = True
            return

        # For loop depth tracking
        if isinstance(node, (c_ast.For, c_ast.While, c_ast.DoWhile)):
            func_info = ctx.get_func(func_name)
            # Simple: just count presence, not nesting depth yet
            func_info.max_loop_depth = max(func_info.max_loop_depth, 1)

        # Default: recurse into children
        for _, child in node.children():
            if isinstance(child, c_ast.FuncDef):
                continue
            self._analyze_node(child, func_name, ctx)

    @staticmethod
    def _extract_type_name(type_node) -> str:
        """Extract a human-readable type name from a type AST node."""
        if isinstance(type_node, c_ast.TypeDecl):
            if isinstance(type_node.type, c_ast.IdentifierType):
                return " ".join(type_node.type.names)
            if isinstance(type_node.type, c_ast.Struct):
                return f"struct {type_node.type.name or '<anon>'}"
            if isinstance(type_node.type, c_ast.Union):
                return f"union {type_node.type.name or '<anon>'}"
            if isinstance(type_node.type, c_ast.Enum):
                return f"enum {type_node.type.name or '<anon>'}"
        if isinstance(type_node, c_ast.PtrDecl):
            return EscapeAnalysisPass._extract_type_name(type_node.type) + "*"
        if isinstance(type_node, c_ast.ArrayDecl):
            return EscapeAnalysisPass._extract_type_name(type_node.type) + "[]"
        return "unknown"

    @staticmethod
    def _infer_bit_width(type_name: str, target_triple=None) -> int:
        if type_name.endswith("*"):
            return pointer_scalar_layout().size * 8
        base = type_name.rstrip("*[]").strip()
        # Remove qualifiers
        for q in ("const", "volatile", "restrict", "unsigned", "signed"):
            base = base.replace(q, "").strip()
        if base == "_Bool":
            return 1
        try:
            return builtin_scalar_layout(base.split(), target_triple).size * 8
        except ValueError:
            return 0
