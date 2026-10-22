"""Fresh indexed x86 shells need no compatibility PHI-repair traversal."""

import pytest

from pcc.backend import self_backend_x86_64_linux as emitter
from pcc.backend.self_backend_ir import ParsedFunction
from pcc.backend.self_backend_kernel import IndexedFunctionKernel, get_indexed_function_kernel
from tests.python.test_x86_64_emission_lifetime import (
    PROGRAM,
    _emit,
    _indexed_module,
    _legacy_module,
)


pytestmark = pytest.mark.usefixtures("precise_stackmap_tables")

ZERO_PHI_PROGRAM = '''target triple = "x86_64-unknown-linux-gnu"
define i32 @pick(i1 %condition) {
entry:
  br i1 %condition, label %yes, label %no
yes:
  ret i32 40
no:
  ret i32 7
}
'''


@pytest.fixture(autouse=True)
def _fixed_emission_configuration(monkeypatch):
    monkeypatch.delenv("PCC_CODE_PROFILE", raising=False)
    monkeypatch.setenv("PCC_SELF_TARGET_PASSES", "none")
    monkeypatch.setenv("PCC_SELF_TARGET_PASS_TRANSPORT", "text")


@pytest.mark.parametrize("source", [ZERO_PHI_PROGRAM, PROGRAM], ids=["zero", "mixed"])
@pytest.mark.parametrize("packed", [False, True], ids=["symbolic", "packed"])
def test_fresh_indexed_shells_are_phi_complete_without_repair(
    tmp_path, monkeypatch, source, packed,
):
    expected = _emit(_legacy_module(tmp_path, source), packed)
    module = _indexed_module(tmp_path, source)
    real_project = emitter._materialize_indexed_control_flow
    real_fact = IndexedFunctionKernel.block_phi_fact
    real_emit_blocks = emitter.emit_function_blocks
    awaiting_emit = set()
    redundant_reads = []
    projected = []

    def observe_fact(kernel, block_id):
        if id(kernel) in awaiting_emit:
            redundant_reads.append((id(kernel), block_id))
        return real_fact(kernel, block_id)

    def observe_project(function, kernel):
        real_project(function, kernel)
        for block_id, block in enumerate(function.blocks):
            fact = real_fact(kernel, block_id)
            expected_phis = tuple(
                kernel.diagnostic_phi(block_id, index)
                for index in range(fact.second)
            )
            assert block.phis == expected_phis
        projected.append(function.name)
        awaiting_emit.add(id(kernel))

    def observe_emit_blocks(function, **kwargs):
        kernel = get_indexed_function_kernel(function)
        assert id(kernel) in awaiting_emit
        awaiting_emit.remove(id(kernel))
        assert redundant_reads == []
        return real_emit_blocks(function, **kwargs)

    monkeypatch.setattr(IndexedFunctionKernel, "block_phi_fact", observe_fact)
    monkeypatch.setattr(emitter, "_materialize_indexed_control_flow", observe_project)
    monkeypatch.setattr(emitter, "emit_function_blocks", observe_emit_blocks)
    assert _emit(module, packed) == expected
    assert projected == [function.name for function in module.functions]
    assert not awaiting_emit
    assert redundant_reads == []


@pytest.mark.parametrize(
    "owner", ["legacy", "function", "kernel", "blocks", "block-map"],
)
def test_compatibility_owners_still_repair_released_scalar_phis(
    tmp_path, monkeypatch, owner,
):
    class CustomFunction(ParsedFunction):
        pass

    class CustomKernel(IndexedFunctionKernel):
        __slots__ = ()

    class CustomList(list):
        pass

    class CustomDict(dict):
        pass

    expected = _emit(_legacy_module(tmp_path), False)
    module = _legacy_module(tmp_path)
    for function in module.functions:
        if owner == "function":
            function.__class__ = CustomFunction
        elif owner == "kernel":
            function.indexed_kernel.__class__ = CustomKernel
        elif owner == "blocks":
            function.blocks = CustomList(function.blocks)

    real_emit = emitter._emit_function
    real_emit_blocks = emitter.emit_function_blocks
    repair_needed = []
    checked = []

    def observe_emit(function, plan, *, phase_timing=None):
        if owner == "block-map":
            function.block_map = CustomDict(function.block_map)
        kernel = get_indexed_function_kernel(function)
        for block_id, block in enumerate(function.blocks):
            if not block.phis and kernel.block_phi_fact(block_id).second:
                repair_needed.append((function.name, block_id))
        return real_emit(function, plan, phase_timing=phase_timing)

    def observe_emit_blocks(function, **kwargs):
        kernel = get_indexed_function_kernel(function)
        for block_id, block in enumerate(function.blocks):
            assert len(block.phis) == kernel.block_phi_fact(block_id).second
        checked.append(function.name)
        return real_emit_blocks(function, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("compatibility owner entered the fresh indexed shell route")

    monkeypatch.setattr(emitter, "_emit_function", observe_emit)
    monkeypatch.setattr(emitter, "emit_function_blocks", observe_emit_blocks)
    monkeypatch.setattr(emitter, "_materialize_indexed_control_flow", forbidden)
    assert _emit(module, False) == expected
    assert repair_needed
    assert checked == [function.name for function in module.functions]
