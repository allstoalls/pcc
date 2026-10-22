"""Dynamic mapping and pair construction shares the authoritative owner path."""
import re

import pytest

from tests.python.test_shared_call_binding import _emit, _function
from tests.python.test_slot_call_lexical_roots import _emit as emit_roots
from pcc.backend.self_backend_prepare import prepare_module_for_target
from pcc.backend.self_backend_precise_stackmaps import build_stack_map_plans
from pcc.backend.self_backend_x86_64_linux import _aggregate_returned_indirect


@pytest.mark.parametrize('annotation', ('', ': object'))
@pytest.mark.parametrize('expression', ('dict(source)', 'dict(source, extra=7)'))
def test_dynamic_protocol_dispatch_uses_existing_slot_transaction(annotation, expression):
    source = 'def probe(source' + annotation + '):\n    return ' + expression + '\n'
    body = _function(_emit(source))
    assert 'dict.constructor.protocol.status' in body
    assert 'dict() argument is not iterable' not in body
    assert 'dict.constructor.notiterable' not in body
    calls = re.findall(r'call i64[^\n]*@py_dict_update_slots\(ptr (%[\w.]+), ptr (%[\w.]+)\)', body)
    assert len(calls) == 1
    aliases = dict(re.findall(r'(%[\w.]+) = bitcast ptr (%[\w.]+) to ptr', body))
    assert 'dict.constructor.result.operand' in aliases.get(calls[0][0], calls[0][0])
    assert 'dict.constructor.source.operand' in aliases.get(calls[0][1], calls[0][1])
    assert '@py_dict_keys(' in body and '@py_dict_set_slots(' in body
    assert not re.search(r'call void @py_dict_update\(', body)
    codegen, text = emit_roots(source)
    prepared = prepare_module_for_target(text, aggregate_returned_indirect=_aggregate_returned_indirect)
    plans = build_stack_map_plans(prepared.functions, prepared.globals_, target='x86_64-linux')
    assert len(plans) == len(prepared.functions)


@pytest.mark.parametrize('body', (
    '    try:\n        return consume(value=dict(source), later=fail())\n    except Exception:\n        return None\n',
    '    try:\n        yield dict(source)\n    except Exception:\n        yield None\n',
))
def test_pair_branch_preserves_caller_and_generator_frame_balance(body):
    source = ('def consume(*, value, later, **extras):\n    return value\n'
              'def fail():\n    raise ValueError("later")\n'
              'def probe(source):\n' + body)
    _codegen, text = emit_roots(source)
    assert '@py_dict_update_slots(' in text
    prepared = prepare_module_for_target(text, aggregate_returned_indirect=_aggregate_returned_indirect)
    plans = build_stack_map_plans(prepared.functions, prepared.globals_, target='x86_64-linux')
    assert len(plans) == len(prepared.functions)
