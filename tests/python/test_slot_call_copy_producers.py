"""Native copy producers preserve input and result ownership at call sites."""
from __future__ import annotations

import textwrap

import pytest

from tests.python.owned_regression_support import (
    assert_owned_program,
    assert_reference_program,
    explicit_owned_runtime,
)
from tests.python.test_shared_call_binding import _emit, _function
from tests.python.test_slot_call_subscript_producers import _assert_immediate_publication


@pytest.mark.parametrize("kind", ("copy", "deepcopy"))
@pytest.mark.parametrize("binding", ("module", "module-alias", "import-alias", "value-alias"))
@pytest.mark.parametrize("site", ("argument", "list", "default", "later-error", "return"))
def test_copy_producer_publishes_to_the_original_call_sink(kind, binding, site, tmp_path):
    imports = {
        "module": "import copy\n",
        "module-alias": "import copy as copier\n",
        "import-alias": "from copy import " + kind + " as clone\n",
        "value-alias": "import copy\nclone = copy." + kind + "\n",
    }
    call = {
        "module": "copy." + kind,
        "module-alias": "copier." + kind,
        "import-alias": "clone",
        "value-alias": "clone",
    }[binding] + "(holder.value)"
    bodies = {
        "argument": "    return take(value=PRODUCER)\n",
        "list": "    return take(value=[PRODUCER])\n",
        "default": "    def target(value=PRODUCER):\n        return value\n    return target()\n",
        "later-error": "    return take(value=PRODUCER, later=fail())\n",
        "return": "    return PRODUCER\n",
    }
    text = _emit(
        imports[binding]
        + "def take(*, value, later=None, **extras):\n    return value\n"
        + "def fail():\n    raise ValueError('later')\n"
        + "def probe(holder):\n" + bodies[site].replace("PRODUCER", call)
    )
    (tmp_path / "program.ll").write_text(text)
    if binding == "value-alias":
        # Assignment produces a callable object, so this route retains the
        # generic slot-call ABI instead of selecting a module builtin.
        assert "@py_obj_call_slots(" in _function(text)
        assert "@py_copy_" + kind + "(" not in _function(text)
    else:
        _assert_immediate_publication(_function(text), "py_copy_" + kind)
    assert "strict.nolib.stub" not in text
    assert "@py_cpy_" not in _function(text)


@pytest.mark.parametrize("kind", ("copy", "deepcopy"))
def test_copy_producer_owns_a_temporary_operand(kind):
    body = _function(_emit(
        "import copy\ndef make():\n    return [[7]]\n"
        "def take(*, value, **extras):\n    return value\n"
        "def probe():\n    return take(value=copy." + kind + "(make()))\n"
    ))
    _assert_immediate_publication(body, "py_copy_" + kind)


def test_copy_shadowed_receiver_keeps_dynamic_dispatch():
    body = _function(_emit(
        "import copy\ndef probe(copy, value):\n    return copy.deepcopy(value)\n"
    ))
    assert "@py_copy_deepcopy(" not in body


@pytest.mark.parametrize("constructor", (
    "If(cond=copy.deepcopy(if_node.cond), iftrue=then_loop, iffalse=else_loop)",
    "Compound(block_items=[copy.deepcopy(stmt)], coord=stmt.coord)",
))
def test_original_bootstrap_copy_constructor_shapes(constructor, tmp_path):
    text = _emit(
        "import copy\n"
        "class If:\n"
        "    def __init__(self, cond, iftrue, iffalse):\n"
        "        self.cond = cond\n        self.iftrue = iftrue\n        self.iffalse = iffalse\n"
        "class Compound:\n"
        "    def __init__(self, block_items, coord):\n"
        "        self.block_items = block_items\n        self.coord = coord\n"
        "def probe(if_node, then_loop, else_loop, stmt):\n"
        "    return " + constructor + "\n"
    )
    (tmp_path / "program.ll").write_text(text)
    _assert_immediate_publication(_function(text), "py_copy_deepcopy")


PROGRAM = textwrap.dedent('''\
    import copy
    import gc
    from copy import deepcopy as clone
    events = []
    class Holder:
        def __init__(self, value):
            self.value = value
    class Hook:
        def __init__(self, value):
            self.value = value
        def __copy__(self):
            gc.collect()
            events.append('copy')
            return Hook(self.value)
        def __deepcopy__(self, memo):
            gc.collect()
            events.append('deepcopy')
            return Hook(self.value + 1)
    class Broken:
        def __deepcopy__(self, memo):
            gc.collect()
            raise ValueError('copy hook')
    def take(*, value, later=None, **extras):
        gc.collect()
        return value
    def make():
        gc.collect()
        events.append('make')
        return [[17]]
    def later():
        gc.collect()
        events.append('later')
        raise ValueError('later operand')
    def main():
        inner = [7]
        source = [inner, inner]
        shallow = take(value=copy.copy(source))
        assert shallow is not source
        assert shallow[0] is inner
        deep = take(value=clone(source))
        assert deep is not source
        assert deep[0] is not inner
        assert deep[0] is deep[1]
        assert deep[0] == [7]
        cycle = []
        cycle.append(cycle)
        cycle_copy = take(value=copy.deepcopy(cycle))
        assert cycle_copy is not cycle
        assert cycle_copy[0] is cycle_copy
        holder = Holder(source)
        nested = take(value=[copy.deepcopy(holder.value)])
        assert nested[0][0] is not inner
        assert nested[0][0] is nested[0][1]
        copied_holder = Holder(value=copy.deepcopy(holder.value))
        assert copied_holder.value[0] is not inner
        nested_holder = Holder(value=[copy.deepcopy(holder.value)])
        assert nested_holder.value[0][0] is not inner
        temporary = take(value=copy.deepcopy(make()))
        assert temporary == [[17]]
        hooked = take(value=copy.copy(Hook(5)))
        assert hooked.value == 5
        hooked_deep = take(value=copy.deepcopy(Hook(5)))
        assert hooked_deep.value == 6
        try:
            take(value=copy.deepcopy(Broken()))
        except ValueError as error:
            assert str(error) == 'copy hook'
        else:
            raise AssertionError('missing hook error')
        try:
            take(value=copy.deepcopy(make()), later=later())
        except ValueError as error:
            assert str(error) == 'later operand'
        else:
            raise AssertionError('missing later error')
        def frozen(value=copy.deepcopy(source)):
            return value
        inner.append(9)
        assert frozen()[0] == [7]
        assert frozen() is frozen()
        assert events == ['make', 'copy', 'deepcopy', 'make', 'later']
        print('COPY_PRODUCER_OWNERSHIP_OK')
    main()
''')


def test_copy_native_control_reference(tmp_path):
    assert_reference_program(PROGRAM, "COPY_PRODUCER_OWNERSHIP_OK\n", tmp_path)


def test_copy_native_control_emits_owned_ir(tmp_path):
    text = _emit(PROGRAM)
    (tmp_path / "program.ll").write_text(text)
    _assert_immediate_publication(text, "py_copy_copy")
    _assert_immediate_publication(text, "py_copy_deepcopy")
    assert "strict.nolib.stub" not in text


@pytest.mark.integration
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_copy_native_five_gc(python_program_compiler, request,
                             explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params["python_program_compiler"]
    assert_owned_program(PROGRAM, "COPY_PRODUCER_OWNERSHIP_OK\n", tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime, capfd)
