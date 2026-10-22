"""Phase 5 readiness check: ON mode reduces fallback count on a
synthetic layer1-shaped source.

This test gives Path A its first quantitative win: a small-but-realistic
program that mixes ``self.builder.X(...)`` calls and
``self.runtime[...]`` lookups (the two top idiom clusters from the
probe report) compiles to strictly fewer ``py_cpy_*`` calls in ON mode
than in OFF mode.

Once Task 20 starts migrating real frontend files, the assertions here
become a sanity check: if a synthetic file's reduction drops, something
broke the dispatch.
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
    )
    return out.read_text(encoding="utf-8")


# Mimics the shape of layer1's emit_assign-ish methods: dict-key
# runtime lookups, repeated builder method calls, mixed arithmetic.
# Should produce many py_cpy_* calls in OFF mode and far fewer in ON.
_LAYER1_SHAPED = textwrap.dedent(
    """
    from pcc.ir.compat import ir

    class FakeCodegen:
        def __init__(self):
            self.runtime = {}
            self.builder = ir.IRBuilder()

        def emit_arith(self, a, b, ptr) -> None:
            sum_v = self.builder.add(a, b)
            prod_v = self.builder.mul(sum_v, a)
            self.builder.store(prod_v, ptr)

        def emit_branch(self, cond, lhs, rhs, t, f) -> None:
            cmp = self.builder.icmp_signed("==", lhs, rhs)
            self.builder.cbranch(cmp, t, f)

        def emit_with_runtime_lookup(self, val) -> None:
            decref = self.runtime["py_cpy_decref"]
            self.builder.call(decref, [val])
    """
)


def _count_py_cpy_calls(ir_text: str) -> int:
    return len(re.findall(r"\bcall [^\n]*@py_cpy_", ir_text))


def test_layer1_shaped_on_reduces_fallbacks():
    """ON mode must keep these idioms at zero ``py_cpy_*`` fallback.

    The test originally asserted ON < OFF on a synthetic source; OFF
    has since closed the same idioms (typed-int / native dispatch
    landed in the standalone path too), so both modes are now zero on
    this source. The remaining invariant — that ON mode produces no
    libpython fallback for these specific scaffolded patterns — is
    still useful as a regression guard.
    """
    ir_off = _compile_to_ll(_LAYER1_SHAPED, "shape_off", mode="off")
    ir_on = _compile_to_ll(_LAYER1_SHAPED, "shape_on", mode="on")
    n_off = _count_py_cpy_calls(ir_off)
    n_on = _count_py_cpy_calls(ir_on)
    assert n_on == 0, (
        f"ON mode emitted unexpected py_cpy_* fallbacks: on={n_on}"
    )
    assert n_on <= n_off, (
        f"ON mode regressed past OFF: off={n_off} on={n_on}"
    )


def test_user_class_builder_field_does_not_emit_scaffold_externs():
    """A user class can replace its field after construction at runtime."""
    ir_on = _compile_to_ll(
        _LAYER1_SHAPED, "shape_externs", mode="on",
    )
    expected_externs = (
        "@user_pcc_ir_ir_IRBuilder_add",
        "@user_pcc_ir_ir_IRBuilder_mul",
        "@user_pcc_ir_ir_IRBuilder_store",
        "@user_pcc_ir_ir_IRBuilder_icmp_signed",
        "@user_pcc_ir_ir_IRBuilder_cbranch",
        "@user_pcc_ir_ir_IRBuilder_call1",
    )
    for sym in expected_externs:
        assert sym not in ir_on, (
            f"ordinary user field was redirected to scaffold extern {sym}"
        )
    # Runtime dict lookup desugar: the looked-up function must be
    # declared and referenced directly.
    assert "@py_cpy_decref" in ir_on


def test_layer1_shaped_off_still_uses_py_cpy_for_those_patterns():
    """Historical regression guard. This synthetic source no longer
    forces ``py_cpy_*`` in OFF mode — the typed-int / native runtime
    dispatch closed the gap on the OFF side too, which is the desired
    direction. Keep the test (so future regressions that re-introduce
    OFF-side fallback on this exact source still trip a noisy gate)
    but flip the polarity: OFF must stay at zero now."""
    ir_off = _compile_to_ll(
        _LAYER1_SHAPED, "shape_off_guard", mode="off",
    )
    n_off = _count_py_cpy_calls(ir_off)
    assert n_off == 0, (
        f"OFF mode regressed on scaffolded synthetic source: "
        f"unexpected py_cpy_* fallback count {n_off}"
    )
