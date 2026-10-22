"""Aborted unexposed definitions retire only their owning namespace edge.

Execute the production abort/slot-plan/refcount bodies. These host models and
IR checks do not qualify the unchanged native weakref/finalizer program.
"""
from __future__ import annotations

import ast
import contextlib
import gc
import hashlib
import io
import os
from pathlib import Path
import re
import runpy
import subprocess
import weakref

import pytest

from pcc.runtime.py import py_abi_constants as abi
from test_class_metaclass_retirement import Retirement, load_functions
from test_class_namespace_default_roots import _emit


ROOT = Path(__file__).parents[2]
ORIGINAL = ROOT / 'tests/fixtures/class_namespace_failed_definition.py'
ORIGINAL_SHA256 = 'b9e71d62dc6304d5bea13ee88c3c8c3da7165a6feca09c09607f7f0c096d048b'


class DefinitionAbort(Retirement):
    def __init__(self, backend, references, relocate=False, relocate_before=False):
        super().__init__(backend, references)
        self.slot = self.cls + abi.PYCLASSOBJECT_ATTRS_OFFSET
        self.words[self.cls + abi.PYCLASSOBJECT_METACLASS_OFFSET] = 0
        self.words[self.slot] = self.meta if references else 0
        self.words[self.cls + abi.PYOBJECTHEADER_TYPE_TAG_OFFSET] = abi.PY_TYPE_CLASS
        self.words[self.cls + abi.PYOBJECTHEADER_FLAGS_OFFSET] = abi.PY_FLAG_IMMORTAL
        self.words[self.meta + abi.PYOBJECTHEADER_TYPE_TAG_OFFSET] = abi.PY_TYPE_DICT
        self.class_slot, self.methods = 9000, 16000
        self.words[self.class_slot] = self.cls
        self.words[self.cls + abi.PYCLASSOBJECT_METHODS_OFFSET] = self.methods
        self.words[self.cls + abi.PYCLASSOBJECT_N_METHODS_OFFSET] = 2
        self.words[self.cls + abi.PYCLASSOBJECT_DEL_METHOD_OFFSET] = 71000
        for index in range(2):
            self.words[self.methods + index * abi.PYCLASSMETHOD_SIZE + abi.PYCLASSMETHOD_FUNC_OFFSET] = 71000 + index * 8
        self.structural = {offset: 60000 + offset for offset in (
            abi.PYCLASSOBJECT_BASES_OFFSET, abi.PYCLASSOBJECT_N_BASES_OFFSET,
            abi.PYCLASSOBJECT_MRO_OFFSET, abi.PYCLASSOBJECT_N_MRO_OFFSET,
            abi.PYCLASSOBJECT_FIELD_NAMES_OFFSET, abi.PYCLASSOBJECT_N_FIELDS_OFFSET,
            abi.PYCLASSOBJECT_INSTANCE_SIZE_OFFSET, abi.PYCLASSOBJECT_TYPE_TAG_ALLOC_OFFSET,
        )}
        for offset, value in self.structural.items():
            self.words[self.cls + offset] = value
        self.globals.update(py_class_attr_cache_epoch=8300, pcc_class_definition_abort_error_map=8400)
        self.words[8300] = 17
        self.frames = set()
        self.pending = 'original ValueError: later default'
        self.original = self.pending
        self.next_stack = 20000
        self.relocate = relocate
        self.relocate_before = relocate_before
        self.retired_class = None
        self.class_moved = False
        self.finalized = []
        model = self

        class Token:
            def __del__(self):
                assert not model.locked
                assert model.words[model.slot] == 0
                assert model.words[8300] > 17
                assert all(model.words[address] == 0 for address in model.aliases)
                model.finalized.append('token')
                model.pending = 'finalizer exception'
                gc.collect()

        token = Token() if references else None
        self.reference = weakref.ref(token) if references else lambda: None
        self.namespace = {'fields': token} if references else {}
        self.default_owner = [token] if references else []
        self.aliases = [self.methods + index * abi.PYCLASSMETHOD_SIZE + abi.PYCLASSMETHOD_FUNC_OFFSET for index in range(2)]
        self.env.update(
            _CLASS_DEFINITION_ABORT_PLAN_BYTES=128,
            stack_alloc=self.allocate,
            pcc_gc_frame_enter=self.enter,
            pcc_gc_frame_leave=self.leave,
            py_tls_exc_swap_slot=self.swap,
            py_clear_exception=self.clear,
            atomic_rmw_i32=self.atomic,
        )
        load_functions(ROOT / 'pcc/runtime/py/freestanding_class_namespace.py',
                       ('_class_namespace_source_locked', 'pcc_class_abort_definition_slots'), self.env)
        load_functions(ROOT / 'pcc/runtime/py/py_class.py',
                       ('py_class_abort_definition_slots',), self.env)

    def allocate(self, size):
        self.next_stack += size + 64
        return self.next_stack

    def load(self, base, offset):
        old = getattr(self, 'retired_class', None)
        assert old is None or not old <= base + offset < old + abi.PYCLASSOBJECT_SIZE, 'stale class address after safepoint'
        return super().load(base, offset)

    def enter(self, frame_map, slot):
        assert frame_map == 8400 and self.load(slot, 0) == 0
        self.frames.add(slot)
        if self.relocate_before:
            old = self.cls
            self.cls = 5120
            for offset in range(0, abi.PYCLASSOBJECT_SIZE, 4):
                self.words[self.cls + offset] = self.words.get(old + offset, 0)
            self.words[self.class_slot] = self.cls
            self.slot = self.cls + abi.PYCLASSOBJECT_ATTRS_OFFSET
            self.retired_class = old
            self.relocate_before = False

    def leave(self, slot):
        assert self.load(slot, 0) == 0
        self.frames.remove(slot)

    def swap(self, slot):
        assert slot in self.frames and not self.locked
        old = self.load(slot, 0)
        self.store(slot, 0, self.pending)
        self.pending = old

    def clear(self):
        assert not self.locked
        self.pending = 0

    def atomic(self, operation, pointer, offset, value, order):
        assert self.locked and (operation, pointer, offset, value, order) == ('add', 8300, 0, 1, 'release')
        old = self.words[pointer]
        self.words[pointer] += 1
        return old

    def lock(self):
        assert not self.locked and self.pending == 0
        self.locked = True
        self.events.append(('lock', self.backend))

    def unlock(self):
        assert self.locked and self.words[self.slot] == 0
        self.locked = False
        self.events.append(('unlock', self.backend))
        if self.relocate:
            old = self.cls
            self.cls = 4096
            for offset in range(0, abi.PYCLASSOBJECT_SIZE, 4):
                self.words[self.cls + offset] = self.words.get(old + offset, 0)
            self.words[self.class_slot] = self.cls
            self.slot = self.cls + abi.PYCLASSOBJECT_ATTRS_OFFSET
            self.retired_class = old
            self.class_moved = True
        if self.references == 2:
            for offset in (0, 8, 12):
                self.words[self.moved + offset] = self.words[self.meta + offset]
            self.retired_address = self.meta
        elif self.references == 1:
            assert self.words[self.meta + abi.PYOBJECTHEADER_FLAGS_OFFSET] & 524288

    def barrier(self, owner, slot, value):
        assert self.locked and (owner, slot, value) == (self.cls, self.slot, 0)
        self.events.append(('barrier', self.backend))

    def deallocate(self, value, tag):
        assert not self.locked and self.words[self.slot] == 0
        assert (value, tag) == (self.meta, abi.PY_TYPE_DICT)
        assert self.words[value] == 0
        assert self.words[value + abi.PYOBJECTHEADER_FLAGS_OFFSET] & 524288
        self.events.append(('deallocate', value))
        self.namespace.clear()

    def abort(self):
        # Failed later-default evaluation first releases the earlier default's
        # temporary owner. The class namespace is its remaining strong owner.
        self.default_owner.clear()
        self.env['py_class_abort_definition_slots'](self.class_slot)


@pytest.mark.parametrize('backend', range(5))
@pytest.mark.parametrize('references', [0, 1, 2], ids=['empty', 'terminal', 'shared'])
@pytest.mark.parametrize('relocate', [False, True], ids=['stationary', 'move-after-unlock'])
def test_abort_actual_plans_preserve_error_structure_and_shared_namespace(backend, references, relocate):
    model = DefinitionAbort(backend, references, relocate)
    model.abort()
    assert not model.locked and not model.frames
    assert model.pending == model.original
    assert model.words[model.slot] == 0
    assert model.words[model.cls + abi.PYOBJECTHEADER_FLAGS_OFFSET] == abi.PY_FLAG_IMMORTAL
    assert {offset: model.words[model.cls + offset] for offset in model.structural} == model.structural
    assert all(model.words[address] == 0 for address in model.aliases)
    assert model.words[model.cls + abi.PYCLASSOBJECT_DEL_METHOD_OFFSET] == 0
    assert model.finalized == (['token'] if references == 1 else [])
    if references == 1:
        assert model.reference() is None
    elif references == 2:
        assert model.reference() is model.namespace['fields']
        assert model.words[model.moved] == 1
    # Repeating an abort cannot consume the detached owner twice.
    model.relocate = False
    model.env['py_class_abort_definition_slots'](model.class_slot)
    assert model.pending == model.original and not model.frames
    assert model.finalized == (['token'] if references == 1 else [])


def test_default_failure_reaches_abort_before_class_owner_release():
    text = _emit('''def make():
    return object()
def fail():
    raise ValueError('later default')
def construct():
    class Broken:
        fields = make()
        def target(self, value=fields, later=fail()):
            return value
    return Broken
''')
    body = re.search(r'^define [^\n]*@user_class_namespace_construct\([^\n]*\).*?^}', text, re.M | re.S).group(0)
    assert '@py_class_abort_definition_slots(' in body
    blocks = dict(re.findall(r'^([\w.$]+):\n(.*?)(?=^[\w.$]+:|^})', body, re.M | re.S))
    rollback = next(value for value in blocks.values() if '@py_class_abort_definition_slots(' in value)
    target = re.search(r'br label %([\w.$]+)', rollback)[1]
    assert 'class.body.unwind' in target
    assert '@pcc_gc_release_known(' in blocks[target]
    assert '@pcc_gc_store_root(' in blocks[target]
    assert not re.search(r'store ptr [^\n]+, ptr @\.classattr\.', body)


def test_set_name_exposure_disarms_definition_abort_before_callback():
    text = _emit('''escaped = []
class Descriptor:
    def __set_name__(self, owner, name):
        escaped.append(owner)
class Owner:
    value = Descriptor()
''')
    callback = re.search(r'call [^\n]*@user_class_namespace_Descriptor___set_name__\(', text)
    assert callback is not None
    before = text[:callback.start()]
    assert re.search(r'store i1 0, ptr %class.definition.abort[^\n]*', before)


def test_custom_metaclass_result_has_no_definition_abort():
    text = _emit('''class Meta(type):
    pass
def construct():
    class Supplied(metaclass=Meta):
        value = object()
    return Supplied
''')
    body = re.search(r'^define [^\n]*@user_class_namespace_construct\([^\n]*\).*?^}', text, re.M | re.S).group(0)
    assert '@py_class_abort_definition_slots(' not in body


@pytest.mark.parametrize('backend', range(5))
def test_abort_reloads_class_root_after_entry_safepoint(backend):
    model = DefinitionAbort(backend, 1, relocate_before=True)
    model.abort()
    assert model.cls == 5120 and model.words[model.slot] == 0
    assert model.reference() is None and model.finalized == ['token']
    assert model.pending == model.original and not model.frames


@pytest.mark.parametrize('value', [0, 71000], ids=['null', 'nonclass'])
def test_abort_invalid_value_preserves_unrelated_class(value):
    model = DefinitionAbort(0, 0)
    before = dict(model.words)
    model.words[model.class_slot] = value
    model.env['py_class_abort_definition_slots'](model.class_slot)
    for address in model.aliases:
        assert model.words[address] == before[address]
    assert model.words[8300] == 17
    assert model.pending == model.original and not model.frames


def _verify_abort_ir_transaction(body):
    blocks = dict(re.findall(r'^([\w.$]+):\n(.*?)(?=^[\w.$]+:|^})', body, re.M | re.S))
    pending = [('entry', False, 0, 0)]
    visited = set()
    commits_seen = 0
    while pending:
        state = pending.pop()
        if state in visited:
            continue
        visited.add(state)
        name, locked, commits, finishes = state
        for line in blocks[name].splitlines():
            if '@pcc_py_gc_minor_graph_lock(' in line:
                assert not locked
                locked = True
            elif '@pcc_py_gc_minor_graph_unlock(' in line:
                assert locked
                locked = False
            elif '@pcc_gc_store_ptr_plan_commit_locked(' in line:
                assert locked and commits == 0 and finishes == 0
                commits += 1
                commits_seen += 1
            elif '@pcc_gc_store_ptr_plan_finish(' in line:
                assert not locked and finishes == 0
                finishes += 1
            elif line.strip().startswith('br '):
                pending.extend((target, locked, commits, finishes)
                               for target in re.findall(r'label %([\w.$]+)', line))
            elif line.strip().startswith('ret '):
                assert not locked and finishes == 1
    assert commits_seen


def test_abort_freestanding_source_emits_unboxed_transaction(tmp_path, monkeypatch):
    from pcc.frontends.python.pipeline import compile_python
    source = ROOT / 'pcc/runtime/py/freestanding_class_namespace.py'
    output = tmp_path / 'class_namespace.ll'
    monkeypatch.setenv('PCC_PYTHON_IR_PASSES', 'off')
    monkeypatch.setenv('PCC_WITH_THREADS', '1')
    compile_python(str(source), str(output), emit_llvm_only=True,
                   backend='self', libpython_mode='off', ir_scaffold_mode='on',
                   python_library=True)
    text = output.read_text()
    matches = re.findall(r'^define [^\n]*@pcc_class_abort_definition_slots\([^\n]*\).*?^}', text, re.M | re.S)
    assert len(matches) == 1
    body = matches[0]
    _verify_abort_ir_transaction(body)
    assert '@py_int_' not in body and '@py_obj_add(' not in body
    assert '@py_tls_exc_swap_slot(' not in body
    with pytest.raises(AssertionError):
        _verify_abort_ir_transaction(body.replace('@pcc_py_gc_minor_graph_lock(', '@missing_lock('))
    with pytest.raises(AssertionError):
        _verify_abort_ir_transaction(body.replace('@pcc_py_gc_minor_graph_unlock(', '@missing_unlock('))


def test_original_failure_program_preserves_reference_and_ir_root_contract():
    from ir_pointer_aliases import canonical_pointer, pointer_bitcast_aliases
    assert hashlib.sha256(ORIGINAL.read_bytes()).hexdigest() == ORIGINAL_SHA256
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        runpy.run_path(str(ORIGINAL), run_name='__main__')
    assert output.getvalue() == 'CLASS_NAMESPACE_FAILURE_OK\n'
    text = _emit(ORIGINAL.read_text())
    assert '@py_class_abort_definition_slots(' in text
    body = re.search(r'^define [^\n]*@user_class_namespace_construct\([^\n]*\).*?^}', text, re.M | re.S).group(0)
    blocks = dict(re.findall(r'^([\w.$]+):\n(.*?)(?=^[\w.$]+:|^})', body, re.M | re.S))
    aliases = pointer_bitcast_aliases(body)
    pending, visited, returns, aborts = [('entry', ())], set(), 0, 0
    while pending:
        state = pending.pop()
        if state in visited:
            continue
        visited.add(state)
        name, incoming = state
        stack = list(incoming)
        for line in blocks[name].splitlines():
            entered = re.search(r'@pcc_gc_frame_enter_lifo\(ptr [^,]+, ptr (%[\w.$]+)\)', line)
            left = re.search(r'@pcc_gc_frame_leave_lifo\(ptr (%[\w.$]+)\)', line)
            if entered:
                root = canonical_pointer(entered[1], aliases)
                assert root not in stack
                stack.append(root)
            if left:
                root = canonical_pointer(left[1], aliases)
                assert stack and stack.pop() == root
            if '@py_class_abort_definition_slots(' in line:
                # The class remains rooted while namespace finalizers run.
                assert stack and 'class.body' in stack[-1]
                aborts += 1
            if line.strip().startswith('br '):
                pending.extend((target, tuple(stack)) for target in re.findall(r'label %([\w.$]+)', line))
            if line.strip().startswith('ret '):
                assert not stack
                returns += 1
    assert returns and aborts


@pytest.mark.integration
def test_native_original_failure_program(tmp_path, pcc_runtime_archive, python_program_compiler):
    assert hashlib.sha256(ORIGINAL.read_bytes()).hexdigest() == ORIGINAL_SHA256
    binary = tmp_path / 'ordinary_failure'
    python_program_compiler(str(ORIGINAL), str(binary), backend='self',
                            libpython_mode='off', runtime_archive=str(pcc_runtime_archive))
    for backend in range(5):
        environment = dict(os.environ, PCC_GC_BACKEND=str(backend))
        environment.pop('LC_ALL', None)
        result = subprocess.run([str(binary)], capture_output=True, text=True,
                                timeout=30, env=environment)
        assert result.returncode == 0, (backend, result.stdout, result.stderr)
        assert result.stdout == 'CLASS_NAMESPACE_FAILURE_OK\n', (backend, result.stdout, result.stderr)
