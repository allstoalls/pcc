#!/usr/bin/env python3
"""Quantify per-pass behavior on benchmark inputs.

Usage:
    env -u LC_ALL uv run python benchmarks/quantify_passes.py
"""

from __future__ import annotations

import argparse
from pathlib import Path

from pcc.frontends.c.evaluator.c_evaluator import (
    _compile_preprocessed_translation_unit_artifact,
    _preprocess_translation_unit_source,
)


BENCHMARKS_DIR = Path(__file__).resolve().parent / "c"
DEFAULT_BENCHES = sorted(path.name for path in BENCHMARKS_DIR.glob("*.c"))


def quantify_file(src_path: Path):
    source = src_path.read_text()
    preprocessed = _preprocess_translation_unit_source(
        source,
        str(src_path.parent),
        use_system_cpp=True,
    )
    artifact = _compile_preprocessed_translation_unit_artifact(
        src_path.name,
        preprocessed,
    )
    report = artifact["pass_report"]
    passes = report.get("passes", {})
    total_time_ms = sum(item["total_time_ms"] for item in passes.values())
    ranked = sorted(
        passes.items(),
        key=lambda item: (
            item[1]["total_time_ms"],
            item[1]["runs"],
            -item[1]["skips"],
        ),
        reverse=True,
    )
    return report, total_time_ms, ranked


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--bench",
        action="append",
        dest="benches",
        help="Benchmark filename under benchmarks/ (repeatable). Defaults to all.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=10,
        help="How many passes to show per benchmark.",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    benches = args.benches or DEFAULT_BENCHES
    aggregate_totals = {}
    aggregate_runs = {}

    print("=" * 80)
    print("Pass Quantification")
    print("=" * 80)

    for bench_name in benches:
        src_path = BENCHMARKS_DIR / bench_name
        if not src_path.is_file():
            raise FileNotFoundError(f"Benchmark source not found: {src_path}")
        report, total_time_ms, ranked = quantify_file(src_path)
        print()
        print(f"{bench_name}")
        print(f"  total_pass_time_ms: {total_time_ms:.3f}")
        print(f"  disabled_passes: {', '.join(report.get('disabled_passes', [])) or '<none>'}")
        print(f"  fail_open: {report.get('fail_open', True)}")
        print("  top_passes:")
        for name, metric in ranked[: args.top]:
            aggregate_totals[name] = aggregate_totals.get(name, 0.0) + metric["total_time_ms"]
            aggregate_runs[name] = aggregate_runs.get(name, 0) + metric["runs"]
            print(
                "   "
                f" {name:<24} "
                f"time_ms={metric['total_time_ms']:.3f} "
                f"runs={metric['runs']:<2} "
                f"skips={metric['skips']:<2} "
                f"failures={metric['failures']:<2} "
                f"status={metric['last_status'] or '-'}"
            )

        for name, metric in ranked[args.top :]:
            aggregate_totals[name] = aggregate_totals.get(name, 0.0) + metric["total_time_ms"]
            aggregate_runs[name] = aggregate_runs.get(name, 0) + metric["runs"]

    print()
    print("Aggregate Top Passes")
    ranked_totals = sorted(
        aggregate_totals.items(),
        key=lambda item: item[1],
        reverse=True,
    )
    for name, total_time_ms in ranked_totals[: args.top]:
        print(
            f"  {name:<24} time_ms={total_time_ms:.3f} "
            f"runs={aggregate_runs.get(name, 0)}"
        )


if __name__ == "__main__":
    main()
