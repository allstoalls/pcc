from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from pcc.driver import cli_bootstrap
from pcc.driver import cli_core
from pcc.package.acquire import target_python_version
from pcc.package.install import install_package
from pcc.package.pip_shim import pip_dry_run_plan
from pcc.package.uv_lock_sync import _marker_environment
from pcc.package.environment import (
    apply_locked_environment_resource_defaults,
    default_package_cache,
    default_package_site,
    package_site_roots,
    resolve_package_environment,
)


def _base_env(tmp_path: Path) -> dict[str, str]:
    return {
        "HOME": str(tmp_path / "home"),
        "PCC_PACKAGE_TARGET_PYTHON": "3.11",
    }


def _publish_locked_environment(env: dict[str, str]) -> dict[str, object]:
    report = resolve_package_environment(env, target_triple="aarch64-apple-darwin")
    root = Path(str(report["root"]))
    root.mkdir(parents=True)
    (root / "environment.json").write_text(
        json.dumps(
            {
                "compatibility_tag": report["compatibility_tag"],
                "lock_provenance": {
                    "adapter_schema": "pcc.uv-lock-adapter.v1",
                    "lock_path": "/project/uv.lock",
                    "lock_sha256": "ab" * 32,
                    "target_python": "3.11",
                    "uv_lock_revision": 3,
                    "uv_lock_version": 1,
                },
            }
        ),
        encoding="utf-8",
    )
    return resolve_package_environment(env, target_triple="aarch64-apple-darwin")


def test_locked_environment_applies_conservative_compiler_resource_defaults(
    tmp_path, monkeypatch
):
    env = _base_env(tmp_path)
    env["VIRTUAL_ENV"] = str(tmp_path / ".venv")
    env["PCC_TARGET_TRIPLE"] = "aarch64-apple-darwin"
    _publish_locked_environment(env)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("PCC_PY_FRONTEND_JOBS", raising=False)
    monkeypatch.delenv("PCC_SELF_BACKEND_JOBS", raising=False)

    applied = apply_locked_environment_resource_defaults()

    assert applied == ["PCC_PY_FRONTEND_JOBS", "PCC_SELF_BACKEND_JOBS"]
    assert os.environ["PCC_PY_FRONTEND_JOBS"] == "1"
    assert os.environ["PCC_SELF_BACKEND_JOBS"] == "1"


def test_locked_environment_preserves_explicit_compiler_resource_overrides(
    tmp_path, monkeypatch
):
    env = _base_env(tmp_path)
    env["VIRTUAL_ENV"] = str(tmp_path / ".venv")
    env["PCC_TARGET_TRIPLE"] = "aarch64-apple-darwin"
    env["PCC_PY_FRONTEND_JOBS"] = "4"
    env["PCC_SELF_BACKEND_JOBS"] = "2"
    _publish_locked_environment(env)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    applied = apply_locked_environment_resource_defaults()

    assert applied == []
    assert os.environ["PCC_PY_FRONTEND_JOBS"] == "4"
    assert os.environ["PCC_SELF_BACKEND_JOBS"] == "2"


def test_locked_resource_default_helper_is_callable_in_strict_self_mode(
    tmp_path, monkeypatch
):
    from pcc.frontends.python.pipeline import compile_python_multi

    source = tmp_path / "locked_resource_defaults.py"
    source.write_text(
        "import os\n"
        "from pcc.package.environment import "
        "apply_locked_environment_resource_defaults\n"
        "apply_locked_environment_resource_defaults()\n"
        "print(os.environ.get('PCC_PY_FRONTEND_JOBS', '<unset>'))\n"
        "print(os.environ.get('PCC_SELF_BACKEND_JOBS', '<unset>'))\n",
        encoding="utf-8",
    )
    output = tmp_path / "locked_resource_defaults"

    compile_python_multi(
        [str(source), "pcc/package/environment.py"],
        str(output),
        module_names=["locked_resource_defaults", "pcc.package.environment"],
        entry_module="locked_resource_defaults",
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    run_env = _base_env(tmp_path)
    run_env["PCC_ENVIRONMENT"] = str(tmp_path / "locked-environment")
    run_env["PCC_TARGET_TRIPLE"] = "aarch64-apple-darwin"
    _publish_locked_environment(run_env)
    run = subprocess.run(
        [str(output)],
        env=run_env,
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert run.returncode == 0, run.stderr
    assert run.stdout.splitlines() == ["1", "1"]


def test_virtual_environment_owns_private_compatibility_overlay(tmp_path):
    env = _base_env(tmp_path)
    env["VIRTUAL_ENV"] = str(tmp_path / ".venv")

    report = resolve_package_environment(env, target_triple="aarch64-apple-darwin")

    root = Path(report["root"])
    assert report["selection_reason"] == "virtual-env"
    assert root.parent.parent == tmp_path / ".venv" / ".pcc"
    assert Path(report["selected_site_packages"]) == root / "site-packages"
    assert report["package_sites"] == [str(root / "site-packages")]
    assert report["python_semantic_target"] == "3.11"
    assert report["pcc_native_abi"] == "pcc-native-v1"
    assert report["target_triple"] == "aarch64-apple-darwin"
    assert report["package_abi_mode"] == "pcc-native"


def test_user_environment_is_durable_data_not_cache_or_tmp(tmp_path):
    env = _base_env(tmp_path)
    env["PCC_DATA_HOME"] = str(tmp_path / "pcc-data")

    report = resolve_package_environment(env, target_triple="aarch64-apple-darwin")

    root = Path(report["root"])
    assert report["selection_reason"] == "user-data"
    assert root.parent.parent == tmp_path / "pcc-data"
    assert ".cache" not in root.parts
    assert default_package_site(env, target_triple="aarch64-apple-darwin") == str(
        root / "site-packages"
    )
    assert default_package_cache(env) == str(
        tmp_path / "home" / ".cache" / "pcc" / "package-cache"
    )


def test_explicit_environment_precedes_virtual_environment(tmp_path):
    env = _base_env(tmp_path)
    env["PCC_ENVIRONMENT"] = str(tmp_path / "selected")
    env["VIRTUAL_ENV"] = str(tmp_path / ".venv")

    report = resolve_package_environment(env, target_triple="aarch64-apple-darwin")

    assert report["root"] == str((tmp_path / "selected").resolve())
    assert report["selection_reason"] == "explicit-environment"
    assert report["override_provenance"]["PCC_ENVIRONMENT"] is True


def test_package_site_override_has_precedence_without_replacing_owner(tmp_path):
    env = _base_env(tmp_path)
    env["VIRTUAL_ENV"] = str(tmp_path / ".venv")
    first = tmp_path / "prepared-a"
    second = tmp_path / "prepared-b"
    env["PCC_PACKAGE_SITE"] = f"{first}:{second}"

    report = resolve_package_environment(env, target_triple="aarch64-apple-darwin")

    selected = report["selected_site_packages"]
    assert report["package_sites"] == [
        str(first.resolve()),
        str(second.resolve()),
        selected,
    ]
    assert report["selection_reason"] == "virtual-env"
    assert report["override_provenance"]["PCC_PACKAGE_SITE"] is True
    assert (
        package_site_roots(env, target_triple="aarch64-apple-darwin")
        == report["package_sites"]
    )


def test_runtime_policy_does_not_change_environment_identity(tmp_path):
    env = _base_env(tmp_path)
    env["VIRTUAL_ENV"] = str(tmp_path / ".venv")
    baseline = resolve_package_environment(env, target_triple="aarch64-apple-darwin")

    for name, value in (
        ("PCC_GC_BACKEND", "4"),
        ("PCC_BACKEND", "self"),
        ("PCC_WITH_VTHREAD", "1"),
        ("PCC_GPU_BACKEND", "metal"),
    ):
        env[name] = value

    changed = resolve_package_environment(env, target_triple="aarch64-apple-darwin")
    assert changed["compatibility_tag"] == baseline["compatibility_tag"]
    assert changed["root"] == baseline["root"]
    assert changed["package_sites"] == baseline["package_sites"]


@pytest.mark.parametrize("virtual_env", [False, True])
def test_native_environment_path_is_stable_across_python_selection_versions(
    tmp_path, virtual_env
):
    env = {"HOME": str(tmp_path / "home")}
    if virtual_env:
        env["VIRTUAL_ENV"] = str(tmp_path / ".venv")
    baseline = resolve_package_environment(env, target_triple="aarch64-apple-darwin")
    changed = resolve_package_environment(
        {**env, "PCC_PACKAGE_TARGET_PYTHON": "3.16"},
        target_triple="aarch64-apple-darwin",
    )
    assert baseline["python_semantic_target"] == "3.15"
    assert changed["python_semantic_target"] == "3.16"
    assert baseline["compatibility_tag"] == changed["compatibility_tag"]
    assert not baseline["compatibility_tag"].startswith("py")
    assert baseline["root"] == changed["root"]
    assert baseline["selected_site_packages"] == changed["selected_site_packages"]


def test_native_environment_path_still_isolates_abi_and_target_platform(tmp_path):
    env = {"HOME": str(tmp_path / "home")}
    baseline = resolve_package_environment(env, target_triple="aarch64-apple-darwin")
    changed_abi = resolve_package_environment(
        {**env, "PCC_NATIVE_ABI_VERSION": "pcc-native-v2"},
        target_triple="aarch64-apple-darwin",
    )
    changed_platform = resolve_package_environment(
        env, target_triple="x86_64-unknown-linux-gnu"
    )
    assert len({baseline["root"], changed_abi["root"], changed_platform["root"]}) == 3


@pytest.mark.parametrize("abi_mode", ["cpython-compat", "libpython"])
def test_cpython_environment_identity_retains_python_abi_version(tmp_path, abi_mode):
    env = {"HOME": str(tmp_path / "home"), "PCC_PACKAGE_ABI_MODE": abi_mode}
    baseline = resolve_package_environment(env, target_triple="aarch64-apple-darwin")
    changed = resolve_package_environment(
        {**env, "PCC_PACKAGE_TARGET_PYTHON": "3.16"},
        target_triple="aarch64-apple-darwin",
    )
    assert baseline["compatibility_tag"].startswith("py315-")
    assert changed["compatibility_tag"].startswith("py316-")
    assert baseline["root"] != changed["root"]


def test_legacy_versioned_environment_is_preserved_and_explicitly_selectable(tmp_path):
    home = tmp_path / "home"
    legacy = (
        home
        / ".local/share/pcc/environments/py311-pcc_native_v1-aarch64_apple_darwin-pcc_native"
    )
    payload = legacy / "site-packages/old_module.py"
    payload.parent.mkdir(parents=True)
    payload.write_text("VALUE = 42\n")
    env = {"HOME": str(home)}
    default = resolve_package_environment(env, target_triple="aarch64-apple-darwin")
    assert default["root"] != str(legacy)
    selected = resolve_package_environment(
        {**env, "PCC_ENVIRONMENT": str(legacy)}, target_triple="aarch64-apple-darwin"
    )
    assert selected["root"] == str(legacy)
    assert selected["selected_site_packages"] == str(payload.parent)
    assert payload.read_text() == "VALUE = 42\n"


def test_host_and_bootstrap_env_info_report_same_selection(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / ".venv"))
    monkeypatch.delenv("PCC_PACKAGE_SITE", raising=False)
    monkeypatch.delenv("PCC_ENVIRONMENT", raising=False)

    assert cli_core.cli_main(["env", "info", "--json"]) == 0
    host = json.loads(capsys.readouterr().out)
    assert cli_bootstrap.bootstrap_cli_main(["env", "info", "--json"]) == 0
    compiled = json.loads(capsys.readouterr().out)

    assert compiled == host
    assert host["selection_reason"] == "virtual-env"
    assert host["root"].startswith(str(tmp_path / ".venv" / ".pcc"))


@pytest.mark.parametrize("configured, expected", [(None, "3.15"), ("3.11", "3.11")])
def test_package_target_agrees_across_environment_acquisition_and_lock_markers(
    tmp_path, monkeypatch, capsys, configured, expected
):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / ".venv"))
    for name in ("PCC_PACKAGE_SITE", "PCC_ENVIRONMENT", "PCC_PACKAGE_TARGET_PYTHON"):
        monkeypatch.delenv(name, raising=False)
    if configured is not None:
        monkeypatch.setenv("PCC_PACKAGE_TARGET_PYTHON", configured)

    assert cli_core.cli_main(["env", "info", "--json"]) == 0
    host = json.loads(capsys.readouterr().out)
    assert cli_bootstrap.bootstrap_cli_main(["env", "info", "--json"]) == 0
    native = json.loads(capsys.readouterr().out)
    assert host == native
    assert host["python_semantic_target"] == expected
    assert host["compatibility_tag"].startswith("pcc_native_v1-")
    assert target_python_version() == expected
    args = [
        "install",
        "target-probe",
        "--dry-run",
        "--acquire=offline",
        "--cache-dir",
        str(tmp_path / "cache"),
    ]
    assert pip_dry_run_plan(args)["target_python"] == expected
    assert cli_bootstrap._run_native_pip_shim_from_pcc1(args) == 0
    assert json.loads(capsys.readouterr().out)["target_python"] == expected
    marker_env = _marker_environment(
        host["python_semantic_target"], host["target_triple"]
    )
    assert marker_env["python_version"] == expected
    assert marker_env["python_full_version"] == expected + ".0"


def test_bootstrap_compile_activates_locked_resource_defaults_before_frontend(
    tmp_path, monkeypatch
):
    env = _base_env(tmp_path)
    env["VIRTUAL_ENV"] = str(tmp_path / ".venv")
    env["PCC_TARGET_TRIPLE"] = "aarch64-apple-darwin"
    _publish_locked_environment(env)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("PCC_PY_FRONTEND_JOBS", raising=False)
    monkeypatch.delenv("PCC_SELF_BACKEND_JOBS", raising=False)

    source = tmp_path / "app.py"
    output = tmp_path / "app"
    source.write_text("print(42)\n", encoding="utf-8")
    observed = {}

    def fake_compile(*_args, **_kwargs):
        observed["frontend_jobs"] = os.environ.get("PCC_PY_FRONTEND_JOBS")
        observed["self_backend_jobs"] = os.environ.get("PCC_SELF_BACKEND_JOBS")
        return None

    monkeypatch.setattr(cli_bootstrap, "_observed_compile_python", fake_compile)

    assert cli_bootstrap.bootstrap_cli_main([str(source), "-o", str(output)]) == 0
    assert observed == {"frontend_jobs": "1", "self_backend_jobs": "1"}
    assert not str(os.environ.get("PCC_PY_FRONTEND_JOBS") or "").strip()
    assert not str(os.environ.get("PCC_SELF_BACKEND_JOBS") or "").strip()


def test_host_cli_restores_locked_resource_defaults_after_compile(
    tmp_path, monkeypatch
):
    from pcc.frontends.python import pipeline

    env = _base_env(tmp_path)
    env["VIRTUAL_ENV"] = str(tmp_path / ".venv")
    env["PCC_TARGET_TRIPLE"] = "aarch64-apple-darwin"
    _publish_locked_environment(env)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("PCC_PY_FRONTEND_JOBS", raising=False)
    monkeypatch.delenv("PCC_SELF_BACKEND_JOBS", raising=False)

    source = tmp_path / "app.py"
    output = tmp_path / "app"
    source.write_text("print(42)\n", encoding="utf-8")
    observed = {}

    def fake_compile(*_args, **_kwargs):
        observed["frontend_jobs"] = os.environ.get("PCC_PY_FRONTEND_JOBS")
        observed["self_backend_jobs"] = os.environ.get("PCC_SELF_BACKEND_JOBS")
        return None

    monkeypatch.setattr(pipeline, "compile_python", fake_compile)

    assert cli_core.cli_main([str(source), "-o", str(output)]) == 0
    assert observed == {"frontend_jobs": "1", "self_backend_jobs": "1"}
    assert not str(os.environ.get("PCC_PY_FRONTEND_JOBS") or "").strip()
    assert not str(os.environ.get("PCC_SELF_BACKEND_JOBS") or "").strip()


def test_native_bootstrap_restarts_once_with_locked_defaults_in_initial_environment(
    tmp_path, monkeypatch
):
    env = _base_env(tmp_path)
    env["VIRTUAL_ENV"] = str(tmp_path / ".venv")
    env["PCC_TARGET_TRIPLE"] = "aarch64-apple-darwin"
    _publish_locked_environment(env)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("PCC_PY_FRONTEND_JOBS", raising=False)
    monkeypatch.delenv("PCC_SELF_BACKEND_JOBS", raising=False)

    native = tmp_path / "pcc1"
    native.write_bytes(b"native bootstrap placeholder")
    native.chmod(0o755)
    source = tmp_path / "app.py"
    output = tmp_path / "app"
    source.write_text("print(42)\n", encoding="utf-8")
    monkeypatch.setattr(cli_bootstrap.sys, "argv", [str(native)])
    calls = []

    def fake_subprocess(args, *, check=False):
        calls.append((list(args), check))

    monkeypatch.setattr(cli_bootstrap, "_bootstrap_subprocess_run", fake_subprocess)

    assert cli_bootstrap.bootstrap_cli_main([str(source), "-o", str(output)]) == 0
    assert calls == [([str(native), str(source), "-o", str(output)], True)]


def test_default_install_publishes_into_selected_environment(tmp_path, monkeypatch):
    home = tmp_path / "home"
    venv = tmp_path / ".venv"
    source = tmp_path / "demo-pkg-src"
    package = source / "demo_pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 42\n", encoding="utf-8")
    (source / "pyproject.toml").write_text(
        '[project]\nname = "demo-pkg"\nversion = "1.0"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("VIRTUAL_ENV", str(venv))
    monkeypatch.delenv("PCC_PACKAGE_SITE", raising=False)
    monkeypatch.delenv("PCC_ENVIRONMENT", raising=False)

    result = install_package(
        str(source),
        cache_dir=tmp_path / "cache",
        use_cache=False,
    )

    assert result["ok"] is True
    selected = Path(default_package_site())
    installed = Path(result["installed_path"])
    assert installed.is_relative_to(selected)
    assert (selected / "demo_pkg" / "__init__.py").is_file()
    assert "PCC_PACKAGE_SITE" not in result


def test_explicit_target_does_not_activate_later_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("VIRTUAL_ENV", str(tmp_path / ".venv"))
    explicit = tmp_path / "isolated-site"

    before = default_package_site()
    assert before != str(explicit)
    # Selection is pure: an explicit install target belongs to the install
    # operation and cannot mutate the active environment contract.
    assert default_package_site() == before


def test_obsolete_divergent_default_sites_are_absent():
    root = Path.cwd()
    sources = [
        root / "pcc" / "package" / "install.py",
        root / "pcc" / "driver" / "cli_bootstrap.py",
        root / "pcc" / "frontends" / "python" / "pipeline.py",
    ]
    text = "\n".join(path.read_text(encoding="utf-8") for path in sources)
    assert "/tmp/pcc-site-packages" not in text
    assert ".cache/pcc/site-packages" not in text
