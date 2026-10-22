"""``int(<float>)`` is exact, and raises CPython's errors, no-libpython.

The builtin lowered to a bare ``fptosi``: ``int(1e20)`` saturated to
9223372036854775807 and ``int(float("nan"))`` was 0 with no error, and the
dynamic path (``py_obj_as_int_object``) boxed the same saturated i64.  Boxed
ints now take the exact object (``py_int_from_f64_exact``); the raw i64 lane
keeps an inlined in-range ``fptosi`` and raises for NaN, infinities and
values beyond the lane instead of producing garbage.
"""

import os
import subprocess
import sys

_BOXED = '''
def main() -> None:
    values = [1e20, -2.5e19, 3.9, -3.9, 2.0 ** 63, -(2.0 ** 63), 1.7976931348623157e308, 0.5]
    for v in values:
        print(int(v))
    for v in [float("nan"), float("inf"), float("-inf")]:
        try:
            print(int(v))
        except (ValueError, OverflowError) as exc:
            print(type(exc).__name__, exc)
    x = 1e20
    y = int(x) + 1
    print(y, int(x) // 7, int(x * 3) % 1000)
    t: float = 12345.678
    n: int = int(t)
    print(n, int(t) * 2)
    dyn = [2.5e30, "12", True, 7]
    for item in dyn:
        print(int(item))
    try:
        print(int([1]))
    except TypeError as exc:
        print("TypeError", exc)


if __name__ == "__main__":
    main()
'''

_RAW = '''from pcc.unsafe import null


def lane(value: float) -> int:
    return int(value)


def main():
    print(lane(3.9), lane(-3.9), lane(9.0e18))
    for value in [float("nan"), float("inf"), 1e20]:
        try:
            print(lane(value))
        except ValueError as exc:
            print("ValueError", exc)
        except OverflowError as exc:
            print("OverflowError", exc)


main()
'''

_RAW_EXPECTED = (
    "3 -3 9000000000000000000\n"
    "ValueError cannot convert float NaN to integer\n"
    "OverflowError cannot convert float infinity to integer\n"
    "OverflowError Python int too large to convert to C int64\n"
)


def _build(tmp_path, name, text, compiler, archive):
    source = tmp_path / (name + ".py")
    source.write_text(text, encoding="utf-8")
    binary = tmp_path / name
    compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(archive),
    )
    return binary


def test_boxed_int_of_float_matches_cpython(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "boxed_ref.py"
    source.write_text(_BOXED, encoding="utf-8")
    expected = subprocess.run(
        [sys.executable, str(source)], capture_output=True, text=True, timeout=60,
    )
    assert expected.returncode == 0, expected.stderr
    binary = _build(
        tmp_path, "boxed_int_float", _BOXED, python_program_compiler,
        pcc_runtime_archive,
    )
    for backend in range(5):
        ran = subprocess.run(
            [str(binary)], capture_output=True, text=True, timeout=60,
            env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
        )
        assert ran.returncode == 0, f"GC{backend}: {ran.stderr}"
        assert ran.stdout == expected.stdout, f"GC{backend}"


def test_raw_lane_int_of_float_raises_instead_of_garbage(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    binary = _build(
        tmp_path, "raw_int_float", _RAW, python_program_compiler,
        pcc_runtime_archive,
    )
    ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60)
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout == _RAW_EXPECTED
