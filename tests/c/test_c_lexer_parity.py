"""Parity test for the native C lexer against PLY (P6C.5 α2).

For each snippet in ``tests/c_parse_oracle/corpus.py``, tokenize with
both the legacy PLY-based ``pcc.frontends.c.lex.c_lexer.CLexer`` and the new
``pcc.frontends.c.parse.c_lex.CLexer``. Token streams must be identical in type
sequence (plus approximate line numbers).

This is the α2 gate. Complements the AST-level diff at
``tests/test_c_parse_driver_parity.py`` by narrowing any divergence
to the lexer layer specifically.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).absolute().parents[2]
sys.path.insert(0, str(REPO))

from pcc.frontends.c.lex.c_lexer import CLexer as PLYCLexer  # noqa: E402
from pcc.frontends.c.parse.c_lex import CLexer as NativeCLexer  # noqa: E402
from tests.c_parse_oracle.corpus import CORPUS  # noqa: E402


def _tokens(cls, src: str) -> list[tuple[str, str]]:
    """Return [(type, value), ...] from a lexer class instance."""
    cl = cls(
        error_func=lambda m, l, c: None,
        on_lbrace_func=lambda: None,
        on_rbrace_func=lambda: None,
        type_lookup_func=lambda n: False,
    )
    cl.build(optimize=False)
    cl.input(src)
    out: list[tuple[str, str]] = []
    while True:
        tok = cl.token()
        if tok is None:
            break
        out.append((tok.type, tok.value))
    return out


@pytest.mark.parametrize("name", sorted(CORPUS))
def test_native_lexer_matches_ply(name: str) -> None:
    src = CORPUS[name]
    ply_stream = _tokens(PLYCLexer, src)
    native_stream = _tokens(NativeCLexer, src)
    if ply_stream != native_stream:
        # Pretty-print the first diverging index.
        diff_idx = None
        for i, (a, b) in enumerate(zip(ply_stream, native_stream)):
            if a != b:
                diff_idx = i
                break
        if diff_idx is None:
            diff_idx = min(len(ply_stream), len(native_stream))
        preview_ply = ply_stream[max(0, diff_idx - 2):diff_idx + 3]
        preview_native = native_stream[max(0, diff_idx - 2):diff_idx + 3]
        pytest.fail(
            f"Lexer diverges at index {diff_idx} for {name!r}\n"
            f"  PLY:    {preview_ply}\n"
            f"  native: {preview_native}"
        )
