"""Two- and three-argument min()/max() return one of their arguments.

The multi-argument forms compared every operand through the i64 lane when
any was dynamically typed, so min(1, "a") returned 0 and min(dyn_a, dyn_b)
truncated floats; two str operands did not compile ("needs runtime
support"); mixed int/float promoted the result to float (min(1, 2.5)
printed 1.0); and the selection compared current < item instead of item <
current, so NaN and signed zeros picked the wrong operand. Only arguments
of one scalar type keep a native lane; everything else asks the runtime's
rich comparison and keeps the selected object.
"""
from __future__ import annotations

import os
import shutil
import subprocess


PROGRAM = """class Ordered:
    def __init__(self, value):
        self.value = value

    def __lt__(self, other):
        return self.value < other.value

    def __gt__(self, other):
        return self.value > other.value

    def __repr__(self):
        return "Ordered(" + str(self.value) + ")"


def main():
    floats = [2.5, 1.5, float("nan"), 0.0, -0.0]
    names = ["b", "a"]
    mixed = [1, "a"]
    print(min(floats[0], floats[1]), max(floats[0], floats[1]))
    print(min(names[0], names[1]), max(names[0], names[1]))
    print(min("b", "a", "c"), max("pear", "apple"))
    print(min(1, 2.5), max(1, 2.5), max(7, 3.0), min(2, 1.5))
    print(max(True, 0), min(0, False), max(False, 0))
    print(min(floats[2], 1.0), min(1.0, floats[2]), max(floats[2], 1.0), max(1.0, floats[2]))
    print(min(0.0, -0.0), max(-0.0, 0.0), min(floats[3], floats[4]))
    print(min(3, 1, 2), max(1.5, 2.5, 0.5), min(10 ** 30, 1), max(10 ** 30, 1))
    print(min(Ordered(2), Ordered(1)), max(Ordered(2), Ordered(5), Ordered(3)))
    try:
        print(min(mixed[0], mixed[1]))
    except TypeError as exc:
        print("TypeError", exc)
    try:
        print(max(1, "a", 2))
    except TypeError as exc:
        print("TypeError", exc)


main()
"""


def test_min_max_arguments_match_cpython(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "min_max_arguments.py"
    exe = tmp_path / "min_max_arguments.out"
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
