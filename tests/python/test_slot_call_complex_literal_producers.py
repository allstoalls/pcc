"""Complex literals publish their actual NEW object before managed handoff."""
from __future__ import annotations

import re

import pytest

from tests.python.owned_regression_support import (
    assert_owned_program,
    explicit_owned_runtime,
)
from tests.python.test_shared_call_binding import (
    _emit as _emit_binding,
)
from tests.python.test_slot_call_operand_roots import (
    _emit as _emit_operand,
)


def _assert_complex_publication(text, count):
    calls = list(re.finditer(
        r"^  (%[^ ]+) = call [^\n]*@py_complex_new\([^\n]*\)\n",
        text,
        re.M,
    ))
    assert len(calls) == count
    for call in calls:
        following = text[call.end():].splitlines()[0]
        assert following.lstrip().startswith("store ptr " + call.group(1) + ","), following
    assert "@pcc_gc_foreign_lease_acquire(" in text
    assert "@pcc_gc_foreign_lease_release(" in text


@pytest.mark.parametrize("expression,count", (
    ("2j", 1),
    ("1 + 2j", 1),
    ("2j + value", 1),
    ("value + 2j", 1),
    ("[2j, 3j]", 2),
    ("(2j, 3j)", 2),
))
def test_complex_literal_operand_publishes_actual_new_result(expression, count):
    text = _emit_operand(
        "def probe(value):\n    return slot_operand_probe(" + expression + ")\n"
    )
    _assert_complex_publication(text, count)


@pytest.mark.parametrize("site", ("argument", "default", "return", "later-error", "module"))
def test_complex_literal_producer_context_matrix(site):
    prelude = (
        "def take(*, value, later=None, **extras):\n    return value\n"
        "def fail():\n    raise ValueError('later')\n"
    )
    if site == "default":
        body = "    def target(value=2j):\n        return value\n    return target()\n"
    elif site == "return":
        body = "    return 2j\n"
    elif site == "later-error":
        body = "    return take(value=2j, later=fail())\n"
    else:
        body = "    return take(value=2j)\n"
    source = prelude + "def probe():\n" + body
    if site == "module":
        source = prelude + "value = 2j\n"
    _assert_complex_publication(_emit_binding(source), 1)


# Unchanged failed native control; assert/source edits cannot hide this boundary.
MIXED_NUMERIC_CALLBACKS = """\
import gc
events = []
state = {'answer': 42}
class Forward:
    def __add__(self, other):
        gc.collect()
        events.append('forward')
        return state
class Reflected:
    def __radd__(self, other):
        gc.collect()
        events.append('reflected')
        return state
class Base:
    def __add__(self, other):
        events.append('base')
        return NotImplemented
class Derived(Base):
    def __radd__(self, other):
        gc.collect()
        events.append('derived')
        return state
def add(left, right):
    return left + right
def main():
    assert add(1.25, Reflected()) is state
    assert add(1 + 2j, Reflected()) is state
    assert add(Forward(), 1.25) is state
    assert add(Forward(), 1 + 2j) is state
    assert events == ['reflected', 'reflected', 'forward', 'forward']
    assert add(Base(), Derived()) is state
    assert events[-1] == 'derived'
    assert 'base' not in events
    print('BINARY_MIXED_CALLBACKS_OK')
main()
"""


def test_unchanged_mixed_numeric_callback_program_emits_complex_owners():
    _assert_complex_publication(_emit_binding(MIXED_NUMERIC_CALLBACKS), 2)


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_complex_literals_native_five_gc(python_program_compiler, request,
                                         explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params["python_program_compiler"]
    assert_owned_program(
        MIXED_NUMERIC_CALLBACKS, "BINARY_MIXED_CALLBACKS_OK\n", tmp_path,
        python_program_compiler, mode, explicit_owned_runtime, capfd,
        provenance_probe="2",
    )
