#!/usr/bin/env python3
"""Run one receipt-bound Stage2 from an already proven Stage1 directory.

This is the single-arm counterpart to ``run_pcc_stage_ab.py``.  It reuses that
tool's Stage2 implementation and process-tree sampler, but does not rebuild a
control/candidate Stage1 pair merely to obtain one current-source Stage2
correctness/profile receipt.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import run_pcc_stage_ab as stage_ab
import run_process_tree_sample as memory_guard


class Stage2ReceiptError(RuntimeError):
    pass


_GIB = 1024 * 1024 * 1024
_HOST_MEMORY_RESERVE_BYTES = memory_guard.DEFAULT_HOST_MEMORY_RESERVE_BYTES
_MIN_PRESSURED_SWAP_FREE_BYTES = 4 * _GIB
# See process-tree-guard-swap-false-positive-highram.md: waive the tiny-swap
# refusal when reclaimable physical memory clears this multiple of the budget.
_SWAP_PRESSURE_RECLAIMABLE_MARGIN = 2

# Measured on the 224-module compiler closure: ~180s coordinator checkpoint
# plus ~50s pcc-owned link (docs/goal/evidence/
# HARNESS-P0-STAGE2-MEMORY-SAFE-DEFAULT/002-stage2-critical-path-prediction.md).
STAGE2_CHECKPOINT_AND_LINK_RESERVE_S = 230.0


def predict_stage2_seconds(prior_stage2_dir: Path) -> dict[str, object]:
    """Predict a full Stage2 wall from a retained partial run's plan state.

    The deferred codegen plan schedules largest-first, so scaling the observed
    lane wall by the completed source-byte fraction is a fit, not a guess: the
    per-module cost tracks source size and the expensive head is what the
    partial run already paid for.
    """
    prior = Path(prior_stage2_dir).expanduser().resolve(strict=True)
    plans = sorted(prior.glob("**/*.pcc-codegen-plan"))
    if not plans:
        raise Stage2ReceiptError(
            "prediction state has no retained codegen plan: " + str(prior)
        )
    plan = plans[0]
    states = sorted(
        plan.parent.glob(plan.name + ".state.*"),
        key=lambda path: path.stat().st_mtime,
    )
    if not states:
        raise Stage2ReceiptError(
            "prediction state has no retained plan state next to " + str(plan)
        )
    state = states[-1]
    manifests = sorted((state / "manifests").glob("worker_*.manifest"))
    if not manifests:
        raise Stage2ReceiptError(
            "prediction state has no worker manifest under " + str(state)
        )
    module_bytes: dict[str, int] = {}
    for line in manifests[0].read_text(encoding="utf-8").splitlines():
        parts = line.split("\t")
        if len(parts) == 3 and parts[0].isdigit():
            try:
                module_bytes[parts[1]] = os.path.getsize(parts[2])
            except OSError:
                module_bytes[parts[1]] = 0
    total_bytes = sum(module_bytes.values())
    if not module_bytes or total_bytes <= 0:
        raise Stage2ReceiptError(
            "prediction manifest lists no measurable module sources"
        )
    completed: dict[str, float] = {}
    for result in sorted((state / "results").glob("worker_*.tsv")):
        fields = result.read_text(encoding="utf-8").split("\t")
        if len(fields) > 2 and fields[0] == "OK":
            completed[fields[2]] = result.stat().st_mtime
    if not completed:
        raise Stage2ReceiptError(
            "prediction needs at least one completed worker result under "
            + str(state)
        )
    completed_bytes = sum(module_bytes.get(name, 0) for name in completed)
    fraction = completed_bytes / float(total_bytes)
    if fraction <= 0.0:
        raise Stage2ReceiptError(
            "completed worker results own zero source bytes; cannot predict"
        )
    observed_lane_s = max(completed.values()) - plan.stat().st_mtime
    if observed_lane_s < 1.0:
        observed_lane_s = 1.0
    predicted_lane_s = observed_lane_s / fraction
    return {
        "schema": "pcc.stage2-prediction.v1",
        "plan": str(plan),
        "state": str(state),
        "total_modules": len(module_bytes),
        "completed_modules": len(completed),
        "total_bytes": total_bytes,
        "completed_bytes": completed_bytes,
        "observed_lane_s": observed_lane_s,
        "predicted_lane_s": predicted_lane_s,
        "checkpoint_and_link_reserve_s": STAGE2_CHECKPOINT_AND_LINK_RESERVE_S,
        "predicted_total_s": predicted_lane_s
        + STAGE2_CHECKPOINT_AND_LINK_RESERVE_S,
    }


def _validate_stage2_prediction(
    prediction: dict[str, object],
    stage2_timeout: int,
) -> None:
    predicted = float(prediction["predicted_total_s"])
    if predicted > float(stage2_timeout):
        raise Stage2ReceiptError(
            "stage2 prediction "
            + f"{predicted:.0f}s exceeds the {stage2_timeout}s contract "
            + f"({prediction['completed_modules']}/{prediction['total_modules']}"
            + " modules observed); a full rerun is forbidden until the "
            + "per-module cost drops or the contract is re-derived from this "
            + "prediction"
        )


def _memory_call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except memory_guard.ProcessTreeSampleError as exc:
        raise Stage2ReceiptError(str(exc)) from exc


def _parse_scaled_bytes(raw):
    return _memory_call(memory_guard._parse_scaled_bytes, raw)


def _parse_vm_stat_reclaimable(raw):
    return _memory_call(memory_guard._parse_vm_stat_reclaimable, raw)


def _parse_swapusage(raw):
    return _memory_call(memory_guard._parse_swapusage, raw)


def _validate_resource_observation(**observation):
    return _memory_call(memory_guard._validate_darwin_resource_observation,
                        reserve_bytes=_configured_host_memory_reserve_bytes(), **observation)


def _configured_host_memory_reserve_bytes():
    return _memory_call(memory_guard.configured_host_memory_reserve_bytes)


def _stage2_resource_preflight(max_tree_rss_bytes: int) -> dict:
    if sys.platform != "darwin":
        return {"platform": sys.platform, "max_tree_rss_bytes": max_tree_rss_bytes,
                "darwin_preflight": "not_applicable"}
    return _memory_call(memory_guard._darwin_resource_preflight,
                        max_tree_rss_bytes=max_tree_rss_bytes,
                        reserve_bytes=_configured_host_memory_reserve_bytes())


def _select_stage2_memory_budget(args):
    return _memory_call(
        memory_guard.select_tree_memory_budget, args.max_tree_rss_bytes,
        default_ceiling=stage_ab.DEFAULT_MAX_TREE_RSS_BYTES,
        reserve_bytes=_configured_host_memory_reserve_bytes(),
        external_budget=os.environ.get("PCC_WORKER_TREE_BUDGET_BYTES"),
    )


def _stage1_record(stage1_dir: Path, *, ab, stage1) -> dict[str, str]:
    root = stage1_dir.resolve(strict=True)
    manifest = stage_ab._read_json(root / "manifest.json")
    receipt_path = root / "build-receipt.json"
    receipt = stage_ab._read_json(receipt_path)
    if manifest.get("status") != "SUCCEEDED" or receipt.get("status") != "SUCCEEDED":
        raise Stage2ReceiptError("Stage1 receipt is not successful: " + str(root))
    compiler = root / "pcc1"
    if not compiler.is_file() or not os.access(compiler, os.X_OK):
        raise Stage2ReceiptError("Stage1 compiler is not executable: " + str(compiler))
    if ab.sha256_path(compiler) != receipt.get("compiler_sha256"):
        raise Stage2ReceiptError("Stage1 compiler hash disagrees with its receipt")
    source_snapshot = root / str(receipt.get("source_snapshot", ""))
    stage1._require_immutable_source(source_snapshot, ab)
    runtime_archive = root / "runtime-bundle" / "libpy_runtime_pcc_py.a"
    if not runtime_archive.is_file():
        raise Stage2ReceiptError("Stage1 runtime bundle is missing")
    if ab.sha256_path(runtime_archive) != receipt.get("runtime_archive_sha256"):
        raise Stage2ReceiptError("Stage1 runtime archive hash disagrees with its receipt")
    return {
        "compiler": str(compiler),
        "source_snapshot": str(source_snapshot),
        "receipt_path": str(receipt_path),
    }


def _runner_args(args: argparse.Namespace, receipt_environment: dict) -> argparse.Namespace:
    """Build the Namespace ``stage_ab._run_stage2`` and ``_resource_envelope`` read.

    Every attribute the envelope reads must be present: the first capped Stage2
    that ever COMPLETED (build/inline-edge-stage2-capped-v4, 1350 s, 7.28 GiB)
    then died in ``_resource_envelope`` on a missing ``frontend_jobs`` and lost
    its receipt.  ``gc_backend`` defaults to the Stage1 receipt's
    ``PCC_GC_BACKEND`` so the envelope parity key matches the stage it reuses.
    """
    gc_backend = args.gc_backend
    if gc_backend is None:
        raw = str(receipt_environment.get("PCC_GC_BACKEND", "0") or "0").strip()
        gc_backend = int(raw) if raw.isdigit() else 0
    return argparse.Namespace(
        stage2_timeout=args.stage2_timeout,
        smoke_timeout=args.smoke_timeout,
        self_backend_jobs=args.self_backend_jobs,
        max_tree_rss_bytes=args.max_tree_rss_bytes,
        frontend_jobs=args.frontend_jobs,
        gc_backend=gc_backend,
    )


def _persist(path: Path, payload: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _validate_limits(args: argparse.Namespace) -> None:
    if (
        args.stage2_timeout <= 0
        or args.smoke_timeout <= 0
        or args.identity_index <= 0
        or args.self_backend_jobs <= 0
        or args.self_backend_jobs > 2
        or args.max_tree_rss_bytes < 0
        or args.max_tree_rss_bytes > stage_ab.MAX_ALLOWED_TREE_RSS_BYTES
    ):
        raise Stage2ReceiptError(
            "jobs must be 1..2 and RSS cap must be positive and no greater "
            "than 16 GiB"
        )


def run(args: argparse.Namespace) -> dict[str, object]:
    _validate_limits(args)
    memory_selection = _select_stage2_memory_budget(args)
    args.max_tree_rss_bytes = memory_selection["max_tree_rss_bytes"]
    _validate_limits(args)
    ab = stage_ab._load_module(
        stage_ab.COMPILE_AB_TOOL,
        "pcc_stage2_receipt_compile",
    )
    stage1 = stage_ab._load_module(
        stage_ab.STAGE1_TOOL,
        "pcc_stage2_receipt_stage1",
    )
    sampler = stage_ab._load_module(
        stage_ab.PROCESS_SAMPLER,
        "pcc_stage2_receipt_sampler",
    )
    stage1_dir = Path(args.stage1_dir).expanduser().resolve(strict=True)
    output = Path(args.output_dir).expanduser().absolute()
    if output.exists():
        raise Stage2ReceiptError("refusing existing output directory: " + str(output))
    output.mkdir(parents=True)
    manifest_path = output / "manifest.json"
    manifest: dict[str, object] = {
        "schema": "pcc.stage2-from-stage1-receipt.v1",
        "status": "RUNNING",
        "started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "stage1_dir": str(stage1_dir),
        "memory_budget_selection": memory_selection,
        "stage2_timeout_s": args.stage2_timeout,
        "smoke_timeout_s": args.smoke_timeout,
    }
    _persist(manifest_path, manifest)
    try:
        record = _stage1_record(stage1_dir, ab=ab, stage1=stage1)
        if args.prediction_state:
            prediction = predict_stage2_seconds(Path(args.prediction_state))
            manifest["stage2_prediction"] = prediction
            _persist(manifest_path, manifest)
            _validate_stage2_prediction(prediction, args.stage2_timeout)
        preflight = _stage2_resource_preflight(args.max_tree_rss_bytes)
        manifest["resource_preflight"] = preflight
        _persist(manifest_path, manifest)
        runner_args = _runner_args(
            args,
            stage_ab._read_json(Path(record["receipt_path"])).get("environment") or {},
        )
        with ab._performance_lock():
            result = stage_ab._run_stage2(
                arm="candidate",
                pair_index=args.identity_index,
                stage1_record=record,
                arm_root=output,
                args=runner_args,
                ab=ab,
                sampler=sampler,
            )
        _persist(output / "stage2-record.json", result)
        manifest.update(
            {
                "status": "COMPLETE",
                "completed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "stage2_record": "stage2-record.json",
                "compiler_sha256": result["compiler_sha256"],
            }
        )
        _persist(manifest_path, manifest)
        return result
    except Exception as exc:
        manifest.update(
            {
                "status": "ERROR",
                "failed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "error": type(exc).__name__ + ": " + str(exc),
            }
        )
        _persist(manifest_path, manifest)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage1-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--stage2-timeout", type=int, default=600)
    parser.add_argument("--smoke-timeout", type=int, default=120)
    parser.add_argument("--self-backend-jobs", type=int, default=2)
    parser.add_argument(
        "--max-tree-rss-bytes",
        type=int,
        default=0,
        help="0 selects an observed safe budget up to 8 GiB; positive values are explicit caps",
    )
    parser.add_argument("--identity-index", type=int, default=1)
    # Envelope bookkeeping only (the compiled stage derives its own width from
    # the auto policy); defaults mirror run_pcc_stage_ab.py so the recorded
    # envelope of a from-receipt Stage2 matches a stage-ab Stage1.
    parser.add_argument("--frontend-jobs", type=int, default=2)
    parser.add_argument(
        "--gc-backend", type=int, choices=range(5), default=None,
        help="default: the Stage1 receipt's PCC_GC_BACKEND",
    )
    parser.add_argument(
        "--prediction-state",
        default="",
        help=(
            "retained prior Stage2 directory whose plan state predicts this "
            "run's wall; the run is refused when the prediction exceeds "
            "--stage2-timeout"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = run(args)
    except (OSError, ValueError, Stage2ReceiptError, stage_ab.StageABError) as exc:
        print("Stage2 receipt error: " + str(exc), file=sys.stderr)
        return 1
    print(
        "PCC_STAGE2_RECEIPT_COMPLETE compiler_sha256="
        + str(result["compiler_sha256"]),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
