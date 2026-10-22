"""Execute percent-format runtime bodies with relocation and ownership checks."""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest


_ABI_SOURCE = Path(__file__).parents[2] / 'pcc/runtime/py/py_abi_constants.py'
ABI = {
    node.targets[0].id: ast.literal_eval(node.value)
    for node in ast.parse(_ABI_SOURCE.read_text()).body
    if isinstance(node, ast.Assign) and len(node.targets) == 1
    and isinstance(node.targets[0], ast.Name)
    and isinstance(node.value, ast.Constant)
}
assert ABI['PYTUPLEOBJECT_ITEMS_OFFSET'] == 24


@dataclass(frozen=True)
class Handle:
    identity: int
    generation: int


@dataclass(frozen=True)
class Payload:
    value: object
    offset: int = 0


@dataclass(frozen=True)
class Field:
    owner: Handle
    offset: int


class PercentMemory:
    def __init__(self, source, relocate=True, failure=None, alias=False, lease_failure=None, copy_failure=None, custom_tag=ABI['PY_TYPE_BYTES']):
        self.source = source
        self.relocate = relocate
        self.failure = failure
        self.alias = alias
        self.lease_failure = lease_failure
        self.lease_failed = False
        self.copy_failure = copy_failure
        self.copy_count = 0
        self.custom_tag = custom_tag
        self.memory = {}
        self.objects = {}
        self.frames = {}
        self.leases = {}
        self.pending_new = None
        self.pending = None
        self.address = 10000
        self.serial = 0
        self.events = []
        self.disposed = []
        self.callback_count = 0
        self.env = self.environment()
        self.install()

    def object(self, handle):
        assert isinstance(handle, Handle), handle
        obj = self.objects[handle.identity]
        assert obj['generation'] == handle.generation, ('stale address', handle, obj)
        assert obj['refs'] > 0, ('dead address', handle)
        return obj

    def current(self, identity):
        return Handle(identity, self.objects[identity]['generation'])

    def new(self, tag, data, role='input', children=(), pending=False):
        if pending and self.failure == role:
            self.pending = role + '-error'
            return None
        self.serial += 1
        obj = dict(tag=tag, data=data, role=role, refs=1, generation=0,
                   children=[child.identity for child in children], pinned=False)
        self.objects[self.serial] = obj
        for child in children:
            self.object(child)['refs'] += 1
        result = Handle(self.serial, 0)
        if pending:
            assert self.pending_new is None
            self.pending_new = result.identity
        return result

    def alloc(self, size):
        self.address += size + 100
        return self.address

    def store(self, base, offset, value):
        assert isinstance(base, int), base
        slot = base + offset
        self.memory[slot] = value
        if isinstance(value, Handle) and self.pending_new == value.identity:
            assert any(count > 0 and frame <= slot < frame + count * 8
                       for frame, count in self.frames.items()), ('NEW not stored in owner', slot)
            self.pending_new = None

    def load(self, base, offset=0):
        if isinstance(base, Field):
            obj = self.object(base.owner)
            index = (base.offset + offset - 24) // 8
            return self.current(obj['children'][index])
        return self.memory.get(base + offset)

    def park(self):
        assert self.pending_new is None, ('unpublished NEW crossed a call', self.pending_new)
        if not self.relocate:
            return
        protected = {self.load(slot).identity for slot in self.leases
                     if isinstance(self.load(slot), Handle)}
        protected.update(i for i, obj in self.objects.items() if obj['pinned'])
        # Move every live unleased object, including tuple/dict children.
        # Traced heap references are logical IDs; raw local handles become
        # stale and must never be reused across this simulated parking point.
        moved = {identity for identity, obj in self.objects.items()
                 if self.frames and obj['refs'] > 0 and identity not in protected}
        for identity in moved:
            self.objects[identity]['generation'] += 1
        for frame, count in self.frames.items():
            for offset in range(abs(count)):
                slot = frame + offset * 8
                value = self.memory.get(slot)
                if isinstance(value, Handle) and value.identity in moved:
                    self.memory[slot] = self.current(value.identity)

    def acquire(self, slot):
        self.park()
        value = self.load(slot)
        if value is None:
            return 0
        obj = self.object(value)
        if obj['role'] == self.lease_failure and not self.lease_failed:
            self.lease_failed = True
            self.pending = 'lease-error'
            return -1
        assert slot not in self.leases
        self.leases[slot] = 1
        return 1

    def release(self, slot, token):
        self.park()
        if token:
            assert self.leases.pop(slot) == token
        return 0

    def copy(self, destination, source):
        self.park()
        self.copy_count += 1
        if self.copy_count == self.copy_failure:
            self.pending = 'copy-error'
            return -1
        value = self.load(source)
        assert self.load(destination) is None
        if value is None:
            return 0
        self.object(value)['refs'] += 1
        self.store(destination, 0, value)
        return self.acquire(destination)

    def decref(self, value):
        if value is None:
            return
        obj = self.object(value)
        obj['refs'] -= 1
        if obj['refs'] == 0:
            assert not any(isinstance(self.load(slot), Handle)
                           and self.load(slot).identity == value.identity for slot in self.leases)
            self.disposed.append(obj['role'])
            if obj['role'] in ('method', 'args', 'converted'):
                self.pending = 'disposal-error'
            for identity in obj['children']:
                self.decref(self.current(identity))

    def drop(self, slot, value):
        self.park()
        assert value is None
        old = self.load(slot)
        self.store(slot, 0, None)
        self.decref(old)

    def ptr(self, base, offset):
        if isinstance(base, int):
            return base + offset
        if isinstance(base, Payload):
            return Payload(base.value, base.offset + offset)
        obj = self.object(base)
        if obj['tag'] == ABI['PY_TYPE_TUPLE']:
            return Field(base, offset)
        return Payload(base, offset - 24)

    def payload(self, pointer, count=None):
        assert isinstance(pointer, Payload), pointer
        if isinstance(pointer.value, Handle):
            data = self.object(pointer.value)['data']
        else:
            data = pointer.value
        if isinstance(data, str):
            data = data.encode()
        end = None if count is None else pointer.offset + count
        return bytes(data[pointer.offset:end])

    def header(self, value, offset):
        if isinstance(value, Handle):
            obj = self.object(value)
            if offset == 8:
                return obj['tag']
            if offset == 16:
                return len(obj['children']) if obj['tag'] == ABI['PY_TYPE_TUPLE'] else len(obj['data']) if isinstance(obj['data'], (str, bytes, bytearray)) else obj['data']
            raise AssertionError(offset)
        return self.load(value, offset) or 0

    def buffer(self, capacity):
        self.park()
        result = self.alloc(24)
        self.memory[result] = Payload(bytearray())
        self.memory[result + 8] = 0
        return result

    def append(self, state, payload, count):
        self.park()
        data = self.payload(payload, count)
        target = self.load(state).value
        target.extend(data)
        self.memory[state + 8] = len(target)
        return 0

    def rendered(self, value, mode):
        self.park()
        obj = self.object(value)
        if self.failure == 'rendered':
            self.pending = 'rendered-error'
            return None
        data = obj['data'] if obj['tag'] == ABI['PY_TYPE_STR'] else 'TEXT'
        return self.new(ABI['PY_TYPE_STR'], data, 'rendered', pending=True)

    def bytes_copy(self, value):
        self.park()
        obj = self.object(value)
        if obj['tag'] == ABI['PY_TYPE_BYTES']:
            obj['refs'] += 1
            self.pending_new = value.identity
            return value
        self.events.append(('bytes-copy', obj['tag']))
        data = self.object(self.current(obj['children'][0]))['data'] if obj['tag'] == ABI['PY_TYPE_MEMORYVIEW'] and obj['children'] else obj['data']
        return self.new(ABI['PY_TYPE_BYTES'], bytes(data), 'copied-bytes', pending=True)

    def environment(self):
        def enter(count, base):
            self.park()
            self.frames[base] = count
        def leave(base):
            self.park()
            del self.frames[base]
        def memset(base, value, size):
            for offset in range(0, size, 8):
                self.memory[base + offset] = None
        def swap(slot):
            self.park()
            self.pending, self.memory[slot] = self.load(slot), self.pending
        def pin(slot):
            self.park()
            value = self.load(slot)
            if value is None:
                return 0
            obj = self.object(value)
            prior = int(obj['pinned'])
            obj['pinned'] = True
            return prior
        def take(slot, prior):
            value = self.load(slot)
            if value is not None:
                self.object(value)['pinned'] = bool(prior)
                self.pending_new = value.identity
            self.memory[slot] = None
            return value
        def getattr_(value, attribute):
            self.park()
            return self.new(ABI['PY_TYPE_FUNC'], 'method', 'method', (value,), pending=True)
        def call(method, args, kwargs):
            self.park()
            self.object(method); self.object(args)
            self.callback_count += 1
            if self.failure == 'converted':
                self.pending = 'converted-error'
                return None
            if self.alias:
                self.object(method)['refs'] += 1
                self.pending_new = method.identity
                return method
            if self.custom_tag == ABI['PY_TYPE_MEMORYVIEW']:
                base = self.new(ABI['PY_TYPE_BYTES'], b'BYTES', 'view-base')
                result = self.new(ABI['PY_TYPE_MEMORYVIEW'], None, 'converted', (base,), pending=True)
                self.decref(base)
                return result
            return self.new(self.custom_tag, bytearray(b'BYTES') if self.custom_tag == ABI['PY_TYPE_BYTEARRAY'] else b'BYTES', 'converted', pending=True)
        def converted_bytearray(value):
            self.park()
            return self.new(ABI['PY_TYPE_BYTEARRAY'], bytearray(self.object(value)['data']), 'bytearray', pending=True)
        def type_error(*args):
            self.park(); self.pending = 'type-error'; return -1
        def output_text(output, payload, length, flags, width, precision, count_chars):
            self.park()
            data = self.payload(payload, length)
            if precision >= 0:
                data = data[:precision]
            return self.append(output, Payload(data), len(data))
        def dict_get(dict_slot, key_slot, default_slot, result_slot):
            self.park()
            mapping = self.object(self.load(dict_slot))['data']
            key = self.object(self.load(key_slot))['data']
            if isinstance(key, bytes): key = key.decode()
            identity = mapping.get(key)
            value = self.current(identity) if identity is not None else self.load(default_slot)
            if value is not None: self.object(value)['refs'] += 1
            self.store(result_slot, 0, value)
            return 0
        def int_text(value, base, meta):
            self.park()
            number = self.object(value)['data']
            text = str(abs(int(number))).encode()
            self.store(meta, 0, int(number < 0)); self.store(meta, 8, 0); self.store(meta, 16, len(text))
            return Payload(text)
        def float_to_int(value):
            self.park(); return self.new(ABI['PY_TYPE_INT'], int(value), 'integer', pending=True)
        def from_buffer(state, tag):
            self.park(); return self.new(tag, bytes(self.load(state).value), 'result', pending=True)
        def byte(pointer, offset):
            if isinstance(pointer, int): return self.memory.get(pointer + offset, 0)
            return self.payload(self.ptr(pointer, offset), 1)[0]
        def type_name(value):
            self.park(); self.object(value); return self.new(ABI['PY_TYPE_STR'], 'typename', 'type-name', pending=True)
        return dict(c_ptr=object, C_POINTER_SIZE=8, PYTUPLEOBJECT_ITEMS_OFFSET=24,
            PY_TYPE_INT=ABI['PY_TYPE_INT'], PY_TYPE_BOOL=ABI['PY_TYPE_BOOL'], PY_TYPE_FLOAT=ABI['PY_TYPE_FLOAT'], PY_TYPE_STR=ABI['PY_TYPE_STR'],
            PY_TYPE_TUPLE=ABI['PY_TYPE_TUPLE'], PY_TYPE_BYTES=ABI['PY_TYPE_BYTES'], PY_TYPE_BYTEARRAY=ABI['PY_TYPE_BYTEARRAY'],
            PY_TYPE_DICT=ABI['PY_TYPE_DICT'], PY_TYPE_INSTANCE=ABI['PY_TYPE_INSTANCE'], PY_TYPE_MEMORYVIEW=ABI['PY_TYPE_MEMORYVIEW'],
            PY_TYPE_USER_CLASS_START=ABI['PY_TYPE_USER_CLASS_START'],
            stack_alloc=self.alloc, store_ptr=self.store, load_ptr=self.load,
            store_i64=self.store, load_i64=self.header, load_i32=self.header,
            store_i8=self.store, load_i8=byte, memset=memset,
            ptr_add=self.ptr, ptr_is_null=lambda value: int(value is None),
            ptr_eq=lambda left, right: left == right, null=lambda: None,
            cstr=lambda text: Payload(text.encode()), is_tagged_int=lambda value: False,
            global_addr=lambda name: -2 if 'borrowed' in name else 8,
            global_load_ptr=lambda name: None,
            pcc_gc_frame_enter=enter, pcc_gc_frame_leave=leave,
            pcc_gc_root_copy_borrowed_lease=self.copy, pcc_gc_root_copy_lease=self.copy,
            pcc_gc_foreign_lease_acquire=self.acquire, pcc_gc_foreign_lease_release=self.release,
            pcc_gc_store_root=self.drop, py_tls_exc_swap_slot=swap,
            pcc_py_gc_minor_graph_lock=lambda: self.park(),
            pcc_py_gc_minor_graph_unlock=lambda: None,
            pcc_gc_note_slot_write_barrier=lambda *args: None,
            pcc_gc_take_pinned_slot=take, _unicode_format_pin=pin,
            pcc_platform_abort=lambda: pytest.fail('owner protocol abort'),
            py_clear_exception=lambda: setattr(self, 'pending', None),
            py_err_occurred=lambda: int(self.pending is not None),
            py_runtime_error_if_unset=lambda *args: setattr(self, 'pending', self.pending or 'owner-error'),
            py_obj_getattr=getattr_, py_obj_call=call,
            py_tuple_new=lambda count: (self.park(), self.new(ABI['PY_TYPE_TUPLE'], None, 'args', pending=True))[1],
            py_tuple_len=lambda value: len(self.object(value)['children']),
            py_dict_get_default_slots=dict_get,
            py_obj_str=lambda value: self.rendered(value, 'str'),
            py_obj_repr=lambda value: self.rendered(value, 'repr'),
            py_obj_ascii=lambda value: self.rendered(value, 'ascii'),
            py_obj_type_name=type_name,
            py_str_utf8=lambda value: Payload(value),
            py_str_byte_len=lambda value: len(self.object(value)['data']),
            py_bytes_from_obj=self.bytes_copy, py_bytearray_from_obj=converted_bytearray,
            py_str_new=lambda data, count: (self.park(), self.new(ABI['PY_TYPE_STR'], self.payload(data, count).decode(), 'key', pending=True))[1],
            py_bytes_new=lambda data, count: (self.park(), self.new(ABI['PY_TYPE_BYTES'], self.payload(data, count), 'key', pending=True))[1],
            py_float_to_f64=lambda value: self.object(value)['data'],
            py_int_from_f64_exact=float_to_int,
            py_int_to_i64=lambda value, overflow: self.object(value)['data'],
            py_chr_from_i64=lambda number: (self.park(), self.new(ABI['PY_TYPE_STR'], chr(number), 'character', pending=True))[1],
            _int_digit_text=int_text, _int_to_double=lambda value: float(self.object(value)['data']),
            _buffer_new=self.buffer, _buffer_free=lambda state: self.park(),
            _buffer_string=lambda state: from_buffer(state, ABI['PY_TYPE_STR']),
            _buffer_bytes=lambda state: from_buffer(state, ABI['PY_TYPE_BYTES']),
            _buffer_append=self.append,
            _buffer_char=lambda state, value: self.append(state, Payload(bytes([value])), 1),
            _buffer_repeat=lambda state, value, count: self.append(state, Payload(bytes([value]) * max(0, count)), max(0, count)),
            _buffer_cstr=lambda state, value: self.append(state, value, len(self.payload(value))),
            _buffer_decimal=lambda state, value: self.append(state, Payload(str(value).encode()), len(str(value))),
            _percent_output_text=output_text,
            _percent_output_number=lambda state, data, size, *args: self.append(state, data, size),
            _percent_type_error=type_error, _percent_arg_error=type_error,
            _percent_not_enough=lambda count: setattr(self, 'pending', 'not-enough'),
            _percent_error_at=type_error,
            _raise_buffer_error=lambda state, kind: setattr(self, 'pending', 'format-error'),
            py_exc_new=lambda kind, message: 'error-' + str(kind),
            py_exc_new_with_value=lambda kind, value: 'key-error',
            py_raise_owned=lambda error: setattr(self, 'pending', error),
            free=lambda pointer: self.park(),
            _utf8_char_count=lambda data, count: len(self.payload(data, count).decode()),
            _utf8_prefix_bytes=lambda data, count, limit: len(self.payload(data, count).decode()[:limit].encode()))

    def install(self):
        tree = ast.parse(self.source.read_text())
        names = {'_format_callback_adopt', '_format_callback_drop', '_type_of',
                 '_format_percent', '_format_percent_body', '_parse_digits', '_skip_digits', '_is_digit_byte',
                 '_append_type_name', '_append_type_name_body', '_append_pystr', '_bytes_payload', '_bytes_payload_length',
                 'py_str_mod', 'py_bytes_mod'}
        names.update(node.name for node in tree.body if isinstance(node, ast.FunctionDef)
                     and node.name.startswith('_percent_') and node.name not in self.env)
        statements = []
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
                    and target.id.startswith(('_PERCENT_', '_ARG_', '_PCT_')) for target in node.targets):
                statements.append(node)
            if isinstance(node, ast.FunctionDef) and node.name in names:
                node.decorator_list = []
                statements.append(node)
        exec(compile(ast.Module(body=statements, type_ignores=[]), str(self.source), 'exec'), self.env)
        # Runtime-port function entries may poll. Intrinsics remain direct.
        for name in names:
            function = self.env[name]
            def entry(*args, _function=function):
                self.park()
                return _function(*args)
            self.env[name] = entry

    def finish(self, result=None):
        if result is not None:
            assert self.pending_new == result.identity
            self.pending_new = None
            self.object(result)
        assert self.pending_new is None
        assert self.frames == {} and self.leases == {}
        assert all(not obj['pinned'] for obj in self.objects.values())


@pytest.fixture
def runtime_source():
    return Path(__file__).parents[2] / 'pcc/runtime/py/py_format_runtime.py'


@pytest.mark.parametrize('relocate', (False, True))
@pytest.mark.parametrize('conversion,tag,data,bytes_mode,expected', [
    (115, ABI['PY_TYPE_STR'], 'TEXT', 0, b'TEXT'),
    (115, ABI['PY_TYPE_INSTANCE'], 'value', 0, b'TEXT'),
    (114, ABI['PY_TYPE_INSTANCE'], 'value', 0, b'TEXT'),
    (97, ABI['PY_TYPE_INSTANCE'], 'value', 0, b'TEXT'),
    (98, ABI['PY_TYPE_BYTES'], b'BYTES', 1, b'BYTES'),
    (98, ABI['PY_TYPE_BYTEARRAY'], bytearray(b'BYTES'), 1, b'BYTES'),
    (98, ABI['PY_TYPE_MEMORYVIEW'], b'BYTES', 1, b'BYTES'),
    (98, ABI['PY_TYPE_INSTANCE'], 'value', 1, b'BYTES'),
])
def test_percent_text_actual_bodies_publish_each_conversion(runtime_source, relocate, conversion, tag, data, bytes_mode, expected):
    model = PercentMemory(runtime_source, relocate)
    argument = model.new(tag, data)
    output = model.buffer(32)
    cursor = model.alloc(72)
    status = model.env['_percent_text'](output, argument, Payload(b'%s'), cursor, conversion, 0, 0, -1, bytes_mode)
    model.finish()
    assert status == 0 and bytes(model.load(output).value) == expected
    assert model.objects[argument.identity]['refs'] == 1
    assert model.pending is None
    assert all(obj['refs'] == 0 for identity, obj in model.objects.items() if identity != argument.identity)


@pytest.mark.parametrize('relocate', (False, True))
@pytest.mark.parametrize('failure', ('rendered', 'converted', 'args'))
def test_percent_conversion_error_survives_temporary_disposal(runtime_source, relocate, failure):
    model = PercentMemory(runtime_source, relocate, failure)
    argument = model.new(ABI['PY_TYPE_INSTANCE'], 'value')
    output = model.buffer(32)
    status = model.env['_percent_text'](output, argument, Payload(b'%s'), model.alloc(72), 115 if failure == 'rendered' else 98, 0, 0, -1, 0 if failure == 'rendered' else 1)
    model.finish()
    assert status == -1
    assert model.pending == failure + '-error'
    assert model.objects[argument.identity]['refs'] == 1
    assert all(obj['refs'] == 0 for identity, obj in model.objects.items() if identity != argument.identity)


@pytest.mark.parametrize('relocate', (False, True))
def test_percent_wrong_callback_alias_is_disposed_once(runtime_source, relocate):
    model = PercentMemory(runtime_source, relocate, alias=True)
    argument = model.new(ABI['PY_TYPE_INSTANCE'], 'value')
    status = model.env['_percent_text'](model.buffer(32), argument, Payload(b'%b'), model.alloc(72), 98, 0, 0, -1, 1)
    model.finish()
    assert status == -1 and model.pending == 'type-error'
    assert model.disposed.count('method') == 1
    assert model.objects[argument.identity]['refs'] == 1


@pytest.mark.parametrize('relocate', (False, True))
@pytest.mark.parametrize('format_tag', (ABI['PY_TYPE_STR'], ABI['PY_TYPE_BYTES'], ABI['PY_TYPE_BYTEARRAY']))
def test_percent_entry_parser_and_final_conversion_keep_result_rooted(runtime_source, relocate, format_tag):
    model = PercentMemory(runtime_source, relocate)
    format_obj = model.new(format_tag, '%s' if format_tag == ABI['PY_TYPE_STR'] else b'%s')
    value = model.new(ABI['PY_TYPE_STR'] if format_tag == ABI['PY_TYPE_STR'] else ABI['PY_TYPE_BYTES'], 'TEXT' if format_tag == ABI['PY_TYPE_STR'] else b'TEXT')
    arguments = model.new(ABI['PY_TYPE_TUPLE'], None, children=[value])
    result = model.env['py_str_mod' if format_tag == ABI['PY_TYPE_STR'] else 'py_bytes_mod'](format_obj, arguments)
    model.finish(result)
    data = model.object(result)['data']
    assert (data.encode() if isinstance(data, str) else bytes(data)) == b'TEXT'
    assert model.object(result)['tag'] == format_tag
    assert model.pending is None
    assert model.objects[value.identity]['refs'] == 2
    assert model.objects[arguments.identity]['refs'] == 1
    assert model.objects[format_obj.identity]['refs'] == 1


@pytest.mark.parametrize('relocate', (False, True))
@pytest.mark.parametrize('case', ('mapping', 'missing-key', 'star', 'float-int', 'character', 'byte-character', 'type-name'))
def test_percent_other_conversion_and_argument_owners(runtime_source, relocate, case):
    model = PercentMemory(runtime_source, relocate)
    if case == 'type-name':
        value = model.new(ABI['PY_TYPE_INSTANCE'], 'value')
        output = model.buffer(32)
        model.env['_append_type_name'](output, value)
        model.finish()
        assert bytes(model.load(output).value) == b'typename'
        assert model.objects[value.identity]['refs'] == 1
        assert model.disposed.count('type-name') == 1
        return
    if case in ('mapping', 'missing-key'):
        value = model.new(ABI['PY_TYPE_STR'], 'TEXT')
        arguments = model.new(ABI['PY_TYPE_DICT'], {'key': value.identity}, children=[value])
        pattern = '%(key)s' if case == 'mapping' else '%(missing)s'
        expected = b'TEXT'
    elif case == 'star':
        width = model.new(ABI['PY_TYPE_INT'], 4)
        value = model.new(ABI['PY_TYPE_STR'], 'TEXT')
        arguments = model.new(ABI['PY_TYPE_TUPLE'], None, children=[width, value])
        pattern, expected = '%*s', b'TEXT'
    elif case == 'float-int':
        arguments = model.new(ABI['PY_TYPE_FLOAT'], 12.5)
        pattern, expected = '%d', b'12'
    elif case == 'character':
        arguments = model.new(ABI['PY_TYPE_INT'], 65)
        pattern, expected = '%c', b'A'
    else:
        arguments = model.new(ABI['PY_TYPE_BYTEARRAY'], bytearray(b'A'))
        pattern, expected = b'%c', b'A'
    format_obj = model.new(ABI['PY_TYPE_BYTES'] if case == 'byte-character' else ABI['PY_TYPE_STR'], pattern)
    baseline = {identity: obj['refs'] for identity, obj in model.objects.items()}
    result = model.env['py_bytes_mod' if case == 'byte-character' else 'py_str_mod'](format_obj, arguments)
    model.finish(result)
    assert all(model.objects[identity]['refs'] == refs for identity, refs in baseline.items())
    if case == 'missing-key':
        assert result is None and model.pending == 'key-error'
    else:
        assert result is not None and model.pending is None
        data = model.object(result)['data']
        assert (data.encode() if isinstance(data, str) else bytes(data)) == expected
    assert all(obj['refs'] == (1 if result is not None and identity == result.identity else 0)
               for identity, obj in model.objects.items() if identity not in baseline)


@pytest.mark.parametrize('failure', ('result', 'bytearray'))
def test_percent_final_result_failures_preserve_original_exception(runtime_source, failure):
    model = PercentMemory(runtime_source, True, failure)
    format_obj = model.new(ABI['PY_TYPE_BYTEARRAY'], bytearray(b'%s'))
    value = model.new(ABI['PY_TYPE_BYTES'], b'TEXT')
    result = model.env['py_bytes_mod'](format_obj, value)
    model.finish(result)
    assert result is None and model.pending == failure + '-error'
    assert model.objects[format_obj.identity]['refs'] == 1
    assert model.objects[value.identity]['refs'] == 1
    assert all(obj['refs'] == 0 for identity, obj in model.objects.items()
               if identity not in (format_obj.identity, value.identity))


@pytest.mark.parametrize('role', ('method', 'args', 'converted', 'rendered', 'key', 'result', 'bytearray'))
def test_percent_lease_failure_retires_the_already_published_owner(runtime_source, role):
    model = PercentMemory(runtime_source, True, lease_failure=role)
    if role in ('method', 'args', 'converted', 'rendered'):
        value = model.new(ABI['PY_TYPE_INSTANCE'], 'value')
        output = model.buffer(32)
        status = model.env['_percent_text'](output, value, Payload(b'%s'), model.alloc(72),
                    115 if role == 'rendered' else 98, 0, 0, -1, 0 if role == 'rendered' else 1)
        model.finish()
        assert status == -1
        baseline = {value.identity: 1}
    else:
        value = model.new(ABI['PY_TYPE_BYTES'] if role == 'bytearray' else ABI['PY_TYPE_STR'], b'TEXT' if role == 'bytearray' else 'TEXT')
        arguments = model.new(ABI['PY_TYPE_DICT'], {'key': value.identity}, children=[value]) if role == 'key' else value
        format_obj = model.new(ABI['PY_TYPE_BYTEARRAY'] if role == 'bytearray' else ABI['PY_TYPE_STR'],
                               b'%s' if role == 'bytearray' else '%(key)s' if role == 'key' else '%s')
        baseline = {identity: obj['refs'] for identity, obj in model.objects.items()}
        result = model.env['py_bytes_mod' if role == 'bytearray' else 'py_str_mod'](format_obj, arguments)
        model.finish(result)
        assert result is None
    assert model.lease_failed and model.pending == 'lease-error'
    assert all(model.objects[identity]['refs'] == refs for identity, refs in baseline.items())
    assert all(obj['refs'] == 0 for identity, obj in model.objects.items() if identity not in baseline)


@pytest.mark.parametrize('relocate', (False, True))
@pytest.mark.parametrize('custom_tag', (ABI['PY_TYPE_BYTEARRAY'], ABI['PY_TYPE_MEMORYVIEW']))
def test_custom_byteslike_result_copies_before_reading_its_payload(runtime_source, relocate, custom_tag):
    # These return types are historically accepted by PCC. This owner repair
    # does not redefine that separate CPython return-type compatibility gap.
    model = PercentMemory(runtime_source, relocate, custom_tag=custom_tag)
    value = model.new(ABI['PY_TYPE_INSTANCE'], 'value')
    output = model.buffer(32)
    status = model.env['_percent_text'](output, value, Payload(b'%b'), model.alloc(72), 98, 0, 0, -1, 1)
    model.finish()
    assert status == 0 and model.pending is None
    assert bytes(model.load(output).value) == b'BYTES'
    assert ('bytes-copy', custom_tag) in model.events
    assert model.objects[value.identity]['refs'] == 1
    assert all(obj['refs'] == 0 for identity, obj in model.objects.items() if identity != value.identity)


@pytest.mark.parametrize('relocate', (False, True))
@pytest.mark.parametrize('copy_failure', (1, 2))
def test_partial_input_copy_failure_releases_only_acquired_owners(runtime_source, relocate, copy_failure):
    model = PercentMemory(runtime_source, relocate, copy_failure=copy_failure)
    pattern = model.new(ABI['PY_TYPE_STR'], '%s')
    value = model.new(ABI['PY_TYPE_STR'], 'TEXT')
    result = model.env['py_str_mod'](pattern, value)
    model.finish(result)
    assert result is None and model.pending == 'copy-error'
    assert model.copy_count == copy_failure
    assert all(obj['refs'] == 1 for obj in model.objects.values())
