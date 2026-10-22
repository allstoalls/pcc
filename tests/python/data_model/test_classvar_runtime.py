from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path


def test_native_class_level_variable_read_write_and_delete(tmp_path, pcc_runtime_archive):
    src = tmp_path / "classvar_probe.c"
    exe = tmp_path / "classvar_probe"
    src.write_text(
        textwrap.dedent(
            '''
            #include "py_internal.h"
            #include <stdio.h>

            int main(void) {
                PyClassObject *cls = py_class_new("Counter", NULL, 0, NULL, 0);
                PyObject *cls_obj = (PyObject *)cls;

                PyObject *one = py_int_from_i64(1);
                PyObject *two = py_int_from_i64(2);

                if (py_obj_setattr(cls_obj, "count", one) != 0) return 1;
                PyObject *got1 = py_obj_getattr(cls_obj, "count");
                if (py_int_to_i64(got1, NULL) != 1) return 2;

                if (py_obj_setattr(cls_obj, "count", two) != 0) return 3;
                PyObject *got2 = py_obj_getattr(cls_obj, "count");
                if (py_int_to_i64(got2, NULL) != 2) return 4;

                PyObject *dict = py_obj_getattr(cls_obj, "__dict__");
                if (dict == NULL || py_dict_len(dict) != 1) return 5;

                if (py_obj_delattr(cls_obj, "count") != 0) return 6;
                PyObject *dict2 = py_obj_getattr(cls_obj, "__dict__");
                if (py_dict_len(dict2) != 0) return 7;

                printf("classvar-ok\\n");
                return 0;
            }
            '''
        ),
        encoding="utf-8",
    )

    subprocess.run(
        [
            os.environ.get("CC", "cc"),
            "-I",
            str(pcc_runtime_archive.parent / "include"),
            "-I",
            str(pcc_runtime_archive.parent / "src"),
            str(src),
            str(pcc_runtime_archive),
            "-lm",
            "-o",
            str(exe),
        ],
        check=True,
    )
    proc = subprocess.run([str(exe)], check=True, text=True, capture_output=True)
    assert proc.stdout.strip() == "classvar-ok"


def test_classvar_uses_dedicated_attr_dict_not_method_table():
    internal = Path("pcc/runtime/src/py_internal.h").read_text(encoding="utf-8")

    assert "PyObject               *attrs;" in internal
