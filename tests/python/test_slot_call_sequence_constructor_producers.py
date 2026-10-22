"""Sequence constructors keep a caller-owned result through fill and teardown."""
from __future__ import annotations

import re
import textwrap

import pytest

from tests.python.owned_regression_support import (
    assert_owned_program,
    assert_reference_program,
    explicit_owned_runtime,
)
from tests.python.test_shared_call_binding import _emit, _function
from tests.python.test_slot_call_subscript_producers import _assert_immediate_publication


@pytest.mark.parametrize("constructor", ("list", "tuple"))
@pytest.mark.parametrize("argument", ("", "item", "[item]", "(item,)", "{'key': item}"))
@pytest.mark.parametrize("site", ("return", "argument", "default", "attribute", "later-error"))
def test_sequence_constructor_publishes_before_fill_and_cleanup(constructor, argument, site):
    expression = constructor + "(" + argument + ")"
    prefix = (
        "def take(*, value, later=None, **extras):\n    return value\n"
        "def fail():\n    raise ValueError('later')\n"
    )
    if site == "return":
        body = "    return " + expression + "\n"
    elif site == "default":
        body = "    def target(value=" + expression + "):\n        return value\n    return target()\n"
    elif site == "attribute":
        body = "    class Holder:\n        value = " + expression + "\n    return Holder\n"
    else:
        body = "    return take(value=" + expression + (", later=fail()" if site == "later-error" else "") + ")\n"
    text = _emit(prefix + "def probe(item):\n" + body)
    runtime = "py_tuple_new" if constructor == "tuple" and not argument else "py_list_new"
    body = _function(text)
    calls = list(re.finditer(
        r"^  (%call\.slot\.runtime[^ ]*) = call [^\n]*@" + runtime + r"\([^\n]*\)\n",
        body, re.M,
    ))
    assert calls, runtime
    for call in calls:
        assert body[call.end():].lstrip().startswith("store ptr " + call.group(1) + ",")
    if constructor == "tuple" and argument:
        _assert_immediate_publication(text, "py_tuple_from_list")
    assert "strict.nolib.stub" not in text


def test_tuple_exact_alias_copies_owner_before_source_release():
    body = _function(_emit("def probe(item):\n    return tuple(item)\n"))
    assert "@py_obj_type_tag(" in body
    alias = re.search(r"^tuple\.builtin\.alias[^:]*:\n(.*?)^call\.slot", body, re.M | re.S)
    assert alias is not None
    assert "@pcc_gc_root_copy_lease(" in alias.group(1)
    # Neither alias nor ordinary conversion routes through splat's copy ABI.
    assert "@py_tuple_from_splat(" not in body
    assert "@pcc_gc_take_pinned_slot(" in body


def test_sequence_evaluates_source_before_constructing_output():
    for constructor in ("list", "tuple"):
        body = _function(_emit(
            "def source():\n    return [1]\n"
            "def probe():\n    return " + constructor + "(source())\n"
        ))
        source = re.search(r"(%[^ ]+) = call [^\n]*@user_binding_source\([^\n]*\)\n", body)
        assert source is not None
        assert body[source.end():].lstrip().startswith("store ptr " + source.group(1) + ",")
        assert source.start() < body.index("@py_list_new(")


@pytest.mark.parametrize("expression", (
    "list(filter(None, [0, 1]))",
    "tuple(map(str, [0, 1]))",
    "list(filter(lambda value: value > 1, [0, 2]))",
    "tuple(map(identity, [item]))",
))
def test_specialized_map_filter_uses_same_result_transaction(expression):
    text = _emit(
        "def identity(value):\n    return value\n"
        "def take(*, value, **extras):\n    return value\n"
        "def probe(item):\n    return take(value=" + expression + ")\n"
    )
    _assert_immediate_publication(text, "py_list_new")
    _assert_immediate_publication(text, "py_obj_getitem")
    if "tuple" in expression:
        _assert_immediate_publication(text, "py_tuple_from_list")


PROGRAM = textwrap.dedent('''\
    import gc
    events = []
    class Item:
        def __init__(self, label):
            self.label = label
        def __del__(self):
            events.append(self.label)
    def take(*, value, later=None, **extras):
        gc.collect()
        return value
    def fail():
        gc.collect()
        raise ValueError('later')
    def values():
        yield Item('first')
        gc.collect()
        yield Item('second')
    def broken():
        yield Item('partial')
        gc.collect()
        raise ValueError('iteration')
    def make_tuple(source):
        return tuple(source)
    def main():
        original = (Item('alias'),)
        alias = take(value=tuple(original))
        assert alias is original
        copied = take(value=list(original))
        del original
        del alias
        gc.collect()
        assert events == []
        assert copied[0].label == 'alias'
        del copied
        gc.collect()
        assert events == ['alias']
        result = make_tuple(values())
        gc.collect()
        assert result[0].label == 'first' and result[1].label == 'second'
        assert len(events) == 1
        del result
        gc.collect()
        assert len(events) == 3
        try:
            list(broken())
        except ValueError as error:
            assert str(error) == 'iteration'
        else:
            raise AssertionError('lost iterator error')
        gc.collect()
        assert events[-1] == 'partial'
        try:
            take(value=tuple([Item('argument')]), later=fail())
        except ValueError:
            pass
        gc.collect()
        assert events[-1] == 'argument'
        def default(value=tuple([Item('default')])):
            gc.collect()
            return value
        stored = default()
        assert stored[0].label == 'default'
        assert events[-1] == 'argument'
        class Holder:
            value = list((Item('class'),))
        gc.collect()
        assert Holder.value[0].label == 'class'
        assert tuple(map(str, [1, 2])) == ('1', '2')
        assert list(filter(None, [0, 1, 0, 2])) == [1, 2]
        assert list() == [] and tuple() == ()
        print('SEQUENCE_OWNERSHIP_OK')
    main()
''')


def test_sequence_native_program_reference(tmp_path):
    assert_reference_program(PROGRAM, "SEQUENCE_OWNERSHIP_OK\n", tmp_path)


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_sequence_native_five_gc(python_program_compiler, request,
                                 explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params["python_program_compiler"]
    assert_owned_program(PROGRAM, "SEQUENCE_OWNERSHIP_OK\n", tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime, capfd)
