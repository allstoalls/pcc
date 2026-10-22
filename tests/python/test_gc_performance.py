"""GC performance ratchet — locks the cost ceiling for memory-management
hot paths so regressions are visible.

Measurements observe the *runtime* (compiled-binary) cost, not the
compile cost. Each test sets a generous ceiling appropriate for the
current refcount/cycle runtime on macOS arm64 / Linux x86_64.

Pattern: each test compiles a self-contained pcc program that runs a
fixed N-iteration workload, measures wall time of the binary, and
asserts ``elapsed < ceiling``. Ceilings are chosen so the test passes
on a 5-year-old laptop with margin; they're regression detectors,
not benchmarks.

When the cycle collector or other GC backends land, add a parallel
file ``test_gc_performance_<backend>.py`` rather than overloading
this one — keeps ratchet history per backend.
"""
from __future__ import annotations

import os
import subprocess
import textwrap
import time

def _compile_program(tmp_path, source: str):
    """Compile a pcc-Python program and return the executable path."""
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    compile_python(str(src), str(exe), ir_scaffold_mode="on")
    return exe


def _run_exe_timed(exe, *, env: dict[str, str] | None = None) -> tuple[float, str]:
    """Run a compiled pcc-Python program and return (wall_seconds, stdout)."""
    run_env = None
    if env is not None:
        run_env = os.environ.copy()
        run_env.update(env)
    # These are hot-path GC ratchets, not first-launch loader benchmarks.
    # A freshly linked Mach-O can pay one-time validation/loader cost on
    # macOS, which has shown up as multi-second noise in otherwise tiny
    # gc.collect() programs. Verify a warmup run, then time the steady run.
    warmup = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=120, env=run_env,
    )
    if warmup.returncode != 0:
        raise RuntimeError(
            f"binary failed during warmup (rc={warmup.returncode}):\n"
            f"{warmup.stderr}"
        )

    t0 = time.perf_counter()
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=120, env=run_env,
    )
    elapsed = time.perf_counter() - t0
    if result.returncode != 0:
        raise RuntimeError(
            f"binary failed (rc={result.returncode}):\n{result.stderr}"
        )
    return elapsed, result.stdout.strip()


def _compile_and_run_timed(tmp_path, source: str) -> tuple[float, str]:
    """Compile a pcc-Python program and return (wall_seconds, stdout)."""
    return _run_exe_timed(_compile_program(tmp_path, source))


# ---------------------------------------------------------------------------
# Allocation throughput — refcount + dealloc round-trip
# ---------------------------------------------------------------------------


def test_allocation_throughput_class_instances(tmp_path):
    """1M class instances should allocate + dealloc in under 5s.

    Stresses ``pcc_gc_alloc`` (or its refcount shim today) +
    ``pcc_gc_release`` cascade through the dealloc switch."""
    elapsed, out = _compile_and_run_timed(tmp_path, """
        class Box:
            pass

        def main() -> None:
            n: int = 1_000_000
            i: int = 0
            while i < n:
                _ = Box()
                i = i + 1
            print("done")

        if __name__ == "__main__":
            main()
        """)
    assert out == "done"
    assert elapsed < 5.0, f"1M class instance alloc/free took {elapsed:.2f}s"


def test_allocation_throughput_strings(tmp_path):
    """1M unique str literals should not blow up runtime — tests
    ``py_str_new`` + decref path."""
    elapsed, out = _compile_and_run_timed(tmp_path, """
        def main() -> None:
            n: int = 1_000_000
            i: int = 0
            while i < n:
                _ = "k" + str(i)
                i = i + 1
            print("done")

        if __name__ == "__main__":
            main()
        """)
    assert out == "done"
    assert elapsed < 8.0, f"1M str alloc took {elapsed:.2f}s"


def test_allocation_throughput_tuples(tmp_path):
    """500k 3-tuples — tests ``py_tuple_new`` + dealloc + element
    refcount fan-out."""
    elapsed, out = _compile_and_run_timed(tmp_path, """
        def main() -> None:
            n: int = 500_000
            i: int = 0
            while i < n:
                _ = (i, i + 1, i + 2)
                i = i + 1
            print("done")

        if __name__ == "__main__":
            main()
        """)
    assert out == "done"
    assert elapsed < 5.0, f"500k tuple alloc took {elapsed:.2f}s"


def test_allocation_throughput_lists(tmp_path):
    """100k small lists — tests ``py_list_new`` + per-element append."""
    elapsed, out = _compile_and_run_timed(tmp_path, """
        def main() -> None:
            n: int = 100_000
            i: int = 0
            while i < n:
                xs = [i, i+1, i+2, i+3, i+4]
                xs.append(i + 5)
                i = i + 1
            print("done")

        if __name__ == "__main__":
            main()
        """)
    assert out == "done"
    assert elapsed < 5.0, f"100k list alloc took {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# Decref cascade — release a long chain without stack overflow
# ---------------------------------------------------------------------------


def test_long_chain_decref_no_stack_overflow(tmp_path):
    """A linked list of 100k nodes must release without recursing
    100k deep into ``py_decref``. If the runtime uses an iterative
    dealloc trampoline this is fast and stack-safe; recursive dealloc
    would either overflow or take pathologically long."""
    elapsed, out = _compile_and_run_timed(tmp_path, """
        class Node:
            def __init__(self, v):
                self.v = v
                self.next = None

        def build_chain(n):
            head = Node(0)
            cur = head
            i: int = 1
            while i < n:
                cur.next = Node(i)
                cur = cur.next
                i = i + 1
            return head

        def main() -> None:
            head = build_chain(100_000)
            head = None  # drop entire chain
            print("released")

        if __name__ == "__main__":
            main()
        """)
    assert out == "released"
    assert elapsed < 3.0, f"100k chain decref took {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# Refcount overhead in tight loops
# ---------------------------------------------------------------------------


def test_local_rebind_overhead(tmp_path):
    """5M local-variable rebinds. Each rebind today is decref(old) +
    incref(new) at the abstraction layer (or noop on tagged-int)."""
    elapsed, out = _compile_and_run_timed(tmp_path, """
        def main() -> None:
            n: int = 5_000_000
            x = "hello"
            y = "world"
            i: int = 0
            while i < n:
                x = y
                y = x
                i = i + 1
            print(x)

        if __name__ == "__main__":
            main()
        """)
    assert out == "world"
    assert elapsed < 3.0, f"5M rebind took {elapsed:.2f}s"


def test_field_assignment_overhead(tmp_path):
    """500k ``self.attr = x`` cycles — tests ``pcc_gc_store_ptr``
    (or its refcount inline today)."""
    elapsed, out = _compile_and_run_timed(tmp_path, """
        class Box:
            def __init__(self):
                self.v = None

        def main() -> None:
            b = Box()
            n: int = 500_000
            i: int = 0
            while i < n:
                b.v = i      # repeatedly retarget field
                i = i + 1
            print(b.v)

        if __name__ == "__main__":
            main()
        """)
    assert out == "499999"
    assert elapsed < 3.0, f"500k field-assign took {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# gc.collect() overhead — must be cheap on a heap with no cycles
# ---------------------------------------------------------------------------


def test_gc_collect_no_cycles_overhead(tmp_path):
    """``gc.collect()`` on a clean heap (no cycles) should be O(1)
    or near-zero — refcount already reclaimed everything. Generous
    ceiling 100ms."""
    elapsed, out = _compile_and_run_timed(tmp_path, """
        import gc

        def main() -> None:
            t = ""
            i: int = 0
            while i < 1000:
                t = t + str(i)
                i = i + 1
            n = gc.collect()    # nothing to collect — refcount got it
            print(n)

        if __name__ == "__main__":
            main()
        """)
    # Whole program incl. compile latency ceiling — collect() itself
    # should be <1ms but we measure end-to-end wall.
    assert elapsed < 0.5, f"gc.collect() noop took {elapsed:.2f}s"
    assert out == "0"


def test_gc_collect_cycle_throughput(tmp_path):
    """One explicit collect drains all 10k two-node cycles.

    Incremental scheduler work may be batch-bounded, but ``gc.collect()``
    must neither stop after the first 1024 candidates nor defer the remainder
    to later calls.  Check both the reported count and the empty second pass.
    """
    elapsed, out = _compile_and_run_timed(tmp_path, """
        import gc

        class Node:
            pass

        def make_cycles(n):
            i: int = 0
            while i < n:
                a = Node()
                b = Node()
                a.peer = b
                b.peer = a
                i = i + 1

        def main() -> None:
            make_cycles(10_000)
            n = gc.collect()
            print(n >= 20_000)   # expect 2 nodes per cycle
            print(gc.collect() == 0)

        if __name__ == "__main__":
            main()
        """)
    assert out == "True\nTrue"
    assert elapsed < 2.0, f"10k-cycle collect took {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# Steady-state memory — workload-in-loop should not grow unboundedly
# ---------------------------------------------------------------------------


def test_steady_state_no_leak_refcount_path(tmp_path):
    """100k iterations of "build + drop" should not exhaust memory.
    Negative-leak baseline for the refcount path (cycle-free).

    The test detects leaks indirectly: if dealloc isn't actually
    reclaiming, the loop slows down due to malloc fragmentation /
    swapping. A clean run finishes in single-digit seconds."""
    elapsed, out = _compile_and_run_timed(tmp_path, """
        def make_and_drop():
            d = {}
            xs = []
            i: int = 0
            while i < 100:
                d["k" + str(i)] = i
                xs.append(i)
                i = i + 1
            return None  # drop everything

        def main() -> None:
            n: int = 100_000
            i: int = 0
            while i < n:
                make_and_drop()
                i = i + 1
            print("done")

        if __name__ == "__main__":
            main()
        """)
    assert out == "done"
    assert elapsed < 15.0, f"100k make/drop iterations took {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# Immortal-singleton fast path — must be O(1) per touch
# ---------------------------------------------------------------------------


def test_immortal_singleton_touch_throughput(tmp_path):
    """10M touches of None / True / False — refcount path skips
    incref/decref via PY_FLAG_IMMORTAL. Should be near pure loop
    overhead."""
    elapsed, out = _compile_and_run_timed(tmp_path, """
        def main() -> None:
            n: int = 10_000_000
            x = None
            i: int = 0
            while i < n:
                x = None
                x = True
                x = False
                i = i + 1
            print(x is False)

        if __name__ == "__main__":
            main()
        """)
    assert out == "True"
    assert elapsed < 3.0, f"10M immortal touches took {elapsed:.2f}s"


# ---------------------------------------------------------------------------
# Cross-backend ratchet placeholder
# ---------------------------------------------------------------------------


def test_tracing_backend_steady_state_matches_refcount(tmp_path):
    """Backend #1 should run a cycle-free steady-state workload in the
    same broad performance class as backend #0.

    This replaces the old NotImplementedError placeholder with a real
    measurement. The production performance matrix in goal.md remains
    stricter; this test is the per-commit regression ratchet.
    """
    exe = _compile_program(tmp_path, """
        from pcc.extern import extern, c_int64

        pcc_gc_backend = extern("pcc_gc_backend", (), c_int64)

        class Box:
            def __init__(self, v: int):
                self.v = v

        def make_and_drop() -> None:
            xs = []
            i: int = 0
            while i < 50:
                xs.append(Box(i))
                i = i + 1

        def main() -> None:
            n: int = 5_000
            i: int = 0
            while i < n:
                make_and_drop()
                i = i + 1
            print(pcc_gc_backend())
            print("done")

        if __name__ == "__main__":
            main()
        """)

    ref_elapsed, ref_out = _run_exe_timed(exe, env={"PCC_GC_BACKEND": "0"})
    trace_elapsed, trace_out = _run_exe_timed(
        exe,
        env={
            "PCC_GC_BACKEND": "1",
            "PCC_GC_DEBT_THRESHOLD": "4096",
            "PCC_GC_PAUSE": "200",
            "PCC_GC_STEPMUL": "200",
        },
    )
    assert ref_out.splitlines() == ["0", "done"]
    assert trace_out.splitlines() == ["1", "done"]
    ceiling = max(ref_elapsed * 2.5, ref_elapsed + 1.0)
    assert trace_elapsed < ceiling, (
        f"backend #1 steady-state {trace_elapsed:.2f}s exceeded "
        f"backend #0 {ref_elapsed:.2f}s budget {ceiling:.2f}s"
    )
