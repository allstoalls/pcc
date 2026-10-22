"""Concurrent-mutation correctness tests for pcc's ``threading.Lock``.

Companion file to the layered investigation in
``docs/investigations/threading-lock-lost-update.md``. Each test isolates
one rung of the layered bisect that pinned the lost-update bug to the
Python codegen path (the C-level ``pcc_mutex_lock`` and PyObject-API
paths serialize correctly; the pcc-Python ``lock.acquire()`` path
loses ~50% of updates at high contention).

Tests in this file:

  * ``test_pthread_lock_disjoint_slot_writes_succeed`` — control: when
    each thread writes to its own list slot under one Lock, no
    cross-thread RMW happens, so the test passes regardless of the
    Lock-mutual-exclusion bug. This is the "the threads do run in
    parallel" baseline.
  * ``test_pthread_lock_low_iter_count_succeeds`` — control: at one
    iter per thread, the contention window is tiny and the bug rarely
    triggers. Asserts the happy path still works.
  * ``test_pthread_lock_list_append_under_contention`` — repro for the
    SIGABRT case: many threads doing ``list.append(1)`` under one
    Lock crashes the runtime under high contention. Marked xfail-strict
    until the T0 fix in ``docs/plans/python-types-codex-roadmap.md``
    lands.

The shared-slot ``counts[0] = counts[0] + 1`` repro lives in
``tests/test_threading_module_native.py::test_pthread_lock_serializes_shared_list_updates``
to keep all native-threading tests reachable via that file's existing
runtime-archive setup. This file adds the surrounding contract.
"""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest


def _compile_threaded(
    monkeypatch,
    runtime_archive: Path,
    src_path: Path,
    exe_path: Path,
) -> None:
    """Compile against an isolated ``PCC_WITH_THREADS=1`` archive."""
    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(runtime_archive))
    from pcc.frontends.python.pipeline import compile_python

    compile_python(
        str(src_path),
        str(exe_path),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )


def test_pthread_lock_disjoint_slot_writes_succeed(
    tmp_path, monkeypatch, threaded_pcc_runtime_archive
):
    """Control: 4 threads each write to their own slot under one Lock.

    Each thread mutates ``my_count[idx]`` (idx differs per thread), so
    even if Lock mutual exclusion is broken there is no cross-thread
    write to the same slot. This test must pass today and after the
    T0 fix — if it ever regresses, threads have stopped running at
    all (e.g. PCC_WITH_THREADS got silently disabled).
    """
    src = tmp_path / "lock_disjoint_slots.py"
    exe = tmp_path / "lock_disjoint_slots.out"
    src.write_text(textwrap.dedent("""
        from threading import Lock, Thread

        lock = Lock()
        my_count = [0, 0, 0, 0]

        def worker(idx: int) -> None:
            i = 0
            while i < 1000:
                lock.acquire()
                my_count[idx] = my_count[idx] + 1
                lock.release()
                i = i + 1

        def main() -> None:
            t0 = Thread(target=worker, args=(0,))
            t1 = Thread(target=worker, args=(1,))
            t2 = Thread(target=worker, args=(2,))
            t3 = Thread(target=worker, args=(3,))
            t0.start(); t1.start(); t2.start(); t3.start()
            t0.join(); t1.join(); t2.join(); t3.join()
            print(my_count[0], my_count[1], my_count[2], my_count[3])

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")

    _compile_threaded(monkeypatch, threaded_pcc_runtime_archive, src, exe)

    for _ in range(3):
        result = subprocess.run(
            [str(exe)], capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "1000 1000 1000 1000", (
            "per-thread slot writes lost updates — threads may not be "
            "running at all, or per-slot writes are racing the list "
            "metadata.\nstdout: " + result.stdout
        )


def test_pthread_lock_low_iter_count_succeeds(
    tmp_path, monkeypatch, threaded_pcc_runtime_archive
):
    """Control: 8 threads × 1 iter each. Contention window is too
    small to trigger the lost-update bug; the canonical case at
    1000 iters/thread does trigger it. Asserts the happy path still
    increments correctly when not stressed.

    Uses explicit Thread variables (rather than a list-of-Threads
    constructed via append-in-while-loop) because the latter pattern
    currently routes pcc's multi-file compile through libpython.
    """
    src = tmp_path / "lock_one_per_thread.py"
    exe = tmp_path / "lock_one_per_thread.out"
    src.write_text(textwrap.dedent("""
        from threading import Lock, Thread

        lock = Lock()
        counts = [0]

        def worker() -> None:
            lock.acquire()
            counts[0] = counts[0] + 1
            lock.release()

        def main() -> None:
            t0 = Thread(target=worker)
            t1 = Thread(target=worker)
            t2 = Thread(target=worker)
            t3 = Thread(target=worker)
            t4 = Thread(target=worker)
            t5 = Thread(target=worker)
            t6 = Thread(target=worker)
            t7 = Thread(target=worker)
            t0.start(); t1.start(); t2.start(); t3.start()
            t4.start(); t5.start(); t6.start(); t7.start()
            t0.join(); t1.join(); t2.join(); t3.join()
            t4.join(); t5.join(); t6.join(); t7.join()
            print(counts[0])

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")

    _compile_threaded(monkeypatch, threaded_pcc_runtime_archive, src, exe)

    for _ in range(3):
        result = subprocess.run(
            [str(exe)], capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "8", (
            "low-contention shared-slot increment lost updates; the "
            "bug threshold has dropped below 1 iter/thread, which "
            "means the Lock mutual-exclusion path is broken even "
            "under no contention.\nstdout: " + result.stdout
        )


def test_pthread_lock_list_append_under_contention(
    tmp_path, monkeypatch, threaded_pcc_runtime_archive
):
    """4 threads × 1000 iters of ``shared.append(1)`` under one Lock.

    Expected: final length 4000, no crash, deterministic.

    History: in the 2026-05-07 investigation this case SIGABRT'd
    reliably under contention. After the in-progress codegen edits
    on the local branch (``pcc/frontends/python/codegen/layer1.py``,
    locally modified), the case stopped crashing. This test asserts
    correctness directly so a future regression flips it back to
    failing.
    """
    src = tmp_path / "lock_list_append.py"
    exe = tmp_path / "lock_list_append.out"
    src.write_text(textwrap.dedent("""
        from threading import Lock, Thread

        lock = Lock()
        shared: list = []

        def worker() -> None:
            i = 0
            while i < 1000:
                lock.acquire()
                shared.append(1)
                lock.release()
                i = i + 1

        def main() -> None:
            t0 = Thread(target=worker)
            t1 = Thread(target=worker)
            t2 = Thread(target=worker)
            t3 = Thread(target=worker)
            t0.start(); t1.start(); t2.start(); t3.start()
            t0.join(); t1.join(); t2.join(); t3.join()
            print(len(shared))

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")

    _compile_threaded(monkeypatch, threaded_pcc_runtime_archive, src, exe)

    for _ in range(3):
        result = subprocess.run(
            [str(exe)], capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, (
            "list.append under contention crashed (likely SIGABRT). "
            f"returncode={result.returncode}\nstderr: {result.stderr}"
        )
        assert result.stdout.strip() == "4000"
