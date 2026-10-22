"""A carrier blocked in the vthread IO wait runs exited: a stop does not wait for it.

The kqueue/epoll wait in pcc_io_waitset_wait_block can block forever (an
infinite poll_io with only quiet fds), but it ran in the running state, so a
stop-the-world waited for that carrier until some fd became ready. With the
GC1/GC2 collector thread seeding a cycle, the vthread IO waitset probe of
gc_production_contract deadlocked intermittently (collector waiting for the
carrier, the main thread parked for the stop, the carrier in kevent). Like
condition waits, joins and time.sleep, the kernel wait now runs exited.

The probe blocks one carrier indefinitely, stops and resumes the world from
the main thread, then wakes the carrier with a second registration.
"""

import subprocess
from pathlib import Path

from _gc_substrate_common import _compile_runtime_probe


PROBE = r'''
    #include "py_runtime.h"
    #include "py_internal.h"

    #include <poll.h>
    #include <stdint.h>
    #include <unistd.h>

    typedef struct {
        int64_t result;
    } Waiter;

    static void *wait_forever(void *opaque) {
        Waiter *waiter = (Waiter *)opaque;
        waiter->result = py_virtual_thread_poll_io(-1);
        return NULL;
    }

    int main(void) {
        if (py_virtual_thread_io_backend() == PCC_VTHREAD_IO_BACKEND_POLL) {
            /* The poll backend never blocks without a deadline. */
            return 0;
        }
        int quiet[2];
        int wake[2];
        if (pipe(quiet) != 0 || pipe(wake) != 0) return 2;
        PyObject *first = py_virtual_thread_new(py_None);
        PyObject *second = py_virtual_thread_new(py_None);
        if (first == NULL || second == NULL) return 3;
        if (py_virtual_thread_block_on_fd(first, quiet[0], POLLIN, -1) != 0) return 4;

        Waiter waiter = {-99};
        PccThreadHandle *thread = NULL;
        if (pcc_thread_start(&thread, wait_forever, &waiter) != 0) return 5;
        int spins = 0;
        while (py_virtual_thread_io_wait_active() != 1) {
            if (++spins > 5000) return 6;
            (void)poll(NULL, 0, 1);
        }
        /* The carrier sits in the kernel wait: the stop must not wait for it. */
        if (pcc_stop_the_world() != 0) return 7;
        if (pcc_resume_world() != 0) return 8;
        /* A second registration interrupts the infinite wait. */
        if (py_virtual_thread_block_on_fd(second, wake[0], POLLIN, -1) != 0) return 9;
        if (pcc_thread_join(thread, NULL) != 0) return 10;
        if (waiter.result != 0) return 11;
        return 0;
    }
'''


def test_stop_the_world_does_not_wait_for_a_carrier_blocked_in_io(tmp_path: Path) -> None:
    executable = _compile_runtime_probe(
        tmp_path,
        kind="pcc_python",
        threaded=True,
        stem="vthread_io_wait_exited",
        source_text=PROBE,
    )
    run = subprocess.run([str(executable)], capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, (run.returncode, run.stdout + run.stderr)
