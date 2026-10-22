#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pcc.diagnostics.virtual_thread_comparison import (
    build_virtual_thread_comparison_report,
    dumps_report,
    format_virtual_thread_comparison_report,
    parse_probe_output,
    run_runtime_probe,
    sample_probe_data,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="compare pcc coroutine thunk, virtual-thread substrate, and OS threads"
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=100,
        help="number of operations per workload when running the runtime probe",
    )
    parser.add_argument(
        "--probe-output",
        type=Path,
        help="parse an existing runtime probe output file instead of running one",
    )
    parser.add_argument(
        "--bootstrap-profile-dir",
        type=Path,
        help="optional PCC_BOOTSTRAP_PROFILE_DIR to summarize bootstrap impact",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="emit deterministic sample data without compiling a probe",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="output format",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=6,
        help="number of bootstrap profile phases to include",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        help="timeout in seconds for runtime build/probe steps",
    )
    parser.add_argument(
        "--keep-tmp",
        action="store_true",
        help="keep the temporary runtime/probe directory for debugging",
    )
    args = parser.parse_args(argv)

    if args.dry_run:
        probe = sample_probe_data(args.iterations)
    elif args.probe_output is not None:
        probe = parse_probe_output(args.probe_output.read_text(encoding="utf-8"))
    else:
        probe = run_runtime_probe(
            iterations=args.iterations,
            timeout=args.timeout,
            keep_tmp=args.keep_tmp,
        )

    report = build_virtual_thread_comparison_report(
        probe,
        bootstrap_profile_dir=args.bootstrap_profile_dir,
        top=args.top,
    )
    if args.format == "json":
        sys.stdout.write(dumps_report(report))
    else:
        sys.stdout.write(format_virtual_thread_comparison_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
