"""``from package import *`` star import, no-libpython.

``from pkg import *`` fell back to libpython under
``--backend self --python-libpython=off``: the textual import-discovery
(``_append_source_import_from_spec`` in pcc/frontends/python/pipeline.py) dropped any
``*``-only from-import statement entirely (it filtered ``*`` out of the imported
names and then skipped the spec because no names remained), so the package was
never added to the native compile set and the import lowered through
``py_cpy_import``. The native star BINDING already worked
(``_bind_native_cross_module_imports`` binds all public exports of a native
sibling) — the gap was purely discovery. The fix records the module for a
``*``-only import so it is discovered and compiled.

B-P0-PKG gap fix. See
docs/investigations/python-star-import-no-libpython.md.
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


def test_star_import_with_all(tmp_path):
    # __init__ re-exports foo, bar from a submodule and declares __all__
    def build(site):
        pkg = site / "q"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text(
            "from q.mod import foo, bar\n__all__ = ['foo', 'bar']\n", encoding="utf-8"
        )
        (pkg / "mod.py").write_text(
            "def foo():\n    return 9\ndef bar():\n    return 5\n", encoding="utf-8"
        )
    out = _compile_run(
        tmp_path, build,
        "from q import *\n"
        "def main():\n"
        "    print(foo() + bar())\n"
        "main()\n",
    )
    assert out == "14", out


def test_star_import_without_all(tmp_path):
    # No __all__: binds all non-underscore module globals (PI, area)
    def build(site):
        pkg = site / "r"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text(
            "PI = 3\n"
            "def area(rr):\n"
            "    return PI * rr * rr\n"
            "_hidden = 99\n",
            encoding="utf-8",
        )
    out = _compile_run(
        tmp_path, build,
        "from r import *\n"
        "def main():\n"
        "    print(area(2))\n"
        "main()\n",
    )
    assert out == "12", out


def test_star_import_constant(tmp_path):
    # star import binding a re-exported module-global constant
    def build(site):
        pkg = site / "s"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text(
            "from s.consts import MAX\n__all__ = ['MAX']\n", encoding="utf-8"
        )
        (pkg / "consts.py").write_text("MAX = 100\n", encoding="utf-8")
    out = _compile_run(
        tmp_path, build,
        "from s import *\n"
        "def main():\n"
        "    print(MAX)\n"
        "main()\n",
    )
    assert out == "100", out


def test_star_import_respects_all_dunder_names(tmp_path):
    def build(site):
        pkg = site / "t"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text(
            "__description__ = 'native star'\n"
            "__url__ = 'https://example.invalid'\n"
            "try:\n"
            "    __version__ = '1.2.3'\n"
            "except Exception:\n"
            "    __version__ = 'unknown'\n"
            "_hidden = 'no'\n"
            "__all__ = ['__version__', '__description__', '__url__']\n",
            encoding="utf-8",
        )
    out = _compile_run(
        tmp_path, build,
        "from t import *\n"
        "def main():\n"
        "    print(__version__ + ' ' + __description__ + ' ' + __url__)\n"
        "main()\n",
    )
    assert out == "1.2.3 native star https://example.invalid", out


def test_star_import_copies_dynamic_globals_exports(tmp_path, monkeypatch):

    def build(site):
        pkg = site / "u"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "types.py").write_text(
            "values = {'uint8': 7}\n"
            "__all__ = []\n"
            "for key in values:\n"
            "    globals()[key] = values[key]\n"
            "    __all__.append(key)\n",
            encoding="utf-8",
        )

    out = _compile_run(
        tmp_path,
        build,
        "from u.types import *\nprint(uint8)\n",
    )
    assert out == "7", out


def test_star_import_publishes_metadata_decorated_function(tmp_path, monkeypatch):

    def build(site):
        pkg = site / "v"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "decorators.py").write_text(
            "def finalize(fn):\n"
            "    fn.__doc__ = fn.__doc__\n"
            "    return fn\n",
            encoding="utf-8",
        )
        (pkg / "api.py").write_text(
            "from v.decorators import finalize\n"
            "__all__ = ['require']\n"
            "@finalize\n"
            "def require(value):\n"
            "    return value + 1\n",
            encoding="utf-8",
        )

    out = _compile_run(
        tmp_path,
        build,
        "from v.api import *\nprint(require(41))\n",
    )
    assert out == "42", out
