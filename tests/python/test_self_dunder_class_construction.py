"""`self.__class__(...)` must build the receiver's runtime class.

The constructor shortcut folded `self.__class__(...)` to the class the
lexical scope resolved, but a method body is shared with every subclass that
inherits it.  `Base.clone` returning `self.__class__()` therefore built a
`Base` even when the receiver was a `Sub` -- the classic "copy returns the
wrong type" defect, silent and type-visible only downstream.

Reading the attribute was always right (`Sub().__class__` is `Sub`); only
the call shortcut folded.  The fix reads the class off the instance at
runtime, which stays libpython-free: falling through to a generic CPython
call would make the whole enclosing function a fail-closed stub under
`--python-libpython=off`.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]

_PROGRAM = textwrap.dedent(
    '''\
    class Base:
        def __init__(self):
            self.marker = "base-init"

        def clone(self):
            return self.__class__()

        def label(self):
            return "Base"


    class Sub(Base):
        def label(self):
            return "Sub"


    class Leaf(Sub):
        def label(self):
            return "Leaf"


    class Solo:
        def clone(self):
            return self.__class__()


    def make_base():
        return Base()


    def make_sub():
        return Sub()


    def main() -> int:
        b = make_base()
        s = make_sub()
        l = Leaf()
        print("sub  :", type(s.clone()).__name__, s.clone().label())
        print("base :", type(b.clone()).__name__, b.clone().label())
        print("leaf :", type(l.clone()).__name__, l.clone().label())
        # A class with no subclass keeps the folded construction; it must
        # still run __init__.
        print("solo :", type(Solo().clone()).__name__)
        print("init :", b.clone().marker)
        return 0


    main()
    '''
)

_EXPECTED = (
    "sub  : Sub Sub\n"
    "base : Base Base\n"
    "leaf : Leaf Leaf\n"
    "solo : Solo\n"
    "init : base-init\n"
)


@pytest.mark.pcc_gate(probe="self_backend")
def test_dunder_class_construction_uses_the_runtime_class(tmp_path):
    source = tmp_path / "dunderclass.py"
    source.write_text(_PROGRAM, encoding="utf-8")
    binary = tmp_path / "dunderclass"

    build = subprocess.run(
        [
            sys.executable, "-m", "pcc",
            "--ir-scaffold=on", "--backend", "self",
            "--python-libpython", "off",
            str(source), "-o", str(binary),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=1800,
    )
    assert build.returncode == 0, build.stdout + build.stderr

    run = subprocess.run(
        [str(binary)], capture_output=True, text=True, timeout=120,
    )
    # `no-libpython function unavailable: ...clone` means the runtime
    # dispatch fell through to a CPython call instead of being lowered.
    assert run.returncode == 0, run.stderr
    assert run.stdout == _EXPECTED, (
        f"`sub  : Base` means the fold survived\ngot {run.stdout!r}"
    )


def test_cpython_reference_behaviour(tmp_path):
    """The same program under CPython, so the contract is not self-defined."""
    source = tmp_path / "dunderclass.py"
    source.write_text(_PROGRAM, encoding="utf-8")
    run = subprocess.run(
        [sys.executable, str(source)], capture_output=True, text=True, timeout=120,
    )
    assert run.returncode == 0, run.stderr
    assert run.stdout == _EXPECTED
