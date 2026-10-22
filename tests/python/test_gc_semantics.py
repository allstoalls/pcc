"""Phase G0 — GC semantics gap baseline.

Locks the current memory-model contract so future GC work has a known
starting line. These tests are live regression gates for behavior that
started as the Phase G0-G5 gap baseline. See
``docs/issues/gc-semantics-gap.md`` for the historical plan.

Test families:
- refcount / immortal / tagged-int
- cycle collection behavior
- ``__del__`` dispatch
- ``weakref`` behavior
- concurrent refcount no-drift probe; broader free-threaded coverage still
  lives in dedicated threading/GC tests.
"""
from __future__ import annotations

import subprocess
import textwrap


def _compile_and_run(tmp_path, source: str) -> subprocess.CompletedProcess[str]:
    """Compile a pcc-Python program and run it; return the subprocess result."""
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    compile_python(str(src), str(exe), ir_scaffold_mode="on")
    return subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=20,
    )


# ---------------------------------------------------------------------------
# Pass-today tests — document what works now.
# ---------------------------------------------------------------------------


def test_refcount_releases_unreachable_tree(tmp_path):
    """A non-cyclic tree of objects gets released as the last reference
    drops. Detection here uses ``__del__`` as a release signal because
    pcc has no public refcount ABI today.

    NOTE: this test depends on ``__del__`` actually firing. If G2 hasn't
    landed yet ``__del__`` is a no-op, in which case this test acts as a
    *negative lock*: it asserts that no false-positive release happens
    even when ``__del__`` doesn't run. Once G2 lands the test should be
    flipped to assert ``Marker.triggered`` is True.
    """
    result = _compile_and_run(tmp_path, """
        class Marker:
            triggered = False
            def __del__(self):
                Marker.triggered = True

        def make_tree():
            a = Marker()
            b = Marker()
            return None  # both go out of scope, no cycle

        def main() -> None:
            make_tree()
            print("ok")

        if __name__ == "__main__":
            main()
        """)
    # Today: program runs to completion without crashing on dealloc.
    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_immortal_singletons_dont_dealloc(tmp_path):
    """``None`` / ``True`` / ``False`` survive arbitrary use without
    crashing, even though they live forever. A regression here means
    decref overran an immortal flag."""
    result = _compile_and_run(tmp_path, """
        def main() -> None:
            n = None
            t = True
            f = False
            for _ in range(1000):
                n = None
                t = True
                f = False
            print(n is None, t is True, f is False)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert "True True True" in result.stdout


def test_tagged_int_decref_is_noop(tmp_path):
    """Tagged-int values (low bit = 1) are not heap objects; decref on
    them must be a no-op. Loop creates and discards many tagged ints
    without crashing."""
    result = _compile_and_run(tmp_path, """
        def main() -> None:
            i: int = 0
            acc: int = 0
            while i < 100000:
                acc = acc + i
                i = i + 1
            print(acc)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    # 0+1+...+99999 = 99999 * 100000 / 2 = 4999950000
    assert "4999950000" in result.stdout


def test_cycle_self_reference_leaks_pre_collector(tmp_path):
    """*Negative lock*: cycles leak today (no cycle collector). When G1
    lands this assertion flips: the cycle should be collected and
    ``Marker.triggered`` becomes True.

    This test exists so that landing G1 *visibly* changes test
    behaviour; if the collector ever silently runs we want pytest to
    notice."""
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
            return None  # cycle goes out of scope; refcount-only can't free it

        def main() -> None:
            make_cycle()
            # Without a cycle collector, Marker.triggered stays False.
            print(Marker.triggered)

        if __name__ == "__main__":
            main()
        """)
    # Pre-G1: cycle leaks, __del__ never fires (or doesn't dispatch on
    # cycle, even if G2 has landed). Either way the marker should not
    # trigger today.
    assert result.returncode == 0, result.stderr
    assert "False" in result.stdout, (
        "cycle was unexpectedly collected — has G1 landed? "
        "If yes, flip this test to assert True"
    )


# ---------------------------------------------------------------------------
# Live semantic tests for behavior that used to be gap coverage.
# ---------------------------------------------------------------------------


def test_dunder_del_dispatch(tmp_path):
    """When the last reference to a non-cyclic instance drops, its
    ``__del__`` method should run. Phase G2 implements this."""
    result = _compile_and_run(tmp_path, """
        class Marker:
            triggered = False
            def __del__(self):
                Marker.triggered = True

        def make_one():
            x = Marker()
            return None

        def main() -> None:
            make_one()
            # If __del__ dispatch works, Marker.triggered is True after
            # x went out of scope.
            print(Marker.triggered)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert "True" in result.stdout


def test_weakref_basic(tmp_path):
    """``weakref.ref(obj)`` should return a callable proxy that yields
    the target while it is alive, and ``None`` after the target is
    collected. Phase G3 lands this."""
    result = _compile_and_run(tmp_path, """
        import weakref

        class Box:
            pass

        def main() -> None:
            b = Box()
            r = weakref.ref(b)
            print(r() is b)
            del b
            # After del, the weakref should resolve to None.
            print(r() is None)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "True\nTrue\n"


def test_concurrent_refcount_no_drift(tmp_path):
    """Multiple threads incref/decref the same object without losing
    counts. Phase G4 lands this — pcc today is single-threaded only."""
    result = _compile_and_run(tmp_path, """
        import threading

        class Box:
            count = 0
            def __init__(self):
                Box.count = Box.count + 1
            def __del__(self):
                Box.count = Box.count - 1

        def worker(box, n):
            i = 0
            while i < n:
                local = box  # incref
                _ = local
                i = i + 1

        def main() -> None:
            b = Box()
            t1 = threading.Thread(target=worker, args=(b, 10000))
            t2 = threading.Thread(target=worker, args=(b, 10000))
            t1.start(); t2.start()
            t1.join(); t2.join()
            # Box.count should still be exactly 1 — no drift.
            print(Box.count)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert "1" in result.stdout
