"""Production vthread IO uses kqueue/epoll with an interruptible wait owner."""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests.runtime_build_cache import cached_threaded_pcc_python_runtime


REPO_ROOT = Path(__file__).absolute().parents[3]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"


_SOURCE = r"""
#define _POSIX_C_SOURCE 200809L
#include "py_runtime.h"
#include "py_internal.h"
#include "py_io_waitset.h"

#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/socket.h>
#include <unistd.h>

static PyObject *new_vthread(void) {
    return py_virtual_thread_new(py_None);
}

static int finish_one_ready(void) {
    PyObject *ready = py_virtual_thread_poll_ready();
    if (ready == NULL) return 0;
    if (((PyVirtualThreadObject *)ready)->io_entry != NULL) return 0;
    if (py_virtual_thread_complete(ready, py_None) != 0) return 0;
    pcc_gc_release(ready);
    return 1;
}

static int check_pipe_ready(void) {
    int fds[2];
    if (pipe(fds) != 0) return 0;
    PyObject *vt = new_vthread();
    if (vt == NULL) return 0;
    if (py_virtual_thread_block_on_fd(vt, fds[0], POLLIN, -1) != 0) return 0;
    if (((PyVirtualThreadObject *)vt)->io_entry == NULL) return 0;
    pcc_gc_release(vt);
    (void)pcc_gc_collect(0);
    if (pcc_gc_scheduler_root_count() != 1) return 0;
    if (write(fds[1], "p", 1) != 1) return 0;
    if (py_virtual_thread_poll_io(0) != 1) return 0;
    if (py_virtual_thread_io_wait_count() != 0) return 0;
    if (pcc_gc_scheduler_root_count() != 1) return 0;
    if (!finish_one_ready()) return 0;
    close(fds[0]);
    close(fds[1]);
    return pcc_gc_scheduler_root_count() == 0;
}

static int check_socket_ready(void) {
    int fds[2];
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, fds) != 0) return 0;
    PyObject *vt = new_vthread();
    if (vt == NULL) return 0;
    if (py_virtual_thread_block_on_fd(vt, fds[0], POLLIN, -1) != 0) return 0;
    pcc_gc_release(vt);
    (void)pcc_gc_collect(0);
    if (write(fds[1], "s", 1) != 1) return 0;
    if (py_virtual_thread_poll_io(0) != 1) return 0;
    if (!finish_one_ready()) return 0;
    close(fds[0]);
    close(fds[1]);
    return pcc_gc_scheduler_root_count() == 0;
}

static int check_timeout(void) {
    int fds[2];
    if (pipe(fds) != 0) return 0;
    PyObject *vt = new_vthread();
    if (vt == NULL) return 0;
    if (py_virtual_thread_block_on_fd(vt, fds[0], POLLIN, 3) != 0) return 0;
    pcc_gc_release(vt);
    (void)pcc_gc_collect(0);
    (void)poll(NULL, 0, 8);
    if (py_virtual_thread_poll_io(0) != 1) return 0;
    if (!finish_one_ready()) return 0;
    close(fds[0]);
    close(fds[1]);
    return pcc_gc_scheduler_root_count() == 0;
}

static int check_same_fd_waiters(void) {
    int fds[2];
    if (pipe(fds) != 0) return 0;
    PyObject *first = new_vthread();
    PyObject *second = new_vthread();
    if (first == NULL || second == NULL) return 0;
    if (py_virtual_thread_block_on_fd(first, fds[0], POLLIN, -1) != 0) return 0;
    if (py_virtual_thread_block_on_fd(second, fds[0], POLLIN, -1) != 0) return 0;
    pcc_gc_release(first);
    pcc_gc_release(second);
    (void)pcc_gc_collect(0);
    if (py_virtual_thread_io_wait_count() != 2) return 0;
    if (pcc_gc_scheduler_root_count() != 2) return 0;
    if (write(fds[1], "m", 1) != 1) return 0;
    if (py_virtual_thread_poll_io(0) != 2) return 0;
    if (py_virtual_thread_ready_count() != 2) return 0;
    if (!finish_one_ready() || !finish_one_ready()) return 0;
    if (py_virtual_thread_poll_ready() != NULL) return 0;
    close(fds[0]);
    close(fds[1]);
    return py_virtual_thread_io_wait_count() == 0
        && pcc_gc_scheduler_root_count() == 0;
}

static int check_complete_cancels_wait(void) {
    int fds[2];
    if (pipe(fds) != 0) return 0;
    PyObject *vt = new_vthread();
    if (vt == NULL) return 0;
    if (py_virtual_thread_block_on_fd(vt, fds[0], POLLIN, -1) != 0) return 0;
    if (pcc_gc_scheduler_root_count() != 1) return 0;
    if (py_virtual_thread_complete(vt, py_None) != 0) return 0;
    if (((PyVirtualThreadObject *)vt)->io_entry != NULL) return 0;
    if (py_virtual_thread_io_wait_count() != 0) return 0;
    if (pcc_gc_scheduler_root_count() != 0) return 0;
    pcc_gc_release(vt);
    if (write(fds[1], "x", 1) != 1) return 0;
    if (py_virtual_thread_poll_io(0) != 0) return 0;
    close(fds[0]);
    close(fds[1]);
    return py_virtual_thread_poll_ready() == NULL;
}

typedef struct LiveWaitWorker {
    int64_t result;
} LiveWaitWorker;

static void *live_wait_worker(void *opaque) {
    LiveWaitWorker *worker = (LiveWaitWorker *)opaque;
    worker->result = py_virtual_thread_poll_io(-1);
    return NULL;
}

static int wait_until_live_wait_active(void) {
    for (int i = 0; i < 500; i++) {
        if (py_virtual_thread_io_wait_active() == 1) return 1;
        (void)poll(NULL, 0, 1);
    }
    return 0;
}

static int check_live_wait_registration_and_cancel_interrupt(void) {
    if (py_virtual_thread_io_backend() == PCC_VTHREAD_IO_BACKEND_POLL) return 1;
    if (!pcc_threads_enabled()) return 1;
    int first_pipe[2];
    int second_pipe[2];
    if (pipe(first_pipe) != 0 || pipe(second_pipe) != 0) return 0;
    PyObject *first = new_vthread();
    PyObject *second = new_vthread();
    if (first == NULL || second == NULL) return 0;
    if (py_virtual_thread_block_on_fd(
        first, first_pipe[0], POLLIN, -1
    ) != 0) return 0;

    /* A concurrent registration must wake the single carrier that owns the
     * infinite live wait, without waiting for its fd to become ready. */
    LiveWaitWorker add_worker = {-99};
    PccThreadHandle *thread = NULL;
    if (pcc_thread_start(&thread, live_wait_worker, &add_worker) != 0) return 0;
    if (!wait_until_live_wait_active()) return 0;
    if (py_virtual_thread_block_on_fd(
        second, second_pipe[0], POLLIN, -1
    ) != 0) return 0;
    if (pcc_thread_join(thread, NULL) != 0) return 0;
    if (add_worker.result != 0 || py_virtual_thread_io_wait_active() != 0)
        return 0;
    if (py_virtual_thread_io_wait_count() != 2) return 0;

    /* Cancellation exercises the same interrupt channel from a second
     * scheduler mutation while another carrier is blocked indefinitely. */
    LiveWaitWorker cancel_worker = {-99};
    thread = NULL;
    if (pcc_thread_start(&thread, live_wait_worker, &cancel_worker) != 0)
        return 0;
    if (!wait_until_live_wait_active()) return 0;
    if (py_virtual_thread_complete(first, py_None) != 0) return 0;
    if (pcc_thread_join(thread, NULL) != 0) return 0;
    if (cancel_worker.result != 0 || py_virtual_thread_io_wait_active() != 0)
        return 0;
    if (py_virtual_thread_io_wait_count() != 1) return 0;
    if (py_virtual_thread_complete(second, py_None) != 0) return 0;
    if (py_virtual_thread_io_wait_count() != 0) return 0;

    pcc_gc_release(first);
    pcc_gc_release(second);
    if (py_virtual_thread_poll_ready() != NULL) return 0;
    close(first_pipe[0]);
    close(first_pipe[1]);
    close(second_pipe[0]);
    close(second_pipe[1]);
    return pcc_gc_scheduler_root_count() == 0;
}

static int check_pool_stop_restart_preserves_parked_io(void) {
    if (!pcc_threads_enabled()) return 1;
    int fds[2];
    if (pipe(fds) != 0) return 0;
    if (py_virtual_thread_carrier_pool_start(2) != 2) return 0;
    PyObject *vt = new_vthread();
    if (vt == NULL) return 0;
    if (py_virtual_thread_block_on_fd(vt, fds[0], POLLIN, -1) != 0) return 0;
    pcc_gc_release(vt);
    (void)pcc_gc_collect(0);
    if (py_virtual_thread_io_wait_count() != 1) return 0;
    if (pcc_gc_scheduler_root_count() != 1) return 0;

    /* Stop owns the initialized waitset lifetime, but the parked IO node and
     * its scheduler root remain the durable registration source. */
    if (py_virtual_thread_carrier_pool_stop() != 2) return 0;
    if (py_virtual_thread_io_wait_active() != 0) return 0;
    if (py_virtual_thread_io_wait_count() != 1) return 0;
    if (pcc_gc_scheduler_root_count() != 1) return 0;

    /* Starting a fresh pool must rebuild that parked fd before any carrier
     * runs.  A second clean stop must preserve the same root exactly once. */
    if (py_virtual_thread_carrier_pool_start(2) != 2) return 0;
    if (py_virtual_thread_carrier_pool_stop() != 2) return 0;
    if (py_virtual_thread_io_wait_count() != 1) return 0;
    if (pcc_gc_scheduler_root_count() != 1) return 0;

    if (write(fds[1], "r", 1) != 1) return 0;
    if (py_virtual_thread_poll_io(0) != 1) return 0;
    if (!finish_one_ready()) return 0;
    close(fds[0]);
    close(fds[1]);
    return py_virtual_thread_io_wait_count() == 0
        && pcc_gc_scheduler_root_count() == 0;
}

int main(int argc, char **argv) {
    if (argc != 3) return 2;
    int64_t backend = (int64_t)atoll(argv[1]);
    const char *mode = argv[2];
    if (setenv("PCC_VTHREAD_IO_BACKEND", mode, 1) != 0) return 3;
    if (pcc_gc_set_backend(backend) != 0) return 4;
    int64_t expected = mode[0] == 'p'
        ? PCC_VTHREAD_IO_BACKEND_POLL
        : (mode[0] == 'e'
            ? (pcc_io_waitset_epoll_available()
                ? PCC_VTHREAD_IO_BACKEND_EPOLL
                : PCC_VTHREAD_IO_BACKEND_POLL)
            : (pcc_io_waitset_kqueue_available()
            ? PCC_VTHREAD_IO_BACKEND_KQUEUE
            : (pcc_io_waitset_epoll_available()
                ? PCC_VTHREAD_IO_BACKEND_EPOLL
                : PCC_VTHREAD_IO_BACKEND_POLL)));
    int ok = py_virtual_thread_io_backend() == expected
        && check_pipe_ready()
        && check_socket_ready()
        && check_timeout()
        && check_same_fd_waiters()
        && check_complete_cancels_wait()
        && check_live_wait_registration_and_cancel_interrupt()
        && check_pool_stop_restart_preserves_parked_io()
        && py_virtual_thread_io_backend() == expected
        && py_virtual_thread_ready_count() == 0
        && py_virtual_thread_io_wait_count() == 0
        && pcc_gc_scheduler_root_count() == 0
        && py_virtual_thread_node_pool_stat(
            PCC_VTHREAD_NODE_IO, PCC_VTHREAD_POOL_REUSES
        ) > 0;
    printf("%lld:%s:%lld:%d\n", (long long)backend, mode,
        (long long)py_virtual_thread_io_backend(), ok);
    return ok ? 0 : (int)(10 + backend);
}
"""


@pytest.fixture(scope="module")
def _io_waitset_runtime_exe(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("gc_vthread_io_waitset_runtime")
    work_runtime = cached_threaded_pcc_python_runtime()
    src = tmp / "vthread_io_waitset_runtime.c"
    src.write_text(textwrap.dedent(_SOURCE).lstrip(), encoding="utf-8")
    exe = tmp / "vthread_io_waitset_runtime_bin"
    build = subprocess.run(
        [
            os.environ.get("CC", "cc"),
            "-std=c11",
            f"-I{work_runtime / 'include'}",
            f"-I{work_runtime / 'src'}",
            str(src),
            str(work_runtime / "libpy_runtime_pcc_py.a"),
            "-pthread",
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
@pytest.mark.parametrize("mode", ["auto", "poll", "epoll"])
def test_production_io_waitset_modes_preserve_roots(
    _io_waitset_runtime_exe, backend, mode
):
    run = subprocess.run(
        [_io_waitset_runtime_exe, backend, mode],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, (
        f"GC{backend} mode={mode} rc={run.returncode}: "
        f"{run.stdout.strip()} {run.stderr.strip()[:400]}"
    )
    fields = run.stdout.strip().split(":")
    assert fields[:2] == [backend, mode]
    assert fields[-1] == "1"


def test_scheduler_owns_waitset_instead_of_per_entry_poll() -> None:
    header = (RUNTIME_DIR / "include" / "py_runtime.h").read_text(
        encoding="utf-8"
    )
    assert "py_virtual_thread_io_backend" in header
    assert "py_virtual_thread_io_wait_active" in header


def test_current_pcc1_scheduler_releases_lock_around_live_wait() -> None:
    source = (RUNTIME_DIR / "py" / "py_virtual_thread_runtime.py").read_text(
        encoding="utf-8"
    )
    poll_io = source.split(
        '@c_abi_export("py_virtual_thread_poll_io")', 1
    )[1].split('@c_abi_export("py_virtual_thread_io_wait_count")', 1)[0]
    block_at = poll_io.index("pcc_io_waitset_wait_block")
    assert poll_io.rfind("_scheduler_unlock()", 0, block_at) >= 0
    assert poll_io.find("_scheduler_lock()", block_at) > block_at
    assert "pcc_io_waitset_wait_prepare" in poll_io
    assert "pcc_io_waitset_wait_finish" in poll_io
    refresh = source.split("def _io_refresh", 1)[1].split(
        "def _io_cancel", 1
    )[0]
    assert "_io_interrupt_locked()" in refresh
    assert '@c_abi_export("py_virtual_thread_io_wait_active")' in source


def test_carrier_stop_disposes_after_join_and_restart_rehydrates_roots() -> None:
    py_source = (
        RUNTIME_DIR / "py" / "py_virtual_thread_runtime.py"
    ).read_text(encoding="utf-8")

    py_stop = py_source.split(
        '@c_abi_export("py_virtual_thread_carrier_pool_stop")', 1
    )[1].split('@c_abi_export("py_virtual_thread_current")', 1)[0]
    assert py_stop.index("pcc_thread_join") < py_stop.index(
        "_waitset_dispose_locked"
    )
    assert py_stop.count("_waitset_dispose_locked") == 1
    assert "if join_failed != 0:" in py_stop
    assert "pcc_vthread_persistent_cleanup_active_py" in py_stop
    assert 'global_store_ptr("pcc_vthread_io_head_py", null())' not in py_stop
    assert "_io_refresh_registered(load_i64(node, 8))" in py_source
    py_start = py_source.split(
        '@c_abi_export("py_virtual_thread_carrier_pool_start")', 1
    )[1].split(
        '@c_abi_export("py_virtual_thread_carrier_pool_stop")', 1
    )[0]
    assert py_start.index("_waitset_init()") < py_start.index(
        "pcc_thread_start"
    )


def test_waitset_dispose_resets_backend_generation_and_wake_state() -> None:
    py_source = (
        RUNTIME_DIR / "py" / "freestanding_io_waitset.py"
    ).read_text(encoding="utf-8")
    py_dispose = py_source.split(
        '@c_abi_export("pcc_io_waitset_dispose")', 1
    )[1].split('@c_abi_export("pcc_io_waitset_interrupt")', 1)[0]
    assert "store_i32(ws, 76, 0)" in py_dispose
    assert "store_i32(ws, 80, -1)" in py_dispose
    assert "store_i32(ws, 0, 0)" in py_dispose
