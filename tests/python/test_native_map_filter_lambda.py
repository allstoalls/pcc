"""``list(map/filter(lambda, iterable))`` with an inline lambda, no-libpython.

Only a named-function or ``None`` predicate was handled; an inline ``lambda``
fell through to a generic name lookup (`NameError: filter`). Fix extends
``_maybe_emit_list_from_map_filter`` to bind the lambda's single param to each
element and emit the lambda body inline (it closes over the current scope).
Frontend-only. (Bare ``for x in filter(...)`` is a separate unsupported path.)
"""
from __future__ import annotations

import subprocess
import sys
import textwrap


def test_map_filter_lambda_matches_cpython(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "mfl.py"
    exe = tmp_path / "mfl.out"
    src.write_text(textwrap.dedent("""
        def is_pos(n: int) -> bool:
            return n > 0

        def main() -> None:
            print(list(filter(lambda x: x > 1, [0, 1, 2, 3])))
            print(list(map(lambda x: x * 2, [1, 2, 3])))
            print(list(filter(lambda s: len(s) > 1, ["a", "bb", "c", "dd"])))
            print(list(map(lambda x: x + 100, [1, 2, 3])))
            print(list(filter(lambda n: n % 2 == 0, range(6))))
            print(list(map(lambda p: p[0], [(1, 2), (3, 4)])))
            # regressions: filter(None) / filter(named) / map(str)
            print(list(filter(None, [0, 1, 0, 2])))
            print(list(filter(is_pos, [-1, 2, -3, 4])))
            print(list(map(str, [1, 2, 3])))

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
