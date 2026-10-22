"""Native ``set.update`` dispatch for pcc-owned sets."""
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
def test_set_update_uses_native_spread(mode):
    program = textwrap.dedent(
        """
        def f(items: list[str]) -> object:
            seen = set()
            seen.update(items)
            return seen
        """
    )
    ir = _compile_to_ll(program, f"set_update_{mode}", mode=mode)
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_set_new" in body, body
    assert "@py_set_add" in body, body
    assert "cpy.fn.update" not in body, body
    assert "cpy.call1.update" not in body, body


@pytest.mark.parametrize("mode", ["off", "on"])
def test_set_ior_uses_native_union(mode):
    program = textwrap.dedent(
        """
        def f(left_items: list[str], right_items: list[str]) -> object:
            left = set(left_items)
            right = set(right_items)
            left |= right
            return left
        """
    )
    ir = _compile_to_ll(program, f"set_ior_{mode}", mode=mode)
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_set_update" in body, body
    assert "@py_int_to_i64" not in body, body


@pytest.mark.parametrize("mode", ["off", "on"])
def test_annotated_set_ior_stays_set_union(mode):
    program = textwrap.dedent(
        """
        def f(excluded: set[str]) -> object:
            module_names: set[str] = set()
            module_names |= set(excluded)
            return module_names
        """
    )
    ir = _compile_to_ll(program, f"set_ior_annotated_{mode}", mode=mode)
    body = _function_body(ir, "f")
    assert body is not None
    assert "@py_set_update" in body, body
    assert "@py_int_to_i64" not in body, body


def test_set_equality_runtime(tmp_path):
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "set_eq.py"
    exe = tmp_path / "set_eq"
    src.write_text(
        textwrap.dedent(
            """
            print(set(["a", "b"]) == set(["b", "a"]))
            print(set(["a"]) == set(["a", "b"]))
            """
        )
    , encoding="utf-8")
    compile_python(str(src), str(exe), ir_scaffold_mode="on")
    run = subprocess.run(
        [str(exe)],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert run.stdout.splitlines() == ["True", "False"]
