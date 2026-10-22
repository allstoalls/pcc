#!/usr/bin/env python3
"""Run repeated self-bootstrap attempts with structured crash-rate artifacts."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return None
    except json.JSONDecodeError:
        return None


def _stage_results(profile_dir: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for stage in (1, 2, 3):
        payload = _load_json(profile_dir / f"stage{stage}.result.json")
        if payload is not None:
            results.append(payload)
    return results


def _run_once(args: argparse.Namespace, index: int) -> dict[str, Any]:
    run_dir = args.out_root / f"run-{index:03d}"
    out_dir = run_dir / "out"
    profile_dir = run_dir / "profile"
    log_path = run_dir / "bootstrap.log"
    run_dir.mkdir(parents=True, exist_ok=True)
    profile_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / "bootstrap.py"),
        "--backend",
        args.backend,
        "--out-dir",
        str(out_dir),
        "--stage",
        str(args.stage),
    ]
    env = os.environ.copy()
    env["PCC_BOOTSTRAP_PROFILE_DIR"] = str(profile_dir)
    env.setdefault("PCC_BOOTSTRAP_RUNTIME_CC", "pcc")
    env.setdefault("PCC_BOOTSTRAP_RUNTIME_HIGH", "py")
    env.setdefault("PCC_BOOTSTRAP_PYTHON_LIBPYTHON", "off")
    if args.debug_runtime:
        env["PCC_DEBUG_RUNTIME"] = "1"
    if "LC_ALL" in env:
        del env["LC_ALL"]

    started = time.monotonic()
    if args.dry_run:
        log_path.write_text("dry-run: " + " ".join(cmd) + "\n", encoding="utf-8")
        return {
            "run": index,
            "returncode": 0,
            "duration_ms": 0,
            "dry_run": True,
            "out_dir": str(out_dir),
            "profile_dir": str(profile_dir),
            "log": str(log_path),
            "stages": [],
        }

    try:
        proc = subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=args.timeout,
        )
        stdout = proc.stdout
        stderr = proc.stderr
        returncode = proc.returncode
        timed_out = False
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        returncode = 124
        timed_out = True

    duration_ms = int((time.monotonic() - started) * 1000)
    log_path.write_text(
        stdout + ("\n[stderr]\n" + stderr if stderr else ""),
        encoding="utf-8",
    )
    return {
        "run": index,
        "returncode": returncode,
        "duration_ms": duration_ms,
        "timed_out": timed_out,
        "out_dir": str(out_dir),
        "profile_dir": str(profile_dir),
        "log": str(log_path),
        "stages": _stage_results(profile_dir),
    }


def _summarize(runs: list[dict[str, Any]]) -> dict[str, Any]:
    failures = [r for r in runs if r["returncode"] != 0]
    stage_failures: list[dict[str, Any]] = []
    for run in failures:
        stages = run.get("stages", [])
        failed = [s for s in stages if s.get("returncode", 0) != 0]
        stage_failures.append(
            {
                "run": run["run"],
                "returncode": run["returncode"],
                "failed_stages": failed,
                "log": run["log"],
            }
        )
    return {
        "schema": "pcc.bootstrap_crash_rate.v1",
        "runs": len(runs),
        "passes": len(runs) - len(failures),
        "failures": len(failures),
        "failure_rate": (len(failures) / len(runs)) if runs else 0.0,
        "stage_failures": stage_failures,
        "run_results": runs,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="measure self-bootstrap crash rate with per-run artifacts",
    )
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--stage", type=int, default=3)
    parser.add_argument("--backend", default="self")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument(
        "--out-root",
        type=Path,
        default=Path("/tmp/pcc-bootstrap-crash-rate"),
    )
    parser.add_argument("--debug-runtime", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if args.runs <= 0:
        parser.error("--runs must be positive")
    args.out_root.mkdir(parents=True, exist_ok=True)

    runs: list[dict[str, Any]] = []
    for index in range(1, args.runs + 1):
        result = _run_once(args, index)
        runs.append(result)
        print(
            f"run={index} rc={result['returncode']} "
            f"duration_ms={result['duration_ms']} log={result['log']}",
            flush=True,
        )
        if result["returncode"] != 0 and not args.dry_run:
            print("failure captured; continuing to measure crash rate", flush=True)

    summary = _summarize(runs)
    summary_path = args.out_root / "summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        f"summary runs={summary['runs']} passes={summary['passes']} "
        f"failures={summary['failures']} failure_rate={summary['failure_rate']:.3f} "
        f"path={summary_path}",
        flush=True,
    )
    return 1 if summary["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
