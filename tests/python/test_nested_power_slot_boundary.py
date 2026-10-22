"""Fail closed before a visible float-capable power reaches an int kernel.

This is bounded expression provenance and owned-IR evidence, not general
power dispatch or native qualification. Names, aliases and callback returns
can hide value provenance; those still require an actual runtime-tag owner.
"""
from __future__ import annotations

import pytest

from pcc.frontends.python.codegen.errors import L1CodegenError
from tests.owned_ir_validation import verify_ir_text
from tests.python.test_dynamic_power_comparison_boundary import assert_rooted_power
from tests.python.test_slot_call_operand_roots import (
    SlotProbeCodegen,
    _emit,
    _probe_function,
)


OUTER = ("({value}) ** 2", "2 ** ({value})", "pow({value}, 2)", "pow(2, {value})")
INNER = ("2 ** -1", "pow(2, -1)")
WRAPPERS = (
    "{power}", "({power}) + 1", "1 + ({power})", "({power}) - 1",
    "1 - ({power})", "({power}) * 2", "2 * ({power})",
    "({power}) // 1", "({power}) % 1", "-({power})", "+({power})",
    "(({power}) if condition else 1)", "(1 if condition else ({power}))",
    "(({power}) or 1)", "(1 and ({power}))",
)


def _source(expression):
    return ("def probe(condition: bool, base: int, exponent: int):\n"
            "    return slot_operand_probe(" + expression + ")\n")


@pytest.mark.parametrize("outer", OUTER)
@pytest.mark.parametrize("inner", INNER)
@pytest.mark.parametrize("wrapper", WRAPPERS)
def test_visible_float_capable_power_never_reaches_outer_integer_kernel(
    monkeypatch, outer, inner, wrapper,
):
    expression = outer.format(value=wrapper.format(power=inner))
    calls = []
    original = SlotProbeCodegen._slot_call_runtime_call

    def record(self, runtime_name, *args, **kwargs):
        calls.append(runtime_name)
        return original(self, runtime_name, *args, **kwargs)

    monkeypatch.setattr(SlotProbeCodegen, "_slot_call_runtime_call", record)
    with pytest.raises(L1CodegenError, match="power operand may be non-integer.*generic power dispatch"):
        _emit(_source(expression))
    # Reject the unsupported outer consumer before evaluating either nested
    # operand, rather than letting its integer-layout helper see a float.
    assert "py_int_pow" not in calls


@pytest.mark.parametrize("inner", ("base ** exponent", "pow(base, exponent)"))
@pytest.mark.parametrize("outer", OUTER)
def test_dynamic_inner_power_also_needs_a_result_kind_proof(inner, outer):
    with pytest.raises(L1CodegenError, match="power operand may be non-integer.*generic power dispatch"):
        _emit(_source(outer.format(value=inner)))


@pytest.mark.parametrize("expression,powers,expected", (
    ("2 ** -1", 1, 0.5),
    ("pow(2, -1)", 1, 0.5),
    ("(2 ** -1) + 1", 1, 1.5),
    ("pow(2, -1) + 1", 1, 1.5),
    ("-(2 ** -1)", 1, -0.5),
    ("(2 ** 3) ** -1", 2, 0.125),
    ("2 ** (2 ** 3)", 2, 256),
    ("pow(2 ** 3, -1)", 2, 0.125),
    ("pow(pow(2, 3), -1)", 2, 0.125),
    ("pow(2, pow(2, 3))", 2, 256),
    ("((2 ** 2) + 1) ** -1", 2, 0.2),
    ("int(2 ** -1) ** 2", 2, 0),
    ("pow(int(pow(2, -1)), 2)", 2, 0),
    ("base ** exponent", 1, 0.5),
    ("pow(base, exponent)", 1, 0.5),
    ("((2 ** -1) if False else 2) ** 2", 1, 4),
    ("(2 if True else pow(2, -1)) ** 2", 1, 4),
    ("(True or (2 ** -1)) ** 2", 1, 1),
    ("(False and (2 ** -1)) ** 2", 1, 0),
    ("pow(pow(pow(2, 3), 2), 2)", 3, 4096),
    ("pow(pow(2, 3), 2) ** 2", 3, 4096),
    ("(pow(2, 3) ** 2) ** 2", 3, 4096),
    ("((pow(2, 3) + 1) ** 2) ** 2", 3, 6561),
    ("(True or ((2 ** -1) ** -1)) ** 2", 1, 1),
    ("(False and ((2 ** -1) ** -1)) ** 2", 1, 0),
    ("((True or ((2 ** -1) ** -1)) and 3) ** 2", 1, 9),
    ("((False and ((2 ** -1) ** -1)) or 3) ** 2", 1, 9),
))
def test_supported_power_values_keep_their_owned_route(expression, powers, expected):
    source = _source(expression)
    text = _emit(source)
    verify_ir_text(text)
    assert len(assert_rooted_power(_probe_function(text))) == powers
    namespace = {"slot_operand_probe": lambda value: value}
    exec(source, namespace)
    result = namespace["probe"](True, 2, -1)
    assert type(result) is type(expected) and result == expected


@pytest.mark.parametrize("expression", (
    "(2 ** -1) ** -1", "pow(pow(2, -1), -1)",
    "2 ** (2 ** -1)", "pow(2, pow(2, -1))",
))
@pytest.mark.parametrize("consumer", ("return", "assignment", "argument", "default", "comparison"))
def test_real_consumers_reject_before_their_integer_power_path(expression, consumer):
    from tests.python.test_lambda_constructor_roots import emit

    prefix = "def take(*, value, **extras):\n    return value\n"
    bodies = {
        "return": "    return " + expression + "\n",
        "assignment": "    value = " + expression + "\n    return value\n",
        "argument": "    return take(value=" + expression + ")\n",
        "default": "    def target(value=" + expression + "):\n        return value\n    return target()\n",
        "comparison": "    return " + expression + " < 1.0\n",
    }
    with pytest.raises(L1CodegenError, match="power operand may be non-integer.*generic power dispatch"):
        emit(prefix + "def probe():\n" + bodies[consumer])


@pytest.mark.parametrize("expression", ("(2 ** -1) ** -1", "pow(pow(2, -1), -1)"))
def test_pipeline_rejects_reported_nested_power(tmp_path, monkeypatch, expression):
    from tests.python.test_dynamic_power_comparison_boundary import compile_ir

    source = "def probe():\n    value = " + expression + "\n    return value\n"
    with pytest.raises(L1CodegenError, match="power operand may be non-integer.*generic power dispatch"):
        compile_ir(tmp_path, monkeypatch, source, "nested_power")


@pytest.mark.parametrize("name,parameters", (
    ("_guard_integer_power_operand_provenance", ("self", "lhs", "rhs")),
    ("_slot_call_power_provenance_expr", ("self", "expr")),
))
def test_guard_host_and_native_method_contracts_match(name, parameters):
    from inspect import Parameter, signature
    from pcc.frontends.python.codegen._l1_codegen_static_methods import L1_CODEGEN_STATIC_METHODS
    from pcc.frontends.python.codegen.host_contract import L1_CODEGEN_HOST_METHODS
    from pcc.frontends.python.codegen.layer1 import L1CodeGen
    from pcc.frontends.python.codegen.layer1_support import _default_native_module_exports

    assert name in L1_CODEGEN_HOST_METHODS
    static = {entry["name"]: entry for entry in L1_CODEGEN_STATIC_METHODS}
    exports = _default_native_module_exports("pcc.frontends.python.codegen.layer1")
    native = {entry["name"]: entry for entry in exports["pcc.frontends.python.codegen.layer1"]["L1CodeGen"]["methods"]}
    assert static[name] == native[name]
    assert tuple(item["name"] for item in static[name]["call_sig"]) == parameters
    assert all(item["kind"] == "pos" and not item["has_default"] for item in static[name]["call_sig"])
    actual = signature(getattr(L1CodeGen, name)).parameters
    assert tuple(actual) == parameters
    assert all(item.kind == Parameter.POSITIONAL_OR_KEYWORD
               and item.default == Parameter.empty for item in actual.values())


@pytest.mark.parametrize("target", ("name", "attribute", "subscript"))
@pytest.mark.parametrize("exponent", ("2 ** -1", "pow(2, -1)", "(2 ** -1) + 1"))
def test_augmented_power_rejects_visible_noninteger_exponent(target, exponent):
    from tests.python.test_lambda_constructor_roots import emit

    prefix = ""
    if target == "attribute":
        prefix = "class Box:\n    def __init__(self):\n        self.value = 2\n"
        setup, name = "    value = Box()\n", "value.value"
    elif target == "subscript":
        setup, name = "    value = [2]\n", "value[0]"
    else:
        setup, name = "    value = 2\n", "value"
    source = (prefix + "def probe():\n" + setup + "    " + name
              + " **= " + exponent + "\n    return " + name + "\n")
    with pytest.raises(L1CodegenError, match="power operand may be non-integer.*generic power dispatch"):
        emit(source)


@pytest.mark.parametrize("target", ("name", "attribute", "subscript"))
@pytest.mark.parametrize("exponent", ("2 ** 3", "pow(2, 3)", "pow(pow(2, 2), 2)"))
def test_augmented_power_keeps_proven_integer_exponent(target, exponent):
    from tests.python.test_lambda_constructor_roots import emit

    prefix = ""
    if target == "attribute":
        prefix = "class Box:\n    def __init__(self):\n        self.value = 2\n"
        setup, name = "    value = Box()\n", "value.value"
    elif target == "subscript":
        setup, name = "    value = [2]\n", "value[0]"
    else:
        setup, name = "    value = 2\n", "value"
    source = (prefix + "def probe():\n" + setup + "    " + name
              + " **= " + exponent + "\n    return " + name + "\n")
    verify_ir_text(emit(source))
    # Reference only: these legacy augmented-assignment routes have not
    # been executed natively or ownership-qualified by this containment.
    namespace = {}
    exec(source, namespace)
    assert namespace["probe"]() == 2 ** eval(exponent, {})


@pytest.mark.parametrize("kind,value", (("int", "2"), ("float", "2.0"), ("complex", "2j")))
@pytest.mark.parametrize("target", ("attribute", "subscript"))
def test_dynamic_augmented_power_cannot_trust_target_annotation(kind, value, target):
    from tests.python.test_lambda_constructor_roots import emit

    if target == "attribute":
        source = ("class Box:\n    value: " + kind + " = " + value + "\n"
                  "def probe(box: Box):\n    box.value **= 2 ** -1\n    return box.value\n")
    else:
        source = ("def probe():\n    box = [" + value + "]\n"
                  "    box[0] **= 2 ** -1\n    return box[0]\n")
    with pytest.raises(L1CodegenError, match="power operand may be non-integer.*generic power dispatch"):
        emit(source)


@pytest.mark.parametrize("target", ("name", "typed-self"))
@pytest.mark.parametrize("rhs,helper", (
    ("(2 ** -1) + 0.0", "pow"),
    ("(2 ** -1) + 0j", "py_complex_pow"),
))
def test_augmented_power_preserves_actual_float_and_complex_rhs_route(target, rhs, helper):
    from tests.python.test_lambda_constructor_roots import emit
    from tests.python.test_slot_call_operand_roots import _calls

    if target == "typed-self":
        source = ("class Box:\n    value: int = 2\n    def probe(self):\n"
                  "        self.value **= " + rhs + "\n        return self.value\n")
    else:
        source = ("def probe():\n    value = 2\n    value **= " + rhs
                  + "\n    return value\n")
    text = emit(source)
    verify_ir_text(text)
    assert len(_calls(text, "py_int_pow")) == 1
    assert len(_calls(text, helper)) == 1


@pytest.mark.parametrize("target", ("name", "typed-self"))
def test_augmented_power_preserves_actual_float_target_route(target):
    from tests.python.test_lambda_constructor_roots import emit
    from tests.python.test_slot_call_operand_roots import _calls

    if target == "typed-self":
        source = ("class Box:\n    value: float = 2.0\n    def probe(self):\n"
                  "        self.value **= 2 ** -1\n        return self.value\n")
    else:
        source = "def probe():\n    value = 2.0\n    value **= 2 ** -1\n    return value\n"
    text = emit(source)
    verify_ir_text(text)
    assert len(_calls(text, "py_int_pow")) == 1
    assert len(_calls(text, "pow")) == 1


@pytest.mark.parametrize("expression", (
    "value ** (2 ** -1)", "(2 ** -1) ** value",
    "value ** pow(2, -1)", "pow(2, -1) ** value",
    "value ** ((2 ** -1) + 1)", "((2 ** -1) + 1) ** value",
    "pow(value, 2 ** -1)", "pow(2 ** -1, value)",
))
@pytest.mark.parametrize("consumer", ("return", "assignment", "argument", "comparison"))
def test_dynamic_peer_does_not_hide_visible_power_provenance(expression, consumer):
    from tests.python.test_lambda_constructor_roots import emit

    prefix = "def take(*, value, **extras):\n    return value\n"
    bodies = {
        "return": "    return " + expression + "\n",
        "assignment": "    result = " + expression + "\n    return result\n",
        "argument": "    return take(value=" + expression + ")\n",
        "comparison": "    return " + expression + " < 1.0\n",
    }
    with pytest.raises(L1CodegenError, match="power operand may be non-integer.*generic power dispatch"):
        emit(prefix + "def probe(value):\n" + bodies[consumer])


@pytest.mark.parametrize("peer", ("value", "(value + 0j)"))
@pytest.mark.parametrize("expression", ("{peer} ** (2 ** -1)", "(2 ** -1) ** {peer}"))
def test_expression_fallback_keeps_selected_float_and_complex_route(peer, expression):
    from tests.python.test_lambda_constructor_roots import emit
    from tests.python.test_slot_call_operand_roots import _calls

    # The returned power is at an object boundary: it takes the runtime
    # power protocol, where a negative float base to a fractional power is
    # complex and 0.0 to a negative power raises, as in CPython's float_pow.
    text = emit("def probe(value: float):\n    return " + expression.format(peer=peer) + "\n")
    verify_ir_text(text)
    assert len(_calls(text, "py_int_pow")) == 1
    assert len(_calls(text, "py_obj_pow")) == 1


@pytest.mark.parametrize("expression", ("value ** (2 ** -1)", "(2 ** -1) ** value"))
def test_expression_fallback_keeps_typed_float_kernel(expression):
    from tests.python.test_lambda_constructor_roots import emit
    from tests.python.test_slot_call_operand_roots import _calls

    # A float lane (the annotated return) keeps the float kernel.
    text = emit("def probe(value: float) -> float:\n    return " + expression + "\n")
    verify_ir_text(text)
    assert len(_calls(text, "py_int_pow")) == 1
    assert len(_calls(text, "pow")) == 1
    assert not _calls(text, "py_obj_pow")


@pytest.mark.parametrize("expression", ("value ** (2 ** -1)", "(2 ** -1) ** value"))
def test_expression_fallback_keeps_selected_class_power_protocol(expression):
    from tests.python.test_lambda_constructor_roots import emit
    from tests.python.test_slot_call_operand_roots import _calls

    source = ("class Value:\n    def __pow__(self, other):\n        return other\n"
              "    def __rpow__(self, other):\n        return other\n"
              "def probe(value: Value):\n    return " + expression + "\n")
    text = emit(source)
    verify_ir_text(text)
    assert len(_calls(text, "py_int_pow")) == 1
