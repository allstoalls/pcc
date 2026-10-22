#!/usr/bin/env python3
"""Standalone OFF/ON probes for the production Stage1 source closure.

Runs selected modules through standalone codegen in the requested scaffold
modes and reports per-module fallback counts. Source discovery uses the
configured production Stage1 path; standalone emissions do not reproduce
its closed-world export context. Modules that hit
``ScaffoldUnsupportedError`` are reported as failed (the error itself
names the missing method/symbol — that's the Phase-3+ migration TODO).

Usage:
    env -u LC_ALL uv run python scripts/probe_stage1_closure_on_mode.py
    env -u LC_ALL uv run python scripts/probe_stage1_closure_on_mode.py \
        --module pcc.frontends.python.type_infer --mode off --emit-ir-dir build/probe

Without options, retain the all-module OFF/ON comparison and diagnostic exit
status. Filtered/artifact runs return 1 if any requested probe fails. An output
directory receives exact IR (or error details) and a source-hashed receipt;
these are standalone emissions, not contextual or native execution proof.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import traceback


def _tightened_closure(entry: str):
    from scripts.probe_stage1_closure import _tightened_closure as select

    return select(entry)


def _compile_standalone(source: str, src: str, mod: str, mode: str) -> str:
    from pcc.frontends.python import type_infer as _type_infer
    from pcc.frontends.python.codegen import layer1 as _layer1
    from pcc.frontends.python.py_lift import parse_and_lift

    ast_mod = parse_and_lift(source, src, mod)
    typed = _type_infer.infer_module(ast_mod)
    codegen = _layer1.L1CodeGen(
        typed, emit_cpy_main_exitcode=False, ir_scaffold_mode=mode,
    )
    return str(codegen.generate(typed))


def _scan_receipt(ir_text: str) -> dict:
    from scripts.probe_fallback_categories import _scan

    stats = _scan(ir_text)
    # The canonical parser's tuple-key Counter needs an explicit JSON shape.
    stats["action_target_pairs"] = [
        {"action": action, "target": target, "count": count}
        for (action, target), count in sorted(stats["action_target_pairs"].items())
    ]
    return stats


def _print_summary(results) -> None:
    print(f"{'module':<45} {'OFF':>6} {'ON':>6} {'Δ':>7}  status")
    print("-" * 90)
    sum_off = sum_on = 0
    migrated_count = 0
    for mod, n_off, n_on, off_status, on_status in results:
        if n_off is None or n_off < 0:
            print(f"{mod:<45} {'?':>6} {'?':>6} {'?':>7}  {off_status}")
            continue
        if n_on is None:
            # ON failed: report stop reason
            print(
                f"{mod:<45} {n_off:>6} {'STOP':>6} {'?':>7}  "
                + on_status[:60]
            )
            continue
        sum_off += n_off
        sum_on += n_on
        migrated_count += 1
        delta = n_off - n_on
        pct = (delta / n_off * 100) if n_off > 0 else 0.0
        print(
            f"{mod:<45} {n_off:>6} {n_on:>6} {delta:>7}  "
            f"({pct:.0f}%)"
        )

    print("-" * 90)
    if sum_off > 0:
        print(
            f"{'TOTAL (modules with ON success)':<45} "
            f"{sum_off:>6} {sum_on:>6} {sum_off - sum_on:>7}  "
            f"({(1 - sum_on / sum_off) * 100:.1f}%)"
        )
        print(
            f"migrated cleanly: {migrated_count}/{len(results)} modules"
        )


def main(argv=None) -> int:
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", action="append", default=[],
                        help="exact production-closure module name; repeat to select several")
    parser.add_argument("--mode", choices=("off", "on", "both"), default="both")
    parser.add_argument("--emit-ir-dir", type=Path,
                        help="write per-module/mode .ll or .error.txt and receipt.json")
    args = parser.parse_args(argv)
    entry = root / "pcc" / "__main__.py"
    srcs, mods = _tightened_closure(str(entry))
    if len(srcs) != len(mods) or len(set(mods)) != len(mods):
        parser.error("production closure has mismatched or duplicate module entries")
    unknown = sorted(set(args.module) - set(mods))
    if unknown:
        parser.error("module outside production closure: " + ", ".join(unknown))
    selected = [(src, mod) for src, mod in zip(srcs, mods)
                if not args.module or mod in args.module]
    modes = ("off", "on") if args.mode == "both" else (args.mode,)
    artifact_dir = args.emit_ir_dir.resolve() if args.emit_ir_dir is not None else None
    if artifact_dir is not None:
        artifact_dir.mkdir(parents=True, exist_ok=True)
    print(f"closure: {len(srcs)} files (production discovery; standalone codegen)", flush=True)
    print()
    records = []
    results = []
    for src, mod in selected:
        source_path = Path(src).resolve()
        outcomes = {}
        for mode in modes:
            record = {"module": mod, "mode": mode, "source_path": str(source_path),
                      "source_sha256": None, "status": "ERROR"}
            try:
                source_bytes = source_path.read_bytes()
                record["source_sha256"] = hashlib.sha256(source_bytes).hexdigest()
                ir_text = _compile_standalone(source_bytes.decode("utf-8"), str(source_path), mod, mode)
                ir_bytes = ir_text.encode("utf-8")
                if artifact_dir is not None:
                    ir_path = artifact_dir / (mod + "." + mode + ".ll")
                    ir_path.write_bytes(ir_bytes)
                    record["ir_path"] = str(ir_path)
                    record["ir_sha256"] = hashlib.sha256(ir_bytes).hexdigest()
                record["scan"] = _scan_receipt(ir_text)
                record["status"] = "OK"
                outcomes[mode] = (record["scan"]["total"], "ok")
            except Exception as exc:
                detail = type(exc).__name__ + ": " + (str(exc) or type(exc).__name__)
                record["error"] = detail
                stop = mode == "on" and type(exc).__name__ == "ScaffoldUnsupportedError"
                outcomes[mode] = (None if mode == "on" else -1,
                                  mode.upper() + (" STOP: " if stop else " FAIL: ") + detail)
                if artifact_dir is not None:
                    error_path = artifact_dir / (mod + "." + mode + ".error.txt")
                    error_path.write_text(
                        f"module: {mod}\nmode: {mode}\nsource: {source_path}\n"
                        f"source_sha256: {record['source_sha256']}\n{detail}\n",
                        encoding="utf-8",
                    )
                    record["error_path"] = str(error_path)
                if stop and os.environ.get("PCC_PROBE_VERBOSE"):
                    print(f"\n--- {mod} traceback ---\n{traceback.format_exc()}", flush=True)
            records.append(record)
        off, off_status = outcomes.get("off", (None, "OFF not selected"))
        on, on_status = outcomes.get("on", (None, "ON not selected"))
        results.append((mod, off, on, off_status, on_status))
    if len(modes) == 2:
        _print_summary(results)
    else:
        for record in records:
            count = record.get("scan", {}).get("total", "?")
            print(f"{record['module']:<45} {record['mode'].upper():>3} {count:>6}  "
                  + record.get("error", "ok"), flush=True)
    failed = any(record["status"] != "OK" for record in records)
    if artifact_dir is not None:
        receipt = {"schema": "pcc.standalone-closure-probe.v1",
                   "scope": "standalone_frontend_ir", "status": "ERROR" if failed else "OK",
                   "tool_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                   "entry": str(entry), "closure_module_count": len(mods),
                   "selected_modules": [mod for _, mod in selected], "modes": list(modes),
                   "results": records}
        receipt_path = artifact_dir / "receipt.json"
        receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print("receipt: " + str(receipt_path), flush=True)
    explicit_probe = bool(args.module or artifact_dir is not None or args.mode != "both")
    return 1 if failed and explicit_probe else 0


if __name__ == "__main__":
    sys.exit(main())
