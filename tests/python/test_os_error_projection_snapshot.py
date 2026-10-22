"""Actual getter/setter interleavings across first OSError payload publication."""
from pathlib import Path

import pytest

from pcc.runtime.py import py_abi_constants as abi
from test_exception_constructor_roots import _Object, _Slots
from test_os_error_attribute_owner import ErrorMemory, ROOT, bodies


def legacy_error(memory):
    error = memory.namespace['py_exc_new'](34, 'legacy')
    memory.handled.fields[0] = error
    return error


def read_args_during_first_setter(memory, field):
    legacy_error(memory)
    original = memory.namespace['py_tuple_new']
    replacement = 2 if field == 'errno' else memory.argument_tuple(('changed',))
    root = _Slots(8)
    root.fields[0] = replacement
    memory.frames['replacement'] = root
    events = []
    armed = True
    def allocate(count):
        nonlocal armed
        if armed:
            armed = False
            assert memory.actual_setattr(memory.handled.fields[0], field, root.fields[0]) == 0
            events.append('published')
        return original(count)
    memory.namespace['py_tuple_new'] = allocate
    memory.dispatch['py_tuple_new'] = allocate
    result = memory.dispatch['py_obj_getattr'](memory.handled.fields[0], 'args')
    assert events == ['published']
    memory.store_root(root, None)
    del memory.frames['replacement']
    return result


def read_primary_during_flag_transition(memory, view, field='errno'):
    legacy_error(memory)
    replacement = 2 if field == 'errno' else memory.argument_tuple(('changed',))
    replacement_root = _Slots(8)
    replacement_root.fields[0] = replacement
    memory.frames['replacement'] = replacement_root
    original_read = memory.namespace['load_i32']
    original_unlock = memory.namespace['pcc_py_gc_minor_graph_unlock']
    armed = True
    deferred = False
    events = []
    def publish():
        assert memory.graph_depth == 0
        assert memory.actual_setattr(memory.handled.fields[0], field, replacement_root.fields[0]) == 0
        events.append('published')
    def read(value, offset):
        nonlocal armed, deferred
        result = original_read(value, offset)
        if armed and value is memory.handled.fields[0] and offset == 12:
            armed = False
            if memory.graph_depth:
                deferred = True
                events.append('deferred-under-lease')
            else:
                publish()
        return result
    def unlock():
        nonlocal deferred
        original_unlock()
        if deferred and memory.graph_depth == 0:
            deferred = False
            publish()
    memory.namespace['load_i32'] = read
    memory.dispatch['load_i32'] = read
    memory.namespace['pcc_py_gc_minor_graph_unlock'] = unlock
    memory.dispatch['pcc_py_gc_minor_graph_unlock'] = unlock
    if view == 'message':
        result = memory.namespace['py_exc_get_message'](memory.handled.fields[0])
    else:
        result = memory.dispatch['py_obj_getattr'](memory.handled.fields[0], 'value')
    assert 'published' in events
    memory.store_root(replacement_root, None)
    del memory.frames['replacement']
    return result, events


@pytest.mark.parametrize('field', ['errno', 'args'])
def test_args_retains_legacy_snapshot_across_first_metadata_setter(field):
    memory = ErrorMemory()
    result = read_args_during_first_setter(memory, field)
    assert result.fields[24] is not memory.handled.fields[0].fields[24], 'private record escaped as args[0]'
    assert memory.python(result) == ('legacy',)
    assert result is not memory.handled.fields[0].fields[24]
    memory.decref(result)
    assert not memory.pin_metric and not memory.graph_depth


def test_value_owns_legacy_argument_when_first_setter_replaces_args():
    memory = ErrorMemory()
    result, events = read_primary_during_flag_transition(memory, 'value', 'args')
    assert memory.python(result) == 'legacy'
    assert result.references == 1
    memory.decref(result)
    assert not result.alive
    assert events == ['deferred-under-lease', 'published']
    assert not memory.pin_metric and not memory.graph_depth


@pytest.mark.parametrize('value,expected', [('no-argument', ()), ('explicit-none', (None,))])
def test_args_snapshot_preserves_empty_versus_explicit_none(value, expected):
    memory = ErrorMemory()
    if value == 'no-argument':
        error = memory.namespace['py_exc_new'](34, None)
    else:
        error = memory.namespace['py_exc_new_with_value'](34, memory.none)
    memory.handled.fields[0] = error
    original = memory.namespace['py_tuple_new']
    events = []
    def allocate(count):
        if not events:
            events.append(count)
            assert memory.actual_setattr(memory.handled.fields[0], 'errno', 2) == 0
        return original(count)
    memory.namespace['py_tuple_new'] = allocate
    result = memory.dispatch['py_obj_getattr'](memory.handled.fields[0], 'args')
    assert memory.python(result) == expected
    assert events == [len(expected)]
    memory.decref(result)
    assert not memory.pin_metric and not memory.graph_depth


def test_structured_args_snapshot_retains_tuple_during_concurrent_replacement():
    memory = ErrorMemory()
    legacy_error(memory)
    assert memory.actual_setattr(memory.handled.fields[0], 'errno', 2) == 0
    old_args = memory.handled.fields[0].fields[24].fields[24]
    new_args = memory.argument_tuple(('changed',))
    replacement_root = _Slots(8)
    replacement_root.fields[0] = new_args
    memory.frames['replacement'] = replacement_root
    original_unlock = memory.namespace['pcc_py_gc_minor_graph_unlock']
    events = []
    def unlock():
        original_unlock()
        if not memory.graph_depth and not events:
            events.append('replace')
            assert memory.actual_setattr(memory.handled.fields[0], 'args', replacement_root.fields[0]) == 0
    memory.namespace['pcc_py_gc_minor_graph_unlock'] = unlock
    result = memory.dispatch['py_obj_getattr'](memory.handled.fields[0], 'args')
    assert events == ['replace']
    assert result is old_args
    assert memory.python(result) == ('legacy',)
    assert result.references == 1
    memory.decref(result)
    assert not old_args.alive
    memory.store_root(replacement_root, None)
    del memory.frames['replacement']
    assert not memory.pin_metric and not memory.graph_depth


@pytest.mark.parametrize('view', ['message', 'value'])
def test_primary_projection_does_not_expose_record_on_first_setter(view):
    memory = ErrorMemory()
    result, events = read_primary_during_flag_transition(memory, view)
    assert memory.python(result) == 'legacy'
    assert events == ['deferred-under-lease', 'published']
    if view == 'value':
        memory.decref(result)
    assert not memory.pin_metric and not memory.graph_depth


def render_print_branch(memory, error, source=ROOT):
    output = []
    namespace = memory.namespace.copy()
    memory.stubs['py_unicode_error_format'] = memory.format_namespace['py_unicode_error_format']
    def read_i64(value, offset):
        if isinstance(value, _Object) and value.tag == abi.PY_TYPE_STR and offset == 16:
            return len(memory.read(value, 'text').encode())
        return memory.read(value, offset)
    def write(fd, pointer, count):
        assert fd == 1
        value, offset = memory.pointer(pointer)
        if isinstance(value, _Object) and value.tag == abi.PY_TYPE_STR:
            raw = memory.read(value, 'text').encode()[offset - 40:offset - 40 + count]
        elif isinstance(value, str):
            raw = value.encode()[offset:offset + count]
        else:
            raw = bytes(memory.read(value, offset + index) for index in range(count))
        output.append(raw)
        return len(raw)
    namespace.update(load_i64=read_i64, write=write,
                     py_int_value_i64=lambda value: value,
                     py_obj_str=memory.stubs['py_obj_str'])
    bodies(source / 'pcc/runtime/py/py_print_fmt.py', namespace,
           names={'_format', '_format_str', '_format_int', '_write_i64', '_write_lit'})
    namespace['_format'](error)
    return b''.join(output).decode()


@pytest.mark.parametrize('kind', ['os', 'unicode', 'legacy'])
def test_actual_print_branch_uses_shared_payload_renderer(kind):
    memory = ErrorMemory()
    if kind == 'os':
        error = memory.fail_path()
        memory.store_root(memory.handled, error)
        memory.namespace['py_clear_exception']()
        expected = str(FileNotFoundError(2, 'No such file or directory', '/missing/owned'))
    elif kind == 'unicode':
        error = memory.namespace['py_unicode_decode_error_new'](
            memory.argument_tuple(('utf-8', b'\xff', 0, 1, 'invalid start byte')))
        memory.handled.fields[0] = error
        expected = str(UnicodeDecodeError('utf-8', b'\xff', 0, 1, 'invalid start byte'))
    else:
        error = memory.namespace['py_exc_new'](2, 'legacy')
        memory.handled.fields[0] = error
        expected = 'legacy'
    assert render_print_branch(memory, memory.handled.fields[0]) == expected
    assert memory.pending.fields[0] is None
    assert not memory.pin_metric and not memory.graph_depth


def test_args_allocation_failure_releases_snapshot_owner():
    memory = ErrorMemory()
    error = legacy_error(memory)
    argument = error.fields[24]
    def fail(_count):
        memory.namespace['py_raise_owned'](memory.namespace['py_exc_new'](19, 'projection out of memory'))
        return None
    memory.namespace['py_tuple_new'] = fail
    assert memory.dispatch['py_obj_getattr'](memory.handled.fields[0], 'args') is None
    assert argument.references == 1 and argument.alive
    assert memory.pending.fields[0].fields[16].fields['exception_tag'] == 19
    assert not memory.pin_metric and not memory.graph_depth


def test_unicode_args_uses_same_owned_projection_and_preserves_identity():
    memory = ErrorMemory()
    arguments = memory.argument_tuple(('utf-8', b'\xff', 0, 1, 'invalid start byte'))
    error = memory.namespace['py_unicode_decode_error_new'](arguments)
    memory.handled.fields[0] = error
    before = arguments.references
    result = memory.dispatch['py_obj_getattr'](error, 'args')
    assert result is arguments and arguments.references == before + 1
    memory.decref(result)
    assert arguments.references == before
    assert not memory.pin_metric and not memory.graph_depth
