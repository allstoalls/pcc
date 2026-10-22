"""pcc1 coverage for real threaded runtime interaction.

``test_pcc1_gc_backend_matrix.py`` covers pcc1 with the pcc-Python runtime
archive, whose ``Thread.start()`` currently runs through a synchronous shim.
This file forces the C runtime archive with ``PCC_WITH_THREADS=1`` so a
pcc1-compiled program exercises the real pthread Thread/Lock path. Explicit
GC collection while pthreads are active is a hard gate for every runtime GC
backend here.
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest


REPO = Path(__file__).absolute().parents[2]
RUNTIME = REPO / "pcc" / "runtime"
_PCC1_CANDIDATES = (
    REPO / "build" / "bootstrap-pytest-self" / "pcc1",
    REPO / "build" / "bootstrap" / "pcc1",
    REPO / "build" / "bootstrap-self-claude" / "pcc1",
    REPO / "build" / "bootstrap-llvm-claude" / "pcc1",
    REPO / "build" / "bootstrap-strict-self" / "pcc1",
    REPO / "build" / "bootstrap-self-darwin_arm64" / "pcc1",
    REPO / "build" / "bootstrap-llvm-darwin_arm64" / "pcc1",
)


def _find_pcc1() -> Path | None:
    env_path = os.environ.get("PCC1_BINARY")
    if env_path:
        p = Path(env_path)
        if p.exists() and p.is_file() and os.access(p, os.X_OK):
            return p
    for p in _PCC1_CANDIDATES:
        if p.exists() and p.is_file() and os.access(p, os.X_OK):
            return p
    return None


def _verify_isolated_runtime_archive(archive: Path) -> None:
    """Keep pcc1 probes away from the repository's shared build products."""

    assert archive.is_file()
    assert RUNTIME not in archive.parents


PCC1 = _find_pcc1()
pytestmark = [
    pytest.mark.pcc_gate(probe="pcc1"),
    # The session-scoped fixture builds a complete threaded runtime archive.
    # Keep this file on one worker so the five backend parameters share that
    # build instead of compiling one archive per xdist worker.
    pytest.mark.xdist_group(name="pcc1_threaded_gc"),
]


_EXPLICIT_THREADED_GC_SOURCE = textwrap.dedent(
    """
    import gc
    from threading import Lock, Thread

    results = []
    lock = Lock()

    def worker() -> None:
        i = 0
        while i < 200:
            chunk = [i, i + 1, i + 2]
            if i % 7 == 0:
                gc.collect()
            lock.acquire()
            results.append(chunk[1] - chunk[0])
            lock.release()
            i = i + 1

    def main() -> None:
        t0 = Thread(target=worker)
        t1 = Thread(target=worker)
        t2 = Thread(target=worker)
        t3 = Thread(target=worker)
        t0.start()
        t1.start()
        t2.start()
        t3.start()
        i = 0
        while i < 100:
            gc.collect()
            i = i + 1
        t0.join()
        t1.join()
        t2.join()
        t3.join()
        gc.collect()
        print(len(results))

    if __name__ == "__main__":
        main()
    """
).lstrip()


_PURE_COMPUTE_THREADED_GC_SOURCE = textwrap.dedent(
    """
    import gc
    from threading import Lock, Thread

    lock = Lock()
    ready = [0]
    result = [0]

    def worker() -> None:
        lock.acquire()
        ready[0] = 1
        lock.release()

        i = 0
        acc = 0
        while i < 2000000:
            acc = acc + i
            i = i + 1

        lock.acquire()
        result[0] = acc
        lock.release()

    def main() -> None:
        t = Thread(target=worker)
        t.start()

        seen = 0
        while seen == 0:
            lock.acquire()
            seen = ready[0]
            lock.release()

        i = 0
        while i < 32:
            gc.collect()
            i = i + 1

        t.join()
        print(result[0] > 0)

    if __name__ == "__main__":
        main()
    """
).lstrip()


def test_pcc1_c_runtime_threads_lock_backend0(
    tmp_path: Path,
    threaded_pcc_runtime_archive: Path,
) -> None:
    """pcc1 must compile a real-pthread Thread/Lock program under the C
    runtime archive.

    The test uses the C runtime archive because the pcc-Python runtime port
    still has a synchronous Thread shim. Re-running the produced binary catches
    common thread handoff and lock races in pcc1-compiled code.
    """
    _verify_isolated_runtime_archive(threaded_pcc_runtime_archive)
    src = tmp_path / "thread_gc.py"
    exe = tmp_path / "thread_gc.out"
    src.write_text(
        textwrap.dedent(
            """
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
            """
        ).lstrip(),
        encoding="utf-8",
    )
    env = dict(os.environ)
    env.pop("LC_ALL", None)
    env.update(
        {
            "PCC_RUNTIME_CC": "cc",
            "PCC_RUNTIME_HIGH": "c",
            "PCC_WITH_THREADS": "1",
            "PCC_GC_BACKEND": "0",
            "PCC_RUNTIME_ARCHIVE": str(threaded_pcc_runtime_archive),
        }
    )
    compile_cmd = [
        str(PCC1),
        "--backend",
        "self",
        "--python-libpython",
        "off",
        "--ir-scaffold",
        "on",
        str(src),
        "-o",
        str(exe),
    ]
    try:
        compile_proc = subprocess.run(
            compile_cmd,
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(REPO),
            env=env,
        )
        assert compile_proc.returncode == 0, (
            f"pcc1 threaded compile failed (exit {compile_proc.returncode}):\n"
            f"cmd: {' '.join(compile_cmd)}\n"
            f"stdout:\n{compile_proc.stdout}\n"
            f"stderr:\n{compile_proc.stderr}"
        )
        assert exe.exists()

        for _ in range(2):
            run_proc = subprocess.run(
                [str(exe)],
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            assert run_proc.returncode == 0, (
                f"pcc1-built threaded binary failed "
                f"(exit {run_proc.returncode}):\n"
                f"stdout:\n{run_proc.stdout}\n"
                f"stderr:\n{run_proc.stderr}"
            )
            assert run_proc.stdout.strip() == "4000"
    finally:
        _verify_isolated_runtime_archive(threaded_pcc_runtime_archive)


@pytest.mark.parametrize("gc_backend", ("0", "1", "2", "3", "4"))
def test_pcc1_c_runtime_threads_and_explicit_gc_collect_all_backends(
    tmp_path: Path,
    gc_backend: str,
    threaded_pcc_runtime_archive: Path,
) -> None:
    """pcc1-built real-pthread programs must survive explicit collection
    from both worker threads and the main thread under every GC backend.

    This covers the regression where a thread blocked in ``Lock.acquire()``
    never reached a safepoint, so another thread's stop-the-world
    ``gc.collect()`` waited forever or the process exited without printing.
    Backend 2 keeps a higher run count because the old shape could sweep an
    unpinned container literal temporary while another thread was explicitly
    collecting.
    """
    _verify_isolated_runtime_archive(threaded_pcc_runtime_archive)
    src = tmp_path / "thread_explicit_gc.py"
    exe = tmp_path / "thread_explicit_gc.out"
    src.write_text(_EXPLICIT_THREADED_GC_SOURCE, encoding="utf-8")
    env = dict(os.environ)
    env.pop("LC_ALL", None)
    env.update(
        {
            "PCC_RUNTIME_CC": "cc",
            "PCC_RUNTIME_HIGH": "c",
            "PCC_WITH_THREADS": "1",
            "PCC_GC_BACKEND": gc_backend,
            "PCC_RUNTIME_ARCHIVE": str(threaded_pcc_runtime_archive),
        }
    )
    compile_cmd = [
        str(PCC1),
        "--backend",
        "self",
        "--python-libpython",
        "off",
        "--ir-scaffold",
        "on",
        str(src),
        "-o",
        str(exe),
    ]
    try:
        compile_proc = subprocess.run(
            compile_cmd,
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(REPO),
            env=env,
        )
        assert compile_proc.returncode == 0, (
            f"pcc1 threaded explicit-GC compile failed "
            f"(exit {compile_proc.returncode}):\n"
            f"cmd: {' '.join(compile_cmd)}\n"
            f"stdout:\n{compile_proc.stdout}\n"
            f"stderr:\n{compile_proc.stderr}"
        )
        assert exe.exists()

        run_count = 5 if gc_backend == "2" else 2
        for _ in range(run_count):
            run_proc = subprocess.run(
                [str(exe)],
                capture_output=True,
                text=True,
                timeout=30,
                env=env,
            )
            assert run_proc.returncode == 0, (
                f"pcc1-built threaded explicit-GC binary failed "
                f"(exit {run_proc.returncode}):\n"
                f"stdout:\n{run_proc.stdout}\n"
                f"stderr:\n{run_proc.stderr}"
            )
            assert run_proc.stdout.strip() == "800"
    finally:
        _verify_isolated_runtime_archive(threaded_pcc_runtime_archive)


@pytest.mark.pcc_gate(env="PCC_PCC1_THREADED_GC_STRESS_RUNS")
def test_pcc1_c_runtime_threaded_explicit_gc_repeated_runs_stress(
    tmp_path: Path,
    threaded_pcc_runtime_archive: Path,
) -> None:
    """Opt-in pcc1 real-pthread explicit-GC flake detector.

    The normal hard gate above keeps CI cost bounded. This stress harness is
    for reliability work: set ``PCC_PCC1_THREADED_GC_STRESS_RUNS`` to repeat
    the same pcc1-built binary under each selected backend and fail with the
    first backend/run that times out, aborts, or loses output.
    """
    runs_raw = os.environ.get("PCC_PCC1_THREADED_GC_STRESS_RUNS", "").strip()
    if not runs_raw:
        pytest.fail("PCC_PCC1_THREADED_GC_STRESS_RUNS must be set when this stress gate is selected")
    try:
        runs = int(runs_raw)
    except ValueError:
        pytest.fail("PCC_PCC1_THREADED_GC_STRESS_RUNS must be an integer")
    if runs <= 0:
        pytest.fail("PCC_PCC1_THREADED_GC_STRESS_RUNS must be > 0")

    backends_raw = os.environ.get(
        "PCC_PCC1_THREADED_GC_STRESS_BACKENDS",
        "0,1,2,3,4",
    )
    backends = []
    for item in backends_raw.split(","):
        backend = item.strip()
        if backend:
            backends.append(backend)
    invalid = [backend for backend in backends if backend not in {"0", "1", "2", "3", "4"}]
    assert not invalid, "invalid GC backend(s): " + ", ".join(invalid)
    assert backends, "no GC backends selected"

    failures: list[str] = []
    for gc_backend in backends:
        _verify_isolated_runtime_archive(threaded_pcc_runtime_archive)
        src = tmp_path / f"thread_explicit_gc_stress_{gc_backend}.py"
        exe = tmp_path / f"thread_explicit_gc_stress_{gc_backend}.out"
        src.write_text(_EXPLICIT_THREADED_GC_SOURCE, encoding="utf-8")
        env = dict(os.environ)
        env.pop("LC_ALL", None)
        env.update(
            {
                "PCC_RUNTIME_CC": "cc",
                "PCC_RUNTIME_HIGH": "c",
                "PCC_WITH_THREADS": "1",
                "PCC_GC_BACKEND": gc_backend,
                "PCC_RUNTIME_ARCHIVE": str(threaded_pcc_runtime_archive),
            }
        )
        compile_cmd = [
            str(PCC1),
            "--backend",
            "self",
            "--python-libpython",
            "off",
            "--ir-scaffold",
            "on",
            str(src),
            "-o",
            str(exe),
        ]
        try:
            compile_proc = subprocess.run(
                compile_cmd,
                capture_output=True,
                text=True,
                timeout=180,
                cwd=str(REPO),
                env=env,
            )
            assert compile_proc.returncode == 0, (
                f"pcc1 stress compile failed for backend={gc_backend} "
                f"(exit {compile_proc.returncode}):\n"
                f"cmd: {' '.join(compile_cmd)}\n"
                f"stdout:\n{compile_proc.stdout}\n"
                f"stderr:\n{compile_proc.stderr}"
            )
            assert exe.exists()

            run_idx = 0
            while run_idx < runs:
                try:
                    run_proc = subprocess.run(
                        [str(exe)],
                        capture_output=True,
                        text=True,
                        timeout=30,
                        env=env,
                    )
                except subprocess.TimeoutExpired as exc:
                    failures.append(
                        f"backend={gc_backend} run={run_idx} timed out after "
                        f"{exc.timeout}s stdout={exc.stdout!r} stderr={exc.stderr!r}"
                    )
                    break
                if run_proc.returncode != 0 or run_proc.stdout.strip() != "800":
                    failures.append(
                        f"backend={gc_backend} run={run_idx} exit={run_proc.returncode} "
                        f"stdout={run_proc.stdout!r} stderr={run_proc.stderr!r}"
                    )
                    break
                run_idx += 1
        finally:
            _verify_isolated_runtime_archive(threaded_pcc_runtime_archive)

    assert not failures, "pcc1 threaded explicit-GC stress failures:\n" + "\n".join(failures)


@pytest.mark.parametrize("gc_backend", ("0", "4"))
def test_pcc1_c_runtime_pure_compute_loop_safepoints_under_threaded_gc(
    tmp_path: Path,
    gc_backend: str,
    threaded_pcc_runtime_archive: Path,
) -> None:
    """pcc1-generated pure compute loops must be cooperative safepoints.

    The worker sets a ready flag, then enters a long i64-only loop with no
    allocation, lock, print, or explicit runtime call in the hot body. The main
    thread performs explicit collection while the worker is active. Without
    generated loop-backedge safepoints this shape can block STW until the pure
    compute loop exits.
    """
    _verify_isolated_runtime_archive(threaded_pcc_runtime_archive)
    src = tmp_path / "thread_pure_compute_safepoint.py"
    exe = tmp_path / "thread_pure_compute_safepoint.out"
    src.write_text(_PURE_COMPUTE_THREADED_GC_SOURCE, encoding="utf-8")
    env = dict(os.environ)
    env.pop("LC_ALL", None)
    env.update(
        {
            "PCC_RUNTIME_CC": "cc",
            "PCC_RUNTIME_HIGH": "c",
            "PCC_WITH_THREADS": "1",
            "PCC_GC_BACKEND": gc_backend,
            "PCC_RUNTIME_ARCHIVE": str(threaded_pcc_runtime_archive),
        }
    )
    compile_cmd = [
        str(PCC1),
        "--backend",
        "self",
        "--python-libpython",
        "off",
        "--ir-scaffold",
        "on",
        str(src),
        "-o",
        str(exe),
    ]
    try:
        compile_proc = subprocess.run(
            compile_cmd,
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(REPO),
            env=env,
        )
        assert compile_proc.returncode == 0, (
            f"pcc1 pure-compute safepoint compile failed for backend={gc_backend} "
            f"(exit {compile_proc.returncode}):\n"
            f"cmd: {' '.join(compile_cmd)}\n"
            f"stdout:\n{compile_proc.stdout}\n"
            f"stderr:\n{compile_proc.stderr}"
        )
        assert exe.exists()

        run_proc = subprocess.run(
            [str(exe)],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        assert run_proc.returncode == 0, (
            f"pcc1-built pure-compute safepoint binary failed for backend={gc_backend} "
            f"(exit {run_proc.returncode}):\n"
            f"stdout:\n{run_proc.stdout}\n"
            f"stderr:\n{run_proc.stderr}"
        )
        assert run_proc.stdout.strip() == "True"
    finally:
        _verify_isolated_runtime_archive(threaded_pcc_runtime_archive)


def test_pcc1_c_runtime_threaded_backend4_exercises_zpage_allocator(
    tmp_path: Path,
    threaded_pcc_runtime_archive: Path,
) -> None:
    """pcc1-built real-pthread code under backend #4 must exercise ZPage
    allocation, not merely run generic Thread/GC paths.

    This keeps the pcc1 threaded gate tied to the backend4 page allocator
    introduced for GenZGC-style relocation work.
    """
    _verify_isolated_runtime_archive(threaded_pcc_runtime_archive)
    src = tmp_path / "thread_backend4_zpage.py"
    exe = tmp_path / "thread_backend4_zpage.out"
    src.write_text(_EXPLICIT_THREADED_GC_SOURCE, encoding="utf-8")
    env = dict(os.environ)
    env.pop("LC_ALL", None)
    env.update(
        {
            "PCC_RUNTIME_CC": "cc",
            "PCC_RUNTIME_HIGH": "c",
            "PCC_WITH_THREADS": "1",
            "PCC_GC_BACKEND": "4",
            "PCC_RUNTIME_ARCHIVE": str(threaded_pcc_runtime_archive),
        }
    )
    compile_cmd = [
        str(PCC1),
        "--backend",
        "self",
        "--python-libpython",
        "off",
        "--ir-scaffold",
        "on",
        str(src),
        "-o",
        str(exe),
    ]
    try:
        compile_proc = subprocess.run(
            compile_cmd,
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(REPO),
            env=env,
        )
        assert compile_proc.returncode == 0, (
            "pcc1 backend4 threaded ZPage compile failed "
            f"(exit {compile_proc.returncode}):\n"
            f"cmd: {' '.join(compile_cmd)}\n"
            f"stdout:\n{compile_proc.stdout}\n"
            f"stderr:\n{compile_proc.stderr}"
        )
        assert exe.exists()

        run_proc = subprocess.run(
            [str(exe)],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        assert run_proc.returncode == 0, (
            "pcc1-built backend4 threaded ZPage binary failed "
            f"(exit {run_proc.returncode}):\n"
            f"stdout:\n{run_proc.stdout}\n"
            f"stderr:\n{run_proc.stderr}"
        )
        assert run_proc.stdout.strip() == "800"

        probe_src = tmp_path / "backend4_zpage_probe.py"
        probe_exe = tmp_path / "backend4_zpage_probe.out"
        probe_src.write_text(
            textwrap.dedent(
                """
                import gc
                from pcc.extern import extern, c_int32, c_int64, c_ptr, c_void, c_obj

                pcc_gc_backend = extern("pcc_gc_backend", (), c_int64)
                pcc_gc_alloc = extern(
                    "pcc_gc_alloc", (c_int64, c_int32, c_int32), c_obj
                )
                pcc_gc_step = extern("pcc_gc_step", (c_int64,), c_int64)
                pcc_gc_telemetry_reset = extern(
                    "pcc_gc_telemetry_reset", (), c_void
                )
                pcc_gc_relocation_set_size = extern(
                    "pcc_gc_relocation_set_size", (), c_int64
                )
                pcc_gc_backend4_evacuated_bytes = extern(
                    "pcc_gc_backend4_evacuated_bytes", (), c_int64
                )
                pcc_gc_backend4_zpage_count = extern(
                    "pcc_gc_backend4_zpage_count", (), c_int64
                )
                pcc_gc_backend4_zpage_used_bytes = extern(
                    "pcc_gc_backend4_zpage_used_bytes", (), c_int64
                )
                pcc_gc_backend4_zpage_allocated_bytes = extern(
                    "pcc_gc_backend4_zpage_allocated_bytes", (), c_int64
                )

                def main() -> None:
                    xs = []
                    i = 0
                    while i < 64:
                        xs.append([i, i + 1])
                        i = i + 1
                    gc.collect()
                    print(pcc_gc_backend())
                    print(pcc_gc_backend4_zpage_count() > 0)
                    print(pcc_gc_backend4_zpage_used_bytes() > 0)
                    print(pcc_gc_backend4_zpage_allocated_bytes() > 0)

                    a = pcc_gc_alloc(128, 2, 256)
                    b = pcc_gc_alloc(128, 2, 256)
                    pcc_gc_telemetry_reset()
                    j = 0
                    while j < 8 and pcc_gc_backend4_evacuated_bytes() < 256:
                        pcc_gc_step(1024)
                        j = j + 1
                    print(pcc_gc_backend4_evacuated_bytes() >= 256)
                    print(pcc_gc_relocation_set_size() == 0)
                    print(pcc_gc_backend4_zpage_count() > 0)

                if __name__ == "__main__":
                    main()
                """
            ).lstrip(),
            encoding="utf-8",
        )
        probe_compile_cmd = [
            str(PCC1),
            "--backend",
            "self",
            "--python-libpython",
            "off",
            "--ir-scaffold",
            "on",
            str(probe_src),
            "-o",
            str(probe_exe),
        ]
        probe_compile_proc = subprocess.run(
            probe_compile_cmd,
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(REPO),
            env=env,
        )
        assert probe_compile_proc.returncode == 0, (
            "pcc1 backend4 ZPage telemetry compile failed "
            f"(exit {probe_compile_proc.returncode}):\n"
            f"cmd: {' '.join(probe_compile_cmd)}\n"
            f"stdout:\n{probe_compile_proc.stdout}\n"
            f"stderr:\n{probe_compile_proc.stderr}"
        )
        probe_run_proc = subprocess.run(
            [str(probe_exe)],
            capture_output=True,
            text=True,
            timeout=30,
            env=env,
        )
        assert probe_run_proc.returncode == 0, (
            "pcc1-built backend4 ZPage telemetry binary failed "
            f"(exit {probe_run_proc.returncode}):\n"
            f"stdout:\n{probe_run_proc.stdout}\n"
            f"stderr:\n{probe_run_proc.stderr}"
        )
        assert probe_run_proc.stdout.strip().splitlines() == [
            "4",
            "True",
            "True",
            "True",
            "True",
            "True",
            "True",
        ]
    finally:
        _verify_isolated_runtime_archive(threaded_pcc_runtime_archive)
