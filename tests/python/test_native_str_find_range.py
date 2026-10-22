"""str ``.find``/``.rfind``/``.index``/``.rindex`` with ``start``[/``end``]
arguments, no-libpython (both tiers).

Previously only the single-argument form lowered natively; a 2- or 3-arg
call matched no branch in ``string_method_lowering.py`` and fell through to
the CPython ``py_cpy_*`` surface (``PCC-PY-COMPILE-001`` under
``--python-libpython=off``). This exercises the new ``py_str_find_range`` /
``py_str_rfind_range`` / ``py_str_index_of_range`` /
``py_str_rindex_of_range`` runtime helpers (mirrored in ``py_str_accessors.c``
and its pcc-Python port) plus both frontend lowering paths (statically-typed
``str`` receiver and the ``DynType`` bridge).

Semantics match CPython ``stringlib_find_slice``: ``start``/``end`` are
codepoint indices, wrapped/clamped by ``ADJUST_INDICES``, and the returned
index is an *absolute* codepoint offset. The unicode case guards the
byte-offset <-> codepoint-offset conversion.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

PROGRAM = textwrap.dedent("""
    def main() -> None:
        s = "abcabc"
        print(s.find("b", 2))        # 4
        print(s.find("b", 2, 4))     # -1 (window "ca")
        print(s.rfind("b", 0, 3))    # 1
        print(s.find("x", 0))        # -1
        print(s.find("bc", 4))       # 4
        print(s.find("b", -4))       # 4 (negative start wraps)
        print(s.find("b", 100))      # -1 (start past end)
        print(s.rfind("a", 1, 100))  # 3 (end clamped)
        print(s.find("", 3))         # 3 (empty substring at start)
        print(s.find("", 100))       # -1 (empty, start past end)
        print(s.rfind("", 2))        # 6 (empty rfind -> end == len)
        print(s.index("b", 2))       # 4
        print(s.rindex("b", 0, 5))   # 4
        # unicode: codepoint offsets, not byte offsets
        u = "héllo"
        print(u.find("l", 1))        # 2
        print(u.find("o", 1, 10))    # 4
        print(u.rfind("l", 0, 4))    # 3
        print(u.find("", 2))         # 2
        # ValueError path for index() when absent, caught by try/except
        try:
            print(s.index("x", 2))
        except ValueError:
            print("ValueError")
        try:
            print(s.rindex("x", 0, 3))
        except ValueError:
            print("rValueError")

    if __name__ == "__main__":
        main()
    """).lstrip()


@pytest.mark.parametrize("runtime_cc", [None], ids=["port"])
def test_str_find_range_matches_cpython(tmp_path, monkeypatch, runtime_cc):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "srange.py"
    exe = tmp_path / "srange.out"
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
