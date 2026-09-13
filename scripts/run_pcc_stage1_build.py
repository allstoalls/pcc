#!/usr/bin/env python3
"""Build one frozen stage1 compiler and emit a claim-grade build receipt."""

from __future__ import annotations

import argparse
from contextlib import nullcontext
import datetime as dt
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
AB_TOOL = REPO_ROOT / "scripts" / "run_pcc_compile_ab.py"
FUNCTION_SMOKE_SOURCE = """\
def add(left: int, right: int) -> int:
    return left + right


print(add(20, 22))
"""

STAGE1_METRIC_SCOPES = {
    "wall_s": "end_to_end_elapsed",
    "user_s": "timed_command_plus_waited_children_cpu",
    "system_s": "timed_command_plus_waited_children_cpu",
    "cpu_s": "timed_command_plus_waited_children_cpu_sum",
    "instructions": "coordinator_only_diagnostic",
    "cycles": "coordinator_only_diagnostic",
    "max_rss_bytes": "nonadditive_process_max_not_tree_sum",
    "peak_footprint_bytes": "coordinator_only_not_tree_sum",
}

STAGE1_COMPARISON_CONTRACT = {
    "primary_compute_metric": "cpu_s",
    "wall_metric_role": "paired_end_to_end_observation",
    "required_comparison": "adjacent_alternating_same_environment_pairs",
    "single_wall_verdict_allowed": False,
    "hardware_counters_allowed_for_stage_verdict": False,
}


def _load_ab_tool():
    spec = importlib.util.spec_from_file_location("pcc_compile_ab_tool", AB_TOOL)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load A/B support tool: {AB_TOOL}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _canonical_sha256(value: object) -> str:
    data = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _host_memory_budget_bytes(explicit: int) -> int:
    if explicit > 0:
        return int(explicit)
    try:
        physical = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return 0
    # ponytail: half of physical RAM is the default host budget; pass
    # --memory-budget-bytes when the machine is shared or the cap must match
    # an external sampler limit.
    return int(physical) // 2


def _resolve_frontend_jobs(raw: str, memory_budget_bytes: int) -> int:
    """One admission formula for every executor: min(cpu, cap, budget/peak).

    Numeric values stay authoritative.  ``auto`` calls the same
    ``budget_jobs`` the compiler uses, with the measured host CPython
    frontend-worker peak, so the harness and the compiled stages share one
    scheduler instead of per-stage hand tuning.
    """
    text = str(raw).strip().lower()
    if text != "auto":
        return int(text)
    sys.path.insert(0, str(REPO_ROOT))
    from pcc.py_frontend import pipeline_frontend_workers as workers

    return workers.budget_jobs(
        os.cpu_count() or 1,
        int(memory_budget_bytes),
        workers.HOST_SOURCE_WORKER_PEAK_BYTES,
        workers.HOST_SOURCE_WORKER_AUTO_CAP,
    )


def source_manifest(source_root: Path, ab) -> dict[str, Any]:
    root = source_root.resolve(strict=True)
    files = {
        path.relative_to(root).as_posix(): ab.sha256_path(path)
        for path in ab.build_source_files(root)
    }
    if not files or ab.PRIMARY_SOURCE not in files:
        raise ab.CompileABError("stage1 source snapshot is missing bootstrap sources")
    return {
        "schema": ab.SOURCE_MANIFEST_SCHEMA,
        "bootstrap_source_sha256": ab._source_manifest_identity(files),
        "files": files,
    }


def _require_immutable_source(source_root: Path, ab) -> None:
    root = source_root.resolve(strict=True)
    directories = {root}
    for source in ab.build_source_files(root):
        if source.stat().st_mode & 0o222:
            raise ab.CompileABError(
                "stage1 canonical source must be read-only: " + str(source)
            )
        current = source.parent
        while True:
            directories.add(current)
            if current == root:
                break
            current = current.parent
    for directory in directories:
        if directory.stat().st_mode & 0o222:
            raise ab.CompileABError(
                "stage1 canonical source directory must be read-only: "
                + str(directory)
            )


def _snapshot_sources(
    source_root: Path,
    manifest: dict[str, Any],
    target_root: Path,
    ab,
) -> None:
    # Canonicalize before creating children.  On Darwin ``/tmp`` aliases
    # ``/private/tmp``; keeping the lexical alias here made the verifier reject
    # files returned through their canonical path as outside the snapshot.
    target_root = target_root.resolve()
    target_root.mkdir()
    for name, expected in sorted(manifest["files"].items()):
        source = source_root / Path(*name.split("/"))
        target = target_root / Path(*name.split("/"))
        ab._copy_frozen(source, target, "stage1 source " + name)
        if ab.sha256_path(target) != expected:
            raise ab.CompileABError("stage1 source snapshot digest mismatch: " + name)
        target.chmod(0o444)
    ab._seal_source_snapshot(target_root, "stage1 source snapshot")
    ab._verify_source_snapshot(
        target_root,
        manifest,
        "stage1 source snapshot",
        require_read_only=True,
    )


def _snapshot_producer_tools(output_dir: Path, ab) -> tuple[dict[str, Any], dict[str, Any]]:
    root = output_dir / "producer-tools"
    root.mkdir()
    evidence: dict[str, Any] = {}
    live_receipts: dict[str, Any] = {}
    for name, source in (
        ("run_pcc_stage1_build.py", Path(__file__).resolve(strict=True)),
        ("run_pcc_compile_ab.py", AB_TOOL.resolve(strict=True)),
    ):
        target = root / name
        live_receipts[name] = ab._copy_frozen(source, target, "build producer tool")
        target.chmod(0o444)
        receipt = ab._path_receipt(target)
        evidence[name] = {
            "path": target.relative_to(output_dir).as_posix(),
            "sha256": receipt["sha256"],
            "size_bytes": receipt["size_bytes"],
        }
    return evidence, live_receipts


def _persist(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _set_stage1_build_modes(
    env: dict[str, str],
    *,
    with_threads: int,
    direct_indexed_emit: bool = False,
    python_ir_passes: str = "default",
) -> None:
    if with_threads not in (0, 1):
        raise ValueError("stage1 thread mode must be 0 or 1")
    env["PCC_WITH_THREADS"] = str(with_threads)
    env["PCC_PYTHON_IR_PASSES"] = python_ir_passes
    if direct_indexed_emit:
        env.update(
            {
                "PCC_DIRECT_INDEXED_KERNEL_CAPTURE": "1",
                "PCC_DIRECT_INDEXED_KERNEL_EMIT": "1",
                "PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK": "1",
                "PCC_DIRECT_INDEXED_KERNEL_FUSE_USES": "1",
                "PCC_DIRECT_INDEXED_KERNEL_RELEASE_FRONTEND": "1",
            }
        )


def _validate_source_root(path: str, ab) -> Path:
    root = Path(path).expanduser().resolve(strict=True)
    if not (root / "pcc" / "__main__.py").is_file():
        raise ab.CompileABError(f"not a pcc source snapshot: {root}")
    try:
        root.relative_to(REPO_ROOT)
    except ValueError:
        return root
    raise ab.CompileABError("stage1 source root must be an isolated external snapshot")


def _run_build(args: argparse.Namespace, ab, *, run_token: str) -> dict[str, Any]:
    ab.require_claim_platform()
    source_root = _validate_source_root(args.source_root, ab)
    runtime_archive = ab._absolute_existing(args.runtime_archive)
    output_dir = ab._validate_output_dir(args.output_dir)
    if ab._paths_overlap(source_root, output_dir):
        raise ab.CompileABError(
            "stage1 output directory must not overlap its frozen source root"
        )
    ab._claim_output_directory(
        output_dir, harness="pcc-stage1-build", run_token=run_token
    )
    output_dir = output_dir.resolve(strict=True)
    manifest_path = output_dir / "manifest.json"
    manifest: dict[str, Any] = {
        "schema": "pcc.stage1-build-run.v1",
        "status": "IN_PROGRESS",
        "run_token": run_token,
        "arm": args.arm,
        "started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_root": str(source_root),
    }
    _persist(manifest_path, manifest)

    producer_tools, live_producer_tools = _snapshot_producer_tools(output_dir, ab)

    _require_immutable_source(source_root, ab)
    before = source_manifest(source_root, ab)
    source_manifest_path = output_dir / "source-manifest.json"
    _persist(source_manifest_path, before)
    source_snapshot_root = output_dir / "source-snapshot"
    _snapshot_sources(source_root, before, source_snapshot_root, ab)
    runtime_bundle = ab._prepare_runtime_bundle(
        runtime_archive,
        output_dir,
        runtime_source_root=source_snapshot_root,
    )
    runtime_evidence = ab.runtime_bundle_evidence(runtime_bundle)
    runtime_evidence_sha256 = _canonical_sha256(runtime_evidence)
    host_python = Path(sys.executable).resolve(strict=True)
    external_tools = ab.external_tool_evidence(host_python)
    external_tools_sha256 = _canonical_sha256(external_tools)
    host_python_runtime = ab.host_python_runtime_evidence(host_python)
    host_python_runtime_sha256 = _canonical_sha256(host_python_runtime)
    bundled_archive = Path(runtime_bundle["archive"])
    private_work = output_dir / "work"
    private_work.mkdir()
    env = ab._measurement_env(
        bundled_archive,
        args.gc_backend,
        host_source_root=source_snapshot_root,
        host_python=host_python,
        private_root=output_dir / "private-state",
        frontend_jobs=args.jobs,
        self_backend_jobs=args.self_backend_jobs,
    )
    env["PCC_BOOTSTRAP_STAGE1_PY_FRONTEND_JOBS"] = str(args.jobs)
    _set_stage1_build_modes(
        env,
        with_threads=args.with_threads,
        direct_indexed_emit=args.direct_indexed_emit,
        python_ir_passes=args.python_ir_passes,
    )
    output = output_dir / "pcc1"
    time_path = output_dir / "stage1.time"
    profile_path = output_dir / "stage1.profile.json"
    command = [
        str(ab.TIME_BINARY),
        "-lp",
        "-o",
        str(time_path),
        str(host_python),
        "-m",
        "pcc",
        "--profile-json",
        str(profile_path),
        "--ir-scaffold=on",
        "--backend",
        "self",
        "--python-libpython",
        "off",
        str(source_snapshot_root / "pcc" / "__main__.py"),
        "-o",
        str(output),
    ]
    ab._verify_source_snapshot(
        source_snapshot_root,
        before,
        "stage1 source snapshot",
        require_read_only=True,
    )
    result = ab._run_process(
        command,
        timeout=args.timeout,
        env=env,
        cwd=source_snapshot_root,
    )
    (output_dir / "stage1.stdout").write_text(result.stdout, encoding="utf-8")
    (output_dir / "stage1.stderr").write_text(result.stderr, encoding="utf-8")
    if result.returncode != 0:
        raise ab.CompileABError(
            f"stage1 build failed rc={result.returncode}: {result.stderr[-4000:]}"
        )
    if not output.is_file() or not os.access(output, os.X_OK):
        raise ab.CompileABError("stage1 build produced no executable pcc1")
    metrics = ab.parse_time_output(time_path.read_text(encoding="utf-8"))
    smoke = ab._run_process(
        [str(output), "--help"],
        timeout=args.smoke_timeout,
        env=env,
        cwd=private_work,
    )
    if smoke.returncode != 0:
        raise ab.CompileABError("stage1 pcc1 --help smoke failed")
    function_smoke_source = private_work / "stage1_function_smoke.py"
    function_smoke_output = private_work / "stage1_function_smoke"
    function_smoke_source.write_text(FUNCTION_SMOKE_SOURCE, encoding="utf-8")
    function_compile = ab._run_process(
        [
            str(output),
            "--backend",
            "self",
            "--python-libpython",
            "off",
            "--ir-scaffold",
            "on",
            str(function_smoke_source),
            "-o",
            str(function_smoke_output),
        ],
        timeout=args.smoke_timeout,
        env=env,
        cwd=private_work,
    )
    (output_dir / "function-smoke-compile.stdout").write_text(
        function_compile.stdout,
        encoding="utf-8",
    )
    (output_dir / "function-smoke-compile.stderr").write_text(
        function_compile.stderr,
        encoding="utf-8",
    )
    if function_compile.returncode != 0 or not function_smoke_output.is_file():
        raise ab.CompileABError(
            "stage1 pcc1 function compile smoke failed rc="
            + str(function_compile.returncode)
            + ": "
            + function_compile.stderr[-2000:]
        )
    function_run = ab._run_process(
        [str(function_smoke_output)],
        timeout=args.smoke_timeout,
        env=env,
        cwd=private_work,
    )
    (output_dir / "function-smoke-run.stdout").write_text(
        function_run.stdout,
        encoding="utf-8",
    )
    (output_dir / "function-smoke-run.stderr").write_text(
        function_run.stderr,
        encoding="utf-8",
    )
    if function_run.returncode != 0 or function_run.stdout != "42\n":
        raise ab.CompileABError(
            "stage1 pcc1 function run smoke failed rc="
            + str(function_run.returncode)
            + " stdout="
            + repr(function_run.stdout)
            + " stderr="
            + function_run.stderr[-2000:]
        )
    linkage = ab._linkage(
        output,
        timeout=args.smoke_timeout,
        env=env,
        cwd=private_work,
    )
    after = source_manifest(source_snapshot_root, ab)
    if after != before:
        raise ab.CompileABError("stage1 source snapshot changed during build")
    ab._verify_source_snapshot(
        source_snapshot_root,
        before,
        "stage1 source snapshot",
        require_read_only=True,
    )
    for name, expected in live_producer_tools.items():
        ab._verify_receipt(expected, "build producer tool " + name)
    for index, tool in enumerate(external_tools):
        ab.verify_portable_file_receipt(tool, f"stage1 external tool {index}")
    if ab.host_python_runtime_evidence(host_python) != host_python_runtime:
        raise ab.CompileABError("host Python runtime changed during stage1 build")
    ab._verify_runtime_bundle(runtime_bundle)
    stage_result = {
        "schema": "pcc.stage1-build-result.v1",
        "returncode": result.returncode,
        "compiler": str(output),
        "compiler_sha256": ab.sha256_path(output),
        "compiler_size_bytes": output.stat().st_size,
        "metrics": metrics,
        "metric_scopes": STAGE1_METRIC_SCOPES,
        "comparison_contract": STAGE1_COMPARISON_CONTRACT,
        "profile_sha256": ab.sha256_path(profile_path),
        "stdout_sha256": hashlib.sha256(result.stdout.encode()).hexdigest(),
        "stderr_sha256": hashlib.sha256(result.stderr.encode()).hexdigest(),
        "linkage": linkage,
        "artifacts": {
            "time": {
                "path": time_path.name,
                "sha256": ab.sha256_path(time_path),
                "size_bytes": time_path.stat().st_size,
            },
            "profile": {
                "path": profile_path.name,
                "sha256": ab.sha256_path(profile_path),
                "size_bytes": profile_path.stat().st_size,
            },
            "stdout": {
                "path": "stage1.stdout",
                "sha256": ab.sha256_path(output_dir / "stage1.stdout"),
                "size_bytes": (output_dir / "stage1.stdout").stat().st_size,
            },
            "stderr": {
                "path": "stage1.stderr",
                "sha256": ab.sha256_path(output_dir / "stage1.stderr"),
                "size_bytes": (output_dir / "stage1.stderr").stat().st_size,
            },
        },
    }
    stage_result_path = output_dir / "stage1-result.json"
    _persist(stage_result_path, stage_result)
    receipt = {
        "schema": ab.BUILD_RECEIPT_SCHEMA,
        "arm": args.arm,
        "status": "SUCCEEDED",
        "compiler_sha256": stage_result["compiler_sha256"],
        "compiler_size_bytes": stage_result["compiler_size_bytes"],
        "runtime_archive_sha256": ab.sha256_path(bundled_archive),
        "bootstrap_source_sha256": before["bootstrap_source_sha256"],
        "primary_source_sha256": before["files"][ab.PRIMARY_SOURCE],
        "origin_source_root": str(source_root),
        "logical_source_root": str(source_snapshot_root),
        "source_manifest": source_manifest_path.name,
        "source_manifest_sha256": ab.sha256_path(source_manifest_path),
        "source_snapshot": source_snapshot_root.name,
        "command": command,
        "command_sha256": _canonical_sha256(command),
        "environment": env,
        "environment_sha256": _canonical_sha256(env),
        "cwd": str(source_snapshot_root),
        "stage_result": stage_result_path.name,
        "stage_result_sha256": ab.sha256_path(stage_result_path),
        "runtime_bundle": runtime_evidence,
        "runtime_bundle_sha256": runtime_evidence_sha256,
        "external_tools": external_tools,
        "external_tools_sha256": external_tools_sha256,
        "producer_tools": producer_tools,
        "producer_tools_sha256": _canonical_sha256(producer_tools),
        "host_python_runtime": host_python_runtime,
        "host_python_runtime_sha256": host_python_runtime_sha256,
    }
    receipt_path = output_dir / "build-receipt.json"
    _persist(receipt_path, receipt)
    manifest.update(
        {
            "status": "SUCCEEDED",
            "completed_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "runtime_bundle": runtime_bundle,
            "origin_source_root": str(source_root),
            "consumed_source_root": str(source_snapshot_root),
            "command": command,
            "environment": {key: env[key] for key in sorted(env)},
            "stage_result": stage_result,
            "build_receipt": str(receipt_path),
        }
    )
    _persist(manifest_path, manifest)
    return manifest


def run(args: argparse.Namespace, *, _ab=None) -> dict[str, Any]:
    """Run one stage1 build while holding the repository performance lock."""
    ab = _load_ab_tool() if _ab is None else _ab
    run_token = os.urandom(16).hex()
    output_dir = Path(args.output_dir).expanduser().absolute()
    try:
        lock_context = (
            ab._performance_lock() if args.performance_lock else nullcontext()
        )
        with lock_context:
            return _run_build(args, ab, run_token=run_token)
    except BaseException as exc:
        ab._mark_owned_run_failure(
            output_dir,
            run_token=run_token,
            status="INTERRUPTED" if isinstance(exc, KeyboardInterrupt) else "ERROR",
            error=str(exc),
        )
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=("candidate", "baseline"), required=True)
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--runtime-archive", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--timeout", type=int, default=420)
    parser.add_argument("--smoke-timeout", type=int, default=30)
    parser.add_argument("--jobs", type=str, default="auto")
    parser.add_argument("--memory-budget-bytes", type=int, default=0)
    parser.add_argument("--self-backend-jobs", type=int, default=8)
    parser.add_argument("--gc-backend", type=int, default=0, choices=range(5))
    parser.add_argument("--with-threads", type=int, default=0, choices=(0, 1))
    parser.add_argument("--direct-indexed-emit", action="store_true")
    parser.add_argument(
        "--python-ir-passes", choices=("default", "off"), default="default",
        help="owned IR pass policy for the compiler being built; off is an explicit diagnostic control",
    )
    lock_group = parser.add_mutually_exclusive_group()
    lock_group.add_argument(
        "--performance-lock",
        dest="performance_lock",
        action="store_true",
    )
    lock_group.add_argument(
        "--no-performance-lock",
        dest="performance_lock",
        action="store_false",
    )
    parser.set_defaults(performance_lock=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    ab = _load_ab_tool()
    signal.signal(signal.SIGINT, ab._interrupt_handler)
    signal.signal(signal.SIGTERM, ab._interrupt_handler)
    try:
        args.memory_budget_bytes = _host_memory_budget_bytes(
            args.memory_budget_bytes
        )
        args.jobs = _resolve_frontend_jobs(args.jobs, args.memory_budget_bytes)
        if (
            args.timeout <= 0
            or args.smoke_timeout <= 0
            or args.jobs <= 0
            or args.self_backend_jobs <= 0
        ):
            raise ab.CompileABError("timeouts and jobs must be positive")
        run(args, _ab=ab)
    except KeyboardInterrupt:
        print("pcc stage1 build interrupted", file=sys.stderr)
        return 130
    except (ab.CompileABError, OSError, ValueError) as exc:
        print(f"pcc stage1 build error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
