"""Bound persistent Python frontend and self-backend object caches.

This module is intentionally host-owned.  Compiled pcc stages invoke it through
their existing host-Python subprocess boundary; it is not part of the native
compiler closure.  Cache use must remain a performance optimization: every
public helper catches lifecycle failures at its caller and degrades to a miss.

Entries are removed in two phases.  A single maintenance owner first renames a
complete entry into the cache-local quarantine, then removes the quarantined
copy.  Readers create an adjacent lease before validation/copy.  Publishers'
lock directories, temporary paths, and explicitly protected entries are never
selected.  A crash therefore leaves either a complete source entry or a
recoverable quarantine item, never a half-deleted cache hit.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Iterable, Sequence


STATE_SCHEMA = "pcc.compiler-cache-retention.v1"
DEFAULT_HIGH_BYTES = 10 * 1024 * 1024 * 1024
DEFAULT_LOW_BYTES = 8 * 1024 * 1024 * 1024
DEFAULT_MAX_UNUSED_DAYS = 30.0
DEFAULT_SCAN_LIMIT = 512
DEFAULT_AUTO_INTERVAL_SECONDS = 300.0
DEFAULT_LEASE_STALE_SECONDS = 6 * 60 * 60.0
DEFAULT_LOCK_STALE_SECONDS = 30 * 60.0

_META_DIR = ".pcc-cache-retention"
_STATE_FILE = "state.json"
_PRUNE_LOCK = "prune.lock"
_QUARANTINE_DIR = "quarantine"
_LAST_USED_SUFFIX = ".pcc-last-used"
_LEASE_INFIX = ".pcc-lease."
_EVICT_SUFFIX = ".pcc-evict"
_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True)
class RetentionPolicy:
    high_bytes: int = DEFAULT_HIGH_BYTES
    low_bytes: int = DEFAULT_LOW_BYTES
    max_unused_days: float = DEFAULT_MAX_UNUSED_DAYS
    scan_limit: int = DEFAULT_SCAN_LIMIT
    auto_interval_seconds: float = DEFAULT_AUTO_INTERVAL_SECONDS

    def validated(self) -> "RetentionPolicy":
        high = max(1, int(self.high_bytes))
        low = max(0, int(self.low_bytes))
        if low > high:
            raise ValueError("compiler cache low-water mark exceeds high-water mark")
        days = float(self.max_unused_days)
        if days < 0.0:
            raise ValueError("compiler cache unused-age policy cannot be negative")
        scan_limit = max(1, int(self.scan_limit))
        interval = max(0.0, float(self.auto_interval_seconds))
        return RetentionPolicy(high, low, days, scan_limit, interval)


@dataclass(frozen=True)
class CacheEntry:
    kind: str
    path: Path
    relative_path: str
    size_bytes: int
    last_used_ns: int
    protected: bool


def _env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name, "") or "").strip()
    return default if not raw else int(raw)


def _env_float(name: str, default: float) -> float:
    raw = str(os.environ.get(name, "") or "").strip()
    return default if not raw else float(raw)


def retention_enabled() -> bool:
    value = str(os.environ.get("PCC_COMPILER_CACHE_RETENTION", "") or "")
    return value.strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
        "disable",
        "disabled",
    )


# Cache trees pcc writes under the user's cache dir, with the depth at which
# one *entry* lives.  Depth is explicit rather than inferred: `macho-
# incremental-link` has only `image/`, `merged/` and `native/` at the top, so
# an entry-per-top-level sweep would delete a whole subtree instead of
# evicting its least-recently-used members.
USER_CACHE_TREES: tuple[tuple[str, int], ...] = (
    ("macho-incremental-link/image", 2),
    ("macho-incremental-link/native", 2),
    ("macho-incremental-link/merged", 2),
    ("python-ir-pass-cache", 2),
    ("compile-cache", 2),
    ("installations", 1),
    ("test-artifacts/runtime-builds", 1),
    ("test-artifacts/self-host-oracle", 1),
    ("test-artifacts/self-host-objects/frontend-ir", 2),
    ("test-artifacts/self-host-objects/macho-incremental-v1/image", 2),
    ("test-artifacts/self-host-objects/macho-incremental-v1/native", 2),
    ("self-backend-object-cache/frontend-ir/module-actions", 1),
)


def user_cache_roots(
    home: str | os.PathLike | None = None,
) -> tuple[tuple[Path, int], ...]:
    """Every unmanaged pcc cache tree, paired with its entry depth.

    `maintain_cache` only understands the self-backend object cache's entry
    shapes, so these trees were never counted and never evicted.  Measured on
    one developer machine: 70 GiB under ~/.cache/pcc, none of it reclaimable
    by the existing policy.
    """
    base = Path(home) if home is not None else Path.home()
    root = base / ".cache" / "pcc"
    return tuple((root / rel, depth) for rel, depth in USER_CACHE_TREES)


def _entries_at_depth(root: Path, depth: int) -> list[tuple[float, int, Path]]:
    """Cache entries at exactly ``depth`` levels below ``root``."""
    level = [root]
    for _ in range(depth - 1):
        nxt: list[Path] = []
        for parent in level:
            try:
                nxt.extend(c for c in parent.iterdir() if c.is_dir())
            except OSError:
                continue
        level = nxt
    rows: list[tuple[float, int, Path]] = []
    for parent in level:
        try:
            children = sorted(parent.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for child in children:
            if (
                child.is_symlink()
                or child.name.startswith(".")
                or child.name.endswith((".sha256", ".lock", _LAST_USED_SUFFIX, _EVICT_SUFFIX))
                or _LEASE_INFIX in child.name
                or ".tmp." in child.name
            ):
                continue
            size = 0
            latest = 0.0
            try:
                if child.is_dir():
                    for walk_root, _dirs, files in os.walk(child):
                        for name in files:
                            try:
                                st = os.stat(os.path.join(walk_root, name))
                            except OSError:
                                continue
                            size += st.st_size
                            latest = max(latest, st.st_mtime)
                else:
                    st = child.stat()
                    size = st.st_size
                    latest = st.st_mtime
                    sidecar = Path(str(child) + ".sha256")
                    if sidecar.is_file():
                        size += sidecar.stat().st_size
            except OSError:
                continue
            if size > 0:
                rows.append((latest, size, child))
    rows.sort(key=lambda row: row[0])
    return rows


def _sweep_victims(root, victims, *, dry_run, now, protected_paths=()):
    """Use the same exclusion marker and lease protocol as ordinary pruning."""
    timestamp = int(now * 1_000_000_000)
    protected = {str(Path(path).expanduser().resolve()) for path in protected_paths}
    removed = 0
    reclaimed = 0
    lock = _acquire_maintenance_lock(root, timestamp)
    if lock is None:
        return removed, reclaimed
    try:
        for used, size, path in victims:
            if _entry_is_protected(path, timestamp, protected):
                continue
            entry = CacheEntry(
                kind="directory" if path.is_dir() else "object",
                path=path,
                relative_path=path.relative_to(root).as_posix(),
                size_bytes=size,
                last_used_ns=_read_timestamp_ns(path, int(used * 1_000_000_000)),
                protected=False,
            )
            if dry_run or _quarantine_entry(root, entry, timestamp):
                removed += 1
                reclaimed += size
    finally:
        _release_maintenance_lock(lock)
    return removed, reclaimed


def sweep_tree(
    root: Path,
    policy: RetentionPolicy,
    depth: int,
    *,
    dry_run: bool = False,
    now: float | None = None,
) -> dict:
    """Evict least-recently-used entries until ``root`` fits the low mark.

    Deliberately shape-agnostic: these trees are pure caches keyed by a
    content hash, so "oldest unused first" is the whole policy.  Entries
    unused for longer than ``max_unused_days`` go regardless of size.
    """
    now = time.time() if now is None else now
    rows = _entries_at_depth(root, depth)
    total = sum(size for _used, size, _path in rows)
    report = {
        "root": str(root),
        "entries": len(rows),
        "bytes_before": total,
        "removed": 0,
        "reclaimed_bytes": 0,
        "dry_run": bool(dry_run),
    }
    if not rows:
        report["bytes_after"] = 0
        return report

    stale_cutoff = now - policy.max_unused_days * 86400.0
    victims = [row for row in rows if row[0] < stale_cutoff]
    keep = [row for row in rows if row[0] >= stale_cutoff]

    remaining = sum(size for _u, size, _p in keep)
    if remaining > policy.high_bytes:
        for row in keep:                      # already oldest-first
            if remaining <= policy.low_bytes:
                break
            victims.append(row)
            remaining -= row[1]

    report["removed"], report["reclaimed_bytes"] = _sweep_victims(
        root, victims, dry_run=dry_run, now=now,
    )
    report["bytes_after"] = total - report["reclaimed_bytes"]
    return report


def user_cache_budget_bytes() -> tuple[int, int]:
    """Total byte budget for every pcc cache tree combined.

    A per-tree budget is the wrong unit: twelve trees at the 10 GiB mark is a
    120 GiB footprint on a user's machine, which is how ~/.cache/pcc reached
    70 GiB here.  `PCC_CACHE_TOTAL_HIGH_BYTES` / `_LOW_BYTES` override it.
    """
    high = _env_int("PCC_CACHE_TOTAL_HIGH_BYTES", 10 * 1024 * 1024 * 1024)
    low = _env_int("PCC_CACHE_TOTAL_LOW_BYTES", 8 * 1024 * 1024 * 1024)
    if low >= high:
        low = max(1, high // 2)
    return high, low


def _sweep_stamp_path(home: str | os.PathLike | None = None) -> Path:
    base = Path(home) if home is not None else Path.home()
    return base / ".cache" / "pcc" / ".user-cache-sweep-stamp"


def _sweep_due(stamp: Path, interval: float, now: float) -> bool:
    if interval <= 0:
        return True
    try:
        return now - stamp.stat().st_mtime >= interval
    except OSError:
        return True


def sweep_user_caches(
    *,
    dry_run: bool = False,
    home: str | os.PathLike | None = None,
    now: float | None = None,
    automatic: bool = False,
    protected_paths: Iterable[os.PathLike[str] | str] = (),
) -> dict:
    """Hold every pcc cache tree to one combined budget, evicting oldest first.

    `automatic=True` is the compile-time path: it runs at most once per
    `auto_interval_seconds`, so an ordinary build never pays a full cache walk.
    """
    policy = policy_from_environment()
    if automatic and not retention_enabled():
        return {
            "schema": "pcc.user-cache-sweep.v1", "skipped_reason": "disabled",
            "removed": 0, "reclaimed_bytes": 0,
        }
    high, low = user_cache_budget_bytes()
    when = time.time() if now is None else now
    stamp = _sweep_stamp_path(home)
    if automatic and not _sweep_due(stamp, policy.auto_interval_seconds, when):
        return {
            "schema": "pcc.user-cache-sweep.v1",
            "skipped_reason": "interval",
            "removed": 0,
            "reclaimed_bytes": 0,
        }
    stale_cutoff = when - policy.max_unused_days * 86400.0

    pooled: list[tuple[float, int, Path]] = []
    per_tree: dict[str, int] = {}
    for root, depth in user_cache_roots(home):
        if not root.is_dir():
            continue
        rows = _entries_at_depth(root, depth)
        per_tree[str(root)] = sum(size for _u, size, _p in rows)
        pooled.extend(rows)
    pooled.sort(key=lambda row: row[0])          # globally oldest first

    total = sum(size for _u, size, _p in pooled)
    victims = [row for row in pooled if row[0] < stale_cutoff]
    remaining = total - sum(size for _u, size, _p in victims)
    if remaining > high:
        for row in pooled:
            if remaining <= low:
                break
            if row[0] < stale_cutoff:
                continue
            victims.append(row)
            remaining -= row[1]

    removed = 0
    reclaimed = 0
    for root, _depth in user_cache_roots(home):
        # Installed toolchains are executable artifacts, not disposable cache
        # entries. Keep their footprint visible but leave their removal to the
        # installation owner, which knows the active/default selection.
        if root.name == "installations" or not root.is_dir():
            continue
        selected = [row for row in victims if row[2].is_relative_to(root)]
        count, size = _sweep_victims(
            root, selected, dry_run=dry_run, now=when, protected_paths=protected_paths,
        )
        removed += count
        reclaimed += size
    if not dry_run:
        try:
            stamp.parent.mkdir(parents=True, exist_ok=True)
            stamp.write_text(str(int(when)), encoding="utf-8")
        except OSError:
            pass
    return {
        "schema": "pcc.user-cache-sweep.v1",
        "dry_run": bool(dry_run),
        "high_bytes": high,
        "low_bytes": low,
        "entries": len(pooled),
        "bytes_before": total,
        "removed": removed,
        "reclaimed_bytes": reclaimed,
        "bytes_after": total - reclaimed,
        "per_tree_bytes": per_tree,
    }


def policy_from_environment() -> RetentionPolicy:
    return RetentionPolicy(
        high_bytes=_env_int(
            "PCC_COMPILER_CACHE_HIGH_BYTES",
            DEFAULT_HIGH_BYTES,
        ),
        low_bytes=_env_int(
            "PCC_COMPILER_CACHE_LOW_BYTES",
            DEFAULT_LOW_BYTES,
        ),
        max_unused_days=_env_float(
            "PCC_COMPILER_CACHE_MAX_UNUSED_DAYS",
            DEFAULT_MAX_UNUSED_DAYS,
        ),
        scan_limit=_env_int(
            "PCC_COMPILER_CACHE_SCAN_LIMIT",
            DEFAULT_SCAN_LIMIT,
        ),
        auto_interval_seconds=_env_float(
            "PCC_COMPILER_CACHE_AUTO_INTERVAL_SECONDS",
            DEFAULT_AUTO_INTERVAL_SECONDS,
        ),
    ).validated()


def _is_digest(text: str) -> bool:
    return len(text) == 64 and all(char in _HEX for char in text)


def _cache_root(root: os.PathLike[str] | str) -> Path:
    return Path(root).expanduser().resolve()


def _meta_root(root: Path) -> Path:
    return root / _META_DIR


def _state_path(root: Path) -> Path:
    return _meta_root(root) / _STATE_FILE


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        path.name + ".tmp." + str(os.getpid()) + "." + str(time.time_ns())
    )
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _read_timestamp_ns(path: Path, fallback_ns: int) -> int:
    marker = Path(str(path) + _LAST_USED_SUFFIX)
    try:
        raw = marker.read_text(encoding="ascii").strip()
        value = int(raw)
        return value if value >= 0 else fallback_ns
    except (OSError, ValueError):
        return fallback_ns


def record_successful_access(
    path: os.PathLike[str] | str,
    *,
    now_ns: int | None = None,
) -> bool:
    """Record explicit cache recency without relying on filesystem atime."""

    entry = Path(path)
    timestamp = time.time_ns() if now_ns is None else int(now_ns)
    marker = Path(str(entry) + _LAST_USED_SUFFIX)
    try:
        if not entry.exists():
            return False
        _atomic_write_text(
            marker,
            str(timestamp) + "\n",
        )
        if not entry.exists():
            marker.unlink(missing_ok=True)
            return False
        return True
    except OSError:
        return False


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _owner_record(path: Path) -> tuple[int, int]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return int(payload.get("pid", 0)), int(payload.get("created_ns", 0))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return 0, 0


def acquire_entry_lease(
    path: os.PathLike[str] | str,
    *,
    now_ns: int | None = None,
    owner_pid: int | None = None,
) -> str:
    """Create a reader lease and return its exact path, or ``""`` on failure."""

    entry = Path(path)
    timestamp = time.time_ns() if now_ns is None else int(now_ns)
    lease_owner = os.getpid() if owner_pid is None else int(owner_pid)
    if lease_owner <= 0:
        return ""
    eviction = Path(str(entry) + _EVICT_SUFFIX)
    nonce = hashlib.sha256(
        (str(entry) + ":" + str(lease_owner) + ":" + str(timestamp)).encode(
            "utf-8"
        )
    ).hexdigest()[:16]
    lease = Path(str(entry) + _LEASE_INFIX + str(lease_owner) + "." + nonce)
    try:
        entry.parent.mkdir(parents=True, exist_ok=True)
        if eviction.exists():
            return ""
        fd = os.open(str(lease), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(
                    {"pid": lease_owner, "created_ns": timestamp},
                    stream,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                stream.write("\n")
        except BaseException:
            lease.unlink(missing_ok=True)
            raise
        # The eviction marker and lease form a two-way handshake.  Whichever
        # side publishes second observes the first and backs out before either
        # payload validation or quarantine begins.
        if eviction.exists():
            lease.unlink(missing_ok=True)
            return ""
        return str(lease)
    except OSError:
        lease.unlink(missing_ok=True)
        return ""


def release_entry_lease(lease_path: os.PathLike[str] | str) -> None:
    if not lease_path:
        return
    try:
        Path(lease_path).unlink()
    except OSError:
        pass


def _entry_leases(path: Path, now_ns: int) -> list[Path]:
    prefix = path.name + _LEASE_INFIX
    leases: list[Path] = []
    try:
        siblings = list(path.parent.iterdir())
    except OSError:
        return leases
    stale_ns = int(DEFAULT_LEASE_STALE_SECONDS * 1_000_000_000)
    for sibling in siblings:
        if not sibling.name.startswith(prefix) or not sibling.is_file():
            continue
        pid, created_ns = _owner_record(sibling)
        malformed_owner = created_ns <= 0 or pid <= 0
        if created_ns <= 0:
            try:
                created_ns = int(sibling.stat().st_mtime_ns)
            except OSError:
                leases.append(sibling)
                continue
        if not malformed_owner and _pid_alive(pid):
            leases.append(sibling)
            continue
        if malformed_owner and now_ns - created_ns <= stale_ns:
            leases.append(sibling)
            continue
        try:
            sibling.unlink()
        except OSError:
            leases.append(sibling)
    return leases


def _publish_lock_active(
    path: Path,
    now_ns: int,
    *,
    cleanup_stale: bool = False,
) -> bool:
    lock = Path(str(path) + ".lock")
    try:
        if not lock.is_dir():
            return lock.exists()
    except OSError:
        return True
    pid, created_ns = _owner_record(lock / "owner.json")
    malformed_owner = pid <= 0 or created_ns <= 0
    if created_ns <= 0:
        try:
            created_ns = int(lock.stat().st_mtime_ns)
        except OSError:
            return True
    stale_ns = int(DEFAULT_LOCK_STALE_SECONDS * 1_000_000_000)
    if not malformed_owner and _pid_alive(pid):
        return True
    if malformed_owner and now_ns - created_ns <= stale_ns:
        return True
    if not cleanup_stale:
        return False
    stale = lock.with_name(
        lock.name + ".stale." + str(os.getpid()) + "." + str(now_ns)
    )
    try:
        os.replace(lock, stale)
        shutil.rmtree(stale, ignore_errors=True)
        return lock.exists()
    except OSError:
        return True


def _entry_is_protected(path: Path, now_ns: int, protected: set[str]) -> bool:
    if str(path.resolve()) in protected:
        return True
    if _publish_lock_active(path, now_ns):
        return True
    return bool(_entry_leases(path, now_ns))


def _directory_size(path: Path) -> tuple[int, int]:
    total = 0
    latest_ns = 0
    pending = [path]
    while pending:
        current = pending.pop()
        try:
            children = list(current.iterdir())
        except OSError:
            continue
        for child in children:
            try:
                if child.is_dir() and not child.is_symlink():
                    pending.append(child)
                elif child.is_file():
                    stat = child.stat()
                    total += int(stat.st_size)
                    latest_ns = max(latest_ns, int(stat.st_mtime_ns))
            except OSError:
                continue
    return total, latest_ns


def _candidate_key(root: Path, kind: str, path: Path) -> str:
    order = "0" if kind == "frontend-ir" else "1"
    return order + "\t" + path.relative_to(root).as_posix()


def _candidate_batch(
    root: Path,
    cursor: str,
    scan_limit: int | None,
) -> tuple[list[tuple[str, Path]], bool]:
    """Return one deterministic shard-bounded scan batch.

    Automatic maintenance never materializes the full cache inventory.  It
    visits at most ``scan_limit`` complete entries, persists the last composite
    key, and resumes from that key.  Only the current two-hex shard is listed;
    the 256 possible shard names themselves are generated rather than scanned.
    A limit-sized terminal batch deliberately needs one cheap follow-up pass to
    prove end-of-cycle, avoiding a hidden full-tail scan on every lookup.
    """

    limit = None if scan_limit is None else max(1, int(scan_limit))
    rows: list[tuple[str, Path]] = []
    nested_frontend = root / "frontend-ir"
    frontend = nested_frontend if nested_frontend.is_dir() else root
    # The incremental Mach-O caches are shard/<digest>.<ext> + a .sha256
    # sibling -- exactly the "object" entry shape -- but they live under their
    # own version directory and were never scanned.  They therefore grew
    # without bound while the policy reported the cache as under its low-water
    # mark: 7.2 GiB unaccounted for in one checkout.
    incremental = root / "macho-incremental-v1"
    categories = (
        ("frontend-ir", frontend),
        ("object", root),
        ("object", incremental / "image"),
        ("object", incremental / "native"),
    )
    for kind, base in categories:
        order = "0" if kind == "frontend-ir" else "1"
        if cursor and order < cursor[:1]:
            continue
        for shard_number in range(256):
            shard_name = f"{shard_number:02x}"
            shard = base / shard_name
            try:
                shard_relative = shard.relative_to(root).as_posix()
            except ValueError:
                continue
            terminal_name = "f" * 64
            if kind == "object":
                # Highest accepted suffix, so the shard cursor still bounds
                # every object-shaped entry (.o/.s/.pco/.macho).
                terminal_name += ".s"
            shard_max_key = order + "\t" + shard_relative + "/" + terminal_name
            if cursor and shard_max_key <= cursor:
                continue
            try:
                if not shard.is_dir():
                    continue
            except OSError:
                continue
            try:
                entries = sorted(shard.iterdir(), key=lambda item: item.name)
            except OSError:
                continue
            for entry in entries:
                try:
                    if kind == "frontend-ir":
                        complete = (
                            entry.is_dir()
                            and _is_digest(entry.name)
                            and (entry / "manifest.json").is_file()
                            and (entry / "ir.bundle").is_file()
                        )
                    else:
                        # `.o`/`.s` are the self-backend object cache; `.pco`
                        # and `.macho` are the incremental link caches, which
                        # share this entry shape (digest name + a `.sha256`
                        # sibling) but were rejected here as incomplete and so
                        # never counted, never evicted.
                        complete = (
                            entry.suffix in (".o", ".s", ".pco", ".macho")
                            and _is_digest(entry.stem)
                            and entry.is_file()
                        )
                except OSError:
                    complete = False
                if not complete:
                    continue
                key = _candidate_key(root, kind, entry)
                if cursor and key <= cursor:
                    continue
                rows.append((kind, entry))
                if limit is not None and len(rows) >= limit:
                    return rows, False
    return rows, True


def _inspect_entry(
    root: Path,
    kind: str,
    path: Path,
    now_ns: int,
    protected: set[str],
) -> CacheEntry | None:
    try:
        if kind == "frontend-ir":
            manifest = path / "manifest.json"
            bundle = path / "ir.bundle"
            if not manifest.is_file() or not bundle.is_file():
                return None
            size, latest_ns = _directory_size(path)
            if size <= 0:
                return None
        else:
            stat = path.stat()
            if stat.st_size <= 0:
                return None
            size = int(stat.st_size)
            latest_ns = int(stat.st_mtime_ns)
            checksum = Path(str(path) + ".sha256")
            if checksum.is_file():
                checksum_stat = checksum.stat()
                size += int(checksum_stat.st_size)
                latest_ns = max(latest_ns, int(checksum_stat.st_mtime_ns))
        used_ns = _read_timestamp_ns(path, latest_ns)
        return CacheEntry(
            kind=kind,
            path=path,
            relative_path=path.relative_to(root).as_posix(),
            size_bytes=size,
            last_used_ns=used_ns,
            # Dynamic leases/locks are rechecked only for selected victims.
            # Avoid listing the same shard once per indexed entry during an
            # automatic bounded scan.
            protected=str(path.resolve()) in protected,
        )
    except (OSError, ValueError):
        return None


def _empty_state() -> dict:
    return {
        "schema": STATE_SCHEMA,
        "cursor": "",
        "scan_cycle": 0,
        "index_complete": False,
        "last_auto_ns": 0,
        "entries": {},
    }


def _load_state(root: Path) -> dict:
    try:
        state = json.loads(_state_path(root).read_text(encoding="utf-8"))
        if state.get("schema") != STATE_SCHEMA:
            return _empty_state()
        if not isinstance(state.get("entries"), dict):
            return _empty_state()
        return state
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return _empty_state()


def _save_state(root: Path, state: dict) -> None:
    state["schema"] = STATE_SCHEMA
    _atomic_write_text(
        _state_path(root),
        json.dumps(state, sort_keys=True, separators=(",", ":")) + "\n",
    )


def _acquire_maintenance_lock(root: Path, now_ns: int) -> Path | None:
    lock = _meta_root(root) / _PRUNE_LOCK
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock.mkdir()
    except FileExistsError:
        owner = lock / "owner.json"
        pid, created_ns = _owner_record(owner)
        stale_ns = int(DEFAULT_LOCK_STALE_SECONDS * 1_000_000_000)
        malformed_owner = pid <= 0 or created_ns <= 0
        if created_ns <= 0:
            try:
                created_ns = int(lock.stat().st_mtime_ns)
            except OSError:
                return None
        if not malformed_owner and _pid_alive(pid):
            return None
        if malformed_owner and now_ns - created_ns <= stale_ns:
            return None
        stale = lock.with_name(
            lock.name + ".stale." + str(os.getpid()) + "." + str(now_ns)
        )
        try:
            os.replace(lock, stale)
            shutil.rmtree(stale, ignore_errors=True)
            lock.mkdir()
        except OSError:
            return None
    except OSError:
        return None
    try:
        _atomic_write_text(
            lock / "owner.json",
            json.dumps(
                {"pid": os.getpid(), "created_ns": now_ns},
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
        )
    except OSError:
        shutil.rmtree(lock, ignore_errors=True)
        return None
    return lock


def _release_maintenance_lock(lock: Path | None) -> None:
    if lock is not None:
        shutil.rmtree(lock, ignore_errors=True)


def _recover_quarantine(root: Path) -> tuple[int, int]:
    quarantine = _meta_root(root) / _QUARANTINE_DIR
    removed = 0
    failed = 0
    try:
        entries = list(quarantine.iterdir())
    except OSError:
        return removed, failed
    for entry in entries:
        try:
            transaction = (
                entry / "transaction.json"
                if entry.is_dir() and not entry.is_symlink()
                else None
            )
            if transaction is not None and transaction.is_file():
                try:
                    payload = json.loads(transaction.read_text(encoding="utf-8"))
                    relative = str(payload.get("relative_path", ""))
                    if not relative:
                        raise ValueError("empty quarantine source path")
                    original = (root / relative).resolve()
                    original.relative_to(root)
                    if original == root:
                        raise ValueError("quarantine source resolves to cache root")
                    Path(str(original) + _EVICT_SUFFIX).unlink(missing_ok=True)
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    failed += 1
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed += 1
        except OSError:
            failed += 1
    return removed, failed


def _quarantine_entry_count(root: Path) -> int:
    try:
        return sum(
            1
            for _entry in (_meta_root(root) / _QUARANTINE_DIR).iterdir()
        )
    except OSError:
        return 0


def _scan_into_state(
    root: Path,
    state: dict,
    *,
    now_ns: int,
    protected: set[str],
    scan_limit: int | None,
) -> tuple[list[CacheEntry], int, bool]:
    cursor = str(state.get("cursor", "") or "")
    cycle = int(state.get("scan_cycle", 0) or 0)
    if not cursor:
        cycle += 1
        state["index_complete"] = False
    elif "\t" not in cursor or cursor[:1] not in ("0", "1"):
        # Pre-release/malformed cursor state is never trusted to skip entries.
        cycle += 1
        cursor = ""
        state["index_complete"] = False
    candidates, completed_cycle = _candidate_batch(root, cursor, scan_limit)
    scanned: list[CacheEntry] = []
    indexed = state.setdefault("entries", {})
    last_cursor = cursor
    for kind, path in candidates:
        relative = path.relative_to(root).as_posix()
        last_cursor = _candidate_key(root, kind, path)
        entry = _inspect_entry(root, kind, path, now_ns, protected)
        if entry is None:
            indexed.pop(relative, None)
            continue
        scanned.append(entry)
        indexed[relative] = {
            "kind": entry.kind,
            "size_bytes": entry.size_bytes,
            "last_used_ns": entry.last_used_ns,
            "seen_cycle": cycle,
        }
    if completed_cycle:
        stale = [
            relative
            for relative, row in indexed.items()
            if int(row.get("seen_cycle", -1)) != cycle
        ]
        for relative in stale:
            indexed.pop(relative, None)
        state["cursor"] = ""
        state["index_complete"] = True
    else:
        state["cursor"] = last_cursor
        state["index_complete"] = False
    state["scan_cycle"] = cycle
    return scanned, len(candidates), completed_cycle


def _entries_from_state(
    root: Path,
    state: dict,
    *,
    now_ns: int,
    protected: set[str],
    refresh_metadata: bool,
) -> list[CacheEntry]:
    out: list[CacheEntry] = []
    stale_rows: list[str] = []
    for relative, row in state.get("entries", {}).items():
        try:
            kind = str(row["kind"])
            if kind not in ("frontend-ir", "object"):
                raise ValueError("unknown compiler cache entry kind")
            path = (root / relative).resolve()
            path.relative_to(root)
            if path == root:
                raise ValueError("compiler cache entry resolves to cache root")
            stored_used_ns = max(0, int(row["last_used_ns"]))
            last_used_ns = stored_used_ns
            dynamic_protected = str(path.resolve()) in protected
            if refresh_metadata:
                if kind == "frontend-ir":
                    complete = (
                        path.is_dir()
                        and (path / "manifest.json").is_file()
                        and (path / "ir.bundle").is_file()
                    )
                else:
                    complete = path.is_file()
                if not complete:
                    stale_rows.append(relative)
                    continue
                last_used_ns = max(
                    stored_used_ns,
                    _read_timestamp_ns(path, stored_used_ns),
                )
                row["last_used_ns"] = last_used_ns
                dynamic_protected = _entry_is_protected(
                    path,
                    now_ns,
                    protected,
                )
            out.append(
                CacheEntry(
                    kind=kind,
                    path=path,
                    relative_path=relative,
                    size_bytes=max(0, int(row["size_bytes"])),
                    last_used_ns=last_used_ns,
                    protected=dynamic_protected,
                )
            )
        except (KeyError, TypeError, ValueError, OSError):
            stale_rows.append(relative)
            continue
    for relative in stale_rows:
        state.get("entries", {}).pop(relative, None)
    return out


def _stabilize_victims(
    entries: list[CacheEntry],
    state: dict,
    policy: RetentionPolicy,
    now_ns: int,
    protected: set[str],
) -> tuple[list[CacheEntry], list[CacheEntry]]:
    """Refresh only likely victims, then recompute deterministic LRU order."""

    # Concurrent hits can keep changing recency while maintenance runs.  A
    # bounded number of restarts preserves safety without allowing cache
    # maintenance to stall compilation indefinitely.
    for _attempt in range(32):
        victims = _select_victims(entries, policy, now_ns)
        changed = False
        for victim in victims:
            try:
                if victim.kind == "frontend-ir":
                    complete = (
                        victim.path.is_dir()
                        and (victim.path / "manifest.json").is_file()
                        and (victim.path / "ir.bundle").is_file()
                    )
                else:
                    complete = victim.path.is_file()
            except OSError:
                complete = False
            if not complete:
                state.get("entries", {}).pop(victim.relative_path, None)
                entries = [
                    entry
                    for entry in entries
                    if entry.relative_path != victim.relative_path
                ]
                changed = True
                continue
            latest_used = max(
                victim.last_used_ns,
                _read_timestamp_ns(victim.path, victim.last_used_ns),
            )
            is_protected = _entry_is_protected(
                victim.path,
                now_ns,
                protected,
            )
            if latest_used == victim.last_used_ns and is_protected == victim.protected:
                continue
            replacement = CacheEntry(
                kind=victim.kind,
                path=victim.path,
                relative_path=victim.relative_path,
                size_bytes=victim.size_bytes,
                last_used_ns=latest_used,
                protected=is_protected,
            )
            entries = [
                replacement if entry.relative_path == victim.relative_path else entry
                for entry in entries
            ]
            row = state.get("entries", {}).get(victim.relative_path)
            if isinstance(row, dict):
                row["last_used_ns"] = latest_used
            changed = True
        if not changed:
            return entries, victims
    return entries, []


def _select_victims(
    entries: Sequence[CacheEntry],
    policy: RetentionPolicy,
    now_ns: int,
) -> list[CacheEntry]:
    available = [entry for entry in entries if not entry.protected]
    available.sort(key=lambda entry: (entry.last_used_ns, entry.relative_path))
    total = sum(entry.size_bytes for entry in entries)
    age_ns = int(policy.max_unused_days * 86_400.0 * 1_000_000_000)
    selected: list[CacheEntry] = []
    selected_paths: set[str] = set()
    for entry in available:
        if age_ns == 0 or now_ns - entry.last_used_ns > age_ns:
            selected.append(entry)
            selected_paths.add(entry.relative_path)
            total -= entry.size_bytes
    if total <= policy.high_bytes:
        return selected

    # Keep the newest sole complete entry to avoid a publish/prune thrash loop
    # for a single bundle larger than the configured budget.  A current entry
    # is normally protected by its publisher lock/explicit protection too.
    remaining = [
        entry for entry in available if entry.relative_path not in selected_paths
    ]
    for entry in remaining:
        if total <= policy.low_bytes:
            break
        remaining_after = len(entries) - len(selected) - 1
        if remaining_after <= 0:
            break
        selected.append(entry)
        selected_paths.add(entry.relative_path)
        total -= entry.size_bytes
    return selected


def _quarantine_entry(root: Path, entry: CacheEntry, now_ns: int) -> bool:
    quarantine = _meta_root(root) / _QUARANTINE_DIR
    quarantine.mkdir(parents=True, exist_ok=True)
    token = hashlib.sha256(
        (entry.relative_path + ":" + str(now_ns)).encode("utf-8")
    ).hexdigest()[:16]
    target = quarantine / (
        entry.relative_path.replace("/", "__")
        + "."
        + str(os.getpid())
        + "."
        + token
    )
    eviction = Path(str(entry.path) + _EVICT_SUFFIX)
    payload_moved = False
    try:
        target.mkdir()
        _atomic_write_text(
            target / "transaction.json",
            json.dumps(
                {
                    "kind": entry.kind,
                    "relative_path": entry.relative_path,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
        )
        fd = os.open(str(eviction), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(
                {"pid": os.getpid(), "created_ns": now_ns},
                stream,
                sort_keys=True,
                separators=(",", ":"),
            )
            stream.write("\n")
        if _publish_lock_active(entry.path, now_ns, cleanup_stale=True):
            return False
        if _entry_leases(entry.path, now_ns):
            return False
        if _read_timestamp_ns(entry.path, entry.last_used_ns) > entry.last_used_ns:
            return False
        # Payload moves first.  The eviction marker prevents a new reader or
        # publisher from entering while the checksum/access sidecars follow.
        os.replace(entry.path, target / "payload")
        payload_moved = True
        sidecars = (
            Path(str(entry.path) + ".sha256"),
            Path(str(entry.path) + _LAST_USED_SUFFIX),
        )
        for sidecar in sidecars:
            if sidecar.exists():
                try:
                    os.replace(sidecar, target / sidecar.name)
                except OSError:
                    sidecar.unlink(missing_ok=True)
    except OSError:
        if not payload_moved:
            return False
    finally:
        eviction.unlink(missing_ok=True)
        if not payload_moved:
            shutil.rmtree(target, ignore_errors=True)
    if not payload_moved:
        return False
    try:
        shutil.rmtree(target)
    except OSError:
        # Recovery on the next maintenance pass owns this leftover.
        pass
    return True


def maintain_cache(
    root: os.PathLike[str] | str,
    *,
    policy: RetentionPolicy | None = None,
    dry_run: bool = False,
    automatic: bool = False,
    protected_paths: Iterable[os.PathLike[str] | str] = (),
    now_ns: int | None = None,
    scan_limit: int | None = None,
) -> dict:
    """Inspect/prune one combined cache root and return a stable report."""

    resolved_root = _cache_root(root)
    selected_policy = (policy or policy_from_environment()).validated()
    timestamp = time.time_ns() if now_ns is None else int(now_ns)
    protected = {str(Path(path).expanduser().resolve()) for path in protected_paths}
    report = {
        "schema": STATE_SCHEMA,
        "root": str(resolved_root),
        "automatic": bool(automatic),
        "dry_run": bool(dry_run),
        "policy": {
            "high_bytes": selected_policy.high_bytes,
            "low_bytes": selected_policy.low_bytes,
            "max_unused_days": selected_policy.max_unused_days,
            "scan_limit": selected_policy.scan_limit,
            "auto_interval_seconds": selected_policy.auto_interval_seconds,
        },
        "completed_entries": 0,
        "protected_entries": 0,
        "bytes_before": 0,
        "bytes_after": 0,
        "scanned_entries": 0,
        "index_complete": False,
        "victims": [],
        "reclaimed_bytes": 0,
        "quarantine_pending": 0,
        "quarantine_recovered": 0,
        "quarantine_failures": 0,
        "maintenance_lock_acquired": False,
        "skipped_reason": "",
    }
    if automatic and not retention_enabled():
        report["skipped_reason"] = "disabled"
        return report
    resolved_root.mkdir(parents=True, exist_ok=True)
    lock = _acquire_maintenance_lock(resolved_root, timestamp)
    if lock is None:
        report["skipped_reason"] = "maintenance-lock-busy"
        return report
    report["maintenance_lock_acquired"] = True
    try:
        state = _load_state(resolved_root)
        if automatic:
            last_auto_ns = int(state.get("last_auto_ns", 0) or 0)
            interval_ns = int(
                selected_policy.auto_interval_seconds * 1_000_000_000
            )
            if last_auto_ns and timestamp - last_auto_ns < interval_ns:
                report["skipped_reason"] = "auto-interval"
                return report
        if dry_run:
            report["quarantine_pending"] = _quarantine_entry_count(resolved_root)
            recovered = 0
            recovery_failures = 0
        else:
            recovered, recovery_failures = _recover_quarantine(resolved_root)
        report["quarantine_recovered"] = recovered
        report["quarantine_failures"] = recovery_failures
        effective_limit = scan_limit
        if automatic and effective_limit is None:
            effective_limit = selected_policy.scan_limit
        _scanned, scan_count, _completed = _scan_into_state(
            resolved_root,
            state,
            now_ns=timestamp,
            protected=protected,
            scan_limit=effective_limit,
        )
        report["scanned_entries"] = scan_count
        report["index_complete"] = bool(state.get("index_complete"))
        if automatic:
            state["last_auto_ns"] = timestamp
        entries = _entries_from_state(
            resolved_root,
            state,
            now_ns=timestamp,
            protected=protected,
            refresh_metadata=not automatic or dry_run,
        )
        if automatic and not dry_run:
            entries, victims = _stabilize_victims(
                entries,
                state,
                selected_policy,
                timestamp,
                protected,
            )
        else:
            victims = _select_victims(entries, selected_policy, timestamp)
        report["completed_entries"] = len(entries)
        report["protected_entries"] = sum(1 for entry in entries if entry.protected)
        bytes_before = sum(entry.size_bytes for entry in entries)
        report["bytes_before"] = bytes_before
        report["bytes_after"] = bytes_before
        report["victims"] = [entry.relative_path for entry in victims]
        if not state.get("index_complete") and not victims:
            report["skipped_reason"] = "incremental-index-incomplete"
        if not dry_run:
            reclaimed = 0
            for entry in victims:
                if _quarantine_entry(resolved_root, entry, timestamp):
                    reclaimed += entry.size_bytes
                    state.get("entries", {}).pop(entry.relative_path, None)
            report["reclaimed_bytes"] = reclaimed
            report["bytes_after"] = max(0, bytes_before - reclaimed)
            report["quarantine_pending"] = _quarantine_entry_count(resolved_root)
        _save_state(resolved_root, state)
        return report
    finally:
        _release_maintenance_lock(lock)


def _policy_from_args(args: argparse.Namespace) -> RetentionPolicy:
    base = policy_from_environment()
    return RetentionPolicy(
        high_bytes=base.high_bytes if args.high_bytes is None else args.high_bytes,
        low_bytes=base.low_bytes if args.low_bytes is None else args.low_bytes,
        max_unused_days=(
            base.max_unused_days
            if args.max_unused_days is None
            else args.max_unused_days
        ),
        scan_limit=base.scan_limit if args.scan_limit is None else args.scan_limit,
        auto_interval_seconds=(
            base.auto_interval_seconds
            if args.auto_interval_seconds is None
            else args.auto_interval_seconds
        ),
    ).validated()


def _add_policy_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--high-bytes", type=int)
    parser.add_argument("--low-bytes", type=int)
    parser.add_argument("--max-unused-days", type=float)
    parser.add_argument("--scan-limit", type=int)
    parser.add_argument("--auto-interval-seconds", type=float)


def _print_report(report: dict, *, automatic: bool) -> None:
    if automatic:
        if (
            report.get("reclaimed_bytes", 0)
            or report.get("quarantine_failures", 0)
            or report.get("quarantine_pending", 0)
        ):
            print(
                "pcc compiler cache retention: entries="
                + str(report.get("completed_entries", 0))
                + " reclaimed="
                + str(report.get("reclaimed_bytes", 0))
                + " bytes_after="
                + str(report.get("bytes_after", 0))
                + " quarantine_failures="
                + str(report.get("quarantine_failures", 0))
                + " quarantine_pending="
                + str(report.get("quarantine_pending", 0)),
                file=sys.stderr,
            )
        return
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m pcc.tools.compiler_cache_retention"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "prune", "auto"):
        command = subparsers.add_parser(name)
        command.add_argument("--root", required=True)
        command.add_argument("--protect", action="append", default=[])
        _add_policy_arguments(command)
    subparsers.choices["prune"].add_argument("--dry-run", action="store_true")
    touch = subparsers.add_parser("touch")
    touch.add_argument("--path", required=True)
    lease = subparsers.add_parser("lease-acquire")
    lease.add_argument("--path", required=True)
    release = subparsers.add_parser("lease-release")
    release.add_argument("--path", required=True)
    # `sweep` covers every pcc cache tree under ~/.cache against one combined
    # budget; `prune` only knows the object cache's own root.
    sweep = subparsers.add_parser("sweep")
    sweep.add_argument("--dry-run", action="store_true")
    sweep.add_argument("--home", default=None)
    args = parser.parse_args(argv)

    if args.command == "sweep":
        report = sweep_user_caches(dry_run=args.dry_run, home=args.home)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0

    if args.command == "touch":
        return 0 if record_successful_access(args.path) else 1
    if args.command == "lease-acquire":
        lease_path = acquire_entry_lease(args.path)
        if not lease_path:
            return 1
        print(lease_path)
        return 0
    if args.command == "lease-release":
        release_entry_lease(args.path)
        return 0

    policy = _policy_from_args(args)
    report = maintain_cache(
        args.root,
        policy=policy,
        dry_run=bool(getattr(args, "dry_run", False) or args.command == "status"),
        automatic=args.command == "auto",
        protected_paths=args.protect,
        scan_limit=None if args.command != "auto" else policy.scan_limit,
    )
    _print_report(report, automatic=args.command == "auto")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
