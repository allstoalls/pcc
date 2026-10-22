import json

from pcc.diagnostics.bench_profile_aggregate import summarize_profiles


def test_summarize_profiles_orders_slowest_first(tmp_path):
    a = tmp_path / "a.json"; b = tmp_path / "b.json"
    a.write_text(json.dumps({"total_ms": 1, "metadata": {"scenario": "a"}}), encoding="utf-8")
    b.write_text(json.dumps({"total_ms": 3, "metadata": {"scenario": "b"}}), encoding="utf-8")
    summary = summarize_profiles([str(a), str(b)])
    assert summary["slowest"]["name"] == "b"
