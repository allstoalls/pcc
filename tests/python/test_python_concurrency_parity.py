"""CPython-parity concurrency tests, ported from ``Lib/test/lock_tests.py``.

Each test below is a small, self-contained Python program adapted from
CPython's reusable lock-test framework. The original suite uses
``Bunch`` / ``wait_threads_blocked`` / ``self.assertEqual`` and other
test infrastructure that pcc cannot compile; the ports preserve the
*observable behaviour* CPython asserts but use plain ``print``-and-grep
assertions and avoid pcc's currently-known frontend gaps:

  * no list comprehensions of Threads (hits
    ``threads[i].start()`` codegen bug — see
    ``docs/investigations/threading-list-index-start-failure.md``)
  * no ``nonlocal`` (use module-level mutable container)
  * no ``with lock:`` in the contended cases yet (the pcc shim has
    ``__enter__`` / ``__exit__`` but exception-edge cases are still
    open in D4 from the data-model gap roadmap)

CPython's contract for each pattern:

  * **acquire/release roundtrip** — ``Lock`` is reusable after release
  * **contended counter** — ``N`` threads each running ``K`` increments
    under one ``Lock`` produce ``counter == N * K`` exactly
    (this is the canonical free-threaded sanity check)
  * **RLock recursion count** — the same thread can re-acquire an
    ``RLock`` arbitrarily many times; ``release`` must be called the
    same number of times before another thread can acquire
  * **Event broadcast** — ``N`` threads waiting on ``Event.wait``
    all return after a single ``set`` from the main thread

Reference: CPython
``Lib/test/lock_tests.py::test_acquire_release``,
``::test_acquire_contended``, ``::test_recursion_count``,
``::test_set_and_clear``.
"""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

from tests.runtime_build_cache import cached_threaded_pcc_python_runtime


REPO_ROOT = Path(__file__).absolute().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"


def _build_threaded_runtime(tmp_path: Path) -> Path:
    del tmp_path
    return cached_threaded_pcc_python_runtime()


def _compile_threaded(monkeypatch, tmp_path: Path, src: Path, exe: Path) -> None:
    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    work_runtime = _build_threaded_runtime(tmp_path)
    from pcc.frontends.python.pipeline import compile_python

    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        runtime_archive=str(work_runtime / "libpy_runtime_pcc_py.a"),
    )


def _run(exe: Path, timeout: float = 30.0) -> str:
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=timeout,
    )
    assert result.returncode == 0, (
        f"{exe.name} exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result.stdout


def test_lock_acquire_release_roundtrip(tmp_path, monkeypatch):
    """Adapted from CPython ``test_acquire_release``: a Lock acquired
    and released once is reusable from the same thread."""
    src = tmp_path / "lock_acquire_release.py"
    exe = tmp_path / "lock_acquire_release.out"
    src.write_text(textwrap.dedent("""
        from threading import Lock

        def main() -> None:
            lock = Lock()
            print(lock.acquire())
            lock.release()
            print(lock.acquire())
            lock.release()
            print("DONE")

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")

    _compile_threaded(monkeypatch, tmp_path, src, exe)
    out = _run(exe).strip().splitlines()
    assert out == ["True", "True", "DONE"], (
        f"acquire/release roundtrip broken.\noutput: {out}"
    )


def test_lock_acquire_contended_counter(tmp_path, monkeypatch):
    """Adapted from CPython ``test_acquire_contended``: 4 threads each
    increment ``counter`` 1000 times under one ``Lock``. CPython
    guarantees ``counter == 4000``. This is the canonical
    free-threaded sanity check (PEP 703 reference behaviour)."""
    src = tmp_path / "lock_contended_counter.py"
    exe = tmp_path / "lock_contended_counter.out"
    src.write_text(textwrap.dedent("""
        from threading import Lock, Thread

        counts = [0]
        lock = Lock()

        def worker() -> None:
            i = 0
            while i < 1000:
                lock.acquire()
                counts[0] = counts[0] + 1
                lock.release()
                i = i + 1

        def main() -> None:
            t0 = Thread(target=worker)
            t1 = Thread(target=worker)
            t2 = Thread(target=worker)
            t3 = Thread(target=worker)
            t0.start(); t1.start(); t2.start(); t3.start()
            t0.join(); t1.join(); t2.join(); t3.join()
            print(counts[0])

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")

    _compile_threaded(monkeypatch, tmp_path, src, exe)
    # Run 3 times — non-determinism would manifest as inconsistent values.
    seen = []
    for _ in range(3):
        seen.append(_run(exe).strip())
    assert seen == ["4000", "4000", "4000"], (
        "contended counter under one Lock did not match CPython "
        f"contract (4000 each run). got: {seen}"
    )


def test_rlock_recursion_count_single_thread(tmp_path, monkeypatch):
    """Adapted from CPython ``test_recursion_count``: an ``RLock`` can
    be re-acquired by the same thread arbitrarily often, releases
    must match acquires."""
    src = tmp_path / "rlock_recursion.py"
    exe = tmp_path / "rlock_recursion.out"
    src.write_text(textwrap.dedent("""
        from threading import RLock

        def main() -> None:
            r = RLock()
            print(r.acquire())
            print(r.acquire())
            print(r.acquire())
            r.release()
            r.release()
            r.release()
            # After matched releases, RLock should be free for re-acquire.
            print(r.acquire())
            r.release()
            print("DONE")

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")

    _compile_threaded(monkeypatch, tmp_path, src, exe)
    out = _run(exe).strip().splitlines()
    assert out == ["True", "True", "True", "True", "DONE"], (
        f"RLock recursion broken.\noutput: {out}"
    )


def test_event_broadcast_releases_all_waiters(tmp_path, monkeypatch):
    """Adapted from CPython ``test_set_and_clear``: ``N`` threads wait
    on ``Event.wait``; one ``set`` from the main thread releases
    every waiter."""
    src = tmp_path / "event_broadcast.py"
    exe = tmp_path / "event_broadcast.out"
    src.write_text(textwrap.dedent("""
        from threading import Event, Thread

        ev = Event()
        results = [0, 0, 0, 0]

        def waiter(idx: int) -> None:
            ok = ev.wait()
            if ok:
                results[idx] = 1

        def main() -> None:
            t0 = Thread(target=waiter, args=(0,))
            t1 = Thread(target=waiter, args=(1,))
            t2 = Thread(target=waiter, args=(2,))
            t3 = Thread(target=waiter, args=(3,))
            t0.start(); t1.start(); t2.start(); t3.start()
            ev.set()
            t0.join(); t1.join(); t2.join(); t3.join()
            print(results[0] + results[1] + results[2] + results[3])

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")

    _compile_threaded(monkeypatch, tmp_path, src, exe)
    out = _run(exe).strip()
    assert out == "4", (
        f"Event.set did not wake all 4 waiters (CPython contract).\noutput: {out}"
    )


def test_disjoint_slot_writes_no_lock(tmp_path, monkeypatch):
    """No CPython-test analogue but a free-threaded sanity check: 4
    threads each writing only to their own slot in a shared list,
    no Lock at all. Free-threaded CPython (3.13t+) guarantees this
    works because the list has its own internal lock around
    setitem; pcc's runtime needs the same guarantee for correctness
    on workloads that partition state across threads."""
    src = tmp_path / "disjoint_slots.py"
    exe = tmp_path / "disjoint_slots.out"
    src.write_text(textwrap.dedent("""
        from threading import Thread

        slots = [0, 0, 0, 0]

        def worker(idx: int) -> None:
            i = 0
            while i < 1000:
                slots[idx] = slots[idx] + 1
                i = i + 1

        def main() -> None:
            t0 = Thread(target=worker, args=(0,))
            t1 = Thread(target=worker, args=(1,))
            t2 = Thread(target=worker, args=(2,))
            t3 = Thread(target=worker, args=(3,))
            t0.start(); t1.start(); t2.start(); t3.start()
            t0.join(); t1.join(); t2.join(); t3.join()
            print(slots[0], slots[1], slots[2], slots[3])

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")

    _compile_threaded(monkeypatch, tmp_path, src, exe)
    out = _run(exe).strip()
    assert out == "1000 1000 1000 1000", (
        "disjoint slot writes lost updates — pcc's free-threaded "
        f"list setitem path is not safe for partitioned writes.\noutput: {out}"
    )


def test_list_indexed_lock_contended_counter(tmp_path, monkeypatch):
    """Same workload as ``test_lock_acquire_contended_counter`` but the
    Lock is fetched via ``locks[0].acquire()`` instead of being a
    bare global. CPython does not distinguish the two; pcc no longer
    does as of the 2026-05-08 codegen fix in ``layer1.py`` that added
    direct dispatch for list-indexed threading method calls."""
    src = tmp_path / "lock_listindex_counter.py"
    exe = tmp_path / "lock_listindex_counter.out"
    src.write_text(textwrap.dedent("""
        from threading import Lock, Thread

        counts = [0]
        locks: list[Lock] = [Lock()]

        def worker() -> None:
            i = 0
            while i < 1000:
                locks[0].acquire()
                counts[0] = counts[0] + 1
                locks[0].release()
                i = i + 1

        def main() -> None:
            t0 = Thread(target=worker)
            t1 = Thread(target=worker)
            t2 = Thread(target=worker)
            t3 = Thread(target=worker)
            t0.start(); t1.start(); t2.start(); t3.start()
            t0.join(); t1.join(); t2.join(); t3.join()
            print(counts[0])

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")

    _compile_threaded(monkeypatch, tmp_path, src, exe)
    seen = []
    for _ in range(3):
        seen.append(_run(exe).strip())
    assert seen == ["4000", "4000", "4000"], (
        f"list-indexed Lock counter mismatch: {seen}"
    )
