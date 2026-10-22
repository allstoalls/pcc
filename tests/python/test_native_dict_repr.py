"""Native ``print(dict)`` repr in no-libpython mode.

Before this regression, ``print(d)`` for a dict fell through ``py_format`` to
the generic ``<object tag=6>`` fallback (dict was missing from the type
switch), while list/tuple already formatted correctly.  Dict repr uses
``repr()`` for keys and values and preserves insertion order, skipping slots
emptied by ``del``.  This is on the package-import path (config/dist-info code
prints dict-shaped data).

All tests use ``libpython_mode="off"`` and the self backend, then run the
produced binary and compare stdout to the CPython reference output.
"""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path


def _compile(monkeypatch, src: Path, exe: Path) -> None:
    from pcc.frontends.python.pipeline import compile_python

    compile_python(
        str(src), str(exe),
        ir_scaffold_mode="on", libpython_mode="off", backend="self",
    )


def _run(exe: Path, timeout: float = 30.0) -> str:
    result = subprocess.run(
        [str(exe)], capture_output=True, text=True, timeout=timeout,
    )
    assert result.returncode == 0, (
        f"{exe.name} exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result.stdout


def test_print_dict_repr_matches_cpython(tmp_path, monkeypatch):
    src = tmp_path / "dict_repr.py"
    exe = tmp_path / "dict_repr.out"
    program = textwrap.dedent("""
        def main() -> None:
            print({1: 2, 3: 4})
            print({k: k * 2 for k in [1, 2, 3]})
            print({})
            print({"a": [1, 2], "b": (3, 4)})
            print({1: {"x": 9}})
            print([{"n": 1}, {"n": 2}])
            d = {"a": 1, "b": 2, "c": 3}
            del d["b"]
            print(d)

        if __name__ == "__main__":
            main()
        """).lstrip()
    src.write_text(program, encoding="utf-8")
    _compile(monkeypatch, src, exe)
    # Reference output is exactly CPython's repr for these values.
    assert _run(exe).splitlines() == [
        "{1: 2, 3: 4}",
        "{1: 2, 2: 4, 3: 6}",
        "{}",
        "{'a': [1, 2], 'b': (3, 4)}",
        "{1: {'x': 9}}",
        "[{'n': 1}, {'n': 2}]",
        "{'a': 1, 'c': 3}",
    ]


def test_str_repr_of_containers_and_float(tmp_path, monkeypatch):
    # ``str()`` / f-string of float and containers used to return ``<null>``
    # in no-libpython mode (py_obj_str/py_obj_repr ports had no float/container
    # support).  Now routed through the same formatter; element repr is used
    # for container members (quoted strings, nested float/bool/None).
    src = tmp_path / "str_containers.py"
    exe = tmp_path / "str_containers.out"
    program = textwrap.dedent("""
        def main() -> None:
            print(str([1, 2]))
            print(str((1, 2)))
            print(str(3.5))
            print(str(3.0))
            print(str(-2.25))
            print(str(1e308 * 10))
            print(str(-1e308 * 10))
            print(str({1: 2}))
            print("v=" + str([1.5, True, None, "x"]))
            print(repr({"f": 1.5, "b": False}))
            print(str((7,)))

        if __name__ == "__main__":
            main()
        """).lstrip()
    src.write_text(program, encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).splitlines() == [
        "[1, 2]",
        "(1, 2)",
        "3.5",
        "3.0",
        "-2.25",
        "inf",
        "-inf",
        "{1: 2}",
        "v=[1.5, True, None, 'x']",
        "{'f': 1.5, 'b': False}",
        "(7,)",
    ]


def test_bytes_str_repr_matches_cpython(tmp_path, monkeypatch):
    # ``str(b'..')`` / ``repr(b'..')`` used to return ``<null>`` (the
    # py_obj_str/py_obj_repr ports had no bytes case); now routed to the bytes
    # formatter.  This also fixes bytes elements rendered via the container str
    # builders.  Quote selection is pcc's existing single-quote rule, so these
    # cases avoid embedded ``'`` (CPython's ``'`` -> ``"`` switch is a separate
    # pre-existing pcc-wide divergence shared with print(bytes)).
    src = tmp_path / "bytes_repr.py"
    exe = tmp_path / "bytes_repr.out"
    program = textwrap.dedent("""
        def main() -> None:
            print(str(b"hi"))
            print(repr(b"a\\tb\\n"))
            print(b"raw")
            print(str([b"x", b"y"]))
            print({"k": b"v"})
            print(str(b"\\x00\\x7f"))

        if __name__ == "__main__":
            main()
        """).lstrip()
    src.write_text(program, encoding="utf-8")
    _compile(monkeypatch, src, exe)
    import sys
    cpython = subprocess.run(
        [sys.executable, str(src)], capture_output=True, text=True, timeout=30,
    ).stdout
    assert _run(exe) == cpython


def test_set_repr_matches_cpython(tmp_path, monkeypatch):
    # Empty set is ``set()``, not ``{}``; non-empty is ``{...}``.  Set element
    # order is hash-defined, so only assert deterministic shapes (empty,
    # single, and a sorted view) plus the small-int case whose order coincides
    # with CPython.
    src = tmp_path / "set_repr.py"
    exe = tmp_path / "set_repr.out"
    program = textwrap.dedent("""
        def main() -> None:
            print(set())
            print({42})
            print(str({7}))
            print({1, 2, 3})
            print(sorted({3, 1, 2}))
            print(str({"a"}))

        if __name__ == "__main__":
            main()
        """).lstrip()
    src.write_text(program, encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).splitlines() == [
        "set()",
        "{42}",
        "{7}",
        "{1, 2, 3}",
        "[1, 2, 3]",
        "{'a'}",
    ]
