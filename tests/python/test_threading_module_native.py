from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

from tests.runtime_build_cache import cached_threaded_pcc_python_runtime


REPO_ROOT = Path(__file__).absolute().parents[2]
RUNTIME_DIR = REPO_ROOT / "pcc" / "runtime"


def _build_threaded_runtime(tmp_path: Path) -> Path:
    del tmp_path
    return cached_threaded_pcc_python_runtime()


def test_threading_stdlib_native_lock_event_smoke(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "threading_smoke.py"
    exe = tmp_path / "threading_smoke.out"
    src.write_text(textwrap.dedent("""
        import threading

        def main() -> None:
            print(threading.get_ident() > 0)
            lock = threading.Lock()
            print(lock.acquire())
            lock.release()
            ev = threading.Event()
            print(ev.is_set())
            ev.set()
            print(ev.is_set())

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    compile_python(str(src), str(exe), ir_scaffold_mode="on", libpython_mode="off")
    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["True", "True", "False", "True"]


def test_thread_start_runs_target_via_native_dispatch_under_default_runtime(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "thread_start_native.py"
    exe = tmp_path / "thread_start_native.out"
    src.write_text(textwrap.dedent("""
        import threading

        def work() -> None:
            print("worker")

        def main() -> None:
            t = threading.Thread(target=work)
            print(t.is_alive())
            t.start()
            t.join()
            print(t.is_alive())

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    compile_python(str(src), str(exe), ir_scaffold_mode="on", libpython_mode="off")
    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["False", "worker", "False"]


def test_thread_start_on_for_loop_target_from_thread_list(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "thread_for_target.py"
    exe = tmp_path / "thread_for_target.out"
    src.write_text(textwrap.dedent("""
        import threading

        results = []

        def work(n) -> None:
            results.append(n * n)

        def main() -> None:
            ts = [threading.Thread(target=work, args=(i,)) for i in range(3)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            print(sorted(results))

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    compile_python(str(src), str(exe), ir_scaffold_mode="on", libpython_mode="off")
    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "[0, 1, 4]"


def test_thread_start_on_for_loop_target_from_appended_thread_name(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "thread_for_append_name.py"
    exe = tmp_path / "thread_for_append_name.out"
    src.write_text(textwrap.dedent("""
        from threading import Thread

        def work(n) -> None:
            print("worker", n)

        def main() -> None:
            threads = []
            i = 0
            while i < 2:
                th = Thread(target=work, args=(i,))
                threads.append(th)
                i += 1
            for th in threads:
                th.start()
            for th in threads:
                th.join()
            print("done")

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    compile_python(str(src), str(exe), ir_scaffold_mode="on", libpython_mode="off")
    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    lines = sorted(result.stdout.strip().splitlines())
    assert lines == ["done", "worker 0", "worker 1"]


def test_threading_import_from_and_sync_primitives_native(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "threading_from_native.py"
    exe = tmp_path / "threading_from_native.out"
    src.write_text(textwrap.dedent("""
        from threading import Thread, Condition, Semaphore

        def work() -> None:
            print("from-worker")

        def main() -> None:
            t = Thread(target=work)
            t.start()
            t.join()
            cond = Condition()
            print(cond.acquire())
            cond.notify()
            cond.release()
            sem = Semaphore(1)
            print(sem.acquire())
            sem.release()

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    compile_python(str(src), str(exe), ir_scaffold_mode="on", libpython_mode="off")
    result = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == [
        "from-worker", "True", "True",
    ]


def test_pthread_thread_object_survives_dropped_user_reference(tmp_path):
    """A started native Thread owns a handoff ref until its worker returns.

    This is a real lifetime regression test, not an IR name scan: without the
    handoff reference, `py_decref(thread)` can free the wrapper before the
    pthread trampoline enters `py_threading_thread_main()`.
    """
    work_runtime = _build_threaded_runtime(tmp_path)

    src = tmp_path / "thread_handoff.c"
    exe = tmp_path / "thread_handoff.out"
    src.write_text(textwrap.dedent(r"""
        #include "py_runtime.h"
        #include <stdio.h>

        static int worker_seen = 0;

        static PyObject *worker(PyObject *captures, PyObject *args) {
            (void)captures;
            (void)args;
            puts("worker");
            fflush(stdout);
            __atomic_store_n(&worker_seen, 1, __ATOMIC_RELEASE);
            return py_None;
        }

        int main(void) {
            PyObject *captures = py_tuple_new(0);
            PyObject *args = py_tuple_new(0);
            PyObject *fn = py_func_new((void *)worker, captures);
            PyObject *thread = py_threading_thread_new(fn, args);
            if (thread == 0) return 1;
            if (py_threading_thread_start(thread) != 0) return 2;

            /* Drop every user-visible reference immediately after start.
             * The native thread's handoff ref must keep the wrapper, target,
             * and args alive until the target finishes. */
            py_decref(thread);
            py_decref(fn);
            py_decref(args);
            py_decref(captures);

            int spins = 0;
            while (__atomic_load_n(&worker_seen, __ATOMIC_ACQUIRE) == 0) {
                pcc_thread_safepoint();
                spins++;
                if (spins > 1000000) return 3;
            }
            return 0;
        }
        """).lstrip(), encoding="utf-8")
    cc = os.environ.get("CC", "cc")
    build = subprocess.run(
        [
            cc, "-std=c11", "-pthread",
            f"-I{work_runtime / 'include'}",
            str(src), str(work_runtime / "libpy_runtime_pcc_py.a"),
            "-o", str(exe),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert build.returncode == 0, build.stderr
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=20)
    assert run.returncode == 0, run.stderr
    assert "worker" in run.stdout


def test_pthread_lock_serializes_shared_list_updates(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    work_runtime = _build_threaded_runtime(tmp_path)

    src = tmp_path / "lock_lost_update.py"
    exe = tmp_path / "lock_lost_update.out"
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
                i += 1

        def main() -> None:
            t0 = Thread(target=worker)
            t1 = Thread(target=worker)
            t2 = Thread(target=worker)
            t3 = Thread(target=worker)
            t0.start()
            t1.start()
            t2.start()
            t3.start()
            t0.join()
            t1.join()
            t2.join()
            t3.join()
            print(counts[0])

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")

    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        runtime_archive=str(work_runtime / "libpy_runtime_pcc_py.a"),
    )
    outputs: list[str] = []
    for _ in range(3):
        result = subprocess.run(
            [str(exe)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        outputs.append(result.stdout.strip())
    assert outputs == ["4000", "4000", "4000"]
