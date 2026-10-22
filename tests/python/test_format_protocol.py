"""Phase D6 — formatting protocol contract.

Locks the contract for ``format()`` / ``__format__`` / f-string
formatting / ``str.format`` per
``docs/issues/python-data-model-gaps.md`` Phase D6.

Sub-protocols locked by this test:

1. ``format(obj, spec)`` calls ``obj.__format__(spec)``
2. ``__format__`` default implementation falls back to ``__str__``
3. f-string format spec is forwarded to ``__format__``
4. ``str.format`` field references call ``__format__``
5. Built-in numeric format specs: width, alignment, padding, signed
6. Built-in string format specs: width, alignment, max-width
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
# format(obj, spec) → __format__
# ---------------------------------------------------------------------------


def test_format_calls_dunder_format(tmp_path):
    result = _compile_and_run(tmp_path, """
        class Money:
            def __init__(self, v):
                self.v = v
            def __format__(self, spec):
                return "$" + str(self.v)

        def main() -> None:
            print(format(Money(10), ""))
            print(format(Money(99), "currency"))  # spec passed through

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["$10", "$99"]


def test_default_format_falls_back_to_str(tmp_path):
    """Object with ``__str__`` but not ``__format__`` and empty spec
    must produce ``str(obj)``."""
    result = _compile_and_run(tmp_path, """
        class Stringy:
            def __str__(self):
                return "stringy-repr"

        def main() -> None:
            print(format(Stringy(), ""))

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "stringy-repr"


# ---------------------------------------------------------------------------
# f-string and str.format
# ---------------------------------------------------------------------------


def test_fstring_forwards_spec(tmp_path):
    result = _compile_and_run(tmp_path, """
        class Tagged:
            def __format__(self, spec):
                return "[" + spec + "]"

        def main() -> None:
            x = Tagged()
            print(f"{x}")        # spec=""
            print(f"{x:wide}")   # spec="wide"
            print(f"{x:>10}")    # spec=">10"

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["[]", "[wide]", "[>10]"]


def test_str_format_calls_dunder_format(tmp_path):
    result = _compile_and_run(tmp_path, """
        class Money:
            def __init__(self, v):
                self.v = v
            def __format__(self, spec):
                return "$" + str(self.v)

        def main() -> None:
            m = Money(7)
            print("price = {}".format(m))
            print("price = {:nice}".format(m))

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    out = result.stdout.strip().split("\n")
    assert out == ["price = $7", "price = $7"]


# ---------------------------------------------------------------------------
# Built-in numeric / string format specs
# ---------------------------------------------------------------------------


def test_numeric_format_spec(tmp_path):
    result = _compile_and_run(tmp_path, """
        def main() -> None:
            print(format(42, "5d"))      # right-aligned width 5
            print(format(42, "<5d"))     # left
            print(format(42, "05d"))     # zero-pad
            print(format(-7, "+05d"))    # with sign

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "   42", "42   ", "00042", "-0007",
    ]


def test_string_format_spec(tmp_path):
    result = _compile_and_run(tmp_path, """
        def main() -> None:
            print(format("hi", ">5"))   # right-align width 5
            print(format("hi", "<5"))   # left
            print(format("hi", "^5"))   # center
            print(format("hello world", ".5"))  # max-width 5

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "   hi", "hi   ", " hi  ", "hello",
    ]


def test_float_format_spec(tmp_path):
    result = _compile_and_run(tmp_path, """
        def main() -> None:
            print(format(3.14159, ".2f"))      # 3.14
            print(format(1234.5,  ",.2f"))     # 1,234.50
            print(format(0.0007,  ".2e"))      # 7.00e-04

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == [
        "3.14", "1,234.50", "7.00e-04",
    ]
