from __future__ import annotations

import os
from pathlib import Path

from pcc.frontends.c.evaluator.c_evaluator import CEvaluator
from pcc.driver.project import TranslationUnit


_SOURCE = "int main(void) { return 0; }"


def test_llvmdump_boolean_uses_cache_artifact_dir_not_cwd(
    monkeypatch,
    tmp_path: Path,
):
    caller_dir = tmp_path / "caller"
    cache_dir = tmp_path / "cache"
    caller_dir.mkdir()
    monkeypatch.chdir(caller_dir)

    result = CEvaluator().evaluate(
        _SOURCE,
        optimize=True,
        llvmdump=True,
        cache_dir=str(cache_dir),
        use_compile_cache=False,
    )

    dump_dir = cache_dir / "llvm-dumps" / str(os.getpid())
    assert result == 0
    assert sorted(path.name for path in dump_dir.iterdir()) == [
        "temp.bcode",
        "temp.ir",
        "temp.ooptimize.bcode",
    ]
    assert list(caller_dir.iterdir()) == []


def test_llvmdump_accepts_explicit_artifact_dir(tmp_path: Path):
    dump_dir = tmp_path / "explicit-dumps"

    result = CEvaluator().evaluate(
        _SOURCE,
        optimize=True,
        llvmdump=dump_dir,
        use_compile_cache=False,
    )

    assert result == 0
    assert sorted(path.name for path in dump_dir.iterdir()) == [
        "temp.bcode",
        "temp.ir",
        "temp.ooptimize.bcode",
    ]


def test_translation_unit_dumps_share_the_explicit_artifact_dir(tmp_path: Path):
    dump_dir = tmp_path / "translation-unit-dumps"
    units = [
        TranslationUnit(
            name="helper.c",
            path="",
            source="int helper(void) { return 0; }",
        ),
        TranslationUnit(
            name="main.c",
            path="",
            source="int helper(void); int main(void) { return helper(); }",
        ),
    ]

    result = CEvaluator().evaluate_translation_units(
        units,
        optimize=True,
        llvmdump=dump_dir,
        jobs=1,
        use_compile_cache=False,
    )

    assert result == 0
    assert sorted(path.name for path in dump_dir.iterdir()) == [
        "temp.helper_c.ir",
        "temp.helper_c.opt.ll",
        "temp.main_c.ir",
        "temp.main_c.opt.ll",
    ]
