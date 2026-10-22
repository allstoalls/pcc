"""``str.rsplit()`` with no args (whitespace split), no-libpython.

Both rsplit lowering sites were gated ``1 <= len(args) <= 2``, so the 0-arg
whitespace form forced a libpython fallback (COMPILE-ERR). With no maxsplit,
``rsplit()`` yields the same parts in the same order as ``split()``, so the
0-arg case routes to ``py_str_split`` with a NULL sep (whitespace path), exactly
like ``split()``. Frontend-only; ``rsplit(sep[, maxsplit])`` unchanged.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap


def test_rsplit_no_args_matches_cpython(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "rsplit0.py"
    exe = tmp_path / "rsplit0.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            print("  x  y  z  ".rsplit())
            print("a b c".rsplit())
            print("".rsplit())
            print("   ".rsplit())
            s = "p q r"
            print(s.rsplit())
            # regressions: rsplit(sep), rsplit(sep, maxsplit), split() unchanged
            print("a,b,c".rsplit(","))
            print("a,b,c".rsplit(",", 1))
            print("a b c".split())

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
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
