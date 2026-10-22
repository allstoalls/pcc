from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

from tests.runtime_build_cache import cached_pcc_python_runtime


REPO_ROOT = Path(__file__).absolute().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"


def _slice_between(src: str, start: str, end: str) -> str:
    start_idx = src.index(start)
    end_idx = src.index(end, start_idx)
    return src[start_idx:end_idx]


def _build_runtime(tmp_path: Path) -> Path:
    del tmp_path
    return cached_pcc_python_runtime()


def _compile_and_run(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    work_runtime = _build_runtime(tmp_path)
    src = tmp_path / "vthread_ready_entry_pool_probe.c"
    exe = tmp_path / "vthread_ready_entry_pool_probe.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
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
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    return subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)


def test_virtual_thread_ready_entry_pool_preserves_roots_across_backends(
    tmp_path: Path,
) -> None:
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include "py_internal.h"
        #include <stdio.h>

        static int check_backend(int64_t backend) {
            if (pcc_gc_set_backend(backend) != 0) return 0;
            pcc_gc_telemetry_reset();
            if (pcc_gc_scheduler_root_count() != 0) return 0;

            for (int64_t i = 0; i < 192; i++) {
                PyObject *vt = py_virtual_thread_new(py_None);
                if (vt == 0) return 0;
                if (py_virtual_thread_start(vt) != 0) return 0;
                pcc_gc_release(vt);

                if (pcc_gc_scheduler_root_count() != 1) return 0;
                if (py_virtual_thread_ready_count() != 1) return 0;
                (void)pcc_gc_collect(0);

                PyObject *ready = py_virtual_thread_poll_ready();
                if (ready == 0) return 0;
                if (pcc_gc_scheduler_root_count() != 0) return 0;
                if (py_type_of(ready) != PY_TYPE_VIRTUAL_THREAD) return 0;
                if (py_virtual_thread_state(ready) != 2) return 0;
                if (py_virtual_thread_complete(ready, py_None) != 0) return 0;
                py_decref(ready);

                if (py_virtual_thread_ready_count() != 0) return 0;
                if (py_virtual_thread_timer_count() != 0) return 0;
                if (py_virtual_thread_io_wait_count() != 0) return 0;
                if (pcc_gc_scheduler_root_count() != 0) return 0;
            }
            if (py_virtual_thread_node_pool_stat(
                    PCC_VTHREAD_NODE_READY,
                    PCC_VTHREAD_POOL_ALLOCATIONS
                ) <= 0) return 0;
            if (py_virtual_thread_node_pool_stat(
                    PCC_VTHREAD_NODE_READY,
                    PCC_VTHREAD_POOL_REUSES
                ) <= 0) return 0;
            int64_t cached = py_virtual_thread_node_pool_stat(
                PCC_VTHREAD_NODE_READY,
                PCC_VTHREAD_POOL_CACHED
            );
            if (cached <= 0 || cached > 4096) return 0;
            return 1;
        }

        int main(void) {
            for (int64_t backend = 0; backend <= 4; backend++) {
                int ok = check_backend(backend);
                printf("%lld:%d\\n", (long long)backend, ok);
                if (!ok) return (int)(40 + backend);
            }
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip().splitlines() == [
        "0:1",
        "1:1",
        "2:1",
        "3:1",
        "4:1",
    ]
