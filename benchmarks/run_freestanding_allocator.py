#!/usr/bin/env python3
"""Measure one bounded host-vs-freestanding allocator churn shape.

The result is source-bound and mode-labeled. It records deltas; it deliberately
does not enforce a speed or footprint ranking because scheduler and allocator
noise make such a threshold unsuitable for a correctness gate.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import signal
import subprocess
import sys
from pathlib import Path
from statistics import median
from typing import Any, Iterable


REPO_ROOT = Path(__file__).absolute().parents[1]
HARNESS_SOURCE = REPO_ROOT / "benchmarks" / "c" / "freestanding_allocator_churn.c"
ALLOCATOR_SOURCE = (
    REPO_ROOT / "pcc" / "runtime" / "py" / "freestanding_allocator.py"
)
SCHEMA_VERSION = 1
DEFAULT_ROUNDS = 200_000
DEFAULT_REPEATS = 3
RESULT_FIELDS = (
    "allocator",
    "rounds",
    "elapsed_ns",
    "throughput_ops_per_sec",
    "peak_rss_bytes",
    "retained_capacity_bytes",
    "live_requested_delta",
    "live_usable_delta",
    "checksum",
)


class AllocatorBenchmarkError(RuntimeError):
    pass


def _source_files() -> tuple[Path, ...]:
    files = [HARNESS_SOURCE, ALLOCATOR_SOURCE, Path(__file__).absolute()]
    for path in (REPO_ROOT / "pcc").rglob("*"):
        if not path.is_file():
            continue
        if any(part in {"__pycache__", "build", "build_py", "build_pcc"} for part in path.parts):
            continue
        if path.suffix in {".a", ".o", ".ll", ".pyc"}:
            continue
        files.append(path)
    return tuple(sorted(set(files)))


def source_digest(paths: Iterable[Path] | None = None) -> str:
    digest = hashlib.sha256()
    for path in paths or _source_files():
        relative = path.relative_to(REPO_ROOT)
        digest.update(str(relative).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _default_cache_root() -> Path:
    configured = os.environ.get("PCC_ALLOCATOR_BENCH_CACHE")
    if configured:
        return Path(configured).expanduser().absolute()
    return Path.home() / ".cache" / "pcc" / "allocator-benchmark"


def _run_checked(
    command: list[str],
    *,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    process = subprocess.Popen(
        command,
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        raise AllocatorBenchmarkError(
            f"command timed out after {timeout}s: {' '.join(command)}"
        ) from exc
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if result.returncode != 0:
        raise AllocatorBenchmarkError(
            f"command failed rc={result.returncode}: {' '.join(command)}\n"
            f"{stdout[-2000:]}\n{stderr[-4000:]}"
        )
    return result


def _build_artifacts(cache_root: Path, *, timeout: int) -> tuple[Path, Path, str]:
    from pcc.backend.self_backend_dispatch import emit_self_asm
    from pcc.frontends.python.pipeline import compile_python

    digest = source_digest()
    build_root = cache_root / digest[:20]
    host_executable = build_root / "allocator_host"
    pcc_executable = build_root / "allocator_pcc"
    if host_executable.is_file() and pcc_executable.is_file():
        return host_executable, pcc_executable, digest

    build_root.mkdir(parents=True, exist_ok=True)
    allocator_ir = build_root / "freestanding_allocator.ll"
    allocator_asm = build_root / "freestanding_allocator.s"
    allocator_obj = build_root / "freestanding_allocator.o"
    compile_python(
        str(ALLOCATOR_SOURCE),
        str(allocator_ir),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    allocator_asm.write_text(
        emit_self_asm(allocator_ir.read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    _run_checked(
        ["clang", "-c", str(allocator_asm), "-o", str(allocator_obj)],
        timeout=timeout,
    )
    _run_checked(
        [
            "clang",
            "-O2",
            "-fno-builtin",
            "-DPCC_ALLOCATOR=0",
            str(HARNESS_SOURCE),
            "-o",
            str(host_executable),
        ],
        timeout=timeout,
    )
    _run_checked(
        [
            "clang",
            "-O2",
            "-fno-builtin",
            "-DPCC_ALLOCATOR=1",
            str(HARNESS_SOURCE),
            str(allocator_obj),
            "-o",
            str(pcc_executable),
        ],
        timeout=timeout,
    )
    return host_executable, pcc_executable, digest


def parse_result_line(line: str, *, expected_mode: str) -> dict[str, int | str]:
    parts = line.strip().split(",")
    if len(parts) != len(RESULT_FIELDS):
        raise AllocatorBenchmarkError(
            f"allocator result expected {len(RESULT_FIELDS)} fields, got {len(parts)}"
        )
    if parts[0] != expected_mode:
        raise AllocatorBenchmarkError(
            f"allocator result mode {parts[0]!r} does not match {expected_mode!r}"
        )
    try:
        numeric = [int(value) for value in parts[1:]]
    except ValueError as exc:
        raise AllocatorBenchmarkError("allocator result contains non-integer fields") from exc
    result: dict[str, int | str] = {"allocator": parts[0]}
    result.update(dict(zip(RESULT_FIELDS[1:], numeric, strict=True)))
    if int(result["rounds"]) <= 0:
        raise AllocatorBenchmarkError("allocator result has no completed rounds")
    if int(result["elapsed_ns"]) <= 0 or int(result["throughput_ops_per_sec"]) <= 0:
        raise AllocatorBenchmarkError("allocator result has invalid timing")
    if int(result["peak_rss_bytes"]) <= 0:
        raise AllocatorBenchmarkError("allocator result has invalid peak RSS")
    if int(result["retained_capacity_bytes"]) < 0:
        raise AllocatorBenchmarkError("allocator result has negative retained capacity")
    return result


def _run_samples(
    executable: Path,
    *,
    mode: str,
    rounds: int,
    repeats: int,
    timeout: int,
) -> list[dict[str, int | str]]:
    samples = []
    for _ in range(repeats):
        result = _run_checked([str(executable), str(rounds)], timeout=timeout)
        lines = [line for line in result.stdout.splitlines() if line]
        if len(lines) != 1:
            raise AllocatorBenchmarkError(
                f"allocator {mode} emitted {len(lines)} result lines"
            )
        samples.append(parse_result_line(lines[0], expected_mode=mode))
    checksums = {int(sample["checksum"]) for sample in samples}
    if len(checksums) != 1:
        raise AllocatorBenchmarkError(f"allocator {mode} checksum is nondeterministic")
    return samples


def _summarize(samples: list[dict[str, int | str]]) -> dict[str, int]:
    return {
        "throughput_ops_per_sec_median": int(
            median(int(sample["throughput_ops_per_sec"]) for sample in samples)
        ),
        "peak_rss_bytes_median": int(
            median(int(sample["peak_rss_bytes"]) for sample in samples)
        ),
        "retained_capacity_bytes_median": int(
            median(int(sample["retained_capacity_bytes"]) for sample in samples)
        ),
        "live_requested_delta_median": int(
            median(int(sample["live_requested_delta"]) for sample in samples)
        ),
        "live_usable_delta_median": int(
            median(int(sample["live_usable_delta"]) for sample in samples)
        ),
    }


def run_gate(
    *,
    rounds: int,
    repeats: int,
    cache_root: Path | None = None,
    build_timeout: int = 180,
    run_timeout: int = 60,
) -> dict[str, Any]:
    if rounds < 4096:
        raise AllocatorBenchmarkError("rounds must be at least 4096")
    if repeats < 1 or repeats > 9:
        raise AllocatorBenchmarkError("repeats must be between 1 and 9")
    host_executable, pcc_executable, digest = _build_artifacts(
        cache_root or _default_cache_root(), timeout=build_timeout
    )
    host_samples = _run_samples(
        host_executable,
        mode="host",
        rounds=rounds,
        repeats=repeats,
        timeout=run_timeout,
    )
    pcc_samples = _run_samples(
        pcc_executable,
        mode="pcc",
        rounds=rounds,
        repeats=repeats,
        timeout=run_timeout,
    )
    host_summary = _summarize(host_samples)
    pcc_summary = _summarize(pcc_samples)
    if pcc_summary["live_requested_delta_median"] != 0:
        raise AllocatorBenchmarkError("pcc allocator leaked requested bytes")
    if pcc_summary["live_usable_delta_median"] != 0:
        raise AllocatorBenchmarkError("pcc allocator leaked usable bytes")
    if pcc_summary["retained_capacity_bytes_median"] <= 0:
        raise AllocatorBenchmarkError("pcc allocator reported no retained capacity")
    return {
        "schema_version": SCHEMA_VERSION,
        "harness": "freestanding-allocator-churn",
        "mode": "host-vs-self-freestanding-allocator",
        "status": "MEASURED",
        "measured_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_sha256": digest,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python_driver": platform.python_version(),
        "rounds": rounds,
        "live_slots": 2048,
        "repeats": repeats,
        "results": [
            {"allocator": "host", "summary": host_summary, "samples": host_samples},
            {"allocator": "pcc", "summary": pcc_summary, "samples": pcc_samples},
        ],
        "deltas": {
            "throughput_pcc_minus_host_ops_per_sec": (
                pcc_summary["throughput_ops_per_sec_median"]
                - host_summary["throughput_ops_per_sec_median"]
            ),
            "peak_rss_pcc_minus_host_bytes": (
                pcc_summary["peak_rss_bytes_median"]
                - host_summary["peak_rss_bytes_median"]
            ),
            "retained_capacity_pcc_minus_host_bytes": (
                pcc_summary["retained_capacity_bytes_median"]
                - host_summary["retained_capacity_bytes_median"]
            ),
        },
        "claim_boundary": (
            "Same-host, same-source, deterministic C churn shape comparing the "
            "host allocator with the self-backend object compiled from strict "
            "freestanding pcc-Python. No speed or footprint ranking is inferred "
            "from one machine or enforced as a correctness threshold."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    parser.add_argument("--build-timeout", type=int, default=180)
    parser.add_argument("--run-timeout", type=int, default=60)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    try:
        manifest = run_gate(
            rounds=args.rounds,
            repeats=args.repeats,
            build_timeout=args.build_timeout,
            run_timeout=args.run_timeout,
        )
    except (OSError, AllocatorBenchmarkError) as exc:
        print(f"allocator benchmark failed: {exc}", file=sys.stderr)
        return 2
    blob = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(blob, encoding="utf-8")
    if not args.quiet:
        sys.stdout.write(blob)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
