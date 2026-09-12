"""The self backend's C optimizer must not reach for llvmlite.

`_prepare_self_backend_units` used to build an LLVM target machine and run
llvmlite's pass manager, so *compiling C with the self backend* raised
``ImportError: llvmlite is required for this C toolchain path`` on a stage
without llvmlite -- the exact dependency `CEvaluator.__init__` documents this
path as not having.  These tests pin the owned pipeline in place.
"""

import pytest

from pcc.evaluater.c_evaluator import CEvaluator
from pcc.py_frontend.pipeline_pass_config import PYTHON_IR_PASS_DEFAULT_TIER


_UNIT_IR = """\
define i32 @main() {
entry:
  %x = alloca i32
  store i32 42, ptr %x
  %v = load i32, ptr %x
  ret i32 %v
}
"""


def _units():
    return [("unit0", _UNIT_IR, "i32", ())]


def test_self_constructor_does_not_load_llvm_layout(monkeypatch):
    import pcc.evaluater.c_evaluator as evaluator_module

    def forbidden():
        pytest.fail("self backend requested an external LLVM data layout")

    monkeypatch.setattr(evaluator_module, "_host_data_layout_text", forbidden)
    evaluator = CEvaluator(backend="self")
    assert evaluator.backend == "self"


def test_prepare_self_backend_units_never_touches_the_llvm_target():
    evaluator = CEvaluator(backend="self")

    def _explode(_self):
        raise AssertionError(
            "the self-backend C path built an LLVM target machine"
        )

    # The property is the single door to llvmlite on this path: `target`
    # calls `_llvm()`, which raises ImportError when llvmlite is absent.
    original = type(evaluator).target
    type(evaluator).target = property(_explode)
    try:
        prepared = evaluator._prepare_self_backend_units(_units(), optimize=2)
    finally:
        type(evaluator).target = original

    assert len(prepared) == 1
    unit_name, ir_text, return_type, external_defs = prepared[0]
    assert unit_name == "unit0"
    assert return_type == "i32"
    assert external_defs == ()
    assert isinstance(ir_text, str) and "define i32 @main()" in ir_text


def test_prepare_self_backend_units_runs_the_owned_default_tier(monkeypatch):
    evaluator = CEvaluator(backend="self")
    calls = []

    import pcc.py_frontend.compiled_owned_passes as owned

    original = owned.run_owned_passes

    def _traced(text, names, strict_no_libpython):
        calls.append((tuple(names), strict_no_libpython))
        return original(text, names, strict_no_libpython)

    monkeypatch.setattr(owned, "run_owned_passes", _traced)
    evaluator._prepare_self_backend_units(_units(), optimize=2)

    assert calls == [(tuple(PYTHON_IR_PASS_DEFAULT_TIER), False)]


def test_mem2reg_actually_ran_on_the_c_unit():
    evaluator = CEvaluator(backend="self")
    prepared = evaluator._prepare_self_backend_units(_units(), optimize=2)
    _unit_name, ir_text, _return_type, _external_defs = prepared[0]
    # An optimizer that silently passed the text through would leave the
    # alloca/store/load triple untouched, and the test above would still see
    # its call.  Assert the transformation, not just the invocation.
    assert "alloca" not in ir_text
