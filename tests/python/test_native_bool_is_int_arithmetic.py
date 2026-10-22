"""``bool`` is-a ``int`` in arithmetic, no-libpython (both runtime tiers).

A ``bool`` operand reaching an int op (``sum([True, False, ...])``,
``True + 1``, ``True * 3``, bignum ``10**25 + True``) used to produce ``<null>``
because ``py_bigint_from_any`` rejected any object whose ``type_tag`` was not
``PY_TYPE_INT`` — bool carries ``PY_TYPE_BOOL``. Fix coerces a bool to its int
value (True->1, False->0) at that single choke point, so bool flows through
every int op exactly like CPython's ``bool`` subclass of ``int``. Mirrored in
``py_int_core.c`` and the pcc-Python port ``py_int_core.py``.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

PROGRAM = textwrap.dedent("""
    def main() -> None:
        print(True + True)
        print(True + 1)
        print(False + 5)
        print(True * 3)
        print(True - False)
        print(5 + True)
        print(sum([True, False, True, True]))
        xs = [1, 2, 3, 4, 5]
        print(sum([v > 2 for v in xs]))
        print(sum(v % 2 == 0 for v in xs))
        big = 10 ** 25
        print(big + True)
        # int regressions: plain int ops unaffected
        print(2 + 3, 10 - 4, 6 * 7, 2 ** 10)
        print(sum([1, 2, 3, 4]))
        print(sum([10, 20], 100))

    if __name__ == "__main__":
        main()
    """).lstrip()


@pytest.mark.parametrize("runtime_cc", [None], ids=["port"])
def test_bool_is_int_arithmetic_matches_cpython(tmp_path, monkeypatch, runtime_cc):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "boolint.py"
    exe = tmp_path / "boolint.out"
    src.write_text(PROGRAM, encoding="utf-8")
    compile_python(
        str(src), str(exe),
        ir_scaffold_mode="on", libpython_mode="off", backend="self",
    )
    cpython = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True, timeout=30,
    ).stdout
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == cpython
