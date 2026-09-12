/* Transitional subprocess timeout helper for the C and pcc-Python runtimes.
 *
 * The child gets its own process group so timeout cleanup reaches
 * grandchildren as well as the immediate command. Platform wait/signal
 * ownership is already routed to freestanding pcc-Python in the production
 * archive; spawn and argv construction remain to migrate.
 */

#include "py_internal.h"

#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <spawn.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

extern char **environ;

#define PCC_SUBPROCESS_TIMEOUT_RC (-124)
#define PCC_TIMEOUT_POLL_NS 10000000L
#define PCC_TIMEOUT_TERM_GRACE_MS 200

static void free_exec_argv(char **items, int64_t count) {
    if (items == NULL) return;
    for (int64_t i = 0; i < count; i++) free(items[i]);
    free(items);
}

static char **build_exec_argv(PyObject *argv, int64_t *count_out) {
    int64_t count = py_obj_len(argv);
    if (count <= 0) return NULL;
    char **items = (char **)calloc((size_t)count + 1, sizeof(char *));
    if (items == NULL) return NULL;

    for (int64_t i = 0; i < count; i++) {
        PyObject *index = py_int_from_i64(i);
        PyObject *item = py_obj_getitem(argv, index);
        py_decref(index);
        PyObject *text = py_obj_str(item);
        py_decref(item);
        if (text == NULL) {
            free_exec_argv(items, i);
            return NULL;
        }
        const char *raw = py_str_utf8(text);
        size_t size = raw != NULL ? strlen(raw) : 0;
        items[i] = (char *)malloc(size + 1);
        if (items[i] == NULL) {
            py_decref(text);
            free_exec_argv(items, i);
            return NULL;
        }
        if (size > 0) memcpy(items[i], raw, size);
        items[i][size] = '\0';
        py_decref(text);
    }
    items[count] = NULL;
    *count_out = count;
    return items;
}

static int64_t monotonic_millis(void) {
    int64_t now_us = pcc_runtime_monotonic_us();
    return now_us > 0 ? now_us / 1000 : -1;
}

#ifndef PCC_USE_FREESTANDING_PLATFORM_PROCESS
int64_t py_process_normalize_wait_status(int64_t raw_status) {
    if (raw_status == -1) return 127;
    int status = (int)raw_status;
    if (WIFEXITED(status)) return (int64_t)WEXITSTATUS(status);
    if (WIFSIGNALED(status)) return -(int64_t)WTERMSIG(status);
    return 127;
}
#endif

static int64_t runtime_waitpid(pid_t pid, int *status, int options) {
#ifdef PCC_USE_FREESTANDING_PLATFORM_PROCESS
    return pcc_platform_waitpid((int64_t)pid, (int32_t *)status,
                                (int64_t)options);
#else
    pid_t waited;
    do {
        waited = waitpid(pid, status, options);
    } while (waited < 0 && errno == EINTR);
    return (int64_t)waited;
#endif
}

static int64_t runtime_kill(pid_t pid, int signal_number) {
#ifdef PCC_USE_FREESTANDING_PLATFORM_PROCESS
    return pcc_platform_kill((int64_t)pid, (int64_t)signal_number);
#else
    return (int64_t)kill(pid, signal_number);
#endif
}

static int wait_for_exit(pid_t pid, int *status, int64_t deadline_ms) {
    for (;;) {
        int64_t waited = runtime_waitpid(pid, status, WNOHANG);
        if (waited == (int64_t)pid) return 1;
        if (waited < 0) return -1;
        int64_t now_ms = monotonic_millis();
        if (now_ms < 0 || now_ms >= deadline_ms) return 0;
        (void)pcc_runtime_sleep_ns(PCC_TIMEOUT_POLL_NS);
    }
}

static void terminate_process_group(pid_t pid, int *status) {
    /* POSIX_SPAWN_SETPGROUP made pid the process-group id. Keep the direct-pid
     * signal as a defensive fallback if a platform rejects group delivery. */
    if (runtime_kill(-pid, SIGTERM) != 0) runtime_kill(pid, SIGTERM);
    int64_t now_ms = monotonic_millis();
    int64_t deadline_ms = now_ms < 0 ? 0 : now_ms + PCC_TIMEOUT_TERM_GRACE_MS;
    int waited = wait_for_exit(pid, status, deadline_ms);
    if (waited == 1 || waited < 0) return;

    if (runtime_kill(-pid, SIGKILL) != 0) runtime_kill(pid, SIGKILL);
    (void)runtime_waitpid(pid, status, 0);
}

int64_t py_subprocess_run_timeout(
    PyObject *argv,
    int32_t capture_output,
    int64_t timeout_ms
) {
    if (timeout_ms <= 0) return 127;

    int64_t count = 0;
    char **items = build_exec_argv(argv, &count);
    if (items == NULL) return 127;

    posix_spawn_file_actions_t actions;
    posix_spawnattr_t attr;
    int actions_ready = posix_spawn_file_actions_init(&actions) == 0;
    int attr_ready = posix_spawnattr_init(&attr) == 0;
    if (!actions_ready || !attr_ready) {
        if (actions_ready) posix_spawn_file_actions_destroy(&actions);
        if (attr_ready) posix_spawnattr_destroy(&attr);
        free_exec_argv(items, count);
        return 127;
    }

    int setup_error = 0;
    if (capture_output != 0) {
        setup_error = posix_spawn_file_actions_addopen(
            &actions, STDOUT_FILENO, "/dev/null", O_WRONLY, 0
        );
        if (setup_error == 0) {
            setup_error = posix_spawn_file_actions_addopen(
                &actions, STDERR_FILENO, "/dev/null", O_WRONLY, 0
            );
        }
    }
    if (setup_error == 0) {
        setup_error = posix_spawnattr_setpgroup(&attr, 0);
    }
    if (setup_error == 0) {
        setup_error = posix_spawnattr_setflags(&attr, POSIX_SPAWN_SETPGROUP);
    }

    pid_t pid = -1;
    int spawn_error = setup_error;
    if (spawn_error == 0) {
#ifdef PCC_USE_FREESTANDING_PLATFORM_ENV
        char **spawn_env = pcc_platform_env_snapshot();
        if (spawn_env == NULL) {
            spawn_error = ENOMEM;
        } else {
            spawn_error = posix_spawnp(
                &pid, items[0], &actions, &attr, items, spawn_env
            );
            pcc_platform_env_snapshot_free(spawn_env);
        }
#else
        spawn_error = posix_spawnp(
            &pid, items[0], &actions, &attr, items, environ
        );
#endif
    }
    posix_spawn_file_actions_destroy(&actions);
    posix_spawnattr_destroy(&attr);
    free_exec_argv(items, count);
    if (spawn_error != 0) return 127;

    int64_t start_ms = monotonic_millis();
    if (start_ms < 0) {
        int status = 0;
        terminate_process_group(pid, &status);
        return 127;
    }
    int status = 0;
    int waited = wait_for_exit(pid, &status, start_ms + timeout_ms);
    if (waited == 1) return py_process_normalize_wait_status(status);
    if (waited < 0) return 127;

    terminate_process_group(pid, &status);
    return PCC_SUBPROCESS_TIMEOUT_RC;
}

int64_t pcc_worker_process_pool(PyObject *specs, int64_t width) {
    int64_t count = py_obj_len(specs);
    if (count <= 0) return 0;
    if (width < 1) width = 1;
    if (width > count) width = count;
    struct WorkerSlot { pid_t pid; int64_t index; };
    struct WorkerSlot *slots = calloc((size_t)width, sizeof(*slots));
    if (!slots) return INT64_C(4294967423);
    int64_t next_index = 0, live = 0, failure = 0;
    int status = 0;
    while (next_index < count || live > 0) {
        for (int64_t slot = 0; slot < width; slot++) {
            pid_t pid = slots[slot].pid;
            if (pid <= 0) continue;
            int64_t waited = runtime_waitpid(pid, &status, WNOHANG);
            if (waited == 0) continue;
            int64_t rc = waited == pid ? py_process_normalize_wait_status(status) : 127;
            runtime_kill(-pid, SIGKILL);
            slots[slot].pid = 0;
            live--;
            if (rc != 0) {
                failure = ((slots[slot].index + 1) << 32) | (rc & INT64_C(4294967295));
                break;
            }
        }
        if (failure) break;
        int64_t slot = 0;
        while (next_index < count && live < width) {
            while (slots[slot].pid != 0) slot++;
            PyObject *index = py_int_from_i64(next_index);
            PyObject *spec = py_obj_getitem(specs, index);
            py_decref(index);
            PyObject *zero = py_int_from_i64(0), *one = py_int_from_i64(1);
            PyObject *argv = py_obj_getitem(spec, zero), *env = py_obj_getitem(spec, one);
            py_decref(zero); py_decref(one);
            int64_t argc = 0, envc = 0;
            char **items = build_exec_argv(argv, &argc);
            char **envp = build_exec_argv(env, &envc);
            pid_t pid = -1;
            if (items && envp) {
                posix_spawnattr_t attr;
                if (posix_spawnattr_init(&attr) == 0) {
                    int rc = posix_spawnattr_setpgroup(&attr, 0);
                    if (rc == 0) rc = posix_spawnattr_setflags(&attr, POSIX_SPAWN_SETPGROUP);
                    if (rc == 0) rc = posix_spawnp(&pid, items[0], NULL, &attr, items, envp);
                    if (rc != 0) pid = -1;
                    posix_spawnattr_destroy(&attr);
                }
            }
            free_exec_argv(items, argc); free_exec_argv(envp, envc);
            py_decref(argv); py_decref(env); py_decref(spec);
            if (pid <= 0) {
                failure = ((next_index + 1) << 32) | 127;
                break;
            }
            slots[slot].pid = pid;
            slots[slot].index = next_index++;
            live++;
            slot++;
        }
        if (failure) break;
        if (live) pcc_runtime_sleep_ns(PCC_TIMEOUT_POLL_NS);
    }
    if (failure) {
        for (int64_t slot = 0; slot < width; slot++) {
            if (slots[slot].pid > 0) runtime_kill(-slots[slot].pid, SIGTERM);
        }
        pcc_runtime_sleep_ns(INT64_C(200000000));
        for (int64_t slot = 0; slot < width; slot++) {
            if (slots[slot].pid > 0) {
                runtime_kill(-slots[slot].pid, SIGKILL);
                runtime_waitpid(slots[slot].pid, &status, 0);
            }
        }
    }
    free(slots);
    return failure;
}
