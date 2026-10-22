"""Execute real kernel threads and per-thread TLS through the owned ports.

PCC_PLATFORM_THREADED_RUNTIME must name a WITH_THREADS=1 archive built from
the same source with pcc.frontends.python.owned_runtime_build. No pthread library
or C compiler is permitted by this test.
"""

import os
from pathlib import Path

import pytest

from tests.integration.test_owned_platform_native import compile_run, native_compiler

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("gc", ["0", "1", "2", "3", "4"])
def test_owned_threads_tls_condition_and_join(native_compiler, tmp_path, gc):
    runtime = os.environ.get("PCC_PLATFORM_THREADED_RUNTIME", "")
    if not runtime or not Path(runtime).is_file():
        pytest.fail("set PCC_PLATFORM_THREADED_RUNTIME to the owned threaded archive")
    compile_run(native_compiler, tmp_path, r"""
        #include <pthread.h>
        #include <time.h>
        #include <string.h>
        extern int sched_yield(void);
        extern long long pcc_platform_wall_time_us(void);
        static pthread_mutex_t mutex = PTHREAD_MUTEX_INITIALIZER;
        static pthread_cond_t condition = PTHREAD_COND_INITIALIZER;
        static _Thread_local long long local_value = 13;
        static int entered;
        static int ready;
        static void *worker(void *argument) {
            if (local_value != 13) return (void *)99;
            local_value = (long long)argument;
            pthread_mutex_lock(&mutex);
            entered = 1;
            while (!ready) {
                if (pthread_cond_wait(&condition, &mutex) != 0) return (void *)98;
            }
            pthread_mutex_unlock(&mutex);
            return (void *)local_value;
        }
        int main(void) {
            pthread_t thread;
            void *result = 0;
            if (sizeof(thread) != sizeof(void *)) return 8;
            if (pthread_mutex_init(&mutex, 0) != 0) return 1;
            if (pthread_cond_init(&condition, 0) != 0) return 2;
            if (pthread_create(&thread, 0, worker, (void *)42) != 0) return 3;
            for (;;) {
                pthread_mutex_lock(&mutex);
                if (entered) break;
                pthread_mutex_unlock(&mutex);
                sched_yield();
            }
            ready = 1;
            if (pthread_cond_signal(&condition) != 0) return 4;
            pthread_mutex_unlock(&mutex);
            if (pthread_join(thread, &result) != 0) return 5;
            if ((long long)result != 42) return 6;
            if (local_value != 13) return 7;
            struct timespec deadline;
            memset(&deadline, 0x7f, sizeof(deadline));
            deadline.tv_sec = pcc_platform_wall_time_us() / 1000000 - 1;
            deadline.tv_nsec = 0;
            pthread_mutex_lock(&mutex);
            if (pthread_cond_timedwait(&condition, &mutex, &deadline) != 110) return 9;
            pthread_mutex_unlock(&mutex);
            if (pthread_mutex_destroy(&mutex) || pthread_cond_destroy(&condition)) return 10;
            return 0;
        }
    """, suffix=".c", gc=gc, extra_env={"PCC_WITH_THREADS": "1", "PCC_RUNTIME_ARCHIVE": str(Path(runtime).resolve())})
