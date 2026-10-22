"""Phase D5 — protocol edges contract.

Locks corner cases of dunder protocols that today's pcc handles
partially or not at all. See ``docs/issues/python-data-model-gaps.md``
Phase D5.

Sub-protocols locked by this test:

1. ``__hash__`` set to ``None`` makes the type unhashable
2. ``__eq__`` without ``__hash__`` makes the type unhashable
3. ``__bool__`` falls back to ``__len__`` when missing
4. ``__index__`` for slice/integer coercion
5. ``__contains__`` overrides ``in`` operator
6. NotImplemented returned from a binop falls back to reflected op
7. ``__getattr__`` only fires on AttributeError from normal lookup
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
# Hashability
# ---------------------------------------------------------------------------


def test_hash_none_makes_unhashable(tmp_path):
    """Setting ``__hash__ = None`` must make ``hash(obj)`` raise
    ``TypeError``."""
    result = _compile_and_run(tmp_path, """
        class NoHash:
            __hash__ = None

        def main() -> None:
            x = NoHash()
            try:
                hash(x)
                print("hashed")
            except TypeError:
                print("TypeError")

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "TypeError"


def test_eq_without_hash_unhashable(tmp_path):
    """Defining ``__eq__`` without ``__hash__`` must auto-clear hash
    (CPython sets ``__hash__`` to None)."""
    result = _compile_and_run(tmp_path, """
        class Point:
            def __init__(self, x):
                self.x = x
            def __eq__(self, other):
                return self.x == other.x

        def main() -> None:
            p = Point(1)
            try:
                hash(p)
                print("hashed")
            except TypeError:
                print("TypeError")

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "TypeError"


# ---------------------------------------------------------------------------
# __bool__ / __len__ fallback
# ---------------------------------------------------------------------------


def test_bool_falls_back_to_len(tmp_path):
    """Without ``__bool__``, ``bool(obj)`` consults ``__len__``."""
    result = _compile_and_run(tmp_path, """
        class Empty:
            def __len__(self):
                return 0

        class Full:
            def __len__(self):
                return 7

        def main() -> None:
            print(bool(Empty()))
            print(bool(Full()))

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["False", "True"]


# ---------------------------------------------------------------------------
# __index__ for integer coercion
# ---------------------------------------------------------------------------


def test_index_coerces_in_slice(tmp_path):
    """Custom int-like classes can implement ``__index__`` to be used
    as slice/sequence indices."""
    result = _compile_and_run(tmp_path, """
        class IntLike:
            def __init__(self, v):
                self.v = v
            def __index__(self):
                return self.v

        def main() -> None:
            xs = [10, 20, 30, 40, 50]
            i = IntLike(2)
            print(xs[i])             # 30
            print(xs[IntLike(1):IntLike(4)])  # [20, 30, 40]

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["30", "[20, 30, 40]"]


# ---------------------------------------------------------------------------
# __contains__ override
# ---------------------------------------------------------------------------


def test_contains_override(tmp_path):
    result = _compile_and_run(tmp_path, """
        class EvenSet:
            def __contains__(self, x):
                return x % 2 == 0

        def main() -> None:
            s = EvenSet()
            print(4 in s)
            print(7 in s)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["True", "False"]


# ---------------------------------------------------------------------------
# NotImplemented binop fallback
# ---------------------------------------------------------------------------


def test_notimplemented_falls_back_to_reflected(tmp_path):
    """If ``a.__add__(b)`` returns NotImplemented, Python tries
    ``b.__radd__(a)``."""
    result = _compile_and_run(tmp_path, """
        class A:
            def __add__(self, other):
                return NotImplemented

        class B:
            def __radd__(self, other):
                return "from-radd"

        def main() -> None:
            print(A() + B())

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "from-radd"


# ---------------------------------------------------------------------------
# __getattr__ vs __getattribute__
# ---------------------------------------------------------------------------


def test_getattr_only_on_attribute_error(tmp_path):
    """``__getattr__`` only fires when normal lookup raises
    AttributeError, NOT for every attribute access."""
    result = _compile_and_run(tmp_path, """
        class Dynamic:
            def __init__(self):
                self.x = 1
            def __getattr__(self, name):
                return "fallback:" + name

        def main() -> None:
            d = Dynamic()
            print(d.x)        # 1 — normal attr, __getattr__ NOT called
            print(d.missing)  # 'fallback:missing'

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["1", "fallback:missing"]
