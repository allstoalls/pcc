"""threading locks between virtual threads on several carriers.

A virtual thread that blocks on a lock parks on the lock's FIFO instead of
holding its carrier, and the release hands the lock to the oldest parked
waiter. Workers yield while holding Lock, RLock and BoundedSemaphore, so
parked holders and parked waiters interleave across three carriers:

* Lock and RLock read-modify-write counters stay exact;
* an RLock re-entered across a park belongs to the virtual thread, not to
  the carrier it ran on (carriers are shared, and a virtual thread resumes
  on any carrier), so a release on another carrier is the owner's release;
* a BoundedSemaphore admits at most two holders;
* an Event gate and a Condition producer/consumer wake parked waiters;
* nonblocking and timed acquires of a lock another virtual thread holds
  answer False, and a blocking acquire then parks until the release.

CPython has no virtual threads, so the expected output is the program's
own arithmetic. Each collector runs the compiled program.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.python.pcc1_gate import repo_root

BACKENDS = ("0", "1", "2", "3", "4")

PROGRAM = '''import threading
import pcc.virtual_thread as vt

lock = threading.Lock()
rlock = threading.RLock()
sem = threading.BoundedSemaphore(2)
state_lock = threading.Lock()
gate = threading.Event()
cond = threading.Condition()
held = threading.Event()
probed = threading.Event()
timed_lock = threading.Lock()
totals = [0, 0, 0]
inside = [0, 0]
queue = []
taken = []
probes = []


def lock_worker():
    for _ in range(40):
        lock.acquire()
        value = totals[0]
        vt.yield_now()
        totals[0] = value + 1
        lock.release()
    return 1


def rlock_worker():
    for _ in range(40):
        rlock.acquire()
        rlock.acquire()
        value = totals[1]
        vt.yield_now()
        totals[1] = value + 1
        rlock.release()
        vt.yield_now()
        rlock.release()
    return 2


def sem_worker():
    for _ in range(20):
        sem.acquire()
        state_lock.acquire()
        inside[0] += 1
        if inside[0] > inside[1]:
            inside[1] = inside[0]
        state_lock.release()
        vt.yield_now()
        state_lock.acquire()
        inside[0] -= 1
        state_lock.release()
        sem.release()
    return 3


def gate_worker():
    gate.wait()
    state_lock.acquire()
    totals[2] += 1
    state_lock.release()
    return 4
    vt.yield_now()


def opener():
    for _ in range(10):
        vt.yield_now()
    gate.set()
    return 5


def cond_consumer():
    got = 0
    while got < 20:
        cond.acquire()
        while not queue:
            cond.wait()
        taken.append(queue.pop(0))
        cond.release()
        got += 1
    return 6
    vt.yield_now()


def cond_producer():
    for value in range(20):
        cond.acquire()
        queue.append(value)
        cond.notify()
        cond.release()
        vt.yield_now()
    return 7


def timed_holder():
    timed_lock.acquire()
    held.set()
    probed.wait()
    vt.yield_now()
    timed_lock.release()
    return 8


def timed_prober():
    held.wait()
    probes.append(timed_lock.acquire(False))
    probes.append(timed_lock.acquire(timeout=0.01))
    probed.set()
    # A parking call's result is bound before it joins a larger expression:
    # an operand evaluated before the park cannot stay in SSA across it.
    reacquired = timed_lock.acquire()
    probes.append(reacquired)
    timed_lock.release()
    return 9
    vt.yield_now()


def main():
    workers = []
    for _ in range(3):
        workers.append(vt.spawn(lock_worker))
        workers.append(vt.spawn(rlock_worker))
        workers.append(vt.spawn(sem_worker))
        workers.append(vt.spawn(gate_worker))
    workers.append(vt.spawn(opener))
    workers.append(vt.spawn(cond_consumer))
    workers.append(vt.spawn(cond_producer))
    workers.append(vt.spawn(timed_holder))
    workers.append(vt.spawn(timed_prober))
    vt.run(3, 1000000)
    print(totals, inside[1] <= 2, inside[1] >= 1, inside[0])
    print(len(taken), sum(taken), taken == sorted(taken))
    print(probes)
    print([vt.result(worker) for worker in workers])
    print(lock.locked(), rlock.locked(), timed_lock.locked())


main()
'''

EXPECTED = (
    "[120, 120, 3] True True 0\n"
    "20 190 True\n"
    "[False, False, True]\n"
    "[1, 2, 3, 4, 1, 2, 3, 4, 1, 2, 3, 4, 5, 6, 7, 8, 9]\n"
    "False False False\n"
)


def _environment(backend=None):
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env.pop("PCC_WITH_THREADS", None)
    if backend is not None:
        env["PCC_GC_BACKEND"] = backend
    return env


@pytest.mark.integration
def test_virtual_threads_share_locks_across_carriers_under_every_collector(tmp_path: Path) -> None:
    src = tmp_path / "vthread_locks.py"
    src.write_text(PROGRAM, encoding="utf-8")
    exe = tmp_path / "vthread_locks"
    build = subprocess.run(
        ["uv", "run", "pcc", "--backend", "self", "--python-libpython=off", str(src), "-o", str(exe)],
        cwd=repo_root(), text=True, capture_output=True, timeout=900, env=_environment(),
    )
    assert build.returncode == 0, build.stderr
    for backend in BACKENDS:
        run = subprocess.run([str(exe)], text=True, capture_output=True, timeout=300, env=_environment(backend))
        assert run.returncode == 0, (backend, run.stderr[-2000:])
        assert run.stdout == EXPECTED, (backend, run.stdout)


def test_resumable_rlock_acquire_parks_the_virtual_thread(tmp_path: Path) -> None:
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "rlock_park.py"
    src.write_text(
        "import threading\n"
        "import pcc.virtual_thread as vt\n"
        "rlock = threading.RLock()\n"
        "def worker():\n"
        "    rlock.acquire()\n"
        "    vt.yield_now()\n"
        "    rlock.release()\n"
        "    return 1\n",
        encoding="utf-8",
    )
    out = tmp_path / "rlock_park.ll"
    compile_python(str(src), str(out), emit_llvm_only=True, libpython_mode="off")
    text = out.read_text(encoding="utf-8")
    assert re.search(r"\bcall [^\n]*@py_threading_rlock_acquire_vthread\(", text)
