"""threading locks keep CPython 3.15's semantics on the threaded runtime.

Two programs, each compiled once and run under all five collectors; their
output must equal CPython's byte for byte:

* the API program checks every argument rule and error of Lock, RLock,
  Semaphore, BoundedSemaphore, Event and Thread.join -- nonblocking and
  timed acquires, the ``timeout`` conversion errors, release errors,
  ``locked()``, join before start and join with a timeout; receivers are
  typed (static lowering) and untyped (the runtime's bound methods);
* the contention program runs four threads per primitive: read-modify-write
  counters under Lock, ``with`` on an untyped Lock, nested RLock, timed and
  nonblocking acquires, a BoundedSemaphore bound, a Condition producer and
  consumer and an Event gate, collecting inside critical sections so
  stop-the-world requests meet threads blocked on each primitive.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.python.pcc1_gate import repo_root

BACKENDS = ("0", "1", "2", "3", "4")

API_PROGRAM = '''import threading


def report(label, error):
    print(label, type(error).__name__, error)


def lock_api():
    lock = threading.Lock()
    print("lock locked", lock.locked())
    print("lock acquire", lock.acquire())
    print("lock locked", lock.locked())
    print("lock nonblocking", lock.acquire(False))
    print("lock blocking=False", lock.acquire(blocking=False))
    print("lock timeout", lock.acquire(timeout=0.01))
    print("lock positional timeout", lock.acquire(True, 0.02))
    print("lock timeout=0", lock.acquire(timeout=0))
    try:
        lock.acquire(False, 1)
    except ValueError as error:
        report("lock nonblocking timeout", error)
    try:
        lock.acquire(timeout=-2)
    except ValueError as error:
        report("lock negative timeout", error)
    try:
        lock.acquire(timeout=float("nan"))
    except ValueError as error:
        report("lock nan timeout", error)
    try:
        lock.acquire(timeout=1e300)
    except OverflowError as error:
        report("lock huge timeout", error)
    try:
        lock.acquire(timeout=10 ** 30)
    except OverflowError as error:
        report("lock huge int timeout", error)
    try:
        lock.acquire(timeout="soon")
    except TypeError as error:
        report("lock str timeout", error)
    try:
        lock.acquire(timeout=None)
    except TypeError as error:
        report("lock None timeout", error)
    lock.release()
    print("lock locked", lock.locked())
    try:
        lock.release()
    except RuntimeError as error:
        report("lock release unlocked", error)
    print("lock timeout=-1", lock.acquire(timeout=-1))
    lock.release()
    with lock:
        print("lock with", lock.locked())
    print("lock after with", lock.locked())


def untyped_acquire(lock, blocking, timeout):
    return lock.acquire(blocking, timeout)


def untyped_release(lock):
    lock.release()


def untyped_lock_api():
    lock = threading.Lock()
    print("untyped acquire", untyped_acquire(lock, True, -1))
    print("untyped nonblocking", untyped_acquire(lock, False, -1))
    print("untyped timeout", untyped_acquire(lock, True, 0.01))
    try:
        untyped_acquire(lock, False, 1)
    except ValueError as error:
        report("untyped nonblocking timeout", error)
    untyped_release(lock)
    try:
        untyped_release(lock)
    except RuntimeError as error:
        report("untyped release unlocked", error)


def rlock_api():
    rlock = threading.RLock()
    print("rlock acquire", rlock.acquire(), rlock.acquire(False), rlock.acquire(timeout=0))
    print("rlock locked", rlock.locked())
    rlock.release()
    rlock.release()
    print("rlock still locked", rlock.locked())
    rlock.release()
    print("rlock locked", rlock.locked())
    try:
        rlock.release()
    except RuntimeError as error:
        report("rlock release unowned", error)
    try:
        rlock.acquire(False, 1)
    except ValueError as error:
        report("rlock nonblocking timeout", error)
    with rlock:
        with rlock:
            print("rlock nested with", rlock.locked())
    print("rlock after with", rlock.locked())


def semaphore_api():
    sem = threading.Semaphore(0)
    print("sem empty", sem.acquire(False), sem.acquire(timeout=0.01), sem.acquire(blocking=False))
    try:
        sem.acquire(False, 1)
    except ValueError as error:
        report("sem nonblocking timeout", error)
    sem.release(3)
    print("sem three", sem.acquire(), sem.acquire(), sem.acquire(), sem.acquire(False))
    try:
        sem.release(0)
    except ValueError as error:
        report("sem release(0)", error)
    try:
        threading.Semaphore(-1)
    except ValueError as error:
        report("sem negative", error)
    bounded = threading.BoundedSemaphore(2)
    print("bounded", bounded.acquire(), bounded.acquire(), bounded.acquire(False))
    bounded.release(2)
    try:
        bounded.release()
    except ValueError as error:
        report("bounded over-release", error)
    try:
        threading.BoundedSemaphore(-1)
    except ValueError as error:
        report("bounded negative", error)
    with bounded:
        print("bounded with", bounded.acquire(False), bounded.acquire(False))
        bounded.release()
    print("bounded after with", bounded.acquire(False), bounded.acquire(False), bounded.acquire(False))


def event_api():
    event = threading.Event()
    print("event", event.is_set(), event.wait(0.01), event.wait(timeout=0), event.wait(-1))
    try:
        event.wait("later")
    except TypeError as error:
        report("event str timeout", error)
    event.set()
    print("event set", event.wait(), event.wait(0), event.wait(5), event.wait("later"))
    event.clear()
    print("event cleared", event.is_set())


def wait_for(gate):
    gate.wait()


def thread_api():
    gate = threading.Event()
    worker = threading.Thread(target=wait_for, args=(gate,))
    try:
        worker.join()
    except RuntimeError as error:
        report("join before start", error)
    worker.start()
    try:
        worker.start()
    except RuntimeError as error:
        report("start twice", error)
    worker.join(0.01)
    print("alive after join(0.01)", worker.is_alive())
    worker.join(timeout=-1)
    print("alive after join(-1)", worker.is_alive())
    try:
        worker.join(float("nan"))
    except ValueError as error:
        report("join nan", error)
    gate.set()
    worker.join()
    print("alive after join", worker.is_alive())
    worker.join()
    worker.join(0)
    print("joined again")


def main():
    lock_api()
    untyped_lock_api()
    rlock_api()
    semaphore_api()
    event_api()
    thread_api()
    print("done")


main()
'''

CONTENTION_PROGRAM = '''import gc
import threading

ROUNDS = 3000


def counted(lock: threading.Lock, totals, index):
    for round_index in range(ROUNDS):
        lock.acquire()
        value = totals[0]
        if round_index % 997 == index:
            gc.collect()
        totals[0] = value + 1
        lock.release()


def counted_with(lock, totals, index):
    for round_index in range(ROUNDS):
        with lock:
            value = totals[1]
            if round_index % 991 == index:
                gc.collect()
            totals[1] = value + 1


def counted_rlock(rlock: threading.RLock, totals, index):
    for round_index in range(ROUNDS):
        rlock.acquire()
        with rlock:
            value = totals[2]
            totals[2] = value + 1
        rlock.release()


def counted_timeout(lock: threading.Lock, totals, index):
    taken = 0
    while taken < ROUNDS:
        if lock.acquire(timeout=0.5):
            value = totals[3]
            totals[3] = value + 1
            lock.release()
            taken += 1


def counted_nonblocking(lock: threading.Lock, totals, index):
    taken = 0
    while taken < 500:
        if lock.acquire(False):
            value = totals[4]
            totals[4] = value + 1
            lock.release()
            taken += 1


def run_workers(target, lock, totals):
    threads = [threading.Thread(target=target, args=(lock, totals, index)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()


def holder(lock: threading.Lock, held: threading.Event, go: threading.Event):
    lock.acquire()
    held.set()
    go.wait()
    lock.release()


def contended_timeouts():
    lock = threading.Lock()
    held = threading.Event()
    go = threading.Event()
    thread = threading.Thread(target=holder, args=(lock, held, go))
    thread.start()
    held.wait()
    print("held nonblocking", lock.acquire(False))
    print("held timeout", lock.acquire(timeout=0.05))
    go.set()
    print("released timeout", lock.acquire(timeout=10))
    lock.release()
    thread.join()


def bounded_section(sem: threading.BoundedSemaphore, state, state_lock: threading.Lock):
    for round_index in range(300):
        sem.acquire()
        with state_lock:
            state[0] += 1
            if state[0] > state[1]:
                state[1] = state[0]
        if round_index % 100 == 0:
            gc.collect()
        with state_lock:
            state[0] -= 1
        sem.release()


def producer(cond: threading.Condition, items, count):
    for value in range(count):
        with cond:
            items.append(value)
            cond.notify()


def consumer(cond: threading.Condition, items, out, count):
    got = 0
    while got < count:
        with cond:
            while not items:
                cond.wait()
            out.append(items.pop(0))
            got += 1


def gated(gate: threading.Event, lock: threading.Lock, totals):
    gate.wait()
    with lock:
        totals[5] += 1


def main():
    totals = [0, 0, 0, 0, 0, 0]
    run_workers(counted, threading.Lock(), totals)
    run_workers(counted_with, threading.Lock(), totals)
    run_workers(counted_rlock, threading.RLock(), totals)
    run_workers(counted_timeout, threading.Lock(), totals)
    run_workers(counted_nonblocking, threading.Lock(), totals)
    contended_timeouts()
    sem = threading.BoundedSemaphore(2)
    state = [0, 0]
    state_lock = threading.Lock()
    threads = [threading.Thread(target=bounded_section, args=(sem, state, state_lock)) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    print("semaphore bound", state[0], state[1] <= 2, state[1] >= 1)
    cond = threading.Condition()
    items = []
    out = []
    consumer_thread = threading.Thread(target=consumer, args=(cond, items, out, 500))
    producer_thread = threading.Thread(target=producer, args=(cond, items, 500))
    consumer_thread.start()
    producer_thread.start()
    producer_thread.join()
    consumer_thread.join()
    print("condition", len(out), sum(out), out == sorted(out))
    gate = threading.Event()
    gate_lock = threading.Lock()
    waiters = [threading.Thread(target=gated, args=(gate, gate_lock, totals)) for _ in range(4)]
    for thread in waiters:
        thread.start()
    gc.collect()
    gate.set()
    for thread in waiters:
        thread.join()
    print("totals", totals)
    with cond:
        print("condition timeout", cond.wait(0.01))


main()
'''

PROGRAMS = {"lock_api": API_PROGRAM, "lock_contention": CONTENTION_PROGRAM}


def _environment(backend=None):
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env.pop("PCC_WITH_THREADS", None)
    if backend is not None:
        env["PCC_GC_BACKEND"] = backend
    return env


def _cpython(source: str) -> str:
    run = subprocess.run([sys.executable, "-c", source], text=True, capture_output=True, timeout=120)
    assert run.returncode == 0, run.stderr
    return run.stdout


def _compile_program(tmp_path: Path, name: str) -> Path:
    src = tmp_path / (name + ".py")
    src.write_text(PROGRAMS[name], encoding="utf-8")
    exe = tmp_path / name
    build = subprocess.run(
        ["uv", "run", "pcc", "--backend", "self", "--python-libpython=off", str(src), "-o", str(exe)],
        cwd=repo_root(), text=True, capture_output=True, timeout=900, env=_environment(),
    )
    assert build.returncode == 0, build.stderr
    return exe


def _run_matches_cpython(exe: Path, name: str, backends) -> None:
    expected = _cpython(PROGRAMS[name])
    for backend in backends:
        run = subprocess.run([str(exe)], text=True, capture_output=True, timeout=300, env=_environment(backend))
        assert run.returncode == 0, (backend, run.stderr[-2000:])
        assert run.stdout == expected, (backend, run.stdout, expected)


@pytest.mark.integration
def test_lock_api_matches_cpython_under_every_collector(tmp_path: Path) -> None:
    _run_matches_cpython(_compile_program(tmp_path, "lock_api"), "lock_api", BACKENDS)


@pytest.mark.integration
def test_lock_contention_matches_cpython_under_non_moving_collectors(tmp_path: Path) -> None:
    exe = _compile_program(tmp_path, "lock_contention")
    _run_matches_cpython(exe, "lock_contention", ("0", "1", "2", "3"))


@pytest.mark.integration
def test_lock_contention_matches_cpython_under_gc4(tmp_path: Path) -> None:
    # Three runs: before the zpage fixes (payload spans registered under the
    # graph lock, no post-free address probes) about 7 runs in 8 lost updates
    # or died with [BAD_INCREF] here.
    exe = _compile_program(tmp_path, "lock_contention")
    _run_matches_cpython(exe, "lock_contention", ("4", "4", "4"))


def test_typed_lock_calls_pass_arguments_to_the_runtime(tmp_path: Path) -> None:
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "lock_calls.py"
    src.write_text(
        "import threading\n"
        "lock = threading.Lock()\n"
        "event = threading.Event()\n"
        "sem = threading.BoundedSemaphore(2)\n"
        "def calls(worker: threading.Thread):\n"
        "    lock.acquire(timeout=0.5)\n"
        "    event.wait(0.1)\n"
        "    sem.acquire(False)\n"
        "    sem.release(2)\n"
        "    worker.join(1.0)\n"
        "    return lock.locked()\n",
        encoding="utf-8",
    )
    out = tmp_path / "lock_calls.ll"
    compile_python(str(src), str(out), emit_llvm_only=True, libpython_mode="off")
    text = out.read_text(encoding="utf-8")
    # Event.wait and Semaphore.acquire may park, so ``calls`` lowers to a
    # resumable body; match the emitted calls wherever that body lives.
    called = set(re.findall(r"\bcall [^\n]*@(py_threading_[a-z_]+)\(", text))
    for runtime_name in (
        "py_threading_lock_acquire_args",
        "py_threading_event_wait_args",
        "py_threading_semaphore_acquire_args",
        "py_threading_semaphore_release_args",
        "py_threading_thread_join_args",
        "py_threading_lock_locked",
        "py_threading_semaphore_new_args",
    ):
        assert runtime_name in called, runtime_name
