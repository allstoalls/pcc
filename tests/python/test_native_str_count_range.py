"""``str.count(sub, start[, end])`` with a range, no-libpython.

Only the 1-arg ``str.count(sub)`` was native; the ``start``/``end`` form forced
a libpython fallback. The range ABI counts directly over the selected UTF-8
window, avoiding both libpython and an allocated temporary slice.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap


def test_str_count_range_matches_cpython(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "cnt.py"
    exe = tmp_path / "cnt.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            print("abcabcabc".count("a"))
            print("abcabcabc".count("a", 1))
            print("abcabcabc".count("a", 1, 7))
            print("aaa".count("a", 1))
            print("banana".count("a", 2, 5))
            print("hello world".count("o", 5))
            print("xxxx".count("x", 1, 3))
            s = "mississippi"
            print(s.count("ss"))
            print(s.count("s", 3))
            print(s.count("i", 0, 4))
            print("éaéa".count("é", 1, 4))
            print("abc".count("", 3))
            print("abc".count("", 4))
            print("abc".count("", 2, 1))

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


def test_dynamic_str_count_bound_method_no_libpython(tmp_path, monkeypatch):
    """A Dyn receiver must expose ``str.count`` through ``py_obj_getattr``.

    The compiled pcc1 module-closure scanner reaches this path for source
    fragments whose precise string type is lost across helper boundaries.
    """
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "dynamic_count.py"
    exe = tmp_path / "dynamic_count.out"
    src.write_text(textwrap.dedent("""
        def count_parens(value):
            return value.count("(") - value.count(")")

        def main() -> None:
            print(count_parens("call(arg) + other("))

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    compile_python(
        str(src), str(exe),
        ir_scaffold_mode="on", libpython_mode="off", backend="self",
    )
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "1\n"
