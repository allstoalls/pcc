"""int()/float() text, complex promotion and isinstance(complex), natively.

Found under the owned ast.literal_eval, which leans on all of them:
int("1_000") and float("1_0.5") raised ValueError (PEP 515 underscores were
unknown to the str path), int("010", 0) answered 8, int - complex raised
TypeError when both operands were dynamic, any value joined with a complex
became complex at compile time (so [1j, "x"][0] - [1j, "x"][1] computed 3j
instead of raising), and isinstance(1j, complex) was False. Native output
under every collector must match CPython.
"""
from __future__ import annotations

import os
import subprocess
import sys

_SOURCE = """
def parse_int(text, base):
    try:
        if base == -1:
            return repr(int(text))
        return repr(int(text, base))
    except ValueError:
        return "ValueError"


def parse_float(text):
    try:
        return repr(float(text))
    except ValueError:
        return "ValueError"


def main():
    for text in ["1_000", "1__0", "_1", "1_", " 1_2 ", "\\x0b7\\x0c", "12"]:
        print("int", repr(text), parse_int(text, -1))
    for text in ["0x_ff", "0xff", "010", "00", "0_0", "-0b_101", "0o_17", "0"]:
        print("int0", repr(text), parse_int(text, 0))
    print("int16", parse_int("f_f", 16), parse_int("_f", 16))
    for text in ["1_000.5", "1_0e1_0", "1._5", "1_.5", "_1.0", "1.0_", "inf", " 2.5 "]:
        print("float", repr(text), parse_float(text))

    number = int("2")
    imag = complex(0.0, 3.0)
    r1 = number - imag
    r2 = number * imag
    r3 = imag / number
    r4 = 2.5 - imag
    r5 = imag - number
    print("ops", repr(r1), repr(r2), repr(r3), repr(r4), repr(r5))
    mixed = [imag, "x"]
    try:
        bad = mixed[0] - mixed[1]
        print("no error", repr(bad))
    except TypeError:
        print("complex - str TypeError")
    values = [imag, 1, 1.5, b"b", bytearray(b"a"), memoryview(b"m"), {1}, None, "s"]
    for value in values:
        print(type(value).__name__, isinstance(value, complex), isinstance(value, memoryview),
              isinstance(value, (complex, set)), isinstance(value, bytes))


main()
"""


def test_numeric_text_and_complex_match_cpython(tmp_path):
    source = tmp_path / "numbers.py"
    source.write_text(_SOURCE, encoding="utf-8")
    exe = tmp_path / "numbers_bin"
    env = os.environ.copy()
    built = subprocess.run(
        ["uv", "run", "pcc", "--backend", "self", "--python-libpython=off",
         "--ir-scaffold=on", str(source), "-o", str(exe)],
        text=True, capture_output=True, timeout=600, env=env,
    )
    assert built.returncode == 0, built.stderr
    expected = subprocess.run([sys.executable, str(source)], text=True, capture_output=True,
                              timeout=60, env=env, cwd=tmp_path)
    assert expected.returncode == 0, expected.stderr
    for backend in range(5):
        native = subprocess.run([str(exe)], text=True, capture_output=True, timeout=60,
                                env=dict(env, PCC_GC_BACKEND=str(backend)))
        assert native.returncode == 0, (backend, native.stderr)
        assert native.stdout == expected.stdout, backend
