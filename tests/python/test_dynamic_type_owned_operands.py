"""The type namespace boundary receives independently leased evaluated owners."""
import re

import pytest

from pcc.frontends.python.codegen.layer1 import L1CodeGen
from pcc.frontends.python.py_lift import parse_and_lift
from pcc.frontends.python.type_infer import infer_module


@pytest.mark.parametrize('call', ('type(name, bases, namespace)', 'type.__new__(type, name, bases, namespace)'))
def test_dynamic_type_arguments_and_result_have_owning_slots(call):
    source = 'def build(name, bases, namespace):\n    return ' + call + '\n'
    module = infer_module(parse_and_lift(source, 'dynamic_type.py', 'dynamic_type'))
    codegen = L1CodeGen(module, ir_scaffold_mode='on')
    codegen._strict_no_libpython = True
    codegen._prefer_native_callable_values = True
    text = str(codegen.generate(module))
    match = re.search(r'^define [^\n]*@user_dynamic_type_build\([^\n]*\).*?^}', text, re.M | re.S)
    assert match is not None
    body = match.group(0)
    call_match = re.search(r'(%[^ ]+) = call [^\n]*@py_class_new_from_objects\([^\n]*\)\n', body)
    assert call_match is not None
    previous = body[:call_match.start()]
    assert previous.count('@pcc_gc_foreign_lease_acquire(') >= 3
    assert previous.count('type.argument') >= 3
    after = body[call_match.end():]
    assert after.splitlines()[0].strip().startswith('store ptr ' + call_match.group(1) + ', ptr ')
    assert '@pcc_gc_foreign_lease_release(' in after
    assert '@pcc_gc_store_root(' in after


def test_literal_name_uses_live_namespace_owner():
    module = infer_module(parse_and_lift('''def build(namespace):
    return type('Copied', (), namespace)
''', 'dynamic_type.py', 'dynamic_type'))
    codegen = L1CodeGen(module, ir_scaffold_mode='on')
    codegen._strict_no_libpython = True
    text = str(codegen.generate(module))
    assert '@py_class_new_from_objects(' in text
    assert 'call ptr @py_class_new(' not in text
