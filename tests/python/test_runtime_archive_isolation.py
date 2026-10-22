from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def darwin_archive_policy(monkeypatch):
    """Exercise the retained Make/libpython policy on every test host.

    Linux/Windows configuration-aware selection is covered separately by
    test_owned_runtime_selection; these cases specify the Darwin route.
    """
    from pcc.frontends.python import pipeline_runtime_archive

    monkeypatch.setattr(pipeline_runtime_archive, "sys", SimpleNamespace(platform="darwin"))


def test_runtime_archive_provenance_stamp_target_cli(tmp_path):
    from pcc.tools.runtime_archive_provenance import main
    from pcc.frontends.python.pipeline_runtime_archive import target_id

    archive = tmp_path / "libpy_runtime_pcc_py.a"
    archive.write_bytes(b"archive")
    assert main([
        "stamp-target",
        "--archive", str(archive),
        "--host-target-triple", "arm64-apple-darwin",
    ]) == 0
    assert Path(str(archive) + ".target").read_text(encoding="ascii") == (
        target_id("arm64-apple-darwin") + "\n"
    )

def _write_completed_capi_bundle(archive: Path) -> Path:
    from pcc.backend.arm64_asm_driver import assemble_file
    from pcc.backend.macho_obj import emit_object
    from pcc.backend.ar_writer import write_archive

    sections, undefined = assemble_file(
        ".section __TEXT,__text,regular,pure_instructions\n"
        ".globl _PyRuntime_IsolationAnchor\n_PyRuntime_IsolationAnchor:\n ret\n")
    archive.write_bytes(write_archive([
        ("anchor.o", emit_object(sections, undefined=undefined)),
    ]))
    Path(str(archive) + ".capi_syms").write_text(
        "_PyRuntime_IsolationAnchor\n", encoding="ascii"
    )
    return archive


def _write_valid_production_runtime_archive(runtime_root: Path) -> Path:
    from pcc.backend.owned_object_emit import emit_owned_object
    from pcc.backend.ar_writer import write_archive
    from pcc.tools.runtime_archive_provenance import (
        assemble_runtime_archive_manifest,
        capi_inventory_path_for_archive,
        write_pcc_python_receipt,
    )

    source = runtime_root / "py" / "member.py"
    ir_path = runtime_root / "build_py" / "member.ll"
    object_path = runtime_root / "build_py" / "member.o"
    source.parent.mkdir(parents=True, exist_ok=True)
    object_path.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("def member() -> int:\n    return 1\n", encoding="utf-8")
    target_triple = "arm64-apple-darwin"
    ir_text = (
        f'target triple = "{target_triple}"\n'
        "define i32 @member() {\nentry:\n  ret i32 1\n}\n"
    )
    ir_path.write_text(ir_text, encoding="utf-8")
    object_path.write_bytes(emit_owned_object(ir_text, target_triple))
    write_pcc_python_receipt(
        object_path=object_path,
        ir_path=ir_path,
        source_path=source,
        runtime_root=runtime_root,
        target_triple=target_triple,
    )
    archive = runtime_root / "libpy_runtime_pcc_py.a"
    archive.write_bytes(write_archive([(object_path.name, object_path.read_bytes())]))
    capi_inventory_path_for_archive(archive).write_text(
        "PyRuntime_IsolationAnchor\n",
        encoding="ascii",
    )
    assemble_runtime_archive_manifest(
        archive,
        [object_path],
        runtime_root=runtime_root,
    )
    return archive


def test_runtime_archive_environment_override_is_fail_closed(
    tmp_path: Path,
    monkeypatch,
    darwin_archive_policy,
):
    from pcc.frontends.python import pipeline

    archive = tmp_path / "libpy_runtime_pcc_py_libpython.a"
    _write_completed_capi_bundle(archive)
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(archive))
    assert pipeline._ensure_runtime(False) == str(archive)

    missing = tmp_path / "missing.a"
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(missing))
    with pytest.raises(pipeline.PyPipelineError, match="explicit runtime archive"):
        pipeline._ensure_runtime(False)


def test_explicit_libpython_runtime_archive_rejects_empty_member_inventory(
    tmp_path: Path,
    monkeypatch,
    darwin_archive_policy,
):
    from pcc.frontends.python import pipeline

    archive = tmp_path / "libpy_runtime_pcc_py_libpython.a"
    # This is the observed corrupt publication shape: a regular archive whose
    # only real content was the ar symbol table, accompanied by an empty C-API
    # completion inventory.
    archive.write_bytes(b"!<arch>\n")
    Path(str(archive) + ".capi_syms").write_text("", encoding="ascii")
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(archive))

    with pytest.raises(
        pipeline.PyPipelineError,
        match="invalid archive/inventory bundle",
    ):
        pipeline._ensure_runtime(False)


def test_explicit_libpython_runtime_archive_rejects_inventory_from_another_archive(
    tmp_path: Path,
    monkeypatch,
    darwin_archive_policy,
):
    from pcc.frontends.python import pipeline

    archive = _write_completed_capi_bundle(
        tmp_path / "libpy_runtime_pcc_py_libpython.a"
    )
    Path(str(archive) + ".capi_syms").write_text(
        "PyDifferentRuntimeSymbol\n",
        encoding="ascii",
    )
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(archive))

    with pytest.raises(
        pipeline.PyPipelineError,
        match="invalid archive/inventory bundle",
    ):
        pipeline._ensure_runtime(False)


def test_runtime_make_never_captures_away_build_diagnostics(monkeypatch):
    from pcc.frontends.python import pipeline

    calls: list[dict] = []

    def fake_run(command, **kwargs):
        calls.append({"command": command, **kwargs})

    monkeypatch.setattr(pipeline.subprocess, "run", fake_run)
    pipeline._run_runtime_make(["make", "runtime"], verbose=False)

    assert len(calls) == 1
    assert calls[0]["check"] is True
    assert calls[0]["capture_output"] is False


def test_runtime_make_reclaims_dead_owner_lock(tmp_path: Path):
    from pcc.frontends.python.pipeline_runtime_archive import run_runtime_make

    lock = tmp_path / ".pcc-runtime-build.lock"
    lock.mkdir()
    (lock / "owner").write_text("99999999\n", encoding="ascii")

    run_runtime_make(
        str(tmp_path),
        [sys.executable, "-c", "pass"],
        verbose=False,
    )

    assert not lock.exists()


def test_runtime_make_does_not_require_PATH_tools(tmp_path, monkeypatch, capfd):
    from pcc.frontends.python.pipeline_runtime_archive import run_runtime_make

    monkeypatch.setenv("PATH", "/nonexistent")
    run_runtime_make(
        str(tmp_path), [sys.executable, "-c", "pass"], verbose=False,
    )

    assert not (tmp_path / ".pcc-runtime-build.lock").exists()
    assert "command not found" not in capfd.readouterr().err


def test_runtime_build_failure_is_reported_before_link(
    tmp_path: Path,
    monkeypatch,
    darwin_archive_policy,
):
    from pcc.frontends.python import pipeline

    runtime_root = tmp_path / "py_runtime"
    runtime_root.mkdir()
    (runtime_root / "Makefile").write_text("all:\n", encoding="utf-8")
    archive = runtime_root / "libpy_runtime_pcc_py.a"

    def fail_build(_make_cmd, *, verbose):
        raise subprocess.CalledProcessError(2, ["make", "libpy_runtime_pcc_py.a"])

    monkeypatch.setattr(pipeline, "_PY_RUNTIME_DIR", str(runtime_root))
    monkeypatch.setattr(pipeline, "_PY_RUNTIME_ARCHIVE_PCC_PY", str(archive))
    monkeypatch.setattr(pipeline, "_run_runtime_make", fail_build)

    with pytest.raises(pipeline.PyPipelineError, match="failed to build required"):
        pipeline._ensure_runtime(False, needs_libpython=False)


def test_runtime_build_rejects_empty_archive_publication(
    tmp_path: Path,
    monkeypatch,
    darwin_archive_policy,
):
    from pcc.frontends.python import pipeline

    runtime_root = tmp_path / "py_runtime"
    runtime_root.mkdir()
    (runtime_root / "Makefile").write_text("all:\n", encoding="utf-8")
    archive = runtime_root / "libpy_runtime_pcc_py_libpython.a"

    def publish_empty(_make_cmd, *, verbose):
        archive.write_bytes(b"!<arch>\n")
        Path(str(archive) + ".capi_syms").write_text("", encoding="ascii")

    monkeypatch.setattr(pipeline, "_PY_RUNTIME_DIR", str(runtime_root))
    monkeypatch.setattr(pipeline, "_PY_RUNTIME_ARCHIVE_PCC_PY_LIBPYTHON", str(archive))
    monkeypatch.setattr(pipeline, "_run_runtime_make", publish_empty)

    with pytest.raises(
        pipeline.PyPipelineError,
        match="invalid archive/inventory bundle",
    ):
        pipeline._ensure_runtime(False, needs_libpython=True)


def test_libpython_runtime_make_publication_requires_nonempty_inventory() -> None:
    makefile = (
        Path(__file__).resolve().parents[2] / "pcc" / "runtime" / "Makefile"
    ).read_text(encoding="utf-8")

    rule = makefile[makefile.index("$(LIB_PCC_PY_LIBPYTHON): $(LIB_PCC_PY)") :]
    rule = rule[: rule.index("$(OBJDIR_LIBPY)/py_libpython.o:")]
    assert 'test -s "$@.capi_syms.nm.tmp"' in rule
    assert 'test -s "$@.capi_syms.tmp"' in rule
    assert rule.index('mv -f "$@.tmp" "$@"') < rule.index(
        'mv -f "$@.capi_syms.tmp" "$@.capi_syms"'
    )


def test_production_runtime_archive_environment_override_rejects_invalid_provenance(
    tmp_path: Path,
    monkeypatch,
    darwin_archive_policy,
):
    from pcc.frontends.python import pipeline

    archive = tmp_path / "libpy_runtime_pcc_py.a"
    archive.write_bytes(b"not a production archive")
    Path(str(archive) + ".provenance.json").write_text(
        '{"schema": "not-the-production-schema"}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(archive))

    with pytest.raises(pipeline.PyPipelineError, match="invalid provenance"):
        pipeline._ensure_runtime(False)


@pytest.mark.parametrize("shortcut", ["fresh", "wheel"])
def test_invalid_production_runtime_shortcut_rebuilds_before_acceptance(
    tmp_path: Path,
    monkeypatch,
    shortcut: str,
    darwin_archive_policy,
):
    from pcc.frontends.python import pipeline

    runtime_root = tmp_path / "py_runtime"
    runtime_root.mkdir()
    makefile = runtime_root / "Makefile"
    makefile.write_text("all:\n", encoding="utf-8")
    archive = _write_valid_production_runtime_archive(runtime_root)
    manifest = Path(str(archive) + ".provenance.json")
    manifest.write_text('{"schema": "tampered"}\n', encoding="utf-8")
    Path(pipeline._runtime_archive_target_stamp(str(archive))).write_text(
        pipeline._runtime_archive_target_id() + "\n",
        encoding="utf-8",
    )
    if shortcut == "wheel":
        Path(str(archive) + ".wheel").write_text(
            "pcc.runtime-wheel-artifact.v1\n"
            + pipeline._runtime_archive_target_id()
            + "\nsha256:"
            + ("a" * 64)
            + "\n",
            encoding="utf-8",
        )
    old = 1_700_000_000
    current = old + 10
    os.utime(makefile, (old, old))
    os.utime(runtime_root / "py" / "member.py", (old, old))
    os.utime(archive, (current, current))

    make_calls: list[list[str]] = []

    def rebuild(make_cmd, *, verbose):
        make_calls.append(list(make_cmd))
        _write_valid_production_runtime_archive(runtime_root)

    monkeypatch.setenv("PCC_RUNTIME_CC", "pcc")
    monkeypatch.setattr(pipeline, "_PY_RUNTIME_DIR", str(runtime_root))
    monkeypatch.setattr(pipeline, "_PY_RUNTIME_ARCHIVE_PCC_PY", str(archive))
    monkeypatch.setattr(
        pipeline,
        "_runtime_archive_compiler_sources_newer_than",
        lambda *_args: False,
    )
    monkeypatch.setattr(pipeline, "_run_runtime_make", rebuild)

    assert pipeline._ensure_runtime(False, needs_libpython=False) == str(archive)
    assert len(make_calls) == 1
    assert pipeline._runtime_archive_stale(str(archive)) is False


def test_production_runtime_is_verified_after_make_before_acceptance(
    tmp_path: Path,
    monkeypatch,
    darwin_archive_policy,
):
    from pcc.frontends.python import pipeline

    runtime_root = tmp_path / "py_runtime"
    runtime_root.mkdir()
    (runtime_root / "Makefile").write_text("all:\n", encoding="utf-8")
    archive = runtime_root / "libpy_runtime_pcc_py.a"

    def build_invalid_archive(_make_cmd, *, verbose):
        archive.write_bytes(b"not a production archive")
        Path(str(archive) + ".provenance.json").write_text(
            '{"schema": "tampered"}\n',
            encoding="utf-8",
        )

    monkeypatch.setenv("PCC_RUNTIME_CC", "pcc")
    monkeypatch.setattr(pipeline, "_PY_RUNTIME_DIR", str(runtime_root))
    monkeypatch.setattr(pipeline, "_PY_RUNTIME_ARCHIVE_PCC_PY", str(archive))
    monkeypatch.setattr(pipeline, "_run_runtime_make", build_invalid_archive)

    with pytest.raises(pipeline.PyPipelineError, match="invalid provenance"):
        pipeline._ensure_runtime(False, needs_libpython=False)
    assert not Path(pipeline._runtime_archive_target_stamp(str(archive))).exists()


def test_invalid_production_runtime_without_makefile_fails_closed(
    tmp_path: Path,
    monkeypatch,
    darwin_archive_policy,
):
    from pcc.frontends.python import pipeline

    runtime_root = tmp_path / "py_runtime"
    runtime_root.mkdir()
    archive = runtime_root / "libpy_runtime_pcc_py.a"
    archive.write_bytes(b"not a production archive")
    Path(str(archive) + ".provenance.json").write_text(
        '{"schema": "tampered"}\n',
        encoding="utf-8",
    )
    Path(pipeline._runtime_archive_target_stamp(str(archive))).write_text(
        pipeline._runtime_archive_target_id() + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("PCC_RUNTIME_CC", "pcc")
    monkeypatch.setattr(pipeline, "_PY_RUNTIME_DIR", str(runtime_root))
    monkeypatch.setattr(pipeline, "_PY_RUNTIME_ARCHIVE_PCC_PY", str(archive))

    with pytest.raises(pipeline.PyPipelineError, match="invalid provenance"):
        pipeline._ensure_runtime(False, needs_libpython=False)


def test_auto_package_compile_propagates_explicit_runtime_archive(
    tmp_path: Path,
    monkeypatch,
):
    from pcc.frontends.python import pipeline

    package = tmp_path / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "lib.py").write_text("VALUE = 7\n", encoding="utf-8")
    entry = package / "__main__.py"
    entry.write_text("from .lib import VALUE\nprint(VALUE)\n", encoding="utf-8")
    archive = tmp_path / "isolated-runtime.a"
    archive.write_bytes(b"archive")
    captured = {}

    def fake_compile_python_multi(*args, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(pipeline, "compile_python_multi", fake_compile_python_multi)
    pipeline.compile_python(
        str(entry),
        str(tmp_path / "out"),
        libpython_mode="off",
        ir_scaffold_mode="on",
        runtime_archive=str(archive),
    )

    assert captured["runtime_archive"] == str(archive)
