#!/usr/bin/env python3
"""Run a pcc-owned Mach-O link after the compiled coordinator exits.

The compiled pcc1 writes a small, versioned plan after all direct artifacts
are frozen.  ``bootstrap.py`` then runs this host-side transition owner only
after pcc1 has returned, so the coordinator's allocator high water cannot
overlap the assembler/linker process tree.  The linked artifact is still
produced by pcc's own linker. Codegen plans can select a separately qualified
native linker with ``--native-linker``; otherwise linking uses host Python.
The receipt distinguishes those execution owners. This script remains a host
orchestrator and does not by itself establish a host-free public CLI path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


PLAN_SCHEMA = "pcc.deferred-self-link.v1"
RESULT_SCHEMA = "pcc.deferred-self-link-result.v1"
CODEGEN_PLAN_SCHEMA_V1 = "pcc.frontend-codegen-plan.v1"
CODEGEN_PLAN_SCHEMA = "pcc.frontend-codegen-plan.v2"
CODEGEN_RESULT_SCHEMA = "pcc.frontend-codegen-result.v1"
INDEXED_PROCESS_SPLIT_MODE = "pidx-pco-v1"


class DeferredLinkError(RuntimeError):
    pass


def _required_absolute_path(raw: str, label: str) -> Path:
    path = Path(raw)
    if not raw or not path.is_absolute():
        raise DeferredLinkError(label + " must be an absolute path")
    return path


def read_plan(path: Path) -> dict[str, object]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DeferredLinkError("cannot read deferred link plan: " + str(exc)) from exc
    if len(lines) < 7 or lines[0] != PLAN_SCHEMA:
        raise DeferredLinkError("invalid deferred link plan schema")
    output = _required_absolute_path(lines[1], "deferred output")
    runtime = None
    if lines[2]:
        runtime = _required_absolute_path(lines[2], "runtime archive")
        if not runtime.is_file():
            raise DeferredLinkError("runtime archive is missing: " + str(runtime))
    inputs = _required_absolute_path(lines[3], "internal-input manifest")
    if not inputs.is_file():
        raise DeferredLinkError("internal-input manifest is missing: " + str(inputs))
    profile = _required_absolute_path(lines[4], "link profile")
    cleanup_root = None
    if lines[5]:
        cleanup_root = _required_absolute_path(lines[5], "cleanup root")
    try:
        extra_count = int(lines[6])
    except ValueError as exc:
        raise DeferredLinkError("invalid deferred extra-input count") from exc
    if extra_count < 0 or len(lines) != 7 + extra_count:
        raise DeferredLinkError("deferred extra-input count mismatch")
    extras = []
    for raw in lines[7:]:
        extra = _required_absolute_path(raw, "deferred extra input")
        if not extra.is_file():
            raise DeferredLinkError("deferred extra input is missing: " + str(extra))
        extras.append(extra)
    return {
        "output": output,
        "runtime": runtime,
        "inputs": inputs,
        "profile": profile,
        "cleanup_root": cleanup_root,
        "extras": extras,
    }


def _persist(path: Path, payload: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def read_codegen_plan(path: Path) -> dict[str, object]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DeferredLinkError("cannot read frontend codegen plan: " + str(exc)) from exc
    if len(lines) < 11 or lines[0] not in (
        CODEGEN_PLAN_SCHEMA_V1,
        CODEGEN_PLAN_SCHEMA,
    ):
        raise DeferredLinkError("invalid frontend codegen plan schema")
    worker = _required_absolute_path(lines[1], "codegen worker")
    output = _required_absolute_path(lines[2], "codegen output")
    runtime = _required_absolute_path(lines[3], "codegen runtime")
    profile = _required_absolute_path(lines[4], "codegen link profile")
    inputs = _required_absolute_path(lines[5], "codegen internal manifest")
    artifacts = _required_absolute_path(lines[6], "codegen artifact root")
    try:
        module_count = int(lines[7])
        oversized_count = int(lines[8])
        safe_jobs = int(lines[9])
        manifest_count = int(lines[10])
    except ValueError as exc:
        raise DeferredLinkError("invalid frontend codegen plan count") from exc
    manifest_start = 11
    indexed_process_split = False
    if lines[0] == CODEGEN_PLAN_SCHEMA:
        if len(lines) < 12 or lines[11] != INDEXED_PROCESS_SPLIT_MODE:
            raise DeferredLinkError("invalid frontend codegen process split mode")
        indexed_process_split = True
        manifest_start = 12
    if (
        module_count < 1
        or oversized_count < 0
        or safe_jobs < 1
        or safe_jobs > 2
        or manifest_count != module_count
        or oversized_count > manifest_count
        or len(lines) != manifest_start + manifest_count
    ):
        raise DeferredLinkError("frontend codegen plan count mismatch")
    manifests = []
    for raw in lines[manifest_start:]:
        manifest = _required_absolute_path(raw, "codegen worker manifest")
        if not manifest.is_file():
            raise DeferredLinkError("codegen worker manifest is missing: " + str(manifest))
        manifests.append(manifest)
    if not worker.is_file() or not os.access(worker, os.X_OK):
        raise DeferredLinkError("codegen worker is not executable: " + str(worker))
    if not runtime.is_file():
        raise DeferredLinkError("codegen runtime is missing: " + str(runtime))
    if not artifacts.is_dir():
        raise DeferredLinkError("codegen artifact root is missing: " + str(artifacts))
    return {
        "worker": worker,
        "output": output,
        "runtime": runtime,
        "profile": profile,
        "inputs": inputs,
        "artifacts": artifacts,
        "module_count": module_count,
        "oversized_count": oversized_count,
        "safe_jobs": safe_jobs,
        "indexed_process_split": indexed_process_split,
        "manifests": manifests,
    }


def _codegen_worker_environment(
    *,
    assembly_only: bool,
    indexed_sidecar: bool = False,
) -> dict[str, str]:
    environment = os.environ.copy()
    environment["PCC_PY_FRONTEND_JOBS"] = "1"
    environment["PCC_DIRECT_INDEXED_NATIVE_OBJECT"] = (
        "0" if assembly_only else "1"
    )
    if indexed_sidecar:
        environment.update(
            {
                "PCC_DIRECT_INDEXED_KERNEL_CAPTURE": "1",
                "PCC_DIRECT_INDEXED_KERNEL_EMIT": "1",
                "PCC_DIRECT_INDEXED_KERNEL_FUSE_USES": "1",
                "PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND": "1",
                "PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK": "1",
                "PCC_DIRECT_INDEXED_SIDECAR": "1",
            }
        )
    else:
        environment.pop("PCC_DIRECT_INDEXED_SIDECAR", None)
    for name in (
        "PCC_DEFER_FRONTEND_CODEGEN_PLAN",
        "PCC_DEFER_FRONTEND_OUTPUT",
        "PCC_DEFER_SELF_LINK_PLAN",
        "PCC_PY_FRONTEND_IN_PROCESS_CODEGEN",
    ):
        environment.pop(name, None)
    return environment


def _run_codegen_worker(worker: Path, manifest: Path, *, assembly_only: bool) -> int:
    completed = subprocess.run(
        [str(worker), "--pcc-python-multi-codegen-worker", str(manifest)],
        check=False,
        env=_codegen_worker_environment(assembly_only=assembly_only),
    )
    return int(completed.returncode)


def _result_path_from_worker_manifest(path: Path) -> Path:
    lines = path.read_text(encoding="utf-8").splitlines()
    if len(lines) < 2 or lines[0] != "pcc.frontends.python.codegen_worker.v4":
        raise DeferredLinkError("invalid deferred worker manifest: " + str(path))
    return _required_absolute_path(lines[1], "worker result")


def _worker_manifest_ast_bytes(path: Path) -> int:
    lines = path.read_text(encoding="utf-8").splitlines()
    if (
        len(lines) < 12
        or lines[0] != "pcc.frontends.python.codegen_worker.v4"
        or lines[-2] != "1"
    ):
        raise DeferredLinkError(
            "deferred scheduling requires a singleton V4 manifest: " + str(path)
        )
    try:
        source_index = int(lines[-1])
    except ValueError as exc:
        raise DeferredLinkError("invalid deferred manifest source index") from exc
    ast_path = Path(lines[5]) / ("module_" + str(source_index) + ".json")
    try:
        return ast_path.stat().st_size
    except OSError as exc:
        raise DeferredLinkError("deferred AST sidecar is missing: " + str(ast_path)) from exc


def _partition_codegen_lanes(plan: dict[str, object]) -> dict[str, list[Path]]:
    manifests = list(plan["manifests"])
    oversized_count = int(plan["oversized_count"])
    serial = manifests[:1] if oversized_count else []
    paired_oversized = manifests[len(serial) : oversized_count]
    heavy = []
    medium = []
    small = []
    for manifest in manifests[oversized_count:]:
        ast_bytes = _worker_manifest_ast_bytes(manifest)
        if ast_bytes >= 3_000_000:
            heavy.append(manifest)
        elif ast_bytes >= 2_000_000:
            medium.append(manifest)
        else:
            small.append(manifest)
    return {
        "serial": serial,
        "paired_oversized": paired_oversized,
        "heavy": heavy,
        "medium": medium,
        "small": small,
    }


_GIB = 1024 * 1024 * 1024
# Reserve kept for the driver/link tail; admission also demands this much
# free headroom before launching one more worker (a fresh worker's RSS is
# near zero at launch and grows afterwards, so the reserve is growth room).
_WINDOW_DRIVER_RESERVE_BYTES = 1 * _GIB
_WINDOW_LAUNCH_RESERVE_BYTES = 2 * _GIB
_WINDOW_POLL_S = 0.2
# Per-worker admission floor derived from the AST sidecar size.  A fresh pcc1
# worker reads near-zero RSS on its first polls, so live RSS alone cannot
# stop several workers from being admitted in one window and ballooning
# together.  Calibrated from the capped Stage2 receipt
# build/inline-edge-stage2-capped-v2 (per-worker peak RSS while it was the
# tree's largest process): small band 1.75-1.92 MB AST -> 2.75+ GiB, medium
# 2.0-3.0 MB -> 1.3-2.7 GiB, heavy 3.2-4.4 MB -> 2.1-4.2 GiB, paired 6.5-8 MB
# -> 3.0-4.0 GiB, serial 13.9 MB -> 6.0 GiB.  Growth is sublinear above ~3 MB
# (frontend state is released before emit), hence the cap; the live-RSS
# ladder below remains the backstop for outliers above their floor.
# ponytail: linear-in-AST floor from one receipt; replace with per-manifest
# measured peaks once workers report their own max RSS in the result row.
_WORKER_FLOOR_BASE_BYTES = 9 * _GIB // 10
_WORKER_FLOOR_PER_AST_MB_BYTES = 12 * _GIB // 10
_WORKER_FLOOR_CAP_BYTES = 7 * _GIB // 2
_INDEXED_ASM_EMIT_BASE_BYTES = 2 * _GIB // 5
# PCO admission shares the native controller's collector-specific envelope.
# The live-RSS ladder and external tree breaker remain independent backstops.
_INDEXED_ASM_PER_SIDECAR_MB_BYTES = 7 * _GIB // 100
_INDEXED_EMIT_FLOOR_CAP_BYTES = 6 * _GIB


def _worker_floor_bytes(ast_bytes: int) -> int:
    scaled = (max(0, int(ast_bytes)) * _WORKER_FLOOR_PER_AST_MB_BYTES) // 1_000_000
    floor = _WORKER_FLOOR_BASE_BYTES + scaled
    return floor if floor < _WORKER_FLOOR_CAP_BYTES else _WORKER_FLOOR_CAP_BYTES


def _lane_floors(manifests: list[Path]) -> list[int]:
    return [_worker_floor_bytes(_worker_manifest_ast_bytes(m)) for m in manifests]


def _indexed_emit_floor_bytes(sidecar: Path, *, assembly_only: bool) -> int:
    """Charge a fresh emitter from its exact packed input, not source AST size."""

    payload_bytes = sidecar.stat().st_size
    if assembly_only:
        floor = _INDEXED_ASM_EMIT_BASE_BYTES + (
            payload_bytes * _INDEXED_ASM_PER_SIDECAR_MB_BYTES
        ) // 1_000_000
    else:
        from pcc.frontends.python.deferred_frontend_schedule import indexed_pco_floor_bytes
        raw_gc = str(os.environ.get("PCC_GC_BACKEND", "0"))
        return indexed_pco_floor_bytes(payload_bytes, 0 if raw_gc == "0" else -1)
    if floor > _INDEXED_EMIT_FLOOR_CAP_BYTES:
        return _INDEXED_EMIT_FLOOR_CAP_BYTES
    return floor


def _indexed_emit_floors(
    sidecars: list[Path],
    *,
    assembly_only: bool,
) -> list[int]:
    return [
        _indexed_emit_floor_bytes(path, assembly_only=assembly_only)
        for path in sidecars
    ]


def _indexed_frontend_floor_bytes(ast_bytes: int) -> int:
    """Bound the post-split frontend from the complete v48 226-worker sample."""
    from pcc.frontends.python.deferred_frontend_schedule import indexed_frontend_floor_bytes
    return indexed_frontend_floor_bytes(ast_bytes)


def _indexed_frontend_floors(manifests: list[Path]) -> list[int]:
    return [
        _indexed_frontend_floor_bytes(_worker_manifest_ast_bytes(manifest))
        for manifest in manifests
    ]
# Hard per-worker deadline (repo rule: no child runs unbounded).  The slowest
# legitimate deferred worker measured to date is ~64s (cli_bootstrap, serial
# lane); the default leaves an order of magnitude for cold caches and load.
_WORKER_DEADLINE_DEFAULT_S = 900.0


def _worker_deadline_s() -> float:
    raw = os.environ.get("PCC_DEFERRED_WORKER_TIMEOUT_S", "").strip()
    try:
        value = float(raw) if raw else 0.0
    except ValueError:
        value = 0.0
    if value > 0:
        return value
    return _WORKER_DEADLINE_DEFAULT_S


def _worker_tree_budget_bytes() -> int:
    raw = os.environ.get("PCC_WORKER_TREE_BUDGET_BYTES", "").strip()
    try:
        value = int(raw) if raw else 0
    except ValueError:
        value = 0
    if value > 0:
        return value
    return 8 * _GIB


def _live_rss_by_pid(pids: list[int]) -> dict[int, int]:
    if not pids:
        return {}
    completed = subprocess.run(
        ["ps", "-o", "pid=,rss=", "-p", ",".join(str(pid) for pid in pids)],
        capture_output=True,
        text=True,
        check=False,
    )
    observed: dict[int, int] = {}
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            observed[int(parts[0])] = int(parts[1]) * 1024
    return observed


def _live_rss_bytes(pids: list[int]) -> int:
    return sum(_live_rss_by_pid(pids).values())


def _first_admissible_pending_offset(
    pending_positions: list[int],
    floors: list[int],
    charged_bytes: int,
    soft_ceiling_bytes: int,
) -> int:
    """Return the first deterministic pending item that fits, or -1."""

    offset = 0
    while offset < len(pending_positions):
        if charged_bytes + floors[pending_positions[offset]] <= soft_ceiling_bytes:
            return offset
        offset += 1
    return -1


def _run_codegen_batches(
    worker: Path,
    manifests: list[Path],
    *,
    width: int,
    assembly_only: bool,
    lane: str,
    floors: list[int] | None = None,
    receipt_path: Path | None = None,
    receipt_extra: dict[str, object] | None = None,
    indexed_sidecar: bool = False,
    indexed_emit_outputs: list[Path] | None = None,
) -> dict[str, object]:
    """Sliding window with charged aggregate-RSS admission and a pressure ladder.

    The previous wave scheduler launched a full batch and waited for ALL of
    it, so (a) each wave cost as long as its slowest member and (b) a batch
    of memory-ballooning workers could cross the external 8 GiB breaker in
    aggregate (Stage2 v8 died at 8.59 GiB with four concurrent small-lane
    workers; evidence 005).  The first sliding window then admitted on live
    RSS alone; a fresh worker reads near zero for its first polls, so the
    width-4 small lane still launched four workers in one 0.2 s window and
    crossed the breaker ten seconds later (inline-edge-stage2-capped-v2 at
    564 s: 2.75 + 2.56 + 1.58 + 1.34 GiB, all still growing).  Here
    ``width`` is only a cap:

    - every running worker is charged ``max(live RSS, floor)`` where
      ``floors[i]`` is the expected peak of ``manifests[i]`` (default: the
      launch reserve); a new worker is admitted only when a slot is free
      AND the charged sum plus its own floor fits the budget minus the
      driver reserve;
    - when live RSS crosses the soft ceiling, every runnable worker but the
      oldest is SIGSTOPped in the same poll (the youngest have the most
      growth left; stopping one per poll let concurrent growth outrun the
      ladder) until pressure drops;
    - an empty window always admits one worker, so pressure misreads can
      only serialize the lane, never stall it.

    ``stats["workers"]`` records every worker's manifest, floor, wall, time
    spent suspended and peak observed RSS; when ``receipt_path`` is given the
    stats (plus ``receipt_extra``) are rewritten after every worker exit so a
    lane killed by the external breaker still leaves per-module evidence.
    """
    lane_started = time.monotonic()
    if width < 1:
        raise DeferredLinkError("deferred lane width must be positive")
    if floors is None:
        floors = [_WINDOW_LAUNCH_RESERVE_BYTES] * len(manifests)
    if len(floors) != len(manifests):
        raise DeferredLinkError("deferred lane floors must align with manifests")
    if indexed_emit_outputs is not None and len(indexed_emit_outputs) != len(manifests):
        raise DeferredLinkError("indexed emit outputs must align with sidecars")
    budget = _worker_tree_budget_bytes()
    soft_ceiling = budget - _WINDOW_DRIVER_RESERVE_BYTES
    deadline_s = _worker_deadline_s()
    phase = "legacy-combined"
    if indexed_emit_outputs is not None:
        phase = "emit"
    elif indexed_sidecar:
        phase = "frontend"
    stats: dict[str, object] = {
        "lane": lane,
        "phase": phase,
        "width": width,
        "soft_ceiling_bytes": soft_ceiling,
        "launched": 0,
        "admission_denied": 0,
        "suspensions": 0,
        "resumes": 0,
        "peak_live_bytes": 0,
        "peak_charged_bytes": 0,
        "workers": [],
    }
    running: list[tuple[Path, subprocess.Popen, int]] = []
    suspended: list[subprocess.Popen] = []
    deadlines: dict[int, float] = {}
    # pid -> {started, suspended_at, suspended_s, peak_rss}
    records: dict[int, dict[str, float]] = {}
    failure: tuple[Path, int | str] | None = None
    pending_positions = list(range(len(manifests)))

    def persist() -> None:
        stats["elapsed_s"] = round(time.monotonic() - lane_started, 3)
        if receipt_path is None:
            return
        payload: dict[str, object] = dict(receipt_extra or {})
        payload["lane"] = stats
        _persist(receipt_path, payload)

    def finish(manifest: Path, process: subprocess.Popen, floor: int, outcome: object) -> None:
        record = records.pop(process.pid, {})
        now = time.monotonic()
        if record.get("suspended_at", 0.0) > 0.0:
            record["suspended_s"] = record.get("suspended_s", 0.0) + (
                now - record["suspended_at"]
            )
        stats["workers"].append(
            {
                "manifest": manifest.name,
                "floor_bytes": floor,
                "wall_s": round(now - record.get("started", now), 3),
                "suspended_s": round(record.get("suspended_s", 0.0), 3),
                "peak_rss_bytes": int(record.get("peak_rss", 0)),
                "outcome": outcome,
            }
        )
        persist()

    def launch(pending_offset: int) -> None:
        position = pending_positions.pop(pending_offset)
        manifest = manifests[position]
        floor = floors[position]
        output = (
            None
            if indexed_emit_outputs is None
            else indexed_emit_outputs[position]
        )
        command = [
            str(worker),
            "--pcc-python-multi-codegen-worker",
            str(manifest),
        ]
        if output is not None:
            command = [
                str(worker),
                "--pcc-self-backend-indexed-emit-worker",
                str(manifest),
                str(output),
                "ASM" if assembly_only else "PCO",
            ]
        process = subprocess.Popen(
            command,
            env=_codegen_worker_environment(
                assembly_only=assembly_only,
                indexed_sidecar=indexed_sidecar and output is None,
            ),
        )
        deadlines[process.pid] = time.monotonic() + deadline_s
        records[process.pid] = {"started": time.monotonic(), "suspended_s": 0.0, "peak_rss": 0.0}
        running.append((manifest, process, floor))
        stats["launched"] += 1

    while pending_positions or running:
        still: list[tuple[Path, subprocess.Popen, int]] = []
        for manifest, process, floor in running:
            returncode = process.poll()
            if returncode is None:
                # The deadline clock is frozen while suspended: on SIGSTOP the
                # remaining time is banked (negative marker), on SIGCONT it is
                # rearmed from now.
                if (
                    process not in suspended
                    and time.monotonic() > deadlines.get(process.pid, 0.0)
                ):
                    process.kill()
                    process.wait()
                    finish(manifest, process, floor, "timeout")
                    if failure is None:
                        failure = (manifest, "timed out after %.0fs" % deadline_s)
                    continue
                still.append((manifest, process, floor))
            else:
                if process in suspended:
                    suspended.remove(process)
                deadlines.pop(process.pid, None)
                finish(manifest, process, floor, returncode)
                if returncode != 0 and failure is None:
                    failure = (manifest, returncode)
        running = still
        if failure is not None:
            for process in suspended:
                process.send_signal(signal.SIGCONT)
            suspended = []
            for _manifest, process, _floor in running:
                process.wait()
            manifest, returncode = failure
            raise DeferredLinkError(
                lane
                + " deferred worker failed rc="
                + str(returncode)
                + ": "
                + str(manifest)
            )
        observed = _live_rss_by_pid([process.pid for _m, process, _f in running])
        live = sum(observed.values())
        charged = 0
        for _manifest, process, floor in running:
            rss = observed.get(process.pid, 0)
            charged += rss if rss > floor else floor
            record = records.get(process.pid)
            if record is not None and rss > record["peak_rss"]:
                record["peak_rss"] = float(rss)
        if live > stats["peak_live_bytes"]:
            stats["peak_live_bytes"] = live
        if charged > stats["peak_charged_bytes"]:
            stats["peak_charged_bytes"] = charged
        active = len(running) - len(suspended)
        if live > soft_ceiling and active > 1:
            for _manifest, process, _floor in reversed(running):
                if active <= 1:
                    break
                if process not in suspended:
                    process.send_signal(signal.SIGSTOP)
                    suspended.append(process)
                    records[process.pid]["suspended_at"] = time.monotonic()
                    deadlines[process.pid] = -(
                        deadlines.get(process.pid, time.monotonic())
                        - time.monotonic()
                    )
                    stats["suspensions"] += 1
                    active -= 1
        elif suspended and (
            active == 0 or charged + _WINDOW_LAUNCH_RESERVE_BYTES <= soft_ceiling
        ):
            process = suspended.pop()
            process.send_signal(signal.SIGCONT)
            record = records.get(process.pid)
            if record is not None and record.get("suspended_at", 0.0) > 0.0:
                record["suspended_s"] = record.get("suspended_s", 0.0) + (
                    time.monotonic() - record["suspended_at"]
                )
                record["suspended_at"] = 0.0
            remaining = deadlines.get(process.pid, deadline_s)
            if remaining < 0:
                remaining = -remaining
            if remaining <= 0:
                remaining = deadline_s
            deadlines[process.pid] = time.monotonic() + remaining
            stats["resumes"] += 1
        elif pending_positions and not running:
            launch(0)
            continue
        elif pending_positions and len(running) < width and not suspended:
            pending_offset = _first_admissible_pending_offset(
                pending_positions,
                floors,
                charged,
                soft_ceiling,
            )
            if pending_offset >= 0:
                launch(pending_offset)
                continue
            stats["admission_denied"] += 1
        if running:
            time.sleep(_WINDOW_POLL_S)
    persist()
    return stats


def _indexed_sidecar_items(
    manifests: list[Path],
    artifact_root: Path,
    *,
    assembly_only: bool,
) -> tuple[list[Path], list[Path]]:
    sidecars = []
    outputs = []
    resolved_root = artifact_root.resolve()
    for manifest in manifests:
        result_path = _result_path_from_worker_manifest(manifest)
        rows = result_path.read_text(encoding="utf-8").splitlines()
        if len(rows) != 1:
            raise DeferredLinkError("indexed frontend result must contain one module")
        parts = rows[0].split("\t")
        if (
            len(parts) < 9
            or parts[0] != "OK"
            or parts[3] != "0"
            or parts[4] != "0"
        ):
            raise DeferredLinkError("invalid indexed frontend worker result")
        matches = []
        marker = 7
        while marker + 1 < len(parts):
            if parts[marker] == "PIDX":
                matches.append(parts[marker + 1])
            marker += 1
        if len(matches) != 1:
            raise DeferredLinkError("indexed frontend result has no unique sidecar")
        sidecar = _required_absolute_path(matches[0], "indexed module sidecar")
        if not sidecar.is_file() or sidecar.suffix != ".pidx":
            raise DeferredLinkError("indexed module sidecar is missing or malformed")
        try:
            sidecar.resolve().relative_to(resolved_root)
        except ValueError as exc:
            raise DeferredLinkError(
                "indexed module sidecar escaped the artifact root"
            ) from exc
        sidecars.append(sidecar)
        outputs.append(sidecar.with_suffix(".s" if assembly_only else ".pco"))
    return sidecars, outputs


def _publish_indexed_emit_results(
    manifests: list[Path],
    sidecars: list[Path],
    outputs: list[Path],
    *,
    artifact_kind: str,
) -> None:
    if artifact_kind not in ("ASM", "PCO"):
        raise DeferredLinkError("indexed emit publication kind is invalid")
    if len(manifests) != len(sidecars) or len(manifests) != len(outputs):
        raise DeferredLinkError("indexed emit publication inventory mismatch")
    for manifest, sidecar, output in zip(manifests, sidecars, outputs):
        if not output.is_file():
            raise DeferredLinkError("indexed emit worker artifact is missing")
        result_path = _result_path_from_worker_manifest(manifest)
        rows = result_path.read_text(encoding="utf-8").splitlines()
        if len(rows) != 1:
            raise DeferredLinkError("indexed emit result must contain one module")
        parts = rows[0].split("\t")
        replacements = 0
        marker = 7
        while marker + 1 < len(parts):
            if parts[marker] == "PIDX" and parts[marker + 1] == str(sidecar):
                parts[marker] = artifact_kind
                parts[marker + 1] = str(output)
                replacements += 1
            marker += 1
        if replacements != 1:
            raise DeferredLinkError("indexed emit result publication mismatch")
        temporary = result_path.with_suffix(result_path.suffix + ".tmp")
        temporary.write_text("\t".join(parts) + "\n", encoding="utf-8")
        os.replace(temporary, result_path)

def _collect_codegen_artifacts(plan: dict[str, object]) -> None:
    module_count = int(plan["module_count"])
    by_index: list[tuple[str, str] | None] = [None] * module_count
    for manifest in plan["manifests"]:
        result_path = _result_path_from_worker_manifest(manifest)
        if not result_path.is_file():
            raise DeferredLinkError("deferred worker produced no result: " + str(manifest))
        rows = result_path.read_text(encoding="utf-8").splitlines()
        if len(rows) != 1:
            raise DeferredLinkError("deferred worker result must contain one module")
        parts = rows[0].split("\t")
        if not parts or parts[0] == "ERR":
            raise DeferredLinkError("deferred worker reported an error: " + rows[0])
        if len(parts) < 9 or parts[0] != "OK" or parts[3] != "0" or parts[4] != "0":
            raise DeferredLinkError("invalid deferred worker result")
        try:
            index = int(parts[1])
        except ValueError as exc:
            raise DeferredLinkError("invalid deferred worker index") from exc
        kind = ""
        artifact_path = ""
        marker = 7
        while marker + 1 < len(parts):
            if parts[marker] in ("ASM", "PCO"):
                kind = parts[marker]
                artifact_path = parts[marker + 1]
                break
            marker += 1
        if (
            index < 0
            or index >= module_count
            or by_index[index] is not None
            or kind not in ("ASM", "PCO")
        ):
            raise DeferredLinkError("deferred worker ownership mismatch")
        artifact = _required_absolute_path(artifact_path, "worker artifact")
        if not artifact.is_file():
            raise DeferredLinkError("deferred worker artifact is missing: " + str(artifact))
        by_index[index] = (kind, str(artifact))
    if any(item is None for item in by_index):
        raise DeferredLinkError("deferred workers did not cover every module")
    input_manifest = plan["inputs"]
    with input_manifest.open("w", encoding="utf-8") as stream:
        stream.write("pcc.macho-internal-inputs.v1\n")
        stream.write(str(module_count) + "\n")
        for item in by_index:
            if item is None:
                raise DeferredLinkError("deferred artifact disappeared")
            stream.write(item[0] + "\t" + item[1] + "\n")


def run_codegen_plan(
    plan_path: Path, *, timeout_s: int, native_linker: Path | None = None,
) -> dict[str, object]:
    if timeout_s <= 0:
        raise DeferredLinkError("frontend codegen timeout must be positive")
    plan = read_codegen_plan(plan_path)
    native_linker_sha256 = ""
    if native_linker is not None:
        if not plan["indexed_process_split"]:
            raise DeferredLinkError("native linker requires an indexed v2 codegen plan")
        native_linker = native_linker.expanduser().resolve(strict=True)
        if not os.access(native_linker, os.X_OK):
            raise DeferredLinkError("native linker is not executable")
        with native_linker.open("rb") as stream:
            if stream.read(4) not in (b"\xcf\xfa\xed\xfe", b"\x7fELF", b"\xca\xfe\xba\xbe", b"\xca\xfe\xba\xbf"):
                raise DeferredLinkError("native linker must be a native executable")
            stream.seek(0)
            native_linker_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
    worker = plan["worker"]
    lanes = _partition_codegen_lanes(plan)
    admission: dict[str, dict[str, object]] = {}
    admission_receipt = Path(str(plan_path) + ".admission.json")
    lane_plan = (
        ("serial", 1, True, None),
        ("paired_oversized", 2, True, None),
        ("heavy", 2, True, _lane_floors(lanes["heavy"])),
        ("medium", 4, True, _lane_floors(lanes["medium"])),
        ("small", 8, False, _lane_floors(lanes["small"])),
    )
    if plan["indexed_process_split"]:
        asm_manifests = []
        for lane_name in ("serial", "paired_oversized", "heavy", "medium"):
            asm_manifests.extend(lanes[lane_name])
        pco_manifests = list(lanes["small"])
        frontend_manifests = list(asm_manifests)
        frontend_manifests.extend(pco_manifests)
        if native_linker is not None:
            # The native linker consumes packed objects directly. Keeping the
            # host linker's large-ASM lanes would make one native process retain
            # their assembler state together with every previously read object.
            # Fresh emit workers bound each module's allocation high water.
            pco_manifests = list(frontend_manifests)
            asm_manifests = []
        frontend_admission = _run_codegen_batches(
            worker,
            frontend_manifests,
            width=12,
            assembly_only=False,
            lane="indexed-frontend",
            floors=_indexed_frontend_floors(frontend_manifests),
            receipt_path=admission_receipt,
            receipt_extra={
                "schema": "pcc.frontend-codegen-admission.v2",
                "plan": str(plan_path),
                "completed_phases": {},
            },
            indexed_sidecar=True,
        )
        asm_sidecars, asm_outputs = _indexed_sidecar_items(
            asm_manifests,
            plan["artifacts"],
            assembly_only=True,
        )
        asm_admission = _run_codegen_batches(
            worker,
            asm_sidecars,
            width=12,
            assembly_only=True,
            lane="indexed-asm-emit",
            floors=_indexed_emit_floors(asm_sidecars, assembly_only=True),
            receipt_path=admission_receipt,
            receipt_extra={
                "schema": "pcc.frontend-codegen-admission.v2",
                "plan": str(plan_path),
                "completed_phases": {"frontend": frontend_admission},
            },
            indexed_emit_outputs=asm_outputs,
        )
        _publish_indexed_emit_results(
            asm_manifests,
            asm_sidecars,
            asm_outputs,
            artifact_kind="ASM",
        )
        pco_sidecars, pco_outputs = _indexed_sidecar_items(
            pco_manifests,
            plan["artifacts"],
            assembly_only=False,
        )
        pco_admission = _run_codegen_batches(
            worker,
            pco_sidecars,
            width=12,
            assembly_only=False,
            lane="indexed-pco-emit",
            floors=_indexed_emit_floors(pco_sidecars, assembly_only=False),
            receipt_path=admission_receipt,
            receipt_extra={
                "schema": "pcc.frontend-codegen-admission.v2",
                "plan": str(plan_path),
                "completed_phases": {
                    "frontend": frontend_admission,
                    "asm_emit": asm_admission,
                },
            },
            indexed_emit_outputs=pco_outputs,
        )
        _publish_indexed_emit_results(
            pco_manifests,
            pco_sidecars,
            pco_outputs,
            artifact_kind="PCO",
        )
        admission = {
            "frontend": frontend_admission,
            "asm_emit": asm_admission,
            "pco_emit": pco_admission,
        }
    else:
        for name, width, assembly_only, floors in lane_plan:
            admission[name] = _run_codegen_batches(
                worker,
                lanes[name],
                width=width,
                assembly_only=assembly_only,
                lane=name.replace("_", "-"),
                floors=floors,
                receipt_path=admission_receipt,
                receipt_extra={
                    "schema": "pcc.frontend-codegen-admission.v1",
                    "plan": str(plan_path),
                    "completed_lanes": dict(admission),
                },
            )
    _collect_codegen_artifacts(plan)
    driver = Path(__file__).resolve().with_name("pcc_link_macho.py")
    command = (
        [sys.executable, str(driver), "--profile-json", str(plan["profile"])]
        if native_linker is None else [str(native_linker)]
    ) + [
        "--out",
        str(plan["output"]),
        "--internal-input-manifest",
        str(plan["inputs"]),
        "--archive",
        str(plan["runtime"]),
    ]
    try:
        linked = subprocess.run(command, check=False, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise DeferredLinkError("deferred frontend link timed out") from exc
    output = plan["output"]
    if linked.returncode != 0 or not output.is_file() or not os.access(output, os.X_OK):
        raise DeferredLinkError(
            "deferred frontend link failed rc=" + str(linked.returncode)
        )
    lane_summary = {}
    for name, jobs in (
        ("serial", 1),
        ("paired_oversized", 2),
        ("heavy", 2),
        ("medium", 4),
        ("small", 8),
    ):
        lane_summary[name] = {"count": len(lanes[name])}
        if plan["indexed_process_split"]:
            lane_summary[name]["artifact_kind"] = (
                "PCO" if native_linker is not None or name == "small" else "ASM"
            )
        else:
            lane_summary[name]["jobs"] = jobs
    result = {
        "schema": (
            "pcc.frontend-codegen-result.v2"
            if plan["indexed_process_split"]
            else CODEGEN_RESULT_SCHEMA
        ),
        "status": "COMPLETE",
        "returncode": linked.returncode,
        "indexed_process_split": bool(plan["indexed_process_split"]),
        "worker_count": sum(len(items) for items in lanes.values()),
        "oversized_count": int(plan["oversized_count"]),
        "safe_jobs": int(plan["safe_jobs"]),
        "lanes": lane_summary,
        "indexed_phases": (
            admission if plan["indexed_process_split"] else {}
        ),
        "legacy_lane_admission": (
            {} if plan["indexed_process_split"] else admission
        ),
        "output": str(output),
        "profile": str(plan["profile"]) if native_linker is None else "",
        "link_execution_owner": "host-cpython" if native_linker is None else "native-linker",
        "native_linker_sha256": native_linker_sha256,
        "command": command,
    }
    _persist(Path(str(plan_path) + ".result.json"), result)
    return result


def run(plan_path: Path, *, timeout_s: int) -> dict[str, object]:
    if timeout_s <= 0:
        raise DeferredLinkError("deferred link timeout must be positive")
    plan = read_plan(plan_path)
    driver = Path(__file__).resolve().with_name("pcc_link_macho.py")
    if not driver.is_file():
        raise DeferredLinkError("pcc Mach-O driver is missing: " + str(driver))
    command = [
        sys.executable,
        str(driver),
        "--out",
        str(plan["output"]),
        "--profile-json",
        str(plan["profile"]),
        "--internal-input-manifest",
        str(plan["inputs"]),
    ]
    runtime = plan["runtime"]
    if runtime is not None:
        command.extend(["--archive", str(runtime)])
    for extra in plan["extras"]:
        command.extend(["--object", str(extra)])
    try:
        completed = subprocess.run(command, check=False, timeout=timeout_s)
    except subprocess.TimeoutExpired as exc:
        raise DeferredLinkError("deferred pcc link timed out") from exc
    output = plan["output"]
    if (
        completed.returncode != 0
        or not output.is_file()
        or not os.access(output, os.X_OK)
    ):
        raise DeferredLinkError(
            "deferred pcc link failed rc=" + str(completed.returncode)
        )
    result = {
        "schema": RESULT_SCHEMA,
        "status": "COMPLETE",
        "returncode": completed.returncode,
        "command": command,
        "output": str(output),
        "profile": str(plan["profile"]),
        # Retain the exact temporary root for later evidence/cleanup.  This
        # helper never deletes a path supplied by a plan.
        "cleanup_root": (
            "" if plan["cleanup_root"] is None else str(plan["cleanup_root"])
        ),
    }
    _persist(Path(str(plan_path) + ".result.json"), result)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--codegen-plan")
    parser.add_argument("--native-linker", type=Path,
                        help="native owned linker executable for --codegen-plan")
    parser.add_argument("plan")
    parser.add_argument(
        "command",
        nargs=argparse.REMAINDER,
        help="optional compiler command to finish before consuming the plan",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.native_linker is not None and not args.codegen_plan:
        raise DeferredLinkError("--native-linker requires --codegen-plan")
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if command:
        completed = subprocess.run(command, check=False)
        if completed.returncode != 0:
            return int(completed.returncode)
    if args.codegen_plan:
        try:
            result = run_codegen_plan(
                Path(args.codegen_plan).expanduser().resolve(),
                timeout_s=args.timeout,
                native_linker=args.native_linker,
            )
        except (DeferredLinkError, OSError, ValueError) as exc:
            print("deferred pcc codegen error: " + str(exc), file=sys.stderr)
            return 1
        print(
            "PCC_DEFERRED_CODEGEN_COMPLETE output=" + str(result["output"]),
            flush=True,
        )
        return 0
    try:
        result = run(Path(args.plan).expanduser().resolve(), timeout_s=args.timeout)
    except (DeferredLinkError, OSError, ValueError) as exc:
        print("deferred pcc link error: " + str(exc), file=sys.stderr)
        return 1
    print("PCC_DEFERRED_LINK_COMPLETE output=" + str(result["output"]), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
