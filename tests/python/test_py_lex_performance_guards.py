from __future__ import annotations

import pytest

from pcc.frontends.python import py_lex


def test_lexer_slice_does_not_poll_environment_after_init(monkeypatch):
    lexer = py_lex.Lexer("name = 1\n", filename="probe.py")

    class RaisingEnv:
        def get(self, key, default=None):  # pragma: no cover - failure path
            raise AssertionError("hot lexer path polled os.environ")

    original_env = py_lex.os.environ
    py_lex.os.environ = RaisingEnv()
    try:
        assert lexer._slice(0, 4) == "name"
    finally:
        py_lex.os.environ = original_env


def test_lexer_debug_slice_guard_still_checks_bounds(monkeypatch):
    monkeypatch.setenv("PCC_DEBUG_BOOTSTRAP", "1")
    lexer = py_lex.Lexer("abc", filename="probe.py")

    with pytest.raises(RuntimeError, match="bounds out of range"):
        lexer._slice(10, 11)


def test_lexer_operator_fast_path_keeps_existing_tokens():
    source = (
        "a **= b\n"
        "c //= d\n"
        "e >>= f\n"
        "g <<= h\n"
        "i ... j\n"
        "k -> l\n"
        "m := n\n"
        "o != p\n"
        "q @ r\n"
    )

    tokens = py_lex.Lexer(source, filename="ops.py").tokenize()
    ops = [tok.text for tok in tokens if tok.kind == py_lex.TK_OP]

    assert ops == ["**=", "//=", ">>=", "<<=", "...", "->", ":=", "!=", "@"]


def test_triple_quoted_string_allows_escaped_triple_quote():
    source = 'value = """before \\"""" + name + """\\" after"""\n'

    tokens = py_lex.Lexer(source, filename="triple.py").tokenize()
    strings = [tok.text for tok in tokens if tok.kind == py_lex.TK_STRING]

    assert strings == ['"""before \\""""', '"""\\" after"""']
