"""Dict-literal owned-temp release regressions (2026-06-12).

Two frontend ownership leaks made steady-state churn grow RSS without
bound (found by the G-P3 long-run harness, attributed via leaks(1)):

1. ``_emit_dict_literal`` (all three emission sites: exact-int branch,
   general branch, splat) called ``py_dict_set``/``py_dict_update`` —
   which BORROW (balanced ``pcc_gc_store_ptr``) — without releasing
   owned key/value temps, leaking one object per stored owned temp.
   The list/tuple literal paths released; dict did not.
2. ``_raw_scaffold_object_rhs_is_owned`` was missing ``str``/``bytes``
   in its builtin-constructor set, so ``s = str(x)`` in a module that
   imports ``pcc.extern`` skipped owned-local management and leaked the
   previous value on every rebind.

The runtime test proves the semantic (finalizers run == temps died) on
every GC backend; the IR test pins the raw-scaffold owned-local shape.
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).absolute().parents[2]

_DICT_RELEASE_PROGRAM = textwrap.dedent(
    """
    finalized = 0
    created = 0


    class Tracked:
        def __init__(self, k: int):
            global created
            created = created + 1
            self.k = k

        def __del__(self):
            global finalized
            finalized = finalized + 1


    def exact_int_branch(n: int) -> int:
        i = 0
        while i < n:
            d = {1: Tracked(i), 2: i}
            i = i + 1
        return 0


    def general_branch(n: int) -> int:
        i = 0
        while i < n:
            d = {"a": Tracked(i)}
            i = i + 1
        return 0


    def splat_branch(n: int) -> int:
        i = 0
        while i < n:
            base = {"x": Tracked(i)}
            d = {**base, "y": Tracked(i + 1)}
            i = i + 1
        return 0


    def main() -> int:
        n = 50
        exact_int_branch(n)
        general_branch(n)
        splat_branch(n)
        print(str(created) + "," + str(finalized))
        return 0


    main()
    """
)


@pytest.fixture(scope="module")
def dict_release_binary(tmp_path_factory):
    from pcc.frontends.python.pipeline import compile_python

    base = tmp_path_factory.mktemp("dict_release")
    src = base / "dict_release_prog.py"
    src.write_text(_DICT_RELEASE_PROGRAM, encoding="utf-8")
    exe = base / "dict_release_prog"
    compile_python(
        str(src),
        str(exe),
        libpython_mode="off",
        ir_scaffold_mode="on",
        backend="self",
    )
    return exe


@pytest.mark.parametrize("backend", ["0", "1", "2", "3", "4"])
def test_dict_literal_value_temps_die(dict_release_binary, backend):
    env = os.environ.copy()
    env["PCC_GC_BACKEND"] = backend
    result = subprocess.run(
        [str(dict_release_binary)],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    line = result.stdout.strip().splitlines()[-1]
    created, finalized = (int(x) for x in line.split(","))
    assert created == 200, line
    # Every Tracked stored as a dict-literal value must have been
    # finalized once the dict died. Before the fix the owned value
    # temps kept refcount 1 forever (py_dict_set borrows), so
    # finalized stayed 0.
    assert finalized == created, line


def test_raw_scaffold_str_call_gets_owned_management(tmp_path):
    """``s = str(x)`` in a pcc.extern-importing module must go through
    owned-local management (release-on-rebind). Asserts the owned
    resolve marker the assignment path only emits when the RHS is
    classified owned in raw-scaffold mode."""
    from pcc.frontends.python.pipeline import compile_python

    src = tmp_path / "raw_scaffold_str.py"
    src.write_text(
        textwrap.dedent(
            """
            from pcc.extern import c_int64, extern

            pcc_monotonic_us = extern(
                "pcc_runtime_monotonic_us", (), c_int64
            )


            def main() -> int:
                j = 0
                acc = 0
                while j < int(pcc_monotonic_us() > 0) + 10:
                    s = str(j % 97)
                    acc = acc + len(s)
                    j = j + 1
                return acc


            main()
            """
        )
    , encoding="utf-8")
    out = tmp_path / "raw_scaffold_str.ll"
    compile_python(
        str(src),
        str(out),
        emit_llvm_only=True,
        libpython_mode="off",
        ir_scaffold_mode="on",
    )
    ir_text = out.read_text(encoding="utf-8")
    assert "s.owned.resolve" in ir_text
