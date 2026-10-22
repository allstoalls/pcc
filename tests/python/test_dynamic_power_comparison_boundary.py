"""Keep admitted dynamic integer powers rooted at mixed comparisons.

Host reference and owned IR evidence only; no native acceptance is implied.
"""
from __future__ import annotations

import operator
import re

import pytest

OPS = (("eq", "==", "py_obj_eq_value"), ("ne", "!=", "py_obj_eq_value"),
       ("lt", "<", "py_obj_lt"), ("le", "<=", "py_obj_le"),
       ("gt", ">", "py_obj_gt"), ("ge", ">=", "py_obj_ge"))
FORMS = ("literal_power", "dynamic_power", "builtin_power")


def matrix_source(form):
    expression = {"literal_power": "2 ** exponent", "dynamic_power": "base ** exponent",
                  "builtin_power": "pow(base, exponent)"}[form]
    chunks = []
    for peer in ("float", "dyn"):
        annotation = ": float" if peer == "float" else ""
        for order in ("left", "right"):
            for name, symbol, _helper in OPS:
                comparison = f"{expression} {symbol} value" if order == "left" else f"value {symbol} {expression}"
                chunks.append(f"def {form}_{peer}_{order}_{name}(base: int, exponent: int, value{annotation}) -> bool:\n    return {comparison}\n")
    return "\n".join(chunks)


LIFETIME_SOURCE = '''events = []
def mark_base(value: int) -> int:
    events.append(1)
    return value
def mark_exponent(value: int) -> int:
    events.append(2)
    return value
def mark_peer(value: float) -> float:
    events.append(3)
    return value
def fail_peer() -> float:
    events.append(4)
    raise ValueError("peer failed")
def ordered_left(base: int, exponent: int, value: float) -> bool:
    return mark_base(base) ** mark_exponent(exponent) < mark_peer(value)
def ordered_right(base: int, exponent: int, value: float) -> bool:
    return mark_peer(value) > pow(mark_base(base), mark_exponent(exponent))
def later_raises(base: int, exponent: int) -> bool:
    return base ** exponent < fail_peer()
def power_raises(base: int, exponent: int, value: float) -> bool:
    return mark_base(base) ** mark_exponent(exponent) < mark_peer(value)
def nested_power(base: int, exponent: int, value: float) -> bool:
    return (base ** exponent) + 1 < value
def negated_power(base: int, exponent: int, value: float) -> bool:
    return -(base ** exponent) < value
'''


def function(ir, name):
    found = re.search(r"(?ms)^define [^\n]*@user_[^\n(]*_" + re.escape(name)
                      + r"\([^\n]*\n.*?^\}", ir)
    assert found is not None, name
    return found.group(0)


def compile_ir(tmp_path, monkeypatch, source, label):
    from pcc.frontends.python.pipeline import compile_python
    from tests.owned_ir_validation import verify_ir_text

    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    path = tmp_path / (label + ".py")
    path.write_text(source)
    output = path.with_suffix(".ll")
    compile_python(str(path), str(output), emit_llvm_only=True, python_library=True,
                   libpython_mode="off", ir_scaffold_mode="on", backend="self",
                   target_triple="arm64-apple-darwin")
    text = output.read_text()
    verify_ir_text(text)
    return text


def root_clear_count(body, slot):
    # Owned IR inserts opaque-pointer bitcasts before the ABI call. Follow
    # only aliases proven to originate at this exact output slot.
    aliases = {slot}
    pairs = re.findall(r"(?m)^  (%[^ ]+) = bitcast ptr (%[^ ]+) to ptr$", body)
    changed = True
    while changed:
        changed = False
        for destination, source in pairs:
            if source in aliases and destination not in aliases:
                aliases.add(destination)
                changed = True
    clears = re.findall(r"@pcc_gc_store_root\(ptr (%[^,]+), ptr null\)", body)
    return sum(value in aliases for value in clears)


def assert_rooted_power(body):
    handoffs = re.findall(r"(?m)^  (%[^ ]+) = call ptr[^\n]*@py_int_pow\([^\n]*\)\n"
                          r"  store ptr \1, ptr (%[^,\n]+)", body)
    assert handoffs, body
    for _result, slot in handoffs:
        # The very first instruction after the NEW producer is the root
        # handoff, ahead of all TLS checks, leases and operand cleanup.
        assert root_clear_count(body, slot) > 0, slot
    assert "@pcc_gc_foreign_lease_acquire" in body
    assert "@pcc_gc_foreign_lease_release" in body
    assert "@py_tls_exc_swap_slot" in body
    assert not re.search(r"\bcall\b[^\n]*@py_(?:int_to_i64|int_to_i64_lane|int_value_i64|obj_index_i64)\(", body)
    return handoffs


def test_dynamic_power_comparison_host_reference():
    functions = {"eq": operator.eq, "ne": operator.ne, "lt": operator.lt,
                 "le": operator.le, "gt": operator.gt, "ge": operator.ge}
    for form in FORMS:
        namespace = {}
        exec(matrix_source(form), namespace)
        for base, exponent, value in ((2, 2, 8.0), (2, -2, 1.0), (2, 80, float(1 << 80)),
                                      (3, 5, 244.0), (2, 53, (1 << 53) + 1)):
            power = (2 if form == "literal_power" else base) ** exponent
            for peer in ("float", "dyn"):
                for order in ("left", "right"):
                    for name, _symbol, _helper in OPS:
                        actual_peer = float(value) if peer == "float" else value
                        expected = functions[name](power, actual_peer) if order == "left" else functions[name](actual_peer, power)
                        assert namespace[f"{form}_{peer}_{order}_{name}"](base, exponent, actual_peer) is expected
    assert isinstance(2 ** -2, float) and 2 ** -2 == 0.25
    namespace = {}
    exec(LIFETIME_SOURCE, namespace)
    for name, order in (("ordered_left", [1, 2, 3]), ("ordered_right", [3, 1, 2])):
        namespace["events"].clear()
        assert namespace[name](2, -2, 1.0) is True
        assert namespace["events"] == order
    namespace["events"].clear()
    with pytest.raises(ValueError, match="peer failed"):
        namespace["later_raises"](2, -2)
    assert namespace["events"] == [4]
    namespace["events"].clear()
    with pytest.raises(ZeroDivisionError):
        namespace["power_raises"](0, -2, 1.0)
    assert namespace["events"] == [1, 2]


@pytest.mark.parametrize("form", FORMS)
def test_dynamic_power_comparison_owned_ir(tmp_path, monkeypatch, form):
    text = compile_ir(tmp_path, monkeypatch, matrix_source(form), form)
    for peer in ("float", "dyn"):
        for order in ("left", "right"):
            for name, _symbol, helper in OPS:
                body = function(text, f"{form}_{peer}_{order}_{name}")
                assert_rooted_power(body)
                assert re.search(r"\bcall\b[^\n]*@" + helper + r"\(", body)


def test_dynamic_power_source_order_and_error_cleanup_ir(tmp_path, monkeypatch):
    text = compile_ir(tmp_path, monkeypatch, LIFETIME_SOURCE, "power_lifetimes")
    for name, expected in (("ordered_left", ["mark_base", "mark_exponent", "mark_peer"]),
                           ("ordered_right", ["mark_peer", "mark_base", "mark_exponent"])):
        body = function(text, name)
        assert_rooted_power(body)
        calls = re.findall(r"\bcall\b[^\n]*@user_[^\s(]*_(mark_base|mark_exponent|mark_peer)\(", body)
        assert calls == expected, (name, calls)
    for name in ("later_raises", "power_raises", "nested_power", "negated_power"):
        body = function(text, name)
        slots = assert_rooted_power(body)
        for _result, slot in slots:
            assert root_clear_count(body, slot) >= 2


def test_power_admission_rejects_shadowed_and_machine_routes(monkeypatch):
    from types import SimpleNamespace
    from pcc.frontends.python.codegen import call_object_lowering as calls
    from pcc.frontends.python.codegen.binary_op_lowering import BinaryOpLoweringMixin
    from pcc.frontends.python.py_ast import BinOp, BoolType, Call, DynType, FloatType, IntLit, IntType, Name

    integer = IntType(name="int")
    argument = Name(span=None, ty=integer, ident="exponent")
    literal = IntLit(span=None, ty=integer, value=2)
    expression = Call(span=None, ty=integer,
                      func=Name(span=None, ty=DynType(name="dyn"), ident="pow"),
                      args=(literal, argument), kwargs=())
    owner = SimpleNamespace(env={}, _module_globals={}, functions={},
                            class_lowering=SimpleNamespace(classes={}),
                            _native_builtin_value_aliases={},
                            _native_builtin_value_for_name=lambda name: None,
                            _has_starred_unpack=lambda args: False,
                            _has_unpacked_operands=lambda args, kwargs: False)
    monkeypatch.setattr(calls, "live_import_name_slot", lambda owner, name: None)
    select = calls.CallObjectLoweringMixin._slot_call_builtin_power_expr
    assert select(owner, expression).op == "**"
    for mapping in (owner.env, owner._module_globals, owner.functions,
                    owner.class_lowering.classes, owner._native_builtin_value_aliases):
        mapping["pow"] = object()
        assert select(owner, expression) is None
        mapping.clear()
    monkeypatch.setattr(calls, "live_import_name_slot", lambda owner, name: object())
    assert select(owner, expression) is None
    monkeypatch.setattr(calls, "live_import_name_slot", lambda owner, name: None)
    owner._native_builtin_value_for_name = lambda name: "math.pow"
    assert select(owner, expression) is None

    owner.current_function = None
    owner._is_valueclass_payload_type = lambda ty: False
    owner._expr_looks_cpython = lambda expr: False
    owner._expr_returns_unsafe_raw_pointer = lambda expr: False
    route = BinaryOpLoweringMixin._slot_call_binary_runtime
    owner._guard_integer_power_operand_provenance = (
        lambda lhs, rhs: BinaryOpLoweringMixin._guard_integer_power_operand_provenance(owner, lhs, rhs)
    )
    power = BinOp(span=None, ty=integer, op="**", lhs=literal, rhs=argument)
    assert route(owner, power, object_boundary=True) == "py_int_pow"
    assert route(owner, power, object_boundary=False) is None
    for rejected in (IntType(name="pcc.i64"), IntType(name="pcc.u64", signed=False)):
        other = Name(span=None, ty=rejected, ident="exponent")
        mixed = BinOp(span=None, ty=integer, op="**", lhs=literal, rhs=other)
        assert route(owner, mixed, object_boundary=True) is None
    # An ordinary float exponent never enters the exact int kernel either: at
    # an object boundary it takes the runtime power protocol, where a negative
    # base to a fractional power is complex; away from one the float kernel
    # keeps its typed lane.
    floating = FloatType(name="float")
    mixed = BinOp(span=None, ty=floating, op="**", lhs=literal,
                  rhs=Name(span=None, ty=floating, ident="exponent"))
    assert route(owner, mixed, object_boundary=True) == "py_obj_pow"
    assert route(owner, mixed, object_boundary=False) is None
    # A dynamic operand never enters the exact int kernel: it takes the
    # runtime power protocol, which dispatches by the operand's actual tag.
    dynamic = Name(span=None, ty=DynType(name="dyn"), ident="exponent")
    mixed = BinOp(span=None, ty=integer, op="**", lhs=literal, rhs=dynamic)
    assert route(owner, mixed, object_boundary=True) == "py_obj_pow"
    for flag in ("_freestanding_module", "_runtime_port_module"):
        setattr(owner, flag, True)
        assert route(owner, power, object_boundary=True) is None
        setattr(owner, flag, False)
    owner.current_function = SimpleNamespace(name="manual")
    owner._manual_pointer_abi_functions = {"manual"}
    assert route(owner, power, object_boundary=True) is None
    owner._manual_pointer_abi_functions = set()
    owner._c_abi_export_symbols = {"manual"}
    assert route(owner, power, object_boundary=True) is None
