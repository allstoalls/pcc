"""User ``//`` dispatch matches CPython on all five collectors.

Forward ``__floordiv__``, reflected ``__rfloordiv__`` after the left side
returns NotImplemented, a subclass right operand's reflected method winning
over the base's forward method, and TypeError when both sides decline. Each
result is an object the caller owns: the program keeps, compares and drops
them while allocations churn.
"""
import os
from pathlib import Path
import subprocess
import sys

PROGRAM = '''class Box:
    def __init__(self, value):
        self.value = value

    def __floordiv__(self, other):
        if isinstance(other, Box):
            return Box(self.value // other.value)
        if isinstance(other, int):
            return Box(self.value // other)
        return NotImplemented

    def __rfloordiv__(self, other):
        if isinstance(other, int):
            return Box(other // self.value)
        return NotImplemented

    def __repr__(self):
        return type(self).__name__ + "(" + str(self.value) + ")"


class SubBox(Box):
    def __rfloordiv__(self, other):
        if isinstance(other, Box):
            return SubBox(-(other.value // self.value))
        return Box.__rfloordiv__(self, other)


class Opaque:
    def __floordiv__(self, other):
        return NotImplemented


def churn(n):
    out = []
    i = 0
    while i < n:
        out.append([i, str(i)])
        i += 1
    return len(out)


def main():
    kept = []
    for n in (7, 20, -9):
        kept.append(Box(n) // Box(3))
        kept.append(Box(n) // 2)
        kept.append(100 // Box(n))
        kept.append(Box(n) // SubBox(4))
        churn(200)
    print(kept)
    for left, right in ((Opaque(), Box(1)), (Box(1), "s"), ("s", Box(1))):
        try:
            left // right
        except TypeError as exc:
            print("TypeError", type(left).__name__, type(right).__name__, "//" in str(exc))
    churn(500)
    print([item.value for item in kept])


main()
'''


def test_user_floordiv_dispatch_matches_cpython_on_five_collectors(tmp_path: Path, pcc_runtime_archive):
    from pcc.frontends.python.pipeline import compile_python

    source = tmp_path / "floordiv.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "floordiv"
    compile_python(str(source), str(binary), backend="self", libpython_mode="off",
                   ir_scaffold_mode="on", runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        ran = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
                             capture_output=True, text=True, timeout=60)
        assert ran.returncode == 0, f"GC{backend}: " + ran.stdout + ran.stderr[-2000:]
        assert ran.stdout == expected.stdout, f"GC{backend}: {ran.stdout}"
