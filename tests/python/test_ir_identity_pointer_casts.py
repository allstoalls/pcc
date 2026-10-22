"""A pointer bitcast whose IR spelling is unchanged re-types its operand.

Under opaque pointers ``bitcast ptr %x to ptr`` only changes the builder's
tracked pointee.  It was ~20% of the instructions (and IR bytes) of a
multi-module pcc1 compile, and the x86_64 backend lowered each one to a
stack-slot load and store.  The builder now returns ``%x`` carrying the new
tracked type instead of emitting the instruction.
"""

from __future__ import annotations

import pytest

from pcc.backend.self_backend_verify import verify_parsed_module
from pcc.ir import ir


I8 = ir.IntType(8)
I64 = ir.IntType(64)
PTR = ir.PointerType(I8)
SLOT = ir.PointerType(PTR)


def _module(monkeypatch, *, direct: bool, triple: str = "aarch64-unknown-linux-gnu"):
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "1" if direct else "0")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "1" if direct else "0")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_REQUIRE_ZERO_FALLBACK", "1")
    module = ir.Module(name="identity-pointer-casts")
    module.triple = triple
    sink = ir.Function(module, ir.FunctionType(ir.VoidType(), [PTR]), name="sink")
    function = ir.Function(module, ir.FunctionType(I64, [PTR]), name="user")
    builder = ir.IRBuilder(function.append_basic_block("entry"))
    slot = builder.alloca(PTR, name="slot")
    builder.store(function.args[0], slot)
    alias = builder.bitcast(slot, PTR, name="gc.ptr")
    builder.call(sink, [alias])
    widened = builder.bitcast(builder.ptrtoint(alias, I64, name="bits"), ir.DoubleType(), name="real")
    builder.ret(builder.bitcast(widened, I64, name="back"))
    return module, slot, alias


def test_identity_pointer_cast_reuses_its_operand_with_the_new_tracked_type(monkeypatch):
    module, slot, alias = _module(monkeypatch, direct=False)
    assert str(alias) == str(slot)
    assert alias.type is PTR
    text = str(module)
    assert "bitcast ptr" not in text
    assert "@sink(ptr " + str(slot) + ")" in text
    # Casts that change the IR spelling are still instructions.
    assert "bitcast i64 " in text and "bitcast double " in text


def test_function_pointer_cast_stays_an_instruction(monkeypatch):
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "0")
    module = ir.Module(name="function-pointer-cast")
    callee = ir.Function(module, ir.FunctionType(ir.VoidType(), []), name="callee")
    function = ir.Function(module, ir.FunctionType(PTR, []), name="user")
    builder = ir.IRBuilder(function.append_basic_block("entry"))
    builder.ret(builder.bitcast(callee, PTR, name="fn"))
    assert "bitcast ptr @callee to ptr" in str(module)


def test_direct_capture_resolves_the_reused_operand_to_its_source(monkeypatch):
    module, slot, alias = _module(monkeypatch, direct=True)
    records = [record for block in module.functions[1].blocks for record in block._instrs]
    assert [record.opname for record in records].count("bitcast") == 2
    direct = module.direct_indexed_module()
    assert module._direct_indexed_fallback_records == 0
    verify_parsed_module(direct)
    assert alias._direct_value_id == slot._direct_value_id >= 0


@pytest.mark.parametrize("triple", ("aarch64-unknown-linux-gnu", "x86_64-unknown-linux-gnu"))
@pytest.mark.parametrize("direct", (False, True))
def test_both_planes_emit_assembly_without_the_identity_cast(monkeypatch, direct, triple):
    from pcc.backend.self_backend_parse import parse_self_backend_module
    from pcc.backend.target_objects import emit_indexed_assembly

    module, _slot, _alias = _module(monkeypatch, direct=direct, triple=triple)
    parsed = module.direct_indexed_module() if direct else parse_self_backend_module(str(module))
    verify_parsed_module(parsed)
    assert "sink" in emit_indexed_assembly(parsed, optimize=False)
