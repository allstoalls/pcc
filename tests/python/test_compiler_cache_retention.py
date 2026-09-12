from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from pcc.py_frontend import compile_cache
from pcc.tools import compiler_cache_retention as retention


_NOW_NS = 2_000_000_000_000_000_000
_DAY_NS = 86_400 * 1_000_000_000


def _digest(index: int) -> str:
    return f"{index:064x}"


def _write_used(path: Path, when_ns: int) -> None:
    Path(str(path) + ".pcc-last-used").write_text(
        str(when_ns) + "\n",
        encoding="ascii",
    )


def _frontend_entry(root: Path, index: int, size: int, used_ns: int) -> Path:
    digest = _digest(index)
    entry = root / "frontend-ir" / digest[:2] / digest
    entry.mkdir(parents=True)
    (entry / "manifest.json").write_text("{}\n", encoding="utf-8")
    (entry / "ir.bundle").write_bytes(b"x" * size)
    _write_used(entry, used_ns)
    return entry


def _object_entry(root: Path, index: int, size: int, used_ns: int) -> Path:
    digest = _digest(index)
    entry = root / digest[:2] / (digest + ".o")
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_bytes(bytes([index % 251]) * size)
    Path(str(entry) + ".sha256").write_text("0" * 64 + "\n", encoding="ascii")
    _write_used(entry, used_ns)
    return entry


def _policy(
    *,
    high: int,
    low: int,
    days: float = 30.0,
    scan_limit: int = 512,
) -> retention.RetentionPolicy:
    return retention.RetentionPolicy(
        high_bytes=high,
        low_bytes=low,
        max_unused_days=days,
        scan_limit=scan_limit,
        auto_interval_seconds=0.0,
    )


def test_user_sweep_honors_automatic_disable(tmp_path, monkeypatch):
    entry = tmp_path / ".cache/pcc/compile-cache/aa/entry"
    entry.mkdir(parents=True)
    (entry / "payload").write_bytes(b"old")
    monkeypatch.setenv("PCC_COMPILER_CACHE_RETENTION", "0")
    monkeypatch.setenv("PCC_COMPILER_CACHE_MAX_UNUSED_DAYS", "0")
    report = retention.sweep_user_caches(home=tmp_path, automatic=True)
    assert report["skipped_reason"] == "disabled"
    assert entry.is_dir()


def test_user_sweep_preserves_leases_locks_protected_paths_and_installations(tmp_path, monkeypatch):
    root = tmp_path / ".cache/pcc/compile-cache/aa"
    entries = []
    for name in ("leased", "locked", "protected", "unused"):
        entry = root / name
        entry.mkdir(parents=True)
        (entry / "payload").write_bytes(b"cached")
        entries.append(entry)
    lease = retention.acquire_entry_lease(entries[0])
    lock = Path(str(entries[1]) + ".lock")
    lock.write_text("builder lock")
    installed = tmp_path / ".cache/pcc/installations/current"
    installed.mkdir(parents=True)
    (installed / "pcc1").write_bytes(b"compiler")
    monkeypatch.setenv("PCC_COMPILER_CACHE_MAX_UNUSED_DAYS", "0")
    try:
        report = retention.sweep_user_caches(home=tmp_path, protected_paths=[entries[2]])
        assert all(entry.is_dir() for entry in entries[:3])
        assert Path(lease).is_file()
        assert lock.is_file()
        assert installed.is_dir()
        assert not entries[3].exists()
        assert report["removed"] == 1
    finally:
        retention.release_entry_lease(lease)


def test_lru_size_policy_is_deterministic_without_large_fixture(tmp_path: Path):
    oldest = _object_entry(tmp_path, 1, 20, _NOW_NS - 3 * _DAY_NS)
    middle = _object_entry(tmp_path, 2, 20, _NOW_NS - 2 * _DAY_NS)
    newest = _object_entry(tmp_path, 3, 20, _NOW_NS - _DAY_NS)

    report = retention.maintain_cache(
        tmp_path,
        policy=_policy(high=200, low=100, days=365),
        dry_run=True,
        now_ns=_NOW_NS,
    )

    assert report["bytes_before"] == 3 * (20 + 65)
    assert report["victims"] == [
        oldest.relative_to(tmp_path).as_posix(),
        middle.relative_to(tmp_path).as_posix(),
    ]
    assert oldest.exists() and middle.exists() and newest.exists()


def test_prune_combines_frontend_and_object_bytes_and_keeps_current_entry(
    tmp_path: Path,
):
    old_frontend = _frontend_entry(tmp_path, 10, 80, _NOW_NS - 40 * _DAY_NS)
    old_object = _object_entry(tmp_path, 11, 40, _NOW_NS - 35 * _DAY_NS)
    current = _frontend_entry(tmp_path, 12, 90, _NOW_NS)

    report = retention.maintain_cache(
        tmp_path,
        policy=_policy(high=1, low=0, days=30),
        protected_paths=(current,),
        now_ns=_NOW_NS,
    )

    assert not old_frontend.exists()
    assert not old_object.exists()
    assert current.exists()
    assert report["protected_entries"] == 1
    assert report["reclaimed_bytes"] > 0
    assert report["bytes_after"] < report["bytes_before"]


def test_explicit_access_marker_not_filesystem_atime_controls_age(tmp_path: Path):
    entry = _object_entry(tmp_path, 20, 20, _NOW_NS - 31 * _DAY_NS)
    # A current artifact mtime cannot override the explicit last-used marker.
    entry.touch()
    report = retention.maintain_cache(
        tmp_path,
        policy=_policy(high=10_000, low=9_000, days=30),
        dry_run=True,
        now_ns=_NOW_NS,
    )
    assert report["victims"] == [entry.relative_to(tmp_path).as_posix()]


def test_active_lease_and_frontend_publish_lock_are_protected(tmp_path: Path):
    leased = _object_entry(tmp_path, 30, 20, _NOW_NS - 90 * _DAY_NS)
    locked = _frontend_entry(tmp_path, 31, 20, _NOW_NS - 90 * _DAY_NS)
    publish_lock = Path(str(locked) + ".lock")
    publish_lock.mkdir()
    (publish_lock / "owner.json").write_text(
        json.dumps({"pid": os.getpid(), "created_ns": _NOW_NS}) + "\n",
        encoding="utf-8",
    )
    lease = retention.acquire_entry_lease(leased, now_ns=_NOW_NS)
    assert lease
    try:
        report = retention.maintain_cache(
            tmp_path,
            policy=_policy(high=1, low=0, days=1),
            now_ns=_NOW_NS,
        )
    finally:
        retention.release_entry_lease(lease)

    assert leased.exists() and locked.exists()
    assert report["protected_entries"] == 2
    assert report["victims"] == []


def test_temporary_publications_are_not_complete_entries(tmp_path: Path):
    shard = tmp_path / "aa"
    shard.mkdir()
    temporary = shard / (_digest(40) + ".o.123.tmp")
    temporary.write_bytes(b"temporary")
    checksum_temporary = shard / (_digest(40) + ".o.123.tmp.sha256")
    checksum_temporary.write_text("not-complete\n", encoding="ascii")

    report = retention.maintain_cache(
        tmp_path,
        policy=_policy(high=1, low=0),
        now_ns=_NOW_NS,
    )
    assert report["completed_entries"] == 0
    assert temporary.exists() and checksum_temporary.exists()


def test_corrupt_persisted_index_cannot_escape_the_cache_root(tmp_path: Path):
    outside = tmp_path.parent / (tmp_path.name + "-outside.o")
    outside.write_bytes(b"do-not-delete")
    state = retention._empty_state()
    state["entries"]["../" + outside.name] = {
        "kind": "object",
        "size_bytes": outside.stat().st_size,
        "last_used_ns": 0,
        "seen_cycle": 1,
    }

    entries = retention._entries_from_state(
        tmp_path,
        state,
        now_ns=_NOW_NS,
        protected=set(),
        refresh_metadata=False,
    )

    assert entries == []
    assert state["entries"] == {}
    assert outside.read_bytes() == b"do-not-delete"


def test_single_oversized_recent_entry_is_retained_to_avoid_thrash(tmp_path: Path):
    only = _frontend_entry(tmp_path, 50, 256, _NOW_NS)
    report = retention.maintain_cache(
        tmp_path,
        policy=_policy(high=32, low=16, days=30),
        now_ns=_NOW_NS,
    )
    assert report["bytes_before"] > 32
    assert report["victims"] == []
    assert only.exists()


def test_repository_and_per_user_roots_apply_the_same_policy(tmp_path: Path):
    reports = []
    for root_name in ("repository-cache", "user-cache"):
        root = tmp_path / root_name
        oldest = _object_entry(root, 60, 20, _NOW_NS - 3 * _DAY_NS)
        _object_entry(root, 61, 20, _NOW_NS - 2 * _DAY_NS)
        _object_entry(root, 62, 20, _NOW_NS - _DAY_NS)
        report = retention.maintain_cache(
            root,
            policy=_policy(high=200, low=100, days=365),
            dry_run=True,
            now_ns=_NOW_NS,
        )
        reports.append((report, oldest.relative_to(root).as_posix()))

    assert reports[0][0]["policy"] == reports[1][0]["policy"]
    assert reports[0][0]["victims"] == reports[1][0]["victims"]
    assert reports[0][1] == reports[1][1]


def test_environment_overrides_and_disabled_auto_remain_visible(
    tmp_path: Path,
    monkeypatch,
):
    entry = _object_entry(tmp_path, 70, 20, _NOW_NS - 90 * _DAY_NS)
    monkeypatch.setenv("PCC_COMPILER_CACHE_RETENTION", "off")
    monkeypatch.setenv("PCC_COMPILER_CACHE_HIGH_BYTES", "1")
    monkeypatch.setenv("PCC_COMPILER_CACHE_LOW_BYTES", "0")
    monkeypatch.setenv("PCC_COMPILER_CACHE_MAX_UNUSED_DAYS", "1")
    automatic = retention.maintain_cache(
        tmp_path,
        automatic=True,
        now_ns=_NOW_NS,
    )
    assert automatic["skipped_reason"] == "disabled"
    assert entry.exists()

    manual = retention.maintain_cache(
        tmp_path,
        now_ns=_NOW_NS,
    )
    assert manual["policy"]["high_bytes"] == 1
    assert manual["policy"]["low_bytes"] == 0
    assert not entry.exists()


def test_automatic_scan_is_bounded_and_persists_cursor(tmp_path: Path):
    for index in range(4):
        _frontend_entry(tmp_path, 100 + index, 8, _NOW_NS - index)
    policy = _policy(high=10_000, low=9_000, scan_limit=2)

    first = retention.maintain_cache(
        tmp_path,
        policy=policy,
        automatic=True,
        now_ns=_NOW_NS,
    )
    second = retention.maintain_cache(
        tmp_path,
        policy=policy,
        automatic=True,
        now_ns=_NOW_NS + 1,
    )
    terminal = retention.maintain_cache(
        tmp_path,
        policy=policy,
        automatic=True,
        now_ns=_NOW_NS + 2,
    )

    assert first["scanned_entries"] == 2
    assert first["index_complete"] is False
    assert first["completed_entries"] == 2
    assert second["scanned_entries"] == 2
    assert second["completed_entries"] == 4
    # A limit-sized last batch uses one empty bounded pass to prove that the
    # cursor reached the end, rather than secretly scanning the full tail.
    assert second["index_complete"] is False
    assert terminal["scanned_entries"] == 0
    assert terminal["index_complete"] is True
    assert terminal["completed_entries"] == 4


def test_recent_marker_refreshes_an_older_persisted_index_row(tmp_path: Path):
    refreshed = _object_entry(tmp_path, 110, 20, _NOW_NS - 20 * _DAY_NS)
    victim = _object_entry(tmp_path, 111, 20, _NOW_NS - 10 * _DAY_NS)
    retention.maintain_cache(
        tmp_path,
        policy=_policy(high=10_000, low=9_000, days=365),
        dry_run=True,
        now_ns=_NOW_NS,
    )
    _write_used(refreshed, _NOW_NS + 1)

    report = retention.maintain_cache(
        tmp_path,
        policy=_policy(high=100, low=0, days=365, scan_limit=1),
        automatic=True,
        now_ns=_NOW_NS + 2,
    )

    assert report["victims"] == [victim.relative_to(tmp_path).as_posix()]
    assert refreshed.exists()
    assert not victim.exists()


def test_live_reader_lease_does_not_expire_only_because_it_is_old(tmp_path: Path):
    entry = _object_entry(tmp_path, 120, 20, _NOW_NS - 90 * _DAY_NS)
    lease = retention.acquire_entry_lease(
        entry,
        now_ns=_NOW_NS - 7 * 60 * 60 * 1_000_000_000,
    )
    assert lease
    try:
        report = retention.maintain_cache(
            tmp_path,
            policy=_policy(high=1, low=0, days=1),
            now_ns=_NOW_NS,
        )
    finally:
        retention.release_entry_lease(lease)

    assert report["protected_entries"] == 1
    assert entry.exists()


def test_eviction_marker_and_reader_lease_use_two_way_handshake(tmp_path: Path):
    entry = _object_entry(tmp_path, 130, 20, _NOW_NS)
    eviction = Path(str(entry) + ".pcc-evict")
    eviction.write_text("{}\n", encoding="utf-8")
    try:
        assert retention.acquire_entry_lease(entry, now_ns=_NOW_NS) == ""
    finally:
        eviction.unlink()

    lease = retention.acquire_entry_lease(entry, now_ns=_NOW_NS)
    assert lease
    try:
        candidate = retention.CacheEntry(
            kind="object",
            path=entry,
            relative_path=entry.relative_to(tmp_path).as_posix(),
            size_bytes=entry.stat().st_size,
            last_used_ns=_NOW_NS,
            protected=False,
        )
        assert retention._quarantine_entry(tmp_path, candidate, _NOW_NS) is False
        assert entry.exists()
    finally:
        retention.release_entry_lease(lease)


def test_interrupted_quarantine_is_recovered(tmp_path: Path):
    quarantine = tmp_path / ".pcc-cache-retention" / "quarantine"
    leftover = quarantine / "interrupted"
    leftover.mkdir(parents=True)
    (leftover / "payload").write_bytes(b"old")

    report = retention.maintain_cache(
        tmp_path,
        policy=_policy(high=10_000, low=9_000),
        now_ns=_NOW_NS,
    )
    assert report["quarantine_recovered"] == 1
    assert not leftover.exists()


def test_interrupted_quarantine_releases_its_eviction_marker(tmp_path: Path):
    entry = _object_entry(tmp_path, 140, 20, _NOW_NS)
    quarantine = tmp_path / ".pcc-cache-retention" / "quarantine"
    leftover = quarantine / "interrupted-transaction"
    leftover.mkdir(parents=True)
    (leftover / "transaction.json").write_text(
        json.dumps(
            {
                "kind": "object",
                "relative_path": entry.relative_to(tmp_path).as_posix(),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    eviction = Path(str(entry) + ".pcc-evict")
    eviction.write_text("{}\n", encoding="utf-8")

    report = retention.maintain_cache(
        tmp_path,
        policy=_policy(high=10_000, low=9_000),
        now_ns=_NOW_NS,
    )

    assert report["quarantine_recovered"] == 1
    assert not eviction.exists()
    assert entry.exists()


def test_cleanup_failure_is_reported_and_recovered_on_next_pass(
    tmp_path: Path,
    monkeypatch,
):
    entry = _object_entry(tmp_path, 145, 20, _NOW_NS - 90 * _DAY_NS)
    real_rmtree = retention.shutil.rmtree

    def fail_quarantine_delete(path, *args, **kwargs):
        candidate = Path(path)
        if candidate.parent.name == "quarantine" and (
            candidate / "payload"
        ).exists():
            raise OSError("synthetic quarantine cleanup failure")
        return real_rmtree(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(retention.shutil, "rmtree", fail_quarantine_delete)
        first = retention.maintain_cache(
            tmp_path,
            policy=_policy(high=1, low=0, days=1),
            now_ns=_NOW_NS,
        )

    assert not entry.exists()
    assert first["quarantine_pending"] == 1
    recovered = retention.maintain_cache(
        tmp_path,
        policy=_policy(high=10_000, low=9_000),
        now_ns=_NOW_NS + 1,
    )
    assert recovered["quarantine_recovered"] == 1
    assert recovered["quarantine_pending"] == 0


def test_dead_maintenance_owner_is_reclaimed_without_waiting_for_age(
    tmp_path: Path,
):
    lock = tmp_path / ".pcc-cache-retention" / "prune.lock"
    lock.mkdir(parents=True)
    (lock / "owner.json").write_text(
        json.dumps({"pid": 999_999_999, "created_ns": _NOW_NS}) + "\n",
        encoding="utf-8",
    )

    report = retention.maintain_cache(
        tmp_path,
        policy=_policy(high=10_000, low=9_000),
        now_ns=_NOW_NS + 1,
    )

    assert report["maintenance_lock_acquired"] is True
    assert not lock.exists()


def test_dead_frontend_publisher_lock_is_recovered_before_quarantine(
    tmp_path: Path,
):
    entry = _frontend_entry(tmp_path, 150, 20, _NOW_NS - 90 * _DAY_NS)
    lock = Path(str(entry) + ".lock")
    lock.mkdir()
    (lock / "owner.json").write_text(
        json.dumps({"pid": 999_999_999, "created_ns": _NOW_NS}) + "\n",
        encoding="utf-8",
    )

    report = retention.maintain_cache(
        tmp_path,
        policy=_policy(high=1, low=0, days=1),
        now_ns=_NOW_NS + 1,
    )

    assert report["reclaimed_bytes"] > 0
    assert not entry.exists()
    assert not lock.exists()


def test_two_pruners_serialize_and_never_leave_partial_object_pair(tmp_path: Path):
    entry = _object_entry(tmp_path, 200, 20, _NOW_NS - 90 * _DAY_NS)
    policy = _policy(high=1, low=0, days=1)

    def run_pruner() -> dict:
        return retention.maintain_cache(
            tmp_path,
            policy=policy,
            now_ns=_NOW_NS,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        reports = list(pool.map(lambda _index: run_pruner(), range(2)))

    assert sum(bool(report["maintenance_lock_acquired"]) for report in reports) >= 1
    assert not entry.exists()
    assert not Path(str(entry) + ".sha256").exists()


def test_manual_status_and_dry_run_cli_report_policy_and_victims(tmp_path: Path):
    # This path crosses a real subprocess/CLI boundary, so unlike the direct
    # maintain_cache fixtures above it must use the same wall clock as the
    # child.  A fixed future timestamp would make this entry look newly used.
    entry = _object_entry(tmp_path, 300, 20, time.time_ns() - 90 * _DAY_NS)
    command = [
        sys.executable,
        "-m",
        "pcc.tools.compiler_cache_retention",
        "prune",
        "--root",
        str(tmp_path),
        "--dry-run",
        "--high-bytes",
        "1",
        "--low-bytes",
        "0",
        "--max-unused-days",
        "1",
    ]
    result = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    report = json.loads(result.stdout)
    assert report["root"] == str(tmp_path.resolve())
    assert report["dry_run"] is True
    assert report["policy"]["high_bytes"] == 1
    assert report["victims"] == [entry.relative_to(tmp_path).as_posix()]
    assert entry.exists()


def test_pipeline_cache_paths_carry_reader_and_publisher_leases() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    pipeline_source = (repo_root / "pcc/py_frontend/pipeline.py").read_text(
        encoding="utf-8"
    )
    compile_cache_source = (
        repo_root / "pcc/py_frontend/compile_cache.py"
    ).read_text(encoding="utf-8")
    self_backend_host_source = (
        repo_root / "pcc/py_frontend/pipeline_self_backend_host.py"
    ).read_text(encoding="utf-8")
    assert "'.pcc-lease.'" in self_backend_host_source
    assert "'.pcc-last-used'" in self_backend_host_source
    assert "'.pcc-evict'" in self_backend_host_source
    assert "_maintain_self_backend_object_cache" in pipeline_source
    assert "_pipeline_self_backend_host._COMPILER_CACHE_RETENTION_HOST_CODE" in (
        pipeline_source
    )
    assert '"lease-acquire"' in compile_cache_source
    assert '"lease-release"' in compile_cache_source
    assert "owner_pid=os.getppid()" in compile_cache_source
    assert 'entry + ".pcc-evict"' in compile_cache_source
    assert '"auto"' in compile_cache_source


def test_frontend_publish_survives_retention_helper_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "demo.py"
    source.write_text("value = 1\n", encoding="utf-8")
    cache_root = tmp_path / "frontend-cache"
    monkeypatch.setenv("PCC_PY_FRONTEND_IR_CACHE_IDENTITY", "retention-test")
    monkeypatch.setenv("PCC_PY_FRONTEND_IR_CACHE_DIR", str(cache_root))
    plan = compile_cache.plan_python_frontend_ir_cache(
        [str(source)],
        ["demo"],
        compiler_executable=sys.executable,
        host_python=sys.executable,
        entry_module="demo",
        sibling_inits=[],
        libpython_mode="off",
        ir_scaffold_mode="on",
        source_root=str(Path(__file__).resolve().parents[2]),
    )
    assert plan is not None
    assert compile_cache.acquire_python_frontend_ir_cache(plan)
    original_retention_helper = compile_cache._retention_helper_output

    def fail_retention(*_args, **_kwargs):
        raise OSError("synthetic retention failure")

    monkeypatch.setattr(
        compile_cache,
        "_retention_helper_output",
        fail_retention,
    )
    try:
        ir_text = "define i32 @main() { ret i32 0 }\n"
        assert compile_cache.publish_python_frontend_ir_cache(
            plan,
            ([("demo", ir_text)], False, False, len(ir_text), []),
        )
    finally:
        compile_cache.release_python_frontend_ir_cache(plan)
        monkeypatch.setattr(
            compile_cache,
            "_retention_helper_output",
            original_retention_helper,
        )

    loaded = compile_cache.load_python_frontend_ir_cache(plan, ["demo"])
    assert loaded is not None
    assert loaded[0] == [("demo", ir_text)]
