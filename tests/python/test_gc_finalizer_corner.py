"""`__del__` finalizer corner cases.

Modeled after CPython's `Lib/test/test_gc.py`:
- `test_boom` / `test_boom2` — `__del__` raises
- `test_legacy_finalizer` — finalizer that prevents collection
- `test_global_del_SystemExit` — `SystemExit` from `__del__`
- `test_garbage_at_shutdown` — uncollectable cycles at interpreter exit
- `test_del` — basic `__del__` on cycle

Each test pins one rough edge of the `__del__` contract that pcc
must preserve once G2 lands. Some are already XPASS'ing today
(e.g. exception swallowing) — those flip when their behavior is
intentional.
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
# __del__ raises generic exception — must not crash
# ---------------------------------------------------------------------------


def test_del_raises_generic_exception(tmp_path):
    """Adapted from CPython test_boom. `__del__` raising any
    exception (other than SystemExit/KeyboardInterrupt) must be
    caught silently with at most a warning."""
    result = _compile_and_run(tmp_path, """
        class Boom:
            def __del__(self):
                raise AttributeError("oops")

        def main() -> None:
            _ = Boom()
            print("survived alloc")
            # _ goes out of scope here; __del__ runs (refcount path)
            # Program must not abort.
            print("survived dealloc")

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert "survived alloc" in result.stdout
    assert "survived dealloc" in result.stdout


def test_del_raises_attribute_error_in_loop(tmp_path):
    """1000 instances each with a raising __del__. Must not crash.
    Stress version of the above."""
    result = _compile_and_run(tmp_path, """
        class Bad:
            def __del__(self):
                raise RuntimeError("oops")

        def main() -> None:
            n: int = 1000
            i: int = 0
            while i < n:
                _ = Bad()
                i = i + 1
            print("done")

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "done"


# ---------------------------------------------------------------------------
# __del__ raises SystemExit — must propagate (CPython contract)
# ---------------------------------------------------------------------------


def test_del_raises_system_exit_propagates(tmp_path):
    """`SystemExit` raised in `__del__` is unraisable, as in CPython.

    `_` stays bound until main returns, so the print runs first; the
    finalizer then raises inside a deallocation, where CPython reports
    "Exception ignored" on stderr and exits 0 (3.13 and 3.15 alike). The
    earlier expectation of exit 7 was not CPython's behavior. pcc does not
    print the unraisable report yet, so stderr is not compared."""
    result = _compile_and_run(tmp_path, """
        import sys

        class Exit:
            def __del__(self):
                sys.exit(7)

        def main() -> None:
            _ = Exit()
            print("reached")

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout == "reached\n"


# ---------------------------------------------------------------------------
# __del__ on cycle — CPython 3.4+ collects cycle WITH __del__
# ---------------------------------------------------------------------------


def test_del_on_cycle_runs_after_collect(tmp_path):
    """CPython 3.4+ (PEP 442): a cycle of objects with `__del__` IS
    collectable. `__del__` runs in unspecified order but runs."""
    result = _compile_and_run(tmp_path, """
        import gc

        runs = []

        class A:
            def __del__(self):
                runs.append("A")

        class B:
            def __del__(self):
                runs.append("B")

        def make_cycle():
            a = A()
            b = B()
            a.peer = b
            b.peer = a
            return None

        def main() -> None:
            make_cycle()
            gc.collect()
            print(sorted(runs))    # ['A', 'B']

        if __name__ == "__main__":
            main()
        """)
    assert result.stdout.strip() == "['A', 'B']"


# ---------------------------------------------------------------------------
# Exception state during __del__
# ---------------------------------------------------------------------------


def test_del_does_not_pollute_exception_state(tmp_path):
    """If `__del__` raises and gets swallowed, the *outer*
    exception-handling state must NOT be polluted."""
    result = _compile_and_run(tmp_path, """
        class Bad:
            def __del__(self):
                raise ValueError("inner")

        def main() -> None:
            try:
                _ = Bad()
                raise RuntimeError("outer")
            except RuntimeError as e:
                print(str(e))     # 'outer' — not 'inner'

        if __name__ == "__main__":
            main()
        """)
    assert result.stdout.strip() == "outer"


# ---------------------------------------------------------------------------
# __del__ creates new objects — finalizer cascade
# ---------------------------------------------------------------------------


def test_del_can_create_new_objects(tmp_path):
    """Some `__del__` implementations construct new transient objects
    (e.g. for logging). Those new objects' lifecycle must not collide
    with the in-progress finalize."""
    result = _compile_and_run(tmp_path, """
        log = []

        class Logger:
            def __del__(self):
                tmp = "freed " + str(id(self))[:0] + "X"
                log.append(tmp)

        def main() -> None:
            for _ in [1, 2, 3]:
                _ = Logger()
            # Loop exit — three Loggers freed; each appends to log.
            print(len(log) == 3)

        if __name__ == "__main__":
            main()
        """)
    assert result.stdout.strip() == "True"


# ---------------------------------------------------------------------------
# Module global __del__ at shutdown
# ---------------------------------------------------------------------------


def test_module_global_del_at_shutdown(tmp_path):
    """Adapted from CPython test_garbage_at_shutdown. Module-global
    objects with `__del__` should still get finalized at interpreter
    teardown. Output goes to stderr because stdout might be torn
    down first."""
    result = _compile_and_run(tmp_path, """
        import sys

        class Holder:
            def __del__(self):
                sys.stderr.write("module_del_ran\\n")

        global_holder = Holder()

        def main() -> None:
            print("main_done")

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert "main_done" in result.stdout
    assert "module_del_ran" in result.stderr


# ---------------------------------------------------------------------------
# __del__ that takes a long time
# ---------------------------------------------------------------------------


def test_long_running_del(tmp_path):
    """A `__del__` that does meaningful work (e.g. flush a buffer)
    must not deadlock or be aborted."""
    result = _compile_and_run(tmp_path, """
        log = []

        class Slow:
            def __del__(self):
                # Simulate buffer flush — ~10k iterations
                acc: int = 0
                i: int = 0
                while i < 10000:
                    acc = acc + i
                    i = i + 1
                log.append(acc)

        def main() -> None:
            _ = Slow()
            # _ drops; __del__ runs the loop
            print(len(log))    # 1

        if __name__ == "__main__":
            main()
        """)
    assert result.stdout.strip() == "1"


# ---------------------------------------------------------------------------
# __del__ with attribute access on partially-initialized object
# ---------------------------------------------------------------------------


def test_del_on_partial_init(tmp_path):
    """If `__init__` raises before assigning expected attrs, `__del__`
    must handle missing attrs gracefully (typically guard with
    hasattr or try/except)."""
    result = _compile_and_run(tmp_path, """
        class Half:
            def __init__(self, fail):
                if fail:
                    raise ValueError("bail")
                self.attr = 42
            def __del__(self):
                if hasattr(self, "attr"):
                    print("attr=" + str(self.attr))
                else:
                    print("no_attr")

        def main() -> None:
            try:
                _ = Half(True)
            except ValueError:
                pass
            _ = Half(False)
            print("done")

        if __name__ == "__main__":
            main()
        """)
    assert "no_attr" in result.stdout
    assert "attr=42" in result.stdout
    assert "done" in result.stdout


# ---------------------------------------------------------------------------
# `gc.garbage` populated for uncollectable cycles
# ---------------------------------------------------------------------------


def test_gc_garbage_populated_for_uncollectable(tmp_path):
    """Pre-PEP-442 finalizer-on-cycle was uncollectable and went into
    `gc.garbage`. CPython 3.4+ collects most cycles with __del__,
    but pathological cases (e.g. pre-3.4 `__tp_del__`) still go
    there. pcc may not implement legacy `__tp_del__`; this test
    locks the modern contract — `gc.garbage` empty after a normal
    cycle collection."""
    result = _compile_and_run(tmp_path, """
        import gc

        class N:
            def __del__(self):
                pass

        def make():
            a = N(); b = N()
            a.peer = b; b.peer = a
            return None

        def main() -> None:
            make()
            gc.collect()
            print(len(gc.garbage))    # 0 — fully collected

        if __name__ == "__main__":
            main()
        """)
    assert result.stdout.strip() == "0"
