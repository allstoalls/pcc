"""Containers, conditional expressions and and/or keep each operand's own value.

``common_type`` is arithmetic promotion (``1 + 2.5`` is a float). A list,
dict, tuple or set literal, ``a if c else b`` and ``a or b`` instead hold or
select one operand object, so ``[1, 2.5]`` holds an int and a float and
``1 if c else 2.5`` is the int 1. Type inference joins those with
``value_join_type``, which never converts.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.python.pcc1_gate import repo_root

PROGRAM = '''def names(values):
    return [type(v).__name__ for v in values]


def main():
    values = [0, 1, 2.5, True, False, -3]
    print(values, names(values))
    mapping = {1: 2.5, 2.5: True, "k": 3}
    print(mapping, names(mapping.keys()), names(mapping.values()))
    joined = (1, 2.5) + (True,)
    print(joined, names(joined))
    for flag in (True, False):
        chosen = 1 if flag else 2.5
        print(chosen, type(chosen).__name__)
        picked = True if flag else 7
        print(picked, type(picked).__name__)
    print(0 or 2.5, 1 or 2.5, 1 and 2.5, 0 and 2.5, True or 1, False or 0)
    print(names([0 or 2.5, 1 or 2.5, True or 1, False or 0]))
    both = [1, 2] + [2.5]
    print(both, names(both))
    merged = sorted({1} | {2.5, 3})
    print(merged, names(merged))
    print(max(1, 2.5), min(True, 1), max((1, 2.5, 0)))
    flags = [True, False, True]
    print(flags, names(flags), [1, True], names([1, True]))


main()
'''


def _expected() -> str:
    run = subprocess.run([sys.executable, "-c", PROGRAM], text=True, capture_output=True, timeout=60)
    assert run.returncode == 0, run.stderr
    return run.stdout


@pytest.mark.integration
@pytest.mark.parametrize("backend", ["0", "4"])
def test_value_joins_never_convert_operands(tmp_path: Path, backend: str) -> None:
    src = tmp_path / "joins.py"
    src.write_text(PROGRAM, encoding="utf-8")
    exe = tmp_path / "joins"
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
