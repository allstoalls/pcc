#!/usr/bin/env python3
"""Run adjacent, receipt-bound Stage1/Stage2 baseline pairs.

This harness compares two frozen compiler source lines under one host/runtime/
jobs/cache contract.  It reports timed-tree CPU as the primary compute metric;
wall remains an adjacent end-to-end observation.  It deliberately emits no
automatic source ACCEPT/DENY verdict.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import statistics
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
COMPILE_AB_TOOL = REPO_ROOT / "scripts" / "run_pcc_compile_ab.py"
STAGE1_TOOL = REPO_ROOT / "scripts" / "run_pcc_stage1_build.py"
PROCESS_SAMPLER = REPO_ROOT / "scripts" / "run_process_tree_sample.py"
DEFAULT_MAX_TREE_RSS_BYTES = 8 * 1024 * 1024 * 1024
MAX_ALLOWED_TREE_RSS_BYTES = 16 * 1024 * 1024 * 1024

STAGE2_METRIC_SCOPES = {
    "compile_wall_ms": "end_to_end_elapsed",
    "compile_time_real_ms": "end_to_end_elapsed",
    "compile_user_ms": "timed_command_plus_waited_children_cpu",
    "compile_sys_ms": "timed_command_plus_waited_children_cpu",
    "publish_barrier_ms": "end_to_end_elapsed",
    "wall_ms": "end_to_end_elapsed_including_publish_barrier",
    "peak_tree_rss_bytes": "sampled_process_tree_sum",
}

COMPARISON_CONTRACT = {
    "primary_compute_metric": "cpu_s",
    "wall_metric_role": "paired_end_to_end_observation",
    "required_order": "adjacent_alternating_pairs",
    "single_wall_verdict_allowed": False,
    "automatic_source_verdict": False,
}


class StageABError(RuntimeError):
    pass


def _validate_resource_limits(args: argparse.Namespace) -> None:
    if not 1 <= args.frontend_jobs <= 2:
        raise StageABError("Stage A/B frontend jobs must stay within 1..2")
    if not 1 <= args.self_backend_jobs <= 2:
        raise StageABError("Stage A/B self-backend jobs must stay within 1..2")
    if not 0 < args.max_tree_rss_bytes <= MAX_ALLOWED_TREE_RSS_BYTES:
        raise StageABError("Stage A/B RSS cap must be positive and <= 16 GiB")


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise StageABError("cannot load tool: " + str(path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pair_order(pair_index: int) -> tuple[str, str]:
    if pair_index < 1:
        raise ValueError("pair index must be positive")
    return ("baseline", "candidate") if pair_index % 2 else (
        "candidate",
        "baseline",
    )


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _persist(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StageABError("cannot read JSON receipt " + str(path)) from exc
    if not isinstance(value, dict):
        raise StageABError("JSON receipt is not an object: " + str(path))
    return value


def normalize_arm_environment(
    environment: dict[str, str],
    *,
    output_root: Path,
    source_root: Path,
) -> dict[str, str]:
    replacements = (
        (str(source_root.resolve()), "<ARM_SOURCE>"),
        (str(output_root.resolve()), "<ARM_OUTPUT>"),
    )
    normalized: dict[str, str] = {}
    for key, raw in sorted(environment.items()):
        value = str(raw)
        for prefix, marker in replacements:
            if value == prefix:
                value = marker
            elif value.startswith(prefix + os.sep):
                value = marker + value[len(prefix) :]
        normalized[str(key)] = value
    return normalized


def _stage_metrics(record: dict[str, Any], stage: str) -> dict[str, float]:
    if stage == "stage1":
        raw = record["result"]["metrics"]
        return {
            "wall_s": float(raw["wall_s"]),
            "cpu_s": float(raw["cpu_s"]),
            "peak_tree_rss_bytes": float(record["process"]["peak_tree_rss_bytes"]),
        }
    raw = record["result"]
    return {
        "wall_s": float(raw["wall_ms"]) / 1000.0,
        "compile_wall_s": float(raw["compile_wall_ms"]) / 1000.0,
        "cpu_s": (
            float(raw.get("compile_user_ms", 0))
            + float(raw.get("compile_sys_ms", 0))
        )
        / 1000.0,
        "peak_tree_rss_bytes": float(record["process"]["peak_tree_rss_bytes"]),
    }


def _process_failure_summary(process: dict[str, Any]) -> str:
    owner = process.get("largest_process_observed")
    if not isinstance(owner, dict):
        owner = {}
    manifests = owner.get("manifest_paths")
    if not isinstance(manifests, list):
        manifests = []
    return (
        "status="
        + str(process.get("status", "UNKNOWN"))
        + " returncode="
        + str(process.get("returncode", "UNKNOWN"))
        + " peak_tree_rss_bytes="
        + str(process.get("peak_tree_rss_bytes", 0))
        + " largest_pid="
        + str(owner.get("pid", 0))
        + " largest_rss_bytes="
        + str(owner.get("rss_bytes", 0))
        + " manifests="
        + ",".join(str(path) for path in manifests)
        + " command="
        + str(owner.get("command", ""))
    )


def _stage2_environment_overrides(
    *,
    pair_index: int,
    arm: str,
    self_backend_jobs: int,
    max_tree_rss_bytes: int,
) -> dict[str, str]:
    identity = "stage-ab-p" + str(pair_index) + "-" + arm
    return {
        "PCC_PY_FRONTEND_IR_CACHE": "0",
        "PCC_SELF_BACKEND_OBJECT_CACHE": "0",
        "PCC_PY_FRONTEND_IR_CACHE_IDENTITY": identity + "-frontend",
        "PCC_SELF_BACKEND_OBJECT_CACHE_IDENTITY": identity + "-object",
        # Native auto lanes serialize the measured high-risk source/AST family
        # before admitting the at-most-two-worker safe lane. A numeric value
        # of two skips that split and can run the largest compiler modules
        # together, which is not memory-equivalent.
        "PCC_BOOTSTRAP_PY_FRONTEND_JOBS": "auto",
        # The stage1 receipt records the HOST build's frontend width (which
        # may legitimately be wide, e.g. 10); replaying it verbatim into a
        # compiled stage trips bootstrap.py's fail-closed worker-budget guard.
        # The compiled stage derives its own width from the auto policy.
        "PCC_BOOTSTRAP_STAGE1_PY_FRONTEND_JOBS": "auto",
        "PCC_PY_FRONTEND_JOBS": "auto",
        "PCC_SELF_BACKEND_JOBS": str(self_backend_jobs),
        "PCC_MACHO_LINK_JOBS": "8",
        "PCC_BOOTSTRAP_EXTERNAL_MEMORY_GUARD": "1",
        # The native admission scheduler and the external watchdog must use
        # the same tree budget. Otherwise it can admit work the guard kills.
        "PCC_BOOTSTRAP_MAX_TREE_RSS_BYTES": str(max_tree_rss_bytes),
    }


# Fields that define "same external resource envelope".  Two stages compared
# for a Stage2/Stage1 ratio must match on ALL of these; peak_tree_rss and the
# admitted worker count are observations INSIDE the envelope, recorded but not
# required equal (the scheduler may admit fewer workers when live RSS is high).
_ENVELOPE_PARITY_KEYS = (
    "max_tree_rss_bytes",
    "cpu_count",
    "frontend_jobs",
    "self_backend_jobs",
    "macho_link_jobs",
    "gc_backend",
    "cache_policy",
)


def _cache_policy(environment: dict[str, str]) -> dict[str, str]:
    """The cold-cache knobs that must match across the two stages."""
    return {
        "PCC_PY_FRONTEND_IR_CACHE": str(environment.get("PCC_PY_FRONTEND_IR_CACHE", "")),
        "PCC_SELF_BACKEND_OBJECT_CACHE": str(
            environment.get("PCC_SELF_BACKEND_OBJECT_CACHE", "")
        ),
        "private_pycache": str(
            "PYTHONPYCACHEPREFIX" in environment
            and "PYTHONDONTWRITEBYTECODE" not in environment
        ),
    }


def _resource_envelope(
    *,
    args: argparse.Namespace,
    environment: dict[str, str],
    process: dict[str, Any],
) -> dict[str, Any]:
    """Bind the external resource envelope and the observations inside it.

    The envelope (cap/CPU/jobs/gc/cache) is what makes a Stage2/Stage1 ratio
    same-resource; ``observed_*`` records what actually happened so a huge
    worker or a memory trip is attributable without another run.
    """
    return {
        "max_tree_rss_bytes": int(args.max_tree_rss_bytes),
        "cpu_count": int(os.cpu_count() or 0),
        "frontend_jobs": int(args.frontend_jobs),
        "self_backend_jobs": int(args.self_backend_jobs),
        "macho_link_jobs": int(environment.get("PCC_MACHO_LINK_JOBS", "0")),
        "gc_backend": int(args.gc_backend),
        "cache_policy": _cache_policy(environment),
        "observed_peak_tree_rss_bytes": int(process.get("peak_tree_rss_bytes", 0)),
        "observed_peak_process_count": int(process.get("peak_process_count", 0)),
        "sampler_status": str(process.get("status", "UNKNOWN")),
        "sampler_returncode": process.get("returncode"),
        "termination_reason": str(process.get("termination_reason", process.get("status", "UNKNOWN"))),
    }


def envelope_parity_key(envelope: dict[str, Any]) -> tuple:
    """The subset of the envelope that two same-resource stages must share."""
    return tuple(_canonical_sha256(envelope.get(key)) for key in _ENVELOPE_PARITY_KEYS)


def assert_stage_envelope_parity(arm_record: dict[str, Any], *, arm: str) -> None:
    """A Stage2/Stage1 ratio may only come from one shared external envelope.

    Both stages already run under the same ``--max-tree-rss-bytes`` here, but
    the earlier operational pair (host Stage1 resolved against ~48 GiB, capped
    Stage2) mixed caps.  This makes the parity machine-checked and fails closed
    if any parity key drifts.
    """
    stage1_env = arm_record["stage1"].get("resource_envelope")
    stage2_record = arm_record.get("stage2")
    if stage2_record is None:
        return
    stage2_env = stage2_record.get("resource_envelope")
    if stage1_env is None or stage2_env is None:
        raise StageABError(arm + " stage record is missing its resource envelope")
    if envelope_parity_key(stage1_env) != envelope_parity_key(stage2_env):
        drift = [
            key
            for key in _ENVELOPE_PARITY_KEYS
            if stage1_env.get(key) != stage2_env.get(key)
        ]
        raise StageABError(
            arm
            + " Stage1/Stage2 resource envelopes differ on "
            + ",".join(drift)
            + "; a Stage2/Stage1 ratio must not mix resource envelopes"
        )


def summarize_pairs(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    if not pairs:
        raise StageABError("cannot summarize empty stage pairs")
    stages = ["stage1"]
    if all("stage2" in pair["baseline"] and "stage2" in pair["candidate"] for pair in pairs):
        stages.append("stage2")
    summary: dict[str, Any] = {
        "comparison_contract": COMPARISON_CONTRACT,
        "verdict": "MEASURED_NO_AUTOMATIC_ACCEPTANCE",
        "stages": {},
    }
    for stage in stages:
        by_arm: dict[str, list[dict[str, float]]] = {"baseline": [], "candidate": []}
        for pair in pairs:
            for arm in by_arm:
                by_arm[arm].append(_stage_metrics(pair[arm][stage], stage))
        metric_names = sorted(by_arm["baseline"][0])
        medians: dict[str, dict[str, float]] = {}
        for arm in by_arm:
            medians[arm] = {
                metric: float(
                    statistics.median(row[metric] for row in by_arm[arm])
                )
                for metric in metric_names
            }
        paired_ratios: dict[str, list[float]] = {}
        for metric in metric_names:
            paired_ratios[metric] = [
                _stage_metrics(pair["candidate"][stage], stage)[metric]
                / _stage_metrics(pair["baseline"][stage], stage)[metric]
                for pair in pairs
            ]
        summary["stages"][stage] = {
            "medians": medians,
            "candidate_over_baseline": {
                metric: medians["candidate"][metric] / medians["baseline"][metric]
                for metric in metric_names
            },
            "paired_candidate_over_baseline": paired_ratios,
            "paired_median_candidate_over_baseline": {
                metric: float(statistics.median(values))
                for metric, values in paired_ratios.items()
            },
        }
    return summary


def _sampler_args(
    sampler,
    *,
    prefix: Path,
    cwd: Path,
    timeout: int,
    max_tree_rss_bytes: int,
    command: list[str],
):
    return sampler._parser().parse_args(
        [
            "--result",
            str(prefix.with_suffix(".result.json")),
            "--samples",
            str(prefix.with_suffix(".samples.tsv")),
            "--stdout",
            str(prefix.with_suffix(".stdout")),
            "--stderr",
            str(prefix.with_suffix(".stderr")),
            "--cwd",
            str(cwd),
            "--timeout",
            str(timeout),
            "--interval",
            "0.25",
            "--progress-interval",
            "30",
            "--max-tree-rss-bytes",
            str(max_tree_rss_bytes),
            "--no-performance-lock",
            "--",
            *command,
        ]
    )


def _run_stage1(
    *,
    arm: str,
    source_root: Path,
    runtime_archive: Path,
    arm_root: Path,
    args: argparse.Namespace,
    ab,
    stage1,
    sampler,
) -> dict[str, Any]:
    output = arm_root / "stage1"
    command = [
        sys.executable,
        str(STAGE1_TOOL),
        "--arm",
        arm,
        "--source-root",
        str(source_root),
        "--runtime-archive",
        str(runtime_archive),
        "--output-dir",
        str(output),
        "--timeout",
        str(args.stage1_timeout),
        "--smoke-timeout",
        str(args.smoke_timeout),
        "--jobs",
        str(args.frontend_jobs),
        "--self-backend-jobs",
        str(args.self_backend_jobs),
        "--gc-backend",
        str(args.gc_backend),
        "--with-threads",
        "0",
        "--direct-indexed-emit",
        "--no-performance-lock",
    ]
    process = sampler.run(
        _sampler_args(
            sampler,
            prefix=arm_root / "stage1-process",
            cwd=REPO_ROOT,
            timeout=args.stage1_timeout + args.smoke_timeout * 3 + 30,
            max_tree_rss_bytes=args.max_tree_rss_bytes,
            command=command,
        )
    )
    if process.get("status") != "COMPLETE" or process.get("returncode") != 0:
        raise StageABError(arm + " Stage1 did not complete")
    result = _read_json(output / "stage1-result.json")
    receipt = _read_json(output / "build-receipt.json")
    if result.get("metric_scopes") != stage1.STAGE1_METRIC_SCOPES:
        raise StageABError(arm + " Stage1 metric scope mismatch")
    if result.get("comparison_contract") != stage1.STAGE1_COMPARISON_CONTRACT:
        raise StageABError(arm + " Stage1 comparison contract mismatch")
    pycache_root = Path(receipt["environment"]["PYTHONPYCACHEPREFIX"])
    pycache_files = sum(1 for path in pycache_root.rglob("*.pyc") if path.is_file())
    if pycache_files <= 0 or "PYTHONDONTWRITEBYTECODE" in receipt["environment"]:
        raise StageABError(arm + " Stage1 private pycache contract failed")
    source_snapshot = output / str(receipt["source_snapshot"])
    normalized_environment = normalize_arm_environment(
        {str(key): str(value) for key, value in receipt["environment"].items()},
        output_root=output,
        source_root=source_snapshot,
    )
    return {
        "result_path": str(output / "stage1-result.json"),
        "receipt_path": str(output / "build-receipt.json"),
        "result": result,
        "process": process,
        "pycache_files": pycache_files,
        "normalized_environment": normalized_environment,
        "normalized_environment_sha256": _canonical_sha256(normalized_environment),
        "source_snapshot": str(source_snapshot),
        "runtime_archive": str(output / "runtime-bundle" / "libpy_runtime_pcc_py.a"),
        "compiler": str(output / "pcc1"),
        "resource_envelope": _resource_envelope(
            args=args,
            environment={
                str(key): str(value)
                for key, value in receipt["environment"].items()
            },
            process=process,
        ),
    }


def _run_stage2(
    *,
    arm: str,
    pair_index: int,
    stage1_record: dict[str, Any],
    arm_root: Path,
    args: argparse.Namespace,
    ab,
    sampler,
) -> dict[str, Any]:
    output = arm_root / "stage2"
    output.mkdir()
    compiler = output / "pcc1"
    shutil.copy2(stage1_record["compiler"], compiler)
    compiler.chmod(0o755)
    source_root = Path(stage1_record["source_snapshot"])
    receipt = _read_json(Path(stage1_record["receipt_path"]))
    environment = {str(key): str(value) for key, value in receipt["environment"].items()}
    profile = output / "profile"
    environment.update({"PCC_BOOTSTRAP_PROFILE_DIR": str(profile)})
    environment.update(
        _stage2_environment_overrides(
            pair_index=pair_index,
            arm=arm,
            self_backend_jobs=args.self_backend_jobs,
            max_tree_rss_bytes=args.max_tree_rss_bytes,
        )
    )
    command = [
        sys.executable,
        str(source_root / "scripts" / "bootstrap.py"),
        "--out-dir",
        str(output),
        "--backend",
        "self",
        "--from-stage",
        "2",
        "--stage",
        "2",
        "--reuse-stage1",
    ]
    original_environment = os.environ.copy()
    os.environ.clear()
    os.environ.update(environment)
    try:
        process = sampler.run(
            _sampler_args(
                sampler,
                prefix=arm_root / "stage2-process",
                cwd=source_root,
                timeout=args.stage2_timeout,
                max_tree_rss_bytes=args.max_tree_rss_bytes,
                command=command,
            )
        )
    finally:
        os.environ.clear()
        os.environ.update(original_environment)
    if process.get("status") != "COMPLETE" or process.get("returncode") != 0:
        raise StageABError(
            arm + " Stage2 did not complete: " + _process_failure_summary(process)
        )
    result = _read_json(profile / "stage2.result.json")
    pcc2 = output / "pcc2"
    if result.get("returncode") != 0 or not pcc2.is_file() or not os.access(pcc2, os.X_OK):
        raise StageABError(arm + " Stage2 produced no executable pcc2")
    help_result = ab._run_process(
        [str(pcc2), "--help"],
        timeout=args.smoke_timeout,
        env=environment,
        cwd=output,
    )
    if help_result.returncode != 0:
        raise StageABError(arm + " Stage2 pcc2 --help failed")
    linkage = ab._linkage(
        pcc2,
        timeout=args.smoke_timeout,
        env=environment,
        cwd=output,
    )
    return {
        "result_path": str(profile / "stage2.result.json"),
        "result": result,
        "metric_scopes": STAGE2_METRIC_SCOPES,
        "comparison_contract": COMPARISON_CONTRACT,
        "process": process,
        "compiler": str(pcc2),
        "compiler_sha256": ab.sha256_path(pcc2),
        "linkage": linkage,
        "resource_envelope": _resource_envelope(
            args=args,
            environment=environment,
            process=process,
        ),
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    _validate_resource_limits(args)
    ab = _load_module(COMPILE_AB_TOOL, "pcc_stage_ab_compile")
    stage1 = _load_module(STAGE1_TOOL, "pcc_stage_ab_stage1")
    sampler = _load_module(PROCESS_SAMPLER, "pcc_stage_ab_sampler")
    baseline_source = Path(args.baseline_source).expanduser().resolve(strict=True)
    candidate_source = Path(args.candidate_source).expanduser().resolve(strict=True)
    runtime_archive = Path(args.runtime_archive).expanduser().resolve(strict=True)
    stage1._validate_source_root(str(baseline_source), ab)
    stage1._validate_source_root(str(candidate_source), ab)
    stage1._require_immutable_source(baseline_source, ab)
    stage1._require_immutable_source(candidate_source, ab)
    source_manifests = {
        "baseline": stage1.source_manifest(baseline_source, ab),
        "candidate": stage1.source_manifest(candidate_source, ab),
    }
    output = Path(args.output_dir).expanduser().absolute()
    if output.exists():
        raise StageABError("refusing existing output directory: " + str(output))
    output.mkdir(parents=True)
    manifest_path = output / "manifest.json"
    manifest: dict[str, Any] = {
        "schema": "pcc.bootstrap-stage-ab.v1",
        "status": "RUNNING",
        "started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "comparison_contract": COMPARISON_CONTRACT,
        "metric_scopes": {
            "stage1": stage1.STAGE1_METRIC_SCOPES,
            "stage2": STAGE2_METRIC_SCOPES,
        },
        "sources": source_manifests,
        "runtime_archive": str(runtime_archive),
        "runtime_archive_sha256": ab.sha256_path(runtime_archive),
        "pairs": [],
    }
    _persist(manifest_path, manifest)
    source_by_arm = {"baseline": baseline_source, "candidate": candidate_source}
    starting_runtime_hash = manifest["runtime_archive_sha256"]
    with ab._performance_lock():
        first_pair_index = args.first_pair_index
        for pair_index in range(first_pair_index, first_pair_index + args.pairs):
            pair_root = output / ("pair-" + str(pair_index))
            pair_root.mkdir()
            pair: dict[str, Any] = {"index": pair_index, "order": pair_order(pair_index)}
            manifest["active_pair"] = pair
            _persist(manifest_path, manifest)
            for arm in pair["order"]:
                arm_root = pair_root / arm
                arm_root.mkdir()
                print(
                    "[stage-ab] pair=" + str(pair_index) + " arm=" + arm + " stage1 begin",
                    flush=True,
                )
                stage1_record = _run_stage1(
                    arm=arm,
                    source_root=source_by_arm[arm],
                    runtime_archive=runtime_archive,
                    arm_root=arm_root,
                    args=args,
                    ab=ab,
                    stage1=stage1,
                    sampler=sampler,
                )
                arm_record: dict[str, Any] = {"stage1": stage1_record}
                if args.stages == "stage1-stage2":
                    print(
                        "[stage-ab] pair=" + str(pair_index) + " arm=" + arm + " stage2 begin",
                        flush=True,
                    )
                    arm_record["stage2"] = _run_stage2(
                        arm=arm,
                        pair_index=pair_index,
                        stage1_record=stage1_record,
                        arm_root=arm_root,
                        args=args,
                        ab=ab,
                        sampler=sampler,
                    )
                assert_stage_envelope_parity(arm_record, arm=arm)
                pair[arm] = arm_record
                manifest["active_pair"] = pair
                _persist(manifest_path, manifest)
            manifest["pairs"].append(pair)
            manifest.pop("active_pair", None)
            _persist(manifest_path, manifest)
            if (
                pair["baseline"]["stage1"]["normalized_environment_sha256"]
                != pair["candidate"]["stage1"]["normalized_environment_sha256"]
            ):
                raise StageABError("paired Stage1 environments differ after normalization")
        if ab.sha256_path(runtime_archive) != starting_runtime_hash:
            raise StageABError("runtime archive changed during stage A/B")
        ending_sources = {
            arm: stage1.source_manifest(source_by_arm[arm], ab)
            for arm in source_by_arm
        }
        if ending_sources != source_manifests:
            raise StageABError("source snapshot changed during stage A/B")
    manifest["summary"] = summarize_pairs(manifest["pairs"])
    manifest["status"] = "COMPLETE"
    manifest["completed_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    _persist(manifest_path, manifest)
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-source", required=True)
    parser.add_argument("--candidate-source", required=True)
    parser.add_argument("--runtime-archive", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--pairs", type=int, default=2)
    parser.add_argument("--first-pair-index", type=int, default=1)
    parser.add_argument(
        "--stages",
        choices=("stage1", "stage1-stage2"),
        default="stage1-stage2",
    )
    parser.add_argument("--stage1-timeout", type=int, default=360)
    parser.add_argument("--stage2-timeout", type=int, default=540)
    parser.add_argument("--smoke-timeout", type=int, default=60)
    parser.add_argument("--frontend-jobs", type=int, default=2)
    parser.add_argument("--self-backend-jobs", type=int, default=2)
    parser.add_argument(
        "--max-tree-rss-bytes",
        type=int,
        default=DEFAULT_MAX_TREE_RSS_BYTES,
    )
    parser.add_argument("--gc-backend", type=int, choices=range(5), default=0)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.pairs <= 0 or args.first_pair_index <= 0:
        print(
            "stage A/B error: --pairs and --first-pair-index must be positive",
            file=sys.stderr,
        )
        return 2
    try:
        manifest = run(args)
    except (OSError, ValueError, KeyError, StageABError, RuntimeError) as exc:
        # RuntimeError covers the dynamically-loaded tools' CompileABError /
        # Stage1BuildError subclasses, which run() re-raises verbatim; KeyError
        # covers a stage1 receipt missing a required environment key.  Both
        # previously escaped as raw tracebacks with the manifest left RUNNING.
        print("stage A/B error: " + str(exc), file=sys.stderr)
        return 1
    print(
        "[stage-ab] complete pairs=" + str(len(manifest["pairs"])),
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
