"""Keep dynamic floor division's runtime target and publish before caller cleanup."""
from __future__ import annotations

import re

import pytest

from pcc.frontends.python import (
    type_infer,
)
from pcc.frontends.python.py_ast import (
    BinOp,
    ComplexType,
    DynType,
    FloatType,
    IntType,
    Name,
)
from pcc.frontends.python.py_lift import (
    parse_and_lift,
)
from tests.python.owned_regression_support import (
    assert_owned_program,
    explicit_owned_runtime,
)
from tests.python.test_shared_call_binding import (
    _emit as emit_binding,
)
from tests.python.test_slot_call_operand_roots import (
    SlotProbeCodegen,
    _calls,
    _emit as emit_operand,
)


def assert_floor_division_publication(text, count=1):
    calls = list(re.finditer(r"^  (%[^ ]+) = call [^\n]*@py_obj_floordiv\([^\n]*\)\n", text, re.M))
    assert len(calls) == count
    for call in calls:
        following = text[call.end():].splitlines()[0]
        assert following.lstrip().startswith("store ptr " + call.group(1) + ","), following
    assert _calls(text, "pcc_gc_foreign_lease_acquire")
    assert _calls(text, "pcc_gc_foreign_lease_release")
    assert not _calls(text, "py_int_to_i64_lane")


@pytest.mark.parametrize("expression,count", [
    ("left // right", 1),
    ("left // 2", 1),
    ("2 // right", 1),
    ("(left // right) // right", 2),
    ("left // (right // left)", 2),
    ("left // len(right)", 1),
    ("left[0] // right", 1),
])
def test_dynamic_floor_division_publishes_before_error_poll_and_operand_cleanup(expression, count):
    text = emit_operand("def probe(left, right):\n    return slot_operand_probe(" + expression + ")\n")
    assert_floor_division_publication(text, count)


@pytest.mark.parametrize("site", ("argument", "default", "return", "later-error"))
def test_dynamic_floor_division_rooted_contexts(site):
    source = "def take(*, value, later=None, **extras):\n    return value\ndef fail():\n    raise ValueError('later')\n"
    if site == "default":
        body = "    def target(value=left // right):\n        return value\n    return target()\n"
    elif site == "return":
        body = "    return left // right\n"
    elif site == "later-error":
        body = "    return take(value=left // right, later=fail())\n"
    else:
        body = "    return take(value=left // right)\n"
    assert_floor_division_publication(emit_binding(source + "def probe(left, right):\n" + body))


@pytest.mark.parametrize("body", ("int(a // b) if b != 0 else None", "a // b"))
def test_lambda_division_uses_the_same_runtime_operator(body):
    text = emit_binding("operations = {'//': lambda a, b: " + body + "}\n")
    assert_floor_division_publication(text)


@pytest.mark.parametrize("numeric", [
    IntType(name="pcc.i64"),
    IntType(name="pcc.u64", signed=False),
    IntType(name="int", width=32),
    FloatType(name="float", width=32),
    FloatType(name="pcc.f64"),
])
def test_dynamic_floor_division_does_not_capture_an_explicit_machine_operand(numeric):
    module = type_infer.infer_module(parse_and_lift("", "division_projection.py", "division_projection"))
    codegen = SlotProbeCodegen(module, ir_scaffold_mode="on")
    left = Name(span=None, ty=numeric, ident="left")
    right = Name(span=None, ty=DynType(name="dyn"), ident="right")
    expr = BinOp(span=None, ty=DynType(name="dyn"), op="//", lhs=left, rhs=right)
    assert codegen._slot_call_binary_runtime(expr, object_boundary=True) is None


@pytest.mark.parametrize("numeric", (IntType(name="int"), FloatType(name="float"), ComplexType(name="complex")))
def test_ordinary_numeric_object_boundary_uses_runtime(numeric):
    # Complex included: at an object boundary its division is the runtime
    # protocol too (CPython's complex division, and TypeError for //);
    # away from one every typed route is kept.
    module = type_infer.infer_module(parse_and_lift("", "typed_division.py", "typed_division"))
    codegen = SlotProbeCodegen(module, ir_scaffold_mode="on")
    left = Name(span=None, ty=numeric, ident="left")
    right = Name(span=None, ty=numeric, ident="right")
    expr = BinOp(span=None, ty=numeric, op="//", lhs=left, rhs=right)
    assert codegen._slot_call_binary_runtime(expr, object_boundary=True) == "py_obj_floordiv"
    assert codegen._slot_call_binary_runtime(expr) is None


PROGRAM = '''\
import gc

events = []
marker = {'value': 42}

class Left:
    def __floordiv__(self, other):
        gc.collect()
        events.append('divide')
        return NotImplemented
    def __del__(self):
        events.append('left-drop')

class Right:
    def __rfloordiv__(self, other):
        gc.collect()
        events.append('reflected')
        return marker
    def __del__(self):
        events.append('right-drop')

class Raising:
    def __floordiv__(self, other):
        gc.collect()
        raise KeyError('division')

def divide(left, right):
    return left // right

def take(*, value, later=None, **extras):
    gc.collect()
    events.append('take')
    return value

def later():
    events.append('later')
    raise ValueError('later')

def main():
    assert divide(7, 2) == 3
    assert divide(-7, 2) == -4
    assert divide(7, -2) == -4
    assert divide(-(1 << 200), 3) == -535646014752996758513987364113720867507400997927597611767126
    assert divide(7.0, 2) == 3.0
    assert divide(True, 2) == 0
    assert divide(1 << 100, 1 << 100) == 1.0
    for zero in (0, 0.0, False):
        try:
            divide(1, zero)
        except ZeroDivisionError:
            pass
        else:
            raise AssertionError('missing division by zero')
    events.clear()
    assert take(value=Left() // Right()) is marker
    assert events[:2] == ['divide', 'reflected']
    assert events.count('left-drop') == 1 and events.count('right-drop') == 1
    assert events[-1] == 'take'
    events.clear()
    try:
        take(value=Left() // Right(), later=later())
    except ValueError:
        pass
    else:
        raise AssertionError('missing later exception')
    gc.collect()
    assert events[-1] == 'later'
    try:
        take(value=Raising() // 1)
    except KeyError as error:
        assert error.args[0] == 'division'
    else:
        raise AssertionError('missing operator exception')
    def defaulted(left, right):
        def target(value=left // right):
            gc.collect()
            return value
        return target()
    assert defaulted(7, 2) == 3.0
    print('FLOOR_DIVISION_OWNER_NATIVE_OK')

main()
'''


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_floor_division_producer_native_five_gc(
    python_program_compiler, request, explicit_owned_runtime, tmp_path, capfd,
):
    mode = request.node.callspec.params["python_program_compiler"]
    assert_owned_program(
        PROGRAM, 'FLOOR_DIVISION_OWNER_NATIVE_OK\n', tmp_path, python_program_compiler, mode,
        explicit_owned_runtime, capfd, provenance_probe="2",
    )
    assert (tmp_path / "compiler-wrapper.stderr").read_text() == ""


def test_floor_division_nested_repetition_matches_the_original_generic_shape():
    text = emit_binding('def classes(ty):\n    return ["none"] * ((ty.slot_size + 7) // 8)\n')
    assert_floor_division_publication(text)
    assert _calls(text, "py_obj_mul_slots")


def test_floor_division_preserves_integer_null_error_check_after_publication():
    text = emit_operand('def probe(left, right):\n    return slot_operand_probe(left // right)\n')
    assert_floor_division_publication(text)
    encoded = ''.join('\\%02X' % value for value in b'division by zero')
    assert encoded in text
    assert re.search(r'@py_exc_new\(i64 9, ptr ', text)
    assert _calls(text, "py_err_occurred")
