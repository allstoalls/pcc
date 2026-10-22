"""Proof that pcc's threading model gives real free-threaded parallelism.

Builds two binaries with ``PCC_WITH_THREADS=1``:

  * ``benchmarks/python/boc_bank_demo.py`` — 4 pthreads each running a CPU-bound mixer.
  * ``benchmarks/python/boc_bank_demo_serial.py`` — same total work on one thread.

Pass criteria:

  1. Both binaries exit 0 within timeout.
  2. The parallel binary's stdout contains ``DONE`` and one ``t<i>`` line
     per worker thread (so all threads completed).
  3. Wall-clock(serial) / Wall-clock(parallel) > 2.5x — proves the
     pthread substrate runs in parallel rather than serializing through
     a GIL or shared lock.

Both binaries link an isolated ``PCC_WITH_THREADS=1`` runtime fixture; the
repository's shared runtime archive is never replaced or deleted.
The default suite compiles and runs the parallel binary and checks every worker;
the wall-clock comparison is selected only by ``PCC_RUN_BOC_SPEEDUP=1`` on a
quiet multicore host.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest


pytestmark = pytest.mark.xdist_group(name="pcc_heavy_llvm")

REPO = Path(__file__).absolute().parents[2]
PARALLEL_SRC = REPO / "benchmarks" / "python" / "boc_bank_demo.py"
SERIAL_SRC = REPO / "benchmarks" / "python" / "boc_bank_demo_serial.py"

# Minimum parallel speedup we require to count the proof as PASS.
# Empirically measured ~3.57x on a 4-thread macOS arm64 host; we set the
# bar at 2.5x to leave headroom for noisy CI hardware while still
# rejecting any run where threads serialized.
MIN_SPEEDUP = 2.5

# Expected number of worker threads in the parallel demo.
N_WORKERS = 4


@pytest.fixture
def threaded_runtime(monkeypatch, threaded_pcc_runtime_archive):
    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(threaded_pcc_runtime_archive))


def _run_binary(
    exe: Path,
    timeout: float = 60.0,
    runs: int = 3,
) -> tuple[float, str]:
    """Run ``exe`` ``runs`` times, return (min wall-clock, stdout-of-min-run).

    CPU-bound benchmarks have one-sided noise — extra time can leak in
    from cold cache, OS scheduling, the first subprocess spawn — but
    nothing makes a fixed number of integer ops finish faster than the
    hardware allows. The minimum across runs is the cleanest estimate.
    """
    best_elapsed = float("inf")
    best_stdout = ""
    for _ in range(runs):
        start = time.perf_counter()
        result = subprocess.run(
            [str(exe)], capture_output=True, text=True, timeout=timeout,
        )
        elapsed = time.perf_counter() - start
        assert result.returncode == 0, (
            f"{exe.name} exited {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
        if elapsed < best_elapsed:
            best_elapsed = elapsed
            best_stdout = result.stdout
    return best_elapsed, best_stdout


def test_pcc_threads_complete_all_workers(tmp_path, threaded_runtime):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "boc_bank_demo.py"
    shutil.copyfile(PARALLEL_SRC, src)
    exe = tmp_path / "parallel.out"

    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    _, out = _run_binary(exe, timeout=60.0, runs=1)

    lines = [ln.strip() for ln in out.strip().splitlines()]
    assert "DONE" in lines, f"parallel demo missing DONE marker.\noutput:\n{out}"
    worker_lines = [ln for ln in lines if ln.startswith("t") and " r=" in ln]
    assert len(worker_lines) == N_WORKERS, (
        f"expected {N_WORKERS} worker output lines, got "
        f"{len(worker_lines)}: {worker_lines}"
    )


@pytest.mark.pcc_gate(env="PCC_RUN_BOC_SPEEDUP")
def test_pcc_threads_give_real_parallel_speedup(tmp_path, threaded_runtime):
    from pcc.frontends.python.pipeline import compile_python

    parallel_src = tmp_path / "boc_bank_demo.py"
    serial_src = tmp_path / "boc_bank_demo_serial.py"
    shutil.copyfile(PARALLEL_SRC, parallel_src)
    shutil.copyfile(SERIAL_SRC, serial_src)
    parallel_exe = tmp_path / "parallel.out"
    serial_exe = tmp_path / "serial.out"

    compile_python(
        str(parallel_src), str(parallel_exe),
        ir_scaffold_mode="on", libpython_mode="off",
    )
    compile_python(
        str(serial_src), str(serial_exe),
        ir_scaffold_mode="on", libpython_mode="off",
    )

    serial_time, serial_out = _run_binary(serial_exe, timeout=60.0)
    parallel_time, parallel_out = _run_binary(parallel_exe, timeout=60.0)

    parallel_lines = [ln.strip() for ln in parallel_out.strip().splitlines()]
    assert "DONE" in parallel_lines, (
        f"parallel demo missing DONE marker.\noutput:\n{parallel_out}"
    )
    worker_lines = [ln for ln in parallel_lines if ln.startswith("t") and " r=" in ln]
    assert len(worker_lines) == N_WORKERS, (
        f"expected {N_WORKERS} worker output lines, got "
        f"{len(worker_lines)}: {worker_lines}"
    )

    speedup = serial_time / parallel_time
    print(
        f"\n[boc-proof] serial={serial_time:.2f}s "
        f"parallel={parallel_time:.2f}s "
        f"speedup={speedup:.2f}x "
        f"(threshold={MIN_SPEEDUP}x)"
    )
    assert speedup >= MIN_SPEEDUP, (
        f"insufficient parallel speedup: {speedup:.2f}x "
        f"(need >= {MIN_SPEEDUP}x). "
        f"serial={serial_time:.2f}s parallel={parallel_time:.2f}s. "
        "Run this explicit gate on a quiet multicore host; do not lower "
        "MIN_SPEEDUP to absorb host contention."
    )
