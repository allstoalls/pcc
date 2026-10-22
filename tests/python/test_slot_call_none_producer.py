"""Exact singleton provenance for Call expressions materializing None."""
from types import SimpleNamespace
import re

import pytest

from pcc.frontends.python.codegen.errors import L1CodegenError
from pcc.frontends.python.codegen import literal_lowering
from tests.python.test_shared_call_binding import _emit
from tests.python.test_slot_call_operand_roots import _emit as _emit_operand
from tests.python.test_slot_call_weakref_constructor import PROGRAM


def test_none_literal_records_only_the_exact_singleton_load(monkeypatch):
    singleton_global = object()
    singleton_value = object()
    unrelated_value = object()
    recorded = []

    def declare(module, name):
        assert module == "module" and name == "py_None"
        return singleton_global

    def load(global_value, name):
        assert global_value is singleton_global and name == "none.result"
        return singleton_value

    monkeypatch.setattr(literal_lowering, "declare_runtime_global", declare)
    host = SimpleNamespace(
        module="module", builder=SimpleNamespace(load=load),
        _fresh=lambda label: label + ".result",
        _note_never_gc_object=recorded.append,
    )
    result = literal_lowering.LiteralLoweringMixin._emit_none_literal(host)
    assert result is singleton_value
    assert recorded == [singleton_value]
    assert unrelated_value not in recorded


def _assert_none_published(text, root_label):
    publications = list(re.finditer(
        r"(%[^ ]+) = load ptr, ptr @py_None\n\s+store ptr \1, ptr (%[^,\n]+)",
        text,
    ))
    assert any(root_label in match.group(2) for match in publications)


def test_list_append_call_result_publishes_exact_none_into_operand():
    text = _emit_operand(
        "def probe(values: list):\n"
        "    return slot_operand_probe(values.append('value'))\n"
    )
    assert "@py_list_append(" in text
    _assert_none_published(text, "probe.operand")


def test_weakref_lambda_side_effect_body_publishes_exact_none():
    text = _emit(
        "import weakref\n"
        "events = []\n"
        "def probe(owner):\n"
        "    return weakref.ref(owner, lambda reference: events.append('lambda'))\n"
    )
    assert "@py_list_append(" in text
    assert "@py_weakref_new(" in text
    _assert_none_published(text, "lambda.body.result")
    assert "strict.nolib.stub" not in text


def test_raw_pointer_call_still_cannot_enter_a_managed_operand():
    with pytest.raises(L1CodegenError, match="raw pointer cannot be a slot-call operand"):
        _emit_operand(
            "from pcc.extern import c_ptr\n"
            "from pcc.unsafe import int_to_ptr\n"
            "__pcc_runtime_port__ = True\n"
            "def address() -> c_ptr:\n"
            "    return int_to_ptr(4096)\n"
            "def probe():\n"
            "    return slot_operand_probe(address())\n"
        )


def test_always_raising_callback_expression_retains_original_error_path():
    text = _emit(
        "import weakref\n"
        "def later_failure():\n"
        "    raise ValueError('callback expression')\n"
        "def probe(owner):\n"
        "    return weakref.ref(owner, later_failure())\n"
    )
    assert "@user_binding_later_failure(" in text
    assert "@py_weakref_new(" in text
    assert "strict.nolib.stub" not in text


def test_complete_unchanged_weakref_callback_program_emits_without_fallback():
    text = _emit(PROGRAM)
    assert "@py_weakref_new(" in text
    assert "@py_list_append(" in text
    assert "strict.nolib.stub" not in text
