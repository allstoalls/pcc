"""Execute provisioning boundaries while keeping rejected requests side-effect free."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from pcc.driver import native_provisioning
from pcc.frontends.python import (
    owned_runtime_build,
    pipeline,
    pipeline_runtime_archive,
    pipeline_targets,
)
from tests.python.test_native_provisioning_guard import container_runtime_model


_HELPERS = (
    "tests.python.test_async_await",
    "tests.python.test_context_manager_full",
    "tests.python.data_model.test_d2_d6_compiled_acceptance",
)


def _inventory(root):
    return {
        str(path.relative_to(root)): None if path.is_dir() else path.read_bytes()
        for path in root.rglob("*")
        if path.is_dir() or path.is_file()
    }


@pytest.fixture
def reserved_runtime(tmp_path, monkeypatch, request):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "Makefile").write_text("PY_MODULES = probe\n")
    monkeypatch.setattr(native_provisioning, "__file__", str(tmp_path / "pcc/driver/native_provisioning.py"))
    monkeypatch.delenv("PCC_RUNTIME_ARCHIVE", raising=False)
    monkeypatch.delenv("PCC_TEST_NO_NATIVE_PROVISIONING", raising=False)
    monkeypatch.setenv("PCC_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("PCC_RUNTIME_BUILD", "owned")
    monkeypatch.setenv("PCC_NO_AUTO_PCC1", "1")
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    monkeypatch.setenv("PCC_PY_FRONTEND_IR_CACHE", "off")
    monkeypatch.setenv("PCC_SELF_BACKEND_OBJECT_CACHE", "off")
    monkeypatch.setenv("PCC_HOST_PYTHON", "/nonexistent/host-python")
    monkeypatch.setenv("PCC_HOST_PCC", "/nonexistent/host-pcc")
    monkeypatch.setenv("PATH", "")
    if request.param == "environment":
        monkeypatch.setenv("PCC_TEST_NO_NATIVE_PROVISIONING", "1")
    else:
        (tmp_path / "build").mkdir()
        (tmp_path / "build" / native_provisioning.GUARD_NAME).write_text("reserved\n")
    return runtime


@pytest.mark.parametrize("reserved_runtime", ["environment", "file"], indirect=True)
@pytest.mark.parametrize("route", ["ensure", "build", "cli-build", "make", "make-selection", "compile", *_HELPERS])
def test_real_provisioning_routes_reject_before_runtime_output(tmp_path, monkeypatch, reserved_runtime, route):
    runtime = reserved_runtime
    output = tmp_path / "user"
    output.mkdir()
    src = output / "user.py"
    src.write_text("def main() -> None:\n    print(42)\n\nmain()\n")
    before = _inventory(tmp_path)
    if route == "make-selection":
        monkeypatch.setenv("PCC_RUNTIME_BUILD", "make")
    with pytest.raises(RuntimeError, match="automatic native test provisioning is disabled"):
        if route == "ensure":
            owned_runtime_build.ensure_target_runtime(str(runtime), "x86_64-unknown-linux-gnu")
        elif route == "build":
            owned_runtime_build.build_runtime_archive(str(runtime), str(runtime / "libpy_runtime_pcc_py.a"), "x86_64-unknown-linux-gnu")
        elif route == "cli-build":
            owned_runtime_build.main(["--runtime-dir", str(runtime), "--output", str(tmp_path / "new/libpy_runtime_pcc_py.a")])
        elif route == "make":
            pipeline_runtime_archive.run_runtime_make(str(runtime), ["/nonexistent/make"], verbose=False)
        elif route == "make-selection":
            pipeline._ensure_runtime(False)
        elif route == "compile":
            pipeline.compile_python(str(src), str(output / "program"), backend="self", libpython_mode="off", ir_scaffold_mode="on")
        else:
            importlib.import_module(route)._compile_and_run(output, src.read_text())
    assert _inventory(tmp_path) == before


@pytest.mark.parametrize("route", ["collection", "consumer"])
def test_stage1_provisioning_honors_shared_reservation(tmp_path, monkeypatch, route):
    from tests import conftest
    from tests.python import pcc1_gate

    monkeypatch.setenv("PCC_TEST_NO_NATIVE_PROVISIONING", "1")
    monkeypatch.delenv("PCC_NO_AUTO_PCC1", raising=False)
    monkeypatch.setattr(conftest, "_PCC1_PROVISIONED", False)
    monkeypatch.setattr(pcc1_gate, "_PROVISION_ATTEMPTED", False)
    before = _inventory(tmp_path)
    with pytest.raises(RuntimeError, match="automatic native test provisioning is disabled"):
        if route == "collection":
            conftest._provision_pcc1()
        else:
            pcc1_gate._provision_stage1_pcc1(tmp_path)
    assert _inventory(tmp_path) == before
    assert conftest._PCC1_PROVISIONED is False
    assert pcc1_gate._PROVISION_ATTEMPTED is False


@pytest.mark.parametrize("route", ["collection", "consumer"])
@pytest.mark.parametrize("value", ["1", "0", "false"])
def test_no_auto_pcc1_retains_nonempty_stage1_opt_out(tmp_path, monkeypatch, route, value):
    from tests import conftest
    from tests.python import pcc1_gate

    monkeypatch.setenv("PCC_NO_AUTO_PCC1", value)
    monkeypatch.setenv("PCC_TEST_NO_NATIVE_PROVISIONING", "1")
    monkeypatch.setattr(conftest, "_PCC1_PROVISIONED", False)
    monkeypatch.setattr(pcc1_gate, "_PROVISION_ATTEMPTED", False)
    if route == "collection":
        assert conftest._provision_pcc1() is None
    else:
        assert pcc1_gate._provision_stage1_pcc1(tmp_path) is False
    assert not list(tmp_path.iterdir())


def test_real_prebuilt_bundle_is_reused_while_provisioning_is_disabled(tmp_path, monkeypatch, container_runtime_model):
    """Tiny real owned-object bundle tests admission, not full runtime semantics."""
    archive = container_runtime_model
    monkeypatch.setenv("PCC_RUNTIME_DIR", str(archive.parent))
    monkeypatch.setenv("PCC_NO_AUTO_PCC1", "1")
    before = _inventory(tmp_path)
    selected = owned_runtime_build.ensure_target_runtime(
        str(archive.parent), pipeline_targets.host_target_triple(),
        explicit_archive=str(archive),
    )
    assert selected == str(archive)
    assert _inventory(tmp_path) == before


@pytest.mark.parametrize("helper", _HELPERS)
def test_helper_invalid_explicit_archive_fails_before_source_output(tmp_path, monkeypatch, helper):
    archive = tmp_path / "libpy_runtime_pcc_py.a"
    archive.write_bytes(b"not an archive")
    Path(str(archive) + ".provenance.json").write_text("{}\n")
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(archive))
    monkeypatch.setenv("PCC_TEST_NO_NATIVE_PROVISIONING", "1")
    before = _inventory(tmp_path)
    with pytest.raises(ValueError, match="invalid runtime archive manifest fields"):
        importlib.import_module(helper)._compile_and_run(tmp_path, "print(42)\n")
    assert _inventory(tmp_path) == before
