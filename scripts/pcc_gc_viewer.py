#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from pcc.diagnostics.gc_log import parse_log_lines, summarize


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Summarize pcc GC/runtime logs")
    parser.add_argument("log")
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="output format (default: text)",
    )
    args = parser.parse_args(argv)
    events = parse_log_lines(Path(args.log).read_text(encoding="utf-8").splitlines())
    summary = summarize(events).as_dict()
    if args.format == "json":
        print(json.dumps({"schema": "pcc.gc_viewer.summary.v1", "summary": summary}, sort_keys=True))
    else:
        for key in sorted(summary):
            print(f"{key}: {summary[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
