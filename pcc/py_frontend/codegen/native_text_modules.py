"""Native ``json`` and ``re`` module lowering helpers."""

from __future__ import annotations

from typing import Optional

from pcc.llvm_capi.compat import ir

from ..py_ast import Assign, Attr, BinOp, BoolLit, Call, Expr, IntLit, Name, StrLit
from .import_lowering import (
    _dataclass_field_names,
    _dataclass_field_value,
)

_I64 = ir.IntType(64)
_CSTR = ir.IntType(8).as_pointer()
_RE_LITERAL_SPLIT_META = frozenset(".^$*+?{}[]\\|()")
_RE_CONSTS = {
    "I": 2,
    "IGNORECASE": 2,
    "M": 8,
    "MULTILINE": 8,
    "S": 16,
    "DOTALL": 16,
    "X": 64,
    "VERBOSE": 64,
}
_RE_ALIAS_METHODS = frozenset(("match", "search", "findall"))


class NativeTextModulesLoweringMixin:
    @staticmethod
    def _native_re_hex_digit(ch: str) -> int:
        if "0" <= ch <= "9":
            return ord(ch) - ord("0")
        if "a" <= ch <= "f":
            return ord(ch) - ord("a") + 10
        if "A" <= ch <= "F":
            return ord(ch) - ord("A") + 10
        return -1

    @staticmethod
    def _native_re_literal_escape_value(ch: str) -> int:
        if ch == "n":
            return 10
        if ch == "t":
            return 9
        if ch == "r":
            return 13
        if ch == "f":
            return 12
        if ch == "v":
            return 11
        return ord(ch)

    def _native_re_strip_verbose_pattern(self, pattern: str) -> str:
        """Apply the lexical part of ``re.X`` to a literal pattern."""
        out = []
        in_class = False
        escaped = False
        in_comment = False
        for ch in pattern:
            if in_comment:
                if ch == "\n":
                    in_comment = False
                continue
            if escaped:
                out.append(ch)
                escaped = False
                continue
            if ch == "\\":
                out.append(ch)
                escaped = True
                continue
            if ch == "[":
                in_class = True
                out.append(ch)
                continue
            if ch == "]" and in_class:
                in_class = False
                out.append(ch)
                continue
            if not in_class and ch == "#":
                in_comment = True
                continue
            if not in_class and ch in " \t\n\r\f\v":
                continue
            out.append(ch)
        return "".join(out)

    def _native_re_findall_supported_pattern_text(self, pattern: str) -> bool:
        return pattern in (
            r"\b[a-z][\w$]*\b",
            r"\(.*?\)",
        )

    def _textwrap_literal_split_lines(self, text: str):
        lines = []
        start = 0
        i = 0
        n = len(text)
        while i < n:
            if text[i] == "\n":
                lines.append(text[start : i + 1])
                start = i + 1
            i += 1
        if start < n:
            lines.append(text[start:n])
        return lines

    def _textwrap_literal_line_body_and_end(self, line: str):
        n = len(line)
        if n >= 2 and line[n - 2 : n] == "\r\n":
            return line[: n - 2], "\r\n"
        if n >= 1 and (line[n - 1] == "\n" or line[n - 1] == "\r"):
            return line[: n - 1], line[n - 1 : n]
        return line, ""

    def _textwrap_literal_is_blank(self, text: str) -> bool:
        for ch in text:
            if ch != " " and ch != "\t":
                return False
        return True

    def _textwrap_literal_indent(self, text: str) -> str:
        i = 0
        n = len(text)
        while i < n and (text[i] == " " or text[i] == "\t"):
            i += 1
        return text[:i]

    def _textwrap_literal_common_prefix(self, a: str, b: str) -> str:
        n = len(a)
        if len(b) < n:
            n = len(b)
        i = 0
        while i < n and a[i] == b[i]:
            i += 1
        return a[:i]

    def _textwrap_dedent_literal_value(self, text: str) -> str:
        parts = []
        margin = None
        for line in self._textwrap_literal_split_lines(text):
            body, end = self._textwrap_literal_line_body_and_end(line)
            if self._textwrap_literal_is_blank(body):
                body = ""
            else:
                indent = self._textwrap_literal_indent(body)
                if margin is None:
                    margin = indent
                else:
                    margin = self._textwrap_literal_common_prefix(margin, indent)
            parts.append((body, end))
        if not margin:
            unchanged = []
            for item in parts:
                unchanged.append(item[0] + item[1])
            return "".join(unchanged)
        width = len(margin)
        out = []
        for item in parts:
            body = item[0]
            end = item[1]
            if body[:width] == margin:
                body = body[width:]
            out.append(body + end)
        return "".join(out)

    def _emit_native_textwrap_dedent_call(
        self,
        args: tuple[Expr, ...],
        kwargs: tuple[tuple[str, Expr], ...],
    ) -> Optional[ir.Value]:
        if kwargs or len(args) != 1:
            return None
        arg = args[0]
        if isinstance(arg, StrLit):
            return self._emit_str_literal(self._textwrap_dedent_literal_value(arg.value))
        result = self.builder.call(
            self.runtime["py_textwrap_dedent"],
            [self._emit_as_object(arg)],
            name=self._fresh("textwrap.dedent"),
        )
        self._emit_post_call_err_check(self._expr_span_or_none(arg))
        return result

    def _native_re_static_flags_value(self, expr: Expr | None) -> Optional[int]:
        if expr is None:
            return 0
        if isinstance(expr, IntLit):
            return int(expr.value)
        if isinstance(expr, Name):
            known = getattr(self, "_native_re_static_flag_aliases", {}).get(expr.ident)
            if known is not None:
                return known
            for stmt in getattr(self.ast_module, "body", ()):
                if not isinstance(stmt, Assign) or len(stmt.targets) != 1:
                    continue
                target = stmt.targets[0]
                if not isinstance(target, Name) or target.ident != expr.ident:
                    continue
                if isinstance(stmt.value, Name) and stmt.value.ident == expr.ident:
                    return None
                return self._native_re_static_flags_value(stmt.value)
            return None
        if (
            isinstance(expr, Attr)
            and isinstance(expr.obj, Name)
            and self._native_builtin_module_for_name(expr.obj.ident) == "re"
            and expr.name in _RE_CONSTS
        ):
            return _RE_CONSTS[expr.name]
        if isinstance(expr, BinOp) and expr.op == "|":
            lhs = self._native_re_static_flags_value(expr.lhs)
            rhs = self._native_re_static_flags_value(expr.rhs)
            if lhs is None or rhs is None:
                return None
            return lhs | rhs
        return None

    def _native_re_compile_alias_info(
        self,
        expr: Expr,
    ) -> Optional[tuple[str, int]]:
        if not isinstance(expr, Call) or expr.kwargs:
            return None
        func = expr.func
        if (
            not isinstance(func, Attr)
            or func.name != "compile"
            or not isinstance(func.obj, Name)
            or self._native_builtin_module_for_name(func.obj.ident) != "re"
        ):
            return None
        if len(expr.args) < 1 or len(expr.args) > 2:
            return None
        pattern = expr.args[0]
        if not isinstance(pattern, StrLit):
            return None
        flags = self._native_re_static_flags_value(
            expr.args[1] if len(expr.args) == 2 else None
        )
        if flags is None:
            return None
        if flags & 64:
            pattern_value = self._native_re_strip_verbose_pattern(pattern.value)
            flags &= ~64
        else:
            pattern_value = pattern.value
        return pattern_value, flags

    def _native_re_compile_alias_for_name(
        self,
        alias: str,
    ) -> Optional[tuple[str, int]]:
        current_func = getattr(self, "current_func_def", None)
        if current_func is not None:
            local_aliases = getattr(self, "_native_re_compile_local_aliases", {})
            key = (id(current_func), alias)
            if key in local_aliases:
                value = local_aliases[key]
                return value if value is not None else None
        return getattr(self, "_native_re_compile_aliases", {}).get(alias)

    def _native_re_compile_alias_uses_are_safe(
        self,
        alias: str,
        initial_stmt,
        scope_body=None,
    ) -> bool:
        def walk(obj, *, assign_target: bool = False) -> bool:
            if obj is None:
                return True
            if isinstance(obj, (str, int, bool, float, bytes)):
                return True
            if isinstance(obj, (tuple, list)):
                for item in obj:
                    if not walk(item, assign_target=assign_target):
                        return False
                return True
            if isinstance(obj, Name):
                return assign_target or obj.ident != alias
            if isinstance(obj, Attr):
                if isinstance(obj.obj, Name) and obj.obj.ident == alias:
                    return obj.name in _RE_ALIAS_METHODS
                return walk(obj.obj)
            if type(obj).__name__ == "Assign":
                if obj is not initial_stmt:
                    for target in getattr(obj, "targets", ()):
                        if isinstance(target, Name) and target.ident == alias:
                            return False
                        if not walk(target, assign_target=True):
                            return False
                return walk(getattr(obj, "value", None))
            if type(obj).__name__ == "AugAssign":
                target = getattr(obj, "target", None)
                if isinstance(target, Name) and target.ident == alias:
                    return False
                return walk(target, assign_target=True) and walk(
                    getattr(obj, "value", None)
                )
            for field_name in _dataclass_field_names(obj):
                if not walk(_dataclass_field_value(obj, field_name)):
                    return False
            return True

        if scope_body is None:
            scope_body = getattr(self.ast_module, "body", ())
        return walk(scope_body)

    def _native_re_class_compile_attr_string_value(
        self,
        class_name: str,
        attr_name: str,
        value_expr: Expr,
    ) -> Optional[str]:
        alias_info = self._native_re_compile_alias_info(value_expr)
        if alias_info is None:
            return None
        pattern, flags = alias_info
        if flags != 0:
            return None

        def is_target_attr(obj) -> bool:
            if not isinstance(obj, Attr) or obj.name != attr_name:
                return False
            if not isinstance(obj.obj, Name):
                return False
            return obj.obj.ident in (class_name, "self", "cls")

        def is_re_func(obj, names: tuple[str, ...]) -> bool:
            return (
                isinstance(obj, Attr)
                and obj.name in names
                and isinstance(obj.obj, Name)
                and self._native_builtin_module_for_name(obj.obj.ident) == "re"
            )

        def walk(obj) -> bool:
            if obj is None:
                return True
            if isinstance(obj, (str, int, bool, float, bytes)):
                return True
            if isinstance(obj, (tuple, list)):
                for item in obj:
                    if not walk(item):
                        return False
                return True
            if is_target_attr(obj):
                return False
            if isinstance(obj, Call):
                if (
                    is_re_func(obj.func, ("split", "findall"))
                    and len(obj.args) >= 1
                    and is_target_attr(obj.args[0])
                ):
                    for arg in obj.args[1:]:
                        if not walk(arg):
                            return False
                    for _key, value in obj.kwargs:
                        if not walk(value):
                            return False
                    return True
                if not walk(obj.func):
                    return False
                for arg in obj.args:
                    if not walk(arg):
                        return False
                for _key, value in obj.kwargs:
                    if not walk(value):
                        return False
                return True
            for field_name in _dataclass_field_names(obj):
                if not walk(_dataclass_field_value(obj, field_name)):
                    return False
            return True

        if not walk(getattr(self.ast_module, "body", ())):
            return None
        return pattern

    def _native_re_findall_supported_pattern(self, pattern: Expr) -> bool:
        if not isinstance(pattern, StrLit):
            return False
        return self._native_re_findall_supported_pattern_text(pattern.value)

    def _native_re_literal_split_pattern(self, pattern: Expr) -> bool:
        if not isinstance(pattern, StrLit):
            return False
        if pattern.value == "":
            return False
        for ch in pattern.value:
            if ch in _RE_LITERAL_SPLIT_META:
                return False
        return True

    def _emit_native_json_call(self, expr: Call) -> Optional[ir.Value]:
        attr = expr.func
        assert isinstance(attr, Attr)
        if (
            not isinstance(attr.obj, Name)
            or self._native_builtin_module_for_name(attr.obj.ident) != "json"
            or len(expr.args) != 1
        ):
            return None
        if attr.name == "load":
            if expr.kwargs:
                return None
            source = expr.args[0]
            if (
                not isinstance(source, Name)
                or not getattr(self, "_native_file_env_flags", {}).get(
                    source.ident,
                    False,
                )
            ):
                return None
            file_obj = self._emit_expr(source)
            text_obj = self.builder.call(
                self.runtime["py_file_read_all"],
                [file_obj],
                name=self._fresh("json.load.read"),
            )
            self._emit_post_call_err_check(getattr(expr, "span", None))
            result = self.builder.call(
                self.runtime["py_json_loads"],
                [text_obj],
                name=self._fresh("json.load"),
            )
            self._emit_post_call_err_check(getattr(expr, "span", None))
            # `py_file_read_all` returns a new string that nothing else owns
            # and `py_json_loads` borrows it, so this frame has to release it.
            # Pin the parsed result first: a moving backend can collect inside
            # the release and relocate it. Mirrors the re.sub lowering.
            self._gc_pin(result)
            self._gc_release(text_obj, "json.load.text")
            self._gc_unpin(result)
            # Both json ABIs return a NEW reference. Record the owner at
            # emission: the shape classifier sees only a DynType Call for a
            # module-level function and answers "not owned", so no release was
            # ever emitted and every parsed document leaked whole.
            self._note_owned_object_value(result)
            return result
        # Native strings are emitted as UTF-8, so literal
        # `ensure_ascii=False` selects the existing native behavior. Literal
        # `sort_keys` can be combined with it. Other keyword forms stay on the
        # general path until the runtime can implement their exact semantics.
        sort_keys = False
        seen_sort_keys = False
        seen_ensure_ascii = False
        if len(expr.kwargs) > 0 and attr.name != "dumps":
            return None
        for key, value in expr.kwargs:
            if key == "sort_keys" and not seen_sort_keys:
                if not isinstance(value, BoolLit):
                    return None
                sort_keys = value.value
                seen_sort_keys = True
                continue
            if key == "ensure_ascii" and not seen_ensure_ascii:
                if not isinstance(value, BoolLit) or value.value:
                    return None
                seen_ensure_ascii = True
                continue
            if key != "sort_keys" and key != "ensure_ascii":
                return None
            return None
        # Every py_json_* ABI below returns a NEW reference. The shape
        # classifier sees only a DynType Call on a module-level function and
        # answers "not owned", so no release was ever emitted: `json.dumps`
        # leaked its whole result string on every call (measured at length + 41
        # bytes per call, so a 200k-request gateway run leaked ~25 MB), and
        # `json.loads` leaked the whole parsed document (907 bytes and two
        # tracked containers per call on a small object). This is the same
        # class of defect `_native_re_call_returns_owned_object` documents for
        # the re lowerings; recording the owner at emission is exact, because
        # only this emitter knows whether its keyword gates fired or the call
        # fell through to generic dispatch.
        if attr.name == "loads":
            result = self.builder.call(
                self.runtime["py_json_loads"],
                [self._emit_as_object(expr.args[0])],
                name=self._fresh("json.loads"),
            )
            self._note_owned_object_value(result)
            return result
        if attr.name == "dumps":
            if sort_keys:
                result = self.builder.call(
                    self.runtime["py_json_dumps_ex"],
                    [self._emit_as_object(expr.args[0]), ir.Constant(_I64, 1)],
                    name=self._fresh("json.dumps"),
                )
                self._note_owned_object_value(result)
                return result
            result = self.builder.call(
                self.runtime["py_json_dumps"],
                [self._emit_as_object(expr.args[0])],
                name=self._fresh("json.dumps"),
            )
            self._note_owned_object_value(result)
            return result
        return None

    @staticmethod
    def _re_subset_parse_counts(pattern: str, j: int) -> tuple[int, int]:
        """Mirror of py_re_engine.c re_parse_counts for the checker.

        Returns (status, end): status 0 = malformed ('{' is a literal),
        1 = valid counted repeat, 2 = valid syntax but over the engine cap
        (engine rejects). Written in the bootstrap-safe dialect because this
        module is inside the self-host closure (fallback baseline pins 0).
        """
        n = len(pattern)
        k = j + 1
        m_val = -1
        n_val = -1
        inf = 0
        while k < n and "0" <= pattern[k] <= "9":
            if m_val < 0:
                m_val = 0
            m_val = m_val * 10 + (ord(pattern[k]) - 48)
            if m_val > 9999:
                return (0, j)
            k += 1
        if k < n and pattern[k] == ",":
            k += 1
            saw = 0
            while k < n and "0" <= pattern[k] <= "9":
                if n_val < 0:
                    n_val = 0
                n_val = n_val * 10 + (ord(pattern[k]) - 48)
                if n_val > 9999:
                    return (0, j)
                saw = 1
                k += 1
            if saw == 0:
                inf = 1
        else:
            n_val = m_val
        if k >= n or pattern[k] != "}":
            return (0, j)
        if m_val < 0 and n_val < 0 and inf == 0:
            return (0, j)
        m_eff = m_val
        if m_eff < 0:
            m_eff = 0
        if inf == 0 and n_val >= 0 and n_val < m_eff:
            return (0, j)
        if m_eff > 64 or (inf == 0 and n_val >= 0 and n_val > 64):
            return (2, k + 1)
        return (1, k + 1)

    @staticmethod
    def _re_engine_subset_supported(pattern: str) -> bool:
        """Conservative mirror of py_re_engine.c's strict subset parser.

        MUST stay a SUBSET of the C engine's accepted language: approving a
        pattern the engine rejects would turn the compile-time gate into a
        construction-time NotImplementedError. The inclusion is pinned by
        tests/python/test_re_engine_differential.py::test_frontend_checker_subset_of_engine.
        When unsure, return False. Written in the bootstrap-safe dialect
        (no set unions / typing generics / closures) because this module is
        inside the self-host closure.
        """
        literal_escapes = "ntrfv\\.*+?()[]{}|^$-/'\" ,:;=<>#!&~@%"
        class_extra = "dwsDWSb"
        n = len(pattern)
        i = 0
        while i < n:
            if ord(pattern[i]) >= 128:
                return False
            i += 1
        # atom-kind stack per group depth: 0 none, 1 single-byte atom,
        # 2 other (group/anchor/quantified)
        stack = [0]
        depth = 0
        seen_names = []
        i = 0
        while i < n:
            c = pattern[i]
            if c == "*" or c == "+" or c == "?":
                if stack[depth] == 0:
                    return False
                i += 1
                if i < n and pattern[i] == "?":
                    i += 1
                if i < n and (
                    pattern[i] == "*"
                    or pattern[i] == "+"
                    or pattern[i] == "?"
                    or pattern[i] == "{"
                ):
                    return False
                stack[depth] = 2
                continue
            if c == "{":
                status_end = NativeTextModulesLoweringMixin._re_subset_parse_counts(
                    pattern, i
                )
                status = status_end[0]
                end = status_end[1]
                if status == 0:
                    stack[depth] = 1
                    i += 1
                    continue
                if status == 2:
                    return False
                if stack[depth] != 1:
                    return False
                i = end
                if i < n and pattern[i] == "?":
                    i += 1
                if i < n and (
                    pattern[i] == "*"
                    or pattern[i] == "+"
                    or pattern[i] == "?"
                    or pattern[i] == "{"
                ):
                    return False
                stack[depth] = 2
                continue
            if c == "|":
                stack[depth] = 0
                i += 1
                continue
            if c == "(":
                if i + 1 < n and pattern[i + 1] == "?":
                    if pattern[i : i + 3] == "(?:":
                        i += 3
                    elif pattern[i : i + 4] == "(?P<":
                        j = i + 4
                        name = ""
                        while j < n and pattern[j] != ">":
                            ch = pattern[j]
                            is_alpha = (
                                ("A" <= ch <= "Z") or ("a" <= ch <= "z") or ch == "_"
                            )
                            is_digit = "0" <= ch <= "9"
                            if name == "":
                                if not is_alpha:
                                    return False
                            elif not (is_alpha or is_digit):
                                return False
                            name = name + ch
                            if len(name) >= 31:
                                return False
                            j += 1
                        if j >= n or name == "":
                            return False
                        if name in seen_names:
                            return False
                        seen_names.append(name)
                        i = j + 1
                    else:
                        return False
                else:
                    i += 1
                depth += 1
                if depth > 30:
                    return False
                stack.append(0)
                continue
            if c == ")":
                if depth == 0:
                    return False
                depth -= 1
                stack.pop()
                stack[depth] = 2
                i += 1
                continue
            if c == "[":
                j = i + 1
                if j < n and pattern[j] == "^":
                    j += 1
                first = 1
                ok = 0
                prev_lit = -1
                while j < n:
                    if pattern[j] == "]" and first == 0:
                        ok = 1
                        break
                    first = 0
                    if pattern[j] == "\\":
                        if j + 1 >= n:
                            return False
                        e = pattern[j + 1]
                        if e == "x":
                            if j + 3 >= n:
                                return False
                            hi_digit = (
                                NativeTextModulesLoweringMixin._native_re_hex_digit(
                                    pattern[j + 2]
                                )
                            )
                            lo_digit = (
                                NativeTextModulesLoweringMixin._native_re_hex_digit(
                                    pattern[j + 3]
                                )
                            )
                            if hi_digit < 0 or lo_digit < 0:
                                return False
                            lo_value = hi_digit * 16 + lo_digit
                            token_end = j + 4
                            if (
                                token_end + 1 < n
                                and pattern[token_end] == "-"
                                and pattern[token_end + 1] != "]"
                            ):
                                high_start = token_end + 1
                                if (
                                    high_start + 3 >= n
                                    or pattern[high_start] != "\\"
                                    or pattern[high_start + 1] != "x"
                                ):
                                    return False
                                high_hi = (
                                    NativeTextModulesLoweringMixin._native_re_hex_digit(
                                        pattern[high_start + 2]
                                    )
                                )
                                high_lo = (
                                    NativeTextModulesLoweringMixin._native_re_hex_digit(
                                        pattern[high_start + 3]
                                    )
                                )
                                if high_hi < 0 or high_lo < 0:
                                    return False
                                if high_hi * 16 + high_lo < lo_value:
                                    return False
                                j = high_start + 4
                            else:
                                j = token_end
                            prev_lit = lo_value
                            continue
                        if e not in class_extra and e not in literal_escapes:
                            return False
                        if e in "dwsDWS":
                            prev_lit = -1
                        elif e == "b":
                            prev_lit = 8
                        else:
                            prev_lit = NativeTextModulesLoweringMixin._native_re_literal_escape_value(
                                e
                            )
                        j += 2
                        continue
                    if ord(pattern[j]) >= 128:
                        return False
                    if (
                        pattern[j] == "-"
                        and prev_lit >= 0
                        and j + 1 < n
                        and pattern[j + 1] != "]"
                    ):
                        hi = pattern[j + 1]
                        if hi == "\\" or ord(hi) >= 128 or ord(hi) < prev_lit:
                            return False
                        prev_lit = -1
                        j += 2
                        continue
                    prev_lit = ord(pattern[j])
                    j += 1
                if ok == 0:
                    return False
                i = j + 1
                stack[depth] = 1
                continue
            if c == "\\":
                if i + 1 >= n:
                    return False
                e = pattern[i + 1]
                if e == "d" or e == "D" or e == "w" or e == "W" or e == "s" or e == "S":
                    stack[depth] = 1
                elif e == "b" or e == "B" or e == "A" or e == "Z":
                    stack[depth] = 2
                elif e == "x":
                    if i + 3 >= n:
                        return False
                    if (
                        NativeTextModulesLoweringMixin._native_re_hex_digit(
                            pattern[i + 2]
                        )
                        < 0
                        or NativeTextModulesLoweringMixin._native_re_hex_digit(
                            pattern[i + 3]
                        )
                        < 0
                    ):
                        return False
                    stack[depth] = 1
                    i += 4
                    continue
                elif e in literal_escapes:
                    stack[depth] = 1
                else:
                    return False
                i += 2
                continue
            if c == "^" or c == "$":
                stack[depth] = 2
                i += 1
                continue
            stack[depth] = 1
            i += 1
        if depth != 0:
            return False
        return True

    def _emit_native_re_call(self, expr: Call) -> Optional[ir.Value]:
        attr = expr.func
        assert isinstance(attr, Attr)
        if isinstance(attr.obj, Name):
            alias_info = self._native_re_compile_alias_for_name(attr.obj.ident)
            if alias_info is not None:
                return self._emit_native_re_compile_alias_method_call(
                    alias_info,
                    attr.name,
                    expr.args,
                    expr.kwargs,
                )
        if (
            not isinstance(attr.obj, Name)
            or self._native_builtin_module_for_name(attr.obj.ident) != "re"
        ):
            return None
        if attr.name == "escape" and not expr.kwargs and len(expr.args) == 1:
            arg = self._emit_as_object(expr.args[0])
            return self.builder.call(
                self.runtime["py_re_escape"],
                [arg],
                name=self._fresh("re.escape"),
            )
        if attr.name == "compile" and not expr.kwargs and 1 <= len(expr.args) <= 2:
            pattern_expr = expr.args[0]
            flags_value = self._native_re_static_flags_value(
                expr.args[1] if len(expr.args) == 2 else None
            )
            pattern_value = (
                pattern_expr.value if isinstance(pattern_expr, StrLit) else None
            )
            if (
                pattern_value is not None
                and flags_value is not None
                and flags_value & 64
            ):
                pattern_value = self._native_re_strip_verbose_pattern(pattern_value)
                flags_value &= ~64
            if (
                pattern_value is not None
                and flags_value is not None
                and (flags_value & ~26) == 0  # re.I|re.M|re.S engine mask
                and self._re_engine_subset_supported(pattern_value)
            ):
                result = self.builder.call(
                    self.runtime["py_re_compile_obj"],
                    [
                        self._emit_str_literal(pattern_value),
                        ir.Constant(_I64, flags_value),
                    ],
                    name=self._fresh("re.compile.obj"),
                )
                self._note_owned_object_value(result)
                self._emit_post_call_err_check(getattr(expr, "span", None))
                return result
            if (
                pattern_value is None
                and flags_value is not None
                and (flags_value & ~26) == 0
            ):
                # Runtime-composed pattern strings cannot use the static
                # subset checker, but they still belong to the same native
                # Pattern contract.  Compile them in the runtime engine and
                # let that engine raise the existing unsupported-pattern
                # diagnostic when necessary.  This keeps pcc-authored tools
                # such as the self-backend IR parser off libpython when they
                # assemble regexes from constant fragments.
                pattern_obj = self._emit_as_object(pattern_expr)
                self._gc_pin(pattern_obj)
                result = self.builder.call(
                    self.runtime["py_re_compile_obj"],
                    [pattern_obj, ir.Constant(_I64, flags_value)],
                    name=self._fresh("re.compile.dynamic.obj"),
                )
                # This exact emission returns a NEW Pattern, even when the
                # expression's inferred type is Dyn. Do not rely on an AST
                # classifier that must also admit borrowed fallback paths.
                self._note_owned_object_value(result)
                self._gc_pin(result)
                self._gc_unpin(pattern_obj)
                self._gc_release_if_owned(pattern_obj, pattern_expr)
                self._gc_unpin(result)
                self._emit_post_call_err_check(getattr(expr, "span", None))
                return result
            return None
        if attr.name == "findall":
            return self._emit_native_re_findall_call(expr.args, expr.kwargs)
        if attr.name == "split":
            legacy_split = self._emit_native_re_split_call(expr.args, expr.kwargs)
            if legacy_split is not None:
                return legacy_split
            return self._emit_native_re_engine_split_call(expr.args, expr.kwargs)
        if attr.name == "sub":
            return self._emit_native_re_sub_call(expr.args, expr.kwargs)
        if attr.name not in ("match", "search", "fullmatch"):
            return None
        return self._emit_native_re_value_call(
            "re." + attr.name,
            expr.args,
            expr.kwargs,
        )

    def _emit_native_re_value_call(
        self,
        kind: str,
        args: tuple[Expr, ...],
        kwargs: tuple[tuple[str, Expr], ...],
    ) -> Optional[ir.Value]:
        if kind not in ("re.match", "re.search", "re.fullmatch"):
            return None
        if kwargs or len(args) < 2 or len(args) > 3:
            return None
        if len(args) == 3:
            helper = {
                "re.match": "py_re_match_flags",
                "re.search": "py_re_search_flags",
                "re.fullmatch": "py_re_fullmatch_flags",
            }[kind]
            result = self.builder.call(
                self.runtime[helper],
                [
                    self._emit_as_object(args[0]),
                    self._emit_as_object(args[1]),
                    self._emit_expr_as_i64(args[2]),
                ],
                name=self._fresh(kind),
            )
            # flags==0 routes through the faithful engine, which raises for
            # patterns outside the native subset instead of mismatching.
            self._emit_post_call_err_check(getattr(args[0], "span", None))
            return result
        helper = {
            "re.match": "py_re_match",
            "re.search": "py_re_search",
            "re.fullmatch": "py_re_fullmatch",
        }[kind]
        result = self.builder.call(
            self.runtime[helper],
            [self._emit_as_object(args[0]), self._emit_as_object(args[1])],
            name=self._fresh(kind),
        )
        self._emit_post_call_err_check(getattr(args[0], "span", None))
        return result

    def _emit_native_re_findall_call(
        self,
        args: tuple[Expr, ...],
        kwargs: tuple[tuple[str, Expr], ...],
    ) -> Optional[ir.Value]:
        if kwargs or len(args) < 2 or len(args) > 3:
            return None
        flags = (
            ir.Constant(_I64, 0) if len(args) == 2 else self._emit_expr_as_i64(args[2])
        )
        result = self.builder.call(
            self.runtime["py_re_findall_flags"],
            [
                self._emit_as_object(args[0]),
                self._emit_as_object(args[1]),
                flags,
            ],
            name=self._fresh("re.findall"),
        )
        # flags==0 routes through the faithful engine, which raises for
        # patterns outside the native subset instead of mismatching.
        self._emit_post_call_err_check(getattr(args[0], "span", None))
        return result

    def _emit_native_re_split_call(
        self,
        args: tuple[Expr, ...],
        kwargs: tuple[tuple[str, Expr], ...],
    ) -> Optional[ir.Value]:
        if len(args) < 2 or len(args) > 4:
            return None
        if not self._native_re_literal_split_pattern(args[0]):
            return None

        maxsplit_expr: Expr | None = None
        flags_expr: Expr | None = None
        if len(args) >= 3:
            maxsplit_expr = args[2]
        if len(args) >= 4:
            flags_expr = args[3]
        for key, value in kwargs:
            if key == "maxsplit":
                if maxsplit_expr is not None:
                    return None
                maxsplit_expr = value
            elif key == "flags":
                if flags_expr is not None:
                    return None
                flags_expr = value
            else:
                return None

        if flags_expr is not None:
            if not isinstance(flags_expr, IntLit) or int(flags_expr.value) != 0:
                return None

        if maxsplit_expr is None:
            return self.builder.call(
                self.runtime["py_str_split"],
                [self._emit_as_object(args[1]), self._emit_as_object(args[0])],
                name=self._fresh("re.split.literal"),
            )
        if not isinstance(maxsplit_expr, IntLit):
            return None
        maxsplit_value = int(maxsplit_expr.value)
        if maxsplit_value <= 0:
            return self.builder.call(
                self.runtime["py_str_split"],
                [self._emit_as_object(args[1]), self._emit_as_object(args[0])],
                name=self._fresh("re.split.literal"),
            )
        return self.builder.call(
            self.runtime["py_str_split_maxsplit"],
            [
                self._emit_as_object(args[1]),
                self._emit_as_object(args[0]),
                ir.Constant(_I64, maxsplit_value),
            ],
            name=self._fresh("re.split.literal.maxsplit"),
        )

    def _emit_native_re_engine_split_call(
        self,
        args: tuple[Expr, ...],
        kwargs: tuple[tuple[str, Expr], ...],
    ) -> Optional[ir.Value]:
        if kwargs or len(args) < 2 or len(args) > 3:
            return None
        # py_re_engine_split requires a STRING pattern. A non-literal first arg
        # (e.g. a compiled ``re.compile(...)`` object passed as
        # ``re.split(self.sep, text)``) is not a string at runtime and would
        # raise "split expects string pattern"; fall back to CPython instead.
        if not isinstance(args[0], StrLit):
            return None
        maxsplit = (
            ir.Constant(_I64, 0) if len(args) == 2 else self._emit_expr_as_i64(args[2])
        )
        result = self.builder.call(
            self.runtime["py_re_engine_split"],
            [
                self._emit_as_object(args[0]),
                self._emit_as_object(args[1]),
                maxsplit,
                ir.Constant(_I64, 0),
            ],
            name=self._fresh("re.split.engine"),
        )
        # the engine raises for patterns outside the native subset
        self._emit_post_call_err_check(getattr(args[0], "span", None))
        return result

    def _emit_native_re_sub_call(
        self,
        args: tuple[Expr, ...],
        kwargs: tuple[tuple[str, Expr], ...],
    ) -> Optional[ir.Value]:
        if kwargs or len(args) < 3 or len(args) > 4:
            return None
        roots = []
        values = []
        for index, argument in enumerate(args[:3]):
            value = self._emit_expr_with_cpy_operand_cleanup(
                argument, (), as_object=True, rooted_pcc_lifetimes=tuple(roots),
            )
            owned = self._owned_release_needed(value, argument)
            root = self._enter_container_temp_root(value, self._fresh("re.sub.argument"))
            roots.append((root, owned))
            values.append(value)
        count = ir.Constant(_I64, 0)
        if len(args) == 4:
            count = self._emit_expr_with_cpy_operand_cleanup(
                args[3], (), as_i64=True, rooted_pcc_lifetimes=tuple(roots),
            )
        values = [self.builder.call(self.runtime["pcc_gc_load_ptr"],
            [ir.Constant(_CSTR, None), self._as_gc_ptr(root)]) for root, _owned in roots]
        result = self.builder.call(
            self.runtime["py_re_engine_sub"],
            [values[0], values[1], values[2], count, ir.Constant(_I64, 0)],
            name=self._fresh("re.sub.engine"),
        )
        self._gc_pin(result)
        self._release_rooted_pcc_lifetimes(tuple(roots))
        self._gc_unpin(result)
        # This ABI always returns a new string reference. Record its owner
        # at emission: raw-scaffold code can infer a Dyn result and otherwise
        # lose the owner when assigning or copying the replacement string.
        self._note_owned_object_value(result)
        # the engine raises for patterns outside the native subset or
        # backslash replacement templates
        self._emit_post_call_err_check(getattr(args[0], "span", None))
        return result

    def _emit_native_re_compile_alias_method_call(
        self,
        alias_info: tuple[str, int],
        method_name: str,
        args: tuple[Expr, ...],
        kwargs: tuple[tuple[str, Expr], ...],
    ) -> Optional[ir.Value]:
        if kwargs or method_name not in _RE_ALIAS_METHODS or len(args) != 1:
            return None
        pattern, flags = alias_info
        helper = {
            "match": "py_re_match_flags",
            "search": "py_re_search_flags",
            "findall": "py_re_findall_flags",
        }[method_name]
        result = self.builder.call(
            self.runtime[helper],
            [
                self._emit_str_literal(pattern),
                self._emit_as_object(args[0]),
                ir.Constant(_I64, flags),
            ],
            name=self._fresh(f"re.compile.alias.{method_name}"),
        )
        # flags==0 match/search route through the faithful engine, which can
        # raise for patterns outside the native subset.
        self._emit_post_call_err_check(getattr(args[0], "span", None))
        return result

    def _emit_native_re_compile_method_attr(
        self,
        expr: Attr,
    ) -> Optional[ir.Value]:
        if expr.name not in ("match", "search", "findall"):
            return None
        if isinstance(expr.obj, Name):
            alias_info = self._native_re_compile_alias_for_name(expr.obj.ident)
            if alias_info is not None:
                pattern, flags = alias_info
                method_kind = {"match": 0, "search": 1, "findall": 2}[expr.name]
                return self.builder.call(
                    self.runtime["py_re_compile_method"],
                    [
                        self._emit_str_literal(pattern),
                        ir.Constant(_I64, flags),
                        ir.Constant(_I64, method_kind),
                    ],
                    name=self._fresh(f"re.compile.alias.{expr.name}"),
                )
        call = expr.obj
        if not isinstance(call, Call) or call.kwargs:
            return None
        func = call.func
        if (
            not isinstance(func, Attr)
            or func.name != "compile"
            or not isinstance(func.obj, Name)
            or self._native_builtin_module_for_name(func.obj.ident) != "re"
        ):
            return None
        if len(call.args) < 1 or len(call.args) > 2:
            return None
        flags = (
            ir.Constant(_I64, 0)
            if len(call.args) == 1
            else self._emit_expr_as_i64(call.args[1])
        )
        method_kind = {"match": 0, "search": 1, "findall": 2}[expr.name]
        return self.builder.call(
            self.runtime["py_re_compile_method"],
            [
                self._emit_as_object(call.args[0]),
                flags,
                ir.Constant(_I64, method_kind),
            ],
            name=self._fresh(f"re.compile.{expr.name}"),
        )


__all__ = ["NativeTextModulesLoweringMixin"]
