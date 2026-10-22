from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path


RUNTIME_DIR = Path(__file__).absolute().parents[2] / "pcc" / "runtime"


def _compile_harness(tmp_path: Path, archive: Path, source: str) -> Path:
    src = tmp_path / "probe.c"
    exe = tmp_path / "probe"
    src.write_text(textwrap.dedent(source), encoding="utf-8")
    cc = os.environ.get("CC", "cc")
    cmd = [
        cc,
        "-std=c11",
        f"-I{RUNTIME_DIR / 'include'}",
        str(src),
        str(archive),
        "-lm",
        "-pthread",
        "-o",
        str(exe),
    ]
    if sys.platform.startswith("linux"):
        cmd.insert(-2, "-ldl")
    build = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert build.returncode == 0, build.stderr
    return exe


def _run_with_log(exe: Path, tmp_path: Path, channel: str) -> list[dict[str, object]]:
    log_path = tmp_path / f"{channel}.jsonl"
    env = os.environ.copy()
    env.update({
        "PCC_LOG": channel,
        "PCC_LOG_FORMAT": "json",
        "PCC_LOG_FILE": str(log_path),
    })
    run = subprocess.run([str(exe)], capture_output=True, text=True, env=env, timeout=20)
    assert run.returncode == 0, run.stdout + run.stderr
    assert log_path.exists()
    return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_exception_runtime_log_channel_records_lifecycle(tmp_path, pcc_runtime_archive):
    exe = _compile_harness(tmp_path, pcc_runtime_archive, r'''
        #include "py_runtime.h"

        int main(void) {
            PyObject *exc = py_exc_new(7, "boom");
            PyObject *cause = py_exc_new(3, "cause");
            py_exc_set_cause(exc, cause);
            py_exc_set_context(exc, cause);
            py_raise(exc);
            if (!py_err_occurred()) return 2;
            py_clear_exception();
            py_decref(exc);
            py_decref(cause);
            return 0;
        }
    ''')
    events = _run_with_log(exe, tmp_path, "exception")
    names = {(event["category"], event["event"]) for event in events}
    assert {event["schema"] for event in events} == {"pcc.diagnostics.runtime_log.v1"}
    assert ("exception", "alloc") in names
    assert ("exception", "new") in names
    assert ("exception", "set_cause") in names
    assert ("exception", "set_context") in names
    assert ("exception", "raise") in names
    assert ("exception", "clear") in names
    assert ("exception", "dealloc") in names


def test_dispatch_runtime_log_channel_records_generic_operations(tmp_path, pcc_runtime_archive):
    exe = _compile_harness(tmp_path, pcc_runtime_archive, r'''
        #include "py_runtime.h"

        int main(void) {
            PyObject *lst = py_list_new(0);
            PyObject *idx = py_int_from_i64(0);
            PyObject *value = py_int_from_i64(42);
            py_list_append(lst, value);
            PyObject *got = py_obj_getitem(lst, idx);
            if (got != NULL) py_decref(got);
            py_obj_setitem(lst, idx, value);
            PyObject *popped = py_list_pop(lst, -1);
            if (popped != NULL) py_decref(popped);

            PyObject *exc = py_exc_new(7, "dispatch");
            PyObject *msg = py_obj_getattr(exc, "value");
            if (msg != NULL) py_decref(msg);
            PyObject *wr = py_weakref_new(exc, NULL);
            PyObject *target = py_obj_call(wr, NULL, NULL);
            if (target != NULL) py_decref(target);
            py_obj_isinstance(exc, py_obj_getattr(exc, "__class__"));

            py_decref(wr);
            py_decref(exc);
            py_decref(value);
            py_decref(idx);
            py_decref(lst);
            return 0;
        }
    ''')
    events = _run_with_log(exe, tmp_path, "dispatch")
    names = {(event["category"], event["event"]) for event in events}
    assert ("dispatch", "getitem") in names
    assert ("dispatch", "setitem") in names
    assert ("dispatch", "getattr") in names
    assert ("dispatch", "call") in names
    assert ("dispatch", "isinstance") in names
