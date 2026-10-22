#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from pcc.driver.build_cache import compute_cache_key


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Compute a pcc C build cache key.")
    parser.add_argument("path", nargs="+")
    parser.add_argument("--flag", action="append", default=[])
    parser.add_argument("--format", choices=("text", "json"), default="text")
    ns = parser.parse_args(argv)
    key = compute_cache_key(ns.path, flags=ns.flag)
    if ns.format == "json":
        print(json.dumps(key.__dict__, indent=2, sort_keys=True))
    else:
        print(key.digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
