"""End-to-end pcc benchmark suite ported from BoCPy.

Three benches, all built with ``PCC_WITH_THREADS=1`` and run as
no-libpython native binaries:

  * ``benchmarks/python/boc_ring.py`` — chain microbenchmark with sliding window
    of locked cowns (BoCPy's ``examples/benchmark.py`` core)
  * ``benchmarks/python/boc_boids.py`` — flocking simulation, disjoint-write
    parallelism (BoCPy's ``examples/boids.py``)
  * ``benchmarks/python/boc_cooking.py`` — staged producer/consumer pipeline
    (BoCPy's ``examples/cooking_boc.py``)

For the ring benchmark we additionally compare wall-clock against
the single-threaded baseline ``benchmarks/python/boc_ring_serial.py`` and assert a
speedup floor — that's the headline parallelism number BoCPy reports
"near-linear" for. We require >= 1.5x as a noise-tolerant floor: high enough
to reject serialized execution, low enough to tolerate noisy CI hosts.
That wall-clock proof is selected only by ``PCC_RUN_BOC_SPEEDUP=1`` on a
quiet multicore host. The default suite still compiles and runs the parallel
ring and checks its invariant without making a performance claim.

The binaries link an isolated ``PCC_WITH_THREADS=1`` runtime fixture; the
repository's shared runtime archive is never replaced or deleted.
"""
from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest


pytestmark = pytest.mark.xdist_group(name="pcc_heavy_llvm")

REPO = Path(__file__).absolute().parents[2]
RING_PARALLEL = REPO / "benchmarks" / "python" / "boc_ring.py"
RING_SERIAL = REPO / "benchmarks" / "python" / "boc_ring_serial.py"
BOIDS = REPO / "benchmarks" / "python" / "boc_boids.py"
COOKING = REPO / "benchmarks" / "python" / "boc_cooking.py"

# Ring speedup floor. BoCPy's published number is "near-linear" on
# their ring; we keep a noise-tolerant threshold that still rejects
# fully serialized execution while avoiding flake on fast/quiet CI hosts
# where the absolute runtime is too short for a hard 2.0x floor.
MIN_RING_SPEEDUP = 1.5


@pytest.fixture
def threaded_runtime(monkeypatch, threaded_pcc_runtime_archive):
    monkeypatch.setenv("PCC_WITH_THREADS", "1")
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(threaded_pcc_runtime_archive))


def _compile(src: Path, exe: Path) -> None:
    from pcc.frontends.python.pipeline import compile_python

    compile_python(
        str(src), str(exe),
        ir_scaffold_mode="on", libpython_mode="off",
    )


def _run_min(exe: Path, runs: int = 3, timeout: float = 60.0) -> tuple[float, str]:
    best = float("inf")
    best_out = ""
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
        if elapsed < best:
            best = elapsed
            best_out = result.stdout
    return best, best_out


def test_boc_ring_correctness(tmp_path, threaded_runtime):
    src = tmp_path / RING_PARALLEL.name
    shutil.copyfile(RING_PARALLEL, src)
    exe = tmp_path / "ring.out"

    _compile(src, exe)
    _, out = _run_min(exe, runs=1)

    assert "PASS" in out, (
        f"ring parallel did not PASS — sum invariant broken.\noutput:\n{out}"
    )


@pytest.mark.pcc_gate(env="PCC_RUN_BOC_SPEEDUP")
def test_boc_ring_correctness_and_speedup(tmp_path, threaded_runtime):
    parallel_src = tmp_path / RING_PARALLEL.name
    serial_src = tmp_path / RING_SERIAL.name
    shutil.copyfile(RING_PARALLEL, parallel_src)
    shutil.copyfile(RING_SERIAL, serial_src)
    parallel_exe = tmp_path / "ring.out"
    serial_exe = tmp_path / "ring_serial.out"

    _compile(parallel_src, parallel_exe)
    _compile(serial_src, serial_exe)

    serial_t, serial_out = _run_min(serial_exe)
    parallel_t, parallel_out = _run_min(parallel_exe)

    assert "PASS" in parallel_out, (
        f"ring parallel did not PASS — sum invariant broken.\noutput:\n{parallel_out}"
    )
    assert "PASS" in serial_out, (
        f"ring serial did not PASS.\noutput:\n{serial_out}"
    )

    speedup = serial_t / parallel_t
    print(
        f"\n[boc-ring] serial={serial_t:.2f}s "
        f"parallel={parallel_t:.2f}s "
        f"speedup={speedup:.2f}x (floor={MIN_RING_SPEEDUP}x)"
    )
    assert speedup >= MIN_RING_SPEEDUP, (
        f"ring parallelism collapsed: {speedup:.2f}x "
        f"(need >= {MIN_RING_SPEEDUP}x). "
        f"serial={serial_t:.2f}s parallel={parallel_t:.2f}s"
    )


def test_boc_boids_completes(tmp_path, threaded_runtime):
    src = tmp_path / BOIDS.name
    shutil.copyfile(BOIDS, src)
    exe = tmp_path / "boids.out"
    _compile(src, exe)

    elapsed, out = _run_min(exe, runs=2)
    print(f"\n[boc-boids] {elapsed:.2f}s")

    lines = [ln.strip() for ln in out.strip().splitlines()]
    assert "DONE" in lines, f"boids missing DONE.\noutput:\n{out}"
    # Sanity: positions moved (sum changed from any obvious initial).
    sum_px = next(
        (int(ln.split("=", 1)[1]) for ln in lines if ln.startswith("sum_px=")),
        None,
    )
    assert sum_px is not None and sum_px > 0


def test_boc_cooking_pipeline_serves_all(tmp_path, threaded_runtime):
    src = tmp_path / COOKING.name
    shutil.copyfile(COOKING, src)
    exe = tmp_path / "cooking.out"
    _compile(src, exe)

    elapsed, out = _run_min(exe, runs=2, timeout=120.0)
    print(f"\n[boc-cooking] {elapsed:.2f}s")
    lines = [ln.strip() for ln in out.strip().splitlines()]
    assert "PASS" in lines, (
        f"cooking pipeline did not serve all items.\noutput:\n{out}"
    )
