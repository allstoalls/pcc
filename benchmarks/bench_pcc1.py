#!/usr/bin/env python3
"""Benchmark an existing pcc1 bootstrap binary.

This script measures the compiled compiler, not CPython-hosted ``pcc``.
It expects a prebuilt ``pcc1`` binary from ``scripts/bootstrap.py`` or a
manually produced strict bootstrap run.

Examples:

    env -u LC_ALL uv run python bench/bench_pcc1.py
    env -u LC_ALL uv run python bench/bench_pcc1.py --pcc1 build/bootstrap/pcc1
    env -u LC_ALL uv run python bench/bench_pcc1.py --include-self-compile
    env -u LC_ALL uv run python bench/bench_pcc1.py \
        --baseline-cmd "env -u LC_ALL uv run pcc"
"""
from __future__ import annotations

import argparse
import json
import math
import os
import platform
import shlex
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

BENCH_CASES: tuple[tuple[str, str, str], ...] = (
    ("print_int", "print(123)\n", "123\n"),
    (
        "typed_call",
        "def add(a: int, b: int) -> int:\n"
        "    return a + b\n\n"
        "print(add(20, 22))\n",
        "42\n",
    ),
    (
        "typed_loop",
        "def total(n: int) -> int:\n"
        "    acc: int = 0\n"
        "    i: int = 0\n"
        "    while i <= n:\n"
        "        acc = acc + i\n"
        "        i = i + 1\n"
        "    return acc\n\n"
        "print(total(1000))\n",
        "500500\n",
    ),
)


@dataclass
class CommandRun:
    command: list[str]
    returncode: int
    seconds: float
    stdout: str
    stderr: str
    timed_out: bool = False


@dataclass
class CaseRun:
    name: str
    compile: CommandRun
    run: CommandRun | None
    stdout_ok: bool


@dataclass
class CompilerBench:
    label: str
    command_prefix: list[str]
    help_runs: list[CommandRun]
    case_runs: list[CaseRun]
    self_compile_runs: list[CommandRun]
    pcc2_help_runs: list[CommandRun]


def _default_pcc1_path() -> Path:
    candidates = (
        REPO_ROOT / "build" / "bootstrap-strict-self" / "pcc1",
        REPO_ROOT / "build" / "bootstrap" / "pcc1",
        REPO_ROOT / "build" / "bootstrap-self" / "pcc1",
        REPO_ROOT / "pcc1",
    )
    for path in candidates:
        if path.is_file():
            return path
    return candidates[0]


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
    except OSError as exc:
        elapsed = time.perf_counter() - start
        return CommandRun(
            command=command,
            returncode=127,
            seconds=elapsed,
            stdout="",
            stderr=str(exc),
        )


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    return statistics.median(values)


def _geomean(values: list[float]) -> float | None:
    positives = [value for value in values if value > 0.0]
    if not positives:
        return None
    return math.exp(sum(math.log(value) for value in positives) / len(positives))


def _fmt_seconds(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.3f}s"


def _compile_args(
    src_path: Path,
    out_path: Path,
    *,
    backend: str,
    python_libpython: str,
    ir_scaffold: str,
) -> list[str]:
    return [
        "--backend",
        backend,
        "--python-libpython",
        python_libpython,
        "--ir-scaffold",
        ir_scaffold,
        str(src_path),
        "-o",
        str(out_path),
    ]


def _bench_compiler(
    *,
    label: str,
    command_prefix: list[str],
    runs: int,
    backend: str,
    python_libpython: str,
    ir_scaffold: str,
    compile_timeout: int,
    run_timeout: int,
    include_self_compile: bool,
    self_compile_runs: int,
    self_compile_timeout: int,
) -> CompilerBench:
    help_runs: list[CommandRun] = []
    for _ in range(runs):
        help_runs.append(
            _run_command(command_prefix + ["--help"], timeout=run_timeout)
        )

    case_runs: list[CaseRun] = []
    with tempfile.TemporaryDirectory(prefix=f"pcc1_bench_{label}_") as tmp_name:
        tmp = Path(tmp_name)
        for case_name, source, expected_stdout in BENCH_CASES:
            src = tmp / f"{case_name}.py"
            src.write_text(source, encoding="utf-8")
            for run_index in range(runs):
                out = tmp / f"{case_name}_{run_index}.out"
                compile_run = _run_command(
                    command_prefix
                    + _compile_args(
                        src,
                        out,
                        backend=backend,
                        python_libpython=python_libpython,
                        ir_scaffold=ir_scaffold,
                    ),
                    timeout=compile_timeout,
                )
                run = None
                stdout_ok = False
                if compile_run.returncode == 0:
                    run = _run_command([str(out)], timeout=run_timeout)
                    stdout_ok = (
                        run.returncode == 0 and run.stdout == expected_stdout
                    )
                case_runs.append(
                    CaseRun(
                        name=case_name,
                        compile=compile_run,
                        run=run,
                        stdout_ok=stdout_ok,
                    )
                )

    pcc2_help_runs: list[CommandRun] = []
    full_self_compile_runs: list[CommandRun] = []
    if include_self_compile:
        with tempfile.TemporaryDirectory(prefix=f"pcc1_self_compile_{label}_") as tmp_name:
            tmp = Path(tmp_name)
            src = REPO_ROOT / "pcc" / "__main__.py"
            for run_index in range(self_compile_runs):
                out = tmp / f"pcc2_{run_index}"
                compile_run = _run_command(
                    command_prefix
                    + _compile_args(
                        src,
                        out,
                        backend=backend,
                        python_libpython=python_libpython,
                        ir_scaffold=ir_scaffold,
                    ),
                    timeout=self_compile_timeout,
                )
                full_self_compile_runs.append(compile_run)
                if compile_run.returncode == 0:
                    pcc2_help_runs.append(
                        _run_command([str(out), "--help"], timeout=run_timeout)
                    )

    return CompilerBench(
        label=label,
        command_prefix=command_prefix,
        help_runs=help_runs,
        case_runs=case_runs,
        self_compile_runs=full_self_compile_runs,
        pcc2_help_runs=pcc2_help_runs,
    )


def _ok_runs(runs: list[CommandRun]) -> list[CommandRun]:
    return [run for run in runs if run.returncode == 0 and not run.timed_out]


def _case_compile_seconds(result: CompilerBench) -> list[float]:
    return [
        run.compile.seconds
        for run in result.case_runs
        if run.compile.returncode == 0 and not run.compile.timed_out
    ]


def _case_run_seconds(result: CompilerBench) -> list[float]:
    values: list[float] = []
    for run in result.case_runs:
        if run.run is None or not run.stdout_ok:
            continue
        if run.run.returncode == 0 and not run.run.timed_out:
            values.append(run.run.seconds)
    return values


def _print_summary(result: CompilerBench) -> None:
    help_ok = _ok_runs(result.help_runs)
    compile_seconds = _case_compile_seconds(result)
    run_seconds = _case_run_seconds(result)
    self_compile_ok = _ok_runs(result.self_compile_runs)
    pcc2_help_ok = _ok_runs(result.pcc2_help_runs)

    failed_compiles = [
        run for run in result.case_runs if run.compile.returncode != 0
    ]
    failed_runs = [
        run for run in result.case_runs if run.run is None or not run.stdout_ok
    ]

    print(f"\n== {result.label}")
    print("command: " + " ".join(shlex.quote(part) for part in result.command_prefix))
    print(
        "help_median="
        + _fmt_seconds(_median([run.seconds for run in help_ok]))
        + f" ok={len(help_ok)}/{len(result.help_runs)}"
    )
    print(
        "smoke_compile_geomean="
        + _fmt_seconds(_geomean(compile_seconds))
        + f" ok={len(compile_seconds)}/{len(result.case_runs)}"
    )
    print(
        "smoke_run_geomean="
        + _fmt_seconds(_geomean(run_seconds))
        + f" ok={len(run_seconds)}/{len(result.case_runs)}"
    )
    if result.self_compile_runs:
        print(
            "self_compile_median="
            + _fmt_seconds(_median([run.seconds for run in self_compile_ok]))
            + f" ok={len(self_compile_ok)}/{len(result.self_compile_runs)}"
        )
        print(
            "pcc2_help_median="
            + _fmt_seconds(_median([run.seconds for run in pcc2_help_ok]))
            + f" ok={len(pcc2_help_ok)}/{len(result.pcc2_help_runs)}"
        )
    if failed_compiles:
        first = failed_compiles[0].compile
        print(f"first_compile_failure={first.returncode}: {(first.stderr or first.stdout)[:240]}")
    if failed_runs:
        first_case = failed_runs[0]
        if first_case.run is None:
            print(f"first_run_failure={first_case.name}: no executable produced")
        else:
            detail = first_case.run.stderr or first_case.run.stdout
            print(f"first_run_failure={first_case.name}: {detail[:240]}")


def _ratio(value: float | None, baseline: float | None) -> float | None:
    if value is None or baseline is None or baseline <= 0.0:
        return None
    return value / baseline


def _print_comparison(lhs: CompilerBench, rhs: CompilerBench) -> None:
    lhs_help = _median([run.seconds for run in _ok_runs(lhs.help_runs)])
    rhs_help = _median([run.seconds for run in _ok_runs(rhs.help_runs)])
    lhs_compile = _geomean(_case_compile_seconds(lhs))
    rhs_compile = _geomean(_case_compile_seconds(rhs))
    lhs_run = _geomean(_case_run_seconds(lhs))
    rhs_run = _geomean(_case_run_seconds(rhs))
    lhs_self_compile = _median([
        run.seconds for run in _ok_runs(lhs.self_compile_runs)
    ])
    rhs_self_compile = _median([
        run.seconds for run in _ok_runs(rhs.self_compile_runs)
    ])
    lhs_pcc2_help = _median([
        run.seconds for run in _ok_runs(lhs.pcc2_help_runs)
    ])
    rhs_pcc2_help = _median([
        run.seconds for run in _ok_runs(rhs.pcc2_help_runs)
    ])
    print(f"\n== ratio {lhs.label}/{rhs.label}")
    for name, value, baseline in (
        ("help", lhs_help, rhs_help),
        ("smoke_compile", lhs_compile, rhs_compile),
        ("smoke_run", lhs_run, rhs_run),
        ("self_compile", lhs_self_compile, rhs_self_compile),
        ("pcc2_help", lhs_pcc2_help, rhs_pcc2_help),
    ):
        ratio = _ratio(value, baseline)
        print(f"{name}={ratio:.3f}" if ratio is not None else f"{name}=n/a")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark an existing pcc1 bootstrap binary."
    )
    parser.add_argument(
        "--pcc1",
        default=str(_default_pcc1_path()),
        help="pcc1 binary to benchmark; default: first existing build/bootstrap*/pcc1",
    )
    parser.add_argument(
        "--baseline-cmd",
        default="",
        help=(
            "optional baseline command prefix, e.g. "
            "'env -u LC_ALL uv run pcc'"
        ),
    )
    parser.add_argument("--runs", type=int, default=3)
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
    parser.add_argument("--compile-timeout", type=int, default=120)
    parser.add_argument("--run-timeout", type=int, default=30)
    parser.add_argument(
        "--include-self-compile",
        action="store_true",
        help="also time pcc1 compiling pcc/__main__.py into a pcc2 binary",
    )
    parser.add_argument("--self-compile-runs", type=int, default=1)
    parser.add_argument("--self-compile-timeout", type=int, default=900)
    parser.add_argument(
        "--allow-libpython-pcc1",
        action="store_true",
        help=(
            "benchmark a pcc1 binary even if it links libpython; default "
            "is to require a strict no-libpython pcc1"
        ),
    )
    parser.add_argument("--json", dest="json_path", default="")
    args = parser.parse_args(argv)

    pcc1 = Path(args.pcc1)
    if not pcc1.is_file():
        print(
            f"pcc1 binary not found: {pcc1}\n"
            "Build a strict pcc1 first with:\n"
            "  mkdir -p build/bootstrap-strict-self\n"
            "  env -u LC_ALL uv run pcc --backend self "
            "--python-libpython off --ir-scaffold on "
            "pcc/__main__.py -o build/bootstrap-strict-self/pcc1\n"
            "or pass --pcc1.",
            file=sys.stderr,
        )
        return 2
    links_libpython = _links_libpython(pcc1)
    if links_libpython is True and not args.allow_libpython_pcc1:
        print(
            f"pcc1 links libpython: {pcc1}\n"
            "Build a strict pcc1 with:\n"
            "  mkdir -p build/bootstrap-strict-self\n"
            "  env -u LC_ALL uv run pcc --backend self "
            "--python-libpython off --ir-scaffold on "
            "pcc/__main__.py -o build/bootstrap-strict-self/pcc1\n"
            "or pass --allow-libpython-pcc1 to benchmark a non-strict binary.",
            file=sys.stderr,
        )
        return 2

    results: list[CompilerBench] = []
    results.append(
        _bench_compiler(
            label="pcc1",
            command_prefix=[str(pcc1)],
            runs=args.runs,
            backend=args.backend,
            python_libpython=args.python_libpython,
            ir_scaffold=args.ir_scaffold,
            compile_timeout=args.compile_timeout,
            run_timeout=args.run_timeout,
            include_self_compile=args.include_self_compile,
            self_compile_runs=args.self_compile_runs,
            self_compile_timeout=args.self_compile_timeout,
        )
    )
    if args.baseline_cmd.strip():
        results.append(
            _bench_compiler(
                label="baseline",
                command_prefix=shlex.split(args.baseline_cmd),
                runs=args.runs,
                backend=args.backend,
                python_libpython=args.python_libpython,
                ir_scaffold=args.ir_scaffold,
                compile_timeout=args.compile_timeout,
                run_timeout=args.run_timeout,
                include_self_compile=args.include_self_compile,
                self_compile_runs=args.self_compile_runs,
                self_compile_timeout=args.self_compile_timeout,
            )
        )

    print("pcc1 benchmark")
    print(f"host={platform.system()} {platform.machine()}")
    print(f"pcc1={pcc1}")
    print(f"pcc1_links_libpython={links_libpython}")
    print(
        f"backend={args.backend} "
        f"python_libpython={args.python_libpython} "
        f"ir_scaffold={args.ir_scaffold}"
    )
    print(f"runs={args.runs}")
    for result in results:
        _print_summary(result)
    if len(results) == 2:
        _print_comparison(results[0], results[1])

    if args.json_path:
        payload = [asdict(result) for result in results]
        Path(args.json_path).write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    for result in results:
        for run in result.help_runs:
            if run.returncode != 0:
                return 1
        for case in result.case_runs:
            if case.compile.returncode != 0 or not case.stdout_ok:
                return 1
        for run in result.self_compile_runs:
            if run.returncode != 0:
                return 1
        for run in result.pcc2_help_runs:
            if run.returncode != 0:
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
