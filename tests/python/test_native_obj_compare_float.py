"""Ordering comparison on a boxed float (DynType) under strict no-libpython.

Completes the boxed-float arithmetic class (after #26 `+`, #27 `-`/`*`).
``a.bal < 50`` where ``a.bal`` is a float at runtime (DynType) returned the wrong
result: the compare fell to the int fast path (``_to_int64``) which misread the
boxed-float pointer, AND the port's ``_cmp_threeway`` had no float case (float
vs int fell through to "equal").

Fix, two parts:
* runtime: add a numeric-float case to ``_cmp_threeway`` in the port
  (py_obj_ops_compare.py) — when both operands are numeric and at least one is a
  float, compare as doubles via py_float_to_f64 (the C already had this case);
* frontend: in compare_membership_lowering, route DynType ordering compares
  (`<` `<=` `>` `>=`) through py_obj_lt/le/gt/ge (float-aware via
  py_obj_cmp_threeway) instead of the int fast path.

Runs under ``--backend self --python-libpython=off`` in DEFAULT runtime mode.
"""
from __future__ import annotations

import ast
import inspect
import os
import subprocess
import textwrap
from pathlib import Path


def _run_pcc_program(tmp_path: Path, source: str) -> str:
    src = tmp_path / "prog.py"
    src.write_text(source, encoding="utf-8")
    exe = tmp_path / "prog_bin"
    env = os.environ.copy()
    env.pop("LC_ALL", None)
    build = subprocess.run(
        [
            "uv", "run", "pcc", "--backend", "self", "--python-libpython=off",
            "--ir-scaffold=on", str(src), "-o", str(exe),
        ],
        text=True, capture_output=True, timeout=420, env=env,
    )
    assert build.returncode == 0, build.stderr
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=30, env=env
    )
    assert run.returncode == 0, run.stderr
    return run.stdout


def test_obj_compare_boxed_float_no_libpython(tmp_path):
    out = _run_pcc_program(
        tmp_path,
        "class A:\n"
        "    def __init__(self, bal):\n"
        "        self.bal = bal\n"
        "def main():\n"
        "    a = A(100.0)\n"
        "    print(a.bal < 50, a.bal > 50, a.bal <= 100, a.bal >= 200)\n"  # F T T F
        "    print(a.bal < 50.0)\n"                                        # False (float-literal regression)
        "    x = a.bal / 4\n"
        "    print(x < 30, x > 30, x >= 25, x <= 25)\n"                    # T F T T
        "    i = A(100)\n"
        "    print(i.bal < 50, i.bal > 50, i.bal <= 100, i.bal >= 200)\n"  # F T T F (int regression)
        "    print(a.bal > i.bal, a.bal == i.bal, i.bal <= a.bal)\n"       # F T T (object/object path)
        "    print(5 < 3, 2.5 > 1.0, 'abc' < 'abd')\n"                     # F T T (typed/str regression)
        "main()\n",
    )
    assert out.split("\n")[:6] == [
        "False True True False",
        "False",
        "True False True True",
        "False True True False",
        "False True True",
        "False True True",
    ], out


def test_runtime_object_comparison_has_one_behavior_owner():
    from pcc.frontends.python.codegen.compare_membership_lowering import (
        CompareMembershipLoweringMixin,
    )
    from pcc.frontends.python.codegen.host_contract import L1_CODEGEN_HOST_METHODS

    owner_source = textwrap.dedent(
        inspect.getsource(CompareMembershipLoweringMixin._emit_runtime_object_compare)
    )
    caller_source = textwrap.dedent(
        inspect.getsource(CompareMembershipLoweringMixin._emit_compare)
    )
    owner_strings = {
        node.value
        for node in ast.walk(ast.parse(owner_source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    caller_strings = {
        node.value
        for node in ast.walk(ast.parse(caller_source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    # The object-vs-object path moved into ``_emit_owned_string_predicate``
    # (it adds operand ownership); it must still delegate to the owner rather
    # than re-select the runtime symbols.
    predicate_source = textwrap.dedent(
        inspect.getsource(CompareMembershipLoweringMixin._emit_owned_string_predicate)
    )
    predicate_strings = {
        node.value
        for node in ast.walk(ast.parse(predicate_source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    assert {"py_obj_lt", "py_obj_le", "py_obj_gt", "py_obj_ge"}.issubset(
        owner_strings
    )
    assert not {"py_obj_lt", "py_obj_le", "py_obj_gt", "py_obj_ge"}.intersection(
        caller_strings | predicate_strings
    )
    # Dyn ordering, class operands and the exact mixed-integer path (whose
    # operands stay in slot roots) in _emit_compare, and the object-vs-object
    # string predicate, all delegate to the owner.
    assert caller_source.count("_emit_runtime_object_compare(") == 3
    assert predicate_source.count("_emit_runtime_object_compare(") == 1
    assert "_emit_post_call_err_check" in owner_source
    assert "_emit_runtime_object_compare" in L1_CODEGEN_HOST_METHODS
