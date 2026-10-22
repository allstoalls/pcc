"""A float-typed call through an object binding yields a boxed float.

Since calls through module-level from-imports became live namespace lookups,
``sqrt(9)`` after ``from math import sqrt`` is an object call whose result is
a boxed float even though its static type is ``float``. ``int(...)`` passed
that pointer to ``py_int_from_f64_exact`` and unary minus emitted ``fneg`` on
it, so both programs failed the self IR verifier.
"""

from __future__ import annotations

import os
import subprocess
import sys


_PROGRAM = '''
import math as m
from math import sqrt


def main() -> None:
    print(int(sqrt(9)))
    print(-sqrt(9))
    print(int(-m.sqrt(16)))
    print(int(m.floor(3.9)))
    root = sqrt(2.25)
    print(int(root * 4), -root)
    print(sqrt(9) + 1.5, -(sqrt(4) * 2))


if __name__ == "__main__":
    main()
'''


def test_float_typed_object_call_results_match_cpython(tmp_path, pcc_runtime_archive, python_program_compiler):
    source = tmp_path / "float_object_calls.py"
    source.write_text(_PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    output = tmp_path / "float_object_calls"
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
