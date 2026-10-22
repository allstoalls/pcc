"""``inspect`` module contract — locks the surface that compile-time
introspection hooks need.

Phase: pulled out of D8 (dynamic import) into its own file because
``inspect`` is the dependency for tools that build on top of pcc
(test discovery, debuggers, type checkers reading runtime info).

Sub-protocols:

1. ``inspect.signature(fn)`` — readable parameter list
2. ``inspect.getsource(fn)`` — source text from the .py file
3. ``inspect.getmro(cls)`` — same as ``cls.__mro__``
4. ``inspect.isfunction`` / ``inspect.ismethod`` /
   ``inspect.isclass`` predicates
5. ``inspect.getfullargspec`` for legacy callers (e.g. older test
   frameworks)
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
        [str(exe)], capture_output=True, text=True, timeout=20,
    )


# ---------------------------------------------------------------------------
# signature
# ---------------------------------------------------------------------------


def test_signature_basic(tmp_path):
    result = _compile_and_run(tmp_path, """
        import inspect

        def add(a, b, c=0):
            return a + b + c

        def main() -> None:
            sig = inspect.signature(add)
            print(str(sig))                 # '(a, b, c=0)'

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "(a, b, c=0)"


def test_signature_parameters_iter(tmp_path):
    result = _compile_and_run(tmp_path, """
        import inspect

        def f(x, y):
            return x + y

        def main() -> None:
            sig = inspect.signature(f)
            for name in sig.parameters:
                print(name)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["x", "y"]


# ---------------------------------------------------------------------------
# getsource
# ---------------------------------------------------------------------------


def test_getsource_returns_text(tmp_path):
    """``getsource`` reads the .py file at runtime — needs a
    source-table embedded in the compiled binary or a runtime path."""
    result = _compile_and_run(tmp_path, """
        import inspect

        def hello():
            return 42

        def main() -> None:
            txt = inspect.getsource(hello)
            print("def hello" in txt)
            print("return 42" in txt)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["True", "True"]


# ---------------------------------------------------------------------------
# getmro / predicates
# ---------------------------------------------------------------------------


def test_getmro_returns_chain(tmp_path):
    result = _compile_and_run(tmp_path, """
        import inspect

        class A:
            pass
        class B(A):
            pass

        def main() -> None:
            mro = inspect.getmro(B)
            for cls in mro:
                print(cls.__name__)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["B", "A", "object"]


def test_predicates(tmp_path):
    result = _compile_and_run(tmp_path, """
        import inspect

        def f():
            pass

        class C:
            def m(self):
                pass

        def main() -> None:
            print(inspect.isfunction(f))
            print(inspect.isclass(C))
            print(inspect.ismethod(C().m))

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["True", "True", "True"]


# ---------------------------------------------------------------------------
# getfullargspec — legacy interface
# ---------------------------------------------------------------------------


def test_getfullargspec(tmp_path):
    result = _compile_and_run(tmp_path, """
        import inspect

        def fn(a, b=1, *args, **kwargs):
            return None

        def main() -> None:
            spec = inspect.getfullargspec(fn)
            print(spec.args)         # ['a', 'b']
            print(spec.varargs)      # 'args'
            print(spec.varkw)        # 'kwargs'

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    out = result.stdout.strip().split("\n")
    assert out[0] == "['a', 'b']"
    assert out[1] == "args"
    assert out[2] == "kwargs"
