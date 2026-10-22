"""Reuse an indexed call's target decision without changing emitted output."""

from __future__ import annotations

import pytest

from pcc.backend import BackendUnavailable
from pcc.backend import self_backend_aarch64_darwin_calls as calls
from pcc.backend import self_backend_aarch64_darwin_mem as memory
from pcc.backend.self_backend_aarch64_darwin_abi import aggregate_returned_indirect
from pcc.backend.self_backend_kernel import get_indexed_function_kernel
from pcc.backend.self_backend_prepare import prepare_module_for_target


_TARGETS = (
    "aarch64-unknown-linux-gnu",
    "arm64-unknown-linux-musl",
    "arm64-apple-darwin23.6.0",
)
_CASES = (
    ("fixed", 0), ("fixed", 12), ("fp", 8), ("fp", 9),
    ("vararg", 12), ("mixed", 12), ("register-align", 12),
    ("stack-align", 12), ("indirect", 12),
)


def _source(target, shape, count=12):
    args = [("i64", str(index + 1)) for index in range(count)]
    if shape == "fp":
        args = [("double", "0.000000e+00")] * count
    elif shape == "mixed":
        args += [("double", "0.000000e+00")] * 10
    elif shape == "register-align":
        args = args[:7] + [("[1 x i128]", "zeroinitializer"), ("i64", "99")]
    elif shape == "stack-align":
        args = args[:9] + [("[1 x i128]", "zeroinitializer"), ("i64", "99")]
    signature = ", ".join(arg_type for arg_type, _value in args)
    actuals = ", ".join(arg_type + " " + value for arg_type, value in args)
    if shape == "vararg":
        signature = "i64, ..."
        invocation = "void (i64, ...) @sink"
    else:
        invocation = "void " + ("%callee" if shape == "indirect" else "@sink")
    declaration = "" if shape == "indirect" else "declare void @sink(" + signature + ")\n"
    parameters = "ptr %callee" if shape == "indirect" else ""
    return (
        'target triple = "' + target + '"\n' + declaration
        + "define void @caller(" + parameters + ") {\nentry:\n"
        + "  call " + invocation + "(" + actuals + ")\n  ret void\n}\n"
    )


def _emit(source, *, indexed, capture=False, fault=None):
    prepared = prepare_module_for_target(
        source, aggregate_returned_indirect=aggregate_returned_indirect,
    )
    assert len(prepared.functions) == 1
    function = prepared.functions[0]
    kernel = get_indexed_function_kernel(function)
    capturing = False
    try:
        memory.require_direct_instruction_capture_idle()
        # Corrupt a real prepared call only to exercise the emitter's existing
        # early guards, without replacing the verifier or relaxing assertions.
        if fault == "intrinsic":
            header = kernel.call_header(0)
            kernel.call_texts[header.second] = "llvm.trap"
        elif fault == "vararg-overflow":
            header = kernel.call_header(0)
            span = kernel.call_span(0)
            kernel.call_scalars.set_unchecked(2, header.third | 2)
            kernel.call_scalars.set_unchecked(5, span.first + 1)
        if capture:
            memory.begin_direct_instruction_capture()
            capturing = True
        if indexed:
            lines = calls.emit_call_instruction_indexed(
                function, kernel, 0, prepared.module_symbols,
            )
        else:
            # This independently retained path still queries the target where
            # it chooses register layout and stack placement.
            lines = calls.emit_call_instruction(
                function, *kernel.diagnostic_call_data(0),
                module_symbols=prepared.module_symbols,
            )
        if not capture:
            return lines
        records = memory.borrow_direct_instruction_records()
        return (
            lines,
            [records.get_unchecked(index) for index in range(len(records))],
            list(memory.borrow_direct_instruction_symbol_names()),
        )
    finally:
        if capturing:
            memory.end_direct_instruction_capture()
        kernel.close_native_tables()


@pytest.mark.parametrize("target", _TARGETS)
@pytest.mark.parametrize("shape,count", _CASES)
@pytest.mark.parametrize("capture", (False, True), ids=("assembly", "records"))
def test_indexed_call_target_reuse_preserves_legacy_output(target, shape, count, capture):
    source = _source(target, shape, count)
    assert _emit(source, indexed=True, capture=capture) == _emit(
        source, indexed=False, capture=capture,
    )


@pytest.mark.parametrize("shape,count,expected_queries", (
    ("fixed", 0, 0), ("fp", 8, 0), ("fp", 9, 1),
    ("fixed", 1, 1), ("fixed", 8, 1), ("fixed", 9, 1),
    ("fixed", 32, 1), ("vararg", 12, 1),
))
@pytest.mark.parametrize("target", (_TARGETS[0], _TARGETS[2]))
def test_indexed_call_classifies_target_only_when_needed(
    monkeypatch, shape, count, expected_queries, target,
):
    classify = calls.is_aarch64_linux_triple
    queries = []

    def counted(triple):
        queries.append(triple)
        return classify(triple)

    source = _source(target, shape, count)
    expected = _emit(source, indexed=False)
    with monkeypatch.context() as patch:
        # The symbol mangler's independent imported classifier is not replaced.
        patch.setattr(calls, "is_aarch64_linux_triple", counted)
        actual = _emit(source, indexed=True)
    assert actual == expected
    assert queries == [target] * expected_queries


@pytest.mark.parametrize("fault,message", (
    ("intrinsic", "indexed regular-call lowering received an LLVM intrinsic"),
    ("vararg-overflow", "fixed args exceed actual args"),
))
@pytest.mark.parametrize("capture", (False, True))
def test_indexed_call_errors_precede_target_classification(monkeypatch, fault, message, capture):
    def forbidden(_triple):
        pytest.fail("target classification ran before an existing call error")

    monkeypatch.setattr(calls, "is_aarch64_linux_triple", forbidden)
    with pytest.raises(BackendUnavailable, match=message):
        _emit(_source(_TARGETS[0], "fixed"), indexed=True, capture=capture, fault=fault)
    assert not memory.direct_instruction_capture_active()


def test_indexed_call_target_decision_is_not_retained_between_calls():
    sequence = (_TARGETS[0], _TARGETS[2], _TARGETS[1], _TARGETS[2], _TARGETS[0])
    outputs = [_emit(_source(target, "vararg"), indexed=True) for target in sequence]
    assert outputs[0] == outputs[2] == outputs[4]
    assert outputs[1] == outputs[3]
    assert outputs[0] != outputs[1]
    # Twelve i64 arguments: Linux spills four, Darwin spills eleven variadic
    # arguments. The resulting 16-byte-aligned outgoing frames must differ.
    assert "  sub sp, sp, #32" in outputs[0]
    assert "  sub sp, sp, #96" in outputs[1]
    assert "  bl sink" in outputs[0]
    assert "  bl _sink" in outputs[1]


def test_indexed_call_preserves_linux_stack_alignment():
    linux = _emit(_source(_TARGETS[0], "stack-align"), indexed=True)
    darwin = _emit(_source(_TARGETS[2], "stack-align"), indexed=True)
    # After a single stack i64, the 16-byte-aligned aggregate starts at 16 on
    # Linux, not at Darwin's compact offset 8; the final i64 follows it.
    assert "  sub sp, sp, #48" in linux
    assert "  add x14, sp, #32" in linux
    assert "  sub sp, sp, #32" in darwin
    assert "  add x14, sp, #24" in darwin


@pytest.mark.parametrize("target", (_TARGETS[0], _TARGETS[2]))
@pytest.mark.parametrize("shape", ("vararg", "stack-align"))
def test_call_target_reuse_matches_assembly_and_native_sections(target, shape):
    from pcc.backend.arm64_asm_driver import assemble_file
    from pcc.backend.self_backend_aarch64_darwin import (
        emit_aarch64_darwin_indexed_module,
        emit_aarch64_darwin_indexed_transport,
    )
    from pcc.backend.self_backend_parse import parse_self_backend_module

    source = _source(target, shape)
    assembly = emit_aarch64_darwin_indexed_module(
        parse_self_backend_module(source), optimize=False,
    )
    transport = emit_aarch64_darwin_indexed_transport(
        parse_self_backend_module(source), optimize=False, structured_instructions=True,
    )
    assert transport.native_finalized
    assert transport.fallback_instruction_count == 0
    # Complete sections include instruction words, symbols, relocations and
    # stackmaps, not merely the human-readable call spelling.
    assert transport.assemble_sections() == assemble_file(assembly)
