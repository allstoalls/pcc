"""The x86 adapter reuses its block loop's absolute instruction ID."""

import pytest

from pcc.backend import BackendUnavailable
from pcc.backend import self_backend_x86_64_linux as emitter
from pcc.backend.self_backend_indexed_codec import (
    decode_indexed_module_file,
    encode_indexed_module_file,
)
from pcc.backend.self_backend_ir import PARSED_INSTRUCTION_KIND_BINOP, ParsedBlock
from pcc.backend.self_backend_kernel import IndexedFunctionKernel, get_indexed_function_kernel
from pcc.backend.target_objects import emit_indexed_assembly
from tests.python.test_x86_64_emission_lifetime import (
    PROGRAM,
    _emit,
    _indexed_module,
    _legacy_module,
)
from tests.python.test_x86_64_indexed_bridge import MIXED, _captured_module


pytestmark = pytest.mark.usefixtures("precise_stackmap_tables")


MULTI_BLOCK = '''target triple = "x86_64-unknown-linux-gnu"
define i32 @branches(i1 %condition, i32 %value) {
entry:
  %base = add i32 %value, 1
  br i1 %condition, label %yes, label %no
yes:
  %up = add i32 %base, 2
  br label %merge
no:
  %down = sub i32 %base, 2
  br label %merge
merge:
  %joined = phi i32 [ %up, %yes ], [ %down, %no ]
  %result = add i32 %joined, 3
  ret i32 %result
}
'''


@pytest.fixture(autouse=True)
def _fixed_emission_configuration(monkeypatch):
    monkeypatch.delenv("PCC_CODE_PROFILE", raising=False)
    monkeypatch.setenv("PCC_SELF_TARGET_PASSES", "none")
    monkeypatch.setenv("PCC_SELF_TARGET_PASS_TRANSPORT", "text")


@pytest.mark.parametrize(
    "source", [PROGRAM, MIXED, MULTI_BLOCK], ids=["call", "fixed-gep-cold", "later-blocks"],
)
@pytest.mark.parametrize("packed", [False, True], ids=["symbolic", "packed"])
def test_adapter_uses_absolute_id_without_reprojecting_block(
    tmp_path, monkeypatch, source, packed,
):
    expected = _emit(_legacy_module(tmp_path, source), packed)
    module = _indexed_module(tmp_path, source)
    original = IndexedFunctionKernel.instruction_data_by_id
    original_adapter = emitter._emit_indexed_instruction_parts
    observed = []

    def forbidden(*args):
        raise AssertionError("instruction decoder reread the block range")

    def checked(kernel, instruction_id, block_id, instruction_index):
        block = kernel.block_fact(block_id)
        assert instruction_id == block.first + instruction_index
        with monkeypatch.context() as guard:
            guard.setattr(IndexedFunctionKernel, "block_fact", forbidden)
            result = original(kernel, instruction_id, block_id, instruction_index)
        observed.append((block_id, instruction_id))
        return result

    def checked_adapter(*args):
        # A regression to the block-local entry must fail before it delegates
        # to the by-ID helper; guarding only inside the helper is too late.
        with monkeypatch.context() as guard:
            guard.setattr(IndexedFunctionKernel, "instruction_data", forbidden)
            return original_adapter(*args)

    monkeypatch.setattr(IndexedFunctionKernel, "instruction_data_by_id", checked)
    monkeypatch.setattr(emitter, "_emit_indexed_instruction_parts", checked_adapter)
    assert _emit(module, packed) == expected
    assert observed
    if source == MULTI_BLOCK:
        assert any(block_id > 0 and instruction_id > 0 for block_id, instruction_id in observed)


@pytest.mark.parametrize("replacement", ["negative", "tuple"])
def test_adapter_preserves_authoritative_payload_and_tuple_fallback(
    tmp_path, monkeypatch, replacement,
):
    module = _indexed_module(tmp_path, MIXED)
    function = module.functions[0]
    kernel = get_indexed_function_kernel(function)
    index = 3  # The flagged binop, after alloca/store/load.
    instruction_id = kernel.block_fact(0).first + index
    hint = kernel.instruction_record_id(0, index)
    expected = kernel.instruction_data(0, index)
    assert kernel.instruction_kind_id(0, index) == PARSED_INSTRUCTION_KIND_BINOP
    if replacement == "negative":
        # Existing emission hooks may replace payload metadata after preparation.
        # A stale positive callback hint must not override the current cold row.
        expected = expected[:-1] + ("9",)
        cold_id = len(kernel.cold_instruction_data)
        kernel.cold_instruction_data.append(expected)
        kernel.instruction_metadata.set_unchecked(instruction_id * 4 + 1, -cold_id - 1)
    else:
        hint = expected

    seen = []

    def capture(function_arg, block, kind, data, **kwargs):
        seen.append((kind, data))
        return ["selected"]

    monkeypatch.setattr(emitter, "emit_instruction_dispatch_parts", capture)
    try:
        assert emitter._emit_indexed_instruction_parts(
            function, ParsedBlock("entry"), kernel, 0, index,
            instruction_id, PARSED_INSTRUCTION_KIND_BINOP, hint, False,
        ) == ["selected"]
        assert seen == [("binop", expected)]
    finally:
        kernel.close_native_tables()


def test_absolute_decoder_preserves_missing_arithmetic_flag_failure(tmp_path, monkeypatch):
    module = _indexed_module(tmp_path, MIXED)
    function = module.functions[0]
    kernel = get_indexed_function_kernel(function)
    index = 3
    instruction_id = kernel.block_fact(0).first + index
    kernel.instruction_arithmetic_flag_values.clear()

    def forbidden(*args, **kwargs):
        raise AssertionError("dispatch ran before the flag check")

    monkeypatch.setattr(emitter, "emit_instruction_dispatch_parts", forbidden)
    try:
        with pytest.raises(KeyError) as caught:
            emitter._emit_indexed_instruction_parts(
                function, ParsedBlock("entry"), kernel, 0, index,
                instruction_id, PARSED_INSTRUCTION_KIND_BINOP,
                kernel.instruction_record_id(0, index), False,
            )
        assert caught.value.args == (instruction_id,)
    finally:
        kernel.close_native_tables()


def test_inconsistent_kind_metadata_is_still_rejected_before_emission(tmp_path, monkeypatch):
    module = _captured_module(monkeypatch, True)
    kernel = get_indexed_function_kernel(module.functions[0])
    path = tmp_path / "inconsistent-kind.pidx"
    try:
        kernel.instruction_metadata.set_unchecked(0, PARSED_INSTRUCTION_KIND_BINOP)
        encode_indexed_module_file(str(path), module)
    finally:
        kernel.close_native_tables()
    decoded = None
    try:
        with pytest.raises(BackendUnavailable, match="seed kind lane is inconsistent"):
            decoded = decode_indexed_module_file(str(path))
            emit_indexed_assembly(decoded)
    finally:
        if decoded is not None:
            for function in decoded.functions:
                if function.indexed_kernel is not None:
                    function.indexed_kernel.close_native_tables()
