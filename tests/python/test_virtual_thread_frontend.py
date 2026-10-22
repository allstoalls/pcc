from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path


def _write_case(path: Path) -> None:
    path.write_text(
        textwrap.dedent("""
            import pcc.virtual_thread as vt

            def worker(left: int, right: int) -> int:
                temp: int = left + right
                return temp + 1

            def main() -> None:
                thread = vt.spawn(worker, 20, 21)
                print(vt.run(2, 8))
                print(vt.state(thread))
                print(vt.result(thread))

            if __name__ == "__main__":
                main()
            """).lstrip(),
        encoding="utf-8",
    )


def _write_from_import_case(path: Path) -> None:
    path.write_text(
        textwrap.dedent("""
            from pcc.virtual_thread import result, run, spawn, state

            def worker(value: int) -> int:
                return value + 1

            def main() -> None:
                thread = spawn(worker, 41)
                print(run(2, 8))
                print(state(thread))
                print(result(thread))

            if __name__ == "__main__":
                main()
            """).lstrip(),
        encoding="utf-8",
    )


def _write_generator_case(path: Path) -> None:
    path.write_text(
        textwrap.dedent("""
            import threading
            import pcc.virtual_thread as vt

            lock = threading.Lock()

            def first():
                base = 10
                lock.acquire()
                vt.yield_now()
                base = base + 1
                lock.release()
                return base

            def second():
                value = 20
                lock.acquire()
                value = value + 2
                lock.release()
                return value
                vt.yield_now()

            def main() -> None:
                a = vt.spawn(first)
                b = vt.spawn(second)
                # One carrier runs the FIFO in order, so second parks on the
                # lock first holds across its yield (4 steps). Two carriers
                # may finish second before first acquires (3 steps); the
                # multi-carrier contention is test_virtual_thread_lock_mechanisms.
                print(vt.run(1, 10))
                print(vt.state(a))
                print(vt.result(a))
                print(vt.state(b))
                print(vt.result(b))

            if __name__ == "__main__":
                main()
            """).lstrip(),
        encoding="utf-8",
    )


def _write_blocking_primitives_case(path: Path) -> None:
    path.write_text(
        textwrap.dedent("""
            import threading
            import pcc.virtual_thread as vt

            event = threading.Event()
            cond = threading.Condition()
            sem = threading.Semaphore(0)

            def event_waiter():
                event.wait()
                return 7
                vt.yield_now()

            def event_setter():
                vt.yield_now()
                event.set()
                return 8

            def cond_waiter():
                cond.acquire()
                cond.wait()
                cond.release()
                return 9
                vt.yield_now()

            def cond_notifier():
                vt.yield_now()
                cond.acquire()
                cond.notify()
                cond.release()
                return 10

            def sem_waiter():
                sem.acquire()
                return 11
                vt.yield_now()

            def sem_releaser():
                vt.yield_now()
                sem.release()
                return 12

            def main() -> None:
                a = vt.spawn(event_waiter)
                b = vt.spawn(event_setter)
                c = vt.spawn(cond_waiter)
                d = vt.spawn(cond_notifier)
                e = vt.spawn(sem_waiter)
                f = vt.spawn(sem_releaser)
                print(vt.run(3, 30))
                print(vt.result(a))
                print(vt.result(b))
                print(vt.result(c))
                print(vt.result(d))
                print(vt.result(e))
                print(vt.result(f))

            if __name__ == "__main__":
                main()
            """).lstrip(),
        encoding="utf-8",
    )


def _write_task_failure_case(path: Path) -> None:
    path.write_text(
        textwrap.dedent("""
            import pcc.virtual_thread as vt

            def bad_worker() -> int:
                vt.yield_now()
                raise ValueError("task boom")

            def good_worker() -> int:
                vt.yield_now()
                return 42

            def main() -> None:
                bad = vt.spawn(bad_worker)
                good = vt.spawn(good_worker)
                print(vt.run(1, 16))
                print(vt.state(bad))
                print(vt.outcome(bad))
                print(vt.exception(bad))
                print(vt.state(good))
                print(vt.outcome(good))
                print(vt.result(good))
                print(vt.exception(good))

            if __name__ == "__main__":
                main()
            """).lstrip(),
        encoding="utf-8",
    )


def _write_join_case(path: Path) -> None:
    path.write_text(
        textwrap.dedent("""
            import pcc.virtual_thread as vt

            def delayed_value() -> int:
                vt.yield_now()
                return 40

            def add_one(target) -> int:
                return vt.join(target) + 1

            def add_two(target) -> int:
                return vt.join(target) + 2

            def delayed_failure() -> int:
                vt.yield_now()
                raise ValueError("join boom")

            def catch_failure(target) -> int:
                try:
                    return vt.join(target)
                except ValueError:
                    return 7

            def immediate_value() -> int:
                return 9

            def main() -> None:
                target = vt.spawn(delayed_value)
                first = vt.spawn(add_one, target)
                second = vt.spawn(add_two, target)
                print(vt.run(1, 16))
                print(vt.result(first))
                print(vt.result(second))

                bad = vt.spawn(delayed_failure)
                catcher = vt.spawn(catch_failure, bad)
                print(vt.run(1, 16))
                print(vt.result(catcher))

                done = vt.spawn(immediate_value)
                print(vt.run(1, 4))
                immediate = vt.spawn(add_one, done)
                print(vt.run(1, 4))
                print(vt.result(immediate))

            if __name__ == "__main__":
                main()
            """).lstrip(),
        encoding="utf-8",
    )


def _write_cancel_case(path: Path) -> None:
    path.write_text(
        textwrap.dedent("""
            import gc
            import pcc.virtual_thread as vt

            def sleeping() -> int:
                try:
                    vt.sleep_current(60000)
                    return 99
                finally:
                    print("timer cleanup")
                    gc.collect()
                    print("timer cleanup done")

            def never_started() -> int:
                print("SHOULD NOT RUN")
                vt.yield_now()
                return 1

            def cleanup_failure() -> int:
                try:
                    vt.sleep_current(60000)
                    return 2
                finally:
                    raise ValueError("cleanup boom")

            def nested_child() -> int:
                try:
                    vt.sleep_current(60000)
                    return 3
                finally:
                    print("child cleanup")

            def nested_parent() -> int:
                try:
                    return nested_child()
                finally:
                    print("parent cleanup")

            def join_target() -> int:
                vt.sleep_current(60000)
                return 4

            def join_waiter(target) -> int:
                try:
                    return vt.join(target)
                finally:
                    print("join cleanup")

            def main() -> None:
                timer = vt.spawn(sleeping)
                print(vt.run(1, 1))
                print(vt.cancel(timer))
                print(vt.cancel(timer))
                print(vt.run(1, 4))
                print(vt.state(timer))
                print(vt.outcome(timer))

                cold = vt.spawn(never_started)
                print(vt.cancel(cold))
                print(vt.run(1, 4))
                print(vt.outcome(cold))

                bad = vt.spawn(cleanup_failure)
                print(vt.run(1, 1))
                print(vt.cancel(bad))
                print(vt.run(1, 4))
                print(vt.outcome(bad))
                print(vt.exception(bad))

                nested = vt.spawn(nested_parent)
                print(vt.run(1, 1))
                print(vt.cancel(nested))
                print(vt.run(1, 4))
                print(vt.outcome(nested))

                target = vt.spawn(join_target)
                waiter = vt.spawn(join_waiter, target)
                print(vt.run(1, 2))
                print(vt.cancel(waiter))
                print(vt.run(1, 4))
                print(vt.outcome(waiter))
                print(vt.cancel(target))
                print(vt.run(1, 4))
                print(vt.outcome(target))

            if __name__ == "__main__":
                main()
            """).lstrip(),
        encoding="utf-8",
    )


def _write_sequential_io_case(path: Path) -> None:
    path.write_text(
        textwrap.dedent("""
            import os
            import pcc.virtual_thread as vt
            from pcc.extern import c_int32, c_ptr, extern
            from pcc.unsafe import free, load_i32, malloc
            from pcc.virtual_thread import readable, writable

            pipe_call = extern("pipe", (c_ptr,), c_int32)
            close_call = extern("close", (c_int32,), c_int32)

            def wait_writable(fd: int) -> int:
                print("write-before")
                writable(fd)
                print("write-after")
                return 22

            def observer() -> int:
                print("observer")
                return 33

            def wait_readable(fd: int) -> int:
                readable(fd)
                print("readable")
                return 11

            def write_one(fd: int) -> int:
                print(os.write(fd, b"x"))
                return 1

            def main() -> None:
                fds = malloc(8)
                if pipe_call(fds) != 0:
                    print("pipe-failed")
                    free(fds)
                    return
                read_fd = load_i32(fds, 0)
                write_fd = load_i32(fds, 4)
                print(vt.io_backend())

                writable_task = vt.spawn(wait_writable, write_fd)
                observer_task = vt.spawn(observer)
                vt.run(1, 16)

                readable_task = vt.spawn(wait_readable, read_fd)
                writer_task = vt.spawn(write_one, write_fd)
                vt.run(1, 32)

                print(vt.result(writable_task))
                print(vt.result(observer_task))
                print(vt.result(readable_task))
                print(vt.result(writer_task))
                close_call(read_fd)
                close_call(write_fd)
                free(fds)

            if __name__ == "__main__":
                main()
            """).lstrip(),
        encoding="utf-8",
    )


def test_virtual_thread_spawn_lowers_to_typed_resume_ir(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "vthread_frontend.py"
    ll = tmp_path / "vthread_frontend.ll"
    _write_case(src)

    compile_python(
        str(src),
        str(ll),
        emit_llvm_only=True,
        ir_scaffold_mode="on",
        libpython_mode="off",
    )

    ir_text = ll.read_text(encoding="utf-8")
    assert "py_continuation_new_typed" in ir_text
    assert "__vthread_resume_2" in ir_text
    assert "py_continuation_get_slot" in ir_text
    assert "py_virtual_thread_complete" in ir_text


def test_virtual_thread_generator_spawn_lowers_to_state_machine_resume_ir(
    tmp_path,
    monkeypatch,
):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "vthread_generator.py"
    ll = tmp_path / "vthread_generator.ll"
    _write_generator_case(src)

    compile_python(
        str(src),
        str(ll),
        emit_llvm_only=True,
        ir_scaffold_mode="on",
        libpython_mode="off",
    )

    ir_text = ll.read_text(encoding="utf-8")
    assert "py_virtual_thread_resume_generator" in ir_text
    assert "py_threading_lock_acquire_vthread" in ir_text
    assert "__gen_resume" in ir_text


def test_virtual_thread_spawn_runs_direct_user_function(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "vthread_frontend.py"
    exe = tmp_path / "vthread_frontend.out"
    _write_case(src)

    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines() == ["1", "4", "42"]


def test_virtual_thread_import_from_aliases_run(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "vthread_from_import.py"
    exe = tmp_path / "vthread_from_import.out"
    _write_from_import_case(src)

    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines() == ["1", "4", "42"]


def test_virtual_thread_generator_spawn_preserves_frame_and_parks_lock(
    tmp_path,
    monkeypatch,
):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "vthread_generator.py"
    exe = tmp_path / "vthread_generator.out"
    _write_generator_case(src)

    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines() == ["4", "4", "11", "4", "22"]


def test_virtual_thread_generator_parks_threading_wait_primitives(
    tmp_path,
    monkeypatch,
):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "vthread_blocking.py"
    exe = tmp_path / "vthread_blocking.out"
    _write_blocking_primitives_case(src)

    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.strip().splitlines()
    assert int(lines[0]) >= 9
    assert lines[1:] == ["7", "8", "9", "10", "11", "12"]


def test_virtual_thread_uncaught_exception_is_task_local(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "vthread_task_failure.py"
    exe = tmp_path / "vthread_task_failure.out"
    _write_task_failure_case(src)

    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines() == [
        "4",
        "4",
        "2",
        "task boom",
        "4",
        "1",
        "42",
        "None",
    ]


def test_virtual_thread_join_parks_and_propagates_outcomes(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "vthread_join.py"
    exe = tmp_path / "vthread_join.out"
    _write_join_case(src)

    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines() == [
        "6",
        "41",
        "42",
        "4",
        "7",
        "1",
        "1",
        "10",
    ]


def test_virtual_thread_cancel_is_cooperative_and_runs_sync_cleanup(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("PCC_GC_BACKEND", "4")
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "vthread_cancel.py"
    exe = tmp_path / "vthread_cancel.out"
    _write_cancel_case(src)

    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().splitlines() == [
        "1",
        "True",
        "False",
        "timer cleanup",
        "timer cleanup done",
        "1",
        "4",
        "3",
        "True",
        "1",
        "3",
        "1",
        "True",
        "1",
        "2",
        "cleanup boom",
        "1",
        "True",
        "child cleanup",
        "parent cleanup",
        "1",
        "3",
        "2",
        "True",
        "join cleanup",
        "1",
        "3",
        "True",
        "1",
        "3",
    ]


def test_virtual_thread_sequential_readable_writable_use_platform_reactor(
    tmp_path,
    monkeypatch,
):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "vthread_sequential_io.py"
    exe = tmp_path / "vthread_sequential_io.out"
    _write_sequential_io_case(src)

    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    if sys.platform == "darwin":
        expected_backend = "1"
    elif sys.platform == "linux":
        expected_backend = "2"
    else:
        expected_backend = "0"
    assert result.stdout.strip().splitlines() == [
        expected_backend,
        "write-before",
        "write-after",
        "observer",
        "1",
        "readable",
        "22",
        "33",
        "11",
        "1",
    ]
