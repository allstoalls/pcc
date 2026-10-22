"""Conditional / indented imports in the entry module, no-libpython.

An import that is INDENTED in the entry module — inside a module-level
``try:`` / ``if:`` block, or inside a function (lazy import) — fell back to
libpython under ``--backend self --python-libpython=off``
("imports still lower through CPython fallback"). The entry import-discovery
(``_top_level_import_targets`` in pcc/frontends/python/pipeline.py) was called with
``top_level_only=True``, so it skipped every indented import line; the
referenced module was never added to the native compile set and the import
lowered through ``py_cpy_import``, tripping the no-libpython gate.

The fix scans the ENTRY module's imports including indented ones.
``add_candidate`` only adds names that resolve to a real module file, so a
missing / optional C-extension import inside ``try`` is still left to
``py_cpy_import`` (the absent-optional-dependency case is a separate follow-up,
documented in the investigation). These tests cover the present-dependency
shapes that now compile + run fully native.

B-P0-PKG gap fix. See
docs/investigations/python-conditional-indented-import-no-libpython.md.
"""

from __future__ import annotations
import os, subprocess


def _compile_run(
    tmp_path,
    main_src,
    package_init="",
    runtime_high=None,
    real_src="Z = 42\n",
    extra_sources=None,
):
    site = tmp_path / "site"
    pkg = site / "p"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(package_init, encoding="utf-8")
    (pkg / "real.py").write_text(real_src, encoding="utf-8")
    for relative_path, source in (extra_sources or {}).items():
        path = pkg / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    main = tmp_path / "main.py"
    main.write_text(main_src, encoding="utf-8")
    exe = tmp_path / "bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_PACKAGE_SITE"] = str(site)
    env["PCC_DISABLE_PY_RUN_CACHE"] = "1"
    if runtime_high is not None:
        env["PCC_RUNTIME_HIGH"] = runtime_high
    b = subprocess.run(
        [
            "uv",
            "run",
            "pcc",
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(main),
            "-o",
            str(exe),
        ],
        text=True,
        capture_output=True,
        timeout=420,
        env=env,
    )
    assert b.returncode == 0, b.stdout + b.stderr
    r = subprocess.run([str(exe)], text=True, capture_output=True, timeout=30, env=env)
    assert r.returncode == 0, r.stderr
    return r.stdout.split("\n")[0]


def test_try_import_present_dep(tmp_path):
    # try: from p import real as m  (present dependency -> native)
    out = _compile_run(
        tmp_path,
        "try:\n"
        "    from p import real as m\n"
        "except ImportError:\n"
        "    m = None\n"
        "def main():\n"
        "    print(m.Z)\n"
        "main()\n",
    )
    assert out == "42", out


def test_if_block_import(tmp_path):
    # if True: from p import real as m  (module-level conditional)
    out = _compile_run(
        tmp_path,
        "if True:\n"
        "    from p import real as m\n"
        "def main():\n"
        "    print(m.Z)\n"
        "main()\n",
    )
    assert out == "42", out


def test_function_local_import(tmp_path):
    # def main(): from p import real as m  (lazy / function-level import)
    out = _compile_run(
        tmp_path,
        "def main():\n" "    from p import real as m\n" "    print(m.Z)\n" "main()\n",
    )
    assert out == "42", out


def test_try_import_submodule_then_attr(tmp_path):
    # try: from p import real ; real.Z + 1  (submodule object via try-import)
    out = _compile_run(
        tmp_path,
        "try:\n"
        "    from p import real\n"
        "except ImportError:\n"
        "    real = None\n"
        "def main():\n"
        "    print(real.Z + 1)\n"
        "main()\n",
    )
    assert out == "43", out


def test_module_control_flow_import_in_transitive_package_module(tmp_path):
    out = _compile_run(
        tmp_path,
        "import p\nprint(p.VALUE)\n",
        package_init=("if True:\n" "    from p import real\n" "VALUE = real.Z\n"),
        runtime_high="c",
    )
    assert out == "42", out


def test_conditional_module_builtin_alias_is_exported(tmp_path):
    out = _compile_run(
        tmp_path,
        "from p.compat import binary_type\n"
        "print(binary_type is bytes)\n",
        runtime_high="c",
        extra_sources={
            "compat.py": (
                "import sys\n"
                "if sys.version_info[0] < 3:\n"
                "    binary_type = str\n"
                "else:\n"
                "    binary_type = bytes\n"
            ),
        },
    )
    assert out == "True", out


def test_module_init_factory_imports_owned_stdlib_port(tmp_path):
    out = _compile_run(
        tmp_path,
        "from p.compat import ordered_type\n"
        "print(ordered_type is not None)\n",
        runtime_high="c",
        extra_sources={
            "compat.py": (
                "def choose_ordered_type():\n"
                "    import collections\n"
                "    return collections.OrderedDict\n"
                "ordered_type = choose_ordered_type()\n"
            ),
        },
    )
    assert out == "True", out


def test_module_builtin_range_alias_is_first_class_across_import(tmp_path):
    out = _compile_run(
        tmp_path,
        "from p.compat import saved_range\n"
        "values = saved_range(1, 6, 2)\n"
        "print(values[0] + values[1] + values[2])\n",
        runtime_high="c",
        extra_sources={"compat.py": "saved_range = range\n"},
    )
    assert out == "9", out


def test_package_state_is_visible_when_child_initializes(tmp_path):
    out = _compile_run(
        tmp_path,
        "import p\nprint(p.VALUE)\n",
        package_init=(
            'REGISTRY = {"ready": 42}\n' "from p import real\n" "VALUE = real.SEEN\n"
        ),
        real_src=("import p\n" 'SEEN = p.REGISTRY["ready"]\n'),
        runtime_high="c",
    )
    assert out == "42", out


def test_partial_module_namespace_publishes_ordinary_assignment(tmp_path):
    out = _compile_run(
        tmp_path,
        "from p import real\nprint(real.VALUE)\n",
        runtime_high="c",
        real_src=(
            "VISIBLE = ['ready']\n" "from p import consumer\n" "VALUE = consumer.SEEN\n"
        ),
        extra_sources={
            "consumer.py": (
                "from p import real\n"
                "def read_visible(module):\n"
                "    return module.VISIBLE[0]\n"
                "SEEN = read_visible(real)\n"
            ),
        },
    )
    assert out == "ready", out


def test_package_namespace_publishes_imported_submodule_binding(tmp_path):
    out = _compile_run(
        tmp_path,
        "import p\nprint(hasattr(p, 'real'))\n",
        package_init="from p import real\n",
        runtime_high="c",
    )
    assert out == "True", out


def test_partial_module_namespace_publishes_imported_value_binding(tmp_path):
    out = _compile_run(
        tmp_path,
        "import p\nprint(p.VALUE)\n",
        package_init=(
            "from p.real import exported_convert as convert\n"
            "from p import consumer\n"
            "VALUE = consumer.SEEN\n"
        ),
        runtime_high="c",
        real_src=(
            "def convert(value):\n"
            "    return value + 1\n"
            "globals()['exported_convert'] = convert\n"
        ),
        extra_sources={
            "consumer.py": "import p\nSEEN = p.convert(41)\n",
        },
    )
    assert out == "42", out


def test_partial_module_namespace_publishes_function_definition(tmp_path):
    out = _compile_run(
        tmp_path,
        "import p\nprint(p.VALUE)\n",
        package_init=(
            "def convert(value):\n"
            "    return value + 1\n"
            "from p import consumer\n"
            "VALUE = consumer.SEEN\n"
        ),
        runtime_high="c",
        extra_sources={
            "consumer.py": "import p\nfn = p.convert\nSEEN = fn(41)\n",
        },
    )
    assert out == "42", out


def test_partial_namespace_publishes_used_metadata_decorated_function(tmp_path):
    out = _compile_run(
        tmp_path,
        "import p\nprint(p.VALUE)\n",
        package_init=(
            "from p.decorators import set_module\n"
            "@set_module('p')\n"
            "def convert(value):\n"
            "    return value + 1\n"
            "from p import consumer\n"
            "VALUE = consumer.SEEN\n"
        ),
        runtime_high="c",
        extra_sources={
            "decorators.py": (
                "def set_module(name):\n"
                "    def decorate(fn):\n"
                "        return fn\n"
                "    return decorate\n"
            ),
            "consumer.py": "import p\nfn = p.convert\nSEEN = fn(41)\n",
        },
    )
    assert out == "42", out


def test_cross_module_function_attribute_is_a_first_class_value(tmp_path):
    out = _compile_run(
        tmp_path,
        "from p import real\nfn = real.convert\nprint(fn(41))\n",
        runtime_high="c",
        real_src="def convert(value):\n    return value + 1\n",
    )
    assert out == "42", out


def test_module_scope_import_scanner_excludes_function_and_class_bodies():
    from pcc.frontends.python import pipeline

    source = (
        "if True:\n"
        "    import eager\n"
        "try:\n"
        "    from p import real\n"
        "except ImportError:\n"
        "    pass\n"
        "def lazy(\n"
        "    value,\n"
        "):\n"
        "    import lazy_dep\n"
        "    from p import lazy_real\n"
        "class Holder:\n"
        "    import class_dep\n"
    )

    assert pipeline._iter_source_import_specs(source, top_level_only=True) == ["eager"]
    assert pipeline._iter_source_import_from_specs(source, top_level_only=True) == [
        ("p", ["real"])
    ]
