"""``isinstance(x, self.KINDS)`` must evaluate the attribute.

``class_name_from_expr`` read every ``a.b`` classinfo as a ``module.Class``
chain and took ``b`` as a class name.  A class attribute holding a class or a
tuple of classes names no class, so the check folded to False -- in the tuple
form too.  pcc1 compiled ``_membership_tuple_literal_is_constant``
(``isinstance(element, self._MEMBERSHIP_CONSTANT_LITERALS)``) that way and
built a runtime tuple for each ``x in ("a", "b")`` that the host compiler
unrolls, so the two compilers emitted different IR for the same module.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

from pcc.frontends.python.pipeline import compile_python

PROGRAM = '''
class A:
    pass


class B:
    pass


class C(A):
    pass


KINDS = (A, B)


class Holder:
    _KINDS = (A, B)
    ONE = A

    def check(self, x) -> bool:
        return isinstance(x, self._KINDS)

    def check_cls(self, x) -> bool:
        return isinstance(x, Holder._KINDS)

    def check_one(self, x) -> bool:
        return isinstance(x, self.ONE)

    def check_mod(self, x) -> bool:
        return isinstance(x, KINDS)

    def check_mixed(self, x) -> bool:
        return isinstance(x, (B, self.ONE))

    def all_const(self, items) -> bool:
        for item in items:
            if not isinstance(item, self._KINDS):
                return False
        return True


h = Holder()
for fn in (h.check, h.check_cls, h.check_one, h.check_mod, h.check_mixed):
    print(fn(A()), fn(B()), fn(C()), fn(1))
print(h.all_const([A(), B(), C()]), h.all_const([A(), 1]))
'''


@pytest.mark.parametrize("mode", ["on", "off"])
def test_attribute_classinfo_matches_cpython(tmp_path, mode):
    src = tmp_path / f"isinstance_attr_{mode}.py"
    exe = tmp_path / f"isinstance_attr_{mode}"
    src.write_text(PROGRAM, encoding="utf-8")
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


TYPES_MODULE = '''
class Type:
    pass


class NoneType(Type):
    pass


class IntType(Type):
    pass


class ListType(Type):
    pass
'''

SHADOW_PROGRAM = '''
from shadow_types import IntType, ListType, NoneType


def scalar(ty) -> bool:
    return isinstance(ty, (NoneType, IntType))


def single(ty) -> bool:
    return isinstance(ty, NoneType)


for ty in (NoneType(), IntType(), ListType(), None):
    print(type(ty).__name__, scalar(ty), single(ty))
'''


@pytest.mark.parametrize("mode", ["on", "off"])
def test_imported_class_named_like_a_builtin_type_in_a_tuple(tmp_path, mode):
    """``(NoneType, IntType)`` with pcc's own descriptor classes: the tuple
    form tested ``NoneType`` against the builtin tag table, so it matched the
    ``None`` object and not a ``NoneType()`` instance.  pcc1's ownership
    classifier asks exactly that and released the ``None`` result of every
    ``lst.append(x)`` statement the host compiler leaves alone."""
    (tmp_path / "shadow_types.py").write_text(TYPES_MODULE, encoding="utf-8")
    src = tmp_path / f"shadow_{mode}.py"
    exe = tmp_path / f"shadow_{mode}"
    src.write_text(SHADOW_PROGRAM, encoding="utf-8")
    expected = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True, timeout=60,
        cwd=str(tmp_path),
    )
    assert expected.returncode == 0, expected.stderr
    compile_python(
        str(src), str(exe),
        libpython_mode="off", ir_scaffold_mode=mode, backend="self",
    )
    for backend in ("0", "4"):
        run = subprocess.run(
            [str(exe)], capture_output=True, text=True, timeout=60,
            env={"PCC_GC_BACKEND": backend, "PATH": "/usr/bin:/bin"},
        )
        assert run.returncode == 0, run.stdout + run.stderr
        assert run.stdout == expected.stdout, (backend, run.stdout, expected.stdout)
