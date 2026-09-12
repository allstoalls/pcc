"""An elided scaffold import must still run the IR provider's top-level init.

In ``--ir-scaffold=on`` mode two rewrites happen independently:

* ``_filter_ir_scaffold_closure`` drops ``pcc.llvm_capi.compat`` from the
  source closure and substitutes ``pcc.llvm_capi.ir``, which holds the real
  definitions behind every emitted ``user_pcc_llvm_capi_ir_*`` call.
* ``_emit_import_from`` treats ``from pcc.llvm_capi.compat import ir_c as ir``
  as a compile-time marker import and returns without emitting runtime IR.

Together they severed the only edge that ran
``_pcc_py_module_top_pcc_llvm_capi_ir``.  The provider was still *registered*
with ``py_compiled_module_register_init``, but registration only records the
initializer -- nothing executed it.  ``@.class.pcc_llvm_capi_ir.IntType``
therefore stayed NULL, and the first ``ir.IntType(1)`` read ``cls._cache``
off that NULL class.  The failure surfaced as

    AttributeError: 'object' object has no attribute '_cache'

which is exactly where pcc1 died compiling C, at ``pcc/codegen/c_types.py:22``
(``true_bit = bool_t(1)``, three lines after ``bool_t = ir.IntType(1)``).

The defect was invisible whenever *any* module in the program also did
``from pcc.llvm_capi.ir import X``: that import is a normal sibling import and
did run the provider's init, so the program depended on an unrelated import's
presence and ordering.  Both shapes are covered below.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]

# Reaches the classes only through the module object the compat shim hands
# back -- the shape every pcc codegen module uses, and the one that had no
# provider-init edge.
_COMPAT_ONLY = textwrap.dedent(
    '''\
    from pcc.llvm_capi.compat import ir_c as ir

    bool_t = ir.IntType(1)
    int8_t = ir.IntType(8)
    void_t = ir.VoidType()


    def main() -> int:
        print("bool:", str(bool_t))
        print("int8:", str(int8_t))
        print("void:", str(void_t))
        print("func:", str(ir.IntType(32)))
        return 0


    main()
    '''
)

_COMPAT_ONLY_EXPECTED = "bool: i1\nint8: i8\nvoid: void\nfunc: i32\n"

# The same program plus a direct sibling import of the provider.  This shape
# always worked; keeping it green proves the fix did not simply move the
# initialization to a place that double-runs it (the top-init is guarded, so
# two edges must still produce one execution and one interned IntType).
_COMPAT_PLUS_DIRECT = textwrap.dedent(
    '''\
    from pcc.llvm_capi.compat import ir_c as ir
    from pcc.llvm_capi.ir import IntType

    via_compat = ir.IntType(64)
    via_direct = IntType(64)


    def main() -> int:
        print("compat:", str(via_compat))
        print("direct:", str(via_direct))
        print("interned:", via_compat is via_direct)
        return 0


    main()
    '''
)

_COMPAT_PLUS_DIRECT_EXPECTED = "compat: i64\ndirect: i64\ninterned: True\n"


# The source has to live inside the repo: the dependency closure walks
# imports relative to the entry file's root, so a program written to a pytest
# ``tmp_path`` never reaches ``pcc.llvm_capi`` and fails at link instead of
# exercising the initialization order under test.
_BUILD = REPO_ROOT / "build" / "irscaffoldinit"


def _build_and_run(name: str, program: str) -> str:
    _BUILD.mkdir(parents=True, exist_ok=True)
    source = _BUILD / f"{name}.py"
    source.write_text(program, encoding="utf-8")
    binary = _BUILD / name

    build = subprocess.run(
        [
            sys.executable, "-m", "pcc",
            "--ir-scaffold=on", "--backend", "self",
            "--python-libpython", "off",
            str(source), "-o", str(binary),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=3600,
    )
    assert build.returncode == 0, build.stdout + build.stderr

    run = subprocess.run(
        [str(binary)], capture_output=True, text=True, timeout=300,
    )
    assert run.returncode == 0, (
        "'object' object has no attribute '_cache' means the provider's "
        "top-level init never ran and the class global is still NULL\n"
        + run.stdout + run.stderr
    )
    return run.stdout


@pytest.mark.pcc_gate(probe="self_backend")
def test_compat_only_import_initializes_the_provider():
    assert _build_and_run("compatonly", _COMPAT_ONLY) == _COMPAT_ONLY_EXPECTED


@pytest.mark.pcc_gate(probe="self_backend")
def test_direct_provider_import_still_interns():
    assert _build_and_run("compatdirect", _COMPAT_PLUS_DIRECT) == (
        _COMPAT_PLUS_DIRECT_EXPECTED
    )


def test_cpython_reference_behaviour(tmp_path):
    """Both contracts come from CPython, not from pcc's own output."""
    for name, program, expected in (
        ("compatonly", _COMPAT_ONLY, _COMPAT_ONLY_EXPECTED),
        ("compatdirect", _COMPAT_PLUS_DIRECT, _COMPAT_PLUS_DIRECT_EXPECTED),
    ):
        source = tmp_path / f"{name}_ref.py"
        source.write_text(program, encoding="utf-8")
        run = subprocess.run(
            [sys.executable, str(source)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert run.returncode == 0, run.stderr
        assert run.stdout == expected
