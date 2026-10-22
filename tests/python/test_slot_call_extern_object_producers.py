"""Declared NEW extern results publish before operand or lease cleanup."""
from __future__ import annotations

import re

import pytest

from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.type_infer import infer_module


def _emit(source, pointer_lane=None):
    module = infer_module(parse_and_lift(source, "extern_publication.py", "extern_publication"))
    codegen = L1CodeGen(module, ir_scaffold_mode="on")
    codegen._strict_no_libpython = True
    if pointer_lane == "__pcc_runtime_port__":
        codegen._runtime_port_module = True
    elif pointer_lane == "__pcc_freestanding__":
        codegen._freestanding_module = True
    codegen._prefer_native_callable_values = True
    return str(codegen.generate(module))


def _published_slots(text, symbol):
    calls = list(re.finditer(r"^  (%[^ ]+) = call [^\n]*@" + symbol + r"\([^\n]*\)\n", text, re.M))
    assert calls, symbol
    slots = []
    for call in calls:
        next_line = text[call.end():].splitlines()[0]
        store = re.match(r"  store ptr " + re.escape(call[1]) + r", ptr (%[^ ,]+)", next_line)
        assert store, next_line
        slots.append(store[1])
    return slots


@pytest.mark.parametrize("site", ("receiver", "return", "argument", "default", "later-error", "assignment"))
@pytest.mark.parametrize("annotation", ("", ": int"))
def test_raw_integer_argument_cleanup_preserves_owned_extern_result(site, annotation):
    expression = "make_bytes(storage, count)"
    if site == "receiver":
        body = "    return " + expression + ".decode('utf-8', 'ignore')\n"
    elif site == "return":
        body = "    return " + expression + "\n"
    elif site == "default":
        body = "    def target(value=" + expression + "):\n        return value\n    return target()\n"
    elif site == "assignment":
        body = "    result = " + expression + "\n    return result\n"
    else:
        body = "    return take(value=" + expression + (", later=fail()" if site == "later-error" else "") + ")\n"
    text = _emit("from pcc.extern import extern, c_ptr, c_obj, c_int64\n"
                 "from pcc.unsafe import stack_alloc\n"
                 "make_bytes = extern('py_bytes_new', (c_ptr, c_int64), c_obj)\n"
                 "def take(*, value, later=None, **extras):\n    return value\n"
                 "def fail():\n    raise ValueError('later')\n"
                 "def probe(count" + annotation + "):\n    storage = stack_alloc(32)\n" + body)
    _published_slots(text, "py_bytes_new")
    assert "@pcc_gc_foreign_lease_acquire(" in text
    assert "@pcc_gc_foreign_lease_release(" in text


@pytest.mark.parametrize("signature,arguments", (("c_obj", "value"), ("c_str, c_obj", "'text', value")))
def test_managed_arguments_publish_owned_alias_before_cleanup(signature, arguments):
    text = _emit("from pcc.extern import extern, c_obj, c_str\n"
                 "identity = extern('owned_identity', (" + signature + ",), c_obj)\n"
                 "def take(*, value, **extras):\n    return value\n"
                 "def probe(value):\n    return take(value=identity(" + arguments + "))\n")
    _published_slots(text, "owned_identity")


def test_nested_externs_do_not_publish_into_the_enclosing_call_sink():
    text = _emit("from pcc.extern import extern, c_obj\n"
                 "inner = extern('owned_inner', (c_obj,), c_obj)\n"
                 "outer = extern('owned_outer', (c_obj,), c_obj)\n"
                 "def take(*, value, **extras):\n    return value\n"
                 "def probe(value):\n    return take(value=outer(inner(value)))\n")
    inner = _published_slots(text, "owned_inner")
    outer = _published_slots(text, "owned_outer")
    assert set(inner).isdisjoint(outer)


def test_annotation_does_not_turn_a_raw_result_into_an_object_owner():
    text = _emit("from pcc.extern import extern, c_rawptr\n"
                 "address = extern('raw_address', (), c_rawptr)\n"
                 "def probe() -> int:\n    return address()\n")
    assert "extern.raw_address.addr" in text
    call = re.search(r"^  (%[^ ]+) = call [^\n]*@raw_address\([^\n]*\)\n", text, re.M)
    assert call
    assert "ptrtoint" in text[call.end():].splitlines()[0]


@pytest.mark.parametrize("directive", ("__pcc_runtime_port__", "__pcc_freestanding__"))
def test_manual_pointer_module_c_obj_keeps_raw_abi(directive):
    text = _emit(directive + " = True\n"
                 "from pcc.extern import extern, c_obj, c_abi_export\n"
                 "pointer = extern('manual_pointer', (), c_obj)\n"
                 "@c_abi_export('user_extern_publication_probe')\n"
                 "def probe():\n    return pointer()\n", pointer_lane=directive)
    assert "@manual_pointer(" in text
    body = re.search(r"^define [^\n]*@user_extern_publication_probe\([^\n]*\).*?^}", text, re.M | re.S)[0]
    assert "call.slot.publish.lease" not in body
    assert "extern.object.result.operand" not in body


def test_ambiguous_pointer_return_does_not_gain_owned_object_contract():
    with pytest.raises(Exception, match="ambiguous between a Python object and a raw address"):
        _emit("from pcc.extern import extern, c_ptr\n"
              "borrowed = extern('borrowed_pointer', (), c_ptr)\n"
              "def probe():\n    return borrowed()\n")
