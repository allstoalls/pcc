"""CPython parity for class features, ported from
``Lib/test/test_class.py`` and ``Lib/test/test_descr.py``.

All tests use ``libpython_mode="off"``.

Reference contract (from CPython):
  * single inheritance + ``super()`` + MRO follow standard rules
  * ``isinstance`` / ``issubclass`` reflect the runtime class hierarchy
  * ``@property`` produces a data descriptor (getter, setter, deleter)
  * ``@staticmethod`` / ``@classmethod`` are non-data descriptors with
    standard binding
  * ``type(x)`` returns ``x.__class__``; ``type(x).__name__`` returns
    the class's source-declared name

Known pcc gaps marked xfail with citations to
``docs/python-limitations.md`` / ``docs/issues/python-data-model-gaps.md``.
"""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest


def _compile(monkeypatch, src: Path, exe: Path) -> None:
    from pcc.frontends.python.pipeline import compile_python

    compile_python(
        str(src), str(exe),
        ir_scaffold_mode="on", libpython_mode="off",
    )


def _run(exe: Path, timeout: float = 30.0) -> str:
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=timeout,
    )
    assert result.returncode == 0, (
        f"{exe.name} exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result.stdout


def test_class_basic_construction(tmp_path, monkeypatch):
    src = tmp_path / "class_basic.py"
    exe = tmp_path / "class_basic.out"
    src.write_text(textwrap.dedent("""
        class Point:
            def __init__(self, x: int, y: int) -> None:
                self.x = x
                self.y = y

            def magnitude_squared(self) -> int:
                return self.x * self.x + self.y * self.y

        def main() -> None:
            p = Point(3, 4)
            print(p.x, p.y)
            print(p.magnitude_squared())

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["3 4", "25"]


def test_unhinted_receiver_uses_runtime_method_when_names_are_ambiguous(
    tmp_path, monkeypatch
):
    src = tmp_path / "ambiguous_method_receiver.py"
    exe = tmp_path / "ambiguous_method_receiver.out"
    src.write_text(textwrap.dedent("""
        class Effect:
            def dispose(self) -> None:
                print("effect")

        class Context:
            def dispose(self) -> None:
                print("context")

        class Fiber:
            def __init__(self) -> None:
                self.context = None

        def deactivate(fiber) -> None:
            scope = fiber.context
            scope.dispose()

        def main() -> None:
            fiber = Fiber()
            fiber.context = Context()
            deactivate(fiber)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip() == "context"


def test_class_inheritance_super(tmp_path, monkeypatch):
    src = tmp_path / "class_inherit.py"
    exe = tmp_path / "class_inherit.out"
    src.write_text(textwrap.dedent("""
        class Animal:
            def __init__(self, name: str) -> None:
                self.name = name

            def greeting(self) -> str:
                return "I am " + self.name

        class Dog(Animal):
            def __init__(self, name: str, breed: str) -> None:
                super().__init__(name)
                self.breed = breed

            def greeting(self) -> str:
                base = super().greeting()
                return base + ", a " + self.breed

        def main() -> None:
            d = Dog("Rex", "labrador")
            print(d.greeting())
            print(d.name)
            print(d.breed)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == [
        "I am Rex, a labrador", "Rex", "labrador",
    ]


def test_class_isinstance(tmp_path, monkeypatch):
    src = tmp_path / "class_isinstance.py"
    exe = tmp_path / "class_isinstance.out"
    src.write_text(textwrap.dedent("""
        class A:
            pass

        class B(A):
            pass

        class C:
            pass

        def main() -> None:
            b = B()
            print(isinstance(b, B))
            print(isinstance(b, A))
            print(isinstance(b, C))

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["True", "True", "False"]


def test_class_property_getter_setter(tmp_path, monkeypatch):
    src = tmp_path / "class_property.py"
    exe = tmp_path / "class_property.out"
    src.write_text(textwrap.dedent("""
        class Box:
            def __init__(self, w: int, h: int) -> None:
                self._w = w
                self._h = h

            @property
            def area(self) -> int:
                return self._w * self._h

            @property
            def w(self) -> int:
                return self._w

            @w.setter
            def w(self, v: int) -> None:
                self._w = v

        def main() -> None:
            b = Box(3, 4)
            print(b.area)
            print(b.w)
            b.w = 5
            print(b.w)
            print(b.area)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["12", "3", "5", "20"]


def test_class_staticmethod(tmp_path, monkeypatch):
    src = tmp_path / "class_static.py"
    exe = tmp_path / "class_static.out"
    src.write_text(textwrap.dedent("""
        class Math:
            @staticmethod
            def add(a: int, b: int) -> int:
                return a + b

            @staticmethod
            def sub(a: int, b: int) -> int:
                return a - b

        def main() -> None:
            print(Math.add(3, 4))
            print(Math.sub(10, 7))
            m = Math()
            print(m.add(1, 1))

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["7", "3", "2"]


def test_class_dunder_eq(tmp_path, monkeypatch):
    src = tmp_path / "class_eq.py"
    exe = tmp_path / "class_eq.out"
    src.write_text(textwrap.dedent("""
        class V:
            def __init__(self, x: int) -> None:
                self.x = x

            def __eq__(self, other) -> bool:
                if isinstance(other, V):
                    return self.x == other.x
                return False

        def main() -> None:
            print(V(5) == V(5))
            print(V(5) == V(6))
            print(V(5) == 5)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["True", "False", "False"]


def test_class_dunder_call(tmp_path, monkeypatch):
    src = tmp_path / "class_call.py"
    exe = tmp_path / "class_call.out"
    src.write_text(textwrap.dedent("""
        class Adder:
            def __init__(self, base: int) -> None:
                self.base = base

            def __call__(self, x: int) -> int:
                return self.base + x

        def main() -> None:
            add5 = Adder(5)
            print(add5(3))
            print(add5(10))

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["8", "15"]

def test_class_type_builtin(tmp_path, monkeypatch):
    src = tmp_path / "class_type_builtin.py"
    exe = tmp_path / "class_type_builtin.out"
    src.write_text(textwrap.dedent("""
        class Foo:
            pass

        def main() -> None:
            f = Foo()
            print(type(f).__name__)
            print(type(42).__name__)
            print(type("hi").__name__)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["Foo", "int", "str"]

def test_class_class_var_read_write(tmp_path, monkeypatch):
    src = tmp_path / "class_var.py"
    exe = tmp_path / "class_var.out"
    src.write_text(textwrap.dedent("""
        class Counter:
            n = 0

            def bump(self) -> None:
                Counter.n = Counter.n + 1

        def main() -> None:
            print(Counter.n)
            Counter.n = 5
            print(Counter.n)
            c = Counter()
            c.bump()
            print(Counter.n)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["0", "5", "6"]


def test_class_user_iter(tmp_path, monkeypatch):
    src = tmp_path / "class_user_iter.py"
    exe = tmp_path / "class_user_iter.out"
    src.write_text(textwrap.dedent("""
        class Range3:
            def __init__(self) -> None:
                self.i = 0

            def __iter__(self):
                return self

            def __next__(self) -> int:
                if self.i >= 3:
                    raise StopIteration()
                v = self.i
                self.i = self.i + 1
                return v

        def main() -> None:
            r = Range3()
            for v in r:
                print(v)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["0", "1", "2"]


def test_class_subclass_type_name_resolution(tmp_path, monkeypatch):
    src = tmp_path / "subclass_type_name.py"
    exe = tmp_path / "subclass_type_name.out"
    src.write_text(textwrap.dedent("""
        class Base:
            pass

        class Sub(Base):
            pass

        def main() -> None:
            print(type(Sub()).__name__)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["Sub"]
