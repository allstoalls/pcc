from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

from tests.runtime_build_cache import cached_pcc_python_runtime


REPO_ROOT = Path(__file__).resolve().parents[3]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"


def test_t4_weakref_callable_and_dealloc_clear_native(tmp_path):
    work_runtime = cached_pcc_python_runtime()
    src = tmp_path / "weakref_probe.c"
    exe = tmp_path / "weakref_probe.out"
    src.write_text(
        textwrap.dedent(
            """
            #include "py_runtime.h"
            #include <stdio.h>

            int main(void) {
                if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
                PyObject *obj = py_list_new(0);
                if (obj == 0) return 3;
                PyObject *wr = py_weakref_new(obj, 0);
                if (wr == 0) return 4;
                PyObject *before = py_weakref_call(wr);
                if (before != obj) return 5;
                pcc_gc_release(before);
                pcc_gc_release(obj);
                (void)pcc_gc_collect(0);
                PyObject *after = py_weakref_call(wr);
                int ok = after == py_None;
                pcc_gc_release(after);
                pcc_gc_release(wr);
                printf("%d\\n", ok);
                return ok ? 0 : 6;
            }
            """
        ),
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            os.environ.get("CC", "cc"),
            "-std=c11",
            f"-I{work_runtime / 'include'}",
            f"-I{work_runtime / 'src'}",
            str(src),
            str(work_runtime / "libpy_runtime_pcc_py.a"),
            "-lm",
            "-o",
            str(exe),
        ],
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    proc = subprocess.run([str(exe)], text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "1"
