"""Contracts for the filesystem-layout seam extracted from pipeline.py."""

from __future__ import annotations

import inspect
from pathlib import Path

from pcc.frontends.python import pipeline
from pcc.driver import paths as pipeline_paths


def _make_pcc_package(root: Path) -> Path:
    package = root / "pcc"
    (package / "backend").mkdir(parents=True)
    (package / "stdlib").mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "backend" / "self_backend_dispatch.py").write_text(
        "", encoding="utf-8"
    )
    (package / "stdlib" / "__init__.py").write_text("", encoding="utf-8")
    return package


def test_explicit_source_root_resolves_package_without_pipeline_import_state(
    tmp_path, monkeypatch
):
    package = _make_pcc_package(tmp_path / "checkout")
    synthetic_pipeline = tmp_path / "installed" / "pcc" / "frontends" / "python" / "pipeline.py"
    synthetic_pipeline.parent.mkdir(parents=True)
    synthetic_pipeline.write_text("", encoding="utf-8")
    monkeypatch.setenv("PCC_SOURCE_ROOT", str(package.parent))
    monkeypatch.delenv("PCC_REPO_ROOT", raising=False)
    monkeypatch.delenv("PCC_PY_STDLIB_ROOT", raising=False)

    assert (
        pipeline_paths.resolve_pcc_dir_from_environment(str(synthetic_pipeline))
        == str(package)
    )


def test_runtime_path_selection_is_ordered_and_falls_back_to_package_runtime(
    tmp_path, monkeypatch
):
    package = _make_pcc_package(tmp_path / "checkout")
    runtime = package / "runtime"
    (runtime / "include").mkdir(parents=True)
    (runtime / "include" / "py_runtime.h").write_text("", encoding="utf-8")
    synthetic_pipeline = package / "frontends" / "python" / "pipeline.py"
    synthetic_pipeline.parent.mkdir(parents=True)
    synthetic_pipeline.write_text("", encoding="utf-8")
    monkeypatch.setenv("PCC_SOURCE_ROOT", str(package))

    pcc_dir, pipeline_dir, runtime_dir, candidates = (
        pipeline_paths.resolve_runtime_paths(str(synthetic_pipeline))
    )

    assert pcc_dir == str(package)
    assert pipeline_dir == str(synthetic_pipeline.parent)
    assert runtime_dir == str(runtime)
    assert candidates[1] == str(runtime)


def test_installed_prefix_candidates_are_stable_and_duplicate_free(tmp_path):
    prefix = tmp_path / "venv"
    versioned = prefix / "lib" / "python3.13" / "site-packages" / "pcc"
    versioned.mkdir(parents=True)
    candidates: list[str] = []

    pipeline_paths.bootstrap_append_install_prefix_candidates(
        candidates, str(prefix)
    )
    pipeline_paths.bootstrap_append_install_prefix_candidates(
        candidates, str(prefix)
    )

    assert len(candidates) == len(set(candidates))
    assert str(versioned) in candidates


def test_pipeline_facade_reexports_path_helpers_without_copying_logic():
    assert (
        pipeline._runtime_dir_has_runtime_files
        is pipeline_paths.runtime_dir_has_runtime_files
    )
    assert (
        pipeline._bootstrap_append_install_prefix_candidates
        is pipeline_paths.bootstrap_append_install_prefix_candidates
    )
    source = inspect.getsource(pipeline._resolve_pcc_dir_from_environment)
    assert "_resolve_pcc_dir_from_environment_impl(__file__)" in source
    pipeline_source = Path(pipeline.__file__).read_text(encoding="utf-8")
    assert "def _bootstrap_append_install_prefix_candidates(" not in pipeline_source
    assert "def _runtime_dir_has_runtime_files(" not in pipeline_source


def test_module_name_and_root_are_derived_from_package_files(tmp_path):
    package = tmp_path / "project" / "pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    main = package / "__main__.py"
    main.write_text("", encoding="utf-8")

    module_name = pipeline_paths.module_name_from_src(str(main))

    assert module_name == "pkg.__main__"
    assert pipeline_paths.module_root_from_src(str(main), module_name) == str(
        package.parent
    )
    assert pipeline_paths.package_parts_for_module(str(main), module_name) == [
        "pkg"
    ]


def test_module_source_resolution_prefers_module_then_package(tmp_path):
    module = tmp_path / "plain.py"
    module.write_text("", encoding="utf-8")
    package = tmp_path / "nested"
    package.mkdir()
    init = package / "__init__.py"
    init.write_text("", encoding="utf-8")

    assert pipeline_paths.resolve_module_src(str(tmp_path), "plain") == str(module)
    assert pipeline_paths.resolve_module_src(str(tmp_path), "nested") == str(init)
    assert pipeline_paths.resolve_module_src(str(tmp_path), "missing") is None


def test_pipeline_facade_reexports_module_path_helpers():
    assert pipeline._join_strings is pipeline_paths.join_strings
    assert pipeline._module_name_from_src is pipeline_paths.module_name_from_src
    assert pipeline._module_root_from_src is pipeline_paths.module_root_from_src
    assert pipeline._resolve_module_src is pipeline_paths.resolve_module_src


def test_directory_listing_is_reused_only_while_the_directory_is_unchanged(
    tmp_path, monkeypatch
):
    import os
    import time

    import pytest

    directory = tmp_path / "pkg"
    directory.mkdir()
    (directory / "a.py").write_text("", encoding="utf-8")
    settled = time.time() - 60
    os.utime(directory, (settled, settled))
    listed: list[str] = []
    real_listdir = os.listdir

    def counting_listdir(path):
        listed.append(path)
        return real_listdir(path)

    monkeypatch.setattr(pipeline_paths.os, "listdir", counting_listdir)
    pipeline_paths._DIRECTORY_NAMES_CACHE.clear()

    assert pipeline_paths.directory_entry_names(str(directory)) == ("a.py",)
    assert pipeline_paths.directory_entry_names(str(directory)) == ("a.py",)
    assert len(listed) == 1

    # A new entry moves the directory mtime, so the next call lists again.
    (directory / "b.py").write_text("", encoding="utf-8")
    assert sorted(pipeline_paths.directory_entry_names(str(directory))) == [
        "a.py",
        "b.py",
    ]
    assert len(listed) == 2
    # Changed moments ago: a same-tick change could hide behind an equal
    # mtime, so the listing is not reused yet.
    pipeline_paths.directory_entry_names(str(directory))
    assert len(listed) == 3

    with pytest.raises(OSError):
        pipeline_paths.directory_entry_names(str(tmp_path / "missing"))
    assert str(tmp_path / "missing") not in pipeline_paths._DIRECTORY_NAMES_CACHE
