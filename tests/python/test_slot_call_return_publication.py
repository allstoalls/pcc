"""Actual Call producers publish before error checks or argument cleanup.

These exercise host frontend lowering and its emitted root protocol. Native
five-GC execution remains an independent qualification gate.
"""
from __future__ import annotations

import re

import pytest

from tests.python.test_slot_call_operand_roots import (
    _emit,
    _probe_function,
)


def _assert_immediate_publication(body, producer):
    matches = list(re.finditer(
        r"^  (%[^ ]+) = " + producer + r"[^\n]*\n", body, re.M,
    ))
    assert matches, producer
    for match in matches:
        next_line = body[match.end():].splitlines()[0].strip()
        assert next_line.startswith("store ptr " + match.group(1) + ","), next_line
        assert ".operand" in next_line


def test_void_call_result_publishes_actual_none_materialization():
    body = _probe_function(_emit(
        "def noop() -> None:\n    pass\n"
        "def probe():\n    return slot_operand_probe(noop())\n"
    ))
    assert "call void () @user_slot_operand_noop(" in body
    _assert_immediate_publication(body, r"load ptr, ptr @py_None")


@pytest.mark.parametrize("element_type", ("", "[int]", "[str]"))
@pytest.mark.parametrize("index", ("", "0"))
def test_list_pop_transfer_publishes_before_error_check(element_type, index):
    body = _probe_function(_emit(
        "def probe(values: list" + element_type + "):\n"
        "    return slot_operand_probe(values.pop(" + index + "))\n"
    ))
    _assert_immediate_publication(body, r"call ptr (?:\([^\n]*?\) )?@py_list_pop\(")
    assert "@py_int_to_i64(" not in body
    assert "@py_cpy_" not in body


def test_boxed_dunder_len_publishes_before_receiver_unpin():
    body = _probe_function(_emit(
        "class Sized:\n"
        "    def __len__(self) -> int:\n        return 7\n"
        "def probe(value: Sized):\n"
        "    return slot_operand_probe(len(value))\n"
    ))
    _assert_immediate_publication(body, r"call ptr (?:\([^\n]*?\) )?@user_slot_operand_Sized___len__\(")
    assert body.index("@user_slot_operand_Sized___len__(") < body.index("@pcc_gc_unpin(")
    assert "@py_int_to_i64(" not in body


@pytest.mark.parametrize("arguments,helper", (
    ("value, name='after'", "py_dataclass_replace"),
    ("value, **changes", "py_dataclass_replace_from_dict"),
))
def test_dataclass_replace_publishes_actual_new_instance(arguments, helper):
    body = _probe_function(_emit(
        "from dataclasses import replace\n"
        "def probe(value, changes):\n"
        "    return slot_operand_probe(replace(" + arguments + "))\n"
    ))
    _assert_immediate_publication(body, r"call ptr (?:\([^\n]*?\) )?@" + helper + r"\(")
    assert "@py_cpy_" not in body



def test_traceback_format_result_is_owned_at_runtime_return():
    body = _probe_function(_emit(
        "import traceback\n"
        "def probe():\n"
        "    return slot_operand_probe(traceback.format_exc())\n"
    ))
    _assert_immediate_publication(
        body, r"call ptr (?:\([^\n]*?\) )?@py_exc_traceback_format_exc\(",
    )
    assert "@py_cpy_" not in body


@pytest.mark.parametrize("annotation", ("bytes", "bytearray", "memoryview"))
def test_indexed_comprehension_elements_use_owned_loop_slots(annotation):
    body = _probe_function(_emit(
        "def probe(values: " + annotation + "):\n"
        "    return [str(value) for value in values]\n"
    ))
    assert "@py_obj_getitem(" in body
    assert "@pcc_gc_store_root_take(" in body
    assert "value.for.obj.addr" in body
    assert "@pcc_gc_root_copy_lease(" in body
    assert "@py_cpy_" not in body

def test_boxed_len_sink_rejects_the_resolved_raw_method_abi():
    from pcc.frontends.python.codegen.errors import L1CodegenError

    with pytest.raises(L1CodegenError, match="raw-pointer method ABI cannot publish"):
        _emit(
            "from pcc.extern import c_ptr\n"
            "from pcc.unsafe import int_to_ptr\n"
            "__pcc_runtime_port__ = True\n"
            "class Unsafe:\n"
            "    def __len__(self) -> c_ptr:\n"
            "        return int_to_ptr(4096)\n"
            "def probe(value: Unsafe):\n"
            "    return slot_operand_probe(len(value))\n"
        )
