#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys

from pcc.diagnostics.runtime_report import format_runtime_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="print pcc runtime capabilities")
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="output format",
    )
    args = parser.parse_args(argv)
    sys.stdout.write(format_runtime_report(args.format))
    if args.format == "json":
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
