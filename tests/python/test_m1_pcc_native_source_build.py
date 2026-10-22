from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from pcc1_gate import find_current_pcc1, skip_or_fail_no_current_pcc1

from pcc.driver import cli_bootstrap

REPO = Path(__file__).absolute().parents[2]


def _write_generic_extension_sdist(tmp_path: Path) -> Path:
    root = tmp_path / "generic-ext-0.1"
    package = root / "generic_ext"
    package.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n',
        encoding="utf-8",
    )
    (root / "setup.py").write_text(
        "from setuptools import Extension, setup\n"
        "setup(name='generic-ext', version='0.1', "
        "ext_modules=[Extension('generic_ext._native', "
        "sources=['generic_ext/_native.c'])])\n",
        encoding="utf-8",
    )
    # Real source projects commonly carry top-level Python helpers (Sphinx
    # conf.py, release scripts, etc.).  They must not make the installer copy
    # the whole project root as though it were the import package.
    (root / "docs_conf.py").write_text("PROJECT = 'generic-ext'\n", encoding="utf-8")
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (package / "_native.c").write_text(
        "#include <Python.h>\n"
        "static int generic_exec(PyObject *module) {\n"
        '    return PyModule_AddIntConstant(module, "ready", 1);\n'
        "}\n"
        "static PyModuleDef_Slot slots[] = {\n"
        "    {Py_mod_exec, generic_exec}, {0, NULL}\n"
        "};\n"
        "static PyModuleDef module = {\n"
        '    PyModuleDef_HEAD_INIT, "_native", NULL, 0, NULL, slots\n'
        "};\n"
        "PyMODINIT_FUNC PyInit__native(void) {\n"
        "    return PyModuleDef_Init(&module);\n"
        "}\n",
        encoding="utf-8",
    )
    archive = tmp_path / "generic-ext-0.1.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(root, arcname=root.name)
    return archive


def _assert_native_source_manifest(manifest: dict[str, object], site: Path) -> None:
    suffix = manifest["pcc_native_extension_suffix"]
    artifact = site / "generic_ext" / ("_native" + suffix)
    assert manifest["ok"] is True, manifest
    assert manifest["install_success"] is True
    assert manifest["links_libpython"] is False
    assert manifest["uses_cpython_extension_abi"] is False
    assert manifest["linkage_native_package_claim"] is True
    assert manifest["capability_profile"]["execution_mode"] == "pcc-native"
    assert suffix.startswith(".pcc3-pcc_native-")
    assert suffix.endswith(".so")
    assert artifact.is_file()
    assert "cpython-" not in artifact.name
    assert "abi3" not in artifact.name
    statuses = {action["status"] for action in manifest["build_report"]["actions"]}
    assert statuses == {"passed"}


def test_native_install_builds_generic_single_c_sdist_without_host_helpers(
    tmp_path, monkeypatch
):
    archive = _write_generic_extension_sdist(tmp_path)
    site = tmp_path / "site"
    cache = tmp_path / "cache"
    monkeypatch.setenv("PCC_HOST_PYTHON", "/usr/bin/false")
    monkeypatch.setenv("PCC_HOST_PCC", "/usr/bin/false")

    manifest = json.loads(
        cli_bootstrap._native_install_manifest_json(
            str(archive), str(site), str(cache), [], "pcc-native"
        )
    )

    _assert_native_source_manifest(manifest, site)


def test_current_pcc1_builds_generic_single_c_sdist_without_host_helpers(tmp_path):
    pcc1 = find_current_pcc1(REPO)
    if pcc1 is None:
        skip_or_fail_no_current_pcc1("no current pcc1 with native sdist build support")
    archive = _write_generic_extension_sdist(tmp_path)
    site = tmp_path / "site"
    cache = tmp_path / "cache"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_HOST_PCC"] = "/usr/bin/false"

    proc = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pip",
            "install",
            str(archive),
            "--abi",
            "pcc-native",
            "--target",
            str(site),
            "--cache-dir",
            str(cache),
        ],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    report = json.loads(proc.stdout)
    assert report["ok"] is True
    _assert_native_source_manifest(report["installs"][0], site)


def test_current_pcc1_imports_generic_built_extension_without_host_helpers(tmp_path):
    pcc1 = find_current_pcc1(REPO)
    if pcc1 is None:
        skip_or_fail_no_current_pcc1("no current pcc1 with native extension support")
    archive = _write_generic_extension_sdist(tmp_path)
    site = tmp_path / "site"
    cache = tmp_path / "cache"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    env["PCC_HOST_PCC"] = "/usr/bin/false"

    install = subprocess.run(
        [
            str(pcc1),
            "-m",
            "pip",
            "install",
            str(archive),
            "--abi",
            "pcc-native",
            "--target",
            str(site),
            "--cache-dir",
            str(cache),
        ],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert install.returncode == 0, install.stdout + install.stderr
    install_report = json.loads(install.stdout)
    assert install_report["ok"] is True
    _assert_native_source_manifest(install_report["installs"][0], site)

    source = tmp_path / "generic_extension_app.py"
    source.write_text(
        "from generic_ext import _native\nprint('ready', _native.ready)\n",
        encoding="utf-8",
    )
    exe = tmp_path / "generic_extension_app"
    compile_env = env.copy()
    compile_env["PCC_PACKAGE_SITE"] = str(site)
    compiled = subprocess.run(
        [
            str(pcc1),
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(source),
            "-o",
            str(exe),
        ],
        cwd=REPO,
        env=compile_env,
        text=True,
        capture_output=True,
        timeout=600,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr

    dependency_command = (
        ["otool", "-L", str(exe)] if sys.platform == "darwin" else ["ldd", str(exe)]
    )
    dependencies = subprocess.run(
        dependency_command,
        cwd=REPO,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert dependencies.returncode == 0, dependencies.stdout + dependencies.stderr
    dependency_text = (dependencies.stdout + dependencies.stderr).lower()
    assert "libpython" not in dependency_text
    assert "python.framework" not in dependency_text
    assert "libllvm" not in dependency_text

    run = subprocess.run(
        [str(exe)],
        cwd=REPO,
        env=compile_env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout == "ready 1\n"
    assert run.stderr == ""


@pytest.mark.parametrize("forbidden", ["simplejson", "immutables", "pyahocorasick"])
def test_source_build_dispatch_has_no_candidate_package_names(forbidden):
    source = (REPO / "pcc" / "driver" / "cli_bootstrap.py").read_text(encoding="utf-8").lower()
    assert forbidden not in source
