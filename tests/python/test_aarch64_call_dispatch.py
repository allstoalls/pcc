"""Calls bypass unrelated kind cases without bypassing destination checks."""

import pytest

from pcc.backend import self_backend_aarch64_darwin_compute as compute
from pcc.backend import self_backend_aarch64_darwin_memory as memory
from pcc.backend.self_backend_ir import (
    PARSED_INSTRUCTION_KIND_ALLOCA,
    PARSED_INSTRUCTION_KIND_CALL,
)
from pcc.backend.self_backend_value_arena import CompilerInt4


class _CountedKind(int):
    def __new__(cls, value):
        result = super().__new__(cls, value)
        result.comparisons = []
        return result

    def __eq__(self, other):
        self.comparisons.append(other)
        return int.__eq__(self, other)


class _Kernel:
    def __init__(self, callee="callee", flags=0, slot_error=False):
        self.call_texts = [callee]
        self.flags = flags
        self.slot_error = slot_error
        self.events = []
        self.data = ("%result", "ret-type", callee, bool(flags & 1), (), 0, False, ())

    def defined_value_id(self, block, instruction):
        self.events.append(("defined", block, instruction))
        return 7

    def value_slot_id(self, value):
        self.events.append(("slot", value))
        if self.slot_error:
            raise IndexError("invalid destination slot")
        return 2

    def call_header(self, call):
        self.events.append(("header", call))
        return CompilerInt4(0, 0, self.flags, 0)

    def diagnostic_call_data(self, call):
        self.events.append(("diagnostic", call))
        return self.data


def test_memory_call_retains_slot_check_without_memory_kind_cascade():
    kind = _CountedKind(PARSED_INSTRUCTION_KIND_CALL)
    kernel = _Kernel()
    assert memory.emit_memory_instruction_by_id(
        object(), kind, 3, object(), indexed_kernel=kernel, indexed_dest_id=7,
    ) is None
    assert kernel.events == [("slot", 7)]
    # The syscall6 check still precedes the destination lookup.
    assert len(kind.comparisons) == 2
    assert kind.comparisons[-1] == PARSED_INSTRUCTION_KIND_CALL


@pytest.mark.parametrize("callee, flags, intrinsic", [
    ("callee", 0, False),
    ("llvm.named_indirect", 1, False),
    ("llvm.bswap.i64", 0, True),
])
def test_compute_call_preserves_dispatch_and_arguments(monkeypatch, callee, flags, intrinsic):
    kind = _CountedKind(PARSED_INSTRUCTION_KIND_CALL)
    kernel = _Kernel(callee, flags)
    func, symbols = object(), object()
    received = []

    def indexed(*args):
        received.append(("indexed", args, {}))
        return ["indexed-call"]

    def ordinary(*args, **kwargs):
        received.append(("ordinary", args, kwargs))
        return ["intrinsic-call"]

    monkeypatch.setattr(compute, "emit_call_instruction_indexed", indexed)
    monkeypatch.setattr(compute, "emit_call_instruction", ordinary)
    result = compute.emit_compute_instruction_by_id(
        func, kind, 3, symbols, indexed_kernel=kernel,
        block_id=2, instruction_index=4, indexed_use_count=5,
        indexed_use0=6, indexed_use_tail=8,
    )
    assert len(kind.comparisons) == 1
    assert kernel.events[:3] == [("defined", 2, 4), ("slot", 7), ("header", 3)]
    if intrinsic:
        assert result == ["intrinsic-call"]
        assert kernel.events[3:] == [("diagnostic", 3)]
        assert received == [("ordinary", (func, *kernel.data, symbols), {
            "dest_value_id": 7, "indexed_use_count": 5,
            "indexed_use0": 6, "indexed_use_tail": 8,
        })]
    else:
        assert result == ["indexed-call"]
        assert kernel.events[3:] == []
        assert received == [("indexed", (func, kernel, 3, symbols), {})]


def test_legacy_call_keeps_ordinary_fallback(monkeypatch):
    kernel = _Kernel()
    func, symbols = object(), object()
    received = []

    def ordinary(*args, **kwargs):
        received.append((args, kwargs))
        return ["legacy-call"]

    monkeypatch.setattr(compute, "emit_call_instruction", ordinary)
    assert compute.emit_compute_instruction_by_id(
        func, PARSED_INSTRUCTION_KIND_CALL, kernel.data, symbols,
    ) == ["legacy-call"]
    assert received == [((func, *kernel.data, symbols), {
        "dest_value_id": -1, "indexed_use_count": -1,
        "indexed_use0": -1, "indexed_use_tail": -1,
    })]


@pytest.mark.parametrize("dispatch", [
    memory.emit_memory_instruction_by_id, compute.emit_compute_instruction_by_id,
])
def test_call_does_not_bypass_invalid_destination_lookup(dispatch):
    kernel = _Kernel(slot_error=True)
    with pytest.raises(IndexError, match="invalid destination slot"):
        dispatch(object(), PARSED_INSTRUCTION_KIND_CALL, 3, object(),
                 indexed_kernel=kernel, indexed_dest_id=7)
    assert kernel.events == [("slot", 7)]


def test_non_call_memory_and_unknown_compute_routes_are_retained():
    kernel = _Kernel()
    assert memory.emit_memory_instruction_by_id(
        object(), PARSED_INSTRUCTION_KIND_ALLOCA, (), object(),
        indexed_kernel=kernel, indexed_dest_id=7,
    ) == []
    assert compute.emit_compute_instruction_by_id(
        object(), -999, (), object(), indexed_kernel=kernel, indexed_dest_id=7,
    ) is None
    assert kernel.events == [("slot", 7), ("slot", 7)]


@pytest.mark.parametrize("dispatch", [
    memory.emit_memory_instruction_by_id, compute.emit_compute_instruction_by_id,
])
def test_real_out_of_range_destination_still_fails_before_call(dispatch):
    from pcc.backend.self_backend_kernel import get_indexed_function_kernel
    from pcc.backend.self_backend_parse import parse_self_backend_module

    module = parse_self_backend_module(
        'target triple = "arm64-apple-darwin"\n'
        "define void @probe() {\nentry:\n ret void\n}\n"
    )
    function = module.functions[0]
    kernel = get_indexed_function_kernel(function)
    try:
        with pytest.raises(IndexError):
            dispatch(function, PARSED_INSTRUCTION_KIND_CALL, -1, object(),
                     indexed_kernel=kernel, indexed_dest_id=len(kernel.value_names) + 1)
    finally:
        kernel.close_native_tables()


def test_second_destination_lookup_keeps_its_failure_order():
    kernel = _Kernel()
    assert memory.emit_memory_instruction_by_id(
        object(), PARSED_INSTRUCTION_KIND_CALL, 3, object(),
        indexed_kernel=kernel, indexed_dest_id=7,
    ) is None
    kernel.slot_error = True
    with pytest.raises(IndexError, match="invalid destination slot"):
        compute.emit_compute_instruction_by_id(
            object(), PARSED_INSTRUCTION_KIND_CALL, 3, object(),
            indexed_kernel=kernel, indexed_dest_id=7,
        )
    assert kernel.events == [("slot", 7), ("slot", 7)]


@pytest.mark.parametrize("callee, args, fixed, variadic, message", [
    ("llvm.trap", (("type", "1"),), 1, False, "llvm.trap expects 0 args"),
    ("callee", (), 1, True, "fixed args exceed actual args"),
])
def test_malformed_call_diagnostics_are_retained(callee, args, fixed, variadic, message):
    from types import SimpleNamespace
    from pcc.backend import BackendUnavailable

    data = (None, "ret-type", callee, False, args, fixed, variadic, ())
    with pytest.raises(BackendUnavailable, match=message):
        compute.emit_compute_instruction_by_id(
            SimpleNamespace(name="probe"), PARSED_INSTRUCTION_KIND_CALL, data, object(),
        )


def test_non_call_darwin_syscall_rejection_still_precedes_slot_lookup():
    from types import SimpleNamespace
    from pcc.backend import BackendUnavailable
    from pcc.backend.self_backend_ir import PARSED_INSTRUCTION_KIND_SYSCALL6

    kernel = _Kernel(slot_error=True)
    with pytest.raises(BackendUnavailable, match="raw AArch64 syscall6 requires Linux"):
        memory.emit_memory_instruction_by_id(
            object(), PARSED_INSTRUCTION_KIND_SYSCALL6, (),
            SimpleNamespace(target_triple="arm64-apple-darwin"),
            indexed_kernel=kernel, indexed_dest_id=7,
        )
    assert kernel.events == []
