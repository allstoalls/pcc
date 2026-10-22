"""Deep dotted package attribute access ``a.b.c.X``, no-libpython.

``import a.b.c; a.b.c.X`` (a 3+-level dotted package access) compiled fine but
RAN with ``AttributeError`` on the intermediate ``a.b`` under
``--backend self --python-libpython=off``. The native module export resolution
(``_native_module_expr_export_info`` in pcc/frontends/python/codegen/native_modules.py)
only handled a one-level module expression (``a.b.X`` — ``Attr`` whose ``.obj``
is a ``Name``); a deeper chain (``a.b.c`` — ``Attr`` whose ``.obj`` is itself an
``Attr``) fell through to the runtime ``py_obj_getattr`` chain, which has no
``a.b`` attribute on the package module object. The fix flattens the pure
Name/Attr chain to its spelled dotted name and looks it up directly in the
native export table, gated on the root being a known native module alias (so a
real object attribute chain ``obj.x.y.z`` is still left to ``py_obj_getattr``).

B-P0-PKG gap fix. See
docs/investigations/python-deep-dotted-package-attr-no-libpython.md.
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
    b = subprocess.run(
        ["uv", "run", "pcc", "--backend", "self", "--python-libpython=off",
         "--ir-scaffold=on", str(main), "-o", str(exe)],
        text=True, capture_output=True, timeout=420, env=env,
    )
    assert b.returncode == 0, b.stdout + b.stderr
    r = subprocess.run([str(exe)], text=True, capture_output=True, timeout=30, env=env)
    assert r.returncode == 0, r.stderr
    return r.stdout.split("\n")[0]


def _mk_pkgs(site, parts, leaf_init_body):
    """Create nested empty packages site/parts[0]/.../parts[-1] with the last
    __init__.py holding leaf_init_body."""
    cur = site
    for i, p in enumerate(parts):
        cur = cur / p
        cur.mkdir(parents=True, exist_ok=True)
        body = leaf_init_body if i == len(parts) - 1 else ""
        (cur / "__init__.py").write_text(body, encoding="utf-8")


def test_three_level_dotted_constant(tmp_path):
    # import a.b.c ; a.b.c.X  (X in a/b/c/__init__.py)
    out = _compile_run(
        tmp_path,
        lambda site: _mk_pkgs(site, ["a", "b", "c"], "X = 13\n"),
        "import a.b.c\n"
        "def main():\n"
        "    print(a.b.c.X)\n"
        "main()\n",
    )
    assert out == "13", out


def test_three_level_dotted_function(tmp_path):
    # import a.b.c ; a.b.c.fn()
    out = _compile_run(
        tmp_path,
        lambda site: _mk_pkgs(site, ["a", "b", "c"], "def fn():\n    return 77\n"),
        "import a.b.c\n"
        "def main():\n"
        "    print(a.b.c.fn())\n"
        "main()\n",
    )
    assert out == "77", out


def test_four_level_dotted_submodule(tmp_path):
    # import a.b.c.leaf ; a.b.c.leaf.X  (leaf is a submodule file, not a pkg)
    def build(site):
        _mk_pkgs(site, ["a", "b", "c"], "")
        (site / "a" / "b" / "c" / "leaf.py").write_text("X = 99\n", encoding="utf-8")
    out = _compile_run(
        tmp_path, build,
        "import a.b.c.leaf\n"
        "def main():\n"
        "    print(a.b.c.leaf.X)\n"
        "main()\n",
    )
    assert out == "99", out


def test_real_object_attr_chain_not_misresolved(tmp_path):
    # Regression: a genuine object attribute chain ``o.n.v`` must NOT be
    # mistaken for dotted module access (the alias-root gate guards this).
    def build(site):
        pkg = site / "p"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "m.py").write_text(
            "class N:\n"
            "    def __init__(self):\n"
            "        self.v = 5\n"
            "class M:\n"
            "    def __init__(self):\n"
            "        self.n = N()\n",
            encoding="utf-8",
        )
    out = _compile_run(
        tmp_path, build,
        "from p.m import M\n"
        "def main():\n"
        "    o = M()\n"
        "    print(o.n.v)\n"
        "main()\n",
    )
    assert out == "5", out
