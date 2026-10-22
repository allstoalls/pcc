"""GC module API contract — `gc.collect / disable / enable / is_tracked
/ is_finalized / get_count / get_objects / get_referents / get_referrers
/ freeze / unfreeze`.

Modeled after CPython's `Lib/test/test_gc.py` (52 tests). The native pcc
surface is intentionally smaller, but every test in this file is now a live
regression gate for implemented Phase G5 behavior.
"""
from __future__ import annotations

import subprocess
import textwrap

def _compile_and_run(tmp_path, source: str) -> subprocess.CompletedProcess[str]:
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    compile_python(str(src), str(exe), ir_scaffold_mode="on")
    return subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=60,
    )


# ---------------------------------------------------------------------------
# gc.collect / gc.disable / gc.enable
# ---------------------------------------------------------------------------


def test_gc_collect_returns_int(tmp_path):
    """`gc.collect()` returns the number of unreachable objects found."""
    result = _compile_and_run(tmp_path, """
        import gc
        def main() -> None:
            n = gc.collect()
            print(type(n).__name__)   # 'int'
            print(n >= 0)
        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["int", "True"]


def test_gc_disable_isenabled(tmp_path):
    result = _compile_and_run(tmp_path, """
        import gc
        def main() -> None:
            gc.disable()
            print(gc.isenabled())     # False
            gc.enable()
            print(gc.isenabled())     # True
        if __name__ == "__main__":
            main()
        """)
    assert result.stdout.strip().split("\n") == ["False", "True"]


def test_gc_collect_works_when_disabled(tmp_path):
    """When auto-GC is disabled, manual `gc.collect()` still works."""
    result = _compile_and_run(tmp_path, """
        import gc
        class N:
            pass
        def make_cycle():
            a = N(); b = N()
            a.peer = b; b.peer = a
            return None
        def main() -> None:
            gc.disable()
            make_cycle()
            n = gc.collect()
            print(n >= 2)
            gc.enable()
        if __name__ == "__main__":
            main()
        """)
    assert result.stdout.strip() == "True"


# ---------------------------------------------------------------------------
# gc.get_count / gc.get_threshold / gc.set_threshold
# ---------------------------------------------------------------------------


def test_get_count_returns_3tuple(tmp_path):
    """CPython contract: `gc.get_count()` returns `(gen0, gen1, gen2)`.
    pcc may not have generations but must return some 3-tuple."""
    result = _compile_and_run(tmp_path, """
        import gc
        def main() -> None:
            c = gc.get_count()
            print(type(c).__name__)
            print(len(c))
            print(all(type(x).__name__ == 'int' for x in c))
        if __name__ == "__main__":
            main()
        """)
    assert result.stdout.strip().split("\n") == ["tuple", "3", "True"]


def test_get_set_threshold(tmp_path):
    result = _compile_and_run(tmp_path, """
        import gc
        def main() -> None:
            t = gc.get_threshold()
            print(len(t) == 3)
            gc.set_threshold(700, 10, 10)
            print(gc.get_threshold() == (700, 10, 10))
        if __name__ == "__main__":
            main()
        """)
    assert result.stdout.strip().split("\n") == ["True", "True"]


# ---------------------------------------------------------------------------
# gc.is_tracked
# ---------------------------------------------------------------------------


def test_is_tracked_atomic_types_false(tmp_path):
    """Atomic immutable types are NOT tracked. CPython contract."""
    result = _compile_and_run(tmp_path, """
        import gc
        def main() -> None:
            print(gc.is_tracked(None))
            print(gc.is_tracked(1))
            print(gc.is_tracked(1.0))
            print(gc.is_tracked(True))
            print(gc.is_tracked("a"))
            print(gc.is_tracked(b"a"))
        if __name__ == "__main__":
            main()
        """)
    out = result.stdout.strip().split("\n")
    assert out == ["False"] * 6


def test_is_tracked_containers_true(tmp_path):
    """Mutable containers + user instances ARE tracked."""
    result = _compile_and_run(tmp_path, """
        import gc
        class C: pass
        def main() -> None:
            print(gc.is_tracked([1, 2]))
            print(gc.is_tracked({"a": 1}))
            print(gc.is_tracked(C()))
            # tuple of immutables is special: untracked
            print(gc.is_tracked((1, 2)))
            print(gc.is_tracked(((),)))
            print(gc.is_tracked(((1, 2), (3, 4))))
            t = (1,)
            i: int = 0
            while i < 20:
                t = (t,)
                i = i + 1
            print(gc.is_tracked(t))
        if __name__ == "__main__":
            main()
        """)
    assert result.stdout.strip().split("\n") == [
        "True", "True", "True", "False", "False", "False", "False"
    ]


# ---------------------------------------------------------------------------
# gc.is_finalized
# ---------------------------------------------------------------------------


def test_is_finalized_atomic_false(tmp_path):
    result = _compile_and_run(tmp_path, """
        import gc
        def main() -> None:
            print(gc.is_finalized(3))   # not even tracked
            print(gc.is_finalized(None))
        if __name__ == "__main__":
            main()
        """)
    assert result.stdout.strip().split("\n") == ["False", "False"]


# ---------------------------------------------------------------------------
# gc.get_objects / gc.get_referents / gc.get_referrers
# ---------------------------------------------------------------------------


def test_get_objects_finds_self_referential_list(tmp_path):
    result = _compile_and_run(tmp_path, """
        import gc
        def main() -> None:
            gc.collect()
            L = []
            L.append(L)
            seen = False
            self_seen = False
            objs = gc.get_objects()
            for o in objs:
                if o is L:
                    seen = True
                if o is objs:
                    self_seen = True
            print(seen)
            print(self_seen)
        if __name__ == "__main__":
            main()
        """)
    assert result.stdout.strip().split("\n") == ["True", "False"]


def test_get_referents_returns_outgoing(tmp_path):
    """For container `c`, `gc.get_referents(c)` returns objects c
    points to."""
    result = _compile_and_run(tmp_path, """
        import gc
        def main() -> None:
            target = "hello"
            xs = [target, 1, 2]
            refs = gc.get_referents(xs)
            print(any(r is target for r in refs))
        if __name__ == "__main__":
            main()
        """)
    assert result.stdout.strip() == "True"


def test_get_referrers_finds_holder(tmp_path):
    """For object `obj`, `gc.get_referrers(obj)` returns containers
    that reference it."""
    result = _compile_and_run(tmp_path, """
        import gc
        def main() -> None:
            target = []
            holder = [target]
            gc.collect()    # stabilize
            refs = gc.get_referrers(target)
            print(any(r is holder for r in refs))
        if __name__ == "__main__":
            main()
        """)
    assert result.stdout.strip() == "True"


# ---------------------------------------------------------------------------
# gc.get_stats
# ---------------------------------------------------------------------------


def test_get_stats_has_required_keys(tmp_path):
    """CPython's `gc.get_stats()` returns a list of dicts; each must
    have 'collections', 'collected', 'uncollectable' keys at minimum."""
    result = _compile_and_run(tmp_path, """
        import gc
        def main() -> None:
            stats = gc.get_stats()
            print(type(stats).__name__)         # 'list'
            d = stats[0]
            print('collections' in d)
            print('collected' in d)
            print('uncollectable' in d)
        if __name__ == "__main__":
            main()
        """)
    assert result.stdout.strip().split("\n") == ["list", "True", "True", "True"]


# ---------------------------------------------------------------------------
# gc.freeze / gc.unfreeze (CPython 3.7+)
# ---------------------------------------------------------------------------


def test_freeze_unfreeze(tmp_path):
    result = _compile_and_run(tmp_path, """
        import gc
        def main() -> None:
            gc.freeze()
            print(gc.get_freeze_count() > 0)
            gc.unfreeze()
            print(gc.get_freeze_count() == 0)
        if __name__ == "__main__":
            main()
        """)
    assert result.stdout.strip().split("\n") == ["True", "True"]


# ---------------------------------------------------------------------------
# gc.garbage — uncollectable list
# ---------------------------------------------------------------------------


def test_garbage_starts_empty(tmp_path):
    result = _compile_and_run(tmp_path, """
        import gc
        def main() -> None:
            print(type(gc.garbage).__name__)    # 'list'
            print(len(gc.garbage) == 0)         # nothing uncollectable yet
        if __name__ == "__main__":
            main()
        """)
    assert result.stdout.strip().split("\n") == ["list", "True"]


# ---------------------------------------------------------------------------
# gc.collect(generation) — generational interface (may be no-op for pcc)
# ---------------------------------------------------------------------------


def test_collect_generation_arg(tmp_path):
    """`gc.collect(0)` collects only generation 0; pcc without
    generations should accept the arg as a no-op or treat as
    full-collect."""
    result = _compile_and_run(tmp_path, """
        import gc
        def main() -> None:
            n0 = gc.collect(0)
            n1 = gc.collect(1)
            n2 = gc.collect(2)
            print(n0 >= 0)
            print(n1 >= 0)
            print(n2 >= 0)
        if __name__ == "__main__":
            main()
        """)
    assert result.stdout.strip().split("\n") == ["True", "True", "True"]


# ---------------------------------------------------------------------------
# gc callbacks
# ---------------------------------------------------------------------------


def test_callbacks_fire_on_collect(tmp_path):
    """Callbacks registered in `gc.callbacks` fire with phase="start"
    and phase="stop" around each collection."""
    result = _compile_and_run(tmp_path, """
        import gc

        observed = []
        def cb(phase, info):
            observed.append(phase)

        def main() -> None:
            gc.callbacks.append(cb)
            gc.collect()
            gc.callbacks.remove(cb)
            gc.collect()
            print("start" in observed)
            print("stop" in observed)
            print(len(observed))
        if __name__ == "__main__":
            main()
        """)
    assert result.stdout.strip().split("\n") == ["True", "True", "2"]
