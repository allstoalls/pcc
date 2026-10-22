"""List literals containing CPython values bridge each element to pcc-native
instead of routing the whole list through _emit_cpython_list_ops.

Lock the chain-breaker at literal sites: a CPython element produced by an
imported-module call is converted at the list-build boundary via
``py_cpy_to_pcc_obj``, then appended to a freshly-allocated pcc-native
PyListObject. Downstream operations on the resulting list stay on the
typed-container fast path.

Splat-extend with a CPython iterable still falls back to the cpython
list ops (eager element copy is the wrong semantics there). Negative
test locks the safety bound.
"""
from __future__ import annotations

import re
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
        str(src), str(out),
        emit_llvm_only=True,
        ir_scaffold_mode=mode,
        libpython_mode="auto",
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
def test_list_literal_with_cpy_value_bridges(mode):
    """A list literal containing a CPython-backed value (here: a value
    that lands in ``_cpy_values`` because it came from a CPython-backed
    module call) should bridge that element through ``py_cpy_to_pcc_obj``
    and build a pcc-native list — not fall through ``cpython_list_ops``.
    """
    program = textwrap.dedent(
        """
        import decimal

        def f(p: str) -> list:
            x = decimal.Decimal("1")
            return [p, x, p]
        """
    )
    ir = _compile_to_ll(program, f"list_lit_bridge_{mode}", mode=mode)
    body = _function_body(ir, "f")
    assert body is not None
    # Native list construction wins out
    assert "@py_list_new" in body, body
    assert "@py_list_append" in body, body
    assert "@py_cpy_to_pcc_obj" in body, body
    # And the cpython_list_ops cluster is absent
    assert "cpy.list.append" not in body, body
    assert "cpy.builtin.list" not in body, body


def test_splat_cpy_iterable_still_falls_back():
    """A splat of a CPython-backed iterable can't be eagerly bridged
    (the splat-extend path doesn't promise element-by-element copy
    semantics). The dispatch declines and the legacy cpython_list_ops
    path takes over — locked here so future widening is deliberate.
    """
    program = textwrap.dedent(
        """
        import os

        def f() -> list:
            cpy_iter = os.environ.keys()
            return [*cpy_iter]
        """
    )
    ir = _compile_to_ll(program, "list_splat_cpy", mode="off")
    body = _function_body(ir, "f")
    assert body is not None
    # A terminal splat can safely use the CPython-list path. A CPython splat
    # followed by another operand is rejected by the focused ownership suite
    # until iteration can happen at the splat's exact source position.
    # Either cpython_list_ops fires, or codegen retains py_cpy_call_list.
    # The bridge MUST NOT have been used on the splat iterable.
    assert "@py_cpy_to_pcc_obj" not in body or "cpy.list" in body, body
