"""CPython parity for iteration protocols, ported from
``Lib/test/test_iter.py`` and ``Lib/test/test_listcomps.py``.

All tests use ``libpython_mode="off"``.

Reference contract (from CPython):
  * ``for x in container:`` calls ``iter(container)`` then ``next``
    until ``StopIteration``
  * ``range`` is a lazy sequence; iteration yields ints
  * ``enumerate(it)`` yields ``(idx, val)`` pairs starting at 0
  * ``zip(a, b)`` stops at the shortest iterable
  * comprehensions (list / dict / set) build the corresponding
    container in one expression
"""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path


def _compile(monkeypatch, src: Path, exe: Path) -> None:
    from pcc.frontends.python.pipeline import compile_python

    compile_python(
        str(src), str(exe),
        ir_scaffold_mode="on", libpython_mode="off",
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


def test_for_over_list(tmp_path, monkeypatch):
    src = tmp_path / "for_list.py"
    exe = tmp_path / "for_list.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            xs = [10, 20, 30]
            total = 0
            for v in xs:
                total = total + v
            print(total)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip() == "60"


def test_for_over_tuple(tmp_path, monkeypatch):
    src = tmp_path / "for_tuple.py"
    exe = tmp_path / "for_tuple.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            xs = (1, 2, 3, 4)
            total = 0
            for v in xs:
                total = total + v
            print(total)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip() == "10"


def test_for_over_dict_keys(tmp_path, monkeypatch):
    src = tmp_path / "for_dict.py"
    exe = tmp_path / "for_dict.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            d = {"a": 1, "b": 2, "c": 3}
            n = 0
            for k in d:
                n = n + 1
            print(n)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip() == "3"


def test_for_over_set(tmp_path, monkeypatch):
    src = tmp_path / "for_set.py"
    exe = tmp_path / "for_set.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            s = {1, 2, 3, 4, 5}
            total = 0
            for v in s:
                total = total + v
            print(total)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip() == "15"


def test_for_over_range(tmp_path, monkeypatch):
    src = tmp_path / "for_range.py"
    exe = tmp_path / "for_range.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            total = 0
            for i in range(10):
                total = total + i
            print(total)
            total2 = 0
            for i in range(2, 8):
                total2 = total2 + i
            print(total2)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["45", "27"]


def test_list_comprehension(tmp_path, monkeypatch):
    src = tmp_path / "lc.py"
    exe = tmp_path / "lc.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            squares = [x * x for x in range(5)]
            print(squares[0], squares[1], squares[2], squares[3], squares[4])

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip() == "0 1 4 9 16"


def test_list_comprehension_filter(tmp_path, monkeypatch):
    src = tmp_path / "lc_filter.py"
    exe = tmp_path / "lc_filter.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            evens = [x for x in range(10) if x % 2 == 0]
            print(evens[0], evens[1], evens[2], evens[3], evens[4], len(evens))

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip() == "0 2 4 6 8 5"


def test_dict_comprehension(tmp_path, monkeypatch):
    src = tmp_path / "dc.py"
    exe = tmp_path / "dc.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            d = {x: x * x for x in range(4)}
            print(d[0], d[1], d[2], d[3], len(d))

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip() == "0 1 4 9 4"


def test_set_comprehension(tmp_path, monkeypatch):
    src = tmp_path / "sc.py"
    exe = tmp_path / "sc.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            s = {x % 3 for x in range(10)}
            print(len(s))
            print(0 in s)
            print(1 in s)
            print(2 in s)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["3", "True", "True", "True"]


def test_enumerate_basic(tmp_path, monkeypatch):
    src = tmp_path / "enum.py"
    exe = tmp_path / "enum.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            xs = ["a", "b", "c"]
            for i, v in enumerate(xs):
                print(i, v)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["0 a", "1 b", "2 c"]


def test_zip_basic(tmp_path, monkeypatch):
    src = tmp_path / "zip.py"
    exe = tmp_path / "zip.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            a = [1, 2, 3]
            b = ["x", "y", "z"]
            for n, ch in zip(a, b):
                print(n, ch)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["1 x", "2 y", "3 z"]


def test_zip_shortest(tmp_path, monkeypatch):
    src = tmp_path / "zip_short.py"
    exe = tmp_path / "zip_short.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            a = [1, 2, 3, 4, 5]
            b = [10, 20]
            n = 0
            for x, y in zip(a, b):
                n = n + 1
            print(n)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip() == "2"
