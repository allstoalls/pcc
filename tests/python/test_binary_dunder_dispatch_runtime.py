"""Runtime binary-dunder dispatch + emission err-checks (audit Review 2).

The err-check audit's binary-op family was a TRUE positive twice over:
(1) `py_obj_add/sub/mul` never dispatched user `__add__`/`__sub__`/
`__mul__` (instances fell to "unsupported operand" TypeError), and
(2) the obj add/sub/mul emission sites had no post-call err-check, so
the pending exception printed the success line and detonated in a
LATER try block (the wrong-catch shape). Both tiers now dispatch
through `py_user_binop_dispatch` (lookup __op__, NotImplemented ->
reflected __rop__, else TypeError) and the emission sites check.
CPython oracle: each operation raises ValueError from the user dunder,
caught by the enclosing except.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

from pcc.frontends.python.pipeline import compile_python

_SOURCE = """
class Angry:
    def __add__(self, other):
        raise ValueError("add-boom")

    def __sub__(self, other):
        raise ValueError("sub-boom")

    def __mul__(self, other):
        raise ValueError("mul-boom")


class Radd:
    def __radd__(self, other):
        return 42


class Rmod:
    def __rmod__(self, other):
        return 8


class AngryMod:
    def __mod__(self, other):
        raise ValueError("mod-boom")


class AngryDiv:
    def __truediv__(self, other):
        raise ValueError("div-boom")

    def __floordiv__(self, other):
        raise ValueError("floordiv-boom")


class AngryBits:
    def __and__(self, other):
        raise ValueError("and-boom")

    def __or__(self, other):
        raise ValueError("or-boom")

    def __xor__(self, other):
        raise ValueError("xor-boom")

    def __lshift__(self, other):
        raise ValueError("lshift-boom")

    def __rshift__(self, other):
        raise ValueError("rshift-boom")


class Rbits:
    def __rand__(self, other):
        return 81

    def __ror__(self, other):
        return 82

    def __rxor__(self, other):
        return 83

    def __rlshift__(self, other):
        return 84

    def __rrshift__(self, other):
        return 85


class Rdiv:
    def __rtruediv__(self, other):
        return 7


class AngryIadd:
    def __iadd__(self, o):
        raise ValueError("iadd-boom")


class GrowIadd:
    def __init__(self):
        self.v = 1

    def __iadd__(self, o):
        self.v = self.v + o
        return self


class Holder:
    def __init__(self, item):
        self.item = item


def take(a):
    try:
        _ = a + 1
        print("add-ok")
    except ValueError:
        print("add-err")
    try:
        _ = a - 1
        print("sub-ok")
    except ValueError:
        print("sub-err")
    try:
        _ = a * 2
        print("mul-ok")
    except ValueError:
        print("mul-err")


def radd_case(lhs, r):
    return lhs + r


def mod_take(a):
    try:
        _ = a % 2
        print("mod-ok")
    except ValueError:
        print("mod-err")


def rmod_case(lhs, r):
    return lhs % r


def div_take(a):
    try:
        _ = a / 2
        print("div-ok")
    except ValueError:
        print("div-err")
    try:
        _ = a // 2
        print("floordiv-ok")
    except ValueError:
        print("floordiv-err")


def rdiv_case(lhs, r):
    return lhs / r


def bit_take(a):
    try:
        _ = a & 1
        print("and-ok")
    except ValueError:
        print("and-err")
    try:
        _ = a | 1
        print("or-ok")
    except ValueError:
        print("or-err")
    try:
        _ = a ^ 1
        print("xor-ok")
    except ValueError:
        print("xor-err")
    try:
        _ = a << 1
        print("lshift-ok")
    except ValueError:
        print("lshift-err")
    try:
        _ = a >> 1
        print("rshift-ok")
    except ValueError:
        print("rshift-err")


def reflected_bits(lhs, rhs):
    print(lhs & rhs)
    print(lhs | rhs)
    print(lhs ^ rhs)
    print(lhs << rhs)
    print(lhs >> rhs)


def iadd_take(a):
    try:
        a += 1
        print("iadd-ok")
    except ValueError:
        print("iadd-err")


def iadd_use(b):
    b += 41
    print(b.v)


def subscript_iadd_take(a):
    xs = [a]
    try:
        xs[0] += 1
        print("sub-iadd-ok")
    except ValueError:
        print("sub-iadd-err")


def subscript_iadd_use(b):
    xs = [b]
    xs[0] += 41
    print(xs[0].v)


def attr_iadd_take(a):
    box = Holder(a)
    try:
        box.item += 1
        print("attr-iadd-ok")
    except ValueError:
        print("attr-iadd-err")


def attr_iadd_use(b):
    box = Holder(b)
    box.item += 41
    print(box.item.v)


def main() -> int:
    take(Angry())
    print(radd_case(1, Radd()))
    mod_take(AngryMod())
    print(rmod_case(1, Rmod()))
    div_take(AngryDiv())
    print(rdiv_case(1, Rdiv()))
    bit_take(AngryBits())
    reflected_bits(1, Rbits())
    iadd_take(AngryIadd())
    iadd_use(GrowIadd())
    subscript_iadd_take(AngryIadd())
    subscript_iadd_use(GrowIadd())
    attr_iadd_take(AngryIadd())
    attr_iadd_use(GrowIadd())
    return 0


main()
"""


@pytest.mark.parametrize("runtime_cc", [False], ids=["port"])
def test_binary_dunder_dispatch_and_err_check(tmp_path, monkeypatch, runtime_cc):
    src = tmp_path / "binop_probe.py"
    exe = tmp_path / "binop_probe"
    src.write_text(dedent(_SOURCE), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
    )
    proc = subprocess.run(
        [str(exe)], text=True, capture_output=True, check=True, timeout=30
    )
    assert [l for l in proc.stdout.splitlines() if l] == [
        "add-err",
        "sub-err",
        "mul-err",
        "42",
        "mod-err",
        "8",
        "div-err",
        "floordiv-err",
        "7",
        "and-err",
        "or-err",
        "xor-err",
        "lshift-err",
        "rshift-err",
        "81",
        "82",
        "83",
        "84",
        "85",
        "iadd-err",
        "42",
        "sub-iadd-err",
        "42",
        "attr-iadd-err",
        "42",
    ]


_AUGASSIGN_SOURCE = '''
class Counter:
    def __init__(self, n: int):
        self.n = n
    def __iadd__(self, o):
        self.n += o
        return self
    def __imul__(self, o):
        self.n *= o
        return self
    def __repr__(self) -> str:
        return "C(" + str(self.n) + ")"


class Vec:
    def __init__(self, x: int):
        self.x = x
    def __add__(self, o):
        return Vec(self.x + o.x)
    def __repr__(self) -> str:
        return "V" + str(self.x)


def main():
    c = Counter(5)      # statically typed ClassType target
    c += 3
    print(c)            # C(8)  (__iadd__)
    c *= 2
    print(c)            # C(16) (__imul__)
    v = Vec(1)
    v += Vec(2)         # no __iadd__ -> falls back to __add__
    print(v)            # V3


main()
'''


@pytest.mark.parametrize("runtime_cc", [False], ids=["port"])
def test_classtype_augassign_dispatches_inplace_dunder(tmp_path, monkeypatch, runtime_cc):
    """``c += x`` / ``c *= x`` on a statically-typed (ClassType) target now
    dispatches ``__iadd__`` / ``__imul__`` (falling back to ``__add__``),
    instead of raising "Layer 1 cannot coerce ClassType to int". The DynType
    target path already worked; this extends it to ClassType targets."""
    src = tmp_path / "aug_probe.py"
    exe = tmp_path / "aug_probe"
    src.write_text(dedent(_AUGASSIGN_SOURCE), encoding="utf-8")
    compile_python(str(src), str(exe), libpython_mode="off",
                   ir_scaffold_mode="on", backend="self")
    proc = subprocess.run([str(exe)], text=True, capture_output=True,
                          check=True, timeout=30)
    assert [l for l in proc.stdout.splitlines() if l] == ["C(8)", "C(16)", "V3"]
