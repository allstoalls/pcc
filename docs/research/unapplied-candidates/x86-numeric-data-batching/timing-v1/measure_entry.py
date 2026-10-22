"""One unhooked ordinary object entry per process; fixed four-slot comparison."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import sys
import time
import traceback


SLOTS = ("b1", "c1", "c2", "b2")


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, value):
    with path.open("w") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n"); stream.flush(); os.fsync(stream.fileno())


def stop_after_first_pair(baseline, candidate):
    assert baseline["slot"] == "b1" and candidate["slot"] == "c1"
    assert baseline["status"] == candidate["status"] == "PASS"
    return candidate["wall_ns"] * 100 > baseline["wall_ns"] * 110


def compare_rows(rows, manifest, manifest_sha256):
    """Pure result arithmetic, called outside all entry timing intervals."""
    assert len(rows) == 4 and [row["slot"] for row in rows] == list(SLOTS)
    assert manifest["thresholds"] == {
        "minimum_mean_wall_reduction_percent": 5,
        "both_order_pairs_must_improve": True,
        "mean_reduction_must_exceed_baseline_spread": True,
        "first_candidate_wall_regression_stop_percent": 10,
    }
    for row in rows:
        source = manifest["sources"]["baseline" if row["slot"].startswith("b") else "candidate"]
        assert row["status"] == "PASS" and row["manifest_sha256"] == manifest_sha256
        assert row["source_inventory_sha256"] == source["inventory_sha256"]
        assert row["codegen_checksum"] == source["codegen_checksum"]
        assert row["assembly_sha256"] == manifest["inputs"]["assembly"]["sha256"]
        assert row["object"]["sha256"] == manifest["inputs"]["reference_object"]["sha256"]
        assert row["byte_and_complete_decoded_contract_equal"] is True
        assert row["observers_installed"] is False
        assert row["wall_ns"] > 0 and row["cpu_ns"] > 0
    b1, c1, c2, b2 = rows
    bsum = b1["wall_ns"] + b2["wall_ns"]
    csum = c1["wall_ns"] + c2["wall_ns"]
    reduction = bsum - csum
    spread_twice = 2 * abs(b1["wall_ns"] - b2["wall_ns"])
    criteria = {
        "minimum_mean_wall_reduction": reduction * 100 >= 5 * bsum,
        "forward_order_improves": c1["wall_ns"] < b1["wall_ns"],
        "reverse_order_improves": c2["wall_ns"] < b2["wall_ns"],
        "mean_reduction_exceeds_baseline_spread": reduction > spread_twice,
        "no_first_pair_stop_loss": not stop_after_first_pair(b1, c1),
    }
    def summary(key):
        baseline = (b1[key] + b2[key]) / 2
        candidate = (c1[key] + c2[key]) / 2
        return {"baseline_mean": baseline, "candidate_mean": candidate,
                "mean_reduction_percent": 100 * (baseline - candidate) / baseline}
    return {
        "schema": "pcc.numeric-batching-whole-entry-comparison.v1",
        "status": "PASS", "sequence": list(SLOTS), "criteria": criteria,
        "decision": ("WHOLE_ENTRY_THRESHOLD_MET_REVIEW_ONLY" if all(criteria.values())
                     else "HOLD_NO_MATERIAL_ENTRY_BENEFIT_ESTABLISHED"),
        "wall_ns": summary("wall_ns"), "cpu_ns": summary("cpu_ns"),
        "through_entry_high_water_rss_bytes": summary("rss_after_entry_bytes"),
        "forward_wall_reduction_percent": 100 * (b1["wall_ns"] - c1["wall_ns"]) / b1["wall_ns"],
        "reverse_wall_reduction_percent": 100 * (b2["wall_ns"] - c2["wall_ns"]) / b2["wall_ns"],
        "baseline_wall_spread_percent": 100 * spread_twice / bsum,
        "all_four_complete_objects_equal": True,
        "limits": "Two samples per arm, one historical host COFF corpus; no current-CI, native-pcc1 or whole-Stage1 speedup claim.",
    }


def main():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--slot", choices=SLOTS, required=True)
    for option in ("source", "assembly", "reference-object", "corpus-result",
                   "corpus-qualification", "output"):
        parser.add_argument("--" + option, type=Path, required=True)
    args = parser.parse_args()
    packet = Path(__file__).resolve().parent
    manifest_path = packet / "manifest.json"
    assert sha(manifest_path) == args.manifest_sha256
    manifest = json.loads(manifest_path.read_text())
    arm = "baseline" if args.slot.startswith("b") else "candidate"
    source_spec = manifest["sources"][arm]
    source = args.source.resolve(strict=True)
    assert Path.cwd().resolve() == source
    assert not sys.flags.optimize and sys.platform == "linux"
    assert resource.getrlimit(resource.RLIMIT_AS) == (4294967296, 4294967296)
    assert resource.getrlimit(resource.RLIMIT_NPROC) == (0, 0)
    assert not any(name == "pcc" or name.startswith("pcc.") for name in sys.modules)
    for key in ("PCC_DISABLE_ROADMAP_DEEPWIRE", "PCC_NO_AUTO_PCC1",
                "PCC_TEST_NO_NATIVE_PROVISIONING"):
        assert os.environ.get(key) == "1"
    reservation = {key: os.environ[key] for key in
                   ("PCC_WORKER_TREE_STATE_PATH", "PCC_WORKER_TREE_BUDGET_BYTES")}
    assert reservation["PCC_WORKER_TREE_STATE_PATH"]
    assert reservation["PCC_WORKER_TREE_BUDGET_BYTES"] == "4294967296"
    source_manifest = source.parent / "source-manifest.json"
    assert sha(source_manifest) == source_spec["inventory_sha256"]
    inventory_document = json.loads(source_manifest.read_text())
    assert inventory_document["candidate_pcc_git_tree"] == source_spec["pcc_tree"]
    inventory = {row["path"]: row for row in inventory_document["files"]}
    paths = {name: getattr(args, name).resolve(strict=True) for name in
             ("assembly", "reference_object", "corpus_result", "corpus_qualification")}
    output = args.output.resolve()
    assert not output.exists() and output.parent.is_dir()
    assert all(not output.is_relative_to(root) and not root.is_relative_to(output)
               for root in (source, packet))
    assert all(not path.is_relative_to(output) for path in paths.values())
    assert not source_manifest.is_relative_to(output)

    def check_pins():
        assert sha(manifest_path) == args.manifest_sha256
        assert sha(Path(__file__)) == manifest["driver_sha256"]
        assert sha(packet / "README.md") == manifest["readme_sha256"]
        assert sha(source_manifest) == source_spec["inventory_sha256"]
        assert sha(source / "pcc/backend/x86_64_asm_driver.py") == source_spec["assembler_sha256"]
        for name, path in paths.items():
            row = manifest["inputs"][name]
            assert path.stat().st_size == row["bytes"] and sha(path) == row["sha256"], name

    def check_imports():
        imported = []
        for name, module in sorted(sys.modules.items()):
            if name != "pcc" and not name.startswith("pcc."):
                continue
            path = Path(module.__file__).resolve(strict=True)
            assert path.is_relative_to(source), name
            relative = path.relative_to(source).as_posix()
            assert relative in inventory and sha(path) == inventory[relative]["sha256"]
            imported.append(relative)
        assert imported
        return sorted(set(imported))

    check_pins()
    prior = json.loads(paths["corpus_result"].read_text())
    assert prior["status"] == "PASS" and prior["decision"] == "MECHANISM_THRESHOLD_MET_REVIEW_ONLY"
    assert prior["byte_and_complete_decoded_contract_equal"] is True
    assert prior["observers_restored"] is True
    assert prior["assembly_sha256"] == manifest["inputs"]["assembly"]["sha256"]
    assert prior["object"]["sha256"] == manifest["inputs"]["reference_object"]["sha256"]
    seal = json.loads(paths["corpus_qualification"].read_text())
    assert seal["status"] == "PASS"
    output.mkdir()
    result = {
        "schema": "pcc.numeric-batching-unhooked-entry.v1", "status": "RUNNING",
        "slot": args.slot, "arm": arm, "manifest_sha256": args.manifest_sha256,
        "source_inventory_sha256": source_spec["inventory_sha256"],
        "assembly_sha256": manifest["inputs"]["assembly"]["sha256"],
        "observers_installed": False,
    }
    save(output / "result.json", result)
    try:
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(source))
        from pcc.backend import coff_x86_64 as coff
        from pcc.backend import x86_64_asm_driver as asm
        from pcc.backend.target_objects import encode_assembly_object
        from pcc.tools.runtime_archive_provenance import codegen_checksum
        result["codegen_checksum"] = codegen_checksum()
        assert result["codegen_checksum"] == source_spec["codegen_checksum"]
        assert getattr(asm, "_NUMERIC_DATA_BATCH_BYTES", None) == (8192 if arm == "candidate" else None)
        result["imported_source_files_before"] = check_imports()
        assembly = paths["assembly"].read_text(encoding="utf-8")
        expected_bytes = paths["reference_object"].read_bytes()
        result["rss_before_entry_bytes"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
        # The entire ordinary production object entry is inside this interval.
        # No hooks, per-item counts, decode, hashing, file IO or source checking.
        wall_start = time.perf_counter_ns()
        cpu_start = time.process_time_ns()
        encoded = encode_assembly_object(assembly, manifest["target"])
        result["cpu_ns"] = time.process_time_ns() - cpu_start
        result["wall_ns"] = time.perf_counter_ns() - wall_start
        result["rss_after_entry_bytes"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
        assert result["wall_ns"] > 0 and result["cpu_ns"] > 0
        assert type(encoded) is bytes and encoded == expected_bytes
        assert hashlib.sha256(encoded).hexdigest() == manifest["inputs"]["reference_object"]["sha256"]
        assert len(encoded) == manifest["inputs"]["reference_object"]["bytes"]
        assert coff.parse_object(encoded) == coff.parse_object(expected_bytes)
        result["byte_and_complete_decoded_contract_equal"] = True
        path = output / "module.obj"
        with path.open("xb") as stream:
            stream.write(encoded); stream.flush(); os.fsync(stream.fileno())
        result["object"] = {"bytes": len(encoded), "sha256": sha(path)}
        result["imported_source_files_after"] = check_imports()
        check_pins()
        assert all(os.environ.get(key) == value for key, value in reservation.items())
        result["rss_scope"] = "Process high-water through entry, including imports/loading; not isolated phase allocation or whole-guard RSS."
        result["timing_scope"] = "Whole ordinary COFF object entry, including SEH rewrite, parse, layout, emission, schema checks, adaptation and serialization."
        result["status"] = "PASS"
    except BaseException:
        result["status"] = "FAIL"; result["error"] = traceback.format_exc()
        save(output / "result.json", result)
        raise
    save(output / "result.json", result)
    print(json.dumps({key: result[key] for key in
                     ("status", "slot", "wall_ns", "cpu_ns", "rss_after_entry_bytes", "object")},
                     sort_keys=True))


if __name__ == "__main__":
    main()
