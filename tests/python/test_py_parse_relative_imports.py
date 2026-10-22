from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pcc.frontends.python import py_ast, py_lex, py_lift, py_parse


def _assert_from_import_matches_python(source: str) -> None:
    expected = ast.parse(source).body[0]
    assert isinstance(expected, ast.ImportFrom)
    names = [(alias.name, alias.asname) for alias in expected.names]

    parsed = py_parse.parse(source, filename="<relative-import>").body[0]
    assert isinstance(parsed, py_parse._ImportFrom)
    assert (parsed.module, parsed.level, parsed.names) == (
        expected.module or "",
        expected.level,
        names,
    )

    lifted = py_lift.parse_and_lift(
        source, "<relative-import>", "pkg.nested.module"
    ).body[0]
    assert isinstance(lifted, py_ast.ImportFrom)
    assert (lifted.module, lifted.level, lifted.names) == (
        expected.module or "",
        expected.level,
        tuple(names),
    )


@pytest.mark.parametrize("level", range(1, 10))
@pytest.mark.parametrize("module", ["", "package.submodule"])
def test_relative_import_depth_matches_python(level, module):
    _assert_from_import_matches_python(
        f"from {'.' * level}{module} import value as alias, other\n"
    )


@pytest.mark.parametrize(
    "source",
    [
        "from . ... import value\n",
        "from ... .package import value\n",
        "from .. ... ..package.submodule import value\n",
        "from ...package import *\n",
        "from ...... import (value as alias,\n                    other,)\n",
        "from ...package.submodule import (value as alias,\n                               other,)\n",
        "from package.submodule import value as alias, other\n",
    ],
)
def test_from_import_token_and_name_forms_match_python(source):
    _assert_from_import_matches_python(source)


def test_ellipsis_token_and_expression_remain_unchanged():
    tokens = py_lex.Lexer("value = ...\n").tokenize()
    assert [(token.kind, token.text) for token in tokens[:3]] == [
        ("NAME", "value"),
        ("OP", "="),
        ("OP", "..."),
    ]
    parsed = py_parse.parse("value = ...\n", filename="<ellipsis>")
    assert isinstance(parsed.body[0].value, py_parse._Name)
    assert parsed.body[0].value.ident == "..."


@pytest.mark.parametrize(
    "source",
    [
        "from ...package..submodule import value\n",
        "from ... import\n",
        "from ... import (value,,)\n",
        "import ...package\n",
    ],
)
def test_malformed_imports_are_still_rejected(source):
    with pytest.raises(SyntaxError):
        ast.parse(source)
    with pytest.raises(py_parse.ParseError):
        py_parse.parse(source, filename="<invalid-import>")
    with pytest.raises(py_lift.LiftError, match="parse failed"):
        py_lift.parse_and_lift(source, "<invalid-import>", "pkg.nested.module")


def test_vendored_meson_gnu_mixin_parses_and_lifts():
    root = Path(__file__).resolve().parents[2]
    source_file = (
        root
        / "projects/numpy-2.4.4/vendored-meson/meson/mesonbuild/compilers/mixins/gnu.py"
    )
    source = source_file.read_text(encoding="utf-8")
    expected = ast.parse(source, filename=str(source_file))
    lifted = py_lift.parse_and_lift(
        source, str(source_file), "mesonbuild.compilers.mixins.gnu"
    )
    assert len(lifted.body) == len(expected.body)
    actual_imports = [
        (stmt.module, stmt.level, stmt.names)
        for stmt in lifted.body
        if isinstance(stmt, py_ast.ImportFrom)
    ]
    expected_imports = [
        (
            stmt.module or "",
            stmt.level,
            tuple((alias.name, alias.asname) for alias in stmt.names),
        )
        for stmt in expected.body
        if isinstance(stmt, ast.ImportFrom)
    ]
    assert actual_imports == expected_imports
    assert ("", 3, (("mesonlib", None),)) in actual_imports
