"""pow(base, exp, mod) runs CPython's ternary nb_power protocol.

The three-argument call lowered to py_int_pow_mod after boxing its operands
as ints and unboxing the result to an i64: a None modulus raised "pow() 3rd
argument cannot be 0", a float or complex operand computed nonsense instead
of raising, a negative exponent refused the modular inverse, a class's
__pow__/__rpow__ never saw the modulus, and a result beyond i64 overflowed.
py_obj_ternary_pow follows ternary_op: the base's slot, then the exponent's
(first when its class derives from the base's type), then the modulus's;
float_pow and complex_pow refuse a modulus, long_pow takes the modular
inverse, and the error names all three types. The program is compared with
CPython on all five collectors.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


PROGRAM = r'''
class P:
    def __repr__(self):
        return type(self).__name__ + "()"

    def __pow__(self, other, mod=None):
        return ("P.pow", other, mod)

    def __rpow__(self, other, mod=None):
        return ("P.rpow", other, mod)


class Q(P):
    def __rpow__(self, other, mod=None):
        return ("Q.rpow", other, mod)


class Declines:
    def __repr__(self):
        return "Declines()"

    def __pow__(self, other, mod=None):
        return NotImplemented

    def __rpow__(self, other, mod=None):
        return NotImplemented


class Binary:
    def __pow__(self, other):
        return "binary only"


def attempt(label, thunk):
    try:
        print(label, repr(thunk()))
    except Exception as error:
        print(label, type(error).__name__ + ":", error)


def ints(base: int, exponent: int, modulus: int) -> int:
    return pow(base, exponent, modulus)


attempt("ints", lambda: pow(2, 10, 1000))
attempt("crypto", lambda: pow(2, 10 ** 6 + 3, 10 ** 9 + 7))
attempt("inverse", lambda: pow(3, -1, 7))
attempt("inverse squared", lambda: pow(3, -2, -7))
attempt("inverse negative base", lambda: pow(-1, -1, 7))
attempt("inverse big", lambda: pow(3, -1, 10 ** 30 + 7))
attempt("inverse of big base", lambda: pow(10 ** 30, -1, 7))
attempt("not invertible", lambda: pow(2, -1, 4))
attempt("modulus one", lambda: pow(0, -1, 1))
attempt("modulus minus one", lambda: pow(4, -1, -1))
attempt("modulus zero", lambda: pow(2, 3, 0))
attempt("negative modulus", lambda: pow(2, 3, -5))
attempt("negative base", lambda: pow(-2, 3, 5))
attempt("bool operands", lambda: pow(True, -1, 5))
attempt("bool modulus", lambda: pow(2, 3, True))
attempt("typed ints", lambda: ints(7, 128, 13))
attempt("typed big", lambda: ints(3, 200, 10 ** 40 + 1))
attempt("none modulus", lambda: pow(2, 3, None))
attempt("none modulus float", lambda: pow(2, -1, None))
modulus = None
attempt("none modulus name", lambda: pow(2, 10, modulus))
attempt("float base", lambda: pow(2.0, 3, 5))
attempt("float exponent", lambda: pow(2, 3.0, 5))
attempt("float modulus", lambda: pow(2, 3, 5.0))
attempt("complex base", lambda: pow(1j, 2, 3))
attempt("complex modulus", lambda: pow(2, 3, 1j))
attempt("str modulus", lambda: pow(2, 3, "x"))
attempt("str base", lambda: pow("x", 3, 5))
attempt("none base", lambda: pow(None, 2, 5))
attempt("three strs", lambda: pow("a", "b", "c"))
attempt("user base", lambda: pow(P(), 2, 5))
attempt("user exponent", lambda: pow(2, P(), 5))
attempt("user modulus", lambda: pow(2, 3, P()))
attempt("user exponent float modulus", lambda: pow(2, P(), 5.0))
attempt("subclass reflected first", lambda: pow(P(), Q(), 5))
attempt("base subclass", lambda: pow(Q(), P(), 5))
attempt("declines base", lambda: pow(Declines(), 2, 5))
attempt("declines exponent", lambda: pow(2, Declines(), 5))
attempt("declines then float", lambda: pow(Declines(), 2.0, 5))
attempt("float before user", lambda: pow(2.0, P(), 5))
attempt("complex before user", lambda: pow(1j, P(), 5))
attempt("user before complex", lambda: pow(P(), 1j, 5))
attempt("binary signature", lambda: pow(Binary(), 2, 5))
attempt("two args user", lambda: pow(P(), 2))
attempt("two args negative", lambda: pow(2, -1) + 1)
values = [2, 3, 5]
attempt("dyn operands", lambda: pow(values[0], values[1], values[2]))
mixed = [2.0, 3, 5]
attempt("dyn float operand", lambda: pow(mixed[0], mixed[1], mixed[2]))
'''


@pytest.mark.integration
def test_ternary_pow_matches_cpython_on_five_collectors(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "ternary_pow.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "ternary_pow"
    python_program_compiler(
        str(source), str(binary), backend="self", libpython_mode="off",
        runtime_archive=str(pcc_runtime_archive),
    )
    for backend in range(5):
        environment = dict(os.environ, PCC_GC_BACKEND=str(backend))
        environment.pop("LC_ALL", None)
        ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60, env=environment)
        assert ran.returncode == 0, f"GC{backend}: {ran.stdout}{ran.stderr}"
        assert ran.stdout == expected.stdout, f"GC{backend}"
