"""``reversed(dict)`` iterates keys in reverse insertion order, no-libpython.

``_emit_reversed_builtin`` reversed by positional ``py_obj_getitem(obj, i)`` —
correct for list/tuple/range, but for a dict that is ``dict[i]`` (key lookup),
so ``reversed({...})`` produced ``<null>`` per element. Fix: for a DictType arg,
reverse the insertion-ordered keys list (``py_dict_keys``) instead. Frontend-only.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap


def test_reversed_dict_matches_cpython(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "revdict.py"
    exe = tmp_path / "revdict.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            d = {'a': 1, 'b': 2, 'c': 3}
            print(list(reversed(d)))
            for k in reversed(d):
                print(k)
            d2 = {1: 'x', 2: 'y'}
            print(list(reversed(d2)))
            # regressions: reversed(list) / reversed(range) unchanged
            print(list(reversed([10, 20, 30])))
            print(list(reversed(range(3))))

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
