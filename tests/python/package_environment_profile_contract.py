"""Shared contract: runtime-profile switches reuse one package environment.

Used by tests/python/test_package_runtime_profile_environment.py, the
tests/python/gc/test_package_environment_gc{0..4}.py per-backend gates,
tests/vthread/test_package_environment_profile.py, and
tests/kernel/test_package_gpu_capabilities.py.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

from pcc.package.uv_lock_sync import sync_uv_lock
from pcc.package.environment import resolve_package_environment

IDENTITY_FIELDS = (
    "root",
    "compatibility_tag",
    "selected_site_packages",
    "package_sites",
    "cache_root",
    "python_semantic_target",
    "pcc_native_abi",
    "package_abi_mode",
    "target_triple",
)


def base_environment(tmp_path: Path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path / "home"),
        "VIRTUAL_ENV": str(tmp_path / "project" / ".venv"),
        "PCC_PACKAGE_TARGET_PYTHON": "3.11",
        "PCC_TARGET_TRIPLE": "aarch64-apple-darwin",
        "PCC_PACKAGE_CACHE": str(tmp_path / "cache"),
    }


def write_locked_project(tmp_path: Path) -> Path:
    """One project with one locked local directory dependency (dep-a)."""

    project = tmp_path / "project"
    dep = project / "deps" / "dep-a"
    (dep / "dep_a").mkdir(parents=True)
    (dep / "pyproject.toml").write_text(
        '[project]\nname = "dep-a"\nversion = "1.0.0"\n', encoding="utf-8"
    )
    (dep / "dep_a" / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (project / "pyproject.toml").write_text(
        '[project]\nname = "demo-project"\nversion = "0.0.0"\n', encoding="utf-8"
    )
    (project / "uv.lock").write_text(
        """version = 1
revision = 3
requires-python = ">=3.11"

[[package]]
name = "demo-project"
version = "0.0.0"
source = { editable = "." }
dependencies = [{ name = "dep-a" }]

[[package]]
name = "dep-a"
version = "1.0.0"
source = { directory = "deps/dep-a" }
""",
        encoding="utf-8",
    )
    return project


def write_fake_pcc1(tmp_path: Path) -> tuple[Path, Path]:
    """Counting installer stub with the pcc1 install CLI shape."""

    counter = tmp_path / "pcc1-count"
    pcc1 = tmp_path / "pcc1"
    script = f"""#!{sys.executable}
import json
from pathlib import Path
import shutil
import sys

counter = Path({str(counter)!r})
counter.write_text(str(int(counter.read_text() if counter.exists() else "0") + 1))
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
    else:
        shutil.copy2(child, destination)
print(json.dumps({{"ok": True, "installs": [{{"build_report": {{"skipped": True}}}}]}}))
"""
    pcc1.write_text(script, encoding="utf-8")
    pcc1.chmod(0o755)
    return pcc1, counter


def identity_fields(report: dict[str, object]) -> dict[str, object]:
    return {name: report[name] for name in IDENTITY_FIELDS}


def artifact_digests(packages: list[dict[str, object]]) -> list[tuple]:
    return [
        (row["name"], row["version"], row["artifact_sha256"], row["build_key"])
        for row in packages
    ]


def assert_profile_environment_invariance(
    tmp_path: Path, profile: dict[str, str]
) -> None:
    """Identity, digest, and zero-work invariance under ``profile``.

    Syncs once under the plain environment, then re-syncs with the
    runtime-policy overlay applied: identity fields, sync key, and
    installed-artifact digests must be identical, and the switch must
    perform zero downloads, zero native builds, and zero installer calls.
    """

    project = write_locked_project(tmp_path)
    env = base_environment(tmp_path)
    profiled_env = dict(env)
    profiled_env.update(profile)

    assert identity_fields(resolve_package_environment(profiled_env)) == (
        identity_fields(resolve_package_environment(env))
    )

    pcc1, counter = write_fake_pcc1(tmp_path)
    first = sync_uv_lock(
        project / "uv.lock", project_root=project, pcc1=str(pcc1), environ=env
    )
    second = sync_uv_lock(
        project / "uv.lock",
        project_root=project,
        pcc1=str(pcc1),
        environ=profiled_env,
    )

    assert first["changed"] is True
    assert second["changed"] is False
    assert second["downloads"] == 0
    assert second["native_builds"] == 0
    assert second["sync_key"] == first["sync_key"]
    assert second["environment_root"] == first["environment_root"]
    assert artifact_digests(second["packages"]) == artifact_digests(
        first["packages"]
    )
    assert counter.read_text(encoding="utf-8") == "1"

    installed = json.loads(
        (Path(str(first["environment_root"])) / "installed.json").read_text(
            encoding="utf-8"
        )
    )
    assert installed["sync_key"] == first["sync_key"]
    assert artifact_digests(installed["packages"]) == artifact_digests(
        first["packages"]
    )
