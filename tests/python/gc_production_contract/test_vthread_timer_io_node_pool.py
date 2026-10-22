"""Timer/IO vthread nodes are bounded, reusable, five-GC-safe roots."""

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

#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

static PyObject *new_vthread(void) {
    return py_virtual_thread_new(py_None);
}

static int timer_round(void) {
    PyObject *vt = new_vthread();
    if (vt == NULL) return 0;
    if (py_virtual_thread_sleep(vt, 1) != 0) return 0;
    pcc_gc_release(vt);
    if (pcc_gc_scheduler_root_count() != 1) return 0;
    (void)pcc_gc_collect(0);
    if (pcc_gc_scheduler_root_count() != 1) return 0;
    (void)poll(NULL, 0, 2);
    if (py_virtual_thread_poll_timers() != 1) return 0;
    if (pcc_gc_scheduler_root_count() != 1) return 0;
    PyObject *ready = py_virtual_thread_poll_ready();
    if (ready == NULL) return 0;
    if (pcc_gc_scheduler_root_count() != 0) return 0;
    if (py_virtual_thread_complete(ready, py_None) != 0) return 0;
    pcc_gc_release(ready);
    return 1;
}

static int io_round(int read_fd) {
    PyObject *vt = new_vthread();
    if (vt == NULL) return 0;
    if (py_virtual_thread_block_on_fd(vt, read_fd, POLLIN, 1) != 0) return 0;
    pcc_gc_release(vt);
    if (pcc_gc_scheduler_root_count() != 1) return 0;
    (void)pcc_gc_collect(0);
    if (pcc_gc_scheduler_root_count() != 1) return 0;
    (void)poll(NULL, 0, 2);
    if (py_virtual_thread_poll_io(0) != 1) return 0;
    if (pcc_gc_scheduler_root_count() != 1) return 0;
    PyObject *ready = py_virtual_thread_poll_ready();
    if (ready == NULL) return 0;
    if (pcc_gc_scheduler_root_count() != 0) return 0;
    if (py_virtual_thread_complete(ready, py_None) != 0) return 0;
    pcc_gc_release(ready);
    return 1;
}

static int pool_reused(int64_t family) {
    int64_t allocations = py_virtual_thread_node_pool_stat(
        family, PCC_VTHREAD_POOL_ALLOCATIONS
    );
    int64_t reuses = py_virtual_thread_node_pool_stat(
        family, PCC_VTHREAD_POOL_REUSES
    );
    int64_t cached = py_virtual_thread_node_pool_stat(
        family, PCC_VTHREAD_POOL_CACHED
    );
    return allocations > 0 && reuses > 0 && cached > 0 && cached <= 4096;
}

int main(int argc, char **argv) {
    if (argc != 2) return 2;
    int64_t backend = (int64_t)atoll(argv[1]);
    if (pcc_gc_set_backend(backend) != 0) return 3;
    int fds[2];
    if (pipe(fds) != 0) return 4;
    for (int i = 0; i < 16; i++) {
        if (!timer_round()) return 10;
        if (!io_round(fds[0])) return 11;
    }
    close(fds[0]);
    close(fds[1]);
    int ok = pool_reused(PCC_VTHREAD_NODE_READY)
        && pool_reused(PCC_VTHREAD_NODE_TIMER)
        && pool_reused(PCC_VTHREAD_NODE_IO)
        && pcc_gc_scheduler_root_count() == 0
        && py_virtual_thread_ready_count() == 0
        && py_virtual_thread_timer_count() == 0
        && py_virtual_thread_io_wait_count() == 0;
    printf("%lld:%d:%lld:%lld:%lld\n",
        (long long)backend,
        ok,
        (long long)py_virtual_thread_node_pool_stat(
            PCC_VTHREAD_NODE_READY, PCC_VTHREAD_POOL_REUSES
        ),
        (long long)py_virtual_thread_node_pool_stat(
            PCC_VTHREAD_NODE_TIMER, PCC_VTHREAD_POOL_REUSES
        ),
        (long long)py_virtual_thread_node_pool_stat(
            PCC_VTHREAD_NODE_IO, PCC_VTHREAD_POOL_REUSES
        )
    );
    return ok ? 0 : (int)(20 + backend);
}
"""


@pytest.fixture(scope="module")
def _timer_io_pool_exe(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("gc_vthread_timer_io_pool")
    work_runtime = cached_pcc_python_runtime()
    src = tmp / "vthread_timer_io_node_pool.c"
    src.write_text(textwrap.dedent(_SOURCE).lstrip(), encoding="utf-8")
    exe = tmp / "vthread_timer_io_node_pool_bin"
    build = subprocess.run(
        [
            os.environ.get("CC", "cc"),
            "-std=c11",
            f"-I{work_runtime / 'include'}",
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
def test_vthread_timer_io_nodes_reuse_and_preserve_roots(
    _timer_io_pool_exe, backend
):
    run = subprocess.run(
        [_timer_io_pool_exe, backend],
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert run.returncode == 0, (
        f"backend #{backend} rc={run.returncode}: "
        f"{run.stdout.strip()} {run.stderr.strip()[:400]}"
    )
    fields = run.stdout.strip().split(":")
    assert fields[:2] == [backend, "1"]
    assert all(int(value) > 0 for value in fields[2:])
