"""Fresh wheel -> installed pcc1 -> hostless package build and execution."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import zipfile

import pytest

from pcc.tools.runtime_archive_provenance import verify_runtime_archive_manifest
from tests.python.pcc1_gate import find_current_pcc1


pytestmark = [pytest.mark.integration, pytest.mark.pcc_gate(probe="pcc1")]

REPO = Path(__file__).resolve().parents[2]


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def _write_pure_wheel(path: Path) -> Path:
    with zipfile.ZipFile(path, "w") as wheel:
        wheel.writestr("hostless_pure/__init__.py", "VALUE = 41\n")
        wheel.writestr(
            "hostless_pure-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: hostless-pure\nVersion: 1.0\n",
        )
        wheel.writestr(
            "hostless_pure-1.0.dist-info/WHEEL",
            "Wheel-Version: 1.0\nTag: py3-none-any\n",
        )
    return path


def _write_native_sdist(root: Path) -> Path:
    source = root / "hostless-native-1.0"
    package = source / "hostless_native"
    package.mkdir(parents=True)
    (source / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools"]\n'
        'build-backend = "setuptools.build_meta"\n',
        encoding="utf-8",
    )
    (source / "setup.py").write_text(
        "from setuptools import Extension, setup\n"
        "setup(name='hostless-native', version='1.0', "
        "ext_modules=[Extension('hostless_native._native', "
        "sources=['hostless_native/_native.c'])])\n",
        encoding="utf-8",
    )
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "_native.c").write_text(
        "#include <Python.h>\n"
        "static int exec_module(PyObject *module) {\n"
        '  return PyModule_AddIntConstant(module, "ready", 1);\n'
        "}\n"
        "static PyModuleDef_Slot slots[] = {\n"
        "  {Py_mod_exec, exec_module}, {0, NULL}\n"
        "};\n"
        "static PyModuleDef definition = {\n"
        '  PyModuleDef_HEAD_INIT, "_native", NULL, 0, NULL, slots\n'
        "};\n"
        "PyMODINIT_FUNC PyInit__native(void) {\n"
        "  return PyModuleDef_Init(&definition);\n"
        "}\n",
        encoding="utf-8",
    )
    archive = root / "hostless-native-1.0.tar.gz"
    with tarfile.open(archive, "w:gz") as stream:
        stream.add(source, arcname=source.name)
    return archive


def _write_unowned_meson_source(root: Path) -> Path:
    package = root / "unsupported_build"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["meson-python"]\n'
        'build-backend = "mesonpy"\n',
        encoding="utf-8",
    )
    (root / "meson.build").write_text(
        "project('unsupported-build', 'c')\n", encoding="utf-8"
    )
    return root


def _guarded_hostless_env(root: Path, venv: Path) -> tuple[dict[str, str], Path]:
    guard = root / "host-command-guard"
    guard.mkdir()
    log = root / "forbidden-host-command.log"
    script = (
        "#!/bin/sh\n"
        'printf "%s\\n" "$0 $*" >> "$PCC_FORBIDDEN_HOST_LOG"\n'
        "exit 97\n"
    )
    for name in (
        "python",
        "python3",
        "python3.13",
        "python3.14",
        "python3-config",
        "pip",
        "pip3",
        "pcc",
    ):
        path = guard / name
        path.write_text(script, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    env = os.environ.copy()
    for name in ("LC_ALL", "PYTHONPATH", "PCC_PACKAGE_SITE"):
        env.pop(name, None)
    env["HOME"] = str(root / "home")
    env["VIRTUAL_ENV"] = str(venv)
    env["PCC_FORBIDDEN_HOST_LOG"] = str(log)
    env["PCC_HOST_PYTHON"] = str(guard / "python3")
    env["PCC_HOST_PCC"] = str(guard / "pcc")
    env["PATH"] = os.pathsep.join(
        [str(guard), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    )
    return env, log


@pytest.fixture(scope="module")
def installed_distribution(tmp_path_factory: pytest.TempPathFactory):
    root = tmp_path_factory.mktemp("pcc1-hostless-distribution")
    current_pcc1 = find_current_pcc1(REPO)
    assert current_pcc1 is not None, "receipt-current pcc1 is required"
    dist = root / "dist"
    dist.mkdir()

    # Building and installing the distribution is the acquisition phase.  Host
    # Python is forbidden only after the wheel's native pcc1 has been installed.
    acquisition_env = os.environ.copy()
    acquisition_env.pop("LC_ALL", None)
    acquisition_env.pop("PCC_HOST_PYTHON", None)
    acquisition_env.pop("PCC_HOST_PCC", None)
    acquisition_env["PCC_BUILD_PCC1"] = str(current_pcc1)
    built = _run(
        ["uv", "build", "--wheel", "--out-dir", str(dist), str(REPO)],
        cwd=REPO,
        env=acquisition_env,
        timeout=600,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    wheels = list(dist.glob("python_cc-*.whl"))
    assert len(wheels) == 1, built.stdout + built.stderr

    venv = root / ".venv"
    created = _run(
        ["uv", "venv", "--python", sys.executable, str(venv)],
        cwd=root,
        env=acquisition_env,
        timeout=60,
    )
    assert created.returncode == 0, created.stdout + created.stderr
    installed = _run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(venv / "bin" / "python"),
            "--no-deps",
            str(wheels[0]),
        ],
        cwd=root,
        env=acquisition_env,
        timeout=120,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr

    pcc1 = venv / "bin" / "pcc1"
    assert pcc1.is_file() and os.access(pcc1, os.X_OK)
    runtime_roots = list(venv.glob("lib/python*/site-packages/pcc/runtime"))
    assert len(runtime_roots) == 1
    runtime_root = runtime_roots[0]
    runtime_archive = runtime_root / "libpy_runtime_pcc_py.a"
    manifest = verify_runtime_archive_manifest(
        runtime_archive,
        runtime_root=runtime_root,
    )
    assert manifest["policy"] == "pcc-production-no-handwritten-c.v1"
    assert all(row["source_kind"] == "pcc-python" for row in manifest["members"])
    assert all(row["uses_host_cc"] is False for row in manifest["members"])
    marker = Path(str(runtime_archive) + ".wheel")
    assert marker.read_text(encoding="utf-8").startswith(
        "pcc.runtime-wheel-artifact.v2\n"
    )
    return root, venv, pcc1


def _install_with_pcc1(
    pcc1: Path,
    artifact: Path,
    *,
    site: Path,
    cache: Path,
    cwd: Path,
    env: dict[str, str],
    timeout: int = 180,
) -> dict[str, object]:
    result = _run(
        [
            str(pcc1),
            "-m",
            "pip",
            "install",
            str(artifact),
            "--abi",
            "pcc-native",
            "--build=owned",
            "--target",
            str(site),
            "--cache-dir",
            str(cache),
        ],
        cwd=cwd,
        env=env,
        timeout=timeout,
    )
    assert result.stdout, result.stderr
    report = json.loads(result.stdout)
    assert result.returncode == 0 and report["ok"] is True, (
        result.stdout + result.stderr
    )
    return report


def test_installed_distribution_builds_and_runs_packages_with_host_tools_blocked(
    installed_distribution,
) -> None:
    root, venv, pcc1 = installed_distribution
    work = root / "successful-build"
    work.mkdir()
    env, forbidden_log = _guarded_hostless_env(work, venv)
    site = work / "site"
    cache = work / "cache"

    pure = _install_with_pcc1(
        pcc1,
        _write_pure_wheel(work / "hostless_pure-1.0-py3-none-any.whl"),
        site=site,
        cache=cache,
        cwd=work,
        env=env,
    )
    native = _install_with_pcc1(
        pcc1,
        _write_native_sdist(work),
        site=site,
        cache=cache,
        cwd=work,
        env=env,
    )
    pure_manifest = pure["installs"][0]
    native_manifest = native["installs"][0]
    assert pure_manifest["install_success"] is True
    assert pure_manifest["links_libpython"] is False
    assert native_manifest["install_success"] is True
    assert native_manifest["links_libpython"] is False
    assert native_manifest["uses_cpython_extension_abi"] is False
    assert native_manifest["build_report"]["build_ownership"] == "owned"
    assert native_manifest["build_report"]["host_assisted"] is False
    assert native_manifest["build_report"]["host_python"] is None

    source = work / "app.py"
    source.write_text(
        "import hostless_pure\n"
        "from hostless_native import _native\n"
        "print(hostless_pure.VALUE + _native.ready)\n",
        encoding="utf-8",
    )
    app = work / "app"
    compile_env = env.copy()
    compile_env["PCC_PACKAGE_SITE"] = str(site)
    compiled = _run(
        [
            str(pcc1),
            "--backend",
            "self",
            "--python-libpython=off",
            "--ir-scaffold=on",
            str(source),
            "-o",
            str(app),
        ],
        cwd=work,
        env=compile_env,
        timeout=600,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    ran = _run([str(app)], cwd=work, env=env, timeout=60)
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert ran.stdout == "42\n"

    if sys.platform == "darwin":
        linkage = _run(["otool", "-L", str(app)], cwd=work, env=env, timeout=30)
        assert linkage.returncode == 0, linkage.stderr
        lowered = linkage.stdout.lower()
        assert "libpython" not in lowered and "python3" not in lowered
    elif sys.platform.startswith("linux"):
        linkage = _run(["readelf", "-d", str(app)], cwd=work, env=env, timeout=30)
        assert linkage.returncode == 0, linkage.stderr
        assert "libpython" not in linkage.stdout.lower()
    assert not forbidden_log.exists(), forbidden_log.read_text(encoding="utf-8")


def test_unsupported_owned_build_fails_before_partial_environment_publish(
    installed_distribution,
) -> None:
    root, venv, pcc1 = installed_distribution
    work = root / "unsupported-build"
    work.mkdir()
    env, forbidden_log = _guarded_hostless_env(work, venv)
    source = _write_unowned_meson_source(work / "source")
    site = work / "site"
    result = _run(
        [
            str(pcc1),
            "-m",
            "pip",
            "install",
            str(source),
            "--abi",
            "pcc-native",
            "--build=owned",
            "--target",
            str(site),
            "--cache-dir",
            str(work / "cache"),
        ],
        cwd=work,
        env=env,
        timeout=180,
    )
    assert result.stdout, result.stderr
    report = json.loads(result.stdout)
    assert result.returncode != 0
    assert report["ok"] is False
    diagnostics = json.dumps(report.get("installs", report), sort_keys=True)
    assert "PCC-PKG-OWNED-BUILD-TOOL-REQUIRED" in diagnostics
    assert not site.exists() or not any(site.iterdir())
    assert not forbidden_log.exists(), forbidden_log.read_text(encoding="utf-8")
