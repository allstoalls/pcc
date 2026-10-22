"""A program finds its project's package without any configuration.

``pcc app.py`` resolves imports from the entry file's directory.  Projects keep
their package at the repository root and their programs in a subdirectory
(``examples/probe/app.py`` importing ``mypkg``), so resolution walks up from the
entry directory and stops at the first project-root marker.  The walk must stop
there: a package above the project root belongs to somebody else and must not
enter the program's closure.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

from pcc.frontends.python.pipeline import compile_python
from pcc.frontends.python.pipeline_packages import project_root_search_dirs


def _project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "mypkg").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    (root / "mypkg" / "__init__.py").write_text(
        "from . import core  # noqa: F401\n", encoding="utf-8"
    )
    (root / "mypkg" / "core.py").write_text(
        textwrap.dedent(
            """
            def greet(name: str) -> str:
                return "hello " + name
            """
        ).lstrip(),
        encoding="utf-8",
    )
    (root / "examples" / "probe").mkdir(parents=True)
    return root


def test_program_in_a_subdirectory_imports_its_project_package(tmp_path: Path) -> None:
    root = _project(tmp_path)
    app = root / "examples" / "probe" / "app.py"
    app.write_text(
        textwrap.dedent(
            """
            from mypkg.core import greet

            print(greet("pcc"))
            """
        ).lstrip(),
        encoding="utf-8",
    )
    exe = root / "examples" / "probe" / "app"
    environment = dict(os.environ)
    environment.pop("PCC_PACKAGE_SITE", None)
    saved = os.environ.pop("PCC_PACKAGE_SITE", None)
    try:
        compile_python(
            str(app), str(exe), libpython_mode="off", ir_scaffold_mode="on", backend="self"
        )
    finally:
        if saved is not None:
            os.environ["PCC_PACKAGE_SITE"] = saved
    done = subprocess.run([str(exe)], capture_output=True, text=True, timeout=180)
    assert done.returncode == 0, done.stderr
    assert done.stdout == "hello pcc\n"


def test_the_walk_stops_at_the_project_root(tmp_path: Path) -> None:
    root = _project(tmp_path)
    # A package that lives ABOVE the project root must not be reachable.
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "__init__.py").write_text("", encoding="utf-8")
    dirs = project_root_search_dirs(str(root / "examples" / "probe"))
    assert dirs[0] == str(root / "examples" / "probe")
    assert str(root) in dirs
    assert str(tmp_path) not in dirs


def test_a_marker_less_tree_walks_a_bounded_number_of_levels(tmp_path: Path) -> None:
    deep = tmp_path
    for name in ("a", "b", "c"):
        deep = deep / name
        deep.mkdir()
    dirs = project_root_search_dirs(str(deep))
    assert dirs[0] == str(deep)
    assert len(dirs) <= 24
    assert project_root_search_dirs("") == []
