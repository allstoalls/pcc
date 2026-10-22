"""``from package import submodule`` bound as a module object, no-libpython.

One of the most common real-package import shapes: ``from pkg import sub``
where ``sub`` is a SUBMODULE file (``pkg/sub.py``), not a name defined in
``pkg/__init__.py``. User code then uses ``sub.attr`` / ``sub.func()``.
(Compare ``from os import path``, ``from numpy import linalg``.)

Before the fix this fell back to libpython (PCC-PY-COMPILE-001 under
``--python-libpython=off``): the top-level import-discovery
(``_top_level_import_targets`` in pcc/frontends/python/pipeline.py) only added the
PACKAGE (``pkg``) to the native compile set for a ``from pkg import sub``
statement, never the submodule (``pkg.sub``). So ``pkg.sub`` was absent from
the native export table, the from-import lowering's
``_native_import_from_submodule`` lookup missed it, and the import fell to
``py_cpy_import``. The dotted form ``import pkg.sub`` and the direct-name form
``from pkg.sub import W`` already worked; only the submodule-as-object
from-import was missing. The fix also tries each imported name as a submodule
candidate during discovery (``add_candidate`` only adds names that resolve to a
real module file, so a genuine function/class export of the package is
correctly skipped).

B-P0-PKG gap fix. See
docs/investigations/python-from-package-import-submodule-no-libpython.md.
"""
from __future__ import annotations
import os, subprocess


def _compile_run(tmp_path, build, main_src):
    site = tmp_path / "site"
    build(site)
    main = tmp_path / "main.py"
    main.write_text(main_src, encoding="utf-8")
    exe = tmp_path / "bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    # Strict no-libpython: a CPython fallback would error PCC-PY-COMPILE-001.
    b = subprocess.run(
        ["uv", "run", "pcc", "--backend", "self", "--python-libpython=off",
         "--ir-scaffold=on", str(main), "-o", str(exe)],
        text=True, capture_output=True, timeout=420, env=env,
    )
    assert b.returncode == 0, b.stdout + b.stderr
    r = subprocess.run([str(exe)], text=True, capture_output=True, timeout=30, env=env)
    assert r.returncode == 0, r.stderr
    return r.stdout.split("\n")[0]


def _pkg_with_sub(sub_body):
    def build(site):
        pkg = site / "p"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "sub.py").write_text(sub_body, encoding="utf-8")
    return build


def test_from_pkg_import_submodule_attr(tmp_path):
    # from p import sub; sub.W  (literal module global)
    out = _compile_run(
        tmp_path, _pkg_with_sub("W = 40\n"),
        "from p import sub\n"
        "def main():\n"
        "    print(sub.W)\n"
        "main()\n",
    )
    assert out == "40", out


def test_from_pkg_import_submodule_computed(tmp_path):
    # from p import sub; sub.W  (computed module global — also exercises the
    # computed-module-top-attr export path on a submodule)
    out = _compile_run(
        tmp_path, _pkg_with_sub("W = 10 * 4\n"),
        "from p import sub\n"
        "def main():\n"
        "    print(sub.W)\n"
        "main()\n",
    )
    assert out == "40", out


def test_from_pkg_import_submodule_func(tmp_path):
    # from p import sub; sub.go()
    out = _compile_run(
        tmp_path, _pkg_with_sub("def go():\n    return 7\n"),
        "from p import sub\n"
        "def main():\n"
        "    print(sub.go())\n"
        "main()\n",
    )
    assert out == "7", out


def test_from_pkg_import_multiple_submodules(tmp_path):
    # from p import sa, sb  (multiple submodules in one statement)
    def build(site):
        pkg = site / "p"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "sa.py").write_text("A = 1\n", encoding="utf-8")
        (pkg / "sb.py").write_text("B = 2\n", encoding="utf-8")
    out = _compile_run(
        tmp_path, build,
        "from p import sa, sb\n"
        "def main():\n"
        "    print(sa.A + sb.B)\n"
        "main()\n",
    )
    assert out == "3", out
