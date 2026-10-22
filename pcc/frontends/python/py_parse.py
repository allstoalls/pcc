"""P6C.3 — native Python parser skeleton.

Recursive-descent parser over the tokens produced by
:mod:`pcc.frontends.python.py_lex`. Produces pcc's internal AST (the same
node types already used by :mod:`pcc.frontends.python.py_ast`).

Current scope is intentionally narrow — just enough to parse
``hello.py``-class programs end-to-end so the P6C.3 milestone has a
concrete proof. The full grammar (f-strings, match/case, decorators
with arguments, walrus, starred assignments) is staged in
successive iterations. CPython's own parser (``Parser/Python.asdl``
and ``Parser/parser.c``) is the reference.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import sys

from pcc.frontends.python.py_lex import Lexer, Token

# Keep these token-kind strings local to avoid pulling sibling-module
# constant imports through the multi-file CPython fallback path.
TK_NEWLINE = "NEWLINE"
TK_INDENT = "INDENT"
TK_DEDENT = "DEDENT"
TK_NAME = "NAME"
TK_NUMBER = "NUMBER"
TK_STRING = "STRING"
TK_OP = "OP"
TK_KEYWORD = "KEYWORD"
TK_EOF = "EOF"


class ParseError(Exception):
    pass


def _debug_parse_enabled() -> bool:
    try:
        value = str(os.environ.get("PCC_DEBUG_PY_PARSE", "") or "")
    except Exception:
        return False
    value = value.strip().lower()
    return value in ("1", "true", "yes", "on")


def _debug_parse(message: str) -> None:
    try:
        sys.stderr.write("[pcc.py_parse] " + message + "\n")
    except Exception:
        pass


def _token_debug_text(t) -> str:
    kind = "<missing>"
    text = "<missing>"
    line = "-1"
    try:
        kind = str(t.kind)
    except Exception:
        pass
    try:
        text = str(t.text)
    except Exception:
        pass
    try:
        line = str(t.line)
    except Exception:
        pass
    return "kind=" + kind + " text=" + text + " line=" + line


def _join_strings(parts: list[str], sep: str) -> str:
    if not parts:
        return ""
    out = parts[0]
    i = 1
    while i < len(parts):
        out += sep + parts[i]
        i += 1
    return out


def _parse_float_literal(text: str) -> float:
    return float(text.replace("_", ""))


@dataclass
class _Module:
    body: list


@dataclass
class _Pass:
    line: int


@dataclass
class _Return:
    value: "object | None"
    line: int


@dataclass
class _Expr:
    expr: object
    line: int


@dataclass
class _Num:
    text: str
    line: int
    is_int: bool


@dataclass
class _ComplexNum:
    text: str
    line: int


@dataclass
class _Str:
    parts: tuple[tuple[str, bool], ...]
    line: int


@dataclass
class _Bytes:
    parts: tuple[tuple[str, bool], ...]
    line: int


@dataclass
class _Name:
    ident: str
    line: int


@dataclass
class _BinOp:
    op: str
    lhs: object
    rhs: object
    line: int


@dataclass
class _Call:
    func: object
    args: list
    line: int


@dataclass
class _If:
    cond: object
    body: list
    else_body: list
    line: int


@dataclass
class _FuncDef:
    name: str
    params: list
    body: list
    line: int
    decorators: list = None
    returns: object = None
    is_async: bool = False


@dataclass
class _Await:
    value: object
    line: int


@dataclass
class _Assign:
    target: object
    value: object
    annotation: "object | None"
    line: int
    has_value: bool = True


@dataclass
class _AugAssign:
    target: object
    op: str  # ``+=`` / ``-=`` / ...
    value: object
    line: int


@dataclass
class _While:
    cond: object
    body: list
    else_body: list
    line: int


@dataclass
class _For:
    target: object
    iter: object
    body: list
    else_body: list
    line: int
    is_async: bool = False


@dataclass
class _Break:
    line: int


@dataclass
class _Continue:
    line: int


@dataclass
class _Import:
    names: list  # list of (module, as_name)
    line: int


@dataclass
class _ImportFrom:
    module: str
    names: list  # list of (attr, as_name)
    level: int
    line: int


@dataclass
class _ClassDef:
    name: str
    bases: list
    body: list
    decorators: list
    line: int


@dataclass
class _Raise:
    exc: "object | None"
    cause: "object | None"
    line: int


@dataclass
class _Try:
    body: list
    handlers: list  # list of (exc_type, as_name, body)
    else_body: list
    finally_body: list
    line: int


@dataclass
class _Attr:
    obj: object
    name: str
    line: int


@dataclass
class _Subscript:
    obj: object
    idx: object
    line: int


@dataclass
class _Slice:
    lo: object
    hi: object
    step: object
    line: int


@dataclass
class _List:
    elems: list
    line: int


@dataclass
class _Tuple:
    elems: list
    line: int


@dataclass
class _UnaryOp:
    op: str
    operand: object
    line: int


@dataclass
class _BoolOp:
    op: str  # "and" / "or"
    values: list
    line: int


def _extend_boolop(lhs, op: str, rhs, line: int) -> _BoolOp:
    if isinstance(lhs, _BoolOp) and lhs.op == op:
        values = list(lhs.values)
        values.append(rhs)
        return _BoolOp(op=op, values=values, line=line)
    return _BoolOp(op=op, values=[lhs, rhs], line=line)


@dataclass
class _Compare:
    op: str
    lhs: object
    rhs: object
    line: int


@dataclass
class _Bool:
    value: bool
    line: int


@dataclass
class _None:
    line: int


@dataclass
class _Dict:
    keys: list
    values: list
    line: int


@dataclass
class _DictCompElt:
    key: object
    value: object
    line: int


@dataclass
class _Set:
    elems: list
    line: int


@dataclass
class _Lambda:
    params: list  # same shape as _FuncDef.params
    body: object
    line: int


@dataclass
class _Del:
    targets: list
    line: int


@dataclass
class _Ternary:
    then_expr: object
    cond: object
    else_expr: object
    line: int


@dataclass
class _Comp:
    kind: str  # "list" / "set" / "dict" / "gen"
    elt: object  # _DictCompElt for dict comprehensions
    generators: list  # list of (target, iter, [ifs])
    line: int


@dataclass
class _Yield:
    value: "object | None"
    is_from: bool
    line: int


@dataclass
class _Starred:
    value: object
    is_kw: bool  # ``*x`` vs ``**x``
    line: int


@dataclass
class _Global:
    names: list
    line: int


@dataclass
class _Nonlocal:
    names: list
    line: int


@dataclass
class _With:
    items: list  # list of (ctx_expr, as_name)
    body: list
    line: int
    is_async: bool = False


@dataclass
class _Assert:
    test: object
    msg: "object | None"
    line: int


@dataclass
class _MatchAs:
    pattern: "object | None"
    name: str
    line: int


@dataclass
class _FStringText:
    text: str
    is_raw: bool
    line: int


@dataclass
class _FString:
    parts: list  # mix of _FStringText and embedded expr nodes
    line: int


@dataclass
class _FStringFormat:
    expr: object
    conversion: "str | None"
    spec: "str | None"
    line: int


@dataclass
class _FStringExprParts:
    expr: str
    conversion: "str | None"
    spec: "str | None"


class Parser:
    """Hand-written recursive-descent parser over the py_lex token
    stream. Returns a narrow AST (the ``_*`` dataclasses above) that
    a companion lowering step (not yet written) will convert into
    :mod:`pcc.frontends.python.py_ast` nodes.
    """

    def __init__(self, src: str, filename: str = "<input>") -> None:
        self.filename = filename
        lexer = Lexer(src, filename)
        self.tokens: list[Token] = Lexer.tokenize(lexer)
        self.pos = 0
        self._match_counter = 0
        # Entered while parsing single-line suites produced by
        # ``_parse_block`` (for example ``if x: a(); b()``). In this
        # mode, semicolons stay available for the block parser to consume
        # so it can continue parsing subsequent statements on the same line.
        self._in_single_line_block = False

    # --------------------------------------------------- token helpers

    def _peek(self, off: int = 0) -> Token:
        t = self.tokens[min(self.pos + off, len(self.tokens) - 1)]
        if t is None:
            raise RuntimeError(
                f"lexer token table entry is None at pos={self.pos + off}"
            )
        if not hasattr(t, "kind"):
            raise RuntimeError(
                f"lexer token table entry missing kind at pos={self.pos + off}: "
                f"{type(t)}"
            )
        return t

    def _advance(self) -> Token:
        t = self.tokens[self.pos]
        if self.pos < len(self.tokens) - 1:
            self.pos += 1
        return t

    def _accept(self, kind: str, text: str | None = None) -> Token | None:
        t_idx = self.pos
        if t_idx >= len(self.tokens):
            return None
        t = self.tokens[t_idx]
        if t.kind != kind:
            return None
        if text is not None and t.text != text:
            return None
        self._advance()
        return t

    def _expect(self, kind: str, text: str | None = None) -> Token:
        # Treat EOF as an implicit NEWLINE — files without a trailing
        # newline are common enough to tolerate.
        if kind == TK_NEWLINE:
            p = self._peek()
            if p.kind == TK_EOF:
                return p
            # ``a; b`` — semicolon is a soft statement terminator.
            if p.kind == TK_OP and p.text == ";":
                if self._in_single_line_block:
                    return p
                return self._advance()
        t = self._accept(kind, text)
        if t is None:
            got = self._peek()
            exp = text or kind
            raise ParseError(
                self.filename
                + ": expected "
                + exp
                + ", got "
                + got.kind
                + " "
                + got.text
            )
        return t

    def _skip_newlines(self) -> None:
        while self._accept(TK_NEWLINE) is not None:
            pass

    # --------------------------------------------------- entry

    def parse_module(self) -> _Module:
        stmts: list = []
        stmt_index = 0
        debug_parse = _debug_parse_enabled()
        while self._peek().kind != TK_EOF:
            self._skip_newlines()
            if self._peek().kind == TK_EOF:
                break
            start_token = self._peek()
            if debug_parse:
                _debug_parse(
                    "stmt_start index="
                    + str(stmt_index)
                    + " "
                    + _token_debug_text(start_token)
                )
            try:
                stmt = self._parse_stmt()
            except Exception as ex:
                raise ParseError(
                    "stmt #"
                    + str(stmt_index)
                    + " failed at "
                    + _token_debug_text(start_token)
                    + ": "
                    + type(ex).__name__
                    + ": "
                    + str(ex)
                )
            if debug_parse:
                _debug_parse("stmt_done index=" + str(stmt_index))
            if isinstance(stmt, list):
                stmts.extend(stmt)
            else:
                stmts.append(stmt)
            stmt_index += 1
        return _Module(body=stmts)

    # --------------------------------------------------- statements

    def _parse_stmt(self):
        t = self._peek()
        if t.kind == TK_KEYWORD:
            if t.text == "async":
                return self._parse_async_stmt()
            if t.text == "def":
                return self._parse_funcdef()
            if t.text == "return":
                return self._parse_return()
            if t.text == "if":
                return self._parse_if()
            if t.text == "pass":
                self._advance()
                self._expect(TK_NEWLINE)
                return _Pass(line=t.line)
            if t.text == "while":
                return self._parse_while()
            if t.text == "for":
                return self._parse_for()
            if t.text == "break":
                self._advance()
                self._expect(TK_NEWLINE)
                return _Break(line=t.line)
            if t.text == "continue":
                self._advance()
                self._expect(TK_NEWLINE)
                return _Continue(line=t.line)
            if t.text == "import":
                return self._parse_import()
            if t.text == "from":
                return self._parse_import_from()
            if t.text == "class":
                return self._parse_classdef(())
            if t.text == "raise":
                return self._parse_raise()
            if t.text == "try":
                return self._parse_try()
            if t.text == "del":
                return self._parse_del()
            if t.text == "global":
                return self._parse_global_or_nonlocal("global")
            if t.text == "nonlocal":
                return self._parse_global_or_nonlocal("nonlocal")
            if t.text == "with":
                return self._parse_with()
            if t.text == "assert":
                return self._parse_assert()
        if t.kind == TK_NAME and t.text == "match" and self._looks_like_match_stmt():
            return self._parse_match()
        if t.kind == TK_OP and t.text == "@":
            # Decorator chain — followed by either ``def`` or ``class``.
            return self._parse_decorated()
        return self._parse_simple_stmt()

    def _looks_like_match_stmt(self) -> bool:
        """Return true only for soft-keyword ``match subject:``.

        ``match`` remains a legal identifier in Python. The lexer keeps
        it as ``NAME`` already, so the parser must only enter pattern
        matching when a statement-level colon terminates the subject.
        """
        depth = 0
        i = 1
        saw_subject_token = False
        assignment_ops = (
            "=",
            "+=",
            "-=",
            "*=",
            "/=",
            "//=",
            "%=",
            "**=",
            "&=",
            "|=",
            "^=",
            "<<=",
            ">>=",
            ":=",
        )
        while True:
            tok = self._peek(i)
            if tok.kind in (TK_NEWLINE, TK_EOF, TK_INDENT, TK_DEDENT):
                return False
            if tok.kind == TK_OP:
                if tok.text in ("(", "[", "{"):
                    depth += 1
                elif tok.text in (")", "]", "}"):
                    if depth > 0:
                        depth -= 1
                elif depth == 0:
                    if tok.text in assignment_ops or tok.text == ";":
                        return False
                    if tok.text == ":":
                        return saw_subject_token
            saw_subject_token = True
            i += 1

    def _parse_del(self) -> _Del:
        kw = self._expect(TK_KEYWORD, "del")
        targets = [self._parse_expr()]
        while self._accept(TK_OP, ","):
            targets.append(self._parse_expr())
        self._expect(TK_NEWLINE)
        return _Del(targets=targets, line=kw.line)

    def _parse_global_or_nonlocal(self, which: str):
        kw = self._advance()
        first = self._expect(TK_NAME)
        names = [first.text]
        while self._accept(TK_OP, ","):
            tok = self._expect(TK_NAME)
            names.append(tok.text)
        self._expect(TK_NEWLINE)
        if which == "global":
            return _Global(names=names, line=kw.line)
        return _Nonlocal(names=names, line=kw.line)

    def _parse_with(
        self, *, is_async: bool = False, line_override: int | None = None
    ) -> _With:
        kw = self._expect(TK_KEYWORD, "with")
        start = self.pos
        parenthesized = self._accept(TK_OP, "(") is not None
        items = [self._parse_with_item()]
        while self._accept(TK_OP, ","):
            if (
                parenthesized
                and self._peek().kind == TK_OP
                and self._peek().text == ")"
            ):
                break
            items.append(self._parse_with_item())
        if parenthesized:
            self._expect(TK_OP, ")")
            if len(items) == 1 and self._accept(TK_KEYWORD, "as"):
                tok = self._expect(TK_NAME)
                items[0] = (items[0][0], tok.text)
            elif not (self._peek().kind == TK_OP and self._peek().text == ":"):
                # ``with (path / name).open() as f:`` -- the parenthesis
                # opened an ordinary expression, not a group of with-items.
                self.pos = start
                items = [self._parse_with_item()]
                while self._accept(TK_OP, ","):
                    items.append(self._parse_with_item())
        self._expect(TK_OP, ":")
        body = self._parse_block()
        line = kw.line
        if line_override is not None:
            line = line_override
        return _With(
            items=items,
            body=body,
            line=line,
            is_async=is_async,
        )

    def _parse_with_item(self) -> tuple:
        ctx = self._parse_expr()
        as_name = None
        if self._accept(TK_KEYWORD, "as"):
            tok = self._expect(TK_NAME)
            as_name = tok.text
        return (ctx, as_name)

    def _parse_assert(self) -> _Assert:
        kw = self._expect(TK_KEYWORD, "assert")
        test = self._parse_expr()
        msg = None
        if self._accept(TK_OP, ","):
            msg = self._parse_expr()
        self._expect(TK_NEWLINE)
        return _Assert(test=test, msg=msg, line=kw.line)

    def _parse_match(self) -> list:
        kw = self._expect(TK_NAME, "match")
        subject = self._parse_expr()
        self._expect(TK_OP, ":")
        self._expect(TK_NEWLINE)
        self._skip_newlines()
        self._expect(TK_INDENT)

        temp = f"__pcc_match_{self._match_counter}"
        self._match_counter += 1
        subject_name = _Name(temp, kw.line)
        cases: list = []
        while self._peek().kind != TK_DEDENT:
            self._skip_newlines()
            if self._peek().kind == TK_DEDENT:
                break
            case_tok = self._expect(TK_NAME, "case")
            pattern = self._parse_expr()
            if self._accept(TK_KEYWORD, "as"):
                as_tok = self._expect(TK_NAME)
                pattern = _MatchAs(
                    pattern=pattern, name=as_tok.text, line=case_tok.line
                )
            self._expect(TK_OP, ":")
            body = self._parse_block()
            cases.append((pattern, body, case_tok.line))
        self._expect(TK_DEDENT)

        chain: list = []
        i = len(cases) - 1
        while i >= 0:
            pattern, body, line = cases[i]
            cond, bindings = self._match_pattern_condition_bindings(
                subject_name,
                pattern,
                line,
            )
            case_body = bindings + body
            if cond is None:
                chain = case_body
            else:
                chain = [_If(cond=cond, body=case_body, else_body=chain, line=line)]
            i -= 1
        return [
            _Assign(
                target=subject_name,
                value=subject,
                annotation=None,
                line=kw.line,
            )
        ] + chain

    def _match_pattern_condition_bindings(
        self,
        subject: _Name,
        pattern: object,
        line: int,
    ) -> tuple[object | None, list]:
        if isinstance(pattern, _MatchAs):
            cond = None
            bindings: list = []
            if pattern.pattern is not None:
                cond, bindings = self._match_pattern_condition_bindings(
                    subject,
                    pattern.pattern,
                    line,
                )
            if pattern.name != "_":
                bindings.append(
                    _Assign(
                        target=_Name(pattern.name, line),
                        value=subject,
                        annotation=None,
                        line=line,
                    )
                )
            return cond, bindings
        if isinstance(pattern, _Name):
            if pattern.ident == "_":
                return None, []
            return None, [
                _Assign(
                    target=_Name(pattern.ident, line),
                    value=subject,
                    annotation=None,
                    line=line,
                )
            ]
        if isinstance(pattern, _Call) and self._is_match_class_pattern(pattern):
            conds: list = [
                _Call(
                    func=_Name("isinstance", line),
                    args=[subject, pattern.func],
                    line=line,
                )
            ]
            bindings: list = []
            if len(pattern.args) == 1:
                arg = pattern.args[0]
                elem_cond, elem_bindings = self._match_pattern_condition_bindings(
                    subject,
                    arg,
                    line,
                )
                if elem_cond is not None:
                    conds.append(elem_cond)
                bindings.extend(elem_bindings)
            elif len(pattern.args) > 1:
                tuple_pattern = _Tuple(elems=pattern.args, line=line)
                elem_cond, elem_bindings = self._match_pattern_condition_bindings(
                    subject,
                    tuple_pattern,
                    line,
                )
                if elem_cond is not None:
                    conds.append(elem_cond)
                bindings.extend(elem_bindings)
            if len(conds) == 1:
                return conds[0], bindings
            return _BoolOp(op="and", values=conds, line=line), bindings
        if isinstance(pattern, (_Tuple, _List)):
            conds: list = [
                _Compare(
                    op="==",
                    lhs=_Call(func=_Name("len", line), args=[subject], line=line),
                    rhs=_Num(str(len(pattern.elems)), line, True),
                    line=line,
                )
            ]
            bindings: list = []
            for idx, elem in enumerate(pattern.elems):
                item = _Subscript(
                    obj=subject,
                    idx=_Num(str(idx), line, True),
                    line=line,
                )
                elem_cond, elem_bindings = self._match_pattern_condition_bindings(
                    item,
                    elem,
                    line,
                )
                if elem_cond is not None:
                    conds.append(elem_cond)
                bindings.extend(elem_bindings)
            if len(conds) == 1:
                cond = conds[0]
            else:
                cond = _BoolOp(op="and", values=conds, line=line)
            return cond, bindings
        if isinstance(pattern, (_Str, _Bytes, _Num, _ComplexNum, _Bool, _None)):
            return _Compare(op="==", lhs=subject, rhs=pattern, line=line), []
        return _Compare(op="==", lhs=subject, rhs=pattern, line=line), []

    def _is_match_class_pattern(self, pattern: _Call) -> bool:
        return isinstance(pattern.func, (_Name, _Attr))

    def _parse_simple_stmt(self):
        """Expression statement OR ``name (: ann)? = value`` assign OR
        ``target op= value`` augmented assign. Also handles tuple
        unpacking on the LHS: ``a, b = pair``."""
        start = self.pos
        if self._peek().kind == TK_OP and self._peek().text == "*":
            lhs = self._parse_list_item()
        else:
            lhs = self._parse_expr()
        # Tuple LHS: ``a, b = ...`` — fold the comma list into a _Tuple
        # so the assign target is a single node.
        if self._peek().kind == TK_OP and self._peek().text == ",":
            elems = [lhs]
            while self._accept(TK_OP, ","):
                p = self._peek()
                if p.kind == TK_NEWLINE:
                    break
                if p.kind == TK_OP and p.text in ("=", ":"):
                    break
                elems.append(self._parse_list_item())
            line = self._peek().line
            lhs = _Tuple(elems=elems, line=line)
        t = self._peek()
        if t.kind == TK_OP and t.text == ":":
            # Annotation. Consume ``: type`` and optional ``= value``.
            self._advance()
            ann_node = self._parse_type_expr()
            has_value = False
            if self._accept(TK_OP, "="):
                value = self._parse_list_item()
                has_value = True
            else:
                value = _None(line=t.line)
            self._expect(TK_NEWLINE)
            return _Assign(
                target=lhs,
                value=value,
                annotation=ann_node,
                line=t.line,
                has_value=has_value,
            )
        if t.kind == TK_OP and t.text == "=":
            self._advance()
            value = self._parse_list_item()
            # Support ``a, b = 1, 2``: tuple RHS without parens.
            if self._peek().kind == TK_OP and self._peek().text == ",":
                elems = [value]
                while self._accept(TK_OP, ","):
                    if self._peek().kind == TK_NEWLINE:
                        break
                    elems.append(self._parse_list_item())
                value = _Tuple(elems=elems, line=t.line)
            # Chained assignment ``a = b = c`` is parsed as repeated
            # ``= rhs`` — fold extras into nested assigns.
            while self._peek().kind == TK_OP and self._peek().text == "=":
                self._advance()
                nxt = self._parse_list_item()
                if self._peek().kind == TK_OP and self._peek().text == ",":
                    elems = [nxt]
                    while self._accept(TK_OP, ","):
                        if self._peek().kind == TK_NEWLINE:
                            break
                        elems.append(self._parse_list_item())
                    nxt = _Tuple(elems=elems, line=t.line)
                # Rewrite ``a = b = c`` as ``a = (b := c)`` — model
                # with a nested _Assign on the RHS.
                value = _Assign(
                    target=value,
                    value=nxt,
                    annotation=None,
                    line=t.line,
                )
            self._expect(TK_NEWLINE)
            return _Assign(target=lhs, value=value, annotation=None, line=t.line)
        if t.kind == TK_OP and t.text in (
            "+=",
            "-=",
            "*=",
            "/=",
            "//=",
            "%=",
            "**=",
            "&=",
            "|=",
            "^=",
            "<<=",
            ">>=",
        ):
            op_tok = self._advance()
            op = op_tok.text
            value = self._parse_expr()
            self._expect(TK_NEWLINE)
            return _AugAssign(target=lhs, op=op, value=value, line=t.line)
        # Plain expression statement.
        self._expect(TK_NEWLINE)
        return _Expr(expr=lhs, line=t.line)

    def _parse_while(self) -> _While:
        kw = self._expect(TK_KEYWORD, "while")
        cond = self._parse_expr()
        self._expect(TK_OP, ":")
        body = self._parse_block()
        else_body: list = []
        if self._peek().kind == TK_KEYWORD and self._peek().text == "else":
            self._advance()
            self._expect(TK_OP, ":")
            else_body = self._parse_block()
        return _While(cond=cond, body=body, else_body=else_body, line=kw.line)

    def _parse_for(
        self, *, is_async: bool = False, line_override: int | None = None
    ) -> _For:
        kw = self._expect(TK_KEYWORD, "for")
        target = self._parse_for_target()
        self._expect(TK_KEYWORD, "in")
        it = self._parse_expr()
        # ``for a in b, c, d:`` — implicit tuple iterable.
        if self._peek().kind == TK_OP and self._peek().text == ",":
            elems = [it]
            while self._accept(TK_OP, ","):
                if self._peek().text == ":":
                    break
                elems.append(self._parse_expr())
            it = _Tuple(elems=elems, line=kw.line)
        self._expect(TK_OP, ":")
        body = self._parse_block()
        else_body: list = []
        if self._peek().kind == TK_KEYWORD and self._peek().text == "else":
            self._advance()
            self._expect(TK_OP, ":")
            else_body = self._parse_block()
        line = kw.line
        if line_override is not None:
            line = line_override
        return _For(
            target=target,
            iter=it,
            body=body,
            else_body=else_body,
            line=line,
            is_async=is_async,
        )

    def _parse_for_target(self):
        """Loop variable: NAME, ``*name``, tuple ``(a, b)``, or comma-list ``a, b``."""
        first = self._parse_for_target_atom()
        if self._peek().kind == TK_OP and self._peek().text == ",":
            elems = [first]
            while self._accept(TK_OP, ","):
                if self._peek().kind == TK_KEYWORD and self._peek().text == "in":
                    break
                if self._peek().kind == TK_OP and self._peek().text in (
                    ")",
                    "]",
                ):
                    break
                elems.append(self._parse_for_target_atom())
            line = 0
            if hasattr(first, "line"):
                line = first.line
            return _Tuple(elems=elems, line=line)
        return first

    def _parse_for_target_atom(self):
        t = self._peek()
        if t.kind == TK_OP and t.text == "(":
            self._advance()
            inner = self._parse_for_target()
            self._expect(TK_OP, ")")
            return inner
        if t.kind == TK_OP and t.text == "[":
            self._advance()
            inner = self._parse_for_target()
            self._expect(TK_OP, "]")
            return inner
        if t.kind == TK_OP and t.text == "*":
            self._advance()
            name = self._expect(TK_NAME)
            return _Starred(
                value=_Name(name.text, name.line),
                is_kw=False,
                line=t.line,
            )
        name = self._expect(TK_NAME)
        node = _Name(name.text, name.line)
        # Allow dotted attr or subscript (rare but legal): ``for obj.x in ...``.
        while True:
            p = self._peek()
            if p.kind == TK_OP and p.text == ".":
                self._advance()
                attr_tok = self._expect(TK_NAME)
                attr = attr_tok.text
                node = _Attr(node, attr, p.line)
            elif p.kind == TK_OP and p.text == "[":
                self._advance()
                idx = self._parse_subscript()
                self._expect(TK_OP, "]")
                node = _Subscript(obj=node, idx=idx, line=p.line)
            else:
                break
        return node

    def _parse_import(self) -> _Import:
        kw = self._expect(TK_KEYWORD, "import")
        names: list = []
        names.append(self._parse_dotted_name_as_pair())
        while self._accept(TK_OP, ","):
            names.append(self._parse_dotted_name_as_pair())
        self._expect(TK_NEWLINE)
        return _Import(names=names, line=kw.line)

    def _parse_import_from(self) -> _ImportFrom:
        kw = self._expect(TK_KEYWORD, "from")
        level = 0
        while True:
            if self._accept(TK_OP, "."):
                level += 1
            elif self._accept(TK_OP, "..."):
                # The lexer keeps an ellipsis as one token in every context.
                # In a relative import it contributes three import levels.
                level += 3
            else:
                break
        if level and self._peek().kind == TK_KEYWORD:
            # Pure ``from . import x`` — no dotted-name portion.
            module = ""
        else:
            module = self._parse_dotted_name()
        self._expect(TK_KEYWORD, "import")
        names: list = []
        if self._accept(TK_OP, "("):
            names.append(self._parse_import_name_item())
            while self._accept(TK_OP, ","):
                if self._peek().text == ")":
                    break
                names.append(self._parse_import_name_item())
            self._expect(TK_OP, ")")
        else:
            names.append(self._parse_import_name_item())
            while self._accept(TK_OP, ","):
                names.append(self._parse_import_name_item())
        self._expect(TK_NEWLINE)
        return _ImportFrom(module=module, names=names, level=level, line=kw.line)

    def _parse_dotted_name_as_pair(self) -> tuple:
        mod = self._parse_dotted_name()
        as_name = None
        if self._accept(TK_KEYWORD, "as"):
            tok = self._expect(TK_NAME)
            as_name = tok.text
        return (mod, as_name)

    def _parse_import_name_item(self) -> tuple:
        # ``from x import *`` — star import.
        if self._peek().kind == TK_OP and self._peek().text == "*":
            self._advance()
            return ("*", None)
        name_tok = self._expect(TK_NAME)
        name = name_tok.text
        as_name = None
        if self._accept(TK_KEYWORD, "as"):
            as_tok = self._expect(TK_NAME)
            as_name = as_tok.text
        return (name, as_name)

    def _parse_dotted_name(self) -> str:
        first = self._expect(TK_NAME)
        parts = [first.text]
        while self._accept(TK_OP, "."):
            tok = self._expect(TK_NAME)
            parts.append(tok.text)
        return _join_strings(parts, ".")

    def _parse_raise(self) -> _Raise:
        kw = self._expect(TK_KEYWORD, "raise")
        exc = None
        cause = None
        if self._peek().kind != TK_NEWLINE:
            exc = self._parse_expr()
            if self._accept(TK_KEYWORD, "from"):
                cause = self._parse_expr()
        self._expect(TK_NEWLINE)
        return _Raise(exc=exc, cause=cause, line=kw.line)

    def _parse_try(self) -> _Try:
        kw = self._expect(TK_KEYWORD, "try")
        self._expect(TK_OP, ":")
        body = self._parse_block()
        handlers: list = []
        else_body: list = []
        finally_body: list = []
        while self._peek().kind == TK_KEYWORD and self._peek().text == "except":
            self._advance()
            exc_type = None
            as_name = None
            if self._peek().kind != TK_OP or self._peek().text != ":":
                exc_type = self._parse_expr()
                if self._accept(TK_KEYWORD, "as"):
                    as_tok = self._expect(TK_NAME)
                    as_name = as_tok.text
            self._expect(TK_OP, ":")
            handlers.append((exc_type, as_name, self._parse_block()))
        if self._peek().kind == TK_KEYWORD and self._peek().text == "else":
            self._advance()
            self._expect(TK_OP, ":")
            else_body = self._parse_block()
        if self._peek().kind == TK_KEYWORD and self._peek().text == "finally":
            self._advance()
            self._expect(TK_OP, ":")
            finally_body = self._parse_block()
        return _Try(
            body=body,
            handlers=handlers,
            else_body=else_body,
            finally_body=finally_body,
            line=kw.line,
        )

    def _parse_decorated(self):
        decorators: list = []
        while self._accept(TK_OP, "@"):
            decorators.append(self._parse_expr())
            self._expect(TK_NEWLINE)
        self._skip_newlines()
        t = self._peek()
        if t.kind == TK_KEYWORD and t.text == "def":
            return self._parse_funcdef(decorators=list(decorators))
        if t.kind == TK_KEYWORD and t.text == "async":
            fn = self._parse_async_stmt(decorators=list(decorators))
            if not isinstance(fn, _FuncDef):
                raise ParseError(
                    f"{self.filename}:{t.line}: expected async def after " "decorators"
                )
            return fn
        if t.kind == TK_KEYWORD and t.text == "class":
            return self._parse_classdef(tuple(decorators))
        raise ParseError(
            f"{self.filename}:{t.line}: expected def or class after " "decorators"
        )

    def _parse_classdef(self, decorators: tuple) -> _ClassDef:
        kw = self._expect(TK_KEYWORD, "class")
        name_tok = self._expect(TK_NAME)
        name = name_tok.text
        bases: list = []
        if self._accept(TK_OP, "("):
            if self._peek().text != ")":
                bases.append(self._parse_call_arg())
                while self._accept(TK_OP, ","):
                    if self._peek().text == ")":
                        break
                    bases.append(self._parse_call_arg())
            self._expect(TK_OP, ")")
        self._expect(TK_OP, ":")
        body = self._parse_block()
        return _ClassDef(
            name=name, bases=bases, body=body, decorators=list(decorators), line=kw.line
        )

    def _parse_async_stmt(self, decorators: list | None = None):
        kw = self._expect(TK_KEYWORD, "async")
        if self._peek().kind == TK_KEYWORD and self._peek().text == "def":
            return self._parse_funcdef(
                is_async=True,
                line_override=kw.line,
                decorators=decorators,
            )
        if self._peek().kind == TK_KEYWORD and self._peek().text == "for":
            return self._parse_for(is_async=True, line_override=kw.line)
        if self._peek().kind == TK_KEYWORD and self._peek().text == "with":
            return self._parse_with(is_async=True, line_override=kw.line)
        raise ParseError(f"{self.filename}:{kw.line}: expected async def/for/with")

    def _parse_funcdef(
        self,
        *,
        is_async: bool = False,
        line_override: int | None = None,
        decorators: list | None = None,
    ) -> _FuncDef:
        phase = "def"
        name = "<unknown>"
        try:
            kw = self._expect(TK_KEYWORD, "def")
            phase = "name"
            name_tok = self._expect(TK_NAME)
            name = name_tok.text
            if _debug_parse_enabled():
                _debug_parse("func_start name=" + name + " line=" + str(kw.line))
            phase = "open_paren"
            self._expect(TK_OP, "(")
            # Each element is a tuple (kind, name) where kind ∈
            # {"pos", "*args", "**kwargs"}. We drop type / default info at
            # the skeleton layer; the AST lift will pick them up once we
            # wire it in.
            params: list[tuple[str, str]] = []
            phase = "params"
            if self._peek().text != ")":
                params.append(self._parse_funcdef_param())
                while self._accept(TK_OP, ","):
                    if self._peek().text == ")":
                        break
                    params.append(self._parse_funcdef_param())
            # Parameter tokens are parsed independently, so names following
            # ``*`` / ``*args`` initially carry the ordinary ``pos`` marker,
            # and names preceding ``/`` do too.  Normalize the surrounding
            # separator semantics before py_lift constructs Arg nodes.  Keep
            # the empty separator entries themselves for the existing
            # closed-world representation; downstream runtime-argument lists
            # already exclude their empty names.
            kw_only_mode = False
            for param_index in range(len(params)):
                param = params[param_index]
                param_kind = param[0]
                if param_kind == "pos-only-sep":
                    for previous_index in range(param_index):
                        previous = params[previous_index]
                        if previous[0] == "pos":
                            params[previous_index] = (
                                "pos_only",
                                previous[1],
                                previous[2],
                                previous[3],
                            )
                    continue
                if param_kind == "kwonly-sep":
                    kw_only_mode = True
                    continue
                if param_kind == "*args":
                    kw_only_mode = True
                    continue
                if param_kind == "pos" and kw_only_mode:
                    params[param_index] = (
                        "kw_only",
                        param[1],
                        param[2],
                        param[3],
                    )
            phase = "close_paren"
            self._expect(TK_OP, ")")
            # Optional ``-> type`` return annotation.
            returns = None
            phase = "returns"
            if self._accept(TK_OP, "->"):
                returns = self._parse_type_expr()
            phase = "colon"
            self._expect(TK_OP, ":")
            phase = "body"
            body = self._parse_block()
            phase = "construct"
            line = kw.line
            if line_override is not None:
                line = line_override
            decorators_list = []
            if decorators is not None:
                decorators_list = decorators
            return _FuncDef(
                name=name,
                params=params,
                body=body,
                line=line,
                decorators=decorators_list,
                returns=returns,
                is_async=is_async,
            )
        except Exception as ex:
            raise ParseError(
                "funcdef "
                + name
                + " failed phase="
                + phase
                + " at "
                + _token_debug_text(self._peek())
                + ": "
                + type(ex).__name__
                + ": "
                + str(ex)
            )

    def _parse_funcdef_param(self, in_lambda: bool = False):
        """Parse one parameter. Returns a 4-tuple
        ``(kind, name, annotation, default)`` — ``annotation`` is the
        AST node for the ``: T`` part (or ``None``), ``default`` the
        value expression after ``=`` (or ``None``)."""
        t = self._peek()
        if t.kind == TK_OP and t.text == "*":
            self._advance()
            if self._peek().kind == TK_OP and self._peek().text in (",", ":"):
                return ("kwonly-sep", "", None, None)
            pname_tok = self._expect(TK_NAME)
            pname = pname_tok.text
            ann = None
            if not in_lambda and self._accept(TK_OP, ":"):
                ann = self._parse_type_expr()
            return ("*args", pname, ann, None)
        if t.kind == TK_OP and t.text == "**":
            self._advance()
            pname_tok = self._expect(TK_NAME)
            pname = pname_tok.text
            ann = None
            if not in_lambda and self._accept(TK_OP, ":"):
                ann = self._parse_type_expr()
            return ("**kwargs", pname, ann, None)
        if t.kind == TK_OP and t.text == "/":
            self._advance()
            return ("pos-only-sep", "", None, None)
        pname_tok = self._expect(TK_NAME)
        pname = pname_tok.text
        ann = None
        default = None
        if not in_lambda and self._accept(TK_OP, ":"):
            ann = self._parse_type_expr()
        if self._accept(TK_OP, "="):
            default = self._parse_expr()
        return ("pos", pname, ann, default)

    def _parse_type_expr(self):
        """Parse a type expression and return it as an expression AST
        node. Accepts Names, singletons (``None`` / ``True`` / ``False``),
        chained ``a.b``, string forward refs, tuple shorthand, ``[X]``
        subscripts, and PEP 604 union ``A | B``."""
        lhs = self._parse_type_atom()
        while self._accept(TK_OP, "|"):
            rhs = self._parse_type_atom()
            line = 0
            try:
                line = lhs.line
            except AttributeError:
                line = self._peek().line
            lhs = _BinOp(op="|", lhs=lhs, rhs=rhs, line=line)
        return lhs

    def _parse_type_atom(self):
        t = self._peek()
        if t.kind == TK_OP and t.text == "...":
            # Ellipsis — ``Callable[..., T]`` / ``tuple[T, ...]``.
            self._advance()
            node = _Name(ident="...", line=t.line)
            return node
        if t.kind == TK_KEYWORD and t.text in ("None", "True", "False"):
            self._advance()
            if t.text == "None":
                node = _None(line=t.line)
            else:
                is_true = t.text == "True"
                node = _Bool(value=is_true, line=t.line)
        elif t.kind == TK_OP and t.text == "(":
            self._advance()
            elems: list = []
            if not self._accept(TK_OP, ")"):
                elems.append(self._parse_type_expr())
                while self._accept(TK_OP, ","):
                    if self._peek().text == ")":
                        break
                    elems.append(self._parse_type_expr())
                self._expect(TK_OP, ")")
            node = _Tuple(elems=elems, line=t.line)
        elif t.kind == TK_NAME:
            self._advance()
            node = _Name(ident=t.text, line=t.line)
            while self._accept(TK_OP, "."):
                attr_tok = self._expect(TK_NAME)
                node = _Attr(node, attr_tok.text, attr_tok.line)
        elif t.kind == TK_STRING:
            self._advance()
            piece = self._string_piece(t.text)
            node = _Str(parts=(piece,), line=t.line)
        elif t.kind == TK_NUMBER:
            self._advance()
            clean = t.text.replace("_", "")
            low = clean.lower()
            is_int = (
                low.startswith("0x")
                or low.startswith("0o")
                or low.startswith("0b")
                or ("." not in clean and "e" not in low and not low.endswith("j"))
            )
            node = _Num(text=clean, line=t.line, is_int=is_int)
        elif t.kind == TK_OP and t.text == "*":
            self._advance()
            inner = self._parse_type_atom()
            node = _Starred(value=inner, is_kw=False, line=t.line)
        else:
            return _None(line=t.line)
        if self._accept(TK_OP, "["):
            elems = []
            if not self._accept(TK_OP, "]"):
                elems.append(self._parse_type_subscript_elem())
                while self._accept(TK_OP, ","):
                    if self._peek().text == "]":
                        break
                    elems.append(self._parse_type_subscript_elem())
                self._expect(TK_OP, "]")
            idx = elems[0] if len(elems) == 1 else _Tuple(elems=elems, line=t.line)
            node = _Subscript(obj=node, idx=idx, line=t.line)
        return node

    def _parse_type_subscript_elem(self):
        """One subscript item — may be a nested list ``[A, B]`` for
        ``Callable[[A, B], R]`` signatures."""
        if self._peek().kind == TK_OP and self._peek().text == "[":
            start = self._advance()
            elems = []
            if not self._accept(TK_OP, "]"):
                elems.append(self._parse_type_expr())
                while self._accept(TK_OP, ","):
                    if self._peek().text == "]":
                        break
                    elems.append(self._parse_type_expr())
                self._expect(TK_OP, "]")
            return _List(elems=elems, line=start.line)
        return self._parse_type_expr()

    def _parse_return(self) -> _Return:
        kw = self._expect(TK_KEYWORD, "return")
        if self._peek().kind == TK_NEWLINE:
            self._advance()
            return _Return(value=None, line=kw.line)
        value = self._parse_list_item()
        # Implicit tuple: ``return a, b``.
        if self._peek().kind == TK_OP and self._peek().text == ",":
            elems = [value]
            while self._accept(TK_OP, ","):
                if self._peek().kind == TK_NEWLINE:
                    break
                elems.append(self._parse_list_item())
            value = _Tuple(elems=elems, line=kw.line)
        self._expect(TK_NEWLINE)
        return _Return(value=value, line=kw.line)

    def _parse_if(self) -> _If:
        kw = self._expect(TK_KEYWORD, "if")
        cond = self._parse_expr()
        self._expect(TK_OP, ":")
        body = self._parse_block()
        else_body: list = []
        if self._peek().kind == TK_KEYWORD and self._peek().text == "elif":
            else_body = [self._parse_if_elif()]
        elif self._peek().kind == TK_KEYWORD and self._peek().text == "else":
            self._advance()
            self._expect(TK_OP, ":")
            else_body = self._parse_block()
        return _If(cond=cond, body=body, else_body=else_body, line=kw.line)

    def _parse_if_elif(self) -> _If:
        # Same shape as ``_parse_if`` but for the ``elif`` keyword.
        kw = self._expect(TK_KEYWORD, "elif")
        cond = self._parse_expr()
        self._expect(TK_OP, ":")
        body = self._parse_block()
        else_body: list = []
        if self._peek().kind == TK_KEYWORD and self._peek().text == "elif":
            else_body = [self._parse_if_elif()]
        elif self._peek().kind == TK_KEYWORD and self._peek().text == "else":
            self._advance()
            self._expect(TK_OP, ":")
            else_body = self._parse_block()
        return _If(cond=cond, body=body, else_body=else_body, line=kw.line)

    def _parse_block(self) -> list:
        # Single-line suite: ``if x: return 1`` — body follows on the
        # same line instead of indenting. CPython supports this for
        # most compound statements.
        if self._peek().kind != TK_NEWLINE:
            saved_single_line = self._in_single_line_block
            self._in_single_line_block = True
            stmts: list = []
            block_index = 0
            debug_parse = _debug_parse_enabled()
            try:
                start_token = self._peek()
                if debug_parse:
                    _debug_parse(
                        "block_stmt_start index="
                        + str(block_index)
                        + " "
                        + _token_debug_text(start_token)
                    )
                try:
                    stmt = self._parse_stmt()
                except Exception as ex:
                    raise ParseError(
                        "block stmt #"
                        + str(block_index)
                        + " failed at "
                        + _token_debug_text(start_token)
                        + ": "
                        + type(ex).__name__
                        + ": "
                        + str(ex)
                    )
                if debug_parse:
                    _debug_parse("block_stmt_done index=" + str(block_index))
                if isinstance(stmt, list):
                    stmts.extend(stmt)
                else:
                    stmts.append(stmt)
                block_index += 1
                while self._peek().kind == TK_OP and self._peek().text == ";":
                    self._advance()
                    if self._peek().kind == TK_NEWLINE:
                        break
                    start_token = self._peek()
                    if debug_parse:
                        _debug_parse(
                            "block_stmt_start index="
                            + str(block_index)
                            + " "
                            + _token_debug_text(start_token)
                        )
                    try:
                        stmt = self._parse_stmt()
                    except Exception as ex:
                        raise ParseError(
                            "block stmt #"
                            + str(block_index)
                            + " failed at "
                            + _token_debug_text(start_token)
                            + ": "
                            + type(ex).__name__
                            + ": "
                            + str(ex)
                        )
                    if debug_parse:
                        _debug_parse("block_stmt_done index=" + str(block_index))
                    if isinstance(stmt, list):
                        stmts.extend(stmt)
                    else:
                        stmts.append(stmt)
                    block_index += 1
            finally:
                self._in_single_line_block = saved_single_line
            return stmts
        self._expect(TK_NEWLINE)
        # Allow blank / comment-only lines between the colon and the
        # indented block — the lexer emits a NEWLINE for each.
        self._skip_newlines()
        self._expect(TK_INDENT)
        stmts = []
        block_index = 0
        debug_parse = _debug_parse_enabled()
        while self._peek().kind != TK_DEDENT:
            self._skip_newlines()
            if self._peek().kind == TK_DEDENT:
                break
            start_token = self._peek()
            if debug_parse:
                _debug_parse(
                    "block_stmt_start index="
                    + str(block_index)
                    + " "
                    + _token_debug_text(start_token)
                )
            try:
                stmt = self._parse_stmt()
            except Exception as ex:
                raise ParseError(
                    "block stmt #"
                    + str(block_index)
                    + " failed at "
                    + _token_debug_text(start_token)
                    + ": "
                    + type(ex).__name__
                    + ": "
                    + str(ex)
                )
            if debug_parse:
                _debug_parse("block_stmt_done index=" + str(block_index))
            if isinstance(stmt, list):
                stmts.extend(stmt)
            else:
                stmts.append(stmt)
            block_index += 1
        self._expect(TK_DEDENT)
        return stmts

    def _parse_expr_stmt(self) -> _Expr:
        e = self._parse_expr()
        line = self._peek().line
        self._expect(TK_NEWLINE)
        return _Expr(expr=e, line=line)

    # --------------------------------------------------- expressions
    #
    # Precedence ladder (narrow but covers pcc source):
    #   or       -> or_term (or or_term)*
    #   or_term  -> and_term (and and_term)*
    #   and_term -> not_term
    #   not_term -> 'not' not_term | compare
    #   compare  -> add ((<|>|<=|>=|==|!=|is|in|'not' 'in') add)*
    #   add      -> mul ((+|-) mul)*
    #   mul      -> unary ((*|/|//|%) unary)*
    #   unary    -> (+|-|~) unary | power
    #   power    -> atom trailer*        trailer = (args) | [idx] | .name
    #   atom     -> NUMBER | STRING | NAME | True | False | None |
    #               '(' expr ')' | '[' items ']' | '(' items ',' ')'

    def _parse_expr(self):
        # Top-level expression accepts ``lambda``, ``yield``, ternary
        # ``a if b else c`` and walrus ``name := expr``. Ternary has the
        # lowest precedence above ``or``; lambda / yield / walrus are
        # parallel entry points.
        t = self._peek()
        if t.kind == TK_KEYWORD and t.text == "lambda":
            return self._parse_lambda()
        if t.kind == TK_KEYWORD and t.text == "yield":
            return self._parse_yield_expr()
        # Walrus: ``name := expr``. Only when the next-next token is
        # exactly ``:=``.
        if (
            t.kind == TK_NAME
            and self._peek(1).kind == TK_OP
            and self._peek(1).text == ":="
        ):
            target_tok = self._advance()
            self._advance()  # ':='
            val = self._parse_expr()
            # Model as an _Assign so the lowering stage can see it
            # as both an expression and a binding.
            return _Assign(
                target=_Name(target_tok.text, target_tok.line),
                value=val,
                annotation="walrus",
                line=target_tok.line,
            )
        lhs = self._parse_or()
        if self._peek().kind == TK_KEYWORD and self._peek().text == "if":
            self._advance()
            cond = self._parse_or()
            self._expect(TK_KEYWORD, "else")
            else_expr = self._parse_expr()
            line = self._peek().line
            return _Ternary(
                then_expr=lhs,
                cond=cond,
                else_expr=else_expr,
                line=line,
            )
        return lhs

    def _parse_lambda(self):
        kw = self._expect(TK_KEYWORD, "lambda")
        params: list = []
        if self._peek().text != ":":
            params.append(self._parse_funcdef_param(in_lambda=True))
            while self._accept(TK_OP, ","):
                params.append(self._parse_funcdef_param(in_lambda=True))
        self._expect(TK_OP, ":")
        body = self._parse_expr()
        return _Lambda(params=params, body=body, line=kw.line)

    def _parse_yield_expr(self):
        kw = self._expect(TK_KEYWORD, "yield")
        if self._accept(TK_KEYWORD, "from"):
            val = self._parse_expr()
            return _Yield(value=val, is_from=True, line=kw.line)
        # Empty ``yield`` allowed as a standalone expression.
        t = self._peek()
        if t.kind == TK_NEWLINE or (
            t.kind == TK_OP and t.text in (")", "]", "}", ",", ":")
        ):
            return _Yield(value=None, is_from=False, line=kw.line)
        val = self._parse_list_item()
        # Implicit tuple: ``yield a, b`` yields the tuple ``(a, b)`` (matches
        # CPython and ``return a, b``).  Without this the trailing ``, b`` is
        # consumed by the enclosing testlist as ``(yield a), b``, which leaks
        # the ``_yield`` sentinel to runtime as ``NameError: _yield``.
        if self._peek().kind == TK_OP and self._peek().text == ",":
            elems = [val]
            while self._accept(TK_OP, ","):
                nt = self._peek()
                if nt.kind == TK_NEWLINE or (
                    nt.kind == TK_OP and nt.text in (")", "]", "}", ":", "=")
                ):
                    break
                elems.append(self._parse_list_item())
            val = _Tuple(elems=elems, line=kw.line)
        return _Yield(value=val, is_from=False, line=kw.line)

    def _parse_or(self):
        lhs = self._parse_and()
        while self._peek().kind == TK_KEYWORD and self._peek().text == "or":
            self._advance()
            rhs = self._parse_and()
            line = self._peek().line
            lhs = _extend_boolop(lhs, "or", rhs, line)
        return lhs

    def _parse_and(self):
        lhs = self._parse_not()
        while self._peek().kind == TK_KEYWORD and self._peek().text == "and":
            self._advance()
            rhs = self._parse_not()
            line = self._peek().line
            lhs = _extend_boolop(lhs, "and", rhs, line)
        return lhs

    def _parse_not(self):
        if self._peek().kind == TK_KEYWORD and self._peek().text == "not":
            kw = self._advance()
            inner = self._parse_not()
            return _UnaryOp(op="not", operand=inner, line=kw.line)
        return self._parse_compare()

    def _parse_compare(self):
        lhs = self._parse_bitor()
        comparisons = []
        prev = lhs
        op = None
        while True:
            t = self._peek()
            line = t.line
            if t.kind == TK_OP and t.text in (
                "<",
                ">",
                "<=",
                ">=",
                "==",
                "!=",
            ):
                op_tok = self._advance()
                op = op_tok.text
            elif t.kind == TK_KEYWORD and t.text == "is":
                self._advance()
                if self._accept(TK_KEYWORD, "not"):
                    op = "is not"
                else:
                    op = "is"
            elif t.kind == TK_KEYWORD and t.text == "in":
                self._advance()
                op = "in"
            elif (
                t.kind == TK_KEYWORD
                and t.text == "not"
                and self._peek(1).kind == TK_KEYWORD
                and self._peek(1).text == "in"
            ):
                self._advance()  # not
                self._advance()  # in
                op = "not in"
            else:
                break
            rhs = self._parse_bitor()
            comparisons.append(_Compare(op=op, lhs=prev, rhs=rhs, line=line))
            prev = rhs
        if not comparisons:
            return lhs
        if len(comparisons) == 1:
            return comparisons[0]
        line = comparisons[0].line
        return _BoolOp(op="and", values=comparisons, line=line)

    def _parse_bitor(self):
        lhs = self._parse_bitxor()
        while self._peek().kind == TK_OP and self._peek().text == "|":
            self._advance()
            rhs = self._parse_bitxor()
            line = self._peek().line
            lhs = _BinOp(op="|", lhs=lhs, rhs=rhs, line=line)
        return lhs

    def _parse_bitxor(self):
        lhs = self._parse_bitand()
        while self._peek().kind == TK_OP and self._peek().text == "^":
            self._advance()
            rhs = self._parse_bitand()
            line = self._peek().line
            lhs = _BinOp(op="^", lhs=lhs, rhs=rhs, line=line)
        return lhs

    def _parse_bitand(self):
        lhs = self._parse_shift()
        while self._peek().kind == TK_OP and self._peek().text == "&":
            self._advance()
            rhs = self._parse_shift()
            line = self._peek().line
            lhs = _BinOp(op="&", lhs=lhs, rhs=rhs, line=line)
        return lhs

    def _parse_shift(self):
        lhs = self._parse_add()
        while self._peek().kind == TK_OP and self._peek().text in ("<<", ">>"):
            op_tok = self._advance()
            op = op_tok.text
            rhs = self._parse_add()
            line = self._peek().line
            lhs = _BinOp(op=op, lhs=lhs, rhs=rhs, line=line)
        return lhs

    def _parse_add(self):
        lhs = self._parse_mul()
        while self._peek().kind == TK_OP and self._peek().text in ("+", "-"):
            op_tok = self._advance()
            op = op_tok.text
            rhs = self._parse_mul()
            line = self._peek().line
            lhs = _BinOp(op=op, lhs=lhs, rhs=rhs, line=line)
        return lhs

    def _parse_mul(self):
        lhs = self._parse_unary()
        while self._peek().kind == TK_OP and self._peek().text in (
            "*",
            "/",
            "//",
            "%",
            "@",
        ):
            op_tok = self._advance()
            op = op_tok.text
            rhs = self._parse_unary()
            line = self._peek().line
            lhs = _BinOp(op=op, lhs=lhs, rhs=rhs, line=line)
        return lhs

    def _parse_unary(self):
        t = self._peek()
        if t.kind == TK_KEYWORD and t.text == "await":
            self._advance()
            return _Await(value=self._parse_power(), line=t.line)
        if t.kind == TK_OP and t.text in ("-", "+", "~"):
            self._advance()
            inner = self._parse_unary()
            return _UnaryOp(op=t.text, operand=inner, line=t.line)
        return self._parse_power()

    def _parse_power(self):
        lhs = self._parse_atom_trailer()
        if self._peek().kind == TK_OP and self._peek().text == "**":
            self._advance()
            rhs = self._parse_unary()  # right-associative
            line = self._peek().line
            return _BinOp(op="**", lhs=lhs, rhs=rhs, line=line)
        return lhs

    @staticmethod
    def _string_prefix(raw: str) -> str:
        i = 0
        while i < len(raw) and raw[i] not in ("'", '"'):
            i += 1
        return raw[:i]

    @classmethod
    def _string_body(cls, raw: str) -> str:
        """Strip quotes and the optional prefix (``b``/``f``/``r``/``u``
        and 2-char combinations) from a STRING token's raw text.

        Returns the string body *with escapes unprocessed* — the
        lowering stage owns escape handling because it needs to know
        whether the prefix said ``r`` (raw, no processing) or not."""
        rest = raw[len(cls._string_prefix(raw)) :]
        if rest[:3] in ('"""', "'''"):
            return rest[3:-3]
        return rest[1:-1]

    @classmethod
    def _string_piece(cls, raw: str) -> tuple[str, bool]:
        prefix = cls._string_prefix(raw).lower()
        return (cls._string_body(raw), "r" in prefix)

    @classmethod
    def _string_is_f(cls, raw: str) -> bool:
        return "f" in cls._string_prefix(raw).lower()

    @classmethod
    def _string_is_b(cls, raw: str) -> bool:
        return "b" in cls._string_prefix(raw).lower()

    @staticmethod
    def _split_fstring_expr_parts(
        text: str,
    ) -> _FStringExprParts:
        depth = 0
        quote = ""
        triple = False
        i = 0
        while i < len(text):
            ch = text[i]
            if quote:
                if ch == "\\":
                    i += 2
                    continue
                if triple:
                    if (
                        ch == quote
                        and i + 2 < len(text)
                        and text[i + 1] == quote
                        and text[i + 2] == quote
                    ):
                        quote = ""
                        triple = False
                        i += 3
                        continue
                elif ch == quote:
                    quote = ""
                    i += 1
                    continue
                i += 1
                continue
            if ch in ("'", '"'):
                quote = ch
                triple = i + 2 < len(text) and text[i + 1] == ch and text[i + 2] == ch
                i += 3 if triple else 1
                continue
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                if depth > 0:
                    depth -= 1
            elif depth == 0 and ch == "!":
                expr = text[:i].strip()
                if i + 1 >= len(text):
                    return _FStringExprParts(expr, "", None)
                conversion = text[i + 1]
                spec = None
                if i + 2 < len(text) and text[i + 2] == ":":
                    # The format spec is taken literally (CPython does NOT strip
                    # it): a leading space is the space-sign option, e.g.
                    # f"{x: d}". Only the expr part is stripped.
                    spec = text[i + 3 :]
                return _FStringExprParts(expr, conversion, spec)
            elif depth == 0 and ch == ":":
                return _FStringExprParts(text[:i].strip(), None, text[i + 1 :])
            i += 1
        return _FStringExprParts(text.strip(), None, None)

    @classmethod
    def _split_fstring_expr(cls, text: str) -> str:
        return cls._split_fstring_expr_parts(text).expr

    def _parse_fstring_expr(self, text: str, line: int):
        expr_text = ""
        conversion = None
        format_spec = None
        depth = 0
        quote = ""
        triple = False
        found_split = False
        i = 0
        while i < len(text):
            ch = text[i]
            if quote:
                if ch == "\\":
                    i += 2
                    continue
                if triple:
                    if (
                        ch == quote
                        and i + 2 < len(text)
                        and text[i + 1] == quote
                        and text[i + 2] == quote
                    ):
                        quote = ""
                        triple = False
                        i += 3
                        continue
                elif ch == quote:
                    quote = ""
                    i += 1
                    continue
                i += 1
                continue
            if ch in ("'", '"'):
                quote = ch
                triple = i + 2 < len(text) and text[i + 1] == ch and text[i + 2] == ch
                i += 3 if triple else 1
                continue
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                if depth > 0:
                    depth -= 1
            elif depth == 0 and ch == "!":
                expr_text = text[:i].strip()
                if i + 1 >= len(text):
                    conversion = ""
                else:
                    conversion = text[i + 1]
                    if i + 2 < len(text) and text[i + 2] == ":":
                        # Format spec is literal (CPython does not strip it): a
                        # leading space is the space-sign option, f"{x: d}".
                        format_spec = text[i + 3 :]
                found_split = True
                break
            elif depth == 0 and ch == ":":
                expr_text = text[:i].strip()
                format_spec = text[i + 1 :]
                found_split = True
                break
            i += 1
        if not found_split:
            expr_text = text.strip()
        debug_prefix = None
        if expr_text.endswith("="):
            debug_prefix = expr_text
            expr_text = expr_text[:-1].rstrip()
        if not expr_text:
            raise ParseError(
                self.filename + ":" + str(line) + ": empty f-string expression"
            )
        parser = Parser(expr_text, self.filename)
        # Use explicit unbound calls for the nested parser. In self-hosted
        # pcc-py, a fresh local Parser alias can lose method-dispatch metadata
        # and return NULL for parser._parse_expr().
        expr = Parser._parse_expr(parser)
        while Parser._peek(parser).kind == TK_NEWLINE:
            Parser._advance(parser)
        if Parser._peek(parser).kind != TK_EOF:
            t = Parser._peek(parser)
            raise ParseError(
                self.filename
                + ":"
                + str(line)
                + ": trailing f-string expression input near "
                + repr(t.text)
            )
        if conversion is not None and conversion != "":
            if conversion != "r" and conversion != "s" and conversion != "a":
                raise ParseError(
                    self.filename
                    + ":"
                    + str(line)
                    + ": unsupported f-string conversion !"
                    + conversion
                )
        if debug_prefix is None and conversion is None and format_spec is None:
            return expr
        if debug_prefix is not None and conversion is None and format_spec is None:
            # CPython: ``f"{x=}"`` defaults to repr (``f"x={x!r}"``), but
            # ``f"{x=:spec}"`` / ``f"{x=!s}"`` keep their explicit spec/conversion.
            conversion = "r"
        formatted = _FStringFormat(
            expr=expr,
            conversion=conversion,
            spec=format_spec,
            line=line,
        )
        if debug_prefix is None:
            return formatted
        return _BinOp(
            op="+",
            lhs=_Str(parts=((debug_prefix, False),), line=line),
            rhs=formatted,
            line=line,
        )

    def _fstring_source_parts(self, raw: str, line: int) -> list:
        body = self._string_body(raw)
        out = []
        literal = []
        i = 0
        while i < len(body):
            ch = body[i]
            if ch == "{" and i + 1 < len(body) and body[i + 1] == "{":
                literal.append("{")
                i += 2
                continue
            if ch == "}" and i + 1 < len(body) and body[i + 1] == "}":
                literal.append("}")
                i += 2
                continue
            if ch == "{":
                if literal:
                    literal_text = "".join(literal)
                    out.append((literal_text, False))
                    literal = []
                start = i + 1
                depth = 0
                quote = ""
                triple = False
                i = start
                while i < len(body):
                    c = body[i]
                    if quote:
                        if c == "\\":
                            i += 2
                            continue
                        if triple:
                            if (
                                c == quote
                                and i + 2 < len(body)
                                and body[i + 1] == quote
                                and body[i + 2] == quote
                            ):
                                quote = ""
                                triple = False
                                i += 3
                                continue
                        elif c == quote:
                            quote = ""
                            i += 1
                            continue
                        i += 1
                        continue
                    if c in ("'", '"'):
                        quote = c
                        triple = (
                            i + 2 < len(body) and body[i + 1] == c and body[i + 2] == c
                        )
                        i += 3 if triple else 1
                        continue
                    if c in "([{":
                        depth += 1
                    elif c in ")]}":
                        if c == "}" and depth == 0:
                            out.append((body[start:i], True))
                            i += 1
                            break
                        if depth > 0:
                            depth -= 1
                    i += 1
                else:
                    raise ParseError(
                        self.filename
                        + ":"
                        + str(line)
                        + ": unterminated f-string expression"
                    )
                continue
            if ch == "}":
                raise ParseError(
                    self.filename + ":" + str(line) + ": single '}' in f-string"
                )
            literal.append(ch)
            i += 1
        if literal:
            literal_text = "".join(literal)
            out.append((literal_text, False))
        return out

    def _parse_fstring_parts(self, raw: str, line: int) -> list:
        is_raw = "r" in self._string_prefix(raw).lower()
        out = []
        for text, is_expression in self._fstring_source_parts(raw, line):
            if is_expression:
                out.append(self._parse_fstring_expr(text, line))
            else:
                out.append(_FStringText(text, is_raw, line))
        return out

    def _parse_subscript(self):
        """``expr``, ``a:b``, ``a:b:c`` or tuple of subscripts."""
        first = self._parse_subscript_elem()
        if self._peek().kind == TK_OP and self._peek().text == ",":
            elems = [first]
            while self._accept(TK_OP, ","):
                if self._peek().text == "]":
                    break
                elems.append(self._parse_subscript_elem())
            line = self._peek().line
            return _Tuple(elems=elems, line=line)
        return first

    def _parse_subscript_elem(self):
        """Single subscript: expression or ``start:stop[:step]`` slice."""
        if self._peek().kind == TK_OP and self._peek().text == ":":
            lo = None
        else:
            lo = self._parse_expr()
        if not (self._peek().kind == TK_OP and self._peek().text == ":"):
            return lo
        self._advance()  # ':'
        if self._peek().kind == TK_OP and self._peek().text in (":", "]", ","):
            hi = None
        else:
            hi = self._parse_expr()
        step = None
        if self._accept(TK_OP, ":"):
            if self._peek().kind == TK_OP and self._peek().text in ("]", ","):
                step = None
            else:
                step = self._parse_expr()
        line = self._peek().line
        return _Slice(lo, hi, step, line)

    def _parse_call_arg(self):
        """Call argument: expression, ``*expr``, ``**expr``, or
        ``name=expr`` kwarg. Lifts to an _Assign / _Starred where
        appropriate so the lowering stage can distinguish shapes."""
        t = self._peek()
        if t.kind == TK_OP and t.text == "*" and self._peek(1).text != "*":
            self._advance()
            val = self._parse_expr()
            return _Starred(value=val, is_kw=False, line=t.line)
        if t.kind == TK_OP and t.text == "**":
            self._advance()
            val = self._parse_expr()
            return _Starred(value=val, is_kw=True, line=t.line)
        # ``name=expr`` kwarg. We need one-token lookahead past the
        # name — but since we parse the whole expression first, we
        # detect the kwarg shape by looking at ``NAME '='`` ahead.
        if (
            t.kind == TK_NAME
            and self._peek(1).kind == TK_OP
            and self._peek(1).text == "="
        ):
            name_tok = self._advance()
            self._advance()  # '='
            val = self._parse_expr()
            return ("__pcc_kwarg__", name_tok.text, val, name_tok.line)
        return self._parse_expr()

    def _parse_atom_trailer(self):
        node = self._parse_atom()
        while True:
            t = self._peek()
            if t.kind == TK_OP and t.text == "(":
                self._advance()
                args: list = []
                if self._peek().text != ")":
                    first_arg = self._parse_call_arg()
                    # ``f(x for y in z)`` — generator comprehension as
                    # the sole argument. The grammar allows a bare
                    # ``for`` right after the first expression.
                    if self._peek().kind == TK_KEYWORD and self._peek().text == "for":
                        gens = self._parse_comp_for()
                        args.append(
                            _Comp(
                                kind="gen",
                                elt=first_arg,
                                generators=gens,
                                line=t.line,
                            )
                        )
                    else:
                        args.append(first_arg)
                        while self._accept(TK_OP, ","):
                            if self._peek().text == ")":
                                break
                            args.append(self._parse_call_arg())
                self._expect(TK_OP, ")")
                node = _Call(func=node, args=args, line=t.line)
            elif t.kind == TK_OP and t.text == "[":
                self._advance()
                idx = self._parse_subscript()
                self._expect(TK_OP, "]")
                node = _Subscript(obj=node, idx=idx, line=t.line)
            elif t.kind == TK_OP and t.text == ".":
                self._advance()
                attr_tok = self._expect(TK_NAME)
                attr = attr_tok.text
                node = _Attr(node, attr, t.line)
            else:
                break
        return node

    def _parse_atom(self):
        t = self._peek()
        if t.kind == TK_NUMBER:
            self._advance()
            clean = t.text.replace("_", "")
            if clean.lower().endswith("j"):
                imag_text = clean[:-1]
                if imag_text == "" or imag_text == "+":
                    imag_text = "1"
                elif imag_text == "-":
                    imag_text = "-1"
                return _ComplexNum(imag_text, t.line)
            low = clean.lower()
            if low.startswith("0x") or low.startswith("0o") or low.startswith("0b"):
                # Hex / octal / binary literal: always an int. A hex literal's
                # 'e'/'E' is a digit (0xDEADBEEF), NOT a float exponent — so the
                # '.'/'e' float test below must not fire for these.
                return _Num(clean, t.line, True)
            if "." in clean or "e" in low:
                return _Num(clean, t.line, False)
            return _Num(clean, t.line, True)
        if t.kind == TK_STRING:
            self._advance()
            strings = [t]
            # Implicit concatenation: ``"foo" "bar"`` / ``"foo" r"bar"``.
            while self._peek().kind == TK_STRING:
                nxt = self._advance()
                strings.append(nxt)
            has_f_string = False
            for tok in strings:
                if self._string_is_f(tok.text):
                    has_f_string = True
                    break
            if has_f_string:
                fparts = []
                for tok in strings:
                    if self._string_is_f(tok.text):
                        fparts.extend(self._parse_fstring_parts(tok.text, tok.line))
                    else:
                        body, is_raw = self._string_piece(tok.text)
                        fparts.append(_FStringText(body, is_raw, tok.line))
                return _FString(fparts, t.line)
            all_bytes = True
            for tok in strings:
                if not self._string_is_b(tok.text):
                    all_bytes = False
                    break
            pieces = []
            for tok in strings:
                pieces.append(self._string_piece(tok.text))
            if all_bytes:
                return _Bytes(tuple(pieces), t.line)
            return _Str(tuple(pieces), t.line)
        if t.kind == TK_KEYWORD:
            if t.text == "True":
                self._advance()
                return _Bool(True, t.line)
            if t.text == "False":
                self._advance()
                return _Bool(False, t.line)
            if t.text == "None":
                self._advance()
                return _None(t.line)
        if t.kind == TK_OP and t.text == "...":
            self._advance()
            # The unbindable spelling matches the host parser and keeps
            # the literal distinct from a user binding named Ellipsis.
            return _Name("...", t.line)
        if t.kind == TK_NAME:
            self._advance()
            return _Name(t.text, t.line)
        if t.kind == TK_OP and t.text == "(":
            self._advance()
            if self._accept(TK_OP, ")"):
                return _Tuple(elems=[], line=t.line)
            first = self._parse_list_item()
            # Generator expression: ``(x for y in z)``.
            if (
                self._peek().kind == TK_KEYWORD
                and self._peek().text == "for"
                and not isinstance(first, _Starred)
            ):
                gens = self._parse_comp_for()
                self._expect(TK_OP, ")")
                return _Comp(kind="gen", elt=first, generators=gens, line=t.line)
            if self._accept(TK_OP, ","):
                elems = [first]
                if self._peek().text != ")":
                    elems.append(self._parse_list_item())
                    while self._accept(TK_OP, ","):
                        if self._peek().text == ")":
                            break
                        elems.append(self._parse_list_item())
                self._expect(TK_OP, ")")
                return _Tuple(elems=elems, line=t.line)
            self._expect(TK_OP, ")")
            return first
        if t.kind == TK_OP and t.text == "[":
            self._advance()
            if self._accept(TK_OP, "]"):
                return _List(elems=[], line=t.line)
            first = self._parse_list_item()
            # List comprehension: ``[expr for ...]``.
            if (
                self._peek().kind == TK_KEYWORD
                and self._peek().text == "for"
                and not isinstance(first, _Starred)
            ):
                gens = self._parse_comp_for()
                self._expect(TK_OP, "]")
                return _Comp(kind="list", elt=first, generators=gens, line=t.line)
            elems: list = [first]
            while self._accept(TK_OP, ","):
                if self._peek().text == "]":
                    break
                elems.append(self._parse_list_item())
            self._expect(TK_OP, "]")
            return _List(elems=elems, line=t.line)
        if t.kind == TK_OP and t.text == "{":
            self._advance()
            if self._accept(TK_OP, "}"):
                # Empty ``{}`` is a dict literal, not a set.
                return _Dict(keys=[], values=[], line=t.line)
            # ``{*x, ...}`` → set with unpacking. Detect before the
            # generic expression parse because ``*`` isn't valid at
            # _parse_expr's top.
            if (
                self._peek().kind == TK_OP
                and self._peek().text == "*"
                and self._peek(1).text != "*"
            ):
                first = self._parse_list_item()
                elems = [first]
                while self._accept(TK_OP, ","):
                    if self._peek().text == "}":
                        break
                    elems.append(self._parse_list_item())
                self._expect(TK_OP, "}")
                return _Set(elems=elems, line=t.line)
            # ``{**m}`` → dict. Otherwise parse first expression and
            # look at the next token to decide dict vs set vs comp.
            if self._peek().kind == TK_OP and self._peek().text == "**":
                self._advance()
                val = self._parse_expr()
                keys: list = [None]
                values: list = [val]
                while self._accept(TK_OP, ","):
                    if self._peek().text == "}":
                        break
                    if self._accept(TK_OP, "**"):
                        keys.append(None)
                        values.append(self._parse_expr())
                    else:
                        k = self._parse_expr()
                        self._expect(TK_OP, ":")
                        keys.append(k)
                        values.append(self._parse_expr())
                self._expect(TK_OP, "}")
                return _Dict(keys=keys, values=values, line=t.line)
            first = self._parse_expr()
            if self._accept(TK_OP, ":"):
                # Dict literal or dict comprehension.
                v = self._parse_expr()
                if self._peek().kind == TK_KEYWORD and self._peek().text == "for":
                    gens = self._parse_comp_for()
                    self._expect(TK_OP, "}")
                    elt = (first, v)
                    return _Comp(kind="dict", elt=elt, generators=gens, line=t.line)
                keys = [first]
                values = [v]
                while self._accept(TK_OP, ","):
                    if self._peek().text == "}":
                        break
                    if self._accept(TK_OP, "**"):
                        keys.append(None)
                        values.append(self._parse_expr())
                        continue
                    k = self._parse_expr()
                    self._expect(TK_OP, ":")
                    keys.append(k)
                    values.append(self._parse_expr())
                self._expect(TK_OP, "}")
                return _Dict(keys=keys, values=values, line=t.line)
            if self._peek().kind == TK_KEYWORD and self._peek().text == "for":
                gens = self._parse_comp_for()
                self._expect(TK_OP, "}")
                return _Comp(kind="set", elt=first, generators=gens, line=t.line)
            # Plain set literal.
            elems = [first]
            while self._accept(TK_OP, ","):
                if self._peek().text == "}":
                    break
                elems.append(self._parse_list_item())
            self._expect(TK_OP, "}")
            return _Set(elems=elems, line=t.line)
        raise ParseError(f"{self.filename}:{t.line}:{t.col}: unexpected token {t!r}")

    def _parse_list_item(self):
        """Element inside ``[...]`` / ``{...}`` / tuple — accepts a
        plain expression or ``*expr`` unpacking (PEP 448)."""
        t = self._peek()
        if t.kind == TK_OP and t.text == "*" and self._peek(1).text != "*":
            self._advance()
            val = self._parse_expr()
            return _Starred(value=val, is_kw=False, line=t.line)
        return self._parse_expr()

    def _parse_comp_for(self) -> list:
        """Parse one or more ``for <target> in <iter> [if <cond>]`` clauses."""
        gens: list = []
        while self._peek().kind == TK_KEYWORD and self._peek().text == "for":
            self._advance()
            target = self._parse_for_target()
            self._expect(TK_KEYWORD, "in")
            # Disjunction rather than full expr to avoid swallowing
            # the trailing ``if``/``for``.
            it = self._parse_or()
            ifs: list = []
            while self._peek().kind == TK_KEYWORD and self._peek().text == "if":
                self._advance()
                ifs.append(self._parse_or())
            gens.append((target, it, ifs))
        return gens


def parse(src: str, filename: str = "<input>") -> _Module:
    parser = Parser(src, filename)
    return Parser.parse_module(parser)
