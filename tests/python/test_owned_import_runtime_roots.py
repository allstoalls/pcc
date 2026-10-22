"""Actual import-consumer bodies against a relocating, counted-lease model.

These bounded component checks and emitted IR checks do not qualify native
collectors or extension loading. No private registry pin is assumed.
"""
from __future__ import annotations

import ast
from pathlib import Path
import re

import pytest

from tests.python.test_compiled_module_parent_publication import _Block, _Memory

ROOT = Path(__file__).resolve().parents[2]
IMPORT = ROOT / 'pcc/runtime/py/py_capi_import_runtime.py'
CAPSULE = IMPORT.with_name('py_capi_capsule_runtime.py')


class ImportMemory(_Memory):
    def __init__(self, *, phase='', fail='', import_source=IMPORT):
        # Reuse byte/slot operations only, without executing a cache/registry.
        self.globals, self.attrs, self.objects, self.raw = {}, {}, [], []
        self.frames, self.leases, self.providers = [], {}, {}
        self.error = self.cleanup_callback = None
        self.phase, self.fail, self.moves = phase, fail, 0
        self.serial = 0
        self.latest_objects = {}
        self.none = self.object('none')
        self.globals['py_None'] = self.none
        self.decoy = self.object('decoy')
        self.module_cleanup = self.value_cleanup = None
        self.len_result = 1
        self.imports = []
        self.foreign_pointer = object()
        self.ns = dict(
            c_ptr=object, C_POINTER_SIZE=8, PYOBJECTHEADER_FLAGS_OFFSET=12,
            PY_FLAG_GC_PINNED=64, PY_TYPE_STR=4,
            c_abi_typed_export=lambda *_a: lambda fn: fn,
            null=lambda: None, ptr_is_null=lambda p: p is None,
            ptr_eq=lambda a, b: a is b, is_tagged_int=lambda v: isinstance(v, int),
            malloc=self.malloc, free=self.free, free_c=self.free,
            PyMem_Malloc=self.top_malloc, PyMem_Free=self.free,
            memcpy=self.memcpy, memcpy_c=self.memcpy,
            cstr=self.cstr, strlen=lambda p: len(self.text(p)),
            ptr_add=self.add, load_i8=self.byte, store_i8=self.setbyte,
            load_i32=self.read, load_i64=self.read, load_ptr=self.read,
            store_i64=self.write, store_ptr=self.write,
            stack_alloc=self.malloc, memset=self.zero,
            global_addr=lambda name: name, global_load_ptr=self.globals.get,
            py_str_utf8=self.utf8, PyUnicode_AsUTF8=self.utf8,
            py_str_byte_len=lambda value: len(value.bytes)-1,
            py_type_of=lambda value: 4 if value.kind == 'string' else 99,
            py_native_extension_import_by_name=self.import_module,
            py_compiled_module_import_by_name=lambda _name: None,
            py_obj_len=self.length, py_obj_getattr=self.getattr,
            py_err_occurred=lambda: int(self.error is not None),
            py_exc_new=lambda tag, message: (tag, self.text(message)),
            py_raise_owned=self.raise_error, py_clear_exception=self.clear_error,
            py_runtime_error_if_unset=self.runtime_error,
            py_incref=self.incref, py_decref=self.decref,
            pcc_gc_frame_enter=self.frame_enter, pcc_gc_frame_leave=self.frame_leave,
            pcc_gc_root_copy_borrowed_lease=self.copy_borrowed,
            pcc_gc_foreign_lease_acquire=self.acquire,
            pcc_gc_foreign_lease_release=self.release,
            pcc_gc_note_slot_write_barrier=self.barrier,
            pcc_gc_store_root=self.store_root,
            pcc_gc_load_ptr=lambda _owner, slot: self.read(slot, 0),
            pcc_gc_pin=self.pin, pcc_gc_take_pinned_slot=self.take_slot,
            pcc_py_gc_minor_graph_lock=lambda: self.gate('lock'),
            pcc_py_gc_minor_graph_unlock=lambda: None,
            py_tls_exc_swap_slot=self.swap_error, pcc_platform_abort=self.abort,
        )
        for source in (import_source, CAPSULE):
            tree = ast.parse(source.read_text(), filename=str(source))
            nodes = [n for n in tree.body if isinstance(n, ast.FunctionDef)
                     or (isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
                         and any(isinstance(t, ast.Name) and t.id.startswith(
                             ('_IMPORT_', '_CAPSULE_IMPORT_')) for t in n.targets))]
            exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), self.ns)
        # Capsule representation itself is separately owned; this boundary
        # validates that import supplies its current, live leased argument.
        self.ns['pcc_py_capsule_get_pointer'] = self.capsule_pointer

    def object(self, kind):
        value = super().object(kind)
        value.serial = self.serial
        self.serial += 1
        self.latest_objects[value.serial] = value
        return value

    def latest(self, value):
        return self.latest_objects[value.serial]

    def gate(self, phase):
        if phase != self.phase:
            return
        for old in list(self.objects):
            if not old.alive or old.pin or old.kind == 'none' or self.leases.get(old.serial, 0):
                continue
            new = _Block(kind=old.kind)
            new.bytes, new.fields, new.refs = old.bytes[:], old.fields.copy(), old.refs
            new.serial, new.text = old.serial, old.text
            self.objects.append(new)
            self.latest_objects[new.serial] = new
            for owner in self.objects + [frame for _mapping, frame in self.frames]:
                if owner.alive:
                    owner.fields = {k: new if v is old else v for k, v in owner.fields.items()}
            if self.error is old:
                self.error = new
            old.alive = False
            self.moves += 1

    def malloc(self, size):
        value = _Block(size)
        self.raw.append(value)
        return value

    def top_malloc(self, size):
        self.gate('allocation')
        return None if self.fail == 'allocation' else self.malloc(size)

    def zero(self, value, byte, size):
        value.bytes[:] = bytes([byte]) * size

    def frame_enter(self, mapping, slots):
        counts = {'pcc_capi_import_borrowed_map': -2, 'pcc_capi_import_owned_map': 5,
                  'pcc_capsule_import_owned_map': 3}
        assert mapping in counts, 'frame ABI is (map, slots)'
        assert len(slots.bytes) == abs(counts[mapping]) * 8
        self.frames.append((mapping, slots))
        self.gate('frame')

    def frame_leave(self, slots):
        self.gate('leave')
        assert self.frames.pop()[1] is slots

    def registered(self, slot):
        block, _offset = self.pointer(slot)
        assert any(frame is block for _mapping, frame in self.frames)

    def acquire(self, slot):
        self.registered(slot)
        self.gate('acquire')
        value = self.read(slot, 0)
        if value is None or isinstance(value, int):
            return 0
        assert value.alive
        if self.fail == 'lease' and value.kind in ('module', 'capsule'):
            return -1
        self.leases[value.serial] = self.leases.get(value.serial, 0) + 1
        return 1

    def copy_borrowed(self, destination, source):
        self.registered(destination)
        self.registered(source)
        self.gate('copy')
        if self.fail == 'copy' and self.pointer(source)[1] == 8:
            return -1
        value = self.read(source, 0)
        assert self.read(destination, 0) is None
        self.incref(value)
        self.write(destination, 0, value)
        return self.acquire(destination)

    def release(self, slot, token):
        self.gate('release')
        if token:
            value = self.read(slot, 0)
            assert token == 1 and value.alive
            assert self.leases[value.serial] > 0
            self.leases[value.serial] -= 1
        return 0

    def barrier(self, _owner, slot, value):
        self.registered(slot)
        assert value is None or isinstance(value, int) or value.alive

    def pin(self, value):
        assert value.alive
        value.pin = True

    def take_slot(self, slot, prior):
        value = self.read(slot, 0)
        self.write(slot, 0, None)
        if isinstance(value, _Block):
            assert value.alive and value.pin
            value.pin = bool(prior & 64)
        return value

    def store_root(self, slot, value):
        self.registered(slot)
        self.gate('store')
        old = self.read(slot, 0)
        self.write(slot, 0, value)
        self.decref(old)

    def swap_error(self, slot):
        self.gate('error')
        old = self.read(slot, 0)
        self.write(slot, 0, self.error)
        self.error = old

    def decref(self, value):
        if not isinstance(value, _Block):
            return
        assert value.alive, 'stale cleanup address'
        value.refs -= 1
        assert value.refs >= 0
        if value.refs == 0:
            assert not value.pin and not self.leases.get(value.serial, 0)
            value.alive = False
            callback = (self.module_cleanup if value.kind == 'module' else
                        self.value_cleanup if value.kind == 'capsule' else None)
            if callback:
                callback()

    def string_arg(self, text):
        value = self.object('string')
        value.bytes = bytearray(text.encode() + b'\0')
        return value

    def utf8(self, value):
        self.gate('utf8')
        assert value.alive
        return value

    def text(self, value):
        obj, _base = self.pointer(value)
        assert obj.alive, 'stale UTF-8 address'
        return super().text(value)

    def import_module(self, name):
        self.gate('loader')
        text = self.text(name)
        self.imports.append(text)
        factory = self.providers.get(text)
        return factory() if factory else None

    def length(self, fromlist):
        assert fromlist.alive, 'stale borrowed fromlist'
        self.gate('len')
        if self.fail == 'len':
            self.raise_error('original len error')
            self.original_error = self.error
            return -1
        return self.len_result

    def getattr(self, module, name):
        assert module.alive, 'stale getattr module'
        self.gate('getattr')
        assert self.text(name) == 'api'
        if self.fail == 'getattr':
            self.raise_error('original getattr error')
            self.original_error = self.error
            return None
        return self.object('capsule')

    def capsule_pointer(self, capsule, name):
        self.gate('pointer')
        assert capsule.alive, 'stale capsule pointer'
        if self.fail == 'pointer':
            self.raise_error('original capsule error')
            self.original_error = self.error
            return None
        return self.foreign_pointer

    def runtime_error(self, _site, message):
        if self.error is None:
            self.raise_error(self.text(message))

    def abort(self):
        raise AssertionError('lease protocol abort')

    def assert_balanced(self):
        assert not self.frames and not any(self.leases.values())
        assert not any(obj.pin for obj in self.objects if obj.alive)


def builtin(m, name='pkg.child'):
    argument = m.string_arg(name)
    fromlist = m.object('fromlist')
    result = m.ns['py_builtin_import'](argument, fromlist)
    assert m.latest(argument).refs == m.latest(fromlist).refs == 1
    return result


@pytest.mark.parametrize('phase', ['len', 'acquire', 'copy', 'frame', 'loader',
                                   'utf8', 'release', 'store', 'error', 'leave', 'lock'])
def test_builtin_import_survives_relocation_at_every_owner_boundary(phase):
    m = ImportMemory(phase=phase)
    m.providers['pkg.child'] = lambda: m.object('module')
    result = builtin(m)
    assert result.alive and result.kind == 'module' and result.refs == 1
    assert m.moves > 0
    m.assert_balanced()


@pytest.mark.parametrize('phase', ['len', 'allocation', 'loader', 'acquire', 'release', 'leave'])
def test_builtin_dotted_import_keeps_top_result_across_child_cleanup(phase):
    m = ImportMemory(phase=phase)
    m.len_result = 0
    m.providers['pkg.child'] = lambda: m.object('module')
    m.providers['pkg'] = lambda: m.object('top')
    m.module_cleanup = lambda: (m.gate(phase), m.raise_error('cleanup ignored'))
    result = builtin(m)
    assert result.alive and result.kind == 'top' and result.refs == 1
    assert m.imports == ['pkg.child', 'pkg'] and m.error is None
    assert not any(o.alive and o.kind == 'module' for o in m.objects)
    m.assert_balanced()


@pytest.mark.parametrize('failure', ['len', 'allocation', 'lease'])
def test_builtin_failure_releases_new_and_preserves_original_error(failure):
    m = ImportMemory(phase='store', fail=failure)
    m.len_result = 0
    m.providers['pkg.child'] = lambda: m.object('module')
    m.module_cleanup = lambda: m.raise_error('cleanup error')
    assert builtin(m) is None
    if failure == 'len':
        assert m.error is m.latest(m.original_error)
        assert m.error.text == 'original len error'
    elif failure == 'allocation':
        assert m.error.text == (19, 'out of memory importing module')
    else:
        assert m.error.text == 'result owner lease failed'
    assert not any(o.alive and o.kind == 'module' for o in m.objects)
    m.assert_balanced()


def test_builtin_missing_top_preserves_capi_diagnostic_through_cleanup():
    m = ImportMemory()
    m.len_result = 0
    m.providers['pkg.child'] = lambda: m.object('module')
    m.module_cleanup = lambda: m.raise_error('cleanup ignored')
    assert builtin(m) is None
    assert m.error.text[0] == 7 and 'module not found: pkg' in m.error.text[1]
    m.assert_balanced()


@pytest.mark.parametrize('phase', ['loader', 'acquire', 'copy', 'leave'])
def test_capi_unicode_import_preserves_borrowed_name_and_new_result(phase):
    m = ImportMemory(phase=phase)
    m.providers['pkg'] = lambda: m.object('module')
    argument = m.string_arg('pkg')
    result = m.ns['PyImport_Import'](argument)
    assert result.alive and result.refs == 1
    assert m.latest(argument).refs == 1
    m.assert_balanced()


@pytest.mark.parametrize('phase', ['getattr', 'acquire', 'release', 'store', 'error', 'leave', 'pointer'])
def test_capsule_import_roots_module_and_capsule_during_callbacks(phase):
    m = ImportMemory(phase=phase)
    m.providers['pkg'] = lambda: m.object('module')
    cleaned = []
    m.module_cleanup = lambda: (cleaned.append('module'), m.gate(phase))
    m.value_cleanup = lambda: cleaned.append('value')
    assert m.ns['PyCapsule_Import'](m.cstr('pkg.api'), 0) is m.foreign_pointer
    assert cleaned == ['module', 'value']
    assert not any(o.alive and o.kind in ('module', 'capsule') for o in m.objects)
    assert m.moves > 0
    m.assert_balanced()


@pytest.mark.parametrize('failure', ['getattr', 'pointer', 'lease'])
def test_capsule_import_preserves_original_error_across_destructors(failure):
    m = ImportMemory(phase='store', fail=failure)
    m.providers['pkg'] = lambda: m.object('module')
    m.module_cleanup = m.value_cleanup = lambda: m.raise_error('cleanup ignored')
    assert m.ns['PyCapsule_Import'](m.cstr('pkg.api'), 0) is None
    if failure == 'lease':
        assert m.error.text == 'result owner lease failed'
    else:
        assert m.error is m.latest(m.original_error)
        assert m.error.text.startswith('original ')
    m.assert_balanced()


def test_builtin_preserves_prior_pin_and_tagged_cache_values():
    m = ImportMemory(phase='leave')
    pinned = m.object('module')
    pinned.pin = True
    m.providers['pkg.child'] = lambda: pinned
    assert builtin(m) is pinned and pinned.pin and pinned.refs == 1
    assert not m.frames and not any(m.leases.values())
    m = ImportMemory()
    m.providers['pkg.child'] = lambda: 17
    assert builtin(m) == 17
    m.assert_balanced()


@pytest.mark.parametrize('name, code, message', [
    ('', 2, 'Empty module name'),
    ('pkg\0child', 2, 'module name contains a null character'),
    ('missing', 21, "No module named 'missing'"),
])
def test_builtin_validation_and_missing_errors_survive_frame_cleanup(name, code, message):
    m = ImportMemory(phase='store')
    assert builtin(m, name) is None
    assert m.error.text == (code, message)
    m.assert_balanced()


def _ir_body(text, symbol):
    found = re.search(r'^define [^\n]*@' + re.escape(symbol)
                      + r'\([^\n]*\)[^\n]*\{\n(.*?)^\}', text, re.M | re.S)
    assert found, symbol
    return found[1]


def _assert_new_is_published(body, callee):
    calls = list(re.finditer(r'(%[-\w.]+) = call ptr [^\n]*@' + callee
                            + r'\([^\n]*\)\n', body))
    assert calls, callee
    for call in calls:
        assert re.match(r'\s*store ptr ' + re.escape(call[1]) + r', ptr ', body[call.end():])
        assert len(re.findall(re.escape(call[1]) + r'(?![-\w.])', body)) == 2


@pytest.mark.parametrize('module', ['py_capi_import_runtime', 'py_capi_capsule_runtime'])
def test_changed_import_consumers_emit_immediate_roots_and_correct_frame_abi(tmp_path, monkeypatch, module):
    from pcc.frontends.python.owned_runtime_build import _compile_runtime_module
    from tests.owned_ir_validation import verify_ir_text

    monkeypatch.setenv('PCC_WITH_THREADS', '1')
    output = tmp_path / (module + '.ll')
    _compile_runtime_module(module, str(IMPORT.with_name(module + '.py')), str(output),
                            'x86_64-unknown-linux-gnu')
    text = output.read_text()
    verify_ir_text(text)
    if module == 'py_capi_import_runtime':
        body = _ir_body(text, 'user_' + module + '__builtin_import_body')
        for callee in ('py_native_extension_import_by_name', 'py_compiled_module_import_by_name',
                       'PyImport_ImportModule'):
            _assert_new_is_published(body, callee)
        wrapper = _ir_body(text, 'user_' + module + '__import_with_roots')
        _assert_new_is_published(wrapper, 'PyImport_ImportModule')
        assert wrapper.index('store ptr %name,') < wrapper.index('@pcc_gc_frame_enter(')
        assert wrapper.index('store ptr %fromlist,') < wrapper.index('@pcc_gc_frame_enter(')
        finish = _ir_body(text, 'user_' + module + '__import_finish')
        transfer = finish.index('@pcc_gc_take_pinned_slot(')
        assert finish.rfind('@pcc_gc_frame_leave(') < transfer
        assert not re.search(r'\bcall\b', finish[finish.index('\n', transfer):])
        maps = ('pcc_capi_import_borrowed_map', 'pcc_capi_import_owned_map')
    else:
        body = _ir_body(text, 'user_' + module + '__capsule_import_body')
        _assert_new_is_published(body, 'py_native_extension_import_by_name')
        _assert_new_is_published(body, 'py_obj_getattr')
        wrapper = _ir_body(text, 'PyCapsule_Import')
        maps = ('pcc_capsule_import_owned_map',)
    for mapping in maps:
        # The frame map is passed directly, or through an identity pointer alias.
        addresses = ['@' + mapping]
        addresses += re.findall(r'(%[-\w.]+) = bitcast ptr @' + mapping + r' to ptr', wrapper)
        assert any(re.search(r'@pcc_gc_frame_enter\(ptr ' + re.escape(address) + r', ptr ', wrapper)
                   for address in addresses), mapping


def test_second_borrowed_copy_failure_drops_only_acquired_owner():
    m = ImportMemory(phase='store', fail='copy')
    assert builtin(m) is None
    assert m.error.text == 'argument owner copy failed'
    m.assert_balanced()


def test_dotted_import_same_object_uses_independent_owner_leases():
    m = ImportMemory(phase='acquire')
    m.len_result = 0
    module = m.object('module')
    m.providers['pkg.child'] = lambda: m.latest(module)
    def top():
        current = m.latest(module)
        m.incref(current)
        return current
    m.providers['pkg'] = top
    result = builtin(m)
    assert result is m.latest(module) and result.refs == 1 and result.alive
    m.assert_balanced()
