"""Codegen-focused regression for nested closure + comprehension scope.

This covers the case where the same comprehension target name appears inside a
nested function. If free-name analysis is wrong, pcc emits a synthesized closure
that expects that target in an outer scope and then fails with
``reference to unbound name <target>``.
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path


def _compile_and_run(tmp_path: Path, source: str) -> list[str]:
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "probe.py"
    exe = tmp_path / "probe.out"
    src.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    compile_python(
        str(src),
        str(exe),
        ir_scaffold_mode="on",
        libpython_mode="off",
        backend="self",
    )
    proc = subprocess.run([str(exe)], text=True, capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout.strip().splitlines()


def test_nested_genexpr_target_named_h_is_not_captured(tmp_path):
    lines = _compile_and_run(
        tmp_path,
        """
        def outer(xs):
            offset = 1

            def inner():
                return tuple(h + offset for h in xs)

            return inner()

        vals = outer([1, 2, 3])
        print(vals[0], vals[1], vals[2])
        """,
    )
    assert lines == ["2 3 4"]


def test_nested_list_dict_set_comp_target_named_h_is_not_captured(tmp_path):
    lines = _compile_and_run(
        tmp_path,
        """
        def outer(xs):
            scale = 2

            def inner():
                ys = [h * scale for h in xs]
                d = {h: h * scale for h in xs}
                s = {h % 2 for h in xs}
                print(len(ys))
                print(d[3])
                print(len(s))
                return ys

            return inner()

        outer([1, 2, 3])
        """,
    )
    assert lines == ["3", "6", "2"]


def test_nested_local_capture_shadows_same_named_module_function():
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.pipeline import count_py_cpy_fallback_calls
    from pcc.frontends.python.type_infer import infer_module

    source = textwrap.dedent("""
        def outer():
            cache = {"answer": 42}

            def read_answer():
                return cache["answer"]

            return read_answer()

        def cache(fn):
            return fn

        print(outer())
        """).lstrip()
    typed = infer_module(parse_and_lift(source, "<probe>", "probe"))
    codegen = L1CodeGen(typed, ir_scaffold_mode="on")
    ir_text = str(codegen.generate(typed))

    assert codegen._hoisted_capture_params["__nested_read_answer"] == ("cache",)
    assert "@user_probe___nested_read_answer(ptr %cache)" in ir_text
    assert count_py_cpy_fallback_calls(ir_text) == 0


def test_module_top_lambda_rechecks_conservative_module_free_var(monkeypatch):
    """A module function reported as free must stay module-resolved.

    The self-hosted recursive free-var collector can conservatively retain a
    module name.  It must not force the general CPython lambda wrapper merely
    because module globals are absent from the local function ``env``.
    """
    from pcc.frontends.python.py_lift import parse_and_lift
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.pipeline import count_py_cpy_fallback_calls
    from pcc.frontends.python.type_infer import infer_module

    source = textwrap.dedent("""
        def dtype(value):
            return value

        values = [3, 1, 2]
        values.sort(key=lambda x: dtype(x))
        """).lstrip()
    typed = infer_module(parse_and_lift(source, "<probe>", "probe"))
    original = L1CodeGen._lambda_free_vars

    def conservative(self, expr, param_names):
        names = original(self, expr, param_names)
        names.add("dtype")
        return names

    monkeypatch.setattr(L1CodeGen, "_lambda_free_vars", conservative)
    codegen = L1CodeGen(typed, ir_scaffold_mode="on")
    ir_text = str(codegen.generate(typed))

    assert "__native_lambda_" in ir_text
    assert count_py_cpy_fallback_calls(ir_text) == 0
