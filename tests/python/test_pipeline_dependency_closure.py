"""Focused contracts for the extracted dependency-closure pipeline seam."""

from __future__ import annotations

def test_pipeline_dependency_closure_facade_has_single_function_owners():
    from pcc.frontends.python import pipeline
    from pcc.frontends.python import pipeline_dependency_closure as closure

    for name in (
        "_validate_package_site_no_libpython_abi",
        "_top_level_import_targets",
        "_package_import_targets",
        "_collect_relative_module_closure",
        "_collect_multi_source_relative_closure",
        "_filter_ir_scaffold_closure",
        "_prepare_multi_source_compile_closure",
        "_classify_python_import",
        "_expand_recursive_stdlib",
        "_order_module_inits",
    ):
        assert getattr(pipeline, name) is getattr(closure, name)


def test_package_import_targets_follow_real_relative_source(tmp_path):
    from pcc.frontends.python.pipeline_dependency_closure import _package_import_targets

    package = tmp_path / "pkg"
    package.mkdir()
    entry = package / "entry.py"
    sibling = package / "sibling.py"
    entry.write_text("from .sibling import answer\n", encoding="utf-8")
    sibling.write_text("answer = 42\n", encoding="utf-8")

    targets = _package_import_targets(
        str(entry),
        "pkg.entry",
        root_dir=str(tmp_path),
    )
    assert targets == [(str(sibling), "pkg.sibling")]


def test_import_classification_uses_shared_policy_tables():
    from pcc.frontends.python.pipeline_dependency_closure import _classify_python_import

    assert _classify_python_import("typing") == "compile_time_only"
    assert _classify_python_import("os") == "builtin_native_dispatch"
    assert (
        _classify_python_import("myapp.worker", native_modules={"myapp.worker"})
        == "native_user_module"
    )
