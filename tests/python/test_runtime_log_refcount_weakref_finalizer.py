from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path


REPO = Path(__file__).absolute().parents[2]
RUNTIME = REPO / "pcc" / "runtime"


def _compile_harness(
    tmp_path: Path,
    name: str,
    source: str,
    runtime_archive: Path,
) -> Path:
    src = tmp_path / f"{name}.c"
    exe = tmp_path / name
    src.write_text(textwrap.dedent(source), encoding="utf-8")
    cc = os.environ.get("CC", "cc")
    cmd = [
        cc,
        "-std=c11",
        f"-I{RUNTIME / 'include'}",
        f"-I{RUNTIME / 'src'}",
        str(src),
        str(runtime_archive),
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


def _run_with_log(exe: Path, tmp_path: Path, channels: str) -> list[dict[str, object]]:
    log_path = tmp_path / f"{exe.name}.jsonl"
    env = os.environ.copy()
    env.update({
        "PCC_LOG": channels,
        "PCC_LOG_FORMAT": "json",
        "PCC_LOG_FILE": str(log_path),
    })
    run = subprocess.run([str(exe)], capture_output=True, text=True, env=env, timeout=20)
    assert run.returncode == 0, run.stderr
    assert log_path.exists()
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_refcount_and_weakref_events_reach_native_log_file(
    tmp_path,
    pcc_runtime_archive,
):
    exe = _compile_harness(
        tmp_path,
        "refcount_weakref_probe",
        r'''
        #include "py_runtime.h"

        int main(void) {
            PyObject *obj = py_list_new(0);
            PyObject *wr = py_weakref_new(obj, py_None);
            py_incref(obj);
            py_decref(obj);
            py_weakref_invalidate(obj);
            py_decref(wr);
            py_decref(obj);
            return 0;
        }
        ''',
        pcc_runtime_archive,
    )
    events = _run_with_log(exe, tmp_path, "refcount,weakref")
    names = {(event["category"], event["event"]) for event in events}
    assert ("refcount", "incref") in names
    assert ("refcount", "decref") in names
    assert ("refcount", "free") in names
    assert ("weakref", "new") in names
    assert ("weakref", "invalidate") in names
    assert ("weakref", "dealloc") in names


def test_finalizer_events_reach_native_log_file(tmp_path, pcc_runtime_archive):
    exe = _compile_harness(
        tmp_path,
        "finalizer_probe",
        r'''
        #include "py_internal.h"
        #include <stdint.h>

        static int finalizer_hits = 0;

        static void finalizer(PyObject *self) {
            (void)self;
            finalizer_hits += 1;
        }

        int main(void) {
            PyClassObject *cls = py_class_new("FinalizerProbe", NULL, 0, NULL, 0);
            if (cls == NULL) return 2;
            py_class_add_method(cls, "__del__", (PyObject *)(uintptr_t)finalizer);
            PyObject *inst = py_instance_new(cls);
            if (inst == NULL) return 3;
            py_decref(inst);
            py_decref((PyObject *)cls);
            return finalizer_hits == 1 ? 0 : 4;
        }
        ''',
        pcc_runtime_archive,
    )
    events = _run_with_log(exe, tmp_path, "finalizer,refcount")
    names = {(event["category"], event["event"]) for event in events}
    assert ("finalizer", "call") in names
    assert ("finalizer", "done") in names


def test_pcc_python_runtime_archive_links_pcc_python_runtime_log():
    makefile = (RUNTIME / "Makefile").read_text(encoding="utf-8")
    assert "py_runtime_log" in makefile.split("PY_MODULES =", 1)[1].splitlines()[0]
    py_obj = (RUNTIME / "py" / "py_obj.py").read_text(encoding="utf-8")
    assert "pcc_diagnostics_runtime_log_event_code = extern(" in py_obj
    assert '"pcc_diagnostics_runtime_log_event_code"' in py_obj
