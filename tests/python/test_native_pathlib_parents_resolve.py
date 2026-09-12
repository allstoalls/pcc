"""``Path.parents`` and ``Path.resolve`` exist and match CPython.

Neither was implemented.  ``pcc/package/inspect.py`` opens with
``_REPO_ROOT = Path(__file__).resolve().parents[2]`` at module scope, so both
missing names became CPython fallbacks in that module's top-level code -- and
module-level code is outside the strict no-libpython stub projection, which
failed the whole self-host compile.

``resolve`` does not follow symlinks: there is no native ``realpath`` yet, so
it is ``absolute()`` plus ``normpath``.  That narrowing is deliberate and is
the same kind already documented on ``is_file``/``is_dir``; the callers in
this tree want an absolute root, not link identity.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


_SOURCE = '''
from pathlib import Path, PurePath


def main() -> None:
    for raw in ["/a/b/c", "a/b/c", "c", ".", "/", ""]:
        parts = []
        for ancestor in PurePath(raw).parents:
            parts.append(str(ancestor))
        print(raw + " -> " + ",".join(parts))
    p = Path("/x/y/z/file.txt")
    print(str(p.parents[2]))
    print(len(p.parents))
    print(str(p.resolve()))
    print(str(Path("/x/y/../z").resolve()))


main()
'''


def test_parents_and_resolve_match_cpython(tmp_path):
    from pcc.py_frontend.pipeline import compile_python

    src = tmp_path / "pathlib_parents.py"
    exe = tmp_path / "pathlib_parents.out"
    src.write_text(_SOURCE, encoding="utf-8")
    compile_python(str(src), str(exe), libpython_mode="off", backend="self")
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=180, check=True
    )
    reference = subprocess.run(
        ["python3", str(src)], text=True, capture_output=True, timeout=180, check=True
    )
    assert run.stdout == reference.stdout
    assert run.stdout.splitlines() == [
        "/a/b/c -> /a/b,/a,/",
        "a/b/c -> a/b,a,.",
        "c -> .",
        ". -> ",
        "/ -> ",
        " -> ",
        "/x",
        # /x/y/z, /x/y, /x, /
        "4",
        "/x/y/z/file.txt",
        "/x/z",
    ]


def test_relative_resolve_is_absolute_against_cwd(tmp_path):
    """``resolve`` on a relative path joins the working directory."""
    from pcc.py_frontend.pipeline import compile_python

    src = tmp_path / "pathlib_relative.py"
    exe = tmp_path / "pathlib_relative.out"
    src.write_text(
        '''
from pathlib import Path


def main() -> None:
    print(str(Path("inner/leaf.txt").resolve()))


main()
''',
        encoding="utf-8",
    )
    compile_python(str(src), str(exe), libpython_mode="off", backend="self")
    workdir = tmp_path / "work"
    workdir.mkdir()
    run = subprocess.run(
        [str(exe)],
        text=True,
        capture_output=True,
        timeout=180,
        check=True,
        cwd=str(workdir),
    )
    reference = subprocess.run(
        ["python3", str(src)],
        text=True,
        capture_output=True,
        timeout=180,
        check=True,
        cwd=str(workdir),
    )
    assert run.stdout == reference.stdout
    assert run.stdout.strip() == str(workdir / "inner" / "leaf.txt")


def test_package_inspect_module_init_needs_no_libpython():
    """The module whose module-scope ``resolve().parents[2]`` forced it."""
    import re as host_re

    from pcc.parse.py_lift import parse_and_lift
    from pcc.py_frontend import type_infer
    from pcc.py_frontend.codegen.layer1 import L1CodeGen

    path = Path(__file__).resolve().parents[2] / "pcc/package/inspect.py"
    typed = type_infer.infer_module(
        parse_and_lift(
            path.read_text(encoding="utf-8"), str(path), "pcc.package.inspect"
        )
    )
    codegen = L1CodeGen(typed, False, "on")
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    codegen._module_source_path = str(path)
    codegen._target_triple = ""
    ir_text = str(codegen.generate(typed))
    top = host_re.search(
        r"(?ms)^define [^\n]*@_pcc_py_module_(?:top|init)_pcc_package_inspect\("
        r".*?\n\}",
        ir_text,
    )
    if top is not None:
        assert host_re.search(r"\bcall [^\n]*@py_cpy_", top.group(0)) is None, (
            top.group(0)[:2000]
        )
    assert os.path.basename(str(path)) == "inspect.py"


def test_resolve_does_not_follow_symlinks(tmp_path):
    """The documented narrowing, pinned so it cannot be mistaken for working.

    ``Path.resolve`` is ``absolute()`` + ``normpath``; there is no native
    ``realpath``.  CPython returns the link target.
    """
    from pcc.py_frontend.pipeline import compile_python

    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target)
    src = tmp_path / "pathlib_symlink.py"
    exe = tmp_path / "pathlib_symlink.out"
    src.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "\n"
        "\n"
        "def main() -> None:\n"
        "    print(str(Path(sys.argv[1]).resolve()))\n"
        "\n"
        "\n"
        "main()\n",
        encoding="utf-8",
    )
    compile_python(str(src), str(exe), libpython_mode="off", backend="self")
    run = subprocess.run(
        [str(exe), str(link)],
        text=True,
        capture_output=True,
        timeout=180,
        check=True,
    )
    reference = subprocess.run(
        ["python3", str(src), str(link)],
        text=True,
        capture_output=True,
        timeout=180,
        check=True,
    )
    # pcc keeps the link path; CPython reports the target.
    assert run.stdout.strip() == str(link)
    assert reference.stdout.strip() == str(target.resolve())
    assert run.stdout != reference.stdout
