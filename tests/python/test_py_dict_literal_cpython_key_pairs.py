"""A dict literal with a CPython-domain key may hold more than one pair.

``{names[0]: 1, "other": 2}`` where ``names`` is a raw libpython list used to
be refused with "multi-pair CPython-key dict literal cannot preserve per-pair
insertion errors before later operand evaluation".  The hazard that names
does not exist on that path: ``_emit_dict_literal``'s collection loop
evaluates every key and value operand, in source order, and only then does a
single ``_emit_cpython_dict_items`` insert them -- the same shape as CPython's
dict display, which pushes all operands and then runs one ``BUILD_MAP``.  So
an unhashable key raises only after the later operands have run.

The gap blocked ``pcc.passes.ast_utils`` and
``pcc.py_frontend.codegen.builtin_type_attr_lowering`` in the C-frontend
self-host closure.

CPython is the oracle, for the values, for duplicate-key precedence, and for
the order in which operand side effects happen.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


_SOURCE = '''
import fnmatch


def two_pairs_cpy_key(values: list) -> dict:
    names = fnmatch.filter(values, "*.py")
    return {names[0]: 1, "other": 2}


def three_pairs_mixed(values: list) -> dict:
    names = fnmatch.filter(values, "*.py")
    return {"first": 0, names[0]: 1, names[1]: 2}


def duplicate_cpy_key(values: list) -> dict:
    names = fnmatch.filter(values, "*.py")
    return {names[0]: 1, names[0]: 2}


def evaluation_order(log: list, values: list) -> dict:
    def note(tag, val):
        log.append(tag)
        return val

    names = fnmatch.filter(values, "*.py")
    return {note("k1", names[0]): note("v1", 1), note("k2", "z"): note("v2", 2)}


def main() -> None:
    vals = ["a.py", "b.py", "c.txt"]
    print(two_pairs_cpy_key(vals))
    print(three_pairs_mixed(vals))
    print(duplicate_cpy_key(vals))
    log = []
    print(evaluation_order(log, vals))
    print(log)


main()
'''

# The unhashable-key probe is deliberately a separate program: its key is a
# native ``list``, so it takes the native ``py_dict_new``/``py_dict_set``
# path, not the CPython-key path this file is about.
_NATIVE_ORDER_SOURCE = '''
def unhashable_key_after_all_operands(log: list) -> str:
    def note(tag, val):
        log.append(tag)
        return val

    try:
        d = {note("k1", []): note("v1", 1), note("k2", "b"): note("v2", 2)}
        return "no error " + str(len(d))
    except TypeError:
        return "TypeError"


def main() -> None:
    log = []
    print(unhashable_key_after_all_operands(log))
    print(log)


main()
'''


def _build_and_run(tmp_path: Path, name: str, source: str) -> str:
    from pcc.py_frontend.pipeline import compile_python

    src = tmp_path / (name + ".py")
    exe = tmp_path / (name + ".out")
    src.write_text(source, encoding="utf-8")
    # libpython is linked so the CPython arm runs rather than being replaced
    # by a fail-closed stub.
    compile_python(str(src), str(exe), libpython_mode="on")
    run = subprocess.run(
        [str(exe)], text=True, capture_output=True, timeout=180, check=True
    )
    reference = subprocess.run(
        ["python3", str(src)], text=True, capture_output=True, timeout=180, check=True
    )
    assert run.stdout == reference.stdout, (
        "pcc:\n" + run.stdout + "\ncpython:\n" + reference.stdout
    )
    return run.stdout


def test_multi_pair_cpython_key_dict_matches_cpython(tmp_path):
    out = _build_and_run(tmp_path, "dict_cpy_key_pairs", _SOURCE)
    assert out.splitlines() == [
        "{'a.py': 1, 'other': 2}",
        "{'first': 0, 'a.py': 1, 'b.py': 2}",
        # A duplicate key keeps the last value, as in CPython.
        "{'a.py': 2}",
        "{'a.py': 1, 'z': 2}",
        # Every operand runs, left to right, before any insertion.
        "['k1', 'v1', 'k2', 'v2']",
    ]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "The native dict-literal path inserts each pair as it is evaluated "
        "(_emit_dict_literal: 'Build one pair at a time so those effects "
        "(and failures) happen before the next key/value expression'), so an "
        "unhashable key raises before the later operands run and their side "
        "effects are lost.  CPython evaluates every operand and then runs one "
        "BUILD_MAP, so it logs ['k1', 'v1', 'k2', 'v2'].  Found while lifting "
        "the CPython-key pair-count restriction; the two paths are separate "
        "and this one is unchanged."
    ),
)
def test_native_dict_literal_runs_all_operands_before_insertion(tmp_path):
    out = _build_and_run(tmp_path, "dict_native_order", _NATIVE_ORDER_SOURCE)
    assert out.splitlines() == ["TypeError", "['k1', 'v1', 'k2', 'v2']"]


def test_the_two_closure_modules_lower_under_strict_no_libpython():
    """The modules the restriction blocked, compiled the way pcc1 compiles."""
    from pcc.parse.py_lift import parse_and_lift
    from pcc.py_frontend import type_infer
    from pcc.py_frontend.codegen.layer1 import L1CodeGen

    repo_root = Path(__file__).resolve().parents[2]
    for rel, mod in (
        ("pcc/passes/ast_utils.py", "pcc.passes.ast_utils"),
        (
            "pcc/py_frontend/codegen/builtin_type_attr_lowering.py",
            "pcc.py_frontend.codegen.builtin_type_attr_lowering",
        ),
    ):
        path = repo_root / rel
        typed = type_infer.infer_module(
            parse_and_lift(path.read_text(encoding="utf-8"), str(path), mod)
        )
        codegen = L1CodeGen(typed, False, "on")
        codegen._strict_no_libpython = True
        codegen._prefer_native_callable_values = True
        codegen._module_source_path = str(path)
        codegen._target_triple = ""
        codegen.generate(typed)
