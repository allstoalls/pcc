#!/usr/bin/env python3
"""Audit scaffold signatures or observe retained host codegen worker replays.

``worker`` uses the existing receipt-bound replay preparer and process-tree
watchdog, then observes scaffold dispatches in that worker. Unknown proofs are
reported as gaps, never promoted from an annotation or matching spelling.
The observation does not itself certify production emission or proof coverage.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def compiler_source_identity() -> str:
    """Detect implementation drift during a diagnostic replay.

    Run this tool from a frozen checkout. The comparison is an additional
    tripwire, not permission to modify an active worker's inputs.
    """
    value = hashlib.sha256()
    for path in sorted((ROOT / "pcc").rglob("*.py")):
        parts = path.relative_to(ROOT).parts
        if any(part == "__pycache__" or part.startswith("build") for part in parts):
            continue
        value.update(str(path.relative_to(ROOT)).encode("utf-8"))
        value.update(bytes.fromhex(digest(path)))
    for name in (
        "pcc_scaffold_decisions.py",
        "replay_pcc_codegen_worker.py",
        "run_process_tree_sample.py",
    ):
        value.update(name.encode("utf-8"))
        value.update(bytes.fromhex(digest(ROOT / "scripts" / name)))
    return value.hexdigest()


def observe_worker(manifest: Path, output: Path) -> int:
    from pcc.frontends.python.pipeline_frontend_workers import read_worker_manifest

    selected = read_worker_manifest(str(manifest))
    if selected["job_kind"] != "codegen":
        raise ValueError("scaffold observation requires a codegen worker")
    if selected["ir_scaffold_mode"] != "on" or selected["libpython_mode"] != "off":
        raise ValueError("scaffold baseline requires ir-scaffold=on and libpython=off")
    from pcc.ir import compat, ir

    if compat.ir is not ir:
        raise ValueError("compat.ir is not the owned provider in this observer")
    from pcc.frontends.python.pipeline import run_python_multi_codegen_worker
    from pcc.tools.ir_scaffold_observer import (
        ScaffoldDecisionRecorder,
        observe_scaffold_decisions,
    )

    with output.open("x", encoding="utf-8") as stream:

        def write_record(row):
            stream.write(json.dumps(row, sort_keys=True) + "\n")
            stream.flush()

        recorder = ScaffoldDecisionRecorder(write_record=write_record)
        with observe_scaffold_decisions(recorder):
            status = run_python_multi_codegen_worker(str(manifest))
    summary = recorder.summary()
    summary["worker_returncode"] = status
    output.with_suffix(".summary.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n"
    )
    return status


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="action", required=True)
    signatures = subcommands.add_parser("signatures")
    signatures.add_argument("--provider", type=Path)
    signatures.add_argument("--output", type=Path, required=True)
    worker = subcommands.add_parser("worker")
    worker.add_argument("--manifest", type=Path, action="append", required=True)
    worker.add_argument("--stage-receipt", type=Path, required=True)
    worker.add_argument("--output-dir", type=Path, required=True)
    worker.add_argument("--timeout", type=float, default=900)
    worker.add_argument("--max-tree-rss-bytes", type=int, default=8 * 1024**3)
    worker.add_argument("--baseline", type=Path)
    child = subcommands.add_parser("_observe-worker")
    child.add_argument("--manifest", type=Path, required=True)
    child.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.action == "signatures":
        from pcc.tools.ir_scaffold_signatures import (
            current_signature_issues,
            definition_signatures,
        )

        provider = args.provider or ROOT / "pcc/ir/ir.py"
        issues = current_signature_issues(provider)
        payload = {
            "schema": "pcc.scaffold-signature-audit.v1",
            "provider": str(provider.resolve()),
            "provider_sha256": digest(provider),
            "issues": [asdict(issue) for issue in issues],
            "definitions": {
                key: [asdict(parameter) for parameter in value]
                for key, value in definition_signatures(provider.read_text()).items()
                if key.startswith(("IRBuilder.", "IRBuilder_", "scaffold_"))
            },
        }
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
        print(f"signature issues: {len(issues)}; receipt: {args.output}")
        return 1 if issues else 0
    if args.action == "_observe-worker":
        return observe_worker(args.manifest, args.output)

    from scripts.replay_pcc_frontends_c_codegen_worker import prepare_replay
    from pcc.tools.ir_scaffold_observer import compare_decision_records

    output_root = args.output_dir.resolve()
    output_root.mkdir(parents=True, exist_ok=False)
    source_before = compiler_source_identity()
    records = []
    receipts = []
    status = 0
    for index, manifest in enumerate(args.manifest):
        directory = output_root / f"worker-{index:04d}"
        _command, environment = prepare_replay(
            compiler=Path(sys.executable),
            manifest=manifest,
            stage_receipt=args.stage_receipt,
            output_dir=directory,
            native_object=0,
            host_source_root=ROOT,
        )
        decisions = directory / "decisions.jsonl"
        command = [
            sys.executable,
            str(ROOT / "scripts/run_process_tree_sample.py"),
            "--result",
            str(directory / "process.json"),
            "--samples",
            str(directory / "process.tsv"),
            "--stdout",
            str(directory / "worker.stdout"),
            "--stderr",
            str(directory / "worker.stderr"),
            "--cwd",
            str(ROOT),
            "--timeout",
            str(args.timeout),
            "--max-tree-rss-bytes",
            str(args.max_tree_rss_bytes),
            "--",
            sys.executable,
            "-P",
            str(Path(__file__).resolve()),
            "_observe-worker",
            "--manifest",
            str(directory / "worker.manifest"),
            "--output",
            str(decisions),
        ]
        status = subprocess.run(
            command, cwd=ROOT, env=environment, check=False
        ).returncode
        receipts.append(
            {
                "manifest": str(manifest.resolve()),
                "sha256": digest(manifest),
                "returncode": status,
            }
        )
        if decisions.is_file():
            records.extend(
                json.loads(line) for line in decisions.read_text().splitlines()
            )
        if status:
            break
    source_after = compiler_source_identity()
    if source_before != source_after:
        status = status or 2
    payload = {
        "schema": "pcc.scaffold-worker-audit.v1",
        "execution_owner": "host-cpython-observer",
        "requested_workers": len(args.manifest),
        "completed_workers": sum(item["returncode"] == 0 for item in receipts),
        "workers": receipts,
        "stage_receipt_sha256": digest(args.stage_receipt),
        "provider_sha256": digest(ROOT / "pcc/ir/ir.py"),
        "observer_sha256": digest(ROOT / "pcc/tools/ir_scaffold_observer.py"),
        "compiler_source_before": source_before,
        "compiler_source_after": source_after,
        "source_stable": source_before == source_after,
        "records": records,
        "returncode": status,
        "equivalence_proved": False,
    }
    if args.baseline is not None:
        baseline = json.loads(args.baseline.read_text())
        expected = [item["sha256"] for item in baseline["workers"]]
        if expected != [item["sha256"] for item in receipts]:
            raise ValueError("baseline has different worker input manifests")
        payload["differences"] = compare_decision_records(baseline["records"], records)
        payload["equivalence_proved"] = (
            status == 0 and bool(records) and not payload["differences"]
        )
    receipt = output_root / "audit.json"
    receipt.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    unproved = sum(row["resolution"] != "proven" for row in records)
    print(
        f"observed {len(records)} lowering decisions; {unproved} unproved; receipt: {receipt}"
    )
    if status:
        return status
    if args.baseline is not None and not payload["equivalence_proved"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
