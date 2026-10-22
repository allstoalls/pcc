"""``float(<str>)`` parses the string (incl. scientific), no-libpython (both tiers).

`float(runtime_str)` routed through `py_float_to_f64`, which has no str case and
returned 0.0 (even for "3.14"). Fix adds a str-aware C-only helper
`py_float_value_of` (strtod-based, raises ValueError on a bad/partial string,
else delegates to py_float_to_f64) and routes `float(StrType)` / `float(DynType)`
through it (`call_expression_lowering.py`). C-only -> linked in both tiers.
(StrLit `float("1e100")` still uses the compile-time fold, last-ULP imprecise —
separate minor follow-up.)
"""
from __future__ import annotations

import subprocess
import sys
import textwrap


PROGRAM = textwrap.dedent("""
    def ident(s):
        return s

    def main() -> None:
        print(float(ident("1e100")))
        print(float(ident("6.022e23")))
        print(float(ident("3.14")))
        print(float(ident("-2.5")))
        print(float(ident("  42  ")))
        print(float(ident("1e-100")))
        print(float(ident("inf")))
        print(float(5))
        print(float(2.5))
        try:
            float(ident("abc"))
        except ValueError as e:
            print("VE:", type(e).__name__)

    if __name__ == "__main__":
        main()
    """).lstrip()


def test_float_of_str_matches_cpython(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "fs.py"
    exe = tmp_path / "fs.out"
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
