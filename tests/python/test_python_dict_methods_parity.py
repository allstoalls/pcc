"""CPython parity for ``dict`` methods, ported from
``Lib/test/test_dict.py``.

All tests use ``libpython_mode="off"``.

Reference contract (from CPython 3.7+):
  * ``dict`` preserves insertion order
  * ``__getitem__`` raises ``KeyError`` on missing key; ``get`` returns
    ``None`` (or the supplied default)
  * ``setdefault`` inserts on miss; ``update`` merges
  * ``in`` tests key presence; iteration yields keys
"""
from __future__ import annotations

import subprocess
import textwrap
import re
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


def test_dict_basic_get_set(tmp_path, monkeypatch):
    src = tmp_path / "dict_basic.py"
    exe = tmp_path / "dict_basic.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            d: dict = {}
            d["a"] = 1
            d["b"] = 2
            print(d["a"])
            print(d["b"])
            print(len(d))

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["1", "2", "2"]


def test_dict_get_default(tmp_path, monkeypatch):
    src = tmp_path / "dict_get.py"
    exe = tmp_path / "dict_get.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            d = {"a": 1}
            print(d.get("a"))
            print(d.get("missing"))
            print(d.get("missing", -1))

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["1", "None", "-1"]


def test_dict_setdefault(tmp_path, monkeypatch):
    src = tmp_path / "dict_setdefault.py"
    exe = tmp_path / "dict_setdefault.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            d: dict = {}
            print(d.setdefault("a", 1))
            print(d.setdefault("a", 99))
            print(d["a"])
            print(d.setdefault("b", 2))
            print(len(d))

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["1", "1", "1", "2", "2"]


def test_dict_keys_values_items(tmp_path, monkeypatch):
    src = tmp_path / "dict_kvi.py"
    exe = tmp_path / "dict_kvi.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            d = {"a": 1, "b": 2, "c": 3}
            ks = sorted(list(d.keys()))
            print(ks[0], ks[1], ks[2])
            vs = sorted(list(d.values()))
            print(vs[0], vs[1], vs[2])
            n = 0
            for k in d:
                n = n + 1
            print(n)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["a b c", "1 2 3", "3"]


def test_dict_items_for_loop_direct_lowering(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "dict_items_for.py"
    exe = tmp_path / "dict_items_for.out"
    ll = tmp_path / "dict_items_for.ll"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            d: dict[str, int] = {"a": 1, "b": 2, "c": 3}
            total = 0
            seen = ""
            for k, v in d.items():
                seen = seen + k
                total = total + v
            print(seen)
            print(total)
            box = Box()
            print(box.total())

        class Box:
            def __init__(self):
                self.d = {"x": 4, "y": 5}

            def total(self):
                n = 0
                for k, v in self.d.items():
                    n = n + v
                return n

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")

    compile_python(
        str(src), str(ll),
        ir_scaffold_mode="on", libpython_mode="off", emit_llvm_only=True,
    )
    ir_text = ll.read_text(encoding="utf-8")
    assert "py_dict_entries_used" in ir_text
    assert "py_dict_entry_key_at" in ir_text
    assert "py_dict_entry_value_at" in ir_text
    assert re.search(r"\bcall\b.*@py_dict_items\b", ir_text) is None

    compile_python(
        str(src), str(exe),
        ir_scaffold_mode="on", libpython_mode="off",
    )
    assert _run(exe).strip().splitlines() == ["abc", "6", "9"]


def test_dict_update(tmp_path, monkeypatch):
    src = tmp_path / "dict_update.py"
    exe = tmp_path / "dict_update.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            d = {"a": 1}
            d.update({"b": 2, "a": 99})
            print(d["a"])
            print(d["b"])
            print(len(d))

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["99", "2", "2"]


def test_dict_union_operator_self_backend(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "dict_union.py"
    exe = tmp_path / "dict_union.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            left = {"a": 1, "b": 2}
            right = {"b": 20, "c": 3}
            merged = left | right
            print(merged["a"])
            print(merged["b"])
            print(merged["c"])
            print(left["b"])

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    assert _run(exe).strip().splitlines() == ["1", "20", "3", "2"]


def test_dict_union_optional_dict_pattern_self_backend(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "dict_union_optional.py"
    exe = tmp_path / "dict_union_optional.out"
    src.write_text(textwrap.dedent("""
        def merge(params: dict[str, int] | None = None):
            params = params or {}
            common = {"b": 20}
            merged = params | {p: common[p] for p in common}
            return merged

        def main() -> None:
            first = merge({"a": 1, "b": 2})
            print(first["a"])
            print(first["b"])
            second = merge()
            print(second["b"])

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    assert _run(exe).strip().splitlines() == ["1", "20", "20"]


def test_chained_assignment_to_dict_subscript_self_backend(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "dict_chain_subscript.py"
    exe = tmp_path / "dict_chain_subscript.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            cache = {}
            name = "f77"
            alias = "gfortran"
            cache[name] = cache[alias] = alias
            print(cache[name])
            print(cache[alias])
            print(len(cache))

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    assert _run(exe).strip().splitlines() == ["gfortran", "gfortran", "2"]


def test_dict_in_operator(tmp_path, monkeypatch):
    src = tmp_path / "dict_in.py"
    exe = tmp_path / "dict_in.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            d = {"a": 1, "b": 2}
            print("a" in d)
            print("missing" in d)
            print("a" not in d)
            print("missing" not in d)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["True", "False", "False", "True"]


def test_dict_pop(tmp_path, monkeypatch):
    src = tmp_path / "dict_pop.py"
    exe = tmp_path / "dict_pop.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            d = {"a": 1, "b": 2, "c": 3}
            v = d.pop("b")
            print(v)
            print(len(d))
            v2 = d.pop("missing", -1)
            print(v2)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["2", "2", "-1"]


def test_dict_iteration_order(tmp_path, monkeypatch):
    src = tmp_path / "dict_order.py"
    exe = tmp_path / "dict_order.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            d: dict = {}
            d["c"] = 1
            d["a"] = 2
            d["b"] = 3
            keys: list = []
            for k in d:
                keys.append(k)
            print(keys[0], keys[1], keys[2])

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    # Insertion order: c, a, b — CPython 3.7+ guarantees.
    assert _run(exe).strip() == "c a b"


def test_dict_clear(tmp_path, monkeypatch):
    src = tmp_path / "dict_clear.py"
    exe = tmp_path / "dict_clear.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            d = {"a": 1, "b": 2}
            print(len(d))
            d.clear()
            print(len(d))
            print("a" in d)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["2", "0", "False"]


def test_dict_int_keys(tmp_path, monkeypatch):
    src = tmp_path / "dict_int_keys.py"
    exe = tmp_path / "dict_int_keys.out"
    src.write_text(textwrap.dedent("""
        def main() -> None:
            d: dict = {}
            d[1] = "one"
            d[2] = "two"
            d[3] = "three"
            print(d[2])
            print(len(d))
            print(1 in d)
            print(99 in d)

        if __name__ == "__main__":
            main()
        """).lstrip(), encoding="utf-8")
    _compile(monkeypatch, src, exe)
    assert _run(exe).strip().splitlines() == ["two", "3", "True", "False"]
