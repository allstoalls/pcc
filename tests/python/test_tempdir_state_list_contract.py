"""TemporaryDirectory state against the actual owned list implementation.

The production list and tempfile function bodies execute unchanged. Raw memory,
GC operations and filesystem effects are deterministic host models; these tests
are not native execution, relocation, concurrency or resource-limit evidence.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pcc.runtime.py import py_abi_constants as abi
from tests.python.test_owned_tempfile_provider import Obj, Ptr, ROOT, Runtime
from tests.python.test_owned_windows_tempdir import WindowsRuntime

LIST_SOURCE = ROOT / 'pcc/runtime/py/py_list.py'
LIST_FUNCTIONS = {
    '_debug_bad_container', '_ptr_can_have_header', '_ptr_is_list',
    '_list_is_sane', '_normalize_index', '_prepare_moving_root',
    '_reload_moving_root', '_finish_moving_root', '_grow_if_needed',
    '_list_factory_finish', 'py_list_new', '_append_rooted', 'py_list_append',
    '_list_get_owned_rooted', '_list_get_finish', 'py_list_get',
    '_list_set_item_transaction', 'py_list_set', 'py_list_setitem', 'py_list_len',
}


class ActualLists:
    """Run real list control flow over the existing nonmoving memory model.

    No list capacity/length/get/set/append semantics are reimplemented here.
    GC plans record the actual stores but do not emulate a moving collector.
    TemporaryDirectory reserves four elements, so no payload growth is needed.
    """
    def __init__(self, runtime, backend=0, threads=0):
        self.runtime = runtime
        self.backend, self.threads = backend, threads
        self.lists = []
        self.stores = []
        self.lock_depth = 0
        ns = runtime.ns
        ns.update({
            'pcc_gc_backend': lambda: self.backend,
            'pcc_threads_enabled': lambda: self.threads,
            'pcc_gc_alloc': self.allocate,
            'pcc_gc_pointer_is_managed': lambda p: int(isinstance(p, (Ptr, Obj))),
            'pcc_gc_foreign_lease_acquire': self.acquire,
            'pcc_gc_note_slot_write_barrier': lambda *args: None,
            'pcc_gc_backend4_zpage_register_owner_payload_span': lambda *args: 0,
            'pcc_gc_publish_initialized': lambda *args: None,
            'py_gc_track': lambda *args: None,
            'pcc_gc_store_ptr': self.store,
            'pcc_gc_store_ptr_plan_init': lambda *args: None,
            'pcc_gc_store_ptr_plan_finish': lambda *args: None,
            'pcc_gc_store_ptr_plan_commit_locked': self.commit,
            'pcc_gc_retain_plan_prepare_locked': lambda plan, value: value,
            'pcc_gc_retain_plan_finish': lambda *args: None,
            'pcc_py_gc_minor_graph_lock': self.lock,
            'pcc_py_gc_minor_graph_unlock': self.unlock,
            'pcc_platform_abort': lambda: pytest.fail('unexpected GC abort'),
            '_pcc_debug_bad_incref': lambda *args: pytest.fail('invalid list'),
        })
        selected = []
        for node in ast.parse(LIST_SOURCE.read_text()).body:
            if isinstance(node, ast.FunctionDef) and node.name in LIST_FUNCTIONS:
                node.decorator_list = []
                selected.append(node)
        assert {node.name for node in selected} == LIST_FUNCTIONS
        exec(compile(ast.Module(selected, type_ignores=[]), str(LIST_SOURCE), 'exec'), ns)

    def allocate(self, size, tag, flags):
        assert size == abi.PYLISTOBJECT_SIZE and tag == abi.PY_TYPE_LIST
        value = self.runtime.alloc(size)
        self.runtime.write(value, abi.PYOBJECTHEADER_TYPE_TAG_OFFSET, tag)
        self.runtime.write(value, abi.PYOBJECTHEADER_FLAGS_OFFSET, flags)
        self.lists.append(value)
        return value

    def acquire(self, slot):
        return self.runtime.copy_lease(slot, slot)

    def lock(self):
        self.lock_depth += 1

    def unlock(self):
        assert self.lock_depth > 0
        self.lock_depth -= 1

    def store(self, owner, slot, value):
        self.stores.append((owner, slot.offset, value))
        self.runtime.write(slot, 0, value)

    def commit(self, plan, owner, slot, value):
        assert self.lock_depth > 0
        self.store(owner, slot, value)
        return 1

    def items(self, state):
        count = self.runtime.read(state, abi.PYLISTOBJECT_LENGTH_OFFSET)
        data = self.runtime.read(state, abi.PYLISTOBJECT_ITEMS_OFFSET)
        return [self.runtime.read(data, 8 * i) for i in range(count)]

    def assert_released(self):
        assert self.lock_depth == 0
        assert not self.runtime.frames and not self.runtime.leases
        assert not self.runtime.handles


def make_runtime(platform, tmp_path, backend=0, threads=0):
    runtime = WindowsRuntime() if platform == 'windows' else Runtime()
    lists = ActualLists(runtime, backend, threads)
    root = runtime.cwd if platform == 'windows' else tmp_path
    return runtime, lists, root


def assert_no_directory(runtime, root):
    if isinstance(runtime, WindowsRuntime):
        assert not runtime.roots[root].children
        assert not runtime.live
    else:
        assert not list(Path(root).iterdir())
    assert runtime.global_load('pcc_tempdir_records') is None


@pytest.mark.parametrize('backend', range(5))
@pytest.mark.parametrize('threads', (0, 1))
def test_list_capacity_is_not_length_and_internal_access_is_nonraising(backend, threads):
    runtime = Runtime()
    lists = ActualLists(runtime, backend, threads)
    ns = runtime.ns
    state = ns['py_list_new'](4)
    assert ns['py_list_len'](state) == 0
    assert runtime.read(state, abi.PYLISTOBJECT_CAPACITY_OFFSET) == 4
    for index in range(4):
        assert ns['py_list_set'](state, index, runtime.true) is None
        assert ns['py_list_get'](state, index) is None
    assert runtime.error is None and ns['py_list_len'](state) == 0
    values = [runtime.string('name'), runtime.false, runtime.true, runtime.none]
    for value in values:
        ns['py_list_append'](state, value)
        assert runtime.error is None
    assert lists.items(state) == values
    assert ns['py_list_len'](state) == 4
    assert ns['py_list_get'](state, -1) is runtime.none
    assert ns['py_list_setitem'](state, 3, runtime.false) == 0
    assert ns['py_list_get'](state, 3) is runtime.false
    lists.assert_released()


@pytest.mark.parametrize('platform', ('posix', 'windows'))
@pytest.mark.parametrize('backend', range(5))
@pytest.mark.parametrize('threads', (0, 1))
def test_constructor_publishes_four_real_list_items(tmp_path, platform, backend, threads):
    runtime, lists, root = make_runtime(platform, tmp_path, backend, threads)
    manager = runtime.create(root)
    assert runtime.error is None
    state = manager.attrs['_pcc_finalizer_state']
    values = lists.items(state)
    assert len(values) == 4
    assert values[:3] == [manager.attrs['name'], runtime.false, runtime.true]
    assert values[3].kind == 'weakref' and values[3].value() is manager
    assert runtime.method(manager, 'enter') is values[0]
    assert runtime.method(manager, 'cleanup') is runtime.none
    assert runtime.error is None
    assert lists.items(state)[3] is runtime.none
    assert_no_directory(runtime, root)
    lists.assert_released()


@pytest.mark.parametrize('platform', ('posix', 'windows'))
@pytest.mark.parametrize('failure_at', (1, 2, 3, 4))
@pytest.mark.parametrize('failure', ('pending', 'silent'))
def test_partial_append_failure_rolls_back_before_finalizer_publication(
        tmp_path, platform, failure_at, failure):
    runtime, lists, root = make_runtime(platform, tmp_path, backend=4)
    append = runtime.ns['py_list_append']
    primary = Obj('exception', (19, 'injected append error'))
    calls = []

    def append_with_failure(state, item):
        calls.append(item)
        if len(calls) == failure_at:
            if failure == 'pending':
                runtime.error = primary
            return
        append(state, item)

    runtime.ns['py_list_append'] = append_with_failure
    runtime.ns['py_func_new_named'] = lambda *args: pytest.fail('partial state published')
    runtime.ns['py_weakref_new'] = lambda *args: pytest.fail('partial state weakref')
    runtime.create(root, delete=False)
    assert runtime.error is not None
    if failure == 'pending':
        assert runtime.error is primary
        assert len(calls) == failure_at
    else:
        assert runtime.error.value == (19, 'TemporaryDirectory finalizer state initialization failed')
        assert len(calls) == 4
    assert len(lists.items(lists.lists[0])) < 4
    assert_no_directory(runtime, root)
    lists.assert_released()


@pytest.mark.parametrize('platform', ('posix', 'windows'))
@pytest.mark.parametrize('boundary', ('malloc', 'root', 'mutex', 'weakref_install', 'attribute'))
def test_initialization_failure_cleans_owner_and_state_cycle(tmp_path, platform, boundary):
    runtime, lists, root = make_runtime(platform, tmp_path, backend=4)
    register = runtime.ns['_td_register']
    primary = Obj('exception', (19, 'injected attribute error'))
    registration_snapshots = []

    def register_with_failure(state, owner):
        registration_snapshots.append(lists.items(state))
        assert lists.items(state)[3] is runtime.none
        if boundary not in ('malloc', 'root', 'mutex'):
            return register(state, owner)
        name = {'malloc': 'malloc', 'root': 'pcc_gc_scheduler_root_register_handle',
                'mutex': '_td_mutex'}[boundary]
        original = runtime.ns[name]
        runtime.ns[name] = lambda *args: None
        try:
            return register(state, owner)
        finally:
            runtime.ns[name] = original

    runtime.ns['_td_register'] = register_with_failure
    if boundary == 'weakref_install':
        setter = runtime.ns['py_list_setitem']

        def fail_store(state, index, value):
            original = runtime.ns['pcc_gc_store_ptr_plan_commit_locked']
            runtime.ns['pcc_gc_store_ptr_plan_commit_locked'] = lambda *args: 0
            try:
                return setter(state, index, value)
            finally:
                runtime.ns['pcc_gc_store_ptr_plan_commit_locked'] = original

        runtime.ns['py_list_setitem'] = fail_store
    elif boundary == 'attribute':
        runtime.ns['py_obj_setattr'] = lambda *args: setattr(runtime, 'error', primary) or -1
    manager = runtime.create(root, delete=False)
    assert len(registration_snapshots) == 1
    assert runtime.error is not None
    if boundary == 'attribute':
        assert runtime.error is primary
    if boundary == 'weakref_install':
        assert runtime.error.value == (5, 'list assignment index out of range')
    state = lists.lists[0]
    assert lists.items(state)[3] is runtime.none
    assert '_pcc_finalizer_state' not in manager.attrs
    assert_no_directory(runtime, root)
    lists.assert_released()


@pytest.mark.parametrize('platform', ('posix', 'windows'))
@pytest.mark.parametrize('boundary', ('header', 'items', 'lease'))
@pytest.mark.parametrize('pending', (False, True))
def test_actual_list_factory_failure_rolls_back_directory(tmp_path, platform, boundary, pending):
    runtime, lists, root = make_runtime(platform, tmp_path, backend=4)
    factory = runtime.ns['py_list_new']
    primary = Obj('exception', (19, 'injected list factory error'))

    def fail_factory(capacity):
        name = {'header': 'pcc_gc_alloc', 'items': 'malloc',
                'lease': 'pcc_gc_foreign_lease_acquire'}[boundary]
        original = runtime.ns[name]

        def fail(*args):
            if pending:
                runtime.error = primary
            return -1 if boundary == 'lease' else None

        runtime.ns[name] = fail
        try:
            return factory(capacity)
        finally:
            runtime.ns[name] = original

    runtime.ns['py_list_new'] = fail_factory
    runtime.ns['py_func_new_named'] = lambda *args: pytest.fail('failed list published')
    runtime.ns['py_weakref_new'] = lambda *args: pytest.fail('failed list weakref')
    runtime.create(root, delete=False)
    if pending:
        assert runtime.error is primary
    else:
        assert runtime.error.value == (19, 'list: out of memory')
    assert_no_directory(runtime, root)
    lists.assert_released()


@pytest.mark.parametrize('platform', ('posix', 'windows'))
def test_length_check_preserves_its_pending_exception(tmp_path, platform):
    runtime, lists, root = make_runtime(platform, tmp_path, backend=4)
    primary = Obj('exception', (19, 'injected length error'))

    def fail_length(state):
        runtime.error = primary
        return 0

    runtime.ns['py_list_len'] = fail_length
    runtime.ns['py_weakref_new'] = lambda *args: pytest.fail('failed length weakref')
    runtime.create(root, delete=False)
    assert runtime.error is primary
    assert_no_directory(runtime, root)
    lists.assert_released()


@pytest.mark.parametrize('platform', ('posix', 'windows'))
@pytest.mark.parametrize('backend', (0, 4))
@pytest.mark.parametrize('delete', (False, True))
@pytest.mark.parametrize('operation', ('context', 'finalizer', 'shutdown'))
def test_real_state_cleanup_policies(tmp_path, platform, backend, delete, operation):
    runtime, lists, root = make_runtime(platform, tmp_path, backend)
    manager = runtime.create(root, delete=delete)
    assert runtime.error is None
    state = manager.attrs['_pcc_finalizer_state']
    name = manager.attrs['name'].value
    if operation == 'context':
        body_error = Obj('exception', (2, 'context body error'))
        assert runtime.method(manager, 'exit', runtime.string('ValueError'),
                              body_error, runtime.none) is runtime.none
        assert body_error.value == (2, 'context body error')
    elif operation == 'finalizer':
        runtime.ns['_td_finalize_state'](state)
    else:
        runtime.ns['_td_shutdown']()
    assert runtime.error is None
    exists = runtime.lookup(name) is not None if platform == 'windows' else Path(name).exists()
    assert exists is not delete
    if operation == 'context' and not delete:
        # The context deliberately keeps delete=False alive; explicit cleanup
        # still owns the directory and must use the populated original state.
        assert runtime.method(manager, 'cleanup') is runtime.none
        assert_no_directory(runtime, root)
    assert lists.items(state)[3] is runtime.none
    lists.assert_released()


@pytest.mark.parametrize('platform', ('posix', 'windows'))
def test_constructor_keeps_primary_exception_across_rollback(tmp_path, platform):
    runtime, lists, root = make_runtime(platform, tmp_path, backend=4)
    primary = Obj('exception', (19, 'constructor primary'))
    secondary = Obj('exception', (19, 'cleanup secondary'))
    runtime.ns['py_obj_setattr'] = lambda *args: setattr(runtime, 'error', primary) or -1
    name = '_td_windows_cleanup' if platform == 'windows' else '_td_remove'
    cleanup = runtime.ns[name]

    def cleanup_with_error(*args):
        result = cleanup(*args)
        runtime.error = secondary
        return result

    runtime.ns[name] = cleanup_with_error
    runtime.create(root, delete=False)
    assert runtime.error is primary
    assert lists.items(lists.lists[0])[3] is runtime.none
    assert_no_directory(runtime, root)
    lists.assert_released()


@pytest.mark.parametrize('target', (
    'aarch64-unknown-linux-gnu', 'arm64-apple-darwin', 'x86_64-pc-windows-msvc',
))
def test_finalizer_state_initialization_in_actual_runtime_ir(tmp_path, monkeypatch, target):
    import re

    from pcc.frontends.python.owned_runtime_build import (
        _compile_runtime_module, runtime_ir_passes,
    )
    from pcc.ir.optimization.driver import optimize_ir
    from tests.owned_ir_validation import verify_ir_text

    monkeypatch.setenv('PCC_WITH_THREADS', '0')
    monkeypatch.setenv('PCC_REFCOUNT_KIND', 'atomic')
    source = ROOT / 'pcc/runtime/py/py_tempfile.py'
    output = tmp_path / 'py_tempfile.ll'
    _compile_runtime_module('py_tempfile', str(source), str(output), target)
    raw = output.read_text()
    optimized = optimize_ir(raw, runtime_ir_passes(str(ROOT / 'pcc/runtime')))
    for text in (raw, optimized):
        verify_ir_text(text)
        match = re.search(r'^define[^\n]*@pcc_tempdir_init_entry\([^\n]*\).*?^}',
                          text, re.M | re.S)
        assert match
        body = match.group(0)
        assert body.count('@py_list_append(') == 4
        assert body.count('@py_list_len(') == 1
        assert body.count('@py_list_setitem(') == 1
        assert '@py_list_set(' not in body
        assert body.index('@user_py_tempfile__td_register(') < body.index('@py_list_setitem(')
        assert '@py_tls_exc_swap_slot(' in body
        assert '@user_py_tempfile__td_hold(' in body
        assert '@user_py_tempfile__td_windows_cleanup(' in body
