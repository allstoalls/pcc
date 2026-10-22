"""Dict-routed dynamic method results keep one authoritative owner.

These fixture-free checks inspect generated IR and precise root joins. The
separate integration test executes the same shapes with an explicit runtime.
"""
from __future__ import annotations

import re
import textwrap

import pytest

from pcc.backend.self_backend_precise_stackmaps import build_stack_map_plans
from pcc.backend.self_backend_prepare import prepare_module_for_target
from pcc.backend.self_backend_x86_64_linux import _aggregate_returned_indirect
from pcc.frontends.python.codegen.freestanding_abi_constants import (
    PY_TYPE_DICT,
    PY_TYPE_LIST,
    PY_TYPE_SET,
)
from tests.python.owned_regression_support import (
    assert_owned_program,
    assert_reference_program,
    explicit_owned_runtime,
)
from tests.python.test_lambda_constructor_roots import immediate_store
from tests.python.test_shared_call_binding import _emit, _function
from tests.python.test_slot_call_lexical_roots import _emit as _emit_roots
from tests.python.test_slot_call_operand_roots import (
    _calls,
    _emit as _emit_operand,
    _probe_function,
    _with_slot_origins,
)


@pytest.mark.parametrize("method", ("copy()", "pop(key, fallback)"))
@pytest.mark.parametrize("annotation", ("", ": dict"))
@pytest.mark.parametrize("site", (
    "return", "argument", "keyword", "binder", "assignment", "statement",
))
def test_dynamic_dict_method_results_have_an_owner(method, annotation, site):
    expression = "receiver." + method
    if site == "return":
        body = "    return " + expression + "\n"
    elif site == "argument":
        body = "    return take(" + expression + ")\n"
    elif site == "keyword":
        body = "    return take(value=" + expression + ")\n"
    elif site == "binder":
        body = "    return bound(value=" + expression + ")\n"
    elif site == "assignment":
        body = "    result = " + expression + "\n    return result\n"
    else:
        body = "    " + expression + "\n    return None\n"
    source = ("def take(value):\n    return value\n"
              "def bound(*, value, **extras):\n    return value\n"
              "def probe(receiver" + annotation + ", key, fallback):\n" + body)
    codegen, text = _emit_roots(source, "binding")
    function = _function(text)
    assert "dyn.dict.result" not in function
    assert "dyn.pop.result" not in function
    assert "@py_obj_call_slots(" in function
    runtime = "py_copy_copy" if method == "copy()" else "py_dict_get"
    assert immediate_store(function, runtime) == 1
    assert "@py_obj_call(" not in function
    assert codegen._slot_call_root_records
    prepared = prepare_module_for_target(
        text, aggregate_returned_indirect=_aggregate_returned_indirect,
    )
    plans = build_stack_map_plans(
        prepared.functions, prepared.globals_, target="x86_64-linux",
    )
    assert len(plans) == len(prepared.functions)


@pytest.mark.parametrize("method", ("copy()", "pop(key, fallback)"))
def test_dynamic_dict_method_branches_publish_the_consumer_root(method):
    function = _probe_function(_emit_operand(
        "def probe(receiver, key, fallback):\n"
        "    return slot_operand_probe(receiver." + method + ")\n"
    ))
    calls = _calls(function, "py_obj_call_slots")
    assert len(calls) == 1
    assert re.search(
        r", ptr %probe\.operand[^ )]*\)",
        _with_slot_origins(function, calls[0]),
    )
    runtime = "py_copy_copy" if method == "copy()" else "py_dict_get"
    producer = re.search(
        r"(%[^ ]+) = call ptr[^\n]*@" + runtime
        + r"\([^\n]*\)\n\s+store ptr \1, ptr %probe\.operand",
        function,
    )
    assert producer is not None
    if method.startswith("pop"):
        copies = [_with_slot_origins(function, call)
                  for call in _calls(function, "pcc_gc_root_copy_lease")]
        assert any("%probe.operand" in call and "dict.pop.default" in call
                   for call in copies)


@pytest.mark.parametrize("method", ("copy()", "pop(key, fallback)", "keys()"))
def test_dict_copy_guard_preserves_all_builtin_container_copy_routes(method):
    function = _function(_emit(
        "def probe(receiver, key, fallback):\n    return receiver." + method + "\n"
    ))
    tag = re.search(r"(%[^ ]+) = call i64[^\n]*@py_obj_type_tag\(", function)
    assert tag is not None
    tested_tags = re.findall(
        r"icmp eq i64 " + re.escape(tag.group(1)) + r", (\d+)", function,
    )
    expected = (PY_TYPE_DICT, PY_TYPE_LIST, PY_TYPE_SET) if method == "copy()" else (PY_TYPE_DICT,)
    assert tuple(map(int, tested_tags)) == expected
    assert "@py_obj_call_slots(" in function


@pytest.mark.parametrize("method", (
    "copy(value)", "get()", "setdefault()", "keys(value)",
    "pop(*value)", "pop(key, *value)", "pop(**value)", "pop(key, **value)",
))
def test_unsupported_dict_fastpath_shapes_keep_owned_generic_dispatch(method):
    function = _function(_emit(
        "def probe(receiver, key, value):\n    return receiver." + method + "\n"
    ))
    assert "@py_obj_call_slots(" in function
    assert "@py_obj_call(" not in function
    assert "dyn.dict.result" not in function
    assert "dyn.pop.result" not in function


@pytest.mark.parametrize("method", ("copy()", "pop(key, fallback)"))
@pytest.mark.parametrize("body", (
    "    try:\n        return take(value=EXPR, later=fail())\n"
    "    except ValueError:\n        return None\n",
    "    try:\n        yield EXPR\n"
    "    except ValueError:\n        yield None\n",
), ids=("caught-error", "generator"))
def test_dict_method_result_roots_balance_exception_and_generator_joins(method, body):
    source = ("def take(*, value, later, **extras):\n    return value\n"
              "def fail():\n    raise ValueError('later')\n"
              "def probe(receiver, key, fallback):\n"
              + body.replace("EXPR", "receiver." + method))
    _codegen, text = _emit_roots(source)
    prepared = prepare_module_for_target(
        text, aggregate_returned_indirect=_aggregate_returned_indirect,
    )
    plans = build_stack_map_plans(
        prepared.functions, prepared.globals_, target="x86_64-linux",
    )
    assert len(plans) == len(prepared.functions)


_PROGRAM_TEMPLATE = textwrap.dedent('''\
    import gc
    import weakref

    events = []
    selected = ValueError('selected method error')

    class Result:
        def __init__(self, label):
            self.label = label
        def __del__(self):
            events.append(self.label)
            gc.collect()

    class Receiver:
        def copy(self):
            gc.collect()
            return Result('result')
        def pop(self, key, default):
            gc.collect()
            return Result('result')
        def __del__(self):
            events.append('receiver')
            gc.collect()

    class Raises:
        def copy(self):
            gc.collect()
            raise selected
        def pop(self, key, default):
            gc.collect()
            raise selected

    class BadHash:
        def __hash__(self):
            gc.collect()
            raise selected

    class Ordered:
        @property
        def pop(self):
            events.append('lookup')
            gc.collect()
            return self.invoke
        def invoke(self, key, default):
            events.append('call')
            gc.collect()
            return default

    def take(value):
        gc.collect()
        return value

    def bound(*, value, **extras):
        gc.collect()
        return value

    def as_argument(receiver):
        return take(EXPRESSION)

    def as_keyword(receiver):
        return take(value=EXPRESSION)

    def as_bound(receiver):
        return bound(value=EXPRESSION)

    def as_return(receiver):
        return EXPRESSION

    def as_assignment(receiver):
        result = EXPRESSION
        return result

    def as_statement(receiver):
        EXPRESSION
        return None

    def popped(receiver, key, default):
        return receiver.pop(key, default)

    def copied(receiver):
        return receiver.copy()

    def missing_copy(receiver):
        try:
            copied(receiver)
        except AttributeError:
            return
        raise AssertionError('copy method invented for unsupported receiver')

    def marked(label, value):
        events.append(label)
        gc.collect()
        return value

    def ordered(receiver, default):
        return receiver.pop(marked('key', 'missing'), marked('default', default))

    def check(function):
        result = function(Receiver())
        reference = weakref.ref(result)
        gc.collect()
        assert reference() is result
        assert result.label == 'result'
        assert events == ['receiver']
        del result
        gc.collect()
        gc.collect()
        assert reference() is None
        assert sorted(events) == ['receiver', 'result']
        events.clear()

    def main():
        global selected
        check(as_argument)
        check(as_keyword)
        check(as_bound)
        check(as_return)
        check(as_assignment)
        assert as_statement(Receiver()) is None
        gc.collect()
        gc.collect()
        assert sorted(events) == ['receiver', 'result']
        events.clear()

        chosen = Result('chosen')
        fallback = Result('fallback')
        mapping = {'key': chosen}
        clone = copied(mapping)
        assert clone is not mapping and clone['key'] is chosen
        sequence = [chosen]
        duplicate = copied(sequence)
        assert duplicate is not sequence and duplicate[0] is chosen
        members = {1, 2}
        duplicate_set = copied(members)
        assert duplicate_set is not members and duplicate_set == members
        duplicate_set.add(3)
        assert members == {1, 2}
        missing_copy((chosen,))
        missing_copy(1)
        result = popped(mapping, 'key', fallback)
        assert result is chosen and len(mapping) == 0
        del result
        result = popped(mapping, 'missing', fallback)
        assert result is fallback
        del result
        gc.collect()
        assert chosen.label == 'chosen' and fallback.label == 'fallback'
        assert events == []

        temporary = popped({}, 'missing', Result('temporary'))
        reference = weakref.ref(temporary)
        gc.collect()
        assert reference() is temporary
        del temporary
        gc.collect()
        gc.collect()
        assert reference() is None and events == ['temporary']
        events.clear()
        assert popped({'key': chosen}, 'key', Result('unused')) is chosen
        gc.collect()
        assert events == ['unused']
        events.clear()

        assert ordered(Ordered(), fallback) is fallback
        assert events == ['lookup', 'key', 'default', 'call']
        events.clear()
        try:
            as_return(Raises())
        except ValueError as error:
            assert error is selected
        else:
            raise AssertionError('user method error lost')
        selected = None
        gc.collect()
        gc.collect()

        selected = ValueError('selected hash error')
        try:
            popped({'present': chosen}, BadHash(), Result('error-default'))
        except ValueError as error:
            assert error is selected
        else:
            raise AssertionError('hash error lost')
        selected = None
        gc.collect()
        gc.collect()
        assert events == ['error-default']
        print('DICT_METHOD_OWNERS_OK')

    main()
''')


def _program(method):
    expression = 'receiver.copy()' if method == 'copy' else "receiver.pop('key', None)"
    return _PROGRAM_TEMPLATE.replace('EXPRESSION', expression)


@pytest.mark.parametrize("method", ("copy", "pop"))
def test_dict_method_result_program_reference_and_emission(method, tmp_path):
    source = _program(method)
    assert_reference_program(source, 'DICT_METHOD_OWNERS_OK\n', tmp_path)
    text = _emit(source)
    assert "@py_obj_call_slots(" in text
    assert "dict.copy.generic.invoke" in text
    assert "dict.pop.generic.invoke" in text
    assert "dyn.dict.result" not in text
    assert "dyn.pop.result" not in text
    assert "strict.nolib.stub" not in text


@pytest.mark.integration
@pytest.mark.parametrize("method", ("copy", "pop"))
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_dict_method_result_owners_native_five_gc(
    python_program_compiler, request, explicit_owned_runtime, tmp_path, capfd, method,
):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(
        _program(method), 'DICT_METHOD_OWNERS_OK\n', tmp_path,
        python_program_compiler, mode, explicit_owned_runtime, capfd,
        provenance_probe='2',
    )
