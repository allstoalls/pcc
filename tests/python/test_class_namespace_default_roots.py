"""Active ordinary class names acquire values from the live namespace owner."""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_ast import DynType, Name
from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.type_infer import infer_module


def _emit(source):
    module = infer_module(parse_and_lift(source, "class_namespace.py", "class_namespace"))
    codegen = L1CodeGen(module, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    return str(codegen.generate(module))


@pytest.mark.parametrize("prepared", (False, True))
@pytest.mark.parametrize("mismatch", ("inactive", "function", "slot", "absent"))
def test_class_namespace_producer_requires_exact_active_binding(mismatch, prepared):
    owner = object()
    slot = object()
    expr = Name(span=None, ty=DynType(name="dyn"), ident="fields")
    context = (owner, object(), {"fields": (slot, "fields")}, {})
    if prepared:
        context += (True,)
    probe = SimpleNamespace(
        current_function=owner,
        env={"fields": (slot, object(), expr.ty)},
        _class_namespace_context=context,
    )
    if mismatch == "inactive":
        probe._class_namespace_context = None
    elif mismatch == "function":
        probe.current_function = object()
    elif mismatch == "slot":
        probe.env["fields"] = (object(), object(), expr.ty)
    else:
        probe.env.clear()
    # No builder or root-creation API exists: mismatches must not emit or
    # certify a source, even when the spelling is a declared class field.
    assert L1CodeGen._emit_class_namespace_name_root(probe, expr, "probe") is None


def _assert_lookup_published_before_cleanup(text):
    pattern = r"(%[^ ]+) = call [^\n]*@py_obj_subscript\([^\n]*\)\n"
    lookups = list(re.finditer(pattern, text))
    assert lookups
    for lookup in lookups:
        following = text[lookup.end():].splitlines()[0].strip()
        assert following.startswith("store ptr " + lookup.group(1) + ", ptr "), following
    assert "class.definition.namespace" in text
    assert "@pcc_gc_foreign_lease_acquire(" in text
    assert "@pcc_gc_foreign_lease_release(" in text
    assert "class.name.fallback" in text
    assert "@py_exc_matches(" in text


@pytest.mark.parametrize("default", ("fields", "(fields,)", "identity(fields)"))
def test_ordinary_class_default_uses_live_namespace(default):
    text = _emit(
        "def identity(value):\n    return value\n"
        "class Holder:\n    fields = ('first',)\n"
        "    def target(self, value=" + default + "):\n        return value\n"
    )
    _assert_lookup_published_before_cleanup(text)
    # The signature keeps the lookup's owner through its metadata allocation.
    publication = re.search(
        r"(%[^ ]+) = call [^\n]*@py_obj_subscript\([^\n]*\)\n"
        r"  store ptr \1, ptr (%[^ ,\n]+)", text,
    )
    assert publication is not None
    tail = text[publication.end():]
    assert "func.sig.kind" in tail
    assert "@py_tuple_set_item(" in tail


def test_dataclass_fields_default_matches_original_py_ast_shape():
    text = _emit('''from dataclasses import dataclass
@dataclass(frozen=True)
class Type:
    name: str
@dataclass(frozen=True)
class ClassType(Type):
    module: str
    fields: tuple[tuple[str, Type], ...] = ()
    bases: tuple["ClassType", ...] = ()
    properties: tuple[tuple[str, Type], ...] = ()
    valueclass: bool = False
''')
    _assert_lookup_published_before_cleanup(text)


def test_class_scope_lookup_does_not_capture_method_parameter():
    text = _emit('''def consume(*, value, **extras):
    return value
class Holder:
    fields = ('class',)
    def target(self, fields):
        return consume(value=fields)
''')
    body = re.search(r"^define [^\n]*@user_class_namespace_Holder_target\([^\n]*\).*?^}", text, re.M | re.S)
    assert body is not None
    assert "class.name" not in body.group(0)
    assert "@pcc_gc_root_copy_borrowed_lease(" in body.group(0)


@pytest.mark.parametrize("default", ("sentinel", "(sentinel,)", "identity(sentinel)", "method"))
def test_prepared_class_default_uses_live_namespace(default):
    text = _emit('''def identity(value):
    return value
class Meta(type):
    @classmethod
    def __prepare__(cls, name, bases):
        return {}
    def __new__(cls, name, bases, namespace):
        return type(name, bases, namespace)
class Holder(metaclass=Meta):
    sentinel = object()
    def method(self):
        return None
    def target(self, value=''' + default + '''):
        return value
''')
    # Both prepared constructors use the same namespace-root producer. Its
    # owner must be published before _call_user retires argument temporaries.
    prepare = re.search(r"(%[^ ]+) = call [^\n]*@user_class_namespace_Meta___prepare__\([^\n]*\)\n", text)
    assert prepare is not None
    next_line = text[prepare.end():].splitlines()[0].strip()
    assert next_line.startswith("store ptr " + prepare.group(1) + ", ptr %class.definition.namespace.prepared.")
    lookups = list(re.finditer(r"(%call\.slot\.runtime[^ ]+) = call [^\n]*@py_obj_subscript\([^\n]*\)\n", text))
    assert lookups
    for lookup in lookups:
        following = text[lookup.end():].splitlines()[0].strip()
        assert following.startswith("store ptr " + lookup.group(1) + ", ptr "), following
    assert "class.name.fallback" in text
    assert "namespace.body.lease" in text
    assert "namespace.body.release" in text


@pytest.mark.parametrize("custom", (False, True), ids=("dict", "custom"))
def test_prepared_namespace_roots_reach_owned_object_emitter(tmp_path, custom):
    from pcc.backend.owned_object_emit import emit_owned_object

    namespace = '''class Namespace:
    def __init__(self):
        self.data = {}
    def __setitem__(self, key, value):
        self.data[key] = value
    def __getitem__(self, key):
        return self.data[key]
''' if custom else ""
    program = '''class Meta(type):
    @classmethod
    def __prepare__(cls, name, bases):
        return {}
    def __new__(cls, name, bases, namespace):
        return type(name, bases, namespace)
try:
    class Holder(metaclass=Meta):
        sentinel = object()
        def method(self, value=sentinel):
            return value
except Exception:
    raise
'''
    if custom:
        program = program.replace("return {}", "return Namespace()")
        program = program.replace("type(name, bases, namespace)", "type(name, bases, namespace.data)")
    text = _emit(namespace + program)
    (tmp_path / "prepared_namespace.ll").write_text(text)
    object_bytes = emit_owned_object(text, "x86_64-unknown-linux-gnu")
    (tmp_path / "prepared_namespace.o").write_bytes(object_bytes)
    assert object_bytes[:4] == b"\x7fELF"
