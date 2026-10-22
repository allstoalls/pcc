"""Generic subtraction publishes NEW into an owner before cleanup."""
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


def assert_subtraction_publication(text, count=1):
    calls = list(re.finditer(r"^  (%[^ ]+) = call [^\n]*@py_obj_sub\([^\n]*\)\n", text, re.M))
    assert len(calls) == count
    for call in calls:
        following = text[call.end():].splitlines()[0]
        assert following.lstrip().startswith("store ptr " + call.group(1) + ","), following
    assert _calls(text, "pcc_gc_foreign_lease_acquire")
    assert _calls(text, "pcc_gc_foreign_lease_release")
    assert not _calls(text, "py_int_to_i64_lane")


@pytest.mark.parametrize("expression,count", [
    ("left - right", 1),
    ("left - 1", 1),
    ("1 - right", 1),
    ("(left - right) - right", 2),
    ("left - (right - left)", 2),
    ("left - len(right)", 1),
    ("left[0] - right", 1),
])
def test_subtraction_object_operands_publish_before_cleanup(expression, count):
    text = emit_operand("def probe(left, right):\n    return slot_operand_probe(" + expression + ")\n")
    assert_subtraction_publication(text, count)


@pytest.mark.parametrize("site", ("argument", "default", "return", "later-error"))
def test_subtraction_rooted_contexts(site):
    source = "def take(*, value, later=None, **extras):\n    return value\ndef fail():\n    raise ValueError('later')\n"
    if site == "default":
        body = "    def target(value=left - right):\n        return value\n    return target()\n"
    elif site == "return":
        body = "    return left - right\n"
    elif site == "later-error":
        body = "    return take(value=left - right, later=fail())\n"
    else:
        body = "    return take(value=left - right)\n"
    assert_subtraction_publication(emit_binding(source + "def probe(left, right):\n" + body))


@pytest.mark.parametrize("annotation", ("int", "float"))
def test_subtraction_annotations_do_not_prove_a_primitive_kernel(annotation):
    text = emit_operand("def probe(left: " + annotation + ", right: " + annotation + "):\n    return slot_operand_probe(left - right)\n")
    assert_subtraction_publication(text)
    assert not _calls(text, "py_int_sub")
    assert not _calls(text, "py_float_sub")


@pytest.mark.parametrize("numeric", [
    IntType(name="pcc.i64"),
    IntType(name="pcc.u64", signed=False),
    IntType(name="int", width=8),
    IntType(name="int", width=32),
    IntType(name="int", signed=False),
    FloatType(name="float", width=32),
    FloatType(name="pcc.f64"),
])
def test_subtraction_keeps_separate_explicit_routes(numeric):
    module = type_infer.infer_module(parse_and_lift("", "subtraction_projection.py", "subtraction_projection"))
    codegen = SlotProbeCodegen(module, ir_scaffold_mode="on")
    left = Name(span=None, ty=numeric, ident="left")
    right = Name(span=None, ty=numeric, ident="right")
    for result_ty in (numeric, DynType(name="dyn")):
        expr = BinOp(span=None, ty=result_ty, op="-", lhs=left, rhs=right)
        assert codegen._slot_call_binary_runtime(expr, object_boundary=True) is None


def test_complex_subtraction_uses_the_runtime_only_at_an_object_boundary():
    module = type_infer.infer_module(parse_and_lift("", "subtraction_complex.py", "subtraction_complex"))
    codegen = SlotProbeCodegen(module, ir_scaffold_mode="on")
    complex_ty = ComplexType(name="complex")
    left = Name(span=None, ty=complex_ty, ident="left")
    right = Name(span=None, ty=complex_ty, ident="right")
    for result_ty in (complex_ty, DynType(name="dyn")):
        expr = BinOp(span=None, ty=result_ty, op="-", lhs=left, rhs=right)
        assert codegen._slot_call_binary_runtime(expr, object_boundary=True) == "py_obj_sub"
        assert codegen._slot_call_binary_runtime(expr) is None


def test_subtraction_retains_the_literal_integer_kernel():
    text = emit_operand("def probe():\n    return slot_operand_probe((1 << 100) - 1)\n")
    assert _calls(text, "py_int_sub")
    assert not _calls(text, "py_obj_sub")


PROGRAM = '''\
import gc

events = []
marker = {'value': 42}

class Left:
    def __sub__(self, other):
        gc.collect()
        events.append('sub')
        return NotImplemented
    def __del__(self):
        events.append('left-drop')

class Right:
    def __rsub__(self, other):
        gc.collect()
        events.append('rsub')
        return marker
    def __del__(self):
        events.append('right-drop')

class Raising:
    def __sub__(self, other):
        gc.collect()
        raise KeyError('subtraction')

def subtract(left, right):
    return left - right

def take(*, value, later=None, **extras):
    gc.collect()
    events.append('take')
    return value

def later():
    events.append('later')
    raise ValueError('later')

def main():
    big = 1 << 200
    assert take(value=big - 1) == big - 1
    assert subtract(-big, big) == -(1 << 201)
    assert subtract(1.25, 0.5) == 0.75
    assert subtract(True, False) == 1
    events.clear()
    assert take(value=Left() - Right()) is marker
    assert events[:2] == ['sub', 'rsub']
    assert events.count('left-drop') == 1 and events.count('right-drop') == 1
    assert events[-1] == 'take'
    events.clear()
    try:
        take(value=Left() - Right(), later=later())
    except ValueError:
        pass
    else:
        raise AssertionError('missing later exception')
    gc.collect()
    assert events[-1] == 'later'
    assert events.count('left-drop') == 1 and events.count('right-drop') == 1
    try:
        take(value=Raising() - 1)
    except KeyError as error:
        assert error.args[0] == 'subtraction'
    else:
        raise AssertionError('missing operator exception')
    def target(value=big - 1):
        gc.collect()
        return value
    assert target() == big - 1
    print('SUBTRACTION_OWNER_NATIVE_OK')

main()
'''


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_subtraction_producer_native_five_gc(
    python_program_compiler, request, explicit_owned_runtime, tmp_path, capfd,
):
    mode = request.node.callspec.params["python_program_compiler"]
    assert_owned_program(
        PROGRAM, 'SUBTRACTION_OWNER_NATIVE_OK\n', tmp_path, python_program_compiler, mode,
        explicit_owned_runtime, capfd, provenance_probe="2",
    )
    assert (tmp_path / "compiler-wrapper.stderr").read_text() == ""
