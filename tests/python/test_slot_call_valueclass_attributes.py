"""Valueclass object boundaries retain the actual scalar or payload-field owner."""
import re
from types import SimpleNamespace

import pytest

from pcc.frontends.python.codegen.attr_load_lowering import AttrLoadLoweringMixin
from pcc.frontends.python.codegen.errors import L1CodegenError
from tests.python.test_shared_call_binding import _emit


PREFIX = '''import pcc
@pcc.valueclass
class Record:
    value: FIELD

def take(*, value, **extras):
    return value
'''


@pytest.mark.parametrize('field', ('int', 'float', 'bool', 'list'))
@pytest.mark.parametrize('site', ('argument', 'default', 'tuple'))
def test_projected_fields_publish_real_owner(field, site, tmp_path):
    body = '    return take(value=record.value)\n'
    if site == 'default':
        body = '    def saved(value=record.value):\n        return value\n    return saved()\n'
    elif site == 'tuple':
        body = '    return take(value=(record.value,))\n'
    source = PREFIX.replace('FIELD', field) + 'def probe(record: Record):\n' + body
    text = _emit(source)
    (tmp_path/'input.py').write_text(source); (tmp_path/'program.ll').write_text(text)
    if field == 'list':
        assert re.search(r'\bcall [^\n]*@pcc_gc_root_copy_borrowed_lease\(', text)
        assert 'record.value.root.0' in text
    else:
        assert 'value.attribute.scalar' in text
        producer = {'int': 'py_int_from_i64', 'float': 'py_float_from_double', 'bool': 'py_bool_from_bit'}[field]
        if field == 'float':
            producer = 'py_float_from_'  # The exact scalar constructor is checked below.
        assert re.search(r'(%[^ ]+) = call [^\n]*@' + producer + r'[^\n]*\n\s*store ptr \1, ptr ', text)


def test_nested_pointer_path_copies_its_registered_leaf(tmp_path):
    source = '''import pcc
@pcc.valueclass
class Leaf:
    values: list
@pcc.valueclass
class Outer:
    leaf: Leaf
    number: int
def take(*, value, **extras):
    return value
def probe(record: Outer):
    return take(value=record.leaf.values)
'''
    text = _emit(source)
    (tmp_path/'program.ll').write_text(text)
    assert 'record.value.root.0_0' in text
    assert re.search(r'\bcall [^\n]*@pcc_gc_root_copy_borrowed_lease\(', text)


def test_nested_payload_box_is_published_before_field_work(tmp_path):
    source = '''import pcc
@pcc.valueclass
class Leaf:
    values: list
    count: int
@pcc.valueclass
class Outer:
    leaf: Leaf
def take(*, value, **extras):
    return value
def probe(record: Outer):
    return take(value=record.leaf)
'''
    text = _emit(source)
    (tmp_path/'program.ll').write_text(text)
    assert re.search(r'(%[^ ]+) = call [^\n]*@py_valuebox_new\([^\n]*\n\s*store ptr \1, ptr ', text)
    assert re.search(r'\bcall [^\n]*@py_valuebox_set_field\(', text)
    assert re.search(r'\bcall [^\n]*@pcc_gc_root_copy_borrowed_lease\(', text)


def test_module_payload_field_uses_its_mapped_global_slot(tmp_path):
    source = PREFIX.replace('FIELD', 'list') + 'record = Record([1])\ndef probe():\n    return take(value=record.value)\n'
    text = _emit(source); (tmp_path/'program.ll').write_text(text)
    assert 'value.attribute.module.source' in text
    assert re.search(r'\bcall [^\n]*@pcc_gc_root_copy_lease\(', text)


def test_scalar_only_temporary_payload_is_evaluated_once(tmp_path):
    source = PREFIX.replace('FIELD', 'int') + 'def make() -> Record:\n    return Record(7)\ndef probe():\n    return take(value=make().value)\n'
    text = _emit(source); (tmp_path/'program.ll').write_text(text)
    body = re.search(r'^define [^\n]*@user_binding_probe\([^\n]*\).*?^}', text, re.M | re.S).group(0)
    assert len(re.findall(r'\bcall [^\n]*@user_binding_make\(', body)) == 1


def test_constructor_temporary_pointer_payload_has_an_owned_projection():
    source = PREFIX.replace('FIELD', 'list') + 'def probe():\n    return take(value=Record([1]).value)\n'
    # Constructors now publish their leaves in registered owning slots. The
    # same program executes in the aggregate ownership native regression.
    _emit(source)


def test_raw_pointer_field_is_not_admitted_as_a_managed_owner():
    source = PREFIX.replace('import pcc', 'import pcc\nfrom pcc.extern import c_ptr').replace('FIELD', 'c_ptr') + 'def probe(record: Record):\n    return take(value=record.value)\n'
    with pytest.raises(L1CodegenError, match='raw'):
        _emit(source)


@pytest.mark.parametrize('registered', (False, True))
def test_pointer_source_requires_exact_slot_registration(registered):
    aggregate, field, unrelated = object(), object(), object()
    host = SimpleNamespace(current_function=SimpleNamespace(name='probe'),
        _fn_valueclass_payload_root_slots={'probe': [(aggregate, (1,), field, True)]},
        _fn_gc_root_slot_registry={'probe': [('field', field if registered else unrelated)]})
    if registered:
        assert AttrLoadLoweringMixin._slot_call_valueclass_field_source(host, aggregate, (1,), False) == (field, True)
    else:
        with pytest.raises(L1CodegenError, match='no authoritative registered payload slot'):
            AttrLoadLoweringMixin._slot_call_valueclass_field_source(host, aggregate, (1,), False)
