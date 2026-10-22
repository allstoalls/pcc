from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

from tests.runtime_build_cache import cached_pcc_python_runtime, cached_threaded_pcc_python_runtime


REPO_ROOT = Path(__file__).absolute().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"


def _cc() -> str:
    return os.environ.get("CC", "cc")


def _build_runtime(tmp_path: Path, *, pcc_python: bool = False) -> tuple[Path, str, list[str]]:
    if pcc_python:
        return (
            cached_threaded_pcc_python_runtime(),
            "libpy_runtime_pcc_py.a",
            ["-pthread"],
        )
    del tmp_path
    return cached_pcc_python_runtime(), "libpy_runtime_pcc_py.a", []


def _assert_suspended_heap_frame_local_survives_collect_across_backends(
    tmp_path: Path,
    work_runtime: Path,
    archive: str,
    *,
    extra_link_args: list[str],
) -> None:
    src = tmp_path / f"{archive}_suspended_heap_frame_roots_probe.c"
    exe = tmp_path / f"{archive}_suspended_heap_frame_roots_probe.out"
    src.write_text(
        textwrap.dedent(
            """
            #include "py_runtime.h"
            #include "py_internal.h"
            #include <stdint.h>
            #include <stdio.h>

            static PyObject *dummy_resume(PyObject *gen, PyObject *frame) {
                (void)gen;
                (void)frame;
                py_incref(py_None);
                return py_None;
            }

            static int check_backend(int64_t backend) {
                if (pcc_gc_set_backend(backend) != 0) return 0;
                pcc_gc_telemetry_reset();

                PyObject *root = 0;
                pcc_gc_scheduler_root_register(&root);

                PyObject *frame = py_list_new(1);
                if (frame == 0) return 0;
                py_list_append(frame, py_None);

                PyObject *child = py_str_new("suspended", 9);
                if (child == 0) return 0;
                py_list_set(frame, 0, child);

                PyObject *gen = py_gen_new((void *)dummy_resume, frame);
                if (gen == 0) return 0;
                pcc_gc_store_root(&root, gen);
                pcc_gc_release(frame);
                pcc_gc_release(child);
                pcc_gc_release(gen);

                (void)pcc_gc_collect(0);

                PyGenObject *g = (PyGenObject *)root;
                PyObject *live_frame = pcc_gc_load_ptr(root, &g->frame);
                PyObject *got = py_list_get(live_frame, 0);
                PyObject *expected = py_str_new("suspended", 9);
                int ok = got != 0 && expected != 0 && py_obj_eq(got, expected);

                py_decref(got);
                py_decref(expected);
                pcc_gc_scheduler_root_unregister(&root);
                pcc_gc_store_root(&root, 0);
                return ok;
            }

            int main(void) {
                for (int64_t backend = 0; backend <= 4; backend++) {
                    int ok = check_backend(backend);
                    printf("%lld:%d\\n", (long long)backend, ok);
                    if (!ok) return (int)(10 + backend);
                }
                return 0;
            }
            """
        ).lstrip()
    , encoding="utf-8")
    link = [
        _cc(),
        "-std=c11",
        f"-I{work_runtime / 'include'}",
        f"-I{work_runtime / 'src'}",
        str(src),
        str(work_runtime / archive),
    ]
    link.extend(extra_link_args)
    link.extend(["-o", str(exe)])
    build = subprocess.run(link, capture_output=True, text=True, timeout=30)
    assert build.returncode == 0, build.stderr

    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == [
        "0:1",
        "1:1",
        "2:1",
        "3:1",
        "4:1",
    ]


def _assert_task_completion_releases_waiter_cycle_across_backends(
    tmp_path: Path,
    work_runtime: Path,
    archive: str,
    *,
    extra_link_args: list[str],
) -> None:
    src = tmp_path / f"{archive}_task_completion_waiter_probe.c"
    exe = tmp_path / f"{archive}_task_completion_waiter_probe.out"
    src.write_text(
        textwrap.dedent(
            """
            #include "py_runtime.h"
            #include "py_internal.h"
            #include <stdint.h>
            #include <stdio.h>

            static int weakref_live(PyObject *wr) {
                PyObject *target = py_weakref_call(wr);
                int live = target != 0 && target != py_None;
                py_decref(target);
                return live;
            }

            static int check_backend(int64_t backend, int *before_live, int *after_dead) {
                if (pcc_gc_set_backend(backend) != 0) return 0;
                pcc_gc_telemetry_reset();

                PyObject *root = 0;
                PyObject *wr_root = 0;
                pcc_gc_scheduler_root_register(&root);
                pcc_gc_scheduler_root_register(&wr_root);

                PyObject *coro = py_coroutine_new_native("task-coro", 0, 0, 0);
                if (coro == 0) return 0;
                PyObject *task = py_task_new(coro);
                if (task == 0) return 0;
                pcc_gc_release(coro);
                pcc_gc_store_root(&root, task);
                pcc_gc_release(task);

                PyObject *waiter = py_list_new(0);
                if (waiter == 0) return 0;
                py_list_append(waiter, waiter);
                PyObject *wr = py_weakref_new(waiter, 0);
                if (wr == 0) return 0;
                pcc_gc_store_root(&wr_root, wr);
                pcc_gc_release(wr);
                py_task_set_waiter(root, waiter);
                pcc_gc_release(waiter);

                (void)pcc_gc_collect(0);
                *before_live = weakref_live(wr_root);

                py_task_set_result(root, py_None);
                (void)pcc_gc_collect(0);
                *after_dead = !weakref_live(wr_root);

                pcc_gc_store_root(&root, 0);
                pcc_gc_store_root(&wr_root, 0);
                pcc_gc_scheduler_root_unregister(&root);
                pcc_gc_scheduler_root_unregister(&wr_root);
                return *before_live && *after_dead;
            }

            int main(void) {
                for (int64_t backend = 0; backend <= 4; backend++) {
                    int before_live = 0;
                    int after_dead = 0;
                    int ok = check_backend(backend, &before_live, &after_dead);
                    printf(
                        "%lld:%d:%d\\n",
                        (long long)backend,
                        before_live,
                        after_dead
                    );
                    if (!ok) return (int)(20 + backend);
                }
                return 0;
            }
            """
        ).lstrip()
    , encoding="utf-8")
    link = [
        _cc(),
        "-std=c11",
        f"-I{work_runtime / 'include'}",
        f"-I{work_runtime / 'src'}",
        str(src),
        str(work_runtime / archive),
    ]
    link.extend(extra_link_args)
    link.extend(["-o", str(exe)])
    build = subprocess.run(link, capture_output=True, text=True, timeout=30)
    assert build.returncode == 0, build.stderr

    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == [
        "0:1:1",
        "1:1:1",
        "2:1:1",
        "3:1:1",
        "4:1:1",
    ]


def _assert_virtual_thread_scheduler_queues_keep_continuation_roots_across_backends(
    tmp_path: Path,
    work_runtime: Path,
    archive: str,
    *,
    extra_link_args: list[str],
) -> None:
    src = tmp_path / f"{archive}_vthread_queue_roots_probe.c"
    exe = tmp_path / f"{archive}_vthread_queue_roots_probe.out"
    src.write_text(
        textwrap.dedent(
            """
            #include "py_runtime.h"
            #include "py_internal.h"
            #include <poll.h>
            #include <stdint.h>
            #include <stdio.h>
            #include <string.h>
            #include <unistd.h>

            static void resume_a(void) {}

            static PyObject *make_vthread(const char *label) {
                int32_t frame_map[1] = {1};
                PyObject *slots[1] = {0};
                PyObject *local = py_str_new(label, (int64_t)strlen(label));
                if (local == 0) return 0;
                pcc_gc_store_root(&slots[0], local);
                PyObject *cont = py_continuation_new(
                    frame_map,
                    slots,
                    (void *)&resume_a
                );
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
                    return 0;
                }
                pcc_gc_release(vt);
                (void)pcc_gc_collect(0);
                if (write(fds[1], "x", 1) != 1) return 0;
                if (py_virtual_thread_poll_io(0) != 1) return 0;
                PyObject *ready = py_virtual_thread_poll_ready();
                if (ready == 0) return 0;
                int ok = continuation_slot_matches(ready, "io-root");
                py_virtual_thread_complete(ready, py_None);
                py_decref(ready);
                close(fds[0]);
                close(fds[1]);
                return ok;
            }

            static int check_backend(int64_t backend) {
                if (pcc_gc_set_backend(backend) != 0) return 0;
                pcc_gc_telemetry_reset();
                if (!check_ready_queue()) return 0;
                if (!check_timer_queue()) return 0;
                if (!check_io_queue()) return 0;
                return py_virtual_thread_ready_count() == 0
                    && py_virtual_thread_timer_count() == 0
                    && py_virtual_thread_io_wait_count() == 0;
            }

            int main(void) {
                for (int64_t backend = 0; backend <= 4; backend++) {
                    int ok = check_backend(backend);
                    printf("%lld:%d\\n", (long long)backend, ok);
                    if (!ok) return (int)(30 + backend);
                }
                return 0;
            }
            """
        ).lstrip()
    , encoding="utf-8")
    link = [
        _cc(),
        "-std=c11",
        f"-I{work_runtime / 'include'}",
        f"-I{work_runtime / 'src'}",
        str(src),
        str(work_runtime / archive),
    ]
    link.extend(extra_link_args)
    link.extend(["-o", str(exe)])
    build = subprocess.run(link, capture_output=True, text=True, timeout=30)
    assert build.returncode == 0, build.stderr

    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == [
        "0:1",
        "1:1",
        "2:1",
        "3:1",
        "4:1",
    ]


def test_suspended_heap_frame_local_survives_collect_across_backends(tmp_path):
    work_runtime, archive, extra_link_args = _build_runtime(tmp_path)
    _assert_suspended_heap_frame_local_survives_collect_across_backends(
        tmp_path,
        work_runtime,
        archive,
        extra_link_args=extra_link_args,
    )


def test_task_completion_releases_waiter_cycle_across_backends(tmp_path):
    work_runtime, archive, extra_link_args = _build_runtime(tmp_path)
    _assert_task_completion_releases_waiter_cycle_across_backends(
        tmp_path,
        work_runtime,
        archive,
        extra_link_args=extra_link_args,
    )


def test_virtual_thread_scheduler_queues_keep_continuation_roots_across_backends(
    tmp_path,
):
    work_runtime, archive, extra_link_args = _build_runtime(tmp_path)
    _assert_virtual_thread_scheduler_queues_keep_continuation_roots_across_backends(
        tmp_path,
        work_runtime,
        archive,
        extra_link_args=extra_link_args,
    )


def test_pcc_python_runtime_suspended_heap_frame_local_survives_collect_across_backends(
    tmp_path,
):
    work_runtime, archive, extra_link_args = _build_runtime(
        tmp_path,
        pcc_python=True,
    )
    _assert_suspended_heap_frame_local_survives_collect_across_backends(
        tmp_path,
        work_runtime,
        archive,
        extra_link_args=extra_link_args,
    )


def test_pcc_python_runtime_task_completion_releases_waiter_cycle_across_backends(
    tmp_path,
):
    work_runtime, archive, extra_link_args = _build_runtime(
        tmp_path,
        pcc_python=True,
    )
    _assert_task_completion_releases_waiter_cycle_across_backends(
        tmp_path,
        work_runtime,
        archive,
        extra_link_args=extra_link_args,
    )


def test_pcc_python_runtime_virtual_thread_scheduler_queues_keep_continuation_roots_across_backends(
    tmp_path,
):
    work_runtime, archive, extra_link_args = _build_runtime(
        tmp_path,
        pcc_python=True,
    )
    _assert_virtual_thread_scheduler_queues_keep_continuation_roots_across_backends(
        tmp_path,
        work_runtime,
        archive,
        extra_link_args=extra_link_args,
    )
