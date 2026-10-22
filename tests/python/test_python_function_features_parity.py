"""CPython parity for function features, ported from
``Lib/test/test_funcattrs.py`` and ``Lib/test/test_keywordonlyarg.py``.

All tests use ``libpython_mode="off"``.

Reference contract (from CPython):
  * default arguments are evaluated once at def time
  * ``*args`` collects positional surplus into a tuple
  * ``**kwargs`` collects keyword surplus into a dict
  * keyword-only args after ``*`` (or after ``*args``)
  * ``lambda`` is an inline anonymous ``def``
  * closures capture enclosing locals by reference
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path


def _compile(monkeypatch, src: Path, exe: Path) -> None:
    from pcc.frontends.python.pipeline import compile_python

    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )


def _run(exe: Path, timeout: float = 30.0) -> str:
    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert result.returncode == 0, (
        f"{exe.name} exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result.stdout


def test_default_args_basic(tmp_path, monkeypatch):
    src = tmp_path / "fn_default.py"
    exe = tmp_path / "fn_default.out"
    src.write_text(
        textwrap.dedent("""
        def greet(name: str = "world") -> str:
            return "hello " + name

        def main() -> None:
            print(greet())
            print(greet("pcc"))

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["hello world", "hello pcc"]


def test_keyword_args(tmp_path, monkeypatch):
    src = tmp_path / "fn_kwargs.py"
    exe = tmp_path / "fn_kwargs.out"
    src.write_text(
        textwrap.dedent("""
        def f(a: int, b: int, c: int = 0) -> int:
            return a * 100 + b * 10 + c

        def main() -> None:
            print(f(1, 2))
            print(f(1, 2, 3))
            print(f(a=1, b=2, c=3))
            print(f(b=2, a=1))

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["120", "123", "123", "120"]


def test_lambda_basic(tmp_path, monkeypatch):
    src = tmp_path / "fn_lambda.py"
    exe = tmp_path / "fn_lambda.out"
    src.write_text(
        textwrap.dedent("""
        def main() -> None:
            sq = lambda x: x * x
            print(sq(3))
            print(sq(7))
            add = lambda a, b: a + b
            print(add(10, 20))

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["9", "49", "30"]


def test_closure_capture(tmp_path, monkeypatch):
    src = tmp_path / "fn_closure.py"
    exe = tmp_path / "fn_closure.out"
    src.write_text(
        textwrap.dedent("""
        def make_adder(n: int):
            def add(x: int) -> int:
                return x + n
            return add

        def main() -> None:
            add5 = make_adder(5)
            add10 = make_adder(10)
            print(add5(3))
            print(add10(3))
            print(add5(0))

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["8", "13", "5"]


def test_nested_function_default_captures_enclosing_parameter(tmp_path, monkeypatch):
    src = tmp_path / "fn_nested_default.py"
    exe = tmp_path / "fn_nested_default.out"
    src.write_text(
        textwrap.dedent("""
            def outer(value):
                def inner(delta, saved=value):
                    return saved + delta
                return inner(2)

            print(outer(40))
            """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip() == "42"


def test_nested_function_defaults_are_fresh_per_def_execution(tmp_path, monkeypatch):
    src = tmp_path / "fn_nested_default_fresh.py"
    exe = tmp_path / "fn_nested_default_fresh.out"
    src.write_text(
        textwrap.dedent("""
            def make_reader(value):
                def read(saved=value):
                    return saved
                return read

            first = make_reader("first")
            second = make_reader("second")
            print(first())
            print(second())
            """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["first", "second"]


def test_recursion_basic(tmp_path, monkeypatch):
    src = tmp_path / "fn_rec.py"
    exe = tmp_path / "fn_rec.out"
    src.write_text(
        textwrap.dedent("""
        def fact(n: int) -> int:
            if n <= 1:
                return 1
            return n * fact(n - 1)

        def main() -> None:
            print(fact(0))
            print(fact(1))
            print(fact(5))
            print(fact(10))

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["1", "1", "120", "3628800"]


def test_mutual_recursion(tmp_path, monkeypatch):
    src = tmp_path / "fn_mutrec.py"
    exe = tmp_path / "fn_mutrec.out"
    src.write_text(
        textwrap.dedent("""
        def is_even(n: int) -> bool:
            if n == 0:
                return True
            return is_odd(n - 1)

        def is_odd(n: int) -> bool:
            if n == 0:
                return False
            return is_even(n - 1)

        def main() -> None:
            print(is_even(0))
            print(is_even(7))
            print(is_odd(5))

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["True", "False", "True"]


def test_function_returns_function(tmp_path, monkeypatch):
    src = tmp_path / "fn_higher.py"
    exe = tmp_path / "fn_higher.out"
    src.write_text(
        textwrap.dedent("""
        def compose(f, g):
            def composed(x: int) -> int:
                return f(g(x))
            return composed

        def main() -> None:
            inc = lambda x: x + 1
            dbl = lambda x: x * 2
            inc_then_dbl = compose(dbl, inc)
            dbl_then_inc = compose(inc, dbl)
            print(inc_then_dbl(3))
            print(dbl_then_inc(3))

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["8", "7"]


def test_args_splat_at_callsite(tmp_path, monkeypatch):
    src = tmp_path / "fn_splat.py"
    exe = tmp_path / "fn_splat.out"
    src.write_text(
        textwrap.dedent("""
        def f(a: int, b: int, c: int) -> int:
            return a + b + c

        def main() -> None:
            args = (1, 2, 3)
            print(f(*args))

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip() == "6"


def test_decorator_runtime_effect(tmp_path, monkeypatch):
    src = tmp_path / "fn_decorator.py"
    exe = tmp_path / "fn_decorator.out"
    src.write_text(
        textwrap.dedent("""
        def trace(fn):
            def wrapper(x: int) -> int:
                print("before")
                r = fn(x)
                print("after")
                return r
            return wrapper

        @trace
        def double(x: int) -> int:
            return x * 2

        def main() -> None:
            print(double(5))

        if __name__ == "__main__":
            main()
        """).lstrip(),
        encoding="utf-8",
    )
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["before", "after", "10"]
