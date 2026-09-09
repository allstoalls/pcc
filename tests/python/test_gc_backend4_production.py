from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

from pcc.py_runtime.py.py_abi_constants import (
    C_POINTER_SIZE,
    PYCLASSMETHOD_FUNC_OFFSET,
    PYCLASSMETHOD_SIZE,
    PYCLASSOBJECT_DEL_METHOD_OFFSET,
    PYCLASSOBJECT_METHODS_OFFSET,
)
from tests.runtime_build_cache import (
    cached_c_runtime,
    cached_threaded_c_runtime,
    cached_threaded_pcc_python_runtime,
)

REPO_ROOT = Path(__file__).absolute().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "py_runtime"
STRICT_OBJECT_SLOTS = RUNTIME_DIR / "py" / "freestanding_gc_object_slots.py"
STRICT_COMMON_MARK_CYCLE = (
    RUNTIME_DIR / "py" / "freestanding_gc_common_mark_cycle.py"
)
STRICT_GENERATIONAL_PROMOTION = (
    RUNTIME_DIR / "py" / "freestanding_gc_generational_promotion.py"
)
STRICT_RELOCATION_REMAP = (
    RUNTIME_DIR / "py" / "freestanding_gc_relocation_remap.py"
)
STRICT_RELOCATION_PAYLOAD = (
    RUNTIME_DIR / "py" / "freestanding_gc_relocation_payload.py"
)
STRICT_RELOCATION_SELECTOR = (
    RUNTIME_DIR / "py" / "freestanding_gc_relocation_selector.py"
)
STRICT_RELOCATION_COPY = (
    RUNTIME_DIR / "py" / "freestanding_gc_relocation_copy.py"
)
STRICT_RELOCATION_DRAIN = (
    RUNTIME_DIR / "py" / "freestanding_gc_relocation_drain.py"
)
STRICT_ZPAGE_ALLOCATION = (
    RUNTIME_DIR / "py" / "freestanding_gc_zpage_allocation.py"
)
STRICT_ZPAGE_MECHANICS = (
    RUNTIME_DIR / "py" / "freestanding_gc_zpage_mechanics.py"
)
STRICT_ZPAGE_LIFECYCLE = (
    RUNTIME_DIR / "py" / "freestanding_gc_zpage_lifecycle.py"
)
STRICT_REFCOUNT_ROOTS = (
    RUNTIME_DIR / "py" / "freestanding_gc_refcount_roots.py"
)
STRICT_TRACING_SWEEP_COLLECTOR = (
    RUNTIME_DIR / "py" / "freestanding_gc_tracing_sweep_collector.py"
)
STRICT_BARRIER_DISPATCHER = (
    RUNTIME_DIR / "py" / "freestanding_gc_barrier_dispatcher.py"
)

# Every probe in this module links the same content-addressed immutable runtime
# archive. Probe sources/executables remain test-local; archive builds are
# shared across modules, workers, and repeated runs.


def _cc() -> str:
    return os.environ.get("CC", "cc")


def _build_runtime(tmp_path: Path) -> Path:
    del tmp_path
    return cached_c_runtime()


def _compile_and_run(tmp_path: Path, source: str) -> subprocess.CompletedProcess[str]:
    work_runtime = _build_runtime(tmp_path)
    src = tmp_path / "backend4_prod_probe.c"
    exe = tmp_path / "backend4_prod_probe.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    build = subprocess.run(
        [
            _cc(),
            "-std=c11",
            f"-I{work_runtime / 'include'}",
            f"-I{work_runtime / 'src'}",
            str(src),
            str(work_runtime / "libpy_runtime.a"),
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


def _compile_and_run_archive(
    tmp_path: Path,
    source: str,
    archive: Path,
    stem: str,
) -> subprocess.CompletedProcess[str]:
    src = tmp_path / f"{stem}.c"
    exe = tmp_path / f"{stem}.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    build = subprocess.run(
        [
            _cc(),
            "-std=c11",
            f"-I{RUNTIME_DIR / 'include'}",
            f"-I{RUNTIME_DIR / 'src'}",
            str(src),
            str(archive),
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


def _compile_and_run_threaded_archive(
    tmp_path: Path,
    source: str,
    archive: Path,
    stem: str,
) -> subprocess.CompletedProcess[str]:
    src = tmp_path / f"{stem}.c"
    exe = tmp_path / f"{stem}.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    build = subprocess.run(
        [
            _cc(),
            "-std=c11",
            "-pthread",
            f"-I{RUNTIME_DIR / 'include'}",
            f"-I{RUNTIME_DIR / 'src'}",
            str(src),
            str(archive),
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


def _reseed_authoritative_evacuation_pages_source() -> str:
    return r'''
        #include "py_runtime.h"
        #include <stdio.h>

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) {
                return 2;
            }
            PyObject *small_root = 0;
            PyObject *medium_root = 0;
            pcc_gc_scheduler_root_register(&small_root);
            pcc_gc_scheduler_root_register(&medium_root);
            PyObject *small = pcc_gc_alloc(128, PY_TYPE_LIST, 0);
            PyObject *medium = pcc_gc_alloc(8192, PY_TYPE_LIST, 0);
            if (small == 0 || medium == 0) return 3;
            pcc_gc_store_root(&small_root, small);
            pcc_gc_store_root(&medium_root, medium);
            pcc_gc_telemetry_reset();
            pcc_gc_publish_initialized((PyObject *)small);
            pcc_gc_publish_initialized((PyObject *)medium);
            if (pcc_gc_select_relocation_set(32) != 2) return 4;

            for (int round = 0; round < 2; round++) {
                pcc_gc_telemetry_reset();
                if (pcc_gc_relocation_set_size() != 2) return 5;
                if (pcc_gc_backend4_evacuation_candidate_score() != 2) return 6;
                if (pcc_gc_backend4_evacuation_candidate_bytes() != 8320) return 7;
                if (pcc_gc_backend4_small_page_candidate_score() != 1) return 8;
                if (pcc_gc_backend4_medium_page_candidate_score() != 1) return 9;
            }

            pcc_gc_reset_relocation_set();
            pcc_gc_store_root(&small_root, 0);
            pcc_gc_store_root(&medium_root, 0);
            pcc_gc_scheduler_root_unregister(&small_root);
            pcc_gc_scheduler_root_unregister(&medium_root);
            printf("backend4-reseed-authoritative-pages-ok\n");
            return 0;
        }
    '''


def _concurrent_reset_reseed_plan_source() -> str:
    return r'''
        #include "py_runtime.h"
        #include <pthread.h>
        #include <sched.h>
        #include <stdatomic.h>
        #include <stdint.h>
        #include <stdio.h>

        enum { OBJECTS = 24, THREADS = 4, ROUNDS = 64 };
        static PyObject *roots[OBJECTS];
        static _Atomic int ready = 0;
        static _Atomic int go = 0;
        static _Atomic int errors = 0;

        static void *worker(void *opaque) {
            intptr_t role = (intptr_t)opaque;
            if (pcc_current_thread_id() <= 0) {
                atomic_fetch_add_explicit(&errors, 1, memory_order_relaxed);
                return 0;
            }
            atomic_fetch_add_explicit(&ready, 1, memory_order_release);
            while (atomic_load_explicit(&go, memory_order_acquire) == 0) {
                sched_yield();
            }
            for (int round = 0; round < ROUNDS; round++) {
                if ((role & 1) == 0) {
                    pcc_gc_telemetry_reset();
                } else {
                    pcc_gc_reset_relocation_set();
                    int64_t selected = pcc_gc_select_relocation_set(OBJECTS);
                    if (selected < 0 || selected > OBJECTS) {
                        atomic_fetch_add_explicit(
                            &errors, 1, memory_order_relaxed
                        );
                    }
                }
            }
            pcc_thread_unregister_current();
            return 0;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) {
                return 2;
            }
            for (int i = 0; i < OBJECTS; i++) {
                pcc_gc_scheduler_root_register(&roots[i]);
                PyObject *obj = pcc_gc_alloc(128, PY_TYPE_LIST, 0);
                if (obj == 0) return 3;
                pcc_gc_store_root(&roots[i], obj);
                pcc_gc_publish_initialized(obj);
            }
            if (pcc_gc_select_relocation_set(OBJECTS) != OBJECTS) return 4;

            pthread_t threads[THREADS];
            for (intptr_t i = 0; i < THREADS; i++) {
                if (pthread_create(&threads[i], 0, worker, (void *)i) != 0) {
                    return 5;
                }
            }
            while (atomic_load_explicit(&ready, memory_order_acquire) != THREADS) {
                sched_yield();
            }
            atomic_store_explicit(&go, 1, memory_order_release);
            for (int i = 0; i < THREADS; i++) {
                if (pthread_join(threads[i], 0) != 0) return 6;
            }

            pcc_gc_reset_relocation_set();
            int64_t selected = pcc_gc_select_relocation_set(OBJECTS);
            pcc_gc_telemetry_reset();
            int64_t set_size = pcc_gc_relocation_set_size();
            int64_t candidate_bytes =
                pcc_gc_backend4_evacuation_candidate_bytes();
            int64_t pages =
                pcc_gc_backend4_evacuation_page_candidate_score();
            int observed_errors = atomic_load_explicit(
                &errors, memory_order_relaxed
            );
            printf("%lld,%lld,%lld,%lld,%d\n",
                   (long long)selected,
                   (long long)set_size,
                   (long long)candidate_bytes,
                   (long long)pages,
                   observed_errors);

            pcc_gc_reset_relocation_set();
            for (int i = 0; i < OBJECTS; i++) {
                pcc_gc_store_root(&roots[i], 0);
                pcc_gc_scheduler_root_unregister(&roots[i]);
            }
            return selected == OBJECTS
                && set_size == OBJECTS
                && candidate_bytes == OBJECTS * 128
                && pages == 1
                && observed_errors == 0 ? 0 : 7;
        }
    '''


def _forced_reseed_plan_paths_source() -> str:
    return r'''
        #include "py_runtime.h"
        #include <pthread.h>
        #include <sched.h>
        #include <stdatomic.h>
        #include <stdint.h>
        #include <stdio.h>

        static PyObject *roots[2];
        static _Atomic int worker_errors = 0;

        static void *reseed_worker(void *opaque) {
            (void)opaque;
            if (pcc_current_thread_id() <= 0) {
                atomic_store_explicit(&worker_errors, 1, memory_order_release);
                return 0;
            }
            pcc_gc_telemetry_reset();
            pcc_thread_unregister_current();
            return 0;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) {
                return 2;
            }
            for (int i = 0; i < 2; i++) {
                pcc_gc_scheduler_root_register(&roots[i]);
            }
            roots[0] = pcc_gc_alloc(128, PY_TYPE_LIST, 0);
            roots[1] = pcc_gc_alloc(8192, PY_TYPE_LIST, 0);
            if (roots[0] == 0 || roots[1] == 0) return 3;
            pcc_gc_publish_initialized(roots[0]);
            pcc_gc_publish_initialized(roots[1]);
            if (pcc_gc_select_relocation_set(1) != 1) return 4;

            pcc_gc_backend4_reseed_plan_probe_config(1, -1);
            pthread_t worker;
            if (pthread_create(&worker, 0, reseed_worker, 0) != 0) return 5;
            int spins = 0;
            while (pcc_gc_backend4_reseed_plan_probe_state() == 0) {
                if (++spins > 10000000) return 6;
                sched_yield();
            }
            if (pcc_gc_select_relocation_set(1) != 1) return 7;
            pcc_gc_backend4_reseed_plan_probe_config(0, -1);
            if (pthread_join(worker, 0) != 0) return 8;
            if (atomic_load_explicit(&worker_errors, memory_order_acquire)) {
                return 9;
            }
            int64_t growth_set = pcc_gc_relocation_set_size();
            int64_t growth_pages =
                pcc_gc_backend4_evacuation_page_candidate_score();
            int64_t growth_bytes =
                pcc_gc_backend4_evacuation_page_candidate_bytes();

            pcc_gc_backend4_reseed_plan_probe_config(0, 0);
            pcc_gc_telemetry_reset();
            int64_t failure_set = pcc_gc_relocation_set_size();
            int64_t failure_pages =
                pcc_gc_backend4_evacuation_page_candidate_score();

            pcc_gc_backend4_reseed_plan_probe_config(0, -1);
            pcc_gc_telemetry_reset();
            int64_t recovered_pages =
                pcc_gc_backend4_evacuation_page_candidate_score();
            int64_t recovered_bytes =
                pcc_gc_backend4_evacuation_page_candidate_bytes();
            printf("%lld,%lld,%lld,%lld,%lld,%lld,%lld,%d\n",
                   (long long)growth_set,
                   (long long)growth_pages,
                   (long long)growth_bytes,
                   (long long)failure_set,
                   (long long)failure_pages,
                   (long long)recovered_pages,
                   (long long)recovered_bytes,
                   atomic_load_explicit(&worker_errors, memory_order_acquire));

            pcc_gc_reset_relocation_set();
            for (int i = 0; i < 2; i++) {
                pcc_gc_store_root(&roots[i], 0);
                pcc_gc_scheduler_root_unregister(&roots[i]);
            }
            return growth_set == 2
                && growth_pages == 2
                && growth_bytes == 8320
                && failure_set == 2
                && failure_pages == 2
                && recovered_pages == 2
                && recovered_bytes == 8320 ? 0 : 10;
        }
    '''


def _many_page_reseed_source() -> str:
    return r'''
        #include "py_runtime.h"
        #include <stdio.h>

        enum { OBJECTS = 24, OBJECT_SIZE = 60000 };
        static PyObject *roots[OBJECTS];

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) {
                return 2;
            }
            for (int i = 0; i < OBJECTS; i++) {
                pcc_gc_scheduler_root_register(&roots[i]);
                roots[i] = pcc_gc_alloc(OBJECT_SIZE, PY_TYPE_LIST, 0);
                if (roots[i] == 0) return 3;
                pcc_gc_publish_initialized(roots[i]);
            }
            if (pcc_gc_select_relocation_set(OBJECTS) != OBJECTS) return 4;
            pcc_gc_telemetry_reset();
            int64_t set_size = pcc_gc_relocation_set_size();
            int64_t pages =
                pcc_gc_backend4_evacuation_page_candidate_score();
            int64_t bytes = pcc_gc_backend4_evacuation_candidate_bytes();
            printf("%lld,%lld,%lld\n",
                   (long long)set_size,
                   (long long)pages,
                   (long long)bytes);
            pcc_gc_reset_relocation_set();
            for (int i = 0; i < OBJECTS; i++) {
                pcc_gc_store_root(&roots[i], 0);
                pcc_gc_scheduler_root_unregister(&roots[i]);
            }
            return set_size == OBJECTS
                && pages == OBJECTS
                && bytes == OBJECTS * OBJECT_SIZE ? 0 : 5;
        }
    '''


def _forced_reseed_count_unlink_source() -> str:
    return r'''
        #include "py_runtime.h"
        #include <pthread.h>
        #include <sched.h>
        #include <stdatomic.h>
        #include <stdio.h>

        enum { OBJECTS = 24, OBJECT_SIZE = 60000 };
        static PyObject *roots[OBJECTS];
        static _Atomic int worker_error = 0;

        static void *reseed_worker(void *opaque) {
            (void)opaque;
            if (pcc_current_thread_id() <= 0) {
                atomic_store_explicit(&worker_error, 1, memory_order_release);
                return 0;
            }
            pcc_gc_telemetry_reset();
            pcc_thread_unregister_current();
            return 0;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) {
                return 2;
            }
            for (int i = 0; i < OBJECTS; i++) {
                pcc_gc_scheduler_root_register(&roots[i]);
                roots[i] = pcc_gc_alloc(OBJECT_SIZE, PY_TYPE_LIST, 0);
                if (roots[i] == 0) return 3;
                pcc_gc_publish_initialized(roots[i]);
            }
            if (pcc_gc_select_relocation_set(OBJECTS) != OBJECTS) return 4;
            pcc_gc_backend4_reseed_plan_probe_config(1, -1);
            pthread_t worker;
            if (pthread_create(&worker, 0, reseed_worker, 0) != 0) return 5;
            int spins = 0;
            while (pcc_gc_backend4_reseed_plan_probe_state() == 0) {
                if (++spins > 10000000) return 6;
                sched_yield();
            }
            pcc_gc_reset_relocation_set();
            pcc_gc_backend4_reseed_plan_probe_config(0, -1);
            if (pthread_join(worker, 0) != 0) return 7;
            int64_t empty_set = pcc_gc_relocation_set_size();
            int64_t empty_pages =
                pcc_gc_backend4_evacuation_page_candidate_score();

            int64_t selected = pcc_gc_select_relocation_set(OBJECTS);
            pcc_gc_telemetry_reset();
            int64_t recovered_pages =
                pcc_gc_backend4_evacuation_page_candidate_score();
            int64_t recovered_bytes =
                pcc_gc_backend4_evacuation_candidate_bytes();
            int error = atomic_load_explicit(
                &worker_error, memory_order_acquire
            );
            printf("%lld,%lld,%lld,%lld,%lld,%d\n",
                   (long long)empty_set,
                   (long long)empty_pages,
                   (long long)selected,
                   (long long)recovered_pages,
                   (long long)recovered_bytes,
                   error);

            pcc_gc_reset_relocation_set();
            for (int i = 0; i < OBJECTS; i++) {
                pcc_gc_store_root(&roots[i], 0);
                pcc_gc_scheduler_root_unregister(&roots[i]);
            }
            return empty_set == 0
                && empty_pages == 0
                && selected == OBJECTS
                && recovered_pages == OBJECTS
                && recovered_bytes == OBJECTS * OBJECT_SIZE
                && error == 0 ? 0 : 8;
        }
    '''


def _forced_reseed_aggregate_unlink_source() -> str:
    return _forced_reseed_count_unlink_source().replace(
        "pcc_gc_backend4_reseed_plan_probe_config(1, -1)",
        "pcc_gc_backend4_reseed_plan_probe_config(2, -1)",
        1,
    )


def _forced_reseed_page_commit_unlink_source() -> str:
    return _forced_reseed_count_unlink_source().replace(
        "pcc_gc_backend4_reseed_plan_probe_config(1, -1)",
        "pcc_gc_backend4_reseed_plan_probe_config(4, -1)",
        1,
    )


def _deallocating_relocation_quarantine_source() -> str:
    return r'''
        #include "py_internal.h"
        #include <stdint.h>

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        static ProbeListObject *new_list(void) {
            ProbeListObject *obj = (ProbeListObject *)pcc_gc_alloc(
                64, PY_TYPE_LIST, 0
            );
            if (obj == 0) return 0;
            obj->length = 0;
            obj->capacity = 0;
            obj->items = 0;
            pcc_gc_publish_initialized((PyObject *)obj);
            return obj;
        }

        int main(void) {
            if (pcc_gc_set_backend(
                    PCC_GC_KIND_COLORED_RELOCATING
                ) != 0) return 2;

            /* pcc_gc_alloc hands back a half-initialized object carrying
             * PY_FLAG_GC_FRESH_ALLOC, and the selector refuses a fresh owner
             * outright, so new_list publishes the way a real constructor
             * does.  Without that, BOTH halves of this probe passed for the
             * wrong reason: nothing was ever selectable, so the DEALLOCATING
             * quarantine below was never the flag that decided anything.  Selection is also per page and budget 1 takes one
             * object from the best-scoring page, which need not be this
             * object's page -- so ask for room and assert containment. */
            ProbeListObject *before_select = new_list();
            if (before_select == 0) return 3;
            py_header_flags_or(
                &before_select->h, PY_FLAG_GC_DEALLOCATING
            );
            pcc_gc_reset_relocation_set();
            (void)pcc_gc_select_relocation_set(1000);
            if (
                pcc_gc_relocation_set_contains((PyObject *)before_select) != 0
            ) return 4;
            py_header_flags_and(
                &before_select->h, ~PY_FLAG_GC_DEALLOCATING
            );
            pcc_gc_release((PyObject *)before_select);

            ProbeListObject *after_select = new_list();
            if (after_select == 0) return 5;
            pcc_gc_reset_relocation_set();
            (void)pcc_gc_select_relocation_set(1000);
            if (
                pcc_gc_relocation_set_contains((PyObject *)after_select) != 1
            ) return 6;
            py_header_flags_or(
                &after_select->h, PY_FLAG_GC_DEALLOCATING
            );
            if (
                pcc_gc_relocate_copy((PyObject *)after_select, 64) != 0
            ) return 7;
            py_header_flags_and(
                &after_select->h, ~PY_FLAG_GC_DEALLOCATING
            );
            PyObject *copied = pcc_gc_relocate_copy(
                (PyObject *)after_select, 64
            );
            if (copied == 0) return 8;
            py_decref(copied);
            pcc_gc_release((PyObject *)after_select);
            return 0;
        }
    '''


def _relocation_slot_retain_balance_source() -> str:
    return r'''
        #include "py_internal.h"
        #include <stdint.h>
        #include <stdio.h>
        #include <stdlib.h>

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        static int64_t refcount_of(PyObject *obj) {
            return pcc_refcount_load(&((PyObjectHeader *)obj)->refcount);
        }

        int main(void) {
            if (
                pcc_refcount_strategy() != PCC_REFCOUNT_STRATEGY_ATOMIC
            ) return 1;
            if (
                pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0
            ) return 2;

            PyObject *child = py_list_new(0);
            ProbeListObject *owner = (ProbeListObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbeListObject), PY_TYPE_LIST, 0
            );
            if (child == 0 || owner == 0) return 3;
            owner->length = 1;
            owner->capacity = 1;
            owner->items = (PyObject **)calloc(1, sizeof(PyObject *));
            if (owner->items == 0) return 4;
            pcc_gc_store_ptr((PyObject *)owner, &owner->items[0], child);
            py_decref(child);
            if (refcount_of(child) != 1) return 5;

            pcc_gc_publish_initialized((PyObject *)owner);
            pcc_gc_reset_relocation_set();
            if (pcc_gc_select_relocation_set(1) != 1) return 6;
            PyObject *moved_raw = pcc_gc_relocate_copy(
                (PyObject *)owner, (int64_t)sizeof(ProbeListObject)
            );
            if (moved_raw == 0 || moved_raw == (PyObject *)owner) return 7;
            ProbeListObject *moved = (ProbeListObject *)moved_raw;
            if (
                moved->length != 1
                || moved->capacity != 1
                || moved->items == 0
                || moved->items[0] != child
                || owner->items[0] != child
            ) return 8;
            /* Source and target each own one slot; the split retain plan must
             * perform exactly one real increment before target publication. */
            if (refcount_of(child) != 2) {
                fprintf(
                    stderr, "slot-retain child refcount=%lld\n",
                    (long long)refcount_of(child)
                );
                return 9;
            }
            /* NEW owns its fresh allocation result, forwarding edge, and the
             * source's transferred outstanding count until caller release. */
            if (refcount_of(moved_raw) != 3) return 10;
            return 0;
        }
    '''


def _relocation_type_specific_raw_payload_source() -> str:
    return r'''
        #include "py_internal.h"
        #include <stdint.h>
        #include <stdlib.h>
        #include <string.h>

        static PyObject *probe_entry(PyObject *args) {
            (void)args;
            return py_None;
        }

        static int select_for_relocation(PyObject *obj) {
            pcc_gc_reset_relocation_set();
            if (pcc_gc_select_relocation_set(65536) <= 0) return 0;
            return pcc_gc_relocation_set_contains(obj) == 1;
        }

        static int check_exception(void) {
            PyExceptionObject *exc = (PyExceptionObject *)pcc_gc_alloc(
                (int64_t)sizeof(PyExceptionObject), PY_TYPE_EXC, 0
            );
            if (exc == 0) return 10;
            exc->exc_class = 0;
            exc->message = 0;
            exc->cause = 0;
            exc->context = 0;
            exc->traceback = (PyFrameRecord *)calloc(2, sizeof(PyFrameRecord));
            if (exc->traceback == 0) return 11;
            exc->n_frames = 2;
            exc->cap_frames = 2;
            exc->traceback[0].func_name = "f0";
            exc->traceback[0].filename = "p0.py";
            exc->traceback[0].source_line = "x0";
            exc->traceback[0].line = 17;
            exc->traceback[1].func_name = "f1";
            exc->traceback[1].filename = "p1.py";
            exc->traceback[1].source_line = "x1";
            exc->traceback[1].line = 29;
            PyFrameRecord *old_traceback = exc->traceback;
            pcc_gc_publish_initialized((PyObject *)exc);
            if (!select_for_relocation((PyObject *)exc)) return 12;
            if (pcc_gc_relocate_copy((PyObject *)exc, 16) != 0) return 15;
            if (pcc_gc_relocation_set_contains((PyObject *)exc) != 1) return 16;
            PyObject *moved_raw = pcc_gc_relocate_copy(
                (PyObject *)exc, (int64_t)sizeof(PyExceptionObject)
            );
            if (moved_raw == 0) return 13;
            PyExceptionObject *moved = (PyExceptionObject *)moved_raw;
            if (
                moved->traceback == 0
                || moved->traceback == old_traceback
                || moved->n_frames != 2
                || moved->cap_frames != 2
                || moved->traceback[0].line != 17
                || moved->traceback[1].line != 29
                || strcmp(moved->traceback[0].func_name, "f0") != 0
                || strcmp(moved->traceback[1].filename, "p1.py") != 0
            ) return 14;
            py_decref(moved_raw);
            return 0;
        }

        static int check_class(void) {
            PyClassObject *base = py_class_new("RawBase", 0, 0, 0, 0);
            if (base == 0) return 20;
            PyClassObject *bases[1] = {base};
            const char *fields[1] = {"payload_field"};
            PyClassObject *cls = py_class_new("RawClass", bases, 1, fields, 1);
            PyObject *func = py_func_new((void *)probe_entry, py_None);
            if (cls == 0 || func == 0) return 21;
            py_class_add_method(cls, "payload_method", func);
            if (
                cls->n_bases != 1
                || cls->n_mro < 1
                || cls->n_methods != 1
                || cls->n_fields != 1
            ) return 22;
            PyClassObject **old_bases = cls->bases;
            PyClassObject **old_mro = cls->mro;
            PyClassMethod *old_methods = cls->methods;
            const char **old_fields = cls->field_names;
            if (!select_for_relocation((PyObject *)cls)) return 23;
            if (pcc_gc_relocate_copy((PyObject *)cls, 16) != 0) return 26;
            if (pcc_gc_relocation_set_contains((PyObject *)cls) != 1) return 27;
            PyObject *moved_raw = pcc_gc_relocate_copy(
                (PyObject *)cls, (int64_t)sizeof(PyClassObject)
            );
            if (moved_raw == 0) return 24;
            PyClassObject *moved = (PyClassObject *)moved_raw;
            if (
                moved->bases == 0
                || moved->mro == 0
                || moved->methods == 0
                || moved->field_names == 0
                || moved->bases == old_bases
                || moved->mro == old_mro
                || moved->methods == old_methods
                || moved->field_names == old_fields
                || moved->n_bases != 1
                || moved->n_mro != cls->n_mro
                || moved->n_methods != 1
                || moved->n_fields != 1
                || moved->bases[0] != base
                || moved->methods[0].func != func
                || strcmp(moved->field_names[0], "payload_field") != 0
            ) return 25;
            py_decref(moved_raw);
            py_decref(func);
            py_decref((PyObject *)base);
            return 0;
        }

        static int check_class_span_order(void) {
            enum {
                N_BASES = 80,
                N_MRO = 80,
                N_METHODS = 40,
            };
            PyClassObject *cls = (PyClassObject *)pcc_gc_alloc(
                (int64_t)sizeof(PyClassObject), PY_TYPE_CLASS, 0
            );
            if (cls == 0) return 70;
            cls->name = "RawSpanOrder";
            cls->n_bases = N_BASES;
            cls->bases = (PyClassObject **)calloc(N_BASES, sizeof(PyClassObject *));
            cls->n_mro = N_MRO;
            cls->mro = (PyClassObject **)calloc(N_MRO, sizeof(PyClassObject *));
            cls->n_methods = N_METHODS;
            cls->methods = (PyClassMethod *)calloc(N_METHODS, sizeof(PyClassMethod));
            cls->n_fields = 0;
            cls->field_names = 0;
            cls->instance_size = 0;
            cls->type_tag_alloc = 0;
            cls->del_method = 0;
            cls->attrs = 0;
            cls->metaclass = 0;
            if (cls->bases == 0 || cls->mro == 0 || cls->methods == 0) return 71;
            pcc_gc_publish_initialized((PyObject *)cls);
            if (!select_for_relocation((PyObject *)cls)) return 72;
            PyObject *moved_raw = pcc_gc_relocate_copy(
                (PyObject *)cls, (int64_t)sizeof(PyClassObject)
            );
            if (moved_raw == 0) return 73;
            PyClassObject *moved = (PyClassObject *)moved_raw;
            int64_t bases_card = pcc_gc_backend4_zpage_owner_slot_span_card(
                moved_raw, &moved->bases[0]
            );
            int64_t mro_card = pcc_gc_backend4_zpage_owner_slot_span_card(
                moved_raw, &moved->mro[0]
            );
            int64_t methods_card = pcc_gc_backend4_zpage_owner_slot_span_card(
                moved_raw, &moved->methods[0].func
            );
            if (bases_card < 0 || mro_card < 0 || methods_card < 0) return 74;
            int64_t bases_to_mro = (mro_card - bases_card + 64) % 64;
            int64_t mro_to_methods = (methods_card - mro_card + 64) % 64;
            if (
                bases_to_mro < 1
                || bases_to_mro > 2
                || mro_to_methods < 1
                || mro_to_methods > 2
            ) return 75;
            py_decref(moved_raw);
            return 0;
        }

        static int check_continuation(void) {
            int32_t frame_map[1] = {4};
            PyObject *slots[4] = {
                py_int_from_i64(101),
                py_int_from_i64(102),
                py_int_from_i64(103),
                py_int_from_i64(104),
            };
            PyObject *cont_raw = py_continuation_new(
                frame_map, slots, (void *)0x1234
            );
            if (cont_raw == 0) return 30;
            PyContinuationObject *cont = (PyContinuationObject *)cont_raw;
            if (cont->stack_chunk == 0 || cont->stack_chunk->slots == 0) return 31;
            PyContinuationStackChunk *old_chunk = cont->stack_chunk;
            PyObject **old_slots = old_chunk->slots;
            if (!select_for_relocation(cont_raw)) return 32;
            if (pcc_gc_relocate_copy(cont_raw, 16) != 0) return 37;
            if (pcc_gc_relocation_set_contains(cont_raw) != 1) return 38;
            PyObject *moved_raw = pcc_gc_relocate_copy(
                cont_raw, (int64_t)sizeof(PyContinuationObject)
            );
            if (moved_raw == 0) return 33;
            PyContinuationObject *moved = (PyContinuationObject *)moved_raw;
            if (
                moved->stack_chunk == 0
                || moved->stack_chunk->slots == 0
                || moved->stack_chunk == old_chunk
                || moved->stack_chunk->slots == old_slots
                || moved->stack_chunk->slot_count != 4
                || moved->stack_chunk->root_map_slot_count != 4
            ) return 34;
            for (int64_t i = 0; i < 4; i++) {
                if (py_int_to_i64(moved->stack_chunk->slots[i], 0) != 101 + i) {
                    return 35;
                }
            }
            if (
                pcc_gc_backend4_zpage_owner_slot_span_card(
                    moved_raw, &moved->stack_chunk->slots[0]
                ) < 0
            ) return 36;
            py_decref(moved_raw);
            return 0;
        }

        static int check_list(void) {
            PyObject *list_raw = py_list_new(0);
            if (list_raw == 0) return 40;
            for (int64_t i = 0; i < 4; i++) {
                PyObject *value = py_int_from_i64(201 + i);
                py_list_append(list_raw, value);
                py_decref(value);
            }
            PyListObject *list = (PyListObject *)list_raw;
            PyObject **old_items = list->items;
            if (!select_for_relocation(list_raw)) return 41;
            if (pcc_gc_relocate_copy(list_raw, 16) != 0) return 42;
            if (pcc_gc_relocation_set_contains(list_raw) != 1) return 43;
            PyObject *moved_raw = pcc_gc_relocate_copy(
                list_raw, (int64_t)sizeof(PyListObject)
            );
            if (moved_raw == 0) return 44;
            PyListObject *moved = (PyListObject *)moved_raw;
            if (
                moved->items == 0
                || moved->items == old_items
                || moved->length != 4
                || moved->capacity < 4
            ) return 45;
            for (int64_t i = 0; i < 4; i++) {
                if (py_int_to_i64(moved->items[i], 0) != 201 + i) return 46;
            }
            py_decref(moved_raw);
            return 0;
        }

        static int check_dict(void) {
            PyObject *dict_raw = py_dict_new();
            PyObject *key = py_int_from_i64(301);
            PyObject *value = py_int_from_i64(302);
            if (dict_raw == 0 || key == 0 || value == 0) return 50;
            py_dict_set(dict_raw, key, value);
            py_decref(key);
            py_decref(value);
            PyDictObject *dict = (PyDictObject *)dict_raw;
            if (
                dict->size != 1
                || dict->capacity <= 0
                || dict->entries_used <= 0
                || dict->indices == 0
                || dict->entries == 0
            ) return 51;
            int64_t *old_indices = dict->indices;
            DictEntry *old_entries = dict->entries;
            int64_t old_capacity = dict->capacity;
            int64_t old_entries_used = dict->entries_used;
            if (!select_for_relocation(dict_raw)) return 52;
            if (pcc_gc_relocate_copy(dict_raw, 16) != 0) return 53;
            if (pcc_gc_relocation_set_contains(dict_raw) != 1) return 54;
            PyObject *moved_raw = pcc_gc_relocate_copy(
                dict_raw, (int64_t)sizeof(PyDictObject)
            );
            if (moved_raw == 0) return 55;
            PyDictObject *moved = (PyDictObject *)moved_raw;
            if (
                moved->indices == 0
                || moved->entries == 0
                || moved->indices == old_indices
                || moved->entries == old_entries
                || moved->size != 1
                || moved->capacity != old_capacity
                || moved->entries_used != old_entries_used
                || memcmp(
                    moved->indices,
                    old_indices,
                    (size_t)old_capacity * sizeof(int64_t)
                ) != 0
                || memcmp(
                    moved->entries,
                    old_entries,
                    (size_t)old_capacity * sizeof(DictEntry)
                ) != 0
                || pcc_gc_backend4_zpage_owner_slot_span_card(
                    moved_raw, &moved->entries[0].key
                ) < 0
            ) return 56;
            py_decref(moved_raw);
            return 0;
        }

        static int check_set(void) {
            PyObject *set_raw = py_set_new();
            PyObject *item = py_int_from_i64(401);
            if (set_raw == 0 || item == 0) return 60;
            py_set_add(set_raw, item);
            py_decref(item);
            PySetObject *set = (PySetObject *)set_raw;
            if (
                set->size != 1
                || set->capacity <= 0
                || set->fill <= 0
                || set->entries == 0
            ) return 61;
            SetEntry *old_entries = set->entries;
            int64_t old_capacity = set->capacity;
            int64_t old_fill = set->fill;
            if (!select_for_relocation(set_raw)) return 62;
            if (pcc_gc_relocate_copy(set_raw, 16) != 0) return 63;
            if (pcc_gc_relocation_set_contains(set_raw) != 1) return 64;
            PyObject *moved_raw = pcc_gc_relocate_copy(
                set_raw, (int64_t)sizeof(PySetObject)
            );
            if (moved_raw == 0) return 65;
            PySetObject *moved = (PySetObject *)moved_raw;
            if (
                moved->entries == 0
                || moved->entries == old_entries
                || moved->size != 1
                || moved->capacity != old_capacity
                || moved->fill != old_fill
                || memcmp(
                    moved->entries,
                    old_entries,
                    (size_t)old_capacity * sizeof(SetEntry)
                ) != 0
                || pcc_gc_backend4_zpage_owner_slot_span_card(
                    moved_raw, &moved->entries[0].key
                ) < 0
            ) return 66;
            py_decref(moved_raw);
            return 0;
        }

        int main(void) {
            if (pcc_refcount_strategy() != PCC_REFCOUNT_STRATEGY_ATOMIC) return 1;
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
        #if PCC_TEST_RAW_CASE == 1
            return check_exception();
        #elif PCC_TEST_RAW_CASE == 2
            return check_class();
        #elif PCC_TEST_RAW_CASE == 3
            return check_continuation();
        #elif PCC_TEST_RAW_CASE == 4
            return check_list();
        #elif PCC_TEST_RAW_CASE == 5
            return check_dict();
        #elif PCC_TEST_RAW_CASE == 6
            return check_set();
        #elif PCC_TEST_RAW_CASE == 7
            return check_class_span_order();
        #else
            return 3;
        #endif
        }
    '''


def test_backend4_deallocating_objects_are_quarantined_from_add_score_and_copy_source():
    c_src = (RUNTIME_DIR / "src" / "py_gc_backend.c").read_text(
        encoding="utf-8"
    )
    c_add = c_src.split("static int pcc_gc_relocation_set_add(", 1)[1].split(
        "static void pcc_gc_relocation_set_remove", 1
    )[0]
    c_add_guard = c_add.split("int32_t flags =", 1)[1].split(
        "if (pcc_gc_forwarding_find", 1
    )[0]
    assert "PY_FLAG_GC_RELOCATION_TARGET" in c_add_guard
    assert "PY_FLAG_GC_DEALLOCATING" in c_add_guard
    assert c_add_guard.index("PY_FLAG_GC_DEALLOCATING") < c_add_guard.index(
        "return 0;"
    )
    assert c_add.index("PY_FLAG_GC_DEALLOCATING") < c_add.index("calloc(")
    assert c_add.index("PY_FLAG_GC_DEALLOCATING") < c_add.index(
        "py_header_flags_or(h, PY_FLAG_GC_RELOCATION_CANDIDATE)"
    )
    c_score = c_src.split(
        "static int pcc_gc_backend4_zpage_candidate_snapshot(", 1
    )[1].split("static int pcc_gc_backend4_select_one_page_object_unlocked", 1)[0]
    assert (
        "if ((flags & PY_FLAG_GC_DEALLOCATING) != 0) return 0;" in c_score
    )
    assert c_score.index("PY_FLAG_GC_DEALLOCATING") < c_score.index(
        "int64_t owner_size = zp->size_bytes"
    )
    assert c_score.index("PY_FLAG_GC_DEALLOCATING") < c_score.index(
        "candidate->mapping = zp"
    )
    c_copy = c_src.split(
        "static int pcc_gc_relocate_copy_snapshot_unlocked(", 1
    )[1].split(
        "static PyObject *pcc_gc_relocate_copy_preallocated_unlocked(", 1
    )[0]
    c_copy_guard = c_copy.split("int32_t from_flags =", 1)[1].split(
        "if (!pcc_gc_colored_relocate_copy_supported_tag", 1
    )[0]
    assert "PY_FLAG_GC_PINNED | PY_FLAG_GC_DEALLOCATING" in c_copy_guard
    assert c_copy_guard.index("PY_FLAG_GC_DEALLOCATING") < c_copy_guard.index(
        "return 0;"
    )
    c_public_copy = c_src.split("PyObject *pcc_gc_relocate_copy(", 1)[1].split(
        "static int64_t pcc_gc_backend4_snapshot_relocation_batch_unlocked", 1
    )[0]
    assert c_public_copy.index("pcc_gc_graph_unlock();") < c_public_copy.index(
        "PyObject *to = pcc_gc_alloc("
    )

    strict_backend = (RUNTIME_DIR / "py" / "py_gc_backend.py").read_text(
        encoding="utf-8"
    )
    strict_add = strict_backend.split("def _relocation_set_add(", 1)[1].split(
        '@c_abi_export("pcc_gc_backend4_relocation_set_remove")', 1
    )[0]
    # Match the bits this contract is about, not the exact literal mask: the
    # guard has since grown the fresh-alloc and pinned bits, which does not
    # weaken the quarantine but did make an exact-string assertion fail while
    # the property it names still held.
    strict_guard = strict_add.split("flags & (", 1)[1].split(")", 1)[0]
    strict_guard_bits = {term.strip() for term in strict_guard.split("|")}
    assert {"8192", "524288"} <= strict_guard_bits, strict_guard
    strict_after_guard = strict_add.split(
        "flags & (" + strict_guard + ")", 1
    )[1]
    assert strict_after_guard.startswith(") != 0:\n        return 0"), (
        strict_after_guard[:60]
    )
    assert strict_add.index("524288") < strict_add.index("node = malloc(16)")
    assert strict_add.index("524288") < strict_add.index(
        "store_i32(obj, 12, flags | 2048)"
    )
    strict_selector = STRICT_RELOCATION_SELECTOR.read_text(encoding="utf-8")
    strict_score = strict_selector.split(
        "def _backend4_zpage_candidate_score(", 1
    )[1].split(
        '@c_abi_export("pcc_gc_relocation_selector_add_candidate_node")', 1
    )[0]
    assert (
        "if (flags & (64 | 2048 | 8192 | 524288)) != 0:\n        return -1"
        in strict_score
    )
    assert strict_score.index("524288") < strict_score.index(
        "size: i64 = load_i64(node, 32)"
    )
    assert strict_score.index("524288") < strict_score.index(
        'atomic_rmw_i32("or", obj, 12, 32768, "acq_rel")'
    )
    strict_copy_source = STRICT_RELOCATION_COPY.read_text(encoding="utf-8")
    strict_copy = strict_copy_source.split(
        '@c_abi_export("pcc_gc_relocate_copy")', 1
    )[1]
    assert "if (flags & (64 | 524288)) != 0:" in strict_copy
    assert strict_copy.index("if (flags & (64 | 524288)) != 0:") < (
        strict_copy.index("eligible = 0", strict_copy.index("524288"))
    )
    assert strict_copy.index("524288") < strict_copy.index(
        "to_obj = pcc_gc_alloc(size, tag, (flags & ~10240) | 64)"
    )


def test_backend4_c_runtime_quarantines_deallocating_objects_from_selection_and_copy(
    tmp_path: Path,
) -> None:
    result = _compile_and_run(tmp_path, _deallocating_relocation_quarantine_source())
    assert result.returncode == 0, result.stdout + result.stderr


def test_backend4_strict_runtime_quarantines_deallocating_objects_from_selection_and_copy(
    tmp_path: Path,
    pcc_py_runtime_archive: Path,
) -> None:
    result = _compile_and_run_archive(
        tmp_path,
        _deallocating_relocation_quarantine_source(),
        pcc_py_runtime_archive,
        "backend4_strict_deallocating_quarantine",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_backend4_c_relocation_copy_balances_owned_slot_retain(
    tmp_path: Path,
) -> None:
    result = _compile_and_run_threaded(
        tmp_path, _relocation_slot_retain_balance_source()
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_backend4_strict_relocation_copy_balances_owned_slot_retain(
    tmp_path: Path,
) -> None:
    runtime = cached_threaded_pcc_python_runtime()
    result = _compile_and_run_threaded_archive(
        tmp_path,
        _relocation_slot_retain_balance_source(),
        runtime / "libpy_runtime_pcc_py.a",
        "backend4_strict_slot_retain_balance",
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _build_threaded_runtime(tmp_path: Path) -> Path:
    del tmp_path
    return cached_threaded_c_runtime()


def _compile_and_run_threaded(
    tmp_path: Path,
    source: str,
) -> subprocess.CompletedProcess[str]:
    work_runtime = _build_threaded_runtime(tmp_path)
    src = tmp_path / "backend4_threaded_probe.c"
    exe = tmp_path / "backend4_threaded_probe.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    build = subprocess.run(
        [
            _cc(),
            "-std=c11",
            "-pthread",
            f"-I{work_runtime / 'include'}",
            f"-I{work_runtime / 'src'}",
            str(src),
            str(work_runtime / "libpy_runtime.a"),
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


@pytest.mark.parametrize("runtime_kind", ["c", "pcc_python"])
@pytest.mark.parametrize(
    ("raw_case", "raw_case_id"),
    [
        ("exception", 1),
        ("class", 2),
        ("continuation", 3),
        ("list", 4),
        ("dict", 5),
        ("set", 6),
        ("class-span-order", 7),
    ],
)
def test_backend4_relocation_copies_type_specific_raw_payloads(
    tmp_path: Path,
    runtime_kind: str,
    raw_case: str,
    raw_case_id: int,
) -> None:
    source = (
        f"#define PCC_TEST_RAW_CASE {raw_case_id}\n"
        + _relocation_type_specific_raw_payload_source()
    )
    if runtime_kind == "c":
        result = _compile_and_run_threaded(tmp_path, source)
    else:
        runtime = cached_threaded_pcc_python_runtime()
        result = _compile_and_run_threaded_archive(
            tmp_path,
            source,
            runtime / "libpy_runtime_pcc_py.a",
            f"backend4_strict_type_specific_raw_payload_{raw_case}",
        )
    assert result.returncode == 0, result.stdout + result.stderr


def test_backend4_skips_zpage_and_graph_for_leaf_objects(tmp_path: Path) -> None:
    result = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>
        #include <stdint.h>

        enum {
            PY_FLAG_GC_ZPAGE_ALLOC = 0x10000,
            PY_FLAG_GC_MALLOC_ALLOC = 0x40000
        };
        extern int64_t pcc_gc_object_is_known(PyObject *obj);

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) {
                return 2;
            }

            PyObject *leaf = pcc_gc_alloc(256, PY_TYPE_FLOAT, 0);
            PyObject *leaf_with_flag = pcc_gc_alloc(
                256,
                PY_TYPE_FLOAT,
                PY_FLAG_GC_ZPAGE_ALLOC
            );
            PyObject *container = pcc_gc_alloc(256, PY_TYPE_LIST, 0);
            if (leaf == 0 || leaf_with_flag == 0 || container == 0) return 3;

            PyObjectHeader *leaf_h = (PyObjectHeader *)leaf;
            PyObjectHeader *leaf_flag_h = (PyObjectHeader *)leaf_with_flag;
            PyObjectHeader *container_h = (PyObjectHeader *)container;
            printf("%d\\n", (leaf_h->flags & PY_FLAG_GC_ZPAGE_ALLOC) != 0);
            printf("%d\\n", (leaf_h->flags & PY_FLAG_GC_MALLOC_ALLOC) != 0);
            printf("%lld\\n", (long long)pcc_gc_object_is_known(leaf));
            printf("%d\\n", (leaf_flag_h->flags & PY_FLAG_GC_ZPAGE_ALLOC) != 0);
            printf("%d\\n", (leaf_flag_h->flags & PY_FLAG_GC_MALLOC_ALLOC) != 0);
            printf("%lld\\n", (long long)pcc_gc_object_is_known(leaf_with_flag));
            printf("%d\\n", (container_h->flags & PY_FLAG_GC_ZPAGE_ALLOC) != 0);
            printf("%d\\n", (container_h->flags & PY_FLAG_GC_MALLOC_ALLOC) != 0);
            printf("%lld\\n", (long long)pcc_gc_object_is_known(container));

            pcc_gc_release(leaf);
            pcc_gc_release(leaf_with_flag);
            pcc_gc_release(container);
            return 0;
        }
        """,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == [
        "0",
        "1",
        "0",
        "0",
        "1",
        "0",
        "1",
        "0",
        "1",
    ]

    c_obj = (RUNTIME_DIR / "src" / "py_obj.c").read_text(encoding="utf-8")
    assert "pcc_alloc_graph_leaf_tag(type_tag)" in c_obj
    assert "PY_FLAG_GC_MALLOC_ALLOC" in c_obj
    py_obj = (RUNTIME_DIR / "py" / "py_obj.py").read_text(encoding="utf-8")
    assert "_gc_graph_leaf_tag(type_tag)" in py_obj
    assert "stored_flags = (stored_flags & ~65536) | 262144" in py_obj


def test_backend4_deallocating_index_node_is_not_active(tmp_path: Path) -> None:
    """Logical death must precede every safepoint-capable deallocation step.

    A zpage object remains in the object index while its type-specific
    deallocator consumes inline fields. Other backends can also retain an
    indexed node between the refcount transition to zero and graph removal.
    The dedicated deallocating bit must make either node non-active; refcount
    zero alone remains insufficient because forwarding shells may use it.
    """
    result = _compile_and_run(
        tmp_path,
        """
        #include "py_internal.h"
        #include <stdio.h>

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) {
                return 2;
            }
            PyObject *obj = pcc_gc_alloc(256, PY_TYPE_LIST, 0);
            if (obj == 0) return 3;
            if (pcc_gc_object_is_known(obj) != 1) return 4;

            PyObjectHeader *header = (PyObjectHeader *)obj;
            header->refcount = 0;
            if (pcc_gc_object_is_known(obj) != 1) return 5;
            header->flags |= PY_FLAG_GC_DEALLOCATING;
            if (pcc_gc_object_is_known(obj) != 0) return 5;

            header->refcount = 1;
            pcc_gc_release(obj);
            printf("backend4-zero-refcount-node-inactive-ok\\n");
            return 0;
        }
        """,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "backend4-zero-refcount-node-inactive-ok"

    c_src = (RUNTIME_DIR / "src" / "py_gc_backend.c").read_text(encoding="utf-8")
    c_active = c_src.split("static int pcc_gc_object_node_is_active", 1)[1].split(
        "static void pcc_gc_object_node_link_head", 1
    )[0]
    assert "PY_FLAG_GC_DEALLOCATING" in c_active

    c_obj = (RUNTIME_DIR / "src" / "py_obj.c").read_text(encoding="utf-8")
    c_prepare_start = c_obj.rindex("static void pcc_decref_prepare(")
    c_finish_start = c_obj.rindex("static void pcc_decref_finish(")
    c_prepare = c_obj[c_prepare_start:c_finish_start]
    c_finish = c_obj[c_finish_start:c_obj.index("void py_decref(", c_finish_start)]
    assert c_prepare.index("pcc_refcount_decref(") < c_prepare.index(
        "prepared->new_refcount == 0"
    ) < c_prepare.index("py_header_flags_or(h, PY_FLAG_GC_DEALLOCATING)")
    assert "pcc_obj_runtime_log_event_code" not in c_prepare
    assert "PY_FLAG_GC_DEALLOCATING" not in c_finish
    assert c_finish.index("pcc_obj_runtime_log_event_code") < c_finish.index(
        "py_weakref_invalidate(o)"
    )

    c_finalize = c_src.split("static void pcc_gc_finalize_unreachable", 1)[1].split(
        "static void pcc_gc_recheck_reachability_after_finalizers", 1
    )[0]
    assert c_finalize.index("PY_FLAG_GC_DEALLOCATING") < c_finalize.index(
        "pcc_gc_note_object_freeing(o)"
    )

    root_src = STRICT_REFCOUNT_ROOTS.read_text(encoding="utf-8")
    py_active = root_src.split("def pcc_gc_object_node_is_active", 1)[1].split(
        "\n@c_abi_export", 1
    )[0]
    assert "if (load_i32(obj, 12) & 524288) != 0:" in py_active

    py_obj = (RUNTIME_DIR / "py" / "py_obj.py").read_text(encoding="utf-8")
    py_prepare = py_obj.split("def _py_decref_prepare(", 1)[1].split(
        "def _py_decref_finish(", 1
    )[0]
    py_finish = py_obj.split("def _py_decref_finish(", 1)[1].split(
        '@c_abi_export("py_decref")', 1
    )[0]
    assert py_prepare.index("new_rc: int = pcc_refcount_decref(o)") < (
        py_prepare.index("if new_rc == 0:")
    ) < py_prepare.index(
        "store_i32(o, PYOBJECTHEADER_FLAGS_OFFSET, flags | 524288)"
    )
    assert "pcc_runtime_log_event_code" not in py_prepare
    assert "flags | 524288" not in py_finish
    assert py_finish.index("pcc_runtime_log_event_code") < py_finish.index(
        "py_weakref_invalidate(o)"
    )

    sweep_src = STRICT_TRACING_SWEEP_COLLECTOR.read_text(encoding="utf-8")
    py_finalize = sweep_src.split(
        "def pcc_gc_tracing_finalize_unreachable", 1
    )[1].split("\n@c_abi_export", 1)[0]
    assert py_finalize.index("store_i32(obj, 12, flags | 524288)") < (
        py_finalize.index("pcc_gc_note_object_freeing(obj)")
    )


def test_backend4_forwarding_target_lookup_is_indexed() -> None:
    c_src = (RUNTIME_DIR / "src" / "py_gc_backend.c").read_text(encoding="utf-8")
    c_target_exists = c_src.split("static int pcc_gc_forwarding_target_exists", 1)[
        1
    ].split("static int pcc_gc_forwarding_target_prepare", 1)[0]
    assert "pcc_gc_forwarding_target_find(target) != NULL" in c_target_exists
    assert "pcc_gc_forwardings" not in c_target_exists
    assert "target_next" in c_src
    assert "target_prev" in c_src
    assert "pcc_gc_forwarding_target_index_clear()" in c_src

    index_src = (RUNTIME_DIR / "src" / "py_gc_index_table.c").read_text(
        encoding="utf-8"
    )
    assert "pcc_gc_forwarding_target_index_find" in index_src
    assert "pcc_gc_forwarding_target_index_upsert" in index_src
    assert "pcc_gc_forwarding_target_index_remove" in index_src

    # The forwarding-target identity surface moved to the freestanding
    # forwarding-identity module as the GC4 relocation policy migrated.
    identity_src = (
        RUNTIME_DIR / "py" / "freestanding_gc_forwarding_identity.py"
    ).read_text(encoding="utf-8")
    py_target_exists = identity_src.split(
        '@c_abi_export("pcc_gc_forwarding_target_find")', 1
    )[1].split('@c_abi_export("pcc_gc_forwarding_target_prepare")', 1)[0]
    assert "pcc_gc_forwarding_target_index_find(target)" in py_target_exists
    assert "_forwarding_head()" not in py_target_exists
    assert "pcc_gc_forwarding_target_index_upsert" in identity_src
    assert "store_ptr(node, 32, old_head)" in identity_src
    assert "store_ptr(node, 40, null())" in identity_src
    assert "pcc_gc_forwarding_target_index_clear" in identity_src
    # The raw forwarding-node layout (offsets 32/40/48/56) is pinned by the
    # shared GC-node module that both the C oracle and the port consume.
    node_src = (RUNTIME_DIR / "py" / "freestanding_gc_object_nodes.py").read_text(
        encoding="utf-8"
    ) if (RUNTIME_DIR / "py" / "freestanding_gc_object_nodes.py").exists() else ""
    if "store_ptr(node, 32" not in identity_src and "node, 32" not in node_src:
        raise AssertionError("forwarding node layout (offset 32) not pinned")


def test_backend4_zpage_owner_lookup_is_indexed(tmp_path: Path) -> None:
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include "py_internal.h"
        #include <stdio.h>

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            PyObject *obj = pcc_gc_alloc(256, PY_TYPE_LIST, 0);
            if (obj == 0) return 3;
            if (pcc_gc_zpage_owner_index_find(obj) == 0) return 4;

            pcc_gc_release(obj);
            if (pcc_gc_zpage_owner_index_find(obj) != 0) return 5;

            printf("backend4-zpage-owner-index-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-zpage-owner-index-ok"

    c_src = (RUNTIME_DIR / "src" / "py_gc_backend.c").read_text(encoding="utf-8")
    c_link = c_src.split("static void pcc_gc_backend4_zpage_link_node_unlocked", 1)[
        1
    ].split("static void pcc_gc_backend4_zpage_unlink_node_unlocked", 1)[0]
    c_unlink = c_src.split("static void pcc_gc_backend4_zpage_unlink_node_unlocked", 1)[
        1
    ].split("static PccGcZPageNode *pcc_gc_backend4_zpage_track_alloc_unlocked", 1)[0]
    c_remove = c_src.split("static void pcc_gc_backend4_zpage_remove_unlocked", 1)[
        1
    ].split("static PccGcZPageNode *pcc_gc_backend4_zpage_find_unlocked", 1)[0]
    c_freeing = c_src.split("void pcc_gc_note_object_freeing", 1)[1].split(
        "if (!pcc_gc_tracks_objects())", 1
    )[0]
    c_free_memory = c_src.split("void pcc_gc_free_object_memory", 1)[1].split(
        "void pcc_gc_note_load", 1
    )[0]
    assert "pcc_gc_zpage_owner_index_upsert(node->owner, node)" in c_link
    assert "pcc_gc_zpage_owner_index_remove(node->owner)" in c_unlink
    assert "pcc_gc_zpage_owner_index_find(owner)" in c_remove
    assert "int64_t size = dead->size_bytes" in c_remove
    assert "int32_t zpage_flags" in c_freeing
    assert "zpage_owner_node" in c_freeing
    assert "int32_t zpage_indexed" in c_freeing
    assert "pcc_gc_backend4_zpage_owns_addr_unlocked(o)" not in c_freeing
    assert "if (zpage_flags != 0 || zpage_indexed != 0)" in c_freeing
    assert "pcc_gc_backend4_zpage_owns_addr_unlocked(o)" not in c_free_memory
    assert "An unlabelled/foreign" in c_free_memory

    py_src = (RUNTIME_DIR / "py" / "py_gc_backend.py").read_text(encoding="utf-8")
    zpage_mechanics = STRICT_ZPAGE_MECHANICS.read_text(encoding="utf-8")
    zpage_lifecycle = STRICT_ZPAGE_LIFECYCLE.read_text(encoding="utf-8")
    py_link = zpage_mechanics.split(
        "def pcc_gc_backend4_zpage_link_node", 1
    )[1].split(
        '@c_abi_export("pcc_gc_backend4_zpage_find_page_for_addr")', 1
    )[0]
    py_unlink = zpage_lifecycle.split(
        "def pcc_gc_backend4_zpage_unlink_node", 1
    )[1].split('@c_abi_export("pcc_gc_backend4_zpage_find")', 1)[0]
    py_remove = zpage_lifecycle.split(
        "def pcc_gc_backend4_zpage_detach_for_relocation(owner)", 1
    )[1].split(
        '@c_abi_export("pcc_gc_backend4_zpage_finish_relocation_detach")', 1
    )[0]
    py_freeing = py_src.split("def pcc_gc_note_object_freeing", 1)[1].split(
        "if _gc_tracks_objects() == 0", 1
    )[0]
    py_free_memory = py_src.split("def pcc_gc_free_object_memory", 1)[1].split(
        '@c_abi_export("pcc_gc_note_alloc")', 1
    )[0]
    assert "pcc_gc_zpage_owner_index_upsert(load_ptr(node, 0), node)" in py_link
    assert "pcc_gc_zpage_owner_index_remove(load_ptr(node, 0))" in py_unlink
    assert "indexed = pcc_gc_zpage_owner_index_find(owner)" in py_remove
    # The port migrated its annotations from `int` to explicit `i64`; pin the
    # slot that matters (the node's size at offset 32), not the annotation.
    assert "load_i64(node, 32)" in py_remove
    assert "zpage_flags: int = load_i32(o, 12) & 65536" in py_freeing
    assert "zpage_owner_node = pcc_gc_object_index_find(o)" in py_freeing
    assert "zpage_indexed: int = 0" in py_freeing
    assert "_backend4_zpage_owns_addr(o)" not in py_freeing
    assert "if zpage_flags != 0 or zpage_indexed != 0" in py_freeing
    assert "_backend4_zpage_owns_addr(o)" not in py_free_memory
    assert "Unknown/foreign origin" in py_free_memory


def test_relocation_reset_retires_detached_nodes_after_graph_unlock() -> None:
    c_src = (RUNTIME_DIR / "src" / "py_gc_backend.c").read_text(
        encoding="utf-8"
    )
    py_src = (RUNTIME_DIR / "py" / "py_gc_backend.py").read_text(
        encoding="utf-8"
    )
    c_reset = c_src.split("void pcc_gc_reset_relocation_set", 1)[1].split(
        "int64_t pcc_gc_relocation_set_contains", 1
    )[0]
    py_reset = py_src.split("def pcc_gc_reset_relocation_set", 1)[1].split(
        '@c_abi_export("pcc_gc_relocation_set_contains")', 1
    )[0]
    for body, unlock, finish in (
        (c_reset, "pcc_gc_graph_unlock();", "pcc_gc_relocation_reset_finish("),
        (py_reset, "_object_graph_unlock()", "_relocation_reset_finish("),
    ):
        unlock_at = body.index(unlock)
        finish_at = body.index(finish)
        assert unlock_at < finish_at
        assert "free(" not in body[:unlock_at]
        assert "relocation_reset_owner" in body[:finish_at]


def test_relocation_reseed_prepares_evacuation_nodes_before_locked_commit() -> None:
    c_src = (RUNTIME_DIR / "src" / "py_gc_backend.c").read_text(
        encoding="utf-8"
    )
    py_src = (RUNTIME_DIR / "py" / "py_gc_backend.py").read_text(
        encoding="utf-8"
    )
    c_reseed = c_src.split(
        "static void pcc_gc_backend4_reseed_relocation_epoch_state", 1
    )[1].split("int64_t pcc_gc_telemetry_reset", 1)[0]
    py_reseed = py_src.split(
        "def _backend4_reseed_relocation_epoch_state", 1
    )[1].split('@c_abi_export("pcc_gc_backend4_evacuation_page_find")', 1)[0]

    for body, unlock, prepare, lock, page_scan in (
        (
            c_reseed,
            "pcc_gc_graph_unlock();",
            "pcc_gc_backend4_evacuation_page_nodes_prepare(",
            "pcc_gc_graph_lock();",
            "pcc_gc_backend4_reseed_plan_probe_wait(4)",
        ),
        (
            py_reseed,
            "_object_graph_unlock()",
            "_backend4_evacuation_page_nodes_prepare(",
            "_object_graph_lock()",
            "_backend4_reseed_plan_probe_wait(4)",
        ),
    ):
        unlock_at = body.index(unlock)
        prepare_at = body.index(prepare)
        relock_at = body.index(lock, prepare_at)
        page_scan_at = body.index(page_scan, relock_at)
        assert unlock_at < prepare_at < relock_at < page_scan_at

    assert "pcc_gc_backend4_evacuation_page_add_unlocked(page)" not in c_reseed
    assert "_backend4_evacuation_page_add(page)" not in py_reseed
    assert "pcc_gc_backend4_evacuation_page_detach_all_unlocked" not in c_reseed
    assert "_backend4_evacuation_page_detach_all()" not in py_reseed
    for body, failed, finish, page_scan in (
        (
            c_reseed,
            "if (prepared_count < required)",
            "pcc_gc_backend4_evacuation_page_finish_detached(",
            "pcc_gc_backend4_reseed_plan_probe_wait(4)",
        ),
        (
            py_reseed,
            "if prepared_count < required:",
            "_backend4_evacuation_page_finish_detached(",
            "_backend4_reseed_plan_probe_wait(4)",
        ),
    ):
        failure_at = body.index(failed)
        finish_at = body.index(finish, failure_at)
        page_scan_at = body.index(page_scan)
        assert failure_at < finish_at < page_scan_at


def test_relocation_reset_batches_raw_node_scans_with_owned_cursor() -> None:
    c_src = (RUNTIME_DIR / "src" / "py_gc_backend.c").read_text(
        encoding="utf-8"
    )
    py_src = (RUNTIME_DIR / "py" / "py_gc_backend.py").read_text(
        encoding="utf-8"
    )
    strict_state = (
        RUNTIME_DIR / "py" / "freestanding_gc_state.py"
    ).read_text(encoding="utf-8")
    strict_nodes = (
        RUNTIME_DIR / "py" / "freestanding_gc_object_nodes.py"
    ).read_text(encoding="utf-8")
    strict_selector = STRICT_RELOCATION_SELECTOR.read_text(encoding="utf-8")
    strict_forwarding = (
        RUNTIME_DIR / "py" / "freestanding_gc_forwarding_identity.py"
    ).read_text(encoding="utf-8")
    c_reset = c_src.split("void pcc_gc_reset_relocation_set", 1)[1].split(
        "int64_t pcc_gc_relocation_set_contains", 1
    )[0]
    py_reset = py_src.split("def pcc_gc_reset_relocation_set", 1)[1].split(
        '@c_abi_export("pcc_gc_relocation_set_contains")', 1
    )[0]

    assert "pcc_gc_backend4_relocation_reset_owner" in c_reset
    assert "PCC_GC_SAFEPOINT_BATCH" in c_reset
    assert c_reset.count("pcc_thread_safepoint();") >= 2
    assert "pcc_gc_backend4_reset_object_cursor" in c_reset
    assert c_src.index("pcc_gc_graph_unlock();", c_src.index(c_reset)) < (
        c_src.index("pcc_thread_safepoint();", c_src.index(c_reset))
    )
    assert "pcc_gc_backend4_reset_object_cursor == n" in c_src
    assert "pcc_gc_backend4_relocation_reset_owner != 0" in c_src

    assert '"pcc_gc_backend4_relocation_reset_owner"' in py_reset
    assert "examined < 16" in py_reset
    assert py_reset.count("pcc_thread_safepoint()") >= 2
    assert '"pcc_gc_backend4_reset_object_cursor"' in py_reset
    assert 'define_global_i64("pcc_gc_backend4_relocation_reset_owner", 0)' in (
        strict_state
    )
    assert 'define_global_ptr_null("pcc_gc_backend4_reset_object_cursor")' in (
        strict_state
    )
    assert '"pcc_gc_backend4_reset_object_cursor"' in strict_nodes
    assert '"pcc_gc_backend4_relocation_reset_owner"' in strict_selector
    assert '"pcc_gc_backend4_relocation_reset_owner"' in strict_forwarding


def test_relocation_reseed_has_deterministic_plan_window_and_failure_control() -> None:
    c_src = (RUNTIME_DIR / "src" / "py_gc_backend.c").read_text(
        encoding="utf-8"
    )
    py_src = (RUNTIME_DIR / "py" / "py_gc_backend.py").read_text(
        encoding="utf-8"
    )
    header = (RUNTIME_DIR / "include" / "py_runtime.h").read_text(
        encoding="utf-8"
    )
    c_reseed = c_src.split(
        "static void pcc_gc_backend4_reseed_relocation_epoch_state", 1
    )[1].split("void pcc_gc_telemetry_reset", 1)[0]
    py_reseed = py_src.split(
        "def _backend4_reseed_relocation_epoch_state", 1
    )[1].split('@c_abi_export("pcc_gc_backend4_evacuation_page_find")', 1)[0]

    for source in (c_src, py_src, header):
        assert "pcc_gc_backend4_reseed_plan_probe_config" in source
        assert "pcc_gc_backend4_reseed_plan_probe_state" in source
    for body, wait, prepare in (
        (
            c_reseed,
            "pcc_gc_backend4_reseed_plan_probe_wait(1)",
            "pcc_gc_backend4_evacuation_page_nodes_prepare(",
        ),
        (
            py_reseed,
            "_backend4_reseed_plan_probe_wait(1)",
            "_backend4_evacuation_page_nodes_prepare(",
        ),
    ):
        assert body.index(wait) < body.index(prepare)
    assert "pcc_gc_backend4_reseed_plan_probe_allocation_limit" in c_src
    assert '"pcc_gc_backend4_reseed_plan_probe_allocation_limit"' in py_src


def test_relocation_reseed_required_page_count_is_bounded_and_restartable() -> None:
    c_src = (RUNTIME_DIR / "src" / "py_gc_backend.c").read_text(
        encoding="utf-8"
    )
    py_src = (RUNTIME_DIR / "py" / "py_gc_backend.py").read_text(
        encoding="utf-8"
    )
    strict_state = (
        RUNTIME_DIR / "py" / "freestanding_gc_state.py"
    ).read_text(encoding="utf-8")
    c_reseed = c_src.split(
        "static void pcc_gc_backend4_reseed_relocation_epoch_state", 1
    )[1].split("void pcc_gc_telemetry_reset", 1)[0]
    py_reseed = py_src.split(
        "def _backend4_reseed_relocation_epoch_state", 1
    )[1].split('@c_abi_export("pcc_gc_backend4_evacuation_page_find")', 1)[0]

    assert "pcc_gc_backend4_reseed_page_count_unlocked" not in c_reseed
    assert "pcc_gc_backend4_reseed_page_count_cursor" in c_reseed
    assert "pcc_gc_backend4_reseed_page_revision" in c_reseed
    assert "PCC_GC_SAFEPOINT_BATCH" in c_reseed
    assert "pcc_thread_safepoint();" in c_reseed
    assert "_backend4_reseed_page_count()" not in py_reseed
    assert '"pcc_gc_backend4_reseed_page_count_cursor"' in py_reseed
    assert '"pcc_gc_backend4_reseed_page_revision"' in py_reseed
    assert "examined < 16" in py_reseed
    assert "pcc_thread_safepoint()" in py_reseed
    assert 'define_global_ptr_null("pcc_gc_backend4_reseed_page_count_cursor")' in (
        strict_state
    )
    assert 'define_global_i64("pcc_gc_backend4_reseed_page_revision", 0)' in (
        strict_state
    )


def test_relocation_reseed_aggregate_is_bounded_and_restartable() -> None:
    c_src = (RUNTIME_DIR / "src" / "py_gc_backend.c").read_text(
        encoding="utf-8"
    )
    py_src = (RUNTIME_DIR / "py" / "py_gc_backend.py").read_text(
        encoding="utf-8"
    )
    strict_state = (
        RUNTIME_DIR / "py" / "freestanding_gc_state.py"
    ).read_text(encoding="utf-8")
    c_reseed = c_src.split(
        "static void pcc_gc_backend4_reseed_relocation_epoch_state", 1
    )[1].split("void pcc_gc_telemetry_reset", 1)[0]
    py_reseed = py_src.split(
        "def _backend4_reseed_relocation_epoch_state", 1
    )[1].split('@c_abi_export("pcc_gc_backend4_evacuation_page_find")', 1)[0]

    assert "pcc_gc_backend4_reseed_relocation_cursor" in c_reseed
    assert "pcc_gc_backend4_reseed_relocation_revision" in c_reseed
    assert c_reseed.count("PCC_GC_SAFEPOINT_BATCH") >= 2
    assert "pcc_gc_backend4_reseed_plan_probe_wait(2)" in c_reseed
    assert '"pcc_gc_backend4_reseed_relocation_cursor"' in py_reseed
    assert '"pcc_gc_backend4_reseed_relocation_revision"' in py_reseed
    assert py_reseed.count("examined < 16") >= 2
    assert "_backend4_reseed_plan_probe_wait(2)" in py_reseed
    assert 'define_global_ptr_null("pcc_gc_backend4_reseed_relocation_cursor")' in (
        strict_state
    )
    assert 'define_global_i64("pcc_gc_backend4_reseed_relocation_revision", 0)' in (
        strict_state
    )


def test_relocation_reseed_page_commit_is_bounded_without_raw_page_escape() -> None:
    c_src = (RUNTIME_DIR / "src" / "py_gc_backend.c").read_text(
        encoding="utf-8"
    )
    py_src = (RUNTIME_DIR / "py" / "py_gc_backend.py").read_text(
        encoding="utf-8"
    )
    strict_state = (
        RUNTIME_DIR / "py" / "freestanding_gc_state.py"
    ).read_text(encoding="utf-8")
    strict_selector = (
        RUNTIME_DIR / "py" / "freestanding_gc_relocation_selector.py"
    ).read_text(encoding="utf-8")
    strict_forwarding = (
        RUNTIME_DIR / "py" / "freestanding_gc_forwarding_identity.py"
    ).read_text(encoding="utf-8")
    strict_copy = (
        RUNTIME_DIR / "py" / "freestanding_gc_relocation_copy.py"
    ).read_text(encoding="utf-8")
    c_reseed = c_src.split(
        "static void pcc_gc_backend4_reseed_relocation_epoch_state", 1
    )[1].split("void pcc_gc_telemetry_reset", 1)[0]
    py_reseed = py_src.split(
        "def _backend4_reseed_relocation_epoch_state", 1
    )[1].split('@c_abi_export("pcc_gc_backend4_evacuation_page_find")', 1)[0]

    assert "pcc_gc_backend4_reseed_commit_owner" in c_reseed
    assert "pcc_gc_backend4_reseed_plan_probe_wait(4)" in c_reseed
    assert c_reseed.count("PCC_GC_SAFEPOINT_BATCH") >= 3
    assert "pcc_gc_backend4_evacuation_page_detach_all_unlocked" not in c_reseed
    assert "pcc_gc_backend4_evacuation_page_add_preallocated_unlocked" not in c_reseed
    assert '"pcc_gc_backend4_reseed_commit_owner"' in py_reseed
    assert "_backend4_reseed_plan_probe_wait(4)" in py_reseed
    assert py_reseed.count("examined < 16") >= 3
    assert "_backend4_evacuation_page_detach_all()" not in py_reseed
    assert "_backend4_evacuation_page_add_preallocated(" not in py_reseed
    assert 'define_global_i64("pcc_gc_backend4_reseed_commit_owner", 0)' in (
        strict_state
    )
    c_candidate_add = c_src.split(
        "static int pcc_gc_relocation_set_add", 1
    )[1].split("static int pcc_gc_relocation_set_add_preallocated", 1)[0]
    c_copy_snapshot = c_src.split(
        "static int pcc_gc_relocate_copy_snapshot_unlocked", 1
    )[1].split("typedef struct", 1)[0]
    c_forwarding_prepare = c_src.rsplit(
        "static PccGcForwardingInstallPlan *pcc_gc_forwarding_install_plan_prepare",
        1,
    )[1].split(
        "static int64_t pcc_gc_install_forwarding_preallocated_unlocked", 1
    )[0]
    strict_candidate_add = strict_selector.split(
        "def _backend4_add_candidate_node", 1
    )[1].split("@c_abi_export", 1)[0]
    strict_forwarding_prepare = strict_forwarding.split(
        "def pcc_gc_forwarding_install_plan_prepare", 1
    )[1].split("@c_abi_export", 1)[0]
    strict_copy_entry = strict_copy.split(
        "def pcc_gc_relocate_copy", 1
    )[1]
    for body in (
        c_candidate_add,
        c_copy_snapshot,
        c_forwarding_prepare,
        strict_candidate_add,
        strict_forwarding_prepare,
        strict_copy_entry,
    ):
        assert "pcc_gc_backend4_reseed_commit_owner" in body


def test_c_reseed_retains_multiple_authoritative_evacuation_pages(
    tmp_path: Path,
) -> None:
    result = _compile_and_run(
        tmp_path, _reseed_authoritative_evacuation_pages_source()
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "backend4-reseed-authoritative-pages-ok\n"


def test_strict_reseed_retains_multiple_authoritative_evacuation_pages(
    tmp_path: Path,
    pcc_py_runtime_archive: Path,
) -> None:
    result = _compile_and_run_archive(
        tmp_path,
        _reseed_authoritative_evacuation_pages_source(),
        pcc_py_runtime_archive,
        "backend4_strict_reseed_authoritative_pages",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "backend4-reseed-authoritative-pages-ok\n"


def test_c_concurrent_reset_reseed_revalidates_prepared_plan(
    tmp_path: Path,
) -> None:
    runtime = cached_threaded_c_runtime()
    result = _compile_and_run_threaded_archive(
        tmp_path,
        _concurrent_reset_reseed_plan_source(),
        runtime / "libpy_runtime.a",
        "backend4_c_concurrent_reset_reseed",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "24,24,3072,1,0\n"


def test_strict_concurrent_reset_reseed_revalidates_prepared_plan(
    tmp_path: Path,
) -> None:
    runtime = cached_threaded_pcc_python_runtime()
    result = _compile_and_run_threaded_archive(
        tmp_path,
        _concurrent_reset_reseed_plan_source(),
        runtime / "libpy_runtime_pcc_py.a",
        "backend4_strict_concurrent_reset_reseed",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "24,24,3072,1,0\n"


def test_c_reseed_forces_plan_growth_and_allocation_failure(
    tmp_path: Path,
) -> None:
    runtime = cached_threaded_c_runtime()
    result = _compile_and_run_threaded_archive(
        tmp_path,
        _forced_reseed_plan_paths_source(),
        runtime / "libpy_runtime.a",
        "backend4_c_forced_reseed_plan_paths",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "2,2,8320,2,2,2,8320,0\n"


def test_strict_reseed_forces_plan_growth_and_allocation_failure(
    tmp_path: Path,
) -> None:
    runtime = cached_threaded_pcc_python_runtime()
    result = _compile_and_run_threaded_archive(
        tmp_path,
        _forced_reseed_plan_paths_source(),
        runtime / "libpy_runtime_pcc_py.a",
        "backend4_strict_forced_reseed_plan_paths",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "2,2,8320,2,2,2,8320,0\n"


def test_c_reseed_counts_more_than_one_page_batch(tmp_path: Path) -> None:
    result = _compile_and_run(tmp_path, _many_page_reseed_source())
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "24,24,1440000\n"


def test_strict_reseed_counts_more_than_one_page_batch(
    tmp_path: Path,
    pcc_py_runtime_archive: Path,
) -> None:
    result = _compile_and_run_archive(
        tmp_path,
        _many_page_reseed_source(),
        pcc_py_runtime_archive,
        "backend4_strict_many_page_reseed",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "24,24,1440000\n"


def test_c_reseed_count_cursor_survives_concurrent_full_reset(
    tmp_path: Path,
) -> None:
    runtime = cached_threaded_c_runtime()
    result = _compile_and_run_threaded_archive(
        tmp_path,
        _forced_reseed_count_unlink_source(),
        runtime / "libpy_runtime.a",
        "backend4_c_forced_reseed_count_unlink",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "0,0,24,24,1440000,0\n"


def test_strict_reseed_count_cursor_survives_concurrent_full_reset(
    tmp_path: Path,
) -> None:
    runtime = cached_threaded_pcc_python_runtime()
    result = _compile_and_run_threaded_archive(
        tmp_path,
        _forced_reseed_count_unlink_source(),
        runtime / "libpy_runtime_pcc_py.a",
        "backend4_strict_forced_reseed_count_unlink",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "0,0,24,24,1440000,0\n"


def test_c_reseed_aggregate_cursor_survives_concurrent_full_reset(
    tmp_path: Path,
) -> None:
    runtime = cached_threaded_c_runtime()
    result = _compile_and_run_threaded_archive(
        tmp_path,
        _forced_reseed_aggregate_unlink_source(),
        runtime / "libpy_runtime.a",
        "backend4_c_forced_reseed_aggregate_unlink",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "0,0,24,24,1440000,0\n"


def test_strict_reseed_aggregate_cursor_survives_concurrent_full_reset(
    tmp_path: Path,
) -> None:
    runtime = cached_threaded_pcc_python_runtime()
    result = _compile_and_run_threaded_archive(
        tmp_path,
        _forced_reseed_aggregate_unlink_source(),
        runtime / "libpy_runtime_pcc_py.a",
        "backend4_strict_forced_reseed_aggregate_unlink",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "0,0,24,24,1440000,0\n"


def test_c_reseed_page_cursor_survives_concurrent_full_reset(
    tmp_path: Path,
) -> None:
    runtime = cached_threaded_c_runtime()
    result = _compile_and_run_threaded_archive(
        tmp_path,
        _forced_reseed_page_commit_unlink_source(),
        runtime / "libpy_runtime.a",
        "backend4_c_forced_reseed_page_unlink",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "0,0,24,24,1440000,0\n"


def test_strict_reseed_page_cursor_survives_concurrent_full_reset(
    tmp_path: Path,
) -> None:
    runtime = cached_threaded_pcc_python_runtime()
    result = _compile_and_run_threaded_archive(
        tmp_path,
        _forced_reseed_page_commit_unlink_source(),
        runtime / "libpy_runtime_pcc_py.a",
        "backend4_strict_forced_reseed_page_unlink",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "0,0,24,24,1440000,0\n"


def test_backend4_unknown_allocation_origin_fails_closed_without_free(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include "py_internal.h"
        #include <stdio.h>
        #include <stdlib.h>

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) {
                return 2;
            }

            PyObject *foreign = (PyObject *)calloc(1, sizeof(PyObjectHeader));
            if (foreign == 0) return 3;
            ((PyObjectHeader *)foreign)->type_tag = PY_TYPE_FLOAT;

            /* No backend-4 allocation-origin flag: runtime must neither scan
             * zpages nor pass this foreign pointer to free(3).  The explicit
             * free below is also a double-free oracle. */
            pcc_gc_free_object_memory(foreign);
            free(foreign);

            printf("backend4-unknown-origin-fail-closed-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-unknown-origin-fail-closed-ok"


def test_backend4_relocation_stress_stable_ids_and_no_old_addresses(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_internal.h"
        #include <stdio.h>

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            PyObject *roots[64];
            for (int i = 0; i < 64; i++) roots[i] = 0;
            for (int i = 0; i < 64; i++) {
                pcc_gc_scheduler_root_register(&roots[i]);
                PyObject *obj = py_list_new(0);
                pcc_gc_store_root(&roots[i], obj);
                (void)pcc_gc_object_id(obj);
                pcc_gc_release(obj);
            }

            pcc_gc_telemetry_reset();
            int64_t moved = 0;
            int64_t round_work[16];
            int64_t round_relocation_set[16];
            int64_t round_forwardings[16];
            for (int round = 0; round < 16; round++) {
                round_work[round] = pcc_gc_step(256);
                moved += round_work[round];
                for (int i = 0; i < 64; i++) {
                    PyObject *resolved = pcc_gc_load_ptr(0, &roots[i]);
                    if (resolved == 0) return 10;
                    if (pcc_gc_object_id(resolved) <= 0) return 11;
                }
                round_relocation_set[round] = pcc_gc_telemetry(
                    PCC_GC_COUNTER_RELOCATION_SET_SIZE
                );
                round_forwardings[round] = pcc_gc_telemetry(
                    PCC_GC_COUNTER_FORWARDING_ENTRIES
                );
            }

            if (moved <= 0) return 12;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_RELOCATION_FORWARDS) <= 0) return 13;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_FORWARDING_ENTRIES) != 0) {
                for (int round = 0; round < 16; round++) {
                    fprintf(
                        stderr,
                        "round=%d work=%lld relocation_set=%lld forwardings=%lld\\n",
                        round,
                        (long long)round_work[round],
                        (long long)round_relocation_set[round],
                        (long long)round_forwardings[round]
                    );
                }
                return 14;
            }
            if (pcc_gc_telemetry(PCC_GC_COUNTER_STABLE_IDS) < 64) return 15;
            if (pcc_gc_backend4_verify_no_old_addresses() != 1) return 16;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_RELOCATION_FRAGMENTATION_SCORE) < 0) return 17;

            for (int i = 0; i < 64; i++) {
                pcc_gc_store_root(&roots[i], 0);
                pcc_gc_scheduler_root_unregister(&roots[i]);
            }
            printf("backend4-production-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-production-ok"


def test_backend4_relocation_preserves_container_payloads_under_stress(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>
        #include <stdlib.h>
        #include <stdlib.h>
        #include <stdlib.h>

        static int check_list(PyObject *obj, int n) {
            PyObject *resolved = pcc_gc_note_relocation_read(obj);
            if (py_list_len(resolved) != n) return 0;
            for (int i = 0; i < n; i++) {
                PyObject *item = py_list_get(resolved, i);
                int ok = py_int_to_i64(item, 0) == i;
                pcc_gc_release(item);
                if (!ok) return 0;
            }
            return 1;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            PyObject *root = 0;
            pcc_gc_scheduler_root_register(&root);
            PyObject *lst = py_list_new(0);
            for (int i = 0; i < 100; i++) {
                py_list_append(lst, py_int_from_i64(i));
            }
            int64_t stable = pcc_gc_object_id(lst);
            pcc_gc_store_root(&root, lst);
            pcc_gc_release(lst);

            for (int round = 0; round < 8; round++) {
                (void)pcc_gc_step(512);
                PyObject *loaded = pcc_gc_load_ptr(0, &root);
                if (loaded == 0) return 10;
                if (pcc_gc_object_id(loaded) != stable) return 11;
                if (!check_list(loaded, 100)) return 12;
            }
            pcc_gc_store_root(&root, 0);
            pcc_gc_scheduler_root_unregister(&root);
            printf("backend4-container-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-container-ok"


def test_backend4_obj_dispatch_loads_forwarded_exception_slots(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>
        #include <stdlib.h>

        typedef struct ProbeClassObject {
            PyObjectHeader h;
            const char *name;
            int32_t n_bases;
            struct ProbeClassObject **bases;
            int32_t n_mro;
            struct ProbeClassObject **mro;
            int32_t n_methods;
            void *methods;
            int32_t n_fields;
            const char **field_names;
            int32_t instance_size;
            int32_t type_tag_alloc;
            PyObject *del_method;
            PyObject *attrs;
        } ProbeClassObject;

        typedef struct {
            PyObjectHeader h;
            ProbeClassObject *exc_class;
            PyObject *message;
            PyObject *cause;
            PyObject *context;
            void *traceback;
            int32_t n_frames;
            int32_t cap_frames;
        } ProbeExceptionObject;

            typedef struct {
                PyObjectHeader h;
                int64_t byte_len;
                int64_t char_len;
                uint64_t hash;
                char data[];
            } ProbeStrObject;

            typedef struct {
                PyObjectHeader h;
                int64_t length;
                int64_t capacity;
                PyObject **items;
            } ProbeListObject;

            static int same_id(PyObject *obj, int64_t expected) {
                return obj != 0 && pcc_gc_object_id(obj) == expected;
            }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            PyObject *exc_root = 0;
            pcc_gc_scheduler_root_register(&exc_root);

            PyObject *message = py_list_new(0);
            PyObject *cause = py_list_new(0);
            PyObject *context = py_list_new(0);
            if (message == 0 || cause == 0 || context == 0) return 3;

            PyObject *exc = py_exc_new_with_value(PY_EXC_VALUEERROR, message);
            if (exc == 0) return 4;
            py_exc_set_cause(exc, cause);
            py_exc_set_context(exc, context);
            pcc_gc_store_root(&exc_root, exc);

            ProbeExceptionObject *raw_exc = (ProbeExceptionObject *)exc;
            PyObject *cls_obj = (PyObject *)raw_exc->exc_class;
            if (cls_obj == 0) return 5;
            int64_t class_id = pcc_gc_object_id(cls_obj);
            int64_t message_id = pcc_gc_object_id(message);
            int64_t cause_id = pcc_gc_object_id(cause);
            int64_t context_id = pcc_gc_object_id(context);

            pcc_gc_telemetry_reset();
            if (pcc_gc_select_relocation_set(128) <= 0) return 6;
            if (pcc_gc_relocation_set_contains(cls_obj) != 1) return 7;
            if (pcc_gc_relocation_set_contains(message) != 1) return 8;
            if (pcc_gc_relocation_set_contains(cause) != 1) return 9;
            if (pcc_gc_relocation_set_contains(context) != 1) return 10;
            if (pcc_gc_relocate_copy(cls_obj, (int64_t)sizeof(ProbeClassObject)) == 0) return 11;
            if (pcc_gc_relocate_copy(message, (int64_t)sizeof(ProbeListObject)) == 0) return 12;
            if (pcc_gc_relocate_copy(cause, (int64_t)sizeof(ProbeListObject)) == 0) return 13;
            if (pcc_gc_relocate_copy(context, (int64_t)sizeof(ProbeListObject)) == 0) return 14;

            PyObject *type_name = py_obj_type_name(exc_root);
            PyObject *expected_type_name = py_str_new("ValueError", 10);
            if (type_name == 0 || expected_type_name == 0) return 15;
            if (py_str_eq(type_name, expected_type_name) != 1) return 16;

            PyObject *got_class = py_type_builtin(exc_root);
            PyObject *got_value = py_obj_getattr(exc_root, "value");
            PyObject *got_cause = py_obj_getattr(exc_root, "__cause__");
            PyObject *got_context = py_obj_getattr(exc_root, "__context__");
            if (!same_id(got_class, class_id)) return 17;
            if (!same_id(got_value, message_id)) return 18;
            if (!same_id(got_cause, cause_id)) return 19;
            if (!same_id(got_context, context_id)) return 20;

            ProbeExceptionObject *loaded_exc = (ProbeExceptionObject *)pcc_gc_load_ptr(0, &exc_root);
            if (loaded_exc == 0) return 21;
            if ((PyObject *)loaded_exc->exc_class == cls_obj) return 22;
            if (loaded_exc->message == message) return 23;
            if (loaded_exc->cause == cause) return 24;
            if (loaded_exc->context == context) return 25;
            if (pcc_gc_backend4_verify_no_old_addresses() != 1) return 26;

            pcc_gc_release(got_context);
            pcc_gc_release(got_cause);
            pcc_gc_release(got_value);
            pcc_gc_release(got_class);
            pcc_gc_release(expected_type_name);
            pcc_gc_release(type_name);
            pcc_gc_store_root(&exc_root, 0);
            pcc_gc_scheduler_root_unregister(&exc_root);
            printf("backend4-obj-dispatch-exc-slots-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-obj-dispatch-exc-slots-ok"


def test_backend4_obj_dispatch_loads_forwarded_instance_class_slot(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        typedef struct ProbeClassObject {
            PyObjectHeader h;
            const char *name;
            int32_t n_bases;
            struct ProbeClassObject **bases;
            int32_t n_mro;
            struct ProbeClassObject **mro;
            int32_t n_methods;
            void *methods;
            int32_t n_fields;
            const char **field_names;
            int32_t instance_size;
            int32_t type_tag_alloc;
            PyObject *del_method;
            PyObject *attrs;
        } ProbeClassObject;

        typedef struct {
            PyObjectHeader h;
            ProbeClassObject *cls;
            PyObject *fields[1];
        } ProbeInstanceObject;

        extern ProbeClassObject *py_class_new(
            const char *name,
            ProbeClassObject **bases,
            int32_t n_bases,
            const char **field_names,
            int32_t n_fields
        );
        extern PyObject *py_instance_new(ProbeClassObject *cls);

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            PyObject *inst_root = 0;
            pcc_gc_scheduler_root_register(&inst_root);

            const char *fields[1] = {"value"};
            ProbeClassObject *cls = py_class_new("MovedClass", 0, 0, fields, 1);
            if (cls == 0) return 3;
            cls->instance_size = (int32_t)sizeof(ProbeInstanceObject);
            PyObject *inst = py_instance_new(cls);
            if (inst == 0) return 4;
            int64_t class_id = pcc_gc_object_id((PyObject *)cls);
            pcc_gc_store_root(&inst_root, inst);

            pcc_gc_telemetry_reset();
            if (pcc_gc_select_relocation_set(128) <= 0) return 5;
            if (pcc_gc_relocation_set_contains((PyObject *)cls) != 1) return 6;
            if (pcc_gc_relocate_copy((PyObject *)cls, (int64_t)sizeof(ProbeClassObject)) == 0) return 7;

            PyObject *type_name = py_obj_type_name(inst_root);
            PyObject *expected_type_name = py_str_new("MovedClass", 10);
            if (type_name == 0 || expected_type_name == 0) return 8;
            if (py_str_eq(type_name, expected_type_name) != 1) return 9;

            PyObject *got_class = py_type_builtin(inst_root);
            if (got_class == 0) return 10;
            if (pcc_gc_object_id(got_class) != class_id) return 11;

            ProbeInstanceObject *loaded_inst = (ProbeInstanceObject *)pcc_gc_load_ptr(0, &inst_root);
            if (loaded_inst == 0) return 12;
            if ((PyObject *)loaded_inst->cls == (PyObject *)cls) return 13;
            if (pcc_gc_backend4_verify_no_old_addresses() != 1) return 14;

            pcc_gc_release(got_class);
            pcc_gc_release(expected_type_name);
            pcc_gc_release(type_name);
            pcc_gc_store_root(&inst_root, 0);
            pcc_gc_scheduler_root_unregister(&inst_root);
            printf("backend4-obj-dispatch-instance-class-slot-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-obj-dispatch-instance-class-slot-ok"


def test_backend4_list_get_loads_forwarded_item_slot(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            PyObject *lst = py_list_new(4);
            PyObject *value = py_list_new(0);
            if (lst == 0 || value == 0) return 3;
            py_list_append(lst, value);
            int64_t value_id = pcc_gc_object_id(value);

            pcc_gc_telemetry_reset();
            if (pcc_gc_select_relocation_set(64) <= 0) return 4;
            if (pcc_gc_relocation_set_contains(value) != 1) return 5;
            PyObject *moved_value_raw = pcc_gc_relocate_copy(
                value,
                (int64_t)sizeof(ProbeListObject)
            );
            if (moved_value_raw == 0) return 6;
            pcc_gc_release(moved_value_raw);

            PyObject *got = py_list_get(lst, 0);
            if (got == 0) return 7;
            if (got == value) return 8;
            if (pcc_gc_object_id(got) != value_id) return 9;
            if (((ProbeListObject *)lst)->items[0] == value) return 10;

            pcc_gc_release(got);
            pcc_gc_release(value);
            pcc_gc_release(lst);
            printf("backend4-list-get-forwarded-item-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-list-get-forwarded-item-ok"


def test_backend4_capi_internal_owner_slots_trace_and_load_forwarded_values(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_internal.h"
        #include <stdio.h>

        typedef long Py_ssize_t;

        PyObject *PySeqIter_New(PyObject *seq);
        PyObject *PyIter_Next(PyObject *iter);
        PyObject *PyContextVar_New(const char *name, PyObject *def);
        int PyContextVar_Get(
            PyObject *var,
            PyObject *default_value,
            PyObject **value
        );
        PyObject *PySlice_New(
            PyObject *start,
            PyObject *stop,
            PyObject *step
        );

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        typedef struct {
            int count;
            PyObject **slots[3];
        } ProbeSlots;

        static void capture_owned_slot(
            PyObject **slot,
            int32_t role,
            void *ctx
        ) {
            ProbeSlots *probe = (ProbeSlots *)ctx;
            if (role != PY_OBJ_SLOT_OWNED || probe->count >= 3) return;
            probe->slots[probe->count++] = slot;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            PyObject *seq = py_list_new(0);
            PyObject *seq_item = py_int_from_i64(42);
            PyObject *default_value = py_list_new(0);
            if (seq == 0 || seq_item == 0 || default_value == 0) return 3;
            py_list_append(seq, seq_item);

            PyObject *iter = PySeqIter_New(seq);
            PyObject *contextvar = PyContextVar_New("owner_slots", default_value);
            PyObject *slice = PySlice_New(
                py_int_from_i64(1),
                py_int_from_i64(5),
                py_None
            );
            if (iter == 0 || contextvar == 0 || slice == 0) return 4;

            ProbeSlots iter_slots = {0};
            ProbeSlots context_slots = {0};
            ProbeSlots slice_slots = {0};
            if (!pcc_capi_visit_cext_object_slots(
                    iter, capture_owned_slot, &iter_slots
                )) return 5;
            if (!pcc_capi_visit_cext_object_slots(
                    contextvar, capture_owned_slot, &context_slots
                )) return 6;
            if (!pcc_capi_visit_cext_object_slots(
                    slice, capture_owned_slot, &slice_slots
                )) return 7;
            if (iter_slots.count != 1) return 8;
            if (context_slots.count != 2) return 9;
            if (slice_slots.count != 3) return 10;

            PyObject *seq_root = 0;
            PyObject *iter_root = 0;
            PyObject *default_root = 0;
            PyObject *context_root = 0;
            PyObject *slice_root = 0;
            pcc_gc_scheduler_root_register(&seq_root);
            pcc_gc_scheduler_root_register(&iter_root);
            pcc_gc_scheduler_root_register(&default_root);
            pcc_gc_scheduler_root_register(&context_root);
            pcc_gc_scheduler_root_register(&slice_root);
            pcc_gc_store_root(&seq_root, seq);
            pcc_gc_store_root(&iter_root, iter);
            pcc_gc_store_root(&default_root, default_value);
            pcc_gc_store_root(&context_root, contextvar);
            pcc_gc_store_root(&slice_root, slice);
            pcc_gc_release(seq);
            pcc_gc_release(iter);
            pcc_gc_release(default_value);
            pcc_gc_release(contextvar);
            pcc_gc_release(slice);

            int64_t seq_id = pcc_gc_object_id(seq);
            int64_t default_id = pcc_gc_object_id(default_value);
            if (pcc_gc_select_relocation_set(128) <= 0) return 11;
            if (!pcc_gc_relocation_set_contains(seq)) return 12;
            if (!pcc_gc_relocation_set_contains(default_value)) return 13;
            PyObject *moved_seq = pcc_gc_relocate_copy(
                seq,
                (int64_t)sizeof(ProbeListObject)
            );
            PyObject *moved_default = pcc_gc_relocate_copy(
                default_value,
                (int64_t)sizeof(ProbeListObject)
            );
            if (moved_seq == 0 || moved_default == 0) return 14;
            pcc_gc_release(moved_seq);
            pcc_gc_release(moved_default);

            PyObject *item = PyIter_Next(iter_root);
            int overflow = 0;
            if (
                item == 0
                || py_int_to_i64(item, &overflow) != 42
                || overflow
            ) return 15;
            pcc_gc_release(item);
            if (*iter_slots.slots[0] == seq) return 16;
            if (pcc_gc_object_id(*iter_slots.slots[0]) != seq_id) return 17;

            PyObject *got_default = 0;
            if (PyContextVar_Get(context_root, 0, &got_default) != 0) return 18;
            if (got_default == 0 || got_default == default_value) return 19;
            if (pcc_gc_object_id(got_default) != default_id) return 20;
            if (*context_slots.slots[0] == default_value) return 21;
            pcc_gc_release(got_default);

            (void)pcc_gc_load_ptr(0, &seq_root);
            (void)pcc_gc_load_ptr(0, &default_root);
            if (!pcc_gc_backend4_verify_no_old_addresses()) return 22;

            pcc_gc_store_root(&slice_root, 0);
            pcc_gc_store_root(&context_root, 0);
            pcc_gc_store_root(&default_root, 0);
            pcc_gc_store_root(&iter_root, 0);
            pcc_gc_store_root(&seq_root, 0);
            pcc_gc_scheduler_root_unregister(&slice_root);
            pcc_gc_scheduler_root_unregister(&context_root);
            pcc_gc_scheduler_root_unregister(&default_root);
            pcc_gc_scheduler_root_unregister(&iter_root);
            pcc_gc_scheduler_root_unregister(&seq_root);
            printf("backend4-capi-owner-slots-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-capi-owner-slots-ok"


def test_backend4_list_concat_loads_forwarded_item_slot(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            PyObject *left = py_list_new(4);
            PyObject *right = py_list_new(4);
            PyObject *value = py_list_new(0);
            if (left == 0 || right == 0 || value == 0) return 3;
            py_list_append(left, value);
            int64_t value_id = pcc_gc_object_id(value);

            pcc_gc_telemetry_reset();
            if (pcc_gc_select_relocation_set(64) <= 0) return 4;
            if (pcc_gc_relocation_set_contains(value) != 1) return 5;
            PyObject *moved_value_raw = pcc_gc_relocate_copy(
                value,
                (int64_t)sizeof(ProbeListObject)
            );
            if (moved_value_raw == 0) return 6;
            pcc_gc_release(moved_value_raw);

            PyObject *out = py_list_concat(left, right);
            if (out == 0) return 7;
            ProbeListObject *left_l = (ProbeListObject *)left;
            ProbeListObject *out_l = (ProbeListObject *)out;
            if (left_l->items[0] == value) return 8;
            if (out_l->items[0] == value) return 9;
            if (pcc_gc_object_id(left_l->items[0]) != value_id) return 10;
            if (pcc_gc_object_id(out_l->items[0]) != value_id) return 11;

            pcc_gc_release(out);
            pcc_gc_release(value);
            pcc_gc_release(right);
            pcc_gc_release(left);
            printf("backend4-list-concat-forwarded-item-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-list-concat-forwarded-item-ok"


def test_backend4_list_mutations_load_forwarded_item_slots(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

            static PyObject *relocate_rooted_box(PyObject **root_slot, PyObject *old_value) {
                if (pcc_gc_select_relocation_set(128) <= 0) return 0;
                if (pcc_gc_relocation_set_contains(*root_slot) != 1) return 0;
                PyObject *moved_raw = pcc_gc_relocate_copy(
                    *root_slot,
                    (int64_t)sizeof(ProbeListObject)
                );
                if (moved_raw == 0) return 0;
            pcc_gc_release(moved_raw);
            PyObject *loaded = pcc_gc_load_ptr(0, root_slot);
            if (loaded == 0 || loaded == old_value) return 0;
            return loaded;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            PyObject *value_root = 0;
            pcc_gc_scheduler_root_register(&value_root);

            PyObject *pop_list = py_list_new(0);
            PyObject *remove_list = py_list_new(0);
            PyObject *clear_list = py_list_new(0);
            PyObject *reverse_list = py_list_new(0);
            PyObject *old_value = py_list_new(0);
            PyObject *other = py_int_from_i64(7);
            if (pop_list == 0 || remove_list == 0 || clear_list == 0 || reverse_list == 0 || old_value == 0 || other == 0) {
                return 3;
            }

            pcc_gc_store_root(&value_root, old_value);
            py_list_append(pop_list, old_value);
            py_list_append(remove_list, old_value);
            py_list_append(clear_list, old_value);
            py_list_append(reverse_list, old_value);
            py_list_append(reverse_list, other);
            pcc_gc_release(old_value);

            PyObject *value = relocate_rooted_box(&value_root, old_value);
            if (value == 0) return 10;

            PyObject *popped = py_list_pop(pop_list, 0);
            if (popped == 0) return 4;
            if (popped != value) return 5;
            py_decref(popped);

            py_list_remove(remove_list, value);
            if (((ProbeListObject *)remove_list)->length != 0) return 6;

            py_list_clear(clear_list);
            if (((ProbeListObject *)clear_list)->length != 0) return 7;

            py_list_reverse(reverse_list);
            ProbeListObject *rl = (ProbeListObject *)reverse_list;
            if (rl->items[1] != value) return 8;
            if (pcc_gc_backend4_verify_no_old_addresses() != 1) return 9;

            py_decref(other);
            py_decref(reverse_list);
            py_decref(clear_list);
            py_decref(remove_list);
            py_decref(pop_list);
            pcc_gc_store_root(&value_root, 0);
            pcc_gc_scheduler_root_unregister(&value_root);
            printf("backend4-list-mutation-barriers-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-list-mutation-barriers-ok"


def test_backend4_tuple_get_loads_forwarded_item_slot(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

            typedef struct {
                PyObjectHeader h;
                int64_t len;
                PyObject *items[1];
            } ProbeTupleObject;

            typedef struct {
                PyObjectHeader h;
                int64_t length;
                int64_t capacity;
                PyObject **items;
            } ProbeListObject;

            int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            PyObject *tuple = py_tuple_new(1);
            PyObject *value = py_list_new(0);
            if (tuple == 0 || value == 0) return 3;
            py_tuple_set_item(tuple, 0, value);
            int64_t value_id = pcc_gc_object_id(value);

            pcc_gc_telemetry_reset();
            if (pcc_gc_select_relocation_set(64) <= 0) return 4;
            if (pcc_gc_relocation_set_contains(value) != 1) return 5;
            PyObject *moved_value_raw = pcc_gc_relocate_copy(
                value,
                (int64_t)sizeof(ProbeListObject)
            );
            if (moved_value_raw == 0) return 6;
            pcc_gc_release(moved_value_raw);

            PyObject *got = py_tuple_get(tuple, 0);
            if (got == 0) return 7;
            if (got == value) return 8;
            if (pcc_gc_object_id(got) != value_id) return 9;
            if (((ProbeTupleObject *)tuple)->items[0] == value) return 10;

            pcc_gc_release(got);
            pcc_gc_release(value);
            pcc_gc_release(tuple);
            printf("backend4-tuple-get-forwarded-item-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-tuple-get-forwarded-item-ok"


def test_backend4_tuple_concat_loads_forwarded_item_slot(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

            typedef struct {
                PyObjectHeader h;
                int64_t len;
                PyObject *items[1];
            } ProbeTupleObject;

            typedef struct {
                PyObjectHeader h;
                int64_t length;
                int64_t capacity;
                PyObject **items;
            } ProbeListObject;

            int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            PyObject *left = py_tuple_new(1);
            PyObject *right = py_tuple_new(0);
            PyObject *value = py_list_new(0);
            if (left == 0 || right == 0 || value == 0) return 3;
            py_tuple_set_item(left, 0, value);
            int64_t value_id = pcc_gc_object_id(value);

            pcc_gc_telemetry_reset();
            if (pcc_gc_select_relocation_set(64) <= 0) return 4;
            if (pcc_gc_relocation_set_contains(value) != 1) return 5;
            PyObject *moved_value_raw = pcc_gc_relocate_copy(
                value,
                (int64_t)sizeof(ProbeListObject)
            );
            if (moved_value_raw == 0) return 6;
            pcc_gc_release(moved_value_raw);

            PyObject *out = py_tuple_concat(left, right);
            if (out == 0) return 7;
            ProbeTupleObject *left_t = (ProbeTupleObject *)left;
            ProbeTupleObject *out_t = (ProbeTupleObject *)out;
            if (left_t->items[0] == value) return 8;
            if (out_t->items[0] == value) return 9;
            if (pcc_gc_object_id(left_t->items[0]) != value_id) return 10;
            if (pcc_gc_object_id(out_t->items[0]) != value_id) return 11;

            pcc_gc_release(out);
            pcc_gc_release(value);
            pcc_gc_release(right);
            pcc_gc_release(left);
            printf("backend4-tuple-concat-forwarded-item-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-tuple-concat-forwarded-item-ok"


def test_backend4_relocation_preserves_descriptor_slots(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        typedef struct {
            PyObjectHeader h;
            PyObject *fget;
            PyObject *fset;
            PyObject *fdel;
        } ProbePropertyObject;

        typedef struct {
            PyObjectHeader h;
            PyObject *func;
        } ProbeClassMethodObject;

        typedef struct {
            PyObjectHeader h;
            PyObject *func;
        } ProbeStaticMethodObject;

        static int64_t resolved_id(PyObject *obj) {
            PyObject *resolved = pcc_gc_note_relocation_read(obj);
            if (resolved == 0) return -1;
            return pcc_gc_object_id(resolved);
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            PyObject *prop_root = 0;
            PyObject *cm_root = 0;
            PyObject *sm_root = 0;
            pcc_gc_scheduler_root_register(&prop_root);
            pcc_gc_scheduler_root_register(&cm_root);
            pcc_gc_scheduler_root_register(&sm_root);

            PyObject *fget = py_str_new("fget", 4);
            PyObject *fset = py_str_new("fset", 4);
            PyObject *fdel = py_str_new("fdel", 4);
            PyObject *cm_func = py_str_new("cm", 2);
            PyObject *sm_func = py_str_new("sm", 2);
            if (fget == 0 || fset == 0 || fdel == 0 || cm_func == 0 || sm_func == 0) return 3;
            int64_t fget_id = pcc_gc_object_id(fget);
            int64_t fset_id = pcc_gc_object_id(fset);
            int64_t fdel_id = pcc_gc_object_id(fdel);
            int64_t cm_id = pcc_gc_object_id(cm_func);
            int64_t sm_id = pcc_gc_object_id(sm_func);

            ProbePropertyObject *prop = (ProbePropertyObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbePropertyObject),
                PY_TYPE_PROPERTY,
                0x100
            );
            ProbeClassMethodObject *cm = (ProbeClassMethodObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbeClassMethodObject),
                PY_TYPE_CLASSMETHOD,
                0x100
            );
            ProbeStaticMethodObject *sm = (ProbeStaticMethodObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbeStaticMethodObject),
                PY_TYPE_STATICMETHOD,
                0x100
            );
            if (prop == 0 || cm == 0 || sm == 0) return 4;
            prop->fget = fget;
            prop->fset = fset;
            prop->fdel = fdel;
            cm->func = cm_func;
            sm->func = sm_func;
            pcc_gc_store_root(&prop_root, (PyObject *)prop);
            pcc_gc_store_root(&cm_root, (PyObject *)cm);
            pcc_gc_store_root(&sm_root, (PyObject *)sm);

            pcc_gc_telemetry_reset();
            pcc_gc_publish_initialized((PyObject *)prop);
            pcc_gc_publish_initialized((PyObject *)cm);
            pcc_gc_publish_initialized((PyObject *)sm);
            if (pcc_gc_select_relocation_set(64) <= 0) return 5;
            if (pcc_gc_relocation_set_contains((PyObject *)prop) != 1) return 6;
            if (pcc_gc_relocation_set_contains((PyObject *)cm) != 1) return 7;
            if (pcc_gc_relocation_set_contains((PyObject *)sm) != 1) return 8;
            if (pcc_gc_relocate_copy((PyObject *)prop, (int64_t)sizeof(ProbePropertyObject)) == 0) return 9;
            if (pcc_gc_relocate_copy((PyObject *)cm, (int64_t)sizeof(ProbeClassMethodObject)) == 0) return 10;
            if (pcc_gc_relocate_copy((PyObject *)sm, (int64_t)sizeof(ProbeStaticMethodObject)) == 0) return 11;

            ProbePropertyObject *moved_prop = (ProbePropertyObject *)pcc_gc_load_ptr(0, &prop_root);
            ProbeClassMethodObject *moved_cm = (ProbeClassMethodObject *)pcc_gc_load_ptr(0, &cm_root);
            ProbeStaticMethodObject *moved_sm = (ProbeStaticMethodObject *)pcc_gc_load_ptr(0, &sm_root);
            if (moved_prop == 0 || moved_cm == 0 || moved_sm == 0) return 12;
            if ((PyObject *)moved_prop == (PyObject *)prop) return 13;
            if ((PyObject *)moved_cm == (PyObject *)cm) return 14;
            if ((PyObject *)moved_sm == (PyObject *)sm) return 15;
            if (resolved_id(moved_prop->fget) != fget_id) return 16;
            if (resolved_id(moved_prop->fset) != fset_id) return 17;
            if (resolved_id(moved_prop->fdel) != fdel_id) return 18;
            if (resolved_id(moved_cm->func) != cm_id) return 19;
            if (resolved_id(moved_sm->func) != sm_id) return 20;

            pcc_gc_store_root(&prop_root, 0);
            pcc_gc_store_root(&cm_root, 0);
            pcc_gc_store_root(&sm_root, 0);
            pcc_gc_scheduler_root_unregister(&prop_root);
            pcc_gc_scheduler_root_unregister(&cm_root);
            pcc_gc_scheduler_root_unregister(&sm_root);
            printf("backend4-descriptor-relocation-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-descriptor-relocation-ok"


def test_backend4_relocation_preserves_memoryview_base(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        typedef struct {
            PyObjectHeader h;
            PyObject *base;
        } ProbeMemoryViewObject;

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            PyObject *root = 0;
            pcc_gc_scheduler_root_register(&root);

            PyObject *base = py_str_new("memoryview-base", 15);
            if (base == 0) return 3;
            int64_t base_id = pcc_gc_object_id(base);

            ProbeMemoryViewObject *mv = (ProbeMemoryViewObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbeMemoryViewObject),
                PY_TYPE_MEMORYVIEW,
                0x100
            );
            if (mv == 0) return 4;
            mv->base = base;
            pcc_gc_store_root(&root, (PyObject *)mv);

            pcc_gc_telemetry_reset();
            pcc_gc_publish_initialized((PyObject *)mv);
            if (pcc_gc_select_relocation_set(64) <= 0) return 5;
            if (pcc_gc_relocation_set_contains((PyObject *)mv) != 1) return 6;
            if (pcc_gc_relocate_copy((PyObject *)mv, (int64_t)sizeof(ProbeMemoryViewObject)) == 0) return 7;

            ProbeMemoryViewObject *moved = (ProbeMemoryViewObject *)pcc_gc_load_ptr(0, &root);
            if (moved == 0) return 8;
            if ((PyObject *)moved == (PyObject *)mv) return 9;
            PyObject *resolved_base = pcc_gc_note_relocation_read(moved->base);
            if (resolved_base == 0) return 10;
            if (pcc_gc_object_id(resolved_base) != base_id) return 11;

            pcc_gc_store_root(&root, 0);
            pcc_gc_scheduler_root_unregister(&root);
            printf("backend4-memoryview-relocation-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-memoryview-relocation-ok"


def test_backend4_memoryview_loads_forwarded_base_slot(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        typedef struct {
            PyObjectHeader h;
            PyObject *base;
        } ProbeMemoryViewObject;

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            PyObject *base_root = 0;
            PyObject *mv_root = 0;
            pcc_gc_scheduler_root_register(&base_root);
            pcc_gc_scheduler_root_register(&mv_root);

            PyObject *base = py_list_new(0);
            PyObject *mv = py_memoryview_new(base);
            if (base == 0 || mv == 0) return 3;
            int64_t base_id = pcc_gc_object_id(base);
            pcc_gc_store_root(&base_root, base);
            pcc_gc_store_root(&mv_root, mv);
            pcc_gc_release(base);
            pcc_gc_release(mv);

            pcc_gc_telemetry_reset();
            if (pcc_gc_select_relocation_set(64) <= 0) return 4;
            if (pcc_gc_relocation_set_contains(base_root) != 1) return 5;
            PyObject *moved_base_raw = pcc_gc_relocate_copy(
                base_root,
                (int64_t)sizeof(ProbeListObject)
            );
            if (moved_base_raw == 0) return 6;
            pcc_gc_release(moved_base_raw);

            PyObject *loaded_base = pcc_gc_load_ptr(0, &base_root);
            if (loaded_base == 0) return 7;
            if (loaded_base == base) return 8;

            ProbeMemoryViewObject *loaded_mv = (ProbeMemoryViewObject *)pcc_gc_load_ptr(0, &mv_root);
            if (loaded_mv == 0) return 11;
            PyObject *loaded_mv_base = pcc_gc_load_ptr(
                (PyObject *)loaded_mv,
                &loaded_mv->base
            );
            if (loaded_mv_base == base) return 12;
            if (pcc_gc_object_id(loaded_mv_base) != base_id) return 13;
            if (pcc_gc_backend4_verify_no_old_addresses() != 1) return 14;

            pcc_gc_store_root(&mv_root, 0);
            pcc_gc_store_root(&base_root, 0);
            pcc_gc_scheduler_root_unregister(&mv_root);
            pcc_gc_scheduler_root_unregister(&base_root);
            printf("backend4-memoryview-base-barrier-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-memoryview-base-barrier-ok"


def test_backend4_libpython_bridge_memoryview_base_uses_read_barrier():
    src = (RUNTIME_DIR / "src" / "py_libpython.c").read_text(encoding="utf-8")
    branch = src.split("case PY_TYPE_MEMORYVIEW:", 1)[1]
    branch = branch.split("case PY_TYPE_LIST:", 1)[0]
    assert "pcc_gc_load_ptr(o, &m->base)" in branch
    assert "py_cpy_from_pcc_obj(m->base)" not in branch


def test_backend4_container_dealloc_loads_forwarded_reference_slots(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        typedef struct {
            PyObjectHeader h;
            int64_t len;
            PyObject *items[1];
        } ProbeTupleObject;

            static int relocate_value(PyObject **root_slot) {
                if (pcc_gc_select_relocation_set(128) <= 0) return 0;
                if (pcc_gc_relocation_set_contains(*root_slot) != 1) return 0;
                PyObject *moved_raw = pcc_gc_relocate_copy(
                    *root_slot,
                    (int64_t)sizeof(ProbeTupleObject)
                );
                if (moved_raw == 0) return 0;
            pcc_gc_release(moved_raw);
            return 1;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            PyObject *value_root = 0;
            pcc_gc_scheduler_root_register(&value_root);

            PyObject *list = py_list_new(0);
            PyObject *tuple = py_tuple_new(1);
            PyObject *dict = py_dict_new();
            PyObject *set = py_set_new();
            PyObject *value = py_tuple_new(1);
            PyObject *key = py_str_new("k", 1);
            if (list == 0 || tuple == 0 || dict == 0 || set == 0 || value == 0 || key == 0) return 3;
            py_tuple_set_item(value, 0, py_None);

            py_list_append(list, value);
            py_tuple_set_item(tuple, 0, value);
            py_dict_set(dict, key, value);
            py_set_add(set, value);
            pcc_gc_store_root(&value_root, value);
            pcc_gc_release(value);
            pcc_gc_release(key);

            pcc_gc_telemetry_reset();
            if (!relocate_value(&value_root)) return 4;
            PyObject *loaded_value = pcc_gc_load_ptr(0, &value_root);
            if (loaded_value == 0) return 5;
            if (loaded_value == value) return 6;

            pcc_gc_telemetry_reset();
            pcc_gc_release(list);
            pcc_gc_release(tuple);
            pcc_gc_release(dict);
            pcc_gc_release(set);
            if (pcc_gc_telemetry(PCC_GC_COUNTER_READ_BARRIERS) < 5) return 7;

            pcc_gc_store_root(&value_root, 0);
            pcc_gc_scheduler_root_unregister(&value_root);
            printf("backend4-container-dealloc-barriers-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-container-dealloc-barriers-ok"


def test_backend4_relocation_preserves_func_entry_and_captures(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_internal.h"
        #include <stdio.h>

        typedef PyObject *(*ProbeFuncEntry)(PyObject *captures, PyObject *args);

        static PyObject *probe_entry(PyObject *captures, PyObject *args) {
            (void)args;
            return py_tuple_get(captures, 0);
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            PyObject *root = 0;
            pcc_gc_scheduler_root_register(&root);

            PyObject *capture = py_str_new("capture", 7);
            PyObject *captures = py_tuple_new(1);
            if (capture == 0 || captures == 0) return 3;
            py_tuple_set_item(captures, 0, capture);

            PyObject *fn = py_func_new((void *)probe_entry, captures);
            if (fn == 0) return 4;
            pcc_gc_store_root(&root, fn);
            pcc_gc_release(fn);
            pcc_gc_release(captures);
            pcc_gc_release(capture);

            pcc_gc_telemetry_reset();
            if (pcc_gc_select_relocation_set(64) <= 0) return 5;
            if (pcc_gc_relocation_set_contains(root) != 1) return 6;
            PyObject *moved_raw = pcc_gc_relocate_copy(root, (int64_t)sizeof(PyFuncObject));
            if (moved_raw == 0) return 7;
            pcc_gc_release(moved_raw);

            PyObject *loaded = pcc_gc_load_ptr(0, &root);
            if (loaded == 0) return 8;
            if (loaded == fn) return 9;
            PyFuncObject *moved = (PyFuncObject *)loaded;
            if (moved->entry != probe_entry) return 10;
            if (moved->captures == 0) return 11;

            PyObject *args = py_tuple_new(0);
            PyObject *result = py_func_call(loaded, args);
            PyObject *expected = py_str_new("capture", 7);
            if (args == 0 || result == 0 || expected == 0) return 12;
            if (py_str_eq(result, expected) != 1) return 13;
            if (pcc_gc_backend4_verify_no_old_addresses() != 1) return 14;

            pcc_gc_release(expected);
            pcc_gc_release(result);
            pcc_gc_release(args);
            pcc_gc_store_root(&root, 0);
            pcc_gc_scheduler_root_unregister(&root);
            printf("backend4-func-relocation-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-func-relocation-ok"


def test_backend4_func_call_loads_forwarded_captures_slot(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_internal.h"
        #include <stdio.h>

        typedef PyObject *(*ProbeFuncEntry)(PyObject *captures, PyObject *args);

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            PyObject *items[];
        } ProbeTupleObject;

        static PyObject *probe_entry(PyObject *captures, PyObject *args) {
            (void)args;
            return py_tuple_get(captures, 0);
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            PyObject *fn_root = 0;
            PyObject *captures_root = 0;
            pcc_gc_scheduler_root_register(&fn_root);
            pcc_gc_scheduler_root_register(&captures_root);

            PyObject *capture = py_str_new("capture", 7);
            PyObject *captures = py_tuple_new(1);
            if (capture == 0 || captures == 0) return 3;
            py_tuple_set_item(captures, 0, capture);

            PyObject *fn = py_func_new((void *)probe_entry, captures);
            if (fn == 0) return 4;
            pcc_gc_store_root(&fn_root, fn);
            pcc_gc_store_root(&captures_root, captures);
            pcc_gc_release(fn);
            pcc_gc_release(captures);
            pcc_gc_release(capture);

            pcc_gc_telemetry_reset();
            if (pcc_gc_select_relocation_set(128) <= 0) return 5;
            if (pcc_gc_relocation_set_contains(captures_root) != 1) return 6;
            PyObject *moved_captures_raw = pcc_gc_relocate_copy(
                captures_root,
                (int64_t)sizeof(ProbeTupleObject) + (int64_t)sizeof(PyObject *)
            );
            if (moved_captures_raw == 0) return 7;
            pcc_gc_release(moved_captures_raw);

            PyObject *loaded_captures = pcc_gc_load_ptr(0, &captures_root);
            if (loaded_captures == 0) return 8;
            if (loaded_captures == captures) return 9;

            PyObject *args = py_tuple_new(0);
            PyObject *result = py_func_call(fn_root, args);
            PyObject *expected = py_str_new("capture", 7);
            if (args == 0 || result == 0 || expected == 0) return 10;
            if (py_str_eq(result, expected) != 1) return 11;

            PyFuncObject *loaded_fn = (PyFuncObject *)pcc_gc_load_ptr(0, &fn_root);
            if (loaded_fn == 0) return 12;
            if (loaded_fn->captures != loaded_captures) return 13;
            if (pcc_gc_backend4_verify_no_old_addresses() != 1) return 14;

            pcc_gc_release(expected);
            pcc_gc_release(result);
            pcc_gc_release(args);
            pcc_gc_store_root(&captures_root, 0);
            pcc_gc_store_root(&fn_root, 0);
            pcc_gc_scheduler_root_unregister(&captures_root);
            pcc_gc_scheduler_root_unregister(&fn_root);
            printf("backend4-func-captures-barrier-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-func-captures-barrier-ok"


def test_backend4_func_relocation_loads_forwarded_captures_slot(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_internal.h"
        #include <stdio.h>

        typedef PyObject *(*ProbeFuncEntry)(PyObject *captures, PyObject *args);

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            PyObject *items[];
        } ProbeTupleObject;

        static PyObject *probe_entry(PyObject *captures, PyObject *args) {
            (void)args;
            return py_tuple_get(captures, 0);
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            PyObject *fn_root = 0;
            PyObject *captures_root = 0;
            pcc_gc_scheduler_root_register(&fn_root);
            pcc_gc_scheduler_root_register(&captures_root);

            PyObject *capture = py_str_new("capture", 7);
            PyObject *captures = py_tuple_new(1);
            if (capture == 0 || captures == 0) return 3;
            py_tuple_set_item(captures, 0, capture);

            PyObject *fn = py_func_new((void *)probe_entry, captures);
            if (fn == 0) return 4;
            pcc_gc_store_root(&fn_root, fn);
            pcc_gc_store_root(&captures_root, captures);
            pcc_gc_release(fn);
            pcc_gc_release(captures);
            pcc_gc_release(capture);

            if (pcc_gc_select_relocation_set(128) <= 0) return 5;
            PyObject *moved_captures_raw = pcc_gc_relocate_copy(
                captures_root,
                (int64_t)sizeof(ProbeTupleObject) + (int64_t)sizeof(PyObject *)
            );
            if (moved_captures_raw == 0) return 6;
            pcc_gc_release(moved_captures_raw);

            PyObject *loaded_captures = pcc_gc_load_ptr(0, &captures_root);
            if (loaded_captures == 0) return 7;
            if (loaded_captures == captures) return 8;

            pcc_gc_telemetry_reset();
            PyObject *moved_fn_raw = pcc_gc_relocate_copy(
                fn_root,
                (int64_t)sizeof(PyFuncObject)
            );
            if (moved_fn_raw == 0) return 9;
            pcc_gc_release(moved_fn_raw);

            PyObject *loaded_fn_obj = pcc_gc_load_ptr(0, &fn_root);
            if (loaded_fn_obj == 0) return 10;
            if (loaded_fn_obj == fn) return 11;
            PyFuncObject *loaded_fn = (PyFuncObject *)loaded_fn_obj;
            if (loaded_fn->entry != probe_entry) return 12;
            if (loaded_fn->captures != loaded_captures) return 13;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_READ_BARRIERS) < 1) return 14;

            PyObject *args = py_tuple_new(0);
            PyObject *result = py_func_call(loaded_fn_obj, args);
            PyObject *expected = py_str_new("capture", 7);
            if (args == 0 || result == 0 || expected == 0) return 15;
            if (py_str_eq(result, expected) != 1) return 16;
            if (pcc_gc_backend4_verify_no_old_addresses() != 1) return 17;

            pcc_gc_release(expected);
            pcc_gc_release(result);
            pcc_gc_release(args);
            pcc_gc_store_root(&captures_root, 0);
            pcc_gc_store_root(&fn_root, 0);
            pcc_gc_scheduler_root_unregister(&captures_root);
            pcc_gc_scheduler_root_unregister(&fn_root);
            printf("backend4-func-relocation-captures-barrier-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-func-relocation-captures-barrier-ok"


def test_backend4_relocation_preserves_iter_sequence_and_index(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        typedef struct {
            PyObjectHeader h;
            PyObject *seq;
            int64_t index;
        } ProbeIterObject;

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            PyObject *root = 0;
            pcc_gc_scheduler_root_register(&root);

            PyObject *seq = py_list_new(0);
            if (seq == 0) return 3;
            py_list_append(seq, py_int_from_i64(10));
            py_list_append(seq, py_int_from_i64(20));

            ProbeIterObject *it = (ProbeIterObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbeIterObject),
                PY_TYPE_ITER,
                0
            );
            if (it == 0) return 4;
            it->seq = 0;
            it->index = 1;
            pcc_gc_store_ptr((PyObject *)it, &it->seq, seq);
            pcc_gc_store_root(&root, (PyObject *)it);
            pcc_gc_release((PyObject *)it);
            pcc_gc_release(seq);

            pcc_gc_telemetry_reset();
            pcc_gc_publish_initialized((PyObject *)it);
            if (pcc_gc_select_relocation_set(64) <= 0) return 5;
            if (pcc_gc_relocation_set_contains(root) != 1) return 6;
            PyObject *moved_raw = pcc_gc_relocate_copy(
                root,
                (int64_t)sizeof(ProbeIterObject)
            );
            if (moved_raw == 0) return 7;
            pcc_gc_release(moved_raw);

            PyObject *loaded = pcc_gc_load_ptr(0, &root);
            if (loaded == 0) return 8;
            if (loaded == (PyObject *)it) return 9;
            ProbeIterObject *moved = (ProbeIterObject *)loaded;
            if (moved->index != 1) return 10;
            if (moved->seq == 0) return 11;

            PyObject *resolved_seq = pcc_gc_note_relocation_read(moved->seq);
            if (py_list_len(resolved_seq) != 2) return 12;
            PyObject *item = py_list_get(resolved_seq, moved->index);
            if (py_int_to_i64(item, 0) != 20) return 13;
            if (pcc_gc_backend4_verify_no_old_addresses() != 1) return 14;

            pcc_gc_release(item);
            pcc_gc_store_root(&root, 0);
            pcc_gc_scheduler_root_unregister(&root);
            printf("backend4-iter-relocation-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-iter-relocation-ok"


def test_backend4_iter_next_loads_forwarded_sequence_slot(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        typedef struct {
            PyObjectHeader h;
            PyObject **items;
            int64_t length;
            int64_t capacity;
        } ProbeListObject;

        typedef struct {
            PyObjectHeader h;
            PyObject *seq;
            int64_t index;
        } ProbeIterObject;

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            PyObject *seq_root = 0;
            PyObject *it_root = 0;
            pcc_gc_scheduler_root_register(&seq_root);
            pcc_gc_scheduler_root_register(&it_root);

            PyObject *seq = py_list_new(0);
            if (seq == 0) return 3;
            py_list_append(seq, py_int_from_i64(10));
            py_list_append(seq, py_int_from_i64(20));

            PyObject *it = py_obj_iter(seq);
            if (it == 0) return 4;
            pcc_gc_store_root(&seq_root, seq);
            pcc_gc_store_root(&it_root, it);
            pcc_gc_release(seq);
            pcc_gc_release(it);

            pcc_gc_telemetry_reset();
            if (pcc_gc_select_relocation_set(64) <= 0) return 5;
            if (pcc_gc_relocation_set_contains(seq_root) != 1) return 6;
            PyObject *moved_seq_raw = pcc_gc_relocate_copy(
                seq_root,
                (int64_t)sizeof(ProbeListObject)
            );
            if (moved_seq_raw == 0) return 7;
            pcc_gc_release(moved_seq_raw);

            PyObject *loaded_seq = pcc_gc_load_ptr(0, &seq_root);
            if (loaded_seq == 0) return 8;
            if (loaded_seq == seq) return 9;

            PyObject *first = py_obj_next(it_root);
            if (first == 0) return 10;
            if (py_int_to_i64(first, 0) != 10) return 11;
            py_decref(first);

            ProbeIterObject *loaded_it = (ProbeIterObject *)pcc_gc_load_ptr(0, &it_root);
            if (loaded_it == 0) return 12;
            if (loaded_it->seq != loaded_seq) return 13;
            if (loaded_it->index != 1) return 14;

            PyObject *second = py_obj_next(it_root);
            if (second == 0) return 15;
            if (py_int_to_i64(second, 0) != 20) return 16;
            py_decref(second);

            if (pcc_gc_backend4_verify_no_old_addresses() != 1) return 17;

            pcc_gc_store_root(&it_root, 0);
            pcc_gc_store_root(&seq_root, 0);
            pcc_gc_scheduler_root_unregister(&it_root);
            pcc_gc_scheduler_root_unregister(&seq_root);
            printf("backend4-iter-next-seq-barrier-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-iter-next-seq-barrier-ok"


def test_backend4_relocation_preserves_generator_state_and_slots(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        typedef PyObject *(*ProbeGenResume)(PyObject *gen, PyObject *frame);

        typedef struct {
            PyObjectHeader h;
            ProbeGenResume resume;
            PyObject *frame;
            int64_t state;
            int64_t done;
            PyObject *send_value;
        } ProbeGenObject;

        static PyObject *probe_resume(PyObject *gen, PyObject *frame) {
            (void)gen;
            return pcc_gc_retain(frame);
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            PyObject *root = 0;
            pcc_gc_scheduler_root_register(&root);

            PyObject *frame = py_list_new(0);
            PyObject *send_value = py_int_from_i64(42);
            if (frame == 0 || send_value == 0) return 3;
            py_list_append(frame, py_int_from_i64(7));

            ProbeGenObject *gen = (ProbeGenObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbeGenObject),
                PY_TYPE_GEN,
                0
            );
            if (gen == 0) return 4;
            gen->resume = probe_resume;
            gen->frame = 0;
            gen->state = 3;
            gen->done = 0;
            gen->send_value = 0;
            pcc_gc_store_ptr((PyObject *)gen, &gen->frame, frame);
            pcc_gc_store_ptr((PyObject *)gen, &gen->send_value, send_value);
            pcc_gc_store_root(&root, (PyObject *)gen);
            pcc_gc_release((PyObject *)gen);
            pcc_gc_release(frame);
            pcc_gc_release(send_value);

            pcc_gc_telemetry_reset();
            pcc_gc_publish_initialized((PyObject *)gen);
            if (pcc_gc_select_relocation_set(64) <= 0) return 5;
            if (pcc_gc_relocation_set_contains(root) != 1) return 6;
            PyObject *moved_raw = pcc_gc_relocate_copy(
                root,
                (int64_t)sizeof(ProbeGenObject)
            );
            if (moved_raw == 0) return 7;
            pcc_gc_release(moved_raw);

            PyObject *loaded = pcc_gc_load_ptr(0, &root);
            if (loaded == 0) return 8;
            if (loaded == (PyObject *)gen) return 9;
            ProbeGenObject *moved = (ProbeGenObject *)loaded;
            if (moved->resume != probe_resume) return 10;
            if (moved->state != 3) return 11;
            if (moved->done != 0) return 12;
            if (moved->frame == 0 || moved->send_value == 0) return 13;

            PyObject *resolved_frame = pcc_gc_note_relocation_read(moved->frame);
            if (py_list_len(resolved_frame) != 1) return 14;
            PyObject *item = py_list_get(resolved_frame, 0);
            if (py_int_to_i64(item, 0) != 7) return 15;
            if (py_int_to_i64(moved->send_value, 0) != 42) return 16;
            if (pcc_gc_backend4_verify_no_old_addresses() != 1) return 17;

            pcc_gc_release(item);
            pcc_gc_store_root(&root, 0);
            pcc_gc_scheduler_root_unregister(&root);
            printf("backend4-gen-relocation-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-gen-relocation-ok"


def test_backend4_relocation_preserves_coroutine_shell_state(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>
        #include <string.h>

        typedef PyObject *(*ProbeCoroEntry)(PyObject *captures, PyObject *args);

        typedef struct {
            PyObjectHeader h;
            const char *name;
            ProbeCoroEntry entry;
            PyObject *captures;
            PyObject *args;
            PyObject *result;
            int32_t closed;
            int32_t done;
        } ProbeCoroutineObject;

        static PyObject *probe_coro_entry(PyObject *captures, PyObject *args) {
            (void)args;
            return py_tuple_get(captures, 0);
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            PyObject *root = 0;
            pcc_gc_scheduler_root_register(&root);

            PyObject *captures = py_tuple_new(1);
            PyObject *args = py_tuple_new(1);
            PyObject *capture = py_str_new("coro-capture", 12);
            PyObject *arg = py_int_from_i64(5);
            PyObject *result = py_int_from_i64(99);
            if (captures == 0 || args == 0 || capture == 0 || arg == 0 || result == 0) return 3;
            py_tuple_set_item(captures, 0, capture);
            py_tuple_set_item(args, 0, arg);

            ProbeCoroutineObject *coro = (ProbeCoroutineObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbeCoroutineObject),
                PY_TYPE_COROUTINE,
                0
            );
            if (coro == 0) return 4;
            coro->name = "probe-coro";
            coro->entry = probe_coro_entry;
            coro->captures = 0;
            coro->args = 0;
            coro->result = 0;
            coro->closed = 1;
            coro->done = 1;
            pcc_gc_store_ptr((PyObject *)coro, &coro->captures, captures);
            pcc_gc_store_ptr((PyObject *)coro, &coro->args, args);
            pcc_gc_store_ptr((PyObject *)coro, &coro->result, result);
            pcc_gc_store_root(&root, (PyObject *)coro);
            pcc_gc_release((PyObject *)coro);
            pcc_gc_release(captures);
            pcc_gc_release(args);
            pcc_gc_release(capture);
            pcc_gc_release(arg);
            pcc_gc_release(result);

            pcc_gc_telemetry_reset();
            pcc_gc_publish_initialized((PyObject *)coro);
            if (pcc_gc_select_relocation_set(64) <= 0) return 5;
            if (pcc_gc_relocation_set_contains(root) != 1) return 6;
            PyObject *moved_raw = pcc_gc_relocate_copy(
                root,
                (int64_t)sizeof(ProbeCoroutineObject)
            );
            if (moved_raw == 0) return 7;
            pcc_gc_release(moved_raw);

            PyObject *loaded = pcc_gc_load_ptr(0, &root);
            if (loaded == 0) return 8;
            if (loaded == (PyObject *)coro) return 9;
            ProbeCoroutineObject *moved = (ProbeCoroutineObject *)loaded;
            if (strcmp(moved->name, "probe-coro") != 0) return 10;
            if (moved->entry != probe_coro_entry) return 11;
            if (moved->closed != 1 || moved->done != 1) return 12;
            if (moved->captures == 0 || moved->args == 0 || moved->result == 0) return 13;

            PyObject *cap = py_tuple_get(moved->captures, 0);
            PyObject *expected = py_str_new("coro-capture", 12);
            if (cap == 0 || expected == 0) return 14;
            if (py_str_eq(cap, expected) != 1) return 15;
            PyObject *arg_loaded = py_tuple_get(moved->args, 0);
            if (py_int_to_i64(arg_loaded, 0) != 5) return 16;
            if (py_int_to_i64(moved->result, 0) != 99) return 17;
            if (pcc_gc_backend4_verify_no_old_addresses() != 1) return 18;

            pcc_gc_release(arg_loaded);
            pcc_gc_release(expected);
            pcc_gc_release(cap);
            pcc_gc_store_root(&root, 0);
            pcc_gc_scheduler_root_unregister(&root);
            printf("backend4-coroutine-relocation-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-coroutine-relocation-ok"


def test_backend4_relocation_preserves_task_slots_and_done(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        typedef struct {
            PyObjectHeader h;
            PyObject *coro;
            PyObject *result;
            PyObject *waiter;
            int64_t done;
        } ProbeTaskObject;

        static int64_t resolved_id(PyObject *obj) {
            PyObject *resolved = pcc_gc_note_relocation_read(obj);
            if (resolved == 0) return -1;
            return pcc_gc_object_id(resolved);
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            PyObject *root = 0;
            pcc_gc_scheduler_root_register(&root);

            PyObject *coro = py_str_new("task-coro", 9);
            PyObject *result = py_str_new("task-result", 11);
            PyObject *waiter = py_str_new("task-waiter", 11);
            if (coro == 0 || result == 0 || waiter == 0) return 3;
            int64_t coro_id = pcc_gc_object_id(coro);
            int64_t result_id = pcc_gc_object_id(result);
            int64_t waiter_id = pcc_gc_object_id(waiter);

            ProbeTaskObject *task = (ProbeTaskObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbeTaskObject),
                PY_TYPE_TASK,
                0x100
            );
            if (task == 0) return 4;
            task->coro = 0;
            task->result = 0;
            task->waiter = 0;
            task->done = 1;
            pcc_gc_store_ptr((PyObject *)task, &task->coro, coro);
            pcc_gc_store_ptr((PyObject *)task, &task->result, result);
            pcc_gc_store_ptr((PyObject *)task, &task->waiter, waiter);
            pcc_gc_store_root(&root, (PyObject *)task);
            pcc_gc_release((PyObject *)task);
            pcc_gc_release(coro);
            pcc_gc_release(result);
            pcc_gc_release(waiter);

            pcc_gc_telemetry_reset();
            pcc_gc_publish_initialized((PyObject *)task);
            if (pcc_gc_select_relocation_set(64) <= 0) return 5;
            if (pcc_gc_relocation_set_contains(root) != 1) return 6;
            PyObject *moved_raw = pcc_gc_relocate_copy(
                root,
                (int64_t)sizeof(ProbeTaskObject)
            );
            if (moved_raw == 0) return 7;
            pcc_gc_release(moved_raw);

            ProbeTaskObject *moved = (ProbeTaskObject *)pcc_gc_load_ptr(0, &root);
            if (moved == 0) return 8;
            if ((PyObject *)moved == (PyObject *)task) return 9;
            if (moved->done != 1) return 10;
            if (resolved_id(moved->coro) != coro_id) return 11;
            if (resolved_id(moved->result) != result_id) return 12;
            if (resolved_id(moved->waiter) != waiter_id) return 13;
            (void)pcc_gc_step(64);
            (void)pcc_gc_step(64);
            if (pcc_gc_backend4_verify_no_old_addresses() != 1) return 14;

            pcc_gc_store_root(&root, 0);
            pcc_gc_scheduler_root_unregister(&root);
            printf("backend4-task-relocation-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-task-relocation-ok"


def test_backend4_relocation_preserves_exception_slots_and_traceback(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>
        #include <stdlib.h>

        typedef struct {
            PyObjectHeader h;
            PyObject *exc_class;
            PyObject *message;
            PyObject *cause;
            PyObject *context;
            void *traceback;
            int32_t n_frames;
            int32_t cap_frames;
        } ProbeExceptionObject;

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            PyObject *root = 0;
            pcc_gc_scheduler_root_register(&root);

            PyObject *exc_class = py_str_new("exc-class", 9);
            PyObject *message = py_list_new(0);
            PyObject *cause = py_list_new(0);
            PyObject *context = py_list_new(0);
            if (exc_class == 0 || message == 0 || cause == 0 || context == 0) return 3;
            int64_t exc_class_id = pcc_gc_object_id(exc_class);
            int64_t message_id = pcc_gc_object_id(message);
            int64_t cause_id = pcc_gc_object_id(cause);
            int64_t context_id = pcc_gc_object_id(context);

            ProbeExceptionObject *exc = (ProbeExceptionObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbeExceptionObject),
                PY_TYPE_EXC,
                0
            );
            if (exc == 0) return 4;
            exc->exc_class = 0;
            exc->message = 0;
            exc->cause = 0;
            exc->context = 0;
            /* Public ABI stores one 32-byte frame: three borrowed pointers,
             * line i32, and padding. */
            exc->traceback = calloc(1, 32);
            exc->n_frames = 1;
            exc->cap_frames = 1;
            if (exc->traceback == 0) return 5;
            void *old_traceback = exc->traceback;
            pcc_gc_store_ptr((PyObject *)exc, &exc->exc_class, exc_class);
            pcc_gc_store_ptr((PyObject *)exc, &exc->message, message);
            pcc_gc_store_ptr((PyObject *)exc, &exc->cause, cause);
            pcc_gc_store_ptr((PyObject *)exc, &exc->context, context);
            pcc_gc_store_root(&root, (PyObject *)exc);
            pcc_gc_release((PyObject *)exc);
            pcc_gc_release(exc_class);
            pcc_gc_release(message);
            pcc_gc_release(cause);
            pcc_gc_release(context);

            pcc_gc_telemetry_reset();
            pcc_gc_publish_initialized((PyObject *)exc);
            if (pcc_gc_select_relocation_set(64) <= 0) return 6;
            if (pcc_gc_relocation_set_contains(root) != 1) return 7;
            PyObject *moved_raw = pcc_gc_relocate_copy(
                root,
                (int64_t)sizeof(ProbeExceptionObject)
            );
            if (moved_raw == 0) return 8;
            pcc_gc_release(moved_raw);

            PyObject *loaded = pcc_gc_load_ptr(0, &root);
            if (loaded == 0) return 9;
            if (loaded == (PyObject *)exc) return 10;
            ProbeExceptionObject *moved = (ProbeExceptionObject *)loaded;
            if (moved->traceback == 0) return 11;
            if (moved->traceback == old_traceback) return 12;
            if (moved->n_frames != 1 || moved->cap_frames != 1) return 13;

            PyObject *resolved_class = pcc_gc_note_relocation_read(moved->exc_class);
            if (resolved_class == 0) return 19;
            if (pcc_gc_object_id(resolved_class) != exc_class_id) return 20;
            PyObject *resolved_message = pcc_gc_note_relocation_read(moved->message);
            PyObject *resolved_cause = pcc_gc_note_relocation_read(moved->cause);
            PyObject *resolved_context = pcc_gc_note_relocation_read(moved->context);
            if (resolved_message == 0 || pcc_gc_object_id(resolved_message) != message_id) return 15;
            if (resolved_cause == 0 || pcc_gc_object_id(resolved_cause) != cause_id) return 16;
            if (resolved_context == 0 || pcc_gc_object_id(resolved_context) != context_id) return 17;
            (void)pcc_gc_step(256);
            (void)pcc_gc_step(256);
            if (pcc_gc_backend4_verify_no_old_addresses() != 1) return 18;

            pcc_gc_store_root(&root, 0);
            pcc_gc_scheduler_root_unregister(&root);
            printf("backend4-exc-relocation-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-exc-relocation-ok"


def test_backend4_exception_accessors_load_forwarded_slots(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        typedef struct {
            PyObjectHeader h;
            PyObject *exc_class;
            PyObject *message;
            PyObject *cause;
            PyObject *context;
            void *traceback;
            int32_t n_frames;
            int32_t cap_frames;
        } ProbeExceptionObject;

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            PyObject *exc_root = 0;
            PyObject *message_root = 0;
            PyObject *cause_root = 0;
            PyObject *context_root = 0;
            pcc_gc_scheduler_root_register(&exc_root);
            pcc_gc_scheduler_root_register(&message_root);
            pcc_gc_scheduler_root_register(&cause_root);
            pcc_gc_scheduler_root_register(&context_root);

            PyObject *message = py_list_new(0);
            PyObject *cause = py_list_new(0);
            PyObject *context = py_list_new(0);
            if (message == 0 || cause == 0 || context == 0) return 3;

            PyObject *exc = py_exc_new_with_value(PY_EXC_RUNTIMEERROR, message);
            if (exc == 0) return 4;
            py_exc_set_cause(exc, cause);
            py_exc_set_context(exc, context);

            pcc_gc_store_root(&exc_root, exc);
            pcc_gc_store_root(&message_root, message);
            pcc_gc_store_root(&cause_root, cause);
            pcc_gc_store_root(&context_root, context);
            pcc_gc_release(exc);
            pcc_gc_release(message);
            pcc_gc_release(cause);
            pcc_gc_release(context);

            pcc_gc_telemetry_reset();
            if (pcc_gc_select_relocation_set(256) <= 0) return 5;
            if (pcc_gc_relocation_set_contains(message_root) != 1) return 6;
            if (pcc_gc_relocation_set_contains(cause_root) != 1) return 7;
            if (pcc_gc_relocation_set_contains(context_root) != 1) return 8;

            PyObject *moved_message_raw = pcc_gc_relocate_copy(
                message_root,
                (int64_t)sizeof(ProbeListObject)
            );
            PyObject *moved_cause_raw = pcc_gc_relocate_copy(
                cause_root,
                (int64_t)sizeof(ProbeListObject)
            );
            PyObject *moved_context_raw = pcc_gc_relocate_copy(
                context_root,
                (int64_t)sizeof(ProbeListObject)
            );
            if (moved_message_raw == 0 || moved_cause_raw == 0 || moved_context_raw == 0) {
                return 9;
            }
            pcc_gc_release(moved_message_raw);
            pcc_gc_release(moved_cause_raw);
            pcc_gc_release(moved_context_raw);

            PyObject *loaded_message = pcc_gc_load_ptr(0, &message_root);
            PyObject *loaded_cause = pcc_gc_load_ptr(0, &cause_root);
            PyObject *loaded_context = pcc_gc_load_ptr(0, &context_root);
            if (loaded_message == 0 || loaded_cause == 0 || loaded_context == 0) return 10;
            if (loaded_message == message || loaded_cause == cause || loaded_context == context) {
                return 11;
            }

            PyObject *loaded_exc = pcc_gc_load_ptr(0, &exc_root);
            if (loaded_exc == 0) return 12;
            ProbeExceptionObject *probe = (ProbeExceptionObject *)loaded_exc;

            PyObject *borrowed_message = py_exc_get_message(loaded_exc);
            if (borrowed_message != loaded_message) return 13;
            if (probe->message != loaded_message) return 14;

            PyObject *got_cause = py_exc_get_cause(loaded_exc);
            if (got_cause != loaded_cause) return 15;
            py_decref(got_cause);
            if (probe->cause != loaded_cause) return 16;

            PyObject *got_context = py_exc_get_context(loaded_exc);
            if (got_context != loaded_context) return 17;
            py_decref(got_context);
            if (probe->context != loaded_context) return 18;

            (void)pcc_gc_step(256);
            (void)pcc_gc_step(256);
            if (pcc_gc_backend4_verify_no_old_addresses() != 1) return 19;

            pcc_gc_store_root(&context_root, 0);
            pcc_gc_store_root(&cause_root, 0);
            pcc_gc_store_root(&message_root, 0);
            pcc_gc_store_root(&exc_root, 0);
            pcc_gc_scheduler_root_unregister(&context_root);
            pcc_gc_scheduler_root_unregister(&cause_root);
            pcc_gc_scheduler_root_unregister(&message_root);
            pcc_gc_scheduler_root_unregister(&exc_root);
            printf("backend4-exc-accessor-barrier-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-exc-accessor-barrier-ok"


def test_backend4_exception_print_loads_forwarded_message_slot(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_internal.h"
        #include <stdio.h>

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        typedef struct {
            PyObjectHeader h;
            PyClassObject *exc_class;
            PyObject *message;
            PyObject *cause;
            PyObject *context;
            void *traceback;
            int32_t n_frames;
            int32_t cap_frames;
        } ProbeExceptionObject;

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            PyObject *exc_root = 0;
            PyObject *message_root = 0;
            pcc_gc_scheduler_root_register(&exc_root);
            pcc_gc_scheduler_root_register(&message_root);

            PyObject *message = py_list_new(0);
            if (message == 0) return 3;
            PyObject *exc = py_exc_new_with_value(PY_EXC_RUNTIMEERROR, message);
            if (exc == 0) return 4;
            pcc_gc_store_root(&message_root, message);
            pcc_gc_store_root(&exc_root, exc);
            pcc_gc_release(message);
            pcc_gc_release(exc);

            if (pcc_gc_select_relocation_set(64) <= 0) return 5;
            if (pcc_gc_relocation_set_contains(message_root) != 1) return 6;
            PyObject *moved_message_raw = pcc_gc_relocate_copy(
                message_root,
                (int64_t)sizeof(ProbeListObject)
            );
            if (moved_message_raw == 0) return 7;
            pcc_gc_release(moved_message_raw);

            PyObject *loaded_message = pcc_gc_load_ptr(0, &message_root);
            PyObject *loaded_exc = pcc_gc_load_ptr(0, &exc_root);
            if (loaded_message == 0 || loaded_exc == 0) return 8;
            if (loaded_message == message) return 9;

            pcc_gc_telemetry_reset();
            py_exc_print_unhandled(loaded_exc);
            if (pcc_gc_telemetry(PCC_GC_COUNTER_READ_BARRIERS) <= 0) return 10;

            ProbeExceptionObject *probe = (ProbeExceptionObject *)loaded_exc;
            if (probe->message != loaded_message) return 11;

            pcc_gc_store_root(&exc_root, 0);
            pcc_gc_store_root(&message_root, 0);
            pcc_gc_scheduler_root_unregister(&exc_root);
            pcc_gc_scheduler_root_unregister(&message_root);
            printf("backend4-exception-print-barrier-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-exception-print-barrier-ok"


def test_backend4_tls_exception_accessors_heal_forwarded_reference(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        typedef struct ProbeClassObject {
            PyObjectHeader h;
            const char *name;
            int32_t n_bases;
            struct ProbeClassObject **bases;
            int32_t n_mro;
            struct ProbeClassObject **mro;
            int32_t n_methods;
            void *methods;
            int32_t n_fields;
            const char **field_names;
            int32_t instance_size;
            int32_t type_tag_alloc;
            PyObject *del_method;
            PyObject *attrs;
        } ProbeClassObject;

        typedef struct {
            PyObjectHeader h;
            ProbeClassObject *exc_class;
            PyObject *message;
            PyObject *cause;
            PyObject *context;
            void *traceback;
            int32_t n_frames;
            int32_t cap_frames;
        } ProbeExceptionObject;

        static int relocate_pending_exception(
            PyObject *old,
            int64_t expected_id
        ) {
            if (pcc_gc_select_relocation_set(128) <= 0) return 0;
            if (!pcc_gc_relocation_set_contains(old)) return 0;
            PyObject *moved = pcc_gc_relocate_copy(
                old,
                (int64_t)sizeof(ProbeExceptionObject)
            );
            if (moved == 0 || pcc_gc_object_id(moved) != expected_id) return 0;
            pcc_gc_release(moved);
            return 1;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            PyObject *first_root = 0;
            pcc_gc_scheduler_root_register(&first_root);
            PyObject *first = py_exc_new(PY_EXC_VALUEERROR, "current");
            if (first == 0) return 3;
            int64_t first_id = pcc_gc_object_id(first);
            pcc_gc_store_root(&first_root, first);
            py_raise(first);
            pcc_gc_release(first);
            if (!relocate_pending_exception(first, first_id)) return 4;

            PyObject *current = py_current_exception();
            if (current == 0 || current == first) return 5;
            if (pcc_gc_object_id(current) != first_id) return 6;
            if ((PyObject *)py_tls_exc_get() != current) return 7;
            py_clear_exception();
            if (py_tls_exc_get() != 0) return 9;
            (void)pcc_gc_load_ptr(0, &first_root);
            pcc_gc_store_root(&first_root, 0);
            pcc_gc_scheduler_root_unregister(&first_root);

            PyObject *second_root = 0;
            pcc_gc_scheduler_root_register(&second_root);
            PyObject *second = py_exc_new(PY_EXC_KEYERROR, "clear");
            if (second == 0) return 10;
            int64_t second_id = pcc_gc_object_id(second);
            pcc_gc_store_root(&second_root, second);
            py_raise(second);
            pcc_gc_release(second);
            if (!relocate_pending_exception(second, second_id)) return 11;

            py_clear_exception();
            if (py_tls_exc_get() != 0) return 12;
            (void)pcc_gc_load_ptr(0, &second_root);
            pcc_gc_store_root(&second_root, 0);
            pcc_gc_scheduler_root_unregister(&second_root);

            printf("backend4-tls-exception-forwarding-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-tls-exception-forwarding-ok"


def test_backend4_raise_context_chaining_resolves_forwarded_current_exception(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        typedef struct ProbeClassObject {
            PyObjectHeader h;
            const char *name;
            int32_t n_bases;
            struct ProbeClassObject **bases;
            int32_t n_mro;
            struct ProbeClassObject **mro;
            int32_t n_methods;
            void *methods;
            int32_t n_fields;
            const char **field_names;
            int32_t instance_size;
            int32_t type_tag_alloc;
            PyObject *del_method;
            PyObject *attrs;
        } ProbeClassObject;

        typedef struct {
            PyObjectHeader h;
            ProbeClassObject *exc_class;
            PyObject *message;
            PyObject *cause;
            PyObject *context;
            void *traceback;
            int32_t n_frames;
            int32_t cap_frames;
        } ProbeExceptionObject;

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            PyObject *first_root = 0;
            PyObject *second_root = 0;
            pcc_gc_scheduler_root_register(&first_root);
            pcc_gc_scheduler_root_register(&second_root);

            PyObject *first = py_exc_new(PY_EXC_KEYERROR, "first");
            PyObject *second = py_exc_new(PY_EXC_VALUEERROR, "second");
            if (first == 0 || second == 0) return 3;
            int64_t first_id = pcc_gc_object_id(first);
            pcc_gc_store_root(&first_root, first);
            pcc_gc_store_root(&second_root, second);

            py_raise(first);

            pcc_gc_telemetry_reset();
            if (pcc_gc_select_relocation_set(128) <= 0) return 4;
            if (pcc_gc_relocation_set_contains(first_root) != 1) return 5;
            PyObject *moved_first_raw = pcc_gc_relocate_copy(
                first_root,
                (int64_t)sizeof(ProbeExceptionObject)
            );
            if (moved_first_raw == 0) return 6;
            pcc_gc_release(moved_first_raw);

            PyObject *loaded_first = pcc_gc_load_ptr(0, &first_root);
            if (loaded_first == 0) return 7;
            if (loaded_first == first) return 8;

            py_raise(second);

            PyObject *loaded_second = pcc_gc_load_ptr(0, &second_root);
            if (loaded_second == 0) return 9;
            PyObject *got_context = py_exc_get_context(loaded_second);
            if (got_context == 0) return 10;
            if (got_context != loaded_first) return 11;
            if (pcc_gc_object_id(got_context) != first_id) return 12;

            ProbeExceptionObject *second_probe = (ProbeExceptionObject *)loaded_second;
            if (second_probe->context == first) return 13;
            if (second_probe->context != loaded_first) return 14;
            if (pcc_gc_backend4_verify_no_old_addresses() != 1) return 15;

            py_decref(got_context);
            py_clear_exception();
            pcc_gc_store_root(&second_root, 0);
            pcc_gc_store_root(&first_root, 0);
            pcc_gc_scheduler_root_unregister(&second_root);
            pcc_gc_scheduler_root_unregister(&first_root);
            printf("backend4-raise-context-forwarded-current-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-raise-context-forwarded-current-ok"


def test_backend4_relocation_retargets_class_attrs_side_table(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>
        #include <stdlib.h>

        typedef struct {
            const char *name;
            PyObject *func;
        } ProbeClassMethod;

        typedef struct ProbeClassObject {
            PyObjectHeader h;
            const char *name;
            int32_t n_bases;
            struct ProbeClassObject **bases;
            int32_t n_mro;
            struct ProbeClassObject **mro;
            int32_t n_methods;
            ProbeClassMethod *methods;
            int32_t n_fields;
            const char **field_names;
            int32_t instance_size;
            int32_t type_tag_alloc;
            PyObject *del_method;
            PyObject *attrs;
        } ProbeClassObject;

        typedef struct {
            PyObjectHeader h;
            int64_t size;
            int64_t capacity;
            int64_t *indices;
            void *entries;
            int64_t entries_used;
        } ProbeDictObject;

        extern int64_t py_class_setattr(ProbeClassObject *cls, const char *name, PyObject *value);
        extern PyObject *py_class_getattr(ProbeClassObject *cls, const char *name);

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            PyObject *root = 0;
            pcc_gc_scheduler_root_register(&root);

            ProbeClassObject *cls = (ProbeClassObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbeClassObject),
                PY_TYPE_CLASS,
                0
            );
            if (cls == 0) return 3;
            cls->name = "Probe";
            cls->n_bases = 0;
            cls->bases = 0;
            cls->n_mro = 1;
            cls->mro = (ProbeClassObject **)calloc(1, sizeof(ProbeClassObject *));
            if (cls->mro == 0) return 4;
            cls->mro[0] = cls;
            cls->n_methods = 0;
            cls->methods = 0;
            cls->n_fields = 0;
            cls->field_names = 0;
            cls->instance_size = 32;
            cls->type_tag_alloc = 1000;
            cls->del_method = 0;
            cls->attrs = 0;

            PyObject *value = py_str_new("class-attr", 10);
            if (value == 0) return 5;
            if (py_class_setattr(cls, "answer", value) != 0) return 6;
            if (cls->attrs == 0) return 17;
            PyObject *old_attrs = cls->attrs;
            int64_t attrs_id = pcc_gc_object_id(cls->attrs);
            pcc_gc_store_root(&root, (PyObject *)cls);
            pcc_gc_release((PyObject *)cls);
            pcc_gc_release(value);

            pcc_gc_telemetry_reset();
            pcc_gc_publish_initialized((PyObject *)cls);
            if (pcc_gc_select_relocation_set(64) <= 0) return 7;
            if (pcc_gc_relocation_set_contains(root) != 1) return 8;
            if (pcc_gc_relocation_set_contains(old_attrs) != 1) return 20;
            PyObject *moved_attrs_raw = pcc_gc_relocate_copy(
                old_attrs,
                (int64_t)sizeof(ProbeDictObject)
            );
            if (moved_attrs_raw == 0) return 22;
            pcc_gc_release(moved_attrs_raw);
            PyObject *moved_raw = pcc_gc_relocate_copy(
                root,
                (int64_t)sizeof(ProbeClassObject)
            );
            if (moved_raw == 0) return 9;
            pcc_gc_release(moved_raw);

            PyObject *loaded = pcc_gc_load_ptr(0, &root);
            if (loaded == 0) return 10;
            if (loaded == (PyObject *)cls) return 11;
            ProbeClassObject *moved = (ProbeClassObject *)loaded;
            if (moved->mro == 0 || moved->mro[0] != moved) return 12;
            if (moved->name == 0 || moved->type_tag_alloc != 1000) return 13;
            if (moved->attrs == 0) return 18;
            if (pcc_gc_object_id(moved->attrs) != attrs_id) return 19;

            PyObject *attr = py_class_getattr(moved, "answer");
            PyObject *expected = py_str_new("class-attr", 10);
            if (attr == 0 || expected == 0) return 14;
            if (moved->attrs == old_attrs) return 23;
            if (pcc_gc_object_id(moved->attrs) != attrs_id) return 24;
            if (py_str_eq(attr, expected) != 1) return 15;
            (void)pcc_gc_step(256);
            (void)pcc_gc_step(256);
            if (pcc_gc_backend4_verify_no_old_addresses() != 1) return 16;

            pcc_gc_release(expected);
            pcc_gc_release(attr);
            pcc_gc_store_root(&root, 0);
            pcc_gc_scheduler_root_unregister(&root);
            printf("backend4-class-relocation-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-class-relocation-ok"


def test_backend4_class_attrs_creation_uses_store_barrier(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>
        #include <stdlib.h>

        typedef struct ProbeClassObject {
            PyObjectHeader h;
            const char *name;
            int32_t n_bases;
            struct ProbeClassObject **bases;
            int32_t n_mro;
            struct ProbeClassObject **mro;
            int32_t n_methods;
            void *methods;
            int32_t n_fields;
            const char **field_names;
            int32_t instance_size;
            int32_t type_tag_alloc;
            PyObject *del_method;
            PyObject *attrs;
        } ProbeClassObject;

        extern int64_t py_class_setattr(ProbeClassObject *cls, const char *name, PyObject *value);

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            ProbeClassObject *cls = (ProbeClassObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbeClassObject),
                PY_TYPE_CLASS,
                0x100
            );
            if (cls == 0) return 3;
            cls->name = "OldClass";
            cls->n_bases = 0;
            cls->bases = 0;
            cls->n_mro = 1;
            cls->mro = (ProbeClassObject **)calloc(1, sizeof(ProbeClassObject *));
            if (cls->mro == 0) return 25;
            cls->mro[0] = cls;
            cls->n_methods = 0;
            cls->methods = 0;
            cls->n_fields = 0;
            cls->field_names = 0;
            cls->instance_size = 32;
            cls->type_tag_alloc = 1001;
            cls->del_method = 0;
            cls->attrs = 0;

            PyObject *value = py_str_new("barrier", 7);
            if (value == 0) return 4;
            pcc_gc_telemetry_reset();
            if (py_class_setattr(cls, "answer", value) != 0) return 5;
            if (cls->attrs == 0) return 6;
            if (pcc_gc_backend4_generation_barrier_score() != 1) return 7;
            if (pcc_gc_backend4_store_buffer_entries() != 1) return 8;

            pcc_gc_release(value);
            printf("backend4-class-attrs-store-barrier-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-class-attrs-store-barrier-ok"


def test_backend4_class_relocation_loads_forwarded_attrs_slot(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>
        #include <stdlib.h>

        typedef struct ProbeClassObject {
            PyObjectHeader h;
            const char *name;
            int32_t n_bases;
            struct ProbeClassObject **bases;
            int32_t n_mro;
            struct ProbeClassObject **mro;
            int32_t n_methods;
            void *methods;
            int32_t n_fields;
            const char **field_names;
            int32_t instance_size;
            int32_t type_tag_alloc;
            PyObject *del_method;
            PyObject *attrs;
        } ProbeClassObject;

        typedef struct {
            PyObjectHeader h;
            int64_t size;
            int64_t capacity;
            int64_t *indices;
            void *entries;
            int64_t entries_used;
        } ProbeDictObject;

        extern int64_t py_class_setattr(ProbeClassObject *cls, const char *name, PyObject *value);
        extern PyObject *py_class_getattr(ProbeClassObject *cls, const char *name);

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            PyObject *root = 0;
            pcc_gc_scheduler_root_register(&root);

            ProbeClassObject *cls = (ProbeClassObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbeClassObject),
                PY_TYPE_CLASS,
                0
            );
            if (cls == 0) return 3;
            cls->name = "ForwardedAttrsClass";
            cls->n_bases = 0;
            cls->bases = 0;
            cls->n_mro = 1;
            cls->mro = (ProbeClassObject **)calloc(1, sizeof(ProbeClassObject *));
            if (cls->mro == 0) return 25;
            cls->mro[0] = cls;
            cls->n_methods = 0;
            cls->methods = 0;
            cls->n_fields = 0;
            cls->field_names = 0;
            cls->instance_size = 32;
            cls->type_tag_alloc = 1002;
            cls->del_method = 0;
            cls->attrs = 0;

            PyObject *value = py_str_new("forwarded-attrs", 15);
            if (value == 0) return 4;
            if (py_class_setattr(cls, "answer", value) != 0) return 5;
            if (cls->attrs == 0) return 6;
            PyObject *old_attrs = cls->attrs;
            int64_t attrs_id = pcc_gc_object_id(old_attrs);
            pcc_gc_store_root(&root, (PyObject *)cls);
            pcc_gc_release((PyObject *)cls);
            pcc_gc_release(value);

            pcc_gc_telemetry_reset();
            pcc_gc_publish_initialized((PyObject *)cls);
            if (pcc_gc_select_relocation_set(64) <= 0) return 7;
            if (pcc_gc_relocation_set_contains(root) != 1) return 8;
            if (pcc_gc_relocation_set_contains(old_attrs) != 1) return 9;

            PyObject *moved_attrs_raw = pcc_gc_relocate_copy(
                old_attrs,
                (int64_t)sizeof(ProbeDictObject)
            );
            if (moved_attrs_raw == 0) return 10;
            pcc_gc_release(moved_attrs_raw);

            PyObject *moved_raw = pcc_gc_relocate_copy(
                root,
                (int64_t)sizeof(ProbeClassObject)
            );
            if (moved_raw == 0) return 11;
            pcc_gc_release(moved_raw);

            PyObject *loaded = pcc_gc_load_ptr(0, &root);
            if (loaded == 0) return 12;
            ProbeClassObject *moved = (ProbeClassObject *)loaded;
            if (moved == cls) return 13;
            if (moved->mro == 0 || moved->mro[0] != moved) return 25;
            if (moved->attrs == old_attrs) return 14;
            if (pcc_gc_object_id(moved->attrs) != attrs_id) return 15;

            PyObject *attr = py_class_getattr(moved, "answer");
            PyObject *expected = py_str_new("forwarded-attrs", 15);
            if (attr == 0 || expected == 0) return 16;
            if (py_str_eq(attr, expected) != 1) return 17;

            pcc_gc_release(expected);
            pcc_gc_release(attr);
            pcc_gc_store_root(&root, 0);
            pcc_gc_scheduler_root_unregister(&root);
            printf("backend4-class-forwarded-attrs-load-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-class-forwarded-attrs-load-ok"


def test_backend4_class_attrs_api_resolves_forwarded_class_argument(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>
        #include <stdlib.h>

        typedef struct ProbeClassObject {
            PyObjectHeader h;
            const char *name;
            int32_t n_bases;
            struct ProbeClassObject **bases;
            int32_t n_mro;
            struct ProbeClassObject **mro;
            int32_t n_methods;
            void *methods;
            int32_t n_fields;
            const char **field_names;
            int32_t instance_size;
            int32_t type_tag_alloc;
            PyObject *del_method;
            PyObject *attrs;
        } ProbeClassObject;

        extern int64_t py_class_setattr(ProbeClassObject *cls, const char *name, PyObject *value);
        extern PyObject *py_class_getattr(ProbeClassObject *cls, const char *name);

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            ProbeClassObject *cls = (ProbeClassObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbeClassObject),
                PY_TYPE_CLASS,
                0
            );
            PyObject *value = py_str_new("forwarded-class-attr", 20);
            if (cls == 0 || value == 0) return 3;
            cls->name = "ForwardedAttrsApiClass";
            cls->n_bases = 0;
            cls->bases = 0;
            cls->n_mro = 1;
            cls->mro = (ProbeClassObject **)calloc(1, sizeof(ProbeClassObject *));
            if (cls->mro == 0) return 13;
            cls->mro[0] = cls;
            cls->n_methods = 0;
            cls->methods = 0;
            cls->n_fields = 0;
            cls->field_names = 0;
            cls->instance_size = 32;
            cls->type_tag_alloc = 1008;
            cls->del_method = 0;
            cls->attrs = 0;

            pcc_gc_telemetry_reset();
            pcc_gc_publish_initialized((PyObject *)cls);
            if (pcc_gc_select_relocation_set(64) <= 0) return 4;
            if (pcc_gc_relocation_set_contains((PyObject *)cls) != 1) return 5;
            PyObject *moved_cls_raw = pcc_gc_relocate_copy(
                (PyObject *)cls,
                (int64_t)sizeof(ProbeClassObject)
            );
            if (moved_cls_raw == 0) return 6;
            pcc_gc_release(moved_cls_raw);

            if (py_class_setattr(cls, "answer", value) != 0) return 7;
            ProbeClassObject *moved = (ProbeClassObject *)pcc_gc_note_relocation_read(
                (PyObject *)cls
            );
            if (moved == cls) return 8;
            if (cls->attrs != 0) return 9;
            if (moved->attrs == 0) return 10;

            PyObject *got = py_class_getattr(cls, "answer");
            PyObject *expected = py_str_new("forwarded-class-attr", 20);
            if (got == 0 || expected == 0) return 11;
            if (py_str_eq(got, expected) != 1) return 12;

            pcc_gc_release(expected);
            pcc_gc_release(got);
            pcc_gc_release(value);
            printf("backend4-class-attrs-forwarded-class-arg-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-class-attrs-forwarded-class-arg-ok"


def test_backend4_class_relocation_loads_forwarded_metadata_slots(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>
        #include <stdlib.h>

        typedef struct {
            const char *name;
            PyObject *func;
        } ProbeClassMethod;

        typedef struct ProbeClassObject {
            PyObjectHeader h;
            const char *name;
            int32_t n_bases;
            struct ProbeClassObject **bases;
            int32_t n_mro;
            struct ProbeClassObject **mro;
            int32_t n_methods;
            ProbeClassMethod *methods;
            int32_t n_fields;
            const char **field_names;
            int32_t instance_size;
            int32_t type_tag_alloc;
            PyObject *del_method;
            PyObject *attrs;
        } ProbeClassObject;

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        static ProbeClassObject *new_probe_class(const char *name, int32_t tag) {
            ProbeClassObject *cls = (ProbeClassObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbeClassObject),
                PY_TYPE_CLASS,
                0
            );
            if (cls == 0) return 0;
            cls->name = name;
            cls->n_bases = 0;
            cls->bases = 0;
            cls->n_mro = 0;
            cls->mro = 0;
            cls->n_methods = 0;
            cls->methods = 0;
            cls->n_fields = 0;
            cls->field_names = 0;
            cls->instance_size = 32;
            cls->type_tag_alloc = tag;
            cls->del_method = 0;
            cls->attrs = 0;
            pcc_gc_publish_initialized((PyObject *)cls);
            return cls;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            PyObject *root = 0;
            pcc_gc_scheduler_root_register(&root);

            ProbeClassObject *base = new_probe_class("Base", 1003);
            ProbeClassObject *cls = new_probe_class("Derived", 1004);
            PyObject *func = py_list_new(0);
            if (base == 0 || cls == 0 || func == 0) return 3;

            cls->n_bases = 1;
            cls->bases = (ProbeClassObject **)calloc(1, sizeof(ProbeClassObject *));
            cls->n_mro = 2;
            cls->mro = (ProbeClassObject **)calloc(2, sizeof(ProbeClassObject *));
            cls->n_methods = 1;
            cls->methods = (ProbeClassMethod *)calloc(1, sizeof(ProbeClassMethod));
            if (cls->bases == 0 || cls->mro == 0 || cls->methods == 0) return 4;
            cls->bases[0] = base;
            cls->mro[0] = cls;
            cls->mro[1] = base;
            cls->methods[0].name = "method";
            cls->methods[0].func = func;
            cls->del_method = func;

            int64_t base_id = pcc_gc_object_id((PyObject *)base);
            int64_t func_id = pcc_gc_object_id(func);
            pcc_gc_store_root(&root, (PyObject *)cls);
            pcc_gc_release((PyObject *)cls);

            pcc_gc_telemetry_reset();
            if (pcc_gc_select_relocation_set(64) <= 0) return 5;
            if (pcc_gc_relocation_set_contains((PyObject *)base) != 1) return 6;
            if (pcc_gc_relocation_set_contains(func) != 1) return 7;
            if (pcc_gc_relocation_set_contains(root) != 1) return 8;

            PyObject *moved_base_raw = pcc_gc_relocate_copy(
                (PyObject *)base,
                (int64_t)sizeof(ProbeClassObject)
            );
            if (moved_base_raw == 0) return 9;

            PyObject *moved_func_raw = pcc_gc_relocate_copy(
                func,
                (int64_t)sizeof(ProbeListObject)
            );
            if (moved_func_raw == 0) return 10;

            PyObject *moved_raw = pcc_gc_relocate_copy(
                root,
                (int64_t)sizeof(ProbeClassObject)
            );
            if (moved_raw == 0) return 11;
            pcc_gc_release(moved_raw);

            PyObject *loaded = pcc_gc_load_ptr(0, &root);
            if (loaded == 0) return 12;
            ProbeClassObject *moved = (ProbeClassObject *)loaded;
            if (moved == cls) return 13;
            if (moved->bases == 0 || moved->mro == 0 || moved->methods == 0) return 14;
            if (moved->bases[0] == base) return 15;
            if (pcc_gc_object_id((PyObject *)moved->bases[0]) != base_id) return 16;
            if (moved->mro[0] != moved) return 17;
            if (moved->mro[1] == base) return 18;
            if (pcc_gc_object_id((PyObject *)moved->mro[1]) != base_id) return 19;
            if (moved->methods[0].func == func) return 20;
            if (pcc_gc_object_id(moved->methods[0].func) != func_id) return 21;
            if (moved->del_method == func) return 22;
            if (pcc_gc_object_id(moved->del_method) != func_id) return 23;

            pcc_gc_store_root(&root, 0);
            pcc_gc_scheduler_root_unregister(&root);
            pcc_gc_release(moved_func_raw);
            pcc_gc_release(moved_base_raw);
            printf("backend4-class-forwarded-metadata-load-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-class-forwarded-metadata-load-ok"


def test_backend4_class_lookup_loads_forwarded_method_slot(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>
        #include <stdlib.h>

        typedef struct {
            const char *name;
            PyObject *func;
        } ProbeClassMethod;

        typedef struct ProbeClassObject {
            PyObjectHeader h;
            const char *name;
            int32_t n_bases;
            struct ProbeClassObject **bases;
            int32_t n_mro;
            struct ProbeClassObject **mro;
            int32_t n_methods;
            ProbeClassMethod *methods;
            int32_t n_fields;
            const char **field_names;
            int32_t instance_size;
            int32_t type_tag_alloc;
            PyObject *del_method;
            PyObject *attrs;
        } ProbeClassObject;

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        extern PyObject *py_class_lookup(ProbeClassObject *cls, const char *name);

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            ProbeClassObject *cls = (ProbeClassObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbeClassObject),
                PY_TYPE_CLASS,
                0
            );
            PyObject *func = py_list_new(0);
            if (cls == 0 || func == 0) return 3;
            cls->name = "LookupClass";
            cls->n_bases = 0;
            cls->bases = 0;
            cls->n_mro = 1;
            cls->mro = (ProbeClassObject **)calloc(1, sizeof(ProbeClassObject *));
            cls->n_methods = 1;
            cls->methods = (ProbeClassMethod *)calloc(1, sizeof(ProbeClassMethod));
            cls->n_fields = 0;
            cls->field_names = 0;
            cls->instance_size = 32;
            cls->type_tag_alloc = 1005;
            cls->del_method = 0;
            cls->attrs = 0;
            if (cls->mro == 0 || cls->methods == 0) return 4;
            cls->mro[0] = cls;
            cls->methods[0].name = "method";
            cls->methods[0].func = func;
            int64_t func_id = pcc_gc_object_id(func);
            /* Fill the method-location cache before the function moves.  The
               second lookup below must hit that cached location and still
               reload/heal the function slot through the relocation barrier. */
            if (py_class_lookup(cls, "method") != func) return 13;
            pcc_gc_telemetry_reset();
            pcc_gc_publish_initialized((PyObject *)cls);
            if (pcc_gc_select_relocation_set(64) <= 0) return 5;
            if (pcc_gc_relocation_set_contains(func) != 1) return 6;
            PyObject *moved_func_raw = pcc_gc_relocate_copy(
                func,
                (int64_t)sizeof(ProbeListObject)
            );
            if (moved_func_raw == 0) return 7;
            pcc_gc_release(moved_func_raw);

            PyObject *looked_up = py_class_lookup(cls, "method");
            if (looked_up == 0) return 8;
            if (looked_up == func) return 9;
            if (pcc_gc_object_id(looked_up) != func_id) return 10;
            if (cls->methods[0].func == func) return 11;
            if (pcc_gc_object_id(cls->methods[0].func) != func_id) return 12;

            printf("backend4-class-lookup-forwarded-method-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-class-lookup-forwarded-method-ok"


def test_backend4_class_add_method_uses_metadata_store_barrier(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>
        #include <stdlib.h>

        typedef struct ProbeClassObject {
            PyObjectHeader h;
            const char *name;
            int32_t n_bases;
            struct ProbeClassObject **bases;
            int32_t n_mro;
            struct ProbeClassObject **mro;
            int32_t n_methods;
            void *methods;
            int32_t n_fields;
            const char **field_names;
            int32_t instance_size;
            int32_t type_tag_alloc;
            PyObject *del_method;
            PyObject *attrs;
        } ProbeClassObject;

        extern void py_class_add_method(ProbeClassObject *cls, const char *name, PyObject *func);
        extern PyObject *py_class_lookup(ProbeClassObject *cls, const char *name);

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            ProbeClassObject *cls = (ProbeClassObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbeClassObject),
                PY_TYPE_CLASS,
                0x100
            );
            PyObject *func = py_list_new(0);
            if (cls == 0 || func == 0) return 3;
            cls->name = "AddMethodClass";
            cls->n_bases = 0;
            cls->bases = 0;
            cls->n_mro = 1;
            cls->mro = (ProbeClassObject **)calloc(1, sizeof(ProbeClassObject *));
            cls->n_methods = 0;
            cls->methods = 0;
            cls->n_fields = 0;
            cls->field_names = 0;
            cls->instance_size = 32;
            cls->type_tag_alloc = 1006;
            cls->del_method = 0;
            cls->attrs = 0;
            if (cls->mro == 0) return 4;
            cls->mro[0] = cls;

            pcc_gc_telemetry_reset();
            py_class_add_method(cls, "method", func);
            if (cls->n_methods != 1) return 5;
            if (pcc_gc_backend4_generation_barrier_score() != 1) return 6;
            if (pcc_gc_backend4_store_buffer_entries() != 1) return 7;
            if (py_class_lookup(cls, "method") != func) return 8;

            printf("backend4-class-add-method-store-barrier-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-class-add-method-store-barrier-ok"


def test_backend4_isinstance_resolves_forwarded_class_argument(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>
        #include <stdlib.h>

        typedef struct ProbeClassObject {
            PyObjectHeader h;
            const char *name;
            int32_t n_bases;
            struct ProbeClassObject **bases;
            int32_t n_mro;
            struct ProbeClassObject **mro;
            int32_t n_methods;
            void *methods;
            int32_t n_fields;
            const char **field_names;
            int32_t instance_size;
            int32_t type_tag_alloc;
            PyObject *del_method;
            PyObject *attrs;
        } ProbeClassObject;

        typedef struct {
            PyObjectHeader h;
            ProbeClassObject *cls;
        } ProbeInstanceObject;

        extern int64_t py_isinstance(PyObject *obj, ProbeClassObject *cls);

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            ProbeClassObject *cls = (ProbeClassObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbeClassObject),
                PY_TYPE_CLASS,
                0
            );
            ProbeInstanceObject *inst = (ProbeInstanceObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbeInstanceObject),
                PY_TYPE_INSTANCE,
                0
            );
            if (cls == 0 || inst == 0) return 3;
            cls->name = "ForwardedIsInstanceClass";
            cls->n_bases = 0;
            cls->bases = 0;
            cls->n_mro = 1;
            cls->mro = (ProbeClassObject **)calloc(1, sizeof(ProbeClassObject *));
            cls->n_methods = 0;
            cls->methods = 0;
            cls->n_fields = 0;
            cls->field_names = 0;
            cls->instance_size = (int32_t)sizeof(ProbeInstanceObject);
            cls->type_tag_alloc = PY_TYPE_INSTANCE;
            cls->del_method = 0;
            cls->attrs = 0;
            if (cls->mro == 0) return 4;
            cls->mro[0] = cls;
            inst->cls = cls;

            pcc_gc_telemetry_reset();
            pcc_gc_publish_initialized((PyObject *)cls);
            pcc_gc_publish_initialized((PyObject *)inst);
            if (pcc_gc_select_relocation_set(64) <= 0) return 5;
            if (pcc_gc_relocation_set_contains((PyObject *)cls) != 1) return 6;
            PyObject *moved_cls_raw = pcc_gc_relocate_copy(
                (PyObject *)cls,
                (int64_t)sizeof(ProbeClassObject)
            );
            if (moved_cls_raw == 0) return 7;
            pcc_gc_release(moved_cls_raw);

            if (py_isinstance((PyObject *)inst, cls) != 1) return 8;
            if (inst->cls == cls) return 9;

            printf("backend4-isinstance-forwarded-class-arg-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-isinstance-forwarded-class-arg-ok"


def test_backend4_instance_get_field_loads_forwarded_slot(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>
        #include <stdlib.h>

        typedef struct ProbeClassObject {
            PyObjectHeader h;
            const char *name;
            int32_t n_bases;
            struct ProbeClassObject **bases;
            int32_t n_mro;
            struct ProbeClassObject **mro;
            int32_t n_methods;
            void *methods;
            int32_t n_fields;
            const char **field_names;
            int32_t instance_size;
            int32_t type_tag_alloc;
            PyObject *del_method;
            PyObject *attrs;
        } ProbeClassObject;

        typedef struct {
            PyObjectHeader h;
            ProbeClassObject *cls;
            PyObject *fields[1];
        } ProbeInstanceObject;

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        extern PyObject *py_instance_get_field(ProbeInstanceObject *inst, int32_t idx);

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            ProbeClassObject *cls = (ProbeClassObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbeClassObject),
                PY_TYPE_CLASS,
                0
            );
            ProbeInstanceObject *inst = (ProbeInstanceObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbeInstanceObject),
                PY_TYPE_INSTANCE,
                0
            );
            PyObject *value = py_list_new(0);
            if (cls == 0 || inst == 0 || value == 0) return 3;
            cls->name = "FieldClass";
            cls->n_bases = 0;
            cls->bases = 0;
            cls->n_mro = 0;
            cls->mro = 0;
            cls->n_methods = 0;
            cls->methods = 0;
            cls->n_fields = 1;
            cls->field_names = (const char **)calloc(1, sizeof(const char *));
            cls->instance_size = (int32_t)sizeof(ProbeInstanceObject);
            cls->type_tag_alloc = PY_TYPE_INSTANCE;
            cls->del_method = 0;
            cls->attrs = 0;
            if (cls->field_names == 0) return 10;
            cls->field_names[0] = "field";
            inst->cls = cls;
            inst->fields[0] = value;
            int64_t value_id = pcc_gc_object_id(value);

            pcc_gc_telemetry_reset();
            pcc_gc_publish_initialized((PyObject *)cls);
            pcc_gc_publish_initialized((PyObject *)inst);
            if (pcc_gc_select_relocation_set(64) <= 0) return 4;
            if (pcc_gc_relocation_set_contains(value) != 1) return 5;
            PyObject *moved_value_raw = pcc_gc_relocate_copy(
                value,
                (int64_t)sizeof(ProbeListObject)
            );
            if (moved_value_raw == 0) return 6;
            pcc_gc_release(moved_value_raw);

            PyObject *got = py_instance_get_field(inst, 0);
            if (got == 0) return 7;
            if (got == value) return 8;
            if (pcc_gc_object_id(got) != value_id) return 9;
            if (inst->fields[0] == value) return 10;
            pcc_gc_release(got);

            printf("backend4-instance-get-field-forwarded-slot-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-instance-get-field-forwarded-slot-ok"


def test_backend4_instance_get_field_resolves_forwarded_class_slot(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>
        #include <stdlib.h>

        typedef struct ProbeClassObject {
            PyObjectHeader h;
            const char *name;
            int32_t n_bases;
            struct ProbeClassObject **bases;
            int32_t n_mro;
            struct ProbeClassObject **mro;
            int32_t n_methods;
            void *methods;
            int32_t n_fields;
            const char **field_names;
            int32_t instance_size;
            int32_t type_tag_alloc;
            PyObject *del_method;
            PyObject *attrs;
        } ProbeClassObject;

        typedef struct {
            PyObjectHeader h;
            ProbeClassObject *cls;
            PyObject *fields[1];
        } ProbeInstanceObject;

        extern PyObject *py_instance_get_field(ProbeInstanceObject *inst, int32_t idx);

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            ProbeClassObject *cls = (ProbeClassObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbeClassObject),
                PY_TYPE_CLASS,
                0
            );
            ProbeInstanceObject *inst = (ProbeInstanceObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbeInstanceObject),
                PY_TYPE_INSTANCE,
                0
            );
            PyObject *value = py_str_new("field", 5);
            if (cls == 0 || inst == 0 || value == 0) return 3;
            cls->name = "ForwardedFieldClass";
            cls->n_bases = 0;
            cls->bases = 0;
            cls->n_mro = 0;
            cls->mro = 0;
            cls->n_methods = 0;
            cls->methods = 0;
            cls->n_fields = 1;
            cls->field_names = (const char **)calloc(1, sizeof(const char *));
            cls->instance_size = (int32_t)sizeof(ProbeInstanceObject);
            cls->type_tag_alloc = PY_TYPE_INSTANCE;
            cls->del_method = 0;
            cls->attrs = 0;
            if (cls->field_names == 0) return 10;
            cls->field_names[0] = "field";
            inst->cls = cls;
            inst->fields[0] = value;
            int64_t cls_id = pcc_gc_object_id((PyObject *)cls);

            pcc_gc_telemetry_reset();
            pcc_gc_publish_initialized((PyObject *)cls);
            pcc_gc_publish_initialized((PyObject *)inst);
            if (pcc_gc_select_relocation_set(64) <= 0) return 4;
            if (pcc_gc_relocation_set_contains((PyObject *)cls) != 1) return 5;
            PyObject *moved_cls_raw = pcc_gc_relocate_copy(
                (PyObject *)cls,
                (int64_t)sizeof(ProbeClassObject)
            );
            if (moved_cls_raw == 0) return 6;
            pcc_gc_release(moved_cls_raw);

            PyObject *got = py_instance_get_field(inst, 0);
            if (got == 0) return 7;
            if (inst->cls == cls) return 8;
            if (pcc_gc_object_id((PyObject *)inst->cls) != cls_id) return 9;
            pcc_gc_release(got);
            pcc_gc_release(value);

            printf("backend4-instance-get-field-forwarded-class-slot-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-instance-get-field-forwarded-class-slot-ok"


def test_backend4_instance_new_resolves_forwarded_class_argument(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        typedef struct ProbeClassObject {
            PyObjectHeader h;
            const char *name;
            int32_t n_bases;
            struct ProbeClassObject **bases;
            int32_t n_mro;
            struct ProbeClassObject **mro;
            int32_t n_methods;
            void *methods;
            int32_t n_fields;
            const char **field_names;
            int32_t instance_size;
            int32_t type_tag_alloc;
            PyObject *del_method;
            PyObject *attrs;
        } ProbeClassObject;

        typedef struct {
            PyObjectHeader h;
            ProbeClassObject *cls;
        } ProbeInstanceObject;

        extern PyObject *py_instance_new(ProbeClassObject *cls);

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            ProbeClassObject *cls = (ProbeClassObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbeClassObject),
                PY_TYPE_CLASS,
                0
            );
            if (cls == 0) return 3;
            cls->name = "NewInstanceClass";
            cls->n_bases = 0;
            cls->bases = 0;
            cls->n_mro = 0;
            cls->mro = 0;
            cls->n_methods = 0;
            cls->methods = 0;
            cls->n_fields = 0;
            cls->field_names = 0;
            cls->instance_size = (int32_t)sizeof(ProbeInstanceObject);
            cls->type_tag_alloc = PY_TYPE_INSTANCE;
            cls->del_method = 0;
            cls->attrs = 0;
            int64_t cls_id = pcc_gc_object_id((PyObject *)cls);

            pcc_gc_telemetry_reset();
            pcc_gc_publish_initialized((PyObject *)cls);
            if (pcc_gc_select_relocation_set(64) <= 0) return 4;
            if (pcc_gc_relocation_set_contains((PyObject *)cls) != 1) return 5;
            PyObject *moved_cls_raw = pcc_gc_relocate_copy(
                (PyObject *)cls,
                (int64_t)sizeof(ProbeClassObject)
            );
            if (moved_cls_raw == 0) return 6;
            pcc_gc_release(moved_cls_raw);

            ProbeInstanceObject *inst = (ProbeInstanceObject *)py_instance_new(cls);
            if (inst == 0) return 7;
            if (inst->cls == cls) return 8;
            if (pcc_gc_object_id((PyObject *)inst->cls) != cls_id) return 9;
            pcc_gc_release((PyObject *)inst);

            printf("backend4-instance-new-forwarded-class-arg-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-instance-new-forwarded-class-arg-ok"


def test_backend4_class_add_method_resolves_forwarded_class_argument(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>
        #include <stdlib.h>

        typedef struct {
            const char *name;
            PyObject *func;
        } ProbeClassMethod;

        typedef struct ProbeClassObject {
            PyObjectHeader h;
            const char *name;
            int32_t n_bases;
            struct ProbeClassObject **bases;
            int32_t n_mro;
            struct ProbeClassObject **mro;
            int32_t n_methods;
            ProbeClassMethod *methods;
            int32_t n_fields;
            const char **field_names;
            int32_t instance_size;
            int32_t type_tag_alloc;
            PyObject *del_method;
            PyObject *attrs;
        } ProbeClassObject;

        extern void py_class_add_method(ProbeClassObject *cls, const char *name, PyObject *func);
        extern PyObject *py_class_lookup(ProbeClassObject *cls, const char *name);

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            ProbeClassObject *cls = (ProbeClassObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbeClassObject),
                PY_TYPE_CLASS,
                0
            );
            PyObject *func = pcc_gc_alloc(64, PY_TYPE_INT, 0);
            if (cls == 0 || func == 0) return 3;
            cls->name = "ForwardedAddMethodClass";
            cls->n_bases = 0;
            cls->bases = 0;
            cls->n_mro = 1;
            cls->mro = (ProbeClassObject **)calloc(1, sizeof(ProbeClassObject *));
            cls->n_methods = 0;
            cls->methods = 0;
            cls->n_fields = 0;
            cls->field_names = 0;
            cls->instance_size = 32;
            cls->type_tag_alloc = 1007;
            cls->del_method = 0;
            cls->attrs = 0;
            if (cls->mro == 0) return 4;
            cls->mro[0] = cls;
            int64_t cls_id = pcc_gc_object_id((PyObject *)cls);

            pcc_gc_telemetry_reset();
            pcc_gc_publish_initialized((PyObject *)cls);
            pcc_gc_publish_initialized((PyObject *)func);
            if (pcc_gc_select_relocation_set(64) <= 0) return 5;
            if (pcc_gc_relocation_set_contains((PyObject *)cls) != 1) return 6;
            PyObject *moved_cls_raw = pcc_gc_relocate_copy(
                (PyObject *)cls,
                (int64_t)sizeof(ProbeClassObject)
            );
            if (moved_cls_raw == 0) return 7;
            pcc_gc_release(moved_cls_raw);

            py_class_add_method(cls, "method", func);
            PyObject *moved_cls_obj = pcc_gc_note_relocation_read((PyObject *)cls);
            ProbeClassObject *moved_cls = (ProbeClassObject *)moved_cls_obj;
            if (moved_cls == cls) return 8;
            if (pcc_gc_object_id((PyObject *)moved_cls) != cls_id) return 9;
            if (cls->n_methods != 0) return 10;
            if (moved_cls->n_methods != 1) return 11;
            if (py_class_lookup(cls, "method") != func) return 12;

            printf("backend4-class-add-method-forwarded-class-arg-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-class-add-method-forwarded-class-arg-ok"


def test_backend4_class_new_resolves_forwarded_base_argument(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        typedef struct ProbeClassObject {
            PyObjectHeader h;
            const char *name;
            int32_t n_bases;
            struct ProbeClassObject **bases;
            int32_t n_mro;
            struct ProbeClassObject **mro;
            int32_t n_methods;
            void *methods;
            int32_t n_fields;
            const char **field_names;
            int32_t instance_size;
            int32_t type_tag_alloc;
            PyObject *del_method;
            PyObject *attrs;
        } ProbeClassObject;

        extern ProbeClassObject *py_class_new(
            const char *name,
            ProbeClassObject **bases,
            int32_t n_bases,
            const char **field_names,
            int32_t n_fields
        );

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            ProbeClassObject *base = py_class_new("ForwardedBase", 0, 0, 0, 0);
            if (base == 0) return 3;
            int64_t base_id = pcc_gc_object_id((PyObject *)base);

            pcc_gc_telemetry_reset();
            if (pcc_gc_select_relocation_set(64) <= 0) return 4;
            if (pcc_gc_relocation_set_contains((PyObject *)base) != 1) return 5;
            PyObject *moved_base_raw = pcc_gc_relocate_copy(
                (PyObject *)base,
                (int64_t)sizeof(ProbeClassObject)
            );
            if (moved_base_raw == 0) return 6;
            pcc_gc_release(moved_base_raw);

            ProbeClassObject *bases[1];
            bases[0] = base;
            ProbeClassObject *derived = py_class_new("Derived", bases, 1, 0, 0);
            if (derived == 0) return 7;
            if (derived->bases == 0 || derived->mro == 0) return 8;
            if (derived->bases[0] == base) return 9;
            if (pcc_gc_object_id((PyObject *)derived->bases[0]) != base_id) return 10;
            if (derived->n_mro < 2) return 11;
            if (derived->mro[1] == base) return 12;
            if (pcc_gc_object_id((PyObject *)derived->mro[1]) != base_id) return 13;

            pcc_gc_release((PyObject *)derived);
            printf("backend4-class-new-forwarded-base-arg-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-class-new-forwarded-base-arg-ok"


def test_backend4_class_new_resolves_forwarded_mro_entries(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        typedef struct ProbeClassObject {
            PyObjectHeader h;
            const char *name;
            int32_t n_bases;
            struct ProbeClassObject **bases;
            int32_t n_mro;
            struct ProbeClassObject **mro;
            int32_t n_methods;
            void *methods;
            int32_t n_fields;
            const char **field_names;
            int32_t instance_size;
            int32_t type_tag_alloc;
            PyObject *del_method;
            PyObject *attrs;
        } ProbeClassObject;

        extern ProbeClassObject *py_class_new(
            const char *name,
            ProbeClassObject **bases,
            int32_t n_bases,
            const char **field_names,
            int32_t n_fields
        );

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            ProbeClassObject *grand = py_class_new("Grand", 0, 0, 0, 0);
            if (grand == 0) return 3;
            ProbeClassObject *base_bases[1];
            base_bases[0] = grand;
            ProbeClassObject *base = py_class_new("Base", base_bases, 1, 0, 0);
            if (base == 0) return 4;
            int64_t grand_id = pcc_gc_object_id((PyObject *)grand);

            pcc_gc_telemetry_reset();
            if (pcc_gc_select_relocation_set(64) <= 0) return 5;
            if (pcc_gc_relocation_set_contains((PyObject *)grand) != 1) return 6;
            PyObject *moved_grand_raw = pcc_gc_relocate_copy(
                (PyObject *)grand,
                (int64_t)sizeof(ProbeClassObject)
            );
            if (moved_grand_raw == 0) return 7;
            pcc_gc_release(moved_grand_raw);

            ProbeClassObject *derived_bases[1];
            derived_bases[0] = base;
            ProbeClassObject *derived = py_class_new("Derived", derived_bases, 1, 0, 0);
            if (derived == 0) return 8;
            if (derived->n_mro < 3) return 9;
            if (derived->mro[2] == grand) return 10;
            if (pcc_gc_object_id((PyObject *)derived->mro[2]) != grand_id) return 11;

            pcc_gc_release((PyObject *)derived);
            printf("backend4-class-new-forwarded-mro-entry-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-class-new-forwarded-mro-entry-ok"


def test_backend4_exception_match_loads_forwarded_mro_entry(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_internal.h"
        #include <stdio.h>
        #include <stdlib.h>

        typedef struct ProbeClassObject {
            PyObjectHeader h;
            const char *name;
            int32_t n_bases;
            struct ProbeClassObject **bases;
            int32_t n_mro;
            struct ProbeClassObject **mro;
            int32_t n_methods;
            void *methods;
            int32_t n_fields;
            const char **field_names;
            int32_t instance_size;
            int32_t type_tag_alloc;
            PyObject *del_method;
            PyObject *attrs;
            struct ProbeClassObject *metaclass;
        } ProbeClassObject;

        static ProbeClassObject *new_probe_class(const char *name) {
            ProbeClassObject *cls = (ProbeClassObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbeClassObject), PY_TYPE_CLASS, 0
            );
            if (cls == 0) return 0;
            cls->name = name;
            cls->n_bases = 0;
            cls->bases = 0;
            cls->n_mro = 0;
            cls->mro = 0;
            cls->n_methods = 0;
            cls->methods = 0;
            cls->n_fields = 0;
            cls->field_names = 0;
            cls->instance_size = 0;
            cls->type_tag_alloc = PY_TYPE_USER + 90;
            cls->del_method = 0;
            cls->attrs = 0;
            cls->metaclass = 0;
            pcc_gc_publish_initialized((PyObject *)cls);
            return cls;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            PyObject *base_root = 0;
            PyObject *derived_root = 0;
            pcc_gc_scheduler_root_register(&base_root);
            pcc_gc_scheduler_root_register(&derived_root);

            ProbeClassObject *base = new_probe_class("Base");
            ProbeClassObject *derived = new_probe_class("Derived");
            if (base == 0 || derived == 0) return 3;
            derived->n_mro = 2;
            derived->mro = (ProbeClassObject **)calloc(2, sizeof(ProbeClassObject *));
            if (derived->mro == 0) return 4;
            pcc_gc_store_ptr((PyObject *)derived, (PyObject **)&derived->mro[0], (PyObject *)derived);
            pcc_gc_store_ptr((PyObject *)derived, (PyObject **)&derived->mro[1], (PyObject *)base);

            pcc_gc_store_root(&base_root, (PyObject *)base);
            pcc_gc_store_root(&derived_root, (PyObject *)derived);
            pcc_gc_release((PyObject *)base);
            pcc_gc_release((PyObject *)derived);

            if (pcc_gc_select_relocation_set(256) <= 0) return 5;
            if (pcc_gc_relocation_set_contains(base_root) != 1) return 6;
            PyObject *moved_base_raw = pcc_gc_relocate_copy(
                base_root,
                (int64_t)sizeof(ProbeClassObject)
            );
            if (moved_base_raw == 0) return 7;
            pcc_gc_release(moved_base_raw);

            PyObject *loaded_base = pcc_gc_load_ptr(0, &base_root);
            PyObject *loaded_derived = pcc_gc_load_ptr(0, &derived_root);
            if (loaded_base == 0 || loaded_derived == 0) return 8;
            if (loaded_base == (PyObject *)base) return 9;

            pcc_gc_telemetry_reset();
            if (py_exc_matches(loaded_derived, loaded_base) != 1) return 10;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_READ_BARRIERS) <= 0) return 11;

            ProbeClassObject *derived_probe = (ProbeClassObject *)loaded_derived;
            if ((PyObject *)derived_probe->mro[1] != loaded_base) return 12;

            pcc_gc_store_root(&derived_root, 0);
            pcc_gc_store_root(&base_root, 0);
            pcc_gc_scheduler_root_unregister(&derived_root);
            pcc_gc_scheduler_root_unregister(&base_root);
            printf("backend4-exception-match-mro-barrier-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-exception-match-mro-barrier-ok"


def test_backend4_dunder_lookup_loads_forwarded_instance_class_slot(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_internal.h"
        #include <stdio.h>

        typedef struct ProbeClassObject {
            PyObjectHeader h;
            const char *name;
            int32_t n_bases;
            struct ProbeClassObject **bases;
            int32_t n_mro;
            struct ProbeClassObject **mro;
            int32_t n_methods;
            void *methods;
            int32_t n_fields;
            const char **field_names;
            int32_t instance_size;
            int32_t type_tag_alloc;
            PyObject *attrs;
            PyObject *del_method;
        } ProbeClassObject;

        typedef struct {
            PyObjectHeader h;
            ProbeClassObject *cls;
            int32_t n_fields;
            PyObject *dict;
            PyObject *fields[1];
        } ProbeInstanceObject;

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            PyObject *cls_root = 0;
            PyObject *inst_root = 0;
            pcc_gc_scheduler_root_register(&cls_root);
            pcc_gc_scheduler_root_register(&inst_root);

            ProbeClassObject *cls = (ProbeClassObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbeClassObject), PY_TYPE_CLASS, 0
            );
            ProbeInstanceObject *inst = (ProbeInstanceObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbeInstanceObject), PY_TYPE_INSTANCE, 0
            );
            if (cls == 0 || inst == 0) return 3;
            cls->name = "DunderProbe";
            cls->n_bases = 0;
            cls->bases = 0;
            cls->n_mro = 0;
            cls->mro = 0;
            cls->n_methods = 0;
            cls->methods = 0;
            cls->n_fields = 0;
            cls->field_names = 0;
            cls->instance_size = (int32_t)sizeof(ProbeInstanceObject);
            cls->type_tag_alloc = PY_TYPE_INSTANCE;
            cls->attrs = 0;
            cls->del_method = 0;
            inst->cls = 0;
            inst->n_fields = 0;
            inst->dict = 0;
            inst->fields[0] = 0;
            pcc_gc_store_ptr((PyObject *)inst, (PyObject **)&inst->cls, (PyObject *)cls);

            pcc_gc_store_root(&cls_root, (PyObject *)cls);
            pcc_gc_store_root(&inst_root, (PyObject *)inst);
            pcc_gc_release((PyObject *)cls);
            pcc_gc_release((PyObject *)inst);

            pcc_gc_publish_initialized((PyObject *)cls);
            pcc_gc_publish_initialized((PyObject *)inst);
            if (pcc_gc_select_relocation_set(256) <= 0) return 4;
            if (pcc_gc_relocation_set_contains(cls_root) != 1) return 5;
            PyObject *moved_cls_raw = pcc_gc_relocate_copy(
                cls_root,
                (int64_t)sizeof(ProbeClassObject)
            );
            if (moved_cls_raw == 0) return 6;
            pcc_gc_release(moved_cls_raw);

            PyObject *loaded_cls = pcc_gc_load_ptr(0, &cls_root);
            PyObject *loaded_inst = pcc_gc_load_ptr(0, &inst_root);
            if (loaded_cls == 0 || loaded_inst == 0) return 7;
            if (loaded_cls == (PyObject *)cls) return 8;

            pcc_gc_telemetry_reset();
            PyObject *s = py_user_str_dispatch(loaded_inst);
            if (s != 0) return 9;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_READ_BARRIERS) <= 0) return 10;

            ProbeInstanceObject *loaded_probe = (ProbeInstanceObject *)loaded_inst;
            if ((PyObject *)loaded_probe->cls != loaded_cls) return 11;

            pcc_gc_store_root(&inst_root, 0);
            pcc_gc_store_root(&cls_root, 0);
            pcc_gc_scheduler_root_unregister(&inst_root);
            pcc_gc_scheduler_root_unregister(&cls_root);
            printf("backend4-dunder-instance-class-barrier-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-dunder-instance-class-barrier-ok"


def test_backend4_relocation_retargets_weakref_intrusive_list(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        typedef struct {
            PyObjectHeader h;
            PyObject *target;
            PyObject *callback;
            void *prev;
            void *next;
        } ProbeWeakRefObject;

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            PyObject *target_root = 0;
            PyObject *weakref_root = 0;
            pcc_gc_scheduler_root_register(&target_root);
            pcc_gc_scheduler_root_register(&weakref_root);

            PyObject *target = py_list_new(0);
            if (target == 0) return 3;
            py_list_append(target, py_int_from_i64(33));
            pcc_gc_store_root(&target_root, target);

            PyObject *wr = py_weakref_new(target, py_None);
            if (wr == 0) return 4;
            pcc_gc_store_root(&weakref_root, wr);
            pcc_gc_release(wr);
            pcc_gc_release(target);

            pcc_gc_telemetry_reset();
            if (pcc_gc_select_relocation_set(64) <= 0) return 5;
            if (pcc_gc_relocation_set_contains(weakref_root) != 1) return 6;
            PyObject *moved_raw = pcc_gc_relocate_copy(
                weakref_root,
                (int64_t)sizeof(ProbeWeakRefObject)
            );
            if (moved_raw == 0) return 7;
            pcc_gc_release(moved_raw);

            PyObject *loaded = pcc_gc_load_ptr(0, &weakref_root);
            if (loaded == 0) return 8;
            if (loaded == wr) return 9;

            PyObject *live = py_weakref_call(loaded);
            if (live == 0 || live == py_None) return 10;
            if (py_list_len(live) != 1) return 11;
            py_decref(live);

            py_weakref_invalidate(target_root);
            PyObject *dead = py_weakref_call(loaded);
            if (dead != py_None) return 12;
            py_decref(dead);
            if (pcc_gc_backend4_verify_no_old_addresses() != 1) return 13;

            pcc_gc_store_root(&weakref_root, 0);
            pcc_gc_store_root(&target_root, 0);
            pcc_gc_scheduler_root_unregister(&weakref_root);
            pcc_gc_scheduler_root_unregister(&target_root);
            printf("backend4-weakref-relocation-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-weakref-relocation-ok"


def test_backend4_weakref_callback_loads_forwarded_slot(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_internal.h"
        #include <stdio.h>

        static PyObject *probe_callback(PyObject *captures, PyObject *args) {
            (void)args;
            PyObject *seen = py_tuple_get(captures, 0);
            if (seen == 0) return 0;
            py_list_append(seen, py_int_from_i64(1));
            py_decref(seen);
            return pcc_gc_retain(py_None);
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            PyObject *target_root = 0;
            PyObject *weakref_root = 0;
            PyObject *callback_root = 0;
            PyObject *seen_root = 0;
            pcc_gc_scheduler_root_register(&target_root);
            pcc_gc_scheduler_root_register(&weakref_root);
            pcc_gc_scheduler_root_register(&callback_root);
            pcc_gc_scheduler_root_register(&seen_root);

            PyObject *target = py_list_new(0);
            PyObject *seen = py_list_new(0);
            PyObject *captures = py_tuple_new(1);
            if (target == 0 || seen == 0 || captures == 0) return 3;
            py_tuple_set_item(captures, 0, seen);

            PyObject *callback = py_func_new((void *)probe_callback, captures);
            if (callback == 0) return 4;
            PyObject *wr = py_weakref_new(target, callback);
            if (wr == 0) return 5;

            pcc_gc_store_root(&target_root, target);
            pcc_gc_store_root(&weakref_root, wr);
            pcc_gc_store_root(&callback_root, callback);
            pcc_gc_store_root(&seen_root, seen);
            pcc_gc_release(target);
            pcc_gc_release(seen);
            pcc_gc_release(captures);
            pcc_gc_release(callback);
            pcc_gc_release(wr);

            pcc_gc_telemetry_reset();
            if (pcc_gc_select_relocation_set(64) <= 0) return 6;
            if (pcc_gc_relocation_set_contains(callback_root) != 1) return 7;
            PyObject *moved_callback_raw = pcc_gc_relocate_copy(
                callback_root,
                (int64_t)sizeof(PyFuncObject)
            );
            if (moved_callback_raw == 0) return 8;
            pcc_gc_release(moved_callback_raw);

            PyObject *loaded_callback = pcc_gc_load_ptr(0, &callback_root);
            if (loaded_callback == 0) return 9;
            if (loaded_callback == callback) return 10;

            py_weakref_invalidate(target_root);

            PyWeakRefObject *loaded_wr = (PyWeakRefObject *)pcc_gc_load_ptr(
                0,
                &weakref_root
            );
            if (loaded_wr == 0) return 11;
            if (loaded_wr->callback != loaded_callback) return 12;

            PyObject *loaded_seen = pcc_gc_load_ptr(0, &seen_root);
            if (loaded_seen == 0) return 13;
            if (py_list_len(loaded_seen) != 1) return 14;
            if (pcc_gc_backend4_verify_no_old_addresses() != 1) return 15;

            pcc_gc_store_root(&seen_root, 0);
            pcc_gc_store_root(&callback_root, 0);
            pcc_gc_store_root(&weakref_root, 0);
            pcc_gc_store_root(&target_root, 0);
            pcc_gc_scheduler_root_unregister(&seen_root);
            pcc_gc_scheduler_root_unregister(&callback_root);
            pcc_gc_scheduler_root_unregister(&weakref_root);
            pcc_gc_scheduler_root_unregister(&target_root);
            printf("backend4-weakref-callback-barrier-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-weakref-callback-barrier-ok"


def test_backend4_remap_rewrites_weakref_target_slot_before_forwarding_retirement(
    tmp_path,
):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_internal.h"
        #include <stdio.h>

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            PyObject *target_root = 0;
            PyObject *weakref_root = 0;
            pcc_gc_scheduler_root_register(&target_root);
            pcc_gc_scheduler_root_register(&weakref_root);

            PyObject *target = py_list_new(0);
            if (target == 0) return 3;
            py_list_append(target, py_int_from_i64(44));
            PyObject *wr = py_weakref_new(target, py_None);
            if (wr == 0) return 4;
            pcc_gc_store_root(&target_root, target);
            pcc_gc_store_root(&weakref_root, wr);
            pcc_gc_release(target);
            pcc_gc_release(wr);
            PyObject *old_target = target_root;

            pcc_gc_reset_relocation_set();
            if (pcc_gc_select_relocation_set(65536) <= 0) return 5;
            if (pcc_gc_relocation_set_contains(old_target) != 1) return 6;
            if (pcc_gc_backend4_evacuation_drain(65536) <= 0) return 7;

            (void)pcc_gc_collect(0);
            PyObject *loaded_target = pcc_gc_load_ptr(0, &target_root);
            PyWeakRefObject *loaded_wr = (PyWeakRefObject *)pcc_gc_load_ptr(
                0,
                &weakref_root
            );
            if (loaded_target == 0 || loaded_wr == 0) return 8;
            if (loaded_target == old_target) return 9;
            if (loaded_wr->target != loaded_target) return 10;
            if (loaded_wr->target == old_target) return 11;

            (void)pcc_gc_collect(0);
            (void)pcc_gc_collect(0);
            if (pcc_gc_backend4_forwarding_entries() != 0) return 12;
            PyObject *live = py_weakref_call((PyObject *)loaded_wr);
            if (live != loaded_target) return 13;
            py_decref(live);

            pcc_gc_store_root(&weakref_root, 0);
            pcc_gc_store_root(&target_root, 0);
            pcc_gc_scheduler_root_unregister(&weakref_root);
            pcc_gc_scheduler_root_unregister(&target_root);
            printf("backend4-weakref-target-remap-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-weakref-target-remap-ok"


def test_backend4_gc_callback_remove_loads_forwarded_list_slot(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_internal.h"
        #include <stdio.h>

        static PyObject *probe_callback(PyObject *captures, PyObject *args) {
            (void)captures;
            (void)args;
            return py_None;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            PyObject *callback_root = 0;
            pcc_gc_scheduler_root_register(&callback_root);

            PyObject *captures = py_tuple_new(0);
            if (captures == 0) return 3;
            PyObject *callback = py_func_new((void *)probe_callback, captures);
            if (callback == 0) return 4;
            py_gc_callbacks_append(callback);
            pcc_gc_store_root(&callback_root, callback);
            pcc_gc_release(captures);
            pcc_gc_release(callback);

            if (pcc_gc_select_relocation_set(64) <= 0) return 5;
            if (pcc_gc_relocation_set_contains(callback_root) != 1) return 6;
            PyObject *moved_raw = pcc_gc_relocate_copy(
                callback_root,
                (int64_t)sizeof(PyFuncObject)
            );
            if (moved_raw == 0) return 12;
            pcc_gc_release(moved_raw);

            PyObject *loaded_callback = pcc_gc_load_ptr(0, &callback_root);
            if (loaded_callback == 0) return 7;
            if (loaded_callback == callback) return 8;

            pcc_gc_telemetry_reset();
            py_gc_callbacks_remove(loaded_callback);
            if (pcc_gc_telemetry(PCC_GC_COUNTER_READ_BARRIERS) <= 0) return 9;

            PyObject *callbacks = py_gc_callbacks_list();
            if (callbacks == 0) return 10;
            if (py_list_len(callbacks) != 0) return 11;
            pcc_gc_release(callbacks);

            pcc_gc_store_root(&callback_root, 0);
            pcc_gc_scheduler_root_unregister(&callback_root);
            printf("backend4-gc-callback-remove-barrier-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-gc-callback-remove-barrier-ok"


def test_backend4_relocation_preserves_unstarted_thread_wrapper(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        typedef struct {
            PyObjectHeader h;
            void *handle;
            PyObject *callable;
            PyObject *args;
            PyObject *result;
            int64_t started;
            int64_t joined;
            int64_t finished;
        } ProbeThreadObject;

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            PyObject *root = 0;
            pcc_gc_scheduler_root_register(&root);

            PyObject *args = py_tuple_new(1);
            PyObject *arg = py_int_from_i64(44);
            if (args == 0 || arg == 0) return 3;
            py_tuple_set_item(args, 0, arg);

            PyObject *thread = py_threading_thread_new(py_None, args);
            if (thread == 0) return 4;
            pcc_gc_store_root(&root, thread);
            pcc_gc_release(thread);
            pcc_gc_release(args);
            pcc_gc_release(arg);

            pcc_gc_telemetry_reset();
            if (pcc_gc_select_relocation_set(64) <= 0) return 5;
            if (pcc_gc_relocation_set_contains(root) != 1) return 6;
            PyObject *moved_raw = pcc_gc_relocate_copy(
                root,
                (int64_t)sizeof(ProbeThreadObject)
            );
            if (moved_raw == 0) return 7;
            pcc_gc_release(moved_raw);

            PyObject *loaded = pcc_gc_load_ptr(0, &root);
            if (loaded == 0) return 8;
            if (loaded == thread) return 9;
            ProbeThreadObject *moved = (ProbeThreadObject *)loaded;
            if (moved->handle != 0) return 10;
            if (moved->callable != py_None) return 11;
            if (moved->args == 0) return 12;
            if (moved->result != 0) return 13;
            if (moved->started != 0 || moved->joined != 0 || moved->finished != 0) return 14;

            PyObject *loaded_arg = py_tuple_get(moved->args, 0);
            if (py_int_to_i64(loaded_arg, 0) != 44) return 15;
            if (pcc_gc_backend4_verify_no_old_addresses() != 1) return 16;

            pcc_gc_release(loaded_arg);
            pcc_gc_store_root(&root, 0);
            pcc_gc_scheduler_root_unregister(&root);
            printf("backend4-thread-relocation-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-thread-relocation-ok"


def test_backend4_thread_start_loads_forwarded_args_slot(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        typedef struct {
            PyObjectHeader h;
            void *handle;
            PyObject *callable;
            PyObject *args;
            PyObject *result;
            int64_t started;
            int64_t joined;
            int64_t finished;
        } ProbeThreadObject;

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            PyObject *items[];
        } ProbeTupleObject;

        static PyObject *probe_thread_entry(PyObject *captures, PyObject *args) {
            (void)captures;
            return py_tuple_get(args, 0);
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            PyObject *thread_root = 0;
            PyObject *args_root = 0;
            pcc_gc_scheduler_root_register(&thread_root);
            pcc_gc_scheduler_root_register(&args_root);

            PyObject *captures = py_tuple_new(0);
            PyObject *callable = py_func_new((void *)probe_thread_entry, captures);
            PyObject *arg = py_str_new("thread-arg", 10);
            PyObject *args = py_tuple_new(1);
            if (captures == 0 || callable == 0 || arg == 0 || args == 0) return 3;
            py_tuple_set_item(args, 0, arg);

            PyObject *thread = py_threading_thread_new(callable, args);
            if (thread == 0) return 4;
            pcc_gc_store_root(&thread_root, thread);
            pcc_gc_store_root(&args_root, args);
            pcc_gc_release(thread);
            pcc_gc_release(args);
            pcc_gc_release(arg);
            pcc_gc_release(callable);
            pcc_gc_release(captures);

            pcc_gc_telemetry_reset();
            if (pcc_gc_select_relocation_set(128) <= 0) return 5;
            if (pcc_gc_relocation_set_contains(args_root) != 1) return 6;
            PyObject *moved_args_raw = pcc_gc_relocate_copy(
                args_root,
                (int64_t)sizeof(ProbeTupleObject) + (int64_t)sizeof(PyObject *)
            );
            if (moved_args_raw == 0) return 7;
            pcc_gc_release(moved_args_raw);

            PyObject *loaded_args = pcc_gc_load_ptr(0, &args_root);
            if (loaded_args == 0) return 8;
            if (loaded_args == args) return 9;

            if (py_threading_thread_start(thread_root) != 0) return 10;

            ProbeThreadObject *loaded_thread = (ProbeThreadObject *)pcc_gc_load_ptr(
                0,
                &thread_root
            );
            if (loaded_thread == 0) return 11;
            if (loaded_thread->args != loaded_args) return 12;
            if (loaded_thread->result == 0) return 13;
            PyObject *expected = py_str_new("thread-arg", 10);
            if (expected == 0) return 14;
            if (py_str_eq(loaded_thread->result, expected) != 1) return 15;
            py_decref(expected);
            if (loaded_thread->started != 1 || loaded_thread->joined != 1) return 16;
            if (loaded_thread->finished != 1) return 17;
            if (pcc_gc_backend4_verify_no_old_addresses() != 1) return 18;

            pcc_gc_store_root(&args_root, 0);
            pcc_gc_store_root(&thread_root, 0);
            pcc_gc_scheduler_root_unregister(&args_root);
            pcc_gc_scheduler_root_unregister(&thread_root);
            printf("backend4-thread-args-barrier-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-thread-args-barrier-ok"


def test_backend4_module_attr_slot_helper_loads_forwarded_value(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            PyObject *slot = 0;
            pcc_gc_scheduler_root_register(&slot);

            PyObject *value = py_list_new(0);
            PyObject *default_value = py_str_new("default", 7);
            if (value == 0 || default_value == 0) return 3;
            int64_t value_id = pcc_gc_object_id(value);
            pcc_gc_store_root(&slot, value);
            pcc_gc_release(value);

            pcc_gc_telemetry_reset();
            if (pcc_gc_select_relocation_set(64) <= 0) return 4;
            if (pcc_gc_relocation_set_contains(slot) != 1) return 5;
            PyObject *moved_raw = pcc_gc_relocate_copy(
                slot,
                (int64_t)sizeof(ProbeListObject)
            );
            if (moved_raw == 0) return 6;
            pcc_gc_release(moved_raw);

            PyObject *loaded = pcc_gc_load_ptr(0, &slot);
            if (loaded == 0) return 7;
            if (loaded == value) return 8;

            PyObject *got = py_module_attr_value_or_default(&slot, default_value);
            if (got != loaded) return 9;
            if (slot != loaded) return 10;
            if (pcc_gc_object_id(got) != value_id) return 11;
            (void)pcc_gc_step(256);
            (void)pcc_gc_step(256);
            if (pcc_gc_backend4_verify_no_old_addresses() != 1) return 13;

            pcc_gc_store_root(&slot, 0);
            pcc_gc_scheduler_root_unregister(&slot);
            printf("backend4-module-attr-slot-barrier-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-module-attr-slot-barrier-ok"


def test_backend4_relocation_skips_thread_wrapper_with_native_handle(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdint.h>
        #include <stdio.h>

        typedef struct {
            PyObjectHeader h;
            void *handle;
            PyObject *callable;
            PyObject *args;
            PyObject *result;
            int64_t started;
            int64_t joined;
            int64_t finished;
        } ProbeThreadObject;

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            PyObject *root = 0;
            pcc_gc_scheduler_root_register(&root);

            ProbeThreadObject *thread = (ProbeThreadObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbeThreadObject),
                PY_TYPE_THREAD,
                0x100
            );
            if (thread == 0) return 3;
            thread->handle = (void *)(uintptr_t)0x1;
            thread->callable = 0;
            thread->args = 0;
            thread->result = 0;
            thread->started = 1;
            thread->joined = 0;
            thread->finished = 0;
            pcc_gc_store_root(&root, (PyObject *)thread);

            pcc_gc_telemetry_reset();
            pcc_gc_publish_initialized((PyObject *)thread);
            if (pcc_gc_select_relocation_set(64) != 0) return 4;
            if (pcc_gc_relocation_set_contains((PyObject *)thread) != 0) return 5;
            if (pcc_gc_relocate_copy((PyObject *)thread, (int64_t)sizeof(ProbeThreadObject)) != 0) return 6;
            if (pcc_gc_backend4_verify_no_old_addresses() != 1) return 7;

            pcc_gc_store_root(&root, 0);
            pcc_gc_scheduler_root_unregister(&root);
            printf("backend4-thread-native-handle-skip-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-thread-native-handle-skip-ok"


def test_backend4_relocation_skips_native_handle_wrappers(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdint.h>
        #include <stdio.h>

        typedef struct {
            PyObjectHeader h;
            void *fp;
            int closed;
        } ProbeFileObject;

        typedef struct {
            PyObjectHeader h;
            void *mutex;
        } ProbeLockObject;

        typedef struct {
            PyObjectHeader h;
            void *mutex;
            int64_t owner;
            int64_t depth;
        } ProbeRLockObject;

        typedef struct {
            PyObjectHeader h;
            void *mutex;
            void *cond;
            int64_t value;
        } ProbeCondLikeObject;

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            ProbeFileObject *file = (ProbeFileObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbeFileObject), PY_TYPE_FILE, 0
            );
            ProbeLockObject *lock = (ProbeLockObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbeLockObject), PY_TYPE_THREAD_LOCK, 0
            );
            ProbeRLockObject *rlock = (ProbeRLockObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbeRLockObject), PY_TYPE_THREAD_RLOCK, 0
            );
            ProbeCondLikeObject *event = (ProbeCondLikeObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbeCondLikeObject), PY_TYPE_THREAD_EVENT, 0
            );
            ProbeCondLikeObject *cond = (ProbeCondLikeObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbeCondLikeObject), PY_TYPE_THREAD_CONDITION, 0
            );
            ProbeCondLikeObject *sem = (ProbeCondLikeObject *)pcc_gc_alloc(
                (int64_t)sizeof(ProbeCondLikeObject), PY_TYPE_THREAD_SEMAPHORE, 0
            );
            if (
                file == 0 || lock == 0 || rlock == 0
                || event == 0 || cond == 0 || sem == 0
            ) return 3;

            file->fp = (void *)(uintptr_t)0x1;
            file->closed = 0;
            lock->mutex = (void *)(uintptr_t)0x2;
            rlock->mutex = (void *)(uintptr_t)0x3;
            rlock->owner = 0;
            rlock->depth = 0;
            event->mutex = (void *)(uintptr_t)0x4;
            event->cond = (void *)(uintptr_t)0x5;
            event->value = 0;
            cond->mutex = (void *)(uintptr_t)0x6;
            cond->cond = (void *)(uintptr_t)0x7;
            cond->value = 0;
            sem->mutex = (void *)(uintptr_t)0x8;
            sem->cond = (void *)(uintptr_t)0x9;
            sem->value = 1;

            pcc_gc_telemetry_reset();
            pcc_gc_publish_initialized((PyObject *)file);
            pcc_gc_publish_initialized((PyObject *)lock);
            pcc_gc_publish_initialized((PyObject *)rlock);
            pcc_gc_publish_initialized((PyObject *)event);
            pcc_gc_publish_initialized((PyObject *)cond);
            pcc_gc_publish_initialized((PyObject *)sem);
            if (pcc_gc_select_relocation_set(64) != 0) return 4;
            if (pcc_gc_relocation_set_contains((PyObject *)file) != 0) return 5;
            if (pcc_gc_relocation_set_contains((PyObject *)lock) != 0) return 6;
            if (pcc_gc_relocation_set_contains((PyObject *)rlock) != 0) return 7;
            if (pcc_gc_relocation_set_contains((PyObject *)event) != 0) return 8;
            if (pcc_gc_relocation_set_contains((PyObject *)cond) != 0) return 9;
            if (pcc_gc_relocation_set_contains((PyObject *)sem) != 0) return 10;

            if (
                pcc_gc_relocate_copy(
                    (PyObject *)file,
                    (int64_t)sizeof(ProbeFileObject)
                ) != 0
            ) return 11;
            if (
                pcc_gc_relocate_copy(
                    (PyObject *)lock,
                    (int64_t)sizeof(ProbeLockObject)
                ) != 0
            ) return 12;
            if (
                pcc_gc_relocate_copy(
                    (PyObject *)rlock,
                    (int64_t)sizeof(ProbeRLockObject)
                ) != 0
            ) return 13;
            if (
                pcc_gc_relocate_copy(
                    (PyObject *)event,
                    (int64_t)sizeof(ProbeCondLikeObject)
                ) != 0
            ) return 14;
            if (
                pcc_gc_relocate_copy(
                    (PyObject *)cond,
                    (int64_t)sizeof(ProbeCondLikeObject)
                ) != 0
            ) return 15;
            if (
                pcc_gc_relocate_copy(
                    (PyObject *)sem,
                    (int64_t)sizeof(ProbeCondLikeObject)
                ) != 0
            ) return 16;

            printf("backend4-native-handle-wrapper-skip-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-native-handle-wrapper-skip-ok"


def test_backend4_fragmentation_score_tracks_live_evacuation_debt(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            PyObject *root = 0;
            pcc_gc_scheduler_root_register(&root);
            PyObject *s = py_list_new(0);
            int64_t stable = pcc_gc_object_id(s);
            if (stable <= 0) return 10;
            pcc_gc_store_root(&root, s);
            pcc_gc_release(s);

            pcc_gc_telemetry_reset();
            if (pcc_gc_backend4_stable_id_entries() != 1) return 11;
            if (pcc_gc_backend4_forwarding_entries() != 0) return 12;
            if (pcc_gc_backend4_fragmentation_score() != 0) return 13;

            if (pcc_gc_select_relocation_set(1) != 1) return 14;
            if (pcc_gc_relocation_set_contains(s) != 1) return 15;
            PyObject *moved = pcc_gc_relocate_copy(
                s,
                (int64_t)sizeof(ProbeListObject)
            );
            if (moved == 0) return 16;
            pcc_gc_release(moved);
            if (pcc_gc_telemetry(PCC_GC_COUNTER_RELOCATION_FORWARDS) != 1) return 17;
            if (pcc_gc_backend4_forwarding_entries() != 1) return 24;
            if (pcc_gc_backend4_fragmentation_score() != 1) return 25;

            PyObject *loaded = pcc_gc_load_ptr(0, &root);
            if (loaded == 0) return 18;
            if (loaded == s) return 19;
            if (pcc_gc_object_id(loaded) != stable) return 20;
            if (pcc_gc_backend4_forwarding_entries() != 1) return 21;
            if (pcc_gc_backend4_stable_id_entries() != 2) return 22;
            if (pcc_gc_backend4_fragmentation_score() != 1) return 23;
            (void)pcc_gc_step(256);
            (void)pcc_gc_step(256);
            if (pcc_gc_backend4_forwarding_entries() != 0) return 26;
            if (pcc_gc_backend4_stable_id_entries() != 1) return 27;
            if (pcc_gc_backend4_fragmentation_score() != 0) return 23;

            pcc_gc_store_root(&root, 0);
            pcc_gc_scheduler_root_unregister(&root);
            printf("backend4-fragmentation-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-fragmentation-ok"


def test_backend4_genzgc_page_policy_records_candidates_and_evacuated_bytes(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        typedef struct ProbeClassObject {
            PyObjectHeader h;
            const char *name;
            int32_t n_bases;
            struct ProbeClassObject **bases;
            int32_t n_mro;
            struct ProbeClassObject **mro;
            int32_t n_methods;
            void *methods;
            int32_t n_fields;
            const char **field_names;
            int32_t instance_size;
            int32_t type_tag_alloc;
            PyObject *del_method;
            PyObject *attrs;
            PyObject *metaclass;
        } ProbeClassObject;

        static PyObject *new_sized_class(int64_t size, const char *name, int32_t tag) {
            ProbeClassObject *cls = (ProbeClassObject *)pcc_gc_alloc(
                size,
                PY_TYPE_CLASS,
                0x100
            );
            if (cls == 0) return 0;
            cls->name = name;
            cls->n_bases = 0;
            cls->bases = 0;
            cls->n_mro = 0;
            cls->mro = 0;
            cls->n_methods = 0;
            cls->methods = 0;
            cls->n_fields = 0;
            cls->field_names = 0;
            cls->instance_size = 32;
            cls->type_tag_alloc = tag;
            cls->del_method = 0;
            cls->attrs = 0;
            cls->metaclass = 0;
            pcc_gc_publish_initialized((PyObject *)cls);
            return (PyObject *)cls;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            PyObject *small_root = 0;
            PyObject *medium_root = 0;
            PyObject *large_root = 0;
            pcc_gc_scheduler_root_register(&small_root);
            pcc_gc_scheduler_root_register(&medium_root);
            pcc_gc_scheduler_root_register(&large_root);

            PyObject *small = new_sized_class(128, "SmallPage", 1101);
            PyObject *medium = new_sized_class(8192, "MediumPage", 1102);
            PyObject *large = new_sized_class(70000, "LargePage", 1103);
            if (small == 0 || medium == 0 || large == 0) return 3;
            pcc_gc_store_root(&small_root, small);
            pcc_gc_store_root(&medium_root, medium);
            pcc_gc_store_root(&large_root, large);

            pcc_gc_telemetry_reset();
            if (pcc_gc_select_relocation_set(32) != 2) return 4;
            if (pcc_gc_relocation_set_size() != 2) return 5;
            if (pcc_gc_relocation_set_contains(small) != 1) return 6;
            if (pcc_gc_relocation_set_contains(medium) != 1) return 24;
            if (pcc_gc_relocation_set_contains(large) != 0) return 7;
            if (pcc_gc_backend4_evacuation_candidate_score() != 2) return 8;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_EVACUATION_CANDIDATES) != 2) return 9;
            if (pcc_gc_backend4_small_page_candidate_score() != 1) return 25;
            if (pcc_gc_backend4_medium_page_candidate_score() != 1) return 26;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_SMALL_PAGE_CANDIDATES) != 1) return 27;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_MEDIUM_PAGE_CANDIDATES) != 1) return 28;
            if (pcc_gc_backend4_evacuation_candidate_bytes() != 8320) return 29;
            if (pcc_gc_backend4_small_page_candidate_bytes() != 128) return 30;
            if (pcc_gc_backend4_medium_page_candidate_bytes() != 8192) return 31;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_EVACUATION_CANDIDATE_BYTES) != 8320) return 32;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_SMALL_PAGE_CANDIDATE_BYTES) != 128) return 33;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_MEDIUM_PAGE_CANDIDATE_BYTES) != 8192) return 34;
            if (pcc_gc_backend4_page_pressure_score() != 78320) return 35;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_PAGE_PRESSURE_SCORE) != 78320) return 36;
            if (pcc_gc_backend4_page_policy_score() != 2) return 10;
            if (pcc_gc_backend4_large_object_defer_score() != 1) return 15;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_LARGE_OBJECT_DEFERS) != 1) return 16;
            if (pcc_gc_backend4_large_object_deferred_bytes() != 70000) return 18;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_LARGE_OBJECT_DEFERRED_BYTES) != 70000) return 19;
            if (pcc_gc_backend4_large_object_reconsiderations() != 0) return 51;

            pcc_gc_telemetry_reset();
            if (pcc_gc_backend4_evacuation_candidate_score() != 2) return 43;
            if (pcc_gc_backend4_evacuation_candidate_bytes() != 8320) return 44;
            if (pcc_gc_backend4_small_page_candidate_score() != 1) return 45;
            if (pcc_gc_backend4_medium_page_candidate_score() != 1) return 46;
            if (pcc_gc_backend4_large_object_defer_score() != 0) return 47;
            if (pcc_gc_backend4_large_object_deferred_bytes() != 0) return 48;
            if (pcc_gc_backend4_large_object_reconsiderations() != 1) return 52;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_LARGE_OBJECT_RECONSIDERATIONS) != 1) return 53;

            if (pcc_gc_select_relocation_set(32) != 0) return 20;
            if (pcc_gc_backend4_large_object_defer_score() != 1) return 21;
            if (pcc_gc_backend4_large_object_deferred_bytes() != 70000) return 22;
            if (pcc_gc_backend4_evacuation_candidate_score() != 2) return 49;
            if (pcc_gc_backend4_evacuation_candidate_bytes() != 8320) return 50;

            if (pcc_gc_step(2) < 2) return 11;
            if (pcc_gc_backend4_evacuated_bytes() != 8320) return 12;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_EVACUATED_BYTES) != 8320) return 13;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_PAGE_POLICY_SCORE) != 8322) return 14;
            if (pcc_gc_backend4_large_object_defer_score() != 1) return 17;
            if (pcc_gc_backend4_large_object_deferred_bytes() != 70000) return 23;

            pcc_gc_telemetry_reset();
            if (pcc_gc_backend4_large_object_defer_score() != 0) return 37;
            if (pcc_gc_backend4_large_object_deferred_bytes() != 0) return 38;
            if (pcc_gc_backend4_large_object_reconsiderations() != 1) return 54;
            if (pcc_gc_select_relocation_set(32) != 0) return 39;
            if (pcc_gc_backend4_large_object_defer_score() != 1) return 40;
            if (pcc_gc_backend4_large_object_deferred_bytes() != 70000) return 41;
            if (pcc_gc_backend4_page_pressure_score() != 70000) return 42;

            pcc_gc_store_root(&small_root, 0);
            pcc_gc_store_root(&medium_root, 0);
            pcc_gc_store_root(&large_root, 0);
            pcc_gc_scheduler_root_unregister(&small_root);
            pcc_gc_scheduler_root_unregister(&medium_root);
            pcc_gc_scheduler_root_unregister(&large_root);
            printf("backend4-page-policy-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-page-policy-ok"


def test_backend4_genzgc_reset_relocation_set_clears_page_policy_shape(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        typedef struct ProbeClassObject {
            PyObjectHeader h;
            const char *name;
            int32_t n_bases;
            struct ProbeClassObject **bases;
            int32_t n_mro;
            struct ProbeClassObject **mro;
            int32_t n_methods;
            void *methods;
            int32_t n_fields;
            const char **field_names;
            int32_t instance_size;
            int32_t type_tag_alloc;
            PyObject *del_method;
            PyObject *attrs;
            PyObject *metaclass;
        } ProbeClassObject;

        static PyObject *new_sized_class(int64_t size, const char *name, int32_t tag) {
            ProbeClassObject *cls = (ProbeClassObject *)pcc_gc_alloc(
                size,
                PY_TYPE_CLASS,
                0x100
            );
            if (cls == 0) return 0;
            cls->name = name;
            cls->n_bases = 0;
            cls->bases = 0;
            cls->n_mro = 0;
            cls->mro = 0;
            cls->n_methods = 0;
            cls->methods = 0;
            cls->n_fields = 0;
            cls->field_names = 0;
            cls->instance_size = 32;
            cls->type_tag_alloc = tag;
            cls->del_method = 0;
            cls->attrs = 0;
            cls->metaclass = 0;
            pcc_gc_publish_initialized((PyObject *)cls);
            return (PyObject *)cls;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            PyObject *small_root = 0;
            PyObject *medium_root = 0;
            pcc_gc_scheduler_root_register(&small_root);
            pcc_gc_scheduler_root_register(&medium_root);

            PyObject *small = new_sized_class(128, "ResetSmallPage", 1111);
            PyObject *medium = new_sized_class(8192, "ResetMediumPage", 1112);
            if (small == 0 || medium == 0) return 3;
            pcc_gc_store_root(&small_root, small);
            pcc_gc_store_root(&medium_root, medium);

            pcc_gc_telemetry_reset();
            if (pcc_gc_select_relocation_set(32) != 2) return 4;
            if (pcc_gc_relocation_set_size() != 2) return 5;
            if (pcc_gc_backend4_evacuation_candidate_score() != 2) return 6;
            if (pcc_gc_backend4_evacuation_candidate_bytes() != 8320) return 7;
            if (pcc_gc_backend4_small_page_candidate_score() != 1) return 8;
            if (pcc_gc_backend4_medium_page_candidate_score() != 1) return 9;

            pcc_gc_reset_relocation_set();
            if (pcc_gc_relocation_set_size() != 0) return 10;
            if (pcc_gc_backend4_evacuation_candidate_score() != 0) return 11;
            if (pcc_gc_backend4_evacuation_candidate_bytes() != 0) return 12;
            if (pcc_gc_backend4_small_page_candidate_score() != 0) return 13;
            if (pcc_gc_backend4_medium_page_candidate_score() != 0) return 14;
            if (pcc_gc_backend4_small_page_candidate_bytes() != 0) return 15;
            if (pcc_gc_backend4_medium_page_candidate_bytes() != 0) return 16;
            if (pcc_gc_backend4_page_pressure_score() != 0) return 17;

            pcc_gc_store_root(&small_root, 0);
            pcc_gc_store_root(&medium_root, 0);
            pcc_gc_scheduler_root_unregister(&small_root);
            pcc_gc_scheduler_root_unregister(&medium_root);
            printf("backend4-reset-relocation-shape-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-reset-relocation-shape-ok"


def test_backend4_genzgc_store_barrier_remembers_old_to_young_slot(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdint.h>
        #include <stdio.h>
        #include <stdlib.h>
        #include <string.h>

        enum {
            PY_FLAG_GC_YOUNG = 0x80,
            PY_FLAG_GC_OLD = 0x100,
            PY_FLAG_GC_REMEMBERED = 0x200
        };

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        static PyObject *new_young_list(void) {
            ProbeListObject *obj = (ProbeListObject *)pcc_gc_alloc(
                sizeof(ProbeListObject),
                PY_TYPE_LIST,
                PY_FLAG_GC_YOUNG
            );
            if (obj == 0) return 0;
            obj->length = 0;
            obj->capacity = 0;
            obj->items = 0;
            pcc_gc_publish_initialized((PyObject *)obj);
            return (PyObject *)obj;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            ProbeListObject *owner = (ProbeListObject *)pcc_gc_alloc(
                sizeof(ProbeListObject),
                PY_TYPE_LIST,
                PY_FLAG_GC_OLD
            );
            if (owner == 0) return 3;
            owner->length = 2;
            owner->capacity = 2;
            owner->items = (PyObject **)calloc(2, sizeof(PyObject *));
            if (owner->items == 0) return 4;

            PyObject *child = new_young_list();
            if (child == 0) return 5;
            PyObject *child2 = new_young_list();
            if (child2 == 0) return 15;
            pcc_gc_store_ptr((PyObject *)owner, &owner->items[0], child);
            pcc_gc_store_ptr((PyObject *)owner, &owner->items[1], child2);
            pcc_gc_store_ptr((PyObject *)owner, &owner->items[0], child);

            if ((owner->h.flags & PY_FLAG_GC_REMEMBERED) == 0) return 6;
            if (pcc_gc_backend4_generation_barrier_score() != 2) return 7;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_STORE_BARRIERS) != 2) return 8;
            if (pcc_gc_backend4_store_buffer_entries() != 2) return 12;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_STORE_BUFFER_ENTRIES) != 2) return 13;
            if (pcc_gc_backend4_store_buffer_duplicate_skips() != 1) return 26;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_STORE_BUFFER_DUPLICATE_SKIPS) != 1) return 27;
            if (pcc_gc_backend4_store_buffer_high_water() != 2) return 28;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_STORE_BUFFER_HIGH_WATER) != 2) return 29;
            if (pcc_gc_backend4_store_buffer_owner_fanout_high_water() != 2) return 31;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_STORE_BUFFER_OWNER_FANOUT_HIGH_WATER) != 2) return 32;

            pcc_gc_publish_initialized((PyObject *)owner);
            if (pcc_gc_step(1) < 1) return 9;
            if ((owner->h.flags & PY_FLAG_GC_REMEMBERED) == 0) return 10;
            if (pcc_gc_backend4_store_buffer_entries() != 1) return 14;
            if (pcc_gc_backend4_store_buffer_drain_batches() != 1) return 20;
            if (pcc_gc_backend4_store_buffer_drained_entries() != 1) return 21;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_STORE_BUFFER_DRAIN_BATCHES) != 1) return 22;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_STORE_BUFFER_DRAINED_ENTRIES) != 1) return 23;
            if (pcc_gc_backend4_store_buffer_incomplete_drains() != 1) return 34;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_STORE_BUFFER_INCOMPLETE_DRAINS) != 1) return 35;

            if (pcc_gc_step(1) < 1) return 16;
            if ((owner->h.flags & PY_FLAG_GC_REMEMBERED) != 0) return 17;
            if ((((PyObjectHeader *)child)->flags & PY_FLAG_GC_OLD) == 0) return 11;
            if ((((PyObjectHeader *)child2)->flags & PY_FLAG_GC_OLD) == 0) return 18;
            if (pcc_gc_backend4_store_buffer_entries() != 0) return 19;
            if (pcc_gc_backend4_store_buffer_drain_batches() != 2) return 24;
            if (pcc_gc_backend4_store_buffer_drained_entries() != 2) return 25;
            if (pcc_gc_backend4_store_buffer_high_water() != 2) return 30;
            if (pcc_gc_backend4_store_buffer_owner_fanout_high_water() != 2) return 33;
            if (pcc_gc_backend4_store_buffer_incomplete_drains() != 1) return 36;

            pcc_gc_store_ptr((PyObject *)owner, &owner->items[0], 0);
            pcc_gc_store_ptr((PyObject *)owner, &owner->items[1], 0);
            pcc_gc_release(child);
            pcc_gc_release(child2);
            pcc_gc_release((PyObject *)owner);
            printf("backend4-genzgc-store-barrier-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-genzgc-store-barrier-ok"


def test_backend4_genzgc_store_buffer_owner_count_high_water(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdint.h>
        #include <stdio.h>
        #include <stdlib.h>

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

        static ProbeListObject *new_owner(void) {
            ProbeListObject *owner = (ProbeListObject *)pcc_gc_alloc(
                sizeof(ProbeListObject),
                PY_TYPE_LIST,
                PY_FLAG_GC_OLD
            );
            if (owner == 0) return 0;
            owner->length = 1;
            owner->capacity = 1;
            owner->items = (PyObject **)calloc(1, sizeof(PyObject *));
            if (owner->items == 0) return 0;
            pcc_gc_publish_initialized((PyObject *)owner);
            return owner;
        }

        static PyObject *new_young_list(void) {
            ProbeListObject *obj = (ProbeListObject *)pcc_gc_alloc(
                sizeof(ProbeListObject),
                PY_TYPE_LIST,
                PY_FLAG_GC_YOUNG
            );
            if (obj == 0) return 0;
            obj->length = 0;
            obj->capacity = 0;
            obj->items = 0;
            pcc_gc_publish_initialized((PyObject *)obj);
            return (PyObject *)obj;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            ProbeListObject *owner1 = new_owner();
            ProbeListObject *owner2 = new_owner();
            if (owner1 == 0 || owner2 == 0) return 3;

            PyObject *child1 = new_young_list();
            PyObject *child2 = new_young_list();
            if (child1 == 0 || child2 == 0) return 4;

            pcc_gc_store_ptr((PyObject *)owner1, &owner1->items[0], child1);
            pcc_gc_store_ptr((PyObject *)owner2, &owner2->items[0], child2);

            if (pcc_gc_backend4_store_buffer_entries() != 2) return 5;
            if (pcc_gc_backend4_store_buffer_high_water() != 2) return 6;
            if (pcc_gc_backend4_store_buffer_owner_fanout_high_water() != 1) return 7;
            if (pcc_gc_backend4_store_buffer_owner_count_high_water() != 2) return 8;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_STORE_BUFFER_OWNER_COUNT_HIGH_WATER) != 2) return 9;

            if (pcc_gc_step(2) < 2) return 10;
            if (pcc_gc_backend4_store_buffer_entries() != 0) return 11;
            if (pcc_gc_backend4_store_buffer_owner_count_high_water() != 2) return 12;

            pcc_gc_release(child1);
            pcc_gc_release(child2);
            pcc_gc_release((PyObject *)owner1);
            pcc_gc_release((PyObject *)owner2);
            printf("backend4-owner-count-high-water-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-owner-count-high-water-ok"


def test_backend4_genzgc_store_buffer_drains_in_bounded_batches(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdint.h>
        #include <stdio.h>
        #include <stdlib.h>

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

        static PyObject *new_young_list(void) {
            ProbeListObject *obj = (ProbeListObject *)pcc_gc_alloc(
                sizeof(ProbeListObject),
                PY_TYPE_LIST,
                PY_FLAG_GC_YOUNG
            );
            if (obj == 0) return 0;
            obj->length = 0;
            obj->capacity = 0;
            obj->items = 0;
            pcc_gc_publish_initialized((PyObject *)obj);
            return (PyObject *)obj;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            ProbeListObject *owner = (ProbeListObject *)pcc_gc_alloc(
                sizeof(ProbeListObject),
                PY_TYPE_LIST,
                PY_FLAG_GC_OLD
            );
            if (owner == 0) return 3;
            owner->length = 10;
            owner->capacity = 10;
            owner->items = (PyObject **)calloc(10, sizeof(PyObject *));
            PyObject **children = (PyObject **)calloc(10, sizeof(PyObject *));
            if (owner->items == 0 || children == 0) return 4;

            for (int i = 0; i < 10; i++) {
                children[i] = new_young_list();
                if (children[i] == 0) return 5;
                pcc_gc_store_ptr((PyObject *)owner, &owner->items[i], children[i]);
            }

            if (pcc_gc_backend4_store_buffer_entries() != 10) return 6;
            if (pcc_gc_backend4_store_buffer_batch_capacity() != 8) return 7;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_STORE_BUFFER_BATCH_CAPACITY) != 8) return 8;

            pcc_gc_publish_initialized((PyObject *)owner);
            if (pcc_gc_step(10) < 8) return 9;
            if (pcc_gc_backend4_store_buffer_entries() != 2) return 10;
            if (pcc_gc_backend4_store_buffer_drain_batches() != 1) return 11;
            if (pcc_gc_backend4_store_buffer_drained_entries() != 8) return 12;
            if (pcc_gc_backend4_store_buffer_max_batch_size() != 8) return 13;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_STORE_BUFFER_MAX_BATCH_SIZE) != 8) return 14;
            if (pcc_gc_backend4_store_buffer_full_batches() != 1) return 15;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_STORE_BUFFER_FULL_BATCHES) != 1) return 16;
            if (pcc_gc_backend4_store_buffer_incomplete_drains() != 1) return 17;

            if (pcc_gc_step(10) < 2) return 18;
            if (pcc_gc_backend4_store_buffer_entries() != 0) return 19;
            if (pcc_gc_backend4_store_buffer_drain_batches() != 2) return 20;
            if (pcc_gc_backend4_store_buffer_drained_entries() != 10) return 21;
            if (pcc_gc_backend4_store_buffer_max_batch_size() != 8) return 22;
            if (pcc_gc_backend4_store_buffer_full_batches() != 1) return 23;
            if (pcc_gc_backend4_store_buffer_incomplete_drains() != 1) return 24;

            for (int i = 0; i < 10; i++) {
                pcc_gc_store_ptr((PyObject *)owner, &owner->items[i], 0);
                pcc_gc_release(children[i]);
            }
            free(children);
            pcc_gc_release((PyObject *)owner);
            printf("backend4-store-buffer-batches-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-store-buffer-batches-ok"


def test_backend4_genzgc_store_buffer_uses_medium_path_before_global_flush(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdint.h>
        #include <stdio.h>
        #include <stdlib.h>

        enum {
            PY_FLAG_GC_YOUNG = 0x80,
            PY_FLAG_GC_OLD = 0x100
        };

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            ProbeListObject *owner = (ProbeListObject *)pcc_gc_alloc(
                sizeof(ProbeListObject),
                PY_TYPE_LIST,
                PY_FLAG_GC_OLD
            );
            if (owner == 0) return 3;
            owner->length = 33;
            owner->capacity = 33;
            owner->items = (PyObject **)calloc(33, sizeof(PyObject *));
            PyObject **children = (PyObject **)calloc(33, sizeof(PyObject *));
            if (owner->items == 0 || children == 0) return 4;

            if (pcc_gc_backend4_store_buffer_medium_capacity() != 32) return 5;
            for (int i = 0; i < 33; i++) {
                children[i] = py_list_new(0);
                if (children[i] == 0) return 6;
                pcc_gc_store_ptr((PyObject *)owner, &owner->items[i], children[i]);
            }

            if (pcc_gc_backend4_store_buffer_entries() != 33) return 7;
            if (pcc_gc_backend4_store_buffer_medium_pending() != 1) return 8;
            if (pcc_gc_backend4_store_buffer_medium_flushes() != 1) return 9;
            if (pcc_gc_backend4_store_buffer_medium_flushed_entries() != 32) return 10;
            if (pcc_gc_backend4_store_buffer_medium_full_flushes() != 1) return 11;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_STORE_BUFFER_MEDIUM_PENDING) != 1) return 12;

            pcc_gc_publish_initialized((PyObject *)owner);
            if (pcc_gc_step(8) < 8) return 13;
            if (pcc_gc_backend4_store_buffer_medium_pending() != 0) return 14;
            if (pcc_gc_backend4_store_buffer_medium_flushes() != 2) return 15;
            if (pcc_gc_backend4_store_buffer_medium_flushed_entries() != 33) return 16;
            if (pcc_gc_backend4_store_buffer_entries() != 25) return 17;

            if (pcc_gc_set_backend(PCC_GC_KIND_REFCOUNT_CYCLE) != 0) return 18;
            if (pcc_gc_backend4_store_buffer_entries() != 0) return 19;
            for (int i = 0; i < 33; i++) {
                owner->items[i] = 0;
                pcc_gc_release(children[i]);
            }
            free(children);
            pcc_gc_release((PyObject *)owner);
            printf("backend4-store-buffer-medium-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-store-buffer-medium-ok"


def test_backend4_genzgc_step_flushes_other_mutator_medium_buffer(tmp_path):
    proc = _compile_and_run_threaded(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdint.h>
        #include <stdio.h>
        #include <stdlib.h>
        #include <unistd.h>

        enum {
            PY_FLAG_GC_YOUNG = 0x80,
            PY_FLAG_GC_OLD = 0x100
        };

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        static int64_t worker_ready = 0;
        static int64_t worker_done = 0;

        static PyObject *probe_thread_entry(PyObject *captures, PyObject *args) {
            (void)captures;
            (void)args;
            ProbeListObject *owner = (ProbeListObject *)pcc_gc_alloc(
                sizeof(ProbeListObject),
                PY_TYPE_LIST,
                PY_FLAG_GC_OLD
            );
            if (owner == 0) return py_None;
            owner->length = 5;
            owner->capacity = 5;
            owner->items = (PyObject **)calloc(5, sizeof(PyObject *));
            if (owner->items == 0) return py_None;

            for (int i = 0; i < 5; i++) {
                PyObject *child = py_list_new(0);
                if (child == 0) return py_None;
                pcc_gc_store_ptr((PyObject *)owner, &owner->items[i], child);
                pcc_gc_release(child);
            }

            __atomic_store_n(&worker_ready, 1, __ATOMIC_RELEASE);
            while (__atomic_load_n(&worker_done, __ATOMIC_ACQUIRE) == 0) {
                usleep(1000);
            }
            return py_None;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            PyObject *captures = py_tuple_new(0);
            PyObject *args = py_tuple_new(0);
            PyObject *callable = py_func_new((void *)probe_thread_entry, captures);
            if (captures == 0 || args == 0 || callable == 0) return 3;
            PyObject *thread = py_threading_thread_new(callable, args);
            if (thread == 0) return 4;
            pcc_gc_release(callable);
            pcc_gc_release(args);
            pcc_gc_release(captures);

            if (py_threading_thread_start(thread) != 0) return 5;
            int spins = 0;
            while (__atomic_load_n(&worker_ready, __ATOMIC_ACQUIRE) == 0) {
                usleep(1000);
                spins++;
                if (spins > 5000) return 6;
            }

            if (pcc_gc_backend4_store_buffer_medium_pending() != 5) return 7;
            if (pcc_gc_backend4_store_buffer_entries() != 5) return 8;
            if (pcc_gc_backend4_store_buffer_cross_thread_medium_flushes() != 0) return 9;
            if (pcc_gc_backend4_store_buffer_cross_thread_medium_flushed_entries() != 0) return 10;

            if (pcc_gc_step(8) < 5) return 11;
            if (pcc_gc_backend4_store_buffer_medium_pending() != 0) return 12;
            if (pcc_gc_backend4_store_buffer_entries() != 0) return 13;
            if (pcc_gc_backend4_store_buffer_medium_flushes() != 1) return 14;
            if (pcc_gc_backend4_store_buffer_medium_flushed_entries() != 5) return 15;
            if (pcc_gc_backend4_store_buffer_cross_thread_medium_flushes() != 1) return 16;
            if (pcc_gc_backend4_store_buffer_cross_thread_medium_flushed_entries() != 5) return 17;

            __atomic_store_n(&worker_done, 1, __ATOMIC_RELEASE);
            if (py_threading_thread_join(thread) != 0) return 18;
            pcc_gc_release(thread);
            printf("backend4-cross-thread-medium-flush-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-cross-thread-medium-flush-ok"


def test_backend4_genzgc_fragmentation_policy_exposes_backlog_and_efficiency(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdint.h>
        #include <stdio.h>

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        static PyObject *alloc_empty_list_payload(int64_t size) {
            ProbeListObject *obj = (ProbeListObject *)pcc_gc_alloc(
                size,
                PY_TYPE_LIST,
                0
            );
            if (obj == 0) return 0;
            obj->length = 0;
            obj->capacity = 0;
            obj->items = 0;
            return (PyObject *)obj;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_reset_relocation_set();
            pcc_gc_telemetry_reset();
            if (pcc_gc_backend4_small_page_limit_bytes() != 4096) return 20;
            if (pcc_gc_backend4_medium_page_limit_bytes() != 65536) return 21;
            if (pcc_gc_backend4_large_defer_limit_bytes() != 65536) return 22;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_SMALL_PAGE_LIMIT_BYTES) != 4096) return 23;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_MEDIUM_PAGE_LIMIT_BYTES) != 65536) return 24;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_LARGE_DEFER_LIMIT_BYTES) != 65536) return 25;

            PyObject *a = alloc_empty_list_payload(64);
            PyObject *b = alloc_empty_list_payload(64);
            if (a == 0 || b == 0) return 3;

            if (pcc_gc_select_relocation_set(2) != 2) return 4;
            if (pcc_gc_backend4_evacuation_candidate_bytes() != 128) return 5;
            if (pcc_gc_backend4_fragmentation_backlog_bytes() != 128) return 6;
            if (pcc_gc_backend4_evacuation_efficiency_per_mille() != 0) return 7;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_FRAGMENTATION_BACKLOG_BYTES) != 128) return 8;

            PyObject *moved = pcc_gc_relocate_copy(a, 64);
            if (moved == 0) return 9;
            if (pcc_gc_backend4_evacuated_bytes() != 64) return 10;
            if (pcc_gc_backend4_fragmentation_backlog_bytes() != 64) return 11;
            if (pcc_gc_backend4_evacuation_efficiency_per_mille() != 500) return 12;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_EVACUATION_EFFICIENCY_PER_MILLE) != 500) return 13;
            if (pcc_gc_backend4_fragmentation_policy_score() != 64) return 14;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_FRAGMENTATION_POLICY_SCORE) != 64) return 15;

            pcc_gc_reset_relocation_set();
            pcc_gc_release(moved);
            pcc_gc_release(a);
            pcc_gc_release(b);
            printf("backend4-fragmentation-policy-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-fragmentation-policy-ok"


def test_backend4_genzgc_selector_prefers_fragmented_zpage(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdint.h>
        #include <stdio.h>

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        static PyObject *alloc_empty_list_payload(int64_t size) {
            ProbeListObject *obj = (ProbeListObject *)pcc_gc_alloc(
                size,
                PY_TYPE_LIST,
                0
            );
            if (obj == 0) return 0;
            obj->length = 0;
            obj->capacity = 0;
            obj->items = 0;
            return (PyObject *)obj;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_reset_relocation_set();
            pcc_gc_telemetry_reset();

            PyObject *medium = alloc_empty_list_payload(8192);
            PyObject *small = alloc_empty_list_payload(128);
            if (medium == 0 || small == 0) return 3;

            if (pcc_gc_select_relocation_set(1) != 1) return 4;
            if (pcc_gc_relocation_set_contains(medium) != 1) return 5;
            if (pcc_gc_relocation_set_contains(small) != 0) return 6;
            if (pcc_gc_backend4_medium_page_candidate_score() != 1) return 7;
            if (pcc_gc_backend4_small_page_candidate_score() != 0) return 8;
            if (pcc_gc_backend4_evacuation_candidate_bytes() != 8192) return 9;

            pcc_gc_reset_relocation_set();
            pcc_gc_release(small);
            pcc_gc_release(medium);
            printf("backend4-fragmented-zpage-selector-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-fragmented-zpage-selector-ok"


def test_backend4_genzgc_remembered_set_tracks_unique_dirty_slots(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdint.h>
        #include <stdio.h>
        #include <stdlib.h>

        enum {
            PY_FLAG_GC_YOUNG = 0x80,
            PY_FLAG_GC_OLD = 0x100
        };

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            ProbeListObject *owner = (ProbeListObject *)pcc_gc_alloc(
                sizeof(ProbeListObject),
                PY_TYPE_LIST,
                PY_FLAG_GC_OLD
            );
            if (owner == 0) return 3;
            owner->length = 2;
            owner->capacity = 2;
            owner->items = (PyObject **)calloc(2, sizeof(PyObject *));
            if (owner->items == 0) return 4;

            PyObject *child1 = py_list_new(0);
            PyObject *child2 = py_list_new(0);
            PyObject *child3 = py_list_new(0);
            if (child1 == 0 || child2 == 0 || child3 == 0) return 5;

            pcc_gc_store_ptr((PyObject *)owner, &owner->items[0], child1);
            if (pcc_gc_backend4_remembered_set_entries() != 1) return 6;
            if (pcc_gc_backend4_remembered_set_high_water() != 1) return 7;

            pcc_gc_store_ptr((PyObject *)owner, &owner->items[0], child2);
            if (pcc_gc_backend4_remembered_set_entries() != 1) return 8;
            if (pcc_gc_backend4_remembered_set_duplicate_skips() != 1) return 9;

            pcc_gc_store_ptr((PyObject *)owner, &owner->items[1], child3);
            if (pcc_gc_backend4_remembered_set_entries() != 2) return 10;
            if (pcc_gc_backend4_remembered_set_high_water() != 2) return 11;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_REMEMBERED_SET_ENTRIES) != 2) return 12;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_REMEMBERED_SET_DUPLICATE_SKIPS) != 1) return 13;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_REMEMBERED_SET_HIGH_WATER) != 2) return 14;

            pcc_gc_telemetry_reset();
            if (pcc_gc_backend4_remembered_set_entries() != 2) return 15;
            if (pcc_gc_backend4_remembered_set_high_water() != 2) return 16;
            if (pcc_gc_backend4_remembered_set_duplicate_skips() != 0) return 17;

            pcc_gc_store_ptr((PyObject *)owner, &owner->items[0], 0);
            pcc_gc_store_ptr((PyObject *)owner, &owner->items[1], 0);
            pcc_gc_release(child1);
            pcc_gc_release(child2);
            pcc_gc_release(child3);
            pcc_gc_release((PyObject *)owner);
            if (pcc_gc_backend4_remembered_set_entries() != 0) return 18;
            printf("backend4-remset-bitmap-slots-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-remset-bitmap-slots-ok"


def test_backend4_genzgc_remembered_page_bitmap_predicate_and_clear(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdint.h>
        #include <stdio.h>
        #include <stdlib.h>
        #include <string.h>

        enum {
            PY_FLAG_GC_YOUNG = 0x80,
            PY_FLAG_GC_OLD = 0x100
        };

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            ProbeListObject *owner = (ProbeListObject *)pcc_gc_alloc(
                sizeof(ProbeListObject),
                PY_TYPE_LIST,
                PY_FLAG_GC_OLD
            );
            if (owner == 0) return 3;
            owner->length = 3;
            owner->capacity = 3;
            /* The remembered-page API groups slots by their real 4 KiB page.
             * A small malloc/calloc allocation may legally begin in the last
             * bytes of a page, putting items[0] and items[2] on two pages.
             * Use one aligned page so the bitmap grouping assertion is
             * deterministic and still exercises two distinct slot bits. */
            owner->items = (PyObject **)aligned_alloc(4096, 4096);
            if (owner->items == 0) return 4;
            memset(owner->items, 0, 4096);

            PyObject *child0 = py_list_new(0);
            PyObject *child2 = py_list_new(0);
            if (child0 == 0 || child2 == 0) return 5;

            pcc_gc_store_ptr((PyObject *)owner, &owner->items[0], child0);
            pcc_gc_store_ptr((PyObject *)owner, &owner->items[2], child2);
            if (pcc_gc_backend4_remembered_page_entries() != 1) return 6;
            if (pcc_gc_backend4_remembered_page_slot_entries() != 2) return 7;
            if (pcc_gc_backend4_remembered_page_contains_slot(&owner->items[0]) != 1) return 8;
            if (pcc_gc_backend4_remembered_page_contains_slot(&owner->items[1]) != 0) return 9;
            if (pcc_gc_backend4_remembered_page_contains_slot(&owner->items[2]) != 1) return 10;

            if (pcc_gc_backend4_remembered_page_clear_slot(&owner->items[0]) != 1) return 11;
            if (pcc_gc_backend4_remembered_page_contains_slot(&owner->items[0]) != 0) return 12;
            if (pcc_gc_backend4_remembered_page_contains_slot(&owner->items[2]) != 1) return 13;
            if (pcc_gc_backend4_remembered_set_entries() != 1) return 14;
            if (pcc_gc_backend4_remembered_page_slot_entries() != 1) return 15;
            if (pcc_gc_backend4_remembered_page_entries() != 1) return 16;

            if (pcc_gc_backend4_remembered_page_clear_slot(&owner->items[0]) != 0) return 17;
            if (pcc_gc_backend4_remembered_page_clear_slot(&owner->items[2]) != 1) return 18;
            if (pcc_gc_backend4_remembered_page_entries() != 0) return 19;
            if (pcc_gc_backend4_remembered_page_slot_entries() != 0) return 20;
            if (pcc_gc_backend4_remembered_set_entries() != 0) return 21;

            pcc_gc_store_ptr((PyObject *)owner, &owner->items[0], 0);
            pcc_gc_store_ptr((PyObject *)owner, &owner->items[2], 0);
            pcc_gc_release(child0);
            pcc_gc_release(child2);
            pcc_gc_release((PyObject *)owner);
            printf("backend4-remset-page-bitmap-api-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-remset-page-bitmap-api-ok"


def test_backend4_genzgc_telemetry_reset_reseeds_pending_store_buffer_shape(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdint.h>
        #include <stdio.h>
        #include <stdlib.h>

        enum {
            PY_FLAG_GC_YOUNG = 0x80,
            PY_FLAG_GC_OLD = 0x100
        };

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            ProbeListObject *owner = (ProbeListObject *)pcc_gc_alloc(
                sizeof(ProbeListObject),
                PY_TYPE_LIST,
                PY_FLAG_GC_OLD
            );
            if (owner == 0) return 3;
            owner->length = 2;
            owner->capacity = 2;
            owner->items = (PyObject **)calloc(2, sizeof(PyObject *));
            if (owner->items == 0) return 4;

            PyObject *child1 = py_list_new(0);
            PyObject *child2 = py_list_new(0);
            if (child1 == 0 || child2 == 0) return 5;

            pcc_gc_store_ptr((PyObject *)owner, &owner->items[0], child1);
            pcc_gc_store_ptr((PyObject *)owner, &owner->items[1], child2);
            if (pcc_gc_backend4_store_buffer_entries() != 2) return 6;
            if (pcc_gc_backend4_store_buffer_high_water() != 2) return 7;
            if (pcc_gc_backend4_store_buffer_owner_fanout_high_water() != 2) return 8;
            if (pcc_gc_backend4_store_buffer_owner_count_high_water() != 1) return 9;

            pcc_gc_telemetry_reset();
            if (pcc_gc_backend4_store_buffer_entries() != 2) return 10;
            if (pcc_gc_backend4_store_buffer_high_water() != 2) return 11;
            if (pcc_gc_backend4_store_buffer_owner_fanout_high_water() != 2) return 12;
            if (pcc_gc_backend4_store_buffer_owner_count_high_water() != 1) return 13;
            if (pcc_gc_backend4_store_buffer_duplicate_skips() != 0) return 14;
            if (pcc_gc_backend4_store_buffer_drain_batches() != 0) return 15;

            pcc_gc_publish_initialized((PyObject *)owner);
            if (pcc_gc_step(2) < 2) return 16;
            if (pcc_gc_backend4_store_buffer_entries() != 0) return 17;

            pcc_gc_release(child1);
            pcc_gc_release(child2);
            pcc_gc_release((PyObject *)owner);
            printf("backend4-reset-reseeds-store-buffer-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-reset-reseeds-store-buffer-ok"


def test_backend4_genzgc_store_buffer_clear_resets_shape_telemetry(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdint.h>
        #include <stdio.h>
        #include <stdlib.h>

        enum {
            PY_FLAG_GC_YOUNG = 0x80,
            PY_FLAG_GC_OLD = 0x100
        };

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            ProbeListObject *owner = (ProbeListObject *)pcc_gc_alloc(
                sizeof(ProbeListObject),
                PY_TYPE_LIST,
                PY_FLAG_GC_OLD
            );
            if (owner == 0) return 3;
            owner->length = 1;
            owner->capacity = 1;
            owner->items = (PyObject **)calloc(1, sizeof(PyObject *));
            if (owner->items == 0) return 4;

            PyObject *child = py_list_new(0);
            if (child == 0) return 5;
            pcc_gc_store_ptr((PyObject *)owner, &owner->items[0], child);

            if (pcc_gc_backend4_store_buffer_entries() != 1) return 6;
            if (pcc_gc_backend4_store_buffer_high_water() != 1) return 7;
            if (pcc_gc_backend4_store_buffer_owner_fanout_high_water() != 1) return 8;
            if (pcc_gc_backend4_store_buffer_owner_count_high_water() != 1) return 9;

            if (pcc_gc_set_backend(PCC_GC_KIND_REFCOUNT_CYCLE) != 0) return 10;
            if (pcc_gc_backend4_store_buffer_entries() != 0) return 11;
            if (pcc_gc_backend4_store_buffer_high_water() != 0) return 12;
            if (pcc_gc_backend4_store_buffer_owner_fanout_high_water() != 0) return 13;
            if (pcc_gc_backend4_store_buffer_owner_count_high_water() != 0) return 14;

            pcc_gc_release(child);
            pcc_gc_release((PyObject *)owner);
            printf("backend4-clear-resets-store-buffer-shape-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-clear-resets-store-buffer-shape-ok"


def test_backend4_genzgc_store_buffer_keeps_value_snapshot_until_drain(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdint.h>
        #include <stdio.h>
        #include <stdlib.h>

        enum {
            PY_FLAG_GC_YOUNG = 0x80,
            PY_FLAG_GC_OLD = 0x100,
            PY_FLAG_GC_REMEMBERED = 0x200
        };

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            ProbeListObject *owner = (ProbeListObject *)pcc_gc_alloc(
                sizeof(ProbeListObject),
                PY_TYPE_LIST,
                PY_FLAG_GC_OLD
            );
            if (owner == 0) return 3;
            owner->length = 1;
            owner->capacity = 1;
            owner->items = (PyObject **)calloc(1, sizeof(PyObject *));
            if (owner->items == 0) return 4;

            PyObject *child = py_list_new(0);
            if (child == 0) return 5;
            PyObject *child2 = py_list_new(0);
            if (child2 == 0) return 16;

            pcc_gc_note_slot_write_barrier((PyObject *)owner, &owner->items[0], child);
            pcc_gc_note_slot_write_barrier((PyObject *)owner, &owner->items[0], child2);
            pcc_gc_note_slot_write_barrier((PyObject *)owner, &owner->items[0], child);
            if ((owner->h.flags & PY_FLAG_GC_REMEMBERED) == 0) return 6;
            if (pcc_gc_backend4_generation_barrier_score() != 2) return 7;
            if (pcc_gc_backend4_store_buffer_entries() != 2) return 8;
            if (owner->items[0] != 0) return 9;

            pcc_gc_publish_initialized((PyObject *)owner);
            if (pcc_gc_step(1) < 1) return 10;
            if ((owner->h.flags & PY_FLAG_GC_REMEMBERED) == 0) return 11;
            if (pcc_gc_backend4_store_buffer_entries() != 1) return 12;
            if (owner->items[0] != 0) return 13;

            if (pcc_gc_step(1) < 1) return 14;
            if ((((PyObjectHeader *)child)->flags & PY_FLAG_GC_OLD) == 0) return 15;
            if ((((PyObjectHeader *)child)->flags & PY_FLAG_GC_YOUNG) != 0) return 17;
            if ((((PyObjectHeader *)child2)->flags & PY_FLAG_GC_OLD) == 0) return 18;
            if ((((PyObjectHeader *)child2)->flags & PY_FLAG_GC_YOUNG) != 0) return 19;
            if ((owner->h.flags & PY_FLAG_GC_REMEMBERED) != 0) return 20;
            if (pcc_gc_backend4_store_buffer_entries() != 0) return 21;
            if (owner->items[0] != 0) return 22;

            pcc_gc_release(child);
            pcc_gc_release(child2);
            pcc_gc_release((PyObject *)owner);
            printf("backend4-genzgc-store-snapshot-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-genzgc-store-snapshot-ok"


def test_backend4_genzgc_allocations_default_young_and_age_to_old(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        enum {
            PY_FLAG_GC_YOUNG = 0x80,
            PY_FLAG_GC_OLD = 0x100
        };

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            PyObject *young = pcc_gc_alloc(64, PY_TYPE_LIST, 0);
            if (young == 0) return 3;
            if ((((PyObjectHeader *)young)->flags & PY_FLAG_GC_YOUNG) == 0) return 4;
            if ((((PyObjectHeader *)young)->flags & PY_FLAG_GC_OLD) != 0) return 5;

            PyObject *explicit_old = pcc_gc_alloc(64, PY_TYPE_LIST, PY_FLAG_GC_OLD);
            if (explicit_old == 0) return 6;
            if ((((PyObjectHeader *)explicit_old)->flags & PY_FLAG_GC_OLD) == 0) return 7;
            if ((((PyObjectHeader *)explicit_old)->flags & PY_FLAG_GC_YOUNG) != 0) return 8;
            if (pcc_gc_backend4_young_object_count() != 1) return 14;
            if (pcc_gc_backend4_old_object_count() != 1) return 15;
            if (pcc_gc_backend4_young_bytes() != 64) return 16;
            if (pcc_gc_backend4_old_bytes() != 64) return 17;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_YOUNG_OBJECTS) != 1) return 18;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_OLD_OBJECTS) != 1) return 19;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_YOUNG_BYTES) != 64) return 20;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_OLD_BYTES) != 64) return 21;

            pcc_gc_publish_initialized((PyObject *)young);
            pcc_gc_publish_initialized((PyObject *)explicit_old);
            if (pcc_gc_step(1) < 1) return 9;
            if ((((PyObjectHeader *)young)->flags & PY_FLAG_GC_OLD) == 0) return 10;
            if ((((PyObjectHeader *)young)->flags & PY_FLAG_GC_YOUNG) != 0) return 11;
            if (pcc_gc_backend4_generation_promotion_score() != 1) return 12;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_YOUNG_PROMOTIONS) != 1) return 13;
            if (pcc_gc_backend4_young_object_count() != 0) return 22;
            if (pcc_gc_backend4_old_object_count() != 2) return 23;
            if (pcc_gc_backend4_young_bytes() != 0) return 24;
            if (pcc_gc_backend4_old_bytes() != 128) return 25;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_YOUNG_OBJECTS) != 0) return 26;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_OLD_OBJECTS) != 2) return 27;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_YOUNG_BYTES) != 0) return 28;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_OLD_BYTES) != 128) return 29;

            pcc_gc_release(young);
            pcc_gc_release(explicit_old);
            printf("backend4-genzgc-aging-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-genzgc-aging-ok"


def test_backend4_genzgc_page_class_live_population_telemetry(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            int64_t small_count0 = pcc_gc_backend4_small_page_object_count();
            int64_t medium_count0 = pcc_gc_backend4_medium_page_object_count();
            int64_t large_count0 = pcc_gc_backend4_large_page_object_count();
            int64_t small_bytes0 = pcc_gc_backend4_small_page_live_bytes();
            int64_t medium_bytes0 = pcc_gc_backend4_medium_page_live_bytes();
            int64_t large_bytes0 = pcc_gc_backend4_large_page_live_bytes();

            PyObject *small = pcc_gc_alloc(128, PY_TYPE_LIST, 0);
            PyObject *medium = pcc_gc_alloc(8192, PY_TYPE_LIST, 0);
            PyObject *large = pcc_gc_alloc(70000, PY_TYPE_LIST, 0);
            if (small == 0 || medium == 0 || large == 0) return 3;

            if (pcc_gc_backend4_small_page_object_count() - small_count0 != 1) return 4;
            if (pcc_gc_backend4_medium_page_object_count() - medium_count0 != 1) return 5;
            if (pcc_gc_backend4_large_page_object_count() - large_count0 != 1) return 6;
            if (pcc_gc_backend4_small_page_live_bytes() - small_bytes0 != 128) return 7;
            if (pcc_gc_backend4_medium_page_live_bytes() - medium_bytes0 != 8192) return 8;
            if (pcc_gc_backend4_large_page_live_bytes() - large_bytes0 != 70000) return 9;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_SMALL_PAGE_OBJECTS) - small_count0 != 1) return 10;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_MEDIUM_PAGE_OBJECTS) - medium_count0 != 1) return 11;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_LARGE_PAGE_OBJECTS) - large_count0 != 1) return 12;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_SMALL_PAGE_BYTES) - small_bytes0 != 128) return 13;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_MEDIUM_PAGE_BYTES) - medium_bytes0 != 8192) return 14;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_LARGE_PAGE_BYTES) - large_bytes0 != 70000) return 15;

            pcc_gc_release(large);
            pcc_gc_release(medium);
            pcc_gc_release(small);
            printf("backend4-page-class-live-telemetry-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-page-class-live-telemetry-ok"


def test_backend4_genzgc_zpage_ownership_telemetry(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            int64_t count0 = pcc_gc_backend4_zpage_count();
            int64_t capacity0 = pcc_gc_backend4_zpage_capacity_bytes();
            int64_t fragmentation0 = pcc_gc_backend4_zpage_fragmentation_bytes();
            int64_t large0 = pcc_gc_backend4_zpage_large_pages();
            int64_t used0 = pcc_gc_backend4_zpage_used_bytes();
            int64_t fragmented0 = pcc_gc_backend4_zpage_fragmented_pages();
            int64_t young0 = pcc_gc_backend4_zpage_young_pages();
            int64_t old0 = pcc_gc_backend4_zpage_old_pages();
            int64_t policy0 = pcc_gc_backend4_zpage_policy_score();

            PyObject *small = pcc_gc_alloc(128, PY_TYPE_LIST, 0);
            PyObject *medium = pcc_gc_alloc(8192, PY_TYPE_LIST, 0);
            PyObject *large = pcc_gc_alloc(70000, PY_TYPE_LIST, 0);
            if (small == 0 || medium == 0 || large == 0) return 3;

            int64_t expected_capacity = 4096 + 65536 + 131072;
            int64_t expected_fragmentation = (4096 - 128) + (65536 - 8192) + (131072 - 70000);
            int64_t expected_policy = expected_fragmentation + 3;
            if (pcc_gc_backend4_zpage_count() - count0 != 3) return 4;
            if (pcc_gc_backend4_zpage_capacity_bytes() - capacity0 != expected_capacity) return 5;
            if (pcc_gc_backend4_zpage_fragmentation_bytes() - fragmentation0 != expected_fragmentation) return 6;
            if (pcc_gc_backend4_zpage_large_pages() - large0 != 1) return 7;
            if (pcc_gc_backend4_zpage_used_bytes() - used0 != 78320) return 12;
            if (pcc_gc_backend4_zpage_fragmentation_per_mille() <= 0) return 13;
            if (pcc_gc_backend4_zpage_fragmented_pages() - fragmented0 != 3) return 23;
            if (pcc_gc_backend4_zpage_young_pages() - young0 != 3) return 26;
            if (pcc_gc_backend4_zpage_old_pages() != old0) return 27;
            if (pcc_gc_backend4_zpage_policy_score() - policy0 != expected_policy) return 16;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_ZPAGE_COUNT) - count0 != 3) return 8;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_ZPAGE_CAPACITY_BYTES) - capacity0 != expected_capacity) return 9;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_ZPAGE_FRAGMENTATION_BYTES) - fragmentation0 != expected_fragmentation) return 10;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_ZPAGE_LARGE_PAGES) - large0 != 1) return 11;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_ZPAGE_USED_BYTES) - used0 != 78320) return 14;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_ZPAGE_FRAGMENTATION_PER_MILLE) <= 0) return 15;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_ZPAGE_POLICY_SCORE) - policy0 != expected_policy) return 17;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_ZPAGE_FRAGMENTED_PAGES) - fragmented0 != 3) return 24;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_ZPAGE_YOUNG_PAGES) - young0 != 3) return 28;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_ZPAGE_OLD_PAGES) != old0) return 29;

            pcc_gc_release(large);
            pcc_gc_release(medium);
            pcc_gc_release(small);
            if (pcc_gc_backend4_zpage_count() != count0) return 18;
            if (pcc_gc_backend4_zpage_capacity_bytes() != capacity0) return 19;
            if (pcc_gc_backend4_zpage_fragmentation_bytes() != fragmentation0) return 20;
            if (pcc_gc_backend4_zpage_large_pages() != large0) return 21;
            if (pcc_gc_backend4_zpage_used_bytes() != used0) return 22;
            if (pcc_gc_backend4_zpage_fragmented_pages() != fragmented0) return 25;
            if (pcc_gc_backend4_zpage_young_pages() != young0) return 30;
            if (pcc_gc_backend4_zpage_old_pages() != old0) return 31;
            printf("backend4-zpage-telemetry-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-zpage-telemetry-ok"


def test_backend4_genzgc_small_objects_share_zpage(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            int64_t count0 = pcc_gc_backend4_zpage_count();
            int64_t capacity0 = pcc_gc_backend4_zpage_capacity_bytes();
            int64_t used0 = pcc_gc_backend4_zpage_used_bytes();
            int64_t fragmentation0 = pcc_gc_backend4_zpage_fragmentation_bytes();
            int64_t fragmented0 = pcc_gc_backend4_zpage_fragmented_pages();
            int64_t young0 = pcc_gc_backend4_zpage_young_pages();

            PyObject *a = pcc_gc_alloc(128, PY_TYPE_LIST, 0);
            PyObject *b = pcc_gc_alloc(128, PY_TYPE_LIST, 0);
            if (a == 0 || b == 0) return 3;

            if (pcc_gc_backend4_zpage_count() - count0 != 1) return 4;
            if (pcc_gc_backend4_zpage_capacity_bytes() - capacity0 != 4096) return 5;
            if (pcc_gc_backend4_zpage_used_bytes() - used0 != 256) return 6;
            if (pcc_gc_backend4_zpage_fragmentation_bytes() - fragmentation0 != 3840) return 7;
            if (pcc_gc_backend4_zpage_fragmented_pages() - fragmented0 != 1) return 8;
            if (pcc_gc_backend4_zpage_young_pages() - young0 != 1) return 9;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_ZPAGE_COUNT) - count0 != 1) return 10;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_ZPAGE_CAPACITY_BYTES) - capacity0 != 4096) return 11;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_ZPAGE_USED_BYTES) - used0 != 256) return 12;

            pcc_gc_release(b);
            if (pcc_gc_backend4_zpage_count() - count0 != 1) return 13;
            if (pcc_gc_backend4_zpage_used_bytes() - used0 != 128) return 14;
            if (pcc_gc_backend4_zpage_fragmentation_bytes() - fragmentation0 != 3968) return 15;

            pcc_gc_release(a);
            if (pcc_gc_backend4_zpage_count() != count0) return 16;
            if (pcc_gc_backend4_zpage_capacity_bytes() != capacity0) return 17;
            if (pcc_gc_backend4_zpage_used_bytes() != used0) return 18;
            if (pcc_gc_backend4_zpage_fragmentation_bytes() != fragmentation0) return 19;
            printf("backend4-small-zpage-sharing-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-small-zpage-sharing-ok"


def test_backend4_genzgc_zpage_reuses_released_virtual_tail(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            int64_t count0 = pcc_gc_backend4_zpage_count();
            int64_t used0 = pcc_gc_backend4_zpage_used_bytes();
            int64_t allocated0 = pcc_gc_backend4_zpage_allocated_bytes();
            int64_t gap0 = pcc_gc_backend4_zpage_reclaimable_gap_bytes();

            PyObject *a = pcc_gc_alloc(128, PY_TYPE_LIST, 0);
            PyObject *b = pcc_gc_alloc(128, PY_TYPE_LIST, 0);
            if (a == 0 || b == 0) return 3;

            if (pcc_gc_backend4_zpage_count() - count0 != 1) return 4;
            if (pcc_gc_backend4_zpage_used_bytes() - used0 != 256) return 5;
            if (pcc_gc_backend4_zpage_allocated_bytes() - allocated0 != 256) return 6;
            if (pcc_gc_backend4_zpage_reclaimable_gap_bytes() != gap0) return 7;

            pcc_gc_release(b);
            if (pcc_gc_backend4_zpage_count() - count0 != 1) return 8;
            if (pcc_gc_backend4_zpage_used_bytes() - used0 != 128) return 9;
            if (pcc_gc_backend4_zpage_allocated_bytes() - allocated0 != 128) return 10;
            if (pcc_gc_backend4_zpage_reclaimable_gap_bytes() != gap0) return 11;

            PyObject *c = pcc_gc_alloc(128, PY_TYPE_LIST, 0);
            if (c == 0) return 12;
            if (pcc_gc_backend4_zpage_owner_offset_bytes(c) != 128) return 13;
            if (pcc_gc_backend4_zpage_count() - count0 != 1) return 14;
            if (pcc_gc_backend4_zpage_used_bytes() - used0 != 256) return 15;
            if (pcc_gc_backend4_zpage_allocated_bytes() - allocated0 != 256) return 16;
            if (pcc_gc_backend4_zpage_reclaimable_gap_bytes() != gap0) return 17;

            pcc_gc_release(c);
            pcc_gc_release(a);
            if (pcc_gc_backend4_zpage_count() != count0) return 18;
            if (pcc_gc_backend4_zpage_used_bytes() != used0) return 19;
            if (pcc_gc_backend4_zpage_allocated_bytes() != allocated0) return 20;
            if (pcc_gc_backend4_zpage_reclaimable_gap_bytes() != gap0) return 21;

            printf("backend4-zpage-virtual-tail-reuse-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-zpage-virtual-tail-reuse-ok"


def test_backend4_genzgc_zpage_does_not_reuse_interior_virtual_hole(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            int64_t allocated0 = pcc_gc_backend4_zpage_allocated_bytes();
            int64_t gap0 = pcc_gc_backend4_zpage_reclaimable_gap_bytes();
            PyObject *a = pcc_gc_alloc(128, PY_TYPE_LIST, 0);
            PyObject *b = pcc_gc_alloc(128, PY_TYPE_LIST, 0);
            if (a == 0 || b == 0) return 3;

            pcc_gc_release(a);
            if (pcc_gc_backend4_zpage_allocated_bytes() - allocated0 != 256) return 4;
            if (pcc_gc_backend4_zpage_reclaimable_gap_bytes() - gap0 != 128) return 5;

            PyObject *c = pcc_gc_alloc(128, PY_TYPE_LIST, 0);
            if (c == 0) return 6;
            if (pcc_gc_backend4_zpage_owner_offset_bytes(c) != 256) return 7;
            if (pcc_gc_backend4_zpage_allocated_bytes() - allocated0 != 384) return 8;
            if (pcc_gc_backend4_zpage_reclaimable_gap_bytes() - gap0 != 128) return 9;

            pcc_gc_release(c);
            if (pcc_gc_backend4_zpage_allocated_bytes() - allocated0 != 256) return 10;
            if (pcc_gc_backend4_zpage_reclaimable_gap_bytes() - gap0 != 128) return 11;
            pcc_gc_release(b);
            printf("backend4-zpage-interior-hole-preserved-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-zpage-interior-hole-preserved-ok"


def test_backend4_genzgc_objects_are_carved_from_zpage_span(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdint.h>
        #include <stdio.h>

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            int64_t count0 = pcc_gc_backend4_zpage_count();
            int64_t used0 = pcc_gc_backend4_zpage_used_bytes();
            int64_t allocated0 = pcc_gc_backend4_zpage_allocated_bytes();

            PyObject *a = pcc_gc_alloc(128, PY_TYPE_LIST, 0);
            PyObject *b = pcc_gc_alloc(128, PY_TYPE_LIST, 0);
            if (a == 0 || b == 0) return 3;
            if ((uintptr_t)b - (uintptr_t)a != 128) return 4;
            if (pcc_gc_backend4_zpage_owner_offset_bytes(a) != 0) return 5;
            if (pcc_gc_backend4_zpage_owner_offset_bytes(b) != 128) return 6;
            if (pcc_gc_backend4_zpage_count() - count0 != 1) return 7;
            if (pcc_gc_backend4_zpage_used_bytes() - used0 != 256) return 8;
            if (pcc_gc_backend4_zpage_allocated_bytes() - allocated0 != 256) return 9;

            pcc_gc_release(b);
            pcc_gc_release(a);
            if (pcc_gc_backend4_zpage_count() != count0) return 10;
            if (pcc_gc_backend4_zpage_used_bytes() != used0) return 11;
            if (pcc_gc_backend4_zpage_allocated_bytes() != allocated0) return 12;

            printf("backend4-zpage-real-span-alloc-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-zpage-real-span-alloc-ok"


def test_backend4_genzgc_large_object_uses_dedicated_zpage_span(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            int64_t count0 = pcc_gc_backend4_zpage_count();
            int64_t large0 = pcc_gc_backend4_zpage_large_pages();
            int64_t capacity0 = pcc_gc_backend4_zpage_capacity_bytes();
            int64_t used0 = pcc_gc_backend4_zpage_used_bytes();
            int64_t allocated0 = pcc_gc_backend4_zpage_allocated_bytes();
            int64_t free0 = pcc_gc_backend4_zpage_free_pages();
            int64_t freecap0 = pcc_gc_backend4_zpage_free_capacity_bytes();

            PyObject *large = pcc_gc_alloc(70000, PY_TYPE_LIST, 0);
            if (large == 0) return 3;
            if (pcc_gc_backend4_zpage_owner_offset_bytes(large) != 0) return 4;
            if (pcc_gc_backend4_zpage_owner_size_bytes(large) != 70000) return 5;
            if (pcc_gc_backend4_zpage_count() - count0 != 1) return 6;
            if (pcc_gc_backend4_zpage_large_pages() - large0 != 1) return 7;
            if (pcc_gc_backend4_zpage_capacity_bytes() - capacity0 != 131072) return 8;
            if (pcc_gc_backend4_zpage_used_bytes() - used0 != 70000) return 9;
            if (pcc_gc_backend4_zpage_allocated_bytes() - allocated0 != 70000) return 10;

            pcc_gc_release(large);
            if (pcc_gc_backend4_zpage_count() != count0) return 11;
            if (pcc_gc_backend4_zpage_large_pages() != large0) return 12;
            if (pcc_gc_backend4_zpage_capacity_bytes() != capacity0) return 13;
            if (pcc_gc_backend4_zpage_used_bytes() != used0) return 14;
            if (pcc_gc_backend4_zpage_allocated_bytes() != allocated0) return 15;
            if (pcc_gc_backend4_zpage_free_pages() != free0) return 16;
            if (pcc_gc_backend4_zpage_free_capacity_bytes() != freecap0) return 17;

            printf("backend4-large-zpage-span-lifecycle-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-large-zpage-span-lifecycle-ok"


def test_backend4_genzgc_step_evacuates_fragmented_large_zpage(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            PyObject *root = 0;
            pcc_gc_scheduler_root_register(&root);

            int64_t large0 = pcc_gc_backend4_zpage_large_pages();
            int64_t capacity0 = pcc_gc_backend4_zpage_capacity_bytes();
            int64_t used0 = pcc_gc_backend4_zpage_used_bytes();
            int64_t allocated0 = pcc_gc_backend4_zpage_allocated_bytes();
            int64_t free0 = pcc_gc_backend4_zpage_free_pages();
            int64_t freecap0 = pcc_gc_backend4_zpage_free_capacity_bytes();

            PyObject *large = pcc_gc_alloc(70000, PY_TYPE_LIST, 0x100);
            if (large == 0) return 3;
            pcc_gc_store_root(&root, large);
            pcc_gc_release(large);
            if (pcc_gc_backend4_zpage_owner_offset_bytes(root) != 0) return 4;
            if (pcc_gc_backend4_zpage_owner_size_bytes(root) != 70000) return 5;
            if (pcc_gc_backend4_zpage_large_pages() - large0 != 1) return 6;
            if (pcc_gc_backend4_zpage_capacity_bytes() - capacity0 != 131072) return 7;
            if (pcc_gc_backend4_zpage_used_bytes() - used0 != 70000) return 8;
            if (pcc_gc_backend4_zpage_allocated_bytes() - allocated0 != 70000) return 9;

            pcc_gc_telemetry_reset();
            pcc_gc_publish_initialized((PyObject *)large);
            if (pcc_gc_step(1) != 1) return 10;
            if (pcc_gc_backend4_evacuated_bytes() != 70000) return 11;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_EVACUATED_BYTES) != 70000) return 12;
            if (pcc_gc_relocation_set_size() != 0) return 13;

            PyObject *moved = pcc_gc_load_ptr(0, &root);
            if (moved == 0) return 14;
            if (moved == large) return 15;
            if (pcc_gc_backend4_zpage_owner_offset_bytes(large) != -1) return 16;
            if (pcc_gc_backend4_zpage_owner_offset_bytes(moved) != 0) return 17;
            if (pcc_gc_backend4_zpage_owner_size_bytes(moved) != 70000) return 18;
            if (pcc_gc_backend4_zpage_large_pages() - large0 != 2) return 19;
            if (pcc_gc_backend4_zpage_capacity_bytes() - capacity0 != 262144) return 20;
            if (pcc_gc_backend4_zpage_used_bytes() - used0 != 70000) return 21;
            if (pcc_gc_backend4_zpage_allocated_bytes() - allocated0 != 140000) return 22;
            if (pcc_gc_backend4_zpage_free_pages() != free0) return 23;
            if (pcc_gc_backend4_zpage_free_capacity_bytes() != freecap0) return 24;
            if (pcc_gc_backend4_forwarding_entries() <= 0) return 25;

            pcc_gc_store_root(&root, 0);
            pcc_gc_scheduler_root_unregister(&root);
            printf("backend4-large-zpage-step-evacuation-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-large-zpage-step-evacuation-ok"


def test_backend4_genzgc_zpage_exposes_owner_virtual_span_location(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            PyObject *a = pcc_gc_alloc(600, PY_TYPE_LIST, 0);
            PyObject *b = pcc_gc_alloc(600, PY_TYPE_LIST, 0);
            if (a == 0 || b == 0) return 3;

            if (pcc_gc_backend4_zpage_owner_offset_bytes(a) != 0) return 4;
            if (pcc_gc_backend4_zpage_owner_size_bytes(a) != 600) return 5;
            if (pcc_gc_backend4_zpage_owner_span_card(a) != 0) return 6;
            if (pcc_gc_backend4_zpage_owner_offset_bytes(b) != 600) return 7;
            if (pcc_gc_backend4_zpage_owner_size_bytes(b) != 600) return 8;
            if (pcc_gc_backend4_zpage_owner_span_card(b) != 1) return 9;

            pcc_gc_release(b);
            if (pcc_gc_backend4_zpage_owner_offset_bytes(b) != -1) return 10;
            if (pcc_gc_backend4_zpage_owner_size_bytes(b) != -1) return 11;
            if (pcc_gc_backend4_zpage_owner_span_card(b) != -1) return 12;

            /* Releasing b exposed the virtual bump tail, and zpage_remove
               rewinds it (see pcc_gc_backend4_zpage_remove: the owner
               reservation is reclaimed once its payload spans are gone).  So
               the next allocation must REUSE 600, not extend to 1200 --
               asserting the extension would pin the pre-reclamation
               behaviour and quietly accept a virtual-space leak. */
            PyObject *c = pcc_gc_alloc(600, PY_TYPE_LIST, 0);
            if (c == 0) return 13;
            if (pcc_gc_backend4_zpage_owner_offset_bytes(c) != 600) return 14;
            if (pcc_gc_backend4_zpage_owner_size_bytes(c) != 600) return 15;
            if (pcc_gc_backend4_zpage_owner_span_card(c) != 1) return 16;

            pcc_gc_release(c);
            pcc_gc_release(a);
            printf("backend4-zpage-owner-span-location-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-zpage-owner-span-location-ok"


def test_backend4_genzgc_zpage_backing_span_survives_free_cache(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            int64_t active_span0 = pcc_gc_backend4_zpage_span_bytes();
            int64_t free_span0 = pcc_gc_backend4_zpage_free_span_bytes();
            int64_t free_pages0 = pcc_gc_backend4_zpage_free_pages();

            PyObject *a = pcc_gc_alloc(128, PY_TYPE_LIST, 0);
            if (a == 0) return 3;
            if (pcc_gc_backend4_zpage_span_bytes() - active_span0 != 4096) return 4;
            if (pcc_gc_backend4_zpage_free_span_bytes() != free_span0) return 5;

            pcc_gc_release(a);
            if (pcc_gc_backend4_zpage_span_bytes() != active_span0) return 6;
            if (pcc_gc_backend4_zpage_free_pages() - free_pages0 != 1) return 7;
            if (pcc_gc_backend4_zpage_free_span_bytes() - free_span0 != 4096) return 8;

            PyObject *b = pcc_gc_alloc(128, PY_TYPE_LIST, 0);
            if (b == 0) return 9;
            if (pcc_gc_backend4_zpage_span_bytes() - active_span0 != 4096) return 10;
            if (pcc_gc_backend4_zpage_free_pages() != free_pages0) return 11;
            if (pcc_gc_backend4_zpage_free_span_bytes() != free_span0) return 12;

            pcc_gc_release(b);
            if (pcc_gc_backend4_zpage_span_bytes() != active_span0) return 13;
            if (pcc_gc_backend4_zpage_free_pages() - free_pages0 != 1) return 14;
            if (pcc_gc_backend4_zpage_free_span_bytes() - free_span0 != 4096) return 15;

            printf("backend4-zpage-backing-span-cache-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-zpage-backing-span-cache-ok"


def test_backend4_genzgc_candidate_zpage_bytes_count_shared_page_once(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            PyObject *a = pcc_gc_alloc(128, PY_TYPE_LIST, 0);
            PyObject *b = pcc_gc_alloc(128, PY_TYPE_LIST, 0);
            if (a == 0 || b == 0) return 3;
            if (pcc_gc_backend4_zpage_count() < 1) return 4;
            int64_t free0 = pcc_gc_backend4_zpage_free_pages();

            pcc_gc_telemetry_reset();
            pcc_gc_publish_initialized((PyObject *)a);
            pcc_gc_publish_initialized((PyObject *)b);
            if (pcc_gc_select_relocation_set(2) != 2) return 5;
            if (pcc_gc_relocation_set_contains(a) != 1) return 6;
            if (pcc_gc_relocation_set_contains(b) != 1) return 7;
            if (pcc_gc_backend4_evacuation_candidate_bytes() != 256) return 8;
            if (pcc_gc_backend4_small_page_candidate_bytes() != 256) return 9;
            if (pcc_gc_backend4_evacuation_candidate_zpage_bytes() != 256) return 10;
            if (pcc_gc_backend4_small_page_candidate_zpage_bytes() != 256) return 11;
            if (pcc_gc_backend4_medium_page_candidate_zpage_bytes() != 0) return 12;
            if (pcc_gc_backend4_evacuation_page_candidate_score() != 1) return 20;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_EVACUATION_CANDIDATE_ZPAGE_BYTES) != 256) return 13;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_SMALL_PAGE_CANDIDATE_ZPAGE_BYTES) != 256) return 14;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_MEDIUM_PAGE_CANDIDATE_ZPAGE_BYTES) != 0) return 15;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_EVACUATION_PAGE_CANDIDATES) != 1) return 21;

            int64_t pages_before_move = pcc_gc_backend4_zpage_count();
            PyObject *moved_a = pcc_gc_relocate_copy(a, 128);
            if (moved_a == 0) return 24;
            if (pcc_gc_backend4_zpage_count() <= pages_before_move) return 29;
            if (pcc_gc_backend4_evacuation_page_candidate_score() != 1) return 25;
            PyObject *moved_b = pcc_gc_relocate_copy(b, 128);
            if (moved_b == 0) return 26;
            if (pcc_gc_backend4_evacuation_page_candidate_score() != 0) return 27;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_EVACUATION_PAGE_CANDIDATES) != 0) return 28;
            if (pcc_gc_backend4_zpage_free_pages() != free0) return 30;
            py_decref(moved_a);
            py_decref(moved_b);

            pcc_gc_telemetry_reset();
            if (pcc_gc_backend4_evacuation_candidate_zpage_bytes() != 0) return 16;
            if (pcc_gc_backend4_small_page_candidate_zpage_bytes() != 0) return 17;
            if (pcc_gc_backend4_evacuation_page_candidate_score() != 0) return 22;

            pcc_gc_reset_relocation_set();
            if (pcc_gc_backend4_evacuation_candidate_zpage_bytes() != 0) return 18;
            if (pcc_gc_backend4_small_page_candidate_zpage_bytes() != 0) return 19;
            if (pcc_gc_backend4_evacuation_page_candidate_score() != 0) return 23;
            printf("backend4-zpage-candidate-bytes-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-zpage-candidate-bytes-ok"


def test_backend4_genzgc_relocation_targets_use_non_evacuation_zpage(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdint.h>
        #include <stdio.h>

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            PyObject *a = pcc_gc_alloc(128, PY_TYPE_LIST, 0);
            PyObject *b = pcc_gc_alloc(128, PY_TYPE_LIST, 0);
            if (a == 0 || b == 0) return 3;
            if ((uintptr_t)b - (uintptr_t)a != 128) return 4;

            int64_t pages0 = pcc_gc_backend4_zpage_count();
            int64_t free0 = pcc_gc_backend4_zpage_free_pages();
            pcc_gc_publish_initialized((PyObject *)a);
            pcc_gc_publish_initialized((PyObject *)b);
            if (pcc_gc_select_relocation_set(2) != 2) return 5;

            PyObject *moved_a = pcc_gc_relocate_copy(a, 128);
            if (moved_a == 0) return 6;
            if (moved_a == a || moved_a == b) return 7;
            if (pcc_gc_backend4_zpage_count() <= pages0) return 8;
            if (pcc_gc_backend4_zpage_owner_offset_bytes(moved_a) != 0) return 9;
            if (pcc_gc_backend4_zpage_owner_offset_bytes(b) != 128) return 10;

            PyObject *moved_b = pcc_gc_relocate_copy(b, 128);
            if (moved_b == 0) return 11;
            if ((uintptr_t)moved_b - (uintptr_t)moved_a != 128) return 12;
            if (pcc_gc_backend4_zpage_owner_offset_bytes(moved_b) != 128) return 13;
            if (pcc_gc_backend4_zpage_free_pages() != free0) return 14;

            py_decref(moved_a);
            py_decref(moved_b);
            printf("backend4-relocation-target-zpage-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-relocation-target-zpage-ok"


def test_backend4_genzgc_medium_objects_share_zpage(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            int64_t count0 = pcc_gc_backend4_zpage_count();
            int64_t capacity0 = pcc_gc_backend4_zpage_capacity_bytes();
            int64_t used0 = pcc_gc_backend4_zpage_used_bytes();
            int64_t fragmentation0 = pcc_gc_backend4_zpage_fragmentation_bytes();
            int64_t fragmented0 = pcc_gc_backend4_zpage_fragmented_pages();

            PyObject *a = pcc_gc_alloc(8192, PY_TYPE_LIST, 0);
            PyObject *b = pcc_gc_alloc(8192, PY_TYPE_LIST, 0);
            if (a == 0 || b == 0) return 3;

            if (pcc_gc_backend4_zpage_count() - count0 != 1) return 4;
            if (pcc_gc_backend4_zpage_capacity_bytes() - capacity0 != 65536) return 5;
            if (pcc_gc_backend4_zpage_used_bytes() - used0 != 16384) return 6;
            if (pcc_gc_backend4_zpage_fragmentation_bytes() - fragmentation0 != 49152) return 7;
            if (pcc_gc_backend4_zpage_fragmented_pages() - fragmented0 != 1) return 8;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_ZPAGE_COUNT) - count0 != 1) return 9;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_ZPAGE_CAPACITY_BYTES) - capacity0 != 65536) return 10;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_ZPAGE_USED_BYTES) - used0 != 16384) return 11;

            pcc_gc_release(b);
            if (pcc_gc_backend4_zpage_count() - count0 != 1) return 12;
            if (pcc_gc_backend4_zpage_used_bytes() - used0 != 8192) return 13;
            if (pcc_gc_backend4_zpage_fragmentation_bytes() - fragmentation0 != 57344) return 14;

            pcc_gc_release(a);
            if (pcc_gc_backend4_zpage_count() != count0) return 15;
            if (pcc_gc_backend4_zpage_capacity_bytes() != capacity0) return 16;
            if (pcc_gc_backend4_zpage_used_bytes() != used0) return 17;
            if (pcc_gc_backend4_zpage_fragmentation_bytes() != fragmentation0) return 18;
            printf("backend4-medium-zpage-sharing-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-medium-zpage-sharing-ok"


def test_backend4_genzgc_reuses_empty_zpages_from_free_list(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            int64_t active0 = pcc_gc_backend4_zpage_count();
            int64_t free0 = pcc_gc_backend4_zpage_free_pages();
            int64_t freecap0 = pcc_gc_backend4_zpage_free_capacity_bytes();

            PyObject *small = pcc_gc_alloc(128, PY_TYPE_LIST, 0);
            if (small == 0) return 3;
            pcc_gc_release(small);
            if (pcc_gc_backend4_zpage_count() != active0) return 4;
            if (pcc_gc_backend4_zpage_free_pages() - free0 != 1) return 5;
            if (pcc_gc_backend4_zpage_free_capacity_bytes() - freecap0 != 4096) return 6;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_ZPAGE_FREE_PAGES) - free0 != 1) return 7;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_ZPAGE_FREE_CAPACITY_BYTES) - freecap0 != 4096) return 8;

            small = pcc_gc_alloc(128, PY_TYPE_LIST, 0);
            if (small == 0) return 9;
            if (pcc_gc_backend4_zpage_count() - active0 != 1) return 10;
            if (pcc_gc_backend4_zpage_free_pages() != free0) return 11;
            if (pcc_gc_backend4_zpage_free_capacity_bytes() != freecap0) return 12;
            pcc_gc_release(small);
            if (pcc_gc_backend4_zpage_free_pages() - free0 != 1) return 13;

            PyObject *medium = pcc_gc_alloc(8192, PY_TYPE_LIST, 0);
            if (medium == 0) return 14;
            pcc_gc_release(medium);
            if (pcc_gc_backend4_zpage_free_pages() - free0 != 2) return 15;
            if (pcc_gc_backend4_zpage_free_capacity_bytes() - freecap0 != 69632) return 16;

            medium = pcc_gc_alloc(8192, PY_TYPE_LIST, 0);
            if (medium == 0) return 17;
            if (pcc_gc_backend4_zpage_free_pages() - free0 != 1) return 18;
            if (pcc_gc_backend4_zpage_free_capacity_bytes() - freecap0 != 4096) return 19;
            pcc_gc_release(medium);
            if (pcc_gc_backend4_zpage_free_pages() - free0 != 2) return 20;

            PyObject *large = pcc_gc_alloc(70000, PY_TYPE_LIST, 0);
            if (large == 0) return 21;
            pcc_gc_release(large);
            if (pcc_gc_backend4_zpage_free_pages() - free0 != 2) return 22;
            if (pcc_gc_backend4_zpage_free_capacity_bytes() - freecap0 != 69632) return 23;

            printf("backend4-zpage-free-list-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-zpage-free-list-ok"


def test_backend4_genzgc_free_zpage_cache_is_bounded(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            int64_t free0 = pcc_gc_backend4_zpage_free_pages();
            int64_t freecap0 = pcc_gc_backend4_zpage_free_capacity_bytes();
            PyObject *small[320];
            PyObject *medium[48];

            for (int i = 0; i < 320; i++) {
                small[i] = pcc_gc_alloc(128, PY_TYPE_LIST, 0);
                if (small[i] == 0) return 3;
            }
            for (int i = 0; i < 320; i++) pcc_gc_release(small[i]);
            if (pcc_gc_backend4_zpage_free_pages() - free0 != 8) return 4;
            if (pcc_gc_backend4_zpage_free_capacity_bytes() - freecap0 != 32768) return 5;

            for (int i = 0; i < 48; i++) {
                medium[i] = pcc_gc_alloc(8192, PY_TYPE_LIST, 0);
                if (medium[i] == 0) return 6;
            }
            for (int i = 0; i < 48; i++) pcc_gc_release(medium[i]);
            if (pcc_gc_backend4_zpage_free_pages() - free0 != 12) return 7;
            if (pcc_gc_backend4_zpage_free_capacity_bytes() - freecap0 != 294912) return 8;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_ZPAGE_FREE_PAGES) - free0 != 12) return 9;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_ZPAGE_FREE_CAPACITY_BYTES) - freecap0 != 294912) return 10;

            printf("backend4-zpage-free-cache-bounded-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-zpage-free-cache-bounded-ok"


def test_backend4_genzgc_zpage_tracks_generation_age_pressure(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        enum {
            PY_FLAG_GC_YOUNG = 0x80,
            PY_FLAG_GC_OLD = 0x100
        };

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            int64_t young0 = pcc_gc_backend4_zpage_young_pages();
            int64_t old0 = pcc_gc_backend4_zpage_old_pages();
            int64_t count0 = pcc_gc_backend4_zpage_count();
            int64_t capacity0 = pcc_gc_backend4_zpage_capacity_bytes();
            int64_t policy0 = pcc_gc_backend4_zpage_policy_score();

            PyObject *young = pcc_gc_alloc(128, PY_TYPE_LIST, 0);
            PyObject *old = pcc_gc_alloc(128, PY_TYPE_LIST, PY_FLAG_GC_OLD);
            if (young == 0 || old == 0) return 3;

            int64_t expected_fragmentation = (4096 - 128) * 2;
            int64_t expected_policy = expected_fragmentation + 2 + 1;
            if (pcc_gc_backend4_zpage_young_pages() - young0 != 1) return 4;
            if (pcc_gc_backend4_zpage_old_pages() - old0 != 1) return 5;
            if (pcc_gc_backend4_zpage_count() - count0 != 2) return 12;
            if (pcc_gc_backend4_zpage_capacity_bytes() - capacity0 != 8192) return 13;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_ZPAGE_YOUNG_PAGES) - young0 != 1) return 6;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_ZPAGE_OLD_PAGES) - old0 != 1) return 7;
            if (pcc_gc_backend4_zpage_policy_score() - policy0 != expected_policy) return 8;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_ZPAGE_POLICY_SCORE) - policy0 != expected_policy) return 9;

            pcc_gc_release(old);
            pcc_gc_release(young);
            if (pcc_gc_backend4_zpage_young_pages() != young0) return 10;
            if (pcc_gc_backend4_zpage_old_pages() != old0) return 11;
            printf("backend4-zpage-age-pressure-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-zpage-age-pressure-ok"


def test_backend4_genzgc_zpage_tracks_owner_remembered_slots(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdint.h>
        #include <stdio.h>
        #include <stdlib.h>

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

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            ProbeListObject *owner = (ProbeListObject *)pcc_gc_alloc(
                sizeof(ProbeListObject),
                PY_TYPE_LIST,
                PY_FLAG_GC_OLD
            );
            if (owner == 0) return 3;
            owner->length = 9;
            owner->capacity = 9;
            owner->items = (PyObject **)calloc(9, sizeof(PyObject *));
            if (owner->items == 0) return 4;

            PyObject *a = py_list_new(0);
            PyObject *b = py_list_new(0);
            PyObject *c = py_list_new(0);
            if (a == 0 || b == 0 || c == 0) return 5;

            int64_t slots0 = pcc_gc_backend4_zpage_remembered_slots();
            int64_t cards0 = pcc_gc_backend4_zpage_remembered_cards();
            int64_t ratio0 = pcc_gc_backend4_zpage_remembered_card_ratio_per_mille();
            int64_t dirty0 = pcc_gc_backend4_zpage_dirty_pages();
            int64_t policy0 = pcc_gc_backend4_zpage_policy_score();
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_ZPAGE_REMEMBERED_SLOTS) != slots0) return 6;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_ZPAGE_REMEMBERED_CARDS) != cards0) return 20;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_ZPAGE_REMEMBERED_CARD_RATIO_PER_MILLE) != ratio0) return 21;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_ZPAGE_DIRTY_PAGES) != dirty0) return 22;

            pcc_gc_store_ptr((PyObject *)owner, &owner->items[0], a);
            if (pcc_gc_backend4_zpage_remembered_slots() - slots0 != 1) return 7;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_ZPAGE_REMEMBERED_SLOTS) - slots0 != 1) return 8;
            if (pcc_gc_backend4_zpage_remembered_cards() - cards0 != 1) return 9;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_ZPAGE_REMEMBERED_CARDS) - cards0 != 1) return 23;
            if (pcc_gc_backend4_zpage_remembered_card_ratio_per_mille() <= ratio0) return 24;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_ZPAGE_REMEMBERED_CARD_RATIO_PER_MILLE) != pcc_gc_backend4_zpage_remembered_card_ratio_per_mille()) return 25;
            if (pcc_gc_backend4_zpage_policy_score() - policy0 != 3) return 26;
            if (pcc_gc_backend4_zpage_dirty_pages() - dirty0 != 1) return 27;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_ZPAGE_DIRTY_PAGES) - dirty0 != 1) return 28;

            pcc_gc_store_ptr((PyObject *)owner, &owner->items[0], b);
            if (pcc_gc_backend4_zpage_remembered_slots() - slots0 != 1) return 10;
            if (pcc_gc_backend4_zpage_remembered_cards() - cards0 != 1) return 26;
            if (pcc_gc_backend4_zpage_dirty_pages() - dirty0 != 1) return 27;

            pcc_gc_store_ptr((PyObject *)owner, &owner->items[1], b);
            if (pcc_gc_backend4_zpage_remembered_slots() - slots0 != 2) return 11;
            if (pcc_gc_backend4_zpage_remembered_cards() - cards0 != 1) return 12;
            if (pcc_gc_backend4_zpage_policy_score() - policy0 != 4) return 13;
            if (pcc_gc_backend4_zpage_dirty_pages() - dirty0 != 1) return 29;

            pcc_gc_store_ptr((PyObject *)owner, &owner->items[8], c);
            if (pcc_gc_backend4_zpage_remembered_slots() - slots0 != 3) return 35;
            if (pcc_gc_backend4_zpage_remembered_cards() - cards0 != 1) return 36;
            if (pcc_gc_backend4_zpage_policy_score() - policy0 != 5) return 37;
            if (pcc_gc_backend4_zpage_dirty_pages() - dirty0 != 1) return 38;

            if (pcc_gc_backend4_remembered_page_clear_slot(&owner->items[0]) != 1) return 14;
            if (pcc_gc_backend4_zpage_remembered_slots() - slots0 != 2) return 15;
            if (pcc_gc_backend4_zpage_remembered_cards() - cards0 != 1) return 16;
            if (pcc_gc_backend4_zpage_dirty_pages() - dirty0 != 1) return 44;
            if (pcc_gc_backend4_remembered_page_clear_slot(&owner->items[0]) != 0) return 17;
            if (pcc_gc_backend4_zpage_remembered_slots() - slots0 != 2) return 18;
            if (pcc_gc_backend4_zpage_remembered_cards() - cards0 != 1) return 19;
            if (pcc_gc_backend4_remembered_page_clear_slot(&owner->items[1]) != 1) return 30;
            if (pcc_gc_backend4_zpage_remembered_slots() - slots0 != 1) return 31;
            if (pcc_gc_backend4_zpage_remembered_cards() - cards0 != 1) return 32;
            if (pcc_gc_backend4_remembered_page_clear_slot(&owner->items[8]) != 1) return 39;
            if (pcc_gc_backend4_zpage_remembered_slots() != slots0) return 40;
            if (pcc_gc_backend4_zpage_remembered_cards() != cards0) return 41;
            if (pcc_gc_backend4_zpage_remembered_card_ratio_per_mille() != ratio0) return 45;
            if (pcc_gc_backend4_zpage_policy_score() != policy0) return 42;
            if (pcc_gc_backend4_zpage_dirty_pages() != dirty0) return 43;

            owner->items[0] = 0;
            owner->items[1] = 0;
            owner->items[8] = 0;
            pcc_gc_release(a);
            pcc_gc_release(b);
            pcc_gc_release(c);
            pcc_gc_release((PyObject *)owner);
            printf("backend4-zpage-remembered-slots-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-zpage-remembered-slots-ok"


def test_backend4_genzgc_zpage_card_api_clears_owner_card(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdint.h>
        #include <stdio.h>
        #include <stdlib.h>

        enum {
            PY_FLAG_GC_YOUNG = 0x80,
            PY_FLAG_GC_OLD = 0x100
        };

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            ProbeListObject *owner = (ProbeListObject *)pcc_gc_alloc(
                sizeof(ProbeListObject),
                PY_TYPE_LIST,
                PY_FLAG_GC_OLD
            );
            if (owner == 0) return 3;
            owner->length = 9;
            owner->capacity = 9;
            owner->items = (PyObject **)calloc(9, sizeof(PyObject *));
            if (owner->items == 0) return 4;

            PyObject *a = py_list_new(0);
            PyObject *b = py_list_new(0);
            PyObject *c = py_list_new(0);
            if (a == 0 || b == 0 || c == 0) return 5;

            /* Place the small payload across a real 4 KiB boundary.  That
               makes this regression deterministic and proves that clearing
               one ZPage card removes every matching remembered slot even
               when the lower-level page table needs two entries. */
            free(owner->items);
            unsigned char *items_allocation =
                (unsigned char *)calloc(3 * 4096, 1);
            if (items_allocation == 0) return 35;
            uintptr_t page_base =
                ((uintptr_t)items_allocation + 4095) & ~(uintptr_t)4095;
            owner->items = (PyObject **)(page_base + 4096 - 16);

            int64_t slots0 = pcc_gc_backend4_zpage_remembered_slots();
            int64_t cards0 = pcc_gc_backend4_zpage_remembered_cards();
            int64_t dirty0 = pcc_gc_backend4_zpage_dirty_pages();
            int64_t pages0 = pcc_gc_backend4_remembered_page_entries();
            int64_t page_slots0 = pcc_gc_backend4_remembered_page_slot_entries();

            pcc_gc_store_ptr((PyObject *)owner, &owner->items[0], a);
            pcc_gc_store_ptr((PyObject *)owner, &owner->items[1], b);
            pcc_gc_store_ptr((PyObject *)owner, &owner->items[8], c);

            if (pcc_gc_backend4_zpage_remembered_slots() - slots0 != 3) return 6;
            if (pcc_gc_backend4_zpage_remembered_cards() - cards0 != 1) return 7;
            if (pcc_gc_backend4_zpage_dirty_pages() - dirty0 != 1) return 8;
            /* The low-level remembered-page table groups slots by their
               actual 4 KiB address page. */
            int64_t page_delta =
                pcc_gc_backend4_remembered_page_entries() - pages0;
            if (page_delta != 2) {
                fprintf(
                    stderr,
                    "remembered page delta=%lld slots=%lld (baseline pages=%lld slots=%lld)\\n",
                    (long long)page_delta,
                    (long long)(pcc_gc_backend4_remembered_page_slot_entries() - page_slots0),
                    (long long)pages0,
                    (long long)page_slots0
                );
                return 9;
            }
            if (pcc_gc_backend4_remembered_page_slot_entries() - page_slots0 != 3) return 10;
            if (pcc_gc_backend4_remembered_page_contains_slot(&owner->items[0]) != 1) return 32;
            if (pcc_gc_backend4_remembered_page_contains_slot(&owner->items[1]) != 1) return 33;
            if (pcc_gc_backend4_remembered_page_contains_slot(&owner->items[8]) != 1) return 34;

            if (pcc_gc_backend4_zpage_contains_remembered_card((PyObject *)owner, &owner->items[0]) != 1) return 11;
            if (pcc_gc_backend4_zpage_contains_remembered_card((PyObject *)owner, &owner->items[7]) != 1) return 12;
            if (pcc_gc_backend4_zpage_contains_remembered_card((PyObject *)owner, &owner->items[8]) != 1) return 13;
            if (pcc_gc_backend4_zpage_contains_remembered_card(a, &owner->items[0]) != 0) return 14;

            if (pcc_gc_backend4_zpage_clear_remembered_card((PyObject *)owner, &owner->items[0]) != 3) return 15;
            if (pcc_gc_backend4_remembered_page_contains_slot(&owner->items[0]) != 0) return 16;
            if (pcc_gc_backend4_remembered_page_contains_slot(&owner->items[1]) != 0) return 17;
            if (pcc_gc_backend4_remembered_page_contains_slot(&owner->items[8]) != 0) return 18;
            if (pcc_gc_backend4_zpage_contains_remembered_card((PyObject *)owner, &owner->items[0]) != 0) return 19;
            if (pcc_gc_backend4_zpage_contains_remembered_card((PyObject *)owner, &owner->items[8]) != 0) return 20;
            if (pcc_gc_backend4_zpage_remembered_slots() != slots0) return 21;
            if (pcc_gc_backend4_zpage_remembered_cards() != cards0) return 22;
            if (pcc_gc_backend4_zpage_dirty_pages() != dirty0) return 23;

            if (pcc_gc_backend4_zpage_clear_remembered_card((PyObject *)owner, &owner->items[0]) != 0) return 24;
            if (pcc_gc_backend4_zpage_clear_remembered_card(a, &owner->items[8]) != 0) return 25;
            if (pcc_gc_backend4_zpage_clear_remembered_card((PyObject *)owner, &owner->items[8]) != 0) return 26;
            if (pcc_gc_backend4_zpage_remembered_slots() != slots0) return 27;
            if (pcc_gc_backend4_zpage_remembered_cards() != cards0) return 28;
            if (pcc_gc_backend4_zpage_dirty_pages() != dirty0) return 29;
            if (pcc_gc_backend4_remembered_page_entries() != pages0) return 30;
            if (pcc_gc_backend4_remembered_page_slot_entries() != page_slots0) return 31;

            owner->items[0] = 0;
            owner->items[1] = 0;
            owner->items[8] = 0;
            owner->length = 0;
            owner->capacity = 0;
            owner->items = 0;
            free(items_allocation);
            pcc_gc_release(a);
            pcc_gc_release(b);
            pcc_gc_release(c);
            pcc_gc_release((PyObject *)owner);
            printf("backend4-zpage-card-api-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-zpage-card-api-ok"


def test_backend4_genzgc_zpage_card_refcount_is_shared_by_page_span_card(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdint.h>
        #include <stdio.h>
        #include <stdlib.h>

        enum {
            PY_FLAG_GC_YOUNG = 0x80,
            PY_FLAG_GC_OLD = 0x100
        };

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            ProbeListObject *left = (ProbeListObject *)pcc_gc_alloc(
                sizeof(ProbeListObject),
                PY_TYPE_LIST,
                PY_FLAG_GC_OLD
            );
            ProbeListObject *right = (ProbeListObject *)pcc_gc_alloc(
                sizeof(ProbeListObject),
                PY_TYPE_LIST,
                PY_FLAG_GC_OLD
            );
            if (left == 0 || right == 0) return 3;
            left->length = 1;
            left->capacity = 1;
            right->length = 1;
            right->capacity = 1;
            left->items = (PyObject **)calloc(1, sizeof(PyObject *));
            right->items = (PyObject **)calloc(1, sizeof(PyObject *));
            if (left->items == 0 || right->items == 0) return 4;

            if (pcc_gc_backend4_zpage_owner_span_card((PyObject *)left) != 0) return 5;
            if (pcc_gc_backend4_zpage_owner_span_card((PyObject *)right) != 0) return 6;

            PyObject *a = py_list_new(0);
            PyObject *b = py_list_new(0);
            if (a == 0 || b == 0) return 7;

            int64_t slots0 = pcc_gc_backend4_zpage_remembered_slots();
            int64_t cards0 = pcc_gc_backend4_zpage_remembered_cards();
            int64_t dirty0 = pcc_gc_backend4_zpage_dirty_pages();
            int64_t page_slots0 = pcc_gc_backend4_remembered_page_slot_entries();

            pcc_gc_store_ptr((PyObject *)left, &left->items[0], a);
            pcc_gc_store_ptr((PyObject *)right, &right->items[0], b);

            if (pcc_gc_backend4_zpage_remembered_slots() - slots0 != 2) return 8;
            if (pcc_gc_backend4_zpage_remembered_cards() - cards0 != 1) return 9;
            if (pcc_gc_backend4_zpage_dirty_pages() - dirty0 != 1) return 10;
            if (pcc_gc_backend4_remembered_page_slot_entries() - page_slots0 != 2) return 11;
            if (pcc_gc_backend4_zpage_contains_remembered_card((PyObject *)left, &left->items[0]) != 1) return 12;
            if (pcc_gc_backend4_zpage_contains_remembered_card((PyObject *)right, &right->items[0]) != 1) return 13;

            if (pcc_gc_backend4_zpage_clear_remembered_card((PyObject *)left, &left->items[0]) != 1) return 14;
            if (pcc_gc_backend4_remembered_page_contains_slot(&left->items[0]) != 0) return 15;
            if (pcc_gc_backend4_remembered_page_contains_slot(&right->items[0]) != 1) return 16;
            if (pcc_gc_backend4_zpage_remembered_slots() - slots0 != 1) return 17;
            if (pcc_gc_backend4_zpage_remembered_cards() - cards0 != 1) return 18;
            if (pcc_gc_backend4_zpage_dirty_pages() - dirty0 != 1) return 19;
            if (pcc_gc_backend4_zpage_contains_remembered_card((PyObject *)left, &left->items[0]) != 1) return 20;
            if (pcc_gc_backend4_zpage_contains_remembered_card((PyObject *)right, &right->items[0]) != 1) return 21;

            if (pcc_gc_backend4_zpage_clear_remembered_card((PyObject *)right, &right->items[0]) != 1) return 22;
            if (pcc_gc_backend4_remembered_page_contains_slot(&right->items[0]) != 0) return 23;
            if (pcc_gc_backend4_zpage_remembered_slots() != slots0) return 24;
            if (pcc_gc_backend4_zpage_remembered_cards() != cards0) return 25;
            if (pcc_gc_backend4_zpage_dirty_pages() != dirty0) return 26;
            if (pcc_gc_backend4_zpage_contains_remembered_card((PyObject *)left, &left->items[0]) != 0) return 27;
            if (pcc_gc_backend4_zpage_contains_remembered_card((PyObject *)right, &right->items[0]) != 0) return 28;

            left->items[0] = 0;
            right->items[0] = 0;
            pcc_gc_release(a);
            pcc_gc_release(b);
            pcc_gc_release((PyObject *)right);
            pcc_gc_release((PyObject *)left);
            printf("backend4-zpage-shared-span-card-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-zpage-shared-span-card-ok"


def test_backend4_genzgc_inline_slots_use_owner_slot_span_card(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stddef.h>
        #include <stdint.h>
        #include <stdio.h>

        enum {
            PY_FLAG_GC_YOUNG = 0x80,
            PY_FLAG_GC_OLD = 0x100
        };

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
            char pad0[520];
            PyObject *near_slot;
            char pad1[512];
            PyObject *far_slot;
        } ProbeInlineSlots;

        static int64_t expected_card(int64_t owner_offset, size_t slot_offset) {
            return ((owner_offset + (int64_t)slot_offset)
                / PCC_GC_BACKEND4_ZPAGE_SPAN_CARD_BYTES) % 64;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            ProbeInlineSlots *owner = (ProbeInlineSlots *)pcc_gc_alloc(
                sizeof(ProbeInlineSlots),
                PY_TYPE_LIST,
                PY_FLAG_GC_OLD
            );
            if (owner == 0) return 3;
            owner->length = 0;
            owner->capacity = 0;
            owner->items = 0;
            owner->near_slot = 0;
            owner->far_slot = 0;

            int64_t owner_offset = pcc_gc_backend4_zpage_owner_offset_bytes((PyObject *)owner);
            if (owner_offset < 0) return 4;
            int64_t near_card = pcc_gc_backend4_zpage_owner_slot_span_card(
                (PyObject *)owner,
                &owner->near_slot
            );
            int64_t far_card = pcc_gc_backend4_zpage_owner_slot_span_card(
                (PyObject *)owner,
                &owner->far_slot
            );
            if (near_card != expected_card(owner_offset, offsetof(ProbeInlineSlots, near_slot))) return 5;
            if (far_card != expected_card(owner_offset, offsetof(ProbeInlineSlots, far_slot))) return 6;
            if (near_card == far_card) return 7;

            PyObject *a = py_list_new(0);
            PyObject *b = py_list_new(0);
            if (a == 0 || b == 0) return 8;

            int64_t slots0 = pcc_gc_backend4_zpage_remembered_slots();
            int64_t cards0 = pcc_gc_backend4_zpage_remembered_cards();
            int64_t dirty0 = pcc_gc_backend4_zpage_dirty_pages();

            pcc_gc_store_ptr((PyObject *)owner, &owner->near_slot, a);
            pcc_gc_store_ptr((PyObject *)owner, &owner->far_slot, b);
            if (pcc_gc_backend4_zpage_remembered_slots() - slots0 != 2) return 9;
            if (pcc_gc_backend4_zpage_remembered_cards() - cards0 != 2) return 10;
            if (pcc_gc_backend4_zpage_dirty_pages() - dirty0 != 1) return 11;
            if (pcc_gc_backend4_zpage_contains_remembered_card((PyObject *)owner, &owner->near_slot) != 1) return 12;
            if (pcc_gc_backend4_zpage_contains_remembered_card((PyObject *)owner, &owner->far_slot) != 1) return 13;

            if (pcc_gc_backend4_zpage_clear_remembered_card((PyObject *)owner, &owner->near_slot) != 1) return 14;
            if (pcc_gc_backend4_remembered_page_contains_slot(&owner->near_slot) != 0) return 15;
            if (pcc_gc_backend4_remembered_page_contains_slot(&owner->far_slot) != 1) return 16;
            if (pcc_gc_backend4_zpage_remembered_slots() - slots0 != 1) return 17;
            if (pcc_gc_backend4_zpage_remembered_cards() - cards0 != 1) return 18;
            if (pcc_gc_backend4_zpage_dirty_pages() - dirty0 != 1) return 19;
            if (pcc_gc_backend4_zpage_contains_remembered_card((PyObject *)owner, &owner->near_slot) != 0) return 20;
            if (pcc_gc_backend4_zpage_contains_remembered_card((PyObject *)owner, &owner->far_slot) != 1) return 21;

            if (pcc_gc_backend4_zpage_clear_remembered_card((PyObject *)owner, &owner->far_slot) != 1) return 22;
            if (pcc_gc_backend4_zpage_remembered_slots() != slots0) return 23;
            if (pcc_gc_backend4_zpage_remembered_cards() != cards0) return 24;
            if (pcc_gc_backend4_zpage_dirty_pages() != dirty0) return 25;

            owner->near_slot = 0;
            owner->far_slot = 0;
            pcc_gc_release(a);
            pcc_gc_release(b);
            pcc_gc_release((PyObject *)owner);
            printf("backend4-inline-slot-span-card-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-inline-slot-span-card-ok"


def test_backend4_genzgc_payload_slots_use_registered_payload_span_card(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdint.h>
        #include <stdio.h>
        #include <stdlib.h>

        enum {
            PY_FLAG_GC_YOUNG = 0x80,
            PY_FLAG_GC_OLD = 0x100
        };

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        static int64_t expected_card(int64_t span_offset) {
            return (span_offset / PCC_GC_BACKEND4_ZPAGE_SPAN_CARD_BYTES) % 64;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            ProbeListObject *owner = (ProbeListObject *)pcc_gc_alloc(
                sizeof(ProbeListObject),
                PY_TYPE_LIST,
                PY_FLAG_GC_OLD
            );
            if (owner == 0) return 3;
            owner->length = 96;
            owner->capacity = 96;
            owner->items = (PyObject **)calloc(96, sizeof(PyObject *));
            if (owner->items == 0) return 4;

            int64_t payload_offset = pcc_gc_backend4_zpage_register_owner_payload_span(
                (PyObject *)owner,
                owner->items,
                96 * (int64_t)sizeof(PyObject *)
            );
            if (payload_offset < 0) return 5;

            int64_t first_card = pcc_gc_backend4_zpage_owner_slot_span_card(
                (PyObject *)owner,
                &owner->items[0]
            );
            int64_t far_card = pcc_gc_backend4_zpage_owner_slot_span_card(
                (PyObject *)owner,
                &owner->items[80]
            );
            if (first_card != expected_card(payload_offset)) return 6;
            if (far_card != expected_card(payload_offset + 80 * (int64_t)sizeof(PyObject *))) return 7;
            if (first_card == far_card) return 8;

            PyObject *a = py_list_new(0);
            PyObject *b = py_list_new(0);
            if (a == 0 || b == 0) return 9;

            int64_t slots0 = pcc_gc_backend4_zpage_remembered_slots();
            int64_t cards0 = pcc_gc_backend4_zpage_remembered_cards();
            int64_t dirty0 = pcc_gc_backend4_zpage_dirty_pages();

            pcc_gc_store_ptr((PyObject *)owner, &owner->items[0], a);
            pcc_gc_store_ptr((PyObject *)owner, &owner->items[80], b);
            if (pcc_gc_backend4_zpage_remembered_slots() - slots0 != 2) return 10;
            if (pcc_gc_backend4_zpage_remembered_cards() - cards0 != 2) return 11;
            if (pcc_gc_backend4_zpage_dirty_pages() - dirty0 != 1) return 12;
            if (pcc_gc_backend4_zpage_contains_remembered_card((PyObject *)owner, &owner->items[0]) != 1) return 13;
            if (pcc_gc_backend4_zpage_contains_remembered_card((PyObject *)owner, &owner->items[80]) != 1) return 14;

            if (pcc_gc_backend4_zpage_clear_remembered_card((PyObject *)owner, &owner->items[0]) != 1) return 15;
            if (pcc_gc_backend4_remembered_page_contains_slot(&owner->items[0]) != 0) return 16;
            if (pcc_gc_backend4_remembered_page_contains_slot(&owner->items[80]) != 1) return 17;
            if (pcc_gc_backend4_zpage_remembered_slots() - slots0 != 1) return 18;
            if (pcc_gc_backend4_zpage_remembered_cards() - cards0 != 1) return 19;
            if (pcc_gc_backend4_zpage_contains_remembered_card((PyObject *)owner, &owner->items[0]) != 0) return 20;
            if (pcc_gc_backend4_zpage_contains_remembered_card((PyObject *)owner, &owner->items[80]) != 1) return 21;

            if (pcc_gc_backend4_zpage_clear_remembered_card((PyObject *)owner, &owner->items[80]) != 1) return 22;
            if (pcc_gc_backend4_zpage_remembered_slots() != slots0) return 23;
            if (pcc_gc_backend4_zpage_remembered_cards() != cards0) return 24;
            if (pcc_gc_backend4_zpage_dirty_pages() != dirty0) return 25;

            owner->items[0] = 0;
            owner->items[80] = 0;
            pcc_gc_release(a);
            pcc_gc_release(b);
            pcc_gc_release((PyObject *)owner);
            printf("backend4-payload-slot-span-card-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-payload-slot-span-card-ok"


def test_backend4_genzgc_container_payload_allocators_register_spans(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_internal.h"
        #include <stdio.h>

        static int card_differs(int64_t a, int64_t b) {
            return a >= 0 && b >= 0 && a != b;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            PyObject *list_obj = py_list_new(96);
            if (list_obj == 0) return 3;
            PyListObject *list = (PyListObject *)list_obj;
            if (list->capacity < 96 || list->items == 0) return 4;
            int64_t list_card0 = pcc_gc_backend4_zpage_owner_slot_span_card(
                list_obj,
                &list->items[0]
            );
            int64_t list_card80 = pcc_gc_backend4_zpage_owner_slot_span_card(
                list_obj,
                &list->items[80]
            );
            if (!card_differs(list_card0, list_card80)) return 5;

            PyObject *dict_obj = py_dict_new();
            if (dict_obj == 0) return 6;
            PyDictObject *dict = (PyDictObject *)dict_obj;
            for (int64_t i = 0; i < 20; i++) {
                PyObject *key = py_int_from_i64(i);
                PyObject *value = py_int_from_i64(i + 1000);
                if (key == 0 || value == 0) return 7;
                py_dict_set(dict_obj, key, value);
                pcc_gc_release(key);
                pcc_gc_release(value);
            }
            if (dict->capacity <= 24 || dict->entries == 0) return 8;
            int64_t dict_card0 = pcc_gc_backend4_zpage_owner_slot_span_card(
                dict_obj,
                &dict->entries[0].key
            );
            int64_t dict_card24 = pcc_gc_backend4_zpage_owner_slot_span_card(
                dict_obj,
                &dict->entries[24].key
            );
            if (!card_differs(dict_card0, dict_card24)) return 9;

            pcc_gc_release(dict_obj);
            pcc_gc_release(list_obj);

            PyObject *set_obj = py_set_new();
            if (set_obj == 0) return 10;
            PySetObject *set = (PySetObject *)set_obj;
            for (int64_t i = 0; i < 22; i++) {
                PyObject *item = py_int_from_i64(i + 2000);
                if (item == 0) return 11;
                py_set_add(set_obj, item);
                pcc_gc_release(item);
            }
            if (set->capacity <= 32 || set->entries == 0) return 12;
            int64_t set_card0 = pcc_gc_backend4_zpage_owner_slot_span_card(
                set_obj,
                &set->entries[0].key
            );
            int64_t set_card32 = pcc_gc_backend4_zpage_owner_slot_span_card(
                set_obj,
                &set->entries[32].key
            );
            if (!card_differs(set_card0, set_card32)) return 13;

            pcc_gc_release(set_obj);
            printf("backend4-container-payload-span-registration-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-container-payload-span-registration-ok"


def test_backend4_relocated_container_payload_tables_register_spans(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_internal.h"
        #include <stdio.h>

        static int card_differs(int64_t a, int64_t b) {
            return a >= 0 && b >= 0 && a != b;
        }

        static int fill_dict(PyObject *dict_obj) {
            for (int64_t i = 0; i < 20; i++) {
                PyObject *key = py_int_from_i64(i);
                PyObject *value = py_int_from_i64(i + 1000);
                if (key == 0 || value == 0) return 0;
                py_dict_set(dict_obj, key, value);
                pcc_gc_release(key);
                pcc_gc_release(value);
            }
            return 1;
        }

        static int fill_set(PyObject *set_obj) {
            for (int64_t i = 0; i < 22; i++) {
                PyObject *item = py_int_from_i64(i + 2000);
                if (item == 0) return 0;
                py_set_add(set_obj, item);
                pcc_gc_release(item);
            }
            return 1;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            PyObject *list_obj = py_list_new(96);
            PyObject *dict_obj = py_dict_new();
            PyObject *set_obj = py_set_new();
            if (list_obj == 0 || dict_obj == 0 || set_obj == 0) return 3;
            if (!fill_dict(dict_obj)) return 4;
            if (!fill_set(set_obj)) return 5;

            pcc_gc_reset_relocation_set();
            if (pcc_gc_select_relocation_set(65536) <= 0) return 6;
            if (pcc_gc_relocation_set_contains(list_obj) != 1) return 7;
            if (pcc_gc_relocation_set_contains(dict_obj) != 1) return 8;
            if (pcc_gc_relocation_set_contains(set_obj) != 1) return 9;

            PyObject *list_moved = pcc_gc_relocate_copy(
                list_obj,
                sizeof(PyListObject)
            );
            PyObject *dict_moved = pcc_gc_relocate_copy(
                dict_obj,
                sizeof(PyDictObject)
            );
            PyObject *set_moved = pcc_gc_relocate_copy(
                set_obj,
                sizeof(PySetObject)
            );
            if (list_moved == 0 || dict_moved == 0 || set_moved == 0) return 10;

            PyListObject *list = (PyListObject *)list_moved;
            if (list->capacity < 96 || list->items == 0) return 11;
            int64_t list_card0 = pcc_gc_backend4_zpage_owner_slot_span_card(
                list_moved,
                &list->items[0]
            );
            int64_t list_card80 = pcc_gc_backend4_zpage_owner_slot_span_card(
                list_moved,
                &list->items[80]
            );
            if (!card_differs(list_card0, list_card80)) return 12;

            PyDictObject *dict = (PyDictObject *)dict_moved;
            if (dict->capacity <= 24 || dict->entries == 0) return 13;
            int64_t dict_card0 = pcc_gc_backend4_zpage_owner_slot_span_card(
                dict_moved,
                &dict->entries[0].key
            );
            int64_t dict_card24 = pcc_gc_backend4_zpage_owner_slot_span_card(
                dict_moved,
                &dict->entries[24].key
            );
            if (!card_differs(dict_card0, dict_card24)) return 14;

            PySetObject *set = (PySetObject *)set_moved;
            if (set->capacity <= 32 || set->entries == 0) return 15;
            int64_t set_card0 = pcc_gc_backend4_zpage_owner_slot_span_card(
                set_moved,
                &set->entries[0].key
            );
            int64_t set_card32 = pcc_gc_backend4_zpage_owner_slot_span_card(
                set_moved,
                &set->entries[32].key
            );
            if (!card_differs(set_card0, set_card32)) return 16;

            pcc_gc_release(list_moved);
            pcc_gc_release(dict_moved);
            pcc_gc_release(set_moved);
            printf("backend4-relocated-container-payload-span-registration-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (
        proc.stdout.strip()
        == "backend4-relocated-container-payload-span-registration-ok"
    )


def test_backend4_continuation_stack_slots_register_payload_spans(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_internal.h"
        #include <stdio.h>

        static int card_differs(int64_t a, int64_t b) {
            return a >= 0 && b >= 0 && a != b;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            int32_t frame_map[1] = {96};
            PyObject *slots[96];
            for (int64_t i = 0; i < 96; i++) {
                slots[i] = py_int_from_i64(i + 5000);
                if (slots[i] == 0) return 3;
            }
            PyObject *cont_obj = py_continuation_new(frame_map, slots, (void *)0x1234);
            for (int64_t i = 0; i < 96; i++) {
                pcc_gc_release(slots[i]);
            }
            if (cont_obj == 0) return 4;

            PyContinuationObject *cont = (PyContinuationObject *)cont_obj;
            if (cont->stack_chunk == 0 || cont->stack_chunk->slots == 0) return 5;
            int64_t initial_card0 = pcc_gc_backend4_zpage_owner_slot_span_card(
                cont_obj,
                &cont->stack_chunk->slots[0]
            );
            int64_t initial_card80 = pcc_gc_backend4_zpage_owner_slot_span_card(
                cont_obj,
                &cont->stack_chunk->slots[80]
            );
            if (!card_differs(initial_card0, initial_card80)) return 6;

            pcc_gc_reset_relocation_set();
            if (pcc_gc_select_relocation_set(65536) <= 0) return 7;
            if (pcc_gc_relocation_set_contains(cont_obj) != 1) return 8;

            PyObject *moved_obj = pcc_gc_relocate_copy(
                cont_obj,
                sizeof(PyContinuationObject)
            );
            if (moved_obj == 0) return 9;
            PyContinuationObject *moved = (PyContinuationObject *)moved_obj;
            if (moved->stack_chunk == 0 || moved->stack_chunk->slots == 0) return 10;
            int64_t moved_card0 = pcc_gc_backend4_zpage_owner_slot_span_card(
                moved_obj,
                &moved->stack_chunk->slots[0]
            );
            int64_t moved_card80 = pcc_gc_backend4_zpage_owner_slot_span_card(
                moved_obj,
                &moved->stack_chunk->slots[80]
            );
            if (!card_differs(moved_card0, moved_card80)) return 11;

            pcc_gc_release(moved_obj);
            printf("backend4-continuation-stack-payload-span-registration-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (
        proc.stdout.strip()
        == "backend4-continuation-stack-payload-span-registration-ok"
    )


def test_backend4_relocated_class_method_table_registers_payload_span(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_internal.h"
        #include <stdio.h>

        static PyObject *probe_entry(PyObject *args) {
            (void)args;
            return py_None;
        }

        static int card_differs(int64_t a, int64_t b) {
            return a >= 0 && b >= 0 && a != b;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            PyClassObject *cls = py_class_new("PayloadClass", 0, 0, 0, 0);
            if (cls == 0) return 3;
            PyObject *func = py_func_new((void *)probe_entry, py_None);
            if (func == 0) return 4;

            for (int i = 0; i < 96; i++) {
                py_class_add_method(cls, "m", func);
            }
            if (cls->methods == 0 || cls->n_methods <= 80) return 5;

            pcc_gc_reset_relocation_set();
            if (pcc_gc_select_relocation_set(65536) <= 0) return 6;
            if (pcc_gc_relocation_set_contains((PyObject *)cls) != 1) return 7;
            PyObject *moved_obj = pcc_gc_relocate_copy(
                (PyObject *)cls,
                sizeof(PyClassObject)
            );
            if (moved_obj == 0) return 8;
            PyClassObject *moved = (PyClassObject *)moved_obj;
            if (moved->methods == 0 || moved->n_methods <= 80) return 9;

            int64_t card0 = pcc_gc_backend4_zpage_owner_slot_span_card(
                moved_obj,
                &moved->methods[0].func
            );
            int64_t card80 = pcc_gc_backend4_zpage_owner_slot_span_card(
                moved_obj,
                &moved->methods[80].func
            );
            if (!card_differs(card0, card80)) return 10;

            pcc_gc_release(moved_obj);
            pcc_gc_release(func);
            printf("backend4-class-method-payload-span-registration-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-class-method-payload-span-registration-ok"


def test_backend4_class_creation_registers_bases_and_mro_payload_spans(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_internal.h"
        #include <stdio.h>

        static PyObject *probe_entry(PyObject *args) {
            (void)args;
            return py_None;
        }

        static int card_differs(int64_t a, int64_t b) {
            return a >= 0 && b >= 0 && a != b;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            PyClassObject *bases[72];
            for (int i = 0; i < 72; i++) {
                bases[i] = py_class_new("Base", 0, 0, 0, 0);
                if (bases[i] == 0) return 3;
            }

            PyClassObject *derived = py_class_new("Derived", bases, 72, 0, 0);
            if (derived == 0) return 4;
            if (derived->bases == 0 || derived->n_bases <= 64) return 5;
            if (derived->mro == 0 || derived->n_mro <= 64) return 6;

            int64_t bases_card0 = pcc_gc_backend4_zpage_owner_slot_span_card(
                (PyObject *)derived,
                (PyObject **)&derived->bases[0]
            );
            int64_t bases_card64 = pcc_gc_backend4_zpage_owner_slot_span_card(
                (PyObject *)derived,
                (PyObject **)&derived->bases[64]
            );
            if (!card_differs(bases_card0, bases_card64)) return 7;

            int64_t mro_card0 = pcc_gc_backend4_zpage_owner_slot_span_card(
                (PyObject *)derived,
                (PyObject **)&derived->mro[0]
            );
            int64_t mro_card64 = pcc_gc_backend4_zpage_owner_slot_span_card(
                (PyObject *)derived,
                (PyObject **)&derived->mro[64]
            );
            if (!card_differs(mro_card0, mro_card64)) return 8;

            printf("backend4-class-create-payload-span-registration-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-class-create-payload-span-registration-ok"


def test_backend4_class_method_growth_registers_payload_span(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_internal.h"
        #include <stdio.h>

        static PyObject *probe_entry(PyObject *args) {
            (void)args;
            return py_None;
        }

        static int card_differs(int64_t a, int64_t b) {
            return a >= 0 && b >= 0 && a != b;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            PyClassObject *cls = py_class_new("MethodPayloadClass", 0, 0, 0, 0);
            if (cls == 0) return 3;
            PyObject *func = py_func_new((void *)probe_entry, py_None);
            if (func == 0) return 4;

            for (int i = 0; i < 96; i++) {
                py_class_add_method(cls, "method", func);
            }
            if (cls->methods == 0 || cls->n_methods <= 80) return 5;

            int64_t card0 = pcc_gc_backend4_zpage_owner_slot_span_card(
                (PyObject *)cls,
                &cls->methods[0].func
            );
            int64_t card80 = pcc_gc_backend4_zpage_owner_slot_span_card(
                (PyObject *)cls,
                &cls->methods[80].func
            );
            if (!card_differs(card0, card80)) return 6;

            pcc_gc_release(func);
            printf("backend4-class-method-growth-payload-span-registration-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (
        proc.stdout.strip()
        == "backend4-class-method-growth-payload-span-registration-ok"
    )


def test_backend4_class_creation_payload_span_registration_is_mirrored_source():
    header = (RUNTIME_DIR / "include" / "py_runtime.h").read_text(encoding="utf-8")
    c_class = (RUNTIME_DIR / "src" / "py_class.c").read_text(encoding="utf-8")
    c_gc = (RUNTIME_DIR / "src" / "py_gc_backend.c").read_text(encoding="utf-8")
    py_class = (RUNTIME_DIR / "py" / "py_class.py").read_text(encoding="utf-8")
    py_gc = (RUNTIME_DIR / "py" / "py_gc_backend.py").read_text(encoding="utf-8")
    abi = (REPO_ROOT / "pcc" / "py_frontend" / "codegen" / "runtime_abi.py").read_text(
        encoding="utf-8"
    )

    assert "pcc_gc_backend4_zpage_unregister_owner_payload_span" in header
    assert "pcc_gc_backend4_zpage_retarget_owner_payload_span" in header
    assert (
        '@c_abi_export("pcc_gc_backend4_zpage_unregister_owner_payload_span")' in py_gc
    )
    assert '@c_abi_export("pcc_gc_backend4_zpage_retarget_owner_payload_span")' in py_gc
    assert '"pcc_gc_backend4_zpage_unregister_owner_payload_span":' in abi
    assert '"pcc_gc_backend4_zpage_retarget_owner_payload_span":' in abi
    assert "node->page->allocated_bytes = node->offset_bytes;" in c_gc
    # The zpage payload-span free moved to the freestanding zpage lifecycle
    # module as GC4 relocation policy migrated.
    zpage_lifecycle = (
        RUNTIME_DIR / "py" / "freestanding_gc_zpage_lifecycle.py"
    ).read_text(encoding="utf-8")
    assert "store_i64(page, 64, offset)" in zpage_lifecycle

    c_new = c_class.split("PyClassObject *py_class_new(", 1)[1].split(
        "void py_class_mark_slots_only", 1
    )[0]
    assert "pcc_gc_backend4_zpage_register_owner_payload_span(" in c_new
    assert "n_bases * (int64_t)sizeof(PyClassObject *)" in c_new
    assert "mro_len * (int64_t)sizeof(PyClassObject *)" in c_new

    c_add_method = c_class.split("void py_class_add_method(", 1)[1].split(
        "PyObject *py_class_lookup", 1
    )[0]
    assert "PyClassMethod *old_methods = cls->methods;" in c_add_method
    assert "pcc_gc_backend4_zpage_retarget_owner_payload_span(" in c_add_method
    assert "pcc_gc_backend4_zpage_unregister_owner_payload_span(" in c_add_method
    assert "pcc_gc_backend4_zpage_register_owner_payload_span(" in c_add_method
    assert "new_n * (int64_t)sizeof(PyClassMethod)" in c_add_method

    assert "pcc_gc_backend4_zpage_register_owner_payload_span = extern(" in py_class
    assert "pcc_gc_backend4_zpage_unregister_owner_payload_span = extern(" in py_class
    assert "pcc_gc_backend4_zpage_retarget_owner_payload_span = extern(" in py_class
    py_new = py_class.split("def py_class_new(", 1)[1].split(
        '@c_abi_export("py_class_mark_slots_only")', 1
    )[0]
    assert "pcc_gc_backend4_zpage_register_owner_payload_span(" in py_new
    assert "n_bases * 8" in py_new
    assert C_POINTER_SIZE == 8
    assert "mro_len * C_POINTER_SIZE" in py_new
    py_add_method = py_class.split("def py_class_add_method(", 1)[1].split(
        '@c_abi_export("py_class_set_metaclass")', 1
    )[0]
    assert "pcc_gc_backend4_zpage_retarget_owner_payload_span(" in py_add_method
    assert "pcc_gc_backend4_zpage_unregister_owner_payload_span(" in py_add_method
    assert "pcc_gc_backend4_zpage_register_owner_payload_span(" in py_add_method
    assert "new_n * 16" in py_add_method


def test_backend4_continuation_payload_span_registration_is_mirrored_source():
    c_coroutine = (RUNTIME_DIR / "src" / "py_coroutine.c").read_text(encoding="utf-8")
    c_gc = (RUNTIME_DIR / "src" / "py_gc_backend.c").read_text(encoding="utf-8")
    py_coroutine = (RUNTIME_DIR / "py" / "py_coroutine.py").read_text(encoding="utf-8")
    py_payload_source = STRICT_RELOCATION_PAYLOAD.read_text(encoding="utf-8")

    c_new = c_coroutine.split("static PyObject *py_continuation_new_with_abi(", 1)[1]
    c_new = c_new.split("PyObject *py_continuation_new(", 1)[0]
    assert "pcc_gc_backend4_zpage_register_owner_payload_span(" in c_new
    assert "n_slots * (int64_t)sizeof(PyObject *)" in c_new

    c_payload = c_gc.split(
        "static int pcc_gc_relocate_copy_payload_prepared_locked(", 1
    )[1].split("static int pcc_gc_relocate_copy_payload(", 1)[0]
    c_cont = c_payload.split("if (tag == PY_TYPE_CONTINUATION)", 1)[1].split(
        "if (tag == PY_TYPE_EXC)", 1
    )[0]
    assert "pcc_gc_backend4_zpage_register_owner_payload_span_unlocked(" in c_cont
    assert "src_chunk->slot_count * (int64_t)sizeof(PyObject *)" in c_cont

    py_new = py_coroutine.split("def _py_continuation_new_with_abi(", 1)[1]
    py_new = py_new.split('@c_abi_export("py_continuation_new")', 1)[0]
    assert "pcc_gc_backend4_zpage_register_owner_payload_span = extern(" in py_coroutine
    assert "pcc_gc_backend4_zpage_register_owner_payload_span(" in py_new
    assert "n_slots * 8" in py_new

    py_payload = py_payload_source.split(
        "def pcc_gc_relocate_copy_payload_prepared_locked", 1
    )[1].split('@c_abi_export("pcc_gc_relocate_copy_payload")', 1)[0]
    py_cont = py_payload.split(
        'if tag == abi_constant("object.type.continuation")', 1
    )[1].split(
        'if tag == abi_constant("object.type.exc")', 1
    )[0]
    assert "pcc_gc_backend4_zpage_register_owner_payload_span(" in py_cont
    assert "n_slots * 8" in py_cont


def test_backend4_relocated_payload_span_registration_is_mirrored_source():
    c_gc = (RUNTIME_DIR / "src" / "py_gc_backend.c").read_text(encoding="utf-8")
    py_gc = (RUNTIME_DIR / "py" / "py_gc_backend.py").read_text(encoding="utf-8")
    py_payload_source = STRICT_RELOCATION_PAYLOAD.read_text(encoding="utf-8")

    assert (
        "static int64_t " "pcc_gc_backend4_zpage_register_owner_payload_span_unlocked("
    ) in c_gc
    assert "pcc_gc_backend4_zpage_remove_payload_span_base_unlocked(" in c_gc
    assert "_backend4_zpage_remove_payload_span_base(" in py_gc
    assert "span_existing = load_ptr(node, 64)" in py_gc
    assert "store_i64(span_existing, 16, size_bytes)" in py_gc
    assert "allocated_existing < end_existing" in py_gc
    unlocked_start = c_gc.index(
        "static int64_t " "pcc_gc_backend4_zpage_register_owner_payload_span_unlocked("
    )
    unlocked_body = c_gc[
        unlocked_start : c_gc.index(
            "int64_t pcc_gc_backend4_zpage_register_owner_payload_span(",
            unlocked_start,
        )
    ]
    assert (
        "pcc_gc_backend4_zpage_remove_payload_spans_unlocked(node)" not in unlocked_body
    )
    assert "span->base != (uint8_t *)base" in unlocked_body
    assert "span->size_bytes = size_bytes" in unlocked_body
    assert "page->allocated_bytes < end" in unlocked_body
    public_start = c_gc.index(
        "int64_t pcc_gc_backend4_zpage_register_owner_payload_span("
    )
    public_body = c_gc[
        public_start : c_gc.index(
            "int64_t pcc_gc_backend4_zpage_fragmentation_per_mille(",
            public_start,
        )
    ]
    assert "pcc_gc_graph_lock();" in public_body
    assert "pcc_gc_backend4_zpage_register_owner_payload_span_unlocked(" in public_body
    assert "pcc_gc_backend4_zpage_remove_payload_span_base_unlocked(" in public_body
    assert "pcc_gc_graph_unlock();" in public_body

    c_payload = c_gc.split(
        "static int pcc_gc_relocate_copy_payload_prepared_locked(", 1
    )[1].split("static int pcc_gc_relocate_copy_payload(", 1)[0]
    c_dict = c_payload.split("if (tag == PY_TYPE_DICT)", 1)[1].split(
        "if (tag == PY_TYPE_SET)", 1
    )[0]
    c_set = c_payload.split("if (tag == PY_TYPE_SET)", 1)[1].split(
        "if (tag == PY_TYPE_TUPLE)", 1
    )[0]
    c_list = c_payload.split("PyListObject *src = (PyListObject *)from;", 1)[1].split(
        "return 0;\n}", 1
    )[0]
    for body, size_expr in (
        (c_dict, "capacity * (int64_t)sizeof(DictEntry)"),
        (c_set, "capacity * (int64_t)sizeof(SetEntry)"),
        (c_list, "capacity * (int64_t)sizeof(PyObject *)"),
    ):
        assert "pcc_gc_backend4_zpage_register_owner_payload_span_unlocked(" in body
        assert size_expr in body
    c_class = c_payload.split("if (tag == PY_TYPE_CLASS)", 1)[1].split(
        "if (tag == PY_TYPE_WEAKREF)", 1
    )[0]
    for size_expr in (
        "n_bases * (int64_t)sizeof(PyClassObject *)",
        "n_mro * (int64_t)sizeof(PyClassObject *)",
        "n_methods * (int64_t)sizeof(PyClassMethod)",
    ):
        assert "pcc_gc_backend4_zpage_register_owner_payload_span_unlocked(" in c_class
        assert size_expr in c_class

    py_payload = py_payload_source.split(
        "def pcc_gc_relocate_copy_payload_prepared_locked", 1
    )[1].split('@c_abi_export("pcc_gc_relocate_copy_payload")', 1)[0]
    py_dict = py_payload.split(
        'if tag == abi_constant("object.type.dict")', 1
    )[1].split(
        'if tag == abi_constant("object.type.set")', 1
    )[0]
    py_set = py_payload.split(
        'if tag == abi_constant("object.type.set")', 1
    )[1].split(
        'if tag == abi_constant("object.type.tuple")', 1
    )[0]
    py_list = py_payload.split(
        'if tag == abi_constant("object.type.list")', 1
    )[1].split(
        "return 1\n\n    return 1", 1
    )[0]
    for body, size_expr in (
        (py_dict, "capacity * 24"),
        (py_set, "capacity * 16"),
        (py_list, "capacity * 8"),
    ):
        assert "pcc_gc_backend4_zpage_register_owner_payload_span(" in body
        assert size_expr in body
    py_class = py_payload.split(
        'if tag == abi_constant("object.type.class")', 1
    )[1].split(
        'if tag == abi_constant("object.type.weakref")', 1
    )[0]
    assert "pcc_gc_backend4_zpage_register_owner_payload_span(" in py_class
    for size_expr in (
        'n_bases * abi_constant("object.pointer.size")',
        'n_mro * abi_constant("object.pointer.size")',
        'n_methods * abi_constant("object.class_method.size")',
    ):
        assert size_expr in py_class


def test_backend4_genzgc_relocation_retargets_remembered_list_slots(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdint.h>
        #include <stdio.h>
        #include <stdlib.h>

        enum {
            PY_FLAG_GC_YOUNG = 0x80,
            PY_FLAG_GC_OLD = 0x100
        };

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            ProbeListObject *owner = (ProbeListObject *)pcc_gc_alloc(
                sizeof(ProbeListObject),
                PY_TYPE_LIST,
                PY_FLAG_GC_OLD
            );
            if (owner == 0) return 3;
            owner->length = 2;
            owner->capacity = 2;
            owner->items = (PyObject **)calloc(2, sizeof(PyObject *));
            if (owner->items == 0) return 4;

            PyObject *young = py_list_new(0);
            if (young == 0) return 5;

            int64_t slots0 = pcc_gc_backend4_zpage_remembered_slots();
            int64_t cards0 = pcc_gc_backend4_zpage_remembered_cards();
            int64_t dirty0 = pcc_gc_backend4_zpage_dirty_pages();

            PyObject **old_slot = &owner->items[0];
            pcc_gc_store_ptr((PyObject *)owner, old_slot, young);
            if (pcc_gc_backend4_remembered_page_contains_slot(old_slot) != 1) return 6;
            if (pcc_gc_backend4_zpage_remembered_slots() - slots0 != 1) return 7;
            if (pcc_gc_backend4_zpage_remembered_cards() - cards0 != 1) return 8;
            if (pcc_gc_backend4_zpage_dirty_pages() - dirty0 != 1) return 9;

            pcc_gc_publish_initialized((PyObject *)owner);
            if (pcc_gc_select_relocation_set(8) <= 0) return 10;
            if (pcc_gc_relocation_set_contains((PyObject *)owner) != 1) return 11;
            PyObject *moved_raw = pcc_gc_relocate_copy(
                (PyObject *)owner,
                (int64_t)sizeof(ProbeListObject)
            );
            if (moved_raw == 0) return 12;
            ProbeListObject *moved = (ProbeListObject *)moved_raw;
            if (moved == owner) return 13;
            if (moved->items == owner->items) return 14;
            if (moved->items[0] != young) return 15;

            PyObject **new_slot = &moved->items[0];
            if (pcc_gc_backend4_remembered_page_contains_slot(old_slot) != 0) return 16;
            if (pcc_gc_backend4_remembered_page_contains_slot(new_slot) != 1) return 17;
            if (pcc_gc_backend4_zpage_remembered_slots() - slots0 != 1) return 18;
            if (pcc_gc_backend4_zpage_remembered_cards() - cards0 != 1) return 19;
            if (pcc_gc_backend4_zpage_dirty_pages() - dirty0 != 1) return 20;

            if (pcc_gc_backend4_remembered_page_clear_slot(new_slot) != 1) return 21;
            if (pcc_gc_backend4_zpage_remembered_slots() != slots0) return 22;
            if (pcc_gc_backend4_zpage_remembered_cards() != cards0) return 23;
            if (pcc_gc_backend4_zpage_dirty_pages() != dirty0) return 24;

            moved->items[0] = 0;
            py_decref(moved_raw);
            py_decref(young);
            printf("backend4-remembered-slot-retarget-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-remembered-slot-retarget-ok"


def test_backend4_genzgc_relocation_retargets_inline_tuple_slots(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdint.h>
        #include <stdio.h>

        enum {
            PY_FLAG_GC_YOUNG = 0x80,
            PY_FLAG_GC_OLD = 0x100
        };

        typedef struct {
            PyObjectHeader h;
            int64_t len;
            PyObject *items[2];
        } ProbeTupleObject;

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            ProbeTupleObject *owner = (ProbeTupleObject *)pcc_gc_alloc(
                sizeof(ProbeTupleObject),
                PY_TYPE_TUPLE,
                PY_FLAG_GC_OLD
            );
            if (owner == 0) return 3;
            owner->len = 2;

            PyObject *young = py_list_new(0);
            if (young == 0) return 4;

            int64_t slots0 = pcc_gc_backend4_zpage_remembered_slots();
            int64_t cards0 = pcc_gc_backend4_zpage_remembered_cards();
            int64_t dirty0 = pcc_gc_backend4_zpage_dirty_pages();

            PyObject **old_slot = &owner->items[0];
            pcc_gc_store_ptr((PyObject *)owner, old_slot, young);
            if (pcc_gc_backend4_remembered_page_contains_slot(old_slot) != 1) return 5;
            if (pcc_gc_backend4_zpage_remembered_slots() - slots0 != 1) return 6;

            pcc_gc_publish_initialized((PyObject *)owner);
            if (pcc_gc_select_relocation_set(8) <= 0) return 7;
            if (pcc_gc_relocation_set_contains((PyObject *)owner) != 1) return 8;
            PyObject *moved_raw = pcc_gc_relocate_copy(
                (PyObject *)owner,
                (int64_t)sizeof(ProbeTupleObject)
            );
            if (moved_raw == 0) return 9;
            ProbeTupleObject *moved = (ProbeTupleObject *)moved_raw;
            if (moved == owner) return 10;
            if (moved->items[0] != young) return 11;

            PyObject **new_slot = &moved->items[0];
            if (pcc_gc_backend4_remembered_page_contains_slot(old_slot) != 0) return 12;
            if (pcc_gc_backend4_remembered_page_contains_slot(new_slot) != 1) return 13;
            if (pcc_gc_backend4_zpage_remembered_slots() - slots0 != 1) return 14;
            if (pcc_gc_backend4_zpage_remembered_cards() - cards0 != 1) return 15;
            if (pcc_gc_backend4_zpage_dirty_pages() - dirty0 != 1) return 16;

            if (pcc_gc_backend4_remembered_page_clear_slot(new_slot) != 1) return 17;
            if (pcc_gc_backend4_zpage_remembered_slots() != slots0) return 18;
            if (pcc_gc_backend4_zpage_remembered_cards() != cards0) return 19;
            if (pcc_gc_backend4_zpage_dirty_pages() != dirty0) return 20;

            moved->items[0] = 0;
            py_decref(moved_raw);
            py_decref(young);
            printf("backend4-inline-slot-retarget-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-inline-slot-retarget-ok"


def test_backend4_genzgc_selector_uses_zpage_remembered_pressure(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdint.h>
        #include <stdio.h>
        #include <stdlib.h>

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

        static ProbeListObject *new_owner(void) {
            ProbeListObject *owner = (ProbeListObject *)pcc_gc_alloc(
                sizeof(ProbeListObject),
                PY_TYPE_LIST,
                PY_FLAG_GC_OLD
            );
            if (owner == 0) return 0;
            owner->length = 1;
            owner->capacity = 1;
            owner->items = (PyObject **)calloc(1, sizeof(PyObject *));
            if (owner->items == 0) return 0;
            pcc_gc_publish_initialized((PyObject *)owner);
            return owner;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            ProbeListObject *dirty = new_owner();
            ProbeListObject *clean = new_owner();
            PyObject *young = pcc_gc_alloc(
                64,
                PY_TYPE_LIST,
                PY_FLAG_GC_YOUNG | PY_FLAG_GC_PINNED
            );
            if (dirty == 0 || clean == 0 || young == 0) return 3;

            pcc_gc_store_ptr((PyObject *)dirty, &dirty->items[0], young);
            if (pcc_gc_backend4_zpage_remembered_slots() < 1) return 4;
            pcc_gc_publish_initialized((PyObject *)young);
            if (pcc_gc_select_relocation_set(1) != 1) return 5;
            if (pcc_gc_relocation_set_contains((PyObject *)dirty) != 1) return 6;
            if (pcc_gc_relocation_set_contains((PyObject *)clean) != 0) return 7;

            pcc_gc_reset_relocation_set();
            dirty->items[0] = 0;
            pcc_gc_release(young);
            pcc_gc_release((PyObject *)clean);
            pcc_gc_release((PyObject *)dirty);
            printf("backend4-zpage-remembered-selector-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-zpage-remembered-selector-ok"


def test_backend4_genzgc_selector_prefers_old_zpage_age_pressure(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        enum {
            PY_FLAG_GC_YOUNG = 0x80,
            PY_FLAG_GC_OLD = 0x100
        };

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();
            pcc_gc_reset_relocation_set();

            PyObject *old_obj = pcc_gc_alloc(128, PY_TYPE_LIST, PY_FLAG_GC_OLD);
            PyObject *young_obj = pcc_gc_alloc(128, PY_TYPE_LIST, 0);
            if (old_obj == 0 || young_obj == 0) return 3;
            if (pcc_gc_backend4_zpage_old_pages() < 1) return 4;
            if (pcc_gc_backend4_zpage_young_pages() < 1) return 5;

            if (pcc_gc_select_relocation_set(1) != 1) return 6;
            if (pcc_gc_relocation_set_contains(old_obj) != 1) return 7;
            if (pcc_gc_relocation_set_contains(young_obj) != 0) return 8;

            pcc_gc_reset_relocation_set();
            pcc_gc_release(young_obj);
            pcc_gc_release(old_obj);
            printf("backend4-zpage-age-selector-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-zpage-age-selector-ok"


def test_backend4_genzgc_selector_skips_zero_benefit_zpage(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();
            pcc_gc_reset_relocation_set();

            PyObject *exact = pcc_gc_alloc(4096, PY_TYPE_LIST, 0);
            if (exact == 0) return 3;
            if (pcc_gc_backend4_zpage_fragmentation_bytes() != 0) return 4;
            if (pcc_gc_backend4_zpage_remembered_slots() != 0) return 5;
            if (pcc_gc_backend4_zpage_fragmented_pages() != 0) return 8;
            if (pcc_gc_select_relocation_set(1) != 0) return 6;
            if (pcc_gc_relocation_set_contains(exact) != 0) return 7;

            pcc_gc_release(exact);
            printf("backend4-zpage-zero-benefit-selector-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-zpage-zero-benefit-selector-ok"


def test_backend4_genzgc_remembered_page_telemetry_groups_dirty_slots(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdint.h>
        #include <stdio.h>
        #include <stdlib.h>

        enum {
            PY_FLAG_GC_YOUNG = 0x80,
            PY_FLAG_GC_OLD = 0x100
        };

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            pcc_gc_telemetry_reset();

            ProbeListObject *owner = (ProbeListObject *)pcc_gc_alloc(
                sizeof(ProbeListObject),
                PY_TYPE_LIST,
                PY_FLAG_GC_OLD
            );
            if (owner == 0) return 3;
            owner->length = 2;
            owner->capacity = 2;
            owner->items = (PyObject **)calloc(2, sizeof(PyObject *));
            if (owner->items == 0) return 4;

            PyObject *a = py_list_new(0);
            PyObject *b = py_list_new(0);
            if (a == 0 || b == 0) return 5;

            pcc_gc_store_ptr((PyObject *)owner, &owner->items[0], a);
            pcc_gc_store_ptr((PyObject *)owner, &owner->items[1], b);
            if (pcc_gc_backend4_remembered_set_entries() != 2) return 6;
            if (pcc_gc_backend4_remembered_page_entries() != 1) return 7;
            if (pcc_gc_backend4_remembered_page_slot_entries() != 2) return 8;
            if (pcc_gc_backend4_remembered_page_high_water() != 1) return 9;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_REMEMBERED_PAGE_ENTRIES) != 1) return 10;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_REMEMBERED_PAGE_SLOT_ENTRIES) != 2) return 11;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_REMEMBERED_PAGE_HIGH_WATER) != 1) return 12;

            if (pcc_gc_set_backend(PCC_GC_KIND_REFCOUNT_CYCLE) != 0) return 13;
            if (pcc_gc_backend4_remembered_page_entries() != 0) return 14;
            if (pcc_gc_backend4_remembered_page_slot_entries() != 0) return 15;
            owner->items[0] = 0;
            owner->items[1] = 0;
            pcc_gc_release(a);
            pcc_gc_release(b);
            pcc_gc_release((PyObject *)owner);
            printf("backend4-remembered-page-telemetry-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-remembered-page-telemetry-ok"


def test_backend4_genzgc_evacuation_incomplete_batches_track_budget_backlog(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;
            PyObject *small_root = 0;
            PyObject *medium_root = 0;
            pcc_gc_scheduler_root_register(&small_root);
            pcc_gc_scheduler_root_register(&medium_root);

            PyObject *small = pcc_gc_alloc(128, PY_TYPE_LIST, 0x100);
            PyObject *medium = pcc_gc_alloc(8192, PY_TYPE_LIST, 0x100);
            if (small == 0 || medium == 0) return 3;
            pcc_gc_store_root(&small_root, small);
            pcc_gc_store_root(&medium_root, medium);

            pcc_gc_telemetry_reset();
            pcc_gc_publish_initialized((PyObject *)small);
            pcc_gc_publish_initialized((PyObject *)medium);
            if (pcc_gc_select_relocation_set(32) != 2) return 4;
            if (pcc_gc_relocation_set_size() != 2) return 5;
            if (pcc_gc_step(1) < 1) return 6;
            if (pcc_gc_relocation_set_size() != 1) return 7;
            if (pcc_gc_backend4_evacuation_incomplete_batches() != 1) return 8;
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_EVACUATION_INCOMPLETE_BATCHES) != 1) return 9;
            if (pcc_gc_step(1) < 1) return 10;
            if (pcc_gc_relocation_set_size() != 0) return 11;
            if (pcc_gc_backend4_evacuation_incomplete_batches() != 1) return 12;

            pcc_gc_store_root(&small_root, 0);
            pcc_gc_store_root(&medium_root, 0);
            pcc_gc_scheduler_root_unregister(&small_root);
            pcc_gc_scheduler_root_unregister(&medium_root);
            printf("backend4-genzgc-evacuation-backlog-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-genzgc-evacuation-backlog-ok"


def test_backend4_genzgc_evacuation_drain_preserves_page_handoff_until_empty(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        enum {
            PY_FLAG_GC_OLD = 0x100
        };

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            PyObject *a = pcc_gc_alloc(128, PY_TYPE_LIST, PY_FLAG_GC_OLD);
            PyObject *b = pcc_gc_alloc(128, PY_TYPE_LIST, PY_FLAG_GC_OLD);
            if (a == 0 || b == 0) return 3;

            pcc_gc_telemetry_reset();
            pcc_gc_publish_initialized((PyObject *)a);
            pcc_gc_publish_initialized((PyObject *)b);
            if (pcc_gc_select_relocation_set(8) != 2) return 4;
            if (pcc_gc_relocation_set_size() != 2) return 5;
            if (pcc_gc_backend4_evacuation_page_candidate_score() != 1) return 6;

            if (pcc_gc_backend4_evacuation_drain(1) != 1) return 7;
            if (pcc_gc_relocation_set_size() != 1) return 8;
            if (pcc_gc_backend4_evacuation_page_candidate_score() != 1) return 9;
            if (pcc_gc_backend4_evacuation_incomplete_batches() != 1) return 10;
            if (pcc_gc_backend4_evacuation_efficiency_per_mille() != 500) return 11;

            if (pcc_gc_backend4_evacuation_drain(1) != 1) return 12;
            if (pcc_gc_relocation_set_size() != 0) return 13;
            if (pcc_gc_backend4_evacuation_page_candidate_score() != 0) return 14;
            if (pcc_gc_backend4_evacuation_incomplete_batches() != 1) return 15;
            if (pcc_gc_backend4_evacuation_efficiency_per_mille() != 1000) return 16;
            if (pcc_gc_backend4_evacuation_drain(1) != 0) return 17;

            printf("backend4-evacuation-drain-page-handoff-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-evacuation-drain-page-handoff-ok"


def test_backend4_genzgc_evacuation_page_handoff_reports_current_pressure(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdint.h>
        #include <stdio.h>
        #include <stdlib.h>

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

        static ProbeListObject *new_old_owner(void) {
            ProbeListObject *owner = (ProbeListObject *)pcc_gc_alloc(
                128,
                PY_TYPE_LIST,
                PY_FLAG_GC_OLD
            );
            if (owner == 0) return 0;
            owner->length = 1;
            owner->capacity = 1;
            owner->items = (PyObject **)calloc(1, sizeof(PyObject *));
            if (owner->items == 0) return 0;
            pcc_gc_publish_initialized((PyObject *)owner);
            return owner;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            ProbeListObject *dirty = new_old_owner();
            ProbeListObject *clean = new_old_owner();
            PyObject *young = pcc_gc_alloc(
                64,
                PY_TYPE_LIST,
                PY_FLAG_GC_YOUNG | PY_FLAG_GC_PINNED
            );
            if (dirty == 0 || clean == 0 || young == 0) return 3;

            pcc_gc_store_ptr((PyObject *)dirty, &dirty->items[0], young);
            pcc_gc_telemetry_reset();
            pcc_gc_publish_initialized((PyObject *)young);
            if (pcc_gc_select_relocation_set(8) != 2) return 4;
            if (pcc_gc_backend4_evacuation_page_candidate_score() != 1) return 5;
            if (pcc_gc_backend4_evacuation_page_candidate_bytes() != 256) return 6;
            if (pcc_gc_backend4_evacuation_page_dirty_cards() != 1) return 7;

            if (pcc_gc_backend4_evacuation_drain(1) != 1) return 8;
            if (pcc_gc_backend4_evacuation_page_candidate_score() != 1) return 9;
            if (pcc_gc_backend4_evacuation_page_candidate_bytes() != 128) return 10;

            if (pcc_gc_backend4_evacuation_drain(1) != 1) return 11;
            if (pcc_gc_backend4_evacuation_page_candidate_score() != 0) return 12;
            if (pcc_gc_backend4_evacuation_page_candidate_bytes() != 0) return 13;
            if (pcc_gc_backend4_evacuation_page_dirty_cards() != 0) return 14;

            printf("backend4-evacuation-page-pressure-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-evacuation-page-pressure-ok"


def test_backend4_genzgc_evacuation_page_drain_moves_whole_selected_page(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        enum {
            PY_FLAG_GC_OLD = 0x100
        };

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            PyObject *a = pcc_gc_alloc(128, PY_TYPE_LIST, PY_FLAG_GC_OLD);
            PyObject *b = pcc_gc_alloc(128, PY_TYPE_LIST, PY_FLAG_GC_OLD);
            if (a == 0 || b == 0) return 3;

            pcc_gc_telemetry_reset();
            pcc_gc_publish_initialized((PyObject *)a);
            pcc_gc_publish_initialized((PyObject *)b);
            if (pcc_gc_select_relocation_set(8) != 2) return 4;
            if (pcc_gc_relocation_set_size() != 2) return 5;
            if (pcc_gc_backend4_evacuation_page_candidate_score() != 1) return 6;
            if (pcc_gc_backend4_evacuation_page_candidate_bytes() != 256) return 7;

            if (pcc_gc_backend4_evacuation_page_drain(1) != 2) return 8;
            if (pcc_gc_relocation_set_size() != 0) return 9;
            if (pcc_gc_backend4_evacuation_page_candidate_score() != 0) return 10;
            if (pcc_gc_backend4_evacuation_page_candidate_bytes() != 0) return 11;
            if (pcc_gc_backend4_evacuation_efficiency_per_mille() != 1000) return 12;
            if (pcc_gc_backend4_evacuation_page_drain(1) != 0) return 13;

            printf("backend4-evacuation-page-drain-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-evacuation-page-drain-ok"


def test_backend4_genzgc_step_drains_selected_zpage_as_page_budget(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        enum {
            PY_FLAG_GC_OLD = 0x100
        };

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            PyObject *a = pcc_gc_alloc(128, PY_TYPE_LIST, PY_FLAG_GC_OLD);
            PyObject *b = pcc_gc_alloc(128, PY_TYPE_LIST, PY_FLAG_GC_OLD);
            if (a == 0 || b == 0) return 3;
            if (pcc_gc_backend4_zpage_owner_offset_bytes(a) != 0) return 4;
            if (pcc_gc_backend4_zpage_owner_offset_bytes(b) != 128) return 5;

            pcc_gc_telemetry_reset();
            pcc_gc_publish_initialized((PyObject *)a);
            pcc_gc_publish_initialized((PyObject *)b);
            if (pcc_gc_select_relocation_set(8) != 2) return 6;
            if (pcc_gc_relocation_set_size() != 2) return 7;
            if (pcc_gc_backend4_evacuation_page_candidate_score() != 1) return 8;
            if (pcc_gc_backend4_evacuation_page_candidate_bytes() != 256) return 9;

            if (pcc_gc_step(1) != 2) return 10;
            if (pcc_gc_relocation_set_size() != 0) return 11;
            if (pcc_gc_backend4_evacuation_page_candidate_score() != 0) return 12;
            if (pcc_gc_backend4_evacuation_page_candidate_bytes() != 0) return 13;
            if (pcc_gc_backend4_evacuation_efficiency_per_mille() != 1000) return 14;

            printf("backend4-step-page-drain-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-step-page-drain-ok"


def test_backend4_genzgc_step_selects_and_drains_whole_zpage(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        enum {
            PY_FLAG_GC_YOUNG = 0x80
        };

        static int offsets_are_one_page_pair(PyObject *a, PyObject *b) {
            int64_t oa = pcc_gc_backend4_zpage_owner_offset_bytes(a);
            int64_t ob = pcc_gc_backend4_zpage_owner_offset_bytes(b);
            return ((oa == 0 && ob == 128) || (oa == 128 && ob == 0));
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            PyObject *root_a = 0;
            PyObject *root_b = 0;
            pcc_gc_scheduler_root_register(&root_a);
            pcc_gc_scheduler_root_register(&root_b);

            int64_t active0 = pcc_gc_backend4_zpage_count();
            int64_t free0 = pcc_gc_backend4_zpage_free_pages();
            int64_t freecap0 = pcc_gc_backend4_zpage_free_capacity_bytes();

            PyObject *a = pcc_gc_alloc(128, PY_TYPE_LIST, 0);
            PyObject *b = pcc_gc_alloc(128, PY_TYPE_LIST, 0);
            if (a == 0 || b == 0) return 3;
            pcc_gc_store_root(&root_a, a);
            pcc_gc_store_root(&root_b, b);
            ((PyObjectHeader *)a)->flags &= ~PY_FLAG_GC_YOUNG;
            ((PyObjectHeader *)b)->flags &= ~PY_FLAG_GC_YOUNG;
            if (pcc_gc_backend4_zpage_count() - active0 != 1) return 4;
            if (!offsets_are_one_page_pair(a, b)) return 5;

            /* Clearing YOUNG deliberately leaves two stale generation-aging
             * worklist entries.  Consume that earlier GC4 phase first so the
             * one-unit step below measures page selection/drain itself. */
            pcc_gc_publish_initialized((PyObject *)a);
            pcc_gc_publish_initialized((PyObject *)b);
            if (pcc_gc_step(2) != 2) return 19;
            if (pcc_gc_relocation_set_size() != 0) return 20;
            pcc_gc_telemetry_reset();
            int64_t step_work = pcc_gc_step(1);
            if (step_work != 2) {
                fprintf(stderr, "whole-page step work=%lld\\n", (long long)step_work);
                return 6;
            }
            if (pcc_gc_relocation_set_size() != 0) return 7;
            if (pcc_gc_backend4_evacuation_page_candidate_score() != 0) return 8;
            if (pcc_gc_backend4_evacuation_page_candidate_bytes() != 0) return 9;

            PyObject *moved_a = pcc_gc_load_ptr(0, &root_a);
            PyObject *moved_b = pcc_gc_load_ptr(0, &root_b);
            if (moved_a == 0 || moved_b == 0) return 10;
            if (moved_a == a || moved_b == b) return 11;
            if (!offsets_are_one_page_pair(moved_a, moved_b)) return 12;
            if (pcc_gc_backend4_zpage_owner_offset_bytes(a) != -1) return 13;
            if (pcc_gc_backend4_zpage_owner_offset_bytes(b) != -1) return 14;
            (void)pcc_gc_step(256);
            (void)pcc_gc_step(256);
            if (pcc_gc_backend4_zpage_count() - active0 != 1) return 15;
            if (pcc_gc_backend4_zpage_free_pages() != free0) return 16;
            if (pcc_gc_backend4_zpage_free_capacity_bytes() != freecap0) return 17;
            if (pcc_gc_backend4_verify_no_old_addresses() != 1) return 18;

            pcc_gc_store_root(&root_a, 0);
            pcc_gc_store_root(&root_b, 0);
            pcc_gc_scheduler_root_unregister(&root_a);
            pcc_gc_scheduler_root_unregister(&root_b);
            printf("backend4-step-selects-drains-whole-zpage-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-step-selects-drains-whole-zpage-ok"


def test_backend4_genzgc_page_drain_retires_source_zpage_without_reusing_retained_span(
    tmp_path,
):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        static int offsets_are_one_page_pair(PyObject *a, PyObject *b) {
            int64_t oa = pcc_gc_backend4_zpage_owner_offset_bytes(a);
            int64_t ob = pcc_gc_backend4_zpage_owner_offset_bytes(b);
            return ((oa == 0 && ob == 128) || (oa == 128 && ob == 0));
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            PyObject *root_a = 0;
            PyObject *root_b = 0;
            pcc_gc_scheduler_root_register(&root_a);
            pcc_gc_scheduler_root_register(&root_b);

            int64_t active0 = pcc_gc_backend4_zpage_count();
            int64_t free0 = pcc_gc_backend4_zpage_free_pages();
            int64_t freecap0 = pcc_gc_backend4_zpage_free_capacity_bytes();

            PyObject *a = pcc_gc_alloc(128, PY_TYPE_LIST, 0);
            PyObject *b = pcc_gc_alloc(128, PY_TYPE_LIST, 0);
            if (a == 0 || b == 0) return 3;
            pcc_gc_store_root(&root_a, a);
            pcc_gc_store_root(&root_b, b);
            if (pcc_gc_backend4_zpage_count() - active0 != 1) return 4;
            if (!offsets_are_one_page_pair(a, b)) return 5;

            pcc_gc_telemetry_reset();
            pcc_gc_publish_initialized((PyObject *)a);
            pcc_gc_publish_initialized((PyObject *)b);
            if (pcc_gc_select_relocation_set(8) != 2) return 6;
            if (pcc_gc_backend4_evacuation_page_candidate_score() != 1) return 7;
            if (pcc_gc_backend4_evacuation_page_drain(1) != 2) return 8;
            if (pcc_gc_backend4_evacuation_page_candidate_score() != 0) return 9;

            PyObject *moved_a = pcc_gc_load_ptr(0, &root_a);
            PyObject *moved_b = pcc_gc_load_ptr(0, &root_b);
            if (moved_a == 0 || moved_b == 0) return 10;
            if (moved_a == a || moved_b == b) return 11;
            if (!offsets_are_one_page_pair(moved_a, moved_b)) return 12;
            if (pcc_gc_backend4_zpage_owner_offset_bytes(a) != -1) return 13;
            if (pcc_gc_backend4_zpage_owner_offset_bytes(b) != -1) return 14;

            (void)pcc_gc_step(256);
            (void)pcc_gc_step(256);
            if (pcc_gc_backend4_zpage_count() - active0 != 1) return 15;
            if (pcc_gc_backend4_zpage_free_pages() != free0) return 16;
            if (pcc_gc_backend4_zpage_free_capacity_bytes() != freecap0) return 17;
            if (pcc_gc_backend4_verify_no_old_addresses() != 1) return 18;

            pcc_gc_store_root(&root_a, 0);
            pcc_gc_store_root(&root_b, 0);
            pcc_gc_scheduler_root_unregister(&root_a);
            pcc_gc_scheduler_root_unregister(&root_b);
            printf("backend4-page-drain-retires-source-zpage-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-page-drain-retires-source-zpage-ok"


def test_backend4_dict_get_loads_forwarded_key_and_value_slots(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        typedef struct {
            PyObjectHeader h;
            int64_t len;
            PyObject *items[1];
        } ProbeTupleObject;

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        static PyObject *new_hashable_payload(void) {
            ProbeTupleObject *tuple = (ProbeTupleObject *)pcc_gc_alloc(
                64,
                PY_TYPE_TUPLE,
                0
            );
            if (tuple == 0) return 0;
            tuple->len = 1;
            pcc_gc_store_ptr((PyObject *)tuple, &tuple->items[0], py_None);
            pcc_gc_publish_initialized((PyObject *)tuple);
            return (PyObject *)tuple;
        }

        static PyObject *new_pointer_payload(void) {
            ProbeListObject *list = (ProbeListObject *)pcc_gc_alloc(
                64,
                PY_TYPE_LIST,
                0
            );
            if (list == 0) return 0;
            list->length = 0;
            list->capacity = 0;
            list->items = 0;
            pcc_gc_publish_initialized((PyObject *)list);
            return (PyObject *)list;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            PyObject *dict = py_dict_new();
            PyObject *key = new_hashable_payload();
            PyObject *value = new_pointer_payload();
            if (dict == 0 || key == 0 || value == 0) return 3;

            int64_t key_id = pcc_gc_object_id(key);
            int64_t value_id = pcc_gc_object_id(value);
            py_dict_set(dict, key, value);

            pcc_gc_telemetry_reset();
            if (pcc_gc_select_relocation_set(256) <= 0) return 4;
            if (pcc_gc_relocation_set_contains(key) != 1) return 5;
            if (pcc_gc_relocation_set_contains(value) != 1) return 6;
            PyObject *new_key = pcc_gc_relocate_copy(key, 64);
            PyObject *new_value = pcc_gc_relocate_copy(value, 64);
            if (new_key == 0 || new_value == 0) return 7;

            PyObject *got = py_dict_get(dict, new_key);
            if (got == 0) return 8;
            if (got == value) return 9;
            if (got != new_value) return 10;
            if (pcc_gc_object_id(new_key) != key_id) return 11;
            if (pcc_gc_object_id(got) != value_id) return 12;
            py_decref(got);

            PyObject *got_again = py_dict_get(dict, new_key);
            if (got_again != new_value) return 13;
            py_decref(got_again);

            pcc_gc_release(new_key);
            pcc_gc_release(new_value);
            py_decref(dict);
            printf("backend4-dict-forwarded-slots-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-dict-forwarded-slots-ok"


def test_backend4_dict_traversal_loads_forwarded_key_and_value_slots(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        typedef struct {
            int64_t hash;
            PyObject *key;
            PyObject *value;
        } ProbeDictEntry;

        typedef struct {
            PyObjectHeader h;
            int64_t size;
            int64_t capacity;
            int64_t *indices;
            ProbeDictEntry *entries;
            int64_t entries_used;
        } ProbeDictObject;

        typedef struct {
            PyObjectHeader h;
            int64_t len;
            PyObject *items[1];
        } ProbeTupleObject;

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        static PyObject *new_hashable_payload(void) {
            ProbeTupleObject *tuple = (ProbeTupleObject *)pcc_gc_alloc(
                64,
                PY_TYPE_TUPLE,
                0
            );
            if (tuple == 0) return 0;
            tuple->len = 1;
            pcc_gc_store_ptr((PyObject *)tuple, &tuple->items[0], py_None);
            pcc_gc_publish_initialized((PyObject *)tuple);
            return (PyObject *)tuple;
        }

        static PyObject *new_pointer_payload(void) {
            ProbeListObject *list = (ProbeListObject *)pcc_gc_alloc(
                64,
                PY_TYPE_LIST,
                0
            );
            if (list == 0) return 0;
            list->length = 0;
            list->capacity = 0;
            list->items = 0;
            pcc_gc_publish_initialized((PyObject *)list);
            return (PyObject *)list;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            PyObject *dict = py_dict_new();
            PyObject *key = new_hashable_payload();
            PyObject *value = new_pointer_payload();
            if (dict == 0 || key == 0 || value == 0) return 3;
            py_dict_set(dict, key, value);

            pcc_gc_telemetry_reset();
            if (pcc_gc_select_relocation_set(256) <= 0) return 4;
            if (pcc_gc_relocation_set_contains(key) != 1) return 5;
            if (pcc_gc_relocation_set_contains(value) != 1) return 6;
            PyObject *new_key = pcc_gc_relocate_copy(key, 64);
            PyObject *new_value = pcc_gc_relocate_copy(value, 64);
            if (new_key == 0 || new_value == 0) return 7;

            PyObject *keys = py_dict_keys(dict);
            PyObject *values = py_dict_values(dict);
            if (keys == 0 || values == 0) return 8;
            PyObject *got_key = py_list_get(keys, 0);
            PyObject *got_value = py_list_get(values, 0);
            if (got_key != new_key) return 9;
            if (got_value != new_value) return 10;

            ProbeDictObject *probe = (ProbeDictObject *)dict;
            if (probe->entries[0].key == key) return 11;
            if (probe->entries[0].value == value) return 12;
            if (probe->entries[0].key != new_key) return 13;
            if (probe->entries[0].value != new_value) return 14;

            py_decref(got_key);
            py_decref(got_value);
            py_decref(keys);
            py_decref(values);
            pcc_gc_release(new_key);
            pcc_gc_release(new_value);
            py_decref(dict);
            printf("backend4-dict-traversal-forwarded-slots-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-dict-traversal-forwarded-slots-ok"


def test_backend4_set_contains_loads_forwarded_key_slot(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include <stdio.h>

        typedef struct {
            int64_t hash;
            PyObject *key;
        } ProbeSetEntry;

        typedef struct {
            PyObjectHeader h;
            int64_t size;
            int64_t capacity;
            int64_t fill;
            ProbeSetEntry *entries;
        } ProbeSetObject;

        typedef struct {
            PyObjectHeader h;
            int64_t len;
            PyObject *items[1];
        } ProbeTupleObject;

        static PyObject *new_hashable_payload(void) {
            ProbeTupleObject *tuple = (ProbeTupleObject *)pcc_gc_alloc(
                64,
                PY_TYPE_TUPLE,
                0
            );
            if (tuple == 0) return 0;
            tuple->len = 1;
            pcc_gc_store_ptr((PyObject *)tuple, &tuple->items[0], py_None);
            pcc_gc_publish_initialized((PyObject *)tuple);
            return (PyObject *)tuple;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            PyObject *set = py_set_new();
            PyObject *value = new_hashable_payload();
            if (set == 0 || value == 0) return 3;
            py_set_add(set, value);
            int64_t value_id = pcc_gc_object_id(value);

            pcc_gc_telemetry_reset();
            if (pcc_gc_select_relocation_set(128) <= 0) return 4;
            if (pcc_gc_relocation_set_contains(value) != 1) return 5;
            PyObject *moved_value = pcc_gc_relocate_copy(value, 64);
            if (moved_value == 0) return 6;

            if (py_set_contains(set, moved_value) != 1) return 7;

            ProbeSetObject *probe = (ProbeSetObject *)set;
            int64_t seen_moved = 0;
            for (int64_t i = 0; i < probe->capacity; i++) {
                PyObject *slot_key = probe->entries[i].key;
                if (slot_key == value) return 8;
                if (slot_key == moved_value) seen_moved++;
            }
            if (seen_moved != 1) return 9;
            if (pcc_gc_object_id(moved_value) != value_id) return 10;

            pcc_gc_release(moved_value);
            pcc_gc_release(value);
            pcc_gc_release(set);
            printf("backend4-set-forwarded-key-slot-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-set-forwarded-key-slot-ok"


def test_backend4_obj_compare_loads_forwarded_container_slots(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include "py_internal.h"
        #include <stdio.h>

        typedef struct {
            PyObjectHeader h;
            int64_t len;
            PyObject *items[1];
        } ProbeTupleObject;

        typedef struct {
            PyObjectHeader h;
            int64_t length;
            int64_t capacity;
            PyObject **items;
        } ProbeListObject;

        static PyObject *new_hashable_payload(void) {
            ProbeTupleObject *tuple = (ProbeTupleObject *)pcc_gc_alloc(
                64,
                PY_TYPE_TUPLE,
                0
            );
            if (tuple == 0) return 0;
            tuple->len = 1;
            pcc_gc_store_ptr((PyObject *)tuple, &tuple->items[0], py_None);
            pcc_gc_publish_initialized((PyObject *)tuple);
            return (PyObject *)tuple;
        }

        static PyObject *new_list_payload(void) {
            ProbeListObject *list = (ProbeListObject *)pcc_gc_alloc(
                64,
                PY_TYPE_LIST,
                0
            );
            if (list == 0) return 0;
            list->length = 0;
            list->capacity = 0;
            list->items = 0;
            pcc_gc_publish_initialized((PyObject *)list);
            return (PyObject *)list;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            PyObject *tuple_a = py_tuple_new(1);
            PyObject *tuple_b = py_tuple_new(1);
            PyObject *list_a = py_list_new(1);
            PyObject *list_b = py_list_new(1);
            PyObject *dict_a = py_dict_new();
            PyObject *dict_b = py_dict_new();
            PyObject *set_a = py_set_new();
            PyObject *set_b = py_set_new();
            PyObject *tuple_s = new_list_payload();
            PyObject *list_s = new_list_payload();
            PyObject *dict_k = new_hashable_payload();
            PyObject *dict_v = new_list_payload();
            PyObject *set_s = new_hashable_payload();
            if (tuple_a == 0 || tuple_b == 0 || list_a == 0 || list_b == 0) return 3;
            if (dict_a == 0 || dict_b == 0 || set_a == 0 || set_b == 0) return 4;
            if (tuple_s == 0 || list_s == 0 || dict_k == 0 || dict_v == 0 || set_s == 0) return 5;

            py_tuple_set_item(tuple_a, 0, tuple_s);
            py_list_append(list_a, list_s);
            py_dict_set(dict_a, dict_k, dict_v);
            py_set_add(set_a, set_s);

            pcc_gc_telemetry_reset();
            if (pcc_gc_select_relocation_set(512) <= 0) return 6;
            if (pcc_gc_relocation_set_contains(tuple_s) != 1) return 19;
            if (pcc_gc_relocation_set_contains(list_s) != 1) return 20;
            if (pcc_gc_relocation_set_contains(dict_k) != 1) return 21;
            if (pcc_gc_relocation_set_contains(dict_v) != 1) return 22;
            if (pcc_gc_relocation_set_contains(set_s) != 1) return 23;
            PyObject *tuple_moved = pcc_gc_relocate_copy(tuple_s, 64);
            PyObject *list_moved = pcc_gc_relocate_copy(list_s, 64);
            PyObject *dict_k_moved = pcc_gc_relocate_copy(dict_k, 64);
            PyObject *dict_v_moved = pcc_gc_relocate_copy(dict_v, 64);
            PyObject *set_moved = pcc_gc_relocate_copy(set_s, 64);
            if (tuple_moved == 0) return 24;
            if (list_moved == 0) return 25;
            if (dict_k_moved == 0) return 26;
            if (dict_v_moved == 0) return 27;
            if (set_moved == 0) return 28;

            py_tuple_set_item(tuple_b, 0, tuple_moved);
            py_list_append(list_b, list_moved);
            py_dict_set(dict_b, dict_k_moved, dict_v_moved);
            py_set_add(set_b, set_moved);

            if (py_obj_eq(tuple_a, tuple_b) != 1) return 7;
            if (((PyTupleObject *)tuple_a)->items[0] != tuple_moved) return 8;
            if (py_obj_eq(list_a, list_b) != 1) return 9;
            if (((PyListObject *)list_a)->items[0] != list_moved) return 10;
            if (py_obj_eq(dict_a, dict_b) != 1) return 11;
            if (((PyDictObject *)dict_a)->entries[0].key != dict_k_moved) return 12;
            if (((PyDictObject *)dict_a)->entries[0].value != dict_v_moved) return 13;
            if (py_obj_eq(set_a, set_b) != 1) return 14;

            int64_t saw_set_moved = 0;
            PySetObject *sa = (PySetObject *)set_a;
            for (int64_t i = 0; i < sa->capacity; i++) {
                if (sa->entries[i].key == set_s) return 15;
                if (sa->entries[i].key == set_moved) saw_set_moved++;
            }
            if (saw_set_moved != 1) return 16;

            PyObject *sorted_set = py_obj_sorted(set_a);
            if (sorted_set == 0) return 17;
            PyObject *sorted_item = py_list_get(sorted_set, 0);
            if (sorted_item != set_moved) return 18;
            py_decref(sorted_item);
            py_decref(sorted_set);

            pcc_gc_release(tuple_moved);
            pcc_gc_release(list_moved);
            pcc_gc_release(dict_k_moved);
            pcc_gc_release(dict_v_moved);
            pcc_gc_release(set_moved);
            py_decref(tuple_a);
            py_decref(tuple_b);
            py_decref(list_a);
            py_decref(list_b);
            py_decref(dict_a);
            py_decref(dict_b);
            py_decref(set_a);
            py_decref(set_b);
            printf("backend4-obj-compare-forwarded-slots-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-obj-compare-forwarded-slots-ok"


def test_backend4_json_dumps_loads_forwarded_container_slots(tmp_path):
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

        static PyObject *new_list_payload(void) {
            ProbeListObject *list = (ProbeListObject *)pcc_gc_alloc(
                64,
                PY_TYPE_LIST,
                0
            );
            if (list == 0) return 0;
            list->length = 0;
            list->capacity = 0;
            list->items = 0;
            pcc_gc_publish_initialized((PyObject *)list);
            return (PyObject *)list;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            PyObject *lst = py_list_new(1);
            PyObject *dict = py_dict_new();
            PyObject *list_value = new_list_payload();
            PyObject *dict_key = py_str_new("json-key", 8);
            PyObject *dict_value = new_list_payload();
            if (lst == 0 || dict == 0 || list_value == 0 || dict_key == 0 || dict_value == 0) return 3;
            py_list_append(lst, list_value);
            py_dict_set(dict, dict_key, dict_value);

            pcc_gc_telemetry_reset();
            if (pcc_gc_select_relocation_set(256) <= 0) return 4;
            if (pcc_gc_relocation_set_contains(list_value) != 1) return 5;
            if (pcc_gc_relocation_set_contains(dict_key) != 0) return 6;
            if (pcc_gc_relocation_set_contains(dict_value) != 1) return 7;

            PyObject *list_moved = pcc_gc_relocate_copy(list_value, 64);
            PyObject *value_moved = pcc_gc_relocate_copy(dict_value, 64);
            if (list_moved == 0 || value_moved == 0) return 8;

            PyObject *list_json = py_json_dumps(lst);
            PyObject *dict_json = py_json_dumps(dict);
            if (list_json == 0 || dict_json == 0) return 9;
            PyObject *expected_list = py_str_new("[[]]", 4);
            PyObject *expected_dict = py_str_new("{\\"json-key\\": []}", 16);
            if (expected_list == 0 || expected_dict == 0) return 10;
            if (py_str_eq(list_json, expected_list) != 1) return 11;
            if (py_str_eq(dict_json, expected_dict) != 1) return 12;

            if (((PyListObject *)lst)->items[0] != list_moved) return 13;
            if (((PyDictObject *)dict)->entries[0].key != dict_key) return 14;
            if (((PyDictObject *)dict)->entries[0].value != value_moved) return 15;

            py_decref(expected_list);
            py_decref(expected_dict);
            py_decref(list_json);
            py_decref(dict_json);
            pcc_gc_release(list_moved);
            pcc_gc_release(value_moved);
            py_decref(lst);
            py_decref(dict);
            printf("backend4-json-forwarded-slots-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-json-forwarded-slots-ok"


def test_backend4_print_format_loads_forwarded_sequence_slots(tmp_path):
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

        static PyObject *new_list_payload(void) {
            ProbeListObject *list = (ProbeListObject *)pcc_gc_alloc(
                64,
                PY_TYPE_LIST,
                0
            );
            if (list == 0) return 0;
            list->length = 0;
            list->capacity = 0;
            list->items = 0;
            pcc_gc_publish_initialized((PyObject *)list);
            return (PyObject *)list;
        }

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            PyObject *lst = py_list_new(1);
            PyObject *tuple = py_tuple_new(1);
            PyObject *many = py_tuple_new(1);
            PyObject *list_value = new_list_payload();
            PyObject *tuple_value = new_list_payload();
            PyObject *many_value = new_list_payload();
            if (lst == 0 || tuple == 0 || many == 0) return 3;
            if (list_value == 0 || tuple_value == 0 || many_value == 0) return 4;
            py_list_append(lst, list_value);
            py_tuple_set_item(tuple, 0, tuple_value);
            py_tuple_set_item(many, 0, many_value);

            pcc_gc_telemetry_reset();
            if (pcc_gc_select_relocation_set(256) <= 0) return 5;
            if (pcc_gc_relocation_set_contains(list_value) != 1) return 6;
            if (pcc_gc_relocation_set_contains(tuple_value) != 1) return 7;
            if (pcc_gc_relocation_set_contains(many_value) != 1) return 8;

            PyObject *list_moved = pcc_gc_relocate_copy(list_value, 64);
            PyObject *tuple_moved = pcc_gc_relocate_copy(tuple_value, 64);
            PyObject *many_moved = pcc_gc_relocate_copy(many_value, 64);
            if (list_moved == 0 || tuple_moved == 0 || many_moved == 0) return 9;

            py_print(lst);
            py_print(tuple);
            py_print_many(many, py_None, py_None);

            if (((PyListObject *)lst)->items[0] != list_moved) return 10;
            if (((PyTupleObject *)tuple)->items[0] != tuple_moved) return 11;
            if (((PyTupleObject *)many)->items[0] != many_moved) return 12;

            pcc_gc_release(list_moved);
            pcc_gc_release(tuple_moved);
            pcc_gc_release(many_moved);
            py_decref(lst);
            py_decref(tuple);
            py_decref(many);
            printf("backend4-print-forwarded-slots-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout == (
        "[[]]\n" "([],)\n" "[]\n" "backend4-print-forwarded-slots-ok\n"
    )


def test_backend4_os_path_helpers_leave_leaf_string_sequence_slots_unforwarded(
    tmp_path,
):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include "py_internal.h"
        #include <stdio.h>

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            PyObject *join_parts = py_list_new(2);
            PyObject *common_paths = py_tuple_new(2);
            PyObject *join_a = py_str_new("root", 4);
            PyObject *join_b = py_str_new("leaf", 4);
            PyObject *common_a = py_str_new("/tmp/pcc/a", 10);
            PyObject *common_b = py_str_new("/tmp/pcc/b", 10);
            if (join_parts == 0 || common_paths == 0) return 3;
            if (join_a == 0 || join_b == 0 || common_a == 0 || common_b == 0) return 4;

            py_list_append(join_parts, join_a);
            py_list_append(join_parts, join_b);
            py_tuple_set_item(common_paths, 0, common_a);
            py_tuple_set_item(common_paths, 1, common_b);

            pcc_gc_telemetry_reset();
            (void)pcc_gc_select_relocation_set(256);
            if (pcc_gc_relocation_set_contains(join_a) != 0) return 6;
            if (pcc_gc_relocation_set_contains(join_b) != 0) return 7;
            if (pcc_gc_relocation_set_contains(common_a) != 0) return 8;
            if (pcc_gc_relocation_set_contains(common_b) != 0) return 9;

            PyObject *joined = py_os_path_join(join_parts);
            PyObject *common = py_os_path_commonpath(common_paths);
            PyObject *expected_join = py_str_new("root/leaf", 9);
            PyObject *expected_common = py_str_new("/tmp/pcc", 8);
            if (joined == 0 || common == 0 || expected_join == 0 || expected_common == 0) return 11;
            if (py_str_eq(joined, expected_join) != 1) return 12;
            if (py_str_eq(common, expected_common) != 1) return 13;

            if (((PyListObject *)join_parts)->items[0] != join_a) return 14;
            if (((PyListObject *)join_parts)->items[1] != join_b) return 15;
            if (((PyTupleObject *)common_paths)->items[0] != common_a) return 16;
            if (((PyTupleObject *)common_paths)->items[1] != common_b) return 17;

            py_decref(joined);
            py_decref(common);
            py_decref(expected_join);
            py_decref(expected_common);
            py_decref(join_parts);
            py_decref(common_paths);
            printf("backend4-os-path-leaf-string-slots-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-os-path-leaf-string-slots-ok"


def test_backend4_str_join_leaves_leaf_string_list_items_unforwarded(tmp_path):
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_runtime.h"
        #include "py_internal.h"
        #include <stdio.h>

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            PyObject *sep = py_str_new("/", 1);
            PyObject *lst = py_list_new(2);
            PyObject *left = py_str_new("left", 4);
            PyObject *right = py_str_new("right", 5);
            if (sep == 0 || lst == 0 || left == 0 || right == 0) return 3;
            py_list_append(lst, left);
            py_list_append(lst, right);

            pcc_gc_telemetry_reset();
            (void)pcc_gc_select_relocation_set(128);
            if (pcc_gc_relocation_set_contains(left) != 0) return 5;
            if (pcc_gc_relocation_set_contains(right) != 0) return 6;

            PyObject *joined = py_str_join(sep, lst);
            PyObject *expected = py_str_new("left/right", 10);
            if (joined == 0 || expected == 0) return 8;
            if (py_str_eq(joined, expected) != 1) return 9;
            if (((PyListObject *)lst)->items[0] != left) return 10;
            if (((PyListObject *)lst)->items[1] != right) return 11;

            py_decref(joined);
            py_decref(expected);
            py_decref(sep);
            py_decref(lst);
            printf("backend4-str-join-leaf-string-items-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-str-join-leaf-string-items-ok"


def test_backend4_public_telemetry_symbols_are_wired():
    header = (RUNTIME_DIR / "include" / "py_runtime.h").read_text(encoding="utf-8")
    c_src = (RUNTIME_DIR / "src" / "py_gc_backend.c").read_text(encoding="utf-8")
    py_src = (RUNTIME_DIR / "py" / "py_gc_backend.py").read_text(encoding="utf-8")
    relocation_payload = STRICT_RELOCATION_PAYLOAD.read_text(encoding="utf-8")
    relocation_selector = STRICT_RELOCATION_SELECTOR.read_text(encoding="utf-8")
    relocation_drain = STRICT_RELOCATION_DRAIN.read_text(encoding="utf-8")
    zpage_allocation = STRICT_ZPAGE_ALLOCATION.read_text(encoding="utf-8")
    zpage_mechanics = STRICT_ZPAGE_MECHANICS.read_text(encoding="utf-8")
    zpage_lifecycle = STRICT_ZPAGE_LIFECYCLE.read_text(encoding="utf-8")
    barrier_dispatcher = STRICT_BARRIER_DISPATCHER.read_text(encoding="utf-8")
    py_gc_state = (RUNTIME_DIR / "py" / "freestanding_gc_state.py").read_text(
        encoding="utf-8"
    )
    abi = (REPO_ROOT / "pcc" / "py_frontend" / "codegen" / "runtime_abi.py").read_text(
        encoding="utf-8"
    )

    assert "PCC_GC_COUNTER_RELOCATION_FRAGMENTATION_SCORE" in header
    assert "PCC_GC_COUNTER_GENZGC_STORE_BARRIERS" in header
    assert "PCC_GC_COUNTER_GENZGC_STORE_BUFFER_ENTRIES" in header
    assert "PCC_GC_COUNTER_GENZGC_YOUNG_PROMOTIONS" in header
    assert "PCC_GC_COUNTER_GENZGC_EVACUATION_CANDIDATES" in header
    assert "PCC_GC_COUNTER_GENZGC_EVACUATED_BYTES" in header
    assert "PCC_GC_COUNTER_GENZGC_PAGE_POLICY_SCORE" in header
    assert "PCC_GC_COUNTER_GENZGC_LARGE_OBJECT_DEFERS" in header
    assert "PCC_GC_COUNTER_GENZGC_LARGE_OBJECT_DEFERRED_BYTES" in header
    assert "PCC_GC_COUNTER_GENZGC_SMALL_PAGE_CANDIDATES" in header
    assert "PCC_GC_COUNTER_GENZGC_MEDIUM_PAGE_CANDIDATES" in header
    assert "PCC_GC_COUNTER_GENZGC_EVACUATION_CANDIDATE_BYTES" in header
    assert "PCC_GC_COUNTER_GENZGC_SMALL_PAGE_CANDIDATE_BYTES" in header
    assert "PCC_GC_COUNTER_GENZGC_MEDIUM_PAGE_CANDIDATE_BYTES" in header
    assert "PCC_GC_COUNTER_GENZGC_STORE_BUFFER_DRAIN_BATCHES" in header
    assert "PCC_GC_COUNTER_GENZGC_STORE_BUFFER_DRAINED_ENTRIES" in header
    assert "PCC_GC_COUNTER_GENZGC_STORE_BUFFER_DUPLICATE_SKIPS" in header
    assert "PCC_GC_COUNTER_GENZGC_STORE_BUFFER_HIGH_WATER" in header
    assert "PCC_GC_COUNTER_GENZGC_STORE_BUFFER_OWNER_FANOUT_HIGH_WATER" in header
    assert "PCC_GC_COUNTER_GENZGC_STORE_BUFFER_OWNER_COUNT_HIGH_WATER" in header
    assert "PCC_GC_COUNTER_GENZGC_STORE_BUFFER_INCOMPLETE_DRAINS" in header
    assert "PCC_GC_COUNTER_GENZGC_EVACUATION_INCOMPLETE_BATCHES" in header
    assert "PCC_GC_COUNTER_GENZGC_STORE_BUFFER_BATCH_CAPACITY" in header
    assert "PCC_GC_COUNTER_GENZGC_STORE_BUFFER_MAX_BATCH_SIZE" in header
    assert "PCC_GC_COUNTER_GENZGC_STORE_BUFFER_FULL_BATCHES" in header
    assert "PCC_GC_COUNTER_GENZGC_STORE_BUFFER_MEDIUM_CAPACITY" in header
    assert "PCC_GC_COUNTER_GENZGC_STORE_BUFFER_MEDIUM_PENDING" in header
    assert "PCC_GC_COUNTER_GENZGC_STORE_BUFFER_MEDIUM_FLUSHES" in header
    assert "PCC_GC_COUNTER_GENZGC_STORE_BUFFER_MEDIUM_FLUSHED_ENTRIES" in header
    assert "PCC_GC_COUNTER_GENZGC_STORE_BUFFER_MEDIUM_FULL_FLUSHES" in header
    assert "PCC_GC_COUNTER_GENZGC_STORE_BUFFER_CROSS_THREAD_MEDIUM_FLUSHES" in header
    assert (
        "PCC_GC_COUNTER_GENZGC_STORE_BUFFER_CROSS_THREAD_MEDIUM_FLUSHED_ENTRIES"
        in header
    )
    assert "PCC_GC_COUNTER_GENZGC_EVACUATION_EFFICIENCY_PER_MILLE" in header
    assert "PCC_GC_COUNTER_GENZGC_FRAGMENTATION_BACKLOG_BYTES" in header
    assert "PCC_GC_COUNTER_GENZGC_FRAGMENTATION_POLICY_SCORE" in header
    assert "PCC_GC_COUNTER_GENZGC_SMALL_PAGE_LIMIT_BYTES" in header
    assert "PCC_GC_COUNTER_GENZGC_MEDIUM_PAGE_LIMIT_BYTES" in header
    assert "PCC_GC_COUNTER_GENZGC_LARGE_DEFER_LIMIT_BYTES" in header
    assert "PCC_GC_COUNTER_GENZGC_LARGE_OBJECT_RECONSIDERATIONS" in header
    assert "PCC_GC_COUNTER_GENZGC_YOUNG_OBJECTS" in header
    assert "PCC_GC_COUNTER_GENZGC_OLD_OBJECTS" in header
    assert "PCC_GC_COUNTER_GENZGC_YOUNG_BYTES" in header
    assert "PCC_GC_COUNTER_GENZGC_OLD_BYTES" in header
    assert "PCC_GC_COUNTER_GENZGC_SMALL_PAGE_OBJECTS" in header
    assert "PCC_GC_COUNTER_GENZGC_MEDIUM_PAGE_OBJECTS" in header
    assert "PCC_GC_COUNTER_GENZGC_LARGE_PAGE_OBJECTS" in header
    assert "PCC_GC_COUNTER_GENZGC_SMALL_PAGE_BYTES" in header
    assert "PCC_GC_COUNTER_GENZGC_MEDIUM_PAGE_BYTES" in header
    assert "PCC_GC_COUNTER_GENZGC_LARGE_PAGE_BYTES" in header
    assert "PCC_GC_COUNTER_GENZGC_REMEMBERED_SET_ENTRIES" in header
    assert "PCC_GC_COUNTER_GENZGC_REMEMBERED_SET_DUPLICATE_SKIPS" in header
    assert "PCC_GC_COUNTER_GENZGC_REMEMBERED_SET_HIGH_WATER" in header
    assert "PCC_GC_COUNTER_GENZGC_REMEMBERED_PAGE_ENTRIES" in header
    assert "PCC_GC_COUNTER_GENZGC_REMEMBERED_PAGE_SLOT_ENTRIES" in header
    assert "PCC_GC_COUNTER_GENZGC_REMEMBERED_PAGE_HIGH_WATER" in header
    assert "pcc_gc_backend4_remembered_page_contains_slot" in header
    assert "pcc_gc_backend4_remembered_page_clear_slot" in header
    assert "pcc_gc_backend4_zpage_contains_remembered_card" in header
    assert "pcc_gc_backend4_zpage_clear_remembered_card" in header
    assert "PCC_GC_COUNTER_GENZGC_ZPAGE_COUNT" in header
    assert "PCC_GC_COUNTER_GENZGC_ZPAGE_CAPACITY_BYTES" in header
    assert "PCC_GC_COUNTER_GENZGC_ZPAGE_FRAGMENTATION_BYTES" in header
    assert "PCC_GC_COUNTER_GENZGC_ZPAGE_LARGE_PAGES" in header
    assert "PCC_GC_COUNTER_GENZGC_ZPAGE_USED_BYTES" in header
    assert "pcc_gc_backend4_zpage_allocated_bytes" in header
    assert "pcc_gc_backend4_zpage_reclaimable_gap_bytes" in header
    assert "pcc_gc_backend4_zpage_span_bytes" in header
    assert "PCC_GC_COUNTER_GENZGC_ZPAGE_FRAGMENTATION_PER_MILLE" in header
    assert "PCC_GC_COUNTER_GENZGC_ZPAGE_POLICY_SCORE" in header
    assert "PCC_GC_COUNTER_GENZGC_ZPAGE_REMEMBERED_SLOTS" in header
    assert "PCC_GC_COUNTER_GENZGC_ZPAGE_REMEMBERED_CARDS" in header
    assert "PCC_GC_COUNTER_GENZGC_ZPAGE_REMEMBERED_CARDS = 108" in header
    assert "PCC_GC_COUNTER_GENZGC_ZPAGE_REMEMBERED_CARD_RATIO_PER_MILLE" in header
    assert "PCC_GC_COUNTER_GENZGC_ZPAGE_REMEMBERED_CARD_RATIO_PER_MILLE = 109" in header
    assert "PCC_GC_COUNTER_GENZGC_ZPAGE_DIRTY_PAGES" in header
    assert "PCC_GC_COUNTER_GENZGC_ZPAGE_FRAGMENTED_PAGES" in header
    assert "PCC_GC_COUNTER_GENZGC_ZPAGE_YOUNG_PAGES" in header
    assert "PCC_GC_COUNTER_GENZGC_ZPAGE_OLD_PAGES" in header
    assert "PCC_GC_COUNTER_GENZGC_ZPAGE_FREE_PAGES" in header
    assert "PCC_GC_COUNTER_GENZGC_ZPAGE_FREE_PAGES = 110" in header
    assert "PCC_GC_COUNTER_GENZGC_ZPAGE_FREE_CAPACITY_BYTES" in header
    assert "PCC_GC_COUNTER_GENZGC_ZPAGE_FREE_CAPACITY_BYTES = 111" in header
    assert "pcc_gc_backend4_zpage_free_span_bytes" in header
    assert "PCC_GC_COUNTER_GENZGC_EVACUATION_CANDIDATE_ZPAGE_BYTES = 112" in header
    assert "PCC_GC_COUNTER_GENZGC_SMALL_PAGE_CANDIDATE_ZPAGE_BYTES = 113" in header
    assert "PCC_GC_COUNTER_GENZGC_MEDIUM_PAGE_CANDIDATE_ZPAGE_BYTES = 114" in header
    assert "PCC_GC_COUNTER_GENZGC_EVACUATION_PAGE_CANDIDATES = 115" in header
    assert "pcc_gc_backend4_evacuation_page_candidate_score" in header
    assert "pcc_gc_backend4_evacuation_page_candidate_bytes" in header
    assert "pcc_gc_backend4_evacuation_page_dirty_cards" in header
    assert "pcc_gc_backend4_evacuation_drain" in header
    assert "pcc_gc_backend4_evacuation_page_drain" in header
    assert "PCC_GC_COUNTER_GENZGC_PAGE_PRESSURE_SCORE" in header
    assert "pcc_gc_note_slot_write_barrier" in header
    assert "pcc_gc_backend4_verify_no_old_addresses" in header
    assert "pcc_gc_backend4_fragmentation_score" in c_src
    assert "pcc_gc_backend4_generation_barrier_score" in c_src
    assert "pcc_gc_backend4_store_buffer_entries" in c_src
    assert "pcc_gc_backend4_generation_promotion_score" in c_src
    assert "pcc_gc_backend4_evacuation_candidate_score" in c_src
    assert "pcc_gc_backend4_evacuated_bytes" in c_src
    assert "pcc_gc_backend4_page_policy_score" in c_src
    assert "pcc_gc_backend4_large_object_defer_score" in c_src
    assert "pcc_gc_backend4_large_object_deferred_bytes" in c_src
    assert "pcc_gc_backend4_small_page_candidate_score" in c_src
    assert "pcc_gc_backend4_medium_page_candidate_score" in c_src
    assert "pcc_gc_backend4_evacuation_candidate_bytes" in c_src
    assert "pcc_gc_backend4_small_page_candidate_bytes" in c_src
    assert "pcc_gc_backend4_medium_page_candidate_bytes" in c_src
    assert "pcc_gc_backend4_evacuation_candidate_zpage_bytes" in c_src
    assert "pcc_gc_backend4_small_page_candidate_zpage_bytes" in c_src
    assert "pcc_gc_backend4_medium_page_candidate_zpage_bytes" in c_src
    assert "pcc_gc_backend4_evacuation_page_candidate_score" in c_src
    assert "pcc_gc_backend4_evacuation_page_candidate_bytes" in c_src
    assert "pcc_gc_backend4_evacuation_page_dirty_cards" in c_src
    assert "pcc_gc_backend4_evacuation_drain" in c_src
    assert "pcc_gc_backend4_evacuation_page_drain" in c_src
    assert "pcc_gc_backend4_snapshot_selected_page_batch_unlocked" in c_src
    assert "pcc_gc_backend4_store_buffer_drain_batches" in c_src
    assert "pcc_gc_backend4_store_buffer_drained_entries" in c_src
    assert "pcc_gc_backend4_store_buffer_duplicate_skips" in c_src
    assert "pcc_gc_backend4_store_buffer_high_water" in c_src
    assert "pcc_gc_backend4_store_buffer_owner_fanout_high_water" in c_src
    assert "pcc_gc_backend4_store_buffer_owner_count_high_water" in c_src
    assert "pcc_gc_backend4_store_buffer_incomplete_drains" in c_src
    assert "pcc_gc_backend4_evacuation_incomplete_batches" in c_src
    assert "pcc_gc_backend4_store_buffer_batch_capacity" in c_src
    assert "pcc_gc_backend4_store_buffer_max_batch_size" in c_src
    assert "pcc_gc_backend4_store_buffer_full_batches" in c_src
    assert "pcc_gc_backend4_store_buffer_medium_capacity" in c_src
    assert "pcc_gc_backend4_store_buffer_medium_pending" in c_src
    assert "pcc_gc_backend4_store_buffer_medium_flushes" in c_src
    assert "pcc_gc_backend4_store_buffer_medium_flushed_entries" in c_src
    assert "pcc_gc_backend4_store_buffer_medium_full_flushes" in c_src
    assert "pcc_gc_backend4_store_buffer_cross_thread_medium_flushes" in c_src
    assert "pcc_gc_backend4_store_buffer_cross_thread_medium_flushed_entries" in c_src
    assert "pcc_gc_backend4_evacuation_efficiency_per_mille" in c_src
    assert "pcc_gc_backend4_fragmentation_backlog_bytes" in c_src
    assert "pcc_gc_backend4_fragmentation_policy_score" in c_src
    assert "pcc_gc_backend4_small_page_limit_bytes" in c_src
    assert "pcc_gc_backend4_medium_page_limit_bytes" in c_src
    assert "pcc_gc_backend4_large_defer_limit_bytes" in c_src
    assert "pcc_gc_backend4_large_object_reconsiderations" in c_src
    assert "pcc_gc_backend4_young_object_count" in c_src
    assert "pcc_gc_backend4_old_object_count" in c_src
    assert "pcc_gc_backend4_young_bytes" in c_src
    assert "pcc_gc_backend4_old_bytes" in c_src
    assert "pcc_gc_backend4_small_page_object_count" in c_src
    assert "pcc_gc_backend4_medium_page_object_count" in c_src
    assert "pcc_gc_backend4_large_page_object_count" in c_src
    assert "pcc_gc_backend4_small_page_live_bytes" in c_src
    assert "pcc_gc_backend4_medium_page_live_bytes" in c_src
    assert "pcc_gc_backend4_large_page_live_bytes" in c_src
    assert "pcc_gc_backend4_remembered_set_entries" in c_src
    assert "pcc_gc_backend4_remembered_set_duplicate_skips" in c_src
    assert "pcc_gc_backend4_remembered_set_high_water" in c_src
    assert "pcc_gc_backend4_remembered_page_entries" in c_src
    assert "pcc_gc_backend4_remembered_page_slot_entries" in c_src
    assert "pcc_gc_backend4_remembered_page_high_water" in c_src
    assert "pcc_gc_backend4_remembered_page_contains_slot" in c_src
    assert "pcc_gc_backend4_remembered_page_clear_slot" in c_src
    assert "pcc_gc_backend4_zpage_contains_remembered_card" in c_src
    assert "pcc_gc_backend4_zpage_clear_remembered_card" in c_src
    assert "pcc_gc_backend4_zpage_card_for_slot_unlocked" in c_src
    assert "PccGcZPageNode" in c_src
    assert "pcc_gc_backend4_zpage_track_alloc_unlocked" in c_src
    assert "pcc_gc_backend4_zpage_find_reusable_page_unlocked" in c_src
    assert "pcc_gc_backend4_evacuation_page_find_unlocked(page) != NULL" in c_src
    assert "pcc_gc_backend4_zpage_note_owner_promoted_unlocked" in c_src
    assert "pcc_gc_backend4_zpage_pop_free_page_unlocked" in c_src
    assert "PCC_GC_BACKEND4_FREE_SMALL_PAGE_LIMIT 8" in c_src
    assert "PCC_GC_BACKEND4_FREE_MEDIUM_PAGE_LIMIT 4" in c_src
    assert "pcc_gc_backend4_zpage_remove_unlocked" in c_src
    assert "typedef struct PccGcZPageEvacuationCandidate" in c_src
    assert "pcc_gc_backend4_zpage_candidate_snapshot" in c_src
    assert (
        "pcc_gc_backend4_selector_scan_cursor = pcc_gc_backend4_zpages;"
        in c_src
    )
    assert "pcc_gc_backend4_zpage_count" in c_src
    assert "pcc_gc_backend4_zpage_capacity_bytes" in c_src
    assert "pcc_gc_backend4_zpage_fragmentation_bytes" in c_src
    assert "pcc_gc_backend4_zpage_large_pages" in c_src
    assert "pcc_gc_backend4_zpage_used_bytes" in c_src
    assert "pcc_gc_backend4_zpage_allocated_bytes" in c_src
    assert "pcc_gc_backend4_zpage_reclaimable_gap_bytes" in c_src
    assert "pcc_gc_backend4_zpage_span_bytes" in c_src
    assert "pcc_gc_backend4_zpage_fragmentation_per_mille" in c_src
    assert "pcc_gc_backend4_zpage_policy_score" in c_src
    assert "typedef struct PccGcZPage" in c_src
    assert "allocated_bytes" in c_src
    assert "offset_bytes" in c_src
    assert "size_bytes" in c_src
    assert "span_base" in c_src
    assert "span_capacity_bytes" in c_src
    assert "PccGcZPage *page" in c_src
    assert "pcc_gc_backend4_pages" in c_src
    assert "pcc_gc_backend4_free_pages" in c_src
    assert "pcc_gc_backend4_zpage_remembered_slots" in c_src
    assert "pcc_gc_backend4_zpage_remembered_cards" in c_src
    assert "pcc_gc_backend4_zpage_remembered_card_ratio_per_mille" in c_src
    assert "pcc_gc_backend4_zpage_dirty_pages" in c_src
    assert "pcc_gc_backend4_zpage_fragmented_pages" in c_src
    assert "pcc_gc_backend4_zpage_young_pages" in c_src
    assert "pcc_gc_backend4_zpage_old_pages" in c_src
    assert "pcc_gc_backend4_zpage_free_pages" in c_src
    assert "pcc_gc_backend4_zpage_free_capacity_bytes" in c_src
    assert "pcc_gc_backend4_zpage_free_span_bytes" in c_src
    assert "PCC_GC_BACKEND4_ZPAGE_SPAN_CARD_BYTES" in header
    assert "PCC_GC_BACKEND4_ZPAGE_SPAN_CARD_BYTES" in c_src
    assert "pcc_gc_backend4_zpage_owner_offset_bytes" in header
    assert "pcc_gc_backend4_zpage_owner_size_bytes" in header
    assert "pcc_gc_backend4_zpage_owner_span_card" in header
    assert "pcc_gc_backend4_zpage_owner_slot_span_card" in header
    assert "pcc_gc_backend4_zpage_register_owner_payload_span" in header
    assert "pcc_gc_backend4_zpage_unregister_owner_payload_span" in header
    assert "pcc_gc_backend4_zpage_retarget_owner_payload_span" in header
    assert "pcc_gc_backend4_zpage_owner_offset_bytes" in c_src
    assert "pcc_gc_backend4_zpage_owner_size_bytes" in c_src
    assert "pcc_gc_backend4_zpage_owner_span_card" in c_src
    assert "pcc_gc_backend4_zpage_owner_slot_span_card" in c_src
    assert "pcc_gc_backend4_zpage_register_owner_payload_span" in c_src
    assert "pcc_gc_backend4_zpage_unregister_owner_payload_span" in c_src
    assert "pcc_gc_backend4_zpage_retarget_owner_payload_span" in c_src
    assert "PccGcZPagePayloadSpanNode" in c_src
    assert "pcc_gc_backend4_zpage_card_for_node_slot_unlocked" in c_src
    assert "pcc_gc_backend4_relocation_set_contains_page_unlocked" in c_src
    assert "PccGcZPageEvacuationNode" in c_src
    assert "pcc_gc_backend4_select_page_objects_batch_unlocked" in c_src
    assert "pcc_gc_backend4_best_relocation_page_batch_unlocked" in c_src
    assert "pcc_gc_backend4_evacuation_page_detach_unlocked" in c_src
    assert "pcc_gc_backend4_page_pressure_score" in c_src
    assert '@c_abi_export("pcc_gc_backend4_fragmentation_score")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_generation_barrier_score")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_store_buffer_entries")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_generation_promotion_score")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_evacuation_candidate_score")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_evacuated_bytes")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_page_policy_score")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_large_object_defer_score")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_large_object_deferred_bytes")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_small_page_candidate_score")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_medium_page_candidate_score")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_evacuation_candidate_bytes")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_small_page_candidate_bytes")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_medium_page_candidate_bytes")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_evacuation_candidate_zpage_bytes")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_small_page_candidate_zpage_bytes")' in py_src
    assert (
        '@c_abi_export("pcc_gc_backend4_medium_page_candidate_zpage_bytes")' in py_src
    )
    assert '@c_abi_export("pcc_gc_backend4_evacuation_page_candidate_score")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_evacuation_page_candidate_bytes")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_evacuation_page_dirty_cards")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_evacuation_drain")' in relocation_drain
    assert (
        '@c_abi_export("pcc_gc_backend4_evacuation_page_drain")'
        in relocation_drain
    )
    assert '@c_abi_export("pcc_gc_backend4_store_buffer_drain_batches")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_store_buffer_drained_entries")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_store_buffer_duplicate_skips")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_store_buffer_high_water")' in py_src
    assert (
        '@c_abi_export("pcc_gc_backend4_store_buffer_owner_fanout_high_water")'
        in py_src
    )
    assert (
        '@c_abi_export("pcc_gc_backend4_store_buffer_owner_count_high_water")' in py_src
    )
    assert '@c_abi_export("pcc_gc_backend4_store_buffer_incomplete_drains")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_evacuation_incomplete_batches")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_store_buffer_batch_capacity")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_store_buffer_max_batch_size")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_store_buffer_full_batches")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_store_buffer_medium_capacity")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_store_buffer_medium_pending")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_store_buffer_medium_flushes")' in py_src
    assert (
        '@c_abi_export("pcc_gc_backend4_store_buffer_medium_flushed_entries")' in py_src
    )
    assert '@c_abi_export("pcc_gc_backend4_store_buffer_medium_full_flushes")' in py_src
    assert (
        '@c_abi_export("pcc_gc_backend4_store_buffer_cross_thread_medium_flushes")'
        in py_src
    )
    assert (
        '@c_abi_export("pcc_gc_backend4_store_buffer_cross_thread_medium_flushed_entries")'
        in py_src
    )
    assert '@c_abi_export("pcc_gc_backend4_evacuation_efficiency_per_mille")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_fragmentation_backlog_bytes")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_fragmentation_policy_score")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_small_page_limit_bytes")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_medium_page_limit_bytes")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_large_defer_limit_bytes")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_large_object_reconsiderations")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_young_object_count")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_old_object_count")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_young_bytes")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_old_bytes")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_small_page_object_count")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_medium_page_object_count")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_large_page_object_count")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_small_page_live_bytes")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_medium_page_live_bytes")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_large_page_live_bytes")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_remembered_set_entries")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_remembered_set_duplicate_skips")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_remembered_set_high_water")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_remembered_page_entries")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_remembered_page_slot_entries")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_remembered_page_high_water")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_remembered_page_contains_slot")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_remembered_page_clear_slot")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_zpage_contains_remembered_card")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_zpage_clear_remembered_card")' in py_src
    assert "_backend4_owner_remembered_slots" in py_src
    assert "pcc_gc_backend4_zpage_track_alloc" in zpage_allocation
    assert "pcc_gc_backend4_try_zpage_alloc" in zpage_allocation
    assert "pcc_gc_backend4_zpage_find_page_for_addr" in zpage_mechanics
    assert "pcc_gc_backend4_try_zpage_alloc" in c_src
    assert "PY_FLAG_GC_ZPAGE_ALLOC" in c_src
    assert "pcc_gc_backend4_zpage_find_reusable_page" in zpage_mechanics
    assert "load_i32(page, 108) == 0" in relocation_selector
    assert "_backend4_zpage_note_owner_promoted" in py_src
    assert "pcc_gc_backend4_zpage_pop_free_page" in zpage_mechanics
    assert "_backend4_relocation_set_contains_page" in py_src
    assert "_backend4_evacuation_page_add" in py_src
    assert "_backend4_evacuation_page_remove" in py_src
    assert "_backend4_zpage_page_for_owner" in py_src
    assert "_backend4_select_page_objects" in relocation_selector
    assert "pcc_gc_backend4_select_relocation_pages" in relocation_selector
    assert "_backend4_zpage_candidate_score" in relocation_selector
    assert "pcc_gc_backend4_free_page_limit_for_class" in zpage_lifecycle
    assert "pcc_gc_backend4_zpage_payload_span_head" in py_src
    assert "pcc_gc_backend4_zpage_remove_payload_spans" in zpage_lifecycle
    assert "pcc_gc_backend4_zpage_remove" in zpage_lifecycle
    assert "_backend4_remembered_set_retarget_slot" in py_src
    assert "_backend4_remembered_set_retarget_inline_slot" in py_src
    assert "def _relocate_copy_slots(from_obj, to_obj, ctx)" in relocation_payload
    assert "pcc_gc_visit_object_slots(from_obj, _relocate_from_slot" in (
        relocation_payload
    )
    assert "pcc_gc_visit_object_slots(to_obj, _relocate_to_slot" in (
        relocation_payload
    )
    assert "from_obj, to_obj, from_slot, to_slot" in relocation_payload
    assert (
        'global_store_ptr("pcc_gc_backend4_selector_scan_cursor", _zpage_head())'
        in relocation_selector
    )
    assert 'define_global_ptr_null("pcc_gc_backend4_zpage_head")' in py_gc_state
    assert '@c_abi_export("pcc_gc_backend4_zpage_count")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_zpage_capacity_bytes")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_zpage_fragmentation_bytes")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_zpage_large_pages")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_zpage_used_bytes")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_zpage_allocated_bytes")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_zpage_reclaimable_gap_bytes")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_zpage_span_bytes")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_zpage_owner_offset_bytes")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_zpage_owner_size_bytes")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_zpage_owner_span_card")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_zpage_owner_slot_span_card")' in py_src
    assert (
        '@c_abi_export("pcc_gc_backend4_zpage_register_owner_payload_span")' in py_src
    )
    assert (
        '@c_abi_export("pcc_gc_backend4_zpage_unregister_owner_payload_span")' in py_src
    )
    assert (
        '@c_abi_export("pcc_gc_backend4_zpage_retarget_owner_payload_span")' in py_src
    )
    assert "ptr_diff" in py_src
    assert "_backend4_zpage_payload_offset_for_slot" in py_src
    assert "inline_delta: int = ptr_diff(slot, owner)" in py_src
    assert "delta: int = ptr_diff(slot, base)" in py_src
    assert "span = malloc(48)" in py_src
    assert "return allocated" in py_src
    assert "return (offset // 512) % 64" in py_src
    assert '@c_abi_export("pcc_gc_backend4_zpage_fragmentation_per_mille")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_zpage_policy_score")' in py_src
    assert "pcc_gc_backend4_page_head" in py_src
    assert "pcc_gc_backend4_free_page_head" in py_src
    assert '@c_abi_export("pcc_gc_backend4_zpage_remembered_slots")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_zpage_remembered_cards")' in py_src
    assert (
        '@c_abi_export("pcc_gc_backend4_zpage_remembered_card_ratio_per_mille")'
        in py_src
    )
    assert '@c_abi_export("pcc_gc_backend4_zpage_dirty_pages")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_zpage_fragmented_pages")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_zpage_young_pages")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_zpage_old_pages")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_zpage_free_pages")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_zpage_free_capacity_bytes")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_zpage_free_span_bytes")' in py_src
    assert '@c_abi_export("pcc_gc_backend4_page_pressure_score")' in py_src
    assert '@c_abi_export("pcc_gc_note_slot_write_barrier")' in barrier_dispatcher
    assert '"pcc_gc_backend4_fragmentation_score": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_generation_barrier_score": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_store_buffer_entries": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_generation_promotion_score": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_evacuation_candidate_score": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_evacuated_bytes": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_page_policy_score": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_large_object_defer_score": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_large_object_deferred_bytes": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_small_page_candidate_score": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_medium_page_candidate_score": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_evacuation_candidate_bytes": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_small_page_candidate_bytes": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_medium_page_candidate_bytes": (_I64, [], False)' in abi
    assert (
        '"pcc_gc_backend4_evacuation_candidate_zpage_bytes": (_I64, [], False)' in abi
    )
    assert (
        '"pcc_gc_backend4_small_page_candidate_zpage_bytes": (_I64, [], False)' in abi
    )
    assert (
        '"pcc_gc_backend4_medium_page_candidate_zpage_bytes": (_I64, [], False)' in abi
    )
    assert '"pcc_gc_backend4_evacuation_page_candidate_score": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_evacuation_page_candidate_bytes": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_evacuation_page_dirty_cards": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_evacuation_drain": (_I64, [_I64], False)' in abi
    assert '"pcc_gc_backend4_evacuation_page_drain": (_I64, [_I64], False)' in abi
    assert '"pcc_gc_backend4_store_buffer_drain_batches": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_store_buffer_drained_entries": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_store_buffer_duplicate_skips": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_store_buffer_high_water": (_I64, [], False)' in abi
    assert (
        '"pcc_gc_backend4_store_buffer_owner_fanout_high_water": (_I64, [], False)'
        in abi
    )
    assert (
        '"pcc_gc_backend4_store_buffer_owner_count_high_water": (_I64, [], False)'
        in abi
    )
    assert '"pcc_gc_backend4_store_buffer_incomplete_drains": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_evacuation_incomplete_batches": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_store_buffer_batch_capacity": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_store_buffer_max_batch_size": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_store_buffer_full_batches": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_store_buffer_medium_capacity": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_store_buffer_medium_pending": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_store_buffer_medium_flushes": (_I64, [], False)' in abi
    assert (
        '"pcc_gc_backend4_store_buffer_medium_flushed_entries": (_I64, [], False)'
        in abi
    )
    assert (
        '"pcc_gc_backend4_store_buffer_medium_full_flushes": (_I64, [], False)' in abi
    )
    assert (
        '"pcc_gc_backend4_store_buffer_cross_thread_medium_flushes": (_I64, [], False)'
        in abi
    )
    assert '"pcc_gc_backend4_store_buffer_cross_thread_medium_flushed_entries":' in abi
    assert '"pcc_gc_backend4_evacuation_efficiency_per_mille": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_fragmentation_backlog_bytes": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_fragmentation_policy_score": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_small_page_limit_bytes": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_medium_page_limit_bytes": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_large_defer_limit_bytes": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_large_object_reconsiderations": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_young_object_count": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_old_object_count": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_young_bytes": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_old_bytes": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_small_page_object_count": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_medium_page_object_count": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_large_page_object_count": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_small_page_live_bytes": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_medium_page_live_bytes": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_large_page_live_bytes": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_remembered_set_entries": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_remembered_set_duplicate_skips": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_remembered_set_high_water": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_remembered_page_entries": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_remembered_page_slot_entries": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_remembered_page_high_water": (_I64, [], False)' in abi
    assert (
        '"pcc_gc_backend4_remembered_page_contains_slot": (_I64, [_PTR], False)' in abi
    )
    assert '"pcc_gc_backend4_remembered_page_clear_slot": (_I64, [_PTR], False)' in abi
    assert (
        '"pcc_gc_backend4_zpage_contains_remembered_card": (_I64, [_PTR, _PTR], False)'
        in abi
    )
    assert (
        '"pcc_gc_backend4_zpage_clear_remembered_card": (_I64, [_PTR, _PTR], False)'
        in abi
    )
    assert '"pcc_gc_backend4_zpage_count": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_zpage_capacity_bytes": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_zpage_fragmentation_bytes": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_zpage_large_pages": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_zpage_used_bytes": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_zpage_allocated_bytes": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_zpage_reclaimable_gap_bytes": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_zpage_span_bytes": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_zpage_owner_offset_bytes": (_I64, [_PYOBJ], False)' in abi
    assert '"pcc_gc_backend4_zpage_owner_size_bytes": (_I64, [_PYOBJ], False)' in abi
    assert '"pcc_gc_backend4_zpage_owner_span_card": (_I64, [_PYOBJ], False)' in abi
    assert (
        '"pcc_gc_backend4_zpage_owner_slot_span_card": (_I64, [_PYOBJ, _PTR], False)'
        in abi
    )
    assert '"pcc_gc_backend4_zpage_register_owner_payload_span":' in abi
    assert '"pcc_gc_backend4_zpage_unregister_owner_payload_span":' in abi
    assert '"pcc_gc_backend4_zpage_retarget_owner_payload_span":' in abi
    assert '"pcc_gc_backend4_zpage_fragmentation_per_mille": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_zpage_policy_score": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_zpage_remembered_slots": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_zpage_remembered_cards": (_I64, [], False)' in abi
    assert (
        '"pcc_gc_backend4_zpage_remembered_card_ratio_per_mille": (_I64, [], False)'
        in abi
    )
    assert '"pcc_gc_backend4_zpage_dirty_pages": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_zpage_fragmented_pages": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_zpage_young_pages": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_zpage_old_pages": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_zpage_free_pages": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_zpage_free_capacity_bytes": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_zpage_free_span_bytes": (_I64, [], False)' in abi
    assert '"pcc_gc_backend4_page_pressure_score": (_I64, [], False)' in abi
    assert (
        '"pcc_gc_note_slot_write_barrier": (_VOID, [_PYOBJ, _PTR, _PYOBJ], False)'
        in abi
    )
    assert 'define_global_ptr_null("pcc_gc_backend4_free_page_head")' in py_gc_state
    assert (
        'define_global_ptr_null("pcc_gc_backend4_retained_page_head")' in py_gc_state
    )
    assert (
        'define_global_ptr_null("pcc_gc_backend4_zpage_payload_span_head")'
        in py_gc_state
    )
    assert (
        'define_global_ptr_null("pcc_gc_backend4_evacuation_page_head")' in py_gc_state
    )
    assert (
        'define_global_i32("pcc_gc_backend4_evacuation_candidate_zpage_bytes_count", 0)'
        in py_gc_state
    )


def test_backend4_class_method_metadata_is_not_treated_as_gc_slots() -> None:
    c_class = (RUNTIME_DIR / "src" / "py_class.c").read_text(encoding="utf-8")
    py_class = (RUNTIME_DIR / "py" / "py_class.py").read_text(encoding="utf-8")
    c_gc = (RUNTIME_DIR / "src" / "py_gc_backend.c").read_text(encoding="utf-8")
    py_gc = (RUNTIME_DIR / "py" / "py_gc_backend.py").read_text(encoding="utf-8")
    strict_slots = STRICT_OBJECT_SLOTS.read_text(encoding="utf-8")
    strict_mark = STRICT_COMMON_MARK_CYCLE.read_text(encoding="utf-8")
    strict_promotion = STRICT_GENERATIONAL_PROMOTION.read_text(encoding="utf-8")
    strict_remap = STRICT_RELOCATION_REMAP.read_text(encoding="utf-8")
    relocation_payload = STRICT_RELOCATION_PAYLOAD.read_text(encoding="utf-8")

    assert (
        "PyObject *func = pcc_gc_note_relocation_read(m->methods[j].func);" in c_class
    )
    assert "m->methods[j].func = func;" in c_class
    assert "&m->methods[j].func" not in c_class
    assert "func = pcc_gc_note_relocation_read(load_ptr(method_slot, 0))" in py_class
    assert "store_ptr(method_slot, 0, func)" in py_class
    assert "pcc_gc_load_ptr(m, ptr_add(methods, m_off + 8))" not in py_class

    assert "pcc_gc_relocate_copy_slots(" in c_gc
    assert "py_obj_update_slot(from_slot);" in c_gc
    assert "from, to, from_slot, to_slot" in c_gc
    helper_start = c_gc.index("static int pcc_gc_visit_class_slots(")
    helper_end = c_gc.index(
        "typedef struct {\n    PyObjSlotVisitor visit;",
        helper_start,
    )
    helper_body = c_gc[helper_start:helper_end]
    assert "visit_borrowed_update_only(&cls->methods[i].func" in helper_body
    assert "visit_borrowed_update_only(&cls->del_method" in helper_body
    visit_start = c_gc.index("int py_obj_visit_slots(", helper_end)
    visit_end = c_gc.index(
        "typedef struct {\n    void (*visit)(PyObject *child);",
        visit_start,
    )
    visit_body = c_gc[visit_start:visit_end]
    assert "pcc_gc_visit_class_slots(" in visit_body
    assert "py_obj_visit_borrowed_update_only_slot" in visit_body
    trace_adapter_start = c_gc.index("static void pcc_gc_trace_owner_slot(")
    update_adapter_start = c_gc.index("static void pcc_gc_update_owner_slot(")
    trace_adapter_body = c_gc[trace_adapter_start:update_adapter_start]
    assert "role == PY_OBJ_SLOT_BORROWED_UPDATE_ONLY" in trace_adapter_body
    promote_adapter_start = c_gc.index("static void pcc_gc_promote_owner_slot(")
    promote_start = c_gc.index(
        "static void pcc_gc_promote_owner_referents(",
        promote_adapter_start,
    )
    promote_adapter_body = c_gc[promote_adapter_start:promote_start]
    assert "role == PY_OBJ_SLOT_OWNED" in promote_adapter_body
    assert "pcc_gc_promote_young_slot_with_mode" in promote_adapter_body
    assert "pcc_gc_promote_young_borrowed_slot_with_mode" in promote_adapter_body
    trace_start = c_gc.index("static void pcc_gc_trace_referents(", promote_start)
    promote_body = c_gc[promote_start:trace_start]
    trace_body = c_gc[
        trace_start : c_gc.index("/* Slot-ADDRESS flavored sibling", trace_start)
    ]
    assert "py_obj_visit_slots(" in promote_body
    assert "pcc_gc_promote_owner_slot" in promote_body
    assert "py_obj_visit_slots(" in trace_body
    assert "pcc_gc_trace_owner_slot" in trace_body
    assert "visit(cls->methods[i].func)" not in c_gc
    assert "visit(cls->del_method)" not in c_gc

    assert "def _relocate_copy_slots(from_obj, to_obj, ctx)" in relocation_payload
    assert "pcc_gc_backend4_remap_heal_slot(from_slot, 0)" in relocation_payload
    assert "from_obj, to_obj, from_slot, to_slot" in relocation_payload
    assert "_PY_OBJ_SLOT_OWNED = 1" in py_gc
    assert "_PY_OBJ_SLOT_BORROWED_TRACED = 2" in py_gc
    assert "_PY_OBJ_SLOT_BORROWED_UPDATE_ONLY = 3" in py_gc
    assert "_PY_OBJ_VISIT_TRACE = 1" in py_gc
    assert "_PY_OBJ_VISIT_PROMOTE = 2" in py_gc
    assert "_PY_OBJ_VISIT_UPDATE = 3" in py_gc
    helper_start = strict_slots.index("def _visit_class_slots(")
    helper_end = strict_slots.index("def _visit_instance_slots(", helper_start)
    helper_body = strict_slots[helper_start:helper_end]
    assert "_visit_slot(" in helper_body
    # The visitor was migrated from literal offsets to named ABI constants.
    # Assert the current spelling AND pin the numbers those constants must
    # still have, so a layout change trips this test the way the old literal
    # assertions did.
    assert PYCLASSOBJECT_METHODS_OFFSET == 64
    assert PYCLASSMETHOD_SIZE == 16
    assert PYCLASSMETHOD_FUNC_OFFSET == 8
    assert PYCLASSOBJECT_DEL_METHOD_OFFSET == 96
    assert 'load_ptr(o, abi_constant("object.class.methods_offset"))' in helper_body
    assert 'index * abi_constant("object.class_method.size")' in helper_body
    assert '+ abi_constant("object.class_method.func_offset")' in helper_body
    # Method slots and __del__ must be visited in UPDATE mode (3) so a moving
    # collector rewrites them; that is what the literal assertions guarded.
    assert helper_body.count("\n                3,\n") >= 1
    assert 'abi_constant("object.class.del_method_offset")' in helper_body
    slot_adapter_start = strict_promotion.index(
        "def pcc_gc_generational_promote_slot("
    )
    slot_adapter_end = strict_promotion.index(
        '@c_abi_export("pcc_gc_generational_promote_shallow_slot")',
        slot_adapter_start,
    )
    slot_adapter_body = strict_promotion[slot_adapter_start:slot_adapter_end]
    assert "if role == 1:" in slot_adapter_body
    assert (
        "pcc_gc_generational_promote_owned_slot_mode(slot, 0, 1)"
        in slot_adapter_body
    )
    assert "pcc_gc_generational_promote_borrowed_slot_mode(" in slot_adapter_body
    managed_slot_adapter = py_gc.split("def _py_obj_visit_slot(", 1)[1].split(
        "def _py_obj_visit_update_slot", 1
    )[0]
    assert "role != 3:  # _PY_OBJ_SLOT_BORROWED_UPDATE_ONLY" in managed_slot_adapter
    trace_py = strict_mark.split("def pcc_gc_trace_referents(obj)", 1)[1].split(
        "\n@c_abi_export", 1
    )[0]
    promote_py = strict_promotion.split(
        "def pcc_gc_trace_referents_for_promotion_mode", 1
    )[1].split(
        '@c_abi_export("pcc_gc_trace_referents_for_promotion")', 1
    )[0]
    remap_py = strict_remap.split(
        "def pcc_gc_backend4_remap_referents(obj)", 1
    )[1]
    covered_body = py_gc.split("def _py_obj_visit_covered_slots(", 1)[1].split(
        "def _subtract_known_child_ref(",
        1,
    )[0]
    assert "pcc_gc_visit_object_slots(" in covered_body
    assert (
        "pcc_gc_visit_object_slots(obj, pcc_gc_trace_slot, null())" in trace_py
    )
    assert (
        "pcc_gc_visit_object_slots(obj, pcc_gc_generational_promote_slot, null())"
        in promote_py
    )
    assert (
        "pcc_gc_visit_object_slots(obj, pcc_gc_backend4_remap_slot, null())"
        in remap_py
    )
    assert "_mark_gray_if_known(load_ptr(methods, k * 16 + 8))" not in py_gc
    assert "_mark_gray_if_known(load_ptr(o, 96))" not in py_gc


def test_backend4_list_extend_old_to_young_uses_store_barrier(tmp_path):
    """py_list_extend must route each grown-slot store through the collector
    write barrier (NULL-init + pcc_gc_store_ptr) so an OLD list that gains
    YOUNG elements records old->young edges in the genZGC store buffer.

    Load-bearing: extend is the ONLY store of these elements, so its barrier
    is the sole source of the store-buffer / REMEMBERED bookkeeping asserted
    below. If extend stored the elements raw (the pre-fix idiom, no barrier),
    the store-buffer count, the GENZGC_STORE_BARRIERS counter, and the
    owner's REMEMBERED flag would all be zero and this test would fail; a
    minor cycle could then drop the young elements reachable only via the
    OLD list. Elements are young *lists* (non-leaf, hence graph-tracked and
    barrier-eligible; leaf str/int are not tracked) carrying a unique
    tagged-int payload for content verification.
    """
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_internal.h"
        #include <stdio.h>

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            PyObject *dst_root = 0;
            pcc_gc_scheduler_root_register(&dst_root);

            /* Destination list, forced OLD so the extend below creates
             * old->young edges that MUST route through the store barrier. */
            PyObject *dst = py_list_new(0);
            if (dst == 0) return 3;
            pcc_gc_store_root(&dst_root, dst);
            {
                PyObjectHeader *dh = (PyObjectHeader *)dst;
                dh->flags =
                    (dh->flags & ~(PY_FLAG_GC_YOUNG | PY_FLAG_GC_MINOR_ARENA))
                    | PY_FLAG_GC_OLD;
            }

            /* Young source holding N tracked (non-leaf) young elements. */
            enum { N = 6 };
            PyObject *src = py_list_new(0);
            if (src == 0) return 4;
            for (int i = 0; i < N; i++) {
                PyObject *elem = py_list_new(1);
                if (elem == 0) return 5;
                py_list_append(elem, py_int_from_i64(1000 + i));
                py_list_append(src, elem);
                pcc_gc_release(elem);
            }

            /* Baseline so the counts below are exactly what extend produces. */
            pcc_gc_telemetry_reset();
            int64_t entries_before = pcc_gc_backend4_store_buffer_entries();

            py_list_extend(dst, src);

            /* Barrier-attributable assertions (all zero without the barrier). */
            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_STORE_BARRIERS) != N)
                return 10;
            if (pcc_gc_backend4_store_buffer_entries() - entries_before != N)
                return 11;
            if ((((PyObjectHeader *)dst)->flags & PY_FLAG_GC_REMEMBERED) == 0)
                return 12;

            /* Correctness: every element copied at the right index. */
            if (py_list_len(dst) != N) return 13;
            for (int i = 0; i < N; i++) {
                PyObject *got = py_list_get(dst, i);
                if (got == 0) return 14;
                PyObject *inner = py_list_get(got, 0);
                int of = 0;
                int64_t v = py_int_to_i64(inner, &of);
                pcc_gc_release(inner);
                pcc_gc_release(got);
                if (of || v != 1000 + i) return 15;
            }

            /* Drop the young source: the extended elements are now reachable
             * only through the OLD list, so their survival across the minor
             * cycle depends on the remembered old->young edges. */
            pcc_gc_release(src);
            for (int r = 0;
                 r < 8 && pcc_gc_backend4_store_buffer_entries() > 0;
                 r++) {
                (void)pcc_gc_step(64);
            }
            dst = pcc_gc_load_ptr(0, &dst_root);
            if (dst == 0) return 16;

            if (py_list_len(dst) != N) return 17;
            for (int i = 0; i < N; i++) {
                PyObject *got = py_list_get(dst, i);
                if (got == 0) return 18;
                PyObject *inner = py_list_get(got, 0);
                int of = 0;
                int64_t v = py_int_to_i64(inner, &of);
                pcc_gc_release(inner);
                pcc_gc_release(got);
                if (of || v != 1000 + i) return 19;
            }

            pcc_gc_store_root(&dst_root, 0);
            pcc_gc_scheduler_root_unregister(&dst_root);
            printf("backend4-list-extend-barrier-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-list-extend-barrier-ok"


def test_backend4_set_rehash_old_to_young_uses_store_barrier(tmp_path):
    """py_set_rehash moves live keys into a freshly allocated entries array
    and must route each moved key through pcc_gc_note_slot_write_barrier so
    the genZGC store buffer tracks the NEW slot (the add-time entry points
    into the old array, which rehash frees).

    Load-bearing / isolation: after promoting the first 5 keys to the OLD
    generation and clearing telemetry, exactly ONE young key is added. It
    crosses the 2/3 load factor and triggers a rehash that moves all six keys.
    The insert creates one pending edge into the old table.  The rehash
    transaction must retarget that exact edge to the new slot before freeing
    the old table; its move barrier then sees the retargeted edge as a
    duplicate, so the enqueue counter remains ONE.  The test checks old-slot
    absence and exact new-slot presence directly rather than treating a second
    enqueue as a proxy.  Keys are young *tuples* (hashable AND
    non-leaf/tracked) carrying a unique tagged-int payload.
    """
    proc = _compile_and_run(
        tmp_path,
        """
        #include "py_internal.h"
        #include <stdio.h>

        int main(void) {
            if (pcc_gc_set_backend(PCC_GC_KIND_COLORED_RELOCATING) != 0) return 2;

            PyObject *set_root = 0;
            pcc_gc_scheduler_root_register(&set_root);

            PyObject *set = py_set_new();
            if (set == 0) return 3;
            pcc_gc_store_root(&set_root, set);
            {
                PyObjectHeader *sh = (PyObjectHeader *)set;
                sh->flags =
                    (sh->flags & ~(PY_FLAG_GC_YOUNG | PY_FLAG_GC_MINOR_ARENA))
                    | PY_FLAG_GC_OLD;
            }

            /* Phase 1: seed 5 young tuple keys. Initial capacity is 8 and the
             * rehash trips only when fill > (cap*2)/3 == 5, so five keys do
             * NOT rehash. Each add records an old(set)->young(key) edge. */
            for (int i = 0; i < 5; i++) {
                PyObject *k = py_tuple_new(1);
                if (k == 0) return 4;
                py_tuple_set_item(k, 0, py_int_from_i64(2000 + i));
                py_set_add(set, k);
                pcc_gc_release(k);
            }
            if (py_set_len(set) != 5) return 5;

            /* Drain the store buffer so those 5 keys are promoted to OLD and
             * the set's REMEMBERED flag clears; a later rehash move of them
             * will then be old->old and fire no barrier. */
            for (int r = 0;
                 r < 16 && pcc_gc_backend4_store_buffer_entries() > 0;
                 r++) {
                (void)pcc_gc_step(64);
            }
            set = pcc_gc_load_ptr(0, &set_root);
            if (set == 0) return 6;
            if (pcc_gc_backend4_store_buffer_entries() != 0) return 7;
            if ((((PyObjectHeader *)set)->flags & PY_FLAG_GC_REMEMBERED) != 0)
                return 8;
            if (((PySetObject *)set)->capacity != 8) return 9;

            /* Phase 2: one more young key -> fill 6 > 5 -> py_set_rehash. */
            pcc_gc_telemetry_reset();
            SetEntry *old_entries = ((PySetObject *)set)->entries;
            PyObject **old_slots[8];
            for (int i = 0; i < 8; i++) {
                old_slots[i] = &old_entries[i].key;
            }
            PyObject *k5 = py_tuple_new(1);
            if (k5 == 0) return 10;
            py_tuple_set_item(k5, 0, py_int_from_i64(2005));
            py_set_add(set, k5);

            if (pcc_gc_telemetry(PCC_GC_COUNTER_GENZGC_STORE_BARRIERS) != 1)
                return 11;
            if ((((PyObjectHeader *)set)->flags & PY_FLAG_GC_REMEMBERED) == 0)
                return 12;
            if (((PySetObject *)set)->capacity <= 8) return 13;
            if (((PySetObject *)set)->entries == old_entries) return 21;
            for (int i = 0; i < 8; i++) {
                if (pcc_gc_backend4_remembered_page_contains_slot(old_slots[i]))
                    return 22;
            }
            PyObject **new_k5_slot = 0;
            PySetObject *grown = (PySetObject *)set;
            for (int64_t i = 0; i < grown->capacity; i++) {
                if (grown->entries[i].key == k5) {
                    new_k5_slot = &grown->entries[i].key;
                    break;
                }
            }
            if (new_k5_slot == 0) return 23;
            if (!pcc_gc_backend4_remembered_page_contains_slot(new_k5_slot))
                return 24;
            if (pcc_gc_backend4_store_buffer_entries() != 1) return 25;
            pcc_gc_release(k5);

            /* No key lost across the rehash: membership + length. */
            if (py_set_len(set) != 6) return 14;
            for (int i = 0; i < 6; i++) {
                PyObject *probe = py_tuple_new(1);
                if (probe == 0) return 15;
                py_tuple_set_item(probe, 0, py_int_from_i64(2000 + i));
                int64_t found = py_set_contains(set, probe);
                pcc_gc_release(probe);
                if (found != 1) return 16;
            }

            /* Drain again; the remembered new-array slot lets the collector
             * promote the moved young key. Re-check membership afterwards. */
            for (int r = 0;
                 r < 16 && pcc_gc_backend4_store_buffer_entries() > 0;
                 r++) {
                (void)pcc_gc_step(64);
            }
            set = pcc_gc_load_ptr(0, &set_root);
            if (set == 0) return 17;
            if (py_set_len(set) != 6) return 18;
            for (int i = 0; i < 6; i++) {
                PyObject *probe = py_tuple_new(1);
                if (probe == 0) return 19;
                py_tuple_set_item(probe, 0, py_int_from_i64(2000 + i));
                int64_t found = py_set_contains(set, probe);
                pcc_gc_release(probe);
                if (found != 1) return 20;
            }

            pcc_gc_store_root(&set_root, 0);
            pcc_gc_scheduler_root_unregister(&set_root);
            printf("backend4-set-rehash-barrier-ok\\n");
            return 0;
        }
        """,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.strip() == "backend4-set-rehash-barrier-ok"
