from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path


def test_coroutine_and_task_done_result_runtime(tmp_path, pcc_runtime_archive):
    src = tmp_path / "coro_task_probe.c"
    exe = tmp_path / "coro_task_probe"
    src.write_text(
        textwrap.dedent(
            """
            #include "py_runtime.h"
            #include <stdio.h>

            static PyObject *entry(PyObject *captures, PyObject *args) {
                (void)captures; (void)args;
                return py_int_from_i64(42);
            }

            static PyObject *silent_entry(PyObject *captures, PyObject *args) {
                (void)captures; (void)args;
                return NULL;
            }

            int main(void) {
                PyObject *coro = py_coroutine_new_native("coro", (void *)entry, NULL, NULL);
                if (py_coroutine_is_done(coro) != 0) return 1;
                PyObject *result = py_await(coro);
                if (py_int_to_i64(result, NULL) != 42) return 2;
                if (py_coroutine_is_done(coro) != 1) return 3;
                PyObject *cached = py_coroutine_get_result(coro);
                if (py_int_to_i64(cached, NULL) != 42) return 4;

                PyObject *coro2 = py_coroutine_new_native("task-coro", (void *)entry, NULL, NULL);
                PyObject *task = py_task_new(coro2);
                if (py_task_is_done(task) != 0) return 5;
                PyObject *task_result = py_task_step(task);
                if (py_int_to_i64(task_result, NULL) != 42) return 6;
                if (py_task_is_done(task) != 1) return 7;
                PyObject *silent = py_coroutine_new_native(
                    "silent-coro", (void *)silent_entry, NULL, NULL
                );
                if (py_coroutine_run(silent) != NULL) return 8;
                if (!py_err_occurred()) return 9;
                py_clear_exception();
                if (py_await(NULL) != NULL || !py_err_occurred()) return 10;
                py_clear_exception();
                printf("coro-task-ok\\n");
                return 0;
            }
            """
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [
            os.environ.get("CC", "cc"),
            "-I", str(pcc_runtime_archive.parent / "include"),
            str(src), str(pcc_runtime_archive),
            "-lm", "-o", str(exe),
        ],
        check=True,
    )
    proc = subprocess.run([str(exe)], check=True, text=True, capture_output=True)
    assert proc.stdout.strip() == "coro-task-ok"


def test_coroutine_pointer_results_guard_silent_null_before_cleanup():
    py_source = Path("pcc/runtime/py/py_coroutine.py").read_text(
        encoding="utf-8"
    )
    messages = (
        "coroutine construction could not allocate coroutine state",
        "coroutine construction could not allocate captures tuple",
        "coroutine construction could not allocate arguments tuple",
        "coroutine entry returned NULL without setting an exception",
        "py_await received NULL awaitable",
        "__await__ could not allocate its argument tuple",
        "__await__ returned NULL without setting an exception",
    )
    for source in (py_source,):
        assert "coroutine_require_result" in source
        for message in messages:
            assert message in source

    for source, call, guard, cleanup in ((
            py_source,
            "iterator = py_obj_call(method, args, global_load_ptr(\"py_None\"))",
            "_coroutine_require_result(",
            "py_decref(args)",
        ),):
        call_pos = source.index(call)
        guard_pos = source.index(guard, call_pos)
        cleanup_pos = source.index(cleanup, call_pos)
        assert call_pos < guard_pos < cleanup_pos
