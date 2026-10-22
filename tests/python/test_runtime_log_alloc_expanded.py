from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path


def test_pcc_log_alloc_covers_scalar_class_func_weakref_exception_paths(tmp_path, pcc_runtime_archive):
    repo = Path(__file__).absolute().parents[2]
    runtime = repo / "pcc" / "runtime"

    src = tmp_path / "alloc_expanded_probe.c"
    exe = tmp_path / "alloc_expanded_probe"
    log_path = tmp_path / "alloc-expanded.jsonl"
    src.write_text(textwrap.dedent(r"""
        #include "py_runtime.h"

        static PyObject *dummy_func(PyObject *captures, PyObject *args) {
            (void)captures;
            (void)args;
            py_incref(py_None);
            return py_None;
        }

        /* PY_EXC_EXCEPTION == 1 in py_internal.h; spelled inline here so the
         * harness only needs the public ``py_runtime.h``. PyClassObject /
         * py_class_new / py_instance_new are internal API and are exercised
         * via the dedicated class native test, not this allocation probe. */
        int main(void) {
            PyObject *s = py_str_new("hello", 5);
            PyObject *b = py_bytes_new("abc", 3);
            PyObject *ba = py_bytearray_from_obj(b);
            PyObject *mv = py_memoryview_new(b);
            PyObject *f = py_float_from_f64(1.25);
            PyObject *z = py_complex_new(1.0, 2.0);
            PyObject *fn = py_func_new((void *)dummy_func, NULL);
            PyObject *exc = py_exc_new(1 /* PY_EXC_EXCEPTION */, "bad");
            PyObject *wr = py_weakref_new(s, NULL);

            py_decref(wr);
            py_decref(exc);
            py_decref(fn);
            py_decref(z);
            py_decref(f);
            py_decref(mv);
            py_decref(ba);
            py_decref(b);
            py_decref(s);
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
        "PCC_LOG": "alloc",
        "PCC_LOG_FORMAT": "json",
        "PCC_LOG_FILE": str(log_path),
    })
    run = subprocess.run([str(exe)], capture_output=True, text=True, env=env, timeout=20)
    assert run.returncode == 0, run.stderr
    assert log_path.exists()

    events = [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    alloc_tags = {
        int(event["value1"])
        for event in events
        if event.get("category") == "alloc" and event.get("event") == "alloc_object"
    }
    # 3 str, 4 bytes, 9 func, 12 exc, 16 float, 17 complex, 18 weakref,
    # 19 bytearray, 21 memoryview (per py_internal.h type tag table).
    # 10 (class) and instance-tag (>=100) are covered separately because the
    # class API is internal-only and not exposed in py_runtime.h.
    assert {3, 4, 9, 12, 16, 17, 18, 19, 21}.issubset(alloc_tags)
