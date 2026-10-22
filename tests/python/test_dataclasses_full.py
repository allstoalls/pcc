"""``dataclasses`` module full contract.

The decorator already exists in ``pcc/stdlib/dataclasses.py`` but
the native class lowering now synthesizes the core dataclass methods
used by this contract.

Sub-protocols:

1. ``@dataclass`` synthesizes ``__init__`` / ``__repr__`` / ``__eq__``
2. Default values
3. ``field(default_factory=list)`` for mutable defaults
4. ``frozen=True`` makes assignment to fields raise
5. ``order=True`` synthesizes ``__lt__`` / ``__le__`` / ``__gt__`` /
   ``__ge__``
6. Inheritance — child fields appended after parent's
7. ``__post_init__`` hook fires after ``__init__``
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
# Basic synthesis: __init__, __repr__, __eq__
# ---------------------------------------------------------------------------


def test_dataclass_init_repr_eq(tmp_path):
    result = _compile_and_run(tmp_path, """
        from dataclasses import dataclass

        @dataclass
        class Point:
            x: int
            y: int

        def main() -> None:
            p = Point(1, 2)
            print(p.x, p.y)
            print(repr(p))
            print(p == Point(1, 2))
            print(p == Point(3, 4))

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    out = result.stdout.strip().split("\n")
    assert out[0] == "1 2"
    assert out[1] == "Point(x=1, y=2)"
    assert out[2] == "True"
    assert out[3] == "False"


def test_dataclass_eq_rejects_other_dataclass_shape(tmp_path):
    """Generated __eq__ must not read fields from an unrelated dataclass."""
    result = _compile_and_run(tmp_path, """
        from dataclasses import dataclass

        @dataclass
        class HasObj:
            obj: int

        @dataclass
        class HasValue:
            value: int

        def main() -> None:
            print(HasObj(1) == HasValue(1))
            print(HasValue(1) == HasObj(1))

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["False", "False"]


# ---------------------------------------------------------------------------
# Defaults / default_factory
# ---------------------------------------------------------------------------


def test_dataclass_default_values(tmp_path):
    result = _compile_and_run(tmp_path, """
        from dataclasses import dataclass

        @dataclass
        class Config:
            name: str
            timeout: int = 30

        def main() -> None:
            c = Config("svc")
            print(c.name, c.timeout)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "svc 30"


def test_dataclass_default_factory(tmp_path):
    """``default_factory=list`` produces a fresh list per instance —
    NOT a shared reference."""
    result = _compile_and_run(tmp_path, """
        from dataclasses import dataclass, field

        @dataclass
        class Bag:
            items: list = field(default_factory=list)

        def main() -> None:
            a = Bag()
            b = Bag()
            a.items.append(1)
            print(a.items)
            print(b.items)         # must be empty — independent

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["[1]", "[]"]


# ---------------------------------------------------------------------------
# frozen
# ---------------------------------------------------------------------------


def test_dataclass_frozen_blocks_assignment(tmp_path):
    result = _compile_and_run(tmp_path, """
        from dataclasses import dataclass

        @dataclass(frozen=True)
        class Const:
            v: int

        def main() -> None:
            c = Const(7)
            try:
                c.v = 99
                print("mutable")
            except Exception:
                print("frozen")

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "frozen"


# ---------------------------------------------------------------------------
# order
# ---------------------------------------------------------------------------


def test_dataclass_order_synthesizes_compares(tmp_path):
    result = _compile_and_run(tmp_path, """
        from dataclasses import dataclass

        @dataclass(order=True)
        class V:
            x: int

        def main() -> None:
            print(V(1) < V(2))
            print(V(2) <= V(2))
            print(V(3) > V(1))

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().split("\n") == ["True", "True", "True"]


# ---------------------------------------------------------------------------
# Inheritance
# ---------------------------------------------------------------------------


def test_dataclass_inheritance(tmp_path):
    result = _compile_and_run(tmp_path, """
        from dataclasses import dataclass

        @dataclass
        class Base:
            a: int

        @dataclass
        class Child(Base):
            b: int

        def main() -> None:
            c = Child(1, 2)
            print(c.a, c.b)
            print(repr(c))

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    out = result.stdout.strip().split("\n")
    assert out[0] == "1 2"
    assert "a=1" in out[1] and "b=2" in out[1]


def test_dataclass_inheritance_calls_inherited_post_init(tmp_path):
    result = _compile_and_run(tmp_path, """
        from dataclasses import dataclass

        @dataclass
        class Base:
            x: int
            def __post_init__(self):
                print("post", self.x)

        @dataclass
        class Child(Base):
            y: int

        def main() -> None:
            child = Child(7, 9)
            print(child.y)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines() == ["post 7", "9"]


# ---------------------------------------------------------------------------
# __post_init__
# ---------------------------------------------------------------------------


def test_dataclass_post_init(tmp_path):
    result = _compile_and_run(tmp_path, """
        from dataclasses import dataclass

        @dataclass
        class Box:
            x: int
            doubled: int = 0
            def __post_init__(self):
                self.doubled = self.x * 2

        def main() -> None:
            b = Box(7)
            print(b.doubled)

        if __name__ == "__main__":
            main()
        """)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "14"
