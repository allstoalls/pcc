"""P6C.3 — lift the native parser's ``_*`` nodes into ``py_ast``.

The native parser (:mod:`pcc.parse.py_parse`) yields a small set of
dataclasses (``_Module``, ``_FuncDef``, ``_Call`` etc.). The rest of
the Python frontend consumes :mod:`pcc.py_frontend.py_ast` nodes. This
module bridges the two.

All lifted :class:`py_ast.Expr` nodes default their ``ty`` field to
:class:`py_ast.DynType` — the type-inference pass fills in concrete
types later.

Coverage:
  - All statement nodes emitted by the parser.
  - Expression nodes: literals, names, binary / unary / compare /
    bool-op, call, attr, subscript (incl. slice), list / tuple / dict
    literals, ternary, lambda.
  - Best-effort for f-strings and comprehensions: f-strings lower to
    a string-concat call, comprehensions to a synthetic call to
    ``_list_comp`` / ``_dict_comp`` / ``_set_comp`` / ``_gen_comp`` —
    a shim the lowering stage rewrites to an explicit loop.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

from ..py_frontend import py_ast as pa
from ..py_frontend.freestanding_constants import fold_module_int_constants
from . import py_parse as pp

_DYN = pa.DynType("dyn")


def _node_text(value: object) -> str:
    if isinstance(value, str):
        return value
    try:
        text = value.text
    except AttributeError:
        return ""
    return text if isinstance(text, str) else ""


def _node_ident(node: object) -> str:
    """Return identifier text from parser nodes with either ``ident`` or ``id``."""
    try:
        out = node.ident
    except AttributeError:
        try:
            out = node.id
        except AttributeError:
            out = ""
    return _node_text(out)


def _node_attr_name(node: object) -> str:
    """Return attribute text from parser nodes across snapshot variants."""
    try:
        out = node.name
    except AttributeError:
        try:
            out = node.attr
        except AttributeError:
            out = ""
    return _node_text(out)


def _node_op(value: object) -> str:
    """Return operator text from parser nodes across token/string variants."""
    return _node_text(value)


class LiftError(Exception):
    """Raised when a parser node has no py_ast equivalent yet."""


def _parse_float_literal_lift(text: str) -> float:
    return float(text.replace("_", ""))


def lift_module(mod: pp._Module, filename: str, module_name: str) -> pa.Module:
    L = _Lifter(filename)
    # Use an explicit loop instead of a generator so we can attach
    # top-level stmt context to any LiftError raised from below.
    # See docs/investigations/pcc1-stage2-lift-expr-raw-value-leak.md.
    body_list: list = []
    idx = 0
    for s in mod.body:
        try:
            lifted = L.lift_stmt(s)
        except LiftError as ex:
            _line = -1
            try:
                _line = s.line
            except AttributeError:
                _line = -1
            raise LiftError(
                str(ex)
                + " | top-stmt #"
                + str(idx)
                + " line="
                + str(_line)
                + " in "
                + filename
            )
        except Exception as ex:
            _line = -1
            try:
                _line = s.line
            except AttributeError:
                _line = -1
            raise LiftError(
                type(ex).__name__
                + ": "
                + str(ex)
                + " | top-stmt #"
                + str(idx)
                + " type="
                + type(s).__name__
                + " line="
                + str(_line)
                + " in "
                + filename
            )
        body_list.append(lifted)
        idx += 1
    body = tuple(body_list)
    # Flatten any lists (chained-assignment fanout etc.).
    flat: list[pa.Stmt] = []
    for s in body:
        if isinstance(s, list):
            flat.extend(s)
        else:
            flat.append(s)
    docstring: Optional[str] = None
    if (
        flat
        and isinstance(flat[0], pa.ExprStmt)
        and isinstance(flat[0].expr, pa.StrLit)
    ):
        docstring = flat[0].expr.value
    return pa.Module(module_name, tuple(flat), docstring)


class _Lifter:
    def __init__(self, filename: str) -> None:
        self.filename = filename

    def _lift_stmt_list(self, body) -> tuple[pa.Stmt, ...]:
        out: list[pa.Stmt] = []
        idx = 0
        for b in body:
            try:
                lifted = self.lift_stmt(b)
            except Exception as ex:
                _line = -1
                try:
                    _line = b.line
                except AttributeError:
                    _line = -1
                raise LiftError(
                    type(ex).__name__
                    + ": "
                    + str(ex)
                    + " | nested-stmt #"
                    + str(idx)
                    + " type="
                    + type(b).__name__
                    + " line="
                    + str(_line)
                    + " in "
                    + self.filename
                )
            if isinstance(lifted, list):
                out.extend(lifted)
            else:
                out.append(lifted)
            idx += 1
        return tuple(out)

    # ------------------------------------------------------------------ spans

    def _span(self, line: int) -> pa.SourceSpan:
        return pa.SourceSpan(self.filename, line, 1, line, 1)

    # ---------------------------------------------------------------- statements

    def lift_stmt(self, s) -> pa.Stmt:
        # Explicit dispatch — matches on concrete class identity. Beats
        # getattr(self, f"_s_{type(s).__name__[1:]}") for self-host:
        # scripts/audit_selfhost.py flags dynamic-attr patterns.
        t = type(s)
        if t is pp._Pass:
            return self._s_Pass(s)
        if t is pp._Break:
            return self._s_Break(s)
        if t is pp._Continue:
            return self._s_Continue(s)
        if t is pp._Return:
            return self._s_Return(s)
        if t is pp._Expr:
            return self._s_Expr(s)
        if t is pp._Assign:
            return self._s_Assign(s)
        if t is pp._AugAssign:
            return self._s_AugAssign(s)
        if t is pp._If:
            return self._s_If(s)
        if t is pp._While:
            return self._s_While(s)
        if t is pp._For:
            return self._s_For(s)
        if t is pp._FuncDef:
            return self._s_FuncDef(s)
        if t is pp._ClassDef:
            return self._s_ClassDef(s)
        if t is pp._Import:
            return self._s_Import(s)
        if t is pp._ImportFrom:
            return self._s_ImportFrom(s)
        if t is pp._Raise:
            return self._s_Raise(s)
        if t is pp._Try:
            return self._s_Try(s)
        if t is pp._With:
            return self._s_With(s)
        if t is pp._Global:
            return self._s_Global(s)
        if t is pp._Nonlocal:
            return self._s_Nonlocal(s)
        if t is pp._Del:
            return self._s_Del(s)
        if t is pp._Assert:
            return self._s_Assert(s)
        raise LiftError(f"no stmt lifter for {t.__name__}")

    def _s_Pass(self, s: pp._Pass) -> pa.Pass:
        return pa.Pass(self._span(s.line))

    def _s_Break(self, s: pp._Break) -> pa.Break:
        return pa.Break(self._span(s.line))

    def _s_Continue(self, s: pp._Continue) -> pa.Continue:
        return pa.Continue(self._span(s.line))

    def _s_Return(self, s: pp._Return) -> pa.Return:
        val = self.lift_expr(s.value) if s.value is not None else None
        return pa.Return(self._span(s.line), val)

    def _s_Expr(self, s: pp._Expr) -> pa.ExprStmt:
        return pa.ExprStmt(self._span(s.line), self.lift_expr(s.expr))

    def _s_Assign(self, s: pp._Assign) -> pa.Stmt:
        # ``annotation`` field holds either:
        #   - an AST node (``x: int = 1`` — original annotation)
        #   - ``"walrus"`` (inline ``:=``)
        #   - ``None`` (plain assign)
        target = self.lift_expr(s.target)
        value = self.lift_expr(s.value)
        ann_type: Optional[pa.Type] = None
        if s.annotation is not None and s.annotation != "walrus":
            ann_type = _lift_type(s.annotation)
        return pa.Assign(self._span(s.line), (target,), value, ann_type)

    def _s_AugAssign(self, s: pp._AugAssign) -> pa.AugAssign:
        return pa.AugAssign(
            self._span(s.line),
            self.lift_expr(s.target),
            _node_op(s.op),
            self.lift_expr(s.value),
        )

    def _s_If(self, s: pp._If) -> pa.If:
        return pa.If(
            self._span(s.line),
            self.lift_expr(s.cond),
            self._lift_stmt_list(s.body),
            self._lift_stmt_list(s.else_body),
        )

    def _s_While(self, s: pp._While) -> pa.While:
        return pa.While(
            self._span(s.line),
            self.lift_expr(s.cond),
            self._lift_stmt_list(s.body),
            self._lift_stmt_list(s.else_body),
        )

    def _s_For(self, s: pp._For) -> pa.For:
        return pa.For(
            self._span(s.line),
            self.lift_expr(s.target),
            self.lift_expr(s.iter),
            self._lift_stmt_list(s.body),
            self._lift_stmt_list(s.else_body),
            bool(getattr(s, "is_async", False)),
        )

    def _s_FuncDef(self, s: pp._FuncDef) -> pa.FuncDef:
        args_list = []
        for p in s.params:
            args_list.append(self._lift_arg(p))
        args = tuple(args_list)
        deco_list = []
        for d in s.decorators or ():
            deco_list.append(self.lift_expr(d))
        decos = tuple(deco_list)
        ret_ty = _lift_type(s.returns) if s.returns is not None else None
        return pa.FuncDef(
            self._span(s.line),
            s.name,
            args,
            ret_ty,
            self._lift_stmt_list(s.body),
            decos,
            False,
            bool(getattr(s, "is_async", False)),
        )

    def _lift_arg(self, param) -> pa.Arg:
        if len(param) == 2:
            kind, name = param
            ann = default = None
        else:
            kind, name, ann, default = param
        kmap = {
            "pos": "pos",
            "pos_only": "pos_only",
            "kw_only": "kw_only",
            "*args": "*args",
            "**kwargs": "**kwargs",
            "pos-only-sep": "pos_only",
            "kwonly-sep": "kw_only",
        }
        default_expr = self.lift_expr(default) if default is not None else None
        return pa.Arg(
            name,
            _lift_type(ann) if ann is not None else None,
            default_expr,
            kmap.get(kind, "pos"),
            default is not None,
        )

    def _s_ClassDef(self, s: pp._ClassDef) -> pa.ClassDef:
        bases: list[pa.Expr] = []
        keywords: list[tuple[str, pa.Expr]] = []
        for b in s.bases:
            if isinstance(b, tuple) and len(b) == 4 and b[0] == "__pcc_kwarg__":
                keywords.append((b[1], self.lift_expr(b[2])))
            elif isinstance(b, pp._Assign) and b.annotation is None:
                # ``name=value`` keyword — we encoded kwargs as _Assign.
                keywords.append(
                    (
                        _node_ident(b.target) if isinstance(b.target, pp._Name) else "",
                        self.lift_expr(b.value),
                    )
                )
            else:
                bases.append(self.lift_expr(b))
        deco_list = []
        for d in s.decorators:
            deco_list.append(self.lift_expr(d))
        decos = tuple(deco_list)
        return pa.ClassDef(
            self._span(s.line),
            s.name,
            tuple(bases),
            tuple(keywords),
            self._lift_stmt_list(s.body),
            decos,
        )

    def _s_Import(self, s: pp._Import) -> pa.Import:
        names_list = []
        for m, a in s.names:
            names_list.append((m, a))
        names = tuple(names_list)
        return pa.Import(self._span(s.line), names)

    def _s_ImportFrom(self, s: pp._ImportFrom) -> pa.ImportFrom:
        names_list = []
        for m, a in s.names:
            names_list.append((m, a))
        names = tuple(names_list)
        return pa.ImportFrom(self._span(s.line), s.module, names, s.level)

    def _s_Raise(self, s: pp._Raise) -> pa.Raise:
        exc = self.lift_expr(s.exc) if s.exc is not None else None
        cause = self.lift_expr(s.cause) if s.cause is not None else None
        return pa.Raise(self._span(s.line), exc, cause)

    def _s_Try(self, s: pp._Try) -> pa.Try:
        handlers = []
        for exc_ty, name, body in s.handlers:
            lifted_ty = self.lift_expr(exc_ty) if exc_ty is not None else None
            handlers.append(
                pa.ExceptHandler(
                    lifted_ty,
                    name,
                    self._lift_stmt_list(body),
                    self._span(s.line),
                )
            )
        return pa.Try(
            self._span(s.line),
            self._lift_stmt_list(s.body),
            tuple(handlers),
            self._lift_stmt_list(s.else_body),
            self._lift_stmt_list(s.finally_body),
        )

    def _s_With(self, s: pp._With) -> pa.With:
        item_list = []
        for ctx, name in s.items:
            as_name = None
            if name:
                as_name = pa.Name(self._span(s.line), _DYN, name)
            item_list.append((self.lift_expr(ctx), as_name))
        items = tuple(item_list)
        return pa.With(
            self._span(s.line),
            items,
            self._lift_stmt_list(s.body),
            bool(getattr(s, "is_async", False)),
        )

    def _s_Global(self, s: pp._Global) -> pa.Global:
        return pa.Global(self._span(s.line), s.names)

    def _s_Nonlocal(self, s: pp._Nonlocal) -> pa.Nonlocal:
        return pa.Nonlocal(self._span(s.line), s.names)

    def _s_Del(self, s: pp._Del) -> pa.Delete:
        targets = []
        for t in s.targets:
            targets.append(self.lift_expr(t))
        return pa.Delete(self._span(s.line), tuple(targets))

    def _s_Assert(self, s: pp._Assert):
        # pcc AST has no Assert — desugar to ``if not test: raise AssertionError(msg)``.
        span = self._span(s.line)
        not_test = pa.UnaryOp(span, _DYN, "not", self.lift_expr(s.test))
        msg = self.lift_expr(s.msg) if s.msg is not None else None
        args = (msg,) if msg is not None else ()
        exc = pa.Call(
            span,
            _DYN,
            pa.Name(span, _DYN, "AssertionError"),
            args,
            (),
        )
        raise_stmt = pa.Raise(span, exc, None)
        return pa.If(span, not_test, (raise_stmt,), ())

    # ---------------------------------------------------------------- expressions

    def lift_expr(self, e) -> pa.Expr:
        t = type(e)
        if t is pp._Num:
            return self._e_Num(e)
        if t is pp._ComplexNum:
            return self._e_ComplexNum(e)
        if t is pp._Str:
            return self._e_Str(e)
        if t is pp._Bytes:
            return self._e_Bytes(e)
        if t is pp._FString:
            return self._e_FString(e)
        if t is pp._FStringFormat:
            return self._e_FStringFormat(e)
        if t is pp._Bool:
            return self._e_Bool(e)
        if t is pp._None:
            return self._e_None(e)
        if t is pp._Name:
            return self._e_Name(e)
        if t is pp._BinOp:
            return self._e_BinOp(e)
        if t is pp._UnaryOp:
            return self._e_UnaryOp(e)
        if t is pp._Compare:
            return self._e_Compare(e)
        if t is pp._BoolOp:
            return self._e_BoolOp(e)
        if t is pp._Call:
            return self._e_Call(e)
        if t is pp._Attr:
            return self._e_Attr(e)
        if t is pp._Subscript:
            return self._e_Subscript(e)
        if t is pp._Slice:
            return self._e_Slice(e)
        if t is pp._List:
            return self._e_List(e)
        if t is pp._Tuple:
            return self._e_Tuple(e)
        if t is pp._Dict:
            return self._e_Dict(e)
        if t is pp._Set:
            return self._e_Set(e)
        if t is pp._Ternary:
            return self._e_Ternary(e)
        if t is pp._Lambda:
            return self._e_Lambda(e)
        if t is pp._Comp:
            return self._e_Comp(e)
        if t is pp._Yield:
            return self._e_Yield(e)
        if t is pp._Await:
            return self._e_Await(e)
        if t is pp._Starred:
            return self._e_Starred(e)
        if t is pp._Assign:
            return self._e_Assign(e)
        try:
            t_name = t.__name__
        except AttributeError:
            t_name = "<no-__name__>"
        # Self-host fallback: nested parser nodes can cross module/class
        # identity boundaries where ``t is pp._X`` fails even though the node
        # shape is intact. Keep this explicit so unsupported raw values still
        # report clearly instead of using dynamic getattr dispatch.
        if t_name == "_Num":
            return self._e_Num(e)
        if t_name == "_ComplexNum":
            return self._e_ComplexNum(e)
        if t_name == "_Str":
            return self._e_Str(e)
        if t_name == "_Bytes":
            return self._e_Bytes(e)
        if t_name == "_FString":
            return self._e_FString(e)
        if t_name == "_FStringFormat":
            return self._e_FStringFormat(e)
        if t_name == "_Bool":
            return self._e_Bool(e)
        if t_name == "_None":
            return self._e_None(e)
        if t_name == "_Name":
            return self._e_Name(e)
        if t_name == "_BinOp":
            return self._e_BinOp(e)
        if t_name == "_UnaryOp":
            return self._e_UnaryOp(e)
        if t_name == "_Compare":
            return self._e_Compare(e)
        if t_name == "_BoolOp":
            return self._e_BoolOp(e)
        if t_name == "_Call":
            return self._e_Call(e)
        if t_name == "_Attr":
            return self._e_Attr(e)
        if t_name == "_Subscript":
            return self._e_Subscript(e)
        if t_name == "_Slice":
            return self._e_Slice(e)
        if t_name == "_List":
            return self._e_List(e)
        if t_name == "_Tuple":
            return self._e_Tuple(e)
        if t_name == "_Dict":
            return self._e_Dict(e)
        if t_name == "_Set":
            return self._e_Set(e)
        if t_name == "_Ternary":
            return self._e_Ternary(e)
        if t_name == "_Lambda":
            return self._e_Lambda(e)
        if t_name == "_Comp":
            return self._e_Comp(e)
        if t_name == "_Yield":
            return self._e_Yield(e)
        if t_name == "_Await":
            return self._e_Await(e)
        if t_name == "_Starred":
            return self._e_Starred(e)
        if t_name == "_Assign":
            return self._e_Assign(e)
        raise LiftError("no expr lifter for " + t_name)

    def _e_Num(self, e: pp._Num) -> pa.Expr:
        span = self._span(e.line)
        if e.is_int:
            return pa.IntLit(span, pa.IntType("int", 64, True), int(e.text, 0))
        return pa.FloatLit(
            span, pa.FloatType("float", 64), _parse_float_literal_lift(e.text)
        )

    def _e_ComplexNum(self, e: pp._ComplexNum) -> pa.Expr:
        return pa.ComplexLit(
            self._span(e.line),
            pa.ComplexType("complex"),
            0.0,
            _parse_float_literal_lift(e.text),
        )

    def _e_Str(self, e: pp._Str) -> pa.StrLit:
        cooked: list[str] = []
        for raw_text, is_raw in e.parts:
            cooked.append(raw_text if is_raw else _decode_escapes(raw_text))
        return pa.StrLit(self._span(e.line), pa.StrType("str"), "".join(cooked))

    def _e_Bytes(self, e: pp._Bytes) -> pa.BytesLit:
        cooked: list[str] = []
        for raw_text, is_raw in e.parts:
            cooked.append(raw_text if is_raw else _decode_escapes(raw_text))
        return pa.BytesLit(
            self._span(e.line),
            pa.BytesType("bytes"),
            "".join(cooked).encode("latin-1"),
        )

    def _e_FString(self, e: pp._FString) -> pa.Expr:
        span = self._span(e.line)
        pieces: list[pa.Expr] = []
        for part in e.parts:
            if type(part) is pp._FStringText:
                text = part.text if part.is_raw else _decode_escapes(part.text)
                if text:
                    pieces.append(pa.StrLit(span, pa.StrType("str"), text))
                continue
            inner = self.lift_expr(part)
            # CPython: ``f"{x}"`` is ``format(x, "")``. The parser
            # strips the ``_FStringFormat`` wrapper for the bare-expr
            # case (no conversion / no spec), so ``part`` lifts to
            # ``inner`` directly. Wrapping in ``str(inner)`` skips the
            # user's ``__format__`` override (e.g. ``Tagged.__format__``
            # in tests/python/test_format_protocol.py). Lower as
            # ``format(inner, "")`` so the runtime ``py_obj_format``
            # path runs ``__format__`` and falls back to ``str`` only
            # for plain objects.
            pieces.append(
                pa.Call(
                    span,
                    pa.StrType("str"),
                    pa.Name(span, _DYN, "format"),
                    (inner, pa.StrLit(span, pa.StrType("str"), "")),
                    (),
                )
            )
        if not pieces:
            return pa.StrLit(span, pa.StrType("str"), "")
        out = pieces[0]
        i = 1
        while i < len(pieces):
            out = pa.BinOp(span, pa.StrType("str"), "+", out, pieces[i])
            i += 1
        return out

    def _spec_field_is_ident(self, s: str) -> bool:
        # Manual identifier test (avoid str.isidentifier so this stays
        # self-host-safe in pcc-py). Non-empty; first char alpha/_, rest
        # alphanumeric/_.
        if not s:
            return False
        c0 = s[0]
        if not (c0 == "_" or ("a" <= c0 <= "z") or ("A" <= c0 <= "Z")):
            return False
        k = 1
        while k < len(s):
            c = s[k]
            ok = c == "_" or ("a" <= c <= "z") or ("A" <= c <= "Z") or ("0" <= c <= "9")
            if not ok:
                return False
            k += 1
        return True

    def _fstring_spec_to_expr(self, spec_text: str, span):
        """Build the format-spec argument for ``f"{value:SPEC}"``. A static SPEC
        is a StrLit (unchanged). A SPEC with a nested replacement field that is a
        bare identifier — ``f"{v:>{w}}"`` (dynamic width), ``f"{v:.{p}f}"``
        (dynamic precision) — becomes a runtime concatenation ``">" + str(w)`` so
        the final spec string is assembled before formatting. ``{{``/``}}`` are
        literal braces. Returns None when a field is not a bare identifier (the
        caller then keeps the static spec)."""
        if "{" not in spec_text and "}" not in spec_text:
            return pa.StrLit(span, pa.StrType("str"), spec_text)
        parts = []
        lit = []
        i = 0
        n = len(spec_text)
        while i < n:
            ch = spec_text[i]
            if ch == "{" and i + 1 < n and spec_text[i + 1] == "{":
                lit.append("{")
                i += 2
                continue
            if ch == "}" and i + 1 < n and spec_text[i + 1] == "}":
                lit.append("}")
                i += 2
                continue
            if ch == "}":
                return None
            if ch == "{":
                j = i + 1
                while j < n and spec_text[j] != "}":
                    j += 1
                if j >= n:
                    return None
                field = spec_text[i + 1 : j].strip()
                if not self._spec_field_is_ident(field):
                    return None
                if lit:
                    parts.append(pa.StrLit(span, pa.StrType("str"), "".join(lit)))
                    lit = []
                parts.append(
                    pa.Call(
                        span,
                        pa.StrType("str"),
                        pa.Name(span, _DYN, "str"),
                        (pa.Name(span, _DYN, field),),
                        (),
                    )
                )
                i = j + 1
                continue
            lit.append(ch)
            i += 1
        if lit:
            parts.append(pa.StrLit(span, pa.StrType("str"), "".join(lit)))
        if not parts:
            return pa.StrLit(span, pa.StrType("str"), "")
        expr = parts[0]
        k = 1
        while k < len(parts):
            expr = pa.BinOp(span, _DYN, "+", expr, parts[k])
            k += 1
        return expr

    def _e_FStringFormat(self, e: pp._FStringFormat) -> pa.Expr:
        span = self._span(e.line)
        inner = self.lift_expr(e.expr)
        conversion = e.conversion
        if conversion == "r":
            inner = pa.Call(
                span, pa.StrType("str"), pa.Name(span, _DYN, "repr"), (inner,), ()
            )
        elif conversion == "s":
            inner = pa.Call(
                span, pa.StrType("str"), pa.Name(span, _DYN, "str"), (inner,), ()
            )
        elif conversion == "a":
            inner = pa.Call(
                span, pa.StrType("str"), pa.Name(span, _DYN, "ascii"), (inner,), ()
            )
        spec_text = ""
        if e.spec is not None:
            spec_text = e.spec
        if not spec_text:
            if conversion is None:
                # CPython semantics: ``f"{x}"`` is ``format(x, "")``,
                # which dispatches to ``x.__format__("")`` and only
                # falls back to ``str(x)`` when ``__format__`` is the
                # default ``object.__format__``. Lowering to ``str(x)``
                # directly skips the user's ``__format__`` override
                # (e.g. ``Tagged.__format__`` in
                # tests/python/test_format_protocol.py). Hand the
                # value to ``format(value, "")`` so custom
                # ``__format__`` runs, and the runtime
                # ``py_obj_format`` path takes care of the str
                # fallback for plain objects.
                inner = pa.Call(
                    span,
                    pa.StrType("str"),
                    pa.Name(span, _DYN, "format"),
                    (inner, pa.StrLit(span, pa.StrType("str"), "")),
                    (),
                )
            return inner
        spec_arg = self._fstring_spec_to_expr(spec_text, span)
        if spec_arg is None:
            # A nested field too complex for the inline builder: keep the
            # literal spec (matches the prior behaviour).
            spec_arg = pa.StrLit(span, pa.StrType("str"), spec_text)
        inner = pa.Call(
            span,
            pa.StrType("str"),
            pa.Name(span, _DYN, "format"),
            (inner, spec_arg),
            (),
        )
        return inner

    def _e_Bool(self, e: pp._Bool) -> pa.BoolLit:
        return pa.BoolLit(self._span(e.line), pa.BoolType("bool"), e.value)

    def _e_None(self, e: pp._None) -> pa.NoneLit:
        return pa.NoneLit(self._span(e.line), pa.NoneType("None"))

    def _e_Name(self, e: pp._Name) -> pa.Name:
        return pa.Name(self._span(e.line), _DYN, _node_ident(e))

    def _e_BinOp(self, e: pp._BinOp) -> pa.BinOp:
        return pa.BinOp(
            self._span(e.line),
            _DYN,
            _node_op(e.op),
            self.lift_expr(e.lhs),
            self.lift_expr(e.rhs),
        )

    def _e_UnaryOp(self, e: pp._UnaryOp) -> pa.UnaryOp:
        return pa.UnaryOp(
            self._span(e.line), _DYN, _node_op(e.op), self.lift_expr(e.operand)
        )

    def _e_Compare(self, e: pp._Compare) -> pa.Compare:
        return pa.Compare(
            self._span(e.line),
            _DYN,
            _node_op(e.op),
            self.lift_expr(e.lhs),
            self.lift_expr(e.rhs),
        )

    def _e_BoolOp(self, e: pp._BoolOp) -> pa.Expr:
        vals = []
        for v in e.values:
            vals.append(self.lift_expr(v))
        span = self._span(e.line)
        op = _node_op(e.op)
        return self._build_balanced_bool_expr(vals, span, op)

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
        return pa.BoolExpr(span, _DYN, op, left, right)

    def _e_Call(self, e: pp._Call) -> pa.Call:
        args: list[pa.Expr] = []
        kwargs: list[tuple[str, pa.Expr]] = []
        operand_order: list[tuple[str, int]] = []
        for a in e.args:
            if isinstance(a, tuple) and len(a) == 4 and a[0] == "__pcc_kwarg__":
                kwargs.append((a[1], self.lift_expr(a[2])))
                operand_order.append(("kw", len(kwargs) - 1))
            elif isinstance(a, pp._Assign) and a.annotation is None:
                # ``name=value`` kwarg encoded as _Assign.
                name = _node_ident(a.target)
                kwargs.append((name, self.lift_expr(a.value)))
                operand_order.append(("kw", len(kwargs) - 1))
            elif isinstance(a, pp._Starred):
                # Starred passes through as a Name("*<arg>") sentinel.
                inner = self.lift_expr(a.value)
                args.append(
                    pa.Call(
                        self._span(e.line),
                        _DYN,
                        pa.Name(self._span(e.line), _DYN, "*" if not a.is_kw else "**"),
                        (inner,),
                        (),
                    )
                )
                operand_order.append(("arg", len(args) - 1))
            else:
                try:
                    a_name = type(a).__name__
                except AttributeError:
                    a_name = ""
                if isinstance(a, pp._Comp) or a_name == "_Comp":
                    args.append(self._e_Comp(a))
                else:
                    args.append(self.lift_expr(a))
                operand_order.append(("arg", len(args) - 1))
        return pa.Call(
            self._span(e.line),
            _DYN,
            self.lift_expr(e.func),
            tuple(args),
            tuple(kwargs),
            tuple(operand_order),
        )

    def _e_Attr(self, e: pp._Attr) -> pa.Attr:
        return pa.Attr(
            self._span(e.line), _DYN, self.lift_expr(e.obj), _node_attr_name(e)
        )

    def _e_Subscript(self, e: pp._Subscript) -> pa.Subscript:
        idx = self.lift_expr(e.idx)
        return pa.Subscript(self._span(e.line), _DYN, self.lift_expr(e.obj), idx)

    def _e_Slice(self, e: pp._Slice) -> pa.Slice:
        lo = None if e.lo is None else self.lift_expr(e.lo)
        hi = None if e.hi is None else self.lift_expr(e.hi)
        step = None if e.step is None else self.lift_expr(e.step)
        lo = None if isinstance(lo, pa.NoneLit) else lo
        hi = None if isinstance(hi, pa.NoneLit) else hi
        step = None if isinstance(step, pa.NoneLit) else step
        return pa.Slice(self._span(e.line), _DYN, lo, hi, step)

    def _e_List(self, e: pp._List) -> pa.ListExpr:
        elem_list = []
        for x in e.elems:
            elem_list.append(self.lift_expr(x))
        elems = tuple(elem_list)
        return pa.ListExpr(self._span(e.line), _DYN, elems)

    def _e_Tuple(self, e: pp._Tuple) -> pa.TupleExpr:
        elem_list = []
        for x in e.elems:
            elem_list.append(self.lift_expr(x))
        elems = tuple(elem_list)
        return pa.TupleExpr(self._span(e.line), _DYN, elems)

    def _e_Dict(self, e: pp._Dict) -> pa.DictExpr:
        pairs = []
        for k, v in zip(e.keys, e.values):
            if k is None:
                # ``**mapping`` — encode as (Name("**"), mapping).
                pairs.append(
                    (
                        pa.Name(self._span(e.line), _DYN, "**"),
                        self.lift_expr(v),
                    )
                )
            else:
                pairs.append((self.lift_expr(k), self.lift_expr(v)))
        return pa.DictExpr(self._span(e.line), _DYN, tuple(pairs))

    def _e_Set(self, e: pp._Set) -> pa.Call:
        # py_ast has no SetExpr — lower to set([...]) so the downstream
        # pipeline sees a recognizable call.
        arg_list = []
        for x in e.elems:
            arg_list.append(self.lift_expr(x))
        args = tuple(arg_list)
        span = self._span(e.line)
        list_literal = pa.ListExpr(span, _DYN, args)
        return pa.Call(span, _DYN, pa.Name(span, _DYN, "set"), (list_literal,), ())

    def _e_Ternary(self, e: pp._Ternary) -> pa.IfExpr:
        return pa.IfExpr(
            self._span(e.line),
            _DYN,
            self.lift_expr(e.cond),
            self.lift_expr(e.then_expr),
            self.lift_expr(e.else_expr),
        )

    def _e_Lambda(self, e: pp._Lambda) -> pa.Lambda:
        arg_list = []
        for p in e.params:
            arg_list.append(self._lift_arg(p))
        args = tuple(arg_list)
        return pa.Lambda(self._span(e.line), _DYN, args, self.lift_expr(e.body))

    def _e_Comp(self, e: pp._Comp) -> pa.Call:
        # No first-class comprehension node — lower to a sentinel Call
        # so the lowering stage can rewrite to an explicit loop.
        span = self._span(e.line)
        sym = {
            "list": "_list_comp",
            "dict": "_dict_comp",
            "set": "_set_comp",
            "gen": "_gen_comp",
        }[e.kind]
        if e.kind == "dict":
            pair = e.elt
            pair_kind = type(pair).__name__
            if pair_kind == "_Tuple":
                elems = pair.elems
                k = elems[0]
                v = elems[1]
            elif pair_kind == "_DictCompElt":
                k = pair.key
                v = pair.value
            else:
                k = pair[0]
                v = pair[1]
            elt = pa.TupleExpr(span, _DYN, (self.lift_expr(k), self.lift_expr(v)))
        else:
            elt = self.lift_expr(e.elt)
        # Flatten generators into a tuple of (target, iter, (ifs,)) calls.
        gens = []
        for gen in e.generators:
            target = gen[0]
            it = gen[1]
            ifs = gen[2]
            if_list = []
            for i in ifs:
                if_list.append(self.lift_expr(i))
            gen_call = pa.Call(
                span,
                _DYN,
                pa.Name(span, _DYN, "_gen_clause"),
                (
                    self.lift_expr(target),
                    self.lift_expr(it),
                    pa.TupleExpr(span, _DYN, tuple(if_list)),
                ),
                (),
            )
            gens.append(gen_call)
        return pa.Call(span, _DYN, pa.Name(span, _DYN, sym), (elt, *gens), ())

    def _e_Yield(self, e: pp._Yield) -> pa.Call:
        # No yield node — sentinel call.
        span = self._span(e.line)
        sym = "_yield_from" if e.is_from else "_yield"
        args = (self.lift_expr(e.value),) if e.value is not None else ()
        return pa.Call(span, _DYN, pa.Name(span, _DYN, sym), args, ())

    def _e_Await(self, e: pp._Await) -> pa.Call:
        span = self._span(e.line)
        return pa.Call(
            span, _DYN, pa.Name(span, _DYN, "__await__"), (self.lift_expr(e.value),), ()
        )

    def _e_Starred(self, e: pp._Starred) -> pa.Call:
        span = self._span(e.line)
        return pa.Call(
            span,
            _DYN,
            pa.Name(span, _DYN, "**" if e.is_kw else "*"),
            (self.lift_expr(e.value),),
            (),
        )

    def _e_Assign(self, e: pp._Assign) -> pa.Expr:
        # Walrus / inline assignment in expression position.
        span = self._span(e.line)
        return pa.Call(
            span,
            _DYN,
            pa.Name(span, _DYN, "_walrus"),
            (self.lift_expr(e.target), self.lift_expr(e.value)),
            (),
        )


# Map a lifted type-expression AST node to a pcc ``Type``. Best effort;
# unknown shapes fall back to ``DynType``.
_TYPE_NAME_MAP = {
    "int": pa.IntType("int", 64, True),
    "i8": pa.IntType("int", 8, True),
    "i16": pa.IntType("int", 16, True),
    "i32": pa.IntType("int", 32, True),
    # Raw machine integers are a distinct semantic projection.  Do not fold
    # them into Python ``int`` here: the type checker/codegen must be able to
    # distinguish explicit wrapping machine arithmetic from arbitrary-
    # precision Python integer arithmetic.
    "i64": pa.IntType("pcc.i64", 64, True),
    "u64": pa.IntType("pcc.u64", 64, False),
    "float": pa.FloatType("float", 64),
    "bool": pa.BoolType("bool"),
    "str": pa.StrType("str"),
    "bytes": pa.BytesType("bytes"),
    "bytearray": pa.ByteArrayType("bytearray"),
    "memoryview": pa.MemoryViewType("memoryview"),
    "None": pa.NoneType("None"),
    "object": _DYN,
    "Any": _DYN,
    # Preserve the mutable/immutable container projection at parse time.
    # Type inference still canonicalizes older DynType(name=...) snapshots.
    "set": pa.SetType("set", _DYN),
    "frozenset": pa.SetType("frozenset", _DYN),
}


def _lookup_type_name(name: str) -> pa.Type | None:
    for key, value in _TYPE_NAME_MAP.items():
        if key == name:
            return value
    return None


def _class_type(name: str) -> pa.ClassType:
    return pa.ClassType(name, "", (), ())


def _lift_type(node) -> pa.Type:
    if node is None:
        return _DYN
    if isinstance(node, pp._Name):
        node_name = _node_ident(node)
        ty = _lookup_type_name(node_name)
        if ty is not None:
            return ty
        return _class_type(node_name)
    if isinstance(node, pp._None):
        return pa.NoneType("None")
    if isinstance(node, pp._Attr):
        # e.g. ``pcc.IntType`` — resolve the tail token only. If the
        # tail name isn't in the builtin map, emit a ``ClassType`` shell
        # so ``resolve_type_refs`` can rebind it against the local +
        # cross-module ``class_types`` table during type inference.
        # Unknown names that never get registered fall back to DynType
        # via ``resolve_type_refs`` returning the unresolved shell as-is.
        attr_name = _node_attr_name(node)
        ty = _lookup_type_name(attr_name)
        if ty is not None:
            return ty
        return _class_type(attr_name)
    if isinstance(node, pp._Subscript):
        base = node.obj
        base_name = ""
        if isinstance(base, pp._Name):
            base_name = _node_ident(base)
        elif isinstance(base, pp._Attr):
            base_name = _node_attr_name(base)
        if base_name:
            if base_name in ("list", "List"):
                return pa.ListType("list", _lift_type(node.idx))
            if base_name == "array":
                inner = node.idx
                if not isinstance(inner, pp._Tuple) or len(inner.elems) != 2:
                    return pa.ValueArrayType("pcc.array", _DYN, -1)
                element_node = inner.elems[0]
                length_node = inner.elems[1]
                if not isinstance(length_node, pp._Num) or not length_node.is_int:
                    return pa.ValueArrayType("pcc.array", _lift_type(element_node), -2)
                return pa.ValueArrayType(
                    "pcc.array",
                    _lift_type(element_node),
                    int(length_node.text, 0),
                )
            if base_name == "i64_buffer":
                length_node = node.idx
                if not isinstance(length_node, pp._Num) or not length_node.is_int:
                    return _DYN
                length = int(length_node.text, 0)
                if length < 1 or length > 1_048_576:
                    return _DYN
                return pa.BytesType("pcc.i64_buffer[" + str(length) + "]")
            if base_name in ("tuple", "Tuple"):
                inner = node.idx
                # ``tuple[T, ...]`` — homogeneous variadic tuple.
                # Phase-1 models this as a ``TupleType`` with an
                # empty ``elems`` and a separate marker so type
                # comparisons treat it as compatible with any
                # TupleType whose elements all subtype ``T``. To
                # stay within the existing dataclass, encode the
                # element type as a length-1 ``elems`` tuple and
                # tag the result via a side channel; the checker
                # consults it via ``_is_variadic_tuple``.
                if (
                    isinstance(inner, pp._Tuple)
                    and len(inner.elems) == 2
                    and isinstance(inner.elems[1], pp._Name)
                    and _node_ident(inner.elems[1]) == "Ellipsis"
                ):
                    return pa.TupleType("tuple_variadic", (_lift_type(inner.elems[0]),))
                if isinstance(inner, pp._Tuple):
                    elem_types = []
                    for x in inner.elems:
                        elem_types.append(_lift_type(x))
                    return pa.TupleType("tuple", tuple(elem_types))
                return pa.TupleType("tuple", (_lift_type(inner),))
            if base_name in ("dict", "Dict"):
                inner = node.idx
                if isinstance(inner, pp._Tuple) and len(inner.elems) == 2:
                    return pa.DictType(
                        "dict", _lift_type(inner.elems[0]), _lift_type(inner.elems[1])
                    )
            if base_name in ("set", "Set", "frozenset", "FrozenSet"):
                name = (
                    "frozenset"
                    if base_name in ("frozenset", "FrozenSet")
                    else "set"
                )
                return pa.SetType(name, _lift_type(node.idx))
            if base_name == "Optional":
                # PEP 484 ``Optional[T]`` ≡ ``T | None``. We unwrap
                # ``Optional[<non-primitive>]`` (class shells, ir.X,
                # str, list, dict, tuple) so that
                # ``builder: Optional[ir.IRBuilder]`` resolves to the
                # full IRBuilder schema and ``self.builder.X(...)``
                # dispatches natively. Primitive numeric types
                # (``int``/``float``/``bool``) stay as DynType because
                # pcc Phase 1 unboxes them and has no nullable
                # representation — assigning ``None`` to an unboxed
                # ``i64`` field would corrupt the IR (verified
                # 2026-05-09 against ``self.align: Optional[int]``
                # patterns in ``pcc/llvm_capi/ir.py``).
                inner = _lift_type(node.idx)
                if isinstance(inner, (pa.IntType, pa.FloatType, pa.BoolType)):
                    return _DYN
                return inner
            if base_name == "Callable":
                inner = node.idx
                if isinstance(inner, pp._Tuple) and len(inner.elems) == 2:
                    params_node = inner.elems[0]
                    ret_node = inner.elems[1]
                    if isinstance(params_node, pp._List):
                        params = tuple(_lift_type(p) for p in params_node.elems)
                    else:
                        params = ()
                    return pa.FuncType("callable", params, _lift_type(ret_node))
        return _DYN
    if isinstance(node, pp._BinOp) and _node_op(node.op) == "|":
        # PCC has no general Union type yet, but ``T | None`` is exactly the
        # PEP 604 spelling of ``Optional[T]`` handled above.  Preserve the
        # object projection for nullable classes and object containers so a
        # guarded receiver retains its native schema.  Primitive numeric
        # optionals remain Dyn because their normal representation is unboxed
        # and therefore cannot carry None.
        left = _lift_type(node.lhs)
        right = _lift_type(node.rhs)
        if isinstance(left, pa.NoneType):
            inner = right
        elif isinstance(right, pa.NoneType):
            inner = left
        else:
            return _DYN
        if isinstance(inner, (pa.IntType, pa.FloatType, pa.BoolType)):
            return _DYN
        return inner
    if isinstance(node, pp._Str):
        # PEP 484 forward-reference string annotation, e.g.
        # ``self: "L1CodeGen"``. pcc does not run host CPython's
        # ``typing.get_type_hints`` resolver, so we turn the string
        # into a ``ClassType`` shell here and let
        # ``resolve_type_refs`` rebind it against the local + cross-
        # module ``class_types`` table during type inference.
        try:
            parts = node.parts
        except AttributeError:
            return _DYN
        cooked: list[str] = []
        for raw_text, is_raw in parts:
            cooked.append(raw_text if is_raw else _decode_escapes(raw_text))
        name = "".join(cooked).strip()
        if not name:
            return _DYN
        ty = _lookup_type_name(name)
        if ty is not None:
            return ty
        return _class_type(name)
    return _DYN


# Python-style escape processing. We only handle the escapes the parser
# tokens might contain — ``\n``, ``\t``, ``\\``, ``\'``, ``\"``, ``\xNN``,
# ``\uNNNN``. The parser strips quotes; escapes remain literal.
_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "\\": "\\",
    "'": "'",
    '"': '"',
    "0": "\0",
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "v": "\v",
}


def _decode_escapes(raw: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(raw):
        c = raw[i]
        if c != "\\" or i + 1 >= len(raw):
            out.append(c)
            i += 1
            continue
        nxt = raw[i + 1]
        if nxt in _ESCAPES:
            out.append(_ESCAPES[nxt])
            i += 2
            continue
        if nxt == "x" and i + 3 < len(raw):
            out.append(chr(int(raw[i + 2 : i + 4], 16)))
            i += 4
            continue
        if nxt == "u" and i + 5 < len(raw):
            out.append(chr(int(raw[i + 2 : i + 6], 16)))
            i += 6
            continue
        # Unknown escape: keep literal (Python does the same with a warning).
        out.append(c)
        i += 1
    return "".join(out)


def parse_and_lift(src: str, filename: str, module_name: str) -> pa.Module:
    """Parse ``src`` with the native parser, lift to py_ast.Module."""
    try:
        mod = pp.parse(src, filename=filename)
    except Exception as ex:
        raise LiftError(
            "parse failed for "
            + module_name
            + " in "
            + filename
            + ": "
            + type(ex).__name__
            + ": "
            + str(ex)
        )
    try:
        lifted = lift_module(mod, filename, module_name)
    except LiftError:
        raise
    except Exception as ex:
        raise LiftError(
            "lift failed for "
            + module_name
            + " in "
            + filename
            + ": "
            + type(ex).__name__
            + ": "
            + str(ex)
        )
    # A freestanding module cannot hold a runtime global, so its lifecycle tags
    # and header offsets are spelled as bare literals at each use.  Resolving a
    # module-scope integer constant is a compile-time binding, so it belongs
    # here rather than at one of the several parse call sites: every path into
    # the frontend comes through this function.  Non-freestanding modules are
    # returned untouched.
    return fold_module_int_constants(lifted)
