"""``isinstance(x, A | B)`` is the same test as ``isinstance(x, (A, B))``.

CPython has accepted a PEP 604 union as ``isinstance``'s second argument
since 3.10: the union object is built and immediately discarded, and the
answer is the OR over its members.  The tuple branch of the lowering already
ORs each member's test, so a ``|`` chain is flattened into a ``TupleExpr``
before dispatch and needs nothing else.

Without it ``pcc/package/uv_lock_sync.py`` failed the C-frontend self-host
closure on ``isinstance(node, ast.List | ast.Tuple)`` with "isinstance second
argument must be a bare class name, a tuple of bare class names, a
module.attr chain, type(None), or type(expr); got BinOp".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


_SOURCE = '''
def probe(x) -> str:
    out = []
    out.append(isinstance(x, int | str))
    out.append(isinstance(x, int | str | float))
    out.append(isinstance(x, list | dict))
    out.append(isinstance(x, type(None) | int))
    return " ".join([str(b) for b in out])


def main() -> None:
    print(probe(1))
    print(probe("a"))
    print(probe(1.5))
    print(probe([1]))
    print(probe({"k": 1}))
    print(probe(None))


main()
'''

_BOOL_SOURCE = '''
def main() -> None:
    t = True
    print(isinstance(t, int))
    print(isinstance(t, (int, str)))
    print(isinstance(t, int | str))


main()
'''


def _build_and_run(tmp_path: Path, name: str, source: str) -> str:
    from pcc.py_frontend.pipeline import compile_python

    src = tmp_path / (name + ".py")
    exe = tmp_path / (name + ".out")
    src.write_text(source, encoding="utf-8")
    # No libpython: the union must lower natively, not through a fallback.
    compile_python(str(src), str(exe), libpython_mode="off", backend="self")
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


def test_union_classinfo_matches_cpython_without_libpython(tmp_path):
    out = _build_and_run(tmp_path, "isinstance_union", _SOURCE)
    assert out.splitlines() == [
        "True True False True",
        "True True False False",
        "False True False False",
        "False False True False",
        "False False True False",
        "False False False True",
    ]


@pytest.mark.xfail(
    strict=True,
    reason=(
        "pcc does not model bool as a subclass of int in isinstance: "
        "isinstance(True, int) and isinstance(True, (int, str)) are both "
        "False, where CPython gives True.  Pre-existing and independent of "
        "the union flattening -- the tuple spelling, which this change does "
        "not touch, is wrong the same way.  All three lines must read True."
    ),
)
def test_bool_is_an_int_subclass_for_isinstance(tmp_path):
    out = _build_and_run(tmp_path, "isinstance_bool_int", _BOOL_SOURCE)
    assert out.splitlines() == ["True", "True", "True"]


def test_uv_lock_sync_lowers_under_strict_no_libpython():
    """The closure module the restriction blocked, compiled the pcc1 way."""
    from pcc.parse.py_lift import parse_and_lift
    from pcc.py_frontend import type_infer
    from pcc.py_frontend.codegen.layer1 import L1CodeGen

    path = Path(__file__).resolve().parents[2] / "pcc/package/uv_lock_sync.py"
    typed = type_infer.infer_module(
        parse_and_lift(
            path.read_text(encoding="utf-8"), str(path), "pcc.package.uv_lock_sync"
        )
    )
    codegen = L1CodeGen(typed, False, "on")
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    codegen._module_source_path = str(path)
    codegen._target_triple = ""
    codegen.generate(typed)
