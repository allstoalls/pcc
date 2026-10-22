from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest

import pcc.package.uv_lock_sync as uv_lock_sync
from pcc.package.uv_lock_sync import (
    UvLockSyncError,
    marker_applies,
    project_uv_lock,
    sync_uv_lock,
)
from pcc.package.environment import environment_info_json, resolve_package_environment


def _write_package(root: Path, name: str, value: int) -> None:
    package = root / name.replace("-", "_")
    package.mkdir(parents=True)
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "{name}"\nversion = "1.0.0"\n', encoding="utf-8"
    )
    (package / "__init__.py").write_text(f"VALUE = {value}\n", encoding="utf-8")


def _environment(tmp_path: Path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path / "home"),
        "VIRTUAL_ENV": str(tmp_path / "project" / ".venv"),
        "PCC_PACKAGE_TARGET_PYTHON": "3.11",
        "PCC_TARGET_TRIPLE": "aarch64-apple-darwin",
        "PCC_PACKAGE_CACHE": str(tmp_path / "cache"),
    }


def _graph_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        '[project]\nname = "demo-project"\nversion = "0.0.0"\n',
        encoding="utf-8",
    )
    for name, value in (
        ("dep-a", 1),
        ("dep-b", 2),
        ("dep-win", 3),
        ("dep-group", 4),
        ("dep-extra", 5),
    ):
        _write_package(project / "deps" / name, name, value)
    (project / "uv.lock").write_text(
        """version = 1
revision = 3
requires-python = ">=3.11"

[[package]]
name = "demo-project"
version = "0.0.0"
source = { editable = "." }
dependencies = [
  { name = "dep-a" },
  { name = "dep-win", marker = "sys_platform == 'win32'" },
]

[package.dev-dependencies]
test = [{ name = "dep-group" }]

[package.optional-dependencies]
speed = [{ name = "dep-extra" }]

[[package]]
name = "dep-a"
version = "1.0.0"
source = { directory = "deps/dep-a" }
dependencies = [{ name = "dep-b" }]

[[package]]
name = "dep-b"
version = "1.0.0"
source = { directory = "deps/dep-b" }

[[package]]
name = "dep-win"
version = "1.0.0"
source = { directory = "deps/dep-win" }

[[package]]
name = "dep-group"
version = "1.0.0"
source = { directory = "deps/dep-group" }

[[package]]
name = "dep-extra"
version = "1.0.0"
source = { directory = "deps/dep-extra" }
""",
        encoding="utf-8",
    )
    return project


def _fake_pcc1(
    path: Path,
    counter: Path,
    *,
    fail: bool = False,
    failure_diagnostic: str = "forced failure",
) -> None:
    script = f"""#!{sys.executable}
import json
from pathlib import Path
import shutil
import sys

counter = Path({str(counter)!r})
count = int(counter.read_text() if counter.exists() else "0") + 1
counter.write_text(str(count))
if {fail!r}:
    print({failure_diagnostic!r}, file=sys.stderr)
    raise SystemExit(7)
args = sys.argv[1:]
source = Path(args[args.index("install") + 1])
target = Path(args[args.index("--target") + 1])
target.mkdir(parents=True, exist_ok=True)
for child in source.iterdir():
    if child.name == "pyproject.toml":
        continue
    destination = target / child.name
    if child.is_dir():
        shutil.copytree(child, destination)
    elif child.suffix == ".py":
        shutil.copy2(child, destination)
print(json.dumps({{"ok": True, "installs": [{{"build_report": {{"skipped": True}}}}]}}))
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)


def test_project_uv_lock_selects_frozen_graph_groups_extras_and_markers(tmp_path):
    project = _graph_project(tmp_path)
    projection = project_uv_lock(
        project / "uv.lock",
        project_root=project,
        target_python="3.11",
        target_triple="aarch64-apple-darwin",
        groups=("test",),
        extras=("speed",),
    )

    assert projection["schema"] == "pcc.uv-lock-adapter.v1"
    assert projection["uv_lock_version"] == 1
    assert projection["uv_lock_revision"] == 3
    assert projection["root_packages"] == ["demo-project"]
    assert [row["name"] for row in projection["packages"]] == [
        "dep-b",
        "dep-a",
        "dep-group",
        "dep-extra",
    ]
    assert all(
        row["artifact"]["kind"] == "local-directory"
        and len(row["artifact"]["sha256"]) == 64
        for row in projection["packages"]
    )


def test_marker_evaluator_is_target_specific_and_fails_closed():
    environment = {
        "sys_platform": "darwin",
        "python_version": "3.11",
        "extra": "",
    }
    assert marker_applies(
        "sys_platform == 'darwin' and python_version >= '3.11'", environment
    )
    assert not marker_applies("sys_platform == 'win32'", environment)
    assert not marker_applies(
        "python_version >= '3.11'", {**environment, "python_version": "3.9"}
    )
    assert marker_applies("extra == 'speed'", environment, extras=("speed",))
    with pytest.raises(UvLockSyncError, match="unsupported marker variable") as exc:
        marker_applies("unknown_platform == 'x'", environment)
    assert exc.value.code == "PCC-PKG-UVLOCK-MARKER-UNSUPPORTED"


def test_uv_lock_rejects_schema_target_and_missing_artifact(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    lock = project / "uv.lock"
    lock.write_text(
        'version = 2\nrevision = 3\nrequires-python = ">=3.11"\npackage = []\n',
        encoding="utf-8",
    )
    with pytest.raises(UvLockSyncError) as schema:
        project_uv_lock(
            lock,
            target_python="3.11",
            target_triple="aarch64-apple-darwin",
        )
    assert schema.value.code == "PCC-PKG-UVLOCK-UNSUPPORTED-SCHEMA"

    lock.write_text(
        """version = 1
revision = 3
requires-python = ">=3.13"
[[package]]
name = "project"
version = "0"
source = { editable = "." }
""",
        encoding="utf-8",
    )
    with pytest.raises(UvLockSyncError) as target:
        project_uv_lock(
            lock,
            target_python="3.11",
            target_triple="aarch64-apple-darwin",
        )
    assert target.value.code == "PCC-PKG-UVLOCK-TARGET-PYTHON-MISMATCH"

    lock.write_text(
        """version = 1
revision = 3
requires-python = ">=3.11"
[[package]]
name = "project"
version = "0"
source = { editable = "." }
dependencies = [{ name = "native-only" }]
[[package]]
name = "native-only"
version = "1"
source = { registry = "https://example.invalid/simple" }
wheels = [{ url = "https://example.invalid/native_only-1-cp313-cp313-macosx_11_0_arm64.whl", hash = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" }]
""",
        encoding="utf-8",
    )
    with pytest.raises(UvLockSyncError) as artifact:
        project_uv_lock(
            lock,
            target_python="3.11",
            target_triple="aarch64-apple-darwin",
        )
    assert artifact.value.code == "PCC-PKG-UVLOCK-INCOMPATIBLE-WHEEL"

    lock.write_text(
        """version = 1
revision = 3
requires-python = ">=3.11"
[[package]]
name = "project"
version = "0"
source = { editable = "." }
dependencies = [{ name = "missing-source" }]
[[package]]
name = "missing-source"
version = "1"
source = { directory = "does-not-exist" }
""",
        encoding="utf-8",
    )
    with pytest.raises(UvLockSyncError) as missing:
        project_uv_lock(
            lock,
            target_python="3.11",
            target_triple="aarch64-apple-darwin",
        )
    assert missing.value.code == "PCC-PKG-UVLOCK-MISSING-ARTIFACT"


def test_locked_sync_is_transactional_and_unchanged_repeat_skips_installer(tmp_path):
    project = _graph_project(tmp_path)
    stale_build = project / "deps" / "dep-b" / "build"
    stale_build.mkdir()
    (stale_build / "stale.cpython-314-darwin.so").write_bytes(b"stale")
    # Keep the transaction probe focused on one local dependency.
    text = (project / "uv.lock").read_text(encoding="utf-8")
    text = text.replace(
        'dependencies = [\n  { name = "dep-a" },\n  { name = "dep-win", marker = "sys_platform == \'win32\'" },\n]',
        'dependencies = [{ name = "dep-b" }]',
    )
    (project / "uv.lock").write_text(text, encoding="utf-8")
    original_lock = (project / "uv.lock").read_bytes()
    env = _environment(tmp_path)
    report = resolve_package_environment(env)
    environment_root = Path(str(report["root"]))
    old_site = environment_root / "site-packages"
    old_site.mkdir(parents=True)
    (old_site / "stale.py").write_text("VALUE = 0\n", encoding="utf-8")
    counter = tmp_path / "pcc1-count"
    pcc1 = tmp_path / "pcc1"
    _fake_pcc1(pcc1, counter)

    first = sync_uv_lock(
        project / "uv.lock",
        project_root=project,
        pcc1=str(pcc1),
        environ=env,
    )
    second = sync_uv_lock(
        project / "uv.lock",
        project_root=project,
        pcc1=str(pcc1),
        environ=env,
    )

    assert first["changed"] is True
    assert second["changed"] is False
    assert second["downloads"] == 0
    assert second["native_builds"] == 0
    assert first["packages"][0]["dependencies"] == []
    assert first["packages"][0]["artifact_path"].endswith("deps/dep-b")
    assert first["packages"][0]["artifact_url"] is None
    assert first["packages"][0]["downloaded"] is False
    assert counter.read_text(encoding="utf-8") == "1"
    assert not (old_site / "stale.py").exists()
    assert not (old_site / "build").exists()
    assert (old_site / "dep_b" / "__init__.py").is_file()
    assert (project / "uv.lock").read_bytes() == original_lock
    published = resolve_package_environment(env)
    assert published["lock_provenance"]["lock_sha256"] == first["lock_sha256"]
    inspected = json.loads(environment_info_json(env))
    assert inspected["lock_provenance"]["lock_sha256"] == first["lock_sha256"]
    assert not (Path(env["VIRTUAL_ENV"]) / "lib" / "python3.13").exists()


def test_locked_sync_reselects_python_markers_in_same_native_environment(tmp_path):
    project = _graph_project(tmp_path)
    lock_path = project / "uv.lock"
    lock_text = lock_path.read_text(encoding="utf-8")
    old_marker = "sys_platform == 'win32'"
    assert lock_text.count(old_marker) == 1
    lock_path.write_text(lock_text.replace(old_marker, "python_version >= '3.16'"))
    env = {**_environment(tmp_path), "PCC_PACKAGE_TARGET_PYTHON": "3.15"}
    pcc1 = tmp_path / "pcc1"
    _fake_pcc1(pcc1, tmp_path / "pcc1-count")

    first = sync_uv_lock(lock_path, project_root=project, pcc1=str(pcc1), environ=env)
    later_env = {**env, "PCC_PACKAGE_TARGET_PYTHON": "3.16"}
    second = sync_uv_lock(
        lock_path, project_root=project, pcc1=str(pcc1), environ=later_env
    )
    assert first["environment_root"] == second["environment_root"]
    assert "dep-win" not in {package["name"] for package in first["packages"]}
    assert second["changed"] is True, second
    assert "dep-win" in {package["name"] for package in second["packages"]}
    assert second["lock_provenance"]["target_python"] == "3.16"
    assert (
        Path(second["environment_root"]) / "site-packages/dep_win/__init__.py"
    ).is_file()
    repeated = sync_uv_lock(
        lock_path, project_root=project, pcc1=str(pcc1), environ=later_env
    )
    assert repeated["changed"] is False


def test_failed_locked_sync_preserves_previous_environment(tmp_path):
    project = _graph_project(tmp_path)
    env = _environment(tmp_path)
    environment_root = Path(str(resolve_package_environment(env)["root"]))
    site = environment_root / "site-packages"
    site.mkdir(parents=True)
    sentinel = site / "keep.py"
    sentinel.write_text("VALUE = 1\n", encoding="utf-8")
    pcc1 = tmp_path / "pcc1-fail"
    _fake_pcc1(pcc1, tmp_path / "failure-count", fail=True)

    with pytest.raises(UvLockSyncError) as failure:
        sync_uv_lock(
            project / "uv.lock",
            project_root=project,
            pcc1=str(pcc1),
            environ=env,
        )

    assert failure.value.code == "PCC-PKG-UVLOCK-INSTALL-FAILED"
    assert sentinel.read_text(encoding="utf-8") == "VALUE = 1\n"


def test_build_isolation_failure_has_stable_diagnostic(tmp_path):
    project = _graph_project(tmp_path)
    env = _environment(tmp_path)
    pcc1 = tmp_path / "pcc1-build-isolation-fail"
    _fake_pcc1(
        pcc1,
        tmp_path / "build-isolation-count",
        fail=True,
        failure_diagnostic="PCC-PKG-ACQUIRE-BUILD-ISOLATION-UNSUPPORTED",
    )

    with pytest.raises(UvLockSyncError) as failure:
        sync_uv_lock(
            project / "uv.lock",
            project_root=project,
            pcc1=str(pcc1),
            environ=env,
        )

    assert failure.value.code == "PCC-PKG-UVLOCK-BUILD-ISOLATION-UNSUPPORTED"


def test_publish_failure_restores_complete_previous_environment(tmp_path, monkeypatch):
    project = _graph_project(tmp_path)
    env = _environment(tmp_path)
    environment_root = Path(str(resolve_package_environment(env)["root"]))
    site = environment_root / "site-packages"
    site.mkdir(parents=True)
    sentinel = site / "keep.py"
    sentinel.write_text("VALUE = 1\n", encoding="utf-8")
    old_manifest = environment_root / "installed.json"
    old_manifest.write_text('{"schema": "old"}\n', encoding="utf-8")
    pcc1 = tmp_path / "pcc1"
    _fake_pcc1(pcc1, tmp_path / "publish-failure-count")
    real_replace = uv_lock_sync.os.replace

    def fail_staging_publish(source, destination):
        source_path = Path(source)
        if (
            source_path.name.startswith(".pcc-sync-")
            and not source_path.name.endswith(".previous")
            and Path(destination) == environment_root
        ):
            raise OSError("forced atomic publish failure")
        return real_replace(source, destination)

    monkeypatch.setattr(uv_lock_sync.os, "replace", fail_staging_publish)
    with pytest.raises(UvLockSyncError) as failure:
        sync_uv_lock(
            project / "uv.lock",
            project_root=project,
            pcc1=str(pcc1),
            environ=env,
        )

    assert failure.value.code == "PCC-PKG-UVLOCK-PUBLISH-FAILED"
    assert sentinel.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert old_manifest.read_text(encoding="utf-8") == '{"schema": "old"}\n'
