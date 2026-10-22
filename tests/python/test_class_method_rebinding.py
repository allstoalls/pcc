"""Rebinding or deleting a class's method reaches every lookup, as in CPython."""

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


def test_rebound_and_deleted_methods_reach_construction_and_dispatch(tmp_path, monkeypatch, pcc_runtime_archive):
    # Base.__init__ = f must construct Base() and an inheriting Child() with
    # f, a rebound method and a newly assigned __str__ answer calls and str(),
    # and del Base.greet must leave no method behind. The class's method
    # table used to keep the compiled originals (a crash on four collectors).
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))

    source = textwrap.dedent(
        """
        class Base:
            def __init__(self, x):
                self.x = x

            def greet(self):
                return "base"


        class Child(Base):
            pass


        def replacement(self):
            self.x = "rebound"


        def shout(self):
            return "shout:" + str(self.x)


        def as_text(self):
            return "str:" + str(self.x)


        def show(label, thunk):
            try:
                print(label, repr(thunk()))
            except Exception as e:
                print(label, type(e).__name__, e)


        def main():
            show("before", lambda: (Base(1).x, Base(1).greet()))
            Base.__init__ = replacement
            show("init", lambda: (Base().x, Child().x))
            Base.greet = shout
            show("method", lambda: (Base().greet(), Child().greet()))
            Base.__str__ = as_text
            show("dunder", lambda: (str(Base()), str(Child())))
            del Base.greet
            show("deleted", lambda: Base().greet())
            show("child-deleted", lambda: Child().greet())
            cls = Base
            show("dynamic", lambda: cls().x)


        main()
        """
    )
    expected = (
        "before (1, 'base')\n"
        "init ('rebound', 'rebound')\n"
        "method ('shout:rebound', 'shout:rebound')\n"
        "dunder ('str:rebound', 'str:rebound')\n"
        "deleted AttributeError 'Base' object has no attribute 'greet'\n"
        "child-deleted AttributeError 'Child' object has no attribute 'greet'\n"
        "dynamic 'rebound'\n"
    )
    assert _cpython_output(source) == expected
    _run_on_five_collectors(tmp_path, "class_method_rebinding", source, expected)
