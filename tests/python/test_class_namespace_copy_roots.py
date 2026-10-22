"""Execute class namespace copying with independently moving item owners.

These production-body models supplement, and do not replace, native five-GC
replays of prepared dict subclasses and type(name, bases, namespace).
"""
import ast
from pathlib import Path

import pytest

from pcc.runtime.py import py_abi_constants as abi
from tests.python.test_dict_super_slot_roots import DictMemory
from tests.python.test_set_call_slot_roots import Block, Object
from tests.python.runtime_model_declarations import global_i32_declarations


ROOT = Path(__file__).resolve().parents[2] / 'pcc/runtime/py'
PHASES = ('register', 'copy', 'acquire', 'callback', 'release', 'move', 'drop', 'unregister', 'lock')


class NamespaceMemory(DictMemory):
    def __init__(self, phase='none', **kwargs):
        super().__init__(phase, **kwargs)
        self.maps = {}
        self.frames = []
        self.writes = []
        self.mutate_source = False
        self.fail_write = False
        self.cleanup_error = False
        self.ns.update({
            'c_ptr': object,
            'strlen': self.strlen,
            'pcc_gc_frame_enter': self.frame_enter,
            'pcc_gc_frame_leave': self.frame_leave,
            'pcc_gc_pin': self.pin,
            'pcc_gc_take_pinned_slot': self.take_pinned,
            'py_str_utf8': self.name,
            'py_list_get': self.list_get,
            'py_tuple_get': self.tuple_get,
            'py_class_write_namespace_key_slots': self.publish_key,
            'py_class_abort_definition_slots': self.abort_class,
            'py_class_new_abi': self.class_new,
            '_ptr_is_class': lambda value: isinstance(value, Object) and value.alive and value.tag == abi.PY_TYPE_CLASS,
            '_ptr_can_have_header': lambda value: isinstance(value, Object) and value.alive,
        })
        for file, prefixes, names in (
            ('py_protocol_runtime.py', (), {'py_dict_storage_slots'}),
            ('freestanding_class_namespace.py', (), {'_class_namespace_source_locked', '_class_namespace_copy_locked', 'pcc_class_namespace_acquire_slots', 'pcc_class_namespace_install_slots'}),
            ('py_dict.py', ('_dict_materialize_',), {'py_dict_items', 'py_dict_get_default_slots'}),
            ('py_class.py', ('_class_apply_',), {'_special_open', '_special_copy', '_special_adopt',
                '_special_drop', '_special_close', '_special_error', '_special_publish',
                'py_class_apply_namespace_dict', '_class_new_from_objects_body', 'py_class_new_from_objects',
                '_class_write_acquire_namespace', '_class_read_namespace_body', 'py_class_read_namespace_slots'}),
        ):
            path = ROOT / file
            body = []
            module = ast.parse(path.read_text())
            self.maps.update(global_i32_declarations(module, self.ns))
            for node in module.body:
                if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id.startswith(('_CLASS_APPLY_', '_CLASS_OBJECTS_', '_DICT_MATERIALIZE_', '_CLASS_READ_', '_CLASS_WRITE_')) for t in node.targets):
                    body.append(node)
                elif isinstance(node, ast.FunctionDef) and (node.name in names or node.name.startswith(prefixes)):
                    node.decorator_list = []
                    body.append(node)
            exec(compile(ast.Module(body, []), str(path), 'exec'), self.ns)

    def global_addr(self, name):
        # Frame maps are keyed by name; every other global is a zero cell.
        return name if name in self.maps else super().global_addr(name)

    def frame_enter(self, name, storage):
        count = abs(self.maps[name])
        handles = []
        for index in range(count):
            handle = object()
            self.roots[handle] = self.add(storage, index * 8)
            handles.append(handle)
        self.frames.append((storage, handles))

    def frame_leave(self, storage):
        owner, handles = self.frames.pop()
        assert owner is storage
        self.collect('unregister')
        for handle in handles:
            del self.roots[handle]

    def pin(self, value):
        assert value.alive
        value.leases += 1

    def take_pinned(self, slot, prior):
        value = self.read(slot, 0)
        if isinstance(value, Object):
            assert value.alive and value.leases > 0
            value.leases -= 1
        self.write(slot, 0, None)
        return value

    def store_root(self, slot, value):
        super().store_root(slot, value)
        if self.cleanup_error:
            self.error = (7, 'cleanup finalizer')

    def make(self, value, tag):
        obj = super().make(value, tag)
        if tag == abi.PY_TYPE_STR:
            # The native str header carries the UTF-8 byte length.
            obj.fields[abi.PYSTROBJECT_BYTE_LEN_OFFSET] = len(value.encode())
        return obj

    @staticmethod
    def strlen(value):
        # A str's bytes are NUL-terminated; strlen stops at the first NUL.
        text = value.value if isinstance(value, Object) else value
        return len(text.encode().split(b'\0', 1)[0])

    def name(self, value):
        assert value.alive and value.leases > 0
        return value

    def tuple_get(self, container, index):
        assert container.alive and container.leases > 0
        value = self.read(container, abi.PYTUPLEOBJECT_ITEMS_OFFSET + index * 8)
        if isinstance(value, Object):
            value.refs += 1
        return value

    def list_get(self, container, index):
        assert container.alive and container.leases > 0
        value = self.read(self.read(container, abi.PYLISTOBJECT_ITEMS_OFFSET), index * 8)
        if isinstance(value, Object):
            value.refs += 1
        return value

    def publish_key(self, class_slot, key_slot, value_slot):
        name = self.read(key_slot, 0)
        self.collect('callback')
        assert not isinstance(name, Object) or name.alive and name.leases > 0
        cls, value = self.read(class_slot, 0), self.read(value_slot, 0)
        assert cls.alive and cls.leases > 0
        assert not isinstance(value, Object) or value.alive and value.leases > 0
        self.writes.append(self.unwrap(name))
        if self.fail_write:
            self.cleanup_error = True
            self.error = (2, 'publication failed')
            return -1
        if self.mutate_source:
            self.mutate_source = False
            source = self.read(self.caller, 8)
            if source.tag != abi.PY_TYPE_DICT:
                env = self.read(source, abi.PYINSTANCEOBJECT_FIELDS_OFFSET)
                source = next(self.read(self.read(env, abi.PYDICTOBJECT_ENTRIES_OFFSET), index * abi.DICTENTRY_SIZE + abi.DICTENTRY_VALUE_OFFSET)
                    for index in range(self.read(env, abi.PYDICTOBJECT_ENTRIES_USED_OFFSET))
                    if self.unwrap(self.read(self.read(env, abi.PYDICTOBJECT_ENTRIES_OFFSET), index * abi.DICTENTRY_SIZE + abi.DICTENTRY_KEY_OFFSET)) == '\x00pcc.dict.items')
            self.read(source, abi.PYDICTOBJECT_ENTRIES_OFFSET).fields.clear()
            self.write(source, abi.PYDICTOBJECT_ITEM_COUNT_OFFSET, 0)
            self.write(source, abi.PYDICTOBJECT_ENTRIES_USED_OFFSET, 0)
            self.collect('callback')
        namespace = self.read(cls, abi.PYCLASSOBJECT_ATTRS_OFFSET)
        namespace.fields[self.unwrap(name)] = value
        if isinstance(value, Object):
            value.refs += 1
        return 0

    def abort_class(self, slot):
        cls = self.read(slot, 0)
        self.read(cls, abi.PYCLASSOBJECT_ATTRS_OFFSET).fields.clear()

    def class_new(self, name, bases, count, fields, n_fields):
        self.collect('callback')
        assert name.alive and name.leases > 0
        for index in range(count):
            assert self.read(bases, index * 8).alive
        return self.make_class(name.value)

    def make_class(self, label='target', flags=0):
        cls = self.make(label, abi.PY_TYPE_CLASS)
        cls.fields.update({abi.PYOBJECTHEADER_FLAGS_OFFSET: flags,
            abi.PYCLASSOBJECT_N_FIELDS_OFFSET: 0,
            abi.PYCLASSOBJECT_ATTRS_OFFSET: Block()})
        return cls

    def source(self, items, subclass):
        if not subclass:
            return self.wrap(items)
        cls = self.make_class('DictSubclass', flags=6)
        receiver = self.make('mapping', abi.PY_TYPE_USER_CLASS_START)
        receiver.fields[abi.PYINSTANCEOBJECT_CLS_OFFSET] = cls
        receiver.fields[abi.PYINSTANCEOBJECT_FIELDS_OFFSET] = self.wrap({'\x00pcc.dict.items': items})
        return receiver

    def apply(self, items, subclass=False, raw=False):
        self.caller = self.input_roots((self.make_class(), self.source(items, subclass)))
        if raw:
            status = self.ns['py_class_apply_namespace_dict'](self.read(self.caller, 0), self.read(self.caller, 8))
        else:
            status = self.ns['_class_apply_namespace_bound'](self.caller, self.add(self.caller, 8), 0)
        assert len(self.roots) == 2 and self.depth == 0 and not self.frames
        assert all(obj.leases == (1000 if obj is self.none else 0) for obj in self.objects if obj.alive)
        cls = self.read(self.caller, 0)
        return status, {key: self.unwrap(value) for key, value in self.read(cls, abi.PYCLASSOBJECT_ATTRS_OFFSET).fields.items()}


@pytest.mark.parametrize('phase', PHASES)
@pytest.mark.parametrize('subclass', (False, True))
@pytest.mark.parametrize('raw', (False, True))
def test_namespace_copy_owns_storage_snapshot_and_each_child(phase, subclass, raw):
    memory = NamespaceMemory(phase)
    memory.mutate_source = True
    assert memory.apply({'first': ('one',), 'second': ('two',)}, subclass, raw) == (0, {'first': ('one',), 'second': ('two',)})
    assert memory.error is None and memory.collections > 0


@pytest.mark.parametrize('phase', PHASES)
def test_type_constructor_keeps_name_bases_namespace_and_result(phase):
    memory = NamespaceMemory(phase)
    caller = memory.input_roots((memory.wrap('Created'), memory.wrap(()), memory.source({'value': ('kept',)}, True)))
    output = memory.ns['py_class_new_from_objects'](memory.read(caller, 0), memory.read(caller, 8), memory.read(caller, 16))
    assert output.alive and memory.error is None
    assert memory.unwrap(memory.read(output, abi.PYCLASSOBJECT_ATTRS_OFFSET).fields['value']) == ('kept',)
    assert len(memory.roots) == 3 and not memory.frames and memory.depth == 0
    assert all(obj.leases == (1000 if obj is memory.none else 0) for obj in memory.objects if obj.alive)


@pytest.mark.parametrize('phase', PHASES)
def test_type_constructor_rejects_a_name_with_nul_before_creating_a_class(phase):
    memory = NamespaceMemory(phase)
    caller = memory.input_roots((memory.wrap('A\0B'), memory.wrap(()), memory.source({'value': ('kept',)}, True)))
    output = memory.ns['py_class_new_from_objects'](memory.read(caller, 0), memory.read(caller, 8), memory.read(caller, 16))
    assert output is None and memory.error[0] == 2
    assert not any(obj.tag == abi.PY_TYPE_CLASS and obj.value == 'A\0B' for obj in memory.objects)
    assert len(memory.roots) == 3 and not memory.frames and memory.depth == 0
    assert all(obj.leases == (1000 if obj is memory.none else 0) for obj in memory.objects if obj.alive)


@pytest.mark.parametrize('case', ('none', 'mapping', 'integer'))
def test_namespace_copy_rejects_non_dicts(case):
    memory = NamespaceMemory('release')
    mapping = memory.source({}, True)
    memory.read(mapping, abi.PYINSTANCEOBJECT_CLS_OFFSET).fields[abi.PYOBJECTHEADER_FLAGS_OFFSET] = 0
    value = memory.none if case == 'none' else 7 if case == 'integer' else mapping
    caller = memory.input_roots((memory.make_class(), value))
    status = memory.ns['_class_apply_namespace_bound'](caller, memory.add(caller, 8), 0)
    assert status == -1 and memory.error[0] == 3
    assert not memory.writes


@pytest.mark.parametrize('key', (1, None, ('key',), 'embedded\x00key'))
@pytest.mark.parametrize('phase', PHASES)
def test_namespace_copy_preserves_original_dictionary_keys(key, phase):
    memory = NamespaceMemory(phase)
    assert memory.apply({key: 'kept'}, True) == (0, {key: 'kept'})


@pytest.mark.parametrize('failure', (1, 2, 3, 4, 7, 15))
def test_namespace_copy_cleans_partial_input_ownership(failure):
    memory = NamespaceMemory('release', fail_copy=failure)
    status, _ = memory.apply({'first': 'one', 'second': 'two'}, True)
    assert status == -1 and memory.error is not None


@pytest.mark.parametrize('registration', (1, 5, 14, 18, 28))
def test_namespace_copy_cleans_partial_root_registration(registration):
    memory = NamespaceMemory('register', fail_register=registration)
    status, _ = memory.apply({'value': 'kept'}, True)
    assert status == -1 and memory.error is not None


def test_namespace_copy_preserves_publication_error_across_cleanup_finalizers():
    memory = NamespaceMemory('release')
    memory.fail_write = True
    status, _ = memory.apply({'value': 'kept'}, False)
    assert status == -1 and memory.error == (2, 'publication failed')


@pytest.mark.parametrize('phase', PHASES)
def test_empty_subclass_gets_owned_storage_without_protocol_calls(phase):
    memory = NamespaceMemory(phase)
    receiver = memory.source({}, True)
    receiver.fields[abi.PYINSTANCEOBJECT_FIELDS_OFFSET] = None
    caller = memory.input_roots((receiver, None))
    assert memory.ns['py_dict_storage_slots'](caller, memory.add(caller, 8)) == 0
    assert memory.unwrap(memory.read(caller, 8)) == {}
    assert memory.error is None and len(memory.roots) == 2
    assert all(obj.leases == (1000 if obj is memory.none else 0) for obj in memory.objects if obj.alive)


@pytest.mark.parametrize('phase', PHASES)
def test_type_constructor_copies_owned_nonempty_base_snapshot(phase):
    memory = NamespaceMemory(phase)
    bases = memory.new_tuple(2)
    memory.set_tuple(bases, 0, memory.make_class('left'))
    memory.set_tuple(bases, 1, memory.make_class('right'))
    caller = memory.input_roots((memory.wrap('Created'), bases, memory.source({'value': ('kept',)}, False)))
    output = memory.ns['py_class_new_from_objects'](memory.read(caller, 0), memory.read(caller, 8), memory.read(caller, 16))
    assert output.alive and memory.error is None
    assert memory.unwrap(memory.read(output, abi.PYCLASSOBJECT_ATTRS_OFFSET).fields['value']) == ('kept',)
    assert len(memory.roots) == 3 and not memory.frames


def test_type_constructor_rolls_back_unexposed_partial_namespace():
    memory = NamespaceMemory('release')
    memory.fail_write = True
    caller = memory.input_roots((memory.wrap('Created'), memory.wrap(()), memory.source({'value': ('kept',)}, True)))
    assert memory.ns['py_class_new_from_objects'](memory.read(caller, 0), memory.read(caller, 8), memory.read(caller, 16)) is None
    assert memory.error == (2, 'publication failed')
    assert len(memory.roots) == 3 and not memory.frames
    for obj in memory.objects:
        if obj.alive and obj.tag == abi.PY_TYPE_CLASS and obj.value == 'Created':
            assert memory.read(obj, abi.PYCLASSOBJECT_ATTRS_OFFSET).fields == {}


def metadata_inputs(memory, binding):
    base = memory.make_class('base')
    original = memory.make_class('derived')
    base.fields[abi.PYCLASSOBJECT_ATTRS_OFFSET] = memory.wrap({'declaration': ('original',)})
    original.fields[abi.PYCLASSOBJECT_ATTRS_OFFSET] = memory.wrap(binding)
    mro = Block()
    mro.fields.update({0: original, 8: base})
    original.fields[abi.PYCLASSOBJECT_N_MRO_OFFSET] = 2
    original.fields[abi.PYCLASSOBJECT_MRO_OFFSET] = mro
    return memory.input_roots((original, None))


@pytest.mark.parametrize('phase', PHASES)
@pytest.mark.parametrize('binding,expected', (({}, ('original',)), ({'declaration': ('override',)}, ('override',)), ({'declaration': None}, None)))
def test_class_metadata_read_owns_current_mro_dictionary_and_result(phase, binding, expected):
    memory = NamespaceMemory(phase)
    caller = metadata_inputs(memory, binding)
    assert memory.ns['py_class_read_namespace_slots'](caller, 'declaration', memory.add(caller, 8)) == 0
    result = memory.read(caller, 8)
    assert isinstance(result, Object) and result.alive
    assert memory.unwrap(result) == expected
    assert memory.error is None and len(memory.roots) == 2
    assert all(obj.leases == (1000 if obj is memory.none else 0) for obj in memory.objects if obj.alive)


def test_class_metadata_absence_has_no_owner_or_exception():
    memory = NamespaceMemory('release')
    caller = metadata_inputs(memory, {})
    assert memory.ns['py_class_read_namespace_slots'](caller, 'missing', memory.add(caller, 8)) == 0
    assert memory.read(caller, 8) is None and memory.error is None
    assert len(memory.roots) == 2


@pytest.mark.parametrize('failure', (1, 2, 3, 4, 6))
def test_class_metadata_read_retires_partial_owners(failure):
    memory = NamespaceMemory('release', fail_copy=failure)
    caller = metadata_inputs(memory, {})
    assert memory.ns['py_class_read_namespace_slots'](caller, 'declaration', memory.add(caller, 8)) == -1
    assert memory.read(caller, 8) is None and memory.error is not None
    assert len(memory.roots) == 2 and memory.depth == 0
