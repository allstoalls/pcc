"""Production vthread timer cancellation is immediate and five-GC safe."""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests.runtime_build_cache import cached_pcc_python_runtime


REPO_ROOT = Path(__file__).absolute().parents[3]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"


_SOURCE = r"""
#include "py_runtime.h"
#include "py_internal.h"

#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

static PyObject *new_vthread(void) {
    return py_virtual_thread_new(py_None);
}

static int check_cancel_reuse_and_root_transfer(void) {
    PyObject *first = new_vthread();
    if (first == NULL) return 0;
    if (py_virtual_thread_sleep(first, 2) != 0) return 0;
    void *first_entry = ((PyVirtualThreadObject *)first)->timer_entry;
    if (first_entry == NULL) return 0;
    if (py_virtual_thread_timer_count() != 1) return 0;
    if (pcc_gc_scheduler_root_count() != 1) return 0;

    if (py_virtual_thread_cancel_timer(first) != 1) return 0;
    if (py_virtual_thread_cancel_timer(first) != 0) return 0;
    if (((PyVirtualThreadObject *)first)->timer_entry != NULL) return 0;
    if (py_virtual_thread_timer_count() != 0) return 0;
    if (pcc_gc_scheduler_root_count() != 0) return 0;
    if (py_virtual_thread_complete(first, py_None) != 0) return 0;
    pcc_gc_release(first);

    /* The bounded node pool must be safe even when the just-cancelled address
     * is immediately reused while its lazy-cancelled heap tuple still exists. */
    PyObject *second = new_vthread();
    if (second == NULL) return 0;
    if (py_virtual_thread_sleep(second, 5) != 0) return 0;
    if (((PyVirtualThreadObject *)second)->timer_entry != first_entry) return 0;
    pcc_gc_release(second);
    (void)pcc_gc_collect(0);
    if (pcc_gc_scheduler_root_count() != 1) return 0;
    (void)poll(NULL, 0, 10);
    if (py_virtual_thread_poll_timers() != 1) return 0;
    if (py_virtual_thread_timer_count() != 0) return 0;
    if (pcc_gc_scheduler_root_count() != 1) return 0;
    PyObject *ready = py_virtual_thread_poll_ready();
    if (ready == NULL) return 0;
    if (pcc_gc_scheduler_root_count() != 0) return 0;
    if (((PyVirtualThreadObject *)ready)->timer_entry != NULL) return 0;
    if (py_virtual_thread_complete(ready, py_None) != 0) return 0;
    pcc_gc_release(ready);
    return 1;
}

static int check_complete_cancels_sleep(void) {
    PyObject *vthread = new_vthread();
    if (vthread == NULL) return 0;
    if (py_virtual_thread_sleep(vthread, 5) != 0) return 0;
    if (pcc_gc_scheduler_root_count() != 1) return 0;
    if (py_virtual_thread_complete(vthread, py_None) != 0) return 0;
    if (((PyVirtualThreadObject *)vthread)->timer_entry != NULL) return 0;
    if (py_virtual_thread_timer_count() != 0) return 0;
    if (pcc_gc_scheduler_root_count() != 0) return 0;
    pcc_gc_release(vthread);
    (void)poll(NULL, 0, 10);
    if (py_virtual_thread_poll_timers() != 0) return 0;
    return py_virtual_thread_poll_ready() == NULL;
}

static int check_unpark_cancels_sleep(void) {
    PyObject *vthread = new_vthread();
    if (vthread == NULL) return 0;
    if (py_virtual_thread_sleep(vthread, 5) != 0) return 0;
    if (py_virtual_thread_unpark(vthread) != 0) return 0;
    if (((PyVirtualThreadObject *)vthread)->timer_entry != NULL) return 0;
    if (py_virtual_thread_timer_count() != 0) return 0;
    /* The timer root was released and exactly one ready-queue root replaced it. */
    if (pcc_gc_scheduler_root_count() != 1) return 0;
    pcc_gc_release(vthread);
    (void)pcc_gc_collect(0);
    (void)poll(NULL, 0, 10);
    if (py_virtual_thread_poll_timers() != 0) return 0;
    PyObject *ready = py_virtual_thread_poll_ready();
    if (ready == NULL) return 0;
    if (py_virtual_thread_poll_ready() != NULL) return 0;
    if (pcc_gc_scheduler_root_count() != 0) return 0;
    if (py_virtual_thread_complete(ready, py_None) != 0) return 0;
    pcc_gc_release(ready);
    return 1;
}

int main(int argc, char **argv) {
    if (argc != 2) return 2;
    int64_t backend = (int64_t)atoll(argv[1]);
    if (pcc_gc_set_backend(backend) != 0) return 3;
    int ok = check_cancel_reuse_and_root_transfer()
        && check_complete_cancels_sleep()
        && check_unpark_cancels_sleep()
        && py_virtual_thread_timer_count() == 0
        && py_virtual_thread_ready_count() == 0
        && pcc_gc_scheduler_root_count() == 0
        && py_virtual_thread_node_pool_stat(
            PCC_VTHREAD_NODE_TIMER, PCC_VTHREAD_POOL_REUSES
        ) > 0;
    printf("%lld:%d\n", (long long)backend, ok);
    return ok ? 0 : (int)(10 + backend);
}
"""


@pytest.fixture(scope="module")
def _timer_cancel_exe(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("gc_vthread_timer_cancel")
    work_runtime = cached_pcc_python_runtime()
    src = tmp / "vthread_timer_cancel.c"
    src.write_text(textwrap.dedent(_SOURCE).lstrip(), encoding="utf-8")
    exe = tmp / "vthread_timer_cancel_bin"
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
    return str(exe)


@pytest.mark.parametrize("backend", ["0", "1", "2", "3", "4"])
def test_vthread_timer_cancel_is_immediate_and_root_safe(
    _timer_cancel_exe, backend
):
    run = subprocess.run(
        [_timer_cancel_exe, backend],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, (
        f"backend #{backend} rc={run.returncode}: "
        f"{run.stdout.strip()} {run.stderr.strip()[:400]}"
    )
    assert run.stdout.strip() == f"{backend}:1"


def test_production_timer_cancel_wiring_is_not_a_sorted_list() -> None:
    header = (RUNTIME_DIR / "include" / "py_runtime.h").read_text(
        encoding="utf-8"
    )
    assert "py_virtual_thread_cancel_timer" in header
