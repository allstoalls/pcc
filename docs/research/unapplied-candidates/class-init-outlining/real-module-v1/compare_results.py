"""Compare two fresh structural results; never infer a speedup from counts."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def load_arm(root, expected_hash, arm, manifest, manifest_hash):
    assert sha(root / "result.json") == expected_hash
    result = json.loads((root / "result.json").read_text())
    assert result["status"] == "PASS" and result["arm"] == arm
    assert result["manifest_sha256"] == manifest_hash
    assert result["source_inventory_sha256"] == manifest["sources"][arm]["inventory_sha256"]
    assert result["input_sha256"] == manifest["shared_source_files"][manifest["input"]]
    assert result["target"] == manifest["target"]
    assert result["passes"] == manifest["passes"]
    assert result["effective_runtime_threads"] == manifest["runtime_threads"]
    assert result["owned_record_containers"] == result["import_source_checks"] == "PASS"
    for name, artifact in result["artifacts"].items():
        assert Path(name).name == name
        assert sha(root / name) == artifact["sha256"]
        assert (root / name).stat().st_size == artifact["bytes"]
    assert set(result["artifacts"]) == {"raw.ll", "postpasses.ll", "raw-contract.json", "post-contract.json"}
    return result, {phase: json.loads((root / (phase + "-contract.json")).read_text()) for phase in ("raw", "post")}


def main():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--baseline-result-sha256", required=True)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--candidate-result-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    packet = Path(__file__).resolve().parent
    assert sha(packet / "manifest.json") == args.manifest_sha256
    manifest = json.loads((packet / "manifest.json").read_text())
    for name, artifact in manifest["artifacts"].items():
        assert sha(packet / name) == artifact["sha256"]
    assert not args.output.exists() and args.output.parent.is_dir()
    assert args.baseline.resolve() != args.candidate.resolve()
    base, baseline = load_arm(args.baseline, args.baseline_result_sha256, "baseline", manifest, args.manifest_sha256)
    candidate, outlined = load_arm(args.candidate, args.candidate_result_sha256, "candidate", manifest, args.manifest_sha256)
    assert base["source_class_order"] == candidate["source_class_order"]
    assert base["generation_witnesses"]["top_dispatch"] == candidate["generation_witnesses"]["top_dispatch"]
    names = base["source_class_order"]
    admitted = candidate["admitted_classes"]
    rejected = candidate["inline_classes"]
    assert not base["admitted_classes"] and base["inline_classes"] == names
    assert not set(admitted).intersection(rejected)
    assert len(admitted) + len(rejected) == len(names) == 56
    assert [name for name in names if name in admitted] == admitted
    assert [name for name in names if name in rejected] == rejected
    checks = {}
    for phase in ("raw", "post"):
        left, right = baseline[phase], outlined[phase]
        assert left["summary"] == base[phase] and right["summary"] == candidate[phase]
        for contract in (left, right):
            summary = contract["summary"]
            assert summary["owned_ssa_cfg_verification"] == summary["all_function_arm_root_planning"] == "PASS"
            assert summary["zero_libpython_calls"] and summary["zero_strict_stubs"]
            assert len(contract["functions"]) == len(contract["root_plans"]) == summary["functions"]
        assert left["public_signatures"] == right["public_signatures"]
        assert left["public_globals"] == right["public_globals"]
        assert set(left["class_order"]) == set(right["class_order"])
        for owner in left["class_order"]:
            assert [row["event"] for row in left["class_order"][owner]] == names
            assert [row["event"] for row in right["class_order"][owner]] == names
        expected_binding = ["Node", "_build_visitor_dispatch", "NodeVisitor"]
        assert [row["event"] for row in left["top_function_binding_order"]] == expected_binding
        assert [row["event"] for row in right["top_function_binding_order"]] == expected_binding
        assert left["summary"]["helpers"] == 0 and right["summary"]["helpers"] == len(admitted)
        assert right["summary"]["functions"] == left["summary"]["functions"] + len(admitted)
        assert not left["helper_status_edges"] and len(right["helper_status_edges"]) == 2 * len(admitted)
        assert all(row["failure"] == "err.exit" for row in right["helper_status_edges"])
        assert not left["helper_root_lease_contracts"]
        assert len(right["helper_root_lease_contracts"]) == len(admitted)
        for row in right["helper_root_lease_contracts"]:
            assert row["status"] == row["lease_cleanup"]["status"] == "pass"
            assert row["root_returns"] and all(item["active_groups"] == 0 for item in row["root_returns"])
        for callee in manifest["equal_expanded_runtime_calls"]:
            assert left["expanded_runtime_call_counts"].get(callee, 0) == right["expanded_runtime_call_counts"].get(callee, 0), (phase, callee)
        all_callees = set(left["expanded_runtime_call_counts"]) | set(right["expanded_runtime_call_counts"])
        differences = {callee: {"baseline": left["expanded_runtime_call_counts"].get(callee, 0),
                                 "candidate": right["expanded_runtime_call_counts"].get(callee, 0)}
                       for callee in sorted(all_callees)
                       if left["expanded_runtime_call_counts"].get(callee, 0) != right["expanded_runtime_call_counts"].get(callee, 0)}
        checks[phase] = {"contracts": "PASS", "other_expanded_runtime_call_count_differences": differences}
    left, right = base["post"], candidate["post"]
    total_field = "text_instructions_including_terminators"
    max_field = "max_function_text_instructions"
    total_reduction = 1.0 - right[total_field] / left[total_field]
    max_reduction = 1.0 - right[max_field] / left[max_field]
    thresholds = manifest["mechanism_thresholds"]
    assert thresholds == {"total_post_default_instruction_reduction": 0.25, "max_function_instruction_reduction": 0.50}
    material = total_reduction >= thresholds["total_post_default_instruction_reduction"] and max_reduction >= thresholds["max_function_instruction_reduction"]
    result = {"schema": "pcc.class-init-outline-real-comparison.v1", "contracts": "PASS",
              "decision": "MECHANISM_THRESHOLD_MET_REVIEW_ONLY" if material else "HOLD_NO_MATERIAL_IR_REDUCTION",
              "manifest_sha256": args.manifest_sha256,
              "baseline_result_sha256": args.baseline_result_sha256,
              "candidate_result_sha256": args.candidate_result_sha256,
              "target": manifest["target"], "passes": manifest["passes"],
              "admitted_classes": admitted, "inline_classes": rejected, "phase_checks": checks,
              "instruction_metric": "all textual function instructions, including PHIs and terminators",
              "total_post_default_instruction_reduction": total_reduction,
              "max_function_instruction_reduction": max_reduction,
              "thresholds": thresholds, "baseline_post": left, "candidate_post": right,
              "scope": "host structural evidence only; no native, object, runtime, timing or full Stage1 acceptance"}
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
