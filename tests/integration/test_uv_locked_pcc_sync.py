"""Real uv-lock projection into a wheel-owned pcc-native environment."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile

import pytest

from tests.python.pcc1_gate import find_current_pcc1

pytestmark = pytest.mark.integration

REPO = Path.cwd()
NUMPY_ROOT = REPO / "projects" / "numpy-2.4.4"


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
        "VIRTUAL_ENV",
        "XDG_DATA_HOME",
    ):
        env.pop(name, None)
    env["HOME"] = str(tmp_path / "home")
    env["PCC_PACKAGE_TARGET_PYTHON"] = "3.11"
    env["PCC_PACKAGE_CACHE"] = str(tmp_path / "package-cache")
    return env


def _uv_run(project: Path, *args: str, env: dict[str, str], timeout: int = 120):
    return _run(
        ["uv", "run", "--project", str(project), "--no-sync", *args],
        cwd=project,
        env=env,
        timeout=timeout,
    )


@pytest.fixture(scope="module")
def pcc_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("uv-locked-wheel")
    dist = root / "dist"
    pcc1 = find_current_pcc1(REPO)
    assert pcc1 is not None, "current-source pcc1 is required for the wheel"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_BUILD_PCC1"] = str(pcc1)
    process = _run(
        ["uv", "build", "--wheel", "--out-dir", str(dist), str(REPO)],
        cwd=REPO,
        env=env,
        timeout=600,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    wheels = list(dist.glob("python_cc-*.whl"))
    assert len(wheels) == 1
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
    assert "pcc/package/uv_lock_sync.py" in names
    assert "pcc/runtime/libpy_runtime_pcc_py.a" in names
    assert "pcc/runtime/libpy_runtime_pcc_py.a.target" in names
    assert "pcc/runtime/libpy_runtime_pcc_py.a.wheel" in names
    assert any(name.endswith(".data/scripts/pcc1") for name in names)
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
    return venv


def _write_local_package(
    root: Path,
    *,
    distribution: str,
    module: str,
    body: str,
    dependencies: tuple[tuple[str, Path], ...] = (),
) -> None:
    root.mkdir(parents=True)
    requirements = ",\n".join(
        f'  "{name} @ {path.resolve().as_uri()}"' for name, path in dependencies
    )
    dependency_table = (
        "dependencies = [\n" + requirements + "\n]\n" if requirements else ""
    )
    (root / "pyproject.toml").write_text(
        "[project]\n"
        f'name = "{distribution}"\n'
        'version = "1.0.0"\n'
        'requires-python = ">=3.11"\n' + dependency_table,
        encoding="utf-8",
    )
    package = root / module
    package.mkdir()
    (package / "__init__.py").write_text(body, encoding="utf-8")


def _write_project(project: Path, dependency: str, dependency_path: Path) -> None:
    project.mkdir()
    (project / "pyproject.toml").write_text(
        "[project]\n"
        f'name = "{project.name}"\n'
        'version = "0.0.0"\n'
        'requires-python = ">=3.11"\n'
        f'dependencies = ["{dependency} @ {dependency_path.resolve().as_uri()}"]\n',
        encoding="utf-8",
    )


def _lock_project(project: Path, env: dict[str, str]) -> bytes:
    process = _run(
        ["uv", "lock", "--project", str(project), "--offline"],
        cwd=project,
        env=env,
        timeout=120,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    return (project / "uv.lock").read_bytes()


def _sync(project: Path, env: dict[str, str], timeout: int = 180) -> dict[str, object]:
    process = _uv_run(
        project,
        "pcc",
        "sync",
        "--locked",
        "--install-timeout",
        str(timeout),
        env=env,
        timeout=timeout + 60,
    )
    assert process.returncode == 0, process.stdout + process.stderr
    return json.loads(process.stdout)


def _assert_no_libpython(path: Path) -> None:
    linkage = _run(["otool", "-L", str(path)], timeout=30)
    assert linkage.returncode == 0, linkage.stderr
    lowered = linkage.stdout.lower()
    assert "libpython" not in lowered, linkage.stdout
    assert "python3" not in lowered, linkage.stdout


def test_uv_locked_generic_graph_is_transactional_and_repeat_is_noop(
    tmp_path: Path, pcc_wheel: Path
):
    env = _base_env(tmp_path)
    base = tmp_path / "sources" / "locked-base"
    leaf = tmp_path / "sources" / "locked-leaf"
    _write_local_package(
        base,
        distribution="locked-base",
        module="locked_base",
        body="VALUE = 40\n",
    )
    _write_local_package(
        leaf,
        distribution="locked-leaf",
        module="locked_leaf",
        body="from locked_base import VALUE\nRESULT = VALUE + 2\n",
        dependencies=(("locked-base", base),),
    )
    project = tmp_path / "generic-project"
    _write_project(project, "locked-leaf", leaf)
    original_lock = _lock_project(project, env)
    venv = _create_project_environment(project, pcc_wheel, env)

    first = _sync(project, env)
    second = _sync(project, env)
    assert first["changed"] is True
    assert [row["name"] for row in first["packages"]] == [
        "locked-base",
        "locked-leaf",
    ]
    assert second["changed"] is False
    assert second["downloads"] == 0
    assert second["native_builds"] == 0
    assert (project / "uv.lock").read_bytes() == original_lock

    environment = json.loads(
        _uv_run(project, "pcc1", "env", "info", "--json", env=env).stdout
    )
    site = Path(str(environment["selected_site_packages"]))
    assert site.is_relative_to(venv / ".pcc")
    assert environment["lock_provenance"]["lock_sha256"] == first["lock_sha256"]
    assert not (venv / "lib" / "python3.13" / "site-packages" / "locked_leaf").exists()
    assert (site / "locked_base" / "pcc-package.json").is_file()
    assert (site / "locked_leaf" / "pcc-package.json").is_file()

    app_source = project / "app.py"
    app_source.write_text(
        "import locked_leaf\nprint(locked_leaf.RESULT)\n", encoding="utf-8"
    )
    app = project / "app"
    compile_process = _uv_run(
        project,
        "pcc1",
        str(app_source),
        "-o",
        str(app),
        env=env,
        timeout=240,
    )
    assert compile_process.returncode == 0, (
        compile_process.stdout + compile_process.stderr
    )
    run_env = env.copy()
    run_env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    run = _run([str(app)], env=run_env, timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout == "42\n"
    _assert_no_libpython(app)


def test_uv_locked_numpy_sync_compiles_and_runs_without_libpython(
    tmp_path: Path, pcc_wheel: Path
):
    assert NUMPY_ROOT.is_dir(), f"real NumPy source tree is required: {NUMPY_ROOT}"
    env = _base_env(tmp_path)
    project = tmp_path / "numpy-project"
    _write_project(project, "numpy", NUMPY_ROOT)
    original_lock = _lock_project(project, env)
    venv = _create_project_environment(project, pcc_wheel, env)

    first = _sync(project, env, timeout=600)
    second = _sync(project, env, timeout=600)
    assert first["changed"] is True
    assert [row["name"] for row in first["packages"]] == ["numpy"]
    assert first["packages"][0]["artifact_kind"] == "local-directory"
    assert len(first["packages"][0]["artifact_sha256"]) == 64
    assert first["native_builds"] == 1
    assert second["changed"] is False
    assert second["downloads"] == 0
    assert second["native_builds"] == 0
    assert (project / "uv.lock").read_bytes() == original_lock

    environment = json.loads(
        _uv_run(project, "pcc1", "env", "info", "--json", env=env).stdout
    )
    site = Path(str(environment["selected_site_packages"]))
    assert site.is_relative_to(venv / ".pcc")
    assert environment["lock_provenance"]["lock_sha256"] == first["lock_sha256"]
    assert list(site.glob("numpy/**/*.pcc*-pcc_native-*.so"))
    assert not list(site.glob("numpy/**/*.cpython-*.so"))
    assert (site / "numpy" / "pcc-package.json").is_file()

    app_source = project / "numpy_app.py"
    app_source.write_text(
        "import numpy as np\n"
        "print(np.__version__)\n"
        "print([int(x) for x in np.array([1, 2, 3]) + 1])\n",
        encoding="utf-8",
    )
    app = project / "numpy-app"
    compile_profile = project / "numpy-compile-profile.json"
    compile_process = _uv_run(
        project,
        "pcc1",
        "--profile-json",
        str(compile_profile),
        str(app_source),
        "-o",
        str(app),
        env=env,
        timeout=600,
    )
    assert compile_process.returncode == 0, (
        compile_process.stdout + compile_process.stderr
    )
    profile = json.loads(compile_profile.read_text(encoding="utf-8"))
    counters = profile["counters"]
    assert counters["multi_frontend_jobs"] == 1
    assert counters["link_self_native_split_modules"] >= 1
    assert (
        counters["link_self_native_split_shards"]
        > counters["link_self_native_split_modules"]
    )
    assert 1 <= counters["link_self_native_configured_jobs"] <= 2
    assert counters["link_self_native_oversized_object_count"] >= 1
    assert counters["link_self_native_safe_object_count"] >= 1
    assert counters["link_self_native_oversized_emit_jobs"] == 1
    assert (
        counters["link_self_native_safe_emit_jobs"]
        == counters["link_self_native_configured_jobs"]
    )
    assert (
        counters["link_self_native_emit_jobs"]
        == counters["link_self_native_configured_jobs"]
    )
    assert "multi_frontend_codegen_worker_collect" in profile["phase_totals_s"]
    assert "link_self_native_pre_split_collect" in profile["phase_totals_s"]
    assert "link_self_native_post_split_collect" in profile["phase_totals_s"]
    run_env = env.copy()
    run_env.pop("VIRTUAL_ENV", None)
    run_env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    run_env["PCC_PACKAGE_SITE"] = ""
    run = _run([str(app)], env=run_env, timeout=60)
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.splitlines() == ["2.4.4", "[2, 3, 4]"]
    _assert_no_libpython(app)
