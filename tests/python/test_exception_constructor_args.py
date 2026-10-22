"""Exception classes keep BaseException.__new__'s args however they are built."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap


def _run_on_five_collectors(tmp_path, stem, source, expected):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / (stem + ".py")
    exe = tmp_path / (stem + ".out")
    src.write_text(source, encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    for backend in range(5):
        run_env = {**os.environ, "PCC_GC_BACKEND": str(backend)}
        result = subprocess.run(
            [str(exe)],
            capture_output=True,
            text=True,
            timeout=20,
            env=run_env,
        )
        assert result.returncode == 0, (
            f"PCC_GC_BACKEND={backend} rc={result.returncode}\n"
            + result.stdout + result.stderr
        )
        assert result.stdout == expected, f"PCC_GC_BACKEND={backend}"


def _cpython_output(source):
    return subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    ).stdout


def test_exception_construction_keeps_new_args(tmp_path, monkeypatch, pcc_runtime_archive):
    # BaseException.__new__ stores the positional arguments as args before
    # __init__ runs: an __init__ that never calls super().__init__, a class
    # whose exception base is indirect, keyword-only __init__ parameters and
    # raise Cls(...) with keywords all keep them, as dynamic construction
    # already did. With no __init__ in the class, keywords are BaseException
    # __init__'s TypeError. A user __new__ still precedes __init__.
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))

    source = textwrap.dedent(
        """
        class A(Exception):
            pass


        class B(A):
            pass


        class NoSuper(Exception):
            def __init__(self, a, b):
                self.a = a


        class WithSuper(Exception):
            def __init__(self, a, b):
                super().__init__(a)
                self.b = b


        class Kw(ValueError):
            def __init__(self, a, *, code=0):
                self.code = code


        class Both:
            def __new__(cls, *a):
                obj = super().__new__(cls)
                obj.log = ["new"]
                return obj

            def __init__(self, x):
                self.log.append("init:" + str(x))


        class Other:
            def __new__(cls, x):
                return "not an instance"

            def __init__(self, x):
                raise AssertionError("init must not run")


        def make(cls, *args):
            return cls(*args)


        def main():
            print(str(A("x")), A("x").args, str(B("y")), B("y").args, B().args)
            print(str(NoSuper(1, 2)), NoSuper(1, 2).args, NoSuper(1, 2).a)
            print(str(WithSuper(1, 2)), WithSuper(1, 2).args)
            print(str(make(A, "d")), make(A, "d", 2).args, make(NoSuper, 3, 4).args, str(make(B)))
            print(Kw("m", code=3).args, Kw("m", code=3).code)
            print(Both(3).log, make(Both, 4).log, Other(1), make(Other, 2))
            try:
                raise NoSuper(5, 6)
            except NoSuper as e:
                print(repr(e), e.args)
            try:
                raise Kw("k", code=7)
            except Kw as e:
                print(e.args, e.code, str(e))
            try:
                A(x=1)
            except TypeError as e:
                print("TypeError", e)


        main()
        """
    )
    expected = (
        "x ('x',) y ('y',) ()\n"
        '(1, 2) (1, 2) 1\n'
        '1 (1,)\n'
        "d ('d', 2) (3, 4) \n"
        "('m',) 3\n"
        "['new', 'init:3'] ['new', 'init:4'] not an instance not an instance\n"
        'NoSuper(5, 6) (5, 6)\n'
        "('k',) 7 k\n"
        'TypeError A() takes no keyword arguments\n'
    )
    assert _cpython_output(source) == expected
    _run_on_five_collectors(tmp_path, "exception_args", source, expected)
