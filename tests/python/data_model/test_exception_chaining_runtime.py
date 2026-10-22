from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path


def test_exception_chaining_traceback_and_unhandled_print_native(
    tmp_path, pcc_runtime_archive
):
    src = tmp_path / "exc_chain_probe.c"
    exe = tmp_path / "exc_chain_probe"
    src.write_text(
        textwrap.dedent(
            """
            #include "py_runtime.h"
            #include <stdio.h>

            int main(void) {
                PyObject *root = py_exc_new(PY_EXC_VALUEERROR, "root");
                PyObject *outer = py_exc_new(PY_EXC_RUNTIMEERROR, "outer");

                py_exc_append_frame(root, "inner", "probe.py", 10);
                py_exc_append_frame(outer, "outer", "probe.py", 20);
                py_exc_set_cause(outer, root);

                if (py_exc_get_cause(outer) != root) return 1;
                if (py_exc_get_context(outer) != py_None) return 2;
                if (py_exc_traceback_len(root) != 1) return 3;
                if (py_exc_traceback_len(outer) != 1) return 4;

                py_exc_print_unhandled(outer);
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
    err = proc.stderr
    assert 'File "probe.py", line 10, in inner' in err
    assert 'File "probe.py", line 20, in outer' in err
    assert "The above exception was the direct cause" in err
    assert "ValueError: root" in err
    assert "RuntimeError: outer" in err


def test_implicit_context_is_set_by_raise_native(tmp_path, pcc_runtime_archive):
    src = tmp_path / "exc_context_probe.c"
    exe = tmp_path / "exc_context_probe"
    src.write_text(
        textwrap.dedent(
            """
            #include "py_runtime.h"

            int main(void) {
                PyObject *first = py_exc_new(PY_EXC_KEYERROR, "first");
                PyObject *second = py_exc_new(PY_EXC_VALUEERROR, "second");
                py_raise(first);
                py_raise(second);
                if (py_exc_get_context(second) != first) return 10;
                py_clear_exception();
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
    subprocess.run([str(exe)], check=True)
