"""Production vthread transitions emit a checkable root/effect event path."""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from tests.runtime_build_cache import cached_pcc_python_runtime

from pcc.diagnostics.contracts.runtime_effects import (
    ProductionVThreadEvent,
    ProductionVThreadEventKind,
    RuntimeEffect,
    check_production_vthread_events,
    compose_production_vthread_event_effects,
)


REPO_ROOT = Path(__file__).absolute().parents[3]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"


_SOURCE = r"""
#define _POSIX_C_SOURCE 200809L
#include "py_runtime.h"

#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

static PyObject *new_vthread(void) {
    return py_virtual_thread_new(py_None);
}

static int take_ready(PyObject *expected) {
    PyObject *ready = py_virtual_thread_poll_ready();
    if (ready == NULL || ready != expected) return 0;
    pcc_gc_release(ready);
    return 1;
}

static int exercise_path(void) {
    if (py_virtual_thread_effect_reset() != 0) return 0;
    PyObject *vt = new_vthread();
    if (vt == NULL) return 0;

    if (py_virtual_thread_start(vt) != 0 || !take_ready(vt)) return 0;
    if (py_virtual_thread_park(vt) != 0) return 0;
    if (py_virtual_thread_unpark(vt) != 0 || !take_ready(vt)) return 0;

    if (py_virtual_thread_sleep(vt, 2) != 0) return 0;
    (void)pcc_gc_collect(0);
    (void)poll(NULL, 0, 6);
    if (py_virtual_thread_poll_timers() != 1 || !take_ready(vt)) return 0;

    int fds[2];
    if (pipe(fds) != 0) return 0;
    if (py_virtual_thread_block_on_fd(vt, fds[0], POLLIN, -1) != 0) return 0;
    (void)pcc_gc_collect(0);
    if (write(fds[1], "r", 1) != 1) return 0;
    if (py_virtual_thread_poll_io(0) != 1 || !take_ready(vt)) return 0;
    char consumed = 0;
    if (read(fds[0], &consumed, 1) != 1 || consumed != 'r') return 0;

    if (py_virtual_thread_sleep(vt, 1000) != 0) return 0;
    if (py_virtual_thread_cancel_timer(vt) != 1) return 0;
    if (py_virtual_thread_block_on_fd(vt, fds[0], POLLIN, -1) != 0) return 0;
    if (py_virtual_thread_complete(vt, py_None) != 0) return 0;
    pcc_gc_release(vt);
    close(fds[0]);
    close(fds[1]);

    return py_virtual_thread_ready_count() == 0
        && py_virtual_thread_timer_count() == 0
        && py_virtual_thread_io_wait_count() == 0
        && pcc_gc_scheduler_root_count() == 0;
}

int main(int argc, char **argv) {
    if (argc != 2) return 2;
    if (setenv("PCC_VTHREAD_IO_BACKEND", "poll", 1) != 0) return 3;
    int64_t backend = (int64_t)atoll(argv[1]);
    if (pcc_gc_set_backend(backend) != 0) return 4;
    int ok = exercise_path();
    int64_t count = py_virtual_thread_effect_count();
    int64_t dropped = py_virtual_thread_effect_dropped();
    printf("%lld:%d:%lld:%lld\n", (long long)backend, ok,
        (long long)count, (long long)dropped);
    for (int64_t i = 0; i < count; i++) {
        printf("%lld,%lld,%lld,%lld\n",
            (long long)py_virtual_thread_effect_kind_at(i),
            (long long)py_virtual_thread_effect_detail_at(i),
            (long long)py_virtual_thread_effect_root_delta_at(i),
            (long long)py_virtual_thread_effect_state_at(i));
    }
    return ok && count > 0 && dropped == 0 ? 0 : (int)(10 + backend);
}
"""


@pytest.fixture(scope="module")
def _runtime_effect_exe(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("gc_vthread_runtime_effects")
    work_runtime = cached_pcc_python_runtime()
    src = tmp / "vthread_runtime_effect_events.c"
    src.write_text(textwrap.dedent(_SOURCE).lstrip(), encoding="utf-8")
    exe = tmp / "vthread_runtime_effect_events_bin"
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
def test_production_vthread_effect_path_is_balanced(_runtime_effect_exe, backend):
    run = subprocess.run(
        [_runtime_effect_exe, backend],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, (
        f"GC{backend} rc={run.returncode}: "
        f"{run.stdout.strip()} {run.stderr.strip()[:400]}"
    )
    lines = run.stdout.strip().splitlines()
    head = lines[0].split(":")
    assert head[:2] == [backend, "1"]
    assert int(head[2]) == len(lines) - 1
    assert head[3] == "0"
    events = []
    for line in lines[1:]:
        kind, detail, root_delta, state = (int(value) for value in line.split(","))
        events.append(
            ProductionVThreadEvent(
                ProductionVThreadEventKind(kind), detail, root_delta, state
            )
        )
    assert check_production_vthread_events(events) == ()
    observed = {event.kind for event in events}
    assert {
        ProductionVThreadEventKind.ROOT_ENTER,
        ProductionVThreadEventKind.ROOT_LEAVE,
        ProductionVThreadEventKind.START,
        ProductionVThreadEventKind.PARK,
        ProductionVThreadEventKind.UNPARK,
        ProductionVThreadEventKind.RESUME,
        ProductionVThreadEventKind.TIMER_PARK,
        ProductionVThreadEventKind.TIMER_WAKE,
        ProductionVThreadEventKind.IO_PARK,
        ProductionVThreadEventKind.IO_WAKE,
        ProductionVThreadEventKind.CANCEL_TIMER,
        ProductionVThreadEventKind.CANCEL_IO,
        ProductionVThreadEventKind.COMPLETE,
    }.issubset(observed)
    effects = compose_production_vthread_event_effects(events)
    assert {
        RuntimeEffect.SCHEDULER_ROOT_ENTER,
        RuntimeEffect.SCHEDULER_ROOT_LEAVE,
        RuntimeEffect.VTHREAD_START,
        RuntimeEffect.VTHREAD_PARK,
        RuntimeEffect.VTHREAD_RESUME,
        RuntimeEffect.VTHREAD_SLEEP,
        RuntimeEffect.VTHREAD_BLOCK_IO,
        RuntimeEffect.VTHREAD_CANCEL,
        RuntimeEffect.VTHREAD_COMPLETE,
    }.issubset(effects)


def test_production_event_abi_is_bounded_and_public() -> None:
    header = (RUNTIME_DIR / "include" / "py_runtime.h").read_text(
        encoding="utf-8"
    )
    assert "py_virtual_thread_effect_count" in header
    assert "PCC_VTHREAD_EFFECT_COMPLETE" in header
