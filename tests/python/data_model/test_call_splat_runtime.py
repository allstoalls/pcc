from __future__ import annotations

import os
import subprocess
import textwrap


def test_call_splat_posargs_and_kwargs_runtime(tmp_path, pcc_runtime_archive):
    src = tmp_path / "call_splat_probe.c"
    exe = tmp_path / "call_splat_probe"
    src.write_text(
        textwrap.dedent(
            """
            #include "py_runtime.h"
            #include <stdio.h>

            int main(void) {
                PyObject *base = py_tuple_new(2);
                py_tuple_set_item(base, 0, py_int_from_i64(1));
                py_tuple_set_item(base, 1, py_int_from_i64(2));

                PyObject *star = py_list_new(0);
                py_list_append(star, py_int_from_i64(3));
                py_list_append(star, py_int_from_i64(4));

                PyObject *merged = py_call_merge_posargs(base, star);
                if (py_tuple_len(merged) != 4) return 1;
                if (py_int_to_i64(py_tuple_get(merged, 0), NULL) != 1) return 2;
                if (py_int_to_i64(py_tuple_get(merged, 3), NULL) != 4) return 3;

                PyObject *kw = py_dict_new();
                py_dict_set(kw, py_str_new("a", 1), py_int_from_i64(10));
                PyObject *extra = py_dict_new();
                py_dict_set(extra, py_str_new("b", 1), py_int_from_i64(20));

                PyObject *merged_kw = py_call_merge_kwargs(kw, extra);
                if (py_dict_len(merged_kw) != 2) return 4;
                if (py_int_to_i64(py_dict_get(merged_kw, py_str_new("a", 1)), NULL) != 10) return 5;
                if (py_int_to_i64(py_dict_get(merged_kw, py_str_new("b", 1)), NULL) != 20) return 6;
                printf("call-splat-ok\\n");
                return 0;
            }
            """
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [
            os.environ.get("CC", "cc"),
            "-I",
            str(pcc_runtime_archive.parent / "include"),
            str(src),
            str(pcc_runtime_archive),
            "-lm",
            "-o",
            str(exe),
        ],
        check=True,
    )
    proc = subprocess.run([str(exe)], check=True, text=True, capture_output=True)
    assert proc.stdout.strip() == "call-splat-ok"
