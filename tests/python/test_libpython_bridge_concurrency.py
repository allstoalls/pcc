"""Threading, callback re-entry, and bigint coverage for the libpython bridge.

The production compatibility archive is derived from the immutable,
content-addressed pcc-Python runtime archive.  This test repeats that small
archive transformation in ``tmp_path`` so it never consumes a mutable archive
from ``pcc/runtime`` and cannot pass merely because a prebuilt libpython
variant happened to be present.
"""

from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import textwrap

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOT = REPO_ROOT / "pcc" / "runtime"


def _command_from_env(name: str, default: str) -> list[str]:
    command = shlex.split(os.environ.get(name, default))
    return command or [default]


def _python_config_command() -> list[str]:
    return _command_from_env("PCC_PYTHON_CONFIG", "python3-config")


def _run(
    command: list[str],
    *,
    cwd: Path = REPO_ROOT,
    timeout: int = 30,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    assert result.returncode == 0, (
        f"command failed ({result.returncode}): {shlex.join(command)}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


def _python_config_flags(*arguments: str) -> list[str]:
    result = _run([*_python_config_command(), *arguments], timeout=10)
    return shlex.split(result.stdout.strip())


def _bridge_prerequisite_failure() -> str | None:
    commands = {
        "C compiler": _command_from_env("CC", "cc"),
        "archiver": _command_from_env("AR", "ar"),
        "archive indexer": _command_from_env("RANLIB", "ranlib"),
        "symbol inspector": _command_from_env("NM", "nm"),
        "python3-config": _python_config_command(),
    }
    for label, command in commands.items():
        if shutil.which(command[0]) is None:
            return f"libpython bridge harness requires {label}: {command[0]}"
    for arguments in (("--includes",), ("--ldflags", "--embed")):
        try:
            result = subprocess.run(
                [*_python_config_command(), *arguments],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return f"python3-config {' '.join(arguments)} unavailable: {exc}"
        if result.returncode != 0 or not result.stdout.strip():
            return (
                f"python3-config {' '.join(arguments)} unavailable: "
                f"{result.stderr.strip()}"
            )
    return None


pytestmark = pytest.mark.pcc_gate(unavailable=_bridge_prerequisite_failure())


def _archive_members(archive: Path) -> set[str]:
    return set(
        _run([*_command_from_env("AR", "ar"), "t", str(archive)], timeout=10)
        .stdout.splitlines()
    )


@pytest.fixture
def libpython_variant_archive(
    tmp_path: Path,
    pcc_runtime_archive: Path,
) -> Path:
    """Create the Makefile's libpython variant from the cached base archive."""

    base_archive = pcc_runtime_archive.resolve()
    base_members = _archive_members(base_archive)
    assert len(base_members) > 20, "content-addressed base archive is vacuous"
    assert "py_extension_loader_runtime.o" in base_members
    assert "py_libpython.o" not in base_members

    bridge_object = tmp_path / "py_libpython.o"
    cppflags = shlex.split(os.environ.get("CPPFLAGS", ""))
    if not sys.platform.startswith("darwin"):
        cppflags.append("-D_DEFAULT_SOURCE")
    cflags = shlex.split(
        os.environ.get("CFLAGS", "-O2 -fPIC -Wall -Wextra -std=c11")
    )
    _run(
        [
            *_command_from_env("CC", "cc"),
            *cppflags,
            *cflags,
            "-DPCC_WITH_THREADS=1",
            "-DPCC_WITH_LIBPYTHON=1",
            *_python_config_flags("--includes"),
            "-I",
            str(RUNTIME_ROOT / "include"),
            "-c",
            str(RUNTIME_ROOT / "src" / "py_libpython.c"),
            "-o",
            str(bridge_object),
        ],
        timeout=30,
    )
    assert bridge_object.stat().st_size > 0
    undefined = {
        line.split()[-1]
        for line in _run(
            [*_command_from_env("NM", "nm"), "-u", str(bridge_object)],
            timeout=10,
        ).stdout.splitlines()
        if line.split()
    }
    if sys.platform.startswith("darwin"):
        undefined = {
            symbol[1:] if symbol.startswith("_") else symbol
            for symbol in undefined
        }
    assert not {
        symbol
        for symbol in undefined
        if symbol.startswith("Py") or symbol.startswith("_Py")
    }, "the bridge object must resolve CPython ABI only through dlsym"

    variant = tmp_path / "libpy_runtime_pcc_py_libpython.a"
    shutil.copy2(base_archive, variant)
    ar = _command_from_env("AR", "ar")
    _run([*ar, "d", str(variant), "py_extension_loader_runtime.o"], timeout=10)
    _run([*ar, "r", str(variant), str(bridge_object)], timeout=10)
    _run(
        [*_command_from_env("RANLIB", "ranlib"), str(variant)],
        timeout=10,
    )

    base_capi_sidecar = Path(str(base_archive) + ".capi_syms")
    assert base_capi_sidecar.is_file(), (
        "content-addressed base archive is missing its C-API inventory"
    )
    shutil.copy2(base_capi_sidecar, Path(str(variant) + ".capi_syms"))

    variant_members = _archive_members(variant)
    assert variant_members == (
        base_members - {"py_extension_loader_runtime.o"}
    ) | {"py_libpython.o"}
    return variant


def test_libpython_bridge_concurrent_first_entry_callback_reentry_and_bigint(
    tmp_path: Path,
    libpython_variant_archive: Path,
) -> None:
    from pcc.frontends.python import pipeline

    (tmp_path / "bridge_callback_helper.py").write_text(
        "import sys\n"
        "import threading\n"
        "\n"
        "def capture(callback, argument):\n"
        "    try:\n"
        "        return 'ok:' + str(callback(argument))\n"
        "    except BaseException as exc:\n"
        "        return type(exc).__name__ + ':' + str(exc)\n"
        "\n"
        "def capture_on_thread(callback, argument):\n"
        "    outcome = []\n"
        "    def run():\n"
        "        outcome.append(capture(callback, argument))\n"
        "    worker = threading.Thread(target=run)\n"
        "    worker.start()\n"
        "    worker.join()\n"
        "    return outcome[0]\n"
        "\n"
        "def catch_then_call(first, second, argument):\n"
        "    try:\n"
        "        first(argument)\n"
        "    except RuntimeError:\n"
        "        pass\n"
        "    return second(argument)\n"
        "\n"
        "def catch_then_raise(callback, argument):\n"
        "    try:\n"
        "        callback(argument)\n"
        "    except RuntimeError:\n"
        "        raise LookupError('replacement')\n"
        "\n"
        "def error_type_refcount():\n"
        "    return sys.getrefcount(WorkerError)\n"
        "\n"
        "class WorkerError(Exception):\n"
        "    pass\n"
        "\n"
        "def fail_worker():\n"
        "    raise WorkerError('worker failure')\n"
        "\n"
        "destroyed_tokens = 0\n"
        "class Token:\n"
        "    def __del__(self):\n"
        "        global destroyed_tokens\n"
        "        destroyed_tokens += 1\n"
        "\n"
        "def make_token():\n"
        "    return Token()\n"
        "\n"
        "def destroyed_token_count():\n"
        "    return destroyed_tokens\n"
        "\n"
        "def alias_cycle_ok(value):\n"
        "    return (value['left'] is value['right']\n"
        "            and value['cycle'][0] is value['cycle'])\n"
        "\n"
        "def backend4_graph_ok(value):\n"
        "    return (value[0] is value[1]\n"
        "            and value[3] is value\n"
        "            and value[0]['n'] == 5\n"
        "            and value[2] == {7})\n",
        encoding="utf-8",
    )
    harness = tmp_path / "libpython_bridge_concurrency.c"
    harness.write_text(
        textwrap.dedent(
            r"""
            #include "py_runtime.h"
            #include "py_internal.h"

            #include <pthread.h>
            #include <stdatomic.h>
            #include <stdint.h>
            #include <stdio.h>
            #include <stdlib.h>
            #include <string.h>

            enum { THREAD_COUNT = 4, ITERATIONS = 256 };

            typedef struct {
                pthread_mutex_t mutex;
                pthread_cond_t condition;
                int arrived;
                int generation;
            } StartGate;

            typedef struct {
                int index;
                int error;
            } Worker;

            static StartGate start_gate = {
                PTHREAD_MUTEX_INITIALIZER,
                PTHREAD_COND_INITIALIZER,
                0,
                0,
            };
            static atomic_int successful_calls = ATOMIC_VAR_INIT(0);

            static int wait_for_all_workers(StartGate *gate) {
                if (pthread_mutex_lock(&gate->mutex) != 0) return -1;
                int generation = gate->generation;
                gate->arrived += 1;
                if (gate->arrived == THREAD_COUNT) {
                    gate->arrived = 0;
                    gate->generation += 1;
                    if (pthread_cond_broadcast(&gate->condition) != 0) {
                        pthread_mutex_unlock(&gate->mutex);
                        return -1;
                    }
                } else {
                    while (generation == gate->generation) {
                        if (pthread_cond_wait(
                                &gate->condition, &gate->mutex
                            ) != 0) {
                            pthread_mutex_unlock(&gate->mutex);
                            return -1;
                        }
                    }
                }
                return pthread_mutex_unlock(&gate->mutex);
            }

            static void *worker_main(void *opaque) {
                Worker *worker = (Worker *)opaque;
                void *builtins = NULL;
                void *abs_function = NULL;

                if (wait_for_all_workers(&start_gate) != 0) {
                    worker->error = 10;
                    return NULL;
                }

                /* No bridge API runs before this barrier.  All four pthreads
                 * therefore race through the bridge's first initialization. */
                builtins = py_cpy_import("builtins");
                if (builtins == NULL) {
                    worker->error = 11;
                    return NULL;
                }
                abs_function = py_cpy_getattr(builtins, "abs");
                if (abs_function == NULL) {
                    worker->error = 12;
                    py_cpy_decref(builtins);
                    return NULL;
                }

                for (int i = 0; i < ITERATIONS; i++) {
                    int64_t magnitude =
                        (int64_t)worker->index * ITERATIONS + i + 1;
                    void *argument = py_cpy_from_i64(-magnitude);
                    if (argument == NULL) {
                        worker->error = 13;
                        break;
                    }

                    /* Exercise the public refcount bridge on both arguments
                     * and results without changing their net ownership. */
                    py_cpy_incref(argument);
                    py_cpy_decref(argument);
                    void *result = py_cpy_call1(abs_function, argument);
                    if (result == NULL) {
                        worker->error = 14;
                        py_cpy_decref(argument);
                        break;
                    }
                    py_cpy_incref(result);
                    py_cpy_decref(result);
                    int64_t observed = py_cpy_to_i64(result);
                    py_cpy_decref(result);
                    py_cpy_decref(argument);
                    if (observed != magnitude) {
                        worker->error = 15;
                        break;
                    }
                    atomic_fetch_add_explicit(
                        &successful_calls, 1, memory_order_relaxed
                    );
                }

                py_cpy_decref(abs_function);
                py_cpy_decref(builtins);
                return NULL;
            }

            static int run_concurrent_bridge_calls(void) {
                pthread_t threads[THREAD_COUNT];
                Worker workers[THREAD_COUNT] = {0};
                for (int i = 0; i < THREAD_COUNT; i++) {
                    workers[i].index = i;
                    if (pthread_create(
                            &threads[i], NULL, worker_main, &workers[i]
                        ) != 0) {
                        return 20;
                    }
                }
                for (int i = 0; i < THREAD_COUNT; i++) {
                    if (pthread_join(threads[i], NULL) != 0) return 21;
                    if (workers[i].error != 0) {
                        fprintf(
                            stderr,
                            "worker %d failed: %d\n",
                            i,
                            workers[i].error
                        );
                        return workers[i].error;
                    }
                }
                int observed = atomic_load_explicit(
                    &successful_calls, memory_order_relaxed
                );
                printf("ok:%d\n", observed);
                return observed == THREAD_COUNT * ITERATIONS ? 0 : 22;
            }

            static void *reentrant_abs(void *argument) {
                /* The wrapper trampoline releases CPython's GIL before this
                 * pcc callback.  Re-entering the public bridge here must
                 * acquire a fresh guard and restore the outer call cleanly. */
                void *builtins = py_cpy_import("builtins");
                if (builtins == NULL) return NULL;
                void *abs_function = py_cpy_getattr(builtins, "abs");
                if (abs_function == NULL) {
                    py_cpy_decref(builtins);
                    return NULL;
                }
                void *result = py_cpy_call1(abs_function, argument);
                py_cpy_decref(abs_function);
                py_cpy_decref(builtins);
                return result;
            }

            static int run_reentrant_callback(void) {
                void *wrapped = py_cpy_wrap_pcc_1arg((void *)reentrant_abs);
                if (wrapped == NULL) return 30;
                void *argument = py_cpy_from_i64(-321);
                if (argument == NULL) {
                    py_cpy_decref(wrapped);
                    return 31;
                }
                void *result = py_cpy_call1(wrapped, argument);
                if (result == NULL) {
                    py_cpy_decref(argument);
                    py_cpy_decref(wrapped);
                    return 32;
                }
                int64_t observed = py_cpy_to_i64(result);
                py_cpy_decref(result);
                py_cpy_decref(argument);
                py_cpy_decref(wrapped);
                printf("reentrant:%lld\n", (long long)observed);
                return observed == 321 ? 0 : 33;
            }

            static void *pcc_error_callback(void *argument) {
                (void)argument;
                py_raise_owned(py_exc_new(PY_EXC_VALUEERROR, "pcc boom"));
                return NULL;
            }

            static void *empty_error_callback(void *argument) {
                (void)argument;
                return NULL;
            }

            static void *cpy_error_with_result_callback(void *argument) {
                (void)argument;
                void *result = py_cpy_from_i64(7);
                void *missing = py_cpy_import("_pcc_missing_callback_module");
                if (missing != NULL) py_cpy_decref(missing);
                return result;
            }

            static void *pcc_error_with_result_callback(void *argument) {
                (void)argument;
                void *result = py_cpy_from_i64(9);
                py_raise_owned(py_exc_new(PY_EXC_VALUEERROR, "pcc owned boom"));
                return result;
            }

            static int run_callback_error_case(
                const char *label,
                void *callback,
                const char *capture_name,
                const char *expected
            ) {
                void *helper = py_cpy_import("bridge_callback_helper");
                if (helper == NULL) return 50;
                void *capture = py_cpy_getattr(helper, capture_name);
                if (capture == NULL) {
                    py_cpy_decref(helper);
                    return 51;
                }
                void *wrapped = py_cpy_wrap_pcc_1arg(callback);
                void *argument = py_cpy_from_i64(1);
                if (wrapped == NULL || argument == NULL) {
                    if (wrapped != NULL) py_cpy_decref(wrapped);
                    if (argument != NULL) py_cpy_decref(argument);
                    py_cpy_decref(capture);
                    py_cpy_decref(helper);
                    return 52;
                }
                void *captured = py_cpy_call2(capture, wrapped, argument);
                PyObject *text = py_cpy_to_pcc_obj(captured);
                const char *observed = text != NULL ? py_str_utf8(text) : NULL;
                int matches = observed != NULL && strcmp(observed, expected) == 0;
                int pcc_error_leaked = py_err_occurred() != 0;
                int status = 0;
                if (!matches) {
                    fprintf(
                        stderr,
                        "%s callback mismatch: %s\n",
                        label,
                        observed != NULL ? observed : "<null>"
                    );
                    status = 53;
                } else if (pcc_error_leaked) {
                    status = 54;
                }
                if (text != NULL) py_decref(text);
                if (captured != NULL) py_cpy_decref(captured);
                py_cpy_decref(argument);
                py_cpy_decref(wrapped);
                py_cpy_decref(capture);
                py_cpy_decref(helper);
                if (status != 0) return status;
                printf("callback:%s\n", label);
                return 0;
            }

            static int run_callback_error_translation(void) {
                int status = run_callback_error_case(
                    "pcc-error",
                    (void *)pcc_error_callback,
                    "capture",
                    "RuntimeError:pcc boom"
                );
                if (status != 0) return status;
                status = run_callback_error_case(
                    "empty-error",
                    (void *)empty_error_callback,
                    "capture",
                    "RuntimeError:pcc callback returned NULL without a CPython exception"
                );
                if (status != 0) return status;
                status = run_callback_error_case(
                    "cpy-error-result",
                    (void *)cpy_error_with_result_callback,
                    "capture",
                    "ModuleNotFoundError:No module named '_pcc_missing_callback_module'"
                );
                if (status != 0) return status;
                return run_callback_error_case(
                    "pcc-error-result",
                    (void *)pcc_error_with_result_callback,
                    "capture",
                    "RuntimeError:pcc owned boom"
                );
            }

            static int run_pcc_callback_error_propagation(void) {
                void *wrapped = py_cpy_wrap_pcc_1arg(
                    (void *)pcc_error_callback
                );
                void *argument = py_cpy_from_i64(1);
                if (wrapped == NULL || argument == NULL) return 60;
                void *result = py_cpy_call1(wrapped, argument);
                py_cpy_decref(argument);
                py_cpy_decref(wrapped);
                if (result != NULL) {
                    py_cpy_decref(result);
                    return 61;
                }
                if (!py_err_occurred()) return 62;
                PyObject *error = py_current_exception();
                PyObject *message = py_exc_get_message(error);
                const char *text = message != NULL ? py_str_utf8(message) : NULL;
                int matches = text != NULL && strcmp(text, "pcc boom") == 0;
                py_clear_exception();
                if (!matches) return 63;
                void *builtins = py_cpy_import("builtins");
                if (builtins == NULL) return 64;
                py_cpy_decref(builtins);
                printf("callback:pcc-propagated\n");
                return 0;
            }

            static int run_cpython_owned_callback_error(void) {
                return run_callback_error_case(
                    "cpy-thread",
                    (void *)pcc_error_callback,
                    "capture_on_thread",
                    "RuntimeError:pcc boom"
                );
            }

            static int run_caught_then_second_callback(void) {
                void *helper = py_cpy_import("bridge_callback_helper");
                void *driver = helper != NULL
                    ? py_cpy_getattr(helper, "catch_then_call")
                    : NULL;
                void *first = py_cpy_wrap_pcc_1arg(
                    (void *)pcc_error_callback
                );
                void *second = py_cpy_wrap_pcc_1arg((void *)reentrant_abs);
                void *argument = py_cpy_from_i64(-321);
                if (
                    helper == NULL
                    || driver == NULL
                    || first == NULL
                    || second == NULL
                    || argument == NULL
                ) return 70;
                void *result = py_cpy_call3(driver, first, second, argument);
                int64_t observed = result != NULL ? py_cpy_to_i64(result) : -1;
                int leaked = py_err_occurred() != 0;
                if (result != NULL) py_cpy_decref(result);
                py_cpy_decref(argument);
                py_cpy_decref(second);
                py_cpy_decref(first);
                py_cpy_decref(driver);
                py_cpy_decref(helper);
                if (result == NULL || observed != 321 || leaked) return 71;
                printf("callback:caught-then-second\n");
                return 0;
            }

            static int run_caught_then_replacement_error(void) {
                void *helper = py_cpy_import("bridge_callback_helper");
                void *driver = helper != NULL
                    ? py_cpy_getattr(helper, "catch_then_raise")
                    : NULL;
                void *wrapped = py_cpy_wrap_pcc_1arg(
                    (void *)pcc_error_callback
                );
                void *argument = py_cpy_from_i64(1);
                if (
                    helper == NULL
                    || driver == NULL
                    || wrapped == NULL
                    || argument == NULL
                ) return 72;
                void *result = py_cpy_call2(driver, wrapped, argument);
                py_cpy_decref(argument);
                py_cpy_decref(wrapped);
                py_cpy_decref(driver);
                py_cpy_decref(helper);
                if (result != NULL) {
                    py_cpy_decref(result);
                    return 73;
                }
                if (!py_err_occurred()) return 74;
                PyObject *error = py_current_exception();
                PyObject *message = py_exc_get_message(error);
                const char *text = message != NULL ? py_str_utf8(message) : NULL;
                int matches = text != NULL && strcmp(text, "replacement") == 0;
                py_clear_exception();
                if (!matches) return 75;
                void *builtins = py_cpy_import("builtins");
                if (builtins == NULL) return 76;
                py_cpy_decref(builtins);
                printf("callback:replacement-error\n");
                return 0;
            }

            static void *pending_error_callable = NULL;

            static void *pending_error_worker(void *opaque) {
                (void)opaque;
                void *result = py_cpy_call_noargs(pending_error_callable);
                if (result != NULL) {
                    py_cpy_decref(result);
                    return (void *)(uintptr_t)1;
                }
                /* Deliberately make the failed import this pthread's final
                 * bridge entry.  Its TSD destructor must release the owned
                 * CPython exception triple without another API call. */
                return NULL;
            }

            static int error_type_refcount(void) {
                void *helper = py_cpy_import("bridge_callback_helper");
                if (helper == NULL) {
                    fprintf(stderr, "refcount helper import failed\n");
                    return -10;
                }
                void *reader = py_cpy_getattr(helper, "error_type_refcount");
                if (reader == NULL) {
                    fprintf(stderr, "refcount reader lookup failed\n");
                    py_cpy_decref(helper);
                    return -11;
                }
                void *value = py_cpy_call_noargs(reader);
                if (value == NULL) {
                    fprintf(stderr, "refcount reader call failed\n");
                }
                int result = value != NULL ? (int)py_cpy_to_i64(value) : -1;
                if (value != NULL) py_cpy_decref(value);
                py_cpy_decref(reader);
                py_cpy_decref(helper);
                return result;
            }

            static int run_pending_error_thread_cleanup(void) {
                void *helper = py_cpy_import("bridge_callback_helper");
                pending_error_callable = helper != NULL
                    ? py_cpy_getattr(helper, "fail_worker")
                    : NULL;
                if (helper != NULL) py_cpy_decref(helper);
                if (pending_error_callable == NULL) return 79;
                pthread_t warm;
                if (pthread_create(&warm, NULL, pending_error_worker, NULL) != 0) {
                    return 80;
                }
                void *worker_result = NULL;
                if (pthread_join(warm, &worker_result) != 0 || worker_result != NULL) {
                    return 81;
                }
                int before = error_type_refcount();
                if (before < 0) return 82;

                enum { ERROR_THREADS = 32 };
                pthread_t threads[ERROR_THREADS];
                for (int i = 0; i < ERROR_THREADS; i++) {
                    if (pthread_create(
                            &threads[i], NULL, pending_error_worker, NULL
                        ) != 0) return 83;
                }
                for (int i = 0; i < ERROR_THREADS; i++) {
                    worker_result = NULL;
                    if (
                        pthread_join(threads[i], &worker_result) != 0
                        || worker_result != NULL
                    ) return 84;
                }
                int after = error_type_refcount();
                if (after < 0 || after > before + 1) {
                    fprintf(
                        stderr,
                        "pending error type refcount leaked: %d -> %d\n",
                        before,
                        after
                    );
                    py_cpy_decref(pending_error_callable);
                    pending_error_callable = NULL;
                    return 85;
                }
                py_cpy_decref(pending_error_callable);
                pending_error_callable = NULL;
                printf("pending-cleanup:%d->%d\n", before, after);
                return 0;
            }

            static int run_container_roundtrip(void) {
                PyObject *root = py_dict_new();
                PyObject *list = py_list_new(1);
                PyObject *tuple = py_tuple_new(2);
                PyObject *set = py_set_new();
                PyObject *list_key = py_str_new("list", 4);
                PyObject *tuple_key = py_str_new("tuple", 5);
                PyObject *set_key = py_str_new("set", 3);
                PyObject *one = py_int_from_i64(1);
                PyObject *two = py_int_from_i64(2);
                PyObject *three = py_int_from_i64(3);
                PyObject *text = py_str_new("nested", 6);
                if (
                    root == NULL || list == NULL || tuple == NULL || set == NULL
                    || list_key == NULL || tuple_key == NULL || set_key == NULL
                    || one == NULL || two == NULL || three == NULL || text == NULL
                ) return 90;

                py_list_append(list, one);
                py_tuple_set_item(tuple, 0, two);
                py_tuple_set_item(tuple, 1, text);
                py_set_add(set, three);
                py_dict_set(root, list_key, list);
                py_dict_set(root, tuple_key, tuple);
                py_dict_set(root, set_key, set);

                py_decref(one);
                py_decref(two);
                py_decref(three);
                py_decref(text);
                py_decref(list_key);
                py_decref(tuple_key);
                py_decref(set_key);
                py_decref(list);
                py_decref(tuple);
                py_decref(set);

                void *cpython_value = py_cpy_from_pcc_obj(root);
                if (cpython_value == NULL) {
                    py_decref(root);
                    return 91;
                }
                if (
                    py_header(list)->refcount != 1
                    || py_header(tuple)->refcount != 1
                    || py_header(set)->refcount != 1
                    || py_header(text)->refcount != 1
                ) {
                    py_cpy_decref(cpython_value);
                    py_decref(root);
                    return 92;
                }
                PyObject *roundtrip = py_cpy_to_pcc_obj(cpython_value);
                int exact = roundtrip != NULL && py_obj_eq(root, roundtrip) != 0;
                if (roundtrip != NULL) py_decref(roundtrip);
                py_cpy_decref(cpython_value);
                py_decref(root);
                if (!exact) return 93;
                printf("containers:ok\n");
                return 0;
            }

            static int run_stealing_failure_cleanup(void) {
                void *helper = py_cpy_import("bridge_callback_helper");
                void *factory = helper != NULL
                    ? py_cpy_getattr(helper, "make_token")
                    : NULL;
                void *reader = helper != NULL
                    ? py_cpy_getattr(helper, "destroyed_token_count")
                    : NULL;
                if (helper == NULL || factory == NULL || reader == NULL) return 94;

                for (int variant = 0; variant < 4; variant++) {
                    void *token = py_cpy_call_noargs(factory);
                    if (token == NULL) return 95;
                    void *argv[1] = {token};
                    void *result = NULL;
                    if (variant == 0) {
                        result = py_cpy_call_argv(NULL, 1, argv);
                    } else if (variant == 1) {
                        result = py_cpy_call_kw(NULL, 1, argv, 0, NULL, NULL);
                    } else if (variant == 2) {
                        result = py_cpy_call_kwdict(NULL, 1, argv, NULL);
                    } else {
                        result = py_cpy_call_kwdict_plus(
                            NULL, 1, argv, 0, NULL, NULL, NULL
                        );
                    }
                    if (result != NULL) {
                        py_cpy_decref(result);
                        return 96;
                    }
                }

                void *count_object = py_cpy_call_noargs(reader);
                int64_t count = count_object != NULL
                    ? py_cpy_to_i64(count_object)
                    : -1;
                if (count_object != NULL) py_cpy_decref(count_object);
                py_cpy_decref(reader);
                py_cpy_decref(factory);
                py_cpy_decref(helper);
                if (count != 4) return 97;
                printf("steal-cleanup:%lld\n", (long long)count);
                return 0;
            }

            static int run_alias_cycle_conversion(void) {
                PyObject *root = py_dict_new();
                PyObject *shared = py_list_new(1);
                PyObject *cycle = py_list_new(1);
                PyObject *left_key = py_str_new("left", 4);
                PyObject *right_key = py_str_new("right", 5);
                PyObject *cycle_key = py_str_new("cycle", 5);
                PyObject *item = py_int_from_i64(17);
                if (
                    root == NULL || shared == NULL || cycle == NULL
                    || left_key == NULL || right_key == NULL || cycle_key == NULL
                    || item == NULL
                ) return 98;
                py_list_append(shared, item);
                py_list_append(cycle, cycle);
                py_dict_set(root, left_key, shared);
                py_dict_set(root, right_key, shared);
                py_dict_set(root, cycle_key, cycle);
                py_decref(item);
                py_decref(left_key);
                py_decref(right_key);
                py_decref(cycle_key);
                py_decref(shared);
                py_decref(cycle);

                void *cpython_root = py_cpy_from_pcc_obj(root);
                void *helper = py_cpy_import("bridge_callback_helper");
                void *checker = helper != NULL
                    ? py_cpy_getattr(helper, "alias_cycle_ok")
                    : NULL;
                if (cpython_root == NULL || helper == NULL || checker == NULL) {
                    if (cpython_root != NULL) py_cpy_decref(cpython_root);
                    if (checker != NULL) py_cpy_decref(checker);
                    if (helper != NULL) py_cpy_decref(helper);
                    py_decref(root);
                    return 99;
                }
                void *result = py_cpy_call1(checker, cpython_root);
                int matches = result != NULL && py_cpy_truthy(result) > 0;
                if (result != NULL) py_cpy_decref(result);
                py_cpy_decref(checker);
                py_cpy_decref(helper);
                py_cpy_decref(cpython_root);
                py_decref(root);
                if (!matches) return 100;
                printf("alias-cycle:ok\n");
                return 0;
            }

            static int run_container_failure_cleanup(void) {
                PyObject *dictionary = py_dict_new();
                PyObject *key = py_tuple_new(1);
                PyObject *unhashable = py_list_new(1);
                PyObject *value = py_str_new("value", 5);
                PyObject *item = py_int_from_i64(5);
                if (
                    dictionary == NULL || key == NULL || unhashable == NULL
                    || value == NULL || item == NULL
                ) return 101;
                py_list_append(unhashable, item);
                py_tuple_set_item(key, 0, unhashable);
                py_dict_set(dictionary, key, value);
                py_decref(item);
                py_decref(key);
                py_decref(unhashable);
                py_decref(value);

                void *converted = py_cpy_from_pcc_obj(dictionary);
                if (converted != NULL) {
                    py_cpy_decref(converted);
                    py_decref(dictionary);
                    return 102;
                }
                if (
                    py_header(key)->refcount != 1
                    || py_header(unhashable)->refcount != 1
                    || py_header(value)->refcount != 1
                ) {
                    py_decref(dictionary);
                    return 103;
                }
                py_decref(dictionary);
                if (py_cpy_main_exitcode() != 1) return 104;

                PyObject *cycle_tuple = py_tuple_new(1);
                PyObject *cycle_list = py_list_new(1);
                if (cycle_tuple == NULL || cycle_list == NULL) return 105;
                py_tuple_set_item(cycle_tuple, 0, cycle_list);
                py_list_append(cycle_list, cycle_tuple);
                py_decref(cycle_list);
                void *cycle_result = py_cpy_from_pcc_obj(cycle_tuple);
                if (cycle_result != NULL) {
                    py_cpy_decref(cycle_result);
                    py_decref(cycle_tuple);
                    return 106;
                }
                if (
                    py_header(cycle_tuple)->refcount != 2
                    || py_header(cycle_list)->refcount != 1
                ) {
                    py_decref(cycle_tuple);
                    return 107;
                }
                py_decref(cycle_tuple);
                printf("failure-cleanup:ok\n");
                return 0;
            }

            static int run_backend4_rooted_conversion(void) {
                int64_t roots_before = pcc_gc_scheduler_root_count();
                PyObject *root = py_list_new(4);
                if (root == NULL) return 120;
                void *root_handle =
                    pcc_gc_scheduler_root_register_handle(&root);
                if (root_handle == NULL) return 121;

                PyObject *dictionary = py_dict_new();
                if (dictionary == NULL) return 122;
                void *dictionary_handle =
                    pcc_gc_scheduler_root_register_handle(&dictionary);
                if (dictionary_handle == NULL) return 123;

                PyObject *set = py_set_new();
                if (set == NULL) return 124;
                void *set_handle =
                    pcc_gc_scheduler_root_register_handle(&set);
                if (set_handle == NULL) return 125;

                PyObject *key = py_str_new("n", 1);
                if (key == NULL) return 126;
                void *key_handle =
                    pcc_gc_scheduler_root_register_handle(&key);
                if (key_handle == NULL) return 127;
                if (pcc_gc_scheduler_root_count() != roots_before + 4) {
                    return 136;
                }

                PyObject *five = py_int_from_i64(5);
                PyObject *seven = py_int_from_i64(7);
                if (five == NULL || seven == NULL) return 128;
                py_dict_set(dictionary, key, five);
                py_set_add(set, seven);
                py_list_append(root, dictionary);
                py_list_append(root, dictionary);
                py_list_append(root, set);
                py_list_append(root, root);
                if (py_err_occurred()) return 129;
                py_decref(five);
                py_decref(seven);

                (void)pcc_gc_collect(0);
                void *cpython_root = py_cpy_from_pcc_obj(root);
                void *helper = py_cpy_import("bridge_callback_helper");
                void *checker = helper != NULL
                    ? py_cpy_getattr(helper, "backend4_graph_ok")
                    : NULL;
                void *result = checker != NULL && cpython_root != NULL
                    ? py_cpy_call1(checker, cpython_root)
                    : NULL;
                int matches = result != NULL && py_cpy_truthy(result) > 0;
                if (result != NULL) py_cpy_decref(result);
                if (checker != NULL) py_cpy_decref(checker);
                if (helper != NULL) py_cpy_decref(helper);
                if (cpython_root != NULL) py_cpy_decref(cpython_root);

                pcc_gc_scheduler_root_unregister_handle(key_handle);
                pcc_gc_scheduler_root_unregister_handle(set_handle);
                pcc_gc_scheduler_root_unregister_handle(dictionary_handle);
                pcc_gc_scheduler_root_unregister_handle(root_handle);
                py_decref(key);
                py_decref(set);
                py_decref(dictionary);
                py_decref(root);
                if (!matches) return 135;
                if (pcc_gc_scheduler_root_count() != roots_before) return 137;
                printf("backend4-roots:ok\n");
                return 0;
            }

            static int run_bigint_roundtrip(
                const char *decimal,
                const char *label
            ) {
                PyObject *original = py_int_from_cstr(decimal, 10);
                if (original == NULL) return 40;
                void *cpython_value = py_cpy_from_pcc_obj(original);
                if (cpython_value == NULL) {
                    py_decref(original);
                    return 41;
                }
                PyObject *roundtrip = py_cpy_to_pcc_obj(cpython_value);
                if (roundtrip == NULL) {
                    py_cpy_decref(cpython_value);
                    py_decref(original);
                    return 42;
                }
                int overflow = 0;
                (void)py_int_to_i64(roundtrip, &overflow);
                int exact = py_int_cmp(original, roundtrip) == 0;
                py_decref(roundtrip);
                py_cpy_decref(cpython_value);
                py_decref(original);
                if (!overflow || !exact) return 43;
                printf("bigint:%s\n", label);
                return 0;
            }

            int main(void) {
                if (getenv("PCC_BRIDGE_GC4_PROBE") != NULL) {
                    return run_backend4_rooted_conversion();
                }
                int status = run_concurrent_bridge_calls();
                if (status != 0) return status;
                status = run_reentrant_callback();
                if (status != 0) return status;
                status = run_callback_error_translation();
                if (status != 0) return status;
                status = run_pcc_callback_error_propagation();
                if (status != 0) return status;
                status = run_cpython_owned_callback_error();
                if (status != 0) return status;
                status = run_caught_then_second_callback();
                if (status != 0) return status;
                status = run_caught_then_replacement_error();
                if (status != 0) return status;
                status = run_pending_error_thread_cleanup();
                if (status != 0) return status;
                status = run_container_roundtrip();
                if (status != 0) return status;
                status = run_stealing_failure_cleanup();
                if (status != 0) return status;
                status = run_alias_cycle_conversion();
                if (status != 0) return status;
                status = run_bigint_roundtrip(
                    "1267650600228229401496703205376", "+ok"
                );
                if (status != 0) return status;
                status = run_bigint_roundtrip(
                    "-1267650600228229401496703205376", "-ok"
                );
                if (status != 0) return status;
                return run_container_failure_cleanup();
            }
            """
        ).lstrip(),
        encoding="utf-8",
    )

    cppflags = shlex.split(os.environ.get("CPPFLAGS", ""))
    if not sys.platform.startswith("darwin"):
        cppflags.append("-D_DEFAULT_SOURCE")
    cflags = shlex.split(
        os.environ.get("CFLAGS", "-O2 -fPIC -Wall -Wextra -std=c11")
    )
    isolation_flags = pipeline._libpython_capi_isolation_link_flags(
        str(libpython_variant_archive),
        True,
    )
    assert isolation_flags, "libpython link must isolate pcc's public C-API"

    executable = tmp_path / "libpython_bridge_concurrency"
    _run(
        [
            *_command_from_env("CC", "cc"),
            *cppflags,
            *cflags,
            "-I",
            str(RUNTIME_ROOT / "include"),
            "-I",
            str(RUNTIME_ROOT / "src"),
            str(harness),
            str(libpython_variant_archive),
            *isolation_flags,
            *_python_config_flags("--ldflags", "--embed"),
            "-pthread",
            "-lm",
            "-o",
            str(executable),
        ],
        timeout=30,
    )

    def assert_bridge_output(result: subprocess.CompletedProcess[str]) -> None:
        lines = result.stdout.splitlines()
        assert lines[:10] == [
            "ok:1024",
            "reentrant:321",
            "callback:pcc-error",
            "callback:empty-error",
            "callback:cpy-error-result",
            "callback:pcc-error-result",
            "callback:pcc-propagated",
            "callback:cpy-thread",
            "callback:caught-then-second",
            "callback:replacement-error",
        ]
        assert lines[10].startswith("pending-cleanup:")
        assert lines[11:] == [
            "containers:ok",
            "steal-cleanup:4",
            "alias-cycle:ok",
            "bigint:+ok",
            "bigint:-ok",
            "failure-cleanup:ok",
        ]
        assert "TypeError" in result.stderr
        assert "unhashable type" in result.stderr

    result = _run([str(executable)], cwd=tmp_path, timeout=20)
    assert_bridge_output(result)
    backend4_env = os.environ.copy()
    backend4_env["PCC_GC_BACKEND"] = "4"
    backend4_env["PCC_BRIDGE_GC4_PROBE"] = "1"
    backend4_result = _run(
        [str(executable)], cwd=tmp_path, timeout=20, env=backend4_env
    )
    assert backend4_result.stdout == "backend4-roots:ok\n"
    assert backend4_result.stderr == ""


def test_libpython_bridge_dynamic_failure_arguments_preserve_error_and_ownership(
    tmp_path: Path,
    libpython_variant_archive: Path,
) -> None:
    """Dynamic argument failures preserve errors and stealing contracts."""
    from pcc.frontends.python import pipeline

    (tmp_path / "bridge_dynamic_failure_helper.py").write_text(
        "target_calls = 0\n"
        "destroyed_tokens = 0\n"
        "\n"
        "class WorkerError(Exception):\n"
        "    pass\n"
        "\n"
        "def fail_worker():\n"
        "    raise WorkerError('worker failure')\n"
        "\n"
        "def target(*args, **kwargs):\n"
        "    global target_calls\n"
        "    target_calls += 1\n"
        "    return len(args) + len(kwargs)\n"
        "\n"
        "def target_call_count():\n"
        "    return target_calls\n"
        "\n"
        "def identity(value):\n"
        "    return value\n"
        "\n"
        "class Token:\n"
        "    def __del__(self):\n"
        "        global destroyed_tokens\n"
        "        destroyed_tokens += 1\n"
        "\n"
        "def make_token():\n"
        "    return Token()\n"
        "\n"
        "def destroyed_token_count():\n"
        "    return destroyed_tokens\n",
        encoding="utf-8",
    )

    harness = tmp_path / "libpython_bridge_dynamic_failure.c"
    harness.write_text(
        textwrap.dedent(
            r"""
            #include "py_runtime.h"

            #include <stdint.h>
            #include <stdio.h>

            static int read_i64(void *callable, int64_t *value) {
                void *result = py_cpy_call_noargs(callable);
                if (result == NULL) return -1;
                *value = py_cpy_to_i64(result);
                py_cpy_decref(result);
                return 0;
            }

            static int expect_worker_error(const char *label) {
                if (py_cpy_main_exitcode() != 1) return -1;
                printf("preserved:%s\n", label);
                return 0;
            }

            static int expect_no_target_call(void *target_call_count) {
                int64_t count = -1;
                if (read_i64(target_call_count, &count) != 0) return -1;
                return count == 0 ? 0 : -2;
            }

            static int run_null_kw_value(
                void *fail_worker,
                void *target,
                void *target_call_count
            ) {
                void *missing = py_cpy_call_noargs(fail_worker);
                if (missing != NULL) {
                    py_cpy_decref(missing);
                    return 10;
                }
                const char *names[1] = {"value"};
                void *values[1] = {missing};
                void *result = py_cpy_call_kw(
                    target, 0, NULL, 1, names, values
                );
                if (result != NULL) {
                    py_cpy_decref(result);
                    return 11;
                }
                if (expect_worker_error("call_kw") != 0) return 12;
                if (expect_no_target_call(target_call_count) != 0) return 13;
                return 0;
            }

            static int run_null_mappings(
                void *fail_worker,
                void *target,
                void *target_call_count
            ) {
                void *missing = py_cpy_call_noargs(fail_worker);
                if (missing != NULL) {
                    py_cpy_decref(missing);
                    return 20;
                }
                void *result = py_cpy_call_kwdict(target, 0, NULL, NULL);
                if (result != NULL) {
                    py_cpy_decref(result);
                    return 21;
                }
                if (expect_worker_error("call_kwdict") != 0) return 22;
                if (expect_no_target_call(target_call_count) != 0) return 23;

                missing = py_cpy_call_noargs(fail_worker);
                if (missing != NULL) {
                    py_cpy_decref(missing);
                    return 24;
                }
                result = py_cpy_call_kwdict_plus(
                    target, 0, NULL, 0, NULL, NULL, NULL
                );
                if (result != NULL) {
                    py_cpy_decref(result);
                    return 25;
                }
                if (expect_worker_error("call_kwdict_plus") != 0) return 26;
                if (expect_no_target_call(target_call_count) != 0) return 27;

                PyObject *args = py_list_new(0);
                if (args == NULL) return 28;
                missing = py_cpy_call_noargs(fail_worker);
                if (missing != NULL) {
                    py_cpy_decref(missing);
                    py_decref(args);
                    return 29;
                }
                result = py_cpy_call_list_kwdict(target, args, NULL);
                py_decref(args);
                if (result != NULL) {
                    py_cpy_decref(result);
                    return 30;
                }
                if (expect_worker_error("call_list_kwdict") != 0) return 31;
                if (expect_no_target_call(target_call_count) != 0) return 32;
                return 0;
            }

            static int run_ignored_failure_then_valid_list(
                void *fail_worker,
                void *identity
            ) {
                void *ignored = py_cpy_call_noargs(fail_worker);
                if (ignored != NULL) {
                    py_cpy_decref(ignored);
                    return 40;
                }

                PyObject *args = py_list_new(1);
                PyObject *value = py_int_from_i64(41);
                if (args == NULL || value == NULL) {
                    py_decref(args);
                    py_decref(value);
                    return 41;
                }
                py_list_append(args, value);
                py_decref(value);
                void *result = py_cpy_call_list(identity, args);
                py_decref(args);
                if (result == NULL) return 42;
                int64_t converted = py_cpy_to_i64(result);
                py_cpy_decref(result);
                if (converted != 41) return 43;
                if (py_cpy_main_exitcode() != 0) return 44;
                printf("call-list-recovered:%lld\n", (long long)converted);
                return 0;
            }

            static int run_null_owned_argv(
                void *fail_worker,
                void *target,
                void *target_call_count,
                void *make_token,
                void *destroyed_token_count
            ) {
                void *first = py_cpy_call_noargs(make_token);
                void *last = py_cpy_call_noargs(make_token);
                if (first == NULL || last == NULL) {
                    if (first != NULL) py_cpy_decref(first);
                    if (last != NULL) py_cpy_decref(last);
                    return 50;
                }
                void *missing = py_cpy_call_noargs(fail_worker);
                if (missing != NULL) {
                    py_cpy_decref(first);
                    py_cpy_decref(missing);
                    py_cpy_decref(last);
                    return 51;
                }
                void *argv[3] = {first, missing, last};
                void *result = py_cpy_call_argv(target, 3, argv);
                if (result != NULL) {
                    py_cpy_decref(result);
                    return 52;
                }
                if (expect_worker_error("call_argv") != 0) return 53;

                int64_t destroyed = -1;
                if (read_i64(destroyed_token_count, &destroyed) != 0) {
                    return 54;
                }
                if (destroyed != 2) return 55;
                if (expect_no_target_call(target_call_count) != 0) return 56;
                printf("owned-null-cleanup:%lld\n", (long long)destroyed);
                return 0;
            }

            int main(void) {
                void *module = py_cpy_import("bridge_dynamic_failure_helper");
                if (module == NULL) return 60;
                void *fail_worker = py_cpy_getattr(module, "fail_worker");
                void *target = py_cpy_getattr(module, "target");
                void *target_call_count = py_cpy_getattr(
                    module, "target_call_count"
                );
                void *identity = py_cpy_getattr(module, "identity");
                void *make_token = py_cpy_getattr(module, "make_token");
                void *destroyed_token_count = py_cpy_getattr(
                    module, "destroyed_token_count"
                );
                if (
                    fail_worker == NULL
                    || target == NULL
                    || target_call_count == NULL
                    || identity == NULL
                    || make_token == NULL
                    || destroyed_token_count == NULL
                ) return 61;

                int status = run_null_kw_value(
                    fail_worker, target, target_call_count
                );
                if (status == 0) {
                    status = run_null_mappings(
                        fail_worker, target, target_call_count
                    );
                }
                if (status == 0) {
                    status = run_ignored_failure_then_valid_list(
                        fail_worker, identity
                    );
                }
                if (status == 0) {
                    status = run_null_owned_argv(
                        fail_worker,
                        target,
                        target_call_count,
                        make_token,
                        destroyed_token_count
                    );
                }

                py_cpy_decref(destroyed_token_count);
                py_cpy_decref(make_token);
                py_cpy_decref(identity);
                py_cpy_decref(target_call_count);
                py_cpy_decref(target);
                py_cpy_decref(fail_worker);
                py_cpy_decref(module);
                if (status != 0) {
                    fprintf(stderr, "dynamic failure probe status=%d\n", status);
                }
                return status;
            }
            """
        ).lstrip(),
        encoding="utf-8",
    )

    cppflags = shlex.split(os.environ.get("CPPFLAGS", ""))
    if not sys.platform.startswith("darwin"):
        cppflags.append("-D_DEFAULT_SOURCE")
    cflags = shlex.split(
        os.environ.get("CFLAGS", "-O2 -fPIC -Wall -Wextra -std=c11")
    )
    isolation_flags = pipeline._libpython_capi_isolation_link_flags(
        str(libpython_variant_archive),
        True,
    )
    assert isolation_flags, "libpython link must isolate pcc's public C-API"

    executable = tmp_path / "libpython_bridge_dynamic_failure"
    _run(
        [
            *_command_from_env("CC", "cc"),
            *cppflags,
            *cflags,
            "-I",
            str(RUNTIME_ROOT / "include"),
            str(harness),
            str(libpython_variant_archive),
            *isolation_flags,
            *_python_config_flags("--ldflags", "--embed"),
            "-pthread",
            "-lm",
            "-o",
            str(executable),
        ],
        timeout=30,
    )

    result = _run([str(executable)], cwd=tmp_path, timeout=20)
    assert result.stdout.splitlines() == [
        "preserved:call_kw",
        "preserved:call_kwdict",
        "preserved:call_kwdict_plus",
        "preserved:call_list_kwdict",
        "call-list-recovered:41",
        "preserved:call_argv",
        "owned-null-cleanup:2",
    ]
    assert result.stderr.count("WorkerError: worker failure") == 5
    assert "NULL keyword argument in libpython bridge" not in result.stderr
    assert "NULL keyword mapping in libpython bridge" not in result.stderr
    assert "NULL positional argument in libpython bridge" not in result.stderr
    assert "SystemError" not in result.stderr


def test_libpython_bridge_reverse_container_graph_survives_gc4_relocation(
    tmp_path: Path,
    libpython_variant_archive: Path,
) -> None:
    """Reverse conversion preserves topology while GC4 moves partial output."""
    from pcc.frontends.python import pipeline

    (tmp_path / "reverse_bridge_helper.py").write_text(
        "class MutatingIndex:\n"
        "    def __init__(self, owner):\n"
        "        self.owner = owner\n"
        "    def __hash__(self):\n"
        "        return 17\n"
        "    def __index__(self):\n"
        "        self.owner.clear()\n"
        "        return 3\n"
        "\n"
        "class CollectIndex:\n"
        "    def __init__(self, collect):\n"
        "        self.collect = collect\n"
        "    def __index__(self):\n"
        "        self.collect(0)\n"
        "        return 9\n"
        "\n"
        "def make_graph(collect):\n"
        "    shared = {1: 5}\n"
        "    shared[MutatingIndex(shared)] = 6\n"
        "    cycle = []\n"
        "    cycle.append(cycle)\n"
        "    holder = []\n"
        "    tuple_node = (holder,)\n"
        "    holder.append(tuple_node)\n"
        "    return [shared, shared, CollectIndex(collect), cycle, tuple_node, {7}]\n",
        encoding="utf-8",
    )
    harness = tmp_path / "libpython_bridge_reverse_graph.c"
    harness.write_text(
        textwrap.dedent(
            r"""
            #include "py_runtime.h"

            #include <stdint.h>
            #include <stdio.h>

            typedef struct ProbeSchedulerRootNode {
                PyObject **slot;
                struct ProbeSchedulerRootNode *next;
                struct ProbeSchedulerRootNode *previous;
            } ProbeSchedulerRootNode;

            extern void *pcc_gc_scheduler_root_head;
            extern int64_t pcc_gc_object_known_size(PyObject *object);
            extern int64_t pcc_gc_backend4_relocation_set_add(
                PyObject *object
            );

            static int64_t expected_root_baseline = 0;
            static int bridge_root_moved = 0;
            static int bridge_relocation_left_pending = 0;

            static void *collect_during_conversion(void *argument) {
                (void)argument;
                PyObject **slots[64] = {0};
                PyObject *old_values[64] = {0};
                int slot_count = 0;
                ProbeSchedulerRootNode *node =
                    (ProbeSchedulerRootNode *)pcc_gc_scheduler_root_head;
                while (node != NULL && slot_count < 64) {
                    if (node->slot != NULL && *node->slot != NULL) {
                        slots[slot_count] = node->slot;
                        old_values[slot_count] = *node->slot;
                        slot_count++;
                    }
                    node = node->next;
                }
                if (
                    pcc_gc_scheduler_root_count() <= expected_root_baseline
                    || slot_count == 0
                ) {
                    return py_cpy_from_i64(0);
                }
                pcc_gc_reset_relocation_set();
                int move_index = -1;
                int64_t move_size = 0;
                int candidates = 0;
                for (int i = 0; i < slot_count && candidates < 2; i++) {
                    int64_t size = pcc_gc_object_known_size(old_values[i]);
                    if (
                        size > 0
                        && pcc_gc_backend4_relocation_set_add(old_values[i]) == 1
                    ) {
                        if (move_index < 0) {
                            move_index = i;
                            move_size = size;
                        }
                        candidates++;
                    }
                }
                int64_t selected = pcc_gc_relocation_set_size();
                if (move_index >= 0 && candidates >= 2) {
                    PyObject *moved = pcc_gc_relocate_copy(
                        old_values[move_index], move_size
                    );
                    PyObject *current = pcc_gc_load_ptr(
                        NULL, slots[move_index]
                    );
                    bridge_root_moved =
                        moved != NULL
                        && moved != old_values[move_index]
                        && current == moved;
                    if (moved != NULL) pcc_gc_release(moved);
                }
                bridge_relocation_left_pending =
                    selected > 1 && pcc_gc_relocation_set_size() > 0;
                return py_cpy_from_i64(0);
            }

            int main(void) {
                int64_t roots_before = pcc_gc_scheduler_root_count();
                expected_root_baseline = roots_before;
                int64_t forwards_before = pcc_gc_telemetry(
                    PCC_GC_COUNTER_RELOCATION_FORWARDS
                );
                void *helper = py_cpy_import("reverse_bridge_helper");
                void *factory = helper != NULL
                    ? py_cpy_getattr(helper, "make_graph")
                    : NULL;
                void *collect = py_cpy_wrap_pcc_1arg(
                    (void *)collect_during_conversion
                );
                void *graph = factory != NULL && collect != NULL
                    ? py_cpy_call1(factory, collect)
                    : NULL;
                if (
                    helper == NULL || factory == NULL
                    || collect == NULL || graph == NULL
                ) return 140;

                PyObject *root = py_cpy_to_pcc_obj(graph);
                py_cpy_decref(graph);
                py_cpy_decref(collect);
                py_cpy_decref(factory);
                py_cpy_decref(helper);
                if (root == NULL) return 141;
                if (pcc_gc_scheduler_root_count() != roots_before) return 142;
                if (!bridge_root_moved) return 145;
                if (!bridge_relocation_left_pending) return 146;
                if (
                    pcc_gc_telemetry(PCC_GC_COUNTER_RELOCATION_FORWARDS)
                    <= forwards_before
                ) return 147;

                PyObject *shared_a = py_list_get(root, 0);
                PyObject *shared_b = py_list_get(root, 1);
                PyObject *trigger = py_list_get(root, 2);
                PyObject *cycle = py_list_get(root, 3);
                PyObject *tuple_node = py_list_get(root, 4);
                PyObject *set = py_list_get(root, 5);
                PyObject *cycle_back = cycle != NULL
                    ? py_list_get(cycle, 0)
                    : NULL;
                PyObject *holder = tuple_node != NULL
                    ? py_tuple_get(tuple_node, 0)
                    : NULL;
                PyObject *tuple_back = holder != NULL
                    ? py_list_get(holder, 0)
                    : NULL;
                PyObject *one = py_int_from_i64(1);
                PyObject *shared_value = shared_a != NULL
                    ? py_dict_get(shared_a, one)
                    : NULL;
                PyObject *three = py_int_from_i64(3);
                PyObject *mutated_value = shared_a != NULL
                    ? py_dict_get(shared_a, three)
                    : NULL;
                PyObject *seven = py_int_from_i64(7);
                int overflow = 0;
                int64_t trigger_value = trigger != NULL
                    ? py_int_to_i64(trigger, &overflow)
                    : -1;
                int value_overflow = 0;
                int64_t shared_integer = shared_value != NULL
                    ? py_int_to_i64(shared_value, &value_overflow)
                    : -1;
                int mutated_overflow = 0;
                int64_t mutated_integer = mutated_value != NULL
                    ? py_int_to_i64(mutated_value, &mutated_overflow)
                    : -1;
                int topology_ok =
                    shared_a != NULL && shared_a == shared_b
                    && shared_integer == 5 && !value_overflow
                    && mutated_integer == 6 && !mutated_overflow
                    && cycle != NULL && cycle == cycle_back
                    && tuple_node != NULL && tuple_node == tuple_back
                    && trigger_value == 9 && !overflow
                    && set != NULL && py_set_len(set) == 1
                    && py_set_contains(set, seven) != 0;

                py_decref(seven);
                py_decref(mutated_value);
                py_decref(three);
                py_decref(shared_value);
                py_decref(one);
                py_decref(tuple_back);
                py_decref(holder);
                py_decref(cycle_back);
                py_decref(set);
                py_decref(tuple_node);
                py_decref(cycle);
                py_decref(trigger);
                py_decref(shared_b);
                py_decref(shared_a);
                py_decref(root);
                if (!topology_ok) return 143;
                if (pcc_gc_scheduler_root_count() != roots_before) return 144;
                printf("reverse-gc4:ok\n");
                return 0;
            }
            """
        ).lstrip(),
        encoding="utf-8",
    )

    cppflags = shlex.split(os.environ.get("CPPFLAGS", ""))
    if not sys.platform.startswith("darwin"):
        cppflags.append("-D_DEFAULT_SOURCE")
    cflags = shlex.split(
        os.environ.get("CFLAGS", "-O2 -fPIC -Wall -Wextra -std=c11")
    )
    isolation_flags = pipeline._libpython_capi_isolation_link_flags(
        str(libpython_variant_archive),
        True,
    )
    executable = tmp_path / "libpython_bridge_reverse_graph"
    _run(
        [
            *_command_from_env("CC", "cc"),
            *cppflags,
            *cflags,
            "-I",
            str(RUNTIME_ROOT / "include"),
            str(harness),
            str(libpython_variant_archive),
            *isolation_flags,
            *_python_config_flags("--ldflags", "--embed"),
            "-pthread",
            "-lm",
            "-o",
            str(executable),
        ],
        timeout=30,
    )
    probe_env = os.environ.copy()
    probe_env["PCC_GC_BACKEND"] = "4"
    probe_env["PCC_GC_DEBT_THRESHOLD"] = "1"
    result = _run(
        [str(executable)], cwd=tmp_path, timeout=20, env=probe_env
    )
    assert result.stdout == "reverse-gc4:ok\n"
    assert result.stderr == ""


def test_libpython_bridge_sys_path_setup_cannot_exit_successfully(
    tmp_path: Path,
    libpython_variant_archive: Path,
) -> None:
    """A setup-time SystemExit must fail closed instead of returning zero."""
    from pcc.frontends.python import pipeline

    (tmp_path / "glob.py").write_text("raise SystemExit(0)\n", encoding="utf-8")
    harness = tmp_path / "libpython_seed_failure.c"
    harness.write_text(
        '#include "py_runtime.h"\n'
        "int main(void) {\n"
        '    void *value = py_cpy_import("builtins");\n'
        "    if (value != 0) py_cpy_decref(value);\n"
        "    return value == 0 ? 91 : 0;\n"
        "}\n",
        encoding="utf-8",
    )

    cppflags = shlex.split(os.environ.get("CPPFLAGS", ""))
    if not sys.platform.startswith("darwin"):
        cppflags.append("-D_DEFAULT_SOURCE")
    cflags = shlex.split(
        os.environ.get("CFLAGS", "-O2 -fPIC -Wall -Wextra -std=c11")
    )
    isolation_flags = pipeline._libpython_capi_isolation_link_flags(
        str(libpython_variant_archive),
        True,
    )
    executable = tmp_path / "libpython_seed_failure"
    _run(
        [
            *_command_from_env("CC", "cc"),
            *cppflags,
            *cflags,
            "-I",
            str(RUNTIME_ROOT / "include"),
            str(harness),
            str(libpython_variant_archive),
            *isolation_flags,
            *_python_config_flags("--ldflags", "--embed"),
            "-pthread",
            "-lm",
            "-o",
            str(executable),
        ],
        timeout=30,
    )

    run_env = dict(os.environ)
    run_env["PYTHONPATH"] = str(tmp_path)
    result = subprocess.run(
        [str(executable)],
        cwd=str(tmp_path),
        env=run_env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode != 0
    assert "libpython bridge sys.path setup failed" in result.stderr
