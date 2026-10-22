"""Phase D2 — generator protocol contract.

Locks the contract for ``yield`` / ``yield from`` / generator
``send`` / ``throw`` / ``close`` / ``return``-from-generator semantics
per ``docs/issues/python-data-model-gaps.md`` Phase D2.

Sub-protocols (in implementation order):

1. Plain ``yield`` (single-shot iterator with state preserved across
   yields)
2. ``yield from`` delegation
3. ``gen.send(value)`` — pushes a value into a paused generator
4. ``gen.throw(exc)`` — injects an exception at the current yield
5. ``gen.close()`` — raises GeneratorExit at the current yield
6. ``return value`` from a generator → StopIteration(value)
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
# Plain yield
# ---------------------------------------------------------------------------


def test_yield_basic_iteration(tmp_path):
    result = _compile_and_run(tmp_path, """
        def gen():
            yield 1
            yield 2
            yield 3

        def main() -> None:
            for v in gen():
                print(v)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["1", "2", "3"]


def test_yield_state_preserved_across_calls(tmp_path):
    """Generator must remember local state between yields (this is the
    core of the protocol — without saved frame state, it's not a
    generator)."""
    result = _compile_and_run(tmp_path, """
        def counter():
            i = 0
            while i < 5:
                yield i
                i = i + 1

        def main() -> None:
            g = counter()
            print(next(g))
            print(next(g))
            print(next(g))

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["0", "1", "2"]


def test_yield_stop_iteration_on_exhaust(tmp_path):
    result = _compile_and_run(tmp_path, """
        def gen():
            yield 1

        def main() -> None:
            g = gen()
            print(next(g))
            try:
                next(g)
                print("no_stop")
            except StopIteration:
                print("StopIteration")

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["1", "StopIteration"]


# ---------------------------------------------------------------------------
# yield from delegation
# ---------------------------------------------------------------------------


def test_yield_from_delegates(tmp_path):
    result = _compile_and_run(tmp_path, """
        def inner():
            yield 1
            yield 2

        def outer():
            yield 0
            yield from inner()
            yield 3

        def main() -> None:
            for v in outer():
                print(v)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["0", "1", "2", "3"]


# ---------------------------------------------------------------------------
# gen.send / gen.throw / gen.close
# ---------------------------------------------------------------------------


def test_generator_send(tmp_path):
    """``gen.send(v)`` makes the paused yield expression evaluate to v."""
    result = _compile_and_run(tmp_path, """
        def echo():
            x = yield 0
            yield x
            yield x + 1

        def main() -> None:
            g = echo()
            print(next(g))      # primes generator, yields 0
            print(g.send(10))   # 'x' becomes 10, yields 10
            print(g.send(99))   # value ignored at second yield, yields 11

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["0", "10", "11"]


def test_generator_throw(tmp_path):
    """``gen.throw(exc)`` injects an exception at the paused yield."""
    result = _compile_and_run(tmp_path, """
        def gen():
            try:
                yield 1
            except ValueError:
                yield 99

        def main() -> None:
            g = gen()
            print(next(g))
            print(g.throw(ValueError("boom")))

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["1", "99"]


def test_generator_close_raises_generator_exit(tmp_path):
    """``gen.close()`` raises ``GeneratorExit`` at the paused yield."""
    result = _compile_and_run(tmp_path, """
        def gen():
            try:
                yield 1
                yield 2
            except GeneratorExit:
                print("handled")
                # Must not yield again here; if we do, RuntimeError must fire.
                pass

        def main() -> None:
            g = gen()
            print(next(g))
            g.close()
            print("closed_ok")

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["1", "handled", "closed_ok"]


def test_dyn_close_dispatches_user_method_not_generator_intrinsic(tmp_path):
    result = _compile_and_run(tmp_path, """
        class C:
            def close(self):
                print("user_close")

        def make(flag):
            if flag:
                return C()
            return C()

        def main() -> None:
            obj = make(True)
            obj.close()

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "user_close"


# ---------------------------------------------------------------------------
# return from generator → StopIteration(value)
# ---------------------------------------------------------------------------


def test_return_value_becomes_stop_iteration_arg(tmp_path):
    """``return v`` from inside a generator raises ``StopIteration(v)``."""
    result = _compile_and_run(tmp_path, """
        def gen():
            yield 1
            return 42

        def main() -> None:
            g = gen()
            print(next(g))
            try:
                next(g)
                print("no_stop")
            except StopIteration as e:
                print(e.value)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["1", "42"]
