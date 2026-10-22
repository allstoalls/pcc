"""An existing valueclass binding selects its next producer's projection."""

import pytest

from pcc.frontends.python.codegen.errors import L1CodegenError
from tests.python.test_shared_call_binding import _emit, _function


PREFIX = '''import pcc

@pcc.valueclass
class Fact:
    first: int
    second: int
'''


PROGRAM = PREFIX + '''
def probe(kernel):
    metadata: Fact = kernel.metadata(0)
    metadata = kernel.metadata(1)
    return metadata.first + metadata.second
'''


INCOMPATIBLE_PROGRAM = PREFIX + '''
calls = []
def produce() -> int:
    calls.append('produce')
    return 7
def probe(metadata: Fact):
    try:
        metadata = produce()
    except TypeError:
        return metadata.first + metadata.second
    raise AssertionError('incompatible projection did not raise')
def main():
    assert probe(Fact(31, 47)) == 78
    assert calls == ['produce']
    print('VALUECLASS_REBIND_TYPE_ERROR_OK')
main()
'''


def test_reassignment_uses_existing_valueclass_storage_before_evaluation():
    body = _function(_emit(PROGRAM))
    assert body.count("@py_valuebox_get_field(") == 4
    assert body.count("@py_obj_call_slots(") == 2


@pytest.mark.parametrize("producer", [
    "kernel.metadata(1)",
    "kernel(1)",
    "kernel.value",
    "items[0]",
    "candidate",
    "kernel.metadata(1) if choose else kernel.metadata(2)",
    "kernel.metadata(1) or kernel.metadata(2)",
])
def test_reassignment_producer_forms_keep_an_authoritative_root(producer):
    body = _function(_emit(PREFIX + '''
def probe(kernel, items, candidate, choose):
    metadata: Fact = kernel.metadata(0)
    metadata = ''' + producer + '''
    return metadata.first + metadata.second
'''))
    assert body.count("@py_valuebox_get_field(") == 4


@pytest.mark.parametrize("scope", ["parameter", "global", "module"])
def test_existing_aggregate_storage_selects_projection_in_each_scope(scope):
    if scope == "parameter":
        source = PREFIX + '''
def probe(metadata: Fact, kernel):
    metadata = kernel.metadata(1)
    return metadata.first + metadata.second
'''
    elif scope == "global":
        source = PREFIX + '''
metadata: Fact = Fact(1, 2)
def probe(kernel):
    global metadata
    metadata = kernel.metadata(1)
    return metadata.first + metadata.second
'''
    else:
        source = PREFIX + '''
def produce(value):
    return value
metadata: Fact = Fact(1, 2)
metadata = produce(Fact(3, 4))
'''
    assert "@py_valuebox_get_field(" in _emit(source)


def test_global_aggregate_does_not_select_local_shadow_projection():
    body = _function(_emit(PREFIX + '''
metadata: Fact = Fact(1, 2)
def probe(kernel):
    metadata = kernel.metadata(1)
    return metadata
'''))
    assert "@py_valuebox_get_field(" not in body


def test_incompatible_scalar_producer_is_evaluated_once_before_type_check(tmp_path):
    from tests.python.owned_regression_support import assert_reference_program

    # CPython annotations do not perform pcc's explicit value projection.
    # Model that checked boundary in the reference, including assignment only
    # after a successful check. The native-ready source above uses the actual
    # compiler projection, whose execution remains a separate native gate.
    reference = INCOMPATIBLE_PROGRAM.replace(
        "        metadata = produce()",
        "        incoming = produce()\n"
        "        if not isinstance(incoming, Fact):\n"
        "            raise TypeError('expected Fact valueclass instance')\n"
        "        metadata = incoming",
    )
    assert_reference_program(reference, "VALUECLASS_REBIND_TYPE_ERROR_OK\n", tmp_path)
    body = _function(_emit(INCOMPATIBLE_PROGRAM))
    assert body.count("@user_binding_produce(") == 1
    assert "@py_obj_isinstance(" in body
    assert "metadata.obj.addr" not in body


def test_reassignment_does_not_publish_an_unsafe_pointer():
    with pytest.raises(L1CodegenError, match="raw pointer"):
        _emit(PREFIX + '''
from pcc.unsafe import stack_alloc
def probe(kernel):
    metadata: Fact = kernel.metadata(0)
    metadata = stack_alloc(8)
    return metadata
''')


def test_dynamic_rebind_native_program_reference_and_strict_lowering(tmp_path):
    from tests.python.owned_regression_support import assert_reference_program
    from tests.python.test_valueclass_aggregate_ownership_native import SCENARIOS

    case = next(case for case in SCENARIOS if case.name == "dynamic-rebind-nested-payload")
    assert_reference_program(case.program, case.expected_stdout, tmp_path)
    text = _emit(case.program)
    assert "strict.nolib.stub" not in text
