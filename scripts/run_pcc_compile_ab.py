#!/usr/bin/env python3
"""Run a receipt-bound, alternating pcc1 compile A/B experiment.

This harness is intentionally narrow: two already-built compiler binaries,
one frozen runtime archive, one warmup input, and at least three matched
measurement inputs.  It records the command/environment identities, parses
macOS ``/usr/bin/time -lp`` counters, validates every pair byte-for-byte, and
writes an incremental JSON manifest so an interrupted run cannot look green.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import difflib
import fcntl
import hashlib
import importlib.util
import json
import math
import os
import platform
import re
import signal
import shutil
import subprocess
import sys
import sysconfig
import time
from pathlib import Path
from pathlib import PurePosixPath
from statistics import median
from typing import Any


SCHEMA_VERSION = 2
RUN_OWNER_FILE = ".pcc-run-owner.json"
REPO_ROOT = Path(__file__).resolve().parents[1]
TIME_BINARY = Path("/usr/bin/time")
OTOOL_BINARY = Path("/usr/bin/otool")
METRIC_PATTERNS = {
    "wall_s": r"(?m)^real\s+([0-9.]+)\s*$",
    "user_s": r"(?m)^user\s+([0-9.]+)\s*$",
    "system_s": r"(?m)^sys\s+([0-9.]+)\s*$",
    "max_rss_bytes": r"(?m)^\s*([0-9]+)\s+maximum resident set size\s*$",
    "instructions": r"(?m)^\s*([0-9]+)\s+instructions retired\s*$",
    "cycles": r"(?m)^\s*([0-9]+)\s+cycles elapsed\s*$",
    "peak_footprint_bytes": r"(?m)^\s*([0-9]+)\s+peak memory footprint\s*$",
}
FLOAT_METRICS = {"wall_s", "user_s", "system_s"}
COMPUTE_METRICS = ("cpu_s", "instructions", "cycles")
MEMORY_METRICS = ("max_rss_bytes", "peak_footprint_bytes")
RESOURCE_METRICS = COMPUTE_METRICS + MEMORY_METRICS
RUNTIME_SIDECAR_SUFFIXES = (
    ".provenance.json",
    ".capi_syms",
    ".target",
)
BUILD_RECEIPT_FIELDS = frozenset(
    {
        "schema",
        "arm",
        "status",
        "compiler_sha256",
        "compiler_size_bytes",
        "runtime_archive_sha256",
        "bootstrap_source_sha256",
        "primary_source_sha256",
        "origin_source_root",
        "logical_source_root",
        "source_manifest",
        "source_manifest_sha256",
        "source_snapshot",
        "command",
        "command_sha256",
        "environment",
        "environment_sha256",
        "cwd",
        "stage_result",
        "stage_result_sha256",
        "runtime_bundle",
        "runtime_bundle_sha256",
        "external_tools",
        "external_tools_sha256",
        "producer_tools",
        "producer_tools_sha256",
        "host_python_runtime",
        "host_python_runtime_sha256",
    }
)
BUILD_RECEIPT_SCHEMA = "pcc.stage1-build-receipt.v3"
STAGE_RESULT_FIELDS = frozenset(
    {
        "schema",
        "returncode",
        "compiler",
        "compiler_sha256",
        "compiler_size_bytes",
        "metrics",
        "profile_sha256",
        "stdout_sha256",
        "stderr_sha256",
        "linkage",
        "artifacts",
    }
)
STAGE_RESULT_SCHEMA = "pcc.stage1-build-result.v1"
STAGE_RESULT_METRIC_FIELDS = frozenset({"metric_scopes", "comparison_contract"})
# These admitted v1 fields describe measurements, never compiler semantics.
# Keep the producer contract exact; the focused test supplies the current
# stage1 producer's constants so producer/consumer drift fails immediately.
STAGE_RESULT_METRIC_SCOPES = {
    "wall_s": "end_to_end_elapsed",
    "user_s": "timed_command_plus_waited_children_cpu",
    "system_s": "timed_command_plus_waited_children_cpu",
    "cpu_s": "timed_command_plus_waited_children_cpu_sum",
    "instructions": "coordinator_only_diagnostic",
    "cycles": "coordinator_only_diagnostic",
    "max_rss_bytes": "nonadditive_process_max_not_tree_sum",
    "peak_footprint_bytes": "coordinator_only_not_tree_sum",
}
STAGE_RESULT_COMPARISON_CONTRACT = {
    "primary_compute_metric": "cpu_s",
    "wall_metric_role": "paired_end_to_end_observation",
    "required_comparison": "adjacent_alternating_same_environment_pairs",
    "single_wall_verdict_allowed": False,
    "hardware_counters_allowed_for_stage_verdict": False,
}
SOURCE_MANIFEST_SCHEMA = "pcc.bootstrap-source-manifest.v1"
PRIMARY_SOURCE = "pcc/llvm_capi/ir.py"
BUILD_SOURCE_SUPPORT = (
    "AGENTS.md",
    "pyproject.toml",
    "scripts/bootstrap.sh",
    "scripts/run_pcc_deferred_link.py",
    "scripts/pcc_link_macho.py",
    "scripts/pcc_link_elf.py",
)
_ACTIVE_PROCESS: subprocess.Popen[str] | None = None


class CompileABError(RuntimeError):
    pass


def require_claim_platform() -> None:
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise CompileABError(
            "claim-grade pcc performance tools currently require Darwin arm64"
        )


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_sha256(value: object) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def build_source_files(source_root: Path) -> list[Path]:
    """Return the complete source/tool closure read by a stage1 build."""
    root = source_root.resolve(strict=True)
    skipped_dirs = {"__pycache__", ".pytest_cache", "_native", "build", "build_py"}
    skipped_suffixes = {".a", ".dylib", ".ll", ".o", ".pyc", ".so", ".tmp"}
    files: list[Path] = []
    pcc_root = root / "pcc"
    fake_libc_root = root / "utils" / "fake_libc_include"
    if pcc_root.is_symlink() or fake_libc_root.is_symlink():
        raise CompileABError("stage source closure must not contain symlinks")
    if not pcc_root.is_dir():
        raise CompileABError(f"stage source has no pcc package: {root}")
    if not fake_libc_root.is_dir():
        raise CompileABError(f"stage source has no fake-libc headers: {root}")
    for closure_root in (pcc_root, fake_libc_root):
        for path in closure_root.rglob("*"):
            relative = path.relative_to(root)
            if any(part in skipped_dirs for part in relative.parts):
                continue
            if path.is_symlink():
                raise CompileABError(
                    "stage source closure must not contain symlinks: " + str(path)
                )
            if path.is_file() and path.suffix not in skipped_suffixes:
                files.append(path)
    for name in BUILD_SOURCE_SUPPORT:
        path = root / name
        if path.is_symlink():
            raise CompileABError(
                "stage source closure must not contain symlinks: " + str(path)
            )
        if not path.is_file():
            raise CompileABError(f"stage source is missing required support file: {name}")
        files.append(path)
    return sorted(set(files), key=lambda path: path.relative_to(root).as_posix())


def parse_time_output(text: str) -> dict[str, float | int]:
    metrics: dict[str, float | int] = {}
    for name, pattern in METRIC_PATTERNS.items():
        matches = re.findall(pattern, text)
        if len(matches) != 1:
            raise CompileABError(
                f"/usr/bin/time output expected one {name}, found {len(matches)}"
            )
        value = matches[0]
        metrics[name] = float(value) if name in FLOAT_METRICS else int(value)
    metrics["cpu_s"] = float(metrics["user_s"]) + float(metrics["system_s"])
    return metrics


def pair_order(pair_index: int) -> tuple[str, str]:
    if pair_index < 1:
        raise ValueError("pair index must be positive")
    return ("candidate", "baseline") if pair_index % 2 else ("baseline", "candidate")


def _validate_thresholds(
    min_speedup_ratio: float,
    max_compute_regression_ratio: float,
    max_memory_regression_ratio: float,
) -> None:
    values = (
        ("min speedup", min_speedup_ratio),
        ("max compute regression", max_compute_regression_ratio),
        ("max memory regression", max_memory_regression_ratio),
    )
    for label, value in values:
        if not math.isfinite(value):
            raise CompileABError(f"{label} ratio must be finite")
    if min_speedup_ratio <= 1.0:
        raise CompileABError("--min-speedup-ratio must be greater than 1.0")
    if max_compute_regression_ratio < 1.0:
        raise CompileABError("--max-compute-regression-ratio must be at least 1.0")
    if max_memory_regression_ratio < 1.0:
        raise CompileABError("--max-memory-regression-ratio must be at least 1.0")


def summarize_pairs(
    pairs: list[dict[str, Any]],
    *,
    min_speedup_ratio: float,
    max_compute_regression_ratio: float,
    max_memory_regression_ratio: float,
) -> dict[str, Any]:
    _validate_thresholds(
        min_speedup_ratio,
        max_compute_regression_ratio,
        max_memory_regression_ratio,
    )
    if not pairs:
        raise CompileABError("cannot summarize an empty A/B run")
    arms: dict[str, dict[str, float]] = {}
    for arm in ("candidate", "baseline"):
        arm_metrics = [pair[arm]["metrics"] for pair in pairs]
        arms[arm] = {
            metric: float(median(row[metric] for row in arm_metrics))
            for metric in ("wall_s",) + RESOURCE_METRICS
        }
    paired_speedups = [
        float(pair["baseline"]["metrics"]["wall_s"])
        / float(pair["candidate"]["metrics"]["wall_s"])
        for pair in pairs
    ]
    paired_median_speedup = float(median(paired_speedups))
    paired_min_speedup = float(min(paired_speedups))
    paired_max_speedup = float(max(paired_speedups))
    median_wall_speedup = (
        arms["baseline"]["wall_s"] / arms["candidate"]["wall_s"]
    )
    paired_resource_ratios: dict[str, list[float]] = {}
    paired_median_resource_ratios: dict[str, float] = {}
    paired_max_resource_ratios: dict[str, float] = {}
    regressions: list[str] = []
    for metric in RESOURCE_METRICS:
        baseline = arms["baseline"][metric]
        candidate = arms["candidate"][metric]
        median_ratio = candidate / baseline if baseline else float("inf")
        pair_ratios = [
            (
                float(pair["candidate"]["metrics"][metric])
                / float(pair["baseline"]["metrics"][metric])
                if pair["baseline"]["metrics"][metric]
                else float("inf")
            )
            for pair in pairs
        ]
        paired_resource_ratios[metric] = pair_ratios
        paired_ratio = float(median(pair_ratios))
        paired_median_resource_ratios[metric] = paired_ratio
        paired_max_ratio = float(max(pair_ratios))
        paired_max_resource_ratios[metric] = paired_max_ratio
        limit = (
            max_compute_regression_ratio
            if metric in COMPUTE_METRICS
            else max_memory_regression_ratio
        )
        if median_ratio > limit or paired_ratio > limit or paired_max_ratio > limit:
            regressions.append(
                f"{metric}=median:{median_ratio:.6f}x/"
                f"paired:{paired_ratio:.6f}x/max:{paired_max_ratio:.6f}x"
            )
    accepted = (
        median_wall_speedup >= min_speedup_ratio
        and paired_median_speedup >= min_speedup_ratio
        and paired_min_speedup >= 1.0
        and not regressions
    )
    return {
        "candidate_medians": arms["candidate"],
        "baseline_medians": arms["baseline"],
        "paired_wall_speedups": paired_speedups,
        "paired_median_wall_speedup": paired_median_speedup,
        "paired_wall_speedup_range": [paired_min_speedup, paired_max_speedup],
        "median_wall_speedup": median_wall_speedup,
        "paired_resource_ratios": paired_resource_ratios,
        "paired_median_resource_ratios": paired_median_resource_ratios,
        "paired_max_resource_ratios": paired_max_resource_ratios,
        "thresholds": {
            "min_median_wall_speedup": min_speedup_ratio,
            "min_paired_median_wall_speedup": min_speedup_ratio,
            "min_each_pair_wall_speedup": 1.0,
            "max_candidate_compute_ratio": max_compute_regression_ratio,
            "max_candidate_memory_ratio": max_memory_regression_ratio,
            "max_each_pair_compute_ratio": max_compute_regression_ratio,
            "max_each_pair_memory_ratio": max_memory_regression_ratio,
        },
        "resource_regressions": regressions,
        "verdict": "ACCEPT" if accepted else "DENY",
    }


def _run_process(
    command: list[str],
    *,
    timeout: int,
    env: dict[str, str],
    cwd: Path = REPO_ROOT,
) -> subprocess.CompletedProcess[str]:
    global _ACTIVE_PROCESS
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    _ACTIVE_PROCESS = process
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_group(process)
        raise CompileABError(
            f"command timed out after {timeout}s: {' '.join(command)}"
        ) from exc
    except BaseException:
        _terminate_process_group(process)
        raise
    finally:
        _ACTIVE_PROCESS = None
    if _process_group_exists(process):
        _terminate_process_group(process)
        raise CompileABError(
            "command returned while a child process remained in its process group: "
            + " ".join(command)
        )
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _signal_process_group(process: subprocess.Popen[str], signum: int) -> None:
    try:
        os.killpg(process.pid, signum)
    except (ProcessLookupError, PermissionError):
        # Darwin may report EPERM once only reaped/non-signalable members
        # remain in an orphaned group.  No live same-uid child can be acted on.
        pass


def _process_group_exists(process: subprocess.Popen[str]) -> bool:
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # A group containing only non-signalable/reaped members is not an
        # active child group we can own.  This occurs transiently on Darwin
        # after TERM has reaped the final orphan.
        return False
    return True


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    # The /usr/bin/time leader may exit before a compiler worker that inherited
    # its stdout/stderr pipes. Waiting only for the leader can therefore hang
    # forever in communicate() and leak that worker into the next A/B arm.
    _signal_process_group(process, signal.SIGTERM)
    try:
        process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    if _process_group_exists(process):
        _signal_process_group(process, signal.SIGKILL)
    try:
        process.communicate(timeout=2)
    except subprocess.TimeoutExpired as exc:
        raise CompileABError("timed-out process group survived SIGKILL") from exc


def _interrupt_handler(_signum: int, _frame: Any) -> None:
    if _ACTIVE_PROCESS is not None:
        _terminate_process_group(_ACTIVE_PROCESS)
    raise KeyboardInterrupt


def _measurement_env(
    runtime_archive: Path,
    gc_backend: int,
    *,
    host_source_root: Path,
    host_python: Path,
    private_root: Path,
    frontend_jobs: int,
    self_backend_jobs: int,
) -> dict[str, str]:
    if frontend_jobs <= 0 or self_backend_jobs <= 0:
        raise CompileABError("measurement worker counts must be positive")
    path = os.environ.get("PATH", "").strip()
    if not path:
        raise CompileABError("PATH is required and must be recorded")
    path_parts = path.split(os.pathsep)
    if any(not part or not Path(part).expanduser().is_absolute() for part in path_parts):
        raise CompileABError("PATH entries must be non-empty absolute directories")
    path = os.pathsep.join(
        str(Path(part).expanduser().resolve(strict=False)) for part in path_parts
    )
    private_home = private_root / "home"
    private_tmp = private_root / "tmp"
    private_cache = private_root / "cache"
    private_pycache = private_root / "pycache"
    for directory in (private_home, private_tmp, private_cache, private_pycache):
        directory.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": path,
        "HOME": str(private_home),
        "TMPDIR": str(private_tmp),
        "XDG_CACHE_HOME": str(private_cache),
        "LANG": "en_US.UTF-8",
        "PYTHONHASHSEED": "0",
        "PYTHONPYCACHEPREFIX": str(private_pycache),
    }
    for key in (
        "DEVELOPER_DIR",
        "SDKROOT",
        "MACOSX_DEPLOYMENT_TARGET",
        "PCC_DEBUG_SELF_IR_DUMP_DIR",
    ):
        value = os.environ.get(key)
        if value:
            env[key] = value
    env.update(
        {
            "PCC_GC_BACKEND": str(gc_backend),
            "PCC_RUNTIME_CC": "/usr/bin/false",
            "PCC_RUNTIME_HIGH": "py",
            "PCC_SELF_LINK": "pcc",
            "PCC_SELF_BACKEND_PUBLISH_SYNC": "1",
            "PCC_PYTHON_IR_PASSES": "off",
            "PCC_PYTHON_IR_PASS_JOBS": "1",
            "PCC_PY_FRONTEND_IR_CACHE": "0",
            "PCC_SELF_BACKEND_OBJECT_CACHE": "0",
            "PCC_PY_FRONTEND_JOBS": str(frontend_jobs),
            "PCC_SELF_BACKEND_JOBS": str(self_backend_jobs),
            "PCC_MACHO_LINK_JOBS": "8",
            "PCC_DEBUG_IR_CALL": "0",
            "PCC_DEBUG_IR_RENDER": "0",
            "PCC_RUNTIME_ARCHIVE": str(runtime_archive),
            "PCC_SOURCE_ROOT": str(host_source_root),
            "PCC_REPO_ROOT": str(host_source_root),
            "PCC_HOST_PYTHON": str(host_python),
        }
    )
    return env


def _normalized_measurement_env(
    env: dict[str, str],
    *,
    host_source_root: Path,
    private_root: Path,
) -> dict[str, str]:
    replacements = (
        (str(host_source_root.resolve()), "<ARM_SOURCE>"),
        (str(private_root.resolve()), "<ARM_PRIVATE>"),
    )
    normalized: dict[str, str] = {}
    for key, value in sorted(env.items()):
        updated = value
        for prefix, marker in replacements:
            if updated == prefix:
                updated = marker
            elif updated.startswith(prefix + os.sep):
                updated = marker + updated[len(prefix) :]
        normalized[key] = updated
    return normalized


def _compile_one(
    compiler: Path,
    source: Path,
    output: Path,
    *,
    timeout: int,
    env: dict[str, str],
    log_prefix: Path,
    cwd: Path,
) -> dict[str, Any]:
    time_path = log_prefix.with_suffix(".time")
    command = [
        str(TIME_BINARY),
        "-lp",
        "-o",
        str(time_path),
        str(compiler),
        "--ir-scaffold=on",
        "--backend",
        "self",
        "--python-libpython",
        "off",
        str(source),
        "-o",
        str(output),
    ]
    started = dt.datetime.now(dt.timezone.utc)
    result = _run_process(command, timeout=timeout, env=env, cwd=cwd)
    log_prefix.with_suffix(".stdout").write_text(result.stdout, encoding="utf-8")
    log_prefix.with_suffix(".stderr").write_text(result.stderr, encoding="utf-8")
    if result.returncode != 0:
        raise CompileABError(
            f"compile failed rc={result.returncode}: {compiler.name} {source.name}\n"
            f"{result.stderr[-4000:]}"
        )
    if not output.is_file():
        raise CompileABError(f"compiler produced no output: {output}")
    if not time_path.is_file():
        raise CompileABError(f"/usr/bin/time produced no metrics: {time_path}")
    time_text = time_path.read_text(encoding="utf-8")
    return {
        "command": command,
        "started_at_utc": started.isoformat(),
        "completed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "returncode": result.returncode,
        "metrics": parse_time_output(time_text),
        "compiler_stdout_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
        "compiler_stderr_sha256": hashlib.sha256(result.stderr.encode()).hexdigest(),
        "time_output_sha256": hashlib.sha256(time_text.encode()).hexdigest(),
        "binary_sha256": sha256_path(output),
        "binary_size_bytes": output.stat().st_size,
        "binary_path": str(output),
    }


def _run_output(
    binary: Path,
    *,
    timeout: int,
    env: dict[str, str],
    cwd: Path,
) -> dict[str, Any]:
    result = _run_process([str(binary)], timeout=timeout, env=env, cwd=cwd)
    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "stdout_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
        "stderr_sha256": hashlib.sha256(result.stderr.encode()).hexdigest(),
    }


def _linkage(
    binary: Path,
    *,
    timeout: int,
    env: dict[str, str],
    cwd: Path,
) -> dict[str, Any]:
    if not OTOOL_BINARY.exists():
        raise CompileABError("/usr/bin/otool is required for no-libpython proof")
    result = _run_process(
        [str(OTOOL_BINARY), "-L", str(binary)],
        timeout=timeout,
        env=env,
        cwd=cwd,
    )
    if result.returncode != 0:
        raise CompileABError(f"otool failed for {binary}: {result.stderr[-2000:]}")
    lowered = result.stdout.lower()
    links_libpython = "libpython" in lowered or "python.framework" in lowered
    links_llvm = "libllvm" in lowered or "llvmlite" in lowered
    if links_libpython or links_llvm:
        owner = "libpython" if links_libpython else "LLVM"
        raise CompileABError(f"strict output links forbidden {owner}: {binary}")
    return {
        "checked": True,
        "links_libpython": False,
        "links_llvm": False,
        "stdout": result.stdout,
    }


def _persist(path: Path, manifest: dict[str, Any]) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)


def _claim_output_directory(output_dir: Path, *, harness: str, run_token: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=False)
    _persist(
        output_dir / RUN_OWNER_FILE,
        {
            "schema": "pcc.performance-run-owner.v1",
            "harness": harness,
            "pid": os.getpid(),
            "run_token": run_token,
            "started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        },
    )


def _mark_owned_run_failure(
    output_dir: Path,
    *,
    run_token: str,
    status: str,
    error: str,
) -> None:
    owner_path = output_dir / RUN_OWNER_FILE
    try:
        owner = json.loads(owner_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(owner, dict) or owner.get("run_token") != run_token:
        return
    manifest_path = output_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "harness": owner.get("harness"),
            "started_at_utc": owner.get("started_at_utc"),
        }
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(manifest, dict):
        return
    manifest.update(
        {
            "status": status,
            "error": error,
            "failed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "run_token": run_token,
        }
    )
    _persist(manifest_path, manifest)


def _frozen_hashes(candidate: Path, baseline: Path, runtime_archive: Path) -> dict[str, str]:
    return {
        "candidate_pcc1_sha256": sha256_path(candidate),
        "baseline_pcc1_sha256": sha256_path(baseline),
        "runtime_archive_sha256": sha256_path(runtime_archive),
    }


def _path_receipt(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path),
        "sha256": sha256_path(path),
        "device": stat.st_dev,
        "inode": stat.st_ino,
        "mode": stat.st_mode,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _verify_receipt(receipt: dict[str, Any], label: str) -> None:
    path = Path(receipt["path"])
    if not path.is_file() or _path_receipt(path) != receipt:
        raise CompileABError(f"{label} changed during A/B: {path}")


def _runtime_source_path(
    logical: object,
    *,
    source_root: Path | None = None,
) -> tuple[Path, Path]:
    if not isinstance(logical, str) or not logical:
        raise CompileABError("runtime manifest member has no source")
    pure = PurePosixPath(logical)
    prefix = PurePosixPath("pcc/py_runtime")
    try:
        relative = pure.relative_to(prefix)
    except ValueError as exc:
        raise CompileABError(f"runtime source is outside {prefix}: {logical}") from exc
    if (
        logical != pure.as_posix()
        or pure.is_absolute()
        or ".." in pure.parts
        or relative.suffix != ".py"
        or not relative.parts
        or relative.parts[0] != "py"
    ):
        raise CompileABError(f"unsafe runtime source in manifest: {logical}")
    root = REPO_ROOT if source_root is None else source_root
    return root / Path(*pure.parts), relative


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _copy_frozen(source: Path, target: Path, label: str) -> dict[str, Any]:
    if not source.is_file():
        raise CompileABError(f"missing {label}: {source}")
    before = _path_receipt(source)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    after = _path_receipt(source)
    copied = _path_receipt(target)
    if before != after or copied["sha256"] != before["sha256"]:
        raise CompileABError(f"{label} changed while being snapshotted: {source}")
    return before


@contextmanager
def _runtime_snapshot_lock(runtime_archive: Path):
    lock_dir = Path(str(runtime_archive) + ".build.lock")
    owner = lock_dir / "owner"
    try:
        lock_dir.mkdir()
    except FileExistsError as exc:
        raise CompileABError(f"runtime archive build lock is held: {lock_dir}") from exc
    try:
        owner.write_text(str(os.getpid()) + "\n", encoding="utf-8")
        yield
    finally:
        try:
            owner.unlink()
        except FileNotFoundError:
            pass
        try:
            lock_dir.rmdir()
        except FileNotFoundError:
            pass


def _load_provenance_provider():
    path = REPO_ROOT / "pcc" / "tools" / "runtime_archive_provenance.py"
    before = _path_receipt(path)
    spec = importlib.util.spec_from_file_location("pcc_ab_runtime_provenance", path)
    if spec is None or spec.loader is None:
        raise CompileABError(f"cannot load runtime provenance verifier: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if _path_receipt(path) != before:
        raise CompileABError("runtime provenance verifier changed while loading")
    return module, before


def _seal_runtime_bundle(
    bundle_dir: Path,
    bundled_archive: Path,
    copied: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    provider, provider_receipt = _load_provenance_provider()
    ar = shutil.which("ar")
    if not ar:
        raise CompileABError("ar is required to verify the runtime bundle")
    ar_path = Path(ar).resolve(strict=True)
    manifest = provider.verify_runtime_archive_manifest(
        bundled_archive,
        runtime_root=bundle_dir,
        ar=str(ar_path),
    )
    target_path = Path(str(bundled_archive) + ".target")
    target_id = target_path.read_text(encoding="utf-8").strip()
    if not target_id or "\n" in target_id or "\r" in target_id:
        raise CompileABError("runtime target stamp is invalid")
    manifest_target = manifest.get("target_triple")
    member_count = manifest.get("member_count")
    members = manifest.get("members")
    if (
        not isinstance(manifest_target, str)
        or not manifest_target
        or not isinstance(member_count, int)
        or member_count <= 0
        or not isinstance(members, list)
        or len(members) != member_count
    ):
        raise CompileABError("runtime provenance target/member summary is invalid")
    expected_target_id = f"darwin:arm64:{manifest_target}"
    if target_id != expected_target_id:
        raise CompileABError(
            f"runtime target {target_id!r} != claim platform {expected_target_id!r}"
        )
    emitters = sorted({row.get("object_emitter") for row in members})
    checksums = sorted({row.get("codegen_checksum") for row in members})
    if len(emitters) != 1 or not isinstance(emitters[0], str) or not emitters[0]:
        raise CompileABError("runtime archive has mixed or unknown object emitters")
    if len(checksums) != 1 or not _is_sha256(checksums[0]):
        raise CompileABError("runtime archive has mixed or unknown codegen checksums")
    manifest_path = Path(str(bundled_archive) + ".provenance.json")
    capi_path = Path(str(bundled_archive) + ".capi_syms")
    wheel_path = Path(str(bundled_archive) + ".wheel")
    wheel_path.write_text(
        "pcc.runtime-wheel-artifact.v2\n"
        + "target="
        + target_id
        + "\narchive-sha256="
        + sha256_path(bundled_archive)
        + "\nmanifest-sha256="
        + sha256_path(manifest_path)
        + "\ncapi-inventory-sha256="
        + sha256_path(capi_path)
        + "\n",
        encoding="utf-8",
    )
    copied[wheel_path.relative_to(bundle_dir).as_posix()] = _path_receipt(wheel_path)
    return {
        "provider": provider_receipt,
        "ar": _path_receipt(ar_path),
        "manifest_target": manifest_target,
        "manifest_member_count": member_count,
        "wheel_target": target_id,
        "object_emitter": emitters[0],
        "codegen_checksum": checksums[0],
        "producer_claim": "binary-integrity-only; producer source closure not proven",
    }


def _prepare_runtime_bundle(
    runtime_archive: Path,
    output_dir: Path,
    *,
    runtime_source_root: Path | None = None,
) -> dict[str, Any]:
    """Snapshot, verify, and seal every runtime input used by timed compilers."""

    bundle_dir = output_dir / "runtime-bundle"
    bundle_dir.mkdir()
    bundled_archive = bundle_dir / runtime_archive.name
    copied: dict[str, dict[str, Any]] = {}
    with _runtime_snapshot_lock(runtime_archive):
        source_archive = _copy_frozen(
            runtime_archive, bundled_archive, "runtime archive"
        )
        copied[bundled_archive.name] = _path_receipt(bundled_archive)
        for suffix in RUNTIME_SIDECAR_SUFFIXES:
            source = Path(str(runtime_archive) + suffix)
            target = Path(str(bundled_archive) + suffix)
            _copy_frozen(source, target, "runtime sidecar " + suffix)
            copied[target.relative_to(bundle_dir).as_posix()] = _path_receipt(target)

        manifest_path = Path(str(bundled_archive) + ".provenance.json")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CompileABError(f"invalid runtime provenance manifest: {exc}") from exc
        members = manifest.get("members") if isinstance(manifest, dict) else None
        if not isinstance(members, list) or not members:
            raise CompileABError("runtime provenance manifest has no members")
        expected_sources: dict[Path, str] = {}
        for row in members:
            if not isinstance(row, dict):
                raise CompileABError("runtime provenance member is not an object")
            source, relative = _runtime_source_path(
                row.get("source"),
                source_root=runtime_source_root,
            )
            expected = row.get("source_sha256")
            if not _is_sha256(expected):
                raise CompileABError("runtime source receipt has invalid SHA-256")
            previous = expected_sources.get(relative)
            if previous is not None and previous != expected:
                raise CompileABError(f"runtime source has conflicting receipts: {relative}")
            expected_sources[relative] = expected
            if not source.is_file() or sha256_path(source) != expected:
                raise CompileABError(
                    f"runtime source does not match archive provenance: {source}"
                )
        for relative, expected in sorted(
            expected_sources.items(), key=lambda row: str(row[0])
        ):
            source_root = (
                REPO_ROOT if runtime_source_root is None else runtime_source_root
            )
            source = source_root / "pcc" / "py_runtime" / relative
            target = bundle_dir / relative
            _copy_frozen(source, target, "runtime source")
            if sha256_path(target) != expected:
                raise CompileABError(f"runtime source copy changed: {source}")
            copied[target.relative_to(bundle_dir).as_posix()] = _path_receipt(target)

    if source_archive["sha256"] != sha256_path(bundled_archive):
        raise CompileABError("source and bundled runtime archives differ")
    verification = _seal_runtime_bundle(bundle_dir, bundled_archive, copied)
    for path in bundle_dir.rglob("*"):
        if path.is_file():
            path.chmod(0o444)
    for name in list(copied):
        copied[name] = _path_receipt(bundle_dir / name)
    return {
        "source_archive": source_archive,
        "archive": str(bundled_archive),
        "files": copied,
        "verification": verification,
    }


def _verify_runtime_bundle(bundle: dict[str, Any]) -> None:
    bundle_dir = Path(bundle["archive"]).parent
    actual_names = sorted(
        path.relative_to(bundle_dir).as_posix()
        for path in bundle_dir.rglob("*")
        if path.is_file()
    )
    expected_names = sorted(bundle["files"])
    if actual_names != expected_names:
        raise CompileABError("runtime bundle file set changed during A/B")
    for name, receipt in bundle["files"].items():
        _verify_receipt(receipt, f"runtime bundle {name}")


def _portable_file_receipt(receipt: dict[str, Any], label: str) -> dict[str, Any]:
    digest = receipt.get("sha256")
    size = receipt.get("size_bytes")
    path = receipt.get("path")
    if not _is_sha256(digest) or not isinstance(size, int) or size < 0:
        raise CompileABError(f"{label} has an invalid content receipt")
    if not isinstance(path, str) or not path:
        raise CompileABError(f"{label} has no resolved path")
    return {"path": path, "sha256": digest, "size_bytes": size}


def verify_portable_file_receipt(receipt: dict[str, Any], label: str) -> None:
    portable = _portable_file_receipt(receipt, label)
    path = Path(portable["path"])
    if (
        not path.is_file()
        or sha256_path(path) != portable["sha256"]
        or path.stat().st_size != portable["size_bytes"]
    ):
        raise CompileABError(f"{label} changed: {path}")


def runtime_bundle_evidence(bundle: dict[str, Any]) -> dict[str, Any]:
    files = bundle.get("files")
    verification = bundle.get("verification")
    if not isinstance(files, dict) or not files or not isinstance(verification, dict):
        raise CompileABError("runtime bundle cannot produce a complete build receipt")
    provider = verification.get("provider")
    ar = verification.get("ar")
    if not isinstance(provider, dict) or not isinstance(ar, dict):
        raise CompileABError("runtime bundle receipt is missing provider or ar identity")
    portable_files = {}
    for name, receipt in sorted(files.items()):
        portable = _portable_file_receipt(receipt, "runtime bundle " + name)
        portable_files[name] = {
            "sha256": portable["sha256"],
            "size_bytes": portable["size_bytes"],
        }
    return {
        "files": portable_files,
        "provider": _portable_file_receipt(provider, "runtime provenance provider"),
        "ar": _portable_file_receipt(ar, "runtime ar tool"),
        "manifest_target": verification.get("manifest_target"),
        "manifest_member_count": verification.get("manifest_member_count"),
        "wheel_target": verification.get("wheel_target"),
        "object_emitter": verification.get("object_emitter"),
        "codegen_checksum": verification.get("codegen_checksum"),
        "producer_claim": verification.get("producer_claim"),
    }


def external_tool_evidence(host_python: Path) -> list[dict[str, Any]]:
    paths = {
        host_python.resolve(strict=True),
        TIME_BINARY.resolve(strict=True),
        OTOOL_BINARY.resolve(strict=True),
    }
    paths.update(Path(row["path"]).resolve(strict=True) for row in _toolchain_receipts())
    return [
        _portable_file_receipt(_path_receipt(path), "external build tool")
        for path in sorted(paths, key=str)
    ]


def _runtime_tree_evidence(root: Path) -> dict[str, Any]:
    resolved = root.resolve(strict=True)
    if not resolved.is_dir():
        raise CompileABError(f"host Python runtime root is not a directory: {resolved}")
    digest = hashlib.sha256()
    file_count = 0
    total_bytes = 0
    for path in sorted(resolved.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(resolved)
        if "__pycache__" in relative.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.is_symlink():
            target = os.readlink(path)
            digest.update(b"L\0")
            digest.update(relative.as_posix().encode("utf-8", "surrogateescape"))
            digest.update(b"\0")
            digest.update(target.encode("utf-8", "surrogateescape"))
            digest.update(b"\0")
            file_count += 1
            continue
        if not path.is_file():
            continue
        size = path.stat().st_size
        content_sha256 = sha256_path(path)
        digest.update(b"F\0")
        digest.update(relative.as_posix().encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        digest.update(content_sha256.encode("ascii"))
        digest.update(b"\0")
        file_count += 1
        total_bytes += size
    return {
        "root": str(resolved),
        "file_count": file_count,
        "total_bytes": total_bytes,
        "content_sha256": digest.hexdigest(),
    }


def host_python_runtime_evidence(host_python: Path) -> dict[str, Any]:
    executable = host_python.resolve(strict=True)
    if not executable.samefile(Path(sys.executable).resolve(strict=True)):
        raise CompileABError(
            "host Python runtime evidence must be produced by that interpreter"
        )
    configured = sysconfig.get_paths()
    candidate_roots = []
    for key in ("stdlib", "platstdlib", "purelib", "platlib"):
        raw = configured.get(key)
        if raw:
            root = Path(raw).expanduser().resolve(strict=True)
            if root not in candidate_roots:
                candidate_roots.append(root)
    roots: list[Path] = []
    for root in sorted(candidate_roots, key=lambda item: (len(item.parts), str(item))):
        if any(root == parent or parent in root.parents for parent in roots):
            continue
        roots.append(root)
    runtime_files: dict[str, dict[str, Any]] = {
        "executable": _portable_file_receipt(
            _path_receipt(executable), "host Python executable"
        )
    }
    framework_binary = Path(sys.base_prefix) / "Python"
    if framework_binary.is_file():
        runtime_files["framework"] = _portable_file_receipt(
            _path_receipt(framework_binary.resolve(strict=True)),
            "host Python framework",
        )
    version_info = sys.implementation.version
    return {
        "schema": "pcc.host-python-runtime.v1",
        "version": sys.version,
        "implementation": sys.implementation.name,
        "implementation_version": [
            version_info.major,
            version_info.minor,
            version_info.micro,
            version_info.releaselevel,
            version_info.serial,
        ],
        "cache_tag": sys.implementation.cache_tag,
        "prefix": str(Path(sys.prefix).resolve()),
        "base_prefix": str(Path(sys.base_prefix).resolve()),
        "platform": sys.platform,
        "machine": platform.machine(),
        "sysconfig_platform": sysconfig.get_platform(),
        "runtime_files": runtime_files,
        "roots": [_runtime_tree_evidence(root) for root in roots],
    }


def current_producer_tool_evidence() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    evidence: dict[str, Any] = {}
    live: list[dict[str, Any]] = []
    for name in ("run_pcc_stage1_build.py", "run_pcc_compile_ab.py"):
        path = REPO_ROOT / "scripts" / name
        receipt = _path_receipt(path.resolve(strict=True))
        live.append(receipt)
        evidence[name] = {
            "path": "producer-tools/" + name,
            "sha256": receipt["sha256"],
            "size_bytes": receipt["size_bytes"],
        }
    return evidence, live


def _load_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CompileABError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CompileABError(f"{label} must contain a JSON object: {path}")
    return value


def _source_manifest_identity(files: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for name in sorted(files):
        encoded = name.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
        digest.update(bytes.fromhex(files[name]))
    return digest.hexdigest()


def _load_source_manifest(path: Path) -> dict[str, Any]:
    manifest = _load_json_object(path, "source manifest")
    if set(manifest) != {"schema", "bootstrap_source_sha256", "files"}:
        raise CompileABError("source manifest fields do not match the v1 schema")
    if manifest.get("schema") != SOURCE_MANIFEST_SCHEMA:
        raise CompileABError("source manifest schema is invalid")
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise CompileABError("source manifest has no files")
    typed: dict[str, str] = {}
    for name, digest in files.items():
        pure = PurePosixPath(name) if isinstance(name, str) else None
        if (
            pure is None
            or name != pure.as_posix()
            or pure.is_absolute()
            or ".." in pure.parts
            or not _is_sha256(digest)
        ):
            raise CompileABError(f"invalid source manifest entry: {name!r}")
        typed[name] = digest
    calculated = _source_manifest_identity(typed)
    if manifest.get("bootstrap_source_sha256") != calculated:
        raise CompileABError("source manifest bootstrap identity is stale")
    return {**manifest, "files": typed}


def _snapshot_tree_entries(
    source_root: Path, label: str
) -> tuple[list[Path], list[Path]]:
    root = source_root
    if root.is_symlink() or not root.is_dir():
        raise CompileABError(f"{label} is not a real directory: {root}")
    files: list[Path] = []
    directories = [root]
    for path in root.rglob("*"):
        if path.is_symlink():
            raise CompileABError(f"{label} contains a symlink: {path}")
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            directories.append(path)
        else:
            raise CompileABError(f"{label} contains a special file: {path}")
    return files, directories


def _seal_source_snapshot(source_root: Path, label: str) -> None:
    files, directories = _snapshot_tree_entries(source_root, label)
    for path in files:
        path.chmod(0o444)
    for path in sorted(directories, key=lambda item: len(item.parts), reverse=True):
        path.chmod(0o555)


def _verify_source_snapshot(
    source_root: Path,
    manifest: dict[str, Any],
    label: str,
    *,
    require_read_only: bool = True,
) -> None:
    root = source_root
    expected_files = manifest.get("files")
    if not isinstance(expected_files, dict) or not expected_files:
        raise CompileABError(f"{label} has no manifest files")
    files, directories = _snapshot_tree_entries(root, label)
    actual_names = sorted(path.relative_to(root).as_posix() for path in files)
    expected_names = sorted(expected_files)
    closure_names = [
        path.relative_to(root).as_posix() for path in build_source_files(root)
    ]
    expected_directories = {"."}
    for name in expected_names:
        parent = PurePosixPath(name).parent
        while parent != PurePosixPath("."):
            expected_directories.add(parent.as_posix())
            parent = parent.parent
    actual_directories = {
        "." if path == root else path.relative_to(root).as_posix()
        for path in directories
    }
    if (
        actual_names != expected_names
        or closure_names != expected_names
        or actual_directories != expected_directories
    ):
        raise CompileABError(f"{label} is not the complete build closure")
    for name, expected in sorted(expected_files.items()):
        source = root / Path(*PurePosixPath(name).parts)
        if not source.is_file() or sha256_path(source) != expected:
            raise CompileABError(f"{label} differs from manifest: {name}")
    if require_read_only:
        for path in [*files, *directories]:
            if path.stat().st_mode & 0o222:
                raise CompileABError(f"{label} must be read-only: {path}")


def _load_build_receipt(
    path: Path,
    *,
    arm: str,
    compiler_sha256: str,
    runtime_sha256: str,
    compiler_size_bytes: int,
    expected_gc_backend: int | None = None,
) -> dict[str, Any]:
    receipt = _load_json_object(path, arm + " build receipt")
    if set(receipt) != BUILD_RECEIPT_FIELDS:
        missing = sorted(BUILD_RECEIPT_FIELDS - set(receipt))
        extra = sorted(set(receipt) - BUILD_RECEIPT_FIELDS)
        raise CompileABError(
            f"{arm} build receipt fields mismatch: missing={missing!r} extra={extra!r}"
        )
    if receipt.get("schema") != BUILD_RECEIPT_SCHEMA:
        raise CompileABError(f"{arm} build receipt schema is invalid")
    if receipt.get("arm") != arm or receipt.get("status") != "SUCCEEDED":
        raise CompileABError(f"{arm} build receipt is not a successful {arm} build")
    for key in (
        "compiler_sha256",
        "runtime_archive_sha256",
        "bootstrap_source_sha256",
        "primary_source_sha256",
        "source_manifest_sha256",
        "command_sha256",
        "environment_sha256",
        "stage_result_sha256",
        "runtime_bundle_sha256",
        "external_tools_sha256",
        "producer_tools_sha256",
        "host_python_runtime_sha256",
    ):
        if not _is_sha256(receipt.get(key)):
            raise CompileABError(f"{arm} build receipt {key} is invalid")
    if receipt["compiler_sha256"] != compiler_sha256:
        raise CompileABError(f"{arm} build receipt names different compiler bytes")
    recorded_compiler_size = receipt.get("compiler_size_bytes")
    if type(recorded_compiler_size) is not int or recorded_compiler_size <= 0:
        raise CompileABError(f"{arm} build receipt compiler size is invalid")
    if recorded_compiler_size != compiler_size_bytes:
        raise CompileABError(f"{arm} build receipt names a different compiler size")
    if receipt["runtime_archive_sha256"] != runtime_sha256:
        raise CompileABError(f"{arm} build receipt names a different runtime")
    command = receipt.get("command")
    environment = receipt.get("environment")
    cwd_raw = receipt.get("cwd")
    if not isinstance(command, list) or not command or any(
        not isinstance(item, str) for item in command
    ):
        raise CompileABError(f"{arm} build receipt command is invalid")
    if not isinstance(environment, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in environment.items()
    ):
        raise CompileABError(f"{arm} build receipt environment is invalid")
    if not isinstance(cwd_raw, str) or not cwd_raw:
        raise CompileABError(f"{arm} build receipt cwd is invalid")
    if _canonical_sha256(command) != receipt["command_sha256"]:
        raise CompileABError(f"{arm} build receipt command digest is stale")
    if _canonical_sha256(environment) != receipt["environment_sha256"]:
        raise CompileABError(f"{arm} build receipt environment digest is stale")
    runtime_bundle = receipt.get("runtime_bundle")
    external_tools = receipt.get("external_tools")
    producer_tools = receipt.get("producer_tools")
    host_python_runtime = receipt.get("host_python_runtime")
    if not isinstance(runtime_bundle, dict) or _canonical_sha256(
        runtime_bundle
    ) != receipt["runtime_bundle_sha256"]:
        raise CompileABError(f"{arm} build runtime bundle evidence is invalid")
    runtime_files = runtime_bundle.get("files")
    if not isinstance(runtime_files, dict) or not any(
        isinstance(row, dict) and row.get("sha256") == runtime_sha256
        for name, row in runtime_files.items()
        if isinstance(name, str) and name.endswith(".a")
    ):
        raise CompileABError(f"{arm} runtime bundle does not bind its archive")
    if not isinstance(external_tools, list) or _canonical_sha256(
        external_tools
    ) != receipt["external_tools_sha256"]:
        raise CompileABError(f"{arm} external tool evidence is invalid")
    if not isinstance(producer_tools, dict) or _canonical_sha256(
        producer_tools
    ) != receipt["producer_tools_sha256"]:
        raise CompileABError(f"{arm} producer tool evidence is invalid")
    if not isinstance(host_python_runtime, dict) or _canonical_sha256(
        host_python_runtime
    ) != receipt["host_python_runtime_sha256"]:
        raise CompileABError(f"{arm} host Python runtime evidence is invalid")
    if host_python_runtime.get("schema") != "pcc.host-python-runtime.v1":
        raise CompileABError(f"{arm} host Python runtime schema is invalid")
    for index, tool in enumerate(external_tools):
        if not isinstance(tool, dict):
            raise CompileABError(f"{arm} external tool {index} is invalid")
        _portable_file_receipt(tool, f"{arm} external tool {index}")
    for name, tool in sorted(producer_tools.items()):
        if not isinstance(name, str) or not isinstance(tool, dict):
            raise CompileABError(f"{arm} producer tool evidence is invalid")
        relative = tool.get("path")
        if not isinstance(relative, str):
            raise CompileABError(f"{arm} producer tool path is invalid")
        pure = PurePosixPath(relative)
        if relative != pure.as_posix() or pure.is_absolute() or ".." in pure.parts:
            raise CompileABError(f"{arm} producer tool path is unsafe")
        portable = _portable_file_receipt(
            {**tool, "path": str(path.parent / Path(*pure.parts))},
            f"{arm} producer tool {name}",
        )
        producer_path = Path(portable["path"])
        if (
            not producer_path.is_file()
            or sha256_path(producer_path) != portable["sha256"]
            or producer_path.stat().st_size != portable["size_bytes"]
        ):
            raise CompileABError(f"{arm} producer tool snapshot changed: {name}")
    build_runtime_raw = environment.get("PCC_RUNTIME_ARCHIVE", "")
    build_runtime = Path(build_runtime_raw)
    if (
        not build_runtime_raw
        or not build_runtime.is_absolute()
        or str(build_runtime) != build_runtime_raw
        or os.path.normpath(build_runtime_raw) != build_runtime_raw
    ):
        raise CompileABError(f"{arm} build runtime artifact path is invalid")
    source_root = Path(cwd_raw)
    if (
        not source_root.is_absolute()
        or str(source_root) != cwd_raw
        or os.path.normpath(cwd_raw) != cwd_raw
    ):
        raise CompileABError(f"{arm} build consumed source root is invalid")
    origin_raw = receipt.get("origin_source_root")
    origin_source_root = Path(origin_raw) if isinstance(origin_raw, str) else Path("")
    if (
        not isinstance(origin_raw, str)
        or not origin_source_root.is_absolute()
        or str(origin_source_root) != origin_raw
        or os.path.normpath(origin_raw) != origin_raw
    ):
        raise CompileABError(f"{arm} build origin source root is invalid")
    logical_raw = receipt.get("logical_source_root")
    logical_source_root = (
        Path(logical_raw) if isinstance(logical_raw, str) else Path("")
    )
    if (
        not isinstance(logical_raw, str)
        or not logical_source_root.is_absolute()
        or str(logical_source_root) != logical_raw
        or os.path.normpath(logical_raw) != logical_raw
        or logical_source_root != source_root
    ):
        raise CompileABError(
            f"{arm} build logical source root differs from consumed source root"
        )
    if (
        environment.get("PCC_SOURCE_ROOT") != str(source_root)
        or environment.get("PCC_REPO_ROOT") != str(source_root)
    ):
        raise CompileABError(
            f"{arm} build source environment differs from consumed snapshot"
        )
    manifest_raw = receipt.get("source_manifest")
    if not isinstance(manifest_raw, str) or not manifest_raw:
        raise CompileABError(f"{arm} build receipt has no source manifest path")
    manifest_relative = PurePosixPath(manifest_raw)
    if (
        manifest_raw != manifest_relative.as_posix()
        or manifest_relative.is_absolute()
        or ".." in manifest_relative.parts
    ):
        raise CompileABError(f"{arm} build receipt source manifest path is unsafe")
    manifest_path = path.parent / Path(*manifest_relative.parts)
    manifest_path = manifest_path.resolve(strict=True)
    if sha256_path(manifest_path) != receipt["source_manifest_sha256"]:
        raise CompileABError(f"{arm} source manifest digest differs from build receipt")
    manifest = _load_source_manifest(manifest_path)
    if manifest["bootstrap_source_sha256"] != receipt["bootstrap_source_sha256"]:
        raise CompileABError(f"{arm} bootstrap source identity differs from manifest")
    if manifest["files"].get(PRIMARY_SOURCE) != receipt["primary_source_sha256"]:
        raise CompileABError(f"{arm} primary source differs from source manifest")
    snapshot_raw = receipt.get("source_snapshot")
    if not isinstance(snapshot_raw, str) or not snapshot_raw:
        raise CompileABError(f"{arm} build receipt has no source snapshot")
    snapshot_relative = PurePosixPath(snapshot_raw)
    if (
        snapshot_raw != snapshot_relative.as_posix()
        or snapshot_relative.is_absolute()
        or ".." in snapshot_relative.parts
    ):
        raise CompileABError(f"{arm} build receipt source snapshot path is unsafe")
    snapshot_root = path.parent / Path(*snapshot_relative.parts)
    snapshot_root = snapshot_root.resolve(strict=True)
    _verify_source_snapshot(
        snapshot_root, manifest, f"{arm} source snapshot", require_read_only=True
    )
    stage_result_raw = receipt.get("stage_result")
    if not isinstance(stage_result_raw, str) or not stage_result_raw:
        raise CompileABError(f"{arm} build receipt has no stage result path")
    stage_result_relative = PurePosixPath(stage_result_raw)
    if (
        stage_result_raw != stage_result_relative.as_posix()
        or stage_result_relative.is_absolute()
        or ".." in stage_result_relative.parts
    ):
        raise CompileABError(f"{arm} build receipt stage result path is unsafe")
    stage_result_path = path.parent / Path(*stage_result_relative.parts)
    stage_result_path = stage_result_path.resolve(strict=True)
    if sha256_path(stage_result_path) != receipt["stage_result_sha256"]:
        raise CompileABError(f"{arm} stage result digest differs from build receipt")
    stage_result = _load_json_object(stage_result_path, arm + " stage result")
    # Original v1 receipts predate measurement labels. Continue admitting that
    # exact shape, or admit both current labels with their exact contract.
    current_stage_fields = STAGE_RESULT_FIELDS | STAGE_RESULT_METRIC_FIELDS
    if set(stage_result) not in (STAGE_RESULT_FIELDS, current_stage_fields):
        missing = sorted(STAGE_RESULT_FIELDS - set(stage_result))
        extra = sorted(set(stage_result) - STAGE_RESULT_FIELDS)
        raise CompileABError(
            f"{arm} stage result fields mismatch: missing={missing!r} extra={extra!r}"
        )
    if set(stage_result) == current_stage_fields and (
        _canonical_sha256(stage_result["metric_scopes"])
        != _canonical_sha256(STAGE_RESULT_METRIC_SCOPES)
        or _canonical_sha256(stage_result["comparison_contract"])
        != _canonical_sha256(STAGE_RESULT_COMPARISON_CONTRACT)
    ):
        raise CompileABError(f"{arm} stage result metric contract is invalid")
    if stage_result.get("schema") != STAGE_RESULT_SCHEMA:
        raise CompileABError(f"{arm} stage result schema is invalid")
    artifacts = stage_result.get("artifacts")
    if not isinstance(artifacts, dict) or set(artifacts) != {
        "time",
        "profile",
        "stdout",
        "stderr",
    }:
        raise CompileABError(f"{arm} stage result has incomplete artifacts")
    artifact_paths: dict[str, str] = {}
    artifact_relatives: dict[str, PurePosixPath] = {}
    for name, artifact in sorted(artifacts.items()):
        if not isinstance(artifact, dict) or set(artifact) != {
            "path",
            "sha256",
            "size_bytes",
        }:
            raise CompileABError(f"{arm} stage artifact {name} is invalid")
        relative = artifact.get("path")
        pure = PurePosixPath(relative) if isinstance(relative, str) else None
        if (
            pure is None
            or relative != pure.as_posix()
            or pure.is_absolute()
            or ".." in pure.parts
            or not _is_sha256(artifact.get("sha256"))
            or type(artifact.get("size_bytes")) is not int
            or artifact["size_bytes"] < 0
        ):
            raise CompileABError(f"{arm} stage artifact {name} receipt is invalid")
        artifact_path = path.parent / Path(*pure.parts)
        if (
            not artifact_path.is_file()
            or sha256_path(artifact_path) != artifact["sha256"]
            or artifact_path.stat().st_size != artifact["size_bytes"]
        ):
            raise CompileABError(f"{arm} stage artifact {name} changed")
        artifact_paths[name] = str(artifact_path)
        artifact_relatives[name] = pure
    if (
        stage_result.get("profile_sha256") != artifacts["profile"]["sha256"]
        or stage_result.get("stdout_sha256") != artifacts["stdout"]["sha256"]
        or stage_result.get("stderr_sha256") != artifacts["stderr"]["sha256"]
    ):
        raise CompileABError(f"{arm} stage result artifact hashes are inconsistent")
    recorded_metrics = stage_result.get("metrics")
    if not isinstance(recorded_metrics, dict) or parse_time_output(
        Path(artifact_paths["time"]).read_text(encoding="utf-8")
    ) != recorded_metrics:
        raise CompileABError(f"{arm} stage metrics differ from time artifact")
    built_compiler_raw = stage_result.get("compiler")
    built_compiler = Path(built_compiler_raw) if isinstance(
        built_compiler_raw, str
    ) else Path("")
    if (
        not isinstance(built_compiler_raw, str)
        or not built_compiler.is_absolute()
        or str(built_compiler) != built_compiler_raw
        or os.path.normpath(built_compiler_raw) != built_compiler_raw
    ):
        raise CompileABError(f"{arm} stage result compiler path is invalid")
    expected_consumed_root = built_compiler.parent / Path(*snapshot_relative.parts)
    if source_root != expected_consumed_root:
        raise CompileABError(
            f"{arm} consumed source root is not the owned build snapshot"
        )
    build_output_root = built_compiler.parent
    if (
        origin_source_root == build_output_root
        or origin_source_root in build_output_root.parents
        or build_output_root in origin_source_root.parents
    ):
        raise CompileABError(
            f"{arm} origin source root overlaps the build output directory"
        )
    linkage = stage_result.get("linkage")
    if (
        type(stage_result.get("returncode")) is not int
        or stage_result.get("returncode") != 0
        or stage_result.get("compiler_sha256") != compiler_sha256
        or type(stage_result.get("compiler_size_bytes")) is not int
        or stage_result.get("compiler_size_bytes") != recorded_compiler_size
        or not isinstance(linkage, dict)
        or set(linkage) != {"checked", "links_libpython", "links_llvm", "stdout"}
        or linkage.get("checked") is not True
        or linkage.get("links_libpython") is not False
        or linkage.get("links_llvm") is not False
        or not isinstance(linkage.get("stdout"), str)
    ):
        raise CompileABError(f"{arm} stage result is not a strict successful build")
    original_artifact_paths = {
        name: build_output_root / Path(*relative.parts)
        for name, relative in artifact_relatives.items()
    }
    expected_command = [
        str(TIME_BINARY),
        "-lp",
        "-o",
        str(original_artifact_paths["time"]),
        environment.get("PCC_HOST_PYTHON", ""),
        "-m",
        "pcc",
        "--profile-json",
        str(original_artifact_paths["profile"]),
        "--ir-scaffold=on",
        "--backend",
        "self",
        "--python-libpython",
        "off",
        str(source_root / "pcc" / "__main__.py"),
        "-o",
        str(built_compiler),
    ]
    if command != expected_command:
        raise CompileABError(f"{arm} build command is not the strict stage1 protocol")
    required_env = {
        "PCC_GC_BACKEND": {"0", "1", "2", "3", "4"},
        "PCC_RUNTIME_HIGH": {"py"},
        "PCC_SELF_LINK": {"pcc"},
        "PCC_SELF_BACKEND_PUBLISH_SYNC": {"1"},
        # Historical unoptimized receipts remain readable; their effective
        # policy stays recorded and is not relabeled as an optimized build.
        "PCC_PYTHON_IR_PASSES": {"off", "default"},
        "PCC_PYTHON_IR_PASS_JOBS": {"1"},
        "PCC_PY_FRONTEND_IR_CACHE": {"0"},
        "PCC_SELF_BACKEND_OBJECT_CACHE": {"0"},
        "PCC_DEBUG_IR_CALL": {"0"},
        "PCC_DEBUG_IR_RENDER": {"0"},
        "PYTHONHASHSEED": {"0"},
    }
    for key, accepted in required_env.items():
        if environment.get(key) not in accepted:
            raise CompileABError(f"{arm} build environment has invalid {key}")
    if (
        expected_gc_backend is not None
        and environment.get("PCC_GC_BACKEND") != str(expected_gc_backend)
    ):
        raise CompileABError(
            f"{arm} build GC backend does not match measurement backend"
        )
    for key in (
        "PCC_PY_FRONTEND_JOBS",
        "PCC_BOOTSTRAP_STAGE1_PY_FRONTEND_JOBS",
        "PCC_SELF_BACKEND_JOBS",
        "PCC_MACHO_LINK_JOBS",
    ):
        try:
            jobs = int(environment.get(key, ""))
        except ValueError as exc:
            raise CompileABError(f"{arm} build environment has invalid {key}") from exc
        if jobs <= 0:
            raise CompileABError(f"{arm} build environment has invalid {key}")
    return {
        "path": str(path),
        "sha256": sha256_path(path),
        "receipt": receipt,
        "source_manifest_path": str(manifest_path),
        "source_manifest": manifest,
        "source_snapshot_root": str(snapshot_root),
        "stage_result_path": str(stage_result_path),
        "stage_result": stage_result,
        "runtime_bundle": runtime_bundle,
        "external_tools": external_tools,
        "producer_tools": producer_tools,
        "host_python_runtime": host_python_runtime,
        "source_root": str(source_root),
        "origin_source_root": str(origin_source_root),
        "logical_source_root": str(logical_source_root),
        "artifact_paths": artifact_paths,
        "build_output_root": str(build_output_root),
    }


def _validate_single_variable(
    candidate: dict[str, Any],
    baseline: dict[str, Any],
    allowed_changed_sources: list[str],
    source_diff: Path,
) -> dict[str, Any]:
    if not allowed_changed_sources or len(set(allowed_changed_sources)) != len(
        allowed_changed_sources
    ):
        raise CompileABError("--allowed-changed-source must be non-empty and unique")
    candidate_files = candidate["source_manifest"]["files"]
    baseline_files = baseline["source_manifest"]["files"]
    changed = sorted(
        name
        for name in set(candidate_files) | set(baseline_files)
        if candidate_files.get(name) != baseline_files.get(name)
    )
    allowed = sorted(allowed_changed_sources)
    if allowed != [PRIMARY_SOURCE]:
        raise CompileABError(
            "this optimization-slice harness only admits pcc/llvm_capi/ir.py; "
            "host-helper changes require a different experiment"
        )
    if changed != allowed:
        raise CompileABError(
            f"source manifests are not single-variable: changed={changed!r} "
            f"allowed={allowed!r}"
        )
    if candidate["origin_source_root"] != baseline["origin_source_root"]:
        raise CompileABError(
            "candidate and baseline must be built from the same canonical source path"
        )
    if (
        candidate["logical_source_root"] != candidate["source_root"]
        or baseline["logical_source_root"] != baseline["source_root"]
        or candidate["logical_source_root"] != baseline["logical_source_root"]
    ):
        raise CompileABError(
            "stage1 arms consumed different absolute source roots; pcc embeds "
            "absolute source paths in __file__ and debug metadata, so this is "
            "not claim-grade single-variable evidence"
        )
    if candidate["build_output_root"] != baseline["build_output_root"]:
        raise CompileABError(
            "stage1 arms must use the same canonical build-output alias"
        )
    if candidate["runtime_bundle"] != baseline["runtime_bundle"]:
        raise CompileABError("stage1 arms used different runtime bundle closures")
    if candidate["external_tools"] != baseline["external_tools"]:
        raise CompileABError("stage1 arms used different external tool closures")
    if candidate["producer_tools"] != baseline["producer_tools"]:
        raise CompileABError("stage1 arms used different build producer tools")
    if candidate["host_python_runtime"] != baseline["host_python_runtime"]:
        raise CompileABError("stage1 arms used different host Python runtimes")

    def normalize_build_value(value: object, build_root: Path) -> object:
        if isinstance(value, str):
            root = str(build_root.resolve())
            if value == root:
                return "<BUILD_OUTPUT>"
            if value.startswith(root + os.sep):
                return "<BUILD_OUTPUT>" + value[len(root) :]
            return value
        if isinstance(value, list):
            return [normalize_build_value(item, build_root) for item in value]
        if isinstance(value, dict):
            return {
                key: normalize_build_value(item, build_root)
                for key, item in sorted(value.items())
            }
        return value

    candidate_root = Path(candidate["build_output_root"])
    baseline_root = Path(baseline["build_output_root"])
    candidate_command = normalize_build_value(
        candidate["receipt"]["command"], candidate_root
    )
    baseline_command = normalize_build_value(
        baseline["receipt"]["command"], baseline_root
    )
    if candidate_command != baseline_command:
        raise CompileABError("stage1 arm build commands differ beyond output paths")
    candidate_env = normalize_build_value(
        candidate["receipt"]["environment"], candidate_root
    )
    baseline_env = normalize_build_value(
        baseline["receipt"]["environment"], baseline_root
    )
    if candidate_env != baseline_env:
        raise CompileABError("stage1 arm build environments differ beyond output paths")
    diff_parts: list[str] = []
    for name in changed:
        baseline_path = Path(baseline["source_snapshot_root"]) / Path(
            *PurePosixPath(name).parts
        )
        candidate_path = Path(candidate["source_snapshot_root"]) / Path(
            *PurePosixPath(name).parts
        )
        try:
            before_lines = baseline_path.read_text(encoding="utf-8").splitlines(
                keepends=True
            )
            after_lines = candidate_path.read_text(encoding="utf-8").splitlines(
                keepends=True
            )
        except UnicodeDecodeError as exc:
            raise CompileABError("allowed changed sources must be UTF-8 text") from exc
        diff_parts.extend(
            difflib.unified_diff(
                before_lines,
                after_lines,
                fromfile="baseline/" + name,
                tofile="candidate/" + name,
                lineterm="\n",
            )
        )
    expected_diff = "".join(diff_parts)
    diff_text = source_diff.read_text(encoding="utf-8")
    if not expected_diff or diff_text != expected_diff:
        raise CompileABError("source diff is not the canonical diff of build snapshots")
    return {
        "allowed_changed_sources": allowed,
        "changed_sources": changed,
        "source_diff": _path_receipt(source_diff),
        "normalized_build_command": candidate_command,
        "normalized_build_environment": candidate_env,
        "logical_source_root": candidate["logical_source_root"],
    }


def _verify_measurement_inputs(
    frozen_stats: dict[str, Any],
    candidate: Path,
    baseline: Path,
    runtime_bundle: dict[str, Any],
    external_receipts: list[dict[str, Any]],
    build_evidence: dict[str, Any],
) -> None:
    _verify_frozen(
        frozen_stats,
        candidate,
        baseline,
        Path(runtime_bundle["archive"]),
    )
    _verify_runtime_bundle(runtime_bundle)
    for receipt in external_receipts:
        _verify_receipt(receipt, "external tool")
    for bundle in build_evidence.values():
        _verify_build_evidence(bundle)


def _toolchain_receipts() -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = []
    for fixed in (
        Path("/bin/sh"),
        Path("/bin/mv"),
        Path("/bin/sync"),
        Path("/usr/bin/false"),
    ):
        if not fixed.is_file():
            raise CompileABError(f"required external tool is absent: {fixed}")
        receipts.append(_path_receipt(fixed.resolve(strict=True)))
    for name in (
        "ar",
        "cat",
        "cc",
        "clang",
        "grep",
        "ld",
        "ls",
        "mkdir",
        "mktemp",
        "nm",
        "rm",
        "sleep",
        "wc",
    ):
        resolved = shutil.which(name)
        if not resolved:
            raise CompileABError(f"required tool is absent from PATH: {name}")
        receipts.append(_path_receipt(Path(resolved).resolve(strict=True)))
    return receipts


def _frozen_stats(candidate: Path, baseline: Path, runtime_archive: Path) -> dict[str, Any]:
    def receipt(path: Path) -> dict[str, int]:
        stat = path.stat()
        return {
            "device": stat.st_dev,
            "inode": stat.st_ino,
            "mode": stat.st_mode,
            "size_bytes": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
        }

    return {
        "candidate_pcc1": receipt(candidate),
        "baseline_pcc1": receipt(baseline),
        "runtime_archive": receipt(runtime_archive),
    }


def _verify_frozen(
    expected: dict[str, Any],
    candidate: Path,
    baseline: Path,
    runtime_archive: Path,
) -> None:
    # Boundary checks use metadata rather than re-reading both 180 MB pcc1
    # binaries immediately before every timed arm. Full hashes are checked at
    # the start and end; repeated pre-arm hashing would itself bias page cache.
    actual = _frozen_stats(candidate, baseline, runtime_archive)
    if actual != expected:
        raise CompileABError("compiler or runtime identity changed during A/B")


def _verify_file_hash(path: Path, expected: str, label: str) -> None:
    if sha256_path(path) != expected:
        raise CompileABError(f"{label} changed during A/B: {path}")


def _require_distinct_input_hashes(input_hashes: list[str]) -> None:
    if len(set(input_hashes)) != len(input_hashes):
        raise CompileABError("claim-grade A/B inputs must have distinct content hashes")


def _absolute_existing(path: str, *, executable: bool = False) -> Path:
    resolved = Path(path).expanduser().resolve(strict=True)
    if not resolved.is_file():
        raise CompileABError(f"missing file: {resolved}")
    if executable and not os.access(resolved, os.X_OK):
        raise CompileABError(f"not executable: {resolved}")
    return resolved


def _validate_output_dir(path: str) -> Path:
    output = Path(path).expanduser().absolute()
    resolved = output.resolve(strict=False)
    try:
        relative = resolved.relative_to(REPO_ROOT.resolve())
    except ValueError:
        return output
    if not relative.parts or relative.parts[0] != "build" or len(relative.parts) < 2:
        raise CompileABError(
            "repository-local output must be a child of build/: " + str(resolved)
        )
    return output


def _paths_overlap(first: Path, second: Path) -> bool:
    left = first.resolve(strict=False)
    right = second.resolve(strict=False)
    return left == right or left in right.parents or right in left.parents


def _declared_build_roots(receipt_path: Path, arm: str) -> tuple[Path, ...]:
    receipt = _load_json_object(receipt_path, arm + " build receipt")
    if receipt.get("schema") != BUILD_RECEIPT_SCHEMA:
        raise CompileABError(f"{arm} build receipt schema is invalid")
    raw = receipt.get("cwd")
    if (
        not isinstance(raw, str)
        or not raw
        or not Path(raw).is_absolute()
        or str(Path(raw)) != raw
        or os.path.normpath(raw) != raw
    ):
        raise CompileABError(f"{arm} build receipt has invalid consumed source root")
    source_root = Path(raw)
    origin_raw = receipt.get("origin_source_root")
    if (
        not isinstance(origin_raw, str)
        or not Path(origin_raw).is_absolute()
        or str(Path(origin_raw)) != origin_raw
        or os.path.normpath(origin_raw) != origin_raw
    ):
        raise CompileABError(f"{arm} build receipt has invalid origin source root")
    origin_source_root = Path(origin_raw)
    logical_raw = receipt.get("logical_source_root")
    if (
        not isinstance(logical_raw, str)
        or not Path(logical_raw).is_absolute()
        or str(Path(logical_raw)) != logical_raw
        or os.path.normpath(logical_raw) != logical_raw
        or Path(logical_raw) != source_root
    ):
        raise CompileABError(f"{arm} build receipt has invalid logical source root")
    snapshot_raw = receipt.get("source_snapshot")
    snapshot = PurePosixPath(snapshot_raw) if isinstance(snapshot_raw, str) else None
    if (
        snapshot is None
        or snapshot_raw != snapshot.as_posix()
        or snapshot.is_absolute()
        or ".." in snapshot.parts
    ):
        raise CompileABError(f"{arm} build receipt has invalid source snapshot")
    snapshot_root = (receipt_path.parent / Path(*snapshot.parts)).resolve(strict=True)
    roots = []
    for root in (origin_source_root, source_root, snapshot_root):
        if root not in roots:
            roots.append(root)
    return tuple(roots)


def _declared_build_source_root(receipt_path: Path, arm: str) -> Path:
    receipt = _load_json_object(receipt_path, arm + " build receipt")
    _declared_build_roots(receipt_path, arm)
    return Path(receipt["origin_source_root"])


def _snapshot_file(
    source: Path,
    target: Path,
    *,
    label: str,
    executable: bool = False,
) -> Path:
    _copy_frozen(source, target, label)
    target.chmod(0o555 if executable else 0o444)
    return target


def _snapshot_build_evidence(
    receipt_path: Path, output_dir: Path, arm: str
) -> dict[str, Any]:
    """Copy a complete stage receipt bundle before trusting any of its fields."""
    arm_dir = "arm-a" if arm == "candidate" else "arm-b"
    evidence_root = output_dir / "build-evidence" / arm_dir
    evidence_root.mkdir(parents=True)
    receipt_copy = _snapshot_file(
        receipt_path,
        evidence_root / "build-receipt.json",
        label=arm + " build receipt",
    )
    receipt = _load_json_object(receipt_copy, arm + " snapshotted build receipt")
    if receipt.get("schema") != BUILD_RECEIPT_SCHEMA:
        raise CompileABError(f"{arm} build receipt schema is invalid")
    original_root = receipt_path.parent

    def original_relative(field: str, label: str) -> tuple[Path, PurePosixPath]:
        raw = receipt.get(field)
        if not isinstance(raw, str) or not raw:
            raise CompileABError(f"{arm} build receipt has no {label}")
        pure = PurePosixPath(raw)
        if raw != pure.as_posix() or pure.is_absolute() or ".." in pure.parts:
            raise CompileABError(f"{arm} build receipt has unsafe {label}")
        return original_root / Path(*pure.parts), pure

    manifest_source, manifest_relative = original_relative(
        "source_manifest", "source manifest"
    )
    manifest_copy = _snapshot_file(
        manifest_source,
        evidence_root / Path(*manifest_relative.parts),
        label=arm + " source manifest",
    )
    manifest = _load_source_manifest(manifest_copy)
    snapshot_source, snapshot_relative = original_relative(
        "source_snapshot", "source snapshot"
    )
    _verify_source_snapshot(
        snapshot_source,
        manifest,
        f"{arm} original source snapshot",
        require_read_only=True,
    )
    snapshot_copy_root = evidence_root / Path(*snapshot_relative.parts)
    for name, expected in sorted(manifest["files"].items()):
        pure = PurePosixPath(name)
        source = snapshot_source / Path(*pure.parts)
        target = snapshot_copy_root / Path(*pure.parts)
        _snapshot_file(source, target, label=arm + " source " + name)
        if sha256_path(target) != expected:
            raise CompileABError(f"{arm} source evidence digest mismatch: {name}")
    _seal_source_snapshot(snapshot_copy_root, arm + " source evidence")
    _verify_source_snapshot(
        snapshot_copy_root,
        manifest,
        f"{arm} source evidence",
        require_read_only=True,
    )

    stage_source, stage_relative = original_relative("stage_result", "stage result")
    stage_copy = _snapshot_file(
        stage_source,
        evidence_root / Path(*stage_relative.parts),
        label=arm + " stage result",
    )
    stage_result = _load_json_object(stage_copy, arm + " snapshotted stage result")
    artifacts = stage_result.get("artifacts")
    if not isinstance(artifacts, dict):
        raise CompileABError(f"{arm} stage result has no artifacts")
    for name, artifact in sorted(artifacts.items()):
        raw = artifact.get("path") if isinstance(artifact, dict) else None
        pure = PurePosixPath(raw) if isinstance(raw, str) else None
        if pure is None or raw != pure.as_posix() or pure.is_absolute() or ".." in pure.parts:
            raise CompileABError(f"{arm} stage artifact {name} path is unsafe")
        _snapshot_file(
            original_root / Path(*pure.parts),
            evidence_root / Path(*pure.parts),
            label=arm + " stage artifact " + name,
        )

    producer_tools = receipt.get("producer_tools")
    if not isinstance(producer_tools, dict):
        raise CompileABError(f"{arm} build receipt has no producer tools")
    for name, tool in sorted(producer_tools.items()):
        raw = tool.get("path") if isinstance(tool, dict) else None
        pure = PurePosixPath(raw) if isinstance(raw, str) else None
        if pure is None or raw != pure.as_posix() or pure.is_absolute() or ".." in pure.parts:
            raise CompileABError(f"{arm} producer tool {name} path is unsafe")
        _snapshot_file(
            original_root / Path(*pure.parts),
            evidence_root / Path(*pure.parts),
            label=arm + " producer tool " + name,
        )

    files = {
        path.relative_to(evidence_root).as_posix(): _path_receipt(path)
        for path in evidence_root.rglob("*")
        if path.is_file()
    }
    return {
        "root": str(evidence_root),
        "receipt": str(receipt_copy),
        "files": files,
    }


def _verify_build_evidence(bundle: dict[str, Any]) -> None:
    root = Path(bundle["root"])
    actual = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file()
    )
    if actual != sorted(bundle["files"]):
        raise CompileABError("build evidence file set changed during A/B")
    for name, receipt in bundle["files"].items():
        _verify_receipt(receipt, "build evidence " + name)


@contextmanager
def _performance_lock():
    lock_path = REPO_ROOT / "build" / ".pcc-performance.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_stream:
        try:
            fcntl.flock(lock_stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CompileABError(
                f"another repository performance run holds {lock_path}"
            ) from exc
        owner = {
            "active": True,
            "pid": os.getpid(),
            "started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "argv": sys.argv,
        }
        lock_stream.seek(0)
        lock_stream.truncate()
        lock_stream.write(json.dumps(owner, sort_keys=True) + "\n")
        lock_stream.flush()
        os.fsync(lock_stream.fileno())
        try:
            yield
        finally:
            owner["active"] = False
            owner["completed_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
            lock_stream.seek(0)
            lock_stream.truncate()
            lock_stream.write(json.dumps(owner, sort_keys=True) + "\n")
            lock_stream.flush()
            os.fsync(lock_stream.fileno())


def _run_experiment(
    args: argparse.Namespace, *, run_token: str
) -> dict[str, Any]:
    require_claim_platform()
    if not TIME_BINARY.is_file():
        raise CompileABError("/usr/bin/time is required")
    if len(args.inputs) < 4 or len(args.inputs) % 2:
        raise CompileABError(
            "claim-grade A/B requires an even number of at least four matched inputs"
        )
    if len(args.expected_output) != len(args.inputs):
        raise CompileABError("--expected-output count must match --input count")
    _validate_thresholds(
        args.min_speedup_ratio,
        args.max_compute_regression_ratio,
        args.max_memory_regression_ratio,
    )
    if args.compile_timeout <= 0 or args.run_timeout <= 0:
        raise CompileABError("compile and run timeouts must be positive")
    if args.frontend_jobs <= 0 or args.self_backend_jobs <= 0:
        raise CompileABError("measurement worker counts must be positive")

    source_candidate = _absolute_existing(args.candidate, executable=True)
    source_baseline = _absolute_existing(args.baseline, executable=True)
    if source_candidate.samefile(source_baseline):
        raise CompileABError("candidate and baseline compiler must be distinct files")
    runtime_archive = _absolute_existing(args.runtime_archive)
    source_warmup = _absolute_existing(args.warmup)
    source_inputs = [_absolute_existing(value) for value in args.inputs]
    source_diff = _absolute_existing(args.source_diff)
    candidate_receipt_path = _absolute_existing(args.candidate_build_receipt)
    baseline_receipt_path = _absolute_existing(args.baseline_build_receipt)
    output_dir = _validate_output_dir(args.output_dir)
    for arm, receipt_path in (
        ("candidate", candidate_receipt_path),
        ("baseline", baseline_receipt_path),
    ):
        for protected_root in _declared_build_roots(receipt_path, arm):
            if _paths_overlap(protected_root, output_dir):
                raise CompileABError(
                    f"A/B output directory must not overlap {arm} build evidence"
                )
    _claim_output_directory(
        output_dir, harness="pcc-compile-alternating-ab", run_token=run_token
    )
    manifest_path = output_dir / "manifest.json"

    private_inputs = output_dir / "input-snapshot"
    private_inputs.mkdir()
    candidate_dir = private_inputs / "arm-a"
    baseline_dir = private_inputs / "arm-b"
    candidate_dir.mkdir()
    baseline_dir.mkdir()
    candidate = _snapshot_file(
        source_candidate,
        candidate_dir / "pcc1",
        label="candidate compiler",
        executable=True,
    )
    baseline = _snapshot_file(
        source_baseline,
        baseline_dir / "pcc1",
        label="baseline compiler",
        executable=True,
    )
    if candidate.name != baseline.name or len(str(candidate)) != len(str(baseline)):
        raise CompileABError("compiler snapshot paths must be shape-identical")
    warmup = _snapshot_file(
        source_warmup,
        private_inputs / "warmup.py",
        label="warmup input",
    )
    inputs = [
        _snapshot_file(
            source,
            private_inputs / f"pair{index}.py",
            label=f"pair {index} input",
        )
        for index, source in enumerate(source_inputs, 1)
    ]
    diff_copy = _snapshot_file(
        source_diff,
        private_inputs / "source-variable.diff",
        label="source diff",
    )

    build_evidence = {
        "candidate": _snapshot_build_evidence(
            candidate_receipt_path, output_dir, "candidate"
        ),
        "baseline": _snapshot_build_evidence(
            baseline_receipt_path, output_dir, "baseline"
        ),
    }

    runtime_bundle = _prepare_runtime_bundle(runtime_archive, output_dir)
    bundled_archive = Path(runtime_bundle["archive"])
    private_work = output_dir / "work"
    private_work.mkdir()
    host_python = Path(sys.executable).resolve(strict=True)
    current_external_tools = external_tool_evidence(host_python)
    current_host_python_runtime = host_python_runtime_evidence(host_python)
    external_receipts = [
        _path_receipt(Path(row["path"]).resolve(strict=True))
        for row in current_external_tools
    ]
    current_producer_tools, producer_live_receipts = current_producer_tool_evidence()
    external_receipts.extend(producer_live_receipts)
    frozen_hashes = _frozen_hashes(candidate, baseline, bundled_archive)
    frozen_stats = _frozen_stats(candidate, baseline, bundled_archive)
    if frozen_hashes["candidate_pcc1_sha256"] == frozen_hashes["baseline_pcc1_sha256"]:
        raise CompileABError("candidate and baseline compiler bytes must differ")
    runtime_sha256 = frozen_hashes["runtime_archive_sha256"]
    candidate_build = _load_build_receipt(
        Path(build_evidence["candidate"]["receipt"]),
        arm="candidate",
        compiler_sha256=frozen_hashes["candidate_pcc1_sha256"],
        runtime_sha256=runtime_sha256,
        compiler_size_bytes=candidate.stat().st_size,
        expected_gc_backend=args.gc_backend,
    )
    baseline_build = _load_build_receipt(
        Path(build_evidence["baseline"]["receipt"]),
        arm="baseline",
        compiler_sha256=frozen_hashes["baseline_pcc1_sha256"],
        runtime_sha256=runtime_sha256,
        compiler_size_bytes=baseline.stat().st_size,
        expected_gc_backend=args.gc_backend,
    )
    single_variable = _validate_single_variable(
        candidate_build,
        baseline_build,
        args.allowed_changed_source,
        diff_copy,
    )
    current_runtime_evidence = runtime_bundle_evidence(runtime_bundle)
    if (
        candidate_build["runtime_bundle"] != current_runtime_evidence
        or baseline_build["runtime_bundle"] != current_runtime_evidence
    ):
        raise CompileABError("stage1 build runtime closure differs from A/B runtime")
    if (
        candidate_build["external_tools"] != current_external_tools
        or baseline_build["external_tools"] != current_external_tools
    ):
        raise CompileABError("stage1 build tool closure differs from A/B tool closure")
    if (
        candidate_build["producer_tools"] != current_producer_tools
        or baseline_build["producer_tools"] != current_producer_tools
    ):
        raise CompileABError("stage1 producer tools differ from the A/B verifier")
    if (
        candidate_build["host_python_runtime"] != current_host_python_runtime
        or baseline_build["host_python_runtime"] != current_host_python_runtime
    ):
        raise CompileABError("stage1 host Python runtime differs from A/B runtime")
    arm_dirs = {"candidate": "arm-a", "baseline": "arm-b"}
    common_host_source_root = Path(baseline_build["source_snapshot_root"])
    host_source_roots = {
        "candidate": common_host_source_root,
        "baseline": common_host_source_root,
    }
    private_roots = {
        arm: output_dir / "private-state" / arm_dirs[arm]
        for arm in ("candidate", "baseline")
    }
    env_by_arm = {
        arm: _measurement_env(
            bundled_archive,
            args.gc_backend,
            host_source_root=host_source_roots[arm],
            host_python=host_python,
            private_root=private_roots[arm],
            frontend_jobs=args.frontend_jobs,
            self_backend_jobs=args.self_backend_jobs,
        )
        for arm in ("candidate", "baseline")
    }
    normalized_env_by_arm = {
        arm: _normalized_measurement_env(
            env_by_arm[arm],
            host_source_root=host_source_roots[arm],
            private_root=private_roots[arm],
        )
        for arm in ("candidate", "baseline")
    }
    if normalized_env_by_arm["candidate"] != normalized_env_by_arm["baseline"]:
        raise CompileABError("measurement arm environments differ beyond private paths")
    compiler_linkage = {
        "candidate": _linkage(
            candidate,
            timeout=args.run_timeout,
            env=env_by_arm["candidate"],
            cwd=private_work,
        ),
        "baseline": _linkage(
            baseline,
            timeout=args.run_timeout,
            env=env_by_arm["baseline"],
            cwd=private_work,
        ),
    }
    warmup_sha256 = sha256_path(warmup)
    input_hashes = [sha256_path(source) for source in inputs]
    _require_distinct_input_hashes(input_hashes)
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "harness": "pcc-compile-alternating-ab",
        "claim_level": "optimization-slice",
        "does_not_prove": [
            "host-pcc0-to-pcc1 versus pcc1-to-pcc2 bootstrap parity",
            "pcc2-to-pcc3 fixed point",
            "five-GC production equality",
        ],
        "status": "IN_PROGRESS",
        "run_token": run_token,
        "started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "mode": (
            "self-link/no-libpython/gc"
            + str(args.gc_backend)
            + "/runtime-object-emitter="
            + str(runtime_bundle["verification"]["object_emitter"])
        ),
        "llvm_edge": runtime_bundle["verification"]["object_emitter"]
        == "llvmlite-target-machine",
        "candidate": {"source": str(source_candidate), "snapshot": str(candidate)},
        "baseline": {"source": str(source_baseline), "snapshot": str(baseline)},
        "runtime_archive": str(bundled_archive),
        "runtime_bundle": runtime_bundle,
        "host_source_roots": {
            arm: str(root) for arm, root in host_source_roots.items()
        },
        "host_helper_policy": "common-frozen-baseline-build-evidence",
        "host_helper_bootstrap_source_sha256": baseline_build[
            "source_manifest"
        ]["bootstrap_source_sha256"],
        "external_tools": external_receipts,
        "host_python_runtime": current_host_python_runtime,
        "verifier_producer_tools": current_producer_tools,
        "frozen_hashes": frozen_hashes,
        "frozen_stats": frozen_stats,
        "build_evidence": build_evidence,
        "single_variable": single_variable,
        "compiler_linkage": compiler_linkage,
        "protocol": {
            "compile_argv_tail": [
                "--ir-scaffold=on",
                "--backend",
                "self",
                "--python-libpython",
                "off",
                "<source>",
                "-o",
                "<output>",
            ],
            "compile_timeout_s": args.compile_timeout,
            "run_timeout_s": args.run_timeout,
            "minimum_pairs": 4,
            "order": [list(pair_order(index)) for index in range(1, len(inputs) + 1)],
            "warmup_in_statistics": False,
        },
        "environment_by_arm": {
            arm: {key: values[key] for key in sorted(values)}
            for arm, values in env_by_arm.items()
        },
        "normalized_environment": normalized_env_by_arm["candidate"],
        "warmup": {
            "input": str(warmup),
            "input_sha256": warmup_sha256,
        },
        "pairs": [],
    }
    _persist(manifest_path, manifest)

    warmup_schedule = ("candidate", "baseline", "baseline", "candidate")
    warmup_rows: list[dict[str, Any]] = []
    for warmup_index, arm in enumerate(warmup_schedule, 1):
        compiler = candidate if arm == "candidate" else baseline
        suffix = arm_dirs[arm]
        print(
            f"[pcc-ab] warmup={warmup_index} arm={arm} start",
            file=sys.stderr,
            flush=True,
        )
        _verify_measurement_inputs(
            frozen_stats,
            candidate,
            baseline,
            runtime_bundle,
            external_receipts,
            build_evidence,
        )
        _verify_file_hash(warmup, warmup_sha256, "warmup input")
        output = output_dir / f"warmup{warmup_index}-{suffix}.bin"
        row = _compile_one(
            compiler,
            warmup,
            output,
            timeout=args.compile_timeout,
            env=env_by_arm[arm],
            log_prefix=output_dir / f"warmup{warmup_index}-{suffix}",
            cwd=private_work,
        )
        row["run"] = _run_output(
            output,
            timeout=args.run_timeout,
            env=env_by_arm[arm],
            cwd=private_work,
        )
        _verify_measurement_inputs(
            frozen_stats,
            candidate,
            baseline,
            runtime_bundle,
            external_receipts,
            build_evidence,
        )
        _verify_file_hash(warmup, warmup_sha256, "warmup input")
        row["arm"] = arm
        row["warmup_index"] = warmup_index
        warmup_rows.append(row)
        print(
            f"[pcc-ab] warmup={warmup_index} arm={arm} done "
            f"wall={row['metrics']['wall_s']:.2f}s",
            file=sys.stderr,
            flush=True,
        )
    if len({row["binary_sha256"] for row in warmup_rows}) != 1:
        raise CompileABError("warmup binaries differ")
    if any(row["run"] != warmup_rows[0]["run"] for row in warmup_rows[1:]):
        raise CompileABError("warmup runtime results differ")
    if warmup_rows[0]["run"]["returncode"] != 0:
        raise CompileABError("warmup runtime returned nonzero")
    manifest["warmup"]["schedule"] = list(warmup_schedule)
    manifest["warmup"]["executions"] = warmup_rows
    _persist(manifest_path, manifest)

    for pair_index, source in enumerate(inputs, 1):
        pair: dict[str, Any] = {
            "pair_index": pair_index,
            "input": str(source),
            "input_sha256": input_hashes[pair_index - 1],
            "expected_stdout": args.expected_output[pair_index - 1] + "\n",
            "order": list(pair_order(pair_index)),
        }
        for arm in pair["order"]:
            compiler = candidate if arm == "candidate" else baseline
            suffix = "arm-a" if arm == "candidate" else "arm-b"
            output = output_dir / f"pair{pair_index}-{suffix}.bin"
            print(
                f"[pcc-ab] pair={pair_index} arm={arm} start input={source.name}",
                file=sys.stderr,
                flush=True,
            )
            _verify_measurement_inputs(
                frozen_stats,
                candidate,
                baseline,
                runtime_bundle,
                external_receipts,
                build_evidence,
            )
            _verify_file_hash(
                source, input_hashes[pair_index - 1], f"pair {pair_index} input"
            )
            row = _compile_one(
                compiler,
                source,
                output,
                timeout=args.compile_timeout,
                env=env_by_arm[arm],
                log_prefix=output_dir / f"pair{pair_index}-{suffix}",
                cwd=private_work,
            )
            _verify_measurement_inputs(
                frozen_stats,
                candidate,
                baseline,
                runtime_bundle,
                external_receipts,
                build_evidence,
            )
            _verify_file_hash(
                source, input_hashes[pair_index - 1], f"pair {pair_index} input"
            )
            pair[arm] = row
            print(
                f"[pcc-ab] pair={pair_index} arm={arm} done "
                f"wall={row['metrics']['wall_s']:.2f}s",
                file=sys.stderr,
                flush=True,
            )
        # Compile both arms before doing asymmetric validation work that could
        # otherwise perturb the second timed compile's page-cache state.
        for arm in ("candidate", "baseline"):
            output = Path(pair[arm]["binary_path"])
            pair[arm]["run"] = _run_output(
                output,
                timeout=args.run_timeout,
                env=env_by_arm[arm],
                cwd=private_work,
            )
            pair[arm]["linkage"] = _linkage(
                output,
                timeout=args.run_timeout,
                env=env_by_arm[arm],
                cwd=private_work,
            )
        if pair["candidate"]["binary_sha256"] != pair["baseline"]["binary_sha256"]:
            raise CompileABError(f"pair {pair_index} binaries differ")
        if pair["candidate"]["run"] != pair["baseline"]["run"]:
            raise CompileABError(f"pair {pair_index} runtime results differ")
        if pair["candidate"]["run"]["returncode"] != 0:
            raise CompileABError(f"pair {pair_index} runtime returned nonzero")
        expected_stdout = pair["expected_stdout"]
        actual = pair["candidate"]["run"]["stdout"]
        if actual != expected_stdout:
            raise CompileABError(
                f"pair {pair_index} output {actual!r} != expected "
                f"{expected_stdout!r}"
            )
        if pair["candidate"]["run"]["stderr"]:
            raise CompileABError(f"pair {pair_index} emitted runtime stderr")
        manifest["pairs"].append(pair)
        _persist(manifest_path, manifest)

    ending_hashes = _frozen_hashes(candidate, baseline, bundled_archive)
    if ending_hashes != frozen_hashes:
        raise CompileABError("compiler or runtime identity changed during A/B")
    _verify_runtime_bundle(runtime_bundle)
    for receipt in external_receipts:
        _verify_receipt(receipt, "external tool")
    if host_python_runtime_evidence(host_python) != current_host_python_runtime:
        raise CompileABError("host Python runtime changed during A/B")
    for index, source in enumerate(inputs, 1):
        _verify_file_hash(source, input_hashes[index - 1], f"pair {index} input")
    _verify_receipt(single_variable["source_diff"], "source diff")
    manifest["summary"] = summarize_pairs(
        manifest["pairs"],
        min_speedup_ratio=args.min_speedup_ratio,
        max_compute_regression_ratio=args.max_compute_regression_ratio,
        max_memory_regression_ratio=args.max_memory_regression_ratio,
    )
    manifest["ending_hashes"] = ending_hashes
    manifest["completed_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    manifest["status"] = (
        "ACCEPTED" if manifest["summary"]["verdict"] == "ACCEPT" else "DENIED"
    )
    _persist(manifest_path, manifest)
    return manifest


def run(args: argparse.Namespace) -> dict[str, Any]:
    run_token = os.urandom(16).hex()
    output_dir = Path(args.output_dir).expanduser().absolute()
    try:
        with _performance_lock():
            return _run_experiment(args, run_token=run_token)
    except BaseException as exc:
        _mark_owned_run_failure(
            output_dir,
            run_token=run_token,
            status="INTERRUPTED" if isinstance(exc, KeyboardInterrupt) else "ERROR",
            error=str(exc),
        )
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--candidate-build-receipt", required=True)
    parser.add_argument("--baseline-build-receipt", required=True)
    parser.add_argument("--source-diff", required=True)
    parser.add_argument(
        "--allowed-changed-source",
        action="append",
        required=True,
        help="bootstrap-source path allowed to differ; repeat for multiple files",
    )
    parser.add_argument("--runtime-archive", required=True)
    parser.add_argument("--warmup", required=True)
    parser.add_argument("--input", dest="inputs", action="append", required=True)
    parser.add_argument("--expected-output", action="append", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--compile-timeout", type=int, default=120)
    parser.add_argument("--run-timeout", type=int, default=20)
    parser.add_argument("--frontend-jobs", type=int, default=10)
    parser.add_argument("--self-backend-jobs", type=int, default=8)
    parser.add_argument("--gc-backend", type=int, default=0, choices=range(5))
    parser.add_argument("--min-speedup-ratio", type=float, default=1.08)
    parser.add_argument("--max-compute-regression-ratio", type=float, default=1.0)
    parser.add_argument("--max-memory-regression-ratio", type=float, default=1.02)
    return parser


def _verdict_exit_code(summary: dict[str, Any]) -> int:
    return 0 if summary.get("verdict") == "ACCEPT" else 2


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    signal.signal(signal.SIGINT, _interrupt_handler)
    signal.signal(signal.SIGTERM, _interrupt_handler)
    try:
        manifest = run(args)
    except KeyboardInterrupt:
        print("pcc compile A/B interrupted", file=sys.stderr)
        return 130
    except (CompileABError, OSError, ValueError) as exc:
        print(f"pcc compile A/B error: {exc}", file=sys.stderr)
        return 1
    summary = manifest["summary"]
    print(
        "[pcc-ab] verdict=" + summary["verdict"]
        + " median_wall_speedup="
        + f"{summary['median_wall_speedup']:.6f}x",
        file=sys.stderr,
        flush=True,
    )
    return _verdict_exit_code(summary)


if __name__ == "__main__":
    raise SystemExit(main())
