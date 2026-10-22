"""A payload field root follows its own alloca on the text-free direct route.

Direct-indexed workers emit records without text. The field-slot placement
must find the defining alloca by its record, not by a text prefix: a miss put
the root GEP before a later entry alloca and failed SSA dominance.
"""
from pcc.frontends.python.codegen.core_helpers import CoreHelperMixin
from pcc.frontends.python.codegen.type_abi_lowering import TypeAbiLoweringMixin
from pcc.ir import ir as pir
from pcc.ir.compat import ir

_CSTR = ir.IntType(8).as_pointer()


class _Placement(CoreHelperMixin, TypeAbiLoweringMixin):
    def __init__(self, function, entry, builder):
        self.current_function = function
        self._current_entry_block = entry
        self.builder = builder


def _direct_entry(monkeypatch):
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1")
    monkeypatch.delenv("PCC_DIRECT_INDEXED_KERNEL_VALIDATE", raising=False)
    monkeypatch.delenv("PCC_TEXT_INDEXED_KERNEL_EMIT", raising=False)
    module = ir.Module(name="probe")
    function = ir.Function(module, ir.FunctionType(ir.VoidType(), []), name="probe")
    entry = function.append_basic_block("entry")
    builder = ir.IRBuilder(entry)
    assert builder._direct_indexed_no_text
    return function, entry, builder


def test_record_defines_value_without_text(monkeypatch):
    function, entry, builder = _direct_entry(monkeypatch)
    first = builder.alloca(_CSTR, name="first")
    second = builder.alloca(_CSTR, name="second")
    placement = _Placement(function, entry, builder)
    records = list(entry._instrs)
    assert [record.text for record in records] == ["", ""]
    assert [placement._record_defines_value(record, second) for record in records] == [False, True]
    assert [placement._record_defines_value(record, first) for record in records] == [True, False]


def test_field_root_slot_follows_payload_alloca_after_earlier_root(monkeypatch):
    function, entry, builder = _direct_entry(monkeypatch)
    pair = ir.LiteralStructType([_CSTR, _CSTR])
    earlier = builder.alloca(pair, name="earlier")
    builder.gep(earlier, [ir.Constant(ir.IntType(32), 0), ir.Constant(ir.IntType(32), 0)], name="earlier.root")
    payload = builder.alloca(pair, name="payload")
    builder.ret_void()
    placement = _Placement(function, entry, builder)
    slot = placement._emit_entry_valueclass_payload_field_slot(payload, (1,), "payload.value.root.1")
    records = list(entry._instrs)
    payload_index = next(i for i, record in enumerate(records) if placement._record_defines_value(record, payload))
    slot_index = next(i for i, record in enumerate(records) if placement._record_defines_value(record, slot))
    assert slot_index == payload_index + 1
    assert isinstance(slot, pir.Value)
