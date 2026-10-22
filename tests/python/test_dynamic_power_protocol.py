"""Dynamic ``**``, ``pow`` and float ordering follow CPython's runtime protocols natively."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap


def _run_on_five_collectors(tmp_path, stem, source, expected):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / (stem + ".py")
    exe = tmp_path / (stem + ".out")
    src.write_text(source, encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        backend="self",
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    for backend in range(5):
        run_env = {**os.environ, "PCC_GC_BACKEND": str(backend)}
        result = subprocess.run(
            [str(exe)],
            capture_output=True,
            text=True,
            timeout=20,
            env=run_env,
        )
        assert result.returncode == 0, (
            f"PCC_GC_BACKEND={backend} rc={result.returncode}\n"
            + result.stdout + result.stderr
        )
        assert result.stdout == expected, f"PCC_GC_BACKEND={backend}"


def _cpython_output(source):
    return subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    ).stdout


SOURCE = textwrap.dedent(
    """
    class P:
        def __pow__(self, other):
            return "pow"

        def __rpow__(self, other):
            return "rpow"


    def square(v):
        x = v ** 2
        return x


    def power_of(v, n):
        y = pow(v, n)
        return y


    def raised(v):
        z = 2 ** v
        return z


    def less(v):
        return v < 1.0


    def greater(v):
        return 0.5 > v


    def show(label, thunk, value):
        try:
            return label + "=" + repr(thunk(value))
        except Exception as e:
            return label + "=" + type(e).__name__ + ": " + str(e)


    def square_value(v):
        return square(v)


    def power_value(v):
        return power_of(v, 2)


    def main():
        for value in (1.5, 3, 2.0, -2, 1j, True, P(), "s", None):
            print(show("sq", square_value, value), show("pow", power_value, value))
        for value in (2.0, 3, -2, True, P(), "s"):
            print(show("raised", raised, value))
        for value in (0.5, 2, True, 1j, (1, 2), "s", None):
            print(show("lt", less, value), show("gt", greater, value))


    main()
    """
).lstrip()


def test_dynamic_power_and_float_ordering_match_cpython(
    tmp_path,
    monkeypatch,
    pcc_runtime_archive,
):
    # A dynamically typed operand can hold a float, complex, bool, user or
    # unsupported object. ``**`` and ``pow`` take the runtime power protocol
    # (exact int power, float_pow, complex power, __pow__/__rpow__, then
    # CPython's TypeError) instead of unboxing the operand as an int, and an
    # ordering against a float takes rich comparison instead of reading the
    # operand as a float. Results are exactly representable, so the owned
    # libm's last-place accuracy is not what this compares.
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))
    expected = _cpython_output(SOURCE)
    assert "sq=2.25 pow=2.25" in expected
    assert "sq=(-1+0j)" in expected
    assert "unsupported operand type(s) for ** or pow(): 'str' and 'int'" in expected
    assert "lt=TypeError: '<' not supported between instances of 'complex' and 'float'" in expected
    _run_on_five_collectors(tmp_path, "dynamic_power_protocol", SOURCE, expected)
