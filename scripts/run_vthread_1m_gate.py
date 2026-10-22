#!/usr/bin/env python3
"""Build and run the production-runtime virtual-thread scale gate.

The C harness links the pcc-Python runtime archive: ``PCC_RUNTIME_ARCHIVE``
when set, otherwise a private copy of ``pcc/runtime`` built with ``make``.

The default one-million run is manual-only and requires ``PCC_VTHREAD_1M=1``.
Smaller ``--n`` values are intended only for focused regression coverage.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable


REPO_ROOT = Path(__file__).absolute().parents[1]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"
BENCHMARK_SOURCE = (
    REPO_ROOT / "tests" / "benchmarks" / "vthread" / "vthread_real_runtime.c"
)
DEFAULT_N = 1_000_000
MANUAL_ENV = "PCC_VTHREAD_1M"
SCHEMA_VERSION = 1


class VThreadRuntimeGateError(RuntimeError):
    pass


def _source_files() -> tuple[Path, ...]:
    files = [BENCHMARK_SOURCE, Path(__file__).absolute(), RUNTIME_DIR / "Makefile"]
    for directory in (RUNTIME_DIR / "include", RUNTIME_DIR / "py"):
        files.extend(
            path
            for path in directory.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        )
    return tuple(sorted(files))


def source_digest(paths: Iterable[Path] | None = None) -> str:
    digest = hashlib.sha256()
    for path in paths or _source_files():
        relative = path.relative_to(REPO_ROOT)
        digest.update(str(relative).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def manual_gate_enabled(env: dict[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    return source.get(MANUAL_ENV) == "1"


def _cache_root() -> Path:
    configured = os.environ.get("PCC_VTHREAD_BENCH_CACHE")
    if configured:
        return Path(configured).expanduser().absolute()
    return Path.home() / ".cache" / "pcc" / "vthread-1m"


def _run_checked(
    command: list[str],
    *,
    timeout: int,
    cwd: Path | None = None,
    capture_output: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        capture_output=capture_output,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise VThreadRuntimeGateError(
            f"command failed rc={result.returncode}: {' '.join(command)}\n"
            f"{result.stdout[-2000:]}\n{result.stderr[-4000:]}"
        )
    return result


def build_benchmark(*, timeout: int = 1800) -> tuple[Path, Path, str]:
    explicit = os.environ.get("PCC_RUNTIME_ARCHIVE", "").strip()
    digest = source_digest()
    if explicit:
        archive = Path(explicit).expanduser().resolve(strict=True)
        digest = hashlib.sha256(
            (digest + "\0" + _archive_digest(archive)).encode("ascii")
        ).hexdigest()
    build_root = _cache_root() / digest[:20]
    runtime_copy = build_root / "py_runtime"
    executable = build_root / "vthread_real_runtime"
    if not explicit:
        archive = runtime_copy / "libpy_runtime_pcc_py.a"
    if executable.exists() and archive.exists():
        return executable, archive, digest

    if build_root.exists():
        shutil.rmtree(build_root)
    build_root.mkdir(parents=True)
    if not explicit:
        shutil.copytree(
            RUNTIME_DIR,
            runtime_copy,
            ignore=shutil.ignore_patterns(
                "build", "build_pcc", "build_py", "build_libpython", "*.a",
                "*.a.*", "_native", "__pycache__",
            ),
        )
        pcc_bin = REPO_ROOT / ".venv" / "bin" / "pcc"
        _run_checked(
            [
                "make",
                "-j" + str(min(8, os.cpu_count() or 1)),
                "-C",
                str(runtime_copy),
                f"PCC={pcc_bin if pcc_bin.is_file() else 'pcc'}",
                f"PYTHON={sys.executable}",
                f"PCC_REPO_ROOT={REPO_ROOT}",
                "libpy_runtime_pcc_py.a",
            ],
            timeout=timeout,
        )
    cc = os.environ.get("CC", "cc")
    _run_checked(
        [
            cc,
            "-O2",
            "-std=c11",
            f"-I{RUNTIME_DIR / 'include'}",
            str(BENCHMARK_SOURCE),
            str(archive),
            "-lm",
            "-o",
            str(executable),
        ],
        timeout=timeout,
    )
    return executable, archive, digest


def _archive_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _compiler_identity() -> str:
    cc = os.environ.get("CC", "cc")
    try:
        result = _run_checked([cc, "--version"], timeout=10)
    except (OSError, subprocess.TimeoutExpired, VThreadRuntimeGateError):
        return cc
    return result.stdout.splitlines()[0].strip() or cc


def _validate_result(result: dict[str, Any], *, backend: int, n: int) -> None:
    if result.get("backend") != backend:
        raise VThreadRuntimeGateError(
            f"backend result mismatch: expected {backend}, got {result.get('backend')}"
        )
    if result.get("n") != n or result.get("completed") != n:
        raise VThreadRuntimeGateError(
            f"incomplete backend {backend}: n={result.get('n')} "
            f"completed={result.get('completed')} expected={n}"
        )
    for key in ("scheduler_roots_final", "ready_final", "timer_final", "io_final"):
        if result.get(key) != 0:
            raise VThreadRuntimeGateError(
                f"backend {backend} leaked {key}={result.get(key)}"
            )
    for key in (
        "peak_rss_bytes",
        "total_ns",
        "throughput_vthreads_per_sec",
        "enqueue_mean_ns",
        "resume_mean_ns",
        "timer_park_mean_ns",
        "timer_wake_mean_ns",
        "io_park_mean_ns",
        "io_wake_mean_ns",
    ):
        if not isinstance(result.get(key), int) or result[key] <= 0:
            raise VThreadRuntimeGateError(
                f"backend {backend} missing positive metric {key}: {result.get(key)}"
            )


def run_backend(
    executable: Path,
    *,
    backend: int,
    n: int,
    timer_n: int,
    io_n: int,
    timeout: int,
) -> dict[str, Any]:
    command = [
        str(executable),
        str(backend),
        str(n),
        str(timer_n),
        str(io_n),
    ]
    # Keep the benchmark's progress stream visible while capturing only its one
    # JSON stdout line. This is a manual long gate, so silence is not acceptable.
    process = subprocess.Popen(command, stdout=subprocess.PIPE, text=True)
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        raise VThreadRuntimeGateError(
            f"backend {backend} timed out after {timeout}s"
        )
    if process.returncode != 0:
        raise VThreadRuntimeGateError(
            f"backend {backend} failed rc={process.returncode}"
        )
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise VThreadRuntimeGateError(
            f"backend {backend} emitted invalid JSON: {stdout[-2000:]}"
        ) from exc
    _validate_result(result, backend=backend, n=n)
    return result


def run_gate(
    *,
    n: int,
    backends: tuple[int, ...],
    timer_n: int,
    io_n: int,
    build_timeout: int,
    backend_timeout: int,
) -> dict[str, Any]:
    if n >= DEFAULT_N and not manual_gate_enabled():
        raise VThreadRuntimeGateError(
            f"real-runtime N={n} is manual-only; set {MANUAL_ENV}=1"
        )
    if n <= 0 or timer_n <= 0 or io_n <= 0 or timer_n + io_n >= n:
        raise VThreadRuntimeGateError(
            f"invalid workload n={n} timer_n={timer_n} io_n={io_n}"
        )
    if not backends or any(backend < 0 or backend > 4 for backend in backends):
        raise VThreadRuntimeGateError(f"invalid backend set: {backends}")

    executable, archive, digest = build_benchmark(timeout=build_timeout)
    results = [
        run_backend(
            executable,
            backend=backend,
            n=n,
            timer_n=timer_n,
            io_n=io_n,
            timeout=backend_timeout,
        )
        for backend in backends
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "harness": "pcc-production-vthread-real-runtime",
        "mode": "real-runtime",
        "status": "MEASURED",
        "measured_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source_sha256": digest,
        "runtime_archive_sha256": _archive_digest(archive),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "compiler": _compiler_identity(),
        "n": n,
        "timer_n": timer_n,
        "io_n": io_n,
        "backends": list(backends),
        "results": results,
        "claim_boundary": (
            "Current-machine no-libpython pcc runtime measurement of simultaneous "
            "pcc virtual-thread scheduler objects. Mean operation latencies are "
            "amortized phase measurements, not percentile distributions; the IO "
            "sample shares one real pipe and does not claim one million fds."
        ),
    }


def _parse_backends(value: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in value.split(",") if part != "")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid backend list: {value}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=DEFAULT_N)
    parser.add_argument("--backends", type=_parse_backends, default=(0, 1, 2, 3, 4))
    parser.add_argument("--timer-n", type=int)
    parser.add_argument("--io-n", type=int)
    parser.add_argument("--build-timeout", type=int, default=1800)
    parser.add_argument("--backend-timeout", type=int, default=600)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    timer_n = args.timer_n if args.timer_n is not None else min(100_000, max(1, args.n // 10))
    io_n = args.io_n if args.io_n is not None else min(1_000, max(1, args.n // 1000))
    try:
        manifest = run_gate(
            n=args.n,
            backends=args.backends,
            timer_n=timer_n,
            io_n=io_n,
            build_timeout=args.build_timeout,
            backend_timeout=args.backend_timeout,
        )
    except (OSError, subprocess.TimeoutExpired, VThreadRuntimeGateError) as exc:
        print(f"vthread real-runtime gate failed: {exc}", file=sys.stderr)
        return 2

    blob = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(blob, encoding="utf-8")
    sys.stdout.write(blob)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
