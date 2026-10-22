"""Executable coverage for the production pcc-Python IO waitset owner."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from pcc.frontends.python import pipeline


REPO = Path(__file__).resolve().parents[2]
RUNTIME = REPO / "pcc" / "runtime"
WAITSET_SOURCE = RUNTIME / "py" / "freestanding_io_waitset.py"


def _compile_waitset_ir(tmp_path: Path) -> Path:
    output = tmp_path / "freestanding_io_waitset.ll"
    pipeline.compile_python(
        str(WAITSET_SOURCE),
        str(output),
        emit_llvm_only=True,
        libpython_mode="off",
        python_library=True,
    )
    return output


def test_production_pcc_python_waitset_poll_and_kqueue(
    tmp_path: Path, pcc_runtime_archive: Path
) -> None:
    source = tmp_path / "io_waitset_probe.c"
    executable = tmp_path / "io_waitset_probe"
    source.write_text(
        r'''
#include "py_io_waitset.h"
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <unistd.h>

_Static_assert(sizeof(PccIoWaitSet) == 88, "freestanding waitset ABI drift");
_Static_assert(sizeof(PccIoWaitBatch) == 64, "split wait batch ABI drift");

typedef struct LiveWaitContext {
    PccIoWaitSet *waitset;
    PccIoWaitBatch *batch;
    int result;
} LiveWaitContext;

static void *block_live_wait(void *opaque) {
    LiveWaitContext *context = (LiveWaitContext *)opaque;
    context->result = pcc_io_waitset_wait_block(
        context->waitset, context->batch
    );
    return NULL;
}

static int live_interrupt_contract(void) {
    int64_t backend = pcc_io_waitset_default_backend();
    if (backend == PCC_IO_WAITSET_BACKEND_POLL) return 0;
    int pipes[2];
    if (pipe(pipes) != 0) return 30;
    PccIoWaitSet ws;
    PccIoWaitBatch batch;
    PccIoWaitResult result;
    if (pcc_io_waitset_init(&ws, (PccIoWaitSetBackend)backend) != 0) return 31;
    if (pcc_io_waitset_add(&ws, pipes[0], PCC_IO_POLLIN, -1, 0) != 0)
        return 32;
    if (pcc_io_waitset_wait_prepare(&ws, 0, -1, &batch) != 0) return 33;
    LiveWaitContext context = {&ws, &batch, -99};
    pthread_t thread;
    if (pthread_create(&thread, NULL, block_live_wait, &context) != 0)
        return 34;
    /* The interrupt may race before or after kernel entry; eventfd and
     * EVFILT_USER both retain/coalesce the notification. */
    if (pcc_io_waitset_interrupt(&ws) != 0) return 35;
    if (pthread_join(thread, NULL) != 0) return 36;
    if (context.result != 0) return 37;
    if (pcc_io_waitset_wait_finish(&ws, &batch, &result) != 0) return 38;
    if (result.ready_len != 0 || result.timeout_len != 0) return 39;
    if (pcc_io_waitset_count(&ws) != 1) return 40;
    if (pcc_io_waitset_remove(&ws, pipes[0]) != 1) return 41;
    pcc_io_waitset_dispose(&ws);
    close(pipes[0]);
    close(pipes[1]);
    return 0;
}

static int poll_contract(void) {
    PccIoWaitSet ws;
    PccIoWaitResult result;
    if (pcc_io_waitset_init(&ws, PCC_IO_WAITSET_BACKEND_POLL) != 0) return 1;
    if (pcc_io_waitset_add(&ws, 31, PCC_IO_POLLIN, 5, 0) != 0) return 2;
    if (pcc_io_waitset_add(&ws, 32, PCC_IO_POLLOUT, 2, 0) != 0) return 3;
    pcc_io_waitset_set_ready(&ws, 31, PCC_IO_POLLIN | PCC_IO_POLLOUT);
    if (pcc_io_waitset_wait(&ws, 3, &result) != 0) return 4;
    if (result.ready_len != 1 || result.ready[0].fd != 31) return 5;
    if (result.ready[0].events != PCC_IO_POLLIN) return 6;
    if (result.timeout_len != 1 || result.timed_out[0] != 32) return 7;
    if (pcc_io_waitset_count(&ws) != 0) return 8;
    if (pcc_io_waitset_add(&ws, 33, PCC_IO_POLLIN, -1, 1) != 0) return 9;
    pcc_io_waitset_set_ready(&ws, 33, PCC_IO_POLLIN);
    pcc_io_waitset_clear_ready(&ws, 33);
    if (pcc_io_waitset_wait(&ws, 100, &result) != 0) return 10;
    if (result.ready_len != 0 || result.timeout_len != 0) return 11;
    if (pcc_io_waitset_remove(&ws, 33) != 1) return 12;
    if (pcc_io_waitset_remove(&ws, 33) != 0) return 13;
    pcc_io_waitset_dispose(&ws);
    return 0;
}

static int kqueue_contract(void) {
#ifdef __APPLE__
    int fds[2];
    PccIoWaitSet ws;
    PccIoWaitResult result;
    if (pcc_io_waitset_kqueue_available() != 1) return 20;
    if (pipe(fds) != 0) return 21;
    if (pcc_io_waitset_init(&ws, PCC_IO_WAITSET_BACKEND_KQUEUE) != 0) return 22;
    if (pcc_io_waitset_add(&ws, fds[0], PCC_IO_POLLIN, -1, 0) != 0) return 23;
    if (write(fds[1], "x", 1) != 1) return 24;
    if (pcc_io_waitset_wait(&ws, 0, &result) != 0) return 25;
    if (result.ready_len != 1 || result.ready[0].fd != fds[0]) return 26;
    if ((result.ready[0].events & PCC_IO_POLLIN) == 0) return 27;
    pcc_io_waitset_dispose(&ws);
    close(fds[0]);
    close(fds[1]);
#else
    PccIoWaitSetSkip skip;
    if (pcc_io_waitset_kqueue_available() != 0) return 28;
    if (pcc_io_waitset_real_kqueue_skip(&skip) != 1) return 29;
#endif
    return 0;
}

int main(void) {
    int rc = poll_contract();
    if (rc != 0) return rc;
    rc = kqueue_contract();
    if (rc != 0) return rc;
    rc = live_interrupt_contract();
    if (rc != 0) return rc;
    puts("io-waitset-ok");
    return 0;
}
''',
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            "clang",
            "-std=c11",
            "-pthread",
            f"-I{RUNTIME / 'src'}",
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
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout == "io-waitset-ok\n"
    if sys.platform == "darwin":
        assert "kqueue" not in run.stderr


def test_waitset_port_executes_after_self_backend_emission(tmp_path: Path) -> None:
    from pcc.backend.self_backend_dispatch import emit_self_asm

    llvm_ir = _compile_waitset_ir(tmp_path)
    assembly = tmp_path / "freestanding_io_waitset.s"
    assembly.write_text(
        emit_self_asm(llvm_ir.read_text(encoding="utf-8")), encoding="utf-8"
    )
    obj = tmp_path / "freestanding_io_waitset.o"
    assembled = subprocess.run(
        ["clang", "-c", str(assembly), "-o", str(obj)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert assembled.returncode == 0, assembled.stdout + assembled.stderr

    harness = tmp_path / "self_waitset_probe.c"
    executable = tmp_path / "self_waitset_probe"
    harness.write_text(
        r'''
#include "py_io_waitset.h"
#include <stdint.h>
#include <stdio.h>
#include <unistd.h>

/* The thread kernel is not linked: like an unregistered thread in the real
 * kernel, the live wait never enters the exited state here. */
int64_t pcc_thread_blocking_enter(void *frame) {
    (void)frame;
    return 0;
}
void pcc_thread_blocking_leave(int64_t entered) {
    (void)entered;
}

static int live_prepare_failure_preserves_readiness(void) {
    int64_t backend = pcc_io_waitset_default_backend();
#ifdef __APPLE__
    if (backend != PCC_IO_WAITSET_BACKEND_KQUEUE) return 19;
#endif
    if (backend == PCC_IO_WAITSET_BACKEND_POLL) return 0;
    int pipes[2];
    if (pipe(pipes) != 0) return 10;
    PccIoWaitSet ws;
    PccIoWaitBatch batch;
    PccIoWaitResult result;
    if (pcc_io_waitset_init(&ws, (PccIoWaitSetBackend)backend) != 0) return 11;
    if (pcc_io_waitset_add(&ws, pipes[0], PCC_IO_POLLIN, -1, 0) != 0)
        return 12;
    if (write(pipes[1], "x", 1) != 1) return 13;
    /* Force the prepare-time output reservation to fail before the live
     * syscall. Restoring the valid count must expose the same readiness: no
     * kqueue edge / epoll one-shot delivery was consumed by the failed call. */
    ws.live_count = INT64_MAX;
    if (pcc_io_waitset_wait_prepare(&ws, 0, 0, &batch) != -1) {
        pcc_io_waitset_wait_discard(&batch);
        return 14;
    }
    ws.live_count = 1;
    if (pcc_io_waitset_wait(&ws, 0, &result) != 0) return 15;
    if (result.ready_len != 1 || result.ready[0].fd != pipes[0]) return 16;
    if ((result.ready[0].events & PCC_IO_POLLIN) == 0) return 17;
    if (pcc_io_waitset_count(&ws) != 0) return 18;
    pcc_io_waitset_dispose(&ws);
    close(pipes[0]);
    close(pipes[1]);
    return 0;
}

int main(void) {
    PccIoWaitSet ws;
    PccIoWaitResult result;
    if (pcc_io_waitset_init(&ws, PCC_IO_WAITSET_BACKEND_POLL) != 0) return 1;
    if (pcc_io_waitset_add(&ws, 9, PCC_IO_POLLIN, 12, 0) != 0) return 2;
    pcc_io_waitset_set_ready(&ws, 9, PCC_IO_POLLIN);
    if (pcc_io_waitset_wait(&ws, 12, &result) != 0) return 3;
    if (result.ready_len != 1 || result.timeout_len != 0) return 4;
    if (result.ready[0].fd != 9 || result.ready[0].events != PCC_IO_POLLIN) return 5;
    pcc_io_waitset_dispose(&ws);
    int live_rc = live_prepare_failure_preserves_readiness();
    if (live_rc != 0) return live_rc;
    puts("self-waitset-ok");
    return 0;
}
''',
        encoding="utf-8",
    )
    linked = subprocess.run(
        [
            "clang",
            f"-I{RUNTIME / 'src'}",
            str(harness),
            str(obj),
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert linked.returncode == 0, linked.stdout + linked.stderr
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout == "self-waitset-ok\n"


def test_linux_waitset_ir_has_no_kqueue_imports(tmp_path: Path, monkeypatch) -> None:
    from pcc.backend.self_backend_dispatch import emit_self_asm
    from pcc.frontends.python.codegen.unsafe_lowering import UnsafeIntrinsicMixin

    monkeypatch.setattr(
        UnsafeIntrinsicMixin,
        "_target_sys_platform_text",
        lambda self: "linux",
    )
    monkeypatch.setattr(
        UnsafeIntrinsicMixin,
        "_target_machine_text",
        lambda self: "x86_64",
    )
    ir_text = _compile_waitset_ir(tmp_path).read_text(encoding="utf-8")
    declarations = [
        line for line in ir_text.splitlines() if line.startswith("declare ")
    ]
    assert all("@kqueue" not in line for line in declarations)
    assert all("@kevent" not in line for line in declarations)
    assert all("@eventfd" not in line for line in declarations)
    linux_ir = "\n".join(
        'target triple = "x86_64-unknown-linux-gnu"'
        if line.startswith("target triple = ")
        else line
        for line in ir_text.splitlines()
    )
    assembly = emit_self_asm(linux_ir, "x86_64-unknown-linux-gnu")
    assert "pcc_io_waitset_kqueue_available" in assembly
    assert "pcc_io_waitset_epoll_available" in assembly
    assert "pcc_io_waitset_wait_until" in assembly
    assert "pcc_io_waitset_interrupt" in assembly
    assert "pcc_io_waitset_wait_prepare" in assembly
    assert "pcc_io_waitset_wait_block" in assembly
    assert "pcc_io_waitset_wait_finish" in assembly
    assert "syscall" in assembly
