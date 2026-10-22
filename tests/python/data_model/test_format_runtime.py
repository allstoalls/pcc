from __future__ import annotations

import os
import subprocess
import textwrap


def test_format_runtime_builtin_and_user_dunder(tmp_path, pcc_runtime_archive):
    src = tmp_path / "format_probe.c"
    exe = tmp_path / "format_probe"
    src.write_text(
        textwrap.dedent(
            """
            #include "py_internal.h"
            #include <stdio.h>
            #include <stdint.h>
            #include <string.h>

            static PyObject *fmt(PyObject *self, PyObject *spec) {
                (void)self; (void)spec;
                return py_str_new("custom-format", 13);
            }

            int main(void) {
                PyObject *seven = py_int_from_i64(255);
                PyObject *hex = py_obj_format(seven, py_str_new("x", 1));
                if (strcmp(py_str_utf8(hex), "ff") != 0) return 1;

                PyClassObject *cls = py_class_new("Fmt", NULL, 0, NULL, 0);
                py_class_add_method(cls, "__format__", (PyObject *)(uintptr_t)fmt);
                PyObject *obj = py_instance_new(cls);
                PyObject *out = py_obj_format(obj, py_str_new("", 0));
                if (strcmp(py_str_utf8(out), "custom-format") != 0) return 2;
                printf("format-ok\\n");
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
    assert proc.stdout.strip() == "format-ok"
