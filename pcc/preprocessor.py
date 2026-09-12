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
"""

import re
import os
import platform
import warnings

IDENTIFIER_RE = re.compile(r"[a-zA-Z_]\w*")
_CPP_TOKEN_RE = re.compile(
    r'''(?:u8|u|U|L)?"(?:\\.|[^"\\])*"|(?:u|U|L)?'(?:\\.|[^'\\])*'|'''
    r"[A-Za-z_]\w*|(?:\d|\.\d)[\w.]*(?:[eEpP][+-][\w.]*)?|##|\S",
)


def _source_lines(source):
    """Translation-phase splicing and comments, preserving literal contents."""
    source = source.replace("\\\r\n", "").replace("\\\n", "")
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
    return "".join(pieces).splitlines()

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


def _eval_cpp_expr(src: str) -> int:
    """Evaluate a C preprocessor ``#if`` expression to an integer.

    Accepts the subset the preprocessor produces after macro expansion
    and unknown-identifier→0 replacement: integer literals (decimal,
    hex, octal, binary, with optional L/U/LL suffix), parens, unary
    ``+ - ~ !``, binary ``* / %``, ``+ -``, shifts ``<< >>``, compare
    ``< <= > >= == !=``, bitwise ``& ^ |``, logical ``&& ||``, ternary
    ``a ? b : c``. No function calls, no names. C semantics for
    booleans (``!0 == 1``, ``!x == 0`` for nonzero x).

    Short-circuits ``&&``/``||`` and the untaken ``?:`` branch so dead
    ``1/0`` on the other side doesn't raise — matches C.

    Raises :class:`_CppExprError` on any failure in the live path.
    """
    p = _CppExprParser(src)
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

    def __init__(self, src: str) -> None:
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
        return ("lit", self.parse_number())

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

    __slots__ = ("name", "params", "body", "is_function", "_pattern")

    def __init__(self, name, body, params=None):
        self.name = name
        self.body = body
        self.params = params  # None for object-like, list for function-like
        self.is_function = params is not None
        self._pattern = (
            re.compile(r"\b" + re.escape(name) + r"\b")
            if not self.is_function
            else None
        )


class Preprocessor:
    def __init__(self, base_dir=None, defines=None, include_dirs=None, cpp_args=None):
        self.base_dir = base_dir or "."
        self.include_dirs = list(include_dirs or [])
        self.system_include_dirs = [os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "utils", "fake_libc_include",
        )]
        self.forced_includes = []
        self.macros = {}
        self._expand_cache = {}
        self._identifier_cache = {}
        self.once_files = set()
        self._include_depth = 0
        self._line_no = 0
        self._file = "<string>"

        # Language/platform predefines only. Library names and typedefs come
        # from headers, so an undeclared name cannot acquire a fake definition.
        predefines = {
            "__STDC__": "1", "__STDC_VERSION__": "201112L",
            "__LP64__": "1", "_LP64": "1",
            "__ORDER_LITTLE_ENDIAN__": "1234", "__ORDER_BIG_ENDIAN__": "4321",
            "__BYTE_ORDER__": "1234", "__SIZEOF_POINTER__": "8",
        }
        machine = platform.machine().lower()
        if machine in ("aarch64", "arm64"):
            predefines["__aarch64__"] = "1"
        elif machine in ("x86_64", "amd64"):
            predefines["__x86_64__"] = "1"
        if platform.system() == "Darwin":
            predefines["__APPLE__"] = "1"
            predefines["__MACH__"] = "1"
            predefines["__PCC_HOST_DARWIN__"] = "1"
        elif platform.system() == "Linux":
            predefines["__linux__"] = "1"
        for name, value in predefines.items():
            self.macros[name] = Macro(name, value)
        if defines:
            for name, value in defines.items():
                self.macros[name] = Macro(name, str(value))
        self._apply_cpp_args(list(cpp_args or []))

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
        prefix.append(self._process_lines(_source_lines(source), self.base_dir))
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
        self._file = filepath
        self._include_depth += 1
        try:
            output.append(self._process_lines(_source_lines(source), os.path.dirname(filepath)))
        finally:
            self._file, self._line_no = old_file, old_line
            self._include_depth -= 1

    def _invalidate_expand_cache(self):
        self._expand_cache.clear()

    def _process_lines(self, lines, base_dir):
        output = []
        i = 0
        skip_stack = []  # stack of (skipping: bool, branch_taken: bool)

        while i < len(lines):
            self._line_no = i + 1
            line = lines[i]
            stripped = line.strip()

            # Line continuation
            while stripped.endswith("\\") and i + 1 < len(lines):
                i += 1
                next_line = lines[i].strip()
                stripped = stripped[:-1] + " " + next_line
                line = stripped

            skipping = any(s[0] for s in skip_stack)

            if stripped.startswith("#"):
                directive = stripped[1:].strip()
                parts = directive.split(None, 1)
                if len(parts) == 2:
                    directive = parts[0] + " " + parts[1]
                self._handle_directive(
                    directive, output, skip_stack, skipping, base_dir
                )
                i += 1
                continue

            if skipping:
                i += 1
                continue

            # Apply macro expansion
            processed = self._expand_line(line)
            output.append(processed)
            i += 1

        if skip_stack:
            raise RuntimeError("owned preprocessor: unterminated conditional in " + self._file)
        return "\n".join(output)

    def _handle_directive(self, directive, output, skip_stack, skipping, base_dir):
        # --- Conditional directives (always processed for nesting) ---
        if directive.startswith("ifdef "):
            name = directive[6:].strip()
            if skipping:
                skip_stack.append((True, False))
            else:
                cond = name in self.macros
                skip_stack.append((not cond, cond))
            return

        if directive.startswith("ifndef "):
            name = directive[7:].strip()
            if skipping:
                skip_stack.append((True, False))
            else:
                cond = name not in self.macros
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

        if directive == "pragma once":
            self.once_files.add(self._file)
        elif directive.startswith("error"):
            raise RuntimeError("owned preprocessor: #" + directive)
        elif directive.startswith("warning"):
            warnings.warn(directive[7:].strip(), stacklevel=2)
        elif directive and not directive.startswith("pragma "):
            raise RuntimeError("unsupported preprocessor directive: #" + directive)
        return

    def _eval_condition(self, expr):
        """Evaluate a #if / #elif expression. Returns True/False."""
        # Strip C comments from expression
        expr = re.sub(r"/\*.*?\*/", "", expr).strip()
        expr = re.sub(r"//.*$", "", expr).strip()
        # Handle defined(NAME) and defined NAME BEFORE macro expansion
        # to avoid expanding the name away
        expanded = re.sub(
            r"\bdefined\s*\(\s*(\w+)\s*\)",
            lambda m: "1" if m.group(1) in self.macros else "0",
            expr,
        )
        expanded = re.sub(
            r"\bdefined\s+(\w+)",
            lambda m: "1" if m.group(1) in self.macros else "0",
            expanded,
        )
        # Now expand macros
        expanded = self._expand_line(expanded)
        # Replace any remaining identifiers with 0 (C standard behavior)
        expanded = re.sub(r"\b[a-zA-Z_]\w*\b", "0", expanded)
        # Evaluate using a narrow integer-only expression evaluator.
        # eval() is out of scope for the self-host target (see
        # scripts/audit_selfhost.py banned-builtin list).
        try:
            return bool(_eval_cpp_expr(expanded))
        except _CppExprError as exc:
            raise RuntimeError(
                f"owned preprocessor: failed to evaluate #if expression: {expanded!r} ({exc})"
            ) from exc

    def _expand_line(self, line):
        """Expand all macros in a line, handling both object and function macros."""
        prev = None
        iterations = 0
        while line != prev and iterations < 30:
            prev = line
            line = self._expand_once(line)
            iterations += 1
        if line != prev:
            raise RuntimeError("owned preprocessor: macro expansion did not converge")
        return line

    def _expand_once(self, line):
        """One pass of macro expansion — optimized."""
        original_line = line
        dynamic = "__LINE__" in line or "__FILE__" in line
        cached = None if dynamic else self._expand_cache.get(line)
        if cached is not None:
            return cached

        pieces = []
        pos = 0
        for token in _CPP_TOKEN_RE.finditer(line):
            if token.start() < pos:
                continue
            name = token.group()
            macro = self.macros.get(name)
            end = token.end()
            if name == "__LINE__":
                body = str(self._line_no)
            elif name == "__FILE__":
                body = '"' + self._file.replace("\\", "\\\\").replace('"', '\\"') + '"'
            elif macro is None:
                continue
            elif not macro.is_function:
                body = macro.body
            else:
                opening = end
                while opening < len(line) and line[opening].isspace():
                    opening += 1
                if opening == len(line) or line[opening] != "(":
                    continue
                args, end = self._find_macro_args(line, opening + 1)
                if args is None:
                    raise RuntimeError("unterminated macro invocation: " + name)
                body = self._substitute_params(macro, args)
            pieces.append(line[pos:token.start()])
            pieces.append(body)
            pos = end
        pieces.append(line[pos:])
        line = "".join(pieces)
        if not dynamic:
            self._expand_cache[original_line] = line
        return line

    def _expand_func_macro(self, line, macro):
        """Expand function-like macro invocations in line."""
        result = []
        pos = 0
        while pos < len(line):
            match = self._find_func_macro_call(line, macro.name, pos)
            if match is None:
                result.append(line[pos:])
                break
            start, args_start = match
            result.append(line[pos:start])
            args, end = self._find_macro_args(line, args_start)
            if args is not None:
                expanded = self._substitute_params(macro, args)
                result.append(expanded)
                pos = end
            else:
                result.append(line[pos:args_start])
                pos = args_start
        return "".join(result)

    def _find_func_macro_call(self, line, name, start):
        """Find the next function-like macro invocation using string scanning."""
        name_len = len(name)
        pos = start

        while True:
            idx = line.find(name, pos)
            if idx == -1:
                return None

            # The match must start at an identifier boundary.
            if idx > 0:
                prev = line[idx - 1]
                if prev == "_" or prev.isalnum():
                    pos = idx + name_len
                    continue

            arg_pos = idx + name_len
            while arg_pos < len(line) and line[arg_pos].isspace():
                arg_pos += 1

            if arg_pos < len(line) and line[arg_pos] == "(":
                return idx, arg_pos + 1

            pos = idx + name_len

    def _find_macro_args(self, line, start):
        """Find comma-separated arguments within balanced parentheses.
        Returns (list_of_args, end_position) or (None, 0)."""
        depth = 1
        pos = start
        args = []
        arg_start = start

        while pos < len(line) and depth > 0:
            c = line[pos]
            if c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    args.append(line[arg_start:pos].strip())
                    return args, pos + 1
            elif c == "," and depth == 1:
                args.append(line[arg_start:pos].strip())
                arg_start = pos + 1
            elif c == '"':
                # Skip string literal
                pos += 1
                while pos < len(line) and line[pos] != '"':
                    if line[pos] == "\\":
                        pos += 1
                    pos += 1
            elif c == "'":
                pos += 1
                while pos < len(line) and line[pos] != "'":
                    if line[pos] == "\\":
                        pos += 1
                    pos += 1
            pos += 1

        return None, 0

    def _substitute_params(self, macro, args):
        """Replace parameter names with arguments in macro body."""
        body = macro.body
        if not macro.params:
            return body
        # Handle __VA_ARGS__
        if macro.params[-1] == "...":
            regular = macro.params[:-1]
            va_args = (
                ", ".join(args[len(regular) :]) if len(args) > len(regular) else ""
            )
            for i, param in enumerate(regular):
                if i < len(args):
                    arg = args[i]
                    body = re.sub(
                        r"\b" + re.escape(param) + r"\b",
                        lambda _m, arg=arg: arg,
                        body,
                    )
            body = body.replace("__VA_ARGS__", va_args)
        else:
            for i, param in enumerate(macro.params):
                if i < len(args):
                    arg = args[i]
                    body = re.sub(
                        r"\b" + re.escape(param) + r"\b",
                        lambda _m, arg=arg: arg,
                        body,
                    )
        # Handle ## token pasting
        body = re.sub(r"\s*##\s*", "", body)
        return body


def preprocess(source, base_dir=None, defines=None, include_dirs=None, cpp_args=None):
    """Preprocess C source code."""
    pp = Preprocessor(base_dir=base_dir, defines=defines, include_dirs=include_dirs, cpp_args=cpp_args)
    return pp.preprocess(source)
