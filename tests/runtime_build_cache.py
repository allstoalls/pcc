"""Per-pytest-process cache for immutable native runtime build artifacts.

Probe sources and executables remain in each test's ``tmp_path``.  Only a
builder's copied runtime directory/archive is reused when its explicit
arguments and build-relevant environment are identical.  A failed builder is
never cached.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache, wraps
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Callable, ParamSpec, TypeVar

from pcc.backend.self_backend_cache_identity import (
    self_backend_emitter_source_identity,
)
from pcc.py_frontend.pipeline_runtime_archive import target_id, write_target_stamp
from pcc.tools.runtime_archive_provenance import (
    ProvenanceError,
    capi_inventory_path_for_archive,
    manifest_path_for_archive,
    verify_runtime_archive_manifest,
)

P = ParamSpec("P")
T = TypeVar("T")

_BUILD_ENV_KEYS = (
    "CC",
    "CFLAGS",
    "CPPFLAGS",
    "LDFLAGS",
    "PCC_BACKEND",
    "PCC_GC_BACKEND",
    "PCC_REFCOUNT_KIND",
    "PCC_WITH_THREADS",
    "PCC_WITH_LIBPYTHON",
    "PCC_RUNTIME_CC",
    "PCC_RUNTIME_HIGH",
)
_PCC_PY_ARCHIVE_ENV_KEYS = (
    "CC",
    "CFLAGS",
    "CPPFLAGS",
    "LDFLAGS",
    "PCC_REFCOUNT_KIND",
    "PCC_IR_TO_OBJ_EMITTER",
)

_REPO_ROOT = Path(__file__).absolute().parents[1]
_RUNTIME_DIR = _REPO_ROOT / "pcc" / "py_runtime"
_PCC_RUNTIME_CACHE_MARKER_SCHEMA = "pcc.runtime-build-cache.v4"
_C_RUNTIME_CACHE_KEY_SCHEMA = "pcc.c-runtime-build-cache.v2"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _pcc_runtime_cache_marker_value(*, key: str, archive: Path) -> dict[str, str]:
    manifest = manifest_path_for_archive(archive)
    capi_inventory = capi_inventory_path_for_archive(archive)
    target = Path(str(archive) + ".target")
    return {
        "schema": _PCC_RUNTIME_CACHE_MARKER_SCHEMA,
        "key": key,
        "archive_sha256": _sha256_file(archive),
        "manifest_sha256": _sha256_file(manifest),
        "capi_inventory_sha256": _sha256_file(capi_inventory),
        "target_sha256": _sha256_file(target),
    }


def _pcc_runtime_cache_is_complete(
    *,
    runtime: Path,
    archive: Path,
    marker: Path,
    key: str,
) -> bool:
    manifest = manifest_path_for_archive(archive)
    capi_inventory = capi_inventory_path_for_archive(archive)
    target = Path(str(archive) + ".target")
    if (
        not archive.is_file()
        or not manifest.is_file()
        or not capi_inventory.is_file()
        or not target.is_file()
        or not marker.is_file()
    ):
        return False
    try:
        marker_value = json.loads(marker.read_text(encoding="utf-8"))
        verified = verify_runtime_archive_manifest(archive, runtime_root=runtime)
        manifest_target = verified.get("target_triple")
        if not isinstance(manifest_target, str):
            return False
        expected_target = target_id(manifest_target) + "\n"
        if target.read_text(encoding="utf-8") != expected_target:
            return False
        expected = _pcc_runtime_cache_marker_value(key=key, archive=archive)
        if marker_value != expected:
            return False
    except (OSError, UnicodeError, json.JSONDecodeError, ProvenanceError):
        return False
    return True


def _write_pcc_runtime_cache_marker(
    marker: Path,
    *,
    key: str,
    archive: Path,
) -> None:
    marker.write_text(
        json.dumps(
            _pcc_runtime_cache_marker_value(key=key, archive=archive),
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


@lru_cache(maxsize=1)
def self_host_source_key() -> str:
    """Fingerprint inputs that can change the self-host compiler stages."""

    digest = hashlib.sha256()
    digest.update(b"pcc.self-host-test-artifact.v2\0")
    digest.update(str(_REPO_ROOT.resolve()).encode("utf-8"))
    digest.update(b"\0")
    digest.update(sys.version.encode("utf-8"))
    digest.update(b"\0")
    for name in (
        "CC",
        "CFLAGS",
        "CPPFLAGS",
        "LDFLAGS",
        "MACOSX_DEPLOYMENT_TARGET",
        "PCC_GC_BACKEND",
        "PCC_HOST_PYTHON",
        "PCC_PYTHON_CONFIG",
        "PCC_REFCOUNT_KIND",
        "PCC_RUNTIME_CC",
        "PCC_RUNTIME_HIGH",
        "PCC_WITH_THREADS",
        "PCC_SELF_TARGET_PASSES",
        "PCC_SELF_TARGET_PASS_TRANSPORT",
    ):
        digest.update(name.encode("utf-8"))
        digest.update(b"=")
        digest.update(str(os.environ.get(name, "")).encode("utf-8"))
        digest.update(b"\0")
    explicit_runtime = str(os.environ.get("PCC_RUNTIME_ARCHIVE", "")).strip()
    digest.update(b"PCC_RUNTIME_ARCHIVE\0")
    digest.update(explicit_runtime.encode("utf-8"))
    digest.update(b"\0")
    if explicit_runtime:
        archive = Path(explicit_runtime).expanduser().resolve()
        for label, artifact in (
            ("archive", archive),
            ("manifest", manifest_path_for_archive(archive)),
            ("capi_inventory", capi_inventory_path_for_archive(archive)),
        ):
            digest.update(label.encode("utf-8"))
            digest.update(b"\0")
            try:
                digest.update(artifact.read_bytes())
            except OSError:
                digest.update(b"<missing>")
            digest.update(b"\0")
    cc = os.environ.get("CC", "cc")
    try:
        toolchain = subprocess.check_output(
            [cc, "--version"],
            stderr=subprocess.STDOUT,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        toolchain = b"<unknown-toolchain>"
    digest.update(toolchain)
    digest.update(b"\0")
    files = []
    for path in (_REPO_ROOT / "pcc").rglob("*"):
        if not path.is_file():
            continue
        if any(part.startswith(".") or part == "__pycache__" for part in path.parts):
            continue
        if path.suffix not in {".c", ".h", ".py"} and path.name != "Makefile":
            continue
        files.append(path)
    for path in sorted(files):
        relative = path.relative_to(_REPO_ROOT).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(path.read_bytes())
    return digest.hexdigest()


@lru_cache(maxsize=1)
def self_backend_object_cache_key() -> str:
    """Fingerprint only the implementation behind IR-shard object emission."""

    return self_backend_emitter_source_identity(_REPO_ROOT)


def cached_self_host_oracle_dir() -> Path:
    """Return a source-addressed directory for immutable pcc1/pcc2/pcc3."""

    root = Path.home() / ".cache" / "pcc" / "test-artifacts" / "self-host-oracle"
    root.mkdir(parents=True, exist_ok=True)
    artifact_dir = root / self_host_source_key()[:24]
    artifact_dir.mkdir(parents=True, exist_ok=True)
    return artifact_dir


def self_host_object_cache_dir() -> Path:
    """Return the persistent compiled self-backend object-cache directory."""

    path = Path.home() / ".cache" / "pcc" / "test-artifacts" / "self-host-objects"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _c_runtime_source_key() -> str:
    """Hash inputs that can change the default C runtime archive."""

    digest = hashlib.sha256()
    digest.update(_C_RUNTIME_CACHE_KEY_SCHEMA.encode("ascii"))
    digest.update(b"\0")
    for name in (
        "CC",
        "CFLAGS",
        "CPPFLAGS",
        "LDFLAGS",
        "PCC_REFCOUNT_KIND",
        "PCC_WITH_LIBPYTHON",
    ):
        digest.update(name.encode("utf-8"))
        digest.update(str(os.environ.get(name, "")).encode("utf-8"))
    files = []
    for path in _RUNTIME_DIR.rglob("*"):
        if not path.is_file():
            continue
        if any(part.startswith(".") or part == "__pycache__" for part in path.parts):
            continue
        if path.suffix not in {".c", ".h"} and path.name != "Makefile":
            continue
        files.append(path)
    for path in sorted(files):
        digest.update(path.relative_to(_RUNTIME_DIR).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:24]


def _cached_c_runtime(*, threaded: bool) -> Path:
    """Build/reuse one immutable C runtime variant across pytest workers.

    Tests must never rebuild or link the repository's mutable
    ``pcc/py_runtime/libpy_runtime.a`` under xdist.  A content-addressed key,
    inter-process lock, staging directory, and atomic publish make readers see
    either a complete old artifact or a complete new artifact.
    """

    variant = "c-threaded" if threaded else "c-default"
    key = _c_runtime_source_key() + "-" + variant
    cache_root = Path.home() / ".cache" / "pcc" / "test-artifacts" / "runtime-builds"
    cache_root.mkdir(parents=True, exist_ok=True)
    runtime = cache_root / key
    marker_name = ".pcc-c-runtime-complete"
    marker = runtime / marker_name
    archive = runtime / "libpy_runtime.a"
    lock_path = cache_root / (key + ".lock")

    with lock_path.open("a", encoding="utf-8") as lock_file:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - POSIX test environment
            fcntl = None
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        staging_root: Path | None = None
        try:
            if archive.is_file() and marker.is_file():
                if marker.read_text(encoding="utf-8") == key:
                    return runtime
            if runtime.exists():
                shutil.rmtree(runtime)
            staging_root = Path(tempfile.mkdtemp(prefix=key + ".", dir=str(cache_root)))
            work_runtime = staging_root / "py_runtime"
            shutil.copytree(
                _RUNTIME_DIR,
                work_runtime,
                ignore=shutil.ignore_patterns(
                    "_native", "__pycache__", "build", "build_*", "*.a", "*.a.target"
                ),
            )
            env = dict(os.environ)
            env.pop("LC_ALL", None)
            command = [
                "make",
                "-C",
                str(work_runtime),
                f"PCC_WITH_THREADS={1 if threaded else 0}",
            ]
            command.append("libpy_runtime.a")
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=180,
                env=env,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            (work_runtime / marker_name).write_text(key, encoding="utf-8")
            os.replace(work_runtime, runtime)
            return runtime
        finally:
            if staging_root is not None:
                shutil.rmtree(staging_root, ignore_errors=True)
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def cached_c_runtime() -> Path:
    """Return the immutable default C runtime source tree and archive."""

    return _cached_c_runtime(threaded=False)


def cached_threaded_c_runtime() -> Path:
    """Return the immutable ``PCC_WITH_THREADS=1`` C runtime variant."""

    return _cached_c_runtime(threaded=True)


def cache_runtime_build(
    builder: Callable[P, T],
) -> Callable[P, T]:
    """Cache a runtime builder while excluding its first ``tmp_path`` arg."""

    cache: dict[tuple[object, ...], T] = {}

    @wraps(builder)
    def wrapped(tmp_path, *args, **kwargs):
        env_key = tuple((name, os.environ.get(name)) for name in _BUILD_ENV_KEYS)
        key = (
            args,
            tuple(sorted(kwargs.items())),
            env_key,
        )
        if key in cache:
            return cache[key]
        artifact = builder(tmp_path, *args, **kwargs)
        cache[key] = artifact
        return artifact

    return wrapped


def _selected_runtime_source(runtime_source: Path | None) -> Path:
    if runtime_source is None:
        return _RUNTIME_DIR
    selected = Path(runtime_source).expanduser().resolve()
    if not selected.is_dir():
        raise NotADirectoryError(f"runtime source directory does not exist: {selected}")
    return selected


def _pcc_runtime_source_key(
    pcc_bin: Path, *, runtime_source: Path | None = None,
) -> str:
    digest = hashlib.sha256()
    digest.update(sys.version.encode("utf-8"))
    digest.update(os.path.realpath(pcc_bin).encode("utf-8"))
    # PCC_BACKEND/PCC_GC_BACKEND do not affect ``--python-library
    # --emit-llvm`` output. Including them would rebuild this identical
    # archive for every meta-matrix label.
    for name in _PCC_PY_ARCHIVE_ENV_KEYS:
        digest.update(name.encode("utf-8"))
        digest.update(str(os.environ.get(name, "")).encode("utf-8"))
    roots = (
        _RUNTIME_DIR,
        _REPO_ROOT / "pcc" / "backend",
        _REPO_ROOT / "pcc" / "codegen",
        _REPO_ROOT / "pcc" / "evaluater",
        _REPO_ROOT / "pcc" / "llvm_capi",
        _REPO_ROOT / "pcc" / "parse",
        _REPO_ROOT / "pcc" / "py_frontend",
        _REPO_ROOT / "pcc" / "tools",
    )
    selected_runtime = _selected_runtime_source(runtime_source)
    files = []
    for root in roots:
        source_root = selected_runtime if root == _RUNTIME_DIR else root
        for source_path in source_root.rglob("*"):
            if not source_path.is_file():
                continue
            # Override location and snapshot permissions are not build inputs.
            # Preserve the original logical paths and ordering in the digest.
            path = root / source_path.relative_to(source_root)
            if any(
                part.startswith(".") or part == "__pycache__" for part in path.parts
            ):
                continue
            if path.suffix not in {".c", ".h", ".py"} and path.name != "Makefile":
                continue
            files.append((path, source_path))
    for path, source_path in sorted(files):
        digest.update(path.relative_to(_REPO_ROOT).as_posix().encode("utf-8"))
        digest.update(source_path.read_bytes())
    return digest.hexdigest()[:24]


def _pcc_runtime_cache_key(
    pcc_bin: Path, *, variant: str, runtime_source: Path | None = None,
) -> str:
    """Bind the immutable cache directory to its publication format."""

    digest = hashlib.sha256()
    digest.update(_PCC_RUNTIME_CACHE_MARKER_SCHEMA.encode("utf-8"))
    digest.update(b"\0")
    source_key = (
        _pcc_runtime_source_key(pcc_bin)
        if runtime_source is None
        else _pcc_runtime_source_key(pcc_bin, runtime_source=runtime_source)
    )
    digest.update(source_key.encode("ascii"))
    return digest.hexdigest()[:24] + "-" + variant


def _make_runtime_staging_writable(runtime: Path) -> None:
    """Change only the copied tree; frozen source snapshots stay read-only."""
    runtime.chmod(stat.S_IMODE(runtime.stat().st_mode) | stat.S_IRWXU)
    for directory, directories, files in os.walk(runtime):
        for name in directories:
            path = Path(directory) / name
            path.chmod(stat.S_IMODE(path.stat().st_mode) | stat.S_IRWXU)
        for name in files:
            path = Path(directory) / name
            path.chmod(stat.S_IMODE(path.stat().st_mode) | stat.S_IRUSR | stat.S_IWUSR)


def _cached_pcc_python_runtime(
    *, threaded: bool, runtime_source: Path | None = None,
) -> Path:
    selected_runtime = _selected_runtime_source(runtime_source)
    pcc_bin = _REPO_ROOT / ".venv" / "bin" / "pcc"
    variant = "threaded-pcc-py" if threaded else "pcc-py"
    key = _pcc_runtime_cache_key(pcc_bin, variant=variant, runtime_source=runtime_source)
    cache_root = Path.home() / ".cache" / "pcc" / "test-artifacts" / "runtime-builds"
    cache_root.mkdir(parents=True, exist_ok=True)
    runtime = cache_root / key
    marker = runtime / (".pcc-" + variant + "-complete")
    archive = runtime / "libpy_runtime_pcc_py.a"
    lock_path = cache_root / (key + ".lock")

    with lock_path.open("a", encoding="utf-8") as lock_file:
        try:
            import fcntl
        except ImportError:  # pragma: no cover - POSIX test environment
            fcntl = None
        if fcntl is not None:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        staging_root: Path | None = None
        try:
            if _pcc_runtime_cache_is_complete(
                runtime=runtime,
                archive=archive,
                marker=marker,
                key=key,
            ):
                return runtime
            if runtime.exists():
                shutil.rmtree(runtime)
            staging_root = Path(tempfile.mkdtemp(prefix=key + ".", dir=str(cache_root)))
            work_runtime = staging_root / "py_runtime"
            shutil.copytree(
                selected_runtime,
                work_runtime,
                ignore=shutil.ignore_patterns(
                    "_native",
                    "__pycache__",
                    "build",
                    "build_*",
                    "*.a",
                    "*.a.target",
                    "*.a.provenance.json",
                    "*.a.capi_syms",
                ),
            )
            _make_runtime_staging_writable(work_runtime)
            command = [
                "make",
                "-C",
                str(work_runtime),
                f"PCC={pcc_bin}",
                f"PYTHON={sys.executable}",
                f"PCC_REPO_ROOT={_REPO_ROOT}",
            ]
            command.append(f"PCC_WITH_THREADS={1 if threaded else 0}")
            command.append("libpy_runtime_pcc_py.a")
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=900,
            )
            assert result.returncode == 0, result.stdout + result.stderr
            work_archive = work_runtime / archive.name
            verified = verify_runtime_archive_manifest(
                work_archive,
                runtime_root=work_runtime,
            )
            manifest_target = verified.get("target_triple")
            if not isinstance(manifest_target, str):
                raise ProvenanceError(
                    "runtime archive manifest target triple is missing"
                )
            expected_target = target_id(manifest_target)
            write_target_stamp(str(work_archive), expected_target)
            work_target = Path(str(work_archive) + ".target")
            if (
                not work_target.is_file()
                or work_target.read_text(encoding="utf-8")
                != expected_target + "\n"
            ):
                raise OSError("failed to publish runtime target stamp")
            _write_pcc_runtime_cache_marker(
                work_runtime / marker.name,
                key=key,
                archive=work_archive,
            )
            os.replace(work_runtime, runtime)
            return runtime
        finally:
            if staging_root is not None:
                shutil.rmtree(staging_root, ignore_errors=True)
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def cached_pcc_python_runtime(*, runtime_source: Path | None = None) -> Path:
    """Build selected runtime sources with the current compiler into a private cache."""

    return _cached_pcc_python_runtime(threaded=False, runtime_source=runtime_source)


def cached_threaded_pcc_python_runtime(*, runtime_source: Path | None = None) -> Path:
    """Build/reuse one immutable threaded pcc-Python runtime source tree."""

    return _cached_pcc_python_runtime(threaded=True, runtime_source=runtime_source)
