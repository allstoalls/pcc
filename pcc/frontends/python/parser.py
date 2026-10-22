"""pcc Python frontend parser.

Parses Python source text into a pcc_py ``Module`` AST node. Uses the
stdlib ``ast`` module as the tokenizer + parser backbone and lifts
``ast.AST`` nodes into the frozen dataclasses defined in ``py_ast``.

Every node carries a :class:`SourceSpan`. ``ty`` fields on expressions
default to :class:`DynType("dyn")` at parse time; a later type-inference
pass assigns concrete types.

Supports Python 3.11+ syntax. ``match``/``case`` is intentionally not
handled.
"""
from __future__ import annotations

# NOTE: this module is the CPython-ast-backed fallback path. The
# self-host default is ``pcc.frontends.python.py_parse`` + ``pcc.frontends.python.py_lift``
# (see pipeline.py's PCC_NATIVE_PARSER gate). Once PCC_NATIVE_PARSER
# becomes the hard default, this file can be deleted wholesale.
import ast as _py_ast
from typing import Optional

from pcc.frontends.python import py_ast as pa


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------

def parse(src: str, filename: str) -> pa.Module:
    """Parse Python source text into a pcc_py Module AST node.

    Args:
        src: Python source code.
        filename: Path to the source file (used in diagnostics / SourceSpan).

    Returns:
        A :class:`pa.Module` wrapping the lifted body.

    Raises:
        SyntaxError: If the stdlib ``ast`` parser rejects the source.
    """
    tree = _py_ast.parse(src, filename=filename, type_comments=True)
    lifter = _Lifter(filename)
    return lifter.lift_module(tree, module_name=_module_name_from_filename(filename))


def _module_name_from_filename(filename: str) -> str:
    """Derive a module name from a filename (drop extension + directories)."""
    base = filename.rsplit("/", 1)[-1]
    if base.endswith(".py"):
        base = base[:-3]
    return base or "<module>"


# ---------------------------------------------------------------------------
# Lifter — one instance per parse() call; holds the filename for spans
# ---------------------------------------------------------------------------

# Default placeholder type for freshly-parsed expressions. Inference replaces.
_DYN: pa.Type = pa.DynType(name="dyn")


# Maps ast.AST operator / unaryop / cmpop / boolop nodes to contract strings.
_BINOP_MAP: dict[type, str] = {
    _py_ast.Add: "+",
    _py_ast.Sub: "-",
    _py_ast.Mult: "*",
    _py_ast.Div: "/",
    _py_ast.FloorDiv: "//",
    _py_ast.Mod: "%",
    _py_ast.Pow: "**",
    _py_ast.BitAnd: "&",
    _py_ast.BitOr: "|",
    _py_ast.BitXor: "^",
    _py_ast.LShift: "<<",
    _py_ast.RShift: ">>",
    _py_ast.MatMult: "@",
}

_UNARYOP_MAP: dict[type, str] = {
    _py_ast.UAdd: "+",
    _py_ast.USub: "-",
    _py_ast.Invert: "~",
    _py_ast.Not: "not",
}

_CMPOP_MAP: dict[type, str] = {
    _py_ast.Eq: "==",
    _py_ast.NotEq: "!=",
    _py_ast.Lt: "<",
    _py_ast.LtE: "<=",
    _py_ast.Gt: ">",
    _py_ast.GtE: ">=",
    _py_ast.Is: "is",
    _py_ast.IsNot: "is not",
    _py_ast.In: "in",
    _py_ast.NotIn: "not in",
}

_BOOLOP_MAP: dict[type, str] = {
    _py_ast.And: "and",
    _py_ast.Or: "or",
}

# AugAssign operator → contract string (with trailing "=").
_AUGOP_MAP: dict[type, str] = {k: v + "=" for k, v in _BINOP_MAP.items()}


class _Lifter:
    """Converts stdlib ast.AST nodes into pcc_py AST nodes."""

    def __init__(self, filename: str) -> None:
        self.filename = filename

    # -- span ------------------------------------------------------------

    def _span(self, node: _py_ast.AST) -> pa.SourceSpan:
        """Build a SourceSpan from an ast.AST node's line/col info."""
        line = getattr(node, "lineno", 0) or 0
        col = getattr(node, "col_offset", 0) or 0
        end_line = getattr(node, "end_lineno", None)
        end_col = getattr(node, "end_col_offset", None)
        if end_line is None:
            end_line = line
        if end_col is None:
            end_col = col
        return pa.SourceSpan(
            file=self.filename,
            line=line,
            col=col,
            end_line=end_line,
            end_col=end_col,
        )

    # -- Module ----------------------------------------------------------

    def lift_module(self, node: _py_ast.Module, module_name: str) -> pa.Module:
        """Lift an ast.Module into a pa.Module."""
        body_stmts = tuple(self.lift_stmt(s) for s in node.body)
        docstring = _py_ast.get_docstring(node)
        return pa.Module(name=module_name, body=body_stmts, docstring=docstring)

    # -- Statements ------------------------------------------------------

    def lift_stmt(self, node: _py_ast.stmt) -> pa.Stmt:
        """Dispatch on the ast.stmt kind."""
        method = _STMT_DISPATCH.get(type(node))
        if method is None:
            raise NotImplementedError(
                f"{self.filename}:{getattr(node, 'lineno', 0)}: "
                f"unsupported statement: {type(node).__name__}"
            )
        return method(self, node)

    def _stmt_FunctionDef(self, node: _py_ast.FunctionDef) -> pa.FuncDef:
        return self._lift_funcdef(node, is_async=False)

    def _stmt_AsyncFunctionDef(self, node: _py_ast.AsyncFunctionDef) -> pa.FuncDef:
        return self._lift_funcdef(node, is_async=True)

    def _lift_funcdef(
        self,
        node: _py_ast.FunctionDef | _py_ast.AsyncFunctionDef,
        *,
        is_async: bool,
    ) -> pa.FuncDef:
        args = self._lift_arguments(node.args)
        return_ty: Optional[pa.Type] = (
            self._lift_annotation(node.returns) if node.returns is not None else None
        )
        body_stmts = tuple(self.lift_stmt(s) for s in node.body)
        decorators = tuple(self.lift_expr(d) for d in node.decorator_list)
        return pa.FuncDef(
            span=self._span(node),
            name=node.name,
            args=args,
            return_ty=return_ty,
            body=body_stmts,
            decorators=decorators,
            is_method=False,   # set by enclosing ClassDef in a later pass
            is_async=is_async,
            has_return_annotation=node.returns is not None,
        )

    def _stmt_ClassDef(self, node: _py_ast.ClassDef) -> pa.ClassDef:
        bases = tuple(self.lift_expr(b) for b in node.bases)
        keywords: tuple[tuple[str, pa.Expr], ...] = tuple(
            (kw.arg, self.lift_expr(kw.value))
            for kw in node.keywords
            if kw.arg is not None
        )
        body_stmts = tuple(self.lift_stmt(s) for s in node.body)
        decorators = tuple(self.lift_expr(d) for d in node.decorator_list)
        return pa.ClassDef(
            span=self._span(node),
            name=node.name,
            bases=bases,
            keywords=keywords,
            body=body_stmts,
            decorators=decorators,
        )

    def _stmt_Return(self, node: _py_ast.Return) -> pa.Return:
        value = self.lift_expr(node.value) if node.value is not None else None
        return pa.Return(span=self._span(node), value=value)

    def _stmt_Delete(self, node: _py_ast.Delete) -> pa.Delete:
        targets = tuple(self.lift_expr(t) for t in node.targets)
        return pa.Delete(span=self._span(node), targets=targets)

    def _stmt_Assign(self, node: _py_ast.Assign) -> pa.Assign:
        targets = tuple(self.lift_expr(t) for t in node.targets)
        value = self.lift_expr(node.value)
        return pa.Assign(
            span=self._span(node),
            targets=targets,
            value=value,
            annotation=None,
        )

    def _stmt_AugAssign(self, node: _py_ast.AugAssign) -> pa.AugAssign:
        op = _AUGOP_MAP.get(type(node.op))
        if op is None:
            raise NotImplementedError(
                f"{self.filename}:{node.lineno}: "
                f"unsupported augmented op: {type(node.op).__name__}"
            )
        target = self.lift_expr(node.target)
        value = self.lift_expr(node.value)
        return pa.AugAssign(
            span=self._span(node),
            target=target,
            op=op,
            value=value,
        )

    def _stmt_AnnAssign(self, node: _py_ast.AnnAssign) -> pa.Assign:
        """``x: T = v`` — treat as Assign with annotation populated.

        An AnnAssign without a value (``x: T``) still emits an Assign with
        an inert ``NoneLit`` placeholder and ``has_value=False``. Explicit
        ``= None`` retains ``has_value=True``.
        """
        annotation = self._lift_annotation(node.annotation)
        target = self.lift_expr(node.target)
        if node.value is not None:
            value = self.lift_expr(node.value)
        else:
            # Declaration-only annotation; synthesize a NoneLit with span from target.
            value = pa.NoneLit(span=self._span(node), ty=_DYN)
        return pa.Assign(
            span=self._span(node),
            targets=(target,),
            value=value,
            annotation=annotation,
            has_value=node.value is not None,
        )

    def _stmt_For(self, node: _py_ast.For) -> pa.For:
        target = self.lift_expr(node.target)
        iter_e = self.lift_expr(node.iter)
        body_stmts = tuple(self.lift_stmt(s) for s in node.body)
        else_stmts = tuple(self.lift_stmt(s) for s in node.orelse)
        return pa.For(
            span=self._span(node),
            target=target,
            iter=iter_e,
            body=body_stmts,
            else_body=else_stmts,
        )

    def _stmt_AsyncFor(self, node: _py_ast.AsyncFor) -> pa.For:
        target = self.lift_expr(node.target)
        iter_e = self.lift_expr(node.iter)
        body_stmts = tuple(self.lift_stmt(s) for s in node.body)
        else_stmts = tuple(self.lift_stmt(s) for s in node.orelse)
        return pa.For(
            span=self._span(node),
            target=target,
            iter=iter_e,
            body=body_stmts,
            else_body=else_stmts,
            is_async=True,
        )

    def _stmt_While(self, node: _py_ast.While) -> pa.While:
        cond = self.lift_expr(node.test)
        body_stmts = tuple(self.lift_stmt(s) for s in node.body)
        else_stmts = tuple(self.lift_stmt(s) for s in node.orelse)
        return pa.While(
            span=self._span(node),
            cond=cond,
            body=body_stmts,
            else_body=else_stmts,
        )

    def _stmt_If(self, node: _py_ast.If) -> pa.If:
        cond = self.lift_expr(node.test)
        body_stmts = tuple(self.lift_stmt(s) for s in node.body)
        else_stmts = tuple(self.lift_stmt(s) for s in node.orelse)
        return pa.If(
            span=self._span(node),
            cond=cond,
            body=body_stmts,
            else_body=else_stmts,
        )

    def _stmt_With(self, node: _py_ast.With) -> pa.With:
        items = tuple(self._lift_with_item(it) for it in node.items)
        body_stmts = tuple(self.lift_stmt(s) for s in node.body)
        return pa.With(
            span=self._span(node),
            items=items,
            body=body_stmts,
        )

    def _stmt_AsyncWith(self, node: _py_ast.AsyncWith) -> pa.With:
        items = tuple(self._lift_with_item(it) for it in node.items)
        body_stmts = tuple(self.lift_stmt(s) for s in node.body)
        return pa.With(
            span=self._span(node),
            items=items,
            body=body_stmts,
            is_async=True,
        )

    def _lift_with_item(
        self, item: _py_ast.withitem
    ) -> tuple[pa.Expr, Optional[pa.Expr]]:
        ctx = self.lift_expr(item.context_expr)
        as_var: Optional[pa.Expr] = (
            self.lift_expr(item.optional_vars)
            if item.optional_vars is not None
            else None
        )
        return (ctx, as_var)

    def _stmt_Raise(self, node: _py_ast.Raise) -> pa.Raise:
        exc = self.lift_expr(node.exc) if node.exc is not None else None
        cause = self.lift_expr(node.cause) if node.cause is not None else None
        return pa.Raise(span=self._span(node), exc=exc, cause=cause)

    def _stmt_Try(self, node: _py_ast.Try) -> pa.Try:
        body_stmts = tuple(self.lift_stmt(s) for s in node.body)
        handlers = tuple(self._lift_except_handler(h) for h in node.handlers)
        else_stmts = tuple(self.lift_stmt(s) for s in node.orelse)
        finally_stmts = tuple(self.lift_stmt(s) for s in node.finalbody)
        return pa.Try(
            span=self._span(node),
            body=body_stmts,
            handlers=handlers,
            else_body=else_stmts,
            finally_body=finally_stmts,
        )

    def _stmt_TryStar(self, node: _py_ast.AST) -> pa.Try:
        """``try*`` (PEP 654, Python 3.11+). Lifted same as Try for now."""
        body_stmts = tuple(self.lift_stmt(s) for s in node.body)  # type: ignore[attr-defined]
        handlers = tuple(
            self._lift_except_handler(h) for h in node.handlers  # type: ignore[attr-defined]
        )
        else_stmts = tuple(self.lift_stmt(s) for s in node.orelse)  # type: ignore[attr-defined]
        finally_stmts = tuple(
            self.lift_stmt(s) for s in node.finalbody  # type: ignore[attr-defined]
        )
        return pa.Try(
            span=self._span(node),
            body=body_stmts,
            handlers=handlers,
            else_body=else_stmts,
            finally_body=finally_stmts,
        )

    def _lift_except_handler(
        self, node: _py_ast.ExceptHandler
    ) -> pa.ExceptHandler:
        exc_type = self.lift_expr(node.type) if node.type is not None else None
        name = node.name
        body_stmts = tuple(self.lift_stmt(s) for s in node.body)
        return pa.ExceptHandler(
            exc_type=exc_type,
            name=name,
            body=body_stmts,
            span=self._span(node),
        )

    def _stmt_Assert(self, node: _py_ast.Assert) -> pa.Stmt:
        """``assert cond, msg`` → lowered to ``if not cond: raise AssertionError(msg)``.

        The contract doesn't enumerate an Assert node. We desugar into the
        existing If + Raise primitives so downstream passes see only
        node types from the frozen contract.
        """
        span = self._span(node)
        # not cond
        cond = self.lift_expr(node.test)
        negated = pa.UnaryOp(span=span, ty=_DYN, op="not", operand=cond)
        # AssertionError(msg) or AssertionError()
        assertion_err_name = pa.Name(span=span, ty=_DYN, ident="AssertionError")
        if node.msg is not None:
            exc_expr: pa.Expr = pa.Call(
                span=span,
                ty=_DYN,
                func=assertion_err_name,
                args=(self.lift_expr(node.msg),),
                kwargs=(),
            )
        else:
            exc_expr = pa.Call(
                span=span,
                ty=_DYN,
                func=assertion_err_name,
                args=(),
                kwargs=(),
            )
        raise_stmt = pa.Raise(span=span, exc=exc_expr, cause=None)
        return pa.If(
            span=span,
            cond=negated,
            body=(raise_stmt,),
            else_body=(),
        )

    def _stmt_Import(self, node: _py_ast.Import) -> pa.Import:
        names: tuple[tuple[str, Optional[str]], ...] = tuple(
            (alias.name, alias.asname) for alias in node.names
        )
        return pa.Import(span=self._span(node), names=names)

    def _stmt_ImportFrom(self, node: _py_ast.ImportFrom) -> pa.ImportFrom:
        module = node.module or ""
        names: tuple[tuple[str, Optional[str]], ...] = tuple(
            (alias.name, alias.asname) for alias in node.names
        )
        level = node.level or 0
        return pa.ImportFrom(
            span=self._span(node),
            module=module,
            names=names,
            level=level,
        )

    def _stmt_Global(self, node: _py_ast.Global) -> pa.Global:
        return pa.Global(span=self._span(node), names=tuple(node.names))

    def _stmt_Nonlocal(self, node: _py_ast.Nonlocal) -> pa.Nonlocal:
        return pa.Nonlocal(span=self._span(node), names=tuple(node.names))

    def _stmt_Expr(self, node: _py_ast.Expr) -> pa.ExprStmt:
        return pa.ExprStmt(span=self._span(node), expr=self.lift_expr(node.value))

    def _stmt_Pass(self, node: _py_ast.Pass) -> pa.Pass:
        return pa.Pass(span=self._span(node))

    def _stmt_Break(self, node: _py_ast.Break) -> pa.Break:
        return pa.Break(span=self._span(node))

    def _stmt_Continue(self, node: _py_ast.Continue) -> pa.Continue:
        return pa.Continue(span=self._span(node))

    # -- Expressions -----------------------------------------------------

    def lift_expr(self, node: _py_ast.expr) -> pa.Expr:
        """Dispatch on the ast.expr kind."""
        method = _EXPR_DISPATCH.get(type(node))
        if method is None:
            raise NotImplementedError(
                f"{self.filename}:{getattr(node, 'lineno', 0)}: "
                f"unsupported expression: {type(node).__name__}"
            )
        return method(self, node)

    def _expr_Constant(self, node: _py_ast.Constant) -> pa.Expr:
        span = self._span(node)
        v = node.value
        # ORDER MATTERS: bool is a subclass of int in Python.
        if v is None:
            return pa.NoneLit(span=span, ty=_DYN)
        if isinstance(v, bool):
            return pa.BoolLit(span=span, ty=_DYN, value=v)
        if isinstance(v, int):
            return pa.IntLit(span=span, ty=_DYN, value=v)
        if isinstance(v, float):
            return pa.FloatLit(span=span, ty=_DYN, value=v)
        if isinstance(v, complex):
            return pa.ComplexLit(
                span=span, ty=_DYN,
                real=float(v.real), imag=float(v.imag),
            )
        if isinstance(v, str):
            return pa.StrLit(span=span, ty=_DYN, value=v)
        if isinstance(v, bytes):
            return pa.BytesLit(span=span, ty=_DYN, value=v)
        if v is Ellipsis:
            return pa.Name(span=span, ty=_DYN, ident="...")
        raise NotImplementedError(
            f"{self.filename}:{node.lineno}: "
            f"unsupported constant type: {type(v).__name__}"
        )

    def _expr_Name(self, node: _py_ast.Name) -> pa.Name:
        return pa.Name(span=self._span(node), ty=_DYN, ident=node.id)

    def _expr_BinOp(self, node: _py_ast.BinOp) -> pa.BinOp:
        op = _BINOP_MAP.get(type(node.op))
        if op is None:
            raise NotImplementedError(
                f"{self.filename}:{node.lineno}: "
                f"unsupported binop: {type(node.op).__name__}"
            )
        return pa.BinOp(
            span=self._span(node),
            ty=_DYN,
            op=op,
            lhs=self.lift_expr(node.left),
            rhs=self.lift_expr(node.right),
        )

    def _expr_UnaryOp(self, node: _py_ast.UnaryOp) -> pa.UnaryOp:
        op = _UNARYOP_MAP.get(type(node.op))
        if op is None:
            raise NotImplementedError(
                f"{self.filename}:{node.lineno}: "
                f"unsupported unaryop: {type(node.op).__name__}"
            )
        return pa.UnaryOp(
            span=self._span(node),
            ty=_DYN,
            op=op,
            operand=self.lift_expr(node.operand),
        )

    def _expr_BoolOp(self, node: _py_ast.BoolOp) -> pa.Expr:
        """``a and b and c`` — fold into right-associated BoolExpr chain."""
        op = _BOOLOP_MAP.get(type(node.op))
        if op is None:
            raise NotImplementedError(
                f"{self.filename}:{node.lineno}: "
                f"unsupported boolop: {type(node.op).__name__}"
            )
        span = self._span(node)
        values = [self.lift_expr(v) for v in node.values]
        return self._build_balanced_bool_expr(values, span, op)

    def _build_balanced_bool_expr(
        self,
        values: list[pa.Expr],
        span: pa.SourceSpan,
        op: str,
    ) -> pa.Expr:
        if len(values) == 1:
            return values[0]
        mid = len(values) // 2
        left = self._build_balanced_bool_expr(values[:mid], span, op)
        right = self._build_balanced_bool_expr(values[mid:], span, op)
        return pa.BoolExpr(
            span=span,
            ty=_DYN,
            op=op,
            left=left,
            right=right,
        )

    def _expr_Compare(self, node: _py_ast.Compare) -> pa.Expr:
        """``a < b < c`` → ``(a < b) and (b < c)`` as an ast-level fold.

        Python allows chained comparisons; we lower them to a chain of
        Compare nodes combined with BoolExpr("and"), matching Python
        short-circuit semantics. Each comparison step reuses the middle
        operand's expression (no re-evaluation).
        """
        span = self._span(node)
        left = self.lift_expr(node.left)
        ops_and_rights: list[tuple[str, pa.Expr]] = []
        for op_node, right_node in zip(node.ops, node.comparators):
            op = _CMPOP_MAP.get(type(op_node))
            if op is None:
                raise NotImplementedError(
                    f"{self.filename}:{node.lineno}: "
                    f"unsupported cmpop: {type(op_node).__name__}"
                )
            ops_and_rights.append((op, self.lift_expr(right_node)))

        # Single comparison — no chaining needed.
        if len(ops_and_rights) == 1:
            op, right = ops_and_rights[0]
            return pa.Compare(span=span, ty=_DYN, op=op, lhs=left, rhs=right)

        # Multi-step comparison: build right-associated and-chain of Compares.
        comparisons: list[pa.Compare] = []
        prev = left
        for op, right in ops_and_rights:
            comparisons.append(
                pa.Compare(span=span, ty=_DYN, op=op, lhs=prev, rhs=right)
            )
            prev = right
        # Right-fold into BoolExpr("and", ...).
        result: pa.Expr = comparisons[-1]
        i = len(comparisons) - 2
        while i >= 0:
            c = comparisons[i]
            result = pa.BoolExpr(
                span=span, ty=_DYN, op="and", left=c, right=result
            )
            i -= 1
        return result

    def _expr_Call(self, node: _py_ast.Call) -> pa.Call:
        func = self.lift_expr(node.func)
        args = tuple(self.lift_expr(a) for a in node.args)
        kwargs = tuple(
            (kw.arg, self.lift_expr(kw.value))
            for kw in node.keywords
            if kw.arg is not None
        )
        # ast.keyword with arg=None represents **kwargs at call site; drop
        # onto a synthetic "**" entry for downstream to recognize.
        splats = tuple(
            ("**", self.lift_expr(kw.value))
            for kw in node.keywords
            if kw.arg is None
        )
        # ``ast.Call`` stores positional/starred operands separately from
        # keyword/``**`` operands even when the source interleaves them (for
        # example ``f(named=v, *items)``).  Preserve the original source stream
        # explicitly so CPython-fallback lowering never reorders side effects.
        # ``kwargs + splats`` below groups the two keyword forms, so retain the
        # corresponding grouped index while sorting by each syntax node's
        # location.
        ordered_operands: list[tuple[int, int, str, int]] = []
        for index, arg in enumerate(node.args):
            ordered_operands.append(
                (arg.lineno, arg.col_offset, "arg", index)
            )
        explicit_index = 0
        splat_index = len(kwargs)
        for kw in node.keywords:
            if kw.arg is None:
                grouped_index = splat_index
                splat_index += 1
            else:
                grouped_index = explicit_index
                explicit_index += 1
            ordered_operands.append(
                (kw.value.lineno, kw.value.col_offset, "kw", grouped_index)
            )
        ordered_operands.sort(key=lambda item: (item[0], item[1]))
        return pa.Call(
            span=self._span(node),
            ty=_DYN,
            func=func,
            args=args,
            kwargs=kwargs + splats,
            operand_order=tuple(
                (kind, index)
                for _line, _column, kind, index in ordered_operands
            ),
        )

    def _expr_Attribute(self, node: _py_ast.Attribute) -> pa.Attr:
        return pa.Attr(
            span=self._span(node),
            ty=_DYN,
            obj=self.lift_expr(node.value),
            name=node.attr,
        )

    def _expr_Subscript(self, node: _py_ast.Subscript) -> pa.Subscript:
        return pa.Subscript(
            span=self._span(node),
            ty=_DYN,
            obj=self.lift_expr(node.value),
            idx=self.lift_expr(node.slice),
        )

    def _expr_Slice(self, node: _py_ast.Slice) -> pa.Slice:
        lo = self.lift_expr(node.lower) if node.lower is not None else None
        hi = self.lift_expr(node.upper) if node.upper is not None else None
        step = self.lift_expr(node.step) if node.step is not None else None
        return pa.Slice(
            span=self._span(node),
            ty=_DYN,
            lo=lo,
            hi=hi,
            step=step,
        )

    def _expr_List(self, node: _py_ast.List) -> pa.ListExpr:
        elems = tuple(self.lift_expr(e) for e in node.elts)
        return pa.ListExpr(span=self._span(node), ty=_DYN, elems=elems)

    def _expr_Tuple(self, node: _py_ast.Tuple) -> pa.TupleExpr:
        elems = tuple(self.lift_expr(e) for e in node.elts)
        return pa.TupleExpr(span=self._span(node), ty=_DYN, elems=elems)

    def _expr_Set(self, node: _py_ast.Set) -> pa.Call:
        """Set literal — lifted via ``set([...])`` for now.

        The syntax marker distinguishes this from an explicit ``set(list)``
        constructor and prevents resolving a user binding named ``set``.
        """
        span = self._span(node)
        elems = tuple(self.lift_expr(e) for e in node.elts)
        list_expr = pa.ListExpr(span=span, ty=_DYN, elems=elems)
        set_name = pa.Name(span=span, ty=_DYN, ident="set")
        return pa.Call(
            span=span,
            ty=_DYN,
            func=set_name,
            args=(list_expr,),
            kwargs=(),
            is_set_literal=True,
        )

    def _expr_Dict(self, node: _py_ast.Dict) -> pa.DictExpr:
        pairs: list[tuple[pa.Expr, pa.Expr]] = []
        span = self._span(node)
        for k_node, v_node in zip(node.keys, node.values):
            # k is None for **spread; represent with a Name("**") placeholder.
            if k_node is None:
                k_expr: pa.Expr = pa.Name(span=span, ty=_DYN, ident="**")
            else:
                k_expr = self.lift_expr(k_node)
            pairs.append((k_expr, self.lift_expr(v_node)))
        return pa.DictExpr(span=span, ty=_DYN, pairs=tuple(pairs))

    def _expr_IfExp(self, node: _py_ast.IfExp) -> pa.IfExpr:
        return pa.IfExpr(
            span=self._span(node),
            ty=_DYN,
            cond=self.lift_expr(node.test),
            then_e=self.lift_expr(node.body),
            else_e=self.lift_expr(node.orelse),
        )

    def _expr_Lambda(self, node: _py_ast.Lambda) -> pa.Lambda:
        args = self._lift_arguments(node.args)
        body = self.lift_expr(node.body)
        return pa.Lambda(
            span=self._span(node),
            ty=_DYN,
            params=args,
            body=body,
        )

    def _expr_Starred(self, node: _py_ast.Starred) -> pa.Expr:
        """``*x`` at an expression position — represented as a Call ``__starred__(x)``.

        The frozen contract has no Starred node; we wrap with a sentinel Name
        so downstream codegen can detect and handle.
        """
        span = self._span(node)
        sentinel = pa.Name(span=span, ty=_DYN, ident="__starred__")
        return pa.Call(
            span=span,
            ty=_DYN,
            func=sentinel,
            args=(self.lift_expr(node.value),),
            kwargs=(),
        )

    def _expr_JoinedStr(self, node: _py_ast.JoinedStr) -> pa.Expr:
        """f-string — lower to repeated str concatenation.

        Each piece is either a Constant (part of the format string) or a
        FormattedValue which we lower through the native format protocol.
        """
        span = self._span(node)
        pieces: list[pa.Expr] = []
        for piece in node.values:
            if isinstance(piece, _py_ast.Constant) and isinstance(piece.value, str):
                pieces.append(pa.StrLit(span=span, ty=_DYN, value=piece.value))
            elif isinstance(piece, _py_ast.FormattedValue):
                pieces.append(self._formatted_value_call(piece))
            else:
                pieces.append(self.lift_expr(piece))
        if not pieces:
            return pa.StrLit(span=span, ty=_DYN, value="")
        result = pieces[0]
        for p in pieces[1:]:
            result = pa.BinOp(span=span, ty=_DYN, op="+", lhs=result, rhs=p)
        return result

    def _expr_FormattedValue(self, node: _py_ast.FormattedValue) -> pa.Expr:
        """Bare FormattedValue (rare outside JoinedStr) → format protocol."""
        return self._formatted_value_call(node)

    def _literal_format_spec(self, node: _py_ast.AST | None) -> str:
        if node is None:
            return ""
        if isinstance(node, _py_ast.JoinedStr):
            parts: list[str] = []
            for piece in node.values:
                if isinstance(piece, _py_ast.Constant) and isinstance(piece.value, str):
                    parts.append(piece.value)
                    continue
                return ""
            return "".join(parts)
        if isinstance(node, _py_ast.Constant) and isinstance(node.value, str):
            return node.value
        return ""

    def _formatted_value_call(self, node: _py_ast.FormattedValue) -> pa.Expr:
        span = self._span(node)
        inner = self.lift_expr(node.value)
        format_name = pa.Name(span=span, ty=_DYN, ident="format")
        spec = pa.StrLit(span=span, ty=_DYN, value=self._literal_format_spec(node.format_spec))
        return pa.Call(
            span=span, ty=_DYN, func=format_name, args=(inner, spec), kwargs=()
        )

    def _expr_NamedExpr(self, node: _py_ast.NamedExpr) -> pa.Expr:
        """``(x := expr)`` — walrus. Represent as a Call to ``__walrus__``.

        Phase 1 MVP does not require walrus support; this keeps the parser
        total rather than crashing on legal syntax the frontend will reject
        later.
        """
        span = self._span(node)
        target = self.lift_expr(node.target)
        value = self.lift_expr(node.value)
        sentinel = pa.Name(span=span, ty=_DYN, ident="__walrus__")
        return pa.Call(
            span=span,
            ty=_DYN,
            func=sentinel,
            args=(target, value),
            kwargs=(),
        )

    def _expr_Yield(self, node: _py_ast.Yield) -> pa.Expr:
        """``yield expr`` — represent as a Call to ``__yield__`` sentinel."""
        span = self._span(node)
        args: tuple[pa.Expr, ...] = (
            (self.lift_expr(node.value),) if node.value is not None else ()
        )
        sentinel = pa.Name(span=span, ty=_DYN, ident="__yield__")
        return pa.Call(span=span, ty=_DYN, func=sentinel, args=args, kwargs=())

    def _expr_YieldFrom(self, node: _py_ast.YieldFrom) -> pa.Expr:
        span = self._span(node)
        sentinel = pa.Name(span=span, ty=_DYN, ident="__yield_from__")
        return pa.Call(
            span=span,
            ty=_DYN,
            func=sentinel,
            args=(self.lift_expr(node.value),),
            kwargs=(),
        )

    def _expr_Await(self, node: _py_ast.Await) -> pa.Expr:
        span = self._span(node)
        sentinel = pa.Name(span=span, ty=_DYN, ident="__await__")
        return pa.Call(
            span=span,
            ty=_DYN,
            func=sentinel,
            args=(self.lift_expr(node.value),),
            kwargs=(),
        )

    def _expr_ListComp(self, node: _py_ast.ListComp) -> pa.Expr:
        """List comprehension → desugar to a Call chain of ``__listcomp__``.

        Phase 1 MVP will reject these in typed code; leaving them parseable
        lets inference produce a proper diagnostic instead of a crash.
        """
        return self._comprehension_sentinel(node, "__listcomp__", node.elt, node.generators)

    def _expr_SetComp(self, node: _py_ast.SetComp) -> pa.Expr:
        return self._comprehension_sentinel(node, "__setcomp__", node.elt, node.generators)

    def _expr_GeneratorExp(self, node: _py_ast.GeneratorExp) -> pa.Expr:
        return self._comprehension_sentinel(node, "__genexpr__", node.elt, node.generators)

    def _expr_DictComp(self, node: _py_ast.DictComp) -> pa.Expr:
        span = self._span(node)
        sentinel = pa.Name(span=span, ty=_DYN, ident="__dictcomp__")
        key_e = self.lift_expr(node.key)
        val_e = self.lift_expr(node.value)
        generators_expr = self._lift_generators(node.generators, span)
        return pa.Call(
            span=span,
            ty=_DYN,
            func=sentinel,
            args=(key_e, val_e, generators_expr),
            kwargs=(),
        )

    def _comprehension_sentinel(
        self,
        node: _py_ast.expr,
        sentinel_name: str,
        elt: _py_ast.expr,
        generators: list[_py_ast.comprehension],
    ) -> pa.Expr:
        span = self._span(node)
        sentinel = pa.Name(span=span, ty=_DYN, ident=sentinel_name)
        elt_e = self.lift_expr(elt)
        generators_expr = self._lift_generators(generators, span)
        return pa.Call(
            span=span,
            ty=_DYN,
            func=sentinel,
            args=(elt_e, generators_expr),
            kwargs=(),
        )

    def _lift_generators(
        self,
        generators: list[_py_ast.comprehension],
        span: pa.SourceSpan,
    ) -> pa.Expr:
        """Pack comprehension clauses into a TupleExpr of TupleExprs.

        Each clause → ``(target, iter, (cond1, cond2, ...), is_async)``.
        """
        packed: list[pa.Expr] = []
        for g in generators:
            target = self.lift_expr(g.target)
            iter_e = self.lift_expr(g.iter)
            ifs: tuple[pa.Expr, ...] = tuple(self.lift_expr(i) for i in g.ifs)
            ifs_tuple: pa.Expr = pa.TupleExpr(span=span, ty=_DYN, elems=ifs)
            is_async = pa.BoolLit(span=span, ty=_DYN, value=bool(g.is_async))
            packed.append(
                pa.TupleExpr(
                    span=span,
                    ty=_DYN,
                    elems=(target, iter_e, ifs_tuple, is_async),
                )
            )
        return pa.TupleExpr(span=span, ty=_DYN, elems=tuple(packed))

    # -- Args + annotations ---------------------------------------------

    def _lift_arguments(self, args: _py_ast.arguments) -> tuple[pa.Arg, ...]:
        """Flatten ast.arguments into a sequence of pa.Arg.

        Order: pos_only, pos, *args, kw_only, **kwargs.
        Defaults:
          - ``args.defaults`` covers trailing positional args (pos_only + pos).
          - ``args.kw_defaults`` covers kw_only args (one-to-one, None for
            required).
        """
        out: list[pa.Arg] = []

        pos_only = list(args.posonlyargs)
        pos = list(args.args)
        all_positional = pos_only + pos
        pos_defaults = list(args.defaults)
        # pos_defaults align to the TAIL of all_positional.
        default_offset = len(all_positional) - len(pos_defaults)

        for i, a in enumerate(pos_only):
            idx_in_all = i
            default_expr: Optional[pa.Expr] = None
            if idx_in_all >= default_offset:
                default_expr = self.lift_expr(
                    pos_defaults[idx_in_all - default_offset]
                )
            out.append(
                pa.Arg(
                    name=a.arg,
                    annotation=self._lift_annotation(a.annotation)
                    if a.annotation is not None
                    else None,
                    default=default_expr,
                    kind="pos_only",
                    has_default=idx_in_all >= default_offset,
                )
            )

        for i, a in enumerate(pos):
            idx_in_all = len(pos_only) + i
            default_expr = None
            if idx_in_all >= default_offset:
                default_expr = self.lift_expr(
                    pos_defaults[idx_in_all - default_offset]
                )
            out.append(
                pa.Arg(
                    name=a.arg,
                    annotation=self._lift_annotation(a.annotation)
                    if a.annotation is not None
                    else None,
                    default=default_expr,
                    kind="pos",
                    has_default=idx_in_all >= default_offset,
                )
            )

        if args.vararg is not None:
            va = args.vararg
            out.append(
                pa.Arg(
                    name=va.arg,
                    annotation=self._lift_annotation(va.annotation)
                    if va.annotation is not None
                    else None,
                    default=None,
                    kind="*args",
                    has_default=False,
                )
            )

        for a, default_node in zip(args.kwonlyargs, args.kw_defaults):
            default_expr = (
                self.lift_expr(default_node) if default_node is not None else None
            )
            out.append(
                pa.Arg(
                    name=a.arg,
                    annotation=self._lift_annotation(a.annotation)
                    if a.annotation is not None
                    else None,
                    default=default_expr,
                    kind="kw_only",
                    has_default=default_node is not None,
                )
            )

        if args.kwarg is not None:
            kwa = args.kwarg
            out.append(
                pa.Arg(
                    name=kwa.arg,
                    annotation=self._lift_annotation(kwa.annotation)
                    if kwa.annotation is not None
                    else None,
                    default=None,
                    kind="**kwargs",
                    has_default=False,
                )
            )

        return tuple(out)

    def _lift_annotation(self, node: _py_ast.expr) -> pa.Type:
        """Best-effort lift of an annotation AST into a pa.Type.

        The real type-inference pass does full resolution. Here we only
        recognize the trivial surface forms: bare names that match one of
        the primitive types. Everything else lifts to :class:`DynType` so
        inference can continue.
        """
        # ``int``, ``float``, ``bool``, ``None``, ``str``.
        if isinstance(node, _py_ast.Name):
            ident = node.id
            # Preserve the typing marker long enough for codegen to elide the
            # annotated value.  Treating it as an anonymous ``dyn`` loses the
            # only reliable signal for aliases composed entirely from local
            # protocol/classes (a common shape in NumPy's typing modules).
            if ident == "TypeAlias":
                return pa.DynType(name="TypeAlias")
            return _PRIMITIVE_TYPES.get(ident, pa.ClassType(ident, "", (), ()))
        if isinstance(node, _py_ast.Attribute):
            from pcc.frontends.python.types import parse_annotation
            return parse_annotation(self.lift_expr(node))
        if isinstance(node, _py_ast.Constant) and node.value is None:
            return pa.NoneType(name="None")
        # list[T], dict[K, V], tuple[...], etc.
        if isinstance(node, _py_ast.Subscript):
            return self._lift_subscripted_annotation(node)
        # Anything else (Optional[X], Callable[...], forward refs, etc.)
        # defers to inference.
        return pa.DynType(name="dyn")

    def _lift_subscripted_annotation(self, node: _py_ast.Subscript) -> pa.Type:
        """Lift ``list[T]`` / ``dict[K, V]`` / ``tuple[...]``."""
        # Identify the base constructor.
        base = node.value
        base_name: Optional[str] = None
        if isinstance(base, _py_ast.Name):
            base_name = base.id
        elif isinstance(base, _py_ast.Attribute):
            base_name = base.attr  # e.g. typing.List -> "List"
        if base_name is None:
            return pa.DynType(name="dyn")

        normalized = base_name.lower()
        slice_node = node.slice

        if normalized in ("list",):
            inner = self._lift_annotation(slice_node)
            return pa.ListType(name="list", elem=inner)
        if normalized in ("dict",):
            if isinstance(slice_node, _py_ast.Tuple) and len(slice_node.elts) == 2:
                k = self._lift_annotation(slice_node.elts[0])
                v = self._lift_annotation(slice_node.elts[1])
                return pa.DictType(name="dict", key=k, value=v)
            return pa.DynType(name="dyn")
        if normalized in ("tuple",):
            if isinstance(slice_node, _py_ast.Tuple):
                elems = tuple(self._lift_annotation(e) for e in slice_node.elts)
            else:
                elems = (self._lift_annotation(slice_node),)
            return pa.TupleType(name="tuple", elems=elems)
        if normalized == "i64_buffer":
            if not (
                isinstance(slice_node, _py_ast.Constant)
                and isinstance(slice_node.value, int)
                and not isinstance(slice_node.value, bool)
            ):
                return pa.DynType(name="dyn")
            length = int(slice_node.value)
            if length < 1 or length > 1_048_576:
                return pa.DynType(name="dyn")
            return pa.BytesType(name="pcc.i64_buffer[" + str(length) + "]")
        if normalized == "callable":
            if (
                isinstance(slice_node, _py_ast.Tuple)
                and len(slice_node.elts) == 2
            ):
                params_node = slice_node.elts[0]
                ret_node = slice_node.elts[1]
                if isinstance(params_node, _py_ast.List):
                    params = tuple(
                        self._lift_annotation(e) for e in params_node.elts
                    )
                else:
                    params = ()
                return pa.FuncType(
                    name="callable",
                    params=params,
                    ret=self._lift_annotation(ret_node),
                )
        return pa.DynType(name="dyn")


# ---------------------------------------------------------------------------
# Primitive-type lookup for annotations
# ---------------------------------------------------------------------------

_PRIMITIVE_TYPES: dict[str, pa.Type] = {
    "int": pa.IntType(name="int", width=64, signed=True),
    "float": pa.FloatType(name="float", width=64),
    "bool": pa.BoolType(name="bool"),
    "str": pa.StrType(name="str"),
    "None": pa.NoneType(name="None"),
}


# ---------------------------------------------------------------------------
# Dispatch tables — module level so they don't rebuild per Lifter instance.
# ---------------------------------------------------------------------------

_STMT_DISPATCH: dict[type, "object"] = {
    _py_ast.FunctionDef: _Lifter._stmt_FunctionDef,
    _py_ast.AsyncFunctionDef: _Lifter._stmt_AsyncFunctionDef,
    _py_ast.ClassDef: _Lifter._stmt_ClassDef,
    _py_ast.Return: _Lifter._stmt_Return,
    _py_ast.Delete: _Lifter._stmt_Delete,
    _py_ast.Assign: _Lifter._stmt_Assign,
    _py_ast.AugAssign: _Lifter._stmt_AugAssign,
    _py_ast.AnnAssign: _Lifter._stmt_AnnAssign,
    _py_ast.For: _Lifter._stmt_For,
    _py_ast.AsyncFor: _Lifter._stmt_AsyncFor,
    _py_ast.While: _Lifter._stmt_While,
    _py_ast.If: _Lifter._stmt_If,
    _py_ast.With: _Lifter._stmt_With,
    _py_ast.AsyncWith: _Lifter._stmt_AsyncWith,
    _py_ast.Raise: _Lifter._stmt_Raise,
    _py_ast.Try: _Lifter._stmt_Try,
    _py_ast.Assert: _Lifter._stmt_Assert,
    _py_ast.Import: _Lifter._stmt_Import,
    _py_ast.ImportFrom: _Lifter._stmt_ImportFrom,
    _py_ast.Global: _Lifter._stmt_Global,
    _py_ast.Nonlocal: _Lifter._stmt_Nonlocal,
    _py_ast.Expr: _Lifter._stmt_Expr,
    _py_ast.Pass: _Lifter._stmt_Pass,
    _py_ast.Break: _Lifter._stmt_Break,
    _py_ast.Continue: _Lifter._stmt_Continue,
}

# TryStar exists only in Python 3.11+; guard the lookup.
_TryStar = getattr(_py_ast, "TryStar", None)
if _TryStar is not None:
    _STMT_DISPATCH[_TryStar] = _Lifter._stmt_TryStar

_EXPR_DISPATCH: dict[type, "object"] = {
    _py_ast.Constant: _Lifter._expr_Constant,
    _py_ast.Name: _Lifter._expr_Name,
    _py_ast.BinOp: _Lifter._expr_BinOp,
    _py_ast.UnaryOp: _Lifter._expr_UnaryOp,
    _py_ast.BoolOp: _Lifter._expr_BoolOp,
    _py_ast.Compare: _Lifter._expr_Compare,
    _py_ast.Call: _Lifter._expr_Call,
    _py_ast.Attribute: _Lifter._expr_Attribute,
    _py_ast.Subscript: _Lifter._expr_Subscript,
    _py_ast.Slice: _Lifter._expr_Slice,
    _py_ast.List: _Lifter._expr_List,
    _py_ast.Tuple: _Lifter._expr_Tuple,
    _py_ast.Set: _Lifter._expr_Set,
    _py_ast.Dict: _Lifter._expr_Dict,
    _py_ast.IfExp: _Lifter._expr_IfExp,
    _py_ast.Lambda: _Lifter._expr_Lambda,
    _py_ast.Starred: _Lifter._expr_Starred,
    _py_ast.JoinedStr: _Lifter._expr_JoinedStr,
    _py_ast.FormattedValue: _Lifter._expr_FormattedValue,
    _py_ast.NamedExpr: _Lifter._expr_NamedExpr,
    _py_ast.Yield: _Lifter._expr_Yield,
    _py_ast.YieldFrom: _Lifter._expr_YieldFrom,
    _py_ast.Await: _Lifter._expr_Await,
    _py_ast.ListComp: _Lifter._expr_ListComp,
    _py_ast.SetComp: _Lifter._expr_SetComp,
    _py_ast.GeneratorExp: _Lifter._expr_GeneratorExp,
    _py_ast.DictComp: _Lifter._expr_DictComp,
}
