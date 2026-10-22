"""Reinstalling the identical artifact must short-circuit, not rebuild.

`pcc1 -m pip install numpy` into an environment that already had the identical
sha256-pinned install redid everything — extract, copy, rescan every binary,
rebuild native sources — because nothing compared the resolved artifact
against the installed `pcc-package.json` manifest (measured at 168s,
PKG-P2-REINSTALL-FASTPATH).

The proof here is not a stopwatch. The second install runs with the expensive
stages replaced by functions that fail, so a fast path that silently stopped
working fails the test instead of just getting slower.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

import pcc.driver.cli_bootstrap as cli_bootstrap
import pcc.package.install as install_mod
from pcc1_gate import find_current_pcc1, repo_root, skip_or_fail_no_current_pcc1
from pcc.package.install import install_package


REPO_ROOT = repo_root()


def _write_wheel(path: Path, *, value: str = "1") -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("demo_pkg/__init__.py", f"VALUE = {value}\n")
        zf.writestr("demo_pkg-0.1.dist-info/METADATA", "Name: demo-pkg\n")
    return path


def _install(wheel: Path, tmp_path: Path, **kwargs):
    return install_package(
        str(wheel),
        target_dir=str(tmp_path / "site"),
        cache_dir=str(tmp_path / "cache"),
        **kwargs,
    )


@pytest.fixture
def explode(monkeypatch):
    """Make every stage the fast path is supposed to skip fail loudly."""

    def _arm():
        for name in ("_copy_or_extract", "linkage_report", "_ensure_meson_build_outputs"):
            monkeypatch.setattr(
                install_mod, name,
                lambda *a, _n=name, **k: pytest.fail(f"{_n} ran on a satisfied reinstall"),
            )

    return _arm


def test_first_install_records_the_artifact_digest(tmp_path):
    wheel = _write_wheel(tmp_path / "demo_pkg-0.1-py3-none-any.whl")
    result = _install(wheel, tmp_path)
    assert result["ok"], result
    assert result["install_action"] == "installed"
    assert len(result["artifact_sha256"]) == 64, result["artifact_sha256"]

    manifest = json.loads(
        Path(result["manifest_path"]).read_text(encoding="utf-8")
    )
    assert manifest["artifact_sha256"] == result["artifact_sha256"]


def test_identical_reinstall_is_already_satisfied(tmp_path, explode):
    wheel = _write_wheel(tmp_path / "demo_pkg-0.1-py3-none-any.whl")
    first = _install(wheel, tmp_path)
    assert first["install_action"] == "installed"

    explode()
    second = _install(wheel, tmp_path)
    assert second["ok"], second
    assert second["install_action"] == "already-satisfied"
    assert second["artifact_sha256"] == first["artifact_sha256"]
    assert second["installed_path"] == first["installed_path"]


def test_force_reinstalls_even_when_satisfied(tmp_path):
    wheel = _write_wheel(tmp_path / "demo_pkg-0.1-py3-none-any.whl")
    _install(wheel, tmp_path)
    again = _install(wheel, tmp_path, force=True)
    assert again["install_action"] == "installed", again


def test_a_changed_artifact_is_not_satisfied(tmp_path):
    """Same name and filename, different bytes — the digest is what decides."""
    wheel = _write_wheel(tmp_path / "demo_pkg-0.1-py3-none-any.whl")
    first = _install(wheel, tmp_path)

    _write_wheel(wheel, value="2")
    second = _install(wheel, tmp_path)
    assert second["install_action"] == "installed", second
    assert second["artifact_sha256"] != first["artifact_sha256"]


def test_a_different_abi_mode_is_not_satisfied(tmp_path):
    wheel = _write_wheel(tmp_path / "demo_pkg-0.1-py3-none-any.whl")
    _install(wheel, tmp_path, abi="pcc-native")
    other = _install(wheel, tmp_path, abi="cpython-compat")
    assert other["install_action"] == "installed", other


def test_deleted_payload_is_not_satisfied(tmp_path):
    """A manifest whose files are gone must not report success."""
    wheel = _write_wheel(tmp_path / "demo_pkg-0.1-py3-none-any.whl")
    first = _install(wheel, tmp_path)
    for payload in first["installed_payloads"]:
        shutil.rmtree(payload, ignore_errors=True)

    second = _install(wheel, tmp_path)
    assert second["install_action"] == "installed", second


def test_directory_sources_never_take_the_fast_path(tmp_path):
    """A source tree has no cheap exact digest, so it always reinstalls."""
    src = tmp_path / "demo_pkg_src"
    (src / "demo_pkg").mkdir(parents=True)
    (src / "demo_pkg" / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")

    first = _install(src, tmp_path)
    assert first["artifact_sha256"] is None, first["artifact_sha256"]
    second = _install(src, tmp_path)
    assert second["install_action"] == "installed", second


@pytest.fixture
def native_installer(monkeypatch, tmp_path):
    """A deterministic pcc1 installer substrate with observable heavy stages."""

    monkeypatch.setattr(
        cli_bootstrap.os,
        "_pcc_sha256_file_hex",
        lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        raising=False,
    )
    monkeypatch.setattr(
        cli_bootstrap,
        "_native_prepare_install_source_tree",
        lambda source, cache, name: [source, None],
    )
    monkeypatch.setattr(
        cli_bootstrap,
        "_native_build_install_source_json",
        lambda name, source, abi, mode: (
            '{"build_mode_requested": "owned", '
            '"build_ownership": "not-attempted", "ok": true}'
        ),
    )

    def _publish(source, target, name):
        root = Path(target) / name
        root.mkdir(parents=True, exist_ok=True)
        (root / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
        return str(root)

    monkeypatch.setattr(
        cli_bootstrap, "_native_install_importable_payload", _publish
    )
    monkeypatch.setattr(
        cli_bootstrap, "_native_linkage_edges_for_root", lambda root: []
    )

    def _install_native(wheel, *, abi="pcc-native", force=False):
        return json.loads(
            cli_bootstrap._native_install_manifest_json(
                str(wheel),
                str(tmp_path / "native-site"),
                str(tmp_path / "native-cache"),
                [],
                abi,
                [],
                None,
                "owned",
                force,
            )
        )

    return _install_native


def _explode_native_installer_stages(monkeypatch):
    def _fail(*args, **kwargs):
        pytest.fail("pcc1 expensive install stage ran on a satisfied reinstall")

    for name in (
        "_native_prepare_install_source_tree",
        "_native_build_install_source_json",
        "_native_install_importable_payload",
        "_native_linkage_edges_for_root",
    ):
        monkeypatch.setattr(cli_bootstrap, name, _fail)


def test_pcc1_identical_reinstall_structurally_skips_expensive_stages(
    tmp_path, monkeypatch, native_installer
):
    wheel = _write_wheel(tmp_path / "demo_pkg-0.1-py3-none-any.whl")
    first = native_installer(wheel)
    assert first["install_action"] == "installed", first
    assert len(first["artifact_sha256"]) == 64

    _explode_native_installer_stages(monkeypatch)
    second = native_installer(wheel)
    assert second["ok"], second
    assert second["install_action"] == "already-satisfied"
    assert second["artifact_sha256"] == first["artifact_sha256"]
    assert second["installed_path"] == first["installed_path"]


def test_pcc1_fastpath_rejects_changed_bytes_and_different_abi(
    tmp_path, native_installer
):
    wheel = _write_wheel(tmp_path / "demo_pkg-0.1-py3-none-any.whl")
    first = native_installer(wheel)

    _write_wheel(wheel, value="2")
    changed = native_installer(wheel)
    assert changed["install_action"] == "installed", changed
    assert changed["artifact_sha256"] != first["artifact_sha256"]

    other_abi = native_installer(wheel, abi="cpython-compat")
    assert other_abi["install_action"] == "installed", other_abi


def test_pcc1_fastpath_rejects_deleted_payload_and_force(
    tmp_path, native_installer
):
    wheel = _write_wheel(tmp_path / "demo_pkg-0.1-py3-none-any.whl")
    first = native_installer(wheel)
    shutil.rmtree(first["installed_path"])
    restored = native_installer(wheel)
    assert restored["install_action"] == "installed", restored

    forced = native_installer(wheel, force=True)
    assert forced["install_action"] == "installed", forced


def test_pcc1_directory_sources_do_not_claim_an_exact_digest(
    tmp_path, native_installer
):
    source = tmp_path / "native-source"
    source.mkdir()
    (source / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    first = native_installer(source)
    second = native_installer(source)
    assert first["artifact_sha256"] is None
    assert second["install_action"] == "installed", second


@pytest.mark.parametrize(
    "entry,args",
    [
        (
            "direct",
            ["demo.whl", "--target", "/tmp/site", "--force", "--json"],
        ),
        (
            "pip",
            [
                "install",
                "demo.whl",
                "--target",
                "/tmp/site",
                "--no-index",
                "--force",
                "--json",
            ],
        ),
    ],
)
def test_pcc1_cli_entrypoints_forward_force(monkeypatch, entry, args):
    observed = []

    def _install(*call_args):
        observed.append(call_args)
        return '{"ok": true}'

    monkeypatch.setattr(cli_bootstrap, "_native_install_manifest_json", _install)
    monkeypatch.setattr(cli_bootstrap, "_write_text", lambda *a, **k: None)
    monkeypatch.setattr(
        cli_bootstrap,
        "_native_resolve_install_source_result",
        lambda *a, **k: ["/tmp/demo.whl", "direct"],
    )

    if entry == "direct":
        rc = cli_bootstrap._run_native_package_install_from_pcc1(args)
    else:
        rc = cli_bootstrap._run_native_pip_shim_from_pcc1(args)
    assert rc == 0
    assert observed
    assert observed[-1][-1] is True


def test_current_pcc1_binary_reinstall_reports_noop_and_force_reinstalls(tmp_path):
    """The compiled pcc1 entrypoint, not only its host-imported mirror, agrees."""
    pcc1 = find_current_pcc1(REPO_ROOT)
    if pcc1 is None:
        skip_or_fail_no_current_pcc1(
            "no current pcc1 binary with the native reinstall fast path"
        )
    wheel = _write_wheel(tmp_path / "demo_pkg-0.1-py3-none-any.whl")
    site = tmp_path / "pcc1-site"
    cache = tmp_path / "pcc1-cache"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_HOST_PYTHON"] = "/usr/bin/false"
    command = [
        str(pcc1),
        "-m",
        "pip",
        "install",
        str(wheel),
        "--target",
        str(site),
        "--cache-dir",
        str(cache),
        "--no-index",
        "--json",
    ]

    first = subprocess.run(
        command, check=True, text=True, capture_output=True, timeout=180, env=env
    )
    second = subprocess.run(
        command, check=True, text=True, capture_output=True, timeout=60, env=env
    )
    forced = subprocess.run(
        command[:-1] + ["--force", "--json"],
        check=True,
        text=True,
        capture_output=True,
        timeout=180,
        env=env,
    )

    first_install = json.loads(first.stdout)["installs"][0]
    second_install = json.loads(second.stdout)["installs"][0]
    forced_install = json.loads(forced.stdout)["installs"][0]
    assert first_install["install_action"] == "installed"
    assert second_install["install_action"] == "already-satisfied"
    assert second_install["artifact_sha256"] == first_install["artifact_sha256"]
    assert forced_install["install_action"] == "installed"
