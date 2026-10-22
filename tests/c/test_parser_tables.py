import sys
from pathlib import Path

from pcc.frontends.c.parse.c_parser import (
    CParser,
    _DEFAULT_PLY_LEXTAB,
    _DEFAULT_PLY_YACCTAB,
    _default_ply_cache_dir,
)


def test_cparser_ignores_stale_yacctab_without_tabversion(tmp_path, monkeypatch):
    (tmp_path / "yacctab.py").write_text(
        "# stale parser table from another environment\n"
        "_lr_action = {}\n",
        encoding="utf-8",
    )

    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("yacctab", None)
    try:
        parser = CParser(
            lex_optimize=False,
            yacc_debug=False,
            yacc_optimize=False,
            yacctab="yacctab",
            taboutputdir=str(tmp_path),
        )
        ast = parser.parse("int value;")
        assert ast is not None
    finally:
        sys.modules.pop("yacctab", None)


def test_default_cparser_uses_stable_cache_and_does_not_dirty_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    parser = CParser()
    ast = parser.parse("int value;")

    assert ast is not None
    assert not (tmp_path / "yacctab.py").exists()
    assert not (tmp_path / "lextab.py").exists()
    assert not (tmp_path / "parser.out").exists()

    cache_dir = Path(_default_ply_cache_dir())
    assert (cache_dir / f"{_DEFAULT_PLY_YACCTAB}.py").exists()
    assert (cache_dir / f"{_DEFAULT_PLY_LEXTAB}.py").exists()
