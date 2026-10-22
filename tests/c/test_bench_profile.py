import json

from pcc.diagnostics.bench_profile import BenchResult, format_bench_json


def test_bench_json_schema():
    data = json.loads(format_bench_json([BenchResult("x", ("pcc",), 1.0, 0)]))
    assert data["schema"] == "pcc.bench.v1"
    assert data["total_wall_ms"] == 1.0
