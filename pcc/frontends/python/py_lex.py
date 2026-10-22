"""P6C.3 — native Python tokenizer (skeleton).

A hand-written lexer for the Python subset pcc uses. Full CPython
tokenization is ~2000 LoC; this skeleton targets ~800 LoC when
complete. Today it handles indentation tracking, identifiers,
numeric / string literals, the keyword set, and common operators —
enough to parse ``hello.py``-class programs.

Anchor: CPython ``Lib/tokenize.py`` is NOT our parity target — we
ship only what pcc needs. PEP 617 grammar references are used where
they simplify decisions.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
# Token kinds. Keep in sync with parser expectations.
TK_NEWLINE  = "NEWLINE"
TK_INDENT   = "INDENT"
TK_DEDENT   = "DEDENT"
TK_NAME     = "NAME"
TK_NUMBER   = "NUMBER"
TK_STRING   = "STRING"
TK_OP       = "OP"
TK_KEYWORD  = "KEYWORD"
TK_EOF      = "EOF"


KEYWORDS = (
    "False", "None", "True",
    "and", "as", "assert", "async", "await", "break", "class",
    "continue", "def", "del", "elif", "else", "except", "finally",
    "for", "from", "global", "if", "import", "in", "is", "lambda",
    "nonlocal", "not", "or", "pass", "raise", "return", "try",
    "while", "with", "yield",
)

# Multi-character operators matched longest-first.
_OPS_MULTI = (
    "**=", "//=", ">>=", "<<=", "...", "->", "**", "//", "<<", ">>",
    "<=", ">=", "==", "!=", "+=", "-=", "*=", "/=", "%=", "&=",
    "|=", "^=", ":=",
)
_OPS_SINGLE = "+-*/%@&|^~<>=()[]{},:.;"


def _is_digit_code(c: int) -> bool:
    return c >= 48 and c <= 57


def _is_alpha_code(c: int) -> bool:
    return (c >= 65 and c <= 90) or (c >= 97 and c <= 122)


def _is_alnum_code(c: int) -> bool:
    return _is_alpha_code(c) or _is_digit_code(c)


def _lower_code(c: int) -> int:
    if c >= 65 and c <= 90:
        return c + 32
    return c


def _is_prefix_code(c: int) -> bool:
    lc = _lower_code(c)
    return lc == 98 or lc == 102 or lc == 114 or lc == 117


def _is_quote_code(c: int) -> bool:
    return c == 34 or c == 39


def _is_single_op_code(c: int) -> bool:
    return (
        c == 37 or c == 38 or c == 40 or c == 41 or c == 42
        or c == 43 or c == 44 or c == 45 or c == 46 or c == 47
        or c == 58 or c == 59 or c == 60 or c == 61 or c == 62
        or c == 64 or c == 91 or c == 93 or c == 94 or c == 123
        or c == 124 or c == 125 or c == 126
    )


def _is_open_paren_code(c: int) -> bool:
    return c == 40 or c == 91 or c == 123


def _is_close_paren_code(c: int) -> bool:
    return c == 41 or c == 93 or c == 125


def _single_op_text(c: int) -> str:
    if c == 37:
        return "%"
    if c == 38:
        return "&"
    if c == 40:
        return "("
    if c == 41:
        return ")"
    if c == 42:
        return "*"
    if c == 43:
        return "+"
    if c == 44:
        return ","
    if c == 45:
        return "-"
    if c == 46:
        return "."
    if c == 47:
        return "/"
    if c == 58:
        return ":"
    if c == 59:
        return ";"
    if c == 60:
        return "<"
    if c == 61:
        return "="
    if c == 62:
        return ">"
    if c == 64:
        return "@"
    if c == 91:
        return "["
    if c == 93:
        return "]"
    if c == 94:
        return "^"
    if c == 123:
        return "{"
    if c == 124:
        return "|"
    if c == 125:
        return "}"
    if c == 126:
        return "~"
    return ""


def _pcc_str_byte_len(s: str) -> int:
    return len(s)


def _pcc_str_byte_at(s: str, pos: int) -> int:
    if pos < len(s):
        return ord(s[pos])
    return -1


def _pcc_str_byte_slice(s: str, lo: int, hi: int) -> str:
    return s[lo:hi]


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    line: int
    col: int


class LexError(Exception):
    pass


class Lexer:
    """Minimal hand-written Python tokenizer.

    Caller usage::

        tokens = Lexer(source, filename="hello.py").tokenize()

    The iterator yields tokens in source order, ending with a single
    ``TK_EOF`` token. Indentation is tracked as virtual INDENT /
    DEDENT tokens so the parser can rely on block boundaries without
    re-counting whitespace.
    """

    def __init__(self, src: str, filename: str = "<input>") -> None:
        self.src = src
        self._src_len = _pcc_str_byte_len(src)
        self._debug_bootstrap = bool(
            os.environ.get("PCC_DEBUG_BOOTSTRAP", "").strip()
        )
        self.filename = filename
        self.pos = 0
        self.line = 1
        self.col = 1
        self._indent_stack: list[int] = [0]
        self._paren_depth = 0
        self._at_line_start = True

    # ------------------------------------------------------ entry

    def tokenize(self) -> list[Token]:
        """Run the lexer to completion, returning all tokens."""
        out: list[Token] = []
        indent_stack: list[int] = self._indent_stack
        while self.pos < self._src_len:
            if self._at_line_start and self._paren_depth == 0:
                self._emit_indent(out)
                self._at_line_start = False
                if self.pos >= self._src_len:
                    break
            ch = self._peek_code(0)
            if ch == 10:
                self._emit_newline(out)
                continue
            if ch == 32 or ch == 9:
                self.pos += 1
                self.col += 1
                continue
            if ch == 35:
                while self.pos < self._src_len and self._peek_code(0) != 10:
                    self.pos += 1
                continue
            if ch == 92 and self._peek_code(1) == 10:
                self.pos += 2
                self.line += 1
                self.col = 1
                continue
            if (
                _is_prefix_code(ch)
                and _is_quote_code(self._peek_code(1))
            ):
                out.append(self._read_string())
                continue
            if (
                _is_prefix_code(ch)
                and _is_prefix_code(self._peek_code(1))
                and _is_quote_code(self._peek_code(2))
            ):
                out.append(self._read_string())
                continue
            if _is_alpha_code(ch) or ch == 95:
                out.append(self._read_name())
                continue
            if _is_digit_code(ch) or (
                ch == 46 and _is_digit_code(self._peek_code(1))
            ):
                out.append(self._read_number())
                continue
            if _is_quote_code(ch):
                out.append(self._read_string())
                continue
            out.append(self._read_op())
        if not self._at_line_start:
            out.append(Token(TK_NEWLINE, "\n", self.line, self.col))
        while len(indent_stack) > 1:
            indent_stack.pop()
            out.append(Token(TK_DEDENT, "", self.line, self.col))
        out.append(Token(TK_EOF, "", self.line, self.col))
        return out

    # ------------------------------------------------------ helpers

    def _code_at(self, pos: int) -> int:
        if pos < self._src_len:
            return _pcc_str_byte_at(self.src, pos)
        return -1

    def _slice(self, lo: int, hi: int) -> str:
        # Temporary bootstrap-time guard: catch pathological slice bounds
        # before they reach C runtime helpers, which can otherwise turn into
        # heap-corruption crashes that are hard to diagnose.
        if self._debug_bootstrap:
            try:
                src_len = self._src_len
            except Exception as e:
                raise RuntimeError(f"lexer._slice source-length probe failed: {e}")
            if lo < 0 or hi < 0 or lo > hi or lo > src_len or hi > src_len:
                raise RuntimeError(
                    f"lexer._slice bounds out of range: lo={lo} hi={hi} src_len={src_len}"
                )

        src_len = self._src_len
        # Clamp to Python-like byte-slice bounds in non-debug mode so
        # malformed offsets cannot propagate into runtime string helpers.
        if lo < 0:
            lo = src_len + lo
        if hi < 0:
            hi = src_len + hi
        if lo < 0:
            lo = 0
        if hi < 0:
            hi = 0
        if lo > src_len:
            lo = src_len
        if hi > src_len:
            hi = src_len
        if hi < lo:
            hi = lo
        return _pcc_str_byte_slice(self.src, lo, hi)

    def _peek_code(self, off: int) -> int:
        return self._code_at(self.pos + off)

    def _peek(self, off: int) -> str:
        p = self.pos + off
        return self._slice(p, p + 1) if p < self._src_len else ""

    def _emit_indent(self, out: list[Token]) -> None:
        indent_stack: list[int] = self._indent_stack
        depth = 0
        p = self.pos
        pc = self._code_at(p)
        while p < self._src_len and (pc == 32 or pc == 9):
            depth += 1 if pc == 32 else 8
            p += 1
            pc = self._code_at(p)
        if p >= self._src_len or pc == 10 or pc == 35:
            self.pos = p
            self.col = depth + 1
            return
        top = indent_stack[-1]
        if depth > top:
            indent_stack.append(depth)
            out.append(Token(TK_INDENT, "", self.line, 1))
        while depth < indent_stack[-1]:
            indent_stack.pop()
            out.append(Token(TK_DEDENT, "", self.line, 1))
        if depth != indent_stack[-1]:
            raise LexError(
                f"{self.filename}:{self.line}: inconsistent indentation"
            )
        self.pos = p
        self.col = depth + 1

    def _emit_newline(self, out: list[Token]) -> None:
        if self._paren_depth == 0:
            out.append(Token(TK_NEWLINE, "\n", self.line, self.col))
            self._at_line_start = True
        self.pos += 1
        self.line += 1
        self.col = 1

    def _read_name(self) -> Token:
        start = self.pos
        start_col = self.col
        while (
            self.pos < self._src_len
            and (
                _is_alnum_code(self._peek_code(0))
                or self._peek_code(0) == 95
            )
        ):
            self.pos += 1
            self.col += 1
        text = self._slice(start, self.pos)
        kind = TK_KEYWORD if text in KEYWORDS else TK_NAME
        return Token(kind, text, self.line, start_col)

    def _read_number(self) -> Token:
        start = self.pos
        start_col = self.col
        # Hex / octal / binary: ``0x…`` / ``0o…`` / ``0b…`` (PEP 3127).
        if (
            self._peek_code(0) == 48
            and self.pos + 1 < self._src_len
            and (
                _lower_code(self._code_at(self.pos + 1)) == 98
                or _lower_code(self._code_at(self.pos + 1)) == 111
                or _lower_code(self._code_at(self.pos + 1)) == 120
            )
        ):
            base = _lower_code(self._code_at(self.pos + 1))
            self.pos += 2
            self.col += 2
            while self.pos < self._src_len:
                c = self._peek_code(0)
                valid = False
                if c == 95:
                    valid = True
                elif base == 120:
                    valid = (
                        _is_digit_code(c)
                        or (c >= 65 and c <= 70)
                        or (c >= 97 and c <= 102)
                    )
                elif base == 111:
                    valid = c >= 48 and c <= 55
                else:
                    valid = c == 48 or c == 49
                if not valid:
                    break
                self.pos += 1
                self.col += 1
            return Token(
                TK_NUMBER, self._slice(start, self.pos), self.line, start_col,
            )
        has_dot = False
        has_exp = False
        while self.pos < self._src_len:
            ch = self._peek_code(0)
            if _is_digit_code(ch):
                self.pos += 1
                self.col += 1
            elif ch == 46 and not has_dot and not has_exp:
                has_dot = True
                self.pos += 1
                self.col += 1
            elif (ch == 101 or ch == 69) and not has_exp:
                has_exp = True
                self.pos += 1
                self.col += 1
                nxt = self._peek_code(0)
                if self.pos < self._src_len and (nxt == 43 or nxt == 45):
                    self.pos += 1
                    self.col += 1
            elif ch == 95:
                self.pos += 1
                self.col += 1
            elif ch == 106 or ch == 74:
                # Imaginary literal suffix — we accept the char so the
                # tokenizer doesn't choke, the frontend doesn't use them.
                self.pos += 1
                self.col += 1
                break
            else:
                break
        return Token(TK_NUMBER, self._slice(start, self.pos), self.line, start_col)

    def _read_string(self) -> Token:
        start = self.pos
        start_col = self.col
        # Consume the optional prefix chars (``b``, ``r``, ``f``, ``u``
        # and their 2-char combinations).
        while (
            self.pos < self._src_len
            and _is_prefix_code(self._peek_code(0))
            and not _is_quote_code(self._peek_code(0))
        ):
            self.pos += 1
            self.col += 1
        quote_code = self._peek_code(0)
        # Handle triple-quoted.
        triple = (
            self._peek_code(1) == quote_code
            and self._peek_code(2) == quote_code
        )
        if triple:
            self.pos += 3
            self.col += 3
            while self.pos < self._src_len:
                ch = self._peek_code(0)
                if ch == 92:
                    self.pos += 1
                    self.col += 1
                    if self.pos < self._src_len:
                        if self._peek_code(0) == 10:
                            self.line += 1
                            self.col = 1
                        else:
                            self.col += 1
                        self.pos += 1
                    continue
                if (
                    self._peek_code(0) == quote_code
                    and self._peek_code(1) == quote_code
                    and self._peek_code(2) == quote_code
                ):
                    self.pos += 3
                    self.col += 3
                    return Token(
                        TK_STRING, self._slice(start, self.pos),
                        self.line, start_col,
                    )
                if ch == 10:
                    self.line += 1
                    self.col = 1
                else:
                    self.col += 1
                self.pos += 1
            raise LexError(
                f"{self.filename}:{self.line}: unterminated triple-quoted string"
            )
        # Single-line.
        self.pos += 1
        self.col += 1
        while self.pos < self._src_len:
            ch = self._peek_code(0)
            if ch == 92:
                self.pos += 2
                self.col += 2
                continue
            if ch == quote_code:
                self.pos += 1
                self.col += 1
                return Token(
                    TK_STRING, self._slice(start, self.pos),
                    self.line, start_col,
                )
            if ch == 10:
                raise LexError(
                    f"{self.filename}:{self.line}: unterminated string"
                )
            self.pos += 1
            self.col += 1
        raise LexError(f"{self.filename}:{self.line}: unterminated string at EOF")

    def _read_op(self) -> Token:
        start_col = self.col
        pos: int = self.pos
        c0 = self._code_at(pos)
        c1 = self._code_at(pos + 1)
        c2 = self._code_at(pos + 2)
        op = ""
        op_len = 0

        # Fixed longest-first matching for the hot self-host lexer path.
        # Avoid per-candidate slicing or dict lookup; both are amplified by
        # bootstrap sources with hundreds of thousands of tokens.
        if c0 == 42 and c1 == 42 and c2 == 61:
            op = "**="
            op_len = 3
        elif c0 == 47 and c1 == 47 and c2 == 61:
            op = "//="
            op_len = 3
        elif c0 == 62 and c1 == 62 and c2 == 61:
            op = ">>="
            op_len = 3
        elif c0 == 60 and c1 == 60 and c2 == 61:
            op = "<<="
            op_len = 3
        elif c0 == 46 and c1 == 46 and c2 == 46:
            op = "..."
            op_len = 3
        elif c0 == 45 and c1 == 62:
            op = "->"
            op_len = 2
        elif c0 == 42 and c1 == 42:
            op = "**"
            op_len = 2
        elif c0 == 47 and c1 == 47:
            op = "//"
            op_len = 2
        elif c0 == 60 and c1 == 60:
            op = "<<"
            op_len = 2
        elif c0 == 62 and c1 == 62:
            op = ">>"
            op_len = 2
        elif c0 == 60 and c1 == 61:
            op = "<="
            op_len = 2
        elif c0 == 62 and c1 == 61:
            op = ">="
            op_len = 2
        elif c0 == 61 and c1 == 61:
            op = "=="
            op_len = 2
        elif c0 == 33 and c1 == 61:
            op = "!="
            op_len = 2
        elif c0 == 43 and c1 == 61:
            op = "+="
            op_len = 2
        elif c0 == 45 and c1 == 61:
            op = "-="
            op_len = 2
        elif c0 == 42 and c1 == 61:
            op = "*="
            op_len = 2
        elif c0 == 47 and c1 == 61:
            op = "/="
            op_len = 2
        elif c0 == 37 and c1 == 61:
            op = "%="
            op_len = 2
        elif c0 == 38 and c1 == 61:
            op = "&="
            op_len = 2
        elif c0 == 124 and c1 == 61:
            op = "|="
            op_len = 2
        elif c0 == 94 and c1 == 61:
            op = "^="
            op_len = 2
        elif c0 == 58 and c1 == 61:
            op = ":="
            op_len = 2
        if op_len != 0:
            self.pos = pos + op_len
            self.col += op_len
            return Token(TK_OP, op, self.line, start_col)

        ch_code = c0
        if _is_single_op_code(ch_code):
            ch = _single_op_text(ch_code)
            if _is_open_paren_code(ch_code):
                self._paren_depth += 1
            elif _is_close_paren_code(ch_code):
                self._paren_depth = max(0, self._paren_depth - 1)
            self.pos += 1
            self.col += 1
            return Token(TK_OP, ch, self.line, start_col)
        ch = _single_op_text(ch_code)
        if not ch:
            ch = self._slice(self.pos, self.pos + 1)
        raise LexError(
            f"{self.filename}:{self.line}:{self.col}: stray character {ch!r}"
        )
