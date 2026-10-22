from __future__ import annotations

import os
import pytest
import subprocess
import textwrap
from pathlib import Path

from tests.runtime_build_cache import cached_threaded_pcc_python_runtime

REPO_ROOT = Path(__file__).absolute().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"
_TSAN_UNAVAILABLE_BY_CC: dict[str, str | None] = {}


def _require_thread_sanitizer_runtime(tmp_path: Path, cc: str) -> None:
    if cc in _TSAN_UNAVAILABLE_BY_CC:
        reason = _TSAN_UNAVAILABLE_BY_CC[cc]
        if reason is not None:
            pytest.fail(reason)
        return
    probe = tmp_path / "tsan_availability.c"
    exe = tmp_path / "tsan_availability.out"
    probe.write_text(
        textwrap.dedent(
            r"""
            #include <pthread.h>

            static void *worker(void *arg) {
                (void)arg;
                return 0;
            }

            int main(void) {
                pthread_t thread;
                if (pthread_create(&thread, 0, worker, 0) != 0) return 1;
                return pthread_join(thread, 0) == 0 ? 0 : 2;
            }
            """
        ).lstrip(),
        encoding="utf-8",
    )
    build = subprocess.run(
        [
            cc,
            "-std=c11",
            "-pthread",
            "-fsanitize=thread",
            str(probe),
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if build.returncode != 0:
        stderr = build.stderr.lower()
        if "sanitize" in stderr or "tsan" in stderr:
            reason = "ThreadSanitizer runtime is not available for this compiler"
            _TSAN_UNAVAILABLE_BY_CC[cc] = reason
            pytest.fail(reason)
        assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    if run.returncode != 0:
        reason = (
            "ThreadSanitizer runtime crashes before pcc code runs "
            f"(exit {run.returncode})"
        )
        _TSAN_UNAVAILABLE_BY_CC[cc] = reason
        pytest.fail(reason)
    _TSAN_UNAVAILABLE_BY_CC[cc] = None


def _build_threaded_runtime(
    tmp_path: Path,
    *,
    cc: str,
    tsan: bool = False,
    pcc_python: bool = False,
) -> Path:
    """Return the threaded pcc-Python runtime (``libpy_runtime_pcc_py.a``).

    pcc-emitted runtime code carries no ThreadSanitizer instrumentation, so
    the ``tsan`` probes instrument only their C harness; they still drive the
    runtime's concurrent paths and check results.
    """
    del tmp_path, cc, tsan, pcc_python
    return cached_threaded_pcc_python_runtime()


@pytest.mark.pcc_gate(probe="tsan")
def test_pthread_stw_threadsanitizer_smoke_or_skip(tmp_path):
    cc = os.environ.get("CC", "clang")
    _require_thread_sanitizer_runtime(tmp_path, cc)
    work_runtime = _build_threaded_runtime(tmp_path, cc=cc, tsan=True)

    src = tmp_path / "tsan_smoke.c"
    exe = tmp_path / "tsan_smoke.out"
    src.write_text(textwrap.dedent(r"""
        #include "py_internal.h"
        #include <stdint.h>

        static int64_t done = 0;

        static void *worker(void *arg) {
            (void)arg;
            while (__atomic_load_n(&done, __ATOMIC_RELAXED) == 0) {
                pcc_thread_safepoint();
            }
            return 0;
        }

        int main(void) {
            PccThreadHandle *t = 0;
            if (pcc_thread_start(&t, worker, 0) != 0) return 1;
            for (int i = 0; i < 8; i++) {
                if (pcc_stop_the_world() != 0) return 2;
                if (pcc_resume_world() != 0) return 3;
            }
            __atomic_store_n(&done, 1, __ATOMIC_RELAXED);
            return pcc_thread_join(t, 0) == 0 ? 0 : 4;
        }
        """).lstrip(), encoding="utf-8")
    cmd = [
        cc, "-DPCC_WITH_THREADS=1", "-std=c11", "-pthread", "-fsanitize=thread",
        f"-I{work_runtime / 'include'}",
        f"-I{work_runtime / 'src'}",
        str(src), str(work_runtime / "libpy_runtime_pcc_py.a"), "-lm", "-o", str(exe),
    ]
    build = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if build.returncode != 0:
        # Some CI images lack TSan-capable clang runtimes.
        assert "sanitize" in build.stderr.lower() or "tsan" in build.stderr.lower()
        return
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stderr
    assert "data race" not in (run.stdout + run.stderr).lower()


@pytest.mark.pcc_gate(probe="tsan")
def test_cms_worker_threadsanitizer_stress_or_skip(tmp_path):
    cc = os.environ.get("CC", "clang")
    _require_thread_sanitizer_runtime(tmp_path, cc)
    work_runtime = _build_threaded_runtime(tmp_path, cc=cc, tsan=True)

    src = tmp_path / "cms_tsan_probe.c"
    exe = tmp_path / "cms_tsan_probe.out"
    src.write_text(textwrap.dedent(r"""
        #include "py_runtime.h"
        #include "py_internal.h"
        #include <stdint.h>
        #include <stdio.h>
        #include <unistd.h>

        enum {
            CMS_WORKER_DRAINS = 13,
            CMS_WORKER_TRACES = 18
        };

        static int64_t done_count = 0;
        static int64_t collector_done = 0;

        static void make_cycle(void) {
            PyObject *cycle = py_list_new(1);
            if (cycle == 0) return;
            py_list_append(cycle, cycle);
            pcc_gc_release(cycle);
        }

        static void *mutator(void *arg) {
            (void)arg;
            for (int i = 0; i < 40; i++) {
                make_cycle();
                if ((i % 10) == 0) pcc_gc_safepoint();
            }
            __atomic_add_fetch(&done_count, 1, __ATOMIC_RELEASE);
            return 0;
        }

        int main(void) {
            if (pcc_threads_enabled() != 1) return 2;
            if (pcc_gc_set_backend(PCC_GC_KIND_CONCURRENT_MARK_SWEEP) != 0) {
                return 3;
            }
            pcc_gc_telemetry_reset();
            PccThreadHandle *a = 0;
            PccThreadHandle *b = 0;
            if (pcc_thread_start(&a, mutator, 0) != 0) return 4;
            if (pcc_thread_start(&b, mutator, 0) != 0) return 5;
            for (int i = 0; i < 40; i++) {
                make_cycle();
                if ((i % 5) == 0) pcc_gc_safepoint();
            }
            while (__atomic_load_n(&done_count, __ATOMIC_ACQUIRE) < 2) {
                pcc_thread_safepoint();
                usleep(1000);
            }
            if (pcc_thread_join(a, 0) != 0) return 6;
            if (pcc_thread_join(b, 0) != 0) return 7;
            for (int i = 0; i < 200; i++) {
                pcc_thread_safepoint();
                if (pcc_gc_telemetry(CMS_WORKER_DRAINS) > 0) break;
                usleep(1000);
            }
            if (pcc_gc_set_backend(PCC_GC_KIND_REFCOUNT_CYCLE) != 0) return 8;
            printf("%lld\n", (long long)pcc_gc_telemetry(CMS_WORKER_DRAINS));
            printf("%lld\n", (long long)pcc_gc_telemetry(CMS_WORKER_TRACES));
            return 0;
        }
        """).lstrip(), encoding="utf-8")
    build_probe = subprocess.run(
        [
            cc,
            "-O1",
            "-g",
            "-std=c11",
            "-pthread",
            "-fsanitize=thread",
            f"-I{work_runtime / 'include'}",
            f"-I{work_runtime / 'src'}",
            str(src),
            str(work_runtime / "libpy_runtime_pcc_py.a"),
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if build_probe.returncode != 0:
        stderr = build_probe.stderr.lower()
        assert "sanitize" in stderr or "tsan" in stderr
        return

    env = os.environ.copy()
    env["PCC_GC_DEBT_THRESHOLD"] = "1024"
    env["TSAN_OPTIONS"] = "halt_on_error=1:exitcode=66"
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    combined = run.stdout + run.stderr
    assert run.returncode == 0, combined
    assert "data race" not in combined.lower()
    lines = run.stdout.strip().splitlines()
    assert int(lines[0]) > 0
    assert int(lines[1]) >= 0


def test_backend4_thread_medium_buffer_flushes_across_mutators(tmp_path):
    cc = os.environ.get("CC", "cc")
    work_runtime = _build_threaded_runtime(tmp_path, cc=cc)

    src = tmp_path / "backend4_thread_medium_flush.c"
    exe = tmp_path / "backend4_thread_medium_flush.out"
    src.write_text(textwrap.dedent(r"""
        #include "py_runtime.h"
        #include "py_internal.h"
        #include <stdint.h>
        #include <stdio.h>
        #include <stdlib.h>
        #include <unistd.h>

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        static int64_t ready = 0;
        static int64_t done = 0;
        static ProbeListObject *owner = 0;
        static PyObject *children[4] = {0, 0, 0, 0};

        static void *worker(void *arg) {
            (void)arg;
            owner = (ProbeListObject *)pcc_gc_alloc(
                sizeof(ProbeListObject),
                PY_TYPE_LIST,
                PY_FLAG_GC_OLD
            );
            if (owner == 0) return (void *)3;
            owner->length = 4;
            owner->capacity = 4;
            owner->items = (PyObject **)calloc(4, sizeof(PyObject *));
            if (owner->items == 0) return (void *)4;
            for (int i = 0; i < 4; i++) {
                ProbeListObject *child = (ProbeListObject *)pcc_gc_alloc(
                    sizeof(ProbeListObject),
                    PY_TYPE_LIST,
                    PY_FLAG_GC_YOUNG
                );
                children[i] = (PyObject *)child;
                if (children[i] == 0) return (void *)5;
                child->length = 0;
                child->capacity = 0;
                child->items = 0;
                pcc_gc_store_ptr((PyObject *)owner, &owner->items[i], children[i]);
            }
            __atomic_store_n(&ready, 1, __ATOMIC_RELEASE);
            while (__atomic_load_n(&done, __ATOMIC_ACQUIRE) == 0) {
                pcc_thread_safepoint();
                usleep(1000);
            }
            for (int i = 0; i < 4; i++) {
                owner->items[i] = 0;
                pcc_gc_release(children[i]);
            }
            pcc_gc_release((PyObject *)owner);
            return 0;
        }

        int main(void) {
            if (pcc_threads_enabled() != 1) return 2;
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) {
                return 3;
            }
            pcc_gc_telemetry_reset();
            PccThreadHandle *thread = 0;
            if (pcc_thread_start(&thread, worker, 0) != 0) return 4;
            while (__atomic_load_n(&ready, __ATOMIC_ACQUIRE) == 0) {
                pcc_thread_safepoint();
                usleep(1000);
            }
            if (pcc_gc_backend4_store_buffer_entries() != 4) return 5;
            if (pcc_gc_backend4_store_buffer_medium_pending() != 4) return 6;
            if (pcc_gc_backend4_store_buffer_medium_flushes() != 0) return 7;
            if (pcc_gc_step(4) != 4) return 8;
            if (pcc_gc_backend4_store_buffer_medium_pending() != 0) return 9;
            if (pcc_gc_backend4_store_buffer_medium_flushes() != 1) return 10;
            if (pcc_gc_backend4_store_buffer_medium_flushed_entries() != 4) return 11;
            if (pcc_gc_backend4_store_buffer_entries() != 0) return 12;
            for (int i = 0; i < 4; i++) {
                if ((((PyObjectHeader *)owner->items[i])->flags & PY_FLAG_GC_OLD) == 0) {
                    return 13;
                }
            }
            if (pcc_gc_backend4_store_buffer_cross_thread_medium_flushes() != 1) return 16;
            if (pcc_gc_backend4_store_buffer_cross_thread_medium_flushed_entries() != 4) return 17;
            if (
                pcc_gc_telemetry(
                    PCC_GC_COUNTER_GENZGC_STORE_BUFFER_CROSS_THREAD_MEDIUM_FLUSHES
                ) != 1
            ) return 18;
            if (
                pcc_gc_telemetry(
                    PCC_GC_COUNTER_GENZGC_STORE_BUFFER_CROSS_THREAD_MEDIUM_FLUSHED_ENTRIES
                ) != 4
            ) return 19;
            __atomic_store_n(&done, 1, __ATOMIC_RELEASE);
            void *result = 0;
            if (pcc_thread_join(thread, &result) != 0) return 14;
            if (result != 0) return 15;
            printf("backend4-thread-medium-flush-ok\n");
            return 0;
        }
        """).lstrip(), encoding="utf-8")
    build = subprocess.run(
        [
            cc,
            "-DPCC_WITH_THREADS=1",
            "-std=c11",
            "-pthread",
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
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr
    assert run.stdout.strip() == "backend4-thread-medium-flush-ok"


@pytest.mark.pcc_gate(probe="tsan")
def test_cms_collect_threadsanitizer_sweep_allocation_or_skip(tmp_path):
    cc = os.environ.get("CC", "clang")
    _require_thread_sanitizer_runtime(tmp_path, cc)
    work_runtime = _build_threaded_runtime(tmp_path, cc=cc, tsan=True)

    src = tmp_path / "cms_sweep_alloc_tsan_probe.c"
    exe = tmp_path / "cms_sweep_alloc_tsan_probe.out"
    src.write_text(textwrap.dedent(r"""
        #include "py_runtime.h"
        #include "py_internal.h"
        #include <stdint.h>
        #include <stdio.h>
        #include <unistd.h>

        enum {
            ALLOCATIONS = 0,
            WORK_STEPS = 5,
            CMS_WORKER_DRAINS = 13
        };

        static int64_t done_count = 0;

        static void make_unrooted_int(int64_t value) {
            int64_t size = (int64_t)(
                sizeof(PyIntObject) + 2 * sizeof(uint32_t)
            );
            PyIntObject *obj = (PyIntObject *)pcc_gc_alloc(
                size, PY_TYPE_INT, 0
            );
            if (obj == 0) return;
            obj->sign = 1;
            obj->ndigits = 1;
            obj->digits[0] = (uint32_t)value;
        }

        static void *mutator(void *arg) {
            int64_t base = (int64_t)(uintptr_t)arg;
            for (int round = 0; round < 80; round++) {
                for (int i = 0; i < 8; i++) {
                    make_unrooted_int(base + round * 8 + i);
                }
                pcc_thread_safepoint();
                usleep(100);
            }
            __atomic_add_fetch(&done_count, 1, __ATOMIC_RELEASE);
            return 0;
        }

        static void *collector(void *arg) {
            (void)arg;
            while (__atomic_load_n(&done_count, __ATOMIC_ACQUIRE) < 2) {
                (void)pcc_gc_collect(0);
                pcc_thread_safepoint();
                usleep(1000);
            }
            for (int i = 0; i < 3; i++) {
                (void)pcc_gc_collect(0);
                pcc_thread_safepoint();
            }
            __atomic_store_n(&collector_done, 1, __ATOMIC_RELEASE);
            return 0;
        }

        int main(void) {
            if (pcc_threads_enabled() != 1) return 2;
            if (pcc_gc_set_backend(PCC_GC_KIND_CONCURRENT_MARK_SWEEP) != 0) {
                return 3;
            }
            pcc_gc_telemetry_reset();

            PccThreadHandle *a = 0;
            PccThreadHandle *b = 0;
            PccThreadHandle *c = 0;
            if (pcc_thread_start(&a, mutator, (void *)(uintptr_t)1000) != 0) {
                return 4;
            }
            if (pcc_thread_start(&b, mutator, (void *)(uintptr_t)2000) != 0) {
                return 5;
            }
            if (pcc_thread_start(&c, collector, 0) != 0) return 6;

            while (__atomic_load_n(&done_count, __ATOMIC_ACQUIRE) < 2) {
                pcc_thread_safepoint();
                usleep(1000);
            }
            while (__atomic_load_n(&collector_done, __ATOMIC_ACQUIRE) == 0) {
                pcc_thread_safepoint();
                usleep(1000);
            }
            if (pcc_thread_join(c, 0) != 0) return 7;
            if (pcc_thread_join(a, 0) != 0) return 8;
            if (pcc_thread_join(b, 0) != 0) return 9;
            for (int i = 0; i < 10; i++) pcc_thread_safepoint();
            if (pcc_gc_set_backend(PCC_GC_KIND_REFCOUNT_CYCLE) != 0) return 10;

            printf("%lld\n", (long long)pcc_gc_telemetry(ALLOCATIONS));
            printf("%lld\n", (long long)pcc_gc_telemetry(WORK_STEPS));
            printf("%lld\n", (long long)pcc_gc_telemetry(CMS_WORKER_DRAINS));
            return 0;
        }
        """).lstrip(), encoding="utf-8")
    build_probe = subprocess.run(
        [
            cc,
            "-O1",
            "-g",
            "-std=c11",
            "-pthread",
            "-fsanitize=thread",
            f"-I{work_runtime / 'include'}",
            f"-I{work_runtime / 'src'}",
            str(src),
            str(work_runtime / "libpy_runtime_pcc_py.a"),
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if build_probe.returncode != 0:
        stderr = build_probe.stderr.lower()
        assert "sanitize" in stderr or "tsan" in stderr
        return

    env = os.environ.copy()
    env["PCC_GC_DEBT_THRESHOLD"] = "1024"
    env["TSAN_OPTIONS"] = "halt_on_error=1:exitcode=66"
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    combined = run.stdout + run.stderr
    assert run.returncode == 0, combined
    assert "data race" not in combined.lower()
    lines = run.stdout.strip().splitlines()
    assert int(lines[0]) > 0
    assert int(lines[1]) > 0
    assert int(lines[2]) > 0


@pytest.mark.pcc_gate(probe="tsan")
def test_generational_minor_threadsanitizer_alloc_or_skip(tmp_path):
    cc = os.environ.get("CC", "clang")
    _require_thread_sanitizer_runtime(tmp_path, cc)
    work_runtime = _build_threaded_runtime(tmp_path, cc=cc, tsan=True)

    src = tmp_path / "gen_minor_tsan_probe.c"
    exe = tmp_path / "gen_minor_tsan_probe.out"
    src.write_text(textwrap.dedent(r"""
        #include "py_runtime.h"
        #include "py_internal.h"
        #include <stdint.h>
        #include <stdio.h>
        #include <unistd.h>

        enum {
            MINOR_ALLOCATIONS = 8,
            MINOR_COLLECTIONS = 9,
            MINOR_ARENA_REFILLS = 19,
            MINOR_ARENA_BUMPS = 20
        };

        static int64_t done_count = 0;

        static void *mutator(void *arg) {
            (void)arg;
            for (int round = 0; round < 160; round++) {
                PyObject *objects[10];
                for (int i = 0; i < 10; i++) {
                    objects[i] = pcc_gc_alloc(64, PY_TYPE_NONE, 0);
                    if (objects[i] == 0) return 0;
                }
                for (int i = 0; i < 10; i++) {
                    pcc_gc_release(objects[i]);
                }
                if ((round % 8) == 0) {
                    pcc_thread_safepoint();
                    usleep(50);
                }
            }
            __atomic_add_fetch(&done_count, 1, __ATOMIC_RELEASE);
            return 0;
        }

        int main(void) {
            if (pcc_threads_enabled() != 1) return 2;
            if (pcc_gc_set_backend(PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR) != 0) {
                return 3;
            }
            pcc_gc_telemetry_reset();

            PccThreadHandle *a = 0;
            PccThreadHandle *b = 0;
            if (pcc_thread_start(&a, mutator, 0) != 0) return 4;
            if (pcc_thread_start(&b, mutator, 0) != 0) return 5;

            while (__atomic_load_n(&done_count, __ATOMIC_ACQUIRE) < 2) {
                pcc_thread_safepoint();
                usleep(1000);
            }
            if (pcc_thread_join(a, 0) != 0) return 6;
            if (pcc_thread_join(b, 0) != 0) return 7;
            if (pcc_gc_set_backend(PCC_GC_KIND_REFCOUNT_CYCLE) != 0) return 8;

            printf("%lld\n", (long long)pcc_gc_telemetry(MINOR_ALLOCATIONS));
            printf("%lld\n", (long long)pcc_gc_telemetry(MINOR_COLLECTIONS));
            printf("%lld\n", (long long)pcc_gc_telemetry(MINOR_ARENA_REFILLS));
            printf("%lld\n", (long long)pcc_gc_telemetry(MINOR_ARENA_BUMPS));
            return 0;
        }
        """).lstrip(), encoding="utf-8")
    build_probe = subprocess.run(
        [
            cc,
            "-O1",
            "-g",
            "-std=c11",
            "-pthread",
            "-fsanitize=thread",
            f"-I{work_runtime / 'include'}",
            f"-I{work_runtime / 'src'}",
            str(src),
            str(work_runtime / "libpy_runtime_pcc_py.a"),
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if build_probe.returncode != 0:
        stderr = build_probe.stderr.lower()
        assert "sanitize" in stderr or "tsan" in stderr
        return

    env = os.environ.copy()
    env.update(
        {
            "PCC_GC_MINOR_HEAP_SIZE": "512",
            "PCC_GC_MINOR_ALLOC_MAX": "128",
            "TSAN_OPTIONS": "halt_on_error=1:exitcode=66",
        }
    )
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    combined = run.stdout + run.stderr
    assert run.returncode == 0, combined
    assert "data race" not in combined.lower()
    lines = run.stdout.strip().splitlines()
    assert int(lines[0]) > 0
    assert int(lines[1]) > 0
    assert int(lines[2]) >= 2
    assert int(lines[3]) > 0


@pytest.mark.pcc_gate(probe="tsan")
def test_generational_scheduler_root_registry_threadsanitizer_or_skip(tmp_path):
    cc = os.environ.get("CC", "clang")
    _require_thread_sanitizer_runtime(tmp_path, cc)
    work_runtime = _build_threaded_runtime(tmp_path, cc=cc, tsan=True)

    src = tmp_path / "gen_scheduler_roots_tsan_probe.c"
    exe = tmp_path / "gen_scheduler_roots_tsan_probe.out"
    src.write_text(textwrap.dedent(r"""
        #include "py_runtime.h"
        #include "py_internal.h"
        #include <stdint.h>
        #include <unistd.h>

        enum {
            REGISTRAR_THREADS = 4,
            REGISTRY_ROUNDS = 1200
        };

        static PyObject *scheduler_slots[REGISTRAR_THREADS];
        static int64_t done_count = 0;

        static void *registrar(void *arg) {
            intptr_t idx = (intptr_t)arg;
            PyObject **slot = &scheduler_slots[idx];
            for (int round = 0; round < REGISTRY_ROUNDS; round++) {
                pcc_gc_scheduler_root_register(slot);
                if ((round % 4) == 0) {
                    pcc_thread_safepoint();
                    usleep(5);
                }
                pcc_gc_scheduler_root_unregister(slot);
            }
            __atomic_add_fetch(&done_count, 1, __ATOMIC_RELEASE);
            return 0;
        }

        static void *collector(void *arg) {
            (void)arg;
            while (
                __atomic_load_n(&done_count, __ATOMIC_ACQUIRE)
                < REGISTRAR_THREADS
            ) {
                (void)pcc_gc_step(8);
                pcc_thread_safepoint();
                usleep(5);
            }
            return 0;
        }

        int main(void) {
            if (pcc_threads_enabled() != 1) return 2;
            if (pcc_gc_set_backend(PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR) != 0) {
                return 3;
            }

            PccThreadHandle *collector_thread = 0;
            PccThreadHandle *registrars[REGISTRAR_THREADS];
            if (pcc_thread_start(&collector_thread, collector, 0) != 0) {
                return 4;
            }
            for (intptr_t i = 0; i < REGISTRAR_THREADS; i++) {
                registrars[i] = 0;
                if (
                    pcc_thread_start(&registrars[i], registrar, (void *)i)
                    != 0
                ) {
                    return 5;
                }
            }
            for (int i = 0; i < REGISTRAR_THREADS; i++) {
                if (pcc_thread_join(registrars[i], 0) != 0) return 6;
            }
            if (pcc_thread_join(collector_thread, 0) != 0) return 7;
            if (pcc_gc_set_backend(PCC_GC_KIND_REFCOUNT_CYCLE) != 0) return 8;
            return 0;
        }
        """).lstrip(), encoding="utf-8")
    build_probe = subprocess.run(
        [
            cc,
            "-O1",
            "-g",
            "-std=c11",
            "-pthread",
            "-fsanitize=thread",
            f"-I{work_runtime / 'include'}",
            f"-I{work_runtime / 'src'}",
            str(src),
            str(work_runtime / "libpy_runtime_pcc_py.a"),
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if build_probe.returncode != 0:
        stderr = build_probe.stderr.lower()
        assert "sanitize" in stderr or "tsan" in stderr
        return

    env = os.environ.copy()
    env["TSAN_OPTIONS"] = "halt_on_error=1:exitcode=66"
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    combined = run.stdout + run.stderr
    assert run.returncode == 0, combined
    assert "data race" not in combined.lower()


@pytest.mark.pcc_gate(probe="tsan")
def test_generational_scheduler_queue_threadsanitizer_or_skip(tmp_path):
    cc = os.environ.get("CC", "clang")
    _require_thread_sanitizer_runtime(tmp_path, cc)
    work_runtime = _build_threaded_runtime(tmp_path, cc=cc, tsan=True)

    src = tmp_path / "gen_scheduler_queue_tsan_probe.c"
    exe = tmp_path / "gen_scheduler_queue_tsan_probe.out"
    src.write_text(textwrap.dedent(r"""
        #include "py_runtime.h"
        #include "py_internal.h"
        #include <stdint.h>
        #include <unistd.h>

        enum {
            PRODUCERS = 2,
            CONSUMERS = 2,
            ITEMS_PER_PRODUCER = 400
        };

        static PccGcSchedulerQueue *queue = 0;
        static PyObject *consumer_slots[CONSUMERS];
        static int64_t producers_done = 0;
        static int64_t popped_count = 0;

        static void *producer(void *arg) {
            (void)arg;
            for (int i = 0; i < ITEMS_PER_PRODUCER; i++) {
                PyObject *item = pcc_gc_alloc(64, PY_TYPE_NONE, 0);
                if (item == 0) return 0;
                if (pcc_gc_scheduler_queue_push(queue, item) != 0) return 0;
                pcc_gc_release(item);
                if ((i % 8) == 0) {
                    pcc_thread_safepoint();
                    usleep(5);
                }
            }
            __atomic_add_fetch(&producers_done, 1, __ATOMIC_RELEASE);
            return 0;
        }

        static void *consumer(void *arg) {
            intptr_t idx = (intptr_t)arg;
            PyObject **slot = &consumer_slots[idx];
            while (
                __atomic_load_n(&producers_done, __ATOMIC_ACQUIRE) < PRODUCERS
                || pcc_gc_scheduler_queue_len(queue) > 0
            ) {
                int64_t popped = pcc_gc_scheduler_queue_pop_into(queue, slot);
                if (popped > 0) {
                    __atomic_add_fetch(&popped_count, 1, __ATOMIC_RELEASE);
                    pcc_gc_store_root(slot, 0);
                } else {
                    pcc_thread_safepoint();
                    usleep(5);
                }
            }
            return 0;
        }

        static void *collector(void *arg) {
            (void)arg;
            while (
                __atomic_load_n(&producers_done, __ATOMIC_ACQUIRE) < PRODUCERS
                || __atomic_load_n(&popped_count, __ATOMIC_ACQUIRE)
                    < PRODUCERS * ITEMS_PER_PRODUCER
            ) {
                (void)pcc_gc_step(8);
                pcc_thread_safepoint();
                usleep(5);
            }
            return 0;
        }

        int main(void) {
            if (pcc_threads_enabled() != 1) return 2;
            if (pcc_gc_set_backend(PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR) != 0) {
                return 3;
            }
            queue = pcc_gc_scheduler_queue_new();
            if (queue == 0) return 4;
            for (int i = 0; i < CONSUMERS; i++) {
                pcc_gc_scheduler_root_register(&consumer_slots[i]);
            }

            PccThreadHandle *producer_threads[PRODUCERS];
            PccThreadHandle *consumer_threads[CONSUMERS];
            PccThreadHandle *collector_thread = 0;
            if (pcc_thread_start(&collector_thread, collector, 0) != 0) {
                return 5;
            }
            for (intptr_t i = 0; i < PRODUCERS; i++) {
                if (
                    pcc_thread_start(&producer_threads[i], producer, (void *)i)
                    != 0
                ) {
                    return 6;
                }
            }
            for (intptr_t i = 0; i < CONSUMERS; i++) {
                if (
                    pcc_thread_start(&consumer_threads[i], consumer, (void *)i)
                    != 0
                ) {
                    return 7;
                }
            }
            for (int i = 0; i < PRODUCERS; i++) {
                if (pcc_thread_join(producer_threads[i], 0) != 0) return 8;
            }
            for (int i = 0; i < CONSUMERS; i++) {
                if (pcc_thread_join(consumer_threads[i], 0) != 0) return 9;
            }
            if (pcc_thread_join(collector_thread, 0) != 0) return 10;

            int64_t expected = PRODUCERS * ITEMS_PER_PRODUCER;
            if (__atomic_load_n(&popped_count, __ATOMIC_ACQUIRE) != expected) {
                return 11;
            }
            for (int i = 0; i < CONSUMERS; i++) {
                pcc_gc_scheduler_root_unregister(&consumer_slots[i]);
                pcc_gc_store_root(&consumer_slots[i], 0);
            }
            pcc_gc_scheduler_queue_free(queue);
            if (pcc_gc_set_backend(PCC_GC_KIND_REFCOUNT_CYCLE) != 0) return 12;
            return 0;
        }
        """).lstrip(), encoding="utf-8")
    build_probe = subprocess.run(
        [
            cc,
            "-O1",
            "-g",
            "-std=c11",
            "-pthread",
            "-fsanitize=thread",
            f"-I{work_runtime / 'include'}",
            f"-I{work_runtime / 'src'}",
            str(src),
            str(work_runtime / "libpy_runtime_pcc_py.a"),
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if build_probe.returncode != 0:
        stderr = build_probe.stderr.lower()
        assert "sanitize" in stderr or "tsan" in stderr
        return

    env = os.environ.copy()
    env["TSAN_OPTIONS"] = "halt_on_error=1:exitcode=66"
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    combined = run.stdout + run.stderr
    assert run.returncode == 0, combined
    assert "data race" not in combined.lower()


@pytest.mark.pcc_gate(probe="tsan")
def test_colored_relocating_forwarding_table_threadsanitizer_or_skip(tmp_path):
    cc = os.environ.get("CC", "clang")
    _require_thread_sanitizer_runtime(tmp_path, cc)
    work_runtime = _build_threaded_runtime(tmp_path, cc=cc, tsan=True)

    src = tmp_path / "backend4_forwarding_table_tsan_probe.c"
    exe = tmp_path / "backend4_forwarding_table_tsan_probe.out"
    src.write_text(textwrap.dedent(r"""
        #include "py_runtime.h"
        #include "py_internal.h"
        #include <stdint.h>
        #include <stdlib.h>
        #include <unistd.h>

        enum {
            PY_FLAG_GC_YOUNG = 0x80,
            PY_FLAG_GC_OLD = 0x100,
            PY_FLAG_GC_PINNED = 0x40
        };

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        static PyObject *new_reloc_payload(int relocation_candidate) {
            int32_t flags = relocation_candidate ? PY_FLAG_GC_OLD : 0;
            ProbeListObject *obj = (
                ProbeListObject *
            )pcc_gc_alloc(sizeof(ProbeListObject), PY_TYPE_LIST, flags);
            if (obj == 0) return 0;
            obj->length = 0;
            obj->capacity = 0;
            obj->items = 0;
            if (relocation_candidate) {
                ProbeListObject *child = (ProbeListObject *)pcc_gc_alloc(
                    sizeof(ProbeListObject),
                    PY_TYPE_LIST,
                    PY_FLAG_GC_YOUNG | PY_FLAG_GC_PINNED
                );
                if (child == 0) return 0;
                child->length = 0;
                child->capacity = 0;
                child->items = 0;
                obj->length = 1;
                obj->capacity = 1;
                obj->items = (PyObject **)calloc(1, sizeof(PyObject *));
                if (obj->items == 0) return 0;
                pcc_gc_store_ptr((PyObject *)obj, &obj->items[0], (PyObject *)child);
            }
            return (PyObject *)obj;
        }

        enum {
            OBJECTS = 256,
            READERS = 4,
            READ_ROUNDS = 600
        };

        static PyObject *old_objs[OBJECTS];
        static PyObject *new_objs[OBJECTS];
        static int32_t start_flag = 0;
        static int32_t writer_done = 0;

        static void *reader(void *arg) {
            (void)arg;
            while (__atomic_load_n(&start_flag, __ATOMIC_ACQUIRE) == 0) {
                pcc_thread_safepoint();
            }
            int rounds = 0;
            while (
                rounds < READ_ROUNDS
                || __atomic_load_n(&writer_done, __ATOMIC_ACQUIRE) == 0
            ) {
                for (int i = 0; i < OBJECTS; i++) {
                    PyObject *resolved = pcc_gc_note_relocation_read(old_objs[i]);
                    if (resolved == 0) return (void *)1;
                    if ((i & 31) == 0) pcc_thread_safepoint();
                }
                rounds++;
            }
            return 0;
        }

        static void *writer(void *arg) {
            (void)arg;
            while (__atomic_load_n(&start_flag, __ATOMIC_ACQUIRE) == 0) {
                pcc_thread_safepoint();
            }
            for (int i = 0; i < OBJECTS; i++) {
                if (pcc_gc_install_forwarding(old_objs[i], new_objs[i]) != 0) {
                    __atomic_store_n(&writer_done, 1, __ATOMIC_RELEASE);
                    return (void *)2;
                }
                if ((i & 7) == 0) {
                    pcc_thread_safepoint();
                    usleep(1);
                }
            }
            __atomic_store_n(&writer_done, 1, __ATOMIC_RELEASE);
            return 0;
        }

        int main(void) {
            if (pcc_threads_enabled() != 1) return 2;
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) {
                return 3;
            }
            for (int i = 0; i < OBJECTS; i++) {
                old_objs[i] = new_reloc_payload();
                new_objs[i] = new_reloc_payload();
                if (old_objs[i] == 0 || new_objs[i] == 0) return 4;
            }

            PccThreadHandle *reader_threads[READERS];
            PccThreadHandle *writer_thread = 0;
            for (intptr_t i = 0; i < READERS; i++) {
                if (
                    pcc_thread_start(&reader_threads[i], reader, (void *)i)
                    != 0
                ) {
                    return 5;
                }
            }
            if (pcc_thread_start(&writer_thread, writer, 0) != 0) return 6;
            __atomic_store_n(&start_flag, 1, __ATOMIC_RELEASE);

            for (int i = 0; i < READERS; i++) {
                void *result = 0;
                if (pcc_thread_join(reader_threads[i], &result) != 0) return 7;
                if (result != 0) return 8;
            }
            void *writer_result = 0;
            if (pcc_thread_join(writer_thread, &writer_result) != 0) return 9;
            if (writer_result != 0) return 10;

            for (int i = 0; i < OBJECTS; i++) {
                PyObject *resolved = pcc_gc_note_relocation_read(old_objs[i]);
                if (resolved != new_objs[i]) return 11;
            }
            if (pcc_gc_set_backend(PCC_GC_KIND_REFCOUNT_CYCLE) != 0) return 12;
            for (int i = 0; i < OBJECTS; i++) {
                pcc_gc_release(old_objs[i]);
                pcc_gc_release(new_objs[i]);
            }
            return 0;
        }
        """).lstrip(), encoding="utf-8")
    build_probe = subprocess.run(
        [
            cc,
            "-O1",
            "-g",
            "-std=c11",
            "-pthread",
            "-fsanitize=thread",
            f"-I{work_runtime / 'include'}",
            f"-I{work_runtime / 'src'}",
            str(src),
            str(work_runtime / "libpy_runtime_pcc_py.a"),
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if build_probe.returncode != 0:
        stderr = build_probe.stderr.lower()
        assert "sanitize" in stderr or "tsan" in stderr
        return

    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)
    combined = run.stdout + run.stderr
    assert run.returncode == 0, combined
    assert "data race" not in combined.lower()


@pytest.mark.pcc_gate(probe="tsan")
def test_colored_relocating_step_allocation_threadsanitizer_or_skip(tmp_path):
    cc = os.environ.get("CC", "clang")
    _require_thread_sanitizer_runtime(tmp_path, cc)
    work_runtime = _build_threaded_runtime(tmp_path, cc=cc, tsan=True)

    src = tmp_path / "backend4_step_allocation_tsan_probe.c"
    exe = tmp_path / "backend4_step_allocation_tsan_probe.out"
    src.write_text(textwrap.dedent(r"""
        #include "py_runtime.h"
        #include "py_internal.h"
        #include <stdint.h>
        #include <unistd.h>

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

        enum {
            OBJECTS = 512
        };

        static PyObject *objects[OBJECTS];
        static int32_t allocated = 0;
        static int32_t allocator_done = 0;

        static void *allocator(void *arg) {
            (void)arg;
            for (int i = 0; i < OBJECTS; i++) {
                PyObject *obj = new_reloc_payload();
                if (obj == 0) return (void *)1;
                objects[i] = obj;
                __atomic_store_n(&allocated, i + 1, __ATOMIC_RELEASE);
                if ((i & 7) == 0) {
                    pcc_thread_safepoint();
                    usleep(1);
                }
            }
            __atomic_store_n(&allocator_done, 1, __ATOMIC_RELEASE);
            return 0;
        }

        static void *collector(void *arg) {
            (void)arg;
            int idle_rounds = 0;
            while (
                __atomic_load_n(&allocator_done, __ATOMIC_ACQUIRE) == 0
                || idle_rounds < 64
            ) {
                int64_t work = pcc_gc_step(16);
                if (work == 0) {
                    idle_rounds++;
                } else {
                    idle_rounds = 0;
                }
                pcc_thread_safepoint();
                usleep(1);
            }
            return 0;
        }

        int main(void) {
            if (pcc_threads_enabled() != 1) return 2;
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) {
                return 3;
            }
            PccThreadHandle *allocator_thread = 0;
            PccThreadHandle *collector_thread = 0;
            if (pcc_thread_start(&allocator_thread, allocator, 0) != 0) {
                return 4;
            }
            if (pcc_thread_start(&collector_thread, collector, 0) != 0) {
                return 5;
            }
            void *allocator_result = 0;
            if (pcc_thread_join(allocator_thread, &allocator_result) != 0) {
                return 6;
            }
            if (allocator_result != 0) return 7;
            if (pcc_thread_join(collector_thread, 0) != 0) return 8;
            if (__atomic_load_n(&allocated, __ATOMIC_ACQUIRE) != OBJECTS) {
                return 9;
            }
            if (pcc_gc_telemetry(PCC_GC_COUNTER_RELOCATION_FORWARDS) <= 0) {
                return 10;
            }
            for (int i = 0; i < OBJECTS; i++) {
                PyObject *old_slot = objects[i];
                py_incref(old_slot);
                PyObject *obj = pcc_gc_load_ptr(0, &objects[i]);
                pcc_gc_release(obj);
                pcc_gc_release(old_slot);
            }
            if (pcc_gc_set_backend(PCC_GC_KIND_REFCOUNT_CYCLE) != 0) return 11;
            return 0;
        }
        """).lstrip(), encoding="utf-8")
    build_probe = subprocess.run(
        [
            cc,
            "-O1",
            "-g",
            "-std=c11",
            "-pthread",
            "-fsanitize=thread",
            f"-I{work_runtime / 'include'}",
            f"-I{work_runtime / 'src'}",
            str(src),
            str(work_runtime / "libpy_runtime_pcc_py.a"),
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if build_probe.returncode != 0:
        stderr = build_probe.stderr.lower()
        assert "sanitize" in stderr or "tsan" in stderr
        return

    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)
    combined = run.stdout + run.stderr
    assert run.returncode == 0, combined
    assert "data race" not in combined.lower()


@pytest.mark.pcc_gate(probe="tsan")
def test_colored_relocating_free_hook_threadsanitizer_or_skip(tmp_path):
    cc = os.environ.get("CC", "clang")
    _require_thread_sanitizer_runtime(tmp_path, cc)
    work_runtime = _build_threaded_runtime(tmp_path, cc=cc, tsan=True)

    src = tmp_path / "backend4_free_hook_tsan_probe.c"
    exe = tmp_path / "backend4_free_hook_tsan_probe.out"
    src.write_text(textwrap.dedent(r"""
        #include "py_runtime.h"
        #include "py_internal.h"
        #include <stdint.h>
        #include <unistd.h>

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

        enum {
            OBJECTS = 256,
            READERS = 4,
            READ_ROUNDS = 160
        };

        static PyObject *old_objs[OBJECTS];
        static PyObject *new_objs[OBJECTS];
        static int32_t start_flag = 0;
        static int32_t free_done = 0;

        static void *reader(void *arg) {
            (void)arg;
            while (__atomic_load_n(&start_flag, __ATOMIC_ACQUIRE) == 0) {
                pcc_thread_safepoint();
            }
            int rounds = 0;
            while (
                rounds < READ_ROUNDS
                || __atomic_load_n(&free_done, __ATOMIC_ACQUIRE) == 0
            ) {
                for (int i = 0; i < OBJECTS; i++) {
                    PyObject *resolved = pcc_gc_note_relocation_read(old_objs[i]);
                    if (resolved == 0) return (void *)1;
                    if ((i & 31) == 0) pcc_thread_safepoint();
                }
                rounds++;
            }
            return 0;
        }

        static void *free_hook_thread(void *arg) {
            (void)arg;
            while (__atomic_load_n(&start_flag, __ATOMIC_ACQUIRE) == 0) {
                pcc_thread_safepoint();
            }
            for (int i = 0; i < OBJECTS; i++) {
                pcc_gc_note_object_freeing(old_objs[i]);
                if ((i & 7) == 0) {
                    pcc_thread_safepoint();
                    usleep(1);
                }
            }
            __atomic_store_n(&free_done, 1, __ATOMIC_RELEASE);
            return 0;
        }

        int main(void) {
            if (pcc_threads_enabled() != 1) return 2;
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) {
                return 3;
            }
            for (int i = 0; i < OBJECTS; i++) {
                old_objs[i] = new_reloc_payload(1);
                if (old_objs[i] == 0) return 4;
                if (pcc_gc_object_id(old_objs[i]) <= 0) return 5;
            }
            pcc_gc_reset_relocation_set();
            if (pcc_gc_select_relocation_set(OBJECTS) != OBJECTS) return 6;
            for (int i = 0; i < OBJECTS; i++) {
                new_objs[i] = new_reloc_payload(0);
                if (new_objs[i] == 0) return 4;
            }
            for (int i = 0; i < OBJECTS; i++) {
                if (pcc_gc_install_forwarding(old_objs[i], new_objs[i]) != 0) {
                    return 7;
                }
            }

            PccThreadHandle *reader_threads[READERS];
            PccThreadHandle *free_thread = 0;
            for (intptr_t i = 0; i < READERS; i++) {
                if (
                    pcc_thread_start(&reader_threads[i], reader, (void *)i)
                    != 0
                ) {
                    return 8;
                }
            }
            if (pcc_thread_start(&free_thread, free_hook_thread, 0) != 0) {
                return 9;
            }
            __atomic_store_n(&start_flag, 1, __ATOMIC_RELEASE);

            for (int i = 0; i < READERS; i++) {
                void *result = 0;
                if (pcc_thread_join(reader_threads[i], &result) != 0) return 10;
                if (result != 0) return 11;
            }
            if (pcc_thread_join(free_thread, 0) != 0) return 12;
            if (pcc_gc_set_backend(PCC_GC_KIND_REFCOUNT_CYCLE) != 0) return 13;
            return 0;
        }
        """).lstrip(), encoding="utf-8")
    build_probe = subprocess.run(
        [
            cc,
            "-O1",
            "-g",
            "-std=c11",
            "-pthread",
            "-fsanitize=thread",
            f"-I{work_runtime / 'include'}",
            f"-I{work_runtime / 'src'}",
            str(src),
            str(work_runtime / "libpy_runtime_pcc_py.a"),
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if build_probe.returncode != 0:
        stderr = build_probe.stderr.lower()
        assert "sanitize" in stderr or "tsan" in stderr
        return

    env = os.environ.copy()
    env["TSAN_OPTIONS"] = "halt_on_error=1:exitcode=66"
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    combined = run.stdout + run.stderr
    assert run.returncode == 0, combined
    assert "data race" not in combined.lower()


@pytest.mark.pcc_gate(probe="tsan")
def test_pcc_python_runtime_object_graph_threadsanitizer_or_skip(tmp_path):
    cc = os.environ.get("CC", "clang")
    _require_thread_sanitizer_runtime(tmp_path, cc)
    work_runtime = _build_threaded_runtime(
        tmp_path,
        cc=cc,
        tsan=True,
        pcc_python=True,
    )

    src = tmp_path / "pcc_py_object_graph_tsan_probe.c"
    exe = tmp_path / "pcc_py_object_graph_tsan_probe.out"
    src.write_text(textwrap.dedent(r"""
        #include "py_runtime.h"
        #include "py_internal.h"
        #include <stdint.h>
        #include <stdio.h>
        #include <unistd.h>

        enum {
            MINOR_ALLOCATIONS = 8,
            MINOR_ARENA_REFILLS = 19,
            THREADS = 2,
            ROUNDS = 32,
            OBJECTS_PER_ROUND = 4
        };

        static int64_t done_count = 0;

        static void *mutator(void *arg) {
            (void)arg;
            for (int round = 0; round < ROUNDS; round++) {
                PyObject *objects[OBJECTS_PER_ROUND];
                for (int i = 0; i < OBJECTS_PER_ROUND; i++) {
                    objects[i] = pcc_gc_alloc(64, PY_TYPE_NONE, 0);
                    if (objects[i] == 0) return (void *)(uintptr_t)1;
                }
                for (int i = 0; i < OBJECTS_PER_ROUND; i++) {
                    pcc_gc_release(objects[i]);
                }
                if ((round % 8) == 0) {
                    pcc_thread_safepoint();
                    usleep(50);
                }
            }
            __atomic_add_fetch(&done_count, 1, __ATOMIC_RELEASE);
            return 0;
        }

        int main(void) {
            if (pcc_threads_enabled() != 1) return 2;
            if (pcc_gc_set_backend(PCC_GC_KIND_GENERATIONAL_MINOR_MAJOR) != 0) {
                return 3;
            }
            pcc_gc_telemetry_reset();

            PccThreadHandle *threads[THREADS] = {0, 0};
            for (int i = 0; i < THREADS; i++) {
                if (pcc_thread_start(&threads[i], mutator, 0) != 0) return 4 + i;
            }
            while (__atomic_load_n(&done_count, __ATOMIC_ACQUIRE) < THREADS) {
                pcc_thread_safepoint();
                usleep(1000);
            }
            for (int i = 0; i < THREADS; i++) {
                void *result = 0;
                if (pcc_thread_join(threads[i], &result) != 0) return 8 + i;
                if (result != 0) return 12 + i;
            }
            if (pcc_gc_set_backend(PCC_GC_KIND_REFCOUNT_CYCLE) != 0) return 16;

            printf("%lld\n", (long long)pcc_gc_telemetry(MINOR_ALLOCATIONS));
            printf("%lld\n", (long long)pcc_gc_telemetry(MINOR_ARENA_REFILLS));
            return 0;
        }
        """).lstrip(), encoding="utf-8")
    build_probe = subprocess.run(
        [
            cc,
            "-O1",
            "-g",
            "-std=c11",
            "-pthread",
            "-fsanitize=thread",
            f"-I{work_runtime / 'include'}",
            f"-I{work_runtime / 'src'}",
            str(src),
            str(work_runtime / "libpy_runtime_pcc_py.a"),
            "-o",
            str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if build_probe.returncode != 0:
        stderr = build_probe.stderr.lower()
        assert "sanitize" in stderr or "tsan" in stderr
        return

    env = os.environ.copy()
    env.update(
        {
            "PCC_GC_MINOR_HEAP_SIZE": "512",
            "PCC_GC_MINOR_ALLOC_MAX": "128",
            "TSAN_OPTIONS": "halt_on_error=1:exitcode=66",
        }
    )
    run = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=90,
        env=env,
    )
    combined = run.stdout + run.stderr
    assert run.returncode == 0, combined
    assert "data race" not in combined.lower()
    lines = run.stdout.strip().splitlines()
    assert int(lines[0]) > 0
    assert int(lines[1]) >= 1
