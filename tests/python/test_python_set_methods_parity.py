"""CPython parity for ``set`` methods, ported from
``Lib/test/test_set.py``.

All tests use ``libpython_mode="off"``.

Reference contract (from CPython):
  * ``set`` is unordered, mutable, no duplicate members
  * ``add`` / ``remove`` / ``discard`` / ``pop`` / ``update`` are
    canonical mutation surface
  * ``|`` ``&`` ``-`` ``^`` and the named-method forms
    ``union`` / ``intersection`` / ``difference`` /
    ``symmetric_difference`` agree
  * ``in`` is membership; iteration yields each element once
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


def test_set_add_remove(tmp_path, monkeypatch):
    src = tmp_path / "set_add.py"
    exe = tmp_path / "set_add.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            s: set = set()
            s.add(1)
            s.add(2)
            s.add(2)  # duplicate ignored
            s.add(3)
            print(len(s))
            print(1 in s)
            s.remove(2)
            print(len(s))
            print(2 in s)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["3", "True", "2", "False"]


def test_set_intersection(tmp_path, monkeypatch):
    src = tmp_path / "set_intersection.py"
    exe = tmp_path / "set_intersection.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            a = {1, 2, 3, 4}
            b = {3, 4, 5, 6}
            c = a & b
            print(len(c))
            print(3 in c)
            print(4 in c)
            print(5 in c)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["2", "True", "True", "False"]


def test_set_union(tmp_path, monkeypatch):
    src = tmp_path / "set_union.py"
    exe = tmp_path / "set_union.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            a = {1, 2, 3}
            b = {3, 4, 5}
            c = a | b
            print(len(c))
            print(1 in c)
            print(5 in c)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["5", "True", "True"]


def test_set_difference(tmp_path, monkeypatch):
    src = tmp_path / "set_difference.py"
    exe = tmp_path / "set_difference.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            a = {1, 2, 3, 4}
            b = {3, 4, 5}
            c = a - b
            print(len(c))
            print(1 in c)
            print(2 in c)
            print(3 in c)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["2", "True", "True", "False"]


def test_set_in_operator(tmp_path, monkeypatch):
    src = tmp_path / "set_in.py"
    exe = tmp_path / "set_in.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            s = {10, 20, 30}
            print(10 in s)
            print(99 in s)
            print(20 not in s)
            print(99 not in s)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["True", "False", "False", "True"]


def test_set_iteration_count(tmp_path, monkeypatch):
    src = tmp_path / "set_iter.py"
    exe = tmp_path / "set_iter.out"
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


def test_set_discard_no_error(tmp_path, monkeypatch):
    src = tmp_path / "set_discard.py"
    exe = tmp_path / "set_discard.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            s = {1, 2, 3}
            s.discard(2)
            print(len(s))
            s.discard(99)  # no error
            print(len(s))

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["2", "2"]


def test_set_from_list_dedup(tmp_path, monkeypatch):
    src = tmp_path / "set_from_list.py"
    exe = tmp_path / "set_from_list.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            xs = [1, 2, 2, 3, 3, 3, 4]
            s = set(xs)
            print(len(s))

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip() == "4"


def test_set_subset_superset(tmp_path, monkeypatch):
    src = tmp_path / "set_subset.py"
    exe = tmp_path / "set_subset.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            a = {1, 2}
            b = {1, 2, 3, 4}
            print(a.issubset(b))
            print(b.issuperset(a))
            print(b.issubset(a))

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["True", "True", "False"]
