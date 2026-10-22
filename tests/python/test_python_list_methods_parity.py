"""CPython parity for ``list`` methods, ported from
``Lib/test/test_list.py``.

All tests use ``libpython_mode="off"`` so we exercise pcc's native
``list`` runtime.

Reference contract (from CPython):
  * ``list`` is a mutable, ordered, heterogeneous sequence
  * indexing, slicing, ``+``, ``*`` honour Python sequence rules
  * ``append`` / ``extend`` / ``insert`` / ``pop`` / ``remove`` /
    ``index`` / ``count`` / ``reverse`` / ``sort`` / ``clear`` /
    ``copy`` are the canonical surface
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


def test_list_append_extend(tmp_path, monkeypatch):
    src = tmp_path / "list_append.py"
    exe = tmp_path / "list_append.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            xs: list = []
            xs.append(1)
            xs.append(2)
            xs.append(3)
            print(xs[0], xs[1], xs[2], len(xs))
            xs.extend([4, 5])
            print(xs[3], xs[4], len(xs))

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["1 2 3 3", "4 5 5"]


def test_list_insert_pop(tmp_path, monkeypatch):
    src = tmp_path / "list_insert.py"
    exe = tmp_path / "list_insert.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            xs = [1, 2, 4]
            xs.insert(2, 3)
            print(xs[0], xs[1], xs[2], xs[3])
            popped = xs.pop()
            print(popped, len(xs))
            popped2 = xs.pop(0)
            print(popped2, len(xs))

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["1 2 3 4", "4 3", "1 2"]


def test_dynamic_pop_dispatches_list_and_dict_by_runtime_tag(tmp_path, monkeypatch):
    src = tmp_path / "dynamic_pop.py"
    exe = tmp_path / "dynamic_pop.out"
    src.write_text(textwrap.dedent("""
        def pop_one(obj, key):
            return obj.pop(key)

        def main() -> None:
            xs = ["a", "b"]
            table = {"a": 7, "b": 9}
            print(pop_one(xs, 0))
            print(xs)
            print(pop_one(table, "b"))
            print(table["a"], len(table))

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["a", "['b']", "9", "7 1"]


def test_list_index_count(tmp_path, monkeypatch):
    src = tmp_path / "list_index_count.py"
    exe = tmp_path / "list_index_count.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            xs = [1, 2, 3, 2, 1]
            print(xs.index(2))
            print(xs.count(1))
            print(xs.count(2))
            print(xs.count(99))

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["1", "2", "2", "0"]


def test_list_remove(tmp_path, monkeypatch):
    src = tmp_path / "list_remove.py"
    exe = tmp_path / "list_remove.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            xs = [1, 2, 3, 2, 1]
            xs.remove(2)
            print(xs[0], xs[1], xs[2], xs[3], len(xs))

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip() == "1 3 2 1 4"


def test_list_reverse(tmp_path, monkeypatch):
    src = tmp_path / "list_reverse.py"
    exe = tmp_path / "list_reverse.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            xs = [1, 2, 3, 4]
            xs.reverse()
            print(xs[0], xs[1], xs[2], xs[3])

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip() == "4 3 2 1"


def test_list_clear(tmp_path, monkeypatch):
    src = tmp_path / "list_clear.py"
    exe = tmp_path / "list_clear.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            xs = [1, 2]
            print(xs.append(3) is None, xs[2], len(xs))
            print(xs.extend([4, 5]) is None, xs[4], len(xs))
            print(xs.insert(0, 0) is None, xs[0], len(xs))
            print(xs.remove(3) is None, len(xs))
            print(xs.reverse() is None, xs[0], len(xs))
            print(xs.clear() is None, len(xs))
            xs.append(6)
            print(xs[0], len(xs))

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == [
        "True 3 3",
        "True 5 5",
        "True 0 6",
        "True 5",
        "True 5 5",
        "True 0",
        "6 1",
    ]


def test_typed_void_list_mutators_lower_to_none_singleton():
    source = (
        Path(__file__).absolute().parents[2]
        / "pcc" / "frontends" / "python"
        / "codegen"
        / "list_method_lowering.py"
    ).read_text(encoding="utf-8")
    typed = source.split("    def _maybe_emit_list_method(", 1)[1].split(
        "    def _emit_list_sort_with_dunder_lt(", 1
    )[0]
    assert "return ir.Constant(_I1, 0)" not in typed
    for name, next_name in (
        ("append", "extend"),
        ("extend", "insert"),
        ("insert", "pop"),
        ("remove", "clear"),
        ("clear", "index"),
        ("reverse", "copy"),
    ):
        body = typed.split(f'if name == "{name}":', 1)[1].split(
            f'if name == "{next_name}":', 1
        )[0]
        assert "return self._emit_none_literal()" in body


def test_list_slicing(tmp_path, monkeypatch):
    src = tmp_path / "list_slice.py"
    exe = tmp_path / "list_slice.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            xs = [10, 20, 30, 40, 50]
            a = xs[1:4]
            print(a[0], a[1], a[2], len(a))
            b = xs[:2]
            print(b[0], b[1], len(b))
            c = xs[3:]
            print(c[0], c[1], len(c))
            d = xs[::2]
            print(d[0], d[1], d[2], len(d))

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == [
        "20 30 40 3", "10 20 2", "40 50 2", "10 30 50 3",
    ]


def test_list_concat_repeat(tmp_path, monkeypatch):
    src = tmp_path / "list_concat.py"
    exe = tmp_path / "list_concat.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            a = [1, 2] + [3, 4]
            print(a[0], a[1], a[2], a[3], len(a))
            b = [0] * 5
            print(b[0], b[1], b[2], b[3], b[4], len(b))

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["1 2 3 4 4", "0 0 0 0 0 5"]


def test_list_in_operator(tmp_path, monkeypatch):
    src = tmp_path / "list_in.py"
    exe = tmp_path / "list_in.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            xs = [1, 2, 3]
            print(2 in xs)
            print(99 in xs)
            print(2 not in xs)
            print(99 not in xs)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["True", "False", "False", "True"]


def test_list_iteration(tmp_path, monkeypatch):
    src = tmp_path / "list_iter.py"
    exe = tmp_path / "list_iter.out"
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

def test_list_sort_method(tmp_path, monkeypatch):
    src = tmp_path / "list_sort.py"
    exe = tmp_path / "list_sort.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            xs = [3, 1, 4, 1, 5, 9, 2, 6]
            xs.sort()
            print(xs[0], xs[1], xs[2], xs[3], xs[4], xs[5], xs[6], xs[7])

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip() == "1 1 2 3 4 5 6 9"


def test_list_sorted_function(tmp_path, monkeypatch):
    src = tmp_path / "list_sorted.py"
    exe = tmp_path / "list_sorted.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            xs = [3, 1, 4, 1, 5, 9]
            ys = sorted(xs)
            print(ys[0], ys[1], ys[2], ys[3], ys[4], ys[5])

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip() == "1 1 3 4 5 9"
