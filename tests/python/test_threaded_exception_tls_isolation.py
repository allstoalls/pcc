from __future__ import annotations

import os
from pathlib import Path
import subprocess
import textwrap

from tests.runtime_build_cache import (
    cached_threaded_pcc_python_runtime,
)


REPO_ROOT = Path(__file__).absolute().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"


def _harness_source(*, expect_registered_tls_roots: bool) -> str:
    expect_roots = 1 if expect_registered_tls_roots else 0
    return textwrap.dedent(
        f"""
        #include "py_runtime.h"
        #include "py_internal.h"
        #include <pthread.h>
        #include <sched.h>
        #include <stdatomic.h>
        #include <stdint.h>
        #include <stdio.h>

        enum {{ EXPECT_REGISTERED_TLS_ROOTS = {expect_roots} }};

        typedef struct {{
            int id;
            PyObject *first_seen;
            PyObject *second_seen;
        }} Worker;

        static atomic_int ready = 0;
        static atomic_int begin_mutation = 0;
        static atomic_int first_cleared = 0;
        static atomic_int second_replaced = 0;
        static atomic_int replacement_cleared = 0;
        static atomic_int failure = 0;

        static void note_failure(int code) {{
            int expected = 0;
            (void)atomic_compare_exchange_strong(&failure, &expected, code);
        }}

        static void wait_for(atomic_int *state, int value) {{
            while (atomic_load_explicit(state, memory_order_acquire) < value) {{
                sched_yield();
            }}
        }}

        static PyObject *raise_new(const char *message) {{
            PyObject *exc = py_exc_new(PY_EXC_RUNTIMEERROR, message);
            if (exc == NULL) return NULL;
            py_raise(exc);
            py_decref(exc);
            return py_current_exception();
        }}

        static void *worker_main(void *opaque) {{
            Worker *worker = (Worker *)opaque;
            if (py_current_exception() != NULL) note_failure(10 + worker->id);

            worker->first_seen = raise_new(
                worker->id == 0 ? "thread-zero-first" : "thread-one-first"
            );
            if (worker->first_seen == NULL) note_failure(20 + worker->id);
            atomic_fetch_add_explicit(&ready, 1, memory_order_release);
            wait_for(&ready, 2);

            if (py_current_exception() != worker->first_seen) {{
                note_failure(30 + worker->id);
            }}
            wait_for(&begin_mutation, 1);

            if (worker->id == 0) {{
                py_clear_exception();
                if (py_current_exception() != NULL) note_failure(40);
                atomic_store_explicit(&first_cleared, 1, memory_order_release);

                wait_for(&second_replaced, 1);
                if (py_current_exception() != NULL) note_failure(41);
                worker->second_seen = raise_new("thread-zero-replacement");
                if (worker->second_seen == NULL) note_failure(42);
                if (py_current_exception() != worker->second_seen) note_failure(43);
                py_clear_exception();
                if (py_current_exception() != NULL) note_failure(44);
                atomic_store_explicit(
                    &replacement_cleared, 1, memory_order_release
                );
            }} else {{
                wait_for(&first_cleared, 1);
                if (py_current_exception() != worker->first_seen) note_failure(50);
                worker->second_seen = raise_new("thread-one-replacement");
                if (worker->second_seen == NULL) note_failure(51);
                if (py_current_exception() != worker->second_seen) note_failure(52);
                atomic_store_explicit(&second_replaced, 1, memory_order_release);

                wait_for(&replacement_cleared, 1);
                if (py_current_exception() != worker->second_seen) note_failure(53);
                py_clear_exception();
                if (py_current_exception() != NULL) note_failure(54);
            }}

            if (py_current_exception() != NULL) note_failure(60 + worker->id);
            return NULL;
        }}

        static void *fresh_thread_after_exit(void *opaque) {{
            (void)opaque;
            if (py_current_exception() != NULL) return (void *)(uintptr_t)1;
            PyObject *current = raise_new("fresh-thread");
            if (current == NULL || py_current_exception() != current) {{
                return (void *)(uintptr_t)2;
            }}
            py_clear_exception();
            if (py_current_exception() != NULL) return (void *)(uintptr_t)3;
            return NULL;
        }}

        static void *runtime_owned_thread_with_active_exception(void *opaque) {{
            (void)opaque;
            if (py_current_exception() != NULL) return (void *)(uintptr_t)1;
            if (raise_new("active-at-runtime-thread-exit") == NULL) {{
                return (void *)(uintptr_t)2;
            }}
            /* pcc_thread_start's trampoline owns thread-exit teardown. */
            return NULL;
        }}

        int main(void) {{
            if (pcc_threads_enabled() != 1) return 2;

            /* Initialize exception classes and prove the main-thread slot/root
             * return to their baseline before native concurrency begins. */
            PyObject *warm = raise_new("warmup");
            if (warm == NULL || py_current_exception() != warm) return 3;
            py_clear_exception();
            if (py_current_exception() != NULL) return 4;
            int64_t baseline_roots = pcc_gc_scheduler_root_count();

            Worker workers[2] = {{ {{0, NULL, NULL}}, {{1, NULL, NULL}} }};
            pthread_t threads[2];
            if (pthread_create(&threads[0], NULL, worker_main, &workers[0]) != 0) {{
                return 5;
            }}
            if (pthread_create(&threads[1], NULL, worker_main, &workers[1]) != 0) {{
                atomic_store_explicit(&ready, 2, memory_order_release);
                atomic_store_explicit(&begin_mutation, 1, memory_order_release);
                atomic_store_explicit(&first_cleared, 1, memory_order_release);
                atomic_store_explicit(&second_replaced, 1, memory_order_release);
                atomic_store_explicit(
                    &replacement_cleared, 1, memory_order_release
                );
                pthread_join(threads[0], NULL);
                return 6;
            }}

            wait_for(&ready, 2);
            if (workers[0].first_seen == workers[1].first_seen) note_failure(70);
            if (EXPECT_REGISTERED_TLS_ROOTS) {{
                int64_t roots = pcc_gc_scheduler_root_count();
                if (roots != baseline_roots + 2) note_failure(71);
            }}
            atomic_store_explicit(&begin_mutation, 1, memory_order_release);

            pthread_join(threads[0], NULL);
            pthread_join(threads[1], NULL);
            if (atomic_load_explicit(&failure, memory_order_acquire) != 0) {{
                return 80 + atomic_load_explicit(&failure, memory_order_relaxed);
            }}
            if (py_current_exception() != NULL) return 81;
            if (
                EXPECT_REGISTERED_TLS_ROOTS
                && pcc_gc_scheduler_root_count() != baseline_roots
            ) return 82;

            /* A native thread created after both owners exited must begin with
             * a fresh slot, including when pthread_t storage is reused. */
            pthread_t fresh;
            void *fresh_result = NULL;
            if (pthread_create(&fresh, NULL, fresh_thread_after_exit, NULL) != 0) {{
                return 83;
            }}
            if (pthread_join(fresh, &fresh_result) != 0) return 84;
            if (fresh_result != NULL) return 85 + (int)(uintptr_t)fresh_result;
            if (
                EXPECT_REGISTERED_TLS_ROOTS
                && pcc_gc_scheduler_root_count() != baseline_roots
            ) return 89;

            PccThreadHandle *owned = NULL;
            void *owned_result = NULL;
            if (
                pcc_thread_start(
                    &owned, runtime_owned_thread_with_active_exception, NULL
                ) != 0
            ) return 90;
            if (pcc_thread_join(owned, &owned_result) != 0) return 91;
            if (owned_result != NULL) return 92 + (int)(uintptr_t)owned_result;
            if (py_current_exception() != NULL) return 95;
            if (
                EXPECT_REGISTERED_TLS_ROOTS
                && pcc_gc_scheduler_root_count() != baseline_roots
            ) return 96;

            printf("threaded-exception-tls-ok\\n");
            return 0;
        }}
        """
    ).lstrip()


def _link_harness(
    tmp_path: Path,
    *,
    name: str,
    archive: Path,
    expect_registered_tls_roots: bool,
) -> Path:
    source = tmp_path / f"{name}.c"
    executable = tmp_path / name
    source.write_text(
        _harness_source(
            expect_registered_tls_roots=expect_registered_tls_roots
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            os.environ.get("CC", "clang"),
            "-std=c11",
            f"-I{RUNTIME_DIR / 'include'}",
            f"-I{RUNTIME_DIR / 'src'}",
            str(source),
            str(archive),
            "-pthread",
            "-o",
            str(executable),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return executable


def test_pcc_python_exception_slot_is_compiler_owned_tls_and_registered_root():
    source = (RUNTIME_DIR / "py" / "py_substrate.py").read_text(encoding="utf-8")
    assert 'define_thread_local_ptr_null("py_tls_current_exc_storage")' in source
    assert 'define_global_ptr_null("py_tls_current_exc_storage")' not in source
    assert 'define_thread_local_ptr_null("py_tls_current_exc_root_handle")' in source
    setter = source.split('c_abi_export("py_tls_exc_set")', 1)[1].split(
        '\n\n@c_abi_export', 1
    )[0]
    assert "pcc_gc_scheduler_root_register_handle" in setter
    assert "pcc_gc_scheduler_root_unregister_handle" in setter
    assert "pcc_gc_note_slot_write_barrier" in setter
    assert 'global_addr("py_tls_current_exc_storage")' in setter

    thread_kernel = (
        RUNTIME_DIR / "py" / "freestanding_thread_kernel_pthread.py"
    ).read_text(encoding="utf-8")
    unregister = thread_kernel.split(
        "def pcc_thread_unregister_current()", 1
    )[1].split("\n\n@c_abi_export", 1)[0]
    assert unregister.index("py_clear_exception()") < unregister.index(
        "pcc_gc_thread_unregister_buffers()"
    )


def test_raw_pthreads_match_c_oracle_for_exception_tls_isolation(tmp_path: Path):
    oracle_runtime = cached_threaded_pcc_python_runtime()
    implementation_runtime = cached_threaded_pcc_python_runtime()
    oracle = _link_harness(
        tmp_path,
        name="threaded_exception_c_oracle",
        archive=oracle_runtime / "libpy_runtime_pcc_py.a",
        expect_registered_tls_roots=False,
    )
    implementation = _link_harness(
        tmp_path,
        name="threaded_exception_pcc_python",
        archive=implementation_runtime / "libpy_runtime_pcc_py.a",
        expect_registered_tls_roots=True,
    )
    for backend in range(5):
        env = {**os.environ, "PCC_GC_BACKEND": str(backend)}
        oracle_result = subprocess.run(
            [str(oracle)], env=env, capture_output=True, text=True, timeout=30
        )
        result = subprocess.run(
            [str(implementation)], env=env, capture_output=True, text=True, timeout=30
        )
        assert oracle_result.returncode == 0, (
            f"backend={backend}\n" + oracle_result.stdout + oracle_result.stderr
        )
        assert result.returncode == 0, (
            f"backend={backend}\n" + result.stdout + result.stderr
        )
        assert result.stdout == oracle_result.stdout == "threaded-exception-tls-ok\n"
