"""Imported constructors publish owners before initialization and cleanup."""
import re

import pytest

from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.pipeline_context import build_closed_world_context
from pcc.frontends.python.type_infer import infer_module


PROVIDER = '''class Box:
    def __init__(self, value, count: int):
        self.value = value
        self.count = count
class Fields:
    __slots__ = ("first", "second")
    first: object
    second: object
class Empty:
    pass
'''


def _compile(tmp_path, monkeypatch, expression, provider_source=PROVIDER, probe_parameters=""):
    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "0")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "0")
    provider = tmp_path / "result_provider.py"
    entry = tmp_path / "entry.py"
    provider.write_text(provider_source)
    entry.write_text('import result_provider as provider\n'
                     'def first():\n    return "first"\n'
                     'def second():\n    return "second"\n'
                     'def consume(value):\n    return value\n'
                     'def probe(' + probe_parameters + '):\n    return ' + expression + '\n')
    modules, exports, _ = build_closed_world_context(
        [str(provider), str(entry)], ["result_provider", "entry"])
    typed = infer_module(modules[1], external_exports={"result_provider": exports["result_provider"]})
    codegen = L1CodeGen(typed, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    codegen._native_module_exports = exports
    emitted = str(codegen.generate(typed))
    (tmp_path / "entry.ll").write_text(emitted)
    assert not re.search(r"\bcall [^\n]*@py_cpy_", emitted)
    match = re.search(r"^define [^\n]*@user_entry_probe\([^\n]*\).*?^}", emitted, re.M | re.S)
    assert match is not None
    return match.group(0)


@pytest.mark.parametrize("expression", [
    'consume(provider.Box("item", 7))',
    'consume(provider.Box(provider.Box("inner", 3), 7))',
    'consume(provider.Fields(first(), second()))',
    'consume(provider.Empty())',
    'consume([provider.Fields(first(), second())])',
    'consume(value=provider.Box("item", 7))',
    'consume(value=[provider.Box("item", 7)])',
    'consume(value=provider.Fields(first(), second()))',
])
def test_imported_constructor_nested_result_has_published_owner(tmp_path, monkeypatch, expression):
    body = _compile(tmp_path, monkeypatch, expression)
    allocations = list(re.finditer(r"(%[^\s]+) = call ptr [^\n]*@py_instance_new\([^\n]+\)", body))
    assert allocations
    for allocation in allocations:
        after = body[allocation.end():].splitlines()
        assert after[1].strip().startswith("store ptr " + allocation.group(1) + ", ptr ")


def test_field_constructor_evaluates_all_operands_before_field_writes(tmp_path, monkeypatch):
    body = _compile(tmp_path, monkeypatch, 'provider.Fields(first(), second())')
    first = body.index("@user_entry_first(")
    second = body.index("@user_entry_second(")
    allocation = body.index("@py_instance_new(")
    setter = body.index("@py_obj_setattr(")
    assert first < second < allocation < setter
    assert body.count("@user_entry_first(") == 1
    assert body.count("@user_entry_second(") == 1


def test_constructor_owner_is_published_before_initializer(tmp_path, monkeypatch):
    body = _compile(tmp_path, monkeypatch, 'provider.Box(first(), 17)')
    allocation = re.search(r"(%[^\s]+) = call ptr [^\n]*@py_instance_new\([^\n]+\)", body)
    assert allocation is not None
    publication = body.index("store ptr " + allocation.group(1) + ", ptr ", allocation.end())
    initializer = body.index("@user_result_provider_Box___init__(")
    assert publication < initializer
    assert "@pcc_gc_take_pinned_slot(" in body[initializer:]


@pytest.mark.parametrize("class_name,arguments", [("Box", '"item", 7'), ("Fields", 'first(), second()')])
def test_imported_class_owner_is_not_replaced_by_same_named_local(tmp_path, monkeypatch, class_name, arguments):
    body = _compile(tmp_path, monkeypatch, 'provider.' + class_name + '(' + arguments + ')',
                    probe_parameters=class_name)
    assert '@.class.result_provider.' + class_name in body


def test_imported_new_uses_complete_class_protocol(tmp_path, monkeypatch):
    provider = '''class Factory:
    def __new__(cls, value):
        return object.__new__(cls)
    def __init__(self, value):
        self.value = value
'''
    body = _compile(tmp_path, monkeypatch, 'consume(value=provider.Factory(first()))', provider)
    assert body.count("@user_entry_first(") == 1
    assert "@py_obj_call_slots(" in body
    assert "@user_result_provider_Factory___new__(" not in body
    assert "@py_instance_new(" not in body


def test_empty_class_arguments_keep_runtime_binding_error(tmp_path, monkeypatch):
    body = _compile(tmp_path, monkeypatch, 'consume(value=provider.Empty(first()))')
    assert body.count("@user_entry_first(") == 1
    assert "@py_obj_call_slots(" in body
    assert "@py_instance_new(" not in body


def test_constructor_lifetime_fixture_reaches_owned_ir(tmp_path, monkeypatch):
    from test_imported_constructor_result_roots_native import _inputs

    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_EMIT", "0")
    monkeypatch.setenv("PCC_DIRECT_INDEXED_KERNEL_CAPTURE", "0")
    provider, entry = _inputs(tmp_path)
    modules, exports, _ = build_closed_world_context(
        [str(provider), str(entry)], ["result_provider", "entry"])
    for module in modules:
        typed = infer_module(module, external_exports={key: value for key, value in exports.items() if key != module.name})
        codegen = L1CodeGen(typed, emit_cpy_main_exitcode=False, ir_scaffold_mode="on")
        codegen._strict_no_libpython = True
        codegen._prefer_native_callable_values = True
        codegen._native_module_exports = exports
        text = str(codegen.generate(typed))
        (tmp_path / (module.name + ".ll")).write_text(text)
        assert not re.search(r"\bcall [^\n]*@py_cpy_", text)
