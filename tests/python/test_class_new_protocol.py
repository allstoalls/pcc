"""``__new__`` runs even when the class also defines ``__init__``.

CPython constructs with ``obj = Cls.__new__(Cls, *args)`` and then applies
``Cls.__init__(obj, *args)`` to whatever ``__new__`` returned.  pcc consulted
``__new__`` only when a class had no ``__init__``, so every interning or
singleton ``__new__`` that also defined one was silently skipped:
``pcc/llvm_capi/ir.py`` interns ``IntType`` per width in ``__new__`` and
defines ``__init__`` as well, so ``IntType(8) is IntType(8)`` was False where
CPython says True -- and codegen's ``value.type is _I64`` identity checks
depend on that interning holding.

The class whose ``__new__`` had no ``__init__`` beside it (``VoidType`` and
the other ``_SingletonType`` scalars) already worked, which is why the gap
survived: the singleton case looked correct.
"""

from __future__ import annotations

import subprocess


_SOURCE = '''
class Type:
    _name = ""

    def __str__(self) -> str:
        return self._name


class _SingletonType(Type):
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance


class VoidType(_SingletonType):
    _name = "void"


class IntType(Type):
    _cache: dict = {}

    def __new__(cls, width: int):
        existing = cls._cache.get(width)
        if existing is not None:
            return existing
        obj = super().__new__(cls)
        obj.width = width
        cls._cache[width] = obj
        return obj

    def __init__(self, width: int) -> None:
        self.width = width

    def __str__(self) -> str:
        return "i" + str(self.width)

    def __call__(self, value: int) -> str:
        return str(self) + "=" + str(value)


class Counted:
    made = 0

    def __new__(cls, tag: str):
        Counted.made = Counted.made + 1
        return super().__new__(cls)

    def __init__(self, tag: str) -> None:
        self.tag = tag


def main() -> None:
    print(str(IntType(1)))
    print(str(IntType(1)(1)))
    # Interning: __new__ returns the cached object, __init__ re-runs on it.
    print(IntType(8) is IntType(8))
    print(IntType(8).width)
    # A singleton whose class has no __init__ kept working throughout.
    print(str(VoidType()))
    print(VoidType() is VoidType())
    # __new__ side effects happen once per construction.
    first = Counted("a")
    second = Counted("b")
    print(Counted.made, first.tag, second.tag)
    print(first is second)


main()
'''


def _build_and_run(tmp_path, name, source):
    from pcc.py_frontend.pipeline import compile_python

    src = tmp_path / (name + ".py")
    exe = tmp_path / (name + ".out")
    src.write_text(source, encoding="utf-8")
    compile_python(str(src), str(exe), libpython_mode="off", backend="self")
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=180, check=True
    )
    reference = subprocess.run(
        ["python3", str(src)], text=True, capture_output=True, timeout=180, check=True
    )
    assert run.stdout == reference.stdout, (
        "pcc:\n" + run.stdout + "\ncpython:\n" + reference.stdout
    )
    return run.stdout


def test_new_and_init_both_run_and_match_cpython(tmp_path):
    out = _build_and_run(tmp_path, "class_new_protocol", _SOURCE)
    assert out.splitlines() == [
        "i1",
        "i1=1",
        "True",
        "8",
        "void",
        "True",
        "2 a b",
        "False",
    ]


_FOREIGN_NEW = '''
class Other:
    def __init__(self) -> None:
        self.marker = "other"


class Odd:
    def __new__(cls):
        return Other()

    def __init__(self) -> None:
        self.marker = "odd"


def main() -> None:
    made = Odd()
    print(made.marker)


main()
'''


def test_init_is_skipped_when_new_returns_a_foreign_object(tmp_path):
    """``__init__`` runs only on an instance of the class being constructed.

    A ``__new__`` that returns a foreign object leaves it untouched, so
    ``Odd()`` keeps ``Other``'s marker.  The lowering emits
    ``py_obj_isinstance(obj, cls)`` and branches on it rather than assuming
    every ``__new__`` in a program returns its own class.
    """
    out = _build_and_run(tmp_path, "class_new_foreign", _FOREIGN_NEW)
    assert out.strip() == "other"
