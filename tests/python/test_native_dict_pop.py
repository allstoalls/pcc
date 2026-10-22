"""Native ``dict.pop(key, default)`` dispatch."""

from __future__ import annotations

import re
import subprocess
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).absolute().parents[2]
_BUILD = _REPO_ROOT / "build"
_BUILD.mkdir(parents=True, exist_ok=True)


def _compile_to_ll(source: str, name: str, *, mode: str) -> str:
    from pcc.frontends.python.pipeline import compile_python

    src = _BUILD / f"{name}.py"
    out = _BUILD / f"{name}.ll"
    src.write_text(source, encoding="utf-8")
    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        ir_scaffold_mode=mode,
    )
    return out.read_text(encoding="utf-8")


def _function_body(ir_text: str, fn_name_suffix: str) -> str | None:
    pattern = re.compile(
        r"define\s+[^\n]*?@[A-Za-z0-9_]*"
        + re.escape(fn_name_suffix)
        + r"\s*\([^)]*\)[^{]*\{(.+?)\n\}",
        re.DOTALL,
    )
    m = pattern.search(ir_text)
    return m.group(1) if m else None


@pytest.mark.parametrize("mode", ["off", "on"])
def test_dict_pop_with_default_uses_native_runtime(mode):
    program = textwrap.dedent("""
        def f(values: dict[str, str], key: str) -> object:
            return values.pop(key, None)
        """)
    ir = _compile_to_ll(program, f"dict_pop_default_{mode}", mode=mode)
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_dict_get" in body, body
    assert "@py_dict_del" in body, body
    assert "cpy.fn.pop" not in body, body
    assert "cpy.call2.pop" not in body, body


def test_dict_pop_without_default_uses_native_keyerror_runtime():
    program = textwrap.dedent("""
        def f(values: dict[str, str], key: str) -> object:
            return values.pop(key)
        """)
    ir = _compile_to_ll(program, "dict_pop_no_default", mode="off")
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_dict_pop" in body, body
    assert "@py_err_occurred" in body, body
    assert "cpy.fn.pop" not in body, body


def test_dict_pop_without_default_raises_keyerror(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "dict_pop_missing.py"
    exe = tmp_path / "dict_pop_missing.out"
    src.write_text(
        textwrap.dedent("""
            def main() -> None:
                values = {"a": "b"}
                try:
                    values.pop("missing")
                except KeyError as e:
                    print(type(e).__name__)

            if __name__ == "__main__":
                main()
            """).lstrip(),
        encoding="utf-8",
    )
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "KeyError\n"


def test_dynamic_pop_attr_on_list_and_dict_no_libpython(
    tmp_path,
    monkeypatch,
    pcc_runtime_archive,
):
    monkeypatch.setenv("PCC_RUNTIME_ARCHIVE", str(pcc_runtime_archive))
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "dynamic_pop_attr.py"
    exe = tmp_path / "dynamic_pop_attr.out"
    src.write_text(
        textwrap.dedent("""
            def pop_index(obj, idx):
                fn = getattr(obj, "pop")
                return fn(idx)

            def pop_key(obj, key, default):
                fn = getattr(obj, "pop")
                return fn(key, default)

            def main() -> None:
                xs = ["a", "b"]
                values = {"x": 1}
                print(pop_index(xs, 0))
                print(xs)
                print(pop_key(values, "missing", 9))
                print(pop_key(values, "x", 0))
                print(values)

            if __name__ == "__main__":
                main()
            """).lstrip(),
        encoding="utf-8",
    )
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
    )
    result = subprocess.run(
        [str(exe)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["a", "['b']", "9", "1", "{}"]


@pytest.mark.parametrize("mode", ["off", "on"])
def test_dict_clear_uses_native_runtime(mode):
    program = textwrap.dedent("""
        def f(values: dict[str, int]) -> int:
            values.clear()
            return len(values)
        """)
    ir = _compile_to_ll(program, f"dict_clear_{mode}", mode=mode)
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_dict_clear" in body, body
    assert "@py_dict_len" in body, body
    assert "cpy.fn.clear" not in body, body
    assert "cpy.call0.clear" not in body, body


def test_dyn_clear_uses_type_dispatch_runtime():
    program = textwrap.dedent("""
        def f(values):
            values.clear()
            return None
        """)
    ir = _compile_to_ll(program, "dyn_clear_dispatch", mode="on")
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_obj_clear" in body, body
    assert "@py_list_clear" not in body, body
    assert "cpy.fn.clear" not in body, body


def test_abc_imports_are_compile_time_only():
    program = textwrap.dedent("""
        from abc import ABC, abstractmethod

        class Base(ABC):
            @abstractmethod
            def value(self) -> int:
                return 0

        class Impl(Base):
            def value(self) -> int:
                return 7

        def f() -> int:
            obj = Impl()
            return obj.value()
        """)
    ir = _compile_to_ll(program, "abc_compile_time_only", mode="on")
    assert "cpy.fromimport.abc" not in ir, ir
    assert "cpy.from.ABC" not in ir, ir
    assert "cpy.from.abstractmethod" not in ir, ir


def test_dict_popitem_lifo_and_keyerror(tmp_path):
    """``dict.popitem()`` removes+returns the LAST-inserted (key, value) pair
    (insertion-ordered dicts), and raises KeyError when empty. Previously
    unsupported (libpython fallback) on the no-libpython path."""
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "popitem.py"
    src.write_text(textwrap.dedent("""
        def main():
            d = {}
            d['x'] = 1
            d['y'] = 2
            d['z'] = 3
            print(d.popitem())           # ('z', 3)  -- LIFO
            print(d.popitem())           # ('y', 2)
            print(sorted(d.items()))     # [('x', 1)]
            print(d.popitem())           # ('x', 1)
            print(len(d))                # 0
            try:
                d.popitem()
                print("NO_RAISE")
            except KeyError:
                print("empty raises KeyError")
        main()
    """), encoding="utf-8")
    exe = tmp_path / "popitem.out"
    compile_python(str(src), str(exe), ir_scaffold_mode="on",
                   libpython_mode="off", backend="self")
    r = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert r.stdout == (
        "('z', 3)\n('y', 2)\n[('x', 1)]\n('x', 1)\n0\n"
        "empty raises KeyError\n"
    ), r.stdout
