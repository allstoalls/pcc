"""Whole valueclass Names cross dynamic call boundaries through owned slots."""
from __future__ import annotations

import re

import pytest

from pcc.frontends.python.codegen.errors import L1CodegenError
from tests.python.test_shared_call_binding import _emit, _function


PREFIX = '''import pcc
@pcc.valueclass
class Record:
    number: int
    value: FIELD

def take(*, value, **extras):
    return value
'''


@pytest.mark.parametrize("field", ("int", "float", "bool", "list"))
@pytest.mark.parametrize("site", ("argument", "list", "tuple"))
def test_payload_name_boxes_into_owned_slot_before_field_work(field, site, tmp_path):
    body = "    return take(value=record)\n"
    if site == "list":
        body = "    return take(value=[record])\n"
    elif site == "tuple":
        body = "    return take(value=(record,))\n"
    source = PREFIX.replace("FIELD", field) + "def probe(record: Record):\n" + body
    text = _emit(source)
    (tmp_path / "input.py").write_text(source)
    (tmp_path / "program.ll").write_text(text)
    function = _function(text)
    assert re.search(
        r"(%[^ ]+) = call [^\n]*@py_valuebox_new\([^\n]*\n\s*store ptr \1, ptr ",
        function,
    )
    assert "@py_valuebox_set_field(" in function
    assert "@pcc_gc_foreign_lease_acquire(" in function
    if field == "list":
        assert "record.value.root.1" in function
        assert "@pcc_gc_root_copy_borrowed_lease(" in function


def test_nested_payload_name_copies_each_registered_pointer_leaf(tmp_path):
    source = '''import pcc
@pcc.valueclass
class Leaf:
    values: list
    count: int
@pcc.valueclass
class Record:
    leaf: Leaf
    trailer: list
def take(*, value, **extras):
    return value
def probe(record: Record):
    boxed = take(value=record)
    return record.leaf.values, record.trailer, boxed
'''
    text = _emit(source)
    (tmp_path / "program.ll").write_text(text)
    function = _function(text)
    allocations = re.findall(r"(%[^ ]+) = call [^\n]*@py_valuebox_new\([^\n]*", function)
    assert len(allocations) == 2
    for value in allocations:
        assert re.search(re.escape(value) + r" = call [^\n]*\n\s*store ptr " + re.escape(value) + r", ptr ", function)
    assert "record.value.root.0_0" in function
    assert "record.value.root.1" in function
    assert function.count("@pcc_gc_root_copy_borrowed_lease(") >= 2


@pytest.mark.parametrize("global_statement", (False, True))
def test_module_payload_name_uses_the_mapped_global(global_statement, tmp_path):
    declaration = "    global record\n" if global_statement else ""
    source = PREFIX.replace("FIELD", "list") + "record = Record(7, [1])\ndef probe():\n" + declaration + "    return take(value=record)\n"
    text = _emit(source)
    (tmp_path / "program.ll").write_text(text)
    function = _function(text)
    assert "value.attribute.module.source" in function
    assert "@pcc_gc_root_copy_lease(" in function


def test_payload_name_with_raw_pointer_field_remains_rejected():
    source = PREFIX.replace("import pcc", "import pcc\nfrom pcc.extern import c_ptr").replace("FIELD", "c_ptr")
    source += "def probe(record: Record):\n    return take(value=record)\n"
    with pytest.raises(L1CodegenError, match="raw"):
        _emit(source)


def test_erased_expression_type_uses_the_actual_payload_binding(monkeypatch):
    from dataclasses import replace
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.py_ast import DynType, Name

    original = L1CodeGen._emit_slot_call_operand

    def erased(self, expr, label):
        if isinstance(expr, Name) and expr.ident == "record":
            expr = replace(expr, ty=DynType(name="dyn"))
        return original(self, expr, label)

    monkeypatch.setattr(L1CodeGen, "_emit_slot_call_operand", erased)
    source = PREFIX.replace("FIELD", "list") + "def probe(record: Record):\n    return take(value=record)\n"
    function = _function(_emit(source))
    assert "@py_valuebox_new(" in function
    assert "record.value.root.1" in function
    assert "@pcc_gc_root_copy_borrowed_lease(" in function


def test_live_import_staging_is_not_a_payload_owner():
    from types import SimpleNamespace
    from pcc.frontends.python.codegen.attr_load_lowering import AttrLoadLoweringMixin
    from pcc.frontends.python.py_ast import DynType, Name

    staging = object()
    host = SimpleNamespace(
        env={},
        _module_globals={"record": (staging, DynType(name="dyn"))},
        _native_extension_module_env={"record": staging},
    )
    expression = Name(span=None, ty=DynType(name="dyn"), ident="record")
    assert AttrLoadLoweringMixin._emit_slot_call_valueclass_name(host, expression, "test") is None


def test_payload_name_producer_is_in_native_method_contract():
    from inspect import signature
    from pcc.frontends.python.codegen.host_contract import L1_CODEGEN_HOST_METHODS
    from pcc.frontends.python.codegen._l1_codegen_static_methods import L1_CODEGEN_STATIC_METHODS
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.codegen.layer1_support import _default_native_module_exports

    name = "_emit_slot_call_valueclass_name"
    static = {entry["name"]: entry for entry in L1_CODEGEN_STATIC_METHODS}
    exports = _default_native_module_exports("pcc.frontends.python.codegen.layer1")
    native = {entry["name"]: entry for entry in exports["pcc.frontends.python.codegen.layer1"]["L1CodeGen"]["methods"]}
    assert name in L1_CODEGEN_HOST_METHODS
    assert static[name] == native[name]
    assert tuple(item["name"] for item in static[name]["call_sig"]) == tuple(signature(getattr(L1CodeGen, name)).parameters)
