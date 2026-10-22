"""Package requirements must come from the artifact, not development environments."""

from pcc.driver import cli_bootstrap
from pcc.package.install import (
    artifact_requires_dist,
    artifact_requires_dist_diagnostics,
)
import io
import tarfile
import zipfile
import pytest


def write_metadata(path, dependency):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "Name: example-tools\nVersion: 1.2.3\nRequires-Dist: " + dependency + "\n"
    )


def test_source_requirements_exclude_virtualenv_and_build_metadata(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname="example-tools"\nversion="1.2.3"\n'
    )
    write_metadata(tmp_path / "example_tools.egg-info/PKG-INFO", "real-dependency")
    write_metadata(
        tmp_path / ".venv/lib/site-packages/unrelated.dist-info/METADATA",
        "foreign-dependency>=99",
    )
    write_metadata(
        tmp_path / "build/unrelated.dist-info/METADATA", "build-only-dependency"
    )
    assert artifact_requires_dist(tmp_path) == ("real-dependency",)
    assert cli_bootstrap._native_requires_from_tree(str(tmp_path)) == [
        "real-dependency"
    ]
    assert artifact_requires_dist_diagnostics(tmp_path) == ()


def test_src_layout_metadata_is_retained(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname="example-tools"\n')
    write_metadata(tmp_path / "src/example_tools.egg-info/PKG-INFO", "real-dependency")
    assert artifact_requires_dist(tmp_path) == ("real-dependency",)
    assert cli_bootstrap._native_requires_from_tree(str(tmp_path)) == [
        "real-dependency"
    ]


def test_unpacked_sdist_wrapper_is_retained(tmp_path):
    write_metadata(tmp_path / "example-tools-1.2.3/PKG-INFO", "real-dependency")
    assert artifact_requires_dist(tmp_path) == ("real-dependency",)
    assert cli_bootstrap._native_requires_from_tree(str(tmp_path)) == [
        "real-dependency"
    ]


@pytest.mark.parametrize("kind", ["zip", "tar"])
def test_archive_requirements_exclude_nested_vendor_metadata(tmp_path, kind):
    members = {
        "example-tools-1.2.3/PKG-INFO": b"Requires-Dist: real-dependency\n",
        "example-tools-1.2.3/vendor/unrelated/PKG-INFO": b"Requires-Dist: foreign-dependency>=99\n",
    }
    archive = tmp_path / (
        "example-tools-1.2.3.zip" if kind == "zip" else "example-tools-1.2.3.tar.gz"
    )
    if kind == "zip":
        with zipfile.ZipFile(archive, "w") as output:
            for name, data in members.items():
                output.writestr(name, data)
    else:
        with tarfile.open(archive, "w:gz") as output:
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                output.addfile(info, io.BytesIO(data))
    assert artifact_requires_dist(archive) == ("real-dependency",)
    assert artifact_requires_dist_diagnostics(archive) == ()
    assert cli_bootstrap._native_artifact_requires_dist(
        str(archive), str(tmp_path / "scratch")
    ) == ["real-dependency"]


def requirements_from_layout(root, tmp_path, kind):
    artifact = root
    if kind == "zip":
        artifact = tmp_path / "example-tools-1.2.3.zip"
        with zipfile.ZipFile(artifact, "w") as output:
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    output.write(path, "./" + path.relative_to(root).as_posix())
    elif kind == "tar":
        artifact = tmp_path / "example-tools-1.2.3.tar.gz"
        with tarfile.open(artifact, "w:gz") as output:
            output.add(root, arcname=".")
    host = artifact_requires_dist(artifact)
    native = cli_bootstrap._native_artifact_requires_dist(
        str(artifact), str(tmp_path / "scratch")
    )
    return host, native


@pytest.mark.parametrize("kind", ["directory", "zip", "tar"])
def test_direct_module_does_not_adopt_vendor_requirements(tmp_path, kind):
    root = tmp_path / "artifact"
    write_metadata(root / "vendor/foreign.dist-info/METADATA", "foreign-dependency")
    (root / "app.py").write_text("print(42)\n")
    assert requirements_from_layout(root, tmp_path, kind) == ((), [])


@pytest.mark.parametrize("kind", ["directory", "zip", "tar"])
def test_unmarked_single_directory_is_not_a_source_wrapper(tmp_path, kind):
    root = tmp_path / "artifact"
    write_metadata(root / "dependency/foreign.dist-info/METADATA", "foreign-dependency")
    assert requirements_from_layout(root, tmp_path, kind) == ((), [])


@pytest.mark.parametrize("kind", ["directory", "zip", "tar"])
@pytest.mark.parametrize("module", ["app.py", "app.so"])
def test_direct_module_blocks_even_a_marked_source_wrapper(tmp_path, kind, module):
    root = tmp_path / "artifact"
    write_metadata(root / "dependency-1.0/PKG-INFO", "foreign-dependency")
    (root / module).write_bytes(b"module payload")
    assert requirements_from_layout(root, tmp_path, kind) == ((), [])


@pytest.mark.parametrize("kind", ["directory", "zip", "tar"])
@pytest.mark.parametrize("marker", ["pyproject.toml", "setup.py", "setup.cfg"])
def test_marked_source_wrapper_preserves_src_metadata(tmp_path, kind, marker):
    root = tmp_path / "artifact"
    wrapper = root / "example-tools-1.2.3"
    write_metadata(wrapper / "src/example_tools.egg-info/PKG-INFO", "real-dependency")
    (wrapper / marker).write_text("# project marker\n")
    assert requirements_from_layout(root, tmp_path, kind) == (
        ("real-dependency",),
        ["real-dependency"],
    )
