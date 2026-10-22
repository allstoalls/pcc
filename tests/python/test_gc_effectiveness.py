"""GC effectiveness — does memory actually get reclaimed.

Performance ratchet (``test_gc_performance.py``) catches "GC ran
too slow". This file catches "GC didn't run / didn't reclaim". The
two are complementary: a fast GC that leaks is just as broken as a
slow GC that doesn't.

Observation channels (in order of preference):
1. ``gc.collect()`` returns reclaimed-object count (G5 — exposed
   in `pcc_gc_collect`).
2. ``__del__`` side-effect counter (G2 — needs finalizer dispatch).
3. Process RSS via parent-side measurement (works without G2/G5,
   but requires the test to fork a child whose RSS we can poll).

The current suite uses the strongest available observation for each
scenario and should not carry hidden expected-failure debt.
"""
from __future__ import annotations

import os
import subprocess
import textwrap

import pytest

BACKEND_TRICOLOR = 1
BACKEND_CONCURRENT = 2
BACKEND_GENERATIONAL = 3
BACKEND_COLORED_RELOCATING = 4


def _compile_and_run(tmp_path, source: str) -> subprocess.CompletedProcess[str]:
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    compile_python(str(src), str(exe), ir_scaffold_mode="on")
    return subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=120,
    )


def _compile_and_run_capture_rss(
    tmp_path, source: str,
) -> tuple[subprocess.CompletedProcess[str], int]:
    """Run the compiled program and capture its peak RSS in KiB.

    Uses ``/usr/bin/time -l`` (macOS) / ``time -v`` (Linux) to
    extract maximum-resident-set-size. Returns (proc_result, rss_kib).
    On platforms where time-l isn't available, returns (-1) for rss.
    """
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    compile_python(str(src), str(exe), ir_scaffold_mode="on")

    cmd = ["/usr/bin/time", "-l", str(exe)]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, timeout=300,
    )
    rss_kib = -1
    for line in proc.stderr.splitlines():
        line = line.strip()
        if "maximum resident set size" in line.lower():
            try:
                rss_bytes = int(line.split()[0])
                # macOS reports bytes; Linux reports kilobytes.
                rss_kib = rss_bytes // 1024 if rss_bytes > 1_000_000 else rss_bytes
            except (ValueError, IndexError):
                pass
            break
    # Re-package as a CompletedProcess on the binary's stdout/stderr,
    # not /usr/bin/time's wrapper output.
    main_proc = subprocess.CompletedProcess(
        args=[str(exe)],
        returncode=proc.returncode,
        stdout=proc.stdout,
        stderr="",
    )
    return main_proc, rss_kib


# ---------------------------------------------------------------------------
# Refcount path: non-cyclic data must be reclaimed
# ---------------------------------------------------------------------------


def test_non_cyclic_releases_correctly(tmp_path):
    """A workload that builds and drops 100k objects should not run
    out of memory. Negative-leak baseline for refcount path."""
    result = _compile_and_run(tmp_path, """
        def make_drop():
            xs = []
            i: int = 0
            while i < 100:
                xs.append("v" + str(i))
                i = i + 1
            return None  # drop everything

        def main() -> None:
            n: int = 100_000
            i: int = 0
            while i < n:
                make_drop()
                i = i + 1
            print("ok")

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


@pytest.mark.pcc_gate(
    unavailable=None
    if os.path.exists("/usr/bin/time")
    else "needs /usr/bin/time -l for RSS measurement"
)
def test_non_cyclic_rss_plateaus(tmp_path):
    """100k iterations of build + drop a 100-string list. Scope exit
    should release every string; peak RSS should plateau around the
    workload steady-state size, not climb linearly with iteration count.

    Observation: today RSS is ~1.5 GB at 100k iterations and ~15 GB
    at 1M (linear), proving strings are leaking. Threshold 200 MB is
    the post-fix target."""
    result, rss_kib = _compile_and_run_capture_rss(tmp_path, """
        def make_drop():
            xs = []
            i: int = 0
            while i < 100:
                xs.append("v" + str(i))
                i = i + 1
            return None

        def main() -> None:
            n: int = 100_000
            i: int = 0
            while i < n:
                make_drop()
                i = i + 1
            print("ok")

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
    if rss_kib > 0:
        # Post-fix expectation: < 200 MB. Today: ~1.5 GB.
        assert rss_kib < 200_000, f"peak RSS {rss_kib} KiB suggests leak"


def test_long_chain_full_release(tmp_path):
    """Build a 50k-deep linked list, drop the head — every node must
    be released, dealloc must not stack-overflow."""
    result = _compile_and_run(tmp_path, """
        class N:
            def __init__(self, v):
                self.v = v
                self.next = None

        def main() -> None:
            n: int = 50_000
            head = N(0)
            cur = head
            i: int = 1
            while i < n:
                cur.next = N(i)
                cur = cur.next
                i = i + 1
            cur = None
            head = None  # entire chain dies
            print("released")

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "released"


# ---------------------------------------------------------------------------
# Cycle collector recall — finds all expected cycles
# ---------------------------------------------------------------------------


def test_cycle_collect_finds_simple_cycles(tmp_path):
    """Build 100 disjoint 2-cycles; gc.collect() must return ≥ 200
    (each cycle has 2 nodes)."""
    result = _compile_and_run(tmp_path, """
        import gc

        class N:
            pass

        def make_cycles(k):
            i: int = 0
            while i < k:
                a = N()
                b = N()
                a.peer = b
                b.peer = a
                i = i + 1

        def main() -> None:
            make_cycles(100)
            n = gc.collect()
            print(n >= 200)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


@pytest.mark.parametrize(
    "backend",
    [
        BACKEND_TRICOLOR,
        BACKEND_CONCURRENT,
        BACKEND_GENERATIONAL,
        BACKEND_COLORED_RELOCATING,
    ],
)
def test_non_default_backends_collect_list_cycle(tmp_path, backend):
    """Non-default collectors should reclaim a simple self-referential list."""
    result = _compile_and_run(tmp_path, f"""
        from pcc.extern import extern, c_int32, c_int64

        pcc_gc_set_backend = extern('pcc_gc_set_backend', (c_int64,), c_int64)
        pcc_gc_collect = extern('pcc_gc_collect', (c_int32,), c_int64)

        def make_cycle() -> None:
            xs = []
            xs.append(xs)

        def main() -> None:
            pcc_gc_set_backend({backend})
            make_cycle()
            print(pcc_gc_collect(0) > 0)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


@pytest.mark.parametrize(
    "backend",
    [
        BACKEND_TRICOLOR,
        BACKEND_CONCURRENT,
        BACKEND_GENERATIONAL,
        BACKEND_COLORED_RELOCATING,
    ],
)
def test_non_default_backends_collect_cross_type_cycle(tmp_path, backend):
    """Non-default collectors should reclaim class/list/dict/tuple mixed cycles."""
    result = _compile_and_run(tmp_path, f"""
        from pcc.extern import extern, c_int32, c_int64

        pcc_gc_set_backend = extern('pcc_gc_set_backend', (c_int64,), c_int64)
        pcc_gc_collect = extern('pcc_gc_collect', (c_int32,), c_int64)

        class Holder:
            pass

        def make() -> None:
            h = Holder()
            d = {{}}
            t = (d, h)
            xs = [t]
            d['xs'] = xs
            h.tuple = t

        def main() -> None:
            pcc_gc_set_backend({backend})
            make()
            print(pcc_gc_collect(0) > 0)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


def test_cycle_collects_self_referential_list(tmp_path):
    result = _compile_and_run(tmp_path, """
        import gc
        from pcc.extern import extern, c_int32, c_int64

        pcc_gc_get_count = extern('py_gc_get_count', (c_int32,), c_int64)

        def make_cycle():
            xs = []
            xs.append(xs)

        def main() -> None:
            baseline = pcc_gc_get_count(0)
            make_cycle()
            n = gc.collect()
            print(n >= 1)
            print(pcc_gc_get_count(0) == baseline)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["True", "True"]


def test_cycle_collector_keeps_reachable_cycle(tmp_path):
    result = _compile_and_run(tmp_path, """
        import gc

        def main() -> None:
            xs = []
            xs.append(xs)
            n = gc.collect()
            print(n)
            print(gc.is_tracked(xs))

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["0", "True"]


def test_cycle_through_list_collected(tmp_path):
    """``xs.append(xs)`` cycle through a list."""
    result = _compile_and_run(tmp_path, """
        import gc

        def make():
            xs = [1, 2, 3]
            xs.append(xs)
            return None

        def main() -> None:
            make()
            n = gc.collect()
            print(n >= 1)

        if __name__ == "__main__":
            main()
        """)
    assert result.stdout.strip() == "True"


def test_cycle_through_dict_collected(tmp_path):
    """``d['k'] = d`` cycle through a dict."""
    result = _compile_and_run(tmp_path, """
        import gc

        def make():
            d = {}
            d["self"] = d
            return None

        def main() -> None:
            make()
            n = gc.collect()
            print(n >= 1)

        if __name__ == "__main__":
            main()
        """)
    assert result.stdout.strip() == "True"


def test_tuple_unpack_instance_return_no_growth(tmp_path):
    """Tuple unpack assignment from instance-returning tuples should not
    retain prior iteration values after re-assignment."""
    result = _compile_and_run(tmp_path, """
        import gc
        from pcc.extern import extern, c_int32, c_int64

        pcc_gc_get_count = extern('py_gc_get_count', (c_int32,), c_int64)

        class A:
            pass

        def make():
            return A(), A()

        def main() -> None:
            baseline = pcc_gc_get_count(0)
            i = 0
            while i < 20:
                x, y = make()
                i = i + 1
            print(pcc_gc_get_count(0) - baseline)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "2"


def test_tuple_unpack_dict_self_cycle_reclaims_between_iterations(tmp_path):
    """Repeated tuple-unpack iterations over dict payload cycles should
    reclaim previous cycles each iteration when collection runs."""
    result = _compile_and_run(tmp_path, """
        import gc
        from pcc.extern import extern, c_int32, c_int64

        pcc_gc_get_count = extern('py_gc_get_count', (c_int32,), c_int64)

        class A:
            pass

        def make():
            d = {}
            d['x'] = A()
            d['self'] = d
            return d, A()

        def main() -> None:
            baseline = pcc_gc_get_count(0)
            i = 0
            while i < 10:
                x, y = make()
                gc.collect()
                print(pcc_gc_get_count(0) - baseline)
                i = i + 1

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().split("\n")
    assert lines == ["3"] * 10


def test_cross_type_cycle_collected(tmp_path):
    """list → dict → tuple-element → instance.attr → list cycle."""
    result = _compile_and_run(tmp_path, """
        import gc

        class Holder:
            pass

        def make():
            h = Holder()
            d = {}
            t = (d, h)
            xs = [t]
            d["xs"] = xs
            h.tuple = t          # closes the loop
            return None

        def main() -> None:
            make()
            n = gc.collect()
            print(n > 0)

        if __name__ == "__main__":
            main()
        """)
    assert result.stdout.strip() == "True"


def test_closure_cell_cycle_collected(tmp_path):
    """A native closure function captured by a list it references."""
    result = _compile_and_run(tmp_path, """
        import gc

        def make():
            payload = []
            def inner():
                return payload
            payload.append(inner)
            return None

        def main() -> None:
            make()
            n = gc.collect()
            print(n >= 1)

        if __name__ == "__main__":
            main()
        """)
    assert result.stdout.strip() == "True"


def test_generator_referencing_self_collected(tmp_path):
    """A generator object stashed inside an attribute that the
    generator's frame can reach."""
    result = _compile_and_run(tmp_path, """
        import gc

        class Box:
            pass

        def make():
            box = Box()
            def gen():
                yield box
            g = gen()
            box.gen = g
            next(g)
            return None

        def main() -> None:
            make()
            n = gc.collect()
            print(n > 0)

        if __name__ == "__main__":
            main()
        """)
    assert result.stdout.strip() == "True"


# ---------------------------------------------------------------------------
# Cycle collector precision — no false positives on live data
# ---------------------------------------------------------------------------


def test_collect_does_not_break_live_cycle(tmp_path):
    """A live cycle (still referenced from main scope) must NOT be
    reclaimed by gc.collect(). False-positive guard."""
    result = _compile_and_run(tmp_path, """
        import gc

        class N:
            pass

        def main() -> None:
            a = N()
            b = N()
            a.peer = b
            b.peer = a
            n = gc.collect()
            # a/b still rooted in main's locals — must NOT be collected.
            print(a.peer is b)
            print(b.peer is a)

        if __name__ == "__main__":
            main()
        """)
    assert result.stdout.strip().split("\n") == ["True", "True"]


def test_collect_preserves_root_reachable_subgraph(tmp_path):
    """Build a graph with a live root reaching some cycles and some
    non-cycle nodes. Collect should reclaim ONLY the unreferenced
    cycles."""
    result = _compile_and_run(tmp_path, """
        import gc

        class N:
            pass

        live_root = []

        def make_dead_cycle():
            a = N()
            b = N()
            a.peer = b
            b.peer = a
            return None

        def main() -> None:
            keep = N()
            live_root.append(keep)
            make_dead_cycle()
            n = gc.collect()
            # 'keep' must still be alive; 'live_root' contains it.
            print(live_root[0] is keep)
            print(n >= 2)         # the dead cycle reclaimed

        if __name__ == "__main__":
            main()
        """)
    out = result.stdout.strip().split("\n")
    assert out == ["True", "True"]


# ---------------------------------------------------------------------------
# Long-running steady-state: cycles per iteration must NOT accumulate
# ---------------------------------------------------------------------------


@pytest.mark.pcc_gate(
    unavailable=None
    if os.path.exists("/usr/bin/time")
    else "needs /usr/bin/time -l for RSS measurement"
)
def test_steady_state_cycle_workload_rss_plateaus(tmp_path):
    """100k iterations of "make 1 cycle, drop, optionally collect()".
    Without G1: RSS grows unbounded. With G1: RSS plateaus.
    Threshold 200 MB is generous; a real leak would exceed 1 GB."""
    result, rss_kib = _compile_and_run_capture_rss(tmp_path, """
        import gc

        class N:
            pass

        def make_one():
            a = N()
            b = N()
            a.peer = b
            b.peer = a
            return None

        def main() -> None:
            n: int = 100_000
            i: int = 0
            while i < n:
                make_one()
                if i % 1000 == 0:
                    gc.collect()
                i = i + 1
            print("ok")

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0
    assert result.stdout.strip() == "ok"
    if rss_kib > 0:
        assert rss_kib < 200_000, f"cycle workload RSS {rss_kib} KiB"


# ---------------------------------------------------------------------------
# Negative lock: pre-G1, cycles DO leak (regression catch)
# ---------------------------------------------------------------------------


def test_pre_g1_cycle_leaks_negative_lock(tmp_path):
    """Today (no G1) cycles leak; this is the negative-lock that
    flips when G1 lands. Don't remove this until G1 lands and the
    test starts XPASSing."""
    result = _compile_and_run(tmp_path, """
        class Marker:
            triggered = False
            def __del__(self):
                Marker.triggered = True

        def make_cycle():
            a = Marker()
            b = Marker()
            a.peer = b
            b.peer = a
            return None  # cycle leaks: refcount-only can't free it

        def main() -> None:
            make_cycle()
            print(Marker.triggered)   # False today

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    # Pre-G1: cycle leaks → __del__ never fires → Marker.triggered False.
    # When G1 lands AND G2 wires __del__, this becomes True — flip the
    # assertion at that point.
    assert "False" in result.stdout, (
        "cycle was unexpectedly collected — has G1 landed? "
        "Flip this assertion to True."
    )


# ---------------------------------------------------------------------------
# Weakref invalidation correctness (gated on G3)
# ---------------------------------------------------------------------------


def test_weakref_invalidates_when_target_dies(tmp_path):
    result = _compile_and_run(tmp_path, """
        import weakref

        class Box:
            pass

        def main() -> None:
            b = Box()
            r = weakref.ref(b)
            print(r() is b)
            del b
            print(r() is None)

        if __name__ == "__main__":
            main()
        """)
    assert result.stdout.strip().split("\n") == ["True", "True"]


def test_weakref_invalidates_when_cycle_collected(tmp_path):
    """Weakref to a cycle target must invalidate when the cycle is
    reclaimed by gc.collect(), not just on direct del."""
    result = _compile_and_run(tmp_path, """
        import gc, weakref

        class N:
            pass

        def main() -> None:
            a = N()
            b = N()
            a.peer = b
            b.peer = a
            ra = weakref.ref(a)
            rb = weakref.ref(b)
            a = None
            b = None             # cycle becomes unreachable
            gc.collect()
            print(ra() is None)
            print(rb() is None)

        if __name__ == "__main__":
            main()
        """)
    assert result.stdout.strip().split("\n") == ["True", "True"]
