"""Dynamic append joins publish native and user-defined results into one owner."""
import re
import textwrap

import pytest

from tests.python.owned_regression_support import assert_owned_program, explicit_owned_runtime
from tests.python.test_shared_call_binding import _emit
from tests.python.test_slot_call_operand_roots import (
    _calls,
    _emit as _emit_operand,
    _probe_function,
    _with_slot_origins,
)


def test_dynamic_append_joins_through_the_consumer_output_root():
    text = _probe_function(_emit_operand(
        "def probe(receiver, value):\n"
        "    return slot_operand_probe(receiver.append(value))\n"
    ))
    assert "@py_list_append(" in text
    assert "dyn.list.result" not in text
    assert re.search(
        r"(%[^ ]+) = load ptr, ptr @py_None\n\s+store ptr \1, ptr %probe\.operand",
        text,
    )
    calls = _calls(text, "py_obj_call_slots")
    assert len(calls) == 1
    generic = _with_slot_origins(text, calls[0])
    assert re.search(r", ptr %probe\.operand[^ )]*\)", generic)
    assert "list.append.method" in text
    assert "list.append.receiver" in text


def test_dynamic_append_binds_the_generic_method_before_arguments():
    text = _probe_function(_emit_operand(
        "def argument():\n    raise ValueError('argument')\n"
        "def probe(receiver):\n"
        "    return slot_operand_probe(receiver.append(argument()))\n"
    ))
    generic = text.split("list.append.generic", 1)[-1]
    # The actual generic block starts after its branch target mention.
    lookup = generic.index("@py_obj_getattr(")
    argument = generic.index("@user_slot_operand_argument(", lookup)
    invoke = generic.index("@py_obj_call_slots(", argument)
    assert lookup < argument < invoke
    assert "call.slot.cleanup" in generic
    assert "dyn.list.result" not in text


# take()'s **extras keeps the call with the signature binder, so the append
# result is a slot-call operand. DIRECT_PROGRAM binds take() statically.
PROGRAM = textwrap.dedent('''\
    import gc
    import weakref
    events = []
    selected = ValueError('selected append error')
    class Result:
        def __init__(self, label):
            self.label = label
        def __del__(self):
            events.append(self.label)
    class ReturnsObject:
        def append(self, value):
            gc.collect()
            return Result(value)
    class Raises:
        def append(self, value):
            gc.collect()
            raise selected
    def take(*, value, **extras):
        gc.collect()
        return value
    def invoke(receiver, item):
        return take(value=receiver.append(item))
    def main():
        global selected
        values = []
        assert invoke(values, 23) is None
        assert values == [23]
        result = invoke(ReturnsObject(), 'returned')
        reference = weakref.ref(result)
        gc.collect()
        assert reference() is result
        assert result.label == 'returned'
        del result
        gc.collect()
        gc.collect()
        assert reference() is None
        assert events == ['returned']
        try:
            invoke(Raises(), Result('operand'))
        except ValueError as error:
            assert error is selected
        else:
            raise AssertionError('dynamic append error lost')
        # CPython's retained exception owns traceback frames and their operands.
        # Drop that deliberate owner before requiring post-collection retirement.
        selected = None
        gc.collect()
        gc.collect()
        assert events == ['returned', 'operand']
        print('DYNAMIC_APPEND_OWNER_OK')
    main()
''')


DIRECT_PROGRAM = PROGRAM.replace("def take(*, value, **extras):", "def take(*, value):")


def test_dynamic_append_owned_result_and_error_program_emits_without_fallback():
    text = _emit(PROGRAM)
    assert "@py_obj_call_slots(" in text
    assert "list.append.invoke" in text
    assert "@py_list_append(" in text
    assert "strict.nolib.stub" not in text


def test_dynamic_append_result_reaches_a_statically_bound_keyword_call():
    assert DIRECT_PROGRAM != PROGRAM
    text = _emit(DIRECT_PROGRAM)
    invoke = re.search(r"^define [^\n]*@user_binding_invoke\([^\n]*\).*?^}", text, re.M | re.S)
    assert invoke is not None
    assert len(re.findall(r"\bcall [^\n]*@user_binding_take\(", invoke.group(0))) == 1
    assert "@py_list_append(" in text
    assert "strict.nolib.stub" not in text


@pytest.mark.integration
@pytest.mark.parametrize("program", ("binder", "direct"))
@pytest.mark.parametrize("python_program_compiler", ("pcc0", "pcc1"), indirect=True)
def test_dynamic_append_owned_result_and_error_native_five_gc(
    python_program_compiler, request, explicit_owned_runtime, tmp_path, capfd, program,
):
    mode = request.node.callspec.params["python_program_compiler"]
    assert_owned_program(
        PROGRAM if program == "binder" else DIRECT_PROGRAM, "DYNAMIC_APPEND_OWNER_OK\n", tmp_path,
        python_program_compiler, mode, explicit_owned_runtime, capfd,
        provenance_probe="2",
    )
