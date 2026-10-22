"""abs() of a float clears the sign bit, including for -0.0.

Both the typed lowering and the runtime's dyn ``py_obj_abs`` computed
``-x if x < 0.0 else x``.  ``-0.0 < 0.0`` is false, so ``abs(-0.0)`` returned
``-0.0`` (pcc1's array-core ``abs`` printed ``-0x0.0p+0``).  The typed path now
calls ``llvm.fabs.f64`` (one ``fabs``) and the runtime uses it too.
"""

import os
import subprocess
import sys


PROGRAM = '''def typed(x: float) -> float:
    return abs(x)


def dyn(x):
    return abs(x)


def main():
    z = -0.0
    vals = [-0.0, -1e-7, 0.0, -2.5, 3.0]
    print(repr(abs(z)), repr(typed(-0.0)), repr(dyn(-0.0)), repr(dyn(-7)))
    print([repr(abs(v)) for v in vals], [repr(typed(v)) for v in vals])


main()
'''

EXPECTED = (
    "0.0 0.0 0.0 7\n"
    "['0.0', '1e-07', '0.0', '2.5', '3.0'] ['0.0', '1e-07', '0.0', '2.5', '3.0']\n"
)


def test_abs_of_negative_zero_is_positive_zero(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "abs_negative_zero.py"
    source.write_text(PROGRAM, encoding="utf-8")
    reference = subprocess.run(
        [sys.executable, str(source)], capture_output=True, text=True, timeout=20,
    )
    assert reference.returncode == 0, reference.stderr
    assert reference.stdout == EXPECTED
    binary = tmp_path / "abs_negative_zero"
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        ran = subprocess.run(
            [str(binary)], capture_output=True, text=True, timeout=20,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
        )
        assert ran.returncode == 0, f"GC{backend}: {ran.stdout}; {ran.stderr}"
        assert ran.stdout == EXPECTED, f"GC{backend}"
