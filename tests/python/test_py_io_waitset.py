"""Gateway-facing backend labels and Linux epoll contract tests."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
import sys


REPO = Path(__file__).resolve().parents[2]
MIRROR_SOURCE = REPO / "pcc" / "runtime" / "py" / "py_io_waitset.py"
PRODUCTION_SOURCE = (
    REPO / "pcc" / "runtime" / "py" / "freestanding_io_waitset.py"
)
UNSAFE_LOWERING_SOURCE = (
    REPO / "pcc" / "frontends" / "python" / "codegen" / "unsafe_lowering.py"
)


def _load_mirror():
    name = "pcc_gateway_py_io_waitset"
    spec = importlib.util.spec_from_file_location(name, MIRROR_SOURCE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_epoll_model_filters_interest_and_ready_wins_deadline() -> None:
    waitset_module = _load_mirror()
    waitset = waitset_module.EpollIoWaitSet()
    assert waitset.add(10, waitset_module.PCC_IO_POLLIN, 50, 1) == 0
    waitset.set_ready(
        10,
        waitset_module.PCC_IO_POLLIN | waitset_module.PCC_IO_POLLOUT,
    )
    result = waitset.wait(50)
    assert [(event.fd, event.events) for event in result.ready] == [
        (10, waitset_module.PCC_IO_POLLIN)
    ]
    assert result.timed_out == []
    assert waitset.count() == 0


def test_epoll_model_deadline_remove_and_error_delivery() -> None:
    waitset_module = _load_mirror()
    waitset = waitset_module.EpollIoWaitSet()
    waitset.add(11, waitset_module.PCC_IO_POLLOUT, 10, 0)
    assert waitset.wait(9).timed_out == []
    assert waitset.wait(10).timed_out == [11]
    waitset.add(12, waitset_module.PCC_IO_POLLIN, -1, 0)
    waitset.set_ready(12, waitset_module.PCC_IO_POLLHUP)
    result = waitset.wait(20)
    assert result.ready[0].events == waitset_module.PCC_IO_POLLHUP
    waitset.add(13, waitset_module.PCC_IO_POLLIN, -1, 0)
    assert waitset.remove(13) == 1
    assert waitset.remove(13) == 0


def test_epoll_model_rejects_stale_fd_generation() -> None:
    waitset_module = _load_mirror()
    waitset = waitset_module.EpollIoWaitSet()
    waitset.add(41, waitset_module.PCC_IO_POLLIN, -1, 0)
    stale_generation = waitset.generation(41)
    assert waitset.remove(41) == 1
    waitset.add(41, waitset_module.PCC_IO_POLLIN, -1, 0)
    assert waitset.generation(41) != stale_generation
    waitset.set_ready_token(
        41, stale_generation, waitset_module.PCC_IO_POLLIN
    )
    assert waitset.wait(0).ready == []
    waitset.set_ready(41, waitset_module.PCC_IO_POLLIN)
    assert [event.fd for event in waitset.wait(0).ready] == [41]


def test_backend_selection_never_labels_poll_as_epoll() -> None:
    waitset_module = _load_mirror()
    assert waitset_module.backend_label(0) == "poll"
    assert waitset_module.backend_label(1) == "kqueue"
    assert waitset_module.backend_label(2) == "epoll"
    assert waitset_module.backend_label(99) == "unknown"
    assert waitset_module.default_backend("darwin") == 1
    assert waitset_module.default_backend("linux") == 0
    assert waitset_module.default_backend("linux", live_epoll=1) == 2
    assert waitset_module.epoll_available() == 0
    skip = waitset_module.real_epoll_skip()
    assert skip[0] == "io_waitset.real_epoll"
    assert "deterministic host mirror" in skip[1]


def test_production_epoll_surface_owns_live_syscalls_and_generation() -> None:
    source = PRODUCTION_SOURCE.read_text(encoding="utf-8")
    assert (
        '@c_abi_typed_export("pcc_io_waitset_epoll_available", "i32", ())'
        in source
    )
    assert (
        '@c_abi_typed_export('
        '"pcc_io_waitset_real_epoll_skip", "i32", ("ptr",))'
        in source
    )
    assert '@c_abi_export("pcc_io_waitset_backend_label")' in source
    assert '@c_abi_export("pcc_io_waitset_default_backend")' in source
    assert 'epoll_create1(524288)' in source
    assert "epoll_ctl(" in source
    assert "epoll_wait(" in source
    assert '@c_abi_export("pcc_io_waitset_wait_until")' in source
    assert '@c_abi_export("pcc_io_waitset_interrupt")' in source
    assert '@c_abi_export("pcc_io_waitset_wait_prepare")' in source
    assert '@c_abi_export("pcc_io_waitset_wait_block")' in source
    assert '@c_abi_export("pcc_io_waitset_wait_finish")' in source
    assert "pcc_io_waitset_find_slot_generation" in source
    assert "generation * 4294967296" in source
    assert "bounded_live > 256" in source
    assert "eventfd_create(0, 526336)" in source
    assert "_write_user_kevent(trigger, 0, 16777216)" in source


def test_compiler_owns_epoll_syscall_lowering() -> None:
    from pcc.frontends.python.codegen.unsafe_lowering import UNSAFE_INTRINSICS

    assert {
        "epoll_create1",
        "epoll_ctl",
        "epoll_wait",
        "eventfd_create",
    } <= UNSAFE_INTRINSICS
    source = UNSAFE_LOWERING_SOURCE.read_text(encoding="utf-8")
    assert 'ir.Constant(_I64, 291)' in source
    assert 'ir.Constant(_I64, 233)' in source
    assert 'ir.Constant(_I64, 232)' in source
    assert 'ir.Constant(_I64, 290)' in source
    branch = source.split('if intrinsic == "epoll_create1"', 1)[1].split(
        'if intrinsic == "thread_safepoint"', 1
    )[0]
    assert '_target_sys_platform_text() == "linux"' in branch
    assert '_target_machine_text() == "x86_64"' in branch
    assert "return ir.Constant(_I64, -38)" in branch


def test_production_archive_exports_backend_labels_and_live_epoll(
    tmp_path: Path, pcc_runtime_archive: Path
) -> None:
    source = tmp_path / "gateway_waitset_backend_probe.c"
    executable = tmp_path / "gateway_waitset_backend_probe"
    source.write_text(
        r'''
#include "py_io_waitset.h"
#include <stdint.h>
#include <string.h>
#include <unistd.h>

int main(void) {
    if (strcmp(pcc_io_waitset_backend_label(0), "poll") != 0) return 1;
    if (strcmp(pcc_io_waitset_backend_label(1), "kqueue") != 0) return 2;
    if (strcmp(pcc_io_waitset_backend_label(2), "epoll") != 0) return 3;
    if (strcmp(pcc_io_waitset_backend_label(99), "unknown") != 0) return 4;
    PccIoWaitSetSkip skip;
#ifdef __APPLE__
    if (pcc_io_waitset_epoll_available() != 0) return 5;
    if (pcc_io_waitset_real_epoll_skip(&skip) != 1) return 6;
    if (strcmp(skip.path, "io_waitset.real_epoll") != 0) return 7;
    PccIoWaitSet ws;
    if (pcc_io_waitset_init(&ws, PCC_IO_WAITSET_BACKEND_EPOLL) != -1) return 8;
    if (pcc_io_waitset_default_backend() != 1) return 9;
#elif defined(__linux__) && defined(__x86_64__)
    if (pcc_io_waitset_epoll_available() != 1) return 10;
    if (pcc_io_waitset_real_epoll_skip(&skip) != 0) return 11;
    if (pcc_io_waitset_default_backend() != PCC_IO_WAITSET_BACKEND_EPOLL)
        return 12;
    int pipes[2];
    if (pipe(pipes) != 0) return 13;
    PccIoWaitSet ws;
    PccIoWaitResult result;
    if (pcc_io_waitset_init(&ws, PCC_IO_WAITSET_BACKEND_EPOLL) != 0) return 14;
    if (pcc_io_waitset_add(&ws, pipes[0], PCC_IO_POLLIN, -1, 0) != 0)
        return 15;
    if (write(pipes[1], "x", 1) != 1) return 16;
    if (pcc_io_waitset_wait(&ws, 0, &result) != 0) return 17;
    if (result.ready_len != 1 || result.ready[0].fd != pipes[0]) return 18;
    if ((result.ready[0].events & PCC_IO_POLLIN) == 0) return 19;
    pcc_io_waitset_dispose(&ws);
    close(pipes[0]);
    close(pipes[1]);
#else
    if (pcc_io_waitset_epoll_available() != 0) return 20;
    if (pcc_io_waitset_real_epoll_skip(&skip) != 1) return 21;
#endif
    return 0;
}
''',
        encoding="utf-8",
    )
    built = subprocess.run(
        [
            "clang",
            f"-I{REPO / 'pcc' / 'runtime' / 'src'}",
            str(source),
            str(pcc_runtime_archive),
            "-lm",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_live_waitset_retries_eintr_against_absolute_deadline(
    tmp_path: Path, pcc_runtime_archive: Path
) -> None:
    source = tmp_path / "gateway_waitset_eintr_probe.c"
    executable = tmp_path / "gateway_waitset_eintr_probe"
    source.write_text(
        r'''
#define _POSIX_C_SOURCE 200809L
#include "py_io_waitset.h"
#include <signal.h>
#include <stdint.h>
#include <sys/time.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

static volatile sig_atomic_t interrupted = 0;
static void note_signal(int signum) { (void)signum; interrupted = 1; }
static int64_t monotonic_ms(void) {
    struct timespec value;
    if (clock_gettime(CLOCK_MONOTONIC, &value) != 0) return -1;
    return (int64_t)value.tv_sec * 1000 + value.tv_nsec / 1000000;
}

int main(void) {
    int64_t backend = pcc_io_waitset_default_backend();
    if (backend == PCC_IO_WAITSET_BACKEND_POLL) return 0;
    int fds[2];
    if (pipe(fds) != 0) return 1;
    PccIoWaitSet ws;
    PccIoWaitResult result;
    if (pcc_io_waitset_init(&ws, (PccIoWaitSetBackend)backend) != 0) return 2;
    int64_t now = monotonic_ms();
    if (now < 0) return 3;
    if (pcc_io_waitset_add(&ws, fds[0], PCC_IO_POLLIN, now + 500, 0) != 0)
        return 4;

    struct sigaction action = {0};
    action.sa_handler = note_signal;
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGALRM, &action, NULL) != 0) return 5;
    struct itimerval timer = {0};
    timer.it_value.tv_usec = 10000;
    if (setitimer(ITIMER_REAL, &timer, NULL) != 0) return 6;

    pid_t child = fork();
    if (child < 0) return 7;
    if (child == 0) {
        struct timespec delay = {0, 50000000};
        nanosleep(&delay, NULL);
        _exit(write(fds[1], "x", 1) == 1 ? 0 : 20);
    }
    if (pcc_io_waitset_wait_until(&ws, now, now + 500, &result) != 0)
        return 8;
    if (!interrupted) return 9;
    if (result.ready_len != 1 || result.ready[0].fd != fds[0]) return 10;
    int status = 0;
    if (waitpid(child, &status, 0) != child) return 11;
    if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) return 12;
    int idle[2];
    if (pipe(idle) != 0) return 13;
    now = monotonic_ms();
    if (pcc_io_waitset_add(&ws, idle[0], PCC_IO_POLLIN, now + 30, 0) != 0)
        return 14;
    if (pcc_io_waitset_wait_until(&ws, now, now + 200, &result) != 0)
        return 15;
    if (result.ready_len != 0 || result.timeout_len != 1) return 16;
    if (result.timed_out[0] != idle[0]) return 17;
    pcc_io_waitset_dispose(&ws);
    close(fds[0]);
    close(fds[1]);
    close(idle[0]);
    close(idle[1]);
    return 0;
}
''',
        encoding="utf-8",
    )
    built = subprocess.run(
        [
            "clang",
            f"-I{REPO / 'pcc' / 'runtime' / 'src'}",
            str(source),
            str(pcc_runtime_archive),
            "-lm",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, run.stdout + run.stderr


def test_backend_label_expectation_matches_host_platform() -> None:
    waitset_module = _load_mirror()
    if sys.platform == "darwin":
        assert waitset_module.backend_label(
            waitset_module.default_backend("darwin")
        ) == "kqueue"
    elif sys.platform.startswith("linux"):
        # The host-safe mirror is deliberately not a syscall owner.  The
        # production freestanding module independently selects live epoll for
        # the supported Linux x86_64 target.
        assert waitset_module.backend_label(
            waitset_module.default_backend("linux")
        ) == "poll"
