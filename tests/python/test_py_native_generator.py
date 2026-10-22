from __future__ import annotations

import subprocess
import textwrap

from pcc.frontends.python.pipeline import compile_python


def _compile_and_run(tmp_path, name: str, source: str) -> subprocess.CompletedProcess[str]:
    src = tmp_path / f"{name}.py"
    src.write_text(textwrap.dedent(source), encoding="utf-8")
    exe = tmp_path / f"{name}.out"
    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    return subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
    )


def test_iter_next_builtin_stays_native(tmp_path):
    run = _compile_and_run(
        tmp_path,
        "iter_next",
        """
        xs = [10, 20, 30]
        it = iter(xs)
        print(next(it))
        print(next(it))
        print(next(it))
        """,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "10\n20\n30\n"


def test_generator_next_preserves_resume_state(tmp_path):
    run = _compile_and_run(
        tmp_path,
        "generator_next",
        """
        def counter():
            i = 0
            while i < 3:
                yield i
                i = i + 1

        g = counter()
        print(next(g))
        print(next(g))
        print(next(g))
        """,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "0\n1\n2\n"


def test_generator_stop_iteration_is_catchable(tmp_path):
    run = _compile_and_run(
        tmp_path,
        "generator_stop",
        """
        def gen():
            yield 1

        g = gen()
        print(next(g))
        try:
            next(g)
            print("no_stop")
        except StopIteration:
            print("StopIteration")
        """,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "1\nStopIteration\n"


def test_for_loop_consumes_native_generator(tmp_path):
    run = _compile_and_run(
        tmp_path,
        "generator_for",
        """
        def gen():
            yield 1
            yield 2
            yield 3

        for v in gen():
            print(v)
        """,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "1\n2\n3\n"


def test_yield_from_delegates_natively(tmp_path):
    run = _compile_and_run(
        tmp_path,
        "generator_yield_from",
        """
        def inner():
            yield 1
            yield 2

        def outer():
            yield 0
            yield from inner()
            yield 3

        for v in outer():
            print(v)
        """,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "0\n1\n2\n3\n"


def test_for_loop_inside_generator_preserves_iterator(tmp_path):
    run = _compile_and_run(
        tmp_path,
        "generator_inner_for",
        """
        def gen():
            for v in [1, 2, 3]:
                yield v

        for v in gen():
            print(v)
        """,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == "1\n2\n3\n"
