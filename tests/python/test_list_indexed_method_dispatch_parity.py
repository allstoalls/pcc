from __future__ import annotations

import subprocess
import textwrap


def test_list_indexed_method_dispatch_parity(
    tmp_path, monkeypatch, threaded_pcc_runtime_archive
):
    """One parity gate for ``list[index].method(...)`` dispatch.

    Covers the three shapes that should stay equivalent to assigning the
    element to a local first:

    * ordinary pcc user class method dispatch;
    * native ``Thread.start`` / ``Thread.join`` dispatch;
    * native ``Lock.acquire`` / ``Lock.release`` dispatch under contention.
    """
    from pcc.frontends.python.pipeline import compile_python

    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(threaded_pcc_runtime_archive))

    src = tmp_path / "list_indexed_method_dispatch_parity.py"
    exe = tmp_path / "list_indexed_method_dispatch_parity.out"
    src.write_text(textwrap.dedent("""
        from threading import Lock, Thread

        class Counter:
            def __init__(self, value: int) -> None:
                self.value = value

            def add(self, delta: int) -> int:
                self.value = self.value + delta
                return self.value

        def child() -> None:
            print("worker")

        def locked_worker(locks: list[Lock], counts: list) -> None:
            i = 0
            while i < 1000:
                locks[0].acquire()
                counts[0] = counts[0] + 1
                locks[0].release()
                i += 1

        def main() -> None:
            counters = [Counter(10), Counter(20)]
            print(counters[0].add(5))
            print(counters[1].add(7))
            print(counters[0].value)

            threads = [Thread(target=child)]
            print("start")
            threads[0].start()
            threads[0].join()
            print("done")

            locks: list[Lock] = []
            locks.append(Lock())
            counts = [0]
            t0 = Thread(target=locked_worker, args=(locks, counts))
            t1 = Thread(target=locked_worker, args=(locks, counts))
            t2 = Thread(target=locked_worker, args=(locks, counts))
            t3 = Thread(target=locked_worker, args=(locks, counts))
            t0.start(); t1.start(); t2.start(); t3.start()
            t0.join(); t1.join(); t2.join(); t3.join()
            print(counts[0])

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")

    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    outputs: list[list[str]] = []
    for _ in range(3):
        result = subprocess.run(
            [str(exe)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        outputs.append(result.stdout.strip().splitlines())
    assert outputs == [
        ["15", "27", "15", "start", "worker", "done", "4000"],
        ["15", "27", "15", "start", "worker", "done", "4000"],
        ["15", "27", "15", "start", "worker", "done", "4000"],
    ]
