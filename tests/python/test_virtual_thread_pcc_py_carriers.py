"""pcc-Python ownership gates for the bounded virtual-thread carrier pool.

The C runtime is an oracle only in this file.  The focused native probe links
the threaded ``libpy_runtime_pcc_py.a`` archive, and the product-level gate
uses a fresh current-pcc1 binary to compile the application with
``backend=self`` and ``python-libpython=off``.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from pcc1_gate import find_current_pcc1
from tests.runtime_build_cache import cached_threaded_pcc_python_runtime


REPO = Path(__file__).resolve().parents[2]
RUNTIME = REPO / "pcc" / "runtime"
RUNTIME_SOURCE = RUNTIME / "py" / "py_virtual_thread_runtime.py"


@pytest.fixture(scope="session")
def threaded_pcc_runtime_archive() -> Path:
    archive = (
        cached_threaded_pcc_python_runtime() / "libpy_runtime_pcc_py.a"
    )
    assert archive.is_file()
    assert RUNTIME not in archive.parents
    return archive


def test_pcc_python_runtime_owns_bounded_carrier_policy() -> None:
    source = RUNTIME_SOURCE.read_text(encoding="utf-8")
    assert 'define_global_null_ptr_array("pcc_vthread_carrier_heads_py", 64)' in source
    assert 'define_thread_local_i32("pcc_current_virtual_thread_carrier_py", -1)' in source
    assert 'function_addr("pcc_vthread_carrier_pool_worker_py")' in source
    assert 'function_addr("pcc_vthread_persistent_carrier_worker_py")' in source
    assert "atomic_cas_i64(" in source
    assert "pcc_cond_timedwait_ms(" in source
    assert "pcc_io_waitset_wait_prepare(" in source
    assert "pcc_io_waitset_wait_block(" in source
    assert "pcc_io_waitset_wait_finish(" in source
    assert "pcc_io_waitset_dispose(" in source
    assert "def _waitset_dispose_locked() -> int:" in source
    assert "_io_refresh_registered(load_i64(node, 8))" in source
    assert 'pcc_platform_getenv(cstr("PCC_VTHREAD_IO_BACKEND"))' in source
    assert '_cstr_equals(requested, cstr("poll"))' in source
    assert '_cstr_equals(requested, cstr("kqueue"))' in source
    assert '_cstr_equals(requested, cstr("epoll"))' in source
    assert "_effect(6, 1, 0, load_i64(vthread, 32))" in source
    assert "_effect(6, 0, 0, load_i64(vthread, 32))" in source
    block_at = source.index("pcc_io_waitset_wait_block(")
    assert source.rfind("_scheduler_unlock()", 0, block_at) >= 0
    assert source.find("_scheduler_lock()", block_at) > block_at
    assert "_io_interrupt_locked()" in source
    assert "while offset < carrier_count:" in source
    assert 'global_addr("pcc_vthread_carrier_steal_count_py")' in source
    assert '@c_abi_export("py_virtual_thread_pin_reason_event_count")' in source
    assert "return py_virtual_thread_run_until_idle(max_steps)" in source
    assert "def py_virtual_thread_carrier_count() -> int:\n    return 1" not in source
    assert "def py_virtual_thread_carrier_steal_count() -> int:\n    return 0" not in source


def test_scheduler_global_intrinsics_stay_literal_and_runtime_compiles(
    tmp_path: Path,
) -> None:
    """Unsafe global-address intrinsics must see literals at their call sites."""
    source = RUNTIME_SOURCE.read_text(encoding="utf-8")
    assert "global_addr(slot" not in source
    assert 'global_addr("pcc_vthread_scheduler_mutex_bits_py")' in source
    assert 'global_addr("pcc_vthread_scheduler_cond_bits_py")' in source

    emitted = tmp_path / "py_virtual_thread_runtime.ll"
    compiled = subprocess.run(
        [
            str(REPO / ".venv" / "bin" / "pcc"),
            "--python-library",
            f"--emit-llvm={emitted}",
            str(RUNTIME_SOURCE),
        ],
        cwd=REPO,
        text=True,
        capture_output=True,
        timeout=60,
    )
    assert compiled.returncode == 0, compiled.stdout + compiled.stderr
    assert emitted.is_file()


@pytest.mark.integration
@pytest.mark.xdist_group(name="pcc_py_vthread_carriers")
def test_threaded_pcc_python_archive_runs_persistent_carriers_and_pin_metrics(
    tmp_path: Path,
    threaded_pcc_runtime_archive: Path,
) -> None:
    probe = tmp_path / "pcc_py_carriers_probe.c"
    exe = tmp_path / "pcc_py_carriers_probe"
    probe.write_text(
        textwrap.dedent(
            r"""
            #include "py_runtime.h"
            #include <poll.h>
            #include <stdint.h>
            #include <stdio.h>
            #include <unistd.h>

            extern int64_t py_virtual_thread_carrier_queue_count(void);
            extern int64_t py_virtual_thread_carrier_queue_depth(int64_t index);
            extern int64_t py_virtual_thread_carrier_failure_count(void);
            extern int64_t py_virtual_thread_pin_reason_event_count(
                const char *reason
            );
            extern int64_t py_virtual_thread_pin_reason_dropped_count(void);

            static int64_t resume_calls = 0;
            static int64_t wrong_current = 0;

            static int64_t resume_typed(PyObject *vthread, PyObject *continuation) {
                (void)continuation;
                PyObject *current = py_virtual_thread_current();
                if (current != vthread) {
                    __atomic_add_fetch(&wrong_current, 1, __ATOMIC_ACQ_REL);
                }
                py_decref(current);
                if (py_virtual_thread_pin_enter(vthread, "gateway.native") != 1) {
                    return -2;
                }
                if (py_virtual_thread_pin_leave(vthread) != 0) return -3;
                __atomic_add_fetch(&resume_calls, 1, __ATOMIC_ACQ_REL);
                return py_virtual_thread_complete(vthread, py_None);
            }

            static PyObject *make_vthread(void) {
                int32_t frame_map[1] = {0};
                PyObject *continuation = py_continuation_new_typed(
                    frame_map, NULL, (void *)&resume_typed
                );
                if (continuation == NULL) return NULL;
                PyObject *vthread = py_virtual_thread_new(continuation);
                py_decref(continuation);
                return vthread;
            }

            int main(void) {
                enum { N = 96 };
                if (!pcc_threads_enabled()) return 2;
                if (pcc_gc_set_backend(PCC_GC_KIND_REFCOUNT_CYCLE) != 0) return 3;
                if (py_virtual_thread_carrier_count() != 1) return 4;
                if (py_virtual_thread_carrier_pool_start(3) != 3) return 5;
                for (
                    int i = 0;
                    i < 1000 && py_virtual_thread_carrier_count() < 4;
                    i++
                ) {
                    (void)poll(NULL, 0, 1);
                }
                if (py_virtual_thread_carrier_count() < 4) return 6;
                if (py_virtual_thread_carrier_queue_count() != 3) return 7;
                for (int i = 0; i < 3; i++) {
                    if (py_virtual_thread_carrier_queue_depth(i) != 0) return 20;
                }

                PyObject *threads[N];
                for (int i = 0; i < N; i++) {
                    threads[i] = make_vthread();
                    if (threads[i] == NULL) return 8;
                    if (py_virtual_thread_start(threads[i]) != 0) return 9;
                }
                for (int spin = 0; spin < 5000; spin++) {
                    int done = 0;
                    for (int i = 0; i < N; i++) {
                        if (py_virtual_thread_state(threads[i]) == 4) done++;
                    }
                    if (done == N) break;
                    (void)poll(NULL, 0, 1);
                }
                for (int i = 0; i < N; i++) {
                    if (py_virtual_thread_state(threads[i]) != 4) return 10;
                }
                if (__atomic_load_n(&resume_calls, __ATOMIC_ACQUIRE) != N) return 11;
                if (__atomic_load_n(&wrong_current, __ATOMIC_ACQUIRE) != 0) return 12;
                if (
                    py_virtual_thread_pin_reason_event_count("gateway.native")
                    != N
                ) return 13;
                if (py_virtual_thread_pin_reason_dropped_count() != 0) return 14;
                if (py_virtual_thread_carrier_failure_count() != 0) return 15;
                if (py_virtual_thread_carrier_pool_stop() != 3) return 16;
                if (py_virtual_thread_carrier_count() != 1) return 17;
                if (py_virtual_thread_carrier_queue_count() != 0) return 18;
                if (py_virtual_thread_ready_count() != 0) return 19;

                int fds[2];
                if (pipe(fds) != 0) return 21;
                PyObject *parked = make_vthread();
                if (parked == NULL) return 22;
                if (py_virtual_thread_carrier_pool_start(2) != 2) return 23;
                if (
                    py_virtual_thread_block_on_fd(parked, fds[0], POLLIN, -1)
                    != 0
                ) return 24;
                if (pcc_gc_scheduler_root_count() != 1) return 25;
                if (py_virtual_thread_carrier_pool_stop() != 2) return 26;
                if (py_virtual_thread_io_wait_count() != 1) return 27;
                if (pcc_gc_scheduler_root_count() != 1) return 28;
                if (py_virtual_thread_carrier_pool_start(2) != 2) return 29;
                if (py_virtual_thread_carrier_pool_stop() != 2) return 30;
                if (py_virtual_thread_io_wait_count() != 1) return 31;
                if (pcc_gc_scheduler_root_count() != 1) return 32;
                if (write(fds[1], "r", 1) != 1) return 33;
                if (py_virtual_thread_poll_io(0) != 1) return 34;
                if (py_virtual_thread_run_once() != 1) return 35;
                if (py_virtual_thread_state(parked) != 4) return 36;
                if (pcc_gc_scheduler_root_count() != 0) return 37;
                close(fds[0]);
                close(fds[1]);
                py_decref(parked);

                for (int i = 0; i < N; i++) py_decref(threads[i]);
                printf(
                    "pcc-py-carriers-ok steals=%lld pins=%lld\n",
                    (long long)py_virtual_thread_carrier_steal_count(),
                    (long long)py_virtual_thread_pin_event_count()
                );
                return 0;
            }
            """
        ).lstrip(),
        encoding="utf-8",
    )
    built = subprocess.run(
        [
            "clang",
            "-std=c11",
            f"-I{RUNTIME / 'include'}",
            str(probe),
            str(threaded_pcc_runtime_archive),
            "-pthread",
            "-o",
            str(exe),
        ],
        cwd=REPO,
        text=True,
        capture_output=True,
        timeout=120,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    ran = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=60
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert ran.stdout.startswith("pcc-py-carriers-ok steals=")


PCC1_APP = textwrap.dedent(
    """
    import gc

    from pcc.virtual_thread import (
        carrier_pool_start,
        carrier_pool_stop,
        result,
        spawn,
        state,
    )

    def gateway_work(value: int) -> int:
        return value * 3 + 1

    def main() -> None:
        started = carrier_pool_start(3)
        jobs = []
        i = 0
        while i < 40:
            jobs.append(spawn(gateway_work, i))
            i = i + 1

        remaining = 40
        spins = 0
        collections = 0
        while remaining > 0 and spins < 2000000:
            remaining = 0
            j = 0
            while j < len(jobs):
                if state(jobs[j]) != 4:
                    remaining = remaining + 1
                j = j + 1
            if spins % 256 == 0 and collections < 16:
                gc.collect()
                collections = collections + 1
            spins = spins + 1

        total = 0
        j = 0
        while j < len(jobs):
            if state(jobs[j]) == 4:
                total = total + result(jobs[j])
            j = j + 1
        stopped = carrier_pool_stop()
        restarted = carrier_pool_start(2)
        restopped = carrier_pool_stop()
        print(started, stopped, restarted, restopped, total, remaining)

    if __name__ == "__main__":
        main()
    """
).lstrip()


@pytest.mark.integration
@pytest.mark.pcc_gate(probe="pcc1")
@pytest.mark.xdist_group(name="pcc1_pcc_py_vthread_carriers")
@pytest.mark.parametrize("gc_backend", ("0", "1", "2", "3", "4"))
def test_current_pcc1_self_no_libpython_multicarrier_gc_matrix(
    tmp_path: Path,
    threaded_pcc_runtime_archive: Path,
    gc_backend: str,
) -> None:
    pcc1 = find_current_pcc1(REPO)
    if pcc1 is None:
        pytest.fail("a source-current pcc1 is required for carrier parity")

    source = tmp_path / f"pcc1_vthread_carriers_gc{gc_backend}.py"
    exe = tmp_path / f"pcc1_vthread_carriers_gc{gc_backend}"
    source.write_text(PCC1_APP, encoding="utf-8")
    env = dict(os.environ)
    env.pop("LC_ALL", None)
    env.update(
        {
            "PCC_GC_BACKEND": gc_backend,
            "PCC_RUNTIME_ARCHIVE": str(threaded_pcc_runtime_archive),
            "PCC_WITH_THREADS": "1",
        }
    )
    command = [
        str(pcc1),
        "--backend",
        "self",
        "--python-libpython=off",
        "--ir-scaffold=on",
        str(source),
        "-o",
        str(exe),
    ]
    built = subprocess.run(
        command,
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        timeout=300,
    )
    assert built.returncode == 0, (
        f"current-pcc1 carrier compile failed for GC{gc_backend}:\n"
        f"command: {' '.join(command)}\n"
        f"stdout:\n{built.stdout}\n"
        f"stderr:\n{built.stderr}"
    )
    assert exe.is_file()
    ran = subprocess.run(
        [str(exe)], env=env, text=True, capture_output=True, timeout=60
    )
    assert ran.returncode == 0, ran.stdout + ran.stderr
    assert ran.stdout.strip() == "3 3 2 2 2380 0"
