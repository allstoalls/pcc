"""Facade and behavior contract for textual pipeline import discovery."""

from pcc.frontends.python import pipeline
from pcc.frontends.python import pipeline_import_scan


def test_pipeline_reexports_import_scanners_by_identity():
    assert (
        pipeline._source_module_scope_lines
        is pipeline_import_scan._source_module_scope_lines
    )
    assert (
        pipeline._iter_source_import_specs
        is pipeline_import_scan._iter_source_import_specs
    )
    assert (
        pipeline._iter_source_import_from_specs
        is pipeline_import_scan._iter_source_import_from_specs
    )
    assert (
        pipeline._iter_source_importlib_literal_specs
        is pipeline_import_scan._iter_source_importlib_literal_specs
    )
    assert (
        pipeline._iter_source_importlib_resource_literal_specs
        is pipeline_import_scan._iter_source_importlib_resource_literal_specs
    )
    assert (
        pipeline._source_import_discovery_text
        is pipeline_import_scan._source_import_discovery_text
    )
    assert (
        pipeline._without_type_checking_imports
        is pipeline_import_scan._without_type_checking_imports
    )


def test_module_scope_scan_keeps_control_flow_and_masks_function_suites():
    source = (
        "if enabled:\n"
        "    import eager\n"
        "def deferred():\n"
        "    import lazy\n"
        "import final\n"
    )
    rows = pipeline_import_scan._source_module_scope_lines(source)
    assert rows == [
        ("if enabled:", True),
        ("    import eager", True),
        ("def deferred():", False),
        ("    import lazy", False),
        ("import final", True),
    ]
    assert pipeline_import_scan._iter_source_import_specs(
        source,
        top_level_only=True,
    ) == ["eager", "final"]


def test_literal_dynamic_resource_and_multiline_from_imports_are_preserved():
    source = (
        "importlib.import_module('pkg.dynamic')\n"
        "importlib.resources.files(\n"
        "    \"pkg.assets\",\n"
        ")\n"
        "from pkg.api import (\n"
        "    first,\n"
        "    second as renamed,\n"
        ")\n"
    )
    assert pipeline_import_scan._iter_source_importlib_literal_specs(
        source,
        top_level_only=True,
    ) == ["pkg.dynamic"]
    assert pipeline_import_scan._iter_source_importlib_resource_literal_specs(
        source,
        top_level_only=True,
    ) == ["pkg.assets"]
    assert pipeline_import_scan._iter_source_import_from_specs(
        source,
        top_level_only=True,
    ) == [("pkg.api", ["first", "second"])]


def test_attribute_error_fallback_imports_remain_in_runtime_source_only():
    source = (
        "try:\n"
        "    modern()\n"
        "except AttributeError:\n"
        "    import legacy\n"
        "import current\n"
    )
    filtered = pipeline_import_scan._without_attribute_error_handler_imports(source)
    assert "import legacy" not in filtered
    assert "import current" in filtered


def test_discovery_masks_strings_comments_and_type_checking_suites():
    source = (
        "import typing as t\n"
        "message = 'import fictional'  # import ignored\n"
        "if t.TYPE_CHECKING:\n"
        "    import annotations_only\n"
        "import runtime_module\n"
    )
    masked = pipeline_import_scan._source_import_discovery_text(source)
    assert "fictional" not in masked
    assert "import ignored" not in masked

    runtime_source = pipeline_import_scan._without_type_checking_imports(source)
    assert "import annotations_only" not in runtime_source
    assert "import runtime_module" in runtime_source


def test_literal_calls_ignore_inline_strings_and_comments_without_losing_calls():
    source = (
        'text = "importlib.import_module(\'phantom\')"; '
        "importlib.import_module('actual')\n"
        'text2 = "importlib.resources.files(\'phantom_resource\')"; '
        "importlib.resources.files('actual_resource')\n"
        "# importlib.import_module('comment')\n"
        "importlib.import_module(\"importlib.resources.files('nested_phantom')\")\n"
        "def deferred():\n"
        "    importlib.import_module('lazy')\n"
    )
    assert pipeline_import_scan._iter_source_importlib_literal_specs(
        source, top_level_only=True,
    ) == ["actual", "importlib.resources.files('nested_phantom')"]
    assert pipeline_import_scan._iter_source_importlib_resource_literal_specs(
        source, top_level_only=True,
    ) == ["actual_resource"]
    assert pipeline_import_scan._iter_source_importlib_literal_specs(
        source, top_level_only=False,
    ) == ["actual", "importlib.resources.files('nested_phantom')", "lazy"]


def test_literal_discovery_keeps_fstring_expression_calls_only():
    source = (
        'value = f"importlib.import_module(\'phantom\') '
        '{importlib.import_module(\'actual\').VALUE}"\n'
        'resource = f"{importlib.resources.files(\'actual_resource\')}"\n'
        'escaped = f"{{importlib.import_module(\'escaped_phantom\')}}"\n'
        'formatted = f"{42:importlib.import_module(\'format_phantom\')}"\n'
        'def deferred():\n'
        '    return f"{importlib.import_module(\'lazy\').VALUE}"\n'
    )
    assert pipeline_import_scan._iter_source_importlib_literal_specs(
        source, top_level_only=True,
    ) == ["actual"]
    assert pipeline_import_scan._iter_source_importlib_resource_literal_specs(
        source, top_level_only=True,
    ) == ["actual_resource"]
    assert pipeline_import_scan._iter_source_importlib_literal_specs(
        source, top_level_only=False,
    ) == ["actual", "lazy"]
