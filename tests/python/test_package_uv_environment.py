from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys

from pcc.package.environment import resolve_package_environment
from pcc.frontends.python.pipeline import (
    _bootstrap_append_install_prefix_candidates,
    _pcc_dir_has_source_files,
    _runtime_archive_target_id,
    _runtime_archive_wheel_stamp_matches,
)

REPO = Path.cwd()


def _write_project(project: Path) -> None:
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname = "uv-pcc-env-test"\nversion = "0.0.0"\n'
        'requires-python = ">=3.13"\n',
        encoding="utf-8",
    )


def test_uv_run_exposes_project_virtual_environment_to_shared_resolver(tmp_path):
    project = tmp_path / "project"
    _write_project(project)
    venv = project / ".venv"
    create = subprocess.run(
        ["uv", "venv", "--python", sys.executable, str(venv)],
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert create.returncode == 0, create.stdout + create.stderr

    probe = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(project),
            "--no-sync",
            "python",
            "-c",
            "import json, os; print(json.dumps({'venv': os.environ.get('VIRTUAL_ENV')}))",
        ],
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert probe.returncode == 0, probe.stdout + probe.stderr
    uv_env = json.loads(probe.stdout)
    assert Path(uv_env["venv"]).resolve() == venv.resolve()

    report = resolve_package_environment(
        {"HOME": str(tmp_path / "home"), "VIRTUAL_ENV": uv_env["venv"]},
        target_triple="aarch64-apple-darwin",
    )
    root = Path(str(report["root"]))
    assert report["selection_reason"] == "virtual-env"
    assert root.is_relative_to(venv / ".pcc" / "environments")
    assert Path(str(report["selected_site_packages"])) == root / "site-packages"


def test_uv_overlay_is_disjoint_from_cpython_site_packages(tmp_path):
    venv = tmp_path / ".venv"
    report = resolve_package_environment(
        {"HOME": str(tmp_path / "home"), "VIRTUAL_ENV": str(venv)},
        target_triple="aarch64-apple-darwin",
    )
    overlay = Path(str(report["selected_site_packages"]))
    cpython_site = venv / "lib" / "python3.13" / "site-packages"

    assert overlay.is_relative_to(venv / ".pcc")
    assert not overlay.is_relative_to(cpython_site)
    assert not cpython_site.is_relative_to(overlay)


def test_recreated_uv_environment_does_not_select_global_package_site(tmp_path):
    venv = tmp_path / ".venv"
    env = {"HOME": str(tmp_path / "home"), "VIRTUAL_ENV": str(venv)}
    before = resolve_package_environment(env, target_triple="aarch64-apple-darwin")
    selected = Path(str(before["selected_site_packages"]))
    selected.mkdir(parents=True)
    (selected / "stale.py").write_text("VALUE = 1\n", encoding="utf-8")

    # Model uv replacing .venv: the owner path stays deterministic, while a
    # fresh environment has no inherited overlay contents.
    import shutil

    shutil.rmtree(venv)
    venv.mkdir()
    after = resolve_package_environment(env, target_triple="aarch64-apple-darwin")

    assert after["root"] == before["root"]
    assert after["package_sites"] == before["package_sites"]
    assert not Path(str(after["selected_site_packages"])).exists()
    assert str(tmp_path / "home" / ".local" / "share") not in ":".join(
        after["package_sites"]
    )


def test_wheel_contract_exposes_native_pcc1_and_verified_reuse_input():
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    hook = (REPO / "hatch_build.py").read_text(encoding="utf-8")

    assert (
        'build_data.setdefault("shared_scripts", {})[str(out_binary)] = "pcc1.exe" if os.name == "nt" else "pcc1"' in hook
    )
    assert "PCC_BUILD_PCC1" in hook
    assert "_validate_prebuilt_pcc1" in hook
    assert '] = rel + ".target"' in hook
    assert "wheel_stamp = self._write_wheel_archive_stamp(archive)" in hook
    assert 'rel + ".wheel"' in hook
    assert "_runtime_archive_inputs_newer" in hook
    assert 'cmd.append("-B")' in hook
    assert "refusing to publish a wheel" in hook
    assert 'if version == "editable":' in hook
    import tomllib

    metadata = tomllib.loads(pyproject)
    assert metadata["project"]["dependencies"] == []
    assert metadata["build-system"]["requires"] == ["hatchling"]
    assert '[project.scripts]\npcc = "pcc.driver.cli_launcher:main"' in pyproject


def test_native_pcc1_finds_runtime_resources_under_installed_prefix(tmp_path):
    prefix = tmp_path / ".venv"
    pcc_dir = prefix / "lib" / "python3.13" / "site-packages" / "pcc"
    (pcc_dir / "backend").mkdir(parents=True)
    (pcc_dir / "runtime" / "include").mkdir(parents=True)
    (pcc_dir / "__init__.py").write_text("", encoding="utf-8")
    (pcc_dir / "backend" / "self_backend_dispatch.py").write_text("", encoding="utf-8")
    (pcc_dir / "runtime" / "include" / "py_runtime.h").write_text(
        "", encoding="utf-8"
    )

    candidates: list[str] = []
    _bootstrap_append_install_prefix_candidates(candidates, str(prefix))

    assert str(pcc_dir) in candidates
    assert _pcc_dir_has_source_files(str(pcc_dir))


def test_installed_wheel_runtime_marker_is_schema_and_target_labeled(tmp_path):
    archive = tmp_path / "libpy_runtime_pcc_py.a"
    archive.write_bytes(b"archive")
    manifest = Path(str(archive) + ".provenance.json")
    manifest.write_bytes(b"manifest\n")
    inventory = Path(str(archive) + ".capi_syms")
    inventory.write_bytes(b"PyRuntime_Anchor\n")
    marker = Path(str(archive) + ".wheel")
    marker.write_text(
        "pcc.runtime-wheel-artifact.v2\n"
        + "target="
        + _runtime_archive_target_id()
        + "\narchive-sha256="
        + hashlib.sha256(archive.read_bytes()).hexdigest()
        + "\nmanifest-sha256="
        + hashlib.sha256(manifest.read_bytes()).hexdigest()
        + "\ncapi-inventory-sha256="
        + hashlib.sha256(inventory.read_bytes()).hexdigest()
        + "\n",
        encoding="utf-8",
    )

    assert _runtime_archive_wheel_stamp_matches(str(archive))
    marker.write_text(marker.read_text(encoding="utf-8").replace(
        "target=" + _runtime_archive_target_id(), "target=wrong-target"
    ), encoding="utf-8")
    assert not _runtime_archive_wheel_stamp_matches(str(archive))
