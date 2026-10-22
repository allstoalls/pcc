from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path


def _compile_and_run(
    tmp_path: Path,
    source: str,
    *,
    extra_files: dict[str, str] | None = None,
) -> list[str]:
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "probe.py"
    exe = tmp_path / "probe.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    for name, content in (extra_files or {}).items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
    compile_python(str(src), str(exe), ir_scaffold_mode="on", libpython_mode="off")
    env = os.environ.copy()
    env["PCC_PYTHON_LIBPYTHON"] = "off"
    proc = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, cwd=tmp_path, env=env
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout.strip().splitlines()


def test_d8_dynamic_import_and_introspection_compiled(tmp_path):
    lines = _compile_and_run(
        tmp_path,
        """
        import importlib
        import inspect
        m = importlib.import_module("mod_d8")
        print(m.value)
        print(hasattr(m, "value"))
        print(getattr(m, "value"))
        print(type(m).__name__)
        print(inspect.getdoc(m.fn))
        print(inspect.isfunction(m.fn))
        """,
        extra_files={
            "mod_d8.py": """
            value = 42
            def fn():
                "doc"
                return value
            """,
        },
    )
    assert lines == ["42", "True", "42", "module", "doc", "True"]


def test_t1_metaclass_type_enum_abcmeta_compiled(tmp_path):
    lines = _compile_and_run(
        tmp_path,
        """
        from enum import Enum, auto
        from abc import ABC, abstractmethod

        class M(type):
            def __new__(mcls, name, bases, ns):
                ns["marker"] = "ok"
                return type.__new__(mcls, name, bases, ns)

        class C(metaclass=M):
            pass

        D = type("D", (C,), {"x": 3})
        print(C.marker)
        print(D.x)
        print(isinstance(D(), C))

        class Color(Enum):
            RED = auto()
            BLUE = auto()

        print(Color.RED.name)
        print(Color.BLUE.value)

        class Base(ABC):
            @abstractmethod
            def f(self):
                pass

        print("f" in Base.__abstractmethods__)
        """,
    )
    assert lines == ["ok", "3", "True", "RED", "2", "True"]


def test_t1_metaclass_super_new_can_set_class_attributes(tmp_path):
    lines = _compile_and_run(
        tmp_path,
        """
        class Meta(type):
            def __new__(mcls, name, bases, ns):
                cls = super().__new__(mcls, name, bases, ns)
                cls.marker = "ok"
                return cls

        class Host(metaclass=Meta):
            pass

        print(Host.marker)
        """,
    )
    assert lines == ["ok"]


def test_t1_native_unary_class_decorator_rebinds_class(tmp_path):
    lines = _compile_and_run(
        tmp_path,
        """
        def label(cls):
            cls.marker = "decorated"
            return cls

        @label
        class Host:
            pass

        print(Host.marker)
        """,
    )
    assert lines == ["decorated"]


def test_t1_dynamic_issubclass_class_argument(tmp_path):
    lines = _compile_and_run(
        tmp_path,
        """
        class Base:
            pass

        class Child(Base):
            pass

        def is_base(cls):
            return issubclass(cls, Base)

        print(is_base(Child))
        print(is_base(Base))
        """,
    )
    assert lines == ["True", "True"]


def test_t1_class_base_metadata(tmp_path):
    lines = _compile_and_run(
        tmp_path,
        """
        class Base:
            pass

        class Child(Base):
            pass

        print(Child.__base__.__name__)
        """,
    )
    assert lines == ["Base"]


def test_t1_memory_error_keeps_builtin_exception_hierarchy(tmp_path):
    lines = _compile_and_run(
        tmp_path,
        """
        class AllocationError(MemoryError):
            pass

        print(issubclass(AllocationError, MemoryError))
        print(issubclass(AllocationError, Exception))
        print(AllocationError.__base__.__name__)
        """,
    )
    assert lines == ["True", "True", "MemoryError"]


def test_t2_typing_runtime_protocol_generic_compiled(tmp_path):
    lines = _compile_and_run(
        tmp_path,
        """
        from typing import Generic, Protocol, TypeVar, runtime_checkable, get_origin, get_args, Optional
        T = TypeVar("T")

        class Box(Generic[T]):
            def __init__(self, value):
                self.value = value

        @runtime_checkable
        class Named(Protocol):
            name: str

        class User:
            def __init__(self):
                self.name = "pcc"

        alias = Optional[int]
        print(Box(7).value)
        print(isinstance(User(), Named))
        print(get_origin(alias).__name__)
        print(get_args(alias)[0].__name__)
        """,
    )
    assert lines == ["7", "True", "Optional", "int"]


def test_t3_mutable_dataclass_default_none_setattr_compiled(tmp_path):
    lines = _compile_and_run(
        tmp_path,
        """
        from dataclasses import dataclass

        @dataclass
        class Node:
            child: object = None

        a = Node()
        b = Node()
        a.child = b
        print(a.child is b)
        print(b.child is None)
        b.child = "x"
        print(a.child.child)
        """,
    )
    assert lines == ["True", "True", "x"]
