"""A local assigned from two-argument ``pow`` keeps the exact int.

``pow(a, b)`` lowers to the py_int_pow object, like ``a ** b``, but the
exact-int planner only knew the operator.  A local such as
``folded = pow(lhs, rhs)`` was planned into the i64 lane, where an object above
2**63-1 unboxes to 0.  pcc's own ``**`` literal folder has exactly that shape,
so pcc1 compiled ``10 ** 30`` as the constant 0 while host pcc called
py_int_pow (host and pcc1 IR differed on it).
"""

import os
import subprocess
import sys


PROGRAM = '''def fold(lhs: int, rhs: int) -> int:
    folded = pow(lhs, rhs)
    if -(1 << 63) <= folded <= (1 << 63) - 1:
        return folded
    return -1


def main():
    print(10 ** 30, 2 ** 64 + 1, -(2 ** 63), 3 ** 41)
    print([10 ** 30, 2 ** 63, 7 ** 23])
    print(fold(10, 30), fold(2, 62), fold(-2, 63), fold(3, 40))
    base = 10
    print(pow(base, 30), pow(base, 18), pow(2, base * 7))


main()
'''


def test_pow_result_local_keeps_the_exact_int(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "int_pow_exact_local.py"
    source.write_text(PROGRAM, encoding="utf-8")
    reference = subprocess.run(
        [sys.executable, str(source)], capture_output=True, text=True, timeout=20,
    )
    assert reference.returncode == 0, reference.stderr
    binary = tmp_path / "int_pow_exact_local"
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
        assert ran.stdout == reference.stdout, f"GC{backend}"
