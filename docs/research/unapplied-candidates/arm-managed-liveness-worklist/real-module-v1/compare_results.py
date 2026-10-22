"""Complete output identity and the predeclared ARM liveness work stop rule."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


SUM_FIELDS = ("B", "E", "T", "row_visits", "positive_word_row_visits",
              "dense_successor_visits", "weighted_word_units", "changed_rows",
              "protocol_calls", "nonprotocol_calls", "nonempty_live_calls")
SAME_FIELDS = ("invocation", "name", "B", "E", "T", "W", "frame_size", "outdegrees",
               "value_names", "prepared_cfg", "origins", "tracked_ids", "protocol_before",
               "protocol_calls", "nonprotocol_calls", "nonempty_live_calls", "live_state_storage")


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def equal_bytes(left, right):
    assert left.stat().st_size == right.stat().st_size
    with left.open("rb") as a, right.open("rb") as b:
        while True:
            x, y = a.read(1024 * 1024), b.read(1024 * 1024)
            assert x == y
            if not x:
                break


def validate_mechanism(result):
    rows = result["mechanism"]["functions"]
    assert len(rows) == result["cfg"]["functions"]
    assert [row["invocation"] for row in rows] == list(range(len(rows)))
    assert len({row["name"] for row in rows}) == len(rows)
    assert {row["name"] for row in rows} == {row["name"] for row in result["cfg"]["rows"]}
    for row in rows:
        B, W, Q = row["B"], row["W"], row["row_visits"]
        assert len(row["outdegrees"]) == B and sum(row["outdegrees"]) == row["E"]
        assert W == (row["T"] + 29) // 30 and Q >= B
        assert 0 <= row["changed_rows"] <= Q
        assert 0 <= row["nonempty_live_calls"] <= row["nonprotocol_calls"]
        if W:
            visits = row["per_block_visits"]
            assert len(visits) == B and all(count >= 1 for count in visits)
            assert sum(visits) == Q == row["positive_word_row_visits"]
            edges = sum(count * degree for count, degree in zip(visits, row["outdegrees"]))
            assert row["dense_successor_visits"] == edges
            assert row["weighted_word_units"] == W * (2 * Q + edges)
            assert row["first_reverse_sweep_checked"] is True
            if result["arm"] == "baseline" and B:
                assert Q % B == 0 and row["baseline_K"] == Q // B
                assert all(count == row["baseline_K"] for count in visits)
                assert row["last_baseline_sweep_changes"] == 0
            else:
                assert row["baseline_K"] is None
        else:
            assert Q == B and row["per_block_visits"] is None
            assert row["baseline_K"] is None and row["first_reverse_sweep_checked"] is False
            assert row["positive_word_row_visits"] == row["dense_successor_visits"] == row["weighted_word_units"] == 0
    assert result["mechanism"]["totals"] == {key: sum(row[key] for row in rows) for key in SUM_FIELDS}
    assert result["mechanism"]["positive_word_functions"] == sum(row["W"] > 0 for row in rows)


def main():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--baseline-sha256", required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--candidate-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    packet = Path(__file__).resolve().parent
    assert sha(packet / "manifest.json") == args.manifest_sha256
    manifest = json.loads((packet / "manifest.json").read_text())
    assert sha(Path(__file__)) == manifest["comparator_sha256"]
    assert manifest["mechanism_threshold"] == {
        "minimum_weighted_word_reduction_percent": 25,
        "total_row_visits_must_not_increase": True,
        "positive_word_successor_visits_must_not_increase": True}
    results = []
    for arm, path, expected in (("baseline", args.baseline, args.baseline_sha256),
                                ("candidate", args.candidate, args.candidate_sha256)):
        assert sha(path) == expected
        value = json.loads(path.read_text())
        assert value["status"] == "PASS" and value["operation"] == "emit" and value["arm"] == arm
        assert value["manifest_sha256"] == args.manifest_sha256 and value["target"] == manifest["target"]
        assert value["source_inventory_sha256"] == manifest["source_inventories"][arm]
        assert value["solver_sha256"] == manifest["solver_sha256"][arm]
        assert value["codegen_checksum"] == manifest["codegen_checksums"][arm]
        assert value["object"]["name"] == "module.pco"
        assert value["call_states"]["name"] == "call-states.jsonl"
        for key in ("object", "call_states"):
            artifact = path.parent / value[key]["name"]
            assert artifact.stat().st_size == value[key]["bytes"] and sha(artifact) == value[key]["sha256"]
        validate_mechanism(value)
        results.append(value)
    baseline, candidate = results
    for key in ("input_sha256", "prepared_result_sha256", "cfg", "object", "call_states",
                "decoded_contract", "stackmap_functions", "transport_counts"):
        assert baseline[key] == candidate[key], key
    for key in ("object", "call_states"):
        equal_bytes(args.baseline.parent / baseline[key]["name"], args.candidate.parent / candidate[key]["name"])
    for left, right in zip(baseline["mechanism"]["functions"], candidate["mechanism"]["functions"]):
        assert {key: left[key] for key in SAME_FIELDS} == {key: right[key] for key in SAME_FIELDS}, left["name"]
    b, c = (value["mechanism"]["totals"] for value in results)
    positive = b["weighted_word_units"] > 0 and baseline["mechanism"]["positive_word_functions"] > 0
    material = positive and 100 * c["weighted_word_units"] <= 75 * b["weighted_word_units"]
    rows_ok = c["row_visits"] <= b["row_visits"]
    edges_ok = c["dense_successor_visits"] <= b["dense_successor_visits"]
    advance = material and rows_ok and edges_ok
    result = {"schema": "pcc.arm-managed-liveness-mechanism-comparison.v1", "status": "PASS",
              "decision": "MECHANISM_PASS_ELIGIBLE_FOR_SEPARATE_TIMING_REVIEW" if advance else "HOLD_NO_MATERIAL_ROW_WORK_REDUCTION",
              "manifest_sha256": args.manifest_sha256,
              "baseline_result_sha256": args.baseline_sha256,
              "candidate_result_sha256": args.candidate_sha256,
              "object_byte_equality": "PASS", "call_state_byte_equality": "PASS",
              "complete_decoded_contract_equality": "PASS", "prepared_cfg_origin_protocol_equality": "PASS",
              "baseline_totals": b, "candidate_totals": c,
              "weighted_word_reduction_percent": 100 * (1 - c["weighted_word_units"] / b["weighted_word_units"]) if positive else None,
              "positive_real_domain": positive, "material_weighted_reduction": material,
              "row_visits_nonincreasing": rows_ok, "dense_successor_visits_nonincreasing": edges_ok,
              "scope": "Dense-row-work proxy excludes indexing, queues, allocation and other codegen; no measured speed/native claim. No timing launch is authorized by this result."}
    output = args.output.resolve()
    assert not output.exists() and output.parent.is_dir()
    with output.open("x") as stream:
        json.dump(result, stream, sort_keys=True, indent=2)
        stream.write("\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
