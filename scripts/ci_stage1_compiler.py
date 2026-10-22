#!/usr/bin/env python3
"""Hand this checkout's passing Stage1 pcc1 to the wheel build.

``hatch_build.py`` self-compiles pcc1 under a 10-minute budget when
``PCC_BUILD_PCC1`` is unset, which cannot hold a 35-60 minute Stage1. A wheel
job therefore bundles the exact pcc1 that ``ci_worker_size_prior_gate.py
--phase stage1`` built and recorded, after checking that the receipt passed,
names this clean commit and still matches the binary's bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.bootstrap_platform import source_identity  # noqa: E402


def stage1_compiler(receipt_path: Path, current_source: dict) -> Path:
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("phase") != "stage1" or receipt.get("status") != "PASS":
        raise RuntimeError(f"{receipt_path} does not record a passing Stage1")
    source = receipt["source"]
    if (
        source.get("clean_commit") is not True
        or source.get("commit") != current_source["commit"]
        or source.get("source_sha256") != current_source["source_sha256"]
    ):
        raise RuntimeError(f"{receipt_path} does not identify this clean checkout")
    compiler = receipt["compiler"]
    path = Path(compiler["path"])
    if not path.is_absolute() or not path.is_file():
        raise RuntimeError(f"recorded Stage1 pcc1 is not an absolute file: {path}")
    with path.open("rb") as stream:
        observed = hashlib.file_digest(stream, "sha256").hexdigest()
    if observed != compiler["sha256"]:
        raise RuntimeError(f"{path} changed after Stage1 recorded it")
    return path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", type=Path, default=Path("build/worker-prior-gate/stage1.json"))
    args = parser.parse_args(argv)
    path = stage1_compiler(args.receipt, source_identity(allow_dirty=True))
    line = f"PCC_BUILD_PCC1={path}\n"
    print(line, end="")
    github_env = os.environ.get("GITHUB_ENV")
    if github_env:
        with open(github_env, "a", encoding="utf-8") as stream:
            stream.write(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
