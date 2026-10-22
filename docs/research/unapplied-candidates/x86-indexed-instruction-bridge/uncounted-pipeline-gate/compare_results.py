"""Read-only four-arm result comparison; never imports the compiler."""
import argparse
import hashlib
import json
from pathlib import Path


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    packet = Path(__file__).resolve().parent
    assert sha(packet / "manifest.json") == args.manifest_sha256
    manifest = json.loads((packet / "manifest.json").read_text())
    assert sha(Path(__file__)) == manifest["comparison_driver_sha256"]
    root = args.root.resolve(strict=True)
    output = args.output.resolve()
    assert not output.exists() and output.parent.is_dir()
    runs = []
    guards = []
    receipts = []
    previous_end = None
    shared_lock_identity = None
    for slot in manifest["schedule"]:
        stage = root / slot["name"]
        result_path = stage / "payload/result.json"
        result = json.loads(result_path.read_text())
        guard_path = stage / "guard/result.json"
        guard = json.loads(guard_path.read_text())
        bootstrap = json.loads((stage / "guard/bootstrap-result.json").read_text())
        wrapper = json.loads((stage / "wrapper.json").read_text())
        assert result["status"] == wrapper["status"] == bootstrap["status"] == "PASS"
        assert result["slot"] == slot["slot"] and result["arm"] == slot["arm"]
        assert result["manifest_sha256"] == args.manifest_sha256
        assert result["input_sha256"] == manifest["input"]["sha256"]
        assert result["source_inventory_sha256"] == manifest["source_inventories"][slot["arm"]]
        assert result["emitter_sha256"] == manifest["emitter_sha256"][slot["arm"]]
        assert result["target"] == manifest["target"]
        assert result["codegen_checksum"] == manifest["codegen_checksum"][slot["arm"]]
        assert result["object"] == manifest["reference_object"]
        assert result["decoded_contract"] == manifest["reference_decoded_contract"]
        assert result["exact_reference_equality"] and result["cfg_rows_equal_reference"]
        assert sha(stage / "payload/module.o") == manifest["reference_object"]["sha256"]
        assert (stage / "payload/module.o").stat().st_size == manifest["reference_object"]["bytes"]
        assert sha(stage / "payload/module.s") == manifest["reference_assembly"]["sha256"]
        assert (stage / "payload/module.s").stat().st_size == manifest["reference_assembly"]["bytes"]
        assert wrapper["result_sha256"] == sha(result_path)
        for boundary in ("source_before", "source_after"):
            for arm in ("baseline", "candidate"):
                row = wrapper[boundary][arm]
                assert row["status"] == "PASS"
                assert row["manifest_sha256"] == manifest["source_inventories"][arm]
        assert guard["status"] == "COMPLETE" and guard["returncode"] == 0
        assert guard["cleanup"]["status"] == "CLEAN"
        assert guard["cleanup"]["root_reaped"] and guard["cleanup"]["echild"]
        assert guard["timeout_s"] == 300 and guard["rss_threshold_bytes"] == 4294967296
        assert guard["min_free_bytes"] == 4294967296 and guard["lock"]["exclusive"]
        assert guard["max_disk_growth_bytes"] == 1073741824
        lock_identity = tuple(guard["lock"][key] for key in ("device", "inode", "path"))
        if shared_lock_identity is None:
            shared_lock_identity = lock_identity
        else:
            assert lock_identity == shared_lock_identity
        assert bootstrap["hard_address_space_bytes"] == 4294967296 and bootstrap["hard_nproc"] == 0
        assert not bootstrap["denied_events"]
        if previous_end is not None:
            assert guard["started_monotonic"] >= previous_end
        previous_end = guard["cleanup"]["last_kill_pass_monotonic"]
        metrics = result["timing_ns"]
        assert all(type(value) is int and value > 0 for value in metrics.values())
        assert metrics["combined_wall"] == metrics["emission_wall"] + metrics["encoding_wall"]
        assert metrics["combined_cpu"] == metrics["emission_cpu"] + metrics["encoding_cpu"]
        runs.append(result)
        guards.append(guard)
        receipts.append({"slot": slot["slot"], "result_sha256": sha(result_path),
                         "guard_sha256": sha(guard_path), "wrapper_sha256": sha(stage / "wrapper.json")})

    regression = manifest["stop_loss"]["when_both_combined_wall_and_cpu_regression_at_least_percent"]
    assert not all(runs[1]["timing_ns"][key] * 100 >= runs[0]["timing_ns"][key] * (100 + regression)
                   for key in ("combined_wall", "combined_cpu")), "declared stop-loss was ignored"

    def gain(baseline, candidate):
        return 100.0 * (baseline - candidate) / baseline

    metrics = {}
    for key in runs[0]["timing_ns"]:
        baselines = [runs[index]["timing_ns"][key] for index in (0, 3)]
        candidates = [runs[index]["timing_ns"][key] for index in (1, 2)]
        baseline_mean = sum(baselines) / 2.0
        candidate_mean = sum(candidates) / 2.0
        metrics[key] = {"baseline_values_ns": baselines, "candidate_values_ns": candidates,
                        "baseline_mean_ns": baseline_mean, "candidate_mean_ns": candidate_mean,
                        "mean_reduction_percent": gain(baseline_mean, candidate_mean),
                        "forward_reduction_percent": gain(baselines[0], candidates[0]),
                        "reverse_reduction_percent": gain(baselines[1], candidates[1]),
                        "baseline_spread_percent": 100.0 * (max(baselines) - min(baselines)) / baseline_mean}
    wall = metrics["combined_wall"]
    cpu = metrics["combined_cpu"]
    rss = {"baseline_through_pipeline": [runs[index]["rss_high_water_bytes"]["through_timed_pipeline"] for index in (0, 3)],
           "candidate_through_pipeline": [runs[index]["rss_high_water_bytes"]["through_timed_pipeline"] for index in (1, 2)],
           "whole_guard_peak_sampled_rss_bytes_in_order": [guard["peak_sampled_tree_rss_bytes"] for guard in guards]}
    rss_growth = 100.0 * (sum(rss["candidate_through_pipeline"]) / sum(rss["baseline_through_pipeline"]) - 1.0)
    threshold = max(manifest["interpretation"]["minimum_mean_wall_gain_percent"], wall["baseline_spread_percent"])
    worthwhile = (wall["forward_reduction_percent"] > 0 and wall["reverse_reduction_percent"] > 0
                 and wall["mean_reduction_percent"] >= threshold
                 and cpu["mean_reduction_percent"] > 0
                 and metrics["emission_wall"]["mean_reduction_percent"] > 0
                 and rss_growth <= manifest["interpretation"]["maximum_mean_high_water_growth_percent"])
    result = {"schema": "pcc.x86-bridge-uncounted-comparison.v1",
              "status": "FOUR_ARMS_CORRECT_AND_CLEAN", "manifest_sha256": args.manifest_sha256,
              "receipts": receipts, "metrics": metrics, "rss": rss,
              "mean_through_pipeline_high_water_growth_percent": rss_growth,
              "required_mean_wall_gain_percent": threshold,
              "decision": "ELIGIBLE_FOR_NEXT_SOURCE_QUALIFICATION_REVIEW" if worthwhile else "HOLD_NO_MATERIAL_PIPELINE_BENEFIT_ESTABLISHED",
              "limits": ["two samples per arm, not a statistical confidence bound",
                         "Linux-host c_ast backend/object pipeline only; no full Stage1, native compiler or Windows timing claim",
                         "process-lifetime and sampled guard RSS are separate non-additive observations",
                         "no automatic publication or additional performance runs"]}
    with output.open("x") as stream:
        json.dump(result, stream, indent=2, sort_keys=True)
        stream.write("\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
