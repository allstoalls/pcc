"""pcc's raw-int lane never turns a big int into a silent 0.

Modules that import ``pcc.unsafe`` (and pcc's own modules) keep ``int`` in a
raw i64 lane unless the exact-int planner proves a local needs the object.
Two defects made that lane produce wrong programs:

* the planner knew ``a ** b`` but not ``pow(a, b)``, so ``folded = pow(...)``
  was an i64 local and ``pow(10, 30)`` unboxed to 0 -- pcc1's own literal
  folder has exactly that shape and compiled ``10 ** 30`` as 0;
* every unbox of an int beyond i64 returned 0 (``py_int_to_i64``), so any
  other unplanned big value was silently wrong.

The planner now treats two-argument ``pow`` like ``**``, and the lane unbox
(``py_int_to_i64_lane``) raises OverflowError for an int that does not fit.
"""

import os
import subprocess
import sys


_EXACT = '''from pcc.unsafe import null


def fold(lhs: int, rhs: int) -> int:
    folded = pow(lhs, rhs)
    if -(1 << 63) <= folded <= (1 << 63) - 1:
        return folded
    return -1


def main():
    print(fold(10, 30), fold(2, 62), fold(-2, 63), fold(3, 40), fold(7, 22))


main()
'''

_TOO_BIG = '''from pcc.unsafe import null


def factorial(n: int) -> int:
    out = 1
    i = 2
    while i <= n:
        out *= i
        i += 1
    return out


def main():
    print(factorial(20))
    print(factorial(25))


main()
'''


def _compile(tmp_path, name, text, compiler, archive):
    source = tmp_path / (name + ".py")
    source.write_text(text, encoding="utf-8")
    binary = tmp_path / name
    compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(archive),
    )
    return binary


def _run(binary, backend):
    return subprocess.run(
        [str(binary)], capture_output=True, text=True, timeout=20,
        env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
    )


def test_pow_result_keeps_the_exact_int_in_the_raw_lane(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    binary = _compile(
        tmp_path, "raw_pow", _EXACT, python_program_compiler, pcc_runtime_archive,
    )
    expected = "-1 4611686018427387904 -9223372036854775808 -1 3909821048582988049\n"
    for backend in range(5):
        ran = _run(binary, backend)
        assert ran.returncode == 0, f"GC{backend}: {ran.stdout}; {ran.stderr}"
        assert ran.stdout == expected, f"GC{backend}"


def test_int_beyond_the_raw_lane_raises_instead_of_reading_zero(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    binary = _compile(
        tmp_path, "raw_too_big", _TOO_BIG, python_program_compiler,
        pcc_runtime_archive,
    )
    for backend in range(5):
        ran = _run(binary, backend)
        # The i64 return of ``factorial`` cannot hold 25!.  Either the value
        # is exact (a future object return) or the program must fail loudly;
        # it may never print another number.
        lines = ran.stdout.splitlines()
        assert lines[:1] == ["2432902008176640000"], f"GC{backend}: {ran.stdout}"
        if ran.returncode == 0:
            assert lines[1:] == ["15511210043330985984000000"], f"GC{backend}"
        else:
            assert lines[1:] == [], f"GC{backend}: {ran.stdout}"
            assert "OverflowError" in ran.stderr, f"GC{backend}: {ran.stderr}"


def test_cpython_agrees_with_the_expected_values():
    env = dict(os.environ, PYTHONPATH=os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))))
    ran = subprocess.run(
        [sys.executable, "-c", _EXACT], capture_output=True, text=True,
        timeout=20, env=env,
    )
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout == (
        "-1 4611686018427387904 -9223372036854775808 -1 3909821048582988049\n"
    )
