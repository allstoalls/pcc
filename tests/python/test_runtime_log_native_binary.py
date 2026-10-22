from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path


def test_pcc_log_file_records_alloc_and_gc_from_native_runtime(tmp_path, pcc_runtime_archive):
    repo = Path(__file__).absolute().parents[2]
    runtime = repo / "pcc" / "runtime"

    src = tmp_path / "runtime_log_probe.c"
    exe = tmp_path / "runtime_log_probe"
    log_path = tmp_path / "pcc-runtime.jsonl"
    src.write_text(textwrap.dedent(r"""
        #include "py_runtime.h"

        int main(void) {
            PyObject *outer = py_list_new(0);
            PyObject *inner = py_list_new(0);
            PyObject *tuple = py_tuple_new(1);
            PyObject *dict = py_dict_new();
            PyObject *set = py_set_new();
            PyObject *key = py_int_from_i64(7);

            py_list_append(outer, inner);
            py_tuple_set_item(tuple, 0, inner);
            py_dict_set(dict, key, inner);
            py_set_add(set, key);

            py_decref(inner);
            (void)pcc_gc_collect(0);
            py_decref(set);
            py_decref(dict);
            py_decref(tuple);
            py_decref(outer);
            return 0;
        }
        """), encoding="utf-8")

    cc = os.environ.get("CC", "cc")
    cmd = [
        cc,
        "-std=c11",
        f"-I{runtime / 'include'}",
        str(src),
        str(pcc_runtime_archive),
        "-lm",
        "-pthread",
        "-o",
        str(exe),
    ]
    if sys.platform.startswith("linux"):
        cmd.insert(-2, "-ldl")
    build = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert build.returncode == 0, build.stderr

    env = os.environ.copy()
    env.update({
        "PCC_LOG": "alloc,gc",
        "PCC_LOG_FORMAT": "json",
        "PCC_LOG_FILE": str(log_path),
    })
    run = subprocess.run([str(exe)], capture_output=True, text=True, env=env, timeout=20)
    assert run.returncode == 0, run.stderr
    assert run.stdout == ""
    assert log_path.exists()

    events = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert events
    assert {event["schema"] for event in events} == {"pcc.diagnostics.runtime_log.v1"}
    names = {(event["category"], event["event"]) for event in events}
    alloc_tags = {
        event["value1"] for event in events
        if event["category"] == "alloc" and event["event"] == "alloc_object"
    }
    assert {5, 6, 7, 8}.issubset(alloc_tags)
    assert ("gc", "store_ptr") in names
    assert ("gc", "collect_start") in names
    assert ("gc", "collect_stop") in names
