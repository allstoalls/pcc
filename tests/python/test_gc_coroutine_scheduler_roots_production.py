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


def _build_runtime(tmp_path: Path, *, with_threads: bool = False) -> Path:
    del tmp_path
    if with_threads:
        return cached_threaded_pcc_python_runtime()
    return cached_pcc_python_runtime()


def _compile_and_run(tmp_path: Path, source: str, *, with_threads: bool = False):
    work_runtime = _build_runtime(tmp_path, with_threads=with_threads)
    src = tmp_path / "coroutine_scheduler_roots_probe.c"
    exe = tmp_path / "coroutine_scheduler_roots_probe.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    thread_flags = ["-DPCC_WITH_THREADS=1", "-pthread"] if with_threads else []
    build = subprocess.run(
        [
            _cc(),
            *thread_flags,
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


def test_scheduler_and_frame_root_observability_across_backends(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include "py_internal.h"
        #include <stdio.h>

        static int check_backend(int64_t backend) {
            if (pcc_gc_set_backend(backend) != 0) return 0;
            pcc_gc_telemetry_reset();

            PyObject *root = 0;
            pcc_gc_scheduler_root_register(&root);

            int32_t frame_map[1] = {1};
            PyObject *slots[1] = {0};
            PyObject *local = py_str_new("frame-local", 11);
            pcc_gc_store_root(&slots[0], local);
            pcc_gc_frame_enter(frame_map, slots);

            PccGcSchedulerQueue *queue = pcc_gc_scheduler_queue_new();
            if (queue == 0) return 0;
            PyObject *queued = py_str_new("queued", 6);
            if (queued == 0) return 0;
            if (pcc_gc_scheduler_queue_push(queue, queued) != 0) return 0;
            pcc_gc_release(queued);

            if (pcc_gc_scheduler_root_count() < 1) return 0;
            if (pcc_gc_frame_root_slot_count() < 1) return 0;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_SCHEDULER_ROOTS) < 1) return 0;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_FRAME_ROOT_SLOTS) < 1) return 0;
            if (pcc_gc_scheduler_queue_len(queue) != 1) return 0;

            (void)pcc_gc_collect(0);

            PyObject *out = 0;
            pcc_gc_scheduler_root_register(&out);
            if (pcc_gc_scheduler_queue_pop_into(queue, &out) != 1) return 0;
            PyObject *expected = py_str_new("queued", 6);
            int ok = out != 0 && expected != 0 && py_obj_eq(out, expected);
            pcc_gc_release(expected);

            pcc_gc_store_root(&out, 0);
            pcc_gc_scheduler_root_unregister(&out);
            pcc_gc_scheduler_queue_free(queue);
            pcc_gc_frame_leave(slots);
            pcc_gc_store_root(&slots[0], 0);
            pcc_gc_release(local);
            pcc_gc_scheduler_root_unregister(&root);
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


def test_scheduler_root_handle_unregister_keeps_legacy_api_observable(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include "py_internal.h"
        #include <stdio.h>

        static int check_backend(int64_t backend) {
            if (pcc_gc_set_backend(backend) != 0) return 0;

            PyObject *handle_root = 0;
            void *handle = pcc_gc_scheduler_root_register_handle(&handle_root);
            if (handle == 0) return 0;
            if (pcc_gc_scheduler_root_count() != 1) return 0;
            if (pcc_gc_slot_is_runtime_root(&handle_root) != 1) return 0;

            PyObject *legacy_root = 0;
            pcc_gc_scheduler_root_register(&legacy_root);
            if (pcc_gc_scheduler_root_count() != 2) return 0;

            pcc_gc_scheduler_root_unregister_handle(handle);
            if (pcc_gc_scheduler_root_count() != 1) return 0;
            if (pcc_gc_slot_is_runtime_root(&handle_root) != 0) return 0;
            if (pcc_gc_slot_is_runtime_root(&legacy_root) != 1) return 0;

            pcc_gc_scheduler_root_unregister(&legacy_root);
            return pcc_gc_scheduler_root_count() == 0;
        }

        int main(void) {
            for (int64_t backend = 0; backend <= 4; backend++) {
                int ok = check_backend(backend);
                printf("%lld:%d\\n", (long long)backend, ok);
                if (!ok) return (int)(20 + backend);
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


def test_scheduler_queue_entry_freelist_preserves_roots_across_backends(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include "py_internal.h"
        #include <stdio.h>

        static int check_backend(int64_t backend) {
            if (pcc_gc_set_backend(backend) != 0) return 0;
            PccGcSchedulerQueue *queue = pcc_gc_scheduler_queue_new();
            if (queue == 0) return 0;

            PyObject *out = 0;
            void *out_handle = pcc_gc_scheduler_root_register_handle(&out);
            if (out_handle == 0) return 0;

            for (int64_t i = 0; i < 128; i++) {
                PyObject *queued = py_int_from_i64(i);
                if (queued == 0) return 0;
                if (pcc_gc_scheduler_queue_push(queue, queued) != 0) return 0;
                pcc_gc_release(queued);
                if (pcc_gc_scheduler_queue_len(queue) != 1) return 0;
                if (pcc_gc_scheduler_queue_pop_into(queue, &out) != 1) return 0;
                int overflow = 0;
                int64_t raw = py_int_to_i64(out, &overflow);
                if (overflow || raw != i) return 0;
                pcc_gc_store_root(&out, 0);
                if (pcc_gc_scheduler_queue_len(queue) != 0) return 0;
            }

            pcc_gc_scheduler_root_unregister_handle(out_handle);
            pcc_gc_scheduler_queue_free(queue);
            return pcc_gc_scheduler_root_count() == 0;
        }

        int main(void) {
            for (int64_t backend = 0; backend <= 4; backend++) {
                int ok = check_backend(backend);
                printf("%lld:%d\\n", (long long)backend, ok);
                if (!ok) return (int)(30 + backend);
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


def test_continuation_root_map_rewrites_backend4_forwarded_slot(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include "py_internal.h"
        #include <stdio.h>

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        static PyObject *new_reloc_payload(void) {
            ProbeListObject *obj = (
                ProbeListObject *
            )pcc_gc_alloc(64, PY_TYPE_LIST, 0);
            if (obj == 0) return 0;
            obj->length = 0;
            obj->capacity = 0;
            obj->items = 0;
            return (PyObject *)obj;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            int32_t frame_map[1] = {1};
            PyObject *slots[1] = {0};
            PyObject *obj = new_reloc_payload();
            if (obj == 0) return 3;
            int64_t stable = pcc_gc_object_id(obj);
            if (stable <= 0) return 4;

            pcc_gc_store_root(&slots[0], obj);
            pcc_gc_register_continuation_root(frame_map, slots);
            if (pcc_gc_continuation_root_slot_count() != 1) return 5;
            if (pcc_gc_coroutine_root_score() < 1) return 6;
            if (pcc_gc_trace_continuation_roots() != 1) return 7;

            pcc_gc_telemetry_reset();
            if (pcc_gc_select_relocation_set(1) != 1) return 8;
            PyObject *moved = pcc_gc_relocate_copy(obj, 64);
            if (moved == 0) return 9;
            if (slots[0] != obj) return 10;
            if (pcc_gc_rewrite_continuation_roots() != 1) return 11;
            if (slots[0] == obj) return 12;
            if (pcc_gc_object_id(slots[0]) != stable) return 13;

            pcc_gc_release(obj);
            pcc_gc_release(moved);
            pcc_gc_store_root(&slots[0], 0);
            pcc_gc_unregister_continuation_root(slots);
            if (pcc_gc_continuation_root_slot_count() != 0) return 14;

            printf("continuation-root-rewrite-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "continuation-root-rewrite-ok"


def test_continuation_object_mount_unmount_scans_and_rewrites_slots(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include "py_internal.h"
        #include <stdio.h>

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        static PyObject *new_reloc_payload(void) {
            ProbeListObject *obj = (
                ProbeListObject *
            )pcc_gc_alloc(64, PY_TYPE_LIST, 0);
            if (obj == 0) return 0;
            obj->length = 0;
            obj->capacity = 0;
            obj->items = 0;
            return (PyObject *)obj;
        }

        static void resume_a(void) {}
        static void resume_b(void) {}

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            PyObject *cont_root = 0;
            pcc_gc_scheduler_root_register(&cont_root);

            int32_t frame_map[1] = {1};
            PyObject *slots[1] = {0};
            PyObject *first = new_reloc_payload();
            if (first == 0) return 3;
            int64_t first_id = pcc_gc_object_id(first);
            slots[0] = first;

            PyObject *cont = py_continuation_new(frame_map, slots, (void *)&resume_a);
            if (cont == 0) return 4;
            pcc_gc_store_root(&cont_root, cont);
            pcc_gc_release(cont);
            pcc_gc_release(first);

            if (py_continuation_is_mounted(cont_root) != 0) return 5;
            if (py_continuation_slot_count(cont_root) != 1) return 6;
            if (pcc_gc_continuation_root_slot_count() != 1) return 7;
            if (py_continuation_resume_pc(cont_root) != (void *)&resume_a) return 8;

            PyObject *before = py_continuation_get_slot(cont_root, 0);
            if (before == 0) return 9;
            if (pcc_gc_object_id(before) != first_id) return 10;
            pcc_gc_release(before);

            pcc_gc_telemetry_reset();
            if (pcc_gc_select_relocation_set(64) <= 0) return 11;
            if (pcc_gc_relocation_set_contains(first) != 1) return 12;
            PyObject *moved = pcc_gc_relocate_copy(first, 64);
            if (moved == 0) return 13;
            if (pcc_gc_rewrite_continuation_roots() != 1) return 14;
            PyObject *after = py_continuation_get_slot(cont_root, 0);
            if (after == 0) return 15;
            if (after == first) return 16;
            if (pcc_gc_object_id(after) != first_id) return 17;
            pcc_gc_release(after);
            pcc_gc_release(moved);

            PyObject *mounted_slots[1] = {0};
            if (py_continuation_mount(cont_root, mounted_slots) != 0) return 18;
            if (py_continuation_is_mounted(cont_root) != 1) return 19;
            if (pcc_gc_continuation_root_slot_count() != 0) return 20;
            if (pcc_gc_object_id(mounted_slots[0]) != first_id) return 21;

            PyObject *replacement = new_reloc_payload();
            if (replacement == 0) return 22;
            int64_t replacement_id = pcc_gc_object_id(replacement);
            pcc_gc_store_root(&mounted_slots[0], replacement);
            pcc_gc_release(replacement);

            if (py_continuation_unmount(cont_root, mounted_slots, (void *)&resume_b) != 0) return 23;
            if (py_continuation_is_mounted(cont_root) != 0) return 24;
            if (pcc_gc_continuation_root_slot_count() != 1) return 25;
            if (py_continuation_resume_pc(cont_root) != (void *)&resume_b) return 26;

            PyObject *saved = py_continuation_get_slot(cont_root, 0);
            if (saved == 0) return 27;
            if (pcc_gc_object_id(saved) != replacement_id) return 28;
            pcc_gc_release(saved);

            pcc_gc_store_root(&mounted_slots[0], 0);
            pcc_gc_store_root(&cont_root, 0);
            pcc_gc_scheduler_root_unregister(&cont_root);
            if (pcc_gc_continuation_root_slot_count() != 0) return 29;

            printf("continuation-object-mount-unmount-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "continuation-object-mount-unmount-ok"


def test_virtual_thread_scheduler_ready_park_unpark_is_cooperative(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        static void resume_a(void) {}

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            int32_t frame_map[1] = {1};
            PyObject *slots[1] = {0};
            PyObject *local = py_str_new("fiber-local", 11);
            if (local == 0) return 3;
            slots[0] = local;

            PyObject *cont = py_continuation_new(frame_map, slots, (void *)&resume_a);
            if (cont == 0) return 4;
            pcc_gc_release(local);

            PyObject *vt = py_virtual_thread_new(cont);
            if (vt == 0) return 5;
            pcc_gc_release(cont);

            if (py_virtual_thread_state(vt) != 0) return 6;
            if (py_virtual_thread_ready_count() != 0) return 7;
            if (py_virtual_thread_start(vt) != 0) return 8;
            if (py_virtual_thread_ready_count() != 1) return 9;

            PyObject *ready = py_virtual_thread_poll_ready();
            if (ready == 0) return 10;
            if (ready != vt) return 11;
            if (py_virtual_thread_state(ready) != 2) return 12;
            if (py_virtual_thread_ready_count() != 0) return 13;

            if (py_virtual_thread_park(ready) != 0) return 14;
            if (py_virtual_thread_state(ready) != 3) return 15;
            if (py_virtual_thread_unpark(ready) != 0) return 16;
            if (py_virtual_thread_state(ready) != 1) return 17;
            if (py_virtual_thread_ready_count() != 1) return 18;

            PyObject *ready2 = py_virtual_thread_poll_ready();
            if (ready2 == 0) return 19;
            if (ready2 != vt) return 20;
            if (py_virtual_thread_complete(ready2, py_None) != 0) return 21;
            if (py_virtual_thread_state(ready2) != 4) return 22;

            pcc_gc_release(ready2);
            pcc_gc_release(ready);
            pcc_gc_release(vt);
            if (py_virtual_thread_ready_count() != 0) return 23;

            printf("virtual-thread-scheduler-cooperative-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "virtual-thread-scheduler-cooperative-ok"


def test_virtual_thread_timer_poller_and_pinning_are_cooperative(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <poll.h>
        #include <stdio.h>
        #include <unistd.h>

        static void resume_a(void) {}

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            int32_t frame_map[1] = {1};
            PyObject *slots[1] = {0};
            PyObject *local = py_str_new("fiber-blocking-local", 20);
            if (local == 0) return 3;
            slots[0] = local;

            PyObject *cont = py_continuation_new(frame_map, slots, (void *)&resume_a);
            if (cont == 0) return 4;
            pcc_gc_release(local);

            PyObject *vt = py_virtual_thread_new(cont);
            if (vt == 0) return 5;
            pcc_gc_release(cont);

            if (py_virtual_thread_sleep(vt, 20) != 0) return 6;
            if (py_virtual_thread_state(vt) != 3) return 7;
            if (py_virtual_thread_timer_count() != 1) return 8;
            usleep(30000);
            if (py_virtual_thread_poll_timers() != 1) return 9;
            if (py_virtual_thread_timer_count() != 0) return 10;
            if (py_virtual_thread_ready_count() != 1) return 11;

            PyObject *ready = py_virtual_thread_poll_ready();
            if (ready == 0) return 12;
            if (ready != vt) return 13;
            if (py_virtual_thread_state(ready) != 2) return 14;

            pcc_gc_telemetry_reset();
            int64_t pin_events_before = py_virtual_thread_pin_event_count();
            if (py_virtual_thread_pin_enter(ready, "poller-test") != 1) return 15;
            if (py_virtual_thread_pin_count(ready) != 1) return 16;
            if (py_virtual_thread_pinned_count() < 1) return 17;
            if (py_virtual_thread_pin_event_count() != pin_events_before + 1) return 18;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_PIN_BALANCE) != 1) return 19;
            if (py_virtual_thread_pin_leave(ready) != 0) return 20;
            if (py_virtual_thread_pin_count(ready) != 0) return 21;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_PIN_BALANCE) != 0) return 22;

            int fds[2];
            if (pipe(fds) != 0) return 23;
            if (py_virtual_thread_block_on_fd(ready, fds[0], POLLIN, 1) != 0) return 24;
            if (py_virtual_thread_state(ready) != 3) return 25;
            if (py_virtual_thread_io_wait_count() != 1) return 26;
            usleep(2000);
            if (py_virtual_thread_poll_io(0) != 1) return 27;
            if (py_virtual_thread_io_wait_count() != 0) return 28;

            PyObject *ready2 = py_virtual_thread_poll_ready();
            if (ready2 == 0) return 29;
            if (ready2 != vt) return 30;
            if (py_virtual_thread_complete(ready2, py_None) != 0) return 31;

            pcc_gc_release(ready2);
            pcc_gc_release(ready);
            pcc_gc_release(vt);
            close(fds[0]);
            close(fds[1]);

            printf("virtual-thread-blocking-poller-pinning-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "virtual-thread-blocking-poller-pinning-ok"


def test_virtual_thread_carrier_run_loop_invokes_resume_pc(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>
        #include <unistd.h>

        static int resume_calls = 0;

        static void resume_step(void) {
            resume_calls++;
        }

        static PyObject *make_vthread(void) {
            int32_t frame_map[1] = {0};
            PyObject *cont = py_continuation_new(frame_map, 0, (void *)&resume_step);
            if (cont == 0) return 0;
            PyObject *vt = py_virtual_thread_new(cont);
            pcc_gc_release(cont);
            return vt;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            PyObject *vt = make_vthread();
            if (vt == 0) return 3;
            if (py_virtual_thread_start(vt) != 0) return 4;
            if (py_virtual_thread_run_once() != 1) return 5;
            if (resume_calls != 1) return 6;
            if (py_virtual_thread_state(vt) != 4) return 7;
            if (py_virtual_thread_run_until_idle(4) != 0) return 8;
            pcc_gc_release(vt);

            PyObject *sleeping = make_vthread();
            if (sleeping == 0) return 9;
            if (py_virtual_thread_sleep(sleeping, 20) != 0) return 10;
            if (py_virtual_thread_run_until_idle(4) != 0) return 11;
            usleep(30000);
            if (py_virtual_thread_run_until_idle(4) != 1) return 12;
            if (resume_calls != 2) return 13;
            if (py_virtual_thread_state(sleeping) != 4) return 14;
            pcc_gc_release(sleeping);

            printf("virtual-thread-carrier-run-loop-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "virtual-thread-carrier-run-loop-ok"


def test_virtual_thread_bounded_carrier_pool_drains_ready_queue(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        static int resume_calls = 0;
        static int max_seen_carriers = 0;

        static void resume_step(void) {
            int64_t carriers = py_virtual_thread_carrier_count();
            int old = __atomic_load_n(&max_seen_carriers, __ATOMIC_ACQUIRE);
            while (
                carriers > old
                && !__atomic_compare_exchange_n(
                    &max_seen_carriers,
                    &old,
                    (int)carriers,
                    0,
                    __ATOMIC_ACQ_REL,
                    __ATOMIC_ACQUIRE
                )
            ) {}
            __atomic_add_fetch(&resume_calls, 1, __ATOMIC_ACQ_REL);
        }

        static PyObject *make_vthread(void) {
            int32_t frame_map[1] = {0};
            PyObject *cont = py_continuation_new(frame_map, 0, (void *)&resume_step);
            if (cont == 0) return 0;
            PyObject *vt = py_virtual_thread_new(cont);
            pcc_gc_release(cont);
            return vt;
        }

        int main(void) {
            if (!pcc_threads_enabled()) return 2;
            if (pcc_gc_set_backend(PCC_GC_KIND_REFCOUNT_CYCLE) != 0) return 3;

            PyObject *threads[8];
            for (int i = 0; i < 8; i++) {
                threads[i] = make_vthread();
                if (threads[i] == 0) return 4;
                if (py_virtual_thread_start(threads[i]) != 0) return 5;
            }
            if (py_virtual_thread_ready_count() != 8) return 6;

            int64_t ran = py_virtual_thread_run_carrier_pool(3, 8);
            if (ran != 8) return 7;
            if (__atomic_load_n(&resume_calls, __ATOMIC_ACQUIRE) != 8) return 8;
            if (__atomic_load_n(&max_seen_carriers, __ATOMIC_ACQUIRE) < 2) return 9;
            if (py_virtual_thread_ready_count() != 0) return 10;
            if (py_virtual_thread_carrier_count() != 1) return 11;

            for (int i = 0; i < 8; i++) {
                if (py_virtual_thread_state(threads[i]) != 4) return 12;
                pcc_gc_release(threads[i]);
            }

            printf("virtual-thread-carrier-pool-ok\\n");
            return 0;
        }
        """,
        with_threads=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "virtual-thread-carrier-pool-ok"


def test_virtual_thread_typed_resume_and_persistent_pool_pin_blocking(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <poll.h>
        #include <stdio.h>

        static PyObject *global_lock = 0;

        static int64_t resume_typed(PyObject *vthread, PyObject *cont) {
            PyObject *current = py_virtual_thread_current();
            if (current != vthread) return -2;
            py_decref(current);

            if (py_continuation_resume_abi(cont) != 1) return -3;
            PyObject *saved = py_continuation_get_slot(cont, 0);
            int overflow = 0;
            int64_t raw = py_int_to_i64(saved, &overflow);
            py_decref(saved);
            if (overflow || raw != 41) return -4;

            int64_t pin_before = py_virtual_thread_pin_event_count();
            if (py_threading_lock_acquire(global_lock) != 0) return -5;
            if (py_threading_lock_release(global_lock) != 0) return -6;
            if (py_virtual_thread_pin_event_count() <= pin_before) return -7;

            PyObject *result = py_int_from_i64(raw + 1);
            int64_t rc = py_virtual_thread_complete(vthread, result);
            py_decref(result);
            return rc;
        }

        int main(void) {
            global_lock = py_threading_lock_new();
            if (global_lock == 0) return 2;

            int32_t frame_map[1] = {1};
            PyObject *slots[1] = {py_int_from_i64(41)};
            PyObject *cont = py_continuation_new_typed(frame_map, slots, (void *)&resume_typed);
            py_decref(slots[0]);
            if (cont == 0) return 3;
            if (py_continuation_resume_abi(cont) != 1) return 4;

            PyObject *vt = py_virtual_thread_new(cont);
            py_decref(cont);
            if (vt == 0) return 5;
            if (py_virtual_thread_carrier_pool_start(2) != 2) return 6;
            for (int i = 0; i < 200 && py_virtual_thread_carrier_count() < 2; i++) {
                (void)poll(NULL, 0, 1);
            }
            if (py_virtual_thread_carrier_count() < 2) return 7;
            if (py_virtual_thread_start(vt) != 0) return 8;
            for (int i = 0; i < 200 && py_virtual_thread_state(vt) != 4; i++) {
                (void)poll(NULL, 0, 1);
            }
            if (py_virtual_thread_state(vt) != 4) return 9;
            if (py_virtual_thread_carrier_pool_stop() != 2) return 10;
            if (py_virtual_thread_carrier_count() != 1) return 11;

            PyObject *result = py_virtual_thread_result(vt);
            int overflow = 0;
            int64_t raw = py_int_to_i64(result, &overflow);
            py_decref(result);
            if (overflow || raw != 42) return 12;

            py_decref(global_lock);
            py_decref(vt);
            printf("virtual-thread-typed-resume-persistent-pool-ok\\n");
            return 0;
        }
        """,
        with_threads=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "virtual-thread-typed-resume-persistent-pool-ok"


def test_coroutine_root_public_symbols_are_wired():
    header = (RUNTIME_DIR / "include" / "py_runtime.h").read_text(encoding="utf-8")
    py_src = (RUNTIME_DIR / "py" / "py_gc_backend.py").read_text(encoding="utf-8")
    registry_src = (
        RUNTIME_DIR / "py" / "freestanding_gc_root_registry.py"
    ).read_text(encoding="utf-8")
    introspection_src = (
        RUNTIME_DIR / "py" / "freestanding_gc_root_introspection.py"
    ).read_text(encoding="utf-8")
    mapped_src = (
        RUNTIME_DIR / "py" / "freestanding_gc_mapped_roots.py"
    ).read_text(encoding="utf-8")
    abi = (REPO_ROOT / "pcc" / "frontends" / "python" / "codegen" / "runtime_abi.py").read_text(
        encoding="utf-8"
    )

    assert "PCC_GC_COUNTER_SCHEDULER_ROOTS" in header
    assert "PCC_GC_COUNTER_FRAME_ROOT_SLOTS" in header
    assert "pcc_gc_scheduler_root_count" in header
    assert "pcc_gc_scheduler_root_register_handle" in header
    assert "pcc_gc_scheduler_root_unregister_handle" in header
    assert "pcc_gc_register_continuation_root" in header
    assert "pcc_gc_rewrite_continuation_roots" in header
    assert "PY_TYPE_CONTINUATION" in header
    assert "py_continuation_new" in header
    assert "py_continuation_new_typed" in header
    assert "py_continuation_resume_abi" in header
    assert "py_virtual_thread_new" in header
    assert "py_virtual_thread_current" in header
    assert "py_virtual_thread_resume_generator" in header
    assert "py_virtual_thread_start" in header
    assert "py_virtual_thread_park" in header
    assert "py_virtual_thread_unpark" in header
    assert "py_virtual_thread_run_once" in header
    assert "py_virtual_thread_run_until_idle" in header
    assert "py_virtual_thread_run_carrier_pool" in header
    assert "py_virtual_thread_carrier_pool_start" in header
    assert "py_virtual_thread_carrier_pool_stop" in header
    assert "py_virtual_thread_carrier_steal_count" in header
    assert "py_virtual_thread_node_pool_stat" in header
    assert "py_virtual_thread_result" in header
    assert "py_virtual_thread_sleep" in header
    assert "py_virtual_thread_block_on_fd" in header
    assert "py_virtual_thread_pin_enter" in header
    assert "py_threading_lock_acquire_vthread" in header
    assert "py_threading_event_wait_vthread" in header
    assert '@c_abi_export("pcc_gc_scheduler_root_count")' in introspection_src
    assert (
        '@c_abi_export("pcc_gc_continuation_root_slot_count")'
        in introspection_src
    )
    assert '@c_abi_export("pcc_gc_scheduler_root_register_handle")' in registry_src
    assert '@c_abi_export("pcc_gc_scheduler_root_unregister_handle")' in registry_src
    assert "PCC_GC_SCHEDULER_QUEUE_ENTRY_POOL_LIMIT" in py_src
    assert "_scheduler_queue_entry_recycle" in py_src
    assert '@c_abi_export("pcc_gc_register_continuation_root")' in registry_src
    assert '@c_abi_export("pcc_gc_trace_continuation_roots")' in mapped_src
    assert '@c_abi_export("pcc_gc_rewrite_continuation_roots")' in mapped_src
    assert '@c_abi_export("py_continuation_new")' in (
        RUNTIME_DIR / "py" / "py_coroutine.py"
    ).read_text(encoding="utf-8")
    assert '"pcc_gc_coroutine_root_score": (_I64, [], False)' in abi
    assert '"pcc_gc_scheduler_root_register_handle": (_PTR, [_PTR], False)' in abi
    assert '"pcc_gc_scheduler_root_unregister_handle": (_VOID, [_PTR], False)' in abi
    assert '"py_continuation_mount": (_I64, [_PYOBJ, _PTR], False)' in abi
    assert '"py_continuation_new_typed": (_PYOBJ, [_PTR, _PTR, _PTR], False)' in abi
    assert '"py_continuation_resume_abi": (_I64, [_PYOBJ], False)' in abi
    assert (
        '"py_virtual_thread_resume_generator": (_I64, [_PYOBJ, _PYOBJ], False)' in abi
    )
    assert '"py_virtual_thread_start": (_I64, [_PYOBJ], False)' in abi
    assert '"py_threading_lock_acquire_vthread": (_I64, [_PYOBJ], False)' in abi
    assert '"py_virtual_thread_poll_io": (_I64, [_I64], False)' in abi
    assert '"py_virtual_thread_run_until_idle": (_I64, [_I64], False)' in abi
    assert '"py_virtual_thread_run_carrier_pool": (_I64, [_I64, _I64], False)' in abi
    assert '"py_virtual_thread_carrier_pool_start": (_I64, [_I64], False)' in abi
    assert '"py_virtual_thread_carrier_steal_count": (_I64, [], False)' in abi
    assert (
        '"py_virtual_thread_node_pool_stat": (_I64, [_I64, _I64], False)'
        in abi
    )
    assert '"py_virtual_thread_result": (_PYOBJ, [_PYOBJ], False)' in abi
    assert '"pcc_gc_rewrite_continuation_roots": (_I64, [], False)' in abi
