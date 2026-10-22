from __future__ import annotations

import json

from scripts import pcc_gc_viewer


def test_gc_viewer_json_output_summarizes_current_runtime_schema(tmp_path, capsys):
    log = tmp_path / "runtime.jsonl"
    log.write_text(
        "\n".join([
            '{"schema":"pcc.diagnostics.runtime_log.v1","category":"alloc","event":"alloc_object","value0":32,"value1":5}',
            '{"schema":"pcc.diagnostics.runtime_log.v1","category":"gc","event":"collect_stop","value0":1,"value1":0}',
        ]) + "\n",
        encoding="utf-8",
    )

    assert pcc_gc_viewer.main([str(log), "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["schema"] == "pcc.gc_viewer.summary.v1"
    assert data["summary"]["allocations"] == 1
    assert data["summary"]["allocated_bytes"] == 32
    assert data["summary"]["collections"] == 1
    assert data["summary"]["events_by_name"]["alloc/alloc_object"] == 1
