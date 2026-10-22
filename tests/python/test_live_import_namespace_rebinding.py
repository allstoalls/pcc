"""Live imported names share the module dictionary's binding and ownership.

Host checks do not build a runtime. The companion integration file executes
the same source fixtures with an explicitly selected, matched runtime archive.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest

from tests.python.test_namespace_value_sources import _assert_immediate_publication
from tests.python.test_owned_import_binding_roots import PROVIDERS, body, emit


PROVIDER = '''payload = ['original']
def compute(value):
    return ('original', value)
'''

REPLACEMENT = '''payload = ['replacement']
def compute(value):
    return ('replacement', value)
'''

BINDINGS = '''import live_provider as values
from live_provider import payload as item, compute as operation

def identity(value):
    return value

def namespace():
    return globals()

def read_module():
    return values

def read_item():
    return item

def item_argument():
    return identity(item)

def read_attribute():
    return values.payload

def attribute_argument():
    return identity(values.payload)

def call_attribute(value):
    return values.compute(value)

def call_operation(value):
    return operation(value)

def source_bind(replacement):
    global values
    values = replacement

def source_delete():
    global values
    del values

def import_global():
    global values
    import live_provider as values

def local_import():
    import live_provider as values
    return values

def local_argument(values):
    return identity(values)

def conditional_local(flag):
    if flag:
        import live_provider as values
    return values

def read_before_local_import():
    result = values
    import live_provider as values
    return result

def conditional_global(flag):
    global conditional_values
    if flag:
        import live_provider as conditional_values

def read_conditional_global():
    return conditional_values

def generate_values():
    yield values
    yield values

def generate_local():
    import live_provider as values
    yield values
    yield values
'''

PUBLICATION = '''import live_provider as values
import live_replacement as replacement
globals()['values'] = replacement

def read():
    return values
'''

STAR_EXPORTS = "from live_replacement import payload as item\n__all__ = ['item']\n"
STAR_BINDINGS = '''from live_provider import payload as item
from live_star_exports import *

def read():
    return item
'''

ENTRY_PREFIX = '''import gc
import live_provider as original
import live_replacement as replacement
import live_bindings as consumer

def external_bind(module, value):
    module.values = value

def expect_missing():
    try:
        consumer.read_module()
    except NameError:
        return
    raise AssertionError('deleted import remained readable')
'''

BINDING_ENTRY = ENTRY_PREFIX + '''import live_publication as published
import live_star_bindings as star

def main():
    assert consumer.read_module() is original
    namespace = consumer.namespace()
    namespace['values'] = replacement
    assert consumer.read_module() is replacement
    assert consumer.namespace() is namespace
    assert consumer.namespace()['values'] is replacement
    assert consumer.namespace()['values'] is replacement
    external_bind(consumer, original)
    assert consumer.read_module() is original
    assert consumer.namespace()['values'] is original
    consumer.source_bind(replacement)
    assert consumer.read_module() is replacement
    assert namespace['values'] is replacement
    consumer.source_delete()
    expect_missing()
    assert 'values' not in consumer.namespace()
    namespace['values'] = original
    assert consumer.read_module() is original
    consumer.source_delete()
    expect_missing()
    external_bind(consumer, replacement)
    assert consumer.read_module() is replacement
    del namespace['values']
    expect_missing()
    assert 'values' not in consumer.namespace()
    try:
        consumer.source_delete()
    except NameError:
        pass
    else:
        raise AssertionError('source deletion ignored missing live binding')
    consumer.import_global()
    assert consumer.read_module() is original
    assert consumer.namespace()['values'] is original
    new_item = ['new imported value']
    namespace['item'] = new_item
    assert consumer.read_item() is new_item
    assert consumer.item_argument() is new_item
    assert consumer.namespace()['item'] is new_item
    assert published.values is replacement
    assert published.read() is replacement
    assert star.item is replacement.payload
    assert star.read() is replacement.payload
    gc.collect()
    print('LIVE_IMPORT_BINDINGS_OK')
main()
'''

SCOPE_ENTRY = ENTRY_PREFIX + '''def main():
    namespace = consumer.namespace()
    namespace['values'] = replacement
    assert consumer.local_import() is original
    assert consumer.local_argument(replacement) is replacement
    assert consumer.read_module() is replacement
    assert consumer.conditional_local(True) is original
    try:
        consumer.conditional_local(False)
    except UnboundLocalError:
        pass
    else:
        raise AssertionError('unexecuted local import became global')
    try:
        consumer.read_before_local_import()
    except UnboundLocalError:
        pass
    else:
        raise AssertionError('read before local import escaped local scope')
    consumer.conditional_global(False)
    try:
        consumer.read_conditional_global()
    except NameError:
        pass
    else:
        raise AssertionError('unexecuted global import became bound')
    namespace['conditional_values'] = replacement
    assert consumer.read_conditional_global() is replacement
    consumer.conditional_global(True)
    assert consumer.read_conditional_global() is original
    generated = consumer.generate_values()
    assert next(generated) is replacement
    namespace['values'] = original
    gc.collect()
    assert next(generated) is original
    generated.close()
    local = consumer.generate_local()
    assert next(local) is original
    namespace['values'] = replacement
    gc.collect()
    assert next(local) is original
    local.close()
    assert consumer.read_module() is replacement
    print('LIVE_IMPORT_SCOPES_OK')
main()
'''

CALL_ENTRY = ENTRY_PREFIX + '''def main():
    namespace = consumer.namespace()
    assert consumer.read_attribute() is original.payload
    assert consumer.call_attribute(3) == ('original', 3)
    namespace['values'] = replacement
    assert consumer.read_attribute() is replacement.payload
    assert consumer.attribute_argument() is replacement.payload
    assert consumer.call_attribute(4) == ('replacement', 4)
    namespace['operation'] = replacement.compute
    assert consumer.call_operation(5) == ('replacement', 5)
    assert consumer.namespace()['operation'] is replacement.compute
    del namespace['operation']
    try:
        consumer.call_operation(6)
    except NameError:
        pass
    else:
        raise AssertionError('deleted imported callable remained callable')
    namespace['operation'] = original.compute
    assert consumer.call_operation(7) == ('original', 7)
    print('LIVE_IMPORT_CALLS_OK')
main()
'''

LIFETIME_PROVIDER = '''import weakref
events = []
class Token:
    def __del__(self):
        events.append('released')
payload = Token()
reference = weakref.ref(payload)
def release_provider():
    global payload
    payload = None
def compute(value):
    return value
'''

LIFETIME_ENTRY = ENTRY_PREFIX + '''def main():
    namespace = consumer.namespace()
    original.release_provider()
    assert original.reference() is not None
    namespace['item'] = None
    gc.collect()
    assert consumer.read_item() is None
    assert original.reference() is None
    assert original.events == ['released']
    print('LIVE_IMPORT_LIFETIME_OK')
main()
'''

OPERATIONS_PROVIDER = '''import gc
import weakref
events = []
class Token:
    def __init__(self, label):
        self.label = label
        self.field = 19
    def __setitem__(self, key, value):
        assert key == 'field'
        self.field = value
    def __iadd__(self, value):
        gc.collect()
        self.field += value
        return self
    def __del__(self):
        events.append(self.label)
set_receiver = Token('set')
del_receiver = Token('del')
int_receiver = Token('int')
attr_receiver = Token('attr-unpack')
item_receiver = Token('item-unpack')
operand = Token('inplace')
set_reference = weakref.ref(set_receiver)
del_reference = weakref.ref(del_receiver)
int_reference = weakref.ref(int_receiver)
attr_reference = weakref.ref(attr_receiver)
item_reference = weakref.ref(item_receiver)
operand_reference = weakref.ref(operand)
members = {1, 2}
placeholder = None
default_reference = None
def make_default():
    global default_reference
    token = Token('default')
    default_reference = weakref.ref(token)
    return token
def release_provider():
    global set_receiver, del_receiver, int_receiver, attr_receiver, item_receiver, operand
    set_receiver = None
    del_receiver = None
    int_receiver = None
    attr_receiver = None
    item_receiver = None
    operand = None
'''

OPERATIONS_BINDINGS = '''import gc
import live_operations_provider as _provider
from live_operations_provider import (
    set_receiver, del_receiver, int_receiver, attr_receiver, item_receiver,
    operand, members, placeholder as definition,
    operand_reference as _operand_reference,
)

def definition(capture=_provider.make_default()):
    return capture

def namespace():
    return globals()

def set_attribute():
    set_receiver.field = 37
    assert set_receiver.field == 37

def delete_attribute():
    del del_receiver.field

def read_integer():
    return int_receiver.field

def unpack_attribute():
    attr_receiver.field, spare = (41, None)
    assert attr_receiver.field == 41
    assert spare is None

def unpack_item():
    item_receiver['field'], spare = (43, None)
    assert item_receiver.field == 43
    assert spare is None

def _rebind_during_rhs():
    global operand
    operand = None
    gc.collect()
    assert _operand_reference() is not None
    return 7

def inplace_arithmetic():
    global operand
    operand += _rebind_during_rhs()
    assert operand.field == 26
    assert operand is _operand_reference()

def inplace_sets():
    global members
    alias = members
    members |= {3}
    assert members is alias and members == {1, 2, 3}
    members &= {2, 3}
    assert members is alias and members == {2, 3}
    members ^= {3, 4}
    assert members is alias and members == {2, 4}

def delete_definition():
    global definition
    del definition
'''

OPERATIONS_ENTRY = '''import gc
import live_operations_provider as provider
import live_operations_bindings as consumer

def main():
    namespace = consumer.namespace()
    provider.release_provider()
    consumer.set_attribute()
    namespace['set_receiver'] = None
    gc.collect()
    assert provider.set_reference() is None
    assert provider.events == ['set']
    consumer.delete_attribute()
    namespace['del_receiver'] = None
    gc.collect()
    assert provider.del_reference() is None
    assert provider.events == ['set', 'del']
    assert consumer.read_integer() == 19
    namespace['int_receiver'] = None
    gc.collect()
    assert provider.int_reference() is None
    assert provider.events == ['set', 'del', 'int']
    consumer.unpack_attribute()
    namespace['attr_receiver'] = None
    gc.collect()
    assert provider.attr_reference() is None
    assert provider.events == ['set', 'del', 'int', 'attr-unpack']
    consumer.unpack_item()
    namespace['item_receiver'] = None
    gc.collect()
    assert provider.item_reference() is None
    assert provider.events == ['set', 'del', 'int', 'attr-unpack', 'item-unpack']
    consumer.inplace_arithmetic()
    namespace['operand'] = None
    gc.collect()
    assert provider.operand_reference() is None
    assert provider.events == ['set', 'del', 'int', 'attr-unpack', 'item-unpack', 'inplace']
    consumer.inplace_sets()
    assert provider.members == {2, 4}
    assert provider.default_reference() is not None
    consumer.delete_definition()
    gc.collect()
    assert provider.default_reference() is None
    assert provider.events == ['set', 'del', 'int', 'attr-unpack', 'item-unpack', 'inplace', 'default']
    print('LIVE_IMPORT_OPERATIONS_OK')
main()
'''

SCENARIOS = {
    'bindings': (BINDING_ENTRY, 'LIVE_IMPORT_BINDINGS_OK\n'),
    'scopes': (SCOPE_ENTRY, 'LIVE_IMPORT_SCOPES_OK\n'),
    'calls': (CALL_ENTRY, 'LIVE_IMPORT_CALLS_OK\n'),
    'lifetime': (LIFETIME_ENTRY, 'LIVE_IMPORT_LIFETIME_OK\n'),
    'operations': (OPERATIONS_ENTRY, 'LIVE_IMPORT_OPERATIONS_OK\n'),
}


def write_sources(directory, scenario):
    entry, expected = SCENARIOS[scenario]
    sources = [
        ('live_provider', LIFETIME_PROVIDER if scenario == 'lifetime' else PROVIDER),
        ('live_replacement', REPLACEMENT),
        ('live_bindings', BINDINGS),
    ]
    if scenario == 'operations':
        sources = [
            ('live_operations_provider', OPERATIONS_PROVIDER),
            ('live_operations_bindings', OPERATIONS_BINDINGS),
        ]
    if scenario == 'bindings':
        sources.extend([
            ('live_publication', PUBLICATION),
            ('live_star_exports', STAR_EXPORTS),
            ('live_star_bindings', STAR_BINDINGS),
        ])
    sources.append(('live_entry', entry))
    paths, names = [], []
    for name, source in sources:
        path = directory / (name + '.py')
        path.write_text(source, encoding='utf-8')
        paths.append(str(path))
        names.append(name)
    return paths, names, expected


def run_reference(directory, paths, expected):
    environment = dict(os.environ)
    environment.pop('LC_ALL', None)
    result = subprocess.run(
        [sys.executable, paths[-1]], cwd=directory, env=environment,
        capture_output=True, text=True, timeout=20,
    )
    (directory / 'reference.stdout').write_text(result.stdout)
    (directory / 'reference.stderr').write_text(result.stderr)
    assert (result.returncode, result.stdout, result.stderr) == (0, expected, ''), result


@pytest.mark.parametrize('scenario', SCENARIOS)
def test_live_import_reference(tmp_path, scenario):
    paths, _names, expected = write_sources(tmp_path, scenario)
    run_reference(tmp_path, paths, expected)


@pytest.mark.parametrize('statement', [
    'return values',
    'return str(values)',
    'return values.value',
    'return str(values.value)',
    'return values.fetch(None)',
])
def test_live_import_reads_namespace_before_consuming_value(tmp_path, monkeypatch, statement):
    text = emit(tmp_path, monkeypatch, 'import provider as values\ndef probe():\n    ' + statement + '\n')
    probe = body(text, 'user_consumer_probe')
    _assert_immediate_publication(probe, 'py_module_attr_get')
    assert 'name.dynamic.values' in probe
    assert not re.search(r'load ptr, ptr @\.modvar\.consumer\.values(?:[,\s])', probe)
    if 'values.' in statement:
        _assert_immediate_publication(probe, 'py_obj_getattr')
        assert probe.index('@py_module_attr_get') < probe.index('@py_obj_getattr')
    assert not re.search(r'call [^\n]*@user_provider_fetch\(', probe)


def test_globals_does_not_republish_import_slot(tmp_path, monkeypatch):
    text = emit(tmp_path, monkeypatch,
                "import provider as values\ndef probe():\n    globals()['values'] = None\n    return globals()\n")
    probe = body(text, 'user_consumer_probe')
    assert len(re.findall(r'call [^\n]*@py_module_attrs_dict\(', probe)) == 2
    assert not re.search(r'call [^\n]*@py_module_attr_set[^\n]*@\.pyattr\.values[, ]', probe)
    assert not re.search(r'load ptr, ptr @\.modvar\.consumer\.values(?:[,\s])', probe)


def test_import_delete_checks_live_namespace(tmp_path, monkeypatch):
    text = emit(tmp_path, monkeypatch,
                'import provider as values\ndef probe():\n    global values\n    del values\n')
    probe = body(text, 'user_consumer_probe')
    assert re.search(r'call [^\n]*@py_module_attr_(?:get|del)\(', probe)
    assert not re.search(r'load i1, ptr @\.modvar\.consumer\.values_initialized', probe)
    assert re.search(r'call [^\n]*@py_exc_new\(i64 10,', probe), 'missing NameError on deleted live binding'


@pytest.mark.parametrize('prefix', ['', '    global values\n'])
def test_import_scope_keeps_local_slots_distinct_from_live_globals(tmp_path, monkeypatch, prefix):
    text = emit(tmp_path, monkeypatch,
                'def probe():\n' + prefix + '    import provider as values\n    return str(values)\n')
    probe = body(text, 'user_consumer_probe')
    if prefix:
        _assert_immediate_publication(probe, 'py_module_attr_get')
        assert 'name.dynamic.values' in probe
    else:
        assert 'name.dynamic.values' not in probe
        assert '@pcc_gc_root_move' in probe
        assert re.search(r'call [^\n]*@pcc_gc_root_copy_lease\(', probe)


def test_owned_namespace_return_transfers_its_result(tmp_path, monkeypatch):
    text = emit(tmp_path, monkeypatch,
                'from provider import value\ndef probe():\n    return value\n')
    probe = body(text, 'user_consumer_probe')
    _assert_immediate_publication(probe, 'py_module_attr_get')
    assert '@pcc_gc_take_pinned_slot(' in probe
    assert 'return.retain' not in probe, 'NEW namespace result was retained as a borrowed global'


def _assert_rooted_receiver(probe, helper):
    aliases = dict(re.findall(r'(%[\w.]+) = bitcast ptr ([%@][\w.]+) to ptr', probe))

    def physical_root(value):
        seen = set()
        while value in aliases:
            assert value not in seen, 'cyclic pointer aliases'
            seen.add(value)
            value = aliases[value]
        return value

    call = re.search(r'call [^\n]*@' + helper + r'\(ptr (%[\w.]+)[^\n]*', probe)
    assert call is not None, helper
    loaded = re.search(re.escape(physical_root(call.group(1))) + r' = load ptr, ptr (%[\w.]+)', probe)
    assert loaded is not None, 'receiver was not read from its operand root'
    receiver_root = physical_root(loaded.group(1))
    assert 'receiver' in receiver_root
    acquired = re.findall(r'call [^\n]*@pcc_gc_foreign_lease_acquire\(ptr ([%@][\w.]+)\)',
                          probe[:call.start()])
    assert any(physical_root(slot) == receiver_root for slot in acquired), (
        'runtime receiver was not protected by its operand root lease'
    )
    after_call = probe[call.end():]
    released = re.findall(r'call [^\n]*@pcc_gc_foreign_lease_release\(ptr ([%@][\w.]+),', after_call)
    assert any(physical_root(slot) == receiver_root for slot in released), (
        'runtime receiver lease was not released after mutation'
    )
    cleared = re.findall(r'call [^\n]*@pcc_gc_store_root\(ptr ([%@][\w.]+), ptr null\)', after_call)
    assert any(physical_root(slot) == receiver_root for slot in cleared), (
        'receiver owner was not retired after mutation'
    )


@pytest.mark.parametrize('statements,helper', [
    ('receiver.field = 7', 'py_obj_setattr'),
    ('del receiver.field', 'py_obj_delattr'),
    ('receiver.field, spare = (7, None)', 'py_obj_setattr'),
    ("receiver['field'], spare = (7, None)", 'py_obj_assign_subscript'),
])
def test_imported_mutation_receiver_has_a_scoped_owner(tmp_path, monkeypatch, statements, helper):
    source = 'from provider import value as receiver\ndef probe():\n    ' + statements + '\n'
    probe = body(emit(tmp_path, monkeypatch, source), 'user_consumer_probe')
    _assert_immediate_publication(probe, 'py_module_attr_get')
    _assert_rooted_receiver(probe, helper)


def test_imported_integer_attribute_owns_receiver_and_result(tmp_path, monkeypatch):
    monkeypatch.setitem(PROVIDERS, 'provider',
                        'class Token:\n    def __init__(self):\n        self.field = 19\nvalue = Token()\n')
    probe = body(emit(tmp_path, monkeypatch,
                     'from provider import value as receiver\ndef probe():\n    return receiver.field\n'),
                 'user_consumer_probe')
    _assert_immediate_publication(probe, 'py_module_attr_get')
    _assert_immediate_publication(probe, 'py_obj_getattr')
    _assert_rooted_receiver(probe, 'py_obj_getattr')
    assert '@pcc_gc_take_pinned_slot(' in probe
    assert not re.search(r'call [^\n]*@py_int_(?:to_i64_lane|value_i64)\(', probe)


def test_imported_augassign_roots_lhs_before_rhs_callback(tmp_path, monkeypatch):
    source = '''from provider import value as operand
def rhs():
    global operand
    operand = None
    return 7
def probe():
    global operand
    operand += rhs()
'''
    probe = body(emit(tmp_path, monkeypatch, source), 'user_consumer_probe')
    _assert_immediate_publication(probe, 'py_module_attr_get')
    _assert_immediate_publication(probe, 'py_obj_inplace_op')
    rhs_call = re.search(r'call [^\n]*@(?:user_consumer_rhs|py_obj_call_slots)\(', probe)
    assert rhs_call is not None, 'missing RHS callback'
    assert probe.index('name.dynamic.operand') < rhs_call.start()
    assert rhs_call.start() < probe.index('@py_obj_inplace_op(')
    assert 'import.augassign.input' in probe
    assert '@pcc_gc_foreign_lease_acquire(' in probe


@pytest.mark.parametrize('operator,helper', [
    ('|=', 'py_set_update'),
    ('&=', 'py_set_intersection_update'),
    ('^=', 'py_set_symmetric_difference_update'),
])
def test_imported_set_augassign_preserves_inplace_dispatch(tmp_path, monkeypatch, operator, helper):
    monkeypatch.setitem(PROVIDERS, 'provider', 'value = {1, 2}\n')
    probe = body(emit(tmp_path, monkeypatch,
                     'from provider import value as members\ndef probe():\n    global members\n    members ' +
                     operator + ' {2, 3}\n'), 'user_consumer_probe')
    assert '@' + helper + '(' in probe
    assert '@pcc_gc_root_copy_lease(' in probe, 'in-place result must retain the original set identity'


def test_definition_rebinding_retires_import_staging_owner(tmp_path, monkeypatch):
    text = emit(tmp_path, monkeypatch,
                'from provider import value as definition\n'
                'def definition(capture=None):\n    return capture\n')
    top = body(text, 'main')
    publication = re.search(r'call [^\n]*@py_module_attr_set[^\n]*@\.pyattr\.definition[, ]', top)
    assert publication is not None
    # Select the def publication, excluding the earlier executing import.
    publication = re.search(r'pcc\.def\.binding\.publish\.definition[^\n]*@py_module_attr_set[^\n]*', top)
    assert publication is not None, 'missing definition publication'
    tail = top[publication.end():]
    # The slot is addressed directly, or through an identity pointer alias.
    slots = ['@.modvar.consumer.definition']
    slots += re.findall(r'(%[\w.]+) = bitcast ptr @\.modvar\.consumer\.definition to ptr', tail)
    assert any(re.search(r'call [^\n]*@pcc_gc_store_root\(ptr ' + re.escape(slot) + r', ptr null\)', tail)
               for slot in slots), 'definition kept the retired import staging owner'


@pytest.mark.parametrize('source', [
    'from sys import modules as mapping\ndef probe():\n    return str(mapping)\n',
    'def bind():\n    global mapping\n    from sys import modules as mapping\ndef probe():\n    return str(mapping)\n',
])
def test_sys_modules_live_global_is_planned_before_consumers(tmp_path, monkeypatch, source):
    probe = body(emit(tmp_path, monkeypatch, source), 'user_consumer_probe')
    _assert_immediate_publication(probe, 'py_module_attr_get')
    assert 'name.dynamic.mapping' in probe
    assert not re.search(r'load ptr, ptr @\.modvar\.consumer\.mapping(?:[,\s])', probe)


def test_import_publication_retires_staging_owner(tmp_path, monkeypatch):
    text = emit(tmp_path, monkeypatch, 'import provider as values\ndef probe():\n    return values\n')
    main = body(text, 'main')
    publication = re.search(r'call [^\n]*@py_module_attr_set[^\n]*@\.pyattr\.values[, ]', main)
    assert publication is not None
    retirement = re.search(r'%import.publish.current[^\n]*= load ptr, ptr @\.modvar\.consumer\.values', main)
    assert retirement is not None
    assert publication.start() < retirement.start()
    tail = main[retirement.start():]
    assert re.search(r'call [^\n]*@pcc_gc_store_root\([^\n]*ptr null\)', tail)


@pytest.mark.parametrize('statements,helper', [
    ('replacement = (7, None)\n    receiver.field, spare = replacement', 'py_obj_setattr'),
    ("replacement = (7, None)\n    receiver['field'], spare = replacement", 'py_obj_assign_subscript'),
    ("receiver['field'] = 7", 'py_obj_assign_subscript'),
    ("del receiver['field']", 'py_obj_delete_subscript'),
    ('receiver[:] = [7]', 'py_obj_set_slice'),
    ('del receiver[:]', 'py_obj_del_slice'),
])
def test_imported_container_mutation_owns_all_receivers(tmp_path, monkeypatch, statements, helper):
    source = 'from provider import value as receiver\ndef probe():\n    ' + statements + '\n'
    probe = body(emit(tmp_path, monkeypatch, source), 'user_consumer_probe')
    _assert_immediate_publication(probe, 'py_module_attr_get')
    _assert_rooted_receiver(probe, helper)


def test_imported_class_is_not_republished_by_globals(tmp_path, monkeypatch):
    monkeypatch.setitem(PROVIDERS, 'provider', 'class Box:\n    pass\n')
    text = emit(tmp_path, monkeypatch,
                'from provider import Box\ndef probe():\n    globals()["Box"] = None\n    return globals()\n')
    probe = body(text, 'user_consumer_probe')
    assert not re.search(r'call [^\n]*@py_module_attr_set[^\n]*@\.pyattr\.Box[, ]', probe)
