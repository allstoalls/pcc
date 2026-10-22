"""Owned UnicodeEncodeError fields, immutable constructor args and formatting."""
from __future__ import annotations

import ast
import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

from pcc.frontends.python.pipeline import compile_python
from pcc.runtime.py import py_abi_constants as abi
from test_exception_constructor_roots import _Memory, _Object, _Slots
from tests.owned_ir_validation import verify_ir_text

ROOT = Path(__file__).resolve().parents[2]


class _TextPointer:
    def __init__(self, owner):
        self.owner = owner


class UnicodeMemory(_Memory):
    def __init__(self, phase):
        self.error = None
        self.index_events = []
        super().__init__(phase)
        self.maps.update(pcc_unicode_borrowed_map=-2, pcc_unicode_owned_map=8,
                         pcc_unicode_format_borrowed_map=-1, pcc_unicode_format_owned_map=8)
        n = self.namespace
        n.update(vars(abi))
        n.update(cstr=lambda x: x, ptr_eq=lambda x, y: x is y,
                 load_i8=self.load_byte,
                 store_i8=self.write, malloc=self.raw_malloc, free=lambda _: None,
                 strlen=lambda x: len(self.raw_text(x)),
                 py_obj_type_name=lambda x: self.string({abi.PY_TYPE_INT: "int", abi.PY_TYPE_BYTES: "bytes", abi.PY_TYPE_LIST: "list", abi.PY_TYPE_NONE: "NoneType"}.get(n['_type_of'](x), "object"), 0),
                 py_str_utf8=lambda x: _TextPointer(x),
                 py_str_byte_len=lambda x: len(self.read(x, 'text').encode('utf8', 'surrogatepass')),
                 atomic_rmw_i32=self.atomic, py_tuple_len=lambda x: self.read(x, 16) if x else 0,
                 py_tuple_new=self.tuple_new, py_tuple_get=self.tuple_get,
                 py_tuple_set_item=self.tuple_set, py_int_from_i64=lambda x: x,
                 py_obj_index=self.index, py_int_to_i64=self.checked_int,
                 py_exc_matches=lambda value, cls: int(isinstance(value, _Object) and value.tag == abi.PY_TYPE_EXC),
                 py_err_occurred=lambda: self.error is not None,
                 py_raise_owned=self.raise_error, py_gc_track=lambda x: None,
                 pcc_gc_backend=lambda: 0, pcc_gc_note_relocation_read=lambda x: x,
                 pcc_gc_store_ptr_plan_init=self.plan_init,
                 pcc_gc_store_ptr_plan_commit_locked=self.plan_commit,
                 pcc_gc_store_ptr_plan_finish=self.plan_finish)
        self.format_namespace = n.copy()
        tree = ast.parse((ROOT / 'pcc/runtime/py/py_format_runtime.py').read_text())
        funcs = [x for x in tree.body if isinstance(x, ast.FunctionDef) and
                 (x.name.startswith('_unicode_') or x.name == 'py_unicode_error_format')]
        f = self.format_namespace
        f.update(py_unicode_error_get_field=n['py_unicode_error_get_field'],
                 py_exc_new=n['py_exc_new'], _type_of=n['_type_of'],
                 py_int_value_i64=lambda x: x,
                 py_str_len=lambda x: len(self.read(x, 'text')),
                 py_str_ord_at_i64=lambda x, i: ord(self.read(x, 'text')[i]),
                 py_obj_str=lambda x: self.string(str(self.python(x)), 0),
                 py_obj_repr=lambda x: self.string(repr(self.python(x)), 0),
                 _buffer_new=lambda _: [], _buffer_free=lambda _: None,
                 _buffer_cstr=lambda s, x: s.append(x),
                 _buffer_char=lambda s, x: s.append(chr(x)),
                 _buffer_decimal=lambda s, x: s.append(str(x)),
                 _buffer_string=lambda s: self.string(''.join(s), 0),
                 _append_pystr=lambda s, x: s.append(self.read(x, 'text')) or 0)
        exec(compile(ast.Module(body=funcs, type_ignores=[]), 'unicode_format', 'exec'), f)

    @property
    def error(self):
        if self._pending_error is None:
            return None
        if hasattr(self, 'frames') and 'error' in self.frames:
            return self.frames['error'].fields[0]
        return self._pending_error

    @error.setter
    def error(self, value):
        self._pending_error = value

    def gc(self, phase):
        # Native _py_decref_prepare publishes DEALLOCATING before running a
        # destructor; relocation selectors exclude that terminal owner.
        terminal = [(value, value.flags & 64) for value in self.objects
                    if value.alive and value.flags & 524288]
        for value, _prior in terminal:
            value.flags |= 64
        super().gc(phase)
        for value, prior in terminal:
            value.flags = (value.flags & ~64) | prior

    def read(self, value, offset):
        if isinstance(value, tuple):
            value, base = value
            offset += base
        return super().read(value, offset)

    def write(self, value, offset, item):
        if isinstance(value, tuple):
            value, base = value
            offset += base
        return super().write(value, offset, item)

    def frame_enter(self, frame_map, slots):
        self.frames[id(slots)] = slots
        self.gc('frame_enter')

    def take(self, slot, prior):
        target, offset = self.pointer(slot)
        value = self.read(target, offset)
        self.write(target, offset, None)
        if isinstance(value, _Object):
            assert value.alive and value.flags & 64
            value.flags = (value.flags & ~64) | prior
            self.pin_metric -= 1
        return value

    def atomic(self, op, value, offset, bits, _ordering):
        old = self.read(value, offset)
        self.write(value, offset, old | bits if op == 'or' else old & bits)
        return old

    def load_byte(self, value, index):
        if isinstance(value, _TextPointer):
            text = self.read(value.owner, 'text').encode('utf8', 'surrogatepass')
            return text[index] if index < len(text) else 0
        if isinstance(value, str):
            return ord(value[index]) if index < len(value) else 0
        return self.read(value, index)

    def raw_malloc(self, size):
        self.gc('malloc')
        return _Slots(size)

    def index(self, value):
        if isinstance(value, int):
            return int(value)
        assert value.alive
        label = value.fields['index_label']
        self.index_events.append(label)
        result = value.fields['index_value']
        self.gc('index')
        assert value.alive, 'source moved during __index__ callback'
        return result

    def checked_int(self, value, overflow):
        valid = -(2**63) <= value < 2**63
        self.write(overflow, 0, 0 if valid else 1)
        return value if valid else 0

    def raw_text(self, value):
        if isinstance(value, str):
            return value
        out = []
        index = 0
        while self.read(value, index):
            out.append(chr(self.read(value, index)))
            index += 1
        return ''.join(out)

    def string(self, text, _length):
        text = self.raw_text(text)
        self.gc('string')
        value = self.make(abi.PY_TYPE_STR)
        value.fields['text'] = text
        return value

    def tuple_new(self, count):
        self.gc('allocation')
        value = self.make(abi.PY_TYPE_TUPLE)
        value.fields[16] = count
        return value

    def tuple_get(self, value, index):
        self.gc('tuple_get')
        result = self.read(value, 24 + index * 8)
        self.incref(result)
        return result

    def tuple_set(self, value, index, item):
        self.gc('tuple_set')
        self.incref(item)
        self.write(value, 24 + index * 8, item)

    def python(self, value):
        if not isinstance(value, _Object):
            return value
        assert value.alive
        if value.tag == abi.PY_TYPE_STR:
            return value.fields['text']
        if value.tag == abi.PY_TYPE_TUPLE:
            return tuple(self.python(value.fields[24 + i * 8]) for i in range(value.fields[16]))
        if value is self.none:
            return None
        return value

    def raise_error(self, error):
        self.error = error
        # TLS owns the new error and participates in tracing.
        slot = _Slots(8)
        slot.fields[0] = error
        self.frames['error'] = slot

    def plan_init(self, plan, _owner, _backend):
        self.frames[id(plan)] = plan

    def plan_commit(self, plan, owner, slot, value):
        self.incref(value)
        target, offset = self.pointer(slot)
        plan.fields[0] = self.read(target, offset)
        self.write(target, offset, value)
        return 1

    def plan_finish(self, plan):
        self.decref(plan.fields[0])
        del self.frames[id(plan)]

    def make_error(self, text='aé€z', start=1, end=3):
        value = self.string(text, 0)
        self.namespace['py_unicode_encode_error'](value, 'ascii', start, end, 'ordinal not in range(128)')
        assert self.error is not None
        return self.frames['error'].fields[0]


@pytest.mark.parametrize('phase', ['allocation', 'string', 'frame_enter', 'frame_leave',
                                  'load_root', 'field_store', 'publish', 'graph_unlock',
                                  'tuple_set', 'tuple_get'])
def test_unicode_helper_roots_original_and_private_args(phase):
    memory = UnicodeMemory(phase)
    error = memory.make_error()
    assert error.flags & abi.PY_FLAG_EXC_UNICODE_PAYLOAD
    payload = error.fields[24]
    assert payload.tag == abi.PY_TYPE_TUPLE
    original = payload.fields[24]
    assert payload is not original
    message = memory.namespace['py_exc_get_message'](error)
    assert memory.python(message) == 'ascii'
    error = memory.frames['error'].fields[0]
    payload = error.fields[24]
    original = payload.fields[24]
    assert message is not payload
    assert memory.python(original) == ('ascii', 'aé€z', 1, 3, 'ordinal not in range(128)')
    assert payload.fields[40] is original.fields[32]
    assert memory.pin_metric == 0 and memory.graph_depth == 0
    assert len(memory.frames) == 1


@pytest.mark.parametrize('name,index,value', [('encoding',1,'changed'), ('object',2,'replacement'),
                                            ('start',3,0), ('end',4,1), ('reason',5,'reason')])
def test_unicode_attribute_mutation_preserves_original_args(name, index, value):
    memory = UnicodeMemory('graph_unlock')
    memory.make_error()
    error = memory.frames['error'].fields[0]
    replacement = memory.string(value, 0) if isinstance(value, str) else value
    error = memory.frames['error'].fields[0]
    assert memory.namespace['py_unicode_error_set_field'](error, index, replacement) == 0
    error = memory.frames['error'].fields[0]
    args = memory.namespace['py_unicode_error_get_field'](error, 0)
    assert memory.python(args) == ('ascii', 'aé€z', 1, 3, 'ordinal not in range(128)')
    memory.decref(args)
    error = memory.frames['error'].fields[0]
    attribute = memory.namespace['py_unicode_error_get_field'](error, index)
    assert memory.python(attribute) == value
    memory.decref(attribute)
    assert memory.pin_metric == 0 and memory.graph_depth == 0


@pytest.mark.parametrize('text,start,end', [('aé€z',1,3), ('aé€z',1,2), ('A',0,1),
    ('€',0,1), ('😀',0,1), ('\ud800',0,1), ('',0,1), ('é',-1,0), ('é',9,10), ('é',-2**63,1), ('é',0,-2**63)])
@pytest.mark.parametrize('phase', ['allocation', 'string', 'frame_leave', 'graph_unlock'])
def test_unicode_format_matches_qualified_cpython(text, start, end, phase):
    memory = UnicodeMemory(phase)
    memory.make_error(text, start, end)
    oracle = UnicodeEncodeError('ascii', text, start, end, 'ordinal not in range(128)')
    for mode in (0, 1):
        result = memory.format_namespace['py_unicode_error_format'](memory.frames['error'].fields[0], mode)
        assert memory.python(result) == (repr(oracle) if mode else str(oracle))
        memory.decref(result)
        assert memory.pin_metric == 0 and memory.graph_depth == 0


@pytest.mark.parametrize("arguments", [(), ("ascii",), (7, "a", 0, 1, "r"),
                                      ("ascii", b"a", 0, 1, "r"), ("ascii", "a", 0, 1, 7)])
def test_unicode_constructor_validation_matches_qualified_cpython(arguments):
    memory = UnicodeMemory("")
    args = memory.tuple_new(len(arguments))
    for index, value in enumerate(arguments):
        if isinstance(value, str):
            item = memory.string(value, 0)
        elif isinstance(value, bytes):
            item = memory.make(abi.PY_TYPE_BYTES)
        else:
            item = value
        memory.tuple_set(args, index, item)
    assert memory.namespace['py_unicode_encode_error_new'](args) is None
    message = memory.python(memory.error.fields[24])
    with pytest.raises(TypeError) as raised:
        UnicodeEncodeError(*arguments)
    assert message == str(raised.value)
    assert memory.graph_depth == 0 and memory.pin_metric == 0


@pytest.mark.parametrize("case,events,message", [
    ("direct_big", [], "Python int too large to convert to C ssize_t"),
    ("index_big", ["start"], "Python int too large to convert to C ssize_t"),
    ("late_reason", ["start", "end"], "argument 5 must be str, not int"),
])
def test_unicode_index_overflow_and_sequential_validation(case, events, message):
    memory = UnicodeMemory('index')
    start = 2**100
    end = 1
    reason = memory.string('reason', 0)
    if case != 'direct_big':
        start = memory.make(abi.PY_TYPE_INSTANCE)
        start.fields.update(index_label='start', index_value=2**100 if case == 'index_big' else 0)
    if case == 'late_reason':
        end = memory.make(abi.PY_TYPE_INSTANCE)
        end.fields.update(index_label='end', index_value=1)
        reason = 7
    args = memory.tuple_new(5)
    for index, value in enumerate([memory.string('ascii', 0), memory.string('a', 0), start, end, reason]):
        memory.tuple_set(args, index, value)
    assert memory.namespace['py_unicode_encode_error_new'](args) is None
    assert memory.index_events == events
    assert memory.python(memory.error.fields[24]) == message
    assert memory.pin_metric == 0 and memory.graph_depth == 0


@pytest.mark.parametrize('phase', ['string', 'malloc', 'graph_unlock'])
def test_unicode_owned_type_name_projection_survives_relocation(phase):
    memory = UnicodeMemory(phase)
    slots = _Slots(64)
    memory.frames['type_error'] = slots
    slots.fields[24] = 7
    memory.namespace['_unicode_type_error'](slots, 24, 1)
    assert memory.python(memory.error.fields[24]) == 'argument 1 must be str, not int'
    assert slots.fields[16] is None
    assert memory.pin_metric == 0 and memory.graph_depth == 0


def test_unicode_displaced_attribute_finalizer_reenters_outside_graph_lease():
    memory = UnicodeMemory('graph_unlock')
    memory.make_error()
    old = memory.make(abi.PY_TYPE_INSTANCE)
    old.fields['finalizer'] = True
    error = memory.frames['error'].fields[0]
    assert memory.namespace['py_unicode_error_set_field'](error, 5, old) == 0
    error = memory.frames['error'].fields[0]
    old = error.fields[24].fields[24 + 5 * 8]
    memory.decref(old)
    original_decref = memory.decref
    events = []
    def decref(value):
        if isinstance(value, _Object) and value.fields.get('finalizer') and value.references == 1:
            value.flags |= 524288
            assert memory.graph_depth == 0
            error = memory.frames['error'].fields[0]
            current = memory.namespace['py_unicode_error_get_field'](error, 5)
            assert memory.python(current) == 'replacement'
            original_decref(current)
            replacement = memory.string('finalized', 0)
            error = memory.frames['error'].fields[0]
            assert memory.namespace['py_unicode_error_set_field'](error, 1, replacement) == 0
            events.append('finalized')
        original_decref(value)
    memory.namespace['py_decref'] = decref
    memory.decref = decref
    replacement = memory.string('replacement', 0)
    error = memory.frames['error'].fields[0]
    assert memory.namespace['py_unicode_error_set_field'](error, 5, replacement) == 0
    assert events == ['finalized']
    assert memory.graph_depth == 0 and memory.pin_metric == 0


@pytest.mark.parametrize('phase', ['allocation', 'string', 'graph_unlock', 'frame_leave'])
def test_unicode_normalize_preserves_instance_and_argument_tuple(phase):
    memory = UnicodeMemory(phase)
    error = memory.make_error()
    result = memory.namespace['py_unicode_encode_error_normalize'](error)
    assert result is memory.frames['error'].fields[0]
    assert result.references == 2
    memory.decref(result)
    error = memory.frames['error'].fields[0]
    args = memory.namespace['py_unicode_error_get_field'](error, 0)
    root = _Slots(8)
    root.fields[0] = args
    memory.frames['external_args'] = root
    memory.error = None
    result = memory.namespace['py_unicode_encode_error_normalize'](root.fields[0])
    assert result.flags & abi.PY_FLAG_EXC_UNICODE_PAYLOAD
    assert memory.python(result.fields[24].fields[24]) == ('ascii', 'aé€z', 1, 3, 'ordinal not in range(128)')
    memory.decref(result)
    memory.decref(root.fields[0])
    assert memory.pin_metric == 0 and memory.graph_depth == 0


def _unicode_capi_model():
    memory = UnicodeMemory('graph_unlock')
    namespace = memory.namespace.copy()
    symbols = {name: object() for name in ['PyExc_BaseException', 'PyExc_Exception',
        'PyExc_ValueError', 'PyExc_TypeError', 'PyExc_KeyError', 'PyExc_IndexError',
        'PyExc_AttributeError', 'PyExc_RuntimeError', 'PyExc_SystemError', 'PyExc_RecursionError',
        'PyExc_StopIteration', 'PyExc_ZeroDivisionError', 'PyExc_NameError', 'PyExc_NotImplementedError',
        'PyExc_ArithmeticError', 'PyExc_FloatingPointError', 'PyExc_LookupError', 'PyExc_OSError',
        'PyExc_IOError', 'PyExc_OverflowError', 'PyExc_AssertionError', 'PyExc_StopAsyncIteration',
        'PyExc_ReferenceError', 'PyExc_MemoryError', 'PyExc_UnicodeError', 'PyExc_UnicodeDecodeError',
        'PyExc_UnicodeEncodeError', 'PyExc_ImportError', 'PyExc_ModuleNotFoundError']}
    cache = _Slots(65 * 8)
    cache.fields[59 * 8] = memory.cls
    memory.maps['py_exc_classes'] = cache
    memory.frames['class_cache'] = cache
    memory.maps['pcc_capi_unicode_string_owned_map'] = 1
    namespace.update(global_load_ptr=lambda name: symbols.get(name, memory.none),
                     c_abi_typed_export=lambda *_args: lambda function: function,
                     py_unicode_encode_error_normalize=memory.namespace['py_unicode_encode_error_normalize'])
    wanted = {'pcc_capi_exception_tag', 'pcc_capi_exception_class', '_capi_is_unicode_sentinel', '_capi_is_unicode_encode_type',
              '_capi_set_unicode_encode_value', 'PyErr_SetString', 'PyErr_SetNone', 'PyErr_SetObject'}
    tree = ast.parse((ROOT / 'pcc/runtime/py/py_capi_exc_runtime.py').read_text())
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in wanted]
    exec(compile(ast.Module(body=functions, type_ignores=[]), 'capi_unicode_model', 'exec'), namespace)
    return memory, namespace, symbols


def test_unicode_capi_sentinel_mapping_and_normalization():
    memory, api, symbols = _unicode_capi_model()
    for name, tag in [('UnicodeError', 57), ('UnicodeDecodeError', 58), ('UnicodeEncodeError', 59)]:
        assert api['pcc_capi_exception_tag'](symbols['PyExc_' + name]) == tag
    api['PyErr_SetNone'](symbols['PyExc_UnicodeEncodeError'])
    assert memory.python(memory.error.fields[24]) == 'function takes exactly 5 arguments (0 given)'
    memory, api, symbols = _unicode_capi_model()
    api['PyErr_SetString'](symbols['PyExc_UnicodeEncodeError'], 'message')
    assert memory.python(memory.error.fields[24]) == 'function takes exactly 5 arguments (1 given)'
    memory, api, symbols = _unicode_capi_model()
    error = memory.make_error()
    api['PyErr_SetObject'](symbols['PyExc_UnicodeEncodeError'], error)
    assert memory.error is memory.frames['error'].fields[0]
    assert memory.error.flags & abi.PY_FLAG_EXC_UNICODE_PAYLOAD


def test_unicode_capi_owned_ir(tmp_path):
    output = tmp_path / 'py_capi_exc_runtime.ll'
    compile_python(str(ROOT / 'pcc/runtime/py/py_capi_exc_runtime.py'), str(output),
                   backend='self', libpython_mode='off', emit_llvm_only=True, python_library=True)
    text = output.read_text()
    verify_ir_text(text)
    assert 'strict.nolib.stub' not in text
    assert '@py_unicode_encode_error_normalize(' in text


def test_unicode_libpython_callback_display_boundary_source():
    # Static compatibility-boundary evidence only; this is not a libpython
    # execution receipt and does not qualify concurrent callback lifetimes.
    source = (ROOT / 'pcc/runtime/src/py_libpython.c').read_text()
    begin = source.index('static CPyObject *py_cpy_gil_resume_after_callback(',
                         source.index('static CPyObject *py_cpy_gil_resume_after_callback(') + 1)
    body = source[begin:source.index('\n    return result;', begin)]
    assert 'py_exc_get_message' not in body
    assert 'error_roots[1] = py_obj_str(' in body
    assert 'pcc_gc_foreign_lease_acquire(&error_roots[1])' in body
    assert 'memcpy(pcc_message, py_str_utf8(text)' in body
    assert 'pcc_gc_scheduler_root_register_handle(' in body
    assert 'pcc_gc_frame_leave(error_roots)' in body
    assert 'pcc_gc_scheduler_root_unregister_handle(context->pcc_exception_root)' in source


@pytest.mark.parametrize('module', ['py_exc_objects', 'py_format_runtime'])
def test_unicode_runtime_owned_ir(tmp_path, module):
    output = tmp_path / (module + '.ll')
    compile_python(str(ROOT / 'pcc/runtime/py' / (module + '.py')), str(output),
                   backend='self', libpython_mode='off', emit_llvm_only=True, python_library=True)
    text = output.read_text()
    verify_ir_text(text)
    assert not re.search(r'\bcall\b[^\n]*@py_cpy_', text)
    assert 'strict.nolib.stub' not in text
    if module == 'py_exc_objects':
        assert re.search(r'^define[^\n]*@py_unicode_encode_error\(ptr[^,]*, ptr[^,]*, i64[^,]*, i64[^,]*, ptr[^)]*\)', text, re.M)


def test_unicode_static_constructor_uses_full_runtime_call(tmp_path):
    source = tmp_path / 'unicode_constructor.py'
    source.write_text('def make():\n    return UnicodeEncodeError("ascii", "é", 0, 1, "reason")\n')
    output = tmp_path / 'unicode_constructor.ll'
    compile_python(str(source), str(output), backend='self', libpython_mode='off',
                   emit_llvm_only=True, python_library=True)
    text = output.read_text()
    verify_ir_text(text)
    _assert_full_unicode_constructor_body(text)


def _unicode_constructor_function(text):
    match = re.search(
        r'^define[^\n]*@user_unicode_constructor_make\([^\n]*\)[^\n]*\{\n(?P<body>.*?)^\}',
        text, re.M | re.S,
    )
    assert match is not None, 'Unicode constructor function was not emitted'
    return match


def _assert_full_unicode_constructor_body(text):
    body = _unicode_constructor_function(text).group('body')
    assert re.search(r'\bcall\b[^\n]*@py_obj_call\(', body), 'full Unicode constructor call is missing'
    assert not re.search(r'\bcall\b[^\n]*@py_exc_new\(', body), 'legacy message-only Unicode constructor path'


def test_unicode_static_constructor_rejects_legacy_call_inside_function(tmp_path):
    test_unicode_static_constructor_uses_full_runtime_call(tmp_path)
    text = (tmp_path / 'unicode_constructor.ll').read_text()
    function = _unicode_constructor_function(text)
    # Recursion activation can precede entry; inject in the actual first block
    # of this user function rather than depending on a particular label.
    label = re.search(r'^[^\s;][^\n]*:\n', function.group('body'), re.M)
    assert label is not None, 'Unicode constructor has no entry block'
    insertion = function.start('body') + label.end()
    legacy = '  %legacy.unicode.constructor = call ptr (i64, ptr) @py_exc_new(i64 59, ptr null)\n'
    mutated = text[:insertion] + legacy + text[insertion:]
    verify_ir_text(mutated)
    with pytest.raises(AssertionError, match='legacy message-only'):
        _assert_full_unicode_constructor_body(mutated)

    # A message-only exception elsewhere must not trigger this scoped check.
    unrelated = text + '\ndefine ptr @unrelated_exception() {\nentry:\n' + legacy + '  ret ptr %legacy.unicode.constructor\n}\n'
    verify_ir_text(unrelated)
    _assert_full_unicode_constructor_body(unrelated)


CONSTRUCTOR_CONTROL_PROGRAM = r'''import gc
trace = []
class Token:
    def __del__(self):
        trace.append("released")
def fail():
    gc.collect()
    raise ValueError("keyword")
def main():
    for index in range(6):
        try:
            UnicodeEncodeError()
            print("AFTER_INVALID_CONSTRUCTOR")
        except TypeError:
            trace.append("constructor")
        try:
            raise UnicodeEncodeError
        except TypeError:
            trace.append("bare")
        try:
            UnicodeEncodeError("ascii", "a", 0, 1, Token(), unknown=fail())
            print("AFTER_FAILED_KEYWORD")
        except ValueError:
            trace.append("keyword")
    gc.collect()
    print(trace)
main()
'''


def test_unicode_constructor_failure_cleanup_ir(tmp_path):
    source = tmp_path / "unicode_constructor_control.py"
    source.write_text(CONSTRUCTOR_CONTROL_PROGRAM)
    output = tmp_path / "unicode_constructor_control.ll"
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   emit_llvm_only=True, python_library=True)
    text = output.read_text()
    verify_ir_text(text)
    assert "strict.nolib.stub" not in text
    assert not re.search(r"\bcall\b[^\n]*@py_cpy_", text)
    assert "exc.unicode.args" in text
    assert "extern.argument.cleanup" in text
    assert "@py_err_occurred(" in text
    # Bare UnicodeEncodeError must share the constructor path, never synthesize
    # a malformed zero-argument tag59 exception through the message-only ABI.
    assert not re.search(r"\bcall\b[^\n]*@py_exc_new\(i64 59,", text)


PROGRAM = r'''import gc
class Index:
    def __index__(self):
        gc.collect()
        return 1

def main():
    original = "aé€z"
    error = UnicodeEncodeError("ascii", original, 1, 3, "ordinal not in range(128)")
    args = error.args
    print(args)
    print(error.object is original, str(error), repr(error))
    error.encoding = "test"
    error.object = "x😀z"
    error.start = 1
    error.end = 2
    error.reason = "changed"
    gc.collect()
    print(error.args == args, error.object, str(error), repr(error))
    error.encoding = None
    error.reason = 42
    print(str(error))
    for value in (b"abc", None, 7):
        error.object = value
        try:
            str(error)
        except TypeError as bad:
            print(str(bad))
    for field in ("start", "end"):
        try:
            setattr(error, field, "bad")
        except TypeError as bad:
            print(field, str(bad))
    invalid = 0
    for arguments in ((), ("ascii",), ("ascii", "a", 0, 1),
                      (7, "a", 0, 1, "r"), ("ascii", b"a", 0, 1, "r"),
                      ("ascii", "a", 0, 1, 7), ("ascii", "a", "bad", 1, "r")):
        try:
            UnicodeEncodeError(*arguments)
        except TypeError:
            invalid += 1
    try:
        UnicodeEncodeError("ascii", "a", 0, 1, "r", unknown=1)
    except TypeError:
        invalid += 1
    print("invalid", invalid)
    index = Index()
    indexed = UnicodeEncodeError("ascii", original, index, 2, "reason")
    print(indexed.args[2] is index, indexed.start, str(indexed))
    alias = UnicodeEncodeError
    other = alias("ascii", original, 1, 2, "reason")
    try:
        raise other
    except UnicodeEncodeError as caught:
        print(caught is other, caught.object is original, repr(caught))
    for iteration in range(20):
        source = "a" + chr(0xe9) + str(iteration)
        try:
            source.encode("ascii")
        except UnicodeEncodeError as caught:
            del source
            gc.collect()
            print(caught.object, caught.args, str(caught), repr(caught))
    print("UNICODE_PAYLOAD_OK")
main()
'''



def test_unicode_private_flag_has_exact_header_and_export_identity():
    header = (ROOT / "pcc/runtime/src/py_internal.h").read_text()
    flags = re.findall(r"^#define (PY_FLAG_[A-Z_]+) (0x[0-9a-fA-F]+)$", header, re.M)
    own = [(name, int(value, 16)) for name, value in flags if name == "PY_FLAG_EXC_UNICODE_PAYLOAD"]
    assert own == [("PY_FLAG_EXC_UNICODE_PAYLOAD", abi.PY_FLAG_EXC_UNICODE_PAYLOAD)]
    assert abi.PY_FLAG_EXC_UNICODE_PAYLOAD == 0x10000000
    assert all(name == "PY_FLAG_EXC_UNICODE_PAYLOAD" or int(value, 16) & abi.PY_FLAG_EXC_UNICODE_PAYLOAD == 0
               for name, value in flags)
    generator = (ROOT / "scripts/gen_port_abi_constants.py").read_text()
    assert '"PY_FLAG_EXC_UNICODE_PAYLOAD"' in generator
    static = ast.parse((ROOT / "pcc/frontends/python/codegen/port_abi_exports.py").read_text())
    values = []
    for node in ast.walk(static):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "PY_FLAG_EXC_UNICODE_PAYLOAD":
                    values.append(ast.literal_eval(value))
    assert values == [{"kind": "constant", "value_kind": "int", "value": abi.PY_FLAG_EXC_UNICODE_PAYLOAD}]


def test_unicode_payload_program_owned_ir(tmp_path):
    source = tmp_path / "unicode_payload.py"
    source.write_text(PROGRAM)
    output = tmp_path / "unicode_payload.ll"
    compile_python(str(source), str(output), backend="self", libpython_mode="off",
                   emit_llvm_only=True, python_library=True)
    text = output.read_text()
    verify_ir_text(text)
    assert "strict.nolib.stub" not in text
    assert not re.search(r"\bcall\b[^\n]*@py_cpy_", text)


CHAINED_TRACEBACK_PROGRAM = r'''import gc
import traceback
class CollectReason:
    def __str__(self):
        for index in range(3):
            gc.collect()
        return "inner reason after collection"
def main():
    for iteration in range(12):
        inner = UnicodeEncodeError("ascii", "é", 0, 1, "inner")
        inner.reason = CollectReason()
        outer = UnicodeEncodeError("ascii", "€", 0, 1, "outer reason")
        try:
            raise outer from inner
        except UnicodeEncodeError:
            text = traceback.format_exc()
            print("inner reason after collection" in text, "outer reason" in text)
    print("UNICODE_CHAINED_TRACEBACK_OK")
main()
'''


@pytest.mark.integration
@pytest.mark.parametrize('program', [CONSTRUCTOR_CONTROL_PROGRAM, CHAINED_TRACEBACK_PROGRAM],
                         ids=['constructor_cleanup', 'chained_traceback_collection'])
def test_unicode_exception_control_native_all_collectors(tmp_path, pcc_diagnostic_runtime_archive,
                                                          python_program_compiler, monkeypatch, program):
    monkeypatch.setenv('PCC_PYTHON_IR_PASSES', 'off')
    path = tmp_path / 'unicode_control.py'
    path.write_text(program)
    oracle = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, timeout=15)
    assert oracle.returncode == 0, oracle.stderr
    binary = tmp_path / 'unicode_control'
    python_program_compiler(str(path), str(binary), backend='self', libpython_mode='off',
                            runtime_archive=str(pcc_diagnostic_runtime_archive))
    for backend in range(5):
        actual = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
                                capture_output=True, text=True, timeout=30)
        assert actual.returncode == 0, (backend, actual.stdout, actual.stderr)
        assert actual.stdout == oracle.stdout, (backend, actual.stdout, oracle.stdout)


@pytest.mark.integration
def test_unicode_payload_native_all_collectors(tmp_path, pcc_diagnostic_runtime_archive,
                                               python_program_compiler, monkeypatch):
    monkeypatch.setenv('PCC_PYTHON_IR_PASSES', 'off')
    path = tmp_path / 'unicode_payload.py'
    path.write_text(PROGRAM)
    oracle = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, timeout=15)
    assert oracle.returncode == 0, oracle.stderr
    binary = tmp_path / 'unicode_payload'
    python_program_compiler(str(path), str(binary), backend='self', libpython_mode='off',
                            runtime_archive=str(pcc_diagnostic_runtime_archive))
    for backend in range(5):
        actual = subprocess.run([str(binary)], env=dict(os.environ, PCC_GC_BACKEND=str(backend)),
                                capture_output=True, text=True, timeout=30)
        assert actual.returncode == 0, (backend, actual.stdout, actual.stderr)
        assert actual.stdout == oracle.stdout, (backend, actual.stdout, oracle.stdout)
