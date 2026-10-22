"""Install the pcc wheel with uv and prove private overlay ownership."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

import pytest

pytestmark = pytest.mark.integration

REPO = Path.cwd()


def _run(
    command: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
    )


def _base_env(tmp_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    for name in (
        "LC_ALL",
        "PCC_DATA_HOME",
        "PCC_ENVIRONMENT",
        "PCC_PACKAGE_SITE",
        "PYTHONPATH",
        "XDG_DATA_HOME",
    ):
        env.pop(name, None)
    env["HOME"] = str(tmp_path / "home")
    return env


def _uv_run(project: Path, *args: str, env: dict[str, str], timeout: int = 120):
    return _run(
        ["uv", "run", "--project", str(project), "--no-sync", *args],
        cwd=project,
        env=env,
        timeout=timeout,
    )


def _json_stdout(process: subprocess.CompletedProcess[str]) -> dict[str, object]:
    assert process.returncode == 0, process.stdout + process.stderr
    return json.loads(process.stdout)


def _build_wheel(tmp_path: Path, env: dict[str, str]) -> Path:
    pcc1 = REPO / "build" / "bootstrap" / "pcc1"
    assert pcc1.is_file(), f"current pcc1 required: {pcc1}"
    dist = tmp_path / "dist"
    build_env = env.copy()
    build_env["PCC_BUILD_PCC1"] = str(pcc1)
    process = _run(
        ["uv", "build", "--wheel", "--out-dir", str(dist), str(REPO)],
        cwd=REPO,
        env=build_env,
        timeout=300,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    wheels = list(dist.glob("python_cc-*.whl"))
    assert len(wheels) == 1, process.stdout + process.stderr
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
    assert any(name.endswith(".data/scripts/pcc1") for name in names)
    assert any(name == "pcc/package/environment.py" for name in names)
    assert "pcc/runtime/libpy_runtime_pcc_py.a" in names
    assert "pcc/runtime/libpy_runtime_pcc_py.a.target" in names
    assert "pcc/runtime/libpy_runtime_pcc_py.a.wheel" in names
    return wheels[0]


def _create_project_environment(
    project: Path, wheel: Path, env: dict[str, str]
) -> Path:
    venv = project / ".venv"
    create = _run(
        ["uv", "venv", "--python", sys.executable, str(venv)],
        cwd=project,
        env=env,
        timeout=60,
    )
    assert create.returncode == 0, create.stdout + create.stderr
    install = _run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(venv / "bin" / "python"),
            "--no-deps",
            str(wheel),
        ],
        cwd=project,
        env=env,
        timeout=120,
    )
    assert install.returncode == 0, install.stdout + install.stderr
    assert (venv / "bin" / "pcc").is_file()
    assert (venv / "bin" / "pcc1").is_file()
    return venv


def test_uv_installed_pcc_and_pcc1_share_private_overlay(tmp_path):
    env = _base_env(tmp_path)
    wheel = _build_wheel(tmp_path, env)
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname = "uv-pcc-project"\nversion = "0.0.0"\n'
        'requires-python = ">=3.13"\n',
        encoding="utf-8",
    )
    venv = _create_project_environment(project, wheel, env)

    host_report = _json_stdout(
        _uv_run(project, "pcc", "env", "info", "--json", env=env)
    )
    pcc1_report = _json_stdout(
        _uv_run(project, "pcc1", "env", "info", "--json", env=env)
    )
    direct_env = env.copy()
    direct_env["VIRTUAL_ENV"] = str(venv)
    direct_report = _json_stdout(
        _run(
            [str(venv / "bin" / "pcc1"), "env", "info", "--json"],
            env=direct_env,
            timeout=30,
        )
    )
    assert host_report == pcc1_report == direct_report
    site = Path(str(host_report["selected_site_packages"]))
    assert host_report["selection_reason"] == "virtual-env"
    assert site.is_relative_to(venv / ".pcc" / "environments")

    source = tmp_path / "generic-source"
    package = source / "uv_overlay_demo"
    package.mkdir(parents=True)
    (source / "pyproject.toml").write_text(
        '[project]\nname = "uv-overlay-demo"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    (package / "__init__.py").write_text("VALUE = 42\n", encoding="utf-8")
    install = _uv_run(
        project,
        "pcc1",
        "-m",
        "pip",
        "install",
        str(source),
        env=env,
    )
    assert install.returncode == 0, install.stdout + install.stderr
    report = json.loads(install.stdout)
    installed = Path(str(report["installs"][0]["installed_path"]))
    assert installed.is_relative_to(site)

    cpython_sites = list((venv / "lib").glob("python*/site-packages"))
    assert len(cpython_sites) == 1
    assert not (cpython_sites[0] / "uv_overlay_demo").exists()
    assert not list(cpython_sites[0].glob("**/*.pcc*-pcc_native-*.so"))

    main = project / "app.py"
    main.write_text(
        "import uv_overlay_demo\nprint(uv_overlay_demo.VALUE)\n",
        encoding="utf-8",
    )
    app = project / "app"
    compile_process = _uv_run(
        project, "pcc1", str(main), "-o", str(app), env=env, timeout=240
    )
    assert compile_process.returncode == 0, (
        compile_process.stdout + compile_process.stderr
    )
    run_env = env.copy()
    run_env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    run = _run([str(app)], env=run_env, timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout == "42\n"

    original_root = host_report["root"]
    shutil.rmtree(venv)
    venv = _create_project_environment(project, wheel, env)
    recreated = _json_stdout(_uv_run(project, "pcc1", "env", "info", "--json", env=env))
    assert recreated["root"] == original_root
    recreated_site = Path(str(recreated["selected_site_packages"]))
    assert recreated_site == site
    assert not recreated_site.exists()

    missing = _uv_run(
        project,
        "pcc1",
        str(main),
        "-o",
        str(project / "missing-app"),
        env=env,
        timeout=120,
    )
    assert missing.returncode == 0, missing.stdout + missing.stderr
    missing_run = _run([str(project / "missing-app")], env=env, timeout=30)
    assert missing_run.returncode != 0
    assert "No module named 'uv_overlay_demo'" in (
        missing_run.stdout + missing_run.stderr
    )
