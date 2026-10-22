"""Plan stable whole-file regressions; never mistake selection for execution.

Run this for each frozen source with its combined source manifest. Feed the
result's whole_files into bounded per-file execution, retaining every native
node in a separate admitted-runtime lane. Pass the previous plan to preserve
affected additions across snapshots. This command does not run the corpus.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


BASELINE = Path(__file__).with_name("snapshot_baseline.json")


def _whole_file(value: str) -> str:
    path = Path(value)
    if (path.is_absolute() or ".." in path.parts or "::" in value
            or path.parts[:1] != ("tests",) or path.suffix != ".py"):
        raise ValueError("expected a repository-relative whole test file: " + value)
    return path.as_posix()


def select_files(baseline, gateway_files, *, affected=(), previous=None):
    """Selection only grows; missing or renamed files require explicit review."""
    selected = {"pcc": set(baseline["whole_files"]),
                "pcc-gateway": set(gateway_files)}
    for repository, files in (previous or {}).get("whole_files", {}).items():
        if repository not in selected:
            raise ValueError("unsupported previous repository: " + repository)
        selected[repository].update(files)
    for repository, file in affected:
        if repository not in selected:
            raise ValueError("unsupported affected repository: " + repository)
        selected[repository].add(file)
    selected["pcc"].update(baseline["required_native"]["pcc"])
    return {repository: sorted({_whole_file(file) for file in files})
            for repository, files in selected.items()}


def make_plan(core, gateway, source_manifest, *, affected=(), previous=None):
    baseline_bytes = BASELINE.read_bytes()
    baseline = json.loads(baseline_bytes)
    source_bytes = source_manifest.read_bytes()
    identities = json.loads(source_bytes)
    roots = {"pcc": core, "pcc-gateway": gateway}
    gateway_files = [path.relative_to(gateway).as_posix()
                     for path in (gateway / "tests").rglob("*.py")
                     if path.name.startswith("test_") or path.name.endswith("_test.py")]
    if not gateway_files:
        raise ValueError("gateway whole test scope is empty")
    files = select_files(baseline, gateway_files, affected=affected, previous=previous)
    test_hashes = {}
    for repository, selected in files.items():
        test_hashes[repository] = {}
        for file in selected:
            actual = hashlib.sha256((roots[repository] / file).read_bytes()).hexdigest()
            expected = identities[repository].get(file)
            if not expected or expected["sha256"] != actual:
                raise ValueError("selected test is outside the frozen source: "
                                 + repository + "/" + file)
            test_hashes[repository][file] = actual
    return {
        "schema": "pcc.snapshot-regression-selection.v1",
        "source_manifest_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "baseline_sha256": hashlib.sha256(baseline_bytes).hexdigest(),
        "whole_files": files,
        "test_sha256": test_hashes,
        "required_native": baseline["required_native"],
        "known_native_or_mixed_files": baseline["known_native_or_mixed_files"],
        "lane_assignment": "PENDING_WHOLE_FILE_COLLECTION: classify every node from its actual fixtures and executed boundary; a file outside known_native_or_mixed_files is not automatically host-only.",
        "execution_state": "UNRUN",
        "collection_is_execution": False,
        "policy": {
            "selection": "Baseline plus previous additions plus affected whole files; no -k narrowing.",
            "host": "Bounded per-file -x execution with durable node receipts and a 512 MiB tree cap.",
            "native": "All native nodes, including whole async and gateway scope, need actual execution with source-matched admitted runtime/compiler artifacts.",
            "outcomes": "Report PASS, FAIL, SKIP, DEFERRED, INFRA_ERROR and UNRUN per node; continue untouched nodes after -x. Collection or host success never closes native execution.",
            "qualification": "Require complete inventories and source-stable receipts. Skips, deferred, infrastructure errors and unrun nodes remain incomplete; a green subset is not whole qualification.",
            "heavy_corpus": "Full native corpus and bootstrap/five-GC qualification are separately scheduled; this plan does not launch them under the host cap.",
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core", type=Path, required=True)
    parser.add_argument("--gateway", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--previous-plan", type=Path)
    parser.add_argument("--affected", action="append", default=[],
                        metavar="REPOSITORY:tests/path.py")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    affected = [value.split(":", 1) for value in args.affected]
    if any(len(value) != 2 for value in affected):
        parser.error("--affected requires REPOSITORY:tests/path.py")
    previous = json.loads(args.previous_plan.read_text()) if args.previous_plan else None
    plan = make_plan(args.core.resolve(strict=True), args.gateway.resolve(strict=True),
                     args.source_manifest, affected=affected, previous=previous)
    args.output.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    print("Planned " + ", ".join(repository + ": " + str(len(files)) + " whole files"
                               for repository, files in plan["whole_files"].items())
          + "; execution remains UNRUN.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
