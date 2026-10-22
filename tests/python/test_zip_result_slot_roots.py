"""Zip producer publication, rooted loop operands, and exception-edge balance.

Host lowering and CFG checks do not qualify the zip-star runtime internals or
replace native execution under all five collectors.
"""
import re

import pytest

from pcc.frontends.python.codegen.layer1 import L1CodeGen
from tests.python.test_shared_call_binding import _emit, _function


def _assert_immediate_publication(text, runtime):
    # Restrict this test to the transaction's runtime calls. Function default
    # metadata also builds tuples through a separate, unchanged producer.
    calls = list(re.finditer(r'^  (%call\.slot\.runtime[^ ]+) = call [^\n]*@'
                            + runtime + r'\([^\n]*\)\n', text, re.M))
    assert calls, runtime
    for match in calls:
        following = text[match.end():].splitlines()[0]
        assert following.lstrip().startswith('store ptr ' + match.group(1) + ','), following
    assert '@pcc_gc_foreign_lease_acquire(' in text
    assert '@pcc_gc_foreign_lease_release(' in text


EXPRESSIONS = (
    'zip(first, second)', 'zip(first, second, strict=True)',
    'zip(first, second, strict=False)', 'zip(mapping, second)',
    'zip(*rows)', 'dict(zip(first, second))',
)


@pytest.mark.parametrize('expression', EXPRESSIONS)
@pytest.mark.parametrize('site', ('argument', 'default', 'return', 'discard', 'later-error'))
def test_zip_publication_contexts(expression, site):
    prefix = ('def take(*, value, later=None, **extras):\n    return value\n'
              'def fail():\n    raise ValueError("later")\n')
    if site == 'default':
        body = '    def saved(item=' + expression + '):\n        return item\n    return saved()\n'
    elif site == 'return':
        body = '    return ' + expression + '\n'
    elif site == 'discard':
        body = '    ' + expression + '\n    return None\n'
    else:
        body = '    return take(value=' + expression + (', later=fail()' if site == 'later-error' else '') + ')\n'
    text = _emit(prefix + 'def probe(first: list, second: list, mapping: dict, rows: list):\n' + body)
    function = _function(text)
    helpers = ('py_zip_star',) if '*rows' in expression else ('py_list_new', 'py_tuple_new', 'py_int_from_i64', 'py_obj_getitem')
    for helper in helpers:
        _assert_immediate_publication(function, helper)
    if 'mapping' in expression:
        _assert_immediate_publication(function, 'py_dict_keys')
    if site in ('argument', 'default', 'later-error'):
        assert 'zip.result.operand' not in function
    else:
        assert '@pcc_gc_take_pinned_slot(' in function
    if 'strict=True' in expression:
        assert 'zip.strict.bad' in function


@pytest.mark.parametrize('expression', EXPRESSIONS)
def test_zip_success_and_error_paths_have_balanced_root_stacks(expression):
    from tests.python.test_slot_call_lexical_roots import _emit as emit_roots
    from pcc.backend.self_backend_prepare import prepare_module_for_target
    from pcc.backend.self_backend_precise_stackmaps import build_stack_map_plans
    from pcc.backend.self_backend_x86_64_linux import _aggregate_returned_indirect
    source = ('def take(*, value, later, **extras):\n    return value\n'
              'def fail():\n    raise ValueError("later")\n'
              'def probe(first, second, mapping: dict, rows):\n    try:\n'
              '        return take(value=' + expression + ', later=fail())\n'
              '    except Exception:\n        return None\n')
    codegen, text = emit_roots(source)
    prepared = prepare_module_for_target(text, aggregate_returned_indirect=_aggregate_returned_indirect)
    plans = build_stack_map_plans(prepared.functions, prepared.globals_, target='x86_64-linux')
    assert len(plans) == len(prepared.functions)
    assert all(flag is None and lifo for _slot, flag, lifo in codegen._slot_call_root_records)


def test_zip_actual_lowerer_supplies_registered_owners_to_every_boundary(monkeypatch):
    original = L1CodeGen._slot_call_runtime_call
    observed = []
    helpers = {'py_obj_len', 'py_obj_type_tag', 'py_dict_keys', 'py_list_new',
               'py_int_from_i64', 'py_tuple_new', 'py_obj_getitem',
               'py_tuple_set_item', 'py_list_append', 'py_zip_star'}
    producers = {'py_dict_keys', 'py_list_new', 'py_int_from_i64',
                 'py_tuple_new', 'py_obj_getitem', 'py_zip_star'}

    def runtime_call(self, name, roots, **kwargs):
        if name in helpers:
            for root in roots:
                self._slot_call_root_record(root)
            if name in producers:
                output = kwargs.get('result_slot')
                assert output is not None
                self._slot_call_root_record(output)
            observed.append((name, len(roots)))
        return original(self, name, roots, **kwargs)

    monkeypatch.setattr(L1CodeGen, '_slot_call_runtime_call', runtime_call)
    text = _emit('def probe(first: dict, second):\n    return zip(first, second)\n')
    function = _function(text)
    assert ('py_dict_keys', 1) in observed
    assert ('py_obj_type_tag', 1) in observed
    assert observed.count(('py_obj_getitem', 2)) == 2
    zip_walk = observed[:observed.index(('py_list_append', 2))]
    assert zip_walk.count(('py_tuple_set_item', 2)) == 2
    assert ('py_list_append', 2) in observed
    assert 'zip.scalar.bad' in function and 'zip.elem.bad' in function
    assert 'call.slot.cleanup' in function
    assert re.search(r'call void[^\n]*@pcc_gc_store_root\(', function)
    observed.clear()
    _emit('def probe(rows):\n    return zip(*rows)\n')
    assert ('py_zip_star', 1) in observed


def test_zip_generator_source_remains_a_caller_owned_result():
    text = _emit('def probe(fields: list, values: tuple):\n'
                 '    return dict(zip((name for name, unused in fields), values))\n')
    function = _function(text)
    _assert_immediate_publication(function, 'py_list_new')
    _assert_immediate_publication(function, 'py_obj_getitem')
