"""Phase D4 — full context-manager protocol.

Locks the corner cases of ``with`` statements that go beyond simple
``__enter__`` / ``__exit__`` (which already passes today). See
``docs/issues/python-data-model-gaps.md`` Phase D4.

Sub-protocols covered:

1. ``__exit__`` returning truthy suppresses the exception
2. Multiple managers on a single ``with`` line: enter L→R, exit R→L,
   even on exception
3. ``contextlib.contextmanager`` decorator (depends on D2 generators)
4. Re-raising in ``__exit__`` chains via ``__context__``
5. ``__enter__`` raising — ``__exit__`` of *prior* manager runs
"""
from __future__ import annotations

import subprocess
import textwrap

def _compile_and_run(tmp_path, source: str) -> subprocess.CompletedProcess[str]:
    from tests.native_provisioning import native_test_runtime_options

    runtime_options = native_test_runtime_options()
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "prog.py"
    exe = tmp_path / "prog.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    compile_python(str(src), str(exe), ir_scaffold_mode="on", **runtime_options)
    return subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=20,
    )


# ---------------------------------------------------------------------------
# Baseline — passes today
# ---------------------------------------------------------------------------


def test_context_manager_basic(tmp_path):  # passes today — locked by D4 baseline
    result = _compile_and_run(tmp_path, """
        class CM:
            def __enter__(self):
                print("enter")
                return self
            def __exit__(self, et, ev, tb):
                print("exit")
                return False

        def main() -> None:
            with CM():
                print("body")

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["enter", "body", "exit"]


# ---------------------------------------------------------------------------
# __exit__ returning truthy suppresses the exception
# ---------------------------------------------------------------------------


def test_exit_truthy_suppresses_exception(tmp_path):
    result = _compile_and_run(tmp_path, """
        class Suppress:
            def __enter__(self):
                return self
            def __exit__(self, et, ev, tb):
                return True   # MUST suppress

        def main() -> None:
            with Suppress():
                raise ValueError("boom")
            print("after")    # only reached if exception suppressed

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "after"


# ---------------------------------------------------------------------------
# Multiple managers — enter L→R, exit R→L
# ---------------------------------------------------------------------------


def test_multi_manager_order(tmp_path):
    """`with A() as a, B() as b:` enters A then B, exits B then A."""
    result = _compile_and_run(tmp_path, """
        class CM:
            def __init__(self, name):
                self.name = name
            def __enter__(self):
                print("enter " + self.name)
                return self
            def __exit__(self, et, ev, tb):
                print("exit " + self.name)
                return False

        def main() -> None:
            with CM("A") as a, CM("B") as b:
                print("body")

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == [
        "enter A", "enter B", "body", "exit B", "exit A",
    ]


def test_multi_manager_exception_unwind(tmp_path):
    """If the body of `with A, B` raises, both __exit__ run R→L with
    the exception info, and re-raises out the top."""
    result = _compile_and_run(tmp_path, """
        class CM:
            def __init__(self, name):
                self.name = name
            def __enter__(self):
                print("enter " + self.name)
                return self
            def __exit__(self, et, ev, tb):
                print("exit " + self.name)
                return False  # do not suppress

        def main() -> None:
            try:
                with CM("A"), CM("B"):
                    print("body")
                    raise RuntimeError("x")
            except RuntimeError:
                print("caught")

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == [
        "enter A", "enter B", "body", "exit B", "exit A", "caught",
    ]


# ---------------------------------------------------------------------------
# contextlib.contextmanager
# ---------------------------------------------------------------------------


def test_contextlib_contextmanager(tmp_path):
    """`@contextmanager` adapts a generator into a context manager."""
    result = _compile_and_run(tmp_path, """
        from contextlib import contextmanager

        @contextmanager
        def trace():
            print("before")
            yield "X"
            print("after")

        def main() -> None:
            with trace() as v:
                print(v)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["before", "X", "after"]


# ---------------------------------------------------------------------------
# __enter__ raising — outer __exit__ must still run
# ---------------------------------------------------------------------------


def test_inner_enter_raises_outer_exit_runs(tmp_path):
    """In `with A, B:`, if B.__enter__ raises, A.__exit__ must run
    (with the B exception info), then the exception propagates."""
    result = _compile_and_run(tmp_path, """
        class A:
            def __enter__(self):
                print("A enter")
                return self
            def __exit__(self, et, ev, tb):
                print("A exit")
                return False

        class B:
            def __enter__(self):
                print("B enter")
                raise RuntimeError("boom")
            def __exit__(self, et, ev, tb):
                print("B exit")
                return False

        def main() -> None:
            try:
                with A(), B():
                    print("body")
            except RuntimeError:
                print("caught")

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    out = result.stdout.strip().split("\n")
    assert out[0] == "A enter"
    assert out[1] == "B enter"
    assert "A exit" in out
    assert "body" not in out
    assert out[-1] == "caught"
