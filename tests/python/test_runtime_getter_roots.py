"""Execute runtime getter producers with relocation at their actual park gates."""

import ast
from pathlib import Path

import pytest

from pcc.runtime.py import py_abi_constants as abi
from test_exception_constructor_roots import _Object, _Slots


ROOT = Path(__file__).resolve().parents[2]


class _GetterMemory:
    def __init__(self, backend=4, phase="unregister", prior_pin=False, fail_register=0):
        self.backend, self.phase, self.fail_register = backend, phase, fail_register
        self.objects, self.payloads, self.handles, self.forward = [], [], {}, {}
        self.depth = self.moves = self.pins = self.registrations = 0
        self.error = None
        self.item = self.make(abi.PY_TYPE_STR, abi.PY_FLAG_GC_PINNED if prior_pin else 0)
        self.items = _Slots(8)
        self.items.fields[0] = self.item
        self.payloads.append(self.items)
        self.lst = self.make(abi.PY_TYPE_LIST)
        self.lst.fields.update({abi.PYLISTOBJECT_LENGTH_OFFSET: 1,
                                abi.PYLISTOBJECT_CAPACITY_OFFSET: 1,
                                abi.PYLISTOBJECT_ITEMS_OFFSET: self.items})
        self.namespace = self._namespace()
        self.load("pcc/runtime/py/py_list.py")

    def load(self, relative):
        path = ROOT / relative
        parsed = ast.parse(path.read_text(), filename=str(path))
        # Functions plus the module's constant assignments they read (for
        # example py_class's _INSTANCE_LOOKUP_VARS); nothing else executes.
        functions = [
            node for node in parsed.body
            if isinstance(node, ast.FunctionDef)
            or (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name) and node.targets[0].id.isupper()
                and isinstance(node.value, (ast.Constant, ast.UnaryOp, ast.BinOp)))
        ]
        exec(compile(ast.Module(body=functions, type_ignores=[]), str(path), "exec"), self.namespace)

    def make(self, tag, flags=0):
        value = _Object(tag, flags)
        self.objects.append(value)
        return value

    def resolve(self, value):
        while isinstance(value, _Object) and value in self.forward:
            value = self.forward[value]
        return value

    def pointer(self, value):
        return value if isinstance(value, tuple) else (value, 0)

    def add(self, value, offset):
        owner, base = self.pointer(value)
        return owner, base + offset

    def read(self, value, offset):
        value, start = self.pointer(value)
        offset += start
        if isinstance(value, _Object):
            assert value.alive, "stale receiver/field address"
            if offset == 0:
                return value.references
            if offset == abi.PYOBJECTHEADER_TYPE_TAG_OFFSET:
                return value.tag
            if offset == abi.PYOBJECTHEADER_FLAGS_OFFSET:
                return value.flags
        return value.fields.get(offset)

    def write(self, value, offset, item):
        value, start = self.pointer(value)
        offset += start
        if isinstance(value, _Object):
            assert value.alive
            if offset == abi.PYOBJECTHEADER_FLAGS_OFFSET:
                value.flags = item
                return
        value.fields[offset] = item

    def gc(self, phase):
        if self.depth or phase != self.phase or self.backend not in (3, 4):
            return
        for old in list(self.objects):
            if not old.alive or old.flags & (abi.PY_FLAG_IMMORTAL | abi.PY_FLAG_GC_PINNED):
                continue
            new = self.make(old.tag, old.flags)
            new.references, new.fields = old.references, old.fields.copy()
            self.forward[old] = new
            for slot in self.handles.values():
                owner, offset = self.pointer(slot)
                if self.read(owner, offset) is old:
                    self.write(owner, offset, new)
            for obj in self.objects:
                if obj.alive:
                    obj.fields = {offset: new if value is old else value
                                  for offset, value in obj.fields.items()}
            for payload in self.payloads:
                payload.fields = {offset: new if value is old else value
                                  for offset, value in payload.fields.items()}
            if self.item is old:
                self.item = new
            if self.lst is old:
                self.lst = new
            old.alive = False
            self.moves += 1

    def register(self, slot):
        self.registrations += 1
        if self.registrations == self.fail_register:
            return None
        handle = object()
        self.handles[handle] = slot
        return handle

    def unregister(self, handle):
        # Registry lock acquisition can park before unlinking the root.
        self.gc("unregister")
        del self.handles[handle]

    def lock(self):
        self.gc("graph_lock")
        self.depth += 1

    def unlock(self):
        self.depth -= 1
        self.gc("graph_unlock")

    def incref(self, value):
        if isinstance(value, _Object):
            assert value.alive, "stale result during retain"
            if not value.flags & abi.PY_FLAG_IMMORTAL:
                value.references += 1

    def retain(self, plan, value):
        assert self.depth > 0
        self.incref(value)
        plan.fields[0] = value
        return value

    def retain_finish(self, plan):
        self.gc("retain_finish")

    def pin(self, value):
        if isinstance(value, _Object):
            assert value.alive
            value.flags |= abi.PY_FLAG_GC_PINNED
            self.pins += 1

    def take(self, slot, prior):
        owner, offset = self.pointer(slot)
        value = self.read(owner, offset)
        self.write(owner, offset, None)
        if isinstance(value, _Object):
            assert value.alive and value.flags & abi.PY_FLAG_GC_PINNED
            value.flags = (value.flags & ~abi.PY_FLAG_GC_PINNED) | prior
            self.pins -= 1
        return value

    def take_list_item(self, _owner, slot):
        owner, offset = self.pointer(slot)
        value = self.read(owner, offset)
        self.write(owner, offset, None)
        return value

    def _namespace(self):
        namespace = {name: getattr(abi, name) for name in dir(abi) if name.isupper()}
        namespace.update({
            "c_abi_export": lambda _name: lambda function: function,
            "c_ptr": object, "c_int64": int, "null": lambda: None,
            "ptr_is_null": lambda value: value is None,
            "is_tagged_int": lambda value: isinstance(value, int),
            "ptr_eq": lambda first, second: first is second,
            "ptr_add": self.add, "stack_alloc": _Slots,
            "load_ptr": self.read, "load_i64": self.read, "load_i32": self.read,
            "store_ptr": self.write,
            "pcc_gc_load_ptr": lambda _owner, slot: self.read(*self.pointer(slot)),
            "pcc_gc_pointer_is_managed": lambda value: isinstance(value, _Object) and value.alive,
            "pcc_gc_backend": lambda: self.backend,
            "pcc_gc_scheduler_root_register_handle": self.register,
            "pcc_gc_scheduler_root_unregister_handle": self.unregister,
            "pcc_py_gc_minor_graph_lock": self.lock,
            "pcc_py_gc_minor_graph_unlock": self.unlock,
            "pcc_gc_retain_plan_prepare_locked": self.retain,
            "pcc_gc_retain_plan_finish": self.retain_finish,
            "pcc_gc_pin": self.pin, "pcc_gc_take_pinned_slot": self.take,
            "py_incref": self.incref, "pcc_gc_try_take_ptr": self.take_list_item,
            "cstr": lambda text: text, "py_exc_new": lambda tag, text: (tag, text),
            "py_raise_owned": lambda error: setattr(self, "error", error),
        })
        return namespace


@pytest.mark.parametrize("name", ("py_list_get", "py_list_getitem", "py_list_get_for_frame"))
@pytest.mark.parametrize("backend", (3, 4))
@pytest.mark.parametrize("phase", ("graph_unlock", "retain_finish", "unregister"))
def test_list_getter_returns_current_owned_element_after_actual_cleanup_park(name, backend, phase):
    memory = _GetterMemory(backend=backend, phase=phase)
    result = memory.namespace[name](memory.lst, 0)
    assert result is memory.item and result.alive
    assert result.references == 2
    assert memory.moves > 0
    assert not memory.handles and memory.depth == 0 and memory.pins == 0


@pytest.mark.parametrize("backend", (0, 1, 2))
@pytest.mark.parametrize("name", ("py_list_get", "py_list_getitem", "py_list_get_for_frame"))
def test_list_getter_nonmoving_paths_keep_reference_contract(name, backend):
    memory = _GetterMemory(backend=backend)
    result = memory.namespace[name](memory.lst, 0)
    assert result is memory.item
    assert result.references == (1 if backend == 0 and name == "py_list_get_for_frame" else 2)
    assert not memory.handles and not memory.moves and not memory.pins


@pytest.mark.parametrize("name", ("py_list_get", "py_list_getitem", "py_list_get_for_frame"))
def test_list_getter_restores_existing_pin(name):
    memory = _GetterMemory(prior_pin=True)
    result = memory.namespace[name](memory.lst, 0)
    assert result is memory.item and result.flags & abi.PY_FLAG_GC_PINNED
    assert result.references == 2
    assert not memory.handles and not memory.pins


@pytest.mark.parametrize("name", ("py_list_get", "py_list_getitem", "py_list_get_for_frame"))
@pytest.mark.parametrize("backend", (3, 4))
@pytest.mark.parametrize("value", (None, 85))
def test_list_getter_null_and_tagged_elements_need_no_pin(name, backend, value):
    memory = _GetterMemory(backend=backend)
    memory.items.fields[0] = value
    assert memory.namespace[name](memory.lst, -1) == value
    assert not memory.handles and not memory.pins and memory.error is None


@pytest.mark.parametrize("name", ("py_list_get", "py_list_getitem", "py_list_get_for_frame"))
@pytest.mark.parametrize("failed_root", (1, 2))
def test_list_getter_registration_failure_does_not_acquire_element_owner(name, failed_root):
    memory = _GetterMemory(fail_register=failed_root)
    assert memory.namespace[name](memory.lst, 0) is None
    assert memory.item.references == 1
    assert not memory.handles and not memory.pins


@pytest.mark.parametrize("name", ("py_list_get", "py_list_getitem", "py_list_get_for_frame"))
def test_list_getter_out_of_range_balances_roots_and_keeps_raising_contract(name):
    memory = _GetterMemory()
    assert memory.namespace[name](memory.lst, 1) is None
    assert memory.item.references == 1
    assert not memory.handles and not memory.pins
    assert memory.error == ((5, "list index out of range") if name == "py_list_getitem" else None)


class _FieldMemory(_GetterMemory):
    def __init__(self, backend=4, stale_receiver=False):
        super().__init__(backend=backend, phase="before_call")
        self.klass = self.make(abi.PY_TYPE_CLASS, abi.PY_FLAG_IMMORTAL)
        self.klass.fields[abi.PYCLASSOBJECT_N_FIELDS_OFFSET] = 1
        self.record = self.make(abi.PY_TYPE_USER_CLASS_START)
        self.record.fields[abi.PYINSTANCEOBJECT_CLS_OFFSET] = self.klass
        self.record.fields[abi.PYINSTANCEOBJECT_FIELDS_OFFSET] = self.item
        self.original = self.record
        self.globals = {name: _Slots(8) for name in
                        ("pcc_gc_read_barrier_enabled", "pcc_gc_backend_selected")}
        self.globals["pcc_gc_read_barrier_enabled"].fields[0] = int(backend in (3, 4))
        self.globals["pcc_gc_backend_selected"].fields[0] = backend
        self.namespace.update({"global_addr": self.globals.__getitem__,
                               "pcc_gc_note_relocation_read": self.resolve})
        self.load("pcc/runtime/py/py_class.py")
        if stale_receiver:
            self.gc("before_call")
            self.record = self.resolve(self.record)


@pytest.mark.parametrize("backend", (3, 4))
@pytest.mark.parametrize("name", ("py_instance_get_field", "py_valuebox_get_field"))
def test_instance_field_getter_uses_healed_receiver_before_deriving_field_slot(backend, name):
    memory = _FieldMemory(backend=backend, stale_receiver=True)
    assert not memory.original.alive and memory.record.alive
    result = memory.namespace[name](memory.original, 0)
    assert result is memory.item and result.alive
    assert result.references == 2
    assert not memory.handles and memory.depth == 0 and memory.pins == 0


@pytest.mark.parametrize("backend", (0, 1, 2))
def test_instance_field_getter_keeps_nonmoving_fast_path(backend):
    memory = _FieldMemory(backend=backend)
    result = memory.namespace["py_instance_get_field"](memory.record, 0)
    assert result is memory.item and result.references == 2
    assert not memory.moves and not memory.handles and not memory.pins


class _MissingFieldMemory(_FieldMemory):
    """Execute the complete new field-null wrapper and semantic field walk."""
    def __init__(self, backend=4, phase='lookup', cache=False, fail_register=0,
                 result='default', prior_pin=False):
        super().__init__(backend=backend)
        self.phase = phase
        self.release_depth = 0
        self.fail_register = fail_register
        self.klass.flags = 0
        self.record.fields[abi.PYINSTANCEOBJECT_FIELDS_OFFSET] = None
        names = _Slots(8)
        names.fields[0] = 'value'
        self.klass.fields[abi.PYCLASSOBJECT_FIELD_NAMES_OFFSET] = names
        self.klass.fields[abi.PYOBJECTHEADER_FLAGS_OFFSET] = 0
        self.cache = _Slots(24) if cache else None
        if self.cache is not None:
            self.cache.fields.update({12: 1, 16: 0})
        self.cached_kinds = []
        self.result_kind = result
        if prior_pin:
            self.record.flags |= abi.PY_FLAG_GC_PINNED
            self.klass.flags |= abi.PY_FLAG_GC_PINNED
            self.item.flags |= abi.PY_FLAG_GC_PINNED
        self.expected_pin = abi.PY_FLAG_GC_PINNED if prior_pin else 0
        self.actual_descriptor = self.namespace['_instance_lookup_descriptor']
        self.namespace.update({
            'memset': lambda slot, _value, size: [self.write(slot, i, None) for i in range(0, size, 8)],
            'store_i64': self.write,
            'atomic_rmw_i32': self.atomic_flags,
            'pcc_gc_unpin': self.unpin,
            'py_obj_getattr': self.semantic_getattr,
            '_class_require_result': self.require_result,
            '_ATTR_RES_FIELD': 1, '_ATTR_RES_METHOD': 2,
            '_ATTR_RES_ABSENT': 3, '_ATTR_RES_DATA_DESCRIPTOR': 4,
            '_attr_res_find_any': lambda cls, name: self.cache,
            '_class_attr_cache_epoch': lambda: 1,
            '_cstr_is_dunder_class': lambda name: False,
            '_cstr_is_dunder_dict': lambda name: False,
            '_class_attr_lookup_in_mro': self.class_attribute,
            '_descriptor_is_data': lambda value: False,
            '_descriptor_call_get': lambda value, inst, cls: None,
            '_instance_lookup_descriptor': lambda value, inst, cls, slots, pins: self.namespace['_descriptor_call_get'](value, inst, cls),
            '_lookup_field_index': lambda cls, name: 0,
            '_attr_res_store': lambda cls, name, kind, payload, epoch: self.cached_kinds.append(kind),
            '_attr_res_method_cacheable': lambda: 1,
            '_dynamic_attr_slot': lambda inst: None,
            '_class_lookup_in_mro': lambda cls, name: None,
            'py_err_occurred': lambda: self.error is not None,
            'py_current_exception': lambda: self.error,
            'py_raise': lambda error: setattr(self, 'error', error),
            'py_decref': self.decref,
            'strlen': len,
        })

    def class_attribute(self, cls, name):
        assert cls.alive and name == 'value'
        self.gc('lookup')
        assert cls.alive, 'class moved during fallback lookup'
        if self.result_kind in ('missing', 'error'):
            if self.result_kind == 'error':
                self.error = (4, 'lookup failed')
            return None
        value = (self.resolve(self.record) if self.result_kind == 'receiver'
                 else self.resolve(self.klass) if self.result_kind == 'class'
                 else self.item)
        self.incref(value)
        return value

    def semantic_getattr(self, inst, name):
        assert inst.alive and inst.flags & abi.PY_FLAG_GC_PINNED
        cls = self.read(inst, abi.PYINSTANCEOBJECT_CLS_OFFSET)
        assert cls.alive and cls.flags & abi.PY_FLAG_GC_PINNED
        value = self.namespace['_instance_getattr_default'](inst, cls, name)
        if value is None and self.error is None:
            self.error = (6, 'missing value')
        return value

    def require_result(self, value, name, message):
        if value is None and self.error is None:
            self.error = (7, message)
        return value

    def decref(self, value):
        assert self.depth == 0, 'finalizers must run outside the graph lease'
        self.release_depth += 1
        if isinstance(value, _Object):
            assert value.alive, 'stale temporary during release'
            if not value.flags & abi.PY_FLAG_IMMORTAL:
                value.references -= 1
                if value.references == 0:
                    value.alive = False
                    if value.tag == abi.PY_TYPE_TUPLE:
                        for offset, item in value.fields.items():
                            if offset >= abi.PYTUPLEOBJECT_ITEMS_OFFSET:
                                self.decref(item)
        self.release_depth -= 1
        if self.release_depth == 0:
            self.gc('decref')

    def unpin(self, value):
        assert value.alive
        value.flags &= ~abi.PY_FLAG_GC_PINNED
        self.pins -= 1

    def atomic_flags(self, operation, value, offset, bits, order):
        assert value.alive
        assert operation == 'or' and offset == abi.PYOBJECTHEADER_FLAGS_OFFSET
        value.flags |= bits


@pytest.mark.parametrize('backend', [3, 4])
@pytest.mark.parametrize('phase', ['graph_lock', 'graph_unlock', 'lookup', 'unregister'])
@pytest.mark.parametrize('cache', [False, True])
def test_missing_field_fallback_roots_receiver_class_and_result(backend, phase, cache):
    memory = _MissingFieldMemory(backend=backend, phase=phase, cache=cache)
    result = memory.namespace['py_instance_get_field'](memory.record, 0)
    assert result is memory.item and result.alive and result.references == 2
    assert memory.moves > 0
    assert not memory.handles and memory.depth == 0 and memory.error is None
    assert memory.pins == 0
    assert memory.resolve(memory.record).flags & abi.PY_FLAG_GC_PINNED == 0
    assert memory.resolve(memory.klass).flags & abi.PY_FLAG_GC_PINNED == 0
    assert result.flags & abi.PY_FLAG_GC_PINNED == 0


@pytest.mark.parametrize('result_kind', ['default', 'receiver', 'class'])
@pytest.mark.parametrize('prior_pin', [False, True])
def test_missing_field_fallback_restores_existing_and_aliased_pins(result_kind, prior_pin):
    memory = _MissingFieldMemory(result=result_kind, prior_pin=prior_pin)
    result = memory.namespace['py_instance_get_field'](memory.record, 0)
    assert result.alive
    for value in (memory.resolve(memory.record), memory.resolve(memory.klass), memory.item):
        assert value.flags & abi.PY_FLAG_GC_PINNED == memory.expected_pin
    assert not memory.handles and memory.depth == 0
    assert memory.pins == 0


@pytest.mark.parametrize('result_kind', ['missing', 'error'])
@pytest.mark.parametrize('cache', [False, True])
def test_missing_field_fallback_preserves_error_and_field_cache(result_kind, cache):
    memory = _MissingFieldMemory(result=result_kind, cache=cache)
    assert memory.namespace['py_instance_get_field'](memory.record, 0) is None
    assert memory.error == ((6, 'missing value') if result_kind == 'missing' else (4, 'lookup failed'))
    assert 3 not in memory.cached_kinds, 'declared empty field must not become an absent-name cache entry'
    assert not memory.handles and memory.depth == 0


@pytest.mark.parametrize('failed_root', [1, 2, 3])
def test_missing_field_root_failure_balances_registration_and_pin_state(failed_root):
    memory = _MissingFieldMemory(fail_register=failed_root)
    assert memory.namespace['py_instance_get_field'](memory.record, 0) is None
    assert memory.error == (7, 'instance field lookup root registration failed')
    assert not memory.handles and memory.depth == 0
    assert memory.resolve(memory.record).flags & abi.PY_FLAG_GC_PINNED == 0
    assert memory.resolve(memory.klass).flags & abi.PY_FLAG_GC_PINNED == 0


@pytest.mark.parametrize('index', [-1, 1])
def test_missing_field_fallback_keeps_invalid_index_low_level_contract(index):
    memory = _MissingFieldMemory()
    assert memory.namespace['py_instance_get_field'](memory.record, index) is None
    assert memory.registrations == 0 and memory.error is None


@pytest.mark.parametrize('backend', [0, 1, 2])
def test_missing_field_nonmoving_lookup_preserves_reference_and_pin_contract(backend):
    memory = _MissingFieldMemory(backend=backend)
    result = memory.namespace['py_instance_get_field'](memory.record, 0)
    assert result is memory.item and result.references == 2
    assert not memory.moves and not memory.handles
    assert memory.record.flags & abi.PY_FLAG_GC_PINNED == 0
    assert memory.klass.flags & abi.PY_FLAG_GC_PINNED == 0


def test_empty_field_callable_default_does_not_hide_later_instance_store():
    memory = _MissingFieldMemory()
    memory.item.tag = abi.PY_TYPE_FUNC
    def decrement(value):
        assert value.alive
        value.references -= 1
    def bind_method(value, inst, name):
        memory.incref(value)
        return value
    memory.namespace.update({'py_decref': decrement, 'py_instance_bind_method': bind_method})
    result = memory.namespace['py_instance_get_field'](memory.record, 0)
    assert result is memory.item and result.references == 2
    assert 2 not in memory.cached_kinds, 'empty field callable must not be cached as a method'
    replacement = memory.make(abi.PY_TYPE_STR)
    memory.resolve(memory.record).fields[abi.PYINSTANCEOBJECT_FIELDS_OFFSET] = replacement
    assert memory.namespace['py_instance_get_field'](memory.resolve(memory.record), 0) is replacement


def _install_missing_field_dynamic_dict(memory, *, found=False, fail=''):
    dynamic = memory.make(abi.PY_TYPE_DICT)
    offset = abi.PYINSTANCEOBJECT_FIELDS_OFFSET + abi.C_POINTER_SIZE
    memory.record.fields[offset] = dynamic
    memory.keys = []

    def new_key(name, length):
        memory.gc('dict_key')
        if fail == 'key':
            return None
        key = memory.make(abi.PY_TYPE_STR)
        memory.keys.append(key)
        return key

    def get_item(dyn, key):
        memory.gc('dict_get')
        assert dyn.alive and key.alive
        if fail == 'dict':
            memory.error = (4, 'dict lookup failed')
            return None
        if found:
            result = memory.resolve(dynamic)
            memory.incref(result)
            return result
        return None

    memory.namespace.update({
        '_dynamic_attr_slot': lambda inst: memory.add(inst, offset),
        'py_str_new': new_key,
        'py_dict_get': get_item,
    })
    return dynamic


@pytest.mark.parametrize('backend', [3, 4])
@pytest.mark.parametrize('cache', [False, True])
@pytest.mark.parametrize('phase', ['dict_key', 'dict_get', 'decref'])
@pytest.mark.parametrize('found', [False, True])
def test_missing_field_dynamic_lookup_keeps_all_live_temporaries(backend, cache, phase, found):
    memory = _MissingFieldMemory(backend=backend, phase=phase, cache=cache)
    dynamic = _install_missing_field_dynamic_dict(memory, found=found)
    result = memory.namespace['py_instance_get_field'](memory.record, 0)
    assert result is (memory.resolve(dynamic) if found else memory.item)
    assert result.alive and result.references == 2
    assert memory.item.references == (1 if found else 2)
    assert memory.resolve(dynamic).references == (2 if found else 1)
    assert all(memory.resolve(key).references == 0 for key in memory.keys)
    assert memory.moves > 0
    assert not memory.handles and memory.depth == memory.pins == 0
    assert memory.error is None
    assert result.flags & abi.PY_FLAG_GC_PINNED == 0


@pytest.mark.parametrize('cache', [False, True])
@pytest.mark.parametrize('data_descriptor', [False, True])
@pytest.mark.parametrize('alias', [False, True])
@pytest.mark.parametrize('phase', ['descriptor', 'decref'])
def test_missing_field_descriptor_result_survives_temporary_cleanup(cache, data_descriptor, alias, phase):
    memory = _MissingFieldMemory(cache=cache, phase=phase)
    answer = memory.item if alias else memory.make(abi.PY_TYPE_STR)

    def describe(descriptor, inst, cls):
        memory.gc('descriptor')
        assert descriptor.alive and inst.alive and cls.alive
        value = memory.resolve(answer)
        memory.incref(value)
        return value

    memory.namespace.update({
        '_descriptor_is_data': lambda value: data_descriptor,
        '_descriptor_call_get': describe,
        'ptr_to_int': lambda value: value,
    })
    result = memory.namespace['py_instance_get_field'](memory.record, 0)
    assert result is memory.resolve(answer) and result.alive and result.references == 2
    assert memory.item.references == (2 if alias else 1)
    assert not memory.handles and memory.depth == memory.pins == 0
    assert result.flags & abi.PY_FLAG_GC_PINNED == 0
    assert memory.error is None and memory.moves > 0


@pytest.mark.parametrize('cache', [False, True])
@pytest.mark.parametrize('failure', ['class', 'key', 'dict', 'method'])
def test_missing_field_pending_error_stops_later_lookup_paths(cache, failure):
    memory = _MissingFieldMemory(cache=cache, result='error' if failure == 'class' else 'missing')
    calls = []
    if failure in ('key', 'dict'):
        _install_missing_field_dynamic_dict(memory, fail=failure)

    def method_lookup(cls, name):
        calls.append(name)
        if failure == 'method':
            memory.error = (4, 'method lookup failed')
            return None
        return memory.item

    memory.namespace['_class_lookup_in_mro'] = method_lookup
    memory.namespace['py_instance_bind_method'] = lambda *args: pytest.fail('bound after producer error')
    assert memory.namespace['py_instance_get_field'](memory.record, 0) is None
    expected = {'class': (4, 'lookup failed'), 'key': (7, 'instance attribute key allocation failed'),
                'dict': (4, 'dict lookup failed'), 'method': (4, 'method lookup failed')}
    assert memory.error == expected[failure]
    assert calls == (['value'] if failure == 'method' else [])
    assert not memory.handles and memory.depth == memory.pins == 0


@pytest.mark.parametrize('phase', ['callback_key', 'callback_tuple', 'callback_store', 'callback_call', 'decref'])
@pytest.mark.parametrize('raw_method', [False, True])
@pytest.mark.parametrize('method_name', ['__getattr__', '__getattribute__'])
def test_missing_field_getattr_keeps_method_key_and_arguments_current(phase, raw_method, method_name):
    memory = _MissingFieldMemory(phase=phase, result='missing')
    method = object() if raw_method else memory.make(abi.PY_TYPE_FUNC)
    memory.arguments = None
    memory.key = None
    memory.namespace['_class_lookup_in_mro'] = lambda cls, name: method if name == method_name else None
    if method_name == '__getattribute__':
        memory.namespace['_no_getattribute_known'] = lambda cls: 0
        memory.namespace['py_obj_getattr'] = memory.namespace['py_instance_getattr']

    def new_key(name, length):
        memory.gc('callback_key')
        memory.key = memory.make(abi.PY_TYPE_STR)
        return memory.key

    def new_tuple(size):
        memory.gc('callback_tuple')
        memory.arguments = memory.make(abi.PY_TYPE_TUPLE)
        return memory.arguments

    def set_item(args, index, value):
        memory.gc('callback_store')
        assert args.alive and value.alive
        memory.incref(value)
        args.fields[abi.PYTUPLEOBJECT_ITEMS_OFFSET + index * abi.C_POINTER_SIZE] = value

    def call(method_arg, args, kwargs):
        memory.gc('callback_call')
        assert method_arg.alive and args.alive
        assert memory.key.alive
        memory.incref(memory.item)
        return memory.item

    def raw_call(method_arg, inst, key):
        memory.gc('callback_call')
        assert method_arg is method and inst.alive and key.alive
        memory.incref(memory.item)
        return memory.item

    memory.namespace.update({'py_str_new': new_key, 'py_tuple_new': new_tuple,
                             'py_tuple_set_item': set_item, 'py_obj_call': call,
                             'call_ptr2': raw_call})
    result = memory.namespace['py_instance_get_field'](memory.record, 0)
    assert result is memory.item and result.alive and result.references == 2
    if not raw_method:
        assert memory.resolve(method).references == 1
    assert memory.resolve(memory.key).references == 0
    assert not memory.handles and memory.depth == memory.pins == 0
    assert memory.error is None


@pytest.mark.parametrize('failed_root', range(7, 15))
def test_missing_field_temporary_root_failure_balances_all_leases(failed_root):
    # The indexed wrapper and semantic default wrapper have three roots each.
    memory = _MissingFieldMemory(fail_register=failed_root)
    assert memory.namespace['py_instance_get_field'](memory.record, 0) is None
    assert memory.error == (7, 'instance field temporary root registration failed')
    assert not memory.handles and memory.depth == memory.pins == 0
    assert memory.item.references == 1


@pytest.mark.parametrize('cache', [False, True])
@pytest.mark.parametrize('property_descriptor', [False, True])
@pytest.mark.parametrize('phase', ['callback_tuple', 'callback_store', 'callback_call', 'decref'])
def test_missing_field_real_descriptor_callback_roots_internal_owners(cache, property_descriptor, phase):
    memory = _MissingFieldMemory(cache=cache, phase=phase)
    memory.item.tag = abi.PY_TYPE_PROPERTY if property_descriptor else abi.PY_TYPE_USER_CLASS_START + 1
    method = memory.make(abi.PY_TYPE_FUNC)
    answer = memory.make(abi.PY_TYPE_STR)
    memory.item.fields[abi.PYPROPERTYOBJECT_FGET_OFFSET] = method
    arguments = []

    def new_tuple(size):
        memory.gc('callback_tuple')
        value = memory.make(abi.PY_TYPE_TUPLE)
        arguments.append(value)
        return value

    def set_item(args, index, value):
        memory.gc('callback_store')
        assert args.alive and value.alive
        memory.incref(value)
        args.fields[abi.PYTUPLEOBJECT_ITEMS_OFFSET + index * abi.C_POINTER_SIZE] = value

    def call(function, args, kwargs):
        memory.gc('callback_call')
        assert function.alive and args.alive
        value = memory.resolve(answer)
        memory.incref(value)
        return value

    memory.namespace.update({
        '_instance_lookup_descriptor': memory.actual_descriptor,
        '_descriptor_is_data': lambda value: property_descriptor,
        '_descriptor_method': lambda value, name: memory.resolve(method),
        'ptr_to_int': lambda value: value,
        'global_load_ptr': lambda name: None,
        'py_tuple_new': new_tuple, 'py_tuple_set_item': set_item, 'py_obj_call': call,
    })
    result = memory.namespace['py_instance_get_field'](memory.record, 0)
    assert result is memory.resolve(answer) and result.references == 2 and result.alive
    assert memory.item.references == 1 and memory.resolve(method).references == 1
    assert memory.resolve(memory.record).references == 1 and memory.resolve(memory.klass).references == 1
    assert all(memory.resolve(value).references == 0 for value in arguments)
    assert memory.error is None and memory.moves > 0
    assert not memory.handles and memory.depth == memory.pins == 0


@pytest.mark.parametrize('populated', [False, True])
def test_setter_only_descriptor_preserves_lookup_owner_when_get_is_absent(populated):
    memory = _MissingFieldMemory(backend=0)
    descriptor = memory.make(abi.PY_TYPE_USER_CLASS_START + 1)
    if populated:
        memory.record.fields[abi.PYINSTANCEOBJECT_FIELDS_OFFSET] = memory.item

    def class_attribute(cls, name):
        memory.incref(descriptor)
        return descriptor

    memory.namespace.update({
        '_class_attr_lookup_in_mro': class_attribute,
        '_descriptor_is_data': lambda value: True,
        '_descriptor_call_get': lambda value, inst, cls: None,
        'ptr_to_int': lambda value: value,
    })
    result = memory.namespace['_instance_getattr_default'](memory.record, memory.klass, 'value')
    assert result is (memory.item if populated else descriptor)
    assert descriptor.references == (1 if populated else 2)
    assert result.references == 2 and result.alive
    assert not memory.handles and memory.depth == memory.pins == 0


@pytest.mark.parametrize('phase', ['callback_tuple', 'callback_store', 'callback_call', 'decref'])
@pytest.mark.parametrize('aliased_method', [False, True])
def test_missing_field_custom_getattribute_fallback_preserves_alias_leases(phase, aliased_method):
    memory = _MissingFieldMemory(phase=phase)
    method = memory.make(abi.PY_TYPE_FUNC)
    fallback = method if aliased_method else memory.make(abi.PY_TYPE_FUNC)
    exception = memory.make(abi.PY_TYPE_EXC)
    calls = []
    memory.namespace.update({
        '_no_getattribute_known': lambda cls: 0,
        'py_obj_getattr': memory.namespace['py_instance_getattr'],
        '_class_lookup_in_mro': lambda cls, name: memory.resolve(method if name == '__getattribute__' else fallback),
        'py_str_new': lambda name, length: memory.make(abi.PY_TYPE_STR),
        'py_exc_builtin_class': lambda tag: 6,
        'py_exc_matches': lambda error, cls: 1,
    })

    def clear_error():
        old, memory.error = memory.error, None
        memory.decref(old)

    def new_tuple(size):
        memory.gc('callback_tuple')
        return memory.make(abi.PY_TYPE_TUPLE)

    def set_item(args, index, value):
        memory.gc('callback_store')
        assert args.alive and value.alive
        memory.incref(value)
        args.fields[abi.PYTUPLEOBJECT_ITEMS_OFFSET + index * abi.C_POINTER_SIZE] = value

    def call(function, args, kwargs):
        memory.gc('callback_call')
        assert function.alive and args.alive
        calls.append(function)
        if len(calls) == 1:
            memory.error = memory.resolve(exception)
            return None
        memory.incref(memory.item)
        return memory.item

    memory.namespace.update({'py_clear_exception': clear_error, 'py_tuple_new': new_tuple,
                             'py_tuple_set_item': set_item, 'py_obj_call': call})
    result = memory.namespace['py_instance_get_field'](memory.record, 0)
    assert result is memory.item and result.alive and result.references == 2
    assert len(calls) == 2 and memory.error is None
    assert memory.resolve(method).references == memory.resolve(fallback).references == 1
    assert memory.resolve(exception).references == 0
    assert memory.resolve(method).flags & abi.PY_FLAG_GC_PINNED == 0
    assert not memory.handles and memory.depth == memory.pins == 0


@pytest.mark.parametrize('rooted', [False, True])
@pytest.mark.parametrize('raises', [False, True])
def test_classmethod_instance_descriptor_uses_actual_class_owner(rooted, raises):
    memory = _MissingFieldMemory(backend=0)
    descriptor = memory.make(abi.PY_TYPE_CLASSMETHOD)
    bound = memory.make(abi.PY_TYPE_FUNC)
    calls = []

    def bind(value, owner):
        calls.append((value, owner))
        assert value is descriptor and owner is memory.klass
        if raises:
            memory.error = (4, 'binding failed')
            return None
        return bound

    memory.namespace['_classmethod_bind'] = bind
    tree = ast.parse((ROOT / 'pcc/runtime/py/py_class.py').read_text())
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_descriptor_call_get')
    exec(compile(ast.Module(body=[function], type_ignores=[]), '<descriptor get>', 'exec'), memory.namespace)
    slots = _Slots(64) if rooted else None
    result = memory.actual_descriptor(descriptor, memory.record, memory.klass, slots, _Slots(64))
    assert calls == [(descriptor, memory.klass)]
    assert result is (None if raises else bound)
    assert memory.error == ((4, 'binding failed') if raises else None)


@pytest.mark.parametrize('populated', [False, True])
def test_classmethod_instance_field_override_precedes_binding(populated):
    memory = _MissingFieldMemory(backend=0)
    descriptor = memory.make(abi.PY_TYPE_CLASSMETHOD)
    bound = memory.make(abi.PY_TYPE_FUNC)
    calls = []
    if populated:
        memory.record.fields[abi.PYINSTANCEOBJECT_FIELDS_OFFSET] = memory.item

    def class_attribute(cls, name):
        memory.incref(descriptor)
        return descriptor

    def bind(value, owner):
        calls.append((value, owner))
        assert value is descriptor and owner is memory.klass
        memory.incref(bound)
        return bound

    memory.namespace.update({
        '_class_attr_lookup_in_mro': class_attribute,
        '_instance_lookup_descriptor': memory.actual_descriptor,
        '_descriptor_call_get': memory.namespace['_descriptor_call_get'],
        '_descriptor_is_data': lambda value: False,
        '_classmethod_bind': bind,
        'ptr_to_int': lambda value: value,
    })
    result = memory.namespace['_instance_getattr_default'](memory.record, memory.klass, 'value')
    assert result is (memory.item if populated else bound)
    assert len(calls) == (0 if populated else 1)
    assert descriptor.references == 1
    assert not memory.handles and memory.depth == memory.pins == 0


class _PropertyAccessorMemory(_GetterMemory):
    """Lease-aware field owner with relocation at each real cleanup boundary."""
    def __init__(self, phase, failure):
        self.leases, self.frames = {}, {}
        self.failure, self.copies = failure, 0
        super().__init__(backend=4, phase=phase)
        self.none_object = self.make(abi.PY_TYPE_NONE)
        self.none_slot = _Slots(8)
        self.none_slot.fields[0] = self.none_object
        self.payloads.append(self.none_slot)
        self.namespace.update({
            'global_addr': lambda name: self.none_slot if name == 'py_None' else name,
            'memset': lambda base, value, size: [self.write(base, i, None) for i in range(0, size, 8)],
            'store_i64': self.write,
            'pcc_gc_frame_enter': self.enter, 'pcc_gc_frame_leave': self.leave,
            'pcc_gc_root_copy_borrowed_lease': self.copy,
            'pcc_gc_root_copy_lease': self.copy,
            'pcc_gc_foreign_lease_release': self.release,
            'pcc_gc_store_root': self.drop,
            'py_tls_exc_swap_slot': self.swap,
            'py_clear_exception': lambda: setattr(self, 'error', None),
            'py_err_occurred': lambda: self.error is not None,
            'pcc_platform_abort': lambda: pytest.fail('property accessor invariant failed'),
            '_cstr_is_dunder_class': lambda name: False,
            'pcc_diagnostics_runtime_log_event_code': lambda *args: None,
            'strcmp': lambda left, right: int(left != right),
        })
        tree = ast.parse((ROOT / 'pcc/runtime/py/py_obj_ops_dispatch.py').read_text())
        names = {'py_obj_getattr', '_property_accessor_get', '_property_accessor_clear'}
        functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
        self.namespace.update({node.targets[0].id: ast.literal_eval(node.value)
            for node in tree.body if isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id.startswith('_PROPERTY_ACCESSOR_')})
        exec(compile(ast.Module(body=functions, type_ignores=[]), '<property accessor>', 'exec'), self.namespace)

    def gc(self, phase):
        # Counted address leases are independent of legacy header pins.
        prior = {value: value.flags for value in self.leases.values()}
        for value in prior:
            value.flags |= abi.PY_FLAG_GC_PINNED
        super().gc(phase)
        for value, flags in prior.items():
            value.flags = flags

    def enter(self, name, slots):
        count = 1 if 'borrowed' in name else 3
        self.frames[slots] = [self.register((slots, index * 8)) for index in range(count)]
        self.gc('frame_enter')

    def leave(self, slots):
        for handle in self.frames.pop(slots):
            self.unregister(handle)

    def copy(self, destination, source):
        self.copies += 1
        self.gc('copy')
        if self.copies == self.failure:
            return -1
        value = self.read(source, 0)
        assert value.alive and self.read(destination, 0) is None
        if self.copies == 2 and self.pointer(source)[0] is not self.none_slot:
            assert self.pointer(source)[0] in self.leases.values()
        self.incref(value)
        self.write(destination, 0, value)
        self.leases[self.pointer(destination)] = value
        self.gc('retain_finish')
        return 1

    def release(self, slot, token):
        assert token == 1
        assert self.leases.pop(self.pointer(slot)) is self.read(slot, 0)
        self.gc('release')
        return 0

    def drop(self, slot, value):
        assert value is None and self.pointer(slot) not in self.leases
        old = self.read(slot, 0)
        self.write(slot, 0, None)
        if isinstance(old, _Object):
            old.references -= 1
        self.gc('decref')
        self.error = (4, 'cleanup error')

    def swap(self, slot):
        self.gc('tls_swap')
        old = self.read(slot, 0)
        self.write(slot, 0, self.error)
        self.error = old


@pytest.mark.parametrize('field', ['fget', 'fset', 'fdel'])
@pytest.mark.parametrize('missing', [False, True])
@pytest.mark.parametrize('phase', ['frame_enter', 'copy', 'retain_finish', 'decref', 'release', 'unregister', 'tls_swap'])
@pytest.mark.parametrize('failure', [0, 1, 2])
def test_property_accessor_attribute_returns_exact_retained_field(field, missing, phase, failure):
    memory = _PropertyAccessorMemory(phase, failure)
    descriptor = memory.make(abi.PY_TYPE_PROPERTY)
    callable_value = memory.make(abi.PY_TYPE_FUNC)
    offset = getattr(abi, 'PYPROPERTYOBJECT_' + field.upper() + '_OFFSET')
    descriptor.fields[offset] = None if missing else callable_value
    result = memory.namespace['py_obj_getattr'](descriptor, field)
    if failure:
        assert result is None
        assert memory.error == (7, 'property accessor owner lease failed')
        assert memory.resolve(callable_value).references == 1
    else:
        assert result is memory.resolve(memory.none_object if missing else callable_value)
        assert result.references == 2 and result.alive
        assert memory.error is None
    assert memory.resolve(descriptor).references == 1
    assert not memory.frames and not memory.handles and not memory.leases
    assert memory.pins == memory.depth == 0
    if failure != 1 or phase not in ('retain_finish', 'release'):
        assert memory.moves > 0
