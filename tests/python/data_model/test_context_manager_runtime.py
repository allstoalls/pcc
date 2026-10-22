from __future__ import annotations

import os
import subprocess
import textwrap


def test_context_enter_exit_runtime(tmp_path, pcc_runtime_archive):
    src = tmp_path / "context_probe.c"
    exe = tmp_path / "context_probe"
    src.write_text(
        textwrap.dedent(
            """
            #include "py_internal.h"
            #include <stdio.h>
            #include <stdint.h>
            #include <string.h>

            static PyObject *enter(PyObject *self) {
                (void)self;
                return py_str_new("entered", 7);
            }

            static PyObject *exit_(PyObject *self, PyObject *exc_type, PyObject *exc, PyObject *tb) {
                (void)self; (void)exc_type; (void)exc; (void)tb;
                return py_bool_from_bit(1);
            }

            static PyObject *silent_enter(PyObject *self) {
                (void)self;
                return NULL;
            }

            static PyObject *silent_exit(PyObject *self, PyObject *exc_type, PyObject *exc, PyObject *tb) {
                (void)self; (void)exc_type; (void)exc; (void)tb;
                return NULL;
            }

            int main(void) {
                PyClassObject *cls = py_class_new("CM", NULL, 0, NULL, 0);
                py_class_add_method(cls, "__enter__", (PyObject *)(uintptr_t)enter);
                py_class_add_method(cls, "__exit__", (PyObject *)(uintptr_t)exit_);
                PyObject *obj = py_instance_new(cls);
                PyObject *value = py_context_enter(obj);
                if (strcmp(py_str_utf8(value), "entered") != 0) return 1;
                if (py_context_exit(obj, py_None, py_None, py_None) != 1) return 2;

                PyClassObject *bad_cls = py_class_new("BadCM", NULL, 0, NULL, 0);
                py_class_add_method(bad_cls, "__enter__", (PyObject *)(uintptr_t)silent_enter);
                py_class_add_method(bad_cls, "__exit__", (PyObject *)(uintptr_t)silent_exit);
                PyObject *bad_obj = py_instance_new(bad_cls);
                if (py_context_enter(bad_obj) != NULL || !py_err_occurred()) return 3;
                py_clear_exception();
                if (py_context_exit(bad_obj, py_None, py_None, py_None) != 0) return 4;
                if (!py_err_occurred()) return 5;
                py_clear_exception();
                printf("context-ok\\n");
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
            "-I", str(pcc_runtime_archive.parent / "src"),
            str(src), str(pcc_runtime_archive),
            "-lm", "-o", str(exe),
        ],
        check=True,
    )
    proc = subprocess.run([str(exe)], check=True, text=True, capture_output=True)
    assert proc.stdout.strip() == "context-ok"


def test_context_runtime_guards_silent_null_before_owned_method_cleanup():
    py_source = open(
        "pcc/runtime/py/py_context_runtime.py", encoding="utf-8"
    ).read()

    for source in (py_source,):
        assert "py_runtime_error_if_unset" in source
        assert "context __enter__ returned NULL without setting an exception" in source
        assert "context __exit__ returned NULL without setting an exception" in source
        assert "py_context_enter received NULL manager" in source
        assert "py_context_exit received NULL manager" in source

    for source, call, guard, cleanup in ((
            py_source,
            "result = _call_enter_method(method, manager)",
            "py_context_enter returned NULL without setting an exception",
            "py_decref(method)",
        ), (
            py_source,
            "result = _call_exit_method(method, manager, exc_type, exc, traceback)",
            "py_context_exit returned NULL without setting an exception",
            "py_decref(method)",
        )):
        call_pos = source.index(call)
        guard_pos = source.index(guard, call_pos)
        cleanup_pos = source.index(cleanup, call_pos)
        assert call_pos < guard_pos < cleanup_pos
