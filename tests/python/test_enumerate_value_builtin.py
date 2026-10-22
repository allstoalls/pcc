"""Value-position ``enumerate`` under strict no-libpython (2026-06-12).

The for-loop form has long been desugared natively, but ``enumerate``
used as a VALUE (``list(enumerate(xs))``, args to calls, ...) compiled
to a dynamic global load and died with ``NameError: name 'enumerate'
is not defined`` at runtime. It now lowers to the C-only
``py_enumerate_list`` helper (eager (index, item) tuple list — the
subset's established eager convention), covering the 1-arg, 2-arg and
``start=`` keyword shapes, raising TypeError for non-iterables and
consuming the iterator protocol's StopIteration cleanly.

Expected output is the CPython oracle for the same program.
"""
from __future__ import annotations

import os
import subprocess
import textwrap

_PROGRAM = textwrap.dedent(
    """
    def main() -> int:
        xs = ["x", "y"]
        print(list(enumerate(xs)))
        print(list(enumerate(xs, 1)))
        print(list(enumerate(xs, start=5)))
        print(list(enumerate("ab")))
        print(list(enumerate((10, 20), 3)))
        try:
            enumerate(42)
            print("no-raise")
        except TypeError:
            print("typeerror-ok")
        return 0


    main()
    """
)

_EXPECTED = [
    "[(0, 'x'), (1, 'y')]",
    "[(1, 'x'), (2, 'y')]",
    "[(5, 'x'), (6, 'y')]",
    "[(0, 'a'), (1, 'b')]",
    "[(3, 10), (4, 20)]",
    "typeerror-ok",
]


def test_enumerate_value_forms_match_cpython(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "enum_value_prog.py"
    src.write_text(_PROGRAM, encoding="utf-8")
    exe = tmp_path / "enum_value_prog"
    compile_python(
        str(src),
        str(exe),
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
    )
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == _EXPECTED


def test_enumerate_value_custom_iterator_reenters_gc4(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "enum_value_gc4.py"
    src.write_text(
        textwrap.dedent(
            """
            import gc

            class Values:
                def __init__(self, values):
                    self.values = values
                    self.index = 0

                def __iter__(self):
                    return self

                def __next__(self):
                    if self.index >= len(self.values):
                        raise StopIteration
                    gc.collect()
                    value = self.values[self.index]
                    self.index += 1
                    return value

            def main() -> int:
                print(list(enumerate(Values([7, 8, 9]), 5)))
                return 0

            main()
            """
        ),
        encoding="utf-8",
    )
    exe = tmp_path / "enum_value_gc4"
    compile_python(
        str(src),
        str(exe),
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
    )
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_GC_BACKEND"] = "4"
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=60, env=env
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "[(5, 7), (6, 8), (7, 9)]"
