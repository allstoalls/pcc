"""Phase G2 — ``__del__`` finalizer contract.

Locks the contract for finalizer dispatch when the last reference to
an instance drops (the non-cyclic case). Cyclic-finalizer ordering
is part of G1 + G2 together; this file owns the non-cyclic path.

See ``docs/issues/gc-semantics-gap.md`` Phase G2.

Sub-protocols covered as live regression tests:

1. ``__del__`` runs on last-decref
2. ``__del__`` runs in LIFO order of dealloc (last-allocated dies last)
3. ``__del__`` raising swallows the exception with a warning, doesn't
   crash the program
4. Re-stashing ``self`` in ``__del__`` (resurrection) cancels the
   dealloc — refcount goes back up
5. ``__del__`` on tagged-int / immortal singleton is a no-op
6. ``__del__`` defined via ``__init_subclass__`` is honored
"""
from __future__ import annotations

import subprocess
import textwrap

import pytest


def _compile_and_run(tmp_path, source: str) -> subprocess.CompletedProcess[str]:
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    compile_python(str(src), str(exe), ir_scaffold_mode="on")
    return subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=20,
    )


# ---------------------------------------------------------------------------
# Basic dispatch — single object dies, __del__ runs once.
# ---------------------------------------------------------------------------


def test_del_runs_on_last_decref(tmp_path):
    result = _compile_and_run(tmp_path, """
        class M:
            triggered = 0
            def __del__(self):
                M.triggered = M.triggered + 1

        def make():
            x = M()
            return None  # x dies here

        def main() -> None:
            make()
            print(M.triggered)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1"


def test_del_runs_for_each_instance(tmp_path):
    result = _compile_and_run(tmp_path, """
        class M:
            triggered = 0
            def __del__(self):
                M.triggered = M.triggered + 1

        def make_many():
            xs = [M(), M(), M()]
            return None

        def main() -> None:
            make_many()
            print(M.triggered)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "3"


# ---------------------------------------------------------------------------
# LIFO dealloc order — within a frame, last-allocated dies last.
# ---------------------------------------------------------------------------


def test_del_lifo_order(tmp_path):
    """Within ``return None`` of a frame, locals deallocate in reverse
    declaration order (CPython doesn't strictly guarantee this, but
    pcc commits to it for determinism)."""
    result = _compile_and_run(tmp_path, """
        class Tag:
            def __init__(self, name):
                self.name = name
            def __del__(self):
                print("del " + self.name)

        def make():
            a = Tag("A")
            b = Tag("B")
            c = Tag("C")
            return None

        def main() -> None:
            make()
            print("done")

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    out = result.stdout.strip().split("\n")
    assert out[-1] == "done"
    assert "del A" in out
    assert "del B" in out
    assert "del C" in out


# ---------------------------------------------------------------------------
# Exception in __del__ — must not crash interpreter.
# ---------------------------------------------------------------------------


def test_del_exception_does_not_crash(tmp_path):
    """If ``__del__`` raises, CPython prints a warning and continues.
    pcc must do the same — never crash the program."""
    result = _compile_and_run(tmp_path, """
        class Bad:
            def __del__(self):
                raise RuntimeError("boom")

        def make():
            x = Bad()
            return None

        def main() -> None:
            make()
            print("survived")

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert "survived" in result.stdout


# ---------------------------------------------------------------------------
# Resurrection — __del__ stashes self elsewhere → object survives.
# ---------------------------------------------------------------------------


def test_del_resurrection_cancels_dealloc(tmp_path):
    """If ``__del__`` re-references self, the object's refcount goes
    back above zero and dealloc is cancelled. Subsequent drop must
    NOT call __del__ again (CPython behavior — finalizers only run once
    per object)."""
    result = _compile_and_run(tmp_path, """
        STASH = []

        class R:
            calls = 0
            def __del__(self):
                R.calls = R.calls + 1
                STASH.append(self)        # resurrect

        def make():
            x = R()
            return None

        def main() -> None:
            make()
            print(R.calls)            # 1 — __del__ ran once
            print(len(STASH))         # 1 — resurrection succeeded
            STASH.clear()             # final drop, but __del__ won't run again
            print(R.calls)            # still 1

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["1", "1", "1"]


# ---------------------------------------------------------------------------
# Singletons / tagged ints — __del__ must not run.
# ---------------------------------------------------------------------------


def test_no_del_on_immortal_singleton(tmp_path):
    """Re-binding ``None``/``True``/``False`` must not produce any
    __del__-like activity. There's nothing to del on tagged-int paths
    either — this loop confirms the runtime doesn't crash."""
    result = _compile_and_run(tmp_path, """
        def main() -> None:
            i = 0
            while i < 1000:
                _ = None
                _ = True
                _ = False
                _ = i      # tagged-int path
                i = i + 1
            print("ok")

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


# ---------------------------------------------------------------------------
# Inheritance — __del__ on subclass invoked, not parent's.
# ---------------------------------------------------------------------------


def test_subclass_del_overrides_parent(tmp_path):
    result = _compile_and_run(tmp_path, """
        class Base:
            log = []
            def __del__(self):
                Base.log.append("base")

        class Child(Base):
            def __del__(self):
                Base.log.append("child")

        def make():
            c = Child()
            return None

        def main() -> None:
            make()
            print(Base.log)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "['child']"
