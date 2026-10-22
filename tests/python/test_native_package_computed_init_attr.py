"""Computed module-level bindings in a package __init__, no-libpython.

A package whose ``__init__.py`` binds a module-level name from a COMPUTED
expression (arithmetic, a same-file call, or an imported call) — e.g.
``V = 5 + 3`` or ``V = answer() + 8`` — must bind as a package attribute so
``import pkg; pkg.V`` works, matching CPython. This is a common real-package
shape (computed version strings / configs / registries at import time).

Before the fix the multi-file export classifier
(pcc/frontends/python/pipeline.py) only registered LITERAL module-top assignments
(str/int/bool/None) as ``constant`` exports and statically-typeable containers
as ``module_global`` exports; a computed-RHS assignment (``BinOp``/``Call``)
produced no export entry at all, so cross-package ``pkg.V`` fell through to
``py_obj_getattr`` on the module-name string and raised ``AttributeError``.
The module init code already computes the value and stores it into the
``.modvar.<mod>.<name>`` slot, so registering the binding as a DynType
``module_global`` (mirroring the existing Name/Attr DynType treatment in
``_export_static_literal_type``) routes ``pkg.V`` through the extern
module-global load and resolves correctly.

B-P0-PKG gap fix. See
docs/investigations/python-package-init-computed-module-attr-no-libpython.md.

(A separate, still-distinct failure — a computed binding in a SUBMODULE
accessed via ``from pkg import sub; sub.W`` — COMPILE-FALLBACKs on the
submodule-as-object import and is out of scope here.)
"""
from __future__ import annotations
import os, subprocess


def _compile_run(tmp_path, init_src, *, core_src=None):
    site = tmp_path / "site"
    pkg = site / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(init_src, encoding="utf-8")
    if core_src is not None:
        (pkg / "core.py").write_text(core_src, encoding="utf-8")
    main = tmp_path / "main.py"
    main.write_text(
        "import pkg\n"
        "def main():\n"
        "    print(pkg.V)\n"
        "main()\n",
        encoding="utf-8",
    )
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


def test_computed_arithmetic_init_attr(tmp_path):
    # V = 5 + 3  ->  pkg.V == 8
    assert _compile_run(tmp_path, "V = 5 + 3\n") == "8"


def test_computed_same_file_call_init_attr(tmp_path):
    # def f(): return 42; V = f() + 8  ->  pkg.V == 50
    assert _compile_run(
        tmp_path,
        "def f():\n    return 42\nV = f() + 8\n",
    ) == "50"


def test_computed_imported_call_init_attr(tmp_path):
    # from .core import answer; V = answer() + 8  ->  pkg.V == 50
    assert _compile_run(
        tmp_path,
        "from pkg.core import answer\nV = answer() + 8\n",
        core_src="def answer():\n    return 42\n",
    ) == "50"
