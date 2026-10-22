"""``list(filter(None, iterable))`` keeps truthy elements, no-libpython.

``filter(None, xs)`` uses no predicate function — it keeps the truthy elements.
``_maybe_emit_list_from_map_filter`` required ``args[0]`` to be a ``Name`` (a
function), so the ``None`` (a ``NoneLit``) form fell back to libpython. Fix
special-cases ``filter(None, ...)``: keep elements whose own truthiness holds.
Frontend-only. (Bare ``for x in filter(...)`` is a separate unsupported path.)
"""
from __future__ import annotations

import subprocess
import sys
import textwrap


def test_filter_none_matches_cpython(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "filtnone.py"
    exe = tmp_path / "filtnone.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            print(list(filter(None, [0, 1, 2, 0, 3, None, 4])))
            print(list(filter(None, ["", "a", "", "b"])))
            print(list(filter(None, [])))
            print(list(filter(None, [0, 0, 0])))
            # regression: filter(func, xs) with a real predicate still works
            def is_even(n: int) -> bool:
                return n % 2 == 0
            print(list(filter(is_even, [1, 2, 3, 4, 5, 6])))

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
