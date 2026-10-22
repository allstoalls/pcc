from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

from tests.owned_c_execution import compile_owned_c_with_runtime


def test_py_type_builtin_native_runtime(tmp_path, pcc_runtime_archive):
    src = tmp_path / "type_builtin_probe.c"
    exe = tmp_path / "type_builtin_probe"
    src.write_text(
        textwrap.dedent(
            r"""
            #include "py_runtime.h"
            #include <stdio.h>
            #include <string.h>

            static const char *class_name(PyObject *obj) {
                PyObject *cls = py_type_builtin(obj);
                PyObject *name = py_obj_getattr(cls, "__name__");
                const char *utf8 = py_str_utf8(name);
                return utf8 ? utf8 : "<null>";
            }

            int main(void) {
                PyObject *i = py_int_from_i64(7);
                PyObject *lst = py_list_new(0);
                PyObject *b = py_bytes_new("\xff", 1);
                const char *n1 = class_name(i);
                const char *n2 = class_name(lst);
                const char *n3 = class_name(b);
                printf("%s\n%s\n%s\n", n1, n2, n3);
                if (strcmp(n1, "int") != 0) return 10;
                if (strcmp(n2, "list") != 0) return 11;
                if (strcmp(n3, "bytes") != 0) return 12;
                return 0;
            }
            """
        ),
        encoding="utf-8",
    )
    compile_owned_c_with_runtime(src, exe, runtime_archive=pcc_runtime_archive)
    proc = subprocess.run([str(exe)], check=True, text=True, capture_output=True)
    assert proc.stdout.splitlines() == ["int", "list", "bytes"]


def test_py_type_builtin_wired_in_c_and_pcc_py_sources():
    py_src = Path("pcc/runtime/py/py_obj_ops_dispatch.py").read_text(encoding="utf-8")
    header = Path("pcc/runtime/include/py_runtime.h").read_text(encoding="utf-8")
    abi = Path("pcc/frontends/python/codegen/runtime_abi.py").read_text(encoding="utf-8")

    assert '@c_abi_export("py_type_builtin")' in py_src
    assert "PyObject *py_type_builtin(PyObject *o);" in header
    assert '"py_type_builtin": (_PYOBJ, [_PYOBJ], False)' in abi
