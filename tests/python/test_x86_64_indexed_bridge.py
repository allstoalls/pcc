"""x86 selection streams indexed payloads without rebuilding instruction arenas."""

from dataclasses import replace

import pytest

from pcc.backend import BackendUnavailable
from pcc.backend import self_backend_x86_64_linux as emitter
from pcc.backend.self_backend_indexed_codec import (
    decode_indexed_module_file,
    encode_indexed_module_file,
)
from pcc.backend.self_backend_ir import (
    CompactParsedInstrArena,
    CompactParsedInstrView,
    ParsedFunction,
    _OPERAND_INTERN,
)
from pcc.backend.self_backend_kernel import (
    IndexedFunctionKernel,
    get_indexed_function_kernel,
)
from pcc.backend.self_backend_parse import parse_self_backend_module
from pcc.backend.target_objects import emit_indexed_assembly
from pcc.ir import ir
from tests.python.test_x86_64_emission_lifetime import (
    PROGRAM,
    ROOTED_PROGRAM,
    TARGET,
    _emit,
    _indexed_module,
    _legacy_module,
)


@pytest.fixture(autouse=True)
def _fixed_emission_configuration(monkeypatch):
    monkeypatch.delenv("PCC_CODE_PROFILE", raising=False)
    monkeypatch.setenv("PCC_SELF_TARGET_PASSES", "none")
    monkeypatch.setenv("PCC_SELF_TARGET_PASS_TRANSPORT", "text")


@pytest.mark.parametrize("source", [PROGRAM, ROOTED_PROGRAM], ids=["phi-call", "rooted"])
@pytest.mark.parametrize("packed", [False, True], ids=["symbolic", "packed"])
def test_owned_indexed_emit_never_rebuilds_instruction_arenas(
    tmp_path, monkeypatch, source, packed,
):
    expected = _emit(_legacy_module(tmp_path, source), packed)
    module = _indexed_module(tmp_path, source)
    original_emit = emitter._emit_function
    seen = []

    def forbidden(*args, **kwargs):
        raise AssertionError("x86 indexed emission rebuilt an instruction arena/view")

    def traced_emit(function, plan, *, phase_timing=None):
        kernel = get_indexed_function_kernel(function)
        before = kernel.instruction_arena_diagnostic_projections
        with monkeypatch.context() as guard:
            guard.setattr(CompactParsedInstrArena, "append", forbidden)
            guard.setattr(CompactParsedInstrView, "__init__", forbidden)
            guard.setattr(IndexedFunctionKernel, "diagnostic_instruction", forbidden)
            lines = original_emit(function, plan, phase_timing=phase_timing)
        assert kernel.instruction_arena_diagnostic_projections == before
        assert not function.blocks and not function.block_map
        seen.append(function.name)
        return lines

    monkeypatch.setattr(emitter, "_emit_function", traced_emit)
    assert _emit(module, packed) == expected
    assert seen == [function.name for function in module.functions]


def _captured_module(monkeypatch, direct):
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1" if direct else "0")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "0")
    module = ir.Module(name="x86-indexed-bridge")
    module.triple = TARGET
    i32 = ir.IntType(32)
    function = ir.Function(module, ir.FunctionType(i32, []), "main")
    builder = ir.IRBuilder(function.append_basic_block("entry"))
    slot = builder.alloca(i32, name="slot")
    builder.store(ir.Constant(i32, 40), slot)
    value = builder.load(slot, name="loaded")
    result = builder.add(value, ir.Constant(i32, 2), name="result")
    builder.ret(result)
    indexed = module.direct_indexed_module()
    assert bool(module._direct_indexed_fallback_records) is (not direct)
    assert all(not function.blocks for function in indexed.functions)
    return indexed


@pytest.mark.parametrize("direct", [False, True], ids=["capture-fallback", "direct-capture"])
def test_direct_and_nonzero_capture_fallback_keep_legacy_bytes(monkeypatch, direct):
    legacy = _captured_module(monkeypatch, direct)
    for function in legacy.functions:
        get_indexed_function_kernel(function).materialize_legacy_blocks(function)
    expected = _emit(legacy, True)
    actual = _emit(_captured_module(monkeypatch, direct), True)
    assert actual == expected


MIXED = '''target triple = "x86_64-unknown-linux-gnu"
define i32 @mixed(i32* %address, i32 %value) {
entry:
  %slot = alloca i32
  store volatile i32 %value, i32* %slot
  %loaded = load volatile i32, i32* %slot
  %sum = add nsw i32 %loaded, 2
  %compare = icmp sgt i32 %sum, 0
  %selected = select i1 %compare, i32 %sum, i32 7
  %wide = zext i32 %selected to i64
  %small = trunc i64 %wide to i32
  %next = getelementptr i32, i32* %address, i64 1
  store atomic i32 %small, i32* %next seq_cst, align 4
  %observed = load atomic i32, i32* %next acquire, align 4
  %frozen = freeze i32 %observed
  ret i32 %frozen
}
'''


def test_cold_atomic_volatile_flagged_and_fixed_payloads_keep_bytes(tmp_path):
    module = _indexed_module(tmp_path, MIXED)
    kernel = get_indexed_function_kernel(module.functions[0])
    assert kernel.cold_instruction_data
    assert kernel.instruction_arithmetic_flag_values
    assert any(kernel.instruction_is_volatile(0, index)
               for index in range(kernel.instruction_count(0)))
    expected = _emit(_legacy_module(tmp_path, MIXED), True)
    assert _emit(module, True) == expected
    assert "xchg DWORD PTR" in expected[0]


def test_legacy_atomic_cold_payloads_keep_bytes(tmp_path):
    def legacy_atomic_module():
        module = _indexed_module(tmp_path, MIXED)
        kernel = get_indexed_function_kernel(module.functions[0])
        for index in range(kernel.instruction_count(0)):
            instruction = kernel.diagnostic_instruction(0, index)
            if instruction.kind in ("load_atomic", "store_atomic"):
                cold_id = len(kernel.cold_instruction_data)
                kernel.cold_instruction_data.append(instruction.data[:5])
                kernel.instruction_metadata.set_unchecked(index * 4 + 1, -cold_id - 1)
        return module

    legacy = legacy_atomic_module()
    for function in legacy.functions:
        get_indexed_function_kernel(function).materialize_legacy_blocks(function)
    assert _emit(legacy_atomic_module(), True) == _emit(legacy, True)


def test_selector_decodes_negative_ids_in_the_fixed_vocabulary(tmp_path, monkeypatch):
    expected = _emit(_legacy_module(tmp_path, MIXED), True)
    original_emit = emitter._emit_function

    def with_cold_payloads(function, plan, *, phase_timing=None):
        # Exercise the shared selector callback's int/tuple protocol after
        # preparation: the verifier requires packed records for these kinds.
        kernel = get_indexed_function_kernel(function)
        for index in range(kernel.instruction_count(0)):
            instruction = kernel.diagnostic_instruction(0, index)
            if instruction.kind in ("alloca", "load", "store", "binop", "gep"):
                cold_id = len(kernel.cold_instruction_data)
                kernel.cold_instruction_data.append(instruction.data)
                kernel.instruction_metadata.set_unchecked(index * 4 + 1, -cold_id - 1)
        return original_emit(function, plan, phase_timing=phase_timing)

    monkeypatch.setattr(emitter, "_emit_function", with_cold_payloads)
    assert _emit(_indexed_module(tmp_path, MIXED), True) == expected


@pytest.mark.parametrize("malformed_tail", [False, True], ids=["selector-error", "verify-error-first"])
def test_unsupported_instruction_and_verification_diagnostics_are_unchanged(malformed_tail):
    source = '''target triple = "x86_64-unknown-linux-gnu"
define i32 @unsupported(<2 x i32> %vector) {
entry:
  %value = extractelement <2 x i32> %vector, i32 0
  TAIL
  ret i32 %value
}
'''.replace("  TAIL", "  %bad = add i32 %undefined, 1" if malformed_tail else "")
    errors = []
    for legacy in (True, False):
        module = parse_self_backend_module(source)
        if legacy:
            for function in module.functions:
                get_indexed_function_kernel(function).materialize_legacy_blocks(function)
        try:
            with pytest.raises(BackendUnavailable) as caught:
                emit_indexed_assembly(module)
            errors.append(str(caught.value))
        finally:
            for function in module.functions:
                get_indexed_function_kernel(function).close_native_tables()
    assert errors[0] == errors[1]
    assert "undefined" in errors[0] if malformed_tail else "extractelement" in errors[0]
    assert "unsupported" in errors[0]


@pytest.mark.parametrize("corruption", ["missing-flags", "opcode-text-id"])
@pytest.mark.parametrize("early_unsupported", [False, True], ids=["single-error", "multiple-errors"])
def test_corrupt_indexed_wire_is_rejected_and_restores_state(
    tmp_path, monkeypatch, corruption, early_unsupported,
):
    source = '''target triple = "x86_64-unknown-linux-gnu"
define i32 @probe(ARGS) {
entry:
  EXTRACT
  %sum = add nsw i32 VALUE, 2
  ret i32 %sum
}
'''.replace("ARGS", "<2 x i32> %vector" if early_unsupported else "")
    source = source.replace(
        "EXTRACT",
        "%value = extractelement <2 x i32> %vector, i32 0" if early_unsupported else "",
    ).replace("VALUE", "%value" if early_unsupported else "40")
    source_module = parse_self_backend_module(source)
    wire = tmp_path / "corrupt-input.pidx"
    try:
        kernel = get_indexed_function_kernel(source_module.functions[0])
        instruction_index = int(early_unsupported)
        if corruption == "missing-flags":
            kernel.instruction_arithmetic_flag_values.clear()
        else:
            record_id = kernel.instruction_record_id(0, instruction_index)
            kernel.instruction_record_scalars.set_unchecked(
                record_id * 4, len(kernel.call_texts) + 7,
            )
        encode_indexed_module_file(str(wire), source_module)
    finally:
        for function in source_module.functions:
            get_indexed_function_kernel(function).close_native_tables()
    # The damage crosses the real worker codec, not an emission monkeypatch.
    module = decode_indexed_module_file(str(wire))
    observed = []
    original_emit = emitter._emit_function

    def traced_emit(function, plan, *, phase_timing=None):
        blocks, block_map = function.blocks, function.block_map
        try:
            return original_emit(function, plan, phase_timing=phase_timing)
        finally:
            assert function.blocks is blocks and not blocks
            assert function.block_map is block_map and not block_map
            observed.append(function.name)

    monkeypatch.setattr(emitter, "_emit_function", traced_emit)
    expected_error = (
        BackendUnavailable if early_unsupported
        else KeyError if corruption == "missing-flags" else IndexError
    )
    assembly = None
    try:
        with pytest.raises(expected_error) as caught:
            assembly = emit_indexed_assembly(module)
        assert type(caught.value) is expected_error
        if early_unsupported:
            # Streaming may reject an earlier unsupported selector before a
            # later corrupt payload that eager legacy projection saw first.
            # Multiple-invalid-input first-error ordering is not a contract.
            assert "'probe'/'entry': extractelement" in str(caught.value)
        elif corruption == "missing-flags":
            assert caught.value.args == (0,)
        else:
            assert str(caught.value) == "list index out of range"
        assert assembly is None
        assert observed == ["probe"]
        assert not emitter._X86_EMISSION_ACTIVE and not emitter._WINDOWS_ABI
    finally:
        for function in module.functions:
            get_indexed_function_kernel(function).close_native_tables()


def test_partial_shell_projection_failure_restores_original_containers(tmp_path, monkeypatch):
    module = _indexed_module(tmp_path)
    function = module.functions[0]
    entry_state = []
    failure = BackendUnavailable("injected partial control-flow projection")
    original_emit = emitter._emit_function
    original_block = IndexedFunctionKernel.diagnostic_block

    def traced_emit(function_arg, plan, *, phase_timing=None):
        assert function_arg is function
        entry_state.append((function_arg.blocks, function_arg.block_map))
        return original_emit(function_arg, plan, phase_timing=phase_timing)

    def fail_after_first_block(kernel, block_id, include_instructions=True):
        if block_id == 1:
            raise failure
        assert not include_instructions
        return original_block(kernel, block_id, include_instructions)

    monkeypatch.setattr(emitter, "_emit_function", traced_emit)
    monkeypatch.setattr(IndexedFunctionKernel, "diagnostic_block", fail_after_first_block)
    try:
        with pytest.raises(BackendUnavailable) as caught:
            emit_indexed_assembly(module)
        assert caught.value is failure
        assert len(entry_state) == 1
        blocks, block_map = entry_state[0]
        assert function.blocks is blocks and not blocks
        assert function.block_map is block_map and not block_map
        assert not emitter._X86_EMISSION_ACTIVE and not emitter._WINDOWS_ABI
    finally:
        for function in module.functions:
            get_indexed_function_kernel(function).close_native_tables()


@pytest.mark.parametrize("owner", ["function", "kernel", "blocks", "block-map"])
def test_nonexact_owners_retain_the_legacy_bridge(tmp_path, monkeypatch, owner):
    class CustomFunction(ParsedFunction):
        pass

    class CustomKernel(IndexedFunctionKernel):
        __slots__ = ()

    class CustomList(list):
        pass

    class CustomDict(dict):
        pass

    expected = _emit(_legacy_module(tmp_path), False)
    module = _indexed_module(tmp_path)
    for function in module.functions:
        if owner == "function":
            function.__class__ = CustomFunction
        elif owner == "kernel":
            function.indexed_kernel.__class__ = CustomKernel
        elif owner == "blocks":
            function.blocks = CustomList()

    original_emit = emitter._emit_function

    def with_custom_map(function, plan, *, phase_timing=None):
        # Preparation legitimately rebuilds block_map. Exercise this boundary
        # with the actual owner that _emit_function receives.
        function.block_map = CustomDict()
        return original_emit(function, plan, phase_timing=phase_timing)

    def forbidden(*args, **kwargs):
        raise AssertionError("nonexact owner used the indexed shell bridge")

    monkeypatch.setattr(emitter, "_materialize_indexed_control_flow", forbidden)
    if owner == "block-map":
        monkeypatch.setattr(emitter, "_emit_function", with_custom_map)
    assert _emit(module, False) == expected


def test_instruction_payload_interning_is_preserved(tmp_path, monkeypatch):
    module = _indexed_module(tmp_path)
    original_dispatch = emitter.emit_instruction_dispatch_parts
    observed = []

    def checked_dispatch(function, block, kind, data, **kwargs):
        for value in data:
            if type(value) is str:
                assert _OPERAND_INTERN[value] is value
                observed.append(value)
        return original_dispatch(function, block, kind, data, **kwargs)

    monkeypatch.setattr(emitter, "emit_instruction_dispatch_parts", checked_dispatch)
    assert emit_indexed_assembly(module)
    assert observed


def test_selector_failure_keeps_context_and_original_containers(tmp_path, monkeypatch):
    module = _indexed_module(tmp_path)
    function = module.functions[1]
    module = replace(module, functions=(function,))
    entry_state = []
    failure = BackendUnavailable("injected indexed selector failure")
    original_emit = emitter._emit_function

    def traced_emit(function_arg, plan, *, phase_timing=None):
        entry_state.append((function_arg.blocks, function_arg.block_map))
        return original_emit(function_arg, plan, phase_timing=phase_timing)

    def fail(function_arg, block, kind, data, **kwargs):
        assert function_arg is function
        assert block.name == "entry" and kind == "binop"
        assert not block.instructions
        raise failure

    monkeypatch.setattr(emitter, "emit_instruction_dispatch_parts", fail)
    monkeypatch.setattr(emitter, "_emit_function", traced_emit)
    try:
        with pytest.raises(BackendUnavailable) as caught:
            emit_indexed_assembly(module)
        assert caught.value is failure
        assert len(entry_state) == 1
        blocks, block_map = entry_state[0]
        assert function.blocks is blocks and not blocks
        assert function.block_map is block_map and not block_map
        assert not emitter._X86_EMISSION_ACTIVE and not emitter._WINDOWS_ABI
    finally:
        get_indexed_function_kernel(function).close_native_tables()
