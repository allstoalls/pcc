#!/usr/bin/env python3
"""List or remove ignored compiler outputs; source, environments and run evidence stay intact."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.file_lock import exclusive_file_lock

_OUTPUT_SUFFIXES = {".o", ".a", ".so", ".dylib", ".dll", ".exe", ".ll", ".s", ".bc", ".bcode", ".pco", ".pidx"}
_STAGE_NAMES = {"pcc1", "pcc2", "pcc3", "pcc1.exe", "pcc2.exe", "pcc3.exe"}
_RUN_MARKERS = {".pcc-run-owner.json", "result.json", "receipt.json", "matrix-receipt.json"}


def artifact_plan(root: Path, scope: str = "all") -> list[Path]:
    root = root.resolve()
    output = subprocess.check_output(
        ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "-z"],
        cwd=root, timeout=30,
    )
    rows = []
    for raw in output.split(b"\0"):
        if not raw:
            continue
        relative = Path(raw.decode("utf-8", "surrogateescape"))
        parts = relative.parts
        if not parts:
            continue
        kind = ""
        if parts[0] == "build" or parts[0].startswith("build_"):
            kind = "build"
        elif len(parts) >= 3 and parts[:2] == ("pcc", "runtime"):
            if parts[2].startswith(("build_", "libpy_runtime")):
                kind = "runtime"
        elif len(parts) == 1:
            kind = "root"
        if not kind or scope not in ("all", kind):
            continue
        path = root / relative
        if path.is_symlink() or not path.is_file():
            continue
        if path.suffix not in _OUTPUT_SUFFIXES and path.name not in _STAGE_NAMES:
            continue
        # Frozen sources and durable experiment/qualification outputs retain
        # their artifacts. They need explicit lifecycle handling by their owner.
        if any(part.startswith(("frozen", "snapshot")) for part in parts):
            continue
        # A read-only directory was frozen by its owner, the same lifecycle.
        if not os.access(path.parent, os.W_OK):
            continue
        current = path.parent
        protected = False
        while current != root:
            if any((current / marker).exists() for marker in _RUN_MARKERS):
                protected = True
                break
            current = current.parent
        if not protected:
            rows.append(path)
    return sorted(rows)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=("all", "build", "runtime", "root"), default="all")
    parser.add_argument("--apply", action="store_true", help="remove the listed ignored output files")
    args = parser.parse_args(argv)
    guard = exclusive_file_lock(ROOT / "build" / ".pcc-performance.lock", blocking=False)
    try:
        with guard:
            paths = artifact_plan(ROOT, args.scope)
            result = {"mode": "remove" if args.apply else "list", "scope": args.scope,
                      "files": [str(path.relative_to(ROOT)) for path in paths],
                      "bytes": sum(path.stat().st_size for path in paths)}
            if args.apply:
                for path in paths:
                    path.unlink()
            print(json.dumps(result, indent=2))
    except BlockingIOError:
        parser.error("a compiler/performance run holds the repository build lock")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
