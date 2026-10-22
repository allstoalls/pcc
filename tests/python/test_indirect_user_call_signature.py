"""Host frontend checks for declared and typed-indirect user-call contracts.

The probes delegate to the real emitter and inspect owned IR and publication
records. They build no native object or runtime and do not qualify pcc1 or GC
execution.
"""
from __future__ import annotations

import re

import pytest

from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_ast import NoneType
from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.type_infer import infer_module
from pcc.ir.compat import ir
from tests.python.test_shared_call_binding import _emit


def _emit_clean(source):
    text = _emit(source)
    assert "strict.nolib.stub" not in text
    assert not re.search(r"\bcall [^\n]*@py_cpy_", text)
    return text


@pytest.fixture(autouse=True)
def _text_ir(monkeypatch):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "0")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "0")


def _capture_calls(
    monkeypatch, *, caller="user_binding_probe",
    declaration="user_binding_produce", indirect=False, omit_metadata=False,
):
    """Observe the real boundary, optionally loading its declaration via a slot.

    The loaded callee is an actual ir.Value with a FunctionType pointee and an
    emitted SSA definition. No function_type/name attribute is added to Value,
    and result publication and error handling keep their production emitters.
    """
    original_call = L1CodeGen._call_user
    original_publish = L1CodeGen._slot_call_note_published
    original_error_check = L1CodeGen._emit_post_call_err_check
    records = []
    active = []

    def call_user(self, fn, args_ir, call_name, *args, **kwargs):
        selected = self.current_function.name == caller and (
            (declaration is None and not isinstance(fn, ir.Function))
            or (isinstance(fn, ir.Function) and fn.name == declaration)
        )
        if not selected:
            return original_call(self, fn, args_ir, call_name, *args, **kwargs)
        declared = fn if isinstance(fn, ir.Function) else None
        if indirect:
            assert declared is not None
            slot = self._alloca_in_entry(
                declared.type, name=self._fresh("signature.callee.slot"),
            )
            self.builder.store(declared, slot)
            fn = self.builder.load(slot, name=self._fresh("signature.callee"))
            assert fn.type.pointee is declared.function_type
        if omit_metadata:
            kwargs.pop("aggregate_result_ty", None)
        record = {
            "codegen": self, "caller": self.current_function,
            "declaration": declared, "callee": fn,
            "options": dict(kwargs), "events": [],
        }
        records.append(record)
        active.append(record)
        try:
            result = original_call(self, fn, args_ir, call_name, *args, **kwargs)
        finally:
            active.pop()
        sites = [
            (block, instruction)
            for block in record["caller"].blocks
            for instruction in block._instrs
            if instruction.opname == "call"
            and " " + str(fn) + "(" in instruction.text
        ]
        assert len(sites) == 1, sites
        record["call_block"], record["call"] = sites[0]
        record["result"] = result
        source = self._valueclass_payload_source(result)
        record["payload_source"] = source
        record["owned_roots"] = (
            () if source is None
            else self._valueclass_payload_owned_roots(source[1])
        )
        return result

    def note_published(self, slot):
        original_publish(self, slot)
        if active and active[-1]["codegen"] is self:
            active[-1]["events"].append(("published", slot))

    def check_error(self, *args, **kwargs):
        if active and active[-1]["codegen"] is self:
            active[-1]["events"].append(("error-check", None))
        return original_error_check(self, *args, **kwargs)

    monkeypatch.setattr(L1CodeGen, "_call_user", call_user)
    monkeypatch.setattr(L1CodeGen, "_slot_call_note_published", note_published)
    monkeypatch.setattr(L1CodeGen, "_emit_post_call_err_check", check_error)
    return records


def _single_boundary(records):
    assert len(records) == 1, records
    return records[0]


def _instruction_after_call(record):
    instructions = record["call_block"]._instrs
    index = instructions.index(record["call"])
    assert index + 1 < len(instructions)
    return instructions[index + 1].text


def test_super_init_keyword_only_argument_uses_real_indirect_signature(monkeypatch):
    records = _capture_calls(
        monkeypatch, caller="user_binding_Child___init__", declaration=None,
    )
    _emit_clean("""class Base:
    def __init__(self, *, loop=None) -> None:
        self.loop = loop
class Child(Base):
    def __init__(self, coro, *, loop=None) -> None:
        super().__init__(loop=loop)
        self.coro = coro
def probe():
    return Child(7, loop=42)
""")
    record = _single_boundary(records)
    callee = record["callee"]
    declared = record["codegen"].class_lowering.classes["Base"].methods["__init__"]
    assert type(callee) is ir.Value
    assert not hasattr(callee, "function_type") and not hasattr(callee, "name")
    assert callee.type is declared.type
    assert callee.type.pointee is declared.function_type
    assert isinstance(callee.type.pointee.return_type, ir.VoidType)
    assert len(callee.type.pointee.args) == 2
    assert isinstance(record["options"]["aggregate_result_ty"], NoneType)
    assert "call void " in record["call"].text
    assert any(event == "error-check" for event, _ in record["events"])


def test_typed_indirect_void_call_can_omit_aggregate_metadata(monkeypatch):
    records = _capture_calls(monkeypatch, indirect=True, omit_metadata=True)
    _emit_clean("""def produce(value) -> None:
    pass
def probe(value) -> None:
    produce(value)
""")
    record = _single_boundary(records)
    assert type(record["callee"]) is ir.Value
    assert "aggregate_result_ty" not in record["options"]
    assert isinstance(record["result"].type, ir.VoidType)
    assert record["payload_source"] is None
    assert "call void " in record["call"].text
    assert any(event == "error-check" for event, _ in record["events"])


def test_indirect_pointer_result_is_published_before_checks_and_cleanup(monkeypatch):
    records = _capture_calls(monkeypatch, indirect=True)
    _emit_clean("""def produce(value):
    return value
def probe(consumer, value):
    return consumer(value=produce([value]))
""")
    record = _single_boundary(records)
    output = record["options"]["result_slot"]
    pinned = record["options"]["pinned_arg_temps"]
    assert output is not None and pinned
    assert any(owned for _value, owned in pinned)
    assert isinstance(record["result"].type, ir.PointerType)
    returned = record["call"].text.split(" = ", 1)[0]
    assert _instruction_after_call(record) == (
        "store ptr " + returned + ", ptr " + str(output)
    )
    assert record["events"][0] == ("published", output)
    assert any(event == "error-check" for event, _ in record["events"][1:])
    cleanups = [
        block for block in record["caller"].blocks
        if block.name.startswith("call.publication.arguments.cleanup")
    ]
    assert len(cleanups) == 1
    cleanup = cleanups[0]
    assert sum("@pcc_gc_unpin(" in item.text for item in cleanup._instrs) == len(pinned)
    assert any(
        "label %" + cleanup.name in item.text
        for block in record["caller"].blocks if block is not cleanup
        for item in block._instrs
    )


@pytest.mark.parametrize(
    "indirect", (False, True), ids=("declared-metadata", "explicit-metadata"),
)
def test_managed_aggregate_keeps_its_producer_owned_payload(monkeypatch, indirect):
    records = _capture_calls(
        monkeypatch, indirect=indirect, omit_metadata=not indirect,
    )
    _emit_clean("""def valueclass(cls):
    return cls
@valueclass
class Packet:
    label: str
    number: int
def produce(value: Packet) -> Packet:
    return value
def probe(value: Packet) -> Packet:
    return produce(value)
""")
    record = _single_boundary(records)
    declared = record["declaration"]
    semantic_type = record["codegen"]._native_symbol_funcdefs[declared.name].return_ty
    if indirect:
        assert record["options"]["aggregate_result_ty"] is semantic_type
    else:
        assert "aggregate_result_ty" not in record["options"]
    assert isinstance(declared.function_type.return_type, ir.LiteralStructType)
    source = record["payload_source"]
    assert source is not None and source[0] is record["result"]
    assert source[3] is semantic_type
    assert len(record["owned_roots"]) == 1
    returned = record["call"].text.split(" = ", 1)[0]
    assert _instruction_after_call(record) == (
        "store " + str(declared.function_type.return_type) + " " + returned
        + ", ptr " + str(source[1])
    )
    publications = [slot for event, slot in record["events"] if event == "published"]
    assert tuple(publications) == record["owned_roots"]
    assert record["events"][0][0] == "published"
    assert any(event == "error-check" for event, _ in record["events"][1:])


@pytest.mark.parametrize("kind", (
    "non-pointer", "opaque-pointer", "missing-pointee",
    "bare-function", "malformed-declaration",
))
def test_invalid_signature_fails_before_emitting_a_call(kind):
    module = infer_module(parse_and_lift("", "signature.py", "signature"))
    codegen = L1CodeGen(module, ir_scaffold_mode="on")
    caller = ir.Function(codegen.module, ir.FunctionType(ir.VoidType(), []), "caller")
    codegen.current_function = caller
    codegen.builder = ir.IRBuilder(caller.append_basic_block("entry"))
    if kind == "malformed-declaration":
        callee = ir.Function(codegen.module, ir.FunctionType(ir.VoidType(), []), "malformed")
        callee.function_type = ir.IntType(8)
    else:
        types = {
            "non-pointer": ir.IntType(64),
            "opaque-pointer": ir.PointerType(ir.IntType(8)),
            "missing-pointee": ir.PointerType(None),
            "bare-function": ir.FunctionType(ir.VoidType(), []),
        }
        callee = ir.Value(types[kind], "%invalid")
    before = str(codegen.module)
    with pytest.raises(TypeError, match="^user call requires a declared function signature$"):
        codegen._call_user(callee, [ir.Constant(ir.IntType(64), 7)], "")
    assert str(codegen.module) == before
    assert caller.blocks[0]._instrs == []
