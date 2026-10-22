"""Object comparisons of two tagged ints are decided inline, without a runtime call.

A tagged int is the runtime's exact representation of an int in
[-2**62, 2**62 - 1]: ``(value << 1) | 1``. Two such words compare as their
values, so ``_emit_runtime_object_compare`` tests both low bits and compares the
words directly; every other operand pair (bigints, floats, bools, user
``__lt__``/``__eq__``) keeps the runtime protocol.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.python.pcc1_gate import repo_root

PROGRAM = '''class Ranked:
    def __init__(self, rank):
        self.rank = rank

    def __lt__(self, other):
        print("lt", self.rank, other)
        return self.rank < other

    def __eq__(self, other):
        return self.rank == other


def compare_all(a, b):
    return [a < b, a <= b, a > b, a >= b, a == b, a != b]


def equality(a, b):
    return [a == b, a != b]


def chained(a, b, c):
    return a < b < c


def main():
    edge = 2 ** 62
    values = [0, 1, -1, 2, -2, 7, edge - 1, -edge, edge, -edge - 1, 10 ** 30, 2.5, True, False]
    for a in values:
        for b in values:
            print(a, b, compare_all(a, b))
    print(Ranked(3) < 4, 4 > Ranked(3), Ranked(3) == 3, 3 == Ranked(3), Ranked(3) != 4)
    print(chained(1, 2, 3), chained(3, 2, 1), chained(1, 2 ** 70, 2 ** 71))
    total = 0
    for i in range(-5, 5):
        if i < 2:
            total += i
        if i == 3:
            total += 100
    print(total)
    try:
        print(compare_all(1, "x"))
    except TypeError as error:
        print("TypeError", error)
    print(equality(1, "x"), equality(3, 3.0), equality(2 ** 62, 2 ** 62))


main()
'''


def _expected() -> str:
    run = subprocess.run([sys.executable, "-c", PROGRAM], text=True, capture_output=True, timeout=60)
    assert run.returncode == 0, run.stderr
    return run.stdout


def test_dynamic_compare_tests_tags_before_the_runtime_call(tmp_path: Path) -> None:
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "cmp_shape.py"
    src.write_text("def below(n):\n    return n < 2\n", encoding="utf-8")
    out = tmp_path / "cmp_shape.ll"
    compile_python(str(src), str(out), emit_llvm_only=True, libpython_mode="off")
    text = out.read_text(encoding="utf-8")
    start = text.index("@user_cmp_shape_below(")
    body = text[start:text.index("\n}\n", start)]
    fast = body.index("int.cmp.fast")
    assert fast < body.index("call i64 (ptr, ptr) @py_obj_lt"), body


@pytest.mark.integration
@pytest.mark.parametrize("backend", ["0", "4"])
def test_tagged_int_compare_matches_cpython(tmp_path: Path, backend: str) -> None:
    src = tmp_path / "compares.py"
    src.write_text(PROGRAM, encoding="utf-8")
    exe = tmp_path / "compares"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    env["PCC_GC_BACKEND"] = backend
    build = subprocess.run(
        ["uv", "run", "pcc", "--backend", "self", "--python-libpython=off", str(src), "-o", str(exe)],
        cwd=repo_root(), text=True, capture_output=True, timeout=600, env=env,
    )
    assert build.returncode == 0, build.stderr
    run = subprocess.run([str(exe)], text=True, capture_output=True, timeout=60, env=env)
    assert run.returncode == 0, run.stderr
    assert run.stdout == _expected()
