"""Concrete Python-managed LLVM leaf-pass shims and lowerings.

This module gives LLVM leaf-pass names first-class visibility inside the
repository's Python pass system. Some passes are real AST transforms
(`lower-constant-intrinsics`, `float2int`, `alignment-from-assumptions`);
others are explicit analysis/no-op shims for LLVM subsystems that pcc does not
yet model directly at the AST/IR-text layer.
"""

from __future__ import annotations

import copy
import math
from collections import Counter

from pcc.frontends.c.ast import c_ast
from pcc.frontends.c.passes.ast_utils import ASTTransformer, collect_ids, contains_node_type, get_int_value, get_safe_int_value, is_side_effect_free, make_int_constant
from pcc.frontends.c.passes.base import ASTPass
from pcc.frontends.c.passes.context import PassContext

_PRAGMA_NODE = getattr(c_ast, "Pragma", ())


def _walk(node):
    """Flatten an AST subtree into a list in preorder."""
    out: list = []
    _walk_into(node, out)
    return out


def _walk_into(node, out: list) -> None:
    if node is None:
        return
    out.append(node)
    if not isinstance(node, c_ast.Node):
        return
    for _, child in node.children():
        if isinstance(child, c_ast.Node):
            _walk_into(child, out)


def _coord(node) -> str:
    coord = getattr(node, "coord", None)
    return f"{coord}" if coord else "?"


def _expr_key(node):
    if node is None:
        return None
    if isinstance(node, c_ast.ID):
        return ("id", node.name)
    if isinstance(node, c_ast.Constant):
        return ("const", node.type, node.value)
    if isinstance(node, c_ast.UnaryOp):
        return ("unary", node.op, _expr_key(node.expr))
    if isinstance(node, c_ast.BinaryOp):
        return ("binary", node.op, _expr_key(node.left), _expr_key(node.right))
    if isinstance(node, c_ast.Cast):
        return ("cast", _type_key(node.to_type), _expr_key(node.expr))
    if isinstance(node, c_ast.TernaryOp):
        return (
            "ternary",
            _expr_key(node.cond),
            _expr_key(node.iftrue),
            _expr_key(node.iffalse),
        )
    if isinstance(node, c_ast.InitList):
        return ("init", tuple(_expr_key(expr) for expr in (node.exprs or ())))
    if isinstance(node, c_ast.ExprList):
        return ("exprs", tuple(_expr_key(expr) for expr in (node.exprs or ())))
    if isinstance(node, c_ast.StructRef):
        return ("structref", node.type, _expr_key(node.name), _expr_key(node.field))
    if isinstance(node, c_ast.ArrayRef):
        return ("arrayref", _expr_key(node.name), _expr_key(node.subscript))
    if isinstance(node, c_ast.FuncCall):
        args = ()
        if node.args:
            args = tuple(_expr_key(a) for a in (node.args.exprs or ()))
        return ("funccall", _expr_key(node.name), args)
    # Unhandled node type — return unique key so it never falsely matches.
    return ("__unknown__", id(node))


def _type_key(node):
    if node is None:
        return None
    if isinstance(node, c_ast.Typename):
        return _type_key(node.type)
    if isinstance(node, c_ast.TypeDecl):
        return _type_key(node.type)
    if isinstance(node, c_ast.IdentifierType):
        return tuple(node.names or ())
    if isinstance(node, c_ast.PtrDecl):
        return ("ptr", _type_key(node.type))
    if isinstance(node, c_ast.ArrayDecl):
        return ("array", _type_key(node.type))
    return (node.__class__.__name__,)


def _is_compile_time_constant_expr(node) -> bool:
    if node is None:
        return False
    if isinstance(node, c_ast.Constant):
        return True
    if isinstance(node, c_ast.Cast):
        return _is_compile_time_constant_expr(node.expr)
    if isinstance(node, c_ast.UnaryOp):
        if node.op in ("+", "-", "~", "!"):
            return _is_compile_time_constant_expr(node.expr)
        if node.op in ("sizeof", "_Alignof", "__alignof", "__alignof__"):
            return True
        return False
    if isinstance(node, c_ast.BinaryOp):
        return (
            node.op
            in {
                "+",
                "-",
                "*",
                "/",
                "%",
                "<<",
                ">>",
                "&",
                "|",
                "^",
                "&&",
                "||",
                "<",
                "<=",
                ">",
                ">=",
                "==",
                "!=",
            }
            and _is_compile_time_constant_expr(node.left)
            and _is_compile_time_constant_expr(node.right)
        )
    if isinstance(node, c_ast.TernaryOp):
        return (
            _is_compile_time_constant_expr(node.cond)
            and _is_compile_time_constant_expr(node.iftrue)
            and _is_compile_time_constant_expr(node.iffalse)
        )
    return False


def _const_float_value(node) -> float | None:
    if isinstance(node, c_ast.Constant) and node.type in ("float", "double"):
        try:
            return float(node.value)
        except (TypeError, ValueError):
            return None
    if isinstance(node, c_ast.UnaryOp) and node.op in ("+", "-"):
        inner = _const_float_value(node.expr)
        if inner is None:
            return None
        return inner if node.op == "+" else -inner
    return None


def _func_name(funcdef) -> str:
    decl = getattr(funcdef, "decl", None)
    return str(getattr(decl, "name", "") or "<anon>")


def _iter_global_object_decls(ast):
    """Return a list of top-level object (non-function) decls."""
    out: list = []
    if not isinstance(ast, c_ast.FileAST):
        return out
    for ext in ast.ext or ():
        if not isinstance(ext, c_ast.Decl):
            continue
        if "typedef" in tuple(getattr(ext, "storage", ()) or ()):
            continue
        if isinstance(getattr(ext, "type", None), c_ast.FuncDecl):
            continue
        if getattr(ext, "name", None):
            out.append(ext)
    return out


def _decl_has_qualifier(decl, qualifier: str) -> bool:
    node = decl
    while node is not None:
        quals = tuple(getattr(node, "quals", ()) or ())
        if qualifier in quals:
            return True
        if isinstance(node, c_ast.Decl):
            node = getattr(node, "type", None)
            continue
        if isinstance(node, (c_ast.TypeDecl, c_ast.PtrDecl, c_ast.ArrayDecl)):
            node = getattr(node, "type", None)
            continue
        break
    return False


def _decl_type_contains(decl, node_type) -> bool:
    node = getattr(decl, "type", None)
    while node is not None:
        if isinstance(node, node_type):
            return True
        if isinstance(node, (c_ast.TypeDecl, c_ast.PtrDecl, c_ast.ArrayDecl)):
            node = getattr(node, "type", None)
            continue
        break
    return False


def _function_local_names(funcdef) -> set[str]:
    names = set()
    ftype = getattr(getattr(funcdef, "decl", None), "type", None)
    for param in getattr(getattr(ftype, "args", None), "params", None) or ():
        if isinstance(param, c_ast.Decl) and getattr(param, "name", None):
            names.add(param.name)
    for node in _walk(getattr(funcdef, "body", None)):
        if isinstance(node, c_ast.Decl) and getattr(node, "name", None):
            names.add(node.name)
    return names


def _is_pointer_like_lookup_expr(node) -> bool:
    if isinstance(node, c_ast.ID):
        return True
    if isinstance(node, c_ast.UnaryOp) and node.op == "&":
        return isinstance(node.expr, c_ast.ID)
    if isinstance(node, c_ast.Cast):
        return _is_pointer_like_lookup_expr(node.expr)
    return False


def _is_pure_data_expr(node) -> bool:
    if isinstance(node, (c_ast.Constant, c_ast.ID)):
        return True
    if isinstance(node, c_ast.ArrayRef):
        return _is_pure_data_expr(node.name) and _is_pure_data_expr(node.subscript)
    if isinstance(node, c_ast.UnaryOp):
        if node.op in ("++", "--", "p++", "p--"):
            return False
        return _is_pure_data_expr(node.expr)
    if isinstance(node, c_ast.BinaryOp):
        return _is_pure_data_expr(node.left) and _is_pure_data_expr(node.right)
    if isinstance(node, c_ast.Cast):
        return _is_pure_data_expr(node.expr)
    if isinstance(node, c_ast.TernaryOp):
        return (
            _is_pure_data_expr(node.cond)
            and _is_pure_data_expr(node.iftrue)
            and _is_pure_data_expr(node.iffalse)
        )
    return False


def _is_pragma_node(node) -> bool:
    return bool(_PRAGMA_NODE) and isinstance(node, _PRAGMA_NODE)


def _stringify_type_bucket(bucket) -> str:
    if isinstance(bucket, tuple):
        return "(" + ", ".join(_stringify_type_bucket(item) for item in bucket) + ")"
    return str(bucket)


def _iter_named_calls(node):
    """Return a list of ``(FuncCall, name)`` pairs for every named
    call reachable from ``node``."""
    out: list = []
    for child in _walk(node):
        if isinstance(child, c_ast.FuncCall) and isinstance(child.name, c_ast.ID):
            out.append((child, child.name.name))
    return out


class _ScannerPass(ASTPass):
    """Tiny base class for explicit named passes that mainly expose control."""

    name = "llvm-scanner"

    def scan(self, ast, ctx: PassContext) -> int:
        return 0

    def run(self, ast, ctx: PassContext):
        count = int(self.scan(ast, ctx) or 0)
        if count:
            ctx.bump(f"{self.name}.matches", count)
            ctx.record(self.name, "analysis", "module", f"{count} match(es)")
        else:
            ctx.bump(f"{self.name}.modules")
        return None


class _BuiltinConstantIntrinsicTransformer(ASTTransformer):
    def __init__(self, ctx: PassContext):
        self.ctx = ctx
        self.changed = False

    def _mark(self, action, node):
        self.changed = True
        self.ctx.bump(f"lower_constant_intrinsics.{action}")
        self.ctx.record("lower-constant-intrinsics", action, _coord(node))

    def visit_FuncCall(self, node):
        self._visit_children(node)
        if not isinstance(node.name, c_ast.ID):
            return node
        if node.name.name != "__builtin_constant_p":
            return node
        if node.args is None or not hasattr(node.args, "exprs"):
            return node
        exprs = list(node.args.exprs or [])
        if len(exprs) != 1:
            return node
        self._mark("lowered", node)
        return make_int_constant(1 if _is_compile_time_constant_expr(exprs[0]) else 0, node.coord)


class LowerConstantIntrinsicsPass(ASTPass):
    """Lower constant-query builtins such as `__builtin_constant_p`."""

    name = "lower-constant-intrinsics"

    def run(self, ast, ctx: PassContext):
        transformer = _BuiltinConstantIntrinsicTransformer(ctx)
        ast = transformer.visit(ast)
        return ast if transformer.changed else None


class _AlignmentFromAssumptionsTransformer(ASTTransformer):
    def __init__(self, ctx: PassContext):
        self.ctx = ctx
        self.changed = False

    def _mark(self, action, node):
        self.changed = True
        self.ctx.bump(f"alignment_from_assumptions.{action}")
        self.ctx.record("alignment-from-assumptions", action, _coord(node))

    def visit_FuncCall(self, node):
        self._visit_children(node)
        if not isinstance(node.name, c_ast.ID):
            return node
        if node.name.name != "__builtin_assume_aligned":
            return node
        if node.args is None or not hasattr(node.args, "exprs"):
            return node
        exprs = list(node.args.exprs or [])
        if len(exprs) not in (2, 3):
            return node
        if not all(is_side_effect_free(expr) for expr in exprs[1:]):
            return node
        self._mark("lowered", node)
        return exprs[0]


class AlignmentFromAssumptionsPass(ASTPass):
    """Lower `__builtin_assume_aligned(...)` to its underlying pointer."""

    name = "alignment-from-assumptions"

    def run(self, ast, ctx: PassContext):
        transformer = _AlignmentFromAssumptionsTransformer(ctx)
        ast = transformer.visit(ast)
        return ast if transformer.changed else None


class _FloatToIntTransformer(ASTTransformer):
    _SUPPORTED_TARGETS = {
        ("int",): (-2**31, 2**31 - 1),
        ("unsigned", "int"): (0, 2**32 - 1),
    }

    def __init__(self, ctx: PassContext):
        self.ctx = ctx
        self.changed = False

    def _mark(self, action, node):
        self.changed = True
        self.ctx.bump(f"float2int.{action}")
        self.ctx.record("float2int", action, _coord(node))

    def visit_Cast(self, node):
        self._visit_children(node)
        float_value = _const_float_value(node.expr)
        if float_value is None or not math.isfinite(float_value):
            return node

        target = _type_key(node.to_type)
        bounds = self._SUPPORTED_TARGETS.get(target)
        if bounds is None:
            return node

        converted = int(float_value)
        lo, hi = bounds
        if converted < lo or converted > hi:
            return node
        if target == ("unsigned", "int") and float_value < 0:
            return node

        self._mark("folded", node)
        return make_int_constant(converted, node.coord)


class FloatToIntPass(ASTPass):
    """Fold simple constant float-to-int casts before codegen."""

    name = "float2int"

    def run(self, ast, ctx: PassContext):
        transformer = _FloatToIntTransformer(ctx)
        ast = transformer.visit(ast)
        return ast if transformer.changed else None


class CoroEarlyPass(_ScannerPass):
    name = "coro-early"

    def run(self, ast, ctx: PassContext):
        prefix = self.name.replace("-", "_")
        counts = Counter()
        for node, callee in _iter_named_calls(ast):
            if callee.startswith("__builtin_coro"):
                counts[callee] += 1
                ctx.record(self.name, "builtin", _coord(node), callee)
        ctx.bump(f"{prefix}.modules")
        ctx.bump(f"{prefix}.sites", sum(counts.values()))
        if counts:
            detail = " ".join(f"{name}={count}" for name, count in sorted(counts.items()))
            ctx.record(self.name, "summary", "module", detail)
        return None


class EEInstrumentPass(_ScannerPass):
    name = "ee-instrument"

    _HOOK_PREFIXES = (
        "__cyg_profile_",
        "__llvm_profile_",
        "__sanitizer_",
        "__ubsan_",
        "__asan_",
        "__tsan_",
        "__msan_",
    )

    def run(self, ast, ctx: PassContext):
        counts = Counter()
        for node, callee in _iter_named_calls(ast):
            if callee.startswith(self._HOOK_PREFIXES):
                counts[callee] += 1
                ctx.record(self.name, "hook", _coord(node), callee)
        ctx.bump("ee_instrument.modules")
        ctx.bump("ee_instrument.hook_sites", sum(counts.values()))
        if counts:
            detail = " ".join(f"{name}={count}" for name, count in sorted(counts.items()))
            ctx.record(self.name, "summary", "module", detail)
        return None


class OpenMPOptPass(_ScannerPass):
    name = "openmp-opt"

    def run(self, ast, ctx: PassContext):
        counts = Counter()
        for node in _walk(ast):
            if _is_pragma_node(node) and "omp" in str(getattr(node, "string", "")).lower():
                counts["pragma_sites"] += 1
                ctx.record(self.name, "pragma", _coord(node), str(getattr(node, "string", "") or ""))
        for node, callee in _iter_named_calls(ast):
            if callee.startswith("__kmpc_") or callee.startswith("omp_"):
                counts["runtime_calls"] += 1
                ctx.record(self.name, "runtime_hook", _coord(node), callee)
        ctx.bump("openmp_opt.modules")
        for key, value in sorted(counts.items()):
            ctx.bump(f"openmp_opt.{key}", value)
        if counts:
            detail = " ".join(f"{name}={count}" for name, count in sorted(counts.items()))
            ctx.record(self.name, "summary", "module", detail)
        return None


class RequirePass(_ScannerPass):
    name = "require"

    def run(self, ast, ctx: PassContext):
        func_count = 0
        pragma_count = 0
        for node in _walk(ast):
            if isinstance(node, c_ast.FuncDef):
                func_count += 1
            elif _is_pragma_node(node):
                pragma_count += 1
        global_count = sum(1 for _ in _iter_global_object_decls(ast))
        ctx.bump("require.modules")
        ctx.bump("require.functions_seen", func_count)
        ctx.bump("require.globals_seen", global_count)
        ctx.bump("require.pragmas_seen", pragma_count)
        ctx.record(
            self.name,
            "satisfied",
            "module",
            f"functions={func_count} globals={global_count} pragmas={pragma_count}",
        )
        return None


class InvalidatePass(_ScannerPass):
    name = "invalidate"

    def run(self, ast, ctx: PassContext):
        tracked_functions = len(ctx.functions)
        tracked_stats = len(ctx.stats)
        tracked_tbaa = len(ctx.tbaa)
        ctx.bump("invalidate.boundaries")
        ctx.bump("invalidate.tracked_functions", tracked_functions)
        ctx.bump("invalidate.tracked_stats", tracked_stats)
        ctx.bump("invalidate.tracked_tbaa_classes", tracked_tbaa)
        ctx.record(
            self.name,
            "barrier",
            "module",
            (
                f"functions={tracked_functions} "
                f"stats={tracked_stats} tbaa={tracked_tbaa}"
            ),
        )
        return None


class LibcallsShrinkwrapPass(_ScannerPass):
    name = "libcalls-shrinkwrap"

    def run(self, ast, ctx: PassContext):
        class _LibcallShrinker(ASTTransformer):
            def __init__(self):
                self.changed = False

            def _mark(self, action, node, detail=""):
                self.changed = True
                ctx.record("libcalls-shrinkwrap", action, _coord(node), detail)
                ctx.bump(f"libcalls_shrinkwrap.{action}")

            def visit_Compound(self, node):
                if not node.block_items:
                    return node

                rewritten = []
                for item in node.block_items:
                    if isinstance(item, c_ast.FuncCall):
                        new_item = self._rewrite_stmt_call(item)
                        if new_item is None:
                            continue
                        rewritten.append(new_item)
                        continue
                    rewritten.append(self.visit(item))
                node.block_items = rewritten
                return node

            def visit_FuncCall(self, node):
                self._visit_children(node)
                return self._rewrite_expr_call(node)

            def _rewrite_stmt_call(self, node):
                self._visit_children(node)
                if not isinstance(node.name, c_ast.ID):
                    return node
                name = node.name.name
                exprs = list(getattr(getattr(node, "args", None), "exprs", ()) or ())
                if (
                    name in {"memcpy", "memmove", "memset"}
                    and len(exprs) == 3
                    and self._is_zero_len(exprs[2])
                    and all(is_side_effect_free(expr) for expr in exprs)
                ):
                    self._mark("drop_zero_length_call", node, name)
                    return None

                rewritten = self._rewrite_expr_call(node)
                if isinstance(rewritten, c_ast.Constant):
                    self._mark("drop_pure_result", node, name)
                    return None
                return rewritten

            def _rewrite_expr_call(self, node):
                if not isinstance(node.name, c_ast.ID):
                    return node
                name = node.name.name
                exprs = list(getattr(getattr(node, "args", None), "exprs", ()) or ())
                if (
                    name == "memcmp"
                    and len(exprs) == 3
                    and self._is_zero_len(exprs[2])
                    and all(is_side_effect_free(expr) for expr in exprs)
                ):
                    self._mark("fold_memcmp_zero", node)
                    return make_int_constant(0, node.coord)
                if (
                    name == "strcmp"
                    and len(exprs) == 2
                    and is_side_effect_free(exprs[0])
                    and is_side_effect_free(exprs[1])
                    and _expr_key(exprs[0]) == _expr_key(exprs[1])
                ):
                    self._mark("fold_strcmp_same", node)
                    return make_int_constant(0, node.coord)
                if name == "strlen" and len(exprs) == 1 and self._is_empty_string(exprs[0]):
                    self._mark("fold_strlen_empty", node)
                    return make_int_constant(0, node.coord)
                return node

            @staticmethod
            def _is_zero_len(node) -> bool:
                return get_int_value(node) == 0

            @staticmethod
            def _is_empty_string(node) -> bool:
                if not isinstance(node, c_ast.Constant) or node.type != "string":
                    return False
                value = str(node.value or "")
                return value in {'""', 'L""', 'u8""', 'u""', 'U""'}

        tx = _LibcallShrinker()
        ast = tx.visit(ast)
        return ast if tx.changed else None


class CoroElidePass(_ScannerPass):
    name = "coro-elide"

    def run(self, ast, ctx: PassContext):
        prefix = self.name.replace("-", "_")
        counts = Counter()
        for node, callee in _iter_named_calls(ast):
            if callee.startswith("__builtin_coro"):
                counts[callee] += 1
                ctx.record(self.name, "builtin", _coord(node), callee)
        ctx.bump(f"{prefix}.modules")
        ctx.bump(f"{prefix}.sites", sum(counts.values()))
        if counts:
            detail = " ".join(f"{name}={count}" for name, count in sorted(counts.items()))
            ctx.record(self.name, "summary", "module", detail)
        return None


class CoroSplitPass(_ScannerPass):
    name = "coro-split"

    def run(self, ast, ctx: PassContext):
        prefix = self.name.replace("-", "_")
        counts = Counter()
        for node, callee in _iter_named_calls(ast):
            if callee.startswith("__builtin_coro"):
                counts[callee] += 1
                ctx.record(self.name, "builtin", _coord(node), callee)
        ctx.bump(f"{prefix}.modules")
        ctx.bump(f"{prefix}.sites", sum(counts.values()))
        if counts:
            detail = " ".join(f"{name}={count}" for name, count in sorted(counts.items()))
            ctx.record(self.name, "summary", "module", detail)
        return None


class CoroAnnotationElidePass(_ScannerPass):
    name = "coro-annotation-elide"

    def run(self, ast, ctx: PassContext):
        prefix = self.name.replace("-", "_")
        counts = Counter()
        for node, callee in _iter_named_calls(ast):
            if callee.startswith("__builtin_coro"):
                counts[callee] += 1
                ctx.record(self.name, "builtin", _coord(node), callee)
        ctx.bump(f"{prefix}.modules")
        ctx.bump(f"{prefix}.sites", sum(counts.values()))
        if counts:
            detail = " ".join(f"{name}={count}" for name, count in sorted(counts.items()))
            ctx.record(self.name, "summary", "module", detail)
        return None


class CoroCleanupPass(_ScannerPass):
    name = "coro-cleanup"

    def run(self, ast, ctx: PassContext):
        prefix = self.name.replace("-", "_")
        counts = Counter()
        for node, callee in _iter_named_calls(ast):
            if callee.startswith("__builtin_coro"):
                counts[callee] += 1
                ctx.record(self.name, "builtin", _coord(node), callee)
        ctx.bump(f"{prefix}.modules")
        ctx.bump(f"{prefix}.sites", sum(counts.values()))
        if counts:
            detail = " ".join(f"{name}={count}" for name, count in sorted(counts.items()))
            ctx.record(self.name, "summary", "module", detail)
        return None


class OpenMPCGSCCPass(_ScannerPass):
    name = "openmp-opt-cgscc"

    def run(self, ast, ctx: PassContext):
        counts = Counter()
        for node in _walk(ast):
            if _is_pragma_node(node) and "omp" in str(getattr(node, "string", "")).lower():
                counts["pragma_sites"] += 1
                ctx.record(self.name, "pragma", _coord(node), str(getattr(node, "string", "") or ""))
        for node, callee in _iter_named_calls(ast):
            if callee.startswith("__kmpc_") or callee.startswith("omp_"):
                counts["runtime_calls"] += 1
                ctx.record(self.name, "runtime_hook", _coord(node), callee)
        ctx.bump("openmp_opt_cgscc.modules")
        for key, value in sorted(counts.items()):
            ctx.bump(f"openmp_opt_cgscc.{key}", value)
        if counts:
            detail = " ".join(f"{name}={count}" for name, count in sorted(counts.items()))
            ctx.record(self.name, "summary", "module", detail)
        return None


class RecomputeGlobalsAAPass(_ScannerPass):
    name = "recompute-globalsaa"

    def run(self, ast, ctx: PassContext):
        globals_ = list(_iter_global_object_decls(ast))
        address_taken = {
            node.expr.name
            for node in _walk(ast)
            if (
                isinstance(node, c_ast.UnaryOp)
                and node.op == "&"
                and isinstance(node.expr, c_ast.ID)
            )
        }
        type_buckets = Counter()
        const_globals = 0
        mutable_globals = 0
        pointer_globals = 0
        array_globals = 0
        address_taken_globals = 0

        for decl in globals_:
            bucket = _type_key(getattr(decl, "type", None)) or ("unknown",)
            type_buckets[bucket] += 1
            if _decl_has_qualifier(decl, "const"):
                const_globals += 1
            else:
                mutable_globals += 1
            if _decl_type_contains(decl, c_ast.PtrDecl):
                pointer_globals += 1
            if isinstance(getattr(decl, "type", None), c_ast.ArrayDecl):
                array_globals += 1
            if decl.name in address_taken:
                address_taken_globals += 1

        ctx.bump("recompute_globalsaa.modules")
        ctx.bump("recompute_globalsaa.globals", len(globals_))
        ctx.bump("recompute_globalsaa.const_globals", const_globals)
        ctx.bump("recompute_globalsaa.mutable_globals", mutable_globals)
        ctx.bump("recompute_globalsaa.pointer_globals", pointer_globals)
        ctx.bump("recompute_globalsaa.array_globals", array_globals)
        ctx.bump("recompute_globalsaa.address_taken_globals", address_taken_globals)
        for bucket, count in sorted(type_buckets.items()):
            ctx.record(
                self.name,
                "alias_bucket",
                "module",
                f"{_stringify_type_bucket(bucket)}:{count}",
            )
        ctx.record(
            self.name,
            "recomputed",
            "module",
            (
                f"globals={len(globals_)} const={const_globals} mutable={mutable_globals} "
                f"ptr={pointer_globals} arrays={array_globals} addr_taken={address_taken_globals}"
            ),
        )
        return None


class LoopDistributePass(_ScannerPass):
    name = "loop-distribute"

    def run(self, ast, ctx: PassContext):
        class _LoopDistributor(ASTTransformer):
            def __init__(self):
                self.changed = False

            def _mark(self, action, node):
                self.changed = True
                ctx.record("loop-distribute", action, _coord(node))
                ctx.bump(f"loop_distribute.{action}")

            def visit_Compound(self, node):
                self._visit_children(node)
                if not node.block_items:
                    return node

                rewritten = []
                for item in node.block_items:
                    replacement = self._rewrite_item(item)
                    if replacement is None:
                        rewritten.append(item)
                    else:
                        rewritten.extend(replacement)
                node.block_items = rewritten
                return node

            def _rewrite_item(self, item):
                if not isinstance(item, c_ast.For):
                    return None
                body = getattr(item, "stmt", None)
                if not isinstance(body, c_ast.Compound):
                    return None
                stmts = list(body.block_items or ())
                if len(stmts) < 2:
                    return None
                if not all(self._is_distributable_stmt(stmt) for stmt in stmts):
                    return None

                deps = [self._stmt_deps(stmt) for stmt in stmts]
                written_names = [written for written, _reads in deps]
                read_names = [reads for _written, reads in deps]
                for idx, written in enumerate(written_names):
                    other_written = set().union(
                        *(names for j, names in enumerate(written_names) if j != idx)
                    )
                    if written & other_written:
                        return None
                    if read_names[idx].intersection(other_written):
                        return None

                self._mark("distribute", item)
                loops = []
                for stmt in stmts:
                    new_loop = copy.deepcopy(item)
                    new_loop.stmt = c_ast.Compound(block_items=[copy.deepcopy(stmt)], coord=getattr(body, "coord", None))
                    loops.append(new_loop)
                return loops

            @staticmethod
            def _is_distributable_stmt(stmt) -> bool:
                if not isinstance(stmt, c_ast.Assignment):
                    return False
                if stmt.op not in ("=", "+=", "-=", "*=", "&=", "|=", "^="):
                    return False
                if not is_side_effect_free(stmt.rvalue):
                    return False
                return LoopDistributePass._lvalue_base_name(stmt.lvalue) is not None

            @staticmethod
            def _stmt_deps(stmt):
                written = set()
                base = LoopDistributePass._lvalue_base_name(stmt.lvalue)
                if base:
                    written.add(base)
                reads = set(collect_ids(stmt.rvalue))
                if isinstance(stmt.lvalue, c_ast.ArrayRef):
                    reads.update(collect_ids(stmt.lvalue.subscript))
                return written, reads

        tx = _LoopDistributor()
        ast = tx.visit(ast)
        return ast if tx.changed else None

    @staticmethod
    def _lvalue_base_name(node):
        if isinstance(node, c_ast.ID):
            return node.name
        if isinstance(node, c_ast.ArrayRef) and isinstance(node.name, c_ast.ID):
            return node.name.name
        return None


class InjectTLIMappingsPass(_ScannerPass):
    name = "inject-tli-mappings"

    _DIRECT_NAME_MAP = {
        "__builtin_memcpy": "memcpy",
        "__builtin_memmove": "memmove",
        "__builtin_memcmp": "memcmp",
        "__builtin_memchr": "memchr",
        "__builtin_memset": "memset",
        "__builtin_strlen": "strlen",
        "__builtin_strcmp": "strcmp",
        "__builtin_strcpy": "strcpy",
        "__builtin_strncpy": "strncpy",
    }

    _TRUNCATED_NAME_MAP = {
        "__builtin___memcpy_chk": ("memcpy", 3),
        "__builtin___memmove_chk": ("memmove", 3),
        "__builtin___memset_chk": ("memset", 3),
        "__builtin___strcpy_chk": ("strcpy", 2),
        "__builtin___strncpy_chk": ("strncpy", 3),
    }

    def run(self, ast, ctx: PassContext):
        class _TLIMapper(ASTTransformer):
            def __init__(self):
                self.changed = False

            def _mark(self, action, node, detail=""):
                self.changed = True
                ctx.record("inject-tli-mappings", action, _coord(node), detail)
                ctx.bump(f"inject_tli_mappings.{action}")

            def visit_FuncCall(self, node):
                self._visit_children(node)
                if not isinstance(node.name, c_ast.ID):
                    return node
                callee = node.name.name
                exprs = list(getattr(getattr(node, "args", None), "exprs", ()) or ())

                direct = InjectTLIMappingsPass._DIRECT_NAME_MAP.get(callee)
                if direct is not None:
                    node.name = c_ast.ID(direct, coord=node.name.coord)
                    self._mark("rename_builtin", node, f"{callee}->{direct}")
                    return node

                truncated = InjectTLIMappingsPass._TRUNCATED_NAME_MAP.get(callee)
                if truncated is not None:
                    target_name, keep = truncated
                    if len(exprs) >= keep:
                        node.name = c_ast.ID(target_name, coord=node.name.coord)
                        node.args = c_ast.ExprList(exprs=exprs[:keep], coord=getattr(node.args, "coord", None))
                        self._mark("truncate_builtin_chk", node, f"{callee}->{target_name}")
                    return node

                if callee == "__builtin_bzero" and len(exprs) == 2:
                    node.name = c_ast.ID("memset", coord=node.name.coord)
                    zero = c_ast.Constant("int", "0", coord=getattr(exprs[0], "coord", None))
                    node.args = c_ast.ExprList(exprs=[exprs[0], zero, exprs[1]], coord=getattr(node.args, "coord", None))
                    self._mark("lower_bzero", node, "__builtin_bzero->memset")
                    return node

                return node

        tx = _TLIMapper()
        ast = tx.visit(ast)
        return ast if tx.changed else None


class LoopVectorizePass(_ScannerPass):
    name = "loop-vectorize"

    def run(self, ast, ctx: PassContext):
        candidate_count = 0
        vector_stmt_count = 0
        reduction_count = 0

        for node in _walk(ast):
            if not isinstance(node, c_ast.For):
                continue
            if getattr(node, "cond", None) is None or getattr(node, "next", None) is None:
                continue
            body = getattr(node, "stmt", None)
            if not isinstance(body, c_ast.Compound) or not body.block_items:
                continue
            if any(
                contains_node_type(item, (c_ast.If, c_ast.Switch, c_ast.Case, c_ast.Default))
                for item in body.block_items
            ):
                continue

            loop_vector_stmts = 0
            loop_reductions = 0
            for item in body.block_items:
                if self._is_vector_store_stmt(item):
                    loop_vector_stmts += 1
                elif self._is_reduction_stmt(item):
                    loop_reductions += 1

            if loop_vector_stmts == 0 and loop_reductions == 0:
                continue

            candidate_count += 1
            vector_stmt_count += loop_vector_stmts
            reduction_count += loop_reductions
            ctx.record(
                self.name,
                "candidate",
                _coord(node),
                f"vector_stmts={loop_vector_stmts} reductions={loop_reductions}",
            )

        ctx.bump("loop_vectorize.modules")
        ctx.bump("loop_vectorize.candidates", candidate_count)
        ctx.bump("loop_vectorize.vector_stmts", vector_stmt_count)
        ctx.bump("loop_vectorize.reductions", reduction_count)
        return None

    @staticmethod
    def _is_vector_store_stmt(stmt) -> bool:
        if not isinstance(stmt, c_ast.Assignment) or stmt.op != "=":
            return False
        if not isinstance(stmt.lvalue, c_ast.ArrayRef):
            return False
        if not _is_pure_data_expr(stmt.rvalue):
            return False
        ids = collect_ids(stmt.rvalue)
        return bool(ids)

    @staticmethod
    def _is_reduction_stmt(stmt) -> bool:
        if not isinstance(stmt, c_ast.Assignment):
            return False
        if not isinstance(stmt.lvalue, c_ast.ID):
            return False
        if stmt.op in {"+=", "-=", "*=", "&=", "|=", "^="}:
            return is_side_effect_free(stmt.rvalue)
        if stmt.op != "=" or not isinstance(stmt.rvalue, c_ast.BinaryOp):
            return False
        target = stmt.lvalue.name
        return target in collect_ids(stmt.rvalue) and is_side_effect_free(stmt.rvalue)


class VectorCombinePass(_ScannerPass):
    name = "vector-combine"

    def run(self, ast, ctx: PassContext):
        class _VectorCombiner(ASTTransformer):
            _COMMUTATIVE = {"&", "|", "^"}

            def __init__(self):
                self.changed = False

            def _mark(self, action, node, detail=""):
                self.changed = True
                ctx.record("vector-combine", action, _coord(node), detail)
                ctx.bump(f"vector_combine.{action}")

            def visit_BinaryOp(self, node):
                self._visit_children(node)
                if node.op not in self._COMMUTATIVE:
                    return node

                extracted = self._extract_nested_const(node.left, node.op)
                outer_const = self._extract_plain_int_const(node.right)
                if extracted is None or outer_const is None:
                    extracted = self._extract_nested_const(node.right, node.op)
                    outer_const = self._extract_plain_int_const(node.left)
                if extracted is None or outer_const is None:
                    return node

                base_expr, inner_const = extracted
                if not is_side_effect_free(base_expr):
                    return node

                combined = self._combine(node.op, inner_const, outer_const)
                if combined is None:
                    return node

                self._mark("combine_nested_bitwise_constants", node, node.op)
                return c_ast.BinaryOp(
                    node.op,
                    base_expr,
                    make_int_constant(combined, coord=node.coord),
                    coord=node.coord,
                )

            @staticmethod
            def _extract_plain_int_const(node):
                if not isinstance(node, c_ast.Constant):
                    return None
                return get_safe_int_value(node)

            def _extract_nested_const(self, node, op):
                if not isinstance(node, c_ast.BinaryOp) or node.op != op:
                    return None
                left_const = self._extract_plain_int_const(node.left)
                if left_const is not None and is_side_effect_free(node.right):
                    return copy.deepcopy(node.right), left_const
                right_const = self._extract_plain_int_const(node.right)
                if right_const is not None and is_side_effect_free(node.left):
                    return copy.deepcopy(node.left), right_const
                return None

            @staticmethod
            def _combine(op, left, right):
                if op == "&":
                    return left & right
                if op == "|":
                    return left | right
                if op == "^":
                    return left ^ right
                return None

        tx = _VectorCombiner()
        ast = tx.visit(ast)
        return ast if tx.changed else None


class TransformWarningPass(_ScannerPass):
    name = "transform-warning"

    def run(self, ast, ctx: PassContext):
        functions_with_blockers = 0
        blocker_hits = 0
        for node in getattr(ast, "ext", ()) or ():
            if not isinstance(node, c_ast.FuncDef):
                continue
            blockers = set()
            local_names = _function_local_names(node)
            for item in _walk(getattr(node, "body", None)):
                if isinstance(item, c_ast.Goto):
                    blockers.add("goto")
                elif isinstance(item, c_ast.Label):
                    blockers.add("label")
                elif isinstance(item, (c_ast.Switch, c_ast.Case, c_ast.Default)):
                    blockers.add("switch")
                elif _is_pragma_node(item):
                    blockers.add("pragma")
                elif isinstance(item, c_ast.FuncCall):
                    if not isinstance(item.name, c_ast.ID) or item.name.name in local_names:
                        blockers.add("indirect-call")
            if blockers:
                functions_with_blockers += 1
                blocker_hits += len(blockers)
                ctx.record(
                    self.name,
                    "blockers",
                    _func_name(node),
                    ",".join(sorted(blockers)),
                )
        ctx.bump("transform_warning.modules")
        ctx.bump("transform_warning.functions_with_blockers", functions_with_blockers)
        ctx.bump("transform_warning.blocker_kinds", blocker_hits)
        return None


class AnnotationRemarksPass(_ScannerPass):
    name = "annotation-remarks"

    def run(self, ast, ctx: PassContext):
        counts = Counter()
        for node in _walk(ast):
            if _is_pragma_node(node):
                counts["pragmas"] += 1
                ctx.record(
                    self.name,
                    "pragma",
                    _coord(node),
                    str(getattr(node, "string", "") or ""),
                )
                continue
            if isinstance(node, c_ast.PtrDecl) and "restrict" in tuple(getattr(node, "quals", ()) or ()):
                counts["restrict_pointers"] += 1
                ctx.record(self.name, "restrict", _coord(node))
                continue
            if not isinstance(node, c_ast.FuncCall) or not isinstance(node.name, c_ast.ID):
                continue
            callee = node.name.name
            if callee in {"__builtin_expect", "__builtin_expect_with_probability"}:
                counts["builtin_expect"] += 1
                ctx.record(self.name, "builtin_expect", _coord(node), callee)
            elif callee == "__builtin_assume_aligned":
                counts["builtin_assume_aligned"] += 1
                ctx.record(self.name, "builtin_assume_aligned", _coord(node))
            elif callee == "__builtin_constant_p":
                counts["builtin_constant_p"] += 1
                ctx.record(self.name, "builtin_constant_p", _coord(node))

        ctx.bump("annotation_remarks.modules")
        for name, count in sorted(counts.items()):
            ctx.bump(f"annotation_remarks.{name}", count)
        if counts:
            detail = " ".join(f"{name}={count}" for name, count in sorted(counts.items()))
            ctx.record(self.name, "summary", "module", detail)
        return None


class VerifyPass(ASTPass):
    """Cheap source-level structural verifier for the translated AST."""

    name = "verify"

    def run(self, ast, ctx: PassContext):
        node_count = 0
        for node in _walk(ast):
            node_count += 1
            if isinstance(node, c_ast.FuncDef):
                if getattr(node, "decl", None) is None or getattr(node, "body", None) is None:
                    raise ValueError("malformed FuncDef encountered during verify")
        ctx.bump("verify.nodes", node_count)
        ctx.record(self.name, "verified", "module", f"{node_count} nodes")
        return None


class ExtraSimpleLoopUnswitchPass(_ScannerPass):
    name = "extra-simple-loop-unswitch-passes"

    def run(self, ast, ctx: PassContext):
        from pcc.frontends.c.passes.llvm_loop_explicit import SimpleLoopUnswitchPass

        return SimpleLoopUnswitchPass().run(ast, ctx)


class MoveAutoInitPass(_ScannerPass):
    name = "move-auto-init"

    def run(self, ast, ctx: PassContext):
        class _AutoInitMover(ASTTransformer):
            def __init__(self):
                self.changed = False

            def _mark(self, action, node, detail=""):
                self.changed = True
                ctx.record("move-auto-init", action, _coord(node), detail)
                ctx.bump(f"move_auto_init.{action}")

            def visit_Compound(self, node):
                if not node.block_items:
                    return node

                items = [self.visit(item) for item in node.block_items]
                if any(
                    contains_node_type(item, (c_ast.Case, c_ast.Default))
                    for item in items
                ):
                    node.block_items = items
                    return node
                rewritten = []
                index = 0
                while index < len(items):
                    current = items[index]
                    if (
                        index + 1 < len(items)
                        and self._can_merge(current, items[index + 1])
                    ):
                        decl = current
                        assign = items[index + 1]
                        decl.init = copy.deepcopy(assign.rvalue)
                        self._mark("merge_decl_store", decl, decl.name)
                        rewritten.append(decl)
                        index += 2
                        continue
                    rewritten.append(current)
                    index += 1
                node.block_items = rewritten
                return node

            @staticmethod
            def _can_merge(decl, assign) -> bool:
                if not isinstance(decl, c_ast.Decl) or getattr(decl, "init", None) is not None:
                    return False
                if getattr(decl, "storage", None):
                    return False
                if not decl.name:
                    return False
                if not isinstance(assign, c_ast.Assignment) or assign.op != "=":
                    return False
                if not isinstance(assign.lvalue, c_ast.ID) or assign.lvalue.name != decl.name:
                    return False
                if decl.name in collect_ids(assign.rvalue):
                    return False
                if not is_side_effect_free(assign.rvalue):
                    return False
                decl_type = getattr(decl, "type", None)
                if isinstance(decl_type, (c_ast.ArrayDecl, c_ast.FuncDecl)):
                    return False
                return True

        tx = _AutoInitMover()
        ast = tx.visit(ast)
        return ast if tx.changed else None


class SLPVectorizerPass(_ScannerPass):
    name = "slp-vectorizer"

    def run(self, ast, ctx: PassContext):
        group_count = 0
        lane_count = 0
        for node in _walk(ast):
            if not isinstance(node, c_ast.Compound) or not node.block_items:
                continue

            streak = []
            last_key = None
            for item in list(node.block_items or ()) + [None]:
                key = self._stmt_key(item)
                if key is not None and key == last_key:
                    streak.append(item)
                else:
                    if len(streak) >= 2:
                        group_count += 1
                        lane_count += len(streak)
                        ctx.record(
                            self.name,
                            "group",
                            _coord(streak[0]),
                            f"lanes={len(streak)} op={last_key}",
                        )
                    streak = [item] if key is not None else []
                last_key = key

        ctx.bump("slp_vectorizer.modules")
        ctx.bump("slp_vectorizer.groups", group_count)
        ctx.bump("slp_vectorizer.lanes", lane_count)
        return None

    @staticmethod
    def _stmt_key(stmt):
        if not isinstance(stmt, c_ast.Assignment):
            return None
        if stmt.op != "=" or not isinstance(stmt.rvalue, c_ast.BinaryOp):
            return None
        if not is_side_effect_free(stmt.rvalue):
            return None
        lhs = stmt.lvalue
        if not isinstance(lhs, (c_ast.ID, c_ast.ArrayRef)):
            return None
        return stmt.rvalue.op


class _DivRemPairAnalyzer(ASTPass):
    name = "div-rem-pairs"

    def run(self, ast, ctx: PassContext):
        class _DivRemPairTransformer(ASTTransformer):
            _SAFE_INT_TYPES = {
                ("int",),
                ("signed", "int"),
                ("unsigned", "int"),
                ("long",),
                ("signed", "long"),
                ("unsigned", "long"),
                ("long", "long"),
                ("signed", "long", "long"),
                ("unsigned", "long", "long"),
            }

            def __init__(self):
                self.changed = False
                self._scopes = []

            def _mark(self, action, node, detail=""):
                self.changed = True
                ctx.record("div-rem-pairs", action, _coord(node), detail)
                ctx.bump(f"div_rem_pairs.{action}")

            def visit_FileAST(self, node):
                self._scopes.append(self._collect_decl_types(node.ext or ()))
                self._visit_children(node)
                self._scopes.pop()
                return node

            def visit_FuncDef(self, node):
                params = {}
                ftype = getattr(getattr(node, "decl", None), "type", None)
                for param in getattr(getattr(ftype, "args", None), "params", None) or ():
                    if isinstance(param, c_ast.Decl) and getattr(param, "name", None):
                        key = self._decl_type_key(param)
                        if key is not None:
                            params[param.name] = key
                self._scopes.append(params)
                self._visit_children(node)
                self._scopes.pop()
                return node

            def visit_Compound(self, node):
                local_scope = self._collect_decl_types(node.block_items or ())
                self._scopes.append(local_scope)
                self._visit_children(node)
                if not node.block_items:
                    self._scopes.pop()
                    return node

                items = list(node.block_items or ())
                rewritten = []
                index = 0
                while index < len(items):
                    if index + 1 < len(items):
                        replacement = self._rewrite_div_rem_pair(items[index], items[index + 1])
                        if replacement is not None:
                            rewritten.extend(replacement)
                            index += 2
                            continue
                    rewritten.append(items[index])
                    index += 1

                node.block_items = rewritten
                self._scopes.pop()
                return node

            def _rewrite_div_rem_pair(self, first, second):
                div_info = self._extract_store(first, "/")
                rem_info = self._extract_store(second, "%")
                if div_info is None or rem_info is None:
                    return None

                if (
                    div_info["left_key"] != rem_info["left_key"]
                    or div_info["right_key"] != rem_info["right_key"]
                ):
                    return None
                if not (
                    is_side_effect_free(div_info["expr"].left)
                    and is_side_effect_free(div_info["expr"].right)
                ):
                    return None

                q_name = div_info["target_name"]
                q_type = div_info["target_type"]
                if q_type not in self._SAFE_INT_TYPES:
                    return None
                if q_name in collect_ids(div_info["expr"].left) or q_name in collect_ids(div_info["expr"].right):
                    return None
                if not self._expr_types_match_target(div_info["expr"].left, q_type):
                    return None
                if not self._expr_types_match_target(div_info["expr"].right, q_type):
                    return None

                new_second = copy.deepcopy(second)
                replacement_expr = c_ast.BinaryOp(
                    "-",
                    copy.deepcopy(div_info["expr"].left),
                    c_ast.BinaryOp(
                        "*",
                        c_ast.ID(q_name, coord=getattr(div_info["target_node"], "coord", None)),
                        copy.deepcopy(div_info["expr"].right),
                        coord=getattr(rem_info["expr"], "coord", None),
                    ),
                    coord=getattr(rem_info["expr"], "coord", None),
                )
                self._assign_store_expr(new_second, replacement_expr)
                self._mark("share_div_result", second, q_name)
                return [first, new_second]

            def _extract_store(self, stmt, op):
                if isinstance(stmt, c_ast.Decl):
                    if getattr(stmt, "init", None) is None or not stmt.name:
                        return None
                    expr = stmt.init
                    target_type = self._decl_type_key(stmt)
                    target_node = stmt
                    target_name = stmt.name
                elif (
                    isinstance(stmt, c_ast.Assignment)
                    and stmt.op == "="
                    and isinstance(stmt.lvalue, c_ast.ID)
                ):
                    expr = stmt.rvalue
                    target_name = stmt.lvalue.name
                    target_type = self._lookup_type(target_name)
                    target_node = stmt.lvalue
                else:
                    return None

                if not isinstance(expr, c_ast.BinaryOp) or expr.op != op:
                    return None
                if target_type is None:
                    return None
                return {
                    "stmt": stmt,
                    "expr": expr,
                    "target_name": target_name,
                    "target_type": target_type,
                    "target_node": target_node,
                    "left_key": _expr_key(expr.left),
                    "right_key": _expr_key(expr.right),
                }

            @staticmethod
            def _assign_store_expr(stmt, expr):
                if isinstance(stmt, c_ast.Decl):
                    stmt.init = expr
                else:
                    stmt.rvalue = expr

            def _lookup_type(self, name):
                for scope in reversed(self._scopes):
                    if name in scope:
                        return scope[name]
                return None

            def _expr_types_match_target(self, expr, target_type):
                if isinstance(expr, c_ast.Constant):
                    return True
                if isinstance(expr, c_ast.ID):
                    return self._lookup_type(expr.name) == target_type
                return False

            @staticmethod
            def _collect_decl_types(items):
                result = {}
                for item in items:
                    if isinstance(item, c_ast.Decl) and getattr(item, "name", None):
                        key = _DivRemPairTransformer._decl_type_key(item)
                        if key is not None:
                            result[item.name] = key
                return result

            @staticmethod
            def _decl_type_key(decl):
                dtype = getattr(decl, "type", None)
                while isinstance(dtype, c_ast.TypeDecl):
                    quals = tuple(getattr(dtype, "quals", ()) or ())
                    if quals:
                        return None
                    dtype = dtype.type
                if not isinstance(dtype, c_ast.IdentifierType):
                    return None
                return tuple(dtype.names or ())

        tx = _DivRemPairTransformer()
        ast = tx.visit(ast)
        if tx.changed:
            return ast

        pairs = 0
        for node in _walk(ast):
            if not isinstance(node, c_ast.Compound):
                continue
            divs = set()
            rems = set()
            for item in node.block_items or ():
                for expr in _walk(item):
                    if not isinstance(expr, c_ast.BinaryOp) or expr.op not in ("/", "%"):
                        continue
                    if not (
                        is_side_effect_free(expr.left) and is_side_effect_free(expr.right)
                    ):
                        continue
                    key = (_expr_key(expr.left), _expr_key(expr.right))
                    if expr.op == "/":
                        divs.add(key)
                    else:
                        rems.add(key)
            pairs += len(divs & rems)
        if not pairs:
            ctx.bump("div-rem-pairs.modules")
            return None
        ctx.bump("div_rem_pairs.opportunities", pairs)
        ctx.record(self.name, "analysis", "module", f"{pairs} div/rem pair(s)")
        return None


class DivRemPairsPass(_DivRemPairAnalyzer):
    pass


class ConstMergePass(_ScannerPass):
    name = "constmerge"

    def run(self, ast, ctx: PassContext):
        if not isinstance(ast, c_ast.FileAST):
            return None

        global_names = [
            ext.name for ext in (ast.ext or ())
            if isinstance(ext, c_ast.Decl) and getattr(ext, "name", None)
        ]
        global_counts = Counter(global_names)

        shadowed_names = set()
        for ext in ast.ext or ():
            if not isinstance(ext, c_ast.FuncDef):
                continue
            decl = getattr(ext, "decl", None)
            ftype = getattr(decl, "type", None)
            args = getattr(getattr(ftype, "args", None), "params", None) or ()
            for param in args:
                if isinstance(param, c_ast.Decl) and getattr(param, "name", None):
                    shadowed_names.add(param.name)
            for node in _walk(ext.body):
                if isinstance(node, c_ast.Decl) and getattr(node, "name", None):
                    shadowed_names.add(node.name)

        address_taken = set()
        for node in _walk(ast):
            if (
                isinstance(node, c_ast.UnaryOp)
                and node.op == "&"
                and isinstance(node.expr, c_ast.ID)
            ):
                address_taken.add(node.expr.name)

        rename_map = {}
        drop_names = set()
        seen = {}
        for ext in ast.ext or ():
            if not self._is_mergeable_const_decl(
                ext,
                global_counts=global_counts,
                shadowed_names=shadowed_names,
                address_taken=address_taken,
            ):
                continue
            key = (_type_key(ext.type), _expr_key(ext.init))
            canonical = seen.get(key)
            if canonical is None:
                seen[key] = ext.name
                continue
            rename_map[ext.name] = canonical
            drop_names.add(ext.name)
            ctx.record("constmerge", "merge_const", _coord(ext), f"{ext.name}->{canonical}")
            ctx.bump("constmerge.merged")

        if not rename_map:
            ctx.bump("constmerge.modules")
            return None

        class _ConstUseRewriter(ASTTransformer):
            def visit_ID(self, node):
                new_name = rename_map.get(node.name)
                if new_name is None:
                    return node
                return c_ast.ID(new_name, coord=node.coord)

        ast.ext = [
            ext for ext in (ast.ext or ())
            if not (isinstance(ext, c_ast.Decl) and getattr(ext, "name", None) in drop_names)
        ]
        ast = _ConstUseRewriter().visit(ast)
        return ast

    @staticmethod
    def _is_mergeable_const_decl(ext, *, global_counts, shadowed_names, address_taken) -> bool:
        if not isinstance(ext, c_ast.Decl):
            return False
        name = getattr(ext, "name", None)
        if not name or global_counts.get(name, 0) != 1:
            return False
        if getattr(ext, "init", None) is None:
            return False
        if name in shadowed_names or name in address_taken:
            return False
        if "static" not in tuple(getattr(ext, "storage", ()) or ()):
            return False
        if not ConstMergePass._decl_is_const_scalar(ext):
            return False
        return _expr_key(ext.init) is not None

    @staticmethod
    def _decl_is_const_scalar(decl) -> bool:
        if "const" not in tuple(getattr(decl, "quals", ()) or ()):
            decl_type = getattr(decl, "type", None)
            if not (
                isinstance(decl_type, c_ast.TypeDecl)
                and "const" in tuple(getattr(decl_type, "quals", ()) or ())
            ):
                return False

        decl_type = getattr(decl, "type", None)
        while isinstance(decl_type, c_ast.TypeDecl):
            decl_type = decl_type.type
        return isinstance(decl_type, c_ast.IdentifierType)


class CGProfilePass(_ScannerPass):
    name = "cg-profile"

    def run(self, ast, ctx: PassContext):
        direct_calls = 0
        indirect_calls = 0
        recursive_calls = 0
        internal_calls = 0
        defined = {
            _func_name(node)
            for node in getattr(ast, "ext", ()) or ()
            if isinstance(node, c_ast.FuncDef)
        }

        for node in getattr(ast, "ext", ()) or ():
            if not isinstance(node, c_ast.FuncDef):
                continue
            caller = _func_name(node)
            local_names = _function_local_names(node)
            for item in _walk(getattr(node, "body", None)):
                if not isinstance(item, c_ast.FuncCall):
                    continue
                if isinstance(item.name, c_ast.ID) and item.name.name not in local_names:
                    callee = item.name.name
                    direct_calls += 1
                    if callee in defined:
                        internal_calls += 1
                    if callee == caller:
                        recursive_calls += 1
                    ctx.record(self.name, "edge", caller, callee)
                else:
                    indirect_calls += 1
                    ctx.record(self.name, "indirect", caller, _coord(item))

        ctx.bump("cg_profile.modules")
        ctx.bump("cg_profile.functions_seen", len(defined))
        ctx.bump("cg_profile.direct_calls", direct_calls)
        ctx.bump("cg_profile.internal_calls", internal_calls)
        ctx.bump("cg_profile.indirect_calls", indirect_calls)
        ctx.bump("cg_profile.recursive_calls", recursive_calls)
        return None


class RelLookupTableConverterPass(_ScannerPass):
    name = "rel-lookup-table-converter"

    def run(self, ast, ctx: PassContext):
        candidates = 0
        entries = 0
        for decl in _iter_global_object_decls(ast):
            if not isinstance(getattr(decl, "type", None), c_ast.ArrayDecl):
                continue
            init = getattr(decl, "init", None)
            if not isinstance(init, c_ast.InitList):
                continue
            exprs = list(init.exprs or ())
            if len(exprs) < 2:
                continue
            if not all(_is_pointer_like_lookup_expr(expr) for expr in exprs):
                continue
            candidates += 1
            entries += len(exprs)
            ctx.record(
                self.name,
                "candidate",
                _coord(decl),
                f"{decl.name}[{len(exprs)}]",
            )
        ctx.bump("rel_lookup_table_converter.modules")
        ctx.bump("rel_lookup_table_converter.candidates", candidates)
        ctx.bump("rel_lookup_table_converter.entries", entries)
        return None


class CHRPass(_ScannerPass):
    name = "chr"

    def run(self, ast, ctx: PassContext):
        class _CHRTransformer(ASTTransformer):
            def __init__(self):
                self.changed = False

            def _mark(self, action, node, detail=""):
                self.changed = True
                ctx.record("chr", action, _coord(node), detail)
                ctx.bump(f"chr.{action}")

            def visit_Compound(self, node):
                self._visit_children(node)
                if not node.block_items:
                    return node

                rewritten = []
                for item in node.block_items:
                    replacement = self._rewrite_item(item)
                    if replacement is None:
                        rewritten.append(item)
                    else:
                        rewritten.extend(replacement)
                node.block_items = rewritten
                return node

            def _rewrite_item(self, item):
                if not isinstance(item, c_ast.If) or item.iffalse is None:
                    return None

                original_true_items = self._stmt_list(item.iftrue)
                original_false_items = self._stmt_list(item.iffalse)
                true_items = list(original_true_items)
                false_items = list(original_false_items)
                if len(true_items) < 2 or len(false_items) < 2:
                    return None

                common_tail = []
                while (
                    true_items
                    and false_items
                    and self._same_hoistable_stmt(true_items[-1], false_items[-1])
                ):
                    common_tail.append(copy.deepcopy(true_items.pop()))
                    false_items.pop()

                if not common_tail:
                    return None
                if self._has_unsafe_prefix_flow(true_items) or self._has_unsafe_prefix_flow(false_items):
                    return None
                branch_declared = self._declared_names_in_items(original_true_items)
                branch_declared.update(self._declared_names_in_items(original_false_items))
                tail_used = self._used_names_in_items(common_tail)
                if tail_used.intersection(branch_declared):
                    return None

                common_tail.reverse()
                new_if = copy.deepcopy(item)
                new_if.iftrue = self._rebuild_branch(true_items, item.iftrue)
                new_if.iffalse = self._rebuild_branch(false_items, item.iffalse)
                self._mark("factor_common_tail", item, f"count={len(common_tail)}")
                return [new_if, *common_tail]

            @staticmethod
            def _stmt_list(stmt):
                if isinstance(stmt, c_ast.Compound):
                    return list(stmt.block_items or ())
                if stmt is None:
                    return []
                return [stmt]

            @staticmethod
            def _rebuild_branch(items, original):
                if not items:
                    return c_ast.EmptyStatement(coord=getattr(original, "coord", None))
                if isinstance(original, c_ast.Compound) or len(items) > 1 or any(
                    isinstance(item, c_ast.Decl) for item in items
                ):
                    return c_ast.Compound(
                        block_items=list(items),
                        coord=getattr(original, "coord", None),
                    )
                return items[0]

            @staticmethod
            def _same_hoistable_stmt(left, right):
                if type(left) is not type(right):
                    return False
                if isinstance(left, c_ast.Return):
                    return _expr_key(left.expr) == _expr_key(right.expr)
                if isinstance(left, c_ast.Assignment):
                    return (
                        left.op == right.op == "="
                        and _expr_key(left.lvalue) == _expr_key(right.lvalue)
                        and _expr_key(left.rvalue) == _expr_key(right.rvalue)
                    )
                if isinstance(left, c_ast.EmptyStatement):
                    return True
                return False

            @staticmethod
            def _has_unsafe_prefix_flow(items):
                risky = (
                    c_ast.Goto,
                    c_ast.Label,
                    c_ast.Case,
                    c_ast.Default,
                    c_ast.Break,
                    c_ast.Continue,
                )
                return any(contains_node_type(item, risky) for item in items)

            @staticmethod
            def _declared_names_in_items(items):
                names = set()
                for item in items:
                    for node in _walk(item):
                        if isinstance(node, c_ast.Decl) and getattr(node, "name", None):
                            names.add(node.name)
                return names

            @staticmethod
            def _used_names_in_items(items):
                names = set()
                for item in items:
                    names.update(collect_ids(item))
                return names

        tx = _CHRTransformer()
        ast = tx.visit(ast)
        return ast if tx.changed else None


def preanalysis_explicit_llvm_passes() -> tuple[ASTPass, ...]:
    """Return explicit LLVM leaf passes that should run before AST transforms."""
    return (
        CoroEarlyPass(),
        EEInstrumentPass(),
        OpenMPOptPass(),
        RequirePass(),
        InvalidatePass(),
    )


def precanonicalize_explicit_llvm_passes() -> tuple[ASTPass, ...]:
    """Return AST lowerings that should run before canonicalization."""
    return (
        LowerConstantIntrinsicsPass(),
        FloatToIntPass(),
        AlignmentFromAssumptionsPass(),
    )


def late_explicit_llvm_passes() -> tuple[ASTPass, ...]:
    """Return later explicit LLVM leaf passes for the default pipeline."""
    return (
        OpenMPCGSCCPass(),
        LibcallsShrinkwrapPass(),
        ExtraSimpleLoopUnswitchPass(),
        CoroElidePass(),
        CoroSplitPass(),
        CoroAnnotationElidePass(),
        RecomputeGlobalsAAPass(),
        CoroCleanupPass(),
        LoopDistributePass(),
        InjectTLIMappingsPass(),
        LoopVectorizePass(),
        VectorCombinePass(),
        TransformWarningPass(),
        AnnotationRemarksPass(),
        VerifyPass(),
        MoveAutoInitPass(),
        SLPVectorizerPass(),
        DivRemPairsPass(),
        ConstMergePass(),
        CGProfilePass(),
        RelLookupTableConverterPass(),
        CHRPass(),
    )
