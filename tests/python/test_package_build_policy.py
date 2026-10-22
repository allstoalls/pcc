"""Source-overlay policy must interpret configuration paths, never string data."""

import tomllib

import pytest

from pcc.package.schema import declarative_python_source_build, source_build_policy

BACKEND = '[build-system]\nrequires=["hatchling"]\nbuild-backend="hatchling.build"\n'


@pytest.mark.parametrize(
    "config",
    [
        BACKEND,
        '["build-system"]\n"build-backend"="hatchling.build"\n',
        'build-system.build-backend="hatchling.build"\n',
        BACKEND + '[tool.hatch.build.targets.wheel]\npackages=[\n "example_tools", # payload\n]\n',
        BACKEND + '[tool.uv.sources]\nexample={path="../example", editable=true}\n',
        BACKEND
        + '[project]\nname="example-tools"\ndescription="data # [tool.hatch.build.hooks.custom]"\n',
    ],
)
def test_ordinary_source_overlay_supported_forms(config):
    assert tomllib.loads(config)["build-system"]["build-backend"] == "hatchling.build"
    assert source_build_policy(config) == "declarative_python_source"
    assert declarative_python_source_build(config) is True


@pytest.mark.parametrize(
    "hook",
    [
        '[tool.hatch.build.hooks.custom]\npath="generate.py"\n',
        '[tool.hatch.build]\nhooks.custom.path="generate.py"\n',
        '[tool.hatch]\nbuild.hooks.custom.path="generate.py"\n',
        '[tool.hatch.build.targets.wheel]\nhooks.custom.path="generate.py"\n',
        '["tool"."hatch"."metadata"."hooks"."custom"]\n',
    ],
)
def test_declared_hook_forms_retain_build_ownership(hook):
    config = BACKEND + hook
    assert "hatch" in tomllib.loads(config)["tool"]
    assert source_build_policy(config) == "requires_build_hook"
    assert declarative_python_source_build(config) is False


def test_in_tree_backend_retains_build_ownership():
    config = BACKEND + 'backend-path=["backend"]\n'
    assert source_build_policy(config) == "requires_build_hook"


@pytest.mark.parametrize(
    "config",
    [
        BACKEND + '[tool]\nhatch={build={hooks={custom={path="generate.py"}}}}\n',
        'build-system={build-backend="hatchling.build", backend-path=["backend"]}\n',
        BACKEND + '[tool.hatch.build]\nhooks={custom={path="generate.py"}}\n',
        BACKEND + '[tool.hatch]\nbuild={hooks={custom={path="generate.py"}}}\n',
        BACKEND + '["tool"."hat\\u0063h".build.hooks.custom]\n',
        BACKEND + '[build-system]\nbackend-path=["backend"]\n',
        '[build-system] trailing\nbuild-backend="hatchling.build"\n',
        BACKEND + "[tool.hatch.build]\ninvalid=[\n[tool.hatch.build.hooks.custom]\n",
    ],
)
def test_unsupported_or_ambiguous_configuration_never_earns_overlay(config):
    with pytest.raises(ValueError, match="PCC-PKG-PROJECT-METADATA-UNSUPPORTED"):
        source_build_policy(config)
    assert declarative_python_source_build(config) is False


@pytest.mark.parametrize(
    "config",
    [
        "",
        '[build-system]\nbuild-backend="setuptools.build_meta"\n',
        '[tool.example]\nbuild-system.build-backend="hatchling.build"\n',
        "description=\"[build-system] build-backend='hatchling.build'\"\n",
    ],
)
def test_other_configuration_is_unrecognized(config):
    assert source_build_policy(config) == "unrecognized"


@pytest.mark.parametrize(
    "settings",
    [
        'description="""\n[build-system]\nbuild-backend="fake.build"\n[tool.hatch.build.hooks.custom]\n"""\n',
        "description='''\n[tool.hatch.build.hooks.custom]\n'''\n",
        'description="\\n[tool.hatch.build.hooks.custom] \\"quoted\\""\n',
        '[[tool.changelog.type]]\nname="first"\n[[tool.changelog.type]]\nname="second"\n',
    ],
)
def test_unrelated_settings_are_opaque_to_build_policy(settings):
    config = settings + BACKEND
    assert tomllib.loads(config)["build-system"]["build-backend"] == "hatchling.build"
    assert source_build_policy(config) == "declarative_python_source"


def test_array_table_does_not_hide_a_later_hatch_hook():
    config = '[[tool.changelog.type]]\nname="first"\n' + BACKEND
    config += '[tool.hatch.build.hooks.custom]\npath="generate.py"\n'
    assert source_build_policy(config) == "requires_build_hook"


@pytest.mark.parametrize(
    "config",
    [
        '[[project]]\nname="example-tools"\n',
        '[[build-system]]\nbuild-backend="hatchling.build"\n',
        BACKEND + "[[tool.hatch.build.hooks.custom]]\n",
        '[build-system]\nbuild-backend="""hatchling.build"""\n',
        '[build-system]\nbuild-backend="hatchling.\\u0062uild"\n',
    ],
)
def test_relevant_unsupported_value_or_array_table_still_fails_closed(config):
    with pytest.raises(ValueError, match="PCC-PKG-PROJECT-METADATA-UNSUPPORTED"):
        source_build_policy(config)


def test_fake_backend_in_multiline_string_is_not_configuration():
    config = 'description="""\n' + BACKEND + '"""\n'
    assert "build-system" not in tomllib.loads(config)
    assert source_build_policy(config) == "unrecognized"
