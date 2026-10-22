"""Shared helpers for full three-stage bootstrap GC gates.

Each file under ``tests/python/gc/test_pcc_bootstrap_full_gc*.py`` runs one GC
backend's real self-host chain. One backend-agnostic ``pcc1`` is built once per
pytest session and shared, then that GC file runs ``pcc1 -> pcc2 -> pcc3`` via
``bootstrap.py --reuse-stage1`` and checks stages present, no libpython linkage,
and ``pcc2 == pcc3`` in their original bytes.

Speed comes from sharing stage1 and content-addressed completed backend
results, never from accepting partial evidence. ``pcc1`` does not depend on
``PCC_GC_BACKEND`` because the collector is selected at stage2+ runtime. A
backend success manifest is written only after its real ``pcc1 -> pcc2 ->
pcc3`` chain, no-libpython checks, publish barriers, and raw-byte fixed-point
comparison pass. Interrupted runs can validate and resume those complete
same-source results; stale, partial, or mismatched results rebuild.

Set ``PCC_BOOTSTRAP_FULL_REBUILD=1`` to force fresh stage1 and backend builds
even when content-addressed completed results exist.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
import functools
import hashlib
import json
import os
import signal
import subprocess

from pcc.diagnostics.dependency_verdict import probe_platform_capability
from pcc.driver.bootstrap_cache_identity import (
    bootstrap_object_cache_identity,
    bootstrap_source_files,
    bootstrap_source_sha256,
)
import shutil
import sys
import time
from typing import Any
from pathlib import Path

import pytest

from pcc.diagnostics.bootstrap_profile_report import build_bootstrap_profile_report
from tests.python.test_bootstrap_gate_baseline import (
    _REPO_ROOT,
    _byte_identical,
    _is_macos_arm64,
    _links_libpython,
)
from tests.python.process_timeout import (
    _kill_process_group,
    run_process_group_timeout,
)

_BOOTSTRAP_PY = _REPO_ROOT / "scripts" / "bootstrap.py"
_PROCESS_TREE_SAMPLER = _REPO_ROOT / "scripts" / "run_process_tree_sample.py"
_SHARED_STAGE1_DIR = _REPO_ROOT / "build" / "bootstrap-pytest-shared-stage1"
_SHARED_STAGE1_LOCK = _REPO_ROOT / "build" / "bootstrap-pytest-shared-stage1.lock"
_SHARED_STAGE1_REBUILD_STAMP = _SHARED_STAGE1_DIR / ".rebuild-run-id"
_BOOTSTRAP_RESOURCE_POOL_DIR = _REPO_ROOT / "build" / "bootstrap-pytest-resource-pool"
_BOOTSTRAP_RESOURCE_POOL_LOCK = (
    _REPO_ROOT / "build" / "bootstrap-pytest-resource-pool.lock"
)
_BOOTSTRAP_OBJECT_CACHE_DIR = _REPO_ROOT / "build" / "bootstrap-pytest-object-cache"
_BOOTSTRAP_HELPER_IMPORT_TIME = time.time()
_GC_BACKENDS = ("0", "1", "2", "3", "4")
_BOOTSTRAP_SUCCESS_SCHEMA = "pcc.bootstrap_full.backend_success.v1"
_BOOTSTRAP_SUCCESS_MANIFEST = "backend-success.json"
_BOOTSTRAP_STAGE2_SUCCESS_SCHEMA = "pcc.bootstrap_full.stage2_success.v1"
_BOOTSTRAP_STAGE2_SUCCESS_MANIFEST = "stage2-success.json"
_GC_BOOTSTRAP_WEIGHT = {"0": 60, "4": 50, "3": 40, "1": 30, "2": 30}
# Full bootstrap chains are multi-gigabyte workloads.  A retained Jetsam/panic
# receipt showed that concurrent pcc1 chains can exhaust compressor segments
# and swap, so the default and ordinary environment overrides stay sequential.
_GC_BOOTSTRAP_MAX_ACTIVE_BACKENDS = 1
_GC_BOOTSTRAP_MAX_FRONTEND_JOBS = 2
_GC_BOOTSTRAP_MAX_SELF_BACKEND_JOBS = 2
_GC_BOOTSTRAP_PARALLEL_MIN_JOBS = 2
_BOOTSTRAP_MAX_TREE_RSS_BYTES = 8 * 1024 * 1024 * 1024
_BOOTSTRAP_STAGE_TIMEOUT_S = 600


@dataclass(frozen=True)
class BootstrapMatrixPlan:
    backends: tuple[str, ...]
    max_workers: int
    frontend_jobs: int
    self_backend_jobs: int
    cpu_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class BootstrapBackendResult:
    gc_backend: str
    out_dir: Path
    elapsed_s: float
    process: subprocess.CompletedProcess[str]
    profile_dir: Path
    profile_report: dict[str, Any] | None
    profile_error: str | None = None


def _stage_bin(out_dir: Path, stage: int) -> Path:
    return out_dir / f"pcc{stage}"


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _bootstrap_out_dir(gc_backend: str) -> Path:
    if gc_backend == "0":
        # Preserve the canonical pcc1 path consumed by direct pcc1 smoke/package
        # tests while making challenger-backend stage artifacts independent.
        return _REPO_ROOT / "build" / "bootstrap-pytest-self"
    return _REPO_ROOT / "build" / f"bootstrap-pytest-self-gc{gc_backend}"


def _bootstrap_profile_dir(gc_backend: str) -> Path:
    return _bootstrap_out_dir(gc_backend) / "profile"


def _bootstrap_success_manifest_path(gc_backend: str) -> Path:
    return _bootstrap_out_dir(gc_backend) / _BOOTSTRAP_SUCCESS_MANIFEST


def _bootstrap_stage2_success_manifest_path(gc_backend: str) -> Path:
    return _bootstrap_out_dir(gc_backend) / _BOOTSTRAP_STAGE2_SUCCESS_MANIFEST


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


@functools.lru_cache(maxsize=1)
def _bootstrap_source_files() -> tuple[Path, ...]:
    return bootstrap_source_files(_REPO_ROOT)


@functools.lru_cache(maxsize=1)
def _bootstrap_source_sha256() -> str:
    return bootstrap_source_sha256(_REPO_ROOT)


@functools.lru_cache(maxsize=1)
def _bootstrap_self_backend_object_identity() -> str:
    return bootstrap_object_cache_identity(_REPO_ROOT)


def _prepare_bootstrap_profile_dir(gc_backend: str) -> Path:
    profile_dir = _bootstrap_profile_dir(gc_backend)
    profile_dir.mkdir(parents=True, exist_ok=True)
    for pattern in ("stage*.json", "stage*.time"):
        for path in profile_dir.glob(pattern):
            if path.is_file():
                path.unlink()
    return profile_dir


def _prepare_bootstrap_stage_profile(profile_dir: Path, stage: int) -> None:
    for suffix in ("json", "time", "result.json"):
        path = profile_dir / f"stage{stage}.{suffix}"
        with contextlib.suppress(OSError):
            path.unlink()


def _path_record(path: Path) -> dict[str, object]:
    return {
        "path": str(path.resolve()),
        "size": path.stat().st_size,
        "sha256": _sha256_path(path),
    }


def _bootstrap_success_inputs(
    gc_backend: str,
    shared_pcc1: Path,
    runtime_archive: Path,
) -> dict[str, object]:
    return {
        "backend": "self",
        "gc_backend": str(gc_backend),
        "platform": sys.platform,
        "machine": os.uname().machine,
        "source_sha256": _bootstrap_source_sha256(),
        "shared_pcc1": _path_record(shared_pcc1),
        "runtime_archive": _path_record(runtime_archive),
        "runtime_cc": os.environ.get("PCC_BOOTSTRAP_RUNTIME_CC", "pcc"),
        "runtime_high": os.environ.get("PCC_BOOTSTRAP_RUNTIME_HIGH", "py"),
        "python_libpython": os.environ.get("PCC_BOOTSTRAP_PYTHON_LIBPYTHON", "off"),
        "python_ir_passes": os.environ.get(
            "PCC_BOOTSTRAP_PYTHON_IR_PASSES",
            os.environ.get("PCC_PYTHON_IR_PASSES", "off"),
        ),
        "ir_scaffold": "on",
    }


def _successful_stage_result_record(
    profile_dir: Path,
    stage: int,
    output: Path,
) -> dict[str, object]:
    result_path = profile_dir / f"stage{stage}.result.json"
    assert result_path.is_file(), f"stage {stage} result profile is missing"
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AssertionError(f"stage {stage} result profile is invalid: {exc}") from exc
    assert result.get("schema") == "pcc.bootstrap_stage_result.v1"
    assert result.get("backend") == "self"
    assert result.get("stage") == stage
    assert result.get("returncode") == 0
    assert result.get("publish_barrier_returncode") == 0
    assert Path(str(result.get("output", ""))).resolve() == output.resolve()
    return _path_record(result_path)


def _completed_backend_output_records(
    gc_backend: str,
    shared_pcc1: Path,
) -> dict[str, object]:
    out_dir = _bootstrap_out_dir(gc_backend)
    pcc1 = _stage_bin(out_dir, 1)
    pcc2 = _stage_bin(out_dir, 2)
    pcc3 = _stage_bin(out_dir, 3)
    for stage, binary in ((1, pcc1), (2, pcc2), (3, pcc3)):
        assert binary.is_file() and os.access(
            binary, os.X_OK
        ), f"pcc{stage} missing or not executable under GC {gc_backend}"
        assert not _links_libpython(
            binary
        ), f"pcc{stage} links libpython under GC {gc_backend}"
    pcc1_record = _path_record(pcc1)
    shared_pcc1_record = _path_record(shared_pcc1)
    assert (
        pcc1_record["size"] == shared_pcc1_record["size"]
        and pcc1_record["sha256"] == shared_pcc1_record["sha256"]
    ), f"backend GC {gc_backend} pcc1 does not match the shared stage1 input"
    assert _byte_identical(pcc2, pcc3), (
        "Self-host determinism failed under "
        f"PCC_GC_BACKEND={gc_backend}: pcc2 and pcc3 are not "
        "byte-identical in their original bytes"
    )
    profile_dir = _bootstrap_profile_dir(gc_backend)
    return {
        "pcc1": pcc1_record,
        "pcc2": _path_record(pcc2),
        "pcc3": _path_record(pcc3),
        "stage2_result": _successful_stage_result_record(profile_dir, 2, pcc2),
        "stage3_result": _successful_stage_result_record(profile_dir, 3, pcc3),
        # Historical schema key; the check above requires strict raw-byte equality.
        "normalized_pcc2_pcc3_equal": True,
        "links_libpython": False,
    }


def _completed_stage2_output_records(
    gc_backend: str,
    shared_pcc1: Path,
) -> dict[str, object]:
    out_dir = _bootstrap_out_dir(gc_backend)
    pcc1 = _stage_bin(out_dir, 1)
    pcc2 = _stage_bin(out_dir, 2)
    for stage, binary in ((1, pcc1), (2, pcc2)):
        assert binary.is_file() and os.access(
            binary, os.X_OK
        ), f"pcc{stage} missing or not executable under GC {gc_backend}"
        assert not _links_libpython(
            binary
        ), f"pcc{stage} links libpython under GC {gc_backend}"
    pcc1_record = _path_record(pcc1)
    shared_pcc1_record = _path_record(shared_pcc1)
    assert (
        pcc1_record["size"] == shared_pcc1_record["size"]
        and pcc1_record["sha256"] == shared_pcc1_record["sha256"]
    ), f"backend GC {gc_backend} pcc1 does not match the shared stage1 input"
    return {
        "pcc1": pcc1_record,
        "pcc2": _path_record(pcc2),
        "stage2_result": _successful_stage_result_record(
            _bootstrap_profile_dir(gc_backend), 2, pcc2
        ),
        "links_libpython": False,
    }


def _stage2_success_inputs(
    gc_backend: str,
    shared_pcc1: Path,
    runtime_archive: Path,
    plan: BootstrapMatrixPlan,
) -> dict[str, object]:
    inputs = _bootstrap_success_inputs(gc_backend, shared_pcc1, runtime_archive)
    inputs["execution_plan"] = {
        "frontend_jobs": plan.frontend_jobs,
        "self_backend_jobs": plan.self_backend_jobs,
        "cpu_ids": list(plan.cpu_ids),
    }
    return inputs


def _write_bootstrap_stage2_success_manifest(
    gc_backend: str,
    shared_pcc1: Path,
    runtime_archive: Path,
    plan: BootstrapMatrixPlan,
) -> Path:
    path = _bootstrap_stage2_success_manifest_path(gc_backend)
    payload = {
        "schema": _BOOTSTRAP_STAGE2_SUCCESS_SCHEMA,
        "inputs": _stage2_success_inputs(
            gc_backend, shared_pcc1, runtime_archive, plan
        ),
        "outputs": _completed_stage2_output_records(gc_backend, shared_pcc1),
        "completed_at_unix_s": time.time(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()
    return path


def _reuse_bootstrap_stage2_success_manifest(
    gc_backend: str,
    shared_pcc1: Path,
    runtime_archive: Path,
    plan: BootstrapMatrixPlan,
) -> tuple[bool, str]:
    if _env_truthy("PCC_BOOTSTRAP_FULL_REBUILD"):
        return False, "PCC_BOOTSTRAP_FULL_REBUILD forces a fresh stage2"
    path = _bootstrap_stage2_success_manifest_path(gc_backend)
    if not path.is_file():
        return False, "no completed stage2 manifest"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"invalid completed stage2 manifest: {exc}"
    if payload.get("schema") != _BOOTSTRAP_STAGE2_SUCCESS_SCHEMA:
        return False, "completed stage2 manifest schema mismatch"
    try:
        expected_inputs = _stage2_success_inputs(
            gc_backend, shared_pcc1, runtime_archive, plan
        )
        if payload.get("inputs") != expected_inputs:
            return False, "completed stage2 input fingerprint mismatch"
        expected_outputs = _completed_stage2_output_records(gc_backend, shared_pcc1)
        if payload.get("outputs") != expected_outputs:
            return False, "completed stage2 output fingerprint mismatch"
    except (AssertionError, OSError) as exc:
        return False, f"completed stage2 verification failed: {exc}"
    return True, "complete same-source stage2 result"


def _write_bootstrap_success_manifest(
    gc_backend: str,
    shared_pcc1: Path,
    runtime_archive: Path,
) -> Path:
    path = _bootstrap_success_manifest_path(gc_backend)
    payload = {
        "schema": _BOOTSTRAP_SUCCESS_SCHEMA,
        "inputs": _bootstrap_success_inputs(gc_backend, shared_pcc1, runtime_archive),
        "outputs": _completed_backend_output_records(gc_backend, shared_pcc1),
        "completed_at_unix_s": time.time(),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink()
    return path


def _reuse_bootstrap_success_manifest(
    gc_backend: str,
    shared_pcc1: Path,
    runtime_archive: Path,
) -> tuple[bool, str]:
    if _env_truthy("PCC_BOOTSTRAP_FULL_REBUILD"):
        return False, "PCC_BOOTSTRAP_FULL_REBUILD forces a fresh backend"
    path = _bootstrap_success_manifest_path(gc_backend)
    if not path.is_file():
        return False, "no completed backend manifest"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"invalid completed backend manifest: {exc}"
    if payload.get("schema") != _BOOTSTRAP_SUCCESS_SCHEMA:
        return False, "completed backend manifest schema mismatch"
    try:
        expected_inputs = _bootstrap_success_inputs(
            gc_backend, shared_pcc1, runtime_archive
        )
        if payload.get("inputs") != expected_inputs:
            return False, "completed backend input fingerprint mismatch"
        expected_outputs = _completed_backend_output_records(gc_backend, shared_pcc1)
        if payload.get("outputs") != expected_outputs:
            return False, "completed backend output fingerprint mismatch"
    except (AssertionError, OSError) as exc:
        return False, f"completed backend verification failed: {exc}"
    return True, "complete same-source backend result"


@functools.lru_cache(maxsize=1)
def _bootstrap_source_latest_mtime() -> float:
    latest = 0.0
    for path in _bootstrap_source_files():
        try:
            latest = max(latest, path.stat().st_mtime)
        except OSError:
            pass
    return latest


def _shared_pcc1_is_fresh(pcc1: Path) -> bool:
    if not pcc1.exists() or not os.access(pcc1, os.X_OK):
        return False
    if pcc1.stat().st_mtime < _bootstrap_source_latest_mtime():
        return False
    return not _links_libpython(pcc1)


def _bootstrap_rebuild_run_id() -> str | None:
    return os.environ.get("PYTEST_XDIST_TESTRUNUID")


def _shared_stage1_rebuilt_for_this_run() -> bool:
    run_id = _bootstrap_rebuild_run_id()
    if not run_id:
        return False
    try:
        return _SHARED_STAGE1_REBUILD_STAMP.read_text(encoding="utf-8") == run_id
    except OSError:
        return False


def _record_shared_stage1_rebuilt_for_this_run() -> None:
    run_id = _bootstrap_rebuild_run_id()
    if run_id:
        _SHARED_STAGE1_REBUILD_STAMP.write_text(run_id, encoding="utf-8")


def _configure_bootstrap_compiler_caches(env: dict[str, str]) -> None:
    """Give every bootstrap stage the same content-addressed cache namespace."""
    env.setdefault("PCC_SELF_BACKEND_OBJECT_CACHE", "1")
    env.setdefault(
        "PCC_SELF_BACKEND_OBJECT_CACHE_DIR",
        str(_BOOTSTRAP_OBJECT_CACHE_DIR),
    )
    env["PCC_PY_FRONTEND_IR_CACHE_IDENTITY"] = _bootstrap_source_sha256()
    env["PCC_SELF_BACKEND_OBJECT_CACHE_IDENTITY"] = (
        _bootstrap_self_backend_object_identity()
    )


def _shared_pcc1_needs_rebuild(pcc1: Path) -> bool:
    if not _shared_pcc1_is_fresh(pcc1):
        return True
    if _env_truthy("PCC_BOOTSTRAP_FULL_REBUILD"):
        if _shared_stage1_rebuilt_for_this_run():
            return False
        return pcc1.stat().st_mtime < _BOOTSTRAP_HELPER_IMPORT_TIME
    return False


@contextlib.contextmanager
def _shared_stage1_build_lock():
    """Serialize shared pcc1 construction across xdist workers."""
    _SHARED_STAGE1_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with _SHARED_STAGE1_LOCK.open("w", encoding="utf-8") as lock_file:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - full bootstrap is POSIX-only.
            yield
            return
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _build_shared_stage1(runtime_archive: Path) -> Path:
    """Build (or reuse, if fresh) one backend-agnostic ``pcc1`` shared by every
    backend. Rebuilt when stale or when ``PCC_BOOTSTRAP_FULL_REBUILD=1``."""
    pcc1 = _SHARED_STAGE1_DIR / "pcc1"
    with _shared_stage1_build_lock():
        if _shared_pcc1_needs_rebuild(pcc1):
            _SHARED_STAGE1_DIR.mkdir(parents=True, exist_ok=True)
            cmd = [
                sys.executable,
                str(_BOOTSTRAP_PY),
                "--backend",
                "self",
                "--out-dir",
                str(_SHARED_STAGE1_DIR),
                "--stage",
                "1",
            ]
            print(
                f"\n[shared stage1] building one backend-agnostic pcc1: {' '.join(cmd)}"
            )
            env = os.environ.copy()
            env.pop("LC_ALL", None)
            env["PCC_RUNTIME_ARCHIVE"] = str(runtime_archive)
            _configure_bootstrap_compiler_caches(env)
            result = run_process_group_timeout(
                cmd, timeout=_BOOTSTRAP_STAGE_TIMEOUT_S, env=env
            )
            if result.returncode != 0:
                print("STDOUT:", result.stdout)
                print("STDERR:", result.stderr)
            assert (
                result.returncode == 0
            ), f"shared stage1 (pcc1) build failed with exit code {result.returncode}"
            _record_shared_stage1_rebuilt_for_this_run()
    assert pcc1.exists() and os.access(
        pcc1, os.X_OK
    ), "shared pcc1 missing after stage1"
    assert not _links_libpython(pcc1), "shared pcc1 unexpectedly links libpython"
    return pcc1


@pytest.fixture(scope="session")
def shared_stage1_pcc1(pcc_runtime_archive) -> Path:
    # The stage-1 publish barrier asks the freshly built standalone pcc1 to
    # compile a smoke program.  That binary cannot rebuild its own runtime
    # archive, so make the host-built archive an explicit fixture dependency
    # instead of relying on pytest's ordering of independent test arguments.
    if not _is_macos_arm64():
        pytest.fail(
            "Full self-backend bootstrap is currently verified on macOS arm64 only"
        )
    return _build_shared_stage1(pcc_runtime_archive)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_process_group_cleanup_ignores_eperm_only_after_parent_exit(monkeypatch):
    class FakeProcess:
        pid = 123

        def __init__(self, returncode):
            self.returncode = returncode

        def poll(self):
            return self.returncode

    def deny(_pid, _sig):
        raise PermissionError("already reaped")

    monkeypatch.setattr(os, "killpg", deny)
    _kill_process_group(FakeProcess(-signal.SIGTERM), signal.SIGTERM)
    with pytest.raises(PermissionError, match="already reaped"):
        _kill_process_group(FakeProcess(None), signal.SIGTERM)


def test_process_group_timeout_reaps_bootstrap_children(tmp_path):
    # POSIX capability classified separately from the guard's behavior; the
    # child-group termination assertions stay hard on supported systems
    # (AUD-P2-PLATFORM-POSIX-RUNTIME-VERDICT).
    posix_verdict = probe_platform_capability(
        "posix-process-groups",
        supported=(os.name == "posix"),
        detail="process-group timeout is a POSIX bootstrap harness guard",
    )
    if not posix_verdict.available:
        pytest.fail(posix_verdict.skip_reason())

    child_pid_path = tmp_path / "child.pid"
    child_script = tmp_path / "child.py"
    child_script.write_text(
        "\n".join(
            [
                "import os",
                "import time",
                f"{str(child_pid_path)!r} and open({str(child_pid_path)!r}, 'w').write(str(os.getpid()))",
                "time.sleep(60)",
            ]
        ),
        encoding="utf-8",
    )
    parent_script = tmp_path / "parent.py"
    parent_script.write_text(
        "\n".join(
            [
                "import pathlib",
                "import subprocess",
                "import sys",
                "import time",
                f"pid_path = pathlib.Path({str(child_pid_path)!r})",
                f"subprocess.Popen([sys.executable, {str(child_script)!r}])",
                "deadline = time.time() + 5.0",
                "while not pid_path.exists() and time.time() < deadline:",
                "    time.sleep(0.01)",
                "time.sleep(60)",
            ]
        ),
        encoding="utf-8",
    )

    result = run_process_group_timeout(
        [sys.executable, str(parent_script)],
        env=os.environ.copy(),
        timeout=1.0,
    )

    assert result.returncode == 124
    assert child_pid_path.exists()
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    deadline = time.time() + 3.0
    while _pid_alive(child_pid) and time.time() < deadline:
        time.sleep(0.05)
    assert not _pid_alive(child_pid), result.stderr


def test_process_group_interrupt_reaps_bootstrap_children(tmp_path):
    posix_verdict = probe_platform_capability(
        "posix-process-groups",
        supported=(os.name == "posix"),
        detail="process-group interrupt handling is a POSIX bootstrap guard",
    )
    if not posix_verdict.available:
        pytest.fail(posix_verdict.skip_reason())

    child_pid_path = tmp_path / "interrupt-child.pid"
    child_script = tmp_path / "interrupt_child.py"
    child_script.write_text(
        "\n".join(
            [
                "import os",
                "import time",
                f"open({str(child_pid_path)!r}, 'w').write(str(os.getpid()))",
                "time.sleep(60)",
            ]
        ),
        encoding="utf-8",
    )
    parent_script = tmp_path / "interrupt_parent.py"
    parent_script.write_text(
        "\n".join(
            [
                "import pathlib",
                "import subprocess",
                "import sys",
                "import time",
                f"pid_path = pathlib.Path({str(child_pid_path)!r})",
                f"subprocess.Popen([sys.executable, {str(child_script)!r}])",
                "deadline = time.time() + 5.0",
                "while not pid_path.exists() and time.time() < deadline:",
                "    time.sleep(0.01)",
                "time.sleep(60)",
            ]
        ),
        encoding="utf-8",
    )
    runner_script = tmp_path / "interrupt_runner.py"
    runner_script.write_text(
        "\n".join(
            [
                "import os",
                "import pathlib",
                "import signal",
                "import sys",
                "import threading",
                "import time",
                f"sys.path.insert(0, {str(_REPO_ROOT)!r})",
                "from tests.python.process_timeout import run_process_group_timeout",
                f"pid_path = pathlib.Path({str(child_pid_path)!r})",
                "def interrupt_parent() -> None:",
                "    deadline = time.time() + 5.0",
                "    while not pid_path.exists() and time.time() < deadline:",
                "        time.sleep(0.01)",
                "    os.kill(os.getpid(), signal.SIGINT)",
                "threading.Thread(target=interrupt_parent, daemon=True).start()",
                "try:",
                f"    run_process_group_timeout([sys.executable, {str(parent_script)!r}], timeout=60.0)",
                "except KeyboardInterrupt:",
                "    raise SystemExit(77)",
                "raise SystemExit(1)",
            ]
        ),
        encoding="utf-8",
    )

    # Give the interrupt dance headroom: this guard spawns parent+child Python
    # processes and asserts a SIGINT propagates within the window. Under the
    # default `-n auto` the child sometimes did not even start within the old
    # 10s budget (CPU starvation), so the SIGINT/reap timing flaked; a wider
    # budget absorbs that without weakening what it checks.
    result = run_process_group_timeout(
        [sys.executable, str(runner_script)],
        env=os.environ.copy(),
        timeout=30.0,
    )

    assert result.returncode == 77, result.stderr
    assert child_pid_path.exists()
    child_pid = int(child_pid_path.read_text(encoding="utf-8"))
    deadline = time.time() + 3.0
    while _pid_alive(child_pid) and time.time() < deadline:
        time.sleep(0.05)
    assert not _pid_alive(child_pid), result.stderr


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _bootstrap_resource_run_id() -> str:
    raw = (
        os.environ.get("PCC_BOOTSTRAP_FULL_RUN_ID")
        or os.environ.get("PYTEST_XDIST_TESTRUNUID")
        or f"pid-{os.getpid()}"
    )
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in raw)
    return safe or f"pid-{os.getpid()}"


def _bootstrap_resource_run_dir() -> Path:
    return _BOOTSTRAP_RESOURCE_POOL_DIR / _bootstrap_resource_run_id()


@contextlib.contextmanager
def _bootstrap_resource_pool_lock():
    _BOOTSTRAP_RESOURCE_POOL_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with _BOOTSTRAP_RESOURCE_POOL_LOCK.open("w", encoding="utf-8") as lock_file:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - full bootstrap is POSIX-only.
            yield
            return
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _mark_bootstrap_gc_done(gc_backend: str) -> None:
    backend = str(gc_backend)
    if backend not in _GC_BACKENDS:
        raise ValueError(f"unknown GC backend {backend!r}")
    with _bootstrap_resource_pool_lock():
        run_dir = _bootstrap_resource_run_dir()
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / f"done-gc{backend}").write_text(str(time.time()), encoding="utf-8")


def _bootstrap_done_gc_count() -> int:
    with _bootstrap_resource_pool_lock():
        run_dir = _bootstrap_resource_run_dir()
        if not run_dir.is_dir():
            return 0
        return sum(
            1 for backend in _GC_BACKENDS if (run_dir / f"done-gc{backend}").exists()
        )


def _bootstrap_active_parallel_slots(parallel_slots: int) -> int:
    slots = _clamp_int(int(parallel_slots), minimum=1, maximum=len(_GC_BACKENDS))
    done_count = _clamp_int(_bootstrap_done_gc_count(), minimum=0, maximum=slots - 1)
    return max(1, slots - done_count)


def _bootstrap_gc_active_limit(parallel_slots: int) -> int:
    slots = _clamp_int(int(parallel_slots), minimum=1, maximum=len(_GC_BACKENDS))
    override = _env_int("PCC_BOOTSTRAP_FULL_MAX_ACTIVE_GC")
    if override is not None:
        return _clamp_int(
            override,
            minimum=1,
            maximum=min(slots, _GC_BOOTSTRAP_MAX_ACTIVE_BACKENDS),
        )
    if slots <= 1:
        return 1
    return min(_GC_BOOTSTRAP_MAX_ACTIVE_BACKENDS, slots)


def _bootstrap_effective_active_limit_locked(
    run_dir: Path,
    parallel_slots: int,
    configured_limit: int,
    gc_backend: str = "0",
) -> int:
    """Run one cold cache warmer before admitting the remaining GC chains."""
    raw_cache = str(os.environ.get("PCC_SELF_BACKEND_OBJECT_CACHE", "1") or "")
    cache_enabled = raw_cache.strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
        "disable",
        "disabled",
    )
    if (
        cache_enabled
        and int(parallel_slots) == len(_GC_BACKENDS)
        and not (run_dir / "done-gc0").is_file()
    ):
        # GC0 is the rollback/reference backend and owns the cold-cache slot.
        # Merely limiting the pool to one is insufficient: an xdist scheduling
        # race can let GC4/GC3 publish and acquire before GC0's waiting file is
        # visible, serializing an entire challenger chain before the reference
        # chain and making the 700-second matrix gate miss its deadline.
        return 1 if str(gc_backend) == "0" else 0
    return max(1, int(configured_limit))


def _bootstrap_parallel_min_jobs(cpu_count: int) -> int:
    override = _env_int("PCC_BOOTSTRAP_FULL_PARALLEL_MIN_JOBS")
    if override is not None:
        return _clamp_int(override, minimum=1, maximum=max(1, int(cpu_count)))
    return min(_GC_BOOTSTRAP_PARALLEL_MIN_JOBS, max(1, int(cpu_count)))


def _bootstrap_gc_priority(gc_backend: str) -> tuple[int, int]:
    backend = str(gc_backend)
    return (_GC_BOOTSTRAP_WEIGHT.get(backend, 0), -_GC_BACKENDS.index(backend))


def _bootstrap_wait_file_backend(path: Path) -> str | None:
    name = path.name
    if not name.startswith("waiting-gc"):
        return None
    backend = name.removeprefix("waiting-gc").split("-", 1)[0]
    return backend if backend in _GC_BACKENDS else None


def _bootstrap_pid_file_alive(path: Path) -> bool:
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    return _pid_alive(pid)


def _prune_bootstrap_resource_files_locked(run_dir: Path) -> None:
    for pattern in ("active-gc*-*", "waiting-gc*-*"):
        for path in run_dir.glob(pattern):
            if not _bootstrap_pid_file_alive(path):
                with contextlib.suppress(OSError):
                    path.unlink()


def _higher_priority_waiting_locked(run_dir: Path, gc_backend: str) -> bool:
    current = _bootstrap_gc_priority(gc_backend)
    for path in run_dir.glob("waiting-gc*-*"):
        backend = _bootstrap_wait_file_backend(path)
        if backend is None or backend == gc_backend:
            continue
        if _bootstrap_gc_priority(backend) > current:
            return True
    return False


@contextlib.contextmanager
def _bootstrap_active_gc_lease(gc_backend: str, parallel_slots: int):
    """Limit concurrent full-bootstrap GC chains while keeping xdist parallel.

    The split GC files can all be scheduled by xdist at once, but running all
    five stage2/stage3 chains concurrently starves the heavy backends. This
    lease keeps a small number of GC chains active and lets waiting xdist
    workers idle without changing the per-backend freshness or byte-identity
    checks.
    """
    backend = str(gc_backend)
    limit = _bootstrap_gc_active_limit(parallel_slots)
    if limit <= 1:
        yield 1
        return

    run_dir = _bootstrap_resource_run_dir()
    pid = os.getpid()
    waiting_path = run_dir / f"waiting-gc{backend}-{pid}"
    active_path = run_dir / f"active-gc{backend}-{pid}"
    acquired = False
    granted_slots = limit

    with _bootstrap_resource_pool_lock():
        run_dir.mkdir(parents=True, exist_ok=True)
        _prune_bootstrap_resource_files_locked(run_dir)
        waiting_path.write_text(str(pid), encoding="utf-8")

    # Let concurrently scheduled xdist workers publish their waiting files so
    # the weighted order can prefer GC4/GC3 before lighter backends.
    time.sleep(0.2)

    try:
        while True:
            with _bootstrap_resource_pool_lock():
                run_dir.mkdir(parents=True, exist_ok=True)
                _prune_bootstrap_resource_files_locked(run_dir)
                active_count = sum(1 for _ in run_dir.glob("active-gc*-*"))
                effective_limit = _bootstrap_effective_active_limit_locked(
                    run_dir,
                    parallel_slots,
                    limit,
                    backend,
                )
                if (
                    active_count < effective_limit
                    and not _higher_priority_waiting_locked(run_dir, backend)
                ):
                    with contextlib.suppress(OSError):
                        waiting_path.unlink()
                    active_path.write_text(str(pid), encoding="utf-8")
                    acquired = True
                    done_count = sum(
                        1
                        for candidate in _GC_BACKENDS
                        if (run_dir / f"done-gc{candidate}").is_file()
                    )
                    remaining_slots = max(1, parallel_slots - done_count)
                    granted_slots = min(effective_limit, remaining_slots)
                    break
            time.sleep(0.2)
        yield granted_slots
    finally:
        with _bootstrap_resource_pool_lock():
            if acquired:
                with contextlib.suppress(OSError):
                    active_path.unlink()
            with contextlib.suppress(OSError):
                waiting_path.unlink()


def _format_cpu_ids(cpu_ids: tuple[int, ...]) -> str:
    if not cpu_ids:
        return ""
    ranges: list[str] = []
    start = prev = cpu_ids[0]
    for cpu_id in cpu_ids[1:]:
        if cpu_id == prev + 1:
            prev = cpu_id
            continue
        ranges.append(str(start) if start == prev else f"{start}-{prev}")
        start = prev = cpu_id
    ranges.append(str(start) if start == prev else f"{start}-{prev}")
    return ",".join(ranges)


def _partition_cpu_ids(cpu_count: int, slots: int) -> tuple[tuple[int, ...], ...]:
    cpu = max(1, int(cpu_count))
    slot_count = _clamp_int(int(slots), minimum=1, maximum=cpu)
    base = cpu // slot_count
    remainder = cpu % slot_count
    partitions: list[tuple[int, ...]] = []
    start = 0
    for slot in range(slot_count):
        size = base + (1 if slot < remainder else 0)
        partitions.append(tuple(range(start, start + size)))
        start += size
    return tuple(partitions)


def _cpu_ids_for_gc_backend(
    gc_backend: str,
    *,
    cpu_count: int | None = None,
    parallel_slots: int = 1,
) -> tuple[int, ...]:
    cpu = max(1, int(cpu_count or os.cpu_count() or 1))
    slots = _clamp_int(
        int(parallel_slots), minimum=1, maximum=min(len(_GC_BACKENDS), cpu)
    )
    partitions = _partition_cpu_ids(cpu, slots)
    backend_index = _GC_BACKENDS.index(str(gc_backend))
    return partitions[backend_index % len(partitions)]


def _unique_backends(backends: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    ordered: list[str] = []
    for backend in backends:
        value = str(backend)
        if value not in _GC_BACKENDS:
            raise ValueError(f"unknown GC backend {value!r}")
        if value not in ordered:
            ordered.append(value)
    return tuple(ordered)


def _bootstrap_backend_run_order(backends: tuple[str, ...]) -> tuple[str, ...]:
    selected = _unique_backends(list(backends))
    original_index = {backend: index for index, backend in enumerate(_GC_BACKENDS)}
    return tuple(
        sorted(
            selected,
            key=lambda backend: (
                -_GC_BOOTSTRAP_WEIGHT.get(backend, 0),
                original_index.get(backend, len(_GC_BACKENDS)),
            ),
        )
    )


def _clamp_int(value: int, *, minimum: int, maximum: int) -> int:
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value


def _bootstrap_matrix_plan(
    backends: tuple[str, ...] | list[str],
    *,
    cpu_count: int | None = None,
    max_workers: int | None = None,
    frontend_jobs: int | None = None,
    self_backend_jobs: int | None = None,
) -> BootstrapMatrixPlan:
    selected = _unique_backends(tuple(backends))
    if not selected:
        raise ValueError("at least one GC backend is required")
    cpu = max(1, int(cpu_count or os.cpu_count() or 1))
    backend_count = len(selected)

    if max_workers is None:
        workers = 1
    else:
        workers = _clamp_int(
            int(max_workers),
            minimum=1,
            maximum=min(backend_count, _GC_BOOTSTRAP_MAX_ACTIVE_BACKENDS),
        )

    if frontend_jobs is None:
        py_jobs = min(_GC_BOOTSTRAP_MAX_FRONTEND_JOBS, cpu)
    else:
        py_jobs = int(frontend_jobs)
    py_jobs = _clamp_int(
        py_jobs,
        minimum=1,
        maximum=max(1, min(_GC_BOOTSTRAP_MAX_FRONTEND_JOBS, cpu)),
    )

    if self_backend_jobs is None:
        if backend_count == 1:
            self_jobs = cpu
        else:
            self_jobs = cpu // workers
        self_jobs = min(self_jobs, _GC_BOOTSTRAP_MAX_SELF_BACKEND_JOBS)
    else:
        self_jobs = int(self_backend_jobs)
    self_jobs = _clamp_int(
        self_jobs,
        minimum=1,
        maximum=max(1, min(_GC_BOOTSTRAP_MAX_SELF_BACKEND_JOBS, cpu)),
    )

    return BootstrapMatrixPlan(
        backends=_bootstrap_backend_run_order(selected),
        max_workers=workers,
        frontend_jobs=py_jobs,
        self_backend_jobs=self_jobs,
    )


def _bootstrap_gc_parallel_slots_from_items(
    items: list[object] | tuple[object, ...],
) -> int:
    full_gc_nodeids: list[str] = []
    for item in items:
        nodeid = str(getattr(item, "nodeid", ""))
        if "/gc/test_pcc_bootstrap_full_gc" in nodeid:
            full_gc_nodeids.append(nodeid)
    if full_gc_nodeids and all(
        nodeid.endswith("@gc_full_bootstrap") for nodeid in full_gc_nodeids
    ):
        return 1
    return max(1, len(full_gc_nodeids))


@pytest.fixture(scope="session")
def bootstrap_gc_parallel_slots(request, testrun_uid) -> int:
    os.environ.setdefault("PCC_BOOTSTRAP_FULL_RUN_ID", str(testrun_uid))
    override = _env_int("PCC_BOOTSTRAP_FULL_PARALLEL_SLOTS")
    if override is not None:
        return _clamp_int(override, minimum=1, maximum=len(_GC_BACKENDS))
    return _bootstrap_gc_parallel_slots_from_items(tuple(request.session.items))


def _bootstrap_gc_backend_plan(
    gc_backend: str,
    *,
    parallel_slots: int,
    cpu_count: int | None = None,
    frontend_jobs: int | None = None,
    self_backend_jobs: int | None = None,
    active_slots_override: int | None = None,
) -> BootstrapMatrixPlan:
    cpu = max(1, int(cpu_count or os.cpu_count() or 1))
    if active_slots_override is None:
        active_slots = _bootstrap_gc_active_limit(parallel_slots)
    else:
        active_slots = _clamp_int(
            int(active_slots_override),
            minimum=1,
            maximum=min(
                len(_GC_BACKENDS),
                cpu,
                _GC_BOOTSTRAP_MAX_ACTIVE_BACKENDS,
            ),
        )
    cpu_ids = _cpu_ids_for_gc_backend(
        gc_backend,
        cpu_count=cpu,
        parallel_slots=active_slots,
    )
    slot_cpu_count = max(1, len(cpu_ids))

    if frontend_jobs is None:
        env_frontend_jobs = _env_int("PCC_BOOTSTRAP_FULL_FRONTEND_JOBS")
        if env_frontend_jobs is not None:
            frontend_jobs = env_frontend_jobs
        else:
            # The self-host closure is large enough that native frontend workers
            # are memory-bound rather than CPU-bound.  Ten concurrent workers on
            # a 12-core host caused macOS to SIGKILL pcc1 while compiling pcc2.
            # Keep one aggregate frontend-worker budget across concurrently
            # admitted GC chains.  Two pcc2->pcc3 parents already retain
            # several GiB each, so assigning four more multi-hundred-MiB
            # frontend workers to *each* chain can exceed the machine budget.
            # PCC_BOOTSTRAP_FULL_FRONTEND_JOBS remains an explicit per-chain
            # override for operators who have measured additional headroom.
            if active_slots_override is None:
                frontend_slots = _bootstrap_gc_active_limit(active_slots)
            else:
                frontend_slots = active_slots
            frontend_jobs = max(
                1,
                min(_GC_BOOTSTRAP_MAX_FRONTEND_JOBS, cpu) // frontend_slots,
            )
    frontend_jobs = _clamp_int(
        int(frontend_jobs),
        minimum=1,
        maximum=max(1, min(_GC_BOOTSTRAP_MAX_FRONTEND_JOBS, cpu)),
    )

    if self_backend_jobs is None:
        env_self_jobs = _env_int("PCC_BOOTSTRAP_FULL_SELF_BACKEND_JOBS")
        if env_self_jobs is not None:
            self_backend_jobs = env_self_jobs
        else:
            self_backend_jobs = min(
                max(slot_cpu_count, _bootstrap_parallel_min_jobs(cpu)),
                _GC_BOOTSTRAP_MAX_SELF_BACKEND_JOBS,
            )
    self_backend_jobs = _clamp_int(
        int(self_backend_jobs),
        minimum=1,
        maximum=max(1, min(_GC_BOOTSTRAP_MAX_SELF_BACKEND_JOBS, cpu)),
    )

    return BootstrapMatrixPlan(
        backends=(str(gc_backend),),
        max_workers=1,
        frontend_jobs=frontend_jobs,
        self_backend_jobs=self_backend_jobs,
        cpu_ids=cpu_ids,
    )


def _load_bootstrap_profile_report(
    profile_dir: Path,
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        return build_bootstrap_profile_report(profile_dir, top=4), None
    except Exception as exc:  # pragma: no cover - diagnostic only.
        return None, str(exc)


def _bootstrap_stage_sampler_command(
    command: list[str],
    *,
    profile_dir: Path,
    stage: int,
) -> tuple[list[str], Path, Path]:
    prefix = profile_dir / ("stage" + str(stage) + ".process")
    result_path = Path(str(prefix) + ".result.json")
    samples_path = Path(str(prefix) + ".samples.tsv")
    stdout_path = Path(str(prefix) + ".stdout")
    stderr_path = Path(str(prefix) + ".stderr")
    sampler_command = [
        sys.executable,
        str(_PROCESS_TREE_SAMPLER),
        "--result",
        str(result_path),
        "--samples",
        str(samples_path),
        "--stdout",
        str(stdout_path),
        "--stderr",
        str(stderr_path),
        "--cwd",
        str(_REPO_ROOT),
        "--timeout",
        str(_BOOTSTRAP_STAGE_TIMEOUT_S),
        "--interval",
        "0.25",
        "--progress-interval",
        "30",
        "--max-tree-rss-bytes",
        str(_BOOTSTRAP_MAX_TREE_RSS_BYTES),
        "--",
        *command,
    ]
    return sampler_command, stdout_path, stderr_path


def _run_bootstrap_stage(
    out_dir: Path,
    gc_backend: str,
    runtime_archive: Path,
    *,
    stage: int,
    frontend_jobs: int,
    self_backend_jobs: int,
    cpu_ids: tuple[int, ...],
    profile_dir: Path,
) -> BootstrapBackendResult:
    """Run one bootstrap stage under ``PCC_GC_BACKEND=gc_backend``."""
    bootstrap_cmd = [
        sys.executable,
        str(_BOOTSTRAP_PY),
        "--backend",
        "self",
        "--out-dir",
        str(out_dir),
        "--stage",
        str(stage),
        "--reuse-stage1",
    ]
    if stage == 3:
        bootstrap_cmd.extend(["--from-stage", "3"])
    cmd = bootstrap_cmd
    cpu_text = _format_cpu_ids(cpu_ids) or "unpartitioned"
    print(
        f"\n[stage{stage}] PCC_GC_BACKEND={gc_backend} "
        f"frontend_jobs={frontend_jobs} self_backend_jobs={self_backend_jobs} "
        f"cpu_budget={cpu_text}: "
        f"{' '.join(cmd)}"
    )
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_RUNTIME_ARCHIVE"] = str(runtime_archive)
    env["PCC_GC_BACKEND"] = gc_backend
    env["PCC_BOOTSTRAP_PROFILE_DIR"] = str(profile_dir)
    env["PCC_BOOTSTRAP_PY_FRONTEND_JOBS"] = str(frontend_jobs)
    env["PCC_SELF_BACKEND_JOBS"] = str(self_backend_jobs)
    env["PCC_BOOTSTRAP_EXTERNAL_MEMORY_GUARD"] = "1"
    _configure_bootstrap_compiler_caches(env)
    start = time.monotonic()
    sampler_command, target_stdout, target_stderr = (
        _bootstrap_stage_sampler_command(
            cmd,
            profile_dir=profile_dir,
            stage=stage,
        )
    )
    sampled = run_process_group_timeout(
        sampler_command,
        timeout=_BOOTSTRAP_STAGE_TIMEOUT_S + 60,
        env=env,
    )
    try:
        stdout = target_stdout.read_text(encoding="utf-8")
    except OSError:
        stdout = ""
    try:
        stderr = target_stderr.read_text(encoding="utf-8")
    except OSError:
        stderr = ""
    sampler_diagnostics = (sampled.stdout or "") + (sampled.stderr or "")
    if sampler_diagnostics:
        stderr += "\n[process-tree sampler]\n" + sampler_diagnostics
    process = subprocess.CompletedProcess(
        cmd,
        sampled.returncode,
        stdout=stdout,
        stderr=stderr,
    )
    elapsed = time.monotonic() - start
    profile_report, profile_error = _load_bootstrap_profile_report(profile_dir)
    return BootstrapBackendResult(
        gc_backend=gc_backend,
        out_dir=out_dir,
        elapsed_s=elapsed,
        process=process,
        profile_dir=profile_dir,
        profile_report=profile_report,
        profile_error=profile_error,
    )


def _seed_shared_stage1(out_dir: Path, shared_pcc1: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    seeded_pcc1 = _stage_bin(out_dir, 1)
    temporary_pcc1 = out_dir / f".pcc1.{os.getpid()}.{time.time_ns()}.tmp"
    try:
        shutil.copy2(shared_pcc1, temporary_pcc1)
        os.chmod(temporary_pcc1, 0o755)
        # Do not truncate an existing Mach-O executable in place.  Darwin can
        # retain vnode/code-sign state for the old inode and SIGKILL the
        # byte-identical replacement at launch.  A fresh inode plus atomic
        # rename also prevents another worker from observing a partial copy.
        os.replace(temporary_pcc1, seeded_pcc1)
    finally:
        with contextlib.suppress(OSError):
            temporary_pcc1.unlink()


def _run_stage2_3(
    out_dir: Path,
    shared_pcc1: Path,
    runtime_archive: Path,
    gc_backend: str,
    *,
    parallel_slots: int,
    active_slots_override: int | None = None,
) -> BootstrapBackendResult:
    """Seed shared pcc1, then run stage2 and stage3 with one resource budget.

    The budget is deliberately fixed across pcc2 and pcc3: changing worker
    counts between those stages can change binary layout and break the byte
    identity gate even when semantics are unchanged.
    """
    with contextlib.suppress(OSError):
        _bootstrap_success_manifest_path(gc_backend).unlink()
    _seed_shared_stage1(out_dir, shared_pcc1)
    plan = _bootstrap_gc_backend_plan(
        gc_backend,
        parallel_slots=parallel_slots,
        active_slots_override=active_slots_override,
    )
    reused_stage2, reuse_reason = _reuse_bootstrap_stage2_success_manifest(
        gc_backend,
        shared_pcc1,
        runtime_archive,
        plan,
    )
    if reused_stage2:
        print(
            f"\nBootstrap stage2 cache HIT under PCC_GC_BACKEND={gc_backend}: "
            f"{reuse_reason}."
        )
        profile_dir = _bootstrap_profile_dir(gc_backend)
    else:
        print(
            f"\nBootstrap stage2 cache MISS under PCC_GC_BACKEND={gc_backend}: "
            f"{reuse_reason}."
        )
        with contextlib.suppress(OSError):
            _bootstrap_stage2_success_manifest_path(gc_backend).unlink()
        profile_dir = _prepare_bootstrap_profile_dir(gc_backend)
        stage2 = _run_bootstrap_stage(
            out_dir,
            gc_backend,
            runtime_archive,
            stage=2,
            frontend_jobs=plan.frontend_jobs,
            self_backend_jobs=plan.self_backend_jobs,
            cpu_ids=plan.cpu_ids,
            profile_dir=profile_dir,
        )
        if stage2.process.returncode != 0:
            return stage2
        _write_bootstrap_stage2_success_manifest(
            gc_backend,
            shared_pcc1,
            runtime_archive,
            plan,
        )

    # pcc2/pcc3 byte identity is sensitive to worker partitioning: stage3 must
    # use the same job budget as stage2 until codegen/link output is proven
    # independent of parallelism.
    _prepare_bootstrap_stage_profile(profile_dir, 3)
    return _run_bootstrap_stage(
        out_dir,
        gc_backend,
        runtime_archive,
        stage=3,
        frontend_jobs=plan.frontend_jobs,
        self_backend_jobs=plan.self_backend_jobs,
        cpu_ids=plan.cpu_ids,
        profile_dir=profile_dir,
    )


def run_full_three_stage_bootstrap_self_gc(
    gc_backend: str,
    shared_stage1_pcc1: Path,
    runtime_archive: Path,
    *,
    parallel_slots: int = 1,
) -> None:
    """Run and verify one GC backend's real pcc1 -> pcc2 -> pcc3 chain."""
    reused, reuse_reason = _reuse_bootstrap_success_manifest(
        gc_backend,
        shared_stage1_pcc1,
        runtime_archive,
    )
    if reused:
        print(
            f"\nBootstrap cache HIT under PCC_GC_BACKEND={gc_backend}: "
            f"{reuse_reason}."
        )
        _mark_bootstrap_gc_done(gc_backend)
        return
    print(
        f"\nBootstrap cache MISS under PCC_GC_BACKEND={gc_backend}: " f"{reuse_reason}."
    )
    try:
        with _bootstrap_active_gc_lease(gc_backend, parallel_slots) as active_slots:
            run = _run_stage2_3(
                _bootstrap_out_dir(gc_backend),
                shared_stage1_pcc1,
                runtime_archive,
                gc_backend,
                parallel_slots=parallel_slots,
                active_slots_override=active_slots,
            )
    finally:
        _mark_bootstrap_gc_done(gc_backend)
    result = run.process
    out_dir = run.out_dir

    if result.returncode != 0:
        print("STDOUT:", result.stdout)
        print("STDERR:", result.stderr)
    assert result.returncode == 0, (
        f"Bootstrap stage2->3 failed under PCC_GC_BACKEND={gc_backend} "
        f"with exit code {result.returncode}"
    )

    _write_bootstrap_success_manifest(
        gc_backend,
        shared_stage1_pcc1,
        runtime_archive,
    )
    print(
        f"\nBootstrap OK under PCC_GC_BACKEND={gc_backend}: "
        "pcc2 and pcc3 are byte-identical."
    )


def test_bootstrap_matrix_plan_uses_bounded_parallelism_on_12_cpu_host():
    plan = _bootstrap_matrix_plan(_GC_BACKENDS, cpu_count=12)

    assert plan.backends == ("0", "4", "3", "1", "2")
    assert plan.max_workers == 1
    assert plan.frontend_jobs == 2
    assert plan.self_backend_jobs == 2
    assert plan.max_workers * plan.frontend_jobs <= 12
    assert plan.max_workers * plan.self_backend_jobs <= 12


def test_bootstrap_stage_sampler_enforces_eight_gib_and_600_seconds(tmp_path):
    command, stdout_path, stderr_path = _bootstrap_stage_sampler_command(
        ["/bin/echo", "stage"],
        profile_dir=tmp_path,
        stage=2,
    )

    assert command[command.index("--timeout") + 1] == "600"
    assert command[command.index("--max-tree-rss-bytes") + 1] == str(
        8 * 1024 * 1024 * 1024
    )
    assert command[-3:] == ["--", "/bin/echo", "stage"]
    assert stdout_path.name == "stage2.process.stdout"
    assert stderr_path.name == "stage2.process.stderr"
    assert Path(command[command.index("--result") + 1]).name == (
        "stage2.process.result.json"
    )


def test_bootstrap_matrix_plan_single_backend_keeps_safe_local_parallelism():
    plan = _bootstrap_matrix_plan(["4"], cpu_count=12)

    assert plan.backends == ("4",)
    assert plan.max_workers == 1
    assert plan.frontend_jobs == 2
    assert plan.self_backend_jobs == 2


def test_bootstrap_matrix_plan_clamps_overrides_to_backend_and_cpu_limits():
    plan = _bootstrap_matrix_plan(
        ["0", "1"],
        cpu_count=4,
        max_workers=99,
        frontend_jobs=99,
        self_backend_jobs=0,
    )

    assert plan.max_workers == 1
    assert plan.frontend_jobs == 2
    assert plan.self_backend_jobs == 1


def test_bootstrap_gc_parallel_slots_count_full_gc_files():
    class Item:
        def __init__(self, nodeid: str) -> None:
            self.nodeid = nodeid

    slots = _bootstrap_gc_parallel_slots_from_items(
        (
            Item("tests/python/gc/test_pcc_bootstrap_full_gc0.py::test_x"),
            Item("tests/python/gc/test_pcc_bootstrap_full_gc1.py::test_x"),
            Item("tests/python/gc/test_pcc_bootstrap_full_gc2.py::test_x"),
            Item("tests/python/gc/test_pcc_bootstrap_full_gc3.py::test_x"),
            Item("tests/python/gc/test_pcc_bootstrap_full_gc4.py::test_x"),
            Item("tests/python/test_pcc_bootstrap_full.py::test_algorithm"),
        )
    )

    assert slots == 5


def test_bootstrap_gc_parallel_slots_grouped_files_use_one_slot():
    class Item:
        def __init__(self, nodeid: str) -> None:
            self.nodeid = nodeid

    slots = _bootstrap_gc_parallel_slots_from_items(
        tuple(
            Item(
                "tests/python/gc/test_pcc_bootstrap_full_gc"
                f"{backend}.py::test_x@gc_full_bootstrap"
            )
            for backend in range(5)
        )
    )

    assert slots == 1


def _use_unique_bootstrap_resource_run(monkeypatch, name: str) -> None:
    monkeypatch.setenv(
        "PCC_BOOTSTRAP_FULL_RUN_ID",
        f"{name}-{os.getpid()}-{time.time_ns()}",
    )


def test_bootstrap_gc_backend_plan_partitions_worker_budget_for_parallel_files(
    monkeypatch,
):
    _use_unique_bootstrap_resource_run(monkeypatch, "partition")

    gc0 = _bootstrap_gc_backend_plan("0", cpu_count=12, parallel_slots=5)
    gc4 = _bootstrap_gc_backend_plan("4", cpu_count=12, parallel_slots=5)

    assert gc0.cpu_ids == tuple(range(12))
    assert gc0.frontend_jobs == 2
    assert gc0.self_backend_jobs == 2
    assert gc4.cpu_ids == tuple(range(12))
    assert gc4.frontend_jobs == 2
    assert gc4.self_backend_jobs == 2


def test_bootstrap_active_gc_limit_defaults_to_one_heavy_chain(monkeypatch):
    monkeypatch.delenv("PCC_BOOTSTRAP_FULL_MAX_ACTIVE_GC", raising=False)

    assert _bootstrap_gc_active_limit(1) == 1
    assert _bootstrap_gc_active_limit(2) == 1
    assert _bootstrap_gc_active_limit(5) == 1


def test_bootstrap_active_gc_limit_clamps_explicit_operator_override(monkeypatch):
    monkeypatch.setenv("PCC_BOOTSTRAP_FULL_MAX_ACTIVE_GC", "3")

    assert _bootstrap_gc_active_limit(2) == 1
    assert _bootstrap_gc_active_limit(5) == 1


def test_bootstrap_gc_backend_plan_expands_for_not_yet_started_gc_after_done(
    monkeypatch,
):
    _use_unique_bootstrap_resource_run(monkeypatch, "expand")

    initial = _bootstrap_gc_backend_plan("4", cpu_count=12, parallel_slots=5)
    _mark_bootstrap_gc_done("0")
    after_one_done = _bootstrap_gc_backend_plan("4", cpu_count=12, parallel_slots=5)
    _mark_bootstrap_gc_done("1")
    _mark_bootstrap_gc_done("2")
    _mark_bootstrap_gc_done("3")
    only_gc4_left = _bootstrap_gc_backend_plan("4", cpu_count=12, parallel_slots=5)

    assert initial.frontend_jobs == 2
    assert initial.self_backend_jobs == 2
    assert after_one_done.frontend_jobs == 2
    assert after_one_done.self_backend_jobs == 2
    assert only_gc4_left.frontend_jobs == 2
    assert only_gc4_left.self_backend_jobs == 2


def test_bootstrap_gc_backend_plan_respects_active_lease_slots(monkeypatch):
    _use_unique_bootstrap_resource_run(monkeypatch, "lease")

    plan = _bootstrap_gc_backend_plan(
        "4",
        cpu_count=12,
        parallel_slots=5,
        active_slots_override=2,
    )

    assert plan.cpu_ids == tuple(range(12))
    assert plan.frontend_jobs == 2
    assert plan.self_backend_jobs == 2


def test_bootstrap_gc_backend_plan_single_file_keeps_memory_safe_frontend_parallelism(
    monkeypatch,
):
    _use_unique_bootstrap_resource_run(monkeypatch, "single")

    plan = _bootstrap_gc_backend_plan("4", cpu_count=12, parallel_slots=1)

    assert plan.cpu_ids == tuple(range(12))
    assert plan.frontend_jobs == 2
    assert plan.self_backend_jobs == 2


def test_bootstrap_gc_backend_plan_keeps_explicit_self_job_override(monkeypatch):
    _use_unique_bootstrap_resource_run(monkeypatch, "self-jobs-override")
    monkeypatch.setenv("PCC_BOOTSTRAP_FULL_SELF_BACKEND_JOBS", "5")

    plan = _bootstrap_gc_backend_plan("0", cpu_count=12, parallel_slots=1)

    assert plan.self_backend_jobs == 2


def test_bootstrap_gc_backend_plan_keeps_explicit_frontend_job_override(monkeypatch):
    _use_unique_bootstrap_resource_run(monkeypatch, "frontend-jobs-override")
    monkeypatch.setenv("PCC_BOOTSTRAP_FULL_FRONTEND_JOBS", "5")

    plan = _bootstrap_gc_backend_plan(
        "0",
        cpu_count=12,
        parallel_slots=5,
        active_slots_override=2,
    )

    assert plan.frontend_jobs == 2


def test_bootstrap_active_gc_lease_grants_full_budget_to_last_chain(monkeypatch):
    _use_unique_bootstrap_resource_run(monkeypatch, "last-chain")
    for backend in ("0", "1", "2", "3"):
        _mark_bootstrap_gc_done(backend)

    with _bootstrap_active_gc_lease("4", 5) as active_slots:
        assert active_slots == 1


def test_bootstrap_gc_object_cache_warmup_serializes_until_gc0_terminal(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("PCC_SELF_BACKEND_OBJECT_CACHE", "1")

    assert _bootstrap_effective_active_limit_locked(tmp_path, 5, 3, "0") == 1
    assert _bootstrap_effective_active_limit_locked(tmp_path, 5, 3, "4") == 0
    (tmp_path / "done-gc0").write_text("done", encoding="utf-8")
    assert _bootstrap_effective_active_limit_locked(tmp_path, 5, 3, "4") == 3


def test_bootstrap_gc_object_cache_warmup_is_disabled_with_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("PCC_SELF_BACKEND_OBJECT_CACHE", "off")

    assert _bootstrap_effective_active_limit_locked(tmp_path, 5, 3) == 3


def test_bootstrap_stage1_and_later_stages_share_content_cache_namespace(
    monkeypatch,
):
    monkeypatch.setattr(
        sys.modules[__name__],
        "_bootstrap_source_sha256",
        lambda: "source-content-id",
    )
    monkeypatch.setattr(
        sys.modules[__name__],
        "_bootstrap_self_backend_object_identity",
        lambda: "emitter-content-id",
    )
    env: dict[str, str] = {}

    _configure_bootstrap_compiler_caches(env)

    assert env["PCC_SELF_BACKEND_OBJECT_CACHE"] == "1"
    assert env["PCC_SELF_BACKEND_OBJECT_CACHE_DIR"] == str(_BOOTSTRAP_OBJECT_CACHE_DIR)
    assert env["PCC_PY_FRONTEND_IR_CACHE_IDENTITY"] == "source-content-id"
    assert env["PCC_SELF_BACKEND_OBJECT_CACHE_IDENTITY"] == "emitter-content-id"


def test_seed_shared_stage1_atomically_replaces_existing_executable(tmp_path):
    shared_pcc1 = tmp_path / "shared-pcc1"
    shared_pcc1.write_bytes(b"new-stage1")
    os.chmod(shared_pcc1, 0o755)
    out_dir = tmp_path / "gc3"
    out_dir.mkdir()
    seeded_pcc1 = out_dir / "pcc1"
    seeded_pcc1.write_bytes(b"old-stage1")
    os.chmod(seeded_pcc1, 0o755)
    old_inode = seeded_pcc1.stat().st_ino

    _seed_shared_stage1(out_dir, shared_pcc1)

    assert seeded_pcc1.read_bytes() == b"new-stage1"
    assert seeded_pcc1.stat().st_mode & 0o111
    assert seeded_pcc1.stat().st_ino != old_inode


def _fake_completed_backend_tree(
    tmp_path: Path,
    monkeypatch,
    *,
    include_stage3_result: bool = True,
) -> tuple[Path, Path, Path]:
    out_dir = tmp_path / "gc0"
    profile_dir = out_dir / "profile"
    profile_dir.mkdir(parents=True)
    shared_pcc1 = tmp_path / "shared-pcc1"
    runtime_archive = tmp_path / "libpy_runtime_pcc_py.a"
    shared_pcc1.write_bytes(b"shared-stage1")
    runtime_archive.write_bytes(b"runtime-archive")
    shared_pcc1.chmod(0o755)
    for stage, payload in (
        (1, b"shared-stage1"),
        (2, b"fixed-point"),
        (3, b"fixed-point"),
    ):
        binary = out_dir / f"pcc{stage}"
        binary.write_bytes(payload)
        binary.chmod(0o755)
    for stage in (2, 3):
        if stage == 3 and not include_stage3_result:
            continue
        output = out_dir / f"pcc{stage}"
        (profile_dir / f"stage{stage}.result.json").write_text(
            json.dumps(
                {
                    "schema": "pcc.bootstrap_stage_result.v1",
                    "backend": "self",
                    "stage": stage,
                    "returncode": 0,
                    "publish_barrier_returncode": 0,
                    "output": str(output),
                }
            ),
            encoding="utf-8",
        )
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "_bootstrap_out_dir", lambda _backend: out_dir)
    monkeypatch.setattr(module, "_bootstrap_source_sha256", lambda: "source-a")
    monkeypatch.setattr(module, "_links_libpython", lambda _path: False)
    monkeypatch.setattr(
        module, "_byte_identical", lambda _left, _right: True
    )
    monkeypatch.delenv("PCC_BOOTSTRAP_FULL_REBUILD", raising=False)
    return out_dir, shared_pcc1, runtime_archive


def test_bootstrap_success_manifest_rejects_raw_byte_drift(tmp_path, monkeypatch):
    from tests.python import test_bootstrap_gate_baseline as baseline

    out_dir, shared_pcc1, runtime_archive = _fake_completed_backend_tree(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(sys.modules[__name__], "_byte_identical", baseline._byte_identical)
    (out_dir / "pcc3").write_bytes(b"different-fixed-point")

    with pytest.raises(AssertionError, match="byte-identical in their original bytes"):
        _write_bootstrap_success_manifest("0", shared_pcc1, runtime_archive)
    assert not (out_dir / _BOOTSTRAP_SUCCESS_MANIFEST).exists()


def test_bootstrap_success_manifest_reuses_only_complete_content_addressed_result(
    tmp_path,
    monkeypatch,
):
    out_dir, shared_pcc1, runtime_archive = _fake_completed_backend_tree(
        tmp_path, monkeypatch
    )

    manifest = _write_bootstrap_success_manifest("0", shared_pcc1, runtime_archive)
    reused, reason = _reuse_bootstrap_success_manifest(
        "0", shared_pcc1, runtime_archive
    )
    assert manifest.is_file()
    assert reused is True
    assert reason == "complete same-source backend result"

    (out_dir / "pcc2").write_bytes(b"tampered-fixed-point")
    reused, reason = _reuse_bootstrap_success_manifest(
        "0", shared_pcc1, runtime_archive
    )
    assert reused is False
    assert "output fingerprint mismatch" in reason

    (out_dir / "pcc2").write_bytes(b"fixed-point")
    (out_dir / "pcc2").chmod(0o755)
    monkeypatch.setattr(
        sys.modules[__name__], "_bootstrap_source_sha256", lambda: "source-b"
    )
    reused, reason = _reuse_bootstrap_success_manifest(
        "0", shared_pcc1, runtime_archive
    )
    assert reused is False
    assert "input fingerprint mismatch" in reason


def test_bootstrap_stage2_success_manifest_is_content_and_plan_addressed(
    tmp_path,
    monkeypatch,
):
    out_dir, shared_pcc1, runtime_archive = _fake_completed_backend_tree(
        tmp_path, monkeypatch
    )
    plan = BootstrapMatrixPlan(
        backends=("0",),
        max_workers=1,
        frontend_jobs=4,
        self_backend_jobs=12,
        cpu_ids=tuple(range(12)),
    )

    manifest = _write_bootstrap_stage2_success_manifest(
        "0", shared_pcc1, runtime_archive, plan
    )
    reused, reason = _reuse_bootstrap_stage2_success_manifest(
        "0", shared_pcc1, runtime_archive, plan
    )
    assert manifest.is_file()
    assert reused is True
    assert reason == "complete same-source stage2 result"

    (out_dir / "pcc2").write_bytes(b"tampered-stage2")
    reused, reason = _reuse_bootstrap_stage2_success_manifest(
        "0", shared_pcc1, runtime_archive, plan
    )
    assert reused is False
    assert "output fingerprint mismatch" in reason

    (out_dir / "pcc2").write_bytes(b"fixed-point")
    (out_dir / "pcc2").chmod(0o755)
    changed_plan = BootstrapMatrixPlan(
        backends=("0",),
        max_workers=1,
        frontend_jobs=3,
        self_backend_jobs=12,
        cpu_ids=tuple(range(12)),
    )
    reused, reason = _reuse_bootstrap_stage2_success_manifest(
        "0", shared_pcc1, runtime_archive, changed_plan
    )
    assert reused is False
    assert "input fingerprint mismatch" in reason


def test_bootstrap_stage2_success_manifest_resume_runs_only_stage3(
    tmp_path,
    monkeypatch,
):
    out_dir, shared_pcc1, runtime_archive = _fake_completed_backend_tree(
        tmp_path, monkeypatch
    )
    plan = BootstrapMatrixPlan(
        backends=("0",),
        max_workers=1,
        frontend_jobs=4,
        self_backend_jobs=12,
        cpu_ids=tuple(range(12)),
    )
    _write_bootstrap_stage2_success_manifest("0", shared_pcc1, runtime_archive, plan)
    monkeypatch.setattr(
        sys.modules[__name__],
        "_bootstrap_gc_backend_plan",
        lambda *_args, **_kwargs: plan,
    )
    stages: list[int] = []

    def fake_run_bootstrap_stage(
        out_dir_arg,
        gc_backend,
        runtime_archive_arg,
        *,
        stage,
        frontend_jobs,
        self_backend_jobs,
        cpu_ids,
        profile_dir,
    ):
        del frontend_jobs, self_backend_jobs, cpu_ids
        assert runtime_archive_arg == runtime_archive
        stages.append(stage)
        return BootstrapBackendResult(
            gc_backend=gc_backend,
            out_dir=out_dir_arg,
            elapsed_s=0.01,
            process=subprocess.CompletedProcess([], 0, "", ""),
            profile_dir=profile_dir,
            profile_report=None,
        )

    monkeypatch.setattr(
        sys.modules[__name__], "_run_bootstrap_stage", fake_run_bootstrap_stage
    )

    result = _run_stage2_3(
        out_dir,
        shared_pcc1,
        runtime_archive,
        "0",
        parallel_slots=1,
    )

    assert result.process.returncode == 0
    assert stages == [3]


def test_bootstrap_success_manifest_rejects_partial_timeout_and_forced_rebuild(
    tmp_path,
    monkeypatch,
):
    _out_dir, shared_pcc1, runtime_archive = _fake_completed_backend_tree(
        tmp_path,
        monkeypatch,
        include_stage3_result=False,
    )

    with pytest.raises(AssertionError, match="stage 3 result profile is missing"):
        _write_bootstrap_success_manifest("0", shared_pcc1, runtime_archive)
    assert not _bootstrap_success_manifest_path("0").exists()

    profile_dir = _bootstrap_profile_dir("0")
    stage3 = _stage_bin(_bootstrap_out_dir("0"), 3)
    (profile_dir / "stage3.result.json").write_text(
        json.dumps(
            {
                "schema": "pcc.bootstrap_stage_result.v1",
                "backend": "self",
                "stage": 3,
                "returncode": 124,
                "publish_barrier_returncode": 124,
                "output": str(stage3),
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(AssertionError):
        _write_bootstrap_success_manifest("0", shared_pcc1, runtime_archive)
    assert not _bootstrap_success_manifest_path("0").exists()

    (profile_dir / "stage3.result.json").write_text(
        json.dumps(
            {
                "schema": "pcc.bootstrap_stage_result.v1",
                "backend": "self",
                "stage": 3,
                "returncode": 0,
                "publish_barrier_returncode": 0,
                "output": str(stage3),
            }
        ),
        encoding="utf-8",
    )
    _write_bootstrap_success_manifest("0", shared_pcc1, runtime_archive)
    monkeypatch.setenv("PCC_BOOTSTRAP_FULL_REBUILD", "1")
    reused, reason = _reuse_bootstrap_success_manifest(
        "0", shared_pcc1, runtime_archive
    )
    assert reused is False
    assert "forces a fresh backend" in reason
