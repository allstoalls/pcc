from __future__ import annotations

import builtins
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import ModuleType, SimpleNamespace

import pytest
from pcc.frontends.python.pipeline_targets import host_target_triple

from pcc.frontends.python import pipeline
from pcc.frontends.python.pipeline_runtime_archive import (
    target_id as pipeline_runtime_target_id,
)
from pcc.tools.ir_to_obj import emit_object
from pcc.tools.runtime_archive_provenance import (
    ProvenanceError,
    assemble_runtime_archive_manifest,
    capi_inventory_path_for_archive,
    manifest_path_for_archive,
    verify_runtime_archive_manifest,
    write_pcc_python_receipt,
)
import tests.runtime_build_cache as runtime_build_cache

REPO = Path(__file__).resolve().parents[1]


def _write_valid_runtime_archive(
    runtime_root: Path,
    *,
    return_value: int = 1,
) -> Path:
    source = runtime_root / "py" / "cache_member.py"
    ir_path = runtime_root / "build_py" / "cache_member.ll"
    object_path = runtime_root / "build_py" / "cache_member.o"
    source.parent.mkdir(parents=True, exist_ok=True)
    object_path.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        f"def cache_member() -> int:\n    return {return_value}\n",
        encoding="utf-8",
    )
    target_triple = host_target_triple()
    ir_text = (
        f'target triple = "{target_triple}"\n'
        "define i32 @cache_member() {\n"
        "entry:\n"
        f"  ret i32 {return_value}\n"
        "}\n"
    )
    ir_path.write_text(ir_text, encoding="utf-8")
    object_bytes = emit_object(ir_text)
    object_path.write_bytes(object_bytes)
    write_pcc_python_receipt(
        object_path=object_path,
        ir_path=ir_path,
        source_path=source,
        runtime_root=runtime_root,
        target_triple=target_triple,
    )
    archive = runtime_root / "libpy_runtime_pcc_py.a"
    result = subprocess.run(
        ["ar", "rc", str(archive), str(object_path)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    capi_inventory_path_for_archive(archive).write_text(
        "PyRuntime_CacheAnchor\n",
        encoding="ascii",
    )
    assemble_runtime_archive_manifest(
        archive,
        [object_path],
        runtime_root=runtime_root,
    )
    return archive


def _configure_pcc_runtime_cache(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, Path]:
    # Pin the supported mode so these tests reach cache/provenance validation.
    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    source_runtime = tmp_path / "source" / "pcc" / "runtime"
    source_runtime.mkdir(parents=True)
    (source_runtime / "Makefile").write_text("all:\n\t@true\n", encoding="utf-8")
    repo_root = tmp_path / "repo"
    pcc_bin = repo_root / ".venv" / "bin" / "pcc"
    pcc_bin.parent.mkdir(parents=True)
    pcc_bin.write_text("", encoding="utf-8")
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(runtime_build_cache, "_RUNTIME_DIR", source_runtime)
    monkeypatch.setattr(runtime_build_cache, "_REPO_ROOT", repo_root)
    monkeypatch.setattr(
        runtime_build_cache,
        "_pcc_runtime_source_key",
        lambda _pcc_bin: "test-source-key",
    )
    key = runtime_build_cache._pcc_runtime_cache_key(
        pcc_bin, variant="pcc-py"
    )
    return (
        source_runtime,
        home
        / ".cache"
        / "pcc"
        / "test-artifacts"
        / "runtime-builds"
        / key,
    )


def _load_hatch_build(
    monkeypatch: pytest.MonkeyPatch,
    *,
    hook_path: Path | None = None,
) -> ModuleType:
    module_names = (
        "hatchling",
        "hatchling.builders",
        "hatchling.builders.hooks",
        "hatchling.builders.hooks.plugin",
        "hatchling.builders.hooks.plugin.interface",
    )
    modules = {name: ModuleType(name) for name in module_names}
    for name, module in modules.items():
        if name != module_names[-1]:
            module.__path__ = []

    class BuildHookInterface:
        pass

    modules[module_names[-1]].BuildHookInterface = BuildHookInterface
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    spec = importlib.util.spec_from_file_location(
        f"_test_hatch_build_{id(monkeypatch)}",
        hook_path or REPO / "hatch_build.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("build_version", ["editable", "standard"])
def test_hatch_build_loads_in_tree_provenance_with_source_absent_from_sys_path(
    monkeypatch: pytest.MonkeyPatch,
    build_version: str,
) -> None:
    real_import = builtins.__import__
    imported_pcc_names: list[str] = []

    def import_without_pcc(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "pcc" or name.startswith("pcc."):
            imported_pcc_names.append(name)
            error = ModuleNotFoundError("No module named 'pcc'")
            error.name = "pcc"
            raise error
        return real_import(name, globals, locals, fromlist, level)

    source_root = REPO.resolve()
    isolated_sys_path = []
    for entry in sys.path:
        candidate = Path(entry or ".").resolve()
        if candidate == source_root:
            continue
        isolated_sys_path.append(entry)
    monkeypatch.setattr(sys, "path", isolated_sys_path)
    monkeypatch.setattr(builtins, "__import__", import_without_pcc)
    monkeypatch.setenv("PCC_BUILD_SKIP", "1")

    hatch_module = _load_hatch_build(monkeypatch)
    hook = _new_build_hook(hatch_module, REPO)
    hook.initialize(build_version, {})

    assert imported_pcc_names == []
    assert source_root not in {
        Path(entry or ".").resolve() for entry in sys.path
    }
    assert Path(hatch_module._provenance.__file__).resolve() == (
        REPO / "pcc" / "tools" / "runtime_archive_provenance.py"
    ).resolve()
    assert hatch_module.ProvenanceError.__name__ == "ProvenanceError"
    assert callable(hatch_module.capi_inventory_path_for_archive)
    assert callable(hatch_module.manifest_path_for_archive)
    assert callable(hatch_module.verify_runtime_archive_manifest)


def test_hatch_build_fails_closed_when_in_tree_provenance_verifier_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isolated_hook = tmp_path / "source" / "hatch_build.py"
    isolated_hook.parent.mkdir(parents=True)
    shutil.copy2(REPO / "hatch_build.py", isolated_hook)

    with pytest.raises(
        RuntimeError,
        match="runtime archive provenance verifier is missing",
    ):
        _load_hatch_build(monkeypatch, hook_path=isolated_hook)


def test_hatch_verifies_archive_without_importing_an_installed_pcc(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"
    archive = _write_valid_runtime_archive(runtime_root)
    real_import = builtins.__import__

    def reject_public_pcc(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "pcc" or name.startswith("pcc."):
            raise AssertionError("isolated hook imported public pcc: " + name)
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", reject_public_pcc)
    module = _load_hatch_build(monkeypatch)
    result = module.verify_runtime_archive_manifest(archive, runtime_root=runtime_root)
    assert result["member_count"] == 1
    reader_source = module._provenance.read_members.__globals__["__file__"]
    assert Path(reader_source).resolve() == (REPO / "pcc/backend/ar.py").resolve()


def test_hatch_target_stamp_uses_owned_host_abi_without_cc(tmp_path, monkeypatch):
    module = _load_hatch_build(monkeypatch)
    hook = _new_build_hook(module, tmp_path)

    def unexpected_tool(*args, **kwargs):
        raise AssertionError("target identification must not invoke cc")

    monkeypatch.setattr(module.subprocess, "check_output", unexpected_tool)
    assert hook._archive_target_id() == pipeline._runtime_archive_target_id()


class _BuildApp:
    def __init__(self) -> None:
        self.info: list[str] = []
        self.warnings: list[str] = []

    def display_info(self, message: str) -> None:
        self.info.append(message)

    def display_warning(self, message: str) -> None:
        self.warnings.append(message)


def _new_build_hook(hatch_module: ModuleType, root: Path):
    hook = object.__new__(hatch_module.CustomBuildHook)
    hook.root = str(root)
    hook.app = _BuildApp()
    return hook


def test_pcc_runtime_cache_rebuilds_an_invalid_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, expected_runtime = _configure_pcc_runtime_cache(monkeypatch, tmp_path)
    builds: list[Path] = []

    def fake_make(command, **_kwargs):
        work_runtime = Path(command[2])
        builds.append(work_runtime)
        _write_valid_runtime_archive(
            work_runtime,
            return_value=len(builds),
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        runtime_build_cache,
        "subprocess",
        SimpleNamespace(run=fake_make),
    )

    first = runtime_build_cache.cached_pcc_python_runtime()
    manifest_path = manifest_path_for_archive(first / "libpy_runtime_pcc_py.a")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["untrusted_build_path"] = str(tmp_path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    second = runtime_build_cache.cached_pcc_python_runtime()

    assert first == second == expected_runtime
    assert len(builds) == 2
    verify_runtime_archive_manifest(
        second / "libpy_runtime_pcc_py.a",
        runtime_root=second,
    )


def test_pcc_runtime_cache_reuses_only_a_bound_verified_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_pcc_runtime_cache(monkeypatch, tmp_path)
    builds: list[Path] = []

    def fake_make(command, **_kwargs):
        work_runtime = Path(command[2])
        builds.append(work_runtime)
        _write_valid_runtime_archive(work_runtime)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    _install_mocked_cache_make(monkeypatch, fake_make)

    first = runtime_build_cache.cached_pcc_python_runtime()
    second = runtime_build_cache.cached_pcc_python_runtime()
    marker = json.loads((first / ".pcc-pcc-py-complete").read_text(encoding="utf-8"))

    assert first == second
    assert len(builds) == 1
    assert set(marker) == {
        "schema",
        "key",
        "archive_sha256",
        "manifest_sha256",
        "capi_inventory_sha256",
        "target_sha256",
    }
    assert marker["schema"] == "pcc.runtime-build-cache.v4"
    assert len(marker["archive_sha256"]) == 64
    assert len(marker["manifest_sha256"]) == 64
    assert len(marker["capi_inventory_sha256"]) == 64
    assert len(marker["target_sha256"]) == 64
    archive = first / "libpy_runtime_pcc_py.a"
    manifest = verify_runtime_archive_manifest(archive, runtime_root=first)
    target = Path(str(archive) + ".target")
    assert target.read_text(encoding="utf-8") == (
        pipeline_runtime_target_id(str(manifest["target_triple"])) + "\n"
    )


def _install_mocked_cache_make(monkeypatch, fake_make) -> None:
    real_admission = runtime_build_cache.require_native_provisioning_allowed
    monkeypatch.setattr(runtime_build_cache, "subprocess", SimpleNamespace(run=fake_make))

    def admit_only_installed_fake_make() -> None:
        if getattr(runtime_build_cache.subprocess, "run", None) is not fake_make:
            real_admission()

    # Only this already-installed fake is admitted; real provisioning retains
    # the original guard, including the host CI job's no-native reservation.
    monkeypatch.setattr(
        runtime_build_cache, "require_native_provisioning_allowed",
        admit_only_installed_fake_make,
    )


def test_pcc_runtime_cache_mock_admission_preserves_unmocked_provisioning_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pcc.driver.native_provisioning import NativeProvisioningError

    _, expected_runtime = _configure_pcc_runtime_cache(monkeypatch, tmp_path)
    monkeypatch.setenv("PCC_TEST_NO_NATIVE_PROVISIONING", "1")
    calls = []

    def fake_make(*args, **kwargs):
        calls.append("fake")
        pytest.fail("the restored subprocess must not reach the old fake Make")

    _install_mocked_cache_make(monkeypatch, fake_make)
    monkeypatch.setattr(runtime_build_cache, "subprocess", subprocess)

    def unexpected_make(*args, **kwargs):
        calls.append("real")
        pytest.fail("unmocked provisioning reached Make before admission")

    # Observe the real subprocess boundary without allowing a Make invocation
    # even if admission regresses. This is not the registered fake callable.
    monkeypatch.setattr(subprocess, "run", unexpected_make)
    with pytest.raises(NativeProvisioningError, match="automatic native test provisioning is disabled"):
        runtime_build_cache.cached_pcc_python_runtime()
    assert calls == []
    assert not expected_runtime.exists()


def test_pcc_runtime_cache_format_change_preserves_the_old_immutable_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, expected_runtime = _configure_pcc_runtime_cache(monkeypatch, tmp_path)
    pcc_bin = runtime_build_cache._REPO_ROOT / ".venv" / "bin" / "pcc"
    old_key = runtime_build_cache._pcc_runtime_source_key(pcc_bin) + "-pcc-py"
    old_runtime = expected_runtime.parent / old_key
    old_runtime.mkdir(parents=True)
    sentinel = old_runtime / "immutable-v2-sentinel"
    sentinel.write_text("old-cache\n", encoding="utf-8")
    builds: list[Path] = []

    def fake_make(command, **_kwargs):
        work_runtime = Path(command[2])
        builds.append(work_runtime)
        _write_valid_runtime_archive(work_runtime)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        runtime_build_cache,
        "subprocess",
        SimpleNamespace(run=fake_make),
    )

    runtime = runtime_build_cache.cached_pcc_python_runtime()

    assert runtime == expected_runtime
    assert runtime != old_runtime
    assert builds
    assert sentinel.read_text(encoding="utf-8") == "old-cache\n"


@pytest.mark.parametrize("replacement", [None, "wrong-target\n"])
def test_pcc_runtime_cache_rebuilds_a_missing_or_replaced_target_stamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: str | None,
) -> None:
    _configure_pcc_runtime_cache(monkeypatch, tmp_path)
    builds: list[Path] = []

    def fake_make(command, **_kwargs):
        work_runtime = Path(command[2])
        builds.append(work_runtime)
        _write_valid_runtime_archive(work_runtime, return_value=len(builds))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        runtime_build_cache,
        "subprocess",
        SimpleNamespace(run=fake_make),
    )

    runtime = runtime_build_cache.cached_pcc_python_runtime()
    target = Path(str(runtime / "libpy_runtime_pcc_py.a") + ".target")
    if replacement is None:
        target.unlink()
    else:
        target.write_text(replacement, encoding="utf-8")

    rebuilt = runtime_build_cache.cached_pcc_python_runtime()

    assert rebuilt == runtime
    assert len(builds) == 2
    manifest = verify_runtime_archive_manifest(
        rebuilt / "libpy_runtime_pcc_py.a", runtime_root=rebuilt
    )
    assert target.read_text(encoding="utf-8") == (
        pipeline_runtime_target_id(str(manifest["target_triple"])) + "\n"
    )


def test_pcc_runtime_cache_marker_binds_manifest_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_pcc_runtime_cache(monkeypatch, tmp_path)
    builds: list[Path] = []

    def fake_make(command, **_kwargs):
        work_runtime = Path(command[2])
        builds.append(work_runtime)
        _write_valid_runtime_archive(work_runtime, return_value=len(builds))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        runtime_build_cache,
        "subprocess",
        SimpleNamespace(run=fake_make),
    )

    runtime = runtime_build_cache.cached_pcc_python_runtime()
    manifest_path = manifest_path_for_archive(runtime / "libpy_runtime_pcc_py.a")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_path.write_text(
        json.dumps(manifest, separators=(",", ":")), encoding="utf-8"
    )
    verify_runtime_archive_manifest(
        runtime / "libpy_runtime_pcc_py.a",
        runtime_root=runtime,
    )

    runtime_build_cache.cached_pcc_python_runtime()

    assert len(builds) == 2


def test_pcc_runtime_cache_marker_binds_archive_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_pcc_runtime_cache(monkeypatch, tmp_path)
    builds: list[Path] = []

    def fake_make(command, **_kwargs):
        work_runtime = Path(command[2])
        builds.append(work_runtime)
        _write_valid_runtime_archive(work_runtime, return_value=len(builds))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        runtime_build_cache,
        "subprocess",
        SimpleNamespace(run=fake_make),
    )

    runtime = runtime_build_cache.cached_pcc_python_runtime()
    archive = _write_valid_runtime_archive(runtime, return_value=99)
    verify_runtime_archive_manifest(archive, runtime_root=runtime)

    runtime_build_cache.cached_pcc_python_runtime()

    assert len(builds) == 2


def test_pcc_runtime_cache_does_not_publish_before_manifest_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, expected_runtime = _configure_pcc_runtime_cache(monkeypatch, tmp_path)

    def fake_make(command, **_kwargs):
        work_runtime = Path(command[2])
        archive = _write_valid_runtime_archive(work_runtime)
        manifest_path_for_archive(archive).unlink()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        runtime_build_cache,
        "subprocess",
        SimpleNamespace(run=fake_make),
    )

    with pytest.raises(ProvenanceError, match="cannot read provenance JSON"):
        runtime_build_cache.cached_pcc_python_runtime()

    assert not expected_runtime.exists()
    marker_name = ".pcc-pcc-py-complete"
    cache_root = expected_runtime.parent
    assert not list(cache_root.rglob(marker_name))
    assert not [path for path in cache_root.iterdir() if path.is_dir()]


def test_pcc_runtime_cache_rebuilds_a_replaced_capi_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, expected_runtime = _configure_pcc_runtime_cache(monkeypatch, tmp_path)
    builds: list[Path] = []

    def fake_make(command, **_kwargs):
        work_runtime = Path(command[2])
        builds.append(work_runtime)
        _write_valid_runtime_archive(work_runtime, return_value=len(builds))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        runtime_build_cache,
        "subprocess",
        SimpleNamespace(run=fake_make),
    )

    first = runtime_build_cache.cached_pcc_python_runtime()
    archive = first / "libpy_runtime_pcc_py.a"
    capi_inventory_path_for_archive(archive).write_text(
        "PyRuntime_ReplacedCacheAnchor\n",
        encoding="ascii",
    )

    second = runtime_build_cache.cached_pcc_python_runtime()

    assert first == second == expected_runtime
    assert len(builds) == 2
    verify_runtime_archive_manifest(
        second / "libpy_runtime_pcc_py.a",
        runtime_root=second,
    )


def test_compiler_anchor_consumer_requires_the_verified_inventory_bundle(
    tmp_path: Path,
) -> None:
    archive = _write_valid_runtime_archive(tmp_path / "pcc" / "runtime")

    assert pipeline._capi_export_anchor_symbols(str(archive)) == [
        "PyRuntime_CacheAnchor"
    ]

    capi_inventory_path_for_archive(archive).write_text(
        "PyRuntime_Truncated\n",
        encoding="ascii",
    )
    with pytest.raises(pipeline.PyPipelineError, match="C-API inventory bundle"):
        pipeline._capi_export_anchor_symbols(str(archive))


def test_pcc_runtime_cache_does_not_copy_an_old_archive_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_runtime, _ = _configure_pcc_runtime_cache(monkeypatch, tmp_path)
    stale_sidecar = source_runtime / "libpy_runtime_pcc_py.a.provenance.json"
    stale_sidecar.write_text('{"stale": true}\n', encoding="utf-8")
    stale_capi_inventory = (
        source_runtime / "libpy_runtime_pcc_py.a.capi_syms"
    )
    stale_capi_inventory.write_text(
        "PyRuntime_StaleCacheAnchor\n",
        encoding="ascii",
    )
    stale_target = source_runtime / "libpy_runtime_pcc_py.a.target"
    stale_target.write_text("stale-target\n", encoding="utf-8")

    def fake_make(command, **_kwargs):
        work_runtime = Path(command[2])
        assert not (work_runtime / stale_sidecar.name).exists()
        assert not (work_runtime / stale_capi_inventory.name).exists()
        assert not (work_runtime / stale_target.name).exists()
        _write_valid_runtime_archive(work_runtime)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(
        runtime_build_cache,
        "subprocess",
        SimpleNamespace(run=fake_make),
    )

    runtime_build_cache.cached_pcc_python_runtime()


def test_hatch_freshness_requires_an_adjacent_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hatch_module = _load_hatch_build(monkeypatch)
    root = tmp_path / "source"
    runtime_root = root / "pcc" / "runtime"
    archive = _write_valid_runtime_archive(runtime_root)
    manifest_path_for_archive(archive).unlink()
    hook = _new_build_hook(hatch_module, root)

    assert hook._runtime_archive_inputs_newer(root, archive)


@pytest.mark.parametrize("failed_phase", ["runtime", "compiler"])
def test_hatch_self_build_failure_never_retries_an_external_backend(
    tmp_path, monkeypatch, failed_phase,
):
    hatch_module = _load_hatch_build(monkeypatch)
    root = tmp_path / "source"
    archive = _write_valid_runtime_archive(root / "pcc" / "runtime")
    hook = _new_build_hook(hatch_module, root)
    attempts = []

    def build_runtime(_runtime, _target, backend, **kwargs):
        attempts.append(("runtime", backend))
        return failed_phase != "runtime"

    def build_compiler(_root, _output, backend):
        attempts.append(("compiler", backend))
        return False

    monkeypatch.setattr(hook, "_discard_wrong_target_archives", lambda _root: None)
    monkeypatch.setattr(hook, "_archive_target_id", lambda: "test-target")
    monkeypatch.setattr(hook, "_run_make", build_runtime)
    monkeypatch.setattr(hook, "_run_pcc_self_compile", build_compiler)
    monkeypatch.setenv("PCC_BUILD_BACKEND", "self")
    for name in ("PCC_BUILD_SKIP", "PCC_BUILD_PCC1", "PCC_BUILD_TARGET"):
        monkeypatch.delenv(name, raising=False)
    build_data = {}
    with pytest.raises(RuntimeError):
        hook.initialize("standard", build_data)
    expected = [("runtime", "self")]
    if failed_phase == "compiler":
        expected.append(("compiler", "self"))
    assert attempts == expected
    assert "shared_scripts" not in build_data
    assert archive.is_file()


def test_hatch_freshness_rejects_invalid_but_accepts_valid_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hatch_module = _load_hatch_build(monkeypatch)
    root = tmp_path / "source"
    runtime_root = root / "pcc" / "runtime"
    archive = _write_valid_runtime_archive(runtime_root)
    hook = _new_build_hook(hatch_module, root)

    assert not hook._runtime_archive_inputs_newer(root, archive)

    manifest_path = manifest_path_for_archive(archive)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["unexpected"] = "field"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert hook._runtime_archive_inputs_newer(root, archive)


def test_hatch_refuses_to_publish_after_make_without_valid_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hatch_module = _load_hatch_build(monkeypatch)
    root = tmp_path / "source"
    runtime_root = root / "pcc" / "runtime"
    archive = _write_valid_runtime_archive(runtime_root)
    manifest_path_for_archive(archive).unlink()
    prebuilt = tmp_path / "pcc1"
    prebuilt.write_text("native", encoding="utf-8")
    prebuilt.chmod(0o755)
    hook = _new_build_hook(hatch_module, root)
    monkeypatch.setattr(hook, "_discard_wrong_target_archives", lambda _root: None)
    monkeypatch.setattr(hook, "_run_make", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(hook, "_archive_target_id", lambda: "test-target")
    monkeypatch.setattr(hook, "_validate_prebuilt_pcc1", lambda _path: True)
    monkeypatch.setenv("PCC_BUILD_PCC1", str(prebuilt))
    monkeypatch.delenv("PCC_BUILD_SKIP", raising=False)
    monkeypatch.delenv("PCC_BUILD_TARGET", raising=False)
    build_data: dict[str, object] = {}

    with pytest.raises(RuntimeError, match="provenance"):
        hook.initialize("standard", build_data)

    assert "force_include" not in build_data
    assert not Path(str(archive) + ".target").exists()


def test_hatch_refuses_to_publish_after_make_without_capi_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hatch_module = _load_hatch_build(monkeypatch)
    root = tmp_path / "source"
    runtime_root = root / "pcc" / "runtime"
    archive = _write_valid_runtime_archive(runtime_root)
    capi_inventory_path_for_archive(archive).unlink()
    prebuilt = tmp_path / "pcc1"
    prebuilt.write_text("native", encoding="utf-8")
    prebuilt.chmod(0o755)
    hook = _new_build_hook(hatch_module, root)
    monkeypatch.setattr(hook, "_discard_wrong_target_archives", lambda _root: None)
    monkeypatch.setattr(hook, "_run_make", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(hook, "_archive_target_id", lambda: "test-target")
    monkeypatch.setattr(hook, "_validate_prebuilt_pcc1", lambda _path: True)
    monkeypatch.setenv("PCC_BUILD_PCC1", str(prebuilt))
    monkeypatch.delenv("PCC_BUILD_SKIP", raising=False)
    monkeypatch.delenv("PCC_BUILD_TARGET", raising=False)
    build_data: dict[str, object] = {}

    with pytest.raises(RuntimeError, match="C-API anchor inventory"):
        hook.initialize("standard", build_data)

    assert "force_include" not in build_data
    assert not Path(str(archive) + ".target").exists()


def test_hatch_force_includes_the_verified_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hatch_module = _load_hatch_build(monkeypatch)
    root = tmp_path / "source"
    runtime_root = root / "pcc" / "runtime"
    archive = _write_valid_runtime_archive(runtime_root)
    manifest = manifest_path_for_archive(archive)
    prebuilt = tmp_path / "pcc1"
    prebuilt.write_text("native", encoding="utf-8")
    prebuilt.chmod(0o755)
    hook = _new_build_hook(hatch_module, root)
    monkeypatch.setattr(hook, "_discard_wrong_target_archives", lambda _root: None)
    monkeypatch.setattr(hook, "_run_make", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(hook, "_archive_target_id", lambda: "test-target")
    monkeypatch.setattr(hook, "_validate_prebuilt_pcc1", lambda _path: True)
    monkeypatch.setenv("PCC_BUILD_PCC1", str(prebuilt))
    monkeypatch.delenv("PCC_BUILD_SKIP", raising=False)
    monkeypatch.delenv("PCC_BUILD_TARGET", raising=False)
    build_data: dict[str, object] = {}

    hook.initialize("standard", build_data)

    assert build_data["force_include"][str(manifest)] == (
        "pcc/runtime/libpy_runtime_pcc_py.a.provenance.json"
    )
    capi_inventory = capi_inventory_path_for_archive(archive)
    assert build_data["force_include"][str(capi_inventory)] == (
        "pcc/runtime/libpy_runtime_pcc_py.a.capi_syms"
    )
    wheel_markers = [
        Path(source)
        for source, destination in build_data["force_include"].items()
        if destination == "pcc/runtime/libpy_runtime_pcc_py.a.wheel"
    ]
    assert len(wheel_markers) == 1
    marker_lines = wheel_markers[0].read_text(encoding="utf-8").splitlines()
    assert marker_lines[0] == "pcc.runtime-wheel-artifact.v2"
    assert marker_lines[1] == "target=test-target"
    from pcc.frontends.python.pipeline_runtime_archive import wheel_stamp_matches

    installed_marker = Path(str(archive) + ".wheel")
    installed_marker.write_bytes(wheel_markers[0].read_bytes())
    assert wheel_stamp_matches(str(archive), "test-target")


def test_hatch_wrong_target_cleanup_removes_the_manifest_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hatch_module = _load_hatch_build(monkeypatch)
    root = tmp_path / "source"
    runtime_root = root / "pcc" / "runtime"
    archive = _write_valid_runtime_archive(runtime_root)
    manifest = manifest_path_for_archive(archive)
    capi_inventory = capi_inventory_path_for_archive(archive)
    target_stamp = Path(str(archive) + ".target")
    target_stamp.write_text("wrong-target\n", encoding="utf-8")
    hook = _new_build_hook(hatch_module, root)
    monkeypatch.setattr(hook, "_archive_target_id", lambda: "wanted-target")

    hook._discard_wrong_target_archives(runtime_root)

    assert not archive.exists()
    assert not target_stamp.exists()
    assert not manifest.exists()
    assert not capi_inventory.exists()


@pytest.mark.parametrize(
    "damage",
    ["missing", "invalid", "capi-missing", "capi-replaced"],
)
def test_hatch_forces_rebuild_for_untrusted_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    damage: str,
) -> None:
    hatch_module = _load_hatch_build(monkeypatch)
    root = tmp_path / "source"
    runtime_root = root / "pcc" / "runtime"
    archive = _write_valid_runtime_archive(runtime_root)
    manifest = manifest_path_for_archive(archive)
    if damage == "missing":
        manifest.unlink()
    elif damage == "invalid":
        manifest.write_text('{"invalid": true}\n', encoding="utf-8")
    elif damage == "capi-missing":
        capi_inventory_path_for_archive(archive).unlink()
    else:
        capi_inventory_path_for_archive(archive).write_text(
            "PyRuntime_ReplacedWheelAnchor\n",
            encoding="ascii",
        )
    prebuilt = tmp_path / "pcc1"
    prebuilt.write_text("native", encoding="utf-8")
    prebuilt.chmod(0o755)
    hook = _new_build_hook(hatch_module, root)
    forces: list[bool] = []

    def fake_make(_runtime_dir, _target, _backend, *, force=False):
        forces.append(force)
        _write_valid_runtime_archive(runtime_root, return_value=2)
        return True

    monkeypatch.setattr(hook, "_discard_wrong_target_archives", lambda _root: None)
    monkeypatch.setattr(hook, "_run_make", fake_make)
    monkeypatch.setattr(hook, "_archive_target_id", lambda: "test-target")
    monkeypatch.setattr(hook, "_validate_prebuilt_pcc1", lambda _path: True)
    monkeypatch.setenv("PCC_BUILD_PCC1", str(prebuilt))
    monkeypatch.delenv("PCC_BUILD_SKIP", raising=False)
    monkeypatch.delenv("PCC_BUILD_TARGET", raising=False)
    build_data: dict[str, object] = {}

    hook.initialize("standard", build_data)

    assert forces == [True]
    verify_runtime_archive_manifest(archive, runtime_root=runtime_root)
    assert str(manifest) in build_data["force_include"]
