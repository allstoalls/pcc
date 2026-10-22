"""Fixture-free owned-IR checks for authoritative call operand producers.

No native object, runtime archive, subprocess, or emitted program is built or
executed here. These are compiler protocol checks, not moving-GC qualification.
"""
from __future__ import annotations

import re
from dataclasses import replace

import pytest

from pcc.frontends.python import type_infer
from pcc.frontends.python import parser as reference_parser
from pcc.frontends.python.codegen.errors import L1CodegenError
from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.codegen.hoist_free_names import compute_free_names
from pcc.frontends.python.py_ast import BinOp, DynType, IntLit, IntType, Name, SetType
from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.pipeline_ast_wire import (
    _py_ast_from_wire, _py_ast_node_replace, _py_ast_to_wire,
)
from pcc.ir.compat import ir


class SlotProbeCodegen(L1CodeGen):
    def _emit_call(self, expr):
        if isinstance(expr.func, Name):
            name = expr.func.ident
            if name == "slot_operand_probe":
                slot = self._emit_slot_call_operand(expr.args[0], "probe.operand")
                return self._take_slot_call_root(slot)
            if name == "slot_args_probe":
                slot = self._emit_slot_call_args_tuple(expr.args, "probe.args")
                return self._take_slot_call_root(slot)
            if name == "slot_kwargs_probe":
                _args, kwargs = self._slot_call_split_operands(expr)
                slot = self._emit_slot_call_kwargs_object(kwargs, None, expr.span, "probe.kwargs")
                return self._take_slot_call_root(slot)
            if name == "slot_cpy_probe":
                self._cpy_env_flags[expr.args[0].ident] = True
                return self._emit_slot_call_operand(expr.args[0], "probe.cpy")
            if name == "slot_raw_erased_probe":
                argument = replace(expr.args[0], ty=DynType(name="dyn"))
                return self._emit_slot_call_operand(argument, "probe.raw.erased")
            if name == "slot_stale_probe":
                slot = self._new_slot_call_root("probe.stale")
                value = self.builder.call(self.runtime["py_list_new"], [ir.Constant(ir.IntType(64), 0)])
                self.builder.call(self.runtime["py_err_occurred"], [])
                self._publish_slot_call_owned(slot, value, label="stale test result")
        return super()._emit_call(expr)


def _emit(source):
    module = type_infer.infer_module(parse_and_lift(source, "slot_operand.py", "slot_operand"))
    codegen = SlotProbeCodegen(module, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    return str(codegen.generate(module))


def _probe_function(text):
    # Callable/cache metadata is a separate emitted owner. These checks
    # describe the user probe's operand contract, not whole-module counts.
    body = re.search(r"^define [^\n]*@user_slot_operand_probe\([^\n]*\).*?^}", text, re.M | re.S)
    assert body is not None
    return body.group(0)


def _calls(text, name):
    return re.findall(r"[^\n]*\bcall\b[^\n]*@" + re.escape(name) + r"\([^\n]*", text)


def _with_slot_origins(text, call):
    for result, source in re.findall(r"(%[^ ]+) = bitcast ptr ([^ ]+) to ptr", text):
        call = call.replace(result + ")", source + ")").replace(result + ",", source + ",")
    return call


def test_parameter_operand_copies_authoritative_borrowed_root():
    text = _emit("""def probe(value):
    return slot_operand_probe(value)
""")
    text = _probe_function(text)
    copies = _calls(text, "pcc_gc_root_copy_borrowed_lease")
    assert len(copies) == 1
    assert "value.addr" in _with_slot_origins(text, copies[0])
    assert not _calls(text, "pcc_gc_root_copy_lease")
    assert _calls(text, "pcc_gc_foreign_lease_release")
    rebound = _emit("""def probe(value):
    value = [1]
    return slot_operand_probe(value)
""")
    rebound = _probe_function(rebound)
    assert _calls(rebound, "pcc_gc_root_copy_lease")
    assert not _calls(rebound, "pcc_gc_root_copy_borrowed_lease")


def test_global_and_class_operands_copy_existing_slots():
    text = _emit("""shared = [1]
class Box:
    pass
def probe():
    first = slot_operand_probe(shared)
    return slot_operand_probe(Box)
""")
    text = _probe_function(text)
    copies = _calls(text, "pcc_gc_root_copy_lease")
    assert len(copies) == 2
    assert any("shared" in _with_slot_origins(text, call) for call in copies)
    assert any("Box" in _with_slot_origins(text, call) for call in copies)


def test_generator_splat_publishes_call_before_cleanup():
    text = _emit("""def items():
    yield (1, 2)
def probe():
    return slot_args_probe(*items())
""")
    assert _calls(text, "py_list_extend")
    assert _calls(text, "py_tuple_from_list")
    # The precise returned owner is immediately stored. The old call-ret
    # retain/root/unregister route cannot appear between return and store.
    match = re.search(r"(%[^ ]+) = call ptr[^\n]*@user_[^\n(]*_items\([^\n]*\n([^\n]+)", text)
    assert match is not None, [line for line in text.splitlines() if "call" in line and "items" in line]
    assert "store ptr " + match.group(1) in match.group(2)
    assert "probe.args.item" in match.group(2)


def test_nested_literal_operands_use_slot_producers():
    text = _emit("""def probe():
    return slot_operand_probe(([1, 2], {'answer': 3}, {4, 5}))
""")
    assert _calls(text, "py_list_append")
    assert _calls(text, "py_dict_set")
    assert _calls(text, "py_set_add")
    assert _calls(text, "py_tuple_from_list")
    assert not _calls(text, "pcc_gc_root_copy_borrowed_lease")
    assert "call.slot.argument.lease" in text
    # An explicit sequence argument is evaluated completely before set()
    # hashes its first member. In particular, a raising a.__hash__ must not
    # prevent effect() from running. Keep the independent review's exact
    # list reduction, and cover the equivalent tuple constructor too.
    for sequence in ("[a, effect()]", "(a, effect())"):
        explicit = _emit("""def effect():
    print("effect")
    return 2
def probe(a):
    return slot_operand_probe(set(""" + sequence + "))\n")
        constructors = _calls(explicit, "py_set_from_iterable")
        assert len(constructors) == 1
        effect = re.search(r"[^\n]*\bcall\b[^\n]*@user_[^\n(]*_effect\([^\n]*", explicit)
        assert effect is not None
        assert effect.start() < explicit.index(constructors[0])
        assert not _calls(explicit, "py_set_add")
        ordinary = _emit("""def effect():
    print("effect")
    return 2
def probe(a):
    return set(""" + sequence + ")\n")
        constructors = _calls(ordinary, "py_set_from_iterable")
        assert len(constructors) == 1
        effect = re.search(r"[^\n]*\bcall\b[^\n]*@user_[^\n(]*_effect\([^\n]*", ordinary)
        assert effect is not None
        assert effect.start() < ordinary.index(constructors[0])
        assert not _calls(ordinary, "py_set_add")
    source = "literal = {1, 2}\nexplicit = set([1, 2])\n"
    lifted = (
        parse_and_lift(source, "set_literal.py", "set_literal"),
        reference_parser.parse(source, "set_literal.py"),
    )
    for module in lifted:
        typed = type_infer.infer_module(module)
        restored = _py_ast_from_wire(_py_ast_to_wire(typed))
        assert restored.body[0].value.is_set_literal
        assert isinstance(restored.body[0].value.ty, SetType)
        assert not restored.body[1].value.is_set_literal
        literal = restored.body[0].value
        assert replace(literal, ty=DynType(name="dyn")).is_set_literal
        assert _py_ast_node_replace(literal, {"ty": DynType(name="dyn")}).is_set_literal
        # Older wire inputs have no provenance and must remain ordinary calls.
        old_wire = _py_ast_to_wire(restored.body[1].value)
        del old_wire["fields"]["is_set_literal"]
        assert not _py_ast_from_wire(old_wire).is_set_literal
    shadowed = _emit("""def set(value):
    return 99
def probe():
    first = slot_operand_probe({1, 2})
    return ({3, 4}, first)
""")
    assert len(_calls(shadowed, "py_set_add")) == 4
    probe = re.search(r"^define [^\n]*@user_[^\n(]*_probe\([^\n]*\) \{(.*?)^\}", shadowed, re.M | re.S)
    assert probe is not None
    shadow_calls = re.findall(r"\bcall\b[^\n]*@user_[^\n(]*_set\(", probe.group(1))
    assert not shadow_calls
    unsafe_shadow = _emit("""from pcc.unsafe import null as set
def probe():
    return slot_operand_probe({1, 2})
""")
    assert len(_calls(unsafe_shadow, "py_set_add")) == 2
    for prefix in ("def set(value):\n    return 99\n", "from pcc.unsafe import null as set\n"):
        source = prefix + "def probe():\n    return {1, 2}\n"
        for module in (
            parse_and_lift(source, "set_shadow.py", "set_shadow"),
            reference_parser.parse(source, "set_shadow.py"),
        ):
            typed = type_infer.infer_module(module)
            literal = typed.body[-1].body[0].value
            assert literal.is_set_literal
            assert isinstance(literal.ty, SetType)
    for display, expected in (
        ("{value}", ("value",)),
        ("set([value])", ("set", "value")),
        ("len(value)", ("len", "value")),
    ):
        module = parse_and_lift(
            "def inner():\n    return " + display + "\n", "set_capture.py", "set_capture",
        )
        captured = compute_free_names(
            module.body[0], (), None, ("set", "len", "value"), (), (), {}, False, {},
        )
        assert captured == expected
    early_hash_error = _emit("""class First:
    def __hash__(self):
        raise ValueError("hash first")
def effect():
    print("effect")
    return 2
def probe():
    a = First()
    return slot_operand_probe(set([a, effect()]))
""")
    constructor = _calls(early_hash_error, "py_set_from_iterable")
    assert len(constructor) == 1
    effect = re.search(r"[^\n]*\bcall\b[^\n]*@user_[^\n(]*_effect\([^\n]*", early_hash_error)
    assert effect is not None
    assert effect.start() < early_hash_error.index(constructor[0])
    assert not _calls(early_hash_error, "py_set_add")


def test_keyword_mapping_merges_remain_owned_through_move():
    text = _emit("""def probe(mapping):
    return slot_kwargs_probe(first=1, **mapping, last=2)
""")
    text = _probe_function(text)
    assert len(_calls(text, "py_call_merge_kwargs_unique")) == 3
    assert len(_calls(text, "pcc_gc_root_move")) == 3
    assert _calls(text, "pcc_gc_root_copy_borrowed_lease")


def test_cpython_parameter_never_enters_native_slot_copy():
    with pytest.raises(L1CodegenError, match="CPython name requires an output-slot bridge"):
        _emit("""def probe(value):
    return slot_cpy_probe(value)
""")


def test_stale_owned_result_fails_closed():
    with pytest.raises(L1CodegenError, match="lacks an immediate owned-result handoff"):
        _emit("""def probe():
    return slot_stale_probe()
""")


def test_module_operand_leaves_root_before_nonparking_take():
    text = _emit("slot_operand_probe([1])\n")
    take = _calls(text, "pcc_gc_take_pinned_slot")[-1]
    prefix = text[:text.index(take)]
    assert _calls(prefix, "pcc_gc_frame_leave_lifo")
    assert prefix.rfind("@pcc_gc_frame_leave_lifo") > prefix.rfind("@pcc_py_gc_minor_graph_unlock")


def test_constructor_call_uses_slot_dispatch():
    text = _emit("""class Meta(type):
    def __call__(cls, value):
        return value
class Plain:
    def __init__(self, value):
        self.value = value
class Custom(metaclass=Meta):
    pass
class Inherited(Custom):
    pass
def probe(parts):
    first = Plain(*parts)
    second = Custom(*parts)
    third = Custom(3)
    fourth = Inherited(4)
    return slot_operand_probe((Plain(1), Custom(2), first, second, third, fourth))
""")
    assert len(_calls(text, "py_obj_call_slots")) >= 6
    assert not _calls(text, "py_obj_call_method_kwargs")
    assert "ctor.unpack.result" in text


def test_classmethod_actual_cls_uses_slot_dispatch():
    text = _emit("""class Base:
    def __init__(self, value):
        self.value = value
    @classmethod
    def build(cls, parts):
        return cls(*parts)
class Meta(type):
    def __call__(cls, value):
        return value
class Child(Base, metaclass=Meta):
    pass
def probe():
    direct = Child(11)
    return Child.build((17,))
""")
    assert len(_calls(text, "py_obj_call_slots")) >= 2
    borrowed = [_with_slot_origins(text, call) for call in _calls(text, "pcc_gc_root_copy_borrowed_lease")]
    assert any("cls.addr" in call for call in borrowed)
    assert not _calls(text, "py_obj_call_method_kwargs")


def test_raw_pointer_call_result_cannot_enter_managed_sink():
    prefix = """from pcc.extern import c_ptr
from pcc.unsafe import int_to_ptr
__pcc_runtime_port__ = True
def address() -> c_ptr:
    return int_to_ptr(4096)
"""
    with pytest.raises(L1CodegenError, match="raw pointer cannot be a slot-call operand"):
        _emit(prefix + "def probe():\n    return slot_operand_probe(address())\n")
    # Losing the call-site type must not erase the callee's raw ABI proof.
    with pytest.raises(L1CodegenError, match="raw-pointer ABI call cannot publish"):
        _emit(prefix + "def probe():\n    return slot_raw_erased_probe(address())\n")



def test_literal_integer_tree_publishes_every_result_before_cleanup():
    text = _emit("""def probe():
    return slot_operand_probe((-(1 << 100), ~((1 << 100) + 3),
                               +18446744073709551615, 3 ** 5,
                               (7 * 9 - 4) // 2, (19 % 7) >> 1,
                               (17 & 9) | (3 ^ 1)))
""")
    for name in ("shl", "neg", "xor", "add", "pow", "mul", "sub",
                 "floordiv", "mod", "shr", "and", "or"):
        calls = _calls(text, "py_int_" + name)
        assert calls, name
        for call in calls:
            result = re.search(r"(%[^ ]+) = call ptr", call)
            assert result is not None, call
            following = text[text.index(call) + len(call):].splitlines()[1]
            assert "store ptr " + result.group(1) in following, (call, following)
            assert ".operand" in following, following
    # Literals outside the tagged lane are static immortal objects
    # (1c0a63ab5), never parsed from text at run time.
    assert not _calls(text, "py_int_from_cstr")
    assert not _calls(text, "py_int_to_i64_lane")
    assert not _calls(text, "py_obj_invert")
    assert not _calls(text, "py_obj_neg")


def test_literal_integer_constructor_and_keyword_sinks():
    text = _emit("""class Row:
    def __init__(self, value, other=0):
        self.value = value
        self.other = other
def probe(value):
    return Row(*(value,), **{'other': -(1 << 100)})
""")
    assert _calls(text, "py_obj_call_slots")
    assert _calls(text, "py_int_shl")
    assert _calls(text, "py_int_neg")
    nested = _emit("""def probe():
    return slot_kwargs_probe(first=[-(1 << 100)], second={'value': ~(3 + 4)})
""")
    assert _calls(nested, "py_int_neg")
    assert _calls(nested, "py_int_xor")
    assert _calls(nested, "py_dict_set")


def test_literal_integer_operator_errors_use_rooted_values():
    for expression, helper in (("7 // 0", "py_int_floordiv"), ("7 % 0", "py_int_mod")):
        text = _emit("def probe():\n    return slot_operand_probe(" + expression + ")\n")
        assert _calls(text, helper)
        assert re.search(r"(%[^ ]+) = load ptr, ptr %[^\n]+\n"
                         r"[^\n]*icmp eq ptr \1, null", text)
        assert "divres_null" in text
        assert "div.zero" in text
    for expression in ("1 << -1", "1 >> -(1 << 100)"):
        text = _emit("def probe():\n    return slot_operand_probe(" + expression + ")\n")
        assert _calls(text, "py_int_cmp")
        assert re.search(r"sext i32 %[^ ]+ to i64", text)
        assert "shift.neg" in text
        assert not _calls(text, "py_int_to_i64_lane")


def test_literal_unary_bool_plus_produces_integer():
    text = _emit("def probe():\n    return slot_operand_probe(+True)\n")
    text = _probe_function(text)
    # Identity-copying the bool would preserve the wrong runtime type.
    assert _calls(text, "py_int_add")
    assert not _calls(text, "pcc_gc_root_copy_lease")
    for expression, helper in (("True & False", "py_obj_and"),
                               ("True | False", "py_obj_or"),
                               ("True ^ False", "py_obj_xor"),
                               ("(True | False) + 1", "py_obj_or"),
                               ("~(True & False)", "py_obj_and")):
        source = "def probe():\n    return slot_operand_probe(" + expression + ")\n"
        module = type_infer.infer_module(parse_and_lift(source, "slot_operand.py", "slot_operand"))
        operand = module.body[0].body[0].value.args[0]
        codegen = SlotProbeCodegen(module, ir_scaffold_mode="on")
        assert codegen._slot_call_literal_integer_kind(operand) == 0
        with pytest.raises(L1CodegenError, match="integer producer requires literal provenance"):
            codegen._emit_slot_call_literal_integer(operand, "unproven.bool.tree")
        text = _probe_function(_emit(source))
        produced = re.search(r"(%[^ ]+) = call ptr[^\n]*@" + helper + r"\([^\n]*\n([^\n]+)", text)
        assert produced is not None
        assert produced.group(2).lstrip().startswith("store ptr " + produced.group(1) + ",")
        assert not _calls(text, "py_int_to_i64_lane")


@pytest.mark.parametrize("source", (
    "def probe(value: int):\n    return slot_operand_probe(-value)\n",
    "def value() -> int:\n    return 4\ndef probe():\n    return slot_operand_probe(-value())\n",
    "class Value:\n    def __neg__(self):\n        return 4\ndef probe(value: Value):\n    return slot_operand_probe(-value)\n",
    "def probe(value):\n    return slot_operand_probe(-value)\n",
))
def test_generic_unary_uses_owned_dispatch_without_literal_integer_proof(source):
    module = type_infer.infer_module(parse_and_lift(source, "slot_operand.py", "slot_operand"))
    expression = module.body[-1].body[0].value.args[0]
    codegen = SlotProbeCodegen(module, ir_scaffold_mode="on")
    # An annotation, callback return annotation or user __neg__ does not
    # establish an exact integer. The primitive producer still rejects it.
    assert codegen._slot_call_literal_integer_kind(expression) == 0
    with pytest.raises(L1CodegenError, match="integer producer requires literal provenance"):
        codegen._emit_slot_call_literal_integer(expression, "unproven.integer")

    text = _probe_function(_emit(source))
    produced = re.search(r"(%[^ ]+) = call ptr[^\n]*@py_obj_neg\([^\n]*\n([^\n]+)", text)
    assert produced is not None
    assert "store ptr " + produced.group(1) in produced.group(2)
    assert "probe.operand" in produced.group(2)
    assert _calls(text, "pcc_gc_foreign_lease_acquire")
    assert _calls(text, "pcc_gc_foreign_lease_release")
    assert not _calls(text, "py_int_neg")
    assert not _calls(text, "py_int_to_i64_lane")


@pytest.mark.parametrize("expression,helper,expected", (
    ("2 ** -1", None, 0.5),
    ("(2 ** -1) + 1", "py_obj_add", 1.5),
    ("1 + (2 ** -1)", "py_obj_add", 1.5),
    ("-(2 ** -1)", "py_obj_neg", -0.5),
))
def test_numeric_slot_negative_power_preserves_boxed_result(expression, helper, expected):
    from tests.python.test_dynamic_power_comparison_boundary import assert_rooted_power
    from tests.owned_ir_validation import verify_ir_text

    source = "def probe():\n    return slot_operand_probe(" + expression + ")\n"
    module = type_infer.infer_module(parse_and_lift(source, "slot_operand.py", "slot_operand"))
    operand = module.body[0].body[0].value.args[0]
    codegen = SlotProbeCodegen(module, ir_scaffold_mode="on")
    # A negative power is still not a proven integer tree. The generic
    # object-boundary producer now publishes the runtime's actual float
    # owner, so the former blanket compilation rejection is obsolete.
    assert codegen._slot_call_literal_integer_kind(operand) == 0
    with pytest.raises(L1CodegenError, match="integer producer requires literal provenance"):
        codegen._emit_slot_call_literal_integer(operand, "unproven.negative.power")

    text = _emit(source)
    verify_ir_text(text)
    body = _probe_function(text)
    assert len(assert_rooted_power(body)) == 1
    if helper is not None:
        produced = re.search(r"(%[^ ]+) = call ptr[^\n]*@" + helper
                             + r"\([^\n]*\n([^\n]+)", body)
        assert produced is not None
        assert produced.group(2).lstrip().startswith("store ptr " + produced.group(1) + ",")
    assert not _calls(body, "py_int_add")
    # This is the Python reference for the exact emitted source, not native
    # execution. The runtime source-body model separately checks 2 ** -1.
    namespace = {"slot_operand_probe": lambda value: value}
    exec(source, namespace)
    result = namespace["probe"]()
    assert type(result) is float and result == expected


def test_numeric_slot_direct_name_copies_authoritative_source_root():
    # Direct names copy their independently authoritative source root;
    # their numeric annotation is not a proof of an exact runtime kind.
    direct = _emit("def probe(value: int):\n    return slot_operand_probe(value)\n")
    direct = _probe_function(direct)
    owned = re.search(r"%value\.owned[^ ]* = alloca i1", direct) is not None
    helper = "pcc_gc_root_copy_lease" if owned else "pcc_gc_root_copy_borrowed_lease"
    copies = [_with_slot_origins(direct, call) for call in _calls(direct, helper)]
    assert any("value.addr" in call for call in copies)
    other = "pcc_gc_root_copy_borrowed_lease" if owned else "pcc_gc_root_copy_lease"
    assert not _calls(direct, other)


def test_non_integer_binop_publishes_before_operand_cleanup():
    # Use runtime strings: two string literals fold before concat lowering.
    # Runtime object dispatch publishes its actual owner before releasing
    # either operand. The independent stale-result test remains a rejection.
    text = _emit("""def probe(left: str, right: str):
    return slot_operand_probe(left + right)
""")
    match = re.search(r"(%[^ ]+) = call ptr[^\n]*@py_obj_add\([^\n]*\n([^\n]+)", text)
    assert match is not None
    assert "store ptr " + match.group(1) in match.group(2)
    assert _calls(text, "pcc_gc_foreign_lease_acquire")
    assert _calls(text, "pcc_gc_foreign_lease_release")


def test_literal_integer_producer_does_not_admit_machine_arithmetic():
    ordinary = _emit("""from pcc import i64, u64
def signed(value: i64) -> i64:
    return value + 1
def unsigned(value: u64) -> u64:
    return value << 1
""")
    assert "add i64" in ordinary
    assert "shl i64" in ordinary
    assert not _calls(ordinary, "py_int_add")
    assert not _calls(ordinary, "py_int_shl")
    # Even entirely literal children must not override explicit lane types.
    module = type_infer.infer_module(parse_and_lift("", "machine.py", "machine"))
    codegen = SlotProbeCodegen(module, ir_scaffold_mode="on")
    for name in ("pcc.i64", "pcc.u64"):
        machine = IntType(name=name)
        literal = IntLit(span=None, ty=machine, value=1)
        expr = BinOp(span=None, ty=machine, op="+", lhs=literal, rhs=literal)
        assert codegen._slot_call_literal_integer_kind(expr) == 0
    from inspect import Parameter, signature
    from pcc.frontends.python.codegen.host_contract import L1_CODEGEN_HOST_METHODS
    from pcc.frontends.python.codegen._l1_codegen_static_methods import L1_CODEGEN_STATIC_METHODS
    from pcc.frontends.python.codegen.layer1_support import _default_native_module_exports

    static = {entry["name"]: entry for entry in L1_CODEGEN_STATIC_METHODS}
    exports = _default_native_module_exports("pcc.frontends.python.codegen.layer1")
    native = {entry["name"]: entry for entry in exports["pcc.frontends.python.codegen.layer1"]["L1CodeGen"]["methods"]}
    for name, parameters in (
        ("_slot_call_literal_integer_kind", ("self", "expr")),
        ("_emit_slot_call_literal_integer", ("self", "expr", "label")),
    ):
        assert name in L1_CODEGEN_HOST_METHODS
        assert static[name] == native[name]
        assert tuple(item["name"] for item in static[name]["call_sig"]) == parameters
        assert all(item["kind"] == "pos" and not item["has_default"]
                   for item in static[name]["call_sig"])
        actual = signature(getattr(L1CodeGen, name)).parameters
        assert tuple(actual) == parameters
        assert all(item.kind == Parameter.POSITIONAL_OR_KEYWORD
                   and item.default == Parameter.empty for item in actual.values())


def test_original_integer_constructors_pipeline_ir_only(tmp_path, monkeypatch):
    from test_ordinary_int_object_boundaries import CONSTRUCTORS
    from pcc.frontends.python.pipeline import compile_python
    from tests.owned_ir_validation import verify_ir_text

    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    source = tmp_path / "ordinary_int_constructors.py"
    output = source.with_suffix(".ll")
    source.write_text(CONSTRUCTORS)
    compile_python(str(source), str(output), emit_llvm_only=True, backend="self",
                   libpython_mode="off", ir_scaffold_mode="on",
                   target_triple="x86_64-unknown-linux-gnu")
    text = output.read_text()
    verify_ir_text(text)
    assert _calls(text, "py_obj_call_slots")
    assert _calls(text, "py_int_shl")
    assert _calls(text, "py_int_neg")
    for cls in ("Box", "Row"):
        signature = re.search(r"^define [^\n]*@user_[^\n]*_" + cls + r"___init__\([^\n]*", text, re.M)
        assert signature is not None
        assert "i64" not in signature.group(0), signature.group(0)



def test_literal_bool_integer_leaves_preserve_kernel_preconditions():
    for expression, helper in (
        ("True << 0", "py_int_shl"),
        ("False >> 0", "py_int_shr"),
        ("1 << True", "py_int_shl"),
        ("4 >> True", "py_int_shr"),
        ("True ** False", "py_int_pow"),
        ("2 ** True", "py_int_pow"),
        ("+True", "py_int_add"),
        ("-False", "py_int_neg"),
        ("~(+True)", "py_int_xor"),
        ("(True << 0) + 1", "py_int_add"),
    ):
        text = _emit("def probe():\n    return slot_operand_probe(" + expression + ")\n")
        text = _probe_function(text)
        assert _calls(text, helper), expression
        # Primitive shift/power kernels require actual int operands, not
        # bool headers which only some bigint conversion paths understand.
        assert not _calls(text, "py_bool_from_bit"), expression
        assert not _calls(text, "py_bool_from_i1"), expression
        assert re.search(r"inttoptr i64 (?:1|3) to ptr", text), expression
    bare = _emit("def probe():\n    return slot_operand_probe(True)\n")
    bare = _probe_function(bare)
    assert _calls(bare, "py_bool_from_bit")
    assert not _calls(bare, "py_int_add")
    for expression, helper in (("True & False", "py_obj_and"),
                               ("True | False", "py_obj_or"),
                               ("True ^ False", "py_obj_xor"),
                               ("(True | False) + 1", "py_obj_or")):
        # These stay bool-valued through their actual generic bitwise call;
        # they never enter the literal-only integer primitive above.
        text = _probe_function(_emit("def probe():\n    return slot_operand_probe(" + expression + ")\n"))
        produced = re.search(r"(%[^ ]+) = call ptr[^\n]*@" + helper + r"\([^\n]*\n([^\n]+)", text)
        assert produced is not None
        assert produced.group(2).lstrip().startswith("store ptr " + produced.group(1) + ",")
    for expression in ("~True", "(~False) + 1"):
        # Direct bool inversion still requires the qualified 3.15 warning.
        with pytest.raises(L1CodegenError, match="proven literal-derived integer tree"):
            _emit("def probe():\n    return slot_operand_probe(" + expression + ")\n")
