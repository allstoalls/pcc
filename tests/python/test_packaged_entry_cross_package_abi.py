"""Packaged entrypoints retain locally resolved dependency ABI providers."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess

import pytest

from pcc.frontends.python.pipeline_dependency_closure import (
    _collect_multi_source_relative_closure,
    _collect_relative_module_closure,
)


def _project(tmp_path):
    app = tmp_path / "app"
    toolkit = tmp_path / "toolkit"
    app.mkdir()
    toolkit.mkdir()
    (app / "__init__.py").write_text("")
    entry = app / "__main__.py"
    entry.write_text("from app.worker import main\nmain()\n")
    (app / "worker.py").write_text(
        "import toolkit\n"
        "import app.helper as helper\n"
        "from pcc.extern import c_int64, extern\n"
        "answer = extern('packaged_provider_answer', (c_int64,), c_int64)\n"
        "def main():\n"
        "    print(answer(helper.seed()))\n"
        "def unused():\n"
        "    import optional_dependency\n"
    )
    (app / "helper.py").write_text("def seed():\n    return 37\n")
    (toolkit / "__init__.py").write_text("from . import kernel\n")
    (toolkit / "kernel.py").write_text(
        "from pcc.extern import c_abi_typed_export\n"
        "@c_abi_typed_export('packaged_provider_answer', 'i64', ('i64',))\n"
        "def answer(value: int) -> int:\n"
        "    return value + 5\n"
    )
    (tmp_path / "optional_dependency.py").write_text(
        "raise AssertionError('lazy optional dependency must stay outside eager closure')\n"
    )
    return entry


def _assert_provider_closure(sources, modules):
    assert len(sources) == len(modules)
    assert len(modules) == len(set(modules))
    for name in ("app.helper", "toolkit", "toolkit.kernel"):
        assert name in modules, (name, modules)
    assert "optional_dependency" not in modules


def test_packaged_entry_closes_eager_cross_package_imports(tmp_path, monkeypatch):
    entry = _project(tmp_path)
    monkeypatch.setenv("PCC_PACKAGE_SITE", str(tmp_path))
    sources, modules = _collect_relative_module_closure(
        str(entry), include_same_package_absolute=True,
        recurse_same_package_absolute=True,
    )
    _assert_provider_closure(sources, modules)


def test_explicit_package_closure_retains_package_initializer_children(tmp_path, monkeypatch):
    _project(tmp_path)
    monkeypatch.setenv("PCC_PACKAGE_SITE", str(tmp_path))
    sources, modules = _collect_multi_source_relative_closure(
        [str(tmp_path / "toolkit" / "__init__.py")], ["toolkit"],
    )
    assert "toolkit.kernel" in modules, modules
    assert len(sources) == len(modules)


def test_packaged_entry_abi_provider_executes_all_collectors(
    tmp_path, monkeypatch, pcc_runtime_archive,
):
    from pcc.frontends.python.pipeline import compile_python

    entry = _project(tmp_path)
    monkeypatch.setenv("PCC_PACKAGE_SITE", str(tmp_path))
    monkeypatch.setenv("PCC_SELF_OBJ", "pcc")
    monkeypatch.setenv("PCC_SELF_LINK", "pcc")
    output = tmp_path / "application"
    compile_python(
        str(entry), str(output), backend="self", libpython_mode="off",
        ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive),
    )
    executions = []
    for collector in range(5):
        environment = dict(os.environ, PATH="/nonexistent", PCC_GC_BACKEND=str(collector),
                           PCC_HOST_PYTHON="/usr/bin/false", PCC_RUNTIME_CC="/usr/bin/false")
        result = subprocess.run(
            [str(output)], env=environment, capture_output=True, text=True, timeout=20,
        )
        assert (result.returncode, result.stdout, result.stderr) == (0, "42\n", ""), (
            collector, result.returncode, result.stdout, result.stderr,
        )
        executions.append({"collector": collector, "status": result.returncode,
                           "stdout": result.stdout, "stderr": result.stderr})
    (tmp_path / "native-receipt.json").write_text(json.dumps({
        "schema": "pcc.cross-package-abi-native.v1", "backend": "self",
        "libpython": "off", "artifact": str(output),
        "artifact_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "runtime_archive": str(pcc_runtime_archive),
        "runtime_sha256": hashlib.sha256(pcc_runtime_archive.read_bytes()).hexdigest(),
        "executions": executions,
    }, indent=2) + "\n")


@pytest.mark.parametrize("route", ["single", "multi"])
def test_cross_package_closure_masks_nonruntime_source(tmp_path, monkeypatch, route):
    entry = _project(tmp_path)
    monkeypatch.setenv("PCC_PACKAGE_SITE", str(tmp_path))
    toolkit = tmp_path / "toolkit"
    initializer = toolkit / "__init__.py"
    initializer.write_text(
        initializer.read_text()
        + "def deferred():\n    from . import optional\n"
        + "try:\n    current = 1\n"
        + "except AttributeError:\n    from toolkit.legacy import Legacy\n"
    )
    kernel = toolkit / "kernel.py"
    kernel.write_text(
        '\"\"\"\nimport doc_only\nfrom toolkit.phantom import Phantom\n'
        "importlib.import_module('literal_phantom')\n"
        "importlib.resources.files('resource_phantom')\n"
        '\"\"\"\n' + kernel.read_text()
    )
    kernel.write_text(
        kernel.read_text()
        + 'note = "importlib.import_module(\'literal_phantom\')"\n'
        + 'note2 = "importlib.resources.files(\'resource_phantom\')"\n'
        + "importlib.import_module('literal_live')\n"
        + "importlib.resources.files('resource_live')\n"
        + 'formatted = f"{importlib.import_module(\'fstring_live\').value}"\n'
        + 'formatted_resource = f"{importlib.resources.files(\'fstring_resource\')}"\n'
        + 'literal = f"importlib.import_module(\'literal_phantom\') {{42}}"\n'
    )
    (tmp_path / "literal_live.py").write_text("value = 42\n")
    (tmp_path / "resource_live").mkdir()
    (tmp_path / "resource_live" / "__init__.py").write_text("value = 42\n")
    (tmp_path / "fstring_live.py").write_text("value = 42\n")
    (tmp_path / "fstring_resource").mkdir()
    (tmp_path / "fstring_resource" / "__init__.py").write_text("value = 42\n")
    for filename in ("optional", "legacy", "phantom"):
        (toolkit / (filename + ".py")).write_text("raise AssertionError('not runtime')\n")
    for filename in ("doc_only", "literal_phantom", "resource_phantom"):
        (tmp_path / (filename + ".py")).write_text("raise AssertionError('not runtime')\n")
    if route == "single":
        sources, modules = _collect_relative_module_closure(
            str(entry), include_same_package_absolute=True,
            recurse_same_package_absolute=True,
        )
    else:
        sources, modules = _collect_multi_source_relative_closure(
            [str(entry)], ["app.__main__"],
        )
    _assert_provider_closure(sources, modules)
    assert "literal_live" in modules
    assert "resource_live" in modules
    assert "fstring_live" in modules
    assert "fstring_resource" in modules
    assert not set(modules).intersection({
        "toolkit.optional", "toolkit.legacy", "toolkit.phantom", "doc_only",
        "literal_phantom", "resource_phantom",
    }), modules


@pytest.mark.parametrize("route", ["single", "multi"])
def test_cross_package_absolute_import_keeps_entry_root_precedence(
    tmp_path, monkeypatch, route,
):
    root = tmp_path / "entry_root"
    site = tmp_path / "package_site"
    (root / "app").mkdir(parents=True)
    (site / "toolkit").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='app'\n")
    (root / "app" / "__init__.py").write_text("")
    entry = root / "app" / "__main__.py"
    entry.write_text("from app import worker\n")
    (root / "app" / "worker.py").write_text("import toolkit\n")
    toolkit = site / "toolkit" / "__init__.py"
    toolkit.write_text("import provider\n")
    (root / "provider.py").write_text("origin = 'entry'\n")
    (site / "provider.py").write_text("origin = 'site'\n")
    monkeypatch.setenv("PCC_PACKAGE_SITE", str(site))
    if route == "single":
        sources, modules = _collect_relative_module_closure(
            str(entry), include_same_package_absolute=True,
            recurse_same_package_absolute=True,
        )
    else:
        sources, modules = _collect_multi_source_relative_closure(
            [str(entry)], ["app.__main__"],
        )
    assert "toolkit" in modules
    assert dict(zip(modules, sources))["provider"] == str(root / "provider.py")


def test_fstring_literal_import_and_resource_calls_execute_all_collectors(
    tmp_path, monkeypatch, pcc_runtime_archive,
):
    from pcc.frontends.python.pipeline import compile_python

    app = tmp_path / "app"
    assets = tmp_path / "formatted_assets"
    app.mkdir()
    assets.mkdir()
    (app / "__init__.py").write_text("")
    (tmp_path / "formatted_provider.py").write_text("VALUE = 42\n")
    (tmp_path / "phantom.py").write_text("raise AssertionError('literal text is data')\n")
    (assets / "__init__.py").write_text("VALUE = 7\n")
    (assets / "proof.txt").write_text("owned resource\n")
    entry = app / "__main__.py"
    entry.write_text(
        "import importlib\nimport importlib.resources\n"
        "def main():\n"
        '    print(f"{importlib.import_module(\'formatted_provider\').VALUE}")\n'
        '    print(f"{importlib.resources.is_resource(\'formatted_assets\', \'proof.txt\')}")\n'
        '    print(f"importlib.import_module(\'phantom\') {42}")\n'
        "main()\n"
    )
    monkeypatch.setenv("PCC_PACKAGE_SITE", str(tmp_path))
    monkeypatch.setenv("PCC_SELF_OBJ", "pcc")
    monkeypatch.setenv("PCC_SELF_LINK", "pcc")
    output = tmp_path / "formatted_application"
    compile_python(
        str(entry), str(output), backend="self", libpython_mode="off",
        ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive),
    )
    executions = []
    for collector in range(5):
        environment = dict(os.environ, PATH="/nonexistent", PCC_GC_BACKEND=str(collector),
                           PCC_HOST_PYTHON="/usr/bin/false", PCC_RUNTIME_CC="/usr/bin/false")
        result = subprocess.run(
            [str(output)], env=environment, capture_output=True, text=True, timeout=20,
        )
        assert (result.returncode, result.stdout, result.stderr) == (
            0, "42\nTrue\nimportlib.import_module('phantom') 42\n", "",
        ), (collector, result.returncode, result.stdout, result.stderr)
        executions.append({"collector": collector, "status": result.returncode,
                           "stdout": result.stdout, "stderr": result.stderr})
    (tmp_path / "native-receipt.json").write_text(json.dumps({
        "schema": "pcc.fstring-import-native.v1", "backend": "self", "libpython": "off",
        "artifact": str(output),
        "artifact_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "runtime_archive": str(pcc_runtime_archive),
        "runtime_sha256": hashlib.sha256(pcc_runtime_archive.read_bytes()).hexdigest(),
        "executions": executions,
    }, indent=2) + "\n")
