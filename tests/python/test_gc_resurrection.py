"""Resurrection — `__del__` re-stashing self prevents reclaim.

Modeled directly on CPython's `Lib/test/test_gc.py`:
- `test_resurrection_only_happens_once_per_object`
- `test_resurrection_is_transitive`
- `test_resurrection_does_not_block_cleanup_of_other_objects`

These pin the exact resurrection contract pcc must preserve once
G2 (`__del__` dispatch) lands. CPython treats resurrection as legal
but **one-shot per object** — once an object's finalizer ran, the
object can never resurrect again.

All resurrection sub-protocols in this file are live regression tests under
the default runtime.
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
# Resurrection happens once per object
# ---------------------------------------------------------------------------


def test_resurrection_only_happens_once_per_object(tmp_path):
    """Adapted from CPython test_resurrection_only_happens_once.
    Once `__del__` ran and resurrected `self`, dropping the new ref
    must NOT call `__del__` again."""
    result = _compile_and_run(tmp_path, """
        import gc

        class Lazarus:
            resurrected = 0
            stash = []
            me = None
            def __init__(self):
                self.me = self        # self-loop forces cycle path
            def __del__(self):
                Lazarus.resurrected = Lazarus.resurrected + 1
                Lazarus.stash.append(self)

        def main() -> None:
            laz = Lazarus()
            laz = None
            gc.collect()
            print(Lazarus.resurrected)        # 1 — __del__ ran once
            Lazarus.stash.clear()             # final drop
            print(Lazarus.resurrected)        # still 1, NOT 2
            gc.collect()
            print(Lazarus.resurrected)        # still 1

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["1", "1", "1"]


# ---------------------------------------------------------------------------
# Resurrection is transitive
# ---------------------------------------------------------------------------


def test_resurrection_is_transitive(tmp_path):
    """When `__del__` resurrects self, anything reachable from self
    is ALSO resurrected. CPython guarantees this — finalizer can rely
    on `self.cargo` still being alive."""
    result = _compile_and_run(tmp_path, """
        import gc

        class Cargo:
            def __init__(self):
                self.me = self        # self-loop: needs cycle collection

        class Lazarus:
            resurrected_with_cargo = []
            def __del__(self):
                Lazarus.resurrected_with_cargo.append(self)

        def main() -> None:
            laz = Lazarus()
            cargo = Cargo()
            laz.cargo = cargo
            cargo.laz = laz             # cycle: laz<->cargo
            laz = None
            cargo = None
            gc.collect()
            print(len(Lazarus.resurrected_with_cargo))   # 1
            instance = Lazarus.resurrected_with_cargo[0]
            print(hasattr(instance, "cargo"))            # True
            print(hasattr(instance.cargo, "laz"))        # True

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["1", "True", "True"]


# ---------------------------------------------------------------------------
# Resurrection does not block other objects' cleanup
# ---------------------------------------------------------------------------


def test_collect_after_resurrection_still_reclaims_unrelated_garbage(tmp_path):
    """A collect whose PASS-0 ran a resurrecting finalizer must not disable the
    NEXT collect.  Backend 1 fabricated an active mark cycle from its write
    barrier, so the following gc.collect() began no cycle, computed no
    candidates and reclaimed nothing at all - not even unrelated garbage."""
    result = _compile_and_run(tmp_path, """
        import gc

        zs = []

        class A:
            def __init__(self):
                self.me = self

        class Z(A):
            def __del__(self):
                zs.append(self)

        def make(n: int) -> None:
            i = 0
            while i < n:
                A()
                i = i + 1

        def main() -> None:
            Z()
            print(gc.collect())        # 0 - Z resurrected itself
            zs.clear()
            make(30)
            print(gc.collect())        # >= 30 - the fresh cycles at least

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    out = result.stdout.strip().split("\n")
    assert out[0] == "0"
    assert int(out[1]) >= 30


def test_resurrection_does_not_block_other_cleanup(tmp_path):
    """A resurrecting object in a batch must NOT prevent peer
    (non-resurrecting) cycles from being reclaimed."""
    result = _compile_and_run(tmp_path, """
        import gc

        # Z resurrects; A doesn't
        zs = []

        class A:
            def __init__(self):
                self.me = self

        class Z(A):
            def __del__(self):
                zs.append(self)

        def main() -> None:
            n = 100
            i = 0
            while i < n:
                A()
                i = i + 1
            t = gc.collect()
            print(t)                  # n — A's all collected
            Z()
            t = gc.collect()
            print(t)                  # 0 — Z resurrected itself
            zs.clear()
            t = gc.collect()
            print(t)                  # >= 1 — Z finally collected

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    out = result.stdout.strip().split("\n")
    assert out[0] == "100"
    assert out[1] == "0"
    assert int(out[2]) >= 1


# ---------------------------------------------------------------------------
# Resurrected attributes survive
# ---------------------------------------------------------------------------


def test_resurrected_object_attrs_intact(tmp_path):
    """When resurrected, all of self's attributes must still be valid
    references — they can't have been already deallocated."""
    result = _compile_and_run(tmp_path, """
        import gc

        STASH = []

        class R:
            def __init__(self, payload):
                self.payload = payload
            def __del__(self):
                STASH.append(self)

        def main() -> None:
            r = R("hello")
            r = None
            gc.collect()
            print(len(STASH))           # 1
            print(STASH[0].payload)     # "hello" — payload survived

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["1", "hello"]


# ---------------------------------------------------------------------------
# Resurrection through external container
# ---------------------------------------------------------------------------


def test_resurrection_via_external_dict(tmp_path):
    """The classic "cache" pattern: __del__ puts self into a global
    dict. Object survives until dict cleared."""
    result = _compile_and_run(tmp_path, """
        import gc

        cache = {}

        class Item:
            ids = 0
            def __init__(self):
                Item.ids = Item.ids + 1
                self.id = Item.ids
            def __del__(self):
                cache[self.id] = self     # resurrect into cache

        def main() -> None:
            x = Item()
            x_id = x.id
            x = None
            gc.collect()
            print(x_id in cache)         # True — resurrected
            print(cache[x_id].id == x_id) # True — same object

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["True", "True"]


# ---------------------------------------------------------------------------
# Subclass __del__ resurrection
# ---------------------------------------------------------------------------


def test_subclass_del_can_resurrect(tmp_path):
    result = _compile_and_run(tmp_path, """
        import gc

        STASH = []

        class Base:
            pass

        class Child(Base):
            def __del__(self):
                STASH.append(self)

        def main() -> None:
            c = Child()
            c = None
            gc.collect()
            print(len(STASH) == 1)
            print(isinstance(STASH[0], Child))

        if __name__ == "__main__":
            main()
        """)
    assert result.stdout.strip().split("\n") == ["True", "True"]
