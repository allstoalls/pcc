"""A str subclass compares equal by its payload, as str.__eq__ does.

str.__eq__ has no method entry in a subclass, so the rich-comparison
dispatch found nothing and equality fell back to identity:
DeclinesStr("a") == DeclinesStr("a") was False natively. The dispatch now
reports whether both selected __eq__ methods declined (identity decides, as
in CPython) or none answered (the inherited str.__eq__ compares payloads).
"""
from __future__ import annotations

import os
import shutil
import subprocess


PROGRAM = """class MyStr(str):
    pass


class Shy(str):
    def __eq__(self, other):
        return NotImplemented


def main():
    print(MyStr("a") == MyStr("a"), MyStr("a") == "a", "a" == MyStr("a"), MyStr("a") != MyStr("b"))
    shy = Shy("a")
    print(shy == Shy("a"), shy == shy, Shy("a") == "a", "a" == Shy("a"), Shy("a") == MyStr("a"))
    print(MyStr("a") in ["b", "a"], ["x", MyStr("y")].count("y"), (1, MyStr("z")) == (1, "z"))


main()
"""


def test_str_subclass_equality_matches_cpython(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "str_subclass_equality.py"
    exe = tmp_path / "str_subclass_equality.out"
    src.write_text(PROGRAM)
    compile_python(str(src), str(exe), ir_scaffold_mode="on", libpython_mode="off")
    python3 = shutil.which("python3")
    assert python3 is not None, "python3 oracle not found on PATH"
    ref = subprocess.run([python3, str(src)], capture_output=True, text=True, timeout=30)
    assert ref.returncode == 0, ref.stderr
    for backend in range(5):
        native = subprocess.run(
            [str(exe)], capture_output=True, text=True, timeout=30,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
        )
        assert native.returncode == 0, f"GC{backend}: {native.stderr}"
        assert native.stdout == ref.stdout, f"GC{backend}"
