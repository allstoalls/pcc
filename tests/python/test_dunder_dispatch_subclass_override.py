"""A statically hinted dunder call must not bypass a subclass override.

``_try_dispatch_dunder_unary`` called the hinted class's dunder directly.  A
hint names a compatible class, not the receiver's runtime class, so
``value.type == _CSTR`` on an ``ir.Type`` hint ran ``Type.__eq__`` (type and
text only) for a ``PointerType`` receiver.  pcc1 therefore treated every
pointer type as ``i8*``, skipped casts, and emitted different IR from the host
compiler for the same module.  Receivers whose hinted class has an overriding
subclass now take the dynamic protocol; this program must print what CPython
prints.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from pcc.frontends.python.pipeline import compile_python

PROGRAM = '''
class Base:
    def __init__(self, n: int) -> None:
        self.n = n

    def __str__(self) -> str:
        return "base"

    def __eq__(self, other) -> bool:
        return type(self) is type(other) and str(self) == str(other)

    def __hash__(self) -> int:
        return 1

    def __lt__(self, other) -> bool:
        return False

    def __add__(self, other):
        return "base-add"

    def __len__(self) -> int:
        return 1

    def __getitem__(self, index: int):
        return "base-item"

    def same_as(self, other) -> bool:
        return self == other


class Leaf(Base):
    def __str__(self) -> str:
        return "leaf"


class Ptr(Base):
    def __init__(self, n: int, inner: Base) -> None:
        self.n = n
        self.inner = inner

    def __str__(self) -> str:
        return "ptr"

    def __eq__(self, other) -> bool:
        return isinstance(other, Ptr) and self.inner == other.inner

    def __hash__(self) -> int:
        return 2

    def __lt__(self, other) -> bool:
        return True

    def __add__(self, other):
        return "ptr-add"

    def __len__(self) -> int:
        return 7

    def __getitem__(self, index: int):
        return "ptr-item"


class Holder:
    def __init__(self, value: Base) -> None:
        self.value = value


LEAF = Leaf(1)
CSTR = Ptr(2, LEAF)


def via_field(h: Holder) -> str:
    v = h.value
    return " ".join([
        str(h.value == CSTR),
        str(h.value < CSTR),
        str(h.value + CSTR),
        str(len(h.value)),
        str(h.value[0]),
    ])


def via_param(b: Base) -> str:
    return str(b == CSTR) + " " + str(b < CSTR)


def main() -> None:
    nested = Holder(Ptr(3, Ptr(4, LEAF)))
    plain = Holder(Ptr(5, LEAF))
    print(via_field(nested))
    print(via_field(plain))
    print(via_field(Holder(Leaf(6))))
    print(via_param(Ptr(7, Ptr(8, LEAF))), via_param(Leaf(9)))
    print(Ptr(10, Ptr(11, LEAF)).same_as(CSTR), Leaf(12).same_as(Leaf(13)))


main()
'''


BINOP_PROGRAM = '''
class Base:
    def __init__(self, n: int) -> None:
        self.n = n

    def __str__(self) -> str:
        return "B" + str(self.n)

    def __sub__(self, other):
        return "base-sub"

    def __rsub__(self, other):
        return "base-rsub"


class Ptr(Base):
    def __add__(self, other):
        return "ptr-add"

    def __radd__(self, other):
        return "ptr-radd:" + str(other)

    def __sub__(self, other):
        return NotImplemented

    def __rsub__(self, other):
        return "ptr-rsub"

    def __mul__(self, other):
        return "ptr-mul"

    def __rmul__(self, other):
        return "ptr-rmul"

    def __pow__(self, other):
        return "ptr-pow"


class Other(Base):
    def __rsub__(self, other):
        return "other-rsub"


class Holder:
    def __init__(self, value: Base) -> None:
        self.value = value


def f(h: Holder) -> str:
    return str(h.value + 1)


def g(b: Base) -> str:
    return str(b + 1.5)


def rev(b: Base) -> str:
    return str(1 + b) + " " + str(2.5 * b) + " " + str("s" + b)


def fmt(b: Base) -> str:
    return "v=%s" % b


def sub(a: Base, b: Base) -> str:
    try:
        return str(a - b)
    except TypeError:
        return "TypeError"


def powr(a: Base) -> str:
    return str(a ** 2)


def missing(b: Base) -> str:
    try:
        return str(b / 2)
    except TypeError:
        return "TypeError"


print(f(Holder(Ptr(1))))
print(g(Ptr(2)))
print(rev(Ptr(3)))
print(fmt(Ptr(4)))
# same type: Ptr.__sub__ -> NotImplemented and no reflected retry
print(sub(Ptr(5), Ptr(6)))
# Ptr.__sub__ NotImplemented, then Other.__rsub__
print(sub(Ptr(5), Other(6)))
# Other subclasses Base and overrides __rsub__: it answers first
print(sub(Base(5), Other(6)))
# Ptr overrides __rsub__ too, subclass of Base: first
print(sub(Base(5), Ptr(6)))
# plain Base - Base
print(sub(Base(5), Base(6)))
print(powr(Ptr(7)))
print(missing(Ptr(8)))
'''


def test_hinted_dunders_follow_subclass_overrides(tmp_path):
    src = tmp_path / "dunder_override.py"
    exe = tmp_path / "dunder_override"
    src.write_text(textwrap.dedent(PROGRAM).lstrip(), encoding="utf-8")
    expected = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True, timeout=60,
    )
    assert expected.returncode == 0, expected.stderr
    compile_python(
        str(src), str(exe),
        libpython_mode="off", ir_scaffold_mode="on", backend="self",
    )
    for backend in ("0", "4"):
        run = subprocess.run(
            [str(exe)], capture_output=True, text=True, timeout=60,
            env={"PCC_GC_BACKEND": backend, "PATH": "/usr/bin:/bin"},
        )
        assert run.returncode == 0, run.stdout + run.stderr
        assert run.stdout == expected.stdout, (backend, run.stdout, expected.stdout)


@pytest.mark.parametrize("mode", ["on", "off"])
def test_dynamic_binary_dunders_follow_the_python_protocol(tmp_path, mode):
    """A class-typed operand whose dunder the hint cannot vouch for goes
    through the runtime protocol: forward, then reflected for another type,
    subclass-first reflection, ``str % instance`` formatting, TypeError."""
    src = tmp_path / f"binop_{mode}.py"
    exe = tmp_path / f"binop_{mode}"
    src.write_text(BINOP_PROGRAM, encoding="utf-8")
    expected = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True, timeout=60,
    )
    assert expected.returncode == 0, expected.stderr
    compile_python(
        str(src), str(exe),
        libpython_mode="off", ir_scaffold_mode=mode, backend="self",
    )
    for backend in ("0", "1", "2", "3", "4"):
        run = subprocess.run(
            [str(exe)], capture_output=True, text=True, timeout=60,
            env={"PCC_GC_BACKEND": backend, "PATH": "/usr/bin:/bin"},
        )
        assert run.returncode == 0, run.stdout + run.stderr
        assert run.stdout == expected.stdout, (backend, run.stdout, expected.stdout)
