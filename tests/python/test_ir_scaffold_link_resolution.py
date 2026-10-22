"""Phase 6 Task 24: end-to-end multi-file compile in ON mode produces
self-contained IR (every scaffold extern is satisfied by a definition
in the same combined IR).

This is the key integration check for Path A: prove that the
``@user_pcc_ir_ir_IRBuilder_*`` symbols my codegen references
are *actually provided* by the natively-compiled ``pcc.ir.ir``
when both files are passed to ``compile_python_multi``. Without that
match, ON-mode binaries would fail to link.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


_REPO_ROOT = Path(__file__).absolute().parents[2]
_BUILD = _REPO_ROOT / "build"
_BUILD.mkdir(parents=True, exist_ok=True)

_LAYER1_IR_PATH = _REPO_ROOT / "pcc" / "ir" / "ir.py"


def _multi_compile_ll(src_paths, module_names, name: str, *, mode: str) -> str:
    from pcc.frontends.python.pipeline import compile_python_multi

    out = _BUILD / f"{name}.ll"
    compile_python_multi(
        [str(p) for p in src_paths],
        str(out),
        emit_llvm_only=True,
        entry_module=module_names[0],
        module_names=list(module_names),
        ir_scaffold_mode=mode,
    )
    return out.read_text(encoding="utf-8")


def _externs_called(ir_text: str) -> set[str]:
    """Return the set of @user_pcc_ir_ir_IRBuilder_* names
    that appear as call targets."""
    return set(
        re.findall(
            r"\bcall[^\n]*@(user_pcc_ir_ir_IRBuilder_[A-Za-z0-9_]+)",
            ir_text,
        )
    )


def _externs_defined(ir_text: str) -> set[str]:
    """Return the set of @user_pcc_ir_ir_IRBuilder_* names
    that appear as definitions."""
    return set(
        re.findall(
            r"^\s*define[^\n]+@(user_pcc_ir_ir_IRBuilder_[A-Za-z0-9_]+)\(",
            ir_text,
            re.MULTILINE,
        )
    )


_FAKELAYER1_PROGRAM = (
    "def emit_arith(builder, a, b, ptr) -> None:\n"
    "    sum_v = builder.add(a, b)\n"
    "    prod_v = builder.mul(sum_v, a)\n"
    "    builder.store(prod_v, ptr)\n"
    "\n"
    "def emit_branch(builder, lhs, rhs, t, f) -> None:\n"
    "    cmp = builder.icmp_signed(\"==\", lhs, rhs)\n"
    "    builder.cbranch(cmp, t, f)\n"
)


@pytest.fixture
def fakelayer1_path():
    src = _BUILD / "linkres_fakelayer1.py"
    src.write_text(_FAKELAYER1_PROGRAM, encoding="utf-8")
    return src


def test_combined_ir_contains_both_calls_and_definitions(fakelayer1_path):
    """When fakelayer1 + pcc.ir.ir are compiled together in ON
    mode, the combined IR should contain BOTH the call sites and the
    matching function definitions for the same scaffold symbols."""
    ir_text = _multi_compile_ll(
        [fakelayer1_path, _LAYER1_IR_PATH],
        ["fakelayer1", "pcc.ir.ir"],
        "linkres_combined",
        mode="on",
    )
    called = _externs_called(ir_text)
    defined = _externs_defined(ir_text)
    expected = {
        "user_pcc_ir_ir_IRBuilder_add",
        "user_pcc_ir_ir_IRBuilder_mul",
        "user_pcc_ir_ir_IRBuilder_store",
        "user_pcc_ir_ir_IRBuilder_icmp_signed",
        "user_pcc_ir_ir_IRBuilder_cbranch",
    }
    assert expected.issubset(called), (
        f"missing expected calls. got={called - expected}; "
        f"want={expected}"
    )
    assert expected.issubset(defined), (
        f"missing expected definitions. got={defined - expected}; "
        f"want={expected}"
    )


def test_no_unresolved_scaffold_symbols(fakelayer1_path):
    """Every IRBuilder scaffold symbol called in fakelayer1's IR must
    be provided by a definition somewhere in the combined IR. If a
    call is missing its definition, link would fail.
    """
    ir_text = _multi_compile_ll(
        [fakelayer1_path, _LAYER1_IR_PATH],
        ["fakelayer1", "pcc.ir.ir"],
        "linkres_unresolved",
        mode="on",
    )
    called = _externs_called(ir_text)
    defined = _externs_defined(ir_text)
    unresolved = called - defined
    assert not unresolved, (
        f"unresolved scaffold symbols (no definition in combined IR):\n  "
        + "\n  ".join(sorted(unresolved))
    )


def test_off_mode_does_not_pull_real_symbols(fakelayer1_path):
    """OFF mode should NOT emit @user_pcc_ir_ir_IRBuilder_*
    references — it routes through py_cpy_*. Pulling pcc.ir.ir
    into the closure in OFF mode is a separate question; here we just
    confirm the scaffold extern isn't accidentally emitted.
    """
    ir_text = _multi_compile_ll(
        [fakelayer1_path],
        ["fakelayer1"],
        "linkres_off",
        mode="off",
    )
    called = _externs_called(ir_text)
    assert not called, (
        f"OFF mode unexpectedly emitted scaffold extern calls: {called}"
    )
