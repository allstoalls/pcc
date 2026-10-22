from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest


def repo_root() -> Path:
    """Repo root by walking up to AGENTS.md.

    ``Path(__file__).resolve().parents[N]`` is unreliable under pytest here:
    tests/conftest.py monkeypatches ``Path.resolve`` to strip the
    ``tests/{c,python}`` level for legacy tests, so fixed-index arithmetic
    lands one directory too high. Walking up is immune to that shim.
    """
    cur = Path(__file__).resolve().parent
    while cur != cur.parent:
        if (cur / "AGENTS.md").exists():
            return cur
        cur = cur.parent
    raise RuntimeError("AGENTS.md not found walking up from " + __file__)


_FRESHNESS_SUFFIXES = {".py", ".c", ".h"}
_IGNORED_FRESHNESS_DIRS = {"__pycache__", "build", "build_py"}


def _normalize_repo_root(repo: Path) -> Path:
    if (repo / "pcc" / "__main__.py").exists():
        return repo
    nested = repo / "pcc"
    if (nested / "pcc" / "__main__.py").exists():
        return nested
    return repo


def _is_pcc1_freshness_source(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.suffix not in _FRESHNESS_SUFFIXES:
        return False
    if any(part in _IGNORED_FRESHNESS_DIRS for part in path.parts):
        return False
    return True


def pcc1_freshness_sources(repo: Path) -> tuple[Path, ...]:
    repo = _normalize_repo_root(repo)
    pcc_dir = repo / "pcc"
    sources: list[Path] = []
    for suffix in _FRESHNESS_SUFFIXES:
        pattern = "*" + suffix
        sources.extend(pcc_dir.rglob(pattern))
    return tuple(sorted(path for path in sources if _is_pcc1_freshness_source(path)))


def pcc1_freshness_cutoff(repo: Path) -> float:
    mtimes = [
        source.stat().st_mtime
        for source in pcc1_freshness_sources(repo)
        if source.exists()
    ]
    if not mtimes:
        return 0.0
    return max(mtimes)


def _pcc1_names() -> tuple[str, ...]:
    """Stage artifact names, including the Windows executable suffix."""

    return ("pcc1.exe", "pcc1") if os.name == "nt" else ("pcc1",)


def current_pcc1_candidates(repo: Path) -> tuple[Path, ...]:
    repo = _normalize_repo_root(repo)
    directories = (
        repo,
        repo / "build" / "bootstrap-pytest-self",
        repo / "build" / "bootstrap",
        repo / "build" / "bootstrap-self-claude",
        repo / "build" / "bootstrap-llvm-claude",
        repo / "build" / "bootstrap-strict-self",
        repo / "build" / "bootstrap-self-darwin_arm64",
        repo / "build" / "bootstrap-llvm-darwin_arm64",
    )
    fixed = tuple(
        directory / name for directory in directories for name in _pcc1_names()
    )
    dynamic = tuple(
        sorted(
            (
                path
                for directory in sorted((repo / "build").glob("bootstrap-*"))
                for name in _pcc1_names()
                for path in (directory / name,)
                if path.exists()
            ),
            key=lambda path: (path.stat().st_mtime, str(path)),
            reverse=True,
        )
    )
    seen: set[Path] = set()
    candidates: list[Path] = []
    for candidate in fixed + dynamic:
        if candidate in seen:
            continue
        seen.add(candidate)
        candidates.append(candidate)
    return tuple(candidates)


def find_current_pcc1(repo: Path) -> Path | None:
    repo = _normalize_repo_root(repo)
    explicit = os.environ.get("PCC_CURRENT_PCC1")
    if explicit:
        raw = Path(explicit)
        candidates = (raw,) if raw.is_absolute() else (raw, repo / raw)
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    source_mtime = pcc1_freshness_cutoff(repo)
    candidates = current_pcc1_candidates(repo)
    if os.environ.get("PCC_DEBUG_PCC1_GATE"):
        try:
            sys.stderr.write(
                "PCC1_GATE repo="
                + str(repo)
                + " source_mtime="
                + str(source_mtime)
                + "\n"
            )
            for candidate in candidates:
                if candidate.exists():
                    sys.stderr.write(
                        "PCC1_GATE candidate="
                        + str(candidate)
                        + " mtime="
                        + str(candidate.stat().st_mtime)
                        + "\n"
                    )
        except Exception:
            pass
    for candidate in candidates:
        if candidate.exists() and candidate.stat().st_mtime >= source_mtime:
            return candidate
    # No fresh pcc1: provision one (find-or-build), then rescan once.
    if _provision_stage1_pcc1(repo):
        source_mtime = pcc1_freshness_cutoff(repo)
        for candidate in current_pcc1_candidates(repo):
            if candidate.exists() and candidate.stat().st_mtime >= source_mtime:
                return candidate
    return None


_PROVISION_ATTEMPTED = False


def host_stage1_support() -> tuple[bool, str]:
    """Whether the owned self backend has an emitter for this host.

    Auto-provisioning is not Darwin-only, but it must not start a 30-minute
    build on a host the self backend cannot target: resolve the host triple
    first and fail fast with the reason instead.
    """

    try:
        from pcc.backend.self_backend_targets import resolve_self_backend_target
        from pcc.frontends.python.pipeline_targets import host_target_triple

        triple = host_target_triple()
        identity = resolve_self_backend_target(triple).identity
    except Exception as exc:  # noqa: BLE001 - reported verbatim to the caller
        return False, f"{type(exc).__name__}: {exc}"
    return True, f"{triple} -> {identity}"


def _provision_timeout_seconds() -> float:
    raw = os.environ.get("PCC_PCC1_PROVISION_TIMEOUT", "1800")
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 1800.0


def _provision_stage1_pcc1(repo: Path) -> bool:
    """Build a fresh stage1 pcc1 instead of letting consumers skip.

    Shares the lock/sentinel with tests/conftest.py's collection-time
    provisioning so xdist workers and runtime callers never build twice.
    Returns True when a rescan is worthwhile. Set PCC_NO_AUTO_PCC1=1 to opt
    out (CI that stages its own binaries).
    """
    global _PROVISION_ATTEMPTED
    if _PROVISION_ATTEMPTED or os.environ.get("PCC_NO_AUTO_PCC1", "").strip():
        return False
    from tests.native_provisioning import require_native_provisioning_allowed

    require_native_provisioning_allowed(repo)
    _PROVISION_ATTEMPTED = True
    root = str(repo)
    if root not in sys.path:
        sys.path.insert(0, root)
    supported, detail = host_stage1_support()
    if not supported:
        sys.stderr.write(
            "[pcc1_gate] this host has no owned self-backend stage1 target "
            f"({detail}); set PCC_NO_AUTO_PCC1=1 and stage pcc1 out of band\n"
        )
        return False
    from scripts.file_lock import exclusive_file_lock

    lock_path = os.path.join(tempfile.gettempdir(), "pcc-pytest-pcc1-provision.lock")
    with exclusive_file_lock(lock_path) as lockfile:
        lockfile.seek(0)
        stamp = lockfile.read().decode("utf-8", "replace").strip()
        now = time.time()
        if stamp:
            try:
                if now - float(stamp) < 300:
                    return True  # someone provisioned moments ago; rescan
            except ValueError:
                pass
        sys.stderr.write(
            "[pcc1_gate] ensuring fresh stage1 pcc1 "
            "(scripts/bootstrap.py --stage 1; ~16s cached, minutes cold; "
            f"timeout {_provision_timeout_seconds():.0f}s, "
            "PCC_PCC1_PROVISION_TIMEOUT overrides)\n"
        )
        env = os.environ.copy()
        env.pop("LC_ALL", None)
        proc = subprocess.run(
            [sys.executable, str(repo / "scripts" / "bootstrap.py"), "--stage", "1"],
            capture_output=True,
            text=True,
            timeout=_provision_timeout_seconds(),
            cwd=str(repo),
            env=env,
        )
        if proc.returncode != 0:
            sys.stderr.write(
                "[pcc1_gate] pcc1 auto-build FAILED:\n"
                + proc.stdout[-2000:]
                + proc.stderr[-2000:]
                + "\n"
            )
            return False
        lockfile.seek(0)
        lockfile.truncate()
        lockfile.write(str(now).encode("ascii"))
        lockfile.flush()
        return True


def require_current_pcc1_enabled() -> bool:
    return os.environ.get("PCC_REQUIRE_CURRENT_PCC1") == "1"


def skip_or_fail_no_current_pcc1(reason: str) -> None:
    pytest.fail(
        reason
        + "; auto-provisioning already tried scripts/bootstrap.py --stage 1 — "
        "read its error output (PCC_NO_AUTO_PCC1=1 disables the auto-build)"
    )
