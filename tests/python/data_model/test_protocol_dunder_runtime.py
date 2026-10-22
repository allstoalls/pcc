from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path


def test_user_protocol_dunders_native(tmp_path, pcc_runtime_archive):
    src = tmp_path / "protocol_probe.c"
    exe = tmp_path / "protocol_probe"
    src.write_text(
        textwrap.dedent(
            """
            #include "py_internal.h"
            #include <stdio.h>
            #include <stdint.h>

            static int64_t last_set = 0;
            static int64_t last_del = 0;

            static PyObject *dunder_len(PyObject *self) {
                (void)self;
                return py_int_from_i64(3);
            }

            static PyObject *dunder_bool(PyObject *self) {
                (void)self;
                return py_bool_from_bit(0);
            }

            static PyObject *dunder_contains(PyObject *self, PyObject *item) {
                (void)self;
                return py_bool_from_bit(py_int_to_i64(item, NULL) == 7);
            }

            static PyObject *dunder_getitem(PyObject *self, PyObject *key) {
                (void)self;
                return py_int_from_i64(py_int_to_i64(key, NULL) + 10);
            }

            static PyObject *dunder_setitem(PyObject *self, PyObject *key, PyObject *value) {
                (void)self;
                last_set = py_int_to_i64(key, NULL) * 100 + py_int_to_i64(value, NULL);
                py_incref(py_None);
                return py_None;
            }

            static PyObject *dunder_delitem(PyObject *self, PyObject *key) {
                (void)self;
                last_del = py_int_to_i64(key, NULL);
                py_incref(py_None);
                return py_None;
            }

            int main(void) {
                PyClassObject *cls = py_class_new("P", NULL, 0, NULL, 0);
                py_class_add_method(cls, "__len__", (PyObject *)(uintptr_t)dunder_len);
                py_class_add_method(cls, "__bool__", (PyObject *)(uintptr_t)dunder_bool);
                py_class_add_method(cls, "__contains__", (PyObject *)(uintptr_t)dunder_contains);
                py_class_add_method(cls, "__getitem__", (PyObject *)(uintptr_t)dunder_getitem);
                py_class_add_method(cls, "__setitem__", (PyObject *)(uintptr_t)dunder_setitem);
                py_class_add_method(cls, "__delitem__", (PyObject *)(uintptr_t)dunder_delitem);

                PyObject *obj = py_instance_new(cls);
                if (py_obj_len(obj) != 3) return 1;
                if (py_obj_truthy(obj) != 0) return 2;
                if (py_obj_contains(obj, py_int_from_i64(7)) != 1) return 3;
                if (py_obj_contains(obj, py_int_from_i64(8)) != 0) return 4;
                if (py_int_to_i64(py_obj_getitem(obj, py_int_from_i64(5)), NULL) != 15) return 5;
                if (py_obj_setitem(obj, py_int_from_i64(2), py_int_from_i64(9)) != 0) return 6;
                if (last_set != 209) return 7;
                if (py_obj_delitem(obj, py_int_from_i64(4)) != 0) return 8;
                if (last_del != 4) return 9;
                printf("protocol-ok\\n");
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
    assert proc.stdout.strip() == "protocol-ok"


def test_protocol_dunder_sources_are_wired():
    dispatch_py = Path("pcc/runtime/py/py_obj_ops_dispatch.py").read_text(encoding="utf-8")
    compare_py = Path("pcc/runtime/py/py_obj_ops_compare.py").read_text(encoding="utf-8")

    assert "py_user_getitem_dispatch" in dispatch_py
    assert "py_user_contains_dispatch" in compare_py
