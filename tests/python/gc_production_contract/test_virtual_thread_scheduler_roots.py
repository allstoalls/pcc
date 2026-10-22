"""5-GC common production contract: virtual-thread scheduler roots.

Part of the 5-GC Production Equality Rule (docs/goal/goal-prompt.md G-track) and
the virtual-thread T-track. Virtual-thread scheduler queues are currently a
runtime ABI surface rather than a Python source construct, so this brick builds
a focused C probe against pcc's no-libpython runtime archive and runs the same
probe under PCC_GC_BACKEND 0..4.

The probe drops the last ordinary reference to a virtual thread after placing it
in the ready queue, timer queue, and IO-wait queue. A gc.collect() happens while
the virtual thread is only reachable from scheduler state; polling the scheduler
must return a virtual thread whose suspended continuation still contains its
pointer-bearing frame slot.
"""
from __future__ import annotations

import os
import subprocess
import textwrap

import pytest

from tests.runtime_build_cache import cached_pcc_python_runtime

from pathlib import Path


REPO_ROOT = Path(__file__).absolute().parents[3]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"


_SOURCE = r"""
#include "py_runtime.h"
#include "py_internal.h"

#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static void resume_a(void) {}

static PyObject *make_vthread(const char *label) {
    int32_t frame_map[1] = {1};
    PyObject *slots[1] = {0};
    PyObject *local = py_str_new(label, (int64_t)strlen(label));
    if (local == 0) return 0;
    pcc_gc_store_root(&slots[0], local);
    PyObject *cont = py_continuation_new(frame_map, slots, (void *)&resume_a);
    pcc_gc_store_root(&slots[0], 0);
    pcc_gc_release(local);
    if (cont == 0) return 0;
    PyObject *vt = py_virtual_thread_new(cont);
    pcc_gc_release(cont);
    return vt;
}

static int continuation_slot_matches(PyObject *vthread, const char *label) {
    PyVirtualThreadObject *vt = (PyVirtualThreadObject *)vthread;
    PyObject *cont = pcc_gc_load_ptr(vthread, &vt->continuation);
    if (cont == 0) return 0;
    PyObject *got = py_continuation_get_slot(cont, 0);
    PyObject *expected = py_str_new(label, (int64_t)strlen(label));
    int ok = got != 0 && expected != 0 && py_obj_eq(got, expected);
    py_decref(got);
    py_decref(expected);
    return ok;
}

static int check_ready_queue(void) {
    PyObject *vt = make_vthread("ready-root");
    if (vt == 0) return 0;
    if (py_virtual_thread_start(vt) != 0) return 0;
    pcc_gc_release(vt);
    (void)pcc_gc_collect(0);
    PyObject *ready = py_virtual_thread_poll_ready();
    if (ready == 0) return 0;
    int ok = continuation_slot_matches(ready, "ready-root");
    py_virtual_thread_complete(ready, py_None);
    py_decref(ready);
    return ok;
}

static int check_timer_queue(void) {
    PyObject *vt = make_vthread("timer-root");
    if (vt == 0) return 0;
    if (py_virtual_thread_sleep(vt, 20) != 0) return 0;
    pcc_gc_release(vt);
    (void)pcc_gc_collect(0);
    usleep(30000);
    if (py_virtual_thread_poll_timers() != 1) return 0;
    PyObject *ready = py_virtual_thread_poll_ready();
    if (ready == 0) return 0;
    int ok = continuation_slot_matches(ready, "timer-root");
    py_virtual_thread_complete(ready, py_None);
    py_decref(ready);
    return ok;
}

static int check_io_queue(void) {
    int fds[2];
    if (pipe(fds) != 0) return 0;
    PyObject *vt = make_vthread("io-root");
    if (vt == 0) return 0;
    if (py_virtual_thread_block_on_fd(vt, fds[0], POLLIN, 100) != 0) {
        close(fds[0]);
        close(fds[1]);
        return 0;
    }
    pcc_gc_release(vt);
    (void)pcc_gc_collect(0);
    if (write(fds[1], "x", 1) != 1) {
        close(fds[0]);
        close(fds[1]);
        return 0;
    }
    if (py_virtual_thread_poll_io(0) != 1) {
        close(fds[0]);
        close(fds[1]);
        return 0;
    }
    PyObject *ready = py_virtual_thread_poll_ready();
    if (ready == 0) {
        close(fds[0]);
        close(fds[1]);
        return 0;
    }
    int ok = continuation_slot_matches(ready, "io-root");
    py_virtual_thread_complete(ready, py_None);
    py_decref(ready);
    close(fds[0]);
    close(fds[1]);
    return ok;
}

int main(int argc, char **argv) {
    if (argc != 2) return 2;
    int64_t backend = (int64_t)atoll(argv[1]);
    if (pcc_gc_set_backend(backend) != 0) return 3;
    pcc_gc_telemetry_reset();
    int ready = check_ready_queue();
    int timer = check_timer_queue();
    int io = check_io_queue();
    int counts = py_virtual_thread_ready_count() == 0
        && py_virtual_thread_timer_count() == 0
        && py_virtual_thread_io_wait_count() == 0;
    printf("%lld:%d:%d:%d:%d\n", (long long)backend, ready, timer, io, counts);
    return ready && timer && io && counts ? 0 : (int)(10 + backend);
}
"""


@pytest.fixture(scope="module")
def _virtual_thread_scheduler_exe(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("gc_vthread_scheduler")
    work_runtime = cached_pcc_python_runtime()

    src = tmp / "virtual_thread_scheduler_roots.c"
    src.write_text(textwrap.dedent(_SOURCE).lstrip(), encoding="utf-8")
    exe = tmp / "virtual_thread_scheduler_roots_bin"
    cc = os.environ.get("CC", "cc")
    build = subprocess.run(
        [
            cc,
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
def test_virtual_thread_scheduler_roots(_virtual_thread_scheduler_exe, backend):
    run = subprocess.run(
        [_virtual_thread_scheduler_exe, backend],
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert run.returncode == 0, (
        f"backend #{backend} rc={run.returncode}: {run.stderr.strip()[:200]}"
    )
    assert run.stdout.strip() == f"{backend}:1:1:1:1", run.stdout
