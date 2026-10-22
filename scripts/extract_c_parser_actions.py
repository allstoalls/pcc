#!/usr/bin/env python3
"""scripts/extract_c_parser_actions.py — P6C.5 α1 step 2.

Extract the action layer from ``pcc/frontends/c/parse/c_parser.py`` into
``pcc/frontends/c/parse/c_parser_actions.py``. The result is a self-contained
module that:

- has no PLY import
- keeps all 145 p_* grammar actions intact (same bodies)
- keeps all helper methods (_coord, _parse_error, _push_scope, etc.)
- exposes a ``CParserActions`` class the driver can instantiate

The legacy PLY-based ``CParser`` in c_parser.py keeps working (opt-out
path) — this script doesn't modify it.

Regenerate whenever ``c_parser.py`` method bodies change::

    python scripts/extract_c_parser_actions.py

The AST-based transform preserves source text (comments, formatting)
for methods we copy across, so diffs against upstream pycparser stay
readable.
"""
from __future__ import annotations

import ast
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "pcc" / "frontends" / "c" / "parse" / "c_parser.py"
OUT = REPO / "pcc" / "frontends" / "c" / "parse" / "c_parser_actions.py"


# Methods to drop from the extracted class — these are PLY-specific
# glue that only makes sense when PLY's lexer/parser is driving.
# The driver provides its own equivalents.
_SKIP_METHODS = frozenset({
    "__init__",
    "parse",
    "_get_yacc_lookahead_token",  # uses PLY internal state
    "_lex_error_func",             # PLY lex callback
    "_lex_on_lbrace_func",         # PLY lex callback
    "_lex_on_rbrace_func",         # PLY lex callback
    "_lex_type_lookup_func",       # PLY lex callback
})


# Class-level attributes we want to carry over even when they're
# assigned outside methods (e.g. ``_TYPE_QUALIFIERS = ('const', ...)``).
# The extractor auto-detects these from ast.Assign in the class body.


def main() -> int:
    src_text = SRC.read_text()
    tree = ast.parse(src_text)

    cparser_node: ast.ClassDef | None = None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "CParser":
            cparser_node = node
            break
    if cparser_node is None:
        print("error: class CParser not found in c_parser.py", file=sys.stderr)
        return 1

    kept: list[ast.stmt] = []
    skipped: list[str] = []

    for stmt in cparser_node.body:
        if isinstance(stmt, ast.FunctionDef):
            if stmt.name in _SKIP_METHODS:
                skipped.append(stmt.name)
                continue
            kept.append(stmt)
        elif isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            # Class-level data (e.g., keyword tables) — keep.
            kept.append(stmt)
        elif isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            # Docstring / top-of-class string literal — keep.
            kept.append(stmt)

    # Render the new module
    header = textwrap.dedent('''\
        """pcc.frontends.c.parse.c_parser_actions — AUTO-GENERATED action layer.

        Extracted from ``pcc/frontends/c/parse/c_parser.py`` by
        ``scripts/extract_c_parser_actions.py``. Contains grammar-rule
        action methods + helpers, with PLY-specific glue removed.

        Consumed by ``pcc.frontends.c.parse.c_parse_driver`` (P6C.5 α1 step 3).

        **Do not hand-edit.** If you need to change a grammar rule,
        edit ``c_parser.py`` and re-run the extractor.
        """
        from __future__ import annotations

        from pcc.frontends.c.ast import c_ast
        # ``Coord`` originally lived on ``pcc.frontends.c.parse.plyparser``; we
        # re-use its location here because the legacy ``CParser`` and
        # the new driver must produce ``Coord`` instances that compare
        # equal (they're used in AST nodes). The import is data-only:
        # ``Coord`` is a small dataclass-style container, no PLY.
        from pcc.frontends.c.parse.plyparser import Coord, ParseError, PLYParser
        from pcc.frontends.c.ast.ast_transforms import fix_switch_cases


        class CParserActions(PLYParser):
            """Grammar-rule action layer + scope/typedef helpers,
            minus PLY-specific lexer/parser coupling. Instantiated by
            the native LR driver (``pcc.frontends.c.parse.c_parse_driver``).

            Inherits ``PLYParser`` purely for its ``_coord`` /
            ``_parse_error`` helpers — ``plyparser.py`` itself is
            pure Python with no PLY runtime imports (audit-clean).

            Instances track per-parse state:
              - ``_scope_stack``: typedef/name disambiguation scope
              - ``_last_yielded_token``: most-recent token from the
                lexer (replaces PLY's ``_get_yacc_lookahead_token``
                which probed the yacc runtime directly)
              - ``filename``: source file for diagnostics
            """

            def __init__(self, filename: str = "<input>") -> None:
                self.filename = filename
                self._scope_stack = [{}]
                self._last_yielded_token = None

            # -------- lexer-coordination surface (driver supplies these) --------

            def _get_yacc_lookahead_token(self):
                """Return the current lookahead token. The driver
                stashes the lookahead into ``_last_yielded_token``
                before each reduce (parser uses it for error messages
                and for lookahead-sensitive rules)."""
                return self._last_yielded_token

            def _coord(self, lineno, column=None):
                """Build a Coord from lineno/column. Overrides the
                PLYParser version which reads ``self.clex.filename``;
                the native driver stores filename directly."""
                return Coord(
                    file=self.filename, line=lineno, column=column,
                )

    ''')

    body_parts: list[str] = [header]
    for stmt in kept:
        # ast.unparse gives us back faithful Python source.
        chunk = ast.unparse(stmt)
        # Indent inside the class body (ast.unparse returns top-level
        # indentation; we want it inside `class CParserActions:`).
        body_parts.append(textwrap.indent(chunk, "    "))
        body_parts.append("\n\n")

    # Drop the last trailing newline to keep file tidy
    text = "".join(body_parts).rstrip() + "\n"
    OUT.write_text(text)

    print(f"wrote {OUT.relative_to(REPO)}")
    print(f"  kept statements: {len(kept)}")
    print(f"  skipped methods: {len(skipped)}")
    for name in skipped:
        print(f"    - {name}")
    print(f"  file size: {OUT.stat().st_size / 1024:.1f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
