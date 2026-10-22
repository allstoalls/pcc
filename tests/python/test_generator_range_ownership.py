"""Materialized range values own their storage across generator suspension."""

import os
import subprocess

import pytest


@pytest.mark.parametrize("exit_mode", ["exhaust", "close", "error"])
def test_generator_range_releases_materialized_iterable(
    tmp_path, pcc_runtime_archive, python_program_compiler, monkeypatch, exit_mode,
):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "range_owner.py"
    source.write_text('''import gc
from pcc.extern import extern, c_int64
heap = extern("pcc_os_heap_in_use_bytes", (), c_int64)
def values(count, fail):
    for index in range(count):
        if fail and index == 1:
            raise ValueError("range failure")
        yield index
def consume(count, mode):
    iterator = values(count, mode == "error")
    total = 0
    try:
        for value in iterator:
            total += value
            if mode == "close":
                iterator.close()
                break
    except ValueError:
        assert mode == "error"
    if mode == "exhaust":
        assert total == count * (count - 1) // 2
    else:
        assert total == 0
def main():
    mode = "''' + exit_mode + '''"
    consume(32, mode)
    gc.collect()
    before = heap()
    consume(10000, mode)
    gc.collect()
    middle = heap()
    consume(10000, mode)
    gc.collect()
    print(middle - before, heap() - middle)
main()
''')
    binary = tmp_path / "range_owner"
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        run = subprocess.run(
            [str(binary)], capture_output=True, text=True, timeout=15,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
        )
        assert run.returncode == 0, (backend, run.stdout, run.stderr)
        growth = list(map(int, run.stdout.split()))
        if backend == 0:
            assert all(value < 16384 for value in growth), growth
