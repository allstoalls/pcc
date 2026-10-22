"""Native namespace producers preserve real roots and ordinary Name lookup.

Host IR contracts only; runtime identity and five-GC execution are separate gates.
"""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_ast import Attr, DynType, Name
from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.type_infer import infer_module


def _emit(source, mode="on", exports=None, siblings=()):
    module = infer_module(parse_and_lift(source, "namespace_values.py", "namespace_values"))
    codegen = L1CodeGen(module, ir_scaffold_mode=mode)
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    if exports is not None:
        codegen._native_module_exports = dict(codegen._native_module_exports or {})
        codegen._native_module_exports.update(exports)
    codegen._sibling_module_inits = siblings
    return str(codegen.generate(module))


def _body(text, name="probe"):
    match = re.search(r"^define [^\n]*@user_namespace_values_" + name + r"\([^\n]*\).*?^}", text, re.M | re.S)
    assert match is not None
    return match.group()


def _assert_immediate_publication(text, symbol):
    calls = list(re.finditer(r"(%[^ ]+) = call [^\n]*@" + symbol + r"\([^\n]*\)\n", text))
    assert calls, symbol
    for call in calls:
        next_line = text[call.end():].splitlines()[0].strip()
        assert next_line.startswith("store ptr " + call.group(1) + ", ptr "), next_line


@pytest.mark.parametrize("name", ("dict", "set", "tuple"))
@pytest.mark.parametrize("mode", ("off", "on"))
def test_builtin_factory_new_owner_is_published_at_the_producer(name, mode):
    text = _emit("def probe():\n    return str(" + name + ")\n", mode)
    _assert_immediate_publication(_body(text), "py_builtin_type_for_tag")
    assert not re.search(r"call [^\n]*@py_cpy_", _body(text))


@pytest.mark.parametrize("name", ("dict", "set", "tuple"))
def test_dataclass_factory_original_shape(name):
    text = _emit("from dataclasses import dataclass, field\n@dataclass\nclass State:\n    values: object = field(default_factory=" + name + ")\n")
    _assert_immediate_publication(text, "py_builtin_type_for_tag")


def test_notimplemented_copies_exact_runtime_singleton_source():
    body = _body(_emit("def probe():\n    return type(NotImplemented)\n"))
    assert "@py_NotImplemented" in body
    assert re.search(r"call [^\n]*@pcc_gc_root_copy_lease\(", body)
    assert not re.search(r"call [^\n]*@py_module_attr_get\(", body)


@pytest.mark.parametrize("name", ("dict", "NotImplemented"))
def test_shadowed_builtin_values_use_their_local_owner(name):
    body = _body(_emit("def probe(" + name + "):\n    return str(" + name + ")\n"))
    assert not re.search(r"call [^\n]*@py_builtin_type_for_tag\(", body)
    assert "@py_NotImplemented" not in body
    assert re.search(r"call [^\n]*@pcc_gc_root_copy_borrowed_lease\(", body)


@pytest.mark.parametrize("name", ("dict", "NotImplemented"))
def test_shadowed_global_builtin_values_use_real_binding(name):
    body = _body(_emit(name + " = 'replacement'\ndef probe():\n    return str(" + name + ")\n"))
    assert not re.search(r"call [^\n]*@py_builtin_type_for_tag\(", body)
    assert "@py_NotImplemented" not in body
    assert ".modvar.namespace_values." + name in body


def test_missing_name_lookup_owns_result_and_retains_nameerror():
    body = _body(_emit("def probe():\n    return str(missing_value)\n"))
    _assert_immediate_publication(body, "py_module_attr_get")
    assert "name.dynamic.missing_value.missing" in body
    assert re.search(r"call [^\n]*@py_exc_new\(i64 10,", body)
    assert re.search(r"call [^\n]*@py_raise\(", body)


@pytest.mark.parametrize("attribute,helper", (
    ("stdout", "py_sys_stream_object"),
    ("stdin", "py_sys_stream_object"),
    ("stderr", "py_sys_stream_object"),
    ("prefix", "py_sys_prefix_str"),
    ("base_prefix", "py_sys_prefix_str"),
    ("executable", "py_sys_executable_str"),
    ("path", "py_sys_path_list"),
))
@pytest.mark.parametrize("alias", ("sys", "system"))
def test_native_namespace_runtime_results_publish_before_checks(attribute, helper, alias):
    body = _body(_emit("import sys as " + alias + "\ndef probe():\n    return str(" + alias + "." + attribute + ")\n"))
    _assert_immediate_publication(body, helper)
    assert not re.search(r"call [^\n]*@py_cpy_", body)


@pytest.mark.parametrize("attribute", ("prefix", "base_prefix"))
def test_from_import_runtime_result_publishes_to_the_name_sink(attribute):
    body = _body(_emit("from sys import " + attribute + " as value\ndef probe():\n    return str(value)\n"))
    _assert_immediate_publication(body, "py_sys_prefix_str")


def test_argv_list_and_elements_publish_before_population():
    body = _body(_emit("import sys\ndef probe():\n    return str(sys.argv[0])\n"))
    _assert_immediate_publication(body, "py_list_new")
    _assert_immediate_publication(body, "py_str_new")
    assert re.search(r"call [^\n]*@py_list_append\(", body)
    assert re.search(r"call [^\n]*@pcc_gc_foreign_lease_acquire\(", body)
    assert re.search(r"call [^\n]*@pcc_gc_store_root\([^\n]*ptr null", body)


@pytest.mark.parametrize("expression", ("operating.sep", "operating.pathsep", "operating.path.sep", "operating.path.pathsep"))
def test_os_projection_uses_registered_alias_and_static_literal_owner(expression):
    body = _body(_emit("import os as operating\ndef probe():\n    return str(" + expression + ")\n"))
    assert not re.search(r"call [^\n]*@py_cpy_", body)
    assert not re.search(r"call [^\n]*@py_obj_getattr\(", body)
    assert bool(re.search(r"@\.pystr\.obj\.", body))


def test_os_path_from_import_retains_native_provenance():
    body = _body(_emit("from os import path as paths\ndef probe():\n    return str(paths.sep)\n"))
    assert not re.search(r"call [^\n]*@py_cpy_", body)
    assert bool(re.search(r"@\.pystr\.obj\.", body))


@pytest.mark.parametrize("part", ("major", "minor", "micro"))
def test_sys_version_component_keeps_native_projection(part):
    body = _body(_emit("import sys as system\ndef probe():\n    return str(system.version_info." + part + ")\n"))
    assert not re.search(r"call [^\n]*@py_cpy_", body)
    assert not re.search(r"call [^\n]*@py_obj_getattr\(", body)


@pytest.mark.parametrize("source", (
    "import sys\ndef probe(sys):\n    return str(sys.prefix)\n",
    "import sys\ndef probe(value):\n    sys = value\n    return str(sys.prefix)\n",
    "import sys\ndef probe(value):\n    output = str(sys.prefix)\n    sys = value\n    return output\n",
))
def test_local_shadow_rebinding_and_unbound_paths_are_not_namespace_projections(source):
    body = _body(_emit(source))
    assert not re.search(r"call [^\n]*@py_sys_prefix_str\(", body)
    assert re.search(r"call [^\n]*@py_obj_getattr\(", body)
    # Only a read that can precede the binding keeps the UnboundLocalError
    # flag (439d8274e drops it for locals bound on every path).
    if "output = str(sys.prefix)" in source:
        assert "sys.bound" in body


def test_builtin_attribute_override_copies_registered_module_slot():
    body = _body(_emit("import os\nos.sep = 'replacement'\ndef probe():\n    return str(os.sep)\n"))
    assert "@.modattr.os.sep" in body
    assert re.search(r"call [^\n]*@pcc_gc_root_copy_lease\(", body)
    assert "namespace.attribute.current" in body


def test_compiled_module_binding_is_loaded_live_before_attribute_lookup():
    exports = {"provider": {"VALUE": {"kind": "constant", "value_kind": "int", "value": 4,
                "owning_module": "provider", "export_name": "VALUE", "has_module_storage": True,
                "value_ty": ["int", 64, True], "box_int_abi": False}}}
    text = _emit("import provider as values\ndef probe():\n    return str(values.VALUE)\n", exports=exports, siblings=("provider",))
    body = _body(text)
    _assert_immediate_publication(body, "py_module_attr_get")
    _assert_immediate_publication(body, "py_obj_getattr")
    assert "name.dynamic.values" in body
    assert not re.search(r"load ptr, ptr @.pcc.ext.modref.values", body)


@pytest.mark.parametrize("source_kind", ("local", "global", "native_binding", "unregistered"))
def test_namespace_projection_requires_authoritative_provenance(source_kind):
    ty = DynType(name="dyn")
    expr = Attr(span=None, ty=ty, obj=Name(span=None, ty=ty, ident="sys"), name="prefix")
    probe = SimpleNamespace(env={}, _module_globals={}, _native_extension_module_env={},
                            _native_builtin_module_for_name=lambda _: None,
                            _native_builtin_value_kind_for_expr=lambda _: None,
                            _native_module_object_export_info=lambda *_: None)
    if source_kind == "local":
        probe.env["sys"] = object()
    elif source_kind == "global":
        probe._module_globals["sys"] = object()
    elif source_kind == "native_binding":
        probe._native_extension_module_env["sys"] = object()
    assert L1CodeGen._native_namespace_projection(probe, expr) is None


@pytest.mark.parametrize('context', ('local', 'unbound', 'sibling'))
def test_published_module_callable_does_not_skip_actual_binding(context):
    exports = {'provider': {'target': {'kind': 'function', 'owning_module': 'provider', 'export_name': 'target'}}}
    if context == 'local':
        source = 'import provider as values\ndef probe(values):\n    return str(values.target)\n'
    elif context == 'unbound':
        source = 'import provider as values\ndef probe(value):\n    output = str(values.target)\n    values = value\n    return output\n'
    else:
        source = 'import provider as values\ndef probe():\n    return str(values.target)\n'
    body = _body(_emit(source, exports=exports, siblings=('provider',)))
    assert bool(re.search(r'call [^\n]*@py_obj_getattr\(', body)), 'module metadata bypassed actual binding'
    if context == 'unbound':
        assert 'values.bound' in body
    elif context == 'sibling':
        assert 'name.dynamic.values' in body


def test_original_stream_default_shape_publishes_before_signature_metadata():
    text = _emit("import sys\nclass Node:\n    def show(self, buf=sys.stdout, offset=0):\n        return buf\n")
    _assert_immediate_publication(text, "py_sys_stream_object")
    assert "func.sig.kind" in text


@pytest.mark.parametrize("runtime_binding", (False, True))
def test_raw_export_metadata_cannot_become_a_managed_namespace_root(runtime_binding):
    from pcc.frontends.python.codegen.errors import L1CodegenError
    ty = DynType(name="dyn")
    expr = Attr(span=None, ty=ty, obj=Name(span=None, ty=ty, ident="values"), name="ADDRESS")
    probe = SimpleNamespace(env={}, _module_globals={},
        _native_extension_module_env={"values": object()} if runtime_binding else {},
        _native_module_object_export_info=lambda *_: ("provider", {"kind": "module_global", "value_ty": ["raw_pointer"]}))
    projection = L1CodeGen._native_namespace_projection(probe, expr)
    assert projection == ("raw",)
    probe._native_namespace_projection = lambda _: projection
    with pytest.raises(L1CodegenError, match="raw module export"):
        L1CodeGen._emit_slot_call_namespace_attribute(probe, expr, "raw")


def test_explicit_raw_attribute_keeps_its_unmanaged_projection():
    from pcc.frontends.python.py_ast import RawPointerType
    expr = Attr(span=None, ty=RawPointerType(name="pcc.extern.c_rawptr"),
                obj=Name(span=None, ty=DynType(name="dyn"), ident="values"), name="ADDRESS")
    raw = object()
    probe = SimpleNamespace(_maybe_emit_ir_scaffold_symbol_value=lambda _: raw)
    # No root API exists: an explicit raw lane must bypass managed publication.
    assert L1CodeGen._emit_attr(probe, expr) is raw


@pytest.mark.parametrize("mode", ("off", "on"))
def test_ply_dir_getattr_original_comprehension_has_owned_names(mode):
    text = _emit('''def probe(module):
    _items = [(k, getattr(module, k)) for k in dir(module)]
    return _items
''', mode)
    assert re.search(r"call [^\n]*@py_obj_dir_slots\(", text)
    assert re.search(r"call [^\n]*@py_obj_getattr\(", text)
    assert not re.search(r"call [^\n]*@py_cpy_", text)
    assert "strict.nolib.stub" not in _body(text)


@pytest.mark.parametrize("site", ("argument", "return", "default", "later-error", "discard"))
def test_dir_result_uses_registered_output_slot_at_every_consumer(site):
    prefix = 'def take(*, value, later=None, **extras):\n    return value\ndef fail():\n    raise ValueError("later")\n'
    if site == "default":
        body = '    def inner(value=dir(module)):\n        return value\n    return inner()\n'
    elif site == "argument":
        body = '    return take(value=dir(module))\n'
    elif site == "later-error":
        body = '    return take(value=dir(module), later=fail())\n'
    elif site == "discard":
        body = '    dir(module)\n'
    else:
        body = '    return dir(module)\n'
    text = _body(_emit(prefix + 'def probe(module):\n' + body))
    call = re.search(r"call [^\n]*@py_obj_dir_slots\(ptr (%[^,]+), ptr ([^)]+)\)", text)
    assert call, text
    assert call.group(1) != call.group(2)
    assert text.index("@pcc_gc_root_copy_borrowed_lease") < call.start()
    assert "strict.nolib.stub" not in text
    assert not re.search(r"call [^\n]*@py_cpy_", text)


@pytest.mark.parametrize("binding", ("function", "parameter", "local", "global", "class"))
def test_shadowed_dir_uses_actual_callable(binding):
    helper = 'def custom(value):\n    return ["custom"]\n'
    if binding == "function":
        source = 'def dir(value):\n    return ["custom"]\ndef probe(module):\n    return dir(module)\n'
    elif binding == "parameter":
        source = 'def probe(dir, module):\n    return dir(module)\n'
    elif binding == "local":
        source = helper + 'def probe(module):\n    dir = custom\n    return dir(module)\n'
    elif binding == "class":
        source = 'class dir:\n    def __init__(self, value):\n        self.value = value\ndef probe(module):\n    return dir(module)\n'
    else:
        source = helper + 'dir = custom\ndef probe(module):\n    return dir(module)\n'
    body = _body(_emit(source))
    assert not re.search(r"call [^\n]*@py_obj_dir_slots\(", body)
    assert not re.search(r"call [^\n]*@py_cpy_", body)
    assert "strict.nolib.stub" not in body


def test_no_argument_dir_names_the_missing_frame_namespace():
    from pcc.frontends.python.codegen.errors import L1CodegenError
    with pytest.raises(L1CodegenError, match="frame-local namespace provider"):
        _emit('def probe():\n    return dir()\n')
    # The same syntax remains legal when the name has an actual local binding.
    assert "strict.nolib.stub" not in _body(_emit('def probe(dir):\n    return dir()\n'))


@pytest.mark.parametrize("foreign,shadowed", ((False, False), (True, False), (False, True), (True, True)))
def test_dir_result_analysis_follows_the_actual_operand_and_binding(foreign, shadowed):
    module = infer_module(parse_and_lift('def probe(value):\n    return dir(value)\n', "reflection.py", "reflection"))
    codegen = L1CodeGen(module, ir_scaffold_mode="on")
    expression = module.body[0].body[0].value
    codegen._cpy_env_flags["value"] = foreign
    if shadowed:
        codegen.env["dir"] = (None, DynType(name="dyn"))
    assert codegen._expr_looks_cpython(expression) is (foreign and not shadowed)


def test_dir_of_imported_module_uses_live_native_binding():
    exports = {"provider": {"value": {"kind": "constant", "value_kind": "int", "value": 4,
               "owning_module": "provider", "export_name": "value", "has_module_storage": True,
               "value_ty": ["int", 64, True], "box_int_abi": False}}}
    text = _emit('''import provider
def probe():
    _items = [(k, getattr(provider, k)) for k in dir(provider)]
    return _items
''', exports=exports, siblings=("provider",))
    assert re.search(r"call [^\n]*@py_obj_dir_slots\(", _body(text))
    assert "strict.nolib.stub" not in text
    assert not re.search(r"call [^\n]*@py_cpy_", text)
