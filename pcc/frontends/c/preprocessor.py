"""C preprocessor for pcc.

Supports:
  #include <stdio.h>                - owned platform headers
  #include "file.h"                 - user headers: read and inline
  #define NAME VALUE                - object-like macros
  #define NAME(a,b) ((a)+(b))       - function-like macros
  #define NAME                      - flag macros
  #undef NAME
  #ifdef / #ifndef / #if / #elif / #else / #endif
  #if defined(X) && (X > 5)        - full expression evaluation
  defined(NAME)                     - in #if expressions
  ## token pasting                  - in macro bodies
  # stringification                 - in function macro bodies
  __LINE__, __FILE__                - built-in macros
  _Pragma("...")                    - macro-expandable pragma operator
"""

import re
import os
import platform
import warnings
from pcc.driver.paths import resolve_pcc_dir_from_environment

IDENTIFIER_RE = re.compile(r"[a-zA-Z_]\w*")
_CPP_TOKEN_RE = re.compile(
    r'''(?:u8|u|U|L)?"(?:\\.|[^"\\])*"|(?:u|U|L)?'(?:\\.|[^'\\])*'|'''
    r"[A-Za-z_]\w*|(?:\d|\.\d)(?:[eEpP][+-]|[\w.])*|"
    r"%:%:|>>=|<<=|\.\.\.|->|\+\+|--|<<|>>|<=|>=|==|!=|&&|\|\||"
    r"\*=|/=|%=|\+=|-=|&=|\^=|\|=|##|<:|:>|<%|%>|%:|\S",
)


class _SourceLine:
    __slots__ = ("text", "physical_line")

    def __init__(self, text, physical_line):
        self.text = text
        self.physical_line = physical_line


def _source_line_records(source):
    """Translation-phase splicing and comments, preserving literal contents."""
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    spliced = []
    physical_lines = [1]
    physical_line = 1
    index = 0
    while index < len(source):
        if source.startswith("\\\r\n", index) or source.startswith("\\\n", index):
            index += 3 if source.startswith("\\\r\n", index) else 2
            physical_line += 1
            continue
        char = source[index]
        spliced.append(char)
        if char == "\n":
            physical_line += 1
            physical_lines.append(physical_line)
        index += 1
    source = "".join(spliced)
    pieces = []
    i = 0
    while i < len(source):
        ch = source[i]
        if ch in ('"', "'"):
            start = i
            i += 1
            while i < len(source):
                if source[i] == "\\":
                    i += 2
                elif source[i] == ch:
                    i += 1
                    break
                else:
                    i += 1
            pieces.append(source[start:i])
        elif source.startswith("/*", i):
            end = source.find("*/", i + 2)
            if end < 0:
                raise RuntimeError("owned preprocessor: unterminated comment")
            pieces.append(" " + "\n" * source[i:end + 2].count("\n"))
            i = end + 2
        elif source.startswith("//", i):
            end = source.find("\n", i + 2)
            i = len(source) if end < 0 else end
            pieces.append(" ")
        else:
            pieces.append(ch)
            i += 1
    lines = "".join(pieces).split("\n")
    if lines[-1] == "":
        lines.pop()
    return [_SourceLine(text, physical_lines[index])
            for index, text in enumerate(lines)]


def _source_lines(source):
    """Keep the string-list utility contract used by native compiler tests."""
    return [line.text for line in _source_line_records(source)]


# System headers silently ignored (libc functions auto-declared from LIBC_FUNCTIONS)
SYSTEM_HEADERS = {
    "stdio.h",
    "stdlib.h",
    "string.h",
    "strings.h",
    "ctype.h",
    "math.h",
    "time.h",
    "unistd.h",
    "fcntl.h",
    "errno.h",
    "assert.h",
    "stdarg.h",
    "stddef.h",
    "stdint.h",
    "stdbool.h",
    "limits.h",
    "float.h",
    "signal.h",
    "setjmp.h",
    "locale.h",
    "inttypes.h",
    "iso646.h",
    "wchar.h",
    "wctype.h",
    "sys/types.h",
    "sys/stat.h",
}

# Clang-compatible feature-test operators. They are evaluated in #if before
# macro expansion (a header name or identifier operand must not expand) and
# answer from pcc's own capabilities: only keyword-backed C11 features and
# compat-erased nullability qualifiers are claimed; no attributes, builtins or
# warnings are, so portable fallbacks stay selected. Like clang,
# ``defined(__has_include)`` and ``#ifdef __has_feature`` hold.
_INCLUDE_TEST_OPERATORS = frozenset(("__has_include", "__has_include_next"))
_UNCLAIMED_FEATURE_OPERATORS = frozenset((
    "__has_feature", "__has_extension", "__has_builtin", "__has_attribute",
    "__has_c_attribute", "__has_cpp_attribute", "__has_declspec_attribute",
    "__has_warning",
))
_FEATURE_TEST_OPERATORS = (
    _INCLUDE_TEST_OPERATORS | _UNCLAIMED_FEATURE_OPERATORS | frozenset(("__is_identifier",))
)
# __has_feature/__has_extension names backed by the C keyword table.
_KEYWORD_BACKED_FEATURES = frozenset((
    "c_alignas", "c_alignof", "c_generic_selections", "c_static_assert", "c_thread_local",
))

BUILTIN_DEFINES = {
    "NULL": "0",
    "EOF": "(-1)",
    "EXIT_SUCCESS": "0",
    "EXIT_FAILURE": "1",
    "RAND_MAX": "2147483647",
    "INT_MAX": "2147483647",
    "INT_MIN": "(-2147483647-1)",
    "CHAR_BIT": "8",
    "CHAR_MAX": "127",
    "UCHAR_MAX": "255",
    "SHRT_MAX": "32767",
    "USHRT_MAX": "65535",
    "UINT_MAX": "4294967295",
    "LONG_MAX": "9223372036854775807",
    "ULONG_MAX": "18446744073709551615",
    "LLONG_MAX": "9223372036854775807",
    "LLONG_MIN": "(-9223372036854775807-1)",
    "ULLONG_MAX": "18446744073709551615",
    "SIZE_MAX": "18446744073709551615",
    "INTPTR_MAX": "9223372036854775807",
    "FLT_MAX": "3.402823466e+38",
    "DBL_MAX": "1.7976931348623158e+308",
    "FLT_MIN": "1.175494351e-38",
    "DBL_MIN": "2.2250738585072014e-308",
    "FLT_MANT_DIG": "24",
    "DBL_MANT_DIG": "53",
    "FLT_MAX_EXP": "128",
    "DBL_MAX_EXP": "1024",
    "HUGE_VAL": "1e309",
    "HUGE_VALF": "1e39f",
    "true": "1",
    "false": "0",
    "__STDC__": "1",
    "__STDC_VERSION__": "201112",
}

# Type definitions injected before user code (like stddef.h / stdint.h)
TYPE_PREAMBLE = """
typedef long size_t;
typedef long ssize_t;
typedef long ptrdiff_t;
typedef long intptr_t;
typedef unsigned long uintptr_t;
typedef void *va_list;
typedef long long intmax_t;
typedef unsigned long long uintmax_t;
typedef int sig_atomic_t;
typedef int wchar_t;
typedef int wint_t;
typedef long off_t;
typedef long clock_t;
typedef long time_t;
typedef int pid_t;
typedef unsigned int mode_t;
typedef char FILE;
"""


class _CppExprError(Exception):
    """Raised by ``_eval_cpp_expr`` when a ``#if`` expression is
    malformed or contains a construct beyond the narrow subset we
    recognize."""


class _IncompleteMacroInvocation(RuntimeError):
    """More physical source lines may complete a function-like invocation."""


def _eval_cpp_expr(src: str, target_triple=None) -> int:
    """Evaluate a C preprocessor ``#if`` expression to an integer.

    Accepts the subset the preprocessor produces after macro expansion
    and unknown-identifier→0 replacement: integer literals (decimal,
    hex, octal, binary, with optional L/U/LL suffix), character constants,
    parens, unary
    ``+ - ~ !``, binary ``* / %``, ``+ -``, shifts ``<< >>``, compare
    ``< <= > >= == !=``, bitwise ``& ^ |``, logical ``&& ||``, ternary
    ``a ? b : c``. No function calls, no names. C semantics for
    booleans (``!0 == 1``, ``!x == 0`` for nonzero x).

    Short-circuits ``&&``/``||`` and the untaken ``?:`` branch so dead
    ``1/0`` on the other side doesn't raise — matches C.

    Raises :class:`_CppExprError` on any failure in the live path.
    """
    p = _CppExprParser(src, target_triple)
    tree = p.parse_ternary()
    p.skip_ws()
    if p.pos < len(p.src):
        raise _CppExprError(f"trailing input at pos {p.pos}: {p.src[p.pos:]!r}")
    return _eval_tree(tree)


def _eval_tree(node) -> int:
    """Evaluate a preprocessor-expression tree with C short-circuit
    semantics. Each node is a tuple: ``(op, *args)``."""
    op = node[0]
    if op == "lit":
        return node[1]
    if op == "u+":
        return _eval_tree(node[1])
    if op == "u-":
        return -_eval_tree(node[1])
    if op == "~":
        return ~_eval_tree(node[1])
    if op == "!":
        return 0 if _eval_tree(node[1]) else 1
    if op == "&&":
        return 1 if _eval_tree(node[1]) and _eval_tree(node[2]) else 0
    if op == "||":
        return 1 if _eval_tree(node[1]) or _eval_tree(node[2]) else 0
    if op == "?:":
        return _eval_tree(node[2]) if _eval_tree(node[1]) else _eval_tree(node[3])
    l = _eval_tree(node[1])
    r = _eval_tree(node[2])
    if op == "+":  return l + r
    if op == "-":  return l - r
    if op == "*":  return l * r
    if op == "/":
        if r == 0:
            raise _CppExprError("division by zero")
        return int(l / r) if (l < 0) ^ (r < 0) else l // r
    if op == "%":
        if r == 0:
            raise _CppExprError("modulo by zero")
        q = int(l / r) if (l < 0) ^ (r < 0) else l // r
        return l - q * r
    if op == "<<": return l << r
    if op == ">>": return l >> r
    if op == "<":  return 1 if l < r else 0
    if op == "<=": return 1 if l <= r else 0
    if op == ">":  return 1 if l > r else 0
    if op == ">=": return 1 if l >= r else 0
    if op == "==": return 1 if l == r else 0
    if op == "!=": return 1 if l != r else 0
    if op == "|":  return l | r
    if op == "^":  return l ^ r
    if op == "&":  return l & r
    raise _CppExprError(f"unknown op {op}")


class _CppExprParser:
    """Recursive-descent parser producing a small tagged-tuple tree."""

    def __init__(self, src: str, target_triple=None) -> None:
        self.target_triple = target_triple
        self.src = src
        self.pos = 0

    def skip_ws(self) -> None:
        while self.pos < len(self.src) and self.src[self.pos] in " \t\r\n":
            self.pos += 1

    def _peek_tok(self, tok: str) -> bool:
        self.skip_ws()
        return self.src[self.pos:self.pos + len(tok)] == tok

    def _eat(self, tok: str) -> bool:
        if self._peek_tok(tok):
            self.pos += len(tok)
            return True
        return False

    def _expect(self, tok: str) -> None:
        if not self._eat(tok):
            raise _CppExprError(f"expected {tok!r} at pos {self.pos}")

    def parse_ternary(self):
        cond = self.parse_or()
        if self._eat("?"):
            then_n = self.parse_ternary()
            self._expect(":")
            else_n = self.parse_ternary()
            return ("?:", cond, then_n, else_n)
        return cond

    def parse_or(self):
        v = self.parse_and()
        while self._eat("||"):
            v = ("||", v, self.parse_and())
        return v

    def parse_and(self):
        v = self.parse_bitor()
        while self._eat("&&"):
            v = ("&&", v, self.parse_bitor())
        return v

    def parse_bitor(self):
        v = self.parse_bitxor()
        while self._peek_tok("|") and not self._peek_tok("||"):
            self.pos += 1
            v = ("|", v, self.parse_bitxor())
        return v

    def parse_bitxor(self):
        v = self.parse_bitand()
        while self._eat("^"):
            v = ("^", v, self.parse_bitand())
        return v

    def parse_bitand(self):
        v = self.parse_eq()
        while self._peek_tok("&") and not self._peek_tok("&&"):
            self.pos += 1
            v = ("&", v, self.parse_eq())
        return v

    def parse_eq(self):
        v = self.parse_rel()
        while True:
            if self._eat("=="):
                v = ("==", v, self.parse_rel())
            elif self._eat("!="):
                v = ("!=", v, self.parse_rel())
            else:
                break
        return v

    def parse_rel(self):
        v = self.parse_shift()
        while True:
            if self._eat("<="):
                v = ("<=", v, self.parse_shift())
            elif self._eat(">="):
                v = (">=", v, self.parse_shift())
            elif self._peek_tok("<") and not self._peek_tok("<<"):
                self.pos += 1
                v = ("<", v, self.parse_shift())
            elif self._peek_tok(">") and not self._peek_tok(">>"):
                self.pos += 1
                v = (">", v, self.parse_shift())
            else:
                break
        return v

    def parse_shift(self):
        v = self.parse_add()
        while True:
            if self._eat("<<"):
                v = ("<<", v, self.parse_add())
            elif self._eat(">>"):
                v = (">>", v, self.parse_add())
            else:
                break
        return v

    def parse_add(self):
        v = self.parse_mul()
        while True:
            if self._eat("+"):
                v = ("+", v, self.parse_mul())
            elif self._eat("-"):
                v = ("-", v, self.parse_mul())
            else:
                break
        return v

    def parse_mul(self):
        v = self.parse_unary()
        while True:
            if self._eat("*"):
                v = ("*", v, self.parse_unary())
            elif self._eat("/"):
                v = ("/", v, self.parse_unary())
            elif self._eat("%"):
                v = ("%", v, self.parse_unary())
            else:
                break
        return v

    def parse_unary(self):
        self.skip_ws()
        if self._eat("+"):
            return ("u+", self.parse_unary())
        if self._eat("-"):
            return ("u-", self.parse_unary())
        if self._eat("~"):
            return ("~", self.parse_unary())
        if self._eat("!"):
            return ("!", self.parse_unary())
        return self.parse_primary()

    def parse_primary(self):
        self.skip_ws()
        if self._eat("("):
            v = self.parse_ternary()
            self._expect(")")
            return v
        if (self.src[self.pos:self.pos + 1] == "'"
                or self.src[self.pos:self.pos + 2] in ("L'", "u'", "U'")):
            return ("lit", self.parse_character())
        return ("lit", self.parse_number())

    def parse_character(self) -> int:
        from pcc.frontends.c.c_character_literals import parse_c_character_constant

        try:
            value, _type_name, self.pos = parse_c_character_constant(
                self.src, self.pos, self.target_triple,
            )
        except ValueError as exc:
            raise _CppExprError(str(exc)) from exc
        return value

    def parse_number(self) -> int:
        self.skip_ws()
        s = self.src
        start = self.pos
        if start >= len(s) or not s[start].isdigit():
            raise _CppExprError(
                f"expected integer at pos {start}: {s[start:start + 20]!r}"
            )
        if s[start] == "0" and start + 1 < len(s) and s[start + 1] in "xXbB":
            base = 16 if s[start + 1] in "xX" else 2
            self.pos = start + 2
            digits_start = self.pos
            valid = "0123456789abcdefABCDEF" if base == 16 else "01"
            while self.pos < len(s) and s[self.pos] in valid:
                self.pos += 1
            digits = s[digits_start:self.pos]
            if not digits:
                raise _CppExprError(f"empty hex/bin literal at pos {start}")
            val = int(digits, base)
        elif s[start] == "0":
            self.pos = start + 1
            while self.pos < len(s) and s[self.pos] in "01234567":
                self.pos += 1
            val = int(s[start:self.pos], 8)
        else:
            self.pos = start
            while self.pos < len(s) and s[self.pos].isdigit():
                self.pos += 1
            val = int(s[start:self.pos])
        while self.pos < len(s) and s[self.pos] in "uUlL":
            self.pos += 1
        return val


class Macro:
    """Represents a #define macro (object-like or function-like)."""

    __slots__ = (
        "name", "params", "body", "is_function", "variadic_parameter", "_pattern",
    )

    def __init__(self, name, body, params=None):
        self.name = name
        self.body = body
        self.params = params  # None for object-like, list for function-like
        self.variadic_parameter = None
        if params:
            if params[-1] == "...":
                self.variadic_parameter = "__VA_ARGS__"
            else:
                named_variadic = re.fullmatch(r"([A-Za-z_]\w*)\s*\.\.\.", params[-1])
                if named_variadic:
                    self.variadic_parameter = named_variadic.group(1)
                    self.params = params[:-1] + ["..."]
        self.is_function = params is not None
        self._pattern = (
            re.compile(r"\b" + re.escape(name) + r"\b")
            if not self.is_function
            else None
        )


class _MacroToken:
    """A preprocessing token and the macros unavailable on its rescan."""

    __slots__ = ("leading", "text", "hide_set")

    def __init__(self, leading, text, hide_set=None):
        self.leading = leading
        self.text = text
        self.hide_set = frozenset() if hide_set is None else hide_set


class _MacroReplacement:
    __slots__ = ("tokens", "parameter", "paste")

    def __init__(self, tokens, parameter=None, paste=False):
        self.tokens = tokens
        self.parameter = parameter
        self.paste = paste


class Preprocessor:
    def __init__(self, base_dir=None, defines=None, include_dirs=None, cpp_args=None, target_triple=None):
        self.base_dir = base_dir or "."
        self.include_dirs = list(include_dirs or [])
        self.system_include_dirs = [os.path.join(
            os.path.dirname(resolve_pcc_dir_from_environment(__file__)),
            "utils", "fake_libc_include",
        )]
        self.forced_includes = []
        self.macros = {}
        self._expand_cache = {}
        self._identifier_cache = {}
        self.once_files = set()
        self._macro_stacks = {}
        self._pragma_generation = 0
        self._pragma_snapshot = None
        self._include_depth = 0
        self._line_no = 0
        self._file = "<string>"
        self._physical_file = "<string>"

        # Language/platform predefines only. Library names and typedefs come
        # from headers, so an undeclared name cannot acquire a fake definition.
        predefines = {
            "__STDC__": "1", "__STDC_VERSION__": "201112L",
            "__LP64__": "1", "_LP64": "1",
            "__ORDER_LITTLE_ENDIAN__": "1234", "__ORDER_BIG_ENDIAN__": "4321",
            "__BYTE_ORDER__": "1234", "__SIZEOF_POINTER__": "8",
        }
        # Fixed-width language facts plus baseline LP64 spellings. Target
        # overrides below come from the same ABI facts as SSA and codegen. Sources
        # such as ``typedef __SIZE_TYPE__ size_t;`` or ``#if __INT_MAX__``
        # rely on them; ``__GNUC__`` stays undefined so headers keep their
        # portable branches.
        predefines.update({
            # GCC-compatible builtin memory-order constants, also consumed by
            # the owned stdatomic.h. These encode language operations, not ABI.
            "__ATOMIC_RELAXED": "0",
            "__ATOMIC_CONSUME": "1",
            "__ATOMIC_ACQUIRE": "2",
            "__ATOMIC_RELEASE": "3",
            "__ATOMIC_ACQ_REL": "4",
            "__ATOMIC_SEQ_CST": "5",
            "__CHAR_BIT__": "8",
            "__SCHAR_MAX__": "127",
            "__SHRT_MAX__": "32767",
            "__INT_MAX__": "2147483647",
            "__LONG_MAX__": "9223372036854775807L",
            "__LONG_LONG_MAX__": "9223372036854775807LL",
            "__WCHAR_MAX__": "2147483647",
            "__WINT_MAX__": "2147483647",
            "__INTMAX_MAX__": "9223372036854775807L",
            "__UINTMAX_MAX__": "18446744073709551615UL",
            "__SIZE_MAX__": "18446744073709551615UL",
            "__PTRDIFF_MAX__": "9223372036854775807L",
            "__INTPTR_MAX__": "9223372036854775807L",
            "__UINTPTR_MAX__": "18446744073709551615UL",
            "__SIG_ATOMIC_MAX__": "2147483647",
            "__INT8_MAX__": "127",
            "__INT16_MAX__": "32767",
            "__INT32_MAX__": "2147483647",
            "__INT64_MAX__": "9223372036854775807L",
            "__UINT8_MAX__": "255",
            "__UINT16_MAX__": "65535",
            "__UINT32_MAX__": "4294967295U",
            "__UINT64_MAX__": "18446744073709551615UL",
            "__SIZEOF_SHORT__": "2",
            "__SIZEOF_INT__": "4",
            "__SIZEOF_LONG__": "8",
            "__SIZEOF_LONG_LONG__": "8",
            "__SIZEOF_FLOAT__": "4",
            "__SIZEOF_DOUBLE__": "8",
            "__SIZEOF_LONG_DOUBLE__": "8",
            "__SIZEOF_SIZE_T__": "8",
            "__SIZEOF_WCHAR_T__": "4",
            "__SIZEOF_WINT_T__": "4",
            "__SIZEOF_PTRDIFF_T__": "8",
            "__SIZE_TYPE__": "unsigned long",
            "__PTRDIFF_TYPE__": "long",
            "__WCHAR_TYPE__": "int",
            "__WINT_TYPE__": "int",
            "__INTMAX_TYPE__": "long",
            "__UINTMAX_TYPE__": "unsigned long",
            "__INTPTR_TYPE__": "long",
            "__UINTPTR_TYPE__": "unsigned long",
            "__CHAR16_TYPE__": "unsigned short",
            "__CHAR32_TYPE__": "unsigned int",
            "__INT8_TYPE__": "signed char",
            "__INT16_TYPE__": "short",
            "__INT32_TYPE__": "int",
            "__INT64_TYPE__": "long",
            "__UINT8_TYPE__": "unsigned char",
            "__UINT16_TYPE__": "unsigned short",
            "__UINT32_TYPE__": "unsigned int",
            "__UINT64_TYPE__": "unsigned long",
            "__INT_LEAST8_TYPE__": "signed char",
            "__INT_LEAST16_TYPE__": "short",
            "__INT_LEAST32_TYPE__": "int",
            "__INT_LEAST64_TYPE__": "long",
            "__UINT_LEAST8_TYPE__": "unsigned char",
            "__UINT_LEAST16_TYPE__": "unsigned short",
            "__UINT_LEAST32_TYPE__": "unsigned int",
            "__UINT_LEAST64_TYPE__": "unsigned long",
            "__INT_FAST8_TYPE__": "signed char",
            "__INT_FAST16_TYPE__": "short",
            "__INT_FAST32_TYPE__": "int",
            "__INT_FAST64_TYPE__": "long",
            "__UINT_FAST8_TYPE__": "unsigned char",
            "__UINT_FAST16_TYPE__": "unsigned short",
            "__UINT_FAST32_TYPE__": "unsigned int",
            "__UINT_FAST64_TYPE__": "unsigned long",
            # IEEE float/double are common to the admitted targets.
            "__FLT_DECIMAL_DIG__": "9",
            "__FLT_DENORM_MIN__": "1.40129846e-45F",
            "__FLT_DIG__": "6",
            "__FLT_EPSILON__": "1.19209290e-7F",
            "__FLT_MANT_DIG__": "24",
            "__FLT_MAX__": "3.40282347e+38F",
            "__FLT_MAX_10_EXP__": "38",
            "__FLT_MAX_EXP__": "128",
            "__FLT_MIN__": "1.17549435e-38F",
            "__FLT_MIN_10_EXP__": "(-37)",
            "__FLT_MIN_EXP__": "(-125)",
            "__FLT_NORM_MAX__": "3.40282347e+38F",
            "__FLT_HAS_DENORM__": "1",
            "__FLT_HAS_INFINITY__": "1",
            "__FLT_HAS_QUIET_NAN__": "1",
            "__DBL_DECIMAL_DIG__": "17",
            "__DBL_DENORM_MIN__": "4.9406564584124654e-324",
            "__DBL_DIG__": "15",
            "__DBL_EPSILON__": "2.2204460492503131e-16",
            "__DBL_MANT_DIG__": "53",
            "__DBL_MAX__": "1.7976931348623157e+308",
            "__DBL_MAX_10_EXP__": "308",
            "__DBL_MAX_EXP__": "1024",
            "__DBL_MIN__": "2.2250738585072014e-308",
            "__DBL_MIN_10_EXP__": "(-307)",
            "__DBL_MIN_EXP__": "(-1021)",
            "__DBL_NORM_MAX__": "1.7976931348623157e+308",
            "__DBL_HAS_DENORM__": "1",
            "__DBL_HAS_INFINITY__": "1",
            "__DBL_HAS_QUIET_NAN__": "1",
            "__LDBL_HAS_DENORM__": "1",
            "__LDBL_HAS_INFINITY__": "1",
            "__LDBL_HAS_QUIET_NAN__": "1",
            "__FLT_RADIX__": "2",
        })
        from pcc.frontends.c.c_abi_layout import c_target_predefines, c_target_triple
        target_triple = c_target_triple(target_triple)
        self.target_triple = target_triple
        from pcc.backend.self_backend_target_match import target_os_name
        target_os = target_os_name(target_triple)
        machine = target_triple.split("-", 1)[0].lower()
        if machine in ("aarch64", "arm64"):
            predefines["__aarch64__"] = "1"
        elif machine in ("x86_64", "amd64"):
            predefines["__x86_64__"] = "1"
        if target_os == "darwin":
            predefines["__APPLE__"] = "1"
            predefines["__MACH__"] = "1"
            predefines["__PCC_HOST_DARWIN__"] = "1"
            # Darwin compilers enable -fblocks by default. pcc has no block
            # literals, so only the __block storage class is accepted (and
            # ignored); __BLOCKS__ stays undefined for ^-literal feature tests.
            predefines["__block"] = ""
        elif target_os == "linux":
            predefines["__linux__"] = "1"
            if machine in ("aarch64", "arm64"):
                predefines["__CHAR_UNSIGNED__"] = "1"
        elif target_os == "win32":
            predefines.pop("__LP64__", None)
            predefines.pop("_LP64", None)
            predefines.update({"_WIN32": "1", "_WIN64": "1", "_M_X64": "100",
                               "__SIZEOF_LONG__": "4", "__LONG_MAX__": "2147483647L",
                               "__SIZEOF_WCHAR_T__": "2", "__WCHAR_TYPE__": "unsigned short",
                               "__WCHAR_MAX__": "65535"})
            for key, value in list(predefines.items()):
                if key.endswith("_TYPE__") and value in ("long", "unsigned long"):
                    predefines[key] = value + " long"
                elif key.endswith("_MAX__") and value.endswith("L") and key != "__LONG_MAX__":
                    if not value.endswith("LL"):
                        predefines[key] = value + "L"
        predefines.update(c_target_predefines(target_triple))
        for name, value in predefines.items():
            self.macros[name] = Macro(name, value)
        if defines:
            for name, value in defines.items():
                self.macros[name] = Macro(name, str(value))
        self._apply_cpp_args(list(cpp_args or []))

    def _apply_language_standard(self, standard):
        # The only preprocessor-visible effect of -std is __STDC_VERSION__,
        # which C89/C90 leaves undefined.  GNU dialects predefine the same
        # value as their ISO base.  An unknown standard still fails closed.
        versions = {
            "c89": None, "c90": None, "gnu89": None, "gnu90": None,
            "iso9899:1990": None,
            "iso9899:199409": "199409L",
            "c99": "199901L", "c9x": "199901L", "gnu99": "199901L",
            "gnu9x": "199901L", "iso9899:1999": "199901L",
            "c11": "201112L", "c1x": "201112L", "gnu11": "201112L",
            "gnu1x": "201112L", "iso9899:2011": "201112L",
            "c17": "201710L", "c18": "201710L", "gnu17": "201710L",
            "gnu18": "201710L", "iso9899:2017": "201710L",
            "iso9899:2018": "201710L",
            "c2x": "202311L", "c23": "202311L", "gnu2x": "202311L",
            "gnu23": "202311L",
        }
        if standard not in versions:
            raise ValueError("unsupported owned preprocessor option: -std=" + standard)
        version = versions[standard]
        if version is None:
            self.macros.pop("__STDC_VERSION__", None)
        else:
            self.macros["__STDC_VERSION__"] = Macro("__STDC_VERSION__", version)
        self._invalidate_expand_cache()

    def _apply_cpp_args(self, args):
        i = 0
        while i < len(args):
            arg = args[i]
            i += 1
            if arg in ("-D", "-U", "-I", "-isystem", "-include"):
                if i == len(args):
                    raise ValueError("missing argument for " + arg)
                option, value = arg, args[i]
                i += 1
            elif arg[:2] in ("-D", "-U", "-I"):
                option, value = arg[:2], arg[2:]
            elif arg == "-nostdinc":
                self.system_include_dirs.clear()
                continue
            elif arg in ("-fblocks", "-fno-blocks"):
                if arg == "-fblocks":
                    self.macros["__block"] = Macro("__block", "")
                else:
                    self.macros.pop("__block", None)
                self._invalidate_expand_cache()
                continue
            elif arg.startswith("-std=") or arg == "-ansi":
                self._apply_language_standard("c89" if arg == "-ansi" else arg[5:])
                continue
            else:
                raise ValueError("unsupported owned preprocessor option: " + arg)
            if option == "-D":
                name, sep, body = value.partition("=")
                self._handle_directive("define " + name + " " + (body if sep else "1"), [], [], False, self.base_dir)
            elif option == "-U":
                self.macros.pop(value, None)
                self._invalidate_expand_cache()
            elif option == "-I":
                self.include_dirs.append(value)
            elif option == "-isystem":
                self.system_include_dirs.insert(0, value)
            else:
                self.forced_includes.append(value)

    def preprocess(self, source):
        self._expand_cache.clear()
        self._identifier_cache.clear()
        prefix = []
        for filename in self.forced_includes:
            self._include('"' + filename + '"', prefix, self.base_dir)
        prefix.append(self._process_lines(_source_line_records(source), self.base_dir))
        return "\n".join(prefix)

    def _include(self, spelling, output, base_dir):
        spelling = spelling.strip()
        if not spelling.startswith(('"', '<')):
            spelling = self._expand_line(spelling).strip()
        quoted = spelling.startswith('"') and spelling.endswith('"')
        angled = spelling.startswith('<') and spelling.endswith('>')
        if not (quoted or angled):
            raise RuntimeError("malformed #include: " + spelling)
        filename = spelling[1:-1]
        paths = ([base_dir] if quoted else []) + self.include_dirs + self.system_include_dirs
        filepath = ""
        for directory in paths:
            candidate = os.path.abspath(os.path.join(directory, filename))
            if os.path.isfile(candidate):
                filepath = candidate
                break
        if not filepath:
            raise RuntimeError("owned preprocessor: header not found: " + filename)
        if filepath in self.once_files:
            return
        if self._include_depth >= 100:
            raise RuntimeError("owned preprocessor: include nesting limit: " + filepath)
        with open(filepath, "r", encoding="utf-8", errors="surrogateescape") as stream:
            source = stream.read()
        old_file, old_line = self._file, self._line_no
        old_physical_file = self._physical_file
        self._file = filepath
        self._physical_file = filepath
        self._include_depth += 1
        try:
            output.append(self._process_lines(_source_line_records(source), os.path.dirname(filepath)))
        finally:
            self._file, self._line_no = old_file, old_line
            self._physical_file = old_physical_file
            self._include_depth -= 1

    def _invalidate_expand_cache(self):
        self._expand_cache.clear()

    def _process_lines(self, lines, base_dir):
        output = []
        i = 0
        skip_stack = []  # stack of (skipping: bool, branch_taken: bool)
        line_offset = 0

        while i < len(lines):
            self._line_no = lines[i].physical_line + line_offset
            # A function argument may expand an alias of __LINE__/__FILE__.
            # Do not reuse its prescanned numeric/string result on another line.
            self._invalidate_expand_cache()
            line = lines[i].text
            stripped = line.strip()

            # Line continuation
            while stripped.endswith("\\") and i + 1 < len(lines):
                i += 1
                next_line = lines[i].text.strip()
                stripped = stripped[:-1] + " " + next_line
                line = stripped

            skipping = any(s[0] for s in skip_stack)

            if stripped.startswith("#"):
                directive = stripped[1:].strip()
                parts = directive.split(None, 1)
                if len(parts) == 2:
                    directive = parts[0] + " " + parts[1]
                next_line_number = self._handle_directive(
                    directive, output, skip_stack, skipping, base_dir
                )
                if next_line_number is not None:
                    next_physical = (lines[i + 1].physical_line if i + 1 < len(lines)
                                     else lines[i].physical_line + 1)
                    line_offset = next_line_number - next_physical
                i += 1
                continue

            if skipping:
                i += 1
                continue

            # Newlines are whitespace inside an ordinary macro invocation.
            # Retry only incomplete calls, without consuming a directive as an
            # argument or weakening the diagnostic for a genuinely open call.
            while True:
                self._pragma_snapshot = None
                try:
                    processed = self._expand_line(line)
                except _IncompleteMacroInvocation:
                    self._rollback_pragma_expansion()
                    if i + 1 >= len(lines) or lines[i + 1].text.lstrip().startswith("#"):
                        raise
                else:
                    tokens = _CPP_TOKEN_RE.findall(processed)
                    tail = self.macros.get(tokens[-1]) if tokens else None
                    following = i + 1
                    while following < len(lines) and not lines[following].text.strip():
                        following += 1
                    if not (
                        tail is not None and tail.is_function
                        and following < len(lines)
                        and lines[following].text.lstrip().startswith("(")
                    ):
                        break
                    self._rollback_pragma_expansion()
                i += 1
                line += "\n" + lines[i].text
            self._pragma_snapshot = None
            output.append(processed)
            i += 1

        if skip_stack:
            raise RuntimeError("owned preprocessor: unterminated conditional in " + self._file)
        return "\n".join(output)

    def _handle_directive(self, directive, output, skip_stack, skipping, base_dir):
        # ``#if(X)`` needs no space after the directive name.
        m = re.match(r"(if|elif|ifdef|ifndef)(?=[^\w\s])", directive)
        if m:
            directive = m.group(1) + " " + directive[m.end():]
        # --- Conditional directives (always processed for nesting) ---
        if directive.startswith("ifdef "):
            name = directive[6:].strip()
            if skipping:
                skip_stack.append((True, False))
            else:
                cond = self._is_defined(name)
                skip_stack.append((not cond, cond))
            return

        if directive.startswith("ifndef "):
            name = directive[7:].strip()
            if skipping:
                skip_stack.append((True, False))
            else:
                cond = not self._is_defined(name)
                skip_stack.append((not cond, cond))
            return

        if directive.startswith("if "):
            expr = directive[3:].strip()
            if skipping:
                skip_stack.append((True, False))
            else:
                cond = self._eval_condition(expr)
                skip_stack.append((not cond, cond))
            return

        if directive.startswith("elif "):
            expr = directive[5:].strip()
            if not skip_stack:
                raise RuntimeError("owned preprocessor: #elif without #if")
            parent_skip = (
                any(s[0] for s in skip_stack[:-1]) if len(skip_stack) > 1 else False
            )
            _, branch_taken = skip_stack[-1]
            if parent_skip or branch_taken:
                skip_stack[-1] = (True, branch_taken)
            else:
                cond = self._eval_condition(expr)
                skip_stack[-1] = (not cond, cond)
            return

        # C23 #elifdef / #elifndef: #elif with a defined test of one name.
        elif_defined = re.match(r"(elifn?def)\s+(\S+)\s*$", directive)
        if elif_defined:
            if not skip_stack:
                raise RuntimeError("owned preprocessor: #" + elif_defined.group(1) + " without #if")
            parent_skip = (
                any(s[0] for s in skip_stack[:-1]) if len(skip_stack) > 1 else False
            )
            _, branch_taken = skip_stack[-1]
            if parent_skip or branch_taken:
                skip_stack[-1] = (True, branch_taken)
            else:
                cond = self._is_defined(elif_defined.group(2))
                if elif_defined.group(1) == "elifndef":
                    cond = not cond
                skip_stack[-1] = (not cond, cond)
            return

        if directive.startswith("else"):
            if not skip_stack:
                raise RuntimeError("owned preprocessor: #else without #if")
            if skip_stack:
                parent_skip = (
                    any(s[0] for s in skip_stack[:-1]) if len(skip_stack) > 1 else False
                )
                _, branch_taken = skip_stack[-1]
                if parent_skip or branch_taken:
                    skip_stack[-1] = (True, branch_taken)
                else:
                    skip_stack[-1] = (False, True)
            return

        if directive.startswith("endif"):
            if skip_stack:
                skip_stack.pop()
            else:
                raise RuntimeError("owned preprocessor: #endif without #if")
            return

        if skipping:
            return

        # --- Non-conditional directives (only when not skipping) ---

        m = re.match(r"include\b(.*)", directive)
        if m:
            self._include(m.group(1), output, base_dir)
            return

        # #define NAME(params) body   -- function-like macro
        m = re.match(r"define\s+(\w+)\(([^)]*)\)\s*(.*)", directive)
        if m:
            name = m.group(1)
            params = [p.strip() for p in m.group(2).split(",") if p.strip()]
            body = m.group(3).strip()
            self.macros[name] = Macro(name, body, params)
            self._invalidate_expand_cache()
            return

        # #define NAME body   -- object-like macro
        m = re.match(r"define\s+(\w+)\s*(.*)", directive)
        if m:
            name = m.group(1)
            body = m.group(2).strip()
            self.macros[name] = Macro(name, body)
            self._invalidate_expand_cache()
            return

        # #undef NAME
        m = re.match(r"undef\s+(\w+)", directive)
        if m:
            self.macros.pop(m.group(1), None)
            self._invalidate_expand_cache()
            return

        line_directive = re.match(r"line\b(.*)", directive)
        if line_directive or re.match(r"\d+(\s|$)", directive):
            spelling = (self._expand_line(line_directive.group(1)).strip()
                        if line_directive else directive)
            marker = re.fullmatch(r'(\d+)(?:\s+("(?:\\.|[^"\\])*"))?(.*)', spelling)
            if marker is None or (line_directive and marker.group(3).strip()):
                raise RuntimeError("malformed line directive: #" + directive)
            if not line_directive and not re.fullmatch(r"(?:\s+[1-4])*\s*", marker.group(3)):
                raise RuntimeError("malformed linemarker: #" + directive)
            if marker.group(2):
                # Filename escapes quote/backslash exactly as __FILE__ emits.
                self._file = re.sub(r'\\([\\"])', r'\1', marker.group(2)[1:-1])
            self._invalidate_expand_cache()
            return int(marker.group(1))

        pragma = re.match(r"pragma\b(.*)", directive)
        if pragma:
            self._handle_pragma(pragma.group(1).strip())
        elif directive.startswith("error"):
            raise RuntimeError("owned preprocessor: #" + directive)
        elif directive.startswith("warning"):
            warnings.warn(directive[7:].strip(), stacklevel=2)
        elif directive:
            raise RuntimeError("unsupported preprocessor directive: #" + directive)
        return

    def _handle_pragma(self, pragma):
        """Share directive and operator semantics without expanding pragma tokens."""
        stack_pragma = re.fullmatch(r'(push_macro|pop_macro)\s*\(\s*"([A-Za-z_]\w*)"\s*\)', pragma)
        if stack_pragma:
            operation, name = stack_pragma.groups()
            stack = self._macro_stacks.setdefault(name, [])
            if operation == "push_macro":
                stack.append(self.macros.get(name))
            elif stack:
                saved = stack.pop()
                if saved is None:
                    self.macros.pop(name, None)
                else:
                    self.macros[name] = saved
                self._invalidate_expand_cache()
            return

        if pragma == "once":
            self.once_files.add(self._physical_file)

    def _rollback_pragma_expansion(self):
        # Appending a physical line retries macro expansion from its beginning.
        # Restore pragma effects first, so an earlier push/pop executes once.
        # Snapshot lazily: ordinary source lines do not copy the macro table.
        if self._pragma_snapshot is not None:
            self.macros, self._macro_stacks, self.once_files = self._pragma_snapshot
            self._pragma_snapshot = None
            self._invalidate_expand_cache()

    def _eval_condition(self, expr):
        """Evaluate a #if / #elif expression. Returns True/False."""
        # Comments were removed by translation-phase processing. Handle
        # defined before expanding its operand, without editing literal text.
        tokens = self._macro_tokens(expr)
        index = 0
        while index < len(tokens):
            if tokens[index].text != "defined":
                index += 1
                continue
            end = index + 1
            parenthesized = end < len(tokens) and tokens[end].text == "("
            if parenthesized:
                end += 1
            if end >= len(tokens) or not IDENTIFIER_RE.fullmatch(tokens[end].text):
                raise RuntimeError("owned preprocessor: invalid defined operand")
            value = "1" if self._is_defined(tokens[end].text) else "0"
            end += 1
            if parenthesized:
                if end >= len(tokens) or tokens[end].text != ")":
                    raise RuntimeError("owned preprocessor: invalid defined operand")
                end += 1
            tokens[index:end] = [_MacroToken(tokens[index].leading, value)]
            index += 1
        tokens = self._evaluate_feature_tests(tokens)
        expanded = self._serialize_macro_fragments(
            [token.leading + token.text for token in tokens]
        )
        # Now expand macros
        expanded = self._expand_line(expanded)
        # Replace identifier tokens only: letters and escapes inside character
        # constants have already survived macro expansion and must stay intact.
        expanded = "".join(
            token.leading + ("0" if IDENTIFIER_RE.fullmatch(token.text) else token.text)
            for token in self._macro_tokens(expanded)
        )
        # Evaluate using a narrow integer-only expression evaluator.
        # eval() is out of scope for the self-host target (see
        # scripts/audit_selfhost.py banned-builtin list).
        try:
            return bool(_eval_cpp_expr(expanded, self.target_triple))
        except _CppExprError as exc:
            raise RuntimeError(
                f"owned preprocessor: failed to evaluate #if expression: {expanded!r} ({exc})"
            ) from exc

    def _is_defined(self, name):
        return name in self.macros or name in _FEATURE_TEST_OPERATORS

    def _evaluate_feature_tests(self, tokens):
        """Replace feature-test operator calls with 0/1 before macro expansion."""
        index = 0
        while index < len(tokens):
            name = tokens[index].text
            # A program may supply its own fallback macro under the same name.
            if name not in _FEATURE_TEST_OPERATORS or name in self.macros:
                index += 1
                continue
            open_index = index + 1
            if open_index >= len(tokens) or tokens[open_index].text != "(":
                raise RuntimeError("owned preprocessor: " + name + " requires a parenthesized operand")
            depth = 0
            close_index = open_index
            while close_index < len(tokens):
                if tokens[close_index].text == "(":
                    depth += 1
                elif tokens[close_index].text == ")":
                    depth -= 1
                    if depth == 0:
                        break
                close_index += 1
            if close_index >= len(tokens):
                raise RuntimeError("owned preprocessor: unterminated " + name + " operand")
            operand = tokens[open_index + 1:close_index]
            if name in _INCLUDE_TEST_OPERATORS:
                found = self._header_exists(operand, name == "__has_include_next")
                value = "1" if found else "0"
            elif name == "__is_identifier":
                from pcc.frontends.c.parse.c_lex import KEYWORD_MAP

                text = "".join(token.text for token in operand).strip()
                value = "1" if IDENTIFIER_RE.fullmatch(text) and text not in KEYWORD_MAP else "0"
            elif name in ("__has_feature", "__has_extension"):
                feature = "".join(token.text for token in operand).strip()
                # Nullability qualifiers are accepted when the compile path
                # erases them (the C compat defines); otherwise not claimed.
                claimed = feature in _KEYWORD_BACKED_FEATURES or (
                    feature == "nullability" and "_Nonnull" in self.macros
                )
                value = "1" if claimed else "0"
            else:
                value = "0"
            tokens[index:close_index + 1] = [_MacroToken(tokens[index].leading, value)]
            index += 1
        return tokens

    def _header_exists(self, operand, include_next):
        spelling = "".join(token.leading + token.text for token in operand).strip()
        if not spelling.startswith(('"', '<')):
            spelling = self._expand_line(spelling).strip()
        quoted = len(spelling) > 1 and spelling.startswith('"') and spelling.endswith('"')
        angled = len(spelling) > 1 and spelling.startswith('<') and spelling.endswith('>')
        if not (quoted or angled):
            raise RuntimeError("owned preprocessor: malformed __has_include operand: " + spelling)
        directories = self.include_dirs + self.system_include_dirs
        in_header = os.path.isabs(self._physical_file)
        current_dir = os.path.dirname(self._physical_file) if in_header else self.base_dir
        if include_next and in_header:
            # Continue after the directory that supplied the current header;
            # in the primary source file this degrades to __has_include.
            current = os.path.abspath(current_dir)
            matches = [i for i, d in enumerate(directories) if os.path.abspath(d) == current]
            directories = directories[matches[0] + 1:] if matches else directories
        elif quoted:
            directories = [current_dir] + directories
        filename = spelling[1:-1]
        return any(os.path.isfile(os.path.join(directory, filename)) for directory in directories)

    @staticmethod
    def _macro_tokens(text):
        tokens = []
        end = 0
        for match in _CPP_TOKEN_RE.finditer(text):
            tokens.append(_MacroToken(text[end:match.start()], match.group()))
            end = match.end()
        return tokens

    @staticmethod
    def _copy_macro_tokens(tokens, leading=None, hide_set=None):
        copied = []
        for token in tokens:
            hidden = token.hide_set
            if hide_set is not None:
                hidden = hidden | hide_set
            copied.append(_MacroToken(token.leading, token.text, hidden))
        if copied and leading is not None:
            copied[0].leading = leading
        return copied

    def _expand_line(self, line):
        """Rescan tokens once, retaining macro-disable state on replacements.

        A string fixed point loses this state: a macro which calls a function
        of its own name then expands forever. Argument prescan and ## also
        have to preserve unavailable tokens, so neither a global disabled set
        nor caching intermediate strings implements the C rescan rules.
        """
        cached = self._expand_cache.get(line)
        if cached is not None:
            return cached
        pragma_generation = self._pragma_generation
        tokens = self._expand_tokens(self._macro_tokens(line))
        trailing = line[len(line.rstrip()):]
        result = self._serialize_macro_fragments(
            [token.leading + token.text for token in tokens] + [trailing]
        )
        if self._pragma_generation == pragma_generation:
            self._expand_cache[line] = result
        return result

    def _expand_tokens(self, tokens, defer_pragma=False):
        tokens = list(tokens)
        index = 0
        while index < len(tokens):
            token = tokens[index]
            name = token.text
            macro = self.macros.get(name)
            end = index + 1
            if name == "_Pragma":
                if defer_pragma and (end == len(tokens) or tokens[end].text != "("):
                    # Argument prescan may supply the operator name while the
                    # surrounding macro/source supplies its operand on rescan.
                    index += 1
                    continue
                if end == len(tokens):
                    raise _IncompleteMacroInvocation("unterminated _Pragma operator")
                if tokens[end].text != "(":
                    raise RuntimeError("malformed _Pragma: expected '(' and one string literal")
                args, end = self._macro_arguments(tokens, end + 1, name)
                argument = self._expand_tokens(args[0]) if len(args) == 1 else []
                literal = (re.fullmatch(r'(?:u8|u|U|L)?"((?:\\.|[^"\\])*)"', argument[0].text)
                           if len(argument) == 1 else None)
                if literal is None:
                    raise RuntimeError("malformed _Pragma: expected one string literal")
                # C11 6.10.9 only destringizes quotes/backslashes. Other escape
                # spellings remain intact for translation-phase-3 tokenization.
                pragma = re.sub(r'\\([\\"])', r'\1', literal.group(1))
                pragma = "\n".join(_source_lines(pragma)).strip()
                if self._pragma_snapshot is None:
                    self._pragma_snapshot = (
                        dict(self.macros),
                        {key: list(stack) for key, stack in self._macro_stacks.items()},
                        set(self.once_files),
                    )
                self._pragma_generation += 1
                self._handle_pragma(pragma)
                replacement = []
                hidden = token.hide_set
            elif name == "__LINE__":
                replacement = [_MacroToken("", str(self._line_no))]
                hidden = token.hide_set
            elif name == "__FILE__":
                spelling = '"' + self._file.replace("\\", "\\\\").replace('"', '\\"') + '"'
                replacement = [_MacroToken("", spelling)]
                hidden = token.hide_set
            elif macro is None or name in token.hide_set:
                index += 1
                continue
            elif not macro.is_function:
                replacement = self._substitute_params(macro, [])
                hidden = token.hide_set | {name}
            else:
                if end == len(tokens) or tokens[end].text != "(":
                    index += 1
                    continue
                args, end = self._macro_arguments(tokens, end + 1, name)
                # Only disable macros common to both ends of the invocation.
                # An alias can provide the name while the original source
                # provides its parentheses, or vice versa.
                hidden = (token.hide_set & tokens[end - 1].hide_set) | {name}
                replacement = self._substitute_params(macro, args)
            replacement = self._copy_macro_tokens(
                replacement, leading=token.leading, hide_set=hidden,
            )
            if not replacement and end < len(tokens):
                following = tokens[end]
                tokens[end] = _MacroToken(
                    token.leading + following.leading,
                    following.text, following.hide_set,
                )
            tokens[index:end] = replacement
            # Rescan the replacement together with the unconsumed input. This
            # permits an expanded alias to meet a following argument list.
        return tokens

    def _macro_arguments(self, tokens, start, name):
        args = []
        depth = 1
        argument_start = start
        index = start
        while index < len(tokens):
            spelling = tokens[index].text
            if spelling == "(":
                depth += 1
            elif spelling == ")":
                depth -= 1
                if depth == 0:
                    args.append(self._copy_macro_tokens(
                        tokens[argument_start:index], leading="",
                    ))
                    return args, index + 1
            elif spelling == "," and depth == 1:
                args.append(self._copy_macro_tokens(
                    tokens[argument_start:index], leading="",
                ))
                argument_start = index + 1
            index += 1
        raise _IncompleteMacroInvocation("unterminated macro invocation: " + name)

    @staticmethod
    def _serialize_macro_fragments(fragments):
        """Keep token boundaries unless ## explicitly made one token.

        Substitution can put separately produced tokens next to each other.
        Relexing their concatenation must not invent an operator, identifier,
        pp-number, literal prefix, or comment.
        """
        pieces = []
        previous = ""
        separated = True
        for fragment in fragments:
            end = 0
            for token in _CPP_TOKEN_RE.finditer(fragment):
                leading = fragment[end:token.start()]
                spelling = token.group()
                if leading:
                    pieces.append(leading)
                    separated = True
                if previous and not separated and (
                    _CPP_TOKEN_RE.findall(previous + spelling) != [previous, spelling]
                    or (previous == "/" and spelling in ("/", "*"))
                    or (previous == "." and spelling == ".")
                ):
                    pieces.append(" ")
                pieces.append(spelling)
                previous = spelling
                separated = False
                end = token.end()
            trailing = fragment[end:]
            if trailing:
                pieces.append(trailing)
                separated = True
        return "".join(pieces)

    @staticmethod
    def _stringify_argument(argument):
        pieces = []
        for token in argument:
            if pieces and token.leading:
                pieces.append(" ")
            pieces.append(token.text)
        spelling = "".join(pieces).replace("\\", "\\\\").replace('"', '\\"')
        return '"' + spelling + '"'

    def _substitute_params(self, macro, args):
        """Prescan ordinary arguments, stringify raw tokens, and then paste."""
        variadic = macro.variadic_parameter is not None
        parameters = macro.params[:-1] if variadic else (macro.params or [])
        if not parameters and len(args) == 1 and not args[0]:
            args = []
        if len(args) < len(parameters) or (not variadic and len(args) != len(parameters)):
            raise RuntimeError("wrong number of arguments for macro: " + macro.name)
        raw = {name: args[index] for index, name in enumerate(parameters)}
        if variadic:
            varargs = []
            for offset, argument in enumerate(args[len(parameters):]):
                if offset:
                    varargs.append(_MacroToken("", ","))
                varargs.extend(self._copy_macro_tokens(
                    argument, leading=" " if offset else "",
                ))
            raw[macro.variadic_parameter] = varargs
        missing_variadic = variadic and len(args) <= len(parameters)
        expanded = {}
        body = self._macro_tokens(macro.body)
        records = []
        index = 0
        while index < len(body):
            token = body[index]
            spelling = token.text
            parameter = spelling if spelling in raw else None
            paste = spelling == "##"
            if spelling == "#" and index + 1 < len(body) and body[index + 1].text in raw:
                index += 1
                # Stringification preserves source whitespace. Serialization
                # adds protective token separators for reparsing, which would
                # incorrectly turn expanded version tokens into "1 . 10 . 0".
                argument = raw[body[index].text]
                replacement = [_MacroToken(token.leading, self._stringify_argument(argument))]
            elif parameter is not None:
                adjacent_to_paste = (
                    (index > 0 and body[index - 1].text == "##")
                    or (index + 1 < len(body) and body[index + 1].text == "##")
                )
                if adjacent_to_paste:
                    replacement = raw[parameter]
                else:
                    if parameter not in expanded:
                        expanded[parameter] = self._expand_tokens(raw[parameter], defer_pragma=True)
                    replacement = expanded[parameter]
                replacement = self._copy_macro_tokens(replacement, leading=token.leading)
            else:
                replacement = [token]
            records.append(_MacroReplacement(replacement, parameter, paste))
            index += 1

        pasted = []
        index = 0
        while index < len(records):
            record = records[index]
            if record.paste:
                if not pasted or index + 1 >= len(records) or records[index + 1].paste:
                    raise RuntimeError("token paste at macro replacement boundary: " + macro.name)
                left = pasted.pop()
                right = records[index + 1]
                left_tokens = left.tokens
                right_tokens = right.tokens
                if (variadic and len(left_tokens) == 1 and left_tokens[0].text == ","
                        and right.parameter == macro.variadic_parameter):
                    replacement = [] if missing_variadic else left_tokens + right_tokens
                elif not left_tokens:
                    replacement = right_tokens
                elif not right_tokens:
                    replacement = left_tokens
                else:
                    last = left_tokens[-1]
                    first = right_tokens[0]
                    joined = last.text + first.text
                    if _CPP_TOKEN_RE.findall(joined) != [joined]:
                        raise RuntimeError("invalid token paste in macro: " + macro.name)
                    replacement = left_tokens[:-1] + [_MacroToken(
                        last.leading, joined, last.hide_set & first.hide_set,
                    )] + right_tokens[1:]
                pasted.append(_MacroReplacement(replacement))
                index += 2
            else:
                pasted.append(record)
                index += 1
        return [token for record in pasted for token in record.tokens]


def preprocess(source, base_dir=None, defines=None, include_dirs=None, cpp_args=None, target_triple=None):
    """Preprocess C source code."""
    pp = Preprocessor(base_dir=base_dir, defines=defines, include_dirs=include_dirs, cpp_args=cpp_args, target_triple=target_triple)
    return pp.preprocess(source)
