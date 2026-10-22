"""A Python package's optional C source data is not an extension build target."""

import json
import tarfile
import zipfile
import pytest

from pcc.driver import cli_bootstrap
from pcc.package.pip_shim import pip_install_plan
from pcc.package.schema import declarative_python_source_build


def project(tmp_path, *, hook=False, with_c=True):
    source = tmp_path / "source"
    package = source / "example_tools"
    native = package / "native"
    native.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 42\n")
    if with_c:
        (native / "optional_provider.c").write_text(
            "#error this optional provider is package data, not a Python extension\n"
        )
    config = (
        '[project]\nname="example-tools"\nversion="1.2.3"\n'
        '[build-system]\nrequires=["hatchling"]\nbuild-backend="hatchling.build"\n'
        '[tool.hatch.build.targets.wheel]\npackages=["example_tools"]\n'
    )
    if hook:
        config += "[tool.hatch.build.hooks.custom]\n"
    (source / "pyproject.toml").write_text(config)
    return source


def test_native_installer_retains_optional_c_data_without_compiling_it(tmp_path, monkeypatch):
    source = project(tmp_path)

    def unexpected_compile(*args, **kwargs):
        raise AssertionError("optional package data was treated as an extension")

    monkeypatch.setattr(cli_bootstrap, "_native_build_exec_json", unexpected_compile)
    result = json.loads(
        cli_bootstrap._native_install_manifest_json(
            str(source), str(tmp_path / "site"), str(tmp_path / "cache"), [], "pcc-native"
        )
    )
    assert result["ok"] is True, result
    assert result["name"] == "example-tools"
    assert result["build_report"]["reason"] == "declarative_python_source"
    assert result["build_report"]["actions"] == []
    assert (tmp_path / "site/example_tools/native/optional_provider.c").is_file()


def test_host_installer_uses_the_same_source_only_build_contract(tmp_path):
    source = project(tmp_path)
    (source / "demo_app.py").write_text("print('a project example, not a package root')\n")
    (source / "examples").mkdir()
    (source / "examples/demo.py").write_text("print('another example')\n")
    result = pip_install_plan(
        [
            "install",
            str(source),
            "--target",
            str(tmp_path / "site"),
            "--cache-dir",
            str(tmp_path / "cache"),
        ]
    )
    assert result["ok"] is True, result
    assert result["installs"][0]["name"] == "example-tools"
    report = result["installs"][0]["build_report"]
    assert report["reason"] == "declarative_python_source"
    assert report["actions"] == []
    assert (tmp_path / "site/example_tools/__init__.py").read_text() == "VALUE = 42\n"


def test_hatch_build_hook_is_not_silently_skipped(tmp_path, monkeypatch):
    source = project(tmp_path, hook=True)
    calls = []

    def explicit_native_build(*args, **kwargs):
        calls.append(args)
        return '{"ok": false, "reason": "test_build_required"}'

    monkeypatch.setattr(cli_bootstrap, "_native_build_exec_json", explicit_native_build)
    report = json.loads(
        cli_bootstrap._native_build_install_source_json("example-tools", str(source), "pcc-native")
    )
    assert not calls
    assert report["ok"] is False
    assert report["reason"] == "declared_build_hook_requires_owner"


@pytest.mark.parametrize("build_mode", ["owned", "host"])
def test_python_generation_hook_without_c_files_fails_before_publication(tmp_path, build_mode):
    source = project(tmp_path, hook=True, with_c=False)
    site = tmp_path / "site"
    cache = tmp_path / "cache"
    native = json.loads(
        cli_bootstrap._native_install_manifest_json(
            str(source), str(site), str(cache), [], "pcc-native", build_mode=build_mode
        )
    )
    host = pip_install_plan(
        [
            "install",
            str(source),
            "--target",
            str(site),
            "--cache-dir",
            str(cache),
            "--build",
            build_mode,
        ]
    )
    for result in (native, host["installs"][0]):
        assert result["ok"] is False, result
        assert result["build_report"]["skipped"] is False
        assert result["build_report"]["reason"] == "declared_build_hook_requires_owner"
    assert not site.exists()


@pytest.mark.parametrize("surface", ["host", "native"])
@pytest.mark.parametrize("build_mode", ["owned", "host"])
def test_unrecognized_generation_backend_fails_before_publication(tmp_path, surface, build_mode):
    source = project(tmp_path, with_c=False)
    config = source / "pyproject.toml"
    config.write_text(config.read_text().replace("hatchling", "generator_backend"))
    (source / "example_tools/__init__.py").write_text("from .generated import VALUE\n")
    site = tmp_path / "site"
    cache = tmp_path / "cache"
    if surface == "native":
        result = json.loads(
            cli_bootstrap._native_install_manifest_json(
                str(source), str(site), str(cache), [], "pcc-native", build_mode=build_mode
            )
        )
    else:
        plan = pip_install_plan(
            [
                "install",
                str(source),
                "--target",
                str(site),
                "--cache-dir",
                str(cache),
                "--build",
                build_mode,
            ]
        )
        result = plan["installs"][0]
    assert result["ok"] is False, result
    assert result["build_report"]["skipped"] is False
    assert not site.exists()


@pytest.mark.parametrize("kind", ["zip", "tar"])
@pytest.mark.parametrize("hook", [True, False])
def test_source_archive_build_policy_runs_before_publication(tmp_path, kind, hook):
    source = project(tmp_path, hook=hook, with_c=False)
    archive = tmp_path / (
        "example-tools-1.2.3.zip" if kind == "zip" else "example-tools-1.2.3.tar.gz"
    )
    if kind == "zip":
        with zipfile.ZipFile(archive, "w") as output:
            for path in sorted(source.rglob("*")):
                if path.is_file():
                    output.write(path, "example-tools-1.2.3/" + str(path.relative_to(source)))
    else:
        with tarfile.open(archive, "w:gz") as output:
            output.add(source, arcname="example-tools-1.2.3")
    native_site = tmp_path / "native-site"
    native = json.loads(
        cli_bootstrap._native_install_manifest_json(
            str(archive), str(native_site), str(tmp_path / "native-cache"), [], "pcc-native"
        )
    )
    host_site = tmp_path / "host-site"
    host = pip_install_plan(
        [
            "install",
            str(archive),
            "--target",
            str(host_site),
            "--cache-dir",
            str(tmp_path / "host-cache"),
        ]
    )
    result = host["installs"][0]
    for installed, site in ((native, native_site), (result, host_site)):
        assert installed["ok"] is not hook, installed
        if hook:
            assert installed["build_report"]["skipped"] is False
            assert installed["build_report"]["reason"] == "declared_build_hook_requires_owner"
            assert not site.exists()
        else:
            assert installed["build_report"]["reason"] == "declarative_python_source"
            assert (site / "example_tools/__init__.py").read_text() == "VALUE = 42\n"
            assert installed["source_path"] == str(archive)


@pytest.mark.parametrize(
    "config",
    [
        '[build-system]\nbuild-backend="hatchling.build"\nbackend-path=["backend"]\n',
        '[build-system]\nbuild-backend="hatchling.build"\n[tool.hatch.metadata.hooks.custom]\n',
        '[build-system]\nbuild-backend="hatchling.build"\n[tool.hatch.build]\nhooks = {custom = {}}\n',
        '[build-system]\nbuild-backend="setuptools.build_meta"\n',
    ],
)
def test_unknown_or_hook_driven_builds_do_not_earn_source_only_classification(config):
    assert declarative_python_source_build(config) is False
