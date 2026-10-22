"""Bootstrap-safe textual import discovery helpers."""

from __future__ import annotations

def _source_module_scope_lines(
    source: str,
    *,
    include_class_bodies: bool = False,
) -> list[tuple[str, bool]]:
    """Classify source lines as module-scope, including control-flow suites.

    Package initialization commonly nests imports under a module-level
    ``try``/``if``/``else``.  Leading whitespace alone cannot distinguish
    those eager imports from lazy imports inside a function or class.  This
    small bootstrap-safe indentation scanner masks function/class suites while
    retaining module-level control-flow suites for closure discovery.
    """
    out: list[tuple[str, bool]] = []
    blocked_indent = -1
    blocked_header_complete = False
    blocked_paren_depth = 0
    for raw_line in source.splitlines():
        stripped = raw_line.strip()
        indent = len(raw_line) - len(raw_line.lstrip())
        if blocked_indent >= 0:
            if not blocked_header_complete:
                code = stripped.split("#", 1)[0].rstrip()
                blocked_paren_depth += code.count("(") - code.count(")")
                if blocked_paren_depth <= 0 and code.endswith(":"):
                    blocked_header_complete = True
                out.append((raw_line, False))
                continue
            if not stripped or stripped.startswith("#"):
                out.append((raw_line, False))
                continue
            if indent > blocked_indent:
                out.append((raw_line, False))
                continue
            blocked_indent = -1
            blocked_header_complete = False
            blocked_paren_depth = 0

        code = stripped.split("#", 1)[0].rstrip()
        opens_local_scope = code.startswith("def ") or code.startswith("async def ")
        if not include_class_bodies and code.startswith("class "):
            opens_local_scope = True
        if opens_local_scope:
            blocked_indent = indent
            blocked_paren_depth = code.count("(") - code.count(")")
            blocked_header_complete = blocked_paren_depth <= 0 and code.endswith(":")
            out.append((raw_line, False))
            continue
        out.append((raw_line, True))
    return out


def _iter_source_import_specs(source: str, *, top_level_only: bool) -> list[str]:
    """Return module names from simple ``import mod[, other]`` lines."""
    out: list[str] = []
    lines = (
        _source_module_scope_lines(source)
        if top_level_only
        else [(line, True) for line in source.splitlines()]
    )
    for raw_line, at_module_scope in lines:
        if top_level_only and not at_module_scope:
            continue
        stripped = raw_line.strip()
        if not stripped.startswith("import "):
            continue
        rest = stripped[len("import ") :]
        if "#" in rest:
            rest = rest.split("#", 1)[0].strip()
        for item in rest.split(","):
            item = item.strip()
            if not item:
                continue
            if " as " in item:
                item = item.split(" as ", 1)[0].strip()
            if item:
                out.append(item)
    return out


def _literal_import_call_lines(source, markers, *, top_level_only):
    """Retain raw argument suffixes only for calls outside literals/comments."""
    scope_lines = _source_module_scope_lines(_source_import_discovery_text(source))
    out = []
    continued_quote = ""
    for raw_line, (_masked_line, at_module_scope) in zip(source.splitlines(), scope_lines):
        remaining = raw_line
        calls = []
        if continued_quote:
            found, remaining = _source_after_unescaped_delimiter(remaining, continued_quote)
            if not found:
                out.append((raw_line, False, calls))
                continue
            continued_quote = ""
        while remaining:
            selected = ""
            position = -1
            for candidate in ["#", "'", '\"'] + markers:
                candidate_position = remaining.find(candidate)
                if candidate_position >= 0 and (position < 0 or candidate_position < position):
                    selected = candidate
                    position = candidate_position
            if position < 0 or selected == "#":
                break
            remaining = remaining[position:]
            if selected in markers:
                remaining = remaining[len(selected):]
                if not top_level_only or at_module_scope:
                    calls.append(remaining)
                continue
            delimiter = selected * 3 if remaining.startswith(selected * 3) else selected
            found, remaining = _source_after_unescaped_delimiter(remaining[len(delimiter):], delimiter)
            if not found:
                continued_quote = delimiter
                break
        out.append((raw_line, not top_level_only or at_module_scope, calls))
    if "importlib." in source:
        # Reuse the production parser's replacement-field split. F-string
        # literal text is data; only its actual expressions execute calls.
        from pcc.frontends.python.py_lex import Lexer, TK_STRING
        from pcc.frontends.python.py_parse import Parser
        parser = Parser("", filename="<import-discovery>")
        for token in Lexer(source, filename="<import-discovery>").tokenize():
            if token.kind != TK_STRING or not parser._string_is_f(token.text):
                continue
            if top_level_only and not scope_lines[token.line - 1][1]:
                continue
            for text, is_expression in parser._fstring_source_parts(token.text, token.line):
                if is_expression:
                    expression = parser._split_fstring_expr_parts(text).expr
                    out.extend(_literal_import_call_lines(
                        expression, markers, top_level_only=False,
                    ))
    return out


def _literal_import_name(rest):
    rest = rest.strip()
    quote = rest[:1]
    if quote not in ("'", '\"'):
        return ""
    rest = rest[1:]
    if quote not in rest:
        return ""
    name = rest.split(quote, 1)[0]
    return name if name and not name.startswith(".") else ""


def _iter_source_importlib_literal_specs(
    source: str,
    *,
    top_level_only: bool,
) -> list[str]:
    """Retain canonical literal imports from real source calls only."""
    out: list[str] = []
    for _line, _active, calls in _literal_import_call_lines(
        source, ["importlib.import_module("], top_level_only=top_level_only,
    ):
        for rest in calls:
            name = _literal_import_name(rest)
            if name:
                out.append(name)
    return out


def _iter_source_importlib_resource_literal_specs(
    source: str,
    *,
    top_level_only: bool,
) -> list[str]:
    """Retain canonical resource anchors, including multiline first arguments."""
    api_names = (
        "files", "contents", "is_resource", "open_binary", "open_text",
        "path", "read_binary", "read_text",
    )
    markers = ["importlib.resources." + name + "(" for name in api_names]
    out: list[str] = []
    pending_first_argument = False
    for raw_line, active, calls in _literal_import_call_lines(
        source, markers, top_level_only=top_level_only,
    ):
        if not active:
            continue
        if pending_first_argument:
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            pending_first_argument = False
            name = _literal_import_name(stripped)
            if name:
                out.append(name)
        for rest in calls:
            stripped = rest.strip()
            if not stripped or stripped.startswith("#"):
                pending_first_argument = True
                continue
            name = _literal_import_name(rest)
            if name:
                out.append(name)
    return out


def _append_source_import_from_spec(specs, stmt: str) -> None:
    stmt = stmt.strip()
    if not stmt.startswith("from "):
        return
    rest = stmt[5:]
    split_token = " import "
    split_idx = rest.find(split_token)
    if split_idx < 0:
        return
    module_spec = rest[:split_idx].strip()
    names_spec = rest[split_idx + len(split_token) :].strip()
    if not module_spec:
        return
    if "#" in names_spec:
        names_spec = names_spec.split("#", 1)[0].strip()
    if names_spec.startswith("(") and names_spec.endswith(")"):
        names_spec = names_spec[1:-1].strip()
    imported_names = []
    saw_star = False
    for raw_name in names_spec.split(","):
        raw_name = raw_name.strip()
        if not raw_name:
            continue
        if raw_name == "*":
            # ``from pkg import *`` — record the MODULE so it is discovered and
            # compiled natively (the star binding itself is handled by the
            # AST-based import lowering, which already binds all public exports
            # of a native sibling).  Without recording the spec, a ``*``-only
            # import produced no discovery entry, the module was never compiled,
            # and the import fell through to ``py_cpy_import`` (no-libpython gate
            # tripped).  No imported NAME is recorded, so the submodule-candidate
            # loops below add nothing spurious.  See investigation
            # docs/investigations/python-star-import-no-libpython.md
            saw_star = True
            continue
        if " as " in raw_name:
            raw_name = raw_name.split(" as ", 1)[0].strip()
        imported_names.append(raw_name)
    if imported_names or saw_star:
        specs.append((module_spec, imported_names))


def _iter_source_import_from_specs(
    source: str, *, top_level_only: bool
) -> list[tuple[str, list[str]]]:
    """Return ``[(module_spec, [imported_name...]), ...]`` from source text.

    Keep this intentionally narrow: package-closure discovery only needs
    textual ``from ... import ...`` statements, not full Python AST
    fidelity. Avoiding CPython AST objects here keeps the compiled
    bootstrap path away from fragile runtime attribute walks.
    """
    specs: list[tuple[str, list[str]]] = []
    pending = ""
    pending_active = False
    paren_depth = 0

    lines = (
        _source_module_scope_lines(source)
        if top_level_only
        else [(line, True) for line in source.splitlines()]
    )
    for raw_line, at_module_scope in lines:
        stripped = raw_line.strip()
        if not pending_active:
            if not stripped:
                continue
            if top_level_only and not at_module_scope:
                continue
            if not stripped.startswith("from "):
                continue
            pending = stripped
            pending_active = True
            paren_depth = stripped.count("(") - stripped.count(")")
            if paren_depth <= 0 and not stripped.endswith("\\"):
                _append_source_import_from_spec(specs, pending)
                pending = ""
                pending_active = False
        else:
            if "#" in stripped:
                stripped = stripped.split("#", 1)[0].rstrip()
            pending = pending + " " + stripped
            paren_depth += stripped.count("(") - stripped.count(")")
            if paren_depth <= 0 and not stripped.endswith("\\"):
                _append_source_import_from_spec(specs, pending)
                pending = ""
                pending_active = False

    if pending_active:
        _append_source_import_from_spec(specs, pending)
    return specs


def _without_attribute_error_handler_imports(source: str) -> str:
    """Leave strict package fallback imports for runtime diagnostics.

    Compatibility shims commonly try a modern attribute and import a legacy
    module only from ``except AttributeError``. Pulling that legacy module into
    the closed-world source set makes an unreachable Python-2 fallback part of
    the no-libpython claim. The import statement remains in compiled code; if
    the primary path really is unavailable it raises the normal strict import
    diagnostic instead of silently disappearing.
    """
    out: list[str] = []
    handler_indent = -1
    for raw_line in source.splitlines(keepends=True):
        stripped = raw_line.strip()
        indent = len(raw_line) - len(raw_line.lstrip())
        if handler_indent >= 0 and stripped and indent <= handler_indent:
            handler_indent = -1
        if stripped.startswith("except AttributeError") and stripped.endswith(":"):
            handler_indent = indent
            out.append(raw_line)
            continue
        if (
            handler_indent >= 0
            and indent > handler_indent
            and (stripped.startswith("import ") or stripped.startswith("from "))
        ):
            out.append("\n" if raw_line.endswith("\n") else "")
            continue
        out.append(raw_line)
    return "".join(out)


def _source_after_unescaped_delimiter(
    source: str,
    delimiter: str,
) -> tuple[bool, str]:
    """Return text after the next unescaped delimiter, if present."""
    remaining = source
    while delimiter in remaining:
        before, _separator, after = remaining.partition(delimiter)
        trailing_slashes = len(before) - len(before.rstrip("\\"))
        if trailing_slashes % 2 == 0:
            return True, after
        remaining = after
    return False, ""


def _source_import_discovery_line(
    raw_line: str,
    continued_quote: str,
) -> tuple[str, str]:
    """Mask literals/comments in one line without codepoint indexing."""
    out: list[str] = []
    remaining = raw_line
    if continued_quote:
        found, remaining = _source_after_unescaped_delimiter(
            remaining,
            continued_quote,
        )
        if not found:
            return "", continued_quote
        out.append(" ")
        continued_quote = ""

    while remaining:
        marker = ""
        marker_pos = -1
        for candidate in ("#", "'", '"'):
            candidate_pos = remaining.find(candidate)
            if candidate_pos >= 0 and (marker_pos < 0 or candidate_pos < marker_pos):
                marker = candidate
                marker_pos = candidate_pos
        if marker_pos < 0:
            out.append(remaining)
            break
        out.append(remaining[:marker_pos])
        if marker == "#":
            break

        after_marker = remaining[marker_pos:]
        delimiter = marker
        triple_delimiter = marker + marker + marker
        if after_marker.startswith(triple_delimiter):
            delimiter = triple_delimiter
        after_open = after_marker[len(delimiter) :]
        found, remaining = _source_after_unescaped_delimiter(
            after_open,
            delimiter,
        )
        out.append(" ")
        if not found:
            continued_quote = delimiter
            break
    return "".join(out), continued_quote


# Closure discovery rescans each source about ten times across its passes
# (2,505 scans of 256 files for pcc itself).  Both transforms below are pure
# functions of the text, so memoize them; the bound keeps a long-lived
# process that compiles many programs from growing without limit.
_SCAN_CACHE_MAX_ENTRIES = 4096
_DISCOVERY_TEXT_CACHE: dict = {}
_WITHOUT_TYPE_CHECKING_CACHE: dict = {}
_WITHOUT_IMPLEMENTATION_CACHE: dict = {}


def _false_implementation_guard(line, alias, implementation):
    """Recognize the literal comparison already folded by native codegen.

    More complex expressions deliberately remain in the dependency closure.
    The caller proves the alias with an immediately preceding local import.
    """
    if not line.startswith("if ") or ":" not in line:
        return False
    condition = line[3:].split(":", 1)[0].strip()
    for operator in ("==", "!="):
        parts = condition.split(operator)
        if len(parts) != 2 or parts[0].strip() != alias + ".implementation.name":
            continue
        literal = parts[1].strip()
        if len(literal) < 2 or literal[0] not in ("'", '"') or literal[-1] != literal[0]:
            continue
        value = literal[1:-1]
        # No escapes, embedded delimiters or executable expression suffixes.
        # Unknown spellings stay conservative rather than guessing a value.
        if not value or not value.replace("_", "").isalnum():
            continue
        equal = value == implementation
        return not equal if operator == "==" else equal
    return False


def _without_inactive_implementation_imports(source, *, implementation="pcc"):
    """Mask a proven-false implementation suite for dependency discovery.

    Only a single-line ``import sys [as alias]`` immediately before the guard
    in the same block establishes identity. Global aliases, parameters,
    reassignments, compound guards and uncertain syntax keep all imports.
    This is intentionally narrower than general constant propagation and
    never edits the source consumed by Python or native code generation.
    """
    if ".implementation.name" not in source:
        return source
    key = (source, implementation)
    cached = _WITHOUT_IMPLEMENTATION_CACHE.get(key)
    if cached is not None:
        return cached
    masked_lines = _source_import_discovery_text(source).splitlines()
    out = []
    alias = ""
    import_indent = -1
    guard_indent = -1
    guard_has_pass = False
    bracket_depth = 0
    continued_statement = False
    for raw_line, masked_line in zip(source.splitlines(keepends=True), masked_lines):
        code = masked_line.strip()
        indent = len(raw_line) - len(raw_line.lstrip())
        if "\t" in raw_line[:indent]:
            # Do not infer Python's mixed tab/space block columns here.
            return source
        newline = "\n" if raw_line.endswith("\n") else ""
        if (code and guard_indent >= 0 and indent <= guard_indent
                and bracket_depth == 0 and not continued_statement):
            guard_indent = -1
            guard_has_pass = False
        if guard_indent >= 0:
            if raw_line.strip() and not guard_has_pass:
                out.append(raw_line[:indent] + "pass" + newline)
                guard_has_pass = True
            else:
                out.append(newline)
            bracket_depth += (
                masked_line.count("(") + masked_line.count("[") + masked_line.count("{")
                - masked_line.count(")") - masked_line.count("]") - masked_line.count("}")
            )
            continued_statement = masked_line.rstrip().endswith("\\")
            continue
        if alias and code and indent == import_indent and code.startswith("if "):
            inline_mask = masked_line.split(":", 1)[-1]
            simple_inline = (
                inline_mask.count("(") == inline_mask.count(")")
                and inline_mask.count("[") == inline_mask.count("]")
                and inline_mask.count("{") == inline_mask.count("}")
                and not inline_mask.rstrip().endswith("\\")
            )
            if simple_inline and _false_implementation_guard(raw_line.strip(), alias, implementation):
                header, inline_body = raw_line.split(":", 1)
                if inline_body.split("#", 1)[0].strip():
                    out.append(header + ": pass" + newline)
                else:
                    out.append(raw_line)
                    guard_indent = indent
                    guard_has_pass = False
                alias = ""
                continue
        if code:
            alias = ""
            parts = code.split()
            if parts == ["import", "sys"]:
                alias = "sys"
            elif len(parts) == 4 and parts[:3] == ["import", "sys", "as"]:
                candidate = parts[3]
                if candidate.replace("_", "").isalnum():
                    alias = candidate
            import_indent = indent
        out.append(raw_line)
    result = "".join(out)
    if len(_WITHOUT_IMPLEMENTATION_CACHE) >= _SCAN_CACHE_MAX_ENTRIES:
        _WITHOUT_IMPLEMENTATION_CACHE.clear()
    _WITHOUT_IMPLEMENTATION_CACHE[key] = result
    return result


def _without_inactive_runtime_imports(source):
    return _without_inactive_implementation_imports(
        _without_type_checking_imports(source)
    )


def _source_import_discovery_text(source: str) -> str:
    """Mask strings/comments while preserving source layout.

    Import closure discovery needs lexical import statements, not a typed AST.
    Keeping newlines and indentation lets the caller distinguish module/class
    initialization from deferred function bodies.
    """
    cached = _DISCOVERY_TEXT_CACHE.get(source)
    if cached is not None:
        return cached
    out: list[str] = []
    continued_quote = ""
    for raw_line in source.splitlines():
        masked_line, continued_quote = _source_import_discovery_line(
            raw_line,
            continued_quote,
        )
        out.append(masked_line)
        out.append("\n")
    masked = "".join(out)
    if len(_DISCOVERY_TEXT_CACHE) >= _SCAN_CACHE_MAX_ENTRIES:
        _DISCOVERY_TEXT_CACHE.clear()
    _DISCOVERY_TEXT_CACHE[source] = masked
    return masked


def _without_type_checking_imports(source: str) -> str:
    cached = _WITHOUT_TYPE_CHECKING_CACHE.get(source)
    if cached is not None:
        return cached
    masked = _without_type_checking_imports_uncached(source)
    if len(_WITHOUT_TYPE_CHECKING_CACHE) >= _SCAN_CACHE_MAX_ENTRIES:
        _WITHOUT_TYPE_CHECKING_CACHE.clear()
    _WITHOUT_TYPE_CHECKING_CACHE[source] = masked
    return masked


def _without_type_checking_imports_uncached(source: str) -> str:
    """Mask imports guarded by ``typing.TYPE_CHECKING``.

    The code generator already folds these guards to ``False``.  Dependency
    discovery must apply the same boundary; otherwise type-only imports can
    pull host stdlib/package providers into a runtime closure even though no
    emitted path can execute them.  Preserve line count and block syntax so
    both the lexical and lifted-AST scanners can consume the result.
    """
    typing_aliases: set[str] = set()
    flag_aliases: set[str] = set()
    pending_typing_import = ""
    collecting_typing_import = False

    def record_typing_imports(rest: str) -> None:
        for item in rest.split(","):
            item = item.strip()
            if not item:
                continue
            if " as " in item:
                imported_name, alias = item.split(" as ", 1)
                if imported_name.strip() == "TYPE_CHECKING":
                    flag_aliases.add(alias.strip())
            elif item == "TYPE_CHECKING":
                flag_aliases.add("TYPE_CHECKING")

    # Reuse the import-discovery lexer so strings and comments cannot create
    # false aliases.  Accumulate the ordinary parenthesized import form too;
    # real build tools commonly format long typing imports over several lines.
    alias_source = _source_import_discovery_text(source)
    for raw_line in alias_source.splitlines():
        stripped = raw_line.strip()
        if collecting_typing_import:
            if ")" in stripped:
                before_close = stripped.split(")", 1)[0]
                record_typing_imports(pending_typing_import + before_close)
                pending_typing_import = ""
                collecting_typing_import = False
            else:
                pending_typing_import += stripped
            continue
        if stripped.startswith("import "):
            rest = stripped[len("import ") :]
            for item in rest.split(","):
                item = item.strip()
                if not item:
                    continue
                if " as " in item:
                    module_name, alias = item.split(" as ", 1)
                    if module_name.strip() == "typing":
                        typing_aliases.add(alias.strip())
                elif item == "typing":
                    typing_aliases.add("typing")
        elif stripped.startswith("from typing import "):
            rest = stripped[len("from typing import ") :].strip()
            if rest.startswith("("):
                rest = rest[1:]
                if ")" in rest:
                    record_typing_imports(rest.split(")", 1)[0])
                else:
                    pending_typing_import = rest
                    collecting_typing_import = True
            else:
                record_typing_imports(rest)

    out: list[str] = []
    guard_indent = -1
    guard_has_pass = False
    for raw_line in source.splitlines(keepends=True):
        stripped = raw_line.strip()
        indent = len(raw_line) - len(raw_line.lstrip())
        if stripped and guard_indent >= 0 and indent <= guard_indent:
            guard_indent = -1
            guard_has_pass = False

        if guard_indent >= 0:
            newline = "\n" if raw_line.endswith("\n") else ""
            if stripped and not guard_has_pass:
                out.append(raw_line[:indent] + "pass" + newline)
                guard_has_pass = True
            else:
                out.append(newline)
            continue

        matched_guard = False
        if stripped.startswith("if ") and ":" in stripped:
            condition, inline_body = stripped[3:].split(":", 1)
            condition = condition.strip()
            while (
                len(condition) >= 2
                and condition.startswith("(")
                and condition.endswith(")")
            ):
                condition = condition[1:-1].strip()
            if condition in flag_aliases:
                matched_guard = True
            else:
                for alias in typing_aliases:
                    if condition == alias + ".TYPE_CHECKING":
                        matched_guard = True
                        break
            if matched_guard:
                if inline_body.strip():
                    newline = "\n" if raw_line.endswith("\n") else ""
                    out.append(raw_line[:indent] + "if " + condition + ": pass" + newline)
                else:
                    out.append(raw_line)
                    guard_indent = indent
                    guard_has_pass = False
                continue

        out.append(raw_line)
    return "".join(out)
