"""pcc.frontends.c.parse.c_lex — P6C.5 α2: native C tokenizer (no PLY lex import).

Drop-in replacement for ``pcc.frontends.c.lex.c_lexer.CLexer``. Same constructor
signature, same ``build``/``input``/``token``/``find_tok_column`` API,
same token type names.

The lexer does not use regexes for the hot path — it's a hand-written
character-at-a-time scanner, similar in style to ``pcc.frontends.python.py_lex``.
Regex is used only to match the few inherently-multi-char patterns
(integer suffix, float exponent) which are simpler to express that way.

Consumed by ``pcc.frontends.c.parse.c_parse_driver``. No PLY import.
"""
from __future__ import annotations

import re
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Token object — minimal surface compatible with PLY's LexToken
# ---------------------------------------------------------------------------


class Token:
    __slots__ = ("type", "value", "lineno", "lexpos")

    def __init__(self, type_: str, value: str, lineno: int, lexpos: int) -> None:
        self.type = type_
        self.value = value
        self.lineno = lineno
        self.lexpos = lexpos

    def __repr__(self) -> str:
        return f"LexToken({self.type},{self.value!r},{self.lineno},{self.lexpos})"


# ---------------------------------------------------------------------------
# Keyword + operator tables (copied verbatim from pcc.frontends.c.lex.c_lexer)
# ---------------------------------------------------------------------------


# (python identifier form) -> (yacc token name)
KEYWORD_MAP: dict[str, str] = {
    "_Alignas": "_ALIGNAS", "_Alignof": "_ALIGNOF", "_Bool": "_BOOL",
    "_Complex": "_COMPLEX", "_Float16": "_FLOAT16", "_Generic": "_GENERIC",
    "_Noreturn": "_NORETURN", "_Static_assert": "_STATIC_ASSERT",
    "_Thread_local": "_THREAD_LOCAL",
    "alignas": "_ALIGNAS", "alignof": "_ALIGNOF",
    "auto": "AUTO", "break": "BREAK", "case": "CASE", "char": "CHAR",
    "const": "CONST", "continue": "CONTINUE", "default": "DEFAULT",
    "do": "DO", "double": "DOUBLE", "else": "ELSE", "enum": "ENUM",
    "extern": "EXTERN", "float": "FLOAT", "for": "FOR", "goto": "GOTO",
    "if": "IF", "inline": "INLINE", "int": "INT", "long": "LONG",
    "nullptr": "NULLPTR", "offsetof": "OFFSETOF", "register": "REGISTER",
    "restrict": "RESTRICT", "return": "RETURN", "short": "SHORT",
    "signed": "SIGNED", "sizeof": "SIZEOF", "static": "STATIC",
    "static_assert": "_STATIC_ASSERT", "struct": "STRUCT", "switch": "SWITCH",
    "thread_local": "_THREAD_LOCAL", "typedef": "TYPEDEF", "typeof": "TYPEOF",
    "union": "UNION", "unsigned": "UNSIGNED", "void": "VOID",
    "volatile": "VOLATILE", "while": "WHILE",
    "__alignof": "_ALIGNOF", "__alignof__": "_ALIGNOF",
    "__builtin_va_arg": "BUILTIN_VA_ARG", "__const": "CONST",
    "__const__": "CONST", "__inline": "INLINE", "__inline__": "INLINE",
    "__int128": "INT128", "__restrict": "RESTRICT",
    "__restrict__": "RESTRICT", "__signed": "SIGNED",
    "__signed__": "SIGNED", "__thread": "_THREAD_LOCAL",
    "__typeof": "TYPEOF", "__typeof__": "TYPEOF",
    "__volatile": "VOLATILE", "__volatile__": "VOLATILE",
}


# Multi-char operators matched longest-first.
_MULTI_CHAR_OPS: tuple[tuple[str, str], ...] = (
    ("...", "ELLIPSIS"),
    ("<<=", "LSHIFTEQUAL"),
    (">>=", "RSHIFTEQUAL"),
    ("->", "ARROW"),
    ("++", "PLUSPLUS"),
    ("--", "MINUSMINUS"),
    ("<<", "LSHIFT"),
    (">>", "RSHIFT"),
    ("<=", "LE"),
    (">=", "GE"),
    ("==", "EQ"),
    ("!=", "NE"),
    ("&&", "LAND"),
    ("||", "LOR"),
    ("+=", "PLUSEQUAL"),
    ("-=", "MINUSEQUAL"),
    ("*=", "TIMESEQUAL"),
    ("/=", "DIVEQUAL"),
    ("%=", "MODEQUAL"),
    ("&=", "ANDEQUAL"),
    ("|=", "OREQUAL"),
    ("^=", "XOREQUAL"),
)

_SINGLE_CHAR_OPS: dict[str, str] = {
    "+": "PLUS", "-": "MINUS", "*": "TIMES", "/": "DIVIDE",
    "%": "MOD", "|": "OR", "&": "AND", "~": "NOT", "^": "XOR",
    "<": "LT", ">": "GT", "=": "EQUALS", "!": "LNOT",
    "(": "LPAREN", ")": "RPAREN", "[": "LBRACKET", "]": "RBRACKET",
    "{": "LBRACE", "}": "RBRACE", ",": "COMMA", ".": "PERIOD",
    ";": "SEMI", ":": "COLON", "?": "CONDOP",
}


# ---------------------------------------------------------------------------
# Number-literal suffix helpers (precompiled)
# ---------------------------------------------------------------------------


_INT_SUFFIX = re.compile(r"([uU][lL]{0,2})|([lL]{1,2}[uU]?)")


# ---------------------------------------------------------------------------
# CLexer — public API matches pcc.frontends.c.lex.c_lexer.CLexer
# ---------------------------------------------------------------------------


class CLexer:
    """Hand-written C tokenizer — drop-in replacement for the PLY-based
    ``pcc.frontends.c.lex.c_lexer.CLexer``."""

    def __init__(
        self,
        error_func: Callable[[str, int, int], None],
        on_lbrace_func: Callable[[], None],
        on_rbrace_func: Callable[[], None],
        type_lookup_func: Callable[[str], bool],
    ) -> None:
        self.error_func = error_func
        self.on_lbrace_func = on_lbrace_func
        self.on_rbrace_func = on_rbrace_func
        self.type_lookup_func = type_lookup_func

        self.filename = ""
        self.last_token: Optional[Token] = None

        # Active input state
        self._src = ""
        self._pos = 0
        self.lineno = 1
        self._line_starts: list[int] = [0]

    # --------------------------------------------------- API

    def build(self, optimize: bool = False) -> None:
        """No-op — included for CLexer API parity. The legacy PLY
        lexer took ``**kwargs``; callers pass only ``optimize=`` in
        practice, so a fixed signature suffices."""
        return None

    def reset_lineno(self) -> None:
        self.lineno = 1

    def input(self, text: str) -> None:
        self._src = text
        self._pos = 0
        self.lineno = 1
        self._line_starts = [0]
        # Pre-compute line start offsets so find_tok_column is O(log n).
        for i, ch in enumerate(text):
            if ch == "\n":
                self._line_starts.append(i + 1)

    def token(self) -> Optional[Token]:
        tok = self._scan()
        self.last_token = tok
        return tok

    def find_tok_column(self, token: Token) -> int:
        """Return the column of ``token`` within its line (1-based)."""
        last_cr = self._src.rfind("\n", 0, token.lexpos)
        return token.lexpos - last_cr

    # --------------------------------------------------- scanner core

    def _scan(self) -> Optional[Token]:
        """Return the next token, or None at EOF."""
        src = self._src
        while True:
            if self._pos >= len(src):
                return None
            ch = src[self._pos]

            # Whitespace (excluding newline)
            if ch in " \t":
                self._pos += 1
                continue
            if ch == "\n":
                self._pos += 1
                self.lineno += 1
                continue

            # Line continuation (backslash-newline) — treat as whitespace
            if ch == "\\" and self._peek(1) == "\n":
                self._pos += 2
                self.lineno += 1
                continue

            # Comments
            if ch == "/" and self._peek(1) == "*":
                self._skip_block_comment()
                continue
            if ch == "/" and self._peek(1) == "/":
                self._skip_line_comment()
                continue

            # Preprocessor line: ``# <num> "file"`` (cpp-generated)
            if ch == "#":
                return self._scan_pphash()

            # Char / wide char / prefixed char constant
            if ch == "'":
                return self._scan_char_const()
            if ch in "LuU" and self._peek(1) == "'":
                return self._scan_prefixed_char_const()

            # String / wide string literal
            if ch == '"':
                return self._scan_string_literal()
            if ch == "L" and self._peek(1) == '"':
                return self._scan_wstring_literal()
            if ch in "uU" and self._peek(1) == '"':
                # u"..." and U"..." are treated as plain strings
                # (pcc doesn't distinguish UTF-8/16/32 at AST level).
                self._pos += 1  # skip prefix
                return self._scan_string_literal()

            # Identifier or keyword
            if ch.isalpha() or ch == "_" or ch == "$":
                return self._scan_identifier()

            # Numeric literal
            if ch.isdigit() or (
                ch == "." and self._pos + 1 < len(src)
                and src[self._pos + 1].isdigit()
            ):
                return self._scan_number()

            # Operator
            return self._scan_operator()

    # --------------------------------------------------- helpers

    def _peek(self, off: int = 1) -> str:
        p = self._pos + off
        return self._src[p] if p < len(self._src) else ""

    def _skip_block_comment(self) -> None:
        src = self._src
        assert src[self._pos:self._pos + 2] == "/*"
        self._pos += 2
        while self._pos < len(src):
            if src[self._pos] == "*" and self._peek(1) == "/":
                self._pos += 2
                return
            if src[self._pos] == "\n":
                self.lineno += 1
            self._pos += 1
        self._error(self._pos, "unterminated block comment")

    def _skip_line_comment(self) -> None:
        src = self._src
        assert src[self._pos:self._pos + 2] == "//"
        while self._pos < len(src) and src[self._pos] != "\n":
            self._pos += 1

    def _scan_identifier(self) -> Token:
        src = self._src
        start = self._pos
        while self._pos < len(src):
            c = src[self._pos]
            if c.isalnum() or c == "_" or c == "$":
                self._pos += 1
            else:
                break
        text = src[start:self._pos]

        kw = KEYWORD_MAP.get(text)
        if kw is not None:
            return Token(kw, text, self.lineno, start)
        if self.type_lookup_func(text):
            return Token("TYPEID", text, self.lineno, start)
        return Token("ID", text, self.lineno, start)

    def _scan_number(self) -> Token:
        """Scan integer / float / hex-float constant. Returns the
        correct token type (INT_CONST_{DEC,OCT,HEX,BIN},
        FLOAT_CONST, HEX_FLOAT_CONST)."""
        src = self._src
        start = self._pos

        # Hex / bin prefix
        if src[self._pos] == "0" and self._peek(1) in "xXbB":
            prefix = self._peek(1).lower()
            self._pos += 2
            if prefix == "x":
                return self._scan_hex_or_hex_float(start)
            # Binary
            while self._pos < len(src) and src[self._pos] in "01":
                self._pos += 1
            self._consume_int_suffix()
            return Token("INT_CONST_BIN", src[start:self._pos], self.lineno, start)

        # Leading-dot float: .5 → 0.5
        if src[self._pos] == ".":
            return self._scan_fraction_from_dot(start)

        # Digit run
        while self._pos < len(src) and src[self._pos].isdigit():
            self._pos += 1

        if self._pos < len(src) and src[self._pos] == ".":
            # Float with dot
            return self._scan_fraction(start)
        if self._pos < len(src) and src[self._pos] in "eE":
            # Float with exponent only
            return self._scan_exponent(start)

        # Integer: decimal vs octal
        self._consume_int_suffix()
        text = src[start:self._pos]
        # Octal: starts with 0. A bare ``0`` (or ``0U``, ``0L`` etc.)
        # is also OCT to match PLY's ``0[0-7]*`` pattern.
        digit_part = text.rstrip("uUlL")
        if digit_part.startswith("0") and all(
            c in "01234567" for c in digit_part[1:]
        ):
            return Token("INT_CONST_OCT", text, self.lineno, start)
        return Token("INT_CONST_DEC", text, self.lineno, start)

    def _scan_hex_or_hex_float(self, start: int) -> Token:
        """``start`` points at the leading 0; ``self._pos`` is just
        past ``0x``."""
        src = self._src
        while self._pos < len(src) and src[self._pos] in "0123456789abcdefABCDEF":
            self._pos += 1
        # Hex float: ``0x1.8p10`` / ``0x1p-3``
        if self._pos < len(src) and (src[self._pos] == "." or src[self._pos] in "pP"):
            if src[self._pos] == ".":
                self._pos += 1
                while self._pos < len(src) and src[self._pos] in "0123456789abcdefABCDEF":
                    self._pos += 1
            # Binary exponent is REQUIRED in a hex float
            if self._pos < len(src) and src[self._pos] in "pP":
                self._pos += 1
                if self._pos < len(src) and src[self._pos] in "+-":
                    self._pos += 1
                while self._pos < len(src) and src[self._pos].isdigit():
                    self._pos += 1
            if self._pos < len(src) and src[self._pos] in "FfLl":
                self._pos += 1
            return Token("HEX_FLOAT_CONST", src[start:self._pos], self.lineno, start)
        self._consume_int_suffix()
        return Token("INT_CONST_HEX", src[start:self._pos], self.lineno, start)

    def _scan_fraction(self, start: int) -> Token:
        """``self._pos`` is at the dot, integer part already consumed."""
        src = self._src
        self._pos += 1  # consume .
        while self._pos < len(src) and src[self._pos].isdigit():
            self._pos += 1
        if self._pos < len(src) and src[self._pos] in "eE":
            self._consume_exponent()
        if self._pos < len(src) and src[self._pos] in "FfLl":
            self._pos += 1
        return Token("FLOAT_CONST", src[start:self._pos], self.lineno, start)

    def _scan_fraction_from_dot(self, start: int) -> Token:
        """Leading-dot float: .5, .5e10."""
        src = self._src
        self._pos += 1  # consume .
        while self._pos < len(src) and src[self._pos].isdigit():
            self._pos += 1
        if self._pos < len(src) and src[self._pos] in "eE":
            self._consume_exponent()
        if self._pos < len(src) and src[self._pos] in "FfLl":
            self._pos += 1
        return Token("FLOAT_CONST", src[start:self._pos], self.lineno, start)

    def _scan_exponent(self, start: int) -> Token:
        """Float with exponent only: ``1e10``."""
        self._consume_exponent()
        if self._pos < len(self._src) and self._src[self._pos] in "FfLl":
            self._pos += 1
        return Token(
            "FLOAT_CONST", self._src[start:self._pos], self.lineno, start,
        )

    def _consume_exponent(self) -> None:
        src = self._src
        assert src[self._pos] in "eE"
        self._pos += 1
        if self._pos < len(src) and src[self._pos] in "+-":
            self._pos += 1
        while self._pos < len(src) and src[self._pos].isdigit():
            self._pos += 1

    def _consume_int_suffix(self) -> None:
        m = _INT_SUFFIX.match(self._src, self._pos)
        if m:
            self._pos = m.end()

    def _scan_char_const(self) -> Token:
        start = self._pos
        self._consume_char_literal(start)
        return Token("CHAR_CONST", self._src[start:self._pos], self.lineno, start)

    def _scan_prefixed_char_const(self) -> Token:
        start = self._pos
        prefix_ch = self._src[self._pos]
        self._pos += 1  # skip prefix
        self._consume_char_literal(start + 1)
        tok_type = "WCHAR_CONST" if prefix_ch == "L" else "CHAR_CONST"
        return Token(tok_type, self._src[start:self._pos], self.lineno, start)

    def _consume_char_literal(self, start_after_quote: int) -> None:
        src = self._src
        assert src[self._pos] == "'"
        self._pos += 1
        while self._pos < len(src):
            c = src[self._pos]
            if c == "\\":
                self._pos += 2
                continue
            if c == "'":
                self._pos += 1
                return
            if c == "\n":
                self._error(self._pos, "unterminated character constant")
                return
            self._pos += 1
        self._error(self._pos, "unterminated character constant")

    def _scan_string_literal(self) -> Token:
        src = self._src
        start = self._pos
        assert src[self._pos] == '"'
        self._pos += 1
        while self._pos < len(src):
            c = src[self._pos]
            if c == "\\":
                self._pos += 2
                continue
            if c == '"':
                self._pos += 1
                return Token(
                    "STRING_LITERAL", src[start:self._pos], self.lineno, start,
                )
            if c == "\n":
                self._error(self._pos, "unterminated string literal")
                self._pos += 1
                self.lineno += 1
                continue
            self._pos += 1
        return Token("STRING_LITERAL", src[start:self._pos], self.lineno, start)

    def _scan_wstring_literal(self) -> Token:
        src = self._src
        start = self._pos
        assert src[self._pos] == "L" and src[self._pos + 1] == '"'
        self._pos += 1  # consume L
        # Reuse string_literal scanner, but tag as WSTRING_LITERAL
        tok = self._scan_string_literal()
        return Token("WSTRING_LITERAL", src[start:self._pos], self.lineno, start)

    def _scan_operator(self) -> Token:
        src = self._src
        start = self._pos
        # Longest-match multi-char first
        for op, tok_type in _MULTI_CHAR_OPS:
            if src.startswith(op, self._pos):
                self._pos += len(op)
                return Token(tok_type, op, self.lineno, start)
        ch = src[self._pos]
        tok_type = _SINGLE_CHAR_OPS.get(ch)
        if tok_type is None:
            self._error(self._pos, f"stray character {ch!r}")
            self._pos += 1
            # Return a synthetic token so upper layer keeps going.
            return Token("error", ch, self.lineno, start)

        self._pos += 1
        # Brace callbacks for scope tracking
        if ch == "{":
            self.on_lbrace_func()
        elif ch == "}":
            self.on_rbrace_func()
        return Token(tok_type, ch, self.lineno, start)

    def _scan_pphash(self) -> Token:
        """Handle ``#`` at start of (possibly-indented) line.

        Three cases:
          - ``# <num> [...]`` or ``#line <num> [...]`` — GCC/cpp line
            directive. Update self.lineno / self.filename, consume
            rest of line, loop.
          - ``#pragma ...`` — consume to EOL, drop.
          - Bare ``#`` — emit PPHASH token.
        """
        src = self._src
        start = self._pos
        self._pos += 1  # consume '#'
        # Skip indent after #
        j = self._pos
        while j < len(src) and src[j] in " \t":
            j += 1

        # Peek keyword
        rest_start = j
        if src.startswith("line", j):
            j += 4
        # Check if next non-space char is a digit ⇒ line directive
        k = j
        while k < len(src) and src[k] in " \t":
            k += 1
        if k < len(src) and src[k].isdigit():
            # Consume line number
            num_start = k
            while k < len(src) and src[k].isdigit():
                k += 1
            lineno = int(src[num_start:k])
            # Optional filename (quoted)
            while k < len(src) and src[k] in " \t":
                k += 1
            if k < len(src) and src[k] == '"':
                fname_start = k + 1
                k += 1
                while k < len(src) and src[k] != '"':
                    k += 1
                fname = src[fname_start:k]
                if k < len(src):
                    k += 1  # closing quote
                self.filename = fname
            # Consume rest of line
            while k < len(src) and src[k] != "\n":
                k += 1
            if k < len(src):
                k += 1  # consume newline
            self._pos = k
            self.lineno = lineno
            # Return the next real token (tail call via scanner loop)
            return self._scan()
        if src.startswith("pragma", rest_start):
            # Consume to end of line
            while self._pos < len(src) and src[self._pos] != "\n":
                self._pos += 1
            if self._pos < len(src):
                self._pos += 1
                self.lineno += 1
            return self._scan()
        # Bare # — rare; emit PPHASH
        return Token("PPHASH", "#", self.lineno, start)

    def _error(self, pos: int, msg: str) -> None:
        line = self.lineno
        # Column computation
        last_cr = self._src.rfind("\n", 0, pos)
        col = pos - last_cr
        self.error_func(msg, line, col)
