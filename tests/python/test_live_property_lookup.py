"""Dynamic property lookup scope and explicit unresolved typed-result coverage."""
from __future__ import annotations

import ast
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from tests.python.owned_regression_support import (
    assert_owned_program,
    assert_reference_program,
    explicit_owned_runtime,
)


ROOT = Path(__file__).resolve().parents[2]
LOWERING = ROOT / "pcc/frontends/python/codegen/attr_load_lowering.py"
CLASS_GEN = ROOT / "pcc/frontends/python/codegen/class_gen.py"
CLASS_MODEL = ROOT / "pcc/frontends/python/codegen/class_model_lowering.py"
SOURCE = ROOT / "tests/fixtures/native/property_lookup_live.py"
TYPED_SOURCE = ROOT / "tests/fixtures/native/property_lookup_typed.py"


class Name:
    def __init__(self, ident):
        self.ident = ident


class DynType:
    pass


class RawPointerType:
    pass


class ValueArrayType:
    pass


def _function(path, name, namespace):
    tree = ast.parse(path.read_text())
    matches = [node for node in ast.walk(tree)
               if isinstance(node, ast.FunctionDef) and node.name == name]
    assert len(matches) == 1
    node = matches[0]
    node.decorator_list = []
    module = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
    exec(compile(module, str(path), "exec"), namespace)
    return namespace[name]


def _lookup_branch(source, route):
    """Execute complete receiver selection, rather than only its final guard."""
    tree = ast.parse(source)
    mixin = next(node for node in tree.body
                 if isinstance(node, ast.ClassDef)
                 and node.name == "AttrLoadLoweringMixin")
    emit = next(node for node in mixin.body
                if isinstance(node, ast.FunctionDef) and node.name == "_emit_attr")
    owner_name = "info" if route == "hinted" else "info_p"
    matches = []
    for node in emit.body:
        if not isinstance(node, ast.If):
            continue
        selected = [child for child in ast.walk(node)
                    if isinstance(child, ast.Assign)
                    and len(child.targets) == 1
                    and isinstance(child.targets[0], ast.Name)
                    and child.targets[0].id == owner_name
                    and isinstance(child.value, ast.Call)
                    and isinstance(child.value.func, ast.Attribute)
                    and child.value.func.attr == "_resolve_property_mro"]
        if selected:
            matches.append(node)
    assert len(matches) == 1
    function = ast.parse("def probe(self, expr):\n    pass\n").body[0]
    prefix = []
    if route == "self":
        prefix = [node for node in emit.body
                  if isinstance(node, ast.Assign)
                  and ast.unparse(node) == "current_class = self.current_class"]
        assert len(prefix) == 1
    function.body = prefix + matches + ast.parse(
        "raise AssertionError('property route did not return')").body
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    namespace = {"Name": Name, "DynType": DynType}
    exec(compile(module, str(LOWERING), "exec"), namespace)
    return namespace["probe"]


def _exercise_lookup(source, route, receiver_kind, result_kind="dyn"):
    events = []
    # The getter stands in for the declared accessor's function: property
    # calls it with exactly the receiver.
    receiver, selected = object(), object()
    getter = SimpleNamespace(args=(object(),))
    result_ty = DynType() if result_kind == "dyn" else object()
    if result_kind == "raw":
        result_ty = RawPointerType()
    expr = SimpleNamespace(obj=Name("record" if route == "hinted" else "self"),
                           name="value", ty=result_ty)

    def info(name, bases, properties):
        return SimpleNamespace(
            name=name, bases_ast=bases, properties=properties,
            valueclass=False, metaclass_name=None, expanded_cd=None,
            owning_module=None, export_class_name=name,
            enum_members={}, enum_string_members={}, class_attrs={},
        )

    base = info("Base", (), {"value": getter})
    child = info("Child", (Name("Base"),), {})
    parent = SimpleNamespace(
        ast_module=SimpleNamespace(name="property_routes", body=()),
        _native_module_exports={}, _runtime_port_module=False,
        _freestanding_module=False,
        _is_valueclass_payload_type=lambda ty: getattr(ty, "valueclass", False),
    )
    if receiver_kind == "metaclass":
        child.metaclass_name = "Meta"
    elif receiver_kind == "dynamic_layout":
        parent._native_module_exports = {
            "property_routes": {"Child": {"dynamic_field_layout": True}}}
    elif receiver_kind == "valueclass":
        child.valueclass = True
    elif receiver_kind == "runtime":
        parent._runtime_port_module = True
    else:
        assert receiver_kind == "ordinary"
    lower = SimpleNamespace(
        parent=parent, classes={"Base": base, "Child": child}, _class_defs=[],
        lookup_class_attr=lambda owner, name: owner.class_attrs.get(name),
    )
    namespace = {
        "Name": Name, "RawPointerType": RawPointerType,
        "ValueArrayType": ValueArrayType, "_is_ast_node": isinstance,
    }
    _function(CLASS_GEN, "_classgen_has_dynamic_field_layout", namespace)
    classify = _function(CLASS_GEN, "uses_live_class_attribute", namespace)
    resolve = _function(CLASS_MODEL, "_resolve_property_mro", namespace)
    takes_protocol_args = _function(
        CLASS_MODEL, "_property_accessor_takes_protocol_args", namespace,
    )
    accessor_definition = _function(CLASS_MODEL, "_property_accessor_definition", namespace)

    def actual_classification(actual, name, value_ty):
        events.append(("classify", actual.name))
        return classify(lower, actual, name, value_ty)

    def live_lookup(actual):
        assert actual is expr
        events.append(("live lookup", "Child"))
        return selected

    def direct(function, arguments, name):
        assert function is getter and arguments == [receiver]
        events.append(("direct getter", "Base"))
        return selected

    lower.uses_live_class_attribute = actual_classification
    lower.emit_live_class_attribute = live_lookup
    parent.class_lowering = lower
    parent.env_class_hint = {"record": "Child"}
    parent.current_class = child
    parent.env = {"self": (object(),)}
    parent._self_receiver_class_name = lambda: "Child"
    parent._resolve_property_mro = MethodType(resolve, parent)
    parent._property_accessor_takes_protocol_args = MethodType(takes_protocol_args, parent)
    parent._property_accessor_definition = MethodType(accessor_definition, parent)
    parent._emit_expr = lambda value: receiver
    parent._fresh = lambda value: value
    parent.builder = SimpleNamespace(call=direct, load=lambda slot, name: receiver)
    # The real resolver selects Base; the emitter must nevertheless classify
    # Child's receiver layout rather than the property declaration's owner.
    assert parent._resolve_property_mro("Child", "value") is base
    assert _lookup_branch(source, route)(parent, expr) is selected
    return events


@pytest.mark.parametrize("route", ("hinted", "self"))
@pytest.mark.parametrize("receiver_kind", (
    "ordinary", "metaclass", "dynamic_layout", "valueclass", "runtime",
))
def test_dynamic_property_route_classifies_the_receiver(route, receiver_kind):
    assert _exercise_lookup(LOWERING.read_text(), route, receiver_kind) == [
        ("classify", "Child"),
        ("live lookup", "Child") if receiver_kind == "ordinary"
        else ("direct getter", "Base"),
    ]


@pytest.mark.parametrize("route", ("hinted", "self"))
@pytest.mark.parametrize("result_kind", ("int", "float", "bool", "str", "raw"))
def test_typed_property_result_does_not_enter_dynamic_result_fix(route, result_kind):
    # This preserves an existing unresolved path, not typed-property support.
    assert _exercise_lookup(LOWERING.read_text(), route, "ordinary", result_kind) == [
        ("direct getter", "Base")]


@pytest.mark.parametrize("fixture", (SOURCE, TYPED_SOURCE), ids=("dynamic", "typed-unresolved"))
def test_property_lookup_fixture_reference(tmp_path, fixture):
    assert_reference_program(fixture.read_text(), "LIVE_PROPERTY_LOOKUP_OK\n", tmp_path)


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_property_lookup_native_five_selectors(
    tmp_path, python_program_compiler, explicit_owned_runtime, request, capfd,
):
    # Selector runs are not evidence that all five collectors collected.
    # The unchanged original time fixture remains the observed-collector gate.
    assert_owned_program(
        SOURCE.read_text(), "LIVE_PROPERTY_LOOKUP_OK\n", tmp_path,
        python_program_compiler,
        request.node.callspec.params["python_program_compiler"],
        explicit_owned_runtime, capfd,
    )


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_typed_property_replacement_native_unresolved(
    tmp_path, python_program_compiler, explicit_owned_runtime, request, capfd,
):
    # Strong assertions intentionally remain. The dynamic-result-only patch
    # does not fix this case; native status is separately reported as UNRUN
    # until executed, and a failure must not be called a qualification pass.
    assert_owned_program(
        TYPED_SOURCE.read_text(), "LIVE_PROPERTY_LOOKUP_OK\n", tmp_path,
        python_program_compiler,
        request.node.callspec.params["python_program_compiler"],
        explicit_owned_runtime, capfd,
    )
