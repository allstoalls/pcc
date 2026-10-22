"""Arithmetic at object boundaries and binary TypeErrors follow CPython.

Static float and complex arithmetic in an expression whose consumer needs an
object (a lambda body, a call argument) failed to compile: complex -, *, **
and float ** kept typed kernels that could not publish their result, while
the runtime protocols they now use already took those operands. Float floor
division computed floor(a / b), so 1.0 // 0.1 was 10.0; it now shares
CPython's _float_div_mod with divmod. Every unsupported binary operation
names both operand types (CPython's binop_type_error), and a float modulo by
zero reads "division by zero" (3.14+). Compared with CPython on all five
collectors.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


PROGRAM = r'''
def attempt(label, thunk):
    try:
        print(label, repr(thunk()))
    except Exception as error:
        print(label, type(error).__name__ + ":", error)


class Plain:
    pass


class Left:
    def __floordiv__(self, other):
        return NotImplemented


class Right:
    def __rfloordiv__(self, other):
        return ("rfloordiv", type(other).__name__)


f = 2.5
c = 1 + 2j
attempt("complex add", lambda: 1j + 2)
attempt("complex sub", lambda: 1j - 2)
attempt("complex mul", lambda: 1j * 2)
attempt("complex div", lambda: 1j / 2)
attempt("complex pow", lambda: 1j ** 2)
attempt("complex floordiv", lambda: 1j // 2)
attempt("complex mod", lambda: 1j % 2)
attempt("float pow", lambda: 2.0 ** 0.5)
attempt("negative float root", lambda: (-8.0) ** 0.5)
attempt("zero negative power", lambda: 0.0 ** -1)
attempt("float floordiv near multiple", lambda: 1.0 // 0.1)
attempt("negative float floordiv", lambda: -1.0 // 0.1)
attempt("int over float floordiv", lambda: 1 // 0.1)
attempt("signed zero floordiv", lambda: 0.0 // -1.0)
attempt("float floordiv zero", lambda: 5.0 // -0.0)
attempt("inf floordiv", lambda: float("inf") // 1.0)
attempt("by inf floordiv", lambda: -1.0 // float("inf"))
attempt("bool over float", lambda: True // 0.5)
attempt("float mod zero", lambda: 1.0 % 0.0)
attempt("divmod near multiple", lambda: divmod(1.0, 0.1))
attempt("name pow", lambda: f ** 2)
attempt("complex name pow", lambda: c ** 2)
attempt("pow float call", lambda: pow(2.0, 3))
attempt("pow complex call", lambda: pow(1j, 2))
attempt("user floordiv reflected", lambda: Left() // Right())
attempt("str sub", lambda: "a" - 1)
attempt("list mul float", lambda: [1] * 1.5)
attempt("dict or int", lambda: {} | 1)
attempt("int lshift str", lambda: 1 << "x")
attempt("none add", lambda: None + 1)
attempt("plain add", lambda: Plain() + 1)
attempt("int add plain", lambda: 1 + Plain())
attempt("plain pow", lambda: Plain() ** 2)
attempt("int pow str", lambda: 1 ** "x")
attempt("matmul", lambda: 1 @ 2)
attempt("divmod str", lambda: divmod("a", 1))
attempt("str floordiv", lambda: "text" // 2)
attempt("plain floordiv", lambda: Plain() // Plain())
attempt("plain and", lambda: Plain() & 1)
attempt("plain mod", lambda: Plain() % 2)
attempt("plain truediv", lambda: Plain() / 2)
attempt("plain rshift", lambda: Plain() >> 1)
values = [7, 0.0, "s", None]
attempt("dyn floordiv zero", lambda: values[0] // values[1])
attempt("dyn sub", lambda: values[2] - values[0])
attempt("dyn mul none", lambda: values[3] * values[0])


class BadIndex:
    def __index__(self):
        return 1.5


class BadFloat:
    def __float__(self):
        return 1


class GoodIndex:
    def __index__(self):
        return 2


attempt("str mul float", lambda: "ab" * 2.0)
attempt("list mul index", lambda: [1] * GoodIndex())
attempt("list mul plain", lambda: [1] * Plain())
attempt("bin float", lambda: bin(1.5))
attempt("bad index slice", lambda: [1, 2][BadIndex():])
attempt("bad index range", lambda: range(10)[BadIndex()])
attempt("bad index bin", lambda: bin(BadIndex()))
attempt("bad float", lambda: float(BadFloat()))


def typed(x: float, y: float):
    return x ** y, x // y, x % y


print(typed(7.5, 2.0))
print(typed(-7.5, 2.0))
'''


@pytest.mark.integration
def test_binary_arithmetic_matches_cpython_on_five_collectors(
    tmp_path, pcc_runtime_archive, python_program_compiler,
):
    source = tmp_path / "binary_arithmetic.py"
    source.write_text(PROGRAM, encoding="utf-8")
    expected = subprocess.run([sys.executable, str(source)], capture_output=True, text=True, timeout=60)
    assert expected.returncode == 0, expected.stderr
    binary = tmp_path / "binary_arithmetic"
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
