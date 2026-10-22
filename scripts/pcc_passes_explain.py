#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Read observed pass telemetry; defaults are not execution evidence.")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--telemetry", type=Path, help="existing PCC_PYTHON_IR_PASS_TELEMETRY_PATH JSONL")
    ns = parser.parse_args(argv)
    records = []
    identity = None
    if ns.telemetry:
        raw = ns.telemetry.read_bytes()
        identity = {"path": str(ns.telemetry.resolve()), "sha256": hashlib.sha256(raw).hexdigest()}
        for number, line in enumerate(raw.decode().splitlines(), 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if not isinstance(record, dict) or "event" not in record:
                    raise ValueError("expected telemetry event object")
                if record["event"] == "pass":
                    if not all(isinstance(record.get(key), str) for key in ("pass", "status", "module")):
                        raise ValueError("pass event needs pass, status and module")
                    records.append(record)
            except ValueError as exc:
                parser.error(f"{ns.telemetry}:{number}: {exc}")
    data = {"schema": "pcc.diagnostics.pass_explain.v1", "status": "OBSERVED" if records else "UNKNOWN",
            "source": identity, "passes": records,
            "ran": [r["pass"] for r in records if r["status"] == "run"],
            "skipped": [r["pass"] for r in records if r["status"].startswith("skip")],
            "limitation": "Telemetry reports this recorded route only; byte counts do not prove useful IR effects or native ownership."}
    if ns.format == "json":
        print(json.dumps(data, indent=2, sort_keys=True))
    elif records:
        for record in records:
            print(f"{record['module']}: {record['pass']}: {record['status']}: "
                  f"{record.get('elapsed_ms', 'unknown')} ms")
    else:
        print("UNKNOWN: no observed pass events; supply --telemetry. Defaults do not prove passes ran.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
