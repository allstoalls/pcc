"""Binary object results enter authoritative roots before cleanup."""
from __future__ import annotations

import re
import textwrap

import pytest

from pcc.frontends.python import (
    type_infer,
)
from pcc.frontends.python.py_ast import (
    BinOp,
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
    _emit as _emit_binding,
)
from tests.python.test_slot_call_operand_roots import (
    SlotProbeCodegen,
    _calls,
    _emit as _emit_operand_probe,
)


def _assert_binary_publication(text, count=None):
    calls = list(re.finditer(
        r"^  (%[^ ]+) = call [^\n]*@py_obj_add\([^\n]*\)\n",
        text,
        re.M,
    ))
    assert calls
    if count is not None:
        assert len(calls) == count
    for call in calls:
        following = text[call.end():].splitlines()[0]
        assert following.lstrip().startswith("store ptr " + call.group(1) + ","), following
    assert "@pcc_gc_foreign_lease_acquire(" in text
    assert "@pcc_gc_foreign_lease_release(" in text


@pytest.mark.parametrize("expression,count", [
    ("left + right", 1),
    ("'prefix:' + right", 1),
    ("left + '.suffix'", 1),
    ("left + right + '.suffix'", 2),
    ("'prefix:' + (left + right)", 2),
    ("[left] + [right]", 1),
    ("(left,) + (right,)", 1),
    ("b'left' + b'right'", 1),
    ("lookup()[0] + right", 1),
])
def test_binary_operand_producer_publishes_immediately(expression, count):
    text = _emit_operand_probe(
        "def lookup():\n    return ['left']\n"
        "def probe(left, right):\n"
        "    return slot_operand_probe(" + expression + ")\n"
    )
    _assert_binary_publication(text, count)


@pytest.mark.parametrize("site", ("argument", "default", "return", "later-error"))
def test_binary_producer_context_matrix(site):
    prelude = ("def take(*, value, later=None, **extras):\n    return value\n"
               "def fail():\n    raise ValueError('later')\n")
    if site == "default":
        body = "    def target(value=left + right):\n        return value\n    return target()\n"
    elif site == "return":
        body = "    return left + right\n"
    elif site == "later-error":
        body = "    return take(value=left + right, later=fail())\n"
    else:
        body = "    return take(value=left + right)\n"
    text = _emit_binding(prelude + "def probe(left, right):\n" + body)
    _assert_binary_publication(text, 1)


@pytest.mark.parametrize("source", [
    "def probe(value: int):\n    return slot_operand_probe(value + 1)\n",
    "value: int = 4\ndef probe():\n    return slot_operand_probe(value + 1)\n",
    "def value() -> int:\n    return 4\ndef probe():\n    return slot_operand_probe(value() + 1)\n",
    "def probe(left: float, right: float):\n    return slot_operand_probe(left + right)\n",
])
def test_ordinary_scalar_addition_uses_runtime_object_dispatch(source):
    text = _emit_operand_probe(source)
    _assert_binary_publication(text, 1)
    # A type annotation is neither an exact runtime-class proof nor a range
    # proof. The object dispatcher, not a primitive kernel, selects behavior.
    assert not _calls(text, "py_int_add")
    assert not _calls(text, "py_int_to_i64_lane")


@pytest.mark.parametrize("numeric", [
    IntType(name="pcc.i64"),
    IntType(name="pcc.u64", signed=False),
    IntType(name="int", width=8),
    IntType(name="int", width=16),
    IntType(name="int", width=32),
    IntType(name="int", signed=False),
    FloatType(name="float", width=32),
    FloatType(name="pcc.f64"),
])
def test_object_addition_does_not_replace_an_explicit_numeric_projection(numeric):
    module = type_infer.infer_module(parse_and_lift("", "numeric_projection.py", "numeric_projection"))
    codegen = SlotProbeCodegen(module, ir_scaffold_mode="on")
    left = Name(span=None, ty=numeric, ident="left")
    right = Name(span=None, ty=numeric, ident="right")
    for result_ty in (numeric, DynType(name="dyn")):
        expr = BinOp(span=None, ty=result_ty, op="+", lhs=left, rhs=right)
        assert codegen._slot_call_binary_runtime(expr, object_boundary=True) is None


def test_global_scaffold_does_not_make_ordinary_addition_a_machine_operation():
    module = type_infer.infer_module(parse_and_lift("", "ordinary_scaffold.py", "pcc.driver.cli_bootstrap"))
    codegen = SlotProbeCodegen(module, ir_scaffold_mode="on")
    codegen._module_uses_raw_int_scaffold = codegen._module_imports_raw_int_scaffold()
    assert codegen._module_uses_raw_int_scaffold
    ordinary = IntType(name="int", width=64, signed=True)
    left = Name(span=None, ty=ordinary, ident="left")
    right = Name(span=None, ty=ordinary, ident="right")
    expr = BinOp(span=None, ty=ordinary, op="+", lhs=left, rhs=right)
    assert codegen._slot_call_binary_runtime(expr, object_boundary=True) == "py_obj_add"
    codegen._runtime_port_module = True
    assert codegen._slot_call_binary_runtime(expr, object_boundary=True) is None


PROGRAMS = {
    "builtins": textwrap.dedent('''\
        import gc
        def take(*, value, **extras):
            gc.collect()
            return value
        def add(left, right):
            return left + right
        def main():
            left = 'left'
            right = 'right'
            assert take(value=left + right) == 'leftright'
            assert take(value='[' + (left + right) + ']') == '[leftright]'
            assert take(value=b'left' + b'right') == b'leftright'
            assert take(value=bytearray(b'a') + b'b') == bytearray(b'ab')
            state = {'value': 42}
            left_items = [state]
            right_items = [state]
            merged = take(value=left_items + right_items)
            assert len(merged) == 2
            assert merged[0] is state
            assert merged[1] is state
            repeated = take(value=left_items + left_items)
            assert len(repeated) == 2
            assert repeated[1] is state
            left_tuple = (state,)
            merged_tuple = take(value=left_tuple + left_tuple)
            assert len(merged_tuple) == 2
            assert merged_tuple[1] is state
            assert take(value=[] + []) == []
            assert take(value=() + ()) == ()
            assert add(1 << 100, 1 << 100) == 1 << 101
            assert add((1 << 62) - 1, 1) == 1 << 62
            assert add(1.25, 2.5) == 3.75
            assert add(True, True) == 2
            def target(value=left + right):
                gc.collect()
                return value
            assert target() == 'leftright'
            print('BINARY_BUILTINS_OK')
        main()
    '''),
    "callbacks": textwrap.dedent('''\
        import gc
        events = []
        state = {'value': 42}
        class Left:
            def __add__(self, other):
                gc.collect()
                events.append('add')
                return NotImplemented
        class Right:
            def __radd__(self, other):
                gc.collect()
                events.append('radd')
                return state
        class Raising:
            def __add__(self, other):
                gc.collect()
                raise KeyError('operator')
        def left():
            events.append('left')
            return Left()
        def right():
            gc.collect()
            events.append('right')
            return Right()
        def later():
            gc.collect()
            events.append('later')
            raise ValueError('later')
        def take(*, value, later=None, **extras):
            gc.collect()
            return value
        def main():
            assert take(value=left() + right()) is state
            assert events == ['left', 'right', 'add', 'radd']
            try:
                take(value=left() + right(), later=later())
            except ValueError:
                pass
            else:
                raise AssertionError('missing later error')
            assert events[-1] == 'later'
            try:
                take(value=left() + later())
            except ValueError:
                pass
            else:
                raise AssertionError('missing right operand error')
            try:
                take(value=Raising() + right())
            except KeyError:
                pass
            else:
                raise AssertionError('missing operator error')
            print('BINARY_CALLBACKS_OK')
        main()
    '''),
}
PROGRAMS["mixed_numeric_callbacks"] = textwrap.dedent("""\
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
""")

PROGRAMS["error_cleanup"] = textwrap.dedent("""\
    import gc
    events = []
    class Result:
        def __del__(self):
            events.append('result-dropped')
    class Left:
        def __add__(self, other):
            gc.collect()
            return Result()
        def __del__(self):
            events.append('left-dropped')
    class Right:
        def __del__(self):
            events.append('right-dropped')
    def later():
        gc.collect()
        events.append('later')
        raise ValueError('later')
    def take(*, value, later=None, **extras):
        raise AssertionError('call must not execute')
    def main():
        try:
            take(value=Left() + Right(), later=later())
        except ValueError:
            pass
        else:
            raise AssertionError('missing error')
        gc.collect()
        assert events.count('left-dropped') == 1
        assert events.count('right-dropped') == 1
        assert events.count('result-dropped') == 1
        assert events.count('later') == 1
        print('BINARY_ERROR_CLEANUP_OK')
    main()
""")

PROGRAMS["ordinary_integer_annotations"] = textwrap.dedent("""\
    import gc
    marker = {'answer': 42}
    class Override:
        def __add__(self, other):
            gc.collect()
            return marker
        def __radd__(self, other):
            gc.collect()
            return marker
    def take(*, value, **extras):
        gc.collect()
        return value
    def forward(value: int):
        return take(value=value + 1)
    def reflected(value: int):
        return take(value=1 + value)
    def floating(left: float, right: float):
        return take(value=left + right)
    def main():
        assert forward(1 << 100) == (1 << 100) + 1
        assert reflected(1 << 100) == (1 << 100) + 1
        assert forward((1 << 62) - 1) == 1 << 62
        assert forward(Override()) is marker
        assert reflected(Override()) is marker
        assert floating(1.25, 2.5) == 3.75
        print('BINARY_ORDINARY_ANNOTATIONS_OK')
    main()
""")

EXPECTED = {
    "builtins": "BINARY_BUILTINS_OK\n",
    "ordinary_integer_annotations": "BINARY_ORDINARY_ANNOTATIONS_OK\n",
    "callbacks": "BINARY_CALLBACKS_OK\n",
    "mixed_numeric_callbacks": "BINARY_MIXED_CALLBACKS_OK\n",
    "error_cleanup": "BINARY_ERROR_CLEANUP_OK\n",
}


@pytest.mark.integration
@pytest.mark.parametrize("case", PROGRAMS)
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_binary_producer_native_five_gc(
    case, python_program_compiler, request, explicit_owned_runtime, tmp_path, capfd,
):
    mode = request.node.callspec.params["python_program_compiler"]
    assert_owned_program(
        PROGRAMS[case], EXPECTED[case], tmp_path,
        python_program_compiler, mode, explicit_owned_runtime,
        capfd, provenance_probe="2",
    )
    assert (tmp_path / "compiler-wrapper.stderr").read_text() == ""
