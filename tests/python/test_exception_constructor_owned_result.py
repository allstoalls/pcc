"""Actual-IR checks for the existing builtin exception producer's handoff.

Constructor dispatch, including its existing keyword semantics, is unchanged.
These tests do not qualify the separate runtime exception-class-call path.
"""
from __future__ import annotations

import re

import pytest


def _emit(tmp_path, monkeypatch, source):
    from pcc.frontends.python.pipeline import compile_python
    from tests.owned_ir_validation import verify_ir_text

    monkeypatch.setenv("PCC_PYTHON_IR_PASSES", "off")
    path = tmp_path / "exception_owner.py"
    path.write_text(source)
    output = path.with_suffix(".ll")
    compile_python(str(path), str(output), emit_llvm_only=True, python_library=True,
                   libpython_mode="off", ir_scaffold_mode="on", backend="self",
                   target_triple="x86_64-unknown-linux-gnu")
    text = output.read_text()
    return verify_ir_text(text), text


def _body(text):
    match = re.search(r"(?ms)^define[^\n]*@user_exception_owner_make_value\([^\n]*\).*?^}", text)
    assert match is not None
    return match.group(0)


def _producer_contract(module, symbol, has_argument):
    from pcc.backend.self_backend_kernel import get_indexed_function_kernel
    from tests.python.root_slot_contract import RootSlotContract

    function = next(fn for fn in module.functions if fn.name == "user_exception_owner_make_value")
    blocks = get_indexed_function_kernel(function).materialize_legacy_blocks(function)
    addresses = RootSlotContract(blocks, module.globals_)
    rows = addresses.rows
    slot, called, args = addresses.slot, addresses.called, addresses.args
    invocations = [(block, index, ins) for block, index, ins in rows
                   if called(ins, symbol) and args(ins)[0] == "2"]
    assert len(invocations) == 1
    block, index, invoke = invocations[0]
    published = block.instructions[index + 1]
    assert published.kind == "store" and published.data[1] == invoke.data[0], (
        "exception producer must publish before any TLS, lease or cleanup call"
    )
    output = slot(published.data[3])
    addresses.require_owning(output, allow_lifo=True)
    loads = {ins.data[0]: ins.data[3] for _, _, ins in rows if ins.kind == "load"}
    argument = slot(loads[args(invoke)[1]]) if has_argument else None
    if argument is not None:
        assert argument != output
        addresses.require_owning(argument, allow_lifo=True)
    by_name = {candidate.name: candidate for candidate in blocks}
    current, offset = block, index + 2
    path, seen, consumed = [], set(), False
    while current.name not in seen:
        seen.add(current.name)
        for ins in current.instructions[offset:]:
            path.append(ins)
            if called(ins, "pcc_gc_store_root") and args(ins)[1] == "null":
                assert slot(args(ins)[0]) != output, "exception result cleared before consumer"
            if called(ins, "pcc_gc_frame_leave_lifo"):
                assert slot(args(ins)[0]) != output, "exception result retired before consumer"
            if called(ins, "py_list_append"):
                consumed = any(value in loads and slot(loads[value]) == output for value in args(ins))
                if consumed:
                    break
        if consumed:
            break
        term = current.terminator
        if term.kind == "br":
            current = by_name[term.data[0]]
        elif term.kind == "br_cond":
            current = by_name[term.data[2]]
        else:
            break
        offset = 0
    assert consumed, "exact exception output root must reach its retaining call-argument consumer"
    assert any(called(ins, "py_err_occurred") for ins in path)
    if argument is not None:
        releases = [ins for ins in path if called(ins, "pcc_gc_foreign_lease_release")
                    and slot(args(ins)[0]) == argument]
        assert releases
        token = args(releases[0])[1]
        assert any(called(ins, "pcc_gc_foreign_lease_acquire") and ins.data[0] == token
                   and slot(args(ins)[0]) == argument for _, _, ins in rows)
        clears = [ins for ins in path if called(ins, "pcc_gc_store_root")
                  and args(ins)[1] == "null" and slot(args(ins)[0]) == argument]
        assert len(clears) == 1
        assert path.index(releases[0]) < path.index(clears[0])
    assert any(called(ins, "py_tls_exc_swap_slot") for _, _, ins in rows)
    addresses.assert_frame_exits()


@pytest.mark.parametrize("expression,symbol,has_argument", (
    ("ValueError()", "py_exc_new", False),
    ("ValueError('sentinel')", "py_exc_new", False),
    ("ValueError(value)", "py_exc_new_with_value", True),
    ("ValueError(TypeError('inner'))", "py_exc_new_with_value", True),
))
def test_exception_constructor_actual_producer_slots(tmp_path, monkeypatch, expression, symbol, has_argument):
    source = ("def consume(first, second=None):\n    return first\n"
              "def make_value(value):\n    return consume(" + expression + ")\n")
    module, _ = _emit(tmp_path, monkeypatch, source)
    _producer_contract(module, symbol, has_argument)


@pytest.mark.parametrize("source", (
    "def make_value(ValueError):\n    return ValueError('shadow')\n",
    "def custom(value):\n    return value\nValueError = custom\n"
    "def make_value():\n    global ValueError\n    return ValueError('shadow')\n",
))
def test_exception_constructor_shadow_routes_stay_unchanged(tmp_path, monkeypatch, source):
    _, text = _emit(tmp_path, monkeypatch, source)
    body = _body(text)
    assert "@py_obj_call_slots(" in body
    assert not re.search(r"@py_exc_new(?:_with_value)?\(i64 2,", body)


def test_exception_constructor_message_keyword_keeps_existing_route(tmp_path, monkeypatch):
    # This is a routing/ownership regression, not a claim that the historical
    # message-keyword extension implements CPython keyword validation.
    source = ("def consume(first, second=None):\n    return first\n"
              "def make_value():\n    return consume(ValueError(msg='sentinel'))\n")
    module, _ = _emit(tmp_path, monkeypatch, source)
    _producer_contract(module, "py_exc_new", False)


def test_exception_constructor_keeps_raise_cause_route(tmp_path, monkeypatch):
    _, text = _emit(tmp_path, monkeypatch,
                    "def make_value(original):\n    error = ValueError('replacement')\n    raise error from original\n")
    body = _body(text)
    assert "@py_exc_set_cause(" in body
    assert "@py_raise(" in body or "@py_raise_owned(" in body
    assert re.search(r"@py_exc_new\(i64 2,", body)


def test_exception_constructor_cpython_reference():
    value = []
    error = ValueError(value)
    assert error.args == (value,) and error.args[0] is value
    nested = ValueError(TypeError("inner"))
    assert isinstance(nested.args[0], TypeError)
    original = RuntimeError("original")
    try:
        raise error from original
    except ValueError as caught:
        assert caught is error and caught.__cause__ is original
