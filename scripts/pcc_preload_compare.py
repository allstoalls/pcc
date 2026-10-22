#!/usr/bin/env python3
"""Compare complete class-preload indexes; this is not a speed benchmark."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
PRELOAD_FUNCTIONS = (
    "build_unique_external_class_preload",
    "build_unique_external_class_preload_index",
)


class PreloadCompareError(ValueError):
    """A comparison cannot establish a source-stable exact result."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _index_bytes(index) -> bytes:
    # Insertion order is part of the contract: sorting hides root/key drift.
    # Keep the original real-wire receipt's ASCII/compact JSON spelling.
    return json.dumps(index, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def extract_baseline(source: str, filename: str, current_globals: dict) -> dict:
    """Link only the two baseline definitions against unchanged current globals."""
    module = ast.parse(source, filename=filename)
    definitions = []
    for name in PRELOAD_FUNCTIONS:
        matches = [
            node for node in module.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ]
        if len(matches) != 1:
            raise PreloadCompareError(
                f"baseline requires exactly one {name} definition; found {len(matches)}"
            )
        if not isinstance(matches[0], ast.FunctionDef):
            raise PreloadCompareError(f"baseline {name} must be a synchronous function")
        definitions.append(matches[0])
    namespace = dict(current_globals)
    selected = ast.Module(body=definitions, type_ignores=[])
    exec(compile(selected, filename, "exec"), namespace)
    return namespace


def _require_unchanged(paths: dict[str, Path], hashes: dict[str, str]) -> None:
    for role, path in paths.items():
        try:
            unchanged = _sha256(path) == hashes[role]
        except OSError as exc:
            raise PreloadCompareError(f"{role} became unavailable during comparison: {path}") from exc
        if not unchanged:
            raise PreloadCompareError(f"{role} changed during comparison: {path}")


def _counts(index) -> dict[str, int]:
    roots = index["roots"]
    return {
        "types": len(index["types"]),
        "base_keys": len(index["base_keys"]),
        "roots": len(roots),
        "nonempty_roots": sum(bool(drop or put) for drop, put in roots.values()),
    }


def run(baseline_source: Path, exports_wire: Path, output: Path) -> dict:
    """Write one fresh receipt; DIFFERENT is a failed comparison, not acceptance."""
    if os.path.lexists(output):
        raise PreloadCompareError(f"refusing existing output: {output}")
    paths = {
        "baseline_source": Path(baseline_source).resolve(),
        "candidate_source": REPO_ROOT / "pcc/frontends/python/type_infer.py",
        "exports_wire": Path(exports_wire).resolve(),
    }
    hashes = {role: _sha256(path) for role, path in paths.items()}
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from pcc.frontends.python import type_infer
    from pcc.frontends.python.pipeline_exports import _read_native_exports_wire

    baseline = extract_baseline(
        paths["baseline_source"].read_text(encoding="utf-8"),
        str(paths["baseline_source"]), vars(type_infer),
    )
    exports, _derived = _read_native_exports_wire(str(paths["exports_wire"]))
    baseline_index = baseline[PRELOAD_FUNCTIONS[1]](exports)
    baseline_bytes = _index_bytes(baseline_index)
    candidate_index = type_infer.build_unique_external_class_preload_index(exports)
    candidate_bytes = _index_bytes(candidate_index)
    semantic_equal = baseline_index == candidate_index
    ordered_bytes_equal = baseline_bytes == candidate_bytes
    _require_unchanged(paths, hashes)
    result = {
        "schema": "pcc.preload-compare.v1",
        "status": "EXACT" if semantic_equal and ordered_bytes_equal else "DIFFERENT",
        "mode": "host semantic differential; no speed or pcc1 claim",
        "baseline_source": str(paths["baseline_source"]),
        "baseline_source_sha256": hashes["baseline_source"],
        "candidate_source": str(paths["candidate_source"]),
        "candidate_source_sha256": hashes["candidate_source"],
        "exports_wire": str(paths["exports_wire"]),
        "exports_sha256": hashes["exports_wire"],
        "modules": len(exports),
        **_counts(candidate_index),
        "baseline_counts": _counts(baseline_index),
        "semantic_equal": semantic_equal,
        "ordered_bytes_equal": ordered_bytes_equal,
        "index_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
        "index_bytes": len(candidate_bytes),
        "baseline_index_sha256": hashlib.sha256(baseline_bytes).hexdigest(),
        "baseline_index_bytes": len(baseline_bytes),
    }
    # Exclusive creation also preserves an artifact created after the initial
    # check. Validation failures publish nothing; mismatches retain a denial.
    with Path(output).open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(result, indent=2) + "\n")
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-source", required=True, type=Path)
    parser.add_argument("--exports-wire", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        result = run(args.baseline_source, args.exports_wire, args.out)
    except (OSError, ValueError, SyntaxError) as exc:
        print(f"pcc preload comparison failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "EXACT" else 2


if __name__ == "__main__":
    raise SystemExit(main())
