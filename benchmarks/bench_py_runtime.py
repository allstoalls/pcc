#!/usr/bin/env python3
"""Compare pcc-emitted Python binaries against CPython runtime speed."""
from __future__ import annotations

import argparse
import math
import os
import platform
import shlex
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class CommandRun:
    command: list[str]
    returncode: int
    seconds: float
    stdout: str
    stderr: str
    timed_out: bool = False


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    return env


def _run_command(
    command: list[str],
    *,
    timeout: int,
    cwd: Path = REPO_ROOT,
) -> CommandRun:
    start = time.perf_counter()
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd),
            env=_child_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        elapsed = time.perf_counter() - start
        return CommandRun(
            command=command,
            returncode=result.returncode,
            seconds=elapsed,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
        )
    except subprocess.TimeoutExpired as exc:
        elapsed = time.perf_counter() - start
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        return CommandRun(
            command=command,
            returncode=124,
            seconds=elapsed,
            stdout=stdout,
            stderr=stderr,
            timed_out=True,
        )


def _links_libpython(path: Path) -> bool | None:
    if not path.exists():
        return None
    if sys.platform == "darwin":
        command = ["otool", "-L", str(path)]
    elif sys.platform.startswith("linux"):
        command = ["ldd", str(path)]
    else:
        return None
    result = _run_command(command, timeout=30)
    if result.returncode != 0:
        return None
    text = result.stdout + result.stderr
    return "libpython" in text or "Python.framework" in text


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return statistics.median(values)


def _fmt_seconds(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.6f}s"


def _ratio(value: float | None, baseline: float | None) -> float | None:
    if value is None or baseline is None or baseline <= 0.0:
        return None
    return value / baseline


def _typed_loop_source(n: int) -> tuple[str, str]:
    source = f"""
def total(n: int) -> int:
    acc: int = 0
    i: int = 0
    while i < n:
        acc = acc + ((i * 17) % 97)
        i = i + 1
    return acc

print(total({n}))
""".lstrip()
    expected = str(sum(((i * 17) % 97) for i in range(n))) + "\n"
    return source, expected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare a pcc-emitted Python binary against CPython."
    )
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--n", type=int, default=2_000_000)
    parser.add_argument(
        "--pcc-cmd",
        default="uv run pcc",
        help="pcc command prefix used to compile the probe",
    )
    parser.add_argument(
        "--backend",
        default="self",
        choices=("self",),
    )
    parser.add_argument(
        "--python-libpython",
        default="off",
        choices=("auto", "on", "off"),
    )
    parser.add_argument("--ir-scaffold", default="on", choices=("off", "on", "auto"))
    parser.add_argument("--compile-timeout", type=int, default=180)
    parser.add_argument("--run-timeout", type=int, default=60)
    parser.add_argument(
        "--allow-libpython",
        action="store_true",
        help="allow the produced benchmark binary to link libpython",
    )
    args = parser.parse_args(argv)

    source, expected_stdout = _typed_loop_source(args.n)
    pcc_cmd = shlex.split(args.pcc_cmd)
    with tempfile.TemporaryDirectory(prefix="pcc_runtime_bench_") as tmp_name:
        tmp = Path(tmp_name)
        src = tmp / "typed_loop.py"
        out = tmp / "typed_loop.out"
        src.write_text(source, encoding="utf-8")

        compile_run = _run_command(
            pcc_cmd
            + [
                "--backend",
                args.backend,
                "--python-libpython",
                args.python_libpython,
                "--ir-scaffold",
                args.ir_scaffold,
                str(src),
                "-o",
                str(out),
            ],
            timeout=args.compile_timeout,
        )
        if compile_run.returncode != 0:
            print("compile_failed=" + str(compile_run.returncode))
            print((compile_run.stderr or compile_run.stdout)[:2000])
            return 1

        links_libpython = _links_libpython(out)
        if links_libpython is True and not args.allow_libpython:
            print(f"compiled binary links libpython: {out}", file=sys.stderr)
            return 1

        cpython_runs: list[CommandRun] = []
        pcc_runs: list[CommandRun] = []
        for _ in range(args.runs):
            cpython_runs.append(
                _run_command([sys.executable, str(src)], timeout=args.run_timeout)
            )
            pcc_runs.append(_run_command([str(out)], timeout=args.run_timeout))

    cpython_ok = [
        run for run in cpython_runs
        if run.returncode == 0 and not run.timed_out and run.stdout == expected_stdout
    ]
    pcc_ok = [
        run for run in pcc_runs
        if run.returncode == 0 and not run.timed_out and run.stdout == expected_stdout
    ]
    cpython_median = _median([run.seconds for run in cpython_ok])
    pcc_median = _median([run.seconds for run in pcc_ok])
    ratio = _ratio(pcc_median, cpython_median)

    print("python runtime benchmark")
    print(f"host={platform.system()} {platform.machine()}")
    print(
        f"backend={args.backend} "
        f"python_libpython={args.python_libpython} "
        f"ir_scaffold={args.ir_scaffold}"
    )
    print(f"case=typed_loop n={args.n}")
    print(f"compile={_fmt_seconds(compile_run.seconds)}")
    print(f"binary_links_libpython={links_libpython}")
    print(f"cpython_run_median={_fmt_seconds(cpython_median)} ok={len(cpython_ok)}/{args.runs}")
    print(f"pcc_run_median={_fmt_seconds(pcc_median)} ok={len(pcc_ok)}/{args.runs}")
    if ratio is None:
        print("ratio_pcc_vs_cpython=n/a")
    else:
        print(f"ratio_pcc_vs_cpython={ratio:.3f}")

    return 0 if len(cpython_ok) == args.runs and len(pcc_ok) == args.runs else 1


if __name__ == "__main__":
    raise SystemExit(main())
