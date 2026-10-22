"""Issue 11.C.1: pcc-Python stdlib port registry.

When the recursive walker locates a stdlib module, it should
prefer ``pcc/stdlib/<name>.py`` over CPython's source. This lets
us provide pcc-Python ports of modules whose CPython source has
features pcc can't compile (e.g. ``struct`` with C-accelerated
internals, ``collections.OrderedDict`` with unsupported decorators).
"""
from __future__ import annotations

from pathlib import Path
import sys

import pytest


_REPO_ROOT = Path(__file__).absolute().parents[2]
_STDLIB_DIR = _REPO_ROOT / "pcc" / "stdlib"
_PY_STDLIB_DIR = _REPO_ROOT / "pcc" / "stdlib"


def test_pcc_port_registry_dir_exists():
    """The pcc/stdlib/ port registry exists and has __init__.py so
    Python recognises it as a package."""
    assert _STDLIB_DIR.is_dir(), f"{_STDLIB_DIR} should be a directory"
    init_py = _STDLIB_DIR / "__init__.py"
    assert init_py.is_file(), f"{init_py} should exist"


def test_pcc_stdlib_dir_exists():
    """The primary native stdlib tree is pcc/stdlib."""
    assert _PY_STDLIB_DIR.is_dir(), f"{_PY_STDLIB_DIR} should be a directory"
    init_py = _PY_STDLIB_DIR / "__init__.py"
    assert init_py.is_file(), f"{init_py} should exist"


def test_locator_prefers_pcc_stdlib_when_present():
    """Normal CPython spelling should resolve to pcc/stdlib ports
    before probing the host CPython installation."""
    from pcc.frontends.python.pipeline import _locate_stdlib_module_source

    located = _locate_stdlib_module_source("string")
    assert located is not None
    assert str(_PY_STDLIB_DIR) in located, (
        f"string should resolve via pcc/stdlib/, got: {located}"
    )


def test_locator_supports_dotted_pcc_stdlib_packages():
    """Dotted CPython stdlib names should map to package paths under
    pcc/stdlib, not only flat ``name.py`` files."""
    from pcc.frontends.python.pipeline import _locate_stdlib_module_source

    located = _locate_stdlib_module_source("urllib.parse")
    assert located is not None
    assert located.endswith("pcc/stdlib/urllib/parse.py"), located


def test_locator_finds_py_stdlib_from_stage_binary_ancestor(tmp_path, monkeypatch):
    """Compiled pcc1 has a synthetic ``__file__`` value, so native stdlib
    discovery must also work from the stage binary path."""
    from pcc.frontends.python import pipeline_dependency_closure as pipeline

    repo = tmp_path / "repo"
    py_stdlib = repo / "pcc" / "stdlib"
    py_stdlib.mkdir(parents=True)
    (py_stdlib / "__init__.py").write_text("", encoding="utf-8")
    (py_stdlib / "string.py").write_text("ascii_lowercase = 'abc'\n", encoding="utf-8")
    stage = repo / "build" / "bootstrap-pytest-self"
    stage.mkdir(parents=True)
    pcc1 = stage / "pcc1"
    pcc1.write_text("", encoding="utf-8")

    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setattr(pipeline, "_PCC_DIR", str(tmp_path / "missing_pcc"))
    monkeypatch.setattr(pipeline, "_PIPELINE_DIR", str(tmp_path / "missing_frontend"))
    monkeypatch.setattr(pipeline, "_PY_RUNTIME_DIR", str(tmp_path / "missing_runtime"))
    monkeypatch.setattr(sys, "argv", [str(pcc1), "--backend", "self"])

    located = pipeline._locate_stdlib_module_source("string")
    assert located == str(py_stdlib / "string.py")
    assert pipeline._native_stdlib_root_for_path(located) == str(py_stdlib)


def test_pcc_owned_stdlib_provider_bypasses_fail_soft_probe(monkeypatch):
    from pcc.frontends.python import pipeline_dependency_closure as pipeline

    monkeypatch.setattr(
        pipeline,
        "_native_stdlib_root_for_path",
        lambda _path: "/fake/pcc/stdlib",
    )

    assert pipeline._stdlib_module_compiles("/missing/provider.py", "provider")


def test_locator_accepts_host_stdlib_provider(monkeypatch, tmp_path):
    from pcc.frontends.python import pipeline_dependency_closure as pipeline

    stdlib = tmp_path / "python" / "lib"
    stdlib.mkdir(parents=True)
    provider = stdlib / "hostmod.py"
    provider.write_text("VALUE = 1\n", encoding="utf-8")

    monkeypatch.setattr(pipeline, "_pcc_package_dir_candidates", lambda: [])
    monkeypatch.setattr(
        pipeline,
        "_host_find_spec_origin",
        lambda _mod_name: str(provider),
    )
    monkeypatch.setattr(pipeline, "_host_stdlib_roots", lambda: [str(stdlib)])
    monkeypatch.setattr(pipeline, "_host_site_roots", lambda: [])

    assert pipeline._locate_stdlib_module_source("hostmod") == str(provider)


def test_native_discovery_never_spawns_host_python_probes(monkeypatch):
    from types import SimpleNamespace
    from pcc.frontends.python import pipeline_dependency_closure as closure

    attempts = []

    def refused_probe(*args, **kwargs):
        attempts.append(args)
        raise OSError("host Python unavailable")

    monkeypatch.setattr(closure, "sys", SimpleNamespace(
        implementation=SimpleNamespace(name="pcc"),
    ))
    monkeypatch.setattr(closure.subprocess, "check_output", refused_probe)
    assert closure._host_find_spec_origin("missing_provider") == ""
    assert closure._host_sysconfig_roots(["stdlib", "platstdlib"]) == []
    assert attempts == []


def test_locator_rejects_host_site_packages_provider(monkeypatch, tmp_path):
    from pcc.frontends.python import pipeline_dependency_closure as pipeline

    stdlib = tmp_path / "python" / "lib"
    site = stdlib / "site-packages"
    site.mkdir(parents=True)
    provider = site / "ThirdParty" / "__init__.py"
    provider.parent.mkdir()
    provider.write_text("VALUE = 1\n", encoding="utf-8")

    monkeypatch.setattr(pipeline, "_pcc_package_dir_candidates", lambda: [])
    monkeypatch.setattr(
        pipeline,
        "_host_find_spec_origin",
        lambda _mod_name: str(provider),
    )
    monkeypatch.setattr(pipeline, "_host_stdlib_roots", lambda: [str(stdlib)])
    monkeypatch.setattr(pipeline, "_host_site_roots", lambda: [str(site)])

    assert pipeline._locate_stdlib_module_source("ThirdParty") is None


def test_host_subprocess_source_root_uses_repo_parent(monkeypatch, tmp_path):
    from pcc.frontends.python import pipeline

    repo = tmp_path / "repo"
    pcc_dir = repo / "pcc"
    pcc_dir.mkdir(parents=True)
    monkeypatch.setattr(pipeline, "_PCC_DIR", str(pcc_dir))

    assert pipeline._pcc_source_root_for_host_subprocess() == str(repo)



def test_import_classifier_keeps_cpython_spelling_with_native_provider():
    from pcc.frontends.python.pipeline import _classify_python_import

    assert _classify_python_import("typing") == "compile_time_only"
    assert _classify_python_import(
        "pkg.sibling", native_modules={"pkg.sibling"},
    ) == "native_user_module"
    assert _classify_python_import("os.path") == "builtin_native_dispatch"
    assert _classify_python_import("urllib.parse") == "native_stdlib"
    assert _classify_python_import("colorsys") == "cpython_fallback"


def test_locator_prefers_pcc_port_when_present(tmp_path):
    """When `pcc/stdlib/X.py` exists, the recursive-stdlib locator
    returns its path instead of CPython's `X.py`."""
    from pcc.frontends.python.pipeline import _locate_stdlib_module_source

    # Drop a probe port file
    probe_name = "_pcc_test_probe_module"
    probe = _STDLIB_DIR / f"{probe_name}.py"
    probe.write_text("# pcc-stdlib probe port\n", encoding="utf-8")
    try:
        located = _locate_stdlib_module_source(probe_name)
        assert located is not None, "probe should resolve"
        assert str(_STDLIB_DIR) in located, (
            f"located via pcc/stdlib/, got: {located}"
        )
    finally:
        probe.unlink()


def test_locator_falls_back_to_cpython(tmp_path):
    """When `pcc/stdlib/X.py` is absent, falls back to CPython's
    source path."""
    from pcc.frontends.python.pipeline import _locate_stdlib_module_source

    located = _locate_stdlib_module_source("colorsys")
    assert located is not None
    # colorsys has no pcc/stdlib port, so should resolve to CPython's
    assert "pcc/stdlib/colorsys.py" not in located.replace("\\", "/")


def test_pcc_port_with_recursive_compile(tmp_path):
    """End-to-end: a port placed in pcc/stdlib/ gets pulled into
    the native closure when user code imports the CPython module name.

    The public spelling remains ``import X``; ``pcc/stdlib/X.py`` is
    the implementation location, not a user-facing ``std.X`` namespace.
    """
    from pcc.frontends.python.pipeline import (
        _collect_multi_source_relative_closure,
    )

    probe_name = "_pcc_test_port"
    probe = _STDLIB_DIR / f"{probe_name}.py"
    probe.write_text(
        "def hello() -> int:\n    return 42\n"
    , encoding="utf-8")
    try:
        u = tmp_path / "u.py"
        u.write_text(
            f"import {probe_name}\n"
            f"def f() -> int:\n    return {probe_name}.hello()\n"
        , encoding="utf-8")
        srcs, mods = _collect_multi_source_relative_closure(
            [str(u)], ["u"], recursive_stdlib=True,
        )
        assert probe_name in mods, (
            f"{probe_name} should be pulled from pcc/stdlib/, "
            f"got mods={mods}"
        )
        assert f"std.{probe_name}" not in mods
        assert f"pcc.ir.support.{probe_name}" not in mods
        # Verify the source path used is the pcc/stdlib one
        idx = mods.index(probe_name)
        assert str(_STDLIB_DIR) in srcs[idx], (
            f"used wrong source for {probe_name}: {srcs[idx]}"
        )
    finally:
        probe.unlink()
