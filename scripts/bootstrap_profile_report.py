#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys

from pcc.diagnostics.bootstrap_profile_report import (
    build_bootstrap_profile_report,
    format_bootstrap_profile_report,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="summarize scripts/bootstrap.py stage profile JSON"
    )
    parser.add_argument(
        "profile_dir",
        help="directory passed as PCC_BOOTSTRAP_PROFILE_DIR",
    )
    parser.add_argument(
        "--log",
        help="optional bootstrap stdout/stderr log with PCC_BOOTSTRAP_STAGE_RESULT lines",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=6,
        help="number of aggregate phases to show",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="output format",
    )
    args = parser.parse_args(argv)
    report = build_bootstrap_profile_report(
        args.profile_dir,
        log_path=args.log,
        top=args.top,
    )
    if args.format == "json":
        sys.stdout.write(json.dumps(report, indent=2, sort_keys=True))
        sys.stdout.write("\n")
    else:
        sys.stdout.write(format_bootstrap_profile_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
