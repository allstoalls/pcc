"""Concurrent append keeps every owned value across all five GC backends."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import textwrap


def concurrent_append_source() -> str:
    return textwrap.dedent('''
        import gc
        import sys
        import threading

        class Base:
            pass

        class Other:
            pass

        original_start = threading.Event()
        original_results = []

        def original_worker():
            original_start.wait()
            original_results.append(Base.__new__)
            original_results.append(Other.__new__)

        def original_case():
            threads = []
            for index in range(4):
                thread = threading.Thread(target=original_worker)
                threads.append(thread)
                thread.start()
            original_start.set()
            for thread in threads:
                thread.join()
            assert len(original_results) == 8
            canonical = original_results[0]
            for value in original_results:
                assert value is canonical
            assert canonical is Base.__new__
            print("NEW_THREAD_IDENTITY_OK")

        barrier_condition = threading.Condition()
        barrier_arrived = 0
        barrier_generation = 0

        def append_barrier_wait():
            global barrier_arrived, barrier_generation
            barrier_condition.acquire()
            generation = barrier_generation
            barrier_arrived = barrier_arrived + 1
            if barrier_arrived == 4:
                barrier_arrived = 0
                barrier_generation = barrier_generation + 1
                barrier_condition.notify_all()
            else:
                while barrier_generation == generation:
                    barrier_condition.wait()
            barrier_condition.release()

        finalized = []

        class Record:
            def __init__(self, value: int):
                self.value = value

            def __del__(self):
                finalized.append(self.value)

        def returned_value(worker: int, step: int) -> list[int]:
            return [worker, step, 0]

        def append_worker(worker: int, rounds: int,
                          values: list[list[int]], records: list[Record]):
            for step in range(rounds):
                payload = returned_value(worker, step)
                payload.append(1)
                append_barrier_wait()
                values.append(payload)
                payload[2] = worker + step
                records.append(Record(worker * rounds + step))

        def capacity_case():
            workers = 4
            rounds = 32
            values: list[list[int]] = []
            records: list[Record] = []
            threads = []
            for worker in range(workers):
                thread = threading.Thread(target=append_worker,
                    args=(worker, rounds, values, records))
                threads.append(thread)
                thread.start()
            for thread in threads:
                thread.join()
            gc.collect()
            assert len(values) == workers * rounds
            assert len(records) == workers * rounds
            indices = []
            for payload in values:
                assert len(payload) == 4
                assert payload[2] == payload[0] + payload[1]
                assert payload[3] == 1
                indices.append(payload[0] * rounds + payload[1])
            assert sorted(indices) == list(range(workers * rounds))
            record_indices = []
            for record in records:
                record_indices.append(record.value)
            assert sorted(record_indices) == list(range(workers * rounds))
            record = records[0]
            alias = record
            previous = record.value
            record.value = -1
            assert alias is records[0]
            assert alias.value == -1
            records.clear()
            record = None
            alias = None
            gc.collect()
            expected_finalized = list(range(workers * rounds))
            expected_finalized.remove(previous)
            expected_finalized.append(-1)
            assert sorted(finalized) == sorted(expected_finalized)
            print("CONCURRENT_APPEND_CONTENT_OK")

        if sys.argv[1] == "original":
            original_case()
        else:
            capacity_case()
    ''').lstrip()


def test_concurrent_append_gate_preserves_host_values(tmp_path):
    source = tmp_path / "append_gate.py"
    source.write_text(concurrent_append_source(), encoding="utf-8")
    for mode, expected in (("original", "NEW_THREAD_IDENTITY_OK\n"),
                           ("capacity", "CONCURRENT_APPEND_CONTENT_OK\n")):
        result = subprocess.run([sys.executable, str(source), mode],
                                capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout == expected


def test_native_concurrent_append_preserves_all_values_and_growth_all_gcs(
    tmp_path: Path, monkeypatch, threaded_pcc_runtime_archive: Path,
):
    from pcc.frontends.python.pipeline import compile_python

    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    source = tmp_path / "append_gate.py"
    executable = tmp_path / "append_gate"
    source.write_text(concurrent_append_source(), encoding="utf-8")
    compile_python(str(source), str(executable), backend="self",
                   libpython_mode="off", ir_scaffold_mode="on",
                   runtime_archive=str(threaded_pcc_runtime_archive))
    for gc_backend in range(5):
        for mode, expected in (("original", "NEW_THREAD_IDENTITY_OK\n"),
                               ("capacity", "CONCURRENT_APPEND_CONTENT_OK\n")):
            result = subprocess.run(
                [str(executable), mode],
                env=dict(os.environ, PCC_GC_BACKEND=str(gc_backend)),
                capture_output=True, text=True, timeout=30,
            )
            assert result.returncode == 0, (gc_backend, mode, result.stdout, result.stderr)
            assert result.stdout == expected, (gc_backend, mode, result.stdout)
