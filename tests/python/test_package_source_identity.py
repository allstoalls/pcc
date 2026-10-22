"""Local project names and archive versions must not collide at the first dash."""

from pathlib import Path

from pcc.driver import cli_bootstrap
import pytest

from pcc.package.schema import literal_project_metadata_fields
from pcc.package.metadata import inspect_artifact
from pcc.package import acquire, install, metadata


def test_local_project_name_comes_from_project_metadata(tmp_path):
    source = tmp_path / "checkout"
    source.mkdir()
    (source / "pyproject.toml").write_text('[project]\nname = "example-tools"\nversion = "1.2.3"\n')
    assert cli_bootstrap._native_package_basename(str(source)) == "example-tools"
    assert cli_bootstrap._native_artifact_version_text(str(source)) == "1.2.3"
    assert inspect_artifact("", source).name == "example-tools"


def test_directory_name_keeps_hyphens_without_metadata(tmp_path):
    source = tmp_path / "example-tools"
    source.mkdir()
    assert cli_bootstrap._native_package_basename(str(source)) == "example-tools"


def test_sdist_name_and_version_split_at_the_version_boundary():
    from pathlib import Path

    path = "/artifacts/example-tools-1.2.3.tar.gz"
    assert cli_bootstrap._native_package_basename(path) == "example-tools"
    assert cli_bootstrap._native_artifact_project_name(path) == "example-tools"
    assert cli_bootstrap._native_artifact_version_text(path) == "1.2.3"
    assert metadata._sdist_name(Path(path)) == "example-tools"
    assert install._artifact_project_name(Path(path)) == "example-tools"
    assert install._artifact_version_text(Path(path)) == "1.2.3"
    assert acquire._artifact_version(path) == "1.2.3"


def test_wheel_name_and_version_keep_their_standard_positions():
    path = "/artifacts/example_tools-1.2.3-py3-none-any.whl"
    assert cli_bootstrap._native_package_basename(path) == "example_tools"
    assert cli_bootstrap._native_artifact_project_name(path) == "example-tools"
    assert cli_bootstrap._native_artifact_version_text(path) == "1.2.3"


@pytest.mark.parametrize("name", ["../escape", "bad/name", "bad name", ".hidden", "name-"])
def test_invalid_declared_project_identity_fails_closed(name):
    with pytest.raises(ValueError, match="PCC-PKG-PROJECT-NAME-INVALID"):
        literal_project_metadata_fields('[project]\nname = "' + name + '"\n')


def test_single_quoted_project_fields_and_inline_comments():
    assert literal_project_metadata_fields(
        "[build-system]\nrequires=[]\n[project] # metadata\n"
        "name = 'example-tools' # comment\nversion = '1.2.3'\n"
    ) == ["example-tools", "1.2.3"]


@pytest.mark.parametrize(
    "config",
    [
        '["project"]\n"name"="example-tools"\n\'version\'="1.2.3"\n',
        'project.name="example-tools"\nproject.version="1.2.3"\n',
        '"project" . "name"="example-tools"\nproject.version="1.2.3"\n',
    ],
)
def test_project_identity_respects_supported_toml_key_forms(config):
    import tomllib

    project = tomllib.loads(config)["project"]
    assert literal_project_metadata_fields(config) == [project["name"], project["version"]]


def test_nested_project_keys_do_not_define_distribution_identity():
    config = '[tool.release]\nproject.name="wrong-name"\nproject.version="9.9"\n'
    assert literal_project_metadata_fields(config) == ["", ""]
    config += '[project]\nname="real-name"\nversion="1.0"\n'
    assert literal_project_metadata_fields(config) == ["real-name", "1.0"]


@pytest.mark.parametrize(
    "config",
    [
        'project={name="example-tools", version="1.2.3"}\n',
        '[project] trailing\nname="example-tools"\n',
        '[project]\nname="example-tools"\n[project]\nversion="1.2.3"\n',
        'project.name="example-tools"\n[project]\nversion="1.2.3"\n',
        '[project]\nname="""example-tools"""\n',
        '[project]\nname="example-tools"\nproject={name="wrong-name"}\n',
        '["pro\\u006aect"]\nname="example-tools"\n',
        '\v[project]\nname="example-tools"\n',
    ],
)
def test_unsupported_or_ambiguous_project_syntax_fails_closed(config):
    with pytest.raises(ValueError, match="PCC-PKG-PROJECT-METADATA-UNSUPPORTED"):
        literal_project_metadata_fields(config)


@pytest.mark.parametrize(
    "settings",
    [
        'description="""\n[project]\nname="fake-name"\n"""\n',
        "description='''\n[project]\nname=\"fake-name\"\n'''\n",
        'description="a \\"quoted\\" setting with \\n escaped text"\n',
        '[[tool.changelog.type]]\nname="first"\n[[tool.changelog.type]]\nname="second"\n',
    ],
)
def test_unrelated_settings_do_not_replace_literal_project_identity(settings):
    import tomllib

    config = settings + '[project]\nname="example-tools"\nversion="1.2.3"\n'
    assert tomllib.loads(config)["project"]["name"] == "example-tools"
    assert literal_project_metadata_fields(config) == ["example-tools", "1.2.3"]


_SOURCE_ROOT = Path(__file__).resolve().parents[2]


# An optional source project that is not checked out deselects its case.
@pytest.mark.parametrize("relative", [
    pytest.param(relative, marks=pytest.mark.pcc_gate(
        probe=lambda relative=relative: (_SOURCE_ROOT / relative).is_file()))
    for relative in ("pyproject.toml", "projects/numpy-2.4.4/pyproject.toml")
])
def test_existing_source_project_identity_matches_tomllib(relative):
    import tomllib

    path = _SOURCE_ROOT / relative
    text = path.read_text(encoding="utf-8")
    project = tomllib.loads(text)["project"]
    assert literal_project_metadata_fields(text) == [project["name"], project["version"]]
