from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from pcc.driver import cli_bootstrap
from pcc.package import build_exec
from pcc.package import install
from pcc.package.pip_shim import _parse_install_args


def _meson_source(root: Path) -> Path:
    root.mkdir()
    (root / "meson.build").write_text("project('demo', 'c')\n", encoding="utf-8")
    return root


def _vendored_meson_entry(root: Path) -> Path:
    entry = root / "vendored-meson" / "meson" / "meson.py"
    entry.parent.mkdir(parents=True)
    entry.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "builddir = Path(sys.argv[2])\n"
        "builddir.mkdir(parents=True, exist_ok=True)\n"
        "(builddir / 'build.ninja').write_text('rule cc\\n  command = cc -c $in -o $out\\n')\n",
        encoding="utf-8",
    )
    return entry


def _existing_payload(root: Path) -> Path:
    root.mkdir()
    (root / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "_native.so").write_text("pcc native fixture\n", encoding="utf-8")
    return root


def _install_owned_digest_seam(monkeypatch):
    monkeypatch.setattr(
        cli_bootstrap.os,
        "_pcc_sha256_file_hex",
        lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        raising=False,
    )


def test_pip_build_mode_defaults_owned_and_rejects_unknown_mode():
    owned = _parse_install_args(["install", "demo"])
    host = _parse_install_args(["install", "demo", "--build=host"])
    invalid = _parse_install_args(["install", "demo", "--build=ambient"])

    assert owned["build_mode"] == "owned"
    assert host["build_mode"] == "host"
    assert invalid == {
        "ok": False,
        "command": "install",
        "error": "PCC-PKG-BUILD-MODE-INVALID",
        "diagnostic": "PCC-PKG-BUILD-MODE-INVALID",
    }


def test_host_installer_owned_meson_boundary_stops_before_any_process(
    tmp_path, monkeypatch
):
    project = _meson_source(tmp_path / "demo")

    def unexpected(*args, **kwargs):
        raise AssertionError("owned build boundary started a host build helper")

    monkeypatch.setattr(install.subprocess, "run", unexpected)
    monkeypatch.setattr(install, "_build_requirement_tool_wrappers", unexpected)

    report = install._ensure_meson_build_outputs(
        project, (), build_mode="owned"
    )

    assert report["ok"] is False
    assert report["diagnostics"] == ["PCC-PKG-OWNED-BUILD-TOOL-REQUIRED"]
    assert report["build_ownership"] == "owned-unavailable"
    assert report["host_assisted"] is False
    assert report["host_python"] is None
    assert report["host_free_build_claim"] is False
    assert report["actions"] == []


def test_pcc1_owned_meson_compiles_and_runs_with_the_native_stage(
    tmp_path, monkeypatch
):
    project = _meson_source(tmp_path / "demo")
    meson_source = _vendored_meson_entry(project)
    compiler = tmp_path / "pcc1"
    ninja = tmp_path / "ninja"
    compiler.write_text("native compiler fixture\n", encoding="utf-8")
    ninja.write_text("native ninja fixture\n", encoding="utf-8")
    compiler.chmod(0o755)
    ninja.chmod(0o755)
    calls = []

    def fake_run(command, *, check=False):
        assert check is True
        command = list(command)
        calls.append(command)
        if command[:2] == ["mkdir", "-p"]:
            Path(command[2]).mkdir(parents=True, exist_ok=True)
        elif command[0] == str(compiler):
            output = Path(command[command.index("-o") + 1])
            output.write_text("native meson fixture\n", encoding="utf-8")
            output.chmod(0o755)
        elif command[0].endswith("/owned-tools/meson"):
            build_dir = Path(command[2])
            build_dir.mkdir(parents=True, exist_ok=True)
            (build_dir / "build.ninja").write_text(
                "rule cc\n  command = cc -c $in -o $out\n",
                encoding="utf-8",
            )
        elif command[0].endswith("/owned-tools/pcc-package-build-exec"):
            report_path = Path(command[command.index("--report") + 1])
            report_path.write_text(
                json.dumps(
                    {
                        "ok": True,
                        "build_backend": "pcc-native-build-exec",
                        "build_mode_requested": "owned",
                        "build_ownership": "owned",
                        "host_assisted": False,
                        "host_python": None,
                        "host_free_build_claim": True,
                    }
                ),
                encoding="utf-8",
            )
        elif command[0] == str(ninja):
            pass
        else:
            raise AssertionError("unexpected owned build command: " + repr(command))

    monkeypatch.setattr(
        cli_bootstrap, "_native_bootstrap_executable", lambda: str(compiler)
    )
    _install_owned_digest_seam(monkeypatch)
    monkeypatch.setattr(
        cli_bootstrap,
        "_native_find_tool_path",
        lambda names, _paths: str(ninja) if "ninja" in names else None,
    )
    monkeypatch.setattr(cli_bootstrap, "_bootstrap_subprocess_run", fake_run)

    report = json.loads(
        cli_bootstrap._native_build_install_source_json(
            "demo", str(project), "pcc-native", "owned"
        )
    )

    assert report["ok"] is True
    assert report["diagnostics"] == []
    assert report["build_mode_requested"] == "owned"
    assert report["build_ownership"] == "owned"
    assert report["host_assisted"] is False
    assert report["host_python"] is None
    assert report["host_free_build_claim"] is True
    assert report["meson_source"] == str(meson_source)
    assert [action["kind"] for action in report["actions"]] == [
        "owned_meson_compile",
        "owned_meson_setup",
        "owned_build_exec_compile",
        "owned_meson_target_replay",
    ]
    flattened = " ".join(token for command in calls for token in command)
    assert "python3" not in flattened
    assert "uv run" not in flattened
    assert str(compiler) in calls[1]


def test_owned_meson_receipt_binds_compiler_tool_and_source_closure(
    tmp_path, monkeypatch
):
    project = _meson_source(tmp_path / "demo")
    meson_source = _vendored_meson_entry(project)
    sibling = meson_source.parent / "mesonbuild" / "core.py"
    sibling.parent.mkdir()
    sibling.write_text("VALUE = 1\n", encoding="utf-8")
    compiler = tmp_path / "pcc1"
    tool = tmp_path / "meson"
    compiler.write_bytes(b"compiler-v1")
    tool.write_bytes(b"tool-v1")
    _install_owned_digest_seam(monkeypatch)

    expected = cli_bootstrap._native_owned_meson_receipt_text(
        str(compiler), str(meson_source), str(tool)
    )
    receipt = tmp_path / "receipt"
    receipt.write_text(expected, encoding="utf-8")

    assert expected.startswith("pcc-owned-meson-receipt-v2\n")
    assert "source-count=2\n" in expected
    assert cli_bootstrap._native_owned_meson_receipt_matches(
        str(receipt), expected
    )

    sibling.write_text("VALUE = 2\n", encoding="utf-8")
    changed = cli_bootstrap._native_owned_meson_receipt_text(
        str(compiler), str(meson_source), str(tool)
    )
    assert changed != expected
    assert not cli_bootstrap._native_owned_meson_receipt_matches(
        str(receipt), changed
    )
    receipt.write_text(expected[:-1], encoding="utf-8")
    assert not cli_bootstrap._native_owned_meson_receipt_matches(
        str(receipt), expected
    )


def test_pcc1_owned_meson_rejects_generated_host_python_before_ninja(
    tmp_path, monkeypatch
):
    project = _meson_source(tmp_path / "demo")
    _vendored_meson_entry(project)
    compiler = tmp_path / "pcc1"
    ninja = tmp_path / "ninja"
    compiler.write_text("native compiler fixture\n", encoding="utf-8")
    ninja.write_text("native ninja fixture\n", encoding="utf-8")
    compiler.chmod(0o755)
    ninja.chmod(0o755)
    calls = []

    def fake_run(command, *, check=False):
        assert check is True
        command = list(command)
        calls.append(command)
        if command[:2] == ["mkdir", "-p"]:
            Path(command[2]).mkdir(parents=True, exist_ok=True)
        elif command[0] == str(compiler):
            output = Path(command[command.index("-o") + 1])
            output.write_text("native meson fixture\n", encoding="utf-8")
            output.chmod(0o755)
        elif command[0].endswith("/owned-tools/meson"):
            build_dir = Path(command[2])
            build_dir.mkdir(parents=True, exist_ok=True)
            (build_dir / "build.ninja").write_text(
                "rule generate\n  command = python3 generator.py\n",
                encoding="utf-8",
            )
        elif command[0] == str(ninja):
            raise AssertionError("host-Python graph reached Ninja")

    monkeypatch.setattr(
        cli_bootstrap, "_native_bootstrap_executable", lambda: str(compiler)
    )
    monkeypatch.setattr(
        cli_bootstrap,
        "_native_find_tool_path",
        lambda names, _paths: str(ninja) if "ninja" in names else None,
    )
    monkeypatch.setattr(cli_bootstrap, "_bootstrap_subprocess_run", fake_run)
    _install_owned_digest_seam(monkeypatch)

    report = json.loads(
        cli_bootstrap._native_build_install_source_json(
            "demo", str(project), "pcc-native", "owned"
        )
    )

    assert report["ok"] is False
    assert report["diagnostics"] == [
        "PCC-PKG-OWNED-BUILD-GRAPH-HOST-PYTHON"
    ]
    assert report["host_free_build_claim"] is False
    assert all(command[0] != str(ninja) for command in calls)


def test_owned_meson_graph_scan_ignores_python_header_arguments(tmp_path):
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    (build_dir / "build.ninja").write_text(
        "rule cc\n"
        "  command = cc -I/opt/pcc/python/include -include Python.h "
        "-c $in -o $out\n",
        encoding="utf-8",
    )

    assert cli_bootstrap._native_meson_graph_host_python_command(
        str(build_dir)
    ) is None


def test_native_build_exec_owned_mode_rejects_host_python_graph(tmp_path):
    project = _meson_source(tmp_path / "demo")
    build_dir = project / "build" / "pcc-package" / "meson-build"
    build_dir.mkdir(parents=True)
    (build_dir / "build.ninja").write_text(
        "rule generate\n  command = /usr/bin/env python3 generator.py\n",
        encoding="utf-8",
    )

    report = build_exec.execute_eager_meson_extensions(
        "demo", project, execute=True, build_mode="owned"
    )

    assert report["ok"] is False
    assert report["build_ownership"] == "owned"
    assert report["host_assisted"] is False
    assert report["host_python"] is None
    assert report["host_free_build_claim"] is False
    assert report["actions"] == []
    assert report["diagnostics"][0]["code"] == (
        "PCC-PKG-OWNED-BUILD-GRAPH-HOST-PYTHON"
    )


def test_host_owned_build_failure_does_not_publish_unbuilt_source(tmp_path):
    project = _meson_source(tmp_path / "demo")
    (project / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    site = tmp_path / "site"
    cache = tmp_path / "cache"

    report = install.install_package(
        str(project),
        target_dir=site,
        cache_dir=cache,
        build_source=True,
        build_mode="owned",
    )

    assert report["ok"] is False
    assert report["install_success"] is False
    assert report["build_report"]["diagnostics"] == [
        "PCC-PKG-OWNED-BUILD-TOOL-REQUIRED"
    ]
    assert not site.exists()
    assert not cache.exists()


def test_pcc1_owned_build_failure_does_not_publish_unbuilt_source(tmp_path):
    project = _meson_source(tmp_path / "demo")
    (project / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    site = tmp_path / "site"
    cache = tmp_path / "cache"

    report = json.loads(
        cli_bootstrap._native_install_manifest_json(
            str(project),
            str(site),
            str(cache),
            [],
            "pcc-native",
            [],
            None,
            "owned",
        )
    )

    assert report["ok"] is False
    assert report["install_success"] is False
    assert report["build_report"]["diagnostics"] == [
        "PCC-PKG-OWNED-BUILD-COMPILER-REQUIRED"
    ]
    assert not site.exists()
    assert not cache.exists()


def test_owned_mode_rejects_unproven_prebuilt_payload(tmp_path):
    payload = _existing_payload(tmp_path / "demo")

    report = json.loads(
        cli_bootstrap._native_build_install_source_json(
            "demo", str(payload), "pcc-native", "owned"
        )
    )

    assert report["ok"] is False
    assert report["build_ownership"] == "prebuilt-unverified"
    assert report["host_assisted"] is None
    assert report["host_free_build_claim"] is False
    assert report["diagnostics"] == ["PCC-PKG-BUILD-PROVENANCE-UNVERIFIED"]


def test_owned_mode_rejects_source_tree_self_attested_owned_payload(tmp_path):
    payload = _existing_payload(tmp_path / "demo")
    (payload / "pcc-package.json").write_text(
        json.dumps(
            {
                "ok": True,
                "build_report": {
                    "ok": True,
                    "build_ownership": "owned",
                    "host_assisted": False,
                    "host_python": None,
                    "host_free_build_claim": True,
                },
            }
        ),
        encoding="utf-8",
    )

    report = json.loads(
        cli_bootstrap._native_build_install_source_json(
            "demo", str(payload), "pcc-native", "owned"
        )
    )

    assert report["ok"] is False
    assert report["build_backend"] == "existing"
    assert report["build_ownership"] == "prebuilt-unverified"
    assert report["host_assisted"] is None
    assert report["host_free_build_claim"] is False
    assert report["diagnostics"] == ["PCC-PKG-BUILD-PROVENANCE-UNVERIFIED"]

    host_report = install._existing_payload_build_report(
        payload, build_mode="host"
    )
    assert host_report is not None
    assert host_report["build_ownership"] == "prebuilt-unverified"
    assert host_report["host_assisted"] is None
    assert host_report["host_python"] is None
    assert host_report["host_free_build_claim"] is False


def test_owned_meson_source_never_bypasses_receipt_path_with_self_attestation(
    tmp_path, monkeypatch
):
    project = _meson_source(tmp_path / "demo")
    (project / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    (project / "_native.so").write_text("prebuilt bytes\n", encoding="utf-8")
    (project / "pcc-package.json").write_text(
        json.dumps(
            {
                "ok": True,
                "build_report": {
                    "ok": True,
                    "build_ownership": "owned",
                    "host_assisted": False,
                    "host_python": None,
                    "host_free_build_claim": True,
                },
            }
        ),
        encoding="utf-8",
    )
    calls = []

    def receipt_path(name, source):
        calls.append((name, source))
        return '{"ok": false, "reason": "receipt-path-probe"}'

    monkeypatch.setattr(
        cli_bootstrap, "_native_owned_meson_build_json", receipt_path
    )

    report = json.loads(
        cli_bootstrap._native_build_install_source_json(
            "demo", str(project), "pcc-native", "owned"
        )
    )

    assert calls == [("demo", str(project))]
    assert report == {"ok": False, "reason": "receipt-path-probe"}


def test_cached_host_build_keeps_host_provenance_and_cannot_enter_owned_mode(
    tmp_path,
):
    payload = _existing_payload(tmp_path / "demo")
    host_python = "/opt/build-python/bin/python3"
    (payload / "pcc-package.json").write_text(
        json.dumps(
            {
                "ok": True,
                "build_report": {
                    "ok": True,
                    "build_ownership": "host",
                    "host_assisted": True,
                    "host_python": host_python,
                    "host_free_build_claim": False,
                },
            }
        ),
        encoding="utf-8",
    )

    owned = install._existing_payload_build_report(payload, build_mode="owned")
    host = install._existing_payload_build_report(payload, build_mode="host")

    assert owned is not None
    assert owned["ok"] is False
    assert owned["diagnostics"] == ["PCC-PKG-BUILD-PROVENANCE-UNVERIFIED"]
    assert owned["build_ownership"] == "prebuilt-unverified"
    assert owned["host_python"] == host_python
    assert host is not None
    assert host["ok"] is True
    assert host["build_ownership"] == "host"
    assert host["host_assisted"] is True
    assert host["host_python"] == host_python
    assert host["host_free_build_claim"] is False


def test_explicit_host_build_report_names_the_interpreter(tmp_path, monkeypatch):
    project = _meson_source(tmp_path / "demo")
    build_dir = project / "build" / "pcc-package" / "meson-build"
    calls = []

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(install, "_build_requirement_tool_wrappers", lambda _: None)
    monkeypatch.setattr(
        install,
        "_meson_setup_command",
        lambda source, target, path: [sys.executable, "meson.py", "setup"],
    )
    monkeypatch.setattr(install.shutil, "which", lambda name, path=None: name)

    def completed(command, **kwargs):
        calls.append(command)
        if command[0] == sys.executable:
            build_dir.mkdir(parents=True, exist_ok=True)
            (build_dir / "build.ninja").write_text("", encoding="utf-8")
        return Completed()

    monkeypatch.setattr(install.subprocess, "run", completed)

    report = install._ensure_meson_build_outputs(project, (), build_mode="host")

    assert report["ok"] is True
    assert len(calls) == 2
    assert report["build_mode_requested"] == "host"
    assert report["build_ownership"] == "host"
    assert report["host_assisted"] is True
    assert report["host_python"] == sys.executable
    assert report["host_free_build_claim"] is False
