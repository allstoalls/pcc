from __future__ import annotations

import os
import subprocess
import textwrap
from unittest import mock


def _compile_probe(
    tmp_path,
    source: str,
    *,
    runtime_cc: str | None = None,
    runtime_high: str | None = None,
    backend: str | None = None,
    ir_scaffold_mode: str | None = "on",
):
    from pcc.py_frontend.pipeline import compile_python

    src = tmp_path / "probe.py"
    exe = tmp_path / "probe.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    env = {}
    if runtime_cc is not None:
        env["PCC_RUNTIME_CC"] = runtime_cc
    if runtime_high is not None:
        env["PCC_RUNTIME_HIGH"] = runtime_high
    with mock.patch.dict(os.environ, env, clear=False):
        compile_python(
            str(src),
            str(exe),
            ir_scaffold_mode=ir_scaffold_mode,
            libpython_mode="off",
            backend=backend,
        )
    return exe


# PCC_GC_DEFAULT_DEBT_THRESHOLD in pcc/py_runtime/src/py_gc_backend.c.  The
# tuned value below is 16x smaller, which is 16x more incremental steps for the
# same allocation volume, because a step fires when debt reaches the threshold.
_DEFAULT_DEBT_THRESHOLD = 65536
_TUNED_DEBT_THRESHOLD = 4096


def _assert_debt_paced_steps(steps: int, allocs: int) -> None:
    """The collector must pace on debt, not on allocations.

    Two bounds, because one alone does not say much.  The ratio is the
    behavioural one: the failure worth catching is a step per allocation, which
    makes ``steps`` approach ``allocs``.  The absolute bound is the original
    ``steps < 500``, scaled by the threshold ratio -- it was written against
    the 65536-byte default and commit e02ae2cc switched both churn probes to
    the 4096-byte tuned threshold without recalibrating it, so it became
    unsatisfiable arithmetic rather than a regression signal.
    """
    assert steps > 0
    assert steps < allocs // 10, (steps, allocs)
    scaled = 500 * (_DEFAULT_DEBT_THRESHOLD // _TUNED_DEBT_THRESHOLD)
    assert steps < scaled, (steps, scaled)


def _run_with_backend_one(exe, *, tuned: bool = True):
    env = os.environ.copy()
    env["PCC_GC_BACKEND"] = "1"
    if tuned:
        env.update(
            {
                "PCC_GC_DEBT_THRESHOLD": str(_TUNED_DEBT_THRESHOLD),
                "PCC_GC_PAUSE": "200",
                "PCC_GC_STEPMUL": "200",
            }
        )
    return subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
        env=env,
    )


def _explicit_collect_sweep_probe() -> str:
    return """
        from pcc.extern import extern, c_int32, c_int64, c_ptr, c_void, c_obj
        from pcc.unsafe import free, malloc, store_i32, store_ptr

        pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_obj)
        pcc_gc_collect = extern("pcc_gc_collect", (c_int32,), c_int64)
        pcc_gc_frame_enter = extern("pcc_gc_frame_enter", (c_ptr, c_ptr), c_void)
        pcc_gc_frame_leave = extern("pcc_gc_frame_leave", (c_ptr,), c_void)
        pcc_gc_has_tracing_sweep = extern("pcc_gc_has_tracing_sweep", (), c_int64)
        pcc_gc_release = extern("pcc_gc_release", (c_ptr,), c_void)

        def main() -> None:
            live = pcc_gc_alloc(64, 5, 0)
            dead = pcc_gc_alloc(64, 5, 0)
            frame_map = malloc(4)
            slots = malloc(8)
            store_i32(frame_map, 0, 1)
            store_ptr(slots, 0, live)
            pcc_gc_frame_enter(frame_map, slots)
            print(pcc_gc_collect(0) > 0)
            print(pcc_gc_has_tracing_sweep())
            pcc_gc_frame_leave(slots)
            pcc_gc_release(live)
            free(frame_map)
            free(slots)

        if __name__ == "__main__":
            main()
        """


def test_incremental_backend_env_selects_default_and_small_alloc_is_not_a_step(
    tmp_path,
):
    exe = _compile_probe(
        tmp_path,
        """
        from pcc.extern import extern, c_int32, c_int64, c_ptr, c_void, c_obj

        pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_obj)
        pcc_gc_release = extern("pcc_gc_release", (c_ptr,), c_void)
        pcc_gc_backend = extern("pcc_gc_backend", (), c_int64)
        pcc_gc_telemetry = extern("pcc_gc_telemetry", (c_int64,), c_int64)
        pcc_gc_telemetry_reset = extern("pcc_gc_telemetry_reset", (), c_void)

        def main() -> None:
            print(pcc_gc_backend())
            pcc_gc_telemetry_reset()
            o = pcc_gc_alloc(24, 2, 0)
            print(pcc_gc_telemetry(0))
            print(pcc_gc_telemetry(5))
            print(pcc_gc_telemetry(7) >= 0)
            pcc_gc_release(o)

        if __name__ == "__main__":
            main()
        """,
    )
    result = _run_with_backend_one(exe)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["1", "1", "0", "True"]


def test_incremental_backend_debt_threshold_triggers_bounded_real_steps(tmp_path):
    exe = _compile_probe(
        tmp_path,
        """
        from pcc.extern import extern, c_int32, c_int64, c_ptr, c_void, c_obj

        pcc_gc_alloc = extern("pcc_gc_alloc", (c_int64, c_int32, c_int32), c_obj)
        pcc_gc_release = extern("pcc_gc_release", (c_ptr,), c_void)
        pcc_gc_telemetry = extern("pcc_gc_telemetry", (c_int64,), c_int64)
        pcc_gc_telemetry_reset = extern("pcc_gc_telemetry_reset", (), c_void)

        def main() -> None:
            pcc_gc_telemetry_reset()
            i: int = 0
            while i < 500:
                o = pcc_gc_alloc(64, 2, 0)
                pcc_gc_release(o)
                i = i + 1
            print(pcc_gc_telemetry(0))
            print(pcc_gc_telemetry(5) > 0)
            print(pcc_gc_telemetry(5) < 200)
            print(pcc_gc_telemetry(6) >= 0)
            print(pcc_gc_telemetry(7) < 50000)

        if __name__ == "__main__":
            main()
        """,
    )
    result = _run_with_backend_one(exe)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == [
        "500",
        "True",
        "True",
        "True",
        "True",
    ]


def test_incremental_backend_collects_container_churn_under_pcc_python_runtime(
    tmp_path,
):
    exe = _compile_probe(
        tmp_path,
        """
        from pcc.extern import extern, c_int64

        pcc_gc_backend = extern("pcc_gc_backend", (), c_int64)
        pcc_gc_telemetry = extern("pcc_gc_telemetry", (c_int64,), c_int64)

        def churn(n: int) -> int:
            total: int = 0
            i: int = 0
            while i < n:
                xs = [i, i + 1, i + 2]
                ys = {"x": xs, "i": i}
                if ys["x"][1] == i + 1:
                    total = total + ys["i"]
                i = i + 1
            return total

        def main() -> None:
            import gc
            print("backend", pcc_gc_backend())
            print("sum", churn(20000))
            print("collect", gc.collect() >= 0)
            print("allocs", pcc_gc_telemetry(0))
            print("steps", pcc_gc_telemetry(5))
            print("debt", pcc_gc_telemetry(6))

        if __name__ == "__main__":
            main()
        """,
        runtime_cc="pcc",
        runtime_high="py",
        backend="self",
        ir_scaffold_mode=None,
    )
    result = _run_with_backend_one(exe, tuned=True)
    assert (
        result.returncode == 0
    ), f"rc={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    lines = result.stdout.strip().splitlines()
    assert lines[:3] == ["backend 1", "sum 199990000", "collect True"]
    allocs = int(lines[3].split()[1])
    steps = int(lines[4].split()[1])
    debt = int(lines[5].split()[1])
    # Two managed containers per iteration is the floor; boxed elements may or
    # may not be counted here depending on the int lane, so this is a lower
    # bound and not an equality.
    assert allocs >= 2 * 20000, allocs
    _assert_debt_paced_steps(steps, allocs)
    assert debt < _DEFAULT_DEBT_THRESHOLD


def test_incremental_backend_keeps_open_file_rooted_across_container_churn(tmp_path):
    data = tmp_path / "incremental-live-file.txt"
    exe = _compile_probe(
        tmp_path,
        f"""
        import gc

        PATH = {str(data)!r}

        def main() -> None:
            counters = {{"0": [0, 1]}}
            with open(PATH, "w", encoding="utf-8") as f:
                i: int = 1
                while i < 20000:
                    counters[str(i % 31)] = [i, i + 1]
                    if i % 127 == 0:
                        gc.collect()
                    i = i + 1
                for key in counters:
                    f.write(key)
                    f.write(":")
                    f.write(str(counters[key][0]))
                    f.write("\\n")
                gc.collect()
                f.write("after\\n")
            print("ok")

        if __name__ == "__main__":
            main()
        """,
        runtime_cc="pcc",
        runtime_high="py",
        backend="self",
        ir_scaffold_mode=None,
    )
    result = _run_with_backend_one(exe, tuned=True)
    assert (
        result.returncode == 0
    ), f"rc={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert result.stdout == "ok\n"
    assert data.read_text(encoding="utf-8").endswith("after\n")


def test_incremental_backend_keeps_profile_file_rooted_with_many_live_locals(
    tmp_path,
):
    data = tmp_path / "incremental-profile-file.json"
    exe = _compile_probe(
        tmp_path,
        f"""
        import gc

        PATH = {str(data)!r}

        def write_profile(path: str) -> None:
            entry = "cli_bootstrap"
            phase = "python-frontend"
            emit_llvm = False
            output_path = "/tmp/output"
            counters = {{}}
            phase_totals = {{"compile_python_total": 1}}
            events = [{{"name": "compile", "ms": 1}}]
            total_ms = 1
            scratch0 = [entry]
            scratch1 = {{"phase": phase}}
            scratch2 = [emit_llvm, output_path]
            scratch3 = {{"events": events}}
            scratch4 = [phase_totals]
            scratch5 = {{"total": total_ms}}
            scratch6 = [scratch0, scratch1]
            scratch7 = {{"nested": scratch2}}
            # A full pcc1 compile exhausts many native dict iterators before
            # profile serialization.  Reproduce that GC/StopIteration state
            # in a small program instead of requiring a bootstrap-sized heap.
            exhausted_total: int = 0
            exhausted_round: int = 0
            while exhausted_round < 50000:
                exhausted = {{"a": exhausted_round, "b": exhausted_round + 1}}
                for exhausted_key in exhausted:
                    exhausted_total = exhausted_total + exhausted[exhausted_key]
                exhausted_round = exhausted_round + 1
            i: int = 0
            while i < 146:
                counters["counter_" + str(i)] = i
                i = i + 1
            with open(path, "w", encoding="utf-8") as f:
                f.write("{{\\n")
                first_counter = True
                for key in counters:
                    if not first_counter:
                        f.write(",\\n")
                    first_counter = False
                    f.write('  "' + key + '": ' + str(counters[key]))
                    gc.collect()
                f.write("\\n}}\\n")
            print(
                len(scratch6)
                + len(scratch7)
                + len(scratch3)
                + len(scratch4)
                + (1 if exhausted_total > 0 else 0)
            )

        def main() -> None:
            write_profile(PATH)
            print("ok")

        if __name__ == "__main__":
            main()
        """,
        runtime_cc="pcc",
        runtime_high="py",
        backend="self",
        ir_scaffold_mode=None,
    )
    result = _run_with_backend_one(exe, tuned=True)
    assert (
        result.returncode == 0
    ), f"rc={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert result.stdout == "6\nok\n"
    assert data.read_text(encoding="utf-8").endswith("\n}\n")


def test_incremental_backend_pcc_python_reports_pause_budget_under_churn(tmp_path):
    exe = _compile_probe(
        tmp_path,
        """
        from pcc.extern import extern, c_int64, c_void

        pcc_gc_backend = extern("pcc_gc_backend", (), c_int64)
        pcc_gc_telemetry = extern("pcc_gc_telemetry", (c_int64,), c_int64)
        pcc_gc_telemetry_reset = extern("pcc_gc_telemetry_reset", (), c_void)

        def churn(n: int) -> int:
            total: int = 0
            i: int = 0
            while i < n:
                xs = [i, i + 1, i + 2, i + 3]
                ys = {"x": xs, "i": i, "j": i + 3}
                if ys["x"][2] == i + 2:
                    total = total + ys["i"] + ys["j"]
                i = i + 1
            return total

        def main() -> None:
            pcc_gc_telemetry_reset()
            print("backend", pcc_gc_backend())
            print("sum", churn(20000))
            print("steps", pcc_gc_telemetry(5))
            print("pause", pcc_gc_telemetry(7))
            print("allocs", pcc_gc_telemetry(0))

        if __name__ == "__main__":
            main()
        """,
        runtime_cc="pcc",
        runtime_high="py",
        backend="self",
        ir_scaffold_mode=None,
    )
    result = _run_with_backend_one(exe, tuned=True)
    assert (
        result.returncode == 0
    ), f"rc={result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    lines = result.stdout.strip().splitlines()
    assert lines[:2] == ["backend 1", "sum 400040000"]
    steps = int(lines[2].split()[1])
    pause_us = int(lines[3].split()[1])
    allocs = int(lines[4].split()[1])
    assert allocs >= 2 * 20000, allocs
    _assert_debt_paced_steps(steps, allocs)
    assert 0 <= pause_us < 50000


def test_incremental_backend_explicit_collect_preserves_owned_locals_and_live_roots(
    tmp_path,
):
    exe = _compile_probe(tmp_path, _explicit_collect_sweep_probe())
    result = _run_with_backend_one(exe)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["False", "0"]


def test_incremental_backend_pcc_python_explicit_collect_preserves_owned_locals_and_live_roots(
    tmp_path,
):
    exe = _compile_probe(
        tmp_path,
        _explicit_collect_sweep_probe(),
        runtime_cc="pcc",
        runtime_high="py",
        backend="self",
        ir_scaffold_mode=None,
    )
    result = _run_with_backend_one(exe)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["False", "0"]
