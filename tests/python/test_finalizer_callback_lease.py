"""Finalizers borrow a temporarily live owner; terminal leases stay forbidden."""
from __future__ import annotations

from pathlib import Path
import textwrap

import pytest

from tests.python.test_foreign_address_leases import Model, _functions
from tests.python.owned_regression_support import assert_owned_program, explicit_owned_runtime

RUNTIME = Path(__file__).resolve().parents[2] / 'pcc/runtime/py'
TERMINAL = 524288


class FinalizerModel(Model):
    def load(self, base, offset=0):
        return super().load(base + offset, 0) if isinstance(base, int) else super().load(base, offset)

    def store(self, base, offset, value):
        if isinstance(base, int):
            super().store(base + offset, 0, value)
        else:
            super().store(base, offset, value)

    def __init__(self, backend, *, terminal=True, resurrect=False, pending=False):
        super().__init__(backend)
        self.self_slot = self.object(flags=TERMINAL if terminal else 0, tag=104)
        self.store(1000, 0, 0 if terminal else 2)
        self.store(1000, 16, 2000)
        self.store('pcc_class_del_defined_count', 0, 1)
        self.store(4000, 0, 1)
        self.store(4000, 12, 64)
        self.pending = 4000 if pending else 0
        self.resurrect = resurrect
        self.marked = []
        self.store('pcc_gc_object_head', 0, self.nodes[1000])
        self.store(self.nodes[1000], 0, 1000)
        self.calls = 0
        self.next_slot = 50000
        self.ns.update(C_POINTER_SIZE=8, PY_TYPE_GEN=15, PY_TYPE_INSTANCE=11,
                       PY_TYPE_USER_CLASS_START=104, c_ptr=int,
                       cstr=lambda value: value, ptr_add=lambda base, offset: base + offset,
                       stack_alloc=self.alloc,
                       pcc_capi_is_cext_type_tag=lambda tag: 0,
                       _load_instance_cls=lambda value: self.load(value, 16),
                       py_class_lookup=lambda cls, name: 3000,
                       pcc_refcount_incref=lambda value: self.ref(value, 1),
                       pcc_refcount_decref=lambda value: self.ref(value, -1),
                       py_incref=lambda value: self.ref(value, 1),
                       py_decref=lambda value: self.ref(value, -1),
                       pcc_gc_mark_root_gray_if_known=self.marked.append,
                       pcc_gc_visit_registered_root_slots=lambda *args: None,
                       pcc_gc_pin=lambda value: self.store(value, 12, self.load(value, 12) | 64),
                       pcc_gc_unpin=lambda value: self.store(value, 12, self.load(value, 12) & ~64),
                       atomic_rmw_i32=lambda op, value, offset, bits, order: self.store(value, offset, self.load(value, offset) | bits),
                       py_current_exception=lambda: self.pending,
                       py_tls_exc_set=self.set_exception,
                       py_clear_exception=lambda: self.set_exception(0),
                       _call_user_unary_method_void=self.callback,
                       pcc_diagnostics_runtime_log_event_code=lambda *args: None,
                       pcc_platform_abort=lambda: pytest.fail('finalizer owner protocol abort'))
        _functions(RUNTIME / 'py_dunder.py', {
            '_finalizer_owner_enter', '_finalizer_owner_leave', 'py_user_del_dispatch'
        }, self.ns)
        _functions(RUNTIME / 'freestanding_gc_object_root_seeding.py', {
            'pcc_gc_gray_current_roots'
        }, self.ns)
        self.on_unlock = self.boundary

    def alloc(self, size):
        self.next_slot += size
        return self.next_slot

    def ref(self, value, change):
        count = self.load(value, 0) + change
        assert count >= 0
        self.store(value, 0, count)
        return count

    def set_exception(self, value):
        self.pending = value

    def boundary(self):
        assert self.pinned() == 1 and self.count() == 1
        assert self.load(1000, 0) > 0
        assert not self.load(1000, 12) & TERMINAL
        self.marked.clear()
        self.ns['pcc_gc_gray_current_roots']()
        assert self.marked == [1000]

    def callback(self, func, value):
        assert self.depth == 0
        self.calls += 1
        self.boundary()
        # Execute the real lease body for a callback's self argument. A legacy
        # helper's unpin must not undo the independent finalizer address lease.
        token = self.acquire(self.self_slot)
        assert token == 1 and self.count() == 2
        self.store(value, 12, self.load(value, 12) & ~64)
        assert self.release(self.self_slot, token) == 0
        self.boundary()
        self.store(value, 12, self.load(value, 12) | 32)
        if self.resurrect:
            self.ref(value, 1)
        self.pending = 6000  # A finalizer error must not replace caller TLS.


@pytest.mark.parametrize('backend', range(5))
@pytest.mark.parametrize('scenario', ('dealloc', 'resurrect', 'cycle', 'pending-error'))
def test_actual_finalizer_body_preserves_owner_leases_flags_and_exception(backend, scenario):
    m = FinalizerModel(backend, terminal=scenario != 'cycle',
                       resurrect=scenario == 'resurrect', pending=scenario == 'pending-error')
    before = m.load(1000, 0)
    m.ns['py_user_del_dispatch'](1000)
    assert m.calls == 1 and m.depth == 0
    assert m.count() == 0 and m.active() == 0
    assert m.load(1000, 0) == before + (scenario == 'resurrect')
    flags = m.load(1000, 12)
    assert flags & 4 and flags & 32
    assert bool(flags & TERMINAL) == (scenario in ('dealloc', 'pending-error'))
    assert m.pending == (4000 if scenario == 'pending-error' else 0)
    if scenario == 'pending-error':
        assert m.load(4000, 0) == 1 and m.load(4000, 12) & 64
    m.ns['py_user_del_dispatch'](1000)
    assert m.calls == 1  # The finalized bit survives the callback transition.


def test_terminal_object_still_rejects_an_ordinary_lease():
    m = FinalizerModel(4)
    m.on_unlock = None
    assert m.acquire(m.self_slot) == -1
    assert m.active() == 0 and m.load(1000, 0) == 0


def test_counted_lease_is_a_production_collector_root_without_a_frame():
    m = FinalizerModel(4)
    token = m.ns['_finalizer_owner_enter'](m.self_slot)
    m.boundary()
    m.ns['_finalizer_owner_leave'](m.self_slot, token, TERMINAL)
    assert m.load(1000, 0) == 0 and m.load(1000, 12) & TERMINAL
    assert m.count() == 0 and m.active() == 0


PROGRAM = textwrap.dedent('''\
    import gc
    import weakref
    events = []
    references = []
    rescued = []
    class Token:
        def __del__(self):
            events.append('token')
    class Value:
        def __init__(self, name, truth=False):
            self.name = name
            self.truth = truth
        def __bool__(self):
            events.append(self.name)
            gc.collect()
            return self.truth
        def __del__(self):
            events.append('dispose:' + self.name)
            gc.collect()
    class Rescue:
        def __init__(self, name):
            self.name = name
        def __del__(self):
            events.append(self.name)
            rescued.append(self)
            gc.collect()
    def drop(*, value, **extras):
        references.append(weakref.ref(value))
    def take(*, value, **extras):
        gc.collect()
        return value
    def rhs(value):
        events.append('rhs')
        gc.collect()
        return value
    def main():
        token = Token()
        ref = weakref.ref(token)
        del token
        gc.collect()
        assert ref() is None and events == ['token']
        events.clear()
        value = Value('direct')
        ref = weakref.ref(value)
        del value
        gc.collect()
        assert ref() is None and events == ['dispose:direct']
        events.clear()
        drop(value=Value('argument'))
        gc.collect()
        assert references[0]() is None and events == ['dispose:argument']
        events.clear()
        selected = {'identity': 42}
        assert take(value=Value('temporary') or rhs(selected)) is selected
        assert events == ['temporary', 'dispose:temporary', 'rhs']
        events.clear()
        rescued_value = Rescue('resurrected')
        del rescued_value
        gc.collect()
        assert events == ['resurrected']
        assert rescued[0].name == 'resurrected'
        rescued.clear()
        gc.collect()
        assert events == ['resurrected']
        print('FINALIZER_CALLBACK_LEASE_OK')
    main()
''')


@pytest.mark.integration
@pytest.mark.parametrize('python_program_compiler', ('pcc0', 'pcc1'), indirect=True)
def test_finalizer_callback_owner_native_five_gc(python_program_compiler, request,
                                                explicit_owned_runtime, tmp_path, capfd):
    mode = request.node.callspec.params['python_program_compiler']
    assert_owned_program(PROGRAM, 'FINALIZER_CALLBACK_LEASE_OK\n', tmp_path,
                         python_program_compiler, mode, explicit_owned_runtime,
                         capfd, provenance_probe='2')
    assert (tmp_path / 'compiler-wrapper.stderr').read_text() == ''
