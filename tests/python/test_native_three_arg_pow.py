"""pow(b, e, mod) — 3-arg modular exponentiation under strict no-libpython.

3-arg pow raised "NameError: name 'pow' is not defined" (call_expression_lowering
handled only 2-arg pow). Fix: a runtime square-and-multiply helper
(py_int_pow_mod in py_int_modexp.c, an OBJ_PY_CC_HELPERS C file linked in the
no-libpython archive) that reduces modulo `mod` at every step — never
materialising b**e, so it is usable for crypto-size exponents (a boxed
(b**e)%mod would OOM). The frontend routes the int 3-arg pow to it.

Runs under ``--backend self --python-libpython=off`` in DEFAULT runtime mode.
"""
from __future__ import annotations
import os, subprocess
from pathlib import Path


def _run(tmp_path, source):
    src = tmp_path / "p.py"; src.write_text(source, encoding="utf-8")
    exe = tmp_path / "p_bin"; env = os.environ.copy(); env.pop("LC_ALL", None)
    b = subprocess.run(["uv","run","pcc","--backend","self","--python-libpython=off","--ir-scaffold=on",str(src),"-o",str(exe)], text=True, capture_output=True, timeout=420, env=env)
    assert b.returncode == 0, b.stderr
    r = subprocess.run([str(exe)], text=True, capture_output=True, timeout=30, env=env)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_three_arg_pow_modexp(tmp_path):
    out = _run(tmp_path,
        "def main():\n"
        "    print(pow(2, 10, 1000))\n"        # 24
        "    print(pow(5, 3, 13))\n"           # 8
        "    print(pow(7, 0, 5))\n"            # 1
        "    print(pow(10, 1, 7))\n"           # 3
        "    print(pow(2, 100, 1000000007))\n" # 976371285 (large exp, no blow-up)
        "    print(pow(3, 1000, 97))\n"        # 3**1000 mod 97
        "    print(pow(2, 10))\n"              # 1024 (2-arg regression)
        "main()\n")
    assert out.split("\n")[:7] == [
        "24", "8", "1", "3",
        str(pow(2, 100, 1000000007)),
        str(pow(3, 1000, 97)),
        "1024",
    ], out


def test_three_arg_pow_mod_zero_raises(tmp_path):
    """CPython raises ``ValueError: pow() 3rd argument cannot be 0`` for a zero
    modulus (NOT ZeroDivisionError). The runtime helper previously returned a
    bare NULL for mod == 0 with no exception set — the frontend
    py_err_occurred() check (there is no stack unwinding) never fired, so the
    program continued with a NULL result. The helper now raises a matching-typed
    ValueError so a defensive ``try/except ValueError`` works and the caught
    message matches CPython exactly."""
    out = _run(tmp_path,
        "def safe(b, e, m):\n"
        "    try:\n"
        "        return pow(b, e, m)\n"
        "    except ValueError as ex:\n"
        "        return 'VE:' + str(ex)\n"
        "def main():\n"
        "    print(safe(2, 10, 1000))\n"   # 24 (normal case still works)
        "    print(safe(2, 10, 0))\n"      # VE:pow() 3rd argument cannot be 0
        "    print(safe(5, 3, 0))\n"       # same VE message
        "    print(safe(5, 3, 13))\n"      # 8 (recovery after the error path)
        "main()\n")
    assert out.split("\n")[:4] == [
        "24",
        "VE:pow() 3rd argument cannot be 0",
        "VE:pow() 3rd argument cannot be 0",
        "8",
    ], out


def test_three_arg_pow_negative_exponent_uses_the_inverse(tmp_path):
    """A negative exponent raises the base's modular inverse to the negated
    exponent, as CPython 3.8+ does (``pow(2, -1, 7) == 4``); a base sharing a
    factor with the modulus has no inverse and raises ValueError."""
    out = _run(tmp_path,
        "def safe(b, e, m):\n"
        "    try:\n"
        "        return pow(b, e, m)\n"
        "    except ValueError as ex:\n"
        "        return 'VE:' + str(ex)\n"
        "def main():\n"
        "    print(safe(2, -1, 7))\n"      # 4
        "    print(safe(3, -2, 7))\n"      # 4 (inverse 5, squared 25 % 7)
        "    print(safe(2, -1, 4))\n"      # no inverse
        "    print(safe(2, 10, 1000))\n"   # 24 (positive exp unaffected)
        "main()\n")
    assert out.split("\n")[:4] == [
        "4", "4", "VE:base is not invertible for the given modulus", "24",
    ], out


def test_two_arg_pow_float_result(tmp_path):
    """``pow(2, 0.5)`` (a float result with an int base) previously emitted
    invalid IR ('ptr' vs 'double'): the float-result path passed the raw boxed
    int operand to _emit_binop_float instead of coercing to double. Mirrors the
    ``**`` operator path. (Dynamic-typed ``pow(b, e)`` with a fractional
    exponent — needs a runtime py_obj_pow — remains a separate gap.)"""
    out = _run(tmp_path,
        "def main():\n"
        "    print(pow(2, 0.5))\n"        # 1.4142135623730951
        "    print(pow(2.0, 0.5))\n"      # 1.4142135623730951
        "    print(pow(2.5, 2))\n"        # 6.25
        "    print(pow(9, 0.5))\n"        # 3.0
        "    print(pow(2, 10))\n"         # 1024  (int path unaffected)
        "    print(2 ** 0.5)\n"           # 1.4142135623730951  (operator)
        "main()\n")
    assert out.split("\n")[:6] == [
        "1.4142135623730951", "1.4142135623730951", "6.25",
        "3.0", "1024", "1.4142135623730951",
    ], out
