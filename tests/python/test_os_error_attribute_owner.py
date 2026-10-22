"""Actual exception/dispatch/TLS/tempfile bodies, with raw ABI leaves modeled.

These bounded host checks are not native or collector qualification.
"""
import ast
from functools import lru_cache
import os
from pathlib import Path

import pytest

from pcc.runtime.py import py_abi_constants as abi
from test_exception_constructor_roots import _Object, _Slots
from test_unicode_decode_error_payload import DecodeMemory


ROOT = Path(__file__).resolve().parents[2]


@lru_cache(maxsize=32)
def _compiled_bodies(source, filename, names):
    # Cache only immutable code. Every exec below creates fresh functions
    # bound to the caller's fresh model namespace and injected raw leaves.
    tree = ast.parse(source, filename=filename)
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and (names is None or node.name in names):
            node.decorator_list = []
            nodes.append(node)
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            nodes.append(node)
    return compile(ast.Module(body=nodes, type_ignores=[]), filename, 'exec')


def bodies(path, namespace, *, names=None):
    # Content, filename and selection identify the code. Reading on every
    # call prevents stale results when a preimage or source file changes.
    selection = None if names is None else frozenset(names)
    exec(_compiled_bodies(path.read_text(), str(path), selection), namespace)


def test_body_code_cache_isolates_model_namespaces(tmp_path):
    source = tmp_path / 'bodies.py'
    source.write_text('def touch(value):\n    state.append(leaf() + value)\n    return state\n')
    first = {'state': [], 'leaf': lambda: 10}
    second = {'state': [], 'leaf': lambda: 20}
    bodies(source, first)
    bodies(source, second)
    assert first['touch'] is not second['touch']
    assert first['touch'].__code__ is second['touch'].__code__
    assert first['touch'].__globals__ is first
    assert second['touch'].__globals__ is second
    assert first['touch'](1) == [11]
    assert second['state'] == []
    assert second['touch'](2) == [22]
    assert first['state'] == [11]


def test_body_code_cache_invalidates_changed_source_and_filename(tmp_path):
    source = tmp_path / 'bodies.py'
    source.write_text('VALUE = 1\ndef read():\n    return VALUE\n')
    first = {}
    bodies(source, first)
    source.write_text('VALUE = 2\ndef read():\n    return VALUE\n')
    second = {}
    bodies(source, second)
    assert first['read']() == 1
    assert second['read']() == 2
    assert first['read'].__code__ is not second['read'].__code__
    other = tmp_path / 'other.py'
    other.write_text(source.read_text())
    third = {}
    bodies(other, third)
    assert third['read']() == 2
    assert second['read'].__code__.co_filename == str(source)
    assert third['read'].__code__.co_filename == str(other)


def test_body_code_cache_keeps_name_selections_distinct(tmp_path):
    source = tmp_path / 'bodies.py'
    source.write_text('VALUE = 1\ndef first():\n    return 1\ndef second():\n    return 2\n')
    names = {'first'}
    first = {}
    bodies(source, first, names=names)
    names.clear()
    names.add('second')
    second = {}
    bodies(source, second, names=names)
    empty = {}
    bodies(source, empty, names=set())
    all_names = {}
    bodies(source, all_names)
    assert first['first']() == 1 and 'second' not in first
    assert second['second']() == 2 and 'first' not in second
    assert empty['VALUE'] == 1 and 'first' not in empty and 'second' not in empty
    assert all_names['first']() == 1 and all_names['second']() == 2


class ErrorMemory(DecodeMemory):
    def __init__(self, phase='', preimage_root=None):
        super().__init__(phase)
        source = preimage_root if preimage_root is not None else ROOT
        self.set_calls = []
        self.maps.update(pcc_raise_borrowed_frame_map=-2, pcc_raise_owned_frame_map=1,
                         pcc_tempdir_frame_map=24)
        names = {1: 'Exception', 2: 'ValueError', 3: 'TypeError', 6: 'AttributeError',
                 14: 'OSError', 19: 'MemoryError', 34: 'FileNotFoundError',
                 35: 'FileExistsError', 36: 'PermissionError', 37: 'IsADirectoryError',
                 38: 'NotADirectoryError', 39: 'ProcessLookupError', 40: 'ChildProcessError',
                 41: 'TimeoutError', 42: 'InterruptedError', 43: 'BlockingIOError',
                 44: 'ConnectionError', 45: 'BrokenPipeError', 46: 'ConnectionAbortedError',
                 47: 'ConnectionRefusedError', 48: 'ConnectionResetError'}
        for tag, name in names.items():
            cls = self.make(abi.PY_TYPE_CLASS, 65)
            cls.fields.update(exception_tag=tag)
            cls.fields[abi.PYCLASSOBJECT_NAME_OFFSET] = name
            self.class_cache.fields[tag * 8] = cls
        for tag in names:
            lineage = [tag]
            if 45 <= tag <= 48:
                lineage.append(44)
            if 34 <= tag <= 48:
                lineage.append(14)
            mro = _Slots(len(lineage) * 8)
            for index, base in enumerate(lineage):
                mro.fields[index * 8] = self.class_cache.fields[base * 8]
            cls = self.class_cache.fields[tag * 8]
            cls.fields[abi.PYCLASSOBJECT_MRO_OFFSET] = mro
            cls.fields[abi.PYCLASSOBJECT_N_MRO_OFFSET] = len(lineage)
        self.handled = _Slots(8)
        self.handled.fields[0] = None
        self.frames['handled'] = self.handled
        self.pending = _Slots(8)
        self.pending.fields[0] = None
        self.frames['pending'] = self.pending
        n = self.namespace
        bodies(source / 'pcc/runtime/py/py_exc_objects.py', n)
        n.update(c_ptr=object, load_i64=self.read, store_i64=self.write,
                 strcmp=lambda a, b: (self.raw_text(a) > self.raw_text(b)) - (self.raw_text(a) < self.raw_text(b)),
                 py_tls_exc_get=lambda: self.pending.fields[0],
                 py_tls_exc_set=lambda value: self.pending.fields.__setitem__(0, value),
                 py_tls_exc_swap_slot=self.swap_pending,
                 py_handled_exception_slot=lambda: self.handled,
                 pcc_gc_unpin=self.unpin, pcc_gc_note_write_barrier=lambda *_: None,
                 errno_message=self.errno_message)
        bodies(ROOT / 'pcc/runtime/py/py_exc_match.py', n)
        tls = n.copy()
        bodies(ROOT / 'pcc/runtime/py/py_exc_tls.py', tls)
        n.update({name: tls[name] for name in ('py_raise_owned', 'py_raise', 'py_clear_exception', 'py_err_occurred')})
        tls.update(n)
        dispatch = n.copy()
        dispatch.update(pcc_capi_type_object_getattr=lambda *_: None,
                        pcc_capi_builtin_object_getattr=lambda *_: None,
                        pcc_capi_is_cext_type_tag=lambda tag: int(tag >= abi.PY_TYPE_CEXT_TAG_BASE))
        bodies(source / 'pcc/runtime/py/py_obj_ops_dispatch.py', dispatch)
        self.dispatch = dispatch
        n['py_obj_getattr'] = dispatch['py_obj_getattr']
        n['py_obj_setattr'] = self.actual_setattr
        td = n.copy()
        bodies(source / 'pcc/runtime/py/py_tempfile.py', td)
        self.tempfile = td
        f = self.format_namespace
        f.update(py_os_error_get_field=n['py_os_error_get_field'],
                 py_os_error_fields_present=n['py_os_error_fields_present'])
        bodies(ROOT / 'pcc/runtime/py/py_format_runtime.py', f,
               names={'_os_error_format_body', 'py_os_error_format', 'py_exc_repr'})
        stubs = n.copy()
        stubs.update(py_os_error_format=f['py_os_error_format'])
        bodies(ROOT / 'pcc/runtime/py/py_obj_stubs.py', stubs, names={'py_obj_str'})
        self.stubs = stubs

    def render(self, repr_mode=0):
        error = self.handled.fields[0]
        if repr_mode:
            result = self.format_namespace['py_exc_repr'](error)
        else:
            result = self.stubs['py_obj_str'](error)
        value = self.python(result)
        self.decref(result)
        return value

    def actual_setattr(self, owner, name, value):
        rc = self.dispatch['py_obj_setattr'](owner, name, value)
        pending = self.pending.fields[0]
        self.set_calls.append((name, rc, pending.fields[16].fields['exception_tag'] if pending else None))
        return rc

    def swap_pending(self, slot):
        old = self.pending.fields[0]
        self.pending.fields[0] = self.read(slot, 0)
        self.write(slot, 0, old)

    def unpin(self, value):
        assert value.alive and value.flags & 64
        value.flags &= ~64
        self.pin_metric -= 1

    def errno_message(self, number, buffer, _size):
        for index, byte in enumerate(os.strerror(number).encode() + b'\0'):
            self.write(buffer, index, byte)
        return 0

    def fail_path(self, status=-2):
        filename = self.string('/missing/owned', 0)
        # The actual tempfile caller already owns/pins the filename in its frame.
        self.pin(filename)
        self.tempfile['_td_os_error'](status, filename)
        self.unpin(filename)
        self.decref(filename)
        return self.pending.fields[0]


@pytest.mark.parametrize('phase', ['', 'allocation', 'string', 'frame_enter',
                                  'frame_leave', 'load_root', 'field_store',
                                  'publish', 'graph_unlock', 'tuple_set', 'tuple_get'])
def test_tempfile_metadata_reaches_shared_exception_owner(phase):
    memory = ErrorMemory(phase)
    error = memory.fail_path()
    assert error.fields[16].fields['exception_tag'] == 34, memory.set_calls
    memory.store_root(memory.handled, error)
    memory.namespace['py_clear_exception']()
    expected = {'errno': 2, 'strerror': os.strerror(2), 'filename': '/missing/owned',
                'filename2': None, 'args': (2, os.strerror(2))}
    for name, value in expected.items():
        actual = memory.dispatch['py_obj_getattr'](memory.handled.fields[0], name)
        assert memory.python(actual) == value
        memory.decref(actual)
    assert memory.pending.fields[0] is None
    assert memory.set_calls == [(name, 0, None) for name in ('errno', 'strerror', 'filename', 'args')]
    oracle = FileNotFoundError(2, os.strerror(2), '/missing/owned')
    assert memory.render() == str(oracle)
    assert memory.render(1) == repr(oracle)
    assert not memory.pin_metric and not memory.graph_depth


def test_os_error_payload_is_not_exposed_as_value():
    memory = ErrorMemory()
    error = memory.fail_path()
    memory.store_root(memory.handled, error)
    memory.namespace['py_clear_exception']()
    assert memory.dispatch['py_obj_getattr'](memory.handled.fields[0], 'value') is None
    assert memory.pending.fields[0].fields[16].fields['exception_tag'] == 6


def test_os_error_invalid_args_preserves_original_metadata():
    memory = ErrorMemory('graph_unlock')
    error = memory.fail_path()
    memory.store_root(memory.handled, error)
    memory.namespace['py_clear_exception']()
    assert memory.actual_setattr(memory.handled.fields[0], 'args', 7) == -1
    assert memory.pending.fields[0].fields[16].fields['exception_tag'] == 3
    memory.namespace['py_clear_exception']()
    args = memory.dispatch['py_obj_getattr'](memory.handled.fields[0], 'args')
    assert memory.python(args) == (2, os.strerror(2))
    memory.decref(args)


def test_non_os_exception_still_rejects_errno_attribute():
    memory = ErrorMemory()
    error = memory.namespace['py_exc_new'](2, 'original')
    memory.handled.fields[0] = error
    assert memory.actual_setattr(error, 'errno', 2) == -1
    assert memory.pending.fields[0].fields[16].fields['exception_tag'] == 6
    assert not error.flags & abi.PY_FLAG_EXC_OS_PAYLOAD
    assert memory.python(error.fields[24]) == 'original'


def test_os_error_optional_fields_default_to_none_without_payload():
    memory = ErrorMemory()
    error = memory.namespace['py_exc_new'](14, 'legacy')
    memory.handled.fields[0] = error
    for name in ('errno', 'strerror', 'filename', 'filename2'):
        value = memory.dispatch['py_obj_getattr'](memory.handled.fields[0], name)
        assert value is memory.none
        memory.decref(value)
    assert not error.flags & abi.PY_FLAG_EXC_OS_PAYLOAD
    assert memory.actual_setattr(error, 'errno', 2) == 0
    args = memory.dispatch['py_obj_getattr'](memory.handled.fields[0], 'args')
    assert memory.python(args) == ('legacy',)
    memory.decref(args)
    assert memory.render() == str(OSError('legacy'))


@pytest.mark.parametrize('allocation', [2, 3])
def test_tempfile_stops_on_first_metadata_allocation_failure(allocation):
    memory = ErrorMemory()
    original = memory.namespace['py_tuple_new']
    calls = 0
    def fail_selected(count):
        nonlocal calls
        calls += 1
        if calls == allocation:
            memory.namespace['py_raise_owned'](memory.namespace['py_exc_new'](19, 'payload out of memory'))
            return None
        return original(count)
    memory.namespace['py_tuple_new'] = fail_selected
    memory.tempfile['py_tuple_new'] = fail_selected
    error = memory.fail_path()
    assert error.fields[16].fields['exception_tag'] == 19
    assert memory.set_calls == [('errno', -1, 19)]
    assert memory.python(error.fields[24]) == 'payload out of memory'
    assert not memory.pin_metric and not memory.graph_depth


@pytest.mark.parametrize('number,cls,tag', [(1, PermissionError, 36), (2, FileNotFoundError, 34),
    (5, OSError, 14), (12, OSError, 14), (13, PermissionError, 36),
    (17, FileExistsError, 35), (20, NotADirectoryError, 38)])
def test_platform_os_errors_keep_metadata_and_correct_class(number, cls, tag):
    memory = ErrorMemory('graph_unlock')
    error = memory.fail_path(-number)
    assert error.fields[16].fields['exception_tag'] == tag
    memory.store_root(memory.handled, error)
    memory.namespace['py_clear_exception']()
    oracle = cls(number, os.strerror(number), '/missing/owned')
    assert memory.render() == str(oracle)
    assert memory.render(1) == repr(oracle)


@pytest.mark.parametrize('field,value', [('errno', None), ('errno', 'custom'),
    ('strerror', None), ('strerror', 'changed'), ('filename', None),
    ('filename', 'new'), ('filename2', 'destination'), ('args', ('different',)),
    ('args', ()), ('args', ('one', 'two')), ('args', ['converted', 'list'])])
def test_os_error_mutable_fields_preserve_independent_args(field, value):
    memory = ErrorMemory('graph_unlock')
    error = memory.fail_path()
    memory.store_root(memory.handled, error)
    memory.namespace['py_clear_exception']()
    replacement = memory.object(value)
    replacement_root = _Slots(8)
    replacement_root.fields[0] = replacement
    memory.frames['replacement'] = replacement_root
    assert memory.actual_setattr(memory.handled.fields[0], field, replacement_root.fields[0]) == 0
    memory.store_root(replacement_root, None)
    del memory.frames['replacement']
    oracle = FileNotFoundError(2, os.strerror(2), '/missing/owned')
    setattr(oracle, field, value)
    assert memory.render() == str(oracle)
    assert memory.render(1) == repr(oracle)
    actual = memory.dispatch['py_obj_getattr'](memory.handled.fields[0], 'args')
    assert memory.python(actual) == oracle.args
    memory.decref(actual)
    assert not memory.pin_metric and not memory.graph_depth


@pytest.mark.parametrize('module', ['py_exc_objects', 'py_format_runtime',
    'py_obj_ops_dispatch', 'py_obj_stubs', 'py_tempfile', 'py_print_fmt'])
def test_os_error_changed_modules_lower_to_owned_ir(tmp_path, module):
    import re
    from pcc.frontends.python.pipeline import compile_python
    from tests.owned_ir_validation import verify_ir_text
    output = tmp_path / (module + '.ll')
    compile_python(str(ROOT / 'pcc/runtime/py' / (module + '.py')), str(output),
                   backend='self', libpython_mode='off', emit_llvm_only=True, python_library=True)
    text = output.read_text()
    verify_ir_text(text)
    assert 'strict.nolib.stub' not in text
    assert not re.search(r'\bcall\b[^\n]*@py_cpy_', text)
    if module == 'py_exc_objects':
        assert re.search(r'^define[^\n]*@py_os_error_set_field\(ptr[^,]*, i64[^,]*, ptr[^)]*\)', text, re.M)
        assert re.search(r'^define[^\n]*@py_os_error_get_field\(ptr[^,]*, i64[^)]*\)', text, re.M)
        assert re.search(r'^define[^\n]*@py_exc_get_args\(ptr[^)]*\)', text, re.M)
        assert re.search(r'^define[^\n]*@py_exc_get_legacy_value\(ptr[^)]*\)', text, re.M)
    elif module == 'py_format_runtime':
        assert re.search(r'^define[^\n]*@py_os_error_format\(ptr[^,]*, i64[^)]*\)', text, re.M)


def test_os_error_flag_has_header_generated_and_static_export_identity():
    import re
    from pcc.frontends.python.codegen.port_abi_exports import PORT_ABI_NATIVE_EXPORTS
    header = (ROOT / 'pcc/runtime/src/py_internal.h').read_text()
    value = int(re.search(r'^#define PY_FLAG_EXC_OS_PAYLOAD (0x[0-9a-f]+)$', header, re.M).group(1), 16)
    assert value == abi.PY_FLAG_EXC_OS_PAYLOAD == 0x40000000
    assert PORT_ABI_NATIVE_EXPORTS['pcc.runtime.py.py_abi_constants']['PY_FLAG_EXC_OS_PAYLOAD']['value'] == value
    assert '"PY_FLAG_EXC_OS_PAYLOAD"' in (ROOT / 'scripts/gen_port_abi_constants.py').read_text()
    # Generator reentry work already reserves bit29 off-tree; don't collide.
    assert not value & 0x20000000
