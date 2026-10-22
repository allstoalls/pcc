"""Bench runner — compares pcc-static vs CPython on micro-benchmarks.

Doesn't touch codegen. Each scenario is a self-contained .py file in
``benchmarks/python/scenarios/``; the runner compiles it with pcc-static, runs
it, and times it against ``python3 -c <same-source>``.

Output is a markdown table fit for pasting into a perf report.

Usage::

    python -m benchmarks.bench_runner               # all scenarios
    python -m benchmarks.bench_runner typed_loop    # one scenario
    python -m benchmarks.bench_runner --runs 5      # 5 timings per side
"""
from __future__ import annotations

import argparse
import dataclasses
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_DIR = Path(__file__).resolve().parent / "python" / "scenarios"


@dataclasses.dataclass
class Result:
    scenario: str
    pcc_seconds: list[float]
    cpy_seconds: list[float]

    @property
    def pcc_best(self) -> float:
        return min(self.pcc_seconds) if self.pcc_seconds else float("inf")

    @property
    def cpy_best(self) -> float:
        return min(self.cpy_seconds) if self.cpy_seconds else float("inf")

    @property
    def ratio(self) -> float:
        if self.cpy_best == 0:
            return float("inf")
        return self.pcc_best / self.cpy_best


def _time_run(cmd: list[str], runs: int) -> list[float]:
    out: list[float] = []
    for _ in range(runs):
        t0 = time.perf_counter()
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        elapsed = time.perf_counter() - t0
        if proc.returncode != 0:
            sys.stderr.write(
                f"[bench_runner] {' '.join(cmd)} failed:\n{proc.stderr}\n"
            )
            return []
        out.append(elapsed)
    return out


def _compile_with_pcc(src: Path, out: Path) -> bool:
    """Compile a scenario with pcc-static. Returns True on success."""
    cmd = [
        sys.executable, "-m", "pcc.driver.cli_core",
        str(src), "-o", str(out),
        "--ir-scaffold=on",
        "--python-libpython=off",
    ]
    proc = subprocess.run(
        cmd, capture_output=True, text=True,
        cwd=str(REPO_ROOT),
        env={**os.environ, "PCC_PYTHON_LIBPYTHON": "off"},
        timeout=600,
    )
    if proc.returncode != 0:
        sys.stderr.write(
            f"[bench_runner] pcc compile of {src.name} failed:\n{proc.stderr}\n"
        )
        return False
    return True


def run_scenario(name: str, runs: int, build_dir: Path) -> Result | None:
    src = SCENARIOS_DIR / f"{name}.py"
    if not src.exists():
        sys.stderr.write(f"[bench_runner] no scenario {name} at {src}\n")
        return None

    exe = build_dir / f"{name}.out"
    if not _compile_with_pcc(src, exe):
        return Result(name, [], [])

    pcc_times = _time_run([str(exe)], runs)
    cpy_times = _time_run([sys.executable, str(src)], runs)
    return Result(name, pcc_times, cpy_times)


def _format_table(results: list[Result]) -> str:
    lines = [
        "| scenario | pcc (best) | cpython (best) | pcc/cpython |",
        "|----------|-----------:|---------------:|------------:|",
    ]
    for r in results:
        if not r.pcc_seconds or not r.cpy_seconds:
            lines.append(f"| {r.scenario} | FAIL | FAIL | n/a |")
            continue
        lines.append(
            f"| {r.scenario} | {r.pcc_best:.3f}s | {r.cpy_best:.3f}s | "
            f"{r.ratio:.2f}x |"
        )
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenarios", nargs="*", help="subset to run")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--build-dir", type=Path, default=Path("/tmp/pcc-bench"))
    args = parser.parse_args(argv)

    args.build_dir.mkdir(parents=True, exist_ok=True)

    if args.scenarios:
        names = args.scenarios
    else:
        if not SCENARIOS_DIR.exists():
            sys.stderr.write(
                f"[bench_runner] no scenarios at {SCENARIOS_DIR} — "
                f"add benchmarks/python/scenarios/<name>.py files\n"
            )
            return 1
        names = sorted(p.stem for p in SCENARIOS_DIR.glob("*.py"))

    if not names:
        sys.stderr.write("[bench_runner] no scenarios\n")
        return 1

    results: list[Result] = []
    for name in names:
        r = run_scenario(name, args.runs, args.build_dir)
        if r is not None:
            results.append(r)
            if r.pcc_seconds and r.cpy_seconds:
                sys.stderr.write(
                    f"  {name}: pcc={r.pcc_best:.3f}s cpy={r.cpy_best:.3f}s "
                    f"ratio={r.ratio:.2f}x\n"
                )

    print(_format_table(results))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
