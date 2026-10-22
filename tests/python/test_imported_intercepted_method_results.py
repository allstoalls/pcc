"""Imported method annotations cannot project a hook replacement's result."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

from pcc.frontends.python import type_infer
from pcc.frontends.python.codegen import class_override_index
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.pipeline_context import build_closed_world_context
from pcc.frontends.python.py_ast import DynType, FuncDef, Return
from pcc.frontends.python.py_lift import parse_and_lift
from tests.python.owned_regression_support import explicit_owned_runtime


HOOK = '''    def __getattribute__(self, name):
        if name == 'target':
            return replacement
        return object.__getattribute__(self, name)
'''


def _sources(tmp_path, shape, annotation="float", literal="1.25"):
    method = ("    def target(self) -> " + annotation + ":\n"
              "        return " + literal + "\n")
    prefix = "def replacement():\n    return 'redirected'\n"
    if shape == "direct":
        provider = prefix + "class Receiver:\n" + method + HOOK
    elif shape == "inherited":
        provider = (prefix + "class Hook:\n" + HOOK
                    + "class Receiver(Hook):\n" + method)
    elif shape == "subclass":
        provider = (prefix + "class Receiver:\n" + method
                    + "class Child(Receiver):\n" + HOOK)
    elif shape == "multiple":
        provider = (prefix + "class Receiver:\n" + method
                    + "class Hook:\n" + HOOK
                    + "class Child(Receiver, Hook):\n    pass\n")
    else:
        provider = prefix + "class Receiver:\n" + method
        if shape == "unrelated":
            provider += "class Other:\n" + HOOK
    entry = "from hook_provider import Receiver\n"
    if shape == "local":
        entry += ("from hook_provider import replacement\n"
                  "class Local(Receiver):\n" + HOOK)
    entry += "def probe(receiver: Receiver):\n    return receiver.target()\n"
    paths = [tmp_path / "hook_provider.py", tmp_path / "entry.py"]
    for path, text in zip(paths, (provider, entry)):
        path.write_text(text, encoding="utf-8")
    return paths


def _typed(tmp_path, shape, annotation="float", literal="1.25", suffix=""):
    paths = _sources(tmp_path, shape, annotation, literal)
    if suffix:
        paths[1].write_text(paths[1].read_text() + suffix)
    modules, exports, _ = build_closed_world_context(
        [str(path) for path in paths], ["hook_provider", "entry"],
    )
    typed = type_infer.infer_module(
        modules[1], external_exports={"hook_provider": exports["hook_provider"]},
    )
    return typed, exports


def _result_type(module, name="probe"):
    function = next(stmt for stmt in module.body
                    if isinstance(stmt, FuncDef) and stmt.name == name)
    return next(stmt.value.ty for stmt in function.body if isinstance(stmt, Return))


@pytest.mark.parametrize("shape", ("direct", "inherited", "subclass", "multiple", "local"))
@pytest.mark.parametrize("annotation,literal", (("float", "1.25"), ("bool", "True"),
                                              ("int", "7"), ("str", "'declared'")))
def test_imported_intercepted_result_infers_dynamic(tmp_path, shape, annotation, literal):
    typed, _ = _typed(tmp_path, shape, annotation, literal)
    assert isinstance(_result_type(typed), DynType)


@pytest.mark.parametrize("shape", ("plain", "unrelated"))
def test_unintercepted_import_keeps_declared_result(tmp_path, shape):
    typed, _ = _typed(tmp_path, shape)
    assert _result_type(typed).name == "float"


def test_class_object_lookup_keeps_annotation_but_shadowed_name_does_not(tmp_path):
    typed, _ = _typed(tmp_path, "direct", suffix='''
def class_call(receiver: Receiver):
    return Receiver.target(receiver)
def parameter_shadow(Receiver: Receiver):
    return Receiver.target()
def assignment_shadow(receiver: Receiver):
    Receiver = receiver
    return Receiver.target()
''')
    assert _result_type(typed, "class_call").name == "float"
    assert isinstance(_result_type(typed, "parameter_shadow"), DynType)
    assert isinstance(_result_type(typed, "assignment_shadow"), DynType)


def test_interceptor_graph_is_cached_per_context(tmp_path, monkeypatch):
    calls = []
    original = class_override_index.build_export_attribute_interceptors

    def counted(exports):
        calls.append(exports)
        return original(exports)

    monkeypatch.setattr(class_override_index, "build_export_attribute_interceptors", counted)
    _typed(tmp_path, "direct", suffix="def repeated(receiver: Receiver):\n"
           + "    receiver.target()\n" * 64)
    assert len(calls) == 1


def test_graph_keeps_reexports_multiple_inheritance_and_unrelated_siblings_separate():
    def record(name, bases=(), hook=False, owner="provider"):
        return {"kind": "class", "class_name": name, "owning_module": owner,
                "base_names": bases, "methods": ({"name": "__getattribute__"},) if hook else ()}

    base = record("Base")
    exports = {"provider": {"Base": base, "Sibling": record("Sibling", ("Base",))},
               "facade": {"Alias": base},
               "other": {"Hook": record("Hook", hook=True, owner="other"),
                         "Child": record("Child", ("facade.Alias", "Hook"), owner="other")}}
    # Qualified re-export spelling is itself an alias, not a canonical key.
    result = class_override_index.build_export_attribute_interceptors(exports)
    assert result == {"provider.Base", "other.Hook", "other.Child"}
    assert "provider.Sibling" not in result


def test_refactored_method_override_graph_keeps_owner_and_alias_identity():
    base = {"kind": "class", "class_name": "Base", "base_names": (), "methods": ()}
    child = {"kind": "class", "class_name": "Child", "base_names": ("Base",),
             "methods": ({"name": "target"},)}
    unrelated = {"kind": "class", "class_name": "Base", "base_names": (), "methods": ()}
    result = class_override_index.build_export_method_overrides(
        {"owner": {"Base": base, "Child": child}, "other": {"Base": unrelated}},
    )
    assert result == {"owner.Base": {"target"}}


def test_attribute_interceptor_helpers_emit_without_cpython():
    path = Path(class_override_index.__file__)
    module = type_infer.infer_module(parse_and_lift(
        path.read_text(), str(path), "class_override_index",
    ))
    codegen = L1CodeGen(module, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    text = str(codegen.generate(module))
    assert "strict.nolib.stub" not in text
    assert not re.search(r"\bcall [^\n]*@py_cpy_", text)
    assert "@user_class_override_index_build_export_attribute_interceptors(" in text


def test_interceptor_bootstrap_exports_include_new_scope_and_helper_metadata():
    names = ["pcc.frontends.python.type_infer",
             "pcc.frontends.python.codegen.class_override_index"]
    _, exports, _ = build_closed_world_context(
        [type_infer.__file__, class_override_index.__file__], names,
    )
    scope = exports[names[0]]["_Scope"]
    assert "class_objects" in scope["field_names"]
    assert "is_class_object" in {method["name"] for method in scope["methods"]}
    assert "_attribute_interceptors" in exports[names[0]]["_InferCtx"]["field_names"]
    method_result = exports[names[0]]["_external_method_return_type"]
    assert len(method_result["param_types"]) == 4
    signature = method_result["call_sig"]
    assert len(signature) == 4
    assert [argument["name"] for argument in signature] == [
        "ctx", "receiver", "method_name", "receiver_is_class_object",
    ]
    assert all(not argument["has_default"] for argument in signature[:3])
    assert signature[3]["has_default"]
    assert signature[3]["default"].value is False
    for name, arity in (("_export_class_graph", 1),
                        ("_attribute_interceptors_for_graph", 2),
                        ("build_export_attribute_interceptors", 1),
                        ("build_export_method_overrides", 1)):
        info = exports[names[1]][name]
        assert info["kind"] == "function"
        assert len(info["param_types"]) == arity
        assert len(info["call_sig"]) == arity


@pytest.mark.parametrize("shape", ("direct", "inherited", "subclass", "multiple", "local"))
@pytest.mark.parametrize("annotation,literal", (("float", "1.25"), ("bool", "True")))
def test_imported_intercepted_result_ir_stays_boxed(tmp_path, shape, annotation, literal):
    typed, exports = _typed(tmp_path, shape, annotation, literal)
    codegen = L1CodeGen(typed, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    codegen._native_module_exports = exports
    text = str(codegen.generate(typed))
    match = re.search(r"^define [^\n]*@user_entry_probe\([^\n]*\).*?^}", text, re.M | re.S)
    assert match is not None
    body = match.group(0)
    assert "@py_obj_call_slots(" in body
    assert "@py_float_to_f64(" not in body
    assert "@py_obj_truthy(" not in body
    assert "@py_int_to_i64_lane(" not in body
    assert not re.search(r"\bcall [^\n]*@user_hook_provider_\w+_target\(", body)
    assert "ret ptr" in body


@pytest.mark.integration
@pytest.mark.parametrize("shape", ("direct", "inherited", "subclass", "multiple", "local"))
@pytest.mark.parametrize("annotation,literal", (("float", "1.25"), ("bool", "True")))
def test_imported_intercepted_result_pcc0_emitted_five_gc(tmp_path, explicit_owned_runtime,
                                                       shape, annotation, literal):
    # Host compile_python_multi emits these binaries. Native-pcc1 imported
    # replay remains a separate qualification requirement.
    from pcc.frontends.python.pipeline import compile_python_multi

    paths = _sources(tmp_path, shape, annotation, literal)
    receiver = "Local" if shape == "local" else "Child" if shape in ("subclass", "multiple") else "Receiver"
    entry = paths[1].read_text()
    if receiver == "Child":
        entry += "from hook_provider import Child\n"
    entry += ("def main():\n    value = probe(" + receiver + "())\n"
              "    assert value == 'redirected'\n"
              "    assert isinstance(value, str)\n"
              "    print('IMPORTED_HOOK_RESULT_OK')\nmain()\n")
    paths[1].write_text(entry)
    reference = subprocess.run([sys.executable, str(paths[1])], capture_output=True,
                               text=True, timeout=20)
    assert (reference.returncode, reference.stdout, reference.stderr) == (
        0, "IMPORTED_HOOK_RESULT_OK\n", "")
    executable = tmp_path / "entry.out"
    compile_python_multi([str(path) for path in paths], str(executable),
                         module_names=["hook_provider", "entry"], entry_module="entry",
                         backend="self", libpython_mode="off", ir_scaffold_mode="on",
                         runtime_archive=str(explicit_owned_runtime))
    for backend in range(5):
        result = subprocess.run([str(executable)], capture_output=True, text=True, timeout=20,
                                env=dict(os.environ, PCC_GC_BACKEND=str(backend),
                                         PCC_GC_REFCOUNT_PROVENANCE_PROBE="2"))
        assert (result.returncode, result.stdout, result.stderr) == (
            reference.returncode, reference.stdout, reference.stderr), backend
