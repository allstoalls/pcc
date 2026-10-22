"""These orchestration checks never provision or execute native artifacts."""

import json
from pathlib import Path
import subprocess

import pytest

from tests.native_provisioning import GUARD_NAME, require_native_provisioning_allowed


def test_guard_file_blocks_even_without_environment_inheritance(tmp_path, monkeypatch):
    monkeypatch.delenv("PCC_TEST_NO_NATIVE_PROVISIONING", raising=False)
    (tmp_path / "build").mkdir()
    guard = tmp_path / "build" / GUARD_NAME
    guard.write_text("central compiler qualification is active\n")
    with pytest.raises(RuntimeError, match="automatic native test provisioning is disabled"):
        require_native_provisioning_allowed(tmp_path)
    guard.unlink()
    require_native_provisioning_allowed(tmp_path)


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_environment_guard_blocks_without_marker(tmp_path, monkeypatch, value):
    monkeypatch.setenv("PCC_TEST_NO_NATIVE_PROVISIONING", value)
    with pytest.raises(RuntimeError, match="automatic native test provisioning is disabled"):
        require_native_provisioning_allowed(tmp_path)


def test_runtime_builder_guard_fails_before_builder_call(tmp_path, monkeypatch):
    from tests.runtime_build_cache import cache_runtime_build

    calls = []

    @cache_runtime_build
    def builder(_temporary):
        calls.append(True)
        return object()

    monkeypatch.setenv("PCC_TEST_NO_NATIVE_PROVISIONING", "1")
    with pytest.raises(RuntimeError, match="automatic native test provisioning is disabled"):
        builder(tmp_path)
    assert calls == []


@pytest.mark.parametrize("fixture_name", ["pcc_runtime_archive", "threaded_pcc_runtime_archive"])
def test_shared_fixture_guard_precedes_automatic_builder(monkeypatch, fixture_name):
    from tests import conftest as fixtures
    from pcc.frontends.python import owned_runtime_build

    monkeypatch.delenv("PCC_RUNTIME_ARCHIVE", raising=False)
    monkeypatch.delenv("PCC_THREADED_RUNTIME_ARCHIVE", raising=False)
    monkeypatch.setenv("PCC_TEST_NO_NATIVE_PROVISIONING", "1")

    def unexpected_builder(*_args, **_kwargs):
        raise AssertionError("runtime builder was entered")

    monkeypatch.setattr(owned_runtime_build, "ensure_target_runtime", unexpected_builder)
    monkeypatch.setattr(fixtures, "cached_pcc_python_runtime", unexpected_builder)
    monkeypatch.setattr(fixtures, "cached_threaded_pcc_python_runtime", unexpected_builder)
    fixture = getattr(fixtures, fixture_name).__wrapped__
    with pytest.raises(RuntimeError, match="automatic native test provisioning is disabled"):
        if fixture_name == "pcc_runtime_archive":
            fixture(None)
        else:
            fixture()


def _forbid_native_work(monkeypatch):
    from pcc.frontends.python import owned_runtime_build, pipeline

    def unexpected_native_work(*_args, **_kwargs):
        raise AssertionError("native compilation, provisioning or execution was entered")

    monkeypatch.setattr(pipeline, "compile_python", unexpected_native_work)
    monkeypatch.setattr(owned_runtime_build, "ensure_target_runtime", unexpected_native_work)
    monkeypatch.setattr(owned_runtime_build, "build_runtime_archive", unexpected_native_work)
    monkeypatch.setattr(subprocess, "run", unexpected_native_work)


@pytest.mark.parametrize("reservation", ["environment", "file"])
def test_container_native_helper_guard_precedes_compilation(
    tmp_path, monkeypatch, reservation,
):
    from tests.python import test_native_container_builtin_error_paths as container

    monkeypatch.setattr(container, "_REPO_ROOT", tmp_path)
    monkeypatch.delenv("PCC_RUNTIME_ARCHIVE", raising=False)
    monkeypatch.delenv("PCC_TEST_NO_NATIVE_PROVISIONING", raising=False)
    if reservation == "environment":
        monkeypatch.setenv("PCC_TEST_NO_NATIVE_PROVISIONING", "1")
    else:
        (tmp_path / "build").mkdir()
        (tmp_path / "build" / GUARD_NAME).write_text("native build reserved\n")
    _forbid_native_work(monkeypatch)

    with pytest.raises(RuntimeError, match="automatic native test provisioning is disabled"):
        container._run_native(tmp_path, "print(1)\n")
    assert not (tmp_path / "prog.py").exists()


def test_container_native_helper_rejects_invalid_explicit_archive(tmp_path, monkeypatch):
    from tests.python import test_native_container_builtin_error_paths as container

    archive = tmp_path / "libpy_runtime_pcc_py.a"
    archive.write_bytes(b"not a runtime archive")
    Path(str(archive) + ".provenance.json").write_text("{}\n")
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(archive))
    monkeypatch.setenv("PCC_TEST_NO_NATIVE_PROVISIONING", "1")
    _forbid_native_work(monkeypatch)

    with pytest.raises(ValueError, match="invalid runtime archive manifest fields"):
        container._run_native(tmp_path, "print(1)\n")
    assert not (tmp_path / "prog.py").exists()


@pytest.fixture
def container_runtime_model(tmp_path, monkeypatch):
    """Real owned object/receipt bytes for a tiny inventory; no runtime build.

    These stub functions cannot execute container programs. Compilation and
    native execution stay blocked or recorded by the consuming tests.
    """
    from pcc.backend.ar_writer import _defined_symbols, write_archive
    from pcc.backend.owned_object_emit import emit_owned_object
    from pcc.frontends.python import owned_runtime_build, pipeline_targets
    from pcc.tools import runtime_archive_provenance as provenance
    from tests import runtime_fixture_provenance

    runtime = tmp_path / "runtime"
    (runtime / "py").mkdir(parents=True)
    (runtime / "Makefile").write_text("PY_MODULES = probe\n")
    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    monkeypatch.setenv("PCC_REFCOUNT_KIND", "local")
    config = owned_runtime_build.runtime_build_config()
    target = pipeline_targets.host_target_triple()
    objects = []
    capi_symbols = set()
    for name in owned_runtime_build.runtime_modules(str(runtime), target, config["threads"]):
        symbol = "PyTest_" + name
        source = runtime / "py" / (name + ".py")
        source.write_text(f"def {symbol}() -> int:\n    return 7\n")
        ir = runtime / (name + ".ll")
        ir.write_text(
            f'target triple = "{target}"\n'
            f"define i32 @{symbol}() {{\nentry:\n  ret i32 7\n}}\n"
        )
        obj = runtime / (name + ".o")
        obj.write_bytes(emit_owned_object(ir.read_text(), target))
        provenance.write_pcc_python_receipt(
            object_path=obj, ir_path=ir, source_path=source,
            runtime_root=runtime, target_triple=target,
            runtime_build_config=config,
        )
        objects.append(obj)
        _kind, symbols = _defined_symbols(obj.read_bytes())
        capi_symbols.update(symbols)
    archive = runtime / "libpy_runtime_pcc_py.a"
    archive.write_bytes(write_archive([(obj.name, obj.read_bytes()) for obj in objects]))
    provenance.capi_inventory_path_for_archive(archive).write_text(
        "\n".join(sorted(capi_symbols)) + "\n"
    )
    provenance.assemble_runtime_archive_manifest(archive, objects, runtime_root=runtime)
    monkeypatch.setattr(runtime_fixture_provenance, "_RUNTIME_ROOT", runtime)
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(archive))
    monkeypatch.setenv("PCC_TEST_NO_NATIVE_PROVISIONING", "1")
    return archive


@pytest.mark.parametrize("mismatch, error", [
    ("source", "source does not match its receipt"),
    ("target", "does not match"),
    ("codegen", "stale for current codegen"),
    ("threads", "does not match"),
    ("refcount", "does not match"),
    ("inventory", "does not match"),
    ("missing_config", "does not match"),
])
def test_container_native_helper_requires_matching_explicit_runtime(
    tmp_path, monkeypatch, container_runtime_model, mismatch, error,
):
    from pcc.frontends.python import pipeline_targets
    from pcc.tools import runtime_archive_provenance as provenance
    from tests.python import test_native_container_builtin_error_paths as container

    archive = container_runtime_model
    if mismatch == "source":
        (archive.parent / "py" / "probe.py").write_text("changed source\n")
    elif mismatch == "target":
        target = pipeline_targets.host_target_triple()
        other = "arm64-apple-darwin" if target != "arm64-apple-darwin" else "x86_64-unknown-linux-gnu"
        monkeypatch.setattr(pipeline_targets, "host_target_triple", lambda: other)
    elif mismatch == "codegen":
        monkeypatch.setattr(provenance, "codegen_checksum", lambda: "0" * 64)
    elif mismatch == "threads":
        monkeypatch.setenv("PCC_WITH_THREADS", "0")
    elif mismatch == "refcount":
        monkeypatch.setenv("PCC_REFCOUNT_KIND", "atomic")
    elif mismatch == "inventory":
        (archive.parent / "Makefile").write_text("PY_MODULES = probe missing\n")
    else:
        manifest_path = provenance.manifest_path_for_archive(archive)
        manifest = json.loads(manifest_path.read_text())
        for member in manifest["members"]:
            member.pop("runtime_build_config")
        manifest["members_sha256"] = provenance._members_sha256(manifest["members"])
        manifest_path.write_text(json.dumps(manifest))
    _forbid_native_work(monkeypatch)

    with pytest.raises(ValueError, match=error):
        container._run_native(tmp_path, "print(1)\n")
    assert not (tmp_path / "prog.py").exists()


@pytest.mark.parametrize("explicit", [False, True])
def test_container_native_helper_forwards_admitted_request(
    tmp_path, monkeypatch, request, explicit,
):
    from pcc.frontends.python import pipeline
    from tests.python import test_native_container_builtin_error_paths as container

    archive = request.getfixturevalue("container_runtime_model") if explicit else None
    if not explicit:
        monkeypatch.delenv("PCC_RUNTIME_ARCHIVE", raising=False)
        monkeypatch.delenv("PCC_TEST_NO_NATIVE_PROVISIONING", raising=False)
        monkeypatch.setattr(container, "_REPO_ROOT", tmp_path)
    _forbid_native_work(monkeypatch)
    calls = []
    result = subprocess.CompletedProcess([str(tmp_path / "prog.out")], 0, "recorded\n", "")

    def record_compile(*args, **kwargs):
        calls.append(("compile", args, kwargs))

    def record_run(*args, **kwargs):
        calls.append(("run", args, kwargs))
        return result

    monkeypatch.setattr(pipeline, "compile_python", record_compile)
    monkeypatch.setattr(subprocess, "run", record_run)
    assert container._run_native(tmp_path, "  print(1)\n") is result
    options = {"ir_scaffold_mode": "on", "libpython_mode": "off", "backend": "self"}
    if explicit:
        options["runtime_archive"] = str(archive.resolve())
    assert calls == [
        ("compile", (str(tmp_path / "prog.py"), str(tmp_path / "prog.out")), options),
        ("run", ([str(tmp_path / "prog.out")],),
         {"capture_output": True, "text": True, "timeout": 60}),
    ]
    assert (tmp_path / "prog.py").read_text() == "print(1)\n"


def test_container_host_ir_uses_private_temporary_directory(tmp_path, monkeypatch):
    from pcc.frontends.python import pipeline
    from tests.python import test_native_container_builtin_error_paths as container

    calls = []
    monkeypatch.setattr(container, "_REPO_ROOT", tmp_path)

    def record_compile(source, output, **kwargs):
        calls.append((Path(source), Path(output), kwargs))
        assert Path(source).read_text() == "print(1)\n"
        Path(output).write_text("host IR\n")

    monkeypatch.setattr(pipeline, "compile_python", record_compile)
    assert container._compile_to_ll("  print(1)\n", "probe") == "host IR\n"
    source, output, options = calls[0]
    assert source.parent == output.parent
    assert source.name == "probe.py" and output.name == "probe.ll"
    assert options == {
        "emit_llvm_only": True, "ir_scaffold_mode": "on", "libpython_mode": "off",
    }
    assert not source.parent.exists()
    assert not (tmp_path / "build").exists()
