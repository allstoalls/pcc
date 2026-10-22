"""Integer-overflow safety for the no-libpython Python frontend.

Source: *Low-Level Software Security for Compiler Developers* — integer
overflow is the root of many memory-corruption primitives. In Python the
correct, security-preserving behavior is that ``int`` is *arbitrary precision*:
arithmetic must never silently wrap a machine word. This is also pcc north-star
obligation 2 / 7 (the typed-int value/object projection: a value-lane overflow
must promote to a bignum, never wrap — see INT-P0-PROJ).

These compile a program through the strict no-libpython path and compare its
output, line for line, against the CPython oracle, across independent
compilations with pcc's owned ``self`` backend.
"""
from __future__ import annotations

import pytest

PROGRAM = """
def mul(a: int, b: int) -> int:
    return a * b


def main() -> int:
    print("POW100:" + str(2 ** 100))
    print("MUL:" + str(mul(2 ** 40, 2 ** 40)))
    n: int = 3037000500
    print("SQ:" + str(n * n))
    acc: int = 1
    i: int = 0
    done: bool = False
    while not done:
        acc = acc * 2
        i = i + 1
        if i >= 70:
            done = True
    print("DOUBLE70:" + str(acc))
    big: int = 9223372036854775807
    print("INC64:" + str(big + 1))
    return 0


main()
"""

# CPython oracle values (also re-derived live in the parity test below).
_EXPECTED = {
    "POW100:1267650600228229401496703205376",
    "MUL:1208925819614629174706176",          # 2**80, overflows i64
    "SQ:9223372037000250000",                  # > 2**63 - 1
    "DOUBLE70:1180591620717411303424",         # 2**70
    "INC64:9223372036854775808",               # INT64_MAX + 1
}


@pytest.fixture(scope="module")
def first_self_out(compile_and_run):
    r = compile_and_run(PROGRAM, backend="self")
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


@pytest.fixture(scope="module")
def self_out(compile_and_run):
    r = compile_and_run(PROGRAM, backend="self")
    assert r.returncode == 0, r.stdout + r.stderr
    return r.stdout


@pytest.mark.parametrize("token", sorted(_EXPECTED))
def test_int_overflow_promotes_not_wraps_first_self_run(first_self_out, token):
    assert token in first_self_out.splitlines(), f"missing/incorrect line: {token}"


@pytest.mark.parametrize("token", sorted(_EXPECTED))
def test_int_overflow_promotes_not_wraps_self_backend(self_out, token):
    assert token in self_out.splitlines(), f"missing/incorrect line: {token}"


def test_matches_cpython_oracle(first_self_out, cpython_run):
    # Reuse the already-compiled owned output; diff against CPython.
    oracle = cpython_run(PROGRAM)
    assert oracle.returncode == 0, oracle.stderr
    assert first_self_out.splitlines() == oracle.stdout.splitlines()
