from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path


def test_module_attr_side_table_runtime(tmp_path, pcc_runtime_archive):
    src = tmp_path / "module_attrs_probe.c"
    exe = tmp_path / "module_attrs_probe"
    src.write_text(
        textwrap.dedent(
            """
            #include "py_runtime.h"
            #include <stdio.h>

            int main(void) {
                if (py_module_attr_len("m") != 0) return 1;
                if (py_module_attr_set("m", "x", py_int_from_i64(42)) != 0) return 2;
                if (py_module_attr_len("m") != 1) return 3;
                PyObject *got = py_module_attr_get("m", "x");
                if (py_int_to_i64(got, NULL) != 42) return 4;
                if (py_module_attr_del("m", "x") != 0) return 5;
                if (py_module_attr_len("m") != 0) return 6;
                printf("module-attrs-ok\\n");
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
    assert proc.stdout.strip() == "module-attrs-ok"


def test_call_splat_and_module_attrs_are_built_and_exposed():
    makefile = Path("pcc/runtime/Makefile").read_text(encoding="utf-8")
    header = Path("pcc/runtime/include/py_runtime.h").read_text(encoding="utf-8")
    abi = Path("pcc/frontends/python/codegen/runtime_abi.py").read_text(encoding="utf-8")

    assert "py_call_splat_runtime" in makefile
    assert "py_module_attrs_runtime" in makefile
    assert "PyObject *py_call_merge_posargs" in header
    assert "int64_t   py_module_attr_set" in header
    assert '"py_call_merge_posargs": (_PYOBJ, [_PYOBJ, _PYOBJ], False)' in abi
    assert '"py_module_attr_set": (_I64, [_CSTR, _CSTR, _PYOBJ], False)' in abi
