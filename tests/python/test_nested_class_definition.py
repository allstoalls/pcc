"""A class defined in a class body is an attribute of the outer class.

Class lowering declared only module-level classes and skipped a nested
``class`` statement, so ``Outer.Inner`` raised AttributeError and a method
call on ``Outer.Inner()`` stopped codegen. Nested classes are now declared
at module level ahead of their outer class and bound as its attribute, with
CPython's ``__name__``/``__qualname__``, also when a module-level class or an
imported name already uses the nested name.
"""

from __future__ import annotations

import os
import subprocess
import sys

_PROGRAM = '''
from collections import OrderedDict as Deep


class Outer:
    tag = 1

    class Inner:
        value = 7

        def get(self):
            return self.value

        class Deep:
            label = "deep"

    kind = Inner

    class Error(Exception):
        pass

    def make(self):
        return Outer.Inner()


class Inner:
    value = "module level"


def main() -> None:
    print(Outer.tag, Outer.Inner.value, Outer.Inner().get(), Outer().make().get())
    print(Outer.Inner.__name__, Outer.Inner.__qualname__, Outer.Inner.Deep.label, Outer.Inner.Deep.__qualname__)
    print(Inner.value, Outer.Inner is not Inner, Outer.kind is Outer.Inner, Deep.__name__)
    try:
        raise Outer.Error("nested error")
    except Outer.Error as exc:
        print(type(exc).__name__, type(exc).__qualname__, exc, repr(exc))


main()
'''


def test_nested_classes_match_cpython_under_five_collectors(tmp_path, pcc_runtime_archive,
                                                            python_program_compiler):
    source = tmp_path / "nested_class.py"
    source.write_text(_PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    output = tmp_path / "nested_class"
    python_program_compiler(
        str(source), str(output), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for gc in range(5):
        ran = subprocess.run(
            [str(output)], capture_output=True, text=True, timeout=60,
            env=dict(os.environ, PCC_GC_BACKEND=str(gc)),
        )
        assert ran.returncode == 0, f"GC{gc}: {ran.stdout}{ran.stderr}"
        assert ran.stdout == expected.stdout, f"GC{gc}: {ran.stdout}"
