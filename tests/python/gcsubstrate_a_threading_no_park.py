"""Threading surface contracts, no-park leases, trampoline and unregister paths.

Split from ``test_gc_threading_substrate.py``; collected only through
that facade so pytest node ids stay stable.
"""
from _gc_substrate_common import *  # noqa: F401,F403


def test_threading_substrate_public_surface_is_in_header_and_runtime_abi():
    header = RUNTIME_HEADER.read_text(encoding="utf-8")
    for name in THREADING_SURFACE:
        assert name in header
        assert name in RUNTIME_SIGNATURES
    assert "Bounded, non-blocking native no-park regions" in header
    assert "must not span user callbacks" in header
    assert "pcc_thread_start argument or result" in header
    assert "generated-code stop polls" in header
    assert "performs no registration, lock, or safepoint" in header
    assert "not an additional liveness lease" in header
    assert "owns an\n * active stopped-world epoch" in header
    assert "Recursive\n * threaded teardown" in header
    for name in [
        "PCC_REFCOUNT_STRATEGY_NONATOMIC",
        "PCC_REFCOUNT_STRATEGY_ATOMIC",
        "PCC_REFCOUNT_STRATEGY_BIASED",
        "PCC_REFCOUNT_STRATEGY_DEFERRED",
    ]:
        assert name in header


def test_thread_no_park_source_order_and_newcomer_lock_contracts():
    # The host-C oracle/transition TU deliberately uses the host libc sink;
    # only the strict pcc-Python production archive owns pcc_platform_abort.

    py_src = THREAD_KERNEL_PTHREAD.read_text(encoding="utf-8")
    py_stop_acquire = py_src.split(
        "def pcc_thread_stop_requested_acquire() -> int:", 1
    )[1].split('@c_abi_export("pcc_refcount_incref")', 1)[0]
    assert 'atomic_load_i32(' in py_stop_acquire
    assert '"acquire"' in py_stop_acquire
    for forbidden in ["pcc_current_thread_id", "pcc_thread_safepoint", "mutex"]:
        assert forbidden not in py_stop_acquire
    py_stop = py_src.split("def pcc_stop_the_world() -> int:", 1)[1].split(
        '@c_abi_export("pcc_resume_world")', 1
    )[0]
    py_resume = py_src.split("def pcc_resume_world() -> int:", 1)[1].split(
        '@c_abi_export("pcc_thread_unregister_current")', 1
    )[0]
    assert '''atomic_store_i32(
        global_addr("pcc_thread_stop_requested"), 0, 1, "release"
    )''' in py_stop
    assert '''atomic_store_i32(
        global_addr("pcc_thread_stop_requested"), 0, 0, "release"
    )''' in py_resume
    assert py_src.count(
        '''atomic_store_i32(
        global_addr("pcc_thread_stop_requested")'''
    ) == 2
    py_world_init = py_src.split("def _world_init() -> int:", 1)[1].split(
        '@c_abi_export("pcc_threads_enabled")', 1
    )[0]
    assert py_world_init.lstrip().startswith("state = atomic_load_i32(")
    py_safepoint = py_src.split('def pcc_thread_safepoint() -> None:', 1)[1].split(
        '@c_abi_export("pcc_thread_owns_stopped_world")', 1
    )[0]
    assert py_safepoint.index("pcc_tls_no_park_depth_py") < py_safepoint.index(
        "_world_init()"
    )
    py_safepoint_init_failure = py_safepoint.split(
        "if _world_init() != 0:", 1
    )[1].split("self_id =", 1)[0]
    assert "pcc_platform_abort()" in py_safepoint_init_failure
    py_current = py_src.split("def pcc_current_thread_id() -> int:", 1)[1].split(
        '@c_abi_export("pcc_thread_no_park_enter")', 1
    )[0]
    assert py_current.index("while thread_id == 0") < py_current.index(
        "pcc_next_thread_id_py"
    )
    # The admission wait holds the world lock and runs before the thread has a
    # record, so it is the raw pthread wait: pcc_cond_wait enters the exited
    # state, which takes that lock (22f3548f8).
    assert py_current.index("pcc_registration_waiter_count_py") < py_current.index(
        "pthread_cond_wait(cond, lock)"
    ) < py_current.rindex("pcc_registration_waiter_count_py") < py_current.index(
        "pcc_next_thread_id_py"
    )
    py_unregister = py_src.split("def pcc_thread_unregister_current() -> None:", 1)[
        1
    ].split('@c_abi_export("pcc_thread_trampoline_py")', 1)[0]
    assert py_unregister.index("pcc_thread_no_park_depth()") < py_unregister.index(
        "py_clear_exception()"
    )
    assert py_unregister.index("pcc_tls_thread_id_py") < py_unregister.index(
        "py_clear_exception()"
    )
    assert py_unregister.index("pcc_tls_unregister_in_progress_py") < (
        py_unregister.index("pcc_tls_thread_id_py")
    )
    assert py_unregister.index(
        'store_i32(global_addr("pcc_tls_unregister_in_progress_py"), 0, 1)'
    ) < py_unregister.index("py_clear_exception()") < py_unregister.index(
        'store_i32(global_addr("pcc_tls_unregister_in_progress_py"), 0, 0)'
    )
    assert py_unregister.index("pcc_thread_owns_stopped_world()") < py_unregister.index(
        "py_clear_exception()"
    )
    py_cleanup = py_unregister.index("pcc_gc_thread_unregister_buffers()")
    py_commit_lock = py_unregister.index("pthread_mutex_lock(lock)", py_cleanup)
    assert py_cleanup < py_commit_lock < py_unregister.rindex(
        "pcc_thread_no_park_depth()"
    ) < py_unregister.index(
        '_tls_store_i64(global_addr("pcc_tls_thread_id_py"), 0)'
    )
    assert py_commit_lock < py_unregister.index(
        'global_addr("pcc_stop_owner_thread_id_py")', py_commit_lock
    ) < py_unregister.index(
        '_tls_store_i64(global_addr("pcc_tls_thread_id_py"), 0)'
    )
    py_current_id = py_src.split(
        '@c_abi_export("pcc_current_thread_id")', 1
    )[1].split('@c_abi_export("pcc_thread_no_park_enter")', 1)[0]
    init_failure = py_current_id.split("if _world_init() != 0:", 1)[1].split(
        "lock =", 1
    )[0]
    assert "pcc_platform_abort()" in init_failure
    assert "return 0" in init_failure
    py_enter = py_src.split(
        '@c_abi_export("pcc_thread_no_park_enter")', 1
    )[1].split('@c_abi_export("pcc_thread_no_park_exit")', 1)[0]
    assert py_enter.index('pcc_current_thread_id()') < py_enter.index(
        '_tls_i64(global_addr("pcc_tls_thread_id_py")) == 0'
    ) < py_enter.rindex(
        'store_i32(global_addr("pcc_tls_no_park_depth_py"), 0, depth + 1)'
    )
    py_trampoline = py_src.split("def _thread_trampoline(start):", 1)[1].split(
        '@c_abi_export("pcc_thread_start")', 1
    )[0]
    assert py_trampoline.index(
        "if pcc_mutex_lock(state_lock) != 0:"
    ) < py_trampoline.index(
        "store_ptr(handle, 24, result)"
    ) < py_trampoline.index(
        "pcc_thread_unregister_current()"
    ) < py_trampoline.rindex("return result")
    py_mutex_lock = py_src.split("def pcc_mutex_lock(mutex) -> int:", 1)[1].split(
        '@c_abi_export("pcc_mutex_unlock")', 1
    )[0]
    assert py_mutex_lock.index("pthread_mutex_trylock") < py_mutex_lock.index(
        "pcc_thread_safepoint()"
    )

    log_src = RUNTIME_LOG_PORT.read_text(encoding="utf-8")
    enabled = log_src.split("def pcc_diagnostics_runtime_log_enabled", 1)[1].split(
        "def _code_enabled", 1
    )[0]
    code_enabled = log_src.split("def _code_enabled", 1)[1].split(
        "def _write_lock_acquire", 1
    )[0]
    event = log_src.split("def pcc_diagnostics_runtime_log_event(", 1)[1].split(
        '@c_abi_export("pcc_diagnostics_runtime_log_event_code")', 1
    )[0]
    assert enabled.index("pcc_current_thread_id()") < enabled.index(
        "_init_once()"
    )
    assert code_enabled.index("pcc_current_thread_id()") < (
        code_enabled.index("_init_once()")
    )
    assert event.index("pcc_current_thread_id()") < event.index(
        "_write_lock_acquire()"
    )

    # The threads-off kernel left the runtime with 560c23fdf: the pthread
    # kernel is the only one, and a one-thread program's no-park and stop
    # contract is the probe in the next test.
    assert not THREAD_KERNEL.exists()
    assert 'define_thread_local_i32("pcc_tls_no_park_depth_py", 0)' in py_src


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_thread_no_park_nonthread_depth_and_world_owner_contract(
    tmp_path: Path,
    kind: str,
) -> None:
    # One thread on the threaded kernel, the only kernel since 560c23fdf (the
    # threads-off kernel's vacuous world ownership went with it): no-park
    # nesting survives a safepoint, the lone mutator owns the world exactly
    # while it holds a stop, and unregistering twice is harmless.
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=False,
        stem="no_park_nonthread",
        source_text=r'''
            #include "py_internal.h"

            int main(void) {
                if (pcc_threads_enabled() == 0) return 2;
                if (pcc_thread_no_park_depth() != 0) return 3;
                if (pcc_thread_owns_stopped_world() != 0) return 4;
                if (pcc_thread_registration_waiter_count() != 0) return 5;
                if (pcc_thread_stop_requested_acquire() != 0) return 6;
                pcc_thread_no_park_enter();
                if (pcc_thread_no_park_depth() != 1) return 7;
                pcc_thread_no_park_enter();
                if (pcc_thread_no_park_depth() != 2) return 8;
                pcc_thread_safepoint();
                if (pcc_thread_no_park_depth() != 2) return 9;
                pcc_thread_no_park_exit();
                if (pcc_thread_no_park_depth() != 1) return 10;
                pcc_thread_no_park_exit();
                if (pcc_thread_no_park_depth() != 0) return 11;
                if (pcc_stop_the_world() != 0) return 12;
                if (pcc_thread_owns_stopped_world() != 1) return 13;
                if (pcc_thread_stop_requested_acquire() == 0) return 14;
                if (pcc_resume_world() != 0) return 15;
                if (pcc_thread_owns_stopped_world() != 0) return 16;
                if (pcc_thread_stop_requested_acquire() != 0) return 17;
                if (pcc_thread_registration_waiter_count() != 0) return 18;
                pcc_thread_unregister_current();
                pcc_thread_unregister_current();
                return 0;
            }
        ''',
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=20
    )
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_thread_no_park_and_stopped_world_newcomers_use_real_pthreads(
    tmp_path: Path,
    kind: str,
) -> None:
    """State-driven C/strict differential; no sleeps or timing admission."""
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="no_park_pthreads",
        source_text=r'''
            #include "py_internal.h"
            #include <pthread.h>
            #include <sched.h>
            #include <stdint.h>

            static int64_t critical_ready;
            static int64_t critical_inner_safepoint_returned;
            static int64_t critical_nested_exit_returned;
            static int64_t critical_outer_exit_returned;

            static void *critical_depth_worker(void *arg) {
                (void)arg;
                pcc_thread_no_park_enter();
                pcc_thread_no_park_enter();
                if (pcc_thread_no_park_depth() != 2) {
                    return (void *)(intptr_t)2;
                }
                __atomic_store_n(&critical_ready, 1, __ATOMIC_RELEASE);
                /* This acquire-only kernel diagnostic is not the ordinary
                 * generated LLVM poll, which remains outside A1. */
                while (pcc_thread_stop_requested_acquire() == 0) {
                    pcc_thread_safepoint();
                    sched_yield();
                }
                pcc_thread_safepoint();
                __atomic_store_n(
                    &critical_inner_safepoint_returned, 1, __ATOMIC_RELEASE
                );
                pcc_thread_no_park_exit();
                if (pcc_thread_no_park_depth() != 1) {
                    return (void *)(intptr_t)3;
                }
                pcc_thread_safepoint();
                __atomic_store_n(
                    &critical_nested_exit_returned, 1, __ATOMIC_RELEASE
                );
                pcc_thread_no_park_exit();
                __atomic_store_n(
                    &critical_outer_exit_returned, 1, __ATOMIC_RELEASE
                );
                return 0;
            }

            static int64_t raw_id_ready;
            static int64_t raw_id_attempted;
            static int64_t raw_id_returned;
            static int64_t raw_log_ready;
            static int64_t raw_log_attempted;
            static int64_t raw_log_returned;
            static int64_t raw_code_ready;
            static int64_t raw_code_attempted;
            static int64_t raw_code_returned;
            static int64_t raw_enter_ready;
            static int64_t raw_enter_attempted;
            static int64_t raw_enter_returned;
            static int64_t raw_attempts_begin;
            static int64_t late_entry_started;

            static void *raw_id_worker(void *arg) {
                (void)arg;
                __atomic_store_n(&raw_id_ready, 1, __ATOMIC_RELEASE);
                while (__atomic_load_n(
                    &raw_attempts_begin, __ATOMIC_ACQUIRE
                ) == 0) {
                    sched_yield();
                }
                __atomic_store_n(&raw_id_attempted, 1, __ATOMIC_RELEASE);
                if (pcc_current_thread_id() <= 0) {
                    return (void *)(intptr_t)4;
                }
                __atomic_store_n(&raw_id_returned, 1, __ATOMIC_RELEASE);
                pcc_thread_unregister_current();
                pcc_thread_unregister_current();
                return 0;
            }

            static void *raw_log_worker(void *arg) {
                (void)arg;
                __atomic_store_n(&raw_log_ready, 1, __ATOMIC_RELEASE);
                while (__atomic_load_n(
                    &raw_attempts_begin, __ATOMIC_ACQUIRE
                ) == 0) {
                    sched_yield();
                }
                __atomic_store_n(&raw_log_attempted, 1, __ATOMIC_RELEASE);
                pcc_diagnostics_runtime_log_event("gc", "raw-newcomer", 1, 2, 0);
                __atomic_store_n(&raw_log_returned, 1, __ATOMIC_RELEASE);
                pcc_thread_unregister_current();
                return 0;
            }

            static void *raw_enter_worker(void *arg) {
                (void)arg;
                __atomic_store_n(&raw_enter_ready, 1, __ATOMIC_RELEASE);
                while (__atomic_load_n(
                    &raw_attempts_begin, __ATOMIC_ACQUIRE
                ) == 0) {
                    sched_yield();
                }
                __atomic_store_n(&raw_enter_attempted, 1, __ATOMIC_RELEASE);
                pcc_thread_no_park_enter();
                pcc_thread_no_park_exit();
                __atomic_store_n(&raw_enter_returned, 1, __ATOMIC_RELEASE);
                pcc_thread_unregister_current();
                return 0;
            }

            static void *raw_code_worker(void *arg) {
                (void)arg;
                __atomic_store_n(&raw_code_ready, 1, __ATOMIC_RELEASE);
                while (__atomic_load_n(
                    &raw_attempts_begin, __ATOMIC_ACQUIRE
                ) == 0) {
                    sched_yield();
                }
                __atomic_store_n(&raw_code_attempted, 1, __ATOMIC_RELEASE);
                pcc_diagnostics_runtime_log_event_code(2, 1, 5, 6, 0);
                __atomic_store_n(&raw_code_returned, 1, __ATOMIC_RELEASE);
                pcc_thread_unregister_current();
                return 0;
            }

            static void *late_pcc_entry(void *arg) {
                (void)arg;
                __atomic_store_n(&late_entry_started, 1, __ATOMIC_RELEASE);
                return 0;
            }

            static int wait_flag(int64_t *flag) {
                while (__atomic_load_n(flag, __ATOMIC_ACQUIRE) == 0) {
                    sched_yield();
                }
                return 0;
            }

            int main(void) {
                if (pcc_threads_enabled() != 1) return 10;
                if (pcc_current_thread_id() <= 0) return 11;
                if (pcc_thread_owns_stopped_world() != 0) return 12;
                if (pcc_thread_stop_requested_acquire() != 0) return 13;

                PccThreadHandle *critical = 0;
                if (pcc_thread_start(
                    &critical, critical_depth_worker, 0
                ) != 0) return 13;
                wait_flag(&critical_ready);
                if (pcc_stop_the_world() != 0) return 14;
                if (pcc_thread_owns_stopped_world() != 1) return 15;
                if (pcc_thread_stop_requested_acquire() != 1) return 16;
                if (__atomic_load_n(
                    &critical_inner_safepoint_returned, __ATOMIC_ACQUIRE
                ) != 1) return 16;
                if (__atomic_load_n(
                    &critical_nested_exit_returned, __ATOMIC_ACQUIRE
                ) != 1) return 17;
                if (__atomic_load_n(
                    &critical_outer_exit_returned, __ATOMIC_ACQUIRE
                ) != 0) return 18;
                if (pcc_resume_world() != 0) return 19;
                if (pcc_thread_stop_requested_acquire() != 0) return 20;
                void *result = 0;
                if (pcc_thread_join(critical, &result) != 0) return 21;
                if (result != 0) return 22;
                if (__atomic_load_n(
                    &critical_outer_exit_returned, __ATOMIC_ACQUIRE
                ) != 1) return 23;

                pthread_t raw_id;
                pthread_t raw_log;
                pthread_t raw_enter;
                pthread_t raw_code;
                if (pthread_create(&raw_id, 0, raw_id_worker, 0) != 0) return 23;
                if (pthread_create(&raw_log, 0, raw_log_worker, 0) != 0) return 24;
                if (pthread_create(
                    &raw_enter, 0, raw_enter_worker, 0
                ) != 0) return 25;
                if (pthread_create(
                    &raw_code, 0, raw_code_worker, 0
                ) != 0) return 26;
                wait_flag(&raw_id_ready);
                wait_flag(&raw_log_ready);
                wait_flag(&raw_enter_ready);
                wait_flag(&raw_code_ready);

                if (pcc_stop_the_world() != 0) return 27;
                if (pcc_thread_owns_stopped_world() != 1) return 28;
                __atomic_store_n(&raw_attempts_begin, 1, __ATOMIC_RELEASE);
                wait_flag(&raw_id_attempted);
                wait_flag(&raw_log_attempted);
                wait_flag(&raw_enter_attempted);
                wait_flag(&raw_code_attempted);
                while (pcc_thread_registration_waiter_count() < 4) {
                    sched_yield();
                }
                if (pcc_thread_registration_waiter_count() != 4) return 29;
                if (__atomic_load_n(&raw_id_returned, __ATOMIC_ACQUIRE) != 0) {
                    return 30;
                }
                if (__atomic_load_n(&raw_log_returned, __ATOMIC_ACQUIRE) != 0) {
                    return 31;
                }
                if (__atomic_load_n(&raw_enter_returned, __ATOMIC_ACQUIRE) != 0) {
                    return 32;
                }
                if (__atomic_load_n(&raw_code_returned, __ATOMIC_ACQUIRE) != 0) {
                    return 33;
                }
                /* The owner may perform I/O, but no no-park region may.  If
                 * the raw logger acquired its write lock before newcomer
                 * admission, this call cannot return to resume the world. */
                pcc_diagnostics_runtime_log_event("gc", "stw-owner", 3, 4, 0);

                /* This proves only that pcc_thread_start's user entry does
                 * not run in the owned epoch.  Its managed opaque-argument
                 * relocation lifetime is intentionally outside A1. */
                PccThreadHandle *late = 0;
                if (pcc_thread_start(&late, late_pcc_entry, 0) != 0) return 34;
                while (pcc_thread_registration_waiter_count() < 5) {
                    sched_yield();
                }
                if (__atomic_load_n(&late_entry_started, __ATOMIC_ACQUIRE) != 0) {
                    return 35;
                }
                if (pcc_resume_world() != 0) return 36;

                if (pcc_thread_join(late, &result) != 0) return 37;
                if (result != 0) return 38;
                if (pthread_join(raw_id, &result) != 0) return 39;
                if (result != 0) return 40;
                if (pthread_join(raw_log, &result) != 0) return 41;
                if (result != 0) return 42;
                if (pthread_join(raw_enter, &result) != 0) return 43;
                if (result != 0) return 44;
                if (pthread_join(raw_code, &result) != 0) return 45;
                if (result != 0) return 46;
                if (__atomic_load_n(&raw_id_returned, __ATOMIC_ACQUIRE) != 1) {
                    return 47;
                }
                if (__atomic_load_n(&raw_log_returned, __ATOMIC_ACQUIRE) != 1) {
                    return 48;
                }
                if (__atomic_load_n(&raw_enter_returned, __ATOMIC_ACQUIRE) != 1) {
                    return 49;
                }
                if (__atomic_load_n(&raw_code_returned, __ATOMIC_ACQUIRE) != 1) {
                    return 50;
                }
                if (__atomic_load_n(&late_entry_started, __ATOMIC_ACQUIRE) != 1) {
                    return 51;
                }
                if (pcc_thread_registration_waiter_count() != 0) return 53;
                if (pcc_thread_owns_stopped_world() != 0) return 54;
                /* All raw newcomers unregistered; no stale live count may
                 * block a subsequent stop. */
                if (pcc_stop_the_world() != 0) return 55;
                if (pcc_thread_owns_stopped_world() != 1) return 56;
                if (pcc_resume_world() != 0) return 57;
                return 0;
            }
        ''',
    )
    run = subprocess.run(
        [str(executable)],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            "PCC_LOG": "gc",
            "PCC_LOG_FORMAT": "text",
            "PCC_LOG_FILE": "/dev/null",
        },
    )
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_thread_trampoline_commits_handle_before_final_unregister(
    tmp_path: Path,
    kind: str,
) -> None:
    handle_access = r'''
        typedef struct {
            void *thread;
            PccMutex *state_lock;
            int32_t done;
            int32_t detached;
            void *result;
        } TestThreadHandle;

        static int test_handle_lock(PccThreadHandle *handle) {
            TestThreadHandle *view = (TestThreadHandle *)handle;
            return (int)pcc_mutex_lock(view->state_lock);
        }
        static int test_handle_unlock(PccThreadHandle *handle) {
            TestThreadHandle *view = (TestThreadHandle *)handle;
            return (int)pcc_mutex_unlock(view->state_lock);
        }
        static int test_handle_done(PccThreadHandle *handle) {
            return ((TestThreadHandle *)handle)->done;
        }
    '''
    source_text = r'''
        #include "py_internal.h"
        #include <pthread.h>
        #include <sched.h>
        #include <stdint.h>

        @HANDLE_ACCESS@

        static int64_t entry_ready;
        static int64_t release_entry;

        static void *worker(void *arg) {
            (void)arg;
            __atomic_store_n(&entry_ready, 1, __ATOMIC_RELEASE);
            while (__atomic_load_n(&release_entry, __ATOMIC_ACQUIRE) == 0) {
                sched_yield();
            }
            return 0;
        }

        int main(void) {
            if (pcc_current_thread_id() <= 0) return 2;
            PccThreadHandle *thread = 0;
            if (pcc_thread_start(&thread, worker, 0) != 0) return 3;
            while (__atomic_load_n(&entry_ready, __ATOMIC_ACQUIRE) == 0) {
                sched_yield();
            }
            if (test_handle_lock(thread) != 0) return 4;
            __atomic_store_n(&release_entry, 1, __ATOMIC_RELEASE);

            /* The worker must remain live while contending for its handle
             * commit lock, and its contention path must park for this stop. */
            if (pcc_stop_the_world() != 0) return 5;
            if (pcc_thread_owns_stopped_world() != 1) return 6;
            if (pcc_thread_registration_waiter_count() != 0) return 7;
            if (test_handle_done(thread) != 0) return 8;
            if (pcc_resume_world() != 0) return 9;
            if (test_handle_done(thread) != 0) return 10;
            if (test_handle_unlock(thread) != 0) return 11;

            void *result = 0;
            if (pcc_thread_join(thread, &result) != 0) return 12;
            if (result != 0) return 13;
            if (pcc_thread_registration_waiter_count() != 0) return 14;
            /* A re-registration after teardown would strand a live pthread
             * and make this otherwise single-live-thread stop hang. */
            if (pcc_stop_the_world() != 0) return 15;
            if (pcc_resume_world() != 0) return 16;
            return 0;
        }
    '''.replace("@HANDLE_ACCESS@", textwrap.dedent(handle_access))
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="thread_trampoline_final_unregister",
        source_text=source_text,
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=30
    )
    assert run.returncode == 0, run.stdout + run.stderr


@pytest.mark.parametrize("kind", ["pcc_python"])
def test_thread_unregister_with_live_no_park_depth_fails_stop(
    tmp_path: Path,
    kind: str,
) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind=kind,
        threaded=True,
        stem="no_park_unregister_abort",
        source_text=r'''
            #include "py_internal.h"

            static void *depth_worker(void *arg) {
                (void)arg;
                pcc_thread_no_park_enter();
                pcc_thread_unregister_current();
                return 0;
            }

            static void *world_owner_worker(void *arg) {
                (void)arg;
                if (pcc_stop_the_world() != 0) return (void *)2;
                pcc_thread_unregister_current();
                return 0;
            }

            int main(int argc, char **argv) {
                (void)argv;
                PccThreadHandle *thread = 0;
                PccThreadMain entry = argc > 1
                    ? world_owner_worker
                    : depth_worker;
                if (pcc_thread_start(&thread, entry, 0) != 0) return 2;
                void *result = 0;
                return pcc_thread_join(thread, &result) == 0 ? 0 : 3;
            }
        ''',
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=20
    )
    abort_returncodes = {-int(signal.SIGABRT), 134}
    assert run.returncode in abort_returncodes, (
        f"expected no-park fail-stop, got {run.returncode}: "
        + run.stdout
        + run.stderr
    )
    owner_run = subprocess.run(
        [str(executable), "world-owner"],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert owner_run.returncode in abort_returncodes, (
        f"expected stopped-world-owner fail-stop, got {owner_run.returncode}: "
        + owner_run.stdout
        + owner_run.stderr
    )


def test_strict_thread_unregister_cleanup_reentry_fails_stop(tmp_path: Path) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind="pcc_python",
        threaded=True,
        stem="thread_unregister_cleanup_reentry",
        source_text=r'''
            #include "py_internal.h"
            #include <stdint.h>

            static void recursive_finalizer(PyObject *self) {
                (void)self;
                pcc_thread_unregister_current();
            }

            static void *worker(void *arg) {
                (void)arg;
                PyClassObject *cls = py_class_new(
                    "RecursiveUnregister", 0, 0, 0, 0
                );
                if (cls == 0) return (void *)(uintptr_t)2;
                py_class_add_method(
                    cls,
                    "__del__",
                    (PyObject *)(uintptr_t)recursive_finalizer
                );
                PyObject *inst = py_instance_new(cls);
                if (inst == 0) return (void *)(uintptr_t)3;
                /* Transfer the instance's sole owned reference to the TLS
                 * exception slot.  Trampoline teardown decrefs it, enters
                 * __del__, and recursively invokes unregister. */
                py_tls_exc_set(inst);
                return 0;
            }

            int main(void) {
                PccThreadHandle *thread = 0;
                if (pcc_thread_start(&thread, worker, 0) != 0) return 4;
                void *result = 0;
                return pcc_thread_join(thread, &result) == 0 ? 0 : 5;
            }
        ''',
    )
    run = subprocess.run(
        [str(executable)], capture_output=True, text=True, timeout=20
    )
    assert run.returncode in {-int(signal.SIGABRT), 134}, (
        f"expected recursive-unregister fail-stop, got {run.returncode}: "
        + run.stdout
        + run.stderr
    )


def test_threading_c_oracle_is_not_linked_into_pcc_python_archive():
    makefile = RUNTIME_MAKEFILE.read_text(encoding="utf-8")
    assert "freestanding_thread_kernel_pthread" in makefile
    assert "PCC_WITH_THREADS" in makefile
    # The retired C runtime compiled its refcount strategy in; no compile
    # define selects it now. PCC_REFCOUNT_KIND only names the build
    # configuration in receipts and variant paths, and the linked thread
    # kernel owns the strategy (0 non-atomic, 1 atomic).
    assert "-DPCC_REFCOUNT" not in makefile
    assert "pcc_threads.c" not in makefile
    runtime_py = RUNTIME_MAKEFILE.parent / "py"
    # The one (threaded) kernel owns the atomic strategy.
    for kernel, strategy in (
        ("freestanding_thread_kernel_pthread.py", 1),
    ):
        source = (runtime_py / kernel).read_text(encoding="utf-8")
        body = source.split('@c_abi_export("pcc_refcount_strategy")', 1)[1]
        body = body.split("\n@c_abi_export", 1)[0]
        assert f"return {strategy}" in body, kernel


def test_refcount_paths_go_through_strategy_helpers():

    py_src = PY_OBJ_PORT.read_text(encoding="utf-8")
    assert 'extern("pcc_refcount_incref"' in py_src
    assert 'extern("pcc_refcount_decref"' in py_src
    assert 'extern("pcc_refcount_forget"' in py_src
    assert "pcc_refcount_forget(o)" in py_src
    assert 'extern("py_dealloc_thread_thread"' in py_src
    assert "pcc_refcount_incref(o)" in py_src
    assert "pcc_refcount_decref(o)" in py_src


